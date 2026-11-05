"""Early J7 research events/revisions, separate from terminal financial dedupe."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping

from kaiba.ingest.j7_launch_bridge import KINDS, images, merge_nonempty, timestamp_ms
from kaiba.ingest.x_stream import XPost


class SocialEvents:
    def __init__(self, *, capacity: int = 5000, sink=None):
        if capacity < 1:
            raise ValueError("positive_capacity_required")
        self.capacity = capacity
        self.entries = OrderedDict()
        if sink is None:
            from kaiba.ingest.social_dispatch import offer

            sink = offer
        self.sink = sink

    def ingest(self, event: str, payload, *, now_ms: int):
        if event == "initialTweets":
            for row in payload if isinstance(payload, list) else []:
                if isinstance(row, Mapping) and isinstance(row.get("id"), str):
                    self._save(row["id"], {"terminal": True})
            return False
        if event not in {"tweet", "tweet_update", "tweet_deleted"} or not isinstance(payload, Mapping):
            return False
        tid = payload.get("id")
        if not isinstance(tid, str) or not tid.isdigit() or not 15 <= len(tid) <= 22:
            return False
        old = self.entries.get(tid, {})
        if event == "tweet_deleted":
            self._save(tid, {"terminal": True})
            return False
        if old.get("terminal"):
            return False
        if payload.get("platform") not in {None, "X"} or payload.get("source") in {"external", "webhook"}:
            return False
        # Retain the documented context fields only, bounded by the local datagram packer.
        fresh = {
            k: v
            for k, v in payload.items()
            if k
            in {
                "id",
                "author",
                "text",
                "type",
                "createdAt",
                "media",
                "isReply",
                "isQuote",
                "isRetweet",
                "quotedTweet",
                "replyTo",
            }
        }
        merged = merge_nonempty(old.get("payload", {}), fresh)
        author = merged.get("author") or {}
        if not isinstance(author, Mapping):
            return False
        handle = str(author.get("handle") or "").lower()
        previous = (old.get("payload", {}).get("author") or {}).get("handle")
        if previous and previous.lower() != handle:
            self._save(tid, {"terminal": True})
            return False
        first_seen = old.get("first_seen_ms", now_ms)
        digest = hashlib.sha256(json.dumps(merged, sort_keys=True).encode()).hexdigest()
        self._save(tid, {"payload": merged, "first_seen_ms": first_seen, "digest": digest})
        created = timestamp_ms(merged.get("createdAt"))
        text = merged.get("text")
        if (
            not handle
            or created is None
            or not isinstance(text, str)
            or not text.strip()
            or digest == old.get("digest")
        ):
            return False
        kind = KINDS.get(str(merged.get("type") or "").upper())
        if kind is None:
            return False
        post = XPost(
            tid,
            handle,
            text[:12000],
            kind,
            images(merged),
            created,
            first_seen,
            None,
            "j7tracker",
            raw=merged,
        )
        return bool(self.sink(post))

    def _save(self, tid, state):
        self.entries[tid] = state
        self.entries.move_to_end(tid)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
