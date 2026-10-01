"""The native-to-USD time series: nearest-sample lookup, the tolerance, and the sampler.

The property under test is not coverage; it is that :func:`native_price.at` can only ever
answer with a sample that was actually taken near the asked instant, and says how near.
A lookup that quietly served the latest price for an old fill would put a wrong number
into a stop on real money, so the refusal paths matter more than the happy path.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Receipt
from kaiba.providers import native_price as npx
from kaiba.providers._http import Fetched

FIXTURES = Path(__file__).parent / "fixtures" / "dexscreener"
T0 = 1_789_890_000_000


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def ok(data: Any, observed_at_ms: int) -> Fetched:
    return Fetched(
        data,
        Receipt(provider="dexscreener", endpoint="price.token_pairs", observed_at_ms=observed_at_ms,
                basis=EvidenceBasis.PROVIDER_REPORTED),
    )


def down(note: str = "ConnectError: refused") -> Fetched:
    return Fetched(None, Receipt(provider="dexscreener", endpoint="price.token_pairs",
                                 basis=EvidenceBasis.UNAVAILABLE, note=note))


@pytest.fixture
def http(monkeypatch):
    """Queue DexScreener answers; record every call the module makes."""
    state: dict[str, Any] = {"queue": [], "calls": []}

    def fake(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        state["calls"].append({"provider": provider, "endpoint": endpoint, "url": url, **kw})
        return state["queue"].pop(0) if state["queue"] else down("no stubbed response")

    monkeypatch.setattr(npx, "get_json", fake)
    return state


def put_sample(conn, ts_ms: int, price: str, chain: Chain = Chain.SOL, source: str = "test") -> None:
    npx.record(
        npx.NativeSample(
            chain=chain, ts_ms=ts_ms, price_usd=Decimal(price), source=source, pair="t:SOL/USDC:x",
            liquidity_usd=Decimal("5000000"),
            receipt=Receipt(provider=source, endpoint="x", observed_at_ms=ts_ms,
                            basis=EvidenceBasis.PROVIDER_REPORTED),
        ),
        conn,
    )


# --------------------------------------------------------------------------------------
# fetching a sample
# --------------------------------------------------------------------------------------


def test_fetch_prices_the_native_from_the_deepest_stable_pool(tmp_db, http):
    """The WSOL fixture has 30 pools; the deepest one that prices SOL itself is Raydium SOL/USDC."""
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is not None
    assert got.price_usd == Decimal("108.73")
    assert got.pair is not None and "raydium:SOL/USDC" in got.pair
    assert got.liquidity_usd is not None and got.liquidity_usd > Decimal(39_000_000)
    assert got.ts_ms == T0, "a sample is stamped with the provider's answer time"


def test_fetch_waits_for_limiter_capacity_and_never_caches(tmp_db, http):
    """CONTRACT: wait_for_slot_s is set; a sample is an observation, so ttl is zero."""
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0))
    npx.fetch(Chain.SOL, conn=tmp_db)
    call = http["calls"][0]
    assert call["wait_for_slot_s"] > 0
    assert call["ttl_s"] == 0
    assert call["provider"] == "dexscreener"
    assert "So11111111111111111111111111111111111111112" in call["url"]


@pytest.fixture(autouse=True)
def _no_gmgn_fallback(monkeypatch):
    """Silence the 2026-09-23 fallback by default.

    `fetch` now asks GMGN when DexScreener has no capacity, which is the point of it --
    but every test in this file is about the DexScreener path, and an unstubbed fallback
    would either shell out to a real CLI or quietly answer for it. Tests that want the
    fallback ask for it explicitly.
    """
    def unavailable(address, chain):
        raise RuntimeError("gmgn is not part of this test")

    monkeypatch.setattr(npx, "_gmgn_token_info", unavailable)


def test_a_dead_provider_yields_no_sample_and_no_exception(tmp_db, http):
    """Dead means BOTH sources dead: the fallback is stubbed out by the fixture above."""
    http["queue"].append(down("HTTP 503"))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "503" in (receipt.note or "")


def test_a_thin_pool_is_refused_as_a_reference(tmp_db, http):
    rows = [r for r in fixture("token_pairs_solana_wsol")
            if r["baseToken"]["address"] == npx.WRAPPED_NATIVE[Chain.SOL]]
    thin = [dict(r, liquidity={"usd": Decimal("500")}) for r in rows[:2]]
    http["queue"].append(ok(thin, T0))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None
    assert "floor" in (receipt.note or "")


def test_pairs_where_sol_is_the_quote_side_are_not_used(tmp_db, http):
    """A BONK/SOL pool's priceUsd is BONK's. It must never be read as SOL's."""
    bonk = fixture("token_pairs_solana_bonk")
    http["queue"].append(ok(bonk, T0))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None
    assert "no pair prices sol native" in (receipt.note or "")


def test_chains_without_a_reference_source_say_so(tmp_db, http):
    got, receipt = npx.fetch(Chain.ROBINHOOD, conn=tmp_db)
    assert got is None
    assert http["calls"] == []
    assert "no native reference source" in (receipt.note or "")


def test_sample_records_the_row_as_text_decimal(tmp_db, http):
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0))
    assert npx.sample(Chain.SOL, tmp_db) is not None
    rows = fetch_all(tmp_db, "SELECT * FROM native_prices", ())
    assert len(rows) == 1
    assert rows[0]["price_usd"] == "108.73"
    assert rows[0]["ts_ms"] == T0
    assert isinstance(rows[0]["price_usd"], str)


def test_recording_the_same_instant_twice_is_a_no_op(tmp_db):
    put_sample(tmp_db, T0, "100")
    put_sample(tmp_db, T0, "999")
    rows = fetch_all(tmp_db, "SELECT price_usd FROM native_prices", ())
    assert [r["price_usd"] for r in rows] == ["100"]


# --------------------------------------------------------------------------------------
# the lookup
# --------------------------------------------------------------------------------------


def test_at_returns_the_nearest_sample_and_its_distance(tmp_db):
    put_sample(tmp_db, T0, "100")
    put_sample(tmp_db, T0 + 30_000, "101")
    put_sample(tmp_db, T0 + 60_000, "102")
    got = npx.at(Chain.SOL, T0 + 40_000, tmp_db)
    assert got.known
    assert got.price_usd == Decimal("101")
    assert got.sample_ts_ms == T0 + 30_000
    assert got.distance_ms == 10_000
    assert got.basis is EvidenceBasis.PROVIDER_REPORTED
    assert "10000 ms away" in (got.receipt.note or "")


def test_at_picks_the_nearer_side_when_a_sample_exists_on_both(tmp_db):
    put_sample(tmp_db, T0, "100")
    put_sample(tmp_db, T0 + 30_000, "101")
    assert npx.at(Chain.SOL, T0 + 20_000, tmp_db).price_usd == Decimal("101")
    assert npx.at(Chain.SOL, T0 + 10_000, tmp_db).price_usd == Decimal("100")


def test_at_refuses_a_sample_outside_the_tolerance(tmp_db):
    """The whole point: no sample near the fill means no USD figure, never the latest one."""
    put_sample(tmp_db, T0, "100")
    got = npx.at(Chain.SOL, T0 + npx.DEFAULT_TOLERANCE_MS + 1, tmp_db)
    assert not got.known
    assert got.price_usd is None
    assert got.basis is EvidenceBasis.UNAVAILABLE
    assert got.distance_ms == npx.DEFAULT_TOLERANCE_MS + 1
    assert "not contemporaneous" in (got.receipt.note or "")


def test_at_within_tolerance_on_the_boundary_is_accepted(tmp_db):
    put_sample(tmp_db, T0, "100")
    got = npx.at(Chain.SOL, T0 + npx.DEFAULT_TOLERANCE_MS, tmp_db)
    assert got.known and got.distance_ms == npx.DEFAULT_TOLERANCE_MS


def test_at_with_no_samples_at_all_says_so_distinctly(tmp_db):
    got = npx.at(Chain.SOL, T0, tmp_db)
    assert not got.known
    assert got.distance_ms is None and got.sample_ts_ms is None
    assert "no sol native price samples" in (got.receipt.note or "")


def test_at_never_reads_another_chains_sample(tmp_db):
    put_sample(tmp_db, T0, "2500", chain=Chain.ETH)
    assert not npx.at(Chain.SOL, T0, tmp_db).known
    assert npx.at(Chain.ETH, T0, tmp_db).price_usd == Decimal("2500")


def test_a_custom_tolerance_is_honoured(tmp_db):
    put_sample(tmp_db, T0, "100")
    assert not npx.at(Chain.SOL, T0 + 5_000, tmp_db, tolerance_ms=4_000).known
    assert npx.at(Chain.SOL, T0 + 5_000, tmp_db, tolerance_ms=5_000).known


def test_the_receipt_carries_the_sample_time_not_the_read_time(tmp_db):
    """``Measure.stale`` depends on this; stamping the read time would make an hour-old
    sample look fresh."""
    put_sample(tmp_db, T0, "100")
    got = npx.at(Chain.SOL, T0 + 1_000, tmp_db)
    assert got.receipt.observed_at_ms == T0


def test_prices_never_pass_through_a_float(tmp_db, monkeypatch):
    def banned(*a, **kw):
        raise AssertionError("float() must never touch a price")

    monkeypatch.setattr(npx, "float", banned, raising=False)
    put_sample(tmp_db, T0, "108.730000000000000001")
    got = npx.at(Chain.SOL, T0, tmp_db)
    assert got.price_usd == Decimal("108.730000000000000001")


def test_latest_and_history(tmp_db):
    for i in range(5):
        put_sample(tmp_db, T0 + i * 30_000, str(100 + i))
    assert npx.latest(Chain.SOL, tmp_db).price_usd == Decimal("104")
    window = npx.history(Chain.SOL, T0 + 30_000, T0 + 90_000, tmp_db)
    assert [s.price_usd for s in window] == [Decimal("101"), Decimal("102"), Decimal("103")]


def test_ensure_recent_only_fetches_when_the_latest_is_old(tmp_db, http, monkeypatch):
    monkeypatch.setattr(npx, "now_ms", lambda: T0 + 10_000)
    put_sample(tmp_db, T0, "100")
    assert npx.ensure_recent(Chain.SOL, tmp_db, max_age_ms=60_000).price_usd == Decimal("100")
    assert http["calls"] == []
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0 + 9_000))
    got = npx.ensure_recent(Chain.SOL, tmp_db, max_age_ms=5_000)
    assert got is not None and got.price_usd == Decimal("108.73")
    assert len(http["calls"]) == 1


def test_prune_drops_only_old_rows(tmp_db, monkeypatch):
    monkeypatch.setattr(npx, "now_ms", lambda: T0 + 100 * 86_400_000)
    put_sample(tmp_db, T0, "100")
    put_sample(tmp_db, T0 + 95 * 86_400_000, "101")
    assert npx.prune(tmp_db, keep_days=90) == 1
    assert [r["price_usd"] for r in fetch_all(tmp_db, "SELECT price_usd FROM native_prices", ())] == ["101"]


# --------------------------------------------------------------------------------------
# the sampler loop
# --------------------------------------------------------------------------------------


def test_run_sampler_records_one_sample_per_chain_per_tick(tmp_db, http):
    sleeps: list[float] = []
    for tick in range(3):
        http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0 + tick * 30_000))
    report = npx.run_sampler([Chain.SOL], interval_s=30.0, conn=tmp_db, max_iterations=3,
                             sleep=sleeps.append)
    assert report.iterations == 3
    assert report.recorded == 3
    assert report.failures == 0
    assert len(sleeps) == 2, "no sleep after the final tick"
    assert all(0 < s <= 30.0 for s in sleeps)
    assert len(fetch_all(tmp_db, "SELECT * FROM native_prices", ())) == 3


def test_run_sampler_counts_failures_and_keeps_going(tmp_db, http):
    http["queue"].extend([down("503"), ok(fixture("token_pairs_solana_wsol"), T0 + 30_000)])
    report = npx.run_sampler([Chain.SOL], interval_s=1.0, conn=tmp_db, max_iterations=2, sleep=lambda s: None)
    assert report.failures == 1 and report.recorded == 1
    assert report.per_chain["sol"] == {"recorded": 1, "duplicates": 0, "failures": 1}


def test_run_sampler_shouts_after_five_consecutive_failures(tmp_db, http):
    http["queue"].extend([down("503")] * 6)
    npx.run_sampler([Chain.SOL], interval_s=1.0, conn=tmp_db, max_iterations=6, sleep=lambda s: None)
    starved = [e for e in ev.recent(conn=tmp_db)
               if e.kind == EventKind.SYSTEM and e.payload.get("event") == "native_price_sampler_starved"]
    assert len(starved) == 1, "one warning, deduped, not one per tick"
    assert starved[0].level == "warn"
    assert starved[0].payload["chain"] == "sol"


def test_run_sampler_stops_on_the_event(tmp_db, http):
    stop = threading.Event()
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0))

    def sleep_then_stop(_: float) -> None:
        stop.set()

    report = npx.run_sampler([Chain.SOL], interval_s=30.0, conn=tmp_db, stop=stop, sleep=sleep_then_stop)
    assert report.iterations == 1 and report.recorded == 1


def test_run_sampler_treats_a_repeated_answer_time_as_a_duplicate(tmp_db, http):
    http["queue"].extend([ok(fixture("token_pairs_solana_wsol"), T0)] * 2)
    report = npx.run_sampler([Chain.SOL], interval_s=1.0, conn=tmp_db, max_iterations=2, sleep=lambda s: None)
    assert report.recorded == 1 and report.duplicates == 1


def test_run_sampler_defaults_to_every_chain_with_a_source(tmp_db, http):
    report = npx.run_sampler(interval_s=1.0, conn=tmp_db, max_iterations=1, sleep=lambda s: None)
    assert set(report.per_chain) == {c.value for c in npx.WRAPPED_NATIVE}


# ------------------------------------- the fallback: a starved bucket is not a dead chain
#
# MEASURED 2026-09-23 on the live box. `price.token_pairs` ran at 60.1 calls/min against a
# bucket refilling at 60/min, so `dexscreener` sat at credit_milli = -18273 permanently and
# this sampler -- which wants three rows a minute -- got none for 1h54m. A missing native
# price is not cosmetic: `evm_cost_model` cannot value a round trip without it, so every
# chain refused every entry with `no_viable_band`. Robinhood took 25 of 37 skips that way
# in 90 minutes.


@pytest.fixture
def gmgn(monkeypatch):
    """Drive the fallback seam. `calls` proves whether it was reached at all."""
    calls: list = []

    def fake(address, chain):
        calls.append((address, chain))
        return type("R", (), {"data": {"price": {"price": "118.1417824"}}})()

    monkeypatch.setattr(npx, "_gmgn_token_info", fake)
    return calls


def test_a_starved_bucket_falls_back_rather_than_reporting_nothing(tmp_db, http, gmgn):
    """THE REGRESSION: this returned None for 1h54m and halted sizing on every chain."""
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is not None, "the fallback never answered"
    assert got.price_usd == Decimal("118.1417824")
    assert gmgn, "the fallback was never asked"


def test_the_fallback_says_where_the_price_came_from(tmp_db, http, gmgn):
    """A reader must always be able to tell which source answered."""
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got.source == "gmgn"
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED


def test_the_fallback_claims_no_liquidity_it_cannot_see(tmp_db, http, gmgn):
    """It is a price, not a pool. Nothing downstream may read depth into it."""
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got.liquidity_usd is None and got.pair is None


def test_a_healthy_bucket_never_reaches_the_fallback(tmp_db, http, gmgn):
    """DexScreener stays the primary: it gives the pair and the depth this cannot."""
    http["queue"].append(ok(fixture("token_pairs_solana_wsol"), T0))
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is not None and got.source != "gmgn"
    assert gmgn == [], "gmgn was asked while dexscreener was answering"


@pytest.mark.parametrize("bad", ["0", "-1", "", "abc", None])
def test_an_unusable_fallback_price_is_refused(tmp_db, http, monkeypatch, bad):
    monkeypatch.setattr(
        npx, "_gmgn_token_info",
        lambda address, chain: type("R", (), {"data": {"price": {"price": bad}}})(),
    )
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    got, receipt = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE


def test_a_raising_fallback_is_no_price_not_a_crash(tmp_db, http, monkeypatch):
    def boom(address, chain):
        raise RuntimeError("cli died")

    monkeypatch.setattr(npx, "_gmgn_token_info", boom)
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    got, _ = npx.fetch(Chain.SOL, conn=tmp_db)
    assert got is None


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.ETH, Chain.BSC])
def test_the_fallback_asks_for_that_chains_own_wrapped_native(tmp_db, http, gmgn, chain):
    """A price for the wrong asset is worse than no price: it would size against it."""
    http["queue"].append(down("rate limited: dexscreener: bucket exhausted"))
    npx.fetch(chain, conn=tmp_db)
    assert gmgn and gmgn[-1][0] == npx.WRAPPED_NATIVE[chain]
    assert gmgn[-1][1] is chain
