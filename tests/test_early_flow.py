"""A token whose first minutes are more sells than buys is one we buy FROM a seller.

MEASURED 2026-09-22, lookahead-free (features from ``[t0, t0+300s]``, outcome strictly
after that instant), two independent samples:

* population, n=1,689 sol tokens, baseline 22.4% reach 2x --
  buy fraction < 0.45 -> 13.4% (0.60x the baseline rate, 15% of the sample);
  buy fraction >= 0.45 -> 24.0% (1.07x).
* our own closed live fills, n=50, mean -20.6% --
  buy fraction < 0.45 -> mean -36.5% over 13 trades;
  buy fraction >= 0.45 -> mean -15.0% over 37.

The direction replicates; the magnitude does not, which is what a 50-sample does. The
threshold and the size of the charge therefore come from the LARGE sample.

THE REJECTED HALF, pinned here because it is the more useful lesson. The same study first
proposed pairing this with "the price ran less than 1.05x in the first five minutes". On
the population that looked like a clean avoid -- 13.7% against a 22.5% baseline. Checked
against our own fills it had excluded two trades averaging **+5.3%**: it was avoiding
winners. A population result that reverses on our own money is not a rule, and it is not
in the code.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution.risk import (
    EARLY_BUY_FRACTION_FLOOR,
    EARLY_FLOW_MIN_PRINTS,
    EARLY_FLOW_MULTIPLIER,
    EARLY_FLOW_WINDOW_S,
    _early_flow_multiplier,
)

TOKEN = "8JVtwRnDiDmjV2pSRTmqzPtXZyNdgUkpijrufgYJRARA"
T0 = 1_790_000_000_000


def tape(conn, sides: list[str], *, offsets_s: list[int] | None = None,
         chain: Chain = Chain.SOL, token: str = TOKEN) -> None:
    offs = offsets_s if offsets_s is not None else list(range(len(sides)))
    for i, (side, off) in enumerate(zip(sides, offs, strict=True)):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "price_usd, usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (chain.value, f"tx{token}{i}", T0 + off * 1000, f"w{i}", token, side,
             "1", "1.0", "10", "test"),
        )
    conn.commit()


def test_a_sell_heavy_open_is_charged(tmp_db):
    """4 buys / 6 sells = 0.40, below the 0.45 floor measured on 1,689 tokens."""
    tape(tmp_db, ["buy"] * 4 + ["sell"] * 6)
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == EARLY_FLOW_MULTIPLIER
    assert "0.40" in label, label


def test_a_buy_heavy_open_is_not_charged(tmp_db):
    tape(tmp_db, ["buy"] * 7 + ["sell"] * 3)
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1)
    assert "0.70" in label, label


def test_exactly_at_the_floor_is_not_charged(tmp_db):
    """The floor is the measured boundary: `< 0.45` was the losing cell, not `<=`."""
    tape(tmp_db, ["buy"] * 9 + ["sell"] * 11)   # 0.45 exactly
    multiplier, _ = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1)


def test_only_the_first_window_counts(tmp_db):
    """Selling that starts AFTER the window is not evidence about the open.

    This is what makes the feature usable at entry: everything it reads is inside the
    first five minutes, so nothing after the decision instant can change it.
    """
    tape(tmp_db, ["buy"] * 8, offsets_s=[0, 1, 2, 3, 4, 5, 6, 7])
    tape(tmp_db, ["sell"] * 40,
         offsets_s=[EARLY_FLOW_WINDOW_S + 10 + i for i in range(40)])
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1), label
    assert "1.00" in label


def test_too_little_tape_is_charged_nothing(tmp_db):
    """No evidence is not evidence of selling; every other arm here keeps that rule."""
    tape(tmp_db, ["sell"] * (EARLY_FLOW_MIN_PRINTS - 1))
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1)
    assert "too_few_prints" in label


def test_no_tape_at_all_is_charged_nothing(tmp_db):
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1) and "no_tape" in label


def test_no_connection_is_charged_nothing():
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, None)
    assert multiplier == Decimal(1) and "no_conn" in label


def test_an_unreadable_tape_never_raises_into_the_sizer(tmp_db, monkeypatch):
    from kaiba.execution import risk as R

    monkeypatch.setattr(
        R, "fetch_one", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1) and "unavailable" in label


def test_another_chains_tape_is_not_read(tmp_db):
    tape(tmp_db, ["sell"] * 10, chain=Chain.BSC)
    multiplier, label = _early_flow_multiplier(Chain.SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(1) and "no_tape" in label


# ------------------------------------------------------------------ the constants


def test_the_threshold_is_the_one_the_large_sample_supports():
    """0.55 looked better on our 50 fills. 50 trades do not choose a threshold."""
    assert EARLY_BUY_FRACTION_FLOOR == Decimal("0.45")


def test_this_arm_only_ever_shrinks():
    assert Decimal(0) < EARLY_FLOW_MULTIPLIER < Decimal(1)


def test_the_window_is_the_measured_one():
    assert EARLY_FLOW_WINDOW_S == 300


def test_the_rejected_momentum_clause_is_absent():
    """`run < 1.05x` excluded two of our own trades averaging +5.3%. It must stay out."""
    import inspect

    from kaiba.execution import risk as R

    source = inspect.getsource(R._early_flow_multiplier)
    assert "price_usd" in source, "the flow read should still touch priced swaps"
    assert "1.05" not in source, "the rejected momentum clause reappeared"
