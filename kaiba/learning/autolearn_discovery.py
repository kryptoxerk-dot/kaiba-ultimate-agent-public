"""Resume a bounded frozen historical discovery roster without retaining raw tape.

Codex AUTOLEARN-IMPLEMENT-20261014. Uses the existing audit/path collector and
GuardedReader. A partial collection never releases observations to the factory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kaiba.learning import signal_audit as sa
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import strategy_search as ss
from kaiba.learning.autolearn_io import (
    GuardedReader,
    SourceDeferred,
    _measure_clock,
    _usable_measure,
    _value,
    load_json,
)

VERSION = "autolearn_discovery/2"
MAX_WORK_BYTES = 10_000_000
DAY_MS = ss.DAY_MS
TAPE_PAGE_ROWS = 5_000
MAX_TAPE_ROWS = 100_000
MAX_TAPE_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_TAPE_ATTEMPTS = 3


class _TapeBudget(SourceDeferred):
    """A private exact tape did not finish before this collection tick's deadline."""


class _TapeLimit(ValueError):
    """The whole selected signal stays unknown, rather than silently sampling its tape."""


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def _versions() -> dict[str, str]:
    return {**sc.source_versions(), "learning/autolearn_discovery.py":
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _progress(work: Mapping[str, Any]) -> dict[str, Any]:
    return {k: work[k] for k in ("anchor_ms", "cutoff_ms", "window_start_ms", "retention_ms",
                                 "source_rowid_frontier", "group_cursor", "roster", "cursor",
                                 "observations", "no_tape", "invalid_prices", "tape_frontier", "tape_failures")}


def _save(path: Path, work: dict[str, Any]) -> None:
    work["progress_hash"] = _hash(_progress(work))
    work["roster_hash"] = _hash(work["roster"])
    text = json.dumps(work, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode()) >= MAX_WORK_BYTES:
        raise ValueError("discovery_work_exceeds_10MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(text + "\n", encoding="utf-8")
    os.replace(partial, path)


def _features(payload: Any, t0: int) -> dict[str, Any]:
    """Match the prospective input adapter: unwrap usable original signal measures."""
    payload = load_json(payload)
    if not isinstance(payload, Mapping):
        return {}
    result = {}
    for key in sa.DOSSIER_KEYS:
        if key not in payload:
            continue
        raw = payload[key]
        if isinstance(raw, Mapping):
            stamp = _measure_clock(raw)
            if raw.get("receipt") is not None and (stamp is None or stamp > t0):
                continue
            if not _usable_measure(raw):
                continue
        value = _value(raw)
        if value is None or not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        result[key] = value
    return result


def _source_hash(row: Mapping[str, Any]) -> str:
    # Hash original bytes/identity; never retain the unfiltered payload or receipts.
    return _hash({k: row[k] for k in ("signal_id", "lane", "chain", "token", "created_ms",
                                    "wallets_json", "payload_json")})


def _roster_row(row: Mapping[str, Any]) -> dict[str, Any]:
    wallets = load_json(row["wallets_json"], [])
    wallets = [str(w) for w in wallets[:1000]] if isinstance(wallets, list) else []
    return {"rowid": int(row["source_rowid"]), "signal_id": str(row["signal_id"]),
            "lane": str(row["lane"]), "chain": str(row["chain"]), "token": str(row["token"]),
            "t0": int(row["created_ms"]), "wallets": wallets,
            "features": _features(row["payload_json"], int(row["created_ms"])),
            "source_hash": _source_hash(row)}


def _retention(reader: GuardedReader) -> int:
    rows = reader.rows("SELECT value FROM kv WHERE key='retention:swaps:horizon_ms'")
    if not rows:
        return 0
    raw = load_json(rows[0]["value"], rows[0]["value"])
    if isinstance(raw, bool):
        raise ValueError("retention_watermark_unreadable")
    value = int(raw)
    if value < 0 or str(value) != str(raw).strip():
        raise ValueError("retention_watermark_unreadable")
    return value


def _roster_valid(reader: GuardedReader, work: Mapping[str, Any]) -> bool:
    if not work["roster"]:
        return True
    expected = {row["rowid"]: row["source_hash"] for row in work["roster"]}
    marks = ",".join("?" * len(expected))
    current = reader.rows("SELECT rowid AS source_rowid,signal_id,lane,chain,token,created_ms,"
                          f"wallets_json,payload_json FROM signals WHERE rowid IN ({marks})", list(expected))
    return (len(current) == len(expected)
            and all(expected.get(int(row["source_rowid"])) == _source_hash(row) for row in current))


def _history_valid(work: Mapping[str, Any], now_ms: int) -> bool:
    # AGENTS.md caps swap readers at 13 days even if row retention still keeps 14.
    # Include the full pre-signal lookback: a frozen 12-day roster can age while
    # waiting for more chunks, and its original anchor cannot extend that ceiling.
    return all(row["t0"] - sa.PRE_WINDOW_MS >= now_ms - 13 * DAY_MS for row in work["roster"])


def _tape(reader: GuardedReader, row: Mapping[str, Any], frontier: int, *, deadline: float,
          clock: Callable[[], float], progress: dict[str, Any]) -> list[tuple]:
    """Read every original row, without OFFSET, under independent guarded connections.

    The global swap ROWID frontier freezes late/backfilled inserts as well as new
    prints. The (timestamp, ROWID) cursor preserves tied timestamps exactly. The
    byte limit measures UTF-8 row payload; Python/model overhead is additional and
    bounded by the separate 100k-row ceiling. Raw pages never reach the work file.
    """
    start, stop = row["t0"] - sa.PRE_WINDOW_MS, row["t0"] + sa.HORIZON_MS
    page_rows = min(TAPE_PAGE_ROWS, reader.max_rows)
    cursor = (start, -1)
    result: list[tuple] = []
    total_bytes = 0
    while True:
        if clock() >= deadline:
            raise _TapeBudget("exact_tape_tick_budget")
        requested_rows = min(page_rows, MAX_TAPE_ROWS - len(result) + 1)
        page = reader.q(
            "SELECT rowid,ts_ms,price_usd,source,wallet,side FROM swaps "
            "WHERE token=? AND chain=? AND ts_ms BETWEEN ? AND ? AND rowid<=? "
            "AND (ts_ms,rowid)>(?,?) ORDER BY ts_ms,rowid LIMIT ?",
            (row["token"], row["chain"], start, stop, frontier, *cursor, requested_rows),
        )
        if not page:
            return result
        total_bytes += sum(sum(len(str(value).encode()) if value is not None else 4 for value in record)
                           for record in page)
        progress.update({"rows_read": len(result) + len(page), "payload_bytes": total_bytes,
                         "last_timestamp_ms": int(page[-1][1]), "last_rowid": int(page[-1][0])})
        if len(result) + len(page) > MAX_TAPE_ROWS or total_bytes > MAX_TAPE_PAYLOAD_BYTES:
            raise _TapeLimit("frozen_discovery_exact_tape_resource_limit")
        result.extend(tuple(record[1:]) for record in page)
        cursor = int(page[-1][1]), int(page[-1][0])
        if len(page) < requested_rows:
            return result


def _result(work: Mapping[str, Any], *, reason: str | None = None, error: str | None = None) -> dict[str, Any]:
    complete = (error is None and reason is None and work["group_cursor"] == len(work["policy"]["groups"])
                and work["cursor"] == len(work["roster"]))
    counts = {"considered": len(work["roster"]), "processed": work["cursor"],
              "priced": len(work["observations"]), "no_tape": work["no_tape"],
              "invalid_prices": work["invalid_prices"],
              "frozen_groups": work["group_cursor"], "groups": len(work["policy"]["groups"])}
    current = work["roster"][work["cursor"]] if work["cursor"] < len(work["roster"]) else None
    return {"status": "complete" if complete else "collecting", "verdict": "UNPROVEN",
            "observations": [ss.Obs(**row) for row in work["observations"]] if complete else [],
            "counts": counts, "anchor_ms": work["anchor_ms"], "cutoff_ms": work["cutoff_ms"],
            "window_start_ms": work["window_start_ms"], "roster_hash": work.get("roster_hash"),
            "reason": reason, "error": error, "terminal": error is not None,
            "current_signal": None if current is None else {
                "signal_id": current["signal_id"], "lane": current["lane"], "chain": current["chain"],
                "token": current["token"], "t0": current["t0"],
                "attempt": work.get("tape_failures", {}).get(str(current["rowid"]))},
            "funding_allowed": False}


def collect(reader: GuardedReader, work_path: Path | str, *, groups: Sequence[tuple[str, str]],
            ladder: sa.ExitLadder, now_ms: int, signal_cap: int = 750, budget_seconds: float = 20,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Freeze, then resume the SAME roster under a per-invocation wall-clock budget.

    Latest settled signals are capped equally by group and deduplicated with the
    original audit's MIN(rowid) per token/UTC day. Only scalar frozen features and
    priced Obs are retained, never swap rows. Deferred SQL does not advance a cursor.
    A deleted/changed selected signal, changed code/policy or expired retention rejects
    the entire work sample. The caller must retain that work rather than erase it.
    """
    if isinstance(signal_cap, bool) or not isinstance(signal_cap, int) or not 1 <= signal_cap <= 750:
        raise ValueError("signal_cap must be an integer between 1 and 750")
    if not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 30:
        raise ValueError("discovery budget must be positive and at most 30 seconds")
    normalized = sorted({(str(lane), str(chain)) for lane, chain in groups})
    if not normalized or len(normalized) > 9:
        raise ValueError("discovery needs one to nine groups")
    started = clock()
    path = Path(work_path).resolve()
    policy = {"groups": [list(g) for g in normalized], "signal_cap": signal_cap,
              "source_db": str(reader.db_path.resolve()),
              "history_days": 12, "dedupe": "MIN(rowid)/token/UTCday",
              "tape": {"page_rows": TAPE_PAGE_ROWS, "max_rows": MAX_TAPE_ROWS,
                       "max_payload_bytes": MAX_TAPE_PAYLOAD_BYTES, "max_stalled_attempts": MAX_TAPE_ATTEMPTS},
              "ladder": ss.ExitPolicy("incumbent", ladder).spec(),
              "feature_keys": sorted(sa.DOSSIER_KEYS)}
    versions = _versions()
    if path.exists():
        if path.stat().st_size >= MAX_WORK_BYTES:
            raise ValueError("discovery_work_exceeds_10MB")
        work = json.loads(path.read_text(encoding="utf-8"))
        if (work.get("version") != VERSION or work.get("source_versions") != versions
                or work.get("policy") != policy):
            return _result(work, error="frozen_discovery_code_or_policy_changed")
        if (work.get("progress_hash") != _hash(_progress(work))
                or work.get("roster_hash") != _hash(work["roster"])):
            return _result(work, error="frozen_discovery_progress_corrupt")
        if work["anchor_ms"] > now_ms:
            return _result(work, error="discovery_clock_moved_backward")
    else:
        work = {"version": VERSION, "policy": policy, "source_versions": versions,
                "anchor_ms": int(now_ms), "cutoff_ms": int(now_ms) - ss.SETTLE_MS,
                "window_start_ms": int(now_ms) - 12 * DAY_MS, "retention_ms": None,
                "source_rowid_frontier": None, "group_cursor": 0, "roster": [], "cursor": 0,
                "observations": [], "no_tape": 0, "invalid_prices": 0,
                "tape_frontier": None, "tape_failures": {}}
        _save(path, work)
    try:
        if not _history_valid(work, now_ms):
            return _result(work, error="frozen_discovery_history_exceeds_13_days")
        horizon = _retention(reader)
        if work["retention_ms"] is None:
            work["retention_ms"] = horizon
            work["window_start_ms"] = max(work["window_start_ms"], horizon)
            _save(path, work)
        if work["roster"] and horizon > min(row["t0"] for row in work["roster"]):
            return _result(work, error="frozen_discovery_roster_expired_by_retention")
        if horizon > work["cutoff_ms"]:
            return _result(work, error="retention_has_no_settled_discovery_window")
        if work["source_rowid_frontier"] is None:
            if clock() - started >= budget_seconds:
                return _result(work, reason="budget")
            rows = reader.rows("SELECT COALESCE(MAX(rowid),0) AS frontier FROM signals")
            work["source_rowid_frontier"] = int(rows[0]["frontier"])
            _save(path, work)
        if work["tape_frontier"] is None:
            if clock() - started >= budget_seconds:
                return _result(work, reason="budget")
            # Unfiltered MAX(rowid) uses SQLite's table-end optimization. A token-
            # filtered MAX could walk an unrelated rowid prefix on the live tape.
            frontier = reader.rows("SELECT COALESCE(MAX(rowid),0) AS frontier FROM swaps")
            work["tape_frontier"] = int(frontier[0]["frontier"])
            _save(path, work)
        base_cap, extra = divmod(signal_cap, len(normalized))
        while work["group_cursor"] < len(normalized):
            if clock() - started >= budget_seconds:
                return _result(work, reason="budget")
            index = work["group_cursor"]
            lane, chain = normalized[index]
            cap = base_cap + int(index < extra)
            rows = reader.rows(
                "SELECT rowid AS source_rowid,signal_id,lane,chain,token,created_ms,wallets_json,payload_json "
                "FROM signals WHERE rowid IN (SELECT MIN(rowid) FROM signals WHERE lane=? AND chain=? "
                "AND created_ms>=? AND created_ms<=? AND rowid<=? "
                "GROUP BY token,created_ms/86400000) ORDER BY created_ms DESC,rowid LIMIT ?",
                (lane, chain, work["window_start_ms"], work["cutoff_ms"], work["source_rowid_frontier"], cap),
            ) if cap else []
            if any(int(row["created_ms"]) < horizon for row in rows):
                return _result(work, error="frozen_discovery_roster_expired_by_retention")
            if any(int(row["created_ms"]) - sa.PRE_WINDOW_MS < now_ms - 13 * DAY_MS for row in rows):
                return _result(work, error="frozen_discovery_history_exceeds_13_days")
            work["roster"].extend(_roster_row(row) for row in rows)
            work["group_cursor"] += 1
            _save(path, work)
        if clock() - started >= budget_seconds:
            return _result(work, reason="budget")
        if not _roster_valid(reader, work):
            return _result(work, error="frozen_discovery_signal_deleted_or_changed")
        policies = [*ss.declared_policies(ladder), *sc.exploration_policies(ladder)]
        collector = ss.PathCollector(policies)
        while work["cursor"] < len(work["roster"]):
            if clock() - started >= budget_seconds:
                return _result(work, reason="budget")
            if not _history_valid(work, now_ms):
                return _result(work, error="frozen_discovery_history_exceeds_13_days")
            row = work["roster"][work["cursor"]]
            signal_key = str(row["rowid"])
            progress: dict[str, Any] = {"rows_read": 0, "payload_bytes": 0}
            try:
                tape = _tape(reader, row, work["tape_frontier"], deadline=started + budget_seconds,
                             clock=clock, progress=progress)
            except SourceDeferred as exc:
                if not str(exc).startswith("wal_over_limit:"):
                    previous = work["tape_failures"].get(signal_key, {})
                    advanced = progress["rows_read"] > int(previous.get("rows_read", 0))
                    stalled = 1 if advanced else int(previous.get("stalled_attempts", 0)) + 1
                    work["tape_failures"][signal_key] = {**progress, "reason": str(exc),
                                                        "stalled_attempts": stalled}
                    _save(path, work)
                    if stalled >= MAX_TAPE_ATTEMPTS:
                        return _result(work, error="frozen_discovery_exact_tape_stalled_after_3_attempts")
                raise
            except _TapeLimit as exc:
                work["tape_failures"][signal_key] = {**progress, "reason": str(exc), "resource_limit": True}
                _save(path, work)
                return _result(work, error=str(exc))
            case = sa.build_case(lane=row["lane"], chain=row["chain"], token=row["token"], t0=row["t0"],
                                 rows=tape, wallets=row["wallets"], payload=row["features"], ladder=ladder,
                                 kol=set(), trusted=set(), grades_now={}, grades_pit=None)
            if case is None:
                work["no_tape"] += 1
            else:
                case.features = {k: v for k, v in case.features.items() if k in sa.DOSSIER_KEYS and v is not None}
                collector.raw.clear()
                collector(case, tape)
                observations = ss.observations([case], collector.raw, settled_before_ms=work["cutoff_ms"])
                if observations and all(math.isfinite(v) for v in observations[0].raw.values()):
                    work["observations"].append(dataclasses.asdict(observations[0]))
                else:
                    work["invalid_prices"] += 1
            work["cursor"] += 1
            _save(path, work)  # each completed signal survives a hard stop
            del tape
        if clock() - started >= budget_seconds:
            return _result(work, reason="budget")
        if not _history_valid(work, now_ms):
            return _result(work, error="frozen_discovery_history_exceeds_13_days")
        # Recheck retention/source membership before releasing a completed sample.
        # A late guard deferral still leaves all priced progress resumable on disk.
        final_horizon = _retention(reader)
        if work["roster"] and final_horizon > min(row["t0"] for row in work["roster"]):
            return _result(work, error="frozen_discovery_roster_expired_by_retention")
        if not _roster_valid(reader, work):
            return _result(work, error="frozen_discovery_signal_deleted_or_changed")
        return _result(work)
    except SourceDeferred as exc:
        return _result(work, reason="source_deferred:" + str(exc))
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return _result(work, error="discovery_input_invalid:" + str(exc)[:120])


__all__ = ["collect", "VERSION", "MAX_WORK_BYTES"]
