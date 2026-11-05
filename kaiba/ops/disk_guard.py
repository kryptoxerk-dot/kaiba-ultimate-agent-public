"""Keep the box's disk under a ceiling by ACTING, not only by paging.

MEASURED 2026-10-12: the root disk reached 98% used (2.3 GB free of 79 GB). db_guard had
been paging ``crit`` -- "disk free 2.3 GB < 3 GB" -- and nobody acted on it. What filled it:

* the database had grown from 22 GB (10-04) to 42 GB, about 2.4 GB a day, because the row
  retention job (kaiba/ops/retention.py) had been built and left switched off;
* two abandoned core-backup ``.partial`` files, 3.9 GB, that core_backup never removed;
* 2.0 GB of journald archives, a 2.2 GB raw database copy in /tmp, 1.3 GB of finished
  research directories under data/, ~0.9 GB of provider cache files no TTL can ever serve.

A page is not a control. This guard runs OUTSIDE the services (a systemd timer, every few
minutes), keeps its state in tmpfs, and on every pass:

1. **Housekeeping, always** -- deletes only things that are provably dead:
   * provider cache files older than the longest TTL + grace any reader can still serve
     (:data:`CACHE_MAX_AGE_S`, per provider directory);
   * core-backup ``.partial`` files older than :data:`PARTIAL_MAX_AGE_S` (no snapshot runs
     that long; core_backup's own ``--max-snapshot-s`` is 1800);
   * raw database copies in /tmp (``kaiba.db*``) older than :data:`TMP_DB_MAX_AGE_S` --
     AGENTS.md forbids them, they caused the 09-28 outage -- and pytest's leftovers;
   * agent research scratch directories ``data/codex-*`` / ``data/claude-*`` older than
     :data:`SCRATCH_MAX_AGE_S`. AGENTS.md: those are scratch, copy what matters off the box.
2. **At WARN** (``used >= warn_pct``): page on Telegram, hourly while it stays.
3. **At ACT** (``used >= act_pct``): run an emergency row-retention pass with the windows
   cut to their floors (:func:`kaiba.ops.retention.emergency_config`), so the database
   frees pages it can reuse and stops growing at once. Deleting rows does not shrink the
   file; it stops the growth, which is what keeps the next GB of disk. **Skipped while the
   database already holds** :data:`REUSABLE_SKIP_BYTES` **of free pages**: such a file cannot
   grow, so more deletes cannot move the disk and only load the database (MEASURED
   2026-11-03: 14.6 GB of the 42 GB file was free pages; the guard sat at ACT running 4k-17k
   row deletes for up to 380 s every 10 minutes, and used% never moved off 86%).
4. **At HALT** (``used >= halt_pct``): halt new entries (reason ``disk_guard:``) unless an
   entry halt is already set by anyone else, and delete raw /tmp database copies of ANY age.
   Exits keep running: a halt stops entries, never protection. The halt is lifted by this
   guard, and only this guard's own halt, once ``used < act_pct``.

``used`` is what ``df`` prints: ``used / (used + available)``, so the 5% root reserve does
not hide the last gigabytes.

What it never touches: the database file and its WAL, ``~/.hermes``, ``~/.config``, any
other project on the box, and anything it cannot classify.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GIB = 1024**3
HOUR_S = 3600
DAY_S = 86_400

#: Thresholds on df's Use%. INVENTED, chosen backwards from the failure: the database writes
#: up to ~2.5 GB/day and the WAL spikes to 3.5 GB, so on a 79 GB disk 92% (~6 GB free) is
#: the last point with a day of runway left. ACT sits a full day's growth earlier.
WARN_PCT = 80.0
ACT_PCT = 85.0
HALT_PCT = 92.0
REPEAT_S = HOUR_S
#: WARN repeats twice a day, not hourly: the box can sit just over the WARN line for days
#: (the database file never shrinks without a VACUUM), and an alert that repeats every hour
#: while nothing changes is how db_guard's `crit` page came to be ignored before 10-12.
WARN_REPEAT_S = 12 * HOUR_S

#: ACT's emergency retention is skipped while the database has at least this much free-page
#: space to reuse. Retention at ACT exists to stop the FILE growing; a file with free pages
#: grows by none of its next writes. 4 GiB is well over a day of the worst measured growth
#: with retention entirely off (~2.4 GB/day, 2026-10-12); the normal retention timer keeps
#: running regardless, and HALT always runs the emergency pass.
REUSABLE_SKIP_BYTES = 4 * GIB

#: The entry-halt reason prefix this guard owns. RiskGate.resume_if_reason_starts_with lifts
#: only a halt that starts with it, so an operator or daily-loss halt is never lifted here.
HALT_PREFIX = "disk_guard:"

#: Provider cache directory -> the oldest file any reader can still be served.
#: kaiba/providers/_http.cache_read serves at most ttl + stale_grace, and get_json's stale
#: fallback adds one day on a provider error. MEASURED 2026-10-13, the largest of those:
#: gmgn_cli._TTL 900 + 3600 (token.security), jupiter DECIMALS_TTL_S 86,400 + 86,400.
#: dedup's coin cache (pumpfun) is 7 d + 7 d grace; dedup's ipfs images 30 d.
CACHE_MAX_AGE_S: dict[str, int] = {
    "pumpfun": 15 * DAY_S,
    "ipfs": 31 * DAY_S,
}
CACHE_DEFAULT_MAX_AGE_S = 3 * DAY_S
#: The cache walk touches ~400k files; once an hour is plenty.
CACHE_SWEEP_INTERVAL_S = HOUR_S

PARTIAL_MAX_AGE_S = 3 * HOUR_S
TMP_DB_MAX_AGE_S = 12 * HOUR_S
PYTEST_TMP_MAX_AGE_S = DAY_S
SCRATCH_MAX_AGE_S = 14 * DAY_S
#: data/ subdirectories that are agent research scratch, never read by kaiba/.
SCRATCH_PREFIXES = ("codex-", "claude-")

OK, WARN, ACT, HALT = "ok", "warn", "act", "halt"
_RANK = {OK: 0, WARN: 1, ACT: 2, HALT: 3}


@dataclass(frozen=True)
class Usage:
    total: int
    used: int
    free: int

    @property
    def used_pct(self) -> float:
        """df's Use%: the root reserve counts as unavailable, not as free."""
        denom = self.used + self.free
        return 100.0 * self.used / denom if denom else 0.0


@dataclass
class Report:
    before: dict[str, Any]
    after: dict[str, Any]
    level: str
    removed: list[dict[str, Any]] = field(default_factory=list)
    freed_bytes: int = 0
    retention: dict[str, Any] | None = None
    db_reusable: int | None = None
    halt: str | None = None
    errors: list[str] = field(default_factory=list)
    alert: str | None = None
    delivered: bool = False


def usage_of(path: Path, disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> Usage:
    u = disk_usage(path)
    return Usage(int(u.total), int(u.used), int(u.free))


def level_for(pct: float, *, warn_pct: float, act_pct: float, halt_pct: float) -> str:
    if pct >= halt_pct:
        return HALT
    if pct >= act_pct:
        return ACT
    if pct >= warn_pct:
        return WARN
    return OK


# --------------------------------------------------------------------------------------
# housekeeping: what is provably dead
# --------------------------------------------------------------------------------------


def _tree_bytes(p: Path) -> int:
    if p.is_symlink() or p.is_file():
        try:
            return p.lstat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(p, followlinks=False):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _remove(p: Path, why: str, removed: list[dict[str, Any]], errors: list[str], dry_run: bool) -> int:
    size = _tree_bytes(p)
    if not dry_run:
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            errors.append(f"{p}: {type(exc).__name__}: {exc}")
            return 0
    removed.append({"path": str(p), "bytes": size, "why": why})
    return size


def _age_s(p: Path, now_s: float) -> float:
    try:
        return now_s - p.lstat().st_mtime
    except OSError:
        return -1.0


def sweep_cache(cache_root: Path, now_s: float, *, dry_run: bool,
                max_age: dict[str, int] | None = None,
                default_max_age: int = CACHE_DEFAULT_MAX_AGE_S) -> tuple[int, int, list[str]]:
    """Delete cache files older than their directory's max age. ``(files, bytes, errors)``.

    One summary line per run instead of one per file: there are hundreds of thousands.
    """
    max_age = CACHE_MAX_AGE_S if max_age is None else max_age
    n = freed = 0
    errors: list[str] = []
    if not cache_root.is_dir():
        return 0, 0, errors
    for sub in sorted(cache_root.iterdir()):
        if not sub.is_dir() or sub.is_symlink():
            continue
        limit = max_age.get(sub.name, default_max_age)
        try:
            entries = os.scandir(sub)
        except OSError as exc:
            errors.append(f"{sub}: {exc}")
            continue
        with entries:
            for e in entries:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if now_s - st.st_mtime <= limit:
                    continue
                if not dry_run:
                    try:
                        os.unlink(e.path)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        if len(errors) < 5:
                            errors.append(f"{e.path}: {exc}")
                        continue
                n += 1
                freed += st.st_size
    return n, freed, errors


def dead_files(data_dir: Path, tmp_dir: Path, now_s: float, *, level: str) -> list[tuple[Path, str]]:
    """Everything outside the cache this pass may delete, with the reason."""
    out: list[tuple[Path, str]] = []
    backups = data_dir / "backups"
    if backups.is_dir():
        for p in backups.glob(".*.partial"):
            if _age_s(p, now_s) > PARTIAL_MAX_AGE_S:
                out.append((p, "abandoned core-backup partial"))
    if tmp_dir.is_dir():
        for p in tmp_dir.glob("kaiba.db*"):
            age = _age_s(p, now_s)
            if age > TMP_DB_MAX_AGE_S or (level == HALT and age >= 0):
                out.append((p, "raw database copy in /tmp (forbidden by AGENTS.md)"))
        for p in tmp_dir.glob("pytest-of-*"):
            if _age_s(p, now_s) > PYTEST_TMP_MAX_AGE_S:
                out.append((p, "pytest leftovers"))
    if data_dir.is_dir():
        for p in data_dir.iterdir():
            if not p.is_dir() or p.is_symlink() or not p.name.startswith(SCRATCH_PREFIXES):
                continue
            if _newest_mtime_age(p, now_s) > SCRATCH_MAX_AGE_S:
                out.append((p, f"research scratch untouched for {SCRATCH_MAX_AGE_S // DAY_S}+ days"))
    return out


def _newest_mtime_age(p: Path, now_s: float) -> float:
    """Age of the most recently modified thing in the tree: a directory still being written
    to is not scratch yet, whatever its name says."""
    newest = 0.0
    for root, dirs, files in os.walk(p, followlinks=False):
        for name in [*dirs, *files]:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
    try:
        newest = max(newest, p.lstat().st_mtime)
    except OSError:
        return -1.0
    return now_s - newest


# --------------------------------------------------------------------------------------
# the database levers: emergency retention and the entry halt
# --------------------------------------------------------------------------------------


def _emergency_retention(db_path: Path, budget_s: float) -> dict[str, Any]:
    from kaiba.core import db as core_db
    from kaiba.ops import retention
    from kaiba.ops.scheduler import config_path, load_config

    cfg = retention.emergency_config(load_config(config_path()).retention, budget_s=budget_s)
    conn = core_db.connect(db_path)
    try:
        report = retention.run_exclusive(conn, cfg, dry_run=False)
    finally:
        conn.close()
    if report.get("mode") == "skipped":
        return {"deleted": 0, "stopped": report.get("reason")}
    return {"deleted": report.get("deleted"), "stopped": report.get("stopped"),
            "elapsed_s": report.get("elapsed_s"),
            "tables": {k: v.get("deleted", v.get("skipped")) for k, v in report.get("tables", {}).items()}}


def _db_reusable_bytes(db_path: Path) -> int | None:
    """Free-page bytes inside the database (page_size x freelist_count), or None when unknown.

    Header reads on a read-only, query-only connection: no table is scanned, so this cannot
    pin the WAL."""
    import sqlite3

    if not Path(db_path).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        conn.execute("PRAGMA query_only=1")
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        return page * free
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _halt_entries(db_path: Path, reason: str) -> str:
    """Set the entry halt unless one is already set. Returns what happened."""
    from kaiba.core import db as core_db
    from kaiba.execution.risk import RiskGate

    conn = core_db.connect(db_path)
    try:
        gate = RiskGate()
        row = gate._state_row(conn)
        if int(row["halted"]):
            current = str(row["halt_reason"] or "")
            return "already halted by us" if current.startswith(HALT_PREFIX) else f"already halted: {current}"
        gate.halt(reason, conn)
        return "halted"
    finally:
        conn.close()


def _lift_halt(db_path: Path) -> bool:
    from kaiba.core import db as core_db
    from kaiba.execution.risk import RiskGate

    conn = core_db.connect(db_path)
    try:
        return RiskGate().resume_if_reason_starts_with(HALT_PREFIX, conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# state, alerts
# --------------------------------------------------------------------------------------


def default_state_path() -> Path:
    shm = Path("/dev/shm")
    return (shm if shm.is_dir() else Path("/tmp")) / "kaiba-disk-guard.json"


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log.warning("disk_guard: could not persist state to %s: %s", path, exc)


def due_alert(state: dict[str, Any], level: str, now_ms: int, repeat_s: int) -> str | None:
    prev = state.get("level", OK)
    if level == OK:
        return "recovered" if prev != OK else None
    if _RANK[level] > _RANK.get(prev, 0):
        return "escalated"
    every = max(repeat_s, WARN_REPEAT_S) if level == WARN else repeat_s
    return "repeat" if now_ms - int(state.get("last_alert_ms") or 0) >= every * 1000 else None


def _gb(n: int) -> str:
    return f"{n / GIB:.1f} GB"


def render(report: Report, why: str, host: str) -> str:
    a = report.after
    head = f"{a['used_pct']:.0f}% used, {_gb(a['free'])} free of {_gb(a['total'])}"
    if report.db_reusable:
        head += f" (+{_gb(report.db_reusable)} reusable inside the database)"
    if why == "recovered":
        return f"KAIBA DISK GUARD — recovered on {host}\n{head}"
    lines = [f"KAIBA DISK GUARD — {report.level.upper()} on {host}", head]
    if report.freed_bytes:
        lines.append(f"freed {_gb(report.freed_bytes)} of dead files this pass")
    if report.retention is not None:
        lines.append(f"emergency retention: {report.retention.get('deleted')} rows deleted "
                     f"({report.retention.get('stopped') or 'complete'})")
    if report.halt:
        lines.append(f"new entries: {report.halt} (exits keep running)")
    if report.level in (ACT, HALT):
        lines.append("Run: du -xsh ~/* ~/.[!.]* /tmp/* | sort -h | tail -15  to find the rest.")
    lines.extend(report.errors[:3])
    return "\n".join(lines)


def _default_notify(text: str) -> bool:
    from kaiba.ops import db_guard

    return db_guard._default_notify(text)


# --------------------------------------------------------------------------------------
# one pass
# --------------------------------------------------------------------------------------


def _usage_dict(u: Usage) -> dict[str, Any]:
    return {"total": u.total, "used": u.used, "free": u.free, "used_pct": round(u.used_pct, 2)}


def run(
    data_dir: Path,
    *,
    db_path: Path | None = None,
    tmp_dir: Path = Path("/tmp"),
    warn_pct: float = WARN_PCT,
    act_pct: float = ACT_PCT,
    halt_pct: float = HALT_PCT,
    repeat_s: int = REPEAT_S,
    retention_budget_s: float = 480.0,
    dry_run: bool = False,
    state_path: Path | None = None,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    now_s: float | None = None,
    notify: Callable[[str], bool] | None = None,
    emergency_retention: Callable[[Path, float], dict[str, Any]] = _emergency_retention,
    halt_entries: Callable[[Path, str], str] = _halt_entries,
    lift_halt: Callable[[Path], bool] = _lift_halt,
    db_reusable: Callable[[Path], int | None] = _db_reusable_bytes,
) -> Report:
    now_s = time.time() if now_s is None else now_s
    now_ms = int(now_s * 1000)
    db_path = db_path or data_dir / "kaiba.db"
    state_path = state_path or default_state_path()
    state = _load_state(state_path)

    before = usage_of(data_dir, disk_usage)
    level = level_for(before.used_pct, warn_pct=warn_pct, act_pct=act_pct, halt_pct=halt_pct)
    report = Report(before=_usage_dict(before), after=_usage_dict(before), level=level)

    # 1. housekeeping -- always.
    for p, why in dead_files(data_dir, tmp_dir, now_s, level=level):
        report.freed_bytes += _remove(p, why, report.removed, report.errors, dry_run)
    last_sweep = float(state.get("cache_sweep_s") or 0)
    if level != OK or now_s - last_sweep >= CACHE_SWEEP_INTERVAL_S:
        n, freed, errs = sweep_cache(data_dir / "cache", now_s, dry_run=dry_run)
        if n:
            report.removed.append({"path": str(data_dir / "cache"), "bytes": freed,
                                   "why": f"{n} cache files past every reader's TTL"})
            report.freed_bytes += freed
        report.errors.extend(errs)
        if not dry_run:
            state["cache_sweep_s"] = now_s

    after = usage_of(data_dir, disk_usage)
    level = level_for(after.used_pct, warn_pct=warn_pct, act_pct=act_pct, halt_pct=halt_pct)

    # 2-4. the database levers, by level.
    if not dry_run and _RANK[level] >= _RANK[ACT] and db_path.exists():
        report.db_reusable = db_reusable(db_path)
        if level == ACT and report.db_reusable is not None and report.db_reusable >= REUSABLE_SKIP_BYTES:
            # The file cannot grow while it has this many free pages; deleting more rows would
            # not free a byte of disk. An unknown count (None) still acts.
            report.retention = {"deleted": 0,
                                "stopped": f"skipped: {_gb(report.db_reusable)} reusable inside the database"}
        else:
            try:
                report.retention = emergency_retention(db_path, retention_budget_s)
            except Exception as exc:  # the guard must finish its pass and page
                report.errors.append(f"emergency retention failed: {type(exc).__name__}: {exc}")
            after = usage_of(data_dir, disk_usage)
            level = level_for(after.used_pct, warn_pct=warn_pct, act_pct=act_pct, halt_pct=halt_pct)
    if not dry_run and db_path.exists():
        try:
            if level == HALT:
                report.halt = halt_entries(
                    db_path, f"{HALT_PREFIX} disk {after.used_pct:.0f}% used, {_gb(after.free)} free")
            elif _RANK[level] < _RANK[ACT] and lift_halt(db_path):
                report.halt = "resumed (disk back under the ACT line)"
        except Exception as exc:
            report.errors.append(f"entry halt step failed: {type(exc).__name__}: {exc}")

    report.after = _usage_dict(after)
    report.level = level
    if dry_run:
        return report

    why = due_alert(state, level, now_ms, repeat_s)
    if report.halt and not why:
        why = "halt"  # a halt set or lifted is always announced
    if why:
        report.alert = render(report, why, socket.gethostname())
        report.delivered = bool((notify or _default_notify)(report.alert))
    new_state = {**state, "level": level}
    if why and report.delivered:
        new_state["last_alert_ms"] = now_ms
    elif why:
        new_state["level"] = state.get("level", OK)  # undelivered: try again next pass
    _save_state(state_path, new_state)
    return report


def summarize(removed: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group removals by reason so the JSON line stays readable."""
    by: dict[str, dict[str, Any]] = {}
    for r in removed:
        g = by.setdefault(r["why"], {"why": r["why"], "count": 0, "bytes": 0})
        g["count"] += 1
        g["bytes"] += int(r["bytes"])
    return sorted(by.values(), key=lambda g: -g["bytes"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.ops.disk_guard", description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", type=Path, default=None, help="kaiba data dir (default: settings)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be removed; change nothing")
    ap.add_argument("--warn-pct", type=float, default=WARN_PCT)
    ap.add_argument("--act-pct", type=float, default=ACT_PCT)
    ap.add_argument("--halt-pct", type=float, default=HALT_PCT)
    ap.add_argument("--retention-budget-s", type=float, default=480.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    data_dir = args.data_dir
    db_path = None
    if data_dir is None:
        from kaiba.core.config import get_settings

        data_dir = Path(get_settings().kaiba_data_dir)
        db_path = get_settings().db_path
    report = run(Path(data_dir), db_path=db_path, warn_pct=args.warn_pct, act_pct=args.act_pct,
                 halt_pct=args.halt_pct, retention_budget_s=args.retention_budget_s,
                 dry_run=args.dry_run)
    out = asdict(report)
    out["removed"] = summarize(report.removed)
    sys.stdout.write(json.dumps(out, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
