"""Record what X says about every token our lanes signal on, at the moment they signal.

The question the owner asked: does a token with real narrative (people posting its contract
address, accounts with reach, posts going up fast) do better than one with none? It cannot be
answered by reading X after the fact, because X after a pump is full of posts about the pump.
So this records X AT signal time, per token, into ``x_token_obs``. The daily signal audit then
reads only observations made at or before each signal (``x_posts_pre`` and
``x_followers_pre``), and they have to hold on both halves like every other feature.

Search key: the contract address, not the cashtag. ``$PEPE`` matches a hundred tokens; a mint
address matches one.

Cost: one query per token per :data:`COOLDOWN_MS`, at most ``max_tokens`` per run, and a daily
query cap (:class:`kaiba.providers.x_search.Budget`). A 401/402 (no credit, bad key) parks the
recorder for :data:`PARK_MS` instead of burning the cap on refusals. MEASURED 2026-10-05: the
configured twitterapi.io key answers 402 "Credits is not enough" until it is topped up.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from kaiba.providers import x_search

log = logging.getLogger(__name__)

COOLDOWN_MS = 6 * 3600_000
PARK_MS = 3600_000
PARK_KEY = "x:parked_until_ms"
SIGNAL_WINDOW_MS = 10 * 60_000


def record_posts(conn: sqlite3.Connection, query: str, result: x_search.SearchResult, *, now_ms: int) -> int:
    """Insert each returned post once. Returns how many were new."""
    new = 0
    for p in result.posts:
        if not p.post_id:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO x_posts (post_id, first_seen_ms, query, backend, author, "
            "author_followers, author_verified, created_ms, text, likes, reposts, replies, views, "
            "url, refs_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.post_id, now_ms, query[:200], result.backend, p.author, p.author_followers,
             None if p.author_verified is None else int(p.author_verified), p.created_ms,
             p.text, p.likes, p.reposts, p.replies, p.views, p.url,
             json.dumps(x_search.extract_refs(p.text))),
        )
        new += cur.rowcount or 0
    return new


def summarise(posts: Sequence[x_search.Post], *, now_ms: int) -> dict[str, Any]:
    """The per-token features. Posts dated in the future of ``now_ms`` are ignored."""
    seen = [p for p in posts if p.created_ms is None or p.created_ms <= now_ms]
    authors = {p.author for p in seen if p.author}
    dated = [p.created_ms for p in seen if p.created_ms is not None]
    return {
        "n_posts": len(seen),
        "n_authors": len(authors),
        "max_followers": max((p.author_followers or 0 for p in seen), default=0),
        "sum_likes": sum(p.likes or 0 for p in seen),
        "sum_views": sum(p.views or 0 for p in seen),
        "earliest_ms": min(dated) if dated else None,
        "posts_1h": sum(1 for t in dated if now_ms - t <= 3600_000),
    }


def parked(conn: sqlite3.Connection, now_ms: int) -> int | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (PARK_KEY,)).fetchone()
    try:
        until = int(row[0]) if row else 0
    except (TypeError, ValueError):
        until = 0
    return until if until > now_ms else None


def _park(conn: sqlite3.Connection, now_ms: int, reason: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_ms=excluded.updated_ms",
        (PARK_KEY, str(now_ms + PARK_MS), now_ms),
    )
    log.warning("x_narrative parked for %d min: %s", PARK_MS // 60_000, reason)


def candidates(
    conn: sqlite3.Connection, *, lanes: Sequence[str], chains: Sequence[str], now_ms: int,
    max_tokens: int,
) -> list[tuple[str, str]]:
    """Tokens signalled in the last :data:`SIGNAL_WINDOW_MS` and not observed within the cooldown."""
    lm, cm = ",".join("?" * len(lanes)), ",".join("?" * len(chains))
    rows = conn.execute(
        f"SELECT chain, token, MAX(created_ms) AS t FROM signals "
        f"WHERE rowid > (SELECT COALESCE(MAX(rowid), 0) - 20000 FROM signals) "
        f"AND created_ms > ? AND lane IN ({lm}) AND chain IN ({cm}) "
        f"GROUP BY chain, token ORDER BY t DESC LIMIT ?",
        (now_ms - SIGNAL_WINDOW_MS, *lanes, *chains, max_tokens * 4),
    ).fetchall()
    out: list[tuple[str, str]] = []
    for chain, token, _t in rows:
        recent = conn.execute(
            "SELECT 1 FROM x_token_obs WHERE chain=? AND token=? AND observed_ms > ? LIMIT 1",
            (chain, token, now_ms - COOLDOWN_MS),
        ).fetchone()
        if not recent:
            out.append((chain, token))
        if len(out) >= max_tokens:
            break
    return out


def observe(
    conn: sqlite3.Connection,
    *,
    env: Mapping[str, str],
    now_ms: int,
    lanes: Sequence[str] = ("sm-trenches", "launch-snipe"),
    chains: Sequence[str] = ("sol", "robinhood", "bsc"),
    max_tokens: int = 8,
    daily_cap: int = 400,
    client: Any = None,
) -> dict[str, Any]:
    """One recorder pass. Returns counts; never raises on a provider failure."""
    if x_search.backend_available(env) is None:
        return {"status": "no_credential"}
    until = parked(conn, now_ms)
    if until:
        return {"status": "parked", "until_ms": until}
    budget = x_search.Budget(conn, daily_cap=daily_cap, clock=lambda: now_ms / 1000)
    done = failed = new_posts = 0
    for chain, token in candidates(conn, lanes=lanes, chains=chains, now_ms=now_ms, max_tokens=max_tokens):
        res = x_search.search(token, env=env, budget=budget, latest=True, client=client)
        if not res.ok:
            reason = res.reason or "unknown"
            if "cap" in reason:
                return {"status": "budget", "observed": done, "failed": failed, "new_posts": new_posts}
            conn.execute(
                "INSERT OR IGNORE INTO x_token_obs (chain, token, observed_ms, backend, ok, reason) "
                "VALUES (?,?,?,?,0,?)", (chain, token, now_ms, res.backend, reason[:200]),
            )
            failed += 1
            if reason.startswith(("http 401", "http 402", "http 403")):
                _park(conn, now_ms, reason)
                return {"status": "parked", "reason": reason, "observed": done, "failed": failed}
            continue
        new_posts += record_posts(conn, token, res, now_ms=now_ms)
        s = summarise(res.posts, now_ms=now_ms)
        conn.execute(
            "INSERT OR IGNORE INTO x_token_obs (chain, token, observed_ms, backend, ok, reason, "
            "n_posts, n_authors, max_followers, sum_likes, sum_views, earliest_ms, posts_1h) "
            "VALUES (?,?,?,?,1,NULL,?,?,?,?,?,?,?)",
            (chain, token, now_ms, res.backend, s["n_posts"], s["n_authors"], s["max_followers"],
             s["sum_likes"], s["sum_views"], s["earliest_ms"], s["posts_1h"]),
        )
        done += 1
    return {"status": "ok", "observed": done, "failed": failed, "new_posts": new_posts,
            "queries_today": budget.used()}
