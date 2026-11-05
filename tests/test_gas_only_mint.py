"""Gas-only mode cannot turn missing evidence or hidden payments into free mints."""
from decimal import Decimal

import pytest

from kaiba.hunters.gas_only_mint import assess_gas_only
from kaiba.hunters.qualification import VERIFY_FIELDS


def inputs(chain="bsc"):
    row = {"kind": "nft_mint", "chain": chain, "status": "open", "ev_score": "1",
           "cost_usd": ".01", "plan_json": {}, "evidence_json": {
               "kind": "nft_mint", "chain": chain, "wallet_count": 1,
               "official_url": "https://fixture.invalid/mint", "meta": {"qualification": {
                   **dict.fromkeys(VERIFY_FIELDS, True), "observed_ms": 100_000,
                   "realisable_value_usd": "1"}}}}
    receipt = dict(version="gas-only-receipt/1", chain=chain, wallet="fixture-wallet",
                   contract="fixture-contract", raw_sha256="a" * 64, observed_ms=100_000,
                   expires_ms=150_000, simulation_succeeded=True, asset_changes_complete=True,
                   fees_complete=True, official_contract_verified=True, owned_recipient_verified=True,
                   mint_price_native="0", contract_fees_native="0", non_gas_native_debit="0",
                   token_debits=[], approval_changes=[], authority_changes=[], risks=[],
                   gas_upper_native=".0001", provider_confirmation_required=False)
    kwargs = dict(wallet="fixture-wallet", contract="fixture-contract", runtime_chains=[chain],
                  at_ms=110_000, per_mint_gas_native=".001", daily_remaining_native=".001",
                  balance_native=".01", reserve_native=".001", ev_floor=Decimal("0"))
    return row, receipt, kwargs


@pytest.mark.parametrize("chain", ["bsc", "robinhood", "base", "sol"])
def test_positive_control_is_only_research_and_requires_actual_chain_capability(chain):
    row, receipt, kwargs = inputs(chain)
    out = assess_gas_only(row, receipt, **kwargs)
    assert out["gas_only_research_qualified"]
    assert out["funded_action_authorized"] is False and out["profit_proven"] is False
    kwargs["runtime_chains"] = []
    assert "runtime_chain_unverified" in assess_gas_only(row, receipt, **kwargs)["reasons"]


@pytest.mark.parametrize("field", ["mint_price_native", "contract_fees_native", "non_gas_native_debit"])
@pytest.mark.parametrize("value", [None, ".000001", True, "NaN"])
def test_zero_call_value_does_not_hide_paid_mint_or_unknown_fee(field, value):
    row, receipt, kwargs = inputs()
    receipt[field] = value
    assert not assess_gas_only(row, receipt, **kwargs)["gas_only_research_qualified"]


@pytest.mark.parametrize("field", ["token_debits", "approval_changes", "authority_changes", "risks"])
@pytest.mark.parametrize("value", [None, ["fixture-change"]])
def test_spends_approvals_delegations_and_missing_arrays_refused(field, value):
    row, receipt, kwargs = inputs()
    receipt[field] = value
    assert not assess_gas_only(row, receipt, **kwargs)["gas_only_research_qualified"]


@pytest.mark.parametrize("field,value", [
    ("wallet", "other-wallet"), ("contract", "other-contract"), ("chain", "base"),
    ("observed_ms", 110_001), ("expires_ms", 109_999), ("raw_sha256", "not-a-hash"),
    ("fees_complete", None), ("asset_changes_complete", False),
    ("provider_confirmation_required", True), ("provider_confirmation_required", None)])
def test_binding_freshness_completeness_and_provider_prompts_stay_binding(field, value):
    row, receipt, kwargs = inputs()
    receipt[field] = value
    assert not assess_gas_only(row, receipt, **kwargs)["gas_only_research_qualified"]


@pytest.mark.parametrize("field,value", [("daily_remaining_native", ".00009"),
                                         ("per_mint_gas_native", ".00009"),
                                         ("balance_native", ".001"),
                                         ("per_mint_gas_native", None)])
def test_fee_reserve_and_remaining_budget_cannot_be_bypassed(field, value):
    row, receipt, kwargs = inputs()
    kwargs[field] = value
    assert not assess_gas_only(row, receipt, **kwargs)["gas_only_research_qualified"]


def test_existing_economic_qualification_is_not_replaced_by_gas_only():
    row, receipt, kwargs = inputs()
    del row["evidence_json"]["meta"]["qualification"]["realisable_value_usd"]
    out = assess_gas_only(row, receipt, **kwargs)
    assert "reward_or_exit_value_unsourced" in out["reasons"]
    assert not out["gas_only_research_qualified"]


def test_no_identifiers_are_exported_and_missing_receipt_refuses():
    row, receipt, kwargs = inputs()
    assert "fixture-wallet" not in str(assess_gas_only(row, receipt, **kwargs))
    out = assess_gas_only(row, None, **kwargs)
    assert not out["gas_only_research_qualified"]
