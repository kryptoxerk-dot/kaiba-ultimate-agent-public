"""Read-only Merkl allocation evidence, never a claim or profit qualification.

Base adapter initially. ABI/proof rules: AngleProtocol/merkl-contracts Distributor.sol
at 01c99d0b5e5b8aa49ead52d819c21b8cd8f9c85a. The official archived Base proxy
deployment supplies the address; its upgradeable implementation is NOT certified here.
Use getMerkleRoot(), not tree(): pending/disputed trees need not be claimable.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from kaiba.execution.policy import keccak256, selector_of

CHAIN_ID = 8453
RPC = "https://mainnet.base.org"
DISTRIBUTOR = "0x3ef3d8ba38ebe18db133cec108f4d14ce00dd9ae"
MAX_BODY = 512 * 1024
MAX_REWARDS = 4
MAX_REQUESTS = 4 + MAX_REWARDS
SCHEMA = "kaiba-merkl-entitlement/1"
SOURCE_COMMIT = "01c99d0b5e5b8aa49ead52d819c21b8cd8f9c85a"


class EvidenceError(ValueError):
    """Closed, identity-free refusal reason; never include a provider payload."""


def address(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise EvidenceError("invalid_address")
    if int(value, 16) == 0:
        raise EvidenceError("zero_address")
    return value.lower()


def data(value: Any, size: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{" + str(size * 2) + r"}", value):
        raise EvidenceError("invalid_abi_data")
    return bytes.fromhex(value[2:])


def atoms(value: Any, bits: int = 208) -> int:
    # No JSON floats, bools, exponents, signs or silently rounded large integers.
    if not isinstance(value, str) or len(value) > 78 or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise EvidenceError("invalid_atoms")
    result = int(value)
    if result >= 1 << bits:
        raise EvidenceError("atoms_out_of_range")
    return result


def quantity(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})", value):
        raise EvidenceError("invalid_rpc_quantity")
    return int(value, 16)


def parse_json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or len(raw) > MAX_BODY:
        raise EvidenceError("response_size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_):
        raise EvidenceError("nonfinite_json")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError("invalid_json") from exc


def reward_rows(payload: Any) -> list[dict]:
    if not isinstance(payload, list) or len(payload) > 1:
        raise EvidenceError("unexpected_chain_scope")
    if not payload:
        return []
    group = payload[0]
    if (not isinstance(group, dict) or not isinstance(group.get("chain"), dict)
            or type(group["chain"].get("id")) is not int or group["chain"]["id"] != CHAIN_ID):
        raise EvidenceError("reward_chain_mismatch")
    rows = group.get("rewards")
    if not isinstance(rows, list) or len(rows) > MAX_REWARDS:
        raise EvidenceError("reward_count_unavailable_or_over_budget")
    seen, validated = set(), []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("token"), dict):
            raise EvidenceError("reward_shape")
        token = address(row["token"].get("address"))
        if row.get("distributionLayer", "TREE") != "TREE":
            raise EvidenceError("unsupported_distribution_layer")
        if "chainId" in row["token"] and (type(row["token"]["chainId"]) is not int
                                          or row["token"]["chainId"] != CHAIN_ID):
            raise EvidenceError("token_chain_mismatch")
        if token in seen:
            raise EvidenceError("duplicate_reward_token")
        seen.add(token)
        amount, claimed = atoms(row.get("amount")), atoms(row.get("claimed"))
        pending = atoms(row.get("pending"), 256)
        proof = row.get("proofs")
        if not isinstance(proof, list) or len(proof) > 64:
            raise EvidenceError("proof_unavailable_or_oversized")
        proof = ["0x" + data(node, 32).hex() for node in proof]
        # API decimals/symbol/prices/claimed are not authoritative valuation inputs.
        expected_root = None if "root" not in row else data(row["root"], 32)
        recipient = None if "recipient" not in row else address(row["recipient"])
        validated.append({"token": token, "amount": amount, "api_claimed": claimed,
                          "pending": pending, "proof": proof, "api_root": expected_root,
                          "recipient": recipient})
    return validated


def proof_root(wallet: str, token: str, amount: int, proof: list[str]) -> bytes:
    if type(amount) is not int or not 0 <= amount < 1 << 208:
        raise EvidenceError("atoms_out_of_range")
    if not isinstance(proof, list) or len(proof) > 64:
        raise EvidenceError("proof_unavailable_or_oversized")
    # abi.encode(address,address,uint256): three 32-byte words, single Keccak leaf.
    leaf = keccak256(data(address(wallet), 20).rjust(32, b"\0")
                     + data(address(token), 20).rjust(32, b"\0") + amount.to_bytes(32, "big"))
    for sibling in proof:
        node = data(sibling, 32)
        leaf = keccak256(min(leaf, node) + max(leaf, node))
    return leaf


def rpc_request(method: str, params: list, call_id: int) -> dict:
    return {"url": RPC, "method": "POST", "json": {
        "jsonrpc": "2.0", "id": call_id, "method": method, "params": params}}


def analyze(wallet: str, exchange: Callable[[dict], tuple[Any, int]]) -> dict:
    """Bounded request algorithm, shared by capture and original-receipt replay.

    exchange returns decoded original JSON and received_ms. No state/write/signer API.
    RPC answers are provider attestations, not independently verified consensus proofs.
    """
    wallet = address(wallet)
    api = {"url": f"https://api.merkl.xyz/v4/users/{wallet}/rewards?chainId={CHAIN_ID}",
           "method": "GET", "json": None}
    payload, observed = exchange(api)
    rows = reward_rows(payload)
    base = {"schema": SCHEMA, "chain_id": CHAIN_ID, "observed_ms": observed,
            "reward_rows": len(rows), "allocation_verified_count": 0,
            "remaining_allocation_count": 0, "rewards": [],
            "qualified": False, "funded_action_authorized": False,
            "net_profit_usd": None, "source_commit": SOURCE_COMMIT}
    if not rows:
        return {**base, "status": "no_rewards_reported", "coverage": "api_wallet_chain_only"}
    calls = 0

    def rpc(method, params):
        nonlocal calls
        calls += 1
        request = rpc_request(method, params, calls)
        value, received = exchange(request)
        base["observed_ms"] = received
        if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                or type(value.get("id")) is not int or value["id"] != calls
                or "error" in value or "result" not in value):
            raise EvidenceError("rpc_error_or_identity")
        return value["result"]

    if quantity(rpc("eth_chainId", [])) != CHAIN_ID:
        raise EvidenceError("rpc_chain_mismatch")
    block = rpc("eth_getBlockByNumber", ["finalized", False])
    if not isinstance(block, dict):
        raise EvidenceError("finalized_block_unavailable")
    block_hash = "0x" + data(block.get("hash"), 32).hex()
    height, timestamp = quantity(block.get("number")), quantity(block.get("timestamp"))
    if not 0 <= base["observed_ms"] - timestamp * 1000 <= 3600_000:
        raise EvidenceError("finalized_block_stale_or_future")
    pin = {"blockHash": block_hash, "requireCanonical": True}

    def call(calldata):
        return rpc("eth_call", [{"to": DISTRIBUTOR, "data": calldata}, pin])

    root = data(call(selector_of("getMerkleRoot()")), 32)
    if not any(root):
        raise EvidenceError("uninitialized_root")
    for row in rows:
        token = row["token"]
        proved = (row["recipient"] in (None, wallet) and row["api_root"] in (None, root)
                  and proof_root(wallet, token, row["amount"], row["proof"]) == root)
        if not proved:
            base["rewards"].append({"token": token, "allocation_verified": False,
                                    "reason": "proof_not_in_accepted_root", "remaining_atoms": None})
            continue
        args = data(wallet, 20).rjust(32, b"\0") + data(token, 20).rjust(32, b"\0")
        claim = data(call(selector_of("claimed(address,address)") + args.hex()), 96)
        claimed = int.from_bytes(claim[:32], "big")
        claimed_at = int.from_bytes(claim[32:64], "big")
        if claimed >= 1 << 208 or claimed_at >= 1 << 48 or claimed_at > timestamp:
            raise EvidenceError("claimed_state_invalid")
        if claimed > row["amount"]:
            base["rewards"].append({"token": token, "allocation_verified": False,
                                    "reason": "cumulative_amount_below_claimed", "remaining_atoms": None})
            continue
        remaining = row["amount"] - claimed
        base["allocation_verified_count"] += 1
        base["remaining_allocation_count"] += int(remaining > 0)
        base["rewards"].append({"token": token, "allocation_verified": True,
                                "cumulative_atoms": str(row["amount"]), "claimed_atoms": str(claimed),
                                "remaining_atoms": str(remaining), "pending_atoms_excluded": str(row["pending"]),
                                "api_claimed_matches_chain": row["api_claimed"] == claimed,
                                "eligibility_verified": False, "claim_execution_verified": False,
                                "value_cost_verified": False})
    return {**base, "status": "allocation_checked", "wallet": wallet,
            "distributor": DISTRIBUTOR, "block_number": height, "block_hash": block_hash,
            "block_timestamp_s": timestamp, "accepted_root": "0x" + root.hex(),
            "coverage": "provider_attested_allocation_at_finalized_block",
            "missing": ["deployed_implementation_review", "claim_recipient_and_transferability",
                        "executable_sale", "claim_and_sale_costs", "execution_path"]}


def replay(bundle: dict, *, at_ms: int) -> dict:
    """Reconstruct evidence from exact saved request/response bytes, never stored flags."""
    if (not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA
            or type(at_ms) is not int or at_ms < 0):
        raise EvidenceError("bundle_schema_or_clock")
    receipts = bundle.get("receipts")
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= MAX_REQUESTS:
        raise EvidenceError("receipt_count")
    consumed = 0
    previous = None
    first = None

    def exchange(request):
        nonlocal consumed, previous, first
        if consumed >= len(receipts):
            raise EvidenceError("missing_receipt")
        receipt = receipts[consumed]
        consumed += 1
        if (not isinstance(receipt, dict)
                or json.dumps(receipt.get("request"), sort_keys=True, allow_nan=False)
                != json.dumps(request, sort_keys=True, allow_nan=False)):
            raise EvidenceError("receipt_request_mismatch")
        start, end = receipt.get("started_ms"), receipt.get("received_ms")
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= at_ms
                or at_ms - start > 600_000 or end - start > 30_000
                or (previous is not None and start < previous)):
            raise EvidenceError("receipt_clock_or_staleness")
        first = start if first is None else first
        if end - first > 120_000:
            raise EvidenceError("capture_window_exceeded")
        previous = end
        encoded = receipt.get("body_hex")
        if not isinstance(encoded, str) or len(encoded) > MAX_BODY * 2:
            raise EvidenceError("response_size")
        try:
            raw = bytes.fromhex(encoded)
        except ValueError as exc:
            raise EvidenceError("invalid_body_encoding") from exc
        if hashlib.sha256(raw).hexdigest() != receipt.get("body_sha256"):
            raise EvidenceError("body_hash_mismatch")
        if type(receipt.get("http_status")) is not int or receipt["http_status"] != 200:
            raise EvidenceError("http_unsuccessful")
        return parse_json(raw), end

    result = analyze(bundle.get("wallet"), exchange)
    if consumed != len(receipts):
        raise EvidenceError("unconsumed_receipts")
    result["requests"] = consumed
    return result


def opportunities(bundle: dict, *, at_ms: int):
    """Existing hunter collector seam: allocation leads only, never inferred dollar EV."""
    from kaiba.core.schemas import Chain
    from kaiba.hunters.ev import OpportunityEvidence, OpportunityKind

    result = replay(bundle, at_ms=at_ms)
    leads = []
    for row in result["rewards"]:
        if not row["allocation_verified"] or int(row["remaining_atoms"]) <= 0:
            continue
        # Token + wallet identity makes distinct allocations distinct in the existing registry.
        identity = hashlib.sha256((result["wallet"] + row["token"]).encode()).hexdigest()[:24]
        leads.append(OpportunityEvidence(
            kind=OpportunityKind.AIRDROP, name=f"Merkl earned allocation {identity}", chain=Chain.BASE,
            official_url="https://app.merkl.xyz", sources=["merkl-allocation-proof"], wallet_count=1,
            first_seen_ms=result["observed_ms"], expected_value_usd=None,
            meta={"entitlement": {**row, "block_hash": result["block_hash"],
                                  "accepted_root": result["accepted_root"],
                                  "observed_ms": result["observed_ms"]},
                  "cost_basis": "zero_new_participation_cost; claim_and_sale_costs_unavailable",
                  "unpriced_costs": ["claim", "sale", "execution_loss"], "qualification": {
                      "official_source_verified": True, "eligibility_verified": False,
                      "value_cost_verified": False, "execution_path_verified": False,
                      "participation_open": False, "observed_ms": result["observed_ms"]}}))
    return leads
