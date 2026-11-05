"""Operator read-surface regression: unrelated heartbeats cannot hide protection."""
from kaiba.core import events as ev
from kaiba.core.schemas import EventKind
from kaiba.mcp import server


def test_newer_engine_heartbeat_does_not_hide_live_blind_watchdog(tmp_db):
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat",
            "checked": 13, "blind": 1, "exits": 0, "price_source": "venue"},
            conn=tmp_db)
    ev.emit(EventKind.SYSTEM, {"service": "engine", "event": "heartbeat",
            "checked": 99, "blind": 0}, conn=tmp_db)
    wd = server._watchdog_liveness(tmp_db)
    assert wd["running"] is True
    assert wd["checked"] == 13
    assert wd["blind"] == 1
    assert "NOT being evaluated" in wd["note"]


def test_unrelated_heartbeat_does_not_refresh_stale_watchdog(tmp_db, monkeypatch):
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat",
            "checked": 2, "blind": 0}, conn=tmp_db)
    stale_ms = 1_000_000
    tmp_db.execute("UPDATE events SET ts_ms=?", (stale_ms,))
    ev.emit(EventKind.SYSTEM, {"service": "scan", "event": "heartbeat"}, conn=tmp_db)
    monkeypatch.setattr(server, "now_ms", lambda: stale_ms + 121_000)
    wd = server._watchdog_liveness(tmp_db)
    assert wd["running"] is False
    assert wd["last_heartbeat_age_s"] == 121.0
    assert "stale" in wd["note"]


def test_missing_watchdog_is_not_inferred_from_engine(tmp_db):
    ev.emit(EventKind.SYSTEM, {"service": "engine", "event": "heartbeat"}, conn=tmp_db)
    wd = server._watchdog_liveness(tmp_db)
    assert wd["running"] is False
    assert "no heartbeat" in wd["note"]


def test_non_heartbeat_and_malformed_events_cannot_hide_watchdog(tmp_db):
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "heartbeat",
            "checked": 7, "blind": 0}, conn=tmp_db)
    ev.emit(EventKind.SYSTEM, {"service": "watchdog", "event": "stopped",
            "note": "heartbeat"}, conn=tmp_db)
    tmp_db.execute("INSERT INTO events (ts_ms,kind,level,payload) VALUES (1,'system','info',?)",
                   ('{bad heartbeat json',))
    wd = server._watchdog_liveness(tmp_db)
    assert wd["running"] is True
    assert wd["checked"] == 7
