"""Evidence checks for inventory closure, coverage and honest behavioral descriptions."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from kaiba.core.schemas import SOL_NATIVE_MINT
from kaiba.learning.wallet_behavior_study import analyze_wallet


def row(side, ts, qty=100, money=100, usd="100", token="TOKEN", tx=None, **kwargs):
    return {
        "chain": "sol",
        "wallet": "WALLET",
        "token": token,
        "side": side,
        "ts_ms": ts,
        "amount_token": qty if isinstance(qty, float) else (str(qty) if qty is not None else None),
        "amount_native": str(money) if money is not None else None,
        "usd_value": usd,
        "tx": tx,
        "source": "fixture:atomic",
        **kwargs,
    }


def study(rows, **kwargs):
    return analyze_wallet(rows, chain="sol", wallet="WALLET", as_of_ms=10_000, **kwargs)


def test_partial_trim_is_open_cashflow_not_a_completed_loss_or_win():
    result = study([row("buy", 1000), row("sell", 2000, qty=40, money=80, usd="80")])
    episode = result["episodes"][0]
    assert result["episode_counts"]["open"] == 1
    assert result["gross_money_axis"]["gross_profit"] is None
    assert result["gross_usd_known_subset"]["complete_closed_profit_usd"] is None
    assert episode["gross_closed_profit_money_units"] is None
    assert episode["leftover_qty_units"] == "60"
    assert episode["first_trim_hold_s"] == 1
    assert episode["full_close_hold_s"] is None
    assert result["behavior"]["open_unreturned_capital_money_units"] == "20"
    assert result["behavior"]["open_observed_net_cashflow_money_units"] == "-20"


def test_trim_add_then_full_close_has_distinct_first_trim_and_full_hold():
    result = study(
        [
            row("buy", 1000),
            row("sell", 2000, qty=40, money=80, usd="80"),
            row("buy", 3000, qty=20, money=20, usd="20"),
            row("sell", 4000, qty=80, money=120, usd="120"),
        ]
    )
    episode = result["episodes"][0]
    assert result["episode_counts"]["closed"] == 1
    assert result["gross_native_atomic"]["gross_profit"] == "80"
    assert (
        result["gross_native_atomic"]["size_weighted_gross_roi"]
        == "0.6666666666666666666666666666666666666667"
    )
    assert episode["first_trim_hold_s"] == 1
    assert episode["full_close_hold_s"] == 3
    assert episode["buy_after_first_sell"] is True
    assert episode["buys"] == episode["sells"] == 2
    assert result["behavior"]["adds_episode_share"] == "1"
    assert result["behavior"]["multiple_sell_episode_share"] == "1"
    assert len(episode["trades"]) == 4


def test_full_close_then_rebuy_becomes_two_episodes():
    result = study(
        [
            row("buy", 1000),
            row("sell", 2000, money=150, usd="150"),
            row("buy", 3000),
            row("sell", 4000, money=50, usd="50"),
        ]
    )
    assert result["episode_counts"]["closed"] == 2
    assert result["gross_native_atomic"]["gross_profit"] == "0"
    assert [e["buy_after_first_sell"] for e in result["episodes"]] == [False, False]
    assert [len(e["trades"]) for e in result["episodes"]] == [2, 2]


def test_size_weighted_loss_despite_positive_mean_episode_return():
    result = study(
        [
            row("buy", 1000, money=1, usd="1", token="SMALL"),
            row("sell", 2000, money=2, usd="2", token="SMALL"),
            row("buy", 3000, money=100, usd="100", token="LARGE"),
            row("sell", 4000, money=90, usd="90", token="LARGE"),
        ]
    )
    gross = result["gross_native_atomic"]
    assert gross["gross_profit"] == "-9"
    assert gross["mean_episode_gross_roi"] == "0.45"
    assert Decimal(gross["size_weighted_gross_roi"]) < 0
    assert result["gross_usd_known_subset"]["gross_profit"] == "-9"


def test_missing_usd_is_a_partial_subset_not_full_wallet_dollar_profit():
    result = study(
        [
            row("buy", 1000, token="KNOWN"),
            row("sell", 2000, money=120, usd="120", token="KNOWN"),
            row("buy", 3000, token="UNKNOWN", usd=None),
            row("sell", 4000, money=1, usd=None, token="UNKNOWN"),
        ]
    )
    assert result["gross_native_atomic"]["gross_profit"] == "-79"
    usd = result["gross_usd_known_subset"]
    assert usd["gross_profit"] == "20"
    assert usd["covered_closed_episodes"] == 1
    assert usd["eligible_closed_episodes"] == 2
    assert usd["coverage_fraction"] == "0.5"
    assert usd["complete_closed_profit_usd"] is None


@pytest.mark.parametrize("bad_qty", [None, "0", "100.5", "NaN", "Infinity", "-100", True, 100.0])
def test_unknown_or_nonintegral_inventory_never_becomes_a_complete_trade(bad_qty):
    result = study([row("buy", 1000, qty=bad_qty), row("sell", 2000)])
    assert result["episode_counts"]["eligible_clean_closed"] == 0
    assert result["gross_money_axis"]["gross_profit"] is None
    assert result["behavior"]["observed_buy_events"] == 1
    assert result["behavior"]["observed_sell_events"] == 1
    assert result["behavior"]["median_observed_first_buy_to_first_sell_s"] == "1"


def test_fractional_money_atoms_are_unavailable_instead_of_silently_floored():
    result = study([row("buy", 1000, money="100.9"), row("sell", 2000, money=200)])
    assert result["coverage"]["invalid_fields_rows"]["amount_native"] == 1
    assert result["episodes"][0]["contamination"] == ["buy_without_native_amount"]
    assert result["gross_money_axis"]["gross_profit"] is None
    assert result["gross_usd_known_subset"]["gross_profit"] == "0"


def test_known_usd_closed_episode_does_not_require_a_native_quote_leg():
    result = study(
        [
            row("buy", 1000, money=None, usd="10"),
            row("sell", 2000, money=None, usd="20"),
        ]
    )
    assert result["gross_native_atomic"]["gross_profit"] is None
    assert result["gross_usd_known_subset"]["gross_profit"] == "10"
    assert result["gross_usd_known_subset"]["complete_closed_profit_usd"] == "10"
    assert result["gross_usd_known_subset"]["coverage_fraction"] == "1"
    assert result["episodes"][0]["money_axis_profit_eligible"] is False
    assert result["episodes"][0]["usd_profit_eligible"] is True
    assert result["behavior"]["median_full_close_hold_s"] == "1"


def test_unmatched_sell_and_observed_transfer_do_not_enter_gross_profit():
    result = study(
        [
            row("sell", 1000, token="UNMATCHED", money=1_000_000, usd="1000000"),
            row("buy", 2000, token="TRANSFER"),
            row("transfer_in", 2500, qty=50, token="TRANSFER", money=None, usd=None),
            row("sell", 3000, qty=150, token="TRANSFER", money=500, usd="500"),
        ]
    )
    assert result["episode_counts"]["unmatched_sell_episodes"] == 1
    assert result["episode_counts"]["contaminated"] == 2
    assert result["gross_money_axis"]["episodes"] == 0
    assert result["gross_usd_known_subset"]["gross_profit"] is None
    assert any("transfer_in" in e["contamination"] for e in result["episodes"])


def test_dedup_requires_strong_tx_identity_and_preserves_missing_tx_rows():
    buy = row("buy", 1000, tx="TX")
    repeated = dict(buy, id=2, source="fixture:second_provider")
    result = study([buy, repeated, row("sell", 2000, tx="EXIT", money=120, usd="120")])
    assert result["coverage"]["rows_dropped_strong_identity_duplicate"] == 1
    assert result["gross_money_axis"]["gross_profit"] == "20"
    missing_identity = study([dict(buy, tx=None), dict(repeated, tx=None), row("sell", 2000)])
    assert missing_identity["coverage"]["rows_dropped_strong_identity_duplicate"] == 0
    assert missing_identity["coverage"]["rows_without_tx_identity"] == 3
    assert missing_identity["episode_counts"]["open"] == 1
    assert missing_identity["episodes"][0]["leftover_qty_units"] == "100"


def test_distinct_legs_in_same_transaction_are_retained_and_reported():
    result = study(
        [
            row("buy", 1000, qty=100, tx="TX"),
            row("buy", 1000, qty=50, money=50, usd="50", tx="TX"),
            row("sell", 2000, qty=150, money=200, usd="200", tx="EXIT"),
        ]
    )
    assert result["coverage"]["rows_dropped_strong_identity_duplicate"] == 0
    assert result["coverage"]["tx_token_side_groups_with_distinct_quantities"] == 1
    assert result["gross_money_axis"]["gross_profit"] == "50"


def test_conflicting_same_identity_rows_are_retained_but_withheld_from_profit():
    result = study(
        [
            row("buy", 1000, tx="TX"),
            row("buy", 1000, tx="TX", money=200, usd="200"),
            row("sell", 2000, qty=200, money=1000, usd="1000"),
        ]
    )
    assert result["coverage"]["strong_identity_conflicting_rows_retained"] == 1
    assert result["coverage"]["included_rows"] == 3
    assert result["episodes"][0]["strong_identity_conflict"] is True
    assert result["gross_money_axis"]["gross_profit"] is None


def test_profit_concentration_removes_all_episodes_of_winning_tokens():
    rows = []
    for index, (token, proceeds) in enumerate(
        [
            ("A", 200),
            ("A", 200),
            ("B", 120),
            ("C", 110),
            ("LOSS", 50),
        ]
    ):
        rows.extend(
            [
                row("buy", index * 1000 + 1, token=token),
                row("sell", index * 1000 + 500, token=token, money=proceeds, usd=str(proceeds)),
            ]
        )
    gross = study(rows)["gross_money_axis"]
    assert gross["gross_profit"] == "180"
    assert gross["top1_positive_token_profit_share"] == "0.8695652173913043478260869565217391304348"
    assert gross["top3_positive_token_profit_share"] == "1"
    assert gross["top3_positive_tokens"] == ["A", "B", "C"]
    assert gross["leave_top3_positive_tokens_out_gross_profit"] == "-50"
    assert gross["leave_top3_positive_tokens_out_episodes"] == 1


def test_same_timestamp_close_reopen_is_unevaluated_without_duplicate_examples():
    result = study(
        [
            row("buy", 1000),
            row("sell", 2000),
            row("buy", 2000),
            row("sell", 3000),
        ]
    )
    assert result["episode_counts"]["closed"] == 2
    assert result["coverage"]["same_timestamp_boundary_ambiguous_episodes"] == 2
    assert result["gross_money_axis"]["gross_profit"] is None
    assert all(not e["detail_evaluated"] and e["trades"] == [] for e in result["episodes"])


def test_same_timestamp_within_single_episode_does_not_invent_buy_sell_order():
    result = study([row("buy", 1000), row("sell", 1000, money=120, usd="120")])
    episode = result["episodes"][0]
    assert episode["detail_evaluated"] is True
    assert episode["same_timestamp_order_ambiguous"] is True
    assert episode["first_sell_was_partial"] is None
    assert episode["buy_after_first_sell"] is None
    assert result["behavior"]["buy_after_first_sell_evaluated_episodes"] == 0
    assert result["gross_money_axis"]["gross_profit"] is None
    assert result["gross_usd_known_subset"]["gross_profit"] is None


def test_same_timestamp_order_changes_cannot_create_eligible_profit():
    buy = row("buy", 1000)
    trim = row("sell", 1000, qty=50, money=80, usd="80")
    close = row("sell", 2000, qty=50, money=80, usd="80")
    for tape in ([buy, trim, close], [trim, buy, close]):
        result = study(tape)
        assert result["coverage"]["same_timestamp_order_ambiguous_tokens"] == ["TOKEN"]
        assert result["gross_native_atomic"]["gross_profit"] is None
        assert result["gross_usd_known_subset"]["gross_profit"] is None
        assert result["behavior"]["eligible_episodes"] == 0
        assert result["behavior"]["observed_buy_events"] == 1
        assert result["behavior"]["observed_sell_events"] == 2


def test_scope_cutoff_and_quote_asset_exclusion_precede_reconstruction():
    result = study(
        [
            row("buy", 1000),
            row("sell", 11_000, money=1_000_000),
            row("buy", 1000, wallet="OTHER"),
            row("buy", 1000, chain="bsc"),
            row("buy", 1000, token=SOL_NATIVE_MINT),
            row("buy", "NaN"),
        ]
    )
    assert result["coverage"]["included_rows"] == 1
    assert result["coverage"]["excluded_rows_by_reason"] == {
        "after_as_of": 1,
        "other_or_missing_wallet": 1,
        "other_or_missing_chain": 1,
        "quote_asset": 1,
        "invalid_timestamp": 1,
    }
    assert result["episode_counts"]["open"] == 1


def test_micro_usd_axis_is_not_labeled_native_coin_profit():
    result = study(
        [
            row("buy", 1000, money=100_000_000),
            row("sell", 2000, money=150_000_000, usd="150"),
        ],
        metadata={"coverage": {"money_axis": "usd_micro", "rows_dropped_mixed_units": 5}},
    )
    assert result["money_axis"] == "usd_micro"
    assert result["gross_native_atomic"] is None
    assert result["gross_money_axis"]["gross_profit"] == "50000000"
    assert result["metadata"]["coverage"]["rows_dropped_mixed_units"] == 5


def test_imputed_usd_is_not_original_known_usd_and_metadata_is_preserved():
    metadata = {"truncated": True, "window": {"start_ms": 1000}, "original_usd_coverage": "partial"}
    result = study(
        [
            row("buy", 1000, usd_imputed=True),
            row("sell", 2000, money=150, usd="150"),
        ],
        metadata=metadata,
    )
    assert result["metadata"] == metadata
    assert result["gross_native_atomic"]["gross_profit"] == "50"
    assert result["gross_usd_known_subset"]["covered_closed_episodes"] == 0
    assert result["coverage"]["partial_history"] is True
    assert result["coverage"]["fees_coverage"] == "unknown"
    assert result["profitability_status"] == "UNPROVEN"
    assert result["live_copy_approved"] is False
    json.dumps(result, allow_nan=False)


def test_integer_cashflow_stays_exact_far_above_binary_float_precision():
    big = 10**60 + 123
    result = study([row("buy", 1000, money=big), row("sell", 2000, money=big + 7)])
    assert result["gross_native_atomic"]["cost"] == str(big)
    assert result["gross_native_atomic"]["gross_profit"] == "7"


def test_profitable_days_are_close_dates_not_trade_or_entry_dates():
    day = 86_400_000
    rows = [
        row("buy", 1000, token="A"),
        row("sell", day + 1000, money=150, usd="150", token="A"),
        row("buy", day + 2000, token="B"),
        row("sell", 2 * day + 1000, money=10, usd="10", token="B"),
    ]
    result = analyze_wallet(rows, chain="sol", wallet="WALLET", as_of_ms=3 * day)
    gross = result["gross_money_axis"]
    assert gross["profitable_utc_days"] == 1
    assert gross["observed_close_utc_days"] == 2
    assert gross["gross_profit_by_close_utc_day"] == {"1970-01-02": "50", "1970-01-03": "-90"}


def test_input_rows_and_metadata_are_not_mutated():
    rows = [row("buy", 1000), row("sell", 2000)]
    metadata = {"money_axis": "native", "window": {"truncated": True}}
    before = deepcopy((rows, metadata))
    study(rows, metadata=metadata)
    assert (rows, metadata) == before


def test_empty_tape_is_unknown_not_a_zero_profit_wallet():
    result = study([])
    assert result["episode_counts"]["all"] == 0
    assert result["gross_money_axis"]["gross_profit"] is None
    assert result["gross_usd_known_subset"]["coverage_fraction"] is None
    assert result["behavior"]["median_observed_buy_usd_unadjusted"] is None


def test_invalid_money_axis_and_cutoff_are_rejected():
    with pytest.raises(ValueError, match="money_axis"):
        study([], metadata={"money_axis": "human_native"})
    with pytest.raises(ValueError, match="as_of_ms"):
        analyze_wallet([], chain="sol", wallet="WALLET", as_of_ms=1.5)


def test_evm_wallet_and_token_addresses_are_case_insensitive():
    rows = [
        row("buy", 1000, chain="robinhood", wallet="0xABCD", token="0xFFFF"),
        row("sell", 2000, chain="robinhood", wallet="0xabcd", token="0xffff", money=120, usd="120"),
        row(
            "buy",
            3000,
            chain="robinhood",
            wallet="0xABCD",
            token="0x0000000000000000000000000000000000000000",
        ),
    ]
    result = analyze_wallet(rows, chain="robinhood", wallet="0xaBcD", as_of_ms=10_000)
    assert result["gross_native_atomic"]["gross_profit"] == "20"
    assert result["coverage"]["excluded_rows_by_reason"]["quote_asset"] == 1
