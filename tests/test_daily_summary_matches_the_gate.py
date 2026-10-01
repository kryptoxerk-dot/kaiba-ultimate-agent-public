"""What the summary reports and what the gate enforces must be one number.

MEASURED 2026-09-23 on the live box, at the same instant:

    daily_summary()["chains"]["sol"]  -> realized -0.213516, stopped False, left 0.2365
    RiskGate.realized_today(SOL)      -> -0.554208, against a 0.450000 stop

Forty-one solana entries were refused with ``daily_loss_stop`` in the hour around that
reading, while every operator-facing view of the day said there was headroom. bsc was the
same shape: -0.010502 and "not stopped" reported, -0.104802 past a 0.10 stop enforced.

The cause is one line of provenance. ``realized_today`` was rewritten on 2026-09-21 to sum
closed positions, because ``risk_state.realized_native_json`` is written by ``record_fill``
and ``record_fill`` has no production callers -- it read zero forever. ``daily_summary``
was left reading the abandoned ledger, so the number that decides nothing was the only one
anybody could see.

This is worse than a stale display. An operator -- or an agent reading this summary to
decide whether to intervene -- is told the brake is off while the brake is on, and the
only way to find out otherwise is to trace a refusal backwards through the gate.
"""

from __future__ import annotations

import json

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution.risk import RiskGate


def close_position(conn, chain: Chain, realized: int, *, mode: str = "live", pid: str = "p1"):
    """One closed round trip on the positions table -- what `realized_today` reads."""
    import time

    conn.execute(
        "INSERT OR REPLACE INTO positions (position_id, chain, token, lane, mode, opened_ms, "
        " closed_ms, qty, qty_total, cost_native, proceeds_native, realized_native) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, chain.value, "tok", "sm-trenches", mode, 1, int(time.time() * 1000),
         0, 0, 0, 0, realized),
    )
    conn.commit()


def store_ledger(gate, conn, mapping: dict[str, int]) -> None:
    """Write the abandoned `record_fill` ledger, which used to drive the summary.

    Through the gate's own writer so the ``day_key`` matches whatever production would
    use. A hand-made key leaves `_state_row` reading a different row and the test passes
    without ever exercising the divergence -- and `_state_row` does not insert, it returns
    a default when the row is absent, so an UPDATE alone silently touches nothing.
    """
    row = gate._state_row(conn)
    gate._write_state({**row, "realized_native_json": json.dumps(mapping)}, conn)
    conn.commit()


@pytest.fixture
def gate():
    return RiskGate()


def test_the_summary_reports_what_the_gate_enforces(tmp_db, gate):
    """THE REGRESSION: the two numbers came from different places and disagreed."""
    close_position(tmp_db, Chain.SOL, -500_000_000)
    store_ledger(gate, tmp_db, {"sol": -1_000})  # the stale ledger says almost nothing was lost

    summary = gate.daily_summary(tmp_db)["chains"]["sol"]
    assert summary["realized_native"] == gate.realized_today(Chain.SOL, tmp_db)
    assert summary["realized_native"] == -500_000_000


def test_a_chain_past_its_stop_is_reported_as_stopped(tmp_db, gate):
    """The half that cost the most: 41 refusals while the summary said there was room."""
    stop = gate.daily_summary(tmp_db)["chains"]["sol"]["daily_loss_stop_base_units"]
    assert stop, "this chain has no configured stop; the test proves nothing"
    close_position(tmp_db, Chain.SOL, -(int(stop) + 1))
    store_ledger(gate, tmp_db, {"sol": 0})

    summary = gate.daily_summary(tmp_db)["chains"]["sol"]
    assert summary["stopped"] is True, "the summary said the brake was off while it was on"
    assert summary["loss_budget_left"] == 0


def test_the_stale_ledger_is_still_reported_under_its_own_name(tmp_db, gate):
    """Kept visible on purpose: a future divergence should be readable here, not traced."""
    close_position(tmp_db, Chain.SOL, -500_000_000)
    store_ledger(gate, tmp_db, {"sol": -1_000})
    summary = gate.daily_summary(tmp_db)["chains"]["sol"]
    assert summary["realized_native_stored"] == -1_000
    assert summary["realized_native"] == -500_000_000


def test_a_shadow_loss_never_reaches_the_summary(tmp_db, gate):
    """Paper money must not report a live drawdown, exactly as it cannot cause one."""
    close_position(tmp_db, Chain.SOL, -500_000_000, mode="shadow", pid="paper")
    assert gate.daily_summary(tmp_db)["chains"]["sol"]["realized_native"] == 0


def test_every_chain_agrees_not_just_solana(tmp_db, gate):
    for i, chain in enumerate((Chain.SOL, Chain.BSC, Chain.ROBINHOOD)):
        close_position(tmp_db, chain, -(i + 1) * 1_000, pid=f"p{i}")
    store_ledger(gate, tmp_db, {"sol": 7, "bsc": 7, "robinhood": 7})
    summary = gate.daily_summary(tmp_db)["chains"]
    for chain in (Chain.SOL, Chain.BSC, Chain.ROBINHOOD):
        if chain.value not in summary:
            continue
        assert summary[chain.value]["realized_native"] == gate.realized_today(chain, tmp_db)
