"""One-shot Elysium launch research through two documented project relays.

This is separate from Kaiba's canonical network status. Agreement at a recent
block is provider-reported evidence. Public launch, settlement and execution
each require separate evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from typing import Any

from kaiba.core.schemas import EvidenceBasis, digest, now_ms
from kaiba.providers._http import request_json
from kaiba.providers.hyper_rpc import PROVIDER, probe_network

# Eleos and PerpMe document 99901 for the early-access network. The relays may
# share an upstream. Their agreement does not establish independent finality.
CHAIN_ID = 99901
RELAYS = {
    "eleos": "https://rpc.eleos.fun",
    "perpme": "https://rpc-elysium.perpme.fun",
}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
BYTECODE = re.compile(r"0x(?:[0-9a-fA-F]{2})*")


def check_launch(token: str | None = None) -> dict[str, Any]:
    """Compare overlapping recent blocks and optionally read exact-address code.

    At most seven read methods. The code requests use the agreed block hash and
    never fall back to a moving head or a numeric block on an RPC error.
    """
    if token is not None:
        if not ADDRESS.fullmatch(token) or int(token, 16) == 0:
            raise ValueError("token must be a nonzero 20-byte EVM address")
        token = token.lower()
    report: dict[str, Any] = {
        "schema": "kaiba.elysium-launch-check/1",
        "chain_id_hypothesis": CHAIN_ID,
        "public_launch_verified": False,
        "execution_ready": False,
        "token": token,
        "token_code_status": "not_requested" if token is None else "unknown",
        "relays": {},
        "additional_receipts": {},
    }

    def read(relay: str, method: str, params: list[Any]) -> Any:
        request_id = f"kaiba-elysium-launch:{method}"
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        fetched = request_json(
            PROVIDER, f"network.{method}", RELAYS[relay], method="POST", json_body=body,
            cache_key="elysium-launch:" + digest({"relay": relay, "body": body}),
            ttl_s=5, stale_grace_s=0, timeout_s=4, retries=0, wait_for_slot_s=1,
        )
        receipt = fetched.receipt
        report["additional_receipts"].setdefault(relay, []).append(receipt.model_dump(
            mode="json", exclude={"note"},
        ))
        age = now_ms() - receipt.observed_at_ms
        data = fetched.data
        if (receipt.basis in (EvidenceBasis.CACHED, EvidenceBasis.PROVIDER_REPORTED)
                and 0 <= age <= 5_000 and isinstance(data, dict) and data.get("jsonrpc") == "2.0"
                and data.get("id") == request_id and "error" not in data):
            return data.get("result")
        return None

    heads = {}
    for name, url in RELAYS.items():
        head = probe_network(url, expected_chain_id=CHAIN_ID)
        report["relays"][name] = head.view()
        heads[name] = head
    if not all(head.available for head in heads.values()):
        return {**report, "status": "relay_probe_unavailable"}

    low, high = sorted(heads, key=lambda name: heads[name].block_number)
    anchor = heads[low]
    block_hash = anchor.block_hash.lower()
    if anchor.block_number == heads[high].block_number:
        matches = (block_hash == heads[high].block_hash.lower()
                   and anchor.block_timestamp_ms == heads[high].block_timestamp_ms)
    else:
        number = hex(anchor.block_number)
        block = read(high, "eth_getBlockByNumber", [number, False])
        if not isinstance(block, dict):
            return {**report, "status": "overlap_header_unavailable"}
        matches = (str(block.get("hash", "")).lower() == block_hash and block.get("number") == number
                   and block.get("timestamp") == hex(anchor.block_timestamp_ms // 1000))
    if not matches:
        return {**report, "status": "overlap_header_disagreement"}

    report.update(status="relays_agree_at_recent_block", block_number=anchor.block_number,
                  block_hash=block_hash, block_timestamp_ms=anchor.block_timestamp_ms)
    if token is None:
        return report
    selector = {"blockHash": block_hash, "requireCanonical": True}
    codes = [read(relay, "eth_getCode", [token, selector]) for relay in RELAYS]
    if not all(isinstance(code, str) and len(code) <= 100_002 and BYTECODE.fullmatch(code) for code in codes):
        return report
    if codes[0].lower() != codes[1].lower():
        return {**report, "token_code_status": "relay_disagreement"}
    raw_code = bytes.fromhex(codes[0][2:])
    return {**report, "token_code_status": "present" if raw_code else "empty_at_block",
            "token_code_bytes": len(raw_code), "token_code_sha256": hashlib.sha256(raw_code).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="Optional exact EVM address to check at the shared block")
    args = parser.parse_args()
    try:
        report = check_launch(args.token)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
