"""Phase 4: can we tell whether any of this works, and if not, when could we?

This module exists because the honest answer to "does the agent have edge" is going to be
"we cannot tell yet" for a long time, and an underpowered number read as a result is worse
than no number at all. So the headline output here is not a verdict on the strategy. It is
a verdict on our *ability to reach a verdict*: how many closed trades we have, how many the
arithmetic needs, and how long that would take at the rate we are actually closing them.

Four things follow from `docs/research/13-validation-and-copytrading-2026.md` and shape
everything below.

**Refusing to conclude is a first-class outcome.** :class:`Verdict` has four values, not
two. ``UNDERPOWERED`` means we looked and the data cannot answer; ``FAIL`` means we looked
and the answer was no. Collapsing them loses the distinction between "the strategy is
wrong" and "we have not measured it", which demand opposite responses. ``BLOCKED`` means an
earlier gate did not pass so this one was never reached — it is not a pass and must never
be rendered as one.

**A missing input is never a skip.** Every criterion carries its own verdict, and a
criterion whose input is absent is ``UNDERPOWERED``, which propagates up: a gate passes only
when every one of its criteria passed. There is no code path where absent data produces a
green light.

**The trial count comes from the registry, never from the caller.** The deflated Sharpe
divides by *N*, the honest count of every configuration ever run, and *N* is the number
people fudge without meaning to. :func:`honest_trials` reads
:mod:`kaiba.learning.registry` and the ``experiments`` table and takes the larger. No
public function here accepts a trial count, because a caller-supplied one could only ever
be used to lower the bar.

**The sample may never arrive, and the report has to be able to say so.** A 30-40% hit rate
with a fat right tail needs roughly 3,500-5,500 closed trades for a bare t = 1.96 and
10,000-25,000 after deflation (research B3, profiles C and D). The venue's rules change
every couple of months. :data:`REGIME_WEEKS` is eight weeks — *a working figure, not a
measured one* — and when the projected time to significance exceeds it,
:class:`PowerReport` says in as many words that a stationary sample is not reachable. That
sentence is the single most decision-relevant output of this module: if it is permanent,
the directional book is not the business.

What is approximated, stated plainly, because a harness that overstates its own rigour is
worse than none:

* The **deflated significance threshold** is ``E[max z](N) + z(80% power)``, using the
  Bailey & Lopez de Prado expected maximum under the null. The research's own table used
  3.29 at N = 100 and 4.06 at N = 1,000; this rule gives 3.37 and 4.10, i.e. the same idea
  and marginally stricter. Marginally stricter is the correct direction to be wrong in.
* **Sample-size arithmetic assumes i.i.d. trades.** It is not. 500 trades across 12 tokens
  in a week is not 500 observations. :func:`block_bootstrap_t` blocks by day to take some
  of that back, and the research's own expectation — unrebutted — is that clustering
  inflates the calendar requirement by a further 2-5x on top of every number here.
* **The control arm's forward edge is realised-only**, reconstructed by
  :mod:`kaiba.intelligence.pnl` from swaps we observed. A wallet that is bagholding looks
  flat rather than down. That is the exact accounting artefact that makes 73% of pump.fun
  wallets appear profitable, and it biases *both* arms, so the contrast survives it better
  than either level does.
* **PBO needs a trial matrix** (T periods by N configurations) and we do not keep one for
  the book as a whole. Gate 2 reports that criterion as unevaluable rather than passing it.
"""

from __future__ import annotations

import logging
import math
import random
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.schemas import Lane, digest, now_ms
from kaiba.learning import gates, metrics
from kaiba.learning.registry import trial_count

log = logging.getLogger(__name__)

ZERO = Decimal(0)
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS

#: The working regime length. **This is a working figure, not a measurement.** The research
#: takes eight weeks from the observed cadence of venue-level changes: pump.fun's mandatory
#: tiered fees (2026-09-01), Jito ShredStream's shutdown (2026-09-05), the launchpad
#: dominance shift (2026-08-29). Nobody has published a regime-length distribution for this
#: market, so treat it as an order of magnitude and not a constant.
REGIME_WEEKS = 8.0

#: A passed gate expires after one regime. The venue changes underneath a verdict.
GATE_TTL_MS = int(REGIME_WEEKS * WEEK_MS)

#: Bare significance, one-sided, as used in the research's sample-size table.
Z_NAIVE = 1.96

#: Normal deviate for 80% power. Added to the deflated threshold so the sample size is the
#: one that would *detect* the edge, not merely the one at which it stops being impossible.
Z_POWER_80 = 0.8416212335729143

#: Two-sample detection threshold for the control arm: alpha = 0.01 one-sided.
Z_ALPHA_01 = 2.3263478740408408

EULER_MASCHERONI = gates.EULER_MASCHERONI

#: Below this many closed trades we refuse to estimate the per-trade Sharpe from our own
#: data at all. A fat-tailed distribution's standard deviation from 40 observations is not
#: an estimate, it is a coin flip with decimals, and the whole sample-size calculation
#: divides by it.
MIN_TRADES_FOR_OWN_DISTRIBUTION = 100

#: Independent closed trades per week the research judges achievable at a size that does
#: not move the pool. Used only to turn "never at the current rate" — which is true and
#: useless when nothing has closed yet — into a calendar figure that can be argued with.
#: It is the research's estimate of a *best case*, not a measurement of this agent.
ACHIEVABLE_TRADES_PER_WEEK: tuple[float, float] = (30.0, 60.0)

#: The research's own unrebutted expectation for how much trade clustering (by token, by
#: wallet, by launch cohort) inflates the calendar requirement over the i.i.d. arithmetic.
CLUSTERING_INFLATION: tuple[float, float] = (2.0, 5.0)

#: The hard tail gate from the research: delete the best 5% of trades and stay positive.
TAIL_DELETE_FRACTION = 0.05

#: Distinct days the block bootstrap needs before it will return a number at all.
#:
#: This is a guard against the method's own failure mode rather than a convention. The
#: block bootstrap estimates uncertainty from variation *between* blocks, so with a handful
#: of blocks it has almost nothing to estimate from — and when the few blocks it has happen
#: to look alike, it reports a very small standard error and therefore a very large t. It
#: fails towards confidence, which is the direction that gets capital deployed. Eighty
#: trades bunched into four days can produce a t of 18 this way; the same eighty spread
#: over eighty days produce 3.5, and the second number is the true one.
MIN_BOOTSTRAP_BLOCKS = 20

#: Gate thresholds, all from the protocol at the end of research doc 13.
GATE2_MIN_TRADES = 1000
GATE2_MIN_REGIMES = 3
GATE2_DSR_MIN = 0.95
GATE2_PBO_MAX = 0.05
GATE3_MIN_TRADES = 500
GATE3_MIN_WEEKS = 6.0
GATE3_CONTROL_ALPHA = 0.01
GATE3_MIN_POSITIVE_WEEK_SHARE = 4.0 / 6.0
GATE3_MAX_SINGLE_WEEK_SHARE = 0.50
GATE3_MIN_DEGRADATION = 0.50
GATE4_MIN_TRADES = 300
GATE4_MIN_DEGRADATION = 0.50
GATE5_MAX_STEP_MULTIPLE = 1.5
GATE5_MIN_TRADES_PER_STEP = 150

#: Gate 0: share of the token universe whose creation time came from chain state.
GATE0_MIN_ONCHAIN_SHARE = 0.98
#: Gate 1: share of trades carrying a measured slippage and a non-zero fee.
GATE1_MIN_SLIPPAGE_SHARE = 0.95
GATE1_MIN_FEE_SHARE = 0.99
#: Gate 1: the research's headline criterion — paper edge at least twice modelled friction.
GATE1_MIN_EDGE_OVER_FRICTION = 2.0

#: Control arm: the smallest cohort that could detect anything worth detecting.
CONTROL_MIN_ARM = 30
#: 1:1 nearest neighbour with a 0.2-SD caliper, matching arXiv:2607.02795's own design.
CONTROL_CALIPER_SD = 0.2
CONTROL_PERMUTATIONS = 10_000

#: Deterministic seeds. A non-reproducible validation result is not a validation result.
SEED = 20260920

ENTRY_ACTIONS = ("enter", "scale_in")


class Verdict(StrEnum):
    """Four outcomes, because two is a lie in this domain.

    ``UNDERPOWERED`` is not an error and not a failure. For the next several months it is
    the correct answer to almost everything asked here, and the API's job is to make that
    answer impossible to mistake for either of the other two.
    """

    PASS = "pass"
    FAIL = "fail"
    UNDERPOWERED = "underpowered"
    BLOCKED = "blocked"


# --------------------------------------------------------------------------------------
# small statistics, kept local so the gate cannot be influenced by anything configurable
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    """A money cell as ``Decimal``, treating unreadable as zero *for counting purposes only*.

    Used where the question is "did this trade record a fee at all", so an unparseable cell
    and a missing one are the same answer: no, it did not.
    """
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip() or "0")
    except (ArithmeticError, ValueError):
        return ZERO


def expected_max_z(trials: int) -> float:
    """Expected maximum of ``trials`` independent standard normals (Bailey & LdP).

    ``E[max z] ~ (1 - g) * Phi^-1(1 - 1/N) + g * Phi^-1(1 - 1/(N e))``. This is the whole
    multiple-testing correction in one line: the best of N attempts at nothing still looks
    good, and how good is a function of N alone.
    """
    n = max(int(trials), 1)
    if n < 2:
        return 0.0
    hi = metrics.normal_inv_cdf(1 - 1 / n)
    lo = metrics.normal_inv_cdf(1 - 1 / (n * math.e))
    return (1 - EULER_MASCHERONI) * hi + EULER_MASCHERONI * lo


def significance_z(trials: int) -> float:
    """The t-statistic a per-trade Sharpe must clear after deflating for ``trials``.

    Never below :data:`Z_NAIVE`: deflation can only ever raise the bar.
    """
    return max(Z_NAIVE, expected_max_z(trials) + Z_POWER_80)


def trades_needed(mean_r: Decimal, sd_r: Decimal, z: float) -> int | None:
    """Closed trades for a per-trade Sharpe of ``mean/sd`` to clear ``z``.

    ``n = (z / SR_trade)^2``. ``None`` when the mean is not positive (there is no edge to
    size a sample for) or the spread is not positive (nothing to divide by). Returning
    ``None`` rather than a number is deliberate: a sample size for a negative edge is a
    sentence that reads as progress.
    """
    if sd_r <= 0 or mean_r <= 0:
        return None
    sr = float(mean_r) / float(sd_r)
    if sr <= 0:
        return None
    return int(math.ceil((z / sr) ** 2))


def delete_best(returns: Sequence[Decimal], fraction: float = TAIL_DELETE_FRACTION) -> list[Decimal]:
    """The series with its best ``fraction`` of trades removed, at least one when non-empty.

    This is the brutal gate. A strategy that only survives with its top 5% intact is a
    lottery ticket with extra steps, and the research has a live demonstration: a 15-day
    deployment at +117.7% cumulative flipped unprofitable on the removal of three trades
    out of 190 (arXiv:2606.08232).
    """
    rows = sorted(returns)
    if not rows:
        return []
    cut = max(1, int(math.ceil(len(rows) * fraction)))
    return rows[: len(rows) - cut]


def block_bootstrap_t(
    observations: Sequence[tuple[int, Decimal]],
    *,
    draws: int = 2000,
    seed: int = SEED,
) -> float | None:
    """t-statistic of the mean, resampling whole days rather than single trades.

    Trades inside one day share a SOL regime, a narrative and often a token, so treating
    them as independent overstates the sample by whatever the intra-day correlation is. The
    day is the resampling unit; the returned t is ``mean / sd(bootstrap means)``.

    ``None`` when there are fewer than :data:`MIN_BOOTSTRAP_BLOCKS` distinct days. That is
    the important refusal: with few blocks the bootstrap cannot estimate between-block
    variance, and if those blocks happen to resemble each other it returns a tiny standard
    error and a huge t. Silence is the only safe answer there, and it makes the criterion
    unevaluable rather than triumphantly satisfied.

    Each day is reduced to ``(sum, count)`` before resampling, so a draw costs one pass over
    the *days* rather than over the trades. That is not an approximation — the mean of a
    concatenation of blocks is the sum of their sums over the sum of their counts — and it
    is what makes this cheap enough to run inside a gate on a six-figure trade series.
    """
    sums: dict[int, float] = {}
    counts: dict[int, int] = {}
    for ts_ms, value in observations:
        key = int(ts_ms) // DAY_MS
        sums[key] = sums.get(key, 0.0) + float(value)
        counts[key] = counts.get(key, 0) + 1
    keys = sorted(sums)
    if len(keys) < MIN_BOOTSTRAP_BLOCKS:
        return None
    block_sums = [sums[k] for k in keys]
    block_counts = [counts[k] for k in keys]
    total_n = sum(block_counts)
    if total_n <= 0:
        return None
    mean = sum(block_sums) / total_n

    rng = random.Random(seed)
    n_blocks = len(keys)
    means: list[float] = []
    for _ in range(max(draws, 2)):
        acc_sum = 0.0
        acc_n = 0
        for _ in range(n_blocks):
            i = rng.randrange(n_blocks)
            acc_sum += block_sums[i]
            acc_n += block_counts[i]
        if acc_n:
            means.append(acc_sum / acc_n)
    if len(means) < 2:
        return None
    mu = sum(means) / len(means)
    var = sum((m - mu) ** 2 for m in means) / (len(means) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def permutation_p(
    treated: Sequence[Decimal],
    control: Sequence[Decimal],
    *,
    draws: int = CONTROL_PERMUTATIONS,
    seed: int = SEED,
) -> float | None:
    """One-sided p for ``mean(treated) > mean(control)`` by label permutation.

    A permutation test rather than a t-test because per-wallet forward returns are as
    fat-tailed as the trades underneath them, and the t-test's normality assumption is the
    first thing a 30x outlier breaks. Seeded, so the same cohorts always produce the same
    p-value; a validation result that moves when you rerun it is not evidence.
    """
    a, b = list(treated), list(control)
    if len(a) < 2 or len(b) < 2:
        return None
    observed = float(sum(a, ZERO) / Decimal(len(a)) - sum(b, ZERO) / Decimal(len(b)))
    pool = [float(v) for v in a + b]
    n_a = len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(max(draws, 1)):
        rng.shuffle(pool)
        left = sum(pool[:n_a]) / n_a
        right = sum(pool[n_a:]) / (len(pool) - n_a)
        if left - right >= observed:
            hits += 1
    return (1 + hits) / (1 + max(draws, 1))


def welch_t(treated: Sequence[Decimal], control: Sequence[Decimal]) -> float | None:
    """Welch's t for two samples with unequal variances. Reported, never decisive."""
    a, b = list(treated), list(control)
    if len(a) < 2 or len(b) < 2:
        return None
    sa, sb = metrics.mean_std(a), metrics.mean_std(b)
    if sa is None or sb is None:
        return None
    (mean_a, sd_a), (mean_b, sd_b) = sa, sb
    se_sq = float(sd_a) ** 2 / len(a) + float(sd_b) ** 2 / len(b)
    if se_sq <= 0:
        return None
    return float(mean_a - mean_b) / math.sqrt(se_sq)


def minimum_detectable_effect(
    treated: Sequence[Decimal], control: Sequence[Decimal], *, alpha_z: float = Z_ALPHA_01
) -> float | None:
    """Smallest difference in means these two cohorts could detect at 80% power.

    The number that separates "no effect" from "no chance of seeing one". An observed
    difference below this is :attr:`Verdict.UNDERPOWERED`, not :attr:`Verdict.FAIL`.
    """
    a, b = list(treated), list(control)
    if len(a) < 2 or len(b) < 2:
        return None
    sa, sb = metrics.mean_std(a), metrics.mean_std(b)
    if sa is None or sb is None:
        return None
    se_sq = float(sa[1]) ** 2 / len(a) + float(sb[1]) ** 2 / len(b)
    if se_sq <= 0:
        return None
    return (alpha_z + Z_POWER_80) * math.sqrt(se_sq)


# --------------------------------------------------------------------------------------
# criteria
# --------------------------------------------------------------------------------------


class Criterion(BaseModel):
    """One numeric test with its own verdict. Absent input is never a pass."""

    model_config = ConfigDict(extra="forbid")

    name: str
    verdict: Verdict
    value: float | None = None
    threshold: float | None = None
    comparison: str = "ge"  # ge | le | gt | lt
    basis: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict is Verdict.PASS


def criterion(
    name: str,
    value: float | None,
    threshold: float | None,
    comparison: str = "ge",
    *,
    basis: str = "",
    missing: str = "input not available",
) -> Criterion:
    """Evaluate one criterion. ``value is None`` is :attr:`Verdict.UNDERPOWERED`, always.

    When the criterion is unevaluable, ``missing`` is carried into the basis alongside the
    description rather than instead of it. The reason a thing could not be measured is the
    half a reader needs: "no T-by-N trial matrix exists" is actionable, "probability that
    the in-sample winner is at or below the median" is just a definition.
    """
    if value is None or threshold is None:
        return Criterion(
            name=name, verdict=Verdict.UNDERPOWERED, value=value, threshold=threshold,
            comparison=comparison, basis=f"{basis} — {missing}" if basis else missing,
        )
    ok = {
        "ge": value >= threshold,
        "le": value <= threshold,
        "gt": value > threshold,
        "lt": value < threshold,
    }[comparison]
    return Criterion(
        name=name, verdict=Verdict.PASS if ok else Verdict.FAIL, value=value,
        threshold=threshold, comparison=comparison, basis=basis,
    )


def combine(criteria: Sequence[Criterion]) -> Verdict:
    """Any failure fails; otherwise any unevaluable criterion is underpowered.

    Note the order: a real failure outranks a missing input, because a gate with one broken
    criterion and one unmeasured one has been answered, and the answer is no.
    """
    if not criteria:
        return Verdict.UNDERPOWERED
    if any(c.verdict is Verdict.FAIL for c in criteria):
        return Verdict.FAIL
    if any(c.verdict in (Verdict.UNDERPOWERED, Verdict.BLOCKED) for c in criteria):
        return Verdict.UNDERPOWERED
    return Verdict.PASS


# --------------------------------------------------------------------------------------
# the honest N
# --------------------------------------------------------------------------------------


def honest_trials(conn: sqlite3.Connection | None = None, lane: Lane | str | None = None) -> int:
    """Every configuration ever run, from the registry. Callers cannot lower this.

    Per lane this is :func:`kaiba.learning.gates._trial_count`, which takes the larger of
    the declared ``experiments`` and the automatically-recorded ``lane_trials``. For the
    book as a whole it is the same comparison across every lane at once. There is
    deliberately no argument to override it: the only reason to pass a trial count into a
    deflation is to make it smaller.
    """
    c = conn or get_conn()
    if lane is not None:
        name = lane.value if isinstance(lane, Lane) else str(lane)
        # Both counts, and the larger wins. `gates._trial_count` already consults the
        # registry, but it swallows a registry failure and falls back to declared
        # experiments alone; asking the registry directly as well means a partial failure
        # can only ever raise N, never quietly lower it.
        return max(1, gates._trial_count(c, name), trial_count(name, c))
    try:
        run = fetch_one(c, "SELECT COUNT(*) AS n FROM lane_trials", ())
        declared = fetch_one(c, "SELECT COUNT(*) AS n FROM experiments", ())
    except sqlite3.Error as exc:
        log.debug("trial tables unreadable: %s", exc)
        return 1
    return max(1, int(run["n"] if run else 0), int(declared["n"] if declared else 0))


# --------------------------------------------------------------------------------------
# 1. statistical power — the headline
# --------------------------------------------------------------------------------------


class TradeProfile(BaseModel):
    """A per-trade return distribution, observed or assumed."""

    model_config = ConfigDict(extra="forbid")

    name: str
    mean_r: Decimal
    sd_r: Decimal
    source: str

    @property
    def sharpe_per_trade(self) -> float | None:
        if self.sd_r <= 0:
            return None
        return float(self.mean_r) / float(self.sd_r)


#: The research's profiles C and D (B3): a 30-35% hit rate with a fat right tail, which is
#: what this market actually pays. Used only when we have too few trades of our own to
#: estimate anything. Profiles A and B in the same table need a few hundred trades and are
#: what people imagine they have; they do not describe a venue where 73% of migrated tokens
#: fall below 40% of migration price within twenty minutes.
FALLBACK_PROFILES: tuple[TradeProfile, ...] = (
    TradeProfile(
        name="research-C",
        mean_r=Decimal("0.115"),
        sd_r=Decimal("4.33"),
        source="research 13 B3 profile C: 35% hit, 65%/-1R, 33%/+0.5R, 2%/+30R",
    ),
    TradeProfile(
        name="research-D",
        mean_r=Decimal("0.120"),
        sd_r=Decimal("3.67"),
        source="research 13 B3 profile D: 32% hit, 68%/-1R, 30%/+1R, 2%/+25R",
    ),
)


class PowerReport(BaseModel):
    """What we can and cannot conclude from the trades we have.

    The most useful field is :attr:`verdict`, and for the foreseeable future it will read
    ``underpowered``. The second most useful is :attr:`weeks_to_significance` against
    :attr:`regime_weeks`: if the first exceeds the second permanently, no amount of patience
    produces a stationary sample and the correct response is to not deploy directional
    capital rather than to deploy it unvalidated.
    """

    model_config = ConfigDict(extra="forbid")

    lane: str | None = None
    verdict: Verdict = Verdict.UNDERPOWERED
    closed_trades: int = 0
    entry_decisions: int = 0
    total_decisions: int = 0
    window_days: float = 0.0
    hit_rate: float | None = None
    mean_r: Decimal | None = None
    sd_r: Decimal | None = None
    sharpe_per_trade: float | None = None
    profile: str = ""
    profile_basis: str = ""
    trials: int = 1
    z_naive: float = Z_NAIVE
    z_deflated: float = Z_NAIVE
    trades_needed_naive: int | None = None
    trades_needed_deflated: int | None = None
    trades_needed_range: tuple[int, int] | None = None
    trades_still_needed: int | None = None
    trades_per_week: float | None = None
    weeks_to_significance: float | None = None
    regime_weeks: float = REGIME_WEEKS
    required_rate_per_week: float | None = None
    years_at_achievable_rate: tuple[float, float] | None = None
    years_with_clustering: tuple[float, float] | None = None
    exceeds_regime: bool = True
    notes: list[str] = Field(default_factory=list)
    checked_ms: int = Field(default_factory=now_ms)

    @property
    def headline(self) -> str:
        """One sentence an operator can act on without reading the JSON."""
        if self.verdict is Verdict.PASS:
            return (
                f"{self.closed_trades} closed trades is enough to conclude at the deflated "
                f"bar (needed {self.trades_needed_deflated}, N={self.trials} trials)."
            )
        need = self.trades_needed_deflated
        still = self.trades_still_needed
        if need is None:
            return (
                f"UNDERPOWERED: {self.closed_trades} closed trades, and no positive per-trade "
                "edge to size a sample against yet."
            )
        when = (
            "never at the current rate"
            if self.weeks_to_significance is None
            else f"{self.weeks_to_significance:.0f} weeks at the current rate"
        )
        projected = ""
        if self.years_at_achievable_rate is not None:
            best, worst = self.years_at_achievable_rate
            projected = (
                f" Even at an achievable "
                f"{ACHIEVABLE_TRADES_PER_WEEK[0]:.0f}-{ACHIEVABLE_TRADES_PER_WEEK[1]:.0f} "
                f"trades/week that is {best:.1f}-{worst:.1f} years"
            )
            if self.years_with_clustering is not None:
                projected += (
                    f" ({self.years_with_clustering[0]:.1f}-"
                    f"{self.years_with_clustering[1]:.1f} allowing for clustering)"
                )
            projected += f", against an {self.regime_weeks:.0f}-week regime."
        return (
            f"UNDERPOWERED: {self.closed_trades} closed trades of the {need} needed "
            f"(N={self.trials} trials deflates to z={self.z_deflated:.2f}); "
            f"{still} still to go, {when}.{projected}"
        )

    def record(self, conn: sqlite3.Connection | None = None) -> str:
        return _record_run(
            conn, kind="power", lane=self.lane, verdict=self.verdict,
            sample_n=self.closed_trades, summary=self.headline,
            payload=self.model_dump(mode="json"),
        )


def _decision_counts(
    conn: sqlite3.Connection, lane: Lane | str | None, since_ms: int
) -> tuple[int, int, int, int]:
    """(total, entries, first_ts, last_ts) over the decision stream."""
    sql = "SELECT COUNT(*) AS n, MIN(ts_ms) AS lo, MAX(ts_ms) AS hi FROM decisions WHERE ts_ms >= ?"
    params: list[Any] = [since_ms]
    if lane is not None:
        sql += " AND lane = ?"
        params.append(lane.value if isinstance(lane, Lane) else str(lane))
    row = fetch_one(conn, sql, params) or {}
    entry_sql = (
        f"SELECT COUNT(*) AS n FROM decisions WHERE ts_ms >= ? AND action IN "
        f"({','.join('?' * len(ENTRY_ACTIONS))})"
    )
    entry_params: list[Any] = [since_ms, *ENTRY_ACTIONS]
    if lane is not None:
        entry_sql += " AND lane = ?"
        entry_params.append(lane.value if isinstance(lane, Lane) else str(lane))
    entries = fetch_one(conn, entry_sql, entry_params) or {}
    return (
        int(row.get("n") or 0),
        int(entries.get("n") or 0),
        int(row.get("lo") or 0),
        int(row.get("hi") or 0),
    )


def power_report(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    mode: str | None = None,
    since_ms: int = 0,
    as_of_ms: int | None = None,
    regime_weeks: float = REGIME_WEEKS,
) -> PowerReport:
    """How far we are from being able to say anything, and how long that would take.

    Reads closed trades, estimates the per-trade return distribution when there are enough
    of them to bother (:data:`MIN_TRADES_FOR_OWN_DISTRIBUTION`) and falls back to the
    research's fat-tail profiles when there are not. The deflation uses the registry's trial
    count. The rate is measured over the observation window, which is the span of the
    decision stream when no trade has closed — otherwise a system that has closed nothing
    reports an undefined rate rather than a zero one.
    """
    c = conn or get_conn()
    now = as_of_ms if as_of_ms is not None else now_ms()
    trades = metrics.load_trades(c, since_ms=since_ms, mode=mode, lane=lane)
    returns = metrics.returns_of(trades)
    total_dec, entries, dec_lo, dec_hi = _decision_counts(c, lane, since_ms)

    notes: list[str] = []
    trials = honest_trials(c, lane)
    z_def = significance_z(trials)

    # ---- the observation window: trades if we have them, the decision stream if not
    if trades:
        lo = min(int(t["closed_ms"]) for t in trades)
        hi = max(int(t["closed_ms"]) for t in trades)
    else:
        lo, hi = dec_lo, max(dec_hi, dec_lo)
    window_ms = max(0, (hi or 0) - (lo or 0))
    window_days = window_ms / DAY_MS
    window_weeks = window_ms / WEEK_MS

    # ---- the per-trade distribution
    profile: TradeProfile | None = None
    hit_rate = metrics.win_rate(trades)
    stats = metrics.mean_std(returns) if len(returns) >= 2 else None
    if len(returns) >= MIN_TRADES_FOR_OWN_DISTRIBUTION and stats is not None:
        profile = TradeProfile(
            name="observed", mean_r=stats[0], sd_r=stats[1],
            source=f"our own {len(returns)} closed trades",
        )
    elif returns:
        notes.append(
            f"{len(returns)} closed trades is below the {MIN_TRADES_FOR_OWN_DISTRIBUTION} "
            "needed to estimate a fat-tailed spread from our own data; the sample-size "
            "figures below use the research's profiles C and D instead of ours"
        )
    else:
        notes.append(
            "no closed trades at all: every figure below is what the research's fat-tail "
            "profiles imply, not a measurement of this agent"
        )

    needed_naive: int | None = None
    needed_def: int | None = None
    needed_range: tuple[int, int] | None = None
    if profile is not None:
        needed_naive = trades_needed(profile.mean_r, profile.sd_r, Z_NAIVE)
        needed_def = trades_needed(profile.mean_r, profile.sd_r, z_def)
        if needed_naive is None:
            notes.append(
                "observed mean per-trade return is not positive, so there is no edge for "
                "which a sample size can be computed; the question is not yet 'how many "
                "trades' but 'is there anything to measure'"
            )
    else:
        naive = [trades_needed(p.mean_r, p.sd_r, Z_NAIVE) for p in FALLBACK_PROFILES]
        defl = [trades_needed(p.mean_r, p.sd_r, z_def) for p in FALLBACK_PROFILES]
        naive_ok = [n for n in naive if n is not None]
        defl_ok = [n for n in defl if n is not None]
        if naive_ok and defl_ok:
            needed_naive = max(naive_ok)
            needed_def = max(defl_ok)
            needed_range = (min(defl_ok), max(defl_ok))
        profile = FALLBACK_PROFILES[0]

    # ---- rate, and what it implies
    closed = len(returns)
    rate_per_week: float | None = None
    if closed > 0 and window_weeks > 0:
        rate_per_week = closed / window_weeks
    elif closed == 0:
        rate_per_week = 0.0
        notes.append(
            f"zero closed trades in {window_days:.2f} days of recorded decisions "
            f"({total_dec} decisions, {entries} of them entries): the closing rate is zero, "
            "so the time to significance is not long, it is unbounded"
        )

    still_needed = None if needed_def is None else max(0, needed_def - closed)
    weeks_to: float | None = None
    if still_needed is not None and rate_per_week:
        weeks_to = still_needed / rate_per_week
    required_rate = None if needed_def is None else needed_def / max(regime_weeks, 1e-9)

    # "Never at the current rate" is true when nothing has closed, and useless. The
    # decision-relevant question is how long this takes even if the book starts working:
    # project the outstanding sample at the best rate the research thinks is achievable at
    # a size that does not move the pool, and then again with the clustering inflation the
    # research expects on top. If that answer is years, the directional book is not the
    # business, and that conclusion is available *today* rather than after a year of
    # gathering data to discover it.
    years_achievable: tuple[float, float] | None = None
    years_clustered: tuple[float, float] | None = None
    if still_needed:
        slow, fast = ACHIEVABLE_TRADES_PER_WEEK
        best = still_needed / fast / 52.0
        worst = still_needed / slow / 52.0
        years_achievable = (best, worst)
        years_clustered = (best * CLUSTERING_INFLATION[0], worst * CLUSTERING_INFLATION[1])

    powered = bool(needed_def is not None and closed >= needed_def)
    exceeds = True
    if powered:
        exceeds = False
    elif weeks_to is not None:
        exceeds = weeks_to > regime_weeks

    if not powered and exceeds:
        horizon = "never at the current rate" if weeks_to is None else f"{weeks_to:.0f} weeks"
        notes.append(
            f"TIME TO SIGNIFICANCE EXCEEDS THE REGIME: reaching {needed_def} closed trades "
            f"would take {horizon}, against a working regime length of {regime_weeks:.0f} "
            "weeks (a working figure from the observed cadence of venue changes, not a "
            "measured constant). A sample gathered across regime boundaries is not a "
            "stationary sample, so it cannot be tested as one. Under this condition "
            "'validate before trading' is not a gate that can be passed, and the correct "
            "response is to not deploy directional capital rather than to deploy it "
            "unvalidated."
        )
        if required_rate is not None:
            notes.append(
                f"to finish inside one regime the book would have to close "
                f"{required_rate:.0f} independent trades per week at a size that does not "
                f"move the pool; the research's own estimate of what is achievable is "
                f"{ACHIEVABLE_TRADES_PER_WEEK[0]:.0f}-{ACHIEVABLE_TRADES_PER_WEEK[1]:.0f}"
            )
        if years_achievable is not None and years_clustered is not None:
            notes.append(
                f"EVEN AT A RATE THAT WORKS: at the achievable "
                f"{ACHIEVABLE_TRADES_PER_WEEK[0]:.0f}-{ACHIEVABLE_TRADES_PER_WEEK[1]:.0f} "
                f"closed trades per week, {still_needed} trades takes "
                f"{years_achievable[0]:.1f}-{years_achievable[1]:.1f} years, and "
                f"{years_clustered[0]:.1f}-{years_clustered[1]:.1f} years once trade "
                "clustering is allowed for. That is many regimes long, so the sample would "
                "never be stationary and the question would never be answerable by waiting. "
                "This is a conclusion available now, not one that needs a year of data to "
                "reach: if the directional book cannot be validated in principle, the "
                "business is the parts that need no validation window at all"
            )
    notes.append(
        "every sample size here assumes independent trades. They are not: trades cluster "
        "by token, by wallet and by launch cohort, and the research's unrebutted "
        f"expectation is that this inflates the calendar requirement by a further "
        f"{CLUSTERING_INFLATION[0]:.0f}-{CLUSTERING_INFLATION[1]:.0f}x"
    )

    report = PowerReport(
        lane=None if lane is None else (lane.value if isinstance(lane, Lane) else str(lane)),
        verdict=Verdict.PASS if powered else Verdict.UNDERPOWERED,
        closed_trades=closed,
        entry_decisions=entries,
        total_decisions=total_dec,
        window_days=round(window_days, 4),
        hit_rate=hit_rate,
        mean_r=profile.mean_r if profile.name == "observed" else None,
        sd_r=profile.sd_r if profile.name == "observed" else None,
        sharpe_per_trade=profile.sharpe_per_trade,
        profile=profile.name,
        profile_basis=profile.source,
        trials=trials,
        z_deflated=z_def,
        trades_needed_naive=needed_naive,
        trades_needed_deflated=needed_def,
        trades_needed_range=needed_range,
        trades_still_needed=still_needed,
        trades_per_week=rate_per_week,
        weeks_to_significance=weeks_to,
        regime_weeks=regime_weeks,
        required_rate_per_week=required_rate,
        years_at_achievable_rate=years_achievable,
        years_with_clustering=years_clustered,
        exceeds_regime=exceeds,
        notes=notes,
        checked_ms=now,
    )
    return report


# --------------------------------------------------------------------------------------
# 2. the matched-control arm for wallet grading
# --------------------------------------------------------------------------------------

#: What the cohorts are matched on. Each is a confounder: it moves both the probability of
#: being graded well and the forward return, so an imbalance in it would be read as grade
#: skill. Log scale for counts because the distribution is a power law.
MATCH_COVARIATES: tuple[str, ...] = (
    "log_closed_trades",
    "log_distinct_tokens",
    "evidence_weight",
    "days_since_last_activity",
)

#: What we could not match on. This list is part of the result, not a caveat appended to
#: it: a matched study whose unmatched confounders are undeclared is an unmatched study.
UNMATCHED_CONFOUNDERS: tuple[str, ...] = (
    "capital at risk — we have no reliable per-wallet position size at t, and size drives "
    "curve impact, which drives realised return directly",
    "operator identity — one operator runs many addresses, so the arms are not independent "
    "at the level that matters; we drop a control that shares a known entity with its pair, "
    "but our entity graph is sparse and most clusters are invisible to it",
    "unrealised inventory — our forward edge is realised-only, so a wallet that is "
    "bagholding scores flat rather than down. This is the same accounting that makes 73% of "
    "pump.fun wallets look profitable; it biases both arms, but not necessarily equally",
    "strategy drift — a wallet is not a stable agent. Addresses are rotated per launch as "
    "standard bundler practice, and a wallet can change hands or bots inside the window",
    "off-chain following — a wallet that is published on a tracker is copied, and being "
    "copied changes its forward returns. We cannot observe who is on which list",
    "venue mix — we match on activity level but not on which venues or curve stages the "
    "wallet trades, and the imitation penalty differs by an order of magnitude across them",
)


class CohortSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cohort_id: str
    frozen_ms: int
    chain: str
    graded_n: int
    control_n: int
    matched_on: list[str]
    unmatched: list[str]
    notes: list[str] = Field(default_factory=list)


class ControlArmReport(BaseModel):
    """Forward comparison of a frozen graded cohort against its matched control.

    This is the test the literature has never run: no published autocorrelation study, no
    forward-tracked cohort with a control, no vendor out-of-sample validation. Until it
    returns ``pass``, wallet grading is an instrumented hypothesis and per the protocol it
    may inform sizing and screening but must not be a sole entry trigger.
    """

    model_config = ConfigDict(extra="forbid")

    cohort_id: str
    verdict: Verdict = Verdict.UNDERPOWERED
    frozen_ms: int = 0
    horizon_ms: int = 0
    chain: str = "sol"
    graded_frozen: int = 0
    control_frozen: int = 0
    graded_observed: int = 0
    control_observed: int = 0
    graded_edge: Decimal | None = None
    control_edge: Decimal | None = None
    difference: Decimal | None = None
    p_value: float | None = None
    welch_t: float | None = None
    minimum_detectable: float | None = None
    alpha: float = GATE3_CONTROL_ALPHA
    attrition_gap: float | None = None
    matched_on: list[str] = Field(default_factory=list)
    unmatched: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    checked_ms: int = Field(default_factory=now_ms)

    @property
    def headline(self) -> str:
        if self.verdict is Verdict.PASS:
            return (
                f"Wallet grading beats its matched control on forward realised edge "
                f"(p={self.p_value:.4f} < {self.alpha}) over {self.graded_observed} vs "
                f"{self.control_observed} wallets."
            )
        if self.verdict is Verdict.FAIL:
            return (
                f"Wallet grading did NOT beat its matched control "
                f"(difference {self.difference}, p={self.p_value}). Per the protocol the "
                "grader comes off the entry path and stays, at most, a sizing input."
            )
        return (
            f"UNDERPOWERED: {self.graded_observed} graded and {self.control_observed} control "
            f"wallets with forward data; this cannot detect an effect of any plausible size."
        )

    def record(self, conn: sqlite3.Connection | None = None) -> str:
        return _record_run(
            conn, kind="control", lane=None, verdict=self.verdict,
            sample_n=min(self.graded_observed, self.control_observed),
            summary=self.headline, payload=self.model_dump(mode="json"),
        )


def _score_rows(conn: sqlite3.Connection, chain: str, as_of_ms: int) -> list[dict[str, Any]]:
    """Wallet scores as they stood at ``as_of_ms``. Later re-scores are invisible on purpose.

    Reading the current grade and calling the test "forward" is the ex-post contamination
    arXiv:2602.14860 warns about in its own Table I, where a top-10 list over a full month
    overlapped its own first fortnight by seven names and the overlap was arithmetic rather
    than persistence.
    """
    return fetch_all(
        conn,
        "SELECT chain, address, grade, score, evidence_weight, closed_trades, distinct_tokens, "
        "scored_at_ms FROM wallet_scores WHERE chain = ? AND scored_at_ms <= ? "
        "ORDER BY address ASC",
        (chain, as_of_ms),
    )


def _covariates(conn: sqlite3.Connection, row: Mapping[str, Any], as_of_ms: int) -> dict[str, float]:
    wallet = fetch_one(
        conn,
        "SELECT last_seen_ms, first_seen_ms FROM wallets WHERE chain = ? AND address = ?",
        (str(row["chain"]), str(row["address"])),
    )
    last_seen = int(wallet["last_seen_ms"]) if wallet and wallet["last_seen_ms"] else None
    closed = row.get("closed_trades")
    tokens = row.get("distinct_tokens")
    weight = row.get("evidence_weight")
    return {
        "log_closed_trades": math.log10(1 + float(closed)) if closed is not None else float("nan"),
        "log_distinct_tokens": math.log10(1 + float(tokens)) if tokens is not None else float("nan"),
        "evidence_weight": float(weight) if weight is not None else float("nan"),
        "days_since_last_activity": (
            (as_of_ms - last_seen) / DAY_MS if last_seen is not None else float("nan")
        ),
    }


def _entity_of(conn: sqlite3.Connection, chain: str, address: str) -> str | None:
    row = fetch_one(
        conn,
        "SELECT entity_id FROM entity_members WHERE chain = ? AND address = ? LIMIT 1",
        (chain, address),
    )
    return str(row["entity_id"]) if row else None


def _sd(values: Sequence[float]) -> float:
    clean = [v for v in values if not math.isnan(v)]
    if len(clean) < 2:
        return 0.0
    mu = sum(clean) / len(clean)
    return math.sqrt(sum((v - mu) ** 2 for v in clean) / (len(clean) - 1))


def freeze_cohort(
    conn: sqlite3.Connection | None = None,
    *,
    as_of_ms: int | None = None,
    chain: str = "sol",
    graded_grades: Sequence[str] = ("A", "B"),
    caliper_sd: float = CONTROL_CALIPER_SD,
) -> CohortSummary:
    """Freeze a graded cohort and a matched control at ``as_of_ms``, and write both down.

    Matching is 1:1 nearest neighbour on the standardised covariates in
    :data:`MATCH_COVARIATES` with a ``caliper_sd`` calliper, the design used by
    arXiv:2607.02795. A candidate control that shares a known entity with its graded pair is
    rejected, because two addresses of one operator are one observation.

    The control arm is the complement of the graded set: it is, unavoidably, selected on
    *not being graded*. That is the standard treated/untreated contrast and it is what the
    protocol asks for, but it means the test measures the grade **label**, not the trait the
    label is trying to name. A control drawn at random from the whole population instead
    would measure something different and weaker. This is stated in the result rather than
    buried here.
    """
    c = conn or get_conn()
    t = as_of_ms if as_of_ms is not None else now_ms()
    rows = _score_rows(c, chain, t)
    notes: list[str] = []
    wanted = {g.upper() for g in graded_grades}

    graded = [r for r in rows if str(r["grade"] or "").upper() in wanted]
    pool = [r for r in rows if str(r["grade"] or "").upper() not in wanted]
    cohort_id = "cohort_" + digest({"chain": chain, "t": t, "grades": sorted(wanted)})[:20]

    if not graded or not pool:
        notes.append(
            f"cohort is empty at t={t}: {len(graded)} graded ({'/'.join(sorted(wanted))}) and "
            f"{len(pool)} candidate controls among {len(rows)} scored wallets"
        )
        summary = CohortSummary(
            cohort_id=cohort_id, frozen_ms=t, chain=chain, graded_n=len(graded), control_n=0,
            matched_on=list(MATCH_COVARIATES), unmatched=list(UNMATCHED_CONFOUNDERS), notes=notes,
        )
        _write_freeze(c, summary, [], [])
        return summary

    g_cov = {str(r["address"]): _covariates(c, r, t) for r in graded}
    p_cov = {str(r["address"]): _covariates(c, r, t) for r in pool}
    spreads = {
        k: _sd([*(v[k] for v in g_cov.values()), *(v[k] for v in p_cov.values())])
        for k in MATCH_COVARIATES
    }
    degenerate = [k for k, s in spreads.items() if s <= 0]
    if degenerate:
        notes.append(
            "covariates with no spread across the population, so matching on them is "
            f"vacuous: {', '.join(degenerate)}"
        )

    def distance(a: Mapping[str, float], b: Mapping[str, float]) -> float | None:
        total = 0.0
        for key in MATCH_COVARIATES:
            spread = spreads[key]
            if spread <= 0:
                continue
            av, bv = a[key], b[key]
            if math.isnan(av) or math.isnan(bv):
                return None  # an unmatched covariate is not a match, it is a guess
            d = abs(av - bv) / spread
            if d > caliper_sd:
                return None
            total += d * d
        return math.sqrt(total)

    used: set[str] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    unmatched_graded = 0
    for pair_id, g in enumerate(sorted(graded, key=lambda r: str(r["address"]))):
        g_addr = str(g["address"])
        g_entity = _entity_of(c, chain, g_addr)
        best: tuple[float, dict[str, Any]] | None = None
        for p in pool:
            p_addr = str(p["address"])
            if p_addr in used or p_addr == g_addr:
                continue
            if g_entity is not None and _entity_of(c, chain, p_addr) == g_entity:
                continue
            d = distance(g_cov[g_addr], p_cov[p_addr])
            if d is None:
                continue
            if best is None or d < best[0]:
                best = (d, p)
        if best is None:
            unmatched_graded += 1
            continue
        used.add(str(best[1]["address"]))
        pairs.append((g, best[1], pair_id))

    if unmatched_graded:
        notes.append(
            f"{unmatched_graded} graded wallet(s) had no control inside the "
            f"{caliper_sd}-SD calliper and were dropped. Dropping them is the right call — "
            "widening the calliper to keep them would let the arms differ on the very "
            "covariates the match exists to hold fixed — but it means the graded arm is now "
            "the subset of graded wallets that look like ungraded ones"
        )
    notes.append(
        "the control arm is the complement of the graded set, so it is selected on not "
        "being graded; this measures the grade label, not the underlying trait"
    )

    graded_rows = [(g, pid) for g, _, pid in pairs]
    control_rows = [(p, pid) for _, p, pid in pairs]
    summary = CohortSummary(
        cohort_id=cohort_id, frozen_ms=t, chain=chain, graded_n=len(graded_rows),
        control_n=len(control_rows), matched_on=list(MATCH_COVARIATES),
        unmatched=list(UNMATCHED_CONFOUNDERS), notes=notes,
    )
    _write_freeze(
        c, summary,
        [(r, pid, g_cov[str(r["address"])]) for r, pid in graded_rows],
        [(r, pid, p_cov[str(r["address"])]) for r, pid in control_rows],
    )
    return summary


def _write_freeze(
    conn: sqlite3.Connection,
    summary: CohortSummary,
    graded: Sequence[tuple[Mapping[str, Any], int, Mapping[str, float]]],
    control: Sequence[tuple[Mapping[str, Any], int, Mapping[str, float]]],
) -> None:
    conn.execute(
        "INSERT INTO wallet_cohort_freezes (cohort_id, frozen_ms, chain, graded_n, control_n, "
        "matched_on_json, unmatched_json, notes_json) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(cohort_id) DO UPDATE SET graded_n=excluded.graded_n, "
        "control_n=excluded.control_n, notes_json=excluded.notes_json",
        (
            summary.cohort_id, summary.frozen_ms, summary.chain, summary.graded_n,
            summary.control_n, jdump(summary.matched_on), jdump(summary.unmatched),
            jdump(summary.notes),
        ),
    )
    for arm, rows in (("graded", graded), ("control", control)):
        for row, pair_id, cov in rows:
            conn.execute(
                "INSERT OR IGNORE INTO wallet_cohorts (cohort_id, arm, chain, address, pair_id, "
                "frozen_ms, grade, covariates_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    summary.cohort_id, arm, summary.chain, str(row["address"]), pair_id,
                    summary.frozen_ms, str(row.get("grade") or ""), jdump(dict(cov)),
                ),
            )


def latest_cohort(
    conn: sqlite3.Connection | None = None, *, chain: str = "sol"
) -> str | None:
    """The most recently frozen cohort for a chain, or ``None`` if nothing was ever frozen.

    Deliberately does not create one. A cohort frozen on demand by whatever happened to ask
    has a forward window of zero and answers nothing; the freeze is an event with a date and
    it has to be chosen, not stumbled into.
    """
    row = fetch_one(
        conn or get_conn(),
        "SELECT cohort_id FROM wallet_cohort_freezes WHERE chain = ? AND graded_n > 0 "
        "ORDER BY frozen_ms DESC LIMIT 1",
        (chain,),
    )
    return str(row["cohort_id"]) if row else None


def _forward_edge(
    conn: sqlite3.Connection, chain: str, address: str, start_ms: int, end_ms: int
) -> Decimal | None:
    """Realised ROI of one wallet over closed, uncontaminated episodes after ``start_ms``.

    ``None`` when the wallet closed nothing in the window — which is data, not a zero. A
    cohort that stops trading has not broken even, it has left, and differential attrition
    between the arms is reported rather than averaged away.
    """
    from kaiba.intelligence.pnl import reconstruct_wallet

    swaps = fetch_all(
        conn,
        "SELECT ts_ms, token, side, amount_token, amount_native, usd_value, chain FROM swaps "
        "WHERE chain = ? AND wallet = ? AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms ASC, id ASC",
        (chain, address, start_ms, end_ms),
    )
    if not swaps:
        return None
    _, summary = reconstruct_wallet(list(swaps), as_of_ms=end_ms)
    if summary.closed_episodes <= 0 or summary.cost_native <= 0:
        return None
    return Decimal(summary.realized_pnl_native) / Decimal(summary.cost_native)


def control_arm(
    conn: sqlite3.Connection | None = None,
    *,
    cohort_id: str | None = None,
    as_of_ms: int | None = None,
    horizon_ms: int = GATE_TTL_MS,
    chain: str = "sol",
    graded_grades: Sequence[str] = ("A", "B"),
    end_ms: int | None = None,
    seed: int = SEED,
) -> ControlArmReport:
    """Track a frozen cohort and its matched control forward, and test the difference.

    Freezes a new cohort when ``cohort_id`` is not given. The statistic is a seeded
    permutation test on the difference in mean forward realised ROI, one-sided, because the
    hypothesis has a direction: the graded cohort should do better. Welch's t is reported
    alongside but never decides — per-wallet returns inherit the fat tail of the trades
    underneath them and the first 30x outlier breaks the normality the t-test assumes.
    """
    c = conn or get_conn()
    t = as_of_ms if as_of_ms is not None else now_ms()
    if cohort_id is None:
        summary = freeze_cohort(c, as_of_ms=t, chain=chain, graded_grades=graded_grades)
        cohort_id = summary.cohort_id
        notes = list(summary.notes)
        frozen_ms, matched_on, unmatched = summary.frozen_ms, summary.matched_on, summary.unmatched
    else:
        row = fetch_one(
            c, "SELECT * FROM wallet_cohort_freezes WHERE cohort_id = ?", (cohort_id,)
        )
        if row is None:
            return ControlArmReport(
                cohort_id=cohort_id, verdict=Verdict.UNDERPOWERED,
                notes=[f"cohort {cohort_id} was never frozen; nothing to track forward"],
            )
        frozen_ms = int(row["frozen_ms"])
        chain = str(row["chain"])
        notes = list(jload(row["notes_json"], []))
        matched_on = list(jload(row["matched_on_json"], []))
        unmatched = list(jload(row["unmatched_json"], []))

    stop = end_ms if end_ms is not None else frozen_ms + horizon_ms
    members = fetch_all(
        c,
        "SELECT arm, address FROM wallet_cohorts WHERE cohort_id = ? ORDER BY pair_id ASC, arm ASC",
        (cohort_id,),
    )
    arms: dict[str, list[str]] = {"graded": [], "control": []}
    for m in members:
        arms.setdefault(str(m["arm"]), []).append(str(m["address"]))

    edges: dict[str, list[Decimal]] = {"graded": [], "control": []}
    for arm, addresses in arms.items():
        for address in addresses:
            edge = _forward_edge(c, chain, address, frozen_ms, stop)
            if edge is not None:
                edges[arm].append(edge)

    graded_vals, control_vals = edges["graded"], edges["control"]
    graded_n, control_n = len(arms["graded"]), len(arms["control"])
    g_obs, c_obs = len(graded_vals), len(control_vals)

    g_edge = sum(graded_vals, ZERO) / Decimal(g_obs) if g_obs else None
    c_edge = sum(control_vals, ZERO) / Decimal(c_obs) if c_obs else None
    diff = (g_edge - c_edge) if (g_edge is not None and c_edge is not None) else None
    p = permutation_p(graded_vals, control_vals, seed=seed)
    t_stat = welch_t(graded_vals, control_vals)
    mde = minimum_detectable_effect(graded_vals, control_vals)

    attrition_gap = None
    if graded_n and control_n:
        attrition_gap = abs((g_obs / graded_n) - (c_obs / control_n))
        if attrition_gap > 0.10:
            notes.append(
                f"differential attrition of {attrition_gap:.0%}: "
                f"{g_obs}/{graded_n} graded and {c_obs}/{control_n} control wallets traded in "
                "the window. That gap is itself a finding — a cohort that stops trading has "
                "not broken even, and comparing the survivors compares two different "
                "populations"
            )

    verdict = Verdict.UNDERPOWERED
    if g_obs < CONTROL_MIN_ARM or c_obs < CONTROL_MIN_ARM:
        notes.append(
            f"each arm needs at least {CONTROL_MIN_ARM} wallets with forward data to detect "
            f"anything; we have {g_obs} graded and {c_obs} control"
        )
    elif p is None or diff is None:
        notes.append("the difference in forward edge is not computable from these cohorts")
    elif p < GATE3_CONTROL_ALPHA and diff > 0:
        verdict = Verdict.PASS
    elif diff <= 0:
        verdict = Verdict.FAIL
        notes.append(
            "the graded cohort did not out-earn its matched control. Per the protocol this "
            "removes the wallet grader from the entry path; the literature gives no basis "
            "for keeping it there on faith"
        )
    else:
        notes.append(
            f"the graded cohort is ahead by {diff} but p={p:.4f} does not clear "
            f"{GATE3_CONTROL_ALPHA}"
            + (f"; the smallest difference these cohorts could detect is {mde:.4f}" if mde else "")
        )

    return ControlArmReport(
        cohort_id=cohort_id, verdict=verdict, frozen_ms=frozen_ms, horizon_ms=stop - frozen_ms,
        chain=chain, graded_frozen=graded_n, control_frozen=control_n, graded_observed=g_obs,
        control_observed=c_obs, graded_edge=g_edge, control_edge=c_edge, difference=diff,
        p_value=p, welch_t=t_stat, minimum_detectable=mde, attrition_gap=attrition_gap,
        matched_on=matched_on, unmatched=unmatched, notes=notes,
    )


# --------------------------------------------------------------------------------------
# 3. the six gates, strictly ordered
# --------------------------------------------------------------------------------------

GATE_NAMES: tuple[str, ...] = (
    "data-integrity",
    "execution-realism",
    "statistics",
    "shadow",
    "micro-live",
    "scale",
)


class GateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ordinal: int
    gate: str
    verdict: Verdict
    sample_n: int = 0
    criteria: list[Criterion] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    checked_ms: int = Field(default_factory=now_ms)
    expires_ms: int | None = None

    @property
    def failures(self) -> list[str]:
        return [c.name for c in self.criteria if c.verdict is Verdict.FAIL]

    @property
    def unmeasured(self) -> list[str]:
        return [c.name for c in self.criteria if c.verdict is Verdict.UNDERPOWERED]


def _finish(
    ordinal: int, criteria: Sequence[Criterion], notes: Sequence[str], sample_n: int,
    checked_ms: int,
) -> GateReport:
    verdict = combine(criteria)
    return GateReport(
        ordinal=ordinal, gate=GATE_NAMES[ordinal], verdict=verdict, sample_n=sample_n,
        criteria=list(criteria), notes=list(notes), checked_ms=checked_ms,
        expires_ms=checked_ms + GATE_TTL_MS if verdict is Verdict.PASS else None,
    )


def _share(numerator: int | None, denominator: int | None) -> float | None:
    if not denominator:
        return None
    return (numerator or 0) / denominator


def _lookahead_decisions(conn: sqlite3.Connection) -> int | None:
    """Decisions citing a signal that was recorded after the decision was made.

    The automated leakage test, and the one number in Gate 0 that catches the failure mode
    the research calls the biggest trap: conditioning on information the decision did not
    have. Uses SQLite's JSON1 to expand ``signals_json`` rather than a substring match
    against every signal ever recorded, which would be a cross join. Falls back to a bounded
    Python walk where JSON1 is unavailable, and returns ``None`` if neither works — which
    makes the criterion unevaluable rather than clean.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(DISTINCT d.decision_id) AS n FROM decisions d "
            "JOIN json_each(d.signals_json) j ON 1=1 "
            "JOIN signals s ON s.signal_id = j.value WHERE s.created_ms > d.ts_ms",
            (),
        )
        return int(row["n"]) if row else 0
    except sqlite3.Error as exc:
        log.debug("JSON1 unavailable for the leakage test, walking in Python: %s", exc)
    try:
        leaks = 0
        for d in fetch_all(
            conn, "SELECT decision_id, ts_ms, signals_json FROM decisions LIMIT 20000", ()
        ):
            ids = [str(s) for s in (jload(d.get("signals_json"), []) or [])]
            if not ids:
                continue
            placeholders = ",".join("?" * len(ids))
            hit = fetch_one(
                conn,
                f"SELECT COUNT(*) AS n FROM signals WHERE signal_id IN ({placeholders}) "
                "AND created_ms > ?",
                [*ids, int(d["ts_ms"])],
            )
            if hit and int(hit["n"]) > 0:
                leaks += 1
        return leaks
    except sqlite3.Error as exc:  # pragma: no cover - both paths failing is a broken db
        log.warning("leakage test could not run: %s", exc)
        return None


def gate_data_integrity(
    conn: sqlite3.Connection | None = None, *, checked_ms: int | None = None
) -> GateReport:
    """Gate 0. Is the data we would be testing on real, point-in-time and complete?

    Strictly prior to everything else: a backtest with a leaked feature or a
    survivorship-pruned universe sails through every statistical test there is. The
    cautionary case is arXiv:2607.02823, where a graduation model scored AUROC 0.859 in
    development and 0.464 a month later, and the cause was label provenance rather than
    modelling.
    """
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    criteria: list[Criterion] = []
    notes: list[str] = []

    tok = fetch_one(
        c, "SELECT COUNT(*) AS n, SUM(created_ms IS NOT NULL) AS onchain FROM tokens", ()
    ) or {}
    criteria.append(
        criterion(
            "token_universe_onchain_share",
            _share(tok.get("onchain"), tok.get("n")),
            GATE0_MIN_ONCHAIN_SHARE,
            basis=f"{tok.get('onchain') or 0}/{tok.get('n') or 0} tokens carry an on-chain "
                  "creation time rather than a listing-API first-seen",
            missing="no tokens recorded, so the universe cannot be audited for survivorship",
        )
    )

    swap = fetch_one(
        c, "SELECT COUNT(*) AS n, SUM(slot IS NOT NULL) AS with_slot FROM swaps", ()
    ) or {}
    criteria.append(
        criterion(
            "clock_discipline_slot_share",
            _share(swap.get("with_slot"), swap.get("n")),
            GATE0_MIN_ONCHAIN_SHARE,
            basis=f"{swap.get('with_slot') or 0}/{swap.get('n') or 0} swaps carry a slot; a "
                  "vendor wall-clock is not a point in the chain's own ordering",
            missing="no swaps recorded, so clock discipline cannot be checked",
        )
    )

    total_dec = (fetch_one(c, "SELECT COUNT(*) AS n FROM decisions", ()) or {}).get("n") or 0
    leaks = _lookahead_decisions(c)
    criteria.append(
        criterion(
            "lookahead_decisions",
            float(leaks) if (total_dec and leaks is not None) else None,
            0.0,
            "le",
            basis=f"{leaks} of {total_dec} decisions cite a signal recorded after the "
                  "decision was made",
            missing="no decisions recorded, so point-in-time discipline cannot be checked",
        )
    )

    trades = (fetch_one(c, "SELECT COUNT(*) AS n FROM trades", ()) or {}).get("n") or 0
    linked = (
        fetch_one(
            c,
            "SELECT COUNT(*) AS n FROM trades t JOIN decisions d ON d.decision_id = t.decision_id "
            "WHERE d.ts_ms <= t.opened_ms",
            (),
        )
        or {}
    ).get("n") or 0
    criteria.append(
        criterion(
            "trade_decision_linkage",
            _share(linked, trades),
            0.99,
            basis=f"{linked}/{trades} closed trades trace back to a decision recorded at or "
                  "before the position opened",
            missing="no closed trades, so trade-level provenance is unmeasured — this is "
                    "absent evidence, not a clean bill of health",
        )
    )

    notes.append(
        "what this gate cannot check from our own tables: whether the universe matches an "
        "independent count of launches for a historical week to within 2%, and whether every "
        "graduated/rugged label was derived from chain state rather than a collector. Both "
        "need an external reconciliation run that does not exist yet"
    )
    return _finish(0, criteria, notes, int(trades), ts)


def gate_execution_realism(
    conn: sqlite3.Connection | None = None,
    *,
    mode: str | None = None,
    lane: Lane | str | None = None,
    checked_ms: int | None = None,
) -> GateReport:
    """Gate 1. Are the fills and costs we recorded something a market would have given us?

    No statistical gate can rescue a wrong fill model. On a bonding curve your buy *is* the
    next tick, so a trade recorded with no slippage and no fee is not a cheap trade, it is a
    trade that was never simulated.
    """
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    trades = metrics.load_trades(c, mode=mode, lane=lane)
    criteria: list[Criterion] = []
    notes: list[str] = []
    n = len(trades)

    with_slip = sum(1 for t in trades if t.get("slippage_bps") is not None)
    criteria.append(
        criterion(
            "slippage_recorded_share", _share(with_slip, n), GATE1_MIN_SLIPPAGE_SHARE,
            basis=f"{with_slip}/{n} closed trades carry a measured slippage",
            missing="no closed trades: the fill model is unexercised, which is not the same "
                    "as correct",
        )
    )

    with_fee = sum(1 for t in trades if (_dec(t.get("fees_native"))) > 0)
    criteria.append(
        criterion(
            "fee_recorded_share", _share(with_fee, n), GATE1_MIN_FEE_SHARE,
            basis=f"{with_fee}/{n} closed trades charged a non-zero fee; the curve alone is "
                  "1.25% each way before priority fees and tips",
            missing="no closed trades, so the cost model is unexercised",
        )
    )

    edge_ratio: float | None = None
    if trades:
        gross_cost = sum((metrics.cost_native(t) for t in trades), ZERO)
        friction = sum((_dec(t.get("fees_native"))) for t in trades)
        slip_bps = [int(t["slippage_bps"]) for t in trades if t.get("slippage_bps") is not None]
        if slip_bps and gross_cost > 0:
            mean_bps = Decimal(sum(slip_bps)) / Decimal(len(slip_bps))
            friction += gross_cost * mean_bps / Decimal(10_000)
        mean_ret = metrics.expectancy_r(trades)
        if gross_cost > 0 and friction > 0 and mean_ret is not None:
            friction_frac = friction / gross_cost
            edge_ratio = float(mean_ret / friction_frac) if friction_frac > 0 else None
    criteria.append(
        criterion(
            "edge_over_modelled_friction", edge_ratio, GATE1_MIN_EDGE_OVER_FRICTION,
            basis="mean per-trade return divided by mean modelled friction (fees plus mean "
                  "slippage on cost). The research asks for 2x before anything is promoted",
            missing="no trades with both a cost and a friction to divide, so the ratio that "
                    "decides this gate does not exist yet",
        )
    )

    orders = fetch_one(
        c,
        "SELECT COUNT(*) AS n, SUM(state = 'filled') AS filled FROM orders "
        "WHERE state IN ('filled','failed','expired','cancelled')",
        (),
    ) or {}
    terminal = int(orders.get("n") or 0)
    land_rate = _share(orders.get("filled"), terminal)
    criteria.append(
        criterion(
            "land_rate_measured", float(terminal) if terminal else None, 30.0,
            basis=f"{terminal} terminal orders, land rate "
                  + (f"{land_rate:.2%}" if land_rate is not None else "unknown")
                  + ". A landing-probability draw has to be calibrated to a measured rate, "
                    "and a transaction that does not land is a paper trade with no live "
                    "counterpart",
            missing="no terminal orders, so the land rate is unmeasured and the inclusion "
                    "model has nothing to be calibrated against",
        )
    )

    notes.append(
        "not checkable here and still required by the protocol: an adversarial replay at "
        "+1 and +2 slots, and an independently reimplemented fill engine agreeing with the "
        "live pre-trade simulator to within 50 bps over a 1,000-trade replay "
        "(arXiv:2603.20319 measured up to 3.71% engine-to-engine divergence in a far easier "
        "setting, driven by how each one implements costs)"
    )
    return _finish(1, criteria, notes, n, ts)


def statistical_verdict(
    returns: Sequence[Decimal],
    *,
    trials: int,
    timestamps: Sequence[int] | None = None,
    trial_matrix: Sequence[Sequence[float]] | None = None,
    min_trades: int = GATE2_MIN_TRADES,
    min_regimes: int = GATE2_MIN_REGIMES,
    seed: int = SEED,
    include_pbo: bool = True,
    bootstrap_draws: int = 2000,
) -> tuple[list[Criterion], dict[str, Any]]:
    """The statistical core, shared by Gate 2 and by the falsification runs.

    Pure arithmetic over a return series: no database, no configuration. That matters
    because the placebo has to run through *exactly* this code for its negative to mean
    anything.

    ``include_pbo=False`` is only for the synthetic falsification arms, which have no trial
    matrix by construction. A real evaluation leaves it on, where an absent matrix makes the
    criterion unevaluable and the gate underpowered — which is the correct answer, since we
    keep no T-by-N matrix for the book as a whole.
    """
    criteria: list[Criterion] = []
    m: dict[str, Any] = {}
    n = len(returns)

    criteria.append(
        criterion(
            "closed_trades", float(n) if n else None, float(min_trades),
            basis=f"{n} closed trades",
            missing="no closed trades",
        )
    )

    regimes: float | None = None
    if timestamps and len(timestamps) >= 2:
        span_weeks = (max(timestamps) - min(timestamps)) / WEEK_MS
        regimes = max(1.0, math.ceil(span_weeks / REGIME_WEEKS))
        m["span_weeks"] = span_weeks
    criteria.append(
        criterion(
            "regimes_spanned", regimes, float(min_regimes),
            basis="a regime boundary is a venue fee/infra change or eight weeks, whichever "
                  "comes first; we can only see the calendar half of that",
            missing="not enough dated trades to measure a span",
        )
    )

    dsr, dsr_notes = gates.deflated_sharpe([float(v) for v in returns], trials)
    m["dsr"] = dsr
    m["trials"] = trials
    m["dsr_notes"] = dsr_notes
    criteria.append(
        criterion(
            "deflated_sharpe", dsr, GATE2_DSR_MIN, "gt",
            basis=f"N={trials} from the trial registry" + ("; " + "; ".join(dsr_notes) if dsr_notes else ""),
            missing="the deflated Sharpe is not computable on this series",
        )
    )

    if include_pbo:
        pbo = gates.pbo_cscv(trial_matrix) if trial_matrix else None
        m["pbo"] = pbo
        criteria.append(
            criterion(
                "pbo_cscv", pbo, GATE2_PBO_MAX, "lt",
                basis="probability that the in-sample winner is at or below the out-of-sample "
                      "median",
                missing="no T-by-N trial matrix exists for this arm, so PBO cannot be "
                        "computed. It is unevaluable, not satisfied",
            )
        )

    trimmed = delete_best(returns)
    tail_total = float(sum(trimmed, ZERO)) if trimmed else None
    m["cumulative_return"] = float(sum(returns, ZERO)) if returns else None
    m["cumulative_return_ex_best_5pct"] = tail_total
    criteria.append(
        criterion(
            "positive_after_deleting_best_5pct", tail_total, 0.0, "gt",
            basis=f"cumulative return over the {len(trimmed)} trades left after removing the "
                  f"best {len(returns) - len(trimmed)}",
            missing="no trades to trim",
        )
    )

    boot_t: float | None = None
    if timestamps and len(timestamps) == n and n >= 2:
        boot_t = block_bootstrap_t(
            list(zip(timestamps, returns, strict=True)), draws=bootstrap_draws, seed=seed
        )
    m["bootstrap_t"] = boot_t
    m["z_deflated"] = significance_z(trials)
    criteria.append(
        criterion(
            "block_bootstrap_t", boot_t, significance_z(trials),
            basis="t of the mean with whole days resampled, against the deflated threshold. "
                  "The naive t treats same-day trades as independent observations and they "
                  "are not",
            missing=f"fewer than {MIN_BOOTSTRAP_BLOCKS} distinct trading days, so the "
                    "bootstrap cannot estimate between-day variance and would fail towards "
                    "confidence if it tried",
        )
    )
    return criteria, m


def gate_statistics(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    mode: str | None = None,
    checked_ms: int | None = None,
    seed: int = SEED,
) -> GateReport:
    """Gate 2. The statistical gate, on a Gate-1-compliant sample only.

    Includes a falsification check as a pass criterion: if the identical arithmetic cannot
    produce a negative on a placebo, a positive from it is not evidence of anything.
    """
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    trades = metrics.load_trades(c, mode=mode, lane=lane)
    returns = metrics.returns_of(trades)
    stamps = [int(t["closed_ms"]) for t in trades]
    trials = honest_trials(c, lane)

    criteria, m = statistical_verdict(returns, trials=trials, timestamps=stamps, seed=seed)
    falsification = falsification_suite(trials=trials, seed=seed)
    criteria.append(
        criterion(
            "harness_can_return_no", 1.0 if falsification.harness_can_say_no else 0.0, 1.0,
            basis=falsification.headline,
            missing="the falsification run did not complete",
        )
    )
    notes = [
        f"deflation used N={trials} from the trial registry, which counts every threshold "
        "that was ever nudged and re-run, not only the experiments somebody filed",
        f"cumulative return {m.get('cumulative_return')}, and "
        f"{m.get('cumulative_return_ex_best_5pct')} after deleting the best 5%",
        f"bootstrap t {m.get('bootstrap_t')} against a deflated threshold of "
        f"{m.get('z_deflated'):.2f}" if m.get("bootstrap_t") is not None
        else "no bootstrap t: fewer than two distinct trading days",
    ]
    notes.extend(m.get("dsr_notes") or [])
    return _finish(2, criteria, notes, len(returns), ts)


def gate_shadow(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    control: ControlArmReport | None = None,
    paper_edge: Decimal | None = None,
    checked_ms: int | None = None,
) -> GateReport:
    """Gate 3. Forward paper against live data: landing, latency, adverse selection, drift.

    Carries the wallet-grading control arm, which is the criterion the whole wallet pillar
    depends on. If it fails, the grader comes off the entry path.
    """
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    trades = metrics.load_trades(c, mode="shadow", lane=lane)
    returns = metrics.returns_of(trades)
    stamps = [int(t["closed_ms"]) for t in trades]
    criteria: list[Criterion] = []
    notes: list[str] = []
    n = len(returns)

    criteria.append(
        criterion(
            "shadow_trades", float(n) if n else None, float(GATE3_MIN_TRADES),
            basis=f"{n} closed shadow trades",
            missing="no closed shadow trades",
        )
    )
    weeks = (max(stamps) - min(stamps)) / WEEK_MS if len(stamps) >= 2 else None
    criteria.append(
        criterion(
            "shadow_weeks", weeks, GATE3_MIN_WEEKS,
            basis="calendar span of the shadow window",
            missing="fewer than two dated shadow trades",
        )
    )

    by_week: dict[int, Decimal] = {}
    for stamp, value in zip(stamps, returns, strict=True):
        by_week[stamp // WEEK_MS] = by_week.get(stamp // WEEK_MS, ZERO) + value
    positive_share = (
        sum(1 for v in by_week.values() if v > 0) / len(by_week) if by_week else None
    )
    criteria.append(
        criterion(
            "positive_week_share", positive_share, GATE3_MIN_POSITIVE_WEEK_SHARE,
            basis=f"{sum(1 for v in by_week.values() if v > 0)}/{len(by_week)} weeks positive",
            missing="no weekly buckets to check sign stability on",
        )
    )
    total = sum(by_week.values(), ZERO)
    max_share = (
        float(max(by_week.values()) / total) if by_week and total > 0 else None
    )
    criteria.append(
        criterion(
            "max_single_week_share", max_share, GATE3_MAX_SINGLE_WEEK_SHARE, "le",
            basis="largest share of total edge contributed by one week",
            missing="no positive total to apportion across weeks",
        )
    )

    degradation: float | None = None
    if paper_edge is not None and paper_edge > 0:
        shadow_edge = metrics.expectancy_r(trades)
        if shadow_edge is not None:
            degradation = float(shadow_edge / paper_edge)
    criteria.append(
        criterion(
            "shadow_over_paper_edge", degradation, GATE3_MIN_DEGRADATION,
            basis="shadow edge per trade as a fraction of the Gate-2 shrunk paper edge",
            missing="no Gate-2 paper edge has been established, so there is nothing to "
                    "degrade from",
        )
    )

    # A gate evaluates; it does not create the evidence it is evaluating. Freezing a cohort
    # here would mean every run of the protocol minted a new "frozen at t" cohort with no
    # forward window yet, which is both a surprising write and a guaranteed underpowered
    # answer. So: use what was handed in, else the latest genuine freeze, else say we have
    # not run the test.
    arm = control
    if arm is None:
        existing = latest_cohort(c)
        arm = control_arm(c, cohort_id=existing) if existing else None
    if arm is None:
        criteria.append(
            Criterion(
                name="wallet_grading_control_arm",
                verdict=Verdict.UNDERPOWERED,
                threshold=GATE3_CONTROL_ALPHA,
                comparison="lt",
                basis="no wallet cohort has ever been frozen, so the forward test the whole "
                      "wallet pillar depends on has not been started. Run `kaiba validate "
                      "freeze` to fix t; until then this is unmeasured, not satisfied",
            )
        )
        notes.append(
            "wallet grading is an instrumented hypothesis with no forward test running: no "
            "published autocorrelation study, no forward-tracked cohort with a control, no "
            "vendor out-of-sample validation, and now none of our own either"
        )
    else:
        criteria.append(
            Criterion(
                name="wallet_grading_control_arm",
                verdict=arm.verdict,
                value=arm.p_value,
                threshold=GATE3_CONTROL_ALPHA,
                comparison="lt",
                basis=arm.headline,
            )
        )
        notes.extend(arm.notes[:4])
    notes.append(
        "the filter-only arm is reported separately and is not a criterion here: exclusion "
        "filters clear a much lower evidentiary bar than entry signals and may pass when the "
        "entry signal fails, which is a usable result rather than a partial one"
    )
    return _finish(3, criteria, notes, n, ts)


def gate_micro_live(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    shadow_edge: Decimal | None = None,
    checked_ms: int | None = None,
) -> GateReport:
    """Gate 4. Real money, hard-capped, with realised-minus-expected slippage as the headline."""
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    trades = metrics.load_trades(c, mode="live", lane=lane)
    returns = metrics.returns_of(trades)
    criteria: list[Criterion] = []
    n = len(returns)

    criteria.append(
        criterion(
            "live_trades", float(n) if n else None, float(GATE4_MIN_TRADES),
            basis=f"{n} closed live trades",
            missing="no closed live trades",
        )
    )
    trimmed = delete_best(returns)
    criteria.append(
        criterion(
            "live_positive_after_deleting_best_5pct",
            float(sum(trimmed, ZERO)) if trimmed else None, 0.0, "gt",
            basis="cumulative live return with the best 5% of trades removed",
            missing="no live trades to trim",
        )
    )
    ratio: float | None = None
    if shadow_edge is not None and shadow_edge > 0:
        live_edge = metrics.expectancy_r(trades)
        if live_edge is not None:
            ratio = float(live_edge / shadow_edge)
    criteria.append(
        criterion(
            "live_over_shadow_edge", ratio, GATE4_MIN_DEGRADATION,
            basis="live edge per trade as a fraction of the Gate-3 shadow edge",
            missing="no Gate-3 shadow edge to compare against",
        )
    )
    notes = [
        "standing kill conditions, any one of which halts trading and none of which this "
        "report can enforce: daily loss over budget; land rate down more than 30% from the "
        "Gate-3 baseline; realised slippage over 2x modelled for 20 consecutive trades; any "
        "venue fee or infrastructure change; 100 consecutive trades with no new edge accrual",
    ]
    return _finish(4, criteria, notes, n, ts)


def gate_scale(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    checked_ms: int | None = None,
) -> GateReport:
    """Gate 5. Size up in steps of at most 1.5x, 150 trades each, impact re-measured."""
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    trades = metrics.load_trades(c, mode="live", lane=lane)
    criteria: list[Criterion] = []

    sizes = [metrics.cost_native(t) for t in trades if metrics.cost_native(t) > 0]
    step_multiple: float | None = None
    if len(sizes) >= 2:
        step_multiple = float(max(sizes) / min(sizes))
    criteria.append(
        criterion(
            "max_size_step_multiple", step_multiple, GATE5_MAX_STEP_MULTIPLE, "le",
            basis="largest over smallest live position cost. A crude proxy: we keep no "
                  "explicit size-step ledger, so a gradual ramp and one jump look identical "
                  "here",
            missing="fewer than two live positions with a cost, so no step exists to measure",
        )
    )
    criteria.append(
        criterion(
            "trades_at_current_size", float(len(sizes)) if sizes else None,
            float(GATE5_MIN_TRADES_PER_STEP),
            basis=f"{len(sizes)} live trades recorded",
            missing="no live trades at any size",
        )
    )
    notes = [
        "a failed step reverts to the previous size permanently, not temporarily; nothing in "
        "this module can enforce that, it is an operator rule",
    ]
    return _finish(5, criteria, notes, len(trades), ts)


class ProtocolReport(BaseModel):
    """The six gates in order, with the power report as the headline above them."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    lane: str | None = None
    verdict: Verdict = Verdict.UNDERPOWERED
    power: PowerReport
    gates: list[GateReport] = Field(default_factory=list)
    falsification: FalsificationReport | None = None
    notes: list[str] = Field(default_factory=list)
    created_ms: int = Field(default_factory=now_ms)
    expires_ms: int | None = None

    @property
    def headline(self) -> str:
        blocked = sum(1 for g in self.gates if g.verdict is Verdict.BLOCKED)
        first = next((g for g in self.gates if g.verdict is not Verdict.PASS), None)
        stopped = "" if first is None else f" stopped at gate {first.ordinal} ({first.gate})"
        return f"{self.verdict.value}{stopped}; {blocked} later gate(s) never reached. {self.power.headline}"

    def record(self, conn: sqlite3.Connection | None = None) -> str:
        c = conn or get_conn()
        _record_run(
            c, kind="gates", lane=self.lane, verdict=self.verdict,
            sample_n=self.power.closed_trades, summary=self.headline,
            payload={"power": self.power.model_dump(mode="json"), "notes": self.notes},
            run_id=self.run_id, expires_ms=self.expires_ms,
        )
        for g in self.gates:
            c.execute(
                "INSERT INTO validation_gates (run_id, ordinal, gate, verdict, sample_n, "
                "criteria_json, notes_json, created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id, g.ordinal, g.gate, g.verdict.value, g.sample_n,
                    jdump([c_.model_dump(mode="json") for c_ in g.criteria]), jdump(g.notes),
                    g.checked_ms, g.expires_ms,
                ),
            )
        return self.run_id


def run_gates(
    conn: sqlite3.Connection | None = None,
    *,
    lane: Lane | str | None = None,
    control: ControlArmReport | None = None,
    record: bool = False,
    checked_ms: int | None = None,
    seed: int = SEED,
) -> ProtocolReport:
    """Run the protocol in order and stop at the first gate that does not pass.

    Later gates are marked ``BLOCKED`` rather than evaluated, because the whole point of an
    ordered protocol is that a later gate cannot compensate for an earlier one. A statistical
    result computed on data that failed the integrity gate is not a weaker result, it is a
    different claim about a different dataset.
    """
    c = conn or get_conn()
    ts = checked_ms if checked_ms is not None else now_ms()
    lane_name = None if lane is None else (lane.value if isinstance(lane, Lane) else str(lane))
    power = power_report(c, lane=lane, as_of_ms=ts)
    falsification = falsification_suite(trials=honest_trials(c, lane), seed=seed)

    builders = (
        lambda: gate_data_integrity(c, checked_ms=ts),
        lambda: gate_execution_realism(c, lane=lane, checked_ms=ts),
        lambda: gate_statistics(c, lane=lane, checked_ms=ts, seed=seed),
        lambda: gate_shadow(c, lane=lane, control=control, checked_ms=ts),
        lambda: gate_micro_live(c, lane=lane, checked_ms=ts),
        lambda: gate_scale(c, lane=lane, checked_ms=ts),
    )

    reports: list[GateReport] = []
    stopped = False
    for ordinal, build in enumerate(builders):
        if stopped:
            reports.append(
                GateReport(
                    ordinal=ordinal, gate=GATE_NAMES[ordinal], verdict=Verdict.BLOCKED,
                    checked_ms=ts,
                    notes=[f"gate {reports[-1].ordinal} did not pass; this gate was not run"],
                )
            )
            continue
        report = build()
        reports.append(report)
        if report.verdict is not Verdict.PASS:
            stopped = True

    verdict = Verdict.PASS
    if any(g.verdict is Verdict.FAIL for g in reports):
        verdict = Verdict.FAIL
    elif any(g.verdict is not Verdict.PASS for g in reports):
        verdict = Verdict.UNDERPOWERED

    notes = [
        f"gate expiry: a pass is valid for {REGIME_WEEKS:.0f} weeks or until the next "
        "venue-level change, whichever is sooner. The eight weeks is a working figure",
        falsification.headline,
    ]
    run_id = "val_" + digest({"lane": lane_name, "ts": ts})[:20]
    out = ProtocolReport(
        run_id=run_id, lane=lane_name, verdict=verdict, power=power, gates=reports,
        falsification=falsification, notes=notes, created_ms=ts,
        expires_ms=ts + GATE_TTL_MS if verdict is Verdict.PASS else None,
    )
    if record:
        out.record(c)
    return out


# --------------------------------------------------------------------------------------
# 4. falsification: prove the harness can say no (and, separately, that it can say yes)
# --------------------------------------------------------------------------------------


class FalsificationReport(BaseModel):
    """Placebo and positive-control runs through the identical statistical core.

    A validation suite that has never returned "no" is not evidence of anything, and one
    that can only return "no" is not either. Both directions are checked, on synthetic data,
    with a fixed seed, so the result is reproducible and cannot drift with the database.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict = Verdict.UNDERPOWERED
    harness_can_say_no: bool = False
    harness_can_say_yes: bool = False
    placebo_random_verdict: Verdict = Verdict.UNDERPOWERED
    placebo_shuffled_verdict: Verdict = Verdict.UNDERPOWERED
    positive_control_verdict: Verdict = Verdict.UNDERPOWERED
    placebo_selection_hit_rate: float | None = None
    trials: int = 1
    seed: int = SEED
    notes: list[str] = Field(default_factory=list)

    @property
    def headline(self) -> str:
        if self.verdict is Verdict.PASS:
            return (
                "Falsification OK: the placebo lane fails and a synthetic true edge passes, "
                "so a negative from this harness means something."
            )
        broken = []
        if not self.harness_can_say_no:
            broken.append("a random-entry placebo PASSED the statistical gate")
        if not self.harness_can_say_yes:
            broken.append("a synthetic true edge did not pass")
        return "Falsification problem: " + "; ".join(broken or ["not run"])


def synthetic_returns(
    n: int, *, edge: bool, seed: int = SEED, mean_shift: float = 0.0
) -> list[Decimal]:
    """A per-trade return series with or without a real edge, for the falsification runs.

    ``edge=False`` is the placebo: the research's fat-tail shape at the **fee-adjusted**
    null. With 1.25% curve fees each way plus priority fees and tips, the expected return of
    a randomly chosen memecoin round trip is meaningfully below zero, so "beats zero" is the
    wrong bar and the placebo is drawn below it.

    ``edge=True`` is the positive control, and it is deliberately **not** fat-tailed: a
    clean 45% hit rate at 2:1. Its job is to prove the gate is not wired shut, so it must be
    able to clear every criterion including the delete-the-best-5% rule. A fat-tailed true
    edge could not: profile C's whole expectancy lives in its top 2%, so trimming 5% of it
    turns +690R into −3,000R. That is the rule working as designed, not a bug, and it is
    exactly why the positive control has to be a different shape from the market.
    """
    rng = random.Random(seed)
    out: list[Decimal] = []
    for _ in range(max(n, 0)):
        u = rng.random()
        if edge:
            value = 2.0 if u < 0.45 else -1.0
        else:
            value = 30.0 if u < 0.0125 else (0.5 if u < 0.30 else -1.0)
        out.append(Decimal(str(round(value + mean_shift, 6))))
    return out


def _synthetic_stamps(n: int, *, start_ms: int = 1_700_000_000_000, per_day: int = 12) -> list[int]:
    """Timestamps that spread ``n`` trades over enough calendar to span three regimes.

    ``per_day`` is low on purpose: at 12 a day, 2,400 trades cover 200 days, which is the
    four eight-week regimes Gate 2 asks for. A denser stamp would make every synthetic run
    fail on span rather than on statistics, which would make the falsification vacuous.
    """
    return [start_ms + (i // max(per_day, 1)) * DAY_MS + (i % max(per_day, 1)) * 3_600_000
            for i in range(n)]


def falsification_suite(
    *,
    trials: int = 1,
    n: int = 2400,
    seed: int = SEED,
    observed: Sequence[Decimal] | None = None,
    attempts: int = 20,
) -> FalsificationReport:
    """Run the placebo lanes and the positive control through :func:`statistical_verdict`.

    Everything here goes through the *identical* arithmetic Gate 2 uses. That is the whole
    point: a negative produced by a different code path proves nothing about the path that
    will one day produce a positive.

    Three checks:

    * **random entries** — the fat-tail shape at the fee-adjusted null. It must not pass. If
      it does, the gate is broken, not the market.
    * **selection placebo** — repeatedly keep a random subset of the placebo, the size a
      "filtered" strategy would have, holding each subset's original timestamps so the
      calendar span is preserved and the subset fails on statistics rather than on shape.
      The reported hit rate is how often pure selection manufactures a pass, which is the
      multiple-testing problem made visible.
    * **positive control** — a clean synthetic edge with a sample big enough to see it. It
      must pass, or the harness cannot say yes and its negatives are worthless too.

    PBO is excluded from all three: it needs a T-by-N trial matrix that a single synthetic
    series does not have, and leaving an unevaluable criterion in would make every arm
    underpowered by construction and the test vacuous.
    """
    notes: list[str] = []
    stamps = _synthetic_stamps(n)

    def verdict_of(returns: Sequence[Decimal], ts: Sequence[int]) -> Verdict:
        criteria, _ = statistical_verdict(
            returns, trials=trials, timestamps=ts, seed=seed, include_pbo=False,
            bootstrap_draws=400,
        )
        return combine(criteria)

    placebo = synthetic_returns(n, edge=False, seed=seed)
    placebo_verdict = verdict_of(placebo, stamps)

    pool = list(observed) if observed else synthetic_returns(n, edge=False, seed=seed + 1)
    pool_stamps = _synthetic_stamps(len(pool))
    keep = min(len(pool), max(GATE2_MIN_TRADES, len(pool) // 4))
    rng = random.Random(seed + 2)
    tries = max(int(attempts), 1)
    passes = 0
    for _ in range(tries):
        # Sample indices, not values, so each subset keeps its own timestamps and therefore
        # its own calendar span. Sampling values and re-stamping them would compress every
        # subset into a window too short to span three regimes, and the criterion that then
        # failed would be the clock, not the statistics.
        picks = sorted(rng.sample(range(len(pool)), keep))
        if verdict_of([pool[i] for i in picks], [pool_stamps[i] for i in picks]) is Verdict.PASS:
            passes += 1
    selection_rate = passes / tries
    shuffled_verdict = Verdict.FAIL if passes else Verdict.PASS
    if passes:
        notes.append(
            f"{passes}/{tries} random subsets of a zero-edge series cleared the gate; "
            "selection alone manufactures significance at this sample size, which is the "
            "multiple-testing problem the deflation exists to absorb"
        )

    control = synthetic_returns(n, edge=True, seed=seed + 3)
    control_criteria, _ = statistical_verdict(
        control, trials=trials, timestamps=stamps, seed=seed, include_pbo=False,
        bootstrap_draws=400,
    )
    control_verdict = combine(control_criteria)

    can_say_no = placebo_verdict is not Verdict.PASS and passes == 0
    can_say_yes = control_verdict is Verdict.PASS
    if not can_say_no:
        notes.append(
            "a zero-edge placebo cleared this gate. Nothing downstream of it is evidence "
            "until that is fixed: a suite that cannot return 'no' has not tested anything"
        )
    if not can_say_yes:
        notes.append(
            "the positive control did not pass, so the gate may be refusing everything "
            "rather than discriminating: "
            + "; ".join(c.name for c in control_criteria if c.verdict is not Verdict.PASS)
        )
    verdict = Verdict.PASS if (can_say_no and can_say_yes) else Verdict.FAIL

    return FalsificationReport(
        verdict=verdict, harness_can_say_no=can_say_no, harness_can_say_yes=can_say_yes,
        placebo_random_verdict=placebo_verdict, placebo_shuffled_verdict=shuffled_verdict,
        positive_control_verdict=control_verdict, placebo_selection_hit_rate=selection_rate,
        trials=trials, seed=seed, notes=notes,
    )


# --------------------------------------------------------------------------------------
# 5. expiry and persistence
# --------------------------------------------------------------------------------------


def _record_run(
    conn: sqlite3.Connection | None,
    *,
    kind: str,
    lane: str | None,
    verdict: Verdict,
    sample_n: int,
    summary: str,
    payload: Mapping[str, Any],
    run_id: str | None = None,
    expires_ms: int | None = None,
) -> str:
    c = conn or get_conn()
    ts = now_ms()
    rid = run_id or (kind[:4] + "_" + digest({"kind": kind, "lane": lane, "ts": ts})[:20])
    expiry = expires_ms
    if expiry is None and verdict is Verdict.PASS:
        expiry = ts + GATE_TTL_MS
    c.execute(
        "INSERT OR REPLACE INTO validation_runs (run_id, kind, lane, verdict, sample_n, "
        "created_ms, expires_ms, summary, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, kind, lane, verdict.value, int(sample_n), ts, expiry, summary[:1000],
         jdump(dict(payload))),
    )
    return rid


def is_expired(expires_ms: int | None, *, at_ms: int | None = None) -> bool:
    """A verdict with no expiry was never a pass; a pass past its expiry is not one either."""
    if expires_ms is None:
        return True
    return (at_ms if at_ms is not None else now_ms()) >= int(expires_ms)


def latest_runs(
    conn: sqlite3.Connection | None = None, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Recorded validation runs, newest first, with expiry resolved against now."""
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT run_id, kind, lane, verdict, sample_n, created_ms, expires_ms, summary "
        "FROM validation_runs ORDER BY created_ms DESC LIMIT ?",
        (int(limit),),
    )
    now = now_ms()
    for r in rows:
        r["expired"] = is_expired(r["expires_ms"], at_ms=now)
    return rows


def standing_verdict(
    conn: sqlite3.Connection | None = None, *, gate: str, at_ms: int | None = None
) -> Verdict:
    """The current verdict for one gate, with expiry applied.

    An expired pass is not a pass. The venue's fee schedule changed on 2026-09-01 and its
    dominant shred feed died on 2026-09-05; a verdict from before either is a verdict about
    a different game, and reading it as current is how a dead edge keeps its funding.
    """
    c = conn or get_conn()
    row = fetch_one(
        c,
        "SELECT verdict, expires_ms FROM validation_gates WHERE gate = ? "
        "ORDER BY created_ms DESC, id DESC LIMIT 1",
        (gate,),
    )
    if row is None:
        return Verdict.UNDERPOWERED
    verdict = Verdict(str(row["verdict"]))
    if verdict is Verdict.PASS and is_expired(row["expires_ms"], at_ms=at_ms):
        return Verdict.UNDERPOWERED
    return verdict


ProtocolReport.model_rebuild()


__all__ = [
    "CONTROL_MIN_ARM",
    "Criterion",
    "CohortSummary",
    "ControlArmReport",
    "FALLBACK_PROFILES",
    "FalsificationReport",
    "GATE_NAMES",
    "GATE_TTL_MS",
    "GateReport",
    "MATCH_COVARIATES",
    "PowerReport",
    "ProtocolReport",
    "REGIME_WEEKS",
    "TradeProfile",
    "UNMATCHED_CONFOUNDERS",
    "Verdict",
    "block_bootstrap_t",
    "combine",
    "control_arm",
    "criterion",
    "delete_best",
    "expected_max_z",
    "falsification_suite",
    "freeze_cohort",
    "gate_data_integrity",
    "gate_execution_realism",
    "gate_micro_live",
    "gate_scale",
    "gate_shadow",
    "gate_statistics",
    "honest_trials",
    "is_expired",
    "latest_runs",
    "minimum_detectable_effect",
    "permutation_p",
    "power_report",
    "run_gates",
    "significance_z",
    "standing_verdict",
    "statistical_verdict",
    "synthetic_returns",
    "trades_needed",
    "welch_t",
]
