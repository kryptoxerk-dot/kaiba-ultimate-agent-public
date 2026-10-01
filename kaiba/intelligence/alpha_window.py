"""Read-only bounded input for alpha research. No qualification or trade authority.

Codex-owned handoff implementation, 2026-09-27. Bus timestamps select the window;
they never stand in for source evidence clocks. Incomplete pages remain explicit.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def capture(conn, *, end_ms: int, window_ms: int = 1_200_000,
            after_id: int = 0, upper_id: int | None = None,
            max_rows: int = 1000, page_size: int = 200) -> dict:
    """Freeze one event window, retaining a resumable raw-event cursor.

    Call on a dedicated read-only connection. Resume with the returned end_ms,
    window_ms, upper_id and next_after_id. Row budgets include unrelated events,
    so filtering cannot silently skip unread relevant events.
    """
    if not 1 <= max_rows <= 5000 or not 1 <= page_size <= 500:
        raise ValueError('row budget must be 1..5000; page size 1..500')
    if not 1 <= window_ms <= 86_400_000 or end_ms < window_ms or after_id < 0:
        raise ValueError('invalid window or cursor')
    if upper_id is not None and (upper_id < 0 or after_id > upper_id):
        raise ValueError('invalid upper cursor')
    if conn.in_transaction:
        raise ValueError('use a dedicated connection outside a transaction')
    conn.execute('BEGIN')
    try:
        ceiling = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        upper = ceiling if upper_id is None else min(upper_id, ceiling)
        if after_id > upper:
            raise ValueError('cursor beyond snapshot')
        start = end_ms-window_ms
        cursor, scanned, selected = after_id, 0, []
        invalid = []
        while scanned < max_rows:
            rows = conn.execute(
                'SELECT id,ts_ms,kind,chain,subject,payload FROM events INDEXED BY idx_events_ts '
                'WHERE ts_ms>=? AND ts_ms<=? AND id>? AND id<=? '
                'ORDER BY id LIMIT ?',
                (start, end_ms, cursor, upper, min(page_size,max_rows-scanned)),
            ).fetchall()
            if not rows:
                break
            for event_id, bus_ms, kind, chain, subject, raw in rows:
                cursor = event_id
                scanned += 1
                if not (kind.startswith('alpha.') or kind == 'hunter.found'):
                    continue
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError('payload must be an object')
                except (ValueError, TypeError):
                    invalid.append(event_id)
                    continue
                selected.append({
                    'id':event_id, 'bus_ts_ms':bus_ms, 'kind':kind,
                    'chain':chain, 'subject':subject, 'payload':payload,
                    'source_evidence':payload.get('evidence'),
                    'classification':'research_lead',
                })
        more = conn.execute(
            'SELECT 1 FROM events INDEXED BY idx_events_ts WHERE ts_ms>=? AND ts_ms<=? AND id>? AND id<=? LIMIT 1',
            (start,end_ms,cursor,upper),
        ).fetchone() is not None
        health = {}
        for table, identity in (('alpha_source_health','source'), ('radar_layer_health','layer')):
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if not exists:
                health[table] = {'available':False}
                continue
            rows = conn.execute(f'SELECT {identity},last_poll_ms,last_ok_ms,last_count,fail_streak FROM {table} LIMIT 201').fetchall()
            health[table] = {'available':True,'complete':len(rows)<=200,
                'rows':[dict(zip((identity,'last_poll_ms','last_ok_ms','last_count','fail_streak'),r,strict=True)) for r in rows[:200]]}
        return {
            'start_ms':start,'end_ms':end_ms,'window_ms':window_ms,
            'upper_id':upper,'next_after_id':cursor,'scanned_rows':scanned,
            'complete':not more,'events':selected,'invalid_payload_event_ids':invalid,
            'health':health, 'qualified_candidate_count':None,
            'trade_authorization':False, 'database_writes':0,'provider_calls':0,
        }
    finally:
        conn.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--max-rows',type=int,default=1000)
    parser.add_argument('--end-ms',type=int,default=None)
    parser.add_argument('--after-id',type=int,default=0)
    parser.add_argument('--upper-id',type=int,default=None)
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    conn = sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True,timeout=2)
    conn.execute('PRAGMA query_only=ON')
    deadline = time.monotonic()+20
    conn.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
    result = capture(conn,end_ms=args.end_ms or int(time.time()*1000),
        after_id=args.after_id,upper_id=args.upper_id,max_rows=args.max_rows)
    conn.close()
    path = Path(args.output)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(result,indent=2),encoding='utf-8')
    temporary.replace(path)
    print(json.dumps({k:result[k] for k in ('scanned_rows','complete','next_after_id','upper_id')}))


if __name__ == '__main__':
    main()
