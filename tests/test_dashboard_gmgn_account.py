"""Synthetic account-contract controls; no settings, credentials or provider calls."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dashboard import gmgn_account as account

NOW = 1_800_000_000_000
SOL = "11111111111111111111111111111111"
EVM = "0x" + "12" * 20


def result(data=None, *, basis="provider_reported", observed=NOW, note=None, **receipt):
    return SimpleNamespace(
        data={"wallets": []} if data is None else data,
        receipt=SimpleNamespace(
            provider="gmgn", endpoint="portfolio.info", basis=basis,
            observed_at_ms=observed, note=note, **receipt,
        ),
    )


def read(value):
    return account.read_account(lambda: value, clock_ms=lambda: NOW)


def wallet(chain="sol", address=SOL, balances=None, **extra):
    return {"chain": chain, "address": address, "balances": [] if balances is None else balances, **extra}


def test_import_does_not_import_provider_or_settings():
    code = (
        "import sys; import dashboard.gmgn_account; "
        "assert 'kaiba.providers.gmgn_cli' not in sys.modules; "
        "assert 'kaiba.core.config' not in sys.modules"
    )
    assert subprocess.run([sys.executable, "-B", "-c", code], capture_output=True).returncode == 0


def test_success_reports_only_validated_account_visibility_and_original_clock():
    view = read(result({"wallets": [wallet(), wallet("robinhood", EVM)], "api_key": "never-return"}, observed=NOW - 2_000))
    assert view["status"] == "connected"
    assert view["fresh"] is True
    assert view["observed_at_ms"] == NOW - 2_000
    assert view["age_ms"] == 2_000
    assert view["configured"] is None
    assert view["scope"] == "account_visibility_only"
    assert view["wallet_count"] == 2
    assert view["wallets"][1]["native_symbol"] == "ETH"
    assert view["wallets"][1]["native_balance"] is None
    assert "never-return" not in json.dumps(view)


def test_normal_new_receipt_uses_check_completion_not_start_clock():
    ticks = iter([NOW, NOW + 100])
    calls = []

    def fetch():
        calls.append(1)
        return result({"wallets": [wallet()]}, observed=NOW + 90)

    view = account.read_account(fetch, clock_ms=lambda: next(ticks))
    assert calls == [1]
    assert view["status"] == "connected"
    assert view["checked_at_ms"] == NOW + 100
    assert view["observed_at_ms"] == NOW + 90
    assert view["age_ms"] == 10


def test_failed_check_records_completion_clock_without_retry():
    ticks = iter([NOW, NOW + 100])
    calls = []

    def fetch():
        calls.append(1)
        raise TimeoutError("do-not-echo")

    view = account.read_account(fetch, clock_ms=lambda: next(ticks))
    assert calls == [1]
    assert view["status"] == "unavailable"
    assert view["checked_at_ms"] == NOW + 100
    assert view["observed_at_ms"] is view["age_ms"] is None
    assert view["reason"] == "provider_timeout"


def test_backwards_check_clock_cannot_connect():
    ticks = iter([NOW, NOW - 1])
    view = account.read_account(lambda: result(), clock_ms=lambda: next(ticks))
    assert view["status"] == "invalid_response"
    assert view["reason"] == "check_clock_invalid"
    assert view["fresh"] is None


@pytest.mark.parametrize("basis,age", [("stale", 0), ("cached", 60_001), ("provider_reported", 60_001)])
def test_old_or_provider_stale_receipt_cannot_become_fresh(basis, age):
    view = read(result(basis=basis, observed=NOW - age))
    assert view["status"] == "stale"
    assert view["fresh"] is False
    assert view["observed_at_ms"] == NOW - age


@pytest.mark.parametrize("observed", [None, True, 0, "1800000000000", NOW + 1])
def test_missing_future_or_invalid_observation_cannot_connect(observed):
    view = read(result(observed=observed))
    assert view["status"] == "invalid_response"
    assert view["fresh"] is None


@pytest.mark.parametrize("data", [{}, {"wallets": {}}, {"wallets": "secret"}, [], "secret"])
def test_malformed_payload_is_not_connected(data):
    assert read(result(data))["status"] == "invalid_response"


@pytest.mark.parametrize("basis", ["verified_onchain", "estimated", "derived", "secret"])
def test_wrong_receipt_basis_is_not_connected(basis):
    assert read(result(basis=basis))["status"] == "invalid_response"


@pytest.mark.parametrize("field,value", [("provider", "other"), ("endpoint", "portfolio.holdings")])
def test_wrong_receipt_identity_cannot_connect(field, value):
    value_result = result()
    setattr(value_result.receipt, field, value)
    assert read(value_result)["status"] == "invalid_response"


def test_invalid_addresses_unsupported_chains_and_duplicates_are_omitted():
    rows = [wallet(), wallet(), wallet("sol", SOL.lower()), wallet("eth", EVM.upper()),
            wallet("unsupported", EVM), wallet("sol", EVM), wallet("eth", "<script>"), None]
    view = read(result({"wallets": rows}))
    assert view["wallets"] == [{"chain": "sol", "address": SOL, "native_symbol": "SOL",
                                "native_balance": None, "balance_status": "unavailable"}]
    assert view["omitted_count"] == 7
    assert view["reason"] == "partial_wallet_data"


def test_only_invalid_identities_is_not_connected():
    assert read(result({"wallets": [wallet("sol", "bad")]}))["status"] == "invalid_response"


def test_solana_case_is_preserved_evm_is_normalized():
    sol = "So11111111111111111111111111111111111111112"
    view = read(result({"wallets": [wallet(address=sol), wallet("eth", "0x" + "Ab" * 20)]}))
    assert view["wallets"][0]["address"] == sol
    assert view["wallets"][1]["address"] == "0x" + "ab" * 20


@pytest.mark.parametrize("address", ["1" * 33, "z" * 32, "z" * 44])
def test_base58_character_length_alone_does_not_prove_32_byte_address(address):
    view = read(result({"wallets": [wallet(address=address)]}))
    assert view["status"] == "invalid_response"
    assert view["reason"] == "wallet_identities_invalid"
    assert view["wallets"] == []
    # The same receipt with an actual 32-byte base58 public key is accepted.
    assert read(result({"wallets": [wallet()]}))["status"] == "connected"


@pytest.mark.parametrize("chain", ["arc", "stable"])
def test_unverified_native_currency_chains_are_not_given_a_guessed_symbol(chain):
    view = read(result({"wallets": [wallet(chain, EVM)]}))
    assert view["wallets"] == []
    assert view["status"] == "invalid_response"


@pytest.mark.parametrize("balances,status", [([], "unavailable"), ({"balance": "4"}, "invalid"),
                                           ("private", "invalid"), ([{}] * 33, "invalid"),
                                           ([{"balance": "123", "symbol": "SOL"}], "unsupported")])
def test_balance_shape_is_not_guessed_or_missing_zero(balances, status):
    value_result = result({"wallets": [wallet(balances=balances)]})
    view = read(value_result)
    assert view["wallets"][0]["native_balance"] is None
    assert view["wallets"][0]["balance_status"] == status


def test_missing_balances_are_invalid_not_zero():
    view = read(result({"wallets": [{"chain": "robinhood", "address": EVM}]}))
    assert view["wallets"][0]["balance_status"] == "invalid"
    assert view["wallets"][0]["native_balance"] is None


@pytest.mark.parametrize("note", ["HTTP 429 bearer private-secret", "limiter refused: key=private-secret", "RATE_LIMIT_BANNED"])
def test_unavailable_rate_limit_is_typed_without_echo(note):
    value_result = result(basis="unavailable", note=note)
    value_result.data = None
    view = read(value_result)
    assert view["status"] == "rate_limited"
    assert view["fresh"] is None
    assert view["wallets"] == []
    assert "private-secret" not in json.dumps(view)


def test_failure_cannot_echo_note_or_preserve_fake_wallet_data():
    view = read(result({"wallets": [wallet()]}, basis="unavailable", note="secret-token"))
    assert view["status"] == "unavailable"
    assert view["wallets"] == []
    assert "secret-token" not in json.dumps(view)


@pytest.mark.parametrize("error,reason", [(TimeoutError("private-secret"), "provider_timeout"),
                                        (RuntimeError("private-secret"), "provider_exception")])
def test_exceptions_are_safe_no_retry(error, reason):
    calls = []

    def fetch():
        calls.append(1)
        raise error

    view = account.read_account(fetch, clock_ms=lambda: NOW)
    assert calls == [1]
    assert view["reason"] == reason
    assert "private-secret" not in json.dumps(view)


def test_one_default_shared_wrapper_call_with_explicit_timeout_no_wait(monkeypatch):
    # Replace the import target before invoking the lazy default. No settings are read.
    calls = []
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", SimpleNamespace(
        account_info=lambda **kw: (calls.append(kw), result())[1],
    ))
    assert account.read_account(clock_ms=lambda: NOW)["status"] == "connected"
    assert calls == [{"timeout_s": 8.0, "wait_for_slot_s": 0.0}]


def test_caps_visible_rows_and_input_size():
    rows = [wallet("eth", "0x" + f"{i:040x}") for i in range(40)]
    view = read(result({"wallets": rows}))
    assert view["wallet_count"] == 32
    assert view["omitted_count"] == 8
    assert view["truncated"] is True
    assert read(result({"wallets": rows * 7}))["reason"] == "payload_limit_exceeded"
    assert read(result({"wallets": [], "extra": "a" * account.MAX_PAYLOAD_BYTES}))["reason"] == "payload_limit_exceeded"


def test_cyclic_nonfinite_and_nonjson_payloads_are_rejected():
    for value in (float("nan"), object()):
        assert read(result({"wallets": [], "extra": value}))["reason"] == "payload_not_json"
    data = {"wallets": []}
    data["cycle"] = data
    assert read(result(data))["reason"] == "payload_not_json"


def test_allowlisted_output_keys_only():
    view = read(result({"wallets": [wallet(secret="never-return")], "arbitrary": "never-return"}))
    assert set(view) == {
        "provider", "scope", "configured", "status", "checked_at_ms", "observed_at_ms", "age_ms",
        "freshness_budget_ms", "fresh", "basis", "wallets", "wallet_count", "omitted_count", "truncated", "reason",
    }
    assert set(view["wallets"][0]) == {"chain", "address", "native_symbol", "native_balance", "balance_status"}
    assert "never-return" not in json.dumps(view)
