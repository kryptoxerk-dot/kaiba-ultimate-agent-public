"""After a losing day, trade smaller until the loss is made back.

OWNER 2026-09-23: *"then if got stoploss next day downsize until recoup"*.

The rule is deliberately day-boundaried, which is what the owner asked for and also what
makes it honest: it reads days that are CLOSED. An intraday drawdown does not shrink the
size of the next trade in the same session, so a run of stops cannot spiral the book down
to nothing inside an hour, and the reduction cannot be triggered by a position that is
merely open and marked down.

WHAT "RECOUP" MEANS HERE. The reference is a HIGH-WATER MARK of cumulative realised P&L,
not the last day's number. If the book made +5 then lost -3, it is 3 below its best and
trades reduced until it is back to +5 -- not restored to full size merely because the next
day happened to be positive. That is what "until recoup" says.

DRAWDOWN IS MEASURED AGAINST BANKROLL, not against the high-water mark, because a 1-unit
drawdown means something different on a 4.5 SOL book than on a 0.39 ETH one, and the
multiplier has to mean the same thing on every chain.

UNKNOWN IS NOT A DRAWDOWN. If the history cannot be read, the size is NOT reduced. A
reduction is a claim that we lost money; inventing one from an unreadable table would be
the same fault as inventing a measurement (CONTRACT rule 2), and it would silently shrink
every entry on a fresh database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import risk as R
from kaiba.execution.risk import RiskGate

# Reuse test_risk.py's envelope fixture rather than a second copy of the same YAML; the
# two would drift and this file would stop testing the shipped shape. Same import shape
# as tests/test_concentration_sizing.py.
from tests.test_risk import (  # noqa: F401 - fixtures are used by name
    LANE,
    write_risk,
)

# ------------------------------------------------------------------ the ladder


def test_no_drawdown_is_full_size():
    mult, label = R.recovery_multiplier(0, 4_500_000_000)
    assert mult == Decimal(1)
    assert "none" in label or "0" in label


def test_a_profit_is_not_a_drawdown():
    """A negative drawdown is a new high-water mark, never a bonus multiplier."""
    mult, _ = R.recovery_multiplier(-1_000_000_000, 4_500_000_000)
    assert mult == Decimal(1), "a profitable book was scaled by something other than 1.0"


@pytest.mark.parametrize("dd_pct,expected_at_most", [(1, 1.0), (7, 0.9), (15, 0.7), (40, 0.5)])
def test_a_deeper_drawdown_never_sizes_larger(dd_pct, expected_at_most):
    bankroll = 1_000_000
    mult, _ = R.recovery_multiplier(int(bankroll * dd_pct / 100), bankroll)
    assert mult <= Decimal(str(expected_at_most)) + Decimal("0.0001")


def test_the_ladder_is_monotonic_and_bounded():
    bankroll = 1_000_000
    prev = Decimal(2)
    for pct in range(0, 101, 2):
        mult, _ = R.recovery_multiplier(int(bankroll * pct / 100), bankroll)
        assert Decimal(0) < mult <= Decimal(1), f"{pct}% drawdown gave {mult}"
        assert mult <= prev, f"drawdown {pct}% sized LARGER than the step before it"
        prev = mult


def test_it_never_reaches_zero():
    """A zero multiplier is a halt wearing a sizer's clothes. Halting is `risk_state`'s
    job and it says so out loud; this only ever trades smaller."""
    mult, _ = R.recovery_multiplier(10_000_000, 1_000)
    assert mult > 0


def test_an_unknown_bankroll_does_not_shrink_anything():
    for bankroll in (0, -1):
        mult, _ = R.recovery_multiplier(500, bankroll)
        assert mult == Decimal(1)


def test_the_label_names_the_drawdown():
    _mult, label = R.recovery_multiplier(150_000, 1_000_000)
    assert "15" in label, label


# ------------------------------------------------------------------ prior-day reading


def write_day(conn, day, per_chain):
    import json
    conn.execute(
        "INSERT OR REPLACE INTO risk_state (day_key, realized_native_json, entries, halted, "
        "updated_ms) VALUES (?,?,?,?,?)", (day, json.dumps(per_chain), 0, 0, 1))
    conn.commit()


def test_no_history_is_no_drawdown(tmp_db):
    dd, high = R.prior_day_drawdown(Chain.SOL, tmp_db)
    assert dd == 0 and high == 0


def test_a_losing_day_shows_as_drawdown_the_next_day(tmp_db):
    """THE OWNER'S RULE: a stop-out today means a smaller size tomorrow."""
    write_day(tmp_db, "2026-09-20", {"sol": 1_000_000})
    write_day(tmp_db, "2026-09-21", {"sol": -400_000})
    dd, high = R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-22")
    assert high == 1_000_000
    assert dd == 400_000


def test_recovery_restores_full_size_only_at_the_high_water_mark(tmp_db):
    """'Until recoup' means back to the BEST, not merely back to a green day."""
    write_day(tmp_db, "2026-09-20", {"sol": 1_000_000})
    write_day(tmp_db, "2026-09-21", {"sol": -400_000})
    write_day(tmp_db, "2026-09-22", {"sol": 100_000})      # green, but still below +1.0m
    dd, _ = R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-23")
    assert dd == 300_000, "a green day wrongly cleared the drawdown"

    write_day(tmp_db, "2026-09-23", {"sol": 300_000})      # now level with the high-water
    dd2, _ = R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-24")
    assert dd2 == 0, "the book recouped and is still being downsized"


def test_today_is_excluded(tmp_db):
    """Day-boundaried on purpose: an open, marked-down day must not shrink its own size."""
    write_day(tmp_db, "2026-09-22", {"sol": -900_000})
    dd, _ = R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-22")
    assert dd == 0


def test_chains_are_independent(tmp_db):
    write_day(tmp_db, "2026-09-21", {"sol": -500_000, "bsc": 200_000})
    assert R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-22")[0] == 500_000
    assert R.prior_day_drawdown(Chain.BSC, tmp_db, today="2026-09-22")[0] == 0


def test_unreadable_history_is_not_a_drawdown(tmp_db):
    tmp_db.execute(
        "INSERT OR REPLACE INTO risk_state (day_key, realized_native_json, entries, halted, "
        "updated_ms) VALUES (?,?,?,?,?)", ("2026-09-21", "{not json", 0, 0, 1))
    tmp_db.commit()
    dd, _ = R.prior_day_drawdown(Chain.SOL, tmp_db, today="2026-09-22")
    assert dd == 0, "an unparseable row invented a drawdown"


# ------------------------------------------------------------------ the sizer applies it
#
# A source grep is not a behaviour test: with `if False:` wrapped around the reduction the
# grep still passed and a mutation that removed the whole mechanism survived. These drive
# the real sizer.



def test_a_prior_losing_day_actually_reduces_the_next_size(write_risk, tmp_db):  # noqa: F811 - the imported fixture, requested by name
    """THE OWNER'S RULE, end to end: stopped out yesterday, smaller today."""
    write_risk()
    full = RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db)
    assert full > 0, "the fixture does not size at all; the test below would prove nothing"

    # 15% of a 10 SOL bankroll lost yesterday -> the 0.60 rung.
    write_day(tmp_db, "2026-01-01", {"sol": 1_000_000_000})
    write_day(tmp_db, "2026-01-02", {"sol": -1_500_000_000})
    reduced = RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db)

    assert reduced < full, f"a 15% drawdown did not reduce the size ({reduced} vs {full})"
    assert reduced == int(full * Decimal("0.60")) or reduced == int(Decimal(full) * Decimal("0.60"))


def test_a_book_at_its_high_water_mark_sizes_full(write_risk, tmp_db):  # noqa: F811 - the imported fixture, requested by name
    write_risk()
    full = RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db)
    write_day(tmp_db, "2026-01-01", {"sol": 1_000_000_000})
    assert RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db) == full


def test_recouping_restores_the_full_size(write_risk, tmp_db):  # noqa: F811 - the imported fixture, requested by name
    """The whole point of 'until recoup': the reduction lifts when the loss is made back."""
    write_risk()
    full = RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db)
    write_day(tmp_db, "2026-01-01", {"sol": 1_000_000_000})
    write_day(tmp_db, "2026-01-02", {"sol": -1_500_000_000})
    assert RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db) < full
    write_day(tmp_db, "2026-01-03", {"sol": 1_500_000_000})   # back to the high-water mark
    assert RiskGate().position_size(Chain.SOL, LANE, 95.0, tmp_db) == full


# ------------------------------------------------------------------ absent evidence


def test_a_profit_keeps_the_none_label():
    """The multiplier is 1.0 either way; the LABEL is what a reader sees in the log, and
    'recovery:dd-22.2%' would describe a drawdown that does not exist."""
    _mult, label = R.recovery_multiplier(-1_000_000_000, 4_500_000_000)
    assert label == "recovery:none", label


def test_a_broken_database_is_not_a_drawdown():
    """The `sqlite3.Error` branch. Writing bad JSON exercises `jload`, not this path, so a
    mutation that returned a drawdown on a dead database survived the file."""
    class Dead:
        def execute(self, *a, **k):
            import sqlite3 as _s
            raise _s.OperationalError("no such table: risk_state")

    dd, high = R.prior_day_drawdown(Chain.SOL, Dead(), today="2026-09-22")
    assert (dd, high) == (0, 0)
