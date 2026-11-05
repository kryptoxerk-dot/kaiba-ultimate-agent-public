"""Additive campaign admission helpers; no grade, trust, or roster-removal authority.

Only existing GMGN-feed cohort nominations use the existing cohort admission policy.
A provider PnL profile is not evidence that a feed covers that wallet. Missing local
shape evidence remains a blocker. Unknown execution quality stays explicit at observe.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter

from kaiba.core.schemas import HARD_QUARANTINE_TAGS
from kaiba.intelligence import grade
from kaiba.intelligence.wallet_campaign import CHAINS, MAX_EVIDENCE_AGE_MS, key, summarize

TARGET_PER_CHAIN = 5_000

SELECT_ROWS = """SELECT s.*,w.address registry_address,w.tags_json,w.cohort,c.status cluster_status,
    h.address hub_address,t.status watch_status,t.meta_json watch_meta,
    EXISTS(SELECT 1 FROM swaps x WHERE x.chain=s.chain AND x.wallet=s.address
        AND x.source IN ('gmgn:smartmoney','gmgn:kol')) feed_observed
    FROM wallet_scores s LEFT JOIN wallets w USING(chain,address)
    LEFT JOIN clustering_coverage c USING(chain,address)
    LEFT JOIN hub_addresses h USING(chain,address)
    LEFT JOIN tracker_watchlist t USING(chain,address)
    WHERE s.grade IN ('A','B') AND s.chain IN ('bsc','sol','robinhood')"""


def rejection(row, audits, *, now_ms):
    if 'registry_address' in row and row['registry_address'] is None:
        return 'missing_registry_risk_evidence'
    if row.get('hub_address') or row.get('cluster_status') in ('hub', 'quarantined'):
        return 'hub_or_cluster_quarantine'
    if row.get('cohort') == 'blacklist':
        return 'blacklist'
    try:
        tags = json.loads(row.get('tags_json') or '[]')
        if not isinstance(tags, list):
            return 'invalid_registry_tags'
        if set(grade._tags_from(tags)) & HARD_QUARANTINE_TAGS:
            return 'hard_quarantine_tag'
    except (TypeError, ValueError):
        return 'invalid_registry_tags'
    if not summarize([row], audits, now_ms=now_ms)['eligible_chain_address_pairs']:
        return 'evidence_requires_review'
    if now_ms-int(row['scored_at_ms']) > MAX_EVIDENCE_AGE_MS:
        return 'stale_grade_requires_refresh'
    return None


def inventory(rows, audits, *, now_ms, target_per_chain=TARGET_PER_CHAIN):
    if target_per_chain < 1:
        raise ValueError('target must be positive')
    raw, filtered, qualified, active = (Counter() for _ in range(4))
    excluded = Counter()
    active_addresses = set()
    candidates = []
    seen, duplicate_keys = set(), set()
    for row in rows:
        identity = key(row['chain'], row['address'])
        if identity in seen:
            duplicate_keys.add(identity)
        seen.add(identity)
    for row in rows:
        chain = row['chain']
        is_active = row.get('watch_status') == 'active'
        raw[chain] += int(is_active)
        why = rejection(row, audits, now_ms=now_ms)
        if key(chain, row['address']) in duplicate_keys:
            why = 'duplicate_identity'
        if why not in ('hub_or_cluster_quarantine', 'blacklist', 'hard_quarantine_tag',
                        'invalid_registry_tags', 'missing_registry_risk_evidence', 'duplicate_identity'):
            filtered[chain] += int(is_active)
        if why:
            excluded[why] += 1
            continue
        qualified[chain] += 1
        active[chain] += int(is_active)
        if is_active:
            active_addresses.add(row['address'] if chain == 'sol' else row['address'].lower())
        else:
            candidates.append(row)
    return {
        'observed_ms': now_ms, 'target_per_chain': target_per_chain,
        'target_chain_wallet_records': target_per_chain*len(CHAINS),
        'raw_active_ab': {c: raw[c] for c in CHAINS},
        'active_ab_after_risk_exclusions': {c: filtered[c] for c in CHAINS},
        'qualified_inventory': {c: qualified[c] for c in CHAINS},
        'qualified_active': {c: active[c] for c in CHAINS},
        'remaining_active_per_chain': {c: max(0, target_per_chain-active[c]) for c in CHAINS},
        'qualified_active_distinct_addresses': len(active_addresses),
        'inventory_target_reached': all(active[c] >= target_per_chain for c in CHAINS),
        'completion_requires_independent_audit': True,
        'excluded': dict(excluded),
    }, candidates


def admission_blocker(row):
    # An earlier removal is an operator/screener decision; this additive pass cannot undo it.
    if row.get('watch_status') is not None:
        return 'existing_roster_row_preserved'
    if not row.get('feed_observed'):
        return 'no_existing_feed_coverage'
    return None


def activate_offers(writer, offers, audits, *, now_ms):
    """Recheck candidates under each write lock; an admission failure rolls back its batch.

    Caller owns a second, non-scanning connection. Existing rows are never updated,
    including removed rows. Live tracker admission supplies the append-only audit log.
    """
    from kaiba.core.db import tx
    from kaiba.core.schemas import Chain
    from kaiba.intelligence import tracker

    out = dict(attempted=len(offers), inserted=0, verified=0, preserved=0,
               store_failed=0, added=[], errors=[])
    for offset in range(0, len(offers), 25):
        batch = offers[offset:offset+25]
        added, preserved = [], 0
        try:
            with tx(writer):
                for offer in batch:
                    original = offer['row']
                    identity = (original['chain'], original['address'])
                    current = writer.execute(SELECT_ROWS + ' AND s.chain=? AND s.address=?', identity).fetchone()
                    row = dict(current) if current else None
                    if (row != original or not 0 <= now_ms-offer['screened_ms'] <= 300_000
                            or row is None or admission_blocker(row)
                            or rejection(row, audits, now_ms=now_ms)):
                        preserved += 1
                        continue
                    screen = tracker.Screen(chain=Chain(identity[0]), address=identity[1],
                                            **{k: v for k, v in offer['screen'].items() if k != 'model'})
                    decision = tracker.cohort_verdict(screen)
                    if (not decision.admissible or screen.grade is None or screen.grade.value != row['grade']
                            or screen.score != row['score']):
                        preserved += 1
                        continue
                    entry = tracker.admit(
                        Chain(identity[0]), identity[1], conn=writer, screen=screen, verdict=decision,
                        source=tracker.COHORT_SOURCE_PREFIX+'feed_only', added_by='codex:wallet-campaign',
                        reason=f"Owner requested graded watchlist expansion; {row['model_version']} "
                               f"{row['grade']} {row['score']}; existing GMGN feed observed. "
                               "Observe only; execution failure rate unknown; feed coverage incomplete.",
                    )
                    if entry is None:
                        raise RuntimeError('tracker admission refused or failed')
                    check = writer.execute('SELECT * FROM tracker_watchlist WHERE chain=? AND address=?', identity).fetchone()
                    if (not check or check['status'] != 'active' or check['tier'] != 'observe'
                            or json.loads(check['meta_json']).get('helius_poll') is not False):
                        raise RuntimeError('watchlist readback or paid-poll invariant failed')
                    added.append(dict(chain=identity[0], address=identity[1]))
            out['inserted'] += len(added)
            out['verified'] += len(added)
            out['preserved'] += preserved
            out['added'].extend(added)
        except (sqlite3.Error, RuntimeError) as exc:
            out['store_failed'] += len(batch)
            out['errors'].append(type(exc).__name__ + ': ' + str(exc))
    return out
