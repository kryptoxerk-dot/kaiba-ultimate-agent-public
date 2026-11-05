"""Codex TL-ALPHA-RUNTIME: bounded research workers independent of launch submission.

No order submission. Explicit alpha chain hints override launch hints; measurement
also covers the watched launch roster. Ambiguous EVM chain identities are not guessed.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

log = logging.getLogger(__name__)


def measure(post, cfg, *, info=None, connect=None, now_ms=None):
    from kaiba.core.db import connect as db_connect
    from kaiba.core.schemas import Chain
    from kaiba.execution import tweet_refs

    if cfg.alpha is None or cfg.alpha.mode == "off":
        return []
    accounts = {**cfg.accounts, **cfg.alpha.accounts}
    hints = accounts.get(post.author)
    if not hints:
        return []
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if post.created_ms is None or not 0 <= now - post.created_ms <= cfg.alpha.max_post_age_s * 1000:
        log.info("tweet-alpha stale/unavailable post=%s", post.tweet_id)
        return []
    texts = [post.text]
    for key in ("quoted_tweet", "quotedTweet", "replyTo"):
        nested = post.raw.get(key)
        if isinstance(nested, dict) and nested.get("text"):
            text = str(nested["text"])[:12000]
            if text not in texts:
                texts.append(text)
    contextual = replace(post, text="\n".join(texts))
    if re.search(r"\b0x[0-9a-fA-F]{40}\b", contextual.text) and len(set(hints) - {Chain.SOL}) > 1:
        log.warning("tweet-alpha ambiguous EVM chain post=%s; needs explicit source hint", post.tweet_id)
        return []
    alpha = replace(cfg.alpha, accounts=accounts)
    if connect is None:
        from pathlib import Path

        from kaiba.core.config import get_settings
        wal = Path(str(get_settings().db_path) + "-wal")
        if wal.exists() and wal.stat().st_size > 1_500_000_000:
            log.warning("tweet-alpha deferred: WAL over 1.5 GB post=%s", post.tweet_id)
            return []
    # A fresh autocommit connection; handle_refs materializes SELECTs before HTTP
    # and uses short write transactions. No read cursor survives provider calls.
    c = (connect or db_connect)()
    try:
        rows = tweet_refs.handle_refs(c, contextual, alpha, cfg, info=info)
        log.info("tweet-alpha measured post=%s refs=%d queue_delay_ms=%d", post.tweet_id,
                 len(rows), max(0, now - post.received_ms))
        return rows
    finally:
        c.close()


class Dispatcher:
    """Two dedicated workers, at most 32 queued/running posts; offer never waits."""

    def __init__(self, *, worker=measure, workers=2, capacity=32):
        self._worker = worker
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tweet-alpha")
        self._slots = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._pending = set()
        self._closed = False

    def offer(self, post, cfg):
        if cfg.alpha is None or cfg.alpha.mode == "off":
            return False
        if post.author not in cfg.accounts and post.author not in cfg.alpha.accounts:
            return False
        with self._lock:
            if self._closed or post.tweet_id in self._pending:
                return False
            if not self._slots.acquire(blocking=False):
                log.warning("tweet-alpha queue full post=%s", post.tweet_id)
                return False
            self._pending.add(post.tweet_id)
            try:
                future = self._pool.submit(self._worker, post, cfg)
            except RuntimeError:
                self._pending.discard(post.tweet_id)
                self._slots.release()
                return False
        def finished(f):
            with self._lock:
                self._pending.discard(post.tweet_id)
                self._slots.release()
            if not f.cancelled() and f.exception() is not None:
                log.error("tweet-alpha failed post=%s error=%s", post.tweet_id,
                          type(f.exception()).__name__)
        future.add_done_callback(finished)
        return True

    def close(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def main():
    """Small read-only alpha inbox, bounded by rowid/limit and a three-second query."""
    import argparse
    import json
    import sqlite3
    from pathlib import Path

    from kaiba.core.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status"])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--chain", choices=["sol", "bsc", "robinhood"])
    args = parser.parse_args()
    path = get_settings().db_path
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size > 1_500_000_000:
        raise SystemExit("WAL_OVER_1.5GB_NO_READ")
    c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    deadline = time.monotonic() + 3
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        rows = c.execute(
            "SELECT ref_id,tweet_id,author,chain,token,resolve_note,seen_ms,entry_price_usd,"
            "entry_liquidity_usd,meta_json,marks_json,marks_done FROM tweet_refs "
            "WHERE rowid > (SELECT COALESCE(MAX(rowid),0)-500 FROM tweet_refs) "
            "AND (? IS NULL OR chain=?) ORDER BY rowid DESC LIMIT ?",
            (args.chain, args.chain, max(1, min(args.limit, 50))),
        ).fetchall()
    finally:
        c.close()
    print(json.dumps({"financial_execution": False, "sample_limit": 500,
                      "refs": [dict(r) for r in rows]}))


if __name__ == "__main__":
    main()
