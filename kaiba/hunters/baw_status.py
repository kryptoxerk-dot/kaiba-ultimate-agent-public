"""Typed, redacted readiness view of the EXISTING local BAW preflight worker (lead, 2026-11-02).

Reads only the worker's ``/status`` (``kaiba.hunters.baw_mint_worker``, owned and deployed by Codex)
over loopback with a 2 s timeout and the existing client config. ``/status`` reads the worker's own
SQLite ledger and in-memory connection snapshot; it never runs the Binance CLI or a provider. This
module never calls ``/refresh``, ``/stage`` or ``/preview``, never reads the wallet session, and never
returns a wallet address, a session/token value or a preview payload. Results are cached for 30 s so
page loads do not hammer the worker.

The readiness verdict is evidence for the operator, not authority: ``can_execute`` is False until an
execution adapter exists and is armed, whatever any field says. ``CLI_LIMITS`` records what the pinned
``@binance/agentic-wallet`` 1.12.0 can and cannot bind (published bundle + Binance public docs, read
2026-11-02, worklog docs/worklogs/claude-20261102.md).
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

CLIENT_CONFIG = Path.home() / ".config/kaiba/baw-worker-client.json"
TIMEOUT_S = 2.0
CACHE_S = 30
KNOWN_SERVICES = frozenset({"kaiba-baw-preflight/1"})
#: The worker's chain set (kaiba.execution.nft_mint_policy.CHAINS). Not imported: on the box the worker
#: runs from its own release under /opt/kaiba-baw, and ~/kaiba has no copy of that module.
#: base + eth added 2026-11-02 with Codex's BAW-FUNDED-MINTS (owner funded BAW on all five).
CHAINS = ("bsc", "robinhood", "sol", "base", "eth")

#: Pinned CLI 1.12.0, published bundle dist/index.js and developers.binance.com agentic-wallet pages.
CLI_LIMITS = (
    "evm_total_fee_ceiling_unsupported: contract-call preview takes --gasLimit (21e3-15e6, 'a cap, not a "
    "bypass') but no gasPrice/maxFeePerGas/maxPriorityFeePerGas; price is a backend tier",
    "solana_fee_ceiling_unverified: the CLI has no separate compute-unit or priority-fee option and sends "
    "--unsignedTx as given; a prebuilt transaction may itself carry ComputeBudget unit-limit/price "
    "instructions, but whether the backend preserves them, and the actual total fee and asset effects "
    "before broadcast, are UNVERIFIED",
    "preview_fields_undocumented: the bundle defines no fee, asset-change or risk fields in preview data",
    "execute_binds_by_requestId_only: 'execute takes just the requestId and reuses the previewed value'; "
    "no expiry handled client-side",
    "no_requestId_status_command: reconcile only by tx hash (wallet tx-history --tx) or the pending list",
    "no_idempotency_key: one fetch per execute, random trace id; backend de-duplication undocumented",
    "wallet_quota_scope_unknown: dailyLimit/quotaLeft are USD, whether they include gas is undocumented",
)

_cache: dict[str, Any] = {}


def _post_status(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    port = config.get("port")
    token = config.get("token")
    if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(token, str) or not token:
        raise ValueError("client_config_invalid")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/status", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only, no ambient proxy
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(128 * 1024))


def fetch(config_path: Path | str | None = None, *, timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """The raw worker status, or ``{"_unavailable": reason}``. Never raises."""
    path = Path(config_path) if config_path else CLIENT_CONFIG
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"_unavailable": "client_config_missing"}
    except (OSError, ValueError):
        return {"_unavailable": "client_config_unreadable"}
    try:
        out = _post_status(config, timeout)
    except urllib.error.HTTPError as exc:
        return {"_unavailable": "worker_auth_refused" if exc.code == 403 else f"worker_http_{exc.code}"}
    except TimeoutError:  # socket.timeout is an alias since 3.10
        return {"_unavailable": "worker_timeout"}
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        return {"_unavailable": "worker_timeout" if isinstance(reason, TimeoutError)
                else "worker_unreachable"}
    except ValueError as exc:
        return {"_unavailable": str(exc) if str(exc) == "client_config_invalid" else "worker_response_invalid"}
    except OSError:
        return {"_unavailable": "worker_unreachable"}
    return out if isinstance(out, dict) else {"_unavailable": "worker_response_invalid"}


def _ms(value: Any) -> int | None:
    """Epoch seconds or ms, or an ISO-8601 time WITH a timezone (the live CLI's devMode.expiresAt is
    "2026-11-10T00:05:22+08:00", Codex 2026-11-02). Naive, non-finite, non-positive or unparseable
    -> None, which the caller reads as "not active"; expiry against the clock is the caller's."""
    if type(value) in (int, float):
        if not math.isfinite(value) or value <= 0:
            return None
        return int(value * 1000) if value < 100_000_000_000 else int(value)
    if isinstance(value, str) and 0 < len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return int(parsed.timestamp() * 1000)
    return None


def _usd(micro: Any) -> float | None:
    return round(micro / 1_000_000, 6) if type(micro) is int else None


#: The only last_preview evidence fields passed through (kaiba.execution.baw_preview, schema
#: baw-preview-evidence/1). Anything else a future worker adds is dropped, so an address or a raw
#: payload cannot leak through this view.
EVIDENCE_SCALARS = ("schema", "raw_sha256", "local_request_sha256", "cli_version", "cli_sha256", "observed_ms",
                    "provider_simulation", "request_id_present", "expires_ms", "provider_confirmation_required",
                    "complete_asset_effects_verified", "locked_fee_ceiling_verified", "financial_execution")
EVIDENCE_COUNTS = ("change_counts", "risk_counts")
_HEX64 = frozenset("0123456789abcdef")


def _connection_state(conn: dict[str, Any], checked_ms: Any) -> str:
    """not_checked / unavailable / unpaired / connected, from the worker's own reason and clock.

    Not from its blocker list: a pre-2026-11-02 worker said ``wallet_pairing_required`` even before
    its first check and after a failed provider check, which would claim a pairing problem nobody
    observed."""
    if conn.get("connected") is True:
        return "connected"
    if not checked_ms or conn.get("reason") in (None, "connection_not_checked"):
        return "not_checked"
    if conn.get("reason") == "wallet_unconnected":
        return "unpaired"
    return "unavailable"


def _chain_flags(row: Any) -> dict[str, bool | None]:
    """supported / wallet_present, each True, False or None (unknown). Never the address itself.

    A missing flag stays unknown rather than becoming False (Codex review, 2026-11-02). A wallet key
    present but empty means the worker saw no address for that chain."""
    row = row if isinstance(row, dict) else {}
    supported = row.get("supported") if isinstance(row.get("supported"), bool) else None
    if "wallet" not in row:
        wallet = None
    else:
        wallet = isinstance(row["wallet"], str) and bool(row["wallet"])
    return {"supported": supported, "wallet_present": wallet}


def _last_preview(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    ident = raw.get("candidate_id")
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    kept = {k: evidence[k] for k in EVIDENCE_SCALARS
            if k in evidence and (evidence[k] is None or type(evidence[k]) in (bool, int, str))}
    for k in EVIDENCE_COUNTS:
        if isinstance(evidence.get(k), dict):
            kept[k] = {str(f)[:40]: v for f, v in evidence[k].items() if v is None or type(v) is int}
    blockers = [str(b)[:120] for b in evidence.get("blockers") or [] if isinstance(b, str)][:24]
    return {"candidate_id": ident if isinstance(ident, str) and len(ident) == 64 and set(ident) <= _HEX64 else None,
            "observed_ms": raw.get("observed_ms") if type(raw.get("observed_ms")) is int else None,
            "evidence_available": "raw_sha256" in kept, "evidence": kept, "evidence_blockers": blockers}


def view(raw: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Typed, redacted readiness. Unknown or missing fields stay unknown; nothing is invented."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    out: dict[str, Any] = {"can_execute": False, "cli_limits": list(CLI_LIMITS)}
    if "_unavailable" in raw:
        return {**out, "available": False, "stage": "worker_unavailable", "reason": raw["_unavailable"],
                "can_preview": False, "blockers": [raw["_unavailable"]]}
    service = raw.get("service")
    recognised = service in KNOWN_SERVICES and isinstance(raw.get("connection"), dict)
    conn = raw.get("connection") if isinstance(raw.get("connection"), dict) else {}
    settings = conn.get("settings") if isinstance(conn.get("settings"), dict) else {}
    mode = settings.get("devMode") if isinstance(settings.get("devMode"), dict) else {}
    expires = _ms(mode.get("expiresAt"))
    dev_active = mode.get("enabled") is True and expires is not None and expires > now
    raw_chains = conn.get("chains") if isinstance(conn.get("chains"), dict) else {}
    chains = {c: _chain_flags(raw_chains[c]) for c in CHAINS if c in raw_chains}
    gas = raw.get("gas") if isinstance(raw.get("gas"), dict) else {}
    state = _connection_state(conn, raw.get("connection_checked_ms"))
    connected = state == "connected"
    usable_chain = any(v["supported"] is True and v["wallet_present"] is True for v in chains.values())
    connection_words = {"wallet_pairing_required", "wallet_connection_not_checked", "wallet_connection_unavailable"}
    blockers = [str(b)[:120] for b in raw.get("blockers") or []
                if isinstance(b, str) and b not in connection_words][:12]  # re-derived below, per state
    if not recognised:
        blockers.append("worker_version_unrecognised")
    if state == "not_checked":
        blockers.append("wallet_connection_not_checked")
    elif state == "unpaired":
        # The owner paired on 2026-11-02 (Codex BAW-PAIR-20261102); unpaired now means the pairing
        # was lost, not deferred.
        blockers.append("wallet_pairing_required")
    elif state == "unavailable":
        blockers.append("wallet_connection_unavailable")
    else:
        if not dev_active:
            blockers.append("developer_mode_required")
        if not usable_chain:
            blockers.append("runtime_chain_or_wallet_unverified")
    if raw.get("execution_enabled") is not False:
        blockers.append("execution_flag_not_false_reported")  # the worker must say False explicitly
    blockers.append("execution_adapter_not_armed")
    stage = ("worker_version_unrecognised" if not recognised
             else {"not_checked": "connection_not_checked", "unpaired": "wallet_unpaired",
                   "unavailable": "connection_unavailable"}.get(state)
             or ("connected_unverified" if not (dev_active and usable_chain) else "preview_capable"))
    return {
        **out, "available": True, "stage": stage, "worker": {"service": service, "recognised": recognised},
        "execution_enabled_reported": raw.get("execution_enabled"),
        "can_preview": stage == "preview_capable",
        "connection": {"state": state, "connected": connected, "reason": conn.get("reason"),
                       "checked_ms": raw.get("connection_checked_ms") or None, "chains": chains,
                       "dev_mode": {"enabled": mode.get("enabled"), "expires_ms": expires, "active": dev_active},
                       "wallet_limits_usd": {"daily_limit": settings.get("dailyLimit"),
                                             "quota_left": settings.get("quotaLeft"),
                                             "dev_mode_daily_limit": mode.get("dailyLimit")}},
        "envelope": {"per_mint_usd": _usd(gas.get("per_mint_micro_usd")), "daily_usd": _usd(gas.get("daily_micro_usd")),
                     "spent_usd": _usd(gas.get("spent_micro_usd")), "pending_usd": _usd(gas.get("pending_micro_usd")),
                     "remaining_usd": _usd(gas.get("remaining_micro_usd")), "halted": gas.get("halted"),
                     "day_wib": gas.get("day_wib"), "scope": gas.get("scope"),
                     "account_spend_reconciled": gas.get("account_spend_reconciled")},
        "candidates": {str(k): v for k, v in (raw.get("candidates") or {}).items() if type(v) is int},
        # None on a worker that predates last_preview (2026-11-02), and when nothing was previewed
        "last_preview": _last_preview(raw.get("last_preview")),
        "blockers": blockers,
    }


def status(config_path: Path | str | None = None, *, max_age_s: float = CACHE_S,
           clock: Callable[[], float] = time.time,
           fetcher: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """The cached readiness view (at most one worker read per ``max_age_s``)."""
    now = clock()
    key = str(config_path or CLIENT_CONFIG)
    hit = _cache.get(key)
    if hit and now - hit[0] < max_age_s:
        return {**hit[1], "cached_age_s": round(now - hit[0], 1)}
    raw = (fetcher or fetch)(config_path)
    out = {**view(raw, now_ms=int(now * 1000)), "read_ms": int(now * 1000)}
    _cache[key] = (now, out)
    return {**out, "cached_age_s": 0.0}
