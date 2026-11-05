"""Tests for the confluence scorer.

The tests that matter most here are not the arithmetic ones. They are:

* :func:`test_no_weight_lacks_provenance` and
  :func:`test_invented_weights_say_so` — the control that stops a second lane of unmarked
  guesses entering the system.
* :func:`test_unknown_scores_below_checked_and_clear` — the contract rule that missing data
  must never read as safe, tested directly rather than assumed.
* :func:`test_correlated_group_contributes_what_one_member_would` — the crux. A naive
  scorer double-counts correlated evidence, and this is what proves ours does not.
* :func:`test_sql_seed_matches_python_weight_set` — two copies of every weight exist (the
  migration and the in-process fallback) and this is what stops them drifting.
"""

from __future__ import annotations

import json
import math

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis, now_ms
from kaiba.intelligence import confluence as cf

SOL = Chain.SOL
MINT = "So11111111111111111111111111111111111111112"
OTHER = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CREATOR = "CreaTor1111111111111111111111111111111111111"


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def obs(**values: cf.Observation) -> cf.Observations:
    return cf.Observations(chain=SOL, token=MINT, as_of_ms=1_700_000_000_000, values=values)


def known(value: float, support: float | None = None, **kw: object) -> cf.Observation:
    return cf.Observation(value=value, basis=EvidenceBasis.DERIVED, support=support, **kw)  # type: ignore[arg-type]


def all_evidenced(signed: float) -> cf.Observations:
    """Every evidenced scoring variable present, each mapped to roughly ``signed``."""
    velocity = cf.VELOCITY_MID_SOL_PER_SWAP * (10 ** (signed * math.log10(8.5 / cf.VELOCITY_MID_SOL_PER_SWAP)))
    if signed < 0:
        velocity = cf.VELOCITY_MID_SOL_PER_SWAP * (
            10 ** (signed * abs(math.log10(0.085 / cf.VELOCITY_MID_SOL_PER_SWAP)))
        )
    bundle_pp = cf.BUNDLE_DELTA_GOOD_PP + (1.0 - signed) / 2.0 * (
        cf.BUNDLE_DELTA_BAD_PP - cf.BUNDLE_DELTA_GOOD_PP
    )
    return obs(
        copycat_reuse=known(signed if signed else 0.0),
        creator_graduation_history=known(cf.KAIBA_CREATOR_POPULATION_GRADUATION, support=200.0),
        curve_velocity_sol_per_swap=known(velocity, support=50.0),
        wash_trading=known(0.0 if signed <= 0 else 32.0, support=100.0),
        bot_dominated_early_activity=known((1.0 - signed) / 2.0, support=100.0),
        bundle_adjusted_concentration=known(bundle_pp, support=50.0),
    )


def neutral_full_coverage() -> cf.Observations:
    """Every evidenced variable known and reading neutral: checked, and nothing adverse."""
    return obs(
        copycat_reuse=known(0.0),
        creator_graduation_history=known(cf.KAIBA_CREATOR_POPULATION_GRADUATION, support=500.0),
        curve_velocity_sol_per_swap=known(cf.VELOCITY_MID_SOL_PER_SWAP, support=50.0),
        # One wash transaction is the smallest positive reading; absence would be -1.
        wash_trading=known(1.0, support=100.0),
        bot_dominated_early_activity=known(0.5, support=100.0),
        bundle_adjusted_concentration=known(
            (cf.BUNDLE_DELTA_GOOD_PP + cf.BUNDLE_DELTA_BAD_PP) / 2.0, support=50.0
        ),
    )


# --------------------------------------------------------------------------------------
# provenance: the control that matters most
# --------------------------------------------------------------------------------------


def test_no_weight_lacks_provenance():
    for spec in cf.LITERATURE_V1.variables:
        assert spec.provenance.strip(), f"{spec.name} has no provenance"
        assert len(spec.provenance) > 80, f"{spec.name} provenance is too thin to be a source"


def test_invented_weights_say_so_and_evidenced_ones_cite_a_source():
    for spec in cf.LITERATURE_V1.variables:
        if spec.grade is cf.EvidenceGrade.INVENTED:
            assert spec.provenance.startswith("INVENTED"), spec.name
        else:
            assert "INVENTED" not in spec.provenance, spec.name
            assert any(m in spec.provenance for m in cf.CITATION_MARKERS), spec.name


def test_literature_weight_set_validates_clean():
    assert cf.validate_weight_set(cf.LITERATURE_V1) == []


def test_validator_catches_an_unannotated_weight():
    bad = cf.WeightSet(
        version="bad",
        variables=(
            cf.VariableSpec(
                name="mystery",
                weight_nats=1.0,
                grade=cf.EvidenceGrade.MEASURED,
                provenance="",
                mapping="signed",
                direction=cf.Direction.SIGNED,
            ),
        ),
    )
    problems = cf.validate_weight_set(bad)
    assert any("no provenance" in p for p in problems)


def test_validator_catches_an_invented_weight_pretending_to_be_measured():
    sneaky = cf.WeightSet(
        version="sneaky",
        variables=(
            cf.VariableSpec(
                name="hunch",
                weight_nats=1.0,
                grade=cf.EvidenceGrade.MEASURED,
                provenance="it felt about right when we tried it on a few tokens",
                mapping="signed",
                direction=cf.Direction.SIGNED,
            ),
        ),
    )
    assert any("cites no source" in p for p in cf.validate_weight_set(sneaky))


def test_validator_catches_an_unexplained_discount():
    spec = cf.LITERATURE_V1.by_name()["creator_graduation_history"]
    stripped = cf.WeightSet(version="x", variables=(cf.VariableSpec(
        name=spec.name, weight_nats=spec.weight_nats, grade=spec.grade, provenance=spec.provenance,
        mapping=spec.mapping, direction=spec.direction, params=spec.params,
        contribution_factor=0.45, factor_reason=None,
    ),))
    assert any("no stated reason" in p for p in cf.validate_weight_set(stripped))


def test_every_mapping_named_by_a_weight_exists():
    for spec in cf.LITERATURE_V1.variables:
        assert spec.mapping in cf.MAPPINGS, spec.name


# --------------------------------------------------------------------------------------
# the weights themselves
# --------------------------------------------------------------------------------------


def test_copycat_weight_is_half_the_published_log_odds_separation():
    spec = cf.LITERATURE_V1.by_name()["copycat_reuse"]
    expected = (math.log(0.0920 / 0.9080) - math.log(0.0086 / 0.9914)) / 2.0
    assert spec.weight_nats == pytest.approx(expected, rel=1e-12)
    assert spec.weight_nats == pytest.approx(1.22895, abs=1e-5)


def test_wash_weight_is_half_the_published_log_odds_separation_and_positive():
    spec = cf.LITERATURE_V1.by_name()["wash_trading"]
    expected = (math.log(0.020 / 0.980) - math.log(0.0090 / 0.9910)) / 2.0
    assert spec.weight_nats == pytest.approx(expected, rel=1e-12)
    # The sign check that matters: more wash trading must raise the score, not lower it.
    high = cf.score(obs(wash_trading=known(64.0, support=500.0)))
    none = cf.score(obs(wash_trading=known(0.0, support=500.0)))
    assert high.by_name()["wash_trading"].contribution_nats > 0
    assert none.by_name()["wash_trading"].contribution_nats < 0


def test_raw_top10_concentration_carries_no_weight():
    spec = cf.LITERATURE_V1.by_name()["raw_top10_concentration"]
    assert spec.weight_nats == 0.0
    assert "no published study establishes" in spec.provenance
    result = cf.score(obs(raw_top10_concentration=known(97.0)))
    assert result.by_name()["raw_top10_concentration"].contribution_nats == 0.0
    assert result.score_evidenced_nats == pytest.approx(-cf.UNKNOWN_PENALTY_CAP_NATS)


@pytest.mark.parametrize(
    "name", ["dev_bought_own_bundle", "freeze_authority_live", "wallet_pnl_grade", "sniper_exposure"]
)
def test_published_nulls_carry_no_weight(name: str):
    spec = cf.LITERATURE_V1.by_name()[name]
    assert spec.weight_nats == 0.0
    assert spec.mapping == "report_only"


def test_rule_u_weight_is_the_floor_of_the_measured_set_not_its_mean():
    floor = min(cf.MEASURED_SEPARATIONS_NATS.values()) / 2.0
    mean = sum(cf.MEASURED_SEPARATIONS_NATS.values()) / len(cf.MEASURED_SEPARATIONS_NATS) / 2.0
    assert cf.RULE_U_WEIGHT_NATS == pytest.approx(floor)
    assert cf.RULE_U_WEIGHT_NATS < mean
    for name in (
        "creator_graduation_history",
        "curve_velocity_sol_per_swap",
        "bot_dominated_early_activity",
        "bundle_adjusted_concentration",
    ):
        assert cf.LITERATURE_V1.by_name()[name].weight_nats == pytest.approx(cf.RULE_U_WEIGHT_NATS)


def test_invented_thresholds_still_mirror_dyor():
    """If dyor moves a threshold, this file is stating something that is no longer true."""
    from kaiba.intelligence import dyor

    assert float(dyor.TOP10_PCT_WARN) == cf.DYOR_TOP10_PCT_WARN
    assert float(dyor.DEV_PCT_BLOCK) == cf.DYOR_DEV_PCT_BLOCK
    assert float(dyor.BUNDLER_PCT_WARN) == cf.DYOR_BUNDLER_PCT_WARN
    assert float(dyor.SNIPER_PCT_WARN) == cf.DYOR_SNIPER_PCT_WARN
    assert float(dyor.LOW_LIQUIDITY_USD) == cf.DYOR_LOW_LIQUIDITY_USD


def test_velocity_mapping_anchors_are_the_published_quotients():
    assert cf.VELOCITY_MID_SOL_PER_SWAP == pytest.approx(85.0 / 457.0)
    assert cf.VELOCITY_HI_SOL_PER_SWAP == pytest.approx(8.5)
    assert cf.VELOCITY_LO_SOL_PER_SWAP == pytest.approx(0.085)
    spec = cf.LITERATURE_V1.by_name()["curve_velocity_sol_per_swap"]
    at_mid = cf.MAPPINGS["log_band"](known(cf.VELOCITY_MID_SOL_PER_SWAP), spec.params)
    at_hi = cf.MAPPINGS["log_band"](known(8.5), spec.params)
    at_lo = cf.MAPPINGS["log_band"](known(0.085), spec.params)
    assert at_mid == pytest.approx(0.0, abs=1e-12)
    assert at_hi == pytest.approx(1.0)
    assert at_lo == pytest.approx(-1.0)


# --------------------------------------------------------------------------------------
# unknown is never good
# --------------------------------------------------------------------------------------


def test_unknown_scores_below_checked_and_clear():
    nothing = cf.score(obs())
    checked = cf.score(neutral_full_coverage())
    assert nothing.coverage == 0.0
    assert checked.coverage == pytest.approx(1.0)
    assert nothing.score_evidenced_nats < checked.score_evidenced_nats
    assert nothing.score_evidenced_nats == pytest.approx(-cf.UNKNOWN_PENALTY_CAP_NATS)


def test_unknown_scores_below_a_good_reading_too():
    nothing = cf.score(obs())
    good = cf.score(all_evidenced(1.0))
    assert nothing.score_evidenced_nats < good.score_evidenced_nats


def test_total_ignorance_is_not_worse_than_a_confirmed_copycat():
    """The penalty is bounded on purpose: not scanning must not beat proving it is a copy."""
    nothing = cf.score(obs())
    copycat = cf.score(obs(copycat_reuse=known(-1.0)))
    assert copycat.score_evidenced_nats < nothing.score_evidenced_nats


def test_partial_coverage_is_charged_proportionally():
    half = cf.score(
        obs(
            copycat_reuse=known(1.0),
            creator_graduation_history=known(cf.KAIBA_CREATOR_POPULATION_GRADUATION, support=100.0),
        )
    )
    assert 0.0 < half.coverage < 1.0
    expected = cf.UNKNOWN_PENALTY_CAP_NATS * (1.0 - half.coverage)
    assert half.unknown_penalty_nats == pytest.approx(expected)
    assert set(half.unknowns) == {
        "curve_velocity_sol_per_swap",
        "wash_trading",
        "bot_dominated_early_activity",
        "bundle_adjusted_concentration",
    }


def test_missing_data_is_none_and_unavailable_never_zero():
    result = cf.score(obs())
    for contribution in result.contributions:
        assert contribution.value is None
        assert contribution.basis is EvidenceBasis.UNAVAILABLE
        assert contribution.signed_score is None
        assert not contribution.known


def test_a_reading_that_cannot_support_a_score_counts_as_unknown():
    """Three swaps and no wash detected is a fact about our sample, not about the token."""
    thin = cf.score(obs(wash_trading=known(0.0, support=3.0)))
    assert "wash_trading" in thin.unknowns
    assert thin.by_name()["wash_trading"].known  # we did have a reading
    assert thin.by_name()["wash_trading"].signed_score is None  # it just cannot say anything

    thick = cf.score(obs(wash_trading=known(0.0, support=50.0)))
    assert "wash_trading" not in thick.unknowns
    assert thick.by_name()["wash_trading"].signed_score == pytest.approx(-1.0)


# --------------------------------------------------------------------------------------
# correlated inputs
# --------------------------------------------------------------------------------------


def test_correlated_group_contributes_what_one_member_would():
    """Three agreeing reads of the same swap stream are one opinion, not three."""
    result = cf.score(
        obs(
            curve_velocity_sol_per_swap=known(8.5, support=50.0),
            wash_trading=known(64.0, support=500.0),
            bot_dominated_early_activity=known(0.0, support=100.0),
        )
    )
    group = next(g for g in result.groups if g.group == cf.GROUP_EARLY_FLOW)
    assert group.raw_sum_nats == pytest.approx(3 * cf.RULE_U_WEIGHT_NATS, rel=1e-6)
    assert group.applied_nats == pytest.approx(cf.RULE_U_WEIGHT_NATS, rel=1e-6)
    assert group.discount_nats < 0
    members = sum(result.by_name()[m].contribution_nats for m in group.members)
    assert members == pytest.approx(cf.RULE_U_WEIGHT_NATS, rel=1e-6)


def test_creator_history_and_copycat_share_a_group():
    spec_a = cf.LITERATURE_V1.by_name()["copycat_reuse"]
    spec_b = cf.LITERATURE_V1.by_name()["creator_graduation_history"]
    assert spec_a.correlation_group == spec_b.correlation_group == cf.GROUP_CREATOR_IDENTITY
    result = cf.score(
        obs(copycat_reuse=known(1.0), creator_graduation_history=known(0.30, support=400.0))
    )
    group = next(g for g in result.groups if g.group == cf.GROUP_CREATOR_IDENTITY)
    # Both point the same way, so the group is capped at the copycat contribution alone.
    assert group.applied_nats == pytest.approx(spec_a.weight_nats, rel=1e-6)
    assert group.applied_nats < group.raw_sum_nats


def test_group_clipping_never_increases_a_magnitude():
    for velocity, wash, bots in ((8.5, 64.0, 0.0), (0.01, 0.0, 1.0), (8.5, 0.0, 1.0), (0.05, 64.0, 0.2)):
        result = cf.score(
            obs(
                curve_velocity_sol_per_swap=known(velocity, support=50.0),
                wash_trading=known(wash, support=500.0),
                bot_dominated_early_activity=known(bots, support=100.0),
            )
        )
        group = next(g for g in result.groups if g.group == cf.GROUP_EARLY_FLOW)
        assert abs(group.applied_nats) <= abs(group.raw_sum_nats) + 1e-12
        assert abs(group.applied_nats) <= group.cap_nats + 1e-12


def test_an_ungrouped_variable_is_not_discounted():
    assert cf.LITERATURE_V1.by_name()["liquidity_floor"].correlation_group is None
    result = cf.score(obs(liquidity_floor=known(500.0)))
    line = result.by_name()["liquidity_floor"]
    assert line.contribution_nats == pytest.approx(line.raw_contribution_nats)
    assert line.contribution_nats == pytest.approx(-cf.RULE_U_WEIGHT_NATS)


def test_group_rationale_is_stated_for_every_group():
    result = cf.score(all_evidenced(1.0))
    for group in result.groups:
        assert group.rationale, group.group
        assert "unmeasured" in group.rationale.lower() or "clipped" in group.rationale.lower()


def test_entity_collapse_is_reported_and_carries_no_weight():
    """Five addresses funded by one wallet are one opinion, and that count never scores."""
    spec = cf.LITERATURE_V1.by_name()["independent_entity_count"]
    assert spec.weight_nats == 0.0
    result = cf.score(obs(independent_entity_count=known(1.0, support=5.0)))
    line = result.by_name()["independent_entity_count"]
    assert line.value == 1.0 and line.support == 5.0
    assert line.contribution_nats == 0.0


# --------------------------------------------------------------------------------------
# evidenced versus everything
# --------------------------------------------------------------------------------------


def test_scores_are_identical_when_no_invented_variable_has_a_reading():
    result = cf.score(all_evidenced(1.0))
    assert result.invented_used == ()
    assert result.score_all_nats == pytest.approx(result.score_evidenced_nats)
    assert result.delta_invented_nats == pytest.approx(0.0)
    assert not result.divergent


def test_invented_variables_move_the_score_and_the_move_is_reported():
    base = all_evidenced(1.0)
    with_invented = (
        base.with_value("raw_top10_threshold", known(90.0))
        .with_value("bundler_exposure_threshold", known(50.0))
        .with_value("liquidity_floor", known(100.0))
    )
    result = cf.score(with_invented)
    assert set(result.invented_used) == {
        "raw_top10_threshold",
        "bundler_exposure_threshold",
        "liquidity_floor",
    }
    assert result.delta_invented_nats < 0
    assert result.divergent
    assert "DIVERGENT" in result.summary()


def test_the_unknown_penalty_is_identical_in_both_scores():
    """So the difference between them is the invented variables and nothing else."""
    result = cf.score(obs(raw_top10_threshold=known(90.0)))
    evidenced_only = cf.score(obs())
    assert result.unknown_penalty_nats == pytest.approx(evidenced_only.unknown_penalty_nats)
    assert result.delta_invented_nats == pytest.approx(-cf.RULE_U_WEIGHT_NATS)


def test_sign_flip_by_invented_variables_is_flagged():
    # Everything checked, everything neutral except one wash transaction: a faintly
    # positive but entirely real evidenced reading.
    base = obs(
        copycat_reuse=known(0.0),
        creator_graduation_history=known(cf.KAIBA_CREATOR_POPULATION_GRADUATION, support=200.0),
        curve_velocity_sol_per_swap=known(cf.VELOCITY_MID_SOL_PER_SWAP, support=50.0),
        wash_trading=known(1.0, support=100.0),
        bot_dominated_early_activity=known(0.5, support=100.0),
        bundle_adjusted_concentration=known(15.0, support=50.0),
    )
    flipped = (
        base.with_value("raw_top10_threshold", known(90.0))
        .with_value("dev_supply_threshold", known(50.0))
        .with_value("liquidity_floor", known(10.0))
    )
    result = cf.score(flipped)
    assert result.score_evidenced_nats > 0 > result.score_all_nats
    assert result.sign_flipped
    assert "SIGN FLIPPED" in result.summary()


def test_evidenced_pass_never_sees_an_invented_weight():
    evidenced = cf.LITERATURE_V1.subset(evidenced_only=True)
    assert all(s.grade is not cf.EvidenceGrade.INVENTED for s in evidenced)
    assert {s.name for s in cf.LITERATURE_V1.variables} - {s.name for s in evidenced} == {
        "raw_top10_threshold",
        "dev_supply_threshold",
        "bundler_exposure_threshold",
        "sniper_exposure_threshold",
        "liquidity_floor",
    }


# --------------------------------------------------------------------------------------
# mappings
# --------------------------------------------------------------------------------------


def test_creator_history_is_shrunk_towards_the_population():
    """One launch that graduated is not the same evidence as two hundred that did."""
    thin = cf.score(obs(creator_graduation_history=known(1.0, support=1.0)))
    thick = cf.score(obs(creator_graduation_history=known(1.0, support=400.0)))
    thin_score = thin.by_name()["creator_graduation_history"].signed_score
    thick_score = thick.by_name()["creator_graduation_history"].signed_score
    assert 0.0 < thin_score < thick_score


def test_creator_contribution_carries_the_measured_defeat_discount():
    result = cf.score(obs(creator_graduation_history=known(0.40, support=400.0)))
    line = result.by_name()["creator_graduation_history"]
    assert line.contribution_factor == pytest.approx(0.45)
    assert line.factor_reason and "55%" in line.factor_reason
    expected = line.weight_nats * line.signed_score * 0.45
    assert line.raw_contribution_nats == pytest.approx(expected)


def test_funding_graph_resolution_removes_the_defeat_discount():
    resolved = cf.Observation(
        value=0.40, basis=EvidenceBasis.DERIVED, support=400.0, factor=1.0, detail="3-hop resolved"
    )
    result = cf.score(obs(creator_graduation_history=resolved))
    line = result.by_name()["creator_graduation_history"]
    assert line.contribution_factor == 1.0
    assert line.raw_contribution_nats == pytest.approx(line.weight_nats * line.signed_score)


def test_bundle_delta_maps_the_published_six_and_twenty_four_pp_anchors():
    good = cf.score(obs(bundle_adjusted_concentration=known(6.0)))
    bad = cf.score(obs(bundle_adjusted_concentration=known(24.0)))
    assert good.by_name()["bundle_adjusted_concentration"].signed_score == pytest.approx(1.0)
    assert bad.by_name()["bundle_adjusted_concentration"].signed_score == pytest.approx(-1.0)


def test_bot_share_maps_linearly_with_no_cutoff():
    spec = cf.LITERATURE_V1.by_name()["bot_dominated_early_activity"]
    for share, expected in ((0.0, 1.0), (0.25, 0.5), (0.5, 0.0), (0.75, -0.5), (1.0, -1.0)):
        got = cf.MAPPINGS["share_inverse"](known(share, support=100.0), spec.params)
        assert got == pytest.approx(expected)


def test_implied_probability_brackets_the_published_rates():
    best = cf.score(all_evidenced(1.0))
    worst = cf.score(all_evidenced(-1.0))
    assert best.implied_graduation_evidenced > cf.BASE_GRADUATION_RATE
    assert worst.implied_graduation_evidenced < cf.BASE_GRADUATION_RATE
    # Nothing in this model may claim more separation than the literature measured.
    assert best.score_evidenced_nats - worst.score_evidenced_nats < cf.COPYCAT_SEPARATION_NATS * 2


# --------------------------------------------------------------------------------------
# purity and replay
# --------------------------------------------------------------------------------------


def test_scoring_is_pure():
    observations = all_evidenced(1.0)
    first = cf.score(observations)
    second = cf.score(observations)
    assert first.to_row() == second.to_row()
    assert first.score_evidenced_nats == second.score_evidenced_nats


def test_scoring_does_not_mutate_its_input():
    observations = all_evidenced(1.0)
    before = dict(observations.values)
    cf.score(observations)
    assert observations.values == before


def test_breakdown_json_carries_every_variable_and_its_grade():
    payload = json.loads(cf.score(all_evidenced(1.0)).to_row()["breakdown_json"])
    names = {c["variable"] for c in payload["contributions"]}
    assert names == {s.name for s in cf.LITERATURE_V1.variables}
    for entry in payload["contributions"]:
        assert entry["grade"] in {"measured", "derived", "invented"}


def test_top_drivers_are_ordered_by_magnitude():
    result = cf.score(all_evidenced(-1.0))
    drivers = result.top_drivers(2)
    assert len(drivers) == 2
    assert abs(drivers[0].contribution_nats) >= abs(drivers[1].contribution_nats)


# --------------------------------------------------------------------------------------
# swap-derived readings
# --------------------------------------------------------------------------------------


def test_wash_transactions_counts_both_published_definitions():
    rows = [
        # WT1: one transaction holding both legs for one wallet.
        {"tx": "a", "ts_ms": 1_000, "wallet": "w1", "side": "buy"},
        {"tx": "a", "ts_ms": 1_000, "wallet": "w1", "side": "sell"},
        # WT2: same wallet, both sides, inside five seconds, two transactions.
        {"tx": "b", "ts_ms": 2_000, "wallet": "w2", "side": "buy"},
        {"tx": "c", "ts_ms": 4_000, "wallet": "w2", "side": "sell"},
        # Not wash: same wallet, both sides, but eleven seconds apart.
        {"tx": "d", "ts_ms": 10_000, "wallet": "w3", "side": "buy"},
        {"tx": "e", "ts_ms": 21_000, "wallet": "w3", "side": "sell"},
    ]
    wt1, wt2, combined = cf.wash_transactions(rows)
    assert wt1 == 1
    assert wt2 >= 1
    assert combined >= 1


def test_wash_transactions_is_empty_on_one_sided_flow():
    rows = [{"tx": f"t{i}", "ts_ms": i * 100, "wallet": "w", "side": "buy"} for i in range(20)]
    assert cf.wash_transactions(rows) == (0, 0, 0)


def test_early_turnover_share_uses_the_published_window():
    created = 1_000_000
    rows = [
        {"ts_ms": created + 1_000, "wallet": "a"},
        {"ts_ms": created + 2_000, "wallet": "a"},
        {"ts_ms": created + 3_000, "wallet": "b"},
        # Outside the five-minute window; must not count.
        {"ts_ms": created + 600_000, "wallet": "c"},
    ]
    share, n = cf.early_turnover_share(rows, created)
    assert n == 3
    assert share == pytest.approx(1.0 - 2 / 3)


def test_early_turnover_share_is_none_on_an_empty_window():
    assert cf.early_turnover_share([], 1_000) == (None, 0)


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------


def test_sql_seed_matches_python_weight_set(tmp_db):
    stored = cf.load_weight_set(conn=tmp_db)
    assert stored.version == cf.LITERATURE_V1.version
    assert stored.unknown_penalty_cap_nats == pytest.approx(cf.LITERATURE_V1.unknown_penalty_cap_nats)
    assert stored.base_graduation_rate == pytest.approx(cf.LITERATURE_V1.base_graduation_rate)
    expected = cf.LITERATURE_V1.by_name()
    actual = stored.by_name()
    assert set(actual) == set(expected)
    for name, spec in expected.items():
        got = actual[name]
        assert got.weight_nats == pytest.approx(spec.weight_nats, rel=1e-12), name
        assert got.grade is spec.grade, name
        assert got.provenance == spec.provenance, name
        assert got.mapping == spec.mapping, name
        assert got.direction is spec.direction, name
        assert got.correlation_group == spec.correlation_group, name
        assert got.contribution_factor == pytest.approx(spec.contribution_factor), name
        assert got.factor_reason == spec.factor_reason, name
        assert dict(got.params) == pytest.approx(dict(spec.params)), name


def test_seeded_weight_set_is_active_and_not_fitted(tmp_db):
    row = fetch_one(tmp_db, "SELECT active, source, fitted_from_trades FROM confluence_weight_sets")
    assert row is not None
    assert row["active"] == 1
    assert row["source"] == "literature"
    assert row["fitted_from_trades"] is None


def test_stored_weights_validate(tmp_db):
    assert cf.validate_weight_set(cf.load_weight_set(conn=tmp_db)) == []


def test_a_new_weight_version_is_a_new_row_not_an_edit(tmp_db):
    fitted = cf.WeightSet(
        version="fitted-2027-01",
        source="fitted",
        created_ms=now_ms(),
        fitted_from_trades=4_000,
        variables=cf.LITERATURE_V1.variables,
        note="hypothetical",
    )
    written = cf.save_weight_set(fitted, tmp_db, activate=True)
    assert written == len(cf.LITERATURE_V1.variables)
    versions = {r["version"] for r in fetch_all(tmp_db, "SELECT version FROM confluence_weight_sets")}
    assert versions == {"literature-v1", "fitted-2027-01"}
    assert cf.load_weight_set(conn=tmp_db).version == "fitted-2027-01"
    # The literature set survives untouched, so a bad fit is one UPDATE away from a rollback.
    assert cf.load_weight_set("literature-v1", tmp_db).by_name()["copycat_reuse"].weight_nats == (
        pytest.approx(cf.LITERATURE_V1.by_name()["copycat_reuse"].weight_nats)
    )


def test_load_falls_back_to_the_literature_set_for_an_unknown_version(tmp_db):
    assert cf.load_weight_set("no-such-version", tmp_db) is cf.LITERATURE_V1


def test_record_score_is_idempotent(tmp_db):
    result = cf.score(all_evidenced(1.0))
    first = cf.record_score(result, tmp_db)
    second = cf.record_score(result, tmp_db)
    assert first == second
    rows = fetch_all(tmp_db, "SELECT score_id, coverage, point_in_time FROM confluence_scores")
    assert len(rows) == 1
    assert rows[0]["coverage"] == pytest.approx(1.0)


def test_recorded_scores_are_keyed_by_instant(tmp_db):
    a = cf.score(all_evidenced(1.0))
    b = cf.score(
        cf.Observations(chain=SOL, token=MINT, as_of_ms=a.as_of_ms + 1, values=a and all_evidenced(1.0).values)
    )
    cf.record_score(a, tmp_db)
    cf.record_score(b, tmp_db)
    rows = fetch_all(tmp_db, "SELECT score_id FROM confluence_scores")
    assert len(rows) == 2


def test_calibration_refuses_to_fit_and_says_why(tmp_db):
    status = cf.calibration_status(tmp_db)
    assert status["can_fit"] is False
    assert status["closed_trades"] == 0
    assert status["min_trades_naive"] == 3_500
    assert "3500" in status["reason"] or "3,500" in status["reason"]


# --------------------------------------------------------------------------------------
# reading the database
# --------------------------------------------------------------------------------------


def _seed_token(conn, *, created_ms: int, creator: str | None = CREATOR) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, symbol, name, creator, created_ms, first_seen_ms) "
        "VALUES (?,?,?,?,?,?,?)",
        (SOL.value, MINT, "TST", "Test", creator, created_ms, created_ms),
    )


def _seed_dedup_original(conn, created_ms: int) -> None:
    conn.execute(
        "INSERT INTO dedup_coverage (chain, first_scan_ms, last_scan_ms, mints_seen) VALUES (?,?,?,?)",
        (SOL.value, created_ms - 10_000, created_ms + 10_000, 5),
    )
    conn.execute(
        "INSERT INTO dedup_mints (chain, mint, created_ms, scanned_ms, image_status) VALUES (?,?,?,?,?)",
        (SOL.value, MINT, created_ms, created_ms + 1_000, "ok"),
    )
    conn.execute(
        "INSERT INTO dedup_mint_fingerprints (chain, mint, kind, value) VALUES (?,?,?,?)",
        (SOL.value, MINT, "image_sha256", "deadbeef"),
    )


def _seed_dedup_copycat(conn, created_ms: int) -> None:
    _seed_dedup_original(conn, created_ms)
    conn.execute(
        "INSERT INTO dedup_fingerprints (chain, kind, value, first_mint, first_created_ms, "
        "first_seen_ms, hits) VALUES (?,?,?,?,?,?,?)",
        (SOL.value, "image_sha256", "deadbeef", OTHER, created_ms - 86_400_000, created_ms - 86_400_000, 3),
    )


def _seed_swaps(conn, created_ms: int, n: int = 30) -> None:
    for i in range(n):
        conn.execute(
            "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_native, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                SOL.value,
                f"tx{i}",
                1_000 + i,
                created_ms + i * 1_000,
                f"w{i % 6}",
                MINT,
                "buy" if i % 2 == 0 else "sell",
                str(10_000_000 + i),
                "test",
            ),
        )


def test_observe_from_db_reads_an_original_with_history(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_dedup_original(tmp_db, created)
    tmp_db.execute(
        "INSERT INTO creators (chain, address, launches, graduated, rugged, updated_ms) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, CREATOR, 120, 12, 0, now_ms()),
    )
    _seed_swaps(tmp_db, created)
    observations = cf.observe_from_db(SOL, MINT, tmp_db)

    assert observations.get("copycat_reuse").value == 1.0
    creator = observations.get("creator_graduation_history")
    assert creator.value == pytest.approx(0.1)
    assert creator.support == 120.0
    assert observations.get("wash_trading").known
    assert observations.get("bot_dominated_early_activity").proxy
    # No holder list is stored anywhere, so the best-evidenced holder variable is unknown.
    assert not observations.get("bundle_adjusted_concentration").known
    assert "holder lists" in (observations.get("bundle_adjusted_concentration").detail or "")


def test_observe_from_db_reads_a_copycat(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_dedup_copycat(tmp_db, created)
    observations = cf.observe_from_db(SOL, MINT, tmp_db)
    assert observations.get("copycat_reuse").value == -1.0
    assert not observations.point_in_time


def test_observe_from_db_marks_an_unscanned_mint_unknown(tmp_db):
    _seed_token(tmp_db, created_ms=now_ms() - 1_000)
    observations = cf.observe_from_db(SOL, MINT, tmp_db)
    reading = observations.get("copycat_reuse")
    assert not reading.known
    assert "never been de-duplication scanned" in (reading.detail or "")


def test_observe_from_db_respects_as_of_ms(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_swaps(tmp_db, created, n=30)
    early = cf.observe_from_db(SOL, MINT, tmp_db, as_of_ms=created + 5_000)
    late = cf.observe_from_db(SOL, MINT, tmp_db, as_of_ms=created + 100_000)
    assert early.get("wash_trading").support == 6.0
    assert late.get("wash_trading").support == 30.0


def _prove_tape(conn, *, created_ms: int, complete: bool = True) -> None:
    """Record tape coverage for ``MINT`` through ``kaiba.ingest.tape``'s own writer.

    Through the writer rather than an INSERT so migration 025's CHECK constraints have to
    accept it: a fixture able to fake completeness would make the gate below untestable.
    """
    from kaiba.ingest import tape

    record = tape.TapeRecord(
        chain=SOL,
        token=MINT,
        coverage=tape.COMPLETE if complete else tape.PARTIAL,
        route=tape.ROUTE_TRADES,
        reason="fixture",
        proof=tape.REASON_END_OF_HISTORY if complete else None,
        covered_from_ms=created_ms - 1 if complete else created_ms + 1,
        covered_to_ms=created_ms + 60_000,
        created_ms=created_ms,
    )
    assert tape.store(record, conn) is True
    conn.commit()


def test_entity_count_over_a_partial_tape_is_a_floor_not_a_reading(tmp_db):
    """The buyers of the slice we pulled are not the buyers of the mint.

    pump.fun's trades endpoint serves a hot window, so for most mints in this database
    the slice is all we will ever have. Reporting its distinct-buyer count as *the* count
    would be a confident number about a mint nobody finished looking at — and it errs
    adverse, because too few independent entities is what this variable warns about.
    """
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_swaps(tmp_db, created, n=30)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("independent_entity_count")
    assert reading.value is None
    assert reading.basis is EvidenceBasis.UNAVAILABLE
    assert not reading.known
    assert reading.support == 3.0, "the floor is still reported as support"
    assert "not proved complete" in (reading.detail or "")
    assert "at least 3" in (reading.detail or "")


def test_entity_count_is_reported_once_the_tape_is_proved_complete(tmp_db):
    """The paired half: the gate must narrow the claim, not delete the variable."""
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_swaps(tmp_db, created, n=30)
    _prove_tape(tmp_db, created_ms=created, complete=True)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("independent_entity_count")
    assert reading.known
    assert reading.value == 3.0
    assert reading.basis is EvidenceBasis.DERIVED
    assert "complete back to launch" in (reading.detail or "")


def test_a_partial_tape_record_does_not_open_the_entity_gate(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_swaps(tmp_db, created, n=30)
    _prove_tape(tmp_db, created_ms=created, complete=False)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("independent_entity_count")
    assert not reading.known
    assert "partial" in (reading.detail or "")


def test_a_complete_tape_proved_against_a_launch_time_that_moved_is_not_coverage(tmp_db):
    """The gate is :func:`kaiba.ingest.tape.completeness`, not a local re-derivation of it,
    so it inherits that module's refusal to carry a proof across a corrected launch time."""
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_swaps(tmp_db, created, n=30)
    _prove_tape(tmp_db, created_ms=created, complete=True)
    tmp_db.execute(
        "UPDATE tokens SET created_ms=? WHERE chain=? AND address=?",
        (created - 500_000, SOL.value, MINT),
    )
    tmp_db.commit()
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("independent_entity_count")
    assert not reading.known
    assert "created_ms_moved" in (reading.detail or "")


def test_observe_from_db_has_no_creator_opinion_without_a_backfill(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("creator_graduation_history")
    assert not reading.known
    assert "no launch history backfilled" in (reading.detail or "")


def test_score_from_db_is_the_one_line_call(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_dedup_original(tmp_db, created)
    _seed_swaps(tmp_db, created)
    result = cf.score_from_db(SOL, MINT, tmp_db)
    assert result.token == MINT
    assert result.weights_version == "literature-v1"
    assert 0.0 < result.coverage < 1.0
    assert result.by_name()["copycat_reuse"].contribution_nats > 0
    assert result.summary()


def test_score_from_db_marks_replay_limits_when_a_dossier_is_used(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    dossier = {
        "address": MINT,
        "chain": SOL.value,
        "top10_pct": {"value": "42.0", "basis": "provider_reported"},
        "dev_pct": {"value": "1.0", "basis": "provider_reported"},
        "liquidity_usd": {"value": "250.0", "basis": "provider_reported"},
        "freeze_authority_revoked": True,
        "graded_wallets": ["a", "b"],
    }
    tmp_db.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, score, grade, blockers_json, "
        "warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, MINT, created + 1_000, 50.0, "C", "[]", "[]", "[]", json.dumps(dossier)),
    )
    result = cf.score_from_db(SOL, MINT, tmp_db)
    assert not result.point_in_time
    assert any("scan time" in note for note in result.notes)
    assert result.by_name()["raw_top10_concentration"].value == pytest.approx(42.0)
    assert result.by_name()["raw_top10_concentration"].contribution_nats == 0.0
    assert result.by_name()["raw_top10_threshold"].contribution_nats < 0
    assert result.by_name()["liquidity_floor"].contribution_nats < 0
    assert result.by_name()["freeze_authority_live"].contribution_nats == 0.0


def test_distribution_summarises_a_batch():
    results = [cf.score(all_evidenced(v)) for v in (-1.0, -0.5, 0.0, 0.5, 1.0)]
    summary = cf.distribution(results)
    assert summary["n"] == 5
    assert summary["evidenced_min"] < summary["evidenced_max"]
    assert summary["distinct_evidenced_scores"] >= 3
    assert cf.distribution([]) == {"n": 0}


def _seed_curve_snapshot(conn, created_ms: int, *, sol_in_curve: str, trades: int) -> None:
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, sol_in_curve, trades_seen, "
        "trades_basis, coverage_from_ms, created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value, MINT, created_ms + 60_000, 1_000_000_000, 31_000_000_000,
            "700000000000000", "1073000000000000", sol_in_curve, trades,
            "covered_from_launch", created_ms, created_ms, "pumpfun",
        ),
    )


def test_an_empty_curve_is_not_a_zero_velocity(tmp_db):
    """A mint that took SOL and gave it all back is a net-flow fact, not a slow curve."""
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_curve_snapshot(tmp_db, created, sol_in_curve="0", trades=40)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("curve_velocity_sol_per_swap")
    assert not reading.known
    assert "net-flow fact" in (reading.detail or "")


def test_the_cumulative_velocity_fallback_is_flagged_as_a_proxy(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    _seed_curve_snapshot(tmp_db, created, sol_in_curve="12.5", trades=50)
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("curve_velocity_sol_per_swap")
    assert reading.known
    assert reading.proxy
    assert reading.basis is EvidenceBasis.ESTIMATED
    assert reading.value == pytest.approx(0.25)


def test_gross_inflow_per_buy_is_used_when_no_snapshot_exists(tmp_db):
    """The lane's own denominator is dead on live data; this route is the closer proxy."""
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    for i in range(10):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_native, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (SOL.value, f"g{i}", i, created + i * 1_000, f"w{i}", MINT, "buy", str(500_000_000), "t"),
        )
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("curve_velocity_sol_per_swap")
    assert reading.known and reading.proxy
    assert reading.value == pytest.approx(0.5)
    assert reading.support == 10.0
    assert "gross inflow" in (reading.detail or "")


def test_velocity_refuses_when_swap_coverage_starts_after_launch(tmp_db):
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created)
    tmp_db.execute(
        "INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_native, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, "late", 1, created + 600_000, "w", MINT, "buy", "500000000", "t"),
    )
    reading = cf.observe_from_db(SOL, MINT, tmp_db).get("curve_velocity_sol_per_swap")
    assert not reading.known
    assert "after launch" in (reading.detail or "")


# --------------------------------------------------------------------------------------
# the groundless guard
#
# Unknown-is-never-good applied at the aggregate rather than per variable. Two live mints
# came out positive on zero evidenced coverage because an unvalidated 35% top-10 line and
# a $10,000 depth floor both happened to clear. That is fail-open, and over half the live
# universe establishes nothing, so it is the common case rather than a corner.
# --------------------------------------------------------------------------------------


def groundless_but_all_invented_clear() -> cf.Observations:
    """Nothing evidenced measured; every invented threshold passes.

    The readings are the ones the live mint 6GmAFSYs4gk3 actually carried.
    """
    return obs(
        raw_top10_threshold=known(19.47503638093939),
        dev_supply_threshold=known(0.006093881540792599),
        liquidity_floor=known(5_692_851.558822458),
    )


def test_invented_thresholds_cannot_make_a_groundless_token_positive():
    result = cf.score(groundless_but_all_invented_clear())
    assert not result.grounded
    assert result.measured_evidenced == ()
    assert result.standing is cf.Standing.GROUNDLESS
    assert result.score_all_nats <= 0.0
    # And specifically: identical to knowing nothing at all, because nothing was added.
    assert result.score_all_nats == pytest.approx(-cf.UNKNOWN_PENALTY_CAP_NATS)
    assert result.score_all_nats == pytest.approx(result.score_evidenced_nats)
    assert result.delta_invented_nats == pytest.approx(0.0)
    assert not result.sign_flipped


def test_invented_thresholds_may_still_subtract_on_a_groundless_token():
    """The guard is asymmetric on purpose: a made-up line may veto, it may not endorse."""
    result = cf.score(
        obs(
            raw_top10_threshold=known(95.0),  # fails the 35% line
            liquidity_floor=known(10.0),  # fails the $10k floor
        )
    )
    assert not result.grounded
    assert result.suppressed_invented == ()
    assert result.delta_invented_nats < 0
    assert result.score_all_nats < result.score_evidenced_nats < 0


def test_one_measured_evidenced_variable_is_enough_to_ground():
    base = groundless_but_all_invented_clear()
    result = cf.score(base.with_value("copycat_reuse", known(1.0)))
    assert result.grounded
    assert result.measured_evidenced == ("copycat_reuse",)
    assert result.suppressed_invented == ()
    assert result.delta_invented_nats > 0  # now the invented variables are allowed to add
    assert result.standing is cf.Standing.EVIDENCED_POSITIVE


def test_a_zero_weight_reading_does_not_ground_a_token():
    """Reading a variable we deliberately gave no weight is not measuring evidence."""
    result = cf.score(
        groundless_but_all_invented_clear()
        .with_value("raw_top10_concentration", known(19.47))
        .with_value("wallet_pnl_grade", known(4.0))
        .with_value("freeze_authority_live", known(0.0))
        .with_value("independent_entity_count", known(9.0))
    )
    assert not result.grounded
    assert result.score_all_nats <= 0.0


def test_a_reading_too_thin_to_score_does_not_ground_a_token():
    """Three swaps and no wash detected is a fact about our sample, not about the token."""
    result = cf.score(
        groundless_but_all_invented_clear().with_value("wash_trading", known(0.0, support=3.0))
    )
    assert result.by_name()["wash_trading"].known
    assert not result.grounded
    assert result.score_all_nats <= 0.0
    # Ten swaps is the published line at which absence starts to mean something.
    grounded = cf.score(
        groundless_but_all_invented_clear().with_value("wash_trading", known(0.0, support=50.0))
    )
    assert grounded.grounded


def test_withheld_credit_stays_visible_and_is_not_silently_rewritten():
    result = cf.score(groundless_but_all_invented_clear())
    assert set(result.suppressed_invented) == {
        "raw_top10_threshold",
        "dev_supply_threshold",
        "liquidity_floor",
    }
    for name in result.suppressed_invented:
        line = result.by_name()[name]
        assert line.suppressed
        assert line.contribution_nats == 0.0
        # The number it would have added is still on the record.
        assert line.raw_contribution_nats > 0.0
        assert line.suppression_reason == cf.GROUNDLESS_GUARD
        assert "WITHHELD" in line.line()
    assert any("groundless guard withheld" in note for note in result.notes)
    assert "GROUNDLESS GUARD" in result.summary()
    payload = json.loads(result.to_row()["breakdown_json"])
    assert payload["grounded"] is False
    assert payload["standing"] == "groundless"
    assert sorted(payload["suppressed_invented"]) == sorted(result.suppressed_invented)
    assert payload["groundless_guard"] == cf.GROUNDLESS_GUARD


def test_standing_separates_an_evidenced_positive_from_an_invented_one():
    assert cf.score(all_evidenced(1.0)).standing is cf.Standing.EVIDENCED_POSITIVE

    # Grounded, but the evidenced variables alone say "no".
    borderline = neutral_full_coverage().with_value("copycat_reuse", known(-0.15))
    assert cf.score(borderline).score_evidenced_nats <= 0.0
    invented_positive = cf.score(
        borderline.with_value("dev_supply_threshold", known(1.0)).with_value(
            "liquidity_floor", known(500_000.0)
        )
    )
    assert invented_positive.grounded
    assert invented_positive.score_evidenced_nats <= 0.0 < invented_positive.score_all_nats
    assert invented_positive.standing is cf.Standing.INVENTED_POSITIVE

    assert cf.score(all_evidenced(-1.0)).standing is cf.Standing.NEGATIVE
    assert cf.score(obs()).standing is cf.Standing.GROUNDLESS


def test_a_withheld_variable_does_not_raise_its_group_cap():
    """A variable that may not speak may not decide how loudly its group speaks either."""
    result = cf.score(
        obs(
            raw_top10_threshold=known(10.0),  # would be +0.4048
            dev_supply_threshold=known(50.0),  # is -0.4048
        )
    )
    group = next(g for g in result.groups if g.group == cf.GROUP_HOLDER_STRUCTURE)
    # Without suppression the sum would be 0.0 and the negative would vanish.
    assert group.raw_sum_nats == pytest.approx(-cf.RULE_U_WEIGHT_NATS)
    assert group.cap_nats == pytest.approx(cf.RULE_U_WEIGHT_NATS)
    assert result.by_name()["dev_supply_threshold"].contribution_nats < 0


@pytest.mark.parametrize("top10", [0.0, 19.5, 34.9, 35.1, 95.0])
@pytest.mark.parametrize("dev", [0.0, 9.9, 10.1, 60.0])
@pytest.mark.parametrize("liquidity", [1.0, 9_999.0, 10_001.0, 5_000_000.0])
def test_no_combination_of_invented_thresholds_lifts_a_groundless_token(top10, dev, liquidity):
    result = cf.score(
        obs(
            raw_top10_threshold=known(top10),
            dev_supply_threshold=known(dev),
            liquidity_floor=known(liquidity),
            bundler_exposure_threshold=known(1.0),
            sniper_exposure_threshold=known(1.0),
        )
    )
    assert not result.grounded
    assert result.score_all_nats <= 0.0, f"groundless token scored {result.score_all_nats:+.4f}"
    assert result.score_all_nats <= result.score_evidenced_nats + 1e-12


def test_score_from_db_reproduces_the_live_sign_flip_as_groundless(tmp_db):
    """Regression for live mints 6GmAFSYs4gk3 and Hqa3scJGHpXQ.

    A dossier and nothing else: no de-duplication scan, no creator backfill, no swaps.
    Before the guard both scored +0.405 on zero evidenced coverage.
    """
    created = now_ms() - 3_600_000
    _seed_token(tmp_db, created_ms=created, creator=None)
    dossier = {
        "address": MINT,
        "chain": SOL.value,
        "top10_pct": {"value": "19.47503638093939", "basis": "provider_reported"},
        "dev_pct": {"value": "0.006093881540792599", "basis": "provider_reported"},
        "liquidity_usd": {"value": "5692851.558822458", "basis": "provider_reported"},
        "freeze_authority_revoked": True,
        "graded_wallets": [],
    }
    tmp_db.execute(
        "INSERT INTO token_dossiers (chain, address, built_at_ms, score, grade, blockers_json, "
        "warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, MINT, created + 1_000, 80.0, "B", "[]", "[]", "[]", json.dumps(dossier)),
    )
    result = cf.score_from_db(SOL, MINT, tmp_db)
    assert result.coverage == 0.0
    assert not result.grounded
    assert result.standing is cf.Standing.GROUNDLESS
    assert result.score_all_nats == pytest.approx(-cf.UNKNOWN_PENALTY_CAP_NATS)
    assert not result.sign_flipped
    assert not result.divergent
    assert len(result.suppressed_invented) == 3
