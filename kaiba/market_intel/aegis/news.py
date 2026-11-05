"""News wire: crypto, markets/macro and central-bank/regulator feeds merged into one tagged stream.

Collector ``news`` (every 5 min). Payload::

    {"items": [{"id", "title", "link", "source", "group", "published", "ts", "summary", "tags", "high"}],
     "feeds": [{"name", "group", "ok", "error", "count"}],
     "counts": {"crypto": n, "markets": n, "policy": n}, "high_24h": n}

``group`` is crypto | markets | policy. ``tags`` come from fixed keyword rules (no model, no
sentiment guess). ``high`` marks the tags that usually move crypto: FOMC/rates, CPI, jobs, ETF,
SEC/regulation, hacks, tariffs. Every item is also appended to ``news_archive.jsonl`` (30 days),
which ``search_archive`` reads for the terminal's news search.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from .base import Collector, Http, cache_dir, parse_when, safe_xml, strip_html, utc_iso

FEEDS: list[tuple[str, str, str]] = [
    ("CoinDesk", "crypto", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "crypto", "https://cointelegraph.com/rss"),
    ("The Block", "crypto", "https://www.theblock.co/rss.xml"),
    ("Decrypt", "crypto", "https://decrypt.co/feed"),
    ("Google News: crypto", "crypto",
     ("https://news.google.com/rss/search?q=bitcoin+OR+ethereum+OR+crypto+when:1d&hl=en-US&gl=US&ceid=US:en")),
    ("Bloomberg Markets", "markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("WSJ Markets", "markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"),
    ("FT Markets", "markets", "https://www.ft.com/markets?format=rss"),
    ("CNBC Top News", "markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy", "markets", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch", "markets", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Google News: macro", "markets",
     ("https://news.google.com/rss/search?q=%22Federal+Reserve%22+OR+inflation+OR+%22Treasury+yields%22+when:1d"
      "&hl=en-US&gl=US&ceid=US:en")),
    ("Federal Reserve", "policy", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Fed speeches", "policy", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ("ECB", "policy", "https://www.ecb.europa.eu/rss/press.html"),
    ("SEC", "policy", "https://www.sec.gov/news/pressreleases.rss"),
]

TAGS: list[tuple[str, str]] = [
    ("FOMC", (r"\bFOMC\b|\bpowell\b|rate (cut|hike)s?|interest rates?|monetary policy|\bdot plot\b"
              r"|\bfed (chair|governor|officials?|minutes|decision|meeting)\b")),
    ("FED", r"\bthe fed\b|federal reserve"),
    ("CPI", r"\bCPI\b|\binflation\b|\bPCE\b|consumer prices"),
    ("JOBS", r"\bpayrolls?\b|\bNFP\b|jobless|unemployment|\bjobs report\b"),
    ("ETF", r"\bETFs?\b|\bETPs?\b|\bIBIT\b|\bFBTC\b|\bGBTC\b|spot (bitcoin|ether) fund"),
    ("REG", r"\bSEC\b|\bCFTC\b|regulat|lawsuit|\bcourt\b|\bbill\b|congress|senate|\bMiCA\b"),
    ("HACK", r"\bhack(ed|er|s)?\b|exploit|drain(ed)?\b|stolen|breach"),
    ("TARIFF", r"\btariffs?\b|trade war|sanction"),
    ("YIELDS", r"\byields?\b|treasur(y|ies)\b|\bbonds?\b"),
    ("BTC", r"\bbitcoin\b|\bBTC\b"),
    ("ETH", r"\bether(eum)?\b|\bETH\b"),
    ("STABLE", r"stablecoin|\bUSDT\b|\bUSDC\b|tether"),
    ("LIQ", r"liquidat|short squeeze|long squeeze|open interest|funding rate"),
    ("WHALE", r"\bwhales?\b|large transfer|smart money"),
    ("MSTR", r"\bstrategy inc\b|microstrategy|\bsaylor\b|\bMSTR\b"),
]
HIGH = {"FOMC", "CPI", "JOBS", "ETF", "REG", "HACK", "TARIFF"}
_TAG_RE = [(t, re.compile(p, re.IGNORECASE)) for t, p in TAGS]
ARCHIVE_DAYS = 30
MAX_ITEMS = 400


def tag(text: str) -> list[str]:
    return [t for t, rx in _TAG_RE if rx.search(text)]


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return "" if el is None else "".join(el.itertext()).strip()


def parse_feed(xml_text: str, source: str, group: str) -> list[dict[str, Any]]:
    """RSS 2.0 or Atom to items. Google News titles end in ' - Publisher'; the publisher becomes the source."""
    root = ET.fromstring(safe_xml(xml_text).encode("utf-8") if isinstance(xml_text, str) else xml_text)
    nodes = [e for e in root.iter() if _local(e) in ("item", "entry")]
    out = []
    for n in nodes:
        title = strip_html(_text(_child(n, "title")))
        link_el = _child(n, "link")
        link = (link_el.get("href") or _text(link_el)) if link_el is not None else ""
        when = parse_when(_text(_child(n, "pubdate", "published", "updated", "date")))
        summary = strip_html(_text(_child(n, "description", "summary", "content")), 280)
        src = source
        pub = _child(n, "source")
        if source.startswith("Google News"):
            if pub is not None and _text(pub):
                src = _text(pub)
            m = re.match(r"(.*) - ([^-]{2,60})$", title)
            if m:
                title, src = m.group(1).strip(), (src if pub is not None and _text(pub) else m.group(2).strip())
            summary = ""                                       # Google's description repeats the title
        if not title or not link:
            continue
        tags = tag(f"{title} {summary}")
        out.append({"id": hashlib.sha1(link.encode()).hexdigest()[:16], "title": title, "link": link,
                    "source": src, "group": group, "published": utc_iso(when),
                    "ts": when.timestamp() if when else None, "summary": summary, "tags": tags,
                    "high": bool(HIGH.intersection(tags))})
    return out


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:90]


#: KAIBA DEVIATION 1 (2026-11-03, MARKET-INTEL-NEWS-CLOCK-CLAUDE-20261103; see SOURCE.md). Aegis's
#: merge rewrote an item dated more than 10 min ahead to ``ts=now, published=now``, fabricating its
#: publication clock, so a future-dated Fed/CPI headline read as current news. Such an item is now
#: QUARANTINED with its original clocks (``split_future``), never re-dated, archived or counted.
FUTURE_SLACK_S = 600
MAX_FUTURE_ITEMS = 50


def split_future(items: list[dict[str, Any]], now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(current, future_dated)``: an item more than FUTURE_SLACK_S ahead of ``now`` keeps its original
    ``ts``/``published`` and goes to the second list (a bad feed clock or an embargoed item)."""
    current, future = [], []
    for it in items:
        (future if it["ts"] is not None and it["ts"] > now + FUTURE_SLACK_S else current).append(it)
    return current, future


def merge(items: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Newest first, one item per normalized title (the earliest feed in FEEDS order wins), capped at
    MAX_ITEMS. A future-dated item is left out, never re-dated (deviation 1); collect() quarantines it."""
    seen: set[str] = set()
    out = []
    for it in split_future(items, now)[0]:
        key = _norm(it["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: -(x["ts"] or 0))
    return out[:MAX_ITEMS]


def _archive(items: list[dict[str, Any]], now: float) -> None:
    path = cache_dir() / "news_archive.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    keep, seen = [], set()
    cutoff = now - ARCHIVE_DAYS * 86400
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(ln)
            except ValueError:
                continue
            if (row.get("ts") or now) >= cutoff and row.get("id") not in seen:
                seen.add(row["id"])
                keep.append(row)
    new = [it for it in items if it["id"] not in seen]
    rows = keep + new
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def search_archive(q: str = "", tag_filter: str = "", limit: int = 200) -> list[dict[str, Any]]:
    path = cache_dir() / "news_archive.jsonl"
    if not path.exists():
        return []
    words = [w for w in q.lower().split() if w]
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        hay = f"{r.get('title', '')} {r.get('summary', '')} {r.get('source', '')}".lower()
        if all(w in hay for w in words) and (not tag_filter or tag_filter in r.get("tags", [])):
            rows.append(r)
    rows.sort(key=lambda x: -(x.get("ts") or 0))
    return rows[:limit]


def collect(http: Http, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    feeds, items = [], []
    for name, group, url in FEEDS:
        try:
            got = parse_feed(http.get_text(url), name, group)
            items += got
            feeds.append({"name": name, "group": group, "ok": True, "error": None, "count": len(got)})
        except Exception as e:  # noqa: BLE001 - one dead feed must not blank the wire
            feeds.append({"name": name, "group": group, "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}",
                          "count": 0})
    if not any(f["ok"] for f in feeds):
        raise RuntimeError("every news feed failed")
    current, future = split_future(items, now)
    merged = merge(current, now)
    _archive(merged, now)
    counts: dict[str, int] = {}
    for it in merged:
        counts[it["group"]] = counts.get(it["group"], 0) + 1
    high = sum(1 for it in merged if it["high"] and (it["ts"] or 0) >= now - 86400)
    future.sort(key=lambda x: x["ts"])
    return {"items": merged, "feeds": feeds, "counts": counts, "high_24h": high,
            "future_dated": future[:MAX_FUTURE_ITEMS], "future_dated_count": len(future)}


COLLECTORS = [Collector("news", "News wire", 300, collect, tuple(u for _, _, u in FEEDS))]
