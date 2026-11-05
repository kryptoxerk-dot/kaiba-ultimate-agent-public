"""Existing snipe vetoes, exact chain identity and closed handoff connections."""
from types import SimpleNamespace

import pytest

from kaiba.execution import official_post_snipe as op
from kaiba.execution import snipe

NOW=1791340000000
MINT="GmJQAASzk2odWAQCh7CG1wXSMP3mNX5ofBKVqKJPpump"
EVM="0x"+"12"*20


class Borrowed:
    def __init__(self,c):
        self.c=c
        self.closed=False

    def __getattr__(self,key):
        return getattr(self.c,key)

    def close(self):
        self.closed=True


def event():
    return {"tweet_id":"777","author_id":"123","author":"project","created_ms":NOW-500,"received_ms":NOW}


def seed(c,chain="sol",token=MINT):
    c.execute("INSERT INTO tokens(chain,address,name,symbol,launchpad,created_ms,first_seen_ms) VALUES (?,?,?,?,?,?,?)",
              (chain,token,"Project","PROJ",{"sol":"pump.fun","robinhood":"pons","bsc":"flap"}[chain],NOW-1000,NOW))


def params(chains=None,cap=5):
    return {**snipe.DEFAULT_PARAMS,"chains":chains or ["sol"],"venues":{"sol":["pump.fun"],"bsc":["flap"],"robinhood":["pons"]},
            "max_snipes_per_day":cap,"name_watchlist":[],"watch_strength":.75}


def test_real_evaluation_preserves_daily_cap_and_positive_control(tmp_db):
    seed(tmp_db)
    c=Borrowed(tmp_db)
    calls=[]
    def deliver(c,launch,v,p,b):
        calls.append(v)
        return "fixture-signal","guarded-engine"
    refused=op.handoff(event(),MINT,enabled=True,connect=lambda:c,params=params(cap=0),deliver=deliver,now_ms=NOW)
    assert "daily_snipe_cap" in refused and calls==[] and c.closed
    result=op.handoff(event(),MINT,enabled=True,connect=lambda:c,params=params(),deliver=deliver,now_ms=NOW,
                      identity_info=lambda *a,**k:SimpleNamespace(ok=True,data={"link":{"twitter_username":"project"}}))
    assert result.startswith("signal:") and calls[0].features["official_account_id"]=="123"
    assert calls[0].strength==.75 and tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]==0


def test_unsupported_chain_gate_and_cross_chain_address_cannot_send(tmp_db):
    seed(tmp_db,"bsc",EVM)
    c=Borrowed(tmp_db)
    assert "chain_not_enabled:bsc" in op.handoff(event(),EVM,enabled=True,connect=lambda:c,params=params(),
        prepare=lambda *a:{},deliver=lambda *a:pytest.fail("bypassed chain gate"),now_ms=NOW)
    seed(tmp_db,"robinhood",EVM)
    assert op.handoff(event(),EVM,enabled=True,connect=lambda:c,params=params(),now_ms=NOW)=="ambiguous_cross_chain_contract"


def test_robinhood_unread_book_veto_remains(tmp_db):
    seed(tmp_db,"robinhood",EVM)
    assert "early_book_unread" in op.handoff(event(),EVM,enabled=True,connect=lambda:Borrowed(tmp_db),
        params=params(["robinhood"]),prepare=lambda *a:{},deliver=lambda *a:pytest.fail("unread tax/book sent"),now_ms=NOW)


def test_stale_and_disabled_handoffs_never_open_live_database():
    assert op.handoff(event(),MINT,enabled=False,connect=lambda:pytest.fail("disabled"),now_ms=NOW)=="handoff_disabled"
    assert op.handoff(event(),MINT,enabled=True,connect=lambda:pytest.fail("stale"),now_ms=NOW+60000)=="stale_at_handoff"


@pytest.mark.parametrize("value,match",[("project",True),("https://x.com/project",True),
    ("@project",True),("other",False),("project/status/123",False),
    ("https://x.com/project/status/123",False),("https://x.com.evil.com/project",False),(None,None)])
def test_token_issuer_profile_is_distinct_from_promoter_or_narrative_tweet(value,match):
    calls=[]
    def info(token,chain,priority=None):
        calls.append(priority.name)
        return SimpleNamespace(ok=True,data={"link":{"twitter_username":value}})
    from kaiba.core.schemas import Chain
    assert op.token_official_profile(Chain.SOL,MINT,"project",info=info)==match
    assert calls==["RESEARCH"]


def test_followed_traders_promotion_does_not_become_official_token_signal(tmp_db):
    seed(tmp_db)
    result=op.handoff(event(),MINT,enabled=True,connect=lambda:Borrowed(tmp_db),params=params(),now_ms=NOW,
        identity_info=lambda *a,**k:SimpleNamespace(ok=True,data={"link":{"twitter_username":"real_issuer"}}),
        deliver=lambda *a:pytest.fail("bought a promoter's token"))
    assert result=="token_official_account_unverified"


def test_paper_entries_do_not_spend_live_cap_but_live_canary_reservations_do(tmp_db):
    from kaiba.core.schemas import Chain
    for i,(mode,chain,token,ts) in enumerate([
        ("shadow","sol","paper",NOW),("live","sol","real",NOW),
        ("canary","sol","real",NOW),("canary","sol","canary",NOW),
        ("live","bsc","other_chain",NOW),("live","sol","yesterday",NOW-86400000)]):
        tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action) VALUES(?,?,?,?,?,?,?)",
                       (str(i),ts,"launch-snipe",mode,chain,token,"enter"))
    assert op.live_snipes_today(tmp_db,Chain.SOL,NOW)==2
    seed(tmp_db)
    result=op.handoff(event(),MINT,enabled=True,connect=lambda:Borrowed(tmp_db),params=params(cap=2),now_ms=NOW,
                      deliver=lambda *a:pytest.fail("live daily cap bypassed"))
    assert "daily_snipe_cap" in result


def test_regular_paper_signal_does_not_suppress_first_official_signal_or_allow_repeat(tmp_db,monkeypatch):
    from kaiba.execution import lanes
    seed(tmp_db)
    launch=op.launch_from_row(dict(tmp_db.execute("SELECT * FROM tokens WHERE address=?",(MINT,)).fetchone()),NOW)
    v=snipe.evaluate(launch,{**params(),"name_watchlist":["PROJ"]},snipe.record_for(tmp_db,launch.chain,launch.creator),snipes_today=0)
    original=snipe.build_signal(launch,v)
    assert lanes.record(original,tmp_db)
    def hand(c,launch,verdict,p,budget,record_signal):
        sig=snipe.build_signal(launch,verdict)
        return (sig.signal_id if record_signal(sig,c) else None),"fixture"
    monkeypatch.setattr(snipe,"hand_to_engine",hand)
    monkeypatch.setattr(op.time,"time",lambda:NOW/1000)
    kw=dict(enabled=True,connect=lambda:Borrowed(tmp_db),params=params(),now_ms=NOW,
            identity_info=lambda *a,**k:SimpleNamespace(ok=True,data={"link":{"twitter_username":"project"}}))
    result=op.handoff(event(),MINT,**kw)
    assert result.startswith("signal:sig_official_")
    assert op.handoff(event(),MINT,**kw)=="snipe_handoff_refused:fixture"
    rows=tmp_db.execute("SELECT signal_id,payload_json FROM signals WHERE token=?",(MINT,)).fetchall()
    assert len(rows)==2 and original.signal_id in [r["signal_id"] for r in rows]
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]==0
