"""How much of what predicts a rug did we actually establish?

``dyor.score_dossier`` answers "what did we find wrong". This module answers the separate
question "how much did we look", and the two answers must not be added together.

**The bug this exists to fix.** Measured 2026-09-20 over 615 stored dossiers: grade A
carried an average of **6.9 unknown fields** and grade B **6.8** — indistinguishable.
``bundler_pct`` and ``sniper_pct`` were unknown on **100%** of them, ``insider_pct``,
``top10_pct``, ``buy_tax_bps`` and ``sell_tax_bps`` on **97%**. So an A meant "we found
nothing wrong", not "we checked and it is fine", and 83 points out of 100 were being
awarded while knowing nothing about concentration, bundling, sniping or taxes — precisely
the properties that predict a rug. Against the only labelled outcomes the system has (26
closed ``migration-fade`` trades, 13 of which exited to the rug monitor) the grade had no
discriminating power at all: 80.52 average score for the tokens that rugged against 79.51
for the ones that did not, 6 A's among the 13 that rugged.

**The fix is a separation, not a new number.** A token with nothing wrong found and seven
unknowns is not the same object as a token with nothing wrong found and zero unknowns.
Coverage is reported as its own object with its own tier, and its only effect on the grade
is a **ceiling**: it can lower a grade, never raise one, and it never touches the score.
Folding it into the score is what produced the bug, because one number cannot carry both
"clean" and "examined" and the reader cannot tell which one moved.

**Two mechanisms, deliberately.**

1. :data:`REQUIRED_FAMILIES` — the top grade requires *concentration, bundling, sniping
   and taxes* each to be fully established. This is a set-membership test with no knob in
   it, and it is the operator's requirement stated literally: a dossier that cannot
   establish those four must not be able to reach A however clean the fields we did read.
2. :data:`FIELD_WEIGHTS` — below A, how bad the remaining gap is, weighted by *published
   separation* rather than equally. ``bundler_pct`` unknown is not ``holder_count``
   unknown.

**Where the weights come from.** ``docs/EDGE-AND-VARIABLES.md`` §1 ranks the variables by
published evidence and supplies exactly two absolute figures: bundle-adjusted holder
concentration separates at **24pp** (§1 #5) and naive top-10 at **6pp** (§1 #8). Those two
are used as-is and define the interval; every other field is placed inside it by §1's own
*rank*, and each placement says in :data:`PROVENANCE` whether the magnitude is evidenced
or invented. No ranking is invented here.

**What is deliberately not in the rubric, and why** — so the mapping to EDGE §1 is
auditable rather than convenient:

* §1 #1 (IPFS content-hash reuse, 10.7×) reaches the dossier as ``copycat``, but it is not
  persisted on ``TokenDossier``, so including it would make coverage unrecoverable from a
  stored row. Its separation is also unverified for 2026: our own 1,038-launch measurement
  put the copycat base rate at 46.4% against the paper's 10.2%.
* §1 #2 (mechanical brackets), #3 (curve velocity) and #7 (certificate transparency) are
  not token-safety properties at all — they live in ``execution/protection.py``,
  ``execution/lanes.py`` and the announcement watchers.
* §1 #4 is a **3-hop funding graph over creators** (``intelligence/creators.py``). The
  dossier's ``cluster_pct`` is RugCheck's insider-network share, which is a different
  measurement, so it does **not** inherit #4's rank. Saying otherwise would be the same
  substring-collision error that quarantined 45 tokens wrongly (TRADING-METHOD §4).
* §1 #9 (wallet PnL grading) has no established persistence and is not a token property.
* ``can_sell``, ``mint_authority_revoked`` and ``freeze_authority_revoked`` are excluded on
  purpose. They already have their own gate (``dyor.CRITICAL_PROPERTIES`` plus the
  ``no_security_coverage`` blocker), they are free to obtain, and EDGE §4 #13 says of
  freeze authority in as many words: "it should not be counted as coverage" — it is 0.6%
  of Solana rugs. Counting cheap near-constants as coverage is how ``evidence_weight``
  reached 80/100 on a dossier that knew nothing about concentration.

**Every field in the rubric is in ``dyor.TRACKED_PROPERTIES``**, which is what makes
:func:`assess_unknowns` exact: a caller holding nothing but a stored ``unknowns_json``
recovers the same tier the scan computed, with no provider call and no migration.
``tests/test_coverage.py`` pins that.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kaiba.core.schemas import Grade

# --------------------------------------------------------------------------------------
# the rubric
# --------------------------------------------------------------------------------------


#: What establishing each property is worth, in "separation points". The two evidenced
#: numbers (24 for bundling, 6 for raw top-10) come straight from EDGE §1 and define the
#: scale; everything else is placed inside that interval by EDGE §1's rank order. See
#: :data:`PROVENANCE` for the per-field argument.
FIELD_WEIGHTS: dict[str, int] = {
    "bundler_pct": 24,
    "cluster_pct": 12,
    "insider_pct": 12,
    "sniper_pct": 12,
    "top10_pct": 6,
    "dev_pct": 6,
    "buy_tax_bps": 6,
    "sell_tax_bps": 6,
    "holder_count": 2,
}

#: The properties grouped into the things a caller asks about. ``holders`` exists so
#: ``holder_count`` has somewhere to live without being required for the top grade.
FAMILIES: dict[str, tuple[str, ...]] = {
    "concentration": ("top10_pct", "insider_pct", "cluster_pct", "dev_pct"),
    "bundling": ("bundler_pct",),
    "sniping": ("sniper_pct",),
    "taxes": ("buy_tax_bps", "sell_tax_bps"),
    "holders": ("holder_count",),
}

#: The operator's requirement, verbatim: a dossier that cannot establish concentration,
#: bundling, sniping or taxes must not be able to reach the top grade. Every field of each
#: named family must be established — there is no fractional credit, because a family half
#: read is a family we cannot claim to have established. That is also the specific bug the
#: score has: ``dyor._components`` awards the whole 14-point ``insider_supply`` block when
#: any *one* of five supply splits is known, which is how ``dev_pct`` alone bought credit
#: for bundling and sniping.
REQUIRED_FAMILIES: tuple[str, ...] = ("concentration", "bundling", "sniping", "taxes")

TOTAL_WEIGHT: int = sum(FIELD_WEIGHTS.values())

#: Derived, not tuned: to count as examined at all, a scan must have established at least
#: as much separation weight as the single strongest published variable. That is
#: ``max(FIELD_WEIGHTS.values())`` — 24, bundle-adjusted concentration — computed rather
#: than written down, so it cannot drift away from the rubric and cannot be nudged.
PARTIAL_MIN_WEIGHT: int = max(FIELD_WEIGHTS.values())


class CoverageTier(StrEnum):
    """How much of the predictive surface a scan managed to establish."""

    #: Every required family established. Nothing is capped.
    FULL = "full"
    #: Some of it, worth at least the strongest single variable.
    PARTIAL = "partial"
    #: Less than that. We looked at the free properties and essentially nothing else.
    BLIND = "blind"


#: The ceiling each tier imposes. BLIND stops at C rather than at D or UNSCORED because a
#: blind scan is not a *bad* result and not an absent one — the authority and sellability
#: checks still ran and still mean something. C is the honest reading: "clean as far as we
#: looked, and we did not look far."
TIER_MAX_GRADE: dict[CoverageTier, Grade] = {
    CoverageTier.FULL: Grade.A,
    CoverageTier.PARTIAL: Grade.B,
    CoverageTier.BLIND: Grade.C,
}

#: Ordering for the ceiling comparison. Declared locally rather than imported from
#: ``execution.lanes.GRADE_POINTS``: intelligence must not depend on execution, and this
#: table deliberately omits QUARANTINED and UNSCORED because neither is a claim of
#: cleanliness and neither may be capped.
_GRADE_RANK: dict[Grade, int] = {Grade.A: 4, Grade.B: 3, Grade.C: 2, Grade.D: 1}


# --------------------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Where a constant came from. Every module-level constant here has one.

    ``tests/test_coverage.py::test_every_constant_has_provenance`` parses this file and
    fails if a constant appears without a row, the way ``execution/viability.py`` and
    ``intelligence/bundles.py`` do it. A silently added knob is how an unmeasured number
    becomes load-bearing, and this module exists because exactly that happened to
    ``dyor.A_MIN_EVIDENCE_WEIGHT``.
    """

    value: Any
    unit: str
    source: str
    note: str


PROVENANCE: dict[str, Provenance] = {
    "FIELD_WEIGHTS": Provenance(
        FIELD_WEIGHTS, "separation points per property",
        "docs/EDGE-AND-VARIABLES.md §1, ranked by published evidence",
        "EVIDENCED at both ends, INTERPOLATED in the middle, by field: "
        "bundler_pct=24 is EVIDENCED — §1 #5 measures a 24pp discriminative gap for "
        "bundle-adjusted holder concentration. "
        "top10_pct=6 is EVIDENCED — §1 #8 measures 6pp for naive top-10, and §4 #15 adds "
        "that no published study establishes any threshold on it. "
        "sniper_pct=12 is RANKED, magnitude INVENTED — §1 #6 (bot-dominated early "
        "activity) ranks above #8 and below #5 and publishes a direction but no pp "
        "figure, so it sits at the midpoint of the 6-24 interval the two evidenced "
        "numbers define. "
        "insider_pct=12 and cluster_pct=12 are RANKED by family, magnitude INVENTED — "
        "both group wallets, so they are weaker proxies for #5 than a proved bundle and "
        "stronger than #8's ungrouped top-10; the grouping is provider-asserted rather "
        "than proved from slot adjacency, which is why they do not get 24. cluster_pct "
        "does NOT inherit §1 #4's rank: #4 is a 3-hop funding graph over creators in "
        "intelligence/creators.py, and this field is RugCheck's insider network share. "
        "dev_pct=6 is EVIDENCED-DOWNWARD — §4 #16 measures a 98.7% base rate for 'the dev "
        "bought his own bundle', so dev-attributable supply is near-universal and carries "
        "little information; it sits at the evidenced floor despite being a mandate "
        "blocker (PLAN §5.5). "
        "buy_tax_bps=6 and sell_tax_bps=6 are MANDATE membership, magnitude INVENTED — "
        "EDGE §1 does not rank taxes at all because they do not predict graduation; they "
        "are in the rubric because they are blocker properties (tax >=50% and "
        "owner-modifiable tax both reject in dyor.RULES) that we cannot check, and an "
        "unchecked blocker is the thing this module is about. Evidenced floor. "
        "holder_count=2 is INVENTED and deliberately below the floor — EDGE §1 does not "
        "rank it, no rule in dyor.py fires on it, and it is the contrast case: a cheap "
        "unknown must never be able to look like an expensive one.",
    ),
    "FAMILIES": Provenance(
        FAMILIES, "property name -> group",
        "the operator's four named families, plus a home for the unranked field",
        "DEFINITIONAL: grouping only, it invents no number. 'holders' exists so "
        "holder_count can carry weight without being required for the top grade.",
    ),
    "REQUIRED_FAMILIES": Provenance(
        REQUIRED_FAMILIES, "family names",
        "operator requirement: concentration, bundling, sniping, taxes",
        "DEFINITIONAL, not a threshold. Every field of each family must be established; "
        "no fractional credit, because partial credit inside a family is exactly how "
        "dyor._components' insider_supply block awarded 14 points for knowing dev_pct.",
    ),
    "TOTAL_WEIGHT": Provenance(
        TOTAL_WEIGHT, "separation points", "sum(FIELD_WEIGHTS.values())",
        "DERIVED. Only a denominator for the reported ratio; nothing gates on it.",
    ),
    "PARTIAL_MIN_WEIGHT": Provenance(
        PARTIAL_MIN_WEIGHT, "separation points", "max(FIELD_WEIGHTS.values())",
        "DERIVED from the rubric rather than chosen: a scan counts as examined only if it "
        "established at least as much separation weight as the strongest single published "
        "variable (bundle-adjusted concentration, 24pp). Computed, so it cannot be tuned "
        "without changing the evidence it is derived from. It was NOT adjusted after "
        "seeing what it does to the stored distribution.",
    ),
    "TIER_MAX_GRADE": Provenance(
        TIER_MAX_GRADE, "tier -> ceiling grade", "this module's definition of the grades",
        "DEFINITIONAL, and it is what the letters now mean. "
        "A = clean and the predictive surface established. B = clean and some of it. "
        "C = clean as far as we looked, and we did not look far. The ceiling only ever "
        "lowers a grade and never touches the score.",
    ),
    "_GRADE_RANK": Provenance(
        _GRADE_RANK, "grade -> rank", "local copy of the A>B>C>D ordering",
        "DEFINITIONAL. QUARANTINED and UNSCORED are absent on purpose: neither asserts "
        "cleanliness, so "
        "neither may be capped. Not imported from execution.lanes — intelligence must not "
        "depend on execution.",
    ),
}


# --------------------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageReport:
    """What we managed to establish, kept separate from what we found.

    A caller can tell "clean and well-covered" (``tier is FULL``) from "clean but barely
    examined" (``tier is BLIND``) from "dirty" (which is the grade and the blocker list,
    not this object) without any of the three collapsing into the others.
    """

    tier: CoverageTier
    established_weight: int
    total_weight: int
    max_grade: Grade
    established: tuple[str, ...]
    unestablished: tuple[str, ...]
    #: Required families with at least one field missing. Empty means the top grade is
    #: reachable as far as coverage is concerned.
    unestablished_families: tuple[str, ...]

    @property
    def ratio(self) -> float:
        """Established weight as a fraction of the rubric. Reporting only; nothing gates."""
        return self.established_weight / self.total_weight if self.total_weight else 0.0

    def render(self) -> str:
        """One line, short enough to survive a 300-character ``Receipt.note``."""
        families = ",".join(self.unestablished_families) or "none"
        return (
            f"coverage={self.tier.value} {self.established_weight}/{self.total_weight} "
            f"({self.ratio * 100:.0f}%) ceiling={self.max_grade.value} "
            f"unestablished_families={families} "
            f"established={','.join(self.established) or 'none'}"
        )


def assess(established: Iterable[str]) -> CoverageReport:
    """Grade the *coverage* of a scan from the properties it managed to establish.

    Pure and allocation-light: this runs inside the 7.6 s tier-1 budget on every scan and
    must not add a provider call, a database read or anything else that can be slow.
    Property names outside :data:`FIELD_WEIGHTS` are ignored rather than rejected, so a
    caller can pass the whole resolved property set.
    """
    have = {name for name in established if name in FIELD_WEIGHTS}
    weight = sum(FIELD_WEIGHTS[name] for name in have)
    missing_families = tuple(
        family for family in REQUIRED_FAMILIES if not set(FAMILIES[family]) <= have
    )

    if not missing_families:
        tier = CoverageTier.FULL
    elif weight >= PARTIAL_MIN_WEIGHT:
        tier = CoverageTier.PARTIAL
    else:
        tier = CoverageTier.BLIND

    order = sorted(FIELD_WEIGHTS, key=lambda name: (-FIELD_WEIGHTS[name], name))
    return CoverageReport(
        tier=tier,
        established_weight=weight,
        total_weight=TOTAL_WEIGHT,
        max_grade=TIER_MAX_GRADE[tier],
        established=tuple(name for name in order if name in have),
        unestablished=tuple(name for name in order if name not in have),
        unestablished_families=missing_families,
    )


def assess_unknowns(unknowns: Iterable[str]) -> CoverageReport:
    """Recover the coverage of a *stored* dossier from its ``unknowns`` list alone.

    ``token_dossiers.unknowns_json`` is written by ``dyor.store_dossier`` and every rubric
    field is in ``dyor.TRACKED_PROPERTIES``, so this reproduces exactly what the scan
    computed — no provider call, no migration, no new column. It is the reason a caller
    that only ever sees the database can still tell a well-covered dossier from a blind one.
    """
    absent = set(unknowns)
    return assess(name for name in FIELD_WEIGHTS if name not in absent)


def cap(grade: Grade, report: CoverageReport) -> Grade:
    """Apply the coverage ceiling. Only ever lowers a grade.

    ``QUARANTINED`` and ``UNSCORED`` pass through untouched: a blocker is a finding about
    the token and ``UNSCORED`` is already a statement that we have no opinion, so neither
    is a claim of cleanliness that coverage could be overstating.
    """
    rank = _GRADE_RANK.get(grade)
    if rank is None:
        return grade
    ceiling = report.max_grade
    return ceiling if rank > _GRADE_RANK[ceiling] else grade


__all__ = [
    "FAMILIES",
    "FIELD_WEIGHTS",
    "PARTIAL_MIN_WEIGHT",
    "PROVENANCE",
    "REQUIRED_FAMILIES",
    "TIER_MAX_GRADE",
    "TOTAL_WEIGHT",
    "CoverageReport",
    "CoverageTier",
    "Provenance",
    "assess",
    "assess_unknowns",
    "cap",
]
