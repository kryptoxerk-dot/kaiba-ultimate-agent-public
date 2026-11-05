"""One frozen SOL nominee inside the existing wallet_tracked job; no scheduler.

An owner-applied four-line hook calls run_selected_under_job after ordinary quota
checks. This module never acquires/releases a job lock or patches a provider.
It commits one unit and 250 MODEL credits before one fresh 100-row backfill. The
entire debit is retained; shared ledger deltas never justify refunds. An admitted
selection is not automatically retried after interruption. No grading or roster
change occurs. HTTP redirects and Enhanced's assumed rate are NOT invoice bounds.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from kaiba.core.config import get_settings
from kaiba.core.schemas import Chain, normalize_address
from kaiba.ingest import backfill
from kaiba.intelligence import grade
from kaiba.intelligence.wallet_candidates import review_candidates
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred
from kaiba.learning.autolearn_wallets import _acquisition_snapshot
from kaiba.ops import scheduler
from kaiba.providers import helius

VERSION = "scheduled-wallet-acquisition/1"
DAY_MS = 86_400_000
RESERVE = 250
ATTEMPT_KEY = "autolearn:wallet-acquisition:attempts"
MAX_ATTEMPTS = 32
STATE_BYTES = 64 * 1024
WAL_LIMIT = 1_500_000_000
ROOT = Path(__file__).resolve().parents[2]
# Exact normalized source from root's 2026-10-15T20:36:35Z VPS capture. Scheduler
# is deliberately excluded: its separately reviewed four-line owner hook changes it.
COLLECTOR_SOURCES = {
    "kaiba/ingest/backfill.py": "5ef71acde394a04acf32c853b3a04f1a13f34861e78800cbd973d8b13a208126",
    "kaiba/providers/helius.py": "a4e7d8489c903d47f7fd9fdd318289b6404043413c2631775573c3614c20ad33",
    "kaiba/providers/_http.py": "5b04162ca07e41aebdfb95bedb802bff5753151c982b0961b2fb7a1744a43eec",
    "kaiba/core/limiter.py": "4ac5bcfc62e491f04abd63910daffc899d8a0dae2506e339052b7b2a24111cc1",
}


class AcquisitionRefused(RuntimeError):
    """Fixed public codes only; never include addresses, SQL, payloads or provider notes."""


def _require(value: bool, reason: str):
    if not value:
        raise AcquisitionRefused(reason)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _clock(value) -> bool:
    return type(value) is int and value > 0


def _sources() -> dict:
    actual = {name: hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
              for name in COLLECTOR_SOURCES}
    _require(actual == COLLECTOR_SOURCES, "collector_source_changed")
    actual["adapter"] = hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    for name, function in (("registry_adapter", _acquisition_snapshot), ("reviewer", review_candidates)):
        actual[name] = hashlib.sha256(Path(function.__code__.co_filename).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return actual


def _wal(db: Path):
    path = Path(str(db) + "-wal")
    size = path.stat().st_size if path.exists() else 0
    if size >= WAL_LIMIT:
        raise SourceDeferred("wal_over_limit; retry after at least 60s")


class _Reader(GuardedReader):
    def guard(self):
        _wal(self.db_path)
        return self._wal_bytes()


def _read(path: Path, limit: int) -> dict:
    _require(not any(p.is_symlink() or p.is_junction() for p in (path, *path.absolute().parents)), "input_symlink")
    _require(stat.S_ISREG(path.stat().st_mode), "input_not_regular")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, "input_size_cap")
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(AcquisitionRefused("nonfinite_json")))
    _require(isinstance(value, dict), "input_shape")
    return value


def _review(path: Path, now: int) -> tuple[dict, list[dict]] | None:
    if not path.exists():
        return None
    review = _read(path, 2_000_000)
    observed = review.get("observed_ms")
    _require(_clock(observed) and observed <= now, "review_clock_invalid")
    if now - observed > 3_600_000 or review.get("acquisition_learning", {}).get("status") != "complete":
        return None
    queue = review.get("acquisition_queue", [])
    _require(isinstance(queue, list) and len(queue) <= 20, "queue_shape")
    summaries = review.get("acquisition_candidates", [])
    _require(isinstance(summaries, list) and len(summaries) <= 20
             and all(isinstance(r, dict) for r in summaries), "candidate_shape")
    seen = set()
    for row in queue:
        _require(isinstance(row, dict) and row.get("chain") == "sol", "queue_identity")
        address = row.get("address")
        _require(isinstance(address, str) and normalize_address(address, Chain.SOL) == address and address not in seen,
                 "queue_identity")
        seen.add(address)
        selected, checked = row.get("selected_ms"), row.get("registry_checked_ms")
        _require(_clock(selected) and 0 <= now - selected <= 13 * DAY_MS
                 and _clock(checked) and selected <= checked <= now and now - checked <= 3_600_000,
                 "selection_or_registry_clock")
        _require(isinstance(row.get("evidence_id"), str) and row["evidence_id"].startswith("wallet-observe:")
                 and len(row["evidence_id"]) <= 128, "selection_evidence")
        for name in ("selection_hash", "nominations_hash"):
            _require(isinstance(row.get(name), str) and len(row[name]) == 64
                     and all(c in "0123456789abcdef" for c in row[name]), "selection_hash")
        receipts = row.get("nominations")
        _require(isinstance(receipts, list) and 1 <= len(receipts) <= 4, "nomination_receipts")
        _require(all(isinstance(r, dict) and _clock(r.get("observed_ms")) and r["observed_ms"] <= selected
                     and isinstance(r.get("receipt_hash"), str) and len(r["receipt_hash"]) == 64 for r in receipts),
                 "nomination_clock_or_hash")
        matched = [r for r in summaries if r.get("chain") == "sol" and r.get("address") == address]
        _require(len(matched) == 1 and matched[0].get("exclusions") == []
                 and matched[0].get("status") == "manual_probe_required"
                 and matched[0].get("selected_ms") == selected
                 and matched[0].get("selection_hash") == row["selection_hash"], "selection_not_revalidated")
    return review, queue


def _held(ctx, now: int):
    _require(ctx.name == "wallet_tracked", "wrong_job")
    path = scheduler.default_lock_dir() / "wallet_tracked.lock"
    holder = _read(path, 4096)
    _require(holder.get("pid") == os.getpid() and holder.get("host") == socket.gethostname()
             and _clock(holder.get("heartbeat_ms")) and 0 <= now - holder["heartbeat_ms"] <= ctx.config.lock_stale_s * 1000,
             "existing_job_lock_not_held")


def _pick(reader, queue: list[dict], now: int):
    snapshot = _acquisition_snapshot(reader, queue)
    _require(all(0 < r["updated_ms"] <= now for r in snapshot["cursors"].values()), "cursor_clock_invalid")
    checked = review_candidates({"controls": [{"chain": "sol", "address": r["address"],
        "observed_ms": r["selected_ms"], "source": "frozen_acquisition_nomination"} for r in queue],
        "history": snapshot["history"], "exclusions": snapshot["exclusions"]}, now_ms=now, budget=0, exploration_slots=0)
    candidates = {r["address"]: r for r in checked["candidates"]}
    for row in queue:
        address = row["address"]
        wallet, score, cursor = (snapshot[k].get(address) for k in ("wallets", "scores", "cursors"))
        if wallet is None or wallet.get("cohort") != "research" or candidates[address]["exclusions"]:
            continue
        if score and (score["grade"] not in {"C", "D", "UNSCORED"} or score["model_version"] != grade.MODEL_ID_TAPE):
            continue
        if cursor and cursor["updated_ms"] >= now - DAY_MS:
            continue
        evidence = {"wallet": wallet, "score": score, "cursor": cursor,
                    "history": [r for r in snapshot["history"] if r["address"] == address]}
        return row, _hash(evidence)
    return None


class _Connection(sqlite3.Connection):
    def arm(self, path: Path, deadline: float):
        self.path, self.deadline, self.fault = path, deadline, None
        self.statement, self.transaction = time.monotonic(), None
        self.set_trace_callback(lambda sql: setattr(self, "statement", time.monotonic()))
        self.set_progress_handler(self._progress, 1000)
        self.set_authorizer(self._authorize)

    def _progress(self):
        now = time.monotonic()
        if (self.fault or now >= self.deadline or now - self.statement >= 3
                or self.transaction is not None and now - self.transaction >= 3):
            self.fault = self.fault or "source_statement_deadline"
            return 1
        return 0

    @staticmethod
    def _authorize(action, table, col, db, trigger):
        allowed = {"ops_quota", "kv", "swaps", "transfers", "swap_meta", "events", "sqlite_sequence",
                   "provider_budget", "provider_calls", "provider_state", "provider_family_bans"}
        return sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and table not in allowed else sqlite3.SQLITE_OK

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "ROLLBACK":
            self.transaction = None
            return super().execute(sql, parameters)
        if self.fault:
            raise SourceDeferred(self.fault)
        if self.transaction is not None and time.monotonic() - self.transaction >= 3:
            self.fault = "source_transaction_deadline"
            raise SourceDeferred(self.fault)
        try:
            _wal(self.path)
        except SourceDeferred as exc:
            self.fault = str(exc)
            raise
        _require(time.monotonic() < self.deadline, "job_deadline")
        self.statement = time.monotonic()
        if sql.lstrip().upper().startswith("BEGIN"):
            self.transaction = self.statement
        result = super().execute(sql, parameters)
        if time.monotonic() - self.statement >= 3:
            self.fault = "source_statement_deadline"
            raise SourceDeferred(self.fault)
        if not self.in_transaction:
            self.transaction = None
        return result

    def commit(self):
        self.execute("COMMIT") if self.in_transaction else None


@contextmanager
def _connection(path: Path, deadline: float):
    _wal(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=.25, factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.arm(path, deadline)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


class _Rows:
    def __init__(self, conn):
        self.conn = conn

    def rows(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]


def _strict_budget(conn, now: int, floor: int):
    rows = conn.execute("SELECT credits FROM provider_budget WHERE provider='helius' AND period=? LIMIT 501",
                        (helius._period(now),)).fetchall()
    _require(len(rows) <= 500 and all(type(r["credits"]) is int and r["credits"] >= 0 for r in rows), "credit_ledger_invalid")
    remaining = helius.monthly_allowance() - sum(r["credits"] for r in rows)
    _require(remaining - floor >= RESERVE, "monthly_floor_or_reserve")


def _attempt_id(row: dict) -> str:
    return _hash(["sol", row["address"], row["selected_ms"], row["evidence_id"], row["selection_hash"]])


def _load_attempts(conn) -> dict:
    row = conn.execute("SELECT CASE WHEN length(value)<=? THEN value END AS value FROM kv WHERE key=?",
                       (STATE_BYTES, ATTEMPT_KEY)).fetchone()
    if row is None:
        return {"version": VERSION, "attempts": {}, "review_ms": 0, "review_hash": None}
    _require(row["value"] is not None, "attempt_state_size")
    state = json.loads(row["value"])
    _require(isinstance(state, dict) and state.get("version") == VERSION
             and isinstance(state.get("attempts"), dict) and len(state["attempts"]) <= MAX_ATTEMPTS
             and type(state.get("review_ms")) is int, "attempt_state_invalid")
    for aid, attempt in state["attempts"].items():
        selection = attempt.get("selection") if isinstance(attempt, dict) else None
        _require(isinstance(selection, dict) and all(k in selection for k in
                 ("chain", "address", "selected_ms", "evidence_id", "selection_hash"))
                 and selection["chain"] == "sol" and _clock(selection["selected_ms"])
                 and aid == _attempt_id(selection)
                 and _clock(attempt.get("admitted_ms")) and attempt.get("charged_credits") == RESERVE,
                 "attempt_identity_invalid")
        _require(attempt.get("status") in {"dispatch_unknown", "returned", "returned_partial_unknown"},
                 "attempt_status_invalid")
    return state


def _save_attempts(conn, state: dict, now: int):
    raw = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
    _require(len(raw.encode()) <= STATE_BYTES, "attempt_state_size")
    conn.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE "
                 "SET value=excluded.value,updated_ms=excluded.updated_ms", (ATTEMPT_KEY, raw, now))


def run_selected_under_job(ctx, *, allow: int, room, cohorts, review_path: Path | None = None) -> dict | None:
    """Return None for no fresh eligible nominee; otherwise own this job outcome.

    Only the existing dispatcher may call this while holding wallet_tracked.lock.
    A non-None result must RETURN from the owner job before legacy quota_add/grade.
    """
    now, settings = ctx.now(), get_settings()
    db = settings.db_path
    _wal(db)
    loaded = _review(review_path or settings.kaiba_data_dir / "learning/autolearn/latest-wallet-review.json", now)
    if loaded is None or not loaded[1] or allow < 1 or room.credits < RESERVE or "research" not in (cohorts or []):
        return None
    review, queue = loaded
    _held(ctx, now)
    sources = _sources()
    _require(ctx.config.jobs["wallet_tracked"].enabled and int(ctx.param("pages", 1)) == 1
             and ctx.param("fresh", False) is True and int(ctx.param("max_credits_per_run", 250)) >= RESERVE,
             "existing_job_protocol")
    _require(not ctx.conn.in_transaction and ctx.time_left_s() >= 5, "job_transaction_or_deadline")
    _require(Path(scheduler._db_path(ctx.conn)).resolve() == db.resolve(), "job_database_mismatch")
    deadline = time.monotonic() + min(75, ctx.time_left_s())
    with _connection(db, deadline) as conn:
        already = set(_load_attempts(conn)["attempts"])
    unattempted = [row for row in queue if _attempt_id(row) not in already]
    if not unattempted:
        return {"wallets": 0, "reason": "selection_already_admitted_no_automatic_retry", "admitted": len(queue)}
    reader = _Reader(db, query_seconds=3, max_rows=2001)
    selected = _pick(reader, unattempted, now)
    if selected is None:
        return None
    nominee, registry_hash = selected
    aid, day = _attempt_id(nominee), scheduler.utc_day(now)
    with _connection(db, deadline) as conn:
        conn.execute("BEGIN IMMEDIATE")
        enabled = conn.execute("SELECT enabled FROM ops_jobs WHERE name='wallet_tracked'").fetchone()
        _require(enabled is not None and enabled["enabled"] == 1, "current_job_disabled_or_unknown")
        current = _pick(_Rows(conn), [nominee], ctx.now())
        _require(current is not None and current[1] == registry_hash, "registry_changed_at_admission")
        state = _load_attempts(conn)
        _require(review["observed_ms"] >= state["review_ms"] and
                 (review["observed_ms"] != state["review_ms"] or state["review_hash"] in {None, _hash(review)}),
                 "review_rollback_or_conflict")
        state["attempts"] = {key: r for key, r in state["attempts"].items()
            if 0 <= now - r["selection"]["selected_ms"] <= 13 * DAY_MS}
        if aid in state["attempts"]:
            return {"wallets": 0, "reason": "selection_already_admitted_no_automatic_retry", "attempt_id": aid}
        _require(len(state["attempts"]) < MAX_ATTEMPTS, "attempt_state_capacity")
        local = scheduler.JobContext("wallet_tracked", conn, ctx.params, ctx.config,
            ctx.started_ms, ctx.deadline_ms, clock=ctx.clock)
        used, credits = local.quota_used()
        fresh_room = scheduler.gather_credit_room(local,
            daily_default=int(ctx.param("wallets_per_day", 5)) * 125 * 2,
            run_max=int(ctx.param("max_credits_per_run", 250)))
        _strict_budget(conn, now, ctx.config.helius.floor_credits)
        _require(scheduler.utc_day(local.now()) == day and used < int(ctx.param("wallets_per_day", 5))
                 and int(ctx.param("wallets_per_run", 1)) >= 1 and fresh_room.credits >= RESERVE,
                 "count_credit_or_day_changed")
        local.quota_add(units=1, credits=RESERVE)
        state["attempts"][aid] = {"selection": {k: nominee[k] for k in
            ("chain", "address", "selected_ms", "evidence_id", "selection_hash", "nominations_hash", "nominations")},
            "admitted_ms": now, "period": day, "charged_credits": RESERVE,
            "status": "dispatch_unknown", "sources": sources}
        state.update(review_ms=review["observed_ms"], review_hash=_hash(review))
        _save_attempts(conn, state, now)
        conn.execute("COMMIT")  # Durable debit/attempt before ANY provider request.
        _held(ctx, ctx.now())
        _require(_sources() == sources and scheduler.utc_day(ctx.now()) == day, "source_or_day_changed_before_dispatch")
        _strict_budget(conn, ctx.now(), ctx.config.helius.floor_credits)
        _require(not conn.in_transaction, "provider_inside_transaction")
        report = backfill.backfill_wallets(conn, Chain.SOL, wallets=[nominee["address"]],
            pages=1, page_limit=100, fresh=True, with_meta=True, rebuild_buyers=False, max_credits=RESERVE)
        result = report.results[0] if len(report.results) == 1 else None
        # A returned source page is observable progress even when the independent
        # metadata walk was already exhausted or unavailable. Neither means full history.
        status = "returned" if result is not None and not result.error and result.pages == 1 else "returned_partial_unknown"
        counts = {k: getattr(report, k) for k in ("wallets", "pages", "transactions", "swaps_written", "meta_written", "wallets_failed")}
        counts["meta_pages"] = result.meta_pages if result is not None else 0
        conn.execute("BEGIN IMMEDIATE")
        state = _load_attempts(conn)
        state["attempts"][aid].update(status=status, finished_ms=ctx.now(), counts=counts)
        _save_attempts(conn, state, ctx.now())
        conn.execute("COMMIT")
    return {**counts, "attempt_id": aid, "acquisition_status": status, "graded": 0,
        "used_today": used + 1, "credits_today": credits + RESERVE, "credits_reserved": RESERVE,
        "refund": 0, "credit_basis": "conservative_modeled_reserve_not_invoice", "billing_verified": False,
        "backfill_shared_ledger_delta": report.credits_spent, "ordinary_slots_unused": max(0, allow - 1),
        "full_history_proven": False, "copy_qualified": False, "funding_allowed": False,
        "original_selected_ms": nominee["selected_ms"], "source_versions": sources}
