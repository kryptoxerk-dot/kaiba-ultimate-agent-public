"""Nonblocking follow/project-post tee on the existing single social socket."""
from __future__ import annotations

import json
import logging
import socket
from pathlib import Path

from kaiba.hunters.social_follow import load_config

log = logging.getLogger(__name__)
_config = None
_watch = None
_socket = None
_drops = 0


def configuration():
    global _config
    p = Path("config/social_follow.yaml")
    stamp = p.stat().st_mtime_ns if p.exists() else 0
    if _config is None or _config[0]!=stamp:
        try:
            _config = (stamp,load_config(p))
        except (OSError,ValueError,TypeError):
            log.warning("follow configuration unavailable")
            return {"enabled":False}
    return _config[1]


def projects():
    global _watch
    cfg = configuration()
    if not cfg.get("enabled"):
        return {}
    p = Path(cfg["watch_path"])
    stamp = p.stat().st_mtime_ns if p.exists() else 0
    if _watch is None or _watch[0]!=stamp:
        try:
            body = json.loads(p.read_text(encoding="utf-8"))
            _watch = (stamp,{r["handle"]:r for r in body["projects"]})
        except (OSError,ValueError,KeyError,TypeError):
            return {}
    return _watch[1]


def send(event):
    global _socket,_drops
    cfg = configuration()
    if not cfg.get("enabled"):
        return False
    payload = json.dumps(event,ensure_ascii=True).encode()
    if len(payload)>60000:
        return False
    try:
        if _socket is None:
            _socket = socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
            _socket.setblocking(False)
        _socket.sendto(payload,str(Path(cfg["socket_path"]).resolve()))
        return True
    except OSError:
        _drops+=1
        if _drops==1 or _drops%100==0:
            log.warning("follow fanout dropped=%d",_drops)
        return False


def offer_event(event,payload,*,now_ms):
    if event not in ("following_update","unfollowing_update","follow_scan"):
        return False
    return send({"event":event,"payload":payload,"received_ms":now_ms})


def offer_post(post):
    project = projects().get(post.author)
    if not project:
        return False
    raw = post.raw
    author = raw.get("author") or {}
    uid = raw.get("author_id") or (author.get("id") if isinstance(author,dict) else None) or raw.get("user_id")
    # Fast events lacking immutable identity cannot authorize an official-source buy.
    if str(uid or "")!=project["id"]:
        return False
    return send({"event":"post","tweet_id":post.tweet_id,"author":post.author,"author_id":str(uid),
                 "text":post.text[:12000],"kind":post.kind,"created_ms":post.created_ms,
                 "received_ms":post.received_ms,"backend":post.backend})
