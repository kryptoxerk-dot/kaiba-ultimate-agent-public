"""kaiba/execution/snipe.py: the Robinhood mark backlog cannot rebuild itself (design
2026-10-21 step 0).

MEASURED on the box 2026-10-21: 53 open 10-09 Robinhood observations were retried every 30 s
as one batch of 212+ calls (218 of 218 rate-limited in 6 h) on the ``robinhood-rpc`` bucket
protection's Pons reads share. Three bounds, one test each:

* ``due_marks`` expires a row more than ``max(horizons) + MARK_EXPIRY_GRACE_S`` old;
* ``mark_rh`` values at most ``RH_MARK_BATCH_MAX`` observations per request;
* ``Sniper.mark_once`` skips ``mark_rh`` when no Robinhood observation was recorded for
  ``RH_FEED_QUIET_S`` (keyed on observations, not the stream's ``last_block``).

All addresses are fake.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf

T0 = 1_791_000_000_000  # ms
HORIZONS = [300, 900, 3600]


def _addr(i: int, tag: str) -> str:
    return "0x" + tag * 4 + format(i, "032x")


def _rh_observation(conn, i: int, *, entry_ms: int, priced: bool = True) -> None:
    launch = lf.Launch(chain=Chain.ROBINHOOD, token=_addr(i, "70"), venue="pons", creator=_addr(i, "de"),
                       curve=_addr(i, "c0"), launched_ms=entry_ms, received_ms=entry_ms)
    feats = {"curve": launch.curve, "fee_bps": 100, "creator_tax_bps": 0}
    entry = ({"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": 10**24, "shadow_quote": 10**16}
             if priced else {"basis": "curve_unread"})
    snipe.record_observation(conn, launch, snipe.Verdict(False, [], 0.0, "", feats), entry, at_ms=entry_ms)


def _counting_rpc(batches: list[list]):
    def fake(calls, **kw):
        batches.append(list(calls))
        return [hex(10**18)] * len(calls)
    return fake


# ------------------------------------------------------------------------- (1) the age bound

def test_rows_past_the_last_horizon_plus_grace_expire_and_are_never_returned(tmp_db):
    """53 rows entered 12 days ago (the box's 10-09 backlog) -> 0 due, all ``expired``."""
    old = T0 - 12 * 86_400_000
    for i in range(53):
        _rh_observation(tmp_db, i, entry_ms=old + i)
    _rh_observation(tmp_db, 999, entry_ms=T0 - 301_000)  # control: young, due for its 300 s mark
    rows = snipe.due_marks(tmp_db, HORIZONS, at_ms=T0)
    assert [r["token"] for r in rows] == [_addr(999, "70")]
    status = {r["status"]: r["n"] for r in fetch_all(
        tmp_db, f"SELECT status, COUNT(*) AS n FROM {snipe.TABLE} GROUP BY status")}
    assert status == {"expired": 53, "open": 1}
    # an expired row is never handed to a marker again
    assert snipe.due_marks(tmp_db, HORIZONS, at_ms=T0 + 30_000)[0]["token"] == _addr(999, "70")


def test_the_bound_is_the_last_horizon_plus_one_hour_not_less(tmp_db):
    """A row inside its grace hour (60-min mark due, not yet taken) is still marked."""
    edge = (max(HORIZONS) + snipe.MARK_EXPIRY_GRACE_S) * 1000
    _rh_observation(tmp_db, 1, entry_ms=T0 - edge + 1_000)  # 1 s inside the bound
    _rh_observation(tmp_db, 2, entry_ms=T0 - edge - 1_000)  # 1 s past it
    assert [r["token"] for r in snipe.due_marks(tmp_db, HORIZONS, at_ms=T0)] == [_addr(1, "70")]
    row = fetch_one(tmp_db, f"SELECT status, marks_json FROM {snipe.TABLE} WHERE token=?", (_addr(2, "70"),))
    assert row["status"] == "expired" and row["marks_json"] == "{}"


# ------------------------------------------------------------------------- (2) the batch cap

def test_mark_rh_values_at_most_the_cap_per_request(tmp_db, monkeypatch):
    for i in range(53):
        _rh_observation(tmp_db, i, entry_ms=T0 - 400_000 + i)
    batches: list[list] = []
    monkeypatch.setattr(snipe, "rh_rpc", _counting_rpc(batches))
    rows = snipe.due_marks(tmp_db, HORIZONS, at_ms=T0)
    assert len(rows) == 53
    assert snipe.mark_rh(tmp_db, rows, HORIZONS, at_ms=T0) == snipe.RH_MARK_BATCH_MAX == 20
    assert len(batches) == 1
    curves = {args[0]["to"] for _method, args in batches[0]}
    assert len(curves) == snipe.RH_MARK_BATCH_MAX
    assert len(batches[0]) == snipe.RH_MARK_BATCH_MAX * len(snipe.RESERVE_SELECTORS)
    # the oldest are valued first; the rest wait for the next pass, still open
    marked = fetch_one(tmp_db, f"SELECT COUNT(*) AS n FROM {snipe.TABLE} WHERE marks_json != '{{}}'")["n"]
    assert marked == 20
    assert len(snipe.due_marks(tmp_db, HORIZONS, at_ms=T0)) == 33


# ------------------------------------------------------------------------- (3) the quiet-feed skip

LANE_WIRED = snipe.LANE_VALUE in {x.value for x in Lane}


@pytest.fixture
def sniper(tmp_db, monkeypatch):
    if not LANE_WIRED:
        pytest.skip("Lane 'launch-snipe' not wired")
    monkeypatch.setattr(snipe, "params", lambda cfg=None: {**snipe.DEFAULT_PARAMS, "mark_horizons_s": [300, 900]})
    monkeypatch.setattr(snipe, "get_conn", lambda path=None: tmp_db)
    monkeypatch.setattr(snipe, "mark_bsc", lambda *a, **k: 0)
    return SimpleNamespace(s=snipe.Sniper(tmp_db, chains=[]), conn=tmp_db)


def test_no_new_robinhood_observation_for_ten_minutes_skips_the_rh_marks(sniper, monkeypatch):
    _rh_observation(sniper.conn, 1, entry_ms=T0)
    batches: list[list] = []
    monkeypatch.setattr(snipe, "rh_rpc", _counting_rpc(batches))
    at = T0 + 901_000  # the 900 s mark is due; the newest RH observation is 901 s old
    assert len(snipe.due_marks(sniper.conn, [300, 900], at_ms=at)) == 1
    assert not snipe.rh_feed_alive(sniper.conn, at_ms=at)
    assert sniper.s.mark_once(at_ms=at) == 0
    assert batches == [] and sniper.s.stats["rh_marks_skipped"] == 1


def test_a_recent_robinhood_observation_lets_the_marks_run(sniper, monkeypatch):
    """Positive control for the skip: the same due row, with the feed alive."""
    _rh_observation(sniper.conn, 1, entry_ms=T0)
    _rh_observation(sniper.conn, 2, entry_ms=T0 + 800_000, priced=False)  # 101 s before the pass
    batches: list[list] = []
    monkeypatch.setattr(snipe, "rh_rpc", _counting_rpc(batches))
    at = T0 + 901_000
    assert snipe.rh_feed_alive(sniper.conn, at_ms=at)
    assert sniper.s.mark_once(at_ms=at) == 1
    assert len(batches) == 1 and "rh_marks_skipped" not in sniper.s.stats


def test_the_quiet_window_is_ten_minutes():
    assert snipe.RH_FEED_QUIET_S == 600
