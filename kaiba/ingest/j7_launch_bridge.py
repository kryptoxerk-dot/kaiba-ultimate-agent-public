"""J7 Socket.IO -> existing tweet-launch plans. This module never submits a transaction.

Claude owns the launcher and its risk integration. This additive adapter supplies XPost
objects, waits for media enrichment and offers a bounded shadow preview. No J7 wallet
credential or token-deployment endpoint is needed. Raw feed content is data, not code.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kaiba.ingest.x_stream import XPost

HOSTS = {"nyc": "https://nyc.j7tracker.io", "dfw": "https://dfw.j7tracker.io"}
TOKEN_FILE = Path.home() / ".config/kaiba/j7.session"
KINDS = {"TWEET": "post", "REPLY": "reply", "QUOTE": "quote", "RETWEET": "repost"}


class FeedUnavailable(Exception):
    """Stable diagnostic; never include provider payloads or credentials."""


def session_token(path: Path | None = None) -> str:
    value = os.environ.get("J7_SESSION_JWT", "").strip()
    if not value:
        try:
            value = (path or TOKEN_FILE).read_text(encoding="utf-8-sig").strip()
        except OSError:
            raise FeedUnavailable("missing_j7_session") from None
    if not value or any(c.isspace() for c in value):
        raise FeedUnavailable("invalid_j7_session")
    return value


def timestamp_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                return int(stamp.timestamp() * 1000)
        except (ValueError, OverflowError):
            pass
    return None


def merge_nonempty(old: dict, new: Mapping) -> dict:
    """J7's later fast-path copies can be emptier than earlier rich copies."""
    result = dict(old)
    for key, value in new.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, Mapping):
            previous = result.get(key)
            result[key] = merge_nonempty(previous if isinstance(previous, dict) else {}, value)
        elif key in {"isReply", "isQuote", "isRetweet"}:
            result[key] = bool(result.get(key)) or bool(value)
        else:
            result[key] = value
    return result


def images(payload: Mapping) -> tuple[str, ...]:
    media = payload.get("media")
    entries = media.get("images", []) if isinstance(media, Mapping) else []
    if not isinstance(entries, list):
        return ()
    urls = []
    for item in entries[:8]:
        url = item.get("url") if isinstance(item, Mapping) else None
        if not isinstance(url, str) or len(url) > 2048:
            continue
        try:
            parsed = urlsplit(url)
            if (parsed.scheme == "https" and parsed.hostname == "pbs.twimg.com"
                    and parsed.username is None and parsed.password is None
                    and parsed.port in {None, 443} and parsed.path.startswith("/media/")):
                urls.append(url)
        except ValueError:
            continue
    return tuple(dict.fromkeys(urls))


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    post_id: str | None = None
    first_seen_ms: int | None = None
    revision: int = 0
    post: XPost | None = None


class Bridge:
    def __init__(self, authors, *, started_ms: int, max_age_s: int = 20,
                 require_image: bool = True, capacity: int = 5000):
        if max_age_s <= 0 or capacity <= 0:
            raise ValueError("positive age and capacity required")
        self.authors = {str(a).lstrip("@").lower() for a in authors}
        self.started_ms = started_ms
        self.max_age_ms = max_age_s * 1000
        self.require_image = require_image
        self.capacity = capacity
        self.entries: OrderedDict[str, dict] = OrderedDict()
        self.saturated = False

    def ingest(self, event: str, payload: Any, *, now_ms: int) -> Outcome:
        if event not in {"tweet", "tweet_update", "initialTweets", "tweet_deleted"}:
            return Outcome("ignored", "not_a_tweet_event")
        if not isinstance(payload, Mapping):
            return Outcome("ignored", "malformed_payload")
        tid = payload.get("id")
        if not isinstance(tid, str) or not re.fullmatch(r"[0-9]{15,22}", tid):
            return Outcome("ignored", "invalid_post_id")
        incoming_author = payload.get("author")
        if isinstance(incoming_author, Mapping):
            incoming_handle = str(incoming_author.get("handle") or "").lstrip("@").lower()
            if incoming_handle and incoming_handle not in self.authors and tid not in self.entries:
                return Outcome("ignored", "unwatched_author", tid)
        state = self.entries.get(tid)
        if state is None:
            # Do not evict terminal IDs: that could make a duplicate eligible again.
            if len(self.entries) >= self.capacity:
                self.saturated = True
                return Outcome("unavailable", "dedupe_capacity", tid)
            state = {"payload": {}, "first_seen_ms": now_ms, "revision": 0, "terminal": None}
            self.entries[tid] = state
        state["revision"] += 1
        base = {"post_id": tid, "first_seen_ms": state["first_seen_ms"],
                "revision": state["revision"]}
        if event in {"initialTweets", "tweet_deleted"}:
            state["terminal"] = "backlog" if event == "initialTweets" else "deleted"
            return Outcome("ignored", state["terminal"], **base)
        if state["terminal"]:
            return Outcome("ignored", state["terminal"], **base)
        if payload.get("source") in {"external", "webhook"} or payload.get("platform") not in {None, "X"}:
            state["terminal"] = "non_x"
            return Outcome("ignored", "non_x", **base)
        old_author = state["payload"].get("author") or {}
        new_author = payload.get("author") or {}
        if not isinstance(new_author, Mapping):
            return Outcome("pending", "missing_author", **base)
        old_handle = str(old_author.get("handle") or "").lstrip("@").lower()
        new_handle = str(new_author.get("handle") or "").lstrip("@").lower()
        old_id, new_id = old_author.get("id"), new_author.get("id")
        if ((old_handle and new_handle and old_handle != new_handle)
                or (old_id and new_id and str(old_id) != str(new_id))):
            state["terminal"] = "author_changed"
            return Outcome("ignored", "author_changed", **base)
        old_created = timestamp_ms(state["payload"].get("createdAt"))
        new_created = timestamp_ms(payload.get("createdAt"))
        if old_created is not None and new_created is not None and old_created != new_created:
            state["terminal"] = "created_time_changed"
            return Outcome("ignored", "created_time_changed", **base)
        merged = merge_nonempty(state["payload"], payload)
        # Retain only documented fields used by this bridge. No arbitrary raw blob persists.
        merged = {k: v for k, v in merged.items() if k in {
            "id", "author", "text", "type", "createdAt", "media", "isReply", "isQuote", "isRetweet"}}
        state["payload"] = merged
        author = merged.get("author") or {}
        handle = str(author.get("handle") or "").lstrip("@").lower()
        if not handle:
            return Outcome("pending", "missing_author", **base)
        if not re.fullmatch(r"[a-z0-9_]{1,15}", handle) or handle not in self.authors:
            state["terminal"] = "unwatched_author"
            return Outcome("ignored", "unwatched_author", **base)
        created = timestamp_ms(merged.get("createdAt"))
        if created is None:
            return Outcome("pending", "missing_created_time", **base)
        reason = ("before_connection" if created < self.started_ms else
                  "future_created_time" if created > now_ms + 1000 else
                  "too_late" if now_ms - created > self.max_age_ms else None)
        if reason:
            state["terminal"] = reason
            return Outcome("ignored", reason, **base)
        kind = KINDS.get(str(merged.get("type") or "").upper())
        if merged.get("isRetweet"):
            kind = "repost"
        elif merged.get("isReply"):
            kind = "reply"
        elif merged.get("isQuote"):
            kind = "quote"
        if kind is None:
            return Outcome("pending", "missing_post_kind", **base)
        media = images(merged)
        if self.require_image and not media:
            return Outcome("pending", "awaiting_tweet_image", **base)
        if not isinstance(merged.get("text"), str) or not merged["text"].strip():
            return Outcome("pending", "awaiting_text", **base)
        state["terminal"] = "delivered"
        post = XPost(tweet_id=tid, author=handle, text=merged["text"][:20000], kind=kind,
                     media=media, created_ms=created, received_ms=now_ms,
                     feed_delay_ms=None, backend="j7tracker", display_name=author.get("name"),
                     raw={"first_seen_ms": state["first_seen_ms"], "eligible_observed_ms": now_ms,
                          "revision": state["revision"], "author_id": author.get("id")})
        return Outcome("ready", "complete_tweet", post=post, **base)

    def queued_post_valid(self, post: XPost, *, now_ms: int) -> bool:
        state = self.entries.get(post.tweet_id)
        return bool(state and state["terminal"] == "delivered" and post.created_ms is not None
                    and 0 <= now_ms - post.created_ms <= self.max_age_ms)


async def stream(jwt: str, authors, *, max_age_s: int = 20, require_image: bool = True,
                 region: str = "dfw", client_factory: Callable | None = None,
                 audit: Callable[[Outcome], None] | None = None) -> AsyncIterator[XPost]:
    """One authenticated connection. Stop on errors; no subscription/launch API is called."""
    if region not in HOSTS or not jwt:
        raise FeedUnavailable("invalid_connection_parameters")
    if client_factory is None:
        try:
            import socketio
        except ImportError:
            raise FeedUnavailable("missing_socketio_dependency") from None
        def client_factory():
            return socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
    client = client_factory()
    queue: asyncio.Queue[XPost] = asyncio.Queue(maxsize=128)
    bridge = Bridge(authors, started_ms=int(time.time() * 1000), max_age_s=max_age_s,
                    require_image=require_image)
    failure = None

    async def ingest(event, payload):
        nonlocal failure
        outcome = bridge.ingest(event, payload, now_ms=int(time.time() * 1000))
        if audit:
            audit(outcome)
        if outcome.status == "unavailable":
            failure = outcome.reason
        if outcome.post:
            try:
                queue.put_nowait(outcome.post)
            except asyncio.QueueFull:
                failure = "feed_queue_full"

    @client.event
    async def connect():
        bridge.started_ms = int(time.time() * 1000)
        await client.emit("user_connected", jwt)

    @client.event
    async def disconnect(*_args):
        nonlocal failure
        failure = failure or "feed_disconnected"

    @client.on("auth_error")
    async def auth_error(_payload):
        nonlocal failure
        failure = "feed_auth_error"

    @client.on("initialTweets")
    async def initial(items):
        for item in items if isinstance(items, list) else []:
            await ingest("initialTweets", item)

    for event in ("tweet", "tweet_update", "tweet_deleted"):
        async def receive(payload, _event=event):
            await ingest(_event, payload)
        client.on(event, receive)
    try:
        await client.connect(HOSTS[region], transports=["websocket"], auth={"token": jwt}, wait_timeout=15)
        while True:
            if failure:
                raise FeedUnavailable(failure)
            try:
                post = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                continue
            if bridge.queued_post_valid(post, now_ms=int(time.time() * 1000)):
                yield post
    except FeedUnavailable:
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        raise FeedUnavailable("feed_connection_failed") from None
    finally:
        if client.connected:
            try:
                await client.disconnect()
            except Exception:
                pass  # Preserve the primary stable error; never expose cleanup payloads.


def shadow_plans(post: XPost, config, *, now_ms: int | None = None):
    """Reuse the real three-chain planner, forcibly shadow; no DB or executor import."""
    from kaiba.execution.tweet_launch import plan_post
    config = replace(config, mode="shadow", armed_by="")
    return plan_post(post, config, now_ms=now_ms)


async def preview(token: str, config, output: Path, seconds: int, region: str) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    posts = plans = 0
    with output.open("a", encoding="utf-8") as handle:
        async def collect():
            nonlocal posts, plans
            async for post in stream(token, config.accounts, max_age_s=config.max_tweet_age_s,
                                     require_image=config.require_image, region=region):
                posts += 1
                for plan in shadow_plans(post, config):
                    data = asdict(plan)
                    data["chain"] = plan.chain.value
                    data["buy_amt_native"] = str(plan.buy_amt_native) if plan.buy_amt_native is not None else None
                    data["argv"] = None  # Preview is not an executable transaction artifact.
                    handle.write(json.dumps({"source": "j7tracker", "first_seen_ms": post.raw["first_seen_ms"],
                                             "eligible_observed_ms": post.received_ms, "plan": data}) + "\n")
                    handle.flush()
                    plans += 1
        try:
            await asyncio.wait_for(collect(), timeout=seconds)
        except TimeoutError:
            pass
    return {"status": "ok", "mode": "shadow", "posts": posts, "plans": plans,
            "bounded_seconds": seconds, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, choices=range(1, 301), default=60)
    parser.add_argument("--region", choices=HOSTS, default="dfw")
    args = parser.parse_args()
    try:
        from kaiba.execution.tweet_launch import load_config
        result = asyncio.run(preview(session_token(args.token_file), load_config(args.config),
                                     args.output, args.seconds, args.region))
        print(json.dumps(result))
        return 0
    except FeedUnavailable as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc), "mode": "shadow"}))
        return 1
    except OSError:
        print(json.dumps({"status": "unavailable", "reason": "local_io_error", "mode": "shadow"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
