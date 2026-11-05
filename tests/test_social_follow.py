"""Follow baseline, immutable identity, provenance and atomic announcement intake."""

import pytest

from kaiba.hunters.social_follow import Store, poll_once
from kaiba.providers.social_follow import has_backlink, normalize_user

NOW = 1791340000000
MINT = "GmJQAASzk2odWAQCh7CG1wXSMP3mNX5ofBKVqKJPpump"


def user(uid="123",handle="project",website="https://project.example"):
    return {"id":uid,"userName":handle,"name":"Project","profile_bio":{"entities":{"url":{"urls":[{"expanded_url":website}]}}}}


def config(tmp_path):
    return {"verification_ttl_s":86400,"max_projects":20,"watch_path":str(tmp_path/"watch.json"),"max_post_age_s":30}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path/"follows.db")


def verified(store):
    store.edge("cz_binance",normalize_user(user()),NOW,baseline=True)
    out = store.verify_one(now=NOW+1,fetch=lambda url:(url,'<a href="https://x.com/project">X</a>'))
    assert out["status"]=="verified"


def event(**over):
    return {"tweet_id":"777","author":"project","author_id":"123","text":"Our token is live. CA: "+MINT,
            "created_ms":NOW+100,"received_ms":NOW+200,"kind":"post",**over}


def test_baseline_partial_pages_do_not_invent_new_follows_or_unfollows(store):
    first = store.snapshot("cz_binance",{"status":"success","followings":[user(),user("124","two")],"has_next_page":True},NOW)
    assert first["baseline"] and not first["complete_following_list"] and not first["exact_follow_time"]
    second = store.snapshot("cz_binance",{"status":"success","followings":[user("125","three")],"has_next_page":True},NOW+10)
    assert not second["baseline"]
    edges = store.query("SELECT * FROM edges ORDER BY target_id")
    assert len(edges)==3 and all(e["active"]==1 for e in edges)
    assert edges[0]["baseline"]==1 and edges[2]["baseline"]==0
    store.snapshot("cz_binance",{"status":"success","followings":[user()],"has_next_page":True},NOW+20)
    assert store.query("SELECT first_ms FROM edges WHERE target_id='123'")[0]["first_ms"]==NOW


def test_website_backlink_exact_handle_and_real_anchor_only():
    assert has_backlink('<a href="https://x.com/Project/">X</a>',"project")
    for html in ['https://x.com/project','<a href="https://x.com/project_fake">X</a>',
                 '<a href="https://x.com.evil.com/project">X</a>','<a href="https://x.com/project/status/1">X</a>']:
        assert not has_backlink(html,"project")


def test_renaming_verified_account_revokes_watch(store,tmp_path):
    verified(store)
    assert store.publish_watch(config(tmp_path),NOW+5)[0]["id"]=="123"
    store.edge("cz_binance",normalize_user(user(handle="renamed")),NOW+10)
    assert store.publish_watch(config(tmp_path),NOW+15)==[]
    assert store.announcement(event(),config(tmp_path),NOW+300) is None


def test_only_exact_id_fresh_own_contract_and_active_follow_can_queue(store,tmp_path):
    verified(store)
    cfg = config(tmp_path)
    assert store.announcement(event(author_id="999"),cfg,NOW+300) is None
    assert store.announcement(event(kind="repost"),cfg,NOW+300) is None
    assert store.query("SELECT status FROM announcements")[0]["status"]=="refused"
    row = store.announcement(event(tweet_id="778"),cfg,NOW+300)[0]
    assert row["token"]==MINT and store.claim("778")
    assert not store.claim("778")
    assert store.announcement(event(tweet_id="779"),cfg,NOW+300)==[]
    store.query("UPDATE edges SET active=0")
    assert store.announcement(event(tweet_id="780"),cfg,NOW+300) is None


@pytest.mark.parametrize("changes,note", [({"created_ms":NOW-60000},"stale_or_missing_post_time"),
    ({"text":"CA: "+MINT+" 0x"+"12"*20},"no_unique_exact_contract"),
    ({"text":"Fake scam contract: "+MINT},"not_positive_project_contract_announcement"),
    ({"text":"I own a wallet: "+MINT},"not_explicit_contract_announcement")])
def test_non_contract_assertions_record_refusal(store,tmp_path,changes,note):
    verified(store)
    assert store.announcement(event(**changes),config(tmp_path),NOW+300) is None
    assert store.query("SELECT note FROM announcements")[0]["note"]==note


def test_j7_follow_unfollow_filter_actor_and_track_target_id(store):
    payload={"user":{"id":"100","handle":"cz_binance"},"following":{"id":"123","handle":"project","profile":{"url":{"url":"https://project.example"}}}}
    assert not store.j7("following_update",payload,NOW,{"other"})
    assert store.j7("following_update",payload,NOW,{"cz_binance"})
    payload["unfollowing"]=payload["following"]
    assert store.j7("unfollowing_update",payload,NOW+1,{"cz_binance"})
    assert store.query("SELECT active,source_kind FROM edges")[0]=={"active":0,"source_kind":"j7_follow_event"}


def test_poll_priority_then_rotate_and_never_use_failed_response_as_empty(store):
    cfg={"source_priority":["z"]}
    calls=[]
    def fetch(handle,**kw):
        calls.append((handle,kw))
        return {"status":"success","followings":[],"has_next_page":False}
    poll_once(cfg,store,fetch=fetch,watched={"a","z"})
    poll_once(cfg,store,fetch=fetch,watched={"a","z"})
    assert [h for h,_ in calls]==["z","a"] and calls[0][1]=={"page_size":20}
    with pytest.raises(ValueError):
        store.snapshot("bad",{"status":"error","followings":[]},NOW)
    assert not store.query("SELECT * FROM polls WHERE source='bad'")


def test_unavailable_source_rotates_without_poisoning_first_successful_baseline(store):
    from kaiba.providers.social_follow import Unavailable
    calls=[]
    def failed(handle,**kwargs):
        calls.append(handle)
        raise Unavailable("following_http_404")
    with pytest.raises(Unavailable):
        poll_once({"source_priority":["z"]},store,fetch=failed,watched={"z","a"})
    with pytest.raises(Unavailable):
        poll_once({},store,fetch=failed,watched={"z","a"})
    assert calls==["z","a"]
    receipt=store.snapshot("z",{"status":"success","followings":[user()]},NOW)
    assert receipt["baseline"]


def test_profile_changed_during_site_fetch_cannot_be_certified(store):
    store.edge("cz_binance",normalize_user(user()),NOW)
    def fetch(url):
        store.edge("cz_binance",normalize_user(user(website="https://changed.example")),NOW+1)
        return url,'<a href="https://x.com/project">X</a>'
    store.verify_one(now=NOW+2,fetch=fetch)
    assert store.query("SELECT status FROM projects")[0]["status"]=="pending"
