"""A chain is not penalised for lacking evidence its chain cannot produce.

MEASURED 2026-09-22. Robinhood (an EVM chain) built 63 dossiers in three hours and graded
34 C, 24 D, 5 QUARANTINED -- **zero B** -- so no robinhood candidate could ever reach a
lane that requires grade B. Sol produced 672 B's over the same kind of window.

The cause is not a missing provider. It is that ``CRITICAL_PROPERTIES`` -- the set the
safety-coverage rules count against -- is::

    ("can_sell", "mint_authority_revoked", "freeze_authority_revoked")

and the last two are **Solana account authorities**. They do not exist on an EVM chain.
``dyor._GMGN_SOL_ONLY_BOOLS`` already established this and drops them on EVM, because GMGN
sends them hardwired to ``false`` there and piping them through produced false blockers on
PEPE and SHIB -- neither of which has a mint function at all. That decision was right. The
scoring never got the memo, so on robinhood:

* ``security_coverage`` reads 1 of 3 (only ``can_sell`` is knowable),
* ``partial_security_coverage`` fires, raising ``UNKNOWN_SAFETY``,
* which costs a flat **15 points** from ``NON_COMPONENT_PENALTY``.

A measured robinhood dossier scored 42.19 with warnings ``['unknown_safety',
'lp_not_burned', 'low_liquidity']`` while a sol grade-B scored 78.0. The token was charged
for not answering a question its chain does not ask.

WHAT THIS DOES NOT DO. It does not weaken any EVM safety check. ``can_sell`` is still
required and still the blocker when nothing establishes it; honeypot, buy/sell tax,
LP burn, top-10 and dev share are all unchanged, and all of them are read on robinhood
today. What changes is only that an inapplicable property stops counting as an unanswered
one. On Solana nothing changes at all.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import EVM_CHAINS, Chain
from kaiba.intelligence import dyor


def a_resolution(chain: Chain, **values):
    return dyor.Resolution(values=dict(values), chain=chain)


# ------------------------------------------------------------------ the applicable set


def test_solana_keeps_all_three_critical_properties():
    got = dyor.critical_properties_for(Chain.SOL)
    assert set(got) == {"can_sell", "mint_authority_revoked", "freeze_authority_revoked"}


@pytest.mark.parametrize("chain", sorted(EVM_CHAINS, key=lambda c: c.value))
def test_every_evm_chain_drops_the_solana_authorities(chain):
    got = dyor.critical_properties_for(chain)
    assert "mint_authority_revoked" not in got
    assert "freeze_authority_revoked" not in got


@pytest.mark.parametrize("chain", sorted(EVM_CHAINS, key=lambda c: c.value))
def test_can_sell_is_still_required_on_every_evm_chain(chain):
    """The one safety property that DOES exist on EVM must not be dropped with them."""
    assert "can_sell" in dyor.critical_properties_for(chain)


def test_an_unknown_chain_keeps_the_strict_set():
    """Unknown means we cannot say the property is inapplicable. Fail toward refusing."""
    got = dyor.critical_properties_for(None)
    assert set(got) == {"can_sell", "mint_authority_revoked", "freeze_authority_revoked"}


# ------------------------------------------------------------------ coverage arithmetic


def test_robinhood_with_can_sell_known_is_fully_covered():
    """THE REGRESSION: this read 1/3 and cost 15 points."""
    r = a_resolution(Chain.ROBINHOOD, can_sell=True)
    assert r.security_coverage == len(r.critical_properties) == 1


def test_solana_with_only_can_sell_is_still_partial():
    """Nothing changes on Solana; there the other two are real questions."""
    r = a_resolution(Chain.SOL, can_sell=True)
    assert r.security_coverage == 1 < len(r.critical_properties)


def test_robinhood_with_nothing_known_is_still_zero():
    """Zero coverage stays a BLOCKER on EVM. This is not a way to skip the check."""
    r = a_resolution(Chain.ROBINHOOD)
    assert r.security_coverage == 0


# ------------------------------------------------------------------ the rules


def _details(resolution) -> list[str]:
    """`Finding` carries risk/severity/detail, not a rule name."""
    return [f.detail for f in dyor.evaluate(resolution)]


def _security_findings(resolution):
    return [f for f in dyor.evaluate(resolution)
            if f.detail.startswith("security_coverage=")]


def test_partial_security_coverage_no_longer_fires_on_robinhood():
    assert _security_findings(a_resolution(Chain.ROBINHOOD, can_sell=True)) == []


def test_partial_security_coverage_still_fires_on_solana():
    found = _security_findings(a_resolution(Chain.SOL, can_sell=True))
    assert found and found[0].severity is dyor.Severity.WARNING
    assert "security_coverage=1/3" in found[0].detail


def test_no_security_coverage_still_blocks_on_robinhood():
    """An EVM token nobody could establish sellability for is still a blocker."""
    found = _security_findings(a_resolution(Chain.ROBINHOOD))
    assert found and found[0].severity is dyor.Severity.BLOCKER
    assert "security_coverage=0/1" in found[0].detail


def test_a_robinhood_token_that_cannot_be_sold_is_still_caught():
    """The check that matters on EVM is untouched: this must still BLOCK."""
    findings = dyor.evaluate(a_resolution(Chain.ROBINHOOD, can_sell=False))
    assert any(f.severity is dyor.Severity.BLOCKER for f in findings), _details(
        a_resolution(Chain.ROBINHOOD, can_sell=False))


# ------------------------------------------------------------------ the score


def test_the_fifteen_point_penalty_is_gone_for_robinhood():
    """42.19 vs a sol B at 78.0 was the measured gap; 15 of it was this penalty."""
    clean = dict(can_sell=True, lp_burned_pct=dyor.Decimal(100), top10_pct=dyor.Decimal(10),
                 dev_pct=dyor.Decimal(2))
    rh = dyor.Resolution(values=dict(clean), chain=Chain.ROBINHOOD)
    sol = dyor.Resolution(values=dict(clean), chain=Chain.SOL)
    rh_score = dyor.score_dossier(rh, dyor.evaluate(rh)).score
    sol_score = dyor.score_dossier(sol, dyor.evaluate(sol)).score
    assert rh_score > sol_score, (rh_score, sol_score)


def test_solana_scoring_is_unchanged_by_this(tmp_path):
    """A Solana dossier must score exactly as it did before the chain became visible."""
    values = dict(can_sell=True, mint_authority_revoked=True, freeze_authority_revoked=True,
                  lp_burned_pct=dyor.Decimal(100), top10_pct=dyor.Decimal(10))
    with_chain = dyor.Resolution(values=dict(values), chain=Chain.SOL)
    without = dyor.Resolution(values=dict(values))
    assert with_chain.security_coverage == without.security_coverage
    a = dyor.score_dossier(with_chain, dyor.evaluate(with_chain)).score
    b = dyor.score_dossier(without, dyor.evaluate(without)).score
    assert a == b


# ------------------------------------------------------------------ the wiring


def test_the_production_build_passes_the_chain():
    """Without this the fix is a silent no-op.

    `resolve()` defaults `chain=None`, which keeps the strict Solana set. If the dossier
    builder ever stops passing the chain, every EVM token goes back to being charged 15
    points for two Solana-only questions and nothing anywhere would fail. The same shape
    of miss made an earlier exit fix a no-op for a full deploy cycle, because the provider
    allow-list silently refused the call it depended on.
    """
    import inspect

    # The scan path is what builds a live dossier; `build_dossier` is handed an
    # already-resolved view, so the chain has to arrive one level up.
    source = inspect.getsource(dyor)
    assert "resolve(claims, chain=" in source, "the scan path dropped the chain"
    fn = next(
        (f for name, f in vars(dyor).items()
         if callable(f) and getattr(f, "__module__", "") == dyor.__name__
         and "resolve(claims, chain=" in (inspect.getsource(f) if inspect.isfunction(f) else "")),
        None,
    )
    assert fn is not None, "no function in dyor passes the chain to resolve()"


def test_resolve_defaults_to_the_strict_set():
    """The default must be the SAFE one, so a caller that forgets loses nothing."""
    r = dyor.resolve([])
    assert r.chain is None
    assert set(r.critical_properties) == set(dyor.CRITICAL_PROPERTIES)


def test_coverage_counts_over_the_applicable_set_not_the_strict_one():
    """The numerator and the denominator must be the same set.

    Counting the numerator over all three while the denominator is one happens to give
    the same answer today, because a Solana authority is never known on an EVM chain. It
    stops being the same answer the moment a provider supplies one -- GMGN already sends
    both fields on EVM hardwired to ``false``, and one mapping change is all it takes for
    them to reach ``values``. Then coverage reads 3 of 1 and the ratio is nonsense.
    """
    r = dyor.Resolution(
        values={"can_sell": True, "mint_authority_revoked": True,
                "freeze_authority_revoked": True},
        chain=Chain.ROBINHOOD,
    )
    assert r.security_coverage == 1, "the Solana authorities were counted on an EVM chain"
    assert r.security_coverage <= len(r.critical_properties)
