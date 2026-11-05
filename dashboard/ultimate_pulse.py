"""Evidence-labelled market rankings for the read-only unified workspace.

TERMINAL-PULSE-20261103. No providers, database, execution or grading imports.
The background producer owns collection; this module validates its bounded
snapshot and preserves discovery, assessment and PnL-window distinctions.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[1]
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500
SNAPSHOT_TTL_MS = 15 * 60_000
DAY_MS = 86_400_000
CHAINS = {
    "sol": "Solana", "eth": "Ethereum", "base": "Base", "bsc": "BNB Chain",
    "arbitrum": "Arbitrum", "robinhood": "Robinhood", "polygon": "Polygon", "avax": "Avalanche",
}
ALIASES = {"solana": "sol", "ethereum": "eth", "bnb chain": "bsc", "bnb": "bsc", "bsc": "bsc"}


def _text(value: Any, limit: int = 180) -> str:
    return str(value).replace("\x00", "")[:limit] if isinstance(value, (str, int, float)) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def _integer(value: Any) -> int | None:
    result = _number(value)
    return int(result) if result is not None and result >= 0 and result == int(result) else None


def _nonnegative(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _clock(value: Any, now_ms: int) -> int | None:
    result = _integer(value)
    return result if result is not None and 0 < result <= now_ms else None


def _chain(value: Any) -> str | None:
    name = _text(value, 30).lower()
    name = ALIASES.get(name, name)
    return name if name in CHAINS else None


def _address(chain: str | None, value: Any) -> str | None:
    address = _text(value, 100)
    if chain == "sol":
        return address if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address) else None
    return address if re.fullmatch(r"0x[0-9a-fA-F]{40}", address) else None


def _status(clock: int | None, now_ms: int, ttl: int, snapshot_fresh: bool) -> str:
    if clock is None:
        return "unknown"
    return "fresh" if snapshot_fresh and now_ms - clock <= ttl else "stale"


def _rows(value: Any) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("rows")
    return [row for row in value[:MAX_ROWS] if isinstance(row, dict)] if isinstance(value, list) else []


def _reject_constant(_value: str):
    raise ValueError("non-finite JSON number")


def _wallets(value: Any, now_ms: int, fresh: bool) -> list[dict]:
    local_day = datetime.fromtimestamp(now_ms / 1000, UTC) + timedelta(hours=7)
    day_start = int((local_day.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=7)).timestamp() * 1000)
    seen, result = set(), []
    for row in sorted(_rows(value), key=lambda item: _clock(item.get("scored_at_ms"), now_ms) or 0, reverse=True):
        chain = _chain(row.get("chain"))
        address = _address(chain, row.get("address"))
        if chain is None or address is None or (chain, address) in seen:
            continue
        seen.add((chain, address))
        assessed = _clock(row.get("scored_at_ms"), now_ms)
        discovered = _clock(row.get("first_seen_ms"), now_ms)
        closed = _integer(row.get("closed_trades"))
        pnl = _number(row.get("realized_pnl_usd"))
        rate = _number(row.get("win_rate"))
        if rate is not None and not 0 <= rate <= 1:
            rate = None
        blockers = row.get("blockers", row.get("blockers_json", []))
        if isinstance(blockers, str) and len(blockers) <= 16_384:
            try:
                blockers = json.loads(blockers)
            except ValueError:
                blockers = []
        flags = [_text(flag, 100) for flag in blockers[:6]] if isinstance(blockers, list) else []
        if closed == 0 and pnl not in (None, 0):
            pnl = None
            flags.append("PnL conflicts with zero recorded closes")
        explorer = {"eth": "https://etherscan.io/address/", "base": "https://basescan.org/address/",
                    "arbitrum": "https://arbiscan.io/address/"}.get(chain)
        wallet_url = f"/w/{chain}/{address}" if chain in ("sol", "bsc", "robinhood") else explorer + address if explorer else None
        result.append({
            "id": f"{chain}:{address}", "chain": chain, "chain_name": CHAINS[chain], "address": address,
            "name": _text(row.get("name"), 70) or f"{address[:7]}…{address[-5:]}",
            "short_address": f"{address[:7]}…{address[-5:]}",
            "url": wallet_url,
            "first_seen_ms": discovered, "scored_at_ms": assessed,
            "found_today": discovered is not None and discovered >= day_start,
            "realized_pnl_usd": pnl, "win_rate_pct": rate * 100 if rate is not None else None,
            "closed_trades": closed, "median_hold_s": _number(row.get("median_hold_s")),
            "grade": _text(row.get("grade"), 20), "model_version": _text(row.get("model_version"), 100),
            "pnl_window": _text(row.get("pnl_window"), 80) or "Stored history · window unspecified",
            "basis": _text(row.get("basis", row.get("source")), 120) or "Stored wallet assessment",
            "flags": flags, "status": _status(assessed, now_ms, DAY_MS, fresh),
            "copy_profitability": "unproven",
        })
    return result


def _tokens(value: Any, now_ms: int, fresh: bool) -> list[dict]:
    result, seen = [], set()
    for row in sorted(_rows(value), key=lambda item: _clock(item.get("observed_ms", item.get("fetched_ms")), now_ms) or 0, reverse=True):
        chain = _chain(row.get("chain"))
        address = _address(chain, row.get("address", row.get("token_address")))
        if chain is None or address is None or (chain, address) in seen:
            continue
        seen.add((chain, address))
        observed = _clock(row.get("observed_ms", row.get("fetched_ms")), now_ms)
        volume1 = _number(row.get("volume_1h_usd"))
        volume24 = _number(row.get("volume_24h_usd"))
        raw_volume = _number(row.get("volume_usd"))
        volume_window = row.get("volume_window")
        if raw_volume is not None and volume_window == "1h" and volume1 is None:
            volume1 = raw_volume
        if raw_volume is not None and volume_window == "24h" and volume24 is None:
            volume24 = raw_volume
        result.append({
            "id": f"{chain}:{address}", "chain": chain, "chain_name": CHAINS[chain], "address": address,
            "symbol": _text(row.get("symbol"), 25) or address[:7], "name": _text(row.get("name"), 80),
            "url": "/terminal?" + urlencode({"t": f"{chain}:{address}"}),
            "source_rank": _integer(row.get("rank", row.get("source_rank"))) or None,
            "trend_window": _text(row.get("trend_window"), 20) or "unknown",
            "volume_1h_usd": volume1 if volume1 is not None and volume1 >= 0 else None,
            "volume_24h_usd": volume24 if volume24 is not None and volume24 >= 0 else None,
            "reported_volume_usd": raw_volume if raw_volume is not None and raw_volume >= 0 else None,
            "volume_window": volume_window if volume_window in ("1h", "24h") else "unknown",
            "market_cap_usd": _nonnegative(row.get("market_cap_usd")), "liquidity_usd": _nonnegative(row.get("liquidity_usd")),
            "observed_ms": observed, "status": _status(observed, now_ms, SNAPSHOT_TTL_MS, fresh),
            "source": _text(row.get("source"), 60) or "GMGN trending",
        })
    return result


def _chains(value: Any, now_ms: int, fresh: bool) -> list[dict]:
    result, seen = [], set()
    for row in _rows(value):
        chain = _chain(row.get("chain"))
        if chain is None or chain in seen:
            continue
        seen.add(chain)
        volume = _number(row.get("volume_24h_usd", row.get("total24h")))
        observed = _clock(row.get("observed_ms", row.get("fetched_ms")), now_ms)
        result.append({
            "chain": chain, "name": CHAINS[chain],
            "volume_24h_usd": volume if volume is not None and volume >= 0 else None,
            "change_24h_pct": _number(row.get("change_24h_pct", row.get("change_1d"))),
            "observed_ms": observed, "status": _status(observed, now_ms, 2 * 3_600_000, fresh),
            "source": _text(row.get("source"), 70) or "DefiLlama reported DEX volume",
            "source_url": "https://defillama.com/dexs/chain/" + {"sol": "solana", "eth": "ethereum", "bsc": "bsc"}.get(chain, chain),
        })
    return result


def build_pulse(*, path: Path | None = None, now_ms: int | None = None) -> dict:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    result = {"status": "unavailable", "observed_ms": None, "tokens": [], "wallets": [], "chains": [],
              "coverage": {}, "source_health": [], "reason": "Awaiting the market and wallet snapshot."}
    target = path or Path(os.environ.get("KAIBA_TERMINAL_PULSE_PATH") or REPO / "data" / "terminal-pulse" / "latest.json")
    try:
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or target.is_symlink() or metadata.st_size > MAX_BYTES:
            raise ValueError("invalid snapshot")
        with target.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("snapshot limit")
        document = json.loads(raw, parse_constant=_reject_constant)
        if not isinstance(document, dict) or document.get("schema_version", document.get("schema")) != 1:
            raise ValueError("snapshot schema")
        observed = _clock(document.get("observed_ms"), now)
        fresh = observed is not None and now - observed <= SNAPSHOT_TTL_MS
        result.update(observed_ms=observed, status="available" if fresh else "stale" if observed else "unknown",
                      reason=None if fresh else "Snapshot is stale or its observation time is unknown.",
                      wallets=_wallets(document.get("wallets"), now, fresh), tokens=_tokens(document.get("tokens"), now, fresh),
                      chains=_chains(document.get("chains"), now, fresh))
        if fresh and document.get("status") in ("partial", "unavailable", "error", "deferred"):
            result.update(status="partial", reason="Some ranking sources are unavailable; check source times and coverage.")
        coverage = document.get("coverage")
        if isinstance(coverage, dict):
            result["coverage"] = {key: value for key, value in coverage.items() if isinstance(key, str)
                                  and isinstance(value, (str, bool, int, float, type(None))) and len(str(value)) <= 300}
            wallet_coverage = coverage.get("wallets")
            if isinstance(wallet_coverage, dict):
                sample = _integer(wallet_coverage.get("unique_wallets_in_sample"))
                revisions = _integer(wallet_coverage.get("history_rows_read"))
                if sample is not None and revisions is not None:
                    result["coverage"]["wallet_scope"] = f"{sample:,} wallets in the latest {revisions:,} grading revisions"
                for name in ("exported", "found_today_wib", "with_score_row"):
                    result["coverage"]["wallets_" + name] = _integer(wallet_coverage.get(name))
        health = document.get("source_health")
        if isinstance(health, dict):
            health = [{**value, "key": key} for key, value in list(health.items())[:15] if isinstance(value, dict)]
        for row in _rows(health)[:15]:
            clock = _clock(row.get("fetched_ms", row.get("observed_ms")), now)
            raw_status = row.get("status") if isinstance(row.get("status"), str) else "unknown"
            source_status = {"ok": "fresh", "invalid": "error", "cooldown": "deferred"}.get(raw_status, raw_status)
            if source_status not in ("fresh", "stale", "partial", "unknown", "unavailable", "error", "missing", "deferred", "available"):
                source_status = "unknown"
            if source_status in ("fresh", "available") and clock is None:
                source_status = "unknown"
            elif source_status in ("fresh", "available") and not fresh:
                source_status = "stale"
            reason = _text(row.get("reason", row.get("error")), 150)
            if row.get("status") == "cooldown":
                reason = "Provider rate-limit cooldown; saved observations retain their original source time."
            result["source_health"].append({"key": _text(row.get("key"), 60), "status": source_status,
                                             "fetched_at": clock, "reason": reason})
        if fresh and any(row["status"] != "fresh" for row in result["source_health"]):
            result.update(status="partial", reason="Some ranking sources are unavailable or stale; inspect source times and coverage.")
        if not any(result[key] for key in ("tokens", "wallets", "chains")) and fresh:
            result.update(status="partial", reason="Snapshot received; ranking data is not available yet.")
    except (OSError, ValueError, RecursionError, OverflowError):
        pass
    return result
