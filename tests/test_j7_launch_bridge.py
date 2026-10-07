"""Feed enrichment, attribution, replay safety and the real three-chain planner seam."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import tweet_launch as tl
from kaiba.ingest import j7_launch_bridge as j7


def _one_chain_config():
    """Lead 2026-10-07: the shipped config now launches @elonmusk on three chains (owner brief);
    these tests are about attribution/enrichment, so pin one chain instead of reading ambient config."""
    return replace(tl.load_config(), accounts={"elonmusk": (Chain.SOL,)}, account_kinds={})

NOW = 1_791_300_000_000
ID = "2045879341243043889"
IMAGE = "https://pbs.twimg.com/media/real.jpg"


def tweet(**changes):
    return {"id": ID, "createdAt": NOW - 500, "type": "TWEET", "text": "Kekius Maximus",
            "author": {"handle": "ElonMusk", "id": "44196397"},
            "media": {"images": [{"url": IMAGE}], "videos": []}, **changes}


def bridge(**changes):
    return j7.Bridge(["elonmusk", "cz_binance", "vladtenev"], started_ms=NOW-1000, **changes)


def test_image_enrichment_is_waited_for_before_one_real_planner_handoff():
    b = bridge()
    first = b.ingest("tweet", tweet(media={"images": []}), now_ms=NOW)
    assert first.status == "pending" and first.reason == "awaiting_tweet_image"
    full = b.ingest("tweet_update", {"id": ID, "media": {"images": [{"url": IMAGE}]}}, now_ms=NOW+800)
    assert full.status == "ready" and full.post.media == (IMAGE,)
    assert full.first_seen_ms == NOW and full.post.received_ms == NOW+800
    assert full.post.raw["eligible_observed_ms"] == NOW+800 and full.revision == 2
    assert full.post.author == "elonmusk" and full.post.url == f"https://x.com/elonmusk/status/{ID}"
    [plan] = j7.shadow_plans(full.post, _one_chain_config(), now_ms=NOW+800)
    assert plan.verdict == "launch" and plan.chain is Chain.SOL
    assert b.ingest("tweet", tweet(), now_ms=NOW+900).reason == "delivered"


def test_empty_update_does_not_erase_author_text_or_media_before_later_kind_arrives():
    b = bridge()
    assert b.ingest("tweet", tweet(type=""), now_ms=NOW).status == "pending"
    result = b.ingest("tweet_update", {"id": ID, "author": {}, "text": "", "media": {"images": []},
                                     "type": "TWEET"}, now_ms=NOW+10)
    assert result.post.author == "elonmusk" and result.post.text == "Kekius Maximus"
    assert result.post.media == (IMAGE,)


def test_backlog_and_its_later_updates_never_become_actionable():
    b = bridge()
    assert b.ingest("initialTweets", tweet(), now_ms=NOW).reason == "backlog"
    assert b.ingest("tweet_update", tweet(), now_ms=NOW+10).reason == "backlog"
    assert b.ingest("tweet", tweet(), now_ms=NOW+20).post is None


def test_delete_invalidates_an_already_queued_post():
    b = bridge()
    post = b.ingest("tweet", tweet(), now_ms=NOW).post
    assert b.queued_post_valid(post, now_ms=NOW)
    assert b.ingest("tweet_deleted", {"id": ID}, now_ms=NOW+1).reason == "deleted"
    assert not b.queued_post_valid(post, now_ms=NOW+2)
    assert b.ingest("tweet_update", tweet(), now_ms=NOW+3).post is None


@pytest.mark.parametrize("delta,reason", [(-1001, "before_connection"), (1001, "future_created_time")])
def test_invalid_publication_window_is_not_repaired_to_now(delta, reason):
    b = bridge()
    result = b.ingest("tweet", tweet(createdAt=NOW+delta), now_ms=NOW)
    assert result.reason == reason and result.post is None


def test_enrichment_deadline_uses_current_time_and_queue_latency():
    b = bridge()
    assert b.ingest("tweet", tweet(media={"images": []}), now_ms=NOW).status == "pending"
    result = b.ingest("tweet_update", {"id": ID, "media": {"images": [{"url": IMAGE}]}}, now_ms=NOW+21_000)
    assert result.reason == "too_late" and result.post is None
    b = bridge()
    post = b.ingest("tweet", tweet(), now_ms=NOW).post
    assert b.queued_post_valid(post, now_ms=NOW+100)
    assert not b.queued_post_valid(post, now_ms=NOW+21_000)


def test_updates_cannot_move_publication_time_forward_to_reset_age():
    b = bridge()
    b.ingest("tweet", tweet(media={"images": []}), now_ms=NOW)
    result = b.ingest("tweet_update", tweet(createdAt=NOW+5000), now_ms=NOW+5000)
    assert result.reason == "created_time_changed" and result.post is None


@pytest.mark.parametrize("change", [{"handle": "cz_binance"}, {"handle": "elonmusk", "id": "different"}])
def test_conflicting_author_update_never_borrows_another_watched_identity(change):
    b = bridge()
    b.ingest("tweet", tweet(media={"images": []}), now_ms=NOW)
    result = b.ingest("tweet_update", tweet(author=change), now_ms=NOW+10)
    assert result.reason == "author_changed" and result.post is None


def test_global_unwatched_feed_does_not_fill_the_watched_dedupe_capacity():
    b = bridge(capacity=1)
    for i in range(20):
        assert b.ingest("tweet", tweet(id=str(int(ID)+i), author={"handle": "someone"}), now_ms=NOW).reason == "unwatched_author"
    assert not b.entries
    assert b.ingest("tweet", tweet(), now_ms=NOW).post
    assert b.ingest("tweet", tweet(id=str(int(ID)+100)), now_ms=NOW).reason == "dedupe_capacity"
    assert b.ingest("tweet", tweet(), now_ms=NOW+1).reason == "delivered"


@pytest.mark.parametrize("event,payload", [("following_update", tweet()), ("external_message", tweet()),
                                          ("tweet", None), ("tweet", {"id": "not-a-status"})])
def test_only_valid_x_tweet_events_are_candidates(event, payload):
    assert bridge().ingest(event, payload, now_ms=NOW).post is None


@pytest.mark.parametrize("url", ["http://pbs.twimg.com/media/a.jpg", "https://evil.invalid/media/a.jpg",
                                 "https://pbs.twimg.com@evil.invalid/media/a.jpg", "file:///etc/passwd",
                                 "https://pbs.twimg.com:9999/media/a.jpg", "https://pbs.twimg.com/profile_images/a.jpg"])
def test_only_actual_x_image_urls_can_become_a_logo(url):
    result = bridge().ingest("tweet", tweet(media={"images": [{"url": url}]}), now_ms=NOW)
    assert result.reason == "awaiting_tweet_image" and result.post is None


@pytest.mark.parametrize("value", [None, "2026-10-06T10:00:00", False, float("nan"), {}, "junk"])
def test_missing_or_malformed_created_time_does_not_get_a_current_timestamp(value):
    assert bridge().ingest("tweet", tweet(createdAt=value), now_ms=NOW).reason == "missing_created_time"


def test_publication_iso_with_offset_is_normalized():
    assert j7.timestamp_ms("2026-10-06T00:00:00Z") == j7.timestamp_ms("2026-10-06T07:00:00+07:00")


@pytest.mark.parametrize("kind,flag,expected", [("RETWEET", "isRetweet", "repost"),
                                              ("REPLY", "isReply", "reply"), ("QUOTE", "isQuote", "quote")])
def test_post_attribution_is_not_taken_from_original_or_quoted_author(kind, flag, expected):
    payload = tweet(type=kind, **{flag: True}, originalAuthor={"handle": "cz_binance"},
                    quotedTweet={"handle": "vladtenev", "text": "Buy", "media": {"images": [{"url": IMAGE}]}})
    post = bridge().ingest("tweet", payload, now_ms=NOW).post
    assert post.author == "elonmusk" and post.kind == expected
    if kind != "QUOTE":
        [p] = j7.shadow_plans(post, _one_chain_config(), now_ms=NOW)
        assert p.verdict == "skip"


def test_one_author_can_automatically_plan_all_three_routes_with_explicit_native_amounts():
    config = tl.load_config()
    chains = {chain: replace(route, buy_amt_native=Decimal("0.01"), max_buy_native=Decimal("0.02"), live=True)
              for chain, route in config.chains.items()}
    config = replace(config, mode="live", armed_by="fixture", chains=chains,
                     accounts={"elonmusk": tuple(chains)})
    post = bridge().ingest("tweet", tweet(), now_ms=NOW).post
    plans = j7.shadow_plans(post, config, now_ms=NOW)
    assert {(p.chain.value, p.dex) for p in plans} == {("sol", "pump"), ("bsc", "flap"), ("robinhood", "pons")}
    assert all(p.verdict == "launch" and p.mode == "shadow" and p.buy_amt_native == Decimal("0.01") for p in plans)
    assert config.mode == "live"  # Caller configuration untouched; preview cannot arm it.


class FakeClient:
    def __init__(self, events, *, auth_failure=False):
        self.handlers = {}
        self.connected = False
        self.events = events
        self.auth_failure = auth_failure
        self.emissions = []

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    def on(self, event, handler=None):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register(handler) if handler else register

    async def emit(self, name, payload):
        self.emissions.append((name, payload))

    async def connect(self, host, **kwargs):
        assert host == j7.HOSTS["dfw"] and kwargs["auth"] == {"token": "fixture-session"}
        assert kwargs["transports"] == ["websocket"]
        self.connected = True
        await self.handlers["connect"]()
        for event, payload in self.events:
            await self.handlers[event](copy.deepcopy(payload))
        if self.auth_failure:
            await self.handlers["auth_error"]({"error": "fixture-session"})

    async def disconnect(self):
        self.connected = False
        await self.handlers["disconnect"]()


def test_async_socket_protocol_emits_session_and_yields_only_enriched_live_post(monkeypatch):
    monkeypatch.setattr(j7.time, "time", lambda: NOW/1000)
    client = FakeClient([("initialTweets", [tweet(id=str(int(ID)-1), createdAt=NOW-1000)]),
                         ("tweet", tweet(createdAt=NOW, media={"images": []})),
                         ("tweet_update", {"id": ID, "media": {"images": [{"url": IMAGE}]}})])
    async def run():
        source = j7.stream("fixture-session", ["elonmusk"], client_factory=lambda: client)
        post = await anext(source)
        await source.aclose()
        return post
    post = asyncio.run(run())
    assert post.media == (IMAGE,) and post.backend == "j7tracker"
    assert client.emissions == [("user_connected", "fixture-session")] and not client.connected


def test_auth_error_is_unavailable_and_never_yields_or_exposes_session(monkeypatch):
    monkeypatch.setattr(j7.time, "time", lambda: NOW/1000)
    client = FakeClient([], auth_failure=True)
    async def run():
        source = j7.stream("fixture-session", ["elonmusk"], client_factory=lambda: client)
        with pytest.raises(j7.FeedUnavailable, match="^feed_auth_error$"):
            await anext(source)
    asyncio.run(run())
    assert not client.connected
