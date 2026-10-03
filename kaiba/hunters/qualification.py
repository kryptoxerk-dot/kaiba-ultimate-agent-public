"""Fail-closed research qualification, separate from scraped leads and trade authority.

The qualification receipt is an explicit enrichment contract. A scraper's fresh
updated_ms, a token's existence or a fallback EV never proves eligibility.
"""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, now_ms
from kaiba.hunters.ev import hunter_config

FAMILIES = {'nft': ('nft_mint',), 'airdrop': ('airdrop', 'points')}
VERIFY_FIELDS = ('official_source_verified', 'eligibility_verified', 'value_cost_verified', 'execution_path_verified', 'participation_open')


def _object(value: Any) -> dict[str, Any]:
    try:
        obj = json.loads(value) if isinstance(value, str) else value
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def qualification(row: dict[str, Any], *, at_ms: int, ev_floor: Decimal, max_age_s: int = 86400) -> dict[str, Any]:
    reasons = []
    evidence = _object(row.get('evidence_json'))
    meta = _object(evidence.get('meta'))
    verification = _object(meta.get('qualification'))
    plan = _object(row.get('plan_json'))
    if row.get('status') != 'open' or plan.get('refused') is True:
        reasons.append('not_open_or_refused')
    if evidence.get('kind') != row.get('kind') or evidence.get('chain') != row.get('chain'):
        reasons.append('evidence_identity_unverified')
    value = _number(row.get('ev_score'))
    if value is None:
        reasons.append('ev_unavailable')
    elif value < ev_floor:
        reasons.append('ev_below_floor')
    cost = _number(row.get('cost_usd'))
    if cost is None or cost < 0:
        reasons.append('cost_unavailable')
    try:
        Chain(row.get('chain'))
    except (ValueError, TypeError):
        reasons.append('chain_unresolved_or_unsupported')
    link = evidence.get('official_url')
    parsed = urlparse(link) if isinstance(link, str) else None
    if parsed is None or parsed.scheme != 'https' or not parsed.netloc:
        reasons.append('official_source_unavailable')
    for field in VERIFY_FIELDS:
        if verification.get(field) is not True:
            reasons.append(field + '_missing')
    observed = _number(verification.get('observed_ms'))
    if observed is None or not 0 <= Decimal(at_ms) - observed <= max_age_s * 1000:
        reasons.append('verified_evidence_stale_or_unavailable')
    # Legacy NFT deadline_ms was populated from LAUNCH START. Never label that
    # value an expiry. An explicit sale/participation end is a separate fact.
    if row.get('kind') == 'nft_mint':
        deadline = _number(verification.get('sale_end_ms'))
    else:
        deadline = _number(row.get('deadline_ms'))
    if deadline is not None and deadline <= at_ms:
        reasons.append('participation_deadline_passed')
    estimate = _number(evidence.get('expected_value_usd'))
    history = evidence.get('prior_airdrop_history')
    valid_history = isinstance(history, list) and bool(history) and all(
        (value := _number(item)) is not None and value > 0 for item in history
    )
    has_value = estimate is not None and estimate > 0
    if estimate is None:
        has_value = valid_history
    if row.get('kind') == 'nft_mint':
        resale = _number(verification.get('realisable_value_usd'))
        has_value = has_value or (resale is not None and resale > 0)
    if not has_value:
        reasons.append('reward_or_exit_value_unsourced')
    wallets = evidence.get('wallet_count')
    if isinstance(wallets, bool) or wallets != 1:
        reasons.append('single_wallet_required')
    return {'qualified': not reasons, 'reasons': reasons, 'classification': 'qualified_research' if not reasons else 'unqualified_lead', 'funded_action_authorized': False}


def opportunity_report(conn, *, kind: str | None = None, limit: int = 15, include_unqualified: bool = False, at_ms: int | None = None) -> dict[str, Any]:
    at_ms = now_ms() if at_ms is None else at_ms
    sql = 'SELECT * FROM opportunities'
    args = []
    if kind:
        kinds = FAMILIES.get(kind, (kind,))
        sql += ' WHERE kind IN (' + ','.join('?' for _ in kinds) + ')'
        args.extend(kinds)
    sql += ' ORDER BY COALESCE(ev_score,-1e9) DESC,opportunity_id'
    rows = fetch_all(conn, sql, args)
    floor = hunter_config().ev_threshold_usd
    checked = [(r, qualification(r, at_ms=at_ms, ev_floor=floor)) for r in rows]
    eligible = [(r, q) for r, q in checked if q['qualified'] or include_unqualified]
    fields = ('opportunity_id', 'kind', 'name', 'chain', 'status', 'ev_score', 'cost_usd', 'deadline_ms', 'updated_ms', 'source')
    selected = []
    for row, verdict in eligible[:max(0, int(limit))]:
        item = {key: row.get(key) for key in fields}
        item['qualification'] = verdict
        if row.get('kind') == 'nft_mint':
            item['stored_date_semantics'] = 'legacy value may be launch start; not verified sale expiry'
        selected.append(item)
    return {'opportunities': selected, 'registry_count': len(rows), 'qualified_count': sum(q['qualified'] for _, q in checked), 'returned_count': len(selected), 'has_more': len(eligible) > len(selected), 'ev_floor_usd': str(floor), 'view': 'audit' if include_unqualified else 'qualified_research', 'unqualified_reason_counts': dict(Counter(reason for _, q in checked for reason in q['reasons'])), 'note': 'Research qualification is not authorization to trade, mint, claim or transfer.'}
