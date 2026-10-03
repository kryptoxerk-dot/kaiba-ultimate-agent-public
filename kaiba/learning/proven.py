"""Proven wallets: the ones whose COPY returns persist out of sample, per chain.

Why this exists, in measured terms:

* **Our grades do not predict on sol.** Buys made AFTER a wallet was graded, scored 24 h
  forward: sol B reached 2x 28.0% of the time (median -40.2%) against C/D at 27.4%
  (-41.1%), both worse than a random sol buy (~18% / -3 to -7%). Robinhood B carries a
  modest edge (mean -5.3% against -19.4%). A grade is a description of the past.
* **GMGN's smart label is anti-calibrated in the live lane.** 3-4 smart wallets +6.8%,
  8-12 -19.1%, 7+ a 0% win rate.
* **Copy persistence is the one wallet-level measure that has ever held out of sample**
  (:func:`kaiba.learning.copytrade.persistence`, robinhood). It answers the question a
  grade does not: had we followed this wallet, at our own lag and our own fees, would we
  have made money -- and does the answer survive into a period it was not chosen on?

So "proven" means exactly that, and nothing softer:

1. **Copy model** -- :func:`kaiba.learning.copytrade.next_print`, reused rather than
   restated. Our entry is the first print at or after their buy plus ``lag_ms``; our exit
   the first print at or after their sell plus ``lag_ms``; ``fee_bps`` (1%/leg) comes off
   every trip. An unfillable ENTRY is dropped (we never bought), never scored 0. One
   deliberate departure, on the exit: when no print follows their sell within the wait,
   ``copytrade`` drops the trip, which silently deletes the copies where the leader
   dumped into a book nobody traded again. Here that exit is priced at the book as it
   stood -- the last print before our exit, usually their own dump (``_price_trips``).
   Slippage and gas are NOT in it, so every number here is still an upper bound.
2. **Split on the MEDIAN SWAP of the chain inside the look-back**, not the clock (the
   reason is in ``copytrade.persistence``: a clock midpoint once put 1,055 swaps on one
   side and 493,426 on the other). Estimated from evenly spaced primary-key samples, so
   it never sorts the tape (see :func:`estimate_median_swap_ms`).
3. **In sample**: >= ``min_trips_in`` fillable round trips and a positive mean.
4. **Out of sample**: >= ``min_trips_out`` fillable trips and a one-sided bootstrap lower
   bound on the mean above zero. Trips that straddle the split belong to neither side.
5. **The chain itself must show persistence**, or nobody on it is proven: wallets that
   won in sample must out-earn wallets that lost in sample, out of sample, with a
   wallet-clustered bootstrap interval on the difference that excludes zero, and must
   beat the whole eligible population (the expected value of a random eligible wallet).
   Without this a few thousand candidates hand back a few dozen "proven" wallets by luck
   alone. The same OOS test applied to the in-sample LOSERS is reported as the false
   positive rate, and a cohort that does not exceed it is published EMPTY.

**MEASURED 2026-10-02 on the box (read-only run of this module):** both chains EMPTY.
Robinhood's in-sample winners did out-earn its losers out of sample (-3.0% against -10.8%
per trip, wallet-clustered difference +5.9..+9.9 pp) -- the rank persists -- but even the
winners lose money copied at 20 s and 1%/leg, and 1 of 73 passing alone is what chance
gives at the losers' pass rate (p = 0.65). Sol's apparent persistence came from a dust
network (median buy $0.04) and vanished under the buy floor (difference -1.6..+5.1 pp).

**MEASURED 2026-10-03: the candidate DRAW was the binding stage, not the tape.** The job
scored a random 3,000 of the recently active wallets -- 36% of robinhood's 8,284 and 7% of
sol's 42,278 -- and most of any draw can never be eligible (under ``2 * min_trips_in``
swaps before the split). An index-only prescreen of that necessary condition
(``ProvenConfig.prescreen``) makes scoring the whole band cheap, and on the box it took sol
from 162 eligible / 0 proven to 2,622 / 11 (chance p 4e-6) and robinhood from 443 / 5 to
1,249 / 6 (p 0.043, at the gate's edge). Same thresholds; on a pinned draw the prescreen
changed no eligibility, statistic or member (``PINNED EQUIVALENT: True``).

**An empty cohort is an answer, not a failure.** It is frozen like any other, with its
reason, so the lane reads "nobody on this chain is proven" rather than falling back to a
stale list or to grades.

Membership is frozen into ``wallet_cohorts`` / ``wallet_cohort_freezes`` (migration 016)
under ``cohort_id = "proven:<chain>:<frozen_ms>"``. The treated arm is spelled
``graded`` and the comparison arm ``control`` because those are the two names
:func:`kaiba.learning.validation.control_arm` reads, so a frozen proven cohort can be
tracked forward against its own random-eligible control with the existing permutation
test instead of new arithmetic. ``wallets.cohort`` is NOT used: it holds one value per
wallet and the tracker polls ``cohort='tracked'``, so overwriting it would silently stop
following a wallet in order to label it.

Everything that touches ``swaps`` is an index seek (``idx_swaps_wallet``,
``idx_swaps_token``) or a primary-key read, bounded by a row cap, a candidate cap and a
deadline. Nothing here writes to a trading path; :func:`freeze` writes the two cohort
tables and nothing else.
"""

from __future__ import annotations

import logging
import math
import random
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload, tx
from kaiba.core.schemas import Chain, is_quote_asset
from kaiba.core.schemas import now_ms as _now_ms
from kaiba.learning.copytrade import ROUND_TRIP_FEE_BPS, next_print

log = logging.getLogger(__name__)

__all__ = [
    "COHORT_PREFIX",
    "CONTROL_ARM",
    "LAG_MS_BY_CHAIN",
    "TREATED_ARM",
    "ProvenCohort",
    "ProvenConfig",
    "ProvenReport",
    "WalletEvidence",
    "binom_tail",
    "bootstrap_lower",
    "build",
    "clear_cache",
    "config_from_params",
    "cotime_clusters",
    "cluster_bootstrap_diff",
    "cohort_id_for",
    "compact_summary",
    "estimate_median_swap_ms",
    "funnel_history",
    "parse_chains",
    "freeze",
    "latest_cohort",
    "proven_members",
    "run",
]

COHORT_PREFIX = "proven"
#: The two arm names :func:`kaiba.learning.validation.control_arm` reads. "graded" is the
#: treated arm here: the proven wallets. Spelled to match so the forward test is reused.
TREATED_ARM = "graded"
CONTROL_ARM = "control"

#: How late our fill is after the leader's trade, per chain. MEASURED 2026-10-02 on the
#: box, ingest lag = running max of ``ts_ms`` in id order minus the row's own ``ts_ms``
#: over the newest 150,000 swaps (the robinhood on-chain tape is the clock: p50 0 s):
#:
#:     robinhood  robinhood (on-chain)   p50  0 s  p75   2 s  p90  16 s
#:     sol        gmgn:smartmoney        p50 25 s  p75  40 s  p90  57 s
#:     sol        pumpfun:trades         p50 43 s  p75 184 s  p90 960 s
#:     bsc        gmgn:smartmoney        p50 41 s  p75  62 s  p90  90 s
#:
#: plus a few seconds of scan -> decide -> paper fill. Robinhood is near real time, so 20 s
#: covers its p90 with room; sol's broad tape is minutes late a quarter of the time, so
#: 120 s sits between its median and p75. Pass a different lag to price a faster feed.
LAG_MS_BY_CHAIN: dict[Chain, int] = {
    Chain.ROBINHOOD: 20_000,
    Chain.SOL: 120_000,
    Chain.BSC: 60_000,
}
DEFAULT_LAG_MS = 60_000


@dataclass(frozen=True)
class ProvenConfig:
    """Every threshold the cohort is chosen with. Recorded verbatim on each freeze."""

    #: The tape held 11.3 days on 2026-10-02 (swaps id 1 is that old); 10 days stays
    #: inside it so the in-sample half is not truncated by retention.
    lookback_s: int = 10 * 86_400
    #: ``None`` reads :data:`LAG_MS_BY_CHAIN`.
    lag_ms: int | None = None
    fee_bps: int = ROUND_TRIP_FEE_BPS
    #: A trip's next print may be at most this late, as in ``copytrade.next_print``.
    max_wait_ms: int = 600_000
    #: Evidence floors. Five trips per side is the least that lets a bootstrap say
    #: anything; it is a floor on evidence, not on quality.
    min_trips_in: int = 5
    min_trips_out: int = 5
    #: One-sided level of the per-wallet OOS lower bound, and of the chain-level test.
    alpha: float = 0.05
    bootstrap_draws: int = 1000
    #: Selection statistics cap a single trip at +500%. A one-print spike on a dead token
    #: would otherwise certify a wallet by itself. Raw means are still reported.
    net_cap: Decimal = Decimal("5")
    #: Candidates per chain, drawn at random from the recently active (``_recent_active``).
    #: With ``prescreen`` on, a drawn wallet that cannot be eligible costs one index-only
    #: seek; the rest cost one bounded index read, and the eligible ones two index seeks
    #: per round trip on top. The deadline, not this, is what binds on the box.
    #:
    #: MEASURED 2026-10-03 on the box (newest 1.5M swaps = 25.2 h): 8,284 robinhood and
    #: 42,278 sol wallets had 4..3000 swaps. The old cap of 3,000 drew 36% and 7% of them,
    #: and only 22% (robinhood) / 37% (sol) of any draw can be eligible at all -- the rest
    #: have under ``2 * min_trips_in`` swaps before the split. The draw, not the tape, was
    #: the stage that bound the cohort: 443 robinhood and 162 sol eligible of 3,000 each.
    #: Drawing the whole band (read-only runs of this code, same day, same thresholds):
    #:
    #:     sol        42,200 drawn  2,622 eligible  326 tested  11 passed vs null 7/1,384
    #:                chance p 4e-6 -> 11 proven (9 co-timing clusters); was 0. 395 s.
    #:     robinhood   8,287 drawn  1,249 eligible  191 tested   6 passed vs null 11/906
    #:                chance p 0.043 -> 6 proven; was 5. 33 s.
    #:
    #: Robinhood sits at the chance gate's edge: a 2,500 pinned draw gave 4 (p 0.016), and
    #: twice the discovery slice (3M rows) 8 passes vs 22/1,260 (p 0.11) -> EMPTY. More
    #: candidates there buys an honest test, not more wallets: its passes are four or five
    #: of the same addresses each time, and noise grows with the denominator.
    max_candidates: int = 50_000
    #: Skip the full read of a drawn wallet with fewer than ``2 * min_trips_in`` swaps of
    #: any kind between ``lo_ms`` and the split: each in-sample round trip is a buy and a
    #: sell inside that range, so such a wallet can never be eligible. A NECESSARY
    #: condition, counted index-only on ``idx_swaps_wallet`` -- it changes which wallets
    #: are read, never which are eligible (``test_the_prescreen_never_changes_who_is_eligible``).
    prescreen: bool = True
    #: How many of the newest swaps (all chains) the candidate draw reads by primary key.
    #: ~1.5M swaps a day on 2026-10-02, so this is roughly the last day of the tape.
    discovery_rows: int = 1_500_000
    #: Swaps a wallet needs inside that slice to be drawn at all.
    discovery_min_swaps: int = 4
    #: More swaps than this inside the look-back is a bot or a router, not a wallet a
    #: follower 20-120 s behind can copy. Also bounds the rows read per wallet.
    max_swaps_per_wallet: int = 3000
    #: How an exit with no print in the wait window is priced: "last_print" (the pool as
    #: it stood at our exit), "loss" (-100% after ``dead_after_ms`` of silence) or "drop"
    #: (``copytrade.evaluate_wallet`` parity). See ``_price_trips``.
    stale_exit: str = "last_print"
    #: For "loss" only: silence this long after the exit point counts as death (88% of
    #: tokens quiet for an hour never trade again); shorter is unobserved and dropped.
    dead_after_ms: int = 3_600_000
    #: A wallet's MEDIAN priced buy inside the look-back must be at least this, in USD.
    #: MEASURED 2026-10-02 on the box: without it, the 27 sol wallets that passed were a
    #: dust network -- median buy $0.04 (p10 $0.015, p90 $83), 2,004 of 4,988 buys landing
    #: within 2 s of another member's buy of the same token, 66 pairs in the same slot.
    #: Their "copy returns" price OUR $100 at a book their 0.0003 SOL never moved, and
    #: their agreement is one operator's schedule, not several opinions. $20 is a fifth of
    #: our own flat entry: a wallet that never puts that much behind a buy is not telling
    #: us anything a copier can act on. INVENTED threshold; the dust is measured.
    min_median_buy_usd: Decimal = Decimal("20")
    #: Two proven wallets that bought the same token within ``cotime_ms`` of each other at
    #: least ``cotime_min_events`` times are one operator for confluence counting (the
    #: co-timing fingerprint that still catches a laundered bundle). The lane counts
    #: clusters, never more than the entity graph would.
    cotime_ms: int = 2_000
    cotime_min_events: int = 3
    #: Primary-key samples used to estimate the median swap.
    split_samples: int = 2000
    #: Publish nothing unless the chain shows persistence (module docstring, rule 5).
    require_chain_persistence: bool = True
    seed: int = 20_261_002

    def lag_for(self, chain: Chain) -> int:
        if self.lag_ms is not None:
            return int(self.lag_ms)
        return int(LAG_MS_BY_CHAIN.get(chain, DEFAULT_LAG_MS))

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookback_s": self.lookback_s,
            "lag_ms": self.lag_ms,
            "fee_bps": self.fee_bps,
            "max_wait_ms": self.max_wait_ms,
            "min_trips_in": self.min_trips_in,
            "min_trips_out": self.min_trips_out,
            "alpha": self.alpha,
            "bootstrap_draws": self.bootstrap_draws,
            "net_cap": str(self.net_cap),
            "max_candidates": self.max_candidates,
            "prescreen": self.prescreen,
            "discovery_rows": self.discovery_rows,
            "discovery_min_swaps": self.discovery_min_swaps,
            "max_swaps_per_wallet": self.max_swaps_per_wallet,
            "min_median_buy_usd": str(self.min_median_buy_usd),
            "cotime_ms": self.cotime_ms,
            "cotime_min_events": self.cotime_min_events,
            "stale_exit": self.stale_exit,
            "dead_after_ms": self.dead_after_ms,
            "split_samples": self.split_samples,
            "require_chain_persistence": self.require_chain_persistence,
            "seed": self.seed,
        }


# --------------------------------------------------------------------------------------
# statistics: small, seeded, pure
# --------------------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def bootstrap_lower(
    values: Sequence[float], *, alpha: float = 0.05, draws: int = 1000, seed: int = 0
) -> float | None:
    """One-sided percentile-bootstrap lower bound on the mean. ``None`` below 3 values.

    The resampling unit is the round trip. That is right for one wallet's trips across
    different tokens and silent about a market-wide common factor -- a chain-wide rally
    lifts every trip together -- which is why the chain-level test in :func:`build`
    resamples whole WALLETS instead (:func:`cluster_bootstrap_diff`).
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 3:
        return None
    rng = random.Random(seed)
    means = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(max(draws, 100)))
    return means[int(alpha * len(means))]


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p). Exact; n is a few hundred at most here."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    p = min(max(float(p), 0.0), 1.0)
    return float(sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def _pooled(groups: Sequence[Sequence[float]]) -> float | None:
    total = sum(len(g) for g in groups)
    return sum(sum(g) for g in groups) / total if total else None


def cluster_bootstrap_diff(
    a: Sequence[Sequence[float]],
    b: Sequence[Sequence[float]],
    *,
    alpha: float = 0.05,
    draws: int = 1000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Two-sided ``1 - 2*alpha`` interval on pooled-trip mean(a) - mean(b).

    Each element of ``a`` and ``b`` is ONE wallet's trips. Wallets are resampled with
    replacement inside each group and their trips pooled, so one prolific wallet cannot
    pose as many independent observations. ``None`` when either side has under 3 wallets.
    """
    ga = [list(map(float, g)) for g in a if g]
    gb = [list(map(float, g)) for g in b if g]
    if len(ga) < 3 or len(gb) < 3:
        return None
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(max(draws, 100)):
        sa = [ga[rng.randrange(len(ga))] for _ in range(len(ga))]
        sb = [gb[rng.randrange(len(gb))] for _ in range(len(gb))]
        ma, mb = _pooled(sa), _pooled(sb)
        if ma is not None and mb is not None:
            diffs.append(ma - mb)
    if not diffs:
        return None
    diffs.sort()
    lo = diffs[int(alpha * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int((1 - alpha) * len(diffs)))]
    return lo, hi


# --------------------------------------------------------------------------------------
# the tape, read in bounded pieces
# --------------------------------------------------------------------------------------


def _row_at_or_after(conn: sqlite3.Connection, row_id: int) -> dict[str, Any] | None:
    return fetch_one(
        conn, "SELECT id, chain, ts_ms FROM swaps WHERE id >= ? ORDER BY id LIMIT 1", (int(row_id),)
    )


def _first_id_at(conn: sqlite3.Connection, ts_ms: int, lo_id: int, hi_id: int) -> int:
    """Smallest id whose ``ts_ms`` is at or after ``ts_ms``, by bisection on the key.

    Ids are assigned in ingest order and ingest is roughly chronological, so this is a
    bisection on a nearly sorted column: off by the ingest lag of a late source (minutes),
    which is immaterial to a median over days. Never a scan.
    """
    a, b = int(lo_id), int(hi_id)
    while a < b:
        mid = (a + b) // 2
        row = _row_at_or_after(conn, mid)
        if row is None:
            b = mid
        elif int(row["ts_ms"]) < ts_ms:
            a = int(row["id"]) + 1
        else:
            b = mid
    return a


def estimate_median_swap_ms(
    conn: sqlite3.Connection, chain: Chain, lo_ms: int, hi_ms: int, *, samples: int = 2000,
    min_samples: int = 30,
) -> tuple[int, int] | None:
    """``(median swap ts, samples used)`` for ``chain`` inside ``[lo_ms, hi_ms]``.

    ``copytrade.persistence`` finds the median with ``ORDER BY ts_ms ... OFFSET COUNT/2``,
    which sorts the chain's whole tape (no ``(chain, ts_ms)`` index exists) and on the box
    is most of that job's several minutes. Here ``samples`` evenly spaced primary-key
    reads across the window's id range estimate the same quantile; with 2,000 samples the
    rank error is about 1% of the tape, immaterial to where a multi-day window is cut.
    ``None`` when the chain has fewer than ``min_samples`` sampled rows in the window.
    """
    top = fetch_one(conn, "SELECT max(id) AS m FROM swaps")
    if not top or top["m"] is None:
        return None
    hi_id = int(top["m"])
    bottom = fetch_one(conn, "SELECT min(id) AS m FROM swaps")
    lo_id = int(bottom["m"]) if bottom and bottom["m"] is not None else 1
    start = _first_id_at(conn, int(lo_ms), lo_id, hi_id)
    if start > hi_id:
        return None
    span = hi_id - start
    n = max(1, int(samples))
    seen: list[int] = []
    last_id = -1
    for k in range(n):
        target = start + (span * k) // max(1, n - 1) if n > 1 else start
        row = _row_at_or_after(conn, target)
        if row is None or int(row["id"]) == last_id:
            continue
        last_id = int(row["id"])
        ts = int(row["ts_ms"])
        if str(row["chain"]) == chain.value and lo_ms <= ts <= hi_ms:
            seen.append(ts)
    if len(seen) < min_samples:
        return None
    seen.sort()
    return seen[len(seen) // 2], len(seen)


def _recent_active(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    rows: int,
    min_swaps: int,
    max_swaps: int,
    limit: int,
    seed: str,
) -> list[str]:
    """A seeded random sample of wallets active in the newest ``rows`` swaps of the tape.

    Every wallet in the tape is a candidate -- never only the ones our own scoring liked
    (``copytrade``'s selection-bias note). The sample comes from the NEWEST rows because
    that is the only part of ``swaps`` a scan can afford: a GROUP BY walking
    ``idx_swaps_wallet`` over the chain's partition took 56.8 s for 500 robinhood wallets on
    2026-10-02 and was killed at 290 s for the whole partition, while the newest 200,000
    rows by primary key read in 0.37 s because they are in the page cache.

    Conditioning on RECENT ACTIVITY is not conditioning on recent RETURNS: nothing here
    looks at a price. It does narrow the population to wallets still trading, which is the
    population a forward cohort can act on anyway, and the out-of-sample test already
    requires trips after the split. ``max_swaps`` drops bots and routers before they cost a
    read; the random draw (not "most active first") keeps hyperactivity from being the
    selection rule.
    """
    return _recent_pool(
        conn, chain, rows=rows, min_swaps=min_swaps, max_swaps=max_swaps, limit=limit, seed=seed
    )[0]


def _recent_pool(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    rows: int,
    min_swaps: int,
    max_swaps: int,
    limit: int,
    seed: str,
) -> tuple[list[str], dict[str, int]]:
    """:func:`_recent_active` plus the counts the wallet funnel reports.

    ``seen``: distinct wallets on ``chain`` in the slice. ``active``: of those, the ones
    with ``min_swaps..max_swaps`` swaps -- the population the draw samples from. Same one
    GROUP BY over the same primary-key range; the band is applied here rather than in a
    HAVING so the population it was cut from can be counted.
    """
    top = fetch_one(conn, "SELECT max(id) AS m FROM swaps")
    if not top or top["m"] is None:
        return [], {"seen": 0, "active": 0}
    hi_id = int(top["m"])
    found = fetch_all(
        conn,
        "SELECT wallet, COUNT(*) AS n FROM swaps WHERE id > ? AND id <= ? AND chain = ? "
        "AND token != '' GROUP BY wallet",
        (hi_id - int(rows), hi_id, chain.value),
    )
    seen = 0
    wallets: list[str] = []
    for r in found:
        if not r["wallet"]:
            continue
        seen += 1
        if int(min_swaps) <= int(r["n"]) <= int(max_swaps):
            wallets.append(str(r["wallet"]))
    wallets.sort()
    random.Random(seed).shuffle(wallets)
    return wallets[: int(limit)], {"seen": seen, "active": len(wallets)}


def _history_floor_met(
    conn: sqlite3.Connection, chain: Chain, wallet: str, lo_ms: int, hi_ms: int, need: int
) -> bool:
    """Whether ``wallet`` has at least ``need`` swaps of any kind in ``[lo_ms, hi_ms)``.

    Index-only (``idx_swaps_wallet`` is ``(chain, wallet, ts_ms)``) and stops at ``need``
    rows, so a wallet with a long history costs no more than one with a short one. Counts
    ``token = ''`` and quote-asset rows too, which only ever over-counts: as a test of a
    NECESSARY condition it can let an ineligible wallet through, never keep an eligible
    one out.
    """
    if need <= 0:
        return True
    row = fetch_one(
        conn,
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM swaps INDEXED BY idx_swaps_wallet "
        "WHERE chain = ? AND wallet = ? AND ts_ms >= ? AND ts_ms < ? LIMIT ?)",
        (chain.value, wallet, int(lo_ms), int(hi_ms), int(need)),
    )
    return bool(row) and int(row["n"]) >= int(need)


@dataclass
class _Tape:
    """One wallet's window of the tape: its episodes and its buys."""

    episodes: list[tuple[str, int, int]]
    buys: list[tuple[str, int, Decimal | None]]

    @property
    def median_buy_usd(self) -> Decimal | None:
        """Median of the PRICED buys. ``None`` when none was priced -- never 0."""
        vals = sorted(u for _, _, u in self.buys if u is not None and u > 0)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _episodes(
    conn: sqlite3.Connection, chain: Chain, wallet: str, lo_ms: int, hi_ms: int, max_rows: int
) -> _Tape | None:
    """``copytrade.round_trips`` inside a window, or ``None`` past ``max_rows`` swaps.

    Same episode shape: open on a buy with nothing open, close on the first sell after it.
    Bounded in time and rows, because ``round_trips`` reads a wallet's entire history and a
    router's is millions of rows. The buys ride along for the size floor and co-timing.
    """
    rows = fetch_all(
        conn,
        "SELECT token, ts_ms, side, usd_value FROM swaps INDEXED BY idx_swaps_wallet "
        "WHERE chain = ? AND wallet = ? AND ts_ms >= ? AND ts_ms <= ? AND token != '' "
        "ORDER BY ts_ms ASC, id ASC LIMIT ?",
        (chain.value, wallet, int(lo_ms), int(hi_ms), int(max_rows) + 1),
    )
    if len(rows) > int(max_rows):
        return None
    open_buy: dict[str, int] = {}
    out: list[tuple[str, int, int]] = []
    buys: list[tuple[str, int, Decimal | None]] = []
    for row in rows:
        token, ts, side = str(row["token"]), int(row["ts_ms"]), str(row["side"])
        if is_quote_asset(chain, token):
            continue
        if side == "buy":
            open_buy.setdefault(token, ts)
            try:
                usd = Decimal(str(row["usd_value"])) if row["usd_value"] not in (None, "") else None
            except Exception:  # noqa: BLE001 - an unreadable value is an unpriced buy
                usd = None
            buys.append((token, ts, usd))
        elif side == "sell" and token in open_buy:
            out.append((token, open_buy.pop(token), ts))
    return _Tape(episodes=out, buys=buys)


def cotime_clusters(
    buys: dict[str, Sequence[tuple[str, int]]], *, within_ms: int, min_events: int
) -> dict[str, str]:
    """``{wallet: cluster id}``: wallets joined when they bought the same token within
    ``within_ms`` of each other at least ``min_events`` times. Union-find; a wallet with no
    such partner is its own cluster. The id is the cluster's smallest address, so it is
    stable across runs that see the same members.
    """
    parent = {w: w for w in buys}

    def find(w: str) -> str:
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    by_token: dict[str, list[tuple[int, str]]] = {}
    for wallet, items in buys.items():
        for token, ts in items:
            by_token.setdefault(token, []).append((int(ts), wallet))
    pairs: dict[tuple[str, str], int] = {}
    for items in by_token.values():
        items.sort()
        for i, (ts, w) in enumerate(items):
            for ts2, w2 in items[i + 1:]:
                if ts2 - ts > within_ms:
                    break
                if w2 != w:
                    key = (w, w2) if w < w2 else (w2, w)
                    pairs[key] = pairs.get(key, 0) + 1
    for (a, b), n in pairs.items():
        if n >= min_events:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {w: find(w) for w in buys}


def _last_print(
    conn: sqlite3.Connection, chain: Chain, token: str, after_ms: int, at_ms: int
) -> tuple[Decimal, int] | None:
    """The newest priced trade in ``[after_ms, at_ms]``: the pool or curve as it stood."""
    row = fetch_one(
        conn,
        "SELECT ts_ms, price_usd FROM swaps WHERE chain = ? AND token = ? AND ts_ms >= ? "
        "AND ts_ms <= ? AND price_usd IS NOT NULL AND price_usd != '' "
        "ORDER BY ts_ms DESC LIMIT 1",
        (chain.value, token, int(after_ms), int(at_ms)),
    )
    if not row:
        return None
    try:
        price = Decimal(str(row["price_usd"]))
    except Exception:  # noqa: BLE001 - a malformed price is no price
        return None
    return (price, int(row["ts_ms"])) if price > 0 else None


#: How an exit with no print inside the wait window is priced. See ``_price_trips``.
STALE_EXITS: frozenset[str] = frozenset({"last_print", "loss", "drop"})


def _price_trips(
    conn: sqlite3.Connection,
    chain: Chain,
    trips: Iterable[tuple[str, int, int]],
    *,
    lag_ms: int,
    fee: Decimal,
    until_ms: int,
    max_wait_ms: int,
    stale_exit: str = "last_print",
    dead_after_ms: int = 3_600_000,
    counts: dict[str, int] | None = None,
) -> list[Decimal]:
    """Net copy return of each fillable trip, priced as ``copytrade.evaluate_wallet`` does.

    Same entry (first print at/after their buy + lag, within ``max_wait_ms``; none means we
    never bought and the trip is dropped) and the same exit when the tape has a print
    within ``max_wait_ms`` of their sell + lag. The one deliberate difference is the exit
    when it does NOT. ``evaluate_wallet`` drops that trip -- but we would already hold the
    token by then, and a leader who dumps into a book nobody trades afterwards is exactly
    the copy that loses most. Dropping it is how a ranker invents an edge. ``stale_exit``:

    * ``"last_print"`` (default): sell into the pool or curve as it stood at our exit --
      the newest print between our entry and that moment, usually the leader's own sell.
      A curve or a pool still quotes after the last trade; the price is the one it left.
    * ``"loss"``: -100%, once the tape has been watched ``dead_after_ms`` with no print
      (unobserved, and dropped, when the period ends sooner). The pessimistic bound.
    * ``"drop"``: exact ``evaluate_wallet`` parity. The optimistic bound.

    Every path is counted in ``counts``. Nothing ever reads a print after ``until_ms``.
    """
    if stale_exit not in STALE_EXITS:
        raise ValueError(f"stale_exit must be one of {sorted(STALE_EXITS)}, not {stale_exit!r}")
    nets: list[Decimal] = []
    tally = counts if counts is not None else {}

    def bump(key: str) -> None:
        tally[key] = tally.get(key, 0) + 1

    for token, buy_ms, sell_ms in trips:
        entry = next_print(conn, chain, token, buy_ms + lag_ms, max_wait_ms=max_wait_ms, until_ms=until_ms)
        if entry is None:
            bump("no_entry_print")
            continue
        at = max(sell_ms + lag_ms, entry[1])
        exit_ = next_print(conn, chain, token, at, max_wait_ms=max_wait_ms, until_ms=until_ms)
        if exit_ is None:
            if stale_exit == "last_print":
                exit_ = _last_print(conn, chain, token, entry[1], min(at, until_ms)) or entry
                bump("exit_last_print")
            elif stale_exit == "loss":
                if until_ms - at < dead_after_ms:
                    bump("exit_unobserved")
                    continue
                bump("exit_dead")
                nets.append(Decimal(-1))
                continue
            else:
                bump("exit_dropped")
                continue
        else:
            bump("filled")
        nets.append(exit_[0] / entry[0] - Decimal(1) - fee)
    return nets


# --------------------------------------------------------------------------------------
# the cohort
# --------------------------------------------------------------------------------------


@dataclass
class WalletEvidence:
    """One wallet's copy record on each side of the split. Nets are fractions (0.1 = +10%)."""

    wallet: str
    source: str
    trips_in: int = 0
    trips_out: int = 0
    nets_in: list[Decimal] = field(default_factory=list)
    nets_out: list[Decimal] = field(default_factory=list)
    oos_lower: float | None = None
    median_buy_usd: Decimal | None = None
    cluster: str | None = None
    buys: list[tuple[str, int]] = field(default_factory=list, repr=False)

    def capped(self, nets: Sequence[Decimal], cap: Decimal) -> list[float]:
        return [float(min(n, cap)) for n in nets]

    @property
    def mean_in(self) -> float | None:
        return _mean([float(n) for n in self.nets_in])

    @property
    def mean_out(self) -> float | None:
        return _mean([float(n) for n in self.nets_out])

    def summary(self, cap: Decimal) -> dict[str, Any]:
        cin, cout = self.capped(self.nets_in, cap), self.capped(self.nets_out, cap)
        return {
            "source": self.source,
            "trips_in": self.trips_in,
            "fillable_in": len(self.nets_in),
            "mean_in_pct": _pct(_mean(cin)),
            "trips_out": self.trips_out,
            "fillable_out": len(self.nets_out),
            "mean_out_pct": _pct(_mean(cout)),
            "median_out_pct": _pct(_median(cout)),
            "mean_out_raw_pct": _pct(self.mean_out),
            "win_rate_out": round(sum(1 for n in self.nets_out if n > 0) / len(self.nets_out), 4)
            if self.nets_out else None,
            "oos_lower_pct": _pct(self.oos_lower),
            "median_buy_usd": None if self.median_buy_usd is None else str(round(self.median_buy_usd, 2)),
            "cotime_cluster": self.cluster,
        }


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100.0, 3)


@dataclass
class ProvenReport:
    chain: Chain
    as_of_ms: int
    config: ProvenConfig
    lo_ms: int = 0
    split_ms: int | None = None
    hi_ms: int = 0
    split_samples: int = 0
    lag_ms: int = 0
    candidates: dict[str, int] = field(default_factory=dict)
    #: Distinct wallets on the chain in the discovery slice, and how many of them fell in
    #: the ``discovery_min_swaps..max_swaps_per_wallet`` band the draw samples from.
    seen: int = 0
    active: int = 0
    #: Drawn wallets skipped by the prescreen: under ``2 * min_trips_in`` swaps in sample.
    prescreen_failed: int = 0
    evaluated: int = 0
    too_active: int = 0
    #: Read in full, and still under ``min_trips_in`` round trips in sample.
    few_trips_in: int = 0
    below_buy_floor: int = 0
    #: Enough round trips, but under ``min_trips_in`` of them fillable at our lag.
    few_fillable_in: int = 0
    trip_counts_in: dict[str, int] = field(default_factory=dict)
    trip_counts_out: dict[str, int] = field(default_factory=dict)
    eligible: int = 0
    in_winners: int = 0
    in_losers: int = 0
    winners_oos: dict[str, Any] = field(default_factory=dict)
    losers_oos: dict[str, Any] = field(default_factory=dict)
    population_oos: dict[str, Any] = field(default_factory=dict)
    diff_ci_pct: tuple[float, float] | None = None
    persists: bool = False
    tested: int = 0
    passed: int = 0
    null_tested: int = 0
    null_passed: int = 0
    expected_false_positives: float | None = None
    chance_p: float | None = None
    proven: list[WalletEvidence] = field(default_factory=list)
    clusters: int = 0
    control: list[WalletEvidence] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def funnel(self) -> dict[str, Any]:
        """seen -> active -> drawn -> enough history -> eligible -> tested -> proven.

        Every stage is a count of wallets, each a subset of the one before it (``drawn``
        also includes the small previous-proven and trusted-copy sources). The owner
        watches this to see whether "more wallets" is growing, and the stage with the
        steepest drop is where the next expansion belongs.
        """
        drawn = sum(self.candidates.values())
        return {
            "seen": self.seen,
            "active": self.active,
            "drawn": drawn,
            "prescreen_failed": self.prescreen_failed,
            "evaluated": self.evaluated,
            "too_active": self.too_active,
            "few_trips_in": self.few_trips_in,
            "below_buy_floor": self.below_buy_floor,
            "few_fillable_in": self.few_fillable_in,
            "eligible": self.eligible,
            "in_winners": self.in_winners,
            "tested": self.tested,
            "passed": self.passed,
            "proven": len(self.proven),
            "clusters": self.clusters,
            "truncated": self.truncated,
        }

    def as_dict(self) -> dict[str, Any]:
        cap = self.config.net_cap
        return {
            "chain": self.chain.value,
            "as_of_ms": self.as_of_ms,
            "lo_ms": self.lo_ms,
            "split_ms": self.split_ms,
            "split_basis": "median_swap_sampled",
            "split_samples": self.split_samples,
            "hi_ms": self.hi_ms,
            "lag_ms": self.lag_ms,
            "candidates": dict(self.candidates),
            "seen": self.seen,
            "active": self.active,
            "prescreen_failed": self.prescreen_failed,
            "evaluated": self.evaluated,
            "too_active": self.too_active,
            "few_trips_in": self.few_trips_in,
            "below_buy_floor": self.below_buy_floor,
            "few_fillable_in": self.few_fillable_in,
            "trip_counts_in": dict(self.trip_counts_in),
            "trip_counts_out": dict(self.trip_counts_out),
            "eligible": self.eligible,
            "in_winners": self.in_winners,
            "in_losers": self.in_losers,
            "winners_oos": self.winners_oos,
            "losers_oos": self.losers_oos,
            "population_oos": self.population_oos,
            "diff_ci_pct": list(self.diff_ci_pct) if self.diff_ci_pct else None,
            "persists": self.persists,
            "tested": self.tested,
            "passed": self.passed,
            "null_tested": self.null_tested,
            "null_passed": self.null_passed,
            "expected_false_positives": self.expected_false_positives,
            "chance_p": self.chance_p,
            "proven_n": len(self.proven),
            "proven_clusters": self.clusters,
            "control_n": len(self.control),
            "proven": {w.wallet: w.summary(cap) for w in self.proven},
            "truncated": self.truncated,
            "error": self.error,
            "notes": list(self.notes),
            "config": self.config.as_dict(),
        }


def cohort_id_for(chain: Chain, frozen_ms: int) -> str:
    return f"{COHORT_PREFIX}:{chain.value}:{int(frozen_ms)}"


def _group_stats(groups: Sequence[Sequence[float]]) -> dict[str, Any]:
    trips = [x for g in groups for x in g]
    return {
        "wallets": sum(1 for g in groups if g),
        "trips": len(trips),
        "pooled_mean_pct": _pct(_mean(trips)),
        "median_pct": _pct(_median(trips)),
        "win_rate": round(sum(1 for x in trips if x > 0) / len(trips), 4) if trips else None,
        "per_wallet_mean_pct": _pct(_mean([_mean(list(g)) for g in groups if g])),  # copytrade's unit
    }


def build(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    config: ProvenConfig | None = None,
    as_of_ms: int | None = None,
    deadline_monotonic: float | None = None,
    split_ms: int | None = None,
) -> ProvenReport:
    """Measure every candidate on both sides of the split and choose the proven set.

    ``split_ms`` pins the split instead of estimating the median swap (tests, and
    re-running a past freeze exactly). Read-only. ``deadline_monotonic`` stops evaluating NEW candidates; whatever was
    evaluated is still scored and the report says ``truncated``. The statistics are only
    ever over wallets that were fully evaluated, so a short run is smaller, not biased by
    a half-read wallet.
    """
    cfg = config or ProvenConfig()
    now = int(as_of_ms if as_of_ms is not None else _now_ms())
    report = ProvenReport(chain=chain, as_of_ms=now, config=cfg)
    report.hi_ms = now
    report.lo_ms = now - int(cfg.lookback_s) * 1000
    report.lag_ms = cfg.lag_for(chain)
    fee = Decimal(int(cfg.fee_bps)) / Decimal(10_000)

    if split_ms is not None:  # a caller's split, as copytrade.persistence allows
        split: tuple[int, int] | None = (int(split_ms), 0)
        report.notes.append("split given by the caller, not estimated")
    else:
        split = estimate_median_swap_ms(
            conn, chain, report.lo_ms, report.hi_ms, samples=cfg.split_samples
        )
    if split is None:
        report.error = "too few swaps on this chain inside the look-back to find a median"
        return report
    report.split_ms, report.split_samples = split

    # ---- candidates: previous cohort (re-validated, no tenure), the curated cohort, and
    # an unbiased slice of the tape. Order matters only under a deadline: the first two
    # are small and are always evaluated.
    sources: dict[str, str] = {}
    previous = latest_cohort(conn, chain)
    for w in sorted(previous.members if previous else ()):
        sources.setdefault(w, "previous_proven")
    for r in fetch_all(
        conn, "SELECT address FROM wallets WHERE cohort = 'trusted_copy' AND chain = ?", (chain.value,)
    ):
        sources.setdefault(str(r["address"]), "trusted_copy")
    drawn, pool = _recent_pool(
        conn, chain,
        rows=int(cfg.discovery_rows), min_swaps=int(cfg.discovery_min_swaps),
        max_swaps=int(cfg.max_swaps_per_wallet), limit=int(cfg.max_candidates),
        seed=f"{cfg.seed}:{chain.value}:{now // 86_400_000}",
    )
    report.seen, report.active = pool["seen"], pool["active"]
    for w in drawn:
        sources.setdefault(w, "tape")
    for src in sources.values():
        report.candidates[src] = report.candidates.get(src, 0) + 1

    # ---- evaluate
    evidence: list[WalletEvidence] = []
    split_ms = int(report.split_ms)
    need_in = 2 * int(cfg.min_trips_in) if cfg.prescreen else 0
    cap = cfg.net_cap
    for wallet, src in sources.items():
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            report.truncated = True
            break
        report.evaluated += 1
        if need_in and not _history_floor_met(conn, chain, wallet, report.lo_ms, split_ms, need_in):
            report.prescreen_failed += 1
            continue
        tape = _episodes(conn, chain, wallet, report.lo_ms, report.hi_ms, int(cfg.max_swaps_per_wallet))
        if tape is None:
            report.too_active += 1
            continue
        episodes = tape.episodes
        # A trip that straddles the split is on neither side: scoring it in sample leaks
        # the later half's prices into the rank, scoring it out of sample leaks the rank.
        ins = [t for t in episodes if t[2] < split_ms]
        outs = [t for t in episodes if t[1] >= split_ms]
        ev = WalletEvidence(wallet=wallet, source=src, trips_in=len(ins), trips_out=len(outs))
        if len(ins) < cfg.min_trips_in:
            report.few_trips_in += 1
            continue
        # Conviction before price: a dust buyer is not copyable at our size and its
        # agreement with other dust buyers is not several opinions (ProvenConfig note).
        ev.median_buy_usd = tape.median_buy_usd
        if cfg.min_median_buy_usd > 0 and (
            ev.median_buy_usd is None or ev.median_buy_usd < cfg.min_median_buy_usd
        ):
            report.below_buy_floor += 1
            continue
        ev.buys = [(t, ts) for t, ts, _ in tape.buys]
        ev.nets_in = _price_trips(
            conn, chain, ins, lag_ms=report.lag_ms, fee=fee, until_ms=split_ms,
            max_wait_ms=cfg.max_wait_ms, stale_exit=cfg.stale_exit, dead_after_ms=cfg.dead_after_ms,
            counts=report.trip_counts_in,
        )
        if len(ev.nets_in) < cfg.min_trips_in:
            report.few_fillable_in += 1
            continue
        if (_mean(ev.capped(ev.nets_in, cap)) or 0.0) <= 0:
            # An in-sample loser can never be proven, and only the proven need their buys
            # (co-timing clusters). Dropped here so a run that scores thousands of wallets
            # does not hold every one's buy list until the end.
            ev.buys = []
        ev.nets_out = _price_trips(
            conn, chain, outs, lag_ms=report.lag_ms, fee=fee, until_ms=report.hi_ms,
            max_wait_ms=cfg.max_wait_ms, stale_exit=cfg.stale_exit, dead_after_ms=cfg.dead_after_ms,
            counts=report.trip_counts_out,
        )
        evidence.append(ev)

    report.eligible = len(evidence)
    winners = [e for e in evidence if (_mean(e.capped(e.nets_in, cap)) or 0.0) > 0]
    losers = [e for e in evidence if (_mean(e.capped(e.nets_in, cap)) or 0.0) <= 0]
    report.in_winners, report.in_losers = len(winners), len(losers)

    def oos(group: Sequence[WalletEvidence]) -> list[list[float]]:
        return [e.capped(e.nets_out, cap) for e in group if e.nets_out]

    report.winners_oos = _group_stats(oos(winners))
    report.losers_oos = _group_stats(oos(losers))
    report.population_oos = _group_stats(oos(evidence))
    diff = cluster_bootstrap_diff(
        oos(winners), oos(losers), alpha=cfg.alpha, draws=cfg.bootstrap_draws, seed=cfg.seed
    )
    report.diff_ci_pct = (round(diff[0] * 100, 3), round(diff[1] * 100, 3)) if diff else None
    w_mean = report.winners_oos.get("pooled_mean_pct")
    p_mean = report.population_oos.get("pooled_mean_pct")
    report.persists = bool(
        diff is not None and diff[0] > 0 and w_mean is not None and p_mean is not None and w_mean > p_mean
    )

    # ---- per-wallet OOS test, on the winners and (as the null) on the losers
    def passes(e: WalletEvidence) -> bool:
        if len(e.nets_out) < cfg.min_trips_out:
            return False
        vals = e.capped(e.nets_out, cap)
        if (_mean(vals) or 0.0) <= 0:
            e.oos_lower = None
            return False
        e.oos_lower = bootstrap_lower(vals, alpha=cfg.alpha, draws=cfg.bootstrap_draws, seed=cfg.seed)
        return e.oos_lower is not None and e.oos_lower > 0

    tested = [e for e in winners if len(e.nets_out) >= cfg.min_trips_out]
    passed = [e for e in tested if passes(e)]
    null_tested = [e for e in losers if len(e.nets_out) >= cfg.min_trips_out]
    null_passed = [e for e in null_tested if passes(e)]
    report.tested, report.passed = len(tested), len(passed)
    report.null_tested, report.null_passed = len(null_tested), len(null_passed)
    if null_tested:
        report.expected_false_positives = round(len(null_passed) / len(null_tested) * len(tested), 2)
    # How likely is this many passes among the in-sample winners if the OOS test passed
    # wallets only by chance, at the in-sample LOSERS' pass rate? Smoothed (+1/+2) so a
    # null group that happened to pass nobody does not make one pass look certain.
    p0 = (len(null_passed) + 1) / (len(null_tested) + 2)
    report.chance_p = round(binom_tail(len(passed), len(tested), p0), 6) if tested else None

    publish = True
    if cfg.require_chain_persistence and not report.persists:
        publish = False
        report.notes.append(
            "EMPTY: in-sample winners did not out-earn in-sample losers out of sample with a "
            f"wallet-clustered interval excluding zero (diff CI {report.diff_ci_pct}); any "
            "wallet that passed alone is indistinguishable from the chance passes"
        )
    elif passed and (report.chance_p is None or report.chance_p >= cfg.alpha):
        publish = False
        report.notes.append(
            f"EMPTY: {len(passed)} of {len(tested)} in-sample winners passed; at the in-sample "
            f"losers' pass rate ({len(null_passed)}/{len(null_tested)}, smoothed {p0:.4f}) that "
            f"many happen by chance with p={report.chance_p} >= {cfg.alpha}"
        )
    if publish:
        report.proven = sorted(passed, key=lambda e: (-(e.oos_lower or 0.0), e.wallet))
        clusters = cotime_clusters(
            {e.wallet: e.buys for e in report.proven},
            within_ms=int(cfg.cotime_ms), min_events=int(cfg.cotime_min_events),
        )
        for e in report.proven:
            e.cluster = clusters.get(e.wallet, e.wallet)
        report.clusters = len(set(clusters.values()))
    if not report.proven and not any(n.startswith("EMPTY") for n in report.notes):
        report.notes.append("EMPTY: no wallet met both the in-sample and the out-of-sample bar")

    # ---- control arm: random eligible wallets, same size, never a proven one
    proven_ids = {e.wallet for e in report.proven}
    pool = sorted(e.wallet for e in evidence if e.wallet not in proven_ids)
    pick = random.Random(f"{cfg.seed}:control:{chain.value}:{now}")
    chosen = set(pick.sample(pool, min(len(pool), len(report.proven)))) if report.proven else set()
    report.control = [e for e in evidence if e.wallet in chosen]
    if report.truncated:
        report.notes.append(
            f"truncated by the deadline after {report.evaluated} of {len(sources)} candidates"
        )
    return report


def freeze(conn: sqlite3.Connection, report: ProvenReport) -> str | None:
    """Write the cohort -- including an EMPTY one -- and return its id.

    ``None`` only when the build errored before a split existed: that run measured nothing,
    so writing a freeze would claim an answer it does not have, and the previous cohort's
    age limit (``proven_members(max_age_s=...)``) retires it on schedule instead.
    """
    if report.error or report.split_ms is None:
        return None
    cohort_id = cohort_id_for(report.chain, report.as_of_ms)
    cap = report.config.net_cap
    summary = report.as_dict()
    summary.pop("proven", None)
    with tx(conn):
        conn.execute(
            "INSERT INTO wallet_cohort_freezes (cohort_id, frozen_ms, chain, graded_n, control_n, "
            "matched_on_json, unmatched_json, notes_json) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cohort_id) DO UPDATE SET graded_n=excluded.graded_n, "
            "control_n=excluded.control_n, notes_json=excluded.notes_json",
            (
                cohort_id,
                int(report.as_of_ms),
                report.chain.value,
                len(report.proven),
                len(report.control),
                jdump(["random draw from the same eligible pool (>= min_trips_in fillable in sample)"]),
                jdump([
                    "control is NOT matched on activity, token mix or launchpad: it is a random "
                    "eligible wallet, the baseline 'copy anyone active' would have got",
                    "copy model omits slippage and gas; returns are upper bounds",
                ]),
                # A list of STRINGS: validation.ControlArmReport.notes is list[str], and the
                # forward test reads this column straight into it.
                jdump([*report.notes, "proven-build " + jdump(summary)]),
            ),
        )
        for arm, members in ((TREATED_ARM, report.proven), (CONTROL_ARM, report.control)):
            for pair_id, ev in enumerate(members):
                conn.execute(
                    "INSERT OR IGNORE INTO wallet_cohorts (cohort_id, arm, chain, address, pair_id, "
                    "frozen_ms, grade, covariates_json) VALUES (?,?,?,?,?,?,?,?)",
                    (cohort_id, arm, report.chain.value, ev.wallet, pair_id, int(report.as_of_ms), "",
                     jdump(ev.summary(cap))),
                )
    _CACHE.clear()
    return cohort_id


# --------------------------------------------------------------------------------------
# reading the cohort (the lane and the scanner)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenCohort:
    cohort_id: str
    chain: Chain
    frozen_ms: int
    members: frozenset[str]
    #: ``{wallet: co-timing cluster}``; a wallet missing here is its own cluster.
    clusters: dict[str, str] = field(default_factory=dict, compare=False, hash=False)

    def cluster_count(self, wallets: Iterable[str]) -> int:
        """Distinct operators among ``wallets`` by the co-timing fingerprint."""
        return len({self.clusters.get(w, w) for w in wallets})

    def age_s(self, at_ms: int) -> float:
        return (int(at_ms) - self.frozen_ms) / 1000.0


def latest_cohort(
    conn: sqlite3.Connection, chain: Chain, *, at_ms: int | None = None
) -> ProvenCohort | None:
    """The newest proven freeze for ``chain`` -- possibly EMPTY -- or ``None`` if never frozen.

    ``at_ms`` bounds it to freezes made at or before that instant, so a replay of an old
    moment cannot count a cohort that was chosen later with data it could not have had.
    """
    sql = (
        "SELECT cohort_id, frozen_ms FROM wallet_cohort_freezes "
        "WHERE chain = ? AND cohort_id LIKE ?"
    )
    args: list[Any] = [chain.value, f"{COHORT_PREFIX}:{chain.value}:%"]
    if at_ms is not None:
        sql += " AND frozen_ms <= ?"
        args.append(int(at_ms))
    row = fetch_one(conn, sql + " ORDER BY frozen_ms DESC LIMIT 1", tuple(args))
    if not row:
        return None
    members = fetch_all(
        conn,
        "SELECT address, covariates_json FROM wallet_cohorts WHERE cohort_id = ? AND arm = ? AND chain = ?",
        (row["cohort_id"], TREATED_ARM, chain.value),
    )
    clusters: dict[str, str] = {}
    for m in members:
        cov = jload(m["covariates_json"], {}) or {}
        if cov.get("cotime_cluster"):
            clusters[str(m["address"])] = str(cov["cotime_cluster"])
    return ProvenCohort(
        cohort_id=str(row["cohort_id"]),
        chain=chain,
        frozen_ms=int(row["frozen_ms"]),
        members=frozenset(str(m["address"]) for m in members),
        clusters=clusters,
    )


#: ``(db key, chain) -> (expires_monotonic, cohort or None)``. The cohort changes when the
#: refresh job runs (hours apart); the lane asks on every scan (about once a second).
_CACHE: dict[tuple[str, str], tuple[float, ProvenCohort | None]] = {}
_CACHE_LOCK = threading.Lock()


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(row[2] or "") if row else ""
    except sqlite3.Error:
        path = ""
    return path or f"conn:{id(conn)}"


def proven_members(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    max_age_s: float,
    at_ms: int | None = None,
    ttl_s: float = 300.0,
) -> ProvenCohort | None:
    """The cohort the lane may use right now, or ``None``.

    ``None`` when no cohort was ever frozen for the chain AND when the newest one is older
    than ``max_age_s``: a refresh job that has stopped running must not leave the lane
    trading on a list chosen weeks ago. An EMPTY cohort is returned as such (``members``
    empty), which is a different answer from ``None`` and the lane reports it differently.
    """
    at = int(at_ms if at_ms is not None else _now_ms())
    key = (_db_key(conn), chain.value)
    now_mono = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None and hit[0] > now_mono:
        cohort = hit[1]
    else:
        try:
            cohort = latest_cohort(conn, chain)
        except sqlite3.Error as exc:
            log.warning("proven cohort unreadable for %s (%s)", chain.value, exc)
            cohort = None
        with _CACHE_LOCK:
            _CACHE[key] = (now_mono + max(0.0, float(ttl_s)), cohort)
    if cohort is not None and cohort.frozen_ms > at:
        # A replay of a moment before the newest freeze: never served from the cache.
        try:
            cohort = latest_cohort(conn, chain, at_ms=at)
        except sqlite3.Error:
            cohort = None
    if cohort is None:
        return None
    if cohort.age_s(at) > float(max_age_s):
        return None
    return cohort


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------------------
# job entry point
# --------------------------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    chains: Sequence[Chain],
    *,
    config: ProvenConfig | None = None,
    as_of_ms: int | None = None,
    deadline_monotonic: float | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build (and by default freeze) one cohort per chain, splitting the time left evenly.

    Returns a COMPACT summary per chain -- the funnel, the chain-level verdict, the cohort
    id and the strongest proven wallets -- because the scheduler stores a job result only
    while it fits ``scheduler._small``'s 2,000 characters. MEASURED 2026-10-03: the full
    two-chain report came to 3,700 characters, so the box's ``ops_runs`` row for every
    proven run read ``{"truncated": true}``. The full report is not lost: :func:`freeze`
    writes it into the freeze's ``notes_json``, which is what :func:`funnel_history` reads.
    """
    cfg = config or ProvenConfig()
    out: dict[str, Any] = {}
    todo = list(chains)
    for i, chain in enumerate(todo):
        chain_deadline = None
        if deadline_monotonic is not None:
            left = max(0.0, deadline_monotonic - time.monotonic())
            chain_deadline = time.monotonic() + left / max(1, len(todo) - i)
        started = time.monotonic()
        report = build(conn, chain, config=cfg, as_of_ms=as_of_ms, deadline_monotonic=chain_deadline)
        cohort_id = freeze(conn, report) if write else None
        out[chain.value] = compact_summary(report, cohort_id, time.monotonic() - started)
        log.info(
            "proven %s: %d proven of %d eligible (persists=%s, diff CI %s, cohort %s)",
            chain.value, len(report.proven), report.eligible, report.persists,
            report.diff_ci_pct, cohort_id,
        )
    return out


#: Proven wallets named in a job result, strongest out-of-sample lower bound first. The
#: full membership is in ``wallet_cohorts``; this only keeps the result under the cap.
COMPACT_WALLETS = 6


def compact_summary(report: ProvenReport, cohort_id: str | None, elapsed_s: float) -> dict[str, Any]:
    """One chain's job result: small enough for ``ops_runs``, enough to act on."""
    return {
        "cohort_id": cohort_id,
        "error": report.error,
        "persists": report.persists,
        "diff_ci_pct": list(report.diff_ci_pct) if report.diff_ci_pct else None,
        "winners_oos_pct": report.winners_oos.get("pooled_mean_pct"),
        "population_oos_pct": report.population_oos.get("pooled_mean_pct"),
        "chance_p": report.chance_p,
        "funnel": report.funnel(),
        "proven_wallets": [e.wallet for e in report.proven[:COMPACT_WALLETS]],
        "elapsed_s": round(float(elapsed_s), 1),
        "note": report.notes[0][:160] if report.notes else None,
    }


#: Funnel stages in the order they narrow. ``funnel_history`` fills the ones an older
#: freeze did not record with ``None`` -- never 0, which would read as "nobody".
FUNNEL_STAGES: tuple[str, ...] = (
    "seen", "active", "drawn", "evaluated", "eligible", "tested", "passed", "proven",
)


def funnel_history(
    conn: sqlite3.Connection, chain: Chain, *, limit: int = 7
) -> list[dict[str, Any]]:
    """The wallet funnel of the newest ``limit`` proven freezes for ``chain``, newest first.

    Read from the freeze itself (the ``proven-build`` summary :func:`freeze` writes into
    ``notes_json``), so it covers every run since the job existed, including the ones
    whose ``ops_runs`` result was truncated. Bounded: one ``LIMIT`` read of a table that
    gains two rows a day.
    """
    rows = fetch_all(
        conn,
        "SELECT cohort_id, frozen_ms, graded_n, notes_json FROM wallet_cohort_freezes "
        "WHERE chain = ? AND cohort_id LIKE ? ORDER BY frozen_ms DESC LIMIT ?",
        (chain.value, f"{COHORT_PREFIX}:{chain.value}:%", int(limit)),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        summary: dict[str, Any] = {}
        for note in jload(r["notes_json"], []) or []:
            if isinstance(note, str) and note.startswith("proven-build "):
                summary = jload(note[len("proven-build "):], {}) or {}
                break
        candidates = summary.get("candidates")
        drawn = sum(int(v) for v in candidates.values()) if isinstance(candidates, dict) else None
        row: dict[str, Any] = {
            "cohort_id": str(r["cohort_id"]),
            "frozen_ms": int(r["frozen_ms"]),
            "seen": summary.get("seen"),
            "active": summary.get("active"),
            "drawn": drawn,
            "evaluated": summary.get("evaluated"),
            "eligible": summary.get("eligible"),
            "tested": summary.get("tested"),
            "passed": summary.get("passed"),
            "proven": int(r["graded_n"] or 0),
            "persists": summary.get("persists"),
            "truncated": summary.get("truncated"),
        }
        out.append(row)
    return out


def parse_chains(raw: Any) -> list[Chain]:
    names = raw if isinstance(raw, (list, tuple)) else str(raw or "").split(",")
    return [Chain(str(n).strip()) for n in names if str(n).strip()]


def config_from_params(params: dict[str, Any] | None) -> ProvenConfig:
    """A :class:`ProvenConfig` from schedule.yaml params. An unknown key fails loudly."""
    p = dict(params or {})
    p.pop("chains", None)
    known = set(ProvenConfig.__dataclass_fields__)
    unknown = sorted(set(p) - known)
    if unknown:
        raise ValueError(f"unknown proven-wallet params: {unknown}")
    for key in ("net_cap", "min_median_buy_usd"):
        if key in p:
            p[key] = Decimal(str(p[key]))
    return ProvenConfig(**p)
