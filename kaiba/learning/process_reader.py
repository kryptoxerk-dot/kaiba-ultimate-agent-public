"""One disposable read-only process per research query, with a wall deadline.

The executable child uses only the standard library. SQLite VM progress remains a
secondary limit: after OS process creation, the parent bounds disk waits and IPC.
Process creation itself can wait in the OS; no SQL is sent until it returns and
the deadline is checked again. A
kernel-uninterruptible child is reported as cleanup-pending, never as reaped.
"""
from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

VERSION = "research-process-reader/1"
WAL_LIMIT = 1_500_000_000
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CELL_BYTES = 8 * 1024 * 1024
MAX_COLUMNS = 256
REAP_SECONDS = 2


def _packed(value: Any, maximum: int) -> bytes:
    result = bytearray()
    for part in json.JSONEncoder(separators=(",", ":"), allow_nan=False).iterencode(value):
        encoded = part.encode()
        if len(result) + len(encoded) > maximum:
            raise ValueError("ipc_byte_cap")
        result.extend(encoded)
    return bytes(result)


def _cell(value: Any) -> Any:
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= MAX_CELL_BYTES and len(value.encode()) <= MAX_CELL_BYTES:
        return value
    if isinstance(value, (bytes, bytearray, memoryview)) and len(value) <= MAX_CELL_BYTES:
        return {"bytes_base64": base64.b64encode(value).decode("ascii")}
    raise ValueError("unsupported_or_oversized_source_cell")


def _unpack_cell(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"bytes_base64"}:
        raw = value["bytes_base64"]
        if not isinstance(raw, str) or len(raw) > 4 * ((MAX_CELL_BYTES + 2) // 3):
            raise ValueError("invalid_source_bytes")
        result = base64.b64decode(raw, validate=True)
        if len(result) > MAX_CELL_BYTES:
            raise ValueError("source_cell_byte_cap")
        return result
    if value is None or type(value) in (bool, int) or type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= MAX_CELL_BYTES and len(value.encode()) <= MAX_CELL_BYTES:
        return value
    raise ValueError("invalid_source_cell")


def _wal_size(path: Path) -> int:
    try:
        return Path(str(path) + "-wal").stat().st_size
    except FileNotFoundError:
        return 0


def _worker_main() -> int:
    """Private stdin/stdout protocol; close the connection before emitting anything."""
    packet, request_id, wal, conn, cursor = {}, None, 0, None, None
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request_byte_cap")
        request = json.loads(raw)
        request_id = request.get("request_id")
        if (set(request) != {"version", "request_id", "db_path", "sql", "args", "query_seconds", "max_rows"}
                or request["version"] != VERSION or not isinstance(request_id, str)
                or len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id)
                or not isinstance(request["db_path"], str) or not Path(request["db_path"]).is_absolute()
                or not isinstance(request["sql"], str) or not 0 < len(request["sql"]) <= MAX_REQUEST_BYTES
                or not isinstance(request["args"], list) or len(request["args"]) > 10_000
                or type(request["max_rows"]) is not int or not 1 <= request["max_rows"] <= 50_000
                or type(request["query_seconds"]) not in (float, int)
                or not math.isfinite(request["query_seconds"]) or not 0 < request["query_seconds"] <= 30):
            raise ValueError("invalid_read_request")
        path = Path(request["db_path"])
        wal = _wal_size(path)
        if wal > WAL_LIMIT:
            packet = {"status": "deferred", "reason": f"wal_over_limit:{wal}; retry after at least 60s"}
        else:
            started = time.monotonic()
            args = tuple(_unpack_cell(v) for v in request["args"])
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25)
            conn.execute("PRAGMA query_only=ON")
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_CELL_BYTES)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_REQUEST_BYTES)
            conn.set_progress_handler(lambda: int(time.monotonic() - started >= request["query_seconds"]), 1000)
            cursor = conn.execute(request["sql"], args)
            if cursor.description is None:
                raise ValueError("source_query_has_no_rows")
            columns = [v[0] for v in cursor.description]
            if len(columns) > MAX_COLUMNS or any(len(n.encode()) > 4096 for n in columns):
                raise ValueError("source_column_cap")
            rows, used = [], len(_packed(columns, MAX_RESPONSE_BYTES)) + 1024
            for row in cursor:
                if len(rows) >= request["max_rows"]:
                    raise ValueError("source_row_cap; no partial sample accepted")
                encoded = [_cell(v) for v in row]
                used += len(_packed(encoded, MAX_RESPONSE_BYTES)) + 1
                if used > MAX_RESPONSE_BYTES:
                    raise ValueError("response_byte_cap; no partial sample accepted")
                rows.append(encoded)
            packet = {"status": "ok", "columns": columns, "rows": rows}
    except (sqlite3.Error, ValueError, TypeError, OverflowError, OSError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "source_query_unavailable:" + type(exc).__name__
        packet = {"status": "deferred", "reason": reason[:160]}
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            if conn is not None:
                conn.close()
    # No live read transaction exists during serialization or pipe backpressure.
    packet.update(version=VERSION, request_id=request_id, wal_bytes=wal)
    try:
        result = _packed(packet, MAX_RESPONSE_BYTES)
    except ValueError:
        result = _packed({"version": VERSION, "request_id": request_id, "wal_bytes": wal,
                          "status": "deferred", "reason": "response_byte_cap; no partial sample accepted"}, MAX_RESPONSE_BYTES)
    sys.stdout.buffer.write(result)
    sys.stdout.buffer.flush()
    return 0


if __name__ != "__main__":
    from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

    _PENDING: dict[str, Any] = {}
    _PENDING_LOCK = threading.Lock()

    def _read_metadata(path: Path, maximum: int) -> bytes:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("cgroup_metadata_byte_cap")
        return raw

    def _check_cgroup(proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"), *,
                      worker_path: str | None = None, current_pid: int | None = None) -> None:
        """Detect a prior exact worker in this cgroup; never kill an unknown PID."""
        try:
            own = os.getpid() if current_pid is None else current_pid
            worker = (worker_path or str(Path(__file__).resolve())).encode()
            hierarchy = _read_metadata(proc_root / "self/cgroup", 4096).decode("utf-8")
            unified = [line[3:] for line in hierarchy.splitlines() if line.startswith("0::")]
            if len(unified) != 1:
                raise ValueError("cgroup_v2_unavailable")
            relative = PurePosixPath(unified[0])
            if not relative.is_absolute() or ".." in relative.parts:
                raise ValueError("cgroup_path_invalid")
            group = cgroup_root.joinpath(*relative.parts[1:])
            if not group.resolve().is_relative_to(cgroup_root.resolve()):
                raise ValueError("cgroup_path_escape")
            raw_pids = _read_metadata(group / "cgroup.procs", 16_384).decode("ascii").split()
            if len(raw_pids) > 256 or any(not p.isdecimal() or int(p) <= 0 for p in raw_pids):
                raise ValueError("cgroup_pid_cap_or_invalid")
            pids = {int(p) for p in raw_pids}
            if own not in pids:
                raise ValueError("current_process_cgroup_changed")
            for pid in pids - {own}:
                try:
                    raw = _read_metadata(proc_root / str(pid) / "cmdline", 8192)
                except FileNotFoundError:
                    continue  # Only a per-PID exit race is safe to skip.
                argv = raw.rstrip(b"\0").split(b"\0")
                if any(value == worker and i + 1 < len(argv) and argv[i + 1] == b"--worker"
                       for i, value in enumerate(argv)):
                    raise SourceDeferred("source_prior_cgroup_worker_present; retry after at least 60s")
        except SourceDeferred:
            raise
        except (OSError, ValueError, UnicodeError) as exc:
            raise SourceDeferred("source_cgroup_state_unknown; retry after at least 60s") from exc

    class ProcessReader(GuardedReader):
        """Same research reader interface; no parent SQLite connection is created."""

        def __init__(self, db_path: Path | str, **kwargs):
            super().__init__(db_path, **kwargs)
            self._active: subprocess.Popen | None = None
            self._threads: list[threading.Thread] = []
            self._busy = threading.Lock()
            self._key = str(self.db_path.resolve())

        def _worker_command(self) -> list[str]:
            return [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker"]

        def _cleanup(self, *, preserve_interrupt: bool = False) -> bool:
            try:
                return self._reap()
            except (KeyboardInterrupt, SystemExit):
                if not preserve_interrupt:
                    raise
                # A repeated interrupt must not mask the original cancellation.
                # Keep the child tracked until a later poll proves actual exit.
                return False

        def _reap(self) -> bool:
            process = self._active
            if process is None:
                with _PENDING_LOCK:
                    if _PENDING.get(self._key) is self:
                        _PENDING.pop(self._key)
                return True
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except OSError:
                    return False
            try:
                process.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                return False  # A kernel-uninterruptible task cannot be declared reaped.
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for thread in self._threads:
                thread.join(timeout=.1)
            self._threads = []
            self._active = None
            with _PENDING_LOCK:
                if _PENDING.get(self._key) is self:
                    _PENDING.pop(self._key)
            return True

        @staticmethod
        def shutdown_all() -> list[int]:
            """Bounded best effort; the CLI's supervisor must own the whole cgroup."""
            with _PENDING_LOCK:
                owners = list(_PENDING.values())
            pending = []
            preserve_interrupt = isinstance(sys.exc_info()[1], (KeyboardInterrupt, SystemExit))
            for owner in owners:
                if not owner._cleanup(preserve_interrupt=preserve_interrupt) and owner._active is not None:
                    pending.append(owner._active.pid)
            return pending

        def guard(self) -> int:
            with _PENDING_LOCK:
                owners = list(_PENDING.values())
            for owner in owners:
                if owner._active is None or owner._active.poll() is None:
                    raise SourceDeferred("source_worker_cleanup_pending; retry after at least 60s")
                owner._cleanup()
            if sys.platform.startswith("linux"):
                _check_cgroup()
            return super().guard()

        def _exchange(self, request: bytes, deadline: float) -> bytes:
            if time.monotonic() >= deadline:
                raise SourceDeferred("source_worker_wall_timeout_before_spawn; retry after at least 60s")
            command = self._worker_command()
            with _PENDING_LOCK:
                if time.monotonic() >= deadline:
                    raise SourceDeferred("source_worker_wall_timeout_before_spawn; retry after at least 60s")
                if _PENDING:
                    raise SourceDeferred("source_worker_cleanup_pending; retry after at least 60s")
                _PENDING[self._key] = self
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, close_fds=True, start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            self._active = process
            if time.monotonic() >= deadline:
                raise SourceDeferred("source_worker_wall_timeout_before_send; retry after at least 60s")
            output, errors, complete = bytearray(), [], threading.Event()

            def send():
                try:
                    view, offset = memoryview(request), 0
                    while offset < len(view):
                        if time.monotonic() >= deadline:
                            errors.append("source_worker_wall_timeout_before_send; retry after at least 60s")
                            break
                        written = process.stdin.write(view[offset:])
                        if not written:
                            raise OSError("child_stdin_closed")
                        offset += written
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        process.stdin.close()
                    except (OSError, ValueError):
                        pass

            def receive():
                try:
                    while True:
                        block = process.stdout.read(min(65_536, MAX_RESPONSE_BYTES + 1 - len(output)))
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > MAX_RESPONSE_BYTES:
                            errors.append("source_worker_response_byte_cap")
                            break
                except (OSError, ValueError):
                    errors.append("source_worker_pipe_unavailable")
                finally:
                    complete.set()

            self._threads = [threading.Thread(target=send, daemon=True), threading.Thread(target=receive, daemon=True)]
            for thread in self._threads:
                thread.start()
            while process.poll() is None or not complete.is_set():
                if errors:
                    raise SourceDeferred(errors[0])
                if time.monotonic() >= deadline:
                    raise SourceDeferred("source_worker_wall_timeout; no partial sample accepted; retry after at least 60s")
                time.sleep(.01)
            if errors:
                raise SourceDeferred(errors[0])
            if process.returncode != 0:
                raise SourceDeferred("source_worker_failed; no partial sample accepted")
            return bytes(output)

        def _read(self, sql: str, args: Sequence[Any], *, dictionaries: bool) -> list:
            if not self._busy.acquire(blocking=False):
                raise SourceDeferred("source_worker_concurrent_read_refused")
            size, started, attempted, outcome = 0, time.monotonic(), False, "deferred"
            try:
                size = self.guard()
                # Preserve the existing CLI reader's measured primary-key plan.
                if (not isinstance(sql, str) or len(sql) > MAX_REQUEST_BYTES
                        or len(sql.encode()) > MAX_REQUEST_BYTES or len(args) > 10_000
                        or any(isinstance(v, (str, bytes, bytearray, memoryview)) and len(v) > MAX_REQUEST_BYTES for v in args)):
                    raise SourceDeferred("source_worker_request_cap")
                sql = sql.replace("FROM decisions WHERE rowid>?", "FROM decisions NOT INDEXED WHERE rowid>?")
                request_id = uuid.uuid4().hex
                request = _packed({"version": VERSION, "request_id": request_id,
                    "db_path": str(self.db_path.resolve()), "sql": sql, "args": [_cell(v) for v in args],
                    "query_seconds": self.query_seconds, "max_rows": self.max_rows}, MAX_REQUEST_BYTES)
                if time.monotonic() >= started + self.query_seconds + 2:
                    raise SourceDeferred("source_worker_wall_timeout_before_spawn; retry after at least 60s")
                attempted = True
                result = self._exchange(request, started + self.query_seconds + 2)
                packet = json.loads(result)
                if (not isinstance(packet, dict) or packet.get("version") != VERSION
                        or packet.get("request_id") != request_id or type(packet.get("wal_bytes")) is not int
                        or packet["wal_bytes"] < 0):
                    raise ValueError("source_worker_protocol_identity")
                if packet.get("status") == "deferred":
                    if set(packet) != {"version", "request_id", "status", "reason", "wal_bytes"} or not isinstance(packet["reason"], str):
                        raise ValueError("source_worker_error_protocol")
                    if "wal_over_limit:" in packet["reason"]:
                        self.wal_waits += 1
                    raise SourceDeferred(packet["reason"])
                if (set(packet) != {"version", "request_id", "status", "columns", "rows", "wal_bytes"}
                        or packet["status"] != "ok" or not isinstance(packet["columns"], list)
                        or len(packet["columns"]) > MAX_COLUMNS
                        or not all(isinstance(n, str) and len(n.encode()) <= 4096 for n in packet["columns"])
                        or not isinstance(packet["rows"], list) or len(packet["rows"]) > self.max_rows):
                    raise ValueError("source_worker_result_protocol")
                columns = packet["columns"]
                rows = []
                for row in packet["rows"]:
                    if not isinstance(row, list) or len(row) != len(columns):
                        raise ValueError("source_worker_row_protocol")
                    values = tuple(_unpack_cell(v) for v in row)
                    rows.append(dict(zip(columns, values, strict=True)) if dictionaries else values)
                outcome = "complete"
                return rows
            except (ValueError, TypeError, OverflowError, OSError, RecursionError) as exc:
                raise SourceDeferred("source_worker_unavailable:" + type(exc).__name__) from exc
            finally:
                try:
                    active_error = sys.exc_info()[1]
                    worker_pid = self._active.pid if self._active is not None else None
                    reaped = self._cleanup(preserve_interrupt=isinstance(active_error, (KeyboardInterrupt, SystemExit)))
                    if not reaped and isinstance(active_error, SourceDeferred):
                        active_error.args = (str(active_error) + "; source_worker_cleanup_pending; retry after at least 60s",)
                    if attempted:
                        self.queries += 1
                        self.receipts.append({"duration_s": round(time.monotonic() - started, 4), "wal_bytes": size,
                                              "status": outcome if active_error is None and reaped else "deferred",
                                              "worker_reaped": reaped, "worker_pid": worker_pid})
                finally:
                    self._busy.release()


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit(2)
    raise SystemExit(_worker_main())
