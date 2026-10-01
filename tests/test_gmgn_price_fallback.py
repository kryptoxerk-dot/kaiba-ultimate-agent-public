"""A last-resort price for tokens no venue reader and no pair can see.

MEASURED 2026-09-23. A live robinhood sm-trenches position (``CASHVLAD``,
``0x9f06f1978809b119...``) could not be priced by anything::

    venue     -> None   (evm-venue)
    prices    -> None   (dexscreener, no pair)
    jupiter   -> None   (Solana router)

It stayed blind for 585 s against a 300 s budget and tripped
``protection_blind_timeout``, which halts entries on EVERY chain. It was still halted
half an hour later, because the watchdog needs a price to compute ``min_out`` and so
could not exit the position either -- blind, unsellable, and blocking all trading.

The token was on ``pons_v2`` at ``launchpad_progress: 0.107``: a newer launchpad revision
our Pons reader does not decode, still on its bonding curve so DexScreener had no pair to
index. Both refusals were correct.

GMGN could price it the whole time. ``token info`` carried
``price: "0.0000052800086"`` alongside the liquidity and supply we already read from the
same call.

WHY IT IS LAST. :class:`FallbackPriceSource` stops at the first usable quote, so this
layer costs nothing -- no request, no limiter credit -- on every position the venue or the
pair already prices. It is asked only when the alternative is blindness, which is the only
situation in which a provider's own mid-price is better than nothing.

WHAT IT IS NOT. It is not a depth source and does not pretend to be: the quote carries a
price and no liquidity, so anything that needs to model impact (the paper broker, the
anti-wick check) still refuses rather than sizing against a number this cannot give.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, Receipt
from kaiba.execution import watchdog as W


@pytest.fixture
def token_info(monkeypatch):
    """Patch the provider call, keyed by token, returning GMGN's real payload shape."""
    calls: list = []

    def fake(address, chain=Chain.SOL, **kw):
        calls.append((str(chain), address))
        if address == "PRICELESS":
            return type("R", (), {"data": {"address": address}, "receipt": None})()
        return type("R", (), {
            "data": {
                "address": address,
                "liquidity": "1732.24176",
                "price": {"price": "0.0000052800086", "price_1m": "0.0000052800086"},
            },
            "receipt": Receipt(provider="gmgn", endpoint="token.info"),
        })()

    monkeypatch.setattr(W, "_gmgn_token_info", fake)
    return calls


def test_it_prices_a_token_nothing_else_can(token_info):
    """THE REGRESSION: this token halted every chain for half an hour."""
    quote = W.GmgnPriceSource().quote(Chain.ROBINHOOD, "0x9f06f1978809b119")
    assert quote.price_usd == Decimal("0.0000052800086")


def test_it_reports_where_the_price_came_from(token_info):
    quote = W.GmgnPriceSource().quote(Chain.ROBINHOOD, "0xabc")
    assert "gmgn" in (getattr(quote, "source", "") or "").lower()


def test_a_payload_with_no_price_is_unavailable_not_zero(token_info):
    quote = W.GmgnPriceSource().quote(Chain.ROBINHOOD, "PRICELESS")
    assert quote.price_usd is None


def test_it_offers_no_liquidity(token_info):
    """It is a price, not a depth. Anything modelling impact must still refuse."""
    quote = W.GmgnPriceSource().quote(Chain.ROBINHOOD, "0xabc")
    assert getattr(quote, "liquidity_usd", None) is None


def test_a_raising_provider_is_blindness_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cli died")

    monkeypatch.setattr(W, "_gmgn_token_info", boom)
    assert W.GmgnPriceSource().quote(Chain.ROBINHOOD, "0xabc").price_usd is None


@pytest.mark.parametrize("bad", ["0", "-1", "", "abc", None])
def test_an_unusable_price_is_refused(monkeypatch, bad):
    monkeypatch.setattr(
        W, "_gmgn_token_info",
        lambda address, chain=Chain.SOL, **kw: type("R", (), {
            "data": {"price": {"price": bad}}, "receipt": None})(),
    )
    assert W.GmgnPriceSource().quote(Chain.ROBINHOOD, "0xabc").price_usd is None


# ------------------------------------------------------------------ where it sits


def test_the_venue_chain_includes_gmgn():
    """Registered, or the incident repeats with a source nobody reaches.

    Pins the PROPERTY, not the class. Until 2026-09-23 this chain was a plain
    `FallbackPriceSource`; it is now chain-aware, because robinhood's curve reader is
    rationed at 0.6 calls/s and asking it first set the protection tick. What must stay
    true is that gmgn is reachable and that the curve reader is still in the chain -- a
    composition, never a replacement.
    """
    built = W._venue_price_source()
    sources = [*getattr(built, "rest", ()), *getattr(built, "sources", ())]
    sources.extend(getattr(built, "first", {}).values())
    assert any(isinstance(x, W.GmgnPriceSource) for x in sources), (
        f"gmgn is not reachable from the venue chain: {built!r}"
    )
    assert any(not isinstance(x, W.GmgnPriceSource) for x in sources), (
        "gmgn REPLACED the venue reader instead of composing with it"
    )


def test_a_priceable_token_never_reaches_it(token_info, monkeypatch):
    """First usable wins, so this layer costs nothing on a position already priced."""
    class _Venue:
        def quote(self, chain, token):
            # The basis matters here too: without it this stub is itself unusable and the
            # chain falls through, which would make the test pass for the wrong reason.
            from kaiba.core.schemas import EvidenceBasis

            return W.PriceQuote(
                price_usd=Decimal("1.5"),
                liquidity_usd=Decimal("1000"),
                basis=EvidenceBasis.PROVIDER_REPORTED,
            )

    chain = W.FallbackPriceSource(_Venue(), W.GmgnPriceSource())
    quote = chain.quote(Chain.ROBINHOOD, "0xabc")
    assert quote.price_usd == Decimal("1.5")
    assert token_info == [], "gmgn was called for a token the venue already priced"


def test_it_is_reached_when_every_earlier_layer_refuses(token_info):
    class _Blind:
        def quote(self, chain, token):
            return W.PriceQuote.unavailable("no pair")

    chain = W.FallbackPriceSource(_Blind(), W.GmgnPriceSource())
    assert chain.quote(Chain.ROBINHOOD, "0xabc").price_usd == Decimal("0.0000052800086")
    assert token_info, "gmgn was never asked"


def test_every_source_in_the_venue_chain_can_actually_quote():
    """A container that is not a source makes the whole chain blind, silently.

    The first version of the gmgn change passed a LIST to `FallbackPriceSource(*sources)`,
    which made the list itself the single source: no `.quote`, every call raising, blind
    everywhere. This asserts the shape rather than the spelling, so it keeps holding now
    that the chain is chain-aware.
    """
    built = W._venue_price_source()
    sources = [*getattr(built, "rest", ()), *getattr(built, "sources", ())]
    sources.extend(getattr(built, "first", {}).values())
    assert len(sources) >= 2, f"the venue chain has no depth: {built!r}"
    for source in sources:
        assert callable(getattr(source, "quote", None)), f"{source!r} cannot quote"


def test_the_curve_reader_is_asked_before_gmgn_on_every_chain():
    """The venue reader stays FIRST, including on robinhood. Tried the other way; reverted.

    MEASURED 2026-09-23, both directions, same day:

    robinhood-rpc is `max_inflight: 1`, `min_interval_ms: 1500`, `refill_per_s: 0.6`, so
    ten open robinhood positions take ~16.7 s to sweep and that set the protection tick.
    Putting `GmgnPriceSource` first for robinhood fixed exactly that -- robinhood-rpc's
    credit recovered from -19,753 to +2,000 -- and it made the thing protection is FOR
    worse: robinhood `protection_blind` events went 40.3/h -> 59.0/h across the same open
    book, and the operator agent paused entries over it within three minutes.

    The likely mechanism is that the curve reader's 1-in-flight path waits for its slot
    while a refused gmgn call raises and falls straight through, so a prefetch burst that
    used to serialise now partly misses. That is a hypothesis. It was reverted rather than
    tuned because a protection gap is not the place to test one.

    The tick cost is carried by `poll_interval_s: 12` instead, which is measured and
    holding. If this is attempted again, the thing to change first is making the fallback
    WAIT for a limiter slot rather than reading a refusal as an inability to price.
    """
    built = W._venue_price_source()
    sources = [*getattr(built, "rest", ()), *getattr(built, "sources", ())]
    assert sources, f"the venue chain is empty: {built!r}"
    assert not isinstance(sources[0], W.GmgnPriceSource), (
        "gmgn is first again; see this test's docstring for the measurement that reverted it"
    )
    assert any(isinstance(x, W.GmgnPriceSource) for x in sources), "gmgn left the chain"
