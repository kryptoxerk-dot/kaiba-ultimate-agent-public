"""Shared spending ceiling, no blind mutation retries, owned-rule isolation and SSRF."""
from types import SimpleNamespace

import httpx
import pytest

from kaiba.providers import social_follow as p


def reader(tmp_path):
    reservations=[]
    return SimpleNamespace(DEFAULT_ROOT=tmp_path,credential=lambda:"fixture-secret",reserve=lambda path,n:reservations.append((path,n)),reservations=reservations)


def test_follow_page_reserves_before_request_and_error_spend_is_not_refunded(tmp_path):
    budget=reader(tmp_path)
    calls=[]
    def send(r):
        assert budget.reservations[0][1]==60
        calls.append(r)
        return httpx.Response(429,json={"message":"fixture-secret"})
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        with pytest.raises(p.Unavailable,match="following_http_429") as exc:
            p.followings("toly",client=client,budget=budget)
    assert len(calls)==1 and len(budget.reservations)==1 and "fixture-secret" not in str(exc.value)


def test_budget_exhaustion_prevents_provider_request(tmp_path):
    b=reader(tmp_path)
    def refuse(*a):
        raise RuntimeError("daily_budget_exhausted")
    b.reserve=refuse
    with httpx.Client(transport=httpx.MockTransport(lambda r:pytest.fail("spent after exhaustion"))) as client:
        with pytest.raises(p.Unavailable):
            p.followings("toly",client=client,budget=b)


def test_actual_legacy_following_profile_retains_tco_website():
    real_shape={"id":"123","userName":"project","screen_name":"project","url":"https://t.co/example","protected":False,"description":"Project"}
    assert p.normalize_user(real_shape)["website"]=="https://t.co/example"
    assert not p.normalize_user({**real_shape,"url":"https://twitter.com/project"})["website"]


def test_rule_updates_touch_only_owned_tag_and_cache_unchanged_roster(tmp_path):
    b=reader(tmp_path)
    rules=[{"rule_id":"lead","tag":"lead-rule","value":"from:elonmusk","is_effect":1,"interval_seconds":5}]
    mutations=[]
    def send(r):
        import json
        body=json.loads(r.content) if r.content else {}
        if r.url.path.endswith("get_rules"):
            return httpx.Response(200,json={"rules":rules})
        mutations.append(body)
        if r.url.path.endswith("add_rule"):
            rules.append({**body,"rule_id":"owned","is_effect":0})
            return httpx.Response(200,json={"rule_id":"owned"})
        rules[-1].update(body)
        return httpx.Response(200,json={"status":"success"})
    cache=tmp_path/"rules.json"
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        assert p.sync_rules(["project"],client=client,budget=b,cache=cache)["status"]=="verified"
        n=len(b.reservations)
        assert p.sync_rules(["project"],client=client,budget=b,cache=cache)["status"]=="cached_verified"
    assert len(b.reservations)==n and rules[0]["tag"]=="lead-rule"
    assert all(m["tag"].startswith("kaiba-official-projects-v1-") for m in mutations)


@pytest.mark.parametrize("url",["http://example.com","https://127.0.0.1/","https://u:p@example.com/","https://example.com:444/"])
def test_untrusted_profile_cannot_read_private_or_non_https_url(url):
    with pytest.raises(p.Unavailable):
        p.public_https(url)
