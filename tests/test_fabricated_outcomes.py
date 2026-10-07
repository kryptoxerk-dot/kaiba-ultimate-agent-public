"""A paper write-off's booked -100% is not an outcome, and every reader says what it left out.

MEASURED 2026-10-04 on the box (read-only): 73 shadow positions closed ``abandoned_unpriceable``
by ``watchdog._abandon_unpriceable_shadow`` -> ``accounting.write_off_dust``; 72 read exactly
-100% with nothing sold, 69 had an exit DECIDED on a usable price first (median -5.5% from
entry), and every one of 709 exit attempts failed "paper exit not modelled: liquidity
unavailable". 0 of 73 have a ``trades`` row. Lead's 10-03 cut of migration-fade sol shadow
(212 closed): -43.2% per trade with them, -15.9% without.

Each test below fails with its module's exclusion removed (mutation-checked). The live write-
offs stay in on purpose: that cost was real money.
"""

from __future__ import annotations

import sqlite3

import pytest

from kaiba.core.schemas import now_ms
from kaiba.learning import fabricated

SHADOW_FAB = "abandoned_unpriceable"
LIVE_FAB = "abandoned_unpriceable:operator_2026-09-24"


def _position(conn, pid, *, mode="shadow", reason=SHADOW_FAB, lane="migration-fade", chain="sol",
              cost=100, proceeds=0, opened_ms=None, closed_ms=None, entry=None):
    opened = opened_ms if opened_ms is not None else now_ms() - 10_000
    closed = closed_ms if closed_ms is not None else opened + 1_000
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, "
        "qty_total, cost_native, proceeds_native, realized_native, entry_price_usd, exit_reason) "
        "VALUES (?,?,?,?,?,?,?,'0','100',?,?,?,?,?)",
        (pid, chain, f"tok_{pid}", lane, mode, opened, closed, str(cost), str(proceeds),
         str(proceeds - cost), entry, reason),
    )


# ---------------------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    ("reason", "mode", "expected"),
    [
        (SHADOW_FAB, "shadow", True),
        ("abandoned_unpriceable:deferred", "shadow", True),
        (LIVE_FAB, "live", False),          # real money: the loss the wallet took
        (SHADOW_FAB, "canary", False),
        ("write_off:no_swap_route", "live", False),
        ("dust_written_off", "shadow", False),
        ("stop_loss", "shadow", False),
        ("abandoned_unpriceablex", "shadow", False),
        (None, "shadow", False),
    ],
)
def test_the_python_and_sql_rules_agree(reason, mode, expected):
    assert fabricated.is_fabricated_outcome(reason, mode) is expected
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE positions (mode TEXT, exit_reason TEXT)")
    c.execute("INSERT INTO positions VALUES (?,?)", (mode, reason))
    got = c.execute(f"SELECT COALESCE({fabricated.sql_predicate()}, 0) FROM positions").fetchone()[0]
    assert bool(got) is expected


def test_a_write_off_never_reaches_the_trades_table(tmp_db):
    """Why every trades-based reader (metrics, reflect, experiment_loop, hold_study) is clean:
    ``write_off_dust`` books the position and writes no trade. If that ever changes, each of
    them would average a -100% that nothing measured, so this pins it."""
    from kaiba.execution import accounting

    _position(tmp_db, "pos_fab", reason=None)
    tmp_db.execute("UPDATE positions SET closed_ms=NULL, qty='100'")
    accounting.write_off_dust("pos_fab", tmp_db, reason=SHADOW_FAB)
    row = tmp_db.execute("SELECT exit_reason, closed_ms FROM positions").fetchone()
    assert row["exit_reason"] == SHADOW_FAB and row["closed_ms"] is not None
    assert tmp_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


# ---------------------------------------------------------------------------- loss_review


def test_loss_review_counts_a_paper_write_off_instead_of_reviewing_it(tmp_db):
    from kaiba.learning.loss_review import build_reviews

    _position(tmp_db, "paper_fab")
    _position(tmp_db, "paper_stop", reason="stop_loss", proceeds=60)
    _position(tmp_db, "live_fab", mode="live", reason=LIVE_FAB, lane="sm-trenches", chain="bsc")
    report = build_reviews(tmp_db, cutoff_ms=now_ms())
    assert {r["position_id"] for r in report["reviews"]} == {"paper_stop", "live_fab"}
    assert report["counts"] == {"shadow": 1, "live": 1}
    assert report["fabricated_outcomes_excluded"] == {"shadow": 1}


# ---------------------------------------------------------------------------- MCP read-outs


def test_performance_counts_write_offs_apart_from_missing_outcomes(tmp_db, monkeypatch):
    from kaiba.mcp import server

    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    _position(tmp_db, "paper_fab")
    _position(tmp_db, "live_missing", mode="live", reason="stop_loss", lane="sm-trenches")
    got = server.kaiba_performance()["coverage"]
    assert got["missing_closed_outcomes"] == [{"chain": "sol", "n": 1}]
    assert got["fabricated_outcomes_excluded"] == [
        {"lane": "migration-fade", "mode": "shadow", "chain": "sol", "n": 1}
    ]


def test_performance_leaves_a_fabricated_trade_row_out_of_the_mean(tmp_db, monkeypatch):
    from kaiba.mcp import server

    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    stamp = now_ms() - 1_000
    for tid, pnl, reason in (("ok", 20, "trailing_stop"), ("fab", -100, SHADOW_FAB)):
        tmp_db.execute(
            "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, "
            "hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"p_{tid}", "migration-fade", "shadow", "sol", f"t_{tid}", stamp - 1_000, stamp, 1,
             "100", str(100 + pnl), str(pnl), float(pnl), reason),
        )
    got = server.kaiba_performance(mode="shadow")
    lane = got["by_lane"][0]
    assert lane["n"] == 1 and lane["avg_pct"] == pytest.approx(20.0)
    assert got["coverage"]["fabricated_trade_rows_excluded"] == 1


def test_closed_positions_are_tagged_when_their_result_was_booked(tmp_db, monkeypatch):
    from kaiba.mcp import server

    monkeypatch.setattr(server, "_conn", lambda: tmp_db)
    _position(tmp_db, "paper_fab")
    _position(tmp_db, "live_fab", mode="live", reason=LIVE_FAB)
    rows = {r["position_id"]: r for r in server.kaiba_positions(include_closed=True)["positions"]}
    assert rows["paper_fab"]["fabricated_outcome"] is True
    assert rows["live_fab"]["fabricated_outcome"] is False


# ---------------------------------------------------------------------------- exit_study


@pytest.fixture
def study_conn():
    from tests.test_exit_study import SCHEMA

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def test_exit_study_compares_shadow_record_and_tape_over_the_same_positions(study_conn):
    from kaiba.learning import exit_study as es
    from tests.test_exit_study import T0, add_position, add_prints

    add_position(study_conn, "keep", token="KEEP", mode="shadow", opened_ms=T0)
    add_prints(study_conn, "KEEP", T0, [(0, 0.001), (10, 0.0009), (20, 0.0006)])
    study_conn.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, "
        "hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) "
        "VALUES ('t_keep','keep','sm-trenches','shadow','sol','KEEP',?,?,60,'100','65','-35',-35.0,'stop_loss')",
        (T0, T0 + 60_000),
    )
    # Written off: on the tape (so it is a replayable path), never in trades.
    add_position(study_conn, "fab", token="FAB", mode="shadow", opened_ms=T0 + 600_000, proceeds=0)
    study_conn.execute("UPDATE positions SET exit_reason=? WHERE position_id='fab'", (SHADOW_FAB,))
    add_prints(study_conn, "FAB", T0 + 600_000, [(0, 0.001), (10, 0.0012), (20, 0.0015)])

    paths, _ = es.load_paths(study_conn)
    keep = next(p for p in paths if p.position_id == "keep")
    expected = -35.0 - es.fixed_stop(keep, es.DECLARED_STOP_PCT).net_return_pct
    caveats = es.study(study_conn).caveats
    assert any(f"{expected:+.1f}pp away" in c and "over the 1 shadow positions" in c for c in caveats), caveats
    assert any(c.startswith("1 replayable shadow positions have no usable recorded outcome")
               and "1 of them closed with a booked" in c for c in caveats), caveats


# ---------------------------------------------------------------------------- gates


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    from tests.test_experiment_loop import write_risk_file

    return write_risk_file(tmp_path / "risk.yaml", monkeypatch)


def test_shadow_gate_drops_a_fabricated_arm_trade_and_reports_the_lanes_write_offs(tmp_db, risk_file):
    from tests.test_experiment_loop import LANE
    from tests.test_relative_gates import shadow_arms, shadow_judge

    shadow_arms(tmp_db, "exp_sh", cand=lambda k: -30 if k % 2 else -70, refused=lambda k: -300)
    first, last = tmp_db.execute(
        "SELECT MIN(t.closed_ms), MAX(t.closed_ms) FROM trades t JOIN experiment_trades x "
        "ON x.trade_id=t.trade_id WHERE x.arm='candidate'"
    ).fetchone()
    mid = (first + last) // 2
    # A written-off paper position labelled into the candidate arm, -100% on cost 1000.
    _position(tmp_db, "p_fab", lane=LANE, chain="robinhood", cost=1000, opened_ms=mid - 60_000, closed_ms=mid)
    tmp_db.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, hold_s, "
        "cost_native, proceeds_native, pnl_native, pnl_pct, exit_reason) "
        "VALUES ('fab','p_fab',?,'shadow','robinhood','tok_p_fab',?,?,60,'1000','0','-1000',-100.0,?)",
        (LANE, mid - 60_000, mid, SHADOW_FAB),
    )
    tmp_db.execute("INSERT INTO experiment_trades (experiment_id, trade_id, arm, linked_ms) "
                   "VALUES ('exp_sh','fab','candidate',?)", (mid,))
    # Real money, same lane and span: not fabricated, not counted.
    _position(tmp_db, "p_live", mode="live", reason=LIVE_FAB, lane=LANE, chain="robinhood",
              opened_ms=mid - 60_000, closed_ms=mid)

    result = shadow_judge(tmp_db)
    assert result.metrics["candidate_trades"] == 60
    assert result.metrics["fabricated_outcomes_excluded"] == 1
    assert result.metrics["improvement"]["delta"] == pytest.approx(0.0833, abs=0.002)
    assert result.passed is True, result.reasons


def test_replay_gate_drops_a_fabricated_trade_and_reports_the_lanes_write_offs(tmp_db, risk_file):
    from tests.test_experiment_loop import BASE_MS, LANE, add_entry
    from tests.test_relative_gates import SPACING, judge, losing_lane_better_subset, stream

    stream(tmp_db, losing_lane_better_subset)
    ts = BASE_MS + 5 * SPACING + 1
    add_entry(tmp_db, "fab", ts=ts, smart=5, pnl=-1000, mode="shadow")
    tmp_db.execute("UPDATE trades SET exit_reason=? WHERE trade_id='trd_fab'", (SHADOW_FAB,))
    _position(tmp_db, "pos_fab", lane=LANE, chain="robinhood", cost=1000, opened_ms=ts, closed_ms=ts + 60_000)

    result = judge(tmp_db)
    assert result.metrics["decisions_replayed"] == 240
    assert result.metrics["fabricated_outcomes_excluded"] == 1


# ---------------------------------------------------------------------------- confluence_size


def test_confluence_size_names_write_offs_among_what_it_skipped(tmp_db):
    from kaiba.learning import confluence_size as CS
    from tests.test_confluence_size import T0, _trade

    _trade(tmp_db, ret=0.1)
    _trade(tmp_db, ret=-1.0)
    fab_trade = tmp_db.execute("SELECT trade_id FROM trades ORDER BY opened_ms DESC LIMIT 1").fetchone()[0]
    tmp_db.execute("UPDATE trades SET exit_reason=? WHERE trade_id=?", (SHADOW_FAB, fab_trade))
    # Written off with no trade row at all: the loop over trades never sees it.
    _position(tmp_db, "pos_unbooked", lane="confluence-5", chain="robinhood",
              opened_ms=T0 + 3_600_000, closed_ms=T0 + 7_200_000)
    trades, skipped = CS.collect_paper_trades(tmp_db)
    assert len(trades) == 1 and float(trades[0].ret) == pytest.approx(0.1)
    assert skipped == {"fabricated_outcome": 2}
