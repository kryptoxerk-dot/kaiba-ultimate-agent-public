# Codex 2026-09-27: owner-requested repair integration; see docs/TASKS.md REPAIR-INTEGRATION-20260927.
"""Airdrop and points-programme hunter.

There is no clean machine-readable airdrop feed in 2026 (research 06), so this module
scrapes four sources that are free and public, merges them, and hands everything to
:mod:`kaiba.hunters.ev` to be priced. Four weak sources that agree are worth more than one
loud one, which is why corroboration raises ``source_count`` instead of creating rows.

Posture, straight from the research and fixed in code rather than in a prompt:

* **Single wallet, organic.** Multi-wallet farming is negative EV once >=20-address cluster
  cuts, retroactive cross-project blacklists, drainer risk and capital lockup are priced.
  :func:`build_plan` refuses to produce a multi-wallet plan at all; the scorer separately
  cuts the gross by 80% if one is handed to it.
* **Steps are recorded, not executed.** ``build_plan`` writes down what a human or the
  executor would do. Anything CRITICAL or HIGH - signing a message, claiming, KYC,
  bridging - carries ``requires_operator=True``. Blind-signing and claim pages are the two
  ways airdrop hunters actually lose money, and they are exactly the steps a bot should
  not take on its own.
* **A layout change must not be an outage.** Every collector returns ``[]`` plus a
  ``PROVIDER_ERROR`` event instead of raising, and records the miss in ``hunter_sources``
  so a source that quietly went to zero is visible.

Collector-supplied gas and time costs are *estimates* (marked ``cost_basis: estimated`` in
the evidence meta). They are deliberately not zero: pretending an opportunity is free is
how a negative-EV programme ends up at the top of a ranking.

Shared HTTP/HTML helpers live here rather than in a fifth module because the other two
hunters (``nft``, ``listings``) import them.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, upsert
from kaiba.core.events import emit
from kaiba.core.limiter import Priority, guarded
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.hunters.ev import (
    EvScore,
    HunterConfig,
    OpportunityEvidence,
    OpportunityKind,
    SybilRisk,
    dedupe,
    hunter_config,
    opportunity_id,
    rank,
    score_opportunity,
)

log = logging.getLogger(__name__)

# Collectors retain their list API. Capture errors in this invocation's context,
# not by racing an events-table query or sharing mutable process-global state.
_COLLECT_ERRORS: ContextVar[list[str] | None] = ContextVar("hunter_collect_errors", default=None)

AIRDROPS_IO_URL = "https://t.me/s/airdrops_io"
AIRDROPALERT_RSS_URL = "https://airdropalert.com/feed/rssfeed"
#: DefiLlama's airdrops page has no documented endpoint; it is the protocol list filtered
#: to tokenless protocols (``symbol == "-"``), which /protocols does expose.
DEFILLAMA_PROTOCOLS_URL = "https://api.llama.fi/protocols"
DEFILLAMA_MIN_TVL_USD = 1_000_000.0
#: Legacy JSON/table and current server-rendered programme cards are tolerated.
ALPHADROPS_URL = "https://alphadrops.net/points-programs"

USER_AGENT = "kaiba-hunter/1.0 (+https://github.com/kaiba; contact via operator)"
# Limits apply to decoded response bytes, not a slice of an already-buffered document.
# /protocols was 8.9MB at the adapter audit; JSON gets headroom, HTML keeps its old budget.
# Callers may select a smaller ceiling via max_body_bytes, never an unbounded read.
MAX_BODY_BYTES = 4_000_000
MAX_JSON_BODY_BYTES = 16_000_000
HTTP_CHUNK_BYTES = 64_000


# ------------------------------------------------------------------- shared http helpers


def provider_error(
    provider: str,
    endpoint: str,
    detail: str,
    conn: sqlite3.Connection | None = None,
    **extra: Any,
) -> None:
    log.warning("hunter provider error %s %s: %s", provider, endpoint, detail)
    errors = _COLLECT_ERRORS.get()
    if errors is not None:
        errors.append(f"{provider} {endpoint}: {detail}"[:500])
    emit(
        EventKind.PROVIDER_ERROR,
        {"provider": provider, "endpoint": endpoint, "detail": detail[:500], **extra},
        level="warn",
        conn=conn,
    )


def fetch_text(
    provider: str,
    endpoint: str,
    url: str,
    *,
    priority: Priority = Priority.RESEARCH,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> str | None:
    """GET a complete bounded body through the limiter; overflow is an explicit failure.

    The ceiling is enforced while streaming decoded bytes, even without Content-Length
    or with compression. A partial document is never returned to a downstream parser.
    """
    hdrs = {"user-agent": USER_AGENT, **(headers or {})}
    received = 0
    status: int | None = None
    try:
        if not isinstance(max_body_bytes, int) or isinstance(max_body_bytes, bool) or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer")
        with guarded(provider, endpoint, priority, conn=conn):
            with httpx.stream("GET", url, timeout=timeout, headers=hdrs, follow_redirects=True) as resp:
                status = resp.status_code
                resp.raise_for_status()
                body = bytearray()
                for chunk in resp.iter_bytes(chunk_size=HTTP_CHUNK_BYTES):
                    received += len(chunk)
                    if received > max_body_bytes:
                        raise ValueError(
                            f"response body exceeds {max_body_bytes} decoded bytes (received {received})"
                        )
                    body.extend(chunk)
                return body.decode(resp.encoding or "utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - a provider being down is not our exception
        provider_error(
            provider, endpoint, f"{type(exc).__name__}: {exc}", conn=conn, url=url,
            http_status=status, received_body_bytes=received, max_body_bytes=max_body_bytes,
        )
        return None


def fetch_json(
    provider: str,
    endpoint: str,
    url: str,
    *,
    priority: Priority = Priority.RESEARCH,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
    conn: sqlite3.Connection | None = None,
    max_body_bytes: int = MAX_JSON_BODY_BYTES,
) -> Any | None:
    raw = fetch_text(
        provider, endpoint, url, priority=priority, timeout=timeout, headers=headers, conn=conn,
        max_body_bytes=max_body_bytes,
    )
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        provider_error(provider, endpoint, f"invalid json: {exc}", conn=conn, url=url)
        return None


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def strip_tags(fragment: str) -> str:
    text = _BR_RE.sub("\n", fragment or "")
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    text = re.sub("[ \t ]+", " ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


_TG_MESSAGE_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"[^>]*data-post="(?P<post>[^"]+)"(?P<body>.*?)'
    r'(?=<div class="tgme_widget_message[^"]*"[^>]*data-post="|\Z)',
    re.S,
)
_TG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<text>.*?)</div>', re.S
)
_TG_TIME_RE = re.compile(r'<time[^>]*datetime="(?P<dt>[^"]+)"')
_HREF_RE = re.compile(r'href="(?P<href>https?://[^"]+)"')


class TelegramPost(BaseModel):
    post: str
    text: str
    html: str
    ts_ms: int | None = None
    links: list[str] = Field(default_factory=list)


def parse_iso_ms(value: str | None) -> int | None:
    """ISO-8601 (with or without ``Z``) to epoch ms. ``None`` when unparseable."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def telegram_posts(page_html: str) -> list[TelegramPost]:
    """Parse a ``t.me/s/<channel>`` web preview. Shared with the listings hunter.

    Telegram's preview markup is stable-ish but not a contract; anything that does not
    match simply is not a post, so a layout change yields an empty list, not an exception.
    """
    out: list[TelegramPost] = []
    for match in _TG_MESSAGE_RE.finditer(page_html or ""):
        body = match.group("body")
        text_match = _TG_TEXT_RE.search(body)
        if not text_match:
            continue
        fragment = text_match.group("text")
        text = strip_tags(fragment)
        if not text:
            continue
        time_match = _TG_TIME_RE.search(body)
        out.append(
            TelegramPost(
                post=match.group("post"),
                text=text,
                html=fragment,
                ts_ms=parse_iso_ms(time_match.group("dt") if time_match else None),
                links=[
                    h
                    for h in _HREF_RE.findall(fragment)
                    if "t.me/" not in h and "telegram.me/" not in h
                ],
            )
        )
    return out


# ------------------------------------------------------------------ text classification

_CONFIRMED_RE = re.compile(
    r"\b(token confirmed|confirmed token|tge confirmed|airdrop confirmed|token is live|"
    r"tge (?:date|on|is)|snapshot taken|claim (?:is )?(?:now )?(?:live|open))\b",
    re.I,
)
_CONFIRMATION_DOUBT_RE = re.compile(
    r"\b(no|not|never|unconfirmed|unverified|denied|false|fake|rumou?rs?|speculative|"
    r"may|might|could|whether|if|pending|unlikely)\b|n['’]t\b",
    re.I,
)
_POINTS_RE = re.compile(r"(points? (?:program|programme|campaign)|season \d|\bxp\b|\bpoints\b)", re.I)
_SYBIL_HARD_RE = re.compile(r"(sybil|anti-?bot|kyc|verification|human passport|gitcoin)", re.I)
_PROGRAMME_RE = re.compile(
    r"\b(airdrops?|points?|rewards?|quests?|claims?|snapshot|tge|testnet|season\s+\d|"
    r"token confirmed|confirmed token)\b", re.I,
)
_NON_PROGRAMME_TITLE_RE = re.compile(
    r"\b(pinned|compilations?|round[- ]?up|recap|(?:market|price)\s+(?:update|news|analysis))\b|"
    r"^(photo|video|image|gm)$", re.I,
)
_MEDIA_PATH_RE = re.compile(r"\.(jpe?g|png|gif|webp|avif|svg|bmp|ico|mp4|webm|mov|mp3|wav)$", re.I)


def _programme_url(url: str | None) -> str | None:
    """Keep web discovery links, never media attachments or unsafe URL schemes.

    Passing this filter does not attest that a URL is official, safe to sign, or live.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(html_lib.unescape(url))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        if _MEDIA_PATH_RE.search(unquote(parts.path).rstrip("/")):
            return None
        if parts.hostname.lower() in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            return None
    except ValueError:
        return None
    return html_lib.unescape(url)


def _has_confirmation(text: str) -> bool:
    """A positive phrase is a source claim, never independent programme verification.

    Negation/uncertainty in its clause vetoes the match. Separate sentences about KYC
    must not erase a positive claim; questions must not become promises.
    """
    return any(
        _CONFIRMED_RE.search(clause)
        and not _CONFIRMATION_DOUBT_RE.search(clause)
        and "?" not in clause
        for clause in re.findall(r"[^.!?;\n]+[.!?;\n]?", text or "")
    )

_CHAIN_WORDS: dict[str, Chain] = {
    "solana": Chain.SOL,
    "sol": Chain.SOL,
    "ethereum": Chain.ETH,
    "eth": Chain.ETH,
    "mainnet": Chain.ETH,
    "base": Chain.BASE,
    "bsc": Chain.BSC,
    "bnb": Chain.BSC,
    "binance smart chain": Chain.BSC,
}

_ROBINHOOD_CHAIN_RE = re.compile(r"\brobinhood[\s-]+chain\b")

#: Chains we cannot trade but can still farm; kept as a hint string, not a fake Chain.
_KNOWN_OTHER_CHAINS = (
    "arbitrum", "optimism", "zksync", "linea", "scroll", "starknet", "monad", "megaeth",
    "abstract", "berachain", "sei", "sui", "aptos", "hyperliquid", "polygon", "blast",
)

#: Estimated gas for one organic interaction, USD. Never zero - free is a lie.
_GAS_ESTIMATE_USD: dict[str | None, Decimal] = {
    Chain.SOL.value: Decimal("0.50"),
    Chain.ETH.value: Decimal("12.00"),
    Chain.BASE.value: Decimal("0.80"),
    Chain.BSC.value: Decimal("0.60"),
    # Robinhood Chain is an Arbitrum Orbit L2 with ETH gas. Before 2026-10-02 it had no
    # entry here, so gas_estimate(Chain.ROBINHOOD) raised KeyError -- latent only because
    # detect_chain never returned it. The OpenSea NFT source does.
    Chain.ROBINHOOD.value: Decimal("0.10"),
    None: Decimal("3.00"),
}


def detect_chain(text: str) -> tuple[Chain | None, str | None]:
    """(tradeable chain, hint). A chain we cannot trade still matters for gas and plans."""
    low = (text or "").lower()
    # A funded chain is checked first: Robinhood Chain is an Arbitrum Orbit chain, so its
    # announcements routinely mention Arbitrum too. Only the two-word name counts --
    # "investment from Robinhood Crypto" is not a chain.
    if _ROBINHOOD_CHAIN_RE.search(low):
        return Chain.ROBINHOOD, Chain.ROBINHOOD.value
    for word in _KNOWN_OTHER_CHAINS:
        if re.search(rf"\b{word}\b", low):
            return None, word
    for word, chain in _CHAIN_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return chain, chain.value
    return None, None


def gas_estimate(chain: Chain | None, hint: str | None = None) -> Decimal:
    if chain is not None:
        return _GAS_ESTIMATE_USD[chain.value]
    if hint in {"monad", "megaeth", "sei", "sui", "aptos", "abstract", "berachain"}:
        return Decimal("0.30")
    return _GAS_ESTIMATE_USD[None]


def _clean_name(raw: str) -> str:
    name = re.sub(r"^[^\w$]+", "", (raw or "").strip())
    name = re.sub(r"\s+", " ", name)
    return name[:80].strip(" -:|")


# ----------------------------------------------------------------------------- collectors


def _evidence_from_text(
    *,
    name: str,
    text: str,
    url: str | None,
    source: str,
    ts_ms: int | None = None,
    kind: OpportunityKind | None = None,
) -> OpportunityEvidence | None:
    name = _clean_name(name)
    if len(name) < 3 or _NON_PROGRAMME_TITLE_RE.search(name) or not _PROGRAMME_RE.search(text):
        return None
    chain, hint = detect_chain(text)
    confirmed = _has_confirmation(text)
    points = bool(_POINTS_RE.search(text))
    return OpportunityEvidence(
        kind=kind or (OpportunityKind.POINTS if points and not confirmed else OpportunityKind.AIRDROP),
        name=name,
        chain=chain,
        chain_hint=hint,
        confirmed_token=confirmed,
        points_program=points,
        gas_cost_usd=gas_estimate(chain, hint),
        time_cost_hours=2.0 if points else 1.0,
        sybil_risk=SybilRisk.HIGH if _SYBIL_HARD_RE.search(text) else SybilRisk.MEDIUM,
        source_count=1,
        sources=[source],
        url=_programme_url(url),
        first_seen_ms=ts_ms or now_ms(),
        meta={"cost_basis": "estimated", "excerpt": text[:280], "source": source,
              "publication_ms": ts_ms},
    )


def from_airdrops_io_telegram(
    raw: str | None = None, conn: sqlite3.Connection | None = None
) -> list[OpportunityEvidence]:
    """airdrops.io's public Telegram preview (71k members, daily radar). No auth, no API."""
    source = "airdrops_io"
    page = raw if raw is not None else fetch_text(source, "telegram.preview", AIRDROPS_IO_URL, conn=conn)
    if page is None:
        return []
    try:
        out: list[OpportunityEvidence] = []
        posts = telegram_posts(page)
        if not posts:
            raise ValueError("unrecognized Telegram preview: no parseable posts")
        for post in posts:
            first_line = post.text.split("\n", 1)[0]
            links = [url for link in post.links if (url := _programme_url(link)) is not None]
            ev = _evidence_from_text(
                name=first_line,
                text=post.text,
                url=links[0] if links else None,
                source=source,
                ts_ms=post.ts_ms,
            )
            if ev is not None:
                ev.meta["source_url"] = f"https://t.me/{post.post}"
                out.append(ev)
        return out
    except Exception as exc:  # noqa: BLE001 - layout change, not a bug we may crash on
        provider_error(source, "telegram.preview", f"parse failed: {exc}", conn=conn)
        return []


def from_airdropalert_rss(
    raw: str | None = None, conn: sqlite3.Connection | None = None
) -> list[OpportunityEvidence]:
    """AirdropAlert's RSS feed - the one genuinely machine-readable airdrop source."""
    source = "airdropalert"
    body = raw if raw is not None else fetch_text(source, "rss.feed", AIRDROPALERT_RSS_URL, conn=conn)
    if body is None:
        return []
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError as exc:
        provider_error(source, "rss.feed", f"invalid rss: {exc}", conn=conn)
        return []
    try:
        out: list[OpportunityEvidence] = []
        if root.tag != "rss" or root.find("channel") is None:
            raise ValueError("unrecognized RSS feed: expected rss/channel")
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip() or None
            description = strip_tags(item.findtext("description") or "")
            pub = item.findtext("pubDate")
            ts = parse_rfc822_ms(pub)
            ev = _evidence_from_text(
                name=title,
                text=f"{title}\n{description}",
                url=link,
                source=source,
                ts_ms=ts,
            )
            if ev is not None:
                out.append(ev)
        return out
    except Exception as exc:  # noqa: BLE001
        provider_error(source, "rss.feed", f"parse failed: {exc}", conn=conn)
        return []


def parse_rfc822_ms(value: str | None) -> int | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def from_defillama(
    raw: Any | None = None, conn: sqlite3.Connection | None = None
) -> list[OpportunityEvidence]:
    """Tokenless protocols with real TVL - the population the airdrops page is drawn from.

    A protocol with no token and money already in it is the only speculative category with
    a defensible base rate, so it is collected as ``speculative`` and priced at p=0.15.
    """
    source = "defillama"
    data = raw if raw is not None else fetch_json(source, "protocols.list", DEFILLAMA_PROTOCOLS_URL, conn=conn)
    if data is None:
        return []
    if not isinstance(data, list):
        provider_error(source, "protocols.list", f"expected a list, got {type(data).__name__}", conn=conn)
        return []
    out: list[OpportunityEvidence] = []
    try:
        if data and not any(isinstance(row, dict) and row.get("name") and "tvl" in row for row in data):
            raise ValueError("unrecognized protocols list: no protocol records")
        for row in data:
            if not isinstance(row, dict):
                continue
            symbol = (row.get("symbol") or "-").strip()
            if symbol not in {"-", "", "none", "None"}:
                continue  # already has a token
            tvl = row.get("tvl")
            if not isinstance(tvl, int | float) or tvl < DEFILLAMA_MIN_TVL_USD:
                continue
            name = _clean_name(str(row.get("name") or ""))
            if not name:
                continue
            chains = [str(c) for c in (row.get("chains") or []) if c]
            chain, hint = detect_chain(" ".join(chains) or str(row.get("chain") or ""))
            out.append(
                OpportunityEvidence(
                    kind=OpportunityKind.AIRDROP,
                    name=name,
                    chain=chain,
                    chain_hint=hint,
                    confirmed_token=False,
                    points_program=False,
                    gas_cost_usd=gas_estimate(chain, hint),
                    time_cost_hours=1.5,
                    sybil_risk=SybilRisk.MEDIUM,
                    sources=[source],
                    url=_programme_url(row.get("url")),
                    # DefiLlama's registry `url` is the protocol's own site, the one field
                    # here that IS an official source. Until 2026-10-02 no collector set
                    # official_url at all, so qualification reported
                    # official_source_unavailable on 1080/1080 rows. Same value as `url`,
                    # so the opportunity key (name + link) is unchanged.
                    official_url=_programme_url(row.get("url")),
                    meta={
                        "cost_basis": "estimated",
                        "tvl_usd": float(tvl),
                        "category": row.get("category"),
                        "chains": chains,
                        "source": source,
                    },
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        provider_error(source, "protocols.list", f"parse failed: {exc}", conn=conn)
        return []


def _largest_record_list(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the biggest list of programme-shaped dicts anywhere in a JSON blob.

    Next.js pages move their data around between releases; the shape of a row (a name plus
    at least one programme-ish field) is far more stable than its path.
    """
    if depth > 8:
        return []
    best: list[dict[str, Any]] = []
    if isinstance(node, list):
        rows = [r for r in node if isinstance(r, dict) and r.get("name")]
        if rows and all(
            any(k in r for k in ("points", "program", "status", "chain", "category", "url"))
            for r in rows
        ):
            best = rows
        for child in node:
            found = _largest_record_list(child, depth + 1)
            if len(found) > len(best):
                best = found
    elif isinstance(node, dict):
        for child in node.values():
            found = _largest_record_list(child, depth + 1)
            if len(found) > len(best):
                best = found
    return best


_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.S
)
_ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(?P<cell>.*?)</t[dh]>", re.S)


class _AlphaDropsCards(HTMLParser):
    """Extract only linked h3 programme titles, not navigation or Next Flight code.

    These are aggregator discovery URLs, not verified official participation URLs.
    Reward, token promises, chains and dates stay unknown when absent from this shape.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._heading = False
        self._href: str | None = None
        self._text: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h3":
            self._heading = True
        if tag == "a" and self._heading:
            href = dict(attrs).get("href") or ""
            if re.fullmatch(r"/airdrops/[a-zA-Z0-9][a-zA-Z0-9_-]*/?", href):
                self._href = href
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            url = urljoin(ALPHADROPS_URL, self._href)
            name = _clean_name("".join(self._text))
            if name and url not in self._seen:
                self.rows.append({"name": name, "url": url, "source_page": ALPHADROPS_URL})
                self._seen.add(url)
            self._href = None
            self._text = []
        if tag == "h3":
            self._heading = False
            self._href = None
            self._text = []


def from_alphadrops(
    raw: str | None = None, conn: sqlite3.Connection | None = None
) -> list[OpportunityEvidence]:
    """Alpha Drops points-programmes page (48+ programmes, perp-DEX heavy).

    Accept legacy ``__NEXT_DATA__``/tables and current linked-title programme cards.
    None is a documented API contract; parsing yields discovery evidence only.
    """
    source = "alphadrops"
    page = raw if raw is not None else fetch_text(source, "points.list", ALPHADROPS_URL, conn=conn)
    if page is None:
        return []
    try:
        rows: list[dict[str, Any]] = []
        explicit_empty = False
        json_error = False
        blob = _NEXT_DATA_RE.search(page)
        if blob:
            try:
                data = json.loads(blob.group("json"))
                rows = _largest_record_list(data)
                # Only the known legacy programme container can attest to emptiness.
                # An empty shell, unrelated JSON list or new layout cannot.
                props = data.get("props") if isinstance(data, dict) else None
                page_props = props.get("pageProps") if isinstance(props, dict) else None
                explicit_empty = isinstance(page_props, dict) and page_props.get("programs") == []
            except ValueError as exc:
                provider_error(source, "points.list", f"invalid __NEXT_DATA__: {exc}", conn=conn)
                json_error = True
                rows = []
        if not rows:
            rows = _alphadrops_table_rows(page)
        if not rows:
            cards = _AlphaDropsCards()
            cards.feed(page)
            cards.close()
            rows = cards.rows
        if not rows and not explicit_empty and not json_error:
            provider_error(
                source, "points.list", "unrecognized points page: no programme rows or explicit empty list",
                conn=conn, body_chars=len(page),
            )
        out: list[OpportunityEvidence] = []
        for row in rows:
            name = _clean_name(str(row.get("name") or ""))
            if not name:
                continue
            blurb = " ".join(
                str(row.get(k)) for k in ("chain", "status", "category", "description") if row.get(k)
            )
            chain, hint = detect_chain(blurb)
            status = str(row.get("status", "")).strip().lower()
            confirmed = _has_confirmation(blurb) or (
                status == "confirmed" and not _CONFIRMATION_DOUBT_RE.search(blurb)
            )
            out.append(
                OpportunityEvidence(
                    kind=OpportunityKind.POINTS,
                    name=name,
                    chain=chain,
                    chain_hint=hint,
                    confirmed_token=confirmed,
                    points_program=True,
                    gas_cost_usd=gas_estimate(chain, hint),
                    time_cost_hours=3.0,  # points programmes run for weeks, not an afternoon
                    capital_required_usd=_as_decimal(row.get("capital_usd")) or Decimal("0"),
                    sybil_risk=SybilRisk.HIGH if row.get("sybil") else SybilRisk.MEDIUM,
                    sources=[source],
                    url=_programme_url(row.get("url")),
                    deadline_ms=parse_iso_ms(row.get("end_date")) if row.get("end_date") else None,
                    meta={"cost_basis": "estimated", "raw": row, "source": source},
                )
            )
        return out
    except Exception as exc:  # noqa: BLE001
        provider_error(source, "points.list", f"parse failed: {exc}", conn=conn)
        return []


def _alphadrops_table_rows(page: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in _ROW_RE.finditer(page):
        cells = [strip_tags(c) for c in _CELL_RE.findall(match.group("row"))]
        cells = [c for c in cells if c]
        if len(cells) < 2 or cells[0].lower() in {"name", "project", "programme", "program"}:
            continue
        href = _HREF_RE.search(match.group("row"))
        rows.append(
            {
                "name": cells[0],
                "chain": cells[1] if len(cells) > 1 else None,
                "status": cells[2] if len(cells) > 2 else None,
                "url": href.group("href") if href else None,
            }
        )
    return rows


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


#: Name -> collector. ``refresh`` iterates this; tests substitute it wholesale.
COLLECTORS: dict[str, Callable[..., list[OpportunityEvidence]]] = {
    "airdrops_io": from_airdrops_io_telegram,
    "airdropalert": from_airdropalert_rss,
    "defillama": from_defillama,
    "alphadrops": from_alphadrops,
}


# -------------------------------------------------------------------------------- refresh


def record_source(
    conn: sqlite3.Connection,
    name: str,
    kind: str,
    count: int,
    error: str | None = None,
    *,
    empty_ok: bool = False,
) -> None:
    """Per-collector health. A source that silently went to zero must be visible."""
    ts = now_ms()
    row = fetch_one(conn, "SELECT fail_streak FROM hunter_sources WHERE name=?", (name,))
    streak = int(row["fail_streak"]) if row else 0
    failed = error is not None or (count == 0 and not empty_ok)
    upsert(
        conn,
        "hunter_sources",
        {
            "name": name,
            "kind": kind,
            "last_run_ms": ts,
            "last_ok_ms": None if failed else ts,
            "last_count": count,
            "fail_streak": streak + 1 if failed else 0,
            "last_error": (error or None),
        },
        conflict=["name"],
        update=["kind", "last_run_ms", "last_count", "fail_streak", "last_error"]
        + ([] if failed else ["last_ok_ms"]),
    )


def collect(
    conn: sqlite3.Connection | None = None,
    collectors: dict[str, Callable[..., list[OpportunityEvidence]]] | None = None,
) -> list[OpportunityEvidence]:
    """Run every collector, deduping across them. One dead source never stops the rest."""
    c = conn or get_conn()
    found: list[OpportunityEvidence] = []
    for name, fn in (COLLECTORS if collectors is None else collectors).items():
        errors: list[str] = []
        token = _COLLECT_ERRORS.set(errors)
        try:
            items = fn(conn=c) or []
            record_source(c, name, "airdrop", len(items),
                          error="; ".join(errors)[:500] or None, empty_ok=True)
        except Exception as exc:  # noqa: BLE001 - a collector must never take down refresh
            provider_error(name, "collect", f"{type(exc).__name__}: {exc}", conn=c)
            record_source(c, name, "airdrop", 0, error=f"{type(exc).__name__}: {exc}")
            continue
        finally:
            _COLLECT_ERRORS.reset(token)
        found.extend(items)
    return dedupe(found)


def store_opportunity(
    conn: sqlite3.Connection,
    opp: OpportunityEvidence,
    score: EvScore,
    plan: dict[str, Any] | None = None,
) -> str:
    """Upsert one scored opportunity. ``created_ms`` survives; everything else refreshes."""
    oid = opp.key
    ts = now_ms()
    existing = fetch_one(conn, "SELECT created_ms FROM opportunities WHERE opportunity_id=?", (oid,))
    row = {
        "opportunity_id": oid,
        "kind": opp.kind.value,
        "name": opp.name,
        "chain": opp.chain.value if opp.chain else (opp.chain_hint or None),
        "url": opp.link,
        # A refusal is a state, not a footnote: the reports and the executor both read it.
        "status": "refused" if score.refused else "open",
        "ev_score": float(score.ev_usd),
        "cost_usd": str(score.cost_usd),
        "deadline_ms": opp.deadline_ms,
        "evidence_json": jdump(opp.model_dump(mode="json")),
        "plan_json": jdump(plan or {}),
        "created_ms": int(existing["created_ms"]) if existing else ts,
        "updated_ms": ts,
        "source": ",".join(opp.sources) or None,
        "symbol": opp.symbol,
        "confidence": score.confidence,
        "gross_usd": str(score.gross_usd),
        "warnings_json": jdump(score.warnings),
        "rationale_json": jdump(score.rationale),
        "last_seen_ms": ts,
    }
    upsert(conn, "opportunities", row, conflict=["opportunity_id"], update=[
        k for k in row if k not in {"opportunity_id", "created_ms"}
    ])
    return oid


def refresh(
    conn: sqlite3.Connection | None = None,
    collectors: dict[str, Callable[..., list[OpportunityEvidence]]] | None = None,
    cfg: HunterConfig | None = None,
    *,
    report: dict[str, Any] | None = None,
) -> int:
    """Collect, dedupe, score, store. Returns the number of opportunities written.

    ``HUNTER_FOUND`` fires only when something crosses the EV threshold *for the first
    time*: a re-scrape of the same programme is not news, and an alert that fires every
    hour is an alert nobody reads.
    """
    c = conn or get_conn()
    cfg = cfg or hunter_config()
    written = 0
    ids: set[str] = set()
    new_ids: set[str] = set()
    from kaiba.hunters.qualification import qualification

    for opp in collect(c, collectors):
        score = score_opportunity(opp, cfg)
        before = fetch_one(c, "SELECT * FROM opportunities WHERE opportunity_id=?", (opp.key,))
        ids.add(opp.key)
        if before is None:
            new_ids.add(opp.key)
        was_above = before is not None and qualification(
            before, at_ms=now_ms(), ev_floor=cfg.ev_threshold_usd,
        )["qualified"]
        # Only worth planning what clears the floor; planning the rest is the time cost the
        # EV model just told us not to spend.
        plan = (
            build_plan(opp, c, cfg).model_dump(mode="json")
            if score.ev_usd >= cfg.ev_threshold_usd
            else None
        )
        store_opportunity(c, opp, score, plan=plan)
        written += 1
        if (score.ev_usd >= cfg.ev_threshold_usd and not was_above and not score.refused
                and plan is not None and plan.get("refused") is False):
            emit(
                EventKind.HUNTER_FOUND,
                {
                    "opportunity_id": opp.key,
                    "kind": opp.kind.value,
                    "name": opp.name,
                    "ev_usd": str(score.ev_usd),
                    "confidence": score.confidence,
                    "cost_usd": str(score.cost_usd),
                    "sources": opp.sources,
                    "url": opp.link,
                    "warnings": score.warnings,
                },
                chain=opp.chain,
                subject=opp.key,
                conn=c,
            )
    if report is not None:
        report.update(refresh_summary(c, ids, new_ids, written, cfg,
                                      COLLECTORS if collectors is None else collectors))
    return written


def refresh_summary(
    conn: sqlite3.Connection, ids: set[str], new_ids: set[str], written: int,
    cfg: HunterConfig, source_names: Iterable[str],
    *, source_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exact per-pass registry receipt; neither an upsert nor a fetch is a fresh lead.

    Qualification runs on persisted evidence. Historical start dates and actual ends
    are counted separately; missing lifecycle/eligibility/value remains unavailable.
    """
    from kaiba.hunters.qualification import qualification

    at = now_ms()
    rows: list[dict[str, Any]] = []
    keys = sorted(ids)
    for start in range(0, len(keys), 200):
        batch = keys[start:start + 200]
        rows.extend(fetch_all(conn, "SELECT * FROM opportunities WHERE opportunity_id IN ("
                              + ",".join("?" for _ in batch) + ")", batch))
    verdicts = [qualification(row, at_ms=at, ev_floor=cfg.ev_threshold_usd) for row in rows]
    sources = {}
    for name in source_names:
        row = fetch_one(conn, "SELECT * FROM hunter_sources WHERE name=?", (name,))
        state = (source_states or {}).get(name)
        if state is None:
            state = "unavailable" if row is None else "error" if row["last_error"] else "ok" if row["last_count"] else "empty"
        sources[name] = {"state": state, "parsed_count": row["last_count"] if row else None,
                         "error": row["last_error"] if row else None}
    qualified = sum(v["qualified"] for v in verdicts)
    errors = sum(s["state"] == "error" for s in sources.values())
    unavailable = any(s["state"] == "unavailable" for s in sources.values())
    outcome = ("partial_source_error" if errors and written else "source_error" if errors
               else "partial_source_unavailable" if unavailable and written else "source_unavailable" if unavailable
               else "qualified_research" if qualified else "unqualified_leads" if new_ids
               else "duplicate_only" if written else "healthy_empty" if sources else "no_sources")
    facts = [json.loads(row["evidence_json"]) for row in rows]
    starts = [e.get("meta", {}).get("launch_ms") for e in facts]
    publications = [e.get("meta", {}).get("publication_ms") for e in facts]
    return {
        "outcome": outcome, "written": written, "distinct_count": len(rows),
        "new_count": len(new_ids), "refreshed_count": len(rows) - len(new_ids),
        "qualified_count": qualified, "unqualified_count": len(rows) - qualified,
        "refused_count": sum(row["status"] == "refused" or json.loads(row["plan_json"]).get("refused") is True for row in rows),
        "known_expired_count": sum("participation_deadline_passed" in v["reasons"] for v in verdicts),
        "past_start_count": sum(isinstance(t, int) and t <= at for t in starts),
        "publication_recent_count": sum(isinstance(t, int) and 0 <= at - t <= 86_400_000 for t in publications),
        "publication_unavailable_count": sum(t is None for t in publications),
        "unqualified_reason_counts": dict(Counter(reason for v in verdicts for reason in v["reasons"])),
        "sources": sources, "funded_action_authorized": False,
        "note": "New means first registry insertion, not verified freshness or eligibility. Past start is not expiry.",
    }


def refresh_report(
    conn: sqlite3.Connection | None = None,
    collectors: dict[str, Callable[..., list[OpportunityEvidence]]] | None = None,
    cfg: HunterConfig | None = None,
) -> dict[str, Any]:
    """Structured scheduler entry point; legacy refresh still returns integer upserts."""
    report: dict[str, Any] = {}
    refresh(conn, collectors, cfg, report=report)
    return report


# ----------------------------------------------------------------------------------- plans


class PlanAction(StrEnum):
    """Fixed vocabulary. If a programme needs something not in here, a human does it."""

    WALLET_CONNECT = "wallet_connect"
    SWAP = "swap"
    BRIDGE = "bridge"
    ADD_LIQUIDITY = "add_liquidity"
    STAKE = "stake"
    MINT = "mint"
    HOLD = "hold"
    GOVERNANCE_VOTE = "governance_vote"
    SIGN_MESSAGE = "sign_message"
    CLAIM = "claim"
    KYC = "kyc"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


#: Why these are where they are:
#: ``sign_message`` - an off-chain signature is how wallets get drained; it is never routine.
#: ``claim`` - claim pages are the single most phished surface in airdrop season.
#: ``kyc`` - identity documents cannot be un-sent.
#: ``bridge`` - bridges lose funds and strand them; ``stake``/``add_liquidity`` lock capital.
ACTION_RISK: dict[PlanAction, RiskLevel] = {
    PlanAction.WALLET_CONNECT: RiskLevel.LOW,
    PlanAction.HOLD: RiskLevel.LOW,
    PlanAction.GOVERNANCE_VOTE: RiskLevel.LOW,
    PlanAction.SWAP: RiskLevel.MEDIUM,
    PlanAction.MINT: RiskLevel.MEDIUM,
    PlanAction.ADD_LIQUIDITY: RiskLevel.HIGH,
    PlanAction.STAKE: RiskLevel.HIGH,
    PlanAction.BRIDGE: RiskLevel.HIGH,
    PlanAction.SIGN_MESSAGE: RiskLevel.CRITICAL,
    PlanAction.CLAIM: RiskLevel.CRITICAL,
    PlanAction.KYC: RiskLevel.CRITICAL,
}

#: Can the position this step creates be undone by us alone?
ACTION_REVERSIBLE: dict[PlanAction, bool] = {
    PlanAction.WALLET_CONNECT: True,
    PlanAction.HOLD: True,
    PlanAction.GOVERNANCE_VOTE: False,  # a cast vote cannot be uncast
    PlanAction.SWAP: False,  # you can swap back, at a price; that is not reversal
    PlanAction.MINT: False,
    PlanAction.ADD_LIQUIDITY: True,
    PlanAction.STAKE: True,
    PlanAction.BRIDGE: False,
    PlanAction.SIGN_MESSAGE: False,
    PlanAction.CLAIM: False,
    PlanAction.KYC: False,
}

OPERATOR_RISKS = frozenset({RiskLevel.CRITICAL, RiskLevel.HIGH})


class PlanStep(BaseModel):
    """One recorded step. Nothing here executes; the executor reads, filters and decides."""

    index: int
    action: PlanAction
    risk: RiskLevel
    detail: str
    capital_usd: Decimal = Decimal("0")
    reversible: bool = True
    requires_operator: bool = False
    chain: Chain | None = None
    chain_hint: str | None = None
    url: str | None = None
    start_slot: int | None = None

    @property
    def executable(self) -> bool:
        return not self.requires_operator and self.reversible


def make_step(
    index: int,
    action: PlanAction,
    detail: str,
    *,
    capital_usd: Decimal = Decimal("0"),
    chain: Chain | None = None,
    chain_hint: str | None = None,
    url: str | None = None,
    start_slot: int | None = None,
) -> PlanStep:
    risk = ACTION_RISK[action]
    return PlanStep(
        index=index,
        action=action,
        risk=risk,
        detail=detail,
        capital_usd=capital_usd,
        reversible=ACTION_REVERSIBLE[action],
        requires_operator=risk in OPERATOR_RISKS,
        chain=chain,
        chain_hint=chain_hint,
        url=url,
        start_slot=start_slot,
    )


class ParticipationPlan(BaseModel):
    """An ordered, recorded plan. ``refused=True`` means we will not do this at all."""

    opportunity_id: str
    name: str
    kind: OpportunityKind
    wallet_count: int = 1
    steps: list[PlanStep] = Field(default_factory=list)
    total_capital_usd: Decimal = Decimal("0")
    warnings: list[str] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    created_ms: int = Field(default_factory=now_ms)

    @property
    def requires_operator(self) -> bool:
        return self.refused or any(s.requires_operator for s in self.steps)

    @property
    def executable_steps(self) -> list[PlanStep]:
        """What the executor may pick up: non-critical, reversible, single-wallet only."""
        if self.refused or self.wallet_count > 1:
            return []
        return [s for s in self.steps if s.executable]


def build_plan(
    opportunity: OpportunityEvidence | dict[str, Any] | str,
    conn: sqlite3.Connection | None = None,
    cfg: HunterConfig | None = None,
) -> ParticipationPlan:
    """Write down how a single organic wallet would participate. Nothing is executed here.

    Multi-wallet plans are refused outright rather than scored down: the research verdict
    is that they are negative EV, and a refusal that lives in code cannot be argued out of
    by a persuasive prompt.
    """
    c = conn or get_conn()
    cfg = cfg or hunter_config()
    stored: dict[str, Any] = {}
    if isinstance(opportunity, str):
        row = fetch_one(c, "SELECT * FROM opportunities WHERE opportunity_id=?", (opportunity,))
        if row is None:
            raise KeyError(f"unknown opportunity {opportunity}")
        opportunity = row
    if isinstance(opportunity, dict) and "evidence_json" in opportunity:
        stored = opportunity
    opp = _as_evidence(opportunity, c)
    plan = ParticipationPlan(
        opportunity_id=opp.key,
        name=opp.name,
        kind=opp.kind,
        wallet_count=opp.wallet_count,
        total_capital_usd=Decimal("0"),
    )

    if opp.wallet_count > cfg.max_wallets:
        plan.refused = True
        plan.refusal_reason = (
            f"{opp.wallet_count}-wallet plan: >=20-address clusters are cut, blacklists are "
            "retroactive and shared between projects, and the EV is negative. Kaiba does not Sybil."
        )
        plan.warnings.append(plan.refusal_reason)
        return plan

    from kaiba.hunters.qualification import qualification

    score = score_opportunity(opp, cfg)
    verdict = qualification(
        {
            "kind": stored.get("kind", opp.kind.value),
            "chain": stored.get("chain", opp.chain.value if opp.chain else opp.chain_hint),
            "status": "refused" if score.refused else stored.get("status", "open"),
            "ev_score": str(score.ev_usd),
            "cost_usd": str(score.cost_usd),
            "deadline_ms": stored.get("deadline_ms", opp.deadline_ms),
            "evidence_json": opp.model_dump(mode="json"),
            "plan_json": stored.get("plan_json", {}),
        },
        at_ms=now_ms(), ev_floor=cfg.ev_threshold_usd,
    )
    if score.ev_usd <= 0:
        verdict["qualified"] = False
        verdict["reasons"].append("non_positive_ev")
    if not verdict["qualified"]:
        plan.refused = True
        plan.refusal_reason = "; ".join(verdict["reasons"])
        plan.warnings.extend(score.warnings)
        plan.warnings.append(plan.refusal_reason)
        return plan

    plan.total_capital_usd = opp.capital_required_usd
    meta = opp.meta or {}
    capital = opp.capital_required_usd
    chain, hint = opp.chain, opp.chain_hint
    i = 0
    steps: list[PlanStep] = []

    def add(action: PlanAction, detail: str, cap: Decimal = Decimal("0"), **kw: Any) -> None:
        nonlocal i
        i += 1
        steps.append(make_step(i, action, detail, capital_usd=cap, chain=chain, chain_hint=hint, **kw))

    add(PlanAction.WALLET_CONNECT, f"connect the single operating wallet to {opp.name}", url=opp.link)

    if meta.get("requires_signature") or opp.points_program:
        add(
            PlanAction.SIGN_MESSAGE,
            "sign the wallet-linking message - operator reads the payload first; a blind "
            "signature is how wallets get drained",
            url=opp.link,
        )

    if meta.get("requires_bridge") or (hint and chain is None):
        add(
            PlanAction.BRIDGE,
            f"bridge working capital to {hint or 'the target chain'} via the official bridge only",
            capital,
        )

    if capital > 0 or opp.kind in {OpportunityKind.AIRDROP, OpportunityKind.POINTS}:
        add(PlanAction.SWAP, "organic swap volume on the protocol's own venue", capital)

    if meta.get("provide_liquidity"):
        add(PlanAction.ADD_LIQUIDITY, "supply the protocol's main pool", capital)
    if meta.get("stake") or opp.capital_lockup_days > 0:
        add(
            PlanAction.STAKE,
            f"stake for {opp.capital_lockup_days}d (capital carry is already priced into the EV)",
            capital,
        )
    if meta.get("governance"):
        add(PlanAction.GOVERNANCE_VOTE, "vote on one live proposal")

    if opp.capital_lockup_days > 0 or opp.kind is OpportunityKind.POINTS:
        add(PlanAction.HOLD, "hold the position across the snapshot window; no wash cycling")

    if meta.get("kyc_required"):
        add(PlanAction.KYC, "KYC is an operator decision: identity documents cannot be un-sent")

    if opp.confirmed_token:
        add(
            PlanAction.CLAIM,
            "claim only from the URL in the project's own announcement - operator verifies "
            "the domain and the contract before signing",
            url=opp.official_url or opp.link,
        )

    plan.steps = steps
    if not plan.executable_steps:
        plan.warnings.append("no step is safe to automate: this opportunity is operator-only")
    if any(s.action is PlanAction.CLAIM for s in steps):
        plan.warnings.append("claim step present: verify the domain against the official announcement")
    return plan


def _as_evidence(
    opportunity: OpportunityEvidence | dict[str, Any] | str, conn: sqlite3.Connection
) -> OpportunityEvidence:
    if isinstance(opportunity, OpportunityEvidence):
        return opportunity
    if isinstance(opportunity, str):
        row = fetch_one(
            conn, "SELECT * FROM opportunities WHERE opportunity_id=?", (opportunity,)
        )
        if row is None:
            raise KeyError(f"unknown opportunity {opportunity}")
        opportunity = row
    if "evidence_json" in opportunity:
        return OpportunityEvidence.model_validate(json.loads(opportunity["evidence_json"]))
    return OpportunityEvidence.model_validate(opportunity)


# ---------------------------------------------------------------------------------- report


def _fmt_deadline(deadline_ms: int | None) -> str:
    if not deadline_ms:
        return "-"
    hours = (deadline_ms - now_ms()) / 3_600_000
    if hours < 0:
        return "passed"
    return f"{hours / 24:.1f}d"


def top_opportunities(
    conn: sqlite3.Connection,
    kinds: Iterable[str] = ("airdrop", "points"),
    limit: int = 15,
) -> list[dict[str, Any]]:
    kinds = list(kinds)
    return fetch_all(
        conn,
        f"SELECT * FROM opportunities WHERE status='open' AND kind IN "
        f"({','.join('?' for _ in kinds)}) ORDER BY ev_score DESC LIMIT ?",
        (*kinds, limit),
    )


def weekly_report(conn: sqlite3.Connection | None = None, limit: int = 15) -> str:
    """Ranked markdown for the operator. Warnings are in the table, not a footnote."""
    c = conn or get_conn()
    rows = top_opportunities(c, limit=limit)
    cfg = hunter_config()
    lines = [
        "# Airdrop hunter - weekly",
        "",
        f"Threshold: EV >= ${cfg.ev_threshold_usd} | operator time ${cfg.operator_hourly_usd}/h | "
        f"capital carry {cfg.capital_rate_annual} p.a.",
        "",
        "Single-wallet organic only. 88% of airdropped tokens lose value within three months "
        "and 64% of recipients sell at TGE; both are priced into every EV below.",
        "",
        "| # | Opportunity | Kind | Chain | EV (USD) | Cost | Conf | Deadline | Sources | Warnings |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not rows:
        lines.append("| - | *nothing above the floor this week* | | | | | | | | |")
    for n, row in enumerate(rows, 1):
        warnings = json.loads(row.get("warnings_json") or "[]")
        lines.append(
            "| {n} | [{name}]({url}) | {kind} | {chain} | {ev} | {cost} | {conf} | {dl} | {src} | {warn} |".format(
                n=n,
                name=row["name"].replace("|", "/"),
                url=row.get("url") or "",
                kind=row["kind"],
                chain=row.get("chain") or "-",
                ev=f"${row['ev_score']:.2f}" if row.get("ev_score") is not None else "-",
                cost=f"${row.get('cost_usd') or '0'}",
                conf=f"{row.get('confidence') or 0:.2f}",
                dl=_fmt_deadline(row.get("deadline_ms")),
                src=(row.get("source") or "-").replace(",", ", "),
                warn="; ".join(w.replace("|", "/") for w in warnings[:2]) or "-",
            )
        )
    negative = [r for r in rows if (r.get("ev_score") or 0) <= 0]
    if negative:
        lines += [
            "",
            f"{len(negative)} of {len(rows)} listed opportunities have non-positive EV once gas, "
            "capital carry and operator hours are subtracted. They are shown so the skip is "
            "auditable, not because they are candidates.",
        ]
    return "\n".join(lines)


def ranked_evidence(
    items: list[OpportunityEvidence], cfg: HunterConfig | None = None
) -> list[tuple[OpportunityEvidence, EvScore]]:
    cfg = cfg or hunter_config()
    return rank([(o, score_opportunity(o, cfg)) for o in items])


__all__ = [
    "ACTION_RISK",
    "COLLECTORS",
    "ParticipationPlan",
    "PlanAction",
    "PlanStep",
    "RiskLevel",
    "build_plan",
    "collect",
    "fetch_json",
    "fetch_text",
    "from_airdropalert_rss",
    "from_airdrops_io_telegram",
    "from_alphadrops",
    "from_defillama",
    "opportunity_id",
    "provider_error",
    "ranked_evidence",
    "record_source",
    "refresh",
    "refresh_report",
    "store_opportunity",
    "strip_tags",
    "telegram_posts",
    "weekly_report",
]
