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

NOT ``swaps``: it is the tape the grader and discovery read, and it has no rollup.

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


@dataclass(frozen=True)
class TimeTable:
    name: str
    time_col: str
    days_field: str


TIME_TABLES: tuple[TimeTable, ...] = (
    TimeTable("provider_calls", "ts_ms", "provider_calls_days"),
    TimeTable("triage_decisions", "ts_ms", "triage_decisions_days"),
)

#: The report key for the events delete.
WALLET_TRADE = "events:wallet.trade"


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

    def delete(self, conn: sqlite3.Connection, body: Callable[[sqlite3.Connection], int]) -> int | None:
        """One short write transaction. ``None`` when the lock was refused (nothing deleted)."""
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
            "final_batch": self.batch, "stopped": self.stopped,
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
    return tables


# --------------------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------------------


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
    pacer = _Pacer(cfg, deadline, clock, sleep)

    if dry_run or not cfg.enabled:
        reason = "dry_run requested" if dry_run else "retention.enabled is false"
        report: dict[str, Any] = {
            "mode": "dry_run", "enabled": cfg.enabled, "deleted": 0, "reason": reason,
            "tables": estimate(conn, cfg, now_ms=now, rng=rng, out_of_time=pacer.out_of_time),
        }
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        return report

    tables: dict[str, Any] = {}
    for t in TIME_TABLES:
        if not _exists(conn, t.name):
            tables[t.name] = {"absent": True}
            continue
        if pacer.out_of_time():
            tables[t.name] = {"skipped": pacer.stopped}
            continue
        days = int(getattr(cfg, t.days_field))
        tables[t.name] = {"retention_days": days, **_prune_time_table(conn, t, now - days * DAY_MS, pacer)}
    if not _exists(conn, HISTORY):
        tables[HISTORY] = {"absent": True}
    elif pacer.out_of_time():
        tables[HISTORY] = {"skipped": pacer.stopped}
    else:
        days = int(cfg.wallet_score_history_days)
        tables[HISTORY] = {"retention_days": days, **_prune_history(conn, cfg, now - days * DAY_MS, pacer)}
    if not cfg.wallet_trade_enabled:
        tables[WALLET_TRADE] = {"skipped": "wallet_trade_enabled is false"}
    elif pacer.out_of_time():
        tables[WALLET_TRADE] = {"skipped": pacer.stopped}
    else:
        days = int(cfg.wallet_trade_days)
        tables[WALLET_TRADE] = {"retention_days": days,
                                **_prune_wallet_trade(conn, cfg, now - days * DAY_MS, pacer, now)}
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
                                     "wallet_score_history_days": d, "wallet_trade_days": d})
    if args.db is None:
        from kaiba.core.config import get_settings

        db_path = get_settings().db_path
    else:
        db_path = args.db
    if args.execute:
        from kaiba.core import db as core_db

        conn = core_db.connect(db_path)
        report = run(conn, cfg, dry_run=False)
    else:
        conn = _ro_connect(db_path)
        report = run(conn, cfg, dry_run=True)
    conn.close()
    print(json.dumps(report, indent=2, default=str))
    if args.execute and report["mode"] != "delete":
        return 2  # asked to delete and refused: say so in the exit code too
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
