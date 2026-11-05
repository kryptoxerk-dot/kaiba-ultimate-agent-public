"""An exit must not be blocked because the VENUE cannot quote the native asset.

THE LIVE BUG, 2026-09-22. Two funded bsc positions could be bought and could not be sold.
Every tick the watchdog tried a 100% exit and every tick it refused:

    exit_failed  order_id=None  pct=100  attempts=41
    detail: native price unavailable; cannot compute min_out and will not send min_out=0

The refusal itself is right. ``min_out=0`` is an open invitation to a sandwich, and a
watchdog that sends one because it could not do the arithmetic is worse than one that
shouts. What was wrong is that the arithmetic WAS possible.

``_min_out`` asks the configured price source for the native asset's own quote. Measured
on the box with the shipped ``venue`` source:

    sol        native So111...  usable=True   $116.87
    bsc        native 0x000...  usable=FALSE  "no venue curve: flap_portal_read_failed"
    robinhood  native 0x000...  usable=True   $2729.12

The venue source prices tokens off their launch curve, and the native asset has no launch
curve -- on sol and robinhood a pool happens to answer anyway, on bsc nothing does. So the
native read is a coin flip on which chain you are on, and bsc lost.

Meanwhile ``native_prices`` held bsc at **$787.98, 36 seconds old**, sampled from
dexscreener every 30 s. That table exists precisely to answer "what is one native unit
worth", and the exit path never looked at it.

These tests pin the fallback and, more importantly, pin that it is a FALLBACK: a stale or
missing sample must still refuse, because the refusal is the safe behaviour and the only
thing being fixed is the case where we genuinely knew the answer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Lane, LaneMode, now_ms
from kaiba.execution import watchdog as wd
from tests.test_watchdog import make_position

BSC_TOKEN = "0x5d444e680f822a246d5c41f9fef5f9b5e97b7777"
NATIVE_USD = Decimal("787.98")


class _Source:
    """Prices the position's token, and optionally refuses the native asset like bsc does."""

    name = "test-venue"

    def __init__(self, *, native_usable: bool) -> None:
        self.native_usable = native_usable
        self.asked: list[str] = []

    def quote(self, chain: Chain, token: str) -> wd.PriceQuote:
        self.asked.append(token)
        if token == wd._native_mint(chain):
            if not self.native_usable:
                return wd.PriceQuote.unavailable("no venue curve: flap_portal_read_failed")
            return wd.PriceQuote(
                price_usd=NATIVE_USD, basis=EvidenceBasis.PROVIDER_REPORTED, source="test-venue"
            )
        return wd.PriceQuote(
            price_usd=Decimal("0.0001"), basis=EvidenceBasis.PROVIDER_REPORTED, source="test-venue"
        )


def store_native(conn, chain: Chain, price: str, *, age_ms: int) -> None:
    conn.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source, pair, liquidity_usd) "
        "VALUES (?,?,?,?,?,?)",
        (chain.value, now_ms() - age_ms, price, "dexscreener", "test", "1000000"),
    )
    conn.commit()


def bsc_position(conn):
    make_position(conn, position_id="pos_bsc", token=BSC_TOKEN, chain=Chain.BSC,
                  mode=LaneMode.LIVE, lane=Lane.SM_TRENCHES, qty=10**21, entry="0.0001")
    conn.execute("INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms) "
                 "VALUES (?,?,?,?)", (Chain.BSC.value, BSC_TOKEN, 18, now_ms()))
    conn.commit()
    watch = wd.Watchdog(conn, price_source=_Source(native_usable=True))
    return [p for p in watch._open_positions() if p.position_id == "pos_bsc"][0]


def submitter(conn, source):
    """`_min_out` lives on the exit submitter, which is the thing that builds the sell."""
    return wd.DefaultExitSubmitter(conn, price_source=source)


def token_quote() -> wd.PriceQuote:
    return wd.PriceQuote(price_usd=Decimal("0.0001"),
                         basis=EvidenceBasis.PROVIDER_REPORTED, source="test-venue")


# ------------------------------------------------------------------ the fix


def test_a_venue_that_cannot_quote_the_native_asset_falls_back_to_the_sampler(tmp_db):
    """The live bsc case: venue says no, the sampler has it 36 s old, the exit proceeds."""
    store_native(tmp_db, Chain.BSC, str(NATIVE_USD), age_ms=36_000)
    pos = bsc_position(tmp_db)
    got = submitter(tmp_db, _Source(native_usable=False))._min_out(pos, pos.qty, 18, token_quote())
    assert got is not None and got > 0, "a fresh stored sample must unblock the exit"


def test_with_neither_source_the_exit_still_refuses(tmp_db):
    """Nothing here weakens the refusal. No price is still no sell."""
    pos = bsc_position(tmp_db)   # no sample stored at all
    assert submitter(tmp_db, _Source(native_usable=False))._min_out(pos, pos.qty, 18, token_quote()) is None


def test_a_stale_sample_does_not_count(tmp_db):
    """Outside the tolerance the sampler answers None, and None must stay a refusal."""
    store_native(tmp_db, Chain.BSC, str(NATIVE_USD), age_ms=48*60*60*1000)
    pos = bsc_position(tmp_db)
    assert submitter(tmp_db, _Source(native_usable=False))._min_out(pos, pos.qty, 18, token_quote()) is None


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_zero_or_negative_sample_is_not_a_price(tmp_db, bad):
    """A stored zero is not a cheap native asset; it is a broken sample."""
    store_native(tmp_db, Chain.BSC, bad, age_ms=1_000)
    pos = bsc_position(tmp_db)
    assert submitter(tmp_db, _Source(native_usable=False))._min_out(
        pos, pos.qty, 18, token_quote()) is None, bad


def test_the_venue_is_preferred_when_it_can_answer(tmp_db):
    """The sampler is a fallback, not a replacement: the venue read is fresher.

    Pinned by giving the two sources DIFFERENT prices and checking which one came out.
    """
    store_native(tmp_db, Chain.BSC, "1.00", age_ms=1_000)     # deliberately absurd
    pos = bsc_position(tmp_db)
    got = submitter(tmp_db, _Source(native_usable=True))._min_out(pos, pos.qty, 18, token_quote())
    stored_would_give = submitter(tmp_db, _Source(native_usable=False))._min_out(
        pos, pos.qty, 18, token_quote())
    assert got is not None and stored_would_give is not None
    assert got != stored_would_give
    assert got < stored_would_give, "at $787.98 a unit we should insist on FEWER native units"


def test_the_token_price_is_still_required(tmp_db):
    """The fallback covers the NATIVE leg only. An unpriceable token still refuses."""
    store_native(tmp_db, Chain.BSC, str(NATIVE_USD), age_ms=1_000)
    pos = bsc_position(tmp_db)
    blind = wd.PriceQuote.unavailable("no quote")
    assert submitter(tmp_db, _Source(native_usable=False))._min_out(pos, pos.qty, 18, blind) is None


def test_slippage_is_applied_to_the_fallback_too(tmp_db):
    """A fallback that skipped the slippage haircut would send a min_out we cannot fill."""
    from kaiba.core.config import get_risk

    store_native(tmp_db, Chain.BSC, str(NATIVE_USD), age_ms=1_000)
    pos = bsc_position(tmp_db)
    got = submitter(tmp_db, _Source(native_usable=False))._min_out(pos, pos.qty, 18, token_quote())
    tokens = Decimal(pos.qty) / (Decimal(10) ** 18)
    gross = tokens * Decimal("0.0001") / NATIVE_USD * (Decimal(10) ** 18)
    bps = Decimal(int(wd._exit_slippage_bps()))
    assert got == int(gross * (Decimal(10_000) - bps) / Decimal(10_000))
