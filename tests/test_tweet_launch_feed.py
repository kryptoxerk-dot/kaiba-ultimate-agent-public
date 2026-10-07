"""Source-selection contracts with no real sockets, credentials or subscriptions."""
import asyncio
from types import SimpleNamespace

import pytest

from kaiba.ingest import tweet_launch_feed as feed
from kaiba.ingest.x_stream import XPost


# Codex SOCIAL-HUNTS-FAST-20261007: legacy contracts isolate the optional research tee.
@pytest.fixture(autouse=True)
def isolated_social_config(monkeypatch):
    monkeypatch.setattr(feed.social_dispatch, 'configuration', lambda: {'enabled': False})
    monkeypatch.setattr(feed.social_dispatch, 'watched_accounts', lambda **kw: set())
    monkeypatch.setattr(feed.social_dispatch, 'offer', lambda p: False)


def config(**over):
    return SimpleNamespace(accounts={"elonmusk": ("sol", "bsc", "robinhood")},
                           max_tweet_age_s=20, require_image=True, logo_generate=False, **over)


def post(author="elonmusk"):
    return XPost(tweet_id="2045879341243043889", author=author, text="Kekius",
                 kind="post", media=(), created_ms=1000, received_ms=1200,
                 feed_delay_ms=None, backend="test")


async def collect(source):
    return [p async for p in source]


def test_legacy_default_preserves_account_monitor_source():
    assert feed.parse_config({}).sync_monitored_accounts


@pytest.mark.parametrize("backend", ["j7", "twitterapi_rule"])
def test_non_monitor_sources_never_request_monitor_subscription(backend):
    assert not feed.parse_config({"feed": {"backend": backend}}).sync_monitored_accounts


@pytest.mark.parametrize("raw", [{"feed": None}, {"feed": []}, {"feed": {"backend": "other"}},
                                 {"feed": {"region": "bad"}}])
def test_bad_configuration_refuses(raw):
    with pytest.raises(feed.FeedConfigurationError):
        feed.parse_config(raw)


@pytest.mark.parametrize("backend", ["twitterapi_monitor", "twitterapi_rule"])
def test_twitter_sources_filter_authors_and_preserve_original_post(monkeypatch, backend):
    expected = post()
    keys = []

    async def socket(key):
        keys.append(key)
        yield post("unwatched")
        yield expected
    monkeypatch.setattr(feed.x_stream, "stream", socket)
    got = asyncio.run(collect(feed.stream(feed.FeedConfig(backend), config(), api_key="fixture")))
    assert got == [expected] and got[0] is expected and keys == ["fixture"]


def test_missing_twitter_key_does_not_connect(monkeypatch):
    def unexpected(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(feed.x_stream, "stream", unexpected)
    with pytest.raises(feed.FeedConfigurationError, match="missing_twitterapi_key"):
        asyncio.run(collect(feed.stream(feed.FeedConfig("twitterapi_rule"), config())))


@pytest.mark.parametrize("generate_logo,require_image", [(False, True), (True, False)])
def test_j7_uses_social_session_and_allows_generated_logo(monkeypatch, generate_logo, require_image):
    expected = post()
    calls = []
    monkeypatch.setattr(feed.j7_launch_bridge, "session_token", lambda: "fixture-social-token")

    async def socket(token, authors, **kw):
        calls.append((token, authors, kw))
        yield expected
    monkeypatch.setattr(feed.j7_launch_bridge, "stream", socket)
    cfg = config()
    cfg.logo_generate = generate_logo
    got = asyncio.run(collect(feed.stream(feed.FeedConfig("j7"), cfg)))
    assert got == [expected]
    assert calls == [("fixture-social-token", cfg.accounts,
                      {"max_age_s": 20, "require_image": require_image, "region": "dfw"})]


def test_j7_auth_failure_never_falls_back_to_paid_twitter(monkeypatch):
    def missing():
        raise feed.j7_launch_bridge.FeedUnavailable("missing_j7_session")
    monkeypatch.setattr(feed.j7_launch_bridge, "session_token", missing)
    monkeypatch.setattr(feed.x_stream, "stream", lambda *a: pytest.fail("paid fallback"))
    with pytest.raises(feed.j7_launch_bridge.FeedUnavailable, match="missing_j7_session"):
        asyncio.run(collect(feed.stream(feed.FeedConfig("j7"), config(), api_key="fixture")))


def test_config_loader_reads_explicit_backend(tmp_path):
    path = tmp_path / "launch.yaml"
    path.write_text("feed:\n  backend: twitterapi_rule\n")
    assert feed.load_config(path) == feed.FeedConfig("twitterapi_rule")
