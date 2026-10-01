"""Jupiter quotes: what a fill would actually have cost, and the three ways it can fail.

Every fixture in ``tests/fixtures/jupiter/`` is a real response recorded from
``https://lite-api.jup.ag`` on 2026-09-20, not a hand-written shape. Two of them exist
because reality disagreed with the documentation:

* ``quote_buy_pumpcurve_0p02_sol.json`` is a **pump.fun token still on its bonding curve**,
  routed one hop through an AMM labelled ``Pump.fun``. Jupiter prices curve tokens. The
  assumption that a pre-graduation token has no pair is out of date, and a test that
  asserted "curve token => no route" would have encoded a falsehood.
* ``quote_not_tradable_400.json`` is the body Jupiter sends for a genuinely unroutable
  mint. It is recorded even though ``_http.request_json`` throws the body away on a 4xx,
  precisely so the next person can see what we are inferring from and how thin that
  inference is.

The property these tests defend above all others: **"no route" and "provider down" are not
the same answer.** One says go and get a curve price; the other says try again later. A
module that flattens both into ``None`` hands the caller a decision it cannot make.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core.limiter import Limits, Priority
from kaiba.core.schemas import SOL_NATIVE_MINT, Chain, EvidenceBasis
from kaiba.providers import jupiter as jup

FIXTURES = Path(__file__).parent / "fixtures" / "jupiter"

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
CURVE = "7qHxdUoTPfohFeHFRvEtbtbvNjbee5xWLAZANzzZpump"
DEAD = "9nQvkFbFCHPZ8nGQZK4UvVhvbfLGaCkA8mQ7yFAuDn3D"
USDC = jup.USDC_MINT


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# offline HTTP, same harness shape as tests/test_dexscreener.py
# --------------------------------------------------------------------------------------


class Recorder:
    """Stand-in for ``httpx.request`` that answers from fixtures and counts calls."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, int, Any]] = []
        self.calls: list[str] = []

    def add(self, match: str, payload: Any, status: int = 200) -> Recorder:
        self.routes.append((match, status, payload))
        return self

    def fail(self, match: str, status: int = 500, payload: Any = None) -> Recorder:
        return self.add(match, payload if payload is not None else {"error": "boom"}, status)

    def hits(self, match: str) -> int:
        return sum(1 for c in self.calls if match in c)

    def __call__(self, method: str, url: str, **kw: Any) -> httpx.Response:
        params = kw.get("params")
        full = str(httpx.URL(url, params=params)) if params else url
        self.calls.append(full)
        request = httpx.Request(method, full)
        for match, status, payload in self.routes:
            if match in full:
                return httpx.Response(status, json=payload, request=request)
        return httpx.Response(404, json={"error": f"unrouted {full}"}, request=request)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(httpx, "request", rec)
    return rec


@pytest.fixture
def fast_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the 1.1 s Jupiter interval so a test can make several calls in a row.

    Only the jupiter budget and the limiter's clock are replaced. Cooldown bookkeeping is
    untouched, so the 429 test still exercises the real path.
    """
    from kaiba.core import limiter
    from kaiba.core.schemas import now_ms

    real = limiter.limits_for
    fast = Limits(min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0, max_inflight=16)
    monkeypatch.setattr(limiter, "limits_for", lambda p: fast if p == jup.PROVIDER else real(p))

    clock = {"ms": now_ms()}

    def ticking() -> int:
        clock["ms"] += 1_000
        return clock["ms"]

    monkeypatch.setattr(limiter, "now_ms", ticking)


@pytest.fixture
def buy_bonk(http: Recorder) -> Recorder:
    return http.add("outputMint=" + BONK, fixture("quote_buy_bonk_0p02_sol"))


# --------------------------------------------------------------------------------------
# the endpoint we actually verified
# --------------------------------------------------------------------------------------


def test_the_free_host_is_the_keyless_lite_host(monkeypatch):
    """``quote-api.jup.ag`` is gone from DNS; building against it again would 404 forever."""
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    from kaiba.core.config import get_settings

    get_settings.cache_clear()
    host, headers = jup._base()
    assert host == "https://lite-api.jup.ag"
    assert headers == {}
    assert "quote-api.jup.ag" not in jup.LITE_HOST


def test_a_configured_key_moves_us_to_the_metered_host_and_travels_in_a_header(monkeypatch):
    """A key in the query string ends up in every httpx exception string. Header only."""
    monkeypatch.setenv("JUPITER_API_KEY", "secret-key-value-123456")
    from kaiba.core.config import get_settings

    get_settings.cache_clear()
    host, headers = jup._base()
    assert host == jup.PRO_HOST
    assert headers == {"x-api-key": "secret-key-value-123456"}


# --------------------------------------------------------------------------------------
# parsing a real quote
# --------------------------------------------------------------------------------------


def test_a_deep_route_parses_into_amounts_impact_and_a_route(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.ok
    assert got.status is jup.QuoteStatus.OK
    assert got.amount_out == int(fixture("quote_buy_bonk_0p02_sol")["outAmount"])
    assert got.min_out == int(fixture("quote_buy_bonk_0p02_sol")["otherAmountThreshold"])
    assert len(got.route) == 2
    recorded = fixture("quote_buy_bonk_0p02_sol")["routePlan"]
    assert got.route_labels == tuple(h["swapInfo"]["label"] for h in recorded)


def test_base_units_are_integers_not_the_strings_jupiter_sent(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert isinstance(got.amount_out, int)
    assert isinstance(got.min_out, int)
    assert isinstance(got.route[0].amount_in, int)


def test_usd_is_decimal_and_never_a_float(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert isinstance(got.swap_usd_value, Decimal)
    assert isinstance(got.price_impact_pct, Decimal)
    assert not isinstance(got.swap_usd_value, float)


def test_price_impact_keeps_every_digit_jupiter_sent(tmp_db, http):
    """``priceImpactPct`` arrives as a 28-digit string. Through a float it would not."""
    payload = dict(fixture("quote_buy_bonk_0p02_sol"))
    payload["priceImpactPct"] = "0.0137829618485124239692561421"
    http.add("swap/v1/quote", payload)
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.price_impact_pct == Decimal("0.0137829618485124239692561421")
    assert got.price_impact_bps == 138


def test_a_zero_impact_is_reported_as_zero_not_as_missing(tmp_db, buy_bonk):
    """Jupiter really does send ``"0"``, and it must not be laundered into ``None``.

    It must not be believed either. The same curve token reported ``"0"`` at 0.02 SOL and
    2.77% at 1 SOL moments later, so a zero here is what the provider said, not evidence
    that the trade is free. :func:`round_trip` is the number that survives scrutiny.
    """
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.price_impact_pct == Decimal(0)
    assert got.price_impact_bps == 0


def test_the_atom_rate_needs_no_decimals_to_be_exact(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.rate_atoms == Decimal(got.amount_out) / Decimal(20_000_000)


def test_a_per_unit_price_requires_both_decimals_and_never_guesses_one(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    sol_per_bonk = got.unit_price_in_per_out(9, 5)
    assert sol_per_bonk is not None and sol_per_bonk > 0
    # BONK has 5 decimals. Handing the method 9 would be a 10,000x error, so the caller
    # must supply it; there is no default to get wrong silently.
    assert got.unit_price_in_per_out(9, 9) != sol_per_bonk


def test_usd_per_token_comes_from_the_routes_own_usd_value(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    whole = Decimal(got.amount_out) / (Decimal(10) ** 5)
    assert got.usd_per_output_token(5) == got.swap_usd_value / whole
    # The input leg prices SOL itself: ~0.02 SOL was worth ~$2.17, so SOL is ~$108.
    sol = got.usd_per_input_token(9)
    assert Decimal(50) < sol < Decimal(500)


# --------------------------------------------------------------------------------------
# bonding curves: Jupiter does price them
# --------------------------------------------------------------------------------------


def test_a_pump_fun_bonding_curve_token_is_routed_and_priced(tmp_db, http):
    """Recorded live. The brief's premise that a curve token has no pair is out of date.

    88 bps of impact on a $3k-market-cap curve token for 0.02 SOL — a real, non-zero,
    size-dependent number, which is exactly what a mid price would have hidden.
    """
    http.add("outputMint=" + CURVE, fixture("quote_buy_pumpcurve_0p02_sol"))
    got = jup.quote_buy(CURVE, 20_000_000, conn=tmp_db)
    assert got.ok
    assert got.route_labels == ("Pump.fun",)
    assert got.on_bonding_curve is True
    assert got.price_impact_bps == 88


def test_the_sell_side_of_a_curve_token_also_quotes(tmp_db, http):
    """The direction that matters for an exit, and the one a honeypot fails."""
    http.add("inputMint=" + CURVE, fixture("quote_sell_pumpcurve"))
    got = jup.quote_sell(CURVE, 683_309_622_024, conn=tmp_db)
    assert got.ok
    assert got.output_mint == SOL_NATIVE_MINT
    assert got.amount_out == 19_480_932


def test_a_deep_pool_route_is_not_mislabelled_as_a_curve(tmp_db, buy_bonk):
    assert jup.quote_buy(BONK, 20_000_000, conn=tmp_db).on_bonding_curve is False


# --------------------------------------------------------------------------------------
# the boundary: no route vs provider failure
# --------------------------------------------------------------------------------------


def test_an_untradable_token_is_no_route_not_an_outage(tmp_db, http):
    http.fail("swap/v1/quote", 400, fixture("quote_not_tradable_400")["body"])
    got = jup.quote_buy(DEAD, 20_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.NO_ROUTE
    assert got.no_route is True
    assert got.failed is False
    assert got.http_status == 400


def test_a_five_hundred_is_an_outage_not_a_statement_about_the_token(tmp_db, http):
    """The distinction the caller acts on: retry later, versus go and price the curve."""
    http.fail("swap/v1/quote", 500)
    got = jup.quote_buy(CURVE, 20_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.UNAVAILABLE
    assert got.no_route is False
    assert got.failed is True


def test_a_rate_limit_is_never_read_as_a_missing_pair(tmp_db, http):
    """A 429 misfiled as NO_ROUTE would permanently retire a tradeable token."""
    http.fail("swap/v1/quote", 429)
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.UNAVAILABLE
    assert got.http_status == 429


def test_an_unreachable_provider_is_unavailable(tmp_db, monkeypatch):
    monkeypatch.setattr(jup, "_base", lambda: ("http://127.0.0.1:1", {}))
    got = jup.quote_buy(BONK, 20_000_000, timeout_s=0.3, wait_for_slot_s=0.0, conn=tmp_db)
    assert got.status is jup.QuoteStatus.UNAVAILABLE
    assert got.amount_out is None


def test_a_two_hundred_with_an_empty_route_is_no_route(tmp_db, http):
    payload = dict(fixture("quote_buy_bonk_0p02_sol"))
    payload["routePlan"] = []
    payload["outAmount"] = "0"
    http.add("swap/v1/quote", payload)
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.NO_ROUTE


def test_a_two_hundred_with_no_out_amount_is_an_outage_not_a_missing_pair(tmp_db, http):
    """A malformed answer says nothing about the token, so it must not retire it."""
    payload = dict(fixture("quote_buy_bonk_0p02_sol"))
    payload.pop("outAmount")
    http.add("swap/v1/quote", payload)
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.UNAVAILABLE


def test_a_missing_amount_is_none_and_never_zero(tmp_db, http):
    http.fail("swap/v1/quote", 400, fixture("quote_not_tradable_400")["body"])
    got = jup.quote_buy(DEAD, 20_000_000, conn=tmp_db)
    assert got.amount_out is None
    assert got.min_out is None
    assert got.rate_atoms is None
    assert got.price_impact_bps is None
    assert bool(got) is False


def test_every_failure_carries_an_unavailable_receipt_naming_the_provider(tmp_db, http):
    http.fail("swap/v1/quote", 503)
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert got.receipt.provider == jup.PROVIDER
    assert got.receipt.endpoint == jup.ENDPOINT_QUOTE


def test_a_success_carries_a_provider_reported_receipt(tmp_db, buy_bonk):
    got = jup.quote_buy(BONK, 20_000_000, conn=tmp_db)
    assert got.receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert got.receipt.provider == jup.PROVIDER


def test_a_status_code_is_not_scraped_out_of_the_request_url(tmp_db, http):
    """``amount=450...`` inside the logged URL must not be read as a 450 response.

    The loose regex that would do this classifies a live token as permanently unroutable,
    so the strict one is the point of the test.
    """
    http.fail("swap/v1/quote", 500)
    got = jup.quote_buy(BONK, 450_000_000, conn=tmp_db)
    assert got.status is jup.QuoteStatus.UNAVAILABLE
    assert got.http_status == 500


# --------------------------------------------------------------------------------------
# our own bad requests never leave the process
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"output_mint": "not-a-mint"}, "malformed mint"),
        ({"output_mint": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"}, "evm address"),
        ({"amount_in": 0}, "zero amount"),
        ({"amount_in": -5}, "negative amount"),
        ({"amount_in": 1.5}, "float amount"),
        ({"slippage_bps": -1}, "negative slippage"),
        ({"slippage_bps": 99_999}, "absurd slippage"),
        ({"output_mint": SOL_NATIVE_MINT}, "input equals output"),
    ],
)
def test_a_request_we_know_is_wrong_is_refused_locally(tmp_db, http, kwargs, why):
    args = {"input_mint": SOL_NATIVE_MINT, "output_mint": BONK, "amount_in": 20_000_000}
    args.update(kwargs)
    got = jup.quote(conn=tmp_db, **args)
    assert got.status is jup.QuoteStatus.BAD_REQUEST, why
    assert got.failed is True
    assert got.no_route is False, "our bug is never a statement about the token"
    assert http.calls == [], "a request we know is malformed must not spend limiter budget"


def test_a_non_solana_chain_is_refused_rather_than_routed(tmp_db, http):
    got = jup.quote(SOL_NATIVE_MINT, BONK, 20_000_000, chain=Chain.ETH, conn=tmp_db)
    assert got.status is jup.QuoteStatus.BAD_REQUEST
    assert http.calls == []


# --------------------------------------------------------------------------------------
# token metadata and decimals
# --------------------------------------------------------------------------------------


def test_token_info_parses_decimals_depth_and_the_mid_price(tmp_db, http):
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    info = jup.token_info(BONK, conn=tmp_db)
    assert info.known is True
    assert info.decimals == 5
    assert isinstance(info.liquidity_usd, Decimal)
    assert isinstance(info.usd_price, Decimal)


def test_a_mint_jupiter_does_not_know_is_an_empty_answer_not_an_error(tmp_db, http):
    http.add("price/v3", fixture("price_v3_unknown"))
    info = jup.token_info(DEAD, conn=tmp_db)
    assert info.known is False
    assert info.decimals is None
    assert info.usd_price is None


def test_decimals_prefer_the_local_tokens_row_over_a_network_call(tmp_db, http):
    tmp_db.execute(
        "INSERT INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
        (Chain.SOL.value, BONK, 5, 0),
    )
    tmp_db.commit()
    value, basis = jup.mint_decimals(BONK, conn=tmp_db)
    assert (value, basis) == (5, jup.DECIMALS_TOKENS_ROW)
    assert http.calls == []


def test_decimals_fall_back_to_jupiter_when_the_token_is_not_ingested(tmp_db, http):
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    value, basis = jup.mint_decimals(BONK, conn=tmp_db)
    assert (value, basis) == (5, jup.DECIMALS_JUPITER)


def test_unknown_decimals_are_unavailable_and_never_a_plausible_default(tmp_db, http):
    """Guessing 6 or 9 is a factor of 1000 on a price. ``None`` is the only honest answer."""
    http.add("price/v3", fixture("price_v3_unknown"))
    assert jup.mint_decimals(DEAD, conn=tmp_db) == (None, jup.DECIMALS_UNAVAILABLE)


# --------------------------------------------------------------------------------------
# executable price
# --------------------------------------------------------------------------------------


def test_the_usd_price_is_the_one_the_route_would_have_delivered(tmp_db, http, fast_limiter):
    http.add("outputMint=" + BONK, fixture("quote_buy_bonk_0p02_sol"))
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    found = jup.executable_price(BONK, conn=tmp_db)
    assert found.known
    assert found.decimals == 5
    assert found.price_usd == found.quote.usd_per_output_token(5)
    assert found.liquidity_usd is not None
    assert found.probe_amount_in == jup.PROBE_LAMPORTS


def test_a_quote_without_decimals_yields_no_price_rather_than_a_wrong_one(tmp_db, http, fast_limiter):
    http.add("outputMint=" + BONK, fixture("quote_buy_bonk_0p02_sol"))
    http.add("price/v3", fixture("price_v3_unknown"))
    found = jup.executable_price(BONK, conn=tmp_db)
    assert found.quote.ok, "the route was fine; only the decimals were missing"
    assert found.price_usd is None
    assert found.known is False
    assert found.receipt.basis is EvidenceBasis.UNAVAILABLE
    assert found.decimals_basis == jup.DECIMALS_UNAVAILABLE


def test_the_native_mint_is_priced_through_the_usdc_leg(tmp_db, http, fast_limiter):
    """The watchdog asks its price source for SOL itself when it computes ``min_out``."""
    http.add("outputMint=" + USDC, fixture("quote_buy_bonk_0p02_sol"))
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    found = jup.executable_price(SOL_NATIVE_MINT, conn=tmp_db)
    assert found.known
    assert Decimal(50) < found.price_usd < Decimal(500)
    assert found.decimals == 9


def test_an_unroutable_token_reports_no_route_all_the_way_up(tmp_db, http, fast_limiter):
    http.fail("swap/v1/quote", 400, fixture("quote_not_tradable_400")["body"])
    found = jup.executable_price(DEAD, conn=tmp_db)
    assert found.known is False
    assert found.no_route is True
    assert found.price_usd is None


# --------------------------------------------------------------------------------------
# round trip and the impact ladder
# --------------------------------------------------------------------------------------


def test_a_round_trip_charges_both_legs_not_just_the_buy_impact(tmp_db, http, fast_limiter):
    """The recorded pair: 0.02 SOL in, 0.019480932 SOL back out of a live curve token.

    The buy-side impact alone was 88 bps. The real cost of touching the position twice was
    260 bps — three times as much — and it is the second number a paper fill has to be
    charged. Quoting impact and calling it slippage understates the cost of a round trip.
    """
    http.add("inputMint=" + CURVE, fixture("quote_sell_pumpcurve"))
    http.add("outputMint=" + CURVE, fixture("quote_buy_pumpcurve_0p02_sol"))
    trip = jup.round_trip(CURVE, 20_000_000, conn=tmp_db)
    assert trip.known
    assert trip.lamports_out == 19_480_932
    assert trip.cost_bps == 260
    assert trip.cost_bps > (trip.buy.price_impact_bps or 0)


def test_a_failed_sell_leg_gives_no_cost_rather_than_a_free_round_trip(tmp_db, http, fast_limiter):
    """``cost_bps = 0`` would tell the paper broker the exit was free. It must be ``None``."""
    http.add("outputMint=" + CURVE, fixture("quote_buy_pumpcurve_0p02_sol"))
    http.fail("inputMint=" + CURVE, 500)
    trip = jup.round_trip(CURVE, 20_000_000, conn=tmp_db)
    assert trip.known is False
    assert trip.cost_bps is None
    assert trip.buy.ok


def test_the_impact_ladder_makes_one_call_per_size_and_loses_none_of_them(tmp_db, http, fast_limiter):
    """A silently-dropped call reads as a flat impact curve, i.e. "our size is free"."""
    http.add("swap/v1/quote", fixture("quote_buy_pumpcurve_0p02_sol"))
    rungs = jup.impact_ladder(CURVE, (20_000_000, 1_000_000_000, 10_000_000_000), conn=tmp_db)
    assert len(rungs) == 3
    assert http.hits("swap/v1/quote") == 3
    assert [r.amount_in for r in rungs] == [20_000_000, 1_000_000_000, 10_000_000_000]


# --------------------------------------------------------------------------------------
# the watchdog price source
# --------------------------------------------------------------------------------------


def test_it_satisfies_the_watchdogs_price_source_protocol():
    watchdog = pytest.importorskip("kaiba.execution.watchdog")
    assert isinstance(jup.JupiterPriceSource(), watchdog.PriceSource)


def test_the_watchdog_gets_a_real_price_quote_it_will_accept(tmp_db, http, fast_limiter):
    """``Watchdog._quote`` rejects anything that is not its own ``PriceQuote`` instance."""
    watchdog = pytest.importorskip("kaiba.execution.watchdog")
    http.add("outputMint=" + BONK, fixture("quote_buy_bonk_0p02_sol"))
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    got = jup.JupiterPriceSource(conn=tmp_db).quote(Chain.SOL, BONK)
    assert isinstance(got, watchdog.PriceQuote)
    assert got.usable
    assert got.executable_quote_usd is not None
    assert got.source.startswith("jupiter:")


def test_the_blind_reason_distinguishes_no_route_from_an_outage(tmp_db, http, fast_limiter):
    """``quote.source`` lands in every ``protection_blind`` event, so it has to say which."""
    pytest.importorskip("kaiba.execution.watchdog")
    http.fail("swap/v1/quote", 400, fixture("quote_not_tradable_400")["body"])
    missing = jup.JupiterPriceSource(conn=tmp_db).quote(Chain.SOL, DEAD)
    assert missing.usable is False
    assert missing.source == "jupiter:no_route"

    http.routes.clear()
    http.fail("swap/v1/quote", 503)
    down = jup.JupiterPriceSource(conn=tmp_db).quote(Chain.SOL, DEAD)
    assert down.usable is False
    assert down.source == "jupiter:unavailable"


def test_the_price_source_never_raises_into_the_watchdog_loop(tmp_db, monkeypatch):
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(jup, "executable_price", boom)
    got = jup.JupiterPriceSource(conn=tmp_db).quote(Chain.SOL, BONK)
    assert got.usable is False
    assert got.source == "jupiter:unavailable"


def test_a_non_solana_chain_leaves_the_watchdog_blind_rather_than_guessing(tmp_db, http):
    got = jup.JupiterPriceSource(conn=tmp_db).quote(Chain.ETH, BONK)
    assert got.usable is False
    assert http.calls == []


# --------------------------------------------------------------------------------------
# the batch price stack
# --------------------------------------------------------------------------------------


def test_it_also_plugs_into_the_prices_registry_without_editing_it(tmp_db, http, fast_limiter):
    from kaiba.providers import prices

    http.add("outputMint=" + BONK, fixture("quote_buy_bonk_0p02_sol"))
    http.add("price/v3", fixture("price_v3_bonk_sol"))
    source = jup.JupiterPricesSource()
    out = source.quotes(Chain.SOL, [BONK], priority=Priority.POSITION, conn=tmp_db)
    assert set(out) == {BONK}
    assert isinstance(out[BONK], prices.Quote)
    assert out[BONK].price_usd is not None
    assert out[BONK].source == jup.PROVIDER


# --------------------------------------------------------------------------------------
# live
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_quote_and_impact_ladder(tmp_db):
    """Proof the endpoint still exists and is still keyless. ``KAIBA_LIVE_TESTS=1``."""
    small = jup.quote_buy(BONK, 20_000_000, wait_for_slot_s=15.0, conn=tmp_db)
    assert small.ok, small.note
    assert small.amount_out and small.amount_out > 0
    assert small.price_impact_pct is not None
    big = jup.quote_buy(BONK, 10_000_000_000, wait_for_slot_s=15.0, conn=tmp_db)
    assert big.ok, big.note
    assert (big.price_impact_bps or 0) >= (small.price_impact_bps or 0)
