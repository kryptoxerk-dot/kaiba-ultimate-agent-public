"""Read-only readiness of the observed networks (HyperEVM, Kinetiq Elysium) -- lead, 2026-11-02.

What Kaiba can and cannot do on each network, capability by capability, so neither the agent nor
the dashboard infers a trading route from a working RPC (board HYPER-ELYSIUM-LEAD-CLAUDE-20261102).

The network reads are Codex's :func:`kaiba.providers.hyper_rpc.probe_network`: chain id, then the
latest header, two keyless methods through the shared limiter and cache, no wallet, no calldata.
:func:`probe_all` runs them from the kaiba-ops job ``network_probe`` and stores each probe's public
view in ``kv``. :func:`status` reads those two ``kv`` rows and nothing else, so an MCP call or a
page load never makes a network request.

The capability states are DEFINITIONAL: read off this tree and the pinned gmgn-cli 1.6.1 on the
box on 2026-11-02, not measured outcomes. ``execution_ready`` is False on every network, whatever a
probe says.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from kaiba.core.db import fetch_all, jdump, jload
from kaiba.core.schemas import OBSERVED_NETWORKS, NetworkSpec, ObservedNetwork, now_ms

SCHEMA = "kaiba.network-status/1"
KV_PREFIX = "network:probe:"
#: A stored probe older than this many job intervals is reported stale, not current.
STALE_INTERVALS = 3

#: (state, why) per capability, the same on both networks. gmgn-cli 1.6.1's validateChain accepts
#: only the seven ``Chain`` values for its core commands; its search command shape-checks any slug
#: and leaves the answer to the server, so search is unverified rather than unsupported.
CAPABILITIES: dict[str, tuple[str, str]] = {
    "gmgn_core": ("unsupported", "gmgn-cli 1.6.1 core commands (token, market, track, portfolio, swap) "
                                 "accept sol, bsc, base, eth, robinhood, arc and stable only; never called"),
    "gmgn_search": ("unverified", "the CLI passes the slug to the server, which decides; not called"),
    "token_metadata": ("", ""),  # filled by _capabilities from ON_DEMAND
    "token_market_observation": ("", ""),
    # Renamed from token_market_data on 2026-11-02 when vendor prices became readable: what is
    # still missing is anything a fill could rely on.
    "executable_market": ("not_built", "no executable quote, depth, fee/tax or holder data on this network; "
                                       "vendor prices are not fillable quotes"),
    "wallet_account_facts": ("", ""),
    "wallet_intelligence": ("not_built", "no token inventory, history, grade or PnL on this network"),
    "trade_execution": ("unsupported", "no GMGN chain, signer-policy entry, lane or risk budget"),
    "grok_research": ("recorded_not_tradeable", "cards naming it are recorded with "
                                                "network_observed_not_tradeable, never decided"),
}
#: PARTIAL capabilities served on demand: (MCP tool, what it reads, needs the RPC, networks the
#: source is verified for or None for any). Codex's kaiba.providers.hyper_intelligence (8904f88)
#: proves the chain first, then fixed latest-state reads; kaiba.providers.observed_market (0663cf3)
#: makes at most one DexScreener request, whose hyperevm slug is verified only for chain 999. None
#: is a snapshot, a grade, a safety verdict, a quote or a route.
ON_DEMAND: dict[str, tuple[str, str, bool, frozenset[str] | None]] = {
    "token_metadata": ("kaiba_network_token_facts",
                       "code sha256, symbol, decimals and total supply; latest state, at most 6 reads", True, None),
    "token_market_observation": ("kaiba_network_token_market",
                                 "DexScreener pool prices, liquidity, volume and FDV / market cap; vendor-reported, "
                                 "at most 1 request; not executable depth or a quote", False, frozenset({"hyperevm"})),
    "wallet_account_facts": ("kaiba_network_wallet_facts",
                             "native balance, nonce and code; no token inventory, grade or PnL; at most 5 reads", True, None),
}


def _capabilities(network: ObservedNetwork, spec: NetworkSpec, configured: bool) -> dict[str, dict[str, str]]:
    caps = {k: {"state": s, "why": w} for k, (s, w) in CAPABILITIES.items()}
    for key, (tool, what, needs_rpc, networks) in ON_DEMAND.items():
        if spec.chain_id is None:
            caps[key] = {"state": "unavailable", "why": "mainnet identity unverified: the reader makes zero calls"}
        elif needs_rpc and not configured:
            caps[key] = {"state": "unavailable", "why": "no RPC configured: the reader makes zero calls"}
        elif networks is not None and network.value not in networks:
            caps[key] = {"state": "unavailable", "why": "vendor network mapping unverified: zero calls"}
        else:
            caps[key] = {"state": "partial_on_demand", "why": f"{tool}: {what}"}
    return caps


def _settings() -> Any:
    from kaiba.core.config import get_settings

    return get_settings()


def rpc_url(network: ObservedNetwork, settings: Any | None = None) -> str:
    return str(getattr(settings or _settings(), f"{network.value}_rpc_url", "") or "").strip()


def _zero_call(blocker: str) -> dict[str, Any]:
    """A branch that never calls the prober: zero RPC methods, provably."""
    return {"available": False, "blockers": [blocker], "method_attempts_upper_bound": 0, "zero_call_branch": True}


def _probe_one(spec: NetworkSpec, url: str, prober: Callable[..., Any]) -> dict[str, Any]:
    if spec.chain_id is None:
        return _zero_call("mainnet_identity_unverified")
    if spec.chain_id in spec.refused_chain_ids:
        return _zero_call("canonical_chain_id_refused")
    if not url:
        return _zero_call("rpc_not_configured")
    view = prober(url, expected_chain_id=spec.chain_id).view()
    # An UPPER BOUND on RPC methods attempted, not measured upstream requests: a cache hit is
    # excluded, but an unavailable receipt may be a local limiter or policy refusal before any HTTP.
    attempts = sum(r.get("basis") != "cached" for r in view.get("receipts") or [])
    return {**view, "method_attempts_upper_bound": attempts, "zero_call_branch": False}


def probe_all(conn: sqlite3.Connection, *, interval_s: int, settings: Any | None = None,
              prober: Callable[..., Any] | None = None, at_ms: int | None = None) -> dict[str, Any]:
    """Probe every observed network once and store each public view. At most two RPC methods each."""
    if prober is None:
        from kaiba.providers.hyper_rpc import probe_network as prober
    at = now_ms() if at_ms is None else at_ms
    out: dict[str, Any] = {}
    for network, spec in OBSERVED_NETWORKS.items():
        view = _probe_one(spec, rpc_url(network, settings), prober)
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (KV_PREFIX + network.value, jdump({"probed_ms": at, "interval_s": interval_s, "view": view}), at),
        )
        out[network.value] = {"available": bool(view.get("available")), "blockers": view.get("blockers") or [],
                              "method_attempts_upper_bound": view["method_attempts_upper_bound"],
                              "zero_call_branch": view["zero_call_branch"]}
    conn.commit()
    return {"networks": out,
            "method_attempts_upper_bound": sum(v["method_attempts_upper_bound"] for v in out.values())}


def _identity(spec: NetworkSpec, configured: bool, stored: dict[str, Any], at: int) -> dict[str, Any]:
    if spec.chain_id is None:
        return {"state": "mainnet_identity_unverified", "blockers": ["mainnet_identity_unverified"]}
    if not configured:
        return {"state": "rpc_not_configured", "blockers": ["rpc_not_configured"]}
    view, probed, interval = stored.get("view"), stored.get("probed_ms"), stored.get("interval_s")
    if not isinstance(view, dict) or type(probed) is not int:
        return {"state": "not_probed_yet", "blockers": ["not_probed_yet"]}
    if type(interval) is not int or interval <= 0:
        return {"state": "probe_record_invalid", "blockers": ["probe_interval_invalid"]}
    if probed > at:  # never made fresh by clamping its age to zero
        return {"state": "probe_record_invalid", "blockers": ["probe_time_in_future"]}
    expected, observed = view.get("expected_chain_id"), view.get("observed_chain_id")
    if type(expected) is not int or expected != spec.chain_id:
        # A record written for another spec (an edited id, a foreign row) proves nothing about this one.
        return {"state": "evidence_spec_mismatch", "blockers": ["probe_expected_chain_id_not_current_spec"]}
    age_s = (at - probed) // 1000
    stamp, read = view.get("block_timestamp_ms"), view.get("block_observed_ms")
    out = {"probed_ms": probed, "age_s": age_s, "block_number": view.get("block_number"),
           # from the header's own receipt, not the job's start (MEASURED: -0.3 s that way)
           "block_age_at_read_s": (round((read - stamp) / 1000, 1)
                                   if type(stamp) is int and type(read) is int else None),
           "base_fee_per_gas_wei": view.get("base_fee_per_gas_wei"),
           "blockers": [str(b)[:80] for b in view.get("blockers") or []][:8]}
    if age_s > STALE_INTERVALS * interval:
        return {**out, "state": "stale"}
    if view.get("available") is not True or out["blockers"]:
        return {**out, "state": "unavailable"}
    if type(observed) is not int or observed != spec.chain_id:
        return {**out, "state": "evidence_spec_mismatch", "blockers": ["probe_observed_chain_id_not_current_spec"]}
    return {**out, "state": "verified"}


def status(conn: sqlite3.Connection | None = None, *, settings: Any | None = None,
           at_ms: int | None = None) -> dict[str, Any]:
    """Every observed network's identity, latest probe and capability states. Reads ``kv`` only.

    Never raises: an unreadable store becomes an ``unavailable`` identity, so a page still renders."""
    keys = [KV_PREFIX + n.value for n in OBSERVED_NETWORKS]
    try:
        if conn is None:
            from kaiba.core.db import get_conn

            conn = get_conn()
        rows = {r["key"]: r["value"] for r in fetch_all(
            conn, f"SELECT key, value FROM kv WHERE key IN ({','.join('?' for _ in keys)})", keys)}
        read_error = None
    except Exception as exc:  # noqa: BLE001 - a readiness view must render without its store
        rows, read_error = {}, "probe_store_unreadable:" + type(exc).__name__
    at = now_ms() if at_ms is None else at_ms  # after the read: a probe written meanwhile is not future
    networks = []
    for network, spec in OBSERVED_NETWORKS.items():
        configured = bool(rpc_url(network, settings))
        stored = jload(rows.get(KV_PREFIX + network.value), {})
        identity = _identity(spec, configured, stored if isinstance(stored, dict) else {}, at)
        if read_error and identity["state"] == "not_probed_yet":
            identity = {"state": "unavailable", "blockers": [read_error]}
        networks.append({
            "network": network.value, "name": spec.name, "chain_id": spec.chain_id,
            "refused_chain_ids": sorted(spec.refused_chain_ids), "stage": spec.stage,
            "native_symbol": spec.native_symbol, "native_decimals": spec.native_decimals,
            "settles_to": spec.settles_to.value if spec.settles_to else None,
            "source": spec.source, "checked": spec.checked, "rpc_configured": configured,
            "identity": identity,
            "capabilities": _capabilities(network, spec, configured),
            "execution_ready": False,
        })
    return {"schema": SCHEMA, "read_ms": at, "networks": networks,
            "note": "Network evidence only. A verified chain identity is not a token, wallet, route, fee "
                    "or execution capability."}
