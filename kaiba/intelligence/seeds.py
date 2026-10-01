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
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from kaiba.core.db import connect, get_conn, jdump, jload
from kaiba.core.schemas import Chain, now_ms

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


def seed_tokens(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    """Tokens on ``chain``'s tape that demonstrably ran. Never raises on a bad row."""
    c = conn or get_conn()
    out: set[str] = set()
    for row in c.execute(
        "SELECT token, COUNT(*) n, MAX(price_usd) hi FROM swaps "
        "WHERE chain = ? AND price_usd IS NOT NULL AND price_usd > 0 "
        "GROUP BY token HAVING n >= ?",
        (chain.value, MIN_PRINTS),
    ):
        first = c.execute(
            "SELECT price_usd FROM swaps WHERE chain = ? AND token = ? AND price_usd > 0 "
            "ORDER BY ts_ms LIMIT 1",
            (chain.value, row[0]),
        ).fetchone()
        if not first:
            continue
        try:
            lo, hi = float(first[0]), float(row[2])
        except (TypeError, ValueError):
            continue
        if lo > 0 and hi / lo >= SEED_MULTIPLE:
            out.add(str(row[0]))
    return out


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


def run(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compute and persist ``seed_confluence`` for every wallet on ``chain``'s tape."""
    c = conn or get_conn()
    started = time.perf_counter()
    seeds = seed_tokens(c, chain)
    counts = seed_counts(c, chain, seeds)
    considered = [
        str(r[0]) for r in c.execute(
            "SELECT DISTINCT wallet FROM swaps WHERE chain = ? AND wallet IS NOT NULL",
            (chain.value,),
        )
    ]
    if limit is not None:
        considered = considered[: max(0, int(limit))]
    report: dict[str, Any] = {
        "chain": chain.value,
        "seeds": len(seeds),
        "wallets_considered": len(considered),
        "wallets_with_a_seed": sum(1 for a in considered if counts.get(a)),
        "written": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
    }
    if dry_run or not considered:
        report["elapsed_s"] = time.perf_counter() - started
        return report

    stamp = now_ms()
    # A dedicated connection: the scan above may still be streaming on `c`, and a write
    # through the connection being read loses the lock on a live box every time.
    w = connect()
    try:
        for i in range(0, len(considered), SEED_WRITE_BATCH):
            chunk = considered[i:i + SEED_WRITE_BATCH]
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
                    break
                except sqlite3.Error as exc:
                    w.rollback()
                    if attempt >= SEED_WRITE_RETRIES:
                        report["failed"] += len(chunk)
                        log.warning("seed batch of %d failed: %s", len(chunk), exc)
                    else:
                        time.sleep(1.5)
    finally:
        w.close()
    report["elapsed_s"] = time.perf_counter() - started
    return report


__all__ = [
    "MIN_PRINTS",
    "SEED_MULTIPLE",
    "SEED_WRITE_BATCH",
    "SEED_WRITE_RETRIES",
    "run",
    "seed_counts",
    "seed_tokens",
]
