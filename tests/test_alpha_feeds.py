"""Offline tests for the shadow alpha-feed recorder (kaiba/ingest/alpha_feeds.py).

Fixtures are SYNTHETIC rows shaped exactly like responses captured from the box on
2026-10-04 (Binance Web3 meme-rush / topic-rush / smart-money, gmgn-cli 1.6.1
hot-searches / created-tokens): same keys, same nesting, same units (ms vs s, string
numbers, ``chainId`` codes). Addresses and wallets are invented. The ``100004`` reply's
exact body was never observed (we were not rate limited); it is built from the vendor's
documented business code and envelope.

The Binance seam is an ``httpx.MockTransport``; the gmgn seam is ``gmgn_cli._spawn``, as
in tests/test_gmgn_cli.py. Nothing here reaches a network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from kaiba.core.db import fetch_all
from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis
from kaiba.ingest import alpha_feeds as af
from kaiba.providers import gmgn_cli as g

# ------------------------------------------------------------------ synthetic fixtures

SOL_TOKEN_A = "SyntheticTokenAAAA1111111111111111111111pump"
SOL_TOKEN_B = "SyntheticTokenBBBB2222222222222222222222pump"
SOL_TOKEN_C = "SyntheticTokenCCCC3333333333333333333333pump"
SOL_DEV = "SyntheticDevWallet111111111111111111111111111"
RH_TOKEN_MIXED = "0xAbCdEf0000000000000000000000000000000001"
RH_CREATOR = "0x00000000000000000000000000000000000000De"
T0 = 1_791_114_191_000  # 2026-10-04, the capture day


def meme_row(address: str, *, narrative: str | None = "Dog AI, Robot Meme", dev: str = SOL_DEV) -> dict[str, Any]:
    return {
        "chainId": "CT_501", "contractAddress": address, "symbol": "SYN", "symbolTranslate": None,
        "name": "Synthetic", "nameTranslate": None, "decimals": 6,
        "icon": "https://example.invalid/icon.png", "price": "0.0000034", "priceChange": "12.5",
        "marketCap": "3438.07", "liquidity": "5200.1", "volume": "880.4", "holders": 32,
        "progress": "41.8", "protocol": 1001, "exclusive": 0, "count": 120, "countBuy": 80,
        "countSell": 40, "holdersTop10Percent": "22.1", "holdersDevPercent": "3.0",
        "devAddress": dev, "devSellPercent": "0", "devMigrateCount": 11, "devPosition": 1,
        "migrateStatus": 0, "migrateTime": None, "createTime": T0 - 60_000,
        "tagDevWashTrading": None, "tagInsiderWashTrading": 1, "auditInfoJson": "{\"x\":1}",
        "twitterInfo": {"followers": 10}, "socials": {"twitter": "https://x.com/example"},
        "narrativeText": None if narrative is None else {"en": narrative, "cn": "狗"},
    }


def envelope(data: Any, code: str = "000000") -> dict[str, Any]:
    return {"code": code, "message": None, "messageDetail": None, "data": data, "success": code == "000000"}


RATE_LIMITED_BODY = {"code": "100004", "message": "too many requests", "messageDetail": None,
                     "data": None, "success": False}

TOPIC = {
    "topicId": "00000000-0000-0000-0000-000000000001", "chainId": "CT_501",
    "name": {"topicNameEn": "Synthetic Owl Meme", "topicNameCn": "合成猫头鹰"},
    "type": "Culture", "close": 0, "topicLink": "https://x.com/example/status/1",
    "createTime": T0 - 120_000, "progress": "44.6",
    "aiSummary": {"aiSummaryEn": "A post claims the owl is superintelligent.", "aiSummaryCn": "猫头鹰"},
    "topicNetInflow": "-137.1", "topicNetInflow1h": "285.2", "topicNetInflowAth": "3201.1",
    "tokenSize": 2, "deepAnalysisFlag": 0, "topicTags": ["Celebrity", "AI Theme"],
    "tokenList": [
        {"chainId": "CT_501", "contractAddress": SOL_TOKEN_A, "symbol": "OWL", "icon": "/x.png",
         "createTime": T0 - 300_000, "marketCap": "713029", "netInflow": "120.4", "holders": 900},
        {"chainId": "CT_501", "contractAddress": SOL_TOKEN_B, "symbol": "OWL2", "icon": "/y.png",
         "createTime": T0 - 200_000, "marketCap": "1000", "netInflow": "3.1", "holders": 12},
    ],
}

SIGNAL_RH = {
    "signalId": 58832, "ticker": "SYNRH", "chainId": "4663", "contractAddress": RH_TOKEN_MIXED,
    "logoUrl": "/images/x.webp", "chainLogoUrl": "https://example.invalid/robinhood.png",
    "tokenDecimals": 18, "isAlpha": False, "launchPlatform": None,
    "tokenTag": {"Social Events": [{"tagName": "DEX Paid"}]}, "smartSignalType": "SMART_MONEY",
    "smartMoneyCount": 3, "direction": "buy", "timeFrame": 504000, "signalTriggerTime": T0 - 3_600_000,
    "alertPrice": "0.00016", "alertMarketCap": "160507.5", "currentPrice": "0.00085",
    "currentMarketCap": "850211.4", "highestPrice": "0.000915", "highestPriceTime": T0,
    "exitRate": 64, "status": "valid", "maxGain": "4.7028", "signalCount": 1,
}

HOT_SEARCHES = [
    {"interval": "5m", "chain": "sol", "version": "SYNTH", "tokens": [
        {"address": SOL_TOKEN_A, "symbol": "OWL", "name": "Synthetic Owl", "rank": 1,
         "visiting_count": 987, "market_cap": 713029, "open_timestamp": (T0 // 1000) - 2400,
         "creation_timestamp": (T0 // 1000) - 2500, "creator": SOL_DEV, "logo": "https://x/l.png",
         "trans_name": "x", "launchpad": "ray_launchpad", "chain": "sol"},
        {"address": SOL_TOKEN_C, "symbol": "SIF", "name": "Synthetic Force", "rank": 2,
         "visiting_count": 410, "market_cap": 9000, "open_timestamp": None,
         "creation_timestamp": (T0 // 1000) - 900, "creator": None, "chain": "sol"},
    ]},
    {"interval": "5m", "chain": "robinhood", "version": "SYNTH", "tokens": [
        {"address": RH_TOKEN_MIXED, "symbol": "MEME", "name": "Synthetic RH", "rank": 1,
         "visiting_count": 55, "open_timestamp": (T0 // 1000) - 86_400, "creator": RH_CREATOR,
         "chain": "robinhood"},
    ]},
]

CREATED_TOKENS = {
    "last_create_timestamp": T0 // 1000, "inner_count": 1537, "open_count": 29, "open_ratio": 0.019,
    "creator_ath_info": {"creator": SOL_DEV, "ath_token": SOL_TOKEN_C, "ath_mc": "231358.5",
                         "token_symbol": "SIF", "token_logo": "https://x/l.png", "token_name": "SF"},
    "tokens": [
        {"token_address": SOL_TOKEN_A, "symbol": "OWL", "token_ath_mc": "40327.9", "market_cap": "3000",
         "is_open": False, "create_timestamp": T0 // 1000, "launchpad_platform": "Pump.fun", "holders": 30,
         "logo": "https://x/l.png"},
        {"token_address": SOL_TOKEN_C, "symbol": "SIF", "token_ath_mc": "231358.5", "market_cap": "90000",
         "is_open": True, "create_timestamp": (T0 // 1000) - 99_999, "launchpad_platform": "Pump.fun",
         "holders": 900},
    ],
}


# ----------------------------------------------------------------------------- helpers


class Vendor:
    """A Binance Web3 stand-in. ``replies`` is consumed in order; the last one repeats."""

    def __init__(self, *replies: tuple[int, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        return httpx.Response(status, json=body)


@pytest.fixture
def fast_binance(monkeypatch):
    """Drop only the binance_web3 spacing, so one poll can ask two chains back to back.

    Families, cooldowns and weights stay real; in production the poller sleeps the
    limiter's own interval between chains instead.
    """
    import dataclasses

    from kaiba.core import limiter as lim

    real = lim.limits_for

    def relaxed(provider: str):
        base = real(provider)
        return dataclasses.replace(base, min_interval_ms=0) if provider == af.BINANCE_PROVIDER else base

    monkeypatch.setattr(lim, "limits_for", relaxed)


@pytest.fixture
def gmgn_replay(monkeypatch):
    calls: list[list[str]] = []

    def install(payload: Any, returncode: int = 0, stderr: str = ""):
        def fake_spawn(argv: list[str], timeout_s: float) -> g._Raw:
            calls.append(list(argv))
            return g._Raw(returncode, json.dumps(payload), stderr)

        monkeypatch.setattr(g, "_spawn", fake_spawn)
        return calls

    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])
    return install


def rows_of(conn, table: str = "alpha_feed_rows") -> list[dict[str, Any]]:
    return fetch_all(conn, f"SELECT * FROM {table} ORDER BY rowid", ())


# ------------------------------------------------------------------------------ parsing


def test_meme_rush_reads_narrative_dev_and_launch_time():
    rows = af.parse_meme_rush([meme_row(SOL_TOKEN_A), meme_row(SOL_TOKEN_B, narrative=None)],
                              af.Chain.SOL, "finalizing")
    assert [r.rank for r in rows] == [1, 2]
    a, b = rows
    assert a.feed == "binance:meme_rush:finalizing" and a.chain == "sol"
    assert a.token_address == SOL_TOKEN_A  # Solana keeps its case
    assert a.narrative == "Dog AI, Robot Meme"
    assert a.context == SOL_DEV
    assert a.source_ts_ms == T0 - 60_000
    assert b.narrative is None  # no narrative yet is None, never ""


def test_raw_json_drops_images_translations_and_respects_the_cap():
    row = meme_row(SOL_TOKEN_A)
    row["description"] = "x" * 5000
    text = af.cap_raw(row)
    doc = json.loads(text)
    assert len(text.encode()) <= af.RAW_CAP_BYTES
    for dropped in ("icon", "auditInfoJson", "twitterInfo", "symbolTranslate"):
        assert dropped not in doc
    assert doc["devMigrateCount"] == 11 and doc["tagInsiderWashTrading"] == 1
    assert doc["description"].endswith("...[cut]")


def test_oversized_raw_is_marked_truncated_and_stays_valid_json():
    huge = {f"k{i}": "\"quoted\" " * 50 for i in range(200)}
    text = af.cap_raw(huge, cap=1024)
    doc = json.loads(text)
    assert doc["_truncated"] is True and doc["_bytes"] > 1024
    assert len(text.encode()) <= 1024


def test_topic_rush_one_row_per_topic_token_with_topic_narrative():
    rows = af.parse_topic_rush([TOPIC], af.Chain.SOL, "latest")
    assert [r.token_address for r in rows] == [SOL_TOKEN_A, SOL_TOKEN_B]
    assert all(r.rank == 1 and r.context == TOPIC["topicId"] for r in rows)
    assert rows[0].narrative.startswith("Synthetic Owl Meme [Culture, Celebrity, AI Theme]: A post claims")
    assert rows[0].source_ts_ms == TOPIC["createTime"]
    assert "tokenList" not in json.loads(af.cap_raw(rows[0].raw))["topic"]


def test_smart_money_on_robinhood_is_lowercased_and_keyed_by_signal():
    (row,) = af.parse_smart_money([SIGNAL_RH], af.Chain.SOL)
    assert row.chain == "robinhood"  # chainId 4663 wins over the requested chain
    assert row.token_address == RH_TOKEN_MIXED.lower()
    assert row.context == "58832:buy"
    assert row.source_ts_ms == SIGNAL_RH["signalTriggerTime"]
    assert json.loads(af.cap_raw(row.raw))["maxGain"] == "4.7028"


def test_hot_searches_blocks_seconds_become_ms_and_rank_is_the_vendors():
    rows = af.parse_hot_searches(HOT_SEARCHES, "5m")
    assert [(r.chain, r.rank) for r in rows] == [("sol", 1), ("sol", 2), ("robinhood", 1)]
    assert rows[0].source_ts_ms == ((T0 // 1000) - 2400) * 1000
    assert rows[1].source_ts_ms == ((T0 // 1000) - 900) * 1000  # falls back to creation
    assert rows[0].context == SOL_DEV and rows[1].context is None
    assert rows[2].token_address == RH_TOKEN_MIXED.lower() and rows[2].context == RH_CREATOR.lower()


def test_binance_envelope_codes():
    assert af.binance_data(envelope([{"a": 1}])) == [{"a": 1}]
    assert af.binance_data(envelope(None)) == []
    with pytest.raises(af.BinanceRateLimited):
        af.binance_data(RATE_LIMITED_BODY)
    with pytest.raises(af.BinanceError, match="100002"):
        af.binance_data(envelope(None, code="100002"))
    with pytest.raises(af.BinanceError):
        af.binance_data([1, 2])


# ------------------------------------------------------------------- rate limit backoff


def test_100004_books_a_rate_limit_and_the_next_call_never_reaches_the_vendor(tmp_db):
    vendor = Vendor((200, RATE_LIMITED_BODY))
    spec = af.FEEDS["binance:meme_rush:finalizing"]
    first = af.poll_feed(spec, conn=tmp_db, client=vendor.client)
    assert first.status == "rate_limited" and first.rows == []
    assert len(vendor.requests) == 1
    calls = fetch_all(tmp_db, "SELECT endpoint, status FROM provider_calls WHERE provider=?", (af.BINANCE_PROVIDER,))
    assert calls == [{"endpoint": "web3.meme_rush", "status": "rate_limited"}]
    bans = fetch_all(tmp_db, "SELECT family FROM provider_family_bans WHERE provider=?", (af.BINANCE_PROVIDER,))
    assert bans == [{"family": "web3"}]

    # Every Binance endpoint shares the "web3" family: the cooldown covers the others too,
    # and our own limiter refuses before anything is sent.
    for name in ("binance:meme_rush:finalizing", "binance:smart_money", "binance:topic_rush:latest"):
        again = af.poll_feed(af.FEEDS[name], conn=tmp_db, client=vendor.client)
        assert again.status == "limiter", (name, again)
    assert len(vendor.requests) == 1
    assert rows_of(tmp_db) == []


def test_http_429_is_a_rate_limit_too(tmp_db):
    vendor = Vendor((429, {"code": "429"}))
    result = af.poll_feed(af.FEEDS["binance:topic_rush:latest"], conn=tmp_db, client=vendor.client)
    assert result.status == "rate_limited"


def test_backoff_doubles_to_the_cap_and_resets_on_success():
    spec = af.FEEDS["binance:meme_rush:finalizing"]
    seen, backoff = [], 0.0
    for _ in range(6):
        delay, backoff = af.next_delay_s(spec, "rate_limited", backoff)
        seen.append(delay)
    assert seen == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    assert af.next_delay_s(spec, "ok", backoff) == (spec.interval_s, 0.0)
    # A refusal by OUR limiter is pacing, not the vendor: it neither escalates nor resets.
    assert af.next_delay_s(spec, "limiter", 240.0) == (spec.interval_s, 240.0)
    # A slow feed never polls sooner than its own interval, even on a short backoff.
    slow = af.FEEDS["binance:topic_rush:latest"]
    assert af.next_delay_s(slow, "error", 0.0)[0] == slow.interval_s


def test_one_chain_failing_keeps_the_other_chains_rows(tmp_db, monkeypatch, fast_binance):
    monkeypatch.setattr(af, "_pace_s", lambda provider: 0.0)
    vendor = Vendor((200, envelope([dict(SIGNAL_RH, chainId="CT_501", contractAddress=SOL_TOKEN_A)])),
                    (200, RATE_LIMITED_BODY))
    result = af.poll_feed(af.FEEDS["binance:smart_money"], conn=tmp_db, client=vendor.client)
    assert result.status == "rate_limited"  # the worse outcome drives the backoff
    assert [r.token_address for r in result.rows] == [SOL_TOKEN_A]
    assert result.written == 1


# ------------------------------------------------------------------------------- dedupe


def _feed_rows(narrative: str | None = "Dog AI") -> list[af.FeedRow]:
    return af.parse_meme_rush([meme_row(SOL_TOKEN_A, narrative=narrative)], af.Chain.SOL, "finalizing")


def test_same_window_writes_once_and_first_seen_is_permanent(tmp_db):
    t = 10 * af.HOUR_MS + 5_000
    assert af.write_rows(tmp_db, _feed_rows(), observed_ms=t) == 1
    for minute in range(1, 60):  # a 1-minute poll for the rest of the hour
        assert af.write_rows(tmp_db, _feed_rows(), observed_ms=t + minute * 60_000 - 5_000) == 0
    assert len(rows_of(tmp_db)) == 1

    assert af.write_rows(tmp_db, _feed_rows(), observed_ms=t + af.HOUR_MS) == 1  # next window
    assert len(rows_of(tmp_db)) == 2
    (first,) = rows_of(tmp_db, "alpha_feed_first_seen")
    assert first["first_seen_ms"] == t and first["first_rank"] == 1
    assert first["first_source_ts_ms"] == T0 - 60_000


def test_a_late_narrative_fills_once_and_is_never_overwritten(tmp_db):
    t = 20 * af.HOUR_MS
    af.write_rows(tmp_db, _feed_rows(narrative=None), observed_ms=t)
    assert af.write_rows(tmp_db, _feed_rows("Golden Shoe"), observed_ms=t + 60_000) == 1
    assert af.write_rows(tmp_db, _feed_rows("Something Else"), observed_ms=t + 120_000) == 0
    (row,) = rows_of(tmp_db)
    assert row["narrative"] == "Golden Shoe"
    (first,) = rows_of(tmp_db, "alpha_feed_first_seen")
    assert first["first_seen_ms"] == t
    assert (first["narrative"], first["narrative_ms"]) == ("Golden Shoe", t + 60_000)


def test_the_seen_cache_keeps_a_quiet_poll_out_of_the_database(tmp_db, monkeypatch):
    seen = af._SeenCache()
    t = 30 * af.HOUR_MS
    assert af.write_rows(tmp_db, _feed_rows(), observed_ms=t, seen=seen) == 1

    def no_tx(conn):
        raise AssertionError("a poll with nothing new opened a write transaction")

    monkeypatch.setattr(af, "tx", no_tx)
    assert af.write_rows(tmp_db, _feed_rows(), observed_ms=t + 60_000, seen=seen) == 0


def test_an_enrichment_lookup_is_never_a_sighting(tmp_db):
    row = af.FeedRow(feed="gmgn:created_tokens", chain="sol", token_address=SOL_TOKEN_A, symbol="OWL",
                     name=None, narrative=None, rank=None, context=SOL_DEV, source_ts_ms=None, raw={})
    assert af.write_rows(tmp_db, [row], observed_ms=T0) == 1
    assert rows_of(tmp_db, "alpha_feed_first_seen") == []


# ------------------------------------------------------------------------------- gmgn


def test_hot_searches_is_one_discovery_call_for_both_chains(tmp_db, gmgn_replay):
    calls = gmgn_replay(HOT_SEARCHES)
    result = af.poll_feed(af.FEEDS["gmgn:hot_searches:5m"], conn=tmp_db)
    assert result.status == "ok" and result.written == 3
    (argv,) = calls
    assert argv[2:] == ["market", "hot-searches", "--chain", "sol", "--chain", "robinhood",
                        "--interval", "5m", "--limit", "100", "--raw"]
    booked = fetch_all(tmp_db, "SELECT endpoint FROM provider_calls WHERE provider='gmgn'", ())
    assert booked == [{"endpoint": "market.hot_searches"}]
    assert {r["chain"] for r in rows_of(tmp_db, "alpha_feed_first_seen")} == {"sol", "robinhood"}


def test_created_tokens_looks_up_the_dev_of_a_surfaced_token_once(tmp_db, gmgn_replay):
    af.write_rows(tmp_db, _feed_rows(), observed_ms=af.now_ms())
    calls = gmgn_replay(CREATED_TOKENS)
    state = af.PollerState()
    spec = af.FEEDS["gmgn:created_tokens"]
    result = af.poll_feed(spec, state=state, conn=tmp_db)
    assert result.status == "ok" and result.written == 1
    assert calls[0][2:] == ["portfolio", "created-tokens", "--wallet", SOL_DEV, "--chain", "sol", "--raw"]
    stored = [r for r in rows_of(tmp_db) if r["feed"] == "gmgn:created_tokens"]
    assert len(stored) == 1 and stored[0]["context"] == SOL_DEV
    raw = json.loads(stored[0]["raw_json"])
    assert (raw["inner_count"], raw["open_count"], raw["listed"], raw["listed_open"]) == (1537, 29, 2, 1)
    assert raw["top_ath"][0]["token_address"] == SOL_TOKEN_C
    assert raw["self"]["token_address"] == SOL_TOKEN_A
    assert "token_logo" not in raw["creator_ath_info"]

    again = af.poll_feed(spec, state=state, conn=tmp_db)
    assert again.status == "skipped" and len(calls) == 1


def test_gmgn_rate_limit_is_classified_and_writes_nothing(tmp_db, gmgn_replay):
    gmgn_replay(None, returncode=1, stderr="HTTP 429 RATE_LIMIT_EXCEEDED: IP rate limit exceeded")
    result = af.poll_feed(af.FEEDS["gmgn:hot_searches:5m"], conn=tmp_db)
    assert result.status == "rate_limited" and rows_of(tmp_db) == []


# --------------------------------------------------------------------------- allow-list


def test_only_the_two_new_read_only_pairs_were_added():
    assert ("market", "hot-searches") in g._ALLOWED
    assert ("portfolio", "created-tokens") in g._ALLOWED
    assert not any(grp in {"swap", "multi-swap", "cooking", "config"} for grp, _ in g._ALLOWED)
    assert {cmd for grp, cmd in g._ALLOWED if grp == "order"} == {"quote"}


@pytest.mark.parametrize(
    "argv",
    [
        ["swap", "--chain", "sol"],
        ["multi-swap", "--chain", "sol"],
        ["order", "get", "--order-id", "abc"],
        ["order", "strategy", "list"],
        ["order", "create", "--chain", "sol"],
        ["cooking", "create"],
        ["market", "hot-searches;", "--chain", "sol"],
    ],
)
def test_order_and_swap_commands_are_still_refused(gmgn_replay, tmp_db, argv):
    calls = gmgn_replay([])
    data, receipt = g.run_read("market.hot_searches", argv, conn=tmp_db)
    assert data is None and receipt.basis is EvidenceBasis.UNAVAILABLE
    assert calls == []  # refused before any process was spawned


def test_the_new_wrappers_default_to_discovery_and_refuse_unknown_chains(gmgn_replay, tmp_db):
    for fn in (g.market_hot_searches, g.portfolio_created_tokens):
        assert inspect.signature(fn).parameters["priority"].default is Priority.DISCOVERY
    calls = gmgn_replay([])
    assert not g.market_hot_searches(["sol", "not-a-chain"], conn=tmp_db)
    assert not g.portfolio_created_tokens(SOL_DEV, "not-a-chain", conn=tmp_db)
    assert calls == []


def test_the_recorder_cannot_trade_or_touch_the_baseline():
    """Shadow by construction: no execution import, no write to the tokens baseline."""
    src = Path(af.__file__).read_text(encoding="utf-8")
    assert "kaiba.execution" not in src
    assert not re.search(r"(INSERT|UPDATE|REPLACE)\b[^\"']*\btokens\b(?!_)", src)
    assert "order_quote" not in src and "submit" not in src


# ---------------------------------------------------------------------------- analysis


def test_lead_time_report_censors_the_first_poll_and_measures_lead(tmp_db):
    feed = "binance:smart_money"
    start = 100 * af.HOUR_MS
    later = start + 60 * 60_000  # well past the censor guard
    first_seen = [
        ("TokCensored", start, None),                   # already listed at recorder start
        ("TokFeedLeads", later, later - 600_000),       # we see it 5 min after the feed
        ("TokFeedLags", later + 1000, later - 50_000),  # we saw it 10 min before the feed
        ("TokUnseen", later + 2000, None),              # we never saw it at all
    ]
    for addr, ts, src in first_seen:
        tmp_db.execute(
            "INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, "
            "first_source_ts_ms) VALUES (?,?,?,?,?)", (feed, "sol", addr, ts, src),
        )
    for addr, ts in (("TokCensored", start - 1), ("TokFeedLeads", later + 300_000),
                     ("TokFeedLags", later + 1000 - 600_000)):
        tmp_db.execute("INSERT INTO tokens (chain, address, first_seen_ms) VALUES ('sol',?,?)", (addr, ts))
    # snipe_observations is created by the snipe lane at runtime, not by a migration.
    tmp_db.execute("CREATE TABLE IF NOT EXISTS snipe_observations (chain TEXT, token TEXT, seen_ms INTEGER)")
    tmp_db.execute("INSERT INTO snipe_observations VALUES ('sol','TokFeedLeads',?)", (later + 240_000,))

    (rep,) = af.lead_time_report(tmp_db, since_ms=0)
    assert (rep["feed"], rep["chain"]) == (feed, "sol")
    assert (rep["surfaced"], rep["censored"], rep["matched"], rep["unmatched"]) == (4, 1, 2, 1)
    assert rep["feed_first"] == 1
    # leads: +240 s (snipe beat tokens) and -600 s; nearest-rank median of two is the lower
    assert rep["median_lead_s"] == -600.0
    assert rep["p75_lead_s"] == 240.0
    assert rep["median_vendor_lead_s"] == pytest.approx(-549.0)


def test_our_first_seen_works_without_a_snipe_table(tmp_db):
    tmp_db.execute("INSERT INTO tokens (chain, address, first_seen_ms) VALUES ('sol','X',5)")
    assert af.our_first_seen(tmp_db, "sol", ["X", "Y"]) == {"X": 5}


def test_prune_deletes_only_old_rows_in_bounded_batches(tmp_db):
    for i in range(7):
        af.write_rows(tmp_db, af.parse_meme_rush([meme_row(f"Tok{i}")], af.Chain.SOL, "migrated"),
                      observed_ms=1_000 + i)
    af.write_rows(tmp_db, _feed_rows(), observed_ms=10 * af.DAY_MS)
    assert af.prune_rows(tmp_db, older_than_ms=5 * af.DAY_MS, batch=3) == 7
    assert [r["token_address"] for r in rows_of(tmp_db)] == [SOL_TOKEN_A]
    assert len(rows_of(tmp_db, "alpha_feed_first_seen")) == 8  # first-seen is kept


# --------------------------------------------------------------------------------- run


def test_run_polls_records_and_stops(tmp_db, monkeypatch):
    monkeypatch.setattr(af, "_pace_s", lambda provider: 0.0)
    vendor = Vendor((200, envelope([meme_row(SOL_TOKEN_A), meme_row(SOL_TOKEN_B)])))

    async def main() -> None:
        stop = asyncio.Event()

        async def stopper() -> None:
            while not rows_of(tmp_db):
                await asyncio.sleep(0.02)
            stop.set()

        await asyncio.wait_for(
            asyncio.gather(
                af.run(stop=stop, feeds=["binance:meme_rush:finalizing"], conn=tmp_db,
                       client=vendor.client, tick_s=0.01, retention_days=None),
                stopper(),
            ),
            timeout=10,
        )

    asyncio.run(main())
    assert {r["token_address"] for r in rows_of(tmp_db)} == {SOL_TOKEN_A, SOL_TOKEN_B}
    assert len(vendor.requests) == 1
    body = json.loads(vendor.requests[0].content)
    assert body == {"chainId": "CT_501", "rankType": 20, "limit": af.MEME_RUSH_LIMIT}
    assert vendor.requests[0].headers["user-agent"] == af.USER_AGENT


def test_run_refuses_an_unknown_feed():
    with pytest.raises(KeyError):
        asyncio.run(af.run(stop=asyncio.Event(), feeds=["binance:nope"]))


# ------------------------------------------------------------------- retry-after / UA


def test_the_user_agent_is_sent_even_on_an_injected_client(tmp_db):
    vendor = Vendor((200, envelope([])))
    af.poll_feed(af.FEEDS["binance:topic_rush:latest"], conn=tmp_db, client=vendor.client)
    assert vendor.requests[0].headers["user-agent"] == af.USER_AGENT


def test_a_vendor_retry_after_is_honoured_and_capped(tmp_db):
    class SlowDown:
        def __init__(self) -> None:
            self.client = httpx.Client(transport=httpx.MockTransport(
                lambda req: httpx.Response(429, headers={"Retry-After": "1500"}, json={})))

    result = af.poll_feed(af.FEEDS["binance:smart_money"], conn=tmp_db, client=SlowDown().client)
    assert (result.status, result.retry_after_s) == ("rate_limited", 1500.0)
    spec = af.FEEDS["binance:smart_money"]
    assert af.next_delay_s(spec, result.status, 0.0, result.retry_after_s)[0] == 1500.0
    assert af.next_delay_s(spec, "rate_limited", 0.0, 10**9)[0] == af.RETRY_AFTER_MAX_S


# --------------------------------------------------------------------- gmgn budget


def test_default_gmgn_load_is_one_discovery_read_per_five_minutes():
    gmgn_defaults = [n for n in af.DEFAULT_FEEDS if af.FEEDS[n].provider == "gmgn"]
    assert gmgn_defaults == ["gmgn:hot_searches:5m"]  # created_tokens is opt-in
    spec = af.FEEDS["gmgn:hot_searches:5m"]
    assert spec.interval_s >= 300.0 and set(spec.chains) == {af.Chain.SOL, af.Chain.ROBINHOOD}
    # Even under repeated errors the cadence only gets slower.
    assert af.next_delay_s(spec, "error", 0.0)[0] >= 300.0


def test_hot_searches_reserves_the_gmgn_limiter_at_discovery(tmp_db, gmgn_replay, monkeypatch):
    seen: list[tuple[str, str, Priority]] = []
    real = g.guarded

    def spy(provider, endpoint, priority, **kw):
        seen.append((provider, endpoint, priority))
        return real(provider, endpoint, priority, **kw)

    monkeypatch.setattr(g, "guarded", spy)
    gmgn_replay(HOT_SEARCHES)
    assert af.poll_feed(af.FEEDS["gmgn:hot_searches:5m"], conn=tmp_db).status == "ok"
    assert seen == [("gmgn", "market.hot_searches", Priority.DISCOVERY)]


def test_a_limiter_refusal_sends_nothing_and_does_not_escalate(tmp_db, gmgn_replay, monkeypatch):
    from kaiba.core.limiter import RateLimited

    calls = gmgn_replay(HOT_SEARCHES)

    def refuse(*a, **kw):
        raise RateLimited("gmgn", "min_interval", 30.0)

    monkeypatch.setattr(g, "guarded", refuse)
    spec = af.FEEDS["gmgn:hot_searches:5m"]
    result = af.poll_feed(spec, conn=tmp_db)
    assert result.status == "limiter" and calls == [] and rows_of(tmp_db) == []
    assert af.next_delay_s(spec, result.status, 120.0) == (spec.interval_s, 120.0)


# ------------------------------------------------------------------- point in time


def _first_seen(conn, feed, chain, token, ts, *, narrative=None, narrative_ms=None, rank=1, src=None):
    conn.execute(
        "INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, first_rank, "
        "first_source_ts_ms, narrative, narrative_ms) VALUES (?,?,?,?,?,?,?,?)",
        (feed, chain, token, ts, rank, src, narrative, narrative_ms),
    )


def test_point_in_time_never_returns_a_sighting_at_or_after_t0(tmp_db):
    t0 = 500 * af.HOUR_MS
    start = t0 - 10 * af.HOUR_MS
    _first_seen(tmp_db, "binance:smart_money", "sol", "Opener", start)  # recorder start
    _first_seen(tmp_db, "binance:smart_money", "sol", SOL_TOKEN_A, t0 - 90_000, src=t0 - 400_000)
    _first_seen(tmp_db, "gmgn:hot_searches:5m", "sol", SOL_TOKEN_A, t0)          # AT t0: unseen
    _first_seen(tmp_db, "binance:topic_rush:latest", "sol", SOL_TOKEN_A, t0 + 1)  # after: unseen
    _first_seen(tmp_db, "binance:meme_rush:migrated", "sol", SOL_TOKEN_A, t0 - 60_000,
                narrative="Owl", narrative_ms=t0)  # narrative arrived AT t0: withheld

    hits = af.feed_sightings_at(tmp_db, "sol", SOL_TOKEN_A, t0)
    assert set(hits) == {"binance:smart_money", "binance:meme_rush:migrated"}
    assert all(h["first_seen_ms"] < t0 for h in hits.values())
    sm = hits["binance:smart_money"]
    assert sm["lead_s"] == 90.0 and sm["source_ts_ms"] == t0 - 400_000 and sm["censored"] is False
    assert hits["binance:meme_rush:migrated"]["narrative"] is None

    later = af.feed_sightings_at(tmp_db, "sol", SOL_TOKEN_A, t0 + 2)
    assert set(later) == set(hits) | {"gmgn:hot_searches:5m", "binance:topic_rush:latest"}
    assert later["binance:meme_rush:migrated"]["narrative"] == "Owl"
    assert af.feed_sightings_at(tmp_db, "sol", SOL_TOKEN_A, t0 - 90_000) == {}


def test_bulk_loader_matches_the_single_lookup_for_every_t0(tmp_db):
    t0 = 600 * af.HOUR_MS
    _first_seen(tmp_db, "binance:smart_money", "robinhood", RH_TOKEN_MIXED.lower(), t0 - 5_000_000)
    _first_seen(tmp_db, "binance:smart_money", "robinhood", "0xother", t0 - 100_000)
    _first_seen(tmp_db, "gmgn:hot_searches:5m", "robinhood", "0xother", t0 - 10_000,
                narrative="n", narrative_ms=t0 - 5_000)
    _first_seen(tmp_db, "gmgn:hot_searches:5m", "sol", "0xother", t0 - 10_000)  # other chain

    def query(sql, params):
        return tmp_db.execute(sql, params).fetchall()

    bulk = af.load_sightings(query, ["robinhood"])
    assert set(bulk) == {("robinhood", RH_TOKEN_MIXED.lower()), ("robinhood", "0xother")}
    for t in (t0 - 200_000, t0 - 10_000, t0 - 9_999, t0 - 5_000, t0 - 4_999, t0, t0 + af.DAY_MS):
        for key, sightings in bulk.items():
            pit = af.sightings_before(sightings, t)
            assert pit == af.feed_sightings_at(tmp_db, key[0], key[1], t), (key, t)
            assert all(h["first_seen_ms"] < t for h in pit.values())

    # Censoring: the first sighting of each (feed, chain) is the recorder's start.
    other = {s.feed: s for s in bulk[("robinhood", "0xother")]}
    assert bulk[("robinhood", RH_TOKEN_MIXED.lower())][0].censored is True
    assert other["gmgn:hot_searches:5m"].censored is True  # first row of that feed on rh
    assert other["binance:smart_money"].censored is False  # 81 min after that feed began

    # Mixed-case EVM input is normalised like the recorder normalises it.
    assert af.feed_sightings_at(tmp_db, "robinhood", RH_TOKEN_MIXED, t0) != {}


def test_flat_features_count_only_what_was_known(tmp_db):
    t0 = 700 * af.HOUR_MS
    _first_seen(tmp_db, "binance:smart_money", "sol", SOL_TOKEN_B, t0 - 30_000)
    _first_seen(tmp_db, "binance:meme_rush:finalizing", "sol", SOL_TOKEN_B, t0 - 600_000,
                narrative="Dog AI", narrative_ms=t0 - 500_000)
    _first_seen(tmp_db, "gmgn:hot_searches:5m", "sol", SOL_TOKEN_B, t0 + 60_000)
    bulk = af.load_sightings(lambda sql, p: tmp_db.execute(sql, p).fetchall(), ["sol"])
    f = af.feed_features(bulk[("sol", SOL_TOKEN_B)], t0)
    assert (f["alpha_feeds_n"], f["alpha_lead_s"], f["alpha_narrative"]) == (2, 600.0, 1)
    assert f["alpha:gmgn:hot_searches:5m"] == 0 and f["alpha:binance:smart_money"] == 1
    assert "alpha:gmgn:created_tokens" not in f  # enrichment is never a sighting
    none = af.feed_features([], t0)
    assert (none["alpha_feeds_n"], none["alpha_lead_s"], none["alpha_narrative"]) == (0, None, 0)


def test_point_in_time_reads_without_the_table_are_unmeasured_not_zero(tmp_db):
    tmp_db.execute("DROP TABLE alpha_feed_first_seen")
    assert af.load_sightings(lambda sql, p: tmp_db.execute(sql, p).fetchall(), ["sol"]) == {}
    assert af.feed_sightings_at(tmp_db, "sol", SOL_TOKEN_A, T0) == {}


# --------------------------------------------------------------- no path to orders


def test_importing_the_recorder_loads_no_execution_module():
    import subprocess
    import sys

    code = (
        "import sys, kaiba.ingest.alpha_feeds\n"
        "bad = sorted(m for m in sys.modules if m.startswith('kaiba.execution'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=str(Path(__file__).resolve().parents[1]))
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == ""


def test_the_only_gmgn_calls_are_the_two_read_wrappers():
    import ast

    tree = ast.parse(Path(af.__file__).read_text(encoding="utf-8"))
    used = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "gmgn"
    }
    assert used == {"market_hot_searches", "portfolio_created_tokens"}
    # And neither wrapper can be steered into a write: both build fixed read argv.
    for fn in (g.market_hot_searches, g.portfolio_created_tokens):
        src = inspect.getsource(fn)
        assert not re.search(r"\"(swap|multi-swap|order|cooking)\"", src)
