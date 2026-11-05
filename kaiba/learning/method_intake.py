"""Preserve Grok method evidence for research; no network, canonical DB or execution.

This registry freezes what was RECEIVED, not an executable experiment. Even a
complete author description needs formalization and the existing campaign gates.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from kaiba.hunters import grok_alpha_bridge
from kaiba.hunters.grok_alpha_bridge import normalize
from kaiba.learning import paper_progress

VERSION = "method-evidence/2"
MAX_BYTES = 8 * 1024 * 1024
MAX_METHODS = 256
MAX_RECEIPTS = 32
PAGE_ROWS = 128
PAGE_BYTES = 1024 * 1024
READER_VERSION = "method-intake-reader/3"
MAX_REJECTIONS = 4096
MIN_FILESYSTEM_FREE = 5 * 1024**3
FILESYSTEM_HALT = 0.92
# Include the whole normalizer module, including its schema/helper constants.
# A changed source version is an explicit migration task, never new authority.
NORMALIZER_VERSION = hashlib.sha256(Path(grok_alpha_bridge.__file__).read_bytes()).hexdigest()
REQUIRED = ("entry", "invalidation", "exit", "timeframe", "holding_period", "costs", "delay", "required_data")
RULE_FIELDS = (*REQUIRED, "category", "cooldown", "failure_conditions")
AUTHORITY = {"financial_execution": False, "auto_promotion": False, "source_claims_verified": False}
ABSENT_TEXT = re.compile(
    r"^(?:unknown|unspecified|n/a|none|not stated|not provided|not specified|not disclosed|unavailable|tbd)"
    r"(?:[.!?]?$|[;:]\s)",
    re.IGNORECASE,
)


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def _present(value: Any) -> bool:
    if isinstance(value, str):
        stripped = value.strip()
        return bool(stripped) and ABSENT_TEXT.match(stripped) is None
    return isinstance(value, (dict, list)) and bool(value)


def assess(row: dict, *, check_identity: bool = True) -> dict | None:
    """No inference from claim text. Originals and author claims remain evidence."""
    card = json.loads(row["payload"])
    if not isinstance(card, dict):
        raise ValueError("card_object_required")
    state = card.get("state")
    if not isinstance(state, dict) or state.get("opportunity_type") != "trading_method":
        return None
    if (
        card.get("track") != "flow"
        or card.get("contract") is not None
        or state.get("research_only") is not True
    ):
        raise ValueError("method_scope_invalid")
    key, digest = row["key"], row["digest"]
    if check_identity:
        actual_key, actual_digest, _, _ = normalize(card, now_ms=row["received_ms"])
        if actual_key != key or actual_digest != digest:
            raise ValueError("intake_identity_mismatch")
    method = state.get("method_hypothesis")
    if not isinstance(method, dict) or not method:
        raise ValueError("method_object_missing")
    # Exclude publisher-specific ids, evidence links and claimed profits from the
    # rules identity. Repeated shills cannot multiply a strategy or its evidence.
    rules = {k: method.get(k) for k in RULE_FIELDS}
    missing = [field for field in REQUIRED if not _present(method.get(field))]
    missing_clocks = ["observed_at"] if card.get("observed_at") is None else []
    for index, source in enumerate(card["sources"]):
        missing_clocks.extend(
            f"sources[{index}].{field}"
            for field in ("published_at", "retrieved_at")
            if source.get(field) is None
        )
    if missing:
        action = "resolve_missing_source_rules"
    elif missing_clocks:
        action = "resolve_missing_source_clocks"
    else:
        action = "preregister_existing_paper_campaign"
    return {
        "method_id": fingerprint(rules),
        "rules_hash": fingerprint(rules),
        "source_method": method,
        "missing_fields": missing,
        "missing_evidence_clocks": missing_clocks,
        "research_status": "needs_source_details" if missing or missing_clocks else "needs_formalization",
        "next_action": action,
        "receipt": {
            "observation_id": key,
            "revision": digest,
            "received_ms": row["received_ms"],
            "observation": card,
        },
        **AUTHORITY,
    }


def update(previous: dict, rows: list[dict], *, now_ms: int) -> tuple[dict, dict]:
    """Append evidence without relabeling incomplete ideas as pass/ready/profitable."""
    if isinstance(previous, dict) and previous.get("version") == "method-evidence/1":
        previous = migrate_v1(previous)
    if (
        not isinstance(previous, dict)
        or previous.get("version") != VERSION
        or not isinstance(previous.get("methods"), dict)
    ):
        raise ValueError("method_checkpoint_invalid")
    state = json.loads(encoded(previous))
    methods = state["methods"]
    legacy_validation = "receipt_validation" not in previous
    stamps = state.setdefault("receipt_validation", {})
    if not isinstance(stamps, dict) or len(stamps) > MAX_METHODS * MAX_RECEIPTS:
        raise ValueError("receipt_validation_invalid")
    migrations = []
    rejections = state.setdefault("rejections", {})
    if not isinstance(rejections, dict) or len(rejections) > MAX_REJECTIONS:
        raise ValueError("rejection_checkpoint_invalid")
    for ident, reject in rejections.items():
        if not isinstance(reject, dict) or fingerprint(reject.get("identity")) != ident:
            raise ValueError("rejection_identity_invalid")
    if len(methods) > MAX_METHODS:
        raise ValueError("method_checkpoint_capacity")
    for mid, record in methods.items():
        if (
            not isinstance(record, dict)
            or record.get("method_id") != mid
            or record.get("rules_hash") != mid
            or not isinstance(record.get("receipts"), list)
            or not 1 <= len(record["receipts"]) <= MAX_RECEIPTS
        ):
            raise ValueError("method_checkpoint_record_invalid")
        # A stored receipt was validated under a particular source version. Keep
        # its immutable hash and provenance if a future normalizer changes; do not
        # relabel it as currently validated or stop all later intake.
        for receipt in record["receipts"]:
            stamp_id = fingerprint([receipt["observation_id"], receipt["revision"], receipt["received_ms"]])
            stamp = stamps.get(stamp_id)
            if stamp is None and not legacy_validation:
                raise ValueError("receipt_validation_missing")
            if stamp is not None and (
                not isinstance(stamp, dict)
                or set(stamp) != {"normalizer_version", "receipt_sha256"}
                or not isinstance(stamp["normalizer_version"], str)
                or re.fullmatch(r"[a-f0-9]{64}", stamp["normalizer_version"]) is None
                or stamp["receipt_sha256"] != fingerprint(receipt)
            ):
                raise ValueError("receipt_validation_hash_mismatch")
            current = stamp is None or stamp["normalizer_version"] == NORMALIZER_VERSION
            checked = assess(
                {
                    "key": receipt["observation_id"],
                    "digest": receipt["revision"],
                    "received_ms": receipt["received_ms"],
                    "payload": json.dumps(receipt["observation"]),
                },
                check_identity=current,
            )
            if checked is None or checked["method_id"] != mid:
                raise ValueError("method_checkpoint_evidence_invalid")
            if stamp is None:
                stamps[stamp_id] = {
                    "normalizer_version": NORMALIZER_VERSION,
                    "receipt_sha256": fingerprint(receipt),
                }
            elif not current:
                migrations.append(
                    {
                        "method_id": mid,
                        "receipt_id": stamp_id,
                        "from_version": stamp["normalizer_version"],
                        "to_version": NORMALIZER_VERSION,
                    }
                )
        record.update(
            {
                k: checked[k]
                for k in (
                    "source_method",
                    "missing_fields",
                    "missing_evidence_clocks",
                    "research_status",
                    "next_action",
                )
            }
        )
        record.update(AUTHORITY)
    errors = collections.Counter()
    deferred_reasons = collections.Counter()
    new_methods = new_receipts = duplicates = deferred = nonmethods = quarantined = 0
    for row in sorted(
        rows,
        key=lambda r: (
            r["received_ms"] if type(r.get("received_ms")) is int else 0,
            str(r.get("key")),
            str(r.get("digest")),
        ),
    ):
        try:
            if type(row["received_ms"]) is not int or not 0 < row["received_ms"] <= now_ms + 5000:
                raise ValueError("receipt_clock_invalid")
            item = assess(row)
        except (ValueError, TypeError, KeyError, OverflowError):
            errors["invalid_method_or_receipt"] += 1
            identity = {
                "key": row.get("key"),
                "digest": row.get("digest"),
                "received_ms": row.get("received_ms"),
                "payload_sha256": hashlib.sha256(str(row.get("payload")).encode()).hexdigest(),
            }
            ident = fingerprint(identity)
            if ident not in rejections:
                if len(rejections) >= MAX_REJECTIONS:
                    deferred += 1
                    deferred_reasons["rejection_capacity"] += 1
                    continue
                rejections[ident] = {
                    "identity": identity,
                    "first_seen_ms": now_ms,
                    "reason": "invalid_method_or_receipt",
                    "payload_retained": False,
                }
                quarantined += 1
            continue
        if item is None:
            nonmethods += 1
            continue
        mid = item["method_id"]
        if mid not in methods:
            if len(methods) >= MAX_METHODS:
                deferred += 1
                deferred_reasons["method_capacity"] += 1
                continue
            methods[mid] = {k: v for k, v in item.items() if k != "receipt"}
            methods[mid].update(
                {"registered_ms": now_ms, "first_received_ms": item["receipt"]["received_ms"], "receipts": []}
            )
            new_methods += 1
        record = methods[mid]
        receipt = item["receipt"]
        identity = receipt["observation_id"], receipt["revision"]
        if any((r["observation_id"], r["revision"]) == identity for r in record["receipts"]):
            duplicates += 1
        elif len(record["receipts"]) >= MAX_RECEIPTS:
            deferred += 1
            deferred_reasons["receipt_capacity"] += 1
        else:
            record["receipts"].append(receipt)
            stamp_id = fingerprint([receipt["observation_id"], receipt["revision"], receipt["received_ms"]])
            stamps[stamp_id] = {
                "normalizer_version": NORMALIZER_VERSION,
                "receipt_sha256": fingerprint(receipt),
            }
            record["first_received_ms"] = min(record["first_received_ms"], receipt["received_ms"])
            new_receipts += 1
    state["observed_ms"] = now_ms
    if len(encoded(state)) > MAX_BYTES:
        raise ValueError("method_checkpoint_byte_budget")
    counts = collections.Counter(m["research_status"] for m in methods.values())
    report = {
        "version": VERSION,
        "observed_ms": now_ms,
        "methods": len(methods),
        "status_counts": dict(counts),
        "new_methods": new_methods,
        "new_receipts": new_receipts,
        "duplicates": duplicates,
        "nonmethods": nonmethods,
        "deferred": deferred,
        "deferred_reasons": dict(deferred_reasons),
        "invalid_rows": dict(errors),
        "new_rejections": quarantined,
        "retained_rejections": len(rejections),
        "receipt_migration_tasks": migrations,
        "invalid_rows_scope": "this page, including already-indexed rejections on replay",
        "provider_calls": 0,
        "input_rows": len(rows),
        "latest_input_received_ms": max(
            (
                r["received_ms"]
                for r in rows
                if type(r.get("received_ms")) is int and 0 < r["received_ms"] <= now_ms + 5000
            ),
            default=None,
        ),
        "latest_method_received_ms": max(
            (r["received_ms"] for m in methods.values() for r in m["receipts"]), default=None
        ),
        **AUTHORITY,
        "tasks": [
            {
                k: m[k]
                for k in (
                    "method_id",
                    "registered_ms",
                    "first_received_ms",
                    "missing_fields",
                    "missing_evidence_clocks",
                    "research_status",
                    "next_action",
                )
            }
            for m in sorted(methods.values(), key=lambda m: (m["registered_ms"], m["method_id"]))
        ],
        "note": "Source descriptions only; no experiment registered, no edge established. Retain failed trials in existing campaign ledger.",
    }
    return state, report


def migrate_v1(previous: dict) -> dict:
    """Keep first-deploy clocks, receipts and legacy ids when separating metadata."""
    if not isinstance(previous.get("methods"), dict) or len(previous["methods"]) > MAX_METHODS:
        raise ValueError("legacy_checkpoint_invalid")
    methods = {}
    for old_id, old in previous["methods"].items():
        receipts = old.get("receipts") if isinstance(old, dict) else None
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= MAX_RECEIPTS:
            raise ValueError("legacy_receipts_invalid")
        for receipt in receipts:
            checked = assess(
                {
                    "key": receipt["observation_id"],
                    "digest": receipt["revision"],
                    "received_ms": receipt["received_ms"],
                    "payload": json.dumps(receipt["observation"]),
                }
            )
            if checked is None:
                raise ValueError("legacy_method_invalid")
            raw = checked["source_method"]
            legacy = fingerprint(
                {k: v for k, v in raw.items() if k not in {"method_id", "permalinks", "claimed_performance"}}
            )
            if legacy != old_id or old.get("method_id") != old_id or old.get("rules_hash") != old_id:
                raise ValueError("legacy_identity_invalid")
            mid = checked["method_id"]
            if mid not in methods:
                methods[mid] = {k: v for k, v in checked.items() if k != "receipt"}
                methods[mid].update(
                    {
                        "registered_ms": old["registered_ms"],
                        "first_received_ms": old["first_received_ms"],
                        "receipts": [],
                        "legacy_method_ids": [],
                    }
                )
            dest = methods[mid]
            if old_id not in dest["legacy_method_ids"]:
                dest["legacy_method_ids"].append(old_id)
            dest["registered_ms"] = min(dest["registered_ms"], old["registered_ms"])
            dest["first_received_ms"] = min(dest["first_received_ms"], receipt["received_ms"])
            if receipt not in dest["receipts"]:
                if len(dest["receipts"]) >= MAX_RECEIPTS:
                    raise ValueError("legacy_merge_receipt_capacity")
                dest["receipts"].append(receipt)
    return {
        **previous,
        "version": VERSION,
        "methods": methods,
        "migration": "v1_metadata_rules_separated; legacy ids and clocks retained",
    }


def _safe(path: Path) -> None:
    if path.absolute() != path.resolve() or path.is_symlink():
        raise ValueError("symlink_path_refused")


def read_intake(path: Path, anchor: dict | None = None) -> dict:
    _safe(path)
    if path.name != "inbox.db" or path.parent.name != "grok-alpha":
        raise ValueError("only_grok_intake_allowed")
    if not path.is_file() or path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("intake_unavailable_or_oversize")
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 1_500_000_000:
        raise ValueError("wal_admission_deferred")
    if anchor is not None and (
        not isinstance(anchor, dict)
        or set(anchor) != {"rowid", "key", "digest", "received_ms"}
        or type(anchor.get("rowid")) is not int
        or anchor["rowid"] <= 0
        or type(anchor.get("received_ms")) is not int
        or any(not isinstance(anchor.get(k), str) or len(anchor[k]) > PAGE_BYTES for k in ("key", "digest"))
    ):
        raise ValueError("intake_cursor_invalid")
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15, isolation_level=None)
    c.row_factory = sqlite3.Row
    deadline = time.monotonic() + 3
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 500)
    try:
        c.execute("PRAGMA query_only=ON")
        a = anchor or {"rowid": 0, "key": "", "digest": "", "received_ms": 0}
        cursor = c.execute(
            "WITH anchor AS (SELECT 1 FROM revisions WHERE rowid=? AND key=? AND digest=? AND received_ms=?) "
            "SELECT 0 kind,0 rid,NULL key,NULL digest,NULL received_ms,NULL payload,EXISTS(SELECT 1 FROM anchor) valid "
            "UNION ALL SELECT 1 kind,rowid rid,key,digest,received_ms,payload,EXISTS(SELECT 1 FROM anchor) valid FROM "
            "(SELECT rowid,key,digest,received_ms,payload FROM revisions WHERE rowid>"
            "CASE WHEN EXISTS(SELECT 1 FROM anchor) THEN ? ELSE 0 END ORDER BY rowid LIMIT ?) ORDER BY kind,rid",
            (a["rowid"], a["key"], a["digest"], a["received_ms"], a["rowid"], PAGE_ROWS + 1),
        )
        rows, size, more, valid = [], 0, False, False
        for row in cursor:
            if row["kind"] == 0:
                valid = bool(row["valid"])
                continue
            if not isinstance(row["payload"], str):
                raise ValueError("intake_payload_invalid")
            length = len(encoded({k: row[k] for k in ("key", "digest", "received_ms", "payload")}))
            if length > PAGE_BYTES:
                raise ValueError("intake_payload_exceeds_page")
            if len(rows) >= PAGE_ROWS or size + length > PAGE_BYTES:
                more = True
                break
            size += length
            rows.append({k: row[k] for k in ("key", "digest", "received_ms", "payload")})
            next_anchor = {
                "rowid": row["rid"],
                "key": row["key"],
                "digest": row["digest"],
                "received_ms": row["received_ms"],
            }
        return {
            "rows": rows,
            "bytes": size,
            "has_more": more,
            "anchor_reset": anchor is not None and not valid,
            "next_anchor": next_anchor if rows else (anchor if valid else None),
        }
    finally:
        c.close()


def atomic(path: Path, value: dict) -> None:
    _safe(path)
    data = encoded(value)
    if len(data) > MAX_BYTES:
        raise ValueError("artifact_byte_budget")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".method-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def worker_lock(path: Path):
    """Kernel releases ownership even after SIGKILL; the file itself is persistent."""
    _safe(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def run(intake: Path, directory: Path, *, now_ms: int) -> dict:
    _safe(directory)
    if directory.resolve() == intake.parent.resolve() or directory.name != "method-lab":
        raise ValueError("separate_method_lab_directory_required")
    disk = shutil.disk_usage(directory.parent if directory.parent.exists() else Path.cwd())
    available = disk.used + disk.free
    # This worker only reads delivered text and atomically writes bounded JSON.
    # Reserve uses filesystem availability, never reusable pages inside kaiba.db.
    # Global trading/disk policy and provider-collector admission are not changed.
    if available <= 0 or disk.used / available >= FILESYSTEM_HALT or disk.free < MIN_FILESYSTEM_FREE:
        raise ValueError("research_disk_capacity_deferred")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with worker_lock(directory / "worker.lock"):
        path = directory / "state.json"
        _safe(path)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            raise ValueError("method_checkpoint_byte_budget")
        previous = json.loads(path.read_bytes()) if path.exists() else {"version": VERSION, "methods": {}}
        if not isinstance(previous, dict) or previous.get("version") not in (VERSION, "method-evidence/1"):
            raise ValueError("method_checkpoint_invalid")
        page = read_intake(intake, previous.get("intake_cursor"))
        state, report = update(previous, page["rows"], now_ms=now_ms)
        blocked = bool(report["deferred"])
        if not blocked:
            state["intake_cursor"] = page["next_anchor"]
        cursor = state.get("intake_cursor")
        cursor_ms = cursor.get("received_ms") if cursor is not None else None
        # The cursor can point to a quarantined row. Report only a plausible
        # receiver timestamp, and never reuse it as a source observation clock.
        report["cursor_received_ms"] = (
            cursor_ms if type(cursor_ms) is int and 0 < cursor_ms <= now_ms else None
        )
        reader = {
            "version": READER_VERSION,
            "page_rows": len(page["rows"]),
            "page_bytes": page["bytes"],
            "has_more": page["has_more"],
            "anchor_reset": page["anchor_reset"],
            "blocked": blocked,
            "cursor_advanced": not blocked and bool(page["rows"]),
        }
        state["last_reader"] = report["reader"] = reader
        report["status"] = "blocked" if blocked else "ok"
        report["checkpoint_sha256"] = fingerprint(state)
        report["paper_progress"] = paper_progress.snapshot(directory.parent, now_ms=now_ms)
        atomic(path, state)
        atomic(directory / "latest.json", report)
        return report


def persist_unavailable(directory: Path, result: dict) -> bool:
    """Best-effort tiny status under the same lock; never alter the checkpoint.

    SIGKILL/OOM cannot run this handler; consumers must still check report age.
    Failed admission may write this small diagnostic if >=1 MiB is available.
    """
    try:
        _safe(directory)
        if directory.name != "method-lab" or not directory.is_dir():
            return False
        if shutil.disk_usage(directory).free < 1024 * 1024:
            return False
        with worker_lock(directory / "worker.lock"):
            latest = directory / "latest.json"
            _safe(latest)
            if latest.is_file() and latest.stat().st_size <= MAX_BYTES:
                try:
                    previous = json.loads(latest.read_bytes())
                except (ValueError, UnicodeError):
                    previous = {}
                if (
                    isinstance(previous, dict)
                    and type(previous.get("observed_ms")) is int
                    and previous["observed_ms"] > result["observed_ms"]
                ):
                    return False
            checkpoint = directory / "state.json"
            _safe(checkpoint)
            result["checkpoint_sha256"] = (
                hashlib.sha256(checkpoint.read_bytes()).hexdigest()
                if checkpoint.is_file() and checkpoint.stat().st_size <= MAX_BYTES
                else None
            )
            atomic(latest, result)
            return True
    except (OSError, ValueError, TypeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake", type=Path, default=Path("data/grok-alpha/inbox.db"))
    parser.add_argument("--directory", type=Path, default=Path("data/method-lab"))
    args = parser.parse_args()
    attempt_ms = int(time.time() * 1000)
    try:
        result = run(args.intake, args.directory, now_ms=attempt_ms)
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as error:
        reason = str(error)
        if not re.fullmatch(r"[a-z_]{1,100}", reason):
            reason = type(error).__name__
        result = {
            "version": VERSION,
            "observed_ms": attempt_ms,
            "status": "unavailable",
            "reason": reason,
            "tasks": [],
            **AUTHORITY,
        }
        persisted = persist_unavailable(args.directory, result)
        result["failure_report_persisted"] = persisted
        print(json.dumps(result))
        raise SystemExit(1) from None
    print(json.dumps({k: v for k, v in result.items() if k != "tasks"}))
    if result.get("reader", {}).get("blocked"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
