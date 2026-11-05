"""Official-account CA announcements -> existing launch-snipe evaluation/engine gates.

No swap, order or launch call. Never mutate risk, mode, venue lists or persisted dev/name
watchlists. The owner's official-source mandate supplies an ephemeral exact name hit;
every existing snipe veto, daily cap, dossier and engine/execution protection still applies.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from kaiba.core.schemas import Chain, digest

_budgets = {}
_budget_lock = threading.Lock()


def dossier_budget(kind="dossier",limit=2):
    with _budget_lock:
        if kind not in _budgets:
            from kaiba.execution.snipe import DossierBudget
            class LockedBudget:
                def __init__(self):
                    self.inner = DossierBudget(limit)
                    self.lock = threading.Lock()

                def take(self):
                    with self.lock:
                        return self.inner.take()
            _budgets[kind] = LockedBudget()
    return _budgets[kind]


def token_official_profile(chain,token,handle,*,info=None):
    """Issuer profile metadata must match; a token's narrative tweet URL does not."""
    import re
    from urllib.parse import urlsplit

    from kaiba.core.limiter import Priority
    from kaiba.providers import gmgn_cli

    if info is None and not dossier_budget("identity",4).take():
        raise RuntimeError("source_identity_budget_exhausted")
    response = (info or gmgn_cli.token_info)(token,chain,priority=Priority.RESEARCH)
    if not getattr(response,"ok",False) or not isinstance(getattr(response,"data",None),dict):
        return None
    link=response.data.get("link") or {}
    value=link.get("twitter_username") if isinstance(link,dict) else None
    if not isinstance(value,str):
        return None
    value=value.strip()
    if value.startswith("https://"):
        parsed=urlsplit(value)
        if parsed.hostname not in {"x.com","twitter.com","www.x.com","www.twitter.com"}:
            return False
        value=parsed.path.strip("/")
    value=value.lstrip("@").lower()
    return bool(re.fullmatch(r"[a-z0-9_]{1,15}",value) and value==handle.lower())


def token_rows(conn,token):
    """Three compound-key lookups, no history scans or ticker guessing."""
    rows = []
    for chain in (Chain.SOL,Chain.BSC,Chain.ROBINHOOD):
        r = conn.execute("SELECT * FROM tokens WHERE chain=? AND address=?",(chain.value,token)).fetchone()
        if r:
            rows.append(dict(r))
    return rows


def live_snipes_today(conn,chain,now):
    """Existing numeric cap, counted against real-money entry reservations only."""
    deadline=time.monotonic()+2
    conn.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
    try:
        row=conn.execute("SELECT COUNT(DISTINCT token) FROM decisions WHERE lane='launch-snipe' "
                         "AND chain=? AND action='enter' AND mode IN ('live','canary') AND ts_ms>=?",
                         (chain.value,now-now%86400000)).fetchone()
        return int(row[0])
    finally:
        conn.set_progress_handler(None,0)


def launch_from_row(row,received_ms):
    from kaiba.ingest import launch_feed as lf
    chain = Chain(row["chain"])
    if chain is Chain.SOL:
        if str(row.get("launchpad") or "").lower() not in {"pump.fun","pumpfun","pump","launchlab"}:
            raise ValueError("unsupported_sol_launchpad")
        return lf.sol_launch_from_row(row,received_ms=received_ms)
    meta = json.loads(row.get("meta_json") or "{}")
    return lf.Launch(chain=chain,token=row["address"],venue=row.get("launchpad") or "unknown",
                     creator=row.get("creator"),curve=meta.get("curve_address") or meta.get("curve"),
                     pair_token=meta.get("pair_token"),block=meta.get("block"),
                     graduation_threshold=int(meta["graduation_threshold_wei"]) if meta.get("graduation_threshold_wei") else None,
                     launched_ms=row.get("created_ms"),received_ms=received_ms,
                     name=row.get("name"),symbol=row.get("symbol"),meta=meta)


def handoff(event,token,*,enabled,max_age_s=30,connect=None,params=None,evaluate=None,
            deliver=None,prepare=None,now_ms=None,bind_resolution=None,identity_info=None):
    from kaiba.core.config import get_settings
    from kaiba.core.db import connect as db_connect
    from kaiba.execution import snipe

    now = int(time.time()*1000) if now_ms is None else now_ms
    created = event.get("created_ms")
    if type(created) is not int or not 0<=now-created<=max_age_s*1000:
        return "stale_at_handoff"
    if not enabled:
        return "handoff_disabled"
    if connect is None:
        wal = Path(str(get_settings().db_path)+"-wal")
        if wal.exists() and wal.stat().st_size>1500000000:
            return "wal_over_1.5GB"
    c = (connect or db_connect)()
    try:
        rows = token_rows(c,token)
        if len(rows)!=1:
            return "chain_or_token_unresolved" if not rows else "ambiguous_cross_chain_contract"
        try:
            launch = launch_from_row(rows[0],event["received_ms"])
        except ValueError:
            return "unsupported_or_unreadable_launch_record"
        # Resolve only indexed compound-key rows; no long cursor spans chain/provider I/O.
        if prepare is None and launch.chain.value not in [str(ch) for ch in (params or snipe.params()).get("chains",[])]:
            return "snipe_refused:chain_not_enabled:"+launch.chain.value
        if not launch.name and not launch.symbol:
            return "token_identity_unavailable"
        if bind_resolution:
            bind_resolution(launch)
        p = dict(params if params is not None else snipe.params())
        # This is the owner's exact official-contract source, scoped only to this CA.
        # Existing evaluate() applies record, venue, tax, liquidity and daily vetoes.
        p["name_watchlist"] = [launch.symbol or launch.name]
        p["watch_strength"] = min(float(p.get("watch_strength",.75)),.75)
        record = snipe.record_for(c,launch.chain,launch.creator)
        kw = {"snipes_today":live_snipes_today(c,launch.chain,now)}
        if prepare is not None:
            kw.update(prepare(c,launch,p))
        elif launch.chain is Chain.ROBINHOOD:
            _,head = snipe.wait_for_tax(launch,p,conn=c)
            kw["book"] = snipe.read_early_book(launch,p,head,conn=c) if head else snipe.EarlyBook(note="chain_stalled")
        elif launch.chain is Chain.BSC:
            kw["flap"] = snipe.read_flap_launch(launch,p,conn=c)
            kw["native_ok"] = snipe.bsc_native_price_ok(c)
        verdict = (evaluate or snipe.evaluate)(launch,p,record,**kw)
        if not verdict.fire:
            return "snipe_refused:"+",".join(verdict.reasons)
        try:
            official=token_official_profile(launch.chain,token,event["author"],info=identity_info)
        except Exception:
            return "token_official_account_unavailable"
        if official is None:
            return "token_official_account_unavailable"
        if not official:
            return "token_official_account_unverified"
        if bind_resolution:
            bind_resolution(launch,event["author"],int(time.time()*1000 if now_ms is None else now_ms))
        verdict.features.update({"official_post_id":event["tweet_id"],"official_account_id":event["author_id"],
                                 "official_account":event["author"],"source_post_created_ms":created,
                                 "source_received_ms":event["received_ms"],"source":"official_contract_post"})
        # The first paper launch must not suppress a later fresh official source.
        # Stable source/token identity still permits only one official signal per token.
        if int(time.time()*1000 if now_ms is None else now_ms)-created>max_age_s*1000:
            return "stale_after_chain_checks"
        if deliver is None:
            from kaiba.execution import lanes
            stale = False
            official_id = "sig_official_"+digest({"chain":launch.chain.value,"token":token})[:20]
            def record(signal,conn):
                nonlocal stale
                stale = int(time.time()*1000)-created>max_age_s*1000
                return False if stale else lanes.record(signal.model_copy(update={"signal_id":official_id}),conn)
            signal_id,note = snipe.hand_to_engine(c,launch,verdict,p,dossier_budget(),record_signal=record)
            if stale:
                return "stale_after_dossier"
            if signal_id:
                signal_id=official_id
        else:
            signal_id,note = deliver(c,launch,verdict,p,dossier_budget())
        return "signal:"+signal_id+":"+note if signal_id else "snipe_handoff_refused:"+note
    finally:
        c.close()


def official_post_permission(signal,conn,*,config=None,now_ms=None):
    """Prepared source-only arming proof; engine hook is NOT installed by this task.

    The source label alone grants nothing. Require owner-enabled configuration,
    actual verified project/active follow, immutable author identity, original fresh
    announcement and a receiver-bound exact token/chain/venue receipt in our tiny DB.
    """
    import sqlite3

    from kaiba.hunters.social_follow import load_config

    cfg = load_config() if config is None else config
    p = signal.payload or {}
    if not cfg.get("enabled") or cfg.get("live_official_posts") is not True or signal.lane.value!="launch-snipe":
        return False
    if p.get("source")!="official_contract_post":
        return False
    now = int(time.time()*1000) if now_ms is None else now_ms
    try:
        deadline = time.monotonic()+2
        c = sqlite3.connect(f"file:{Path(cfg['db_path']).as_posix()}?mode=ro",uri=True,timeout=1)
        c.row_factory = sqlite3.Row
        c.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        try:
            r = c.execute("SELECT a.*,p.checked_ms FROM announcements a JOIN projects p ON p.id=a.project_id "
                          "WHERE a.tweet_id=? AND a.project_id=? AND a.handle=? AND a.token=? AND a.chain=? "
                          "AND a.status IN ('processing','handed') AND p.handle=a.handle AND p.status='verified' "
                          "AND EXISTS(SELECT 1 FROM edges e WHERE e.target_id=p.id AND e.active=1)",
                          (p.get("official_post_id"),p.get("official_account_id"),p.get("official_account"),signal.token,signal.chain.value)).fetchone()
        finally:
            c.close()
        if r is None or not 0<=now-r["created_ms"]<=min(30,int(cfg["max_post_age_s"]))*1000:
            return False
        if r["issuer_account"]!=p.get("official_account") or r["issuer_checked_ms"] is None or not 0<=now-r["issuer_checked_ms"]<=30000:
            return False
        if not 0<=now-r["checked_ms"]<=int(cfg["verification_ttl_s"])*1000:
            return False
        allowed = {"sol":{"pump.fun"},"bsc":{"flap"},"robinhood":{"pons"}}
        if r["venue"] not in allowed.get(signal.chain.value,set()):
            return False
        row = conn.execute("SELECT launchpad FROM tokens WHERE chain=? AND address=?",(signal.chain.value,signal.token)).fetchone()
        return bool(row and str(row["launchpad"]).lower()==r["venue"])
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
        return False
