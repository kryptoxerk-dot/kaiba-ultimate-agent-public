"""Conservative historical excursion inventory and explicitly authorized repair.

No imports from trading services; no migration, network, or default DB connection.
A retained peak is a *candidate* lower bound, not a timestamped USD observation.
Unknown is None, including an unsupported legacy zero. Existing None/nonzero values
are never overwritten. Consumers must retain the manifest's measurement basis.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from decimal import Decimal, DecimalException, localcontext
from typing import Any

VERSION = 1
METRICS = ("mfe_pct", "mae_pct")
KEYS = {"positions": "position_id", "trades": "trade_id"}
IDENTITY = ("position_id", "chain", "token", "lane", "mode", "opened_ms", "closed_ms")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, args)
    keys = [c[0] for c in cur.description]
    return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]


def _positive(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (DecimalException, TypeError, ValueError):
        return None


def _return(entry: Decimal, price: Decimal) -> float | None:
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            result = float((price / entry - 1) * 100)
        return result if math.isfinite(result) else None
    except (DecimalException, OverflowError):
        return None


def _snapshot(conn: sqlite3.Connection, cutoff_ms: int) -> dict:
    positions = _rows(conn, "SELECT * FROM positions WHERE opened_ms<=? ORDER BY position_id", (cutoff_ms,))
    tables = {r["name"] for r in _rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
    sources = {}
    for p in positions:
        pid = p["position_id"]
        sources[pid] = {
            "marks": _rows(conn, "SELECT * FROM position_marks WHERE position_id=? ORDER BY id", (pid,))
            if "position_marks" in tables else [],
        }
    return {"positions": positions, "sources": sources,
            "trades": _rows(conn, "SELECT * FROM trades WHERE closed_ms<=? ORDER BY trade_id", (cutoff_ms,))}


def _measurements(position: dict, sources: dict) -> tuple[dict, list[dict]]:
    entry = _positive(position["entry_price_usd"])
    returns, rejected = [], []
    for mark in sources["marks"]:
        price = _positive(mark["price_usd"])
        reason = None
        if position["mode"] not in {"shadow", "paper"}:
            reason = "live_mark_without_quote_provenance"
        elif not (position["closed_ms"] is not None
                  and position["opened_ms"] < mark["ts_ms"] <= position["closed_ms"]):
            reason = "outside_position_interval"
        elif entry is None or price is None:
            reason = "invalid_entry_or_price"
        else:
            value = _return(entry, price)
            recorded = mark["return_pct"]
            if (value is None or not isinstance(recorded, (int, float))
                    or not math.isfinite(recorded) or abs(recorded - value) > 0.0000011):
                reason = "mark_entry_basis_unverified"
            else:
                returns.append(value)
        if reason:
            rejected.append({"table": "position_marks", "key": mark["id"], "reason": reason})
    if not returns:
        return {name: {"value": None, "basis": "unavailable"} for name in METRICS}, rejected
    mfe, mae = max(0.0, max(returns)), min(0.0, min(returns))
    return {
        "mfe_pct": {"value": mfe, "basis": "sampled_lower_bound" if mfe else "sampled_zero", "samples": len(returns)},
        "mae_pct": {"value": mae, "basis": "sampled_upper_bound" if mae else "sampled_zero", "samples": len(returns)},
    }, rejected


def inventory(conn: sqlite3.Connection, *, cutoff_ms: int) -> dict:
    """Read a consistent inventory; no writes, even when supplied a writable connection."""
    if type(cutoff_ms) is not int or cutoff_ms <= 0:
        raise ValueError("cutoff_ms must be a positive integer")
    conn.execute("SAVEPOINT excursion_inventory")
    try:
        snapshot = _snapshot(conn, cutoff_ms)
    finally:
        conn.execute("RELEASE excursion_inventory")
    items, changes = [], []
    for position in snapshot["positions"]:
        pid = position["position_id"]
        entry, peak = _positive(position["entry_price_usd"]), _positive(position["peak_price_usd"])
        candidate = _return(entry, peak) if entry is not None and peak is not None and peak >= entry else None
        measurements, rejected = _measurements(position, snapshot["sources"][pid])
        item = {
            "position_id": pid,
            "identity": {key: position[key] for key in IDENTITY},
            "peak_candidate": {
                "mfe_pct": candidate,
                "basis": "unverified_retained_peak" if candidate is not None else "invalid_or_missing_peak_entry",
                "entry_price_usd": position["entry_price_usd"],
                "peak_price_usd": position["peak_price_usd"],
                "limitation": "No peak observation timestamp, quote identity, or interval proof; never auto-applied.",
            },
            "measurements": measurements,
            "rejected_evidence": rejected,
            "blockers": [],
        }
        trades = [t for t in snapshot["trades"] if t["position_id"] == pid]
        if any(any(trade[key] != position[key] for key in IDENTITY) for trade in trades):
            item["blockers"].append("trade_position_identity_mismatch")
        if position["closed_ms"] is None or position["closed_ms"] > cutoff_ms:
            item["blockers"].append("open_or_after_cutoff")
        if not item["blockers"]:
            for table, rows in (("positions", [position]), ("trades", trades)):
                for row in rows:
                    for metric in METRICS:
                        measurement = measurements[metric]
                        if row[metric] == 0 and measurement["value"] != row[metric]:
                            changes.append({"table": table, "key": row[KEYS[table]], "position_id": pid,
                                            "column": metric, "old": row[metric], "new": measurement["value"],
                                            "basis": measurement["basis"]})
        items.append(item)
    manifest = {"version": VERSION, "cutoff_ms": cutoff_ms, "snapshot": snapshot,
                "items": items, "changes": changes,
                "summary": {"positions": len(snapshot["positions"]), "trades": len(snapshot["trades"]),
                            "changed_fields": len(changes)}}
    manifest["manifest_id"] = _digest(manifest)
    return manifest


class DriftError(RuntimeError):
    """The reviewed evidence or a protected ledger value has changed; nothing applied."""


def _validate_manifest(manifest: dict) -> None:
    body = {key: value for key, value in manifest.items() if key != "manifest_id"}
    if manifest.get("version") != VERSION or manifest.get("manifest_id") != _digest(body):
        raise ValueError("invalid manifest version or digest")


def _expected_snapshot(manifest: dict) -> dict:
    snapshot = json.loads(_json(manifest["snapshot"]))
    for change in manifest["changes"]:
        if change["table"] not in KEYS or change["column"] not in METRICS or change["old"] != 0:
            raise ValueError("manifest attempts an out-of-scope write")
        row = next(r for r in snapshot[change["table"]] if r[KEYS[change["table"]]] == change["key"])
        row[change["column"]] = change["new"]
    return snapshot


def _cas_changes(conn: sqlite3.Connection, manifest: dict, *, reverse: bool = False) -> None:
    expected = _expected_snapshot(manifest) if reverse else json.loads(_json(manifest["snapshot"]))
    for change in manifest["changes"]:
        table, column = change["table"], change["column"]
        row = next(r for r in expected[table] if r[KEYS[table]] == change["key"])
        value = change["old"] if reverse else change["new"]
        # Full-row CAS protects the identity, interval, accounting, stop and original None values.
        where = " AND ".join('"' + key.replace('"', '""') + '" IS ?' for key in row)
        cur = conn.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (value, *row.values()))
        if cur.rowcount != 1:
            raise DriftError(f"CAS failed for {table}:{change['key']}:{column}")
        row[column] = value


def _journal(conn: sqlite3.Connection, manifest_id: str) -> dict | None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='excursion_repair_runs'").fetchone()
    if not exists:
        return None
    rows = _rows(conn, "SELECT * FROM excursion_repair_runs WHERE manifest_id=?", (manifest_id,))
    return rows[0] if rows else None


def apply_manifest(conn: sqlite3.Connection, manifest: dict, *, authorize: bool = False) -> dict:
    """Apply a reviewed inventory atomically. Never called by the dry-run CLI.

    The caller must explicitly authorize writes and supply an idle writable connection.
    BEGIN IMMEDIATE + full-row CAS + evidence readback precede one atomic commit.
    A durable copy of the entire manifest retains old values and provenance. Digest
    checks detect corruption, not authorization; the manifest is independently rebuilt.
    """
    if authorize is not True:
        raise PermissionError("explicit authorize=True is required")
    if conn.in_transaction:
        raise ValueError("repair requires an idle connection; will not commit caller work")
    _validate_manifest(manifest)
    conn.execute("BEGIN IMMEDIATE")
    try:
        run = _journal(conn, manifest["manifest_id"])
        current = inventory(conn, cutoff_ms=manifest["cutoff_ms"])
        expected = _expected_snapshot(manifest)
        if run:
            if run["status"] != "applied" or json.loads(run["manifest_json"]) != manifest:
                raise DriftError("manifest journal does not describe an active matching repair")
            if current["snapshot"] != expected:
                raise DriftError("previously repaired rows or evidence drifted")
            result = {"status": "already_applied", "changed_fields": 0}
        else:
            if current != manifest:
                raise DriftError("inventory changed; regenerate and review the dry run")
            if not manifest["changes"]:
                result = {"status": "no_changes", "changed_fields": 0}
            else:
                _cas_changes(conn, manifest)
                if _snapshot(conn, manifest["cutoff_ms"]) != expected:
                    raise DriftError("post-write verification failed")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS excursion_repair_runs ("
                    "manifest_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN ('applied','rolled_back')))"
                )
                conn.execute("INSERT INTO excursion_repair_runs VALUES (?,?,'applied')",
                             (manifest["manifest_id"], _json(manifest)))
                result = {"status": "applied", "changed_fields": len(manifest["changes"])}
        conn.commit()
        return {**result, "manifest_id": manifest["manifest_id"]}
    except BaseException:
        conn.rollback()
        raise
