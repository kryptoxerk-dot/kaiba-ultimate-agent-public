"""Per-token price extent -- first priced print, peak, print count -- kept current from a cursor.

WHY THIS EXISTS (2026-10-02). Two scheduled jobs answered the same question -- "what was this
token's first observed price, its best price, and how many priced prints does it have?" --
by scanning ``swaps`` from scratch every run:

* ``deployer.refresh`` ran two ``GROUP BY token`` queries per chain, every 30 minutes. The
  plan on the box is ``SEARCH swaps USING INDEX idx_swaps_token (chain=?)``: the
  ``ts_ms > ?`` window is NOT used as a bound, so each query walks every swap the chain has
  ever had (10.9M rows in the table) in token order, with a table lookup per row to read
  ``price_usd``. Six whole-chain walks per run; 197 s mean and 5 of 28 runs past the 300 s
  timeout in 24 h.
* ``seeds.seed_tokens`` ran the same unbounded ``GROUP BY token`` per chain every 4 hours.

And both took the peak as ``MAX(price_usd)``. ``price_usd`` is a TEXT column, so that is
the LEXICOGRAPHIC maximum: ``'9.1e-05' > '0.0005'``. MEASURED on the box over the newest
20,000 swaps: 1,758 prices are in scientific notation, and for 35 of the 178 tokens with
eight or more prints the string maximum is not the numeric maximum. A wrong peak moves a
token across the 2x "runner" line (deployer sizing) and the 5x "seed" line (12 grader
points) in either direction.

WHAT THIS DOES. One table, one row per ``(chain, token)``, folded forward from a
``swaps.id`` cursor in ``kv``. ``swaps.id`` is AUTOINCREMENT and SQLite has one writer, so
an id becomes visible only after every lower id has committed or rolled back: a cursor on
it misses nothing. A steady-state run reads only the rows inserted since the last one --
MEASURED 0.56 s per 200,000 recent rows -- instead of the whole chain.

Both peaks are kept: ``peak_px``, the numeric maximum, and ``peak_text``, the string maximum
the old SQL produced. Readers choose with a rule (:data:`PEAK_NUMERIC` /
:data:`PEAK_LEGACY_TEXT`) and both shipped readers default to the LEGACY rule, so this
change moves no sizing label and no grader point by itself. Switching is one constant per
reader and is the lead's decision on the measurement in :data:`PEAK_NUMERIC`'s comment.

What is folded: the first PRICED print (earliest ``ts_ms``; the lowest id on a tie), both
peaks, the priced-print count and the last print time. A price that does not parse
as a finite positive number is not a print here. All four are order-independent (min,
max, sum), so a late backfill of an old swap lands correctly whenever it arrives.

Each batch's contribution and the cursor move in ONE transaction, after a re-read of the
cursor inside that transaction: two jobs advancing at once cannot both fold the same rows
(the loser sees the moved cursor and folds nothing). The write lock is held only for the
upserts of one batch (:data:`BATCH_ROWS` rows read, typically 1-8k token rows written).

BOOTSTRAP. An empty cursor folds the table from id 0: MEASURED 0.56-16.8 s per 200,000
rows (warm to cold), so the 10.9M rows on the box take about 15 minutes of IO once. Every
caller passes a deadline and the cursor persists per batch, so the bootstrap spreads over
as many runs as it needs, or the lead runs it once before restarting the ops service::

    nice -n 19 ionice -c3 .venv/bin/python -m kaiba.intelligence.price_extent --budget-s 1800

Readers must not use a half-folded table: :func:`AdvanceReport.caught_up` says whether
everything inserted before the call is in.
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any

from kaiba.core.db import tx

log = logging.getLogger(__name__)

TABLE = "token_price_extent"

#: ``kv`` key holding the highest ``swaps.id`` already folded into :data:`TABLE`.
KV_CURSOR = "price_extent:swaps_id"

#: The two peak rules a reader can ask for. NUMERIC is the price's real maximum.
#: LEGACY_TEXT reproduces ``MAX(price_usd)`` over the TEXT column -- the string maximum --
#: which is what ``deployer`` and ``seeds`` computed before 2026-10-02 and what the shipped
#: sizing ladder was measured against. It is kept so the IO fix can ship without moving a
#: single sizing label; switching a reader to NUMERIC is a separate, measured decision
#: (MEASURED 2026-10-02 on the box: 184 of 1,740 sol mid/spam deployers change label,
#: 177 of them OUT of a charged bucket).
PEAK_NUMERIC = "numeric"
PEAK_LEGACY_TEXT = "legacy_text"
PEAK_RULES = (PEAK_NUMERIC, PEAK_LEGACY_TEXT)

#: Swap rows read per batch. Bounds the write transaction (one upsert per distinct token in
#: the batch) and the memory of the fold; 20,000 recent rows held ~300 tokens on the box.
BATCH_ROWS = 20_000

_DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "  chain       TEXT    NOT NULL,"
    "  token       TEXT    NOT NULL,"
    "  first_ts_ms INTEGER NOT NULL,"
    "  first_px    REAL    NOT NULL,"
    "  peak_px     REAL    NOT NULL,"
    "  peak_text   TEXT    NOT NULL,"
    "  prints      INTEGER NOT NULL,"
    "  last_ts_ms  INTEGER NOT NULL,"
    "  PRIMARY KEY (chain, token)) WITHOUT ROWID",
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_first ON {TABLE}(chain, first_ts_ms)",
)

_UPSERT = (
    f"INSERT INTO {TABLE} (chain, token, first_ts_ms, first_px, peak_px, peak_text, prints, "
    "last_ts_ms) VALUES (?,?,?,?,?,?,?,?) "
    "ON CONFLICT(chain, token) DO UPDATE SET "
    # Every right-hand side reads the row as it was BEFORE this update, so first_px is
    # decided against the old first_ts_ms. Strict '<': on a tie the earlier fold (lower id)
    # keeps the price, which is the lowest-id-wins rule the batch fold uses too.
    f"  first_px = CASE WHEN excluded.first_ts_ms < {TABLE}.first_ts_ms "
    f"                  THEN excluded.first_px ELSE {TABLE}.first_px END, "
    f"  first_ts_ms = MIN({TABLE}.first_ts_ms, excluded.first_ts_ms), "
    f"  peak_px = MAX({TABLE}.peak_px, excluded.peak_px), "
    # TEXT against TEXT under BINARY collation: exactly the comparison MAX(price_usd) made.
    f"  peak_text = MAX({TABLE}.peak_text, excluded.peak_text), "
    f"  prints = {TABLE}.prints + excluded.prints, "
    f"  last_ts_ms = MAX({TABLE}.last_ts_ms, excluded.last_ts_ms)"
)


class ExtentNotReady(RuntimeError):
    """The extent has not folded the whole tape yet, so no outcome read from it can be trusted.

    Raised by readers instead of answering from a half-built table: the bootstrap folds in
    id order, so the NEWEST swaps land last and a partial table reads every recent token as
    unpriced. Carries the fold's progress for the caller's record.
    """

    def __init__(self, progress: dict[str, Any]) -> None:
        self.progress = progress
        super().__init__(
            f"price extent not caught up: cursor {progress.get('cursor_to')} of "
            f"{progress.get('target_id')}"
        )


@dataclass(frozen=True)
class Extent:
    chain: str
    token: str
    first_ts_ms: int
    first_px: float
    peak_px: float
    prints: int
    last_ts_ms: int
    #: The string maximum of the raw prices: see :data:`PEAK_LEGACY_TEXT`.
    peak_text: str = ""

    @property
    def multiple(self) -> float | None:
        """Peak over first observed price, or ``None`` when there is no usable first price."""
        return self.multiple_for(PEAK_NUMERIC)

    def peak_for(self, rule: str) -> float | None:
        if rule == PEAK_NUMERIC:
            return self.peak_px
        if rule == PEAK_LEGACY_TEXT:
            return price_of(self.peak_text)
        raise ValueError(f"unknown peak rule {rule!r}")

    def multiple_for(self, rule: str) -> float | None:
        peak = self.peak_for(rule)
        if self.first_px <= 0 or peak is None:
            return None
        return peak / self.first_px


@dataclass
class AdvanceReport:
    cursor_from: int = 0
    cursor_to: int = 0
    #: ``max(swaps.id)`` when the call began: the target "caught up" is measured against.
    target_id: int = 0
    rows_read: int = 0
    priced_rows: int = 0
    token_rows_written: int = 0
    batches: int = 0
    #: Batches another caller folded first; this call wrote nothing for them.
    lost_races: int = 0
    elapsed_s: float = 0.0

    @property
    def caught_up(self) -> bool:
        """Everything inserted before the call began is folded in."""
        return self.cursor_to >= self.target_id

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["caught_up"] = self.caught_up
        out["elapsed_s"] = round(self.elapsed_s, 2)
        return out


def ensure_table(conn: sqlite3.Connection) -> None:
    for stmt in _DDL:
        conn.execute(stmt)


def read_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_CURSOR,)).fetchone()
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def _write_cursor(conn: sqlite3.Connection, value: int, now_ms: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (KV_CURSOR, str(int(value)), int(now_ms)),
    )


def price_of(raw: Any) -> float | None:
    """A print's USD price as a float, or ``None`` when it is not a finite positive number."""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def fold(rows: Iterable[tuple[Any, ...]]) -> dict[tuple[str, str], list[Any]]:
    """``(chain, token) -> [first_ts, first_px, peak_px, prints, last_ts, peak_text]``.

    Over priced rows only. ``peak_text`` is the string maximum of the raw values, compared
    as Python compares ``str`` -- by code point, the same as SQLite's BINARY collation.

    ``rows`` are ``(id, chain, token, ts_ms, price_usd)`` in ascending id order, so on a
    ``ts_ms`` tie the first row met (lowest id) keeps the first price.
    """
    acc: dict[tuple[str, str], list[Any]] = {}
    for _id, chain, token, ts_ms, raw in rows:
        px = price_of(raw)
        if px is None or ts_ms is None:
            continue
        ts = int(ts_ms)
        key = (str(chain), str(token))
        cur = acc.get(key)
        text = str(raw)
        if cur is None:
            acc[key] = [ts, px, px, 1, ts, text]
            continue
        if ts < cur[0]:
            cur[0], cur[1] = ts, px
        if px > cur[2]:
            cur[2] = px
        cur[3] += 1
        if ts > cur[4]:
            cur[4] = ts
        if text > cur[5]:
            cur[5] = text
    return acc


def advance(
    conn: sqlite3.Connection,
    *,
    deadline: float | None = None,
    batch_rows: int = BATCH_ROWS,
    clock: Callable[[], float] = time.time,
) -> AdvanceReport:
    """Fold every swap inserted since the cursor, in batches, until caught up or ``deadline``.

    ``deadline`` is a ``clock()`` value (epoch seconds by default); it is checked before
    each batch, so a call overruns it by at most one batch. Never raises on a bad price.
    """
    ensure_table(conn)
    started = time.monotonic()
    rep = AdvanceReport()
    rep.cursor_from = rep.cursor_to = read_cursor(conn)
    rep.target_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM swaps").fetchone()[0])
    size = max(1, int(batch_rows))
    while rep.cursor_to < rep.target_id:
        if deadline is not None and clock() >= deadline:
            break
        start = rep.cursor_to
        rows = conn.execute(
            "SELECT id, chain, token, ts_ms, price_usd FROM swaps "
            "WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
            (start, rep.target_id, size),
        ).fetchall()
        if not rows:
            # Nothing between the cursor and the target (deleted rows): the gap is done.
            end = rep.target_id
            acc: dict[tuple[str, str], list[Any]] = {}
        else:
            end = int(rows[-1][0])
            acc = fold(rows)
        with tx(conn):
            if read_cursor(conn) != start:
                # Another caller folded this range while we read it. Take its cursor.
                rep.lost_races += 1
                rep.cursor_to = read_cursor(conn)
                continue
            if acc:
                conn.executemany(
                    _UPSERT,
                    [(ch, tok, v[0], v[1], v[2], v[5], v[3], v[4])
                     for (ch, tok), v in acc.items()],
                )
            _write_cursor(conn, end, int(clock() * 1000))
        rep.batches += 1
        rep.rows_read += len(rows)
        rep.priced_rows += sum(v[3] for v in acc.values())
        rep.token_rows_written += len(acc)
        rep.cursor_to = end
    rep.elapsed_s = time.monotonic() - started
    return rep


def _row(r: Any) -> Extent:
    return Extent(str(r[0]), str(r[1]), int(r[2]), float(r[3]), float(r[4]), int(r[5]),
                  int(r[6]), str(r[7]))


_COLS = "chain, token, first_ts_ms, first_px, peak_px, prints, last_ts_ms, peak_text"


def get(conn: sqlite3.Connection, chain: str, token: str) -> Extent | None:
    """One token's extent. One primary-key seek; ``None`` if absent or the table is missing."""
    try:
        r = conn.execute(
            f"SELECT {_COLS} FROM {TABLE} WHERE chain = ? AND token = ?", (chain, token)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return _row(r) if r is not None else None


def first_seen_since(
    conn: sqlite3.Connection, chain: str, since_ms: int, *, min_prints: int
) -> list[Extent]:
    """Tokens whose FIRST priced print is after ``since_ms``, with at least ``min_prints``.

    A range on ``idx_token_price_extent_first (chain, first_ts_ms)``.
    """
    return [
        _row(r)
        for r in conn.execute(
            f"SELECT {_COLS} FROM {TABLE} WHERE chain = ? AND first_ts_ms > ? AND prints >= ?",
            (chain, int(since_ms), int(min_prints)),
        )
    ]


def ran(
    conn: sqlite3.Connection, chain: str, *, min_prints: int, multiple: float,
    rule: str = PEAK_NUMERIC,
) -> set[str]:
    """Tokens with ``min_prints`` priced prints whose peak reached ``multiple`` x first price.

    ``rule`` picks the peak: :data:`PEAK_NUMERIC` or :data:`PEAK_LEGACY_TEXT`.
    """
    if rule not in PEAK_RULES:
        raise ValueError(f"unknown peak rule {rule!r}")
    peak = "peak_px" if rule == PEAK_NUMERIC else "CAST(peak_text AS REAL)"
    return {
        str(r[0])
        for r in conn.execute(
            f"SELECT token FROM {TABLE} WHERE chain = ? AND prints >= ? AND first_px > 0 "
            f"AND {peak} >= ? * first_px",
            (chain, int(min_prints), float(multiple)),
        )
    }


def main(argv: list[str] | None = None) -> int:
    """Fold the backlog by hand -- the bootstrap, before the ops service is restarted."""
    from kaiba.core.db import connect

    ap = argparse.ArgumentParser(description="fold swaps into token_price_extent")
    ap.add_argument("--budget-s", type=float, default=1800.0,
                    help="stop after this many seconds; the cursor persists, rerun to continue")
    ap.add_argument("--batch-rows", type=int, default=BATCH_ROWS)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        rep = advance(conn, deadline=time.time() + args.budget_s, batch_rows=args.batch_rows)
    finally:
        conn.close()
    print(rep.as_dict())
    return 0 if rep.caught_up else 2


__all__ = [
    "BATCH_ROWS",
    "PEAK_LEGACY_TEXT",
    "PEAK_NUMERIC",
    "PEAK_RULES",
    "KV_CURSOR",
    "TABLE",
    "AdvanceReport",
    "Extent",
    "ExtentNotReady",
    "advance",
    "ensure_table",
    "first_seen_since",
    "fold",
    "get",
    "price_of",
    "ran",
    "read_cursor",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
