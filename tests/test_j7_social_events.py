from kaiba.ingest.j7_social_events import SocialEvents


def post(tid="2107623610483720463", **kw):
    return {
        "id": tid,
        "type": "QUOTE",
        "author": {"handle": "new_j7_author"},
        "text": "Mint open",
        "createdAt": 10000,
        "quotedTweet": {"handle": "collection", "id": "123456", "text": "NFT mint whitelist"},
        **kw,
    }


def test_all_delivered_accounts_reach_research_and_context_updates_keep_first_receipt():
    seen = []
    s = SocialEvents(sink=lambda p: seen.append(p) or True)
    assert s.ingest("tweet", post(), now_ms=10001)
    assert seen[0].raw["quotedTweet"]["handle"] == "collection"
    assert not s.ingest("tweet_update", post(), now_ms=10002)
    assert s.ingest(
        "tweet_update", {"id": seen[0].tweet_id, "quotedTweet": {"text": "NFT mint now closed"}}, now_ms=10003
    )
    assert len(seen) == 2 and seen[1].received_ms == 10001


def test_backlog_deletion_and_author_reassignment_never_become_a_new_opportunity():
    seen = []
    s = SocialEvents(sink=lambda p: seen.append(p) or True)
    s.ingest("initialTweets", [post()], now_ms=10001)
    assert not s.ingest("tweet_update", post(), now_ms=10002)
    other = post("2107623610483720464")
    assert s.ingest("tweet", other, now_ms=10003)
    assert not s.ingest("tweet_update", {**other, "author": {"handle": "different"}}, now_ms=10004)
    assert not s.ingest("tweet", other, now_ms=10005)


def test_research_lru_does_not_saturate_the_financial_bridge():
    seen = []
    s = SocialEvents(capacity=2, sink=lambda p: seen.append(p) or True)
    for i in range(10):
        assert s.ingest("tweet", post(str(2107623610483720463 + i)), now_ms=10001 + i)
    assert len(s.entries) == 2 and len(seen) == 10
