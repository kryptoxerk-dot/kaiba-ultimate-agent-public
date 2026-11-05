"""Synthetic protocol-boundary tests. Not proof of a real reward or deployed ABI."""

import copy
import hashlib
import json
from decimal import Decimal

import pytest

from kaiba.hunters import merkl_entitlements as m
from kaiba.hunters.ev import HunterConfig, score_opportunity
from kaiba.hunters.qualification import qualification

WALLET = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
HASH = "0x" + "ab" * 32
AT = 1793268000000


def fixture_bundle(*, claimed=300, pending=5000, api_claimed=200, proof=None, root=None):
    proof = ["0x" + "cd" * 32, "0x" + "01" * 32] if proof is None else proof
    root = "0x" + m.proof_root(WALLET, TOKEN, 1000, proof).hex() if root is None else root
    reward = {"token": {"address": TOKEN, "chainId": 8453}, "recipient": WALLET,
              "distributionLayer": "TREE", "root": root, "amount": "1000",
              "claimed": str(api_claimed), "pending": str(pending), "proofs": proof}
    api = [{"chain": {"id": 8453}, "rewards": [reward]}]
    def word(n):
        return n.to_bytes(32, "big").hex()
    values = [api, hex(8453), {"hash": HASH, "number": hex(1_000_000), "timestamp": hex(AT // 1000 - 1200)},
              root, "0x" + word(claimed) + word(AT // 1000 - 1800) + "12" * 32]
    receipts = []

    def exchange(request):
        i = len(receipts)
        value = values[i]
        value = value if i == 0 else {"jsonrpc": "2.0", "id": i, "result": value}
        raw = json.dumps(value).encode()
        receipts.append({"request": copy.deepcopy(request), "http_status": 200,
                         "started_ms": AT + i * 100, "received_ms": AT + i * 100 + 50,
                         "body_hex": raw.hex(), "body_sha256": hashlib.sha256(raw).hexdigest()})
        return value, AT + i * 100 + 50

    m.analyze(WALLET, exchange)
    return {"schema": m.SCHEMA, "wallet": WALLET, "receipts": receipts}


def change_response(bundle, index, change):
    receipt = bundle["receipts"][index]
    value = json.loads(bytes.fromhex(receipt["body_hex"]))
    change(value)
    raw = json.dumps(value).encode()
    receipt.update(body_hex=raw.hex(), body_sha256=hashlib.sha256(raw).hexdigest())


def run(bundle):
    return m.replay(bundle, at_ms=AT + 1000)


def test_verified_allocation_uses_chain_claimed_and_excludes_pending():
    result = run(fixture_bundle())
    assert result["remaining_allocation_count"] == result["allocation_verified_count"] == 1
    row = result["rewards"][0]
    assert row["remaining_atoms"] == "700"  # Not API's 800; never add pending 5000.
    assert row["api_claimed_matches_chain"] is False
    assert row["pending_atoms_excluded"] == "5000"
    assert row["eligibility_verified"] is row["claim_execution_verified"] is False
    assert result["net_profit_usd"] is None
    assert result["qualified"] is result["funded_action_authorized"] is False


def test_fully_claimed_is_zero_allocation_not_a_fresh_airdrop():
    bundle = fixture_bundle(claimed=1000)
    assert run(bundle)["remaining_allocation_count"] == 0
    assert m.opportunities(bundle, at_ms=AT + 1000) == []


def test_cumulative_underflow_never_clamped_to_positive():
    row = run(fixture_bundle(claimed=1001))["rewards"][0]
    assert row["remaining_atoms"] is None
    assert row["reason"] == "cumulative_amount_below_claimed"


def test_verified_lead_cannot_bypass_existing_qualification():
    lead, = m.opportunities(fixture_bundle(), at_ms=AT + 1000)
    score = score_opportunity(lead, HunterConfig(), at_ms=AT + 1000)
    assert score.ev_usd is None
    # Existing scorer reports base participation cost even when scenario costs are absent.
    assert score.cost_usd == 0
    assert lead.meta["unpriced_costs"] == ["claim", "sale", "execution_loss"]
    assert not score.valuation_available
    verdict = qualification({"opportunity_id": lead.key, "kind": "airdrop", "chain": "base",
                             "status": "open", "ev_score": None, "cost_usd": None,
                             "evidence_json": lead.model_dump(mode="json")},
                            at_ms=AT + 1000, ev_floor=Decimal(25))
    assert not verdict["qualified"]
    assert {"eligibility_verified_missing", "value_cost_verified_missing", "ev_unavailable"} <= set(verdict["reasons"])


@pytest.mark.parametrize("field,value", [("amount", 1000), ("amount", True), ("amount", "1e3"),
                                        ("amount", "-1"), ("amount", str(1 << 208)),
                                        ("claimed", None), ("pending", "NaN"),
                                        ("proofs", ["0xab"]), ("distributionLayer", "LEDGER")])
def test_malformed_units_and_unsupported_reward_modes_refuse(field, value):
    bundle = fixture_bundle()
    change_response(bundle, 0, lambda v: v[0]["rewards"][0].update({field: value}))
    with pytest.raises(m.EvidenceError):
        run(bundle)


@pytest.mark.parametrize("field", ["wallet", "token", "root", "recipient", "amount"])
def test_proof_binds_every_identity_and_current_root(field):
    bundle = fixture_bundle()
    if field == "wallet":
        bundle["wallet"] = "0x" + "33" * 20
        with pytest.raises(m.EvidenceError, match="request_mismatch"):
            run(bundle)
        return
    def alter(v):
        row = v[0]["rewards"][0]
        if field == "token":
            row["token"]["address"] = "0x" + "33" * 20
        else:
            row[field] = {"root": "0x" + "00" * 32, "recipient": "0x" + "33" * 20,
                          "amount": "1001"}[field]
    change_response(bundle, 0, alter)
    # Invalid proof means claimed() must not be called. Drop now-unused synthetic receipt.
    bundle["receipts"].pop()
    result = run(bundle)
    assert result["allocation_verified_count"] == 0
    assert result["rewards"][0]["remaining_atoms"] is None


def test_pending_root_does_not_override_contract_accepted_root():
    bundle = fixture_bundle()
    change_response(bundle, 3, lambda v: v.update(result="0x" + "45" * 32))
    bundle["receipts"].pop()
    assert run(bundle)["allocation_verified_count"] == 0
    assert bundle["receipts"][3]["request"]["json"]["params"][0]["data"] == "0x49590657"


@pytest.mark.parametrize("index,mutation,reason", [
    (1, lambda v: v.update(result="0x1"), "rpc_chain_mismatch"),
    (2, lambda v: v["result"].update(timestamp=hex(AT // 1000 - 3601)), "finalized_block_stale"),
    (3, lambda v: v.update(result="0x" + "00" * 32), "uninitialized_root"),
    (4, lambda v: v.update(result="0x01"), "invalid_abi_data"),
    (4, lambda v: v.update(error={"message": "sensitive"}), "rpc_error"),
    (4, lambda v: v.update(id=3), "rpc_error"),
])
def test_bad_chain_state_is_unavailable(index, mutation, reason):
    bundle = fixture_bundle()
    change_response(bundle, index, mutation)
    with pytest.raises(m.EvidenceError, match=reason):
        run(bundle)


def test_all_contract_calls_bind_same_finalized_hash():
    bundle = fixture_bundle()
    assert bundle["receipts"][2]["request"]["json"]["params"] == ["finalized", False]
    for receipt in bundle["receipts"][3:]:
        assert receipt["request"]["json"]["params"][1] == {"blockHash": HASH, "requireCanonical": True}
    bundle["receipts"][4]["request"]["json"]["params"][1] = "latest"
    with pytest.raises(m.EvidenceError, match="request_mismatch"):
        run(bundle)


def test_rpc_request_identity_does_not_coerce_boolean_to_integer():
    bundle = fixture_bundle()
    bundle["receipts"][1]["request"]["json"]["id"] = True
    with pytest.raises(m.EvidenceError, match="request_mismatch"):
        run(bundle)


def test_original_byte_hash_clock_and_full_receipt_sequence_are_required():
    bundle = fixture_bundle()
    broken = copy.deepcopy(bundle)
    broken["receipts"][0]["body_sha256"] = "f" * 64
    with pytest.raises(m.EvidenceError, match="body_hash"):
        run(broken)
    with pytest.raises(m.EvidenceError, match="staleness"):
        m.replay(bundle, at_ms=AT + 601_000)
    broken = copy.deepcopy(bundle)
    broken["receipts"].pop()
    with pytest.raises(m.EvidenceError, match="missing_receipt"):
        run(broken)
    broken = copy.deepcopy(bundle)
    broken["receipts"].append(broken["receipts"][-1])
    with pytest.raises(m.EvidenceError, match="unconsumed"):
        run(broken)


def test_api_chain_duplicate_token_and_budget_are_not_silently_filtered():
    payload = json.loads(bytes.fromhex(fixture_bundle()["receipts"][0]["body_hex"]))
    for path in ("chain", "token"):
        wrong = copy.deepcopy(payload)
        if path == "chain":
            wrong[0]["chain"]["id"] = 1
        else:
            wrong[0]["rewards"][0]["token"]["chainId"] = 1
        with pytest.raises(m.EvidenceError, match="chain_mismatch"):
            m.reward_rows(wrong)
    payload[0]["rewards"] *= 2
    with pytest.raises(m.EvidenceError, match="duplicate_reward"):
        m.reward_rows(payload)
    payload[0]["rewards"] *= 3
    with pytest.raises(m.EvidenceError, match="over_budget"):
        m.reward_rows(payload)


def test_json_duplicate_keys_and_nonfinite_numbers_refuse():
    for raw in (b'{"amount":"1","amount":"2"}', b'{"p":NaN}'):
        with pytest.raises(m.EvidenceError):
            m.parse_json(raw)


def test_single_leaf_and_sorted_sibling_abi_vector():
    assert m.proof_root(WALLET, TOKEN, 1000, []).hex() == "f3d09005c5ca44ea4e701c2ef5afc7c5059c3f1a8a11e7b8c9d9561776733bf0"
    assert run(fixture_bundle(proof=[]))["remaining_allocation_count"] == 1
