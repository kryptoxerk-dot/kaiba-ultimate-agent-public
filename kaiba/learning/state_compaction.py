"""One-shot, lossless storage repair for an oversized learner state JSON file.

Dry-run is the default. Applying requires the observed raw SHA256 and acquires
the learner's existing worker.lock without reclaiming it. No field, clock,
admission, evidence or history is removed. The exact original is retained.
This does not fix a writer that later pretty-prints the same oversized state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

VERSION = "learning-state-compaction/1"
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")


class CompactionRefused(ValueError):
    """No new repair may proceed; existing durable artifacts are retained."""


def _require(value: bool, reason: str) -> None:
    if not value:
        raise CompactionRefused(reason)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe(path: Path) -> Path:
    absolute = path.absolute()
    _require(not any(p.is_symlink() for p in (absolute, *absolute.parents)), "symlink_refused")
    return absolute


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read(path: Path, cap: int) -> tuple[bytes, os.stat_result]:
    _safe(path)
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "regular_single_link_file_required")
        raw = handle.read(cap + 1)
        after = os.fstat(handle.fileno())
    _require(len(raw) <= cap, "input_byte_cap")
    current = path.stat(follow_symlinks=False)
    _require(_identity(before) == _identity(after) == _identity(current)
             and len(raw) == before.st_size, "source_changed_during_read")
    return raw, current


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        _require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def _constant(_value: str) -> None:
    raise CompactionRefused("nonfinite_json_number")


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        _require(isinstance(value, dict), "state_object_required")
        # Reject finite-looking exponent overflow too, e.g. 1e999 -> float inf.
        json.dumps(value, allow_nan=False)
    except (UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise CompactionRefused("invalid_state_json") from exc
    except ValueError as exc:
        if isinstance(exc, CompactionRefused):
            raise
        raise CompactionRefused("nonfinite_json_number") from exc
    return value


def _compact(raw: bytes) -> bytes:
    original = _parse(raw)
    try:
        compact = (json.dumps(original, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    except (ValueError, RecursionError, OverflowError) as exc:
        raise CompactionRefused("state_cannot_be_serialized") from exc
    _require(len(compact) < MAX_OUTPUT_BYTES, "compacted_state_still_at_or_above_cap")
    _require(_parse(compact) == original, "parsed_state_changed")
    _require(len(compact) < len(raw), "no_size_reduction")
    return compact


def _sync_dir(path: Path) -> None:
    if os.name == "nt":
        return  # Actual deployment is Linux; Windows fixtures still fsync every file.
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_once(path: Path, raw: bytes, mode: int = 0o600) -> None:
    _safe(path)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _owns_lock(path: Path, raw: bytes, inode: tuple[int, int]) -> bool:
    try:
        observed, info = _read(path, len(raw))
        return observed == raw and (info.st_dev, info.st_ino) == inode
    except (OSError, CompactionRefused):
        return False


def _source_cas(path: Path, original: bytes, identity: os.stat_result) -> None:
    current, info = _read(path, MAX_INPUT_BYTES)
    _require(current == original and _identity(info) == _identity(identity), "source_CAS_conflict")


def compact_state(state_dir: Path | str, *, expected_sha256: str | None = None,
                  apply: bool = False, clock=time.time) -> dict[str, Any]:
    """Inspect or repair exactly state_dir/state.json; all repair evidence is private.

    The exclusive worker lock protects cooperative writers. Raw content and file
    identity are checked again immediately before replacement. Non-cooperating
    external writers must not mutate this directory during an operator repair.
    """
    root = _safe(Path(state_dir)).resolve()
    _require(root.is_dir(), "state_directory_required")
    source = _safe(root / "state.json")
    _require(expected_sha256 is None or (isinstance(expected_sha256, str)
             and SHA256.fullmatch(expected_sha256) is not None), "invalid_expected_sha256")
    _require(not apply or expected_sha256 is not None, "apply_requires_expected_sha256")
    raw, source_info = _read(source, MAX_INPUT_BYTES)
    original_hash = _sha(raw)
    _require(expected_sha256 is None or original_hash == expected_sha256, "observed_sha256_changed")
    compact = _compact(raw)
    report = {
        "version": VERSION, "status": "dry_run", "original_sha256": original_hash,
        "compacted_sha256": _sha(compact), "original_bytes": len(raw), "compacted_bytes": len(compact),
        "saved_bytes": len(raw) - len(compact), "parsed_equal": True, "history_removed": False,
        "provider_calls": 0, "database_queries": 0, "funding_allowed": False,
        "recurrence_risk": "The current worker may pretty-print state above the reader cap again",
    }
    _source_cas(source, raw, source_info)
    if not apply:
        return report
    lock = _safe(root / "worker.lock")
    now = int(clock() * 1000)
    _require(now > 0, "invalid_clock")
    lock_raw = _encoded({"pid": os.getpid(), "started_ms": now, "owner": VERSION})
    try:
        _write_once(lock, lock_raw)
    except FileExistsError as exc:
        raise CompactionRefused("worker_lock_occupied_no_reclaim") from exc
    lock_info = lock.stat(follow_symlinks=False)
    lock_inode = (lock_info.st_dev, lock_info.st_ino)
    temporary: Path | None = None
    replaced = False
    try:
        _require(_owns_lock(lock, lock_raw, lock_inode), "worker_lock_changed")
        _source_cas(source, raw, source_info)
        parent = _safe(root / "state-compaction")
        if parent.exists():
            _require(parent.is_dir(), "artifact_parent_not_directory")
        else:
            parent.mkdir(mode=0o700)
            _sync_dir(root)
        artifacts = _safe(parent / original_hash)
        try:
            artifacts.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise CompactionRefused("repair_artifacts_occupied_no_overwrite") from exc
        _sync_dir(parent)
        backup = artifacts / "state.original.json"
        receipt_path = artifacts / "receipt.json"
        _write_once(backup, raw)
        backed, _ = _read(backup, MAX_INPUT_BYTES)
        _require(backed == raw, "backup_verification_failed")
        receipt = {**report, "status": "prepared", "prepared_ms": now,
                   "backup_file": "state.original.json", "source_file": "state.json",
                   "source_mode": stat.S_IMODE(source_info.st_mode)}
        receipt_raw = _encoded(receipt)
        _write_once(receipt_path, receipt_raw)
        _sync_dir(artifacts)
        temporary = root / ("state.json.compact-" + original_hash + ".partial")
        _write_once(temporary, compact, stat.S_IMODE(source_info.st_mode))
        if os.name != "nt":
            os.chmod(temporary, stat.S_IMODE(source_info.st_mode))
        _require(_owns_lock(lock, lock_raw, lock_inode), "worker_lock_changed")
        _source_cas(source, raw, source_info)
        os.replace(temporary, source)
        replaced = True
        temporary = None
        _sync_dir(root)
        actual, _ = _read(source, MAX_OUTPUT_BYTES)
        _require(actual == compact and _parse(actual) == _parse(backed), "replaced_state_verification_failed")
        _require(_owns_lock(lock, lock_raw, lock_inode), "worker_lock_changed_after_replace")
        existing_receipt, _ = _read(receipt_path, 16384)
        _require(existing_receipt == receipt_raw, "receipt_CAS_conflict_after_replace")
        receipt.update(status="committed", completed_ms=int(clock() * 1000))
        receipt_temp = artifacts / "receipt.committed.partial"
        _write_once(receipt_temp, _encoded(receipt))
        os.replace(receipt_temp, receipt_path)
        _sync_dir(artifacts)
        return {**receipt, "artifact_directory": str(artifacts)}
    except Exception as exc:
        if replaced:
            raise CompactionRefused("state_replaced_but_finalization_failed; preserve_backup_and_prepared_receipt") from exc
        raise
    finally:
        # Keep failed preparation artifacts and any partial file for diagnosis.
        # Only the exact lock created by this process may be released.
        if _owns_lock(lock, lock_raw, lock_inode):
            lock.unlink()
            _sync_dir(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--apply", action="store_true", help="apply reviewed lossless repair; otherwise dry-run")
    args = parser.parse_args(argv)
    try:
        result = compact_state(args.state_dir, expected_sha256=args.expected_sha256, apply=args.apply)
    except (CompactionRefused, OSError) as exc:
        print(json.dumps({"version": VERSION, "status": "refused", "reason": str(exc), "funding_allowed": False}))
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
