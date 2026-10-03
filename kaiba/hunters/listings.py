"""Listing / news hunter - and the latency measurement that justifies it.

A listing-pop lane only exists if we are early. "Early" is not a feeling, it is
``detected_ms - announced_ms``, so every item this module produces carries a measured
latency and every item that *cannot* carry one is marked unmeasurable rather than quietly
given a zero. If the latency numbers come back at forty seconds, the lane is a fantasy and
the honest thing is to see that in the data within a week instead of after a loss.

The second thing this module refuses to guess at is the contract address. A listing
announcement gives a ticker; a ticker is not an asset. Two tokens sharing a symbol is the
normal case on Solana, and buying the wrong one during a listing pump is the classic way
to hand money to someone who registered the name first. :func:`resolve_token` returns
``None`` and warns when a symbol is ambiguous, and the caller is expected to stand down.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, digest, now_ms
from kaiba.hunters.airdrops import (
    fetch_json,
    fetch_text,
    parse_iso_ms,
    parse_rfc822_ms,
    provider_error,
    record_source,
    strip_tags,
    telegram_posts,
)

log = logging.getLogger(__name__)

BWENEWS_URL = "https://t.me/s/BWEnews"
#: Free tier is delayed on purpose by the vendor; we measure exactly how delayed.
CRYPTOLISTING_URL = "https://api.cryptolisting.app/v1/listings?tier=free"
BINANCE_ANNOUNCEMENTS_URL = (
    "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    "?type=1&catalogId=48&pageNo=1&pageSize=20"
)
UPBIT_ANNOUNCEMENTS_URL = (
    "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade"
)
BITHUMB_ANNOUNCEMENTS_URL = "https://feed.bithumb.com/notice"

#: Words that make an announcement a listing rather than maintenance noise.
_LISTING_RE = re.compile(
    r"(will list|listing|lists |new listing|market support|상장|거래 지원|마켓 추가|perpetual|"
    r"spot trading|seed tag|launchpool)",
    re.I,
)
_DELISTING_RE = re.compile(r"(delist|상장 ?폐지|거래 ?종료|removal of)", re.I)
_TICKER_RE = re.compile(r"\(([A-Z0-9]{2,12})\)")
_BARE_TICKER_RE = re.compile(r"\$([A-Z0-9]{2,12})\b")


class ListingItem(BaseModel):
    """One detected listing. ``latency_ms is None`` means unmeasurable, never fast."""

    exchange: str
    symbol: str
    title: str = ""
    url: str | None = None
    chain: Chain | None = None
    token: str | None = None
    announced_ms: int | None = None
    detected_ms: int = Field(default_factory=now_ms)
    source: str = "unknown"
    delisting: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def latency_ms(self) -> int | None:
        if self.announced_ms is None:
            return None
        return self.detected_ms - self.announced_ms

    @property
    def measurable(self) -> bool:
        return self.announced_ms is not None

    @property
    def listing_id(self) -> str:
        return digest(
            {
                "e": self.exchange.lower(),
                "s": self.symbol.upper(),
                "a": self.announced_ms or self.title[:120],
            }
        )[:32]


def extract_symbol(title: str) -> str | None:
    """``"Binance Will List Ondo (ONDO)"`` -> ``ONDO``. Ambiguity here is not tolerable."""
    if not title:
        return None
    for match in _TICKER_RE.finditer(title):
        candidate = match.group(1)
        if candidate.upper() not in {"USDT", "USDC", "KRW", "BTC", "ETH", "BNB", "USD", "FDUSD"}:
            return candidate.upper()
    bare = _BARE_TICKER_RE.search(title)
    if bare:
        return bare.group(1).upper()
    return None


def is_listing(title: str) -> bool:
    return bool(_LISTING_RE.search(title or ""))


# ------------------------------------------------------------------------------- feeds


def bwenews_feed(
    raw: str | None = None, conn: sqlite3.Connection | None = None
) -> list[ListingItem]:
    """BWEnews' public Telegram preview - the fastest free listing wire we have found."""
    source = "bwenews"
    page = raw if raw is not None else fetch_text(source, "telegram.preview", BWENEWS_URL, conn=conn)
    if page is None:
        return []
    try:
        out: list[ListingItem] = []
        for post in telegram_posts(page):
            title = post.text.split("\n", 1)[0][:200]
            if not is_listing(post.text):
                continue
            symbol = extract_symbol(post.text)
            if not symbol:
                continue
            exchange = _exchange_from_text(post.text) or "unknown"
            out.append(
                ListingItem(
                    exchange=exchange,
                    symbol=symbol,
                    title=title,
                    url=f"https://t.me/{post.post}",
                    announced_ms=post.ts_ms,
                    source=source,
                    delisting=bool(_DELISTING_RE.search(post.text)),
                    payload={"post": post.post, "excerpt": post.text[:280]},
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001 - layout drift, not a crash
        provider_error(source, "telegram.preview", f"parse failed: {exc}", conn=conn)
        return []


_EXCHANGES = (
    "binance", "upbit", "bithumb", "coinbase", "okx", "bybit", "kraken", "bitget",
    "kucoin", "gate", "mexc", "htx",
)


def _exchange_from_text(text: str) -> str | None:
    low = (text or "").lower()
    for name in _EXCHANGES:
        if name in low:
            return name
    return None


def cryptolisting_feed(
    raw: Any | None = None, conn: sqlite3.Connection | None = None
) -> list[ListingItem]:
    """CryptoListing's free (delayed) tier. Kept precisely to measure the delay."""
    source = "cryptolisting"
    data = raw if raw is not None else fetch_json(source, "listings.feed", CRYPTOLISTING_URL, conn=conn)
    if data is None:
        return []
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        provider_error(source, "listings.feed", f"expected a list, got {type(rows).__name__}", conn=conn)
        return []
    out: list[ListingItem] = []
    try:
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or row.get("coin") or "").strip().upper()
            title = str(row.get("title") or row.get("event") or "")
            if not symbol:
                symbol = extract_symbol(title) or ""
            if not symbol:
                continue
            announced = (
                row.get("announced_at_ms")
                or parse_iso_ms(row.get("announced_at"))
                or parse_iso_ms(row.get("date"))
            )
            chain = _chain_from(row.get("chain"))
            out.append(
                ListingItem(
                    exchange=str(row.get("exchange") or "unknown").lower(),
                    symbol=symbol,
                    title=title,
                    url=row.get("url"),
                    chain=chain,
                    token=row.get("contract") or None,
                    announced_ms=int(announced) if isinstance(announced, int | float) else None,
                    source=source,
                    delisting=bool(_DELISTING_RE.search(title)),
                    payload={"raw": row},
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        provider_error(source, "listings.feed", f"parse failed: {exc}", conn=conn)
        return []


def _chain_from(value: Any) -> Chain | None:
    if not value:
        return None
    try:
        return Chain(str(value).lower())
    except ValueError:
        return {"solana": Chain.SOL, "ethereum": Chain.ETH, "binance-smart-chain": Chain.BSC}.get(
            str(value).lower()
        )


def _find_records(node: Any, key: str, depth: int = 0) -> list[dict[str, Any]]:
    """Largest list of dicts containing ``key`` anywhere in a JSON blob.

    Exchange CMS payloads reshuffle their envelopes; the row shape outlives the path.
    """
    if depth > 8:
        return []
    best: list[dict[str, Any]] = []
    if isinstance(node, list):
        rows = [r for r in node if isinstance(r, dict) and key in r]
        if len(rows) > len(best):
            best = rows
        for child in node:
            found = _find_records(child, key, depth + 1)
            if len(found) > len(best):
                best = found
    elif isinstance(node, dict):
        for child in node.values():
            found = _find_records(child, key, depth + 1)
            if len(found) > len(best):
                best = found
    return best


def parse_binance(data: Any) -> list[ListingItem]:
    items: list[ListingItem] = []
    for row in _find_records(data, "title"):
        title = str(row.get("title") or "")
        if not is_listing(title):
            continue
        symbol = extract_symbol(title)
        if not symbol:
            continue
        released = row.get("releaseDate") or row.get("publishDate")
        code = row.get("code") or row.get("id")
        items.append(
            ListingItem(
                exchange="binance",
                symbol=symbol,
                title=title,
                url=f"https://www.binance.com/en/support/announcement/{code}" if code else None,
                announced_ms=int(released) if isinstance(released, int | float) else None,
                source="binance",
                delisting=bool(_DELISTING_RE.search(title)),
                payload={"raw": row},
            )
        )
    return items


def parse_upbit(data: Any) -> list[ListingItem]:
    items: list[ListingItem] = []
    for row in _find_records(data, "title"):
        title = str(row.get("title") or "")
        if not is_listing(title):
            continue
        symbol = extract_symbol(title)
        if not symbol:
            continue
        announced = parse_iso_ms(row.get("listed_at") or row.get("created_at") or row.get("first_listed_at"))
        rid = row.get("id")
        items.append(
            ListingItem(
                exchange="upbit",
                symbol=symbol,
                title=title,
                url=f"https://upbit.com/service_center/notice?id={rid}" if rid else None,
                announced_ms=announced,
                source="upbit",
                delisting=bool(_DELISTING_RE.search(title)),
                payload={"raw": row},
            )
        )
    return items


def parse_bithumb(body: str) -> list[ListingItem]:
    """Bithumb publishes an RSS-ish feed; fall back to anchors if the XML does not parse."""
    items: list[ListingItem] = []
    try:
        root = ET.fromstring((body or "").strip())
    except ET.ParseError:
        root = None
    if root is not None:
        for item in root.iter("item"):
            title = strip_tags(item.findtext("title") or "")
            if not is_listing(title):
                continue
            symbol = extract_symbol(title)
            if not symbol:
                continue
            items.append(
                ListingItem(
                    exchange="bithumb",
                    symbol=symbol,
                    title=title,
                    url=(item.findtext("link") or "").strip() or None,
                    announced_ms=parse_rfc822_ms(item.findtext("pubDate"))
                    or parse_iso_ms(item.findtext("pubDate")),
                    source="bithumb",
                    delisting=bool(_DELISTING_RE.search(title)),
                    payload={"title": title},
                )
            )
        return items
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body or "", re.S):
        title = strip_tags(text)
        if not is_listing(title):
            continue
        symbol = extract_symbol(title)
        if not symbol:
            continue
        items.append(
            ListingItem(
                exchange="bithumb",
                symbol=symbol,
                title=title,
                url=href,
                announced_ms=None,  # the HTML fallback carries no timestamp: unmeasurable
                source="bithumb",
                delisting=bool(_DELISTING_RE.search(title)),
                payload={"title": title},
            )
        )
    return items


#: exchange -> (endpoint, url, parser, json?)
ANNOUNCEMENT_SOURCES: dict[str, tuple[str, str, Callable[[Any], list[ListingItem]], bool]] = {
    "binance": ("announcements.list", BINANCE_ANNOUNCEMENTS_URL, parse_binance, True),
    "upbit": ("announcements.list", UPBIT_ANNOUNCEMENTS_URL, parse_upbit, True),
    "bithumb": ("announcements.list", BITHUMB_ANNOUNCEMENTS_URL, parse_bithumb, False),
}

#: Pages the default sweep no longer requests, with the measurement that retired them.
#: Their parsers stay (a supplied ``raw`` still parses), so a working route -- a proxy, a
#: different host -- is one line to restore. Upbit and Bithumb listings still reach the
#: registry through the BWEnews relay, which recorded both exchanges in the same week.
DISABLED_ANNOUNCEMENT_SOURCES: dict[str, str] = {
    "upbit": "403 from the VPS address on every call (39/day on 2026-10-01); 200 from a "
             "residential address with the same request, so it is the IP, not the request",
    "bithumb": "403 Cloudflare challenge from the VPS (39/day) and from a residential address",
}


def exchange_announcements(
    raw: dict[str, Any] | None = None, conn: sqlite3.Connection | None = None
) -> list[ListingItem]:
    """Binance / Upbit / Bithumb announcement pages. One exchange failing costs the others nothing."""
    out: list[ListingItem] = []
    for name, (endpoint, url, parser, as_json) in ANNOUNCEMENT_SOURCES.items():
        if raw is not None and name not in raw:
            continue
        if raw is None and name in DISABLED_ANNOUNCEMENT_SOURCES:
            continue
        if raw is not None:
            body = raw[name]
        elif as_json:
            body = fetch_json(name, endpoint, url, conn=conn)
        else:
            body = fetch_text(name, endpoint, url, conn=conn)
        if body is None:
            continue
        try:
            out.extend(parser(body))
        except Exception as exc:  # noqa: BLE001 - one exchange's redesign is not an outage
            provider_error(name, endpoint, f"parse failed: {exc}", conn=conn)
    return out


# ---------------------------------------------------------------------------- recording


def record_listing(
    conn: sqlite3.Connection, item: ListingItem, resolve: bool = True
) -> bool:
    """Store one listing and emit ``ALPHA_LISTING``. Returns False if we had it already.

    The event carries the measured latency, or an explicit ``latency_measurable: false``.
    A missing number must look missing on the dashboard, because the entire case for this
    lane rests on these numbers.
    """
    token = item.token
    if resolve and token is None:
        token = resolve_token(item.symbol, item.chain, conn)
    row_exists = fetch_one(
        conn, "SELECT listing_id FROM listing_events WHERE listing_id=?", (item.listing_id,)
    )
    if row_exists:
        return False
    conn.execute(
        "INSERT OR IGNORE INTO listing_events "
        "(listing_id, exchange, symbol, title, url, chain, token, announced_ms, detected_ms, "
        " latency_ms, source, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.listing_id,
            item.exchange,
            item.symbol,
            item.title,
            item.url,
            item.chain.value if item.chain else None,
            token,
            item.announced_ms,
            item.detected_ms,
            item.latency_ms,
            item.source,
            jdump(item.payload),
        ),
    )
    emit(
        EventKind.ALPHA_LISTING,
        {
            "exchange": item.exchange,
            "symbol": item.symbol,
            "title": item.title,
            "url": item.url,
            "token": token,
            "announced_ms": item.announced_ms,
            "detected_ms": item.detected_ms,
            "latency_ms": item.latency_ms,
            "latency_measurable": item.measurable,
            "delisting": item.delisting,
            "source": item.source,
        },
        chain=item.chain,
        subject=token or item.symbol,
        level="warn" if item.delisting else "info",
        dedupe_key=f"listing:{item.listing_id}",
        conn=conn,
    )
    return True


#: ``cryptolisting`` left the default sweep on 2026-10-02: ``api.cryptolisting.app`` does
#: not resolve (ConnectError "Name or service not known", 39 times on 2026-10-01) and the
#: source had never once succeeded on the box (fail_streak 364, last_ok_ms NULL). Its
#: parser is kept for the fixture and for a vendor that comes back.
FEEDS: dict[str, Callable[..., list[ListingItem]]] = {
    "bwenews": bwenews_feed,
    "exchange_announcements": exchange_announcements,
}


def refresh(
    conn: sqlite3.Connection | None = None,
    feeds: dict[str, Callable[..., list[ListingItem]]] | None = None,
) -> int:
    """Poll every feed and record what is new. Returns the number of new listings."""
    c = conn or get_conn()
    new = 0
    for name, fn in (feeds or FEEDS).items():
        try:
            items = fn(conn=c) or []
        except Exception as exc:  # noqa: BLE001
            provider_error(name, "feed", f"{type(exc).__name__}: {exc}", conn=c)
            record_source(c, name, "listing", 0, error=f"{type(exc).__name__}: {exc}")
            continue
        record_source(c, name, "listing", len(items))
        for item in items:
            if record_listing(c, item):
                new += 1
    return new


# --------------------------------------------------------------------- symbol resolution


def resolve_token(
    symbol: str, chain: Chain | None = None, conn: sqlite3.Connection | None = None
) -> str | None:
    """Map a listing ticker to a contract address, or refuse.

    Refusing is the feature. Two tokens with one symbol is the normal case, and a wrong
    contract address during a listing pump is how people buy the scam clone at the top.
    """
    c = conn or get_conn()
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    sql = "SELECT DISTINCT chain, address FROM tokens WHERE UPPER(symbol)=?"
    params: list[Any] = [sym]
    if chain is not None:
        sql += " AND chain=?"
        params.append(chain.value)
    rows = fetch_all(c, sql, params)
    if not rows:
        log.info("listing %s: no known token for that symbol%s", sym, f" on {chain}" if chain else "")
        return None
    addresses = {r["address"] for r in rows}
    if len(addresses) > 1:
        emit(
            EventKind.SYSTEM,
            {
                "reason": "ambiguous_symbol",
                "symbol": sym,
                "chain": chain.value if chain else None,
                "candidates": sorted(addresses)[:10],
                "detail": "two or more tokens share this symbol; refusing to guess a contract",
            },
            level="warn",
            subject=sym,
            conn=c,
        )
        log.warning("listing %s: %d candidate contracts; refusing to guess", sym, len(addresses))
        return None
    return rows[0]["address"]


# -------------------------------------------------------------------------------- report


def latency_stats(conn: sqlite3.Connection | None = None, since_ms: int = 0) -> dict[str, Any]:
    """Per-source latency. ``unmeasurable`` is reported, not hidden in an average."""
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT source, latency_ms FROM listing_events WHERE detected_ms >= ?",
        (since_ms,),
    )
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        stat = by_source.setdefault(row["source"], {"n": 0, "measured": [], "unmeasurable": 0})
        stat["n"] += 1
        if row["latency_ms"] is None:
            stat["unmeasurable"] += 1
        else:
            stat["measured"].append(int(row["latency_ms"]))
    for stat in by_source.values():
        measured = sorted(stat.pop("measured"))
        stat["measured_n"] = len(measured)
        stat["median_ms"] = measured[len(measured) // 2] if measured else None
        stat["p90_ms"] = measured[int(len(measured) * 0.9)] if measured else None
        stat["best_ms"] = measured[0] if measured else None
    return by_source


def weekly_report(conn: sqlite3.Connection | None = None, limit: int = 20) -> str:
    """Latency first. If the median is minutes, say so and kill the lane."""
    c = conn or get_conn()
    stats = latency_stats(c)
    lines = [
        "# Listing hunter - weekly",
        "",
        "| Source | Items | Measured | Median latency | p90 | Best | Unmeasurable |",
        "|---|---|---|---|---|---|---|",
    ]
    if not stats:
        lines.append("| - | 0 | 0 | - | - | - | - |")
    for source, stat in sorted(stats.items()):
        lines.append(
            "| {s} | {n} | {m} | {med} | {p90} | {best} | {un} |".format(
                s=source,
                n=stat["n"],
                m=stat["measured_n"],
                med=f"{stat['median_ms'] / 1000:.1f}s" if stat["median_ms"] is not None else "-",
                p90=f"{stat['p90_ms'] / 1000:.1f}s" if stat["p90_ms"] is not None else "-",
                best=f"{stat['best_ms'] / 1000:.1f}s" if stat["best_ms"] is not None else "-",
                un=stat["unmeasurable"],
            )
        )
    rows = fetch_all(
        c,
        "SELECT * FROM listing_events ORDER BY detected_ms DESC LIMIT ?",
        (limit,),
    )
    lines += [
        "",
        "| Detected | Exchange | Symbol | Contract | Latency | Title |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lat = row["latency_ms"]
        lines.append(
            "| {d} | {e} | {s} | {c} | {l} | {t} |".format(
                d=row["detected_ms"],
                e=row["exchange"],
                s=row["symbol"],
                c=(row["token"] or "unresolved"),
                l=f"{lat / 1000:.1f}s" if lat is not None else "unmeasurable",
                t=str(row["title"] or "").replace("|", "/")[:70],
            )
        )
    unresolved = sum(1 for r in rows if not r["token"])
    if unresolved:
        lines += [
            "",
            f"{unresolved} of {len(rows)} listings have no resolved contract address. That is a "
            "refusal, not a gap: an ambiguous ticker during a listing pump is how people buy the "
            "clone.",
        ]
    return "\n".join(lines)


def recent_listings(conn: sqlite3.Connection | None = None, limit: int = 50) -> list[dict[str, Any]]:
    c = conn or get_conn()
    rows = fetch_all(c, "SELECT * FROM listing_events ORDER BY detected_ms DESC LIMIT ?", (limit,))
    for row in rows:
        row["payload"] = json.loads(row.pop("payload_json") or "{}")
    return rows


__all__ = [
    "ListingItem",
    "bwenews_feed",
    "cryptolisting_feed",
    "exchange_announcements",
    "extract_symbol",
    "latency_stats",
    "parse_binance",
    "parse_bithumb",
    "parse_upbit",
    "recent_listings",
    "record_listing",
    "refresh",
    "resolve_token",
    "weekly_report",
]
