"""Bounded, read-only canonical evidence packets; never economic certification.

Codex AGENT-CAPTURE-20261015 owns this additive module. The caller owns selection,
durable retry and rotating open inventory. Every query uses the supplied guarded
reader; this module opens no connection, writes no ledger, and calls no provider.

Stored fee/reconciliation rows are observations, not isolated economic cashflows.
No wallet identity, zero opening inventory, absent recoveries, custody quantity,
or executable liquidation quote is inferred. Raw capture completeness and economic
completeness are deliberately separate. Monetary TEXT stays TEXT.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from kaiba.learning.autolearn_io import DAY_MS, GuardedReader, SourceDeferred

VERSION = "economic-capture/1"
MAX_POSITIONS = 30
HISTORY_MS = 13 * DAY_MS
TEXT_LIMIT = 4096
MAX_PACKET_BYTES = 64 * 1024
COMMAND_SECONDS = 15.0
COMMAND_QUERIES = 128
ORDER_LIMIT = 64
EVENT_LIMIT = 128
RECONCILIATION_LIMIT = 128
TRADE_LIMIT = 8
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_ECONOMIC_COVERAGE = ("orders_complete", "attempts_complete", "recoveries_complete", "inventory_complete")
_RAW_SECTIONS = ("position_order_links", "referenced_orders", "orders", "trades", "fill_costs",
                 "fill_prices", "fill_reconciliations", "order_events", "watchdog_state",
                 "exit_timing_events", "foreign_position_links", "attempts")
_TIMESTAMPS = {
    "positions": ("opened_ms", "closed_ms"), "position_orders": ("ts_ms",),
    "orders": ("created_ms", "updated_ms"), "trades": ("opened_ms", "closed_ms"),
    "fill_costs": ("computed_ms", "block_time_ms"),
    "fill_prices": ("computed_ms", "fill_ts_ms", "native_sample_ts_ms"),
    "fill_reconciliations": ("checked_ms", "block_time_ms"), "order_events": ("ts_ms",),
    "watchdog_state": ("updated_ms",), "events": ("ts_ms",),
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _clock(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _same(left: Any, right: Any, chain: str) -> bool:
    return (isinstance(left, str) and isinstance(right, str)
            and (left == right if chain == "sol" else left.lower() == right.lower()))


class _Capture:
    def __init__(self, reader: GuardedReader, now: int):
        self.reader, self.now = reader, now
        self.schemas: dict[str, list[dict]] = {}
        self.started, self.queries = time.monotonic(), 0

    def check_time(self) -> None:
        if time.monotonic() - self.started >= COMMAND_SECONDS:
            raise SourceDeferred("economic_capture_command_budget; retry without advancing queue")

    def read(self, sql: str, args: tuple = ()) -> list[dict]:
        self.check_time()
        if self.queries >= COMMAND_QUERIES:
            raise SourceDeferred("economic_capture_command_budget; retry without advancing queue")
        self.queries += 1
        rows = self.reader.rows(sql, args)
        self.check_time()
        return rows

    def select(self, table: str, alias: str = "") -> str:
        """Bound TEXT before SQLite returns it, retaining explicit size qualifiers."""
        if table not in self.schemas:
            rows = self.read("SELECT name,type FROM pragma_table_info(?) ORDER BY cid LIMIT 129", (table,))
            if not rows or len(rows) > 128 or any(not _IDENT.fullmatch(r["name"]) for r in rows):
                raise SourceDeferred("economic_capture_schema_missing_or_oversized:" + table)
            self.schemas[table] = rows
        prefix = alias + "." if alias else ""
        parts = []
        for row in self.schemas[table]:
            field = row["name"]
            column = prefix + '"' + field + '"'
            if str(row["type"]).upper() == "TEXT":
                parts.extend([f"CASE WHEN length({column})>? THEN NULL ELSE {column} END AS \"{field}\"",
                              f"length({column}) AS \"_capture_length_{field}\""])
            else:
                parts.append(column)
        return ",".join(parts)

    def text_args(self, table: str) -> tuple[int, ...]:
        return tuple(TEXT_LIMIT for row in self.schemas[table] if str(row["type"]).upper() == "TEXT")

    def rows(self, packet: dict, name: str, table: str, where: str, args: tuple = (), *,
             order: str = "", cap: int = ORDER_LIMIT) -> list[dict]:
        projection = self.select(table)
        raw = self.read(f"SELECT {projection} FROM {table} WHERE {where}"
                               + (" ORDER BY " + order if order else "") + " LIMIT ?",
                               (*self.text_args(table), *args, cap + 1))
        oversized = 0
        future_rows, invalid_rows = 0, 0
        rows = []
        for value in raw[:cap]:
            row, unavailable = {}, {}
            for field, item in value.items():
                if field.startswith("_capture_length_"):
                    if item is not None and item > TEXT_LIMIT:
                        unavailable[field[len("_capture_length_"):]] = {
                            "status": "oversized_unavailable", "characters": item, "limit": TEXT_LIMIT}
                else:
                    row[field] = item
            if unavailable:
                row["_field_qualifications"] = unavailable
                oversized += len(unavailable)
            future = [field for field in _TIMESTAMPS.get(table, ())
                      if type(row.get(field)) is int and row[field] > self.now]
            invalid = [field for field in _TIMESTAMPS.get(table, ())
                       if row.get(field) is not None and _clock(row[field]) is None]
            if future or invalid:
                row["_capture_qualifications"] = {"available_at_frontier": False,
                    "future_timestamps": future, "invalid_timestamps": invalid}
                future_rows += bool(future)
                invalid_rows += bool(invalid)
            rows.append(row)
        packet["capture_coverage"]["sections"][name] = {
            "status": "partial" if len(raw) > cap or oversized or future_rows or invalid_rows else "captured",
            "row_limit": cap, "observed_rows_lower_bound": len(raw), "retained_rows": len(rows),
            "truncated": len(raw) > cap, "oversized_fields": oversized,
            "future_timestamp_rows": future_rows, "invalid_timestamp_rows": invalid_rows,
        }
        return rows


def _reject(packet: dict, reason: str, reference: Any = None) -> None:
    coverage = packet["capture_coverage"]
    coverage["rejected_attempt_link_counts"][reason] += 1
    if len(coverage["rejected_attempt_link_examples"]) < 8:
        coverage["rejected_attempt_link_examples"].append({"reason": reason, "reference": str(reference)[:256]})


def _qualify_identity(packet: dict, section: str, row: dict, expected: dict) -> None:
    """Retain contradictory rows, explicitly preventing an as-of identity claim."""
    mismatches = []
    chain = packet["position"].get("chain", "")
    for field, value in expected.items():
        observed = row.get(field)
        if observed is None or value is None:
            continue
        equal = _same(observed, value, chain) if field in {"token", "signature"} else observed == value
        if not equal:
            mismatches.append(field)
    if mismatches:
        row.setdefault("_capture_qualifications", {}).update({"position_identity_verified": False,
                                                              "identity_mismatches": mismatches})
        coverage = packet["capture_coverage"]["sections"].setdefault(section, {})
        coverage["status"] = "partial"
        coverage["identity_mismatch_rows"] = coverage.get("identity_mismatch_rows", 0) + 1


def _fit(packet: dict, capture: _Capture) -> None:
    """Keep retained packets modest; every dropped row remains a declared gap."""
    for field in ("exit_timing_events", "order_events", "fill_reconciliations", "fill_prices",
                  "trades", "foreign_position_links", "referenced_orders", "attempts",
                  "fill_costs", "orders", "position_order_links", "watchdog_state"):
        capture.check_time()
        while len(_json(packet).encode()) > MAX_PACKET_BYTES - 512 and packet.get(field):
            capture.check_time()
            rows = packet[field]
            packet[field] = rows[:len(rows) // 2]
            section = packet["capture_coverage"]["sections"].setdefault(field, {})
            section.update({"status": "partial", "packet_budget_truncated": True,
                            "retained_rows": len(packet[field])})
    if len(_json(packet).encode()) > MAX_PACKET_BYTES - 512:
        raise SourceDeferred("economic_capture_position_metadata_exceeds_packet_budget")


def _episode(capture: _Capture, position_id: str) -> dict | None:
    packet: dict[str, Any] = {
        "source": "canonical_economic_capture", "capture_version": VERSION,
        "projection_frontier_ms": capture.now,
        "coverage": dict.fromkeys(_ECONOMIC_COVERAGE, False),
        "capture_coverage": {"sections": {}, "history_start_ms": max(0, capture.now - HISTORY_MS),
            "history_horizon_ms": HISTORY_MS, "rejected_attempt_link_counts": Counter(),
            "rejected_attempt_link_examples": [], "warnings": [],
            "economic_proofs": "identity_cashflows_attempt_coverage_recoveries_custody_and_exit_quote_not_certified"},
        "cashflows": [], "recoveries": [],
    }
    p = capture.rows(packet, "position", "positions", "position_id=?", (position_id,), cap=1)
    if not p:
        return None
    position = packet["position"] = p[0]
    opened = _clock(position.get("opened_ms"))
    if opened is None or opened > capture.now:
        packet["capture_coverage"]["warnings"].append("position_opening_clock_missing_or_future")
    start = max(0, capture.now - HISTORY_MS, opened or 0)
    packet["capture_coverage"]["history_clipped"] = opened is None or opened < capture.now - HISTORY_MS
    packet["identity"] = {"chain": position.get("chain"), "token": position.get("token"), "verified": False}
    packet["inventory"] = {"qty_remaining": position.get("qty"), "frontier_ms": capture.now,
                           "opening_quantity": None, "quantity_reconciled": False, "transfers_complete": False}
    packet["position_order_links"] = capture.rows(packet, "position_order_links", "position_orders",
        "position_id=?", (position_id,), order="ts_ms,order_id")
    packet["trades"] = capture.rows(packet, "trades", "trades", "position_id=?", (position_id,),
                                      order="closed_ms DESC,trade_id", cap=TRADE_LIMIT)
    for trade in packet["trades"]:
        _qualify_identity(packet, "trades", trade,
                          {field: position.get(field) for field in ("chain", "token", "mode", "lane")})
    packet["watchdog_state"] = capture.rows(packet, "watchdog_state", "watchdog_state", "position_id=?",
                                              (position_id,), cap=1)
    prefix = "exit_timing:" + position_id
    packet["exit_timing_events"] = capture.rows(packet, "exit_timing_events", "events",
        "dedupe_key>=? AND dedupe_key<? AND ts_ms>=? AND ts_ms<=?",
        (prefix + ":", prefix + ";", start, capture.now), order="events.dedupe_key", cap=EVENT_LIMIT)
    proofs: dict[str, list[dict]] = {}
    for state in packet["watchdog_state"]:
        order_id = state.get("exit_order_id")
        stamp = _clock(state.get("updated_ms"))
        if (isinstance(order_id, str) and 0 < len(order_id) <= 256
                and stamp is not None and opened is not None and opened <= stamp <= capture.now):
            proofs.setdefault(order_id, []).append({"source": "watchdog_state.exit_order_id", "updated_ms": stamp})
        elif order_id:
            _reject(packet, "watchdog_reference_clock_unproved", order_id)
    for event in packet["exit_timing_events"]:
        try:
            body = json.loads(event.get("payload") or "null")
        except (TypeError, ValueError):
            body = None
        valid = (isinstance(body, dict) and body.get("service") == "watchdog"
                 and body.get("event") == "exit_timing" and body.get("position_id") == position_id
                 and event.get("chain") == position.get("chain")
                 and _same(event.get("subject"), position.get("token"), position.get("chain", ""))
                 and body.get("mode") == position.get("mode") and body.get("lane") == position.get("lane"))
        order_id = body.get("order_id") if isinstance(body, dict) else None
        if not valid or not isinstance(order_id, str) or not order_id or len(order_id) > 256:
            _reject(packet, "exit_timing_identity_or_payload_unproved", event.get("id"))
            continue
        proofs.setdefault(order_id, []).append({"source": "events.exit_timing", "event_id": event["id"],
                                                "dedupe_key": event.get("dedupe_key")})
    applied_links = {row["order_id"]: row for row in packet["position_order_links"] if row.get("order_id")}
    applied = set(applied_links)
    extra = sorted(set(proofs) - applied)
    if len(extra) > ORDER_LIMIT:
        packet["capture_coverage"]["warnings"].append("attempt_order_reference_cap")
        extra = extra[:ORDER_LIMIT]
    order_ids = sorted(applied | set(extra))
    if order_ids:
        marks = ",".join("?" * len(order_ids))
        packet["referenced_orders"] = capture.rows(packet, "referenced_orders", "orders",
            f"order_id IN ({marks})", tuple(order_ids), order="order_id", cap=2 * ORDER_LIMIT)
        packet["foreign_position_links"] = capture.rows(packet, "foreign_position_links", "position_orders",
            f"order_id IN ({marks}) AND position_id!=?", (*order_ids, position_id),
            order="order_id,position_id", cap=2 * ORDER_LIMIT)
    else:
        packet["referenced_orders"], packet["foreign_position_links"] = [], []
    foreign = {row["order_id"] for row in packet["foreign_position_links"]}
    foreign_incomplete = packet["capture_coverage"]["sections"].get("foreign_position_links", {}).get("truncated", False)
    packet["orders"], packet["attempts"] = [], []
    missing = set(order_ids)
    for order in packet["referenced_orders"]:
        oid = order["order_id"]
        missing.discard(oid)
        if oid in applied:
            _qualify_identity(packet, "referenced_orders", order,
                {**{field: position.get(field) for field in ("chain", "token", "mode", "lane")},
                 "side": applied_links[oid].get("side")})
            if applied_links[oid].get("_capture_qualifications", {}).get("available_at_frontier") is False:
                order.setdefault("_capture_qualifications", {})["position_link_available_at_frontier"] = False
            packet["orders"].append(order)
            continue
        valid = (order.get("side") == "sell" and order.get("chain") == position.get("chain")
                 and _same(order.get("token"), position.get("token"), position.get("chain", ""))
                 and order.get("mode") == position.get("mode") and order.get("lane") == position.get("lane")
                 and opened is not None and _clock(order.get("created_ms")) is not None
                 and opened <= order["created_ms"] <= capture.now)
        if not valid or oid in foreign or foreign_incomplete:
            _reject(packet, "attempt_order_identity_or_position_conflict", oid)
            continue
        packet["orders"].append(order)
        packet["attempts"].append({**order, "position_id": position_id, "mapping_proof": proofs[oid]})
    if missing:
        packet["capture_coverage"]["missing_order_ids"] = sorted(missing)
    selected = [row["order_id"] for row in packet["orders"]]
    specs = (("fill_costs", "order_id", 2 * ORDER_LIMIT), ("fill_prices", "order_id", 2 * ORDER_LIMIT),
             ("fill_reconciliations", "order_id,checked_ms DESC,id DESC", RECONCILIATION_LIMIT),
             ("order_events", "id DESC", EVENT_LIMIT))
    for table, ordering, cap in specs:
        if selected:
            marks = ",".join("?" * len(selected))
            where, args = f"order_id IN ({marks})", tuple(selected)
            if table == "order_events":
                where += " AND ts_ms>=? AND ts_ms<=?"
                args = (*args, start, capture.now)
            packet[table] = capture.rows(packet, table, table, where, args, order=ordering, cap=cap)
            indexed = {row["order_id"]: row for row in packet["orders"]}
            for row in packet[table]:
                order = indexed[row["order_id"]]
                expected = {"chain": position.get("chain"), "token": position.get("token"),
                            "side": order.get("side")}
                if table in {"fill_costs", "fill_reconciliations"}:
                    expected["signature"] = order.get("tx_hash")
                _qualify_identity(packet, table, row, expected)
        else:
            packet[table] = []
    _fit(packet, capture)
    coverage = packet["capture_coverage"]
    coverage["rejected_attempt_link_counts"] = dict(coverage["rejected_attempt_link_counts"])
    coverage["status"] = "partial" if (coverage["history_clipped"] or coverage["warnings"] or coverage.get("missing_order_ids")
        or coverage["rejected_attempt_link_counts"] or any(s.get("status") == "partial"
        for s in coverage["sections"].values())) else "captured"
    packet["source_hash"] = _hash({"version": VERSION, "position": packet["position"],
        **{name: packet.get(name, []) for name in _RAW_SECTIONS},
        "qualifications": {k: v for k, v in coverage.items() if k != "history_start_ms"}})
    packet["capture_spec_hash"] = _hash({"version": VERSION, "text_limit": TEXT_LIMIT,
        "packet_bytes": MAX_PACKET_BYTES, "orders": ORDER_LIMIT, "events": EVENT_LIMIT,
        "reconciliations": RECONCILIATION_LIMIT, "trades": TRADE_LIMIT, "history_ms": HISTORY_MS,
        "command_seconds": COMMAND_SECONDS, "command_queries": COMMAND_QUERIES})
    if len(_json(packet).encode()) > MAX_PACKET_BYTES:
        raise SourceDeferred("economic_capture_final_packet_budget")
    return packet


def capture_positions(reader: GuardedReader, position_ids: Sequence[str], *, now_ms: int) -> dict:
    """Capture explicit new/retry/open IDs without advancing or acknowledging a queue.

    Source WAL/deadline/reader-cap refusals propagate. Ordinary per-section clipping
    remains explicit partial evidence, bounded to64KiB per packet. Rereading the same
    ID sees a later cost row; unchanged evidence keeps its stable ``source_hash``.
    """
    if type(now_ms) is not int or now_ms <= 0:
        raise ValueError("capture requires positive integer now_ms")
    if (not isinstance(position_ids, (list, tuple)) or len(position_ids) > MAX_POSITIONS
            or any(not isinstance(pid, str) or not pid or len(pid) > 256 for pid in position_ids)):
        raise ValueError("capture requires at most 30 explicit nonempty position IDs")
    if not 0 < reader.query_seconds <= 3:
        raise ValueError("capture reader query deadline must be <=3s")
    reader.guard()
    before = reader.queries
    capture = _Capture(reader, now_ms)
    packets, missing = [], []
    for position_id in dict.fromkeys(position_ids):
        packet = _episode(capture, position_id)
        if packet is None:
            missing.append(position_id)
        else:
            packets.append(packet)
    capture.check_time()
    return {"version": VERSION, "frontier_ms": now_ms, "packets": packets,
            "missing_position_ids": missing,
            "capture_coverage": {"requested": len(set(position_ids)), "captured": len(packets),
                "partial": sum(p["capture_coverage"]["status"] == "partial" for p in packets),
                "economic_completeness_certified": False, "max_packet_bytes": MAX_PACKET_BYTES},
            "query_receipt": {"count": reader.queries - before, "maximum_s": max(
                (r["duration_s"] for r in reader.receipts[before:]), default=0)}}
