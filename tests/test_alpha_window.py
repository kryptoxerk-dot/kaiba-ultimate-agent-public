import json
import sqlite3

import pytest

from kaiba.intelligence.alpha_window import capture


def database():
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,ts_ms INTEGER,kind TEXT,chain TEXT,subject TEXT,payload TEXT)')
    conn.execute('CREATE INDEX idx_events_ts ON events(ts_ms)')
    return conn


def put(conn, ident, timestamp, kind='hunter.found', payload=None, chain='sol'):
    conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                 (ident,timestamp,kind,chain,'same-name',json.dumps(payload or {'hunter':'radar'})))


def test_window_uses_current_time_not_oldest_tail_and_preserves_unknown_clocks():
    c = database()
    put(c,1,1)
    put(c,2,900, payload={'hunter':'radar','evidence':{'observed_ms':450,'nested':{'x':1}}})
    put(c,3,950)
    put(c,4,1001)
    c.commit()
    result = capture(c,end_ms=1000,window_ms=200)
    assert [x['id'] for x in result['events']] == [2,3]
    assert result['events'][0]['source_evidence']['observed_ms'] == 450
    assert result['events'][1]['source_evidence'] is None
    assert result['complete']
    assert result['qualified_candidate_count'] is None


def test_filtered_pages_do_not_skip_unread_ids_and_new_events_stay_out():
    c = database()
    for i in range(1,7):
        put(c,i,900,kind='service.heartbeat' if i<4 else 'alpha.signal')
    c.commit()
    first = capture(c,end_ms=1000,window_ms=200,max_rows=3,page_size=2)
    assert first['events']==[] and not first['complete']
    assert first['next_after_id']==3 and first['upper_id']==6
    put(c,7,950)
    c.commit()
    second = capture(c,end_ms=1000,window_ms=200,after_id=3,upper_id=6,max_rows=3)
    assert [x['id'] for x in second['events']]==[4,5,6]
    assert second['complete']


def test_same_names_different_chains_are_not_merged_and_invalid_is_reported():
    c = database()
    put(c,1,900,chain='sol')
    put(c,2,900,chain='robinhood')
    c.execute("INSERT INTO events VALUES (3,900,'alpha.signal','sol','x','not-json')")
    c.commit()
    before=c.total_changes
    result=capture(c,end_ms=1000,window_ms=200)
    assert len(result['events'])==2
    assert result['invalid_payload_event_ids']==[3]
    assert c.total_changes==before
    assert not c.in_transaction


def test_invalid_budget_refused_and_exceptions_close_snapshot():
    c=database()
    with pytest.raises(ValueError):
        capture(c,end_ms=1000,window_ms=200,max_rows=5001)
    with pytest.raises(ValueError):
        capture(c,end_ms=1000,window_ms=200,after_id=9)
    assert not c.in_transaction


def test_small_current_window_does_not_scan_entire_old_bus():
    c=database()
    c.executemany('INSERT INTO events VALUES (?,1,\'alpha.signal\',\'sol\',\'old\',\'{}\')',
                  ((i,) for i in range(1,20001)))
    put(c,20001,900)
    c.commit()
    ticks=0
    def budget():
        nonlocal ticks
        ticks+=1
        return int(ticks>50)
    c.set_progress_handler(budget,1000)
    result=capture(c,end_ms=1000,window_ms=200)
    assert [x['id'] for x in result['events']]==[20001]
    assert result['complete']
