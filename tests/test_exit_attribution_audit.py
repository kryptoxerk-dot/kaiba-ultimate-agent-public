"""Validate descriptive arithmetic without claiming an executable counterfactual."""

import pytest

from kaiba.learning import exit_attribution as audit


def fixture():
    return {
        "snapshot_ms": 100000,
        "source_hashes": {},
        "positions": [
            {
                "position_id": "p",
                "chain": "sol",
                "token": "token",
                "opened_ms": 1000,
                "closed_ms": 20000,
                "cost_native": "100",
                "proceeds_native": "80",
                "qty_total": "10",
                "exit_reason": "stop_loss",
            }
        ],
        "trades": [],
        "links": [{"position_id": "p", "order_id": "buy"}, {"position_id": "p", "order_id": "sell"}],
        "orders": [
            {"order_id": "buy", "side": "buy", "state": "filled"},
            {"order_id": "sell", "side": "sell", "state": "filled"},
        ],
        "fills": [
            {
                "order_id": "buy",
                "price_usd": "100",
                "token_atoms": "10",
                "native_atoms": "100",
                "basis": "fill_ratio",
                "fill_ts_ms": 1000,
            },
            {
                "order_id": "sell",
                "price_usd": "80",
                "token_atoms": "10",
                "native_atoms": "80",
                "basis": "fill_ratio",
                "fill_ts_ms": 19999,
                "fill_ts_basis": "order_updated",
            },
        ],
        "events": [
            {
                "id": 1,
                "ts_ms": 10000,
                "kind": "system",
                "payload": {
                    "position_id": "p",
                    "event": "quote_decision",
                    "pct": "100",
                    "quote_provenance": {"price_usd": "120", "observed_ms": 9999, "source": "mark"},
                },
            }
        ],
    }


def test_mark_gap_is_percentage_points_and_money_stays_native():
    rows, _, summary = audit.analyze(fixture())
    row = rows[0]
    assert row["comparison_eligible"]
    assert row["gross_native_return_pct"] == -20
    assert row["first_mark_return_pct_usd"] == 20
    assert row["exit_fill_return_pct_usd"] == -20
    assert row["mark_to_fill_gap_pp"] == 40
    assert row["first_full_quote_to_close_s"] == 10
    assert summary["closed_without_trade"] == ["p"]
    assert summary["fill_fee_coverage"] == {"fills": 2, "with_nonnull_fee": 0}


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("quantity", "token_quantity_mismatch"),
        ("native", "native_ledger_mismatch"),
        ("partial", "open_or_multiple_or_missing_fills"),
        ("basis", "fill_price_not_measured_ratio"),
        ("future", "quote_observation_in_future"),
        ("late", "quote_after_recorded_fill"),
        ("no_quote", "missing_full_exit_quote"),
        ("open", "open_or_multiple_or_missing_fills"),
    ],
)
def test_noncomparable_cases_do_not_enter_the_mark_gap_denominator(damage, reason):
    data = fixture()
    if damage == "quantity":
        data["fills"][1]["token_atoms"] = "9"
    elif damage == "native":
        data["fills"][1]["native_atoms"] = "70"
    elif damage == "partial":
        data["orders"].append({"order_id": "partial", "side": "sell", "state": "filled"})
        data["links"].append({"position_id": "p", "order_id": "partial"})
    elif damage == "basis":
        data["fills"][1]["basis"] = "unavailable"
    elif damage == "future":
        data["events"][0]["payload"]["quote_provenance"]["observed_ms"] = 10001
    elif damage == "late":
        data["fills"][1]["fill_ts_ms"] = 9000
    elif damage == "no_quote":
        data["events"] = []
    else:
        data["positions"][0]["closed_ms"] = None
    rows, _, summary = audit.analyze(data)
    assert not rows[0]["comparison_eligible"]
    assert rows[0]["comparison_exclusion"] == reason
    assert summary["matched_single_roundtrips"]["n"] == 0


def test_banner_is_not_mistaken_for_the_venue_error():
    banner = "gmgn-cli refused before send (confirmation prompt): Swap confirmation required"
    assert audit.failure_type(banner, "HTTP 400 error=40003702 GEvmInsufficientSlippage") == "venue_slippage"
    assert audit.failure_type(banner, "POST /v1/trade/swap failed: unknown") == "venue_error_unclassified"
    assert audit.failure_type(banner) == "legacy_confirmation_banner_unresolved"
    assert audit.failure_type("wallet holds none of token") == "balance_or_inventory"


def test_local_limiter_failure_keeps_retry_delay_without_claiming_venue_failure():
    data = fixture()
    data["events"].append(
        {
            "id": 2,
            "ts_ms": 10100,
            "kind": "system",
            "payload": {
                "position_id": "p",
                "event": "exit_failed",
                "pct": "100",
                "order_id": None,
                "detail": "RateLimited: minimum interval (retry in 0.2s)",
                "retry_in_ms": 1800000,
            },
        }
    )
    rows, failures, summary = audit.analyze(data)
    assert rows[0]["failed_exit_events"] == 1
    assert failures[0]["type"] == "local_minimum_interval"
    assert summary["local_interval_backoff_s"]["max"] == 1800
    assert not failures[0]["provider_order_id_present"]


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", True])
def test_unknown_or_nonfinite_money_is_not_counted_as_zero(value):
    assert audit.return_pct(value, "100") is None
    assert audit.stats([None, float("nan")]) == {"n": 0}
