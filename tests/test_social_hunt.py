"""Social events must never arm financial actions or wait for creative enrichment."""

from dataclasses import replace
from pathlib import Path

from kaiba.hunters import social_hunt as sh
from kaiba.ingest import social_dispatch as sd
from kaiba.ingest.x_stream import XPost

CFG = {
    "enabled": True,
    "accounts": {"nft": ["opensea"], "airdrop": ["base"], "token": ["sama"]},
    "max_post_age_s": 86400,
}


def event(text="Mint allowlist and airdrop snapshot are live", **kw):
    return {
        "tweet_id": "2107623610483720463",
        "author": "opensea",
        "text": text,
        "kind": "post",
        "created_ms": 10000,
        "received_ms": 10005,
        "dispatched_ms": 10006,
        **kw,
    }


def test_one_post_routes_to_multiple_hunts_without_a_trade():
    rows = sh.classify(event(), CFG, now_ms=10007)
    assert {r["lane"] for r in rows} == {"nft", "airdrop"}
    assert all(not r["financial_execution"] for r in rows)
    assert rows[0]["dispatch_latency_ms"] == 2 and rows[0]["feed_latency_ms"] == 5


def test_quote_contract_is_preserved_without_becoming_a_chain_claim():
    ca = "0x" + "1" * 40
    rows = sh.classify(
        event("Useful context", quoted={"author": "project", "tweet_id": "123456", "text": "Airdrop " + ca}),
        CFG,
        now_ms=10007,
    )
    assert {r["lane"] for r in rows} == {"airdrop", "token"}
    assert all(r["addresses"] == [ca] and "chain" not in r for r in rows)
    assert rows[0]["quoted"]["author"] == "project"


def test_stale_future_repost_and_malformed_posts_are_refused():
    for e in [event(created_ms=100000), event(created_ms=1), event(kind="repost"), event(tweet_id="bad")]:
        assert sh.classify(e, {**CFG, "max_post_age_s": 1}, now_ms=10007) == []
    assert sh.classify(event(), {**CFG, "enabled": False}, now_ms=10007) == []
    assert sh.classify(event(), CFG, now_ms=10007)  # positive control


def test_revisions_dedupe_and_preserve_first_receipt(tmp_path):
    store = sh.Store(tmp_path / "hunt.db")
    e = event()
    store.record(e, [], 10007)
    later = {**e, "received_ms": 10200, "text": "Airdrop snapshot"}
    rows = sh.classify(later, CFG, now_ms=10201)
    store.record(later, rows, 10201)
    store.record(later, rows, 10202)
    assert store.query("SELECT COUNT(*) n FROM posts")[0]["n"] == 1
    assert store.query("SELECT received_ms FROM posts")[0]["received_ms"] == 10005
    assert store.query("SELECT COUNT(*) n FROM signals")[0]["n"] == 1
    assert sh.status(store)["dispatch"]["p50_ms"] == 197


def test_maintenance_does_not_delete_recent_signals(tmp_path):
    s = sh.Store(tmp_path / "hunt.db")
    e = event()
    s.record(e, sh.classify(e, CFG, now_ms=10007), 10007)
    new = event(tweet_id="2107623610483720464")
    now = 2 * 86400000
    s.record(new, sh.classify(new, CFG, now_ms=10007), now)
    s.maintain(1, now)
    assert s.query("SELECT tweet_id FROM posts") == [{"tweet_id": new["tweet_id"]}]
    assert all(r["tweet_id"] == new["tweet_id"] for r in s.query("SELECT tweet_id FROM signals"))


def test_offer_is_nonblocking_and_drops_are_visible(monkeypatch):
    class Fake:
        def sendto(self, *args):
            raise BlockingIOError

    monkeypatch.setattr(sd, "configuration", lambda: {"enabled": True, "socket_path": "unused"})
    monkeypatch.setattr(sd, "_socket", Fake())
    monkeypatch.setattr(sd, "_drops", 0)
    post = XPost("2107623610483720463", "sama", "Mint now", "post", (), 10000, 10005, 5, "fixture")
    assert sd.offer(post) is False and sd._drops == 1
    monkeypatch.setattr(sd, "configuration", lambda: {"enabled": False})
    assert sd.offer(replace(post, text="other")) is False and sd._drops == 1


def test_config_rejects_financial_lanes_and_handles(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("enabled: true\naccounts:\n  buy: [sama]\n")
    import pytest

    with pytest.raises(ValueError):
        sh.load_config(p)
    p.write_text('enabled: true\naccounts:\n  nft: ["invalid handle"]\n')
    with pytest.raises(ValueError):
        sh.load_config(p)


def test_shipped_scopes_exist_in_j7_or_are_traced_upstream():
    import csv

    c = sh.load_config()
    j7 = {r["handle"].lower() for r in csv.DictReader(Path(c["roster"]).open())}
    if j7:  # the public snapshot ships the J7 roster header-only; export your own to check
        assert all(h in j7 for lane in ("nft", "airdrop") for h in c["accounts"][lane])
    assert c["accounts"]["token"] and set(c["accounts"]) == {"nft", "airdrop", "token"}


def test_research_tee_arrives_before_launch_work_and_never_yields_research_authors(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from kaiba.ingest import tweet_launch_feed as feed

    research = XPost("2107623610483720463", "opensea", "NFT mint", "post", (), 10000, 10005, 5, "fixture")
    launch = replace(research, author="elonmusk", tweet_id="2107623610483720464")
    order = []

    async def source(key):
        yield research
        yield launch

    async def consume():
        cfg = SimpleNamespace(accounts={"elonmusk": ()}, alpha=None)
        async for post in feed.stream(feed.FeedConfig("twitterapi_rule"), cfg, api_key="fixture"):
            order.append(("launch", post.author))

    monkeypatch.setattr(feed.x_stream, "stream", source)
    monkeypatch.setattr(sd, "watched_accounts", lambda **kw: {"opensea"})
    monkeypatch.setattr(sd, "offer", lambda post: order.append(("research", post.author)))
    asyncio.run(consume())
    assert order == [("research", "opensea"), ("research", "elonmusk"), ("launch", "elonmusk")]


def test_review_claim_is_exclusive_and_expired_lease_can_be_reclaimed(tmp_path):
    s = sh.Store(tmp_path / "claims.db")
    e = event("NFT mint")
    s.record(e, sh.classify(e, CFG, now_ms=10007), 10007)
    assert len(s.claim(8, 10007)) == 1
    assert s.claim(8, 10008) == []
    assert len(s.claim(8, 310007)) == 1
    s.query("UPDATE reviews SET reviewed_ms=1,status=?", ("verified",))
    assert s.claim(8, 620007) == []
