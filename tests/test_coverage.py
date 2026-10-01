"""Coverage: how much of the predictive surface a scan established.

The tests that are load-bearing rather than decorative:

* ``test_every_constant_has_provenance`` — a weight added without a row saying where it
  came from fails the build, the way ``test_viability.py`` and ``test_bundles.py`` do it.
  This module exists because ``dyor.A_MIN_EVIDENCE_WEIGHT`` was exactly such a number.
* ``test_bundling_outweighs_holder_count_by_the_published_ratio`` — the weights carry the
  EDGE §1 ranking, and the two evidenced figures (24pp and 6pp) are in the table as
  themselves rather than as something proportional to them.
* ``test_the_top_grade_needs_all_four_families`` — one missing family is enough to stop an
  A, whichever one it is.
* ``test_every_rubric_field_is_tracked_on_the_dossier`` — the reason
  :func:`coverage.assess_unknowns` can recover the tier from a stored row with no new
  column and no migration.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kaiba.core.schemas import Grade
from kaiba.intelligence import coverage, dyor
from kaiba.intelligence.coverage import (
    FAMILIES,
    FIELD_WEIGHTS,
    PARTIAL_MIN_WEIGHT,
    PROVENANCE,
    REQUIRED_FAMILIES,
    TIER_MAX_GRADE,
    TOTAL_WEIGHT,
    CoverageTier,
)

#: Everything in the rubric, which is what ``assess`` is handed on a fully covered scan.
ALL_FIELDS = tuple(FIELD_WEIGHTS)


# ------------------------------------------------------------------ provenance


def _module_constants() -> set[str]:
    tree = ast.parse(Path(coverage.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
        for target in targets:
            if target.id.isupper():
                names.add(target.id)
    return names


def test_every_constant_has_provenance():
    """Fails on a silently-added knob.

    A weight that appears in this module without a row saying which published figure it
    came from is the exact failure the module exists to prevent: an unmeasured constant
    that quietly decides whether a token is tradeable.
    """
    assert _module_constants() == set(PROVENANCE) | {"PROVENANCE"}


def test_provenance_rows_are_filled_in():
    for name, row in PROVENANCE.items():
        assert row.value == getattr(coverage, name), f"{name}: provenance value is stale"
        assert row.unit and row.source and row.note


def test_every_provenance_row_classifies_itself():
    """Every entry must say whether its number is evidenced, derived or invented."""
    words = ("EVIDENCED", "INVENTED", "DERIVED", "DEFINITIONAL", "STRUCTURAL", "MANDATE", "RANKED")
    for name, row in PROVENANCE.items():
        assert any(w in row.note for w in words), f"{name} does not classify its own provenance"


def test_the_weights_cite_the_two_published_figures():
    """EDGE §1 supplies exactly two absolute separations, and both are in the table as-is.

    24pp for bundle-adjusted holder concentration (§1 #5) and 6pp for naive top-10 (§1 #8).
    If either stops matching, the weights have drifted off their evidence.
    """
    assert FIELD_WEIGHTS["bundler_pct"] == 24
    assert FIELD_WEIGHTS["top10_pct"] == 6
    note = PROVENANCE["FIELD_WEIGHTS"].note
    assert "24pp" in note and "6pp" in note
    assert "EDGE-AND-VARIABLES.md §1" in PROVENANCE["FIELD_WEIGHTS"].source


def test_no_weight_sits_outside_the_evidenced_interval():
    """Only ``holder_count`` may fall below the evidenced floor, and nothing above 24.

    The interpolated weights are placed inside the 6-24 interval the two published figures
    define. A new field above 24 would be claiming stronger evidence than any that exists.
    """
    for name, weight in FIELD_WEIGHTS.items():
        assert weight <= FIELD_WEIGHTS["bundler_pct"], f"{name} claims more than the top evidence"
        if name != "holder_count":
            assert weight >= FIELD_WEIGHTS["top10_pct"], f"{name} is below the evidenced floor"


def test_the_partial_threshold_is_derived_from_the_rubric_not_chosen():
    """It is ``max(FIELD_WEIGHTS)``, so it cannot be tuned independently of the evidence."""
    assert PARTIAL_MIN_WEIGHT == max(FIELD_WEIGHTS.values()) == FIELD_WEIGHTS["bundler_pct"] == 24
    assert "DERIVED" in PROVENANCE["PARTIAL_MIN_WEIGHT"].note


# ------------------------------------------------------------------ the rubric


def test_bundling_outweighs_holder_count_by_the_published_ratio():
    """The requirement in one line: ``bundler_pct`` unknown is not ``holder_count`` unknown."""
    lost_bundling = coverage.assess_unknowns(["bundler_pct"])
    lost_holder_count = coverage.assess_unknowns(["holder_count"])

    assert lost_bundling.established_weight < lost_holder_count.established_weight
    assert lost_bundling.tier is CoverageTier.PARTIAL
    assert lost_holder_count.tier is CoverageTier.FULL
    assert FIELD_WEIGHTS["bundler_pct"] == 12 * FIELD_WEIGHTS["holder_count"]


def test_every_rubric_field_is_tracked_on_the_dossier():
    """What makes ``assess_unknowns`` exact rather than approximate.

    ``token_dossiers.unknowns_json`` is built from ``dyor.TRACKED_PROPERTIES``. A rubric
    field outside that list would be silently treated as established on every stored row,
    which is the same fail-open the whole module is about.
    """
    assert set(FIELD_WEIGHTS) <= set(dyor.TRACKED_PROPERTIES)


def test_the_families_partition_the_rubric():
    """No field counted twice, no field with no home."""
    members = [field for group in FAMILIES.values() for field in group]
    assert sorted(members) == sorted(FIELD_WEIGHTS)
    assert len(members) == len(set(members))
    assert set(REQUIRED_FAMILIES) < set(FAMILIES)
    assert TOTAL_WEIGHT == sum(FIELD_WEIGHTS.values()) == 86


def test_the_free_safety_booleans_are_not_counted_as_coverage():
    """EDGE §4 #13, on freeze authority: 'it should not be counted as coverage'.

    It is 0.6% of Solana rugs and it is free. Counting cheap near-constants is how the
    score's own ``evidence_weight`` reached 80/100 on a dossier that knew nothing about
    concentration, and repeating it here would rebuild the bug in the new module.
    """
    for name in dyor.CRITICAL_PROPERTIES:
        assert name not in FIELD_WEIGHTS


# ------------------------------------------------------------------ the assessment


def test_full_coverage_when_everything_is_established():
    report = coverage.assess(ALL_FIELDS)
    assert report.tier is CoverageTier.FULL
    assert report.established_weight == TOTAL_WEIGHT
    assert report.unestablished == ()
    assert report.unestablished_families == ()
    assert report.max_grade is Grade.A
    assert report.ratio == 1.0


@pytest.mark.parametrize("family", REQUIRED_FAMILIES)
def test_the_top_grade_needs_all_four_families(family):
    """Whichever one is missing, and however little of it is missing."""
    report = coverage.assess_unknowns([FAMILIES[family][0]])
    assert report.tier is not CoverageTier.FULL
    assert family in report.unestablished_families
    assert report.max_grade is not Grade.A
    assert coverage.cap(Grade.A, report) is not Grade.A


def test_a_family_half_read_is_not_established():
    """No fractional credit inside a family.

    This is the specific shape of the bug in ``dyor._components``: its ``insider_supply``
    block pays out all 14 points when any one of five supply splits is known, so knowing
    ``dev_pct`` bought credit for bundling and sniping.
    """
    report = coverage.assess(["top10_pct", "dev_pct", "bundler_pct", "sniper_pct",
                              "buy_tax_bps", "sell_tax_bps", "holder_count"])
    assert "concentration" in report.unestablished_families
    assert report.tier is CoverageTier.PARTIAL


def test_the_shape_615_of_615_stored_dossiers_had():
    """Dev and cluster answered, everything else dark. 18 of 86 points: BLIND."""
    report = coverage.assess(["dev_pct", "cluster_pct"])
    assert report.established_weight == 18
    assert report.tier is CoverageTier.BLIND
    assert report.max_grade is Grade.C
    assert report.unestablished_families == ("concentration", "bundling", "sniping", "taxes")


def test_blind_needs_less_than_the_strongest_single_variable():
    """Establishing bundling alone is enough to count as examined; it is worth 24."""
    assert coverage.assess(["bundler_pct"]).tier is CoverageTier.PARTIAL
    assert coverage.assess(["cluster_pct", "insider_pct"]).tier is CoverageTier.PARTIAL
    assert coverage.assess(["cluster_pct", "dev_pct"]).tier is CoverageTier.BLIND
    assert coverage.assess([]).tier is CoverageTier.BLIND


def test_unknown_property_names_are_ignored_not_counted():
    """Callers pass whole property sets; a name outside the rubric must not add weight."""
    report = coverage.assess(["dev_pct", "cluster_pct", "can_sell", "not_a_property"])
    assert report.established_weight == 18
    assert "can_sell" not in report.established


def test_assess_unknowns_is_the_inverse_of_assess():
    """A stored row recovers exactly what the scan computed, with no provider call."""
    for established in ([], ["bundler_pct"], ["dev_pct", "cluster_pct"], list(ALL_FIELDS)):
        unknown = [f for f in FIELD_WEIGHTS if f not in established]
        assert coverage.assess_unknowns(unknown) == coverage.assess(established)


# ------------------------------------------------------------------ the ceiling


@pytest.mark.parametrize(
    ("tier", "ceiling"),
    [(CoverageTier.FULL, Grade.A), (CoverageTier.PARTIAL, Grade.B), (CoverageTier.BLIND, Grade.C)],
)
def test_every_tier_has_a_declared_ceiling(tier, ceiling):
    assert TIER_MAX_GRADE[tier] is ceiling
    assert set(TIER_MAX_GRADE) == set(CoverageTier)


def test_the_ceiling_only_ever_lowers():
    blind = coverage.assess([])
    full = coverage.assess(ALL_FIELDS)

    assert coverage.cap(Grade.A, blind) is Grade.C
    assert coverage.cap(Grade.B, blind) is Grade.C
    assert coverage.cap(Grade.C, blind) is Grade.C
    assert coverage.cap(Grade.D, blind) is Grade.D  # already below the ceiling
    for grade in (Grade.A, Grade.B, Grade.C, Grade.D):
        assert coverage.cap(grade, full) is grade


def test_quarantined_and_unscored_pass_through_untouched():
    """Neither is a claim of cleanliness, so neither is something coverage can overstate."""
    blind = coverage.assess([])
    assert coverage.cap(Grade.QUARANTINED, blind) is Grade.QUARANTINED
    assert coverage.cap(Grade.UNSCORED, blind) is Grade.UNSCORED


def test_the_report_renders_both_facts_inside_a_receipt_note():
    """``Receipt.note`` truncates at 300 characters and this must survive it intact."""
    line = coverage.assess(["dev_pct", "cluster_pct"]).render()
    assert len(line) <= 300
    assert "blind" in line and "18/86" in line and "ceiling=C" in line
    assert "bundling" in line and "sniping" in line
