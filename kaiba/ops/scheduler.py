# Codex 2026-09-27: owner-requested repair integration; see docs/TASKS.md REPAIR-INTEGRATION-20260927.
"""Deterministic maintenance scheduler — the Phase 7 "Operate" gap.

By 2026-09-20 the agent ran end to end, but every piece of intelligence it depends on went
stale unless a human typed a command: `kaiba backfill creators` (new creators every
minute), `kaiba backfill wallets` and `kaiba wallet grade` (buyers appear on every scan and
are never graded, which is the coverage gap that keeps three lanes from firing), `kaiba hunt
signals refresh`, `kaiba ingest trades`, native price sampling, the Helius budget ledger, the
journal hash chain, the validation power report. Each was built by a different agent with a
CLI entry point and no scheduler. This module calls their *Python* entry points on a clock.

It is deliberately not model-driven. Hermes cron (``hermes/cron/jobs.yaml``) is for prompts;
this is maintenance that must run whether or not a model is available, so it is a plain
loop over a table.

Five rules, each one a failure this project has already met or would meet on the first
unattended night:

1. **A job cannot take the service down.** Every job runs in its own thread with a hard
   timeout and its own database connection; everything it raises is caught, redacted and
   recorded. A job that times out is abandoned (Python cannot kill a thread) and stays
   marked busy until its thread returns, so it is never started twice in one process.
2. **Never two instances of one job**, including across process restarts: a lock file per
   job with the same stale-holder rule as ``pumpportal.ConnectionLock``, heart-beaten by the
   main loop while the job runs, plus one lock for the service itself.
3. **Budget before credits.** Any job that spends Helius credits reads
   ``helius.budget_status()`` first and is skipped, with the reason recorded, when the
   month's remaining credits are under the operator's floor. The wallet jobs also carry a
   per-UTC-day quota in ``ops_quota`` so a restart inside the day cannot reset the count.
   Burning a month of credits on a schedule is the one failure this must make impossible.
4. **State is in the database, not the process.** ``ops_jobs`` holds last run, last
   success, last error, duration and result per job, and ``next_due_ms`` survives a
   restart, so boot does not re-run everything and `kaiba ops status` can say whether
   maintenance is actually happening. A job that silently stopped is the failure mode
   this project keeps meeting, so "never ran", "late" and "stale" are first-class states.
5. **Failures back off, and one failing job never stops the others.** Intervals are
   jittered and boot is staggered so ten jobs do not hit the limiter together.

Nothing here modifies the jobs themselves. Where an entry point does not exist yet
(``kaiba.providers.native_price`` is being built by another agent) the job records
``entry_point_missing`` instead of shelling out to a CLI.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaiba.core import db as core_db
from kaiba.core.config import REPO_ROOT, get_settings
from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.events import emit
from kaiba.core.redact import redact_text
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.ops.retention import RetentionConfig

log = logging.getLogger(__name__)

#: Helius Free allowance, used only to sanity-check budgets in the config (the ledger owns
#: the live figure via ``helius.monthly_allowance``).
FREE_MONTHLY_CREDITS = 1_000_000

#: Planning figure for one wallet backfill + grade at ``pages: 1``. Derived from the live
#: ledger 2026-09-20 (78 Enhanced pages at the assumed 100/page + 79 full-transaction pages
#: at 10, over ~70 wallets = ~123/wallet) and rounded up. CONFIRMED by the scheduler's own
#: quota ledger on the same day: 8 wallets cost exactly 880 credits, **110 each**. Kept at
#: 125 rather than lowered to 110 because the Enhanced rate underneath it is itself an
#: assumption (see ``helius.CREDIT_COSTS``), and the module-wide rule is to charge the
#: higher plausible figure — over-counting starves us early, under-counting bills us.
CREDITS_PER_WALLET_EST = 125

#: ``wallet_scores.model_version`` of a grade built from our own partial tape
#: (``grade.MODEL_ID_TAPE``; the equality is asserted in tests/test_scheduler.py so the two
#: cannot drift). A tape grade is provisional by construction, so two readers of
#: ``wallet_scores`` must look straight through it: the paid queue (a wallet with only a
#: tape grade is still ungraded, or storing tape grades would silently drop every feed
#: wallet out of the backfill that buys it real evidence) and the free regrade (the
#: full-history model must not overwrite a tape grade with a verdict over the same thin
#: tape, which would then look like a measured grade forever). Both are prerequisites for
#: ``jobs.wallet_tape.store`` ever being true.
TAPE_MODEL_VERSION = "kaiba-wallet-tape-v1"
#: ``kaiba.intelligence.gmgn_grade.MODEL_ID_PROVIDER``, spelled here so the scheduler does not
#: import the provider grader at module load. tests/test_gather_throughput.py pins the two equal.
GMGN_PROVIDER_MODEL_VERSION = "kaiba-wallet-gmgn-v1"

#: SQL fragment: the wallet named by ``{alias}`` holds a grade that is NOT a tape grade.
#: Bind :data:`TAPE_MODEL_VERSION` for the ``?``. ``model_version`` is NOT NULL in the
#: schema, so ``!=`` cannot lose a row to a NULL.
_HAS_REAL_GRADE_SQL = (
    "EXISTS (SELECT 1 FROM wallet_scores ws WHERE ws.chain = {chain} AND ws.address = {wallet} "
    "AND ws.model_version != ?)"
)

#: Cap on the error/reason text stored per job. A traceback is in the log, not the table.
MAX_ERROR_CHARS = 500
#: How often, in SQLite VM steps, a job's connection checks its deadline. One clock read
#: per 50k steps is noise next to the query work it guards.
PROGRESS_EVERY_OPS = 50_000

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED = "skipped"
STATUS_RUNNING = "running"

REASON_BUDGET_FLOOR = "helius_budget_floor"
REASON_LOCKED = "locked_elsewhere"
REASON_ENTRY_MISSING = "entry_point_missing"
REASON_QUOTA = "daily_quota_reached"


# --------------------------------------------------------------------------------------
# exceptions
# --------------------------------------------------------------------------------------


class ScheduleConfigError(Exception):
    """config/schedule.yaml is missing or invalid. Loud at boot, tolerated on reload."""


class JobFailed(Exception):
    """A job's own verdict that its run failed. Carries a partial result for the record."""

    def __init__(self, message: str, result: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result: dict[str, Any] = dict(result or {})


class EntryPointMissing(Exception):
    """The module or function a job would call does not exist yet. Recorded, not failed."""


class AlreadyRunning(RuntimeError):
    def __init__(self, path: Path, holder: Mapping[str, Any] | None) -> None:
        self.path = path
        self.holder = dict(holder or {})
        super().__init__(f"{path.name} is held by {self.holder or 'an unreadable holder'}")


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


class BackoffConfig(BaseModel):
    """After ``after_failures`` consecutive failures the interval is multiplied per failure."""

    after_failures: int = 3
    multiplier: float = 2.0
    max_s: int = 6 * 3600


class HeliusGate(BaseModel):
    """Skip every Helius-spending job while the month's remaining credits are below this."""

    floor_credits: int = 300_000


class JobConfig(BaseModel):
    """One job's schedule. Anything beyond the three known fields is a job parameter."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    interval_s: int = Field(gt=0)
    timeout_s: int = Field(gt=0)

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class ScheduleConfig(BaseModel):
    version: str = "v1"
    tick_s: float = Field(default=5.0, gt=0)
    jitter_pct: float = Field(default=10.0, ge=0, le=50)
    stagger_s: float = Field(default=20.0, ge=0)
    max_concurrent: int = Field(default=2, ge=1)
    #: Jobs the trading path waits on. They run in slots of their own, outside
    #: ``max_concurrent``. MEASURED 2026-09-29: eighteen minutes after a restart,
    #: execute_planned (the only route from a decision to a venue), native_price (every
    #: size needs it), signals, hunters and budget_check had not run once -- three study
    #: jobs with 10-25 minute timeouts held all three slots. Nothing could be sized or sent.
    critical_jobs: list[str] = Field(
        default_factory=lambda: ["execute_planned", "native_price", "budget_check"]
    )
    critical_slots: int = Field(default=2, ge=1)
    #: A job whose timeout is at least this is "long", and at most ``max_concurrent_long``
    #: of them hold general slots at once, so they cannot take every slot between them.
    #: The box has two CPUs; three studies at once also starved the services of CPU.
    long_timeout_s: float = Field(default=600.0, gt=0)
    max_concurrent_long: int = Field(default=1, ge=1)
    #: A timed-out job keeps running -- most of them finish late and their late result is
    #: the only output they produce -- until ``timeout_s * hard_timeout_factor``. Then its
    #: SQL is aborted. Until that point a long orphan still holds its long slot, so timed-out
    #: studies cannot pile up behind the scheduler's back (they did: every timeout freed a
    #: slot while the work carried on).
    hard_timeout_factor: float = Field(default=3.0, ge=1.0)
    lock_stale_s: float = Field(default=900.0, gt=0)
    #: Deliberately much shorter than ``lock_stale_s``. MEASURED 2026-09-20: after the
    #: scheduler was hard-killed (SIGKILL, so no drain and no lock release) a restart was
    #: refused for the full ``lock_stale_s``. Under the unit's Restart=always with
    #: StartLimitBurst=10 in 300 s, systemd would exhaust its start limit and leave the
    #: service dead — a crash turning into a permanent outage, which is the exact failure
    #: this scheduler exists to prevent. The service lock is heart-beaten every tick, so
    #: 60 s is a dozen missed beats and cannot evict a live holder. Job locks keep the long
    #: window because reclaiming one means running a job that may still be in flight.
    service_lock_stale_s: float = Field(default=60.0, gt=0)
    reload_every_s: float = Field(default=60.0, gt=0)
    stop_grace_s: float = Field(default=30.0, ge=0)
    runs_retention_days: int = Field(default=14, ge=1)
    #: Row retention for provider_calls, triage_decisions and unchanged wallet_score_history
    #: (kaiba/ops/retention.py). ``retention.enabled`` defaults to False: the ``retention``
    #: job then reports a dry run and deletes nothing.
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    helius: HeliusGate = Field(default_factory=HeliusGate)
    backoff: BackoffConfig = Field(default_factory=BackoffConfig)
    jobs: dict[str, JobConfig] = Field(default_factory=dict)


def config_path() -> Path:
    override = os.environ.get("KAIBA_SCHEDULE_CONFIG")
    return Path(override) if override else REPO_ROOT / "config" / "schedule.yaml"


def load_config(path: Path | None = None) -> ScheduleConfig:
    """Parse the schedule. Raises :class:`ScheduleConfigError`; never returns a guess."""
    p = path or config_path()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ScheduleConfigError(f"{p} does not exist") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ScheduleConfigError(f"{p} unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScheduleConfigError(f"top level of {p} must be a mapping")
    try:
        return ScheduleConfig(**raw)
    except ValidationError as exc:
        raise ScheduleConfigError(f"{p} invalid: {exc}") from exc


def default_lock_dir() -> Path:
    return get_settings().kaiba_data_dir / "ops"


# --------------------------------------------------------------------------------------
# lock
# --------------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """POSIX-only. On Windows ``os.kill(pid, 0)`` would terminate the holder, so never probe."""
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class JobLock:
    """Advisory lock file, same rules as ``pumpportal.ConnectionLock``.

    A live holder is never evicted. A holder whose heartbeat is older than ``stale_after_s``,
    or whose pid is provably dead on this host, is debris from a crash and is reclaimed.
    """

    path: Path
    stale_after_s: float = 900.0
    _held: bool = field(default=False, init=False)

    def _payload(self) -> dict[str, Any]:
        ts = now_ms()
        return {"pid": os.getpid(), "host": socket.gethostname(), "started_ms": ts, "heartbeat_ms": ts}

    def read(self) -> dict[str, Any] | None:
        try:
            holder = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return holder if isinstance(holder, dict) else None

    def is_stale(self, holder: dict[str, Any] | None) -> bool:
        if not holder:
            return True
        beat = holder.get("heartbeat_ms") or holder.get("started_ms") or 0
        try:
            if (now_ms() - int(beat)) > self.stale_after_s * 1000:
                return True
        except (TypeError, ValueError):
            return True
        pid, host = holder.get("pid"), holder.get("host")
        return isinstance(pid, int) and host == socket.gethostname() and not _pid_alive(pid)

    def acquire(self) -> JobLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self.read()
                if not self.is_stale(holder):
                    raise AlreadyRunning(self.path, holder) from None
                log.warning("reclaiming stale ops lock %s (holder=%s)", self.path.name, holder)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._payload(), fh)
            self._held = True
            return self
        raise AlreadyRunning(self.path, self.read())

    def heartbeat(self) -> None:
        if not self._held:
            return
        holder = self.read() or self._payload()
        holder["heartbeat_ms"] = now_ms()
        try:
            self.path.write_text(json.dumps(holder), encoding="utf-8")
        except OSError as exc:
            log.warning("ops lock heartbeat failed for %s: %s", self.path.name, exc)

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except (FileNotFoundError, OSError):
            pass

    @property
    def held(self) -> bool:
        return self._held

    def __enter__(self) -> JobLock:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


# --------------------------------------------------------------------------------------
# job contract
# --------------------------------------------------------------------------------------


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def quota_used(conn: sqlite3.Connection, job: str, period: str) -> tuple[int, int]:
    """``(units, credits)`` this job has spent in ``period`` (a UTC day)."""
    row = fetch_one(conn, "SELECT units, credits FROM ops_quota WHERE job=? AND period=?", (job, period))
    return (int(row["units"]), int(row["credits"])) if row else (0, 0)


def quota_add(conn: sqlite3.Connection, job: str, period: str, *, units: int, credits: int) -> None:
    conn.execute(
        "INSERT INTO ops_quota (job, period, units, credits, updated_ms) VALUES (?,?,?,?,?) "
        "ON CONFLICT(job, period) DO UPDATE SET units = ops_quota.units + excluded.units, "
        "credits = ops_quota.credits + excluded.credits, updated_ms = excluded.updated_ms",
        (job, period, int(max(0, units)), int(max(0, credits)), now_ms()),
    )


@dataclass
class JobContext:
    """What a job gets: its own connection, its parameters, a deadline and a quota ledger."""

    name: str
    conn: sqlite3.Connection
    params: dict[str, Any]
    config: ScheduleConfig
    started_ms: int
    deadline_ms: int
    clock: Callable[[], float] = time.time

    def param(self, key: str, default: Any) -> Any:
        value = self.params.get(key)
        return default if value is None else value

    def now(self) -> int:
        return int(self.clock() * 1000)

    def time_left_s(self) -> float:
        return max(0.0, (self.deadline_ms - self.now()) / 1000.0)

    def quota_used(self) -> tuple[int, int]:
        return quota_used(self.conn, self.name, utc_day(self.now()))

    def quota_add(self, *, units: int, credits: int = 0) -> None:
        quota_add(self.conn, self.name, utc_day(self.now()), units=units, credits=credits)


@dataclass(frozen=True)
class JobSpec:
    name: str
    run: Callable[[JobContext], dict[str, Any]]
    why: str
    spends_helius: bool = False


@dataclass
class RunOutcome:
    job: str
    status: str
    started_ms: int
    finished_ms: int
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    credits: int = 0

    @property
    def duration_ms(self) -> int:
        return max(0, self.finished_ms - self.started_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "status": self.status,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "duration_ms": self.duration_ms,
            "credits": self.credits,
            "result": self.result,
            "error": self.error,
        }


def _error_text(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]


def _small(value: Any, limit: int = 2000) -> dict[str, Any]:
    """A result that fits in a table cell. Never raises: a bad result is still a result."""
    if hasattr(value, "as_dict"):
        try:
            value = value.as_dict()
        except Exception as exc:  # noqa: BLE001 - reporting must not fail the run
            return {"repr": repr(value)[:200], "as_dict_error": _error_text(exc)}
    if not isinstance(value, dict):
        return {"repr": repr(value)[:200]}
    text = jdump(value)
    if len(text) <= limit:
        return json.loads(text)
    return {"truncated": True, "keys": sorted(str(k) for k in value)[:50], "chars": len(text)}


# --------------------------------------------------------------------------------------
# jobs — each calls an existing Python entry point and returns a small dict
# --------------------------------------------------------------------------------------


def ungraded_buyers(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    limit: int,
    source: str = "pumpfun:trades",
    retry_after_ms: int = 7 * 86_400_000,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Buyers seen on scanned tokens that have no grade, most-confluent first.

    This is the targeting that matters: ``confluence-5``, ``trusted-copy`` and
    ``sm-trenches`` need graded buyers on the tokens tier 1 scans, and those buyers arrive
    through ``token_flow`` (``source='pumpfun:trades'``), not through the imported wallet
    list. A wallet on more scanned tokens is worth grading before one on a single token.
    Blacklisted wallets and wallets attempted within ``retry_after_ms`` are excluded so a
    permanently failing address cannot be paid for every run. A wallet whose only grade is
    a tape grade (:data:`TAPE_MODEL_VERSION`) is still ungraded here: the tape is the
    evidence this queue exists to go beyond.
    """
    ts = now if now is not None else now_ms()
    graded = _HAS_REAL_GRADE_SQL.format(chain="s.chain", wallet="s.wallet")
    return fetch_all(
        conn,
        "SELECT s.wallet AS wallet, COUNT(DISTINCT s.token) AS tokens, MAX(s.ts_ms) AS last_ms "
        "FROM swaps s "
        "WHERE s.chain = ? AND s.source = ? AND s.side = 'buy' "
        f"  AND NOT {graded} "
        "  AND NOT EXISTS (SELECT 1 FROM wallets w WHERE w.chain = s.chain AND w.address = s.wallet "
        "                  AND COALESCE(w.cohort, '') = 'blacklist') "
        "  AND COALESCE((SELECT updated_ms FROM kv WHERE kv.key = 'ops:wallet_buyers:' || s.chain || ':' "
        "                || s.wallet), 0) < ? "
        "GROUP BY s.wallet ORDER BY tokens DESC, last_ms DESC LIMIT ?",
        (chain.value, source, TAPE_MODEL_VERSION, ts - retry_after_ms, int(limit)),
    )


def _mark_attempted(conn: sqlite3.Connection, chain: Chain, wallet: str, note: str) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (f"ops:wallet_buyers:{chain.value}:{wallet}", jdump({"note": note, "ms": ts}), ts),
    )


def stale_scores(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    max_age_ms: int,
    limit: int,
    now: int | None = None,
) -> list[str]:
    """Graded wallets whose evidence moved since they were scored, or whose score is old.

    New evidence means a backfill cursor newer than the score or an on-chain swap newer
    than the score. Nothing regraded before this job existed; a grade is a snapshot and a
    snapshot ages. Tape grades (:data:`TAPE_MODEL_VERSION`) are never returned: the
    full-history grader would replace one with a verdict over the same thin tape, and the
    ``wallet_tape`` job is the one that refreshes them.
    """
    ts = now if now is not None else now_ms()
    rows = fetch_all(
        conn,
        "SELECT ws.address FROM wallet_scores ws WHERE ws.chain = ? AND ws.model_version NOT IN (?, ?) AND ("
        "  ws.scored_at_ms < ? "
        "  OR EXISTS (SELECT 1 FROM kv WHERE kv.key = 'backfill:swaps:' || ws.chain || ':' || ws.address "
        "             AND kv.updated_ms > ws.scored_at_ms) "
        "  OR EXISTS (SELECT 1 FROM swaps s WHERE s.chain = ws.chain AND s.wallet = ws.address "
        "             AND s.ts_ms > ws.scored_at_ms)"
        ") ORDER BY ws.scored_at_ms ASC LIMIT ?",
        # Provider grades are GMGN's arithmetic under a model version of their own; the
        # full-history grader would replace one with a verdict over whatever our tape holds.
        # MEASURED 2026-09-29: regrade had already turned 22 EVM GMGN B grades (robinhood 17,
        # bsc 5) into ungraded rows, and all 11,099 GMGN rows reach the 7-day age on 10-01.
        (chain.value, TAPE_MODEL_VERSION, GMGN_PROVIDER_MODEL_VERSION, ts - max_age_ms, int(limit)),
    )
    return [str(r["address"]) for r in rows]


def flow_targets(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    lookback_ms: int,
    refresh_ms: int,
    limit: int,
    source: str = "pumpfun:trades",
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Recently scanned tokens whose trade flow is missing or older than ``refresh_ms``."""
    ts = now if now is not None else now_ms()
    rows = fetch_all(
        conn,
        "SELECT d.address AS token, d.built_at_ms AS built_ms, t.created_ms AS created_ms, "
        "  (SELECT MAX(s.ts_ms) FROM swaps s WHERE s.chain = d.chain AND s.token = d.address "
        "   AND s.source = ?) AS last_ms "
        "FROM token_dossiers d LEFT JOIN tokens t ON t.chain = d.chain AND t.address = d.address "
        "WHERE d.chain = ? AND d.built_at_ms >= ? ORDER BY d.built_at_ms DESC",
        (source, chain.value, ts - lookback_ms),
    )
    due = [r for r in rows if r["last_ms"] is None or int(r["last_ms"]) < ts - refresh_ms]
    return due[: max(0, int(limit))]


def job_creators(ctx: JobContext) -> dict[str, Any]:
    from kaiba.intelligence.creators import INDEX_COMPLETE, INDEX_CREATED, BackfillConfig, backfill_creators

    index = str(ctx.param("index", "created")).lower()
    indexes = {"created": (INDEX_CREATED,), "complete": (INDEX_COMPLETE,),
               "both": (INDEX_CREATED, INDEX_COMPLETE)}.get(index)
    if indexes is None:
        raise JobFailed(f"unknown index {index!r}; one of created, complete, both")
    report = backfill_creators(
        ctx.conn,
        chain=Chain.SOL,
        pages=int(ctx.param("pages", 2)),
        indexes=indexes,
        max_creators=int(ctx.param("max_creators", 60)) or None,
        config=BackfillConfig(refresh_after_s=float(ctx.param("refresh_hours", 12)) * 3600.0),
    )
    d = report.as_dict()
    keep = ("creators_written", "creators_failed", "creators_discovered", "creators_skipped_fresh",
            "index_pages_ok", "index_pages_failed", "index_coins", "requests", "elapsed_s")
    out = {k: d.get(k) for k in keep}
    if d.get("index_pages_ok", 0) == 0 and d.get("index_pages_failed", 0) > 0:
        raise JobFailed("every discovery page failed", out)
    return out


def _grade_many(ctx: JobContext, wallets: Iterable[str]) -> dict[str, Any]:
    from kaiba.intelligence.grade import grade_address

    grades: dict[str, int] = {}
    failed = 0
    for address in wallets:
        try:
            score = grade_address(address, Chain.SOL, ctx.conn)
        except Exception as exc:  # noqa: BLE001 - one wallet's grade must not stop the batch
            failed += 1
            log.warning("ops: grading %s failed: %s", address[:12], _error_text(exc))
            continue
        grades[score.grade] = grades.get(score.grade, 0) + 1
    return {"graded": sum(grades.values()), "grade_failed": failed, "by_grade": grades}


# --------------------------------------------------------------------------------------
# wallet gathering: a budget-bound quota and a signal-ordered queue
# --------------------------------------------------------------------------------------
#
# MEASURED on the live box, 2026-09-21, before this block existed: the wallet_buyers job
# graded 15 wallets a day for 1,650 credits (110 each) against a month of 1,000,000 with
# a 300,000 floor, i.e. 0.5% of the discretionary budget, while `swaps` held 61,334
# distinct ungraded pump.fun buyers, 605 wallets seen on the GMGN smart-money feed and 82
# on the KOL feed, 255 graduated tokens with 9,368 wallets that bought inside their first
# 300 s, and 56 ungraded members of entities that already had a graded member. The queue
# was ordered by token count and then by recency. `wallet_scores` had 27 rows ever.
#
# Two things change here and nothing else: WHAT bounds a run (credits, paced to the
# month, under a hard daily ceiling, instead of a count) and WHICH wallets a credit is
# spent on (free signals first, recency last).

#: Hard ceiling on what one wallet job may spend in one UTC day, whatever the YAML says.
#: INVENTED guard: 5% of the free month (50,000). A typo that adds a zero to
#: ``credits_per_day`` is clamped here and reported as ``ceiling_clamped``. A runaway that
#: burns the month in a day is the failure this line exists for; the operator's floor is
#: the other half.
#:
#: Why 5% and not 50%: a clamp only guards if it sits BETWEEN the shipped ceiling and that
#: ceiling's one-zero typo. The shipped ``credits_per_day`` is 10,000; the typo is
#: 100,000. At 5% the typo lands on 50,000 and the operator still has 5x of headroom to
#: raise the YAML without touching code. At 50% (500,000) the typo passes untouched and one
#: job may spend 71% of the 700,000 above the floor in a single UTC day: a guard that
#: catches nothing the YAML could plausibly say. ``// 2`` was found on disk on 2026-09-22
#: under a comment and a test that both said 5%; the test's own docstring records "a hard
#: max of 50% of the month" as a mutant its earlier tests let through, so the likeliest
#: story (INFERRED, the agent that wrote it died before finishing) is a mutation run that
#: was never reverted. ``tests/test_scheduler.py`` pins ``* 20 == FREE_MONTHLY_CREDITS``.
GATHER_DAILY_CREDIT_HARD_MAX = FREE_MONTHLY_CREDITS // 20

#: A buy inside this many ms of a token's ``created_ms`` counts as early. MEASURED
#: horizon: the held-out result this piece leans on (early distinct buy count and early
#: independent entity count in the first 300 s predict graduation, AUC 0.82-0.92,
#: 2026-09-21) is a 300 s window, so the wallet-side reading uses the same window.
GATHER_EARLY_WINDOW_MS = 300_000

#: Provider cohort labels read off ``wallet.trade`` events (``payload.tags``). The feeds
#: were writing zero rows until 2026-09-21, so no yield per tag has ever been measured;
#: "smart money" is the vendor's own "wallet who often earns money" with no published
#: method, which is why a tag is a *prior on where to spend a credit* and never a grade.
GATHER_POSITIVE_TAGS: tuple[str, ...] = (
    "smart_degen", "kol", "launchpad_smart", "app_smart_money", "smart_money", "pump_smart",
    "renowned", "bluechip_owner", "top_trader",
)
GATHER_NEGATIVE_TAGS: tuple[str, ...] = (
    "wash_trader", "sandwich_bot", "mev_bot", "dex_bot", "scammer", "rat_trader", "fomo",
)

#: Optional hook for a piece that flags wallets worth grading (the tape-grading candidate
#: flag, if and when it lands). Either shape is read; neither is required.
GATHER_HOOK_TABLE = "wallet_gather_candidates"
GATHER_HOOK_KV_PREFIX = "gather:candidate:"

#: The ordering. Every weight is INVENTED and says so; there is no measured yield per
#: signal yet because nothing downstream ever consumed these feeds. What IS measured is
#: the direction: each signal is free, arrives before any credit is spent, and either
#: raises the chance the grade lands where a lane can use it (A/B, on a token that
#: graduates, in an entity we already know) or lowers it (a vendor bot label, a
#: buy-starved tape the grader would penalise anyway). The job stamps the signals that
#: admitted each wallet on its attempt mark so the yield per signal can be MEASURED from
#: ``wallet_scores`` later, at which point these numbers should be replaced.
GATHER_SIGNALS: dict[str, dict[str, Any]] = {
    "tape_profitable": {
        "weight": 20, "cap": 1,
        "provenance": "MEASURED 2026-09-29 over 431 paid sol grades: a wallet already "
                      "profitable on our own tape (tape grade, win rate >= 0.5, realized > 0) "
                      "became an earned B at 10.4% per paid grade, 15.0% with >= 20 closed "
                      "trades (95% CI 9.4-22.9%, n=107), against 2.6% for the rest of this "
                      "queue. The weight only has to outrank every invented signal combined.",
    },
    "feed": {
        "weight": 4, "cap": 1,
        "provenance": "INVENTED. Seen on a GMGN smart-money or KOL feed: a wallet.trade event "
                      "whose payload.source is gmgn:* (swaps.source as the fallback). MEASURED "
                      "coverage 2026-09-21: 720 sol feed wallets on the live box, 242 of them "
                      "pump.fun buyers with no grade.",
    },
    "tags_positive": {
        "weight": 2, "cap": 1,
        "provenance": "INVENTED. At least one positive cohort tag on a wallet.trade event "
                      "(GATHER_POSITIVE_TAGS). MEASURED: 699 sol wallets carried tags on "
                      "2026-09-21, smart_degen the most frequent.",
    },
    "tags_negative": {
        "weight": -6, "cap": 1,
        "provenance": "INVENTED. A vendor bot/wash label (GATHER_NEGATIVE_TAGS) is a low "
                      "prior on an A/B grade, so it goes to the back, not out: the grader "
                      "still decides. Deliberately larger than the feed weight so a labelled "
                      "bot on the feed sorts behind a wallet we know nothing about.",
    },
    "early_graduated": {
        "weight": 3, "cap": 3,
        "provenance": "INVENTED weight on a MEASURED direction: distinct graduated tokens "
                      "the wallet bought inside GATHER_EARLY_WINDOW_MS of launch (AUC "
                      "0.82-0.92 token-side, held out by time, 2026-09-21). Per token, "
                      "capped at 3.",
    },
    "graded_siblings": {
        "weight": 2, "cap": 1,
        "provenance": "INVENTED. Member of an entity with >= 1 A/B/C-graded member. A "
                      "graded sibling makes the entity's grade a question a credit can "
                      "settle; confluence counts entities, not addresses.",
    },
    "flagged": {
        "weight": 3, "cap": 1,
        "provenance": "INVENTED. Flagged by another piece through GATHER_HOOK_TABLE or a "
                      "GATHER_HOOK_KV_PREFIX key. UNAVAILABLE until such a piece exists.",
    },
    "tokens": {
        "weight": 1, "cap": 5,
        "provenance": "INVENTED. One point per two distinct scanned tokens bought, capped "
                      "at 5: more observed trades is more evidence per credit and a lower "
                      "chance of paying 110 credits for UNSCORED (2 of 15 live grades were).",
    },
}

#: Signals that make a wallet a worse-than-random use of a credit and that the grader
#: would penalise on arrival: a tape with >= SELL_ONLY_MIN_TRADES observations and a buy
#: share <= BUY_STARVED_MAX_BUY_SHARE is a settlement/router address, not a trader. Both
#: thresholds are the grader's own (kaiba.intelligence.grade), not invented here.
GATHER_EXCLUSIONS: dict[str, str] = {
    "buy_starved": "swaps trades >= grade.SELL_ONLY_MIN_TRADES and buys/trades <= "
                   "grade.BUY_STARVED_MAX_BUY_SHARE; the grader applies BUY_STARVED_PENALTY "
                   "to these so an A/B outcome is unreachable and the credit buys nothing.",
}

REASON_BUDGET_UNREADABLE = "budget_unreadable"

#: In-process cache of the feed/tag scan, keyed by database file and chain. Events are
#: append-only with an autoincrement id and are never pruned, so a later run in the same
#: scheduler process reads only the rows past the last id it saw; a restart rebuilds it
#: once. MEASURED 2026-09-21: the full scan is 0.9 s warm on the local database and part
#: of a 2.5 s queue build on the live box, but 40 s under disk contention, and the table
#: only grows.
_FEED_TAG_CACHE: dict[tuple[str, str], tuple[int, dict[str, int], dict[str, int]]] = {}
_FEED_TAG_LOCK = threading.Lock()


def _db_path(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2] or "") or ":memory:"
    except sqlite3.Error:
        return ":memory:"


@dataclass(frozen=True)
class CreditRoom:
    """What one run of a paid wallet job may spend, and which limit decided it.

    Money is integers in credits. ``pace_per_day``, ``remaining`` and ``days_left`` are
    ``None`` when the ledger could not be read — that run spends nothing.
    """

    credits: int
    binding: str
    daily_ceiling: int
    used_today: int
    run_max: int
    clamped: bool = False
    pace_per_day: int | None = None
    remaining: int | None = None
    days_left: int | None = None
    floor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "credits": self.credits, "binding": self.binding, "daily_ceiling": self.daily_ceiling,
            "used_today": self.used_today, "run_max": self.run_max, "ceiling_clamped": self.clamped,
            "pace_per_day": self.pace_per_day if self.pace_per_day is not None else "UNAVAILABLE",
            "remaining": self.remaining if self.remaining is not None else "UNAVAILABLE",
            "days_left": self.days_left if self.days_left is not None else "UNAVAILABLE",
            "floor": self.floor,
        }


def gather_credit_room(ctx: JobContext, *, daily_default: int, run_max: int) -> CreditRoom:
    """The binding limit on this run's spend: the tightest of four, each of them a stop.

    1. ``run_max`` — the run's own ceiling (``max_credits_per_run``).
    2. ``daily_ceiling`` — ``credits_per_day`` from the schedule, clamped to
       :data:`GATHER_DAILY_CREDIT_HARD_MAX`, minus what ``ops_quota`` says this job already
       spent today. Held in the database so a restart inside the UTC day cannot reset it.
       When the schedule names no ``credits_per_day`` the default is ``daily_default``,
       which the callers set to the OLD implicit ceiling (``wallets_per_day`` × the
       estimate × 2) so nothing changes until the operator raises it.
    3. ``monthly_pace`` — what is left above the operator's floor, spread over the days
       until Helius resets the allowance, minus today's spend. This is what makes the
       budget the binding limit near the end of a month instead of an arbitrary count.
    4. ``helius_budget_floor`` — nothing at all once remaining credits are at the floor.
       The dispatcher already refuses to start the job there; this is the in-run copy.

    An unreadable ledger is ``budget_unreadable`` and zero credits, never a guess.
    """
    from kaiba.providers import helius

    raw_ceiling = max(0, int(ctx.param("credits_per_day", daily_default)))
    clamped = raw_ceiling > GATHER_DAILY_CREDIT_HARD_MAX
    ceiling = min(raw_ceiling, GATHER_DAILY_CREDIT_HARD_MAX)
    _, used_credits = ctx.quota_used()
    floor = int(ctx.config.helius.floor_credits)
    try:
        st = helius.budget_status(conn=ctx.conn)
        remaining = int(st["remaining"])
        resets_in_s = max(0, int(st.get("resets_in_s") or 0))
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger must block spending
        log.warning("ops: %s cannot read the helius ledger: %s", ctx.name, _error_text(exc))
        return CreditRoom(0, REASON_BUDGET_UNREADABLE, ceiling, used_credits, int(run_max), clamped,
                          floor=floor)
    above_floor = remaining - floor
    days_left = max(1, (resets_in_s + 86_399) // 86_400)
    pace_per_day = max(0, above_floor) // days_left
    # Ties go to the earlier key: at the floor both pace and floor read zero and the floor
    # is the reason the dispatcher would have given, so it is the one recorded here.
    limits = {
        REASON_BUDGET_FLOOR: max(0, above_floor),
        "monthly_pace": max(0, pace_per_day - used_credits),
        "daily_ceiling": max(0, ceiling - used_credits),
        "run_size": max(0, int(run_max)),
    }
    binding = min(limits, key=lambda k: (limits[k], list(limits).index(k)))
    return CreditRoom(
        limits[binding], binding, ceiling, used_credits, int(run_max), clamped,
        pace_per_day=pace_per_day, remaining=remaining, days_left=days_left, floor=floor,
    )


@dataclass
class GatherQueue:
    """The ordered queue plus what it was ordered by, so a run can say what it used."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    candidates: int = 0
    signals: dict[str, str] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)

    def signal_tag(self, wallet: str) -> str:
        """Compact ``name:value`` list of the non-zero signals that admitted ``wallet``."""
        for r in self.rows:
            if r["wallet"] == wallet:
                parts = [f"{k}:{r[k]}" for k in GATHER_SIGNALS if int(r.get(k) or 0)]
                return ",".join(parts) or "none"
        return "UNAVAILABLE"

    def summary(self) -> dict[str, Any]:
        return {
            "ordered_by": "priority DESC, tokens DESC, last_ms DESC (recency is the last tiebreak)",
            "candidates": self.candidates,
            "signals": dict(self.signals),
            "excluded": dict(self.excluded),
            "admitted": [
                {"wallet": r["wallet"][:12], "priority": r["priority"],
                 **{k: r[k] for k in GATHER_SIGNALS if int(r.get(k) or 0)}}
                for r in self.rows[:10]
            ],
        }


def gather_priority(row: Mapping[str, Any]) -> int:
    """The INVENTED ordinal prior over free signals. Pure, so it is mutation-testable."""
    total = 0
    for name, spec in GATHER_SIGNALS.items():
        value = int(row.get(name) or 0)
        if name == "tokens":
            value = value // 2
        total += int(spec["weight"]) * min(max(0, value), int(spec["cap"]))
    return total


def _gather_signal(queue: GatherQueue, name: str, fn: Callable[[], dict[str, int]]) -> dict[str, int]:
    """Run one free-signal query; a missing table or function is UNAVAILABLE, not a crash."""
    try:
        out = fn()
    except sqlite3.Error as exc:
        queue.signals[name] = f"UNAVAILABLE: {_error_text(exc)}"
        return {}
    queue.signals[name] = f"ok:{len(out)}"
    return out


def gather_queue(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    limit: int,
    source: str = "pumpfun:trades",
    retry_after_ms: int = 7 * 86_400_000,
    now: int | None = None,
) -> GatherQueue:
    """Ungraded buyers on scanned tokens, ordered by the free signals in :data:`GATHER_SIGNALS`.

    Same admission rules as :func:`ungraded_buyers` (a buy on ``source``, no grade, not
    blacklisted, not attempted inside ``retry_after_ms``) plus the buy-starved exclusion,
    then ordered by :func:`gather_priority` with recency as the last tiebreak. Each signal
    is one query; one that cannot run (no JSON1, a table another piece has not created
    yet) is reported as UNAVAILABLE and the ordering degrades to the signals that could.
    """
    from kaiba.intelligence.grade import BUY_STARVED_MAX_BUY_SHARE, SELL_ONLY_MIN_TRADES

    ts = now if now is not None else now_ms()
    ch = chain.value
    queue = GatherQueue()
    # The measured best use of a credit goes first -- and when it fills the batch, the
    # swaps aggregate below is never run. MEASURED 2026-09-29: that aggregate walks every
    # sol swap (~7M rows) to rank ~436k candidates; the job timed out on it for four days
    # while spending zero credits (90-132 paid grades/day on 09-22..25, then 0, 18, 6, 0).
    tape = _tape_profitable_candidates(conn, ch, limit=limit, ts=ts, retry_after_ms=retry_after_ms)
    queue.signals["tape_profitable"] = f"ok:{len(tape)}"
    if len(tape) >= limit:
        queue.candidates = len(tape)
        queue.rows = tape[: max(0, int(limit))]
        return queue
    # Aggregate per wallet FIRST, then apply the per-wallet exclusions: MEASURED on the
    # local database (747k swaps) the exclusions-per-row shape took 26 s, this one 11 s.
    # A tape-only grade does not count as graded here (see TAPE_MODEL_VERSION).
    graded = _HAS_REAL_GRADE_SQL.format(chain="?", wallet="c.wallet")
    cand = fetch_all(
        conn,
        "SELECT * FROM ("
        "  SELECT s.wallet AS wallet, "
        "    COUNT(DISTINCT CASE WHEN s.side = 'buy' THEN s.token END) AS tokens, "
        "    SUM(s.side = 'buy') AS buys, COUNT(*) AS trades, MAX(s.ts_ms) AS last_ms "
        "  FROM swaps s WHERE s.chain = ? AND s.source = ? "
        "  GROUP BY s.wallet HAVING SUM(s.side = 'buy') > 0"
        ") c "
        f"WHERE NOT {graded} "
        "  AND NOT EXISTS (SELECT 1 FROM wallets w WHERE w.chain = ? AND w.address = c.wallet "
        "                  AND COALESCE(w.cohort, '') = 'blacklist') "
        "  AND COALESCE((SELECT updated_ms FROM kv WHERE kv.key = 'ops:wallet_buyers:' || ? || ':' "
        "                || c.wallet), 0) < ?",
        (ch, source, ch, TAPE_MODEL_VERSION, ch, ch, ts - retry_after_ms),
    )
    queue.candidates = len(cand)
    if not cand:
        return queue

    def feed_from_swaps() -> dict[str, int]:
        """Fallback only: a full scan of ``swaps`` (MEASURED 9-47 s locally, cache-dependent)."""
        return {str(r["wallet"]): 1 for r in fetch_all(
            conn, "SELECT DISTINCT wallet FROM swaps WHERE chain = ? AND source LIKE 'gmgn:%'", (ch,))}

    def early() -> dict[str, int]:
        # Tokens first, then the (chain, token, ts_ms) index with the window as a range:
        # MEASURED 0.6 s locally against 25 s for the same join left to the planner.
        return {str(r["wallet"]): int(r["n"]) for r in fetch_all(
            conn,
            "SELECT s.wallet AS wallet, COUNT(DISTINCT s.token) AS n "
            "FROM tokens t CROSS JOIN swaps s ON s.chain = t.chain AND s.token = t.address "
            "  AND s.ts_ms BETWEEN t.created_ms AND t.created_ms + ? "
            "WHERE t.chain = ? AND t.migrated_ms IS NOT NULL AND t.created_ms IS NOT NULL "
            "  AND s.side = 'buy' "
            "GROUP BY s.wallet",
            (GATHER_EARLY_WINDOW_MS, ch),
        )}

    def siblings() -> dict[str, int]:
        return {str(r["wallet"]): int(r["graded"]) for r in fetch_all(
            conn,
            "SELECT em.address AS wallet, "
            "  (SELECT COUNT(*) FROM entity_members em2 JOIN wallet_scores ws "
            "     ON ws.chain = em2.chain AND ws.address = em2.address "
            "   WHERE em2.entity_id = em.entity_id AND ws.grade IN ('A', 'B', 'C')) AS graded "
            "FROM entity_members em WHERE em.chain = ?",
            (ch,),
        ) if int(r["graded"]) > 0}

    def feed_and_tags() -> tuple[dict[str, int], dict[str, int]]:
        """Feed membership and cohort tags from one pass over the feed's own events.

        Every GMGN feed row is also a ``wallet.trade`` event whose payload carries the
        row's ``source`` and ``tags`` (gmgn_feeds.write_swap), so this one scan answers
        both. MEASURED locally: 0.9 s warm against 9-47 s for the equivalent scan of
        ``swaps``, which has no index on ``source``.

        Once ``feed_tags.table_ready``, the same answer comes from the ``wallet_feed_tags``
        rollup (one primary-key prefix scan of a small table), and old ``wallet.trade``
        events may then be pruned -- so the event scan below is the not-yet-ready path only.
        """
        from kaiba.intelligence import feed_tags

        if feed_tags.table_ready(conn):
            rolled: dict[str, list[Any]] = {}
            for tag_row in feed_tags.chain_rows(conn, ch, gmgn_only=True):
                rolled.setdefault(tag_row.address, []).append(tag_row)
            table_feed: dict[str, int] = {}
            table_bits: dict[str, int] = {}
            for wallet, tag_rows in rolled.items():
                fed, b = feed_tags.gather_signals(tag_rows, GATHER_POSITIVE_TAGS, GATHER_NEGATIVE_TAGS)
                if fed:
                    table_feed[wallet] = 1
                if b:
                    table_bits[wallet] = b
            return table_feed, table_bits
        key = (_db_path(conn), ch)
        with _FEED_TAG_LOCK:
            last_id, cached_feed, cached_bits = _FEED_TAG_CACHE.get(key, (0, {}, {}))
            on_feed, bits, max_id = dict(cached_feed), dict(cached_bits), last_id
            for r in fetch_all(
                conn,
                "SELECT e.id AS id, e.subject AS wallet, e.payload AS payload FROM events e "
                "WHERE e.kind = ? AND e.id > ? AND e.chain = ? "
                "  AND e.payload LIKE '%\"source\":\"gmgn:%'",
                (EventKind.WALLET_TRADE.value, last_id, ch),
            ):
                max_id = max(max_id, int(r["id"]))
                wallet = str(r["wallet"])
                on_feed[wallet] = 1
                raw = jload(r["payload"], {}).get("tags") or []
                b = bits.get(wallet, 0)
                for tag in raw if isinstance(raw, list) else []:
                    if tag in GATHER_POSITIVE_TAGS:
                        b |= 1
                    elif tag in GATHER_NEGATIVE_TAGS:
                        b |= 2
                if b:
                    bits[wallet] = b
            _FEED_TAG_CACHE[key] = (max_id, on_feed, bits)
        return on_feed, bits

    def flagged() -> dict[str, int]:
        out = {str(r["key"]).rsplit(":", 1)[-1]: 1 for r in fetch_all(
            conn, "SELECT key FROM kv WHERE key LIKE ?", (f"{GATHER_HOOK_KV_PREFIX}{ch}:%",))}
        if fetch_one(conn, "SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = ?",
                     (GATHER_HOOK_TABLE,)):
            out.update({str(r["address"]): 1 for r in fetch_all(
                conn, f"SELECT address FROM {GATHER_HOOK_TABLE} WHERE chain = ?", (ch,))})
        if not out:
            raise sqlite3.OperationalError(
                f"no {GATHER_HOOK_TABLE} rows and no {GATHER_HOOK_KV_PREFIX}{ch}:* keys")
        return out

    try:
        on_feed, tagged = feed_and_tags()
        queue.signals["feed"] = f"ok:{len(on_feed)}"
        queue.signals["tags"] = f"ok:{len(tagged)}"
    except sqlite3.Error as exc:
        queue.signals["tags"] = f"UNAVAILABLE: {_error_text(exc)}"
        tagged = {}
        on_feed = _gather_signal(queue, "feed", feed_from_swaps)
        if on_feed:
            queue.signals["feed"] += " via swaps (events unreadable)"
    early_n = _gather_signal(queue, "early_graduated", early)
    sibs = _gather_signal(queue, "graded_siblings", siblings)
    flags = _gather_signal(queue, "flagged", flagged)

    min_trades = int(SELL_ONLY_MIN_TRADES)
    max_share = float(BUY_STARVED_MAX_BUY_SHARE)
    rows: list[dict[str, Any]] = []
    starved = 0
    for r in cand:
        wallet = str(r["wallet"])
        trades, buys = int(r["trades"] or 0), int(r["buys"] or 0)
        if trades >= min_trades and buys / trades <= max_share:
            starved += 1
            continue
        bits = tagged.get(wallet, 0)
        row = {
            "wallet": wallet, "tokens": int(r["tokens"] or 0), "buys": buys, "trades": trades,
            "last_ms": int(r["last_ms"] or 0),
            "feed": on_feed.get(wallet, 0), "early_graduated": early_n.get(wallet, 0),
            "graded_siblings": sibs.get(wallet, 0), "tags_positive": bits & 1,
            "tags_negative": 1 if bits & 2 else 0, "flagged": flags.get(wallet, 0),
        }
        row["priority"] = gather_priority(row)
        rows.append(row)
    rows.sort(key=lambda r: (-r["priority"], -r["tokens"], -r["last_ms"], r["wallet"]))
    queue.excluded["buy_starved"] = starved
    seen = {r["wallet"] for r in tape}
    queue.candidates += len(tape)
    queue.rows = (tape + [r for r in rows if r["wallet"] not in seen])[: max(0, int(limit))]
    return queue


#: The fewest closed round trips a tape-profitable candidate needs: the tape grader's own
#: B gate asks for 8 closed trades over 4 tokens, so fewer is noise the paid grade would
#: most likely not confirm.
TAPE_CANDIDATE_MIN_CLOSED = 8
TAPE_CANDIDATE_MIN_WIN_RATE = 0.5


def _tape_profitable_candidates(
    conn: sqlite3.Connection, chain: str, *, limit: int, ts: int, retry_after_ms: int
) -> list[dict[str, Any]]:
    """Wallets our own tape already shows closing profitable round trips, most closed first.

    A tape grade is not a paid grade (see TAPE_MODEL_VERSION), so every row here still
    needs the full-history grade this job buys. Same admission rules as the queue below:
    not blacklisted, not attempted inside ``retry_after_ms``. MEASURED 6.1 s on the box.
    """
    if limit <= 0:
        return []
    rows = fetch_all(
        conn,
        "SELECT ws.address AS wallet, ws.closed_trades AS closed, "
        "  COALESCE(ws.distinct_tokens, 0) AS tokens, ws.scored_at_ms AS last_ms "
        "FROM wallet_scores ws "
        "WHERE ws.chain = ? AND ws.model_version = ? "
        "  AND ws.closed_trades >= ? AND ws.win_rate >= ? "
        "  AND CAST(ws.realized_pnl_usd AS REAL) > 0 "
        "  AND NOT EXISTS (SELECT 1 FROM wallets w WHERE w.chain = ws.chain "
        "                  AND w.address = ws.address AND COALESCE(w.cohort, '') = 'blacklist') "
        "  AND COALESCE((SELECT updated_ms FROM kv WHERE kv.key = 'ops:wallet_buyers:' || ? || ':' "
        "                || ws.address), 0) < ? "
        "ORDER BY ws.closed_trades DESC, CAST(ws.realized_pnl_usd AS REAL) DESC LIMIT ?",
        (chain, TAPE_MODEL_VERSION, TAPE_CANDIDATE_MIN_CLOSED, TAPE_CANDIDATE_MIN_WIN_RATE,
         chain, ts - retry_after_ms, int(limit)),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        closed = int(r["closed"] or 0)
        row = {
            "wallet": str(r["wallet"]), "tokens": int(r["tokens"] or 0), "buys": closed,
            "trades": 2 * closed, "last_ms": int(r["last_ms"] or 0), "tape_profitable": 1,
            "feed": 0, "early_graduated": 0, "graded_siblings": 0, "tags_positive": 0,
            "tags_negative": 0, "flagged": 0,
        }
        row["priority"] = gather_priority(row)
        out.append(row)
    return out


def _gather_grade_many(ctx: JobContext, chain: Chain, wallets: Iterable[str]) -> dict[str, Any]:
    """Chain-aware twin of :func:`_grade_many`; free, local, one wallet's failure is logged."""
    from kaiba.intelligence.grade import grade_address

    grades: dict[str, int] = {}
    failed = 0
    for address in wallets:
        try:
            score = grade_address(address, chain, ctx.conn)
        except Exception as exc:  # noqa: BLE001 - one wallet's grade must not stop the batch
            failed += 1
            log.warning("ops: grading %s on %s failed: %s", address[:12], chain.value, _error_text(exc))
            continue
        grades[score.grade] = grades.get(score.grade, 0) + 1
    return {"graded": sum(grades.values()), "grade_failed": failed, "by_grade": grades}


def free_grade_targets(conn: sqlite3.Connection, chain: Chain, *, limit: int) -> list[str]:
    """Ungraded wallets seen on a GMGN feed for ``chain``, deepest local evidence first.

    For a chain with no Helius history route (BSC on 2026-09-21) the feed rows already in
    ``swaps`` are the only evidence there is, and grading them costs nothing. Deepest
    first because the grader answers UNSCORED under its evidence floor and the point is
    to find the wallets it can actually grade. A tape-only grade does not count as graded
    (see :data:`TAPE_MODEL_VERSION`).

    NOT wired in the shipped schedule (``free_grade_chains`` is absent on purpose): the
    verifier showed a 7-row feed wallet graded to B through this uncapped model, and a B
    is what ``sm-trenches`` sizes on.
    """
    graded = _HAS_REAL_GRADE_SQL.format(chain="s.chain", wallet="s.wallet")
    return [str(r["wallet"]) for r in fetch_all(
        conn,
        "SELECT s.wallet AS wallet, COUNT(*) AS trades FROM swaps s "
        "WHERE s.chain = ? AND s.source LIKE 'gmgn:%' "
        f"  AND NOT {graded} "
        "  AND NOT EXISTS (SELECT 1 FROM wallets w WHERE w.chain = s.chain AND w.address = s.wallet "
        "                  AND COALESCE(w.cohort, '') = 'blacklist') "
        "GROUP BY s.wallet HAVING SUM(s.side = 'buy') > 0 "
        "ORDER BY trades DESC, wallet ASC LIMIT ?",
        (chain.value, TAPE_MODEL_VERSION, max(0, int(limit))),
    )]


def _gather_free_grades(ctx: JobContext) -> dict[str, Any]:
    """Grade feed wallets on chains that have no paid history route. Zero credits.

    Never Solana: a free grade there would write a ``wallet_scores`` row and silently drop
    the wallet out of the paid queue that would have bought it real evidence.
    """
    raw = str(ctx.param("free_grade_chains", "") or "")
    per_run = int(ctx.param("free_grades_per_run", 50))
    out: dict[str, Any] = {}
    for name in [c.strip().lower() for c in raw.split(",") if c.strip()]:
        try:
            chain = Chain(name)
        except ValueError:
            out[name] = {"reason": "unknown_chain"}
            continue
        if chain is Chain.SOL:
            out[name] = {"reason": "sol_has_a_paid_path"}
            continue
        targets = free_grade_targets(ctx.conn, chain, limit=per_run)
        out[name] = {"candidates": len(targets), "credits": 0,
                     **_gather_grade_many(ctx, chain, targets)}
    return out


def job_wallet_buyers(ctx: JobContext) -> dict[str, Any]:
    """Backfill then grade the ungraded buyers on scanned tokens, bound by credits.

    The count quota (``wallets_per_day``) stays as a secondary cap; the binding limit is
    :func:`gather_credit_room`. The queue is :func:`gather_queue`, ordered by free
    signals, and every attempted wallet's mark carries the signals that admitted it so
    yield per signal can be measured later. ``free_grade_chains`` adds a zero-credit
    pass over feed wallets on chains with no Helius route (see :func:`_gather_free_grades`).
    """
    from kaiba.ingest.backfill import backfill_wallets

    per_run = int(ctx.param("wallets_per_run", 3))
    per_day = int(ctx.param("wallets_per_day", 15))
    used_units, used_credits = ctx.quota_used()
    allow = min(per_run, per_day - used_units)
    free = _gather_free_grades(ctx)
    if allow <= 0:
        return {"reason": REASON_QUOTA, "binding": "wallets_per_day", "wallets_per_day": per_day,
                "used_today": used_units, "credits_today": used_credits, "wallets": 0,
                "free_grades": free}
    room = gather_credit_room(
        ctx, daily_default=per_day * CREDITS_PER_WALLET_EST * 2,
        run_max=int(ctx.param("max_credits_per_run", allow * CREDITS_PER_WALLET_EST * 2)),
    )
    allow = min(allow, room.credits // CREDITS_PER_WALLET_EST)
    if allow <= 0:
        return {"reason": REASON_QUOTA, "binding": room.binding, "wallets_per_day": per_day,
                "used_today": used_units, "credits_today": used_credits, "wallets": 0,
                "credit_room": room.as_dict(), "free_grades": free}
    retry_days = float(ctx.param("retry_after_days", 7))
    queue = gather_queue(
        ctx.conn, Chain.SOL, limit=allow, source=str(ctx.param("source", "pumpfun:trades")),
        retry_after_ms=int(retry_days * 86_400_000), now=ctx.now(),
    )
    if not queue.rows:
        return {"reason": "no_ungraded_buyers", "wallets": 0, "used_today": used_units,
                "queue": queue.summary(), "credit_room": room.as_dict(), "free_grades": free}
    addresses = [str(r["wallet"]) for r in queue.rows]
    report = backfill_wallets(
        ctx.conn, Chain.SOL,
        wallets=addresses,
        pages=int(ctx.param("pages", 1)),
        with_meta=bool(ctx.param("with_meta", True)),
        max_credits=min(room.run_max, room.credits),
    )
    attempted = [r.wallet for r in report.results]
    for r in report.results:
        _mark_attempted(ctx.conn, Chain.SOL, r.wallet,
                        f"{r.error or 'backfilled'}|signals={queue.signal_tag(r.wallet)}")
    graded = _grade_many(ctx, [r.wallet for r in report.results if not r.error])
    ctx.quota_add(units=len(attempted), credits=report.credits_spent)
    return {
        "wallets": len(attempted),
        "candidates": queue.candidates,
        "tokens_per_wallet": [int(r["tokens"]) for r in queue.rows][:10],
        "swaps_written": report.swaps_written,
        "first_buyers": report.first_buyers_written,
        "wallets_failed": report.wallets_failed,
        "backfill_credits": report.credits_spent,
        "stopped": report.stopped,
        "used_today": used_units + len(attempted),
        "credits_today": used_credits + report.credits_spent,
        "wallets_per_day": per_day,
        "credit_room": room.as_dict(),
        "queue": queue.summary(),
        "free_grades": free,
        **graded,
    }


def job_wallet_tracked(ctx: JobContext) -> dict[str, Any]:
    """Top up the imported (tracked) wallet list, least-recently-touched first, then regrade.

    Same credit room as ``wallet_buyers``; the list itself stays the operator's order.
    """
    from kaiba.ingest.backfill import backfill_wallets

    per_run = int(ctx.param("wallets_per_run", 1))
    per_day = int(ctx.param("wallets_per_day", 5))
    used_units, used_credits = ctx.quota_used()
    allow = min(per_run, per_day - used_units)
    if allow <= 0:
        return {"reason": REASON_QUOTA, "binding": "wallets_per_day", "wallets_per_day": per_day,
                "used_today": used_units, "credits_today": used_credits, "wallets": 0}
    room = gather_credit_room(
        ctx, daily_default=per_day * CREDITS_PER_WALLET_EST * 2,
        run_max=int(ctx.param("max_credits_per_run", allow * CREDITS_PER_WALLET_EST * 2)),
    )
    allow = min(allow, room.credits // CREDITS_PER_WALLET_EST)
    if allow <= 0:
        return {"reason": REASON_QUOTA, "binding": room.binding, "wallets_per_day": per_day,
                "used_today": used_units, "credits_today": used_credits, "wallets": 0,
                "credit_room": room.as_dict()}
    cohorts_raw = ctx.param("cohorts", "")
    cohorts = [c.strip() for c in str(cohorts_raw).split(",") if c.strip()] or None
    report = backfill_wallets(
        ctx.conn, Chain.SOL,
        limit=allow,
        cohorts=cohorts,
        pages=int(ctx.param("pages", 1)),
        fresh=bool(ctx.param("fresh", True)),
        max_credits=min(room.run_max, room.credits),
    )
    graded = _grade_many(ctx, [r.wallet for r in report.results if not r.error])
    ctx.quota_add(units=len(report.results), credits=report.credits_spent)
    return {
        "wallets": len(report.results),
        "swaps_written": report.swaps_written,
        "wallets_failed": report.wallets_failed,
        "backfill_credits": report.credits_spent,
        "stopped": report.stopped,
        "used_today": used_units + len(report.results),
        "credits_today": used_credits + report.credits_spent,
        "wallets_per_day": per_day,
        "credit_room": room.as_dict(),
        **graded,
    }


def job_wallet_regrade(ctx: JobContext) -> dict[str, Any]:
    """Regrade, for free, every wallet whose evidence moved or whose grade is old.

    Every chain that has a ``wallet_scores`` row, not only Solana: the Robinhood rows
    (MEASURED 49 of ``kaiba-wallet-v1`` on the live box, 2026-09-22, against 15 sol) only
    stay current through this job, and BSC rows would too if ``free_grade_chains`` were
    ever wired (it is not; see :func:`free_grade_targets`). Tape grades are never
    touched (:func:`stale_scores`). ``chains`` narrows it; ``wallets_per_run`` is shared
    across chains, oldest first.
    """
    max_age_days = float(ctx.param("max_age_days", 7))
    limit = int(ctx.param("wallets_per_run", 200))
    raw = str(ctx.param("chains", "") or "")
    names = [c.strip().lower() for c in raw.split(",") if c.strip()] or [
        str(r["chain"]) for r in fetch_all(ctx.conn, "SELECT DISTINCT chain FROM wallet_scores ORDER BY chain")
    ]
    grades: dict[str, int] = {}
    per_chain: dict[str, dict[str, Any]] = {}
    candidates = changed = graded = failed = 0
    for name in names:
        if limit <= 0:
            break
        try:
            chain = Chain(name)
        except ValueError:
            per_chain[name] = {"reason": "unknown_chain"}
            continue
        targets = stale_scores(
            ctx.conn, chain, max_age_ms=int(max_age_days * 86_400_000), limit=limit, now=ctx.now()
        )
        if not targets:
            per_chain[name] = {"candidates": 0}
            continue
        marks = ",".join("?" for _ in targets)
        before = {
            str(r["address"]): str(r["grade"]) for r in fetch_all(
                ctx.conn,
                f"SELECT address, grade FROM wallet_scores WHERE chain = ? AND address IN ({marks})",
                (chain.value, *targets),
            )
        }
        out = _gather_grade_many(ctx, chain, targets)
        after = {
            str(r["address"]): str(r["grade"]) for r in fetch_all(
                ctx.conn,
                f"SELECT address, grade FROM wallet_scores WHERE chain = ? AND address IN ({marks})",
                (chain.value, *targets),
            )
        }
        moved = sum(1 for a in targets if before.get(a) != after.get(a))
        per_chain[name] = {"candidates": len(targets), "changed": moved, **out}
        candidates += len(targets)
        changed += moved
        graded += int(out["graded"])
        failed += int(out["grade_failed"])
        for g, n in out["by_grade"].items():
            grades[g] = grades.get(g, 0) + n
        limit -= len(targets)
    return {"candidates": candidates, "changed": changed, "graded": graded, "grade_failed": failed,
            "by_grade": grades, "per_chain": per_chain}


def job_gmgn_grade(ctx: JobContext) -> dict[str, Any]:
    """Re-grade the GMGN provider-grade pool on a call budget (``gmgn_grade.run_budgeted``).

    Journal #4987: nothing scheduled this pass after the 2026-09-24 bulk run, which left
    628 robinhood wallets UNSCORED one evidence point under the floor for want of a win rate
    that GMGN now answers. Each run spends at most ``max_calls_per_run`` GMGN reads at
    DISCOVERY priority, ``pace_s`` apart, and the UTC day at most ``max_calls_per_day``
    (``ops_quota``, so a restart cannot reset it). Chains are visited in the order given and
    share the run's budget. Every chain resumes from its own ``kv`` cursor. No threshold is
    touched: grades are OUR rubric over GMGN's arithmetic, under ``kaiba-wallet-gmgn-v1``.
    """
    from kaiba.core.limiter import Priority
    from kaiba.intelligence import gmgn_grade

    raw = str(ctx.param("chains", "robinhood") or "")
    names = [c.strip().lower() for c in raw.split(",") if c.strip()]
    per_run = int(ctx.param("max_calls_per_run", 120))
    per_day = int(ctx.param("max_calls_per_day", 1200))
    used, _ = ctx.quota_used()
    budget = max(0, min(per_run, per_day - used))
    result: dict[str, Any] = {"budget": budget, "used_today_before": used, "per_chain": {}}
    if budget < 2:
        return {**result, "reason": REASON_QUOTA, "calls": 0}
    spent = 0
    for name in names:
        try:
            chain = Chain(name)
        except ValueError:
            result["per_chain"][name] = {"stopped": "unknown_chain"}
            continue
        left = budget - spent
        if left < 2:
            result["per_chain"][name] = {"stopped": "budget"}
            continue
        out = gmgn_grade.run_budgeted(
            ctx.conn, chain, max_calls=left, period=str(ctx.param("period", "all")),
            priority=Priority.DISCOVERY, pace_s=float(ctx.param("pace_s", 2.0)),
            deadline_monotonic=time.monotonic() + max(5.0, ctx.time_left_s() - 15.0),
            store=bool(ctx.param("store", True)),
        )
        spent += int(out["calls"])
        ctx.quota_add(units=int(out["calls"]))
        result["per_chain"][name] = out
        if out["stopped"] in ("deadline", "provider_unavailable"):
            break
    result["calls"] = spent
    return result


# --------------------------------------------------------------------------------------
# evidence deepening: more history for the few paid grades within reach of A
# --------------------------------------------------------------------------------------
#
# WHY THIS EXISTS, AND WHY IT IS SMALL (MEASURED on the live box, 2026-10-02, read-only).
#
# The brief was "1,600 wallets score >= 70 and are capped only by evidence". They are not
# high-quality wallets short of evidence. Their score is ONE component normalised to 100:
# of 1,489 sol and 760 robinhood tape grades at >= 60, every one carries seed_confluence
# 12/12 and nothing else measured (sol: 1,238 have 0 closed round trips; 88 = 100 minus the
# no_sells penalty; robinhood 65 = 100 minus the 35-point realized-loss penalty). GMGN's
# all-time arithmetic for a random 100 of the sol ones: median 1,023 buys, ROI -86%, 0 of
# 100 above +15%. They buy everything, so they hold three "seed" tokens by count.
#
# Where full evidence already exists the bar binds on QUALITY: 979 paid sol grades with
# evidence_weight >= 90, best pre-penalty score 65.4, none reaches A_MIN_SCORE. So the one
# place more pages can change an A verdict is a paid grade near the A score whose history
# walk has not reached the wallet's first transaction -- which since grade.history_truncated
# is itself an A gate. That set was 1 wallet on 2026-10-02 (6 before the min_closed rule).
#
# Each pass walks ``pages_per_wallet`` more Enhanced pages BACKWARD from the stored cursor
# (no meta pass: signer/lookup facts are clustering's input, not the grade's), regrades at
# once, and marks the wallet so the next pass waits ``retry_after_hours``. A wallet leaves
# the set by itself when the new history pulls its score under the line, when its walk is
# exhausted, or at ``max_pages`` -- beyond that the wallet is a high-frequency trader whose
# complete history we will not buy, and it stays capped at B by the truncation gate.

#: How far under ``grade.A_MIN_SCORE`` a paid grade may sit and still be worth more pages.
#: INVENTED: one decile of the score. Deeper history moves a score both ways; a wallet 10
#: points short needs a large move, and the pass that does not make it drops it.
DEEPEN_SCORE_MARGIN = 10.0
#: The fewest closed round trips a candidate needs. INVENTED floor, MEASURED reason: the
#: five 2026-10-02 candidates with 0 closed held 583-1,708 tokens with no exit seen -- the
#: spray-buyer shape whose GMGN all-time ROI was -86% at the median in the pilot.
DEEPEN_MIN_CLOSED = 1
#: Planning cost of one Enhanced page without the meta pass. ``helius.CREDIT_COSTS`` charges
#: 100 for the route; the scheduler's rule is to plan with the higher plausible figure.
DEEPEN_PAGE_CREDITS_EST = 100
#: ``kv`` prefix of a deepening attempt mark (per chain and wallet).
DEEPEN_MARK_PREFIX = "ops:wallet_deepen:"


def deepen_targets(
    conn: sqlite3.Connection,
    chain: Chain = Chain.SOL,
    *,
    min_score: float,
    min_closed: int = DEEPEN_MIN_CLOSED,
    max_pages: int,
    limit: int,
    retry_after_ms: int,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Paid full-history grades near the A score whose history walk can still go deeper.

    Paid means ``grade.MODEL_ID`` with a Helius cursor: a tape or provider grade cannot be
    an A by construction, so pages bought for one buy no A. Highest score first. One
    indexed range on ``idx_wallet_scores_grade (grade, score DESC)`` and a key lookup per
    row, never a walk of the chain's scores.
    """
    from kaiba.intelligence.grade import BACKFILL_CURSOR_PREFIX, MODEL_ID

    ts = now if now is not None else now_ms()
    rows = fetch_all(
        conn,
        "SELECT ws.address AS wallet, ws.score AS score, ws.grade AS grade, "
        "  ws.evidence_weight AS evidence_weight, ws.closed_trades AS closed, kv.value AS cursor "
        "FROM wallet_scores ws INDEXED BY idx_wallet_scores_grade "
        "JOIN kv ON kv.key = ? || ws.chain || ':' || ws.address "
        # B or C only: an UNSCORED row (evidence_weight < grade.MIN_EVIDENCE_WEIGHT) has no
        # opinion to deepen, and an A or QUARANTINED row has nothing to gain.
        "WHERE ws.grade IN ('B', 'C') AND ws.score >= ? AND ws.chain = ? AND ws.model_version = ? "
        "  AND COALESCE(ws.closed_trades, 0) >= ? "
        "  AND COALESCE((SELECT k2.updated_ms FROM kv k2 WHERE k2.key = ? || ws.chain || ':' "
        "                || ws.address), 0) < ? "
        "ORDER BY ws.score DESC, ws.address ASC",
        (BACKFILL_CURSOR_PREFIX, float(min_score), chain.value, MODEL_ID, int(min_closed),
         DEEPEN_MARK_PREFIX, ts - int(retry_after_ms)),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        cursor = jload(r["cursor"], {})
        if not isinstance(cursor, dict) or cursor.get("exhausted"):
            continue
        pages = int(cursor.get("pages") or 0)
        if pages >= max_pages:
            continue
        out.append({"wallet": str(r["wallet"]), "score": float(r["score"]), "grade": str(r["grade"]),
                    "closed": int(r["closed"] or 0), "pages": pages})
        if len(out) >= max(0, int(limit)):
            break
    return out


def job_wallet_deepen(ctx: JobContext) -> dict[str, Any]:
    """Walk more history for paid grades within reach of A, then regrade them at once.

    Bound by credits like ``wallet_buyers`` (:func:`gather_credit_room`, under this job's
    own ``credits_per_day``), Helius calls at the provider client's RESEARCH priority, the
    lowest the limiter has. Solana only: no other chain has a paid history route.
    """
    from kaiba.ingest.backfill import backfill_wallets
    from kaiba.intelligence.grade import A_MIN_SCORE

    chain = Chain(str(ctx.param("chain", Chain.SOL.value)))
    if chain is not Chain.SOL:
        return {"reason": "no_history_route", "chain": chain.value, "wallets": 0}
    per_run = int(ctx.param("wallets_per_run", 2))
    pages = max(1, int(ctx.param("pages_per_wallet", 5)))
    max_pages = int(ctx.param("max_pages", 20))
    min_score = A_MIN_SCORE - float(ctx.param("score_margin", DEEPEN_SCORE_MARGIN))
    retry_ms = int(float(ctx.param("retry_after_hours", 6)) * 3_600_000)
    targets = deepen_targets(
        ctx.conn, chain, min_score=min_score, min_closed=int(ctx.param("min_closed", DEEPEN_MIN_CLOSED)),
        max_pages=max_pages, limit=max(0, per_run), retry_after_ms=retry_ms, now=ctx.now(),
    )
    base = {"candidates": len(targets), "min_score": min_score, "max_pages": max_pages}
    if not targets:
        return {**base, "reason": "no_candidates", "wallets": 0}
    _, used_credits = ctx.quota_used()
    page_cost = DEEPEN_PAGE_CREDITS_EST
    room = gather_credit_room(
        ctx, daily_default=int(ctx.param("credits_per_day", 1_000)),
        run_max=int(ctx.param("max_credits_per_run", per_run * pages * page_cost)),
    )
    budget = min(room.run_max, room.credits)
    plan: list[tuple[dict[str, Any], int]] = []
    for t in targets:
        n = min(pages, max_pages - t["pages"], (budget - sum(p for _, p in plan) * page_cost) // page_cost)
        if n <= 0:
            break
        plan.append((t, n))
    if not plan:
        return {**base, "reason": REASON_QUOTA, "binding": room.binding, "wallets": 0,
                "credits_today": used_credits, "credit_room": room.as_dict()}

    before = {t["wallet"]: (t["grade"], t["score"]) for t, _ in plan}
    walked: list[dict[str, Any]] = []
    spent = first_buyers = 0
    for t, n in plan:
        # One wallet per call because the page count differs per wallet. The first-buyer
        # rebuild inside is scoped to the tokens this walk wrote buys for (backfill's
        # ``buy_tokens``), so a per-wallet call costs no chain-wide re-sort.
        report = backfill_wallets(
            ctx.conn, chain, wallets=[t["wallet"]], pages=n, with_meta=False,
            max_credits=max(0, budget - spent),
        )
        spent += report.credits_spent
        first_buyers += report.first_buyers_written
        for r in report.results:
            walked.append({"wallet": r.wallet, "pages": r.pages, "swaps_written": r.swaps_written,
                           "exhausted": r.exhausted, "error": r.error})
        if report.stopped:
            break
    graded = _gather_grade_many(ctx, chain, [w["wallet"] for w in walked if not w["error"]])
    after = {
        str(r["address"]): (str(r["grade"]), float(r["score"])) for r in fetch_all(
            ctx.conn,
            f"SELECT address, grade, score FROM wallet_scores WHERE chain = ? AND address IN "
            f"({','.join('?' for _ in walked) or 'NULL'})",
            (chain.value, *[w["wallet"] for w in walked]),
        )
    }
    ts = now_ms()
    for w in walked:
        prev, now_g = before.get(w["wallet"]), after.get(w["wallet"])
        w["grade"] = f"{prev[0]}->{now_g[0] if now_g else '?'}" if prev else None
        w["score"] = f"{prev[1]:.1f}->{now_g[1]:.1f}" if prev and now_g else None
        ctx.conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (f"{DEEPEN_MARK_PREFIX}{chain.value}:{w['wallet']}", jdump(w), ts),
        )
    ctx.quota_add(units=len(walked), credits=spent)
    return {
        **base,
        "wallets": len(walked),
        "pages": sum(w["pages"] for w in walked),
        "credits": spent,
        "credits_today": used_credits + spent,
        "first_buyers": first_buyers,
        "walked": [{**w, "wallet": w["wallet"][:12]} for w in walked],
        "credit_room": room.as_dict(),
        **graded,
    }


def job_signals(ctx: JobContext) -> dict[str, Any]:
    """Poll the due early-alpha sources, one call per source, least-recently-tried first.

    ``signals.refresh()`` with no ``only`` polls every due source in one sequential pass,
    which makes the pass all-or-nothing: health is written per source as each finishes, so
    a pass killed by the job timeout records nothing, every source is still due next run,
    and the same slow source leads again.

    MEASURED on 2026-09-20, cold start, all nine sources due: the single pass took **332 s
    and did succeed** (600 s timeout), but crt.sh alone held roughly the first 250 s of it
    after being rate limited into a 60 s family cooldown — at 251 s in, zero of the nine
    had been recorded. So the margin against the timeout was under 2x, and the duration is
    set by someone else's rate limiter rather than by us.

    Two changes remove the all-or-nothing property, both here rather than in the hunter:
    poll one source per call, so a slow one costs the others a turn rather than the whole
    run's record; and order by when *we* last tried a source (not only by what it managed
    to record), so a source that consumed a run goes last on the next one. The deadline
    guard leaves a run time to return and record what it did instead of being killed
    mid-source.
    """
    from kaiba.hunters import signals

    cfg = signals.load_config()
    due = list(signals.due_sources(ctx.conn, cfg))
    tried = {
        str(r["key"]).rsplit(":", 1)[-1]: int(r["updated_ms"])
        for r in fetch_all(ctx.conn, "SELECT key, updated_ms FROM kv WHERE key LIKE 'ops:signals_attempt:%'")
    }
    polled_ms = {
        str(r["source"]): int(r["last_poll_ms"] or 0)
        for r in fetch_all(ctx.conn, "SELECT source, last_poll_ms FROM alpha_source_health")
    }
    due.sort(key=lambda n: max(tried.get(n, 0), polled_ms.get(n, 0)))

    reserve = float(ctx.param("per_source_reserve_s", 30))
    new = 0
    polled: list[str] = []
    for name in due:
        if ctx.time_left_s() < reserve:
            break
        ts = ctx.now()
        ctx.conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (f"ops:signals_attempt:{name}", jdump({"started_ms": ts}), ts),
        )
        ctx.conn.commit()  # the marker must survive this run being killed mid-source
        new += int(signals.refresh(ctx.conn, only=[name], cfg=cfg))
        polled.append(name)

    health = signals.source_health(ctx.conn)
    states: dict[str, int] = {}
    for row in health:
        states[row["state"]] = states.get(row["state"], 0) + 1
    return {
        "new": new,
        "polled": polled,
        "deferred": [n for n in due if n not in polled],
        "sources": len(health),
        "states": states,
        "dead": [r["source"] for r in health if r["state"] == "dead"],
    }


def job_trade_flow(ctx: JobContext) -> dict[str, Any]:
    """Follow-up trade collection for tokens tier 1 scanned recently."""
    from kaiba.ingest import token_flow

    lookback_s = int(ctx.param("lookback_s", 7200))
    refresh_s = int(ctx.param("refresh_s", 600))
    per_run = int(ctx.param("tokens_per_run", 20))
    max_pages = int(ctx.param("max_pages", 2))
    targets = flow_targets(
        ctx.conn, Chain.SOL, lookback_ms=lookback_s * 1000, refresh_ms=refresh_s * 1000,
        limit=per_run, source=token_flow.SOURCE, now=ctx.now(),
    )
    # A mint with no pump.fun trade history answers "unavailable" too, so a short streak
    # is not evidence of an outage. MEASURED 2026-09-20: 6 of 10 scanned tokens were
    # unavailable on a pass that nonetheless wrote 601 rows from the other 4, and a
    # 3-in-a-row breaker then tripped on two later runs and stopped them doing the work
    # that would have succeeded. The breaker is for "the route is down", so it only trips
    # when nothing at all has worked this run, or on a streak long past coincidence.
    streak_limit = int(ctx.param("unavailable_streak", 5))
    tokens = pages = written = unavailable = ok_tokens = 0
    consecutive_unavailable = 0
    stopped: str | None = None
    for t in targets:
        # Each page is one request at one per second; leave the hard timeout a margin.
        if ctx.time_left_s() < max_pages * 2.0 + 5.0:
            stopped = "deadline"
            break
        token = str(t["token"])
        last = int(t["last_ms"]) if t["last_ms"] is not None else None
        created = int(t["created_ms"]) if t["created_ms"] is not None else None
        result = token_flow.collect_trades(
            Chain.SOL, token, ctx.conn, since_ms=last, created_ms=created, max_pages=max_pages,
        )
        tokens += 1
        pages += result.pages
        written += result.rows_written
        if result.reason == "unavailable":
            unavailable += 1
            consecutive_unavailable += 1
            if consecutive_unavailable >= streak_limit or (ok_tokens == 0 and consecutive_unavailable >= 3):
                stopped = "provider_unavailable"
                break
        else:
            ok_tokens += 1
            consecutive_unavailable = 0
    return {"candidates": len(targets), "tokens": tokens, "ok_tokens": ok_tokens, "pages": pages,
            "rows_written": written, "unavailable": unavailable, "stopped": stopped}


def job_snapshot_prune(ctx: JobContext) -> dict[str, Any]:
    from kaiba.ingest import token_flow

    deleted = int(token_flow.prune_snapshots(ctx.conn))
    row = fetch_one(ctx.conn, "SELECT COUNT(*) AS n FROM curve_snapshots")
    return {"deleted": deleted, "remaining": int(row["n"]) if row else 0}


def job_radar(ctx: JobContext) -> dict[str, Any]:
    """Discovery radar: the next venue, launchpad or chain, before a human has to look.

    ``kaiba/hunters/radar.py`` is 2,128 lines and had **no caller anywhere in the tree**
    -- not the scheduler, not the CLI, not MCP. It was written precisely because nothing
    here notices a new venue: pump.fun was hardcoded, Robinhood Chain and Pons each cost
    a 24-hour research agent, and StonkFun was found roughly seven weeks after it started
    trading, by which point it was 23.9% of the Solana launchpad fee pool.

    ``sweep`` is built for this caller specifically. It keeps its own per-layer due times
    (so this job's interval is a floor, not the poll rate), and it never raises -- a
    failing layer is recorded in ``radar_layer_health`` and the other layers still run.
    """
    from kaiba.hunters import radar

    result = radar.sweep(ctx.conn, now=ctx.now())
    return {
        "requests": result.requests,
        "candidates": int(result.seen),
        "reported": getattr(result, "reported", 0),
        "layers_failed": sorted(result.errors),
    }


def job_hunters(ctx: JobContext) -> dict[str, Any]:
    """Airdrop, NFT and listing registries. Each refreshes independently.

    These three had no schedule. They were reachable only if a human typed the CLI
    command or Hermes chose to call ``kaiba_run_hunter``, which made "automated airdrop
    hunting" true only while somebody was watching. One hunter failing must not stop the
    others, so each is caught separately.

    Each receipt is cut to its counts and per-source states. MEASURED 2026-10-02: the
    full receipts came to 2,149 chars, over ``_small``'s 2,000 cap, so every hunters run
    on the box was stored as ``{"truncated": true}`` and its outcome was unreadable from
    the scheduler's own record. Self-contained on purpose (no module helper): the hunter
    scheduler test executes this function's source on its own.
    """
    import importlib

    keep = ("outcome", "written", "new_count", "qualified_count", "distinct_count",
            "refused_count", "funded_action_authorized", "reason")

    def receipt(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        small: dict[str, Any] = {k: result[k] for k in keep if k in result}
        if isinstance(result.get("sources"), dict):
            small["sources"] = {
                name: {"state": s.get("state"), "parsed_count": s.get("parsed_count"),
                       **({"error": str(s["error"])[:80]} if s.get("error") else {})}
                if isinstance(s, dict) else s
                for name, s in result["sources"].items()
            }
        return small

    out: dict[str, Any] = {}
    for kind, module in (
        ("airdrop", "kaiba.hunters.airdrops"),
        ("nft", "kaiba.hunters.nft"),
        ("listing", "kaiba.hunters.listings"),
    ):
        try:
            mod = importlib.import_module(module)
            refresh = getattr(mod, "refresh_report", None) or getattr(mod, "refresh", None) or getattr(mod, "sweep", None)
            if refresh is None:
                out[kind] = "no refresh entry point"
                continue
            out[kind] = receipt(refresh(ctx.conn))
        except Exception as exc:  # noqa: BLE001 - one dead hunter is not an outage
            log.warning("hunter %s failed: %s", kind, exc)
            out[kind] = f"failed: {type(exc).__name__}"
    return out


def job_execute_planned(ctx: JobContext) -> dict[str, Any]:
    """Submit live orders the engine has planned, then reconcile what is in flight.

    THE MISSING HALF OF EXECUTION. ``engine.handoff`` writes a ``planned`` row for a live
    decision and stops, by design: deciding and spending are meant to be separately
    auditable. But nothing ever read those rows back. ``executor.unresolved_orders``
    selects only UNKNOWN/SUBMITTING/SUBMITTED, and ``executor.submit`` had exactly one
    caller in the tree -- ``watchdog.py``, the *exit* path. So exits could fire and
    entries could not: every live buy stopped at ``planned`` forever. Found 2026-09-21
    while auditing why an armed box had never opened a position.

    Safety comes from the functions this calls, not from here. ``executor.submit`` ->
    ``_check_mode`` re-reads the risk config at submit time and refuses the buy if the
    lane is off/shadow, the kill switch is set, reduce-only is on, or entries are paused;
    ``_authorize`` fails closed against the signer policy. This job therefore cannot widen
    anything: it can only deliver an order to a gate that may still refuse it. That is
    deliberate -- the mode is re-checked at submit rather than trusted from plan time,
    because a lane can be pulled to shadow between the two.

    Stale plans are abandoned rather than sent. An order planned an hour ago was sized
    against a price and a liquidity depth that no longer exist, and submitting it is a
    market order into a book nobody looked at. ``max_plan_age_s`` is the freshness bound.
    """
    from kaiba.core.db import fetch_all
    from kaiba.core.schemas import Chain, Lane, LaneMode, Order, OrderState, Side
    from kaiba.execution import executor as ex

    max_age_s = int(ctx.param("max_plan_age_s", 300))
    max_batch = int(ctx.param("max_submits_per_run", 3))
    now = ctx.now()

    planned = fetch_all(
        ctx.conn,
        "SELECT * FROM orders WHERE state=? ORDER BY created_ms",
        (OrderState.PLANNED.value,),
    )

    submitted: list[str] = []
    refused: list[dict[str, str]] = []
    abandoned: list[str] = []

    for row in planned:
        if len(submitted) >= max_batch:
            break
        age_s = (now - int(row["created_ms"])) / 1000.0
        if age_s > max_age_s:
            # Too old to trust the size it was given. Mark it and move on; the decision
            # stays in the record as planned-then-abandoned, which is the honest outcome.
            ctx.conn.execute(
                "UPDATE orders SET state=?, error=?, updated_ms=? WHERE order_id=?",
                (OrderState.EXPIRED.value, f"plan stale: {age_s:.0f}s > {max_age_s}s",
                 now, row["order_id"]),
            )
            ctx.conn.execute(
                "INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
                (row["order_id"], now, OrderState.EXPIRED.value,
                 f"abandoned by ops.execute_planned: stale by {age_s:.0f}s"),
            )
            abandoned.append(row["order_id"])
            continue

        order = Order(
            order_id=row["order_id"], decision_id=row["decision_id"], chain=Chain(row["chain"]),
            token=row["token"], side=Side(row["side"]), lane=Lane(row["lane"]),
            mode=LaneMode(row["mode"]), input_token=row["input_token"],
            output_token=row["output_token"], amount_in=int(row["amount_in"]),
            min_out=int(row["min_out"]), slippage_bps=row["slippage_bps"],
            state=OrderState(row["state"]), provider=row["provider"],
            provider_order_id=row["provider_order_id"], tx_hash=row["tx_hash"],
            created_ms=row["created_ms"], updated_ms=row["updated_ms"],
        )
        try:
            result = ex.submit(order, ctx.conn)
            submitted.append(f"{order.order_id}:{result.state.value}")
        except ex.ExecutionRefused as exc:
            # A refusal is the gate doing its job, not an outage. Record and continue.
            refused.append({"order_id": order.order_id, "reason": str(exc)[:200]})
        except Exception as exc:  # noqa: BLE001 - one bad order is not an outage
            log.warning("execute_planned: %s failed: %s", order.order_id, exc)
            refused.append({"order_id": order.order_id, "reason": f"{type(exc).__name__}: {exc}"[:200]})

    # Then resolve anything already in flight, so SUBMITTED does not pile up unwatched.
    reconciled: dict[str, str] = {}
    try:
        reconciled = {k: (v.value if hasattr(v, "value") else str(v))
                      for k, v in ex.reconcile_all(ctx.conn).items()}
    except Exception as exc:  # noqa: BLE001
        log.warning("execute_planned: reconcile_all failed: %s", exc)
        reconciled = {"error": f"{type(exc).__name__}"}

    return {
        "planned_seen": len(planned),
        "submitted": submitted,
        "refused": refused,
        "abandoned_stale": abandoned,
        "reconciled": reconciled,
    }


def job_copy_manager(ctx: JobContext) -> dict[str, Any]:
    """Manage the owner's GMGN copy trades on HIS wallet (kaiba.execution.copy_manager).

    Owner, 2026-09-30: trim winners, sell to protect profit, cut losers on a weak trend,
    never touch the copy settings. Owner, 2026-10-03: the copy book is on 0x7243..., "do not
    buy" there, "control the copy trade sell before it rug".

    ``wallet`` is REQUIRED and is never Kaiba's own (``CM.wallet_refusal``). Holdings, the
    balance read and every sell use it. A configured ``live: true`` runs dry, and says
    ``wallet_not_api_bound``, until GMGN's ``portfolio info`` lists the wallet for the chain
    (read at most every ``binding_ttl_s``, DISCOVERY priority): GMGN refuses ``--from`` a
    wallet the API key is not bound to.
    """
    import dataclasses
    from decimal import Decimal

    from kaiba.core.limiter import Priority
    from kaiba.execution import copy_manager as CM
    from kaiba.execution import watchdog
    from kaiba.execution.policy import get_policy
    from kaiba.providers import gmgn_cli, native_price

    chain = Chain(str(ctx.param("chain", "robinhood")))
    cfg = CM.CopyConfig.from_params(ctx.params)
    wallet = str(ctx.param("wallet", "") or "").strip().lower()
    kaiba_wallet = watchdog.exit_wallet_for(chain)
    refused = CM.wallet_refusal(chain, wallet, kaiba_wallet=kaiba_wallet, owned=get_policy().owned(chain))
    if refused:
        raise JobFailed(f"copy_manager refuses {wallet[:10] or '(no wallet)'}: {refused}")

    blocked: list[str] = []
    alarms: list[str] = []
    info = gmgn_cli.account_info(priority=Priority.DISCOVERY, ttl_s=float(ctx.param("binding_ttl_s", 3600)))
    if info.ok:
        binding = CM.parse_binding(info.data, chain, wallet, kaiba_wallet)
    else:
        binding = CM.Binding(None, None, detail=f"portfolio info unavailable: {getattr(info.receipt, 'note', '')}"[:200])
    if binding.bound is not True:
        blocked.append(CM.BLOCK_NOT_BOUND if binding.bound is False else CM.BLOCK_BINDING_UNKNOWN)
    if binding.kaiba_bound is False:
        # Binding the copy wallet must ADD it, not replace Kaiba's. If this fires, every Kaiba
        # trade and EXIT on the chain is about to be refused by the venue. Reported loudly; it
        # does not stop the owner's copy protection, which would help nothing.
        log.error("ops: copy_manager: Kaiba's own %s wallet is no longer bound to the GMGN API key "
                  "(bound now: %s)", chain.value, ", ".join(binding.wallets_on_chain) or "none")
        alarms.append("kaiba_wallet_not_api_bound")
    configured_live = cfg.live
    if cfg.live and blocked:
        cfg = dataclasses.replace(cfg, live=False)

    def fetch() -> Any:
        res = gmgn_cli.portfolio_holdings(wallet, chain, limit=50, order_by="usd_value", direction="desc")
        if not res.ok:
            raise JobFailed(f"gmgn holdings unavailable: {getattr(res.receipt, 'note', '')}"[:300])
        return res.data

    def native() -> Decimal | None:
        for c in ((chain, Chain.ETH) if chain is Chain.ROBINHOOD else (chain,)):
            sample = native_price.latest(c, ctx.conn)
            if sample is not None and ctx.now() - int(sample.ts_ms) < 600_000:
                return Decimal(str(sample.price_usd))
        return None

    slippage = int(watchdog._exit_slippage_bps())
    report = CM.run(
        ctx.conn, chain, cfg, fetch_holdings=fetch,
        wallet_units=lambda token, decimals: watchdog.wallet_token_units(chain, wallet, token, decimals),
        native_usd=native, submit=CM.gmgn_submitter(ctx.conn, chain, slippage, wallet=wallet),
        slippage_bps=slippage, now=ctx.now(), wallet=wallet, blocked=blocked, binding=binding.as_dict(),
    )
    return {**report.as_dict(), "configured_live": configured_live, "alarms": alarms}


def job_native_price(ctx: JobContext) -> dict[str, Any]:
    """One sampling pass per configured chain, via ``native_price.run_sampler``.

    ``run_sampler`` is that module's own loop; ``max_iterations=1`` runs exactly one pass
    over the chains and returns without its trailing sleep, which makes this scheduler the
    clock rather than a second one inside the job. Keyless (DexScreener), so no credits.

    The module is only listed in ``WRAPPED_NATIVE`` for sol/eth/bsc/base; a chain the
    operator names that has no reference pool is reported rather than silently skipped.
    """
    try:
        from kaiba.providers import native_price
    except ImportError as exc:
        raise EntryPointMissing(f"kaiba.providers.native_price not importable: {exc}") from exc
    if not callable(getattr(native_price, "run_sampler", None)):
        raise EntryPointMissing("native_price exposes no run_sampler()")
    wanted = ctx.param("chains", ["sol"])
    names = [c.strip() for c in (wanted.split(",") if isinstance(wanted, str) else wanted) if str(c).strip()]
    chains: list[Chain] = []
    unknown: list[str] = []
    for name in names:
        try:
            chain = Chain(str(name))
        except ValueError:
            unknown.append(str(name))
            continue
        if chain in native_price.WRAPPED_NATIVE:
            chains.append(chain)
        else:
            unknown.append(chain.value)
    if not chains:
        raise JobFailed(f"no sampleable chain in {names}", {"unsupported": unknown})
    report = native_price.run_sampler(
        chains, conn=ctx.conn, max_iterations=1,
        wait_for_slot_s=float(ctx.param("wait_for_slot_s", native_price.DEFAULT_WAIT_FOR_SLOT_S)),
    )
    out: dict[str, Any] = {
        "recorded": report.recorded, "duplicates": report.duplicates, "failures": report.failures,
        "per_chain": report.per_chain,
    }
    if unknown:
        out["unsupported"] = unknown
    if report.recorded == 0 and report.duplicates == 0 and report.failures:
        raise JobFailed(f"no native price sampled on any of {[c.value for c in chains]}", out)
    return out


def job_budget_check(ctx: JobContext) -> dict[str, Any]:
    from kaiba.providers import helius

    st = helius.budget_status(conn=ctx.conn)
    floor = int(ctx.config.helius.floor_credits)
    below = int(st.get("remaining", 0)) < floor
    out = {k: st.get(k) for k in ("period", "allowance", "used", "remaining", "pct_used", "calls",
                                  "denied_calls", "estimated_credits", "resets_in_s")}
    out.update({"floor": floor, "below_floor": below})
    if below:
        emit(
            EventKind.PROVIDER_BUDGET,
            {"provider": "helius", "state": "below_ops_floor", **out},
            level="warn",
            dedupe_key=f"ops:helius:floor:{st.get('period')}:{utc_day(ctx.now())}",
            conn=ctx.conn,
        )
    return out


def job_journal_verify(ctx: JobContext) -> dict[str, Any]:
    from kaiba.core import journal

    ok, err = journal.verify(ctx.conn)
    row = fetch_one(ctx.conn, "SELECT COUNT(*) AS n FROM journal")
    total = int(row["n"]) if row else 0
    if not ok:
        raise JobFailed(f"journal chain broken: {err}", {"entries": total, "chain_ok": False})
    return {"entries": total, "chain_ok": True}


def job_validate_power(ctx: JobContext) -> dict[str, Any]:
    from kaiba.learning import validation

    report = validation.power_report(ctx.conn)
    if bool(ctx.param("record", True)):
        report.record(ctx.conn)
        ctx.conn.commit()
    return {
        "verdict": report.verdict.value,
        "closed_trades": report.closed_trades,
        "trades_needed_deflated": report.trades_needed_deflated,
        "trials": report.trials,
        "headline": str(report.headline)[:160],
    }


def job_tracker_sweep(ctx: JobContext) -> dict[str, Any]:
    """Route T: one free pass of the per-token tape over the hottest mints.

    Keyless (pump.fun), so it is never gated on the credit floor. MEASURED 2026-09-20 over
    915 consecutive polls: 2.1 s of wall clock per mint and 87.5% availability — 30 mints
    is about 63 s, which is why the interval is five minutes rather than one.
    """
    from dataclasses import replace

    from kaiba.intelligence import tracker

    config = tracker.DEFAULT_CONFIG
    mints = int(ctx.param("mints", config.token_sweep_size))
    if mints != config.token_sweep_size:
        config = replace(config, token_sweep_size=mints)
    results = tracker.sweep_tokens(Chain.SOL, ctx.conn, config=config)
    ok = sum(1 for r in results if r.ok)
    detections = sum(len(r.detections) for r in results)
    out = {
        "route": tracker.ROUTE_PUMPFUN,
        "mints": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "rows_seen": sum(r.rows_seen for r in results),
        "detections": detections,
        "credits": sum(r.credits for r in results),
    }
    # The route answering nothing at all is an outage; a minority of 503s is its normal
    # 87.5% availability and must not look like one.
    if results and ok == 0:
        raise JobFailed("every mint poll failed", out)
    return out


def job_tracker_poll(ctx: JobContext) -> dict[str, Any]:
    """Route W: one paid Helius page per active watched wallet, 10 credits each.

    The interval is the whole finding of the tracking work: at 10 credits a poll, ten
    wallets polled every 260 s is the Helius free tier's ceiling for this route
    (``tracker.max_free_tier_wallets``). ``config/schedule.yaml`` sets 300 s, and the
    floor is asserted in ``tests/test_scheduler.py`` so it cannot be lowered by accident.
    """
    from kaiba.intelligence import tracker

    # The projection is over what this route actually pays for. ``poll_wallets`` polls
    # ``pollable_addresses`` (active rows minus the cohort wallets whose meta says
    # ``helius_poll: false`` because the GMGN feed that nominated them already delivers
    # their trades), so projecting over ``watched_addresses`` over-stated the bill by the
    # whole cohort: at 10 credits a poll every 300 s a 700-wallet cohort reads as
    # ~2,000,000 credits a day that nothing spends.
    watched = sorted(tracker.watched_addresses(Chain.SOL, ctx.conn))
    pollable = sorted(tracker.pollable_addresses(Chain.SOL, ctx.conn))
    cohort_only = max(0, len(watched) - len(pollable))
    if not pollable:
        return {"route": tracker.ROUTE_HELIUS, "wallets": 0, "reason": "watchlist_empty",
                "watched": len(watched), "cohort_only": cohort_only}
    projected = tracker.projected_wallet_cost(
        len(pollable), float(ctx.param("interval_s_for_projection", 300))
    )
    results = tracker.poll_wallets(Chain.SOL, ctx.conn)
    ok = sum(1 for r in results if r.ok)
    out = {
        "route": tracker.ROUTE_HELIUS,
        "wallets": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "detections": sum(len(r.detections) for r in results),
        "credits": sum(r.credits for r in results),
        "rows_seen": sum(r.rows_seen for r in results),
        "watched": len(watched),
        "cohort_only": cohort_only,
        "monthly_credits_at_this_size": projected.get("credits"),
        "fits_free_tier": projected.get("fits_free_tier"),
    }
    if results and ok == 0:
        raise JobFailed("every wallet poll failed", out)
    return out


def job_tracker_rescreen(ctx: JobContext) -> dict[str, Any]:
    """Re-run admission on the watchlist and evict what now fails. 10 credits per wallet.

    Admission is a judgement about a moving target, so it expires. Daily, because it is
    paid: a wallet that turned into a bot costs us a day of stale watching, not a month.
    """
    from kaiba.intelligence import tracker
    from kaiba.intelligence.watchlist_policy import reconcile_active

    policy_result = reconcile_active(ctx.conn, actor=str(ctx.param("actor", "ops")))
    result = tracker.rescreen(Chain.SOL, ctx.conn, actor=str(ctx.param("actor", "ops")))
    evicted = result.get("evicted", [])
    return {
        "active_grade_policy": policy_result,
        "screened": result.get("screened", 0),
        "kept": len(result.get("kept", [])),
        "evicted": len(evicted),
        "credits": result.get("credits", 0),
        # Addresses, not the full reason text: a table cell, and the audit trail has both.
        "evicted_addresses": [str(item.get("address"))[:12] for item in evicted][:10],
    }


def _chain_names(value: Any, default: Iterable[str]) -> list[str]:
    """A ``chains`` parameter as a list of lower-case names: YAML list or comma string."""
    if value is None:
        return [str(c) for c in default]
    raw = value.split(",") if isinstance(value, str) else list(value)
    return [str(c).strip().lower() for c in raw if str(c).strip()]


#: Reason keys for the cohort job's paid path being declined for this run.
REASON_PAID_CHECK_UNBOUNDED = "paid_check_needs_max_credits_per_run"


def _cohort_summary(report: Any) -> dict[str, Any]:
    """A ``CohortSeedReport`` as counts: the address lists go in the audit log, not here.

    A cohort pass considers every wallet the feeds listed (MEASURED 2026-09-21: 405 on
    bsc, 794 on sol, 950 on robinhood), so the report's ``admitted``/``refused`` lists are
    hundreds of addresses and would blow past what ``_small`` keeps in a table cell.
    """
    d = report.as_dict() if hasattr(report, "as_dict") else dict(report)
    out: dict[str, Any] = {}
    for key in ("considered", "smart_cohort", "kol_cohort", "feed_only", "tags_unavailable",
                "failure_rate_measured", "failure_rate_unmeasurable", "credits_spent",
                "wallet_rows_written", "smart_tags_written", "smart_tags_stripped"):
        out[key] = d.get(key, 0)
    for key in ("admitted", "already_watched", "retiered", "refused", "removed"):
        out[key] = len(d.get(key) or [])
    out["refusal_counts"] = dict(d.get("refusal_counts") or {})
    out["admitted_by_tier"] = dict(d.get("admitted_by_tier") or {})
    out["watchlist_after"] = dict(d.get("watchlist_after") or {})
    return out


def job_tracker_cohorts(ctx: JobContext) -> dict[str, Any]:
    """Seed the watchlist and ``wallets.tags_json`` from the GMGN cohort feeds, per chain.

    The ``wallets`` table had 0 rows on the live box on 2026-09-21 and ``sm-trenches``
    reads ``wallets.tags_json`` for its smart cohort, so the only live lane that covers
    bsc could not fire. ``tracker.seed_from_cohorts`` writes those rows; nothing ran it.

    **The shipped run is free.** ``check_failure_rate`` is false in ``config/schedule.yaml``
    and the reason is a verifier finding: ``seed_from_cohorts`` pays a 10-credit Helius
    screen per Solana cohort wallet *before* it checks ``get_entry()``, so with the check
    on, every run re-screens every sol cohort wallet it has already admitted -- 794 sol
    feed wallets on the live box is ~7,900 credits a run, ~380,000 a day at a 30-minute
    interval. The paid path is therefore only taken when the operator turns the check on
    AND names a positive ``max_credits_per_run`` (an unbounded paid pass is refused and
    recorded), AND the ledger is readable and above ``helius.floor_credits``. What it
    spends goes to ``ops_quota`` under this job's name.

    ``spends_helius`` is deliberately False on the :class:`JobSpec`: the dispatcher's floor
    gate would otherwise stop the free bsc/robinhood seeding in a month where credits ran
    low, and bsc has no paid path to gate. The floor is applied here, to the paid path only.
    """
    from kaiba.intelligence import tracker

    check = bool(ctx.param("check_failure_rate", False))
    lookback_s = int(ctx.param("lookback_s", 86_400))
    run_cap = int(ctx.param("max_credits_per_run", 0)) or None
    names = _chain_names(ctx.param("chains", None), ("bsc", "sol", "robinhood"))
    since_ms = ctx.now() - lookback_s * 1000
    # A chain is not started with less than this left on the clock, so a run returns and
    # records what it did instead of being killed mid-chain; the chains it did not reach
    # are listed as deferred and lead the next run's report. MEASURED on a snapshot of
    # the live database (2026-09-22, dry run, 24 h lookback): bsc 4.5 s for 407 wallets,
    # robinhood 63 s for 968, sol 151 s for 804 -- the screen builds evidence per wallet,
    # so the cost is per wallet listed, not per row. Default INVENTED: 120 s is under
    # the cheapest chain's cost x 30 and over the slowest's per-wallet cost x 500.
    reserve_s = float(ctx.param("per_chain_reserve_s", 120))

    paid: dict[str, Any] = {"requested": check, "taken": False}
    if check and run_cap is None:
        check = False
        paid["reason"] = REASON_PAID_CHECK_UNBOUNDED
    elif check:
        from kaiba.providers import helius

        floor = int(ctx.config.helius.floor_credits)
        try:
            st = helius.budget_status(conn=ctx.conn)
            remaining = int(st["remaining"])
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger must block spending
            check = False
            paid["reason"] = REASON_BUDGET_UNREADABLE
            paid["detail"] = _error_text(exc)
        else:
            if remaining < floor:
                check = False
                paid.update({"reason": REASON_BUDGET_FLOOR, "remaining": remaining, "floor": floor})
            else:
                paid.update({"taken": True, "remaining": remaining, "floor": floor, "run_cap": run_cap})

    per_chain: dict[str, Any] = {}
    totals = {"considered": 0, "admitted": 0, "refused": 0, "removed": 0, "already_watched": 0,
              "wallet_rows_written": 0, "smart_tags_written": 0, "credits": 0}
    deferred: list[str] = []
    for name in names:
        try:
            chain = Chain(name)
        except ValueError:
            per_chain[name] = {"reason": "unknown_chain"}
            continue
        if ctx.time_left_s() < reserve_s:
            deferred.append(name)
            per_chain[name] = {"reason": "deferred", "time_left_s": round(ctx.time_left_s(), 1)}
            continue
        report = tracker.seed_from_cohorts(
            chain, ctx.conn,
            check_failure_rate=check,
            since_ms=since_ms,
            max_credits=run_cap,
        )
        summary = _cohort_summary(report)
        per_chain[name] = summary
        for key in ("considered", "admitted", "refused", "removed", "already_watched",
                    "wallet_rows_written", "smart_tags_written"):
            totals[key] += int(summary.get(key) or 0)
        spent = int(summary.get("credits_spent") or 0)
        totals["credits"] += spent
        if spent:
            ctx.quota_add(units=int(summary.get("failure_rate_measured") or 0), credits=spent)
    return {**totals, "check_failure_rate": check, "lookback_s": lookback_s, "paid_check": paid,
            "deferred": deferred, "per_chain": per_chain}


def job_wallet_naming(ctx: JobContext) -> dict[str, Any]:
    """Name the wallets we hold evidence on and write it into ``wallets`` (naming module).

    Free, local, and ``enabled: true`` in the shipped schedule since 2026-09-22. It shipped
    off while ``naming.registry_tags`` mapped the vendor's ``smart_degen`` /
    ``app_smart_money`` onto ``smart_money`` and ``launchpad_smart`` onto ``pump_smart``,
    the exact words ``lanes.SMART_TAGS`` and ``grade.POSITIVE_REPUTATION_TAGS`` read as a
    grade. naming-v2 closed that: a GMGN label lands in ``tags_json`` as ``gmgn:<label>``
    (``naming.vendor_tag``) and nothing else, and no lane or grader vocabulary matches the
    ``gmgn:`` namespace (tests/test_naming.py pins the non-collision; tests/test_scheduler.py
    re-checks it on the real namer as the condition of this flip). The only writer of a bare
    ``smart_money`` is ``tracker.seed_from_cohorts``, as a derived tag on admission after
    the screen; nothing in the tree writes ``pump_smart`` from a vendor label.

    Order of operations that matters for money: ``wallet_tracked.cohorts`` is pinned to the
    operator's cohorts FIRST, because the first naming pass inserts ~88k rows with ``cohort``
    NULL (MEASURED dry run against a snapshot of the live database, 2026-09-22: 87,991) and
    an unfiltered ``backfill.tracked_wallets`` would start paying to top those up
    alphabetically.

    2026-10-02: INCREMENTAL by default (``naming.name_wallets_incremental``). The full pass
    -- a GROUP BY over every swap plus a LIKE over every ``wallet.trade`` event -- had not
    finished inside its 300 s timeout since 2026-09-24 (4 of 4 runs timed out, then 25
    consecutive failures). Each run now re-names only the wallets whose evidence moved
    since its kv cursors, by index seeks, stopping at ``max_wallets`` or ``budget_s``
    (and always 30 s inside the timeout), and reports each change feed's lag.
    ``mode: full`` runs the old pass, for a deliberate rebuild.
    """
    from kaiba.intelligence import naming

    if str(ctx.param("mode", "incremental")) == "full":
        report = naming.name_wallets(ctx.conn)
        out = report.as_dict()
        # Three sample names per archetype is a screenful; the table cell keeps a handful.
        out["samples"] = list(out.get("samples") or [])[:5]
        return out
    budget_s = float(ctx.param("budget_s", 200))
    deadline = min(ctx.now() + int(budget_s * 1000), ctx.deadline_ms - 30_000)
    inc = naming.name_wallets_incremental(
        ctx.conn,
        max_wallets=int(ctx.param("max_wallets", naming.INCREMENTAL_MAX_WALLETS)),
        chunk_wallets=int(ctx.param("chunk_wallets", naming.INCREMENTAL_CHUNK_WALLETS)),
        insert_swap_only=bool(ctx.param("insert_swap_only", False)),
        max_tape_lag_ids=int(ctx.param("max_tape_lag_ids", naming.INCREMENTAL_MAX_TAPE_LAG_IDS)),
        deadline_ms=deadline,
        clock=ctx.clock,
    )
    out = inc.as_dict()
    out["samples"] = list(out.get("samples") or [])[:5]
    return out


def job_wallet_feed_tags(ctx: JobContext) -> dict[str, Any]:
    """Backfill, then parity-check, the ``wallet_feed_tags`` rollup (intelligence/feed_tags.py).

    The rollup is what lets old ``wallet.trade`` events be deleted (~0.4 GB/day on the box)
    without losing GMGN's labels, which exist nowhere else. Phases, one per run:

    * ``waiting_for_writer`` -- nothing until ``gmgn_feeds.write_swap`` (new code, kaiba-ingest
      restarted) has rolled up its first event and stamped the id it started from;
    * ``backfilling`` -- the events below that id, newest window first, inside ``budget_s``,
      resumable from kv, each window and its cursor in one short transaction;
    * parity -- once complete, at most every ``parity_interval_s``: sampled wallets' table
      rows against their own events, and every reader's old answer against its new one.
      A failed check FAILS the job (and blocks retention); a passed one is what switches
      the readers over (``feed_tags.table_ready``).

    Free, local, no provider. Reads at most one ``window_ids`` range of events per step.
    """
    from kaiba.intelligence import feed_tags

    budget_s = float(ctx.param("budget_s", 200))
    deadline = min(ctx.now() + int(budget_s * 1000), ctx.deadline_ms - 30_000)
    out: dict[str, Any] = {}
    backfill = feed_tags.backfill_step(
        ctx.conn,
        deadline_ms=deadline,
        window_ids=int(ctx.param("window_ids", 20_000)),
        sleep_s=float(ctx.param("sleep_s", 0.2)),
        clock=ctx.clock,
    )
    out["backfill"] = backfill
    if not backfill.get("complete"):
        out["ready"] = feed_tags.table_ready(ctx.conn)
        return out
    last = feed_tags.parity_record(ctx.conn)
    interval_ms = int(ctx.param("parity_interval_s", 21_600)) * 1000
    if (last.get("ok") and last.get("sufficient") and last.get("checked_ms")
            and ctx.now() - int(last["checked_ms"]) < interval_ms):
        out["parity"] = {"skipped": "interval", "last_checked_ms": last.get("checked_ms"),
                         "last_mode": last.get("mode"), "last_compared": last.get("compared")}
    elif ctx.now() >= deadline:
        out["parity"] = {"skipped": "no_time_left"}
    else:
        # MEASURED 2026-10-02: ~1.8 s a sampled wallet on the box, so the deadline, not the
        # sample size, is what bounds this.
        parity = feed_tags.parity_check(ctx.conn, sample=int(ctx.param("parity_sample", 60)),
                                        record=True, now_ms=ctx.now(), deadline_ms=deadline,
                                        clock=ctx.clock)
        out["parity"] = parity
        if not parity.get("ok"):
            out["ready"] = feed_tags.table_ready(ctx.conn)
            raise JobFailed("wallet_feed_tags parity check failed", out)
    out["ready"] = feed_tags.table_ready(ctx.conn)
    return out


def job_wallet_seeds(ctx: JobContext) -> dict[str, Any]:
    """Recompute `seed_confluence` for every wallet on each chain's tape.

    Free, local, no provider call, and it must stay ahead of ``wallet_tape``: the tape
    grader reads the number this writes, and a wallet graded before its seeds are
    counted is graded without 12 of the 100 points. See kaiba.intelligence.seeds.

    2026-10-02: writes only the wallets whose count CHANGED (it rewrote all ~915k every
    run), takes seeds from the cursor-folded price extent (no whole-tape scan, numeric
    peak), and gives each chain a fair share of the time left so a cut pass resumes next
    run instead of timing out. FAILS while the extent is still bootstrapping.
    """
    from kaiba.intelligence import seeds as SEEDS

    names = ctx.param("chains", ["sol", "bsc", "robinhood"]) or []
    limit = ctx.param("wallets_per_chain", None)
    dry = bool(ctx.param("dry_run", False))
    per_chain: dict[str, Any] = {}
    totals = {"seeds": 0, "written": 0, "unchanged": 0, "failed": 0}
    not_ready: list[str] = []
    for i, name in enumerate(names):
        try:
            chain = Chain(name)
        except ValueError:
            per_chain[str(name)] = {"reason": "unknown_chain"}
            continue
        rep = SEEDS.run(chain, ctx.conn, limit=(int(limit) if limit else None), dry_run=dry,
                        deadline=_share_deadline(ctx, len(names) - i), clock=ctx.clock)
        per_chain[chain.value] = rep
        if rep.get("not_ready"):
            not_ready.append(chain.value)
        for k in totals:
            totals[k] += int(rep.get(k) or 0)
    result = {**totals, "per_chain": per_chain}
    if not_ready:
        raise JobFailed(f"price extent still folding the tape; no seeds counted on {not_ready}",
                        result)
    if totals["failed"]:
        raise JobFailed("wallet seed writes failed", result)
    return result


def _share_deadline(ctx: JobContext, chains_left: int, *, margin_s: float = 30.0) -> float:
    """``ctx.clock()`` seconds by which this chain must stop: an equal share of what is left.

    Time a chain does not use flows to the chains after it. The margin keeps the last
    chain's final transaction clear of the scheduler's own timeout.
    """
    end = ctx.deadline_ms / 1000.0 - margin_s
    now = ctx.clock()
    return now + max(0.0, end - now) / max(1, int(chains_left))

def job_wallet_tape(ctx: JobContext) -> dict[str, Any]:
    """Grade every wallet on each chain's own tape (grade.grade_tape), read-only by default.

    Per chain, the batch report's counts are returned and its ranked ``candidates`` -- the
    wallets whose partial tape says a paid backfill is worth it -- are written to kv under
    ``ops:wallet_tape:candidates:<chain>`` as a JSON list, so the paid queue (or an
    operator) can read them without re-running the pass.

    ``store`` stays false in the shipped schedule, and the reason is no longer a missing
    prerequisite. All three are in place: :func:`ungraded_buyers`, :func:`gather_queue` and
    :func:`free_grade_targets` look through :data:`TAPE_MODEL_VERSION` rows;
    :func:`stale_scores` never hands one to the full-history regrade; and the grader's tape
    B gate (``grade._tape_cap``) and breadth term (``grade._breadth``) count CLOSED episodes
    only, so twenty unseen exits can no longer lift a C to a B. What remains is a decision,
    not a fix: a stored tape grade lands in ``wallet_scores``, which is what ``sm-trenches``
    sizes on (its grade read and its ``archetype`` smart route both come from that table),
    so flipping ``store`` is the lead's call on the evidence, not a mechanical consequence
    of the fixes.
    """
    from kaiba.intelligence import grade

    # 2026-10-02 -- MEASURED on the box: 3 of the last 4 runs timed out at 1,500 s (sol
    # 755-1,149 s, robinhood 389-691 s, bsc 142-187 s for 928 wallets), every one storing
    # all ~525k scored wallets. Three changes, none of which alters a grade:
    #   * skip_unchanged (default on): a stored tape grade is rewritten only if it moved
    #     (grade.unchanged_tape_scores), A/B every pass, the rest restamped every 3-6 days;
    #   * each chain gets a fair share of the time left and stops BETWEEN wallets at it;
    #     the next run resumes after the last wallet finished (kv ops:wallet_tape:pass:*),
    #     so the job finishes inside its timeout and a long tape spreads over runs;
    #   * chains run smallest completed pass first, so the time small tapes leave unused
    #     flows to sol instead of sol's share starving the others.
    store = bool(ctx.param("store", False))
    skip_unchanged = bool(ctx.param("skip_unchanged", True))
    limit = int(ctx.param("candidate_limit", grade.TAPE_CANDIDATE_LIMIT))
    names = _chain_names(ctx.param("chains", None), ("sol", "bsc", "robinhood"))
    per_chain: dict[str, Any] = {}
    totals = {"wallets_seen": 0, "wallets_scored": 0, "candidates": 0, "stored": 0,
              "store_failed": 0, "b_or_better": 0, "unchanged_skipped": 0}
    chains: list[Chain] = []
    for name in names:
        try:
            chains.append(Chain(name))
        except ValueError:
            per_chain[name] = {"reason": "unknown_chain"}
    states = {c: _tape_pass_state(ctx.conn, c) for c in chains}
    chains.sort(key=lambda c: (states[c].get("full_pass_wallets") is None,
                               states[c].get("full_pass_wallets") or 0))
    for i, chain in enumerate(chains):
        state = states[chain]
        start_after = state.get("resume_after") or None
        report = grade.grade_tape(
            ctx.conn, chain, store=store, candidate_limit=limit,
            skip_unchanged=skip_unchanged, start_after=start_after,
            deadline=_share_deadline(ctx, len(chains) - i), clock=ctx.clock,
        )
        ts = ctx.now()
        truncated = bool(getattr(report, "truncated", False))
        cursor = (getattr(report, "resume_after", None) or start_after) if truncated else None
        candidates = list(report.candidates)
        if start_after is not None or truncated:
            candidates = _merge_tape_candidates(
                ctx.conn, chain, candidates, lo=start_after,
                hi=cursor if truncated else None, limit=limit,
            )
        ctx.conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (f"ops:wallet_tape:candidates:{chain.value}", jdump(candidates), ts),
        )
        carried = int(state.get("pass_wallets") or 0) if start_after else 0
        seen = int(report.wallets_seen) + carried
        _save_tape_pass_state(ctx.conn, chain, ts, {
            "resume_after": cursor,
            "pass_wallets": seen if truncated else 0,
            "full_pass_wallets": state.get("full_pass_wallets") if truncated else seen,
        })
        d = report.as_dict()
        summary = {k: d.get(k) for k in (
            "wallets_seen", "wallets_scored", "wallets_too_thin", "by_grade", "b_or_better",
            "sell_only_refused", "quarantined_by_tag", "candidates", "stored", "store_failed", "kept_full_grade",
            "unchanged_skipped", "truncated", "elapsed_s",
        )}
        if start_after:
            summary["resumed"] = True
        per_chain[chain.value] = summary
        for key in totals:
            totals[key] += int(summary.get(key) or 0)
    result = {**totals, "store": store, "candidate_limit": limit, "per_chain": per_chain}
    if totals["store_failed"]:
        raise JobFailed("wallet tape writes failed", result)
    return result


def _tape_pass_state(conn: sqlite3.Connection, chain: Chain) -> dict[str, Any]:
    """``{resume_after, pass_wallets, full_pass_wallets}`` for one chain's tape pass."""
    row = conn.execute(
        "SELECT value FROM kv WHERE key = ?", (f"ops:wallet_tape:pass:{chain.value}",)
    ).fetchone()
    value = jload(row[0], {}) if row else {}
    return value if isinstance(value, dict) else {}


def _save_tape_pass_state(
    conn: sqlite3.Connection, chain: Chain, ts: int, state: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (f"ops:wallet_tape:pass:{chain.value}", jdump(state), ts),
    )


def _merge_tape_candidates(
    conn: sqlite3.Connection, chain: Chain, fresh: list[dict[str, Any]], *,
    lo: str | None, hi: str | None, limit: int,
) -> list[dict[str, Any]]:
    """This run's candidates plus last list's entries for wallets this run did not reach.

    A run that covered ``(lo, hi]`` of the address space re-judged every wallet in it, so
    an old entry inside that range is dropped (re-ranked, or no longer a candidate) and one
    outside it is kept until the pass reaches it. Ranked as the grader ranks: closed
    episodes, then realised PnL.
    """
    row = conn.execute(
        "SELECT value FROM kv WHERE key = ?", (f"ops:wallet_tape:candidates:{chain.value}",)
    ).fetchone()
    previous = jload(row[0], []) if row else []

    def covered(address: str) -> bool:
        return (lo is None or address > lo) and (hi is None or address <= hi)

    kept = [c for c in (previous if isinstance(previous, list) else [])
            if isinstance(c, dict) and isinstance(c.get("address"), str)
            and not covered(c["address"])]

    def rank(c: dict[str, Any]) -> tuple[int, float]:
        try:
            pnl = float(c.get("realized_pnl") or 0)
        except (TypeError, ValueError):
            pnl = 0.0
        return int(c.get("closed_episodes") or 0), pnl

    merged = sorted([*fresh, *kept], key=rank, reverse=True)
    return merged[: max(0, int(limit))]


def job_clustering(ctx: JobContext) -> dict[str, Any]:
    """Rebuild the entity graph from evidence we already hold. Free, local, no provider.

    Deliberately separate from :func:`job_clustering_funding` so the free rebuild is never
    skipped by ``helius.floor_credits``: the graph is what "count entities, not addresses"
    means, and a month where credits ran low must not be a month where confluence silently
    counts five addresses from one hand as five operators.
    """
    from kaiba.intelligence import clustering

    run = clustering.run(
        ctx.conn,
        Chain.SOL,
        fund_credits=0,
        max_entity_size=int(ctx.param("max_entity_size", clustering.MAX_ENTITY_SIZE)),
    )
    out = {
        "run_id": run.run_id,
        "status": run.status,
        "reason": run.reason,
        "edges_total": sum(run.edges_by_rule.values()),
        "edges_by_rule": dict(run.edges_by_rule),
        "pruned_edges": run.pruned_edges,
        "addresses_seen": run.addresses_seen,
        "addresses_checked": run.addresses_checked,
        "entities": len(run.entities),
        "entity_members": run.entity_members,
        "largest_entity": run.largest_entity,
        "largest_component": run.largest_component,
        "quarantined": len(run.quarantined),
        "quarantined_members": sum(q.size for q in run.quarantined),
        "services": len(run.services),
    }
    if run.status not in {"ok", "running"}:
        raise JobFailed(f"clustering run {run.status}: {run.reason}", out)
    return out


def job_clustering_funding(ctx: JobContext) -> dict[str, Any]:
    """Buy first-funder facts for the addresses worth paying for. Spends Helius credits.

    10 credits an address (``clustering.FUNDING_CREDITS_PER_WALLET``). At the shipped
    2,500/day that is 250 addresses a day and ~7.8% of the free month, on top of
    ``wallet_buyers`` (31% at its ``credits_per_day`` ceiling) and ``wallet_tracked``
    (1.9%): ~40.7% of the month on scheduled paid work, which the shipped-config test
    holds under the floor.

    The daily figure is held in ``ops_quota`` and not in this process, for the same reason
    the wallet jobs hold theirs there: a restart inside the UTC day must not reset the
    count and hand the schedule a second day's budget.
    """
    from kaiba.intelligence import clustering

    per_run = int(ctx.param("max_credits", clustering.DEFAULT_FUNDING_CREDIT_BUDGET))
    per_day = int(ctx.param("credits_per_day", per_run))
    used_units, used_credits = ctx.quota_used()
    budget = min(per_run, per_day - used_credits)
    if budget < clustering.FUNDING_CREDITS_PER_WALLET:
        return {"reason": REASON_QUOTA, "credits_per_day": per_day, "credits_today": used_credits,
                "addresses_today": used_units, "looked_up": 0}
    looked_up, resolved, credits = clustering.enrich_funding(
        ctx.conn, Chain.SOL, max_credits=budget
    )
    ctx.quota_add(units=looked_up, credits=credits)
    return {
        "looked_up": looked_up,
        "resolved": resolved,
        "unresolved": max(0, looked_up - resolved),
        "credits": credits,
        "budget_this_run": budget,
        "credits_today": used_credits + credits,
        "credits_per_day": per_day,
    }


def job_ops_prune(ctx: JobContext) -> dict[str, Any]:
    """Keep the scheduler's own history bounded. Quota rows are kept longer for the ledger."""
    days = int(ctx.config.runs_retention_days)
    now = ctx.now()
    runs = ctx.conn.execute("DELETE FROM ops_runs WHERE started_ms < ?", (now - days * 86_400_000,)).rowcount
    quota = ctx.conn.execute(
        "DELETE FROM ops_quota WHERE period < ?", (utc_day(now - 62 * 86_400_000),)
    ).rowcount
    return {"runs_deleted": int(runs or 0), "quota_rows_deleted": int(quota or 0), "retention_days": days}


def job_retention(ctx: JobContext) -> dict[str, Any]:
    """Row retention for the tables that only grow (kaiba/ops/retention.py).

    A separate job from ``ops_prune`` on purpose: ``ops_prune`` is a 60 s job over the
    scheduler's own small tables, and this is a LONG, low-priority one (timeout >=
    ``long_timeout_s``, so it shares the single long slot with the studies) that walks
    millions of rows in short transactions with sleeps between them.

    Two switches, both off as shipped: ``jobs.retention.enabled`` decides whether the job
    runs at all, and the top-level ``retention.enabled`` decides whether a run may delete.
    With the second off a run is a read-only dry run that reports what it would delete.
    ``dry_run: true`` as a job parameter forces the dry run either way.
    """
    from kaiba.ops import retention

    cfg = ctx.config.retention
    # Stop 30 s inside the job's timeout whatever budget_s says: a run cut off by the
    # scheduler would leave its last transaction to the hard-deadline interrupt.
    deadline = min(ctx.now() + int(cfg.budget_s * 1000), ctx.deadline_ms - 30_000)
    return retention.run(
        ctx.conn, cfg, dry_run=bool(ctx.param("dry_run", False)), deadline_ms=deadline, clock=ctx.clock,
    )


def job_deployer_stats(ctx: JobContext) -> dict[str, Any]:
    """Recompute every deployer's record from our own tape.

    A batch job because the query is a seven-day scan of ``swaps``; the entry path reads
    the table it writes. Without this job the table is empty, every lookup reads
    "unknown", and the sizing arm that depends on it is a rule with no data -- which is
    the exact failure ``creator_rug_count`` had: a shipped rule that had never once fired
    because nothing populated its input.

    2026-10-02: the outcomes come from ``price_extent``, folded forward from a swaps
    cursor, not from two whole-chain ``GROUP BY`` walks per chain (197 s mean, 5 of 28
    runs past the 300 s timeout). A steady-state run folds ~30 minutes of swaps. While the
    extent is still bootstrapping (first deploy: ~15 min of IO over the whole tape, spread
    across runs by the deadline) the job FAILS with the fold's progress instead of writing
    records from a half-built table; the lookups meanwhile go stale and read "unknown",
    which the sizer charges nothing.
    """
    from kaiba.intelligence import price_extent
    from kaiba.intelligence.deployer import ExtentNotReady, refresh

    chains = [c.strip() for c in str(ctx.param("chains", "sol,bsc")).split(",") if c.strip()]
    deadline = (ctx.deadline_ms - 30_000) / 1000.0
    # Fold once up front so the result records what this run read; each refresh's own
    # advance is then a no-op, and refresh is what refuses a half-folded extent.
    fold = price_extent.advance(ctx.conn, deadline=deadline)
    folded = {k: fold.as_dict()[k] for k in ("rows_read", "batches", "cursor_to", "target_id",
                                              "caught_up", "elapsed_s")}
    out: dict[str, Any] = {}
    for name in chains:
        try:
            chain = Chain(name)
        except ValueError:
            raise JobFailed(f"unknown chain {name!r}") from None
        try:
            out[name] = refresh(ctx.conn, chain, window_days=int(ctx.param("days", 7)),
                                deadline=deadline)
        except ExtentNotReady as exc:
            raise JobFailed(str(exc), {"deployers_written": out,
                                       "price_extent": exc.progress}) from None
    return {"deployers_written": out, "price_extent": folded}


def job_exit_study(ctx: JobContext) -> dict[str, Any]:
    """Score every exit policy against our own replayed episodes, in and out of sample.

    RECORDS ONLY. MEASURED 2026-09-23: the shipped -30% stop came 13th of 14 policies out
    of sample (-30.5% mean, -36.0% on live-only episodes), beaten by every trailing and
    take-profit variant tested -- but `dsr_out_of_sample` was 0.000 for ALL of them, so
    after deflating for 14 hypotheses on 96 episodes none is distinguishable from luck.
    The owner's call was to change nothing until the sample is better, which is exactly
    what scheduling this is for: the evidence has to accrue before it can decide anything.
    """
    from kaiba.learning.exit_study import study

    result = study(ctx.conn)
    best = None
    for policy in result.policies:
        mean = policy.out_of_sample.mean_pct
        if mean is not None and (best is None or mean > best[1]):
            best = (policy.name, mean)
    incumbent = next(
        (p for p in result.policies if "incumbent" in p.name), None
    )
    out = {
        "replayable": result.eligibility.get("replayable"),
        "in_sample": result.n_in_sample,
        "out_of_sample": result.n_out_of_sample,
        "live_only": result.n_live,
        "span_hours": round(result.span_hours, 1),
        "best_oos": {"policy": best[0], "mean_pct": round(best[1], 2)} if best else None,
        "incumbent_oos_mean_pct": (
            round(incumbent.out_of_sample.mean_pct, 2)
            if incumbent is not None and incumbent.out_of_sample.mean_pct is not None
            else None
        ),
        "any_policy_significant": any(
            (p.dsr_out_of_sample or 0) > 0.05 for p in result.policies
        ),
    }
    log.info("ops: exit_study %s", out)
    return out


def job_copytrade(ctx: JobContext) -> dict[str, Any]:
    """Rank copyable wallets per chain, and test whether that ranking PERSISTS.

    The ranking alone is the trap. MEASURED 2026-09-23 with our real 60 s feed lag and
    1%/leg fees: on sol and bsc the top-20 by past copy-profit went on to do WORSE than the
    population out of sample (-0.22% vs +3.89%, -3.68% vs +0.33%) -- the ranking was
    selecting last period's luck. Only robinhood persisted (+28.98% vs -2.97%).

    So this job reports the persistence test beside the ranking, and never the ranking
    alone: a leaderboard with no out-of-sample check is how a book talks itself into
    copying noise.
    """
    from kaiba.learning.copytrade import persistence, rank_wallets

    chains = [c.strip() for c in str(ctx.param("chains", "sol,bsc,robinhood")).split(",") if c.strip()]
    top_n = int(ctx.param("top_n", 20))
    out: dict[str, Any] = {}
    for name in chains:
        try:
            chain = Chain(name)
        except ValueError:
            raise JobFailed(f"unknown chain {name!r}") from None
        check = persistence(ctx.conn, chain, top_n=top_n)
        if check.get("error"):
            out[name] = {"error": check["error"]}
            continue
        top = check.get("top_later_mean_pct")
        allm = check.get("all_later_mean_pct")
        out[name] = {
            "ranked": len(rank_wallets(ctx.conn, chain, limit=int(ctx.param("limit", 2000)))),
            "top_later_mean_pct": round(top, 2) if top is not None else None,
            "all_later_mean_pct": round(allm, 2) if allm is not None else None,
            "persists": bool(top is not None and allm is not None and top > allm),
        }
        log.info("ops: copytrade %s %s", name, out[name])
    return out


def job_proven_wallets(ctx: JobContext) -> dict[str, Any]:
    """Freeze a FORWARD-VALIDATED wallet cohort per chain: ``proven:<chain>:<ms>``.

    Added 2026-10-02 (kaiba/learning/proven.py). A wallet is proven when copying it, at
    our own lag and 1%/leg, made money in sample AND again out of sample (split on the
    median swap) with a bootstrap lower bound above zero -- and only on a chain whose
    in-sample winners out-earned its in-sample losers out of sample. Grades do not enter
    into it: sol B grades measured no better than C/D forward. A chain where nothing
    persists freezes an EMPTY cohort, which the lane reads as "nobody counts here".

    Read-only on ``swaps``: primary-key reads of the newest rows for the candidate draw,
    then ``idx_swaps_wallet`` / ``idx_swaps_token`` seeks per candidate, under a deadline
    (time left minus 60 s, split evenly across chains). Writes only
    ``wallet_cohorts`` / ``wallet_cohort_freezes``. MEASURED 2026-10-02 on the box under
    ``ionice -c3``: robinhood 3,014 candidates in 324 s, sol 3,000 in 64 s -- and both
    cohorts came out EMPTY (see the proven_wallets entry in config/schedule.yaml).
    """
    from kaiba.learning import proven

    try:
        chains = proven.parse_chains(ctx.param("chains", "sol,robinhood"))
        cfg = proven.config_from_params(ctx.params)
    except ValueError as exc:
        raise JobFailed(str(exc)) from None
    deadline = time.monotonic() + max(5.0, ctx.time_left_s() - 60.0)
    return proven.run(ctx.conn, chains, config=cfg, as_of_ms=ctx.now(), deadline_monotonic=deadline)


def job_variable_study(ctx: JobContext) -> dict[str, Any]:
    """Search every variable and pair for separation, confirming each on a time holdout.

    The search is the easy half; the holdout is the point. 65 single hypotheses over a 7%
    base rate will hand back several winners by luck, so the older half of the tape
    discovers and the newer half confirms, and anything that fails the second half is
    logged as REJECTED rather than dropped -- MEASURED 2026-09-23, five candidates died
    that way, including `holder_count=q4` going 1.56x -> 0.88x.

    RECORDS ONLY, like every other study here. A variable that survives has earned a place
    in the sizer's evidence and a human's attention, not an automatic threshold change: a
    gate that retunes itself on its own output is a feedback loop nobody is reading.
    """
    from kaiba.learning.variables import lines, run

    chains = [c.strip() for c in str(ctx.param("chains", "") or "").split(",") if c.strip()]
    targets: list[Chain | None] = [None]
    for name in chains:
        try:
            targets.append(Chain(name))
        except ValueError:
            raise JobFailed(f"unknown chain {name!r}") from None
    out: dict[str, Any] = {}
    for chain in targets:
        study = run(ctx.conn, chain, pairs=bool(ctx.param("pairs", True)))
        key = chain.value if chain else "all"
        out[key] = {
            "sample": study.sample,
            "hypotheses": study.hypotheses,
            "confirmed": len(study.confirmed),
            "rejected": len(study.rejected),
            "confluences": len(study.pairs),
        }
        for line in lines(study)[:26]:
            log.info("%s", line)
    return out


def job_entry_study(ctx: JobContext) -> dict[str, Any]:
    """Re-measure whether the entry features still separate outcomes, on fresh tape.

    Runs alongside live and paper trading. It only RECORDS: moving a threshold in
    ``risk.py`` stays a deliberate act with a diff, because a gate that retunes itself on
    its own output is a feedback loop nobody is reading.
    """
    from kaiba.learning.entry_study import lines, record, run_study

    chains = [c.strip() for c in str(ctx.param("chains", "sol")).split(",") if c.strip()]
    out: dict[str, Any] = {}
    for name in chains:
        try:
            chain = Chain(name)
        except ValueError:
            raise JobFailed(f"unknown chain {name!r}") from None
        result = run_study(ctx.conn, chain, window_days=int(ctx.param("days", 7)))
        out[name] = {"sample": result.sample, "baseline_rate": round(result.baseline_rate, 2),
                     "cells": record(ctx.conn, result)}
        for line in lines(result)[:12]:
            log.info("%s", line)
    return out


def job_learning_sweep(ctx: JobContext) -> dict[str, Any]:
    """Run every learning study, journal the findings, change nothing.

    MEASURED 2026-09-22: ten modules in ``kaiba/learning/`` and two of them reachable from
    this scheduler. The rest were fully implemented and had never executed -- the same
    "declared, not enforced" shape as ``creator_rug_count``, a shipped rule whose input
    nothing ever populated.

    Spends no provider credit: every study reads tables we already own. That matters --
    raising scanner concurrency the same day drained the ``robinhood-rpc`` bucket and left
    two live positions with no evaluable stop.
    """
    from kaiba.learning.sweep import run_sweep

    report = run_sweep(ctx.conn, only=(ctx.param("only", "") or None))
    for line in report.lines():
        log.info("%s", line)
    return {
        "ran": report.ran,
        "failed": report.failed,
        "studies": {r.name: (r.headline if r.ok else r.error) for r in report.results},
    }

def job_experiment_gates(ctx: JobContext) -> dict[str, Any]:
    """Judge proposed experiments with the deterministic gates, on a schedule.

    Added 2026-10-02 (kaiba/learning/experiment_loop.py). MEASURED that day on the box: 4
    experiments, all ``proposed``, 2 gate_results rows ever (one hand-run), 0
    ``experiment_trades`` -- nothing ran the gates and nothing wrote a candidate arm, so no
    proposal could ever be judged, let alone promoted. Each pass: ``proposed`` -> replay
    gate -> ``rejected`` or ``shadow``; ``shadow`` -> label forward trades into arms ->
    shadow gate once its own minimums are met -> ``rejected`` / ``awaiting_owner`` /
    ``gates.promote``; ``promoted`` -> the pre-registered rollback rule.

    Spends nothing: reads recorded rows only, no provider call, no quote, no order. Its only
    trading-relevant write is ``gates.promote`` (risk.yaml lane params, never bounds or
    sizing), behind ``bounds.allow_self_promotion`` and this job's ``auto_promote``.
    """
    from kaiba.learning import experiment_loop

    try:
        params = experiment_loop._params(ctx.params)
    except ValueError as exc:
        raise JobFailed(str(exc)) from None
    deadline = time.monotonic() + max(5.0, ctx.time_left_s() - 15.0)
    result = experiment_loop.run(ctx.conn, now=ctx.now(), params=params, deadline_monotonic=deadline)
    log.info(
        "ops: experiment_gates seen=%s rejected=%s shadow=%s promoted=%s awaiting_owner=%s "
        "rolled_back=%s waiting=%s errors=%s",
        result["seen"], len(result["rejected"]), len(result["shadow_started"]),
        len(result["promoted"]), len(result["awaiting_owner"]), len(result["rolled_back"]),
        len(result["waiting"]), len(result["errors"]),
    )
    if result["errors"]:
        raise JobFailed(f"{len(result['errors'])} experiment(s) failed", result)
    return result


def job_nft_mint_study(ctx: JobContext) -> dict[str, Any]:
    """PAPER NFT mint study on Robinhood Chain: would auto-minting make money? Spends nothing.

    Added 2026-10-02 (kaiba/learning/mint_study.py). No wallet, no signer, no transaction,
    no OpenSea key: it reads SeaDrop mints and Seaport sales off the chain, writes paper
    ``mint``/``shadow`` rows priced at the on-chain public price + gas, scores them at
    +24 h / +72 h from REAL fills (accepted bids first; an ask nobody paid is never a
    price), and stores a PASS/PENDING/FAIL gate in ``kv``. Live minting is not even
    discussable before PASS.

    RPC budget on the shared ``robinhood-rpc`` bucket, all at Priority.RESEARCH in the
    ``chain.`` family (refused during any chain cooldown, never EXIT/POSITION), its own
    requests >= ``pace_s`` (3 s) apart and never more than 4 reads per batch: a steady run
    is 2 HTTP calls / 4 reads for the tape, plus <= ``max_checks_per_run`` (6) candidate
    checks of 2 reads, or 4 when the drop's public stage is open, once per collection per
    UTC day.
    """
    from kaiba.learning import mint_study

    deadline = time.monotonic() + max(5.0, ctx.time_left_s() - 15.0)
    return mint_study.run(ctx.conn, now=ctx.now(), schedule_params=ctx.params, deadline_monotonic=deadline)


def job_loss_attribution(ctx: JobContext) -> dict[str, Any]:
    """Split every closed live loss into ENTRY vs EXIT, and rank entry features on the
    SCANNED population. Added 2026-10-03 (kaiba/learning/loss_attribution.py).

    Proposes nothing and changes nothing that trades: it writes its own ``loss_attribution``
    table and ``kv`` ``learning:loss_attribution``, which the daily LLM learning loop and the
    daily report read. The gates judge whatever the loop proposes from it. Spends nothing:
    recorded rows and the swap tape only, no provider call.
    """
    from kaiba.learning import loss_attribution

    deadline = time.monotonic() + max(5.0, ctx.time_left_s() - 15.0)
    result = loss_attribution.run(ctx.conn, now=ctx.now(), params=ctx.params, deadline_monotonic=deadline)
    log.info(
        "ops: loss_attribution %s losses=%s entry_share=%s top_leak=%s top_feature=%s "
        "scanned=%s truncated=%s %.1fs",
        result["window"], result["losses"], result["entry_share"], result["top_leak"], result["top_feature"],
        result["scanned_measured"], result["truncated"], result["elapsed_s"],
    )
    return result


JOBS: dict[str, JobSpec] = {
    "learning_sweep": JobSpec(
        "learning_sweep", job_learning_sweep,
        "run every learning study and journal what it found; records only, never retunes",
    ),
    "exit_study": JobSpec(
        "exit_study", job_exit_study,
        "score every exit policy on our own replayed episodes, in and out of sample, with "
        "a deflated Sharpe so 14 hypotheses cannot manufacture a winner; records only",
    ),
    "copytrade": JobSpec(
        "copytrade", job_copytrade,
        "rank copyable wallets AND test whether the ranking survives out of sample: it "
        "does on robinhood and does not on sol or bsc",
    ),
    "proven_wallets": JobSpec(
        "proven_wallets", job_proven_wallets,
        "freeze the wallets whose copy returns persisted OUT OF SAMPLE, per chain, with a "
        "random-eligible control; an empty cohort where nothing persists",
    ),
    "variable_study": JobSpec(
        "variable_study", job_variable_study,
        "search every variable and pair for separation, confirmed on a TIME holdout: the "
        "older half discovers, the newer half must agree, and what fails is reported",
    ),
    "entry_study": JobSpec(
        "entry_study", job_entry_study,
        "does the entry feature still separate outcomes? decision instant at t0+300s, "
        "outcome strictly after; records only, never retunes a gate",
    ),
    "deployer_stats": JobSpec(
        "deployer_stats", job_deployer_stats,
        "who shipped this token and what their last launches did: the spam-factory cell "
        "reaches 2x at 5.3% against a 13.2% baseline",
    ),
    "creators": JobSpec(
        "creators", job_creators,
        "pump.fun creator history feeds tier-0 triage; new creators launch every minute",
    ),
    "wallet_buyers": JobSpec(
        "wallet_buyers", job_wallet_buyers,
        "backfill + grade the buyers seen on scanned tokens: the input three lanes lack",
        spends_helius=True,
    ),
    "wallet_tracked": JobSpec(
        "wallet_tracked", job_wallet_tracked,
        "top up the imported wallet list, least-recently-touched first",
        spends_helius=True,
    ),
    "wallet_regrade": JobSpec(
        "wallet_regrade", job_wallet_regrade,
        "regrade wallets whose evidence moved; free, local",
    ),
    "gmgn_grade": JobSpec(
        "gmgn_grade", job_gmgn_grade,
        "re-grade the GMGN provider-grade pool on a paced DISCOVERY call budget with a resume "
        "cursor; 628 robinhood wallets sat one point under the floor for want of a win rate",
    ),
    "wallet_deepen": JobSpec(
        "wallet_deepen", job_wallet_deepen,
        "buy more history only for paid grades within reach of A whose walk is truncated, "
        "then regrade; the A gate now refuses a truncated sample",
        spends_helius=True,
    ),
    "signals": JobSpec(
        "signals", job_signals,
        "early-alpha sources (crt.sh, venues, governance, repos, pre-launch), each on its own clock",
    ),
    "trade_flow": JobSpec(
        "trade_flow", job_trade_flow,
        "per-token trade follow-up for recently scanned tokens (pump.fun, keyless)",
    ),
    "snapshot_prune": JobSpec(
        "snapshot_prune", job_snapshot_prune,
        "curve snapshot retention, independent of whether writes happen",
    ),
    "radar": JobSpec(
        "radar", job_radar,
        "discovery radar: new venues, launchpads and chains before a human looks",
    ),
    "hunters": JobSpec(
        "hunters", job_hunters,
        "airdrop / nft / listing registry refresh; each hunter independent",
    ),
    "copy_manager": JobSpec(
        "copy_manager", job_copy_manager,
        "sells the owner's GMGN copy trades on HIS named wallet (giveback; rug rules dry until "
        "measured); never buys, never Kaiba's wallet, dry until GMGN binds the wallet",
    ),
    "native_price": JobSpec(
        "native_price", job_native_price,
        "native price samples; entry point owned by another agent, recorded as missing until it lands",
    ),
    "budget_check": JobSpec(
        "budget_check", job_budget_check,
        "Helius credit ledger, so the month's burn is visible before it is a problem",
    ),
    "journal_verify": JobSpec(
        "journal_verify", job_journal_verify,
        "hash-chain integrity of the learning journal",
    ),
    "validate_power": JobSpec(
        "validate_power", job_validate_power,
        "Phase 4 statistical power report, recorded to validation_runs",
    ),
    "tracker_sweep": JobSpec(
        "tracker_sweep", job_tracker_sweep,
        "free per-token tape over the hottest mints; the only detection route that costs nothing",
    ),
    "tracker_poll": JobSpec(
        "tracker_poll", job_tracker_poll,
        "paid per-wallet Helius route; 10 credits a wallet, so the interval is the budget",
        spends_helius=True,
    ),
    "tracker_rescreen": JobSpec(
        "tracker_rescreen", job_tracker_rescreen,
        "admission expires: re-screen the watchlist daily and evict what now fails",
        spends_helius=True,
    ),
    "tracker_cohorts": JobSpec(
        "tracker_cohorts", job_tracker_cohorts,
        "seed the watchlist and wallets.tags_json from the GMGN cohort feeds; the input sm-trenches reads",
        # False on purpose: the shipped run cannot spend (check_failure_rate false), and the
        # free bsc/robinhood seeding must not be stopped by the sol credit floor. The paid
        # path applies the floor itself (see job_tracker_cohorts).
        spends_helius=False,
    ),
    "wallet_naming": JobSpec(
        "wallet_naming", job_wallet_naming,
        "name every wallet we hold evidence on; a vendor label lands as gmgn:<label>, never as a lane word",
    ),
    "wallet_feed_tags": JobSpec(
        "wallet_feed_tags", job_wallet_feed_tags,
        "roll GMGN's wallet labels up out of wallet.trade events, then prove the rollup equals "
        "the events; until it does, no wallet.trade event may be deleted",
    ),
    "wallet_seeds": JobSpec(
        "wallet_seeds", job_wallet_seeds,
        "count the tokens that ran and how many each wallet bought; feeds the grader's "
        "seed_confluence, which was None for every wallet before 2026-09-24",
    ),
    "wallet_tape": JobSpec(
        "wallet_tape", job_wallet_tape,
        "grade each chain's own tape read-only and publish backfill candidates to kv",
    ),
    "clustering": JobSpec(
        "clustering", job_clustering,
        "rebuild the entity graph from evidence we hold; free, and never gated on credits",
    ),
    "clustering_funding": JobSpec(
        "clustering_funding", job_clustering_funding,
        "buy first-funder facts, the one clustering rule that has to be paid for",
        spends_helius=True,
    ),
    "ops_prune": JobSpec(
        "ops_prune", job_ops_prune,
        "bound the scheduler's own run history",
    ),
    "retention": JobSpec(
        "retention", job_retention,
        "batched, opt-in row retention for provider_calls, triage_decisions and unchanged "
        "wallet_score_history; a dry run until retention.enabled",
    ),
    "execute_planned": JobSpec(
        "execute_planned", job_execute_planned,
        "submit live orders the engine planned, then reconcile; the only entry path to a venue",
    ),
    "nft_mint_study": JobSpec(
        "nft_mint_study", job_nft_mint_study,
        "PAPER Robinhood NFT mint study: on-chain mint/sale tape, paper mints scored on real "
        "fills at 24/72 h, PASS/FAIL gate; spends nothing",
    ),
    "experiment_gates": JobSpec(
        "experiment_gates", job_experiment_gates,
        "run proposed experiments through the replay and shadow gates, promote only what "
        "passes both within the whitelist, roll back on the pre-registered live rule",
    ),
    "loss_attribution": JobSpec(
        "loss_attribution", job_loss_attribution,
        "split each closed live loss into entry vs exit execution and rank entry features on "
        "the scanned population; writes its own table + kv, proposes nothing, spends nothing",
    ),
}


# --------------------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------------------


@dataclass
class _Run:
    name: str
    run_id: int
    started_ms: int
    timeout_s: float
    lock: JobLock
    done: threading.Event
    thread: threading.Thread | None = None
    outcome: RunOutcome | None = None
    #: The job's own connection, so a timeout can interrupt its SQL. See ``_interrupt``.
    conn: sqlite3.Connection | None = None


class Scheduler:
    """One loop over ``ops_jobs``. Construct, then :meth:`run_forever` or :meth:`run_once`."""

    def __init__(
        self,
        config: ScheduleConfig,
        *,
        jobs: Mapping[str, JobSpec] | None = None,
        conn: sqlite3.Connection | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        lock_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        budget_status: Callable[[sqlite3.Connection], Mapping[str, Any]] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.jobs: dict[str, JobSpec] = dict(jobs if jobs is not None else JOBS)
        self._factory = conn_factory or (lambda: core_db.connect())
        self.conn = conn if conn is not None else self._factory()
        self.lock_dir = lock_dir or default_lock_dir()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._budget_status = budget_status
        self._config_path = config_path
        self.running: dict[str, _Run] = {}
        self.orphans: dict[str, _Run] = {}
        self.outcomes: list[RunOutcome] = []
        self.service_lock = JobLock(
            self.lock_dir / "scheduler.lock", stale_after_s=config.service_lock_stale_s
        )
        self._stop = False
        self._last_reload = 0
        self._ticks = 0

    # ----------------------------------------------------------------- helpers

    def _now(self) -> int:
        return int(self._clock() * 1000)

    def _budget(self, conn: sqlite3.Connection) -> dict[str, Any]:
        if self._budget_status is not None:
            return dict(self._budget_status(conn))
        from kaiba.providers import helius

        return helius.budget_status(conn=conn)

    def _credits_used(self, conn: sqlite3.Connection) -> int:
        try:
            return int(self._budget(conn).get("used", 0))
        except Exception as exc:  # noqa: BLE001 - the ledger being unreadable is not a job failure
            log.warning("ops: helius ledger unreadable: %s", _error_text(exc))
            return 0

    def jittered(self, interval_s: float) -> float:
        j = self.config.jitter_pct / 100.0
        return float(interval_s) * (1.0 + self._rng.uniform(-j, j)) if j else float(interval_s)

    def backoff_interval(self, name: str, consecutive_failures: int) -> float:
        cfg = self.config.jobs[name]
        bo = self.config.backoff
        interval = float(cfg.interval_s)
        if consecutive_failures >= bo.after_failures:
            exp = consecutive_failures - bo.after_failures + 1
            interval = min(float(bo.max_s), interval * (bo.multiplier ** exp))
        return interval

    def stop(self) -> None:
        self._stop = True

    # ----------------------------------------------------------------- boot

    def sync_jobs(self, now: int | None = None) -> None:
        """Make ``ops_jobs`` match the config without re-running what is not due.

        New and overdue jobs are staggered from now; a future ``next_due_ms`` is kept, which
        is what makes a restart idempotent. A row left ``running`` by a process that died is
        recorded as an error, never quietly reset.
        """
        ts = now if now is not None else self._now()
        rows = {r["name"]: r for r in fetch_all(self.conn, "SELECT * FROM ops_jobs")}
        k = 0
        for name, cfg in self.config.jobs.items():
            spec = self.jobs.get(name)
            if spec is None:
                log.warning("ops: schedule.yaml names unknown job %r; ignored", name)
                continue
            row = rows.get(name)
            if row is None:
                self.conn.execute(
                    "INSERT INTO ops_jobs (name, enabled, interval_s, timeout_s, spends_helius, next_due_ms, "
                    "updated_ms) VALUES (?,?,?,?,?,?,?)",
                    (name, int(cfg.enabled), int(cfg.interval_s), int(cfg.timeout_s),
                     int(spec.spends_helius),
                     ts if self._is_critical(name) else ts + int(self._stagger(k) * 1000), ts),
                )
                k += 0 if self._is_critical(name) else 1
                continue
            next_due = row["next_due_ms"]
            if row["last_status"] == STATUS_RUNNING and name not in self.running and name not in self.orphans:
                self._mark_dead_run(name, ts)
            if next_due is None or int(next_due) <= ts:
                if self._is_critical(name):
                    # The trading path does not wait out a boot stagger. MEASURED 2026-09-29:
                    # after a restart execute_planned was staggered 8 minutes out, behind
                    # every overdue study.
                    next_due = ts
                else:
                    next_due = ts + int(self._stagger(k) * 1000)
                    k += 1
            self.conn.execute(
                "UPDATE ops_jobs SET enabled=?, interval_s=?, timeout_s=?, spends_helius=?, next_due_ms=?, "
                "updated_ms=? WHERE name=?",
                (int(cfg.enabled), int(cfg.interval_s), int(cfg.timeout_s), int(spec.spends_helius),
                 int(next_due), ts, name),
            )
        retired = [n for n in rows if n not in self.config.jobs]
        for name in retired:
            self.conn.execute("UPDATE ops_jobs SET enabled=0, updated_ms=? WHERE name=?", (ts, name))
        self.conn.commit()

    def _stagger(self, k: int) -> float:
        """k-th due job starts ``k`` staggers out, plus up to half a stagger of jitter.

        Half, so two consecutive jobs are always at least half a stagger apart.
        """
        s = float(self.config.stagger_s)
        return s * k + self._rng.uniform(0, s / 2.0)

    def _mark_dead_run(self, name: str, ts: int) -> None:
        note = "scheduler restarted mid-run; outcome unknown"
        self.conn.execute(
            "UPDATE ops_runs SET status=?, finished_ms=?, error=? WHERE job=? AND status=?",
            (STATUS_ERROR, ts, note, name, STATUS_RUNNING),
        )
        self.conn.execute(
            "UPDATE ops_jobs SET last_status=?, last_error=?, last_error_ms=?, failures=failures+1, "
            "consecutive_failures=consecutive_failures+1, updated_ms=? WHERE name=?",
            (STATUS_ERROR, note, ts, ts, name),
        )
        log.warning("ops: %s was running when the previous scheduler died", name)

    # ----------------------------------------------------------------- planning

    def due_jobs(self, now: int | None = None, *, only: Iterable[str] | None = None,
                 force: bool = False) -> list[str]:
        """Jobs that should start now, soonest-due first.

        ``force`` overrides the *clock* and nothing else. ``enabled: false`` is the
        operator's per-job kill switch, and a flag that silently defeats a kill switch is
        the class of bug this repository refuses elsewhere (``save_risk`` re-reads bounds
        from disk so a self-tune cannot widen them). To run a disabled job, enable it.
        """
        ts = now if now is not None else self._now()
        wanted = {n.strip() for n in only if n.strip()} if only else None
        out: list[str] = []
        for row in fetch_all(self.conn, "SELECT name, enabled, next_due_ms FROM ops_jobs ORDER BY next_due_ms ASC"):
            name = str(row["name"])
            if name not in self.jobs or name not in self.config.jobs:
                continue
            if wanted is not None and name not in wanted:
                continue
            if not row["enabled"] or not self.config.jobs[name].enabled:
                continue
            if name in self.running or name in self.orphans:
                continue
            if force or (row["next_due_ms"] is not None and int(row["next_due_ms"]) <= ts):
                out.append(name)
        return out

    def _schedule_next(self, name: str, now: int, consecutive_failures: int) -> int:
        interval = self.backoff_interval(name, consecutive_failures)
        next_due = now + int(self.jittered(interval) * 1000)
        self.conn.execute("UPDATE ops_jobs SET next_due_ms=?, updated_ms=? WHERE name=?", (next_due, now, name))
        return next_due

    # ----------------------------------------------------------------- dispatch

    def _record_skip(self, name: str, now: int, reason: str, detail: Mapping[str, Any]) -> RunOutcome:
        result = {"reason": reason, **detail}
        outcome = RunOutcome(name, STATUS_SKIPPED, now, now, result=_small(result), error=reason)
        self.conn.execute(
            "INSERT INTO ops_runs (job, started_ms, finished_ms, status, duration_ms, credits, result_json, "
            "error, host, pid) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, now, now, STATUS_SKIPPED, 0, 0, jdump(outcome.result), reason, socket.gethostname(),
             os.getpid()),
        )
        row = fetch_one(self.conn, "SELECT consecutive_failures FROM ops_jobs WHERE name=?", (name,))
        failures = int(row["consecutive_failures"]) if row else 0
        self.conn.execute(
            "UPDATE ops_jobs SET last_run_ms=?, last_status=?, last_error=?, last_duration_ms=0, "
            "last_result_json=?, runs=runs+1, updated_ms=? WHERE name=?",
            (now, STATUS_SKIPPED, reason, jdump(outcome.result), now, name),
        )
        self._schedule_next(name, now, failures)
        self.conn.commit()
        level = "warn" if reason in {REASON_BUDGET_FLOOR, REASON_ENTRY_MISSING} else "info"
        self._emit(outcome, level=level, dedupe_key=f"ops:{name}:skip:{reason}:{utc_day(now)}")
        self.outcomes.append(outcome)
        return outcome

    def _dispatch(self, name: str, now: int) -> None:
        spec = self.jobs[name]
        cfg = self.config.jobs[name]
        if spec.spends_helius:
            try:
                st = self._budget(self.conn)
            except Exception as exc:  # noqa: BLE001 - an unreadable ledger must block spending
                self._record_skip(name, now, REASON_BUDGET_FLOOR, {"ledger_error": _error_text(exc)})
                return
            remaining = int(st.get("remaining", 0))
            if remaining < self.config.helius.floor_credits:
                self._record_skip(name, now, REASON_BUDGET_FLOOR, {
                    "remaining": remaining, "floor": self.config.helius.floor_credits,
                    "used": st.get("used"), "allowance": st.get("allowance"),
                    "resets_in_s": st.get("resets_in_s"),
                })
                return
        lock = JobLock(self.lock_dir / f"{name}.lock", stale_after_s=self.config.lock_stale_s)
        try:
            lock.acquire()
        except AlreadyRunning as exc:
            self._record_skip(name, now, REASON_LOCKED, {"holder": exc.holder})
            return
        except OSError as exc:
            self._record_skip(name, now, REASON_LOCKED, {"lock_error": _error_text(exc)})
            return

        cur = self.conn.execute(
            "INSERT INTO ops_runs (job, started_ms, status, host, pid) VALUES (?,?,?,?,?)",
            (name, now, STATUS_RUNNING, socket.gethostname(), os.getpid()),
        )
        run_id = int(cur.lastrowid or 0)
        self.conn.execute(
            "UPDATE ops_jobs SET last_run_ms=?, last_status=?, updated_ms=? WHERE name=?",
            (now, STATUS_RUNNING, now, name),
        )
        self.conn.commit()

        run = _Run(name, run_id, now, float(cfg.timeout_s), lock, threading.Event())
        thread = threading.Thread(
            target=self._execute, args=(spec, cfg, run), name=f"kaiba-ops-{name}", daemon=True
        )
        run.thread = thread
        self.running[name] = run
        thread.start()

    def _execute(self, spec: JobSpec, cfg: JobConfig, run: _Run) -> None:
        """Worker-thread body. Sets ``run.outcome`` and ``run.done`` no matter what."""
        status, error, result, credits = STATUS_ERROR, None, {}, 0
        conn: sqlite3.Connection | None = None
        try:
            conn = self._factory()
            # A query still running at the deadline is aborted, and so is every statement
            # after it on this connection. Without this an abandoned job kept its read
            # snapshot for as long as its query ran -- hours -- and SQLite cannot reset the
            # WAL past a live snapshot: on 2026-09-29 the WAL reached 19.8 GB and the disk
            # filled. Jobs that open connections of their own are not covered by this.
            deadline = time.monotonic() + float(cfg.timeout_s) * self.config.hard_timeout_factor
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_EVERY_OPS
            )
            run.conn = conn
            ctx = JobContext(
                spec.name, conn, cfg.params, self.config, run.started_ms,
                run.started_ms + int(cfg.timeout_s * 1000), clock=self._clock,
            )
            before = self._credits_used(conn) if spec.spends_helius else 0
            try:
                out = spec.run(ctx)
                status, result = STATUS_OK, _small(out if out is not None else {})
            except EntryPointMissing as exc:
                status, error = STATUS_SKIPPED, REASON_ENTRY_MISSING
                result = {"reason": REASON_ENTRY_MISSING, "detail": _error_text(exc)}
            except JobFailed as exc:
                status, error, result = STATUS_ERROR, redact_text(str(exc))[:MAX_ERROR_CHARS], _small(exc.result)
            except BaseException as exc:  # noqa: BLE001 - nothing a job raises may reach the loop
                status, error = STATUS_ERROR, _error_text(exc)
                log.exception("ops: job %s raised", spec.name)
            if spec.spends_helius:
                credits = max(0, self._credits_used(conn) - before)
            try:
                conn.commit()
            except sqlite3.Error as exc:
                log.warning("ops: commit after %s failed: %s", spec.name, exc)
        except BaseException as exc:  # noqa: BLE001 - even opening the connection must not kill the thread
            status, error = STATUS_ERROR, _error_text(exc)
            log.exception("ops: job %s could not be set up", spec.name)
        finally:
            run.conn = None
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            run.outcome = RunOutcome(spec.name, status, run.started_ms, self._now(), result, error, credits)
            run.done.set()

    # ----------------------------------------------------------------- collection

    def _reap(self, now: int) -> None:
        for name, run in list(self.running.items()):
            if run.done.is_set() and run.outcome is not None:
                self._finish(run, run.outcome, now)
                del self.running[name]
            elif now - run.started_ms > run.timeout_s * 1000:
                outcome = RunOutcome(
                    name, STATUS_TIMEOUT, run.started_ms, now,
                    error=f"timed out after {run.timeout_s:g}s; thread abandoned until it returns",
                )
                self._finish(run, outcome, now, release_lock=False)
                del self.running[name]
                self.orphans[name] = run
        for name, run in list(self.orphans.items()):
            if not run.done.is_set() and self._past_hard_deadline(run, now):
                self._interrupt(run)
            if run.done.is_set():
                run.lock.release()
                del self.orphans[name]
                late = run.outcome.as_dict() if run.outcome else {}
                log.warning("ops: %s finished %.0fs after its timeout: %s", name,
                            (now - run.started_ms) / 1000.0 - run.timeout_s, jdump(late)[:300])
                self.conn.execute(
                    "UPDATE ops_runs SET result_json=? WHERE id=?",
                    (jdump({"late": True, **late.get("result", {})}), run.run_id),
                )
                self.conn.commit()

    @staticmethod
    def _interrupt(run: _Run) -> None:
        """Abort a job's running SQL once it is past its hard deadline.

        Backs up the deadline progress handler set in ``_execute``, which cannot fire while
        a statement is waiting on a lock rather than executing.
        """
        conn = run.conn
        if conn is None:
            return
        try:
            conn.interrupt()
        except Exception:  # noqa: BLE001 - the thread may have closed it a moment ago
            pass

    def _is_critical(self, name: str) -> bool:
        return name in self.config.critical_jobs

    def _is_long(self, name: str) -> bool:
        jc = self.config.jobs.get(name)
        return jc is not None and float(jc.timeout_s) >= self.config.long_timeout_s

    def _past_hard_deadline(self, run: _Run, now: int) -> bool:
        return now - run.started_ms > run.timeout_s * self.config.hard_timeout_factor * 1000

    def _live_orphans(self, now: int) -> list[str]:
        """Timed-out, non-critical jobs still working inside their hard deadline.

        They hold general slots. MEASURED 2026-09-29: the ops process was OOM-killed at
        6.4 GB with wallet_seeds, wallet_naming and variable_study running and
        wallet_regrade, wallet_buyers and entry_study still grinding on past their
        timeouts -- a timeout freed the slot while the work carried on, so a two-CPU,
        7.6 GB box ran six heavy jobs at once. It froze the whole box for twenty minutes
        (SSH, protection, ingest) and had done the same twice on 09-28.
        """
        return [
            n for n, run in self.orphans.items()
            if not self._is_critical(n) and not run.done.is_set()
            and not self._past_hard_deadline(run, now)
        ]

    def _has_slot(self, name: str) -> bool:
        """Whether ``name`` may start now. Critical jobs draw on their own slots."""
        if self._is_critical(name):
            busy = sum(1 for n in self.running if self._is_critical(n))
            return busy < self.config.critical_slots
        general = [n for n in self.running if not self._is_critical(n)]
        general += self._live_orphans(self._now())
        if len(general) >= self.config.max_concurrent:
            return False
        if self._is_long(name):
            return sum(1 for n in general if self._is_long(n)) < self.config.max_concurrent_long
        return True

    def _finish(self, run: _Run, outcome: RunOutcome, now: int, *, release_lock: bool = True) -> None:
        name = run.name
        if release_lock:
            run.lock.release()
        row = fetch_one(self.conn, "SELECT consecutive_failures FROM ops_jobs WHERE name=?", (name,))
        prior_failures = int(row["consecutive_failures"]) if row else 0
        failed = outcome.status in {STATUS_ERROR, STATUS_TIMEOUT}
        failures = prior_failures + 1 if failed else (prior_failures if outcome.status == STATUS_SKIPPED else 0)

        self.conn.execute(
            "UPDATE ops_runs SET finished_ms=?, status=?, duration_ms=?, credits=?, result_json=?, error=? "
            "WHERE id=?",
            (outcome.finished_ms, outcome.status, outcome.duration_ms, outcome.credits,
             jdump(outcome.result), outcome.error, run.run_id),
        )
        self.conn.execute(
            "UPDATE ops_jobs SET last_status=?, last_duration_ms=?, last_result_json=?, runs=runs+1, "
            "credits_total=credits_total+?, consecutive_failures=?, updated_ms=?, "
            "last_success_ms=CASE WHEN ? THEN ? ELSE last_success_ms END, "
            "last_error=CASE WHEN ? THEN ? ELSE last_error END, "
            "last_error_ms=CASE WHEN ? THEN ? ELSE last_error_ms END, "
            "failures=failures + ? WHERE name=?",
            (outcome.status, outcome.duration_ms, jdump(outcome.result), outcome.credits, failures, now,
             int(outcome.status == STATUS_OK), outcome.finished_ms,
             int(outcome.error is not None), outcome.error,
             int(failed), outcome.finished_ms,
             int(failed), name),
        )
        self._schedule_next(name, now, failures)
        self.conn.commit()
        self.outcomes.append(outcome)

        if failed:
            self._emit(outcome, level="warn")
        elif outcome.status == STATUS_OK and prior_failures > 0:
            self._emit(outcome, level="info", note=f"recovered after {prior_failures} failure(s)")
        elif outcome.status == STATUS_SKIPPED:
            self._emit(outcome, level="warn", dedupe_key=f"ops:{name}:skip:{outcome.error}:{utc_day(now)}")
        log.info("ops: %s %s in %dms credits=%d %s", name, outcome.status, outcome.duration_ms,
                 outcome.credits, (outcome.error or jdump(outcome.result))[:160])

    def _emit(self, outcome: RunOutcome, *, level: str, note: str | None = None,
              dedupe_key: str | None = None) -> None:
        payload = {"action": "ops.job", **outcome.as_dict()}
        if note:
            payload["note"] = note
        try:
            emit(EventKind.SYSTEM, payload, level=level, dedupe_key=dedupe_key, conn=self.conn)
        except sqlite3.Error as exc:  # telemetry must not break the loop
            log.debug("ops: event not recorded: %s", exc)

    # ----------------------------------------------------------------- loop

    def _heartbeat(self) -> None:
        self.service_lock.heartbeat()
        for run in self.running.values():
            run.lock.heartbeat()

    def _maybe_reload(self, now: int) -> None:
        if self._config_path is None or now - self._last_reload < self.config.reload_every_s * 1000:
            return
        self._last_reload = now
        try:
            fresh = load_config(self._config_path)
        except ScheduleConfigError as exc:
            log.warning("ops: schedule config unusable, keeping the last good one: %s", exc)
            return
        if fresh != self.config:
            log.info("ops: schedule config changed; applying")
            self.config = fresh
            self.sync_jobs(now)

    def tick(self, *, only: Iterable[str] | None = None, force: bool = False) -> list[str]:
        """One pass: reap finished runs, heartbeat, dispatch what is due. Returns what started."""
        now = self._now()
        self._ticks += 1
        self._reap(now)
        self._heartbeat()
        self._maybe_reload(now)
        started: list[str] = []
        for name in self.due_jobs(now, only=only, force=force):
            if not self._has_slot(name):
                continue  # not break: a critical job further down still has its own slot
            self._dispatch(name, now)
            if name in self.running:
                started.append(name)
        return started

    def wait_idle(self, *, poll_s: float = 0.05) -> None:
        """Block until no run is in flight (timeouts included). Orphans are not waited for."""
        while self.running:
            self._sleep(poll_s)
            self._reap(self._now())
            self._heartbeat()

    def run_once(self, *, only: Iterable[str] | None = None, force: bool = False) -> list[RunOutcome]:
        """Dispatch every due job, wait for all of them, return their outcomes."""
        self.sync_jobs()
        start = len(self.outcomes)
        pending = self.due_jobs(only=only, force=force)
        while pending or self.running:
            now = self._now()
            self._reap(now)
            self._heartbeat()
            for name in list(pending):
                if self._has_slot(name):
                    pending.remove(name)
                    self._dispatch(name, now)
            if self.running:
                self._sleep(0.05)
        return self.outcomes[start:]

    def run_forever(self, *, only: Iterable[str] | None = None) -> int:
        """The service loop. Returns an exit code."""
        try:
            self.service_lock.acquire()
        except AlreadyRunning as exc:
            log.error("ops: another scheduler holds %s: %s", exc.path, exc.holder)
            return 3
        try:
            self._install_signals()
            self.sync_jobs()
            log.info("ops: scheduler up; %d job(s) configured, tick %.1fs", len(self.config.jobs),
                     self.config.tick_s)
            while not self._stop:
                try:
                    self.tick(only=only)
                except Exception as exc:  # noqa: BLE001 - the loop itself must survive a bad tick
                    log.exception("ops: tick failed: %s", _error_text(exc))
                self._sleep(self.config.tick_s)
            self._drain()
        finally:
            self.service_lock.release()
        return 0

    def _drain(self) -> None:
        deadline = self._now() + int(self.config.stop_grace_s * 1000)
        while self.running and self._now() < deadline:
            self._sleep(0.2)
            self._reap(self._now())
        for name, run in list(self.running.items()):
            log.warning("ops: leaving %s to finish on its own; its lock goes stale in %.0fs", name,
                        self.config.lock_stale_s)
            run.lock.release()
            del self.running[name]

    def _install_signals(self) -> None:
        def on_signal(_sig: int, _frm: object) -> None:
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError, AttributeError):  # not main thread, or unsupported
                pass


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------

STATE_ORDER = ("disabled", "running", "never_ran", "missing", "backoff", "failing", "late", "skipped",
               "stale", "ok")


def status(
    conn: sqlite3.Connection | None = None,
    config: ScheduleConfig | None = None,
    *,
    now: int | None = None,
) -> list[dict[str, Any]]:
    """Every configured job with a derived state. Reads only the database."""
    c = conn or core_db.get_conn()
    ts = now if now is not None else now_ms()
    cfg = config
    if cfg is None:
        try:
            cfg = load_config()
        except ScheduleConfigError:
            cfg = ScheduleConfig()
    after = cfg.backoff.after_failures
    out: list[dict[str, Any]] = []
    try:
        rows = fetch_all(c, "SELECT * FROM ops_jobs ORDER BY name")
    except sqlite3.Error:
        rows = []
    for r in rows:
        interval = int(r["interval_s"])
        timeout = int(r["timeout_s"])
        result = jload(r["last_result_json"], {})
        fails = int(r["consecutive_failures"])
        last_run = r["last_run_ms"]
        last_ok = r["last_success_ms"]
        next_due = r["next_due_ms"]
        if not r["enabled"]:
            state = "disabled"
        elif r["last_status"] == STATUS_RUNNING and last_run and ts - int(last_run) <= (timeout + 60) * 1000:
            state = "running"
        elif int(r["runs"]) == 0:
            state = "never_ran"
        elif r["last_status"] == STATUS_SKIPPED and result.get("reason") == REASON_ENTRY_MISSING:
            state = "missing"
        elif fails >= after:
            state = "backoff"
        elif fails > 0:
            state = "failing"
        elif next_due is not None and ts > int(next_due) + max(interval, 60) * 1000:
            state = "late"
        elif r["last_status"] == STATUS_SKIPPED:
            state = "skipped"
        elif last_ok is None or ts - int(last_ok) > (2 * interval + timeout) * 1000:
            state = "stale"
        else:
            state = "ok"
        out.append({
            "name": r["name"],
            "state": state,
            "enabled": bool(r["enabled"]),
            "interval_s": interval,
            "timeout_s": timeout,
            "spends_helius": bool(r["spends_helius"]),
            "last_status": r["last_status"],
            "last_run_ms": last_run,
            "last_success_ms": last_ok,
            "since_success_s": None if last_ok is None else max(0, (ts - int(last_ok)) // 1000),
            "next_due_ms": next_due,
            "next_in_s": None if next_due is None else (int(next_due) - ts) // 1000,
            "last_duration_ms": r["last_duration_ms"],
            "consecutive_failures": fails,
            "runs": int(r["runs"]),
            "failures": int(r["failures"]),
            "credits_total": int(r["credits_total"]),
            "last_error": r["last_error"],
            "last_result": result,
            "why": JOBS[r["name"]].why if r["name"] in JOBS else "",
        })
    return out


def recent_runs(conn: sqlite3.Connection | None = None, *, limit: int = 20,
                job: str | None = None) -> list[dict[str, Any]]:
    c = conn or core_db.get_conn()
    sql = "SELECT * FROM ops_runs"
    params: list[Any] = []
    if job:
        sql += " WHERE job = ?"
        params.append(job)
    sql += " ORDER BY started_ms DESC, id DESC LIMIT ?"
    params.append(int(limit))
    rows = fetch_all(c, sql, params)
    for r in rows:
        r["result"] = jload(r.pop("result_json"), {})
    return rows


def service_holder(lock_dir: Path | None = None, *, stale_after_s: float | None = None,
                   now: int | None = None) -> dict[str, Any] | None:
    """Who holds the service lock, if anyone alive, plus how old their heartbeat is.

    ``heartbeat_age_s`` matters because on Windows a dead holder cannot be detected by pid
    (``os.kill(pid, 0)`` would terminate it, so ``_pid_alive`` always says True there).
    Until the heartbeat ages out, a hard-killed scheduler still looks alive, so the status
    screen shows the age rather than only the claim.
    """
    lock = JobLock((lock_dir or default_lock_dir()) / "scheduler.lock",
                   stale_after_s=stale_after_s if stale_after_s is not None
                   else ScheduleConfig().service_lock_stale_s)
    holder = lock.read()
    if holder is None or lock.is_stale(holder):
        return None
    ts = now if now is not None else now_ms()
    beat = holder.get("heartbeat_ms") or holder.get("started_ms") or 0
    return {**holder, "heartbeat_age_s": max(0, (ts - int(beat)) // 1000)}


__all__ = [
    "CREDITS_PER_WALLET_EST",
    "JOBS",
    "TAPE_MODEL_VERSION",
    "AlreadyRunning",
    "EntryPointMissing",
    "JobContext",
    "JobFailed",
    "JobLock",
    "JobSpec",
    "RunOutcome",
    "ScheduleConfig",
    "ScheduleConfigError",
    "Scheduler",
    "config_path",
    "default_lock_dir",
    "flow_targets",
    "load_config",
    "quota_add",
    "quota_used",
    "recent_runs",
    "service_holder",
    "stale_scores",
    "status",
    "ungraded_buyers",
    "utc_day",
]
