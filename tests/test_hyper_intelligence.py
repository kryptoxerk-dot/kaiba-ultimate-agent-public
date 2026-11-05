"""Fixed read methods with independent latest-state evidence; no live providers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.providers import hyper_intelligence as hi
from kaiba.providers._http import Fetched
from kaiba.providers.hyper_rpc import NetworkProbe

AT = 1_793_620_000_000
ADDRESS = "0x" + "1" * 40
SETTINGS = SimpleNamespace(hyperevm_rpc_url="https://rpc.hyperliquid.xyz/evm", elysium_rpc_url="")


def word(value):
    return "0x" + format(value, "064x")


def symbol(value):
    data = value.encode()
    return "0x" + (32).to_bytes(32).hex() + len(data).to_bytes(32).hex() + data.ljust(((len(data) + 31) // 32) * 32, b"\0").hex()


@pytest.fixture
def world(monkeypatch):
    calls, probes = [], []
    state = {"code": "0x60006000", "decimals": word(18), "totalSupply": word(2**200),
             "symbol": symbol("WHYPE"), "native_balance_base_units": "0x0", "transaction_count": "0x0",
             "basis": EvidenceBasis.PROVIDER_REPORTED, "observed": AT, "id": None, "probe_available": True}
    monkeypatch.setattr(hi, "now_ms", lambda: AT)

    def probe(url, **kw):
        probes.append((url, kw))
        return NetworkProbe(999, observed_chain_id=999 if state["probe_available"] else 998,
                            block_hash="0x" + "a" * 64, block_number=42, block_timestamp_ms=AT - 1000,
                            blockers=() if state["probe_available"] else ("rpc_chain_id_mismatch",),
                            receipts=(Receipt(provider=hi.PROVIDER, endpoint="network.eth_chainId", observed_at_ms=AT),
                                      Receipt(provider=hi.PROVIDER, endpoint="network.eth_getBlockByNumber", observed_at_ms=AT)))

    def fetch(provider, endpoint, url, **kw):
        calls.append((provider, endpoint, url, kw))
        body = kw["json_body"]
        field = body["id"].split(":", 1)[1]
        response = {"jsonrpc": "2.0", "id": state["id"] or body["id"], "result": state.get(field)}
        if state.get("error"):
            response["error"] = {"message": "private upstream error"}
        return Fetched(response, Receipt(provider=provider, endpoint=endpoint, basis=state["basis"],
                                         observed_at_ms=state["observed"], note="private URL must not be exported"))

    monkeypatch.setattr(hi, "probe_network", probe)
    monkeypatch.setattr(hi, "request_json", fetch)
    return state, calls, probes


def test_token_metadata_is_exact_and_does_not_claim_market_safety_or_a_snapshot(world):
    _, calls, probes = world
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["symbol"] == "WHYPE" and out["facts"]["decimals"] == 18
    assert out["facts"]["total_supply_base_units"] == str(2**200)
    assert out["facts"]["code_present"] is True and len(out["facts"]["code_sha256"]) == 64
    assert out["facts"]["liquidity_usd"] is None and out["safety_verified"] is None
    assert out["atomic_snapshot"] is False and out["execution_ready"] is False
    assert out["state_basis"] == "independent_latest_observations" and not out["unknowns"]
    assert len(probes) == 1 and len(calls) == 4 and out["method_attempts_upper_bound"] == 6
    assert all(c[0] == hi.PROVIDER and c[3]["retries"] == 0 and c[3]["stale_grace_s"] == 0 for c in calls)
    assert all(c[3]["json_body"]["params"][-1] == "latest" for c in calls)
    assert {c[3]["json_body"]["params"][0]["data"] for c in calls[1:]} == set(hi.SELECTORS.values())
    assert "private URL" not in str(out) and SETTINGS.hyperevm_rpc_url not in str(out)


def test_zero_supply_and_zero_decimals_are_real_values_not_missing(world):
    state, _, _ = world
    state.update(decimals=word(0), totalSupply=word(0))
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["decimals"] == 0 and out["facts"]["total_supply_base_units"] == "0"


def test_zero_wallet_balance_does_not_prove_ownership_pnl_grade_or_all_holdings(world):
    state, calls, _ = world
    state["code"] = "0x"
    out = hi.wallet_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["native_balance_base_units"] == "0" and out["facts"]["transaction_count"] == 0
    assert out["facts"]["account_code_present"] is False
    assert out["native_symbol"] == "HYPE" and out["native_decimals"] == 18
    assert out["facts"]["realized_pnl"] is None and out["facts"]["wallet_grade"] is None
    assert out["facts"]["owned_by_operator"] is None and out["facts"]["token_holdings_enumerated"] is False
    assert len(calls) == 3 and out["method_attempts_upper_bound"] == 5


@pytest.mark.parametrize("function", [hi.token_facts, hi.wallet_facts])
def test_unverified_elysium_mainnet_is_zero_call(world, function):
    _, calls, probes = world
    out = function("elysium", ADDRESS, settings=SETTINGS)
    assert out["unknowns"] == ["mainnet_identity_unverified"] and not calls and not probes
    assert out["method_attempts_upper_bound"] == 0


def test_wrong_network_proof_refuses_all_address_reads(world):
    state, calls, _ = world
    state["probe_available"] = False
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["unknowns"] == ["network:rpc_chain_id_mismatch"] and not calls


def test_missing_contract_code_stops_before_any_metadata_call(world):
    state, calls, _ = world
    state["code"] = "0x"
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["code_present"] is False and "contract_code_absent" in out["unknowns"]
    assert len(calls) == 1 and calls[0][3]["json_body"]["method"] == "eth_getCode"


@pytest.mark.parametrize("basis", [EvidenceBasis.STALE, EvidenceBasis.UNAVAILABLE, EvidenceBasis.ESTIMATED])
def test_missing_evidence_never_turns_into_a_zero_balance(world, basis):
    state, _, _ = world
    state["basis"] = basis
    out = hi.wallet_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["native_balance_base_units"] is None and out["facts"]["transaction_count"] is None


@pytest.mark.parametrize("change", [{"id": "different"}, {"error": True}, {"observed": AT - 10001}, {"observed": AT + 1}])
def test_rpc_identity_error_and_evidence_clock_are_not_accepted(world, change):
    state, _, _ = world
    state.update(change)
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["code_present"] is None and out["unknowns"]
    assert "private upstream" not in str(out)


def test_optional_metadata_and_invalid_uint8_remain_partial(world):
    state, _, _ = world
    state.update(decimals=word(256), symbol=None)
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["decimals"] is None and out["facts"]["symbol"] is None
    assert out["facts"]["total_supply_base_units"] == str(2**200)


@pytest.mark.parametrize("raw", ["0x", word(0), "0x" + "ff" * 32, symbol("bad\ntext"),
                                 "0x" + (64).to_bytes(32).hex() + "00" * 64])
def test_malformed_or_control_text_does_not_become_a_symbol(raw):
    assert hi._symbol(raw) is None


def test_legacy_bytes32_symbol_is_explicitly_supported():
    assert hi._symbol("0x" + b"LEGACY".ljust(32, b"\0").hex()) == "LEGACY"


@pytest.mark.parametrize("network,address", [("unknown", ADDRESS), ("hyperevm", "not-an-address")])
def test_bad_input_is_zero_call(world, network, address):
    _, calls, probes = world
    assert hi.wallet_facts(network, address, settings=SETTINGS)["unknowns"]
    assert not calls and not probes


def test_metadata_429_also_blocks_a_chain_probe_without_retrying_upstream(tmp_db, monkeypatch):
    import httpx

    from kaiba.core import limiter
    from kaiba.providers.hyper_rpc import probe_network

    monkeypatch.setattr(limiter, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(hi, "probe_network", lambda *a, **kw: NetworkProbe(
        999, observed_chain_id=999, block_hash="0x" + "a" * 64))
    calls = []

    def refused(method, url, **kw):
        calls.append(kw["json"]["method"])
        return httpx.Response(429, headers={"retry-after": "60"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", refused)
    out = hi.wallet_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert out["facts"]["native_balance_base_units"] is None
    assert not probe_network(SETTINGS.hyperevm_rpc_url, expected_chain_id=999).available
    assert calls == ["eth_getBalance"]  # metadata, remaining fields and the probe share the backoff


def test_a_probe_that_consumes_the_budget_cannot_start_address_reads(world, monkeypatch):
    _, calls, _ = world
    clock = {"now": 0}
    monkeypatch.setattr(hi.time, "monotonic", lambda: clock["now"])

    def slow_probe(*args, **kw):
        clock["now"] = 26
        return NetworkProbe(999, observed_chain_id=999, block_hash="0x" + "a" * 64)

    monkeypatch.setattr(hi, "probe_network", slow_probe)
    out = hi.token_facts("hyperevm", ADDRESS, settings=SETTINGS)
    assert "code:observation_budget_exhausted" in out["unknowns"] and not calls
