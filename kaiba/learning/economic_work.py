"""Bounded, durable economic-evidence revisits; no source writes or transport.

Codex AGENT-CAPTURE-20261015. This is a rolling historical audit, never a
prospective strategy sample. Missing receipts remain unknown after every retry.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from kaiba.learning.autolearn_io import DAY_MS, GuardedReader

VERSION = "economic-work/1"
MAX_RECORDS = 128
MAX_BYTES = 16 * 1024 * 1024
MAX_PACKET_BYTES = 256 * 1024
AUDIT_DAYS = 12
RETRY_MS = 60_000
MAX_OUTBOX = 256
MAX_CAPTURE_PER_CYCLE = 1


def _encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _load(path: Path, now: int) -> dict:
    if path.is_symlink():
        raise ValueError("unsafe_economic_work_path")
    if not path.exists():
        return {"version": VERSION, "records": {}, "closed_cursor": [now - AUDIT_DAYS * DAY_MS - 1, 0],
                "open_cursor": 0, "evicted_total": 0, "expired_total": 0, "sweeps": 0,
                "revision_outbox": {}, "source_observed_ms": None}
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("economic_work_storage_budget")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("economic_work_storage_budget")
    state = json.loads(raw)
    if (state.get("version") != VERSION or not isinstance(state.get("records"), dict)
            or len(state["records"]) > MAX_RECORDS):
        raise ValueError("invalid_economic_work_checkpoint")
    outbox = state.setdefault("revision_outbox", {})
    if (not isinstance(outbox, dict) or len(outbox) > MAX_OUTBOX
            or any(not isinstance(r, dict) or key != _hash(r) for key, r in outbox.items())):
        raise ValueError("invalid_economic_revision_outbox")
    cursor = state.get("closed_cursor")
    if (not isinstance(cursor, list) or len(cursor) != 2 or any(type(n) is not int or n < 0 for n in cursor)
            or type(state.get("open_cursor")) is not int or state["open_cursor"] < 0):
        raise ValueError("invalid_economic_work_cursor")
    for pid, row in state["records"].items():
        if (not isinstance(row, dict) or not isinstance(pid, str) or not pid or len(pid) > 512
                or row.get("position_id") != pid
                or any(type(row.get(k)) is not int or row[k] < 0
                       for k in ("first_seen_ms", "last_checked_ms", "last_changed_ms", "revision_count"))
                or (row.get("packet") is not None
                    and row["retained_hash"] != _hash(row["packet"]))):
            raise ValueError("invalid_economic_work_record")
    _source_frontier(state)
    return state


def _source_frontier(state: Mapping) -> int | None:
    """A checkpoint write or journal acknowledgment is not a source observation.

    Legacy checkpoints only kept ``updated_ms``, including zero-read outbox passes.
    Their retained records' actual check clocks give a conservative fallback; an
    empty legacy checkpoint has no independently retained observation clock.
    """
    if "source_observed_ms" not in state:
        return max((r["last_checked_ms"] for r in state["records"].values()), default=None)
    value = state["source_observed_ms"]
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("invalid_economic_work_source_frontier")
    return value


def _save(path: Path, state: Mapping) -> int:
    data = _encode(state) + b"\n"
    if len(data) > MAX_BYTES:
        raise ValueError("economic_work_storage_budget")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    if temporary.is_symlink():
        raise ValueError("unsafe_economic_work_temporary")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return len(data)


def capture_work(reader: GuardedReader, path: Path | str, *, now_ms: int) -> dict:
    """Capture new closes, rotate open inventory and revisit retained episodes.

    The research worker's exclusive lock serializes callers. Selection and packet
    assembly must all succeed before one atomic checkpoint
    advances. A failed read leaves every cursor/retry unchanged. Successful raw
    truncation or missing source rows are explicitly retained as unavailable,
    rather than permanently losing their retry identity.
    """
    from kaiba.core.redact import redact_text
    from kaiba.learning.economic_capture import capture_positions
    from kaiba.learning.economic_outcomes import project_outcomes

    if type(now_ms) is not int or now_ms < AUDIT_DAYS * DAY_MS:
        raise ValueError("invalid_economic_work_frontier")
    path = Path(path)
    source = reader.db_path.resolve()
    if (path.resolve() in {source, Path(str(source) + "-wal"), Path(str(source) + "-shm")}
            or (path.exists() and source.exists() and path.samefile(source))):
        raise ValueError("economic_work_must_not_replace_source")
    state = _load(path, now_ms)
    source_observed_ms = _source_frontier(state)
    records = state["records"]
    floor = now_ms - AUDIT_DAYS * DAY_MS
    before_queries = len(reader.receipts)
    capacity = MAX_OUTBOX - len(state["revision_outbox"])
    outbox_full = capacity == 0
    # Only real-money modes enter this audit. Real write-offs remain losses;
    # shadow write-offs and every other paper result are outside its population.
    cursor_ms, cursor_row = state["closed_cursor"]
    if cursor_ms < floor:
        cursor_ms, cursor_row = floor - 1, 0
    closed = [] if outbox_full else reader.rows(
        "SELECT rowid AS source_rowid,position_id,closed_ms FROM positions "
        "WHERE mode IN ('live','canary') AND closed_ms>=? AND closed_ms<=? "
        "AND (closed_ms,rowid)>(?,?) ORDER BY closed_ms,rowid LIMIT ?",
        (max(floor, cursor_ms), now_ms, cursor_ms, cursor_row, min(3, capacity)))
    opened = [] if outbox_full else reader.rows(
        "SELECT rowid AS source_rowid,position_id FROM positions "
        "WHERE closed_ms IS NULL AND mode IN ('live','canary') AND opened_ms<=? "
        "AND rowid>? ORDER BY rowid LIMIT ?", (now_ms, state["open_cursor"], min(2, max(0, capacity - len(closed)))))
    new_ids = [r["position_id"] for r in closed]
    open_ids = [r["position_id"] for r in opened]
    retries = sorted((r for r in records.values()
                      if now_ms - r["last_checked_ms"] >= RETRY_MS
                      and r["position_id"] not in new_ids + open_ids),
                     key=lambda r: (r["last_checked_ms"], r["position_id"]))[:min(3, max(0, capacity - len(closed) - len(opened)))]
    # A large all-or-nothing batch repeatedly lost admitted work to cold SQLite
    # reads on the production box. Commit one episode per cycle and rotate source
    # kinds so closes cannot starve open custody or late-receipt revisits.
    turn = int(state.get("selection_turn", 0)) % 3
    queues = (new_ids, open_ids, [r["position_id"] for r in retries])
    selected = []
    for offset in range(3):
        selected.extend(pid for pid in queues[(turn + offset) % 3] if pid not in selected)
        if selected:
            selected = selected[:MAX_CAPTURE_PER_CYCLE]
            break
    # Advance only a captured contiguous prefix. A retry of a later row must not
    # acknowledge unseen closes or open rows that precede it in the source scan.
    captured_closed = closed[:1] if closed and closed[0]["position_id"] in selected else []
    captured_open = opened[:1] if opened and opened[0]["position_id"] in selected else []
    capture = capture_positions(reader, selected, now_ms=now_ms) if selected else {"packets": [], "missing_position_ids": []}
    packets = {p["position"]["position_id"]: p for p in capture["packets"]}
    missing = set(capture.get("missing_position_ids", []))
    if set(packets) & missing or set(packets) | missing != set(selected):
        raise ValueError("economic_capture_identity_set_mismatch")
    changed, unchanged, oversized, revisions = [], [], [], []
    for pid in selected:
        old = records.get(pid, {})
        packet = packets.get(pid)
        # Never project an earlier proof as current when its source disappeared.
        source_hash = packet.get("source_hash") if packet else None
        if packet is not None and (not isinstance(source_hash, str) or len(source_hash) != 64):
            raise ValueError("economic_capture_source_hash_missing")
        retained = json.loads(redact_text(_encode(packet).decode())) if packet else None
        if retained is not None and len(_encode(retained)) > MAX_PACKET_BYTES:
            oversized.append(pid)
            retained = None
        if retained is not None and retained.get("position", {}).get("mode") not in {"live", "canary"}:
            raise ValueError("economic_capture_mode_changed")
        status = "source_missing" if packet is None else "packet_budget_exceeded" if retained is None else "captured"
        has_change = not old or old.get("source_hash") != source_hash or old.get("status") != status
        if not has_change:
            # The capture frontier moves on every poll. Keep the original packet
            # when its stable source evidence did not change, rather than inventing
            # a new evidence revision just by revisiting it.
            retained = old.get("packet")
        history = list(old.get("recent_source_revisions", []))
        if has_change:
            history.append({"source_hash": source_hash, "status": status, "at_ms": now_ms})
            if old:
                position = (retained or {}).get("position", {})
                revision = {"position_id": pid, "previous_source_hash": old.get("source_hash"),
                    "source_hash": source_hash, "status": status, "at_ms": now_ms,
                    "chain": position.get("chain"), "mode": position.get("mode"), "lane": position.get("lane"),
                    "raw_cost_rows": len((retained or {}).get("fill_costs", [])),
                    "raw_cost_rows_hash": _hash((retained or {}).get("fill_costs", []))}
                revisions.append(revision)
                state["revision_outbox"][_hash(revision)] = revision
        (changed if has_change else unchanged).append(pid)
        records[pid] = {"position_id": pid, "first_seen_ms": old.get("first_seen_ms", now_ms),
            "last_checked_ms": now_ms, "last_changed_ms": now_ms if has_change else old["last_changed_ms"],
            "revision_count": old.get("revision_count", 0) + int(has_change),
            "recent_source_revisions": history[-8:],
            "source_hash": source_hash, "retained_hash": _hash(retained) if retained else None,
            "retained_redacted": retained != packet, "status": status, "packet": retained,
            "last_closed_ms": packet["position"].get("closed_ms") if packet else old.get("last_closed_ms")}
    expired = [pid for pid, r in records.items()
               if r.get("last_closed_ms") is not None and r["last_closed_ms"] < floor]
    for pid in expired:
        del records[pid]
    evicted = []
    # Retention is explicit, not a completeness claim. Repeated bounded sweeps of
    # the 12-day close window can reacquire episodes evicted by the cache budget.
    while len(records) > MAX_RECORDS or len(_encode(state)) + 1024 > MAX_BYTES:
        if not records:
            raise ValueError("economic_work_metadata_budget")
        victim = min(records.values(), key=lambda r: (r["last_checked_ms"], r["first_seen_ms"], r["position_id"]))
        evicted.append(victim["position_id"])
        del records[victim["position_id"]]
    if captured_closed:
        state["closed_cursor"] = [captured_closed[-1]["closed_ms"], captured_closed[-1]["source_rowid"]]
    elif not closed and not outbox_full:
        state["closed_cursor"] = [floor - 1, 0]
        state["sweeps"] += 1
    if captured_open:
        state["open_cursor"] = captured_open[-1]["source_rowid"]
    elif not opened and not outbox_full:
        state["open_cursor"] = 0
    if not outbox_full:
        state["selection_turn"] = (turn + 1) % 3
    state["evicted_total"] += len(evicted)
    state["expired_total"] += len(expired)
    observed = bool(reader.receipts[before_queries:])
    state["source_observed_ms"] = now_ms if observed else source_observed_ms
    state["updated_ms"] = now_ms
    # Project the entire retained set together, so cross-episode transaction reuse
    # is still checked. Other unseen/evicted episodes remain outside this subset.
    economic = project_outcomes(r["packet"] for r in records.values() if r.get("packet") is not None)
    economic["population_scope"] = "rolling_historical_real_money_audit_not_strategy_validation"
    economic["checkpoint_version"] = VERSION
    economic["as_of_ms"] = state["source_observed_ms"]
    economic["unavailable_retained_count"] = sum(r["packet"] is None for r in records.values())
    size = _save(path, state)
    receipt = {"version": VERSION, "at_ms": state["source_observed_ms"], "checked_ms": now_ms,
        "new_source_observation": observed, "population_scope": economic["population_scope"],
        "new_closed_ids": [r["position_id"] for r in captured_closed],
        "open_ids": [r["position_id"] for r in captured_open],
        "retry_ids": [r["position_id"] for r in retries if r["position_id"] in selected],
        "selection_policy": "one_episode_rotating_closed_open_retry/1", "max_capture_per_cycle": MAX_CAPTURE_PER_CYCLE,
        "changed_ids": changed, "unchanged_ids": unchanged, "missing_ids": sorted(missing),
        "revisions": list(state["revision_outbox"].values()), "observed_revisions": revisions,
        "revision_outbox_count": len(state["revision_outbox"]),
        "selection_deferred": "economic_journal_outbox_full" if outbox_full else None,
        "oversized_ids": oversized, "evicted_ids": evicted, "expired_ids": expired,
        "retained_count": len(records), "evicted_total": state["evicted_total"],
        "expired_total": state["expired_total"], "completed_close_sweeps": state["sweeps"],
        "closed_cursor": state["closed_cursor"], "open_cursor": state["open_cursor"],
        "audit_days": AUDIT_DAYS, "max_retained_records": MAX_RECORDS,
        "checkpoint_bytes": size, "checkpoint_budget_bytes": MAX_BYTES,
        "queries": reader.receipts[before_queries:], "source_writes": 0,
        "population_complete": False, "funding_allowed": False}
    return {"economic": economic, "capture": receipt}


def acknowledge_revisions(path: Path | str, evidence_ids: set[str], *, now_ms: int) -> int:
    """Remove only revisions acknowledged by canonical idempotent journalling.

    Call under the same exclusive worker lock. A crash before this acknowledgment
    repeats the canonical evidence ID; it cannot create another journal credit.
    """
    path = Path(path)
    state = _load(path, now_ms)
    removed = 0
    for key in list(state["revision_outbox"]):
        if "economic-revision:" + key in evidence_ids:
            del state["revision_outbox"][key]
            removed += 1
    if removed:
        _save(path, state)
    return removed


def cached_work(path: Path | str, *, now_ms: int, reason: str) -> dict:
    """Read bounded retained observations/outbox without a source query or refresh.

    Paper polling can continue while optional economic capture waits. Preserve the
    original as-of clock and outstanding journal receipts; file reads are not new
    economic evidence or an acknowledgment of any source cursor.
    """
    from kaiba.learning.economic_outcomes import project_outcomes
    state = _load(Path(path), now_ms)
    records = state["records"]
    source_observed_ms = _source_frontier(state)
    economic = project_outcomes(r["packet"] for r in records.values() if r.get("packet") is not None)
    economic.update(population_scope="rolling_historical_real_money_audit_not_strategy_validation",
                     checkpoint_version=VERSION, as_of_ms=source_observed_ms,
                    unavailable_retained_count=sum(r.get("packet") is None for r in records.values()))
    return {"economic": economic, "capture": {"version": VERSION, "status": "cached", "reason": reason,
        "at_ms": source_observed_ms, "checked_ms": now_ms, "new_source_observation": False,
        "retained_count": len(records), "revisions": list(state["revision_outbox"].values()),
        "revision_outbox_count": len(state["revision_outbox"]), "selection_deferred": None,
        "queries": [], "source_writes": 0, "funding_allowed": False}}
