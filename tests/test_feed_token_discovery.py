"""Offline discovery contracts, using synthetic token observations (not live coverage)."""

from __future__ import annotations

import contextlib
import json
import socket

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain
from kaiba.execution import scanner, triage
from kaiba.ingest import gmgn_feeds as gf

TOKENS = {
    Chain.SOL: "5uiWN8tHqR4ykLDt88UR8htfcG6YK7fqcadfKBgDEPMh",
    Chain.BSC: "0x1234567890abcdef1234567890abcdef12345678",
    Chain.ROBINHOOD: "0xabcdef1234567890abcdef1234567890abcdef12",
}


@pytest.fixture
def feed_db(tmp_db, monkeypatch):
    @contextlib.contextmanager
    def passthrough(*_args, **_kwargs):
        yield

    def no_network(*_args, **_kwargs):
        raise AssertionError("discovery and tier-0 must stay offline")

    monkeypatch.setattr(gf, "guarded", passthrough)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    triage.set_queue(None)
    triage.reset_dedup()
    yield tmp_db
    triage.set_queue(None)
    triage.reset_dedup()


def poll(conn, chain, feed, rows):
    return gf.poll_once(chain, feed, conn, runner=lambda *_a, **_k: rows)


@pytest.mark.parametrize("feed", ["smartmoney", "kol", "signal", "trenches", "trending"])
def test_every_gmgn_feed_shape_registers_and_screens_a_manual_token(feed_db, feed):
    address = TOKENS[Chain.BSC]
    if feed in {"smartmoney", "kol"}:
        rows = [{
            "transaction_hash": f"0x{feed}{'a' * 60}",
            "maker": "0xwallet000000000000000000000000000000001",
            "base_address": address,
            "side": "buy",
            "timestamp": 1790020000,
            "token_amount": 1,
            "base_token": {"name": "Manual trade", "symbol": "MT", "launchpad": ""},
            "program": "0xprogram0000000000000000000000000000000001",
        }]
    elif feed == "signal":
        rows = [{
            "id": "signal-manual",
            "token_address": address,
            "signal_type": 6,
            "trigger_at": 1790020000,
            "data": {
                "chain": "bsc", "address": address, "name": "Manual signal", "symbol": "MS",
                "launchpad": "", "program": "0xprogram0000000000000000000000000000000001",
            },
        }]
    else:
        rows = [{
            "address": address,
            "chain": "bsc",
            "name": f"Manual {feed}",
            "symbol": "MF",
            "launchpad": "",
            "program": "0xprogram0000000000000000000000000000000001",
        }]

    assert poll(feed_db, Chain.BSC, feed, rows) == 1
    token = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (address,))
    assert token is not None
    # The row must also carry what the feed actually said about the token, or a later
    # weakening of the facts (an empty mapping still registers an address) would pass.
    assert token["symbol"] == {"smartmoney": "MT", "kol": "MT", "signal": "MS"}.get(feed, "MF")
    assert token["launchpad"] is None, "an unnamed launchpad must not be invented"
    decision = fetch_one(feed_db, "SELECT * FROM triage_decisions WHERE chain='bsc' AND token=?", (address,))
    assert decision is not None
    assert decision["source"] == f"gmgn:{feed}"
    assert decision["launchpad"] == "unknown"
    triage.set_queue(None)
    work = scanner._db_queue_work(feed_db, 8, config=scanner.DEFAULT_CONFIG)
    assert [(item.chain, item.token) for item in work] == [(Chain.BSC, address)]


@pytest.mark.parametrize("chain", TOKENS)
def test_unknown_launchpad_trending_token_reaches_durable_dyor_queue(feed_db, chain):
    """A manual token needs no recognised launchpad, creation time, or buy instruction."""
    address = TOKENS[chain]
    assert poll(feed_db, chain, "trending", [{
        "address": address, "name": "Manual fixture", "symbol": "MANUAL",
        "launchpad": "", "launchpad_platform": "",
    }]) == 1
    token = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain=? AND address=?", (chain.value, address))
    assert token is not None, "a feed-observed manual token was silently dropped"
    assert token["launchpad"] is None and token["created_ms"] is None and token["decimals"] is None
    decision = fetch_one(
        feed_db, "SELECT * FROM triage_decisions WHERE chain=? AND token=?", (chain.value, address)
    )
    assert decision is not None, "registration alone is invisible to the scanner"
    assert decision["source"] == "gmgn:trending"
    assert decision["launchpad"] == "unknown", "unknown Solana launchpad must not default to pump.fun"
    assert decision["verdict"] == "defer", "unknown is data, not promotion or rejection"
    # Simulate the separate scanner process: only the durable DB can hand it this work.
    triage.set_queue(None)
    work = scanner._db_queue_work(feed_db, 8, config=scanner.DEFAULT_CONFIG)
    assert [(w.chain, w.token) for w in work] == [(chain, address)]
    assert fetch_all(feed_db, "SELECT * FROM signals") == []
    assert fetch_all(feed_db, "SELECT * FROM orders") == []


def test_launchpad_and_program_provenance_survive_registration(feed_db):
    address = TOKENS[Chain.BSC]
    row = {
        "address": address,
        "chain": "bsc",
        "name": "Fourmeme fixture",
        "symbol": "FM",
        "launchpad": "fourmeme",
        "launchpad_platform": "fourmeme",
        "program": "0xprogram0000000000000000000000000000000001",
        "exchange": "0xexchange00000000000000000000000000000001",
        "creator": "0xcreator000000000000000000000000000000001",
        "created_timestamp": 1790020000,
    }
    assert poll(feed_db, Chain.BSC, "trending", [row]) == 1
    token = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='bsc' AND address=?", (address,))
    assert token["launchpad"] == "fourmeme"
    meta = json.loads(token["meta_json"])
    assert meta["source"] == "gmgn:trending"
    assert meta["launchpad_platform"] == "fourmeme"
    assert meta["program"] == row["program"]
    assert meta["exchange"] == row["exchange"]


def test_listener_owned_row_is_not_overwritten_or_re_screened(feed_db):
    address = TOKENS[Chain.ROBINHOOD]
    feed_db.execute(
        "INSERT INTO tokens (chain, address, symbol, name, creator, created_ms, launchpad, first_seen_ms, "
        "meta_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("robinhood", address, "OWNER", "listener row", "0xowner", 11, "pons", 12, '{"source":"robinhood"}'),
    )
    row = {
        "address": address,
        "chain": "robinhood",
        "name": "GMGN row",
        "symbol": "GMGN",
        "launchpad": "unknown",
        "program": "gmgn-program",
    }
    assert poll(feed_db, Chain.ROBINHOOD, "trending", [row]) == 1
    token = fetch_one(feed_db, "SELECT * FROM tokens WHERE chain='robinhood' AND address=?", (address,))
    assert token["symbol"] == "OWNER" and token["launchpad"] == "pons"
    assert fetch_all(feed_db, "SELECT * FROM triage_decisions") == []

