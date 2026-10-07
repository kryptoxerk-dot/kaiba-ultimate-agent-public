"""Production tweet-launch integration with isolated databases and provider fixtures."""
import contextlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core import db
from kaiba.core.config import LaneConfig, get_settings, load_risk
from kaiba.core.schemas import Chain, Lane, LaneMode
from kaiba.execution import executor as ex
from kaiba.execution import tweet_launch as tl
from kaiba.execution.risk import RiskGate
from kaiba.ingest.x_stream import XPost

NOW = 1_791_300_000_000


def cfg(**over):
    return tl.Config(**{**dict(mode="live", armed_by="fixture", namer_enabled=False,
                               logo_generate=False, per_author_cooldown_s=0,
                               accounts={"elonmusk": (Chain.SOL,)},
                               chains={Chain.SOL: tl.ChainRoute(Chain.SOL, "pump", Decimal(10), live=True)}),
                        **over})


def post(tid="2045879341243043889", age=500, received=None):
    return XPost(tweet_id=tid, author="elonmusk", text="Kekius", kind="post",
                 media=("https://pbs.twimg.com/media/x.jpg",), created_ms=NOW-age,
                 received_ms=NOW if received is None else received, feed_delay_ms=100, backend="fixture")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_DB_PATH", str(tmp_path / "isolated.db"))
    monkeypatch.setenv("KAIBA_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    c = db.connect(tmp_path / "isolated.db")
    db.migrate(c)
    yield c
    c.close()
    get_settings.cache_clear()


@pytest.fixture
def venue(monkeypatch):
    risk = load_risk()
    risk = risk.model_copy(update={
        "global_mode": LaneMode.LIVE, "kill_switch": False, "entries_paused": False, "reduce_only": False,
        "bounds": risk.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
        "lanes": {**risk.lanes, Lane.TWEET_LAUNCH: LaneConfig(mode=LaneMode.LIVE, chains=[Chain.SOL])},
    })
    monkeypatch.setattr(tl, "get_risk", lambda: risk)
    monkeypatch.setattr(ex, "get_risk", lambda: risk)
    monkeypatch.setattr(tl.time, "time", lambda: NOW/1000)
    monkeypatch.setattr(tl.time, "sleep", lambda s: None)
    monkeypatch.setattr(ex, "_guarded_patiently", lambda *a, **k: contextlib.nullcontext())
    # lead 2026-10-07: the pump.fun pre-flight reads the chain; tests never touch the network.
    from kaiba.execution import launch_preflight as lp
    monkeypatch.setattr(lp, "read_pump_global", lambda conn=None: (None, "fixture"))
    calls = []
    transitions = []
    def run(args, **kw):
        calls.append(args)
        if args[:2] == ["cooking", "create"]:
            return {"data": {"order_id": "fixture_order", "status": "pending"}}
        return {"data": {"status": "confirmed", "report": {"output_token": "FixtureMint"}}}
    monkeypatch.setattr(ex, "_run_gmgn", run)
    monkeypatch.setattr(ex, "_transition", lambda *a, **k: transitions.append(a))
    return calls, transitions


def admit(monkeypatch, allowed=True):
    seen = []
    def gate(self, chain, lane, amount, conn, *, token):
        seen.append((chain, lane, amount, conn.in_transaction, token))
        return SimpleNamespace(allowed=allowed, reason="fixture_admission")
    monkeypatch.setattr(RiskGate, "check_entry", gate)
    return seen


def planned(conn, c=None, p=None):
    [plan] = tl.handle_post(conn, p or post(), c or cfg())
    return plan


def state(conn, plan):
    return conn.execute("SELECT state,error FROM tweet_launches WHERE launch_id=?", (plan.launch_id,)).fetchone()


def test_decision_time_age_includes_queue_and_model_latency():
    old = post(age=31000, received=NOW-30500)
    [p] = tl.plan_post(old, cfg(), now_ms=NOW)
    assert p.verdict == "skip" and "too_late:31000ms" in p.reasons
    [fresh] = tl.plan_post(post(), cfg(), now_ms=NOW)
    assert fresh.verdict == "launch"


@pytest.mark.parametrize("created", [None, NOW+1000])
def test_missing_or_future_timestamp_refuses(created):
    [p] = tl.plan_post(replace(post(), created_ms=created), cfg(), now_ms=NOW)
    assert p.verdict == "skip" and "invalid_publication_time" in p.reasons


@pytest.mark.parametrize("ch,dex", [(Chain.SOL,"pump"),(Chain.BSC,"flap"),(Chain.ROBINHOOD,"pons")])
def test_never_undersizes_five_percent_and_rounds_up(ch,dex):
    from kaiba.execution.tweet_launch_policy import quote_five_percent
    route = tl.ChainRoute(ch, dex, Decimal(10))
    amount, share, basis = tl.dev_buy_native(route, 5)
    assert amount is not None and share == 5 and "capped" not in basis
    if ch is Chain.SOL:
        quote = quote_five_percent(total_supply_atoms=10**15, virtual_token_atoms=1073*10**12,
                                  virtual_native_atoms=30*10**9, fee_bps=125, native_decimals=9)
        assert amount == quote.native_amount
    small = replace(route, max_buy_native=amount-Decimal("0.00001"))
    assert tl.dev_buy_native(small,5) == (None,None,"five_percent_exceeds_route_cap")


# Lead 2026-10-07: the launcher's admission is ``tweet_launch._admission`` -- every entry brake
# EXCEPT the flat per-trade size cap, which would refuse every owner-authorized 5% dev buy
# (1.485 SOL vs a 0.1 SOL cap on the box). These tests pin that contract.

def test_full_gate_denies_before_provider_call(conn, venue, monkeypatch):
    calls, _ = venue
    seen = []

    def deny(c, plan, cfg_, lane):
        seen.append((plan.chain, lane, c.in_transaction))
        return "risk:fixture_denied"
    monkeypatch.setattr(tl, "_admission", deny)
    p = planned(conn)
    tl.send(conn,p,cfg())
    assert calls == [] and tuple(state(conn,p)) == ("failed", "risk:fixture_denied")
    assert seen == [(Chain.SOL, Lane.TWEET_LAUNCH, False)]      # resolved outside any transaction


def test_the_flat_per_trade_cap_does_not_block_the_authorized_five_percent(conn, venue, monkeypatch):
    calls, _ = venue
    risk = tl.get_risk()
    tiny = risk.model_copy(update={"chains": {**risk.chains, Chain.SOL: risk.chain_budget(Chain.SOL).model_copy(
        update={"max_position_base_units": 100_000_000})}})            # 0.1 SOL, the box's flat cap
    monkeypatch.setattr(tl, "get_risk", lambda: tiny)
    monkeypatch.setattr(ex, "get_risk", lambda: tiny)
    p = planned(conn)
    tl.send(conn,p,cfg())
    assert [a[:2] for a in calls][0] == ["cooking","create"] and state(conn,p)[0] == "confirmed"


def test_the_launch_lanes_own_caps_still_bind(conn, venue):
    calls, _ = venue
    c = cfg(daily_native_cap={Chain.SOL: Decimal("1.0")})                 # below one 5% buy
    p = planned(conn, c)
    tl.send(conn,p,c)
    assert calls == [] and state(conn,p)[1].startswith("daily_native_cap:sol")


def test_positive_admission_creates_once_and_submits_reconciliation(conn,venue,monkeypatch):
    calls, transitions = venue
    admit(monkeypatch)
    p = planned(conn)
    tl.send(conn,p,cfg())
    tl.send(conn,p,cfg())
    assert [a[:2] for a in calls] == [["cooking","create"],["order","get"]]
    assert len(transitions)==1 and state(conn,p)[0]=="confirmed"
    assert not conn.in_transaction


def test_send_rechecks_age_after_plan(conn,venue,monkeypatch):
    calls, _ = venue
    admit(monkeypatch)
    p = planned(conn)
    monkeypatch.setattr(tl.time,"time",lambda:(NOW+25000)/1000)
    tl.send(conn,p,cfg())
    assert calls==[] and state(conn,p)[1]=="invalid_or_stale_at_send"


@pytest.mark.parametrize("prior", ["submitting","submitted","ambiguous","confirmed"])
def test_unbooked_spend_blocks_next_launch_even_from_previous_day(conn,venue,monkeypatch,prior):
    calls, _ = venue
    admit(monkeypatch)
    first=planned(conn)
    conn.execute("UPDATE tweet_launches SET state=?,decided_ms=?,token='PriorMint' WHERE launch_id=?",
                 (prior,NOW-86400001,first.launch_id))
    second=planned(conn,p=post("2045879341243043890"))
    tl.send(conn,second,cfg())
    assert calls==[] and state(conn,second)[1]=="launch_exposure_unresolved"


def test_submitting_counts_against_daily_cap(conn,venue,monkeypatch):
    calls, _ = venue
    admit(monkeypatch)
    c=cfg(daily_launch_cap=1)
    first=planned(conn,c)
    conn.execute("UPDATE tweet_launches SET state='submitting' WHERE launch_id=?",(first.launch_id,))
    second=planned(conn,c,post("2045879341243043890"))
    tl.send(conn,second,c)
    assert calls==[] and state(conn,second)[1].startswith("daily_launch_cap")


@pytest.mark.parametrize("status", ["failed","expired","cancelled","canceled","rejected"])
def test_failed_status_with_token_never_confirms(conn,venue,monkeypatch,status):
    _, transitions=venue
    def query(args,**kw):
        return {"data":{"status":status,"report":{"output_token":"FailureMint"}}}
    monkeypatch.setattr(ex,"_run_gmgn",query)
    p=planned(conn)
    assert not tl._await_token(conn,p.launch_id,Chain.SOL,"o",1,Lane.TWEET_LAUNCH,cfg(),0,1)
    assert not transitions and state(conn,p)[0]=="failed"


def test_token_on_pending_report_waits_for_success(conn,venue,monkeypatch):
    _, transitions=venue
    statuses=iter(["pending","confirmed"])
    queried=[]
    def query(args,**kw):
        status=next(statuses)
        queried.append(status)
        return {"data":{"status":status,"report":{"output_token":"NewMint"}}}
    monkeypatch.setattr(ex,"_run_gmgn",query)
    p=planned(conn)
    assert tl._await_token(conn,p.launch_id,Chain.SOL,"o",1,Lane.TWEET_LAUNCH,cfg(),0,1)
    assert queried==["pending","confirmed"] and len(transitions)==1


def test_ownership_change_during_admission_cannot_erase_inflight_state(conn,venue,monkeypatch):
    calls,_=venue
    p=planned(conn)
    def gate(*a,**kw):
        conn.execute("UPDATE tweet_launches SET state='ambiguous' WHERE launch_id=?",(p.launch_id,))
        return "risk:fixture_denied"
    monkeypatch.setattr(tl,"_admission",gate)
    tl.send(conn,p,cfg())
    assert calls==[] and state(conn,p)[0]=="ambiguous"


def test_limiter_delay_cannot_send_stale_post(conn,venue,monkeypatch):
    calls,_=venue
    admit(monkeypatch)
    @contextlib.contextmanager
    def delayed(*a,**kw):
        monkeypatch.setattr(tl.time,"time",lambda:(NOW+25000)/1000)
        yield
    monkeypatch.setattr(ex,"_guarded_patiently",delayed)
    p=planned(conn)
    tl.send(conn,p,cfg())
    assert calls==[] and state(conn,p)[1]=="invalid_or_stale_at_broadcast"


def test_non_five_percent_persisted_plan_is_refused(conn,venue,monkeypatch):
    calls,_=venue
    admit(monkeypatch)
    p=planned(conn)
    p.supply_pct=3
    tl.send(conn,p,cfg())
    assert calls==[] and state(conn,p)[1]=="five_percent_target_required"


def test_two_connections_cannot_reserve_two_unbooked_launches(conn,venue,monkeypatch):
    import concurrent.futures
    import threading
    calls,_=venue
    barrier=threading.Barrier(2)
    counts=threading.local()
    def gate(*a,**kw):
        n=getattr(counts,"n",0)
        counts.n=n+1
        if n==0:
            barrier.wait(timeout=5)
        return SimpleNamespace(allowed=True,reason="fixture_admitted")
    monkeypatch.setattr(RiskGate,"check_entry",gate)
    p1=planned(conn)
    p2=planned(conn,p=post("2045879341243043890"))
    db_path=conn.execute("PRAGMA database_list").fetchone()[2]
    def send(p):
        c=db.connect(Path(db_path))
        try:
            tl.send(c,p,cfg())
        finally:
            c.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=[pool.submit(send,p) for p in (p1,p2)]
        for result in results:
            result.result(timeout=10)
    assert sum(a[:2]==["cooking","create"] for a in calls)==1
    assert sorted([state(conn,p1)[0],state(conn,p2)[0]])==["confirmed","failed"]


def test_pending_other_chain_does_not_block_this_chains_separate_budget(conn,venue,monkeypatch):
    calls,_=venue
    admit(monkeypatch)
    first=planned(conn)
    conn.execute("UPDATE tweet_launches SET chain='bsc',state='ambiguous' WHERE launch_id=?",
                 (first.launch_id,))
    second=planned(conn,p=post("2045879341243043890"))
    tl.send(conn,second,cfg())
    assert state(conn,second)[0]=="confirmed"
    assert sum(a[:2]==["cooking","create"] for a in calls)==1


def test_author_cooldown_applies_to_new_posts_and_allows_same_post_routes(conn,venue):
    c=cfg(per_author_cooldown_s=900)
    first=planned(conn,c)
    conn.execute("UPDATE tweet_launches SET state='confirmed' WHERE launch_id=?",(first.launch_id,))
    other_route=replace(first,chain=Chain.BSC,dex="flap")
    assert tl.spend_check(conn,other_route,c,NOW) is None
    next_post=replace(first,tweet_id="2045879341243043890")
    assert tl.spend_check(conn,next_post,c,NOW)=="author_cooldown:elonmusk"
    assert tl.spend_check(conn,next_post,replace(c,per_author_cooldown_s=0),NOW) is None


@pytest.mark.parametrize("backend", ["j7","twitterapi_rule"])
def test_real_candidate_runner_uses_selected_feed_and_records_all_three_chains(conn,venue,monkeypatch,backend):
    import asyncio
    import threading

    from kaiba.ingest import tweet_launch_feed, x_stream
    calls,_=venue
    c=cfg(mode="shadow",armed_by="", accounts={"elonmusk": (Chain.SOL,Chain.BSC,Chain.ROBINHOOD)},
          chains={ch:tl.ChainRoute(ch,dex,Decimal(10)) for ch,dex in
                  [(Chain.SOL,"pump"),(Chain.BSC,"flap"),(Chain.ROBINHOOD,"pons")]})
    monkeypatch.setattr(tl,"load_config",lambda *a:c)
    monkeypatch.setattr(tl,"get_settings",lambda:SimpleNamespace(twitterapi_io_key="fixture"))
    monkeypatch.setattr(tl,"connect",lambda:conn)
    monkeypatch.setattr(tl,"observe_once",lambda *a:0)
    monkeypatch.setattr(tl,"_activity_own_conn",lambda *a:None)
    # lead 2026-10-07: background tasks added since (vamp scan, alpha marks) open and CLOSE their
    # own connection; with connect() pinned to this shared test connection they closed it under the
    # runner (ProgrammingError on Linux, a segfault on Windows). Production gives each task its own.
    monkeypatch.setattr(tl,"_vamp_own_conn",lambda *a:None)
    monkeypatch.setattr(tl,"_refs_marks_own_conn",lambda *a:None)
    from kaiba.execution import launch_preflight as _lp
    monkeypatch.setattr(_lp,"read_pump_global",lambda conn=None:(None,"fixture"))
    monkeypatch.setattr(tweet_launch_feed,"load_config",lambda *a:tweet_launch_feed.FeedConfig(backend))
    monkeypatch.setattr(x_stream,"sync_accounts",lambda *a:pytest.fail("must not subscribe"))
    done=threading.Event()
    plans=[]
    def process(p,cfg):
        plans.extend(tl.handle_post(conn,p,cfg))
        done.set()
    monkeypatch.setattr(tl,"_process_post",process)
    class Finished(Exception):
        pass
    async def selected(feed,cfg,**kw):
        assert feed.backend==backend
        yield post()
        assert await asyncio.to_thread(done.wait,2)
        raise Finished
    monkeypatch.setattr(tweet_launch_feed,"stream",selected)
    with pytest.raises(Finished):
        asyncio.run(tl.run())
    assert calls==[] and {p.chain for p in plans}=={Chain.SOL,Chain.BSC,Chain.ROBINHOOD}
    assert all(p.mode=="shadow" and p.verdict=="launch" for p in plans)
    assert conn.execute("SELECT count(*) FROM tweet_launches").fetchone()[0]==3
