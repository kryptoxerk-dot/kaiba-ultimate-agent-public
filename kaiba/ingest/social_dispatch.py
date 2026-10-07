"""Nonblocking fan-out from the ONE shared provider stream, before model/enrichment work."""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path

from kaiba.hunters.social_hunt import load_config

log = logging.getLogger(__name__)
_cached: tuple[int, dict] | None = None
_socket = None
_drops = 0


def configuration(path: Path = Path("config/social_hunt.yaml")) -> dict:
    global _cached
    stamp = path.stat().st_mtime_ns if path.exists() else 0
    if _cached is None or _cached[0] != stamp:
        try:
            _cached = (stamp, load_config(path))
        except (ValueError, OSError, TypeError):
            log.warning("social-hunt configuration unavailable; retaining prior research configuration")
            if _cached is None:
                return {"enabled": False, "accounts": {}}
    return _cached[1]


def watched_accounts(*, j7: bool = False) -> set[str]:
    c = configuration()
    if not c["enabled"]:
        return set()
    handles = {h for hs in c["accounts"].values() for h in hs}
    if j7 and c.get("all_j7_for_hunts"):
        import csv

        try:
            with Path(c["roster"]).open(encoding="utf-8-sig") as f:
                handles.update(row["handle"].lower() for row in csv.DictReader(f))
        except (OSError, KeyError):
            log.warning("social-hunt roster unavailable; using configured purpose lists")
    return handles


def offer(post) -> bool:
    """One bounded local send; never wait on a consumer, HTTP, model or SQLite lock."""
    global _socket, _drops
    c = configuration()
    if not c["enabled"]:
        return False
    raw = post.raw
    quote = raw.get("quoted_tweet") or raw.get("quotedTweet") or {}
    if not isinstance(quote, dict):
        quote = {}
    qa = quote.get("author") or {}
    if not isinstance(qa, dict):
        qa = {}
    event = {
        "tweet_id": post.tweet_id,
        "author": post.author,
        "text": post.text[:12000],
        "kind": post.kind,
        "created_ms": post.created_ms,
        "received_ms": post.received_ms,
        "dispatched_ms": int(time.time() * 1000),
        "feed_delay_ms": post.feed_delay_ms,
        "backend": post.backend,
        "media": list(post.media[:4]),
        "quoted": {
            "tweet_id": quote.get("id"),
            "author": qa.get("userName") or qa.get("handle"),
            "text": str(quote.get("text") or "")[:12000],
        },
        "parent_id": raw.get("inReplyToId"),
    }
    payload = json.dumps(event, ensure_ascii=True).encode()
    if len(payload) > 60000:
        event["text"] = event["text"][:4000]
        event["quoted"]["text"] = event["quoted"]["text"][:4000]
        payload = json.dumps(event, ensure_ascii=True).encode()
    try:
        if _socket is None:
            _socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _socket.setblocking(False)
        _socket.sendto(payload, str(Path(c["socket_path"]).resolve()))
        return True
    except (OSError, AttributeError):
        _drops += 1
        if _drops == 1 or _drops % 100 == 0:
            log.warning("social-hunt fanout unavailable/dropped total=%d", _drops)
        return False
