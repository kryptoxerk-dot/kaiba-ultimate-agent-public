"""Evidence weight is measured against what the CHAIN can answer, not a Solana rubric.

MEASURED 2026-09-22. Making the safety-coverage rules chain-aware removed a false
``unknown_safety`` warning and lifted robinhood's average dossier score from 35.5 to 45.4
(sol control unchanged, 63.5 to 64.8). It was not enough to produce a single grade B,
because the same Solana assumption is baked into the scoring arithmetic a second time.

``COMPONENT_MAX`` sums to exactly 100, of which::

    mint_authority    14
    freeze_authority  12

are **Solana account authorities**, unreachable on any EVM chain. So an EVM token's
ceiling is 74, and::

    confidence = CONFIDENCE_FLOOR + (1 - CONFIDENCE_FLOOR) * evidence_weight / 100

discounts a PERFECT robinhood dossier to 0.35 + 0.65 * 0.74 = **0.831** of its normalised
score. Worse, ``A_MIN_EVIDENCE_WEIGHT`` is 80 and an EVM chain tops out at 74, so
**robinhood could never be graded A at all** -- not for any token, however clean.

THE RULE. Normalise against the chain's own ceiling: "how much of what this chain CAN tell
us did we establish". A Solana dossier is unchanged, because there the ceiling IS 100.

WHAT THIS IS NOT. It is not a lower bar for EVM. The same fraction of the achievable
rubric is still required; the thresholds scale with the ceiling rather than sitting above
it. An EVM token that establishes nothing is still UNSCORED, and a blocker still
quarantines regardless.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import EVM_CHAINS, Chain
from kaiba.intelligence import dyor


# ------------------------------------------------------------------ the ceiling


def test_the_solana_ceiling_is_the_whole_rubric():
    assert dyor.achievable_evidence_weight(Chain.SOL) == pytest.approx(100.0)
    assert sum(dyor.COMPONENT_MAX.values()) == pytest.approx(100.0)


@pytest.mark.parametrize("chain", sorted(EVM_CHAINS, key=lambda c: c.value))
def test_every_evm_ceiling_excludes_the_solana_authorities(chain):
    got = dyor.achievable_evidence_weight(chain)
    expected = 100.0 - dyor.COMPONENT_MAX["mint_authority"] - dyor.COMPONENT_MAX["freeze_authority"]
    assert got == pytest.approx(expected) == pytest.approx(74.0)


def test_an_unknown_chain_keeps_the_full_ceiling():
    """Unknown means we cannot assert a component is unreachable. Keep the strict bar."""
    assert dyor.achievable_evidence_weight(None) == pytest.approx(100.0)


# ------------------------------------------------------------------ confidence


def test_a_perfect_evm_dossier_is_no_longer_discounted():
    """THE REGRESSION: 0.831 on a dossier that answered every question its chain has."""
    full_evm = dyor.achievable_evidence_weight(Chain.ROBINHOOD)
    assert dyor.confidence_for(full_evm, Chain.ROBINHOOD) == pytest.approx(1.0)


def test_a_perfect_solana_dossier_is_still_undiscounted():
    assert dyor.confidence_for(100.0, Chain.SOL) == pytest.approx(1.0)


def test_a_half_measured_evm_dossier_is_still_discounted():
    """Scaling the ceiling must not remove the discount, only correct its denominator."""
    half = dyor.achievable_evidence_weight(Chain.ROBINHOOD) / 2
    got = dyor.confidence_for(half, Chain.ROBINHOOD)
    assert float(dyor.CONFIDENCE_FLOOR) < got < 1.0


def test_the_same_fraction_of_the_rubric_scores_the_same_on_either_chain():
    """This is the whole claim: equal effort, equal confidence."""
    evm = dyor.confidence_for(dyor.achievable_evidence_weight(Chain.ROBINHOOD) * 0.6,
                              Chain.ROBINHOOD)
    sol = dyor.confidence_for(100.0 * 0.6, Chain.SOL)
    assert evm == pytest.approx(sol)


# ------------------------------------------------------------------ the thresholds


def test_grade_a_is_reachable_on_an_evm_chain():
    """A_MIN_EVIDENCE_WEIGHT was 80 against an EVM ceiling of 74: A was impossible."""
    ceiling = dyor.achievable_evidence_weight(Chain.ROBINHOOD)
    assert dyor.min_evidence_for(dyor.A_MIN_EVIDENCE_WEIGHT, Chain.ROBINHOOD) <= ceiling


def test_the_a_bar_is_the_same_FRACTION_on_both_chains():
    evm = dyor.min_evidence_for(dyor.A_MIN_EVIDENCE_WEIGHT, Chain.ROBINHOOD)
    sol = dyor.min_evidence_for(dyor.A_MIN_EVIDENCE_WEIGHT, Chain.SOL)
    assert evm / dyor.achievable_evidence_weight(Chain.ROBINHOOD) == pytest.approx(sol / 100.0)


def test_the_unscored_floor_scales_too():
    evm = dyor.min_evidence_for(dyor.MIN_EVIDENCE_WEIGHT, Chain.ROBINHOOD)
    assert evm < dyor.MIN_EVIDENCE_WEIGHT
    assert evm / 74.0 == pytest.approx(dyor.MIN_EVIDENCE_WEIGHT / 100.0)


def test_solana_thresholds_are_untouched():
    assert dyor.min_evidence_for(dyor.A_MIN_EVIDENCE_WEIGHT, Chain.SOL) == dyor.A_MIN_EVIDENCE_WEIGHT
    assert dyor.min_evidence_for(dyor.MIN_EVIDENCE_WEIGHT, Chain.SOL) == dyor.MIN_EVIDENCE_WEIGHT


# ------------------------------------------------------------------ end to end


def _verdict(chain, **values):
    r = dyor.Resolution(values=dict(values), chain=chain)
    return dyor.score_dossier(r, dyor.evaluate(r))


CLEAN_EVM = dict(
    can_sell=True,
    lp_burned_pct=dyor.Decimal(100),
    top10_pct=dyor.Decimal(5),
    dev_pct=dyor.Decimal(1),
    buy_tax_bps=dyor.Decimal(0),
    sell_tax_bps=dyor.Decimal(0),
    liquidity_usd=dyor.Decimal(50_000),
)


def test_a_clean_robinhood_token_can_now_reach_b_or_better():
    """It graded C at 42-49 before, on a token with nothing wrong with it."""
    verdict = _verdict(Chain.ROBINHOOD, **CLEAN_EVM)
    assert verdict.grade in {dyor.Grade.A, dyor.Grade.B}, (verdict.grade, verdict.score,
                                                           verdict.evidence_weight, verdict.notes)


def test_an_evm_token_with_nothing_established_is_still_unscored_or_quarantined():
    """Scaling the ceiling is not a way in for a token nobody could check."""
    verdict = _verdict(Chain.ROBINHOOD)
    assert verdict.grade in {dyor.Grade.UNSCORED, dyor.Grade.QUARANTINED}


def test_an_evm_token_that_cannot_be_sold_is_still_quarantined():
    verdict = _verdict(Chain.ROBINHOOD, **{**CLEAN_EVM, "can_sell": False})
    assert verdict.grade is dyor.Grade.QUARANTINED


def test_a_solana_verdict_is_bit_for_bit_unchanged():
    """The control. Any drift here means this touched more than the EVM ceiling."""
    values = dict(CLEAN_EVM, mint_authority_revoked=True, freeze_authority_revoked=True)
    with_chain = _verdict(Chain.SOL, **values)
    r = dyor.Resolution(values=dict(values))          # chain=None, the pre-change path
    without = dyor.score_dossier(r, dyor.evaluate(r))
    assert with_chain.score == without.score
    assert with_chain.grade == without.grade
    assert with_chain.evidence_weight == without.evidence_weight


def test_the_evidence_weight_cap_no_longer_binds_on_an_evm_chain():
    """The A path is what proves the chain reaches `_grade_from_score`.

    B is reachable whether or not the threshold is scaled, so only the A cap distinguishes
    them: unscaled, ``A_MIN_EVIDENCE_WEIGHT`` (80) sits above the EVM ceiling (74) and
    every EVM token is capped with "capped to B: evidence_weight 74 < 80" -- a cap that is
    unreachable by construction, not a judgement about the token.

    A flawless robinhood token is STILL capped to B here, by a different and correct
    mechanism: ``coverage`` reports ``unestablished_families=(concentration, bundling,
    sniping)``, which robinhood genuinely cannot establish. That cap is about the token's
    predictive surface, not about a Solana-shaped ruler, so it stays.
    """
    verdict = _verdict(Chain.ROBINHOOD, **CLEAN_EVM)
    assert verdict.evidence_weight == pytest.approx(74.0), verdict.evidence_weight
    assert verdict.score > float(dyor.B_MIN_SCORE), verdict.score
    assert not any("capped to B: evidence_weight" in n for n in verdict.notes), verdict.notes
    assert not any("UNSCORED" in n for n in verdict.notes), verdict.notes
