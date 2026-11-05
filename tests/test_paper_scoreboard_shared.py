"""One paper rule for every reader (2026-10-23, step 3 follow-up).

The owner funds the agent again only when paper shows profit honestly, and he reads paper results in
three places: the daily Telegram report (``kaiba.learning.paper_report``), the operator console
(``dashboard.trading``) and the agent's answer to the console's "Paper results" question (the
``kaiba_performance`` MCP tool). Before this the console's trade summary claimed "the same
exclusions as the paper report" and applied only the 10-18 placeholder one, and ``kaiba_performance``
applied none: every dossier-priced robinhood/bsc row, every untagged eth/base row and every
pre-cutoff row counted there. One book, three readers, the same n per strategy -- or the test fails.
"""

from __future__ import annotations

import pytest

from dashboard import trading
from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import paper_costs as pc
from kaiba.learning import paper_report as PR
from kaiba.mcp import server
from tests.test_paper_entry_quote_evm import LANDED

HOUR = 3_600_000


@pytest.fixture
def db(tmp_db, monkeypatch):
    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    trading.clear_caches()
    yield tmp_db
    trading.clear_caches()


def _pos(conn, pid: str, chain: str, *, ret: float = 0.5, lane: str = "sm-trenches", opened_ms: int | None = None,
         tag: str | None = None, transfer_tax: str | None = None) -> None:
    """A closed paper position an hour ago, its ``trades`` row, and (``tag``) its paper_costs entry tag."""
    now = now_ms()
    closed = now - HOUR
    opened = opened_ms if opened_ms is not None else closed - HOUR
    cost = 10**18
    pnl = int(cost * ret)
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, chain, f"T{pid}", lane, "shadow", opened, closed, "0", "0", str(cost), str(cost + pnl), str(pnl),
         "trailing_stop"),
    )
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, hold_s, "
        "cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"t_{pid}", pid, lane, "shadow", chain, f"T{pid}", opened, closed, 3600, str(cost), str(cost + pnl),
         str(pnl), ret * 100, "trailing_stop"),
    )
    if tag is not None:
        pc.record_entry_basis(conn, pid, Chain(chain), source=tag, fill=None, buy_tax_bps=0, sell_tax_bps=0,
                              tax_in_price=False, transfer_tax=transfer_tax, landing=LANDED)
        pc.record_exit_basis(conn, pid, Chain(chain), mark_source="evm-pool:x", fill="v2_formula", tax_bps=0,
                             filled=True)


def _book(conn) -> None:
    for i in range(3):
        _pos(conn, f"sol{i}", "sol", ret=0.1)
    _pos(conn, "sol_old", "sol", opened_ms=PR.PAPER_HONEST_MS - HOUR)  # opened before the cut
    for i in range(2):
        _pos(conn, f"bsc_pool{i}", "bsc", tag="pool:pancake-v2:v2_formula")
    for i in range(2):
        _pos(conn, f"bsc_dossier{i}", "bsc", tag="dossier")
    _pos(conn, "bsc_untagged", "bsc")
    _pos(conn, "rh_pons", "robinhood", tag="pool:uniswap-v4:v4_quoter", transfer_tax=pc.TRANSFER_TAX_PONS)
    _pos(conn, "rh_other", "robinhood", tag="pool:uniswap-v3:quoter_v2", transfer_tax=pc.TRANSFER_TAX_UNMEASURED)
    for i in range(2):
        _pos(conn, f"eth{i}", "eth", lane="manual")
    conn.commit()


EXPECTED_N = {("sm-trenches", "sol"): 3, ("sm-trenches", "bsc"): 2, ("sm-trenches", "robinhood"): 1}
EXPECTED_LEFT_OUT = {PR.BEFORE_HONEST: 1, PR.NOT_POOL_PRICED: 2 + 1 + 1 + 2}


def test_the_report_the_console_and_the_agent_count_the_same_paper_rows(db):
    _book(db)
    now = now_ms()
    board = PR.scoreboard(db, now)
    assert {(p.score.lane, p.score.chain): p.score.n for p in board.papers} == EXPECTED_N

    summary = trading.trade_summary(12)
    console = {(r["lane"], r["chain"]): r["n"] for r in summary["rows"] if r["mode"] == "shadow"}
    assert console == EXPECTED_N, summary
    assert summary["excluded_by_reason"] == EXPECTED_LEFT_OUT

    shown = trading.paper_scoreboard()
    assert {(r["lane"], r["chain"]): r["n"] for r in shown["rows"]} == EXPECTED_N
    assert shown["not_pool_priced"] == {"bsc": 3, "eth": 2, "robinhood": 1}

    agent = server.kaiba_performance(days=12, mode="shadow")
    assert {(r["lane"], r["chain"]): r["n"] for r in agent["by_chain"]} == EXPECTED_N, agent["by_chain"]
    assert agent["coverage"]["paper_rows_not_counted"] == EXPECTED_LEFT_OUT
    assert {(r["lane"], r["chain"]): r["n"] for r in agent["paper_scoreboard"]["rows"]} == EXPECTED_N
    assert {(r["lane"], r["chain"]): r["verdict"] for r in agent["paper_scoreboard"]["rows"]} == \
        {(p.score.lane, p.score.chain): p.score.verdict for p in board.papers}


def test_the_console_names_why_a_paper_trade_is_left_out(db):
    _book(db)
    flags = {t["position_id"]: t["flags"] for t in trading.closed_trades("shadow", days=1)}
    assert "entry not priced from the pool or curve (excluded from scores)" in flags["bsc_dossier0"]
    assert "entry not priced from the pool or curve (excluded from scores)" in flags["eth0"]
    assert "entry not priced from the pool or curve (excluded from scores)" in flags["rh_other"]
    assert "opened before the honest cutoff (excluded from scores)" in flags["sol_old"]
    assert not any("excluded" in f for f in flags["bsc_pool0"] + flags["sol0"] + flags["rh_pons"])


def test_a_console_that_cannot_build_the_rule_counts_no_paper_row(db, monkeypatch):
    """Default-deny holds in the reader too: no filter, no paper statistic -- never every row."""
    _book(db)

    def broken(*a, **k):
        raise RuntimeError("no rule")

    monkeypatch.setattr(PR, "paper_filter", broken)
    trading.clear_caches()
    summary = trading.trade_summary(12)
    assert not [r for r in summary["rows"] if r["mode"] == "shadow"]
    assert summary["excluded_by_reason"] == {trading.FILTER_UNAVAILABLE: 13}


def test_the_trial_table_leaves_out_the_same_off_sol_rows_for_the_report_and_the_console(db, monkeypatch):
    from kaiba.learning import time_exit_trial

    _book(db)
    trial = time_exit_trial.Trial(lane="sm-trenches", hold_s=60, since_ms=PR.PAPER_HONEST_MS - 2 * HOUR, split="all")
    trials = {"sm-trenches": trial}
    monkeypatch.setattr(trading, "_trials", lambda: trials)
    rows = PR.trial_board(db, now_ms(), trials)
    n = {r["chain"]: r["n_treat"] for r in rows}
    assert n == {"sol": 4, "bsc": 2, "robinhood": 1}, rows  # the trial window replaces the date cut
    assert trading.trial_results() == rows


def test_the_console_lane_returns_command_counts_by_the_same_rule(db, capsys):
    """``scripts/dashboard/lane_returns.py`` (the console's "Lane returns by window" command)."""
    import runpy
    from pathlib import Path

    _book(db)
    script = Path(__file__).resolve().parents[1] / "scripts" / "dashboard" / "lane_returns.py"
    assert runpy.run_path(str(script))["main"]() == 0
    out = capsys.readouterr().out
    assert "7 paper trades left out by the paper report's rule: before_honest_cutoff 1, not_pool_priced 6." in out
    lines = {ln.split()[1] + "/" + ln.split()[3]: ln.split() for ln in out.splitlines() if ln.startswith("paper ")}
    assert {k: int(v[-2]) for k, v in lines.items()} == {"sm-trenches/bsc": 2, "sm-trenches/robinhood": 1,
                                                          "sm-trenches/sol": 3}


# ----------------------------------------------------------------------------- the console header and activity feed
#
# 2026-10-23 (step 3 follow-up): the "paper today $X / n closed / w won" header on every console page
# (``dashboard.intel.ticker``) summed EVERY closed shadow position today, and the activity feed's
# sold / won / mean (``dashboard.intel.activity``) mixed live with paper and counted every paper close
# -- including each row the scoreboard leaves out. The owner reads the header first.


def _console(db, monkeypatch):
    """The book above plus two LOSING rows the scoreboard leaves out: a dossier-priced bsc entry and a
    booked write-off. Counted by mistake they would change the header's n, wins and sign."""
    from dashboard import data, intel

    _book(db)
    _pos(db, "bsc_dossier_loser", "bsc", ret=-0.9, tag="dossier")
    _pos(db, "sol_writeoff", "sol", ret=-1.0)
    db.execute("UPDATE positions SET exit_reason='abandoned_unpriceable' WHERE position_id='sol_writeoff'")
    db.commit()
    # every row closed an hour ago; make "today" start before that whatever the clock says
    monkeypatch.setattr(data, "day_start_ms", lambda now=None: now_ms() - 3 * HOUR)
    intel.clear_caches()
    return intel


def test_the_console_header_counts_the_paper_rows_the_report_counts(db, monkeypatch):
    """Mutant pinned: the header counting every closed shadow row (the rule dropped) must fail here."""
    intel = _console(db, monkeypatch)
    board = PR.scoreboard(db, now_ms())
    counted = sum(p.score.n for p in board.papers)
    assert counted == sum(EXPECTED_N.values()) == 6
    paper = intel.ticker()["today"]["paper"]
    assert paper["n"] == counted, paper
    assert paper["wins"] == 6, "every counted row closed up; the two losers are not counted"
    assert paper["not_counted"] == sum(EXPECTED_LEFT_OUT.values()) + 2 == 9
    assert paper["not_counted_by"] == {"opened before the honest cutoff": 1,
                                       "entry not priced from the pool or curve": 7, "booked write-off": 1}


def test_the_console_header_caps_each_paper_close_at_the_scoreboards_300_percent(db, monkeypatch):
    """One +1000% paper close cannot carry the header: each counted close is capped at
    ``trading.OUTCOME_CAP`` (+300%), the cap every scoreboard applies. Mutant pinned: removing the cap
    in ``intel.ticker`` must fail here."""
    from dashboard import data, intel

    _pos(db, "sol_moon", "sol", ret=10.0)
    _pos(db, "sol_loss", "sol", ret=-0.5)
    db.commit()
    monkeypatch.setattr(data, "day_start_ms", lambda now=None: now_ms() - 3 * HOUR)
    monkeypatch.setattr(intel, "native_usd", lambda chain: 100.0)
    intel.clear_caches()
    paper = intel.ticker()["today"]["paper"]
    cost_sol = 10**18 / 10**9  # `_pos` books cost 10**18 base units; sol has 9 decimals
    assert paper["n"] == 2 and paper["wins"] == 1 and paper["priced"] and paper["not_counted"] == 0, paper
    assert trading.OUTCOME_CAP == 3.0
    assert paper["usd"] == pytest.approx((trading.OUTCOME_CAP - 0.5) * cost_sol * 100.0), paper
    assert paper["usd"] < (10.0 - 0.5) * cost_sol * 100.0
    # the header and the scoreboard agree on the capped mean
    board = {(p.score.lane, p.score.chain): p.score for p in PR.scoreboard(db, now_ms()).papers}
    score = board[("sm-trenches", "sol")]
    assert score.n == 2 and score.mean == pytest.approx((trading.OUTCOME_CAP - 0.5) / 2)
    assert paper["usd"] / (2 * cost_sol * 100.0) == pytest.approx(score.mean)


def test_the_activity_feed_counts_paper_by_the_rule_and_live_apart(db, monkeypatch):
    intel = _console(db, monkeypatch)
    _pos(db, "sol_live", "sol", ret=-0.2)
    db.execute("UPDATE positions SET mode='live' WHERE position_id='sol_live'")
    db.commit()
    intel.clear_caches()
    act = intel.activity()
    board = PR.scoreboard(db, now_ms())
    assert act["counts"]["sells"] == sum(p.score.n for p in board.papers) == 6
    assert act["counts"]["wins"] == 6 and act["counts"]["mean"] > 0
    assert act["counts_live"] == {"sells": 1, "wins": 0, "mean": -0.2}
    assert act["not_counted"] == 9
    rows = {e["token"]: e for e in act["feed"] if e["type"] == "sell"}
    assert "not counted (entry not priced from the pool or curve)" in rows["Tbsc_dossier_loser"]["sub"]
    assert "not counted" not in rows["Tbsc_pool0"]["sub"] and "not counted" not in rows["Tsol_live"]["sub"]


def test_a_console_header_that_cannot_build_the_rule_counts_no_paper_row(db, monkeypatch):
    intel = _console(db, monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("no rule")

    monkeypatch.setattr(PR, "paper_filter", broken)
    intel.clear_caches()
    paper = intel.ticker()["today"]["paper"]
    assert paper["n"] == 0 and paper["not_counted"] == 15
    assert paper["not_counted_by"] == {"paper filter unavailable": 15}


def test_the_header_and_the_feed_render_what_is_not_counted(db, monkeypatch, tmp_path):
    import shutil

    from fastapi.testclient import TestClient

    from dashboard import auth  # noqa: F401 - the app's session plumbing
    from dashboard.app import create_app
    from kaiba.core.config import DEFAULT_RISK_PATH, get_settings

    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", "header-test-password")
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    get_settings.cache_clear()
    _console(db, monkeypatch)
    client = TestClient(create_app())
    try:
        assert client.post("/login", data={"password": "header-test-password"},
                           follow_redirects=False).status_code == 303
        ticker = client.get("/terminal/ticker").text
        assert "6 closed · 6 won · not counted: 9" in ticker, ticker
        assert "entry not priced from the pool or curve ×7" in ticker
        activity = client.get("/terminal/activity").text
        assert "Sold (paper, counted)" in activity and "paper not counted: 9" in activity
    finally:
        client.close()
        get_settings.cache_clear()
