"""SHADOW recorder for free, keyless alpha and narrative feeds. Observation only.

Narrative is the weakest input the mooner study has: there was no live narrative data at
all. These feeds carry it, plus early-alpha rankings, for free. This module RECORDS them so
their lead time (versus our own first sighting) and, later, their forward returns can be
measured. It does not gate, size or trigger anything: it writes the two tables of
migration 036 and nothing that decides reads them. It must never write ``tokens`` either,
because ``tokens.first_seen_ms`` is the baseline its lead time is measured against.

Feeds, all probed from the box (Asia) on 2026-10-04 with one call each:

* ``binance:meme_rush:{new,finalizing,migrated}`` -- Binance Web3 launchpad lifecycle
  lists (rankType 10/20/30), POST, no key. Rows carry ``narrativeText`` (AI narrative,
  attached a little after a token first lists: 15 of 20 fresh rows had one), dev migration
  counts and wash-trading tags. sol (``CT_501``) answers; robinhood (``4663``) answers
  ``000000`` with an EMPTY list, so it is not covered. ``new`` is NOT in the defaults:
  MEASURED 20 rows spanning 52 s, i.e. every sol launch (~1,400/h), which our own launch
  feed already sees at creation, so it cannot lead us and would add ~33k rows a day.
* ``binance:topic_rush:{latest,rising}`` -- AI-detected hot topics, each with up to six
  associated tokens. GET. One row per (topic, token); the narrative is the topic name and
  its AI summary. sol only (robinhood: empty list). ``rising`` is NOT in the defaults:
  MEASURED it returned the same 30 topic ids as ``latest`` (sort=10).
* ``binance:smart_money`` -- smart-money buy/sell signals with ``maxGain`` (a FRACTION:
  ``"4.7028"`` was a 5.7x alert-to-high move) and ``exitRate``. MEASURED: robinhood
  (``4663``) IS served although the vendor documents only bsc and sol.
* ``gmgn:hot_searches:5m`` -- GMGN's search-heat ranking. One call covers sol and
  robinhood. Shares the gmgn budget with live exits, so DISCOVERY priority, every 5 min.
* ``gmgn:created_tokens`` -- an ENRICHMENT lookup, not a sighting: the launch record
  (ATH market cap, graduated / still-on-curve counts) of the dev behind a token another
  feed surfaced. One dev per 5-min poll, DISCOVERY priority. Never written to first-seen.
  OPT-IN (not in :data:`DEFAULT_FEEDS`): it would be a second gmgn consumer.

Point-in-time reads for the daily audit: :func:`load_sightings` (bulk) with
:func:`sightings_before` / :func:`feed_features` per signal, or :func:`feed_sightings_at`
for one token. They never return a sighting, or a narrative, observed at or after t0.

Budgets. Binance calls go through the shared limiter as provider ``binance_web3``; every
endpoint shares the ``web3`` family, so a ``100004`` (the vendor's rate-limit code, sent
with HTTP 200) or an HTTP 429 on any of them opens one cooldown for all of them, doubling
per repeat (the limiter's own ban schedule). On top of that each feed backs off on its
own, doubling to :data:`BACKOFF_MAX_S`. Nothing here retries in a loop: a refused or
failed poll simply waits for its next turn. Response bodies are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from kaiba.core.db import get_conn, tx
from kaiba.core.events import emit
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import EVM_CHAINS, Chain, EventKind, digest, now_ms

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------ vendors

BINANCE_PROVIDER = "binance_web3"
_BAPI = "https://web3.binance.com/bapi/defi"
MEME_RUSH_URL = f"{_BAPI}/v1/public/wallet-direct/buw/wallet/market/token/pulse/rank/list/ai"
TOPIC_RUSH_URL = f"{_BAPI}/v2/public/wallet-direct/buw/wallet/market/token/social-rush/rank/list/ai"
SMART_MONEY_URL = f"{_BAPI}/v1/public/wallet-direct/buw/wallet/web/signal/smart-money/ai"

#: The vendor skill's own User-Agent (binance-skills-hub, meme-rush/scripts/cli.mjs).
#: MEASURED 2026-10-05 from the box with this exact UA: meme-rush, topic-rush and
#: smart-money all answered HTTP 200 / ``000000`` in 0.15-0.27 s (no geo-block).
USER_AGENT = "binance-web3/2.0 (Skill)"
#: Sent on EVERY request, not only as client defaults: an injected client (a test, a
#: shared pool) would otherwise go out as ``python-httpx/x.y``.
REQUEST_HEADERS: dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "application/json"}
HTTP_TIMEOUT_S = 10.0

#: Binance Web3 chain ids. ``4663`` is MEASURED (smart-money serves it; the rush lists
#: answer it with an empty list), not documented.
BINANCE_CHAIN_IDS: dict[Chain, str] = {
    Chain.SOL: "CT_501",
    Chain.BSC: "56",
    Chain.BASE: "8453",
    Chain.ROBINHOOD: "4663",
}
_CHAIN_BY_BINANCE_ID = {v: k for k, v in BINANCE_CHAIN_IDS.items()}
BINANCE_OK = "000000"
BINANCE_RATE_LIMITED = "100004"

MEME_RUSH_STAGES: dict[str, int] = {"new": 10, "finalizing": 20, "migrated": 30}
TOPIC_RUSH_LISTS: dict[str, int] = {"latest": 10, "rising": 20}

# ------------------------------------------------------------------------------ storage

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
#: Hard cap on ``raw_json`` per row, after dropping icons / translations / audit blobs.
#: MEASURED vendor rows: meme-rush ~2.8 KB, hot-searches ~2.6 KB, a topic with its token
#: list ~7.6 KB (each stored row keeps only its own token).
RAW_CAP_BYTES = 3072
RAW_STRING_CAP = 600
NARRATIVE_CAP = 1000
SYMBOL_CAP = 64
NAME_CAP = 200

#: Keys dropped from ``raw_json`` at any depth: images, translated duplicates, nested
#: blobs and the other tokens of a topic. None is a fact the study reads.
DROP_KEYS: frozenset[str] = frozenset(
    {
        "icon", "logo", "logoUrl", "chainLogoUrl", "token_logo", "previewLink",
        "nameTranslate", "symbolTranslate", "trans_name", "trans_name_zhcn",
        "trans_symbol", "trans_symbol_zhcn", "topicNameCn", "aiSummaryCn",
        "auditInfoJson", "twitterInfo", "taxFeeDistribution", "firstGasInfo",
        "twitter_name_change_history", "tokenList",
    }
)

#: Rows kept this long, then pruned in bounded chunks. First-seen (with the first
#: narrative) is kept forever, so pruning loses only the hourly rank trajectory.
#: INVENTED as a duration; the need is MEASURED: the 2026-10-04 dry run sized the default
#: feeds at ~1.4k rows/h x ~1.8 KB, ~65 MB/day (~0.9 GB steady state at 14 days), on a box
#: with 18 GB free where a full disk froze every service for 14 h on 2026-09-28.
RETENTION_DAYS = 14.0
PRUNE_EVERY_S = 3600.0

# ------------------------------------------------------------------------------ backoff

#: First wait after a rate limit or error; doubles per repeat up to the max. INVENTED.
BACKOFF_MIN_S = 60.0
BACKOFF_MAX_S = 900.0
#: A vendor Retry-After is honoured up to this; a nonsense header must not park a feed.
RETRY_AFTER_MAX_S = 3600.0


# -------------------------------------------------------------------------------- types


@dataclass(frozen=True)
class FeedSpec:
    """One recorded list. ``sighting=False`` marks an enrichment lookup (no first-seen)."""

    name: str
    provider: str
    chains: tuple[Chain, ...]
    interval_s: float
    window_ms: int = HOUR_MS
    sighting: bool = True


@dataclass(frozen=True)
class FeedRow:
    feed: str
    chain: str
    token_address: str
    symbol: str | None
    name: str | None
    narrative: str | None
    rank: int | None
    context: str | None
    source_ts_ms: int | None
    raw: dict[str, Any]


@dataclass
class PollResult:
    feed: str
    status: str  # ok | empty | rate_limited | limiter | error | skipped
    rows: list[FeedRow] = field(default_factory=list)
    written: int = 0
    detail: str = ""
    #: The vendor's own Retry-After, when it sent one; the next poll never comes sooner.
    retry_after_s: float | None = None


class BinanceRateLimited(Exception):
    """``100004`` or HTTP 429. ``status_code`` makes the limiter open a family cooldown."""

    status_code = 429

    def __init__(self, detail: str, retry_after_s: float | None = None) -> None:
        super().__init__(f"binance web3 rate limited ({detail})")
        self.retry_after_s = retry_after_s


class BinanceError(Exception):
    """Any other failure: bad HTTP status, non-JSON body, unexpected shape, error code."""


# Cadences and windows are INVENTED (no measurement can settle them before data exists);
# each is sized against the measured churn of the list and, for gmgn, the shared budget.
FEEDS: dict[str, FeedSpec] = {
    spec.name: spec
    for spec in (
        FeedSpec("binance:meme_rush:new", BINANCE_PROVIDER, (Chain.SOL,), 60.0),
        FeedSpec("binance:meme_rush:finalizing", BINANCE_PROVIDER, (Chain.SOL,), 60.0),
        FeedSpec("binance:meme_rush:migrated", BINANCE_PROVIDER, (Chain.SOL,), 120.0),
        # MEASURED: 30 topics spanned 40 min, so a 5-minute poll misses none.
        FeedSpec("binance:topic_rush:latest", BINANCE_PROVIDER, (Chain.SOL,), 300.0),
        FeedSpec("binance:topic_rush:rising", BINANCE_PROVIDER, (Chain.SOL,), 300.0),
        # MEASURED: 50 sol signals spanned 2.6 days; robinhood is served (undocumented).
        FeedSpec("binance:smart_money", BINANCE_PROVIDER, (Chain.SOL, Chain.ROBINHOOD), 120.0),
        FeedSpec("gmgn:hot_searches:5m", "gmgn", (Chain.SOL, Chain.ROBINHOOD), 300.0),
        FeedSpec(
            "gmgn:created_tokens", "gmgn", (Chain.SOL, Chain.ROBINHOOD), 300.0,
            window_ms=7 * DAY_MS, sighting=False,
        ),
    )
}

#: ``meme_rush:new`` and ``topic_rush:rising`` are recordable but not default; see the
#: module docstring for the measurements behind each.
BINANCE_FEEDS: tuple[str, ...] = (
    "binance:meme_rush:finalizing",
    "binance:meme_rush:migrated",
    "binance:topic_rush:latest",
    "binance:smart_money",
)
#: gmgn's rate limit is IP-wide and the same budget carries live stop-loss sells, so the
#: default is ONE DISCOVERY read every 5 min covering sol and robinhood together (12/h).
#: ``gmgn:created_tokens`` (one dev lookup per 5 min) is opt-in: name it in ``feeds=``.
GMGN_FEEDS: tuple[str, ...] = ("gmgn:hot_searches:5m",)
DEFAULT_FEEDS: tuple[str, ...] = BINANCE_FEEDS + GMGN_FEEDS

#: Feeds whose ``context`` column is a dev / creator wallet, read by ``created_tokens``.
DEV_SOURCE_FEEDS: tuple[str, ...] = (
    "binance:meme_rush:finalizing",
    "binance:meme_rush:migrated",
    "gmgn:hot_searches:5m",
)

MEME_RUSH_LIMIT = 50
SMART_MONEY_PAGE_SIZE = 25
HOT_SEARCH_LIMIT = 100


# ------------------------------------------------------------------------------ helpers


def _text(value: Any, cap: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s[:cap] if s else None


def _ms(value: Any) -> int | None:
    """A vendor timestamp as epoch ms. Values under 1e11 are seconds (GMGN), else ms."""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v * 1000 if v < 100_000_000_000 else v


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def norm_address(chain: Chain | str, address: Any) -> str | None:
    """EVM addresses lowercase (as ``tokens`` stores them); Solana keeps its case."""
    a = _text(address, 128)
    if not a:
        return None
    try:
        is_evm = Chain(str(chain)) in EVM_CHAINS
    except ValueError:
        is_evm = a.startswith("0x")
    return a.lower() if is_evm else a


def _slim(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {k: _slim(v, depth + 1) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, list):
        return [_slim(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > RAW_STRING_CAP:
        return value[:RAW_STRING_CAP] + "...[cut]"
    return value


def cap_raw(raw: Any, cap: int = RAW_CAP_BYTES) -> str:
    """Slimmed JSON, never more than ``cap`` bytes. Truncation is marked, never silent.

    The head of an oversized document is kept as a STRING (escaping grows it, so it is
    shrunk until the wrapper fits); the result is always valid JSON.
    """
    text = json.dumps(_slim(raw), separators=(",", ":"), ensure_ascii=False, default=str)
    size = len(text.encode("utf-8"))
    if size <= cap:
        return text
    n = cap
    while n > 16:
        out = json.dumps({"_truncated": True, "_bytes": size, "_head": text[:n]},
                         separators=(",", ":"), ensure_ascii=False)
        if len(out.encode("utf-8")) <= cap:
            return out
        n = int(n * 0.7)
    return json.dumps({"_truncated": True, "_bytes": size}, separators=(",", ":"))


def _chunks(items: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


# ------------------------------------------------------------------------------ parsing


def binance_data(payload: Any) -> list[dict[str, Any]]:
    """The ``data`` list of a Binance Web3 envelope, or a raise that says why not."""
    if not isinstance(payload, dict):
        raise BinanceError(f"payload is {type(payload).__name__}, not an envelope")
    code = str(payload.get("code") or "")
    if code == BINANCE_RATE_LIMITED:
        raise BinanceRateLimited(f"code {code}")
    if code != BINANCE_OK:
        msg = _text(payload.get("message") or payload.get("messageDetail"), 120) or ""
        raise BinanceError(f"code {code or '?'} {msg}".strip())
    data = payload.get("data")
    if data is None:
        return []
    if not isinstance(data, list):
        raise BinanceError(f"data is {type(data).__name__}, not a list")
    return [row for row in data if isinstance(row, dict)]


def _row_chain(row: dict[str, Any], default: Chain) -> Chain:
    return _CHAIN_BY_BINANCE_ID.get(str(row.get("chainId") or ""), default)


def _meme_narrative(row: dict[str, Any]) -> str | None:
    nt = row.get("narrativeText")
    if isinstance(nt, dict):
        return _text(nt.get("en"), NARRATIVE_CAP) or _text(nt.get("cn"), NARRATIVE_CAP)
    return _text(nt, NARRATIVE_CAP)


def parse_meme_rush(data: list[dict[str, Any]], chain: Chain, stage: str) -> list[FeedRow]:
    """One row per listed token. ``context`` is the dev wallet (for ``created_tokens``)."""
    feed = f"binance:meme_rush:{stage}"
    out: list[FeedRow] = []
    for i, row in enumerate(data, start=1):
        ch = _row_chain(row, chain)
        addr = norm_address(ch, row.get("contractAddress"))
        if not addr:
            continue
        out.append(
            FeedRow(
                feed=feed, chain=ch.value, token_address=addr,
                symbol=_text(row.get("symbol"), SYMBOL_CAP), name=_text(row.get("name"), NAME_CAP),
                narrative=_meme_narrative(row), rank=i,
                context=norm_address(ch, row.get("devAddress")),
                source_ts_ms=_ms(row.get("createTime")), raw=row,
            )
        )
    return out


def parse_topic_rush(data: list[dict[str, Any]], chain: Chain, list_name: str) -> list[FeedRow]:
    """One row per (topic, associated token). ``rank`` is the TOPIC's position."""
    feed = f"binance:topic_rush:{list_name}"
    out: list[FeedRow] = []
    for t_rank, topic in enumerate(data, start=1):
        name = topic.get("name") if isinstance(topic.get("name"), dict) else {}
        summary = topic.get("aiSummary") if isinstance(topic.get("aiSummary"), dict) else {}
        title = _text(name.get("topicNameEn") or name.get("topicNameCn"), 200)
        tags = [str(t) for t in (topic.get("topicTags") or []) if t]
        label = ", ".join(p for p in (str(topic.get("type") or ""), *tags) if p)
        body = _text(summary.get("aiSummaryEn") or summary.get("aiSummaryCn"), NARRATIVE_CAP)
        text = title or ""
        if label:
            text += f" [{label}]"
        if body:
            text += f": {body}"
        narrative = _text(text, NARRATIVE_CAP)
        topic_meta = {k: v for k, v in topic.items() if k != "tokenList"}
        for t_index, token in enumerate(topic.get("tokenList") or [], start=1):
            if not isinstance(token, dict):
                continue
            ch = _row_chain(token, chain)
            addr = norm_address(ch, token.get("contractAddress"))
            if not addr:
                continue
            out.append(
                FeedRow(
                    feed=feed, chain=ch.value, token_address=addr,
                    symbol=_text(token.get("symbol"), SYMBOL_CAP), name=None,
                    narrative=narrative, rank=t_rank,
                    context=_text(topic.get("topicId"), 64),
                    source_ts_ms=_ms(topic.get("createTime")),
                    raw={"topic": topic_meta, "token": token, "token_index": t_index},
                )
            )
    return out


def parse_smart_money(data: list[dict[str, Any]], chain: Chain) -> list[FeedRow]:
    """One row per signal. ``context`` is ``signalId:direction``; no narrative."""
    out: list[FeedRow] = []
    for i, row in enumerate(data, start=1):
        ch = _row_chain(row, chain)
        addr = norm_address(ch, row.get("contractAddress"))
        if not addr:
            continue
        out.append(
            FeedRow(
                feed="binance:smart_money", chain=ch.value, token_address=addr,
                symbol=_text(row.get("ticker"), SYMBOL_CAP), name=None, narrative=None, rank=i,
                context=_text(f"{row.get('signalId')}:{row.get('direction') or '?'}", 64),
                source_ts_ms=_ms(row.get("signalTriggerTime")), raw=row,
            )
        )
    return out


def parse_hot_searches(payload: Any, interval: str = "5m") -> list[FeedRow]:
    """GMGN hot-searches blocks -> rows. ``context`` is the token's creator wallet."""
    feed = f"gmgn:hot_searches:{interval}"
    blocks = payload if isinstance(payload, list) else []
    out: list[FeedRow] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        try:
            ch = Chain(str(block.get("chain") or ""))
        except ValueError:
            continue
        for i, tok in enumerate(block.get("tokens") or [], start=1):
            if not isinstance(tok, dict):
                continue
            addr = norm_address(ch, tok.get("address"))
            if not addr:
                continue
            out.append(
                FeedRow(
                    feed=feed, chain=ch.value, token_address=addr,
                    symbol=_text(tok.get("symbol"), SYMBOL_CAP), name=_text(tok.get("name"), NAME_CAP),
                    narrative=None, rank=_int(tok.get("rank")) or i,
                    context=norm_address(ch, tok.get("creator")),
                    source_ts_ms=_ms(tok.get("open_timestamp") or tok.get("creation_timestamp")),
                    raw=tok,
                )
            )
    return out


def summarize_created_tokens(payload: Any, token: str | None = None) -> dict[str, Any]:
    """The dev-record facts the study reads, from ``portfolio created-tokens``.

    ``inner_count`` / ``open_count`` are GMGN's totals (still on curve / graduated) for
    the dev; the ``listed_*`` fields cover only the <=100 tokens the endpoint returns.
    """
    body = payload if isinstance(payload, dict) else {}
    tokens = [t for t in (body.get("tokens") or []) if isinstance(t, dict)]

    def ath(t: dict[str, Any]) -> float:
        try:
            v = float(t.get("token_ath_mc") or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if math.isfinite(v) else 0.0

    keep = ("token_address", "symbol", "token_ath_mc", "market_cap", "is_open",
            "create_timestamp", "launchpad_platform", "holders")
    top = sorted(tokens, key=ath, reverse=True)[:5]
    me = next((t for t in tokens if token and str(t.get("token_address")) == token), None)
    return {
        "inner_count": body.get("inner_count"),
        "open_count": body.get("open_count"),
        "open_ratio": body.get("open_ratio"),
        "last_create_timestamp": body.get("last_create_timestamp"),
        "creator_ath_info": body.get("creator_ath_info"),
        "listed": len(tokens),
        "listed_open": sum(1 for t in tokens if t.get("is_open") in (True, 1, "1", "true")),
        "listed_max_ath_mc": max((ath(t) for t in tokens), default=None),
        "top_ath": [{k: t.get(k) for k in keep} for t in top],
        "self": {k: me.get(k) for k in keep} if me else None,
    }


# ------------------------------------------------------------------------------ fetching

_client_lock = threading.Lock()
_client: httpx.Client | None = None


def _http() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers=REQUEST_HEADERS,
                timeout=HTTP_TIMEOUT_S,
                follow_redirects=False,
            )
        return _client


def binance_request(
    endpoint: str,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    priority: Priority = Priority.DISCOVERY,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """One Binance Web3 read through the limiter. Raises; :func:`poll_feed` classifies."""
    with guarded(BINANCE_PROVIDER, endpoint, priority, conn=conn):
        resp = (client or _http()).request(
            method, url, params=params, json=body, headers=REQUEST_HEADERS, timeout=HTTP_TIMEOUT_S
        )
        if resp.status_code == 429:
            retry = _int(resp.headers.get("retry-after"))
            raise BinanceRateLimited("http 429", float(retry) if retry else None)
        if resp.status_code >= 400:
            raise BinanceError(f"http {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise BinanceError("body was not JSON") from exc
        return binance_data(payload)


def _fetch_meme_rush(stage: str, chain: Chain, client: Any, conn: Any) -> list[FeedRow]:
    body = {"chainId": BINANCE_CHAIN_IDS[chain], "rankType": MEME_RUSH_STAGES[stage], "limit": MEME_RUSH_LIMIT}
    data = binance_request("web3.meme_rush", MEME_RUSH_URL, method="POST", body=body, client=client, conn=conn)
    return parse_meme_rush(data, chain, stage)


def _fetch_topic_rush(list_name: str, chain: Chain, client: Any, conn: Any) -> list[FeedRow]:
    params = {"chainId": BINANCE_CHAIN_IDS[chain], "rankType": TOPIC_RUSH_LISTS[list_name], "sort": 10, "asc": "false"}
    data = binance_request("web3.topic_rush", TOPIC_RUSH_URL, params=params, client=client, conn=conn)
    return parse_topic_rush(data, chain, list_name)


def _fetch_smart_money(chain: Chain, client: Any, conn: Any) -> list[FeedRow]:
    body = {"chainId": BINANCE_CHAIN_IDS[chain], "page": 1, "pageSize": SMART_MONEY_PAGE_SIZE}
    data = binance_request("web3.smart_money", SMART_MONEY_URL, method="POST", body=body, client=client, conn=conn)
    return parse_smart_money(data, chain)


def _binance_fetcher(name: str) -> Callable[[Chain, Any, Any], list[FeedRow]]:
    parts = name.split(":")
    if parts[1] == "meme_rush":
        return lambda chain, client, conn: _fetch_meme_rush(parts[2], chain, client, conn)
    if parts[1] == "topic_rush":
        return lambda chain, client, conn: _fetch_topic_rush(parts[2], chain, client, conn)
    if parts[1] == "smart_money":
        return _fetch_smart_money
    raise KeyError(name)


def _failure_status(exc: BaseException) -> str:
    if isinstance(exc, BinanceRateLimited):
        return "rate_limited"
    if isinstance(exc, RateLimited):  # our own limiter refused; nothing was sent
        return "limiter"
    return "error"


_STATUS_WEIGHT = {"rate_limited": 3, "error": 2, "limiter": 1}


def _gmgn_status(note: str | None) -> str:
    n = (note or "").lower()
    if "limiter refused" in n:
        return "limiter"
    if "429" in n or "rate limit" in n:
        return "rate_limited"
    return "error"


# -------------------------------------------------------------------------------- writing


class _SeenCache:
    """Keys already written this process, so a quiet poll opens no write transaction.

    Value is whether the stored row has a narrative: a row without one is re-offered once
    so a narrative that arrives later can fill it. Bounded; the UNIQUE key is the truth.
    """

    def __init__(self, max_items: int = 50_000) -> None:
        self._d: OrderedDict[tuple[str, str, str, int], bool] = OrderedDict()
        self._max = max_items

    def wants(self, key: tuple[str, str, str, int], has_narrative: bool) -> bool:
        stored = self._d.get(key)
        return stored is None or (not stored and has_narrative)

    def mark(self, key: tuple[str, str, str, int], has_narrative: bool) -> None:
        self._d[key] = self._d.get(key, False) or has_narrative
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)


def window_start(observed_ms: int, window_ms: int) -> int:
    return observed_ms - observed_ms % max(1, window_ms)


def write_rows(
    conn: sqlite3.Connection,
    rows: Sequence[FeedRow],
    *,
    observed_ms: int,
    seen: _SeenCache | None = None,
) -> int:
    """Insert new (feed, chain, token, window) rows; fill a NULL narrative once.

    Returns rows inserted or filled. First sightings go to ``alpha_feed_first_seen`` with
    INSERT OR IGNORE, so the earliest one is permanent. Enrichment feeds never do.
    """
    todo: list[tuple[FeedRow, int]] = []
    batch_keys: set[tuple[str, str, str, int]] = set()
    for r in rows:
        spec = FEEDS.get(r.feed)
        win = window_start(observed_ms, spec.window_ms if spec else HOUR_MS)
        key = (r.feed, r.chain, r.token_address, win)
        has_narr = r.narrative is not None
        if key in batch_keys or (seen is not None and not seen.wants(key, has_narr)):
            continue
        batch_keys.add(key)
        todo.append((r, win))
    if not todo:
        return 0
    changed = 0
    with tx(conn):
        for r, win in todo:
            cur = conn.execute(
                "INSERT INTO alpha_feed_rows (feed, chain, token_address, symbol, name, narrative, "
                " rank, context, source_ts_ms, window_ms, observed_ms, raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(feed, chain, token_address, window_ms) DO UPDATE SET "
                " narrative=excluded.narrative, raw_json=excluded.raw_json "
                "WHERE alpha_feed_rows.narrative IS NULL AND excluded.narrative IS NOT NULL",
                (r.feed, r.chain, r.token_address, r.symbol, r.name, r.narrative, r.rank,
                 r.context, r.source_ts_ms, win, observed_ms, cap_raw(r.raw)),
            )
            changed += max(0, cur.rowcount)
            spec = FEEDS.get(r.feed)
            if spec is None or spec.sighting:
                narr_ms = observed_ms if r.narrative is not None else None
                first = conn.execute(
                    "INSERT OR IGNORE INTO alpha_feed_first_seen (feed, chain, token_address, "
                    " first_seen_ms, first_rank, first_source_ts_ms, symbol, narrative, narrative_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.feed, r.chain, r.token_address, observed_ms, r.rank, r.source_ts_ms,
                     r.symbol, r.narrative, narr_ms),
                )
                if first.rowcount == 0 and r.narrative is not None:
                    conn.execute(
                        "UPDATE alpha_feed_first_seen SET narrative=?, narrative_ms=? "
                        "WHERE feed=? AND chain=? AND token_address=? AND narrative IS NULL",
                        (r.narrative, observed_ms, r.feed, r.chain, r.token_address),
                    )
    if seen is not None:
        for r, win in todo:
            seen.mark((r.feed, r.chain, r.token_address, win), r.narrative is not None)
    return changed


def prune_rows(conn: sqlite3.Connection, *, older_than_ms: int, batch: int = 5000, max_batches: int = 20) -> int:
    """Delete ``alpha_feed_rows`` older than the cut, in short bounded transactions."""
    total = 0
    for _ in range(max_batches):
        with tx(conn):
            cur = conn.execute(
                "DELETE FROM alpha_feed_rows WHERE row_id IN (SELECT row_id FROM alpha_feed_rows "
                "WHERE observed_ms < ? ORDER BY observed_ms LIMIT ?)",
                (older_than_ms, batch),
            )
        n = max(0, cur.rowcount)
        total += n
        if n < batch:
            break
    return total


# -------------------------------------------------------------------------------- polling


@dataclass
class PollerState:
    next_due: dict[str, float] = field(default_factory=dict)
    backoff_s: dict[str, float] = field(default_factory=dict)
    seen: _SeenCache = field(default_factory=_SeenCache)
    #: dev wallet -> monotonic time of its last created-tokens lookup (24 h memory).
    devs_done: OrderedDict[str, float] = field(default_factory=OrderedDict)
    turn: int = 0


def next_delay_s(
    spec: FeedSpec, status: str, prev_backoff_s: float, retry_after_s: float | None = None
) -> tuple[float, float]:
    """``(seconds until the next poll, backoff to remember)`` after one poll.

    Rate limits and errors double from :data:`BACKOFF_MIN_S` to :data:`BACKOFF_MAX_S` and
    never come sooner than the feed's own interval or the vendor's ``Retry-After``; success
    resets. A limiter refusal is our own pacing, not the vendor's, so it waits one interval
    and does not escalate.
    """
    if status in ("rate_limited", "error"):
        backoff = min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, prev_backoff_s * 2.0))
        vendor_wait = min(RETRY_AFTER_MAX_S, max(0.0, retry_after_s or 0.0))
        return max(spec.interval_s, backoff, vendor_wait), backoff
    if status == "limiter":
        return spec.interval_s, prev_backoff_s
    return spec.interval_s, 0.0


def pending_devs(
    conn: sqlite3.Connection, chain: Chain, *, since_ms: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Recently surfaced tokens whose dev has not been looked up for that token yet."""
    marks = ",".join("?" for _ in DEV_SOURCE_FEEDS)
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT r.chain, r.token_address, r.symbol, r.name, r.context AS dev "
            f"FROM alpha_feed_rows r WHERE r.observed_ms >= ? AND r.chain = ? "
            f"AND r.feed IN ({marks}) AND r.context IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM alpha_feed_rows c WHERE c.chain = r.chain "
            f"  AND c.token_address = r.token_address AND c.feed = 'gmgn:created_tokens') "
            f"ORDER BY r.observed_ms DESC LIMIT ?",
            (since_ms, chain.value, *DEV_SOURCE_FEEDS, limit),
        ).fetchall()
    ]


#: Seconds a gmgn read may wait out OUR limiter's spacing (never a provider 429): the
#: ingest gmgn sweep shares the bucket and would otherwise bounce these at random.
GMGN_SLOT_WAIT_S = 3.0


def _poll_created_tokens(spec: FeedSpec, state: PollerState, conn: Any, gmgn: Any) -> PollResult:
    """One dev lookup per poll, newest surfaced token first, chains taken in turn."""
    c = conn or get_conn()
    chains = list(spec.chains)
    now_mono = time.monotonic()
    for dev, at in list(state.devs_done.items()):
        if now_mono - at > DAY_MS / 1000:
            state.devs_done.pop(dev, None)
    pick: dict[str, Any] | None = None
    chain = chains[0]
    for k in range(len(chains)):
        chain = chains[(state.turn + k) % len(chains)]
        pending = pending_devs(c, chain, since_ms=now_ms() - 6 * HOUR_MS, limit=50)
        pick = next((p for p in pending if p["dev"] not in state.devs_done), None)
        if pick is not None:
            break
    state.turn += 1
    if pick is None:
        return PollResult(spec.name, "skipped", detail="no pending dev")
    got = gmgn.portfolio_created_tokens(
        pick["dev"], chain, priority=Priority.DISCOVERY, wait_for_slot_s=GMGN_SLOT_WAIT_S, conn=conn
    )
    status = "ok" if got.ok else _gmgn_status(got.receipt.note)
    if status != "limiter":  # a refused reservation sent nothing; try this dev again
        state.devs_done[pick["dev"]] = now_mono
    if not got.ok:
        return PollResult(spec.name, status, detail=(got.receipt.note or "")[:200])
    summary = summarize_created_tokens(got.data, pick["token_address"])
    row = FeedRow(
        feed=spec.name, chain=chain.value, token_address=pick["token_address"],
        symbol=pick.get("symbol"), name=pick.get("name"), narrative=None, rank=None,
        context=pick["dev"], source_ts_ms=_ms(summary.get("last_create_timestamp")),
        raw={"dev": pick["dev"], **summary},
    )
    return PollResult(spec.name, "ok", rows=[row])


def _fetch(spec: FeedSpec, state: PollerState, conn: Any, client: Any, gmgn: Any) -> PollResult:
    parts = spec.name.split(":")
    rows: list[FeedRow] = []
    if spec.name.startswith("binance:"):
        # One chain failing must not discard another chain's rows. Chains are spaced by
        # the limiter's minimum interval: DISCOVERY never waits inside `reserve`, so two
        # back-to-back calls would have the second refused by our own pacing.
        fetch = _binance_fetcher(spec.name)
        failures: list[tuple[str, BaseException]] = []
        for i, chain in enumerate(spec.chains):
            if i:
                time.sleep(_pace_s(spec.provider))
            try:
                rows += fetch(chain, client, conn)
            except (RateLimited, BinanceRateLimited, BinanceError, httpx.HTTPError) as exc:
                failures.append((chain.value, exc))
        if not failures:
            return PollResult(spec.name, "ok" if rows else "empty", rows=rows)
        status = max((_failure_status(e) for _, e in failures), key=_STATUS_WEIGHT.__getitem__)
        detail = "; ".join(f"{c}: {type(e).__name__}: {e}" for c, e in failures)[:200]
        retry = max(
            (e.retry_after_s or 0.0 for _, e in failures if isinstance(e, BinanceRateLimited)),
            default=0.0,
        )
        return PollResult(spec.name, status, rows=rows, detail=detail, retry_after_s=retry or None)
    if spec.name.startswith("gmgn:hot_searches:"):
        got = gmgn.market_hot_searches(
            list(spec.chains), interval=parts[2], limit=HOT_SEARCH_LIMIT,
            priority=Priority.DISCOVERY, ttl_s=0.0, wait_for_slot_s=GMGN_SLOT_WAIT_S, conn=conn,
        )
        if not got.ok:
            return PollResult(spec.name, _gmgn_status(got.receipt.note), detail=(got.receipt.note or "")[:200])
        rows = parse_hot_searches(got.data, parts[2])
        return PollResult(spec.name, "ok" if rows else "empty", rows=rows)
    if spec.name == "gmgn:created_tokens":
        return _poll_created_tokens(spec, state, conn, gmgn)
    raise KeyError(spec.name)


def _provider_error(conn: Any, spec: FeedSpec, status: str, detail: str) -> None:
    try:
        emit(
            EventKind.PROVIDER_ERROR,
            {"provider": spec.provider, "feed": spec.name, "status": status,
             "error": detail[:200], "observation_only": True},
            level="warn",
            dedupe_key=f"provider_error:alpha_feeds:{spec.name}:{status}:{digest(detail)}:{now_ms() // 600_000}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks a poll
        log.debug("alpha_feeds: could not record provider error: %s", type(exc).__name__)


def poll_feed(
    spec: FeedSpec,
    *,
    state: PollerState | None = None,
    conn: Any = None,
    client: httpx.Client | None = None,
    gmgn: Any = None,
    write: bool = True,
) -> PollResult:
    """Poll one feed once. Never raises. ``write=False`` parses and returns, writes nothing
    (the limiter still books the call, as every call must)."""
    st = state or PollerState()
    if gmgn is None:
        from kaiba.providers import gmgn_cli as gmgn
    try:
        result = _fetch(spec, st, conn, client, gmgn)
    except RateLimited as exc:  # our own limiter, before anything was sent
        result = PollResult(spec.name, "limiter", detail=str(exc)[:200])
    except BinanceRateLimited as exc:
        result = PollResult(spec.name, "rate_limited", detail=str(exc)[:200],
                            retry_after_s=exc.retry_after_s)
    except (BinanceError, httpx.HTTPError) as exc:
        result = PollResult(spec.name, "error", detail=f"{type(exc).__name__}: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001 - a poll is a sample, not a reason to crash
        log.exception("alpha_feeds %s failed", spec.name)
        result = PollResult(spec.name, "error", detail=f"{type(exc).__name__}: {exc}"[:200])
    # gmgn_cli records its own provider errors; only the Binance path needs one here.
    if result.status in ("rate_limited", "error") and spec.provider == BINANCE_PROVIDER:
        _provider_error(conn, spec, result.status, result.detail)
    if write and result.rows:
        try:
            result.written = write_rows(conn or get_conn(), result.rows, observed_ms=now_ms(), seen=st.seen)
        except sqlite3.Error as exc:
            log.warning("alpha_feeds %s: write failed: %s", spec.name, exc)
            result.detail = f"write failed: {exc}"[:200]
    return result


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _pace_s(provider: str) -> float:
    """Gap between two calls to one provider: its limiter minimum interval, plus slack."""
    try:
        from kaiba.core.limiter import limits_for

        return limits_for(provider).min_interval_ms / 1000.0 + 0.1
    except Exception:  # noqa: BLE001 - an unreadable budget must not stop the poller
        return 1.2


async def run(
    *,
    stop: asyncio.Event,
    feeds: Iterable[str] | None = None,
    status_name: str | None = None,
    retention_days: float | None = RETENTION_DAYS,
    tick_s: float = 5.0,
    conn: Any = None,
    gmgn: Any = None,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Poll each named feed on its own cadence until ``stop``. Observation only."""
    names = list(feeds or DEFAULT_FEEDS)
    unknown = [n for n in names if n not in FEEDS]
    if unknown:
        raise KeyError(f"unknown alpha feed(s) {unknown}; known: {sorted(FEEDS)}")
    state = PollerState()
    start = clock()
    for i, name in enumerate(names):
        state.next_due[name] = start + i * 2.0  # stagger the first sweep
    last_prune = start
    last_call: dict[str, float] = {}
    while not stop.is_set():
        for name in names:
            if stop.is_set():
                return
            if state.next_due.get(name, 0.0) > clock():
                continue
            spec = FEEDS[name]
            gap = _pace_s(spec.provider) - (clock() - last_call.get(spec.provider, -1e9))
            if gap > 0:
                await _wait(stop, gap)
            result = await asyncio.to_thread(poll_feed, spec, state=state, conn=conn, client=client, gmgn=gmgn)
            last_call[spec.provider] = clock()  # after: a multi-chain poll ends on a call
            delay, state.backoff_s[name] = next_delay_s(
                spec, result.status, state.backoff_s.get(name, 0.0), result.retry_after_s
            )
            state.next_due[name] = clock() + delay
            if result.status not in ("ok", "empty", "skipped"):
                log.info("alpha_feeds %s: %s (%s); next poll in %.0fs", name, result.status, result.detail, delay)
            if result.written and status_name:
                with contextlib.suppress(Exception):
                    from kaiba.ingest.runner import note_events

                    note_events(status_name, result.written, conn)
        if retention_days and clock() - last_prune >= PRUNE_EVERY_S:
            last_prune = clock()
            n = await asyncio.to_thread(_prune_job, conn, retention_days)
            if n:
                log.info("alpha_feeds: pruned %d rows older than %.0f days", n, retention_days)
        await _wait(stop, tick_s)


def _prune_job(conn: Any, retention_days: float) -> int:
    """Runs in a worker thread, on that thread's own connection."""
    try:
        return prune_rows(conn or get_conn(), older_than_ms=now_ms() - int(retention_days * DAY_MS))
    except sqlite3.Error as exc:
        log.warning("alpha_feeds: prune failed: %s", exc)
        return 0


# ------------------------------------------------------------------------------ analysis


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def our_first_seen(conn: sqlite3.Connection, chain: str, addresses: Iterable[str]) -> dict[str, int]:
    """Our earliest sighting per token: min(tokens.first_seen_ms, snipe_observations.seen_ms).

    Each statement is short and fully consumed, so no read transaction outlives it.
    """
    addrs = list(dict.fromkeys(a for a in addresses if a))
    out: dict[str, int] = {}
    has_snipe = _table_exists(conn, "snipe_observations")
    for chunk in _chunks(addrs, 400):
        marks = ",".join("?" for _ in chunk)
        for addr, ts in conn.execute(
            f"SELECT address, first_seen_ms FROM tokens WHERE chain=? AND address IN ({marks})",
            (chain, *chunk),
        ).fetchall():
            if ts is not None:
                out[addr] = min(int(ts), out.get(addr, int(ts)))
        if has_snipe:
            for addr, ts in conn.execute(
                f"SELECT token, MIN(seen_ms) FROM snipe_observations WHERE chain=? "
                f"AND token IN ({marks}) GROUP BY token",
                (chain, *chunk),
            ).fetchall():
                if ts is not None:
                    out[addr] = min(int(ts), out.get(addr, int(ts)))
    return out


def _pct(sorted_vals: Sequence[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[idx]


#: A token first seen this soon after a feed's recording began was probably already on
#: the list: its first_seen is the recorder's start, not the feed's. Twice the feed's
#: interval, and never under ten minutes.
CENSOR_MIN_MS = 10 * 60_000


def lead_time_report(
    conn: sqlite3.Connection, since_ms: int, *, until_ms: int | None = None
) -> list[dict[str, Any]]:
    """Per (feed, chain): how early each feed surfaced tokens WE also saw. Read-only.

    ``lead_s`` = our first sighting minus the feed's first sighting; POSITIVE means the
    feed surfaced the token before we did. ``vendor_lead_s`` uses the vendor's own event
    time instead (signal trigger, topic creation, token launch), which is the honest
    clock for a list whose rows predate our recorder. ``unmatched`` tokens are ones we
    never saw at all -- a coverage gap, reported rather than dropped. ``censored`` tokens
    were already listed when the recorder started and are left out of every lead figure.
    Forward returns are the mooner study's job, joined on (chain, token_address).

    Every statement is short and fully consumed; no read transaction outlives one.
    """
    hi = until_ms if until_ms is not None else now_ms() + 1
    starts = {
        (feed, chain): int(ts)
        for feed, chain, ts in conn.execute(
            "SELECT feed, chain, MIN(first_seen_ms) FROM alpha_feed_first_seen GROUP BY feed, chain"
        ).fetchall()
    }
    rows = conn.execute(
        "SELECT feed, chain, token_address, first_seen_ms, first_source_ts_ms "
        "FROM alpha_feed_first_seen WHERE first_seen_ms >= ? AND first_seen_ms < ?",
        (since_ms, hi),
    ).fetchall()
    by_chain: dict[str, set[str]] = {}
    for _feed, chain, addr, _ts, _src in rows:
        by_chain.setdefault(chain, set()).add(addr)
    ours = {chain: our_first_seen(conn, chain, sorted(addrs)) for chain, addrs in by_chain.items()}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for feed, chain, addr, ts, src in rows:
        g = groups.setdefault((feed, chain), {"surfaced": 0, "censored": 0, "unmatched": 0,
                                              "leads": [], "vendor": []})
        g["surfaced"] += 1
        if int(ts) < starts.get((feed, chain), 0) + _censor_guard_ms(feed):
            g["censored"] += 1
            continue
        mine = ours.get(chain, {}).get(addr)
        if mine is None:
            g["unmatched"] += 1
            continue
        g["leads"].append((mine - int(ts)) / 1000.0)
        if src is not None:
            g["vendor"].append((mine - int(src)) / 1000.0)
    report: list[dict[str, Any]] = []
    for (feed, chain), g in sorted(groups.items()):
        leads, vendor = sorted(g["leads"]), sorted(g["vendor"])
        report.append(
            {
                "feed": feed,
                "chain": chain,
                "surfaced": g["surfaced"],
                "censored": g["censored"],
                "matched": len(leads),
                "unmatched": g["unmatched"],
                "feed_first": sum(1 for v in leads if v > 0),
                "median_lead_s": _pct(leads, 0.5),
                "p25_lead_s": _pct(leads, 0.25),
                "p75_lead_s": _pct(leads, 0.75),
                "median_vendor_lead_s": _pct(vendor, 0.5),
            }
        )
    return report

# ---------------------------------------------------------------- point-in-time features


@dataclass(frozen=True)
class Sighting:
    """One feed's first surfacing of one token, as recorded in ``alpha_feed_first_seen``."""

    feed: str
    first_seen_ms: int
    first_rank: int | None
    source_ts_ms: int | None
    narrative: str | None
    narrative_ms: int | None
    #: The feed's recording had only just begun: the token was probably already listed, so
    #: ``first_seen_ms`` is an upper bound on when the feed surfaced it (lead is a floor).
    censored: bool


#: ``(sql, params) -> rows``. ``signal_audit.Reader.q`` has this shape, and so does
#: ``lambda sql, p: conn.execute(sql, p).fetchall()``.
Query = Callable[[str, Sequence[Any]], Sequence[Sequence[Any]]]


def _censor_guard_ms(feed: str) -> int:
    spec = FEEDS.get(feed)
    return max(CENSOR_MIN_MS, int(2 * (spec.interval_s if spec else 300.0) * 1000))


def _sighting(feed: Any, ts: Any, rank: Any, src: Any, narr: Any, narr_ms: Any, start: int | None) -> Sighting:
    ts_i = int(ts)
    return Sighting(
        feed=str(feed), first_seen_ms=ts_i, first_rank=_int(rank), source_ts_ms=_int(src),
        narrative=narr, narrative_ms=_int(narr_ms),
        censored=ts_i < (ts_i if start is None else start) + _censor_guard_ms(str(feed)),
    )


def load_sightings(
    query: Query, chains: Sequence[str], *, before_ms: int | None = None
) -> dict[tuple[str, str], list[Sighting]]:
    """Every recorded sighting, by ``(chain, token_address)``, for :func:`sightings_before`.

    One bulk read for the daily audit (the table holds one row per feed and token, so it
    stays small). ``before_ms`` drops sightings at or after it, which is only a size cut:
    the point-in-time rule is applied per signal by :func:`sightings_before`. Returns ``{}``
    when migration 036 is not applied -- "unmeasured", never "not surfaced".
    """
    if not chains:
        return {}
    marks = ",".join("?" for _ in chains)
    try:
        starts = {
            (str(feed), str(chain)): int(ts)
            for feed, chain, ts in query(
                f"SELECT feed, chain, MIN(first_seen_ms) FROM alpha_feed_first_seen "
                f"WHERE chain IN ({marks}) GROUP BY feed, chain", list(chains),
            )
        }
        hi = before_ms if before_ms is not None else 2**62
        rows = query(
            f"SELECT feed, chain, token_address, first_seen_ms, first_rank, first_source_ts_ms, "
            f"narrative, narrative_ms FROM alpha_feed_first_seen "
            f"WHERE chain IN ({marks}) AND first_seen_ms < ?", [*chains, hi],
        )
    except sqlite3.OperationalError:
        return {}
    out: dict[tuple[str, str], list[Sighting]] = {}
    for feed, chain, addr, ts, rank, src, narr, narr_ms in rows:
        start = starts.get((str(feed), str(chain)))
        out.setdefault((str(chain), str(addr)), []).append(
            _sighting(feed, ts, rank, src, narr, narr_ms, start)
        )
    return out


def sightings_before(sightings: Iterable[Sighting], t0_ms: int) -> dict[str, dict[str, Any]]:
    """Point in time: per feed, whether and when it surfaced the token STRICTLY before t0.

    Only what was known at ``t0_ms`` is returned. A sighting observed at or after t0 is
    absent (the feed "had not surfaced it"); a narrative that arrived at or after t0 is
    withheld even when the sighting itself was earlier. ``lead_s`` is t0 minus our
    observation of the feed (how long before the signal the feed had it); for a censored
    sighting it is a floor. Vendor times (``source_ts_ms``) are reported as the vendor
    gave them and are never used to decide visibility.
    """
    out: dict[str, dict[str, Any]] = {}
    for s in sightings:
        if s.first_seen_ms >= t0_ms:
            continue
        narr_known = s.narrative is not None and s.narrative_ms is not None and s.narrative_ms < t0_ms
        out[s.feed] = {
            "first_seen_ms": s.first_seen_ms,
            "lead_s": (t0_ms - s.first_seen_ms) / 1000.0,
            "first_rank": s.first_rank,
            "source_ts_ms": s.source_ts_ms,
            "narrative": s.narrative if narr_known else None,
            "censored": s.censored,
        }
    return out


def feed_features(sightings: Iterable[Sighting], t0_ms: int) -> dict[str, Any]:
    """Flat audit features from :func:`sightings_before`: counts, longest lead, narrative.

    ``alpha_feeds_n`` is how many distinct feeds had surfaced the token before t0;
    ``alpha_lead_s`` the longest of their leads (None when none had); ``alpha_narrative``
    whether any narrative text was known before t0. Per-feed ``alpha:<feed>`` flags are 1/0.
    """
    hits = sightings_before(sightings, t0_ms)
    feats: dict[str, Any] = {
        "alpha_feeds_n": len(hits),
        "alpha_lead_s": max((h["lead_s"] for h in hits.values()), default=None),
        "alpha_narrative": int(any(h["narrative"] for h in hits.values())),
    }
    for name, spec in FEEDS.items():
        if spec.sighting:
            feats[f"alpha:{name}"] = int(name in hits)
    return feats


def feed_sightings_at(
    conn: sqlite3.Connection, chain: str, token_address: str, t0_ms: int
) -> dict[str, dict[str, Any]]:
    """Single-token form of :func:`load_sightings` + :func:`sightings_before`.

    Two short statements, both fully consumed; no read transaction outlives the call.
    """
    addr = norm_address(chain, token_address) or token_address
    try:
        starts = {
            str(f): int(ts)
            for f, ts in conn.execute(
                "SELECT feed, MIN(first_seen_ms) FROM alpha_feed_first_seen WHERE chain=? GROUP BY feed",
                (chain,),
            ).fetchall()
        }
        rows = conn.execute(
            "SELECT feed, first_seen_ms, first_rank, first_source_ts_ms, narrative, narrative_ms "
            "FROM alpha_feed_first_seen WHERE chain=? AND token_address=? AND first_seen_ms < ?",
            (chain, addr, int(t0_ms)),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    sightings = [
        _sighting(feed, ts, rank, src, narr, narr_ms, starts.get(str(feed)))
        for feed, ts, rank, src, narr, narr_ms in rows
    ]
    return sightings_before(sightings, t0_ms)


__all__ = [
    "BINANCE_FEEDS",
    "DEFAULT_FEEDS",
    "FEEDS",
    "GMGN_FEEDS",
    "BinanceError",
    "BinanceRateLimited",
    "FeedRow",
    "FeedSpec",
    "PollResult",
    "PollerState",
    "Sighting",
    "binance_data",
    "binance_request",
    "cap_raw",
    "feed_features",
    "feed_sightings_at",
    "lead_time_report",
    "load_sightings",
    "next_delay_s",
    "our_first_seen",
    "parse_hot_searches",
    "parse_meme_rush",
    "parse_smart_money",
    "parse_topic_rush",
    "pending_devs",
    "poll_feed",
    "prune_rows",
    "run",
    "sightings_before",
    "summarize_created_tokens",
    "write_rows",
]
