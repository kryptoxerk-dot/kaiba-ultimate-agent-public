"""Read-only health for the shared social research inbox (Codex, 2026-10-29).

The existing change-triggered monitor must distinguish no data from a healthy quiet queue.
No credentials, providers, signing or financial database access. Never reclaims a lock.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

MAX_STORE_BYTES = 256 * 1024 * 1024
MAX_WAL_BYTES = 1_500_000_000
QUERY_SECONDS = 2.0
PERSISTENT_LOCK_SECONDS = 60


def delivery_health(latest_ms: Any, *, now_ms: int, enabled: bool, max_age_s: int) -> dict[str, Any]:
    """Stable condition, with the evidence timestamp but no ever-changing age/clock field."""
    if not enabled:
        return {"status": "disabled", "latest_receipt_ms": None}
    if latest_ms is None:
        return {"status": "no_retained_receipt", "latest_receipt_ms": None,
                "meaning": "No retained post proves delivery; this does not prove upstream silence."}
    if type(latest_ms) is not int or latest_ms < 0 or latest_ms > now_ms + 2000:
        return {"status": "receipt_clock_invalid", "latest_receipt_ms": None}
    return {"status": "stale" if now_ms - latest_ms > max_age_s * 1000 else "recent",
            "latest_receipt_ms": latest_ms}


def budget_health(ledger: Path, *, now_ms: int) -> dict[str, Any]:
    """Inspect only counters/lock metadata. No owner inference, mutation or key read."""
    lock = ledger.with_name(ledger.name + ".lock")
    try:
        if ledger.is_symlink() or lock.is_symlink():
            return {"status": "path_unverified"}
        if lock.exists():
            age_ms = now_ms - int(lock.stat().st_mtime * 1000)
            if age_ms < -2000:
                return {"status": "lock_clock_invalid"}
            if age_ms >= PERSISTENT_LOCK_SECONDS * 1000:
                return {"status": "persistent_lock_owner_unverified",
                        "meaning": "Budget reservation is blocked. Age alone does not authorize deleting the lock."}
        if not ledger.exists():
            return {"status": "not_initialized"}
        with ledger.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            return {"status": "ledger_oversized"}
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1 or any(
            type(value.get(k)) is not int or value[k] < 0 for k in ("daily_credits", "lifetime_credits")
        ) or not isinstance(value.get("day"), str):
            return {"status": "ledger_invalid"}
        # Match the existing reservation helper's caps; do not change/reset the ledger.
        day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
        if value["lifetime_credits"] >= 2_000_000:
            return {"status": "lifetime_limit_reached"}
        if value["day"] == day and value["daily_credits"] >= 50_000:
            return {"status": "daily_limit_reached"}
        return {"status": "within_limits"}
    except (OSError, ValueError, RecursionError):
        return {"status": "ledger_unavailable"}


def monitor_snapshot(database: Path, *, enabled: bool, max_age_s: int = 86400,
                     budget: Path | None = None, now_ms: int | None = None) -> dict[str, Any]:
    """Pending IDs and delivery state, bounded RO reads; every query closes its connection.

    Result deliberately omits timestamps of polling, age, counts and read durations. A
    repeated fault should not trigger another model run solely because the clock advanced.
    Pending items retain the same order, limit and identity as the original monitor.
    """
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if type(now) is not int or now < 0 or type(max_age_s) is not int or not 1 <= max_age_s <= 604800:
        raise ValueError("invalid_health_clock_or_age")
    result: dict[str, Any] = {"pending": [], "delivery": {"status": "unavailable"},
                              "financial_execution": False}
    if budget is not None:
        result["research_budget"] = budget_health(budget, now_ms=now)
    if not enabled:
        result["delivery"] = delivery_health(None, now_ms=now, enabled=False, max_age_s=max_age_s)
        return result

    def read(sql: str) -> list[tuple]:
        wal = Path(str(database) + "-wal")
        if database.is_symlink() or wal.is_symlink():
            raise ValueError("store_path_unverified")
        if database.stat().st_size > MAX_STORE_BYTES:
            raise ValueError("store_size_exceeded")
        if wal.exists() and wal.stat().st_size > MAX_WAL_BYTES:
            raise ValueError("wal_budget_deferred")
        start = time.monotonic()
        conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() - start > QUERY_SECONDS), 1000)
            rows = conn.execute(sql).fetchall()
            if time.monotonic() - start > QUERY_SECONDS:
                raise ValueError("read_deadline")
            return rows
        finally:
            conn.close()

    try:
        if not database.exists():
            result["delivery"] = {"status": "store_missing"}
            return result
        latest = read("SELECT MAX(handled_ms) FROM posts")[0][0]
        pending = read(
            "SELECT s.tweet_id,s.lane FROM signals s LEFT JOIN reviews r "
            "ON r.tweet_id=s.tweet_id AND r.lane=s.lane WHERE r.reviewed_ms IS NULL "
            "ORDER BY s.lane,s.tweet_id LIMIT 50"
        )
        result["pending"] = [{"tweet_id": tid, "lane": lane} for tid, lane in pending]
        result["delivery"] = delivery_health(latest, now_ms=now, enabled=True, max_age_s=max_age_s)
    except sqlite3.Error:
        result["delivery"] = {"status": "store_read_unavailable"}
    except OSError:
        result["delivery"] = {"status": "store_io_unavailable"}
    except ValueError as exc:
        result["delivery"] = {"status": str(exc)}
    return result


def monitor_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep health transitions stable while retaining original pending-ID change detection."""
    result = {**snapshot, "delivery": dict(snapshot["delivery"])}
    # In a healthy stream a new receipt alone is not a new candidate: avoid model churn.
    result["delivery"].pop("latest_receipt_ms", None)
    return result
