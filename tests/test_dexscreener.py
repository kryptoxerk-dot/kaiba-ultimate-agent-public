"""Offline tests for the DexScreener adapter and the price layer built on it.

Every fixture under ``tests/fixtures/dexscreener/`` is a verbatim recording of a real
response taken on 2026-09-20; nothing here was hand-written except where a test says so
explicitly and explains why (the CTO flag and the zero-price case never appeared in a
sample of live data, and waiting for one is not a test strategy).

The tests that matter most are the ones asserting what happens when there is *no* answer:
:func:`test_price_unknown_when_no_pair_exists` and
:func:`test_price_unknown_when_provider_is_down`. A zero returned from either of those
paths reads as a total loss to a stop-loss calculation and would close a healthy position.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core import events as ev
from kaiba.core.limiter import Limits, Priority
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Measure, Receipt
from kaiba.providers import dexscreener as ds
from kaiba.providers import prices

FIXTURES = Path(__file__).parent / "fixtures" / "dexscreener"

WSOL = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# offline HTTP
# --------------------------------------------------------------------------------------


class Recorder:
    """Stand-in for ``httpx.request`` that answers from fixtures and counts calls."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, int, Any]] = []
        self.calls: list[str] = []

    def add(self, match: str, payload: Any, status: int = 200) -> Recorder:
        self.routes.append((match, status, payload))
        return self

    def fail(self, match: str, status: int = 500) -> Recorder:
        return self.add(match, {"error": "boom"}, status)

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
    """Let a test make several calls in a row without waiting out the real 1.1s interval.

    Two things are neutralised: the dexscreener budget (replaced with a permissive one) and
    the limiter's clock, which is replaced with one that advances a second per read. The
    clock matters because :func:`kaiba.core.limiter.reserve` keeps a hard 50 ms floor even
    for high-priority work, and a test doing two lookups in the same millisecond would
    otherwise be refused by the limiter rather than by the code under test. Cooldown
    bookkeeping is left completely intact so the 429 test still means something.
    """
    from kaiba.core import limiter
    from kaiba.core.schemas import now_ms

    real = limiter.limits_for
    fast = Limits(min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0, max_inflight=16)
    monkeypatch.setattr(limiter, "limits_for", lambda p: fast if p == ds.PROVIDER else real(p))

    clock = {"ms": now_ms()}

    def ticking() -> int:
        clock["ms"] += 1_000
        return clock["ms"]

    monkeypatch.setattr(limiter, "now_ms", ticking)


@pytest.fixture
def clean_sources() -> Any:
    prices.reset_sources()
    yield
    prices.reset_sources()


@pytest.fixture
def wired(tmp_db, http: Recorder, fast_limiter, clean_sources) -> Recorder:  # noqa: ANN001
    """The usual offline rig: migrated temp db, faked HTTP, no rate-limit sleeping."""
    return http


def sol_address(seed: int) -> str:
    """A syntactically valid base58 Solana address, for batching tests."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    out = []
    n = seed + 10**20
    while len(out) < 43:
        out.append(alphabet[n % len(alphabet)])
        n = n // len(alphabet) + 7 * (len(out) + seed)
    return "".join(out)


# --------------------------------------------------------------------------------------
# chain mapping
# --------------------------------------------------------------------------------------


def test_chain_slugs_round_trip() -> None:
    for slug, chain in ds.SLUG_TO_CHAIN.items():
        assert ds.chain_from_slug(slug) is chain
        assert ds.slug_for_chain(chain) == slug
    assert ds.chain_from_slug("SOLANA") is Chain.SOL
    assert ds.slug_for_chain(Chain.SOL) == "solana"
    assert ds.slug_for_chain(Chain.ETH) == "ethereum"


def test_unmodelled_chain_returns_nothing_rather_than_guessing() -> None:
    for slug in ("near", "arbitrum", "sui", "", None, "  "):
        assert ds.chain_from_slug(slug) is None
    # Chain.STABLE has no verified DexScreener slug, so it must resolve to "uncovered".
    assert ds.slug_for_chain(Chain.STABLE) is None


def test_unsupported_chain_never_makes_a_request(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    pairs, receipt = ds.token_pairs(Chain.STABLE, WSOL, conn=tmp_db)
    assert pairs == []
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "no slug" in (receipt.note or "")
    assert wired.calls == []


# --------------------------------------------------------------------------------------
# decimal parsing
# --------------------------------------------------------------------------------------


def test_to_decimal_handles_every_shape_dexscreener_sends() -> None:
    assert ds.to_decimal("0.000002975") == Decimal("0.000002975")
    assert ds.to_decimal(285137.04) == Decimal("285137.04")
    assert ds.to_decimal(232793) == Decimal(232793)
    assert ds.to_decimal(Decimal("1.5")) == Decimal("1.5")
    for junk in (None, "", "   ", "abc", True, False, [], {}, float("inf"), float("nan")):
        assert ds.to_decimal(junk) is None


def test_price_of_zero_is_not_a_price() -> None:
    """A provider saying "0" is a provider with nothing to say, not a worthless token."""
    raw = copy.deepcopy(fixture("tokens_solana_bonk")[0])
    raw["priceUsd"] = "0"
    assert ds._parse_pair(raw).price_usd is None
    raw["priceUsd"] = "0.00000000"
    assert ds._parse_pair(raw).price_usd is None
    raw["priceUsd"] = "-1"
    assert ds._parse_pair(raw).price_usd is None


# --------------------------------------------------------------------------------------
# endpoint parsing
# --------------------------------------------------------------------------------------


def test_token_profiles_latest_parses_and_filters(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("token_profiles_latest")
    wired.add("/token-profiles/latest/v1", raw)
    profiles, receipt = ds.token_profiles_latest(conn=tmp_db)

    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert len(profiles) == sum(1 for r in raw if ds.chain_from_slug(r["chainId"]))
    assert all(p.chain in ds.SLUG_TO_CHAIN.values() for p in profiles)
    first = profiles[0]
    assert first.address == raw[0]["tokenAddress"]
    assert first.cto is False
    assert first.key == f"{first.chain.value}:{first.address}"


def test_profile_socials_are_flattened() -> None:
    raw = next(r for r in fixture("token_profiles_latest") if r.get("links"))
    profile = ds._parse_profile(raw)
    assert isinstance(profile.socials, dict)
    assert all(isinstance(v, str) for v in profile.socials.values())


def test_token_boosts_latest_and_top_differ_in_shape(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    latest_raw, top_raw = fixture("token_boosts_latest"), fixture("token_boosts_top")
    wired.add("/token-boosts/latest/v1", latest_raw).add("/token-boosts/top/v1", top_raw)

    latest, _ = ds.token_boosts_latest(conn=tmp_db)
    top, _ = ds.token_boosts_top(conn=tmp_db)

    # The "latest" feed carries the size of the most recent purchase; "top" only the total.
    assert any(b.amount is not None for b in latest)
    assert all(b.amount is None for b in top)
    assert all(b.total_amount > 0 for b in top)


def test_boosts_on_unmodelled_chains_are_dropped(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("token_boosts_latest")
    unmapped = [r for r in raw if ds.chain_from_slug(r["chainId"]) is None]
    assert unmapped, "fixture should contain at least one chain we do not model"
    wired.add("/token-boosts/latest/v1", raw)

    boosts, _ = ds.token_boosts_latest(conn=tmp_db)
    assert len(boosts) == len(raw) - len(unmapped)
    dropped = {r["tokenAddress"] for r in unmapped}
    assert not dropped & {b.address for b in boosts}


def test_orders_accepts_the_live_envelope(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """Live ``orders/v1`` returns ``{"orders": [...], "boosts": [...]}`` although it is
    documented as a bare array. Both shapes must parse."""
    raw = fixture("orders_solana_wsol")
    assert isinstance(raw, dict) and "orders" in raw
    wired.add("/orders/v1/solana/", raw)

    orders, receipt = ds.token_orders(Chain.SOL, WSOL, conn=tmp_db)
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    assert len(orders) == len(raw["orders"])
    assert {o.type for o in orders} >= {"tokenProfile", "communityTakeover"}
    assert any(o.approved for o in orders)


def test_orders_also_accepts_a_bare_array(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/orders/v1/solana/", fixture("orders_solana_wsol")["orders"])
    orders, _ = ds.token_orders(Chain.SOL, WSOL, conn=tmp_db)
    assert len(orders) == 10


def test_community_takeover_distinguishes_no_from_unknown(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """The recorded token's only takeover order was cancelled, so the answer is False.
    When the provider is unreachable the answer must be None, not False."""
    wired.add("/orders/v1/solana/", fixture("orders_solana_wsol"))
    answer, _ = ds.has_community_takeover(Chain.SOL, WSOL, conn=tmp_db)
    assert answer is False

    down = Recorder().fail("/orders/v1/")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "request", down)
        answer, receipt = ds.has_community_takeover(Chain.SOL, BONK, conn=tmp_db)
    assert answer is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE


def test_token_pairs_parses_full_pair_list(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("token_pairs_solana_bonk")
    wired.add("/token-pairs/v1/solana/", raw)
    pairs, receipt = ds.token_pairs(Chain.SOL, BONK, conn=tmp_db)

    assert len(pairs) == len(raw)
    assert receipt.endpoint == ds.EP_TOKEN_PAIRS
    best = max(pairs, key=lambda p: p.liquidity_usd or Decimal(-1))
    assert isinstance(best.price_usd, Decimal)
    assert isinstance(best.liquidity_usd, Decimal)
    assert best.prices_for(BONK)
    assert best.age_s and best.age_s > 0


def test_dexscreener_puts_the_queried_token_in_the_base_slot(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """The empirical basis for the base/quote guard in :func:`prices.pick_pair`.

    WSOL is the canonical *quote* asset on Solana, yet all 30 recorded pairs return it as
    the base. So the guard is not expected to fire in normal operation — which is exactly
    why it has to be there: if that ever changes we would silently quote USDC's price for
    a memecoin instead of noticing.
    """
    raw = fixture("token_pairs_solana_wsol")
    assert all(p["baseToken"]["address"] == WSOL for p in raw)
    wired.add("/token-pairs/v1/solana/", raw)

    pairs, _ = ds.token_pairs(Chain.SOL, WSOL, conn=tmp_db)
    assert pairs and all(p.prices_for(WSOL) for p in pairs)
    assert prices.pick_pair(pairs, WSOL) is not None
    assert prices.pick_pair(pairs, BONK) is None, "a token absent from the base slot is unpriced"


def test_tokens_respects_the_thirty_address_cap(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/tokens/v1/solana/", [])
    asked = [sol_address(i) for i in range(45)]
    _, receipt = ds.tokens(Chain.SOL, asked, conn=tmp_db)

    assert wired.hits("/tokens/v1/") == 1
    sent = wired.calls[0].rsplit("/", 1)[-1].split(",")
    assert len(sent) == ds.MAX_TOKEN_ADDRESSES == 30
    assert "dropped 15" in (receipt.note or "")


def test_search_sorts_by_liquidity_and_drops_unmodelled_chains(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("search_bonk")
    wired.add("/latest/dex/search", raw)
    pairs, _ = ds.search("BONK", conn=tmp_db)

    kept = [p for p in raw["pairs"] if ds.chain_from_slug(p["chainId"])]
    assert len(pairs) == len(kept)
    liquidity = [p.liquidity_usd or Decimal(-1) for p in pairs]
    assert liquidity == sorted(liquidity, reverse=True)


def test_empty_query_and_empty_address_list_short_circuit(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    assert ds.search("   ", conn=tmp_db)[0] == []
    assert ds.tokens(Chain.SOL, [], conn=tmp_db)[0] == []
    assert wired.calls == []


# --------------------------------------------------------------------------------------
# failure behaviour
# --------------------------------------------------------------------------------------


def test_provider_down_returns_empty_plus_unavailable_and_an_event(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.fail("/token-boosts/latest/v1", 503)
    boosts, receipt = ds.token_boosts_latest(conn=tmp_db)

    assert boosts == []
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    errors = ev.recent(limit=20, kinds=[EventKind.PROVIDER_ERROR])
    assert errors and errors[0].payload["provider"] == ds.PROVIDER


def test_a_429_cools_the_discovery_family_and_leaves_price_alone(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """This is why the endpoint strings are grouped the way they are: a rate limit on the
    promotion feeds must not stop the exit watchdog reading a price."""
    from kaiba.core import limiter

    wired.add("/token-boosts/latest/v1", {"error": "slow down"}, status=429)
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))

    boosts, receipt = ds.token_boosts_latest(conn=tmp_db)
    assert boosts == []
    assert receipt.basis is EvidenceBasis.UNAVAILABLE

    assert limiter.family_banned_until(tmp_db, ds.PROVIDER, ds.EP_BOOSTS_LATEST) > 0
    assert limiter.family_banned_until(tmp_db, ds.PROVIDER, ds.EP_PROFILES_LATEST) > 0  # same bucket
    assert limiter.family_banned_until(tmp_db, ds.PROVIDER, ds.EP_TOKEN_PAIRS) == 0
    assert limiter.family_banned_until(tmp_db, ds.PROVIDER, ds.EP_SEARCH) == 0

    pairs, _ = ds.token_pairs(Chain.SOL, BONK, conn=tmp_db)
    assert pairs, "the price route must still work while discovery is cooling down"


def test_garbage_rows_are_skipped_not_fatal(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-boosts/latest/v1", ["nonsense", 7, None, {}, {"chainId": "solana"}])
    boosts, receipt = ds.token_boosts_latest(conn=tmp_db)
    assert boosts == []
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED  # the call itself succeeded


# --------------------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------------------


def test_second_call_inside_the_ttl_is_served_from_cache(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-boosts/top/v1", fixture("token_boosts_top"))
    first, r1 = ds.token_boosts_top(conn=tmp_db)
    second, r2 = ds.token_boosts_top(conn=tmp_db)

    assert wired.hits("/token-boosts/top/v1") == 1
    assert r1.basis is EvidenceBasis.PROVIDER_REPORTED
    assert r2.basis is EvidenceBasis.CACHED
    assert [b.address for b in first] == [b.address for b in second]


def test_pair_data_is_never_cached_longer_than_ttl_pairs(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """A caller asking for a five-minute-old price does not get one."""
    assert ds._pair_ttl(300.0) == ds.TTL_PAIRS_S
    assert ds._pair_ttl(2.0) == 2.0
    assert ds._pair_ttl(0.0) == 0.0
    assert ds._pair_ttl(None) == ds.TTL_PAIRS_S

    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    ds.token_pairs(Chain.SOL, BONK, ttl_s=0.0, conn=tmp_db)
    ds.token_pairs(Chain.SOL, BONK, ttl_s=0.0, conn=tmp_db)
    assert wired.hits("/token-pairs/v1/") == 2, "ttl_s=0 must disable the cache entirely"


# --------------------------------------------------------------------------------------
# alpha events
# --------------------------------------------------------------------------------------


def test_boost_events_do_not_repeat_every_poll(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-boosts/latest/v1", fixture("token_boosts_latest"))
    boosts, _ = ds.token_boosts_latest(conn=tmp_db)

    first = ds.emit_boost_events(boosts, conn=tmp_db)
    second = ds.emit_boost_events(boosts, conn=tmp_db)

    assert len(first) == len(boosts) > 0
    assert second == [], "a poller must not re-emit the same boost on the next cycle"
    assert len(ev.recent(limit=200, kinds=[EventKind.ALPHA_BOOST])) == len(boosts)


def test_a_bigger_boost_total_is_new_news(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """Deduping on the token alone would hide a token going from 10 to 500 boost, which is
    exactly the escalation worth trading."""
    raw = fixture("token_boosts_latest")[0]
    small = ds._parse_boost({**raw, "totalAmount": 10})
    big = ds._parse_boost({**raw, "totalAmount": 500})

    assert len(ds.emit_boost_events([small], conn=tmp_db)) == 1
    assert ds.emit_boost_events([small], conn=tmp_db) == []
    assert len(ds.emit_boost_events([big], conn=tmp_db)) == 1


def test_cto_events_only_fire_for_takeovers(tmp_db) -> None:  # noqa: ANN001
    """No live sample carried ``cto: true``, so the flag is set here on a real row — the
    field itself is present and ``false`` on all 30 recorded profiles."""
    raw = fixture("token_profiles_latest")
    assert all("cto" in r for r in raw)
    plain = [ds._parse_profile(r) for r in raw[:3]]
    taken = ds._parse_profile({**raw[3], "cto": True})

    assert ds.emit_cto_events(plain, conn=tmp_db) == []
    assert len(ds.emit_cto_events([*plain, taken], conn=tmp_db)) == 1
    assert ds.emit_cto_events([taken], conn=tmp_db) == [], "a takeover happens once"

    fired = ev.recent(limit=10, kinds=[EventKind.ALPHA_CTO])
    assert len(fired) == 1 and fired[0].subject == taken.address


def test_poll_alpha_survives_a_dead_provider(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.fail("/token-boosts/latest/v1").fail("/token-profiles/latest/v1")
    out = ds.poll_alpha(conn=tmp_db)
    assert out == {
        "boosts_seen": 0,
        "boosts_emitted": 0,
        "profiles_seen": 0,
        "ctos_emitted": 0,
        "receipts": out["receipts"],
    }
    assert all(r.basis is EvidenceBasis.UNAVAILABLE for r in out["receipts"])


def test_poll_alpha_spaces_its_calls_under_the_real_limiter(
    http: Recorder, tmp_db, monkeypatch: pytest.MonkeyPatch, clean_sources
) -> None:  # noqa: ANN001
    """Regression: the first live run fetched boosts and was then refused for profiles.

    ``_http`` never blocks on the limiter, so ``poll_alpha`` has to leave the configured
    1.1 s between its own calls. Real limits apply here — only the sleeping is virtual, and
    the limiter's clock advances by exactly what was slept, so a missing ``_pace()`` shows
    up as a refused second call rather than as a slow test.
    """
    from kaiba.core import limiter
    from kaiba.core.schemas import now_ms

    clock = {"ms": now_ms()}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["ms"] += int(seconds * 1000)

    monkeypatch.setattr(limiter, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(ds.time, "sleep", fake_sleep)

    http.add("/token-boosts/latest/v1", fixture("token_boosts_latest"))
    http.add("/token-boosts/top/v1", fixture("token_boosts_top"))
    http.add("/token-profiles/latest/v1", fixture("token_profiles_latest"))

    out = ds.poll_alpha(include_top=True, conn=tmp_db)

    assert http.hits("/token-boosts/latest/v1") == 1
    assert http.hits("/token-boosts/top/v1") == 1
    assert http.hits("/token-profiles/latest/v1") == 1
    assert out["boosts_seen"] > 0 and out["profiles_seen"] > 0
    assert len(slept) == 2
    min_gap_s = limiter.limits_for(ds.PROVIDER).min_interval_ms / 1000.0
    assert all(s >= min_gap_s for s in slept)


def test_poll_alpha_emits_on_a_healthy_provider(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-boosts/latest/v1", fixture("token_boosts_latest"))
    wired.add("/token-profiles/latest/v1", fixture("token_profiles_latest"))
    out = ds.poll_alpha(conn=tmp_db)
    assert out["boosts_emitted"] > 0
    assert out["profiles_seen"] > 0
    assert out["ctos_emitted"] == 0  # none of the recorded profiles is a takeover


# --------------------------------------------------------------------------------------
# prices: the rules that protect a position
# --------------------------------------------------------------------------------------


def test_price_unknown_when_no_pair_exists(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """THE test. An unlisted token must not read as worthless.

    ``0.0`` here would be a 100% drawdown to any stop-loss comparison and would sell a
    healthy position on the strength of an empty JSON array.
    """
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_empty"))
    measure = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)

    assert measure.value is None
    assert measure.value != Decimal(0)
    assert measure.known is False
    assert measure.basis is EvidenceBasis.UNAVAILABLE
    assert not bool(measure.value)  # and a caller doing `if price:` still gets nothing


def test_price_unknown_when_provider_is_down(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """Same rule for the other way it fails: a 503 is not a price of zero."""
    wired.fail("/token-pairs/v1/solana/", 503)
    measure = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)

    assert measure.value is None
    assert measure.known is False
    assert measure.basis is EvidenceBasis.UNAVAILABLE
    assert prices.liquidity_usd(Chain.SOL, BONK, conn=tmp_db).value is None


def test_an_unknown_price_says_why_it_is_unknown(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """"Unknown" is not enough to debug a stop loss that did not fire. The two reasons —
    nothing listed versus provider down — must be distinguishable from the receipt."""
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_empty"))
    no_pair = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)
    assert no_pair.receipt is not None
    assert "no pair prices this token" in (no_pair.receipt.note or "")

    down = Recorder().fail("/token-pairs/v1/solana/", 503)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "request", down)
        outage = prices.price_usd(Chain.SOL, WSOL, conn=tmp_db)
    assert outage.value is None
    assert "provider unavailable" in (outage.receipt.note or "")
    assert "503" in (outage.receipt.note or "")


def test_a_batch_miss_also_carries_a_receipt(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/tokens/v1/solana/", fixture("tokens_solana_multi"))
    missing = sol_address(11)
    out = prices.prices_usd(Chain.SOL, [WSOL, USDC_SOL, missing], conn=tmp_db)

    assert out[missing].value is None
    assert out[missing].receipt is not None, "a gap in a batch must not be a receiptless zero"
    assert out[missing].receipt.provider == ds.PROVIDER


def test_price_unknown_when_every_pair_quotes_zero(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = copy.deepcopy(fixture("token_pairs_solana_bonk"))
    for pair in raw:
        pair["priceUsd"] = "0"
    wired.add("/token-pairs/v1/solana/", raw)

    assert prices.price_usd(Chain.SOL, BONK, conn=tmp_db).value is None


def test_price_unknown_when_the_token_is_only_ever_the_quote_side(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """``priceUsd`` belongs to the base token. Reading it for a quote-side token would
    report WSOL's price as the memecoin's."""
    raw = copy.deepcopy(fixture("token_pairs_solana_bonk"))
    for pair in raw:
        pair["baseToken"], pair["quoteToken"] = pair["quoteToken"], pair["baseToken"]
    wired.add("/token-pairs/v1/solana/", raw)

    measure = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)
    assert measure.value is None
    assert measure.known is False


def test_price_unknown_for_a_malformed_address_without_calling_out(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    measure = prices.price_usd(Chain.SOL, "not-an-address", conn=tmp_db)
    assert measure.value is None
    assert "not a valid" in (measure.receipt.note or "")
    assert wired.calls == []


def test_price_unknown_on_a_chain_no_source_covers(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    measure = prices.price_usd(Chain.STABLE, WETH, conn=tmp_db)
    assert measure.value is None
    assert "no price source" in (measure.receipt.note or "")
    assert wired.calls == []


def test_price_picks_the_deepest_pool_not_the_first_row(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """A price out of a $200 pool is not a price. Order the response worst-first to prove
    the choice is by liquidity and not by position."""
    raw = copy.deepcopy(fixture("token_pairs_solana_bonk"))
    raw.sort(key=lambda p: p.get("liquidity", {}).get("usd") or 0)
    deepest = max(raw, key=lambda p: p.get("liquidity", {}).get("usd") or 0)
    assert raw[0]["pairAddress"] != deepest["pairAddress"]
    wired.add("/token-pairs/v1/solana/", raw)

    q = prices.quote(Chain.SOL, BONK, conn=tmp_db)
    assert q.pair_address == deepest["pairAddress"]
    assert q.price_usd == Decimal(deepest["priceUsd"])
    assert q.price_usd != Decimal(raw[0]["priceUsd"])
    assert q.pairs_considered == len(raw)


def test_receipt_records_which_pool_the_price_came_from(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("token_pairs_solana_bonk")
    wired.add("/token-pairs/v1/solana/", raw)
    measure = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)

    note = measure.receipt.note or ""
    deepest = max(raw, key=lambda p: p.get("liquidity", {}).get("usd") or 0)
    assert deepest["pairAddress"] in note
    assert deepest["dexId"] in note
    assert "liq_usd=" in note
    assert f"chosen from {len(raw)} pair(s)" in note


def test_usd_is_decimal_and_exactly_what_the_provider_said(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """The string is the lossless form; a float round trip would change the digits."""
    raw = fixture("token_pairs_solana_bonk")
    wired.add("/token-pairs/v1/solana/", raw)
    deepest = max(raw, key=lambda p: p.get("liquidity", {}).get("usd") or 0)

    measure = prices.price_usd(Chain.SOL, BONK, conn=tmp_db)
    assert isinstance(measure.value, Decimal)
    assert not isinstance(measure.value, float)
    assert str(measure.value) == deepest["priceUsd"]
    assert measure.value * Decimal(3) == Decimal(deepest["priceUsd"]) * 3


def test_liquidity_is_the_chosen_pool_not_the_sum(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("token_pairs_solana_bonk")
    wired.add("/token-pairs/v1/solana/", raw)
    deepest = max(raw, key=lambda p: p.get("liquidity", {}).get("usd") or 0)
    total = sum(p.get("liquidity", {}).get("usd") or 0 for p in raw)

    measure = prices.liquidity_usd(Chain.SOL, BONK, conn=tmp_db)
    assert isinstance(measure.value, Decimal)
    assert measure.value == Decimal(str(deepest["liquidity"]["usd"]))
    assert measure.value < Decimal(str(total))


def test_quote_answers_price_and_depth_in_one_request(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    q = prices.quote(Chain.SOL, BONK, conn=tmp_db)

    assert wired.hits("/token-pairs/v1/") == 1
    assert q.known and q.price_usd > 0 and q.liquidity_usd > 0
    assert q.source == ds.PROVIDER
    assert q.as_measure().known and q.liquidity_measure().known


def test_is_thin_treats_unknown_as_unknown_not_as_safe() -> None:
    assert prices.is_thin(Measure(value=Decimal(200), basis=EvidenceBasis.PROVIDER_REPORTED))
    assert not prices.is_thin(Measure(value=Decimal(50_000), basis=EvidenceBasis.PROVIDER_REPORTED))
    assert not prices.is_thin(Measure.unknown()), "unknown depth is not 'deep enough'"


def test_exit_priority_is_accepted_by_the_price_path(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """The exit watchdog reads prices at :attr:`Priority.EXIT`; that call must work and
    must reach the limiter, not be swallowed by a default."""
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    measure = prices.price_usd(Chain.SOL, BONK, priority=Priority.EXIT, conn=tmp_db)
    assert measure.known

    logged = tmp_db.execute(
        "SELECT endpoint FROM provider_calls WHERE provider=? ORDER BY id DESC LIMIT 1",
        (ds.PROVIDER,),
    ).fetchone()
    assert logged["endpoint"] == ds.EP_TOKEN_PAIRS


def test_freshness_budget_follows_max_age(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    measure = prices.price_usd(Chain.SOL, BONK, max_age_s=5.0, conn=tmp_db)
    assert measure.freshness_budget_s == 5
    assert measure.stale is False


# --------------------------------------------------------------------------------------
# prices: batching
# --------------------------------------------------------------------------------------


def test_prices_usd_batches_to_the_thirty_address_cap(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/tokens/v1/solana/", [])
    asked = [sol_address(i) for i in range(45)]
    out = prices.prices_usd(Chain.SOL, asked, conn=tmp_db)

    assert wired.hits("/tokens/v1/") == 2, "45 tokens is two requests, not 45"
    assert len(out) == 45
    assert all(m.value is None and m.known is False for m in out.values())


def test_batch_chunks_are_paced_under_the_real_limiter(
    http: Recorder, tmp_db, monkeypatch: pytest.MonkeyPatch, clean_sources
) -> None:  # noqa: ANN001
    """Same trap as ``poll_alpha``: without pacing, chunk two is refused unread and 15 of
    45 positions silently have no price. Real limits, virtual sleeping."""
    from kaiba.core import limiter
    from kaiba.core.schemas import now_ms

    clock = {"ms": now_ms()}
    slept: list[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["ms"] += int(seconds * 1000)

    monkeypatch.setattr(limiter, "now_ms", lambda: clock["ms"])
    monkeypatch.setattr(ds.time, "sleep", fake_sleep)
    http.add("/tokens/v1/solana/", fixture("tokens_solana_multi"))

    asked = [sol_address(i) for i in range(45)]
    out = prices.prices_usd(Chain.SOL, asked, conn=tmp_db)

    assert http.hits("/tokens/v1/") == 2, "the second chunk must actually reach the provider"
    assert len(slept) == 1, "one gap between two chunks, and none before the first"
    assert len(out) == 45


def test_prices_usd_uses_the_batch_route_and_prices_what_it_finds(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    raw = fixture("tokens_solana_multi")
    wired.add("/tokens/v1/solana/", raw)
    out = prices.prices_usd(Chain.SOL, [WSOL, USDC_SOL], conn=tmp_db)

    assert wired.hits("/tokens/v1/") == 1
    by_address = {p["baseToken"]["address"]: p for p in raw}
    assert out[WSOL].value == Decimal(by_address[WSOL]["priceUsd"])
    assert out[USDC_SOL].value == Decimal(by_address[USDC_SOL]["priceUsd"])


def test_prices_usd_returns_unknown_for_tokens_the_batch_missed(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """Every input key is present in the result; a missing key would let a caller's
    ``.get(token, 0)`` turn a gap into a zero."""
    wired.add("/tokens/v1/solana/", fixture("tokens_solana_multi"))
    missing = sol_address(99)
    out = prices.prices_usd(Chain.SOL, [WSOL, missing], conn=tmp_db)

    assert set(out) == {WSOL, missing}
    assert out[WSOL].known
    assert out[missing].value is None and out[missing].known is False


def test_prices_usd_is_keyed_by_the_caller_s_own_strings(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """EVM addresses get lowercased internally; the caller should not have to know.

    The two spellings collapse to one address, so this takes the single-token route.
    """
    wired.add("/token-pairs/v1/ethereum/", fixture("tokens_ethereum_weth"))
    mixed_case = WETH
    assert mixed_case != mixed_case.lower()
    out = prices.prices_usd(Chain.ETH, [mixed_case, mixed_case.lower()], conn=tmp_db)

    assert set(out) == {mixed_case, mixed_case.lower()}
    assert out[mixed_case].known and out[mixed_case.lower()].known
    assert out[mixed_case].value == out[mixed_case.lower()].value
    assert wired.hits("/token-pairs/v1/") == 1, "one address asked for twice is still one lookup"


def test_prices_usd_marks_a_bad_address_unknown_without_poisoning_the_batch(wired, tmp_db) -> None:  # noqa: ANN001
    wired.add("/tokens/v1/solana/", fixture("tokens_solana_multi"))
    out = prices.prices_usd(Chain.SOL, [WSOL, "0xdeadbeef", USDC_SOL], conn=tmp_db)

    assert out["0xdeadbeef"].value is None
    assert out[WSOL].known and out[USDC_SOL].known
    sent = wired.calls[0].rsplit("/", 1)[-1]
    assert "0xdeadbeef" not in sent


def test_prices_usd_of_one_token_uses_the_full_pair_list(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    """Single-token lookups take the accurate route: every pool, our own pick."""
    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    out = prices.prices_usd(Chain.SOL, [BONK], conn=tmp_db)

    assert wired.hits("/token-pairs/v1/") == 1
    assert wired.hits("/tokens/v1/") == 0
    assert out[BONK].known


def test_empty_batch_is_an_empty_dict(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    assert prices.prices_usd(Chain.SOL, [], conn=tmp_db) == {}
    assert wired.calls == []


# --------------------------------------------------------------------------------------
# prices: pluggability, without inventing a second provider
# --------------------------------------------------------------------------------------


class StubSource:
    """A stand-in for a future source. It answers only what it was told, which is the
    contract: omit a token rather than guess at it."""

    name = "stub"

    def __init__(self, answers: dict[str, tuple[str, str]]) -> None:
        self.answers = answers
        self.asked: list[list[str]] = []

    def quotes(self, chain, tokens, *, max_age_s, priority, conn):  # noqa: ANN001, ANN201
        self.asked.append(list(tokens))
        out = {}
        for t in tokens:
            if t in self.answers:
                price, liq = self.answers[t]
                out[t] = prices.Quote(
                    chain=chain,
                    token=t,
                    price_usd=Decimal(price),
                    liquidity_usd=Decimal(liq),
                    receipt=Receipt(provider=self.name, endpoint="stub.quote"),
                    source=self.name,
                )
        return out


def test_a_second_source_fills_gaps_dexscreener_could_not(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    wired.add("/tokens/v1/solana/", fixture("tokens_solana_multi"))
    orphan = sol_address(7)
    stub = StubSource({orphan: ("1.25", "40000")})
    prices.register_source(stub)

    out = prices.prices_usd(Chain.SOL, [WSOL, orphan], conn=tmp_db)
    assert out[WSOL].known and out[orphan].value == Decimal("1.25")
    assert stub.asked == [[orphan]], "a later source is only asked about what is still missing"


def test_register_source_is_idempotent_and_ordered(clean_sources) -> None:  # noqa: ANN001
    stub = StubSource({})
    prices.register_source(stub)
    prices.register_source(stub)
    names = [s.name for s in prices.sources()]
    assert names == [ds.PROVIDER, "stub"]

    prices.register_source(StubSource({}), first=True)
    assert [s.name for s in prices.sources()] == ["stub", ds.PROVIDER]
    prices.reset_sources()
    assert [s.name for s in prices.sources()] == [ds.PROVIDER]


def test_a_source_that_raises_does_not_take_the_price_with_it(wired: Recorder, tmp_db) -> None:  # noqa: ANN001
    class Broken:
        name = "broken"

        def quotes(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("upstream exploded")

    wired.add("/token-pairs/v1/solana/", fixture("token_pairs_solana_bonk"))
    prices.register_source(Broken(), first=True)

    assert prices.price_usd(Chain.SOL, BONK, conn=tmp_db).known
    out = prices.prices_usd(Chain.SOL, [BONK], conn=tmp_db)
    assert out[BONK].known


# --------------------------------------------------------------------------------------
# live — skipped unless KAIBA_LIVE_TESTS=1
# --------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_wsol_has_a_believable_price(tmp_db) -> None:  # noqa: ANN001
    measure = prices.price_usd(Chain.SOL, WSOL, conn=tmp_db)
    assert measure.known and isinstance(measure.value, Decimal)
    assert Decimal(1) < measure.value < Decimal(100_000)
    assert prices.liquidity_usd(Chain.SOL, WSOL, conn=tmp_db).value > Decimal(100_000)


@pytest.mark.live
def test_live_nonexistent_token_is_unknown_not_zero(tmp_db) -> None:  # noqa: ANN001
    measure = prices.price_usd(Chain.SOL, "11111111111111111111111111111112", conn=tmp_db)
    assert measure.value is None


@pytest.mark.live
def test_live_discovery_endpoints_still_have_the_shape_we_recorded(tmp_db) -> None:  # noqa: ANN001
    import time

    profiles, r1 = ds.token_profiles_latest(conn=tmp_db)
    time.sleep(1.5)  # the real minimum interval; the limiter refuses anything faster
    boosts, r2 = ds.token_boosts_latest(conn=tmp_db)
    assert r1.basis is not EvidenceBasis.UNAVAILABLE and profiles
    assert r2.basis is not EvidenceBasis.UNAVAILABLE and boosts
    assert all(isinstance(p.cto, bool) for p in profiles)
