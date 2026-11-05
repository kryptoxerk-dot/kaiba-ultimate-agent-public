"""Provider token outcomes must never silently become reconstructed episodes."""
from copy import deepcopy
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.intelligence.provider_outcomes import extract_provider_outcomes

ADDRESS = "0x" + "a" * 40


def arguments():
    return dict(chain=Chain.BSC, address=ADDRESS, period="30d", receipt=Receipt(
        provider="gmgn", endpoint="portfolio.stats", observed_at_ms=1790239000000,
        basis=EvidenceBasis.PROVIDER_REPORTED))


def row():
    return {"wallet_address": ADDRESS, "pnl_stat": {
        "token_num": 100, "pnl_gt_5x_num": 2, "pnl_2x_5x_num": 4,
        "pnl_0x_2x_num": 34, "pnl_nd5_0x_num": 45, "pnl_lt_nd5_num": 15,
        "avg_holding_period": "119.999"}}


def test_exact_bucket_boundary_and_average_are_retained_without_reconstruction():
    outcome = extract_provider_outcomes(row(), **arguments())
    assert outcome.above_500pct == 2
    assert outcome.from_200_to_500pct == 4
    assert outcome.above_500pct_token_share == Decimal("0.02")
    assert outcome.average_hold_s == Decimal("119.999")
    assert outcome.basis == "provider_reported_token_distribution_not_closed_episodes"
    assert outcome.receipt == arguments()["receipt"]
    assert not hasattr(outcome, "closed_episodes")


@pytest.mark.parametrize("bad", [None, True, -1, 0.2, "NaN", "Infinity", "bad"])
def test_bad_bucket_does_not_turn_into_zero(bad):
    data = row()
    data["pnl_stat"]["pnl_gt_5x_num"] = bad
    with pytest.raises(ValueError):
        extract_provider_outcomes(data, **arguments())


def test_missing_bucket_is_unknown_not_zero():
    data = row()
    del data["pnl_stat"]["pnl_gt_5x_num"]
    with pytest.raises(ValueError):
        extract_provider_outcomes(data, **arguments())


def test_inconsistent_denominator_is_rejected():
    data = row()
    data["pnl_stat"]["token_num"] = 99
    with pytest.raises(ValueError, match="bucket_total_mismatch"):
        extract_provider_outcomes(data, **arguments())


def test_zero_distribution_is_measured_but_rate_is_undefined():
    data = row()
    data["pnl_stat"] = {key: 0 for key in data["pnl_stat"]}
    assert extract_provider_outcomes(data, **arguments()).above_500pct_token_share is None


def test_wrong_wallet_and_solana_case_are_rejected():
    data = row()
    data["wallet_address"] = "0x" + "b" * 40
    with pytest.raises(ValueError, match="wallet_identity"):
        extract_provider_outcomes(data, **arguments())
    args = arguments() | {"chain": Chain.SOL, "address": "AbCd"}
    with pytest.raises(ValueError, match="wallet_identity"):
        extract_provider_outcomes({**data, "wallet_address": "abcd"}, **args)


def test_evm_case_is_normalized_without_changing_chain():
    data = row()
    data["wallet_address"] = "0x" + "A" * 40
    outcome = extract_provider_outcomes(data, **arguments())
    assert outcome.address == ADDRESS and outcome.chain == Chain.BSC


@pytest.mark.parametrize("key,value", [("provider", "other"), ("endpoint", "portfolio.profits"),
                                       ("basis", EvidenceBasis.UNAVAILABLE)])
def test_wrong_receipt_cannot_supply_outcome_evidence(key, value):
    args = arguments()
    args["receipt"] = args["receipt"].model_copy(update={key: value})
    with pytest.raises(ValueError, match="receipt_basis_or_endpoint"):
        extract_provider_outcomes(row(), **args)


def test_labels_and_profit_numbers_do_not_change_outcome_evidence():
    data = row()
    enriched = deepcopy(data)
    enriched.update(common={"tags": ["kol", "smart_degen"]}, realized_profit="1000000")
    assert extract_provider_outcomes(data, **arguments()) == extract_provider_outcomes(
        enriched, **arguments())


def test_period_is_explicit_not_silently_relabelled():
    with pytest.raises(ValueError, match="unsupported_period"):
        extract_provider_outcomes(row(), **(arguments() | {"period": "unknown"}))
