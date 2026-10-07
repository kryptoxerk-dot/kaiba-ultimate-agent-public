"""Owner's five-percent launch and holder-reward requirements, as pure calculations.

No network, transaction, database or signer access. Live integration belongs to P8-TL.
Curve quotes are modelled quantities, not evidence of a five-percent on-chain fill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from kaiba.core.schemas import Chain


class RequirementUnavailable(ValueError):
    pass


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RequirementUnavailable(f"invalid_{name}")
    return value


@dataclass(frozen=True)
class SupplyQuote:
    target_atoms: int
    total_supply_atoms: int
    native_atoms: int
    native_decimals: int
    basis: str = "constant_product_fee_on_top_model"

    @property
    def native_amount(self) -> Decimal:
        return Decimal(f"{self.native_atoms}e-{self.native_decimals}")


def quote_five_percent(*, total_supply_atoms: int, virtual_token_atoms: int,
                       virtual_native_atoms: int, fee_bps: int, native_decimals: int) -> SupplyQuote:
    """Inverse constant-product quote, rounded UP. Inputs must be a verified fresh curve.

Target is 5% of TOTAL supply, rounded up by <1 indivisible token unit. fee_bps means
fees charged on top of curve cost, not an arbitrary tax model. Creation gas/fees are
separate. A taxed initial allocation needs a venue-specific net-receipt quote instead.
"""
    supply = _integer(total_supply_atoms, "supply", 1)
    virtual_tokens = _integer(virtual_token_atoms, "virtual_token_reserve", 1)
    virtual_native = _integer(virtual_native_atoms, "virtual_native_reserve", 1)
    fee = _integer(fee_bps, "fee_bps")
    decimals = _integer(native_decimals, "native_decimals")
    if fee > 10000 or decimals > 18:
        raise RequirementUnavailable("invalid_quote_units")
    target = (supply * 5 + 99) // 100
    if target >= virtual_tokens:
        raise RequirementUnavailable("target_exceeds_curve")
    # Use integer rational arithmetic: Decimal's ambient precision must not affect wei.
    numerator = virtual_native * target * (10000 + fee)
    denominator = (virtual_tokens - target) * 10000
    amount = (numerator + denominator - 1) // denominator
    return SupplyQuote(target, supply, amount, decimals)


def quote_five_percent_fee_on_input(*, total_supply_atoms: int, virtual_token_atoms: int,
                                    virtual_native_atoms: int, fee_bps: int,
                                    native_decimals: int) -> SupplyQuote:
    """Single fee deducted from gross input, rounded down as in measured Pons receipts.

No creator/transfer/anti-sniper tax is assumed. Those require a separate venue quote.
Returns the minimum gross input whose net curve output reaches the five-percent target;
token/native quantization can produce an overfill, which still needs receipt reporting.
"""
    fee = _integer(fee_bps, "fee_bps")
    if fee >= 10000:
        raise RequirementUnavailable("invalid_fee_bps")
    net = quote_five_percent(total_supply_atoms=total_supply_atoms,
                            virtual_token_atoms=virtual_token_atoms,
                            virtual_native_atoms=virtual_native_atoms, fee_bps=0,
                            native_decimals=native_decimals)
    # net(gross) = gross - floor(gross*fee/10000) = ceil(gross*(10000-fee)/10000).
    gross = (net.native_atoms - 1) * 10000 // (10000 - fee) + 1
    return SupplyQuote(net.target_atoms, net.total_supply_atoms, gross, native_decimals,
                       basis="constant_product_single_fee_on_input_model")


def require_cap(quote: SupplyQuote, max_native_amount: Decimal) -> SupplyQuote:
    if not isinstance(max_native_amount, Decimal) or not max_native_amount.is_finite() or max_native_amount <= 0:
        raise RequirementUnavailable("missing_native_cap")
    if quote.native_amount > max_native_amount:
        raise RequirementUnavailable("five_percent_exceeds_cap")
    return quote  # Never silently resize a five-percent allocation down to fit a cap.


def allocation_receipt(*, total_supply_atoms: int, received_atoms: int) -> dict:
    supply = _integer(total_supply_atoms, "supply", 1)
    received = _integer(received_atoms, "received")
    if received > supply:
        raise RequirementUnavailable("received_exceeds_supply")
    target = (supply * 5 + 99) // 100
    return {"target_supply_pct": "5", "target_atoms": target, "received_atoms": received,
            "total_supply_atoms": supply, "meets_target": received == target,
            "difference_atoms": received - target, "automatic_top_up_allowed": False}


def holder_fee_args(*, chain: Chain, dex: str, wallet: str, tax_bps: int,
                    minimum_holding_tokens: int = 10000) -> tuple[str, dict]:
    """Prepare 100% distributable tax to native holder dividends on supported BNB routes.

Gas/platform fees are not this tax. The recipient field is syntactically required, but
its allocation is zero. Mainnet receipt must prove the deployed processor/dividend
configuration. Do not interpret a creator-wallet split as dynamic holder rewards.
"""
    if chain is not Chain.BSC or dex not in {"flap", "fourmeme"}:
        raise RequirementUnavailable(f"holder_distribution_unverified:{chain.value}/{dex}")
    if not isinstance(wallet, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet) or int(wallet, 16) == 0:
        raise RequirementUnavailable("invalid_bound_wallet")
    tax = _integer(tax_bps, "tax_bps", 1)
    minimum = _integer(minimum_holding_tokens, "minimum_holding", 10000)
    if tax > 1000:
        raise RequirementUnavailable("tax_above_ten_percent")
    if dex == "flap":
        return "--flap-rate-conf", {
            "buy_tax_rate": tax, "sell_tax_rate": tax,
            "mkt_bps": 0, "deflation_bps": 0, "dividend_bps": 10000, "lp_bps": 0,
            "minimum_share_balance": minimum, "recipient_type": "split",
            "twitter_account": "", "split_conf": [{"recipient": wallet, "bps": 10000}],
        }
    if tax % 100:
        raise RequirementUnavailable("fourmeme_requires_whole_percent_tax")
    return "--fourmeme-rate-conf", {
        "fee_plan": True, "fee_rate": tax // 100, "recipient_address": wallet,
        "recipient_rate": 0, "burn_rate": 0, "divide_rate": 100, "liquidity_rate": 0,
        "min_sharing": minimum,
    }


def j7_holder_fields(*, chain: Chain, dex: str, tax_bps: int | None = None,
                     reward_mint: str | None = None, reward_symbol: str | None = None,
                     minimum_holding_tokens: int = 10000) -> dict:
    """Public J7 platform fields only; NOT a signed request or a GMGN flag mapping.

Requires separate authorized J7 signing credentials, financial admission and verified
net-allocation/payout receipts. Never use as an automatic fallback from GMGN. SOL's
OTC reward mint must be independently verified against the J7 registry and chosen by
the operator; this function checks syntax, not admission or payout eligibility.
"""
    if chain is Chain.SOL and dex == "pump":
        if tax_bps is not None:
            raise RequirementUnavailable("otc_tax_rate_not_a_documented_field")
        if not isinstance(reward_mint, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", reward_mint):
            raise RequirementUnavailable("verified_otc_reward_mint_required")
        if not isinstance(reward_symbol, str) or not re.fullmatch(r"[A-Za-z0-9.]{1,20}", reward_symbol):
            raise RequirementUnavailable("otc_reward_symbol_required")
        return {"mode": "pump", "otc_reward_mint": reward_mint,
                "otc_reward_symbol": reward_symbol}
    if reward_mint is not None or reward_symbol is not None:
        raise RequirementUnavailable("otc_reward_fields_wrong_route")
    tax = _integer(tax_bps, "explicit_tax_bps", 1)
    if tax > 1000:
        raise RequirementUnavailable("tax_above_ten_percent")
    if chain is Chain.ROBINHOOD and dex == "pons":
        return {"mode": "pons", "pair": "eth", "creator_tax_bps": tax,
                "holder_dividends": True, "buyback_enabled": False}
    if chain in {Chain.BSC, Chain.ROBINHOOD} and dex == "flap":
        minimum = _integer(minimum_holding_tokens, "minimum_holding", 10000)
        return {"mode": "flap", "chain": "bnb" if chain is Chain.BSC else "robinhood",
                "pair": "native", "pve": False, "buy_tax_rate": tax, "sell_tax_rate": tax,
                "dividend_bps": 10000, "deflation_bps": 0, "lp_bps": 0,
                "dividend_token": "quote", "minimum_share_balance": minimum}
    raise RequirementUnavailable(f"j7_holder_route_unverified:{chain.value}/{dex}")


def remaining_increment(*, rung_start_atoms: int, pct_remaining: Decimal,
                        confirmed_sold_atoms: int, order_in_flight: bool) -> int:
    """Remaining quantity for ONE rung. Freeze rung_start at admission; book real fills.

An unresolved order sends nothing. A partial fill subtracts from that rung's frozen
target, rather than repeatedly taking the same percent of the shrinking bag. This does
not choose a price/time trigger or submit an order; existing watchdog owns both.
"""
    start = _integer(rung_start_atoms, "rung_start", 1)
    sold = _integer(confirmed_sold_atoms, "sold")
    if not isinstance(pct_remaining, Decimal) or not pct_remaining.is_finite() or not 0 < pct_remaining <= 100:
        raise RequirementUnavailable("invalid_sell_percentage")
    if type(order_in_flight) is not bool:
        raise RequirementUnavailable("invalid_in_flight_state")
    numerator, denominator = pct_remaining.as_integer_ratio()
    denominator *= 100
    target = (start * numerator + denominator - 1) // denominator
    if sold > target:
        raise RequirementUnavailable("rung_overfilled")
    return 0 if order_in_flight else target - sold
