"""Who deployed this token, and what happened to the last things they deployed.

MEASURED 2026-09-22 on our own tape: sol, 7 days, 3,062 tokens attributed to 1,994
deployers, outcome = peak price divided by the first price we observed, for tokens with at
least 8 priced prints. Baseline over that set is 13.2% reaching 2x and 3.4% reaching 5x.

Cut by the deployer's STRICTLY EARLIER launches and by how many tokens they ship a week::

    volume        prior record       n     >=2x    >=5x
    low  (1-10)   no prior        1754    14.9%    3.7%
    low  (1-10)   all dud          182    11.5%    3.3%
    low  (1-10)   >=2x              47    17.0%    4.3%
    mid  (11-50)  no prior         189    12.7%    3.7%
    mid  (11-50)  all dud          304     8.2%    2.6%
    mid  (11-50)  >=2x             125    19.2%    5.6%
    spam (51+)    no prior          52    15.4%    3.8%
    spam (51+)    all dud           266     5.3%    0.4%
    spam (51+)    >=2x              143    12.6%    4.2%

Two findings, and the second one only appears once you control for the first:

* **Volume and record COMPOSE.** A deployer shipping 51+ tokens a week whose earlier ones
  all flopped hits 2x at 5.3% against a 13.2% baseline, and 5x at 0.4% against 3.4% --
  eight times worse. That is the cleanest negative filter in the set.
* **A prior runner is worth something, but only modestly**, and it is NOT visible in a
  pooled table: pooling spam factories with selective deployers makes "prior >=2x" look
  identical to "first launch ever". Within a volume band it runs 17-19% against 13.2%.

WHAT THIS IS NOT. It is not a claim that good devs stay good. The >=5x cells are small
(47/125/143) and their means are dominated by single outliers -- the >=2x RATE is the
statistic to trust here, not the mean or the median. The window is 7 days and one chain.

MEASUREMENT CAVEATS, stated because they bound how hard this may be leaned on:

* The outcome is peak over the FIRST PRICE WE SAW, which is our observation start and not
  the launch price. For a token we picked up late this understates the true multiple.
* The ">= 8 priced prints" filter drops tokens that died before we could price them, so
  the baseline is already lifted by survivorship. Every bucket shares that lift, so the
  COMPARISON between buckets is still fair; the absolute rates are optimistic.
* ``tokens.creator`` is the launchpad's reported deployer. A dev using a fresh wallet per
  launch is invisible to this and correctly lands in "no prior".

This module computes into a table rather than at decision time: the underlying query is a
seven-day scan of ``swaps`` and belongs nowhere near an entry decision.

2026-10-02: THE OUTCOMES COME FROM :mod:`kaiba.intelligence.price_extent`, NOT A SCAN.
``refresh`` used to run two ``GROUP BY token`` queries over ``swaps`` per chain every 30
minutes, and the box's plan bounded them by ``chain`` only -- every swap the chain ever had,
token by token, with a table lookup per row (``SEARCH swaps USING INDEX idx_swaps_token
(chain=?)``). 197 s mean, 5 of 28 runs past the 300 s timeout in 24 h, plus a second full
copy inside ``learning_sweep``. The extent table is folded forward from a ``swaps.id``
cursor, so a run reads only the swaps inserted since the last one.

(SUPERSEDED 2026-10-04 -- the peak is now numeric and single-source; see the last section.)
THE PEAK RULE IS UNCHANGED BY DEFAULT, AND IT IS WRONG. ``MAX(price_usd)`` on the TEXT
column is the lexicographic maximum (``'9.1e-05' > '0.0005'``); on the box's newest 20,000
swaps it differed from the numeric maximum for 35 of 178 tokens with eight or more prints.
:data:`PEAK_RULE` keeps that string maximum (``price_extent.PEAK_LEGACY_TEXT``) because the
shipped ``DEPLOYER_LADDER`` was measured with it and moving the labels under the sizer is
not this change's call. MEASURED 2026-10-02 on the box, all 1,740 fresh sol deployers with
11+ launches, old rule recomputed from the tape (it reproduced the stored table for 1,737):
under the numeric rule 184 change label -- 135 ``mid/all_dud`` and 39 ``spam/all_dud``
become ``runner``, 3 become ``no_prior``, 6 ``mid/runner`` become ``mid/all_dud``. That is
177 deployers OUT of the charged buckets (727 -> 556) and 6 in. Flip :data:`PEAK_RULE` to
``PEAK_NUMERIC`` only with the ladder's bucket rates re-measured under it.

Two smaller differences from the scan, both deliberate and both present under either rule:

* **A token is an outcome of the week it was FIRST priced in.** The scan clipped every
  token's tape to the window, so a token born before the window was scored from whatever
  price it happened to show seven days ago -- a mid-life price, not the launch price this
  table is defined on. Such a token is no longer scored; its launch is outside the window,
  which is also where ``launches`` (``tokens.first_seen_ms``) already put it.
* A print whose price does not parse as a finite positive number is not a print.

Launch counts still come from ``tokens``: MEASURED 370k rows in all, and the newest eight
days read in 0.75 s, so that half was never the cost.

2026-10-04: ONE SOURCE PER SERIES, AND THE NUMERIC PEAK. ``swaps`` holds prints from
several feeds (``pumpfun:trades``, ``gmgn:smartmoney``, ``gmgn:kol``, ``helius:backfill``,
``alchemy:ws``, the Robinhood poller ``robinhood``). Every one of them stores USD per WHOLE
token -- MEASURED on the box's newest 2M swaps, the median cross-source ratio for the same
token within 60 s is 1.00 on every chain -- but they disagree print by print: on sol 6.3%
of ``gmgn:smartmoney``/``pumpfun:trades`` pairs differ by more than 2x and 0.8% by more
than 10x, against 0.8% / 0.07% for consecutive prints of ``pumpfun:trades`` itself. A peak
taken over the MIXED series picks up the other feed's high misprint, and a first price
taken from it picks up the other feed's low one. MEASURED the same day on the newest 1M
swaps: 27.6% of sol tokens with eight prints "reach 2x" on the mixed series and 20.0% on a
single-source one (robinhood and bsc unchanged). The string maximum this module used to
take as the peak read 11.7% -- wrong in the other direction.

So a launch's outcome is now read from ONE source (:func:`series_source`: the source with
the most priced prints for that token) and from its NUMERIC peak (:data:`PEAK_RULE`). The
per-source first/peak/count is folded from its own swaps cursor into
:data:`SOURCE_TABLE` exactly as :mod:`price_extent` folds the mixed one, so a run still
reads only the swaps inserted since the last. Which tokens are outcomes of the window is
unchanged: the FIRST priced print over every source must be inside it, so a token born
before the window is not re-admitted because one feed started covering it later.
:func:`series_source` is also the rule :mod:`kaiba.learning.outcomes` and
:mod:`kaiba.learning.mooner` read their series by, so the three agree on what "the price"
of a token was.

RE-MEASURED 2026-10-04 on the box (read-only), the table above under the new rule. The
original window (sol, as of 09-22 12:00) reproduces under the OLD rule (n 3,220, baseline
12.9%, spam/all_dud 5.4%, mid/>=2x 18.6%); under one source + numeric peak the baseline is
21.7% and the shape holds (spam/all_dud 7.9%, mid/>=2x 36.6%, low/>=2x 31.8%). On the
newest week (09-27..10-04, sol n 25,144, baseline 19.9%) it is weaker: spam/all_dud 16.0%,
mid/all_dud 15.2%, mid/>=2x 26.5%, low/>=2x 23.7%; on robinhood (baseline 27.0%) mid/>=2x
is 25.9% -- no lift. Reach on the tape is not P&L.

DEPLOY NOTE: :data:`SOURCE_TABLE` starts empty and must fold the whole tape once before
:func:`refresh` will write (it raises :class:`ExtentNotReady` until then, and the stats go
stale meanwhile). Fold it by hand before restarting the ops service, repeating until it
exits 0 (it waits while the WAL is over 1.5 GB; the cursor persists between runs)::

    timeout 1200 nice -n 19 ionice -c3 .venv/bin/python -m kaiba.intelligence.deployer \\
        --budget-s 1080

``--refresh sol,bsc,robinhood`` then rewrites ``deployer_stats`` at once -- only after the
ops service runs this code, or its next scheduled run rewrites them under the old rule.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, tx
from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import price_extent

log = logging.getLogger(__name__)

#: Outcome window and the minimum evidence per token, both as measured above.
WINDOW_DAYS = 7
#: Addresses per ``IN (...)`` when reading the creators of the scored tokens; well under
#: SQLite's 999-variable floor on old builds.
CREATOR_LOOKUP_CHUNK = 500
MIN_PRINTS = 8

#: A launch counts as a "runner" for the deployer's record at this multiple. Chosen as the
#: threshold the measurement actually discriminates on: the >=2x rate separates the buckets
#: cleanly (5.3% to 19.2%) while the >=5x rate runs on cells of 47-143 tokens.
RUNNER_MULTIPLE = Decimal(2)

#: Launch-volume bands, from the table above.
SPAM_LAUNCHES = 51
MID_LAUNCHES = 11

#: How long a computed record stays usable. The inputs move slowly -- a deployer's weekly
#: launch count does not change meaningfully inside an hour -- and a stale record is far
#: cheaper than a seven-day scan on the entry path.
STALE_AFTER_S = 3600

#: Deployer rows upserted per write transaction. The table is ~83,000 rows across the three
#: chains on the box and was written in ONE transaction, holding the single WAL writer for
#: the whole upsert while protection waits behind a 10 s busy timeout.
WRITE_BATCH = 5_000

#: Which peak a launch's outcome is measured on. NUMERIC since 2026-10-04 (it was the
#: LEGACY_TEXT string maximum; see the module docstring for what that read).
PEAK_RULE = price_extent.PEAK_NUMERIC


#: Raised instead of writing a record from a half-folded extent. See price_extent.
ExtentNotReady = price_extent.ExtentNotReady


# ---------------------------------------------------------------------------- one source


#: When two sources hold the same number of priced prints for a token, the first one named
#: here wins: per-token tape and on-chain derivations before the wallet feeds. A source
#: not named ranks after every named one, then by name, so the choice is deterministic.
SERIES_SOURCE_PRIORITY: dict[str, tuple[str, ...]] = {
    "sol": ("pumpfun:trades", "alchemy:ws", "helius:backfill", "gmgn:smartmoney", "gmgn:kol"),
    "robinhood": ("robinhood", "alchemy:ws", "gmgn:smartmoney", "gmgn:kol"),
    "bsc": ("gmgn:smartmoney", "gmgn:kol"),
}


def series_source(counts: Mapping[str, int], chain: str) -> str | None:
    """The ONE swap source a token's price series is read from, or ``None`` with no prints.

    ``counts`` is priced prints per source for one token. The source with the most wins;
    a tie goes to :data:`SERIES_SOURCE_PRIORITY`, then to the name.
    """
    live = {str(s): int(n) for s, n in counts.items() if s is not None and int(n) > 0}
    if not live:
        return None
    order = SERIES_SOURCE_PRIORITY.get(str(chain), ())
    return min(live, key=lambda s: (-live[s], order.index(s) if s in order else len(order), s))


def single_source(
    rows: Iterable[tuple[int, float, str]], chain: str
) -> list[tuple[int, float]]:
    """``(ts_ms, price)`` of the one source :func:`series_source` picks, in input order.

    ``rows`` are ``(ts_ms, price, source)`` with the price already parsed and positive.
    """
    rows = list(rows)
    counts: dict[str, int] = {}
    for _ts, _px, source in rows:
        counts[source] = counts.get(source, 0) + 1
    pick = series_source(counts, chain)
    return [(ts, px) for ts, px, source in rows if source == pick]


#: The per-source extent: price_extent's first/peak/count, one row per (chain, token, source).
SOURCE_TABLE = "token_price_extent_by_source"

#: ``kv`` key holding the highest ``swaps.id`` already folded into :data:`SOURCE_TABLE`.
KV_SOURCE_CURSOR = "price_extent_by_source:swaps_id"

_SOURCE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} ("
    "  chain       TEXT    NOT NULL,"
    "  token       TEXT    NOT NULL,"
    "  source      TEXT    NOT NULL,"
    "  first_ts_ms INTEGER NOT NULL,"
    "  first_px    REAL    NOT NULL,"
    "  peak_px     REAL    NOT NULL,"
    "  peak_text   TEXT    NOT NULL,"
    "  prints      INTEGER NOT NULL,"
    "  last_ts_ms  INTEGER NOT NULL,"
    "  PRIMARY KEY (chain, token, source)) WITHOUT ROWID",
    f"CREATE INDEX IF NOT EXISTS idx_{SOURCE_TABLE}_first ON {SOURCE_TABLE}(chain, first_ts_ms)",
)

_SOURCE_UPSERT = (
    f"INSERT INTO {SOURCE_TABLE} (chain, token, source, first_ts_ms, first_px, peak_px, "
    "peak_text, prints, last_ts_ms) VALUES (?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(chain, token, source) DO UPDATE SET "
    # Same order-independent fold as price_extent._UPSERT: the right-hand sides read the row
    # as it was before this update, and the earlier fold keeps the first price on a tie.
    f"  first_px = CASE WHEN excluded.first_ts_ms < {SOURCE_TABLE}.first_ts_ms "
    f"                  THEN excluded.first_px ELSE {SOURCE_TABLE}.first_px END, "
    f"  first_ts_ms = MIN({SOURCE_TABLE}.first_ts_ms, excluded.first_ts_ms), "
    f"  peak_px = MAX({SOURCE_TABLE}.peak_px, excluded.peak_px), "
    f"  peak_text = MAX({SOURCE_TABLE}.peak_text, excluded.peak_text), "
    f"  prints = {SOURCE_TABLE}.prints + excluded.prints, "
    f"  last_ts_ms = MAX({SOURCE_TABLE}.last_ts_ms, excluded.last_ts_ms)"
)


@dataclass(frozen=True)
class SourceExtent:
    """One token's first price, peak and print count on ONE source."""

    source: str
    first_ts_ms: int
    first_px: float
    peak_px: float
    peak_text: str
    prints: int


def ensure_source_table(conn: sqlite3.Connection) -> None:
    for stmt in _SOURCE_DDL:
        conn.execute(stmt)


def _read_source_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_SOURCE_CURSOR,)).fetchone()
    if row is None:
        return 0
    try:
        return max(0, int(row[0]))
    except (TypeError, ValueError):
        return 0


def fold_sources(rows: Iterable[tuple[Any, ...]]) -> dict[tuple[str, str, str], list[Any]]:
    """``(chain, token, source) -> [first_ts, first_px, peak_px, prints, last_ts, peak_text]``.

    ``rows`` are ``(id, chain, token, source, ts_ms, price_usd)`` in ascending id order. A
    price that is not a finite positive number is not a print (``price_extent.price_of``).
    """
    acc: dict[tuple[str, str, str], list[Any]] = {}
    for _id, chain, token, source, ts_ms, raw in rows:
        px = price_extent.price_of(raw)
        if px is None or ts_ms is None:
            continue
        ts = int(ts_ms)
        key = (str(chain), str(token), str(source))
        text = str(raw)
        cur = acc.get(key)
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


def advance_sources(
    conn: sqlite3.Connection,
    *,
    deadline: float | None = None,
    batch_rows: int = price_extent.BATCH_ROWS,
    clock: Callable[[], float] = time.time,
) -> price_extent.AdvanceReport:
    """Fold every swap inserted since the cursor into :data:`SOURCE_TABLE`.

    The same contract as :func:`price_extent.advance` -- batches, a per-batch transaction
    that re-reads the cursor so two callers cannot fold one range twice, a deadline checked
    before each batch -- on its own cursor.
    """
    ensure_source_table(conn)
    started = time.monotonic()
    rep = price_extent.AdvanceReport()
    rep.cursor_from = rep.cursor_to = _read_source_cursor(conn)
    rep.target_id = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM swaps").fetchone()[0])
    size = max(1, int(batch_rows))
    while rep.cursor_to < rep.target_id:
        if deadline is not None and clock() >= deadline:
            break
        start = rep.cursor_to
        rows = conn.execute(
            "SELECT id, chain, token, source, ts_ms, price_usd FROM swaps "
            "WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
            (start, rep.target_id, size),
        ).fetchall()
        if not rows:
            end = rep.target_id
            acc: dict[tuple[str, str, str], list[Any]] = {}
        else:
            end = int(rows[-1][0])
            acc = fold_sources(rows)
        with tx(conn):
            if _read_source_cursor(conn) != start:
                rep.lost_races += 1
                rep.cursor_to = _read_source_cursor(conn)
                continue
            if acc:
                conn.executemany(
                    _SOURCE_UPSERT,
                    [(ch, tok, src, v[0], v[1], v[2], v[5], v[3], v[4])
                     for (ch, tok, src), v in acc.items()],
                )
            conn.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) "
                "DO UPDATE SET value = excluded.value, updated_ms = excluded.updated_ms",
                (KV_SOURCE_CURSOR, str(int(end)), int(clock() * 1000)),
            )
        rep.batches += 1
        rep.rows_read += len(rows)
        rep.priced_rows += sum(v[3] for v in acc.values())
        rep.token_rows_written += len(acc)
        rep.cursor_to = end
    rep.elapsed_s = time.monotonic() - started
    return rep


def _source_extent(r: Any) -> SourceExtent:
    return SourceExtent(str(r[0]), int(r[1]), float(r[2]), float(r[3]), str(r[4]), int(r[5]))


_SOURCE_COLS = "source, first_ts_ms, first_px, peak_px, peak_text, prints"


def outcome_multiple(
    chain: str,
    extents: Iterable[SourceExtent],
    since_ms: int,
    *,
    min_prints: int = MIN_PRINTS,
    rule: str | None = None,
) -> Decimal | None:
    """One launch's outcome: peak over first price on its ONE series source, or ``None``.

    ``None`` when the token's first priced print on ANY source is not after ``since_ms``
    (its launch is outside the window), or when the chosen source has fewer than
    ``min_prints`` prints. ``rule`` defaults to :data:`PEAK_RULE` read at call time.
    """
    extents = list(extents)
    if not extents or min(e.first_ts_ms for e in extents) <= since_ms:
        return None
    pick = series_source({e.source: e.prints for e in extents}, chain)
    ext = next((e for e in extents if e.source == pick), None)
    if ext is None or ext.prints < min_prints:
        return None
    rule = rule or PEAK_RULE
    if rule == price_extent.PEAK_NUMERIC:
        peak_f: float | None = ext.peak_px
    elif rule == price_extent.PEAK_LEGACY_TEXT:
        peak_f = price_extent.price_of(ext.peak_text)
    else:
        raise ValueError(f"unknown peak rule {rule!r}")
    first = _dec(repr(ext.first_px))
    peak = _dec(repr(peak_f)) if peak_f is not None else None
    if not first or not peak or first <= 0 or peak <= 0:
        return None
    return peak / first


def outcomes_since(
    conn: sqlite3.Connection, chain: str, since_ms: int, *, min_prints: int = MIN_PRINTS
) -> dict[str, Decimal]:
    """``token -> multiple`` for every launch first priced after ``since_ms`` on ``chain``.

    A range on ``(chain, first_ts_ms)`` finds the candidates; every source row of each is
    then read so the series source is chosen over all of them.
    """
    grouped: dict[str, list[SourceExtent]] = {}
    for r in conn.execute(
        f"SELECT token, {_SOURCE_COLS} FROM {SOURCE_TABLE} WHERE chain = ? AND token IN "
        f"(SELECT token FROM {SOURCE_TABLE} WHERE chain = ? AND first_ts_ms > ?)",
        (chain, chain, int(since_ms)),
    ):
        grouped.setdefault(str(r[0]), []).append(_source_extent(r[1:]))
    out: dict[str, Decimal] = {}
    for token, extents in grouped.items():
        multiple = outcome_multiple(chain, extents, since_ms, min_prints=min_prints)
        if multiple is not None:
            out[token] = multiple
    return out


@dataclass(frozen=True)
class DeployerRecord:
    """What we know about a deployer, and how sure we are."""

    chain: Chain
    wallet: str | None
    launches: int
    prior_scored: int
    prior_runners: int
    prior_best_multiple: Decimal | None
    basis: EvidenceBasis
    note: str = ""

    @property
    def known(self) -> bool:
        return self.basis is not EvidenceBasis.UNAVAILABLE

    @property
    def volume_band(self) -> str:
        if self.launches >= SPAM_LAUNCHES:
            return "spam"
        if self.launches >= MID_LAUNCHES:
            return "mid"
        return "low"

    @property
    def record(self) -> str:
        if self.prior_scored <= 0:
            return "no_prior"
        return "runner" if self.prior_runners > 0 else "all_dud"

    @property
    def label(self) -> str:
        """``volume/record``, or ``unknown``. The key the size table is written against."""
        if not self.known:
            return "unknown"
        return f"{self.volume_band}/{self.record}"


UNKNOWN = DeployerRecord(
    chain=Chain.SOL, wallet=None, launches=0, prior_scored=0, prior_runners=0,
    prior_best_multiple=None, basis=EvidenceBasis.UNAVAILABLE, note="no record",
)


# ---------------------------------------------------------------------------- the table


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deployer_stats ("
        "  chain TEXT NOT NULL,"
        "  wallet TEXT NOT NULL,"
        "  launches INTEGER NOT NULL,"
        "  scored INTEGER NOT NULL,"
        "  runners INTEGER NOT NULL,"
        "  best_multiple TEXT,"
        "  computed_ms INTEGER NOT NULL,"
        "  PRIMARY KEY (chain, wallet))"
    )


def refresh(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    window_days: int = WINDOW_DAYS,
    now_ms: int | None = None,
    deadline: float | None = None,
) -> int:
    """Recompute every deployer's record on ``chain``. Returns rows written.

    Folds the swaps inserted since the last call into the per-source extent first (bounded
    by ``deadline``, epoch seconds), then reads each launch's outcome from its one series
    source. Raises :class:`ExtentNotReady` rather than write a record from a half-folded
    extent.
    """
    c = conn or get_conn()
    ensure_table(c)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    since = now - window_days * 86_400_000

    progress = advance_sources(c, deadline=deadline)
    if not progress.caught_up:
        raise ExtentNotReady(progress.as_dict())

    multiples = outcomes_since(c, chain.value, since, min_prints=MIN_PRINTS)

    # Only the tokens that HAVE an outcome need their creator, read by primary key in
    # chunks. Until 2026-10-05 this read every row the chain ever stored, with no window;
    # bounded by the outcome set, it reads only what it uses and does not grow with the
    # ~23k rows a day the Flap listener writes on bsc (review 2026-10-05). Same answer.
    creators: dict[str, str] = {}
    outcome_tokens = list(multiples)
    for offset in range(0, len(outcome_tokens), CREATOR_LOOKUP_CHUNK):
        chunk = outcome_tokens[offset:offset + CREATOR_LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        for row in fetch_all(
            c,
            f"SELECT address, creator FROM tokens WHERE chain=? AND address IN ({marks}) "
            # Unary + keeps SQLite on the (chain, address) primary key: without it the planner
            # picks idx_tokens_creator and range-scans every token on the chain once per chunk
            # (MEASURED pre-deploy 2026-10-05: sol 109 s vs 0.9 s, robinhood 18 s vs 1.1 s).
            "AND +creator IS NOT NULL AND +creator <> ''",
            (chain.value, *chunk),
        ):
            creators[str(row["address"])] = str(row["creator"])
    launches: dict[str, int] = {
        str(row["creator"]): int(row["n"])
        for row in fetch_all(
            c,
            "SELECT creator, COUNT(*) AS n FROM tokens "
            "WHERE chain=? AND creator IS NOT NULL AND creator <> '' AND first_seen_ms > ? "
            "GROUP BY creator",
            (chain.value, since),
        )
    }

    scored: dict[str, list[Decimal]] = {}
    for token, multiple in multiples.items():
        wallet = creators.get(token)
        if wallet:
            scored.setdefault(wallet, []).append(multiple)

    rows: list[tuple[Any, ...]] = []
    for wallet, count in launches.items():
        outcomes = scored.get(wallet, [])
        best = max(outcomes) if outcomes else None
        runners = sum(1 for m in outcomes if m >= RUNNER_MULTIPLE)
        rows.append(
            (chain.value, wallet, count, len(outcomes), runners,
             str(best) if best is not None else None, now)
        )
    if not rows:
        return 0
    for offset in range(0, len(rows), WRITE_BATCH):
        with tx(c) as conn2:
            conn2.executemany(
                "INSERT INTO deployer_stats (chain, wallet, launches, scored, runners, "
                "best_multiple, computed_ms) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(chain, wallet) DO UPDATE SET launches=excluded.launches, "
                "scored=excluded.scored, runners=excluded.runners, "
                "best_multiple=excluded.best_multiple, computed_ms=excluded.computed_ms",
                rows[offset:offset + WRITE_BATCH],
            )
    log.info("deployer_stats: %d wallets on %s", len(rows), chain.value)
    return len(rows)


# ---------------------------------------------------------------------------- lookup


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def creator_of(conn: sqlite3.Connection, chain: Chain, token: str) -> str | None:
    row = fetch_one(
        conn, "SELECT creator FROM tokens WHERE chain=? AND address=?", (chain.value, token)
    )
    creator = (row["creator"] if row else None) or ""
    return str(creator).strip() or None


def lookup(
    conn: sqlite3.Connection,
    chain: Chain,
    token: str,
    *,
    now_ms: int | None = None,
) -> DeployerRecord:
    """This token's deployer record. Cheap: one indexed read of ``deployer_stats``.

    The deployer's own launch of THIS token is excluded from the record -- the whole
    measurement is of strictly earlier launches, and counting the token being judged is the
    leakage that made a first pass of this study report "all dud" cells at exactly 0%.
    """
    try:
        wallet = creator_of(conn, chain, token)
    except sqlite3.Error as exc:
        # The creator read is a database call like any other, and it happens BEFORE the
        # stats read. Leaving it outside the guard made a disk error propagate into the
        # entry path instead of degrading to "we do not know".
        return DeployerRecord(chain, None, 0, 0, 0, None,
                              EvidenceBasis.UNAVAILABLE, f"creator unreadable: {exc}")
    if not wallet:
        return DeployerRecord(chain, None, 0, 0, 0, None,
                              EvidenceBasis.UNAVAILABLE, "no creator recorded")
    try:
        ensure_table(conn)
        row = fetch_one(
            conn,
            "SELECT launches, scored, runners, best_multiple, computed_ms FROM deployer_stats "
            "WHERE chain=? AND wallet=?",
            (chain.value, wallet),
        )
    except sqlite3.Error as exc:
        return DeployerRecord(chain, wallet, 0, 0, 0, None,
                              EvidenceBasis.UNAVAILABLE, f"stats unreadable: {exc}")
    if row is None:
        return DeployerRecord(chain, wallet, 0, 0, 0, None,
                              EvidenceBasis.UNAVAILABLE, "deployer not in stats")
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    age_s = (now - int(row["computed_ms"])) / 1000
    if age_s > STALE_AFTER_S:
        return DeployerRecord(chain, wallet, 0, 0, 0, None, EvidenceBasis.UNAVAILABLE,
                              f"stats stale by {age_s:.0f}s")

    # This token is in the aggregate; take it back out so the record is prior-only.
    scored = int(row["scored"])
    runners = int(row["runners"])
    this = _this_token_multiple(conn, chain, token, now)
    if this is not None:
        scored = max(0, scored - 1)
        if this >= RUNNER_MULTIPLE:
            runners = max(0, runners - 1)
    return DeployerRecord(
        chain=chain,
        wallet=wallet,
        launches=int(row["launches"]),
        prior_scored=scored,
        prior_runners=runners,
        prior_best_multiple=_dec(row["best_multiple"]),
        basis=EvidenceBasis.DERIVED,
        note=f"age{age_s:.0f}s",
    )


def _this_token_multiple(
    conn: sqlite3.Connection, chain: Chain, token: str, now_ms: int
) -> Decimal | None:
    """The multiple this token itself contributed to the aggregate, if it contributed one.

    Read from the same per-source rows ``refresh`` aggregated, under the same rule (first
    priced in the window on any source, the series source chosen the same way, at least
    :data:`MIN_PRINTS` prints on it), so the subtraction in :func:`lookup` takes back
    exactly what was added. One primary-key prefix seek; no swap is read.
    """
    since = now_ms - WINDOW_DAYS * 86_400_000
    try:
        rows = conn.execute(
            f"SELECT {_SOURCE_COLS} FROM {SOURCE_TABLE} WHERE chain = ? AND token = ?",
            (chain.value, token),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    return outcome_multiple(chain.value, (_source_extent(r) for r in rows), since)


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fold the per-source extent by hand (the bootstrap), and optionally refresh the stats.

    Folds in slices of at most ``--slice-s`` seconds and, before each slice, waits while
    the database's WAL file is over ``--max-wal-mb`` (the box's DB-guard rule: a bootstrap
    must not add pressure while the WAL cannot reset). Exits 2 when the budget ran out
    first; the cursor persists, so rerunning continues where it stopped.
    """
    from kaiba.core.config import get_settings
    from kaiba.core.db import connect

    ap = argparse.ArgumentParser(description="fold swaps into token_price_extent_by_source")
    ap.add_argument("--budget-s", type=float, default=1080.0,
                    help="stop folding after this many seconds; the cursor persists, rerun")
    ap.add_argument("--slice-s", type=float, default=60.0,
                    help="fold at most this long between WAL checks")
    ap.add_argument("--max-wal-mb", type=float, default=1536.0,
                    help="wait (60 s at a time) while the WAL file is larger than this")
    ap.add_argument("--batch-rows", type=int, default=price_extent.BATCH_ROWS)
    ap.add_argument("--refresh", default="",
                    help="comma-separated chains to recompute deployer_stats for once caught up")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    wal = get_settings().db_path.with_name(get_settings().db_path.name + "-wal")
    stop_at = clock() + args.budget_s
    conn = connect()
    try:
        caught_up = False
        while clock() < stop_at:
            wal_mb = wal.stat().st_size / 1e6 if wal.exists() else 0.0
            if wal_mb > args.max_wal_mb:
                log.info("WAL %.0f MB > %.0f MB; waiting 60 s", wal_mb, args.max_wal_mb)
                sleep(60)
                continue
            rep = advance_sources(conn, deadline=min(stop_at, clock() + args.slice_s),
                                  batch_rows=args.batch_rows, clock=clock)
            print(rep.as_dict(), flush=True)
            caught_up = rep.caught_up
            if caught_up:
                break
        if not caught_up:
            return 2
        for name in [c.strip() for c in args.refresh.split(",") if c.strip()]:
            print(name, refresh(conn, Chain(name)), flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
