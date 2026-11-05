"""Vendor observations, not executable quotes. All provider traffic is mocked."""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.providers import observed_market as market
from kaiba.providers._http import Fetched

AT = 1_793_620_000_000


def address(n):
    return "0x" + format(n, "040x")


TOKEN = address(1)


def pair(n=100, *, base=TOKEN, quote=None, price="2.50", liquidity=1000):
    return {"chainId": "hyperevm", "dexId": "fixture", "pairAddress": address(n),
            "baseToken": {"address": base, "symbol": "BASE"},
            "quoteToken": {"address": quote or address(2), "symbol": "QUOTE"},
            "priceUsd": price, "marketCap": None, "fdv": 100_000,
            "liquidity": {"usd": liquidity}, "volume": {"m5": 0},
            "txns": {"m5": {"buys": 0, "sells": 1}}, "pairCreatedAt": AT - 1000,
            "url": "https://untrusted.invalid/do-not-fetch"}


@pytest.fixture
def world(monkeypatch):
    state = {"data": [pair()], "basis": EvidenceBasis.PROVIDER_REPORTED, "stamp": AT}
    calls = []

    def fetch(provider, endpoint, url, **kw):
        calls.append((provider, endpoint, url, kw))
        return Fetched(copy.deepcopy(state["data"]), Receipt(
            provider=provider, endpoint=endpoint, basis=state["basis"], observed_at_ms=state["stamp"],
            request_digest="a" * 64, response_digest="b" * 64, note="private upstream details"))

    monkeypatch.setattr(market, "get_json", fetch)
    monkeypatch.setattr(market, "now_ms", lambda: AT)
    return state, calls


def test_price_selection_is_pool_specific_and_unknowns_never_turn_into_quote_or_profit(world):
    state, calls = world
    state["data"] = [pair(100, price="9", liquidity=10), pair(101, price="2.50", liquidity=1000)]
    out = market.token_market("hyperevm", TOKEN)
    selected = out["selected_price_pair"]
    assert out["available"] and out["accepted_pair_count"] == 2
    assert selected["pair_address"] == address(101) and selected["requested_token_price_usd"] == "2.50"
    assert out["price_range_usd"] == {"minimum": "2.50", "maximum": "9"}
    assert selected["requested_token_market_cap_usd"] is None  # FDV is not circulating cap
    assert selected["requested_token_fdv_usd"] == "100000"
    assert selected["market_as_of_ms"] is None and out["complete_market_snapshot"] is False
    assert out["execution_ready"] is False and out["atomic_snapshot"] is False
    assert all(out[k] is None for k in ("executable_quote", "executable_depth", "venue_fee_bps",
                                        "token_tax_bps", "holders", "safety_verified", "profitability_proven"))
    assert out["receipts"][0]["observed_at_ms"] == AT and "private" not in str(out)
    assert "untrusted.invalid" not in str(out)
    assert len(calls) == 1 and out["method_attempts_upper_bound"] == 1
    _, endpoint, url, kw = calls[0]
    assert endpoint.startswith("research.") and "/token-pairs/v1/hyperevm/" in url
    assert kw["priority"] is market.Priority.RESEARCH and kw["retries"] == 0
    assert kw["timeout_s"] == 5 and kw["stale_grace_s"] == 0 and kw["ttl_s"] == 10


def test_quote_token_pool_does_not_supply_the_requested_tokens_usd_price_or_market_cap(world):
    state, _ = world
    state["data"] = [pair(100, base=address(3), quote=TOKEN, price="999", liquidity=99999),
                     pair(101, price="2", liquidity=10)]
    out = market.token_market("hyperevm", TOKEN)
    quote = out["pairs"][0]
    assert quote["requested_token_role"] == "quote" and quote["base_token_price_usd"] == "999"
    assert quote["requested_token_price_usd"] is None and quote["requested_token_market_cap_usd"] is None
    assert quote["requested_token_fdv_usd"] is None
    assert out["selected_price_pair"]["pair_address"] == address(101)
    state["data"] = state["data"][:1]
    out = market.token_market("hyperevm", TOKEN)
    assert out["available"] and out["selected_price_pair"] is None and out["price_range_usd"] is None
    assert "requested_token_price_unavailable" in out["unknowns"]


def test_zero_liquidity_counts_and_volume_are_known_zeros_not_missing_or_fake_price(world):
    state, _ = world
    state["data"] = [pair(liquidity=0, price="0")]
    state["data"][0]["marketCap"] = 0
    out = market.token_market("hyperevm", TOKEN)
    p = out["pairs"][0]
    assert p["pool_liquidity_usd"] == p["requested_token_market_cap_usd"] == "0"
    assert p["pool_volume_usd"]["m5"] == "0" and p["pool_volume_usd"]["h1"] is None
    assert p["pool_transactions"]["m5"] == {"buys": 0, "sells": 1}
    assert out["selected_price_pair"] is None


@pytest.mark.parametrize("value", [True, False, -1, "NaN", "Infinity", "-Infinity", "bad", "1e999999", "1e-999999", "1" * 129,
    pytest.param(10**5000, id="huge_integer"),
    pytest.param(Decimal("1." + "1" * 140), id="huge_decimal_coefficient")])
def test_invalid_or_unbounded_numbers_remain_unknown_without_exception(world, value):
    state, _ = world
    p = pair(price=value, liquidity=value)
    p["marketCap"] = p["fdv"] = value
    state["data"] = [p]
    out = market.token_market("hyperevm", TOKEN)
    assert out["available"] and out["selected_price_pair"] is None
    assert out["pairs"][0]["pool_liquidity_usd"] is None
    assert out["pairs"][0]["requested_token_market_cap_usd"] is None
    assert out["pairs"][0]["requested_token_fdv_usd"] is None


def test_ranking_does_not_round_decimal_liquidity_at_the_default_context_precision(world):
    state, _ = world
    low, high = "1." + "0" * 40 + "1", "1." + "0" * 40 + "2"
    state["data"] = [pair(100, liquidity=low), pair(101, liquidity=high)]
    assert market.token_market("hyperevm", TOKEN)["selected_price_pair"]["pair_address"] == address(101)


@pytest.mark.parametrize("network", ["elysium", "hyperliquid", "hypercore", "bsc", "hyperevm testnet", None])
def test_unknown_mainnet_or_unsupported_network_has_zero_provider_calls(world, network):
    _, calls = world
    out = market.token_market(network, TOKEN)
    assert not calls and not out["available"] and out["method_attempts_upper_bound"] == 0
    assert out["unknowns"] == ["mainnet_identity_unverified" if network == "elysium" else "network_not_observed"]


@pytest.mark.parametrize("token", [None, "", "../", "https://elsewhere.invalid", "0x1", address(0), TOKEN + "/extra"])
def test_address_cannot_be_a_fetch_target_or_a_missing_contract(world, token):
    _, calls = world
    out = market.token_market("hyperevm", token)
    assert not calls and out["address"] is None and out["unknowns"] == ["address_invalid"]


@pytest.mark.parametrize("basis,age,available", [
    (EvidenceBasis.CACHED, 10000, True), (EvidenceBasis.CACHED, 10001, False),
    (EvidenceBasis.PROVIDER_REPORTED, -1, False), (EvidenceBasis.STALE, 0, False),
    (EvidenceBasis.UNAVAILABLE, 0, False),
])
def test_original_receipt_freshness_and_basis_are_preserved(world, basis, age, available):
    state, _ = world
    state.update(basis=basis, stamp=AT - age)
    out = market.token_market("hyperevm", TOKEN)
    assert out["available"] is available and out["receipts"][0]["observed_at_ms"] == AT - age
    assert out["method_attempts_upper_bound"] == int(basis is not EvidenceBasis.CACHED)
    if not available:
        assert out["pairs"] == [] and out["selected_price_pair"] is None


@pytest.mark.parametrize("changes,reason", [
    ({"chainId": "elysium"}, "pair_network_mismatch"),
    ({"pairAddress": "wrong"}, "pair_identity_invalid"),
    ({"baseToken": {"address": address(3)}}, "pair_token_mismatch"),
    ({"quoteToken": {"address": TOKEN}}, "pair_identity_invalid"),
    ({"dexId": "../../anything"}, "dex_identity_invalid"),
])
def test_foreign_or_ambiguous_pair_identity_is_not_attributed_to_the_requested_token(world, changes, reason):
    state, _ = world
    state["data"] = [{**pair(), **changes}]
    out = market.token_market("hyperevm", TOKEN)
    assert not out["available"] and out["rejected_pair_reasons"] == {reason: 1}


def test_missing_optional_data_stays_unknown_and_bad_clocks_and_text_are_not_exported(world):
    state, _ = world
    p = pair()
    p.update(pairCreatedAt=AT + 1, liquidity=None, volume=None, txns={"m5": {"buys": True, "sells": -1}})
    p["baseToken"]["symbol"] = "untrusted\x00control"
    state["data"] = [p]
    out = market.token_market("hyperevm", TOKEN)
    assert out["pairs"][0]["pair_created_ms"] is None and out["pairs"][0]["base_symbol"] is None
    assert out["pairs"][0]["pool_liquidity_usd"] is None
    assert out["pairs"][0]["pool_transactions"]["m5"] == {"buys": None, "sells": None}


def test_duplicate_conflict_cannot_inflate_liquidity_or_pick_a_convenient_price(world):
    state, _ = world
    state["data"] = [pair(), pair()]
    assert market.token_market("hyperevm", TOKEN)["accepted_pair_count"] == 1
    state["data"] = [pair(), pair(price="999"), pair(), pair(101)]
    out = market.token_market("hyperevm", TOKEN)
    assert out["accepted_pair_count"] == 1 and out["pairs"][0]["pair_address"] == address(101)
    assert out["rejected_pair_reasons"] == {"conflicting_duplicate_pair": 1}


def test_bounded_output_does_not_hide_an_input_cap_or_misreport_the_selected_pool(world):
    state, _ = world
    state["data"] = [pair(i, liquidity=i) for i in range(100, 125)]
    out = market.token_market("hyperevm", TOKEN)
    assert out["accepted_pair_count"] == 25 and len(out["pairs"]) == 16 and out["omitted_pair_count"] == 9
    assert out["selected_price_pair"]["pair_address"] == address(124)
    state["data"] = [pair(i) for i in range(100, 100 + market.MAX_INPUT_PAIRS + 1)]
    out = market.token_market("hyperevm", TOKEN)
    assert not out["available"] and out["unknowns"] == ["market_response_pair_cap"]


@pytest.mark.parametrize("data", [None, {}, {"pairs": [pair()]}, "bad", []])
def test_missing_or_wrong_response_shape_never_fabricates_a_price(world, data):
    state, _ = world
    state["data"] = data
    out = market.token_market("hyperevm", TOKEN)
    assert not out["available"] and out["selected_price_pair"] is None


def test_shared_transport_reuses_the_existing_provider_cache_with_original_clock(tmp_db, monkeypatch):
    import httpx

    from kaiba.providers import _http

    monkeypatch.setattr(_http, "now_ms", lambda: AT - 1000)
    url = f"{market.dex.BASE_URL}/token-pairs/v1/hyperevm/{TOKEN}"
    _http.cache_write(market.dex.PROVIDER, f"GET:{url}:{{}}", [pair()])
    monkeypatch.setattr(_http, "now_ms", lambda: AT)

    def denied(*a, **kw):
        pytest.fail("fresh existing DexScreener cache caused a network call")

    monkeypatch.setattr(httpx, "request", denied)
    out = market.token_market("hyperevm", TOKEN, conn=tmp_db, at_ms=AT)
    assert out["available"] and out["method_attempts_upper_bound"] == 0
    assert out["receipts"][0]["basis"] == "cached" and out["receipts"][0]["observed_at_ms"] == AT - 1000


def test_actual_429_is_not_retried_and_the_research_cooldown_preserves_exit_reservation(tmp_db, monkeypatch):
    import httpx

    from kaiba.core import limiter

    calls = []
    monkeypatch.setattr(limiter, "get_conn", lambda: tmp_db)

    def limited(method, url, **kw):
        calls.append(url)
        return httpx.Response(429, headers={"retry-after": "60"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", limited)
    first = market.token_market("hyperevm", TOKEN, conn=tmp_db)
    second = market.token_market("hyperevm", address(3), conn=tmp_db)
    assert not first["available"] and not second["available"] and len(calls) == 1
    assert first["unknowns"] == second["unknowns"] == ["market_evidence_unavailable"]
    # Use the real shared limiter; no changed limits, credit reset or mocked guard.
    with limiter.guarded(market.dex.PROVIDER, market.dex.EP_TOKEN_PAIRS, limiter.Priority.EXIT, conn=tmp_db):
        pass


def test_a_server_error_consumes_one_exchange_and_is_not_retried(tmp_db, monkeypatch):
    import httpx

    calls = []

    def failed(method, url, **kw):
        calls.append(url)
        return httpx.Response(503, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", failed)
    out = market.token_market("hyperevm", TOKEN, conn=tmp_db)
    assert len(calls) == 1 and not out["available"]
    assert out["unknowns"] == ["market_evidence_unavailable"]
