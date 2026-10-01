"""The Phase 4 validation harness.

The tests that matter most here are the ones that prove the harness can return a negative.
A validation suite that has never said "no" is not evidence of anything, so the
falsification block below is load-bearing in a way the happy paths are not: it feeds a
zero-edge placebo through the identical arithmetic Gate 2 uses and insists it fails, and
feeds a synthetic true edge through and insists it passes. If either direction breaks, a
verdict from this module means nothing.

The second theme is that **underpowered is not failed and not passed**. Almost every
assertion about our real data is an assertion that the answer is "we cannot tell", and the
tests check that this third state survives every path: per criterion, per gate, up through
the protocol, and into the database. A harness that quietly rounds "no data" to either
neighbour is worse than no harness, because it launders absence into a result.

Fixtures are hand-computable on purpose. Every expected number below can be worked out
with a pencil from the rows the helpers insert.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Lane
from kaiba.learning import registry
from kaiba.learning import validation as v
from kaiba.learning.validation import Verdict

BASE_MS = 1_700_000_000_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
SOL_TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
WALLET_A = "2xNweLHLqrbx4zo1waDvgWJHgsUpPj8Y8icbAFeewsMR"


# ------------------------------------------------------------------------------ fixtures


def insert_trade(
    conn,
    trade_id: str,
    *,
    lane: str = "confluence-5",
    mode: str = "shadow",
    closed_ms: int = BASE_MS,
    cost: int = 1000,
    pnl: int = 100,
    fees: int = 12,
    slippage_bps: int | None = 40,
    decision_id: str | None = None,
    opened_ms: int | None = None,
) -> str:
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, "
        "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, "
        "fees_native, slippage_bps) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id, f"pos_{trade_id}", decision_id, lane, mode, "sol", SOL_TOKEN,
            opened_ms if opened_ms is not None else closed_ms - 60_000, closed_ms, 60,
            str(cost), str(cost + pnl), str(pnl), (pnl / cost) * 100 if cost else 0.0,
            str(fees), slippage_bps,
        ),
    )
    return trade_id


def insert_decision(conn, decision_id: str, *, ts_ms: int = BASE_MS, action: str = "enter",
                    lane: str = "confluence-5", signals: str = "[]") -> str:
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, "
        "signals_json) VALUES (?,?,?,?,?,?,?,?)",
        (decision_id, ts_ms, lane, "shadow", "sol", SOL_TOKEN, action, signals),
    )
    return decision_id


def insert_signal(conn, signal_id: str, *, created_ms: int = BASE_MS) -> str:
    conn.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms) "
        "VALUES (?,?,?,?,?,?)",
        (signal_id, "confluence-5", "sol", SOL_TOKEN, 0.5, created_ms),
    )
    return signal_id


def insert_wallet_score(
    conn, address: str, *, grade: str, closed_trades: int = 20, distinct_tokens: int = 10,
    evidence_weight: float = 0.8, scored_at_ms: int = BASE_MS, last_seen_ms: int = BASE_MS,
) -> None:
    conn.execute(
        "INSERT INTO wallets (chain, address, source, first_seen_ms, last_seen_ms) "
        "VALUES (?,?,?,?,?)",
        ("sol", address, "test", BASE_MS - DAY_MS, last_seen_ms),
    )
    conn.execute(
        "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
        "closed_trades, distinct_tokens, model_version, scored_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", address, 70.0, grade, evidence_weight, "sniper", closed_trades,
         distinct_tokens, "v1", scored_at_ms),
    )


def insert_swap(conn, address: str, *, token: str, side: str, ts_ms: int,
                amount_token: int, amount_native: int, tx: str) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, "
        "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("sol", tx, 1, ts_ms, address, token, side, str(amount_token), str(amount_native),
         "test"),
    )


def round_trip(conn, address: str, *, token: str, ts_ms: int, cost: int, proceeds: int,
               tag: str) -> None:
    """One closed episode: buy the whole lot, sell the whole lot."""
    insert_swap(conn, address, token=token, side="buy", ts_ms=ts_ms, amount_token=1_000_000,
                amount_native=cost, tx=f"buy_{tag}")
    insert_swap(conn, address, token=token, side="sell", ts_ms=ts_ms + 60_000,
                amount_token=1_000_000, amount_native=proceeds, tx=f"sell_{tag}")


# ================================================================= refusing to conclude
#
# The distinction this whole module exists to preserve. If these fail, nothing else in the
# file means anything, because every other result could be an absence in disguise.


def test_underpowered_is_not_pass_and_not_fail():
    assert Verdict.UNDERPOWERED is not Verdict.PASS
    assert Verdict.UNDERPOWERED is not Verdict.FAIL
    assert Verdict.BLOCKED is not Verdict.PASS
    assert len({v_.value for v_ in Verdict}) == 4


def test_a_criterion_with_no_input_is_underpowered_never_passed():
    crit = v.criterion("anything", None, 10.0)
    assert crit.verdict is Verdict.UNDERPOWERED
    assert not crit.passed


def test_a_criterion_with_no_threshold_is_also_underpowered():
    """A threshold nobody set is not a threshold everybody met."""
    assert v.criterion("anything", 5.0, None).verdict is Verdict.UNDERPOWERED


def test_a_criterion_that_is_met_passes_and_one_that_is_not_fails():
    assert v.criterion("n", 10.0, 5.0, "ge").verdict is Verdict.PASS
    assert v.criterion("n", 1.0, 5.0, "ge").verdict is Verdict.FAIL
    assert v.criterion("p", 0.01, 0.05, "lt").verdict is Verdict.PASS
    assert v.criterion("p", 0.06, 0.05, "lt").verdict is Verdict.FAIL


def test_a_real_failure_outranks_a_missing_input():
    """One broken criterion and one unmeasured one: the gate has been answered, and it is no."""
    verdict = v.combine([
        v.criterion("a", 1.0, 5.0, "ge"),   # fail
        v.criterion("b", None, 5.0),        # underpowered
    ])
    assert verdict is Verdict.FAIL


def test_one_missing_input_makes_the_whole_gate_underpowered():
    verdict = v.combine([
        v.criterion("a", 10.0, 5.0, "ge"),  # pass
        v.criterion("b", None, 5.0),        # underpowered
    ])
    assert verdict is Verdict.UNDERPOWERED


def test_an_empty_criteria_list_is_underpowered_not_a_vacuous_pass():
    assert v.combine([]) is Verdict.UNDERPOWERED


def test_all_criteria_met_is_the_only_way_to_pass():
    assert v.combine([v.criterion("a", 10.0, 5.0), v.criterion("b", 6.0, 5.0)]) is Verdict.PASS


# ============================================================== the delete-best-5% rule


def test_deleting_the_best_5_percent_removes_the_top_tail():
    series = [Decimal(x) for x in range(100)]  # 0..99
    trimmed = v.delete_best(series)
    assert len(trimmed) == 95
    assert max(trimmed) == Decimal(94)


def test_deleting_the_best_always_removes_at_least_one_trade():
    """On a short series 5% rounds to nothing; one is still deleted, or the gate is free."""
    assert len(v.delete_best([Decimal(1), Decimal(2)])) == 1
    assert len(v.delete_best([Decimal(5)])) == 0


def test_delete_best_on_an_empty_series_is_empty():
    assert v.delete_best([]) == []


def test_a_strategy_that_lives_on_its_tail_fails_the_tail_rule():
    """The 2606.08232 case: triple-digit cumulative return, three trades from zero.

    190 trades, 187 of them losing 1R, three of them winning 100R. Cumulative +113R.
    Delete the best 5% — ten trades, which takes all three winners — and it is −187R.
    """
    returns = [Decimal(-1)] * 187 + [Decimal(100)] * 3
    assert sum(returns) == Decimal(113)
    assert sum(v.delete_best(returns)) < 0

    criteria, _ = v.statistical_verdict(
        returns, trials=1, timestamps=[BASE_MS + i * 3_600_000 for i in range(190)],
        include_pbo=False,
    )
    tail = next(c for c in criteria if c.name == "positive_after_deleting_best_5pct")
    assert tail.verdict is Verdict.FAIL


def test_a_broad_based_edge_survives_the_tail_rule():
    returns = [Decimal(2)] * 45 + [Decimal(-1)] * 55
    assert sum(v.delete_best(returns)) > 0


# ========================================================================= power report


def test_with_no_trades_the_verdict_is_underpowered(tmp_db):
    report = v.power_report(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED
    assert report.closed_trades == 0
    assert "UNDERPOWERED" in report.headline


def test_with_no_trades_the_needed_sample_comes_from_the_research_profiles(tmp_db):
    """We cannot estimate a fat tail from nothing, so we say whose numbers we are using."""
    report = v.power_report(tmp_db)
    assert report.profile in {p.name for p in v.FALLBACK_PROFILES}
    assert "research" in report.profile_basis
    assert report.trades_needed_deflated is not None
    assert 3000 <= report.trades_needed_deflated <= 30000


def test_a_zero_closing_rate_gives_an_unbounded_time_not_a_zero_one(tmp_db):
    insert_decision(tmp_db, "d1", action="skip")
    report = v.power_report(tmp_db)
    assert report.trades_per_week == 0.0
    assert report.weeks_to_significance is None
    assert report.exceeds_regime is True
    assert any("unbounded" in n for n in report.notes)


def test_the_report_says_in_words_when_the_sample_outlives_the_regime(tmp_db):
    report = v.power_report(tmp_db)
    shouted = [n for n in report.notes if "TIME TO SIGNIFICANCE EXCEEDS THE REGIME" in n]
    assert shouted, "the headline finding must be stated, not left to be inferred"
    assert "working figure" in shouted[0]


def test_an_unreachable_sample_is_still_quoted_in_years(tmp_db):
    """"Never at the current rate" is true and useless; the years figure is the decision."""
    report = v.power_report(tmp_db)
    assert report.years_at_achievable_rate is not None
    best, worst = report.years_at_achievable_rate
    assert 0 < best < worst
    assert "years" in report.headline


def test_the_deflation_uses_the_registry_and_callers_cannot_lower_it(tmp_db):
    for n in range(1, 12):
        registry.register(Lane.CONFLUENCE_5, {"min_entities": n}, tmp_db)
    assert v.honest_trials(tmp_db, Lane.CONFLUENCE_5) == 11
    # There is no argument to pass a smaller N: the signature simply does not accept one.
    import inspect

    params = inspect.signature(v.power_report).parameters
    assert not any("trial" in name for name in params)


def test_more_trials_raise_the_bar_and_the_required_sample(tmp_db):
    """N is monotone, and every re-tune makes promotion harder. That is the intended incentive."""
    low = v.power_report(tmp_db)
    for n in range(500):
        registry.register(Lane.CONFLUENCE_5, {"threshold": n}, tmp_db)
    high = v.power_report(tmp_db)
    assert high.trials > low.trials
    assert high.z_deflated > low.z_deflated
    assert high.trades_needed_deflated > low.trades_needed_deflated


def test_the_window_falls_back_to_the_decision_stream_when_nothing_has_closed(tmp_db):
    insert_decision(tmp_db, "d1", ts_ms=BASE_MS, action="skip")
    insert_decision(tmp_db, "d2", ts_ms=BASE_MS + 2 * DAY_MS, action="skip")
    report = v.power_report(tmp_db)
    assert report.window_days == pytest.approx(2.0, abs=0.01)
    assert report.total_decisions == 2
    assert report.entry_decisions == 0


def test_a_measured_rate_produces_a_finite_time_to_significance(tmp_db):
    for i in range(20):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * DAY_MS, pnl=50)
    report = v.power_report(tmp_db)
    assert report.trades_per_week is not None and report.trades_per_week > 0
    assert report.weeks_to_significance is not None
    assert report.weeks_to_significance > 0


def test_too_few_trades_to_estimate_our_own_spread_says_so(tmp_db):
    for i in range(10):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * DAY_MS)
    report = v.power_report(tmp_db)
    assert report.profile != "observed"
    assert any(str(v.MIN_TRADES_FOR_OWN_DISTRIBUTION) in n for n in report.notes)


def test_enough_trades_switches_to_our_own_measured_distribution(tmp_db):
    for i in range(v.MIN_TRADES_FOR_OWN_DISTRIBUTION + 5):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * 3_600_000,
                     pnl=100 if i % 3 else -200)
    report = v.power_report(tmp_db)
    assert report.profile == "observed"
    assert report.mean_r is not None and report.sd_r is not None
    assert report.hit_rate is not None


def test_a_negative_edge_gets_no_sample_size_at_all(tmp_db):
    """Sizing a sample for a losing strategy is a sentence that reads as progress."""
    for i in range(v.MIN_TRADES_FOR_OWN_DISTRIBUTION + 5):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * 3_600_000, pnl=-100 - i)
    report = v.power_report(tmp_db)
    assert report.trades_needed_deflated is None
    assert report.verdict is Verdict.UNDERPOWERED
    assert "no positive per-trade edge" in report.headline


def test_the_power_report_persists_and_reads_back(tmp_db):
    report = v.power_report(tmp_db)
    run_id = report.record(tmp_db)
    rows = v.latest_runs(tmp_db)
    assert [r["run_id"] for r in rows] == [run_id]
    assert rows[0]["verdict"] == "underpowered"


# ================================================================ sample-size arithmetic


def test_expected_max_z_reproduces_the_published_values():
    """Bailey & Lopez de Prado's expected maximum under the null, to two decimals."""
    assert v.expected_max_z(100) == pytest.approx(2.53, abs=0.01)
    assert v.expected_max_z(1000) == pytest.approx(3.26, abs=0.01)
    assert v.expected_max_z(10000) == pytest.approx(3.86, abs=0.01)
    # The CoinGecko pump.fun wallet population: the best of 3.1M zero-skill wallets shows a
    # ~5-sigma record. A 4-sigma wallet is not a discovery.
    assert v.expected_max_z(3_142_559) == pytest.approx(5.09, abs=0.02)


def test_deflation_can_only_raise_the_bar():
    assert v.significance_z(1) == v.Z_NAIVE
    assert v.significance_z(2) >= v.Z_NAIVE
    assert v.significance_z(1000) > v.significance_z(100) > v.Z_NAIVE


def test_the_research_sample_sizes_are_reproduced():
    """Profile C: 35% hit with a fat tail needs ~5,400 trades for a bare t=1.96."""
    profile = v.FALLBACK_PROFILES[0]
    assert v.trades_needed(profile.mean_r, profile.sd_r, v.Z_NAIVE) == pytest.approx(5447, rel=0.01)
    # Profile D, the 32% variant, needs ~3,600.
    d = v.FALLBACK_PROFILES[1]
    assert v.trades_needed(d.mean_r, d.sd_r, v.Z_NAIVE) == pytest.approx(3594, rel=0.01)


def test_no_sample_size_for_a_non_positive_edge():
    assert v.trades_needed(Decimal(0), Decimal(1), 1.96) is None
    assert v.trades_needed(Decimal("-0.1"), Decimal(1), 1.96) is None
    assert v.trades_needed(Decimal("0.1"), Decimal(0), 1.96) is None


def test_the_block_bootstrap_needs_more_than_one_day():
    same_day = [(BASE_MS + i * 1000, Decimal(1)) for i in range(50)]
    assert v.block_bootstrap_t(same_day) is None


def test_the_bootstrap_refuses_rather_than_overstate_on_too_few_days():
    """The method's own failure mode, guarded.

    Eighty trades bunched into four days give the bootstrap four blocks to estimate
    between-block variance from. When those blocks resemble each other it reports a tiny
    standard error and a t of ~18 — it fails *towards* confidence, which is the direction
    that gets capital deployed. So below the block minimum it says nothing at all.
    """
    pattern = [Decimal(str(1.0 if i % 3 else -0.9)) for i in range(80)]
    clustered = [(BASE_MS + (i // 20) * DAY_MS, r) for i, r in enumerate(pattern)]
    assert v.block_bootstrap_t(clustered, draws=800) is None


def test_the_block_bootstrap_discounts_a_day_level_common_factor():
    """400 trades driven by 40 daily coin flips are 40 observations, not 400.

    This is the clustering the research warns about, in its purest form: every trade on a
    day shares that day's SOL regime and narrative, so the day is the unit. The naive t
    divides by sqrt(400) and reads ~4.9, comfortably "significant". The block bootstrap
    divides by the between-day spread and reads ~1.5, which is the truth. The gate uses the
    second one.
    """
    observations: list[tuple[int, Decimal]] = []
    for day in range(40):
        effect = Decimal("2") if day % 20 < 11 else Decimal("-1.5")
        for _ in range(10):
            observations.append((BASE_MS + day * DAY_MS, effect))

    values = [value for _, value in observations]
    mean, sd = v.metrics.mean_std(values)
    naive_t = float(mean) / (float(sd) / len(values) ** 0.5)
    block_t = v.block_bootstrap_t(observations, draws=1500)

    assert block_t is not None
    assert naive_t > 4.0            # what pretending they are independent buys you
    assert block_t < naive_t / 2    # what honesty costs
    assert block_t < v.Z_NAIVE      # and it is no longer significant at all


def test_an_unmeasurable_bootstrap_makes_the_criterion_underpowered_not_absent(tmp_db):
    returns = [Decimal("0.5")] * 10
    criteria, _ = v.statistical_verdict(
        returns, trials=1, timestamps=[BASE_MS + i * 1000 for i in range(10)],
        include_pbo=False,
    )
    boot = next(c for c in criteria if c.name == "block_bootstrap_t")
    assert boot.verdict is Verdict.UNDERPOWERED
    assert "fail towards confidence" in boot.basis


# ======================================================================= the six gates


def test_gate_zero_passes_its_measurable_checks_on_clean_data(tmp_db):
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, created_ms, first_seen_ms) VALUES (?,?,?,?)",
        ("sol", SOL_TOKEN, BASE_MS - DAY_MS, BASE_MS),
    )
    insert_swap(tmp_db, WALLET_A, token=SOL_TOKEN, side="buy", ts_ms=BASE_MS,
                amount_token=1, amount_native=1, tx="tx1")
    insert_signal(tmp_db, "s1", created_ms=BASE_MS - 1000)
    did = insert_decision(tmp_db, "d1", ts_ms=BASE_MS, signals='["s1"]')
    insert_trade(tmp_db, "t1", decision_id=did, opened_ms=BASE_MS + 1000,
                 closed_ms=BASE_MS + 60_000)
    report = v.gate_data_integrity(tmp_db)
    assert report.verdict is Verdict.PASS
    assert report.expires_ms is not None


def test_gate_zero_catches_a_decision_citing_a_signal_from_its_own_future(tmp_db):
    """The automated leakage test. A feature the decision did not have is look-ahead."""
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, created_ms, first_seen_ms) VALUES (?,?,?,?)",
        ("sol", SOL_TOKEN, BASE_MS - DAY_MS, BASE_MS),
    )
    insert_swap(tmp_db, WALLET_A, token=SOL_TOKEN, side="buy", ts_ms=BASE_MS,
                amount_token=1, amount_native=1, tx="tx1")
    insert_signal(tmp_db, "s_future", created_ms=BASE_MS + 60_000)
    insert_decision(tmp_db, "d1", ts_ms=BASE_MS, signals='["s_future"]')
    report = v.gate_data_integrity(tmp_db)
    leak = next(c for c in report.criteria if c.name == "lookahead_decisions")
    assert leak.verdict is Verdict.FAIL
    assert report.verdict is Verdict.FAIL


def test_gate_zero_is_underpowered_not_passed_on_an_empty_database(tmp_db):
    report = v.gate_data_integrity(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED
    assert report.expires_ms is None
    assert set(report.unmeasured)


def test_gate_zero_fails_a_survivorship_pruned_universe(tmp_db):
    """A universe built from tokens that still have a pool has deleted the failure mass."""
    for i in range(10):
        tmp_db.execute(
            "INSERT INTO tokens (chain, address, created_ms, first_seen_ms) VALUES (?,?,?,?)",
            ("sol", f"listing_only_{i}", None, BASE_MS),
        )
    report = v.gate_data_integrity(tmp_db)
    universe = next(c for c in report.criteria if c.name == "token_universe_onchain_share")
    assert universe.verdict is Verdict.FAIL


def test_gate_one_is_underpowered_with_no_trades_rather_than_clean(tmp_db):
    report = v.gate_execution_realism(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED
    assert "slippage_recorded_share" in report.unmeasured


def test_gate_one_fails_fantasy_fills(tmp_db):
    """Filling at the signal tick's price with no fee is not a cheap trade, it is no trade."""
    for i in range(10):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * DAY_MS, fees=0,
                     slippage_bps=None)
    report = v.gate_execution_realism(tmp_db)
    assert report.verdict is Verdict.FAIL
    assert "slippage_recorded_share" in report.failures
    assert "fee_recorded_share" in report.failures


def test_gate_one_fails_when_the_edge_does_not_clear_twice_the_friction(tmp_db):
    # cost 1000, pnl 5 (0.5%), fees 12 plus 40bps slippage = 0.52% friction. Edge < 2x.
    for i in range(10):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * DAY_MS, cost=1000, pnl=5,
                     fees=12, slippage_bps=40)
    report = v.gate_execution_realism(tmp_db)
    ratio = next(c for c in report.criteria if c.name == "edge_over_modelled_friction")
    assert ratio.verdict is Verdict.FAIL
    assert ratio.value is not None and ratio.value < 2.0


def test_gate_two_is_underpowered_on_a_thin_sample(tmp_db):
    for i in range(20):
        insert_trade(tmp_db, f"t{i}", closed_ms=BASE_MS + i * DAY_MS, pnl=50)
    report = v.gate_statistics(tmp_db)
    assert report.verdict is not Verdict.PASS
    sample = next(c for c in report.criteria if c.name == "closed_trades")
    assert sample.verdict is Verdict.FAIL  # 20 against a threshold of 1,000


def test_gate_two_reports_pbo_as_unevaluable_rather_than_satisfied(tmp_db):
    report = v.gate_statistics(tmp_db)
    pbo = next(c for c in report.criteria if c.name == "pbo_cscv")
    assert pbo.verdict is Verdict.UNDERPOWERED
    assert "unevaluable, not satisfied" in pbo.basis


def test_gate_two_requires_the_harness_to_be_able_to_say_no(tmp_db):
    report = v.gate_statistics(tmp_db)
    check = next(c for c in report.criteria if c.name == "harness_can_return_no")
    assert check.verdict is Verdict.PASS


def test_gate_three_is_underpowered_when_no_cohort_was_ever_frozen(tmp_db):
    report = v.gate_shadow(tmp_db)
    arm = next(c for c in report.criteria if c.name == "wallet_grading_control_arm")
    assert arm.verdict is Verdict.UNDERPOWERED
    assert "has not been started" in arm.basis


def test_gate_three_does_not_freeze_a_cohort_as_a_side_effect(tmp_db):
    """A gate evaluates evidence; it does not mint it. Freezing is a dated decision."""
    v.gate_shadow(tmp_db)
    rows = tmp_db.execute("SELECT COUNT(*) FROM wallet_cohort_freezes").fetchone()[0]
    assert rows == 0


def test_gate_three_checks_sign_stability_across_weeks(tmp_db):
    """One huge week and five flat ones is not an edge, it is an event."""
    insert_trade(tmp_db, "big", closed_ms=BASE_MS, pnl=100_000)
    for i in range(1, 6):
        insert_trade(tmp_db, f"w{i}", closed_ms=BASE_MS + i * WEEK_MS, pnl=-10)
    report = v.gate_shadow(tmp_db)
    share = next(c for c in report.criteria if c.name == "max_single_week_share")
    positive = next(c for c in report.criteria if c.name == "positive_week_share")
    assert share.verdict is Verdict.FAIL
    assert positive.verdict is Verdict.FAIL


def test_gate_four_is_underpowered_with_no_live_trades(tmp_db):
    report = v.gate_micro_live(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED
    assert "live_trades" in report.unmeasured


def test_gate_four_applies_the_tail_rule_to_live_money_too(tmp_db):
    insert_trade(tmp_db, "win", mode="live", closed_ms=BASE_MS, pnl=10_000)
    for i in range(1, 30):
        insert_trade(tmp_db, f"l{i}", mode="live", closed_ms=BASE_MS + i * DAY_MS, pnl=-500)
    report = v.gate_micro_live(tmp_db)
    tail = next(
        c for c in report.criteria if c.name == "live_positive_after_deleting_best_5pct"
    )
    assert tail.verdict is Verdict.FAIL


def test_gate_five_is_underpowered_without_a_size_step_ledger(tmp_db):
    report = v.gate_scale(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED


def test_gate_five_fails_a_size_jump_over_one_and_a_half_times(tmp_db):
    insert_trade(tmp_db, "small", mode="live", closed_ms=BASE_MS, cost=1000)
    insert_trade(tmp_db, "huge", mode="live", closed_ms=BASE_MS + DAY_MS, cost=10_000)
    report = v.gate_scale(tmp_db)
    step = next(c for c in report.criteria if c.name == "max_size_step_multiple")
    assert step.verdict is Verdict.FAIL
    assert step.value == pytest.approx(10.0)


# ============================================================ ordering, expiry, protocol


def test_the_gates_are_strictly_ordered_and_later_ones_are_blocked_not_passed(tmp_db):
    report = v.run_gates(tmp_db)
    assert [g.gate for g in report.gates] == list(v.GATE_NAMES)
    assert report.gates[0].verdict is not Verdict.PASS
    assert all(g.verdict is Verdict.BLOCKED for g in report.gates[1:])
    assert not any(g.verdict is Verdict.PASS for g in report.gates[1:])


def test_a_blocked_gate_never_carries_an_expiry(tmp_db):
    report = v.run_gates(tmp_db)
    for gate in report.gates:
        if gate.verdict is not Verdict.PASS:
            assert gate.expires_ms is None


def test_the_protocol_verdict_is_underpowered_on_an_empty_database(tmp_db):
    report = v.run_gates(tmp_db)
    assert report.verdict is Verdict.UNDERPOWERED
    assert report.expires_ms is None


def test_a_real_failure_makes_the_protocol_verdict_fail_not_underpowered(tmp_db):
    insert_signal(tmp_db, "s_future", created_ms=BASE_MS + 60_000)
    insert_decision(tmp_db, "d1", ts_ms=BASE_MS, signals='["s_future"]')
    report = v.run_gates(tmp_db)
    assert report.verdict is Verdict.FAIL


def test_a_pass_expires_after_one_regime(tmp_db):
    assert v.GATE_TTL_MS == int(v.REGIME_WEEKS * WEEK_MS)
    assert v.is_expired(None) is True                       # never a pass
    assert v.is_expired(BASE_MS, at_ms=BASE_MS - 1) is False
    assert v.is_expired(BASE_MS, at_ms=BASE_MS) is True
    assert v.is_expired(BASE_MS, at_ms=BASE_MS + 1) is True


def test_an_expired_pass_stops_counting_as_a_pass(tmp_db):
    tmp_db.execute(
        "INSERT INTO validation_gates (run_id, ordinal, gate, verdict, sample_n, created_ms, "
        "expires_ms) VALUES (?,?,?,?,?,?,?)",
        ("run1", 0, "data-integrity", "pass", 10, BASE_MS, BASE_MS + v.GATE_TTL_MS),
    )
    fresh = v.standing_verdict(tmp_db, gate="data-integrity", at_ms=BASE_MS + DAY_MS)
    stale = v.standing_verdict(tmp_db, gate="data-integrity",
                               at_ms=BASE_MS + v.GATE_TTL_MS + DAY_MS)
    assert fresh is Verdict.PASS
    assert stale is Verdict.UNDERPOWERED


def test_a_gate_nobody_has_run_is_underpowered(tmp_db):
    assert v.standing_verdict(tmp_db, gate="statistics") is Verdict.UNDERPOWERED


def test_the_protocol_persists_every_gate_including_the_blocked_ones(tmp_db):
    report = v.run_gates(tmp_db, record=True)
    rows = tmp_db.execute(
        "SELECT gate, verdict FROM validation_gates WHERE run_id = ? ORDER BY ordinal",
        (report.run_id,),
    ).fetchall()
    assert len(rows) == 6
    assert [r[1] for r in rows].count("blocked") == 5


# ====================================================== the matched-control arm (wallets)


def test_freezing_with_no_graded_wallets_is_an_empty_cohort_not_a_crash(tmp_db):
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS)
    assert summary.graded_n == 0
    assert summary.control_n == 0
    assert any("cohort is empty" in n for n in summary.notes)


def test_a_frozen_cohort_matches_one_to_one_on_the_declared_covariates(tmp_db):
    for i in range(6):
        insert_wallet_score(tmp_db, f"graded{i}", grade="A", closed_trades=20,
                            distinct_tokens=10)
    for i in range(6):
        insert_wallet_score(tmp_db, f"plain{i}", grade="C", closed_trades=20,
                            distinct_tokens=10)
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS + DAY_MS)
    assert summary.graded_n == 6
    assert summary.control_n == 6
    assert set(summary.matched_on) == set(v.MATCH_COVARIATES)


def test_a_control_that_cannot_be_matched_inside_the_calliper_is_dropped(tmp_db):
    """Widening the calliper to keep a pair would let the arms differ on the confounder."""
    insert_wallet_score(tmp_db, "graded0", grade="A", closed_trades=500, distinct_tokens=200)
    for i in range(5):
        insert_wallet_score(tmp_db, f"tiny{i}", grade="D", closed_trades=1, distinct_tokens=1)
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS + DAY_MS)
    assert summary.graded_n == 0
    assert any("no control inside" in n for n in summary.notes)


def test_the_freeze_records_what_it_could_not_match_on(tmp_db):
    """An undeclared confounder makes a matched study an unmatched one with arithmetic."""
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS)
    assert len(summary.unmatched) >= 5
    joined = " ".join(summary.unmatched)
    for expected in ("capital", "operator identity", "unrealised", "off-chain"):
        assert expected in joined


def test_the_freeze_says_the_control_is_selected_on_not_being_graded(tmp_db):
    for i in range(4):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS + DAY_MS)
    assert any("complement of the graded set" in n for n in summary.notes)


def test_membership_is_written_down_at_t_and_not_recomputed(tmp_db):
    """Recomputing membership at read time is the ex-post contamination 2602.14860 warns of."""
    for i in range(4):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS + DAY_MS)
    rows = tmp_db.execute(
        "SELECT arm, COUNT(*) FROM wallet_cohorts WHERE cohort_id = ? GROUP BY arm",
        (summary.cohort_id,),
    ).fetchall()
    assert dict(rows) == {"graded": 4, "control": 4}


def test_a_wallet_scored_after_t_is_not_in_a_cohort_frozen_at_t(tmp_db):
    insert_wallet_score(tmp_db, "early", grade="A", scored_at_ms=BASE_MS - DAY_MS)
    insert_wallet_score(tmp_db, "later", grade="A", scored_at_ms=BASE_MS + 10 * DAY_MS)
    insert_wallet_score(tmp_db, "ctrl", grade="C", scored_at_ms=BASE_MS - DAY_MS)
    summary = v.freeze_cohort(tmp_db, as_of_ms=BASE_MS)
    addresses = {
        r[0] for r in tmp_db.execute(
            "SELECT address FROM wallet_cohorts WHERE cohort_id = ?", (summary.cohort_id,)
        ).fetchall()
    }
    assert "later" not in addresses
    assert "early" in addresses


def test_a_tiny_cohort_is_underpowered_rather_than_a_verdict_on_grading(tmp_db):
    for i in range(3):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    report = v.control_arm(tmp_db, as_of_ms=BASE_MS + DAY_MS)
    assert report.verdict is Verdict.UNDERPOWERED
    assert str(v.CONTROL_MIN_ARM) in " ".join(report.notes)


def test_the_control_arm_on_an_unknown_cohort_refuses_rather_than_invents(tmp_db):
    report = v.control_arm(tmp_db, cohort_id="cohort_nope")
    assert report.verdict is Verdict.UNDERPOWERED
    assert any("never frozen" in n for n in report.notes)


def test_a_graded_cohort_that_loses_to_its_control_is_a_failure_not_an_absence(tmp_db):
    """A real negative. Per the protocol this takes the grader off the entry path."""
    t = BASE_MS + DAY_MS
    for i in range(40):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=t)
    assert summary.graded_n == 40
    for i in range(40):
        # graded wallets lose 40% forward; controls gain 40%.
        round_trip(tmp_db, f"g{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=600, tag=f"g{i}")
        round_trip(tmp_db, f"c{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=1400, tag=f"c{i}")
    report = v.control_arm(tmp_db, cohort_id=summary.cohort_id)
    assert report.verdict is Verdict.FAIL
    assert report.difference is not None and report.difference < 0
    assert any("entry path" in n for n in report.notes)


def test_a_graded_cohort_that_clearly_wins_passes_at_p_below_one_percent(tmp_db):
    t = BASE_MS + DAY_MS
    for i in range(40):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=t)
    for i in range(40):
        round_trip(tmp_db, f"g{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=1500 + i, tag=f"g{i}")
        round_trip(tmp_db, f"c{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=700 + i, tag=f"c{i}")
    report = v.control_arm(tmp_db, cohort_id=summary.cohort_id)
    assert report.verdict is Verdict.PASS
    assert report.p_value is not None and report.p_value < 0.01


def test_forward_edge_ignores_trading_from_before_the_freeze(tmp_db):
    """If the window leaked backwards the test would be scoring the selection criterion."""
    t = BASE_MS + 10 * DAY_MS
    for i in range(40):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=t)
    for i in range(40):
        # Spectacular history, before t. It must not count.
        round_trip(tmp_db, f"g{i}", token=SOL_TOKEN, ts_ms=t - 5 * DAY_MS, cost=1000,
                   proceeds=50_000, tag=f"hist{i}")
    report = v.control_arm(tmp_db, cohort_id=summary.cohort_id)
    assert report.graded_observed == 0
    assert report.verdict is Verdict.UNDERPOWERED


def test_differential_attrition_is_reported_rather_than_averaged_away(tmp_db):
    t = BASE_MS + DAY_MS
    for i in range(40):
        insert_wallet_score(tmp_db, f"g{i}", grade="A")
        insert_wallet_score(tmp_db, f"c{i}", grade="C")
    summary = v.freeze_cohort(tmp_db, as_of_ms=t)
    for i in range(40):
        round_trip(tmp_db, f"g{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=1100, tag=f"g{i}")
    for i in range(4):  # only a tenth of the control arm keeps trading
        round_trip(tmp_db, f"c{i}", token=SOL_TOKEN, ts_ms=t + DAY_MS, cost=1000,
                   proceeds=1100, tag=f"c{i}")
    report = v.control_arm(tmp_db, cohort_id=summary.cohort_id)
    assert report.attrition_gap is not None and report.attrition_gap > 0.10
    assert any("attrition" in n for n in report.notes)


def test_the_minimum_detectable_effect_is_reported_for_a_near_miss():
    treated = [Decimal("0.10")] * 30
    control = [Decimal("0.09")] * 30
    # Both arms are flat, so nothing is detectable: refuse rather than report p.
    assert v.minimum_detectable_effect(treated, control) is None
    noisy_t = [Decimal(str(0.1 + (i % 7) * 0.01)) for i in range(40)]
    noisy_c = [Decimal(str(0.09 + (i % 7) * 0.01)) for i in range(40)]
    mde = v.minimum_detectable_effect(noisy_t, noisy_c)
    assert mde is not None and mde > 0


def test_the_permutation_test_is_deterministic():
    a = [Decimal(str(i)) for i in range(30)]
    b = [Decimal(str(i - 5)) for i in range(30)]
    assert v.permutation_p(a, b, draws=500) == v.permutation_p(a, b, draws=500)


def test_the_permutation_test_refuses_a_one_sided_cohort():
    assert v.permutation_p([Decimal(1)], [Decimal(2), Decimal(3)]) is None


# ============================================================== falsification: say "no"
#
# The most important block in the file. Everything above only matters if these hold.


def test_the_harness_can_return_a_negative_on_a_placebo():
    report = v.falsification_suite(trials=2)
    assert report.harness_can_say_no is True
    assert report.placebo_random_verdict is not Verdict.PASS


def test_a_random_entry_placebo_fails_the_statistical_core():
    """If a zero-edge series clears the gate, the gate is broken, not the market."""
    placebo = v.synthetic_returns(2400, edge=False)
    criteria, _ = v.statistical_verdict(
        placebo, trials=2, timestamps=v._synthetic_stamps(2400), include_pbo=False,
        bootstrap_draws=400,
    )
    assert v.combine(criteria) is not Verdict.PASS


def test_the_placebo_has_a_negative_expectancy_by_construction():
    """The null is the fee-adjusted one. "Beats zero" is the wrong bar in this market."""
    placebo = v.synthetic_returns(4000, edge=False)
    assert sum(placebo) < 0


def test_the_harness_can_also_return_a_positive_or_its_negatives_are_worthless():
    report = v.falsification_suite(trials=2)
    assert report.harness_can_say_yes is True
    assert report.positive_control_verdict is Verdict.PASS


def test_the_falsification_verdict_needs_both_directions():
    report = v.falsification_suite(trials=2)
    assert report.verdict is Verdict.PASS
    assert "Falsification OK" in report.headline


def test_selection_alone_does_not_manufacture_a_pass_here():
    report = v.falsification_suite(trials=2)
    assert report.placebo_selection_hit_rate == 0.0
    assert report.placebo_shuffled_verdict is Verdict.PASS


def test_falsification_is_reproducible():
    """A validation result that moves when you rerun it is not a validation result."""
    first = v.falsification_suite(trials=2, seed=7, attempts=3)
    second = v.falsification_suite(trials=2, seed=7, attempts=3)
    assert first.model_dump() == second.model_dump()


def test_a_different_seed_does_not_change_the_conclusion():
    for seed in (1, 99, 12345):
        report = v.falsification_suite(trials=2, seed=seed, attempts=3)
        assert report.harness_can_say_no is True
        assert report.harness_can_say_yes is True


def test_the_synthetic_edge_is_deliberately_not_tail_dependent():
    """A fat-tailed true edge could not clear the delete-5% rule, so the control is clean."""
    control = v.synthetic_returns(2000, edge=True)
    assert sum(control) > 0
    assert sum(v.delete_best(control)) > 0


def test_a_fat_tailed_true_edge_would_correctly_fail_the_tail_rule():
    """Not a bug in the rule: profile C's whole expectancy lives in its top 2%."""
    fat = v.synthetic_returns(4000, edge=False, mean_shift=0.30)
    assert sum(fat) > 0                      # genuinely profitable in aggregate
    assert sum(v.delete_best(fat)) < sum(fat)


def test_the_placebo_runs_through_the_same_code_as_the_real_gate(tmp_db):
    """A negative produced by a different code path proves nothing about the real one."""
    import inspect

    source = inspect.getsource(v.falsification_suite)
    assert "statistical_verdict" in source
    gate_source = inspect.getsource(v.gate_statistics)
    assert "statistical_verdict" in gate_source


# ================================================================================= CLI


def test_the_validate_commands_are_registered():
    from typer.testing import CliRunner

    from kaiba.cli.main import app

    result = CliRunner().invoke(app, ["validate", "--help"])
    assert result.exit_code == 0
    for command in ("power", "freeze", "control", "gates", "falsify", "status"):
        assert command in result.stdout


def test_the_power_command_runs_and_says_underpowered(tmp_db, monkeypatch):
    from typer.testing import CliRunner

    from kaiba.cli.main import app
    from kaiba.core import db

    monkeypatch.setattr(db, "ensure_db", lambda path=None: tmp_db)
    result = CliRunner().invoke(app, ["validate", "power", "--json"])
    assert result.exit_code == 0
    assert "underpowered" in result.stdout
