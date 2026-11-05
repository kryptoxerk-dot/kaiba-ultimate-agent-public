"""Network identity/liveness evidence; fake transport, no live provider calls."""
from __future__ import annotations

from dataclasses import replace

import pytest

from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.providers import hyper_rpc as h
from kaiba.providers._http import Fetched

AT = 1_793_616_000_000
URL = "https://rpc.hyperliquid.xyz/evm"


@pytest.fixture
def transport(monkeypatch):
    calls = []
    state = {"chain": "0x3e7", "header": {"number": "0x12", "hash": "0x" + "a" * 64,
             "timestamp": hex(AT // 1000 - 2), "gasLimit": "0x1c9c380", "baseFeePerGas": "0x7"},
             "basis": EvidenceBasis.PROVIDER_REPORTED, "observed_ms": AT, "response_id": None}

    def fetch(provider, endpoint, url, **kw):
        calls.append((provider, endpoint, url, kw))
        body = kw["json_body"]
        result = state["chain"] if body["method"] == "eth_chainId" else state["header"]
        data = {"jsonrpc": "2.0", "id": state["response_id"] or body["id"], "result": result}
        if "error" in state:
            data["error"] = state["error"]
        receipt = Receipt(provider=provider, endpoint=endpoint, basis=state["basis"],
                          observed_at_ms=state["observed_ms"], response_digest="fixture",
                          note="private RPC URL or provider error is never exported")
        return Fetched(data, receipt)

    monkeypatch.setattr(h, "request_json", fetch)
    return calls, state


def test_live_hyperevm_identity_and_block_have_receipts_but_no_execution(transport):
    calls, _ = transport
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert result.available and result.observed_chain_id == 999 and result.block_number == 18
    assert result.gas_limit == 30_000_000 and result.base_fee_per_gas_wei == 7
    assert len(result.receipts) == 2
    assert [c[3]["json_body"]["method"] for c in calls] == ["eth_chainId", "eth_getBlockByNumber"]
    assert calls[1][3]["json_body"]["params"] == ["latest", False]
    assert all(c[0] == h.PROVIDER and c[3]["retries"] == 0 and c[3]["stale_grace_s"] == 0
               and c[3]["timeout_s"] == 4.0 for c in calls)
    assert all(c[3]["cache_key"].startswith("network-probe:") for c in calls)
    view = result.view()
    assert view["execution_ready"] is False and URL not in str(view) and "private RPC" not in str(view)


@pytest.mark.parametrize("wrong", [998, 99801, 1339, 1])
def test_wrong_network_stops_before_any_header_or_other_read(transport, wrong):
    calls, state = transport
    state["chain"] = hex(wrong)
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert not result.available and result.observed_chain_id == wrong
    assert result.blockers == ("rpc_chain_id_mismatch",) and len(calls) == 1


def test_unknown_elysium_mainnet_does_not_probe_a_testnet_or_guess(transport):
    calls, _ = transport
    result = h.probe_network("https://testnet-rpc.elysium.kinetiq.xyz", expected_chain_id=None, at_ms=AT)
    assert result.blockers == ("mainnet_identity_unverified",) and not result.available and not calls


@pytest.mark.parametrize("chain", [None, True, 999, "999", "0x03e7", "0x", "0xgarbage", "0x" + "f" * 80])
def test_malformed_quantity_never_looks_like_a_matching_chain(transport, chain):
    calls, state = transport
    state["chain"] = chain
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert result.blockers == ("rpc_chain_id_malformed",) and len(calls) == 1


@pytest.mark.parametrize("basis", [EvidenceBasis.STALE, EvidenceBasis.UNAVAILABLE, EvidenceBasis.ESTIMATED])
def test_data_presence_does_not_override_missing_or_stale_evidence(transport, basis):
    calls, state = transport
    state["basis"] = basis
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert not result.available and result.blockers == ("rpc_evidence_unavailable",) and len(calls) == 1


def test_a_cached_header_receipt_keeps_its_original_observation_time(transport):
    _, state = transport
    state.update(basis=EvidenceBasis.CACHED, observed_ms=AT - 2000)
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert result.available and result.block_observed_ms == AT - 2000
    state["observed_ms"] = AT - 6000  # chain ID may be cached60s; block header may only be cached5s
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert not result.available and result.blockers == ("rpc_evidence_out_of_window",)


def test_json_rpc_identity_and_error_are_checked_before_using_a_result(transport):
    calls, state = transport
    state["response_id"] = "unrelated"
    assert h.probe_network(URL, expected_chain_id=999, at_ms=AT).blockers == ("rpc_response_identity_mismatch",)
    state["response_id"] = None
    state["error"] = {"code": -1, "message": "private provider details"}
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert result.blockers == ("rpc_method_unavailable",) and "private provider" not in str(result.view())
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [("hash", None), ("hash", "0x" + "0" * 64),
    ("number", "12"), ("timestamp", "0x0"), ("gasLimit", "0x0")])
def test_invalid_block_cannot_establish_liveness(transport, field, value):
    _, state = transport
    state["header"][field] = value
    assert h.probe_network(URL, expected_chain_id=999, at_ms=AT).blockers == ("block_header_malformed",)


@pytest.mark.parametrize("age,blocker", [(121, "block_header_stale"), (-6, "block_header_in_future")])
def test_stale_or_future_block_is_evidence_of_unavailability(transport, age, blocker):
    _, state = transport
    state["header"]["timestamp"] = hex(AT // 1000 - age)
    assert h.probe_network(URL, expected_chain_id=999, at_ms=AT).blockers == (blocker,)


def test_missing_fee_stays_unknown_and_fee_does_not_grant_execution(transport):
    _, state = transport
    state["header"].pop("baseFeePerGas")
    result = h.probe_network(URL, expected_chain_id=999, at_ms=AT)
    assert result.available and result.base_fee_per_gas_wei is None
    assert result.view()["execution_ready"] is False
    assert replace(result, base_fee_per_gas_wei=1).view()["execution_ready"] is False


@pytest.mark.parametrize("url", ["http://rpc.example", "https://name:secret@rpc.example", "https://rpc.example/#fragment"])
def test_invalid_transport_target_does_not_make_a_request(transport, url):
    calls, _ = transport
    assert h.probe_network(url, expected_chain_id=999, at_ms=AT).blockers == ("rpc_url_invalid",)
    assert not calls


def test_a_probe_429_backs_off_without_penalizing_protection_rpc(tmp_db, monkeypatch):
    import httpx

    from kaiba.core import limiter

    monkeypatch.setattr(limiter, "get_conn", lambda: tmp_db)
    calls = []

    def rate_limited(method, url, **kw):
        calls.append((method, url))
        return httpx.Response(429, headers={"retry-after": "60"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", rate_limited)
    result = h.probe_network(URL, expected_chain_id=999)
    assert not result.available and len(calls) == 1
    state = tmp_db.execute("SELECT penalty_level FROM provider_state WHERE provider=?", (h.PROVIDER,)).fetchone()
    assert state is not None and state[0] == 1
    assert tmp_db.execute("SELECT * FROM provider_state WHERE provider='rpc'").fetchone() is None
    # The same probe observes the new cooldown and does NOT retry into the upstream429.
    assert not h.probe_network(URL, expected_chain_id=999).available
    assert len(calls) == 1
    with limiter.guarded("rpc", "protection.header", limiter.Priority.EXIT, conn=tmp_db):
        pass
    assert tmp_db.execute("SELECT penalty_level FROM provider_state WHERE provider='rpc'").fetchone()[0] == 0


def test_alchemy_policy_still_refuses_with_a_different_limiter_namespace(tmp_db, monkeypatch):
    import httpx

    from kaiba.core import limiter

    monkeypatch.setattr(limiter, "get_conn", lambda: tmp_db)
    calls = []
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: calls.append(a))
    result = h.probe_network("https://eth-mainnet.g.alchemy.com/v2/fixture", expected_chain_id=1)
    assert not result.available and not calls
    assert result.receipts[0].basis is EvidenceBasis.UNAVAILABLE
