"""Bounded vendor market observations for canonical observed networks.

DexScreener reports pool prices and liquidity; it supplies neither executable
depth nor a swap quote, fee/tax evidence, holder analysis or a block-pinned state.
Unknown Elysium mainnet identity makes zero requests. No Chain enum or trade path
is extended. Board OBSERVED-MARKET-20261102, Codex, 2026-11-02.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from decimal import Decimal
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import OBSERVED_NETWORKS, EvidenceBasis, ObservedNetwork, now_ms
from kaiba.providers import dexscreener as dex
from kaiba.providers._http import get_json

SCHEMA = "kaiba.observed-market/1"
ENDPOINT = "research.observed_token_pairs"
TTL_S = 10.0
MAX_INPUT_PAIRS = 256
MAX_OUTPUT_PAIRS = 16
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_DEX = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


def _address(value: Any) -> str | None:
    if isinstance(value, str) and _ADDRESS.fullmatch(value.strip()):
        result = value.strip().lower()
        return result if result != "0x" + "0" * 40 else None
    return None


def _number(value: Any, *, positive: bool = False) -> str | None:
    # JSON numbers have already passed through a float. Decimal strings here
    # preserve the returned representation, not exact on-chain money.
    if isinstance(value, str) and len(value) > 128:
        return None
    if type(value) is int and value.bit_length() > 256:
        return None
    if isinstance(value, Decimal) and len(value.as_tuple().digits) > 128:
        return None
    result = dex.to_decimal(value)
    if result is None or result < 0 or (positive and result == 0):
        return None
    if result != 0 and not -40 <= result.adjusted() <= 40:
        return None  # bound exponent expansion, including adversarial 1e999999
    return format(result, "f")


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    return value if not any(unicodedata.category(c).startswith("C") for c in value) else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pair(raw: Any, token: str, at: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(raw, dict) or raw.get("chainId") != "hyperevm":
        return None, "pair_network_mismatch"
    address = _address(raw.get("pairAddress"))
    base, quote = _mapping(raw.get("baseToken")), _mapping(raw.get("quoteToken"))
    base_address, quote_address = _address(base.get("address")), _address(quote.get("address"))
    if not address or not base_address or not quote_address or base_address == quote_address:
        return None, "pair_identity_invalid"
    if token not in (base_address, quote_address):
        return None, "pair_token_mismatch"
    dex_id = raw.get("dexId")
    if not isinstance(dex_id, str) or not _DEX.fullmatch(dex_id):
        return None, "dex_identity_invalid"
    role = "base" if token == base_address else "quote"
    created = raw.get("pairCreatedAt")
    created = created if type(created) is int and 0 < created <= at else None
    liquidity, volume, txns = (_mapping(raw.get(k)) for k in ("liquidity", "volume", "txns"))
    volumes = {k: _number(volume.get(k)) for k in ("m5", "h1", "h6", "h24")}
    trades = {}
    for window in ("m5", "h1", "h6", "h24"):
        item = _mapping(txns.get(window))
        trades[window] = {k: v if type(v := item.get(k)) is int and 0 <= v < 2**53 else None
                          for k in ("buys", "sells")}
    return {
        "pair_address": address, "dex_id": dex_id,
        "url": f"https://dexscreener.com/hyperevm/{address}",
        "base_address": base_address, "quote_address": quote_address,
        "base_symbol": _text(base.get("symbol")), "quote_symbol": _text(quote.get("symbol")),
        "requested_token_role": role,
        # DexScreener's priceUsd, marketCap and fdv describe the BASE token.
        # A returned pool containing the requested token as quote is NOT its USD price.
        "requested_token_price_usd": _number(raw.get("priceUsd"), positive=True) if role == "base" else None,
        "base_token_price_usd": _number(raw.get("priceUsd"), positive=True),
        "pool_liquidity_usd": _number(liquidity.get("usd")),
        "requested_token_market_cap_usd": _number(raw.get("marketCap")) if role == "base" else None,
        "requested_token_fdv_usd": _number(raw.get("fdv")) if role == "base" else None,
        "pool_volume_usd": volumes, "pool_transactions": trades,
        "pair_created_ms": created, "market_as_of_ms": None,
    }, None


def token_market(network: str | ObservedNetwork, address: str, *, conn: Any = None,
                 at_ms: int | None = None) -> dict[str, Any]:
    """At most one keyless GET; cache first, no retry, no RPC, no executable-quote claim.

    Uses the existing DexScreener provider/cache/account-wide limiter at RESEARCH
    priority. The research cooldown family cannot freeze protection's price family.
    Receipt clocks retain original fetch time. The vendor's own update time is unknown.
    """
    out: dict[str, Any] = {
        "schema": SCHEMA, "network": None, "chain_id": None, "address": _address(address),
        "available": False, "pairs": [], "selected_price_pair": None,
        "response_pair_count": None, "accepted_pair_count": 0, "omitted_pair_count": 0,
        "rejected_pair_reasons": {}, "unknowns": [], "receipts": [],
        "method_attempts_upper_bound": 0, "price_range_usd": None,
        "selection_basis": "maximum_reported_pool_liquidity_among_base_token_price_rows",
        "evidence_basis": "vendor_reported_market_observation",
        "complete_market_snapshot": False, "atomic_snapshot": False,
        "execution_ready": False, "executable_quote": None, "executable_depth": None,
        "venue_fee_bps": None, "token_tax_bps": None, "holders": None,
        "safety_verified": None, "profitability_proven": None,
        "limitations": ["vendor_update_time_unknown", "reported_liquidity_is_not_executable_depth",
                        "prices_are_not_fillable_quotes", "json_numbers_are_not_onchain_atoms",
                        "no_holder_safety_fee_or_profit_evidence"],
    }
    try:
        observed = ObservedNetwork(network)
    except (ValueError, TypeError):
        out["unknowns"] = ["network_not_observed"]
        return out
    spec = OBSERVED_NETWORKS[observed]
    out.update(network=observed.value, chain_id=spec.chain_id)
    if spec.chain_id is None:
        out["unknowns"] = ["mainnet_identity_unverified"]
        return out
    if observed is not ObservedNetwork.HYPEREVM or type(spec.chain_id) is not int or spec.chain_id != 999:
        out["unknowns"] = ["vendor_network_mapping_unverified"]
        return out
    if out["address"] is None:
        out["unknowns"] = ["address_invalid"]
        return out
    got = get_json(dex.PROVIDER, ENDPOINT,
                   f"{dex.BASE_URL}/token-pairs/v1/hyperevm/{out['address']}",
                   priority=Priority.RESEARCH, ttl_s=TTL_S, stale_grace_s=0.0,
                   retries=0, timeout_s=5.0, conn=conn)
    receipt = got.receipt
    out["receipts"] = [receipt.model_dump(mode="json", exclude={"note"})]
    out["method_attempts_upper_bound"] = int(receipt.basis is not EvidenceBasis.CACHED)
    at = now_ms() if at_ms is None else at_ms
    if receipt.basis not in {EvidenceBasis.CACHED, EvidenceBasis.PROVIDER_REPORTED}:
        out["unknowns"] = ["market_evidence_unavailable"]
        return out
    if not 0 <= at - receipt.observed_at_ms <= TTL_S * 1000:
        out["unknowns"] = ["market_evidence_out_of_window"]
        return out
    if not isinstance(got.data, list):
        out["unknowns"] = ["market_response_not_pair_list"]
        return out
    out["response_pair_count"] = len(got.data)
    if len(got.data) > MAX_INPUT_PAIRS:
        out["unknowns"] = ["market_response_pair_cap"]
        return out
    pairs: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    reasons: Counter[str] = Counter()
    for raw in got.data:
        pair, reason = _pair(raw, out["address"], at)
        if pair is None:
            reasons[reason] += 1
            continue
        key = pair["pair_address"]
        if key in conflicts:
            continue
        if key in pairs and pairs[key] != pair:
            conflicts.add(key)
            pairs.pop(key)
            reasons["conflicting_duplicate_pair"] += 1
        else:
            pairs[key] = pair
    ordered = sorted(pairs.values(), key=lambda p: (
        Decimal(p["pool_liquidity_usd"]).copy_negate() if p["pool_liquidity_usd"] is not None else Decimal("Infinity"),
        p["pair_address"],
    ))
    priced = [p for p in ordered if p["requested_token_price_usd"] is not None]
    out.update(available=bool(ordered), pairs=ordered[:MAX_OUTPUT_PAIRS],
               accepted_pair_count=len(ordered), omitted_pair_count=max(0, len(ordered) - MAX_OUTPUT_PAIRS),
               rejected_pair_reasons=dict(reasons))
    if priced:
        out["selected_price_pair"] = priced[0]
        values = [Decimal(p["requested_token_price_usd"]) for p in priced]
        out["price_range_usd"] = {"minimum": str(min(values)), "maximum": str(max(values))}
    else:
        out["unknowns"].append("requested_token_price_unavailable")
    if not ordered:
        out["unknowns"].append("no_matching_pairs_in_response")
    return out
