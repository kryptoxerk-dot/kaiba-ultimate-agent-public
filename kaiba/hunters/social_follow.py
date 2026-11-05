"""Watched-account follow graph, official project verification and prospective CA inbox.

Codex BOARD-CODEX-SOCIALFOLLOW-DEADLINE-20261027: reverse-edge lookup index and
bounded retry/defer for the two maintenance reads; handoff and budgets unchanged.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from kaiba.providers import social_follow as provider

log = logging.getLogger(__name__)
CONFIG = Path("config/social_follow.yaml")
RETRY_READS = {
    "verify_project": "SELECT * FROM projects WHERE (status='pending' OR "
        "(status='unavailable' AND checked_ms<?) OR (status IN ('verified','rejected') AND checked_ms<?)) "
        "AND EXISTS (SELECT 1 FROM edges WHERE target_id=projects.id AND active=1) "
        "ORDER BY (website!='') DESC, first_ms DESC LIMIT 1",
    "publish_watch": "SELECT id,handle,checked_ms,evidence_url FROM projects WHERE status='verified' AND checked_ms>=? "
        "AND EXISTS(SELECT 1 FROM edges WHERE target_id=projects.id AND active=1) "
        "ORDER BY first_ms DESC LIMIT ?",
}


class StoreReadDeferred(RuntimeError):
    """Both fresh bounded read attempts failed; never substitute an empty result."""

    def __init__(self, phase, reason, attempts):
        self.phase, self.reason, self.attempts = phase, reason, attempts
        super().__init__(f"social_follow_read_deferred:{phase}:{reason}")


def _deferred(command,exc):
    log.warning("social_follow deferred command=%s phase=%s reason=%s attempts=%s",
                command,exc.phase,exc.reason,exc.attempts)
    return {"status":"deferred","command":command,"phase":exc.phase,
            "reason":exc.reason,"attempts":exc.attempts,"partial_results_accepted":False}


def load_config(path=CONFIG):
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {"enabled":False,"db_path":"data/social-follow.db","socket_path":"data/social-follow.sock",
            "watch_path":"data/social-project-watch.json","max_projects":20,"max_post_age_s":30,
            "verification_ttl_s":86400,"handoff":False,**(raw or {})}


def sources():
    from kaiba.ingest import social_dispatch
    raw = yaml.safe_load(Path("config/tweet_launch.yaml").read_text(encoding="utf-8")) or {}
    handles = {str(a["handle"]).lstrip("@").lower() for a in raw.get("accounts",[])}
    handles |= {str(a["handle"]).lstrip("@").lower() for a in (raw.get("alpha") or {}).get("accounts",[])}
    return handles | social_dispatch.watched_accounts()


class Store:
    def __init__(self,path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.query("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY,handle TEXT,name TEXT,website TEXT,profile_json TEXT,first_ms INTEGER,last_ms INTEGER,status TEXT DEFAULT 'pending',checked_ms INTEGER DEFAULT 0,evidence_url TEXT,note TEXT)")
        self.query("CREATE TABLE IF NOT EXISTS edges (source TEXT,target_id TEXT,first_ms INTEGER,last_ms INTEGER,source_kind TEXT,baseline INTEGER,active INTEGER DEFAULT 1,PRIMARY KEY(source,target_id))")
        self.query("CREATE TABLE IF NOT EXISTS polls (source TEXT PRIMARY KEY,last_ms INTEGER,has_more INTEGER,n INTEGER,note TEXT)")
        poll_columns = {r["name"] for r in self.query("PRAGMA table_info(polls)")}
        if "success_ms" not in poll_columns:
            self.query("ALTER TABLE polls ADD COLUMN success_ms INTEGER DEFAULT 0")
            self.query("UPDATE polls SET success_ms=last_ms WHERE n IS NOT NULL AND note IN ('baseline','newest_page_only')")
        self.query("CREATE TABLE IF NOT EXISTS announcements (tweet_id TEXT PRIMARY KEY,project_id TEXT,handle TEXT,token TEXT,created_ms INTEGER,received_ms INTEGER,status TEXT,note TEXT,payload_json TEXT,chain TEXT,venue TEXT)")
        columns = {r["name"] for r in self.query("PRAGMA table_info(announcements)")}
        for name in ("chain","venue","issuer_account","issuer_checked_ms"):
            if name not in columns:
                kind = "INTEGER" if name=="issuer_checked_ms" else "TEXT"
                self.query(f"ALTER TABLE announcements ADD COLUMN {name} {kind}")
        self.query("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_announcement ON announcements(project_id,token) WHERE status IN ('queued','processing','handed')")
        self.query("CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status,checked_ms)")
        self.query("CREATE INDEX IF NOT EXISTS idx_edges_target_active ON edges(target_id,active)")

    def query(self,sql,args=(),*,read_retry=None):
        if self.path.exists() and self.path.stat().st_size > 256*1024**2:
            raise RuntimeError("social_follow_store_capacity")
        if read_retry is not None and RETRY_READS.get(read_retry)!=sql:
            raise ValueError("social_follow_invalid_read_retry")
        attempts = 2 if read_retry is not None else 1
        for attempt in range(1,attempts+1):
            c = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2) if read_retry else sqlite3.connect(self.path,timeout=2)
            c.row_factory = sqlite3.Row
            deadline, expired = time.monotonic()+2, False
            def progress(_deadline=deadline):
                nonlocal expired
                expired = time.monotonic()>_deadline
                return int(expired)
            c.set_progress_handler(progress,1000)
            reason = None
            try:
                rows = [dict(r) for r in c.execute(sql,args).fetchall()]
                if read_retry and time.monotonic()>deadline:
                    reason = "read_deadline"
                else:
                    c.commit()
                    return rows
            except sqlite3.OperationalError as exc:
                code = getattr(exc,"sqlite_errorcode",None)
                code = code & 255 if isinstance(code,int) else None
                if read_retry and code==sqlite3.SQLITE_INTERRUPT and expired:
                    reason = "read_deadline"
                elif read_retry and code in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):
                    reason = "read_busy" if code==sqlite3.SQLITE_BUSY else "read_locked"
                else:
                    raise
            finally:
                c.close()
            # No connection or read transaction survives into a retry or defer.
            if attempt==attempts:
                raise StoreReadDeferred(read_retry,reason,attempt) from None

    def edge(self,source,user,now,*,baseline=False,kind="poll_observed"):
        old = self.query("SELECT handle,website,profile_json FROM projects WHERE id=?",(user["id"],))
        changed = bool(old and (old[0]["handle"]!=user["handle"] or old[0]["website"]!=user["website"]
                              or json.loads(old[0]["profile_json"]).get("parody")!=user["parody"]
                              or json.loads(old[0]["profile_json"]).get("unavailable")!=user["unavailable"]))
        self.query("INSERT INTO projects (id,handle,name,website,profile_json,first_ms,last_ms) VALUES (?,?,?,?,?,?,?) "
                   "ON CONFLICT(id) DO UPDATE SET handle=excluded.handle,name=excluded.name,website=excluded.website,profile_json=excluded.profile_json,last_ms=excluded.last_ms",
                   (user["id"],user["handle"],user["name"],user["website"],json.dumps(user),now,now))
        if changed:
            self.query("UPDATE projects SET status='pending',checked_ms=0,note='profile_identity_changed' WHERE id=?",(user["id"],))
        new = not self.query("SELECT 1 FROM edges WHERE source=? AND target_id=?",(source,user["id"]))
        self.query("INSERT INTO edges (source,target_id,first_ms,last_ms,source_kind,baseline,active) VALUES (?,?,?,?,?,?,1) "
                   "ON CONFLICT(source,target_id) DO UPDATE SET last_ms=excluded.last_ms,active=1",
                   (source,user["id"],now,now,kind,int(baseline)))
        return new

    def snapshot(self,source,body,now):
        if not isinstance(body,dict) or body.get("status")!="success" or not isinstance(body.get("followings"),list):
            raise ValueError("invalid_following_snapshot")
        baseline = not self.query("SELECT 1 FROM polls WHERE source=? AND success_ms>0",(source,))
        users = [provider.normalize_user(u) for u in body["followings"]]
        new = sum(self.edge(source,u,now,baseline=baseline) for u in users if u)
        self.query("INSERT INTO polls (source,last_ms,has_more,n,note,success_ms) VALUES (?,?,?,?,?,?) "
                   "ON CONFLICT(source) DO UPDATE SET last_ms=excluded.last_ms,has_more=excluded.has_more,n=excluded.n,note=excluded.note,success_ms=excluded.success_ms",
                   (source,now,int(bool(body.get("has_next_page"))),len(body["followings"]),"baseline" if baseline else "newest_page_only",now))
        return {"source":source,"baseline":baseline,"first_seen_edges":new,"page_rows":len(body["followings"]),
                "complete_following_list":not bool(body.get("has_next_page")),"exact_follow_time":False}

    def j7(self,event,payload,now,watched):
        if event not in ("following_update","unfollowing_update") or not isinstance(payload,dict):
            return False
        actor = provider.normalize_user(payload.get("user"))
        if not actor or actor["handle"] not in watched:
            return False
        user = provider.normalize_user(payload.get("following") if event=="following_update" else payload.get("unfollowing"))
        if not user:
            return False
        if event=="unfollowing_update":
            self.query("UPDATE edges SET active=0,last_ms=? WHERE source=? AND target_id=?",(now,actor["handle"],user["id"]))
        else:
            self.edge(actor["handle"],user,now,kind="j7_follow_event")
        return True

    def verify_one(self,*,now=None,fetch=provider.public_https):
        now = now or int(time.time()*1000)
        rows = self.query(RETRY_READS["verify_project"],(now-3600000,now-86400000),read_retry="verify_project")
        if not rows:
            return None
        p = rows[0]
        profile = json.loads(p["profile_json"])
        status,note,url = "unavailable","website_unavailable",p["website"]
        if profile["parody"] or profile["unavailable"] or not url:
            status,note = "rejected","no_public_project_website_or_parody"
        else:
            try:
                final,html = fetch(url)
                status = "verified" if provider.has_backlink(html,p["handle"]) else "rejected"
                note = "profile_website_links_exact_account" if status=="verified" else "no_exact_account_backlink"
                url = final
            except Exception as exc:
                note = "website_unavailable:"+type(exc).__name__
        # Identity may have changed during network I/O; do not certify an old profile.
        self.query("UPDATE projects SET status=?,checked_ms=?,evidence_url=?,note=? WHERE id=? AND handle=? AND website=? AND profile_json=?",
                   (status,now,url,note,p["id"],p["handle"],p["website"],p["profile_json"]))
        return {"id":p["id"],"handle":p["handle"],"status":status,"note":note}

    def publish_watch(self,cfg,now=None):
        now = now or int(time.time()*1000)
        rows = self.query(RETRY_READS["publish_watch"],
                          (now-int(cfg["verification_ttl_s"])*1000,min(20,int(cfg["max_projects"]))),read_retry="publish_watch")
        target = Path(cfg["watch_path"])
        target.parent.mkdir(parents=True,exist_ok=True)
        data = json.dumps({"projects":rows},sort_keys=True)
        if not target.exists() or target.read_text(encoding="utf-8")!=data:
            temp = target.with_suffix(".new")
            temp.write_text(data,encoding="utf-8")
            temp.replace(target)
        return rows

    def announcement(self,event,cfg,now=None):
        now = now or int(time.time()*1000)
        uid,handle = str(event.get("author_id") or ""),event.get("author")
        rows = self.query("SELECT id FROM projects WHERE id=? AND handle=? AND status='verified' AND checked_ms>=? "
                          "AND EXISTS(SELECT 1 FROM edges WHERE target_id=projects.id AND active=1)",
                          (uid,handle,now-int(cfg["verification_ttl_s"])*1000))
        if not rows:
            return None
        age = now-event.get("created_ms",0) if type(event.get("created_ms")) is int else -1
        text = str(event.get("text") or "")
        candidates = set(re.findall(r"\b0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b",text))
        candidates = {x.lower() if x.startswith("0x") else x for x in candidates}
        reason = None
        if not 0<=age<=int(cfg["max_post_age_s"])*1000:
            reason = "stale_or_missing_post_time"
        elif event.get("kind") not in ("post","reply","quote","thread"):
            reason = "not_own_original_text"
        elif len(candidates)!=1:
            reason = "no_unique_exact_contract"
        elif not re.search(r"\b(?:ca|contract|mint|token address)\b",text,re.I):
            reason = "not_explicit_contract_announcement"
        elif re.search(r"\b(?:scam|fake|unofficial|testnet|avoid|do not buy|not our|old contract)\b",text,re.I):
            reason = "not_positive_project_contract_announcement"
        token = next(iter(candidates)) if len(candidates)==1 else None
        self.query("INSERT OR IGNORE INTO announcements (tweet_id,project_id,handle,token,created_ms,received_ms,status,note,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                   (event["tweet_id"],uid,handle,token,event.get("created_ms"),event.get("received_ms"),
                    "refused" if reason else "queued",reason,json.dumps(event)))
        if not reason and not self.query("SELECT 1 FROM announcements WHERE tweet_id=?",(event["tweet_id"],)):
            self.query("INSERT OR IGNORE INTO announcements (tweet_id,project_id,handle,token,created_ms,received_ms,status,note,payload_json) VALUES (?,?,?,?,?,?,'refused','duplicate_active_contract',?)",
                       (event["tweet_id"],uid,handle,token,event.get("created_ms"),event.get("received_ms"),json.dumps(event)))
        return None if reason else self.query("SELECT * FROM announcements WHERE tweet_id=? AND status='queued'",(event["tweet_id"],))

    def claim(self,tid):
        return bool(self.query("UPDATE announcements SET status='processing' WHERE tweet_id=? AND status='queued' RETURNING *",(tid,)))

    def status(self):
        return {"edges":self.query("SELECT COUNT(*) n,SUM(active) active FROM edges"),
                "projects":self.query("SELECT status,COUNT(*) n FROM projects GROUP BY status"),
                "polls":self.query("SELECT source,last_ms,n,has_more,note FROM polls ORDER BY last_ms DESC LIMIT 10"),
                "announcements":self.query("SELECT tweet_id,handle,token,status,note FROM announcements ORDER BY rowid DESC LIMIT 10")}


def poll_once(cfg,store,*,fetch=provider.followings,watched=None):
    handles = sorted(watched if watched is not None else sources())
    if not handles:
        return {"status":"no_sources"}
    counts = {p["source"]:p["last_ms"] for p in store.query("SELECT source,last_ms FROM polls")}
    priorities = {h:i for i,h in enumerate(cfg.get("source_priority") or [])}
    handle = min(handles,key=lambda h:(counts.get(h,0),priorities.get(h,999),h))
    try:
        body = fetch(handle,page_size=20)
    except provider.Unavailable as exc:
        store.query("INSERT INTO polls(source,last_ms,note) VALUES (?,?,?) ON CONFLICT(source) DO UPDATE SET last_ms=excluded.last_ms,note=excluded.note",
                    (handle,int(time.time()*1000),"unavailable:"+str(exc)))
        raise
    return store.snapshot(handle,body,int(time.time()*1000))


def process_announcement(row,cfg,store):
    from kaiba.execution.official_post_snipe import handoff
    if not store.claim(row["tweet_id"]):
        return
    try:
        def bind(launch,issuer=None,checked=None):
            store.query("UPDATE announcements SET chain=?,venue=?,issuer_account=?,issuer_checked_ms=? WHERE tweet_id=? AND status='processing'",
                        (launch.chain.value,launch.venue,issuer,checked,row["tweet_id"]))
        note = handoff(json.loads(row["payload_json"]),row["token"],enabled=cfg["handoff"],max_age_s=cfg["max_post_age_s"],bind_resolution=bind)
    except Exception as exc:
        note = "handoff_error:"+type(exc).__name__
    store.query("UPDATE announcements SET status=?,note=? WHERE tweet_id=?",
                ("handed" if note.startswith("signal:") else "refused",note,row["tweet_id"]))


def run(cfg,store):
    import fcntl

    from kaiba.ingest.follow_dispatch import configuration
    path = Path(cfg["socket_path"]).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    lock = path.with_suffix(".lock").open("a")
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if path.exists():
        path.unlink()
    sock = socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
    sock.bind(str(path))
    path.chmod(0o600)
    sock.settimeout(1)
    # At most two immediate CA jobs; no unbounded executor queue.
    futures = set()
    last_publish = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            while True:
                cfg = configuration()
                if time.monotonic()-last_publish>10:
                    try:
                        store.publish_watch(cfg)
                    except StoreReadDeferred as exc:
                        _deferred("run",exc)
                    last_publish = time.monotonic()
                futures = {f for f in futures if not f.done()}
                try:
                    event = json.loads(sock.recv(60001))
                except TimeoutError:
                    continue
                except (ValueError,UnicodeError):
                    continue
                if not isinstance(event,dict):
                    continue
                if not cfg.get("enabled"):
                    continue
                if event.get("event") in ("following_update","unfollowing_update"):
                    store.j7(event["event"],event["payload"],int(time.time()*1000),sources())
                elif event.get("event")=="follow_scan":
                    # J7's latest-CA scan is historical discovery, never a new-time buy.
                    payload = event.get("payload") or {}
                    contracts = ((payload.get("scan") or {}).get("contracts") or {})
                    if contracts.get("ok") is True:
                        project = store.query("SELECT id FROM projects WHERE handle=?",(str(payload.get("handle") or "").lower(),))
                        if len(project)==1:
                            for r in contracts.get("rows") or []:
                                if not r.get("isDeleted"):
                                    log.info("follow-scan source=%s contract=%s historical=true",payload.get("handle"),r.get("ca"))
                elif event.get("event")=="post":
                    rows = store.announcement(event,cfg)
                    if rows:
                        if len(futures)>=2:
                            store.query("UPDATE announcements SET status='refused',note='worker_capacity' WHERE tweet_id=?",(event["tweet_id"],))
                        else:
                            futures.add(pool.submit(process_announcement,rows[0],cfg,store))
        finally:
            sock.close()
            path.unlink(missing_ok=True)
            lock.close()


def _command(args,cfg,store):
    if args.command=="run":
        run(cfg,store)
        return
    if args.command in ("poll","maintain"):
        if not cfg.get("enabled"):
            print(json.dumps({"status":"disabled"}))
            return
        try:
            result = poll_once(cfg,store)
        except provider.Unavailable as exc:
            result = {"status":"unavailable","reason":str(exc)}
        if args.command=="maintain":
            verification = [store.verify_one() for _ in range(2)]
            watch = store.publish_watch(cfg)
            launch_cfg = yaml.safe_load(Path("config/tweet_launch.yaml").read_text(encoding="utf-8")) or {}
            sync = {"status":"j7_pending_or_selected"}
            if (launch_cfg.get("feed") or {}).get("backend")=="twitterapi_rule":
                try:
                    sync = provider.sync_rules([r["handle"] for r in watch])
                except Exception as exc:
                    sync = {"status":"unavailable","reason":type(exc).__name__}
            store.query("DELETE FROM announcements WHERE received_ms<?",(int(time.time()*1000)-7*86400000,))
            result = {"poll":result,"verification":verification,"project_rules":sync}
    elif args.command=="verify":
        result = [store.verify_one() for _ in range(min(3,max(1,args.limit)))]
        store.publish_watch(cfg)
    elif args.command=="projects":
        result = store.query("SELECT id,handle,website,status,note FROM projects ORDER BY first_ms DESC LIMIT ?",(min(50,max(1,args.limit)),))
    elif args.command=="edges":
        result = store.query("SELECT e.*,p.handle FROM edges e JOIN projects p ON p.id=e.target_id ORDER BY e.first_ms DESC LIMIT ?",(min(50,max(1,args.limit)),))
    else:
        result = store.status()
    print(json.dumps(result))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["run","poll","verify","maintain","status","projects","edges"])
    p.add_argument("--limit",type=int,default=10)
    args = p.parse_args()
    cfg = load_config()
    store = Store(cfg["db_path"])
    try:
        _command(args,cfg,store)
    except StoreReadDeferred as exc:
        print(json.dumps(_deferred(args.command,exc)))
        return 75
    return 0


if __name__=="__main__":
    raise SystemExit(main())
