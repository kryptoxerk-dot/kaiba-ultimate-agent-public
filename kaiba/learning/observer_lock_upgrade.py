"""Atomic complete-PID lock publication for the unchanged budgeted observer.

Codex OBSERVER-LOCK-UPGRADE-20261018. Explicit installation is required. Neither
an existing lock nor an abandoned staging file is reclaimed by this worker.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import threading
from pathlib import Path

from kaiba.core.config import get_settings
from kaiba.learning import observer_upgrade as original
from kaiba.learning import prospective_quotes as base

VERSION = "observer-lock-upgrade/1"
POLICY = "atomiclock/1"
MANIFEST_NAME = "atomic-lock-upgrade.json"
STAGE_NAME = ".atomic-lock-stage"
MAX_MANIFEST_BYTES = 32_768
MAX_SOURCE_BYTES = 1_048_576


def _require(ok, reason):
    if not ok:
        raise ValueError("observer_lock_upgrade:" + reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _object(raw, cap):
    _require(type(raw) is bytes and 0 < len(raw) <= cap, "artifact_byte_cap")
    result = json.loads(raw, object_pairs_hook=original._pairs, parse_constant=original._bad_number)
    _require(type(result) is dict, "artifact_object_required")
    return result


def source_bindings():
    path = base._safe(Path(__file__), Path(__file__).resolve().parents[1])
    _require(path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES, "source_byte_cap")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    def fingerprint(s):
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink
    _require(len(raw) <= MAX_SOURCE_BYTES and fingerprint(before) == fingerprint(after)
             == fingerprint(current) and before.st_ctime_ns == after.st_ctime_ns
             # Windows fstat exposes change time whereas path.stat exposes birth
             # time as ctime; only compare those two APIs on POSIX.
             and (os.name == "nt" or after.st_ctime_ns == current.st_ctime_ns)
             and all(stat.S_ISREG(s.st_mode) and s.st_nlink == 1
                 for s in (before, after, current)), "source_changed_during_read")
    return {"learning/observer_lock_upgrade.py": _sha(raw)}


def _original(raw):
    manifest = _object(raw, original.DECLARATION_CAP)
    expected = {"version": original.VERSION, "policy": original.quote_budget.POLICY,
        "base_version": base.VERSION, "base_policy": base.POLICY,
        "parent_cap": original.PARENT_CAP, "observer_state_cap": base.MAX_JSON_BYTES,
        "allocation_snapshot_token": manifest.get("allocation_snapshot_token"),
        "source_bindings": original.source_bindings(), "funding_allowed": False}
    token = manifest.get("allocation_snapshot_token")
    _require(manifest == expected and set(manifest) == set(expected)
             and manifest.get("funding_allowed") is False, "original_manifest_changed")
    _require(token is None or (type(token) is str and len(token) == 64
             and all(c in "0123456789abcdef" for c in token)), "original_snapshot_invalid")
    return manifest


def prepare_manifest(original_manifest_raw, *, prepared_ms):
    """Return an installer recipe; never writes, resets or examines day counters."""
    _require(type(prepared_ms) is int and prepared_ms > 0, "prepared_clock")
    old = _original(original_manifest_raw)
    manifest = {"version": VERSION, "policy": POLICY, "prepared_ms": prepared_ms,
        "original_manifest_sha256": _sha(original_manifest_raw),
        "original_source_bindings": copy.deepcopy(old["source_bindings"]),
        "source_bindings": source_bindings(), "lock_reclaim": False, "funding_allowed": False}
    manifest["manifest_sha256"] = _sha(_encoded(manifest))
    _require(len(_encoded(manifest)) <= MAX_MANIFEST_BYTES, "manifest_byte_cap")
    return manifest


def _fsync_directory(path):
    # Windows has no portable directory-fsync API. POSIX production requires it.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        _require(stat.S_ISDIR(os.fstat(fd).st_mode), "directory_required")
        os.fsync(fd)
    finally:
        os.close(fd)


class LockSafeQuoteCollector(original.BudgetedQuoteCollector):
    """Publish one complete original PID payload before inherited work can begin."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock_manifest_path = self.state_dir / MANIFEST_NAME
        self._lock_manifest = None
        self._lock_identity = None
        self._lock_raw = None
        self._lock_fd = None
        self._once_active = False
        self._cycle_lock = threading.Lock()

    def _lock_declaration(self):
        _, declared = original._read(self.lock_manifest_path, self.root, MAX_MANIFEST_BYTES)
        old_raw, _ = original._read(self.manifest_path, self.root, original.DECLARATION_CAP)
        prepared = declared.get("prepared_ms")
        _require(type(prepared) is int and 0 < prepared <= self._now(), "prepared_clock")
        expected = prepare_manifest(old_raw, prepared_ms=prepared)
        _require(declared == expected and set(declared) == set(expected)
                 and declared.get("funding_allowed") is False and declared.get("lock_reclaim") is False,
                 "manifest_or_source_changed")
        if self._lock_manifest is not None:
            _require(declared == self._lock_manifest, "manifest_changed_during_cycle")
        self._lock_manifest = copy.deepcopy(declared)
        return declared

    def _matches(self, path, links):
        if self._lock_identity is None or self._lock_raw is None:
            return False
        try:
            base._safe(path, self.root)
            before_path = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before_path.st_mode):
                return False
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(len(self._lock_raw) + 1)
                after = os.fstat(stream.fileno())
            current = path.stat(follow_symlinks=False)
            held = () if self._lock_fd is None else (os.fstat(self._lock_fd),)
            return raw == self._lock_raw and all(
                stat.S_ISREG(s.st_mode) and (s.st_dev, s.st_ino) == self._lock_identity
                and s.st_nlink in links for s in (before_path, before, after, current, *held))
        except (OSError, ValueError):
            return False

    def _owns_lock(self):
        _require(self._matches(self.state_dir / "worker.lock", {1}), "worker_lock_changed")

    def _declaration(self):
        self._owns_lock()
        self._lock_declaration()
        return super()._declaration()

    def _load_targets(self, now):
        result = super()._load_targets(now)
        # The inherited provider boundary reloads targets immediately before dispatch.
        self._owns_lock()
        self._lock_declaration()
        return result

    def _remove_owned(self, path, links):
        if self._matches(path, links):
            path.unlink()
            return True
        return False

    @staticmethod
    def _locked():
        return {"version": VERSION, "status": "locked", "provider_attempts": 0, "funding_allowed": False}

    def once(self):
        # A reentrant call must not clear the outer cycle's inode pin/cleanup state.
        if not self._cycle_lock.acquire(blocking=False):
            return self._locked()
        self._once_active = True
        try:
            return self._locked_once()
        finally:
            self._once_active = False
            self._cycle_lock.release()

    def _locked_once(self):
        lock, stage = self.state_dir / "worker.lock", self.state_dir / STAGE_NAME
        self._lock_manifest, self._lock_identity, self._lock_raw = None, None, None
        self._lock_fd = None
        # Includes malformed/dead/empty locks, symlinks and one bounded orphan stage.
        if os.path.lexists(lock) or os.path.lexists(stage):
            return self._locked()
        published = False
        report = {"version": VERSION, "status": "partial", "reason": "atomic_lock_evidence_invalid",
                  "provider_attempts": 0, "funding_allowed": False}
        try:
            self._lock_declaration()
            try:
                fd = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                report = self._locked()
                fd = None
            if fd is None:
                return report
            self._lock_raw = base._json({"pid": os.getpid()}).encode()
            with os.fdopen(fd, "wb") as stream:
                created = os.fstat(stream.fileno())
                self._lock_identity = created.st_dev, created.st_ino
                _require(stat.S_ISREG(created.st_mode) and created.st_nlink == 1, "private_stage_required")
                stream.write(self._lock_raw)
                stream.flush()
                os.fsync(stream.fileno())
                written = os.fstat(stream.fileno())
                _require((written.st_dev, written.st_ino) == self._lock_identity
                         and written.st_nlink == 1 and written.st_size == len(self._lock_raw), "stage_changed")
                # Pin the production inode until cleanup, preventing inode reuse after
                # an unrelated process removes/replaces the pathname. Windows CRT
                # handles prevent link deletion, so use fresh fd/path checks there.
                if os.name != "nt":
                    self._lock_fd = os.dup(stream.fileno())
            _require(self._matches(stage, {1}), "stage_changed_before_publication")
            try:
                os.link(stage, lock, follow_symlinks=False)
            except FileExistsError:
                report = self._locked()
                return report
            published = True
            _require(self._remove_owned(stage, {2}), "stage_changed_after_publication")
            _fsync_directory(self.state_dir)
            self._owns_lock()
            self._lock_declaration()
            # Do not invoke base.once: its O_EXCL/write/reclaim/cleanup is replaced.
            report = super()._once()
        except (ValueError, KeyError, TypeError, OSError):
            pass
        finally:
            try:
                changed = self._remove_owned(stage, {1, 2})
                if published:
                    changed = self._remove_owned(lock, {1}) or changed
                if changed:
                    _fsync_directory(self.state_dir)
            except OSError:
                report.update(status="partial", reason="atomic_lock_cleanup_unproved")
            finally:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                self._lock_manifest, self._lock_identity, self._lock_raw = None, None, None
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--research-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    root = get_settings().kaiba_data_dir / "learning"
    report = LockSafeQuoteCollector(args.research_dir or root / "autolearn",
        args.state_dir or root / "prospective-quotes", db_path=args.db).once()
    print(base._json({k: report.get(k) for k in ("status", "reason", "provider_attempts", "funding_allowed")}))
    return 1 if report.get("status") in ("partial", "unknown") else 0


if __name__ == "__main__":
    raise SystemExit(main())
