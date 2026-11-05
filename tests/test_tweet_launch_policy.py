"""Exact supply target, no silent cap resize, holder allocation and partial-sell accounting."""
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution.tweet_launch_policy import (
    RequirementUnavailable,
    allocation_receipt,
    holder_fee_args,
    quote_five_percent,
    remaining_increment,
    require_cap,
)

WALLET = "0x" + "1" * 40


def quote(**changes):
    return quote_five_percent(total_supply_atoms=1_000_000_000 * 10**6,
        virtual_token_atoms=1_073_000_000 * 10**6, virtual_native_atoms=30 * 10**9,
        fee_bps=125, native_decimals=9, **changes)


def test_five_percent_is_total_supply_and_native_quote_rounds_up_not_down():
    q = quote()
    assert q.target_atoms == 50_000_000 * 10**6
    assert Decimal("1.484") < q.native_amount < Decimal("1.486")
    numerator = 30 * 10**9 * q.target_atoms * 10125
    denominator = (1_073_000_000 * 10**6 - q.target_atoms) * 10000
    assert q.native_atoms * denominator >= numerator
    assert (q.native_atoms-1) * denominator < numerator
    assert require_cap(q, Decimal("1.5")) is q
    with pytest.raises(RequirementUnavailable, match="five_percent_exceeds_cap"):
        require_cap(q, Decimal("0.5"))


def test_subunit_supply_rounding_is_explicit_and_real_receipt_is_required():
    q = quote_five_percent(total_supply_atoms=21, virtual_token_atoms=100,
                          virtual_native_atoms=100, fee_bps=0, native_decimals=0)
    assert q.target_atoms == 2
    assert allocation_receipt(total_supply_atoms=21, received_atoms=2)["meets_target"]
    bad = allocation_receipt(total_supply_atoms=1000, received_atoms=49)
    assert not bad["meets_target"] and bad["difference_atoms"] == -1
    assert bad["automatic_top_up_allowed"] is False


@pytest.mark.parametrize("field,value", [("total_supply_atoms", 0), ("virtual_token_atoms", True),
    ("virtual_native_atoms", -1), ("fee_bps", 1.5), ("fee_bps", 10001), ("native_decimals", 19)])
def test_invalid_curve_inputs_are_unavailable(field, value):
    args = dict(total_supply_atoms=1000, virtual_token_atoms=2000,
                virtual_native_atoms=100, fee_bps=100, native_decimals=18)
    args[field] = value
    with pytest.raises(RequirementUnavailable):
        quote_five_percent(**args)


@pytest.mark.parametrize("dex", ["flap", "fourmeme"])
def test_all_distributable_tax_goes_to_dividends_with_zero_creator_cut(dex):
    flag, cfg = holder_fee_args(chain=Chain.BSC, dex=dex, wallet=WALLET, tax_bps=100)
    assert flag == f"--{dex}-rate-conf"
    if dex == "flap":
        assert cfg["dividend_bps"] == 10000
        assert cfg["mkt_bps"] == cfg["lp_bps"] == cfg["deflation_bps"] == 0
        assert cfg["buy_tax_rate"] == cfg["sell_tax_rate"] == 100
    else:
        assert cfg["divide_rate"] == 100
        assert cfg["recipient_rate"] == cfg["burn_rate"] == cfg["liquidity_rate"] == 0


@pytest.mark.parametrize("chain,dex", [(Chain.SOL, "pump"), (Chain.ROBINHOOD, "pons"),
                                      (Chain.ROBINHOOD, "flap"), (Chain.SOL, "bags")])
def test_unsupported_holder_route_never_falls_back_to_paying_creator(chain, dex):
    with pytest.raises(RequirementUnavailable, match="holder_distribution_unverified"):
        holder_fee_args(chain=chain, dex=dex, wallet=WALLET, tax_bps=100)


@pytest.mark.parametrize("args", [{"tax_bps": 0}, {"tax_bps": 1001}, {"tax_bps": True},
                                 {"minimum_holding_tokens": 9999}, {"wallet": "0x"+"0"*40},
                                 {"wallet": "not-bound-wallet"}])
def test_invalid_holder_fee_parameters_refuse(args):
    parameters = dict(chain=Chain.BSC, dex="flap", wallet=WALLET, tax_bps=100)
    parameters.update(args)
    with pytest.raises(RequirementUnavailable):
        holder_fee_args(**parameters)


def test_whole_percent_fourmeme_requirement_is_not_silently_rounded():
    with pytest.raises(RequirementUnavailable, match="whole_percent"):
        holder_fee_args(chain=Chain.BSC, dex="fourmeme", wallet=WALLET, tax_bps=125)


def test_partial_fills_and_unknown_orders_do_not_sell_the_same_increment_twice():
    args = dict(rung_start_atoms=1000, pct_remaining=Decimal(25), confirmed_sold_atoms=0,
                order_in_flight=False)
    assert remaining_increment(**args) == 250
    assert remaining_increment(**{**args, "confirmed_sold_atoms": 100}) == 150
    assert remaining_increment(**{**args, "confirmed_sold_atoms": 100, "order_in_flight": True}) == 0
    assert remaining_increment(**{**args, "confirmed_sold_atoms": 250}) == 0
    with pytest.raises(RequirementUnavailable, match="overfilled"):
        remaining_increment(**{**args, "confirmed_sold_atoms": 251})


def test_three_existing_rungs_take_percentages_of_remaining_with_explicit_amounts():
    remaining = 1_000_000
    amounts = []
    for pct in [50, 25, 15]:
        amount = remaining_increment(rung_start_atoms=remaining, pct_remaining=Decimal(pct),
                                    confirmed_sold_atoms=0, order_in_flight=False)
        amounts.append(amount)
        remaining -= amount
    assert amounts == [500000, 125000, 56250] and remaining == 318750
