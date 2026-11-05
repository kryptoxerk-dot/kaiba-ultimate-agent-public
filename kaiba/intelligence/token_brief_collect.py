"""Bounded financial-read-only token research, separate from pure brief formatting.

Live collection uses Kaiba's GMGN allowlist and shared limiter. No trades, roster
updates, service changes or metadata URL fetches occur here. The provider helpers
do their normal cache/receipt/limiter accounting. Offline normalization imports no
database module and preserves request identity, source clocks and partial coverage.

Codex TOKEN-BRIEF-FEE-20261018: pool fees below are provider-reported components;
their normalization grants no route, execution-cost or profitability authority.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.intelligence.token_brief import CHAINS, SECTIONS, build_brief, render_brief

WAL_LIMIT_BYTES = 1_500_000_000
MAX_DEADLINE_S = 100
MAX_WALLETS = 20
MAX_PROFIT_WALLETS = 12
PAGE_LIMIT = 100
POOL_FEE_UNITS_SOURCE = (
    "https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-token/SKILL.md"
)
ENDPOINTS = {
    "token_info": "token.info",
    "token_security": "token.security",
    "holders": "token.holders",
    "traders": "token.traders",
    "buyer_profit_7d": "portfolio.profits",
    "buyer_profit_30d": "portfolio.profits",
    "developer_tokens": "portfolio.created_tokens",
}
_EVM = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_SOL = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}\Z")
_EXPLORERS = {
    "eth": "https://eth.blockscout.com/token/",
    "bsc": "https://bscscan.com/token/",
    "sol": "https://solscan.io/token/",
    "base": "https://basescan.org/token/",
}


def _number(value: Any, *, nonnegative: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or (nonnegative and result < 0):
        return None
    return result


def _scalar(value: Any, *, nonnegative: bool = False) -> str | None:
    result = _number(value, nonnegative=nonnegative)
    return str(result) if result is not None else None


def _reported_pool_fee_bps(value: Any) -> str | None:
    """Convert GMGN's percent field exactly, with bounded input and output sizes.

    ``pool.fee_ratio=0.1`` means 0.1%, hence 10 bps. Shifting the Decimal tuple
    avoids rounding under the caller's decimal context. This is a reported pool
    component, never an assertion of the fee applicable to a particular swap.
    """
    if type(value) not in (str, int, float, Decimal):
        return None
    if type(value) is int and not 0 <= value < 100:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite() or len(value.as_tuple().digits) > 64:
            return None
    text = str(value)
    if len(text) > 128 or re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", text
    ) is None:
        return None
    number = Decimal(text)
    sign, digits, exponent = number.as_tuple()
    if sign or len(digits) > 64 or abs(exponent) > 64 or not 0 <= number < 100:
        return None
    return "0" if number == 0 else format(Decimal((sign, digits, exponent + 2)), "f")


def _count(value: Any) -> int | None:
    result = _number(value, nonnegative=True)
    return int(result) if result is not None and result == result.to_integral_value() else None


def _timestamp(value: Any) -> int | None:
    result = _count(value)
    return result if result is not None and 0 < result <= 253_402_300_799_999 else None


def _seconds_ms(value: Any) -> int | None:
    result = _number(value, nonnegative=True)
    return _timestamp(result * 1000) if result is not None else None


def _address(value: Any, chain: str, *, strict: bool = False) -> str | None:
    if not isinstance(value, str) or (strict and value != value.strip()):
        return None
    value = value.strip()
    if not (_SOL if chain == "sol" else _EVM).fullmatch(value):
        return None
    return value if chain == "sol" else value.lower()


def _identity(chain: Any, address: Any) -> tuple[str, str]:
    if chain not in CHAINS:
        raise ValueError("explicit supported chain is required")
    result = _address(address, chain)
    if result is None:
        raise ValueError("a valid token address for the explicit chain is required")
    return chain, result


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1, "0", "1", "false", "true"):
        return value in (1, "1", "true")
    return None


def _receipt(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _unavailable(note: str) -> dict[str, Any]:
    return {"provider": "kaiba.token_brief_collect", "endpoint": "not_collected",
            "basis": "unavailable", "observed_at_ms": None, "note": note}


def _section(facts: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    return {"facts": facts, "receipt": copy.deepcopy(receipt)}


def _warn(warnings: list[str], reason: str) -> None:
    if reason not in warnings:
        warnings.append(reason)


def _context_identity(context: Any, chain: str, token: str) -> dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise ValueError("supplemental context must be an object")
    if _identity(context.get("chain"), context.get("address")) != (chain, token):
        raise ValueError("supplemental context chain/token identity mismatch")
    return copy.deepcopy(context)


def _protocols(
    context: dict[str, Any], chain: str, as_of_ms: int, warnings: list[str],
) -> dict[str, dict[str, Any]]:
    values = context.get("protocol_addresses", [])
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("vetted protocol_addresses must be a bounded list")
    out = {}
    for item in values:
        data = item if isinstance(item, dict) else {"address": item}
        address = _address(data.get("address"), chain, strict=True)
        if address is None or data.get("chain", chain) != chain:
            raise ValueError("invalid or cross-chain vetted protocol address")
        proof = _receipt(data.get("receipt", context.get("receipt")))
        observed = _timestamp(proof.get("observed_at_ms"))
        if observed is None or not proof.get("provider") or not proof.get("endpoint"):
            _warn(warnings, f"protocol_address:{address}:classification_undated_or_unsourced_not_used")
            continue
        if observed > as_of_ms:
            _warn(warnings, f"protocol_address:{address}:classification_future_excluded")
            continue
        if str(proof.get("basis", "")).lower() in {"", "unknown", "unavailable"}:
            _warn(warnings, f"protocol_address:{address}:classification_unavailable_not_used")
            continue
        out[address] = {
            "address": address, "label": data.get("label"),
            "classification_basis": "vetted_context",
            "receipt": proof,
        }
    return out


def _rows(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("list"), list):
        return payload["list"]
    return None


def _call_valid(
    call: Any, chain: str, token: str, as_of: int, warnings: list[str],
) -> dict[str, Any] | None:
    if not isinstance(call, dict) or call.get("kind") not in ENDPOINTS:
        _warn(warnings, "unrecognized_provider_call")
        return None
    kind = call["kind"]
    receipt = _receipt(call.get("receipt"))
    observed = _timestamp(receipt.get("observed_at_ms"))
    if receipt.get("provider") != "gmgn" or receipt.get("endpoint") != ENDPOINTS[kind]:
        _warn(warnings, f"{kind}:provider_or_endpoint_mismatch")
        return None
    if observed is None or observed > as_of:
        _warn(warnings, f"{kind}:source_time_unknown_or_future")
        return None
    if str(receipt.get("basis", "")).lower() not in {"provider_reported", "cached", "stale"}:
        _warn(warnings, f"{kind}:unavailable_or_unrecognized_source_basis")
        return None
    if call.get("chain", chain) != chain:
        _warn(warnings, f"{kind}:request_chain_mismatch")
        return None
    if "address" in call and _address(call["address"], chain, strict=True) != token:
        _warn(warnings, f"{kind}:request_token_mismatch")
        return None
    payload = call.get("payload", call.get("data"))
    if isinstance(payload, dict) and "data" in payload and "code" in payload:
        if payload["code"] not in (0, "0"):
            _warn(warnings, f"{kind}:provider_error_envelope")
            return None
        payload = payload["data"]
    if kind in {"token_info", "token_security"}:
        if not isinstance(payload, dict) or _address(payload.get("address"), chain, strict=True) != token:
            _warn(warnings, f"{kind}:response_token_mismatch_or_bad_shape")
            return None
        for block in (payload, *(payload.get(k) for k in ("price", "dev", "link"))):
            if not isinstance(block, dict):
                continue
            if block.get("chain", chain) != chain:
                _warn(warnings, f"{kind}:response_chain_mismatch")
                return None
            if "address" in block and _address(block["address"], chain, strict=True) != token:
                _warn(warnings, f"{kind}:nested_response_token_mismatch")
                return None
        pool = payload.get("pool")
        if isinstance(pool, dict):
            if pool.get("chain", chain) != chain:
                _warn(warnings, f"{kind}:pool_chain_mismatch")
                return None
            # pool.address can identify the pool, not the researched token.
            if "base_address" in pool and _address(pool["base_address"], chain, strict=True) != token:
                _warn(warnings, f"{kind}:pool_base_token_mismatch")
                return None
    elif kind == "developer_tokens":
        if not isinstance(payload, dict) or payload.get("chain", chain) != chain:
            _warn(warnings, f"{kind}:bad_shape_or_chain")
            return None
    elif _rows(payload) is None:
        _warn(warnings, f"{kind}:bad_list_shape")
        return None
    return {**copy.deepcopy(call), "payload": copy.deepcopy(payload), "receipt": receipt}


def _calls(
    bundle: dict[str, Any], chain: str, token: str, as_of: int, warnings: list[str],
) -> dict[str, dict[str, Any]]:
    raw = bundle.get("calls", [])
    if not isinstance(raw, list) or len(raw) > len(ENDPOINTS):
        raise ValueError("provider bundle must contain at most seven call records")
    result = {}
    duplicates = set()
    for call in raw:
        kind = call.get("kind") if isinstance(call, dict) else None
        if kind in result or kind in duplicates:
            duplicates.add(kind)
            result.pop(kind, None)
            _warn(warnings, f"{kind}:duplicate_call_ambiguous")
            continue
        validated = _call_valid(call, chain, token, as_of, warnings)
        if validated is not None:
            result[kind] = validated
    return result


def _fraction(value: Any) -> dict[str, Any]:
    parsed = _number(value, nonnegative=True)
    return {"value": str(parsed) if parsed is not None and parsed <= 1 else None,
            "unit": "fraction", "denominator": None,
            "denominator_basis": "provider fraction; supply denominator not independently verified"}


def _token_rows(
    call: dict[str, Any] | None, chain: str, protocols: dict[str, Any],
    warnings: list[str],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    if call is None:
        return None, []
    rows, excluded, seen = [], [], set()
    for raw in (_rows(call["payload"]) or [])[:PAGE_LIMIT]:
        if not isinstance(raw, dict):
            _warn(warnings, f"{call['kind']}:invalid_row")
            continue
        address = _address(raw.get("address") or raw.get("wallet_address"), chain, strict=True)
        if address is None or raw.get("chain", chain) != chain:
            _warn(warnings, f"{call['kind']}:row_chain_or_address_mismatch")
            continue
        if address in protocols:
            excluded.append({
                **protocols[address], "token_realized_usd": _scalar(raw.get("realized_profit")),
                "token_unrealized_usd": _scalar(raw.get("unrealized_profit")),
                "buy_usd": _scalar(raw.get("buy_volume_cur"), nonnegative=True),
                "holding_share": _fraction(raw.get("amount_percentage")),
                "token_pnl_basis": "provider reported; never independent trader profit evidence",
                "trade_receipt": copy.deepcopy(call["receipt"]),
            })
            continue
        if address in seen:
            _warn(warnings, f"{call['kind']}:duplicate_wallet_excluded")
            continue
        seen.add(address)
        rows.append({**raw, "address": address})
    return rows, excluded


def _history_rows(
    call: dict[str, Any] | None, period: str, chain: str, requested: set[str] | None,
    warnings: list[str],
) -> dict[str, dict[str, Any]]:
    if call is None:
        return {}
    if call.get("period", period) != period:
        _warn(warnings, f"buyer_profit_{period}:period_mismatch")
        return {}
    result, duplicates = {}, set()
    for row in (_rows(call["payload"]) or [])[:MAX_WALLETS]:
        if not isinstance(row, dict):
            continue
        wallet = _address(row.get("wallet_address"), chain, strict=True)
        if wallet is None or row.get("chain", chain) != chain:
            _warn(warnings, f"buyer_profit_{period}:row_chain_or_wallet_mismatch")
            continue
        if requested is not None and wallet not in requested:
            _warn(warnings, f"buyer_profit_{period}:unsolicited_wallet_excluded")
            continue
        if wallet in result or wallet in duplicates:
            result.pop(wallet, None)
            duplicates.add(wallet)
            _warn(warnings, f"buyer_profit_{period}:duplicate_wallet_ambiguous")
            continue
        profit = _number(row.get("realized_profit"))
        cost = _number(row.get("realized_profit_cost"), nonnegative=True)
        roi = profit / cost * 100 if profit is not None and cost is not None and cost > 0 else None
        result[wallet] = {
            "chain": chain, "address": wallet, "period": period,
            "realized_usd": _scalar(row.get("realized_profit")),
            "realized_cost_usd": _scalar(row.get("realized_profit_cost"), nonnegative=True),
            "roi_pct": str(roi) if roi is not None else None,
            "roi_basis": "derived reported realized profit / reported realized cost; fees unspecified",
            "unrealized_usd": _scalar(row.get("unrealized_profit")),
            "unrealized_cost_usd": _scalar(row.get("unrealized_profit_cost"), nonnegative=True),
            "all_time_realized_usd": _scalar(row.get("total_realized_profit")),
            "all_time_realized_cost_usd": _scalar(row.get("total_realized_profit_cost"), nonnegative=True),
            "all_time_total_profit_usd": _scalar(row.get("total_profit")),
            "buy_count": _count(row.get("buy")), "sell_count": _count(row.get("sell")),
            "net_profit_verified": False, "copy_profit_verified": False,
            "receipt": copy.deepcopy(call["receipt"]),
        }
    return result


def _supplement(
    raw: Any, label: str, chain: str, fallback_receipt: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("facts"), dict):
        return None
    if raw.get("chain", chain) != chain:
        return None
    return {"label": label, "chain": chain, "kind": "supplemental_source_observation",
            "facts": copy.deepcopy(raw["facts"]),
            "receipt": _receipt(raw.get("receipt", fallback_receipt))}


def normalize_provider_bundle(
    bundle: dict[str, Any], context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure normalization to build_brief input; never fetches or initializes a DB.

    The raw bundle has chain/address/as_of_ms and up to seven calls, each with
    kind/payload/receipt. Supplemental context is explicitly bound to the same
    chain/token. Its sections and wallet facts remain separate observations.
    """
    if not isinstance(bundle, dict):
        raise ValueError("provider bundle must be an object")
    chain, token = _identity(bundle.get("chain"), bundle.get("address"))
    as_of = _timestamp(bundle.get("as_of_ms"))
    if as_of is None:
        raise ValueError("provider bundle requires an explicit finite as_of_ms")
    context = _context_identity(context, chain, token)
    warnings: list[str] = []
    protocols = _protocols(context, chain, as_of, warnings)
    calls = _calls(bundle, chain, token, as_of, warnings)
    sections = {name: _section({}, _unavailable("section not collected")) for name in SECTIONS}
    info_call = calls.get("token_info")
    info = info_call["payload"] if info_call is not None else {}
    info_receipt = info_call["receipt"] if info_call is not None else _unavailable("token info unavailable")
    price = info.get("price") if isinstance(info.get("price"), dict) else {}
    stat = info.get("stat") if isinstance(info.get("stat"), dict) else {}
    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
    tags = info.get("wallet_tags_stat") if isinstance(info.get("wallet_tags_stat"), dict) else {}
    pool = info.get("pool") if isinstance(info.get("pool"), dict) else {}
    link = info.get("link") if isinstance(info.get("link"), dict) else {}
    unit_price = _number(price.get("price"), nonnegative=True)
    circulating = _number(info.get("circulating_supply"), nonnegative=True)
    supply = _number(info.get("total_supply"), nonnegative=True)
    market_cap = unit_price * circulating if unit_price is not None and circulating is not None else None
    fdv = unit_price * supply if unit_price is not None and supply is not None else None
    holder_count, stat_count = _count(info.get("holder_count")), _count(stat.get("holder_count"))
    market = {
        "symbol": info.get("symbol"), "name": info.get("name"),
        "price_usd": str(unit_price) if unit_price is not None else None,
        "market_cap_usd": str(market_cap) if market_cap is not None else None,
        "market_cap_basis": "derived price times provider circulating supply",
        "fdv_usd": str(fdv) if fdv is not None else None,
        "circulating_supply": str(circulating) if circulating is not None else None,
        "total_supply": str(supply) if supply is not None else None,
        "liquidity_usd": _scalar(info.get("liquidity"), nonnegative=True),
        "holder_count": holder_count, "stat_holder_count": stat_count,
        "holder_count_conflict": holder_count is not None and stat_count is not None and holder_count != stat_count,
        "launchpad": info.get("launchpad_platform") or info.get("launchpad"),
        "creation_ms": _seconds_ms(info.get("creation_timestamp")),
        "open_ms": _seconds_ms(info.get("open_timestamp")),
        "pool_address": info.get("biggest_pool_address"),
        "quote_address": pool.get("quote_address"), "quote_symbol": pool.get("quote_symbol"),
        "exchange": pool.get("exchange"), "gmgn_url": f"https://gmgn.ai/{chain}/token/{token}",
        "explorer_url": _EXPLORERS.get(chain, "") + token if chain in _EXPLORERS else None,
        "ath_market_cap_usd": None,
        "time_windows_basis": "provider reported trailing windows at receipt time",
    }
    for window in ("1m", "5m", "1h", "6h", "24h"):
        market[f"buy_{window}_count"] = _count(price.get(f"buys_{window}"))
        market[f"sell_{window}_count"] = _count(price.get(f"sells_{window}"))
        for field in ("volume", "buy_volume", "sell_volume"):
            market[f"{field}_{window}_usd"] = _scalar(price.get(f"{field}_{window}"), nonnegative=True)
    if market["holder_count_conflict"]:
        _warn(warnings, "holder_count:top_level_vs_stat_disagreement_retained")
    sections["market"] = _section(market, info_receipt)
    composition = {
        "category_counts_are_provider_claims": True,
        "category_counts_can_overlap": True,
        "category_counts_are_not_first20": True,
        "fresh_wallet_count": _count(tags.get("fresh_wallets")),
        "smart_wallet_count": _count(tags.get("smart_wallets")),
        "sniper_wallet_count": _count(tags.get("sniper_wallets")),
        "bundler_wallet_count": _count(tags.get("bundler_wallets")),
        "rat_trader_wallet_count": _count(tags.get("rat_trader_wallets")),
        "renowned_wallet_count": _count(tags.get("renowned_wallets")),
        "creator_wallet_count": _count(tags.get("creator_wallets")),
        "top10_share": _fraction(stat.get("top_10_holder_rate", dev.get("top_10_holder_rate"))),
        "creator_holding_share": _fraction(stat.get("creator_hold_rate")),
        "team_holding_share": _fraction(stat.get("dev_team_hold_rate")),
        "sniper_current_holding_share": _fraction(stat.get("top70_sniper_hold_rate")),
        "bundler_trading_volume_share": _fraction(stat.get("top_bundler_trader_percentage")),
        "bundler_trading_volume_window": "provider window unknown",
        "launch_bundle_share": None,
        "launch_bundle_coverage": "unavailable; current holdings are not launch purchase shares",
        "elite_wallet_count": None, "pro_wallet_count": None, "weighted_score": None,
    }
    holder_rows, excluded_holders = _token_rows(calls.get("holders"), chain, protocols, warnings)
    composition.update({
        "holder_sample_coverage": "partial_current_top_holders" if holder_rows is not None else "unavailable",
        "holder_sample_returned_count": len(_rows(calls["holders"]["payload"]) or []) if "holders" in calls else None,
        "holder_sample_wallet_count": len(holder_rows) if holder_rows is not None else None,
        "holder_sample": [{"chain": chain, "address": row["address"],
                          "holding_share": _fraction(row.get("amount_percentage")),
                          "holding_usd": _scalar(row.get("usd_value")),
                          "contract_classification": "not_independently_checked",
                          "provider_addr_type": row.get("addr_type")}
                         for row in (holder_rows or [])],
        "excluded_protocol_holders": excluded_holders,
        "holder_sample_receipt": calls["holders"]["receipt"] if "holders" in calls else None,
        "holder_page_cap_hit": len(_rows(calls["holders"]["payload"]) or []) >= PAGE_LIMIT if "holders" in calls else None,
        "holder_count_is_not_returned_page_length": True,
    })
    sections["holder_composition"] = _section(composition, info_receipt)
    sec_call = calls.get("token_security")
    sec = sec_call["payload"] if sec_call is not None else {}
    taxes = {}
    for side in ("buy", "sell"):
        tax = _number(sec.get(f"{side}_tax"), nonnegative=True)
        taxes[f"token_{side}_tax_bps"] = str(tax * 10000) if tax is not None and tax <= 1 else None
    pool_fee_bps = _reported_pool_fee_bps(pool.get("fee_ratio"))
    sections["security"] = _section({
        **taxes, "honeypot_reported": _bool(sec.get("is_honeypot")),
        "open_source_reported": _bool(sec.get("is_open_source")),
        "blacklist_reported": _bool(sec.get("is_blacklist")),
        "can_not_sell_reported": _bool(sec.get("can_not_sell")),
        "provider_can_sell_code": sec.get("can_sell"),
        "sellability_independently_verified": None,
        "sol_mint_authority_revoked": _bool(sec.get("renounced_mint")) if chain == "sol" else None,
        "sol_freeze_authority_revoked": _bool(sec.get("renounced_freeze_account")) if chain == "sol" else None,
        "sol_authority_fields_applicable": chain == "sol",
        "provider_pool_fee_ratio_raw": pool.get("fee_ratio"),
        "provider_pool_fee_bps": pool_fee_bps,
        "provider_pool_fee_basis": "provider_reported_component" if pool_fee_bps is not None else "unavailable",
        "provider_pool_fee_receipt": copy.deepcopy(info_receipt),
        "provider_pool_fee_input_unit": "percent",
        "provider_pool_fee_output_unit": "basis_points",
        "provider_pool_fee_units_source": POOL_FEE_UNITS_SOURCE,
        "provider_pool_fee_zero_reported": pool_fee_bps == "0" if pool_fee_bps is not None else None,
        "provider_pool_fee_zero_caveat": "reported zero does not verify zero applicable trading cost",
        "pool_fee_current_route_verified": None,
        "pool_fee_total_cost_complete": None,
        "token_transfer_tax_is_not_pool_or_hook_fee": True,
        "fees_and_route_require_separate_receipts": True,
    }, sec_call["receipt"] if sec_call is not None else _unavailable("token security unavailable"))
    requested_raw = bundle.get("requested_wallets")
    requested = None
    if requested_raw is not None:
        if not isinstance(requested_raw, list) or len(requested_raw) > MAX_WALLETS:
            raise ValueError("requested_wallets must be a bounded list")
        parsed = [_address(value, chain, strict=True) for value in requested_raw]
        if any(value is None for value in parsed):
            raise ValueError("invalid requested wallet identity")
        requested = set(parsed)
    histories = {period: _history_rows(calls.get(f"buyer_profit_{period}"), period, chain, requested, warnings)
                 for period in ("7d", "30d")}
    trader_rows, excluded_buyers = _token_rows(calls.get("traders"), chain, protocols, warnings)
    buyers = []
    wallet_context = context.get("wallet_context", {})
    if not isinstance(wallet_context, dict) or len(wallet_context) > 100:
        raise ValueError("wallet_context must be a bounded keyed object")
    context_by_wallet = {_address(key, chain, strict=True): value for key, value in wallet_context.items()}
    if None in context_by_wallet:
        raise ValueError("invalid wallet_context address")
    for row in trader_rows or []:
        address = row["address"]
        history = {period: values[address] for period, values in histories.items() if address in values}
        buyer = {
            "chain": chain, "address": address,
            "buy_usd": _scalar(row.get("buy_volume_cur"), nonnegative=True),
            "sell_usd": _scalar(row.get("sell_volume_cur"), nonnegative=True),
            "current_position_cost_usd": _scalar(row.get("cost_cur"), nonnegative=True),
            "token_realized_usd": _scalar(row.get("realized_profit")),
            "token_unrealized_usd": _scalar(row.get("unrealized_profit")),
            "provider_netflow_usd": _scalar(row.get("netflow_usd")),
            "provider_netflow_sign_unchanged": True,
            "buy_count": _count(row.get("buy_tx_count_cur")),
            "sell_count": _count(row.get("sell_tx_count_cur")),
            "holding_start_ms": _seconds_ms(row.get("start_holding_at")),
            "last_active_ms": _seconds_ms(row.get("last_active_timestamp")),
            "tags": [*(row.get("tags") if isinstance(row.get("tags"), list) else []),
                     *(row.get("maker_token_tags") if isinstance(row.get("maker_token_tags"), list) else [])],
            "identity": {"handle": row.get("twitter_username") or row.get("name"), "basis": "provider_reported"},
            "provider_addr_type": row.get("addr_type"),
            "contract_classification": "not_independently_checked",
            "wallet_histories": history,
            "wallet_history": history.get("7d"),
            "receipt": copy.deepcopy(calls["traders"]["receipt"]),
            "net_profit_verified": False, "copy_profit_verified": False,
            "token_pnl_basis": "provider reported; fees may already be included; no independent cashflow audit",
        }
        supplemental = _supplement(context_by_wallet.get(address), "vetted wallet context", chain, context.get("receipt"))
        if supplemental is not None:
            buyer["supplemental_observations"] = [supplemental]
        buyers.append(buyer)
    crosschecks = {period: list(values.values()) for period, values in histories.items()}
    sections["buyers"] = _section({
        "rows": buyers if trader_rows is not None else None, "ordering": "current_top_traders",
        "coverage": "partial_current_top_traders" if trader_rows is not None else "unavailable",
        "limit": PAGE_LIMIT, "total_count": None,
        "provider_returned_count": len(_rows(calls["traders"]["payload"]) or []) if "traders" in calls else None,
        "trader_page_cap_hit": len(_rows(calls["traders"]["payload"]) or []) >= PAGE_LIMIT if "traders" in calls else None,
        "excluded_protocol_buyers": excluded_buyers,
        "remaining_contract_classification": "not independently checked; provider addr_type0 does not prove EOA",
        "wallet_crosschecks": crosschecks, "requested_wallet_roster_known": requested is not None,
        "requested_wallets_unanswered": {
            period: sorted(requested - set(values)) if requested is not None else None
            for period, values in histories.items()
        },
        "wallet_crosschecks_not_top_trader_rows": {
            period: sorted(set(values) - {row["address"] for row in buyers})
            for period, values in histories.items()
        },
    }, calls["traders"]["receipt"] if "traders" in calls else _unavailable("top traders unavailable"))
    sections["early_buyers"] = _section({
        "rows": None, "coverage": "unavailable", "ordering": None,
        "first20_fresh_count": None, "first20_dormant_count": None,
        "first20_common_count": None, "first20_fomo_count": None,
        "reason": "chronological full coverage from launch and wallet-age classifications were not collected",
    }, _unavailable("first20 chronological buyers unproven"))
    creator = _address(dev.get("creator_address"), chain, strict=True)
    developer_call = calls.get("developer_tokens")
    created = developer_call["payload"] if developer_call is not None else {}
    created_ath = created.get("creator_ath_info") if isinstance(created.get("creator_ath_info"), dict) else {}
    reported_creator = _address(created_ath.get("creator"), chain, strict=True)
    if developer_call is not None and (creator is None or (reported_creator is not None and reported_creator != creator)):
        _warn(warnings, "developer_tokens:creator_identity_mismatch_or_unknown")
        developer_call, created, created_ath = None, {}, {}
    listed = created.get("tokens") if isinstance(created.get("tokens"), list) else None
    valid_listed = [
        row for row in (listed or [])[:100]
        if isinstance(row, dict) and row.get("chain", chain) == chain
        and _address(row.get("token_address"), chain, strict=True) is not None
    ]
    own_token = next((row for row in valid_listed if _address(row["token_address"], chain) == token), None)
    if own_token is not None:
        market["ath_market_cap_usd"] = _scalar(own_token.get("token_ath_mc"), nonnegative=True)
        market["ath_market_cap_basis"] = "provider-reported target token in developer listing"
        market["ath_market_cap_receipt"] = developer_call["receipt"]
    inner, opened = _count(created.get("inner_count")), _count(created.get("open_count"))
    sections["developer"] = _section({
        "creator_address": creator, "creator_receipt": info_receipt,
        "creator_hold_share": _fraction(stat.get("creator_hold_rate")),
        "creator_team_hold_share": _fraction(stat.get("dev_team_hold_rate")),
        "reported_launch_count": inner + opened if inner is not None and opened is not None else None,
        "reported_inner_count": inner, "reported_open_count": opened,
        "listed_launch_count": len(valid_listed) if listed is not None else None,
        "listed_launches_coverage": "capped provider listing at most100; lifetime count is separate",
        "creator_best_ath_market_cap_usd": _scalar(created_ath.get("ath_mc"), nonnegative=True),
        "creator_best_ath_token": created_ath.get("ath_token"),
        "creator_best_ath_is_current_token": (
            _address(created_ath.get("ath_token"), chain) == token if created_ath.get("ath_token") else None
        ),
        "provider_funding_label": dev.get("fund_from") or None,
        "first_ever_funding_verified": False, "human_identity_verified": False,
    }, developer_call["receipt"] if developer_call is not None else info_receipt)
    sections["social"] = _section({
        "twitter_handle": link.get("twitter_username"), "website": link.get("website") or None,
        "description": link.get("description") or None, "logo_url": info.get("logo"),
        "profile_url": f"https://x.com/{link['twitter_username']}" if link.get("twitter_username") else None,
        "identity_control_verified": False, "metadata_urls_fetched": False,
        "metadata_is_untrusted_source_content": True,
    }, info_receipt)
    sections["calls"] = _section({
        "verified_call_count": None, "resolved_profitable_call_count": None,
        "reported_signal_count": _count(stat.get("signal_count")),
        "reported_degen_call_count": _count(stat.get("degen_call_count")),
        "reason": "no independently verified caller outcomes or executable net returns collected",
    }, info_receipt)
    supplemental_sections = context.get("sections", {})
    if not isinstance(supplemental_sections, dict):
        raise ValueError("context sections must be an object")
    for name, value in supplemental_sections.items():
        if name not in SECTIONS:
            _warn(warnings, f"context:{name}:unknown_section")
            continue
        observation = _supplement(value, f"vetted supplemental {name}", chain, context.get("receipt"))
        if observation is not None:
            sections[name]["facts"].setdefault("supplemental_observations", []).append(observation)
    sections["coverage"] = _section({
        "collection_status": "partial" if bundle.get("deferred") or bundle.get("stop_reason") else "reported",
        "provider_calls_recorded_count": len(bundle.get("calls", [])),
        "validated_provider_calls_count": len(calls),
        "missing_provider_calls": [kind for kind in ENDPOINTS if kind not in calls],
        "normalization_warnings": warnings,
        "source_receipts": {kind: copy.deepcopy(call.get("receipt")) for kind, call in calls.items()},
        "deferred": copy.deepcopy(bundle.get("deferred")),
        "stop_reason": bundle.get("stop_reason"),
        "financial_execution": False, "profitability_proven": False,
        "protocol_classification_context_required": True,
        "first20_chronological_coverage_verified": False,
        "list_limits_are_partial_samples": True,
    }, {"provider": "kaiba.token_brief_collect", "endpoint": "bundle.coverage",
        "basis": "derived", "observed_at_ms": as_of})
    # Coverage supplement must survive the derived coverage section construction.
    extra = _supplement(supplemental_sections.get("coverage"), "vetted supplemental coverage", chain, context.get("receipt"))
    if extra is not None:
        sections["coverage"]["facts"]["supplemental_observations"] = [extra]
    return {"chain": chain, "address": token, "as_of_ms": as_of, "sections": sections}


def _wallets(values: Sequence[str], chain: str, max_wallets: int) -> list[str]:
    if isinstance(max_wallets, bool) or not isinstance(max_wallets, int) or not 1 <= max_wallets <= MAX_WALLETS:
        raise ValueError("max_wallets must be1..20")
    if isinstance(values, str) or len(values) > MAX_WALLETS:
        raise ValueError("at most20 explicit wallet addresses may be requested")
    parsed = [_address(value, chain) for value in values]
    if any(value is None for value in parsed):
        raise ValueError("invalid wallet address for the requested chain")
    return list(dict.fromkeys(parsed))[:min(max_wallets, MAX_PROFIT_WALLETS)]


def _wal_bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _config_check(provider: Any, timeout_s: float) -> dict[str, Any]:
    base = provider.cli_argv()
    if not base:
        return {"available": False, "reason": "gmgn_cli_not_installed"}
    env = os.environ.copy()
    env["GMGN_ALLOW_AUTOMATED_TRADES"] = "0"
    env["NO_UPDATE_NOTIFIER"] = "1"
    env.pop("GMGN_DEBUG", None)
    try:
        result = subprocess.run(
            [*base, "config", "--check"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, env=env, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    # CLI stdout/stderr may include credential diagnostics. Keep only the result.
    return {"exit_code": result.returncode, "available": result.returncode == 0}


def collect_provider_bundle(
    chain: str, address: str, *, wallets: Sequence[str] = (), max_wallets: int = 12,
    context: dict[str, Any] | None = None, deadline_s: float = MAX_DEADLINE_S,
    provider: Any = None, db_path: Path | None = None,
) -> dict[str, Any]:
    """Collect at most seven allowlisted reads; stop on any unavailable result.

    Caller must run production use under nice19/ionice3 and a hard timeout. This
    function adds a100s command budget and a WAL check before every call. It closes
    Kaiba's thread connection after each provider attempt and never retries or sleeps.
    """
    chain, token = _identity(chain, address)
    if isinstance(deadline_s, bool) or not math.isfinite(deadline_s) or not 0 < deadline_s <= MAX_DEADLINE_S:
        raise ValueError("deadline_s must be finite and at most100s")
    selected = _wallets(wallets, chain, max_wallets)
    context = _context_identity(context, chain, token)
    protocol_warnings: list[str] = []
    protocols = _protocols(context, chain, int(time.time() * 1000), protocol_warnings)
    # Imports are deliberately inside live collection, never the offline normalizer.
    from kaiba.core.config import get_settings
    from kaiba.core.db import close_thread_conn
    from kaiba.core.limiter import Priority
    from kaiba.providers import gmgn_cli

    provider = gmgn_cli if provider is None else provider
    wal = Path(str(db_path if db_path is not None else get_settings().db_path) + "-wal")
    started = time.monotonic()
    deadline = started + deadline_s
    bundle: dict[str, Any] = {
        "chain": chain, "address": token, "started_ms": int(time.time() * 1000),
        "calls": [], "requested_wallets": selected, "max_profit_wallets": min(max_wallets, MAX_PROFIT_WALLETS),
        "explicit_wallets_requested_count": len(wallets), "deadline_s": deadline_s,
        "protocol_classification_warnings": protocol_warnings,
    }

    def guard() -> tuple[int, float] | None:
        remaining = deadline - time.monotonic()
        try:
            size = _wal_bytes(wal)
        except OSError as exc:
            bundle["deferred"] = {"reason": "wal_stat_unavailable", "detail": type(exc).__name__}
            return None
        if size > WAL_LIMIT_BYTES:
            bundle["deferred"] = {"reason": "wal_over_1_5GB", "wal_bytes": size}
            return None
        if remaining <= 0:
            bundle["deferred"] = {"reason": "command_deadline"}
            return None
        return size, remaining

    def attempt(kind: str, method: str, *args: Any, **extra: Any) -> bool:
        budget = guard()
        if budget is None:
            return False
        size, remaining = budget
        begin = time.monotonic()
        try:
            result = getattr(provider, method)(
                *args, priority=Priority.RESEARCH, ttl_s=0, stale_grace_s=0,
                timeout_s=min(12.0, remaining), wait_for_slot_s=0, **extra,
            )
            record = {
                "kind": kind, "chain": chain, "address": token,
                "payload": copy.deepcopy(result.data), "receipt": _receipt(result.receipt),
                "wal_bytes": size, "elapsed_s": round(time.monotonic() - begin, 4),
            }
            if kind.startswith("buyer_profit_"):
                record.update(period=extra["period"], requested_wallets=list(selected))
            if kind == "developer_tokens":
                record["requested_creator"] = args[0]
            bundle["calls"].append(record)
            if not result.ok:
                bundle.update(stopped_after_unavailable=True, stop_reason=f"{kind}:provider_unavailable")
                return False
            check_warnings: list[str] = []
            validated = _call_valid(record, chain, token, int(time.time() * 1000), check_warnings)
            if validated is None:
                bundle.update(stopped_after_unavailable=True, stop_reason=f"{kind}:identity_or_receipt_invalid",
                              validation_warnings=check_warnings)
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - retain failure type, never credential diagnostics
            bundle["calls"].append({
                "kind": kind, "chain": chain, "address": token, "payload": None,
                "receipt": {"provider": "gmgn", "endpoint": ENDPOINTS[kind],
                            "basis": "unavailable", "observed_at_ms": int(time.time() * 1000),
                            "note": type(exc).__name__},
                "wal_bytes": size, "elapsed_s": round(time.monotonic() - begin, 4),
            })
            bundle.update(stopped_after_unavailable=True, stop_reason=f"{kind}:{type(exc).__name__}")
            return False
        finally:
            close_thread_conn()

    budget = guard()
    if budget is not None:
        try:
            bundle["config_check"] = _config_check(provider, min(10.0, budget[1]))
        finally:
            close_thread_conn()
        if not bundle["config_check"].get("available"):
            bundle["stop_reason"] = "config_check_unavailable"
        else:
            base = (
                ("token_info", "token_info", {}),
                ("token_security", "token_security", {}),
                ("holders", "token_holders", {"limit": PAGE_LIMIT, "order_by": "amount_percentage", "direction": "desc"}),
                ("traders", "token_traders", {"limit": PAGE_LIMIT, "order_by": "buy_volume_cur", "direction": "desc"}),
            )
            complete = all(attempt(kind, method, token, chain, **kw) for kind, method, kw in base)
            if complete:
                if not selected:
                    rows, _ = _token_rows(bundle["calls"][-1], chain, protocols, [])
                    selected = [row["address"] for row in (rows or [])][:min(max_wallets, MAX_PROFIT_WALLETS)]
                    bundle["requested_wallets"] = selected
                    bundle["wallet_selection_basis"] = "first valid nonprotocol rows in requested buy-volume order"
                else:
                    bundle["wallet_selection_basis"] = "explicit requested addresses, no profit reselection"
                profits_ok = True
                for period in ("7d", "30d"):
                    if selected and not attempt(f"buyer_profit_{period}", "portfolio_profits", selected, chain, period=period):
                        profits_ok = False
                        break
                if profits_ok:
                    info = bundle["calls"][0]["payload"]
                    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
                    creator = _address(dev.get("creator_address") or info.get("creator_address"), chain, strict=True)
                    if creator is not None:
                        attempt("developer_tokens", "portfolio_created_tokens", creator, chain)
                    else:
                        bundle["developer_skipped_reason"] = "creator_unknown"
    bundle["as_of_ms"] = int(time.time() * 1000)
    bundle["elapsed_s"] = round(time.monotonic() - started, 4)
    close_thread_conn()
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--chain", required=True, choices=sorted(CHAINS))
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Structured brief JSON")
    parser.add_argument("--markdown", type=Path, required=True, help="Rendered brief Markdown")
    parser.add_argument("--input", type=Path, help="Saved provider bundle; no live calls")
    parser.add_argument("--context", type=Path, help="Vetted same-chain/token supplemental context")
    parser.add_argument("--wallet", action="append", default=[])
    parser.add_argument("--max-wallets", type=int, default=12)
    parser.add_argument("--deadline-s", type=float, default=MAX_DEADLINE_S)
    args = parser.parse_args(argv)
    try:
        chain, token = _identity(args.chain, args.address)
        selected = _wallets(args.wallet, chain, args.max_wallets)
        context = json.loads(args.context.read_text(encoding="utf-8-sig")) if args.context else None
        destinations = [args.output.resolve(), args.markdown.resolve()]
        inputs = {p.resolve() for p in (args.input, args.context) if p is not None}
        if len(set(destinations)) != 2 or any(p in inputs for p in destinations):
            raise ValueError("output files must be distinct from each other and input/context")
        if args.input:
            bundle = json.loads(args.input.read_text(encoding="utf-8-sig"))
            if _identity(bundle.get("chain"), bundle.get("address")) != (chain, token):
                raise ValueError("CLI chain/token differs from saved provider bundle")
            if selected:
                bundle["requested_wallets"] = selected
        else:
            bundle = collect_provider_bundle(
                chain, token, wallets=args.wallet, max_wallets=args.max_wallets,
                context=context, deadline_s=args.deadline_s,
            )
        brief = build_brief(normalize_provider_bundle(bundle, context))
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(brief, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        args.markdown.write_text(render_brief(brief) + "\n", encoding="utf-8")
        print(json.dumps({"chain": chain, "address": token, "provider_calls": len(bundle.get("calls", [])),
                          "offline": bool(args.input), "deferred": bundle.get("deferred"),
                          "stop_reason": bundle.get("stop_reason"),
                          "json": str(args.output), "markdown": str(args.markdown)}))
        return 0 if not bundle.get("deferred") and not bundle.get("stop_reason") else 3
    except (OSError, ValueError, TypeError) as exc:
        # Avoid echoing arbitrary raw provider, URL, credential or context diagnostics.
        print(f"Token brief collection rejected: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
