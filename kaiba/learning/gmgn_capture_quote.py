"""Read-only tax augmentation from the exact original selected GMGN capture.

Market values, component clocks, dossier queries, model and provider calls are
unchanged. Stored capsules prove trusted-local capture lineage, not wire truth,
route fees, actual paid costs or profitability. Importing this module activates
nothing and reads no capsule.
"""
# Root integration completion after delegated-agent interruption: preserve the
# capsule reference through the existing paper source field; no raw9 edits.
from __future__ import annotations

import copy
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.schemas import Chain, digest
from kaiba.learning import tax_quote
from kaiba.providers.gmgn_cli import normalize_security

VERSION = "gmgn_capture_quote/1"
MAX_CAPSULE_BYTES = 128 * 1024
MAX_TAX_TEXT = 128
_SHA = re.compile(r"[0-9a-f]{64}")


def _require(value):
    if not value:
        raise ValueError("original_capture_quote_binding_invalid")


def _integer(value, *, minimum=0):
    _require(type(value) is int and value >= minimum)
    return value


def _hash(value):
    _require(type(value) is str and _SHA.fullmatch(value))
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str) and len(value) > MAX_TAX_TEXT:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def _tax(payload, chain, side):
    """Use the existing explicit 0–1 tax-fraction normalizer, with no defaults."""
    value = normalize_security(payload, chain=Chain(chain)).get(side + "_tax_bps")
    number = _number(value)
    if number is None or number != number.to_integral_value() or not 0 <= number < 10_000:
        return None
    return int(number)


def _matched(existing, raw, chain, token, now, max_age):
    from kaiba.learning.gmgn_observations import validate_capsule
    from kaiba.learning.prospective_quotes import _token

    _require(type(existing) is dict and type(raw) is bytes and 0 < len(raw) <= MAX_CAPSULE_BYTES)
    _require(type(chain) is str and type(token) is str and Chain(chain).value == chain)
    _integer(now, minimum=1)
    _require(type(max_age) is int and 0 < max_age <= 120_000)
    _require(existing.get("chain") == chain and existing.get("token") == token
             and existing.get("basis") == "captured_gmgn_pool_depth" and not existing.get("unavailable_reason"))
    capsule = validate_capsule(raw)
    _require(type(capsule) is dict and capsule.get("target", {}).get("chain") == chain
             and capsule["target"].get("token") == token)
    data, cache, receipt = capsule["data"], capsule["cache"], capsule["receipt"]
    selected = existing.get("diagnostics", {}).get("cached_info")
    _require(type(data) is dict and type(cache) is dict and type(receipt) is dict and type(selected) is dict)
    fetched = _integer(cache.get("fetched_ms"), minimum=1)
    observed = _integer(receipt.get("observed_at_ms"), minimum=1)
    _require(selected.get("sha256") == _hash(cache.get("cache_sha256"))
             and _integer(selected.get("fetched_ms"), minimum=1) == fetched and fetched <= observed <= now)
    _require(receipt.get("provider") == "gmgn" and receipt.get("endpoint") == "token.info"
             and receipt.get("basis") in {"provider_reported", "cached"})
    request = f"gmgn-cli token info --address {token} --chain {chain} --raw"
    _require(receipt.get("request_digest") == digest(request)
             and receipt.get("response_digest") == _hash(cache.get("data_sha256")) == digest(data))
    _require(_token(chain, data.get("address")) == token)
    for block in (data, data.get("price"), data.get("dev"), data.get("link")):
        if isinstance(block, dict):
            _require(block.get("chain", chain) == chain
                     and ("address" not in block or _token(chain, block["address"]) == token))
    pool = data.get("pool")
    if isinstance(pool, dict):
        _require(pool.get("chain", chain) == chain)
        if pool.get("base_address") and pool.get("quote_address"):
            _require(token in {_token(chain, pool["base_address"]), _token(chain, pool["quote_address"])})
    _require(_integer(existing.get("price_ts_ms"), minimum=1) == fetched
             and _integer(existing.get("depth_ts_ms"), minimum=1) == fetched
             and fetched <= _integer(existing.get("quote_ts_ms"), minimum=1) <= now
             and 0 <= now - fetched <= max_age and 0 <= now - observed <= max_age)
    price = data.get("price")
    _require(type(price) is dict and _number(price.get("price")) is not None and _number(price["price"]) > 0
             and _number(existing.get("price_usd")) == _number(price["price"])
             and _number(data.get("liquidity")) is not None and _number(data["liquidity"]) > 0
             and _number(existing.get("liquidity_usd")) == _number(data["liquidity"]))
    if data.get("decimals") is not None:
        _require(type(data["decimals"]) is int and data["decimals"] == existing.get("decimals"))
    return capsule, hashlib.sha256(raw).hexdigest()


def augment(existing_quote: dict, capsule: bytes, chain: str, token: str, now_ms: int, *,
            max_age_ms: int = 60_000) -> dict:
    """Invalid/unmatched evidence delegates unchanged; conflicting valid tax stays unknown."""
    try:
        captured, capsule_sha = _matched(existing_quote, capsule, chain, token, now_ms, max_age_ms)
        data, receipt = captured["data"], captured["receipt"]
        values = {side: _tax(data, chain, side) for side in ("buy", "sell")}
    except (ImportError, ValueError, TypeError, KeyError, ArithmeticError, RecursionError, OverflowError):
        return existing_quote
    result = copy.deepcopy(existing_quote)
    components = {}
    for side, value in values.items():
        key, current = side + "_tax_bps", existing_quote.get(side + "_tax_bps")
        if value is None:
            components[side] = "capture_tax_unavailable"
        elif current is None:
            result[key], components[side] = value, "explicit_original_capture"
        elif _number(current) == value:
            components[side] = "agrees_with_existing_tax"
        else:
            result[key], components[side] = None, "conflict_tax_unknown"
    diagnostics = result.setdefault("diagnostics", {})
    diagnostics["gmgn_original_capture"] = {"version": VERSION, "capsule_sha256": capsule_sha,
        "cache_fetched_ms": captured["cache"]["fetched_ms"], "receipt_observed_ms": receipt["observed_at_ms"],
        "basis": receipt["basis"], "origin": "trusted_local_original_capture",
        "wire_verified": False, "venue_fee_inferred": False, "tax_components": components}
    result["source"] = (f"gmgn_original_capture/1:{capsule_sha}:{len(capsule)}:"
                        f"{receipt['observed_at_ms']}:{captured['cache']['fetched_ms']}")
    if any(value in {"explicit_original_capture", "agrees_with_existing_tax"} for value in components.values()):
        # Availability includes the original tax receipt. Market-print clocks stay
        # unchanged, so this cannot make an old print satisfy the entry delay.
        result["quote_ts_ms"] = max(result["quote_ts_ms"], receipt["observed_at_ms"])
    return result


def parse_capture_source(source: str) -> dict:
    """Recover an exact immutable reference from existing journalled fill source."""
    _require(type(source) is str and len(source) <= 192)
    match = re.fullmatch(r"gmgn_original_capture/1:([0-9a-f]{64}):([1-9][0-9]{0,5}):([1-9][0-9]{0,18}):([1-9][0-9]{0,18})", source)
    _require(match is not None)
    sha, size, observed, fetched = match.groups()
    size, observed, fetched = int(size), int(observed), int(fetched)
    _require(0 < size <= MAX_CAPSULE_BYTES and 0 < fetched <= observed <= 2**63 - 1)
    return {"reference": {"sha256": sha, "bytes": size},
            "receipt_observed_ms": observed, "cache_fetched_ms": fetched}


def quote_evidence(reader: Any, chain: str, token: str, *, now_ms: int, max_age_ms: int = 120_000,
                   cache_root=None, capsule_loader=None, capsule_reference=None) -> dict:
    """One unchanged market/dossier read; optional exact supplied capsule reference only."""
    existing = tax_quote.quote_evidence(reader, chain, token, now_ms=now_ms,
                                       max_age_ms=max_age_ms, cache_root=cache_root)
    if capsule_loader is None or capsule_reference is None or existing.get("unavailable_reason"):
        return existing
    capsule = capsule_loader(capsule_reference)  # SourceDeferred/I/O errors propagate; no retry/fallback.
    return augment(existing, capsule, chain, token, now_ms, max_age_ms=max_age_ms)


__all__ = ["augment", "quote_evidence", "parse_capture_source"]
