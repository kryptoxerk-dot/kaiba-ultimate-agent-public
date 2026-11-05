"""Bounded read-only network evidence for the HyperEVM / Elysium integration.

Canonical network identity comes from the lead-owned core configuration, passed
as ``expected_chain_id``. This module is not a chain registry or an executor.
A configured URL, successful HTTP response, or matching ticker is insufficient:
the RPC must report the expected chain and a recent, well-formed block header.
Unknown mainnet identity causes no RPC calls. All traffic uses the existing
cache, provider policy and limiter; each RPC method is charged separately.
Probe backoff has its own namespace so a new public host cannot penalize the
``rpc`` bucket protection uses on unrelated hosts. No account quota is reset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis, Receipt, digest, now_ms
from kaiba.providers._http import request_json

SCHEMA = "kaiba.network-probe/1"
PROVIDER = "network-probe-rpc"
_QUANTITY = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _quantity(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) > 66 or not _QUANTITY.fullmatch(value):
        return None
    return int(value, 16)


@dataclass(frozen=True, slots=True)
class NetworkProbe:
    expected_chain_id: int | None
    observed_chain_id: int | None = None
    block_number: int | None = None
    block_hash: str | None = None
    block_timestamp_ms: int | None = None
    block_observed_ms: int | None = None
    gas_limit: int | None = None
    base_fee_per_gas_wei: int | None = None
    blockers: tuple[str, ...] = ()
    receipts: tuple[Receipt, ...] = ()

    @property
    def available(self) -> bool:
        return not self.blockers and self.block_hash is not None

    def view(self) -> dict[str, Any]:
        """No credentials, RPC URL, wallet address or raw provider exception text."""
        return {
            "schema": SCHEMA, "available": self.available,
            "expected_chain_id": self.expected_chain_id, "observed_chain_id": self.observed_chain_id,
            "block_number": self.block_number, "block_hash": self.block_hash,
            "block_timestamp_ms": self.block_timestamp_ms, "block_observed_ms": self.block_observed_ms,
            "gas_limit": self.gas_limit, "base_fee_per_gas_wei": self.base_fee_per_gas_wei,
            "blockers": list(self.blockers), "execution_ready": False,
            "note": "Network evidence only; token, wallet, route, fee and execution capabilities are separate.",
            "receipts": [{"provider": r.provider, "endpoint": r.endpoint,
                          "basis": r.basis.value, "observed_at_ms": r.observed_at_ms,
                          "request_digest": r.request_digest, "response_digest": r.response_digest}
                         for r in self.receipts],
        }


def probe_network(
    rpc_url: str,
    *,
    expected_chain_id: int | None,
    at_ms: int | None = None,
    max_block_age_s: int = 120,
) -> NetworkProbe:
    """Read chain ID, then a live block, with at most two bounded RPC methods.

    ``expected_chain_id=None`` deliberately represents an unverified mainnet.
    It must not be replaced by a testnet ID or another same-named chain's ID.
    Only the two literal read methods below can be sent. No financial method,
    arbitrary calldata, retry loop, alternate host or hidden provider fallback.
    """
    if expected_chain_id is None:
        return NetworkProbe(None, blockers=("mainnet_identity_unverified",))
    if type(expected_chain_id) is not int or not 0 < expected_chain_id < 2**63:
        return NetworkProbe(None, blockers=("expected_chain_id_invalid",))
    if type(max_block_age_s) is not int or not 1 <= max_block_age_s <= 300:
        return NetworkProbe(expected_chain_id, blockers=("freshness_bound_invalid",))
    try:
        url = urlsplit(rpc_url)
        valid_url = (url.scheme == "https" and bool(url.hostname) and url.username is None
                     and url.password is None and not url.fragment and url.port in (None, 443))
    except (TypeError, ValueError):
        valid_url = False
    if not valid_url:
        return NetworkProbe(expected_chain_id, blockers=("rpc_url_invalid",))
    at = now_ms() if at_ms is None else at_ms
    if type(at) is not int or at <= 0:
        return NetworkProbe(expected_chain_id, blockers=("observation_clock_invalid",))
    receipts: list[Receipt] = []

    def read(method: str, params: list[Any], *, ttl_s: int) -> tuple[Any, str | None]:
        request_id = "kaiba-network:" + method
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        fetched = request_json(
            PROVIDER, "network." + method, rpc_url, method="POST", json_body=body,
            priority=Priority.RESEARCH, ttl_s=ttl_s, stale_grace_s=0,
            cache_key="network-probe:" + digest({"url": rpc_url, "expected": expected_chain_id, "body": body}),
            timeout_s=4.0, retries=0, wait_for_slot_s=1.0,
        )
        receipts.append(fetched.receipt)
        if fetched.receipt.basis not in (EvidenceBasis.PROVIDER_REPORTED, EvidenceBasis.CACHED):
            return None, "rpc_evidence_unavailable"
        receipt_clock = at if at_ms is not None else now_ms()
        observation_age = receipt_clock - fetched.receipt.observed_at_ms
        if observation_age < -5_000 or observation_age > ttl_s * 1000:
            return None, "rpc_evidence_out_of_window"
        data = fetched.data
        if not isinstance(data, dict):
            return None, "rpc_unavailable"
        if data.get("jsonrpc") != "2.0" or data.get("id") != request_id:
            return None, "rpc_response_identity_mismatch"
        if "error" in data or "result" not in data:
            return None, "rpc_method_unavailable"
        return data["result"], None

    raw_chain, error = read("eth_chainId", [], ttl_s=60)
    observed = _quantity(raw_chain)
    if error or observed != expected_chain_id:
        return NetworkProbe(expected_chain_id, observed_chain_id=observed,
                            blockers=(error or ("rpc_chain_id_malformed" if observed is None else "rpc_chain_id_mismatch"),),
                            receipts=tuple(receipts))
    header, error = read("eth_getBlockByNumber", ["latest", False], ttl_s=5)
    if error or not isinstance(header, dict):
        return NetworkProbe(expected_chain_id, observed_chain_id=observed,
                            blockers=(error or "block_header_unavailable",), receipts=tuple(receipts))
    number, stamp, gas_limit = (_quantity(header.get(k)) for k in ("number", "timestamp", "gasLimit"))
    block_hash = header.get("hash")
    base_fee = _quantity(header.get("baseFeePerGas"))
    bad_header = (number is None or stamp is None or stamp <= 0 or gas_limit is None or gas_limit <= 0
                  or not isinstance(block_hash, str) or not _HASH.fullmatch(block_hash)
                  or block_hash == "0x" + "0" * 64)
    if bad_header:
        return NetworkProbe(expected_chain_id, observed_chain_id=observed,
                            blockers=("block_header_malformed",), receipts=tuple(receipts))
    timestamp_ms = stamp * 1000
    if at_ms is None:
        at = now_ms()
    blockers = ()
    if timestamp_ms > at + 5_000:
        blockers = ("block_header_in_future",)
    elif at - timestamp_ms > max_block_age_s * 1000:
        blockers = ("block_header_stale",)
    return NetworkProbe(expected_chain_id, observed_chain_id=observed, block_number=number,
                        block_hash=block_hash, block_timestamp_ms=timestamp_ms,
                        block_observed_ms=receipts[-1].observed_at_ms, gas_limit=gas_limit,
                        base_fee_per_gas_wei=base_fee, blockers=blockers, receipts=tuple(receipts))
