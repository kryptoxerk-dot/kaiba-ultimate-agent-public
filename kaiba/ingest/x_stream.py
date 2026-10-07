"""Real-time posts from watched X accounts, for the tweet launcher.

Why this backend (2026-10-06)
-----------------------------

The tweet launcher only earns anything if it is early: a viral post draws a median of 12
copycat launches, the first within ~73 s (docs/research/tweet-launch-tracked-accounts-20261006.md).
Search APIs (``kaiba.providers.x_search``) poll; they are seconds-to-minutes late. The routes
that push:

* **twitterapi.io Stream** (chosen). One WebSocket, ``wss://ws.twitterapi.io/twitter/tweet/websocket``,
  ``x-api-key`` header. Accounts are subscribed by handle over REST
  (``/oapi/x_user_stream/add_user_to_monitor_tweet``). Published latency: 50% < 500 ms, 80% <
  1 s; each ``fast_tweet`` event carries ``snow_delay_ms`` (post -> their server). Starter plan
  $29/mo for 6 accounts. The key lives on the box as ``TWITTERAPI_IO_KEY``.
* twitterapi.io filter rules (``from:a OR from:b``) arrive on the SAME socket as batched
  ``tweet`` events with a ``rule_id``; they are parsed too, so either subscription works.
* j7tracker (Socket.IO feed, < 200 ms claimed) needs a login session token; not wired.

ONE connection per API key: a second socket is rejected with close code 1008, and after any
disconnect the provider asks for 90 s before reconnecting. So exactly one process opens this
socket (the ``tweet-launch`` service) and reconnects slowly.

Nothing here decides or trades. :func:`stream` yields :class:`XPost`; the launcher decides.
The API key is never logged: only :func:`_redact`'d strings leave this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

WS_URL = "wss://ws.twitterapi.io/twitter/tweet/websocket"
API_BASE = "https://api.twitterapi.io"
ADD_USER = "/oapi/x_user_stream/add_user_to_monitor_tweet"
REMOVE_USER = "/oapi/x_user_stream/remove_user_to_monitor_tweet"
LIST_USERS = "/oapi/x_user_stream/get_user_to_monitor_tweet"

#: The provider asks for >= 90 s between a disconnect and the next connect (one socket per key).
RECONNECT_FLOOR_S = 90.0
#: Close 1013 = "at capacity"; 1008 = duplicate connection or bad key. Both want a long wait.
SLOW_CLOSE_CODES = {1008, 1013}
DEDUPE_CAP = 5000
#: The ``type`` values the stream's flat ``fast_tweet`` shape uses for what a post IS.
STREAM_KINDS = frozenset({"post", "reply", "repost", "quote", "thread"})


@dataclass(frozen=True)
class XPost:
    tweet_id: str
    author: str                       # screen name, lower case, no "@"
    text: str
    kind: str                         # post | reply | repost | quote | thread | ...
    media: tuple[str, ...]
    created_ms: int | None
    received_ms: int
    feed_delay_ms: int | None
    backend: str
    display_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author}/status/{self.tweet_id}"

    @property
    def age_ms(self) -> int | None:
        return None if self.created_ms is None else self.received_ms - self.created_ms


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redact(text: str, key: str | None) -> str:
    return text.replace(key, "***") if key else text


_LEGACY_TIME = "%a %b %d %H:%M:%S %z %Y"


def _legacy_ms(value: Any) -> int | None:
    if not value:
        return None
    from datetime import datetime
    try:
        return int(datetime.strptime(str(value), _LEGACY_TIME).timestamp() * 1000)
    except ValueError:
        return None


def _snowflake_ms(tweet_id: str) -> int | None:
    """X snowflake ids carry their creation time: (id >> 22) + 1288834974657."""
    try:
        return (int(tweet_id) >> 22) + 1288834974657
    except (TypeError, ValueError):
        return None


def _media_from_full(t: Mapping[str, Any]) -> tuple[str, ...]:
    out: list[str] = []
    for src in (t.get("extendedEntities") or {}, t.get("entities") or {}):
        for m in src.get("media") or []:
            u = m.get("media_url_https") or m.get("media_url")
            if u and u not in out:
                out.append(u)
    return tuple(out)


def _kind_from_full(t: Mapping[str, Any]) -> str:
    if t.get("retweeted_tweet"):
        return "repost"
    if t.get("quoted_tweet"):
        return "quote"
    if t.get("isReply") or t.get("inReplyToId"):
        return "reply"
    return "post"


def parse_event(msg: Mapping[str, Any], *, received_ms: int | None = None) -> list[XPost]:
    """Every post in one socket message. Housekeeping events (connected, ping) yield none.

    Two shapes (twitterapi.io WebSocket guide, April 2026):

    * ``fast_tweet`` -- one flat ``tweet`` object: ``id, screen_name, text, type, created_ms,
      media[], snow_delay_ms``.
    * ``tweet`` -- a ``tweets[]`` batch of full Twitter-shape objects (``author.userName``,
      ``createdAt`` legacy string, ``entities.media``). Rule matches carry ``rule_id``; stream
      posts for accounts not yet in the fast lane come the same way without it.
    """
    rec = received_ms if received_ms is not None else _now_ms()
    kind = msg.get("event_type")
    if kind == "fast_tweet":
        t = msg.get("tweet") or {}
        tid = str(t.get("id") or "")
        author = str(t.get("screen_name") or "").lstrip("@").lower()
        if not tid or not author:
            return []
        created = t.get("created_ms") or t.get("snowflake_created_ms") or _snowflake_ms(tid)
        return [XPost(
            tweet_id=tid, author=author, text=str(t.get("text") or ""),
            kind=str(t.get("type") or "post"), media=tuple(t.get("media") or ()),
            created_ms=int(created) if created else None, received_ms=rec,
            feed_delay_ms=t.get("snow_delay_ms"), backend="twitterapi_io_stream",
            display_name=t.get("display_name"), raw=t,
        )]
    if kind == "tweet":
        backend = "twitterapi_io_rule" if msg.get("rule_id") else "twitterapi_io_stream"
        posts = []
        for t in msg.get("tweets") or ([msg["tweet"]] if isinstance(msg.get("tweet"), Mapping) else []):
            tid = str(t.get("id") or "")
            a = t.get("author") or {}
            author = str(a.get("userName") or a.get("username") or a.get("screen_name")
                         or t.get("screen_name") or "").lstrip("@").lower()
            if not tid or not author:
                continue
            created = _legacy_ms(t.get("createdAt")) or t.get("created_ms") or _snowflake_ms(tid)
            media = tuple(t.get("media") or ()) or _media_from_full(t)
            # In the full Twitter shape ``type`` is the OBJECT type ("tweet" on every row,
            # MEASURED on 27 rule deliveries 2026-10-07), not post/reply/quote. Reading it as
            # the kind made every rule-feed post fail the post-kind filter.
            declared = str(t.get("type") or "")
            kind = declared if declared in STREAM_KINDS else _kind_from_full(t)
            posts.append(XPost(
                tweet_id=tid, author=author, text=str(t.get("text") or ""),
                kind=kind, media=media,
                created_ms=int(created) if created else None, received_ms=rec,
                feed_delay_ms=None, backend=backend, display_name=a.get("name"), raw=t,
            ))
        return posts
    return []


class Deduper:
    """Drop a post id already seen (the provider may deliver one twice)."""

    def __init__(self, cap: int = DEDUPE_CAP) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.cap = cap

    def first(self, tweet_id: str) -> bool:
        if tweet_id in self._seen:
            return False
        self._seen[tweet_id] = None
        if len(self._seen) > self.cap:
            self._seen.popitem(last=False)
        return True


async def stream(
    api_key: str,
    *,
    connect: Any = None,
    reconnect_floor_s: float = RECONNECT_FLOOR_S,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[XPost]:
    """Yield posts forever, reconnecting no faster than the provider allows."""
    if not api_key:
        raise ValueError("TWITTERAPI_IO_KEY is not configured")
    if connect is None:
        import websockets
        def connect():  # noqa: E306 - local factory keeps the import lazy
            return websockets.connect(
                WS_URL, additional_headers={"x-api-key": api_key},
                ping_interval=40, ping_timeout=30, max_size=8 * 2**20,
            )
    dedupe = Deduper()
    while stop is None or not stop.is_set():
        wait = reconnect_floor_s
        try:
            async with connect() as ws:
                log.info("x_stream connected")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if msg.get("event_type") == "connected":
                        log.info("x_stream: provider confirmed the key")
                    for post in parse_event(msg):
                        if dedupe.first(post.tweet_id):
                            yield post
                    if stop is not None and stop.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any socket failure -> slow reconnect
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            if code in SLOW_CLOSE_CODES:
                wait = max(wait, 120.0)
            log.warning("x_stream disconnected (%s %s); reconnect in %.0f s",
                        type(exc).__name__, code, wait)
        if stop is not None and stop.is_set():
            return
        await asyncio.sleep(wait)


# --------------------------------------------------------------------------------------
# account subscription (REST)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    added: tuple[str, ...]
    already: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    unlisted: tuple[str, ...]     # monitored on the provider but not in our config (left alone)


def monitored(api_key: str, *, client: httpx.Client | None = None) -> list[dict[str, Any]]:
    own = client is None
    http = client or httpx.Client(timeout=15)
    try:
        r = http.get(API_BASE + LIST_USERS, headers={"X-API-Key": api_key})
        if r.status_code != 200:
            raise RuntimeError(_redact(f"HTTP {r.status_code}: {r.text[:200]}", api_key))
        return list((r.json() or {}).get("data") or [])
    finally:
        if own:
            http.close()


def sync_accounts(
    api_key: str, handles: Iterable[str], *, client: httpx.Client | None = None
) -> SyncResult:
    """Subscribe every configured handle not already monitored. Never unsubscribes: removing
    an account the owner added by hand on the provider's page is not this module's call."""
    want = {h.lstrip("@").lower(): h.lstrip("@") for h in handles if h}
    own = client is None
    http = client or httpx.Client(timeout=15)
    try:
        have = {str(u.get("x_user_screen_name") or "").lower() for u in monitored(api_key, client=http)}
        added, already, failed = [], [], []
        for low, handle in want.items():
            if low in have:
                already.append(handle)
                continue
            r = http.post(API_BASE + ADD_USER, headers={"X-API-Key": api_key},
                          json={"x_user_name": handle})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and body.get("status") == "success":
                added.append(handle)
            else:
                failed.append((handle, _redact(f"HTTP {r.status_code}: {body.get('msg') or r.text[:160]}",
                                               api_key)))
        unlisted = tuple(sorted(have - set(want)))
        return SyncResult(tuple(added), tuple(already), tuple(failed), unlisted)
    finally:
        if own:
            http.close()
