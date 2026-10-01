"""The Flap curve's safety refusals, each of which decides a real position size.

WHY THIS FILE EXISTS. The adversarial mutation pass over the Flap depth reader on
2026-09-22 reported seven surviving mutants, and four of them were load-bearing safety
gates with no test pinning them at all. They were left unpinned only because that work was
told not to touch `tests/` -- a rule meant to protect a recovered specification, not to
stop new coverage being written. So this file exists to close that gap.

Each gate here refuses a fill. Getting a refusal wrong does not produce an error; it
produces a SIZE, computed from numbers that do not mean what the arithmetic assumes. The
module's own docstring names the precedent in this tree: two algebraically identical
spellings of the Pons curve differed by one atom on 40 of 40 replayed fills, and only the
chain could say which was right. The Flap fill spelling is still UNVERIFIED against a live
fill, which makes its preconditions the part that has to hold.
"""

from __future__ import annotations

import pytest

from kaiba.execution.evm_price import FLAP_CURVE_DECIMALS, FLAP_WAD, FlapCurve

#: A curve whose every precondition is satisfied. Each test below breaks exactly one.
GOOD = dict(
    r_scaled=30 * FLAP_WAD,
    h_scaled=10**9 * FLAP_WAD,
    k_scaled=10**12 * FLAP_WAD,
    total_supply_atoms=10**9 * 10**18,
    tokens_sold_atoms=10**8 * 10**18,
    graduation_tokens_atoms=8 * 10**26,   # word 8's documented rule
    quote_reserve_base=5 * 10**18,
    token_decimals=FLAP_CURVE_DECIMALS,
    quote_decimals=FLAP_CURVE_DECIMALS,
)


def curve(**over) -> FlapCurve:
    return FlapCurve(**{**GOOD, **over})


def test_the_baseline_curve_is_usable():
    """Without this the whole file could pass by refusing everything."""
    assert curve().refusal is None


# ------------------------------------------------------------------ the decimals gate


@pytest.mark.parametrize(
    ("token_decimals", "quote_decimals"),
    [(6, 18), (18, 6), (8, 8), (0, 18), (18, 0)],
)
def test_a_non_wad_pairing_refuses_rather_than_mis_scaling(token_decimals, quote_decimals):
    """Every quantity on this curve is scaled by 1e18. A 6-decimal side is not a small
    number, it is a number in different units, and multiplying it through the same
    arithmetic silently mis-scales the fill by twelve orders of magnitude.

    Refusing is the only safe answer, because nothing downstream can tell a wrongly scaled
    size from a right one.
    """
    got = curve(token_decimals=token_decimals, quote_decimals=quote_decimals).refusal
    assert got is not None, "a non-WAD pairing must never be priced"
    assert got.startswith("flap_decimals_not_wad:"), got
    assert str(token_decimals) in got and str(quote_decimals) in got, got


# ------------------------------------------------------------------ the graduation gate


def test_a_zero_graduation_point_refuses():
    """Word 8 is the graduation rule. A zero there leaves the fill unbounded."""
    got = curve(graduation_tokens_atoms=0).refusal
    assert got is not None and got.startswith("flap_graduation_point_unusable:"), got


def test_a_graduation_point_already_behind_us_refuses():
    """A curve that graduates mid-order does not fill the size that was asked for."""
    got = curve(tokens_sold_atoms=9 * 10**26, graduation_tokens_atoms=8 * 10**26).refusal
    assert got is not None and got.startswith("flap_graduation_point_unusable:"), got


def test_a_graduation_point_beyond_the_supply_refuses():
    got = curve(graduation_tokens_atoms=10**40).refusal
    assert got is not None and got.startswith("flap_graduation_point_unusable:"), got


# ------------------------------------------------------------------ the reserve gate


def test_an_uncorroborated_reserve_refuses():
    """A fill is priced against the reserve; ``None`` means the cross-check rejected it.

    The mutation report called removing this 'not a real hole' because a downstream None
    would refuse anyway. That is true today and is exactly why it is worth pinning: the
    refusal currently happens in one named place, and a later change that gave the
    downstream a default would turn this into a silent guess.
    """
    got = curve(quote_reserve_base=None).refusal
    assert got == "flap_reserve_uncorroborated", got


def test_a_negative_reserve_refuses():
    assert curve(quote_reserve_base=-1).refusal == "flap_reserve_uncorroborated"


# ------------------------------------------------------------------ the constants gate


@pytest.mark.parametrize("field", ["r_scaled", "h_scaled", "k_scaled"])
def test_a_missing_curve_constant_refuses(field):
    """(x+h)(y+r)=K is not a curve with a zero in it."""
    assert curve(**{field: 0}).refusal == "flap_curve_constants_missing"


# ------------------------------------------------------------------ supply sanity


@pytest.mark.parametrize(
    "over",
    [
        {"total_supply_atoms": 0},
        {"tokens_sold_atoms": -1},
        {"tokens_sold_atoms": 10**9 * 10**18 + 1},
    ],
)
def test_supply_out_of_range_refuses(over):
    got = curve(**over).refusal
    assert got is not None, over
    assert got in {"flap_tokens_sold_out_of_range", "flap_curve_constants_missing"} or \
        got.startswith("flap_graduation_point_unusable:"), got


# ------------------------------------------------------------------ the refusal is honoured


def test_a_refusing_curve_prices_nothing():
    """`refusal` exists so `tokens_out` and `max_size_base_units` cannot disagree.

    A gate that reports a refusal while the arithmetic answers anyway is worse than no
    gate, because the refusal reads as though it were enforced.
    """
    bad = curve(token_decimals=6)
    assert bad.refusal is not None
    assert bad.tokens_out(10**16) is None, "a refusing curve must not quote a fill"
    assert bad.max_size_base_units is None, "a refusing curve must not offer a max size"
