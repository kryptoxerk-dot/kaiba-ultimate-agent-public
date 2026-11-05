"""Controls for lookahead, selection and cash errors in small-bankroll research."""
import copy
import json

import pytest

from kaiba.learning.pullback_study import classify, main, study


def obs(t, px, cap=150_000, **kwargs):
    return dict(token="fixture-a", source="fixture-feed", observed_ms=t, available_ms=t,
                price_usd=px, market_cap_usd=cap, cap_basis="market_cap", **kwargs)


def path():
    return [obs(0, 1), obs(30_000, .6), obs(60_000, .7)]


def case(i="a", token="fixture-a", signal=60_000, entry=90_000, cost=300_000_000,
         settled=200_000, proceeds=330_000_000, evidence="modeled"):
    return dict(id=i, token=token, signal_ms=signal, entry_ms=entry, cost_lamports=cost,
                settled_ms=settled, proceeds_lamports=proceeds, evidence=evidence)


def data(cases=None):
    return dict(version="pullback-input/1", chain="sol", population="signals", scanned_count=10,
                start_ms=0, end_ms=500_000, observations=path(), opportunities=cases or [case()])


def baseline(report):
    return report["results"][0]


def test_positive_control_all_four_rules_and_exact_cash():
    assert all(classify(path(), 60_000)[0].values())
    result = baseline(study(data(), capitals=(500_000_000,)))
    assert result["cash_lamports"] == 530_000_000
    assert result["realized_pnl_lamports"] == 30_000_000
    assert result["profit_proven"] is False


def test_future_peak_cannot_turn_small_dip_into_eligible_dip():
    rows = [obs(0, 1), obs(60_000, .95)]
    original = classify(rows, 60_000)
    assert not original[0]["cap_dip"]
    assert classify(rows + [obs(61_000, 1.3)], 60_000) == original


def test_backfilled_peak_unavailable_at_decision_is_excluded():
    rows = [obs(0, 1), obs(30_000, 2), obs(60_000, .95)]
    rows[1]["available_ms"] = 80_000
    assert not classify(rows, 60_000)[0]["cap_dip"]


@pytest.mark.parametrize("basis", ["fdv", "unknown"])
def test_fdv_is_not_market_cap(basis):
    rows = path()
    rows[-1]["cap_basis"] = basis
    flags, reason = classify(rows, 60_000)
    assert flags["baseline"] and not flags["cap_100k"]
    assert reason == "market_cap_unverified"


def test_current_floor_required_even_if_token_previously_exceeded_it():
    rows = path()
    rows[-1]["market_cap_usd"] = 90_000
    assert not classify(rows, 60_000)[0]["cap_dip"]


def test_prior_crossing_must_have_been_observed():
    rows = path()
    for row in rows[:-1]:
        row["market_cap_usd"] = 90_000
    flags, reason = classify(rows, 60_000)
    assert flags["cap_100k"] and not flags["cap_dip"]
    assert reason == "prior_crossing_unobserved"


def test_reclaim_is_separate_from_buying_falling_price():
    rows = [obs(0, 1), obs(60_000, .7)]
    flags, reason = classify(rows, 60_000)
    assert flags["cap_dip"] and not flags["cap_dip_reclaim"] and reason == "no_reclaim"


def test_sources_are_not_spliced_to_invent_a_peak():
    rows = path()
    for row in rows[:-1]:
        row["source"] = "other-feed"
    assert not classify(rows, 60_000)[0]["cap_dip"]


def test_stale_or_ambiguous_current_data_never_qualifies():
    assert classify(path(), 121_000)[1] == "stale_observation"
    other = obs(60_000, 3)
    other["source"] = "other-feed"
    assert classify(path() + [other], 60_000)[1] == "ambiguous_source"


def test_finite_cash_cannot_take_both_overlapping_entries():
    d = data([case(), case("b", "fixture-b", entry=100_000)])
    result = baseline(study(d, capitals=(500_000_000,)))
    assert result["counts"]["entered"] == 1
    assert result["counts"]["insufficient_cash"] == 1
    assert result["cash_lamports"] == 530_000_000


def test_small_and_large_accounts_use_same_fixed_size_not_scaled_fills():
    d = data([case(), case("b", "fixture-b", entry=100_000)])
    report = study(d)
    assert report["results"][0]["counts"]["entered"] == 1
    assert report["results"][4]["counts"]["entered"] == 2
    assert report["results"][4]["realized_pnl_lamports"] == 60_000_000


def test_unresolved_and_unverified_proceeds_do_not_replenish_cash():
    for first in [case(settled=None, proceeds=None), case(evidence="unknown")]:
        result = baseline(study(data([first, case("b", "fixture-b", entry=250_000, settled=400_000)]),
                                capitals=(500_000_000,)))
        assert result["counts"]["entered"] == 1
        assert result["unsettled_cost_lamports"] == 300_000_000
        assert result["realized_pnl_lamports"] == 0
        assert result["liquidation_equity_lamports"] is None


def test_only_proceeds_settled_strictly_before_entry_can_fund_it():
    for entry, expected in [(200_000, 1), (200_001, 2)]:
        d = data([case(), case("b", "fixture-b", entry=entry, settled=400_000)])
        assert baseline(study(d, capitals=(500_000_000,)))["counts"]["entered"] == expected


def test_reserve_and_total_fee_inclusive_cost_are_debited():
    result = baseline(study(data([case(cost=480_000_001)]), capitals=(500_000_000,)))
    assert result["counts"]["insufficient_cash"] == 1
    assert result["cash_lamports"] == 500_000_000


def test_repeat_buys_do_not_spend_unlimited_cash_on_one_coin():
    d = data([case(), case("b", entry=250_000, settled=400_000)])
    result = baseline(study(d, capitals=(500_000_000,)))
    assert result["counts"]["repeat_token"] == 1


def test_delay_and_missing_entry_are_counted_not_filled_at_leader_price():
    d = data([case(entry=60_001), case("b", entry=None, cost=None, settled=None, proceeds=None)])
    result = baseline(study(d, capitals=(500_000_000,)))
    assert result["counts"]["entry_faster_than_declared_delay"] == 1
    assert result["counts"]["missing_entry"] == 1
    assert result["cash_lamports"] == 500_000_000


def test_unknown_cap_is_in_coverage_denominator_not_silently_discarded():
    d = data()
    d["observations"] = []
    report = study(d)
    assert report["opportunities"] == 1 and report["classification_counts"]["missing_observation"] == 1
    assert report["results"][0]["counts"]["entered"] == 1
    assert report["results"][1]["counts"]["qualified"] == 0
    assert report["population_verified"] is False


@pytest.mark.parametrize("field,value", [("cost_lamports", -1), ("proceeds_lamports", -1),
                                         ("settled_ms", 80_000), ("evidence", "definitely_safe")])
def test_invalid_cashflows_refused(field, value):
    d = data()
    d["opportunities"][0][field] = value
    with pytest.raises(ValueError):
        study(d)


def test_duplicate_and_nan_observations_refused():
    for d in [data(), data()]:
        d = copy.deepcopy(d)
        d["observations"].append(d["observations"][0].copy())
        with pytest.raises(ValueError, match="duplicate_observation"):
            study(d)
        d["observations"].pop()
        d["observations"][0]["price_usd"] = float('nan')
        with pytest.raises(ValueError, match="invalid_price"):
            study(d)


def test_cli_writes_aggregate_only_and_invalid_input_is_sanitized(tmp_path, capsys):
    file = tmp_path / "private.json"
    file.write_text(json.dumps(data()), encoding="utf-8")
    assert main(["--input", str(file)]) == 0
    text = capsys.readouterr().out
    report = json.loads(text)
    assert report["status"] == "research_only" and len(report["input_sha256"]) == 64
    assert "fixture-a" not in text and str(file) not in text
    assert main(["--input", str(file), "--reserve-sol", "NaN"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_input"
