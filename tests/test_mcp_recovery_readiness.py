"""A fresh heartbeat must not hide stranded inventory or refused exits."""
from kaiba.core import events as ev
from kaiba.core.schemas import EventKind
from kaiba.mcp import server


def test_heartbeat_reports_stranded_inventory_and_exit_failures(tmp_db):
    ev.emit(EventKind.SYSTEM, {
        'service':'watchdog','event':'heartbeat','checked':1,'blind':0,
        'exits':0,'exit_failures':1,'stranded_deferred':6,'requests_drained':2,
        'standing_unprotected':1,'price_source':'venue+gmgn',
    },conn=tmp_db)
    result=server._watchdog_liveness(tmp_db)
    assert result['running'] is True
    assert result.get('stranded_deferred') == 6
    assert result.get('exit_failures') == 1
    assert result.get('requests_drained') == 2
    assert result.get('standing_unprotected') == 1
    assert '6' in result['note'] and 'stranded' in result['note']
    assert 'exit failure' in result['note']


def test_failed_live_exit_remains_visible_between_retry_ticks(tmp_db):
    tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,qty) "
                   "VALUES('held','sol','token','sm-trenches','live',1,'10')")
    tmp_db.execute("INSERT INTO watchdog_state(position_id,exit_state,exit_order_id,updated_ms) "
                   "VALUES('held','failed','failed-sell',1)")
    ev.emit(EventKind.SYSTEM, {'service':'watchdog','event':'heartbeat',
            'checked':1,'blind':0,'exits':0,'exit_failures':0,'stranded_deferred':0},conn=tmp_db)
    result=server._watchdog_liveness(tmp_db)
    assert result.get('failed_live_exit_positions') == 1
    assert 'failed live exit' in result['note']


def test_legacy_counters_stay_unavailable(tmp_db):
    ev.emit(EventKind.SYSTEM, {'service':'watchdog','event':'heartbeat','checked':1,'blind':0},conn=tmp_db)
    result=server._watchdog_liveness(tmp_db)
    assert result['stranded_deferred'] is None
    assert result['exit_failures'] is None
