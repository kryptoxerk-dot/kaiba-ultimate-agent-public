"""Offline evidence check for a Solana network fee quote on exact message bytes."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kaiba.execution.txwire import WireError, parse_sol_transaction

MAX_LEGACY_OR_V0_BYTES = 1232
MAX_RECEIPT_AGE_MS = 60_000


@dataclass(frozen=True, slots=True)
class NetworkFeeEvidence:
    reason: str
    network_fee_lamports: int | None = None
    message_sha256: str | None = None
    context_slot: int | None = None


def assess_network_fee(
    unsigned_tx: Any,
    request: Any,
    response: Any,
    *,
    fee_payer: str,
    received_ms: int,
    at_ms: int,
) -> NetworkFeeEvidence:
    """Bind a retained getFeeForMessage response to a legacy or v0 unsigned candidate.

    The caller supplies the transport-paired RPC objects and receive clock.
    A quote describes the network fee for the original message. It does not
    attest blockhash validity now, wallet effects, total mint cost, or whether
    a downstream wallet service will sign the same bytes.
    """
    if (type(received_ms) is not int or type(at_ms) is not int or received_ms <= 0
            or not 0 <= at_ms - received_ms <= MAX_RECEIPT_AGE_MS):
        return NetworkFeeEvidence("fee_receipt_clock_unavailable")
    max_encoded = 4 * ((MAX_LEGACY_OR_V0_BYTES + 2) // 3)
    if not isinstance(unsigned_tx, str) or not 0 < len(unsigned_tx) <= max_encoded:
        return NetworkFeeEvidence("unsigned_transaction_unreadable")
    try:
        raw = base64.b64decode(unsigned_tx, validate=True)
        if len(raw) > MAX_LEGACY_OR_V0_BYTES:
            raise WireError("unsupported_packet_size")
        parsed = parse_sol_transaction(raw)
    except ValueError:
        return NetworkFeeEvidence("unsigned_transaction_unreadable")
    if any(any(signature) for signature in parsed.signatures):
        return NetworkFeeEvidence("signature_slots_not_empty")
    if not isinstance(fee_payer, str) or parsed.message.account_keys[0] != fee_payer:
        return NetworkFeeEvidence("fee_payer_mismatch")

    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        return NetworkFeeEvidence("rpc_receipt_unbound")
    params = request.get("params")
    encoded_message = base64.b64encode(parsed.message_bytes).decode("ascii")
    if (request.get("method") != "getFeeForMessage" or not isinstance(params, list)
            or len(params) not in (1, 2) or params[0] != encoded_message):
        return NetworkFeeEvidence("fee_request_message_mismatch")
    options = params[1] if len(params) == 2 else {}
    if not isinstance(options, Mapping):
        return NetworkFeeEvidence("fee_request_options_unreadable")
    min_slot = options.get("minContextSlot", 0)
    if (type(min_slot) is not int or min_slot < 0
            or options.get("commitment", "finalized") not in ("processed", "confirmed", "finalized")):
        return NetworkFeeEvidence("fee_request_options_unreadable")
    request_id, response_id = request.get("id"), response.get("id")
    if (request.get("jsonrpc") != "2.0" or response.get("jsonrpc") != "2.0"
            or type(request_id) not in (int, str) or type(response_id) is not type(request_id)
            or response_id != request_id or "error" in response):
        return NetworkFeeEvidence("rpc_receipt_unbound")
    result = response.get("result")
    if not isinstance(result, Mapping) or not isinstance(result.get("context"), Mapping):
        return NetworkFeeEvidence("rpc_context_unavailable")
    slot = result["context"].get("slot")
    if type(slot) is not int or slot < min_slot:
        return NetworkFeeEvidence("rpc_context_unavailable")
    fee = result.get("value")
    if type(fee) is not int or not 0 <= fee < 2**64:
        return NetworkFeeEvidence("rpc_network_fee_unavailable", context_slot=slot)
    return NetworkFeeEvidence("network_fee_quoted", fee, hashlib.sha256(parsed.message_bytes).hexdigest(), slot)
