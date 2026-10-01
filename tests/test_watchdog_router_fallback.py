"""A Solana position the launch curve cannot describe must still get a price - or refuse.

THE LIVE BUG, 2026-09-22. Four of eight live positions were unpriceable and had therefore
no working stop; one of them (``FnkzzU3t55RQNbjc6Jynn7LHrPEbRTnBenvHQwJCebYB``) had already
tripped ``protection_blind_timeout`` and halted entries agent-wide.

MEASURED 2026-09-22, against the live world rather than any database, for that mint:

* ``https://frontend-api-v3.pump.fun/coins/Fnkzz...`` answers **200 in 0.36 s** and the
  coin is **on its curve** (``complete: false``). So the token is not graduated and the
  provider is not down.
* The payload says ``program: raydium_launchpad``, ``platform: bonk`` and
  ``quote_mint: Dz9mQ9NzkBcCsuGPFJ3r1bS4wgqKMHBPiVuniW8Mbonk`` with ``quote_decimals: 6``.
  It is a **letsbonk.fun launch quoted in BONK, not in SOL**.
* ``scanner.curve_from_payload`` on that exact payload returns
  ``(None, 'non_sol_quote:Dz9mQ9NzkBcC')``, and so does ``CurveState.from_payload``.
  **That refusal is correct**: those reserves are 6-decimal BONK units, and reading them
  as lamports would produce a confident wrong price on a stop.
* ``https://api.dexscreener.com/latest/dex/tokens/Fnkzz...`` answers ``pairs: null``.
  **Also correct**: a token on a curve has no pair.

So both layers of the Solana branch of ``price_source: venue`` refuse, both for good
reasons, and there was no third layer. The position goes blind and stays blind.

* ``lite-api.jup.ag`` DOES price it: ``SOL -> BONK (Scorch) -> token (Raydium Launchlab)``,
  **200 in 0.064-0.256 s**, and ``JupiterPriceSource`` turned that into a usable executable
  quote of **$0.000002346 at 342 bps impact in 570-611 ms** (``with_liquidity=False``).

Latency, MEASURED the same day on the shipped ``jupiter`` limiter budget
(``min_interval_ms: 1100``, ``capacity: 5``, ``refill_per_s: 1.0``, ``max_inflight: 2``),
four mints quoted concurrently the way ``Watchdog._prefetch_quotes`` quotes them, at
``Priority.EXIT``:

* ``with_liquidity=False``, ``wait_for_slot_s=2.0``: **2356 ms / 2362 ms wall, 4 of 4
  priced**, against the 5000 ms tick budget.
* ``with_liquidity=True``: 2552 ms / 2638 ms wall and **1 of 4 lost its price entirely** -
  the extra ``/price/v3`` round trip spent the slot budget the quote needed. That is why
  the last resort drops liquidity: ``PriceQuote`` says in its own docstring that the rug
  monitor may be unavailable and the stop may not, and these positions have no liquidity
  figure today anyway because they have no price at all.

End to end through the fixed ``venue`` source, MEASURED after this change:

* the incident mint prices in **1193 ms** at ``jupiter:Scorch+Raydium Launchlab``,
  ``$0.000002346``, 305 bps impact;
* four mints through the whole prefetch shape: **1396 ms / 1377 ms wall, 4 of 4 priced**,
  and **two of them never reached the router at all** - DexScreener answered first, which
  is the last-resort ordering doing its job;
* a mint nothing can price refuses with
  ``no curve: no_curve_snapshot; prices: not a valid sol address: ...; jupiter:
  bad_request: outputMint is not a solana mint``.

These tests pin the third layer and, much more importantly, pin that it is a LAST RESORT
that REFUSES. A price source that guessed here would be strictly worse than the blindness
it replaced: a wrong price on a stop sells at the wrong moment and calls it protection.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import watchdog as wd

#: The live mint from the incident. Used as a label; no test here touches the network.
BONK_QUOTED_MINT = "FnkzzU3t55RQNbjc6Jynn7LHrPEbRTnBenvHQwJCebYB"

#: The exact refusals MEASURED above, so the regression test reproduces the real shape.
CURVE_REFUSAL = "no curve: non_sol_quote:Dz9mQ9NzkBcC"
DEX_REFUSAL = "provider returned no price"


class _Source:
    """A ``PriceSource`` that answers, refuses, raises, or returns junk - on demand."""

    def __init__(self, name: str, *, quote: object = None, raises: bool = False) -> None:
        self.name = name
        self._quote = quote
        self._raises = raises
        self.calls: list[tuple[Chain, str]] = []

    def quote(self, chain: Chain, token: str) -> object:
        self.calls.append((chain, token))
        if self._raises:
            raise RuntimeError("provider exploded")
        return self._quote


def priced(name: str, price: str = "0.000002346") -> _Source:
    return _Source(
        name,
        quote=wd.PriceQuote(
            price_usd=Decimal(price),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            source=f"{name}:route",
        ),
    )


def refusing(name: str, note: str) -> _Source:
    return _Source(name, quote=wd.PriceQuote.unavailable(note, source=name))


# ============================================================ the refusal paths, first


def test_when_every_source_refuses_the_result_is_a_refusal_not_a_guess() -> None:
    """The whole point. No layer had a price, so there is no price. Blind, honestly."""
    src = wd.FallbackPriceSource(
        refusing("curve", CURVE_REFUSAL),
        refusing("prices", DEX_REFUSAL),
        refusing("jupiter", "no_route"),
    )
    got = src.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert not got.usable
    assert got.price_usd is None, "a refusal must not carry a number a stop could read"
    assert got.basis is EvidenceBasis.UNAVAILABLE


def test_the_refusal_names_every_layer_that_said_no() -> None:
    """``Watchdog._blind`` uses ``quote.note`` as the event's ``reason``.

    One joined string is the difference between an operator who can see that the curve
    refused a non-SOL quote and the router found no route, and one who sees
    ``price_unavailable`` for the fourth hour running.
    """
    got = wd.FallbackPriceSource(
        refusing("curve", CURVE_REFUSAL),
        refusing("prices", DEX_REFUSAL),
        refusing("jupiter", "no_route"),
    ).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.note is not None
    for fragment in ("curve", "non_sol_quote", "prices", "jupiter", "no_route"):
        assert fragment in got.note, f"{fragment!r} missing from {got.note!r}"


def test_one_chatty_layer_cannot_truncate_the_layers_behind_it() -> None:
    """The LAST refusal is usually the one that says why nothing could answer.

    Capping only the join would let a provider that returns a 4 KB error page push the
    router's ``no_route`` off the end of the reason the operator reads.
    """
    chain = wd.FallbackPriceSource(
        refusing("prices", "x" * 4000),
        refusing("jupiter", "no_route"),
    )
    got = chain.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert "jupiter: no_route" in (got.note or ""), got.note
    # And the reason stays a reason, not a 4 KB payload on every blind event.
    assert len(got.note or "") <= (chain.NOTE_CHARS_PER_SOURCE + 4) * len(chain.sources)


def test_a_source_that_raises_is_a_refusal_and_the_chain_keeps_going() -> None:
    """"Must never raise" is a promise, not a guarantee - the watchdog's own words.

    A dead layer must not cost us the layer behind it: that would turn one provider
    outage into a blind position that a working source could have priced.
    """
    boom = _Source("curve", raises=True)
    last = priced("jupiter")
    got = wd.FallbackPriceSource(boom, last).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.usable and got.source == "jupiter:route"
    assert last.calls == [(Chain.SOL, BONK_QUOTED_MINT)]


def test_a_source_that_raises_and_no_survivor_still_refuses() -> None:
    got = wd.FallbackPriceSource(_Source("curve", raises=True)).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert not got.usable
    assert "RuntimeError" in (got.note or ""), "the exception type is the diagnosis"


def test_a_source_that_returns_something_that_is_not_a_pricequote_is_a_refusal() -> None:
    """``jupiter._DetachedPriceQuote`` exists for exactly this case and is not a PriceQuote.

    The watchdog rejects anything that is not its own ``PriceQuote``; the chain must reject
    it the same way instead of handing a duck-typed object to ``evaluate``.
    """
    junk = _Source("jupiter", quote={"price_usd": "1"})
    got = wd.FallbackPriceSource(junk).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert not got.usable
    assert "dict" in (got.note or "")


@pytest.mark.parametrize("bad", ["0", "-0.5"])
def test_a_zero_or_negative_price_is_not_a_price_and_falls_through(bad: str) -> None:
    """A curve that priced to zero is a broken read, not a free token."""
    zero = _Source(
        "curve",
        quote=wd.PriceQuote(
            price_usd=Decimal(bad), basis=EvidenceBasis.DERIVED, source="curve:snapshot"
        ),
    )
    last = priced("jupiter")
    got = wd.FallbackPriceSource(zero, last).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.usable and got.source == "jupiter:route", bad


def test_a_price_carried_on_an_unavailable_basis_is_not_usable() -> None:
    """``PriceQuote.usable`` is the single definition of "good enough for a stop"."""
    tainted = _Source(
        "curve",
        quote=wd.PriceQuote(
            price_usd=Decimal("1"), basis=EvidenceBasis.UNAVAILABLE, source="curve:stale"
        ),
    )
    last = priced("jupiter")
    assert wd.FallbackPriceSource(tainted, last).quote(Chain.SOL, BONK_QUOTED_MINT).usable
    assert last.calls, "an UNAVAILABLE basis must not short-circuit the chain"


def test_an_empty_chain_refuses_rather_than_crashing_and_says_so() -> None:
    """An empty note is a blind event with no reason on it, which is how this stayed hidden."""
    got = wd.FallbackPriceSource().quote(Chain.SOL, BONK_QUOTED_MINT)
    assert not got.usable and got.price_usd is None
    assert "no price sources configured" in (got.note or "")


def test_none_entries_are_dropped_not_called() -> None:
    """A layer whose module failed to import arrives as ``None``; it must not be a crash."""
    got = wd.FallbackPriceSource(None, priced("jupiter")).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.usable


# ============================================================ it is a LAST resort


def test_the_first_usable_quote_wins_and_later_sources_are_never_called() -> None:
    """The last resort costs a network call. It must not be paid when the curve answered.

    Pinned by call counting rather than by timing, because the cost that matters is the
    limiter reservation, and that is taken whether or not the call is slow.
    """
    curve = priced("curve", "0.0001")
    router = priced("jupiter", "0.0002")
    got = wd.FallbackPriceSource(curve, router).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.source == "curve:route" and got.price_usd == Decimal("0.0001")
    assert router.calls == [], "the router was consulted although the curve had a price"


def test_the_order_is_the_order_given() -> None:
    a, b, c = refusing("curve", "x"), refusing("prices", "y"), priced("jupiter")
    wd.FallbackPriceSource(a, b, c).quote(Chain.SOL, BONK_QUOTED_MINT)
    assert [s.calls and s.name for s in (a, b, c)] == ["curve", "prices", "jupiter"]


def test_the_live_shape_a_bonk_quoted_curve_priced_by_the_router() -> None:
    """The incident, end to end, with the measured refusals in the measured order."""
    src = wd.FallbackPriceSource(
        refusing("curve", CURVE_REFUSAL),
        refusing("prices", DEX_REFUSAL),
        priced("jupiter", "0.000002346"),
    )
    got = src.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.usable and got.price_usd == Decimal("0.000002346")


# ============================================================ the wiring


def sol_route(_tmp_db):
    """The Solana route of the source the live box actually runs (``price_source: venue``)."""
    from kaiba.execution.evm_price import venue_price_source

    return venue_price_source().routes[Chain.SOL]


def test_the_solana_route_is_still_the_curve_source_reading_at_exit(tmp_db) -> None:
    """Widening the fallback must not cost the properties the curve route already had.

    ``tests/test_curve_price_priority.py`` pins ``routes[Chain.SOL].resolver`` at
    ``Priority.EXIT``; wrapping the route in another object would have quietly broken it.
    """
    route = sol_route(tmp_db)
    assert getattr(route, "name", None) == "curve"
    assert callable(getattr(route, "resolver", None))


def test_the_solana_route_has_a_chain_behind_the_curve_not_one_source(tmp_db) -> None:
    """``price_source: venue`` is what the live box runs; this is the thing being fixed."""
    chained = sol_route(tmp_db).fallback
    assert isinstance(chained, wd.FallbackPriceSource), (
        "the curve still has a single fallback; a DEX with no pair is blindness again"
    )
    names = [getattr(s, "name", type(s).__name__) for s in chained.sources]
    assert names[0] == "prices", f"the DEX stack must stay first behind the curve: {names}"
    assert names[-1] == "jupiter", f"the router must be LAST, not preferred: {names}"


def test_the_last_resort_is_built_with_a_bounded_slot_wait(tmp_db) -> None:
    """An unbounded wait is how a fallback becomes a protection overrun.

    ``jupiter.DEFAULT_WAIT_FOR_SLOT_S`` is 10.0 s against a 5 s poll interval: a throttled
    limiter would park a prefetch worker for two whole ticks. ``OVERRUN_HALT_TICKS`` says
    three consecutive ticks at twice the budget halt entries on every chain, which is the
    failure this fix must not trade for the one it repairs.
    """
    from kaiba.execution.evm_price import ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S

    router = sol_route(tmp_db).fallback.sources[-1]
    assert router.wait_for_slot_s == ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S
    assert 0 < ROUTER_LAST_RESORT_WAIT_FOR_SLOT_S <= 2.5, "measured budget is 5000 ms a tick"
    assert router.with_liquidity is False, (
        "the liquidity round trip cost 1 of 4 mints its price at this slot budget"
    )


def test_the_last_resort_reads_at_exit_priority(tmp_db) -> None:
    """A read that decides whether to close a position outranks discovery. Always has."""
    from kaiba.core.limiter import Priority

    assert sol_route(tmp_db).fallback.sources[-1].priority is Priority.EXIT


def test_the_evm_routes_are_unchanged(tmp_db) -> None:
    """Jupiter routes Solana only. Nothing about BSC or Robinhood moves here."""
    from kaiba.execution.evm_price import VENUE_READERS, EvmVenuePriceSource, venue_price_source

    routes = venue_price_source().routes
    for chain in VENUE_READERS:
        assert isinstance(routes[chain], EvmVenuePriceSource), chain


def test_venue_still_resolves_to_a_real_source(tmp_db) -> None:
    """The 2026-09-21 incident: a name that degrades to blindness looks armed and is not."""
    assert not isinstance(wd.resolve_price_source("venue"), wd.NullPriceSource)


# ============================================================ the blind reason says why


def test_the_curve_keeps_its_own_refusal_when_the_fallback_also_refuses(tmp_db) -> None:
    """``non_sol_quote`` is the diagnosis, and it used to be discarded.

    ``Watchdog._blind`` reports ``quote.note`` as the ``reason``. Before this, a curve
    refusal followed by a DEX refusal reported only the DEX's — so four live positions
    said "provider returned no price" for hours while the real answer, that the launch is
    quoted in BONK, was computed and thrown away.
    """
    from kaiba.execution import curve_price as cp

    source = cp.CurvePriceSource(
        tmp_db,
        sol_usd=Decimal("116.32"),
        resolver=lambda chain, token, at_ms: (None, "non_sol_quote:Dz9mQ9NzkBcC"),
        fallback=refusing("prices", DEX_REFUSAL),
    )
    got = source.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert not got.usable
    assert "non_sol_quote" in (got.note or ""), got.note
    assert DEX_REFUSAL in (got.note or ""), got.note


def test_a_usable_fallback_is_still_returned_untouched(tmp_db) -> None:
    """The note is only rebuilt on the refusal path; a real answer must pass through."""
    from kaiba.execution import curve_price as cp

    source = cp.CurvePriceSource(
        tmp_db,
        sol_usd=Decimal("116.32"),
        resolver=lambda chain, token, at_ms: (None, "no_curve_snapshot"),
        fallback=priced("prices", "0.5"),
    )
    got = source.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert got.usable and got.source == "prices:route" and got.price_usd == Decimal("0.5")


def test_a_fallback_that_hands_back_a_stand_in_becomes_an_honest_refusal(tmp_db) -> None:
    """``jupiter._DetachedPriceQuote`` is not a ``PriceQuote`` and the watchdog rejects it.

    Returning it unchanged made the position blind with ``price source returned
    _DetachedPriceQuote`` as its reason. It is still blind, but now it says why.
    """
    from kaiba.execution import curve_price as cp
    from kaiba.providers.jupiter import _DetachedPriceQuote

    stand_in = _DetachedPriceQuote(note="no_route", source="jupiter:no_route")
    assert not isinstance(stand_in, wd.PriceQuote), "the premise of this test"

    source = cp.CurvePriceSource(
        tmp_db,
        sol_usd=Decimal("116.32"),
        resolver=lambda chain, token, at_ms: (None, "non_sol_quote:Dz9mQ9NzkBcC"),
        fallback=_Source("jupiter", quote=stand_in),
    )
    got = source.quote(Chain.SOL, BONK_QUOTED_MINT)
    assert isinstance(got, wd.PriceQuote) and not got.usable
    assert "non_sol_quote" in (got.note or "") and "no_route" in (got.note or "")
