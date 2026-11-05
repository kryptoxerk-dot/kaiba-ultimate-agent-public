import sqlite3

import pytest

from kaiba.learning.finite_paper_capital import BANKROLL, RESERVED, read_arm, replay


def plan(ident, start, end=None, net=None, status=None, observation="open"):
    return {"id": ident, "planned_ms": start, "at_ms": end, "net_sol": net,
            "arm_status": status, "observation_status": observation, "tx_sol": 0.0003, "rent_sol": 0,
            "exit_reason": None, "fee_pct": None}


def test_overlapping_plans_cannot_spend_unsettled_proceeds_or_use_own_outcome():
    rows = [plan(1, 10, 20, .15, "closed"), plan(2, 11, 25, -.1, "closed"),
            plan(3, 20, 22, 1, "closed"), plan(4, 21, 27, -.0003, "missed")]
    out = replay(rows, as_of_ms=30)
    assert out["admitted"] == 3 and out["cash_skipped"] == 1
    assert out["peak_concurrent_reservations"] == 2
    assert out["admitted_missed"] == 1 and out["settled_pnl_lamports"] == 49_700_000
    assert out["unconstrained_pnl_lamports"] == 1_049_700_000
    assert out["free_cash_lamports"] == BANKROLL + 49_700_000


def test_unpriced_and_abandoned_plans_hold_reserve_until_evidence_exists():
    out = replay([plan(1, 10, 12, status="stale"), plan(2, 13, observation="abandoned"),
                  plan(3, 16, 20, .1, "closed")], as_of_ms=25)
    assert out["unpriced_reserved"] == 2 and out["still_reserved_lamports"] == 2 * RESERVED
    assert out["free_cash_lamports"] == BANKROLL - 2 * RESERVED and out["cash_skipped"] == 1
    assert out["settled_pnl_lamports"] is None
    assert not out["financial_execution"] and not out["promotion_authority"]


def test_same_profile_database_does_not_supply_another_arms_or_future_close(tmp_path):
    path = tmp_path / "lp_paper.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE observations (id INTEGER, planned_ms INTEGER, status TEXT, version TEXT);
            CREATE INDEX idx_obs_time ON observations(planned_ms);
            CREATE TABLE arm_results (obs_id INTEGER, arm TEXT, status TEXT, at_ms INTEGER, net_sol REAL,
                                      tx_sol REAL, rent_sol REAL, exit_reason TEXT, fee_pct REAL);
            INSERT INTO observations VALUES (1, 10, 'open', 'lp-paper/1'), (2, 11, 'open', 'lp-paper/1');
            INSERT INTO arm_results VALUES (1, 'r20_60', 'closed', 15, 0.25, 0, 0, NULL, NULL),
                                           (2, 'f40_6h', 'closed', 50, 2, 0, 0, NULL, NULL);
        """)
    out = replay(read_arm(path, "f40_6h", since_ms=1, as_of_ms=20), as_of_ms=20)
    assert out["settled"] == 0 and out["pending_reserved"] == 2
    with pytest.raises(ValueError, match="cashflow_exceeds_declared_reserve"):
        replay([plan(1, 10, 15, -1.41, "closed")], as_of_ms=20)
    with pytest.raises(ValueError, match="paper_costs_exceed_reserve"):
        replay([plan(1, 10, 15, .10, "closed") | {"rent_sol": .41}], as_of_ms=20)
