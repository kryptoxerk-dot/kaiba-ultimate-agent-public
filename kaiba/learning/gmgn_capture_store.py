"""Bounded immutable private bytes for the learning observer; no provider or DB IO.

The caller owns capsule validation and durable admission. Storage never authorizes
a dispatch, evicts evidence, repairs an ambiguous partial or reclaims a lock.
POSIX owner/mode checks are enforced; Windows ACL privacy remains the caller's
learning-root responsibility (chmod there does not implement a POSIX ACL).
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

MAX_FILES = 512
MAX_BYTES = 64 * 1024 * 1024
MAX_CAPSULE_BYTES = 128 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_ENTRY = re.compile(r"[0-9a-f]{64}\.(?:json|partial)")
_LOCK = ".store.lock"
_LOCK_PREFIX = b"gmgn-capture-store-lock/1:"
_LOCK_BYTES = len(_LOCK_PREFIX) + 32 + 1


class CaptureStoreError(RuntimeError):
    """Fixed safe reason; never includes capsule bytes or filesystem paths."""


def _require(value, reason):
    if not value:
        raise CaptureStoreError(reason)


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _identity(info):
    # Windows path/handle ctime can differ on an otherwise unchanged file.
    change_time = info.st_ctime_ns if os.name == "posix" else None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, change_time,
            info.st_mode, info.st_nlink, getattr(info, "st_uid", None))


def _owner(info, *, private):
    if os.name == "posix":
        _require(info.st_uid == os.getuid() and info.st_mode & (0o077 if private else 0o022) == 0,
                 "owner_or_permissions_refused")


class CaptureStore:
    def __init__(self, root, *, learning_root, max_files=MAX_FILES, max_bytes=MAX_BYTES,
                 max_capsule_bytes=MAX_CAPSULE_BYTES):
        for value, ceiling in ((max_files, MAX_FILES), (max_bytes, MAX_BYTES),
                               (max_capsule_bytes, MAX_CAPSULE_BYTES)):
            _require(type(value) is int and 0 < value <= ceiling, "store_limit_invalid")
        self.learning_root = Path(learning_root).absolute()
        self.root = Path(root).absolute()
        _require(".." not in self.root.parts and ".." not in self.learning_root.parts
                 and self.root != self.learning_root and self.root.is_relative_to(self.learning_root),
                 "store_path_refused")
        self.max_files, self.max_bytes, self.max_capsule_bytes = max_files, max_bytes, max_capsule_bytes
        try:
            self._paths(create=False, missing_ok=True)
        except OSError as exc:
            raise CaptureStoreError("store_path_refused") from exc

    def _paths(self, *, create=False, missing_ok=False):
        # Check lexical ancestors first: resolve alone would conceal a link/junction.
        for path in reversed((self.learning_root, *self.learning_root.parents)):
            info = path.lstat()
            _require(stat.S_ISDIR(info.st_mode) and not _linked(info), "store_path_refused")
        _owner(self.learning_root.lstat(), private=False)
        current = self.learning_root
        for part in self.root.relative_to(self.learning_root).parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if create:
                    parent_id = _identity(current.parent.lstat())[:2]
                    current.mkdir(mode=0o700)
                    info = current.lstat()
                    self._sync_parent(current.parent, parent_id)
                elif missing_ok:
                    return None
                else:
                    raise CaptureStoreError("store_missing") from None
            _require(stat.S_ISDIR(info.st_mode) and not _linked(info), "store_path_refused")
            _owner(info, private=current == self.root)
        _require(self.root.resolve() == self.root and self.learning_root.resolve() == self.learning_root,
                 "store_path_refused")
        return self.root.lstat()

    @staticmethod
    def _sync_parent(parent, expected):
        if os.name != "nt":
            fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                _require(_identity(os.fstat(fd))[:2] == expected
                         and _identity(parent.lstat())[:2] == expected, "store_directory_changed")
                os.fsync(fd)
                _require(_identity(parent.lstat())[:2] == expected, "store_directory_changed")
            finally:
                os.close(fd)

    def _inventory(self):
        root = self._paths()
        files = size = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                files += 1  # Count before collecting/processing; no unbounded list/glob.
                _require(files <= self.max_files, "store_file_budget")
                # Windows DirEntry.stat omits inode/link count; fresh lstat is required.
                info = Path(entry.path).lstat()
                _require(stat.S_ISREG(info.st_mode) and not _linked(info) and info.st_nlink == 1
                         and (entry.name == _LOCK or _ENTRY.fullmatch(entry.name)), "store_entry_refused")
                _owner(info, private=True)
                size += info.st_size
                _require(size <= self.max_bytes, "store_byte_budget")
                _require(info.st_size <= (_LOCK_BYTES if entry.name == _LOCK else self.max_capsule_bytes),
                         "store_entry_size_refused")
        _require(_identity(root)[:2] == _identity(self._paths())[:2], "store_directory_changed")
        return {"files": files, "bytes": size}

    def _read(self, path, limit, *, expected=None):
        self._paths()
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and not _linked(before) and before.st_nlink == 1
                 and 0 <= before.st_size <= limit, "store_entry_refused")
        _owner(before, private=True)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            _require(_identity(os.fstat(stream.fileno())) == _identity(before), "store_file_changed")
            raw = stream.read(limit + 1)
            _require(len(raw) <= limit and _identity(os.fstat(stream.fileno())) == _identity(before),
                     "store_file_changed")
        _require(_identity(path.lstat()) == _identity(before), "store_file_changed")
        if expected is not None:
            _require(raw == expected, "store_bytes_conflict")
        return raw, _identity(before)

    def _sync_directory(self):
        if os.name != "nt":
            fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _budget(self, snapshot, *, files=0, size=0):
        _require(snapshot["files"] + files <= self.max_files, "store_file_budget")
        _require(snapshot["bytes"] + size <= self.max_bytes, "store_byte_budget")

    @contextmanager
    def _locked(self):
        self._paths(create=True)
        directory_id = _identity(self.root.lstat())[:2]
        snapshot = self._inventory()
        lock = self.root / _LOCK
        _require(not lock.exists(), "store_busy_or_ambiguous")
        self._budget(snapshot, files=1, size=_LOCK_BYTES)
        raw = _LOCK_PREFIX + secrets.token_hex(16).encode("ascii") + b"\n"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                     | getattr(os, "O_NOFOLLOW", 0), 0o600)
        # Unknown write/fsync ACK deliberately leaves the exclusive lock consumed.
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _, lock_id = self._read(lock, _LOCK_BYTES, expected=raw)
        self._sync_directory()

        def guard():
            _require(_identity(self._paths())[:2] == directory_id, "store_directory_changed")
            _, current = self._read(lock, _LOCK_BYTES, expected=raw)
            _require(current == lock_id, "store_lock_changed")

        try:
            guard()
            yield guard
        finally:
            guard()  # A foreign/replaced lock is never unlinked.
            lock.unlink()
            self._sync_directory()

    def put(self, raw):
        """Retain exact bounded bytes; duplicate validation never overwrites anything."""
        _require(type(raw) is bytes and 0 < len(raw) <= self.max_capsule_bytes, "capsule_bytes_invalid")
        sha = hashlib.sha256(raw).hexdigest()
        ref = {"sha256": sha, "bytes": len(raw)}
        final, partial = self.root / (sha + ".json"), self.root / (sha + ".partial")
        try:
            with self._locked() as guard:
                if final.exists():
                    self.get(ref)
                    guard()
                    return ref
                _require(not partial.exists(), "capsule_partial_ambiguous")
                # Charge the temporary and exclusive publication link at their peak.
                self._budget(self._inventory(), files=2, size=2 * len(raw))
                guard()
                fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                _, partial_id = self._read(partial, self.max_capsule_bytes, expected=raw)
                guard()
                _require(_identity(partial.lstat()) == partial_id, "store_file_changed")
                os.link(partial, final, follow_symlinks=False)  # Atomic exclusive publication; never replace.
                guard()
                _require(partial.lstat().st_ino == final.lstat().st_ino
                         and partial.lstat().st_dev == final.lstat().st_dev, "store_file_changed")
                partial.unlink()
                self._sync_directory()
                self.get(ref)
                self._inventory()
                guard()
            return ref
        except CaptureStoreError:
            raise
        except OSError as exc:
            raise CaptureStoreError("storage_io_unknown") from exc

    def get(self, ref):
        """Read only the derived digest file; no directory scan, cache or repair."""
        _require(type(ref) is dict and set(ref) == {"sha256", "bytes"}
                 and type(ref["sha256"]) is str and _SHA.fullmatch(ref["sha256"])
                 and type(ref["bytes"]) is int and 0 < ref["bytes"] <= self.max_capsule_bytes,
                 "capture_reference_invalid")
        try:
            raw, _ = self._read(self.root / (ref["sha256"] + ".json"), self.max_capsule_bytes)
            _require(len(raw) == ref["bytes"] and hashlib.sha256(raw).hexdigest() == ref["sha256"],
                     "capture_hash_or_size_changed")
            return raw
        except CaptureStoreError:
            raise
        except OSError as exc:
            raise CaptureStoreError("capture_read_unknown") from exc

    def stats(self):
        """Advisory bounded snapshot; put rechecks the authoritative budget under lock."""
        try:
            value = {"files": 0, "bytes": 0} if self._paths(missing_ok=True) is None else self._inventory()
            return {**value, "max_files": self.max_files, "max_bytes": self.max_bytes,
                    "max_capsule_bytes": self.max_capsule_bytes}
        except CaptureStoreError:
            raise
        except OSError as exc:
            raise CaptureStoreError("store_snapshot_unknown") from exc

    def preflight(self, capsule_bytes):
        _require(type(capsule_bytes) is int and 0 < capsule_bytes <= self.max_capsule_bytes,
                 "capsule_bytes_invalid")
        value = self.stats()
        self._budget(value, files=3, size=_LOCK_BYTES + 2 * capsule_bytes)
        return value
