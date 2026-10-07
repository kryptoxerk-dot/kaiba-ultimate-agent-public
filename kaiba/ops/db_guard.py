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

2026-10-04: it also STOPS AD-HOC READERS that pin the log. An analysis script run over ssh
held one read transaction for 87 minutes, 355,415 frames behind, and the WAL reached 4.4 GB
before anyone noticed. Warning was not enough. When the WAL is over ``evict_wal_bytes`` and
the checkpoint is refused, a reader at least ``evict_frames_behind`` frames behind is sent
SIGTERM, then SIGKILL after ``evict_grace_s`` if it is still alive (a process inside a long
sqlite call ignores SIGTERM). Only processes OUTSIDE a ``kaiba-*.service`` systemd unit are
eligible; trading, protection, ingest, the sniper, the backup and the dashboard are never
touched, and a process whose unit cannot be read is treated as protected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
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
#: Stop an ad-hoc reader only while the WAL is over this AND the checkpoint was refused.
EVICT_WAL_BYTES = int(1.5 * GIB)
#: ...and only a reader at least this many frames behind the log head. One frame is a
#: 4 KiB page plus a 24-byte header, so 100k frames is ~400 MB of log one reader holds back.
#: Live services measured 0-1,016 frames behind on 2026-10-04; the evicted script 355,415.
EVICT_FRAMES_BEHIND = 100_000
EVICT_GRACE_S = 10.0
#: A systemd unit this agent runs. A reader inside one is never stopped.
_PROTECTED_UNIT = re.compile(r"^kaiba-[\w.@-]+\.service$")
#: Second valve (2026-10-04): when the WAL is past this and the checkpoint is refused with no
#: ad-hoc pinner to stop, restart the most-behind RESTARTABLE kaiba service. MEASURED that
#: day: with only service readers left, kaiba-scan (43,341 frames behind) and kaiba-ops
#: (an `entry_study` read) kept the log from ever resetting; WAL 4.8 GB and growing. A
#: restart of those two dropped it to 512 MB within seconds.
RESTART_WAL_BYTES = 3 * GIB
RESTART_FRAMES_BEHIND = 10_000
RESTART_COOLDOWN_S = 1800
#: The only units the guard may restart: neither holds money. Protection, engine, snipe and
#: ingest are never restarted by the guard -- a pinned log there pages a human instead.
RESTARTABLE_UNITS = frozenset({"kaiba-scan.service", "kaiba-ops.service"})
#: The guard runs on Linux; the fallback only lets the tests import it on Windows.
_SIGKILL = getattr(signal, "SIGKILL", 9)

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
    evicted: list[dict[str, Any]] = field(default_factory=list)
    restarted: dict[str, Any] | None = None
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


def _cgroup(pid: int) -> str | None:
    """The process's cgroup v2 path (``0::/...``), or None if it cannot be read."""
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:]
        return None
    except OSError:
        return None


def is_protected(cgroup: str | None) -> bool:
    """True for a reader inside a ``kaiba-*.service`` unit, or one we cannot classify."""
    if not cgroup:
        return True
    return any(_PROTECTED_UNIT.match(part) for part in cgroup.split("/"))


def select_evictions(
    readers: list[dict[str, Any]],
    *,
    frames_behind: int,
    own_pid: int,
    cgroup_of: Callable[[int], str | None] = _cgroup,
) -> list[dict[str, Any]]:
    """The readers to stop: far behind, not this process, not pid 1, not a kaiba service."""
    chosen = []
    for r in readers:
        pid = int(r.get("pid") or 0)
        if pid <= 1 or pid == own_pid or int(r.get("frames_behind") or 0) < frames_behind:
            continue
        cg = cgroup_of(pid)
        if is_protected(cg):
            continue
        chosen.append({**r, "cgroup": cg})
    return chosen


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def evict(
    targets: list[dict[str, Any]],
    *,
    grace_s: float = EVICT_GRACE_S,
    kill: Callable[[int, int], None] = os.kill,
    alive: Callable[[int], bool] = _alive,
    cmdline: Callable[[int], str] = _cmdline,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """SIGTERM each target, then SIGKILL whatever is still alive (and still the same
    process: same command line) after ``grace_s``. Returns what was done, per target."""
    done: list[dict[str, Any]] = []
    for t in targets:
        rec = {"pid": t["pid"], "cmd": t.get("cmd", "?"), "frames_behind": t.get("frames_behind"),
               "signals": []}
        try:
            kill(int(t["pid"]), signal.SIGTERM)
            rec["signals"].append("TERM")
        except ProcessLookupError:
            rec["signals"].append("gone")
        except OSError as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"
        done.append(rec)
    if any("TERM" in r["signals"] for r in done):
        sleep(grace_s)
    for rec in done:
        if "TERM" not in rec["signals"]:
            continue
        pid = int(rec["pid"])
        if alive(pid) and cmdline(pid) == rec["cmd"]:
            try:
                kill(pid, _SIGKILL)
                rec["signals"].append("KILL")
            except ProcessLookupError:
                pass
            except OSError as exc:
                rec["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("db_guard: stopped ad-hoc reader pid %s (%s), %s frames behind: %s",
                    pid, rec["cmd"], rec["frames_behind"], "+".join(rec["signals"]))
    return done


def unit_of(cgroup: str | None) -> str | None:
    """The systemd unit name in a cgroup path (its last ``*.service`` component), or None."""
    if not cgroup:
        return None
    units = [p for p in cgroup.split("/") if p.endswith(".service")]
    return units[-1] if units else None


def select_restart(
    readers: list[dict[str, Any]],
    *,
    frames_behind: int,
    cgroup_of: Callable[[int], str | None],
    last_restart_ms: dict[str, int],
    now_ms: int,
    cooldown_s: int,
) -> dict[str, Any] | None:
    """The most-behind reader in a RESTARTABLE unit that is far enough behind and off cooldown."""
    best = None
    for r in readers:
        unit = unit_of(cgroup_of(int(r.get("pid") or 0)))
        if unit not in RESTARTABLE_UNITS or int(r.get("frames_behind") or 0) < frames_behind:
            continue
        if now_ms - int(last_restart_ms.get(unit, 0)) < cooldown_s * 1000:
            continue
        if best is None or int(r["frames_behind"]) > int(best["frames_behind"]):
            best = {**r, "unit": unit}
    return best


def _orders_in_flight(db_path: Path) -> int | None:
    """Orders reserved/submitted/unknown; None if the database cannot be read (fail closed)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE state IN ('reserved','submitted','unknown')"
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _restart_unit(unit: str) -> bool:
    import subprocess

    done = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, timeout=60)
    return done.returncode == 0


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
    if report.restarted:
        r = report.restarted
        lines.append(
            f"RESTARTED {r['unit']} (pid {r['pid']}, {int(r.get('frames_behind') or 0):,} frames behind): "
            f"{'ok' if r.get('ok') else 'FAILED'}"
        )
    for e in report.evicted[:4]:
        lines.append(
            f"STOPPED ad-hoc reader pid {e['pid']} ({e.get('cmd', '?')}), "
            f"{int(e.get('frames_behind') or 0):,} frames behind: {'+'.join(e.get('signals', []))}"
        )
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
    do_evict: bool = True,
    evict_wal_bytes: int = EVICT_WAL_BYTES,
    evict_frames_behind: int = EVICT_FRAMES_BEHIND,
    evict_grace_s: float = EVICT_GRACE_S,
    cgroup_of: Callable[[int], str | None] = _cgroup,
    kill: Callable[[int, int], None] = os.kill,
    alive: Callable[[int], bool] = _alive,
    sleep: Callable[[float], None] = time.sleep,
    readers_of: Callable[[Path, dict[str, Any] | None], list[dict[str, Any]]] | None = None,
    do_restart: bool = True,
    restart_wal_bytes: int = RESTART_WAL_BYTES,
    restart_frames_behind: int = RESTART_FRAMES_BEHIND,
    restart_cooldown_s: int = RESTART_COOLDOWN_S,
    restart_unit: Callable[[str], bool] = _restart_unit,
    orders_in_flight: Callable[[Path], int | None] = _orders_in_flight,
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
    readers_of = readers_of or wal_readers
    header = read_wal_header(db_path)
    readers = readers_of(db_path, header) if (ckpt and ckpt[0]) or level != OK else []
    evicted: list[dict[str, Any]] = []
    if do_evict and ckpt and ckpt[0] and before.wal_bytes > evict_wal_bytes:
        targets = select_evictions(readers, frames_behind=evict_frames_behind,
                                   own_pid=os.getpid(), cgroup_of=cgroup_of)
        if targets:
            evicted = evict(targets, grace_s=evict_grace_s, kill=kill, alive=alive, sleep=sleep)
            try:
                ckpt = checkpoint(db_path, busy_ms)
                ckpt_error = None
            except sqlite3.Error as exc:
                ckpt_error = f"{type(exc).__name__}: {exc}"
            after = measure(db_path, disk_usage)
            level, reasons = assess(after, wal_alert_bytes=wal_alert_bytes,
                                    disk_warn_free=disk_warn_free, disk_crit_free=disk_crit_free)
            header = read_wal_header(db_path)
            readers = readers_of(db_path, header) if (ckpt and ckpt[0]) or level != OK else []
    restarted: dict[str, Any] | None = None
    state_path = state_path or default_state_path(db_path)
    restarts_state = dict(_load_state(state_path).get("restarts") or {})
    if do_restart and ckpt and ckpt[0] and after.wal_bytes > restart_wal_bytes:
        pick = select_restart(readers, frames_behind=restart_frames_behind, cgroup_of=cgroup_of,
                              last_restart_ms=restarts_state, now_ms=now_ms,
                              cooldown_s=restart_cooldown_s)
        if pick is not None:
            inflight = orders_in_flight(db_path)
            if inflight == 0:
                ok = bool(restart_unit(pick["unit"]))
                restarted = {"unit": pick["unit"], "pid": pick["pid"],
                             "frames_behind": pick.get("frames_behind"), "ok": ok}
                restarts_state[pick["unit"]] = now_ms
                log.warning("db_guard: restarted %s (pid %s, %s frames behind): %s",
                            pick["unit"], pick["pid"], pick.get("frames_behind"), ok)
                sleep(5.0)
                try:
                    ckpt = checkpoint(db_path, busy_ms)
                    ckpt_error = None
                except sqlite3.Error as exc:
                    ckpt_error = f"{type(exc).__name__}: {exc}"
                after = measure(db_path, disk_usage)
                level, reasons = assess(after, wal_alert_bytes=wal_alert_bytes,
                                        disk_warn_free=disk_warn_free, disk_crit_free=disk_crit_free)
                header = read_wal_header(db_path)
                readers = readers_of(db_path, header) if (ckpt and ckpt[0]) or level != OK else []
            else:
                reasons.append(f"would restart {pick['unit']} but orders in flight: {inflight}")
    report = Report(
        before=before,
        after=after,
        level=level,
        reasons=reasons,
        checkpoint=ckpt,
        checkpoint_error=ckpt_error,
        wal_header=header,
        readers=readers,
        evicted=evicted,
        restarted=restarted,
    )
    if not do_alert:
        if restarted:
            _save_state(state_path, {**_load_state(state_path), "restarts": restarts_state})
        return report

    state = _load_state(state_path)
    why = due_alert(state, level, now_ms, repeat_s)
    if (evicted or restarted) and not why:
        why = "evicted"  # stopping or restarting a process is always announced
    if why:
        report.alert = render(report, why, socket.gethostname())
        report.delivered = bool((notify or _default_notify)(report.alert))
    new_state = {"level": level, "last_alert_ms": state.get("last_alert_ms", 0),
                 "restarts": restarts_state}
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
    parser.add_argument("--no-evict", action="store_true", help="never stop ad-hoc readers")
    parser.add_argument("--evict-wal-gb", type=float, default=EVICT_WAL_BYTES / GIB)
    parser.add_argument("--evict-frames", type=int, default=EVICT_FRAMES_BEHIND)
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
        do_evict=not (args.dry_run or args.no_evict),
        do_restart=not (args.dry_run or args.no_evict),
        evict_wal_bytes=int(args.evict_wal_gb * GIB),
        evict_frames_behind=args.evict_frames,
    )
    out = asdict(report)
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
