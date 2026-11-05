"""Decode captured Meteora accounts into bin inventory and gross fee entitlements.

Codex DLMM-REPLAY-20261029. Pure reads: no RPC, keys, database or transaction builder.
Layouts/math checked against MeteoraAg/dlmm-sdk af36dd53ab180783ccecda306926189a4700953c,
IDL SHA256 045c0f4af044be046b6a14b350077e4242e8ac026b64a855803dab30fcdd8b35.
Amounts are integer atoms before transfer fees. This is not an executable withdrawal quote.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

import base58

from kaiba.execution.fill_costs import find_program_address

PROGRAM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
SDK_REVISION = "af36dd53ab180783ccecda306926189a4700953c"
DISCRIMINATORS = {
    "pool": bytes([33, 11, 49, 98, 181, 101, 177, 13]),
    "position": bytes([117, 176, 212, 199, 245, 180, 133, 182]),
    "bins": bytes([92, 142, 92, 220, 5, 148, 70, 181]),
}
MAX_BINS = 1400
U64_MAX = 2**64 - 1


class DecodeError(ValueError):
    """Captured state is unsupported, incomplete or internally inconsistent."""


def public_key(value: Any) -> bytes:
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        raise DecodeError("invalid_public_key")
    try:
        raw = base58.b58decode(value)
    except ValueError as exc:
        raise DecodeError("invalid_public_key") from exc
    if len(raw) != 32 or base58.b58encode(raw).decode() != value:
        raise DecodeError("invalid_public_key")
    return raw


def _u(raw: bytes, offset: int, width: int = 8, *, signed: bool = False) -> int:
    if offset < 0 or offset + width > len(raw):
        raise DecodeError("truncated_account")
    return int.from_bytes(raw[offset:offset + width], "little", signed=signed)


def _key(raw: bytes, offset: int) -> str:
    return base58.b58encode(raw[offset:offset + 32]).decode()


def _raw(account: Any, kind: str, minimum: int, maximum: int) -> bytes:
    if not isinstance(account, dict) or account.get("owner") != PROGRAM or account.get("executable") is not False:
        raise DecodeError("account_owner_or_executable_invalid")
    data = account.get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64" or not isinstance(data[0], str):
        raise DecodeError("account_encoding_invalid")
    if len(data[0]) > (maximum + 2) // 3 * 4:
        raise DecodeError("account_size_invalid")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecodeError("account_encoding_invalid") from exc
    if not minimum <= len(raw) <= maximum or raw[:8] != DISCRIMINATORS[kind]:
        raise DecodeError("account_layout_invalid")
    return raw


def decode_pool(account: dict) -> dict:
    raw = _raw(account, "pool", 904, 904)
    if raw[882] not in (0, 1) or raw[880] not in (0, 1) or raw[881] not in (0, 1):
        raise DecodeError("unsupported_pool_version_or_token_program")
    if raw[35] not in (0, 1, 2) or raw[36] not in (0, 1) or raw[82] not in (0, 1):
        raise DecodeError("unsupported_pool_mode")
    if _u(raw, 80, 2) == 0:
        raise DecodeError("invalid_bin_step")
    return {
        "active_bin": _u(raw, 76, 4, signed=True), "bin_step": _u(raw, 80, 2),
        "version": raw[882], "status": raw[82], "function_type": raw[35],
        "collect_fee_mode": raw[36], "token_program_flags": [raw[880], raw[881]],
        "mints": [_key(raw, 88), _key(raw, 120)],
        "rewards_present": any(raw[o:o + 32] != bytes(32) for o in (264, 408)),
    }


def decode_position(account: dict) -> dict:
    raw = _raw(account, "position", 8120, 8120 + (MAX_BINS - 70) * 112)
    lo, hi = _u(raw, 7912, 4, signed=True), _u(raw, 7916, 4, signed=True)
    width = hi - lo + 1
    if not 1 <= width <= MAX_BINS or len(raw) != 8120 + max(0, width - 70) * 112 or raw[8033] not in (0, 1):
        raise DecodeError("unsupported_position_width_or_layout")
    bins = []
    for i in range(width):
        share = 72 + 16 * i if i < 70 else 8120 + (i - 70) * 112
        reward = 1192 + 48 * i if i < 70 else share + 16
        fee = 4552 + 48 * i if i < 70 else share + 64
        bins.append({"id": lo + i, "shares": _u(raw, share, 16),
                     "fee_checkpoint": [_u(raw, fee, 16), _u(raw, fee + 16, 16)],
                     "fee_pending": [_u(raw, fee + 32), _u(raw, fee + 40)],
                     "reward_pending": [_u(raw, reward + 32), _u(raw, reward + 40)]})
    # Owner identity is retained only as a change-detection hash, never a returned address.
    return {"pool": _key(raw, 8), "lower_bin": lo, "upper_bin": hi, "bins": bins,
            "claimed_fees": [_u(raw, 7928), _u(raw, 7936)], "updated_at": _u(raw, 7920, signed=True),
            "owner_fingerprint": hashlib.sha256(raw[40:72] + raw[8001:8033]).hexdigest(),
            "lock_release_point": _u(raw, 7992), "version": raw[8033]}


def bin_array_address(pool: str, index: int) -> str:
    if type(index) is not int or not -(2**31) <= index < 2**31:
        raise DecodeError("invalid_bin_array_index")
    return find_program_address([b"bin_array", public_key(pool), index.to_bytes(8, "little", signed=True)], PROGRAM)[0]


def decode_bins(account: dict) -> dict:
    raw = _raw(account, "bins", 10136, 10136)
    if raw[16] not in (0, 1, 2):
        raise DecodeError("unsupported_bin_array_version")
    index = _u(raw, 8, signed=True)
    bins = []
    for i in range(70):
        at = 56 + 144 * i
        bins.append({"id": index * 70 + i, "amounts": [_u(raw, at), _u(raw, at + 8)],
                     "price_q64": _u(raw, at + 16, 16), "supply": _u(raw, at + 32, 16),
                     "fee_growth": [_u(raw, at + 80, 16), _u(raw, at + 96, 16)]})
    return {"pool": _key(raw, 24), "index": index, "version": raw[16], "bins": bins}


def _uint(value: Any, name: str) -> int:
    if type(value) is not int or not 0 <= value < 2**64:
        raise DecodeError(name + "_invalid")
    return value


def snapshot(receipt: dict) -> dict:
    """Decode one captured finalized getMultipleAccounts response (one bank context).

    The RPC's owner/context claims are checked, not cryptographically authenticated. Missing
    accounts are unknown, not zero. No mint scaling, transfer fees, rewards or gas are inferred.
    """
    if not isinstance(receipt, dict) or receipt.get("schema") != "kaiba-dlmm-capture/1":
        raise DecodeError("capture_schema_invalid")
    request, response = receipt.get("request"), receipt.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise DecodeError("capture_envelope_invalid")
    params = request.get("params")
    if (request.get("jsonrpc") != "2.0" or type(request.get("id")) is not int or request["id"] <= 0 or
            request.get("method") != "getMultipleAccounts" or not isinstance(params, list) or len(params) != 2):
        raise DecodeError("capture_method_invalid")
    keys, options = params
    if (not isinstance(keys, list) or not 3 <= len(keys) <= 30 or
            any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys)):
        raise DecodeError("capture_keys_invalid")
    for key in keys:
        public_key(key)
    if not isinstance(options, dict) or options.get("encoding") != "base64" or options.get("commitment") != "finalized":
        raise DecodeError("capture_commitment_invalid")
    if (response.get("jsonrpc") != "2.0" or type(response.get("id")) is not int or
            response.get("id") != request["id"] or "error" in response):
        raise DecodeError("capture_response_invalid")
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
        raise DecodeError("capture_context_invalid")
    slot = _uint(result["context"].get("slot"), "slot")
    if slot < _uint(options.get("minContextSlot", 0), "minContextSlot"):
        raise DecodeError("capture_slot_regressed")
    values = result.get("value")
    if not isinstance(values, list) or len(values) != len(keys):
        raise DecodeError("capture_accounts_invalid")
    accounts = dict(zip(keys, values, strict=True))
    pool_key, position_key = receipt.get("pool"), receipt.get("position")
    public_key(pool_key)
    public_key(position_key)
    if pool_key == position_key or pool_key not in accounts or position_key not in accounts:
        raise DecodeError("capture_identity_invalid")
    pool = decode_pool(accounts[pool_key])
    position = decode_position(accounts[position_key])
    if position["pool"] != pool_key:
        raise DecodeError("position_pool_mismatch")
    start = _uint(receipt.get("started_ms"), "started_ms")
    end = _uint(receipt.get("received_ms"), "received_ms")
    if not 0 <= end - start <= 60_000:
        raise DecodeError("capture_clock_invalid")
    bins = {}
    for key, account in accounts.items():
        if key in (pool_key, position_key, *pool["mints"]):
            continue
        array = decode_bins(account)
        if array["pool"] != pool_key or bin_array_address(pool_key, array["index"]) != key:
            raise DecodeError("bin_array_identity_mismatch")
        for item in array["bins"]:
            if item["id"] in bins:
                raise DecodeError("duplicate_bin")
            bins[item["id"]] = item
    principal, fees, details = [0, 0], [0, 0], []
    for pbin in position["bins"]:
        bid, shares = pbin["id"], pbin["shares"]
        if bid not in bins:
            raise DecodeError("position_bin_missing")
        b = bins[bid]
        if shares > b["supply"]:
            raise DecodeError("position_share_exceeds_supply")
        amounts = [shares * n // b["supply"] if shares else 0 for n in b["amounts"]]
        claimable = []
        for i in range(2):
            delta = b["fee_growth"][i] - pbin["fee_checkpoint"][i]
            if shares and delta < 0:
                raise DecodeError("fee_growth_below_checkpoint")
            fee = pbin["fee_pending"][i] + (((shares >> 64) * delta) >> 64 if shares else 0)
            if not 0 <= fee <= U64_MAX:
                raise DecodeError("fee_overflow")
            claimable.append(fee)
            principal[i] += amounts[i]
            fees[i] += fee
            if principal[i] > U64_MAX or fees[i] > U64_MAX:
                raise DecodeError("position_amount_overflow")
        details.append({**pbin, "principal_atoms": amounts, "fee_atoms_gross": claimable})
    return {"schema": "kaiba-dlmm-state/1", "sdk_revision": SDK_REVISION, "slot": slot,
            "received_ms": end, "pool_address": pool_key, "position_address": position_key,
            "pool": pool, "position": position, "pool_bins": sorted(bins.values(), key=lambda b: b["id"]),
            "position_bins": details, "principal_atoms": principal, "fee_atoms_gross": fees,
            "chain_verified": False, "financial_execution": False, "strategy_promotable": False,
            "limitations": ["Finalized RPC state, not independent consensus verification.",
                            "Raw atoms before Token-2022 transfer fees or UI scaling; rewards excluded.",
                            "Snapshot entitlements do not establish trade, deposit or withdrawal history.",
                            "Gas, rent, execution costs and executable liquidation value are not measured."]}
