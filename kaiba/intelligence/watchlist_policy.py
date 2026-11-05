"""Owner-scoped active-watchlist quality policy; never changes wallet grades or trust.

Kaiba-operator owns this policy seam. A missing policy preserves legacy research
behavior; an enabled or malformed policy fails closed to fresh measured A/B rows.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, now_ms

POLICY_KEY = 'tracker:active_grade_policy'


def policy(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = fetch_one(conn, 'SELECT value FROM kv WHERE key=?', (POLICY_KEY,))
    if row is None:
        return None
    try:
        value = json.loads(row['value'])
        if not isinstance(value, dict):
            raise ValueError('policy must be an object')
        if value.get('enabled') is False:
            return None
        if value.get('enabled') is not True or value.get('allowed_grades') != ['A', 'B']:
            raise ValueError('invalid A/B policy')
        age = value.get('max_grade_age_s')
        if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
            raise ValueError('missing finite grade-age budget')
        return value
    except (TypeError, ValueError):
        return {'enabled': True, 'invalid': True}


_SELECT = (
    'SELECT s.address,s.grade,s.evidence_weight,s.scored_at_ms,'
    'w.cohort,c.status AS cluster_status FROM wallet_scores s '
    'LEFT JOIN wallets w ON w.chain=s.chain AND w.address=s.address '
    'LEFT JOIN clustering_coverage c ON c.chain=s.chain AND c.address=s.address '
)


def _row_rejection(row: dict[str, Any] | None, rule: dict[str, Any], stamp: int) -> str | None:
    if rule.get('invalid'):
        return 'active watchlist policy invalid; admission refused'
    if row is None or row['grade'] not in {'A', 'B'}:
        actual = row['grade'] if row is not None else 'missing'
        return f'owner A/B-only watchlist: current grade {actual}'
    try:
        weight = float(row['evidence_weight'])
        scored = int(row['scored_at_ms'])
    except (TypeError, ValueError, OverflowError):
        return 'owner A/B-only watchlist: malformed grade evidence'
    if not math.isfinite(weight) or weight < 30:
        return 'owner A/B-only watchlist: insufficient measured evidence'
    if not 0 <= stamp - scored <= rule['max_grade_age_s'] * 1000:
        return 'owner A/B-only watchlist: stale or future grade'
    if row.get('cohort') == 'blacklist' or row.get('cluster_status') == 'quarantined':
        return 'owner A/B-only watchlist: wallet or cluster quarantined'
    return None


def rejection(conn: sqlite3.Connection, chain: Chain, address: str) -> str | None:
    rule = policy(conn)
    if rule is None:
        return None
    row = fetch_one(conn, _SELECT + 'WHERE s.chain=? AND s.address=?', (chain.value, address))
    return _row_rejection(row, rule, now_ms())


def filter_addresses(conn: sqlite3.Connection, chain: Chain, addresses: set[str]) -> set[str]:
    """Re-check downgrades in the detection path with one grade query, not N reads."""
    rule = policy(conn)
    if rule is None:
        return addresses
    if rule.get('invalid') or not addresses:
        return set()
    stamp = now_ms()
    eligible = {
        row['address'] for row in fetch_all(
            conn, _SELECT + "WHERE s.chain=? AND s.grade IN ('A','B')", (chain.value,)
        ) if _row_rejection(row, rule, stamp) is None
    }
    return addresses & eligible


def reconcile_active(conn: sqlite3.Connection, *, actor: str) -> dict[str, Any]:
    """Retire ineligible active rows, retaining grades, raw evidence and audit history."""
    from kaiba.core.db import tx
    from kaiba.intelligence import tracker

    rule = policy(conn)
    if rule is None:
        return {'enabled': False, 'removed': 0, 'by_reason': {}}
    rows = fetch_all(conn, "SELECT chain,address FROM tracker_watchlist WHERE status='active'")
    removed = 0
    reasons: dict[str, int] = {}
    for row in rows:
        chain, address = Chain(row['chain']), row['address']
        # Re-read the current grade under the write transaction: a concurrent
        # improvement must not be removed based on an older shortlist snapshot.
        with tx(conn):
            why = rejection(conn, chain, address)
            if why and tracker.remove(chain, address, reason=why, actor=actor, conn=conn):
                removed += 1
                reasons[why] = reasons.get(why, 0) + 1
    return {'enabled': True, 'removed': removed, 'by_reason': reasons,
            'target_wallets': rule.get('target_wallets')}
