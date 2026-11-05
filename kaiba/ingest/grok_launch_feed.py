"""Grok launch cards -> the tweet launcher's posts (lead, 2026-11-02).

Owner, 2026-11-02: "auto launching ... using grok bot doom scrolling make sure the feature work".

The Grok Bot scans X on its own schedule (public native X search, every 15 min on 2026-11-02) and
sends signed cards to Codex's intake (``kaiba/hunters/grok_alpha_bridge.py``). A card on the
``launch`` track names ONE post Grok thinks a token could be launched on. Its first source is that
post: URL, ``published_at`` and the exact ``text``; ``state.media`` may list its image URLs and
``state.post_kind`` its kind. Contract: ``docs/runbooks/grok-launch-cards.md``.

This module reads those cards READ-ONLY from the intake store and turns each into the same
:class:`XPost` the X stream yields, so the launcher's picker, logo, plan, record and (only if
armed) send are the same code. Nothing here writes, leases or reviews a card; a repeat is dropped
by the launcher's own ``tweet_launch_tweets`` key.

What a Grok post is not: fast. MEASURED 2026-11-02 on the real intake (88 cards, 215 dated
sources): cited posts were a median 125 min old at receipt (p10 11 min, min 1.3 min). The
2026-10-06 census found only the FIRST launch on a post has an edge, median 23 s after it. The
launcher therefore gives Grok posts their own age limit and their own mode (``grok:`` in
``config/tweet_launch.yaml``): shadow unless the owner arms that source too.

Text, kind and media are sender-reported (Grok's reading of the post), not verified against X
here. Media URLs are kept only on X's own image host over HTTPS, so a card cannot point the logo
fetcher at an arbitrary address.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kaiba.ingest.x_stream import XPost

TRACK = "launch"
#: The post must name its author: ``/i/web/status/<id>`` links cannot be attributed to an account.
POST_URL = re.compile(r"https://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{5,25})/?")
MEDIA_HOSTS = frozenset({"pbs.twimg.com"})
MAX_MEDIA = 4
KINDS = frozenset({"post", "quote", "reply", "repost"})
BACKENDS = frozenset({"grok_bot_x_search", "grok_bot_browser", "manual_export"})
#: Cards read per poll; the intake holds at most a few thousand revisions in all.
PAGE_ROWS = 50

#: (received_ms, observation key, revision digest) of the last card read.
Cursor = tuple[int, str, str]


def store_path() -> Path:
    from kaiba.hunters.grok_alpha_processor import INTAKE_NAME, grok_dir

    return grok_dir() / INTAKE_NAME


def start_cursor(since_ms: int) -> Cursor:
    return (since_ms, "", "")


def _ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return int(parsed.timestamp() * 1000)


def _media(state: dict[str, Any]) -> tuple[str, ...]:
    out = []
    for url in state.get("media") or []:
        if not isinstance(url, str) or len(url) > 512:
            continue
        parts = urlsplit(url)
        if (parts.scheme == "https" and parts.hostname in MEDIA_HOSTS and parts.username is None
                and parts.password is None and parts.port in (None, 443)):
            out.append(url)
        if len(out) == MAX_MEDIA:
            break
    return tuple(out)


def to_post(card: dict[str, Any], *, received_ms: int, observation_id: str, revision: str) -> tuple[XPost | None, str]:
    """(the post, "ok") or (None, why it cannot be one). Pure."""
    if card.get("track") != TRACK:
        return None, "not_launch_track"
    sources = card.get("sources") or []
    first = sources[0] if sources and isinstance(sources[0], dict) else {}
    match = POST_URL.fullmatch(str(first.get("url") or ""))
    if match is None:
        return None, "first_source_not_an_attributable_post"
    created = _ms(first.get("published_at"))
    if created is None:
        return None, "publication_time_missing"
    text = first.get("text")
    if text is not None and not isinstance(text, str):
        return None, "text_not_a_string"
    state = card.get("state") if isinstance(card.get("state"), dict) else {}
    kind = state.get("post_kind") if state.get("post_kind") in KINDS else "post"
    backend = (card.get("provenance") or {}).get("backend")
    display = state.get("display_name")
    return XPost(
        tweet_id=match[2], author=match[1].lower(), text=text or "", kind=kind, media=_media(state),
        created_ms=created, received_ms=received_ms, feed_delay_ms=received_ms - created,
        backend=backend if backend in BACKENDS else "grok_bot_x_search",
        display_name=display[:80] if isinstance(display, str) else None,
        raw={"grok": {"observation_id": observation_id, "revision": revision,
                      "observed_at": card.get("observed_at"), "project": str(card.get("project") or "")[:160],
                      "claim": str(card.get("claim") or "")[:500], "text_source": "sender_reported"}},
    ), "ok"


def read_new(path: Path, after: Cursor, *, limit: int = PAGE_ROWS) -> tuple[list[tuple[XPost | None, str]], Cursor]:
    """Launch-track revisions received after ``after``, oldest first, and the new cursor.

    One short read-only connection (the intake's own bounded opener). A missing store is an
    empty read: the intake creates it on its first card.
    """
    from kaiba.hunters.grok_alpha_bridge import IntakeRefused, Store

    try:
        store = Store(path, readonly=True)
    except IntakeRefused:
        return [], after
    with store.connection() as con:
        rows = con.execute(
            "SELECT r.key, r.digest, r.received_ms, r.payload FROM revisions r JOIN observations o ON o.key = r.key "
            "WHERE o.track = ? AND (r.received_ms, r.key, r.digest) > (?, ?, ?) "
            "ORDER BY r.received_ms, r.key, r.digest LIMIT ?",
            (TRACK, *after, limit)).fetchall()
    out: list[tuple[XPost | None, str]] = []
    for key, digest, received_ms, payload in rows:
        try:
            card = json.loads(payload)
        except (ValueError, TypeError):
            out.append((None, "payload_unreadable"))
            continue
        out.append(to_post(card, received_ms=int(received_ms), observation_id=key, revision=digest))
    return out, ((int(rows[-1][2]), rows[-1][0], rows[-1][1]) if rows else after)


def _cli(argv: list[str] | None = None) -> int:
    """Print the launch posts the intake holds (read-only), for an operator check."""
    import argparse
    import time

    ap = argparse.ArgumentParser(prog="python -m kaiba.ingest.grok_launch_feed")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--store", default=None)
    a = ap.parse_args(argv)
    since = int(time.time() * 1000) - int(a.hours * 3_600_000)
    try:
        items, _ = read_new(Path(a.store) if a.store else store_path(), start_cursor(since), limit=500)
    except sqlite3.Error as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1
    for post, why in items:
        print(json.dumps({"why": why} if post is None else
                         {"author": post.author, "post": post.url, "kind": post.kind, "media": len(post.media),
                          "age_at_receipt_s": None if post.feed_delay_ms is None else post.feed_delay_ms // 1000,
                          "text": post.text[:120]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
