"""Prepared source-only arming policy; does not modify the production engine."""
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain, LaneMode
from kaiba.execution import official_post_snipe as op
from kaiba.execution import snipe
from kaiba.hunters.social_follow import Store

NOW=1791340000000
MINT="GmJQAASzk2odWAQCh7CG1wXSMP3mNX5ofBKVqKJPpump"
EVM="0x"+"12"*20


@pytest.fixture
def source(tmp_path):
    store=Store(tmp_path/"follow.db")
    cfg={"enabled":True,"live_official_posts":True,"db_path":str(store.path),"max_post_age_s":30,"verification_ttl_s":86400}
    store.query("INSERT INTO projects(id,handle,profile_json,first_ms,last_ms,status,checked_ms) VALUES ('123','project','{}',?,?,'verified',?)",(NOW,NOW,NOW-1000))
    store.query("INSERT INTO edges(source,target_id,first_ms,last_ms,active) VALUES ('toly','123',?,?,1)",(NOW,NOW))
    return store,cfg


def setup_case(db,source,chain):
    store,cfg=source
    token=MINT if chain is Chain.SOL else EVM
    venue={Chain.SOL:"pump.fun",Chain.BSC:"flap",Chain.ROBINHOOD:"pons"}[chain]
    db.execute("INSERT INTO tokens(chain,address,launchpad,first_seen_ms) VALUES (?,?,?,?)",(chain.value,token,venue,NOW))
    store.query("INSERT INTO announcements(tweet_id,project_id,handle,token,created_ms,received_ms,status,chain,venue,payload_json) VALUES ('777','123','project',?,?,?,'processing',?,?,'{}')",
                (token,NOW-500,NOW,chain.value,venue))
    store.query("UPDATE announcements SET issuer_account='project',issuer_checked_ms=?",(NOW,))
    verdict=snipe.Verdict(True,[],.75,"name_watchlist",{"source":"official_contract_post","official_post_id":"777","official_account_id":"123","official_account":"project"})
    from kaiba.ingest.launch_feed import Launch
    signal=snipe.build_signal(Launch(chain,token,venue,None,received_ms=NOW),verdict)
    return signal,cfg


@pytest.mark.parametrize("chain",[Chain.SOL,Chain.BSC,Chain.ROBINHOOD])
def test_verified_source_permission_requires_enabled_owner_arm_and_matching_receipts(tmp_db,source,chain):
    sig,cfg=setup_case(tmp_db,source,chain)
    assert op.official_post_permission(sig,tmp_db,config=cfg,now_ms=NOW)
    assert not op.official_post_permission(sig,tmp_db,config={**cfg,"live_official_posts":False},now_ms=NOW)
    assert not op.official_post_permission(sig,tmp_db,config=cfg,now_ms=NOW+31000)
    source[0].query("UPDATE edges SET active=0")
    assert not op.official_post_permission(sig,tmp_db,config=cfg,now_ms=NOW)


@pytest.mark.parametrize("change",["actor","chain","token","unverified","refused","venue","issuer"])
def test_forged_label_or_changed_proof_never_grants_permission(tmp_db,source,change):
    sig,cfg=setup_case(tmp_db,source,Chain.BSC)
    if change=="actor":
        sig=sig.model_copy(update={"payload":{**sig.payload,"official_account_id":"999"}})
    elif change=="chain":
        source[0].query("UPDATE announcements SET chain='robinhood'")
    elif change=="token":
        source[0].query("UPDATE announcements SET token='fake'")
    elif change=="unverified":
        source[0].query("UPDATE projects SET status='pending'")
    elif change=="refused":
        source[0].query("UPDATE announcements SET status='refused'")
    elif change=="venue":
        source[0].query("UPDATE announcements SET venue='fourmeme'")
    elif change=="issuer":
        source[0].query("UPDATE announcements SET issuer_account='a_promoter'")
    assert not op.official_post_permission(sig,tmp_db,config=cfg,now_ms=NOW)


def test_unverified_source_label_cannot_escape_current_engine_paper_gate(tmp_db):
    from kaiba.execution import engine
    from kaiba.ingest.launch_feed import Launch
    sig=snipe.build_signal(Launch(Chain.BSC,EVM,"flap",None),snipe.Verdict(True,[],.75,"watch",{"source":"official_contract_post"}))
    cfg=SimpleNamespace(lane=lambda lane:SimpleNamespace(params={"live_launchpads_by_chain":{"bsc":[]}}))
    assert engine._launchpad_refusal(sig,LaneMode.LIVE,cfg,tmp_db).startswith("launchpad_not_live:")


@pytest.mark.parametrize("chain",[Chain.SOL,Chain.BSC,Chain.ROBINHOOD])
def test_prepared_engine_patch_grants_only_the_bound_official_source(tmp_db,source,chain,monkeypatch):
    import sys
    import types
    from pathlib import Path

    from kaiba.execution import engine
    sig,cfg=setup_case(tmp_db,source,chain)
    risk=SimpleNamespace(lane=lambda lane:SimpleNamespace(params={"live_launchpads_by_chain":{chain.value:[]}}))
    assert engine._launchpad_refusal(sig,LaneMode.LIVE,risk,tmp_db).startswith("launchpad_not_live:")
    original=op.official_post_permission
    monkeypatch.setattr(op,"official_post_permission",lambda s,c:original(s,c,config=cfg,now_ms=NOW))
    text=Path(engine.__file__).read_text(encoding="utf-8")
    marker="    if signal.chain in PAPER_ONLY_LANE_CHAINS.get(signal.lane, frozenset()):"
    assert text.count(marker)==1
    hook=("    # Owner-requested official-post source; gated by persisted identity/CA proof.\n"
          "    if (signal.payload or {}).get('source') == 'official_contract_post':\n"
          "        from kaiba.execution.official_post_snipe import official_post_permission\n"
          "        if official_post_permission(signal, conn):\n"
          "            return None\n")
    candidate=types.ModuleType("kaiba.execution._official_arming_candidate")
    monkeypatch.setitem(sys.modules,candidate.__name__,candidate)
    exec(compile(text.replace(marker,hook+marker),"<prepared-only-official-arming>","exec"),candidate.__dict__)
    assert candidate._launchpad_refusal(sig,LaneMode.LIVE,risk,tmp_db) is None
    ordinary=sig.model_copy(update={"payload":{"source":"launch_snipe"}})
    assert candidate._launchpad_refusal(ordinary,LaneMode.LIVE,risk,tmp_db).startswith("launchpad_not_live:")
    source[0].query("UPDATE announcements SET status='refused'")
    assert candidate._launchpad_refusal(sig,LaneMode.LIVE,risk,tmp_db).startswith("launchpad_not_live:")
