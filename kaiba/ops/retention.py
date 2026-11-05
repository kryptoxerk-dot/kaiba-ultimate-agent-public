"""Opt-in, bounded row retention for the tables that only grow.

MEASURED on the live box 2026-10-01 (docs/research/audit-20261001-reliability.md, P0-3):
the database grows ~2.9 GB a day and nothing in ``kaiba/`` deletes from ``swaps``,
``events``, ``wallet_score_history`` or ``provider_calls``. This module bounds three tables:

* ``provider_calls`` and ``triage_decisions``: rows older than N days (default 14). Every
  reader in the tree reads a recent window (limiter/tape/stonkfun accounting, the scanner's
  id watermark, triage's verdict split); ``learning.replay`` loses lookback past N days.
* ``wallet_score_history``: UNCHANGED rows older than N days. A row is unchanged when it
  repeats the last KEPT row of its wallet -- same grade, same ``model_version``, score
  within :data:`HISTORY_MIN_SCORE_DELTA` -- which is the rule the grader now writes under
  (``grade._history_moved``). Every grade transition and the latest row per
  ``(chain, address)`` are kept, so the as-of lookup the readers use (latest row at or
  before T) answers the same grade for every T; ``tests/test_retention.py`` checks that
  property row by row.

* ``events`` of kind ``wallet.trade`` older than N days (default 7) -- and ONLY those, and
  only behind a gate of their own. They held the only copy of GMGN's wallet labels; the
  ``wallet_feed_tags`` rollup (kaiba/intelligence/feed_tags.py) now holds them, and
  :func:`kaiba.intelligence.feed_tags.retention_gate` refuses every delete until its
  backfill has passed every event id below the live writer's first one AND an exact parity
  check (table rows == rows re-derived from the events, for sampled wallets, plus every
  reader's old answer == its new one) has passed and is recent. Needs
  ``wallet_trade_enabled`` as well as ``enabled``. MEASURED 2026-10-02: 336,028 such events
  a day, ~0.4 GB/day with their four index entries; 1,649,589 already older than 7 days.

* ``swaps`` older than N days (default 14), behind ``swaps_enabled`` -- added 2026-10-13
  when the disk reached 98%. MEASURED that day: 23.5M rows spanning 09-21..10-12, about
  1M rows a day, the largest single grower. Three properties make it safe to bound:

  - **The bounded readers fit inside the window.** The widest scheduled ones are
    signal_audit (12 d + 30 min), proven (10 d), copy_leaders (7 d) and the paper sprint
    (from its start minus 30 min). loss_attribution's decision scan is capped below the
    window in config/schedule.yaml (``scan_days``), not left to read a hole.
  - **The all-history readers are told.** Every delete stamps :data:`SWAPS_HORIZON_KEY` in
    the same transaction. ``grade.history_truncated`` treats a wallet whose kept tape reaches
    back to the horizon as truncated (so a pruned history can never pass the A gate as
    complete), and ``backfill.rebuild_first_buyers`` refuses to re-rank a token that
    predates the horizon (its early buys are gone; the stored ranks are the record).
  - **Our own trades keep their tape.** Swaps of any token that has ever had a row in
    ``positions`` (live or paper) are never deleted, so exit_study, loss_attribution and
    loss_review keep the price path of everything we held.

  ``ts_ms`` is chain time and ``id`` is insert order -- backfill inserts old trades under new
  ids -- so the boundary search used for the other tables does not hold here. The prune
  walks the WHOLE id range in windows with a cursor kept in kv, and a pass repeats every
  ``swaps_pass_interval_s``.

* ``events`` of the high-volume telemetry kinds in ``events_other_kinds`` (wallet.graded,
  system heartbeats, scan/triage/provider telemetry) older than N days (default 30), behind
  ``events_other_enabled``. **A row with a ``dedupe_key`` is never deleted**: the row IS the
  dedupe record (``events.emit_once`` returns "new" when its insert succeeds), so deleting
  an old ``token.created`` would make a re-sighted token look like a fresh launch.

DISABLED BY DEFAULT. :func:`run` is the only entry point that deletes, and it deletes
nothing unless ``retention.enabled`` is true in config/schedule.yaml AND the caller did not
ask for a dry run. Otherwise it returns a dry-run estimate built from bounded queries.

How it keeps out of protection's way (protection writes every tick and its connection
waits ``busy_timeout`` = 10 s for the lock before it reports ``database is locked``):

* a delete transaction touches at most ``batch_rows`` rows (hard maximum 5,000), and the
  batch halves whenever one transaction took longer than ``max_tx_ms``;
* the rows a transaction will delete are read first, OUTSIDE it, so the write lock is held
  for the write and not for cold disk reads on an IO-saturated box;
* it sleeps ``sleep_s`` between transactions and stops when ``budget_s`` is spent;
* a refused BEGIN (the lock stayed busy past this connection's own busy_timeout) halves
  the batch and backs off; three in a row end the run with ``stopped: locked``.

Deleting frees pages for reuse; it does not shrink the file (``VACUUM`` needs about the
database's size in temporary space, which the box does not have). The point is to stop
the growth.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.db import tx
from kaiba.intelligence import feed_tags

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

#: The hard ceiling on rows in one delete transaction, whatever the config says.
MAX_BATCH_ROWS = 5_000

#: Same number as ``kaiba.intelligence.grade.HISTORY_MIN_SCORE_DELTA``, spelled here so this
#: module does not import the grader. tests/test_wallet_history_on_change.py pins the two
#: equal: the write rule and the prune rule must agree on what "unchanged" means.
HISTORY_MIN_SCORE_DELTA = 1.0

HISTORY = "wallet_score_history"

#: Where the history walk keeps its place between runs. A full pass over ~9M rows does not
#: fit one run's budget, and losing the cursor only means re-walking (the rule is
#: idempotent), so it is saved after every scanned batch.
CURSOR_KEY = "retention:wallet_score_history:cursor"

#: Consecutive refused BEGINs that end a run. INVENTED: one is a busy moment, three in a
#: row is a writer that holds the lock for tens of seconds, and we should not be queuing.
LOCK_FAILS_TO_STOP = 3

#: kv: the swaps walk's place between runs, and the newest cutoff any swaps delete used.
#: ``kaiba.intelligence.grade`` and ``kaiba.ingest.backfill`` spell the horizon key too;
#: tests/test_retention_swaps.py pins them equal.
SWAPS_CURSOR_KEY = "retention:swaps:cursor"
SWAPS_HORIZON_KEY = "retention:swaps:horizon_ms"
EVENTS_OTHER_CURSOR_KEY = "retention:events_other:cursor"

#: signal_audit reads 12 d + 30 min of swaps; one day of margin.
SWAPS_FLOOR_DAYS = 13
#: The WAL brake an emergency pass uses (see RetentionConfig.wal_pause_bytes).
EMERGENCY_WAL_PAUSE_BYTES = 3_500_000_000
EVENTS_OTHER_FLOOR_DAYS = 7

#: MEASURED 2026-10-13 on the box over the newest 60k events: wallet.graded 55,577/day,
#: system 27,682/day, triage.verdict 3,583, token.scanned 3,182, scan.tier1 3,173,
#: provider.error 1,416, triage.backpressure 478, provider.budget 273, alpha.meta 307.
#: Their readers read the newest rows or a window of at most a day (watchdog's bus cursor,
#: lookalike's newest 400 heartbeats, the CLI's 24 h tier-1 count, loss_review's CLI walk).
#: token.created and alpha.signal are NOT here: their rows carry dedupe keys, and the
#: dedupe_key IS NULL condition would keep them anyway.
EVENTS_OTHER_KINDS: tuple[str, ...] = (
    "wallet.graded", "system", "triage.verdict", "token.scanned", "scan.tier1", "scan.failed",
    "provider.error", "provider.budget", "triage.backpressure", "alpha.meta",
)


class RetentionConfig(BaseModel):
    """The ``retention:`` block of config/schedule.yaml. Every default is the safe one."""

    #: The operator's switch. False: :func:`run` deletes nothing and reports a dry run.
    enabled: bool = False
    provider_calls_days: int = Field(default=14, ge=1)
    triage_decisions_days: int = Field(default=14, ge=1)
    wallet_score_history_days: int = Field(default=14, ge=1)
    #: Rows per delete transaction at the start of a run; halved on a slow transaction,
    #: grown back toward this on fast ones. Never above MAX_BATCH_ROWS.
    batch_rows: int = Field(default=2_000, ge=1, le=MAX_BATCH_ROWS)
    #: A delete transaction slower than this halves the batch. Protection waits up to
    #: 10 s for the lock; 1 s keeps us an order of magnitude inside that.
    max_tx_ms: int = Field(default=1_000, gt=0)
    sleep_s: float = Field(default=0.5, ge=0)
    #: Wall-clock budget for one run. The scheduler job also stops 30 s before its timeout.
    budget_s: float = Field(default=600.0, gt=0)
    #: History rows read per walk step (index order, so one step is a few wallets).
    history_scan_rows: int = Field(default=5_000, ge=10, le=50_000)
    #: After a complete pass over the history, wait this long before starting the next.
    #: New rows only age into the window at the rate they were written.
    history_pass_interval_s: int = Field(default=86_400, ge=0)
    #: Random rows sampled by the dry run to estimate the history's deletable fraction.
    sample_rows: int = Field(default=200, ge=0, le=5_000)
    #: ``wallet.trade`` events. A switch of its own UNDER ``enabled`` (both must be true),
    #: and feed_tags.retention_gate must also allow it on every run.
    wallet_trade_enabled: bool = False
    wallet_trade_days: int = Field(default=7, ge=1)
    #: The latest feed_tags parity result must be younger than this. INVENTED: the parity
    #: job runs every 6 h, so two days is several missed runs, not one.
    wallet_trade_parity_max_age_s: int = Field(default=172_800, ge=60)
    #: ``swaps``. A switch of its own under ``enabled``. The floor is the widest bounded
    #: scheduled reader: signal_audit reads 12 d + 30 min back.
    swaps_enabled: bool = False
    swaps_days: int = Field(default=14, ge=SWAPS_FLOOR_DAYS)
    #: Wait this long after a complete pass over the swaps id range before starting the next.
    swaps_pass_interval_s: int = Field(default=21_600, ge=0)
    #: Telemetry ``events`` kinds (rows WITHOUT a dedupe_key only). Own switch under ``enabled``.
    events_other_enabled: bool = False
    events_other_days: int = Field(default=30, ge=EVENTS_OTHER_FLOOR_DAYS)
    events_other_kinds: list[str] = Field(default_factory=lambda: list(EVENTS_OTHER_KINDS))
    #: WAL brake (2026-10-13). Before every delete transaction: WAL over this -> wait
    #: ``wal_wait_s`` for checkpoints to catch up; still over it -> end the run with
    #: ``stopped: wal``. MEASURED on the box that day: deletes log ~1.25 KB of WAL per row
    #: (136,252 rows -> +170 MB), so a full 600 s run adds at most ~0.85 GB; and a writer in
    #: short transactions never pins the log the way a long reader does (AGENTS.md's 1.5 GB
    #: line is for readers). kaiba-scan alone keeps the WAL at 1.5-4 GB between db_guard's
    #: 30-minute restarts, so a 1.0 or 1.5 GB brake stopped every run with 0 deleted.
    #: 2.5 GB keeps a run's worst case under db_guard's 3 GiB restart line and 4 GiB alert.
    #: emergency_config raises it: at the disk guard's ACT line a full disk is worse.
    wal_pause_bytes: int = Field(default=2_500_000_000, gt=0)
    wal_wait_s: float = Field(default=60.0, ge=0)


@dataclass(frozen=True)
class TimeTable:
    name: str
    time_col: str
    days_field: str


TIME_TABLES: tuple[TimeTable, ...] = (
    TimeTable("provider_calls", "ts_ms", "provider_calls_days"),
    TimeTable("triage_decisions", "ts_ms", "triage_decisions_days"),
)

#: The report keys for the events deletes.
WALLET_TRADE = "events:wallet.trade"
EVENTS_OTHER = "events:telemetry"


# --------------------------------------------------------------------------------------
# bounded reads
# --------------------------------------------------------------------------------------


def _exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _id_range(conn: sqlite3.Connection, table: str) -> tuple[int | None, int | None]:
    """Two rowid seeks. NOT ``SELECT min(id), max(id)``: with both aggregates in one
    query SQLite drops the min/max optimisation and SCANS the table -- MEASURED on the box
    2026-10-01, 462 s over wallet_score_history and provider_calls for one dry run."""
    row = conn.execute(
        f'SELECT (SELECT min(id) FROM "{table}"), (SELECT max(id) FROM "{table}")'
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def _probe(conn: sqlite3.Connection, table: str, time_col: str, rid: int) -> tuple[int, int] | None:
    """The first row at or after ``rid``: one rowid seek."""
    row = conn.execute(
        f'SELECT id, "{time_col}" FROM "{table}" WHERE id >= ? ORDER BY id LIMIT 1', (rid,)
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else None


def id_boundary(conn: sqlite3.Connection, table: str, time_col: str, cutoff_ms: int) -> int | None:
    """The last id whose time is before ``cutoff_ms``, by binary search on the rowid.

    ~2 log2(n) rowid seeks, never a scan. It assumes ids grow with time, which holds for
    these append-only tables; where it does not, the DELETE still carries the time
    condition, so a wrong boundary can only make a run delete LESS, never a newer row.
    ``None`` when no row is older than the cutoff.
    """
    lo, hi = _id_range(conn, table)
    if lo is None or hi is None:
        return None
    first = _probe(conn, table, time_col, lo)
    if first is None or first[1] >= cutoff_ms:
        return None
    last = _probe(conn, table, time_col, hi)
    if last is not None and last[1] < cutoff_ms:
        return hi
    lo_id, hi_id = first[0], hi
    while hi_id - lo_id > 1:
        mid = (lo_id + hi_id) // 2
        got = _probe(conn, table, time_col, mid)
        if got is None or got[0] >= hi_id:
            hi_id = mid  # no row in [mid, hi_id): the answer is below mid
        elif got[1] < cutoff_ms:
            lo_id = got[0]
        else:
            hi_id = got[0]
    return lo_id


# --------------------------------------------------------------------------------------
# the history rule
# --------------------------------------------------------------------------------------

# Row shape used throughout: (id, chain, address, scored_at_ms, grade, score, model_version)
_H_COLS = "id, chain, address, scored_at_ms, grade, score, model_version"


def _moved(kept: Sequence[Any], row: Sequence[Any], delta: float) -> bool:
    return (
        row[4] != kept[4]
        or row[6] != kept[6]
        or abs(float(row[5]) - float(kept[5])) >= delta
    )


def deletable_history_ids(
    rows: Sequence[Sequence[Any]], cutoff_ms: int, delta: float = HISTORY_MIN_SCORE_DELTA
) -> list[int]:
    """One wallet's rows, in ``(scored_at_ms, id)`` order -> the ids the rule may delete.

    A row is kept when it is the wallet's first, when it moved against the last KEPT row
    (not the previous row, so a slow drift is not erased one sub-point step at a time),
    when it is the wallet's latest row, or when it is not older than ``cutoff_ms``.
    """
    out: list[int] = []
    kept: Sequence[Any] | None = None
    last = len(rows) - 1
    for i, row in enumerate(rows):
        if int(row[3]) >= cutoff_ms:
            break  # ordered by time, so everything after this is inside the window too
        if kept is None or _moved(kept, row, delta):
            kept = row
            continue
        if i == last:
            break  # the wallet's latest row stays, changed or not
        out.append(int(row[0]))
    return out


def _wallet_rows(conn: sqlite3.Connection, chain: str, address: str) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            f"SELECT {_H_COLS} FROM {HISTORY} WHERE chain = ? AND address = ? "
            "ORDER BY scored_at_ms, id",
            (chain, address),
        ).fetchall()
    ]


def _rows_after(
    conn: sqlite3.Connection, cursor: tuple[str, str], limit: int
) -> list[tuple[Any, ...]]:
    """The next ``limit`` history rows in ``idx_wsh`` order after wallet ``cursor``."""
    return [
        tuple(r)
        for r in conn.execute(
            f"SELECT {_H_COLS} FROM {HISTORY} WHERE (chain, address) > (?, ?) "
            "ORDER BY chain, address, scored_at_ms, id LIMIT ?",
            (cursor[0], cursor[1], int(limit)),
        ).fetchall()
    ]


def _group(rows: Sequence[tuple[Any, ...]]) -> list[tuple[tuple[str, str], list[tuple[Any, ...]]]]:
    groups: list[tuple[tuple[str, str], list[tuple[Any, ...]]]] = []
    for row in rows:
        key = (str(row[1]), str(row[2]))
        if groups and groups[-1][0] == key:
            groups[-1][1].append(row)
        else:
            groups.append((key, [row]))
    return groups


# --------------------------------------------------------------------------------------
# pacing: batch size, sleeps, budget, lock back-off
# --------------------------------------------------------------------------------------


@dataclass
class _Pacer:
    cfg: RetentionConfig
    deadline_ms: int
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    #: Times each delete transaction; a test injects a fake to exercise the halving.
    timer: Callable[[], float] = time.monotonic
    batch: int = 0
    transactions: int = 0
    max_rows_per_tx: int = 0
    slow_tx: int = 0
    lock_refusals: int = 0
    stopped: str | None = None
    wal_waits: int = 0
    #: Returns the live WAL size in bytes; None disables the brake (in-memory databases).
    wal_bytes: Callable[[], int | None] | None = None
    _lock_streak: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.batch = max(1, min(int(self.cfg.batch_rows), MAX_BATCH_ROWS))

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def out_of_time(self) -> bool:
        if self.stopped is not None:
            return True
        if self.now_ms() >= self.deadline_ms:
            self.stopped = "budget"
            return True
        return False

    def wal_ok(self) -> bool:
        """False -- and ``stopped = "wal"`` -- when the WAL stays over the brake after a wait."""
        if self.wal_bytes is None:
            return True
        size = self.wal_bytes()
        if size is None or size <= self.cfg.wal_pause_bytes:
            return True
        self.wal_waits += 1
        self.sleep(self.cfg.wal_wait_s)
        size = self.wal_bytes()
        if size is None or size <= self.cfg.wal_pause_bytes:
            return True
        self.stopped = "wal"
        log.warning("retention: WAL %s bytes over the %s brake after %ss; stopping this run",
                    size, self.cfg.wal_pause_bytes, self.cfg.wal_wait_s)
        return False

    def delete(self, conn: sqlite3.Connection, body: Callable[[sqlite3.Connection], int]) -> int | None:
        """One short write transaction. ``None`` when the lock was refused (nothing deleted)
        or the WAL brake stopped the run (then ``stopped`` is set and every loop ends)."""
        if not self.wal_ok():
            return None
        started = self.timer()
        try:
            with tx(conn):
                n = int(body(conn))
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" not in text and "busy" not in text:
                raise
            self.lock_refusals += 1
            self._lock_streak += 1
            self.batch = max(1, self.batch // 2)
            if self._lock_streak >= LOCK_FAILS_TO_STOP:
                self.stopped = "locked"
            else:
                self.sleep(max(1.0, self.cfg.sleep_s * 4))
            return None
        took_ms = (self.timer() - started) * 1000.0
        self._lock_streak = 0
        self.transactions += 1
        self.max_rows_per_tx = max(self.max_rows_per_tx, n)
        if took_ms > self.cfg.max_tx_ms:
            self.slow_tx += 1
            self.batch = max(1, self.batch // 2)
        elif took_ms < self.cfg.max_tx_ms / 4:
            self.batch = min(int(self.cfg.batch_rows), MAX_BATCH_ROWS, max(self.batch + 1, int(self.batch * 1.5)))
        if self.cfg.sleep_s:
            self.sleep(self.cfg.sleep_s)
        return n

    def as_dict(self) -> dict[str, Any]:
        return {
            "transactions": self.transactions, "max_rows_per_tx": self.max_rows_per_tx,
            "slow_tx": self.slow_tx, "lock_refusals": self.lock_refusals,
            "final_batch": self.batch, "stopped": self.stopped, "wal_waits": self.wal_waits,
        }


# --------------------------------------------------------------------------------------
# deletes -- private: :func:`run` is the gate
# --------------------------------------------------------------------------------------


def _prune_time_table(conn: sqlite3.Connection, t: TimeTable, cutoff_ms: int, pacer: _Pacer) -> dict[str, Any]:
    out: dict[str, Any] = {"cutoff_ms": cutoff_ms, "deleted": 0}
    boundary = id_boundary(conn, t.name, t.time_col, cutoff_ms)
    out["boundary_id"] = boundary
    if boundary is None:
        out["done"] = True
        return out
    cursor, _ = _id_range(conn, t.name)
    where = f'id >= ? AND id <= ? AND "{t.time_col}" < ?'
    while cursor is not None and cursor <= boundary and not pacer.out_of_time():
        lo, hi = cursor, min(cursor + pacer.batch - 1, boundary)
        # Read the range first, outside the write lock, so the transaction below finds its
        # pages already in cache. An empty range costs no transaction at all.
        n_old = conn.execute(f'SELECT count(*) FROM "{t.name}" WHERE {where}', (lo, hi, cutoff_ms)).fetchone()[0]
        if n_old:
            n = pacer.delete(
                conn,
                lambda c, lo=lo, hi=hi: c.execute(
                    f'DELETE FROM "{t.name}" WHERE {where}', (lo, hi, cutoff_ms)
                ).rowcount,
            )
            if n is None:
                continue  # the lock was refused: same range again, at the smaller batch
            out["deleted"] += n
        cursor = hi + 1
    out["done"] = cursor is None or cursor > boundary
    return out


def _load_cursor(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (CURSOR_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_cursor(conn: sqlite3.Connection, state: dict[str, Any], now: int) -> None:
    """Best effort: a lost cursor costs a re-walk, never a wrong delete."""
    try:
        with tx(conn):
            conn.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
                (CURSOR_KEY, json.dumps(state, separators=(",", ":")), now),
            )
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" not in text and "busy" not in text:
            raise
        log.warning("retention: history cursor not saved (%s); the next run re-walks", exc)


def _delete_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    cur = conn.executemany(f"DELETE FROM {HISTORY} WHERE id = ?", [(int(i),) for i in ids])
    return int(cur.rowcount)


def _prune_history(conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, pacer: _Pacer) -> dict[str, Any]:
    out: dict[str, Any] = {"cutoff_ms": cutoff_ms, "deleted": 0, "rows_read": 0, "wallets": 0}
    state = _load_cursor(conn)
    now = pacer.now_ms()
    if state.get("chain") is None:
        done_ms = state.get("last_pass_done_ms")
        if done_ms is not None and now - int(done_ms) < cfg.history_pass_interval_s * 1000:
            out["skipped"] = "pass_interval"
            out["last_pass_done_ms"] = done_ms
            return out
        state = {"chain": "", "address": "", "pass_started_ms": now,
                 "last_pass_done_ms": state.get("last_pass_done_ms")}
    cursor = (str(state["chain"]), str(state["address"]))
    while not pacer.out_of_time():
        rows = _rows_after(conn, cursor, cfg.history_scan_rows)
        full = len(rows) >= cfg.history_scan_rows
        groups = _group(rows)
        if full and len(groups) == 1:
            key = groups[0][0]
            groups = [(key, _wallet_rows(conn, *key))]  # one wallet larger than a scan step
        elif full:
            groups = groups[:-1]  # the last wallet may continue past the limit
        ids = [i for _, wrows in groups for i in deletable_history_ids(wrows, cutoff_ms)]
        out["rows_read"] += sum(len(w) for _, w in groups)
        k = 0
        while k < len(ids) and not pacer.out_of_time():
            chunk = ids[k : k + pacer.batch]
            n = pacer.delete(conn, lambda c, chunk=chunk: _delete_ids(c, chunk))
            if n is None:
                continue
            out["deleted"] += n
            k += len(chunk)
        if k < len(ids):
            break  # out of time mid-step: keep the cursor before it; the step is recomputed
        out["wallets"] += len(groups)
        if not groups:
            full = False
        else:
            cursor = groups[-1][0]
        if not full:
            state = {"chain": None, "address": None, "pass_started_ms": state.get("pass_started_ms"),
                     "last_pass_done_ms": pacer.now_ms()}
            _save_cursor(conn, state, pacer.now_ms())
            out["pass_complete"] = True
            return out
        state = {**state, "chain": cursor[0], "address": cursor[1]}
        _save_cursor(conn, state, pacer.now_ms())
    out["pass_complete"] = False
    out["cursor"] = {"chain": cursor[0], "address": cursor[1][:12]}
    return out


#: Both read and delete are bounded id ranges; idx_events_kind is (kind, id).
_WT_WHERE = "kind = 'wallet.trade' AND id >= ? AND id <= ? AND ts_ms < ?"


def _wallet_trade_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (feed_tags.KV_RETENTION,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _set_wallet_trade_state(conn: sqlite3.Connection, through: int, deleted: int, now: int) -> None:
    """Inside the caller's transaction: the delete and the cursor that records it are one."""
    prev = _wallet_trade_state(conn)
    state = {"deleted_through_id": int(through),
             "deleted_total": int(prev.get("deleted_total") or 0) + int(deleted),
             "updated_ms": int(now)}
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (feed_tags.KV_RETENTION, json.dumps(state, separators=(",", ":")), int(now)),
    )


def _wallet_trade_start(conn: sqlite3.Connection) -> int | None:
    state = _wallet_trade_state(conn)
    if state.get("deleted_through_id"):
        return int(state["deleted_through_id"]) + 1
    row = conn.execute("SELECT min(id) FROM events WHERE kind = 'wallet.trade'").fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _prune_wallet_trade(
    conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, pacer: _Pacer, now: int
) -> dict[str, Any]:
    """Delete ``wallet.trade`` events older than the cutoff, in id order, behind the gate.

    The gate is re-read here, on every run, from the database: a parity check that failed
    since the last run stops the next delete. Each transaction deletes one id window (at
    most ``pacer.batch`` <= 5,000 ids) and records how far it got in the same transaction,
    so a later run resumes and never re-reads ranges it has already emptied.
    """
    out: dict[str, Any] = {"cutoff_ms": cutoff_ms, "deleted": 0}
    gate = feed_tags.retention_gate(conn, now_ms=now, parity_max_age_s=cfg.wallet_trade_parity_max_age_s)
    out["gate"] = gate
    if not gate.get("ok"):
        out["refused"] = gate.get("reason")
        return out
    boundary = id_boundary(conn, "events", "ts_ms", cutoff_ms)
    out["boundary_id"] = boundary
    cursor = _wallet_trade_start(conn)
    out["from_id"] = cursor
    if boundary is None or cursor is None or cursor > boundary:
        out["done"] = True
        return out
    stored_cursor = cursor
    while cursor <= boundary and not pacer.out_of_time():
        lo, hi = cursor, min(cursor + pacer.batch - 1, boundary)
        n_old = conn.execute(f"SELECT count(*) FROM events WHERE {_WT_WHERE}", (lo, hi, cutoff_ms)).fetchone()[0]
        if n_old:

            def body(c: sqlite3.Connection, lo: int = lo, hi: int = hi) -> int:
                n = int(c.execute(f"DELETE FROM events WHERE {_WT_WHERE}", (lo, hi, cutoff_ms)).rowcount)
                _set_wallet_trade_state(c, hi, n, pacer.now_ms())
                return n

            n = pacer.delete(conn, body)
            if n is None:
                continue  # the lock was refused: same range again, at the smaller batch
            out["deleted"] += n
            stored_cursor = hi + 1
        cursor = hi + 1
    if cursor > stored_cursor and _wallet_trade_state(conn).get("deleted_total"):
        # Empty windows at the end: move the stored cursor past them once, not per window.
        try:
            with tx(conn):
                _set_wallet_trade_state(conn, cursor - 1, 0, pacer.now_ms())
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
    out["through_id"] = cursor - 1
    out["done"] = cursor > boundary
    return out


def _kv_json(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _kv_put(conn: sqlite3.Connection, key: str, value: Any, now: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (key, json.dumps(value, separators=(",", ":")), int(now)),
    )


def _kv_put_best_effort(conn: sqlite3.Connection, key: str, value: Any, now: int) -> None:
    """A lost cursor costs a re-walk, never a wrong delete."""
    try:
        with tx(conn):
            _kv_put(conn, key, value, now)
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
            raise
        log.warning("retention: cursor %s not saved (%s)", key, exc)


def swaps_horizon_ms(conn: sqlite3.Connection) -> int | None:
    """The newest cutoff any swaps delete has used: no swap older than this is guaranteed
    to still be on the tape. ``None`` while swaps have never been pruned."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (SWAPS_HORIZON_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return int(json.loads(row[0]))
    except (TypeError, ValueError):
        return None


def _held_tokens(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """(chain, token) of every position we have ever had, live or paper. Their tape stays."""
    if not _exists(conn, "positions"):
        return set()
    return {(str(c), str(t)) for c, t in conn.execute("SELECT DISTINCT chain, token FROM positions")}


def _prune_swaps(conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, pacer: _Pacer) -> dict[str, Any]:
    """Delete swaps with ``ts_ms`` before the cutoff, except held tokens, by walking ids.

    ``id`` is insert order, not trade time (backfill inserts old trades under new ids), so
    there is no boundary to search for: each pass walks the id range as it stood when the
    pass began, one window of ``pacer.batch`` ids at a time. The window is read OUTSIDE the
    transaction; the delete is by id. The cursor is saved after every window.
    """
    out: dict[str, Any] = {"cutoff_ms": cutoff_ms, "deleted": 0, "kept_held": 0, "windows": 0}
    now = pacer.now_ms()
    state = _kv_json(conn, SWAPS_CURSOR_KEY)
    if state.get("next_id") is None:
        done_ms = state.get("last_pass_done_ms")
        if done_ms is not None and now - int(done_ms) < cfg.swaps_pass_interval_s * 1000:
            out["skipped"] = "pass_interval"
            return out
        lo, hi = _id_range(conn, "swaps")
        if lo is None or hi is None:
            out["done"] = True
            return out
        state = {"next_id": int(lo), "end_id": int(hi), "pass_started_ms": now,
                 "last_pass_done_ms": state.get("last_pass_done_ms")}
    held = _held_tokens(conn)
    cursor, end = int(state["next_id"]), int(state["end_id"])
    while cursor <= end and not pacer.out_of_time():
        hi_id = min(cursor + pacer.batch - 1, end)
        rows = conn.execute(
            "SELECT id, chain, token FROM swaps WHERE id >= ? AND id <= ? AND ts_ms < ?",
            (cursor, hi_id, cutoff_ms),
        ).fetchall()
        ids = [int(r[0]) for r in rows if (str(r[1]), str(r[2])) not in held]
        out["kept_held"] += len(rows) - len(ids)
        out["windows"] += 1
        if ids:

            def body(c: sqlite3.Connection, ids: list[int] = ids) -> int:
                n = int(c.executemany("DELETE FROM swaps WHERE id = ? AND ts_ms < ?",
                                      [(i, cutoff_ms) for i in ids]).rowcount)
                if n:
                    prev = swaps_horizon_ms(c)
                    if prev is None or cutoff_ms > prev:
                        _kv_put(c, SWAPS_HORIZON_KEY, int(cutoff_ms), pacer.now_ms())
                return n

            n = pacer.delete(conn, body)
            if n is None:
                continue  # the lock was refused: same window again, at the smaller batch
            out["deleted"] += n
        cursor = hi_id + 1
        state = {**state, "next_id": cursor}
        _kv_put_best_effort(conn, SWAPS_CURSOR_KEY, state, pacer.now_ms())
    if cursor > end:
        _kv_put_best_effort(conn, SWAPS_CURSOR_KEY, {"next_id": None, "end_id": None,
                            "last_pass_done_ms": pacer.now_ms()}, pacer.now_ms())
        out["pass_complete"] = True
    else:
        out["pass_complete"] = False
        out["next_id"] = cursor
    return out


def _events_other_where(kinds: Sequence[str]) -> str:
    marks = ",".join("?" for _ in kinds)
    return f"id >= ? AND id <= ? AND ts_ms < ? AND dedupe_key IS NULL AND kind IN ({marks})"


def _prune_events_other(conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, pacer: _Pacer) -> dict[str, Any]:
    """Telemetry events older than the cutoff, rows without a dedupe key only.

    events.id grows with time (ts_ms is the emit clock), so the boundary search holds; the
    cursor starts where the last run stopped, because the kept kinds stay at the bottom of
    the id range forever and re-walking them every run would be most of the work.
    """
    kinds = [k for k in cfg.events_other_kinds if k and k != "wallet.trade"]
    out: dict[str, Any] = {"cutoff_ms": cutoff_ms, "deleted": 0, "kinds": len(kinds)}
    if not kinds:
        out["done"] = True
        return out
    boundary = id_boundary(conn, "events", "ts_ms", cutoff_ms)
    state = _kv_json(conn, EVENTS_OTHER_CURSOR_KEY)
    cursor = state.get("next_id")
    if cursor is None:
        cursor = _id_range(conn, "events")[0]
    out.update(boundary_id=boundary, from_id=cursor)
    if boundary is None or cursor is None or int(cursor) > boundary:
        out["done"] = True
        return out
    cursor = int(cursor)
    where = _events_other_where(kinds)
    while cursor <= boundary and not pacer.out_of_time():
        lo, hi = cursor, min(cursor + pacer.batch - 1, boundary)
        args = (lo, hi, cutoff_ms, *kinds)
        if conn.execute(f"SELECT count(*) FROM events WHERE {where}", args).fetchone()[0]:

            def body(c: sqlite3.Connection, args: tuple[Any, ...] = args, hi: int = hi) -> int:
                n = int(c.execute(f"DELETE FROM events WHERE {where}", args).rowcount)
                _kv_put(c, EVENTS_OTHER_CURSOR_KEY, {"next_id": hi + 1}, pacer.now_ms())
                return n

            n = pacer.delete(conn, body)
            if n is None:
                continue
            out["deleted"] += n
        cursor = hi + 1
    _kv_put_best_effort(conn, EVENTS_OTHER_CURSOR_KEY, {"next_id": cursor}, pacer.now_ms())
    out["through_id"] = cursor - 1
    out["done"] = cursor > boundary
    return out


def emergency_config(cfg: RetentionConfig, *, budget_s: float | None = None) -> RetentionConfig:
    """The config disk_guard runs when the disk crosses its ACT line.

    Every switch on and every window halved, never below its floor -- the floors are the
    widest bounded reader of each table, so an emergency pass still breaks nothing that a
    normal one would not. The wallet.trade delete still needs feed_tags.retention_gate on
    every run; the emergency does not bypass a parity check.
    """
    half = lambda days, floor: max(int(floor), int(days) // 2)  # noqa: E731
    update: dict[str, Any] = {
        "enabled": True, "wallet_trade_enabled": True, "swaps_enabled": True, "events_other_enabled": True,
        "provider_calls_days": half(cfg.provider_calls_days, 7),
        "triage_decisions_days": half(cfg.triage_decisions_days, 7),
        "wallet_score_history_days": half(cfg.wallet_score_history_days, 7),
        "wallet_trade_days": half(cfg.wallet_trade_days, 3),
        "swaps_days": half(cfg.swaps_days, SWAPS_FLOOR_DAYS),
        "events_other_days": half(cfg.events_other_days, EVENTS_OTHER_FLOOR_DAYS),
        # An emergency must not sit out a pass interval it inherited from a normal run.
        "swaps_pass_interval_s": 0, "history_pass_interval_s": 0,
        "wal_pause_bytes": max(int(cfg.wal_pause_bytes), EMERGENCY_WAL_PAUSE_BYTES),
    }
    if budget_s is not None:
        update["budget_s"] = float(budget_s)
    return cfg.model_copy(update=update)


# --------------------------------------------------------------------------------------
# dry run: estimates from bounded queries only. Never writes.
# --------------------------------------------------------------------------------------


def _estimate_wallet_trade(
    conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, now: int, rng: random.Random
) -> dict[str, Any]:
    """Gate status plus a sampled row estimate: five id windows of 2,000, never a scan."""
    out: dict[str, Any] = {
        "retention_days": int(cfg.wallet_trade_days), "switch": bool(cfg.wallet_trade_enabled),
        "cutoff_ms": cutoff_ms,
        "gate": feed_tags.retention_gate(conn, now_ms=now, parity_max_age_s=cfg.wallet_trade_parity_max_age_s),
        "method": "share of wallet.trade rows in 5 random 2,000-id windows x the id span to the cutoff "
                  "(an exact count when the span is under 10,000 ids)",
    }
    boundary = id_boundary(conn, "events", "ts_ms", cutoff_ms)
    start = _wallet_trade_start(conn)
    out.update(boundary_id=boundary, from_id=start)
    if boundary is None or start is None or start > boundary:
        out["rows_older_est"] = 0
        return out
    width = boundary - start + 1
    if width <= 10_000:  # small enough to count outright
        out["rows_older_est"] = conn.execute(
            f"SELECT count(*) FROM events WHERE {_WT_WHERE}", (start, boundary, cutoff_ms)).fetchone()[0]
        return out
    hits = 0
    for _ in range(5):
        lo = rng.randint(start, boundary - 1_999)
        hits += conn.execute(f"SELECT count(*) FROM events WHERE {_WT_WHERE}", (lo, lo + 1_999, cutoff_ms)).fetchone()[0]
    out["rows_older_est"] = int(hits / 10_000 * width)
    return out


def _estimate_time_table(conn: sqlite3.Connection, t: TimeTable, cutoff_ms: int) -> dict[str, Any]:
    lo, _ = _id_range(conn, t.name)
    boundary = id_boundary(conn, t.name, t.time_col, cutoff_ms)
    return {
        "cutoff_ms": cutoff_ms,
        "boundary_id": boundary,
        "rows_older_est": (boundary - lo + 1) if (boundary is not None and lo is not None) else 0,
        "method": "id span from min(id) to the last id older than the cutoff (rowid binary search)",
    }


def _estimate_history(
    conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, rng: random.Random,
    out_of_time: Callable[[], bool],
) -> dict[str, Any]:
    lo, _ = _id_range(conn, HISTORY)
    boundary = id_boundary(conn, HISTORY, "scored_at_ms", cutoff_ms)
    out: dict[str, Any] = {
        "cutoff_ms": cutoff_ms, "boundary_id": boundary,
        "method": "random rows among the older ones; each row's wallet is replayed under the rule",
    }
    if boundary is None or lo is None:
        out.update(rows_older_est=0, sampled=0, deletable_est=0)
        return out
    older = boundary - lo + 1
    hits = n = 0
    for _ in range(int(cfg.sample_rows)):
        if out_of_time():
            break
        row = conn.execute(
            f"SELECT id, chain, address, scored_at_ms FROM {HISTORY} WHERE id >= ? ORDER BY id LIMIT 1",
            (rng.randint(lo, boundary),),
        ).fetchone()
        if row is None or int(row[3]) >= cutoff_ms:
            continue
        n += 1
        if int(row[0]) in set(deletable_history_ids(_wallet_rows(conn, row[1], row[2]), cutoff_ms)):
            hits += 1
    p = hits / n if n else 0.0
    half = 1.96 * math.sqrt(p * (1 - p) / n) if n else 0.0
    out.update(
        rows_older_est=older, sampled=n, deletable_fraction=round(p, 4),
        deletable_est=int(p * older),
        deletable_est_ci95=[int(max(0.0, p - half) * older), int(min(1.0, p + half) * older)],
    )
    return out


def estimate(
    conn: sqlite3.Connection, cfg: RetentionConfig, *, now_ms: int,
    rng: random.Random | None = None, out_of_time: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """What a run would delete, from rowid seeks and a bounded sample. Reads only."""
    tables: dict[str, Any] = {}
    for t in TIME_TABLES:
        days = int(getattr(cfg, t.days_field))
        if not _exists(conn, t.name):
            tables[t.name] = {"absent": True}
            continue
        tables[t.name] = {"retention_days": days, **_estimate_time_table(conn, t, now_ms - days * DAY_MS)}
    if _exists(conn, HISTORY):
        days = int(cfg.wallet_score_history_days)
        tables[HISTORY] = {
            "retention_days": days,
            **_estimate_history(conn, cfg, now_ms - days * DAY_MS, rng or random.Random(), out_of_time),
        }
    else:
        tables[HISTORY] = {"absent": True}
    tables[WALLET_TRADE] = _estimate_wallet_trade(
        conn, cfg, now_ms - int(cfg.wallet_trade_days) * DAY_MS, now_ms, rng or random.Random())
    if _exists(conn, "swaps"):
        tables["swaps"] = _estimate_swaps(conn, cfg, now_ms - int(cfg.swaps_days) * DAY_MS, rng or random.Random())
    tables[EVENTS_OTHER] = _estimate_events_other(conn, cfg, now_ms - int(cfg.events_other_days) * DAY_MS)
    return tables


def _estimate_swaps(conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int, rng: random.Random) -> dict[str, Any]:
    """Share of rows older than the cutoff in 5 random 2,000-id windows x the id span.
    Held tokens are not subtracted: an upper bound."""
    lo, hi = _id_range(conn, "swaps")
    out: dict[str, Any] = {"retention_days": int(cfg.swaps_days), "switch": bool(cfg.swaps_enabled),
                           "cutoff_ms": cutoff_ms, "horizon_ms": swaps_horizon_ms(conn),
                           "method": "share older than the cutoff in 5 random 2,000-id windows x the id span "
                                     "(held tokens not subtracted: an upper bound)"}
    if lo is None or hi is None:
        out["rows_older_est"] = 0
        return out
    if hi - lo < 10_000:
        out["rows_older_est"] = conn.execute(
            "SELECT count(*) FROM swaps WHERE id >= ? AND id <= ? AND ts_ms < ?", (lo, hi, cutoff_ms)).fetchone()[0]
        return out
    old = seen = 0
    for _ in range(5):
        a = rng.randint(lo, hi - 1_999)
        o, n = conn.execute("SELECT sum(ts_ms < ?), count(*) FROM swaps WHERE id >= ? AND id <= ?",
                            (cutoff_ms, a, a + 1_999)).fetchone()
        old, seen = old + int(o or 0), seen + int(n or 0)
    out["rows_older_est"] = int(old / seen * (hi - lo + 1)) if seen else 0
    return out


def _estimate_events_other(conn: sqlite3.Connection, cfg: RetentionConfig, cutoff_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {"retention_days": int(cfg.events_other_days), "switch": bool(cfg.events_other_enabled),
                           "cutoff_ms": cutoff_ms, "kinds": len(cfg.events_other_kinds)}
    boundary = id_boundary(conn, "events", "ts_ms", cutoff_ms)
    start = _kv_json(conn, EVENTS_OTHER_CURSOR_KEY).get("next_id") or _id_range(conn, "events")[0]
    out.update(boundary_id=boundary, from_id=start,
               note="the delete counts per id window; no estimate is taken, to keep this a few seeks")
    return out


# --------------------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------------------


def _wal_reader(conn: sqlite3.Connection) -> Callable[[], int | None] | None:
    """A function returning the size of ``conn``'s WAL file, or None for no file database."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    path = str(row[2]) if row and row[2] else ""
    if not path:
        return None
    wal = Path(path + "-wal")

    def size() -> int | None:
        try:
            return wal.stat().st_size
        except OSError:
            return 0  # no WAL file: nothing logged since the last truncate

    return size


def run(
    conn: sqlite3.Connection,
    cfg: RetentionConfig,
    *,
    dry_run: bool,
    now_ms: int | None = None,
    deadline_ms: int | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Prune, or report what a prune would do. The ONLY entry point that deletes.

    Deletes only when ``cfg.enabled`` is true AND ``dry_run`` is false. Every other
    combination returns the dry-run estimate and names why nothing was deleted.
    """
    if conn.in_transaction:
        # Our transactions must be our own and short; inside a caller's they would be a
        # savepoint in a transaction of unknown length.
        raise RuntimeError("retention.run needs a connection with no open transaction")
    started = time.monotonic()
    wall = int(clock() * 1000)
    now = now_ms if now_ms is not None else wall  # the instant the windows are measured from
    deadline = deadline_ms if deadline_ms is not None else wall + int(cfg.budget_s * 1000)
    pacer = _Pacer(cfg, deadline, clock, sleep, wal_bytes=_wal_reader(conn))

    if dry_run or not cfg.enabled:
        reason = "dry_run requested" if dry_run else "retention.enabled is false"
        report: dict[str, Any] = {
            "mode": "dry_run", "enabled": cfg.enabled, "deleted": 0, "reason": reason,
            "tables": estimate(conn, cfg, now_ms=now, rng=rng, out_of_time=pacer.out_of_time),
        }
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        return report

    # Every stage gets a FAIR SHARE of the time left (what one stage does not use flows to
    # the stages after it), and the CHEAP stages go first: once their backlog is gone they
    # finish in seconds and hand nearly the whole budget to the three heavy tables.
    # MEASURED 2026-10-13 on the box: in strict order the provider_calls backlog (1.12M rows
    # of 67 B) spent a whole 150 s pass; heavy-first, one 468 s pass deleted 457,807
    # provider_calls rows but only 9,677 swaps (~145 rows/s: four random index touches per
    # row on an IOPS-bound disk). A "wal" or "locked" stop still ends the whole run.
    def time_table(t: TimeTable) -> Callable[[], dict[str, Any]]:
        def go() -> dict[str, Any]:
            if not _exists(conn, t.name):
                return {"absent": True}
            days = int(getattr(cfg, t.days_field))
            return {"retention_days": days, **_prune_time_table(conn, t, now - days * DAY_MS, pacer)}
        return go

    def wallet_trade() -> dict[str, Any]:
        if not cfg.wallet_trade_enabled:
            return {"skipped": "wallet_trade_enabled is false"}
        days = int(cfg.wallet_trade_days)
        return {"retention_days": days, **_prune_wallet_trade(conn, cfg, now - days * DAY_MS, pacer, now)}

    def swaps() -> dict[str, Any]:
        if not cfg.swaps_enabled:
            return {"skipped": "swaps_enabled is false"}
        if not _exists(conn, "swaps"):
            return {"absent": True}
        days = int(cfg.swaps_days)
        return {"retention_days": days, **_prune_swaps(conn, cfg, now - days * DAY_MS, pacer)}

    def events_other() -> dict[str, Any]:
        if not cfg.events_other_enabled:
            return {"skipped": "events_other_enabled is false"}
        days = int(cfg.events_other_days)
        return {"retention_days": days, **_prune_events_other(conn, cfg, now - days * DAY_MS, pacer)}

    def history() -> dict[str, Any]:
        if not _exists(conn, HISTORY):
            return {"absent": True}
        days = int(cfg.wallet_score_history_days)
        return {"retention_days": days, **_prune_history(conn, cfg, now - days * DAY_MS, pacer)}

    stages: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        (EVENTS_OTHER, events_other), *[(t.name, time_table(t)) for t in TIME_TABLES],
        (WALLET_TRADE, wallet_trade), ("swaps", swaps), (HISTORY, history),
    ]
    tables: dict[str, Any] = {}
    final_deadline = pacer.deadline_ms
    short = False  # some stage ran out of its slice or never got one: work remains
    for i, (key, stage) in enumerate(stages):
        if pacer.stopped in ("wal", "locked"):
            tables[key] = {"skipped": pacer.stopped}
            continue
        t = pacer.now_ms()  # one clock read per stage
        if t >= final_deadline:
            tables[key] = {"skipped": "budget"}
            short = True
            continue
        pacer.deadline_ms = t + max(1, (final_deadline - t) // (len(stages) - i))
        pacer.stopped = None
        tables[key] = stage()
        if pacer.stopped == "budget":
            tables[key]["slice_spent"] = True
            short = True
    pacer.deadline_ms = final_deadline
    if pacer.stopped not in ("wal", "locked"):
        pacer.stopped = "budget" if short else None
    deleted = sum(int(v.get("deleted", 0)) for v in tables.values())
    report = {"mode": "delete", "deleted": deleted, "tables": tables, **pacer.as_dict()}
    report["elapsed_s"] = round(time.monotonic() - started, 2)
    log.info("retention: deleted %d row(s) %s", deleted, json.dumps(pacer.as_dict()))
    return report


# --------------------------------------------------------------------------------------
# CLI: python -m kaiba.ops.retention [--dry-run | --execute]
# --------------------------------------------------------------------------------------


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


#: One delete run at a time across processes: the kaiba-retention timer and disk_guard's
#: emergency pass take this lock non-blocking and skip when it is held. tmpfs, so it stays
#: usable when the disk does not.
LOCK_PATH = Path("/dev/shm/kaiba-retention.lock")


def run_exclusive(conn: sqlite3.Connection, cfg: RetentionConfig, **kw: Any) -> dict[str, Any]:
    """:func:`run` under :data:`LOCK_PATH`; ``{"mode": "skipped", ...}`` when another run holds it."""
    try:
        import fcntl
    except ImportError:  # Windows: tests and local runs; the box is Linux
        return run(conn, cfg, **kw)
    path = LOCK_PATH if LOCK_PATH.parent.is_dir() else Path("/tmp/kaiba-retention.lock")
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"mode": "skipped", "deleted": 0, "reason": "another retention run holds the lock"}
        try:
            return run(conn, cfg, **kw)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m kaiba.ops.retention",
        description="Row retention for provider_calls, triage_decisions, unchanged "
        "wallet_score_history rows and (behind the feed_tags gate) old wallet.trade events. "
        "Dry run unless --execute AND retention.enabled.",
    )
    ap.add_argument("--db", type=Path, default=None, help="database (default: the configured one)")
    ap.add_argument("--config", type=Path, default=None, help="schedule.yaml (default: the scheduler's)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="estimate only, read-only connection (default)")
    mode.add_argument("--execute", action="store_true", help="delete; refused unless retention.enabled")
    ap.add_argument("--sample", type=int, default=None, help="dry-run sample size for the history estimate")
    ap.add_argument("--days", type=int, default=None, help="override every retention window (dry run only)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")

    from kaiba.ops.scheduler import config_path, load_config  # lazy: scheduler imports this module

    cfg = load_config(args.config or config_path()).retention
    if args.sample is not None:
        cfg = cfg.model_copy(update={"sample_rows": max(0, int(args.sample))})
    if args.days is not None:
        if args.execute:
            ap.error("--days is a dry-run what-if; change retention.*_days in the config to delete")
        d = max(1, int(args.days))
        cfg = cfg.model_copy(update={"provider_calls_days": d, "triage_decisions_days": d,
                                     "wallet_score_history_days": d, "wallet_trade_days": d,
                                     "swaps_days": d, "events_other_days": d})
    if args.db is None:
        from kaiba.core.config import get_settings

        db_path = get_settings().db_path
    else:
        db_path = args.db
    if args.execute:
        from kaiba.core import db as core_db

        conn = core_db.connect(db_path)
        report = run_exclusive(conn, cfg, dry_run=False)
    else:
        conn = _ro_connect(db_path)
        report = run(conn, cfg, dry_run=True)
    conn.close()
    print(json.dumps(report, indent=2, default=str))
    if args.execute and report["mode"] not in ("delete", "skipped"):
        return 2  # asked to delete and refused: say so in the exit code too
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
