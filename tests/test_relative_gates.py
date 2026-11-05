"""The relative criterion: a TIGHTENING is judged on EV per trade against the incumbent.

Lead's decision 2026-10-02 (owner objective: "judge every change by whether it moves
expected value per trade"). On a losing lane the absolute rule (DSR >= 0.95 on the
candidate's own returns) can never pass a filter that only cuts the loss; a tightening now
passes on (a) PBO < 0.5, (b) out-of-sample EV improvement > 0 with a paired day-cluster
bootstrap 95% lower bound > 0, (c) >= 30 candidate trades out of sample, (d) <= 50% of the
lane's decided signals removed -- plus the drawdown condition kept from the absolute rule.
Anything that is not a tightening keeps the absolute rule.

Each stream below is built so that exactly ONE criterion fails, so removing that criterion
from the gate makes its test fail (mutation-checked). Rows use the real sm-trenches payload
(``smart_wallets``); candidate ``min_smart_degen`` 3 -> 4 keeps the smart_wallets=5 rows.
"""

from __future__ import annotations

import json

import pytest

from kaiba.learning import gates, validation
from tests.test_experiment_loop import BASE_MS, DAY, HOUR, LANE, add_entry, set_param, write_risk_file


@pytest.fixture
def risk_file(tmp_path, monkeypatch):
    return write_risk_file(tmp_path / "risk.yaml", monkeypatch)


N = 240
SPACING = 10 * HOUR  # 100 days; the last 30% (72 rows) cover 30 distinct days


def stream(conn, ret, *, n: int = N, spacing: int = SPACING, prefix: str = "r") -> None:
    """``ret(i) -> (smart_wallets, pnl on a cost of 1000)`` for entry ``i``."""
    for i in range(n):
        smart, pnl = ret(i)
        add_entry(conn, f"{prefix}{i}", ts=BASE_MS + i * spacing, smart=smart, pnl=pnl)


def losing_lane_better_subset(i: int) -> tuple[int, int]:
    """Every entry loses; the 2-in-3 with smart_wallets=5 lose ~5%, the rest ~30%."""
    if i % 3 != 2:
        return 5, (-30 if i % 2 else -70)
    return 3, (-280 if i % 2 else -320)


def judge(conn, *, old=3, new=4, **kw) -> gates.GateResult:
    row = {"experiment_id": "exp_rel", "lane": LANE,
           "diff_json": json.dumps({"lane": LANE, "key": "min_smart_degen", "old": old, "new": new})}
    return gates.replay_gate(row, conn, record=False, **kw)


def failures(result: gates.GateResult) -> list[str]:
    return [r for r in result.reasons if not r.startswith("info:") and "criteria met" not in r]


# ======================================================================= replay: passes


def test_a_tightening_that_raises_ev_per_trade_on_a_losing_lane_passes(tmp_db, risk_file):
    stream(tmp_db, losing_lane_better_subset)

    result = judge(tmp_db)

    assert result.passed is True, result.reasons
    m = result.metrics
    assert m["criterion"] == "relative"
    imp = m["oos_improvement"]
    assert imp["n_candidate"] == 48 and imp["n_incumbent"] == 72 and imp["days"] >= 20
    assert imp["delta"] == pytest.approx(0.0833, abs=0.002)  # -5% vs -13.3% per trade
    assert imp["ci_lo"] > 0
    assert m["dsr"] < gates.DSR_MIN  # the candidate still loses money: absolute rule would fail
    assert any(r.startswith("info: absolute DSR") for r in result.reasons)
    assert m["oos_candidate_expectancy"] < 0


# ======================================================================= replay: each failure


def test_a_tightening_that_lowers_ev_per_trade_fails(tmp_db, risk_file):
    stream(tmp_db, lambda i: (5, -300) if i % 3 != 2 else (3, -50))
    result = judge(tmp_db)
    assert result.passed is False
    assert result.metrics["oos_improvement"]["delta"] < 0
    assert failures(result) == [next(r for r in result.reasons if "does not earn more" in r)]


def test_an_improvement_whose_bound_straddles_zero_fails(tmp_db, risk_file):
    """Better on the point estimate, but a coin flip day to day: not evidence."""
    def ret(i):
        if i % 3 != 2:
            return 5, (900 if i % 2 == 0 else -850)
        return 3, -20

    stream(tmp_db, ret)
    result = judge(tmp_db)
    imp = result.metrics["oos_improvement"]
    assert imp["delta"] > 0 and imp["ci_lo"] <= 0
    assert result.passed is False
    assert len(failures(result)) == 1 and "lower bound" in failures(result)[0]


def test_pbo_at_or_above_half_fails_even_with_a_clean_oos_improvement(tmp_db, risk_file):
    """The candidate wins in blocks 1,3,5,6,7 and loses in 0,2,4 by the same margin: the
    walk-forward tail (blocks 5-7) looks great, but in CSCV whichever configuration wins
    in-sample loses out of sample."""
    block = N // 8

    def ret(i):
        winning = (i // block) in (1, 3, 5, 6, 7)
        if i % 3 != 2:
            return 5, (50 if winning else -150)
        return 3, (-250 if winning else 150)

    stream(tmp_db, ret)
    result = judge(tmp_db)
    m = result.metrics
    assert m["pbo"] >= gates.PBO_MAX
    assert m["oos_improvement"]["delta"] > 0 and m["oos_improvement"]["ci_lo"] > 0
    assert result.passed is False
    assert len(failures(result)) == 1 and failures(result)[0].startswith("PBO")


def test_too_few_out_of_sample_candidate_trades_fail(tmp_db, risk_file):
    stream(tmp_db, losing_lane_better_subset, n=120, spacing=20 * HOUR)  # OOS: 36 rows, 24 kept
    result = judge(tmp_db)
    assert result.passed is False
    assert failures(result) == ["candidate took 24 out-of-sample trades, need >= 30"]


def test_removing_most_of_the_lanes_decided_signals_fails(tmp_db, risk_file):
    """Judged on fills it removes a third; on the signals the lane decided on, three quarters."""
    stream(tmp_db, losing_lane_better_subset)
    for i in range(400):
        add_entry(tmp_db, f"skip{i}", ts=BASE_MS + i * 6 * HOUR, smart=3, pnl=0, action="skip")
    result = judge(tmp_db)
    share = result.metrics["removal"]
    assert (share["removed"], share["measured"]) == (80 + 400, 240 + 400)
    assert result.passed is False
    assert len(failures(result)) == 1 and "decided signals" in failures(result)[0]


def test_too_few_distinct_days_cannot_be_bounded_and_fail(tmp_db, risk_file):
    stream(tmp_db, losing_lane_better_subset, spacing=2 * HOUR)  # OOS: 72 rows in 6 days
    result = judge(tmp_db)
    imp = result.metrics["oos_improvement"]
    assert imp["delta"] > 0 and imp["ci_lo"] is None and imp["days"] < gates.MIN_CLUSTER_DAYS
    assert result.passed is False
    assert len(failures(result)) == 1 and "cannot be bounded" in failures(result)[0]


# ======================================================================= replay: the absolute rule stays


@pytest.mark.parametrize(("incumbent", "candidate"), [(5, 4), (4, 4)])
def test_anything_but_a_tightening_keeps_the_absolute_rule(tmp_db, risk_file, incumbent, candidate):
    stream(tmp_db, losing_lane_better_subset)
    set_param(risk_file, "min_smart_degen", incumbent)
    result = judge(tmp_db, old=incumbent, new=candidate)
    assert result.metrics["criterion"] == "absolute"
    assert result.passed is False
    assert any(r.startswith("DSR ") and "< 0.95" in r for r in result.reasons)
    assert "oos_improvement" not in result.metrics


# ======================================================================= shadow


def shadow_arms(conn, exp_id: str, *, cand, refused, n_cand: int = 60, n_ref: int = 30,
                days: int = 25, label_refused: bool = True) -> None:
    """Labelled forward trades; ``cand(i)`` / ``refused(i)`` give pnl on a cost of 1000."""
    total = n_cand + n_ref
    step = (days * DAY) // (total - 1)
    k_c = k_r = 0
    for i in range(total):
        is_cand = i % 3 != 2 if n_ref else True
        ts = BASE_MS + 200 * DAY + i * step
        if is_cand:
            pnl, arm, k_c = cand(k_c), "candidate", k_c + 1
        else:
            pnl, arm, k_r = refused(k_r), "incumbent", k_r + 1
        tid = f"sh_{i}"
        conn.execute(
            "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, closed_ms, "
            "hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"p_{tid}", LANE, "live", "robinhood", f"t_{tid}", ts, ts + 60_000, 60, "1000",
             str(1000 + pnl), str(pnl), pnl / 10.0),
        )
        if arm == "candidate" or label_refused:
            conn.execute(
                "INSERT INTO experiment_trades (experiment_id, trade_id, arm, linked_ms) VALUES (?,?,?,?)",
                (exp_id, tid, arm, ts),
            )


def shadow_judge(conn, *, old=3, new=4) -> gates.GateResult:
    row = {"experiment_id": "exp_sh", "lane": LANE,
           "diff_json": json.dumps({"lane": LANE, "key": "min_smart_degen", "old": old, "new": new})}
    return gates.shadow_gate(row, conn, record=False)


def test_shadow_passes_a_subset_that_loses_less_per_trade(tmp_db, risk_file):
    shadow_arms(tmp_db, "exp_sh", cand=lambda k: -30 if k % 2 else -70, refused=lambda k: -300)
    result = shadow_judge(tmp_db)
    assert result.passed is True, result.reasons
    imp = result.metrics["improvement"]
    assert result.metrics["criterion"] == "relative"
    assert imp["delta"] == pytest.approx(0.0833, abs=0.002) and imp["ci_lo"] > 0


def test_shadow_refuses_a_point_estimate_whose_bound_straddles_zero(tmp_db, risk_file):
    """The old rule compared point expectancies and would have passed this."""
    shadow_arms(tmp_db, "exp_sh", cand=lambda k: 900 if k % 2 == 0 else -850, refused=lambda k: -20)
    result = shadow_judge(tmp_db)
    imp = result.metrics["improvement"]
    assert imp["delta"] > 0 and imp["ci_lo"] <= 0
    assert result.passed is False
    assert len(failures(result)) == 1 and "lower bound" in failures(result)[0]


def test_shadow_needs_the_refused_arm_labelled(tmp_db, risk_file):
    shadow_arms(tmp_db, "exp_sh", cand=lambda k: -30, refused=lambda k: -300, label_refused=False)
    result = shadow_judge(tmp_db)
    assert result.passed is False
    assert any("no refused trades are labelled" in r for r in result.reasons)


def test_shadow_with_too_few_distinct_days_cannot_be_bounded(tmp_db, risk_file):
    shadow_arms(tmp_db, "exp_sh", cand=lambda k: -30 if k % 2 else -70, refused=lambda k: -300, days=16)
    result = shadow_judge(tmp_db)
    assert result.passed is False
    assert len(failures(result)) == 1 and "cannot be bounded" in failures(result)[0]


def test_shadow_of_a_non_tightening_keeps_the_absolute_rule(tmp_db, risk_file):
    shadow_arms(tmp_db, "exp_sh", cand=lambda k: -30, refused=lambda k: -300)
    result = shadow_judge(tmp_db, old=5, new=4)
    assert result.metrics["criterion"] == "absolute"


# ======================================================================= the bootstrap itself


def test_the_day_minimum_is_the_validation_modules_own():
    assert gates.MIN_CLUSTER_DAYS == validation.MIN_BOOTSTRAP_BLOCKS


def test_paired_improvement_is_hand_computable_and_reproducible():
    # 20 days, each with one kept +1 and one refused -1: delta = 1 - 0 = 1 on every resample.
    obs = []
    for d in range(20):
        obs += [(BASE_MS + d * DAY, 1.0, True), (BASE_MS + d * DAY + HOUR, -1.0, False)]
    out = gates.paired_improvement(obs)
    assert out["delta"] == 1.0 and out["ci_lo"] == 1.0 and out["ci_hi"] == 1.0
    assert gates.paired_improvement(obs) == out
    short = gates.paired_improvement(obs[:38])  # 19 days
    assert short["ci_lo"] is None and "19 distinct UTC days" in short["unevaluable"]
