"""An ingest feed that goes down or silent reaches the operator (2026-10-21).

From 2026-10-09 to 10-21 four feeds (sol_wallets, rh_wallets, flap, leader_exit) were dark
and nothing paged anyone. Nothing read ``ingest_status``; the runner wrote the same "idle"
text for a feed that lost its endpoint as for telegram, which was never configured; and
from 10-09 to 10-13 three of them sat in ``running`` with a frozen ``last_event_ms``.

Each guard here has a test that fails with the guard removed (the work log records the
mutation runs):

* down for > 300 s, AND it used to produce or it is expected  -> pages (``_down_alert``)
* running past the feed's silence limit                        -> pages (``_silent_alert``)
* leader_exit silent only counts with >= 3 positions to observe
* one page per ref; a feed that recovers and dies again pages again
* the runner keeps the listener's idle reason, redacted, and the idle event is an error
  when the feed used to produce
* the daily RED line repeats it (``kaiba.mcp.server._red_flags``)
* a replay of the box's real ``ingest_status`` rows pages exactly the four dark feeds
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import EventKind
from kaiba.ingest import pumpportal, runner
from kaiba.mcp import server
from kaiba.ops import trade_notify as N

#: The box read behind the replay fixture: 2026-10-21 05:37 UTC.
NOW = 1_792_561_046_850
SEC, MIN, HOUR, DAY = 1_000, 60_000, 3_600_000, 86_400_000
FIXTURE = Path(__file__).parent / "fixtures" / "ingest" / "ingest_status_box_20261021.json"


@pytest.fixture(autouse=True)
def _fresh_position_cache():
    N._POSITION_COUNTS.clear()
    yield
    N._POSITION_COUNTS.clear()


def put_feed(conn, feed: str, state: str, *, events_seen: int = 0, last_event_ms: int | None = None,
             started_ms: int | None = NOW - 2 * DAY, updated_ms: int = NOW - 10 * MIN,
             last_error: str | None = None, restarts: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ingest_status (feed, state, started_ms, last_event_ms, events_seen, "
        "restarts, last_error, updated_ms) VALUES (?,?,?,?,?,?,?,?)",
        (feed, state, started_ms, last_event_ms, events_seen, restarts, last_error, updated_ms),
    )


def paged(conn, now: int = NOW) -> dict[str, N.FeedAlert]:
    return {a.feed: a for a in N.feed_alerts(conn, now_ms=now)}


@pytest.fixture
def sent(monkeypatch):
    """``once()`` on the real send path, with Telegram replaced and the clock frozen."""
    box: list[str] = []
    monkeypatch.setattr(N, "send", lambda text, **kw: (box.append(text), True)[1])
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    monkeypatch.setattr(N, "channel_id", lambda: None)
    monkeypatch.setattr(N, "time", SimpleNamespace(time=lambda: NOW / 1000))
    return box


def feed_pages(box: list[str]) -> list[str]:
    return [t for t in box if t.startswith("INGEST FEED")]


# ------------------------------------------------------------------ the down rule


def test_idle_with_events_seen_pages_and_telegram_at_zero_does_not(tmp_db):
    # pumpportal is in neither EXPECTED_FEEDS nor FEED_SILENT_MS: only its history pages it.
    assert "pumpportal" not in N.EXPECTED_FEEDS
    put_feed(tmp_db, "pumpportal", "idle", events_seen=918_465, last_event_ms=NOW - 2 * HOUR)
    put_feed(tmp_db, "telegram", "idle", events_seen=0, last_event_ms=None)
    got = paged(tmp_db)
    assert set(got) == {"pumpportal"}
    assert got["pumpportal"].kind == "down"
    assert got["pumpportal"].text.startswith("INGEST FEED DOWN")
    assert "pumpportal is idle (marked " in got["pumpportal"].text
    assert "918,465 events in its life" in got["pumpportal"].text


def test_stopped_for_60s_during_a_restart_does_not_page(tmp_db):
    put_feed(tmp_db, "sol_wallets", "stopped", events_seen=113_709, last_event_ms=NOW - 2 * MIN,
             updated_ms=NOW - 60 * SEC)
    assert paged(tmp_db) == {}
    # positive control: the same row left stopped past the grace is a dark feed
    put_feed(tmp_db, "sol_wallets", "stopped", events_seen=113_709, last_event_ms=NOW - 2 * MIN,
             updated_ms=NOW - N.FEED_DOWN_GRACE_MS - SEC)
    assert set(paged(tmp_db)) == {"sol_wallets"}


def test_leader_exit_pages_when_expected_and_never_produced(tmp_db):
    """0 events in its life: only the declared expectation can tell it from telegram."""
    put_feed(tmp_db, "leader_exit", "idle", events_seen=0, last_event_ms=None,
             last_error="idle: no Alchemy Solana endpoint (SOLANA_RPC_URL with /v2/)")
    got = paged(tmp_db)
    assert set(got) == {"leader_exit"}
    assert "expected to run and has never produced an event" in got["leader_exit"].text
    assert "reason: idle: no Alchemy Solana endpoint" in got["leader_exit"].text
    assert got["leader_exit"].ref == "leader_exit:idle:None"


def test_a_crash_reason_carrying_a_key_is_redacted_before_it_is_sent(tmp_db):
    """A ``crashed`` row's last_error is an exception string; httpx renders the full URL."""
    secret = "KEYSECRET123abcdefghijklmnopqrstu"
    put_feed(tmp_db, "flap", "crashed", events_seen=10, last_event_ms=NOW - DAY,
             last_error=f"HTTPStatusError: 429 for url https://bnb-mainnet.g.alchemy.com/v2/{secret}")
    alert = paged(tmp_db)["flap"]
    assert "https://bnb-mainnet.g.alchemy.com/v2/" in alert.text
    assert "KEYSECRET123" not in alert.text and "KEYSECRET123" not in alert.line


def test_rh_wallets_idle_says_it_waits_on_the_owner_not_that_it_crashed(tmp_db):
    put_feed(tmp_db, "rh_wallets", "idle", events_seen=520, last_event_ms=NOW - 12 * DAY,
             last_error="idle: no websocket endpoint")
    alert = paged(tmp_db)["rh_wallets"]
    assert "rh_wallets idle: needs a websocket provider; owner decision" in alert.text
    assert "owner decision" in alert.text.splitlines()[0]
    assert "DOWN" not in alert.text and "crash" not in alert.text.replace("Not a crash", "")
    assert alert.line == "rh_wallets idle: needs a websocket provider; owner decision"


# ------------------------------------------------------------------ the silence rule


def test_a_running_feed_with_a_13h_old_event_pages(tmp_db):
    """The 10-09 to 10-13 case: `running`, in a 429 loop, last event frozen."""
    for feed in ("flap", "sol_wallets", "rh_wallets"):
        put_feed(tmp_db, feed, "running", events_seen=1_000, last_event_ms=NOW - 13 * HOUR,
                 started_ms=NOW - 2 * DAY)
    got = paged(tmp_db)
    # flap (30 min) and sol_wallets (6 h) are past their limits; rh_wallets (24 h) is not
    assert set(got) == {"flap", "sol_wallets"}
    assert got["flap"].kind == "silent" and got["flap"].text.startswith("INGEST FEED SILENT")
    assert "written nothing for 13.0h (limit 30m)" in got["flap"].text
    assert got["flap"].ref == f"flap:running:{NOW - 13 * HOUR}"
    # positive control: a fresh event, nothing to say
    put_feed(tmp_db, "flap", "running", events_seen=1_001, last_event_ms=NOW - MIN, started_ms=NOW - 2 * DAY)
    assert "flap" not in paged(tmp_db)


def test_a_crash_looping_feed_is_judged_in_reconnecting_too(tmp_db):
    put_feed(tmp_db, "flap", "reconnecting", events_seen=1_000, last_event_ms=NOW - 13 * HOUR)
    alert = paged(tmp_db)["flap"]
    assert alert.kind == "silent"
    assert alert.ref == f"flap:running:{NOW - 13 * HOUR}"  # one ref across running/reconnecting


def test_a_restart_gives_a_feed_one_full_window(tmp_db):
    """A feed re-enabled after 12 dark days must not page in its first poll."""
    put_feed(tmp_db, "flap", "running", events_seen=52_136, last_event_ms=NOW - 12 * DAY,
             started_ms=NOW - 5 * MIN)
    assert paged(tmp_db) == {}
    put_feed(tmp_db, "flap", "running", events_seen=52_136, last_event_ms=NOW - 12 * DAY,
             started_ms=NOW - 31 * MIN)
    assert set(paged(tmp_db)) == {"flap"}


def _position(conn, pid: str, opened_ms: int, *, chain: str = "sol", lane: str = "sm-trenches",
              mode: str = "live") -> None:
    conn.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms) VALUES (?,?,?,?,?,?)",
                 (pid, chain, f"tok_{pid}", lane, mode, opened_ms))


def test_leader_exit_silence_counts_only_with_positions_to_observe(tmp_db):
    put_feed(tmp_db, "leader_exit", "running", events_seen=0, last_event_ms=None, started_ms=NOW - 7 * HOUR)
    _position(tmp_db, "a", NOW - 6 * HOUR)
    _position(tmp_db, "b", NOW - 5 * HOUR, mode="shadow")
    # none of these is a position leader_exit watches
    _position(tmp_db, "c", NOW - 4 * HOUR, chain="bsc")
    _position(tmp_db, "d", NOW - 4 * HOUR, lane="launch-snipe")
    _position(tmp_db, "e", NOW - 4 * HOUR, mode="canary")
    _position(tmp_db, "f", NOW - 8 * HOUR)  # before this run started
    assert paged(tmp_db) == {}, "two positions to observe is not enough to call it dead"
    _position(tmp_db, "g", NOW - 3 * HOUR)
    N._POSITION_COUNTS.clear()
    alert = paged(tmp_db)["leader_exit"]
    assert alert.kind == "silent"
    assert "3 sol sm-trenches positions opened in that time" in alert.text


def test_the_position_count_is_cached_for_a_minute(tmp_db):
    put_feed(tmp_db, "leader_exit", "running", events_seen=0, started_ms=NOW - 7 * HOUR)
    for i in range(3):
        _position(tmp_db, f"p{i}", NOW - HOUR)
    assert "leader_exit" in paged(tmp_db)
    tmp_db.execute("DELETE FROM positions")
    assert "leader_exit" in paged(tmp_db, NOW + 30 * SEC)  # cached
    assert "leader_exit" not in paged(tmp_db, NOW + 61 * SEC)  # re-counted


def test_leader_exit_scope_matches_the_observer():
    from kaiba.learning import leader_exit as le

    assert N.LEADER_EXIT_SCOPE == (le.CHAIN, le.LANE, tuple(le.DEFAULT_PARAMS["modes"]))
    assert le.FEED == "leader_exit" and le.FEED in N.EXPECTED_FEEDS and le.FEED in N.FEED_SILENT_MS


# ------------------------------------------------------------------ delivery and dedupe


def test_the_same_ref_pages_once(tmp_db, sent):
    put_feed(tmp_db, "flap", "idle", events_seen=52_136, last_event_ms=NOW - 12 * DAY)
    N.once(tmp_db)
    N.once(tmp_db)
    N.once(tmp_db)
    assert len(feed_pages(sent)) == 1, sent
    rows = fetch_all(tmp_db, "SELECT source, ref FROM notify_alerts WHERE source='ingest'")
    assert rows == [{"source": "ingest", "ref": f"flap:idle:{NOW - 12 * DAY}"}]


def test_a_feed_that_recovers_then_dies_again_pages_again(tmp_db, sent):
    first = NOW - 12 * DAY
    put_feed(tmp_db, "flap", "idle", events_seen=52_136, last_event_ms=first)
    N.once(tmp_db)
    # it comes back and writes ...
    put_feed(tmp_db, "flap", "running", events_seen=52_136, last_event_ms=first, started_ms=NOW - HOUR)
    runner.note_events("flap", 40, tmp_db, last_event_ms=NOW - MIN)
    N.once(tmp_db)
    assert len(feed_pages(sent)) == 1
    # ... and dies again
    tmp_db.execute("UPDATE ingest_status SET state='idle', updated_ms=? WHERE feed='flap'", (NOW - 10 * MIN,))
    N.once(tmp_db)
    pages = feed_pages(sent)
    assert len(pages) == 2, pages
    refs = {r["ref"] for r in fetch_all(tmp_db, "SELECT ref FROM notify_alerts WHERE source='ingest'")}
    assert refs == {f"flap:idle:{first}", f"flap:idle:{NOW - MIN}"}


def test_a_failed_send_is_retried_next_pass(tmp_db, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(N, "credentials", lambda: ("tok", "chat"))
    monkeypatch.setattr(N, "channel_id", lambda: None)
    monkeypatch.setattr(N, "time", SimpleNamespace(time=lambda: NOW / 1000))
    put_feed(tmp_db, "flap", "idle", events_seen=1, last_event_ms=NOW - DAY)
    monkeypatch.setattr(N, "send", lambda text, **kw: (calls.append(text), False)[1])
    N.once(tmp_db)
    assert fetch_all(tmp_db, "SELECT ref FROM notify_alerts WHERE source='ingest'") == []
    monkeypatch.setattr(N, "send", lambda text, **kw: (calls.append(text), True)[1])
    N.once(tmp_db)
    assert len(feed_pages(calls)) == 2  # refused once, delivered on the next pass
    assert len(fetch_all(tmp_db, "SELECT ref FROM notify_alerts WHERE source='ingest'")) == 1


def test_an_unreadable_status_table_never_stops_the_trade_feed(tmp_db, sent):
    tmp_db.execute("DROP TABLE ingest_status")
    out = N.once(tmp_db)  # must not raise
    assert out["failed"] == 0 and feed_pages(sent) == []


# ------------------------------------------------------------------ the runner


@pytest.fixture
def fast_runner(monkeypatch):
    async def no_wait(stop, seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(runner, "_wait", no_wait)
    monkeypatch.setattr(pumpportal, "_rand", lambda: 0.0)


def _idle_events(conn, feed: str) -> list[dict]:
    out = []
    for row in fetch_all(conn, "SELECT level, payload FROM events WHERE kind=?", (EventKind.SYSTEM.value,)):
        payload = json.loads(row["payload"])
        if payload.get("component") == f"ingest.{feed}" and payload.get("status") == "idle":
            out.append({"level": row["level"], **payload})
    return out


def _supervise(conn, feed: str, result) -> None:
    async def listener(stop):
        return result

    asyncio.run(runner._supervise(feed, listener, asyncio.Event(), conn=conn))


def test_the_runner_stores_the_idle_reason_in_last_error(tmp_db, fast_runner):
    _supervise(tmp_db, "probe_idle", {"idle": "no websocket endpoint"})
    row = fetch_one(tmp_db, "SELECT state, last_error FROM ingest_status WHERE feed='probe_idle'")
    assert row == {"state": "idle", "last_error": "idle: no websocket endpoint"}
    (event,) = _idle_events(tmp_db, "probe_idle")
    assert event["error"] == "idle: no websocket endpoint"


def test_a_listener_with_no_reason_keeps_the_nothing_configured_text(tmp_db, fast_runner):
    _supervise(tmp_db, "probe_none", None)
    row = fetch_one(tmp_db, "SELECT state, last_error FROM ingest_status WHERE feed='probe_none'")
    assert row == {"state": "idle", "last_error": runner.IDLE_NO_REASON}


def test_the_idle_event_is_an_error_when_the_feed_used_to_produce(tmp_db, fast_runner):
    put_feed(tmp_db, "probe_hist", "running", events_seen=113_709, last_event_ms=NOW - DAY)
    _supervise(tmp_db, "probe_hist", {"idle": "no websocket endpoint"})
    _supervise(tmp_db, "probe_new", {"idle": "no websocket endpoint"})
    (hist,) = _idle_events(tmp_db, "probe_hist")
    (new,) = _idle_events(tmp_db, "probe_new")
    assert (hist["level"], hist["events_seen"]) == ("error", 113_709)
    assert (new["level"], new["events_seen"]) == ("warn", 0)


def test_an_idle_reason_never_carries_a_key(tmp_db, fast_runner):
    secret = "KEYSECRET123abcdefghijklmnopqrstu"
    _supervise(tmp_db, "probe_key", {"idle": f"dial failed wss://solana-mainnet.g.alchemy.com/v2/{secret}"})
    row = fetch_one(tmp_db, "SELECT last_error FROM ingest_status WHERE feed='probe_key'")
    assert row["last_error"].startswith("idle: dial failed wss://solana-mainnet.g.alchemy.com/v2/")
    stored = json.dumps([row, fetch_all(tmp_db, "SELECT payload FROM events")])
    assert "KEYSECRET123" not in stored


def test_a_fallback_reason_is_kept_on_the_restart(tmp_db, fast_runner):
    calls = {"n": 0}

    async def listener(stop):
        calls["n"] += 1
        if calls["n"] >= 2:
            stop.set()
            return None
        return {"fallback": "alchemy_monthly_cap"}

    asyncio.run(runner._supervise("probe_fb", listener, asyncio.Event(), conn=tmp_db))
    restarting = [json.loads(r["payload"]) for r in fetch_all(tmp_db, "SELECT payload FROM events")
                  if json.loads(r["payload"]).get("status") == "restarting"]
    assert [e["error"] for e in restarting] == ["returned early (fallback: alchemy_monthly_cap)"]


def test_note_events_zero_does_not_move_last_event_ms(tmp_db):
    put_feed(tmp_db, "flap", "running", events_seen=7, last_event_ms=NOW - DAY, updated_ms=NOW - DAY)
    runner.note_events("flap", 0, tmp_db, last_event_ms=NOW)
    row = fetch_one(tmp_db, "SELECT events_seen, last_event_ms, updated_ms FROM ingest_status WHERE feed='flap'")
    assert row == {"events_seen": 7, "last_event_ms": NOW - DAY, "updated_ms": NOW - DAY}


# ------------------------------------------------------------------ the daily RED line


def _risk():
    from tests.test_mcp_operator_readouts import _risk as risk

    return risk()


def test_red_flags_lists_a_dead_feed(tmp_db):
    put_feed(tmp_db, "flap", "idle", events_seen=52_136, last_event_ms=NOW - 12 * DAY,
             updated_ms=NOW - 10 * HOUR)
    put_feed(tmp_db, "rh_wallets", "idle", events_seen=520, last_event_ms=NOW - 12 * DAY)
    put_feed(tmp_db, "telegram", "idle")
    report = server.health_report(tmp_db, hours=24, now=NOW, risk=_risk())
    assert [f["feed"] for f in report["ingest"]] == ["flap", "rh_wallets"]
    assert "ingest flap idle, last event 12.0d ago" in report["red"]
    assert "ingest rh_wallets idle: needs a websocket provider; owner decision" in report["red"]
    assert not any("telegram" in r for r in report["red"])
    # it repeats: the RED line has no dedupe, a second report says it again
    assert "ingest flap idle, last event 12.0d ago" in server.health_report(
        tmp_db, hours=24, now=NOW, risk=_risk())["red"]


def test_an_unreadable_status_table_is_red_not_silent(tmp_db):
    tmp_db.execute("DROP TABLE ingest_status")
    report = server.health_report(tmp_db, hours=24, now=NOW, risk=_risk())
    assert any(r.startswith("ingest: cannot measure") for r in report["red"]), report["red"]


# ------------------------------------------------------------------ replay of the box


def test_replay_of_the_box_rows_pages_the_four_dark_feeds_only(tmp_db, sent):
    """The box's real ``ingest_status`` at 2026-10-21 05:37 UTC (11 rows, read-only)."""
    snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert snap["read_at_ms"] == NOW
    for r in snap["rows"]:
        put_feed(tmp_db, r["feed"], r["state"], events_seen=r["events_seen"], last_event_ms=r["last_event_ms"],
                 started_ms=r["started_ms"], updated_ms=r["updated_ms"], restarts=r["restarts"],
                 last_error="returned immediately; nothing configured" if r["state"] == "idle" else None)
    got = paged(tmp_db, snap["read_at_ms"])
    assert set(got) == {"sol_wallets", "rh_wallets", "flap", "leader_exit"}
    assert {a.kind for a in got.values()} == {"down"}
    assert "idle: needs a websocket provider; owner decision" in got["rh_wallets"].text
    # telegram (idle, never configured) and rhscannerr (running, 0 events, no limit) stay quiet
    assert "telegram" not in got and "rhscannerr" not in got
    # and through the real send path: four pages, then none on the next pass
    N.once(tmp_db)
    N.once(tmp_db)
    pages = feed_pages(sent)
    assert len(pages) == 4, pages
    assert sum("flap is idle" in p for p in pages) == 1
