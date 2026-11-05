"""Tokens named in watched posts: early alpha (buy what they post) and vamps (clone it).

Owner, 2026-10-07: "Using the same tweet skill get early alpha from there" and "the agent can
vamp launches out of tweets". Both start from the same event -- a watched account's post that
names a token by contract address or $CASHTAG -- and share the launcher's single X socket
(one connection per twitterapi.io key), so they run inside the ``tweet-launch`` service.

* **Alpha** -- resolve the token, read its price the moment the post reached us
  (GMGN ``token info``), then again at fixed horizons. That is the measurement a buy-on-post
  rule has to beat: the forward return from the moment WE could have bought, not from the
  post. Nothing here buys. A live mode would hand the token to the engine as a signal under
  every gate; it is not wired, because there is no evidence yet that it pays (the repo's
  copy-trading study found callers' tokens already bought by the time a follower fills,
  memory: profitable-wallet-playbook-1005).
* **Vamp** -- the j7/uxento sense: clone an existing token's metadata (name, ticker, logo,
  links) into our own launch. Planned, recorded with the exact ``cooking create`` argv,
  never sent. MEASURED prior (arXiv 2609.10246, docs/research/tweet-launch-tracked-accounts-
  20261006.md): copycats graduate 0.86% vs 9.20% for originals and a 1.02% baseline; our own
  4-hour census found every launch after the first on a tweet negative. Cross-chain vamps
  (a sol token cloned on BNB / Robinhood) are unmeasured, which is why the default target
  chains exclude the source chain.

Rate: GMGN ``token info`` is ``Priority.RESEARCH`` (behind exits and entries) and bounded by
``alpha.max_refs_per_post`` and the horizons, a handful of calls per post.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, jdump, jload, tx
from kaiba.core.schemas import Chain
from kaiba.ingest.x_stream import XPost

log = logging.getLogger(__name__)

_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
QUOTE_SYMBOLS = frozenset({"SOL", "WSOL", "ETH", "WETH", "BNB", "WBNB", "USDT", "USDC", "USD1", "BTC"})


@dataclass(frozen=True)
class AlphaConfig:
    mode: str = "shadow"
    accounts: dict[str, tuple[Chain, ...]] = field(default_factory=dict)   # handle -> chain hints
    horizons_s: tuple[int, ...] = (60, 300, 900, 3600)
    max_refs_per_post: int = 3
    #: A post older than this when it reaches us is not "early": a new rule replays history
    #: (MEASURED 2026-10-07: the first rule's opening batch was 0.7-11 hours old), and pricing
    #: an old post now would record a fake entry. Such posts are skipped, not measured.
    max_post_age_s: int = 120
    cashtag_lookback_s: int = 86_400
    vamp_mode: str = "shadow"
    vamp_targets: tuple[Chain, ...] = (Chain.BSC, Chain.ROBINHOOD, Chain.SOL)
    vamp_same_chain: bool = False


def parse_alpha_config(raw: dict[str, Any]) -> AlphaConfig:
    a = raw.get("alpha") or {}
    v = raw.get("vamp") or {}
    accounts = {}
    for row in a.get("accounts") or []:
        accounts[str(row["handle"]).lstrip("@").lower()] = tuple(Chain(c) for c in row.get("chains") or ["sol"])
    return AlphaConfig(
        mode=str(a.get("mode", "shadow")), accounts=accounts,
        horizons_s=tuple(int(x) for x in a.get("horizons_s") or (60, 300, 900, 3600)),
        max_refs_per_post=int(a.get("max_refs_per_post", 3)),
        max_post_age_s=int(a.get("max_post_age_s", 120)),
        cashtag_lookback_s=int(a.get("cashtag_lookback_s", 86_400)),
        vamp_mode=str(v.get("mode", "shadow")),
        vamp_targets=tuple(Chain(c) for c in v.get("targets") or ("bsc", "robinhood", "sol")),
        vamp_same_chain=bool(v.get("same_chain", False)),
    )


@dataclass(frozen=True)
class Ref:
    kind: str            # address | cashtag
    raw: str
    chain: Chain | None
    token: str | None
    note: str = ""


def extract_refs(post: XPost, hints: tuple[Chain, ...], conn: sqlite3.Connection | None,
                 cfg: AlphaConfig) -> list[Ref]:
    """Contract addresses first (exact), then cashtags resolved against recent launches."""
    from kaiba.ingest.telegram_calls import extract_addresses

    evm_hint = next((c for c in hints if c is not Chain.SOL), Chain.BSC)
    out: list[Ref] = []
    seen = set()
    for chain, addr in extract_addresses(post.text or "", evm_chain=evm_hint):
        if addr in seen:
            continue
        seen.add(addr)
        out.append(Ref("address", addr, chain, addr))
    for m in _CASHTAG.finditer(post.text or ""):
        sym = m.group(1).upper()
        if sym in QUOTE_SYMBOLS or sym in seen:
            continue
        seen.add(sym)
        out.append(resolve_cashtag(sym, hints, conn, cfg, post))
    return out[: cfg.max_refs_per_post]


def resolve_cashtag(sym: str, hints: tuple[Chain, ...], conn: sqlite3.Connection | None,
                    cfg: AlphaConfig, post: XPost) -> Ref:
    """A ticker means a token only when exactly one recent launch on a hinted chain carries it.
    Two candidates is ambiguous and is recorded as such -- never guessed."""
    if conn is None:
        return Ref("cashtag", sym, None, None, "no_db")
    since = (post.created_ms or post.received_ms) - cfg.cashtag_lookback_s * 1000
    marks = ",".join("?" for _ in hints) or "''"
    rows = fetch_all(conn,
        f"SELECT DISTINCT chain, address FROM tokens WHERE created_ms >= ? AND chain IN ({marks}) "
        "AND UPPER(symbol) = ? LIMIT 5", (since, *[c.value for c in hints], sym))
    if len(rows) == 1:
        return Ref("cashtag", sym, Chain(rows[0]["chain"]), rows[0]["address"], "tokens_table")
    return Ref("cashtag", sym, None, None, "ambiguous" if rows else "unresolved")


# --------------------------------------------------------------------------------------
# price + metadata (GMGN token info)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    name: str | None
    symbol: str | None
    logo: str | None
    twitter: str | None
    launchpad: str | None
    holders: int | None
    note: str = "ok"


def _d(v: Any) -> Decimal | None:
    try:
        return None if v in (None, "") else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def snapshot(chain: Chain, token: str, info: Any = None) -> Snapshot:
    from kaiba.core.limiter import Priority

    if info is None:
        from kaiba.providers import gmgn_cli
        info = gmgn_cli.token_info
    try:
        r = info(token, chain, priority=Priority.RESEARCH)
    except Exception as exc:  # noqa: BLE001
        return Snapshot(None, None, None, None, None, None, None, None, f"error:{type(exc).__name__}")
    if not getattr(r, "ok", False) or not isinstance(getattr(r, "data", None), dict):
        return Snapshot(None, None, None, None, None, None, None, None, f"unavailable:{getattr(r, 'reason', '')}"[:80])
    d = r.data
    price = d.get("price")
    price = price.get("price") if isinstance(price, dict) else price
    link = d.get("link") or {}
    tw = link.get("twitter_username") if isinstance(link, dict) else None
    return Snapshot(
        price_usd=_d(price), liquidity_usd=_d(d.get("liquidity")),
        name=d.get("name"), symbol=d.get("symbol"), logo=d.get("logo"),
        twitter=f"https://x.com/{tw}" if tw else None,
        launchpad=d.get("launchpad"), holders=d.get("holder_count"),
    )


# --------------------------------------------------------------------------------------
# record, mark, vamp plan
# --------------------------------------------------------------------------------------


def handle_refs(conn: sqlite3.Connection, post: XPost, cfg: AlphaConfig, launch_cfg: Any, *,
                info: Any = None) -> list[dict[str, Any]]:
    """Record every token a watched post names, with its price at our first look."""
    hints = cfg.accounts.get(post.author)
    if not hints:
        return []
    age = post.age_ms
    if age is None or age < 0 or age > cfg.max_post_age_s * 1000:
        log.info("tweet-ref skip %s %s: post age %s ms", post.author, post.tweet_id, age)
        return []
    rows = []
    for ref in extract_refs(post, hints, conn, cfg):
        ref_id = f"tr:{post.tweet_id}:{ref.raw}"
        snap = snapshot(ref.chain, ref.token, info) if (ref.chain and ref.token) else None
        seen_ms = int(time.time() * 1000)
        vamp = plan_vamp(post, ref, snap, cfg, launch_cfg) if snap and snap.name and snap.symbol else None
        row = {
            "ref_id": ref_id, "tweet_id": post.tweet_id, "author": post.author, "kind": ref.kind,
            "raw": ref.raw, "chain": ref.chain.value if ref.chain else None, "token": ref.token,
            "resolve_note": ref.note, "post_created_ms": post.created_ms, "seen_ms": seen_ms,
            "entry_price_usd": None if not snap or snap.price_usd is None else str(snap.price_usd),
            "entry_liquidity_usd": None if not snap or snap.liquidity_usd is None else str(snap.liquidity_usd),
            "meta_json": jdump({} if not snap else {"name": snap.name, "symbol": snap.symbol, "logo": snap.logo,
                                                    "twitter": snap.twitter, "launchpad": snap.launchpad,
                                                    "holders": snap.holders, "note": snap.note}),
            "alpha_mode": cfg.mode, "vamp_json": jdump(vamp or {}),
        }
        with tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO tweet_refs (ref_id, tweet_id, author, kind, raw, chain, token, "
                "resolve_note, post_created_ms, seen_ms, entry_price_usd, entry_liquidity_usd, meta_json, "
                "alpha_mode, vamp_json) VALUES (:ref_id, :tweet_id, :author, :kind, :raw, :chain, :token, "
                ":resolve_note, :post_created_ms, :seen_ms, :entry_price_usd, :entry_liquidity_usd, "
                ":meta_json, :alpha_mode, :vamp_json)", row)
        log.info("tweet-ref %s %s %s %s price=%s vamp=%s", post.author, ref.kind, ref.raw,
                 ref.chain.value if ref.chain else "?", row["entry_price_usd"], bool(vamp))
        rows.append(row)
    return rows


def plan_vamp(post: XPost, ref: Ref, snap: Snapshot, cfg: AlphaConfig, launch_cfg: Any) -> dict[str, Any]:
    """Our clone of the named token on each target chain: the exact argv, recorded, not sent."""
    from kaiba.execution import tweet_launch as tl

    plans = {}
    for chain in cfg.vamp_targets:
        if chain is ref.chain and not cfg.vamp_same_chain:
            continue
        route = launch_cfg.chains.get(chain)
        if route is None:
            continue
        amt, share, basis = tl.dev_buy_native(route, launch_cfg.dev_buy_supply_pct)
        p = tl.Plan(
            launch_id=f"vamp:{post.tweet_id}:{ref.raw}:{chain.value}", tweet_id=post.tweet_id,
            author=post.author, chain=chain, dex=route.dex, mode=cfg.vamp_mode, verdict="plan",
            reasons=[f"vamp_of:{ref.chain.value if ref.chain else '?'}:{ref.token}"], score=0.0,
            name=(snap.name or "")[:32], symbol=tl._symbolize(snap.symbol or "")[:10],
            image_source="clone", image_url=snap.logo, buy_amt_native=amt, supply_pct=share,
            buy_basis=basis,
        )
        if amt is None or not p.symbol:
            plans[chain.value] = {"refused": basis if amt is None else "no_symbol"}
            continue
        argv = tl.build_argv(p, post, launch_cfg, "<chain-wallet>")
        if snap.twitter and "--twitter" in argv:
            argv[argv.index("--twitter") + 1] = snap.twitter
        plans[chain.value] = {"name": p.name, "symbol": p.symbol, "buy_amt_native": str(amt),
                              "argv": tl.recordable_argv(argv)}
    return plans


def mark_due(conn: sqlite3.Connection, cfg: AlphaConfig, *, now_ms: int | None = None,
             info: Any = None, limit: int = 20) -> int:
    """Read the price again for every ref whose next horizon has passed."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    longest = max(cfg.horizons_s) * 1000
    rows = fetch_all(conn,
        "SELECT ref_id, chain, token, seen_ms, marks_json FROM tweet_refs WHERE token IS NOT NULL "
        "AND marks_done = 0 AND seen_ms >= ? ORDER BY seen_ms LIMIT ?", (now - longest - 3_600_000, limit))
    n = 0
    for r in rows:
        marks = jload(r["marks_json"], {}) or {}
        due = [h for h in cfg.horizons_s if str(h) not in marks and now >= r["seen_ms"] + h * 1000]
        if not due:
            continue
        snap = snapshot(Chain(r["chain"]), r["token"], info)
        for h in due:
            marks[str(h)] = {"price_usd": None if snap.price_usd is None else str(snap.price_usd),
                             "liquidity_usd": None if snap.liquidity_usd is None else str(snap.liquidity_usd),
                             "at_ms": now, "note": snap.note}
        done = int(all(str(h) in marks for h in cfg.horizons_s))
        with tx(conn):
            conn.execute("UPDATE tweet_refs SET marks_json = ?, marks_done = ? WHERE ref_id = ?",
                         (jdump(marks), done, r["ref_id"]))
        n += 1
    return n


def report(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per author: refs, resolved, and the mean forward return per horizon from our first look."""
    rows = fetch_all(conn, "SELECT author, entry_price_usd, marks_json FROM tweet_refs WHERE token IS NOT NULL")
    agg: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        e = _d(r["entry_price_usd"])
        if not e:
            continue
        for h, m in (jload(r["marks_json"], {}) or {}).items():
            p = _d((m or {}).get("price_usd"))
            if p is not None:
                agg.setdefault(r["author"], {}).setdefault(h, []).append(float(p / e - 1))
    out = []
    for author, by_h in sorted(agg.items()):
        out.append({"author": author, **{f"ret_{h}s": (round(sum(v) / len(v), 4), len(v)) for h, v in sorted(by_h.items(), key=lambda kv: int(kv[0]))}})
    return out
