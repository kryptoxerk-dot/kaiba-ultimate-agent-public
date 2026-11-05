"""The CoinGecko fallback in :mod:`kaiba.providers.native_price` (2026-10-21).

Order is DexScreener, then GMGN, then CoinGecko. The fixture is a real ``/simple/price``
answer taken from the box on 2026-10-21 with the configured demo key (key not recorded).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.providers import native_price as npx
from kaiba.providers._http import Fetched

FIXTURE = Path(__file__).parent / "fixtures" / "coingecko" / "simple_price_sol_eth_bnb.json"
UPDATED_S = 1792573844
SECRET = "CG-testsecretvalue0123456789"


def cg_body() -> Any:
    return json.loads(FIXTURE.read_text(encoding="utf-8"), parse_float=Decimal)


def ok(provider: str, data: Any, observed_at_ms: int) -> Fetched:
    return Fetched(data, Receipt(provider=provider, endpoint="x", observed_at_ms=observed_at_ms,
                                 basis=EvidenceBasis.PROVIDER_REPORTED))


def down(provider: str = "dexscreener", note: str = "rate limited: bucket exhausted") -> Fetched:
    return Fetched(None, Receipt(provider=provider, endpoint="x", basis=EvidenceBasis.UNAVAILABLE, note=note))


@pytest.fixture
def rig(monkeypatch):
    """DexScreener and GMGN down; CoinGecko answers from the fixture unless told otherwise."""
    from kaiba.core.config import get_settings

    monkeypatch.setenv("COINGECKO_API_KEY", SECRET)
    get_settings.cache_clear()
    state: dict[str, Any] = {"calls": [], "cg": ok("coingecko", cg_body(), UPDATED_S * 1000 + 2_000)}

    def fake(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        state["calls"].append({"provider": provider, "endpoint": endpoint, "url": url, **kw})
        if provider == "dexscreener":
            return state.get("ds", down())
        return state["cg"]

    def gmgn_down(address, chain):
        raise RuntimeError("gmgn down")

    monkeypatch.setattr(npx, "get_json", fake)
    monkeypatch.setattr(npx, "_gmgn_token_info", gmgn_down)
    return state


@pytest.mark.parametrize(("chain", "cg_id", "usd"), [
    (Chain.SOL, "solana", "106.68849009620193"),
    (Chain.ETH, "ethereum", "2455.6154591885334"),
    (Chain.BASE, "ethereum", "2455.6154591885334"),
    (Chain.BSC, "binancecoin", "746.1309694976243"),
])
def test_coingecko_answers_when_both_other_sources_are_down(tmp_db, rig, chain, cg_id, usd):
    got, receipt = npx.fetch(chain, conn=tmp_db)
    assert got is not None and got.price_usd == Decimal(usd)
    assert got.source == "coingecko" and got.pair is None and got.liquidity_usd is None
    assert got.ts_ms == UPDATED_S * 1000, "stamped with CoinGecko's own update time"
    call = rig["calls"][-1]
    assert call["provider"] == "coingecko", "its own limiter bucket"
    assert call["params"]["ids"] == cg_id
    assert call["headers"] == {"x-cg-demo-api-key": SECRET}
    assert call["ttl_s"] == 0
    assert SECRET not in (receipt.note or "")


def test_a_healthy_dexscreener_never_reaches_coingecko(tmp_db, rig):
    fixture = Path(__file__).parent / "fixtures" / "dexscreener" / "token_pairs_solana_wsol.json"
    rig["ds"] = ok("dexscreener", json.loads(fixture.read_text(encoding="utf-8"), parse_float=Decimal), 1)
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is not None and got.source == "dexscreener"
    assert [c["provider"] for c in rig["calls"]] == ["dexscreener"]


def test_gmgn_answering_means_coingecko_is_not_asked(tmp_db, rig, monkeypatch):
    monkeypatch.setattr(npx, "_gmgn_token_info",
                        lambda a, c: type("R", (), {"data": {"price": {"price": "118.14"}}})())
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is not None and got.source == "gmgn"
    assert all(c["provider"] != "coingecko" for c in rig["calls"])


def test_all_three_down_is_none_with_dexscreeners_reason(tmp_db, rig):
    rig["cg"] = down("coingecko", "HTTP 429")
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "bucket exhausted" in (receipt.note or "")


@pytest.mark.parametrize("bad", [0, -1, None, "abc"])
def test_an_unusable_coingecko_price_is_refused(tmp_db, rig, bad):
    body = cg_body()
    body["solana"]["usd"] = bad
    rig["cg"] = ok("coingecko", body, UPDATED_S * 1000)
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None


def test_a_stale_coingecko_price_is_refused(tmp_db, rig):
    rig["cg"] = ok("coingecko", cg_body(), (UPDATED_S + npx.COINGECKO_MAX_AGE_S + 1) * 1000)
    got, receipt = npx._fetch_via_coingecko(Chain.SOL, conn=tmp_db)
    assert got is None and "last updated" in (receipt.note or "")


def test_keyless_sends_no_key_header(tmp_db, rig, monkeypatch):
    from kaiba.core.config import get_settings

    monkeypatch.setenv("COINGECKO_API_KEY", "")
    get_settings.cache_clear()
    assert npx.fetch(Chain.SOL, conn=tmp_db)[0] is not None
    assert rig["calls"][-1]["headers"] is None


def test_robinhood_still_has_no_direct_source(tmp_db, rig):
    """Robinhood reads eth samples through NATIVE_PRICE_ALIAS; nothing here changes that."""
    got, _ = npx.fetch(Chain.ROBINHOOD, conn=tmp_db)
    assert got is None
    assert all(c["provider"] != "coingecko" for c in rig["calls"])
