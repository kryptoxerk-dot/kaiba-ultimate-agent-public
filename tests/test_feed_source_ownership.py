"""Which feed found a token, and whose row it is. Two gaps the mutation pass left open.

The every-launchpad work on 2026-09-22 reported two surviving mutants, both about the
FEED LABEL rather than the discovery behaviour, and both real gaps in the specification
rather than in the code:

1. The ownership check reverted from a PREFIX match on ``tokens.meta_json["source"]`` to
   an equality match against the trenches value. Nothing failed, because the damage only
   appears on a SECOND sighting: our own trending-written row is then read as foreign and
   kept instead of refreshed, so a token we discovered stops being updated by the feed
   that discovered it. No test polled the same feed twice and looked.
2. The ``token.created`` event's ``source`` reverted to the trenches constant for every
   feed. Nothing failed, because the only assertion on that field pins the trenches case.

Both matter for the same reason: after the fact, the only way to tell a launchpad sighting
from a market-wide one is the label, and the label is what says a manually deployed token
was found at all.
"""

from __future__ import annotations

import contextlib
import json
import socket

import pytest

from kaiba.core.db import fetch_one
from kaiba.core.schemas import Chain
from kaiba.execution import triage
from kaiba.ingest import gmgn_feeds as gf

ADDR = "0x1234567890abcdef1234567890abcdef12345678"


@pytest.fixture
def feed_db(tmp_db, monkeypatch):
    @contextlib.contextmanager
    def passthrough(*_a, **_k):
        yield

    def no_network(*_a, **_k):
        raise AssertionError("discovery must stay offline")

    monkeypatch.setattr(gf, "guarded", passthrough)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    triage.set_queue(None)
    triage.reset_dedup()
    yield tmp_db
    triage.set_queue(None)
    triage.reset_dedup()


def poll(conn, chain, feed, rows):
    return gf.poll_once(chain, feed, conn, runner=lambda *_a, **_k: rows)


def meta(conn, chain: Chain = Chain.BSC) -> dict:
    row = fetch_one(conn, "SELECT meta_json FROM tokens WHERE chain=? AND address=?",
                    (chain.value, ADDR))
    return json.loads(row["meta_json"] or "{}")


# ------------------------------------------------------- the label names the feed


def test_the_row_records_which_feed_found_it(feed_db):
    poll(feed_db, Chain.BSC, "trending", [{"address": ADDR, "symbol": "A", "name": "a"}])
    assert meta(feed_db)["source"] == gf.token_source("trending") == "gmgn:trending"


def test_the_created_event_names_the_same_feed(feed_db):
    """Gap 2. The event is how the rest of the tree learns a token exists."""
    poll(feed_db, Chain.BSC, "trending", [{"address": ADDR, "symbol": "A", "name": "a"}])
    row = fetch_one(feed_db, "SELECT payload FROM events WHERE kind LIKE '%token%' "
                             "AND payload LIKE ? ORDER BY ts_ms DESC LIMIT 1", (f"%{ADDR}%",))
    assert row is not None, "registering a token must emit its created event"
    payload = json.loads(row["payload"])
    assert payload.get("source") == "gmgn:trending", payload
    assert payload.get("source") != gf.TOKEN_SOURCE, (
        "a trending sighting must not be labelled as a trenches one"
    )


def test_each_feed_gets_its_own_label():
    assert gf.token_source("trenches") == gf.TOKEN_SOURCE
    assert gf.token_source("trending") != gf.TOKEN_SOURCE
    for feed in ("trenches", "trending", "anything"):
        assert gf.token_source(feed).startswith(gf.TOKEN_SOURCE_PREFIX)


# ------------------------------------------------- we keep refreshing our own rows


def test_our_own_row_is_still_ours_on_a_second_sighting(feed_db):
    """Gap 1, and it only shows on the SECOND poll.

    An equality check against the trenches value reads our own trending row as foreign,
    so the feed that discovered a token stops being allowed to update it.
    """
    poll(feed_db, Chain.BSC, "trending", [{"address": ADDR, "symbol": "FIRST", "name": "first"}])
    assert fetch_one(feed_db, "SELECT symbol FROM tokens WHERE address=?", (ADDR,))["symbol"] == "FIRST"

    poll(feed_db, Chain.BSC, "trending",
         [{"address": ADDR, "symbol": "SECOND", "name": "second", "launchpad": "fourmeme"}])

    row = fetch_one(feed_db, "SELECT symbol, launchpad FROM tokens WHERE address=?", (ADDR,))
    assert row["symbol"] == "SECOND", (
        "our own row must stay refreshable by the feed that discovered it"
    )
    assert row["launchpad"] == "fourmeme", "later provenance must be allowed to land"


def test_a_row_another_writer_owns_is_never_taken_over(feed_db):
    """The other half of the same rule, and the one that must not regress while fixing it."""
    feed_db.execute(
        "INSERT INTO tokens (chain, address, symbol, name, launchpad, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (Chain.BSC.value, ADDR, "OWNER", "listener row", "pons", 1, '{"source":"robinhood"}'),
    )
    feed_db.commit()

    poll(feed_db, Chain.BSC, "trending", [{"address": ADDR, "symbol": "GMGN", "name": "gmgn"}])

    row = fetch_one(feed_db, "SELECT symbol, launchpad FROM tokens WHERE address=?", (ADDR,))
    assert row["symbol"] == "OWNER" and row["launchpad"] == "pons"
    assert meta(feed_db)["source"] == "robinhood", "the owner's label must survive"
