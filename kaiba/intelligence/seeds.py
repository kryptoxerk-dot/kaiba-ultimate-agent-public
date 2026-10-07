"""Seed tokens, and how many of them each wallet bought.

`grade.seed_confluence` is worth 12 of the grader's 100 points and was ``None`` for every
wallet ever scored: it is read from ``wallets.meta_json`` and nothing wrote it. The
grader's own module docstring records its predecessor dying of the same thing --
"seeds came from trending lists, so ``seed_confluence`` was always zero and no wallet ever
reached A". That single gap is why grading 186,031 wallets produced ELEVEN A/B.

A SEED here is measured from our own tape, never a trending list: a token with at least
:data:`MIN_PRINTS` priced prints whose best observed price reached :data:`SEED_MULTIPLE`
times its FIRST observed price. That is a token that demonstrably ran. ``seed_confluence``
is how many distinct seeds a wallet BOUGHT, which is what ``grade._seed_confluence``
scores through ``_frac_linear(n, 1, 3)`` -- three seeds is full credit.

MEASURED 2026-09-24, first run: sol 883 seeds, robinhood 554, bsc 59; of the wallets
scoring 28-39 (within reach of 12 points of the B bar at 40) 80 of 105 on sol, 48 of 55 on
robinhood and 11 of 14 on bsc had bought at least one. Regrading took A/B from 11 to 48
before sol had even finished.

TWO THINGS THIS IS CAREFUL ABOUT.

* **A measured zero is not a missing value.** ``seed_confluence=None`` means "we never ran
  the seed pass"; ``0`` means "we ran it and this wallet touched no seed". The grader
  treats them differently -- a missing component is normalised out of ``evidence_weight``
  while a zero counts against the wallet -- so this writes a number for every wallet it
  considered, not only for the ones that scored.
* **Adding a component is not free.** Because the score is normalised over components that
  HAD data, a measured LOW value adds 12 to the denominator and little to the numerator.
  Seed credit lifts wallets with three or more seeds and can lower a wallet with one. That
  is correct -- it is evidence either way -- but it is why this cannot be assumed to raise
  every grade.

Writes on its own connection, in bounded batches, for the reason recorded in
``grade.TAPE_STORE_BATCH``: the read that feeds this is still streaming, and a live box
refuses a write that shares its connection.

2026-10-02 -- WHAT A RUN COSTS NOW. MEASURED on the box before this change, each 4-hourly
run rewrote ``wallets.meta_json`` for EVERY wallet on the tape (sol 762,993 + robinhood
151,767 rows a run) because ``seed_confluence_ms`` was restamped whether or not the count
moved; it ran 489-1047 s, one run timed out at 2,207 s and one lost a 200-wallet batch to
``database is locked``. And the seed set came from an unbounded ``GROUP BY token`` walk of
the chain's whole tape whose ``MAX(price_usd)`` compared TEXT, so the "5x" test ran on the
string maximum. Now:

* seeds come from :mod:`kaiba.intelligence.price_extent` (cursor-folded), under
  :data:`SEED_PEAK_RULE` -- by default the SAME string-maximum peak as before, because
  correcting it moves 12 grader points for many wallets and is the lead's decision.
  MEASURED 2026-10-02 on the box over all 14,058 sol tokens traded in the newest 2M swaps:
  the string rule finds 335 seeds, the numeric rule 532 (+197, none lost). The string rule
  under-counts seeds by 37%;
* the stored value is READ first, and only a wallet whose count differs -- or that has no
  measured value yet, which is still written as a measured 0 -- is written;
  ``seed_confluence_ms`` therefore records when the count last CHANGED (nothing reads it);
* a pass that reaches its deadline stops between chunks and the next run resumes after the
  last wallet it finished (``kv`` :data:`KV_RESUME`), so a slow day cannot pin the tail of
  the address space at a stale count forever.
"""

from __future__ import annotations

import bisect
import logging
import sqlite3
import time
from typing import Any

from kaiba.core.db import connect, get_conn, jdump, jload
from kaiba.core.schemas import Chain, now_ms
from kaiba.intelligence import price_extent

log = logging.getLogger(__name__)

#: A token must have run this many times its first observed price to count as a seed.
#: 5x is the "actual winner" line the copyability rubric already uses elsewhere.
SEED_MULTIPLE = 5.0

#: And it needs this many priced prints first, so one bad tick cannot mint a seed.
MIN_PRINTS = 8

#: Wallets per write transaction. See ``grade.TAPE_STORE_BATCH`` for the measurement.
SEED_WRITE_BATCH = 200

#: Retries per batch when the live writers hold the lock.
SEED_WRITE_RETRIES = 3

#: Which peak decides "ran 5x". LEGACY_TEXT reproduces the old ``MAX(price_usd)`` string
#: maximum; ``price_extent.PEAK_NUMERIC`` is the correct one. See the module docstring.
SEED_PEAK_RULE = price_extent.PEAK_LEGACY_TEXT

#: Wallets whose stored value is read per query (an ``IN`` list on the primary key).
SEED_READ_CHUNK = 500

#: ``kv`` key (per chain) for the last wallet a deadline-cut pass finished.
KV_RESUME = "seeds:resume:{chain}"
#: ``kv`` key: the highest ``swaps.id`` already folded into ``seed_wallets``.
KV_WALLET_CURSOR = "seeds:wallet_cursor"
#: Swap rows per read when folding new wallets into ``seed_wallets`` (a PK range: each read is
#: a short, bounded transaction, never a scan of the whole tape).
WALLET_SCAN_CHUNK = 100_000

#: ``kv`` key (per chain): the highest ``swaps.id`` folded into ``seed_buyers``.
KV_BUYER_CURSOR = "seeds:buyer_cursor:{chain}"
#: Swap rows per page when backfilling one seed's history on ``idx_swaps_token``. The index
#: carries no ``side``/``wallet``, so each row is a table read: MEASURED 2026-10-06, 220 bsc
#: seeds took 64 s; a page this size stays a few seconds even on a cold cache.
BACKFILL_PAGE = 5_000


def seed_tokens(
    conn: sqlite3.Connection, chain: Chain, *, deadline: float | None = None,
    clock: Any = time.time,
) -> set[str]:
    """Tokens on ``chain``'s tape that demonstrably ran.

    Folds the swaps inserted since the last call into the price extent first (bounded by
    ``deadline``, ``clock()`` seconds) and raises :class:`price_extent.ExtentNotReady`
    rather than answer from a half-folded table.
    """
    c = conn or get_conn()
    progress = price_extent.advance(c, deadline=deadline, clock=clock)
    if not progress.caught_up:
        raise price_extent.ExtentNotReady(progress.as_dict())
    return price_extent.ran(c, chain.value, min_prints=MIN_PRINTS, multiple=SEED_MULTIPLE,
                            rule=SEED_PEAK_RULE)


def _stored_value(meta_json: Any) -> int | None:
    """The measured ``seed_confluence`` in a wallet's meta, or ``None`` if never measured."""
    meta = jload(meta_json, {}) or {}
    if not isinstance(meta, dict):
        return None
    value = meta.get("seed_confluence")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _stored_values(conn: sqlite3.Connection, chain: Chain, chunk: list[str]) -> dict[str, int | None]:
    qs = ",".join("?" * len(chunk))
    return {
        str(r[0]): _stored_value(r[1])
        for r in conn.execute(
            f"SELECT address, meta_json FROM wallets WHERE chain = ? AND address IN ({qs})",
            (chain.value, *chunk),
        )
    }


def seed_counts(conn: sqlite3.Connection, chain: Chain, seeds: set[str]) -> dict[str, int]:
    """How many distinct seeds each wallet BOUGHT, read from ``seed_buyers``.

    Never from ``swaps``: MEASURED 2026-10-06, ``GROUP BY wallet ... token IN (400 seeds)``
    on ``swaps`` was planned onto ``idx_swaps_wallet`` and walked the whole chain, > 60 s
    per chunk on every chain, so wallet_seeds timed out every run. Only meaningful once
    :func:`refresh_seed_buyers` reports ``complete``.
    """
    counts: dict[str, int] = {}
    if not seeds:
        return counts
    ordered = sorted(seeds)
    for i in range(0, len(ordered), 400):
        chunk = ordered[i:i + 400]
        qs = ",".join("?" * len(chunk))
        for (wallet,) in conn.execute(
            f"SELECT wallet FROM seed_buyers WHERE chain = ? AND token IN ({qs})",
            (chain.value, *chunk),
        ):
            counts[str(wallet)] = counts.get(str(wallet), 0) + 1
    return counts


def _kv_int(c: sqlite3.Connection, key: str) -> int | None:
    row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else None
    except (TypeError, ValueError):
        return None


def _put_kv(w: sqlite3.Connection, key: str, value: Any) -> None:
    w.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) "
        "DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
        (key, str(value), now_ms()),
    )


def _commit_buyers(w: sqlite3.Connection, rows: list[tuple[str, str, str]], extra: Any) -> int:
    """Insert ``rows`` and run ``extra(w)`` (cursor bookkeeping) in ONE transaction."""
    w.execute("BEGIN")
    try:
        before = w.total_changes
        if rows:
            w.executemany(
                "INSERT OR IGNORE INTO seed_buyers (chain, token, wallet) VALUES (?, ?, ?)",
                sorted(set(rows)))
        added = w.total_changes - before
        extra(w)
        w.execute("COMMIT")
    except Exception:
        w.execute("ROLLBACK")
        raise
    return added


def refresh_seed_buyers(
    c: sqlite3.Connection, w: sqlite3.Connection | None, chain: Chain, seeds: set[str], *,
    deadline: float | None = None, clock: Any = time.time,
) -> dict[str, Any]:
    """Bring ``seed_buyers`` up to date for ``seeds``; ``complete`` when every seed is.

    Two halves, both resumable and each a series of short reads:

    1. **New rows** since :data:`KV_BUYER_CURSOR` are read by primary-key range and their
       buys of a seed are kept. A chain with no cursor starts at today's ``MAX(id)``: every
       older buy of a seed reaches the table through step 2.
    2. **Each seed not yet backfilled** has its whole history paged off ``idx_swaps_token``
       (:data:`BACKFILL_PAGE` rows a read), its position saved in ``seed_buyer_tokens``
       after every page so a deadline-cut token resumes where it stopped.

    Inserts are ``OR IGNORE`` on (chain, token, wallet), so the two halves overlapping is
    harmless. At least one unit of work runs per call, then the deadline is checked.
    """
    rep: dict[str, Any] = {"seeds": len(seeds), "added": 0, "backfilled": 0,
                           "pending": 0, "complete": False}
    if w is None:
        done = {str(r[0]) for r in c.execute(
            "SELECT token FROM seed_buyer_tokens WHERE chain = ? AND done_ms IS NOT NULL",
            (chain.value,))}
        rep["pending"] = len(seeds - done)
        rep["complete"] = not rep["pending"]
        return rep
    worked = False

    def out_of_time() -> bool:
        return worked and deadline is not None and clock() >= deadline

    key = KV_BUYER_CURSOR.format(chain=chain.value)
    max_id = int(c.execute("SELECT COALESCE(MAX(id), 0) FROM swaps").fetchone()[0] or 0)
    cursor = _kv_int(c, key)
    if cursor is None:
        _commit_buyers(w, [], lambda x: _put_kv(x, key, max_id))
        cursor = max_id
    while cursor < max_id and not out_of_time():
        upper = min(cursor + WALLET_SCAN_CHUNK, max_id)
        rows = [
            (chain.value, str(tok), str(wl))
            for tok, wl in c.execute(
                "SELECT token, wallet FROM swaps WHERE id > ? AND id <= ? AND chain = ? "
                "AND side = 'buy' AND wallet IS NOT NULL",
                (cursor, upper, chain.value),
            )
            if wl and tok in seeds
        ]
        rep["added"] += _commit_buyers(w, rows, lambda x, u=upper: _put_kv(x, key, u))
        cursor = upper
        worked = True
    rep["cursor"], rep["max_id"] = cursor, max_id

    state = {
        str(r[0]): (int(r[1]), int(r[2]), r[3])
        for r in c.execute(
            "SELECT token, at_ts, at_id, done_ms FROM seed_buyer_tokens WHERE chain = ?",
            (chain.value,))
    }
    todo = sorted(t for t in seeds if state.get(t, (0, 0, None))[2] is None)
    for token in todo:
        at_ts, at_id = state.get(token, (-1, -1, None))[:2]
        finished = False
        while not out_of_time():
            page = c.execute(
                "SELECT ts_ms, id, side, wallet FROM swaps INDEXED BY idx_swaps_token "
                "WHERE chain = ? AND token = ? AND (ts_ms > ? OR (ts_ms = ? AND id > ?)) "
                "ORDER BY ts_ms, id LIMIT ?",
                (chain.value, token, at_ts, at_ts, at_id, BACKFILL_PAGE),
            ).fetchall()
            if page:
                at_ts, at_id = int(page[-1][0]), int(page[-1][1])
            finished = len(page) < BACKFILL_PAGE
            stamp = now_ms() if finished else None

            def mark(x: sqlite3.Connection, ts: int = at_ts, i: int = at_id,
                     d: int | None = stamp, t: str = token) -> None:
                x.execute(
                    "INSERT INTO seed_buyer_tokens (chain, token, at_ts, at_id, done_ms) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chain, token) DO UPDATE SET "
                    "at_ts = excluded.at_ts, at_id = excluded.at_id, done_ms = excluded.done_ms",
                    (chain.value, t, ts, i, d))

            rows = [(chain.value, token, str(r[3])) for r in page if r[2] == "buy" and r[3]]
            rep["added"] += _commit_buyers(w, rows, mark)
            worked = True
            if finished:
                rep["backfilled"] += 1
                break
        if not finished:
            break
    rep["pending"] = len(todo) - rep["backfilled"]
    # A fold that is behind only makes counts minutes stale; an unbackfilled seed makes them
    # WRONG (an undercount written as a measured value). Only the second blocks writes.
    rep["complete"] = rep["pending"] == 0
    rep["fold_caught_up"] = cursor >= max_id
    return rep


def _write_counts(
    w: sqlite3.Connection, chain: Chain, chunk: list[str], counts: dict[str, int],
    stamp: int, report: dict[str, Any],
) -> None:
    """Write ``seed_confluence`` for ``chunk`` in one transaction, with retries."""
    for attempt in range(1, SEED_WRITE_RETRIES + 1):
        try:
            w.execute("BEGIN")
            for addr in chunk:
                row = w.execute(
                    "SELECT meta_json FROM wallets WHERE chain = ? AND address = ?",
                    (chain.value, addr),
                ).fetchone()
                meta = (jload(row[0], {}) if row else {}) or {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["seed_confluence"] = int(counts.get(addr, 0))
                meta["seed_confluence_ms"] = stamp
                if row:
                    w.execute(
                        "UPDATE wallets SET meta_json = ? WHERE chain = ? AND address = ?",
                        (jdump(meta), chain.value, addr),
                    )
                else:
                    w.execute(
                        "INSERT INTO wallets (chain, address, meta_json, first_seen_ms, "
                        "last_seen_ms) VALUES (?,?,?,?,?)",
                        (chain.value, addr, jdump(meta), stamp, stamp),
                    )
            w.commit()
            report["written"] += len(chunk)
            return
        except sqlite3.Error as exc:
            w.rollback()
            if attempt >= SEED_WRITE_RETRIES:
                report["failed"] += len(chunk)
                log.warning("seed batch of %d failed: %s", len(chunk), exc)
            else:
                time.sleep(1.5)


def _set_resume(conn: sqlite3.Connection, chain: Chain, address: str | None) -> None:
    key = KV_RESUME.format(chain=chain.value)
    if address is None:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
            (key, address, now_ms()),
        )


def refresh_wallet_index(
    c: sqlite3.Connection, w: sqlite3.Connection | None, *, deadline: float | None = None,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Fold swap rows added since the last call into ``seed_wallets`` (chain, wallet).

    Replaces ``SELECT DISTINCT wallet FROM swaps WHERE chain = ?``. MEASURED 2026-10-06 on the
    box: that query walks every index entry for the chain (counting sol's alone took > 47 s)
    in ONE read the deadline cannot interrupt, so wallet_seeds timed out 10 runs in a row
    (last success 55 h earlier) and pinned the WAL while it ran. This reads ``swaps`` by
    primary-key range, :data:`WALLET_SCAN_CHUNK` rows at a time, stops at ``deadline`` and
    resumes from :data:`KV_WALLET_CURSOR`; once caught up, a call reads only the new rows.
    ``w`` is the write connection; ``None`` (a dry run) reads without folding.
    """
    row = c.execute("SELECT value FROM kv WHERE key = ?", (KV_WALLET_CURSOR,)).fetchone()
    try:
        cursor = int(row[0]) if row and row[0] is not None else 0
    except (TypeError, ValueError):
        cursor = 0
    max_id = int(c.execute("SELECT COALESCE(MAX(id), 0) FROM swaps").fetchone()[0] or 0)
    added = 0
    first = True
    while w is not None and cursor < max_id:
        # At least one chunk per call, so a tight budget still makes progress; then the
        # deadline is checked before each further chunk.
        if not first and deadline is not None and clock() >= deadline:
            break
        first = False
        upper = min(cursor + WALLET_SCAN_CHUNK, max_id)
        pairs = {
            (str(ch), str(wl))
            for ch, wl in c.execute(
                "SELECT chain, wallet FROM swaps WHERE id > ? AND id <= ? AND wallet IS NOT NULL",
                (cursor, upper),
            )
            if wl
        }
        w.execute("BEGIN")
        try:
            before = w.total_changes
            w.executemany("INSERT OR IGNORE INTO seed_wallets (chain, wallet) VALUES (?, ?)",
                          sorted(pairs))
            added += w.total_changes - before
            w.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) "
                "DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
                (KV_WALLET_CURSOR, str(upper), now_ms()),
            )
            w.execute("COMMIT")
        except Exception:
            w.execute("ROLLBACK")
            raise
        cursor = upper
    return {"cursor": cursor, "max_id": max_id, "complete": cursor >= max_id, "added": added}


def run(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    deadline: float | None = None,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Compute ``seed_confluence`` for every wallet on ``chain``'s tape; write what changed.

    ``deadline`` (``clock()`` seconds) bounds the extent fold and the comparison pass; a
    cut pass resumes after its last finished wallet on the next call. While the extent is
    still bootstrapping nothing is written and the report says ``not_ready``.
    """
    c = conn or get_conn()
    started = time.perf_counter()
    report: dict[str, Any] = {
        "chain": chain.value,
        "seeds": 0,
        "wallets_considered": 0,
        "wallets_with_a_seed": 0,
        "unchanged": 0,
        "written": 0,
        "failed": 0,
        "truncated": False,
        "dry_run": bool(dry_run),
    }
    try:
        seeds = seed_tokens(c, chain, deadline=deadline, clock=clock)
    except price_extent.ExtentNotReady as exc:
        report.update(not_ready=True, price_extent=exc.progress,
                      elapsed_s=time.perf_counter() - started)
        return report
    # A dedicated connection: a read may still be streaming on `c`, and a write through the
    # connection being read loses the lock on a live box every time.
    w = None if dry_run else connect()
    try:
        index = refresh_wallet_index(c, w, deadline=deadline, clock=clock)
        buyers = refresh_seed_buyers(c, w, chain, seeds, deadline=deadline, clock=clock)
    except Exception:
        if w is not None:
            w.close()
        raise
    report["wallet_index"] = index
    report["seed_buyers"] = buyers
    if not buyers["complete"]:
        # Some seed's buyers are not all read yet: any count now would be an undercount
        # written as a measured value. Write nothing; the next run resumes the backfill.
        if w is not None:
            w.close()
        report.update(seeds=len(seeds), backfilling=True,
                      elapsed_s=time.perf_counter() - started)
        return report
    counts = seed_counts(c, chain, seeds)
    considered = [
        str(r[0]) for r in c.execute(
            "SELECT wallet FROM seed_wallets WHERE chain = ? ORDER BY wallet", (chain.value,),
        )
    ]
    resumable = limit is None
    if not resumable:
        considered = considered[: max(0, int(limit))]
    report.update(
        seeds=len(seeds),
        wallets_considered=len(considered),
        wallets_with_a_seed=sum(1 for a in considered if counts.get(a)),
    )
    # Resume after the last wallet a cut pass finished, wrapping round to the start.
    order = considered
    if resumable:
        row = c.execute("SELECT value FROM kv WHERE key = ?",
                        (KV_RESUME.format(chain=chain.value),)).fetchone()
        if row is not None and row[0]:
            at = bisect.bisect_right(considered, str(row[0]))
            order = considered[at:] + considered[:at]
            report["resumed_after"] = str(row[0])
    if not order:
        if w is not None:
            w.close()
        report["elapsed_s"] = time.perf_counter() - started
        return report

    stamp = now_ms()
    last_done: str | None = None
    try:
        for i in range(0, len(order), SEED_READ_CHUNK):
            if deadline is not None and clock() >= deadline:
                report["truncated"] = True
                break
            chunk = order[i:i + SEED_READ_CHUNK]
            stored = _stored_values(c, chain, chunk)
            changed = [a for a in chunk if stored.get(a) != int(counts.get(a, 0))]
            report["unchanged"] += len(chunk) - len(changed)
            if w is None:
                report["would_write"] = report.get("would_write", 0) + len(changed)
            else:
                for j in range(0, len(changed), SEED_WRITE_BATCH):
                    _write_counts(w, chain, changed[j:j + SEED_WRITE_BATCH], counts, stamp, report)
            last_done = chunk[-1]
        if w is not None and resumable:
            _set_resume(w, chain, last_done if report["truncated"] else None)
    finally:
        if w is not None:
            w.close()
    report["elapsed_s"] = time.perf_counter() - started
    return report


__all__ = [
    "MIN_PRINTS",
    "SEED_MULTIPLE",
    "SEED_WRITE_BATCH",
    "SEED_WRITE_RETRIES",
    "SEED_READ_CHUNK",
    "SEED_PEAK_RULE",
    "KV_RESUME",
    "run",
    "seed_counts",
    "seed_tokens",
]
