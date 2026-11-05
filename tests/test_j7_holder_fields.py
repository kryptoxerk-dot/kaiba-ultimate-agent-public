"""Documented J7 holder routing; no signing credentials or network requests."""
import pytest

from kaiba.core.schemas import Chain
from kaiba.execution.tweet_launch_policy import RequirementUnavailable, j7_holder_fields


def test_pons_rewards_go_to_holders_without_creator_recipient_or_buyback():
    fields = j7_holder_fields(chain=Chain.ROBINHOOD, dex="pons", tax_bps=200)
    assert fields["holder_dividends"] is True and fields["creator_tax_bps"] == 200
    assert fields["pair"] == "eth" and fields["buyback_enabled"] is False
    assert "fee_recipient" not in fields
    assert "api_key" not in fields and "session_id" not in fields


@pytest.mark.parametrize("chain,wire_chain", [(Chain.BSC, "bnb"), (Chain.ROBINHOOD, "robinhood")])
def test_flap_has_no_remainder_to_creator_and_explicit_chain_tax_and_native_pair(chain, wire_chain):
    fields = j7_holder_fields(chain=chain, dex="flap", tax_bps=100)
    assert fields["chain"] == wire_chain and fields["pair"] == "native"
    assert fields["buy_tax_rate"] == fields["sell_tax_rate"] == 100
    assert fields["dividend_bps"] == 10000
    assert fields["lp_bps"] == fields["deflation_bps"] == 0
    assert fields["pve"] is False and fields["dividend_token"] == "quote"
    assert "fee_recipient" not in fields


def test_solana_requires_operator_chosen_reward_and_avoids_trader_cashback():
    # Syntax-only fixture. Does NOT prove this mint is in the provider's reward registry.
    fields = j7_holder_fields(chain=Chain.SOL, dex="pump", reward_mint="A"*44, reward_symbol="AAPLx")
    assert fields["otc_reward_mint"] == "A"*44
    assert "cashback_mode" not in fields and "pump_fee_shareholders" not in fields
    with pytest.raises(RequirementUnavailable, match="verified_otc_reward_mint_required"):
        j7_holder_fields(chain=Chain.SOL, dex="pump")


@pytest.mark.parametrize("tax", [None, True, 0, -1, 1.5, 1001])
def test_no_implicit_or_invalid_tax_default(tax):
    with pytest.raises(RequirementUnavailable):
        j7_holder_fields(chain=Chain.ROBINHOOD, dex="pons", tax_bps=tax)


def test_rejects_unsupported_routes_or_mixing_provider_specific_fields():
    with pytest.raises(RequirementUnavailable, match="j7_holder_route_unverified"):
        j7_holder_fields(chain=Chain.SOL, dex="bags", tax_bps=100)
    with pytest.raises(RequirementUnavailable, match="otc_reward_fields_wrong_route"):
        j7_holder_fields(chain=Chain.ROBINHOOD, dex="pons", tax_bps=100, reward_mint="A"*44)
    with pytest.raises(RequirementUnavailable, match="otc_tax_rate_not_a_documented_field"):
        j7_holder_fields(chain=Chain.SOL, dex="pump", tax_bps=100, reward_mint="A"*44, reward_symbol="AAPLx")


def test_minimum_holding_and_pending_reward_are_not_silently_relaxed():
    with pytest.raises(RequirementUnavailable, match="invalid_minimum_holding"):
        j7_holder_fields(chain=Chain.BSC, dex="flap", tax_bps=100, minimum_holding_tokens=9999)
    with pytest.raises(RequirementUnavailable, match="verified_otc_reward_mint_required"):
        j7_holder_fields(chain=Chain.SOL, dex="pump", reward_mint="pending:NVDA", reward_symbol="NVDA")
