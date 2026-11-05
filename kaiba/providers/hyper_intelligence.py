"""Bounded latest-state facts for observed networks, never an executor or grader.

The default HyperEVM RPC documents latest state only for eth_call/getCode/balance.
The header is context, NOT a pinned state snapshot. Every answer retains its own
receipt; unknown supply, balances, safety, ownership and PnL are never zero-filled.
"""
from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.limiter import Priority
from kaiba.core.schemas import OBSERVED_NETWORKS, EvidenceBasis, ObservedNetwork, digest, now_ms
from kaiba.providers._http import request_json
from kaiba.providers.hyper_rpc import PROVIDER, _quantity, probe_network

SCHEMA = "kaiba.network-facts/1"
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
SELECTORS = {"decimals": "0x313ce567", "totalSupply": "0x18160ddd", "symbol": "0x95d89b41"}


def _word(raw: Any) -> int | None:
    if not isinstance(raw, str) or len(raw) != 66 or not _HEX.fullmatch(raw):
        return None
    return int(raw[2:], 16)


def _bytes(raw: Any, maximum: int) -> bytes | None:
    if not isinstance(raw, str) or len(raw) > 2 + maximum * 2 or not _HEX.fullmatch(raw):
        return None
    return bytes.fromhex(raw[2:])


def _symbol(raw: Any) -> str | None:
    data = _bytes(raw, 256)
    if data is None:
        return None
    if len(data) == 32:  # legacy bytes32 metadata, still only a provider claim
        content = data.rstrip(b"\0")
    elif len(data) >= 64 and int.from_bytes(data[:32]) == 32:
        length = int.from_bytes(data[32:64])
        if not 1 <= length <= 128 or len(data) != 64 + ((length + 31) // 32) * 32:
            return None
        content = data[64:64 + length]
        if any(data[64 + length:]):
            return None
    else:
        return None
    try:
        value = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value if value and not any(unicodedata.category(c).startswith("C") for c in value) else None


def _receipt_view(receipt: Any) -> dict[str, Any]:
    return {"provider": receipt.provider, "endpoint": receipt.endpoint,
            "basis": receipt.basis.value, "observed_at_ms": receipt.observed_at_ms,
            "request_digest": receipt.request_digest, "response_digest": receipt.response_digest}


class _Reader:
    def __init__(self, network: str | ObservedNetwork, address: str, settings: Any | None):
        self.url = ""
        self.proof: dict[str, Any] | None = None
        self.receipts: list[dict[str, Any]] = []
        self.unknowns: list[str] = []
        self.attempts_upper_bound = 0
        self.deadline = time.monotonic() + 25
        self.address = address.strip().lower() if isinstance(address, str) else ""
        self.network: ObservedNetwork | None = None
        self.expected: int | None = None
        self.native_symbol: str | None = None
        self.native_decimals: int | None = None
        try:
            self.network = ObservedNetwork(network)
        except (ValueError, TypeError):
            self.unknowns.append("network_not_observed")
            return
        spec = OBSERVED_NETWORKS[self.network]
        self.expected = spec.chain_id
        self.native_symbol, self.native_decimals = spec.native_symbol, spec.native_decimals
        if not _ADDRESS.fullmatch(self.address):
            self.unknowns.append("address_invalid")
            return
        if self.expected is None:
            self.unknowns.append("mainnet_identity_unverified")
            return
        if self.expected in spec.refused_chain_ids:
            self.unknowns.append("canonical_chain_id_refused")
            return
        self.url = str(getattr(settings or get_settings(), f"{self.network.value}_rpc_url", "") or "").strip()
        if not self.url:
            self.unknowns.append("rpc_not_configured")
            return
        result = probe_network(self.url, expected_chain_id=self.expected)
        self.proof = result.view()
        self.receipts = self.proof["receipts"].copy()
        self.attempts_upper_bound = sum(r["basis"] != "cached" for r in self.receipts)
        if not result.available:
            self.unknowns.extend("network:" + reason for reason in result.blockers)

    @property
    def ready(self) -> bool:
        return self.proof is not None and self.proof.get("available") is True and not self.unknowns

    def call(self, method: str, params: list[Any], field: str) -> Any:
        # Literal read methods only. Calldata is constructed only from SELECTORS below.
        if method not in {"eth_getCode", "eth_call", "eth_getBalance", "eth_getTransactionCount"}:
            raise ValueError("unsupported_read_method")
        if time.monotonic() >= self.deadline:
            self.unknowns.append(field + ":observation_budget_exhausted")
            return None
        request_id = "kaiba-facts:" + field
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        fetched = request_json(
            # Same cooldown family as the chain probe: upstream IP limits cover both.
            PROVIDER, "network.facts." + method, self.url, method="POST", json_body=body,
            priority=Priority.RESEARCH, ttl_s=10, stale_grace_s=0, retries=0,
            timeout_s=min(3.0, max(0.1, self.deadline - time.monotonic())), wait_for_slot_s=1.0,
            cache_key="network-facts:" + digest({"url": self.url, "expected": self.expected, "body": body}),
        )
        r = fetched.receipt
        self.receipts.append(_receipt_view(r))
        self.attempts_upper_bound += r.basis is not EvidenceBasis.CACHED
        age = now_ms() - r.observed_at_ms
        data = fetched.data
        if r.basis not in {EvidenceBasis.PROVIDER_REPORTED, EvidenceBasis.CACHED} or not 0 <= age <= 10_000:
            self.unknowns.append(field + ":evidence_unavailable")
        elif not isinstance(data, dict) or data.get("jsonrpc") != "2.0" or data.get("id") != request_id:
            self.unknowns.append(field + ":response_identity_mismatch")
        elif "error" in data or "result" not in data:
            self.unknowns.append(field + ":method_unavailable")
        else:
            return data["result"]
        return None

    def view(self, kind: str, facts: dict[str, Any]) -> dict[str, Any]:
        return {"schema": SCHEMA, "kind": kind, "network": self.network.value if self.network else None,
                "chain_id": self.expected, "address": self.address if _ADDRESS.fullmatch(self.address) else None,
                "native_symbol": self.native_symbol, "native_decimals": self.native_decimals,
                "facts": facts, "unknowns": list(dict.fromkeys(self.unknowns)),
                "network_proof": self.proof, "receipts": self.receipts,
                "method_attempts_upper_bound": self.attempts_upper_bound,
                "state_basis": "independent_latest_observations", "atomic_snapshot": False,
                "execution_ready": False, "safety_verified": None, "profitability_proven": None,
                "limitations": ["no_price_or_liquidity_evidence", "no_holder_or_entity_analysis",
                                "no_ownership_or_profitability_inference", "not_a_historical_or_complete_snapshot"]}


def token_facts(network: str | ObservedNetwork, address: str, *, settings: Any | None = None) -> dict[str, Any]:
    """At most six read methods including network proof; no arbitrary calldata or token writes."""
    reader = _Reader(network, address, settings)
    facts: dict[str, Any] = {"code_present": None, "code_sha256": None, "symbol": None,
                             "decimals": None, "total_supply_base_units": None,
                             "liquidity_usd": None, "market_cap_usd": None, "holders": None}
    if not reader.ready:
        return reader.view("token", facts)
    code = _bytes(reader.call("eth_getCode", [reader.address, "latest"], "code"), 65_536)
    if code is None:
        reader.unknowns.append("code_unavailable")
        return reader.view("token", facts)
    facts["code_present"] = bool(code)
    if not code:
        reader.unknowns.append("contract_code_absent")
        return reader.view("token", facts)
    facts["code_sha256"] = hashlib.sha256(code).hexdigest()
    for name, selector in SELECTORS.items():
        raw = reader.call("eth_call", [{"to": reader.address, "data": selector}, "latest"], name)
        value = _symbol(raw) if name == "symbol" else _word(raw)
        if name == "decimals" and value is not None and value > 255:
            value = None
        if value is None:
            reader.unknowns.append(name + "_unavailable")
        else:
            key = "total_supply_base_units" if name == "totalSupply" else name
            facts[key] = str(value) if name == "totalSupply" else value
    return reader.view("token", facts)


def wallet_facts(network: str | ObservedNetwork, address: str, *, settings: Any | None = None) -> dict[str, Any]:
    """At most five reads. Native balance/nonce/code only; never all holdings, ownership or PnL."""
    reader = _Reader(network, address, settings)
    facts: dict[str, Any] = {"native_balance_base_units": None, "transaction_count": None,
                             "account_code_present": None, "token_holdings_enumerated": False,
                             "realized_pnl": None, "wallet_grade": None, "owned_by_operator": None}
    if not reader.ready:
        return reader.view("wallet", facts)
    for method, field in (("eth_getBalance", "native_balance_base_units"),
                          ("eth_getTransactionCount", "transaction_count")):
        value = _quantity(reader.call(method, [reader.address, "latest"], field))
        if value is None:
            reader.unknowns.append(field + "_unavailable")
        else:
            facts[field] = str(value) if field == "native_balance_base_units" else value
    code = _bytes(reader.call("eth_getCode", [reader.address, "latest"], "code"), 65_536)
    if code is None:
        reader.unknowns.append("code_unavailable")
    else:
        facts["account_code_present"] = bool(code)
    return reader.view("wallet", facts)
