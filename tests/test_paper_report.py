"""The owner's daily paper report and its readiness rule (kaiba/learning/paper_report.py).

Owner 2026-10-13: "report to me daily its progress and let me know when it's confident to
actually live trade". Each test is the failure it prevents:

* a strategy is called ready on ONE measurement, or on one good day;
* a write-off booked by hand (fabricated) moves a paper mean;
* a chain stops paper trading and the report stays quiet about it;
* "ready" is announced every day (noise) or never retracted when it stops passing;
* the message is too long for Telegram.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.learning import paper_report as PR
from kaiba.learning import sprint
from kaiba.ops import scheduler as S
from tests.test_paper_entry_quote_evm import LANDED

DAY = PR.DAY_MS
NOW = 1_800_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def _pos(conn, pid: str, lane: str, chain: str, *, opened_ago_d: float, closed_ago_d: float | None,
         ret: float = 0.1, reason: str = "trailing_stop", mode: str = "shadow") -> None:
    cost = 100_000_000
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, chain, f"T{pid}", lane, mode, int(NOW - opened_ago_d * DAY),
         None if closed_ago_d is None else int(NOW - closed_ago_d * DAY), "0", "0",
         str(cost), str(int(cost * (1 + ret))), str(int(cost * ret)), reason),
    )


def _strategy(conn, lane: str, chain: str, n: int, ret_old: float, ret_new: float, tag: str = "") -> None:
    for i in range(n):
        age = PR.WINDOW_DAYS * (1 - (i + 0.5) / n)  # spread evenly over the window
        ret = ret_old if age > PR.WINDOW_DAYS / 2 else ret_new
        _pos(conn, f"{tag}{lane}{chain}{i}", lane, chain, opened_ago_d=age + 0.01, closed_ago_d=age, ret=ret)


def _replay(conn, items: list[dict], age_h: float = 1) -> None:
    conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                 (sprint.KV_RESULTS, json.dumps(items), int(NOW - age_h * 3_600_000)))


def test_fabricated_write_offs_are_left_out_and_counted(tmp_db):
    _strategy(tmp_db, "sm-trenches", "sol", 50, 0.10, 0.10)
    for i in range(20):
        _pos(tmp_db, f"fab{i}", "sm-trenches", "sol", opened_ago_d=3, closed_ago_d=2.9, ret=-1.0,
             reason="abandoned_unpriceable")
    (p,) = PR.paper_scores(PR.paper_rows(tmp_db, NOW), NOW)
    assert p.excluded == 20 and p.score.n == 50
    assert abs(p.score.mean - 0.10) < 1e-6 and p.score.verdict == "PASS"


def test_a_live_write_off_is_never_left_out():
    # positions with mode live are not in the paper report at all, but the rule must hold anyway
    rows = [("sm-trenches", "sol", NOW, -1.0, "abandoned_unpriceable", "live")]
    (p,) = PR.paper_scores(rows, NOW)
    assert p.excluded == 0 and p.score.n == 1


def test_a_chain_that_stops_paper_trading_is_flagged(tmp_db):
    _pos(tmp_db, "a", "sm-trenches", "sol", opened_ago_d=0.2, closed_ago_d=0.1)
    _pos(tmp_db, "b", "sm-trenches", "bsc", opened_ago_d=3, closed_ago_d=2.9)  # traded earlier, not today
    acts = {a.chain: a for a in PR.activity(tmp_db, NOW)}
    assert not acts["sol"].silent and acts["sol"].opened_24h == 1
    assert acts["bsc"].silent, "a chain that went quiet must be called out"
    assert not acts["robinhood"].silent, "never traded in the window: nothing to compare against"


def _score(verdict: str, lane: str = "sm-trenches", chain: str = "sol", n: int = 50) -> PR.PaperScore:
    s = sprint.Score(lane, chain, n, n // 2, n - n // 2, 0.05, 0.05, 0.05, 0.01, verdict, "")
    return PR.PaperScore(s)


_DAY0 = 1_791_000_000_000  # a UTC midnight-ish anchor; only differences matter


def _day(i: int) -> tuple[str, int]:
    """(``YYYY-MM-DD``, report ms) for day ``i`` of a run of daily reports."""
    import datetime as dt

    ms = _DAY0 + i * PR.DAY_MS
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).date().isoformat(), ms


def _ready(state, verdict, replay, i, *, n=None, replay_age_ms=3_600_000):
    day, ms = _day(i)
    return PR.update_readiness(state, [_score(verdict, n=50 + i if n is None else n)], replay, day,
                               replay_ms=ms - replay_age_ms, now_ms=ms)


REPLAY_PASS = {("sm-trenches", "sol"): {"verdict": "PASS", "mean": 0.04, "n": 60}}
REPLAY_FAIL = {("sm-trenches", "sol"): {"verdict": "FAIL", "mean": -0.04, "n": 60}}


def test_ready_needs_both_measurements_for_three_separate_days():
    state: dict = {}
    for i in range(1, 5):  # paper passes, replay fails: never ready
        state, r = _ready(state, "PASS", REPLAY_FAIL, i)
    assert r.ready == [] and r.streaks["sm-trenches/sol"] == 0
    for _ in range(3):  # both pass, but three runs on the SAME day count once
        state, r = _ready(state, "PASS", REPLAY_PASS, 5)
    assert r.streaks["sm-trenches/sol"] == 1 and r.ready == []
    state, r = _ready(state, "PASS", REPLAY_PASS, 6)
    assert r.ready == []
    state, r = _ready(state, "PASS", REPLAY_PASS, 7)
    assert r.ready == ["sm-trenches/sol"] and r.newly_ready == ["sm-trenches/sol"]
    state, r = _ready(state, "PASS", REPLAY_PASS, 8)
    assert r.ready == ["sm-trenches/sol"] and r.newly_ready == [], "announced once, not every day"
    state, r = _ready(state, "FAIL", REPLAY_PASS, 9)
    assert r.ready == [] and r.no_longer_ready == ["sm-trenches/sol"], "a lapse is said out loud"
    assert r.streaks["sm-trenches/sol"] == 0


def test_one_unchanged_pass_reused_across_gapped_days_is_never_ready():
    """Codex reproduction 2026-10-14: the same PASS on Oct 1, 5 and 14 used to reach READY."""
    state: dict = {}
    replay_ms = _day(0)[1]
    for i in (1, 5, 14):
        day, ms = _day(i)
        state, r = PR.update_readiness(state, [_score("PASS", n=50)], REPLAY_PASS, day,
                                       replay_ms=replay_ms, now_ms=ms)
    assert r.ready == [] and r.streaks["sm-trenches/sol"] == 0


def test_a_gap_restarts_the_streak():
    state: dict = {}
    state, r = _ready(state, "PASS", REPLAY_PASS, 1)
    state, r = _ready(state, "PASS", REPLAY_PASS, 2)
    state, r = _ready(state, "PASS", REPLAY_PASS, 4)  # day 3 had no report
    assert r.streaks["sm-trenches/sol"] == 1 and r.ready == []


def test_stale_replay_results_do_not_count():
    state: dict = {}
    for i in (1, 2, 3):
        state, r = _ready(state, "PASS", REPLAY_PASS, i, replay_age_ms=40 * 3_600_000)
    assert r.streaks["sm-trenches/sol"] == 0 and r.ready == []
    assert state["strategies"]["sm-trenches/sol"]["why"] == "replay results stale or missing"


def test_an_identical_paper_score_is_not_new_evidence():
    state: dict = {}
    for i in (1, 2, 3):
        state, r = _ready(state, "PASS", REPLAY_PASS, i, n=50)
    assert r.ready == []
    assert state["strategies"]["sm-trenches/sol"]["why"] == "no new evidence since the last counted day"


def test_the_whole_report_runs_and_fits_in_one_telegram_message(tmp_db):
    _strategy(tmp_db, "sm-trenches", "sol", 60, 0.08, 0.06)
    _strategy(tmp_db, "migration-fade", "sol", 60, -0.05, -0.02)
    _strategy(tmp_db, "sm-trenches", "robinhood", 10, 0.2, 0.2)
    for i in range(30):
        _strategy(tmp_db, f"lane{i}", "bsc", 3, 0.01, 0.01, tag=f"x{i}")
    _pos(tmp_db, "live1", "sm-trenches", "sol", opened_ago_d=6, closed_ago_d=None, mode="live")
    _replay(tmp_db, [{"lane": "sm-trenches", "chain": "sol", "verdict": "PASS", "mean": 0.03, "n": 80},
                     {"lane": "launch-snipe[buyers<50%]", "chain": "sol", "verdict": "PENDING", "mean": 0.1, "n": 20}])
    out = PR.run(tmp_db, NOW)
    text = out["text"]
    assert len(text) <= 4096
    assert "live buying OFF" in text and "Paper trading, last 24 h:" in text
    assert "PASS    sm-trenches/sol" in text and "replay PASS" in text
    assert "replay only launch-snipe[buyers<50%]/sol" in text
    assert "still open from before the pause" in text
    assert "Ready for live: none yet" in text, "one day of passing is not ready"
    stored = tmp_db.execute("SELECT value FROM kv WHERE key = ?", (PR.KV_LAST,)).fetchone()[0]
    assert stored == text


def test_a_stale_replay_is_said_in_the_message(tmp_db):
    _strategy(tmp_db, "sm-trenches", "sol", 10, 0.1, 0.1)
    _replay(tmp_db, [], age_h=50)
    assert "replay results are 50 h old" in PR.run(tmp_db, NOW)["text"]


def test_shipped_config_sends_one_daily_message():
    cfg = S.load_config(ROOT / "config" / "schedule.yaml")
    rep, spr = cfg.jobs["paper_report"], cfg.jobs["sprint_scorecard"]
    assert rep.enabled and rep.interval_s == 86_400 and "paper_report" in S.JOBS
    assert rep.params.get("notify", True) is True
    assert spr.enabled and spr.params.get("notify") is False, "one message a day, not two"


def test_the_sprint_scorecard_is_rolling_and_exports_results():
    now = NOW
    rows = [(int(now - (i + 0.5) * DAY / 5), 0.05) for i in range(60)]
    s = sprint.score("sm-trenches", "sol", rows, now - sprint.ROLLING_DAYS * DAY, now)
    (item,) = sprint.results([s])
    assert item["verdict"] == s.verdict and item["lane"] == "sm-trenches" and "mean" in item
    assert "DAY 7" not in sprint.render([s], now - sprint.ROLLING_DAYS * DAY, now)
    assert sprint.ROLLING_DAYS <= 12, "the replay needs 30 min before and 6 h after; swaps keep 14 d"


def test_twin_lines_show_paired_rows_and_say_when_there_are_none():
    assert PR.twin_lines(None, 0) == []
    empty = {"at_ms": 0, "window_days": 7, "rows": []}
    assert "no paired positions yet" in PR.twin_lines(empty, 0)[-1]
    row = {"lane": "sm-trenches", "chain": "sol", "challenger": "hold60s", "pairs": 45, "verdict": "FAIL",
           "challenger_mean": -0.02, "incumbent_model_mean": -0.04, "paired_diff_mean": 0.02, "paired_diff_lower90": -0.01}
    lines = PR.twin_lines({"at_ms": 0, "window_days": 7, "rows": [row]}, 0)
    assert "sm-trenches/sol hold60s: n=45 -2.0% vs -4.0% now (paired +2.0%, low90 -1.0%)" in lines[-1]


def test_positions_distorted_by_the_native_price_bug_are_left_out_and_counted(tmp_db):
    """2026-10-18: a paper exit that changed price basis before the fix, or a position open
    across the fix, booked a fiction; the scoreboard leaves it out and says how many."""
    fix = PR.PAPER_NATIVE_FIX_MS
    now = fix + 3 * PR.DAY_MS
    rows = [
        ("clean", fix - PR.DAY_MS, fix - PR.DAY_MS + 60_000, False),      # same basis, before: kept
        ("switched", fix - PR.DAY_MS, fix - PR.DAY_MS + 60_000, True),    # basis change, before: out
        ("straddle", fix - 60_000, fix + 60_000, False),                 # open across the fix: out
        ("after", fix + PR.DAY_MS, fix + PR.DAY_MS + 60_000, True),       # basis change after: kept
        ("curvebuy", fix - PR.DAY_MS, fix - PR.DAY_MS + 20_000, False),   # curve-priced entry before: out
    ]
    for pid, opened, closed, switched in rows:
        tmp_db.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, "
                       "cost_native, realized_native) VALUES (?,?,?,?,?,?,?,?,?)",
                       (pid, "sol", "T" + pid, "launch-snipe", "shadow", opened, closed, "100", "-58"))
        tmp_db.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
                       (pid, "o_" + pid, "sell", closed))
        detail = '{"basis_changed": {"from": "curve", "to": "dex"}}' if switched else "{}"
        tmp_db.execute("INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
                       ("o_" + pid, closed, "filled", detail))
        buy = '{"ok":true,"basis":"curve","side":"buy"}' if pid == "curvebuy" else '{"basis":"dex","side":"buy"}'
        tmp_db.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
                       (pid, "b_" + pid, "buy", opened))
        tmp_db.execute("INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
                       ("b_" + pid, opened, "filled", buy))
    bad = PR.native_bug_positions(tmp_db, now)
    assert bad == {"switched", "straddle", "curvebuy"}
    skipped: dict = {}
    kept = PR.paper_rows(tmp_db, now, skip=bad, skipped=skipped)
    assert len(kept) == 2 and skipped == {("launch-snipe", "sol"): 3}



def test_every_twin_section_carries_the_price_feed_caveat():
    """2026-10-18: the only twin PASS was a DexScreener-lag artefact (on chain the 60 s exit lost
    -16%/trade). Until paper is pool-priced, no twin verdict may be shown without the caveat."""
    twin = {"window_days": 7, "at_ms": 0, "rows": [{"pairs": 109, "verdict": "PASS", "lane": "migration-fade",
            "chain": "sol", "challenger": "hold60s", "challenger_mean": 0.096, "incumbent_model_mean": -0.008,
            "paired_diff_mean": 0.104, "paired_diff_lower90": 0.038}]}
    lines = PR.twin_lines(twin, 0)
    assert any(PR.PRICE_FEED_CAVEAT in line for line in lines)


def test_the_scoreboard_counts_only_paper_trades_since_paper_became_honest(tmp_db):
    """2026-10-19: sol paper is pool-priced and every chain pays live costs from PAPER_HONEST_MS;
    earlier trades are left out of the scoreboard and counted, never silently dropped."""
    now = 1_900_000_000_000
    cost = 1_000_000_000
    for pid, opened in (("old", PR.PAPER_HONEST_MS - 60_000), ("new", PR.PAPER_HONEST_MS + 60_000)):
        tmp_db.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
            "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "sol", "T" + pid, "migration-fade", "shadow", opened, now - 1_000, "0", "1", str(cost),
             str(cost), "0", "test"))
    tmp_db.commit()
    skipped: dict = {}
    rows = PR.paper_rows(tmp_db, now, days=400, skipped=skipped, trials={}, since_ms=PR.PAPER_HONEST_MS)
    assert len(rows) == 1 and skipped == {("__before_honest__", ""): 1}


def test_sol_trades_that_paid_the_pool_fee_twice_stay_on_the_scoreboard_and_are_named(tmp_db):
    """2026-10-21: sol paper pool entries opened before PAPER_COST_FIX_MS paid the PumpSwap fee twice
    (paper_costs.SUPERSEDED_ENTRY_LATENCY_BPS). Their prices are real, so they stay on the scoreboard;
    the report says how many there are and how much more each paid, until the window moves past."""
    import time

    now = PR.PAPER_COST_FIX_MS + 2 * DAY
    cost = 1_000_000_000
    for pid, chain, opened in (("before", "sol", PR.PAPER_COST_FIX_MS - 60_000),
                               ("after", "sol", PR.PAPER_COST_FIX_MS + 60_000),
                               ("rh", "robinhood", PR.PAPER_COST_FIX_MS - 60_000),
                               ("unscored", "sol", PR.PAPER_HONEST_MS - 60_000)):
        tmp_db.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
            "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, chain, "T" + pid, "migration-fade", "shadow", opened, now - 1_000, "0", "1", str(cost),
             str(cost), "0", "test"))
    tmp_db.commit()
    assert PR.sol_fee_counted_twice(tmp_db, now) == 1, "sol only, and only trades the scoreboard counts"
    rows = PR.paper_rows(tmp_db, now, skipped={}, trials={}, since_ms=PR.PAPER_HONEST_MS)
    assert sorted(chain for _lane, chain, *_ in rows) == ["robinhood", "sol", "sol"], "kept, not dropped"
    since = time.strftime("%m-%d %H:%M", time.gmtime(PR.PAPER_COST_FIX_MS / 1000))
    text = PR.run(tmp_db, now)["text"]
    assert (f"Note: 1 sol paper trades opened before {since} UTC paid 132 bps (~1.3 pp) more per "
            "pool-priced entry than later ones (the PumpSwap fee was counted twice)") in text
    # Opened before the fix, closed after it: still on the scoreboard 13 days on, so still named.
    straddle = PR.PAPER_COST_FIX_MS + (PR.WINDOW_DAYS + 1) * DAY
    assert PR.sol_fee_counted_twice(tmp_db, straddle) == 1, "the window is set by closed_ms"
    gone = now - 1_000 + PR.WINDOW_DAYS * DAY + 60_000
    assert PR.sol_fee_counted_twice(tmp_db, gone) == 0
    assert "counted twice" not in PR.render(now_ms=gone, acts=[], papers=[], replay={}, replay_ms=gone,
                                            ready=PR.Readiness(), no_depth=0, leftovers={})
    assert PR.PAPER_COST_FIX_MS > PR.PAPER_HONEST_MS


def test_curve_priced_and_written_off_sol_trades_are_not_named_as_double_charged(tmp_db):
    """Only POOL-priced entries paid the drift charge twice; curve buys never did, and fabricated
    write-offs are not on the scoreboard."""
    now = PR.PAPER_COST_FIX_MS + 2 * DAY
    for pid, reason in (("pool", "trailing_stop"), ("curve", "trailing_stop"), ("wo", "abandoned_unpriceable")):
        tmp_db.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
            "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, "sol", "T" + pid, "launch-snipe", "shadow", PR.PAPER_COST_FIX_MS - 60_000, now - 1_000,
             "0", "1", "1000000000", "1000000000", "0", reason))
    tmp_db.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES ('curve', 'ord_curve_buy', 'buy', ?)",
                   (PR.PAPER_COST_FIX_MS - 60_000,))
    tmp_db.execute("INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES ('ord_curve_buy', ?, 'filled', ?)",
                   (PR.PAPER_COST_FIX_MS - 60_000, 'paper buy {"basis":"curve","side":"buy"}'))
    tmp_db.commit()
    assert PR.sol_fee_counted_twice(tmp_db, now) == 1


def _tag(conn, pid: str, chain: str, *, entry: str, fill: str, transfer_tax: str | None = "pons_launch") -> None:
    """A tagged row. ``transfer_tax`` defaults to a Pons launch, whose tax the quoter already
    carries: a robinhood POOL entry counts only with its transfer tax accounted for
    (``paper_costs.TRANSFER_TAX_ACCOUNTED``; bsc and curve entries ignore it)."""
    from kaiba.core.schemas import Chain
    from kaiba.execution import paper_costs as pc

    pc.record_entry_basis(conn, pid, Chain(chain), source=entry, fill=None, buy_tax_bps=0, sell_tax_bps=0,
                          tax_in_price=False, transfer_tax=transfer_tax, landing=LANDED)
    pc.record_exit_basis(conn, pid, Chain(chain), mark_source="prices:dexscreener", fill=fill, tax_bps=0,
                         filled=True)


def test_robinhood_and_bsc_rows_count_only_when_their_own_tag_says_pool_priced(tmp_db):
    """2026-10-21 (design step 3): PAPER_HONEST_MS is a SOL cut and certified robinhood/bsc rows by
    date alone, while 89/89 robinhood and 45/45 bsc entries after it were dossier-priced. A row on
    those chains counts only when its ENTRY came from the pool or curve; untagged rows (opened before
    the tag existed) do not count either, and every left-out row is counted. 2026-10-23 (step 3
    review, blocking #5): an honest entry whose exit was filled at the mark now STAYS, flagged."""
    for chain in ("robinhood", "bsc"):
        for i in range(6):
            pid = f"{chain}{i}"
            _pos(tmp_db, pid, "sm-trenches", chain, opened_ago_d=2, closed_ago_d=1, ret=0.5)
            if i < 2:
                _tag(tmp_db, pid, chain, entry="pool:uniswap-v4:v4_quoter", fill="v4_quoter")
            elif i < 4:
                _tag(tmp_db, pid, chain, entry="dossier", fill="v4_quoter")
            elif i == 4:
                _tag(tmp_db, pid, chain, entry="pool:pancake-v2:v2_formula", fill="mark")
    _strategy(tmp_db, "sm-trenches", "sol", 5, 0.1, 0.1)  # sol is not tagged and is not touched
    left_out, counts = PR.not_pool_priced(tmp_db, NOW)
    assert counts == {"robinhood": 3, "bsc": 3} and len(left_out) == 6
    assert PR.exit_not_pool_filled(tmp_db, NOW) == {"robinhood4", "bsc4"}
    out = PR.run(tmp_db, NOW)
    papers = PR.paper_scores(PR.paper_rows(tmp_db, NOW, trials={}, not_honest=left_out), NOW)
    n = {(p.score.lane, p.score.chain): p.score.n for p in papers}
    assert n[("sm-trenches", "robinhood")] == 3 and n[("sm-trenches", "bsc")] == 3
    assert n[("sm-trenches", "sol")] == 5
    # the daily message's own scoreboard, not only the helper
    assert "sm-trenches/robinhood: paper n=3 " in out["text"] and "sm-trenches/bsc: paper n=3 " in out["text"]
    assert "entry stage: 6 paper trades off sol left out of the scoreboard" in out["text"]
    assert "bsc 3, robinhood 3" in out["text"]
    assert "exit stage: 2 paper trades off sol" in out["text"]


def test_eth_and_base_rows_never_count_by_date_alone(tmp_db):
    """2026-10-23 (default-deny): only sol is honest by date. The manual lane's eth/base rows are
    never tagged and never pool-priced; they used to count because the filter listed only
    robinhood and bsc."""
    for chain in ("eth", "base"):
        for i in range(3):
            _pos(tmp_db, f"{chain}{i}", "manual", chain, opened_ago_d=2, closed_ago_d=1, ret=0.5)
    _strategy(tmp_db, "sm-trenches", "sol", 5, 0.1, 0.1)
    left_out, counts = PR.not_pool_priced(tmp_db, NOW)
    assert counts == {"eth": 3, "base": 3} and len(left_out) == 6
    board = PR.scoreboard(tmp_db, NOW, trials={})
    assert {(p.score.lane, p.score.chain) for p in board.papers} == {("sm-trenches", "sol")}
    assert board.filter.reason("eth0", "eth", NOW - DAY) == PR.NOT_POOL_PRICED
    assert board.filter.reason("never-seen", "base", NOW - DAY) == PR.NOT_POOL_PRICED, "default-deny"
    assert board.filter.reason("never-seen", "sol", NOW - DAY) is None
    out = PR.run(tmp_db, NOW)
    assert "manual/eth" not in out["text"] and "manual/base" not in out["text"]
    assert "base 3, eth 3" in out["text"]


def _exit_stage_strategy(conn, lane: str, chain: str, *, honest: int, honest_ret: float, stage: int,
                         stage_ret: float, stage_fill: str = "pool_no_fill_bound") -> None:
    """``honest`` pool-in/pool-out rows plus ``stage`` pool-in rows whose exit the pool did not fill,
    spread evenly over the window (both halves populated)."""
    total = honest + stage
    stage_at = {int((j + 0.5) * total / stage) for j in range(stage)}
    assert len(stage_at) == stage
    for i in range(total):
        age = PR.WINDOW_DAYS * (1 - (i + 0.5) / total)
        is_stage = i in stage_at
        pid = f"{lane}{chain}{i}"
        _pos(conn, pid, lane, chain, opened_ago_d=age + 0.01, closed_ago_d=age,
             ret=stage_ret if is_stage else honest_ret)
        _tag(conn, pid, chain, entry="pool:pancake-v2:v2_formula", fill=stage_fill if is_stage else "v2_formula")


def test_exit_stage_losers_stay_on_the_scoreboard_instead_of_carrying_a_pass(tmp_db):
    """The review's probe: 40 honest +8% trades and 12 pool-priced entries whose exit the pool could
    not fill, booked at -90%. Leaving the 12 out printed PASS +8.0% (n=40); all 52 are -14.6%."""
    _exit_stage_strategy(tmp_db, "sm-trenches", "bsc", honest=40, honest_ret=0.08, stage=12, stage_ret=-0.9)
    assert len(PR.exit_not_pool_filled(tmp_db, NOW)) == 12
    out = PR.run(tmp_db, NOW)
    line = next(ln for ln in out["text"].splitlines() if "sm-trenches/bsc: paper" in ln)
    assert "paper n=52 -14.6%" in line and line.strip().startswith("FAIL"), line
    assert "12 exits not pool-filled (mean -90.0%)" in line, line
    assert "exit stage: 12 paper trades off sol" in out["text"]
    assert "sm-trenches/bsc" not in out["ready"]


@pytest.mark.parametrize("stage,verdict", [(4, "PASS"), (7, "HOLD")])
def test_a_pass_that_leans_on_exits_the_pool_did_not_fill_is_held(tmp_db, stage, verdict):
    """Positive either way, so sprint.score says PASS; above EXIT_STAGE_MAX_SHARE (10%) of the rows
    it is withheld as HOLD, whatever those rows were booked at, and cannot start a readiness streak."""
    _exit_stage_strategy(tmp_db, "sm-trenches", "robinhood", honest=50, honest_ret=0.1, stage=stage, stage_ret=0.1,
                         stage_fill="mark")
    out = PR.run(tmp_db, NOW)
    line = next(ln for ln in out["text"].splitlines() if "sm-trenches/robinhood: paper" in ln)
    assert line.strip().startswith(verdict), line
    if verdict == "HOLD":
        assert f"exit stage: {stage} of {50 + stage} exits not filled from the pool" in line, line
        state = json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?", (PR.KV_STATE,)).fetchone()[0])
        assert state["strategies"]["sm-trenches/robinhood"]["streak"] == 0
