"""Which X accounts the market launches tokens from -- a running league table.

Owner, 2026-10-07: "get more accounts to follow, check tokens from tweets and see what accounts
they launched it from". Every pump.fun launch carries a metadata JSON (``tokens.meta_json.uri``);
tweet-launch tools (J7Tracker, Uxento) put the source post in its ``twitter`` or ``website``
field as ``x.com/<handle>/status/<id>``. Reading that for every new launch gives, per author:
how many of their posts get launched on, how many tokens each draws, and -- joined to
graduation and price extent later -- whether launches off that account ever pay.

MEASURED baseline (docs/research/tweet-launch-outcomes-20261006.md, 4 h census, 7,801
launches): 1,911 tweet-launches on 887 posts from 511 authors, 1.5% graduated. The most-
launched authors graduated nothing (felixonchain 0/87, bencookss 0/67); the first token on a
post graduated 3.9% vs 1.3% for later ones. Ranking by launch COUNT therefore picks the wrong
accounts -- :func:`board` ranks by first-launch graduations and keeps counts beside them.

Gateways (MEASURED from the box by that study): the launch tool's own host first, then
``pump.mypinata.cloud``, ``4everland.io``, ``ipfs.filebase.io``; ``ipfs.io``, ``dweb.link``,
``w3s.link``, ``nftstorage.link`` answered 403 and ``gateway.pinata.cloud`` 429.

DB read rule (AGENTS.md): every query is a short, rowid-bounded batch on its own connection;
no transaction is held across a network fetch.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from kaiba.core.db import connect, fetch_all, fetch_one, jload, tx

log = logging.getLogger(__name__)

GATEWAYS = ("https://pump.mypinata.cloud/ipfs/", "https://4everland.io/ipfs/", "https://ipfs.filebase.io/ipfs/")
BLOCKED_HOSTS = ("ipfs.io", "dweb.link", "w3s.link", "gateway.pinata.cloud", "nftstorage.link")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
      "Accept": "application/json,*/*"}
_CID = re.compile(r"(?:/ipfs/|^)((?:Qm[1-9A-HJ-NP-Za-km-z]{44}|baf[a-z2-7]{50,}))")
_STATUS = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,25})", re.I)
_PROFILE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?(?:\?|$)", re.I)
WATERMARK = "tweet_sources:rowid"


def candidate_urls(uri: str) -> list[str]:
    m = _CID.search(uri or "")
    if m:
        urls = [g + m.group(1) for g in GATEWAYS]
        if uri.startswith("http") and not any(b in uri for b in BLOCKED_HOSTS) and "pump.mypinata" not in uri:
            urls.insert(0, uri)
        return urls[:4]
    return [uri] if (uri or "").startswith("http") else []


def classify(meta: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """(kind, author, tweet_id): ``tweet`` when a field links a post, ``profile`` for a bare
    account link, else ``none``. ``x.com/i/status/<id>`` keeps author ``i`` (unknown)."""
    fields = [str(meta.get(k) or "") for k in ("twitter", "website")]
    for f in fields:
        m = _STATUS.search(f)
        if m:
            return "tweet", m.group(1).lower(), m.group(2)
    for f in fields:
        m = _PROFILE.search(f)
        if m and m.group(1).lower() not in {"i", "home", "intent", "search"}:
            return "profile", m.group(1).lower(), None
    return "none", None, None


def fetch_meta(uri: str, client: httpx.Client, timeout_s: float = 6.0) -> tuple[dict[str, Any] | None, str]:
    err = "no_url"
    for u in candidate_urls(uri):
        try:
            r = client.get(u, headers=UA, timeout=timeout_s, follow_redirects=True)
            if r.status_code != 200:
                err = f"http_{r.status_code}"
                continue
            j = json.loads(r.content[:200_000].decode("utf-8", "replace"))
            if isinstance(j, dict):
                return j, u.split("/")[2]
            err = "not_a_dict"
        except Exception as exc:  # noqa: BLE001 - next gateway
            err = type(exc).__name__
    return None, err


def pending(conn: Any, after_rowid: int, limit: int) -> list[dict[str, Any]]:
    """New launches with a metadata uri: sol (``uri``) and EVM Flap/Pons (``meta_uri``)."""
    rows = fetch_all(conn,
        "SELECT rowid AS rid, chain, address, symbol, name, created_ms, launchpad, meta_json FROM tokens "
        "WHERE rowid > ? ORDER BY rowid LIMIT ?", (after_rowid, limit))
    out = []
    for r in rows:
        meta = jload(r["meta_json"], {}) or {}
        uri = meta.get("uri") or meta.get("meta_uri")
        out.append({**r, "uri": uri if isinstance(uri, str) else None})
    return out


def record(conn: Any, tok: dict[str, Any], meta: dict[str, Any] | None, via: str) -> None:
    kind, author, tid = classify(meta) if meta else ("unfetched", None, None)
    with tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO tweet_sources (chain, token, kind, author, tweet_id, token_created_ms, "
            "fetched_ms, via, name, symbol, launchpad) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tok["chain"], tok["address"], kind, author, tid, tok["created_ms"], int(time.time() * 1000),
             via[:60], (meta or {}).get("name") or tok.get("name"), (meta or {}).get("symbol") or tok.get("symbol"),
             tok.get("launchpad")))


def run_once(*, batch: int = 200, workers: int = 4, client: httpx.Client | None = None,
             fetch: Any = None) -> int:
    """One batch past the watermark. Each DB touch opens and closes its own connection."""
    c = connect()
    try:
        wm = fetch_one(c, "SELECT value FROM kv WHERE key = ?", (WATERMARK,))
        if wm is None:
            # First start: follow from NOW, and persist that at once. Recomputing MAX(rowid) on
            # every run (the first version) meant the follower never saw a single new launch.
            start = int((fetch_one(c, "SELECT MAX(rowid) AS m FROM tokens") or {"m": 0})["m"] or 0)
            _set_watermark(c, start)
            return 0
        toks = pending(c, int(wm["value"]), batch)
    finally:
        c.close()
    if not toks:
        return 0
    want = [t for t in toks if t["uri"]]
    own = client is None
    http = client or httpx.Client()
    fetcher = fetch or (lambda uri: fetch_meta(uri, http))
    try:
        with ThreadPoolExecutor(max(1, workers)) as ex:
            got = list(ex.map(lambda t: fetcher(t["uri"]), want))
    finally:
        if own:
            http.close()
    c = connect()
    try:
        for t, (meta, via) in zip(want, got, strict=True):
            record(c, t, meta, via)
        _set_watermark(c, int(toks[-1]["rid"]))
    finally:
        c.close()
    return len(want)


def _set_watermark(c: Any, rowid: int) -> None:
    with tx(c):
        c.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                  "SET value = excluded.value, updated_ms = excluded.updated_ms",
                  (WATERMARK, str(rowid), int(time.time() * 1000)))


def board(conn: Any, *, since_ms: int, min_posts: int = 3, top: int = 40) -> list[dict[str, Any]]:
    """Per author: posts launched on, tokens, graduations, and graduations of the FIRST token
    per post (the only launch with a measured edge). Ranked by first-launch graduations, then
    posts. Graduation = ``tokens.migrated_ms`` or a ``graduation_observations`` row."""
    rows = fetch_all(conn,
        "SELECT s.author, s.tweet_id, s.chain, s.token, s.token_created_ms, "
        "(t.migrated_ms IS NOT NULL OR EXISTS (SELECT 1 FROM graduation_observations g "
        " WHERE g.chain = s.chain AND g.token = s.token)) AS grad "
        "FROM tweet_sources s LEFT JOIN tokens t ON t.chain = s.chain AND t.address = s.token "
        "WHERE s.kind = 'tweet' AND s.author IS NOT NULL AND s.author != 'i' AND s.token_created_ms >= ?",
        (since_ms,))
    first: dict[str, tuple[int, str]] = {}
    for r in rows:
        k = r["tweet_id"]
        if k not in first or (r["token_created_ms"] or 0) < first[k][0]:
            first[k] = (r["token_created_ms"] or 0, r["token"])
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"tokens": 0, "posts": set(), "grad": 0, "first_grad": 0,
                                                          "chains": set()})
    for r in rows:
        a = agg[r["author"]]
        a["tokens"] += 1
        a["posts"].add(r["tweet_id"])
        a["grad"] += int(bool(r["grad"]))
        a["chains"].add(r["chain"])
        if first[r["tweet_id"]][1] == r["token"]:
            a["first_grad"] += int(bool(r["grad"]))
    out = [{"author": k, "posts": len(v["posts"]), "tokens": v["tokens"], "graduated": v["grad"],
            "first_graduated": v["first_grad"],
            "first_grad_rate": round(v["first_grad"] / len(v["posts"]), 4) if v["posts"] else 0.0,
            "chains": sorted(v["chains"])}
           for k, v in agg.items() if len(v["posts"]) >= min_posts]
    out.sort(key=lambda d: (-d["first_graduated"], -d["posts"], -d["graduated"]))
    return out[:top]


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.ingest.tweet_sources")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="follow new launches forever")
    r.add_argument("--interval-s", type=float, default=20.0)
    b = sub.add_parser("board", help="print the author league table")
    b.add_argument("--days", type=float, default=7.0)
    b.add_argument("--min-posts", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.cmd == "board":
        c = connect()
        try:
            for row in board(c, since_ms=int(time.time() * 1000 - a.days * 86_400_000), min_posts=a.min_posts):
                print(json.dumps(row))
        finally:
            c.close()
        return 0
    while True:
        try:
            n = run_once()
            if n:
                log.info("tweet-sources: %d launches read", n)
        except Exception:  # noqa: BLE001 - a bad batch must not stop the follower
            log.exception("tweet-sources batch")
            n = 0
        if not n:
            time.sleep(a.interval_s)


if __name__ == "__main__":
    raise SystemExit(_cli())
