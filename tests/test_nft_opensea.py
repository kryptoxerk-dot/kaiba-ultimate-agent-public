"""OpenSea drops as the NFT hunter's working source, and the dead sources it replaced.

Fixture: real keyless responses captured 2026-10-02 (``tests/fixtures/hunters/opensea_drops.json``).
Every test is offline; the network functions are replaced by stubs that count calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain, EventKind
from kaiba.hunters import airdrops, listings, nft

FIXTURE = Path(__file__).parent / "fixtures" / "hunters" / "opensea_drops.json"
AT = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)
ETH_USD = Decimal("2700")


def body():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["body"]


def by_slug(mints):
    return {m.collection: m for m in mints}


def test_parses_live_public_stages_on_funded_and_evm_chains_only(tmp_db):
    b = body()
    mints = by_slug(nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=ETH_USD,
                                      conn=tmp_db, at_ms=AT))
    # ape_chain is not a chain any Kaiba wallet holds; rare-friends-genesis has no stage left.
    assert set(mints) == {"officialmooncats", "robbin-hood-2026", "early-men-origins", "jpeg-frens",
                          "ottoclubhouse"}
    moon = mints["officialmooncats"]
    assert moon.chain is Chain.ROBINHOOD and moon.public_phase is True
    assert moon.mint_price_native == Decimal("0.01") and moon.mint_price_usd == Decimal("27.00")
    assert moon.recent_floor_usd == Decimal("40.50")  # 0.015 ETH x 2700
    assert moon.launch_ms < AT < moon.end_ms
    assert moon.meta["stage_state"] == "open" and moon.meta["is_minting"] is True
    assert moon.meta["sales_total"] == 231 and moon.meta["sales_1d"] == 0
    assert moon.gas_cost_usd == Decimal("0.10")  # robinhood gas; used to raise KeyError
    assert moon.url == "https://opensea.io/collection/officialmooncats"
    # A floor quoted in Robinhood's USDG stablecoin is already dollars.
    assert mints["robbin-hood-2026"].recent_floor_usd == Decimal("77.00")
    # Upcoming presale: not public, no stats call spent on it, free mint priced at zero.
    otto = mints["ottoclubhouse"]
    assert otto.meta["stage_state"] == "upcoming" and otto.public_phase is False
    assert otto.meta["stats_state"] == "not_open" and otto.mint_price_usd == Decimal("0.00")
    row = dict(tmp_db.execute("SELECT * FROM hunter_sources WHERE name='opensea'").fetchone())
    assert row["last_count"] == 5 and row["fail_streak"] == 0


def test_an_ask_nobody_ever_filled_is_not_a_floor(tmp_db):
    b = body()
    b["stats"]["officialmooncats"]["total"]["sales"] = 0
    mints = by_slug(nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=ETH_USD, at_ms=AT))
    assert mints["officialmooncats"].recent_floor_usd is None
    assert mints["officialmooncats"].meta["floor_price"] == "0.015"  # the fact is kept, unpriced


def test_without_an_eth_price_nothing_is_dollarised(tmp_db):
    b = body()
    mints = by_slug(nft.opensea_drops(raw=b["drops"], stats=b["stats"], conn=tmp_db, at_ms=AT))
    moon = mints["officialmooncats"]
    assert moon.mint_price_native == Decimal("0.01") and moon.mint_price_usd is None
    assert moon.recent_floor_usd is None
    assert mints["robbin-hood-2026"].recent_floor_usd == Decimal("77.00")  # USDG needs no FX


def test_a_stale_eth_sample_is_not_used(tmp_db):
    tmp_db.execute("INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES ('eth', ?, '2700', 't')",
                   (nft.now_ms() - nft.NATIVE_PRICE_MAX_AGE_MS - 60_000,))
    assert nft._eth_usd(tmp_db) is None
    tmp_db.execute("INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES ('eth', ?, '2650', 't')",
                   (nft.now_ms() - 60_000,))
    assert nft._eth_usd(tmp_db) == Decimal("2650")


def test_live_sweep_paces_calls_caps_stats_and_prioritises_robinhood(tmp_db, monkeypatch):
    b = body()
    lists, stats_calls, sleeps = [], [], []

    def fake_fetch_json(provider, endpoint, url, **kw):
        assert provider == "opensea" and "type=" in url  # a bare /drops answers 401
        lists.append(url)
        return b["drops"][url.split("type=")[1].split("&")[0]]

    def fake_stats(slug, conn=None):
        stats_calls.append(slug)
        return b["stats"].get(slug), "ok"

    monkeypatch.setattr(nft, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(nft, "fetch_opensea_stats", fake_stats)
    monkeypatch.setattr(nft, "OPENSEA_MAX_STATS", 2)
    mints = by_slug(nft.opensea_drops(conn=tmp_db, eth_usd=ETH_USD, at_ms=AT, sleep=sleeps.append))
    assert len(lists) == 3
    # Robinhood first, the one actually minting first among those, then by slug; capped at 2.
    assert stats_calls == ["officialmooncats", "early-men-origins"]
    assert mints["robbin-hood-2026"].meta["stats_state"] == "skipped_cap"
    assert mints["jpeg-frens"].recent_floor_usd is None
    # Every call after the first waits its turn: the limiter refuses rather than waits.
    assert sleeps == [nft.OPENSEA_PACE_S] * (len(lists) + len(stats_calls) - 1)


def test_every_list_failing_is_a_source_error(tmp_db, monkeypatch):
    monkeypatch.setattr(nft, "fetch_json", lambda *a, **k: None)
    assert nft.opensea_drops(conn=tmp_db, at_ms=AT, sleep=lambda s: None) == []
    row = dict(tmp_db.execute("SELECT * FROM hunter_sources WHERE name='opensea'").fetchone())
    assert row["fail_streak"] == 1 and "unavailable" in row["last_error"]


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_stats_401_is_a_documented_gate_not_a_provider_error(tmp_db, monkeypatch):
    import contextlib

    # Isolate status handling from the limiter's 1 s minimum interval (the live sweep paces
    # itself; three back-to-back calls here would otherwise be refused, not answered).
    monkeypatch.setattr(nft, "guarded", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(nft.httpx, "get", lambda *a, **k: _Resp(401, {"errors": ["Missing an API Key"]}))
    assert nft.fetch_opensea_stats("cheap-shot", tmp_db) == (None, "needs_api_key")
    errors = tmp_db.execute("SELECT COUNT(*) FROM events WHERE kind=?", (EventKind.PROVIDER_ERROR.value,))
    assert errors.fetchone()[0] == 0
    monkeypatch.setattr(nft.httpx, "get", lambda *a, **k: _Resp(500))
    assert nft.fetch_opensea_stats("cheap-shot", tmp_db) == (None, "unavailable")
    errors = tmp_db.execute("SELECT COUNT(*) FROM events WHERE kind=?", (EventKind.PROVIDER_ERROR.value,))
    assert errors.fetchone()[0] == 1
    monkeypatch.setattr(nft.httpx, "get", lambda *a, **k: _Resp(200, body()["stats"]["jpeg-frens"]))
    got, state = nft.fetch_opensea_stats("jpeg-frens", tmp_db)
    assert state == "ok" and got["total"]["floor_price_symbol"] == "ETH"


def test_default_sweep_is_opensea_not_the_stale_launchpad(tmp_db, monkeypatch):
    monkeypatch.setattr(nft, "magic_eden_launchpad", lambda **k: pytest.fail("ME left the sweep"))
    b = body()
    real = nft.opensea_drops
    monkeypatch.setattr(nft, "opensea_drops", lambda conn=None: real(
        raw=b["drops"], stats=b["stats"], eth_usd=ETH_USD, conn=conn, at_ms=AT))
    report = nft.refresh_report(tmp_db, force=True)
    assert set(report["sources"]) == {"opensea", "helius_mint_watch"}
    assert report["sources"]["opensea"]["state"] == "ok" and report["written"] == 5


def test_opensea_rows_are_stored_refused_with_their_evidence(tmp_db):
    b = body()
    mints = nft.opensea_drops(raw=b["drops"], stats=b["stats"], eth_usd=ETH_USD, at_ms=AT)
    assert nft.refresh(tmp_db, mints=mints, force=True) == len(mints)
    rows = {r["name"]: dict(r) for r in tmp_db.execute("SELECT * FROM opportunities WHERE kind='nft_mint'")}
    robbin = rows["ROBBIN HOOD"]
    # Research for the owner only: Kaiba has no EVM mint route, so automation refuses it.
    assert robbin["status"] == "refused" and robbin["chain"] == "robinhood"
    meta = json.loads(robbin["evidence_json"])["meta"]
    assert meta["recent_floor_usd"] == "77.00" and meta["mint_price_usd"] == "27.00"
    assert meta["public_phase"] is True and meta["stage_state"] == "open"
    assert "unsupported NFT mint execution route" in json.loads(robbin["warnings_json"])[-1]


# ------------------------------------------------------------------ retired sources


def test_cryptolisting_left_the_listing_sweep(tmp_db, monkeypatch):
    assert "cryptolisting" not in listings.FEEDS
    assert listings.cryptolisting_feed(json.loads(
        (FIXTURE.parent / "cryptolisting_feed.json").read_text(encoding="utf-8"))["body"], conn=tmp_db)


def test_box_blocked_exchange_pages_are_not_requested_by_default(tmp_db, monkeypatch):
    asked = []
    monkeypatch.setattr(listings, "fetch_json", lambda name, ep, url, **k: asked.append(name) or None)
    monkeypatch.setattr(listings, "fetch_text", lambda name, ep, url, **k: asked.append(name) or None)
    listings.exchange_announcements(conn=tmp_db)
    assert asked == ["binance"]
    assert set(listings.DISABLED_ANNOUNCEMENT_SOURCES) == {"upbit", "bithumb"}
    # A supplied body for a retired page still parses: the parser was not removed.
    upbit = json.loads((FIXTURE.parent / "upbit_announcements.json").read_text(encoding="utf-8"))["body"]
    assert listings.exchange_announcements({"upbit": upbit}, conn=tmp_db)


# ------------------------------------------------------------------ airdrop text and chains


def test_robinhood_chain_is_detected_before_its_arbitrum_parent():
    assert airdrops.detect_chain("Points live on Robinhood Chain, built on Arbitrum Orbit") == (
        Chain.ROBINHOOD, "robinhood")
    assert airdrops.detect_chain("investment from Robinhood Crypto")[0] is None
    assert airdrops.gas_estimate(Chain.ROBINHOOD) == Decimal("0.10")


def test_defillama_sets_the_official_url_without_changing_the_key():
    rows = [{"name": "Tokenless Vault", "symbol": "-", "tvl": 5_000_000, "url": "https://vault.example",
             "chains": ["Ethereum"]}]
    opp, = airdrops.from_defillama(rows)
    assert opp.official_url == opp.url == "https://vault.example"
    legacy = opp.model_copy(update={"official_url": None})
    assert opp.key == legacy.key
