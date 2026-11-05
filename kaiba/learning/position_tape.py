"""Bounded original quote-tape selection; no pricing, database or report changes.

Capture and verify finite daily-file prefixes, spool only the requested positions,
then lend one position's stable-time-sorted rows at a time. A yielded list is valid
only until iteration advances: it is cleared before another position is decoded.
All source fields and duplicate observations survive. Prefix provenance is exposed
as ``receipt``; this is not a claim of tape completeness at the requested end time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "position-tape/1"
DAY_MS = 86_400_000
PREFIX = "kaiba-position-tape-"
HARD_SPOOL_BYTES = 256 * 1024 * 1024


class TapeError(ValueError):
    """Incomplete or untrusted tape selection; caller must not publish a pass."""


def _require(ok: bool, why: str) -> None:
    if not ok:
        raise TapeError("position_tape:" + why)


@dataclass(frozen=True)
class Window:
    pid: str
    opened: int
    closed: int


@dataclass(frozen=True)
class Limits:
    max_positions: int = 10_000
    max_days: int = 32
    max_line_bytes: int = 1_048_576
    max_source_bytes: int = 512 * 1024 * 1024
    max_source_rows: int = 3_000_000
    max_spool_bytes: int = 128 * 1024 * 1024
    max_position_bytes: int = 8 * 1024 * 1024
    max_position_rows: int = 50_000
    max_decoded_bytes: int = 64 * 1024 * 1024

    def validate(self) -> None:
        upper = Limits()
        for name, value in vars(self).items():
            ceiling = HARD_SPOOL_BYTES if name == "max_spool_bytes" else getattr(upper, name)
            _require(type(value) is int and 0 < value <= ceiling, "invalid_limit:" + name)
        _require(self.max_position_bytes <= self.max_spool_bytes, "position_cap_exceeds_spool")


def _safe(path: Path) -> Path:
    path = Path(path).absolute()
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_refused")
    return path.resolve()


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open(path: Path, flags: int, mode: int = 0o600) -> int:
    _safe(path)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode), "regular_file_required")
        _require(_identity(info) == _identity(path.stat(follow_symlinks=False)), "file_identity_changed")
        _safe(path)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _days(start: int, end: int) -> list[str]:
    # Exact day enumeration used by shadow_tape.load, including the adjacent days.
    return sorted({time.strftime("%Y-%m-%d", time.gmtime(stamp / 1000))
                   for stamp in range(start - DAY_MS, end + DAY_MS + 1, DAY_MS)})


def _decode(raw: bytes) -> dict[str, Any] | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise TapeError("position_tape:invalid_record_encoding_or_depth") from exc
    try:
        row = json.loads(text)
    except ValueError as exc:
        # Existing loader skips malformed complete JSONL records. A prefix cut
        # through a record is truncation, not permission to skip that observation.
        if raw.endswith(b"\n"):
            return None
        raise TapeError("position_tape:partial_prefix_record") from exc
    except RecursionError as exc:
        raise TapeError("position_tape:invalid_record_encoding_or_depth") from exc
    _require(type(row) is dict and "obs" in row, "invalid_record_shape")
    return row


def _decoded_size(value: Any) -> int:
    """Conservative retained Python-size accounting, including nested field values."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return total


class PositionTape:
    """Context-managed finite spool. Construction is lazy; __enter__ captures it.

    ``windows`` is ordered and unique. Iteration includes empty positions in that
    order, yielding ``(pid, borrowed_rows)``. Calling code must finish evaluation
    before advancing. A cap, source race or spool change raises; no partial report
    should be published. Source files are never modified. Cleanup removes only
    files created by this instance in its exact privately created directory.
    """

    def __init__(self, tape_dir: Path, start_ms: int, end_ms: int, windows: Sequence[Window], *,
                 tail_ms: int = 60_000, spool_root: Path | None = None,
                 limits: Limits = Limits()):
        limits.validate()
        _require(type(start_ms) is int and type(end_ms) is int and 0 <= start_ms <= end_ms,
                 "invalid_global_window")
        _require(end_ms - start_ms <= limits.max_days * DAY_MS, "day_cap")
        _require(type(tail_ms) is int and tail_ms == 60_000, "tail_window_must_remain_60s")
        _require(len(windows) <= limits.max_positions, "position_cap")
        self.windows = tuple(windows)
        self.selected = {}
        for index, window in enumerate(self.windows):
            _require(isinstance(window, Window) and type(window.pid) is str and bool(window.pid)
                     and len(window.pid.encode()) <= 512 and window.pid not in self.selected
                     and type(window.opened) is int and type(window.closed) is int
                     and 0 <= window.opened <= window.closed, "invalid_or_duplicate_position")
            self.selected[window.pid] = index
        self.tape_dir = _safe(tape_dir)
        self.spool_root = _safe(spool_root or Path("/dev/shm"))
        _require(self.spool_root.is_dir(), "spool_root_missing")
        self.start, self.end, self.tail, self.limits = start_ms, end_ms, tail_ms, limits
        self.directory: Path | None = None
        self._directory_identity = None
        self._files: dict[int, dict] = {}
        self._handles: OrderedDict[int, Any] = OrderedDict()
        self._ready = self._iterating = self._used = False
        self._borrowed: list[dict] | None = None
        self._receipt = {"version": VERSION, "requested_start_ms": start_ms, "requested_end_ms": end_ms,
            "per_position_tail_ms": tail_ms, "position_count": len(windows),
            "scope": "verified_captured_file_prefixes_only; not a complete end-time snapshot",
            "source_files": [], "missing_days": [], "missing_day_count": 0,
            "captured_file_count": 0, "observed_growing_file_count": 0,
            "source_rows": 0, "malformed_complete_lines": 0,
            "selected_rows": 0, "spool_bytes": 0, "max_loaded_position_rows": 0,
            "max_loaded_decoded_bytes": 0, "captured_ms": None, "verified_ms": None}

    @property
    def receipt(self) -> dict:
        return copy.deepcopy(self._receipt)

    def _own_directory(self) -> None:
        _require(self.directory is not None and self.directory.name.startswith(PREFIX)
                 and _safe(self.directory).parent == self.spool_root
                 and _identity(self.directory.stat()) == self._directory_identity,
                 "spool_directory_identity_changed")

    def __enter__(self) -> PositionTape:
        _require(self.directory is None and not self._used, "single_use_required")
        self._used = True
        path = Path(tempfile.mkdtemp(prefix=PREFIX, dir=self.spool_root))
        self.directory = path
        self._directory_identity = _identity(path.stat())
        try:
            self._capture()
            self._ready = True
            return self
        except BaseException:
            self.close()
            raise

    def _append(self, index: int, raw: bytes) -> None:
        raw = raw if raw.endswith(b"\n") else raw + b"\n"
        info = self._files.get(index)
        nbytes, nrows = (info["bytes"], info["rows"]) if info else (0, 0)
        _require(nrows + 1 <= self.limits.max_position_rows, "position_row_cap")
        _require(nbytes + len(raw) <= self.limits.max_position_bytes, "position_byte_cap")
        _require(self._receipt["spool_bytes"] + len(raw) <= self.limits.max_spool_bytes, "spool_byte_cap")
        if info is None:
            self._own_directory()
            path = self.directory / f"{index:05d}.jsonl"
            fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            info = {"path": path, "identity": _identity(os.fstat(fd)), "bytes": 0, "rows": 0,
                    "digest": hashlib.sha256()}
            self._files[index] = info
            self._handles[index] = os.fdopen(fd, "wb")
        if index not in self._handles:
            self._own_directory()
            fd = _open(info["path"], os.O_WRONLY | os.O_APPEND)
            if _identity(os.fstat(fd)) != info["identity"]:
                os.close(fd)
                raise TapeError("position_tape:spool_file_identity_changed")
            self._handles[index] = os.fdopen(fd, "ab")
        handle = self._handles.pop(index)
        self._handles[index] = handle
        handle.write(raw)
        if len(self._handles) > 16:
            self._handles.popitem(last=False)[1].close()
        info["bytes"] += len(raw)
        info["rows"] += 1
        info["digest"].update(raw)
        self._receipt["selected_rows"] += 1
        self._receipt["spool_bytes"] += len(raw)

    def _close_handles(self) -> BaseException | None:
        """Attempt every close even if a buffered flush fails (for example ENOSPC)."""
        first = None
        while self._handles:
            _, handle = self._handles.popitem(last=False)
            try:
                handle.close()
            except BaseException as exc:
                first = first or exc
                # Standard file close closes its descriptor even on flush error.
                # Never os.close a descriptor still claimed by a buffered object:
                # its later destructor could otherwise close a recycled descriptor.
        return first

    def _capture(self) -> None:
        self._receipt["captured_ms"] = int(time.time() * 1000)
        prefixes, total = [], 0
        for day in _days(self.start, self.end):
            path = self.tape_dir / (day + ".jsonl")
            _safe(path)
            if not path.exists():
                self._receipt["missing_days"].append(day)
                continue
            info = path.stat()
            _require(stat.S_ISREG(info.st_mode), "regular_file_required")
            total += info.st_size
            _require(total <= self.limits.max_source_bytes, "source_byte_cap")
            prefixes.append((path, info))
        for path, captured in prefixes:
            digest, remaining, rows, malformed = hashlib.sha256(), captured.st_size, 0, 0
            fd = _open(path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                _require(_identity(os.fstat(stream.fileno())) == _identity(captured), "source_identity_changed")
                while remaining:
                    raw = stream.readline(min(remaining, self.limits.max_line_bytes + 1))
                    _require(bool(raw), "source_prefix_truncated")
                    _require(len(raw) <= self.limits.max_line_bytes, "line_byte_cap")
                    remaining -= len(raw)
                    digest.update(raw)
                    rows += 1
                    self._receipt["source_rows"] += 1
                    _require(self._receipt["source_rows"] <= self.limits.max_source_rows, "source_row_cap")
                    row = _decode(raw)
                    if row is None:
                        malformed += 1
                        continue
                    try:
                        within = self.start <= row["obs"] <= self.end
                    except TypeError as exc:
                        raise TapeError("position_tape:invalid_observation_time") from exc
                    if not within:
                        continue
                    try:
                        index = self.selected.get(row["pid"])
                    except (KeyError, TypeError) as exc:
                        raise TapeError("position_tape:invalid_position_id") from exc
                    if index is not None:
                        window = self.windows[index]
                        if window.opened <= row["obs"] <= window.closed + self.tail:
                            self._append(index, raw)
                # A second finite-prefix hash admits append-only growth while
                # detecting truncation or changes to bytes used by this replay.
                stream.seek(0)
                check, remaining = hashlib.sha256(), captured.st_size
                while remaining:
                    block = stream.read(min(remaining, 65536))
                    _require(bool(block), "source_prefix_truncated")
                    check.update(block)
                    remaining -= len(block)
                after = os.fstat(stream.fileno())
                _require(check.digest() == digest.digest(), "source_prefix_changed")
                _require(after.st_size >= captured.st_size and _identity(path.stat()) == _identity(captured),
                         "source_replaced_or_truncated")
                _safe(path)
            self._receipt["malformed_complete_lines"] += malformed
            self._receipt["source_files"].append({"file": path.name, "captured_prefix_bytes": captured.st_size,
                "prefix_sha256": digest.hexdigest(), "captured_mtime_ns": captured.st_mtime_ns,
                "observed_size_after": after.st_size, "appended_bytes_observed": after.st_size - captured.st_size,
                "rows": rows, "malformed_complete_lines": malformed})
        close_error = self._close_handles()
        if close_error is not None:
            raise close_error
        self._receipt["missing_day_count"] = len(self._receipt["missing_days"])
        self._receipt["captured_file_count"] = len(self._receipt["source_files"])
        self._receipt["observed_growing_file_count"] = sum(
            f["appended_bytes_observed"] > 0 for f in self._receipt["source_files"])
        self._receipt["verified_ms"] = int(time.time() * 1000)

    def _load(self, index: int) -> list[dict]:
        info = self._files.get(index)
        if info is None:
            return []
        self._own_directory()
        rows: list[dict] = []
        decoded, consumed, digest = 0, 0, hashlib.sha256()
        fd = _open(info["path"], os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            _require(_identity(before) == info["identity"] and before.st_size == info["bytes"], "spool_changed")
            while raw := stream.readline(self.limits.max_line_bytes + 2):
                consumed += len(raw)
                _require(consumed <= info["bytes"] and len(rows) < info["rows"], "spool_changed")
                digest.update(raw)
                row = _decode(raw)
                _require(row is not None, "spool_changed")
                decoded += _decoded_size(row)
                # List capacity and stable-sort key/temp arrays are reserved too.
                _require(decoded + (len(rows) + 1) * 64 <= self.limits.max_decoded_bytes, "decoded_position_cap")
                rows.append(row)
            _require(digest.digest() == info["digest"].digest() and len(rows) == info["rows"], "spool_changed")
        rows.sort(key=lambda row: row["obs"])
        self._receipt["max_loaded_position_rows"] = max(self._receipt["max_loaded_position_rows"], len(rows))
        self._receipt["max_loaded_decoded_bytes"] = max(self._receipt["max_loaded_decoded_bytes"], decoded + len(rows) * 64)
        return rows

    def __iter__(self) -> Iterator[tuple[str, list[dict]]]:
        _require(self._ready and not self._iterating, "single_iteration_required")
        self._iterating = True
        for index, window in enumerate(self.windows):
            self._borrowed = self._load(index)
            try:
                yield window.pid, self._borrowed
            finally:
                self._borrowed.clear()
                self._borrowed = None

    def close(self) -> None:
        self._ready = False
        if self._borrowed is not None:
            self._borrowed.clear()
        close_error = self._close_handles()
        if self.directory is None:
            if close_error is not None:
                raise close_error
            return
        self._own_directory()
        known = {info["path"].name for info in self._files.values()}
        _require({p.name for p in self.directory.iterdir()} == known, "unexpected_spool_file_cleanup_refused")
        for info in self._files.values():
            _require(not info["path"].is_symlink() and _identity(info["path"].stat()) == info["identity"],
                     "spool_file_identity_changed")
        for index, info in list(self._files.items()):
            info["path"].unlink()
            # A later unlink may fail transiently. Keep only remaining ownership
            # records so an explicit retry can finish without permitting strangers.
            del self._files[index]
        self.directory.rmdir()
        self.directory = None
        if close_error is not None:
            raise close_error

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = ["PositionTape", "Window", "Limits", "TapeError", "VERSION"]
