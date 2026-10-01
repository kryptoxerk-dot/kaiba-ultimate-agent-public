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
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, tx
from kaiba.core.schemas import Chain, EvidenceBasis

log = logging.getLogger(__name__)

#: Outcome window and the minimum evidence per token, both as measured above.
WINDOW_DAYS = 7
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
) -> int:
    """Recompute every deployer's record on ``chain``. Returns rows written.

    Deliberately a batch job. See the module docstring: this is a seven-day scan.
    """
    c = conn or get_conn()
    ensure_table(c)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    since = now - window_days * 86_400_000

    peaks = fetch_all(
        c,
        "SELECT token, MAX(price_usd) AS pk, COUNT(*) AS n FROM swaps "
        "WHERE chain=? AND price_usd IS NOT NULL AND ts_ms > ? "
        "GROUP BY token HAVING n >= ?",
        (chain.value, since, MIN_PRINTS),
    )
    firsts = {
        row["token"]: row["px"]
        for row in fetch_all(
            c,
            "SELECT token, price_usd AS px FROM swaps "
            "WHERE chain=? AND price_usd IS NOT NULL AND ts_ms > ? "
            "GROUP BY token HAVING ts_ms = MIN(ts_ms)",
            (chain.value, since),
        )
    }
    multiples: dict[str, Decimal] = {}
    for row in peaks:
        first = _dec(firsts.get(row["token"]))
        peak = _dec(row["pk"])
        if first and peak and first > 0 and peak > 0:
            multiples[str(row["token"])] = peak / first

    creators = {
        str(row["address"]): str(row["creator"])
        for row in fetch_all(
            c,
            "SELECT address, creator FROM tokens "
            "WHERE chain=? AND creator IS NOT NULL AND creator <> ''",
            (chain.value,),
        )
    }
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
    with tx(c) as conn2:
        conn2.executemany(
            "INSERT INTO deployer_stats (chain, wallet, launches, scored, runners, "
            "best_multiple, computed_ms) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(chain, wallet) DO UPDATE SET launches=excluded.launches, "
            "scored=excluded.scored, runners=excluded.runners, "
            "best_multiple=excluded.best_multiple, computed_ms=excluded.computed_ms",
            rows,
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
    """The multiple this token itself contributed to the aggregate, if it contributed one."""
    since = now_ms - WINDOW_DAYS * 86_400_000
    row = fetch_one(
        conn,
        "SELECT MAX(price_usd) AS pk, MIN(ts_ms) AS tf, COUNT(*) AS n FROM swaps "
        "WHERE chain=? AND token=? AND price_usd IS NOT NULL AND ts_ms > ?",
        (chain.value, token, since),
    )
    if row is None or not row["n"] or int(row["n"]) < MIN_PRINTS:
        return None
    first = fetch_one(
        conn,
        "SELECT price_usd AS px FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
        "AND ts_ms > ? ORDER BY ts_ms LIMIT 1",
        (chain.value, token, since),
    )
    start = _dec(first["px"]) if first else None
    peak = _dec(row["pk"])
    if not start or not peak or start <= 0:
        return None
    return peak / start
