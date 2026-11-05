"""Rank ungraded EVM candidates by usable observations, never by vendor labels.

This only orders research. Buy/sell counts are NOT proof of closed episodes or a
grade. Existing scores and removed roster members are left to their existing paths.
"""
from __future__ import annotations

import json

from kaiba.intelligence.grade import HARD_QUARANTINE_TAGS, _tags_from
from kaiba.intelligence.wallet_campaign import key


def provider_candidates(conn, chain, *, attempts, now_ms, limit=1000):
    if chain not in ('bsc', 'robinhood'):
        raise ValueError('EVM provider route only')
    if not 0 <= limit <= 1000:
        raise ValueError('candidate cap is 1000 per chain')
    if not limit:
        return []
    rows = conn.execute("""
        WITH feed AS (
            SELECT wallet,COUNT(*) feed_count,SUM(side='buy') buys,SUM(side='sell') sells
            FROM swaps WHERE chain=? AND source IN ('gmgn:smartmoney','gmgn:kol')
            GROUP BY wallet
        )
        SELECT w.chain,w.address,w.tags_json,w.last_seen_ms,
            COALESCE(f.feed_count,0) feed_count,COALESCE(f.buys,0) buys,
            COALESCE(f.sells,0) sells,t.status watch_status
        FROM wallets w LEFT JOIN wallet_scores s USING(chain,address)
        LEFT JOIN feed f ON f.wallet=w.address
        LEFT JOIN hub_addresses h USING(chain,address)
        LEFT JOIN clustering_coverage c USING(chain,address)
        LEFT JOIN tracker_watchlist t USING(chain,address)
        WHERE w.chain=? AND s.address IS NULL AND COALESCE(w.cohort,'')!='blacklist'
        AND h.address IS NULL AND COALESCE(c.status,'') NOT IN ('hub','quarantined')
        AND (t.status IS NULL OR t.status='active')
        ORDER BY (COALESCE(f.buys,0)>0 AND COALESCE(f.sells,0)>0) DESC,
            (COALESCE(f.feed_count,0)>0) DESC,
            (COALESCE(t.status,'')='active') DESC,w.last_seen_ms DESC,w.address
    """, (chain, chain))
    selected = []
    seen = set()
    for raw in rows:
        r = dict(raw)
        identity = key(chain, r['address'])
        if identity is None or identity in seen:
            continue
        seen.add(identity)
        if now_ms-attempts.get(f"{chain}:{r['address']}", 0) < 86_400_000:
            continue
        try:
            tags = json.loads(r['tags_json'] or '[]')
            if not isinstance(tags, list) or set(_tags_from(tags)) & HARD_QUARANTINE_TAGS:
                continue
        except (TypeError, ValueError):
            continue
        reason = ('observed_two_sided_feed' if r['buys'] and r['sells']
                  else 'observed_feed' if r['feed_count'] else 'registry_recency')
        selected.append(dict(chain=chain, address=r['address'], prior_grade=None,
                             prior_score=None, scored_at_ms=None,
                             route='provider_arithmetic_candidate_only',
                             priority_basis=reason, observed_feed_buys=r['buys'],
                             observed_feed_sells=r['sells']))
        if len(selected) >= limit:
            break
    return selected
