import asyncio
from types import SimpleNamespace

from kaiba.ingest import follow_dispatch as fd
from kaiba.ingest import tweet_launch_feed as feed
from kaiba.ingest.x_stream import XPost


def post(uid="123"):
    return XPost(tweet_id="777",author="project",text="CA: 0x"+"12"*20,kind="post",media=(),
                 created_ms=1000,received_ms=1200,feed_delay_ms=None,backend="fixture",raw={"author":{"id":uid}})


def test_immutable_identity_required_before_local_send(monkeypatch):
    monkeypatch.setattr(fd,"projects",lambda:{"project":{"id":"123"}})
    sent=[]
    monkeypatch.setattr(fd,"send",lambda e:sent.append(e) or True)
    assert not fd.offer_post(post("999"))
    assert fd.offer_post(post())
    assert sent[0]["author_id"]=="123" and "quote" not in sent[0]


def test_project_only_posts_reach_sniper_tee_without_creator_launch(monkeypatch):
    config=SimpleNamespace(accounts={"elonmusk":()},alpha=None)
    monkeypatch.setattr(fd,"projects",lambda:{"project":{"id":"123"}})
    sent=[]
    monkeypatch.setattr(fd,"offer_post",lambda p:sent.append(p))
    monkeypatch.setattr(feed.social_dispatch,"watched_accounts",lambda **k:set())
    async def stream(*a,**k):
        yield post()
    monkeypatch.setattr(feed.x_stream,"stream",stream)
    async def collect():
        return [p async for p in feed.stream(feed.FeedConfig("twitterapi_rule"),config,api_key="fixture")]
    assert asyncio.run(collect())==[] and len(sent)==1
