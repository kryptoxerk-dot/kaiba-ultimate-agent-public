"""Read-only X (Twitter) search for narrative evidence: who is posting a token, how early, how loud.

Why this exists (2026-10-05)
----------------------------

The owner asked for the agent to "doomscroll X" for narrative and upcoming launches. A review
of the usual repos found every free route broken or account-risking as of October 2026:

* nitter was archived after X's cease-and-desist (2026-08-24);
* snscrape has been dead since 2023;
* twikit and twitter-cli search return 404 since X's September rebuild (twitter-cli's fix is
  an unmerged PR);
* cookie scrapers get burner accounts flagged and locked.

The routes that work are paid APIs. This module speaks to them, in order of preference:

1. ``twitterapi.io`` (``TWITTERAPI_IO_KEY``). A key is already configured on the box.
   MEASURED 2026-10-05: one probe answered HTTP 402 "Credits is not enough", so the key is
   valid and only needs a top-up. Roughly $0.15 per 1,000 posts returned.
2. The official X API v2 recent search (``X_BEARER_TOKEN``). Pay-per-use, about $0.005 per
   post read.

Every call is READ-ONLY. There is no post, like, follow or DM path in this module, and none
must be added: a narrative reader that can also speak is a liability with the owner's name on
it.

What comes back is normalised into :class:`Post`. :func:`extract_refs` pulls cashtags and
contract addresses (Solana base58 mints, EVM ``0x`` addresses) out of the text, so a post can
be tied to a token we are scanning. What a post MEANS for a trade is not decided here. The
recorder (:mod:`kaiba.ingest.x_narrative`) stores the evidence, and the daily signal audit is
where it has to earn a place.

Cost discipline: :class:`Budget` caps queries per UTC day in ``kv``, and the caller passes the
cap. A query refused by the budget never reaches the network.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

TWITTERAPI_IO_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
X_API_URL = "https://api.x.com/2/tweets/search/recent"
TIMEOUT_S = 20.0
MAX_TEXT = 600

#: Solana mint: base58, 32-44 chars. pump.fun mints end in "pump", but that is not required.
_SOL_ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_EVM_ADDR = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9_]{1,14})\b")


@dataclass(frozen=True)
class Post:
    post_id: str
    created_ms: int | None
    author: str | None
    author_followers: int | None
    author_verified: bool | None
    text: str
    likes: int | None
    reposts: int | None
    replies: int | None
    views: int | None
    url: str | None


@dataclass
class SearchResult:
    ok: bool
    backend: str | None
    posts: list[Post] = field(default_factory=list)
    reason: str | None = None


def extract_refs(text: str) -> dict[str, list[str]]:
    """Cashtags and contract addresses in ``text``. Order kept, duplicates dropped."""
    def uniq(xs: Sequence[str]) -> list[str]:
        seen: dict[str, None] = {}
        for x in xs:
            seen.setdefault(x, None)
        return list(seen)

    evm = uniq([m.lower() for m in _EVM_ADDR.findall(text or "")])
    sol = uniq([m for m in _SOL_ADDR.findall(text or "") if not m.startswith("0x")])
    tags = uniq([m.upper() for m in _CASHTAG.findall(text or "")])
    return {"cashtags": tags, "sol": sol, "evm": evm}


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _parse_time(v: Any) -> int | None:
    """twitterapi.io uses Twitter's legacy format; X API v2 uses ISO 8601."""
    if not v:
        return None
    s = str(v)
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(_dt.datetime.strptime(s.replace("Z", "+0000"), fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _from_twitterapi_io(t: Mapping[str, Any]) -> Post:
    a = t.get("author") or {}
    return Post(
        post_id=str(t.get("id") or ""),
        created_ms=_parse_time(t.get("createdAt")),
        author=a.get("userName") or a.get("username"),
        author_followers=_int(a.get("followers") or a.get("followersCount")),
        author_verified=(bool(a.get("isBlueVerified")) if "isBlueVerified" in a else None),
        text=str(t.get("text") or "")[:MAX_TEXT],
        likes=_int(t.get("likeCount")),
        reposts=_int(t.get("retweetCount")),
        replies=_int(t.get("replyCount")),
        views=_int(t.get("viewCount")),
        url=t.get("url") or t.get("twitterUrl"),
    )


def _from_x_api(t: Mapping[str, Any], users: Mapping[str, Mapping[str, Any]]) -> Post:
    u = users.get(str(t.get("author_id")), {})
    m = t.get("public_metrics") or {}
    um = u.get("public_metrics") or {}
    handle = u.get("username")
    return Post(
        post_id=str(t.get("id") or ""),
        created_ms=_parse_time(t.get("created_at")),
        author=handle,
        author_followers=_int(um.get("followers_count")),
        author_verified=(bool(u.get("verified")) if "verified" in u else None),
        text=str(t.get("text") or "")[:MAX_TEXT],
        likes=_int(m.get("like_count")),
        reposts=_int(m.get("retweet_count")),
        replies=_int(m.get("reply_count")),
        views=_int(m.get("impression_count")),
        url=(f"https://x.com/{handle}/status/{t.get('id')}" if handle else None),
    )


class Budget:
    """Queries per UTC day, counted in ``kv``. A refused query never reaches the network."""

    def __init__(self, conn: sqlite3.Connection | None, *, daily_cap: int, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.daily_cap = int(daily_cap)
        self.clock = clock

    def _key(self) -> str:
        return "x:queries:" + time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def used(self) -> int:
        if self.conn is None:
            return 0
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (self._key(),)).fetchone()
        return _int(row[0]) or 0 if row else 0

    def take(self) -> bool:
        if self.conn is None:
            return True
        if self.used() >= self.daily_cap:
            return False
        self.conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?, '1', ?) ON CONFLICT(key) DO UPDATE "
            "SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT), updated_ms = excluded.updated_ms",
            (self._key(), int(self.clock() * 1000)),
        )
        return True


def backend_available(env: Mapping[str, str]) -> str | None:
    """Which backend would be used, or ``None``. Names only, never values."""
    if env.get("twitterapi_io_key"):
        return "twitterapi_io"
    if env.get("x_bearer_token"):
        return "x_api"
    return None


def search(
    query: str,
    *,
    env: Mapping[str, str],
    budget: Budget | None = None,
    latest: bool = True,
    max_posts: int = 20,
    client: httpx.Client | None = None,
) -> SearchResult:
    """One search, newest first. ``env`` holds lower-case credential names (Settings fields)."""
    q = (query or "").strip()
    if not q:
        return SearchResult(False, None, reason="empty query")
    backend = backend_available(env)
    if backend is None:
        return SearchResult(False, None, reason="no X credential configured "
                            "(TWITTERAPI_IO_KEY or X_BEARER_TOKEN in ~/.config/kaiba/.env)")
    if budget is not None and not budget.take():
        return SearchResult(False, backend, reason=f"daily X query cap {budget.daily_cap} reached")
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT_S)
    try:
        if backend == "twitterapi_io":
            r = http.get(
                TWITTERAPI_IO_URL,
                params={"query": q, "queryType": "Latest" if latest else "Top"},
                headers={"X-API-Key": env["twitterapi_io_key"]},
            )
            if r.status_code != 200:
                return SearchResult(False, backend, reason=_http_reason(r))
            data = r.json()
            posts = [_from_twitterapi_io(t) for t in (data.get("tweets") or [])][:max_posts]
            return SearchResult(True, backend, posts)
        r = http.get(
            X_API_URL,
            params={
                "query": q, "max_results": max(10, min(100, max_posts)),
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id", "user.fields": "username,public_metrics,verified",
                **({"sort_order": "recency"} if latest else {}),
            },
            headers={"Authorization": f"Bearer {env['x_bearer_token']}"},
        )
        if r.status_code != 200:
            return SearchResult(False, backend, reason=_http_reason(r))
        data = r.json()
        users = {str(u.get("id")): u for u in ((data.get("includes") or {}).get("users") or [])}
        posts = [_from_x_api(t, users) for t in (data.get("data") or [])][:max_posts]
        return SearchResult(True, backend, posts)
    except (httpx.HTTPError, ValueError) as exc:
        return SearchResult(False, backend, reason=f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if own:
            http.close()


def _http_reason(r: httpx.Response) -> str:
    """Status plus the provider's own message; never the request (it carries the key)."""
    msg = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            msg = str(body.get("message") or body.get("detail") or body.get("title") or body.get("error") or "")
    except ValueError:
        msg = r.text[:120]
    return f"http {r.status_code}: {msg}"[:200]


def credentials(settings: Any) -> dict[str, str]:
    """The two credential values this module may use, read from :class:`Settings`."""
    return {
        "twitterapi_io_key": str(getattr(settings, "twitterapi_io_key", "") or ""),
        "x_bearer_token": str(getattr(settings, "x_bearer_token", "") or ""),
    }
