"""Nightly snapshot of the state Kaiba cannot rebuild, small enough to fit on the box.

MEASURED 2026-10-01 (docs/research/audit-20261001-reliability.md, P1-1): there was no backup
of anything. The only copy was a 7-day-old full database in ``/tmp``, world-readable. The
live database is ~20 GB and grows ~2.9 GB a day against ~27 GB free, so a full ``.backup``
does not fit. Almost all of it is tape (``swaps``, ``events``, ``wallet_score_history``)
that is either re-derivable or not worth a second copy on the same disk. What cannot be
rebuilt -- the book, the orders, the journal, risk state, the paid grades, the operator's
imported wallets -- MEASURED at ~0.76 GB of row payload on the box the same day (row count
x sampled row length per table; ``--dry-run`` reprints it).

One run:

1. ``ATTACH`` the live database READ-ONLY (``mode=ro``) to a fresh file and copy the tables
   in :data:`CORE_TABLES` inside ONE read transaction, so every table comes from the same
   instant (an order can never reference a position the snapshot lacks). A ``mode=ro``
   attachment cannot take the write lock, and in WAL mode a reader never blocks a writer,
   so protection never waits on this. What it does cost: the WAL cannot be reset past an
   open snapshot, so the copy is aborted after ``max_snapshot_s`` rather than pinning it.
2. ``quick_check`` the copy and, when the journal was copied, re-verify its hash chain in
   the copy with :func:`kaiba.core.journal.verify`.
3. gzip it, write a manifest (table -> rows, sha256 of the raw and gzipped files), and keep
   the newest ``keep``. Files are 0600 in a 0700 directory.

Indexes are NOT copied (size and time); their DDL is stored in the copy's ``_kaiba_backup``
table and in the manifest, so a restore can rebuild them.

``--verify FILE`` is the restore drill: decompress to a scratch file, ``integrity_check``,
count every table against the manifest, re-verify the journal chain.

Not here, and deliberately: secrets (``*.env``, ``auth.json``) are never read; an off-box
copy needs a destination and a key the operator chooses.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

log = logging.getLogger(__name__)

FORMAT = "kaiba-core-backup-v1"
DAY_MS = 86_400_000
GIB = 1024**3

#: Other connections wait this long for a lock before failing; ours too.
BUSY_TIMEOUT_MS = 10_000
#: The snapshot holds the WAL open; past this it is aborted. INVENTED: ~10x the copy time
#: expected for ~0.8 GB at a few MB/s of idle-class IO on a saturated disk.
DEFAULT_MAX_SNAPSHOT_S = 900.0
#: Refuse to start below this much free disk. kaiba.ops.db_guard pages "critical" at 3 GiB;
#: the raw copy is ~1 GB and is deleted once gzipped.
DEFAULT_MIN_FREE_GB = 5.0
DEFAULT_KEEP = 7
#: A lock file older than this belongs to a dead run.
LOCK_STALE_S = 2 * 3600

NAME_RE = re.compile(r"^core-(\d{8})\.db\.gz$")


@dataclass(frozen=True)
class CoreTable:
    """One table to copy. ``time_col``/``days``: copy only rows newer than ``days``."""

    name: str
    why: str
    time_col: str | None = None
    days: int | None = None


#: Each table was checked against the live schema on 2026-10-01 (``sqlite_master``, read
#: only). A table missing from a database is reported in the manifest, never an error.
CORE_TABLES: tuple[CoreTable, ...] = (
    CoreTable("positions", "the book: every position ever opened, live and paper"),
    CoreTable("orders", "what was sent to a venue, with provider ids"),
    CoreTable("order_events", "the order state history"),
    CoreTable("position_orders", "which orders opened and closed which positions"),
    CoreTable("trades", "closed round trips: the PnL record"),
    CoreTable("fill_prices", "what fills actually paid -- the evidence loss_review lacks"),
    CoreTable("fill_reconciliations", "fill accounting repairs"),
    CoreTable("watchdog_state", "exit state per position; stranded/blind bookkeeping"),
    CoreTable("position_marks", "in-lifetime marks: the other missing evidence"),
    CoreTable("risk_state", "halts and the daily-loss ledger"),
    CoreTable("kv", "cursors, deferrals, stranded markers, copy-manager state"),
    CoreTable("journal", "the hash-chained learning journal (verified in the copy)"),
    CoreTable("playbook", "operator playbook"),
    CoreTable("playbook_hits", "playbook evidence"),
    CoreTable("playbook_retirements", "why a playbook entry was retired"),
    CoreTable("decisions", "engine decisions, last 30 days", "ts_ms", 30),
    CoreTable("signals", "lane signals, last 30 days", "created_ms", 30),
    CoreTable("decision_outcomes", "decision -> outcome links"),
    CoreTable("experiments", "experiment registry"),
    CoreTable("experiment_trades", "experiment membership"),
    CoreTable("gate_results", "proposal verdicts"),
    CoreTable("validation_gates", "validation gate definitions"),
    CoreTable("validation_runs", "validation history"),
    CoreTable("lane_trials", "lane trial records"),
    CoreTable("wallet_scores", "current grades, including PAID full-history grades"),
    CoreTable("wallets", "the operator's imported cohorts and GMGN tags"),
    CoreTable("wallet_cohorts", "cohort membership"),
    CoreTable("wallet_cohort_freezes", "frozen cohorts"),
    CoreTable("entities", "entity graph (clustering is disabled; this is the last build)"),
    CoreTable("entity_members", "entity membership"),
    CoreTable("tracker_watchlist", "admitted wallets and why"),
    CoreTable("standing_orders", "standing orders"),
    CoreTable("standing_sync", "standing-order sync state"),
    CoreTable("provider_budget", "the Helius credit ledger"),
    CoreTable("ops_jobs", "scheduler state"),
    CoreTable("ops_quota", "per-day paid quotas"),
    CoreTable("schema_migrations", "which migrations the copy's schema reflects"),
)

META_TABLE = "_kaiba_backup"


class BackupRefused(RuntimeError):
    """Preconditions not met (disk, lock). Nothing was written."""


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _uri(path: Path, *, read_only: bool = False) -> str:
    return path.resolve().as_uri() + ("?mode=ro" if read_only else "")


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(path, read_only=True), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA query_only=1")
    return conn


def _day(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y%m%d")


def _iso(now_ms: int) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _where(t: CoreTable, now_ms: int) -> tuple[str, tuple[Any, ...], str | None]:
    if t.time_col and t.days:
        cutoff = now_ms - t.days * DAY_MS
        return f' WHERE "{t.time_col}" >= ?', (cutoff,), f"{t.time_col} >= {cutoff} ({t.days}d)"
    return "", (), None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _HashingWriter:
    """File-like sink for GzipFile that counts and hashes what reaches the disk."""

    def __init__(self, fh: BinaryIO) -> None:
        self.fh = fh
        self.h = hashlib.sha256()
        self.n = 0

    def write(self, data: bytes) -> int:
        self.h.update(data)
        self.n += len(data)
        return self.fh.write(data)

    def flush(self) -> None:
        self.fh.flush()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # never probe on Windows: os.kill(pid, 0) terminates there
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _RunLock:
    """One backup at a time per output directory. Stale when old or its pid is gone."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False

    def __enter__(self) -> _RunLock:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    holder = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    holder = {}
                age = time.time() - float(holder.get("ts", 0) or 0)
                same_host = holder.get("host") == socket.gethostname()
                pid = holder.get("pid")
                dead = same_host and isinstance(pid, int) and not _pid_alive(pid)
                if age < LOCK_STALE_S and not dead:
                    raise BackupRefused(f"another backup holds {self.path.name}: {holder}") from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "ts": time.time()}, fh)
            self.held = True
            return self
        raise BackupRefused(f"could not take {self.path.name}")

    def __exit__(self, *exc: object) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


# --------------------------------------------------------------------------------------
# the snapshot
# --------------------------------------------------------------------------------------


@dataclass
class Snapshot:
    tables: dict[str, dict[str, Any]]
    missing: list[str]
    indexes: dict[str, list[str]]
    snapshot_s: float


def snapshot(
    src: Path,
    dst: Path,
    *,
    now_ms: int,
    tables: Sequence[CoreTable] = CORE_TABLES,
    max_snapshot_s: float = DEFAULT_MAX_SNAPSHOT_S,
    between_tables: Callable[[str], None] | None = None,
) -> Snapshot:
    """Copy ``tables`` from ``src`` into a new database at ``dst``, from one read snapshot.

    ``between_tables`` is a test hook, called after each table is copied while the read
    transaction is still open. On any failure ``dst`` is removed and the error re-raised.
    """
    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(_uri(dst), uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    started = time.monotonic()
    try:
        conn.execute("PRAGMA journal_mode=OFF")  # a failed copy is deleted, never rolled back
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("ATTACH DATABASE ? AS src", (_uri(src, read_only=True),))
        deadline = started + float(max_snapshot_s)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)

        conn.execute("BEGIN")
        # The first read of src opens its read transaction; every table below is read
        # inside it, so the whole copy is one instant of the live database.
        schema = conn.execute(
            "SELECT type, name, tbl_name, sql FROM src.sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        create = {name: sql for typ, name, _tbl, sql in schema if typ == "table"}
        index_sql: dict[str, list[str]] = {}
        for typ, _name, tbl, sql in schema:
            if typ == "index":
                index_sql.setdefault(tbl, []).append(sql)
        copied: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        indexes: dict[str, list[str]] = {}
        for t in tables:
            ddl = create.get(t.name)
            if ddl is None:
                missing.append(t.name)
                continue
            conn.execute(ddl)  # unqualified CREATE TABLE lands in main, i.e. the copy
            where, params, label = _where(t, now_ms)
            cur = conn.execute(f'INSERT INTO main."{t.name}" SELECT * FROM src."{t.name}"{where}', params)
            copied[t.name] = {"rows": int(cur.rowcount), "filter": label}
            indexes[t.name] = index_sql.get(t.name, [])
            if between_tables is not None:
                between_tables(t.name)
        conn.execute(f'CREATE TABLE "{META_TABLE}" (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        meta = {
            "format": FORMAT, "created_ms": str(now_ms), "source": str(src),
            "tables": json.dumps(copied), "missing": json.dumps(missing), "indexes": json.dumps(indexes),
        }
        conn.executemany(f'INSERT INTO "{META_TABLE}" (key, value) VALUES (?, ?)', list(meta.items()))
        conn.execute("COMMIT")  # ends the read snapshot on the live database
        conn.set_progress_handler(None, 0)
        conn.execute("DETACH DATABASE src")
        took = time.monotonic() - started
    except BaseException:
        try:
            conn.close()
        finally:
            dst.unlink(missing_ok=True)
        raise
    conn.close()
    return Snapshot(copied, missing, indexes, round(took, 2))


# --------------------------------------------------------------------------------------
# checks shared by the backup and the restore drill
# --------------------------------------------------------------------------------------


def _journal_check(conn: sqlite3.Connection) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal'").fetchone() is None:
        return {"present": False}
    from kaiba.core.journal import verify

    previous = conn.row_factory
    conn.row_factory = sqlite3.Row  # verify() reads columns by name
    try:
        ok, err = verify(conn)
        rows = conn.execute("SELECT count(*) FROM journal").fetchone()[0]
    finally:
        conn.row_factory = previous
    return {"present": True, "ok": bool(ok), "error": err, "rows": int(rows)}


def _meta(conn: sqlite3.Connection) -> dict[str, Any]:
    try:
        rows = dict(conn.execute(f'SELECT key, value FROM "{META_TABLE}"').fetchall())
    except sqlite3.OperationalError:
        return {}
    out: dict[str, Any] = dict(rows)
    for key in ("tables", "missing", "indexes"):
        if key in out:
            out[key] = json.loads(out[key])
    return out


def _gzip(raw: Path, gz_tmp: Path) -> tuple[str, str, int]:
    """Stream ``raw`` into ``gz_tmp``. Returns (raw sha256, gz sha256, gz bytes)."""
    raw_h = hashlib.sha256()
    with open(gz_tmp, "wb") as fh:
        sink = _HashingWriter(fh)
        with gzip.GzipFile(filename=raw.name, mode="wb", fileobj=sink, compresslevel=6, mtime=0) as gz:  # type: ignore[arg-type]
            with open(raw, "rb") as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    raw_h.update(chunk)
                    gz.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    return raw_h.hexdigest(), sink.h.hexdigest(), sink.n


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def prune_old(out_dir: Path, keep: int) -> list[str]:
    """Keep the newest ``keep`` backups (by the date in the name). Touches only our names."""
    found = sorted(
        (m.group(1), p) for p in out_dir.iterdir() if (m := NAME_RE.match(p.name))
    )
    removed: list[str] = []
    for day, gz_path in found[: max(0, len(found) - max(1, int(keep)))]:
        gz_path.unlink(missing_ok=True)
        (out_dir / f"core-{day}.json").unlink(missing_ok=True)
        removed.append(gz_path.name)
    cutoff = time.time() - DAY_MS / 1000
    for p in out_dir.glob(".core-*.partial"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------------------


def run_backup(
    src: Path,
    out_dir: Path,
    *,
    keep: int = DEFAULT_KEEP,
    max_snapshot_s: float = DEFAULT_MAX_SNAPSHOT_S,
    min_free_bytes: int = int(DEFAULT_MIN_FREE_GB * GIB),
    now_ms: int | None = None,
    tables: Sequence[CoreTable] = CORE_TABLES,
    between_tables: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Snapshot, check, gzip, manifest, rotate. Returns the manifest."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if not src.exists():
        raise BackupRefused(f"source database {src} does not exist")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        pass
    free = shutil.disk_usage(out_dir).free
    if free < min_free_bytes:
        raise BackupRefused(f"{free / GIB:.1f} GiB free under {out_dir}, below the {min_free_bytes / GIB:.1f} GiB floor")

    day = _day(now)
    raw = out_dir / f".core-{day}.db.partial"
    gz_tmp = out_dir / f".core-{day}.db.gz.partial"
    final = out_dir / f"core-{day}.db.gz"
    with _RunLock(out_dir / ".core-backup.lock"):
        try:
            snap = snapshot(src, raw, now_ms=now, tables=tables, max_snapshot_s=max_snapshot_s,
                            between_tables=between_tables)
            check = sqlite3.connect(_uri(raw, read_only=True), uri=True)
            try:
                quick = check.execute("PRAGMA quick_check").fetchone()[0]
                journal = _journal_check(check)
            finally:
                check.close()
            raw_bytes = raw.stat().st_size
            raw_sha, gz_sha, gz_bytes = _gzip(raw, gz_tmp)
            os.chmod(gz_tmp, 0o600)
            os.replace(gz_tmp, final)
        finally:
            raw.unlink(missing_ok=True)
            gz_tmp.unlink(missing_ok=True)
        manifest = {
            "format": FORMAT,
            "created_utc": _iso(now),
            "created_ms": now,
            "source": str(src),
            "file": final.name,
            "snapshot_s": snap.snapshot_s,
            "tables": snap.tables,
            "missing_tables": snap.missing,
            "indexes": snap.indexes,
            "raw_bytes": raw_bytes,
            "gz_bytes": gz_bytes,
            "raw_sha256": raw_sha,
            "gz_sha256": gz_sha,
            "quick_check": quick,
            "journal_chain": journal,
        }
        _write_json(out_dir / f"core-{day}.json", manifest)
        manifest["pruned"] = prune_old(out_dir, keep)
    return manifest


def verify_backup(path: Path, *, scratch_dir: Path | None = None) -> dict[str, Any]:
    """Restore drill: open the backup, integrity_check, count rows against the manifest,
    re-verify the journal chain. A ``.gz`` is decompressed to a scratch file first."""
    report: dict[str, Any] = {"file": str(path), "ok": False}
    if not path.exists():
        report["error"] = "no such file"
        return report
    m = NAME_RE.match(path.name)
    manifest_path = path.with_name(f"core-{m.group(1)}.json") if m else None
    manifest: dict[str, Any] = {}
    if manifest_path is not None and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report["manifest"] = str(manifest_path) if manifest else None
    checks: dict[str, bool] = {}
    temp: Path | None = None
    db = path
    try:
        if path.suffix == ".gz":
            if manifest.get("gz_sha256"):
                checks["gz_sha256"] = _sha256(path) == manifest["gz_sha256"]
            scratch = scratch_dir or path.parent
            temp = scratch / f".verify-{path.stem}.{os.getpid()}"
            raw_h = hashlib.sha256()
            try:
                with gzip.open(path, "rb") as src, open(temp, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        raw_h.update(chunk)
                        out.write(chunk)
            except (OSError, EOFError, zlib.error) as exc:
                report["error"] = f"decompress failed: {exc}"
                report["checks"] = checks
                return report
            if manifest.get("raw_sha256"):
                checks["raw_sha256"] = raw_h.hexdigest() == manifest["raw_sha256"]
            db = temp
        conn = sqlite3.connect(_uri(db, read_only=True), uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            checks["integrity_check"] = integrity == "ok"
            meta = _meta(conn)
            expected = meta.get("tables") or manifest.get("tables") or {}
            counts: dict[str, Any] = {}
            for name, info in expected.items():
                try:
                    got = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                except sqlite3.OperationalError as exc:
                    got = f"error: {exc}"
                counts[name] = {"expected": info.get("rows"), "found": got}
                checks[f"rows:{name}"] = got == info.get("rows")
            checks["has_tables"] = bool(expected)
            journal = _journal_check(conn)
            if journal.get("present"):
                checks["journal_chain"] = bool(journal.get("ok"))
        finally:
            conn.close()
        report.update(
            integrity=integrity, journal_chain=journal, rows=counts,
            created_utc=manifest.get("created_utc") or meta.get("created_ms"),
        )
        report["checks"] = {k: v for k, v in checks.items() if not k.startswith("rows:") or not v}
        report["tables_checked"] = len(counts)
        report["ok"] = all(checks.values())
        return report
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def dry_run(src: Path, out_dir: Path, *, now_ms: int | None = None,
            tables: Sequence[CoreTable] = CORE_TABLES, count_cap_s: float = 20.0) -> dict[str, Any]:
    """What a backup would copy and roughly how big it would be. Reads only, writes nothing.

    rows: ``count(*)`` (with the table's time filter), capped at ``count_cap_s`` per table;
    on the cap, the rowid span instead (an upper bound). bytes: rows x the mean length of
    the newest 1,000 rows (``length(CAST(col AS BLOB))`` summed over columns) x 1.1 for
    record and page overhead (INVENTED). gzip: zlib level 6 over the same sampled rows.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    conn = _ro_connect(src)
    deadline = [0.0]
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline[0] else 0, 20_000)
    out: dict[str, Any] = {"source": str(src), "out_dir": str(out_dir), "tables": {}, "missing": []}
    total_raw = total_gz = 0.0
    started = time.monotonic()
    try:
        for t in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t.name}")')]
            if not cols:
                out["missing"].append(t.name)
                continue
            where, params, label = _where(t, now)
            deadline[0] = time.monotonic() + count_cap_s
            try:
                rows = int(conn.execute(f'SELECT count(*) FROM "{t.name}"{where}', params).fetchone()[0])
                method = "count"
            except sqlite3.OperationalError:
                deadline[0] = time.monotonic() + count_cap_s
                # two seeks; min() and max() in ONE query scan the whole table
                lo, hi = conn.execute(
                    f'SELECT (SELECT min(rowid) FROM "{t.name}"), (SELECT max(rowid) FROM "{t.name}")'
                ).fetchone()
                rows, method = (int(hi - lo + 1) if hi is not None else 0), "rowid_span_upper_bound"
            deadline[0] = time.monotonic() + count_cap_s
            sample = conn.execute(
                f'SELECT * FROM "{t.name}"{where} ORDER BY rowid DESC LIMIT 1000', params
            ).fetchall()
            blob = "\n".join(repr(r) for r in sample).encode()
            avg = (len(blob) / len(sample)) if sample else 0.0
            ratio = (len(zlib.compress(blob, 6)) / len(blob)) if blob else 1.0
            est_raw = rows * avg * 1.1
            total_raw += est_raw
            total_gz += est_raw * ratio
            out["tables"][t.name] = {
                "rows": rows, "rows_method": method, "filter": label,
                "avg_row_bytes": round(avg), "est_mb": round(est_raw / 1e6, 1),
                "gzip_ratio": round(ratio, 3),
            }
    finally:
        conn.close()
    free = shutil.disk_usage(out_dir if out_dir.exists() else out_dir.parent).free
    out.update(
        est_raw_mb=round(total_raw / 1e6, 1),
        est_gz_mb=round(total_gz / 1e6, 1),
        free_gb=round(free / GIB, 1),
        elapsed_s=round(time.monotonic() - started, 1),
        method=(
            "rows x mean repr length of the newest 1,000 rows x 1.1 overhead; gzip by zlib-6 "
            "on the same sample. INFERRED, not a measured backup."
        ),
    )
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kaiba.ops.core_backup",
        description="Snapshot the irreplaceable tables to a gzipped SQLite file, keep the last N.",
    )
    ap.add_argument("--db", type=Path, default=None, help="live database (default: the configured one)")
    ap.add_argument("--out-dir", type=Path, default=None, help="default: <db dir>/backups")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--max-snapshot-s", type=float, default=DEFAULT_MAX_SNAPSHOT_S)
    ap.add_argument("--min-free-gb", type=float, default=DEFAULT_MIN_FREE_GB)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report what would be copied; write nothing")
    mode.add_argument("--verify", type=Path, default=None, metavar="FILE", help="restore drill on a backup")
    ap.add_argument("--scratch-dir", type=Path, default=None, help="--verify: where to decompress")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    os.umask(0o077)

    if args.verify is not None:
        report = verify_backup(args.verify, scratch_dir=args.scratch_dir)
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("ok") else 1

    if args.db is None:
        from kaiba.core.config import get_settings

        src = get_settings().db_path
    else:
        src = args.db
    out_dir = args.out_dir or (src.parent / "backups")
    if args.dry_run:
        print(json.dumps(dry_run(src, out_dir), indent=2, default=str))
        return 0
    try:
        manifest = run_backup(
            src, out_dir, keep=args.keep, max_snapshot_s=args.max_snapshot_s,
            min_free_bytes=int(args.min_free_gb * GIB),
        )
    except BackupRefused as exc:
        log.error("core backup refused: %s", exc)
        _last(out_dir, {"status": "refused", "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001 - recorded for the operator, then re-raised as a failed unit
        log.exception("core backup failed")
        _last(out_dir, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 1
    _last(out_dir, {"status": "ok", "file": manifest["file"], "gz_bytes": manifest["gz_bytes"],
                    "snapshot_s": manifest["snapshot_s"], "journal_ok": manifest["journal_chain"].get("ok")})
    print(json.dumps({k: v for k, v in manifest.items() if k != "indexes"}, indent=2, default=str))
    ok = manifest["quick_check"] == "ok" and manifest["journal_chain"].get("ok", True)
    return 0 if ok else 1


def _last(out_dir: Path, payload: dict[str, Any]) -> None:
    """``last.json``: one small file a monitor (or db_guard) can read without the database."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(out_dir / "last.json", {"ts_utc": _iso(int(time.time() * 1000)), **payload})
    except OSError as exc:
        log.warning("could not write last.json: %s", exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
