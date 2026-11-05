"""Read-only audit of original candidate books, including retired arms.

Run ``python -m kaiba.learning.candidate_pair_audit --state-dir PATH``. PATH is
the autolearn directory containing paper/ and research-allocation/. No worker,
canonical database, provider, scoring, promotion or execution is invoked.

An operational report is NOT a book inventory: retired arms have no book_path.
Use the original allocation receipt and initialization seal, bound by authority.
This checks their hashes and exact book identities, not the entire runtime's
execution authorization or immutable archive byte history. Reads use a common
close-time frontier but separate short transactions; this is not a global DB
snapshot. Concurrent late writes that break the closed/fill join are refused.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

VERSION = 'candidate-pair-audit/1'
MAX_BYTES = 64 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def checked_path(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_refused')
    require(path.resolve() == path, 'noncanonical_path')
    require(path.name != 'kaiba.db', 'canonical_db_refused')
    return path


def read_artifact(path):
    path = checked_path(path)
    before = path.stat()
    require(path.is_file() and before.st_size <= 2 * 1024 * 1024, 'artifact_size')
    with path.open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    after = path.stat()
    require(len(raw) <= 2 * 1024 * 1024 and
            (before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), 'artifact_changed')
    value = json.loads(raw)
    require(isinstance(value, dict), 'artifact_shape')
    return value, hashlib.sha256(raw).hexdigest()


def inventory(state_dir):
    state = checked_path(state_dir)
    folder = state / 'research-allocation'
    artifacts = {name: read_artifact(folder / name) for name in
                 ('authority.json', 'receipt.json', 'new-books.json')}
    authority, receipt, seal = (artifacts[n][0] for n in
                                ('authority.json', 'receipt.json', 'new-books.json'))
    receipt_hash = digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
    seal_hash = digest({k: v for k, v in seal.items() if k != 'seal_sha256'})
    require(receipt_hash == receipt.get('receipt_sha256') == authority.get('receipt_sha256')
            == seal.get('receipt_sha256'), 'receipt_hash_mismatch')
    require(seal_hash == seal.get('seal_sha256') == authority.get('new_books_sha256'), 'seal_hash_mismatch')
    legacy, new = receipt.get('legacy_books'), seal.get('new_books')
    require(isinstance(legacy, list) and isinstance(new, list), 'inventory_shape')
    records = legacy + new
    require(0 < len(records) <= 32 and len(records) == receipt.get('lifetime_family_size'), 'inventory_count')
    require(all(r.get('lease') in ('retired', 'drain_only') for r in legacy), 'legacy_lease')
    expected_old = {(m['manifest']['campaign_id'], a['hypothesis_id'])
                    for m in receipt['legacy_manifests'] for a in m['manifest']['hypotheses']}
    expected_new = {(receipt['new_manifest']['campaign_id'], a['hypothesis_id'])
                    for a in receipt['new_manifest']['hypotheses']}
    require({(r['campaign_id'], r['hypothesis_id']) for r in legacy} == expected_old and
            {(r['campaign_id'], r['hypothesis_id']) for r in new} == expected_new, 'manifest_inventory_mismatch')
    require(len(expected_old | expected_new) == len(records), 'duplicate_arm_inventory')
    require(len({r['book'] for r in records}) == len(records), 'duplicate_book_inventory')
    for record in records:
        expected = 'candidate-' + digest([record['campaign_id'], record['hypothesis_id']])[:32] + '.db'
        require(record['book'] == expected, 'book_name_mismatch')
        identity = record.get('identity')
        require(isinstance(identity, dict) and identity.get('campaign_id') == record['campaign_id']
                and identity.get('hypothesis_id') == record['hypothesis_id'], 'inventory_identity')
    return records, {n: h for n, (_, h) in artifacts.items()}


class Reader:
    def __init__(self, paper_root):
        self.root = checked_path(paper_root)
        self.queries = self.total = 0
        self.durations = []

    def query(self, path, sql, limit, params=()):
        path = checked_path(path)
        require(path.parent == self.root and re.fullmatch(r'candidate-[0-9a-f]{32}\.db', path.name), 'book_path')
        require(path.is_file() and path.stat().st_nlink == 1, 'book_missing_or_hardlinked')
        wal = checked_path(Path(str(path) + '-wal'))
        require(not wal.exists() or wal.stat().st_size <= 1_500_000_000, 'wal_limit')
        began = time.monotonic()
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: time.monotonic() - began > 2, 1000)
            conn.execute('PRAGMA query_only=ON')
            rows = [dict(r) for r in conn.execute(sql + ' LIMIT ' + str(limit + 1), params).fetchall()]
            require(len(rows) <= limit, 'row_limit')
        finally:
            conn.close()
            self.durations.append(time.monotonic() - began)
        self.queries += 1
        require(self.durations[-1] <= 2.5, 'query_deadline')
        self.total += len(json.dumps(rows))
        require(self.total <= MAX_BYTES, 'retrieval_limit')
        return rows


def audit(state_dir, *, frontier_ms=None):
    frontier = int(time.time() * 1000) if frontier_ms is None else frontier_ms
    require(type(frontier) is int and frontier > 0, 'frontier_invalid')
    state = checked_path(state_dir)
    records, artifact_hashes = inventory(state)
    paper_root = state / 'paper'
    reader = Reader(paper_root)
    groups = defaultdict(list)
    counts = Counter()
    field_counts = Counter()
    arms = []
    for record in records:
        path = paper_root / record["book"]
        identities = reader.query(path, "SELECT value FROM candidate_meta WHERE key='identity'", 1)
        require(len(identities) == 1, 'book_identity_missing')
        identity = json.loads(identities[0]['value'])
        require(identity == record['identity'], 'book_identity_mismatch')
        foreign = reader.query(path, "SELECT 1 FROM orders WHERE mode!='shadow' OR provider!='paper'", 1)
        require(not foreign, 'foreign_inventory')
        closed = reader.query(path, '''SELECT t.position_id,t.chain,t.token,t.opened_ms,t.closed_ms,t.cost_native,
            t.proceeds_native,t.mode,e.signal_id,p.qty_total FROM trades t JOIN candidate_entries e
            ON e.position_id=t.position_id JOIN positions p ON p.position_id=t.position_id
            WHERE t.closed_ms<=? ORDER BY t.closed_ms,t.trade_id''', 4096, (frontier,))
        actual_count = reader.query(path, 'SELECT COUNT(*) AS n FROM trades WHERE closed_ms<=?', 1, (frontier,))
        require(actual_count[0]['n'] == len(closed), 'unmatched_closed_rows_or_concurrent_append')
        legs = reader.query(path, '''SELECT p.position_id,o.side,o.amount_in,o.filled_out,o.fee_native,o.created_ms,
            m.value AS evidence,(SELECT detail FROM order_events ev WHERE ev.order_id=o.order_id
              AND detail LIKE '%"basis"%' ORDER BY id DESC LIMIT 1) AS basis
            FROM position_orders p JOIN orders o ON p.order_id=o.order_id
            JOIN trades t ON t.position_id=p.position_id
            LEFT JOIN candidate_meta m ON m.key='fill_evidence:'||o.order_id
            WHERE o.state='filled' AND t.closed_ms<=? ORDER BY p.position_id,o.created_ms,o.order_id''', 8192, (frontier,))
        closed_ids = {r['position_id'] for r in closed}
        require(len(closed_ids) == len(closed), 'duplicate_closed_position')
        require(all(r['position_id'] in closed_ids for r in legs), 'book_changed_during_read')
        by_position = defaultdict(list)
        for leg in legs:
            leg['evidence'] = json.loads(leg['evidence']) if leg['evidence'] else {}
            raw = leg['basis'] or ''
            leg['basis'] = json.loads(raw[raw.find('{'):]) if '{' in raw else {}
            by_position[leg['position_id']].append(leg)
            counts['filled_legs'] += 1
            for prefix, doc in [('evidence', leg['evidence']), ('basis', leg['basis'])]:
                for key in doc:
                    if re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9]{0,60}', key):
                        field_counts[prefix + '.' + key] += 1
            for flag in ('tax_complete', 'venue_complete', 'commission_complete'):
                value = leg['evidence'].get(flag)
                counts['legs_' + flag + ':' + ('true' if value is True else 'false' if value is False else 'unknown')] += 1
        arm_returns = []
        for row in closed:
            require(row['mode'] == 'shadow', 'foreign_trade')
            fills = by_position[row['position_id']]
            buys = [r for r in fills if r['side'] == 'buy']
            sells = [r for r in fills if r['side'] == 'sell']
            counts['closed_rows'] += 1
            counts['closed_with_one_buy'] += len(buys) == 1
            counts['closed_with_sells'] += bool(sells)
            counts['closed_all_tax_complete'] += bool(fills) and all(f['evidence'].get('tax_complete') is True for f in fills)
            counts['closed_all_venue_complete'] += bool(fills) and all(f['evidence'].get('venue_complete') is True for f in fills)
            counts['closed_has_pool_id_field_every_leg'] += bool(fills) and all(any(f['evidence'].get(k) or f['basis'].get(k) for k in ('pool', 'pool_address', 'pool_id', 'priced_pool')) for f in fills)
            cost = int(row['cost_native'])
            if cost > 0:
                arm_returns.append((int(row['proceeds_native']) - cost) / cost)
            row['buy'] = buys[0] if len(buys) == 1 else None
            row['arm'] = identity['hypothesis_id']
            row['runner'] = identity['runner']
            row['fills'] = fills
            groups[(identity['campaign_id'], row['chain'], row['token'], row['signal_id'])].append(row)
        spec = identity['spec']
        label = spec.get('exit', {}).get('name', 'unknown')
        if not isinstance(label, str) or label not in ('incumbent', 'hold60s', 'hold5m', 'tp1.25all'):
            label = 'other'
        chain = spec.get('chain') if spec.get('chain') in ('sol', 'bsc', 'robinhood') else 'other'
        arms.append({'chain': chain, 'exit': label, 'allocation': record.get('lease', 'new'), 'closed': len(closed), 'modeled_mean_return': sum(arm_returns)/len(arm_returns) if arm_returns else None,
                     'modeled_wins': sum(x > 0 for x in arm_returns), 'absolute_profit_proven': False})

    pairs = Counter()
    open_deltas = []
    for rows in groups.values():
        require(len({r['arm'] for r in rows}) == len(rows), 'duplicate_arm_signal')
        for a, b in itertools.combinations(rows, 2):
            pairs['same_campaign_token_signal_closed_pairs'] += 1
            if not a['buy'] or not b['buy']:
                pairs['missing_single_entry'] += 1
                continue
            aa, bb = a['buy'], b['buy']
            fields = ('amount_in', 'filled_out', 'fee_native', 'created_ms')
            evidence_fields = ('quote_ms', 'price_ts_ms', 'depth_ts_ms', 'native_usd', 'native_price_ts_ms', 'decimals', 'source', 'fee_policy', 'tax_bps')
            required = ('quote_ms', 'price_ts_ms', 'depth_ts_ms', 'native_usd', 'native_price_ts_ms', 'decimals', 'source', 'fee_policy')
            if any(aa['evidence'].get(k) is None or bb['evidence'].get(k) is None for k in required):
                pairs['entry_evidence_missing'] += 1
                continue
            same_econ = all(aa[k] == bb[k] for k in fields) and all(aa['evidence'].get(k) == bb['evidence'].get(k) for k in evidence_fields)
            if same_econ:
                pairs['identical_recorded_entry'] += 1
                open_deltas.append(abs(a['opened_ms'] - b['opened_ms']))
            else:
                pairs['entry_differs'] += 1
                for k in fields:
                    pairs['entry_diff_order_' + k] += aa[k] != bb[k]
                for k in evidence_fields:
                    pairs['entry_diff_evidence_' + k] += aa['evidence'].get(k) != bb['evidence'].get(k)
            pairs['runner_policy_identical'] += a['runner'] == b['runner']
            pairs['both_all_tax_complete'] += all(f['evidence'].get('tax_complete') is True for f in a['fills'] + b['fills'])
            pairs['both_all_venue_complete'] += all(f['evidence'].get('venue_complete') is True for f in a['fills'] + b['fills'])
    for name, original in artifact_hashes.items():
        require(read_artifact(state / 'research-allocation' / name)[1] == original, 'allocation_changed_during_audit')
    return {'version': VERSION, 'status': 'complete', 'frontier_ms': frontier,
            'completed_ms': int(time.time() * 1000), 'allocation_artifact_hashes': artifact_hashes,
            'books': len(records), 'retired_books': sum(r.get('lease') == 'retired' for r in records),
            'queries': reader.queries, 'canonical_queries': 0, 'provider_calls': 0,
            'rows': dict(counts), 'pairs': dict(pairs), 'fields': dict(field_counts), 'arms': arms,
            'retrieved_json_bytes': reader.total,
            'max_connection_ms': round(max(reader.durations) * 1000, 3),
            'total_connection_ms': round(sum(reader.durations) * 1000, 3),
            'identical_entry_opened_delta_ms_max': max(open_deltas) if open_deltas else None,
            'scoring_performed': False, 'absolute_profit_proven': False,
            'limitations': ['Same-signal combinations are not independent samples.',
                            'Field presence is not authenticated pool or constant-tax provenance.',
                            'Percentage fees do not cancel when proceeds or sale quantities differ.',
                            'Modeled means are descriptive; costs and execution remain unverified.',
                            'Separate short reads are not an atomic multi-book snapshot.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(args.state_dir)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        # No addresses, source rows, file paths or provider bodies in diagnostics.
        reason = str(exc)
        result = {'version': VERSION, 'status': 'unavailable', 'error_class': type(exc).__name__,
                  'reason': reason if re.fullmatch(r'[a-z_]{1,80}', reason) else 'input_or_read_failed',
                  'scoring_performed': False, 'absolute_profit_proven': False}
    print(json.dumps(result, allow_nan=False))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
