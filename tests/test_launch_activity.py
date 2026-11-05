"""Actual own-launch provenance chooses the venue; failed reads are not activity."""
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import evm_price as ep
from kaiba.execution import launch_activity as la
from kaiba.execution import tweet_launch as tl

TOKEN="0x"+"12"*20


def own(c,dex):
    c.execute("INSERT INTO tweet_launches(launch_id,tweet_id,author,chain,dex,mode,verdict,decided_ms,token,state) "
              "VALUES ('tl:fixture','fixture','flapdotsh','bsc',?,'live','launch',1,?,'confirmed')",(dex,TOKEN))


def rpc(monkeypatch):
    calls=[]
    def factory(chain,**kw):
        calls.append((chain,kw['priority'].name))
        return "fixture-rpc"
    monkeypatch.setattr(ep,"json_rpc_batch",factory)
    return calls


def test_fourmeme_uses_own_portal_and_exit_priority(tmp_db,monkeypatch):
    own(tmp_db,"fourmeme")
    calls=rpc(monkeypatch)
    monkeypatch.setattr(ep,"read_flap",lambda *a:pytest.fail("Four.meme sent to Flap portal"))
    curve=ep.FourMemeCurve(1,2,3,4,100,5)
    monkeypatch.setattr(ep,"read_bsc_non_flap",lambda token,reader:(ep.NonFlapVenue("fourmeme_curve","verified",curve),"ok"))
    assert tl.curve_fingerprint(Chain.BSC,TOKEN,tmp_db)==((2,3,5),"verified")
    assert calls==[(Chain.BSC,"EXIT")]


def test_flap_still_uses_flap_portal(tmp_db,monkeypatch):
    own(tmp_db,"flap")
    rpc(monkeypatch)
    monkeypatch.setattr(ep,"read_bsc_non_flap",lambda *a:pytest.fail("Flap sent to Four.meme"))
    monkeypatch.setattr(ep,"read_flap",lambda *a:(SimpleNamespace(curve=SimpleNamespace(tokens_sold_atoms=7),quote_reserve_base=8),"ok"))
    assert tl.curve_fingerprint(Chain.BSC,TOKEN,tmp_db)==((7,8),"ok")


def test_missing_or_shadow_venue_does_not_spend_rpc(tmp_db,monkeypatch):
    monkeypatch.setattr(ep,"json_rpc_batch",lambda *a,**k:pytest.fail("guessed an own venue"))
    assert la.bsc_fingerprint(TOKEN,tmp_db)==(None,"own_bsc_venue_unavailable")
    own(tmp_db,"fourmeme")
    tmp_db.execute("UPDATE tweet_launches SET mode='shadow'")
    assert la.bsc_fingerprint(TOKEN,tmp_db)==(None,"own_bsc_venue_unavailable")


def test_fourmeme_movement_failed_reads_and_graduation_at_actual_activity_writer(tmp_db,monkeypatch):
    own(tmp_db,"fourmeme")
    rpc(monkeypatch)
    monkeypatch.setattr(tl,"fetch_all",lambda c,sql,args=():[{"chain":"bsc","token":TOKEN}] if "JOIN positions" in sql else [])
    def venue(funds):
        return ep.NonFlapVenue("fourmeme_curve","verified",ep.FourMemeCurve(1,2,funds,4,100,5))
    rows=iter([(venue(1),"ok"),(venue(1),"ok"),(None,"rpc_down"),(venue(2),"ok"),
               (ep.NonFlapVenue("fourmeme_graduated","fourmeme:graduated"),"ok")])
    monkeypatch.setattr(ep,"read_bsc_non_flap",lambda *a:next(rows))
    seen={}
    got=[]
    for now in [1000,2000,3000,4000,5000]:
        tl.watch_activity(tmp_db,seen,now_ms=now)
        row=tmp_db.execute("SELECT value FROM kv WHERE key=?",("token_activity:bsc:"+TOKEN,)).fetchone()
        got.append(None if row is None else int(row[0]))
    assert got==[None,None,None,4000,5000]
