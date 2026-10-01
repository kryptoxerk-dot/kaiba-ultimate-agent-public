"""Two ways the GMGN feed lied about itself on the live box, 2026-09-21.

1. ``ingest_status`` said ``running / events_seen 0 / last_event_ms None`` for 1.5 h while
   the journal showed ~200 rows a sweep on three chains: ``run()`` never reported.
   The row exists to tell a dead feed from a quiet one, and it said "dead" about the feed
   that supplies sm-trenches its smart-money buyers.
2. ``market.signal``: 129 calls in the hour, 129 errors, all ``unknown option '--limit'``.
   ``gmgn-cli market signal`` takes no ``--limit``; the spec passed one; the feed had never
   returned a row.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import math
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.schemas import Chain
from kaiba.ingest import gmgn_feeds as gf
from kaiba.ingest.runner import set_status


def test_the_signal_feed_passes_no_limit_flag():
    spec = gf.FEEDS["signal"]
    assert "limit" not in spec.flags, spec.flags
    assert "--limit" not in gf._flags_to_argv({"chain": "sol", **spec.flags})


def test_the_swap_feeds_still_pass_their_limit():
    """The fix is for signal alone; the 100-row page on smartmoney/kol is deliberate."""
    for name in ("smartmoney", "kol"):
        assert "--limit" in gf._flags_to_argv(gf.FEEDS[name].flags), name


async def test_run_reports_written_rows_to_ingest_status(tmp_db, monkeypatch):
    stop = asyncio.Event()

    async def fake_poll_all(chains, feeds, conn=None, *, stop=None):
        stop.set()  # one sweep, then the loop exits
        return 7

    monkeypatch.setattr(gf, "poll_all", fake_poll_all)
    set_status("gmgn", "running", tmp_db, started_ms=1, restarts=0)  # what runner.run_feed does at start
    await gf.run(interval_s=0, stop=stop, chains=[Chain.SOL], feeds=["smartmoney"], conn=tmp_db)

    row = fetch_one(tmp_db, "SELECT events_seen, last_event_ms FROM ingest_status WHERE feed='gmgn'")
    assert row is not None
    assert int(row["events_seen"]) == 7
    assert row["last_event_ms"], "a sweep that wrote rows is an event"


async def test_a_sweep_that_writes_nothing_reports_nothing(tmp_db, monkeypatch):
    """Zero rows is not an event; a quiet feed must not look busy."""
    stop = asyncio.Event()

    async def fake_poll_all(chains, feeds, conn=None, *, stop=None):
        stop.set()
        return 0

    monkeypatch.setattr(gf, "poll_all", fake_poll_all)
    set_status("gmgn", "running", tmp_db, started_ms=1, restarts=0)
    await gf.run(interval_s=0, stop=stop, chains=[Chain.SOL], feeds=["smartmoney"], conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT events_seen, last_event_ms FROM ingest_status WHERE feed='gmgn'")
    assert int(row["events_seen"]) == 0 and row["last_event_ms"] is None


# ======================================================================================
# rug_ratio on every alpha feed
#
# Every row below is REAL: captured read-only 2026-09-22 from the live box's own provider
# cache (the running feed's last poll) or from the one metered `market signal --chain bsc`
# call made that day -- the first rows that feed ever returned -- and trimmed to the keys
# the parser reads. `dyor._stored_feed_rug_ratio` looks the score up by name in the stored
# alpha.signal payload; trending and signal rows carried it and the parsers dropped it.
# ======================================================================================

TRENDING_SOL = {
    "address": "FQVeKbsDekWZZCadPsoySud4dDeFsyt9fXKMXrbyvdDD", "chain": "sol", "symbol": "GO",
    "name": "CryptGO", "rank": 1, "volume": 1569340, "swaps": 17815, "price": 0.000604961,
    "price_change_percent1h": 17995.6, "smart_degen_count": 42, "renowned_count": 3,
    "holder_count": 2246, "market_cap": 604961, "liquidity": 77109.1, "rug_ratio": 0.186,
    "open_timestamp": 1790019162, "creation_timestamp": 1790019162, "launchpad": "ray_launchpad",
    "launchpad_platform": "stonkfun", "id": 0, "total_supply": 999999738,
}
# bsc: every one of the 50 rows carried the integer 0 (robinhood likewise). Whether that is
# a score or "unscored" is not something the vendor says; the parser stores it as sent.
TRENDING_BSC = {
    "address": "0xc69b16cf18cea1e5d0bb6a1a9db802097790ddd2", "chain": "bsc", "symbol": "CNPY",
    "name": "CNPY", "rank": 1, "volume": 963253, "swaps": 1478, "price": 0.392858,
    "price_change_percent1h": -1.48092, "smart_degen_count": 28, "renowned_count": 16,
    "holder_count": 14910, "market_cap": 11883100, "liquidity": 2444640, "rug_ratio": 0,
    "open_timestamp": 0, "creation_timestamp": 1789718988, "launchpad": "",
    "launchpad_platform": "", "id": 0, "total_supply": 30247757,
}
# `market signal --chain bsc --raw`, as the wrapper hands it to the parser: a bare list.
# No top-level chain / symbol / timestamp; the token object is nested under `data`.
SIGNAL_BSC = [
    {
        "id": "3c632275-60c3-4386-bd57-533516e080f3",
        "token_address": "0xf4ce05e0b8f27fb474b8ded26078ebe577954444", "signal_type": 6,
        "ath": 34553.138, "market_cap": 34715.912, "trigger_at": 1790020255,
        "trigger_mc": 32791.992, "first_trigger_mc": 32791.992, "signal_times": 2,
        "signal_times_by_type": {"6": 1, "8": 1},
        "cur_data": {"top_10_holder_rate": 0.2357, "holder_count": 308, "liquidity": 16540.029},
        "data": {
            "chain": "bsc", "address": "0xf4ce05e0b8f27fb474b8ded26078ebe577954444",
            "quote_address": "0x55d398326f99059ff775485246999027b3197955",
            "symbol": "龙虾人生🔶", "name": "龙虾人生🔶", "launchpad": "fourmeme",
            "launchpad_platform": "fourmeme", "exchange": "0x5c952063c7fc8610ffdb798152d69f0b9550762b",
            "progress": 0.8968147648129923, "total_supply": 1000000000, "market_cap": 32791.992,
            "holder_count": 47, "liquidity": 15841.6003,
            "creator": "0x6f4ee3c1712c60f19b6fb19441e8cdc9779c0562", "rug_ratio": 0,
            "created_timestamp": 1790020158, "open_timestamp": 0, "complete_timestamp": 0,
            "smart_degen_count": 0, "renowned_count": 0, "bot_degen_count": 6,
            "bundler_trader_amount_rate": 0.7071, "top_10_holder_rate": 0.2357,
            "is_honeypot": "no", "decimals": 0,
        },
    },
    {
        "id": "d7371ca9-0267-4979-ab82-1525acbec753",
        "token_address": "0x1c2b16700ea72186e106a0b2b817126c46177777", "signal_type": 6,
        "ath": 24643.902, "market_cap": 24728.899, "trigger_at": 1790019587,
        "trigger_mc": 20284.184, "first_trigger_mc": 6701.4273, "signal_times": 1,
        "signal_times_by_type": {"6": 1},
        "cur_data": {"top_10_holder_rate": 0.3018, "holder_count": 57, "liquidity": 12975.5385},
        "data": {
            "chain": "bsc", "address": "0x1c2b16700ea72186e106a0b2b817126c46177777",
            "quote_address": "0x0000000000000000000000000000000000000000",
            "symbol": "Binancians", "name": "Binancians", "launchpad": "flap",
            "launchpad_platform": "flap", "exchange": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
            "progress": 0.5083, "total_supply": 1000000000, "market_cap": 20284.184,
            "holder_count": 42, "liquidity": 10515.4769,
            "creator": "0x6b73ab1e4a4a7a7da757ac2ab89f0cf93fa291a5", "rug_ratio": 0,
            "created_timestamp": 1789476008, "open_timestamp": 0, "complete_timestamp": 0,
            "smart_degen_count": 0, "renowned_count": 0, "bot_degen_count": 31,
            "bundler_trader_amount_rate": 0.1469, "top_10_holder_rate": 0.3018,
            "is_honeypot": "no", "decimals": 0,
        },
    },
    {
        "id": "bfa978df-8b07-46b8-b7e2-41059b0ddba1",
        "token_address": "0xf140586d90e84ebd78ed198df4503a33b7057777", "signal_type": 8,
        "ath": 128416.93, "market_cap": 33083.193999999996, "trigger_at": 1790019575,
        "trigger_mc": 32989.146, "first_trigger_mc": 30407.008, "signal_times": 1,
        "signal_times_by_type": {"8": 1},
        "cur_data": {"top_10_holder_rate": 0.2439, "holder_count": 132, "liquidity": 16679.912588616484},
        "data": {
            "chain": "bsc", "address": "0xf140586d90e84ebd78ed198df4503a33b7057777",
            "quote_address": "0x4b0f1812e5df2a09796481ff14017e6005508003",
            "symbol": "trust", "name": "trust me bro", "launchpad": "flap",
            "launchpad_platform": "flap", "exchange": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
            "progress": 1, "total_supply": 1000000000, "market_cap": 32989.146,
            "holder_count": 132, "liquidity": 16541.568569055773,
            "creator": "0x1f7cb94316ddbea0610961f29fb3c3868e0a7c8b", "rug_ratio": 0,
            "created_timestamp": 1788750721, "open_timestamp": 1788770420,
            "complete_timestamp": 1788770420, "smart_degen_count": 0, "renowned_count": 14,
            "bot_degen_count": 89, "bundler_trader_amount_rate": 0.1633,
            "top_10_holder_rate": 0.2439, "is_honeypot": "no", "decimals": 0,
        },
    },
]
# `market trenches --chain bsc`, near_completion list: the row has NO rug_ratio key at all
# (0/180 rows on one poll, 34/180 -- all integer 0 -- on the next).
TRENCHES_BSC_NEAR_COMPLETION = {
    "address": "0xc588062b2a9e275aedfe4313a65b3609f26b7777", "chain": "bsc", "symbol": "ZERO",
    "name": "Zero coin", "creator": "0xfb9422e5a697b87bd3f0ab1b23214cef0853d6dd",
    "launchpad": "flap", "launchpad_platform": "flap", "launchpad_status": 0,
    "created_timestamp": 1789954838, "open_timestamp": 0, "complete_timestamp": 0,
    "quote_address": "0x1ba42e5193dfa8b03d15dd1b86a3113bbbef8eeb", "quote_address_type": 0,
    "exchange": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0", "total_supply": 1000000000,
    "holder_count": 102, "smart_degen_count": 7, "renowned_count": 4, "bot_degen_count": 31,
    "progress": 0.3503, "liquidity": 6751.8696, "market_cap": 12347.51, "is_honeypot": "no",
    "creator_token_status": "creator_close", "creator_created_count": 3,
    "fund_from_address": "0xc38fd785dfbc7cea2d563b03c37e434cf2c86bf8",
    "bundler_trader_amount_rate": 0.0895, "rat_trader_amount_rate": 0, "fresh_wallet_rate": 0.0916,
    "top_10_holder_rate": 0.3356, "buys_24h": 1131, "sells_24h": 780, "price": 1.2347508e-05,
    "volume_24h": 92997.04,
}


@pytest.fixture
def no_limiter(monkeypatch):
    """Bypass the shared limiter so one test can poll twice; the limiter has its own tests."""

    @contextlib.contextmanager
    def passthrough(*_args, **_kwargs):
        yield

    monkeypatch.setattr(gf, "guarded", passthrough)


def _one_trending(row: dict) -> dict:
    (parsed,) = gf.parse_trending([row], Chain.SOL)
    return parsed.payload


def _one_signal(row: dict) -> dict:
    (parsed,) = gf.parse_signal([row], Chain.BSC)
    return parsed.payload


def _one_trenches(row: dict) -> dict:
    (parsed,) = gf.parse_trenches({"near_completion": [row]}, Chain.BSC)
    return parsed.payload


def _scored(base: dict, value, *, nested: bool) -> dict:
    """A copy of ``base`` whose score is ``value`` (``...`` removes the key)."""
    row = copy.deepcopy(base)
    target = row["data"] if nested else row
    if value is ...:
        target.pop("rug_ratio", None)
    else:
        target["rug_ratio"] = value
    return row


def test_trending_stores_rug_ratio_as_the_raw_number():
    sol = _one_trending(TRENDING_SOL)
    assert sol["rug_ratio"] == 0.186 and type(sol["rug_ratio"]) is float
    bsc = _one_trending(TRENDING_BSC)
    assert bsc["rug_ratio"] == 0 and type(bsc["rug_ratio"]) is int  # as sent; never a bool
    assert "trenches_category" not in bsc  # trending rows name no lifecycle stage


def test_signal_reads_the_real_row_shape():
    """The wrapper hands the parser a bare list; the token object sits under ``data``."""
    rows = gf.parse_signal(SIGNAL_BSC, Chain.SOL)  # polled "as sol": the row says bsc
    assert len(rows) == 3
    first = rows[0]
    assert first.chain is Chain.BSC
    assert first.token == "0xf4ce05e0b8f27fb474b8ded26078ebe577954444"
    assert first.ts_ms == 1790020255000  # trigger_at, not our clock
    assert first.symbol == "龙虾人生🔶"
    assert first.ident == "gmgn:signal:3c632275-60c3-4386-bd57-533516e080f3"
    assert first.label == "signal_6"
    assert first.payload["rug_ratio"] == 0 and type(first.payload["rug_ratio"]) is int
    assert first.payload["market_cap_usd"] == "34715.912"
    assert first.payload["liquidity_usd"] == "16540.029"  # cur_data, the live number
    assert first.payload["signal_type"] == 6
    # the same rows inside the --raw envelope parse identically
    assert [r.dedupe_key for r in gf.parse_signal({"code": 0, "data": SIGNAL_BSC}, Chain.BSC)] == [
        r.dedupe_key for r in rows
    ]


def test_trenches_row_without_the_key_stores_no_rug_ratio():
    payload = _one_trenches(TRENCHES_BSC_NEAR_COMPLETION)
    assert "rug_ratio" not in payload
    assert payload["trenches_category"] == "near_completion"
    assert payload["smart_degen_count"] == 7


@pytest.mark.parametrize(
    ("parse", "base", "nested"),
    [
        (_one_trending, TRENDING_SOL, False),
        (_one_signal, SIGNAL_BSC[0], True),
        (_one_trenches, TRENCHES_BSC_NEAR_COMPLETION, False),
    ],
    ids=["trending", "signal", "trenches"],
)
def test_every_alpha_feed_stores_only_a_number_and_never_invents_one(parse, base, nested):
    assert parse(_scored(base, 0.27, nested=nested))["rug_ratio"] == 0.27
    assert type(parse(_scored(base, 1, nested=nested))["rug_ratio"]) is int
    for not_a_score in (..., None, "0.27", "", True, False, math.nan, math.inf, [0.27], {"v": 0.27}):
        payload = parse(_scored(base, not_a_score, nested=nested))
        assert "rug_ratio" not in payload, (not_a_score, payload.get("rug_ratio"))


def test_a_top_level_score_wins_over_a_nested_one():
    row = _scored(SIGNAL_BSC[0], 0.4, nested=True)
    row["rug_ratio"] = 0.9
    assert _one_signal(row)["rug_ratio"] == 0.9


def test_stored_score_round_trips_through_dyors_stored_feed_reader(tmp_db, no_limiter):
    """End to end: poll -> alpha.signal event -> ``dyor._stored_feed_rug_ratio``."""
    from kaiba.intelligence import dyor

    assert gf.poll_once(Chain.SOL, "trending", tmp_db, runner=lambda *a, **k: [TRENDING_SOL]) == 1
    stored = fetch_one(
        tmp_db,
        "SELECT json_type(payload,'$.rug_ratio') AS t, json_extract(payload,'$.rug_ratio') AS v, "
        "json_extract(payload,'$.provider') AS p FROM events WHERE kind='alpha.signal'",
    )
    assert (stored["t"], stored["v"], stored["p"]) == ("real", 0.186, "gmgn")
    found = dyor._stored_feed_rug_ratio(tmp_db, TRENDING_SOL["address"], Chain.SOL)
    assert found is not None
    value, receipt = found
    assert value == Decimal("0.186")
    assert receipt.endpoint == "feed.trending"
    assert receipt.provider == "gmgn"


def test_signal_poll_stores_the_nested_score_as_an_integer_json_number(tmp_db, no_limiter):
    """bsc signal rows carry ``data.rug_ratio: 0``; it is stored as the integer 0, as sent.

    Note for the reader of these events: ``dyor._stored_feed_rug_ratio`` accepts any
    number in [0, 1], so this 0 passes the lane's ``< 0.3`` gate. Whether a bsc 0 is a
    score or an unscored placeholder is the vendor's to say; the parser does not decide.
    """
    call = lambda *a, **k: SIGNAL_BSC  # noqa: E731
    assert gf.poll_once(Chain.BSC, "signal", tmp_db, runner=call) == 3
    assert gf.poll_once(Chain.BSC, "signal", tmp_db, runner=call) == 0  # deduped by uuid
    rows = tmp_db.execute(
        "SELECT chain, subject, json_type(payload,'$.rug_ratio'), json_extract(payload,'$.rug_ratio'), "
        "json_extract(payload,'$.feed') FROM events WHERE kind='alpha.signal' ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("bsc", "0xf4ce05e0b8f27fb474b8ded26078ebe577954444", "integer", 0, "signal"),
        ("bsc", "0x1c2b16700ea72186e106a0b2b817126c46177777", "integer", 0, "signal"),
        ("bsc", "0xf140586d90e84ebd78ed198df4503a33b7057777", "integer", 0, "signal"),
    ]


def test_the_unmeasured_meaning_of_an_evm_zero_is_labelled():
    """A ``0`` on every bsc and robinhood row is consistent with "unscored", but that is a
    reading, not a measurement; the module must say so where the numbers are cited."""
    doc = gf.__doc__ or ""
    assert "NOT measured" in doc and "unscored" in doc
    assert "MEASURED" in (gf._rug_ratio.__doc__ or "")
