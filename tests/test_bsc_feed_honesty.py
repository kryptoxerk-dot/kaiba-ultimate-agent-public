"""GMGN feed rows must be honest about money and must create tokens on listener-less chains.

Every fixture here is a REAL row: the trenches objects and the track row were copied
2026-09-21 from the live box's own provider cache (``data/cache/gmgn``), trimmed of logos.
Their shapes are the whole point -- the old parser returned zero rows on the trenches
shape and wrote a UI float into a base-units column on the track shape -- so the shapes
are the contract these tests pin, not a convenience.

Offline by construction: the feed runner is injected, the limiter is bypassed, and the
decimals lookup is forbidden from touching a chain.

Section 3 pins the ``amount_token`` CONTRACT for gmgn rows: the column carries the
provider's UI text (``20530293.283241913``), never ``None`` and never base units, because
``grade.HUMAN_UNIT_SOURCE_PREFIXES == ("gmgn:",)`` and ``grade.normalise_tape_rows`` scale
gmgn rows themselves (``tests/test_tape_grading.py``). Round 1 of this work pinned the
opposite, and it graded every gmgn-only wallet -- on bsc, every wallet, the feed being the
only swaps source there -- as ``closed_episodes=0 / contaminated=1``. Section 3b is the
end-to-end regression.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import re
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.schemas import EVM_ZERO, SOL_NATIVE_MINT, Chain, EventKind
from kaiba.execution import lanes
from kaiba.ingest import gmgn_feeds
from kaiba.intelligence import grade

# ======================================================================================
# real payloads (MEASURED 2026-09-21, live cache, trimmed)
# ======================================================================================

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT_BSC = "0x55d398326f99059ff775485246999027b3197955"

NEW_CREATION = {
    "address": "0x23b283fc0dd98da194c5d52a53d6a071df0a4444", "chain": "bsc", "symbol": "MTS",
    "name": "MTS", "creator": "0x02c89cb00da8aec602587b2f1bb5be7e278a1801",
    "launchpad": "fourmeme", "launchpad_platform": "fourmeme", "launchpad_status": 0,
    "created_timestamp": 1790006666, "open_timestamp": 0, "complete_timestamp": 0,
    "quote_address": WBNB, "quote_address_type": 7,
    "exchange": "0x5c952063c7fc8610ffdb798152d69f0b9550762b", "holder_count": 2,
    "smart_degen_count": 1, "renowned_count": 0, "bot_degen_count": 1,
    "progress": 0.001050086815469534, "liquidity": 7.729, "market_cap": 4635.7,
    "is_honeypot": "no", "total_supply": 1000000000, "bundler_trader_amount_rate": 0,
    "fresh_wallet_rate": 0, "buys_24h": 4, "sells_24h": 3,
    "creator_token_status": "creator_close", "creator_created_count": 245,
    "fund_from_address": "0x9ea049a558a63007535df34f6f43239e7dd64aba",
    "top_10_holder_rate": 0.0008, "rat_trader_amount_rate": 0, "price": 4.6356969e-06,
    "volume_24h": 555.99,
}
NEAR_COMPLETION = {
    "address": "0xf660cb724a25c8108c1e88ed127001fca1057777", "chain": "bsc",
    "symbol": "蝴蝶先驱", "name": "蝴蝶先驱",
    "creator": "0x3ec0d63125f6d9a1c14e5343c9f54a9597bc1fea", "launchpad": "flap",
    "launchpad_platform": "flap", "launchpad_status": 0, "created_timestamp": 1789965972,
    "open_timestamp": 0, "complete_timestamp": 0, "quote_address": EVM_ZERO,
    "quote_address_type": 6, "exchange": "0xe2ce6ab80874fa9fa2aae65d277dd6b8e65c9de0",
    "holder_count": 741, "smart_degen_count": 3, "renowned_count": 2, "bot_degen_count": 22,
    "progress": 0.7503, "liquidity": 19214.9787, "market_cap": 39631.96, "is_honeypot": "no",
    "total_supply": 1000000000, "bundler_trader_amount_rate": 0, "fresh_wallet_rate": 0.143,
    "buys_24h": 2822, "sells_24h": 1313, "creator_token_status": "creator_close",
    "creator_created_count": 3,
    "fund_from_address": "0x1fbe2acee135d991592f167ac371f3dd893a508b",
    "top_10_holder_rate": 0.2572, "rat_trader_amount_rate": 0, "price": 3.9631957e-05,
    "volume_24h": 132200.98,
}
COMPLETED = {
    "address": "0x5727a62145babe985a2948e5b859579687697777", "chain": "bsc", "symbol": "BAOS",
    "name": "Binance Agent OS", "creator": "0xf0126a0f10a44ada3dfce0bbb83336df38e995df",
    "launchpad": "flap", "launchpad_platform": "flap", "launchpad_status": 1,
    "created_timestamp": 1790005581, "open_timestamp": 1790005601,
    "complete_timestamp": 1790005601, "quote_address": WBNB, "quote_address_type": 7,
    "exchange": "pancake_v2", "holder_count": 273, "smart_degen_count": 7,
    "renowned_count": 8, "bot_degen_count": 162, "progress": 1,
    "liquidity": 25216.05960375742, "market_cap": 61000.81, "is_honeypot": "no",
    "total_supply": 1000000000, "bundler_trader_amount_rate": 0.4134,
    "fresh_wallet_rate": 0.3038, "buys_24h": 1187, "sells_24h": 972,
    "creator_token_status": "creator_close", "creator_created_count": 1,
    "fund_from_address": "0x3c783c21a0383057d128bae431894a5c19f9cf06",
    "top_10_holder_rate": 0.2525, "rat_trader_amount_rate": 0.0202, "price": 6.1000809e-05,
    "volume_24h": 160928.87,
}


def trenches_payload(*, chain: str = "bsc", **overrides) -> dict:
    """The real ``market trenches`` envelope: three category lists, never ``list``."""
    def stamp(row: dict) -> dict:
        return {**row, "chain": chain, **overrides.get(row["address"], {})}

    return {
        "code": 0, "msg": "success",
        "data": {
            "completed": [stamp(COMPLETED)],
            "near_completion": [stamp(NEAR_COMPLETION)],
            "new_creation": [stamp(NEW_CREATION)],
        },
    }


TRACK_ROW = {
    "transaction_hash": "0xf87f2de283062819885952603f61726e08a4a1d2e91650ab743974adaa389d38",
    "maker": "0x4af2561ba7b7b7a61c975b6560bd1d94df657563", "base_amount": 20530293.283241913,
    "quote_amount": 153.99549113740258, "buy_cost_usd": 117.0594882975638,
    "token_amount": 20530293.283241913, "amount_usd": 115.30412398913019,
    "price": 7.500890952352014e-06, "price_usd": 5.6162921005735705e-06,
    "timestamp": 1790007732, "side": "sell", "is_open_or_close": 1,
    "base_address": "0x5dfb89349c229efc3efca11787e6f1cc8e27eb50", "balance": 0,
    "base_token": {"symbol": "Dust", "total_supply": "983458989.8488209", "launchpad": "geniusfun"},
    "maker_info": {"avatar": "", "name": "", "tags": ["smart_degen", "gmgn"],
                   "twitter_username": "", "twitter_name": ""},
}


def track_payload(*rows: dict) -> dict:
    return {"code": 0, "msg": "success", "data": {"list": list(rows or [TRACK_ROW])}}


def buy_row(**overrides) -> dict:
    return {**TRACK_ROW, "side": "buy", "transaction_hash": "0x" + "ab" * 32, **overrides}


# ======================================================================================
# fixtures
# ======================================================================================


@pytest.fixture
def feed_db(tmp_db, monkeypatch):
    """A migrated db, the limiter bypassed, and the chain forbidden on the ingest path."""

    @contextlib.contextmanager
    def passthrough(*_args, **_kwargs):
        yield

    monkeypatch.setattr(gmgn_feeds, "guarded", passthrough)

    from kaiba.execution import fills

    def forbidden(*_args, **_kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("the ingest path must never read decimals from the chain")

    monkeypatch.setattr(fills, "_fetch_decimals", forbidden)
    return tmp_db


def runner_for(payload):
    def run(_group, _command, **_flags):
        return payload

    return run


def tokens_on(conn, chain: Chain) -> list[dict]:
    return fetch_all(conn, "SELECT * FROM tokens WHERE chain=? ORDER BY address", (chain.value,))


def events_of(conn, kind: EventKind, chain: Chain | None = None) -> list[dict]:
    if chain is None:
        return fetch_all(conn, "SELECT * FROM events WHERE kind=? ORDER BY id", (kind.value,))
    return fetch_all(
        conn, "SELECT * FROM events WHERE kind=? AND chain=? ORDER BY id", (kind.value, chain.value)
    )


# ======================================================================================
# 1. the trenches shape parses, and the parse is honest about time
# ======================================================================================


def test_the_real_trenches_shape_yields_one_row_per_object():
    """The bug: ``_rows`` knew ``list``/``rank`` but not the three category lists."""
    rows = gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC)
    assert [r.payload["trenches_category"] for r in rows] == ["new_creation", "near_completion", "completed"]
    assert {r.token for r in rows} == {NEW_CREATION["address"], NEAR_COMPLETION["address"], COMPLETED["address"]}
    assert all(r.chain is Chain.BSC and r.feed == "trenches" and r.bucket_s == 0 for r in rows)


def test_category_rows_are_flattened_in_lifecycle_order_and_pump_is_an_alias():
    data = {"completed": [{"address": "c"}], "pump": [{"address": "b"}], "new_creation": [{"address": "a"}]}
    rows = gmgn_feeds._rows({"code": 0, "data": data})
    assert [(r["address"], r["trenches_category"]) for r in rows] == [("a", "new_creation"), ("b", "pump"), ("c", "completed")]
    # the flatten copies; the provider's own objects are untouched
    assert "trenches_category" not in data["completed"][0]


def test_list_shaped_payloads_still_parse_exactly_as_before():
    assert [r["address"] for r in gmgn_feeds._rows({"data": {"list": [{"address": "x"}]}})] == ["x"]
    assert gmgn_feeds._rows({"data": {"nothing": 1}}) == []
    assert gmgn_feeds._rows({"data": {"new_creation": "not a list"}}) == []


def test_token_facts_take_creation_and_graduation_from_the_provider_never_from_our_clock():
    by_addr = {r.token: r.token_facts for r in gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC)}
    new, done = by_addr[NEW_CREATION["address"]], by_addr[COMPLETED["address"]]
    assert new["created_ms"] == 1790006666000 and new["migrated_ms"] is None
    assert done["created_ms"] == 1790005581000 and done["migrated_ms"] == 1790005601000
    assert new["launchpad"] == "fourmeme" and done["launchpad"] == "flap"
    assert new["creator"] == NEW_CREATION["creator"] and new["symbol"] == "MTS" and new["name"] == "MTS"
    assert new["pool"] is None  # `exchange` is a venue, not a pool address
    meta = done["meta"]
    assert meta["source"] == gmgn_feeds.TOKEN_SOURCE
    assert meta["quote_address"] == WBNB and meta["quote_is_native"] is True
    assert meta["trenches_category"] == "completed" and meta["holder_count"] == 273
    assert "logo_small_base64" not in meta


def test_a_missing_creation_timestamp_is_none_not_first_seen():
    payload = trenches_payload(**{NEW_CREATION["address"]: {"created_timestamp": 0, "open_timestamp": 0}})
    row = next(r for r in gmgn_feeds.parse_trenches(payload, Chain.BSC) if r.token == NEW_CREATION["address"])
    assert row.token_facts["created_ms"] is None
    assert row.payload["created_ms"] is None


def test_a_non_native_quote_is_recorded_as_such():
    payload = trenches_payload(**{NEAR_COMPLETION["address"]: {"quote_address": USDT_BSC, "quote_address_type": 8}})
    row = next(r for r in gmgn_feeds.parse_trenches(payload, Chain.BSC) if r.token == NEAR_COMPLETION["address"])
    assert row.token_facts["meta"]["quote_is_native"] is False


def test_trenches_alpha_rows_record_the_preset_that_filtered_them_server_side(feed_db):
    """MUTATION GUARD: dropping ``filter_preset`` from the trenches payload fails here.

    gmgn-cli applies ``max_rug_ratio 0.3`` server-side under ``--filter-preset smart-money``
    (CITED: dist/commands/market.js), so every stored trenches ``rug_ratio`` is < 0.3 by
    construction and dyor's stored fallback can only ever say pass. The row must say which
    preset it came through, and feeds polled without one must not pretend to.
    """
    assert gmgn_feeds.FEEDS["trenches"].flags["filter_preset"] == gmgn_feeds.TRENCHES_FILTER_PRESET == "smart-money"
    rows = gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC)
    assert len(rows) == 3 and all(r.payload["filter_preset"] == "smart-money" for r in rows)
    gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    stored = [jload(e["payload"]) for e in events_of(feed_db, EventKind.ALPHA_SIGNAL, Chain.BSC)]
    assert len(stored) == 3 and all(p["filter_preset"] == "smart-money" for p in stored)
    # a payload without the key came from an unfiltered feed
    trending = gmgn_feeds.parse_trending({"code": 0, "data": {"rank": [{"address": "0x" + "2" * 40}]}}, Chain.BSC)
    signal = gmgn_feeds.parse_signal({"code": 0, "data": {"signals": [{"token_address": "0x" + "3" * 40}]}}, Chain.BSC)
    assert len(trending) == 1 and "filter_preset" not in trending[0].payload
    assert len(signal) == 1 and "filter_preset" not in signal[0].payload


# ======================================================================================
# 2. tokens are created on listener-less chains, exactly like a listener would
# ======================================================================================


def test_polling_bsc_trenches_creates_a_tokens_row_per_object(feed_db):
    """MUTATION GUARD: a trenches object that skips the tokens upsert fails here."""
    n = gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    assert n == 3
    rows = tokens_on(feed_db, Chain.BSC)
    assert {r["address"] for r in rows} == {NEW_CREATION["address"], NEAR_COMPLETION["address"], COMPLETED["address"]}
    for r in rows:
        assert r["first_seen_ms"] > 0 and r["decimals"] is None
        assert jload(r["meta_json"], {})["source"] == gmgn_feeds.TOKEN_SOURCE
    done = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (COMPLETED["address"],))
    assert done["symbol"] == "BAOS" and done["name"] == "Binance Agent OS" and done["launchpad"] == "flap"
    assert done["created_ms"] == 1790005581000 and done["migrated_ms"] == 1790005601000
    assert done["creator"] == COMPLETED["creator"]
    # the same columns pumpportal.record_new_token writes, and the same event
    created = events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC)
    assert {jload(e["payload"])["mint"] for e in created} == {r["address"] for r in rows}
    assert all(jload(e["payload"])["source"] == gmgn_feeds.TOKEN_SOURCE for e in created)
    # and the alpha event is still there for the lane
    assert len(events_of(feed_db, EventKind.ALPHA_SIGNAL, Chain.BSC)) == 3


def test_write_alpha_alone_registers_the_token(feed_db):
    row = gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC)[0]
    assert gmgn_feeds.write_alpha(feed_db, row) is True
    assert [r["address"] for r in tokens_on(feed_db, Chain.BSC)] == [row.token]
    # a second write of the same row is a refresh, never a second row or event
    assert gmgn_feeds.write_alpha(feed_db, row) is False
    assert len(tokens_on(feed_db, Chain.BSC)) == 1
    assert len(events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC)) == 1


def test_registered_tokens_are_handed_to_tier0_triage(feed_db):
    """The scanner reads ``triage_decisions``, not ``tokens``; a row alone is invisible."""
    gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    decisions = fetch_all(feed_db, "SELECT chain, token, source FROM triage_decisions ORDER BY id")
    assert {d["token"] for d in decisions} == {NEW_CREATION["address"], NEAR_COMPLETION["address"], COMPLETED["address"]}
    assert all(d["chain"] == "bsc" and d["source"] == gmgn_feeds.TOKEN_SOURCE for d in decisions)


def test_screening_can_be_switched_off_without_losing_the_row(feed_db, monkeypatch):
    monkeypatch.setattr(gmgn_feeds, "SCREEN_NEW_TOKENS", False)
    gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    assert len(tokens_on(feed_db, Chain.BSC)) == 3
    assert fetch_all(feed_db, "SELECT 1 FROM triage_decisions") == []


def test_a_triage_failure_never_costs_the_tokens_row(feed_db, monkeypatch):
    import kaiba.execution.triage as triage

    def boom(*_a, **_k):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(triage, "screen_launch", boom)
    gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    assert len(tokens_on(feed_db, Chain.BSC)) == 3


def test_polling_twice_changes_nothing(feed_db):
    for _ in range(2):
        gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    assert len(tokens_on(feed_db, Chain.BSC)) == 3
    assert len(events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC)) == 3
    assert len(events_of(feed_db, EventKind.ALPHA_SIGNAL, Chain.BSC)) == 3
    assert len(fetch_all(feed_db, "SELECT 1 FROM triage_decisions")) == 3


def test_a_listener_written_row_is_never_overwritten(feed_db):
    feed_db.execute(
        "INSERT INTO tokens (chain, address, symbol, name, created_ms, first_seen_ms, meta_json) "
        "VALUES ('bsc', ?, 'LSTN', 'listener saw it first', 1700000000000, 1700000000001, '{}')",
        (COMPLETED["address"],),
    )
    row = next(r for r in gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC) if r.token == COMPLETED["address"])
    assert gmgn_feeds.write_token(feed_db, row) == "kept"
    kept = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (COMPLETED["address"],))
    assert kept["symbol"] == "LSTN" and kept["name"] == "listener saw it first"
    assert kept["created_ms"] == 1700000000000 and kept["first_seen_ms"] == 1700000000001
    assert kept["migrated_ms"] is None and kept["meta_json"] == "{}"
    assert events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC) == []


def test_a_writer_racing_the_insert_wins_and_the_sweep_survives(feed_db, monkeypatch):
    """Between our SELECT and INSERT another writer registered the address."""
    row = gmgn_feeds.parse_trenches(trenches_payload(), Chain.BSC)[0]
    feed_db.execute(
        "INSERT INTO tokens (chain, address, symbol, first_seen_ms, meta_json) VALUES ('bsc', ?, 'RACE', 7, '{}')",
        (row.token,),
    )
    monkeypatch.setattr(gmgn_feeds, "fetch_one", lambda *_a, **_k: None)  # the SELECT saw nothing
    assert gmgn_feeds.write_token(feed_db, row) == "kept"
    kept = fetch_one(feed_db, "SELECT symbol, first_seen_ms FROM tokens WHERE chain='bsc' AND address=?", (row.token,))
    assert kept["symbol"] == "RACE" and kept["first_seen_ms"] == 7
    assert events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC) == []


def test_our_own_row_is_refreshed_when_the_token_graduates(feed_db):
    addr = NEAR_COMPLETION["address"]
    first = gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(trenches_payload()))
    assert first == 3
    before = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (addr,))
    assert before["migrated_ms"] is None
    graduated = {"code": 0, "data": {"completed": [{**NEAR_COMPLETION, "complete_timestamp": 1790010000,
                                                     "open_timestamp": 1790010000, "exchange": "pancake_v2"}],
                                     "near_completion": [], "new_creation": []}}
    gmgn_feeds.poll_once(Chain.BSC, "trenches", feed_db, runner=runner_for(graduated))
    after = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (addr,))
    assert after["migrated_ms"] == 1790010000000
    assert after["first_seen_ms"] == before["first_seen_ms"] and after["created_ms"] == before["created_ms"]
    assert jload(after["meta_json"])["trenches_category"] == "completed"
    # a refresh is not a second launch
    assert len(events_of(feed_db, EventKind.TOKEN_CREATED, Chain.BSC)) == 3
    assert len(fetch_all(feed_db, "SELECT 1 FROM triage_decisions WHERE token=?", (addr,))) == 1


def test_listener_chains_get_the_alpha_event_but_no_tokens_row(feed_db):
    n = gmgn_feeds.poll_once(Chain.SOL, "trenches", feed_db, runner=runner_for(trenches_payload(chain="sol")))
    assert n == 3
    assert tokens_on(feed_db, Chain.SOL) == []
    assert len(events_of(feed_db, EventKind.ALPHA_SIGNAL, Chain.SOL)) == 3
    row = gmgn_feeds.parse_trenches(trenches_payload(chain="sol"), Chain.SOL)[0]
    assert gmgn_feeds.write_token(feed_db, row) == "skipped"


def test_a_feed_row_without_token_facts_writes_no_token(feed_db):
    row = gmgn_feeds.AlphaRow(chain=Chain.BSC, feed="trending", token="0x" + "1" * 40, ts_ms=1)
    assert gmgn_feeds.write_token(feed_db, row) == "skipped"
    assert gmgn_feeds.write_alpha(feed_db, row) is True
    assert tokens_on(feed_db, Chain.BSC) == []


# ======================================================================================
# 3. amount_native / amount_quote / amount_token honesty on the track feeds
# ======================================================================================


def test_a_real_track_row_never_puts_a_ui_number_into_amount_native():
    row = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    assert row.amount_native is None and row.amount_native_basis == gmgn_feeds.NATIVE_BASIS_UNAVAILABLE
    assert row.amount_quote == "153.99549113740258" and row.quote_mint is None
    # amount_token IS the UI text, by contract (grade.HUMAN_UNIT_SOURCE_PREFIXES; section 3b)
    assert row.amount_token == "20530293.283241913" == row.amount_token_ui
    assert row.amount_token_atoms is None
    assert row.usd_value == "115.30412398913019" and row.side == "sell"
    assert "smart_degen" in row.tags and row.token_symbol == "Dust"
    swap = row.to_swap()
    assert swap["amount_native"] is None and swap["amount_quote"] == "153.99549113740258"
    assert swap["source"] == "gmgn:smartmoney"


@pytest.mark.parametrize(
    "chain, quote, expected",
    [
        (Chain.BSC, WBNB, "153995491137402580000"),
        (Chain.BSC, WBNB.upper().replace("0X", "0x"), "153995491137402580000"),
        (Chain.BSC, EVM_ZERO, "153995491137402580000"),
        (Chain.BSC, USDT_BSC, None),
        (Chain.BSC, None, None),
        (Chain.SOL, SOL_NATIVE_MINT, "153995491137"),
        (Chain.SOL, "So11111111111111111111111111111111111111111", None),
        (Chain.SOL, WBNB, None),
    ],
)
def test_amount_native_is_written_only_on_a_proven_native_quote(chain, quote, expected):
    atoms, basis = gmgn_feeds.native_atoms(chain, "153.99549113740258", quote)
    assert atoms == expected
    assert basis == (gmgn_feeds.NATIVE_BASIS_PAYLOAD if expected else gmgn_feeds.NATIVE_BASIS_UNAVAILABLE)


def test_a_payload_that_names_a_native_quote_yields_exact_base_units():
    row = gmgn_feeds.parse_smartmoney(track_payload({**TRACK_ROW, "quote_address": WBNB}), Chain.BSC)[0]
    assert row.amount_native == "153995491137402580000" and row.quote_mint == WBNB
    assert row.amount_native_basis == gmgn_feeds.NATIVE_BASIS_PAYLOAD
    assert Decimal(row.amount_native) == Decimal("153.99549113740258") * Decimal(10) ** 18


def test_ui_to_atoms_never_rounds_up_and_refuses_nonsense():
    assert gmgn_feeds.ui_to_atoms("1.9999999999", 9) == "1999999999"
    assert gmgn_feeds.ui_to_atoms("0.000000001", 9) == "1"
    assert gmgn_feeds.ui_to_atoms("1", None) is None
    assert gmgn_feeds.ui_to_atoms("abc", 9) is None
    assert gmgn_feeds.ui_to_atoms(None, 9) is None
    assert gmgn_feeds._dec_text(1e-05) == "0.00001"
    assert gmgn_feeds._dec_text(True) is None and gmgn_feeds._dec_text("") is None


def test_write_swap_stores_the_quote_leg_in_its_own_columns(feed_db):
    """MUTATION GUARD: dropping ``amount_quote`` from the INSERT fails here."""
    row = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    assert gmgn_feeds.write_swap(feed_db, row) is True
    stored = fetch_one(feed_db, "SELECT * FROM swaps WHERE tx=?", (row.tx,))
    assert stored["amount_native"] is None
    assert stored["amount_quote"] == "153.99549113740258" and stored["quote_mint"] is None
    # The contract: a gmgn row's amount_token is the provider's UI text, because
    # grade.HUMAN_UNIT_SOURCE_PREFIXES == ("gmgn:",) and grade.normalise_tape_rows scales
    # it (tests/test_tape_grading.py). None here is what made every gmgn-only wallet
    # ungradeable in round 1.
    assert stored["amount_token"] == "20530293.283241913"
    assert stored["usd_value"] == "115.30412398913019"
    assert stored["source"] == "gmgn:smartmoney"
    event = jload(events_of(feed_db, EventKind.WALLET_TRADE, Chain.BSC)[0]["payload"])
    assert event["amount_token_ui"] == "20530293.283241913"
    assert event["amount_token_atoms"] is None and event["token_decimals"] is None
    assert event["amount_native_basis"] == gmgn_feeds.NATIVE_BASIS_UNAVAILABLE
    assert event["token_decimals_basis"] == "unavailable"
    assert event["source"] == "gmgn:smartmoney" and event["tags"] == ["smart_degen", "gmgn"]


def test_known_decimals_ride_on_the_event_and_never_replace_the_ui_text(feed_db):
    """MUTATION GUARD for the round-1 bug: base units written into ``amount_token`` fail here.

    The contract (grade.HUMAN_UNIT_SOURCE_PREFIXES, tests/test_tape_grading.py) is that a
    gmgn row's ``amount_token`` is UI text whatever the registry knows. The atoms are
    evidence for the ``wallet.trade`` event only; in the column they would sit next to
    rows written before the registry learned the decimals and ``pnl`` would flag the
    episode ``oversold`` (section 3b).
    """
    token = TRACK_ROW["base_address"]
    feed_db.execute(
        "INSERT INTO tokens (chain, address, decimals, first_seen_ms, meta_json) VALUES ('bsc', ?, 18, 1, '{}')",
        (token,),
    )
    row = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    resolved = gmgn_feeds.resolve_token_atoms(feed_db, row)
    assert resolved.amount_token == "20530293.283241913"  # untouched
    assert resolved.amount_token_atoms == "20530293283241913000000000"
    assert resolved.token_decimals == 18 and resolved.token_decimals_basis == "tokens_row"
    assert resolved.amount_token_ui == "20530293.283241913"
    assert gmgn_feeds.write_swap(feed_db, row) is True
    stored = fetch_one(feed_db, "SELECT amount_token FROM swaps WHERE tx=?", (row.tx,))
    assert stored["amount_token"] == "20530293.283241913"
    event = jload(events_of(feed_db, EventKind.WALLET_TRADE, Chain.BSC)[0]["payload"])
    assert event["amount_token"] == "20530293.283241913"
    assert event["amount_token_atoms"] == "20530293283241913000000000"
    assert event["token_decimals"] == 18 and event["token_decimals_basis"] == "tokens_row"


def test_unknown_decimals_leave_the_ui_text_in_place_and_never_touch_the_chain(feed_db):
    row = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    resolved = gmgn_feeds.resolve_token_atoms(feed_db, row)  # `_fetch_decimals` is forbidden by the fixture
    assert resolved.amount_token == "20530293.283241913"
    assert resolved.amount_token_atoms is None and resolved.token_decimals_basis == "unavailable"


def test_amount_native_never_carries_a_dot_and_amount_token_keeps_the_ui_text(feed_db):
    """``amount_native`` is base units or None. ``amount_token`` on a gmgn row is UI text BY
    CONTRACT (grade.HUMAN_UNIT_SOURCE_PREFIXES): forbidding a dot there was the round-1 bug.
    """
    rows = gmgn_feeds.parse_smartmoney(
        track_payload(TRACK_ROW, {**TRACK_ROW, "quote_address": WBNB, "transaction_hash": "0x" + "cd" * 32},
                      buy_row(quote_amount=0.25, token_amount=1e-05)),
        Chain.BSC,
    )
    assert len(rows) == 3
    for row in rows:
        gmgn_feeds.write_swap(feed_db, row)
    stored = fetch_all(feed_db, "SELECT amount_token, amount_native, source FROM swaps")
    assert len(stored) == 3
    for r in stored:
        assert r["amount_native"] is None or re.fullmatch(r"\d+", r["amount_native"]), r
        assert r["source"].startswith(grade.HUMAN_UNIT_SOURCE_PREFIXES), r
        assert r["amount_token"] is not None and Decimal(r["amount_token"]) > 0, r
    assert {r["amount_token"] for r in stored} == {"20530293.283241913", "0.00001"}


# --------------------------------------------------------------------------------------
# 3b. the contract end to end: a gmgn-only wallet must be able to close a round trip
# --------------------------------------------------------------------------------------


def _buy_then_sell(feed_db, *, register_decimals_between: bool = False) -> str:
    """A buy and then the real sell of the same token by the real track wallet, via write_swap."""
    buy = gmgn_feeds.parse_smartmoney(
        track_payload(buy_row(timestamp=TRACK_ROW["timestamp"] - 600, amount_usd=100.0)), Chain.BSC
    )[0]
    assert gmgn_feeds.write_swap(feed_db, buy) is True
    if register_decimals_between:
        # An upsert since 2026-09-23: `write_swap` now REGISTERS the traded token (with
        # decimals unknown), so the registry learning decimals later is an update to an
        # existing row rather than a first insert. The scenario is unchanged -- decimals
        # arrive between the buy and the sell -- only the statement that models it.
        feed_db.execute(
            "INSERT INTO tokens (chain, address, decimals, first_seen_ms, meta_json) "
            "VALUES ('bsc', ?, 18, 1, '{}') "
            "ON CONFLICT(chain, address) DO UPDATE SET decimals=18",
            (TRACK_ROW["base_address"],),
        )
    sell = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    assert gmgn_feeds.write_swap(feed_db, sell) is True
    return TRACK_ROW["maker"]


AS_OF_MS = TRACK_ROW["timestamp"] * 1000 + 10**6


def test_a_gmgn_only_wallet_with_no_registry_decimals_closes_a_round_trip(feed_db):
    """The verifier's end-to-end case. Round 1 graded exactly this closed=0 / contaminated=1.

    On bsc the gmgn feed is the ONLY swaps source (MEASURED 2026-09-22 live: 13,753 gmgn
    rows, 436 wallets, 2,897 buy+sell pairs, no other source), so a shape that cannot
    close a round trip here means no bsc wallet can ever earn a grade.
    """
    wallet = _buy_then_sell(feed_db)
    # The property is "the registry knows no decimals for this token", which is what the
    # grader has to cope with. It used to be spelled "there is no tokens row at all" --
    # true only until `write_swap` began registering the tokens it sees, which it now does
    # with `decimals` explicitly NULL. Asserting the decimals keeps testing the thing;
    # asserting the row count tested the old plumbing.
    assert fetch_one(
        feed_db,
        "SELECT COUNT(*) AS n FROM tokens WHERE decimals IS NOT NULL",
    )["n"] == 0
    ev = grade.build_tape_evidence(wallet, Chain.BSC, feed_db, as_of_ms=AS_OF_MS)
    assert ev.tape is not None and ev.tape.money_axis == "usd_micro"
    assert ev.tape.rows == 2 and ev.tape.rows_without_money == 0
    pnl = ev.pnl
    assert pnl is not None
    assert pnl.closed_episodes == 1 and pnl.contaminated_episodes == 0
    assert pnl.win_rate == 1.0
    assert pnl.cost_native == 100_000_000 and pnl.proceeds_native == 115_304_123  # micro-USD


def test_a_buy_before_and_a_sell_after_the_registry_learns_decimals_still_closes(feed_db):
    """The verifier's secondary finding: base units next to UI text is an ``oversold`` episode.

    MUTATION GUARD: ``resolve_token_atoms`` writing atoms into ``amount_token`` once the
    decimals are known fails here, because the buy was written before they were.
    """
    wallet = _buy_then_sell(feed_db, register_decimals_between=True)
    stored = fetch_all(feed_db, "SELECT side, amount_token FROM swaps ORDER BY ts_ms")
    assert [(r["side"], r["amount_token"]) for r in stored] == [
        ("buy", "20530293.283241913"), ("sell", "20530293.283241913"),
    ]
    ev = grade.build_tape_evidence(wallet, Chain.BSC, feed_db, as_of_ms=AS_OF_MS)
    assert ev.pnl is not None
    assert ev.pnl.closed_episodes == 1 and ev.pnl.contaminated_episodes == 0


def test_the_column_contract_is_the_one_the_grader_documents():
    """If the grader ever stops treating gmgn rows as human units, this file must change too."""
    assert "gmgn:" in grade.HUMAN_UNIT_SOURCE_PREFIXES
    row = gmgn_feeds.parse_smartmoney(track_payload(), Chain.BSC)[0]
    swap = row.to_swap()
    assert swap["source"].startswith(grade.HUMAN_UNIT_SOURCE_PREFIXES)
    assert swap["amount_token"] == row.amount_token_ui == "20530293.283241913"
    assert "amount_token_atoms" not in swap  # atoms never reach the table


# ======================================================================================
# 4. lanes._net_buyers tolerates what the table actually holds
# ======================================================================================


def _buy(wallet: str, native, usd: str = "50") -> dict:
    return {"wallet": wallet, "ts_ms": 1_000, "side": "buy", "usd_value": usd, "amount_native": native}


def test_a_dotted_amount_native_reaching_the_lane_is_unavailable_not_a_crash(monkeypatch, caplog):
    """MUTATION GUARD: ``int(row["amount_native"])`` on ``"0.147380625"`` fails here."""
    monkeypatch.delattr(lanes._net_buyers, "_warned_ui_units", raising=False)
    rows = [_buy("w1", "0.147380625"), _buy("w2", "0.5"), _buy("w3", "150000000000000000"), _buy("w4", None)]
    with caplog.at_level(logging.WARNING, logger="kaiba.execution.lanes"):
        out = lanes._net_buyers(rows, since_ms=0, until_ms=2_000, min_buy_usd=Decimal(0))
    assert set(out) == {"w1", "w2", "w3", "w4"}
    assert out["w1"]["native"] == 0 and out["w1"]["buys"] == 1 and out["w1"]["buy_usd"] == Decimal(50)
    assert out["w3"]["native"] == 150000000000000000
    assert out["w4"]["native"] == 0 and out["w4"]["buys"] == 1
    warnings = [r for r in caplog.records if "amount_native" in r.getMessage()]
    assert len(warnings) == 1, "two dotted rows must produce exactly one warning"
    assert "UNAVAILABLE" in warnings[0].getMessage()


def test_a_float_typed_amount_native_is_not_silently_truncated(monkeypatch):
    monkeypatch.delattr(lanes._net_buyers, "_warned_ui_units", raising=False)
    out = lanes._net_buyers([_buy("w", 2.9)], since_ms=0, until_ms=2_000, min_buy_usd=Decimal(0))
    assert out["w"]["native"] == 0 and out["w"]["buys"] == 1


def test_integer_and_zero_native_amounts_behave_exactly_as_before():
    out = lanes._net_buyers([_buy("a", 0), _buy("a", "7"), _buy("b", "")], since_ms=0, until_ms=2_000, min_buy_usd=Decimal(0))
    assert out["a"]["native"] == 7 and out["a"]["buys"] == 2 and out["b"]["native"] == 0


# ======================================================================================
# 5. the one-shot backfill for the rows written before this
# ======================================================================================


def _seed_swap(conn, *, source: str, amount_native, amount_quote=None, tx: str) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, "
        " amount_quote, usd_value, source) VALUES ('bsc', ?, 1, 'w', 't', 'buy', '1', ?, ?, '5', ?)",
        (tx, amount_native, amount_quote, source),
    )


def _seed_live_shapes(conn) -> None:
    _seed_swap(conn, source="gmgn:smartmoney", amount_native="0.147380625", tx="dotted")
    _seed_swap(conn, source="gmgn:kol", amount_native="5", tx="whole-ui")
    _seed_swap(conn, source="helius:backfill", amount_native="0.5", tx="foreign-dotted")
    _seed_swap(conn, source="pumpfun:trades", amount_native="450000000", tx="lamports")
    _seed_swap(conn, source="pumpfun:trades", amount_native=None, tx="null")
    _seed_swap(conn, source="gmgn:smartmoney", amount_native=None, amount_quote="1.5", tx="already-moved")
    _seed_swap(conn, source="gmgn:smartmoney", amount_native="2500000000", amount_quote="2.5", tx="proven-native")


def test_backfill_moves_ui_values_out_of_amount_native_and_touches_nothing_else(tmp_db):
    _seed_live_shapes(tmp_db)
    dry = gmgn_feeds.backfill_amount_native(tmp_db, dry_run=True)
    assert dry["candidates"] == 3 and dry["moved"] == 0 and dry["remaining"] == 3
    assert dry["by_source"] == {"gmgn:smartmoney": 1, "gmgn:kol": 1, "helius:backfill": 1}
    assert fetch_one(tmp_db, "SELECT amount_native FROM swaps WHERE tx='dotted'")["amount_native"] == "0.147380625"

    report = gmgn_feeds.backfill_amount_native(tmp_db, batch_size=2)
    assert report["moved"] == 3 and report["remaining"] == 0 and report["dotted_amount_native_left"] == 0
    rows = {r["tx"]: r for r in fetch_all(tmp_db, "SELECT tx, amount_native, amount_quote, quote_mint, amount_token FROM swaps")}
    assert rows["dotted"]["amount_native"] is None and rows["dotted"]["amount_quote"] == "0.147380625"
    assert rows["whole-ui"]["amount_native"] is None and rows["whole-ui"]["amount_quote"] == "5"
    assert rows["foreign-dotted"]["amount_native"] is None and rows["foreign-dotted"]["amount_quote"] == "0.5"
    assert rows["lamports"]["amount_native"] == "450000000" and rows["lamports"]["amount_quote"] is None
    assert rows["null"]["amount_native"] is None and rows["null"]["amount_quote"] is None
    assert rows["already-moved"]["amount_quote"] == "1.5"
    assert rows["proven-native"]["amount_native"] == "2500000000" and rows["proven-native"]["amount_quote"] == "2.5"
    assert all(r["quote_mint"] is None and r["amount_token"] == "1" for r in rows.values())

    again = gmgn_feeds.backfill_amount_native(tmp_db)
    assert again["candidates"] == 0 and again["moved"] == 0


def test_backfill_cli_prints_a_report_and_is_safe_to_rerun(tmp_db, tmp_path, capsys):
    _seed_live_shapes(tmp_db)
    tmp_db.commit()
    db = str(tmp_path / "kaiba.db")
    assert gmgn_feeds.main(["backfill-amount-native", "--db", db, "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and dry["candidates"] == 3 and dry["moved"] == 0
    assert gmgn_feeds.main(["backfill-amount-native", "--db", db, "--batch", "1"]) == 0
    run = json.loads(capsys.readouterr().out)
    assert run["moved"] == 3 and run["remaining"] == 0
    assert gmgn_feeds.main(["backfill-amount-native", "--db", db]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == 0
    assert gmgn_feeds.main(["backfill-amount-native", "--db", str(tmp_path / "missing.db")]) == 2


# ======================================================================================
# 6. provenance labels, and the constants come from the tree
# ======================================================================================

LABELS = ("MEASURED", "DEFINITIONAL", "INVENTED", "DERIVED", "STRUCTURAL")


@pytest.mark.parametrize(
    "name",
    ["NATIVE_QUOTE_ADDRESSES", "LISTENER_CHAINS", "SCREEN_NEW_TOKENS", "TRENCHES_CATEGORIES", "BACKFILL_PREDICATE",
     "TRENCHES_FILTER_PRESET"],
)
def test_every_new_constant_says_where_its_value_came_from(name):
    lines = inspect.getsource(gmgn_feeds).splitlines()
    idx = next(i for i, line in enumerate(lines) if re.match(rf"^{name}\b", line))
    comment: list[str] = []
    for line in reversed(lines[:idx]):
        if line.startswith("#"):
            comment.append(line)
        else:
            break
    text = "\n".join(comment)
    assert comment, f"{name} has no provenance comment"
    assert any(word in text for word in LABELS), f"{name} is not labelled: {text[:120]}"


def test_native_quote_addresses_are_the_trees_constants_not_typed_from_memory():
    from kaiba.providers.native_price import WRAPPED_NATIVE

    assert gmgn_feeds.NATIVE_QUOTE_ADDRESSES[Chain.SOL] == frozenset({SOL_NATIVE_MINT.lower()})
    assert gmgn_feeds.NATIVE_QUOTE_ADDRESSES[Chain.BSC] == frozenset({EVM_ZERO, WRAPPED_NATIVE[Chain.BSC]})
    assert gmgn_feeds.NATIVE_QUOTE_ADDRESSES[Chain.ROBINHOOD] == frozenset({EVM_ZERO})
    assert gmgn_feeds.LISTENER_CHAINS == frozenset({Chain.SOL, Chain.ROBINHOOD})


def test_the_feed_registry_still_polls_trenches_on_bsc():
    assert Chain.BSC in gmgn_feeds.FEEDS["trenches"].chains
    assert gmgn_feeds.FEEDS["trenches"].parser is gmgn_feeds.parse_trenches
