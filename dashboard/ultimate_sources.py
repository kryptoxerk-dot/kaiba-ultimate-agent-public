"""Bounded local ledger views for the unified dashboard (Codex, 2026-11-03).

This reader never refreshes a provider or changes execution state. Each query owns
and closes a mode=ro connection, checks the WAL, and has a three-second deadline.
Unknown or incomplete money stays unknown; observations are not trade advice.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

WAL_LIMIT_BYTES = 1_500_000_000
QUERY_SECONDS = 3.0
POSITION_LIMIT = 40
DAY_LIMIT = 2000
ALERT_LIMIT = 20
TOKEN_LIMIT = 12
NATIVE = {"sol": ("SOL", 9), "bsc": ("BNB", 18), "robinhood": ("ETH", 18),
          "eth": ("ETH", 18), "base": ("ETH", 18)}
OPPORTUNITY_KINDS = ("airdrop", "nft_mint", "listing", "chain", "launchpad")


def _db_path() -> Path | None:
    try:
        from kaiba.core.config import get_settings

        return Path(get_settings().db_path)
    except Exception:
        return None


def _query(path: Path | None, sql: str, params: tuple = ()) -> tuple[list[dict], str | None]:
    """Return an explicit reason instead of equating an error with an empty book."""
    try:
        if path is None or not path.is_file():
            return [], "Local ledger unavailable"
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size > WAL_LIMIT_BYTES:
            return [], "Ledger read deferred: WAL exceeds 1.5 GB"
        deadline = time.monotonic() + QUERY_SECONDS
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                               timeout=0.15, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("PRAGMA query_only=ON")
            return [dict(row) for row in conn.execute(sql, params).fetchall()], None
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        # Do not expose filesystem locations, SQL payloads, or database error text.
        return [], "Local ledger query unavailable or deadline exceeded"


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        n = float(raw)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _integer(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _native(raw: Any, chain: str) -> float | None:
    n = _integer(raw)
    decimals = NATIVE.get(chain)
    if n is None or decimals is None:
        return None
    try:
        return _number(n / 10 ** decimals[1])
    except OverflowError:
        return None


def _timestamp(raw: Any, now_ms: int, *, allow_future: bool = False) -> int | None:
    """SQLite's dynamic typing must not turn bad clocks into recent observations."""
    value = _integer(raw)
    if value is None or not 0 < value <= 253_402_300_799_000:
        return None
    if not allow_future and value > now_ms:
        return None
    return value


def _first_timestamp(row: dict, fields: tuple[str, ...], now_ms: int) -> int | None:
    # A malformed newer clock remains unknown; an older fallback cannot freshen it.
    return next((_timestamp(row[key], now_ms) for key in fields if row.get(key) is not None), None)


def _url(raw: Any) -> str | None:
    if not isinstance(raw, str) or len(raw) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    try:
        p = urlsplit(raw)
        if p.scheme in ("https", "http") and p.hostname and not p.username and not p.password:
            return urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        return None
    except ValueError:
        return None


def _payload(raw: Any) -> dict:
    if not isinstance(raw, str) or len(raw) > 256_000:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _mode() -> str:
    try:
        from dashboard import data

        return data.current_mode()
    except Exception:
        return "unknown"


def build_kaiba_snapshot() -> dict[str, Any]:
    """Read the existing portfolio, wallet alerts and research opportunity registry.

    Day totals are exact only when the bounded query returned fewer than its cap.
    Paper and live PnL remain separate, in native units; no USD mark is invented.
    """
    now = int(time.time() * 1000)
    path = _db_path()
    health: list[dict] = []

    def read(key: str, sql: str, params: tuple = ()) -> tuple[list[dict], bool]:
        rows, reason = _query(path, sql, params)
        health.append({"key": key, "status": "unavailable" if reason else "available", "reason": reason})
        return rows, reason is None

    positions, positions_ok = read(
        "positions",
        "SELECT p.position_id, p.chain, p.token, p.lane, p.mode, p.opened_ms, "
        "p.cost_native, p.protected, t.symbol FROM positions p INDEXED BY idx_positions_open "
        "LEFT JOIN tokens t ON t.chain=p.chain AND t.address=p.token "
        "WHERE p.closed_ms IS NULL LIMIT ?", (POSITION_LIMIT + 1,),
    )
    truncated_positions = len(positions) > POSITION_LIMIT
    if truncated_positions:
        health[-1].update(status="partial", reason="Open-position display limit reached; totals unknown")
    visible_positions = positions[:POSITION_LIMIT]
    open_rows = [{"id": r["position_id"], "chain": r["chain"], "token": r["token"],
                  "symbol": r["symbol"], "lane": r["lane"], "mode": r["mode"],
                  "opened_ms": _timestamp(r["opened_ms"], now), "cost_native": _native(r["cost_native"], r["chain"]),
                  "native": NATIVE.get(r["chain"], ("", 0))[0], "protected": bool(r["protected"]),
                  "mark_price_usd": None} for r in visible_positions]
    day = int(datetime.fromtimestamp(now / 1000, UTC).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    closed, closed_ok = read(
        "realized_today",
        "SELECT chain, mode, realized_native, closed_ms FROM positions INDEXED BY idx_positions_open "
        "WHERE closed_ms >= ? LIMIT ?", (day, DAY_LIMIT + 1),
    )
    clock_ok = all(_timestamp(row["closed_ms"], now) is not None for row in closed)
    day_complete = closed_ok and len(closed) <= DAY_LIMIT and clock_ok
    if closed_ok and not day_complete:
        reason = "Daily ledger observation time invalid; totals unknown" if not clock_ok \
            else "Daily ledger read limit reached; totals unknown"
        health[-1].update(status="partial", reason=reason)
    totals: dict[tuple[str, str], dict] = {}
    if day_complete:
        for r in closed:
            key = (r["chain"], r["mode"])
            bucket = totals.setdefault(key, {"chain": key[0], "mode": key[1], "trades": 0,
                                            "symbol": NATIVE.get(key[0], ("", 0))[0], "base_units": 0,
                                            "complete": True})
            bucket["trades"] += 1
            value = _integer(r["realized_native"])
            if value is None:
                bucket["complete"] = False
            else:
                bucket["base_units"] += value
    day_pnl = [{"chain": b["chain"], "mode": b["mode"], "trades": b["trades"], "symbol": b["symbol"],
                "pnl_native": _native(b["base_units"], b["chain"]) if b["complete"] else None}
               for b in totals.values()]

    alerts, alerts_ok = read(
        "wallet_alerts",
        "SELECT signal_key, subject, title, url, chain, first_seen_ms, payload_json "
        "FROM alpha_signals INDEXED BY idx_alpha_signals_source "
        "WHERE source = ? ORDER BY first_seen_ms DESC LIMIT ?", ("kaiba:wallet_track", ALERT_LIMIT),
    )
    wallet_alerts = []
    for r in alerts:
        p = _payload(r["payload_json"])
        tiers = p.get("tiers") if isinstance(p.get("tiers"), dict) else {}
        safe_tiers = {t: _integer(tiers.get(t)) for t in ("S", "A", "B") if _integer(tiers.get(t)) is not None}
        level = _integer(p.get("level"))
        observed = _timestamp(r["first_seen_ms"], now)
        wallet_alerts.append({
            "id": r["signal_key"], "title": str(r["title"] or "Tracked wallet confluence")[:180],
            "detail": "Tracked wallets bought together; copying profitability is unproven.",
            "chain": r["chain"], "token": r["subject"], "symbol": str(p.get("symbol") or "")[:30],
            "observed_ms": observed, "amount_usd": _number(p.get("usd")),
            "wallet_count": level, "tiers": safe_tiers, "level": level,
            "source": "Kaiba wallet tracker", "url": _url(r["url"]),
            "stale": observed is None or now - observed > 3600_000,
            "observation_status": "unknown" if observed is None else "stale" if now - observed > 3600_000 else "fresh",
        })

    opportunities = []
    category_coverage = []
    for kind in OPPORTUNITY_KINDS:
        rows, ok = read(
            "opportunities:" + kind,
            "SELECT opportunity_id, kind, name, chain, url, status, deadline_ms, created_ms, updated_ms, "
            "last_seen_ms, source "
            "FROM opportunities INDEXED BY idx_opps WHERE kind=? AND status='open' "
            "ORDER BY ev_score DESC LIMIT 8", (kind,),
        )
        category_coverage.append({"kind": kind, "status": "available" if ok else "unavailable", "visible": len(rows)})
        for r in rows:
            observed = _first_timestamp(r, ("last_seen_ms", "updated_ms", "created_ms"), now)
            deadline = _timestamp(r["deadline_ms"], now, allow_future=True)
            opportunities.append({
                "id": r["opportunity_id"], "kind": r["kind"], "title": str(r["name"])[:180],
                "chain": r["chain"], "url": _url(r["url"]), "observed_ms": observed,
                "deadline_ms": deadline, "status": r["status"],
                "source": str(r["source"] or "Kaiba opportunity registry")[:100],
                "verification": "Research lead; verify official terms", "stale": bool(
                    observed is None or now - observed > 86_400_000
                    or (deadline is not None and deadline < now)),
                "observation_status": "unknown" if observed is None else "stale" if now - observed > 86_400_000 else "fresh",
            })

    # Feed sightings are observations, not validated memecoins or newly deployed
    # contracts. Keep that distinct from the triage system's launch candidates.
    feed_rows, feed_ok = read(
        "opportunities:token",
        "SELECT feed, chain, token_address, symbol, first_seen_ms, first_source_ts_ms "
        "FROM alpha_feed_first_seen INDEXED BY idx_alpha_feed_first_seen_ts "
        "WHERE first_seen_ms >= ? ORDER BY first_seen_ms DESC LIMIT ?", (now - 86_400_000, TOKEN_LIMIT * 3),
    )
    launch_rows, launch_ok = read(
        "opportunities:new_launch",
        "SELECT d.id, d.ts_ms, d.chain, d.token, d.launchpad, d.source, d.event_ms, t.symbol "
        "FROM triage_decisions d INDEXED BY idx_triage_verdict "
        "LEFT JOIN tokens t ON t.chain=d.chain AND t.address=d.token "
        "WHERE d.verdict='promote' AND d.ts_ms >= ? ORDER BY d.ts_ms DESC LIMIT ?",
        (now - 86_400_000, TOKEN_LIMIT * 3),
    )
    for kind, rows, available in (("token", feed_rows, feed_ok), ("new_launch", launch_rows, launch_ok)):
        seen: set[tuple] = set()
        for row in rows:
            chain = row["chain"]
            token = row.get("token") or row.get("token_address")
            if not chain or not token or (chain, token) in seen:
                continue
            seen.add((chain, token))
            observed = _first_timestamp(row, ("ts_ms", "first_seen_ms"), now)
            source = row.get("source") or row.get("feed") or "Kaiba local feed"
            symbol = str(row.get("symbol") or "")[:30]
            title = symbol or str(token)[:10] + "…"
            opportunities.append({
                "id": f"{kind}:{chain}:{token}", "kind": kind, "title": title,
                "chain": chain, "token": token, "symbol": symbol,
                "url": "/terminal?" + urlencode({"t": f"{chain}:{token}"}),
                "observed_ms": observed,
                "source_event_ms": _first_timestamp(row, ("event_ms", "first_source_ts_ms"), now),
                "deadline_ms": None, "status": "research_only", "source": str(source)[:100],
                "launchpad": row.get("launchpad"),
                "verification": "Launch candidate promoted by triage; not approval to trade" if kind == "new_launch"
                else "Cached token feed sighting; safety and profitability unverified",
                "stale": observed is None or now - observed > 3600_000,
                "observation_status": "unknown" if observed is None else "stale" if now - observed > 3600_000 else "fresh",
            })
            if len(seen) >= TOKEN_LIMIT:
                break
        category_coverage.append({"kind": kind, "status": "available" if available else "unavailable",
                                  "visible": len(seen)})
    complete_book = positions_ok and not truncated_positions
    successes = sum(h["status"] == "available" for h in health)
    status = "available" if successes == len(health) else "partial" if successes else "unavailable"
    reason = next((h["reason"] for h in health if h["reason"]), None)
    if reason and "WAL" in reason and not successes:
        status = "deferred"
    return {
        "captured_at_ms": now, "status": status, "source": "Kaiba local ledger", "reason": reason,
        "mode": _mode(),
        "metrics": {
            "open_positions": len(positions) if complete_book else None,
            "live_positions": sum(r["mode"] == "live" for r in positions) if complete_book else None,
            "paper_positions": sum(r["mode"] != "live" for r in positions) if complete_book else None,
            "protected_positions": sum(bool(r["protected"]) for r in positions) if complete_book else None,
            "realized_today_by_chain": day_pnl,
            "live_closed_today": sum(r["mode"] == "live" for r in closed) if day_complete else None,
            "paper_closed_today": sum(r["mode"] != "live" for r in closed) if day_complete else None,
            "day_complete": day_complete, "positions_complete": complete_book,
        },
        "positions": open_rows, "wallet_alerts": wallet_alerts, "wallet_alerts_available": alerts_ok,
        "opportunities": opportunities, "category_coverage": category_coverage, "health": health,
        "performance": {"basis": "Ledger realized only; not an edge estimate", "profitability": "unproven"},
    }
