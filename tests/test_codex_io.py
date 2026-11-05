"""Offline tests for the Codex.io adapter and its monthly compute-unit guard.

Every fixture under ``tests/fixtures/codex_io/`` is a real response recorded from the box
on 2026-10-21 (free plan). The only edit is the ``usageData`` block: the probes combined
several root fields per request, so each fixture carries that field's share of the bill,
computed from the real per-resource breakdown (noted in each file's ``_recorded``).

The guard tests matter most. The plan has 10,000 CU per rolling 30 days and no
auto-scale: at 100% every key is paused with HTTP 403. A guard that lets one request
through past the ceiling is how a research loop silently takes the key down for a month.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core.limiter import Limits
from kaiba.core.schemas import Chain
from kaiba.providers import codex_io as cx

FIXTURES = Path(__file__).parent / "fixtures" / "codex_io"
SECRET = "cxk_TESTSECRET_0123456789abcdefABCDEF"
PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
KIMCHI = "0xdb9264a5a1f9fd98a6dce9a01fa123bce3b3802f"
MAKER = "0xb9f5f4ea1af1f5d3678470eb98e8fbdcadeb24b0"


def fixture(name: str) -> dict[str, Any]:
    body = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    body.pop("_recorded", None)
    return body


class FakeCodex:
    """Stand-in for ``httpx.request``: answers by root field, records every request."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.raise_with: Exception | None = None

    def route(self, root: str, body: Any, status: int = 200) -> FakeCodex:
        self.routes[root] = (status, body)
        return self

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        self.calls.append({"method": method, "url": url, **kw})
        if self.raise_with is not None:
            raise self.raise_with
        query = (kw.get("json") or {}).get("query", "")
        request = httpx.Request(method, url)
        for root, (status, body) in self.routes.items():
            if f"{root}(" in query:
                return httpx.Response(status, json=body, request=request)
        return httpx.Response(404, json={"errors": [{"message": "unrouted"}]}, request=request)


@pytest.fixture
def codex(tmp_db, monkeypatch: pytest.MonkeyPatch) -> FakeCodex:
    from kaiba.core import limiter
    from kaiba.core.config import get_settings
    from kaiba.core.schemas import now_ms

    monkeypatch.setenv("CODEX_IO_API_KEY", SECRET)
    monkeypatch.delenv(cx.CEILING_ENV, raising=False)
    get_settings.cache_clear()
    fake = FakeCodex()
    monkeypatch.setattr(httpx, "request", fake)
    real = limiter.limits_for
    fast = Limits(min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0, max_inflight=16)
    monkeypatch.setattr(limiter, "limits_for", lambda p: fast if p == cx.PROVIDER else real(p))
    clock = {"ms": now_ms()}

    def ticking() -> int:
        clock["ms"] += 1_000
        return clock["ms"]

    monkeypatch.setattr(limiter, "now_ms", ticking)
    return fake


def errors(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM events WHERE kind='provider.error'").fetchall()
    return [json.loads(r[0]) for r in rows]


def spend(conn, cu: str, days_ago: int = 0) -> None:
    from kaiba.core.schemas import now_ms

    day = cx._day(now_ms() - days_ago * 86_400_000)
    cx._add_micro(conn, day, int(Decimal(cu) * cx.MICRO))


# --------------------------------------------------------------------------- parsing


def test_token_prices_parse_price_and_liquidity_per_network(codex, tmp_db):
    codex.route("getTokenPrices", fixture("get_token_prices"))
    got = cx.token_prices([
        (Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE]), (Chain.ETH, cx.WRAPPED_NATIVE[Chain.ETH]),
        (Chain.BSC, cx.WRAPPED_NATIVE[Chain.BSC]), (Chain.SOL, cx.WRAPPED_NATIVE[Chain.SOL]),
    ], conn=tmp_db)
    assert got is not None and len(got) == 4
    base = got[(8453, cx.WRAPPED_NATIVE[Chain.BASE])]
    assert base.price_usd == Decimal("2452.84031461")
    assert base.liquidity_usd == Decimal("87691805")
    assert base.ts_s == 1792573099 and base.block_number == 52891876
    assert got[(56, cx.WRAPPED_NATIVE[Chain.BSC])].price_usd == Decimal("745.824455228")
    sol = got[(1399811149, cx.WRAPPED_NATIVE[Chain.SOL].lower())]
    assert sol.price_usd == Decimal("106.608100082")


def test_price_returns_one_row_and_the_query_carries_usage_and_auth(codex, tmp_db):
    codex.route("getTokenPrices", fixture("get_token_prices"))
    got = cx.price(Chain.ETH, cx.WRAPPED_NATIVE[Chain.ETH], conn=tmp_db)
    assert got is not None and got.price_usd == Decimal("2452.82148648")
    call = codex.calls[-1]
    assert call["headers"]["Authorization"] == SECRET
    assert "usageData" in call["json"]["query"], "without usageData the quota counter is blind"
    assert SECRET not in call["json"]["query"]


def test_a_zero_or_missing_price_is_absent_not_zero(codex, tmp_db):
    body = fixture("get_token_prices")
    body["data"]["getTokenPrices"][0]["priceUsd"] = 0
    body["data"]["getTokenPrices"][1]["priceUsd"] = None
    codex.route("getTokenPrices", body)
    got = cx.token_prices([(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE])], conn=tmp_db)
    assert got is not None
    assert (8453, cx.WRAPPED_NATIVE[Chain.BASE]) not in got
    assert (1, cx.WRAPPED_NATIVE[Chain.ETH]) not in got


def test_token_pairs_sorts_deepest_pool_first(codex, tmp_db):
    codex.route("listPairsWithMetadataForToken", fixture("list_pairs_robinhood_pons"))
    pairs = cx.token_pairs(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db)
    assert pairs is not None and len(pairs) == 3
    assert [p.liquidity_usd for p in pairs] == [Decimal("1410665"), Decimal("1184420"), Decimal("412973")]
    top = pairs[0]
    assert top.protocol == "UniswapV4" and top.exchange == "Uniswap"
    assert top.backing_symbol == "USDG" and top.network_id == 4663


def test_trending_tokens_parse_robinhood_screen(codex, tmp_db):
    codex.route("filterTokens", fixture("filter_tokens_robinhood_trending"))
    got = cx.trending_tokens(Chain.ROBINHOOD, limit=3, conn=tmp_db)
    assert got is not None and [t.symbol for t in got] == ["WETH", "USDG", "PONS"]
    pons = got[2]
    assert pons.address == PONS and pons.holders == 103605
    assert pons.liquidity_usd == Decimal("5047300") and pons.creator == MAKER
    query = codex.calls[-1]["json"]["query"]
    assert "network: [4663]" in query and "attribute: trendingScore24" in query


def test_new_tokens_parse_base_screen(codex, tmp_db):
    codex.route("filterTokens", fixture("filter_tokens_base_new"))
    got = cx.new_tokens(Chain.BASE, min_liquidity_usd=5000, limit=3, conn=tmp_db)
    assert got is not None and got[0].address == KIMCHI
    assert got[0].token_created_at_s == 1792572311 and got[0].pair_created_at_s == 1792572331
    assert got[0].price_usd == Decimal("3.37918794296e-5")
    query = codex.calls[-1]["json"]["query"]
    assert "attribute: createdAt" in query and "liquidity: {gte: 5000.0}" in query


def test_token_events_parse_makers_and_the_priced_side(codex, tmp_db):
    codex.route("getTokenEvents", fixture("token_events_robinhood_pons"))
    page = cx.token_events(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db)
    assert page is not None and len(page.items) == 3 and page.cursor
    first = page.items[0]
    assert first.side == "Sell" and first.event_type == "Swap"
    assert first.token_address == PONS, "quoteToken token0 means token0 is the priced token"
    assert first.maker == "0x7274a84318536839e64de49144c4366cb8e06612"
    assert first.price_usd == Decimal("0.28159492469688688166")
    assert first.token_amount == Decimal("2002.061539600674062336")
    assert first.wallet_labels == ("BOT",) and not first.sandwich


def test_early_buyer_read_asks_oldest_buys_first(codex, tmp_db):
    codex.route("getTokenEvents", fixture("token_events_base_kimchi_buys"))
    page = cx.token_events(Chain.BASE, KIMCHI, sides=["Buy"], oldest_first=True, limit=3, conn=tmp_db)
    assert page is not None and all(e.side == "Buy" for e in page.items)
    assert page.items[0].token_address == KIMCHI, "quoteToken token1 means token1 is priced"
    assert page.items[0].maker == "0xea53e7f0f8ba771a64eacaea145ee41aa8fa4eb9"
    query = codex.calls[-1]["json"]["query"]
    assert "direction: ASC" in query and "eventDisplayType: [Buy]" in query


def test_maker_events_parse_one_wallets_swaps(codex, tmp_db):
    codex.route("getTokenEventsForMaker", fixture("maker_events_robinhood"))
    page = cx.maker_events(MAKER, chain=Chain.ROBINHOOD, limit=3, conn=tmp_db)
    assert page is not None and page.items
    assert {e.maker for e in page.items} == {MAKER}
    assert all(e.network_id == 4663 for e in page.items)


# -------------------------------------------------------------------------- the guard


def test_usage_is_settled_to_what_codex_billed(codex, tmp_db):
    codex.route("getTokenPrices", fixture("get_token_prices"))
    cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db)
    assert cx.quota_status(tmp_db).spent_cu == Decimal("4.018")


def test_the_guard_refuses_at_the_ceiling_and_sends_nothing(codex, tmp_db, monkeypatch):
    monkeypatch.setenv(cx.CEILING_ENV, "10")
    codex.route("getTokenPrices", fixture("get_token_prices"))
    spend(tmp_db, "8")  # 8 + a ~4.006 CU estimate > 10
    got = cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db)
    assert got is None
    assert codex.calls == [], "a refused request must not leave the box"
    assert any("quota guard" in e["detail"] for e in errors(tmp_db))
    assert cx.quota_status(tmp_db).spent_cu == Decimal(8), "a refusal charges nothing"


def test_positive_control_the_same_call_passes_under_the_ceiling(codex, tmp_db, monkeypatch):
    monkeypatch.setenv(cx.CEILING_ENV, "10")
    codex.route("getTokenPrices", fixture("get_token_prices"))
    spend(tmp_db, "5")
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is not None
    assert len(codex.calls) == 1


def test_the_default_ceiling_is_eighty_percent_of_the_plan(codex, tmp_db):
    assert cx.ceiling_cu() == Decimal(8000) == cx.PLAN_CU_PER_WINDOW * Decimal("0.8")
    codex.route("getTokenPrices", fixture("get_token_prices"))
    spend(tmp_db, "7998")
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None
    assert codex.calls == []


def test_spend_outside_the_rolling_window_does_not_count(codex, tmp_db, monkeypatch):
    monkeypatch.setenv(cx.CEILING_ENV, "10")
    codex.route("getTokenPrices", fixture("get_token_prices"))
    spend(tmp_db, "9", days_ago=cx.WINDOW_DAYS)  # just left the window
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is not None
    spend(tmp_db, "9", days_ago=cx.WINDOW_DAYS - 1)  # still inside it
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None


def test_a_403_latches_a_pause(codex, tmp_db):
    codex.route("getTokenPrices", {"errors": [{"message": "paused"}]}, status=403)
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None
    sent = len(codex.calls)
    codex.route("getTokenPrices", fixture("get_token_prices"))
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None
    assert len(codex.calls) == sent, "a paused key must not be asked again"
    assert cx.quota_status(tmp_db).paused_until_ms > 0


def test_plan_gated_field_is_none_with_its_code_and_costs_nothing(codex, tmp_db):
    codex.route("listPairsWithMetadataForToken", fixture("not_authorized"))
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, conn=tmp_db, ttl_s=300) is None
    assert any("NOT_AUTHORIZED" in e["detail"] for e in errors(tmp_db))
    assert cx.quota_status(tmp_db).spent_cu == 0, "errored root fields are not billed"
    codex.route("listPairsWithMetadataForToken", fixture("list_pairs_robinhood_pons"))
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, conn=tmp_db, ttl_s=300) is not None, "the error was cached"


def test_a_cache_hit_sends_nothing_and_costs_nothing(codex, tmp_db):
    codex.route("listPairsWithMetadataForToken", fixture("list_pairs_robinhood_pons"))
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db, ttl_s=60) is not None
    spent = cx.quota_status(tmp_db).spent_cu
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db, ttl_s=60) is not None
    assert len(codex.calls) == 1
    assert cx.quota_status(tmp_db).spent_cu == spent == Decimal("4.0144")


def test_a_cached_answer_is_still_served_at_the_ceiling(codex, tmp_db, monkeypatch):
    """The guard protects the plan; a cache hit spends none of it, so it must not be refused."""
    codex.route("listPairsWithMetadataForToken", fixture("list_pairs_robinhood_pons"))
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db, ttl_s=300) is not None
    monkeypatch.setenv(cx.CEILING_ENV, "10")
    spend(tmp_db, "10")
    assert cx.token_pairs(Chain.ROBINHOOD, PONS, limit=3, conn=tmp_db, ttl_s=300) is not None
    assert len(codex.calls) == 1


def test_float_bearing_answers_are_not_cached_so_each_call_is_charged(codex, tmp_db):
    """trendingScore is a JSON float; _http cannot cache a Decimal body. Pin it, so a
    change there that starts caching (or a reader that assumes a cache) is noticed."""
    codex.route("filterTokens", fixture("filter_tokens_base_new"))
    cx.new_tokens(Chain.BASE, limit=3, conn=tmp_db, ttl_s=60)
    cx.new_tokens(Chain.BASE, limit=3, conn=tmp_db, ttl_s=60)
    assert len(codex.calls) == 2
    assert cx.quota_status(tmp_db).spent_cu == Decimal("10.024")


# ------------------------------------------------------------------- failure contract


def test_no_key_is_none_and_sends_nothing(codex, tmp_db, monkeypatch):
    from kaiba.core.config import get_settings

    monkeypatch.setenv("CODEX_IO_API_KEY", "")
    get_settings.cache_clear()
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None
    assert codex.calls == []
    assert any("not configured" in e["detail"] for e in errors(tmp_db))


@pytest.mark.parametrize("bad", ['0xabc" } evil { x', "", "short", None])
def test_a_malformed_address_is_refused_before_it_reaches_the_query(codex, tmp_db, bad):
    assert cx.token_events(Chain.BASE, bad, conn=tmp_db) is None
    assert codex.calls == []


def test_a_transport_failure_is_none_not_an_exception(codex, tmp_db):
    codex.raise_with = httpx.ConnectError("refused")
    assert cx.maker_events(MAKER, chain=Chain.ROBINHOOD, conn=tmp_db) is None
    assert errors(tmp_db)


def test_no_key_appears_in_logs_events_or_cache(codex, tmp_db, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    # 1. a transport error whose text carries the key
    codex.raise_with = httpx.ConnectError(f"refused Authorization: {SECRET}")
    assert cx.price(Chain.BASE, cx.WRAPPED_NATIVE[Chain.BASE], conn=tmp_db) is None
    # 2. a GraphQL error message that echoes the key
    codex.raise_with = None
    codex.route("filterTokens", {"errors": [{"message": f"bad key {SECRET}",
                                             "extensions": {"code": "UNAUTHENTICATED"}}], "data": None})
    assert cx.trending_tokens(Chain.BASE, conn=tmp_db) is None
    # 3. a successful, cached answer
    codex.route("getTokenEvents", fixture("token_events_robinhood_pons"))
    assert cx.token_events(Chain.ROBINHOOD, PONS, conn=tmp_db, ttl_s=60) is not None

    rows = tmp_db.execute("SELECT payload FROM events").fetchall()
    assert rows, "the failures were not reported at all"
    assert all(SECRET not in r[0] for r in rows)
    assert SECRET not in caplog.text
    cached = [p.read_text(encoding="utf-8") for p in (tmp_path / "cache").rglob("*.json")]
    assert cached, "nothing was cached, so the cache check proved nothing"
    assert all(SECRET not in text for text in cached)
    kv = tmp_db.execute("SELECT key, value FROM kv").fetchall()
    assert all(SECRET not in f"{k}{v}" for k, v in kv)
