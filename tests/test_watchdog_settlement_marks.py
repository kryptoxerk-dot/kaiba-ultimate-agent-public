"""Exits are decided only on marks that settle in the asset a sell receives (journal #5048).

COZY (sol ``ETXqxf``, 2026-10-02): every protection mark came from a DexScreener pool quoted
in Bonk, and every one of its three exits filled 33-48% below the mark it fired on --
including a ``tp1_at_2.0x`` on a +141% mark. On sol, a foreign-quoted pool mark is now set
aside and the rest of the configured chain is asked (Jupiter's executable quote, then
GMGN); if nothing settles, the position is blind for the tick rather than decided on it.

Transport is fixtures throughout: DexScreener rows go through the real ``prices`` /
``pick_pair`` / ``ProviderPriceSource`` path via a stubbed ``get_json``; the executable layer
is a stub named like Jupiter's source so the watchdog classifies it the way it would.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import SOL_NATIVE_MINT, Chain, EvidenceBasis, Lane, LaneMode, Receipt, now_ms
from kaiba.execution import watchdog as wd
from kaiba.execution.evm_price import ChainRoutedPriceSource
from kaiba.execution.protection import ProtectionConfig
from kaiba.providers import dexscreener as ds
from kaiba.providers import prices
from kaiba.providers._http import Fetched
from tests.test_watchdog import RecordingSubmitter, events_named, make_position

COZY = "ETXqxfVoPN9f1qX1EV9qPWNqYuu5TUUUXMUM4SsniJga"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
BONK_POOL = "E7VkHPdTo8UKh54agKifhXtfVRMubzW4pttytSGuyApq"
SOL_DLMM = "mzUAZ9PLS83nbBH8zbB8JPu2KYtidLQqJLYtnEysPfE"
SOL_DYN2 = "2FA4bowRbuD2F97sRU6mQxNkwSL4vRgVjvDHUfbZHEKB"
RH_TOKEN = "0xef2edf46742057ab999518244921cc9b7e420a44"
RH_META = "0xc0d6457c16cc70d6790dd43521c899c87ce02f35"
RH_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def pair(base: str, quote: str, symbol: str, price, liquidity, address: str, chain: str = "solana") -> dict:
    row = {
        "chainId": chain, "dexId": "raydium", "pairAddress": address,
        "baseToken": {"address": base, "symbol": "TKN"},
        "quoteToken": {"address": quote, "symbol": symbol},
        "priceUsd": str(price),
    }
    if liquidity is not None:
        row["liquidity"] = {"usd": str(liquidity)}
    return row


def dex(monkeypatch, rows: list[dict]) -> None:
    receipt = Receipt(provider="dexscreener", endpoint="token.pairs", observed_at_ms=now_ms(),
                      basis=EvidenceBasis.PROVIDER_REPORTED)
    monkeypatch.setattr(ds, "get_json", lambda *a, **k: Fetched(list(rows), receipt))


def cozy_pools(monkeypatch, bonk_price) -> None:
    """COZY's real pool list, MEASURED 2026-10-03: a deep Bonk CPMM and two dust SOL pools."""
    dex(monkeypatch, [
        pair(COZY, BONK, "Bonk", bonk_price, "20081.37", BONK_POOL),
        pair(COZY, SOL_NATIVE_MINT, "SOL", Decimal(bonk_price) * Decimal("0.005"), "189.55", SOL_DLMM),
        pair(COZY, SOL_NATIVE_MINT, "SOL", Decimal(bonk_price) * Decimal("3.4"), "1.98", SOL_DYN2),
    ])


class Layer:
    """A downstream price layer under test control, labelled like the real one."""

    def __init__(self, source: str, price=None, *, chain: Chain = Chain.SOL, settlement=None) -> None:
        self.source = source
        self.price = price
        self.chain = chain
        self.settlement = settlement
        self.calls = 0
        self.name = source.split(":")[0]

    def quote(self, chain, token):
        self.calls += 1
        if self.price is None:
            return wd.PriceQuote.unavailable("fixture refuses", source=self.source)
        return wd.PriceQuote(price_usd=Decimal(str(self.price)), basis=EvidenceBasis.PROVIDER_REPORTED,
                             source=self.source, chain=chain, token=token, observed_ms=now_ms(),
                             settlement=self.settlement)


def cfg() -> ProtectionConfig:
    return ProtectionConfig(
        use_provider_orders=False, stop_loss_bps=3000,
        tp_ladder=[(Decimal("2.0"), Decimal(50))],
        trailing=[(Decimal("100.0"), 1000)],
        breakeven_after_tp1=False, stale_no_volume_exit_s=0, moonbag_retain_pct=0,
    )


def sol_chain(jupiter: Layer, gmgn: Layer):
    """The production sol route, shape for shape: venue routing, then GMGN; the DEX pool
    reader and the executable router sit behind the curve source's fallback."""
    from kaiba.core.db import get_conn
    from kaiba.execution.curve_price import CurvePriceSource

    curve = CurvePriceSource(get_conn(), sol_usd=Decimal("120"),
                             fallback=wd.FallbackPriceSource(wd.ProviderPriceSource(), jupiter,
                                                             name="sol-after-curve"))
    routed = ChainRoutedPriceSource({Chain.SOL: curve})
    return wd.FallbackPriceSource(routed, gmgn, name="venue+gmgn")


@pytest.fixture
def cozy(tmp_db):
    make_position(tmp_db, token=COZY, entry="1.0", mode=LaneMode.LIVE, lane=Lane.SM_TRENCHES)
    tmp_db.execute(
        "INSERT INTO tokens (chain,address,decimals,launchpad,migrated_ms,first_seen_ms) VALUES (?,?,?,?,?,?)",
        (Chain.SOL.value, COZY, 6, "ray_launchpad", now_ms() - 600_000, now_ms()),
    )
    return tmp_db


def dog(conn, source, *, submitter=None, chains=None):
    return wd.Watchdog(conn, price_source=source, submitter=submitter or RecordingSubmitter(),
                       cfg_provider=cfg, settlement_chains=chains)


# --------------------------------------------------------------------------------------
# pool selection
# --------------------------------------------------------------------------------------


def snapshots(rows):
    return [p for p in (ds._parse_pair(r) for r in rows) if p is not None]


def test_a_dust_native_pool_never_beats_the_deep_one():
    """COZY: choosing 'the deepest SOL pool' would have priced it 200x low."""
    rows = [pair(COZY, BONK, "Bonk", "0.0000146", "20081.37", BONK_POOL),
            pair(COZY, SOL_NATIVE_MINT, "SOL", "0.0000000688", "189.55", SOL_DLMM),
            pair(COZY, SOL_NATIVE_MINT, "SOL", "0.0000502", "1.98", SOL_DYN2)]
    chosen = prices.pick_pair(snapshots(rows), COZY, prefer_quotes={SOL_NATIVE_MINT})
    assert chosen is not None and chosen.pair_address == BONK_POOL


@pytest.mark.parametrize("native_liq,expected", [
    ("2000", "native"),      # >= $1,000 and >= 25% of $8,000: a market
    ("1999", "deep"),        # under 25% of the deepest pool
    ("999", "deep"),         # under the thin-pool floor
])
def test_a_native_pool_is_preferred_only_when_it_is_a_market(native_liq, expected):
    rows = [pair(COZY, BONK, "Bonk", "1.0", "8000", BONK_POOL),
            pair(COZY, SOL_NATIVE_MINT, "SOL", "1.0", native_liq, SOL_DLMM)]
    if native_liq == "999":
        rows[0]["liquidity"]["usd"] = "1000"
    chosen = prices.pick_pair(snapshots(rows), COZY, prefer_quotes={SOL_NATIVE_MINT})
    assert chosen.pair_address == (SOL_DLMM if expected == "native" else BONK_POOL)


def test_without_a_preference_the_deepest_pool_still_wins():
    rows = [pair(COZY, BONK, "Bonk", "1.0", "8000", BONK_POOL),
            pair(COZY, SOL_NATIVE_MINT, "SOL", "1.0", "7000", SOL_DLMM)]
    assert prices.pick_pair(snapshots(rows), COZY).pair_address == BONK_POOL


def test_the_provider_quote_names_the_asset_the_pool_is_quoted_in(tmp_db, monkeypatch):
    cozy_pools(monkeypatch, "0.0000146")
    got = prices.quote(Chain.SOL, COZY, conn=tmp_db)
    assert got.quote_address == BONK and got.quote_symbol == "Bonk"


# --------------------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------------------


def test_the_provider_source_classifies_pool_marks(tmp_db, monkeypatch):
    cozy_pools(monkeypatch, "0.0000146")
    foreign = wd.ProviderPriceSource().quote(Chain.SOL, COZY)
    assert foreign.usable and foreign.settlement == wd.SETTLE_FOREIGN_POOL
    assert foreign.quote_symbol == "Bonk" and wd.is_foreign_mark(foreign)

    dex(monkeypatch, [pair(COZY, SOL_NATIVE_MINT, "SOL", "0.0000146", "50000", SOL_DLMM)])
    native = wd.ProviderPriceSource().quote(Chain.SOL, COZY)
    assert native.settlement == wd.SETTLE_NATIVE_POOL and not wd.is_foreign_mark(native)

    # Pricing the native asset itself off SOL/USDC is the right pool (min_out depends on it).
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    dex(monkeypatch, [pair(SOL_NATIVE_MINT, usdc, "USDC", "120", "9000000", SOL_DYN2)])
    assert wd.ProviderPriceSource().quote(Chain.SOL, SOL_NATIVE_MINT).settlement == wd.SETTLE_NATIVE_POOL


@pytest.mark.parametrize("quote_asset,native", [
    (wd.EVM_ZERO, True),                 # robinhood "ETH" pools: Uniswap v4 native
    (RH_WETH.upper().replace("0X", "0x"), True),
    (RH_META, False),
])
def test_robinhood_native_assets_are_measured_spellings(tmp_db, monkeypatch, quote_asset, native):
    dex(monkeypatch, [pair(RH_TOKEN, quote_asset, "X", "0.5", "40000", "0x" + "1" * 40, chain="robinhood")])
    got = wd.ProviderPriceSource().quote(Chain.ROBINHOOD, RH_TOKEN)
    assert (got.settlement == wd.SETTLE_NATIVE_POOL) is native


@pytest.mark.parametrize("source,expected", [
    ("curve:pumpfun", wd.SETTLE_CURVE), ("evm-venue:pons", wd.SETTLE_CURVE),
    ("jupiter:Raydium CP", wd.SETTLE_EXECUTABLE), ("pancake-v2:wbnb", wd.SETTLE_EXECUTABLE),
    ("gmgn:token.info", wd.SETTLE_AGGREGATOR), ("fake", None),
])
def test_settlement_by_source(source, expected):
    assert wd.settlement_of(wd.PriceQuote(price_usd=Decimal(1), source=source)) == expected


@pytest.mark.parametrize("raw,expected", [
    (None, {Chain.SOL}),
    (["sol", "robinhood"], {Chain.SOL, Chain.ROBINHOOD}),
    ("sol, bsc", {Chain.SOL, Chain.BSC}),
    ([], set()),
    (["robinhood", "nonsense"], {Chain.SOL}),   # a typo falls back to the default, not to a guess
    (7, {Chain.SOL}),
])
def test_configured_settlement_chains(monkeypatch, raw, expected):
    monkeypatch.setattr(wd, "_protection_setting", lambda key: raw if key == "settlement_mark_chains" else None)
    assert wd.configured_settlement_mark_chains() == frozenset(expected)


# --------------------------------------------------------------------------------------
# decisions on sol (enforcing by default)
# --------------------------------------------------------------------------------------


def test_cozy_tp1_does_not_fire_on_the_bonk_pool(cozy, monkeypatch):
    """The journal case: tp1 fired on a +141% Bonk-pool mark (live, it filled at +48%).
    Here the executable layer says -17%, so nothing may sell."""
    jupiter, gmgn = Layer("jupiter:Raydium CP+Whirlpool", "1.00"), Layer("gmgn:token.info", "1.00")
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "1.10")
    assert watchdog.tick().exits == 0

    cozy_pools(monkeypatch, "2.41")
    jupiter.price = "0.83"
    report = watchdog.tick()

    assert report.exits == 0 and submitter.calls == []
    assert gmgn.calls == 0, "the executable layer answered; GMGN must not be paid for"
    noted = [e for e in events_named(cozy, "mark_set_aside") if e["foreign_alone_would_sell"]]
    assert noted and noted[0]["foreign_alone_reason"].startswith("tp1")
    assert noted[0]["foreign"]["quote_symbol"] == "Bonk"
    assert noted[0]["used"]["settlement"] == wd.SETTLE_EXECUTABLE


def test_a_real_stop_still_fires_on_the_executable_mark(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", None)
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "1.00")
    watchdog.tick()

    cozy_pools(monkeypatch, "1.50")       # the foreign pool says +50%
    jupiter.price = "0.60"                 # a sell would realise -40%
    assert watchdog.tick().exits == 1
    assert submitter.calls[0][2] == "stop_loss"
    sent = events_named(cozy, "exit_submitted")[0]
    assert sent["quote_provenance"]["source"].startswith("jupiter:")
    assert sent["set_aside_quote"]["quote_symbol"] == "Bonk"


def test_a_false_stop_on_the_foreign_pool_does_not_sell(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", None)
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "1.00")
    watchdog.tick()

    cozy_pools(monkeypatch, "0.55")
    jupiter.price = "0.95"
    assert watchdog.tick().exits == 0 and submitter.calls == []


def test_nothing_settled_means_blind_not_a_decision_on_the_foreign_pool(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", None), Layer("gmgn:token.info", None)
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "0.55")       # a -45% mark nobody can corroborate
    report = watchdog.tick()

    assert report.exits == 0 and report.blind == 1 and submitter.calls == []
    assert jupiter.calls == 1 and gmgn.calls == 1
    blind = events_named(cozy, "protection_blind")[0]
    assert "mark_not_settled_in_native" in blind["reason"]
    assert blind["rejected_quote"]["quote_symbol"] == "Bonk"
    assert blind["stops_evaluated"] is False


def test_a_later_layer_that_is_also_a_foreign_pool_is_not_settlement(cozy, monkeypatch):
    """Only a layer that settles natively may replace the mark, whatever its name."""
    pooled = Layer("jupiter:Raydium CP", "0.95", settlement=wd.SETTLE_FOREIGN_POOL)
    gmgn = Layer("gmgn:token.info", "0.62")
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(pooled, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "0.55")
    first = watchdog._quote(next(iter(watchdog._open_positions())))
    assert first.source == "gmgn:token.info" and pooled.calls == 1 and gmgn.calls == 1


def test_a_native_pool_mark_decides_without_asking_further(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", "1.00")
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    dex(monkeypatch, [pair(COZY, SOL_NATIVE_MINT, "SOL", "1.0", "60000", SOL_DLMM)])
    watchdog.tick()
    dex(monkeypatch, [pair(COZY, SOL_NATIVE_MINT, "SOL", "0.6", "60000", SOL_DLMM)])
    assert watchdog.tick().exits == 1
    assert jupiter.calls == 0 and gmgn.calls == 0


def test_corroboration_never_comes_from_a_foreign_pool(cozy, monkeypatch):
    """A first-sight stop on a NEW executable route asks the chain for a second opinion
    (`_validate_quote`). The foreign pool is in that chain; it must not be the opinion."""
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", None)
    submitter = RecordingSubmitter()
    watchdog = dog(cozy, sol_chain(jupiter, gmgn), submitter=submitter)
    cozy_pools(monkeypatch, "1.00")
    watchdog.tick()

    jupiter.source = "jupiter:Orca"       # a route identity we have not seen
    jupiter.price = "0.60"
    cozy_pools(monkeypatch, "1.50")        # the foreign pool would 'corroborate' a hold
    report = watchdog.tick()
    assert report.exits == 0 and report.blind == 1
    assert "uncorroborated" in events_named(cozy, "protection_blind")[0]["reason"]


def test_a_vetoed_paper_position_is_blind_not_abandoned(tmp_db, monkeypatch):
    """A shadow position older than the abandonment age HAS a price -- just not one an exit
    may be decided on. Closing it as 'unpriceable' would be a fiction on the paper record."""
    make_position(tmp_db, token=COZY, entry="1.0", mode=LaneMode.SHADOW, lane=Lane.SM_TRENCHES)
    tmp_db.execute("UPDATE positions SET opened_ms=?", (now_ms() - (wd.SHADOW_BLIND_ABANDON_S + 60) * 1000,))
    tmp_db.execute(
        "INSERT INTO tokens (chain,address,decimals,launchpad,migrated_ms,first_seen_ms) VALUES (?,?,?,?,?,?)",
        (Chain.SOL.value, COZY, 6, "ray_launchpad", now_ms() - 600_000, now_ms()),
    )
    watchdog = dog(tmp_db, sol_chain(Layer("jupiter:Raydium CP", None), Layer("gmgn:token.info", None)))
    cozy_pools(monkeypatch, "0.9")
    report = watchdog.tick()
    assert report.blind == 1
    row = tmp_db.execute("SELECT closed_ms FROM positions WHERE position_id='pos_1'").fetchone()
    assert row[0] is None
    assert not events_named(tmp_db, "shadow_position_abandoned")


def test_a_would_be_sale_on_the_foreign_pool_is_recorded_once_per_reason(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", None)
    watchdog = dog(cozy, sol_chain(jupiter, gmgn))
    cozy_pools(monkeypatch, "2.41")
    for _ in range(3):
        watchdog.tick()
    noted = events_named(cozy, "mark_set_aside")
    assert len(noted) == 1 and noted[0]["foreign_alone_would_sell"]


def test_a_shadow_position_does_not_spend_past_the_tick_deadline(cozy, monkeypatch):
    jupiter, gmgn = Layer("jupiter:Raydium CP", "1.00"), Layer("gmgn:token.info", "1.00")
    watchdog = dog(cozy, sol_chain(jupiter, gmgn))
    cozy_pools(monkeypatch, "1.00")
    foreign = watchdog.price_source.quote(Chain.SOL, COZY)
    assert wd.is_foreign_mark(foreign)
    watchdog._quote_deadline = 0.0          # long past
    assert watchdog._settle(watchdog.price_source, Chain.SOL, COZY, foreign, may_overrun=False) is foreign
    assert jupiter.calls == 0
    settled = watchdog._settle(watchdog.price_source, Chain.SOL, COZY, foreign, may_overrun=True)
    assert settled.source.startswith("jupiter:") and jupiter.calls == 1


# --------------------------------------------------------------------------------------
# robinhood: unchanged unless enabled (see the measurement in watchdog.py)
# --------------------------------------------------------------------------------------


@pytest.fixture
def rh_position(tmp_db):
    make_position(tmp_db, token=RH_TOKEN, chain=Chain.ROBINHOOD, entry="1.0",
                  mode=LaneMode.LIVE, lane=Lane.SM_TRENCHES)
    tmp_db.execute("INSERT INTO tokens (chain,address,decimals,first_seen_ms) VALUES (?,?,?,?)",
                   (Chain.ROBINHOOD.value, RH_TOKEN, 18, now_ms()))
    return tmp_db


def rh_meta_pool(monkeypatch, price) -> None:
    dex(monkeypatch, [pair(RH_TOKEN, RH_META, "META", price, "40000", "0x" + "6" * 40, chain="robinhood")])


def test_robinhood_foreign_pool_still_decides_by_default(rh_position, monkeypatch):
    submitter = RecordingSubmitter()
    watchdog = dog(rh_position, wd.FallbackPriceSource(wd.ProviderPriceSource(), Layer("gmgn:token.info", None)),
                   submitter=submitter)
    assert Chain.ROBINHOOD not in watchdog.settlement_chains()
    rh_meta_pool(monkeypatch, "1.0")
    watchdog.tick()
    rh_meta_pool(monkeypatch, "0.6")
    assert watchdog.tick().exits == 1


def test_robinhood_can_be_switched_on(rh_position, monkeypatch):
    submitter = RecordingSubmitter()
    gmgn = Layer("gmgn:token.info", None, chain=Chain.ROBINHOOD)
    watchdog = dog(rh_position, wd.FallbackPriceSource(wd.ProviderPriceSource(), gmgn),
                   submitter=submitter, chains={Chain.SOL, Chain.ROBINHOOD})
    rh_meta_pool(monkeypatch, "0.6")
    report = watchdog.tick()
    assert report.exits == 0 and report.blind == 1 and gmgn.calls == 1
