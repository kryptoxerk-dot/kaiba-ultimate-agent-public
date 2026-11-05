"""Read-only, evidence-linked reviews of every closed losing position.

No parameter tuning, order submission, portfolio writes or provider calls. A loss
is not automatically a mistake; causal conclusions remain UNPROVEN without the
required counterfactual and execution evidence.

Codex LOSS-REVIEW-READS-20261019: CLI reads use the existing guarded reader,
with independent short observations rather than a report-wide transaction.
This bounds source reads; it does not improve expected trading returns.

Codex LOSS-COST-EVIDENCE-20261102: attach matched existing fill_costs observations
using filled-live-only keyed reads. Decision-reference drift and decoded core
components DO NOT replace realized-slippage/full-cashflow gaps or UNPROVEN.
Lead's pagination, incident ledger and all read/capture caps stay unchanged.

Claude LOSS-EVIDENCE-DEFERRAL-20261102: the cron deferred 1,322 runs in a row because ONE query
(position_orders x orders, 19,692 rows on the box) could never fit the guard's 10,000-row cap, so
every run ended "source_deferred". The three growing sources are now read as keyset pages of at
most PAGE_ROWS rows, each page its own guarded query (row cap, time limit, WAL check); a deferral
on any page still defers the whole capture, so no partial result is accepted. A deferral now names
its reason category and the age of the evidence it leaves in place.

The watchdog incident scan cannot be one capture's work at all. MEASURED on the box 2026-11-02:
kind='system' events start 2026-09-21 and arrive at ~16,500 a day; one hour of them reads in
0.08 s through idx_events_kind(kind, id), a day does not fit the 3 s query limit, and the whole
history is over a minute of reads against a 30 s capture budget. The CLI therefore keeps an
incident ledger BESIDE its outputs: event-id pages of at most INCIDENT_ID_PAGE ids, each one
guarded query, merged only once the whole page was read, with the watermark saved after every
run. A run that has not caught up publishes nothing and defers as ``incident_scan_in_progress``
with the ids still to read; once current, a run reads only the ids added since the last one.

Host CLI (from /home/ubuntu/kaiba):
    timeout 1200 nice -n19 ionice -c3 .venv/bin/python -m kaiba.learning.loss_review \
        --db /home/ubuntu/kaiba/data/kaiba.db --output /home/ubuntu/kaiba/data/loss-review
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

from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred
from kaiba.learning.fabricated import RULE as FABRICATED_RULE
from kaiba.learning.fabricated import is_fabricated_outcome
from kaiba.learning.loss_cost_evidence import cost_coverage, position_cost_evidence

VERSION = 'loss-review-v1'
QUERY_SECONDS = 3
QUERY_ROWS = 10_000
CAPTURE_SECONDS = 30
#: Rows per keyset page and ids per evidence lookup, below the guard's caps (unchanged). MEASURED
#: on the box 2026-11-02 under ionice -c3: 10,000-row pages took 1.3-2.6 s and 500-id decisions
#: lookups 1.2-3.1 s (random reads on a contended disk), so the 3 s limit failed intermittently.
PAGE_ROWS = 2_000
LOOKUP_IDS = 100
INCIDENT_LEDGER_SCHEMA = 'loss-review-incidents/1'
#: Event ids (all kinds) per guarded incident page. MEASURED on the box 2026-11-02: ~9,900 ids an
#: hour, ~980 of them kind='system'; that hour read in 0.08 s.
INCIDENT_ID_PAGE = 10_000
#: Share of CAPTURE_SECONDS one run may spend catching the ledger up; at least one page always
#: runs. INVENTED split: the other queries took ~6.7 s on the box (2026-11-02).
INCIDENT_SCAN_SECONDS = 15
_INCIDENTS = ("json_valid(payload) AND json_extract(payload,'$.service')='watchdog' "
              "AND json_extract(payload,'$.event') IN ('exit_failed','protection_blind_timeout')")


class _ReviewReader(GuardedReader):
    """Reuse the unchanged query guard, adding only a whole-capture deadline."""

    def __init__(self, db_path: Path, *, clock=None):
        super().__init__(db_path, query_seconds=QUERY_SECONDS, max_rows=QUERY_ROWS,
                         clock=clock or time.monotonic)
        self.deadline = self.clock() + CAPTURE_SECONDS

    def check_budget(self) -> None:
        if self.clock() >= self.deadline:
            raise SourceDeferred('loss_review_capture_budget')

    def rows(self, sql: str, args=()) -> list[dict[str, Any]]:
        self.check_budget()
        rows = super().rows(sql, args)
        self.check_budget()  # The inherited reader has already closed its connection.
        return rows


def _rows(conn: sqlite3.Connection | GuardedReader, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    if isinstance(conn, GuardedReader):
        return conn.rows(sql, args)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _keyset(conn: sqlite3.Connection | GuardedReader, sql: str, args: tuple, keys: tuple[tuple[str, str], ...],
            *, page: int | None = None) -> list[dict[str, Any]]:
    """Every row of ``sql`` (a SELECT ... WHERE ... without ORDER BY), read as keyset pages.

    ``keys`` are (SQL expression, result column) pairs forming a unique ascending key. Each page is
    one ordinary call through ``_rows`` -- so through the guard -- of at most ``page`` rows; a page
    that defers raises, and nothing collected so far is returned."""
    page = page or PAGE_ROWS
    exprs = ", ".join(expr for expr, _ in keys)
    out: list[dict[str, Any]] = []
    last: tuple | None = None
    while True:
        after = "" if last is None else f" AND ({exprs}) > ({', '.join('?' for _ in keys)})"
        rows = _rows(conn, f"{sql}{after} ORDER BY {exprs} LIMIT ?", (*args, *(last or ()), page))
        out.extend(rows)
        if len(rows) < page:
            return out
        last = tuple(rows[-1][column] for _, column in keys)


def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the paging helper columns before the rows are used as evidence."""
    return [{k: v for k, v in r.items() if not k.startswith('_page_')} for r in rows]


def _keyed(conn: sqlite3.Connection | GuardedReader, table: str, key: str, values: set[str]) -> list[dict[str, Any]]:
    if (table, key) not in {('decisions', 'decision_id'), ('signals', 'signal_id'), ('fill_costs', 'order_id')}:
        raise ValueError('unsupported evidence lookup')
    out = []
    ordered = sorted(values)
    for start in range(0, len(ordered), LOOKUP_IDS):
        chunk = ordered[start:start + LOOKUP_IDS]
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


def _incident(e: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    payload = _json(e['payload'], {})
    return payload.get('position_id'), {'event_id': e['id'], 'ts_ms': e['ts_ms'], 'event': payload.get('event'), 'order_id': payload.get('order_id'), 'detail_data': str(payload.get('detail') or payload.get('reason') or '')[:500]}


def build_reviews(conn: sqlite3.Connection | GuardedReader, *, cutoff_ms: int, mode: str | None = None,
                  incidents: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """One record per losing position; a reader supplies independent observations.

    The original Connection API remains available for callers owning a snapshot.
    This function never starts or commits a caller's transaction. ``incidents`` (position id ->
    watchdog incidents, from :func:`scan_incidents`) replaces the single events query.
    """
    positions = _keyset(conn, 'SELECT * FROM positions WHERE closed_ms IS NOT NULL AND closed_ms<?', (cutoff_ms,),
                        (('closed_ms', 'closed_ms'), ('position_id', 'position_id')))
    losses, invalid = [], []
    fabricated: Counter = Counter()
    for p in positions:
        if mode is not None and p['mode'] != mode:
            continue
        if is_fabricated_outcome(p['exit_reason'], p['mode']):
            # A paper write-off with no sell modelled: its -100% was booked, not observed,
            # so it is not a loss to review. Counted, never dropped silently.
            fabricated[p['mode']] += 1
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
    for t in _strip(_keyset(conn, 'SELECT rowid AS _page_rowid,* FROM trades WHERE closed_ms<?', (cutoff_ms,),
                            (('rowid', '_page_rowid'),))):
        if t['position_id'] in ids:
            trades[t['position_id']].append(t)
    orders = defaultdict(list)
    joined = _keyset(conn, 'SELECT po.rowid AS _page_rowid,po.ts_ms AS _page_ts,po.position_id,o.* '
                           'FROM position_orders po JOIN orders o USING(order_id) WHERE 1=1', (),
                     (('po.rowid', '_page_rowid'),))
    joined.sort(key=lambda r: (r['_page_ts'], r['order_id']))  # the previous ORDER BY po.ts_ms,o.order_id
    for o in _strip(joined):
        if o['position_id'] in ids:
            orders[o['position_id']].append(o)
    # No paper/model fees are decoded chain evidence. Only fetch existing rows
    # needed by losing LIVE positions; every <=100-id batch uses the same guard.
    live_ids = {p['position_id'] for p in losses if p['mode'] == 'live'}
    cost_ids = {o['order_id'] for pid in live_ids for o in orders[pid]
                if o['mode'] == 'live' and o['state'] == 'filled'}
    decoded_costs = {r['order_id']: r for r in _keyed(conn, 'fill_costs', 'order_id', cost_ids)}
    decision_ids = {t['decision_id'] for ts in trades.values() for t in ts if t['decision_id']}
    decision_ids.update(o['decision_id'] for os_ in orders.values() for o in os_ if o['side'] == 'buy' and o['decision_id'])
    decisions = {d['decision_id']: d for d in _keyed(conn, 'decisions', 'decision_id', decision_ids)}
    signal_ids = {sid for d in decisions.values() for sid in _json(d['signals_json'], []) if isinstance(sid, str)}
    signals = {s['signal_id']: s for s in _keyed(conn, 'signals', 'signal_id', signal_ids)}
    marks = {r['position_id']: r for r in _rows(conn, 'SELECT m.position_id,COUNT(*) n,MIN(m.ts_ms) first_ms,MAX(m.ts_ms) last_ms FROM position_marks m JOIN positions p USING(position_id) WHERE p.closed_ms<? AND m.ts_ms>=p.opened_ms AND m.ts_ms<=p.closed_ms GROUP BY m.position_id', (cutoff_ms,))}
    if incidents is not None:
        incidents = defaultdict(list, incidents)  # filtered to each lifetime below, like the query
    else:
        incidents = defaultdict(list)
        if losses:
            first = min(p['opened_ms'] for p in losses)
            for e in _rows(conn, f"SELECT id,ts_ms,payload FROM events WHERE kind='system' AND ts_ms>=? AND ts_ms<? AND {_INCIDENTS} ORDER BY id", (first, cutoff_ms)):
                pid, record = _incident(e)
                if isinstance(pid, str) and pid in ids:
                    incidents[pid].append(record)
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
        if p['mode'] == 'live':
            review['decoded_fill_cost_evidence'] = position_cost_evidence(p, os_, decoded_costs, cutoff_ms=cutoff_ms)
            # Keep strict economic gaps independent. Core fields are partial
            # decoder evidence, not complete rent/tax/custody or landing drift.
            if any(leg['decoder_reported_exec_vs_decision_bps'] is not None
                   or any(v is not None for v in leg['reported_fee_atoms'].values())
                   for leg in review['decoded_fill_cost_evidence']['legs']):
                review['observed_flags'].append('decoded_cost_fields_recorded')
                review['learning_actions'].append('Read matched decoded fee/reference observations separately; resolve rent, tax, fee payer and executable-reference gaps before attributing net loss or landing slippage.')
        review['evidence_fingerprint'] = hashlib.sha256(json.dumps(review, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        reviews.append(review)
    return {'version': VERSION, 'cutoff_ms': cutoff_ms, 'population_rule': 'closed positions with negative recorded realized_native; live/shadow kept separate; ' + FABRICATED_RULE, 'counts': dict(Counter(r['mode'] for r in reviews)), 'fabricated_outcomes_excluded': dict(fabricated), 'evidence_gap_counts': dict(Counter(g for r in reviews for g in r['evidence_gaps'])), 'observed_flag_counts': dict(Counter(g for r in reviews for g in r['observed_flags'])), 'unreviewable_positions': invalid, 'decoded_fill_cost_coverage': cost_coverage(reviews), 'reviews': reviews}


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
    manifest = {'version': VERSION, 'cutoff_ms': report['cutoff_ms'], 'counts': report['counts'], 'fabricated_outcomes_excluded': report.get('fabricated_outcomes_excluded', {}), 'fingerprints': fingerprints, 'unreviewable_positions': report['unreviewable_positions']}
    if 'capture_scope' in report:
        manifest['capture_scope'] = report['capture_scope']
    if 'decoded_fill_cost_coverage' in report:
        manifest['decoded_fill_cost_coverage'] = report['decoded_fill_cost_coverage']
    _atomic_text(manifest_path, json.dumps(manifest, indent=2))
    return {'new': new, 'updated': changed, 'total': len(fingerprints)}


def incident_ledger_path(output: Path) -> Path:
    """Beside the published outputs, never inside them: the ledger advances on runs that defer."""
    return output.with_name(output.name + '.incidents.json')


def _empty_ledger() -> dict[str, Any]:
    return {'schema': INCIDENT_LEDGER_SCHEMA, 'scanned_through_id': 0, 'incidents': {}}


def _load_ledger(path: Path) -> dict[str, Any]:
    try:
        ledger = json.loads(path.read_text())
        if (ledger.get('schema') == INCIDENT_LEDGER_SCHEMA and type(ledger.get('scanned_through_id')) is int
                and isinstance(ledger.get('incidents'), dict)):
            return ledger
    except (OSError, ValueError, AttributeError):
        pass
    return _empty_ledger()  # unreadable or foreign: read the history again, never trust a guess


def scan_incidents(reader: _ReviewReader, path: Path, progress: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Watchdog exit incidents of every position, caught up across runs; see the module docstring.

    Events are append-only with increasing ids, so the ids at or below the watermark never need
    reading again. A page that defers is not counted and is read again next run. ``progress`` is
    filled for the status line. Raises SourceDeferred('incident_scan_in_progress') while behind."""
    ledger = _load_ledger(path)
    top = _rows(reader, "SELECT MAX(id) AS top FROM events WHERE kind='system'")[0]['top'] or 0
    if top < ledger['scanned_through_id']:
        ledger = _empty_ledger()  # a rebuilt or different database: its ids are not ours
    through, pages, started = ledger['scanned_through_id'], 0, reader.clock()
    try:
        while through < top and (pages == 0 or reader.clock() - started < INCIDENT_SCAN_SECONDS):
            upper = min(top, through + INCIDENT_ID_PAGE)
            page = [_incident(e) for e in _rows(
                reader, f"SELECT id,ts_ms,payload FROM events WHERE kind='system' AND id>? AND id<=? AND {_INCIDENTS} ORDER BY id",
                (through, upper))]
            for pid, record in page:
                if isinstance(pid, str):
                    ledger['incidents'].setdefault(pid, []).append(record)
            through, pages = upper, pages + 1
    finally:
        progress.update({'state': 'current' if through >= top else 'catching_up', 'pages_this_run': pages,
                         'event_ids_remaining': top - through})
        if pages:
            ledger['scanned_through_id'] = through
            _atomic_text(path, json.dumps(ledger, sort_keys=True, separators=(',', ':')))
    if through < top:
        raise SourceDeferred('incident_scan_in_progress')
    return ledger['incidents']


#: Reasons that recur on every run until a query or the data changes (not a WAL spike).
STRUCTURAL_DEFERRALS = frozenset({'source_row_cap'})


def deferral_reason(exc: Exception) -> str:
    """The guard's reason as a fixed category; never the raw text (it may carry sizes or names)."""
    text = str(exc)
    for category in ('wal_over_limit', 'source_row_cap', 'source_query_unavailable', 'loss_review_capture_budget',
                     'incident_scan_in_progress'):
        if text.startswith(category):
            return category
    return 'other'


def retained_evidence(output: Path, *, now_ms: int) -> dict[str, Any]:
    """What the last COMPLETED capture left in place. Stale is reported as stale, never as current."""
    try:
        manifest = json.loads((output / 'manifest.json').read_text())
        cutoff = int(manifest['cutoff_ms'])
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'never_completed_or_unreadable'}
    counts = manifest.get('counts') if isinstance(manifest.get('counts'), dict) else {}  # per mode
    return {'state': 'stale', 'last_completed_cutoff_ms': cutoff, 'age_s': max(0, (now_ms - cutoff) // 1000),
            'reviewed_losses': sum(v for v in counts.values() if type(v) is int)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    cutoff_ms = int(time.time() * 1000)  # before the ledger's top id, so it covers every event before it
    reader = _ReviewReader(args.db)
    scan: dict[str, Any] = {'state': 'not_started'}
    try:
        incidents = scan_incidents(reader, incident_ledger_path(args.output), scan)
        report = build_reviews(reader, cutoff_ms=cutoff_ms, incidents=incidents)
        reader.check_budget()
    except SourceDeferred as exc:
        # No source exception text, identifier or filesystem path enters the status: only a
        # reason CATEGORY and what the previous completed capture left in place.
        # Publication happens only after the entire capture succeeds. Exit 2 stays a non-success.
        reason = deferral_reason(exc)
        print(json.dumps({'status': 'source_deferred', 'reason': reason,
                          'structural': reason in STRUCTURAL_DEFERRALS, 'outputs_written': False,
                          'retained_evidence': retained_evidence(args.output, now_ms=int(time.time() * 1000)),
                          'incident_scan': scan, 'retry_after_s': 60,
                          'retry_guidance': 'Wait at least 60 seconds; recheck WAL before each query.'},
                         sort_keys=True))
        return 2
    report['capture_scope'] = {
        'basis': 'per_query_observations', 'single_snapshot': False,
        'query_seconds': QUERY_SECONDS, 'max_rows_per_query': QUERY_ROWS,
        'capture_budget_seconds': CAPTURE_SECONDS, 'query_count': reader.queries,
        'query_receipts': reader.receipts,
        'incident_basis': 'ledger_by_event_id_watermark', 'incident_pages_this_run': scan['pages_this_run'],
    }
    delta = write_reviews(report, args.output)
    if delta['new'] or delta['updated']:
        print(json.dumps({'review_coverage': report['counts'], 'fabricated_outcomes_excluded': report['fabricated_outcomes_excluded'], 'changes': delta, 'evidence_gaps': report['evidence_gap_counts'], 'flags': report['observed_flag_counts'], 'causal_findings': 'UNPROVEN unless separately validated; no live parameter changes'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
