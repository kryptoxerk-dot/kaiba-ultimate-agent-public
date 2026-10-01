"""Keep the live database writable: reclaim an overgrown WAL, and page before the disk fills.

MEASURED 2026-09-29. The box ran out of disk at about 23:25 UTC on 09-28 and every service
committed nothing for fourteen hours: no ingest, no scan verdicts, no ops jobs, and the
protection service could not record an exit. Nobody was told. The services logged
``database or disk is full`` into a journal that was itself being discarded for lack of
space, and the only alert channel (``trade_notify``) reads the database that had stopped.

Two causes, both silent:

1. The WAL was 19.8 GB -- larger than the 14.5 GB database. SQLite resets its log only at
   a moment when no connection is reading from it. The ops scheduler's timed-out jobs are
   "abandoned until they return", and an abandoned job keeps its read transaction for as
   long as its query runs, so some reader nearly always held an old snapshot and the log
   never restarted. Nothing in kaiba ever asked for a truncating checkpoint.
2. About 19 GB of hand-made database copies in ``/tmp``.

This guard runs OUTSIDE the services it protects (a systemd timer), keeps its state in
tmpfs rather than the database -- the database is the thing that stops accepting writes --
and does three things each pass:

- If the WAL is over ``wal_truncate_bytes``, attempt ``wal_checkpoint(TRUNCATE)`` with a
  short busy wait. Writers queue behind it for at most ``busy_ms``.
- Name the processes holding WAL read slots and how far behind each one's snapshot is, so
  a pinned log points at its pinner. Finding that by hand took twenty minutes.
- Alert on Telegram when the disk or the WAL crosses a threshold, repeat hourly while it
  stays bad, and say so when it recovers.

It deletes nothing. What to delete is an operator's decision.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import socket
import sqlite3
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GIB = 1024**3

#: Attempt a truncating checkpoint above this. At the measured ~1 MB/s of WAL traffic on
#: the box this is roughly a quarter hour of writes that never got reset.
WAL_TRUNCATE_BYTES = 1 * GIB
#: Page above this: the checkpoint is being refused and the log is growing without bound.
WAL_ALERT_BYTES = 4 * GIB
DISK_WARN_FREE_BYTES = 10 * GIB
DISK_CRIT_FREE_BYTES = 3 * GIB
#: How long a checkpoint may hold the write lock waiting for readers to drain. Every
#: service's own busy_timeout is 10 s, so writers wait through this rather than failing.
BUSY_MS = 3000
REPEAT_S = 3600

OK, WARN, CRIT = "ok", "warn", "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}

#: WAL-index lock bytes, from sqlite3's wal.c: 120 write, 121 checkpoint, 122 recover,
#: 123..127 read slots 0..4. Slot 0 means "reading the database file, not the log".
_READ_LOCK_BASE = 123
_READ_SLOTS = 5

#: One line of /proc/locks. Linux MERGES adjacent ranges one process holds, so a process
#: reading through two slots shows as ``124 125`` -- read the whole range, not its start.
_LOCK_RE = re.compile(
    r"^\d+:\s+(?P<type>POSIX|OFDLCK|FLOCK)\s+\S+\s+(?P<kind>READ|WRITE)\s+(?P<pid>-?\d+)\s+"
    r"[0-9a-fA-F]+:[0-9a-fA-F]+:(?P<inode>\d+)\s+(?P<start>\d+)\s+(?P<end>\d+|EOF)\s*$"
)


@dataclass(frozen=True)
class Reading:
    db_bytes: int
    wal_bytes: int
    disk_free_bytes: int
    disk_total_bytes: int


@dataclass
class Report:
    before: Reading
    after: Reading
    level: str
    reasons: list[str]
    checkpoint: tuple[int, int, int] | None = None
    checkpoint_error: str | None = None
    wal_header: dict[str, Any] | None = None
    readers: list[dict[str, Any]] = field(default_factory=list)
    alert: str | None = None
    delivered: bool = False


def wal_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-wal")


def shm_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + "-shm")


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def measure(db_path: Path, disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> Reading:
    usage = disk_usage(db_path.parent)
    return Reading(_size(db_path), _size(wal_path(db_path)), int(usage.free), int(usage.total))


def checkpoint(db_path: Path, busy_ms: int = BUSY_MS) -> tuple[int, int, int]:
    """``(busy, log_frames, checkpointed_frames)`` from ``wal_checkpoint(TRUNCATE)``.

    ``busy == 0`` means the log was copied in full and truncated to zero bytes.
    """
    conn = sqlite3.connect(str(db_path), timeout=busy_ms / 1000, isolation_level=None)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return int(row[0]), int(row[1]), int(row[2])
    finally:
        conn.close()


def read_wal_header(db_path: Path) -> dict[str, Any] | None:
    """The WAL-index header: last committed frame, frames copied back, and read marks.

    Native byte order, per wal.c. ``None`` when there is no shm file (no open WAL).
    """
    try:
        with shm_path(db_path).open("rb") as fh:
            raw = fh.read(136)
    except OSError:
        return None
    if len(raw) < 136:
        return None
    mx_frame = struct.unpack_from("=I", raw, 16)[0]
    n_backfill = struct.unpack_from("=I", raw, 96)[0]
    marks = list(struct.unpack_from("=5I", raw, 100))
    return {"mx_frame": mx_frame, "n_backfill": n_backfill, "read_marks": marks}


def parse_wal_readers(locks_text: str, inode: int) -> list[dict[str, Any]]:
    """Processes holding a WAL read slot on the shm file with ``inode``, from /proc/locks."""
    held: dict[int, set[int]] = {}
    for line in locks_text.splitlines():
        if "->" in line:  # a waiter, not a holder
            continue
        m = _LOCK_RE.match(line.strip())
        if not m or int(m["inode"]) != inode or m["kind"] != "READ":
            continue
        start = int(m["start"])
        end = start if m["end"] == "EOF" else int(m["end"])
        slots = {
            s for s in range(_READ_SLOTS) if start <= _READ_LOCK_BASE + s <= end
        }
        if slots:
            held.setdefault(int(m["pid"]), set()).update(slots)
    return [{"pid": pid, "slots": sorted(slots)} for pid, slots in sorted(held.items())]


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "?"
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()[:160]


def wal_readers(db_path: Path, header: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Who holds a read slot now, and how many frames behind the log head each one is."""
    try:
        inode = shm_path(db_path).stat().st_ino
        text = Path("/proc/locks").read_text()
    except OSError:
        return []
    readers = parse_wal_readers(text, inode)
    for r in readers:
        r["cmd"] = _cmdline(r["pid"])
        if header:
            behind = [
                header["mx_frame"] - header["read_marks"][s]
                for s in r["slots"]
                if s > 0 and header["read_marks"][s] <= header["mx_frame"]
            ]
            r["frames_behind"] = max(behind) if behind else 0
    return readers


def assess(
    reading: Reading, *, wal_alert_bytes: int, disk_warn_free: int, disk_crit_free: int
) -> tuple[str, list[str]]:
    level, reasons = OK, []
    free_gb = reading.disk_free_bytes / GIB
    if reading.disk_free_bytes < disk_crit_free:
        level = CRIT
        reasons.append(f"disk free {free_gb:.1f} GB < {disk_crit_free / GIB:.0f} GB")
    elif reading.disk_free_bytes < disk_warn_free:
        level = WARN
        reasons.append(f"disk free {free_gb:.1f} GB < {disk_warn_free / GIB:.0f} GB")
    if reading.wal_bytes > wal_alert_bytes:
        if _RANK[level] < _RANK[WARN]:
            level = WARN
        reasons.append(
            f"WAL {reading.wal_bytes / GIB:.1f} GB > {wal_alert_bytes / GIB:.0f} GB "
            "(checkpoint is being refused)"
        )
    return level, reasons


def default_state_path(db_path: Path) -> Path:
    """tmpfs when there is one: it stays writable when the disk does not."""
    shm = Path("/dev/shm")
    if shm.is_dir():
        return shm / "kaiba-db-guard.json"
    return db_path.parent / "ops" / "db_guard.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log.warning("db_guard: could not persist state to %s: %s", path, exc)


def due_alert(state: dict[str, Any], level: str, now_ms: int, repeat_s: int) -> str | None:
    """``"escalated"``, ``"repeat"``, ``"recovered"`` or ``None``."""
    prev = state.get("level", OK)
    if level == OK:
        return "recovered" if prev != OK else None
    if _RANK[level] > _RANK.get(prev, 0):
        return "escalated"
    last = int(state.get("last_alert_ms") or 0)
    return "repeat" if now_ms - last >= repeat_s * 1000 else None


def _fmt_gb(n: int) -> str:
    return f"{n / GIB:.1f} GB"


def render(report: Report, why: str, host: str) -> str:
    a = report.after
    if why == "recovered":
        return (
            f"KAIBA DB GUARD — recovered on {host}\n"
            f"disk free {_fmt_gb(a.disk_free_bytes)} of {_fmt_gb(a.disk_total_bytes)}; "
            f"WAL {_fmt_gb(a.wal_bytes)}; DB {_fmt_gb(a.db_bytes)}"
        )
    lines = [
        f"KAIBA DB GUARD — {report.level.upper()} on {host}",
        *report.reasons,
        f"disk free {_fmt_gb(a.disk_free_bytes)} of {_fmt_gb(a.disk_total_bytes)}; "
        f"WAL {_fmt_gb(a.wal_bytes)}; DB {_fmt_gb(a.db_bytes)}",
    ]
    if report.checkpoint is not None:
        busy, logged, copied = report.checkpoint
        lines.append(
            "checkpoint: truncated" if busy == 0
            else f"checkpoint refused: {copied:,} of {logged:,} frames copied back"
        )
    elif report.checkpoint_error:
        lines.append(f"checkpoint error: {report.checkpoint_error}")
    pinners = [r for r in report.readers if r.get("frames_behind", 0) > 0]
    for r in pinners[:4]:
        lines.append(
            f"WAL held by pid {r['pid']} ({r.get('cmd', '?')}), "
            f"{r['frames_behind']:,} frames behind"
        )
    if report.level == CRIT:
        lines.append(
            "When the disk is full no service can write: no entries, no recorded exits, "
            "no ingest. Free space on the box."
        )
    return "\n".join(lines)


def _default_notify(text: str) -> bool:
    from kaiba.ops import trade_notify

    token, chat = trade_notify.credentials()
    if not token or not chat:
        log.warning("db_guard: no Telegram credentials; alert not sent")
        return False
    return trade_notify.send(text, token=token, chat_id=chat)


def run(
    db_path: Path,
    *,
    wal_truncate_bytes: int = WAL_TRUNCATE_BYTES,
    wal_alert_bytes: int = WAL_ALERT_BYTES,
    disk_warn_free: int = DISK_WARN_FREE_BYTES,
    disk_crit_free: int = DISK_CRIT_FREE_BYTES,
    busy_ms: int = BUSY_MS,
    repeat_s: int = REPEAT_S,
    state_path: Path | None = None,
    notify: Callable[[str], bool] | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    now_ms: int | None = None,
    do_checkpoint: bool = True,
    do_alert: bool = True,
) -> Report:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    before = measure(db_path, disk_usage)
    ckpt: tuple[int, int, int] | None = None
    ckpt_error: str | None = None
    if do_checkpoint and db_path.exists() and before.wal_bytes > wal_truncate_bytes:
        try:
            ckpt = checkpoint(db_path, busy_ms)
        except sqlite3.Error as exc:
            ckpt_error = f"{type(exc).__name__}: {exc}"
    after = measure(db_path, disk_usage) if (ckpt or ckpt_error) else before
    level, reasons = assess(
        after,
        wal_alert_bytes=wal_alert_bytes,
        disk_warn_free=disk_warn_free,
        disk_crit_free=disk_crit_free,
    )
    header = read_wal_header(db_path)
    report = Report(
        before=before,
        after=after,
        level=level,
        reasons=reasons,
        checkpoint=ckpt,
        checkpoint_error=ckpt_error,
        wal_header=header,
        readers=wal_readers(db_path, header) if (ckpt and ckpt[0]) or level != OK else [],
    )
    if not do_alert:
        return report

    state_path = state_path or default_state_path(db_path)
    state = _load_state(state_path)
    why = due_alert(state, level, now_ms, repeat_s)
    if why:
        report.alert = render(report, why, socket.gethostname())
        report.delivered = bool((notify or _default_notify)(report.alert))
    new_state = {"level": level, "last_alert_ms": state.get("last_alert_ms", 0)}
    if why and report.delivered:
        new_state["last_alert_ms"] = now_ms
    elif why and not report.delivered:
        # Undelivered: keep the previous level so the next pass tries again.
        new_state["level"] = state.get("level", OK)
    _save_state(state_path, new_state)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.ops.db_guard", description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="database path (default: settings)")
    parser.add_argument("--dry-run", action="store_true", help="measure and print only")
    parser.add_argument("--wal-truncate-gb", type=float, default=WAL_TRUNCATE_BYTES / GIB)
    parser.add_argument("--wal-alert-gb", type=float, default=WAL_ALERT_BYTES / GIB)
    parser.add_argument("--disk-warn-gb", type=float, default=DISK_WARN_FREE_BYTES / GIB)
    parser.add_argument("--disk-crit-gb", type=float, default=DISK_CRIT_FREE_BYTES / GIB)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    db_path = args.db
    if db_path is None:
        from kaiba.core.config import get_settings

        db_path = get_settings().db_path
    report = run(
        Path(db_path),
        wal_truncate_bytes=int(args.wal_truncate_gb * GIB),
        wal_alert_bytes=int(args.wal_alert_gb * GIB),
        disk_warn_free=int(args.disk_warn_gb * GIB),
        disk_crit_free=int(args.disk_crit_gb * GIB),
        do_checkpoint=not args.dry_run,
        do_alert=not args.dry_run,
    )
    out = asdict(report)
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
