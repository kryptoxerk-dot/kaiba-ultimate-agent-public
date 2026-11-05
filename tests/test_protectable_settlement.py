"""The entry probe asks the question protection will: is there a mark that settles natively?

2026-10-03: protection on sol now refuses to decide on a DexScreener pool quoted in a third
asset (``protection.settlement_mark_chains``; journal #5048, COZY's Bonk pool). Before this
file, ``viability.quote_asset_is_protectable`` accepted that same foreign mark as
"protectable" -- so a token whose only price was such a pool could be ENTERED and then sit
blind under protection, and a live position blind past ``max_blind_s`` halts entries on
every chain. The probe now settles the mark through the same layers protection asks
(``watchdog.settle_foreign_mark``) and refuses when none answers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import viability as V
from kaiba.execution import watchdog as wd
from tests.test_watchdog_settlement_marks import COZY, RH_TOKEN, Layer, cozy_pools, dex, pair, sol_chain


@pytest.fixture(autouse=True)
def _clean():
    V.reset_protectable_cache()
    yield
    V.reset_protectable_cache()


def wire(monkeypatch, source, chains=frozenset({Chain.SOL})):
    monkeypatch.setattr(wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: source)
    monkeypatch.setattr(wd, "configured_settlement_mark_chains", lambda *a, **k: frozenset(chains))


def test_a_token_priced_only_by_a_foreign_pool_is_not_protectable(tmp_db, monkeypatch):
    cozy_pools(monkeypatch, "0.0000146")
    jupiter, gmgn = Layer("jupiter:route"), Layer("gmgn:token_info")
    wire(monkeypatch, sol_chain(jupiter, gmgn))
    ok, why = V.quote_asset_is_protectable(Chain.SOL, COZY)
    assert (ok, why) == (False, "protection_mark_not_settled_in_native")
    assert jupiter.calls == 1 and gmgn.calls == 1  # it asked every layer protection would


def test_an_executable_quote_settles_it(tmp_db, monkeypatch):
    cozy_pools(monkeypatch, "0.0000146")
    jupiter, gmgn = Layer("jupiter:route", "0.0000120"), Layer("gmgn:token_info")
    wire(monkeypatch, sol_chain(jupiter, gmgn))
    assert V.quote_asset_is_protectable(Chain.SOL, COZY) == (True, "protectable")
    assert gmgn.calls == 0  # first native answer wins, in protection's order


def test_gmgn_settles_it_when_jupiter_refuses(tmp_db, monkeypatch):
    cozy_pools(monkeypatch, "0.0000146")
    jupiter, gmgn = Layer("jupiter:route"), Layer("gmgn:token_info", "0.0000118")
    wire(monkeypatch, sol_chain(jupiter, gmgn))
    assert V.quote_asset_is_protectable(Chain.SOL, COZY) == (True, "protectable")


def test_the_refusal_follows_the_protection_setting(tmp_db, monkeypatch):
    """Off for sol in config -> protection decides on the foreign mark, so it is protectable."""
    cozy_pools(monkeypatch, "0.0000146")
    jupiter, gmgn = Layer("jupiter:route"), Layer("gmgn:token_info")
    wire(monkeypatch, sol_chain(jupiter, gmgn), chains=frozenset())
    assert V.quote_asset_is_protectable(Chain.SOL, COZY) == (True, "protectable")
    assert jupiter.calls == 0


def test_a_native_pool_mark_is_untouched(tmp_db, monkeypatch):
    from kaiba.core.schemas import SOL_NATIVE_MINT

    dex(monkeypatch, [pair(COZY, SOL_NATIVE_MINT, "SOL", "0.0000146", "50000", "PoolNative1111")])
    jupiter, gmgn = Layer("jupiter:route"), Layer("gmgn:token_info")
    wire(monkeypatch, sol_chain(jupiter, gmgn))
    assert V.quote_asset_is_protectable(Chain.SOL, COZY) == (True, "protectable")
    assert jupiter.calls == 0 and gmgn.calls == 0


def test_evm_probes_ask_about_the_quote_asset_and_are_not_settled(monkeypatch):
    """On EVM the probe is asked about the QUOTE asset, whose USD read never meets the rule."""
    foreign = wd.PriceQuote(price_usd=Decimal("1.0"),
                            basis=wd.EvidenceBasis.PROVIDER_REPORTED, source="dexscreener:pair",
                            chain=Chain.ROBINHOOD, token=RH_TOKEN, observed_ms=wd.now_ms(),
                            settlement=wd.SETTLE_FOREIGN_POOL)

    class _Src:
        def quote(self, chain, token):
            return foreign

    wire(monkeypatch, _Src(), chains=frozenset({Chain.SOL, Chain.ROBINHOOD}))
    assert V.quote_asset_is_protectable(Chain.ROBINHOOD, RH_TOKEN) == (True, "protectable")
