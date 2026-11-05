"""Fixed-selection follow-through must retain old inventory and honest missing evidence."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from kaiba.learning.wallet_followthrough import analyze_followthrough, summarize_cohort

SELECTION = 1791190106296


def row(side, offset, *, token="TOKEN", qty=100, native=100, usd="100", wallet="WALLET", **kwargs):
    return {
        "chain": "sol",
        "wallet": wallet,
        "token": token,
        "ts_ms": SELECTION + offset,
        "side": side,
        "amount_token": None if qty is None else str(qty),
        "amount_native": None if native is None else str(native),
        "usd_value": usd,
        "tx": None,
        "source": "fixture:atomic",
        **kwargs,
    }


def follow(rows, **kwargs):
    return analyze_followthrough(
        rows, chain="sol", wallet="WALLET", selection_ms=SELECTION, as_of_ms=SELECTION + 10_000, **kwargs
    )


def test_post_cutoff_positive_cashflow_is_not_profit_on_an_old_bag():
    result = follow(
        [
            row("buy", -3000),
            row("sell", -1000, qty=50, native=60, usd="60"),
            row("buy", 1000, qty=30, native=30, usd="30"),
            row("sell", 3000, qty=80, native=40, usd="40"),
        ]
    )
    assert result["baseline_open_episode_count"] == 1
    assert result["baseline"]["gross_money_axis"]["gross_profit"] is None
    assert result["post_selection_new_entries"]["counts"]["all"] == 0
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] is None
    overlap = result["preexisting_inventory_followthrough"]
    assert overlap["gross_money_axis"]["gross_profit"] == "-30"
    assert overlap["gross_money_axis"]["cost"] == "130"
    assert result["post_selection_observed_cashflows"]["net_cashflow_money_units_known_subset"] == "10"
    assert "not profit" in result["post_selection_observed_cashflows"]["meaning"]
    assert result["episodes"][0]["selection_category"] == "preexisting_closed_after_selection"


def test_old_inventory_add_and_partial_sale_cannot_restart_inventory_at_zero():
    result = follow(
        [
            row("buy", -2000),
            row("buy", 1000, qty=50, native=50, usd="50"),
            row("sell", 2000, qty=100, native=200, usd="200"),
        ]
    )
    assert result["post_selection_new_entries"]["counts"]["all"] == 0
    assert result["preexisting_inventory_followthrough"]["counts"]["open"] == 1
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None
    assert result["episodes"][0]["leftover_qty_units"] == "50"
    assert result["episodes"][0]["full_close_hold_s"] is None


def test_new_entries_and_old_bag_closures_have_separate_pnl():
    result = follow(
        [
            row("buy", -2000, token="OLD"),
            row("sell", 1000, native=50, usd="50", token="OLD"),
            row("buy", 2000, token="NEW"),
            row("sell", 3000, native=150, usd="150", token="NEW"),
        ]
    )
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] == "-50"
    assert result["accounting_breakdown"]["new_closed_after_selection"]["counts"]["closed"] == 1
    assert result["accounting_breakdown"]["preexisting_closed_after_selection"]["counts"]["closed"] == 1


def test_last_partial_sale_remains_open_even_after_getting_cost_back():
    result = follow([row("buy", 1000), row("sell", 2000, qty=90, native=200, usd="200")])
    forward = result["post_selection_new_entries"]
    assert forward["counts"]["closed"] == 0
    assert forward["counts"]["open"] == 1
    assert forward["gross_money_axis"]["gross_profit"] is None
    assert forward["gross_usd_known_subset"]["gross_profit"] is None
    assert forward["behavior"]["median_first_trim_hold_s"] == "1"
    assert forward["behavior"]["median_full_close_hold_s"] is None
    assert result["episodes"][0]["leftover_qty_units"] == "10"
    assert result["profitability_status"] == "UNPROVEN"


def test_baseline_positive_new_period_negative_changes_without_overlap_laundering():
    result = follow(
        [
            row("buy", -3000, token="BASE"),
            row("sell", -2000, native=150, usd="150", token="BASE"),
            row("buy", 1000, token="NEW"),
            row("sell", 2000, native=70, usd="70", token="NEW"),
        ]
    )
    assert result["baseline"]["gross_money_axis"]["size_weighted_gross_roi"] == "0.5"
    assert result["post_selection_new_entries"]["gross_money_axis"]["size_weighted_gross_roi"] == "-0.3"
    assert result["comparison"]["new_minus_baseline_size_weighted_gross_roi"] == "-0.8"
    assert result["accounting_breakdown"]["closed_before_selection"]["counts"]["closed"] == 1


def test_native_and_original_usd_coverage_are_independent():
    native = follow([row("buy", 1000, usd=None), row("sell", 2000, native=150, usd=None)])
    assert native["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"
    assert native["post_selection_new_entries"]["gross_usd_known_subset"]["gross_profit"] is None
    usd = follow([row("buy", 1000, native=None, usd="10"), row("sell", 2000, native=None, usd="20")])
    assert usd["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] is None
    assert usd["post_selection_new_entries"]["gross_usd_known_subset"]["gross_profit"] == "10"
    assert usd["post_selection_new_entries"]["gross_usd_known_subset"]["coverage_fraction"] == "1"


@pytest.mark.parametrize("bad_qty", [None, "100.1", "NaN"])
def test_unknown_inventory_cannot_create_a_profitable_followthrough(bad_qty):
    result = follow([row("buy", -1000, qty=bad_qty), row("sell", 1000, native=500, usd="500")])
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None
    assert result["preexisting_inventory_followthrough"]["gross_usd_known_subset"]["gross_profit"] is None


def test_unmatched_post_selection_sale_is_reported_as_unknown_not_profit():
    result = follow([row("sell", 1000, native=10000, usd="10000")])
    period = result["post_selection_new_entries"]
    assert period["counts"]["unmatched_sell"] == 1
    assert period["gross_money_axis"]["gross_profit"] is None
    assert period["gross_usd_known_subset"]["gross_profit"] is None


def test_observed_transfer_contaminates_the_crossing_bag():
    result = follow(
        [
            row("buy", -2000),
            row("transfer_in", -1000, qty=50, native=None, usd=None),
            row("sell", 1000, qty=150, native=500, usd="500"),
        ]
    )
    assert result["preexisting_inventory_followthrough"]["counts"]["contaminated"] == 1
    assert result["preexisting_inventory_followthrough"]["gross_usd_known_subset"]["gross_profit"] is None


def test_purchase_at_selection_is_old_inventory_and_future_sales_are_excluded():
    result = follow([row("buy", 0), row("sell", 11_000, native=1_000_000, usd="1000000")])
    assert result["baseline_open_episode_count"] == 1
    assert result["post_selection_new_entries"]["counts"]["all"] == 0
    assert result["accounting_breakdown"]["preexisting_open_as_of"]["counts"]["open"] == 1
    assert result["coverage"]["combined"]["excluded_rows_by_reason"]["after_as_of"] == 1


def test_mixed_side_same_timestamp_and_boundary_reopen_are_excluded():
    result = follow([row("buy", -1000), row("sell", 0), row("buy", 0), row("sell", 1000)])
    assert result["baseline"]["gross_money_axis"]["gross_profit"] is None
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None
    assert all(not e["profit_eligible"] for e in result["episodes"])


def test_declared_missing_interval_excludes_crossing_episodes_only():
    result = follow(
        [
            row("buy", -3000, token="GAP"),
            row("sell", 3000, native=500, usd="500", token="GAP"),
            row("buy", 4000, token="AFTER"),
            row("sell", 5000, native=150, usd="150", token="AFTER"),
        ],
        metadata={"unobserved_intervals": [{"start_ms": SELECTION - 2000, "end_ms": SELECTION + 2000}]},
    )
    assert result["preexisting_inventory_followthrough"]["counts"]["intersects_unobserved_interval"] == 1
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"


def test_token_scoped_gap_does_not_discard_another_token_episode():
    result = follow(
        [
            row("buy", 1000, token="OTHER"),
            row("sell", 3000, native=150, usd="150", token="OTHER"),
        ],
        metadata={
            "unobserved_intervals": [{"start_ms": SELECTION, "end_ms": SELECTION + 5000, "token": "GAP"}]
        },
    )
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"


@pytest.mark.parametrize("gap_offset", [-2000, 1000, 3000])
def test_dropped_incompatible_leg_point_gap_excludes_old_bag_inclusive_boundaries(gap_offset):
    # A provider leg rejected for units is absent here. The retained legs alone
    # appear fully closed; its point-gap receipt must block that false conclusion.
    rows = [
        row("buy", -2000, token="DROPPED"),
        row("sell", 3000, native=500, usd="500", token="DROPPED"),
        row("buy", 1000, token="OTHER"),
        row("sell", 3000, native=150, usd="150", token="OTHER"),
    ]
    assert follow(rows)["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] == "400"
    gap_ms = SELECTION + gap_offset
    result = follow(
        rows,
        metadata={"unobserved_intervals": [{"start_ms": gap_ms, "end_ms": gap_ms, "token": "DROPPED"}]},
    )
    old = result["preexisting_inventory_followthrough"]
    assert old["counts"]["intersects_unobserved_interval"] == 1
    assert old["gross_money_axis"]["gross_profit"] is None
    assert old["gross_usd_known_subset"]["gross_profit"] is None
    assert old["behavior"]["median_full_close_hold_s"] is None
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"


def test_gap_token_normalizes_evm_case_and_sol_preserves_case():
    rows = [row("buy", -2000, token="0xAbCd", chain="eth"), row("sell", 3000, token="0xABCD", chain="eth")]
    result = analyze_followthrough(
        rows,
        chain="eth",
        wallet="WALLET",
        selection_ms=SELECTION,
        as_of_ms=SELECTION + 10_000,
        metadata={
            "unobserved_intervals": [{"start_ms": SELECTION, "end_ms": SELECTION, "token": " 0xAbCd "}]
        },
    )
    assert result["coverage"]["unobserved_intervals"][0]["token"] == "0xabcd"
    assert result["preexisting_inventory_followthrough"]["gross_money_axis"]["gross_profit"] is None
    sol = follow(
        [row("buy", 1000, token="MixedCase"), row("sell", 3000, token="MixedCase", native=150)],
        metadata={
            "unobserved_intervals": [
                {"start_ms": SELECTION + 2000, "end_ms": SELECTION + 2000, "token": "mixedcase"}
            ]
        },
    )
    assert sol["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "50"


@pytest.mark.parametrize("invalid_token", ["", " ", True, 1, []])
def test_invalid_gap_token_cannot_silently_leave_a_false_closed_result(invalid_token):
    with pytest.raises(ValueError, match="token"):
        follow(
            [],
            metadata={
                "unobserved_intervals": [{"start_ms": SELECTION, "end_ms": SELECTION, "token": invalid_token}]
            },
        )


def test_capped_unknown_history_cannot_produce_profit_or_copy_approval():
    meta = {
        "baseline": {"row_cap_truncated": True, "history_complete": False},
        "post_selection": {"query_missing": True},
    }
    result = follow([row("buy", 1000), row("sell", 2000, native=150, usd="150")], metadata=meta)
    assert "row_cap_truncated" in result["evidence_flags"]
    assert "query_observations_missing" in result["evidence_flags"]
    assert result["metadata"] == meta
    assert result["profitability_status"] == "UNPROVEN"
    assert result["live_copy_approved"] is False
    json.dumps(result, allow_nan=False)


def test_changed_normalization_axis_is_flagged_and_comparison_withheld():
    rows = [
        row("buy", -3000, native=1000000, token="BASE"),
        row("sell", -1000, native=1500000, token="BASE"),
        row("buy", 1000, native=1000000),
        row("sell", 2000, native=2000000),
    ]
    result = follow(rows, metadata={"money_axis": "usd_micro", "baseline": {"money_axis": "native"}})
    assert result["baseline_normalization_axis_changed"] is True
    assert result["comparison"]["new_minus_baseline_size_weighted_gross_roi"] is None
    assert result["post_selection_new_entries"]["gross_native_atomic"] is None


def test_cohort_does_not_turn_missing_open_wallets_into_zero_returns():
    positive = follow([row("buy", 1000), row("sell", 2000, native=150, usd="150")])
    empty = analyze_followthrough(
        [], chain="sol", wallet="EMPTY", selection_ms=SELECTION, as_of_ms=SELECTION + 10_000
    )
    summary = summarize_cohort([positive, empty])
    stats = summary["groups"][0]["periods"]["post_selection_new_entries:money_axis"]
    assert summary["wallets"] == 2
    assert stats["wallets_with_closed_result"] == 1
    assert stats["size_weighted_gross_roi_known_subset"] == "0.5"
    assert summary["profitability_status"] == "UNPROVEN"


def test_cohort_rejects_duplicate_identities_and_different_windows():
    profile = follow([])
    with pytest.raises(ValueError, match="duplicate"):
        summarize_cohort([profile, profile])
    other = analyze_followthrough(
        [], chain="sol", wallet="OTHER", selection_ms=SELECTION, as_of_ms=SELECTION + 20_000
    )
    with pytest.raises(ValueError, match="window"):
        summarize_cohort([profile, other])


def test_huge_atomic_cashflow_stays_exact():
    big = 10**60 + 123
    result = follow([row("buy", 1000, native=big), row("sell", 2000, native=big + 7)])
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] == "7"
    summary = summarize_cohort([result])
    assert (
        summary["groups"][0]["periods"]["post_selection_new_entries:money_axis"]["gross_profit_known_subset"]
        == "7"
    )


def test_cohort_atomic_sums_preserve_digits_beyond_decimal_precision():
    big = 10**60 + 123
    first = follow([row("buy", 1000, native=1), row("sell", 2000, native=big)])
    second = analyze_followthrough(
        [row("buy", 1000, native=1, wallet="SECOND"), row("sell", 2000, native=big, wallet="SECOND")],
        chain="sol",
        wallet="SECOND",
        selection_ms=SELECTION,
        as_of_ms=SELECTION + 10_000,
    )
    stats = summarize_cohort([first, second])["groups"][0]["periods"]["post_selection_new_entries:money_axis"]
    assert stats["gross_profit_known_subset"] == str(2 * (big - 1))
    assert stats["gross_cost_known_subset"] == "2"


def test_cohort_keeps_native_and_synthetic_usd_axes_in_separate_groups():
    native = follow([row("buy", 1000), row("sell", 2000, native=150)])
    synthetic = analyze_followthrough(
        [
            row("buy", 1000, native=100000000, wallet="SYNTH"),
            row("sell", 2000, native=150000000, wallet="SYNTH"),
        ],
        chain="sol",
        wallet="SYNTH",
        selection_ms=SELECTION,
        as_of_ms=SELECTION + 10_000,
        metadata={"money_axis": "usd_micro"},
    )
    summary = summarize_cohort([native, synthetic])
    assert len(summary["groups"]) == 2
    assert {g["money_axis"] for g in summary["groups"]} == {"native_atomic", "usd_micro"}


def test_old_unchanged_bag_does_not_invent_fresh_behavior_or_zero_forward_profit():
    result = follow([row("buy", -3000), row("buy", -2000, qty=50, native=50, usd="50")])
    assert result["named_observed_patterns"] == {}
    assert result["post_selection_observed_cashflows"]["observed_swap_rows"] == 0
    assert result["post_selection_new_entries"]["gross_money_axis"]["gross_profit"] is None
    assert (
        result["preexisting_inventory_followthrough"]["behavior"]["open_unreturned_capital_money_units"]
        == "150"
    )


def test_inputs_are_unchanged_and_invalid_windows_rejected():
    rows = [row("buy", -1000), row("sell", 1000)]
    meta = {"baseline": {"row_cap_truncated": True}}
    before = deepcopy((rows, meta))
    follow(rows, metadata=meta)
    assert (rows, meta) == before
    with pytest.raises(ValueError, match="selection_ms"):
        analyze_followthrough([], chain="sol", wallet="W", selection_ms=2, as_of_ms=1)
    with pytest.raises(ValueError, match="milliseconds"):
        analyze_followthrough([], chain="sol", wallet="W", selection_ms=1.5, as_of_ms=2)
    with pytest.raises(ValueError, match="start_ms"):
        follow([], metadata={"unobserved_intervals": [{"start_ms": 2, "end_ms": 1}]})
