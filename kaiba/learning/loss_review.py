"""Read-only, evidence-linked reviews of every closed losing position.

No parameter tuning, order submission, portfolio writes or provider calls. A loss
is not automatically a mistake; causal conclusions remain UNPROVEN without the
required counterfactual and execution evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

VERSION = 'loss-review-v1'


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _keyed(conn: sqlite3.Connection, table: str, key: str, values: set[str]) -> list[dict[str, Any]]:
    if (table, key) not in {('decisions', 'decision_id'), ('signals', 'signal_id')}:
        raise ValueError('unsupported evidence lookup')
    out = []
    ordered = sorted(values)
    for start in range(0, len(ordered), 500):
        chunk = ordered[start:start + 500]
        marks = ','.join('?' for _ in chunk)
        out.extend(_rows(conn, f'SELECT * FROM {table} WHERE {key} IN ({marks})', tuple(chunk)))
    return out


def _json(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _positive(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() and d > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_reviews(conn: sqlite3.Connection, *, cutoff_ms: int, mode: str | None = None) -> dict[str, Any]:
    """One record per losing closed position. Caller owns the read snapshot."""
    positions = _rows(conn, 'SELECT * FROM positions WHERE closed_ms IS NOT NULL AND closed_ms<? ORDER BY closed_ms,position_id', (cutoff_ms,))
    losses, invalid = [], []
    for p in positions:
        if mode is not None and p['mode'] != mode:
            continue
        try:
            pnl = int(p['realized_native'])
        except (TypeError, ValueError):
            invalid.append({'position_id': p['position_id'], 'reason': 'realized PnL unavailable'})
            continue
        if pnl < 0:
            losses.append(p)
    ids = {p['position_id'] for p in losses}
    trades = defaultdict(list)
    for t in _rows(conn, 'SELECT * FROM trades WHERE closed_ms<?', (cutoff_ms,)):
        if t['position_id'] in ids:
            trades[t['position_id']].append(t)
    orders = defaultdict(list)
    for o in _rows(conn, 'SELECT po.position_id,o.* FROM position_orders po JOIN orders o USING(order_id) ORDER BY po.ts_ms,o.order_id'):
        if o['position_id'] in ids:
            orders[o['position_id']].append(o)
    decision_ids = {t['decision_id'] for ts in trades.values() for t in ts if t['decision_id']}
    decision_ids.update(o['decision_id'] for os_ in orders.values() for o in os_ if o['side'] == 'buy' and o['decision_id'])
    decisions = {d['decision_id']: d for d in _keyed(conn, 'decisions', 'decision_id', decision_ids)}
    signal_ids = {sid for d in decisions.values() for sid in _json(d['signals_json'], []) if isinstance(sid, str)}
    signals = {s['signal_id']: s for s in _keyed(conn, 'signals', 'signal_id', signal_ids)}
    marks = {r['position_id']: r for r in _rows(conn, 'SELECT m.position_id,COUNT(*) n,MIN(m.ts_ms) first_ms,MAX(m.ts_ms) last_ms FROM position_marks m JOIN positions p USING(position_id) WHERE p.closed_ms<? AND m.ts_ms>=p.opened_ms AND m.ts_ms<=p.closed_ms GROUP BY m.position_id', (cutoff_ms,))}
    incidents = defaultdict(list)
    if losses:
        first = min(p['opened_ms'] for p in losses)
        for e in _rows(conn, "SELECT id,ts_ms,payload FROM events WHERE kind='system' AND ts_ms>=? AND ts_ms<? AND json_valid(payload) AND json_extract(payload,'$.service')='watchdog' AND json_extract(payload,'$.event') IN ('exit_failed','protection_blind_timeout') ORDER BY id", (first, cutoff_ms)):
            payload = _json(e['payload'], {})
            if payload.get('position_id') in ids:
                incidents[payload['position_id']].append({'event_id': e['id'], 'ts_ms': e['ts_ms'], 'event': payload.get('event'), 'order_id': payload.get('order_id'), 'detail_data': str(payload.get('detail') or payload.get('reason') or '')[:500]})
    reviews = []
    for p in losses:
        pid = p['position_id']; ts = trades[pid]; os_ = orders[pid]
        t = ts[0] if len(ts) == 1 else None
        gaps, flags = [], []
        if not ts: gaps.append('missing_canonical_outcome')
        if len(ts) > 1: gaps.append('duplicate_canonical_outcomes')
        if t and (t['chain'] != p['chain'] or t['mode'] != p['mode'] or t['lane'] != p['lane']):
            gaps.append('outcome_scope_mismatch'); t = None
        if t and int(t['pnl_native']) != int(p['realized_native']): gaps.append('outcome_ledger_pnl_mismatch')
        buys = [o for o in os_ if o['side'] == 'buy']
        did = t['decision_id'] if t else None
        if not did and buys: did = buys[0]['decision_id']
        d = decisions.get(did)
        if d and (d['chain'] != p['chain'] or d['token'] != p['token'] or d['mode'] != p['mode'] or d['lane'] != p['lane']):
            gaps.append('entry_decision_scope_mismatch'); d = None
        if d and d['ts_ms'] > p['opened_ms']:
            gaps.append('entry_decision_not_point_in_time'); d = None
        if d is None: gaps.append('entry_decision_unavailable')
        if not os_: gaps.append('linked_order_evidence_unavailable')
        if not os_ or any(o['fee_native'] is None for o in os_): gaps.append('fee_complete_cashflow_unavailable')
        if t is None or t['slippage_bps'] is None: gaps.append('realized_slippage_unavailable')
        if not marks.get(pid): gaps.append('no_in_lifetime_position_marks')
        entry, peak = _positive(p['entry_price_usd']), _positive(p['peak_price_usd'])
        mfe = t['mfe_pct'] if t else None
        if mfe is None: gaps.append('outcome_mfe_unavailable')
        elif mfe == 0 and entry is not None and peak is not None and peak > entry:
            gaps.append('zero_mfe_contradicts_recorded_peak')
        if t is None or t['mae_pct'] is None: gaps.append('outcome_mae_unavailable')
        source_signals = []
        for sid in (_json(d['signals_json'], []) if d else []):
            s = signals.get(sid)
            if not s or s['created_ms'] > d['ts_ms'] or s['chain'] != p['chain'] or s['token'] != p['token']:
                gaps.append('signal_evidence_not_point_in_time'); continue
            entities = _json(s['entities_json'], [])
            source_signals.append({'signal_id': sid, 'strength': s['strength'], 'created_ms': s['created_ms'], 'wallet_count': len(_json(s['wallets_json'], [])), 'entity_labels': len(entities), 'unlinked_labels': sum(str(e).startswith('addr:') for e in entities)})
        if any(s['unlinked_labels'] for s in source_signals): flags.append('independence_not_fully_verified')
        ie = [e for e in incidents[pid] if p['opened_ms'] <= e['ts_ms'] <= p['closed_ms']]
        if any(e['event'] == 'exit_failed' for e in ie): flags.append('exit_failure_observed_during_position')
        if any(e['event'] == 'protection_blind_timeout' for e in ie): flags.append('protection_blind_timeout_observed')
        cost = int(p['cost_native'])
        pct = float(Decimal(p['realized_native']) * 100 / cost) if cost > 0 else None
        actions = []
        if 'exit_failure_observed_during_position' in flags: actions.append('Trace referenced exit failures and route/inventory reconciliation before tuning exits.')
        if 'independence_not_fully_verified' in flags: actions.append('Verify entity independence; raw address count is not confirmed independent flow.')
        if 'fee_complete_cashflow_unavailable' in gaps: actions.append('Reconcile actual transaction and provider fees before calling the return fully net.')
        if 'missing_canonical_outcome' in gaps: actions.append('Repair terminal-outcome coverage from verified ledger evidence without inventing a sell.')
        if 'zero_mfe_contradicts_recorded_peak' in gaps or 'no_in_lifetime_position_marks' in gaps: actions.append('Repair excursion/mark evidence before judging peak capture or alternate exits.')
        if not actions: actions.append('Compare this setup with matched winners and skips in a future time block; a loss alone does not establish a bad rule.')
        review = {'version': VERSION, 'position_id': pid, 'chain': p['chain'], 'mode': p['mode'], 'lane': p['lane'], 'token': p['token'], 'opened_ms': p['opened_ms'], 'closed_ms': p['closed_ms'], 'pnl_native': p['realized_native'], 'recorded_return_pct': pct, 'exit_reason_data': p['exit_reason'], 'canonical_trade_ids': [x['trade_id'] for x in ts], 'decision_id': d['decision_id'] if d else None, 'entry_confidence': d['confidence'] if d else None, 'confidence_semantics': 'not assumed to be a calibrated win probability', 'entry_dossier_grade': d['dossier_grade'] if d else None, 'entry_thesis_data': str(d['thesis'])[:500] if d else None, 'linked_order_ids': [o['order_id'] for o in os_], 'entry_legs': len(buys), 'source_signals': source_signals, 'incident_evidence': ie, 'mark_coverage': marks.get(pid), 'observed_flags': sorted(set(flags)), 'evidence_gaps': sorted(set(gaps)), 'causal_verdict': 'UNPROVEN', 'counterfactual_exit_quality': 'UNAVAILABLE: no executable continuation comparison performed', 'learning_actions': actions}
        review['evidence_fingerprint'] = hashlib.sha256(json.dumps(review, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        reviews.append(review)
    return {'version': VERSION, 'cutoff_ms': cutoff_ms, 'population_rule': 'closed positions with negative recorded realized_native; live/shadow kept separate', 'counts': dict(Counter(r['mode'] for r in reviews)), 'evidence_gap_counts': dict(Counter(g for r in reviews for g in r['evidence_gaps'])), 'observed_flag_counts': dict(Counter(g for r in reviews for g in r['observed_flags'])), 'unreviewable_positions': invalid, 'reviews': reviews}


def _atomic_text(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=os.environ.get('TMPDIR'), delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_reviews(report: dict[str, Any], output: Path) -> dict[str, int]:
    """Write versioned data artifacts only; unchanged evidence is not new learning."""
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'manifest.json'
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    previous = old.get('fingerprints', {})
    fingerprints = {r['position_id']: r['evidence_fingerprint'] for r in report['reviews']}
    new = sum(k not in previous for k in fingerprints)
    changed = sum(k in previous and previous[k] != v for k, v in fingerprints.items())
    _atomic_text(output / 'reviews.json', json.dumps(report, indent=2))
    handle = io.StringIO(newline='')
    fields = ['position_id', 'chain', 'mode', 'lane', 'pnl_native', 'recorded_return_pct', 'exit_reason_data', 'decision_id', 'causal_verdict', 'observed_flags', 'evidence_gaps']
    writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
    for r in report['reviews']:
        writer.writerow({k: '; '.join(r[k]) if isinstance(r[k], list) else r[k] for k in fields})
    _atomic_text(output / 'reviews.csv', handle.getvalue())
    manifest = {'version': VERSION, 'cutoff_ms': report['cutoff_ms'], 'counts': report['counts'], 'fingerprints': fingerprints, 'unreviewable_positions': report['unreviewable_positions']}
    _atomic_text(manifest_path, json.dumps(manifest, indent=2))
    return {'new': new, 'updated': changed, 'total': len(fingerprints)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    conn = sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON'); conn.execute('BEGIN')
    try:
        report = build_reviews(conn, cutoff_ms=int(time.time() * 1000))
    finally:
        conn.rollback(); conn.close()
    delta = write_reviews(report, args.output)
    if delta['new'] or delta['updated']:
        print(json.dumps({'review_coverage': report['counts'], 'changes': delta, 'evidence_gaps': report['evidence_gap_counts'], 'flags': report['observed_flag_counts'], 'causal_findings': 'UNPROVEN unless separately validated; no live parameter changes'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
