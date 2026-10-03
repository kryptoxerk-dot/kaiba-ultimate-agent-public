"""A paper exit is modelled against MEASURED depth, and is never closed for want of a fetch.

MEASURED 2026-10-03 on the box (read-only, ``~/kaiba/data/kaiba.db``), 18.4 h of events:

* All 255 ``exit_failed`` events were SHADOW exits refused with ``paper exit not modelled:
  liquidity unavailable``. Each was decided on ``gmgn:token.info`` (a price with no depth, by
  design) while the SAME tick's DexScreener read sat in ``rejected_quote`` -- for robinhood
  HOOKR, ``uniswap:HOOKR/ETH`` at $1,137,821 of depth.
* Why it was rejected: ``_credible_depth`` sizes the position with ``tokens.decimals`` and
  refuses when that is NULL. 23 of 27 shadow positions opened in 72 h had no recorded
  decimals (live: 0 of 34), because only the live path records them. So every pool read was
  "not credible", ``_validate_quote`` fell through to GMGN, and the paper broker -- whose
  whole value is modelling impact against real depth -- correctly refused.
* What it cost: 18 of 18 shadow positions without decimals closed in the last 3 days were
  closed as ``abandoned_unpriceable`` at exactly -100% of cost; the 4 with decimals exited
  on stops and trails. ``_abandon_unpriceable_shadow`` measures the position's AGE, and the
  first unusable tick after an hour closed them -- and 1,553 of 1,557 shadow "unusable"
  ticks were the prefetch's own deferral ("shadow quote deferred while live protection has
  priority"), i.e. a quote nobody asked for.

The rule kept throughout: no guessed or default depth ever reaches the paper broker. What
changes is (1) a shadow position is sized in the units the paper broker minted it in, (2) a
paper exit decided on a depthless corroborating price borrows THIS tick's measured pool
depth, when that read is fresh, names its pool and is credible for our size, and (3) a
deferred quote is not evidence that a position is unpriceable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import SOL_NATIVE_MINT, Chain, EvidenceBasis, Lane, LaneMode, now_ms
from kaiba.execution import paper
from kaiba.execution import watchdog as wd
from kaiba.execution.protection import ProtectionConfig
from tests.test_watchdog import (  # noqa: F401 - risk_file is a fixture used by name
    RecordingSubmitter,
    events_named,
    make_position,
    risk_file,
)

TOKEN = "6WNogxXa4Lswh8i7U4BSaN9J5jgHrVbwvuYjr9zgpump"
POOL = "7hiQwEhBskLVW34jiW5jKGAwnFAwxrC2BUTGHYRWM45p"
RH_TOKEN = "0x18e674231a58c239dc7daedcffe15ec3a24cff5c"


# --------------------------------------------------------------------------------------
# doubles: the production leaves, shape for shape
# --------------------------------------------------------------------------------------


def pool_quote(token: str = TOKEN, *, price="1.0", liquidity="25017.12", chain: Chain = Chain.SOL,
               pool: str | None = POOL, kind: str | None = "dex_tvl_usd", age_s: float = 0.0) -> wd.PriceQuote:
    """A DexScreener pool mark, as ``ProviderPriceSource`` builds it."""
    return wd.PriceQuote(
        price_usd=Decimal(price),
        liquidity_usd=None if liquidity is None else Decimal(liquidity),
        basis=EvidenceBasis.PROVIDER_REPORTED,
        observed_ms=now_ms() - int(age_s * 1000),
        source="prices:dexscreener",
        chain=chain, token=token, pool_id=pool, venue="pumpswap", liquidity_kind=kind,
        quote_token=SOL_NATIVE_MINT if chain is Chain.SOL else wd.EVM_ZERO,
        settlement=wd.SETTLE_NATIVE_POOL,
    )


def gmgn_quote(token: str = TOKEN, *, price="1.0", chain: Chain = Chain.SOL) -> wd.PriceQuote:
    """GMGN's mid: a price and no depth, exactly as ``GmgnPriceSource`` returns it."""
    return wd.PriceQuote(price_usd=Decimal(price), liquidity_usd=None, basis=EvidenceBasis.PROVIDER_REPORTED,
                         observed_ms=now_ms(), freshness_budget_s=15.0, source="gmgn:token.info",
                         chain=chain, token=token)


class PoolLayer(wd.ProviderPriceSource):
    """Stands where the DexScreener reader stands (``_validate_quote`` keys on the class)."""

    def __init__(self, price: str | None, liquidity: str = "25017.12") -> None:
        self.price, self.liquidity, self.calls = price, liquidity, 0

    def quote(self, chain, token):
        self.calls += 1
        if self.price is None:
            return wd.PriceQuote.unavailable("fixture: pool refuses this tick", source="prices")
        return pool_quote(token, price=self.price, liquidity=self.liquidity, chain=chain)


class GmgnLayer(wd.GmgnPriceSource):
    def __init__(self, price: str | None) -> None:
        self.price, self.calls = price, 0

    def quote(self, chain, token):
        self.calls += 1
        if self.price is None:
            return wd.PriceQuote.unavailable("fixture: gmgn refuses", source="gmgn")
        return gmgn_quote(token, price=self.price, chain=chain)


def cfg() -> ProtectionConfig:
    return ProtectionConfig(
        use_provider_orders=False, stop_loss_bps=3000,
        tp_ladder=[(Decimal("5.0"), Decimal(50))], trailing=[(Decimal("100.0"), 1000)],
        breakeven_after_tp1=False, stale_no_volume_exit_s=0, moonbag_retain_pct=0,
    )


@pytest.fixture
def offline_curve(monkeypatch):
    """Keep the paper broker on its offline snapshot resolver (no pump.fun call in a test)."""
    monkeypatch.setattr(wd, "_curve_resolver", lambda conn: None)


def shadow(conn, *, token: str = TOKEN, chain: Chain = Chain.SOL, qty: int = 1_000_000,
           position_id: str = "pos_1", age_s: float = 0.0, mode: LaneMode = LaneMode.SHADOW):
    make_position(conn, position_id=position_id, token=token, chain=chain, mode=mode,
                  lane=Lane.MIGRATION_FADE if chain is Chain.SOL else Lane.SM_TRENCHES, qty=qty)
    if age_s:
        conn.execute("UPDATE positions SET opened_ms=? WHERE position_id=?",
                     (now_ms() - int(age_s * 1000), position_id))
    conn.commit()
    return next(p for p in wd.open_positions(conn) if p.position_id == position_id)


def closed(conn, position_id: str = "pos_1"):
    return conn.execute("SELECT closed_ms, exit_reason, qty FROM positions WHERE position_id=?",
                        (position_id,)).fetchone()


def no_decimals(conn, token: str = TOKEN, chain: Chain = Chain.SOL) -> None:
    """The box's shadow tokens: a ``tokens`` row may exist, ``decimals`` is NULL."""
    assert wd.token_decimals(conn, chain, token) is None


# --------------------------------------------------------------------------------------
# (1) a shadow position is sized in the paper broker's own units
# --------------------------------------------------------------------------------------


def test_shadow_pool_depth_is_credible_without_recorded_decimals(tmp_db):
    """THE ROOT CAUSE: NULL decimals made every pool read 'not credible' for paper."""
    position = shadow(tmp_db)
    no_decimals(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert dog._credible_depth(position, pool_quote(), None) is True


def test_shadow_sizing_uses_the_units_the_paper_broker_minted(tmp_db):
    """Same function both ends: the size judged here is the size the broker will sell."""
    position = shadow(tmp_db, qty=5_000_000_000)          # 5,000 tokens at sol's paper 6 dp
    assert wd._paper_qty_decimals(tmp_db, position) == paper._token_decimals(Chain.SOL, TOKEN, tmp_db, None) == 6
    rh = shadow(tmp_db, token=RH_TOKEN, chain=Chain.ROBINHOOD, position_id="pos_rh")
    assert wd._paper_qty_decimals(tmp_db, rh) == 18


def test_a_thin_pool_is_still_not_credible_for_paper(tmp_db):
    """Units are not a free pass: our size must still trade inside the slippage bound."""
    position = shadow(tmp_db, qty=5_000_000_000)          # $5,000 at $1
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    thin = pool_quote(liquidity="1000")                   # 5000/(1000+5000) = 8,333 bps
    assert dog._credible_depth(position, thin, None) is False


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_live_depth_still_needs_real_decimals(tmp_db, mode):
    """LIVE IS UNCHANGED: a real position's units are a fact or the depth is not credible."""
    position = shadow(tmp_db, mode=mode)
    no_decimals(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    assert wd._paper_qty_decimals(tmp_db, position) is None
    assert dog._credible_depth(position, pool_quote(), None) is False


@pytest.mark.usefixtures("risk_file", "offline_curve")
def test_an_established_pool_closes_a_paper_stop_against_its_depth(tmp_db):
    """End to end, the box's sol migration-fade case: no decimals, pool + GMGN chain."""
    shadow(tmp_db)
    no_decimals(tmp_db)
    source = wd.FallbackPriceSource(PoolLayer("1.0"), GmgnLayer("1.0"), name="venue+gmgn")
    dog = wd.Watchdog(tmp_db, price_source=source, cfg_provider=cfg,
                      submitter=wd.DefaultExitSubmitter(tmp_db, source))
    dog.tick()                                            # hold: the pool is now the source
    source.sources[0].price = "0.6"
    source.sources[1].price = "0.6"
    report = dog.tick()                                   # -40% against a -30% stop
    assert not events_named(tmp_db, "exit_failed")
    assert report.exits == 1
    row = closed(tmp_db)
    assert row["closed_ms"] is not None and row["exit_reason"] == "stop_loss"
    order = tmp_db.execute("SELECT order_id, provider, state FROM orders WHERE side='sell'").fetchone()
    assert order["provider"] == "paper" and order["state"] == "filled"
    basis = paper.fill_basis(tmp_db, order["order_id"])
    assert Decimal(basis["liquidity_usd"]) == Decimal("25017.12")


# --------------------------------------------------------------------------------------
# (2) a paper exit decided on a depthless price borrows THIS tick's measured depth
# --------------------------------------------------------------------------------------


@pytest.fixture
def lend(tmp_db):
    position = shadow(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    state = wd.WatchdogState(position_id=position.position_id)
    return dog, position, state


def test_depth_is_lent_from_the_ticks_own_pool_read(lend):
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    got = dog._with_paper_depth(position, state, deciding, pool_quote(price="0.61"), None)
    assert got.liquidity_usd == Decimal("25017.12")
    assert got.price_usd == Decimal("0.6") and got.source == "gmgn:token.info"   # price unchanged
    assert got.pool_id == POOL and got.liquidity_kind == "dex_tvl_usd"
    basis = state.quote_evidence["paper_exit_depth"]
    assert basis["pool_id"] == POOL and basis["source"] == "prices:dexscreener"
    assert basis["liquidity_usd"] == "25017.12" and basis["price_source"] == "gmgn:token.info"
    assert 0 <= basis["age_ms"] < 30_000


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY])
def test_a_live_exit_quote_is_passed_through_untouched(tmp_db, mode):
    """LIVE IS UNCHANGED: the very same object reaches the executor path."""
    position = shadow(tmp_db, mode=mode)
    tmp_db.execute("INSERT INTO tokens (chain,address,decimals,first_seen_ms) VALUES (?,?,?,?)",
                   (Chain.SOL.value, TOKEN, 6, now_ms()))
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    state = wd.WatchdogState(position_id=position.position_id)
    deciding = gmgn_quote(price="0.6")
    assert dog._with_paper_depth(position, state, deciding, pool_quote(), None) is deciding
    assert "paper_exit_depth" not in state.quote_evidence


def test_a_deciding_quote_with_its_own_depth_keeps_it(lend):
    dog, position, state = lend
    deciding = pool_quote(price="0.6", liquidity="500000", pool="OtherPool111")
    got = dog._with_paper_depth(position, state, deciding, pool_quote(), None)
    assert got is deciding and got.liquidity_usd == Decimal("500000")


def test_a_stale_pool_read_is_not_lent(lend):
    """Freshness is the bar every stop uses (``PriceQuote.usable``), checked at the exit."""
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    assert dog._with_paper_depth(position, state, deciding, pool_quote(age_s=60), None) is deciding
    assert "paper_exit_depth" not in state.quote_evidence


def test_nothing_measured_nothing_lent(lend):
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    assert dog._with_paper_depth(position, state, deciding, None, None) is deciding


@pytest.mark.parametrize("liquidity", [None, "0"])
def test_a_read_without_depth_lends_none(lend, liquidity):
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    assert dog._with_paper_depth(position, state, deciding, pool_quote(liquidity=liquidity), None) is deciding


@pytest.mark.parametrize("pool,kind", [(None, "dex_tvl_usd"), (POOL, None)])
def test_depth_from_a_pool_we_cannot_name_is_not_lent(lend, pool, kind):
    """Token-wide 'liquidity' (e.g. Jupiter's) is not a pool the impact model can stand on."""
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    assert dog._with_paper_depth(position, state, deciding, pool_quote(pool=pool, kind=kind), None) is deciding


def test_depth_from_another_token_is_not_lent(lend):
    dog, position, state = lend
    deciding = gmgn_quote(price="0.6")
    other = pool_quote(token="So11111111111111111111111111111111111111112")
    assert dog._with_paper_depth(position, state, deciding, other, None) is deciding


def test_depth_that_cannot_carry_our_size_is_not_lent(tmp_db):
    """A pool our size would move past the slippage bound is not the market GMGN priced."""
    position = shadow(tmp_db, qty=5_000_000_000)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    state = wd.WatchdogState(position_id=position.position_id)
    deciding = gmgn_quote(price="1.0")
    assert dog._with_paper_depth(position, state, deciding, pool_quote(liquidity="1000"), None) is deciding


@pytest.mark.usefixtures("risk_file", "offline_curve")
def test_a_corroborated_paper_stop_fills_against_the_pool(tmp_db):
    """End to end, the box's robinhood case: GMGN was being followed, the pool shows the
    stop first, ``_validate_quote`` lets GMGN decide, and the fill uses the pool's depth."""
    shadow(tmp_db, token=RH_TOKEN, chain=Chain.ROBINHOOD, qty=4_000_000_000_000_000_000)  # 4 tokens
    no_decimals(tmp_db, RH_TOKEN, Chain.ROBINHOOD)
    pool, gmgn = PoolLayer(None, liquidity="1137821.15"), GmgnLayer("1.0")
    source = wd.FallbackPriceSource(pool, gmgn, name="venue+gmgn")
    dog = wd.Watchdog(tmp_db, price_source=source, cfg_provider=cfg,
                      submitter=wd.DefaultExitSubmitter(tmp_db, source))
    dog.tick()                                            # pool refuses; GMGN is remembered
    pool.price, gmgn.price = "0.6", "0.6"
    report = dog.tick()
    assert not events_named(tmp_db, "exit_failed"), events_named(tmp_db, "exit_failed")
    assert report.exits == 1 and closed(tmp_db)["closed_ms"] is not None
    submitted = events_named(tmp_db, "exit_submitted")[0]
    assert submitted["quote_provenance"]["source"] == "gmgn:token.info"      # GMGN decided
    assert submitted["rejected_quote"]["source"] == "prices:dexscreener"
    assert submitted["paper_exit_depth"]["pool_id"] == POOL                  # basis recorded
    order = tmp_db.execute("SELECT order_id FROM orders WHERE side='sell'").fetchone()
    assert Decimal(paper.fill_basis(tmp_db, order["order_id"])["liquidity_usd"]) == Decimal("1137821.15")


# --------------------------------------------------------------------------------------
# (3) a deferred quote is not evidence that a position is unpriceable
# --------------------------------------------------------------------------------------


UNPRICEABLE = wd.PriceQuote.unavailable("shadow quote deferred while live protection has priority",
                                        source="watchdog")


def test_a_deferred_shadow_is_not_abandoned(tmp_db):
    position = shadow(tmp_db, age_s=2 * 3600)
    dog = wd.Watchdog(tmp_db, price_source=wd.NullPriceSource())
    dog._deferred_keys = {(position.chain, position.token)}
    assert dog._abandon_unpriceable_shadow(position, UNPRICEABLE) is False
    assert closed(tmp_db)["closed_ms"] is None


def _book(conn):
    """One live position and three hour-old paper ones: the prefetch fetches two, defers one."""
    make_position(conn, position_id="pos_live", token="LiveToken1111111111111111111111111111111111",
                  mode=LaneMode.LIVE, lane=Lane.SM_TRENCHES)
    conn.execute("INSERT INTO tokens (chain,address,decimals,first_seen_ms) VALUES (?,?,?,?)",
                 (Chain.SOL.value, "LiveToken1111111111111111111111111111111111", 6, now_ms()))
    for i, token in enumerate(["Aaaa1111111111111111111111111111111111111111",
                               "Bbbb1111111111111111111111111111111111111111",
                               "Cccc1111111111111111111111111111111111111111"]):
        shadow(conn, token=token, position_id=f"pos_s{i}", age_s=2 * 3600)
    conn.commit()


class Book:
    name = "book"

    def __init__(self, unpriceable: set[str] | None = None) -> None:
        self.unpriceable = unpriceable or set()

    def quote(self, chain, token):
        if token in self.unpriceable:
            return wd.PriceQuote.unavailable("fixture: nobody can price this")
        return pool_quote(token)


@pytest.mark.usefixtures("risk_file")
def test_a_tick_that_defers_paper_work_closes_no_paper_position(tmp_db):
    """THE REGRESSION: on the box every abandonment fell on a tick with live inventory and
    more than two paper keys, i.e. a tick that deferred paper quotes by design."""
    _book(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=Book(), cfg_provider=cfg, submitter=RecordingSubmitter())
    dog.tick()
    assert dog._deferred_keys, "the fixture must exercise the deferral"
    assert not events_named(tmp_db, "shadow_position_abandoned")
    assert len([p for p in wd.open_positions(tmp_db) if p.mode is LaneMode.SHADOW]) == 3


@pytest.mark.usefixtures("risk_file")
def test_a_really_unpriceable_paper_position_is_still_abandoned(tmp_db):
    """The 2026-09-23 abandon rule survives: asked, and nobody could price it. Also pins
    that a deferral is forgotten at the next tick (the set is per tick, like the cache)."""
    _book(tmp_db)
    dead = "Cccc1111111111111111111111111111111111111111"
    dog = wd.Watchdog(tmp_db, price_source=Book(unpriceable={dead}), cfg_provider=cfg,
                      submitter=RecordingSubmitter())
    dog.tick()                                            # fetches A, B; defers C
    assert (Chain.SOL, dead) in dog._deferred_keys
    assert closed(tmp_db, "pos_s2")["closed_ms"] is None
    dog.tick()                                            # fetches C, A; defers B
    row = closed(tmp_db, "pos_s2")
    assert row["closed_ms"] is not None and row["exit_reason"] == "abandoned_unpriceable"
    assert closed(tmp_db, "pos_s1")["closed_ms"] is None  # B was deferred on tick 2
