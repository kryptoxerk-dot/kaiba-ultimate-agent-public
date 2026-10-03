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
    """How many distinct seeds each wallet BOUGHT. Chunked so the IN list stays sane."""
    counts: dict[str, int] = {}
    if not seeds:
        return counts
    ordered = sorted(seeds)
    for i in range(0, len(ordered), 400):
        chunk = ordered[i:i + 400]
        qs = ",".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT wallet, COUNT(DISTINCT token) n FROM swaps WHERE chain = ? "
            "AND side = 'buy' AND wallet IS NOT NULL AND token IN (%s) GROUP BY wallet" % qs,
            (chain.value, *chunk),
        ):
            counts[str(row[0])] = counts.get(str(row[0]), 0) + int(row[1])
    return counts


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
    counts = seed_counts(c, chain, seeds)
    considered = sorted(
        str(r[0]) for r in c.execute(
            "SELECT DISTINCT wallet FROM swaps WHERE chain = ? AND wallet IS NOT NULL",
            (chain.value,),
        )
    )
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
        report["elapsed_s"] = time.perf_counter() - started
        return report

    stamp = now_ms()
    # A dedicated connection: the scan above may still be streaming on `c`, and a write
    # through the connection being read loses the lock on a live box every time.
    w = None if dry_run else connect()
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
