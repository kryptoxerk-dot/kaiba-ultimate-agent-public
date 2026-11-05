"""The standing entry study must not be able to rediscover the mistakes it was built after.

Two of them are pinned here because both were made for real on 2026-09-22 and both
produced a confident, wrong answer:

* measuring a feature over the SAME window as the outcome, which made "more unique wallets"
  look like a 2.3x lift when it was a token that pumped attracting wallets;
* reporting a cell of eight tokens as a finding, when a 13.9% base rate throws up a 37.5%
  cell on n=8 roughly one time in eight and hundreds of cells were scanned.
"""

from __future__ import annotations

import time

import pytest

from kaiba.core.schemas import Chain
from kaiba.learning import entry_study as S

NOW = int(time.time() * 1000)
T0 = NOW - 3_600_000


def swap(conn, token, *, at_s, price, side="buy", wallet="w", chain=Chain.SOL):
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, "
        "usd_value, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain.value, f"tx{token}{at_s}{side}{wallet}", T0 + int(at_s * 1000), wallet, token,
         side, "1", str(price), "10", "test"),
    )


def a_token(conn, token, *, in_window, after):
    """``in_window`` and ``after`` are lists of ``(offset_s, price, side)``."""
    for off, price, side in in_window:
        swap(conn, token, at_s=off, price=price, side=side, wallet=f"w{off}")
    for off, price, side in after:
        swap(conn, token, at_s=off, price=price, side=side, wallet=f"x{off}")
    conn.commit()


# ------------------------------------------------------------------ the lookahead rule


def test_activity_after_the_decision_cannot_reach_a_feature(tmp_db):
    """THE BUG: wallets/prints counted after the outcome made a pumped token look 'active'.

    Two tokens identical inside the window. One explodes with 60 new wallets afterwards.
    Their FEATURES must be identical; only the outcome may differ.
    """
    quiet = [(i, 1.0, "buy") for i in range(6)]
    a_token(tmp_db, "TokFlat", in_window=quiet, after=[(400, 1.0, "buy")])
    a_token(
        tmp_db, "TokPump", in_window=quiet,
        after=[(400 + i, 5.0, "buy") for i in range(60)],
    )
    obs = S._observations(tmp_db, Chain.SOL, window_days=7, now_ms=NOW)
    assert obs["TokFlat"]["wallets"] == obs["TokPump"]["wallets"]
    assert obs["TokFlat"]["prints"] == obs["TokPump"]["prints"]
    assert obs["TokFlat"]["buy_fraction"] == obs["TokPump"]["buy_fraction"]
    assert obs["TokPump"]["multiple"] > obs["TokFlat"]["multiple"], "only the outcome differs"


def test_the_outcome_is_measured_from_the_decision_price(tmp_db):
    """Not from the first print: we cannot buy at a price that has already passed."""
    a_token(
        tmp_db, "Tok",
        in_window=[(0, 1.0, "buy"), (1, 2.0, "buy"), (2, 3.0, "buy"), (3, 4.0, "buy"),
                   (4, 5.0, "buy"), (5, 5.0, "buy")],
        after=[(400, 10.0, "buy")],
    )
    obs = S._observations(tmp_db, Chain.SOL, window_days=7, now_ms=NOW)
    # entry is the LAST in-window price (5.0), so 10.0 is 2x -- not 10x off the first print
    assert obs["Tok"]["multiple"] == pytest.approx(2.0)


def test_a_token_with_no_tape_after_the_window_is_excluded(tmp_db):
    """No outcome is not a zero outcome."""
    a_token(tmp_db, "Tok", in_window=[(i, 1.0, "buy") for i in range(6)], after=[])
    assert S._observations(tmp_db, Chain.SOL, window_days=7, now_ms=NOW) == {}


def test_too_little_tape_in_the_window_is_excluded(tmp_db):
    a_token(tmp_db, "Tok",
            in_window=[(i, 1.0, "buy") for i in range(S.MIN_PRINTS_IN_WINDOW - 1)],
            after=[(400, 9.0, "buy")])
    assert S._observations(tmp_db, Chain.SOL, window_days=7, now_ms=NOW) == {}


# ------------------------------------------------------------------ the small-cell rule


def test_a_small_cell_is_never_reported(tmp_db):
    """A 37.5%-on-eight cell is what a scan produces, not what it finds."""
    for i in range(10):
        a_token(tmp_db, f"Tok{i}",
                in_window=[(j, 1.0, "buy") for j in range(6)],
                after=[(400, 9.0 if i < 4 else 1.0, "buy")])
    result = S.run_study(tmp_db, Chain.SOL, now_ms=NOW)
    assert result.sample == 10
    assert result.cells == [], "10 tokens cannot support a cell"
    assert any("below MIN_CELL" in n for n in result.notes)


def test_the_minimum_cell_is_not_tiny():
    assert S.MIN_CELL >= 30


def test_every_suppressed_cell_is_still_named(tmp_db):
    """Silence about a dropped cell is how a reader mistakes a scan for a result."""
    for i in range(12):
        a_token(tmp_db, f"Tok{i}",
                in_window=[(j, 1.0, "buy") for j in range(6)],
                after=[(400, 2.0, "buy")])
    result = S.run_study(tmp_db, Chain.SOL, now_ms=NOW)
    assert len(result.notes) >= len(S.FEATURES), result.notes


# ------------------------------------------------------------------ it only reports


def test_the_study_never_changes_a_gate(tmp_db):
    """A gate that retunes itself on its own output is a loop nobody reads."""
    import inspect

    source = inspect.getsource(S)
    assert "EARLY_BUY_FRACTION_FLOOR" not in source
    assert "save_risk" not in source and "set_lane" not in source


def test_results_are_appended_not_overwritten(tmp_db):
    """Drift over time is the whole reason this runs on a schedule."""
    for i in range(80):
        a_token(tmp_db, f"Tok{i}",
                in_window=[(j, 1.0, "buy") for j in range(6)],
                after=[(400, 9.0 if i % 4 == 0 else 1.0, "buy")])
    first = S.run_study(tmp_db, Chain.SOL, now_ms=NOW)
    assert S.record(tmp_db, first) > 0
    second = S.run_study(tmp_db, Chain.SOL, now_ms=NOW + 1_800_000)
    S.record(tmp_db, second)
    stamps = tmp_db.execute("SELECT COUNT(DISTINCT computed_ms) FROM entry_study").fetchone()[0]
    assert stamps == 2


def test_the_report_states_the_denominator_and_the_clock(tmp_db):
    for i in range(80):
        a_token(tmp_db, f"Tok{i}",
                in_window=[(j, 1.0, "buy") for j in range(6)],
                after=[(400, 9.0 if i % 3 == 0 else 1.0, "buy")])
    text = "\n".join(S.lines(S.run_study(tmp_db, Chain.SOL, now_ms=NOW)))
    assert "baseline" in text and "n=" in text
    assert f"t0+{S.FEATURE_WINDOW_S}s" in text, "the decision instant must be stated"
    assert "strictly after" in text


def test_chains_are_studied_separately(tmp_db):
    a_token(tmp_db, "TokSol", in_window=[(i, 1.0, "buy") for i in range(6)],
            after=[(400, 9.0, "buy")])
    result = S.run_study(tmp_db, Chain.BSC, now_ms=NOW)
    assert result.sample == 0
