from __future__ import annotations

import base64
import hashlib

import pytest
from base58 import b58encode

from kaiba.learning.sol_message_fee import NetworkFeeEvidence, assess_network_fee
from tests.signer_fixtures import SYSTEM, raw_message, unsigned_tx

NOW = 1_793_600_000_000
PAYER = b58encode(b"\x11" * 32).decode()
RECIPIENT = b58encode(b"\x22" * 32).decode()
BLOCKHASH = b58encode(b"\x33" * 32).decode()


def example(*, version=None, transfer_lamports=99_000):
    # A synthetic transfer deliberately has a non-fee asset effect. A network
    # quote can describe it and cannot certify that the transfer is permitted.
    ix_data = (2).to_bytes(4, "little") + transfer_lamports.to_bytes(8, "little")
    message = raw_message((1, 0, 1), [PAYER, RECIPIENT, SYSTEM], [(2, [0, 1], ix_data)],
                          version=version, blockhash=BLOCKHASH)
    request = {"jsonrpc": "2.0", "id": 7, "method": "getFeeForMessage",
               "params": [base64.b64encode(message).decode(), {"commitment": "confirmed", "minContextSlot": 100}]}
    response = {"jsonrpc": "2.0", "id": 7, "result": {"context": {"slot": 123}, "value": 25_000}}
    return message, unsigned_tx(message), request, response


def assess(tx, req, res, *, payer=PAYER, received=NOW):
    return assess_network_fee(tx, req, res, fee_payer=payer, received_ms=received, at_ms=NOW)


@pytest.mark.parametrize("version", [None, 0])
def test_network_quote_binds_exact_unsigned_legacy_and_v0_message(version):
    message, tx, request, response = example(version=version)
    assert assess(tx, request, response) == NetworkFeeEvidence(
        "network_fee_quoted", 25_000, hashlib.sha256(message).hexdigest(), 123,
    )


def test_same_payer_with_changed_instruction_and_full_tx_as_request_are_refused():
    _, tx, request, response = example()
    _, edited, _, _ = example(transfer_lamports=99_001)
    assert assess(edited, request, response).reason == "fee_request_message_mismatch"
    request["params"][0] = tx
    assert assess(tx, request, response).reason == "fee_request_message_mismatch"


def test_fee_payer_and_empty_signature_slots_are_required():
    _, tx, request, response = example()
    assert assess(tx, request, response, payer=RECIPIENT).reason == "fee_payer_mismatch"
    raw = bytearray(base64.b64decode(tx))
    raw[1] = 1
    assert assess(base64.b64encode(raw).decode(), request, response).reason == "signature_slots_not_empty"


def test_malformed_unsupported_or_oversized_wire_data_is_refused():
    _, tx, request, response = example(version=1)
    assert assess(tx, request, response).reason == "unsigned_transaction_unreadable"
    assert assess("%%%%", request, response).reason == "unsigned_transaction_unreadable"
    assert assess(base64.b64encode(bytes(1233)).decode(), request, response).reason == "unsigned_transaction_unreadable"


@pytest.mark.parametrize("fee", [None, True, "25000", -1, 2**64])
def test_missing_or_invalid_fee_is_unknown(fee):
    _, tx, request, response = example()
    response["result"]["value"] = fee
    result = assess(tx, request, response)
    assert result.reason == "rpc_network_fee_unavailable"
    assert result.network_fee_lamports is None


def test_explicit_zero_is_distinct_from_missing_network_fee():
    _, tx, request, response = example()
    response["result"]["value"] = 0
    assert assess(tx, request, response).network_fee_lamports == 0


@pytest.mark.parametrize("received", [NOW + 1, NOW - 60_001, None, True])
def test_clock_must_be_fresh_and_typed(received):
    _, tx, request, response = example()
    assert assess(tx, request, response, received=received).reason == "fee_receipt_clock_unavailable"


def test_typed_rpc_id_and_minimum_context_slot_are_bound():
    _, tx, request, response = example()
    response["id"] = "7"
    assert assess(tx, request, response).reason == "rpc_receipt_unbound"
    response["id"] = 7
    response["result"]["context"]["slot"] = 99
    assert assess(tx, request, response).reason == "rpc_context_unavailable"
    response["result"]["context"]["slot"] = 100
    assert assess(tx, request, response).network_fee_lamports == 25_000
