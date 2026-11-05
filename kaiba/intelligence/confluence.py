"""One place where the evidenced variables are combined, with every weight traceable.

Why this module exists
----------------------
Variables are scattered across :mod:`kaiba.intelligence.dedup`,
:mod:`kaiba.intelligence.creators`, :mod:`kaiba.intelligence.concentration`,
:mod:`kaiba.ingest.token_flow` and the eight lanes. Each one knows its own number. Nothing
knew how much any of them was *worth*, and the lane thresholds that combined them were
invented. This module is the combination step, and its whole design constraint is that a
caller must be able to see not only the score but which evidence produced it.

The unit is nats of log-odds
----------------------------
Every weight is a **half log-odds separation in nats**, so a variable's full swing from its
worst state to its best state equals the separation the literature actually measured. For
copycat reuse that is::

    ln(0.0920 / 0.9080) - ln(0.0086 / 0.9914) = 2.4579 nats     (arXiv:2609.10246)

so the weight is 1.2290 and the variable spans +-1.2290. This is not a cosmetic choice: it
makes "how much should this move the answer" a published number rather than a preference,
and it makes the score additive in the only space where adding independent evidence is
correct. :attr:`ConfluenceScore.implied_graduation_evidenced` converts back to a
probability against a base rate, and is explicitly marked ESTIMATED because that step
assumes conditional independence that we have not verified.

Three tiers, kept apart
-----------------------
``MEASURED``  a published effect size, cited, with the arithmetic shown.
``DERIVED``   arithmetic on published constants, or a magnitude assigned by Rule U below.
``INVENTED``  no published support. The word appears in the provenance string and a test
              enforces it.

:func:`score` computes the evidenced score and the all-variables score in two separate
passes and reports the difference. If the invented variables move the answer by more than
the smallest measured effect in the set is worth, :attr:`ConfluenceScore.divergent` is set
and the caller is expected to say so out loud.

Rule U — a published direction without a published magnitude
------------------------------------------------------------
Several variables have a direction the literature establishes and a magnitude it never
states. Those get **the floor of the measured set** (the wash-trading half-separation,
0.4048 nats), not its mean and not the ceiling. Under-weighting a real effect costs less
than over-weighting an imagined one, and the floor is the only magnitude in the set that no
evidence contradicts. Raising these is the first thing a calibration fit should do.

Correlated inputs
-----------------
Two levels, because a naive scorer gets both wrong.

*Addresses to entities.* Five addresses funded by one wallet are one opinion.
:func:`kaiba.intelligence.entity.independent_entity_count` is what collapses them, and it is
used for the reported entity count. That count carries **zero weight**, because the
evidence behind "smart wallets bought" is a published null (arXiv:2602.14860). It is also
gated on the mint's trade tape being proved complete (:func:`kaiba.ingest.tape.completeness`):
counting the buyers in the slice of a tape we managed to pull is not counting a mint's
buyers, and on a partial tape the reading is reported as a floor with an UNAVAILABLE basis
rather than as a number the scorer may spend.

*Variables to groups.* Creator history and copycat status are not independent when the same
creator mass-produces copies; velocity, wash trading and bot share are all read off the same
swap stream over the same window. Declared groups therefore obey one rule: **a group can
never say more than its loudest member.** The group's algebraic sum is clipped to
``+-max|member contribution|``. We cannot measure the true correlations yet - there are zero
closed trades - so this is deliberately the conservative end: three agreeing members in one
group contribute what one of them would.

Unknown is never good
---------------------
A variable we could not establish contributes nothing, which would make ignorance identical
to a clean check. It is not. Missing evidenced weight is charged an explicit penalty,
bounded by the *smallest* measured effect in the set so that total ignorance is never worse
than one confirmed adverse measurement. The penalty is applied identically to both scores,
so the evidenced-vs-all difference isolates the invented variables and nothing else.

That handles ignorance one variable at a time. It does not handle ignorance in aggregate,
and the first live run showed why it has to: two mints on which we established *nothing*
came out positive, because an unvalidated 35% top-10 line and a $10,000 depth floor both
happened to clear. Over half the universe - 654 of 1,199 mints - establishes nothing, so
that is not a corner case; it is the half where a positive score is least earned. It is
also fail-open, which is the failure mode this project has already been bitten by twice: a
gate that looked closed and was not.

So the same rule applies at the aggregate. **A score may not be positive unless at least
one evidenced - non-INVENTED, non-zero-weight - variable was actually measured.** The
mechanism is asymmetric rather than a clamp: when nothing evidenced was measured, invented
thresholds may still *subtract* and may not *add*. Clearing a threshold nobody validated is
not evidence of anything; it means we checked a number against a line we made up, and a
line we made up cannot be the reason we bought something.

Nothing is silently rewritten. The withheld contribution stays visible on
:attr:`Contribution.raw_contribution_nats` with :attr:`Contribution.suppressed` set,
:attr:`ConfluenceScore.suppressed_invented` names every variable it hit, and
:attr:`ConfluenceScore.standing` separates a positive that rests on measured evidence from
one that rests on invented thresholds. The positive case is impossible when groundless, so
the rule holds by construction rather than by a final clamp on the number.

Calibration
-----------
Weights live in ``confluence_weights`` (migration 020) keyed by version, not in constants.
:data:`LITERATURE_V1` is the seeded default and the fallback when the table is empty; a
future fit is :func:`save_weight_set` writing a new version, never an edit to this file.
**Nothing is fitted now.** :func:`calibration_status` reports how far short we are;
docs/research/13 §B3 puts the requirement at 3,500-5,500 closed trades for a bare t=1.96
and 10,000-25,000 once deflated, and we have zero.

A note on floats
----------------
Observation values are *covariates*, never amounts. Nothing here computes a size, a price or
a PnL. Money arrives as ``Decimal`` and is converted once, at the boundary, for a threshold
comparison only; the contract's float-free rule protects money arithmetic, which this module
does not do.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jload, tx, upsert
from kaiba.core.schemas import Chain, EvidenceBasis, digest, now_ms

log = logging.getLogger(__name__)

MODEL_VERSION = "kaiba-confluence-v1"

# --------------------------------------------------------------------------------------
# published constants
#
# Every number below is quoted from a cited source. Nothing in this block is a preference.
# --------------------------------------------------------------------------------------

#: arXiv:2609.10246 (Szwajcok, Tsuchiya, Liu, Soska, Payer, Christin, *Meme Coin
#: Factories*, ACM CCS'26), all 15,245,966 pump.fun coins: originals graduate at 9.20%,
#: copycats at 0.86%. A 10.7x ratio and the largest published effect size in the review.
ORIGINAL_GRADUATION_RATE = 0.0920
COPYCAT_GRADUATION_RATE = 0.0086

#: Same paper: wash-traded coins graduate at 2.0%, non-wash-traded at 0.90%. A logistic
#: regression there puts a doubling of wash-trading transactions at ~19% higher graduation
#: odds, p = 3e-59. Positive, which is the opposite of consensus and of how our dossier
#: used to score it.
WASH_GRADUATION_RATE = 0.0200
NONWASH_GRADUATION_RATE = 0.0090
WASH_ODDS_PER_DOUBLING = 1.19

#: Same paper: the wash-trade rate is 0.41% among coins with 1-10 transactions against
#: 50.31% among coins with 10,000+. Below ten observed swaps, "no wash detected" is a
#: statement about our sample, not about the token.
WASH_MIN_SWAPS_FOR_ABSENCE = 10.0

#: arXiv:2602.14860 (Marino, Naviglio, Tarantelli, Lillo), 655,770 tokens: the number of
#: swaps needed to reach a given vSol is the dominant graduation predictor; liquidity
#: accumulated in ~10 trades dramatically outperforms the same liquidity over 1,000+; the
#: median successful launch graduates in 457 steps. pump.fun graduation is 85 SOL of real
#: reserve, so those three published figures give the whole mapping without a free
#: parameter.
GRADUATION_SOL = 85.0
MEDIAN_GRADUATE_STEPS = 457.0
FAST_GRADUATE_STEPS = 10.0
SLOW_GRADUATE_STEPS = 1000.0
VELOCITY_MID_SOL_PER_SWAP = GRADUATION_SOL / MEDIAN_GRADUATE_STEPS  # 0.18600
VELOCITY_HI_SOL_PER_SWAP = GRADUATION_SOL / FAST_GRADUATE_STEPS  # 8.5
VELOCITY_LO_SOL_PER_SWAP = GRADUATION_SOL / SLOW_GRADUATE_STEPS  # 0.085

#: MELT (arXiv:2602.13480): high-risk tokens show a 24 percentage-point higher
#: bundle-adjusted top-10 concentration *increase*, against 6pp for low-risk tokens. The
#: discriminative quantity is the adjusted-minus-raw delta, which is exactly what
#: :attr:`kaiba.intelligence.concentration.ConcentrationReport.delta` holds.
BUNDLE_DELTA_GOOD_PP = 6.0
BUNDLE_DELTA_BAD_PP = 24.0

#: arXiv:2609.10246 §4.4 as digested in docs/research/10: a per-address dev-history lookup
#: is defeated 55% of the time by cluster-fresh creator wallets, so an unclustered creator
#: reading survives with probability 0.45. This is a measured discount, not a haircut.
CREATOR_ADDRESS_LOOKUP_DEFEAT = 0.55
CREATOR_EVIDENCE_SURVIVAL = 1.0 - CREATOR_ADDRESS_LOOKUP_DEFEAT

#: Our own measurement, docs/STATE.md: 576 creators covering 47,433 lifetime coins at
#: 3.43% graduation. Used as the reference a single creator's own rate is compared against.
#: Published pump.fun base rates disagree wildly across windows (1.02% lifetime on 15.2M
#: coins, 0.63% for Sep 2025, ~5% measured here in Sep 2026), which is why the reference is
#: our own population and why it is a versioned parameter rather than a constant.
KAIBA_CREATOR_POPULATION_GRADUATION = 0.0343

#: docs/EDGE-AND-VARIABLES.md §3a: measured pump.fun graduation is about 5% in Sep 2026.
#: Only used to turn a log-odds score back into a readable probability.
BASE_GRADUATION_RATE = 0.05

#: arXiv:2602.14860: the median successful launch graduates in 4.4 minutes, so the first
#: five minutes is the window in which early-activity character is decided.
EARLY_WINDOW_S = 300.0
#: CCS'26 WT2 uses a 5-second buy/sell window. Reused here verbatim.
WASH_WINDOW_MS = 5_000


def _log_odds(p: float) -> float:
    return math.log(p / (1.0 - p))


def _separation(p_good: float, p_bad: float) -> float:
    """Full log-odds separation in nats between two published conditional rates."""
    return _log_odds(p_good) - _log_odds(p_bad)


#: 2.45790 nats.
COPYCAT_SEPARATION_NATS = _separation(ORIGINAL_GRADUATION_RATE, COPYCAT_GRADUATION_RATE)
#: 0.80967 nats.
WASH_SEPARATION_NATS = _separation(WASH_GRADUATION_RATE, NONWASH_GRADUATION_RATE)

#: Every separation we can actually compute from a published pair of conditional rates.
MEASURED_SEPARATIONS_NATS: dict[str, float] = {
    "copycat_reuse": COPYCAT_SEPARATION_NATS,
    "wash_trading": WASH_SEPARATION_NATS,
}

#: Rule U. A published direction with an unpublished magnitude gets the floor of the
#: measured set, halved into a weight like every other variable. 0.40483 nats.
MEASURED_FLOOR_NATS = min(MEASURED_SEPARATIONS_NATS.values())
RULE_U_WEIGHT_NATS = MEASURED_FLOOR_NATS / 2.0

#: The largest thing any single variable may say, used to normalise the creator log-odds.
MEASURED_CEILING_NATS = max(MEASURED_SEPARATIONS_NATS.values())

#: Full ignorance costs this much, scaled by the fraction of evidenced weight we failed to
#: establish. Bounded by the smallest measured effect so that knowing nothing is never
#: worse than one confirmed adverse measurement.
UNKNOWN_PENALTY_CAP_NATS = MEASURED_FLOOR_NATS / 2.0

#: The prior is worth exactly one expected graduation at the population rate, which fixes
#: the shrinkage strength without a free parameter.
CREATOR_PRIOR_LAUNCHES = 1.0 / KAIBA_CREATOR_POPULATION_GRADUATION

#: Thresholds the system uses today, mirrored from ``kaiba.intelligence.dyor``. None of
#: them has published support; they are here so their effect on the ranking is measurable.
#: ``tests/test_confluence.py`` asserts they still match dyor's constants.
DYOR_TOP10_PCT_WARN = 35.0
#: The dev veto: 10 -> 30 on 2026-09-22 (operator instruction superseding PLAN §5.5), then
#: 30 -> 50 on 2026-09-23 on a 7,738-token population study showing the 30-50% band reaches
#: 2x at 37.8% against a 26.2% baseline while >=50% is ordinary. See
#: ``kaiba.intelligence.dyor.DEV_PCT_BLOCK`` for the numbers.
#:
#: Mirrored so the assertion above keeps holding, and deliberately NOT used by
#: ``dev_supply_threshold``: the scorer asks where a dev holding starts COSTING, which is
#: still 10%, and repointing it at the veto would widen the ranking silently on the same
#: change that widened the veto.
DYOR_DEV_PCT_BLOCK = 50.0
#: The review line, which is what the scorer below is about.
DYOR_DEV_PCT_WARN = 10.0
DYOR_BUNDLER_PCT_WARN = 15.0
DYOR_SNIPER_PCT_WARN = 20.0
DYOR_LOW_LIQUIDITY_USD = 10_000.0


# --------------------------------------------------------------------------------------
# grades, observations, specs
# --------------------------------------------------------------------------------------


class EvidenceGrade(StrEnum):
    """Why a weight is the size it is. The tiers are never summed together blindly."""

    MEASURED = "measured"
    DERIVED = "derived"
    INVENTED = "invented"


#: Grades that may contribute to the evidenced-only score.
EVIDENCED_GRADES: frozenset[EvidenceGrade] = frozenset({EvidenceGrade.MEASURED, EvidenceGrade.DERIVED})


class Direction(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    SIGNED = "signed"
    REPORT_ONLY = "report_only"


@dataclass(frozen=True, slots=True)
class Observation:
    """One variable's reading. ``value is None`` with ``UNAVAILABLE`` means unknown.

    ``support`` is the sample size behind the reading, which is what lets "we looked and
    found no wash trading" be distinguished from "we saw three swaps". ``proxy`` marks a
    reading taken with a substitute measurement rather than the one the paper used.
    """

    value: float | None = None
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    support: float | None = None
    detail: str | None = None
    proxy: bool = False
    #: Overrides the spec's ``contribution_factor`` when the caller knows better - for
    #: instance a creator resolved through the funding graph rather than by address.
    factor: float | None = None

    @property
    def known(self) -> bool:
        return self.value is not None and self.basis is not EvidenceBasis.UNAVAILABLE

    @classmethod
    def unavailable(cls, detail: str) -> Observation:
        return cls(value=None, basis=EvidenceBasis.UNAVAILABLE, detail=detail)


@dataclass(frozen=True, slots=True)
class VariableSpec:
    """A weight and the reason it is that size. ``provenance`` is never optional."""

    name: str
    weight_nats: float
    grade: EvidenceGrade
    provenance: str
    mapping: str
    direction: Direction
    params: Mapping[str, float] = field(default_factory=dict)
    correlation_group: str | None = None
    #: Multiplies the contribution after mapping. Used for measured discounts such as the
    #: 55% defeat rate on per-address creator lookups.
    contribution_factor: float = 1.0
    factor_reason: str | None = None

    @property
    def evidenced(self) -> bool:
        return self.grade in EVIDENCED_GRADES

    @property
    def scoring(self) -> bool:
        """False for variables carried for the reader and deliberately worth nothing."""
        return self.weight_nats > 0.0


@dataclass(frozen=True, slots=True)
class WeightSet:
    """A versioned set of weights. A refit is a new version, never an edit."""

    version: str
    variables: tuple[VariableSpec, ...]
    source: str = "literature"
    created_ms: int = 0
    fitted_from_trades: int | None = None
    unknown_penalty_cap_nats: float = UNKNOWN_PENALTY_CAP_NATS
    base_graduation_rate: float = BASE_GRADUATION_RATE
    note: str | None = None

    def by_name(self) -> dict[str, VariableSpec]:
        return {spec.name: spec for spec in self.variables}

    def subset(self, *, evidenced_only: bool) -> tuple[VariableSpec, ...]:
        if not evidenced_only:
            return self.variables
        return tuple(spec for spec in self.variables if spec.evidenced)

    @property
    def evidenced_weight_nats(self) -> float:
        return sum(s.weight_nats for s in self.variables if s.evidenced and s.scoring)

    @property
    def invented_weight_nats(self) -> float:
        return sum(s.weight_nats for s in self.variables if not s.evidenced and s.scoring)


# --------------------------------------------------------------------------------------
# mappings: value -> signed score in [-1, +1]
#
# Kept in a registry so a weight row can name one. A mapping returns None when the reading
# exists but cannot support a score, which is treated exactly like a missing reading.
# --------------------------------------------------------------------------------------


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _map_signed(obs: Observation, params: Mapping[str, float]) -> float | None:
    """The reading is already a signed score; pass it through."""
    return _clamp(float(obs.value))  # type: ignore[arg-type]


def _map_share_inverse(obs: Observation, params: Mapping[str, float]) -> float | None:
    """A share on [0, 1] where more is worse. Linear, no cutoff, midpoint is its own 0.5."""
    minimum = params.get("min_support", 0.0)
    if minimum and (obs.support is None or obs.support < minimum):
        return None
    return _clamp(1.0 - 2.0 * float(obs.value))  # type: ignore[arg-type]


def _map_log_band(obs: Observation, params: Mapping[str, float]) -> float | None:
    """Log-scale band with published anchors at ``lo``, ``mid`` and ``hi``."""
    value = float(obs.value)  # type: ignore[arg-type]
    if value <= 0.0:
        return -1.0
    mid, hi, lo = params["mid"], params["hi"], params["lo"]
    delta = math.log10(value / mid)
    if delta >= 0.0:
        return _clamp(delta / math.log10(hi / mid))
    return _clamp(delta / abs(math.log10(lo / mid)))


def _map_pp_band(obs: Observation, params: Mapping[str, float]) -> float | None:
    """Percentage-point band: ``good_pp`` maps to +1, ``bad_pp`` to -1, linear between."""
    good, bad = params["good_pp"], params["bad_pp"]
    if bad == good:
        return 0.0
    return _clamp(1.0 - 2.0 * (float(obs.value) - good) / (bad - good))  # type: ignore[arg-type]


def _map_threshold_above_bad(obs: Observation, params: Mapping[str, float]) -> float | None:
    """A cutoff. Only ever used by INVENTED variables; the research supports none of these."""
    return -1.0 if float(obs.value) > params["limit"] else 1.0  # type: ignore[arg-type]


def _map_threshold_below_bad(obs: Observation, params: Mapping[str, float]) -> float | None:
    """A floor. Also invented."""
    return -1.0 if float(obs.value) < params["limit"] else 1.0  # type: ignore[arg-type]


def _map_wash(obs: Observation, params: Mapping[str, float]) -> float | None:
    """Wash transaction count to a signed score, using the published doubling coefficient.

    Absence only scores negative once we have seen enough swaps for absence to mean
    anything; below that the honest answer is that we do not know.
    """
    count = float(obs.value)  # type: ignore[arg-type]
    if count <= 0.0:
        support = obs.support
        if support is None or support < params["min_support"]:
            return None
        return -1.0
    per_doubling = math.log(params["odds_per_doubling"])
    return _clamp(per_doubling * math.log2(1.0 + count) / params["half_separation"], 0.0, 1.0)


def _map_creator_log_odds(obs: Observation, params: Mapping[str, float]) -> float | None:
    """A creator's own graduation rate against the population, shrunk by sample size."""
    support = obs.support
    if support is None or support <= 0.0:
        return None
    rate = float(obs.value)  # type: ignore[arg-type]
    population = params["pop_rate"]
    prior = params["prior_launches"]
    shrunk = (rate * support + population * prior) / (support + prior)
    shrunk = min(max(shrunk, 1e-6), 1.0 - 1e-6)
    return _clamp((_log_odds(shrunk) - _log_odds(population)) / params["normaliser"])


def _map_report_only(obs: Observation, params: Mapping[str, float]) -> float | None:
    """Carried so a caller can read it. Never contributes, by design."""
    return None


MappingFn = Callable[[Observation, Mapping[str, float]], float | None]

MAPPINGS: dict[str, MappingFn] = {
    "signed": _map_signed,
    "share_inverse": _map_share_inverse,
    "log_band": _map_log_band,
    "pp_band": _map_pp_band,
    "threshold_above_bad": _map_threshold_above_bad,
    "threshold_below_bad": _map_threshold_below_bad,
    "wash": _map_wash,
    "creator_log_odds": _map_creator_log_odds,
    "report_only": _map_report_only,
}


# --------------------------------------------------------------------------------------
# correlation groups
# --------------------------------------------------------------------------------------

GROUP_CREATOR_IDENTITY = "creator_identity"
GROUP_EARLY_FLOW = "early_flow"
GROUP_HOLDER_STRUCTURE = "holder_structure"

#: Why each group exists. Surfaced with the score so the discount is never silent.
GROUP_RATIONALE: dict[str, str] = {
    GROUP_CREATOR_IDENTITY: (
        "A creator who mass-produces copies emits both the copycat signal and the creator-history "
        "signal from one underlying fact. CCS'26 puts the top 1% of creator clusters at 53-59% of "
        "all coins, so the overlap is the common case, not the edge case. Correlation unmeasured: "
        "we hold zero closed trades, so the group is clipped to its loudest member."
    ),
    GROUP_EARLY_FLOW: (
        "SOL-per-swap, wash transactions and bot turnover are three reads of the same swap stream "
        "over the same window for the same token, and wash transactions mechanically inflate the "
        "swap count that is velocity's denominator. Correlation unmeasured; clipped."
    ),
    GROUP_HOLDER_STRUCTURE: (
        "Bundle-adjusted concentration, raw top-10, dev supply, bundler and sniper share are all "
        "reads of one holder table. MELT's ablation shows concentration adds -0.0036 AUPRC once "
        "bundle statistics are present, i.e. they are near-redundant. Clipped."
    ),
}


# --------------------------------------------------------------------------------------
# the literature-derived weight set
# --------------------------------------------------------------------------------------


def _literature_v1() -> WeightSet:
    return WeightSet(
        version="literature-v1",
        source="literature",
        created_ms=0,
        fitted_from_trades=None,
        note=(
            "Derived entirely from published effect sizes and from kaiba's own creator measurement. "
            "Nothing here is fitted; there are zero closed trades and a weight fitted on nothing is "
            "worse than a weight chosen from a paper."
        ),
        variables=(
            VariableSpec(
                name="copycat_reuse",
                weight_nats=COPYCAT_SEPARATION_NATS / 2.0,
                grade=EvidenceGrade.MEASURED,
                mapping="signed",
                direction=Direction.SIGNED,
                correlation_group=GROUP_CREATOR_IDENTITY,
                provenance=(
                    "MEASURED - Szwajcok, Tsuchiya, Liu, Soska, Payer, Christin, 'Meme Coin Factories', "
                    "ACM CCS'26, arXiv:2609.10246, all 15,245,966 pump.fun coins: originals graduate at "
                    "9.20%, copycats at 0.86% (10.7x). Weight = half the log-odds separation "
                    "ln(.092/.908) - ln(.0086/.9914) = 2.45790 nats. 17.7% of graduates are themselves "
                    "copycats, so this is a strong prior and not a veto, which is exactly what a bounded "
                    "log-odds contribution expresses."
                ),
            ),
            VariableSpec(
                name="creator_graduation_history",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.DERIVED,
                mapping="creator_log_odds",
                direction=Direction.HIGHER_IS_BETTER,
                correlation_group=GROUP_CREATOR_IDENTITY,
                params={
                    "pop_rate": KAIBA_CREATOR_POPULATION_GRADUATION,
                    "prior_launches": CREATOR_PRIOR_LAUNCHES,
                    "normaliser": MEASURED_CEILING_NATS,
                },
                contribution_factor=CREATOR_EVIDENCE_SURVIVAL,
                factor_reason=(
                    "A per-address dev-history lookup is defeated 55% of the time by cluster-fresh "
                    "creator wallets (arXiv:2609.10246 via docs/research/10 §4.4), so an unclustered "
                    "reading survives with probability 0.45. Set Observation.factor=1.0 once the "
                    "3-hop funding graph resolves the creator."
                ),
                provenance=(
                    "DERIVED - direction and concentration are measured (CCS'26: top 1% of creator "
                    "clusters produce 53-59% of all coins; kaiba's own backfill, docs/STATE.md: 576 "
                    "creators / 47,433 coins / 3.43% graduation), but arXiv:2602.14860 tested whether "
                    "prolific or historically successful creators predict graduation and found 'limited "
                    "predictive power, with observed patterns consistent with value extraction rather "
                    "than project support'. With a direct null against the naive form, Rule U applies: "
                    "weight = the smallest measured separation in this set / 2 = 0.40483 nats. The "
                    "mapping is a shrunk empirical log-odds against kaiba's own population rate, "
                    "normalised by the largest measured separation so no single variable can say more "
                    "than the strongest measured one. Shrinkage prior = 1/pop_rate launches, i.e. the "
                    "prior is worth exactly one expected graduation."
                ),
            ),
            VariableSpec(
                name="curve_velocity_sol_per_swap",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.DERIVED,
                mapping="log_band",
                direction=Direction.HIGHER_IS_BETTER,
                correlation_group=GROUP_EARLY_FLOW,
                params={
                    "mid": VELOCITY_MID_SOL_PER_SWAP,
                    "hi": VELOCITY_HI_SOL_PER_SWAP,
                    "lo": VELOCITY_LO_SOL_PER_SWAP,
                },
                provenance=(
                    "DERIVED - Marino, Naviglio, Tarantelli, Lillo, arXiv:2602.14860, 655,770 tokens: "
                    "'the number of swaps required to reach a given vSol level is the dominant predictor "
                    "of graduation'; liquidity accumulated in ~10 trades dramatically outperforms the "
                    "same liquidity over 1,000+; the median successful launch graduates in 457 steps. "
                    "The paper states rank, not effect size, so Rule U fixes the magnitude at the "
                    "measured floor / 2 = 0.40483 nats - deliberately under-weighting the highest-ranked "
                    "variable rather than borrowing another paper's magnitude for it. Every mapping "
                    "anchor is published and none is invented: neutral at 85 SOL / 457 steps = 0.18600 "
                    "SOL per swap, +1 at 85/10 = 8.5, -1 at 85/1000 = 0.085. Caveat from docs/research/13 "
                    "§B5.3: this predictor is coincident, not leading - it is observable only after the "
                    "liquidity has arrived."
                ),
            ),
            VariableSpec(
                name="wash_trading",
                weight_nats=WASH_SEPARATION_NATS / 2.0,
                grade=EvidenceGrade.MEASURED,
                mapping="wash",
                direction=Direction.HIGHER_IS_BETTER,
                correlation_group=GROUP_EARLY_FLOW,
                params={
                    "min_support": WASH_MIN_SWAPS_FOR_ABSENCE,
                    "odds_per_doubling": WASH_ODDS_PER_DOUBLING,
                    "half_separation": WASH_SEPARATION_NATS / 2.0,
                },
                provenance=(
                    "MEASURED - arXiv:2609.10246, all 15.2M pump.fun coins: wash-traded coins graduate at "
                    "2.0%, non-wash-traded at 0.90%, and doubling wash-trading transactions raises "
                    "graduation odds by ~19% (p = 3e-59). The sign is POSITIVE, which is the opposite of "
                    "consensus and of how kaiba's dossier used to score it. Weight = half the log-odds "
                    "separation ln(.02/.98) - ln(.009/.991) = 0.80967 nats. The intermediate mapping uses "
                    "the published per-doubling coefficient ln(1.19). Absence scores -1 only above 10 "
                    "observed swaps, because the same paper measures a 0.41% wash rate among coins with "
                    "1-10 transactions against 50.31% among coins with 10,000+."
                ),
            ),
            VariableSpec(
                name="bot_dominated_early_activity",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.DERIVED,
                mapping="share_inverse",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_EARLY_FLOW,
                params={"min_support": WASH_MIN_SWAPS_FOR_ABSENCE},
                provenance=(
                    "DERIVED - arXiv:2602.14860: 'markets dominated by bot-like activity exhibit "
                    "systematically lower graduation probabilities... high turnover and algorithmic "
                    "trading do not translate into sustained capital commitment'. Direction published, "
                    "magnitude not, so Rule U: measured floor / 2 = 0.40483 nats. The paper's own bot "
                    "flag is a coarse proxy (frontend routing vs direct contract calls) and ours is a "
                    "different coarse proxy - early-window turnover concentration, 1 - distinct wallets "
                    "/ swaps - so the reading is ESTIMATED and flagged proxy=True. The mapping is a "
                    "linear share with its own midpoint as neutral; it has no cutoff and no free "
                    "parameter, which is the only honest shape when no threshold is published."
                ),
            ),
            VariableSpec(
                name="bundle_adjusted_concentration",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.DERIVED,
                mapping="pp_band",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                params={"good_pp": BUNDLE_DELTA_GOOD_PP, "bad_pp": BUNDLE_DELTA_BAD_PP},
                provenance=(
                    "DERIVED - MELT, arXiv:2602.13480: high-risk tokens show a 24 percentage-point higher "
                    "bundle-adjusted top-10 concentration increase against 6pp for low-risk tokens; "
                    "bundle statistics are 35 of 122 features and rank #2 in importance. That is a "
                    "discriminative gap, not a pair of conditional rates, and converting it to log-odds "
                    "needs a within-class variance the paper does not publish, so the magnitude cannot be "
                    "computed. The same paper's ablation - AUPRC 0.5729 -> 0.5451 removing bundle stats, "
                    "on an 84%-positive class - argues for the floor rather than a large magnitude, so "
                    "Rule U: 0.40483 nats. Mapping anchors are the published 6pp / 24pp pair applied to "
                    "the adjusted-minus-raw delta, which is the quantity the paper actually measured and "
                    "is exactly ConcentrationReport.delta."
                ),
            ),
            VariableSpec(
                name="raw_top10_concentration",
                weight_nats=0.0,
                grade=EvidenceGrade.MEASURED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                provenance=(
                    "MEASURED NULL - weight is deliberately zero. MELT (arXiv:2602.13480) ablation: "
                    "removing concentration features alone moves AUPRC 0.5729 -> 0.5693, i.e. -0.0036, "
                    "'essentially nothing on its own'. docs/research/10 §4.7 is blunt that no published "
                    "study establishes any specific holder-concentration threshold on Solana memecoins "
                    "with a measured hit rate; those thresholds appear only in vendor blogs. The evidenced "
                    "version of this measurement is bundle_adjusted_concentration, which is a different "
                    "quantity. Reported so a caller can see the number without it moving the score."
                ),
            ),
            VariableSpec(
                name="dev_bought_own_bundle",
                weight_nats=0.0,
                grade=EvidenceGrade.MEASURED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                provenance=(
                    "MEASURED NULL - weight is deliberately zero. MELT (arXiv:2602.13480): 98.7% of "
                    "creation events co-occur with developer purchases at the lowest pricing tier. A "
                    "signal that fires on essentially every launch carries no information, and as a "
                    "binary filter it would reject ~99% of the universe. What carries signal is bundle "
                    "magnitude, which is bundle_adjusted_concentration."
                ),
            ),
            VariableSpec(
                name="freeze_authority_live",
                weight_nats=0.0,
                grade=EvidenceGrade.MEASURED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                provenance=(
                    "MEASURED NULL - weight is deliberately zero. 'From Hype to Collapse', "
                    "arXiv:2603.24625, 76,469 Solana rug candidates: Pump-and-Dump 78.9%, Liquidity "
                    "Withdrawal 20.4%, Freeze Authority Abuse 0.6% (461 cases). Keep the check - it is "
                    "cheap and it is a real blocker when it fires - but it must not be counted as "
                    "coverage, which is what a nonzero weight here would do."
                ),
            ),
            VariableSpec(
                name="sniper_exposure",
                weight_nats=0.0,
                grade=EvidenceGrade.MEASURED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                provenance=(
                    "MEASURED NULL - weight is deliberately zero. Sniper presence lifts buyer count by "
                    "+16.1% [13.0, 19.4] but SOL inflow by +6.3% [-0.5, +15.1], which contains zero "
                    "(docs/research/10 §2.4b). That is the appearance of demand without the capital, "
                    "which is what a trap looks like rather than what an edge looks like. Webacy's "
                    "Critical/High/Medium tiers have no published validation against outcomes."
                ),
            ),
            VariableSpec(
                name="wallet_pnl_grade",
                weight_nats=0.0,
                grade=EvidenceGrade.MEASURED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                provenance=(
                    "MEASURED NULL - weight is deliberately zero. arXiv:2602.14860 conditioned graduation "
                    "probability on whether ex-ante identified historically profitable wallets traded a "
                    "token, across 655,770 tokens, and found 'at most a modest and non-monotonic effect'; "
                    "their explanation is that a skilled wallet in the book is also a competent seller. "
                    "docs/research/13 Part A: no cohort-persistence study exists anywhere, and our 6,236 "
                    "imported wallets came from vendor leaderboards that publish no method. Input only, "
                    "never a trigger."
                ),
            ),
            VariableSpec(
                name="independent_entity_count",
                weight_nats=0.0,
                grade=EvidenceGrade.DERIVED,
                mapping="report_only",
                direction=Direction.REPORT_ONLY,
                provenance=(
                    "DERIVED, report only - weight is deliberately zero. docs/CONTRACT.md and "
                    "kaiba.intelligence.entity.independent_entity_count: confluence counts entities, not "
                    "addresses, because five addresses funded by one wallet are one opinion. The collapse "
                    "is applied when this number is computed. It carries no weight because the underlying "
                    "evidence for wallet quality is a published null (see wallet_pnl_grade); promoting it "
                    "to a weighted variable would re-import the wallet-PnL assumption through the side "
                    "door. Use it for sizing, per docs/EDGE-AND-VARIABLES.md §4 item 10."
                ),
            ),
            # ---------------------------------------------------------------- invented tier
            VariableSpec(
                name="raw_top10_threshold",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.INVENTED,
                mapping="threshold_above_bad",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                params={"limit": DYOR_TOP10_PCT_WARN},
                provenance=(
                    "INVENTED - mirrors kaiba.intelligence.dyor.TOP10_PCT_WARN = 35%. No published study "
                    "establishes any holder-concentration threshold on Solana memecoins "
                    "(docs/research/10 §4.7, docs/EDGE-AND-VARIABLES.md §1 #8). The weight is set to the "
                    "measured floor so that its effect on the ranking can be measured against the "
                    "evidenced score, not because 35% is justified. It is not."
                ),
            ),
            VariableSpec(
                name="dev_supply_threshold",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.INVENTED,
                mapping="threshold_above_bad",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                params={"limit": DYOR_DEV_PCT_WARN},
                provenance=(
                    "INVENTED - mirrors kaiba.intelligence.dyor.DEV_PCT_WARN = 10%, which was the veto "
                    "until 2026-09-22 and is now the review line; the veto moved to 30% and this "
                    "variable deliberately did not follow it. No published source "
                    "sets a dev-supply threshold; the one adjacent published number is that 98.7% of "
                    "launches involve a developer purchase at all (MELT, arXiv:2602.13480), which says "
                    "nothing about magnitude. Weight at the measured floor so the comparison is visible."
                ),
            ),
            VariableSpec(
                name="bundler_exposure_threshold",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.INVENTED,
                mapping="threshold_above_bad",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                params={"limit": DYOR_BUNDLER_PCT_WARN},
                provenance=(
                    "INVENTED - mirrors kaiba.intelligence.dyor.BUNDLER_PCT_WARN = 15%. MELT finds "
                    "coordinated accounts hold 36.5% of supply on average, so 15% is below the population "
                    "mean and would fire on most launches; no study sets a cutoff. Weight at the measured "
                    "floor so the comparison is visible."
                ),
            ),
            VariableSpec(
                name="sniper_exposure_threshold",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.INVENTED,
                mapping="threshold_above_bad",
                direction=Direction.LOWER_IS_BETTER,
                correlation_group=GROUP_HOLDER_STRUCTURE,
                params={"limit": DYOR_SNIPER_PCT_WARN},
                provenance=(
                    "INVENTED - mirrors kaiba.intelligence.dyor.SNIPER_PCT_WARN = 20%. The published "
                    "sniper result is a null on SOL inflow (docs/research/10 §2.4b) and the vendor tiers "
                    "that name 20% publish no validation. Weight at the measured floor so the comparison "
                    "is visible."
                ),
            ),
            VariableSpec(
                name="liquidity_floor",
                weight_nats=RULE_U_WEIGHT_NATS,
                grade=EvidenceGrade.INVENTED,
                mapping="threshold_below_bad",
                direction=Direction.HIGHER_IS_BETTER,
                correlation_group=None,
                params={"limit": DYOR_LOW_LIQUIDITY_USD},
                provenance=(
                    "INVENTED - mirrors kaiba.intelligence.dyor.LOW_LIQUIDITY_USD = $10,000. A depth "
                    "floor is a sizing constraint with a real rationale (our own exit is the adverse "
                    "move), but $10,000 is not a measured discriminator of anything and no study "
                    "establishes one. Ungrouped, because depth is not a read of the holder table. Weight "
                    "at the measured floor so the comparison is visible."
                ),
            ),
        ),
    )


LITERATURE_V1: WeightSet = _literature_v1()
DEFAULT_WEIGHTS_VERSION = LITERATURE_V1.version

#: A provenance string on an evidenced weight has to point at something. One of these must
#: appear in it, so "derived from experience" cannot pass for a citation.
CITATION_MARKERS: tuple[str, ...] = ("arXiv:", "docs/research/", "docs/STATE.md", "docs/EDGE-")


def validate_weight_set(weights: WeightSet) -> list[str]:
    """Every problem with a weight set, as a list of sentences. Empty means it is usable.

    This is the control behind "every weight is annotated with its source". It runs on any
    set loaded from the database as well as on the built-in one, because a fitted set
    written by a future job is exactly where an unannotated weight would appear.
    """
    problems: list[str] = []
    seen: set[str] = set()
    for spec in weights.variables:
        where = f"{weights.version}.{spec.name}"
        if spec.name in seen:
            problems.append(f"{where}: duplicate variable")
        seen.add(spec.name)
        text = (spec.provenance or "").strip()
        if not text:
            problems.append(f"{where}: weight has no provenance")
            continue
        if spec.grade is EvidenceGrade.INVENTED:
            if not text.startswith("INVENTED"):
                problems.append(f"{where}: invented weight does not say INVENTED in its provenance")
        else:
            if "INVENTED" in text:
                problems.append(f"{where}: graded {spec.grade.value} but the provenance says INVENTED")
            if not any(marker in text for marker in CITATION_MARKERS):
                problems.append(f"{where}: evidenced weight cites no source")
        if spec.weight_nats < 0.0:
            problems.append(f"{where}: negative weight; direction belongs in the mapping")
        if spec.mapping not in MAPPINGS:
            problems.append(f"{where}: unknown mapping {spec.mapping!r}")
        if spec.scoring and spec.mapping == "report_only":
            problems.append(f"{where}: report-only mapping carries a nonzero weight")
        if not 0.0 <= spec.contribution_factor <= 1.0:
            problems.append(f"{where}: contribution_factor {spec.contribution_factor} outside [0, 1]")
        if spec.contribution_factor != 1.0 and not spec.factor_reason:
            problems.append(f"{where}: discounted contribution with no stated reason")
    if weights.source == "fitted" and not weights.fitted_from_trades:
        problems.append(f"{weights.version}: marked fitted but names no sample it was fitted on")
    return problems


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


#: The rule, quoted wherever the guard fires so a caller never has to come and read this.
GROUNDLESS_GUARD = (
    "groundless: no evidenced variable was measured, so invented thresholds may subtract "
    "but may not add. Clearing a threshold nobody validated is not evidence of anything."
)


class Standing(StrEnum):
    """What kind of answer this is, which is not the same question as how big it is.

    A caller deciding whether to act needs to tell "positive, and grounded in something
    somebody measured" from "positive because five invented thresholds happened to clear".
    :attr:`INVENTED_POSITIVE` is the second of those and is never a reason to size up.
    """

    #: Nothing evidenced was established. The score cannot be positive; see the guard.
    GROUNDLESS = "groundless"
    #: The evidenced variables alone put this above the base rate.
    EVIDENCED_POSITIVE = "evidenced_positive"
    #: Positive only once the invented thresholds are added. Treat as unproven.
    INVENTED_POSITIVE = "invented_positive"
    #: Grounded, and at or below the base rate.
    NEGATIVE = "negative"


@dataclass(frozen=True, slots=True)
class Contribution:
    """One variable's line in the breakdown, including why its weight is what it is."""

    variable: str
    value: float | None
    support: float | None
    basis: EvidenceBasis
    signed_score: float | None
    weight_nats: float
    raw_contribution_nats: float
    contribution_nats: float
    grade: EvidenceGrade
    provenance: str
    correlation_group: str | None
    contribution_factor: float
    factor_reason: str | None
    direction: Direction
    proxy: bool
    detail: str | None
    known: bool
    #: True when the groundless guard withheld this variable's positive contribution.
    #: ``raw_contribution_nats`` still shows what it would have added.
    suppressed: bool = False
    suppression_reason: str | None = None

    @property
    def scoring(self) -> bool:
        return self.weight_nats > 0.0

    def line(self) -> str:
        value = "n/a" if self.value is None else f"{self.value:.4g}"
        score = "n/a" if self.signed_score is None else f"{self.signed_score:+.3f}"
        flags = f"{self.grade.value}{'/proxy' if self.proxy else ''}"
        held = (
            f"  WITHHELD {self.raw_contribution_nats:+.4f} (groundless)" if self.suppressed else ""
        )
        return (
            f"{self.variable:<30} value={value:<10} score={score:<8} "
            f"w={self.weight_nats:.4f} -> {self.contribution_nats:+.4f} nats [{flags}]{held}"
        )


@dataclass(frozen=True, slots=True)
class GroupAdjustment:
    """What a correlation group cost. ``discount_nats`` is never silently zero-filled."""

    group: str
    members: tuple[str, ...]
    raw_sum_nats: float
    cap_nats: float
    applied_nats: float
    discount_nats: float
    rationale: str


@dataclass(frozen=True, slots=True)
class ConfluenceScore:
    """The score, both versions of it, and everything needed to argue with either."""

    chain: Chain
    token: str
    as_of_ms: int
    weights_version: str
    model_version: str
    evidence_sum_nats: float
    invented_sum_nats: float
    unknown_penalty_nats: float
    score_evidenced_nats: float
    score_all_nats: float
    delta_invented_nats: float
    coverage: float
    missing_weight_nats: float
    total_evidenced_weight_nats: float
    base_graduation_rate: float
    contributions: tuple[Contribution, ...] = ()
    groups: tuple[GroupAdjustment, ...] = ()
    unknowns: tuple[str, ...] = ()
    proxies: tuple[str, ...] = ()
    invented_used: tuple[str, ...] = ()
    #: At least one evidenced, weight-bearing variable produced a score. When this is
    #: False the invented thresholds were not allowed to add anything.
    grounded: bool = False
    measured_evidenced: tuple[str, ...] = ()
    suppressed_invented: tuple[str, ...] = ()
    point_in_time: bool = True
    notes: tuple[str, ...] = ()

    @property
    def standing(self) -> Standing:
        """The distinction a caller acts on. See :class:`Standing`."""
        if not self.grounded:
            return Standing.GROUNDLESS
        if self.score_evidenced_nats > 0.0:
            return Standing.EVIDENCED_POSITIVE
        if self.score_all_nats > 0.0:
            return Standing.INVENTED_POSITIVE
        return Standing.NEGATIVE

    @property
    def divergent(self) -> bool:
        """True when the invented variables moved the score by more than the measured floor.

        The comparison point is the smallest effect anyone has actually measured in this
        set. If ungrounded thresholds can shift the answer by more than that, the ranking
        a caller sees is not the ranking the evidence supports, and that is worth saying
        every single time.
        """
        return abs(self.delta_invented_nats) >= MEASURED_FLOOR_NATS

    @property
    def sign_flipped(self) -> bool:
        """The invented variables changed whether the answer is positive or negative."""
        a, b = self.score_evidenced_nats, self.score_all_nats
        return (a > 0.0 > b) or (a < 0.0 < b)

    def _implied(self, score_nats: float) -> float:
        base = _log_odds(self.base_graduation_rate)
        return 1.0 / (1.0 + math.exp(-(base + score_nats)))

    @property
    def implied_graduation_evidenced(self) -> float:
        """ESTIMATED. Assumes conditional independence inside groups, which is unverified."""
        return self._implied(self.score_evidenced_nats)

    @property
    def implied_graduation_all(self) -> float:
        return self._implied(self.score_all_nats)

    def by_name(self) -> dict[str, Contribution]:
        return {c.variable: c for c in self.contributions}

    def top_drivers(self, limit: int = 3) -> tuple[Contribution, ...]:
        ranked = sorted(
            (c for c in self.contributions if c.contribution_nats),
            key=lambda c: -abs(c.contribution_nats),
        )
        return tuple(ranked[:limit])

    def summary(self) -> str:
        head = (
            f"{self.token[:12]} [{self.standing.value}] "
            f"evidenced {self.score_evidenced_nats:+.3f} nats "
            f"(p~{self.implied_graduation_evidenced * 100:.2f}%), all {self.score_all_nats:+.3f}, "
            f"delta {self.delta_invented_nats:+.3f}, coverage {self.coverage * 100:.0f}%"
        )
        if self.suppressed_invented:
            head += (
                f" [GROUNDLESS GUARD withheld {len(self.suppressed_invented)}: "
                f"{', '.join(self.suppressed_invented)}]"
            )
        if self.divergent:
            head += " [DIVERGENT: invented variables moved the score by more than the measured floor]"
        if self.sign_flipped:
            head += " [SIGN FLIPPED by invented variables]"
        return head

    def to_row(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "token": self.token,
            "as_of_ms": int(self.as_of_ms),
            "weights_version": self.weights_version,
            "model_version": self.model_version,
            "score_evidenced_nats": float(self.score_evidenced_nats),
            "score_all_nats": float(self.score_all_nats),
            "delta_invented_nats": float(self.delta_invented_nats),
            "unknown_penalty_nats": float(self.unknown_penalty_nats),
            "coverage": float(self.coverage),
            "divergent": 1 if self.divergent else 0,
            "point_in_time": 1 if self.point_in_time else 0,
            # ``grounded``, ``standing`` and ``suppressed_invented`` live here rather than
            # in their own columns because migration 020 is already applied on the live
            # database and docs/CONTRACT.md forbids editing an applied migration. They are
            # still queryable: json_extract(breakdown_json, '$.standing').
            "breakdown_json": json.dumps(
                {
                    "grounded": self.grounded,
                    "standing": self.standing.value,
                    "measured_evidenced": list(self.measured_evidenced),
                    "suppressed_invented": list(self.suppressed_invented),
                    "groundless_guard": GROUNDLESS_GUARD if self.suppressed_invented else None,
                    "contributions": [
                        {
                            "variable": c.variable,
                            "value": c.value,
                            "support": c.support,
                            "basis": c.basis.value,
                            "signed_score": c.signed_score,
                            "weight_nats": c.weight_nats,
                            "raw_contribution_nats": c.raw_contribution_nats,
                            "contribution_nats": c.contribution_nats,
                            "grade": c.grade.value,
                            "group": c.correlation_group,
                            "factor": c.contribution_factor,
                            "proxy": c.proxy,
                            "known": c.known,
                            "suppressed": c.suppressed,
                            "detail": c.detail,
                        }
                        for c in self.contributions
                    ],
                    "groups": [
                        {
                            "group": g.group,
                            "members": list(g.members),
                            "raw_sum_nats": g.raw_sum_nats,
                            "cap_nats": g.cap_nats,
                            "applied_nats": g.applied_nats,
                            "discount_nats": g.discount_nats,
                        }
                        for g in self.groups
                    ],
                    "unknowns": list(self.unknowns),
                    "proxies": list(self.proxies),
                    "invented_used": list(self.invented_used),
                    "notes": list(self.notes),
                },
                separators=(",", ":"),
                default=str,
            ),
        }


@dataclass(frozen=True, slots=True)
class Observations:
    """Everything known about one token at one instant. The scorer's only input."""

    chain: Chain
    token: str
    as_of_ms: int
    values: Mapping[str, Observation] = field(default_factory=dict)
    #: False when at least one reading came from a source that cannot be replayed exactly
    #: as of ``as_of_ms``. The promotion gates require point-in-time replay, so a caller
    #: running a backtest must check this rather than assume it.
    point_in_time: bool = True
    notes: tuple[str, ...] = ()

    def get(self, name: str) -> Observation:
        return self.values.get(name, Observation.unavailable("not supplied"))

    def with_value(self, name: str, obs: Observation) -> Observations:
        merged = dict(self.values)
        merged[name] = obs
        return replace(self, values=merged)


# --------------------------------------------------------------------------------------
# the scorer
# --------------------------------------------------------------------------------------


def _evaluate(spec: VariableSpec, obs: Observation) -> tuple[float | None, float]:
    """Map one reading to a signed score and a raw contribution in nats."""
    if not obs.known:
        return None, 0.0
    fn = MAPPINGS.get(spec.mapping)
    if fn is None:
        log.warning("confluence: unknown mapping %r for %s", spec.mapping, spec.name)
        return None, 0.0
    try:
        signed = fn(obs, spec.params)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        log.warning("confluence: mapping %s failed for %s: %s", spec.mapping, spec.name, exc)
        return None, 0.0
    if signed is None:
        return None, 0.0
    factor = spec.contribution_factor if obs.factor is None else float(obs.factor)
    return signed, spec.weight_nats * signed * factor


def _apply_groups(
    specs: Sequence[VariableSpec], raw: Mapping[str, float]
) -> tuple[dict[str, float], list[GroupAdjustment]]:
    """Clip each correlation group to the magnitude of its loudest member.

    One rule, applied uniformly: a group can never say more than any single member of it
    says on its own. When members agree it collapses three votes into one; when they
    disagree the algebraic sum is already inside the cap and nothing happens, and the
    members keep their own contributions so the breakdown still shows who said what. Both
    cases are strictly more conservative than a naive sum, which is the right side to be on
    while the correlations are unmeasured.
    """
    grouped: dict[str, list[str]] = {}
    for spec in specs:
        if spec.correlation_group:
            grouped.setdefault(spec.correlation_group, []).append(spec.name)

    applied = dict(raw)
    adjustments: list[GroupAdjustment] = []
    for group, members in sorted(grouped.items()):
        values = [raw.get(name, 0.0) for name in members]
        total = math.fsum(values)
        cap = max((abs(v) for v in values), default=0.0)
        clipped = _clamp(total, -cap, cap)
        if clipped != total:
            scale = 0.0 if total == 0.0 else clipped / total
            for name in members:
                applied[name] = raw.get(name, 0.0) * scale
        adjustments.append(
            GroupAdjustment(
                group=group,
                members=tuple(members),
                raw_sum_nats=total,
                cap_nats=cap,
                applied_nats=clipped,
                discount_nats=clipped - total,
                rationale=GROUP_RATIONALE.get(group, "correlated inputs; clipped to the loudest member"),
            )
        )
    return applied, adjustments


@dataclass(frozen=True, slots=True)
class _Pass:
    """The result of one scoring pass over a subset of the weight set."""

    signed: dict[str, float | None]
    applied: dict[str, float]
    groups: list[GroupAdjustment]
    total: float
    suppressed: tuple[str, ...] = ()


def _pass(
    specs: Sequence[VariableSpec], obs: Observations, *, withhold_invented_credit: bool = False
) -> _Pass:
    """One scoring pass. ``withhold_invented_credit`` implements the groundless guard.

    Suppression happens *before* the correlation groups are applied, so a withheld
    variable does not raise its group's cap either. A variable that may not speak also
    may not decide how loudly its group is allowed to speak.
    """
    signed: dict[str, float | None] = {}
    raw: dict[str, float] = {}
    suppressed: list[str] = []
    for spec in specs:
        score, contribution = _evaluate(spec, obs.get(spec.name))
        if (
            withhold_invented_credit
            and spec.grade is EvidenceGrade.INVENTED
            and spec.scoring
            and contribution > 0.0
        ):
            suppressed.append(spec.name)
            contribution = 0.0
        signed[spec.name] = score
        raw[spec.name] = contribution
    applied, adjustments = _apply_groups(specs, raw)
    total = math.fsum(applied.get(spec.name, 0.0) for spec in specs)
    return _Pass(signed, applied, adjustments, total, tuple(suppressed))


def score(obs: Observations, weights: WeightSet | None = None) -> ConfluenceScore:
    """Score one token. Pure: same observations and same weights give the same answer.

    Two passes. The first uses only MEASURED and DERIVED variables; the second adds the
    INVENTED ones. Both carry the identical unknown penalty, so the difference between them
    is exactly the invented variables' contribution and nothing else.

    The groundless guard runs between them: see :data:`GROUNDLESS_GUARD`.
    """
    ws = weights or LITERATURE_V1
    evidenced_specs = ws.subset(evidenced_only=True)
    all_specs = ws.subset(evidenced_only=False)

    evidenced = _pass(evidenced_specs, obs)

    total_weight = 0.0
    missing_weight = 0.0
    unknowns: list[str] = []
    measured: list[str] = []
    for spec in evidenced_specs:
        if not spec.scoring:
            continue
        total_weight += spec.weight_nats
        if evidenced.signed.get(spec.name) is None:
            missing_weight += spec.weight_nats
            unknowns.append(spec.name)
        else:
            measured.append(spec.name)

    # The guard. Grounding is "at least one evidenced, weight-bearing variable actually
    # produced a reading we could score" - not "we read something", and not "a threshold
    # cleared". Everything downstream keys off this one boolean.
    grounded = bool(measured)
    everything = _pass(all_specs, obs, withhold_invented_credit=not grounded)

    fraction_missing = (missing_weight / total_weight) if total_weight else 1.0
    penalty = ws.unknown_penalty_cap_nats * fraction_missing
    coverage = 1.0 - fraction_missing

    withheld = set(everything.suppressed)
    contributions: list[Contribution] = []
    proxies: list[str] = []
    invented_used: list[str] = []
    for spec in all_specs:
        observation = obs.get(spec.name)
        signed_value, raw_contribution = _evaluate(spec, observation)
        applied_value = everything.applied.get(spec.name, 0.0)
        factor = spec.contribution_factor if observation.factor is None else float(observation.factor)
        if observation.proxy and observation.known:
            proxies.append(spec.name)
        if spec.grade is EvidenceGrade.INVENTED and signed_value is not None and spec.scoring:
            invented_used.append(spec.name)
        contributions.append(
            Contribution(
                variable=spec.name,
                value=observation.value,
                support=observation.support,
                basis=observation.basis,
                signed_score=signed_value,
                weight_nats=spec.weight_nats,
                # ``raw`` stays the un-withheld figure on purpose: a caller has to be able
                # to see what the guard took away, not just that the total is smaller.
                raw_contribution_nats=raw_contribution,
                contribution_nats=applied_value,
                grade=spec.grade,
                provenance=spec.provenance,
                correlation_group=spec.correlation_group,
                contribution_factor=factor,
                factor_reason=spec.factor_reason,
                direction=spec.direction,
                proxy=observation.proxy,
                detail=observation.detail,
                known=observation.known,
                suppressed=spec.name in withheld,
                suppression_reason=(GROUNDLESS_GUARD if spec.name in withheld else None),
            )
        )

    evidence_sum = evidenced.total
    all_sum = everything.total
    score_evidenced = evidence_sum - penalty
    score_all = all_sum - penalty
    notes = list(obs.notes)
    if not invented_used:
        notes.append("no invented variable had a reading, so the two scores are identical by construction")
    if withheld:
        notes.append(
            f"groundless guard withheld positive credit from {len(withheld)} invented "
            f"variable(s): {', '.join(sorted(withheld))}"
        )

    all_groups = everything.groups
    return ConfluenceScore(
        chain=obs.chain,
        token=obs.token,
        as_of_ms=obs.as_of_ms,
        weights_version=ws.version,
        model_version=MODEL_VERSION,
        evidence_sum_nats=evidence_sum,
        invented_sum_nats=all_sum - evidence_sum,
        unknown_penalty_nats=penalty,
        score_evidenced_nats=score_evidenced,
        score_all_nats=score_all,
        delta_invented_nats=score_all - score_evidenced,
        coverage=coverage,
        missing_weight_nats=missing_weight,
        total_evidenced_weight_nats=total_weight,
        base_graduation_rate=ws.base_graduation_rate,
        contributions=tuple(contributions),
        groups=tuple(all_groups),
        unknowns=tuple(unknowns),
        proxies=tuple(proxies),
        invented_used=tuple(invented_used),
        grounded=grounded,
        measured_evidenced=tuple(measured),
        suppressed_invented=tuple(sorted(withheld)),
        point_in_time=obs.point_in_time,
        notes=tuple(notes),
    )


# --------------------------------------------------------------------------------------
# weight storage: the calibration hook
# --------------------------------------------------------------------------------------


def _params_from_json(raw: str | None) -> dict[str, float]:
    payload = jload(raw, {}) or {}
    out: dict[str, float] = {}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            try:
                out[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def load_weight_set(
    version: str | None = None, conn: sqlite3.Connection | None = None
) -> WeightSet:
    """Read a stored weight set. Falls back to :data:`LITERATURE_V1` when there is none.

    The fallback is not a convenience. The scorer has to be runnable against point-in-time
    data in a process that never opened the operator's database, and a weight set that only
    exists in a table would make replay depend on the machine it runs on.
    """
    c = conn if conn is not None else _safe_conn()
    if c is None:
        return LITERATURE_V1
    wanted = version
    try:
        if wanted is None:
            row = fetch_one(
                c,
                "SELECT version FROM confluence_weight_sets WHERE active=1 "
                "ORDER BY created_ms DESC LIMIT 1",
            )
            wanted = str(row["version"]) if row else DEFAULT_WEIGHTS_VERSION
        head = fetch_one(
            c,
            "SELECT version, created_ms, source, fitted_from_trades, unknown_penalty_cap_nats, "
            "base_graduation_rate, note FROM confluence_weight_sets WHERE version=?",
            (wanted,),
        )
        rows = fetch_all(
            c,
            "SELECT variable, weight_nats, grade, provenance, mapping, direction, params_json, "
            "correlation_group, contribution_factor, factor_reason, position "
            "FROM confluence_weights WHERE version=? ORDER BY position, variable",
            (wanted,),
        )
    except sqlite3.Error as exc:
        log.warning("confluence: weight tables unreadable (%s); using %s", exc, LITERATURE_V1.version)
        return LITERATURE_V1
    if head is None or not rows:
        if wanted != DEFAULT_WEIGHTS_VERSION:
            log.warning("confluence: weight set %r not found; using %s", wanted, LITERATURE_V1.version)
        return LITERATURE_V1

    variables = tuple(
        VariableSpec(
            name=str(r["variable"]),
            weight_nats=float(r["weight_nats"]),
            grade=EvidenceGrade(str(r["grade"])),
            provenance=str(r["provenance"]),
            mapping=str(r["mapping"]),
            direction=Direction(str(r["direction"])),
            params=_params_from_json(r["params_json"]),
            correlation_group=(
                str(r["correlation_group"]) if r["correlation_group"] is not None else None
            ),
            contribution_factor=float(r["contribution_factor"]),
            factor_reason=(str(r["factor_reason"]) if r["factor_reason"] is not None else None),
        )
        for r in rows
    )
    fitted = head["fitted_from_trades"]
    loaded = WeightSet(
        version=str(head["version"]),
        variables=variables,
        source=str(head["source"]),
        created_ms=int(head["created_ms"] or 0),
        fitted_from_trades=None if fitted is None else int(fitted),
        unknown_penalty_cap_nats=float(head["unknown_penalty_cap_nats"]),
        base_graduation_rate=float(head["base_graduation_rate"]),
        note=(str(head["note"]) if head["note"] is not None else None),
    )
    problems = validate_weight_set(loaded)
    if problems:
        # Loud, and then used anyway: refusing to score would be a worse failure than
        # scoring with a set whose annotation is imperfect, and the operator needs to see
        # which row is wrong rather than a silent fallback to a different model.
        log.error("confluence: weight set %s has %d problems: %s", loaded.version, len(problems),
                  "; ".join(problems[:5]))
    return loaded


def save_weight_set(
    weights: WeightSet, conn: sqlite3.Connection | None = None, *, activate: bool = False
) -> int:
    """Write a weight set as a new version. This is how a future fit lands.

    Rows are replaced within the version, never merged, so a partially-written fit cannot
    leave a hybrid of two models behind. Activating one deactivates every other.
    """
    c = conn if conn is not None else get_conn()
    written = 0
    with tx(c):
        upsert(
            c,
            "confluence_weight_sets",
            {
                "version": weights.version,
                "created_ms": int(weights.created_ms or now_ms()),
                "source": weights.source,
                "fitted_from_trades": weights.fitted_from_trades,
                "unknown_penalty_cap_nats": float(weights.unknown_penalty_cap_nats),
                "base_graduation_rate": float(weights.base_graduation_rate),
                "active": 1 if activate else 0,
                "note": weights.note,
            },
            conflict=["version"],
            update=[
                "created_ms",
                "source",
                "fitted_from_trades",
                "unknown_penalty_cap_nats",
                "base_graduation_rate",
                "active",
                "note",
            ],
        )
        if activate:
            c.execute(
                "UPDATE confluence_weight_sets SET active=0 WHERE version<>?", (weights.version,)
            )
        c.execute("DELETE FROM confluence_weights WHERE version=?", (weights.version,))
        for position, spec in enumerate(weights.variables):
            c.execute(
                "INSERT INTO confluence_weights (version, variable, weight_nats, grade, provenance, "
                "mapping, direction, params_json, correlation_group, contribution_factor, "
                "factor_reason, position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    weights.version,
                    spec.name,
                    float(spec.weight_nats),
                    spec.grade.value,
                    spec.provenance,
                    spec.mapping,
                    spec.direction.value,
                    json.dumps(dict(spec.params), sort_keys=True, separators=(",", ":")),
                    spec.correlation_group,
                    float(spec.contribution_factor),
                    spec.factor_reason,
                    position,
                ),
            )
            written += 1
    return written


def record_score(result: ConfluenceScore, conn: sqlite3.Connection | None = None) -> str:
    """Persist one score so a later fit has a point-in-time record to learn from."""
    c = conn if conn is not None else get_conn()
    row = result.to_row()
    row["score_id"] = digest(
        [row["chain"], row["token"], row["as_of_ms"], row["weights_version"], row["model_version"]]
    )[:32]
    row["created_ms"] = now_ms()
    upsert(
        c,
        "confluence_scores",
        row,
        conflict=["score_id"],
        update=[
            "score_evidenced_nats",
            "score_all_nats",
            "delta_invented_nats",
            "unknown_penalty_nats",
            "coverage",
            "divergent",
            "point_in_time",
            "breakdown_json",
            "created_ms",
        ],
    )
    return str(row["score_id"])


#: docs/research/13 §B3: for a 30-40% hit rate with a fat right tail, 3,500-5,500 closed
#: trades for a bare t = 1.96, and 10,000-25,000 once deflated for 100-1,000 configurations
#: tried. Anything below the first number cannot distinguish a fitted weight from noise.
MIN_TRADES_FOR_NAIVE_FIT = 3_500
MIN_TRADES_FOR_DEFLATED_FIT = 10_000


def calibration_status(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """How far we are from being allowed to fit weights. Deliberately does not fit anything.

    A weight fitted on a handful of closed trades is worse than a weight taken from a paper,
    because it carries the same authority and none of the sample. This function exists so
    that the refusal is a measured refusal rather than an omission.
    """
    c = conn if conn is not None else _safe_conn()
    closed = 0
    if c is not None:
        try:
            row = fetch_one(c, "SELECT COUNT(*) AS n FROM trades")
            closed = int(row["n"]) if row else 0
        except sqlite3.Error:
            closed = 0
    return {
        "closed_trades": closed,
        "min_trades_naive": MIN_TRADES_FOR_NAIVE_FIT,
        "min_trades_deflated": MIN_TRADES_FOR_DEFLATED_FIT,
        "can_fit": False,
        "reason": (
            f"{closed} closed trades against a requirement of {MIN_TRADES_FOR_NAIVE_FIT} for a bare "
            f"t=1.96 and {MIN_TRADES_FOR_DEFLATED_FIT} once deflated (docs/research/13 §B3). "
            "No fit is performed at any sample size by this module; fitting is a separate, "
            "registered trial that writes a new weights version."
        ),
        "active_version": DEFAULT_WEIGHTS_VERSION,
    }


# --------------------------------------------------------------------------------------
# reading the database into observations
# --------------------------------------------------------------------------------------


def _safe_conn() -> sqlite3.Connection | None:
    try:
        return get_conn()
    except Exception as exc:  # pragma: no cover - only when no data dir exists at all
        log.warning("confluence: no database connection (%s)", exc)
        return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _measure_value(payload: Any) -> tuple[float | None, EvidenceBasis]:
    """Pull a value and its basis out of a serialised :class:`kaiba.core.schemas.Measure`."""
    if not isinstance(payload, Mapping):
        return None, EvidenceBasis.UNAVAILABLE
    value = _to_float(payload.get("value"))
    try:
        basis = EvidenceBasis(str(payload.get("basis", EvidenceBasis.UNAVAILABLE.value)))
    except ValueError:
        basis = EvidenceBasis.UNAVAILABLE
    if value is None:
        return None, EvidenceBasis.UNAVAILABLE
    return value, basis


def _latest_dossier(
    chain: Chain, token: str, conn: sqlite3.Connection, as_of_ms: int
) -> Mapping[str, Any] | None:
    row = fetch_one(
        conn,
        "SELECT dossier_json FROM token_dossiers WHERE chain=? AND address=? AND built_at_ms<=?",
        (chain.value, token, int(as_of_ms)),
    )
    if row is None:
        return None
    payload = jload(str(row["dossier_json"]), None)
    return payload if isinstance(payload, Mapping) else None


def _copycat_observation(
    chain: Chain, token: str, conn: sqlite3.Connection
) -> tuple[Observation, bool]:
    """Read the cached de-duplication verdict without touching the network.

    Returns ``(observation, point_in_time)``. The registry resolves "who used this
    fingerprint first" against its current contents, so a replay can see a verdict that
    depends on a mint registered after ``as_of_ms``. That is a real replay limitation and
    the caller is told rather than left to assume.
    """
    from kaiba.intelligence import dedup

    record = dedup.scan_record(chain, token, conn)
    if record is None:
        return Observation.unavailable("mint has never been de-duplication scanned"), True
    fingerprints = dedup.cached_fingerprints(chain, token, conn)
    if not fingerprints:
        return Observation.unavailable("scanned but no usable fingerprint was recovered"), True
    meta = dedup.TokenMeta(mint=token, chain=chain, created_ms=_int_or_none(record.get("created_ms")))
    verdict = dedup.classify_meta(
        meta,
        conn,
        image_status=(str(record["image_status"]) if record.get("image_status") else None),
        register_mint=False,
        extra=fingerprints,
    )
    if verdict.status is dedup.Status.UNKNOWN:
        return Observation.unavailable(verdict.note or "de-duplication verdict is unknown"), False
    if verdict.status is dedup.Status.COPYCAT:
        best = verdict.best
        return (
            Observation(
                value=-1.0,
                basis=EvidenceBasis.DERIVED,
                detail=verdict.summary(),
                proxy=not (best is not None and best.proof),
            ),
            False,
        )
    return Observation(value=1.0, basis=EvidenceBasis.DERIVED, detail="original"), False


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _creator_observation(
    chain: Chain, token: str, conn: sqlite3.Connection
) -> Observation:
    row = fetch_one(
        conn, "SELECT creator FROM tokens WHERE chain=? AND address=?", (chain.value, token)
    )
    creator = str(row["creator"]) if row and row.get("creator") else None
    if not creator:
        return Observation.unavailable("token has no recorded creator")
    history = fetch_one(
        conn,
        "SELECT launches, graduated FROM creators WHERE chain=? AND address=?",
        (chain.value, creator),
    )
    if history is None:
        return Observation.unavailable(f"no launch history backfilled for creator {creator[:8]}")
    launches = _to_float(history["launches"]) or 0.0
    graduated = _to_float(history["graduated"]) or 0.0
    if launches <= 0.0:
        return Observation.unavailable("creator row holds zero launches, which is not a measurement")
    return Observation(
        value=graduated / launches,
        basis=EvidenceBasis.DERIVED,
        support=launches,
        detail=f"{int(graduated)}/{int(launches)} graduated, creator {creator[:8]}",
    )


def _swap_rows(
    chain: Chain, token: str, conn: sqlite3.Connection, as_of_ms: int
) -> list[dict[str, Any]]:
    return fetch_all(
        conn,
        "SELECT tx, ts_ms, wallet, side FROM swaps WHERE chain=? AND token=? AND ts_ms<=? "
        "ORDER BY ts_ms, id",
        (chain.value, token, int(as_of_ms)),
    )


def wash_transactions(rows: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
    """Count wash transactions using CCS'26's own two definitions.

    ``WT1`` is an atomic same-transaction buy and sell by one wallet. ``WT2`` is the same
    wallet on both sides inside a five-second window. Returns ``(wt1, wt2, combined)``
    where combined counts a wallet-pairing once.
    """
    by_tx: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (str(row.get("tx") or ""), str(row.get("wallet") or ""))
        by_tx.setdefault(key, set()).add(str(row.get("side") or "").lower())
    wt1 = sum(1 for sides in by_tx.values() if {"buy", "sell"} <= sides)

    ordered: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        wallet = str(row.get("wallet") or "")
        stamp = _int_or_none(row.get("ts_ms"))
        side = str(row.get("side") or "").lower()
        if not wallet or stamp is None or side not in {"buy", "sell"}:
            continue
        ordered.setdefault(wallet, []).append((stamp, side))
    wt2 = 0
    for events in ordered.values():
        events.sort()
        for index, (stamp, side) in enumerate(events):
            for later_stamp, later_side in events[index + 1 :]:
                if later_stamp - stamp > WASH_WINDOW_MS:
                    break
                if later_side != side:
                    wt2 += 1
                    break
    return wt1, wt2, max(wt1, wt2)


def early_turnover_share(
    rows: Sequence[Mapping[str, Any]], created_ms: int | None
) -> tuple[float | None, int]:
    """Turnover concentration over the early window: ``1 - distinct wallets / swaps``.

    A proxy for the paper's bot-dominance flag, and a different proxy from the paper's own.
    Returns ``(share, swaps_in_window)``; ``share`` is ``None`` when the window is empty.
    """
    if created_ms is not None:
        cutoff = created_ms + int(EARLY_WINDOW_S * 1000)
        window = [r for r in rows if (_int_or_none(r.get("ts_ms")) or 0) <= cutoff]
    else:
        window = list(rows[:50])
    if not window:
        return None, 0
    wallets = {str(r.get("wallet") or "") for r in window if r.get("wallet")}
    if not wallets:
        return None, len(window)
    return 1.0 - (len(wallets) / len(window)), len(window)


LAMPORTS_PER_SOL = 1_000_000_000


def _gross_inflow_per_buy(
    chain: Chain, token: str, conn: sqlite3.Connection, as_of_ms: int, *, created_ms: int | None
) -> Observation:
    """Gross SOL bought per buy, from our own swap rows. Only valid covered from launch.

    The published quantity is SOL accumulated per trade. ``sol_in_curve`` answers a
    different question - what is still committed - and on live mints that took money and
    gave it back it reads as one lamport across dozens of trades. Gross inflow per buy is
    the closest thing our swap table holds to the published quantity, and it is still a
    proxy: gross is an upper bound on accumulation, so this reads high where sells are
    heavy. Flagged accordingly rather than passed off as the measured variable.
    """
    if created_ms is None:
        return Observation.unavailable("creation time unknown, so swap coverage cannot be checked")
    row = fetch_one(
        conn,
        "SELECT COUNT(*) AS n, MIN(ts_ms) AS first_ms, "
        "SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buys, "
        "SUM(CASE WHEN side='buy' THEN CAST(amount_native AS INTEGER) ELSE 0 END) AS inflow "
        "FROM swaps WHERE chain=? AND token=? AND ts_ms<=?",
        (chain.value, token, int(as_of_ms)),
    )
    if row is None or not row["n"]:
        return Observation.unavailable("no swap rows held for this mint")
    first_ms = _int_or_none(row["first_ms"])
    buys = _int_or_none(row["buys"]) or 0
    inflow = _int_or_none(row["inflow"]) or 0
    if first_ms is None:
        return Observation.unavailable("no swap rows held for this mint")
    lag_s = (first_ms - created_ms) / 1000.0
    if lag_s > 60:  # kaiba.ingest.token_flow.FlowConfig.coverage_grace_s
        return Observation.unavailable(f"swap coverage starts {lag_s:.0f}s after launch")
    if buys <= 0 or inflow <= 0:
        return Observation.unavailable("no priced buy-side flow collected for this mint")
    return Observation(
        value=(inflow / LAMPORTS_PER_SOL) / float(buys),
        basis=EvidenceBasis.ESTIMATED,
        support=float(buys),
        proxy=True,
        detail=f"gross inflow {inflow / LAMPORTS_PER_SOL:.4f} SOL over {buys} covered buys",
    )


def _velocity_observation(
    chain: Chain, token: str, conn: sqlite3.Connection, as_of_ms: int, *, created_ms: int | None
) -> Observation:
    """SOL per swap. Three routes, preferred in order of how close each is to the paper.

    1. The delta between two curve snapshots, which is what
       :func:`kaiba.ingest.token_flow.velocity_between` computes and the only route whose
       refusals are already audited.
    2. Gross inflow per buy off our own swap rows, covered from launch.
    3. The cumulative ``sol_in_curve / trades_seen``, which is what the lane reads today
       and is furthest from the published quantity.
    """
    from kaiba.ingest import token_flow

    snapshots = fetch_all(
        conn,
        "SELECT observed_ms, real_sol_lamports, sol_in_curve, trades_seen, coverage_from_ms, "
        "created_ms FROM curve_snapshots WHERE chain=? AND token=? AND observed_ms<=? "
        "ORDER BY observed_ms DESC LIMIT 2",
        (chain.value, token, int(as_of_ms)),
    )
    if not snapshots:
        gross = _gross_inflow_per_buy(chain, token, conn, as_of_ms, created_ms=created_ms)
        if gross.known:
            return gross
        return Observation.unavailable(
            f"no curve snapshot held for this mint; {gross.detail}"
        )
    newest = snapshots[0]
    if len(snapshots) >= 2:
        older = snapshots[1]
        rows = _swap_rows(chain, token, conn, as_of_ms)
        lo = _int_or_none(older.get("observed_ms")) or 0
        hi = _int_or_none(newest.get("observed_ms")) or 0
        window = [r for r in rows if lo < (_int_or_none(r.get("ts_ms")) or 0) <= hi]
        result = token_flow.velocity_between(older, newer=newest, trades=len(window))
        value = result.get("sol_per_swap")
        if value is not None:
            return Observation(
                value=_to_float(value),
                basis=EvidenceBasis.DERIVED,
                support=float(len(window)),
                detail=f"delta of two snapshots, {len(window)} trades in interval",
            )
        refusal = str(result.get("refusal") or "no rate derivable from the snapshot pair")
    else:
        refusal = "only one curve snapshot, and a rate needs two observations"

    gross = _gross_inflow_per_buy(chain, token, conn, as_of_ms, created_ms=created_ms)
    if gross.known:
        return gross

    # Cumulative fallback: only sound when our trade collection reaches back to launch, and
    # only a proxy even then. `sol_in_curve` is the SOL *still in* the curve, so a mint that
    # took real money and gave all of it back reads as ~0 across dozens of trades - a true
    # statement about what is committed now and a false one about capital per participant
    # (kaiba.ingest.token_flow.gross_flow documents exactly this on live 2026-09-20 mints).
    # A non-positive balance is therefore refused rather than reported as a floor reading,
    # which is the same refusal token_flow.velocity_between makes as `no_sol_added`.
    covered = _int_or_none(newest.get("coverage_from_ms"))
    created = _int_or_none(newest.get("created_ms"))
    trades = _int_or_none(newest.get("trades_seen"))
    sol_in_curve = _to_float(newest.get("sol_in_curve"))
    if covered is None or created is None or trades is None or not trades or sol_in_curve is None:
        return Observation.unavailable(refusal)
    if covered > created + int(token_flow.DEFAULT_CONFIG.coverage_grace_s * 1000):
        return Observation.unavailable(f"{refusal}; cumulative fallback not covered from launch")
    if sol_in_curve <= 0.0:
        return Observation.unavailable(
            f"{refusal}; curve holds {sol_in_curve} SOL, which is a net-flow fact and not a velocity"
        )
    return Observation(
        value=sol_in_curve / float(trades),
        basis=EvidenceBasis.ESTIMATED,
        support=float(trades),
        proxy=True,
        detail=f"cumulative: {sol_in_curve:.4f} SOL still in curve over {trades} covered trades",
    )


def _entity_observation(
    chain: Chain, token: str, conn: sqlite3.Connection, as_of_ms: int
) -> Observation:
    """Distinct operators behind the buyers, not distinct addresses.

    Gated on the mint's trade tape being proved complete, because the count is *how many
    entities bought this mint* and the rows are only that when we hold every trade it
    ever had. On a partial tape the same query returns how many entities bought it **in
    the slice we pulled**, which is a floor of an unknown number: the pump.fun trades
    endpoint serves a hot window, so for most mints in this database the slice is all we
    will ever have (:mod:`kaiba.ingest.tape`).

    A floor is not nothing, so it is still reported — as ``support`` and in ``detail``,
    where a reader can see it — but with an UNAVAILABLE basis, so the scorer cannot spend
    it. Both directions matter here: an under-count reads as *fewer independent buyers*,
    which is the adverse side of this variable, and inventing an adverse reading out of
    our own sampling would be as wrong as inventing a clean one. ``independent_entity_count``
    already collapses addresses to operators, so the number is a floor twice over.

    The gate is :func:`kaiba.ingest.tape.completeness` itself rather than a local
    re-derivation of it; that rule is already implemented four times in this tree with two
    different grace constants, and a fifth copy is how they came to disagree.
    """
    from kaiba.ingest.tape import completeness
    from kaiba.intelligence.entity import independent_entity_count

    rows = fetch_all(
        conn,
        "SELECT DISTINCT wallet FROM swaps WHERE chain=? AND token=? AND ts_ms<=? AND side='buy'",
        (chain.value, token, int(as_of_ms)),
    )
    addresses = [str(r["wallet"]) for r in rows if r.get("wallet")]
    if not addresses:
        return Observation.unavailable("no buy-side wallets observed for this mint")
    try:
        entities = independent_entity_count(chain, addresses, conn)
    except (sqlite3.Error, ValueError) as exc:
        return Observation.unavailable(f"entity graph unreadable: {exc}")

    complete, why = completeness(chain, token, conn)
    if not complete:
        return Observation(
            value=None,
            basis=EvidenceBasis.UNAVAILABLE,
            support=float(len(addresses)),
            detail=(
                f"trade tape not proved complete ({why}), so the {len(addresses)} buy "
                f"addresses we hold collapse to at least {entities} entities and an unknown "
                "number above that; a count over the slice we pulled is not a count of this "
                "mint's buyers"
            ),
        )
    return Observation(
        value=float(entities),
        basis=EvidenceBasis.DERIVED,
        support=float(len(addresses)),
        detail=(
            f"{len(addresses)} buy addresses collapse to {entities} entities, over a trade "
            f"tape proved complete back to launch ({why})"
        ),
    )


def observe_from_db(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    as_of_ms: int | None = None,
    concentration_report: Any | None = None,
    extra: Mapping[str, Observation] | None = None,
) -> Observations:
    """Build the scorer's input from what the database holds at ``as_of_ms``.

    Every reader below filters on the caller's timestamp where the table carries one. Two
    sources cannot be replayed exactly and say so through
    :attr:`Observations.point_in_time`: the de-duplication registry resolves first-use
    against its present contents, and a dossier is a snapshot whose provider readings were
    taken at scan time rather than at ``as_of_ms``.

    ``concentration_report`` takes a :class:`kaiba.intelligence.concentration.ConcentrationReport`.
    It is a parameter rather than a lookup because kaiba stores no holder lists, so the
    bundle-adjusted variable is unavailable unless the caller has just computed it.
    """
    c = conn if conn is not None else get_conn()
    stamp = int(as_of_ms if as_of_ms is not None else now_ms())
    values: dict[str, Observation] = {}
    notes: list[str] = []
    point_in_time = True

    copycat, copycat_pit = _copycat_observation(chain, token, c)
    values["copycat_reuse"] = copycat
    point_in_time = point_in_time and copycat_pit
    if not copycat_pit:
        notes.append(
            "dedup registry resolves first-use against its current contents, so this verdict is "
            "not exactly replayable as of as_of_ms"
        )

    values["creator_graduation_history"] = _creator_observation(chain, token, c)

    token_row = fetch_one(
        c, "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token)
    )
    created_ms = _int_or_none(token_row["created_ms"]) if token_row else None
    values["curve_velocity_sol_per_swap"] = _velocity_observation(
        chain, token, c, stamp, created_ms=created_ms
    )

    rows = _swap_rows(chain, token, c, stamp)
    if rows:
        wt1, wt2, combined = wash_transactions(rows)
        values["wash_trading"] = Observation(
            value=float(combined),
            basis=EvidenceBasis.DERIVED,
            support=float(len(rows)),
            detail=f"WT1={wt1} atomic, WT2={wt2} in a {WASH_WINDOW_MS}ms window, {len(rows)} swaps held",
        )
        share, in_window = early_turnover_share(rows, created_ms)
        if share is None:
            values["bot_dominated_early_activity"] = Observation.unavailable(
                "no swaps inside the early window"
            )
        else:
            values["bot_dominated_early_activity"] = Observation(
                value=share,
                basis=EvidenceBasis.ESTIMATED,
                support=float(in_window),
                proxy=True,
                detail=(
                    f"turnover concentration over {in_window} early swaps "
                    f"({'first 300s' if created_ms is not None else 'first 50 swaps'})"
                ),
            )
    else:
        values["wash_trading"] = Observation.unavailable("no swap rows held for this mint")
        values["bot_dominated_early_activity"] = Observation.unavailable(
            "no swap rows held for this mint"
        )

    if concentration_report is not None and getattr(concentration_report, "adjustment_available", False):
        delta = getattr(concentration_report, "delta_pct", None)
        if delta is None:
            values["bundle_adjusted_concentration"] = Observation.unavailable(
                "concentration report ran but produced no delta"
            )
        else:
            values["bundle_adjusted_concentration"] = Observation(
                value=_to_float(delta),
                basis=EvidenceBasis.DERIVED,
                support=float(getattr(concentration_report, "holder_count", 0) or 0),
                detail=getattr(concentration_report, "note", None) or "adjusted minus raw, pp",
            )
    else:
        values["bundle_adjusted_concentration"] = Observation.unavailable(
            "no bundle-adjusted concentration supplied: kaiba stores no holder lists and the "
            "cluster graph must be non-empty for the adjustment to run"
        )

    dossier = _latest_dossier(chain, token, c, stamp)
    if dossier is None:
        for name in (
            "raw_top10_concentration",
            "dev_bought_own_bundle",
            "freeze_authority_live",
            "sniper_exposure",
            "wallet_pnl_grade",
            "raw_top10_threshold",
            "dev_supply_threshold",
            "bundler_exposure_threshold",
            "sniper_exposure_threshold",
            "liquidity_floor",
        ):
            values[name] = Observation.unavailable("no dossier built for this mint")
    else:
        point_in_time = False
        notes.append("dossier readings were taken at scan time, not at as_of_ms")
        top10, top10_basis = _measure_value(dossier.get("top10_pct"))
        dev, dev_basis = _measure_value(dossier.get("dev_pct"))
        bundler, bundler_basis = _measure_value(dossier.get("bundler_pct"))
        sniper, sniper_basis = _measure_value(dossier.get("sniper_pct"))
        liquidity, liquidity_basis = _measure_value(dossier.get("liquidity_usd"))
        frozen = dossier.get("freeze_authority_revoked")
        graded = dossier.get("graded_wallets")

        values["raw_top10_concentration"] = Observation(top10, top10_basis, detail="top-10 %")
        values["raw_top10_threshold"] = Observation(top10, top10_basis, detail="top-10 %")
        values["dev_bought_own_bundle"] = Observation(dev, dev_basis, detail="dev supply %")
        values["dev_supply_threshold"] = Observation(dev, dev_basis, detail="dev supply %")
        values["bundler_exposure_threshold"] = Observation(
            bundler, bundler_basis, detail="bundler supply %"
        )
        values["sniper_exposure"] = Observation(sniper, sniper_basis, detail="sniper supply %")
        values["sniper_exposure_threshold"] = Observation(
            sniper, sniper_basis, detail="sniper supply %"
        )
        values["liquidity_floor"] = Observation(liquidity, liquidity_basis, detail="liquidity USD")
        values["freeze_authority_live"] = (
            Observation(
                value=0.0 if frozen else 1.0,
                basis=EvidenceBasis.PROVIDER_REPORTED,
                detail="freeze authority revoked" if frozen else "freeze authority still live",
            )
            if frozen is not None
            else Observation.unavailable("freeze authority state not established")
        )
        values["wallet_pnl_grade"] = (
            Observation(
                value=float(len(graded)),
                basis=EvidenceBasis.DERIVED,
                detail=f"{len(graded)} graded wallets seen on this token",
            )
            if isinstance(graded, list)
            else Observation.unavailable("no graded wallets recorded")
        )

    values["independent_entity_count"] = _entity_observation(chain, token, c, stamp)

    if extra:
        values.update(extra)

    return Observations(
        chain=chain,
        token=token,
        as_of_ms=stamp,
        values=values,
        point_in_time=point_in_time,
        notes=tuple(notes),
    )


def score_from_db(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    as_of_ms: int | None = None,
    concentration_report: Any | None = None,
    weights: WeightSet | None = None,
    extra: Mapping[str, Observation] | None = None,
) -> ConfluenceScore:
    """The one call a lane or the scanner makes.

    ``confluence.score_from_db(chain, token, conn)`` returns the score, both tiers of it,
    the per-variable breakdown and the evidence grade behind every weight.
    """
    c = conn if conn is not None else get_conn()
    observations = observe_from_db(
        chain, token, c, as_of_ms=as_of_ms, concentration_report=concentration_report, extra=extra
    )
    return score(observations, weights or load_weight_set(conn=c))


def distribution(
    results: Iterable[ConfluenceScore],
) -> dict[str, Any]:
    """Summary statistics over a batch. Used by the operator report, not by any lane."""
    items = list(results)
    if not items:
        return {"n": 0}
    evidenced = sorted(r.score_evidenced_nats for r in items)
    coverage = sorted(r.coverage for r in items)
    deltas = sorted(abs(r.delta_invented_nats) for r in items)

    def _quantile(values: Sequence[float], q: float) -> float:
        if not values:
            return float("nan")
        index = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return values[index]

    return {
        "n": len(items),
        "evidenced_min": evidenced[0],
        "evidenced_p25": _quantile(evidenced, 0.25),
        "evidenced_median": _quantile(evidenced, 0.5),
        "evidenced_p75": _quantile(evidenced, 0.75),
        "evidenced_max": evidenced[-1],
        "evidenced_spread": evidenced[-1] - evidenced[0],
        "distinct_evidenced_scores": len({round(v, 6) for v in evidenced}),
        "coverage_median": _quantile(coverage, 0.5),
        "coverage_max": coverage[-1],
        "zero_coverage": sum(1 for r in items if r.coverage <= 0.0),
        "delta_median": _quantile(deltas, 0.5),
        "delta_max": deltas[-1],
        "divergent": sum(1 for r in items if r.divergent),
        "sign_flipped": sum(1 for r in items if r.sign_flipped),
    }


__all__ = [
    "BASE_GRADUATION_RATE",
    "CITATION_MARKERS",
    "COPYCAT_SEPARATION_NATS",
    "LITERATURE_V1",
    "MEASURED_FLOOR_NATS",
    "MODEL_VERSION",
    "RULE_U_WEIGHT_NATS",
    "WASH_SEPARATION_NATS",
    "ConfluenceScore",
    "Contribution",
    "Direction",
    "EvidenceGrade",
    "GROUNDLESS_GUARD",
    "GroupAdjustment",
    "Observation",
    "Observations",
    "Standing",
    "VariableSpec",
    "WeightSet",
    "calibration_status",
    "distribution",
    "early_turnover_share",
    "load_weight_set",
    "observe_from_db",
    "record_score",
    "save_weight_set",
    "score",
    "score_from_db",
    "validate_weight_set",
    "wash_transactions",
]
