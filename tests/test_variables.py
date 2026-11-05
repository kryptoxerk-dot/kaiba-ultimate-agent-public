"""A variable search must reject what it cannot confirm, and say how much it looked at.

Twenty variables in five bins is a hundred hypotheses. At a 26% base rate several will
clear any lift threshold by luck, and each arrives with a plausible story attached. The
protection is not the statistics, it is the holdout: the older half of the tape discovers,
the newer half confirms, and a finding that does not clear its threshold in BOTH is
reported as rejected rather than quietly dropped.

The split is by TIME and not at random on purpose. The question that matters is whether a
variable still worked LATER, which is the only sense in which it might work tomorrow. A
random split lets a fad that lived six hours pass, because its tokens land on both sides.

MEASURED 2026-09-23 on the live tape: 10,822 tokens, 65 single hypotheses, and the holdout
threw out 5 that discovery liked -- `holder_count=q4` went 1.56x -> 0.88x, `bundler_pct=q1`
1.39x -> 1.00x. Those are exactly the findings this module exists to stop us trading on.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain
from kaiba.learning import variables as V


def tape(conn, token, prices, *, chain=Chain.SOL, wallet="w1"):
    for i, price in enumerate(prices):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, price_usd, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (chain.value, f"tx{token}{i}", 1_000_000 + i * 1000, wallet, token, "buy",
             str(price), "test"),
        )


def dossier(conn, token, *, chain=Chain.SOL, grade="C", **fields):
    import json

    body = {k: {"value": v} for k, v in fields.items()}
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, token, 1, None, grade, "[]", "[]", "[]", json.dumps(body)),
    )


def population(conn, *, n=400, signal_rate=0.9, noise_rate=0.05):
    """Half the tokens carry a REAL signal, half are noise, spread evenly over time."""
    for i in range(n):
        token = f"T{i:04d}"
        carries = i % 2 == 0
        runs = (i % 10) < (10 * signal_rate) if carries else (i % 20) < (20 * noise_rate)
        prices = [1.0] * 10 + ([20.0] if runs else [1.0]) + [1.0] * 5
        tape(conn, token, prices)
        dossier(conn, token, holder_count=(500 if carries else 5), liquidity_usd=1000.0)
    conn.commit()


# ------------------------------------------------------------------ the holdout


def test_a_real_signal_is_confirmed(tmp_db):
    population(tmp_db)
    study = V.run(tmp_db, pairs=False)
    assert study.sample > 0
    names = {f.variable for f in study.confirmed}
    assert "holder_count" in names, [f"{f.variable}={f.bin_label}" for f in study.confirmed]


def test_a_signal_present_only_in_the_first_half_is_rejected(tmp_db):
    """THE POINT OF THE MODULE: a fad that stopped working must not be reported.

    `holder_count` separates perfectly in the older half and not at all in the newer one.
    Discovery alone would rank it top; the holdout is what refuses it.
    """
    n = 400
    for i in range(n):
        token = f"T{i:04d}"
        high = i % 2 == 0
        first_half = i < n // 2
        runs = high if first_half else (i % 7 == 0)  # the edge dies in the second half
        prices = [1.0] * 10 + ([20.0] if runs else [1.0]) + [1.0] * 5
        tape(tmp_db, token, prices)
        dossier(tmp_db, token, holder_count=(500 if high else 5))
    tmp_db.commit()
    study = V.run(tmp_db, pairs=False)
    confirmed = {f.variable for f in study.confirmed}
    assert "holder_count" not in confirmed, "a dead edge was confirmed"
    assert any(f.variable == "holder_count" for f in study.rejected), (
        "the dead edge was not even reported as rejected; it vanished silently"
    )


def test_the_split_is_by_time_not_by_chance(tmp_db):
    population(tmp_db)
    study = V.run(tmp_db, pairs=False)
    rows = V.build_rows(tmp_db)
    half = len(rows) // 2
    assert study.split_ms == rows[half][2]
    assert all(rows[i][2] <= rows[i + 1][2] for i in range(len(rows) - 1)), "rows not in time order"


def test_a_thin_bin_is_never_confirmed(tmp_db):
    """A bin that cannot fill MIN_CELL in both halves cannot confirm anything."""
    population(tmp_db, n=60)
    study = V.run(tmp_db, pairs=False)
    assert all(f.discovery.n >= V.MIN_CELL and f.holdout.n >= V.MIN_CELL for f in study.confirmed)


def test_the_number_of_hypotheses_is_reported(tmp_db):
    """Without this a reader cannot judge how impressed to be by the survivors."""
    population(tmp_db)
    study = V.run(tmp_db, pairs=False)
    assert study.hypotheses > 0
    assert str(study.hypotheses) in " ".join(V.lines(study))


# ------------------------------------------------------------------ no lookahead


def test_the_outcome_is_measured_after_the_feature_window(tmp_db):
    """Inherited from `mooner`, and re-pinned here because this module depends on it."""
    tape(tmp_db, "EARLY", [1.0] * 9 + [50.0] + [50.0] * 6)
    tmp_db.commit()
    rows = V.build_rows(tmp_db)
    assert rows and rows[0][1] is False, "a move inside the feature window counted as the outcome"


# ------------------------------------------------------------------ confluence


def test_a_pair_that_only_restates_one_half_is_not_a_confluence(tmp_db):
    """Two views of the same signal is conviction counted twice, not evidence."""
    n = 400
    for i in range(n):
        token = f"T{i:04d}"
        carries = i % 2 == 0
        runs = carries and (i % 4 != 3)
        prices = [1.0] * 10 + ([20.0] if runs else [1.0]) + [1.0] * 5
        tape(tmp_db, token, prices)
        # two fields that are the SAME fact wearing different names
        dossier(tmp_db, token, holder_count=(500 if carries else 5),
                liquidity_usd=(9999.0 if carries else 1.0))
    tmp_db.commit()
    study = V.run(tmp_db)
    for pair in study.pairs:
        assert pair.gain >= V.MIN_PAIR_GAIN, pair.label


def test_an_empty_tape_measures_nothing_rather_than_zero(tmp_db):
    study = V.run(tmp_db)
    assert study.sample == 0 and study.confirmed == []
    assert "not enough tape" in " ".join(V.lines(study))


def test_rejected_findings_keep_their_discovery_number(tmp_db):
    """So a reader can see exactly what was tempting, not just that something was dropped."""
    n = 400
    for i in range(n):
        token = f"T{i:04d}"
        high = i % 2 == 0
        runs = high if i < n // 2 else (i % 7 == 0)
        prices = [1.0] * 10 + ([20.0] if runs else [1.0]) + [1.0] * 5
        tape(tmp_db, token, prices)
        dossier(tmp_db, token, holder_count=(500 if high else 5))
    tmp_db.commit()
    study = V.run(tmp_db, pairs=False)
    assert study.rejected
    assert all(f.discovery_lift >= V.MIN_LIFT for f in study.rejected)
    assert "REJECTED" in " ".join(V.lines(study))
