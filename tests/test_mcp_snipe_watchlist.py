"""The operator's handle on the launch-snipe watchlists (``skills/launch-snipe``).

``kaiba_set_lane_param`` stores scalars only, so the dev/name watchlists -- lists the snipe
lane re-reads every 60 s -- had no tool. This one validates each entry for its chain, caps
each list, refuses a lane that is not configured, and journals every change.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import LaneConfig, get_risk, save_risk
from kaiba.core.schemas import Lane, LaneMode
from kaiba.mcp import server
from tests.test_mcp import SOL, mcp_db  # noqa: F401 - fixture used by name

EVM = "0xABCDEF0123456789ABCDEF0123456789ABCDEF01"  # synthetic; never a real wallet in a test


@pytest.fixture
def snipe_lane(mcp_db):  # noqa: F811
    risk = get_risk()
    risk.lanes[Lane("launch-snipe")] = LaneConfig(mode=LaneMode.SHADOW, chains=[], params={})
    save_risk(risk)
    return mcp_db


def watch():
    return get_risk().lane(Lane("launch-snipe")).params


def test_an_evm_dev_is_added_normalised_and_journaled(snipe_lane):
    out = server.kaiba_snipe_watchlist("add", "dev", EVM, "robinhood", "runner record")
    assert out["ok"] and watch()["dev_watchlist"] == [EVM.lower()]
    row = snipe_lane.execute("SELECT body FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
    assert "dev_watchlist add" in row["body"]


def test_a_sol_dev_keeps_its_case(snipe_lane):
    assert server.kaiba_snipe_watchlist("add", "dev", SOL, "sol")["ok"]
    assert watch()["dev_watchlist"] == [SOL]


def test_a_dev_that_is_not_an_address_for_its_chain_is_refused(snipe_lane):
    out = server.kaiba_snipe_watchlist("add", "dev", "not-a-wallet", "robinhood")
    assert not out["ok"]
    assert not watch().get("dev_watchlist")


def test_adding_twice_is_a_no_op(snipe_lane):
    server.kaiba_snipe_watchlist("add", "name", "PEPE")
    out = server.kaiba_snipe_watchlist("add", "name", "pepe")
    assert out.get("unchanged") and watch()["name_watchlist"] == ["PEPE"]


def test_a_name_must_be_short_and_printable(snipe_lane):
    assert not server.kaiba_snipe_watchlist("add", "name", "x")["ok"]
    assert not server.kaiba_snipe_watchlist("add", "name", "y" * 33)["ok"]


def test_the_cap_holds(snipe_lane, monkeypatch):
    monkeypatch.setitem(server.SNIPE_WATCHLIST_CAPS, "name", 2)
    server.kaiba_snipe_watchlist("add", "name", "AAA")
    server.kaiba_snipe_watchlist("add", "name", "BBB")
    out = server.kaiba_snipe_watchlist("add", "name", "CCC")
    assert not out["ok"] and watch()["name_watchlist"] == ["AAA", "BBB"]


def test_remove_and_list(snipe_lane):
    server.kaiba_snipe_watchlist("add", "dev", EVM, "robinhood")
    server.kaiba_snipe_watchlist("remove", "dev", EVM, "robinhood")
    listed = server.kaiba_snipe_watchlist("list")
    assert listed["ok"] and listed["dev_watchlist"] == []


def test_an_unconfigured_lane_is_refused_not_created(mcp_db):  # noqa: F811
    risk = get_risk()
    risk.lanes.pop(Lane("launch-snipe"), None)
    save_risk(risk)
    out = server.kaiba_snipe_watchlist("add", "name", "PEPE")
    assert not out["ok"] and Lane("launch-snipe") not in get_risk().lanes


def test_it_is_registered_behind_the_lane_param_policy():
    assert server.TOOLS["kaiba_snipe_watchlist"] is server.kaiba_snipe_watchlist
    assert server.TOOL_OPERATIONS["kaiba_snipe_watchlist"] == "lane_param_set"
