from __future__ import annotations

import base64
import struct

import pytest
from base58 import b58decode, b58encode

from kaiba.learning.pump_quote_evidence import assess_quote

MINT = b58encode(bytes(range(1, 33))).decode()
# Independent witness from fill_costs.find_program_address for the synthetic mint above.
CURVE = "HycbmCe1QKCrobbdjZf7NGicxYPV6NRCQZYwGJv8u54d"
PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN = bytes(range(32, 0, -1))
WSOL = "So11111111111111111111111111111111111111112"


def curve_bytes(quote: bytes) -> bytes:
    return bytes.fromhex("17b7f83760d8ac60") + struct.pack(
        "<QQQQQ?32s??32s",
        1_073_000_000_000_000, 31_000_000_000, 793_100_000_000_000, 1_000_000_000,
        1_000_000_000_000_000, False, b"\x7f" * 32, True, False, quote,
    )


def receipt(raw: bytes) -> tuple[dict, dict]:
    request = {"jsonrpc": "2.0", "id": 5, "method": "getAccountInfo",
               "params": [CURVE, {"encoding": "base64", "commitment": "finalized"}]}
    value = {"owner": PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"],
             "space": len(raw), "executable": False}
    response = {"jsonrpc": "2.0", "id": 5, "result": {"context": {"slot": 123}, "value": value}}
    return request, response


@pytest.mark.parametrize("quote, expected_kind", [(bytes(32), "native_sol"), (TOKEN, "token"),
                                                   (b58decode(WSOL), "token")])
def test_quote_identity_survives_following_layout_fields(quote, expected_kind):
    raw = curve_bytes(quote) + bytes(range(51))
    found = assess_quote(MINT, *receipt(raw))
    assert found.kind == expected_kind
    assert found.quote_mint == (b58encode(quote).decode() if expected_kind == "token" else None)
    assert found.context_slot == 123


@pytest.mark.parametrize("size", [49, 81, 82, 83, 114])
def test_legacy_or_short_account_is_not_native_evidence(size):
    found = assess_quote(MINT, *receipt(curve_bytes(bytes(32))[:size]))
    assert (found.kind, found.reason) == ("unknown", "quote_mint_field_unavailable")


def test_owner_and_account_type_are_required_even_with_zero_quote_bytes():
    request, response = receipt(curve_bytes(bytes(32)))
    response["result"]["value"]["owner"] = WSOL
    assert assess_quote(MINT, request, response).reason == "account_owner_or_type_unproved"
    response["result"]["value"].update(owner=PROGRAM, executable=True)
    assert assess_quote(MINT, request, response).reason == "account_owner_or_type_unproved"


def test_discriminator_and_borsh_bools_are_checked():
    raw = bytearray(curve_bytes(bytes(32)))
    raw[0] ^= 1
    assert assess_quote(MINT, *receipt(raw)).reason == "curve_discriminator_mismatch"
    raw[0] ^= 1
    raw[48] = 2
    assert assess_quote(MINT, *receipt(raw)).reason == "curve_header_unreadable"


def test_request_targets_exact_curve_and_is_not_sliced():
    request, response = receipt(curve_bytes(bytes(32)))
    assert assess_quote(WSOL, request, response).reason == "request_curve_address_unproved"
    assert assess_quote("bad-key", request, response).reason == "request_curve_address_unproved"
    request["params"][1]["dataSlice"] = {"offset": 0, "length": 115}
    assert assess_quote(MINT, request, response).reason == "full_base64_account_required"


def test_response_id_binds_without_type_coercion_and_context_matches_request():
    request, response = receipt(curve_bytes(bytes(32)))
    response["id"] = "5"
    assert assess_quote(MINT, request, response).reason == "rpc_response_unbound"
    response["id"] = 5
    request["params"][1]["minContextSlot"] = 124
    assert assess_quote(MINT, request, response).reason == "account_context_below_requested_slot"
    response["result"]["context"]["slot"] = 124
    assert assess_quote(MINT, request, response).kind == "native_sol"


def test_wrong_size_encoding_and_missing_account_stay_unknown():
    request, response = receipt(curve_bytes(bytes(32)))
    value = response["result"]["value"]
    value["space"] = 116
    assert assess_quote(MINT, request, response).reason == "account_data_size_mismatch"
    value["data"][0] += "!"
    assert assess_quote(MINT, request, response).reason == "account_data_unreadable"
    response["result"]["value"] = None
    assert assess_quote(MINT, request, response).reason == "account_unavailable"
    assert assess_quote(MINT, request, None).reason == "rpc_response_unbound"
