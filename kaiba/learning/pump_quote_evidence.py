"""Identify the immediate quote asset of a Pump curve from retained account evidence."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from base58 import b58encode

from kaiba.ingest.launch_feed import PUMP_PROGRAM, pump_curve_address

# Pump IDL at 639b87fb45c3adeffa6f7c4a1bf0cb85e392bf22. The quote field
# follows the discriminator, five u64s, complete, creator and two bools.
CURVE_DISCRIMINATOR = bytes((23, 183, 248, 55, 96, 216, 172, 96))
QUOTE_START, QUOTE_END = 83, 115
MAX_ENCODED_BYTES = 4096


@dataclass(frozen=True, slots=True)
class PumpQuoteEvidence:
    kind: Literal["native_sol", "token", "unknown"]
    reason: str
    quote_mint: str | None = None
    context_slot: int | None = None


def assess_quote(mint: str, request: Any, response: Any) -> PumpQuoteEvidence:
    """Check a full base64 getAccountInfo pair before reading the quote mint.

    The caller must preserve the actual transport pairing and source identity.
    The reported slot comes from the response. No freshness, executable price,
    nested conversion path or execution permission is inferred from it.
    """
    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        return PumpQuoteEvidence("unknown", "rpc_response_unbound")
    expected_address = pump_curve_address(mint) if isinstance(mint, str) and 32 <= len(mint) <= 44 else None
    params = request.get("params")
    if (expected_address is None or request.get("method") != "getAccountInfo"
            or not isinstance(params, list) or len(params) != 2 or params[0] != expected_address):
        return PumpQuoteEvidence("unknown", "request_curve_address_unproved")
    options = params[1]
    if not isinstance(options, Mapping) or options.get("encoding") != "base64" or "dataSlice" in options:
        return PumpQuoteEvidence("unknown", "full_base64_account_required")
    request_id = request.get("id")
    response_id = response.get("id")
    if (request.get("jsonrpc") != "2.0" or response.get("jsonrpc") != "2.0"
            or type(request_id) not in (int, str) or type(response_id) is not type(request_id)
            or response_id != request_id or "error" in response):
        return PumpQuoteEvidence("unknown", "rpc_response_unbound")
    result = response.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("context"), Mapping):
        return PumpQuoteEvidence("unknown", "account_context_unavailable")
    slot = result["context"].get("slot")
    if type(slot) is not int or slot < 0:
        return PumpQuoteEvidence("unknown", "account_context_unavailable")
    min_slot = options.get("minContextSlot", 0)
    if type(min_slot) is not int or min_slot < 0 or slot < min_slot:
        return PumpQuoteEvidence("unknown", "account_context_below_requested_slot", context_slot=slot)
    value = result.get("value")
    if not isinstance(value, Mapping):
        return PumpQuoteEvidence("unknown", "account_unavailable", context_slot=slot)
    if value.get("owner") != PUMP_PROGRAM or value.get("executable") is not False:
        return PumpQuoteEvidence("unknown", "account_owner_or_type_unproved", context_slot=slot)
    encoded = value.get("data")
    if (not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64"
            or not isinstance(encoded[0], str) or len(encoded[0]) > MAX_ENCODED_BYTES):
        return PumpQuoteEvidence("unknown", "account_data_unreadable", context_slot=slot)
    try:
        raw = base64.b64decode(encoded[0], validate=True)
    except ValueError:
        return PumpQuoteEvidence("unknown", "account_data_unreadable", context_slot=slot)
    space = value.get("space", len(raw))
    if type(space) is not int or space != len(raw):
        return PumpQuoteEvidence("unknown", "account_data_size_mismatch", context_slot=slot)
    if not raw.startswith(CURVE_DISCRIMINATOR):
        return PumpQuoteEvidence("unknown", "curve_discriminator_mismatch", context_slot=slot)
    if len(raw) < QUOTE_END:
        return PumpQuoteEvidence("unknown", "quote_mint_field_unavailable", context_slot=slot)
    if any(raw[offset] > 1 for offset in (48, 81, 82)):
        return PumpQuoteEvidence("unknown", "curve_header_unreadable", context_slot=slot)
    quote = raw[QUOTE_START:QUOTE_END]
    if not any(quote):
        return PumpQuoteEvidence("native_sol", "default_quote_mint", context_slot=slot)
    return PumpQuoteEvidence("token", "explicit_quote_mint", b58encode(quote).decode("ascii"), slot)
