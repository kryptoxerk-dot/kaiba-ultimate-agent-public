"""Structured, observation-only records from the public @rhscannerr preview.

No instructions, trading calls, wallet grades, or on-chain facts are derived from post
prose. Missing fields stay None. This module is deliberately not registered with a worker.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, Receipt, digest, now_ms

SOURCE = "telegram:rhscannerr"
CHANNEL = "rhscannerr"
log = logging.getLogger(__name__)

PREVIEW_URL = "https://t.me/s/rhscannerr"
MAX_HTML_CHARS = 2_000_000
_ADDRESS = r"0x[0-9a-fA-F]{40}"
_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_AMOUNT = rf"{_NUMBER}[KMB]?"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WalletObservation(_Model):
    address: str
    label: str | None = None
    holdings_pct: Decimal | None = None


class MarketMetrics(_Model):
    market_cap_usd: Decimal | None = None
    ath_market_cap_usd: Decimal | None = None
    prior_market_cap_usd: Decimal | None = None
    price_multiple_x: Decimal | None = None
    age_label: str | None = None
    volume_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    bonding_pct: Decimal | None = None
    holders: int | None = None
    swaps: int | None = None
    views: int | None = None


class DevObservation(_Model):
    status: Literal["holds", "sold", "unknown"] = "unknown"
    holdings_pct: Decimal | None = None
    launches: int | None = None
    active_launches: int | None = None
    rugs: int | None = None
    wallet: str | None = None


class HolderConfluence(_Model):
    top10_pct: Decimal | None = None
    insider_pct: Decimal | None = None
    sniper_count: int | None = None
    sniper_pct: Decimal | None = None
    bundle_pct: Decimal | None = None
    bundle_clusters: int | None = None
    bundle_wallets: int | None = None
    kol_count: int | None = None
    smart_count: int | None = None
    wallets: tuple[WalletObservation, ...] = ()
    entities: tuple[str, ...] = ()


class RhscannerrAlert(_Model):
    record_type: Literal["alert", "milestone"] = "alert"
    source: Literal["telegram:rhscannerr"] = SOURCE
    channel: Literal["rhscannerr"] = CHANNEL
    message_id: int
    message_ts_ms: int
    observed_at_ms: int
    message_url: str
    reply_to_message_id: int | None = None
    telegram_views: int | None = None
    receipt: Receipt
    chain: Chain
    address: str
    symbol: str
    name: str | None = None
    bonding_state: Literal["bonding", "bonded", "unknown"] = "unknown"
    og_status: Literal["og", "not_og", "unknown"] = "unknown"
    launchpad: str | None = None
    pair: str | None = None
    metrics: MarketMetrics
    dev: DevObservation
    funding_source: str | None = None
    funding_wallet: str | None = None
    confluence: HolderConfluence
    links: dict[str, str] = Field(default_factory=dict)
    observation_only: Literal[True] = True
    requires_fresh_dyor: Literal[True] = True
    execution_eligible: Literal[False] = False
    dyor_blockers: tuple[str, ...] = ("untrusted_telegram_observation", "fresh_dyor_required")
    dedupe_key: str


class ParseResult(_Model):
    records: tuple[RhscannerrAlert, ...] = ()
    provider_available: bool
    unavailable_reason: str | None = None
    messages_seen: int = 0
    message_text_nodes: int = 0
    unsupported_messages: int = 0
    field_coverage: dict[str, int] = Field(default_factory=dict)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)
    closed: bool = False

    def select(self, class_name: str) -> list[_Node]:
        found = [self] if class_name in self.attrs.get("class", "").split() else []
        for child in self.children:
            if isinstance(child, _Node):
                found.extend(child.select(class_name))
        return found

    def text(self) -> str:
        if self.tag == "br":
            return "\n"
        return "".join(child.text() if isinstance(child, _Node) else child for child in self.children)

    def elements(self, tag: str) -> list[_Node]:
        found = [self] if self.tag == tag else []
        for child in self.children:
            if isinstance(child, _Node):
                found.extend(child.elements(tag))
        return found


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) > 100:
            raise ValueError("html_nesting_limit")
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"br", "img", "meta", "link", "input", "hr", "wbr", "source", "area", "base", "embed"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _capture(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


def _amount(value: str | None) -> Decimal | None:
    if value is None:
        return None
    match = re.fullmatch(rf"({_NUMBER})([KMB]?)", value)
    if match is None:
        return None
    return Decimal(match[1].replace(",", "")) * {"": 1, "K": 1000, "M": 1000000, "B": 1000000000}[match[2]]


def _count(pattern: str, text: str) -> int | None:
    value = _capture(pattern, text)
    return int(value.replace(",", "")) if value is not None else None


def _linked_lines(node: _Node) -> list[tuple[str, list[_Node]]]:
    """Retain anchors on their exact text line; addresses in hrefs are not token CAs."""
    rows: list[tuple[str, list[_Node]]] = []
    parts: list[str] = []
    anchors: list[_Node] = []

    def append(text: str) -> None:
        for index, piece in enumerate(text.split("\n")):
            if index:
                rows.append(("".join(parts).strip(), list(anchors)))
                parts.clear()
                anchors.clear()
            parts.append(piece)

    def walk(child: _Node | str) -> None:
        if isinstance(child, str):
            append(child)
        elif child.tag == "br":
            append("\n")
        elif child.tag == "a":
            anchors.append(child)
            append(child.text())
        else:
            for item in child.children:
                walk(item)

    walk(node)
    append("\n")
    return [(text, anchors) for text, anchors in rows if text]


def _wallet_href(node: _Node, host: str) -> str | None:
    url = urlsplit(node.attrs.get("href", ""))
    match = re.fullmatch(rf"/(?:robinhood/)?address/({_ADDRESS})", url.path)
    return match[1].lower() if url.scheme == "https" and url.netloc == host and match else None


def _parse_message(node: _Node, observed_at_ms: int) -> RhscannerrAlert:
    if not re.fullmatch(r"rhscannerr/[1-9][0-9]*", node.attrs.get("data-post", "")):
        raise ValueError("invalid_channel_or_message_id")
    message_id = int(node.attrs["data-post"].split("/")[1])
    bodies = [item for item in node.select("js-message_text")
              if "js-message_reply_text" not in item.attrs.get("class", "").split()]
    if len(bodies) != 1 or not node.closed or not bodies[0].closed:
        raise ValueError("message_body_missing_or_truncated")
    main = bodies[0]
    linked_lines = _linked_lines(main)
    lines = [line for line, _ in linked_lines]
    text = "\n".join(lines)
    title = re.fullmatch(r".*?\$(\S+) — (.+)", lines[0])
    milestone = re.fullmatch(
        rf".*?\$(\S+) · \$({_AMOUNT}) \(([0-9.]+)x from \$({_AMOUNT})\) · (.+)", lines[0]
    )
    if title is None and milestone is None:
        raise ValueError("unsupported_rhscannerr_header")
    symbol = title[1] if title else milestone[1]
    name = title[2] if title else None
    addresses = {line for line in lines if re.fullmatch(_ADDRESS, line)}
    if len({address.lower() for address in addresses}) != 1:
        raise ValueError("missing_or_ambiguous_contract_address")
    address = sorted(addresses)[0].lower()
    if address == "0x" + "0" * 40:
        raise ValueError("zero_contract_address")
    venue = re.search(r"^💧 (PONS_V2|UNISWAP_V4) · (\S+/\S+)$", text, re.MULTILINE)
    state = _capture(r"ROBINHOOD · [^\n]*?\b(bonding|bonded)\b", lines[1])
    if title and (not lines[1].startswith("ROBINHOOD · ") or state is None):
        raise ValueError("unsupported_chain_or_state")
    metrics = MarketMetrics(
        market_cap_usd=(_amount(_capture(rf"^💰 MC: \$({_AMOUNT})\b", text))
                        if title else _amount(milestone[2])),
        ath_market_cap_usd=_amount(_capture(rf"^💰 MC: [^\n]+ ATH: \$({_AMOUNT})\b", text)),
        prior_market_cap_usd=_amount(milestone[4]) if milestone else None,
        price_multiple_x=Decimal(milestone[3]) if milestone else None,
        age_label=milestone[5] if milestone else None,
        volume_usd=_amount(_capture(rf"^(?:📊 |├ )Vol: \$({_AMOUNT})\b", text)),
        liquidity_usd=_amount(_capture(rf"^(?:📊 |├ )Vol: [^\n]+ Liq: \$({_AMOUNT})\b", text)),
        bonding_pct=_amount(_capture(r"^(?:🔵 |├ )Bonding: [▓░]+ ([0-9.]+)%$", text)),
        holders=_count(r"^👥 Holders: ([0-9,]+)\b", text),
        swaps=_count(r"^👥 Holders: [^\n]+ Swaps: ([0-9,]+)\b", text),
        views=_count(r"^👥 Holders: [^\n]+ 👁: ([0-9,]+)$", text),
    )
    dev = DevObservation(
        status="sold" if re.search(r"^🛠 Dev: [^\n]*Sold$", text, re.MULTILINE)
        else "holds" if re.search(r"^🛠 Dev: [^\n]*Holds [0-9.]+%$", text, re.MULTILINE) else "unknown",
        holdings_pct=_amount(_capture(r"^🛠 Dev: [^\n]*Holds ([0-9.]+)%$", text)),
        launches=_count(r"^├ Made: ([0-9,]+) ·", text),
        active_launches=_count(r"^├ Made: [0-9,]+ · ([0-9,]+) active", text),
        rugs=_count(r"^├ Made: [^\n]* · ([0-9,]+) rugs$", text),
        wallet=next((_wallet_href(link, "rh-scan.com") for line, anchors in linked_lines
                     if re.match(r"^[├└] (?:Dev wallet|Wallet): ", line)
                     for link in anchors), None),
    )
    funding_source = _capture(r"^├ (?:🚰 )?(?:Funding|Funded by): (.+)$", text)
    funding_wallet = next((_wallet_href(link, "rh-scan.com") for line, anchors in linked_lines
                           if re.match(r"^├ (?:🚰 )?(?:Funding|Funded by): ", line)
                           for link in anchors), None)
    wallet_line = _capture(r"^├ Wallets: (.+)$", text) or ""
    wallets = tuple(WalletObservation(address=m[1].lower(), label=m[2]) for m in re.finditer(
        rf"({_ADDRESS}) \(([^)]+)\)", wallet_line
    ))
    for line, anchors in linked_lines:
        if not line.startswith("[") or not line.endswith("]"):
            continue
        for anchor in anchors:
            wallet = _wallet_href(anchor, "gmgn.ai")
            match = re.fullmatch(r"(?:(.*?) )?([0-9.]+)%", anchor.text())
            if wallet and match:
                wallets += (WalletObservation(address=wallet, label=match[1], holdings_pct=_amount(match[2])),)
    confluence = HolderConfluence(
        top10_pct=_amount(_capture(r"^[├📊] Top ?10: ([0-9.]+)%", text)),
        insider_pct=_amount(_capture(r"^[├📊] Top ?10: [^\n]+ Insider: ([0-9.]+)%", text)),
        sniper_count=_count(r"^[├🔫] Snipers: ([0-9,]+)\b", text),
        sniper_pct=_amount(_capture(r"^🔫 Snipers: [0-9,]+ \(([0-9.]+)%\)$", text)),
        bundle_pct=_amount(_capture(r"^(?:├ Snipers: [^\n]+ Bundle|🫧 Bundles): ([0-9.]+)%", text)),
        bundle_clusters=_count(r"^(?:├ Snipers|🫧 Bundles): [^\n]+ · ([0-9,]+) (?:clusters?|kluster)\b", text),
        bundle_wallets=_count(r"^(?:├ Snipers|🫧 Bundles): [^\n]+ · ([0-9,]+) wallets?\b", text),
        kol_count=_count(r"^[├⭐] KOL:? ([0-9,]+)\b", text),
        smart_count=_count(r"^[├⭐🧠] [^\n]*?Smart:? ([0-9,]+)\b", text),
        wallets=wallets,
    )
    links = {}
    for link in main.elements("a"):
        label, url = link.text().strip().lower(), link.attrs.get("href", "")
        if label in {"gmgn", "dexscreener", "x", "web"} and urlsplit(url).scheme == "https":
            links[label] = url
    gmgn = urlsplit(links.get("gmgn", ""))
    token_link = re.fullmatch(rf"/robinhood/token/(?:[A-Za-z0-9]+_)?({_ADDRESS})", gmgn.path)
    if gmgn.netloc != "gmgn.ai" or not token_link or token_link[1].lower() != address:
        raise ValueError("token_link_missing_or_conflicting")
    dt = datetime.fromisoformat(node.elements("time")[0].attrs["datetime"])
    if dt.tzinfo is None:
        raise ValueError("timezone_missing")
    views = node.select("tgme_widget_message_views")
    return RhscannerrAlert(
        message_ts_ms=int(dt.timestamp() * 1000), observed_at_ms=observed_at_ms,
        telegram_views=_count(r"^([0-9,]+)$", views[0].text()) if views else None,
        receipt=Receipt(provider=SOURCE, endpoint=f"https://t.me/{CHANNEL}/{message_id}",
                        observed_at_ms=observed_at_ms, response_digest=digest(main.text())),
        record_type="milestone" if milestone else "alert",
        message_id=message_id, message_url=f"https://t.me/{CHANNEL}/{message_id}",
        reply_to_message_id=next((int(link.attrs["href"].rsplit("/", 1)[-1])
                                  for link in node.elements("a")
                                  if "tgme_widget_message_reply" in link.attrs.get("class", "").split()
                                  and link.attrs.get("href", "").rsplit("/", 1)[-1].isdigit()), None),
        address=address, chain=Chain.ROBINHOOD, symbol=symbol, name=name,
        bonding_state=state or "unknown",
        og_status=("not_og" if title and "Not OG" in lines[1] else "og" if title else "unknown"),
        launchpad=venue[1].lower() if venue else None, pair=venue[2] if venue else None,
        metrics=metrics, dev=dev, funding_source=funding_source, funding_wallet=funding_wallet,
        confluence=confluence, links=links,
        dedupe_key=(f"rhscannerr:message:{message_id}" if title
                    else f"rhscannerr:milestone:{address.lower()}:{metrics.market_cap_usd}:{metrics.price_multiple_x}"),
    )


def _coverage(records: tuple[RhscannerrAlert, ...]) -> dict[str, int]:
    values: dict[str, int] = {}
    for record in records:
        fields = record.model_dump()
        for section in ("metrics", "dev", "confluence"):
            fields.update({(key if section == "metrics" else f"{section}.{key}"): value
                           for key, value in fields.pop(section).items()})
        for key, value in fields.items():
            known = value is not None and value != () and value != {}
            values[key] = values.get(key, 0) + int(known)
    return values


def parse_preview_html(html: str, *, observed_at_ms: int | None = None) -> ParseResult:
    observed_at_ms = observed_at_ms if observed_at_ms is not None else now_ms()
    if not isinstance(html, str) or not html or len(html) > MAX_HTML_CHARS:
        return ParseResult(provider_available=False, unavailable_reason="invalid_html_size_or_type")
    parser = _HTML()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, RecursionError):
        return ParseResult(provider_available=False, unavailable_reason="invalid_html")
    history = parser.root.select("tgme_channel_history")
    if not history:
        return ParseResult(provider_available=False, unavailable_reason="channel_history_marker_missing")
    if len(history) != 1 or not history[0].closed:
        return ParseResult(provider_available=False, unavailable_reason="invalid_history_envelope")
    nodes = parser.root.select("js-widget_message")
    if not nodes:
        return ParseResult(provider_available=False, unavailable_reason="message_markers_missing")
    text_nodes = parser.root.select("js-message_text")
    if nodes and not text_nodes:
        return ParseResult(
            provider_available=False,
            unavailable_reason="message_text_marker_missing",
            messages_seen=len(nodes),
        )
    parsed_list: list[RhscannerrAlert] = []
    parse_errors = 0
    for node in nodes:
        try:
            parsed_list.append(_parse_message(node, observed_at_ms))
        except (IndexError, KeyError, TypeError, ValueError):
            parse_errors += 1
    # A post we cannot decode is one post, not a dead provider.
    #
    # MEASURED 2026-09-23 against the live page: 20 messages, 19 parsed, 1 raised
    # ``unsupported_rhscannerr_header`` -- and because ANY error failed the whole batch,
    # the feed reported ``message_parse_error`` and emitted nothing. It had never
    # delivered an observation. The channel mixes alert formats and adds new ones, so
    # under the old rule a single unrecognised post blinded the feed until someone
    # noticed, which is precisely the silent outage this module's fetch guards avoid.
    #
    # The honesty rule is kept, not relaxed: a failure is still never converted into an
    # empty answer. NOTHING parsing means the format changed under us and is still
    # ``provider_available=False``; anything else reports what was decoded AND how many
    # posts were not, so `unsupported_messages` climbing is visible rather than silent.
    if parse_errors and not parsed_list:
        return ParseResult(
            provider_available=False,
            unavailable_reason="message_parse_error",
            messages_seen=len(nodes),
            message_text_nodes=len(text_nodes),
            unsupported_messages=parse_errors,
        )
    if parse_errors:
        log.info(
            "rhscannerr: %d of %d posts were not decoded; reporting the %d that were",
            parse_errors, len(nodes), len(parsed_list),
        )
    parsed = tuple(parsed_list)
    records = tuple({record.dedupe_key: record for record in parsed}.values())
    return ParseResult(
        records=records,
        provider_available=True,
        messages_seen=len(nodes),
        message_text_nodes=len(text_nodes),
        # The real count, not a constant. This used to be hardcoded to 0 because the
        # function could not reach here with any error; now that one undecoded post no
        # longer fails the batch, reporting it is what keeps partial acceptance honest --
        # a format drift shows up as this number climbing instead of as silence.
        unsupported_messages=parse_errors,
        field_coverage=_coverage(records),
    )


def emit_alerts(records: list[RhscannerrAlert] | tuple[RhscannerrAlert, ...], *, conn: Any = None) -> list[int]:
    """Emit observation-only ``alpha.meta`` events; the event table enforces dedupe."""
    emitted: list[int] = []
    for record in records:
        event_id = emit(
            EventKind.ALPHA_META,
            record.model_dump(mode="json"),
            chain=record.chain,
            subject=record.address,
            dedupe_key=record.dedupe_key,
            conn=conn,
        )
        if event_id is not None:
            emitted.append(event_id)
    return emitted


def fetch_public_html(*, url: str = PREVIEW_URL, timeout_s: float = 15.0) -> str:
    """Fetch only the public preview; no Telegram account or write capability is used."""
    response = httpx.get(
        url,
        headers={"User-Agent": "kaiba-rhscannerr-readonly/1"},
        follow_redirects=True,
        timeout=timeout_s,
    )
    response.raise_for_status()
    return response.text


def _provider_unavailable(reason: str, *, conn: Any = None) -> None:
    emit(
        EventKind.PROVIDER_ERROR,
        {
            "provider": "telegram",
            "source": SOURCE,
            "status": "unavailable",
            "reason": reason,
            "observation_only": True,
        },
        level="warn",
        dedupe_key=f"provider_unavailable:{SOURCE}:{reason}",
        conn=conn,
    )


def poll_once(
    *,
    fetcher: Callable[[str], str] | None = None,
    observed_at_ms: int | None = None,
    conn: Any = None,
) -> ParseResult:
    """Read, parse, and emit one public snapshot using an injectable offline fetch seam."""
    try:
        html = fetcher(PREVIEW_URL) if fetcher is not None else fetch_public_html()
    except Exception as exc:  # noqa: BLE001 - provider failure is a health state
        reason = f"fetch_failed:{type(exc).__name__}"
        _provider_unavailable(reason, conn=conn)
        return ParseResult(provider_available=False, unavailable_reason=reason)
    result = parse_preview_html(html, observed_at_ms=observed_at_ms)
    if not result.provider_available:
        _provider_unavailable(result.unavailable_reason or "unavailable", conn=conn)
        return result
    emit_alerts(result.records, conn=conn)
    return result


async def run(
    *,
    stop: asyncio.Event,
    interval_s: float = 60.0,
    fetcher: Callable[[str], str] | None = None,
    conn: Any = None,
) -> None:
    """Supervised read-only poller for the public channel preview.

    It emits only observation events. A provider failure is recorded and the supervisor
    keeps the feed alive; no failure is converted into an empty or safe alert set.
    """
    while not stop.is_set():
        await asyncio.to_thread(poll_once, fetcher=fetcher, conn=conn)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, float(interval_s)))