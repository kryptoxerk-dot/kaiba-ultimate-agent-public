from __future__ import annotations

import pytest

from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.learning import elysium_launch_check as launch
from kaiba.providers import hyper_rpc
from kaiba.providers._http import Fetched

AT = 1_793_824_000_000
ANCHOR = "0x" + "ab" * 32
TOKEN = "0x" + "11" * 20


@pytest.fixture
def relays(monkeypatch):
    calls = []
    state = {"chain": "0x1863d", "overlap": ANCHOR, "code": ["0x6000", "0x6000"],
             "code_basis": EvidenceBasis.PROVIDER_REPORTED, "code_age": 0, "same_height": False}

    def fetch(provider, endpoint, url, **kwargs):
        body = kwargs["json_body"]
        method, params = body["method"], body["params"]
        calls.append((url, method, params))
        assert kwargs["retries"] == kwargs["stale_grace_s"] == 0
        assert url in launch.RELAYS.values()
        primary = url == launch.RELAYS["eleos"]
        basis, observed = EvidenceBasis.PROVIDER_REPORTED, AT
        if method == "eth_chainId":
            result = hex(launch.CHAIN_ID) if primary else state["chain"]
        elif method == "eth_getBlockByNumber":
            lower_head = primary or state["same_height"]
            number = "0x1" if lower_head or params[0] == "0x1" else "0x2"
            block_hash = ANCHOR if primary else state["overlap"] if number == "0x1" else "0x" + "cd" * 32
            result = {"number": number, "hash": block_hash, "timestamp": hex(AT // 1000 - 1),
                      "gasLimit": "0x1234"}
        else:
            assert method == "eth_getCode"
            result = state["code"][0 if primary else 1]
            basis, observed = state["code_basis"], AT - state["code_age"]
        return Fetched({"jsonrpc": "2.0", "id": body["id"], "result": result},
                       Receipt(provider=provider, endpoint=endpoint, basis=basis, observed_at_ms=observed))

    for module in (hyper_rpc, launch):
        monkeypatch.setattr(module, "request_json", fetch)
        monkeypatch.setattr(module, "now_ms", lambda: AT)
    return calls, state


def test_checks_shared_block_then_exact_token_code(relays):
    calls, _ = relays
    result = launch.check_launch(TOKEN)
    assert result["status"] == "relays_agree_at_recent_block"
    assert result["block_hash"] == ANCHOR and result["block_number"] == 1
    assert result["token_code_status"] == "present" and result["token_code_bytes"] == 2
    assert result["execution_ready"] is False and result["public_launch_verified"] is False
    assert len(calls) == 7
    assert calls[4] == (launch.RELAYS["perpme"], "eth_getBlockByNumber", ["0x1", False])
    assert all(call[1:] == ("eth_getCode", [TOKEN, {"blockHash": ANCHOR, "requireCanonical": True}])
               for call in calls[-2:])


def test_same_height_needs_no_extra_header_and_token_is_optional(relays):
    calls, state = relays
    state["same_height"] = True
    result = launch.check_launch()
    assert result["status"] == "relays_agree_at_recent_block"
    assert result["token_code_status"] == "not_requested" and len(calls) == 4


@pytest.mark.parametrize("same_height", [False, True])
def test_different_hashes_refuse_token_probe(relays, same_height):
    calls, state = relays
    state.update(overlap="0x" + "ee" * 32, same_height=same_height)
    result = launch.check_launch(TOKEN)
    assert result["status"] == "overlap_header_disagreement"
    assert "block_hash" not in result and all(call[1] != "eth_getCode" for call in calls)


@pytest.mark.parametrize("wrong_chain", [99801, 1339, 999])
def test_other_chains_cannot_pass(relays, wrong_chain):
    calls, state = relays
    state["chain"] = hex(wrong_chain)
    assert launch.check_launch(TOKEN)["status"] == "relay_probe_unavailable"
    assert len(calls) == 3


@pytest.mark.parametrize(("overrides", "status"), [
    ({"code": ["0x", "0x"]}, "empty_at_block"),
    ({"code": ["0x6000", "0x"]}, "relay_disagreement"),
    ({"code": ["0x6000", None]}, "unknown"),
    ({"code": ["0x0", "0x0"]}, "unknown"),
    ({"code_basis": EvidenceBasis.STALE}, "unknown"),
    ({"code_basis": EvidenceBasis.CACHED, "code_age": 5_001}, "unknown"),
])
def test_code_distinguishes_disagreement_and_missing_evidence(relays, overrides, status):
    _, state = relays
    state.update(overrides)
    result = launch.check_launch(TOKEN)
    assert result["token_code_status"] == status
    assert ("token_code_sha256" in result) == (status == "empty_at_block")


@pytest.mark.parametrize("invalid", ["ELEOS", "0x" + "0" * 40, "0x1234"])
def test_invalid_token_does_not_reach_network(relays, invalid):
    calls, _ = relays
    with pytest.raises(ValueError, match="20-byte EVM address"):
        launch.check_launch(invalid)
    assert not calls
