"""Codex.io (formerly Defined.fi) GraphQL adapter: prices, token screens, swap tape.

Research provider only. Nothing in the engine, the watchdog, the lanes or paper pricing
calls this module; wiring it into any of them is a separate, measured decision.

**The plan, MEASURED 2026-10-21 (docs + six probes from the box).** The key is on the
"Almost free" plan:

* **10,000 compute units (CU) per rolling 30-day window**, 5 requests/second, 1 key, no
  burst/bucket figures published. No auto-scale: at 100% every key is paused and requests
  come back **HTTP 403** (docs.codex.io/concepts/compute-units). Codex emails at 50/80/100%.
* Usage is metered in CU, not requests. Every query pays a base cost (``getTokenPrices`` 4,
  ``filterTokens`` 5, ``getTokenEvents`` 5, ``getTokenEventsForMaker`` 10,
  ``listPairsWithMetadataForToken`` 4) plus tiny per-object costs (``Price`` 0.004,
  ``EnhancedToken`` 0.004, ``Pair`` 0.0004, ``Price.liquidityUsd`` 0.00025 ...).
  Errored root fields are not charged.
* **Usage is reported per request only**, by the free ``usageData`` root field, in
  micro-CU strings (``"14042000"`` = 14.042 CU). There is no query for the cumulative
  monthly total; it is only on the dashboard. So this module adds ``usageData`` to every
  query and keeps its OWN rolling 30-day counter in ``kv``, which is the quota guard.
* No response headers carry rate or quota state (checked: none on a 200).
* Rate limit: HTTP 429 ``TOO_MANY_REQUESTS``, ``Retry-After`` when known.
* Websockets / subscriptions / webhooks: **not on this plan** (pricing page).
* Plan-gated, MEASURED as ``NOT_AUTHORIZED: please upgrade your plan`` (HTTP 200 with a
  GraphQL error): ``tokenTopTraders``, ``filterTokenWallets``. Documented Growth-only:
  ``balances``, ``holders``, ``filterWallets``, wallet charts / PnL. So there is **no wallet
  PnL or holdings helper here**.
* **NFT data is gone**: ``getNftEvents`` still validates but answers ``Failed to fetch from
  Subgraph 'nfts'`` (Codex removed NFT coverage on 2026-03-31). No NFT helper here.
* Works on the free plan, MEASURED: ``getTokenPrices`` (with ``liquidityUsd``),
  ``filterTokens``, ``listPairsWithMetadataForToken``, ``getTokenEvents``,
  ``getTokenEventsForMaker`` -- on robinhood 4663, base 8453, eth 1, bsc 56 and sol.

At ~4.1 CU per 25-token price call the whole 10,000 CU buys about 2,400 calls a month, i.e.
one every ~18 minutes. That is a cross-check budget, not a price feed: it can audit a
sample of paper fills, it cannot price a book every few seconds.

**Quota guard.** Before a request leaves, its worst-case cost is RESERVED against a
rolling 30-day ``kv`` counter in one transaction; over the ceiling it is refused locally
(``None`` + a ``provider.error`` event) and nothing is sent. After the answer the
reservation is settled to the ``usageData`` the server billed. The ceiling defaults to
8,000 CU (80% of the plan) and is overridden by ``KAIBA_CODEX_IO_CU_CEILING``. An HTTP 403
latches a 6-hour pause, because it means the plan is exhausted (perhaps by another holder
of the key, which this counter cannot see).

**Limiter.** Provider name ``codex-io``. No ``provider_budgets`` entry and no core default:
``limiter.limits_for`` falls back to ``Limits()`` (1 s interval, 2 in flight), which is
already under the plan's 5 rps. The monthly CU, not the rate, is the binding constraint.

Every public helper returns ``None`` on any failure and never raises into its caller. The
key is sent only as the ``Authorization`` header; it never enters a cache key, a cache
file, a receipt, a log line or an event.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import get_conn, tx
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, digest, now_ms
from kaiba.providers._http import cache_path, cache_read, post_json, redact_text

log = logging.getLogger(__name__)

PROVIDER = "codex-io"
GRAPHQL_URL = "https://graph.codex.io/graphql"

#: Codex network ids. The five chains we trade, MEASURED via ``getNetworks`` 2026-10-21.
NETWORK_IDS: dict[Chain, int] = {
    Chain.SOL: 1399811149,
    Chain.ETH: 1,
    Chain.BSC: 56,
    Chain.BASE: 8453,
    Chain.ROBINHOOD: 4663,
}
#: Other networks the same probe listed, for research screens beyond our chains.
EXTRA_NETWORK_IDS: dict[str, int] = {
    "abstract": 2741, "apechain": 33139, "monad": 143, "berachain": 80094, "hyperevm": 999,
}

#: Wrapped native per network, as Codex prices it. Robinhood's WETH is the address Codex's
#: own robinhood screen returned (MEASURED $2,452.32 on 2026-10-21, $54.9M liquidity).
WRAPPED_NATIVE: dict[Chain, str] = {
    Chain.SOL: "So11111111111111111111111111111111111111112",
    Chain.ETH: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    Chain.BSC: "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    Chain.BASE: "0x4200000000000000000000000000000000000006",
    Chain.ROBINHOOD: "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
}

# ----------------------------------------------------------------------------- quota

#: The plan's allowance, per rolling 30 days (docs.codex.io/concepts/compute-units).
PLAN_CU_PER_WINDOW = Decimal(10_000)
WINDOW_DAYS = 30
#: 80% of the plan: leaves room for the dashboard, a probe, and usage we cannot see.
DEFAULT_CU_CEILING = Decimal(8_000)
CEILING_ENV = "KAIBA_CODEX_IO_CU_CEILING"
#: How long an HTTP 403 (plan exhausted / key paused) stops every request.
PAUSE_AFTER_403_MS = 6 * 3_600_000

MICRO = 1_000_000
KV_DAY_PREFIX = "codex-io:cu_micro:"
KV_PAUSED_UNTIL = "codex-io:paused_until_ms"

#: Base cost per root field, CU (docs.codex.io/concepts/compute-unit-costs, 2026-10-16 list).
BASE_COST_CU: dict[str, Decimal] = {
    "getTokenPrices": Decimal(4),
    "filterTokens": Decimal(5),
    "listPairsWithMetadataForToken": Decimal(4),
    "getTokenEvents": Decimal(5),
    "getTokenEventsForMaker": Decimal(10),
}
#: Upper bound on the per-object + per-field cost of one returned row. The largest MEASURED
#: is 0.0045 (Price 0.004 + liquidityUsd 0.00025 + priceChange24 0.00025); 0.006 covers a
#: listPairs row (EnhancedToken + Pair + Exchange = 0.0048) with margin.
PER_ROW_BOUND_CU = Decimal("0.006")

# ------------------------------------------------------------------------- endpoints

EP_PRICES = "graphql.getTokenPrices"
EP_FILTER_TOKENS = "graphql.filterTokens"
EP_PAIRS = "graphql.listPairsWithMetadataForToken"
EP_EVENTS = "graphql.getTokenEvents"
EP_MAKER_EVENTS = "graphql.getTokenEventsForMaker"

#: Per-request limits from docs.codex.io/concepts/rate-limits.
MAX_PRICE_INPUTS = 25
MAX_FILTER_LIMIT = 200
MAX_EVENTS_LIMIT = 200
MAX_PAIRS_LIMIT = 250

DEFAULT_WAIT_FOR_SLOT_S = 5.0
#: httpx timeout of one Codex request (``_query``), unless the caller passes its own. One attempt
#: (``retries=1``), so this is the whole HTTP wait apart from connect + read being two phases.
HTTP_TIMEOUT_S = 15.0

_USAGE = "usageData { total { computeUnits count } }"
_ADDRESS = re.compile(r"^[A-Za-z0-9]{20,80}$")
_CURSOR = re.compile(r"^[A-Za-z0-9+/=_-]{1,4096}$")
_LAUNCHPAD = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")

#: ``TokenRankingAttribute`` values this module will send. trendingScore24 and createdAt
#: were MEASURED; the rest are documented members of the same enum.
RANKINGS: frozenset[str] = frozenset({
    "trendingScore", "trendingScore5m", "trendingScore1", "trendingScore4", "trendingScore12",
    "trendingScore24", "createdAt", "volume24", "liquidity", "marketCap", "holders",
})
DISPLAY_TYPES: frozenset[str] = frozenset({"Buy", "Sell", "Mint", "Burn", "Sync", "Collect"})


# ---------------------------------------------------------------------------- shapes


@dataclass(frozen=True)
class TokenPrice:
    """``getTokenPrices`` row: aggregate price across pools, with pooled liquidity."""

    network_id: int
    address: str
    price_usd: Decimal
    liquidity_usd: Decimal | None
    ts_s: int | None
    block_number: int | None
    change24: Decimal | None


@dataclass(frozen=True)
class PairInfo:
    """One pool for a token, from ``listPairsWithMetadataForToken``."""

    network_id: int
    pair_address: str
    protocol: str | None
    exchange: str | None
    exchange_address: str | None
    token0: str | None
    token1: str | None
    backing_token: str | None
    backing_symbol: str | None
    liquidity_usd: Decimal | None
    volume_usd: Decimal | None
    fee: int | None
    created_at_s: int | None


@dataclass(frozen=True)
class TokenCandidate:
    """One ``filterTokens`` result: a screenable token, never a recommendation."""

    network_id: int
    address: str
    name: str | None
    symbol: str | None
    decimals: int | None
    token_created_at_s: int | None
    pair_created_at_s: int | None
    creator: str | None
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    volume24_usd: Decimal | None
    holders: int | None
    market_cap_usd: Decimal | None
    trending_score: Decimal | None
    txn24: int | None
    buys24: int | None
    sells24: int | None
    change24: Decimal | None


@dataclass(frozen=True)
class TokenEvent:
    """One swap (or other pool event) with the wallet that made it."""

    network_id: int
    pair_address: str
    token_address: str | None
    tx_hash: str
    log_index: int | None
    block_number: int | None
    ts_s: int
    maker: str | None
    event_type: str | None
    side: str | None
    price_usd: Decimal | None
    value_usd: Decimal | None
    token_amount: Decimal | None
    value_base: Decimal | None
    wallet_age_s: int | None
    wallet_labels: tuple[str, ...] = ()
    sandwich: bool = False
    washtrade: bool = False


@dataclass(frozen=True)
class EventsPage:
    items: list[TokenEvent]
    cursor: str | None


@dataclass(frozen=True)
class QuotaStatus:
    spent_cu: Decimal
    ceiling_cu: Decimal
    plan_cu: Decimal
    window_days: int
    paused_until_ms: int

    @property
    def remaining_cu(self) -> Decimal:
        return max(Decimal(0), self.ceiling_cu - self.spent_cu)


@dataclass
class _Outcome:
    """Internal: the root field's data, or why there is none."""

    data: Any = None
    note: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- parsing


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return None if not d.is_finite() else d


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_prices(rows: Any) -> list[TokenPrice]:
    out: list[TokenPrice] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        price = _dec(r.get("priceUsd"))
        address, network = _str(r.get("address")), _int(r.get("networkId"))
        if price is None or price <= 0 or address is None or network is None:
            continue  # a zero price is not a price
        out.append(TokenPrice(
            network_id=network, address=address, price_usd=price,
            liquidity_usd=_dec(r.get("liquidityUsd")), ts_s=_int(r.get("timestamp")),
            block_number=_int(r.get("blockNumber")), change24=_dec(r.get("priceChange24")),
        ))
    return out


def parse_pairs(block: Any) -> list[PairInfo]:
    rows = block.get("results") if isinstance(block, dict) else None
    out: list[PairInfo] = []
    for r in rows if isinstance(rows, list) else []:
        pair = r.get("pair") if isinstance(r, dict) else None
        if not isinstance(pair, dict) or not _str(pair.get("address")):
            continue
        ex = r.get("exchange") if isinstance(r.get("exchange"), dict) else {}
        back = r.get("backingToken") if isinstance(r.get("backingToken"), dict) else {}
        out.append(PairInfo(
            network_id=_int(pair.get("networkId")) or 0, pair_address=pair["address"],
            protocol=_str(pair.get("protocol")), exchange=_str(ex.get("name")),
            exchange_address=_str(ex.get("address")), token0=_str(pair.get("token0")),
            token1=_str(pair.get("token1")), backing_token=_str(back.get("address")),
            backing_symbol=_str(back.get("symbol")), liquidity_usd=_dec(r.get("liquidity")),
            volume_usd=_dec(r.get("volume")), fee=_int(pair.get("fee")),
            created_at_s=_int(pair.get("createdAt")),
        ))
    # Codex does not sort these (docs); the deepest pool first is what a pricer wants.
    out.sort(key=lambda p: p.liquidity_usd if p.liquidity_usd is not None else Decimal(-1), reverse=True)
    return out


def parse_filter_tokens(block: Any) -> list[TokenCandidate]:
    rows = block.get("results") if isinstance(block, dict) else None
    out: list[TokenCandidate] = []
    for r in rows if isinstance(rows, list) else []:
        tok = r.get("token") if isinstance(r, dict) else None
        if not isinstance(tok, dict) or not _str(tok.get("address")):
            continue
        out.append(TokenCandidate(
            network_id=_int(tok.get("networkId")) or 0, address=tok["address"],
            name=_str(tok.get("name")), symbol=_str(tok.get("symbol")), decimals=_int(tok.get("decimals")),
            token_created_at_s=_int(tok.get("createdAt")), pair_created_at_s=_int(r.get("createdAt")),
            creator=_str(tok.get("creatorAddress")), price_usd=_dec(r.get("priceUSD")),
            liquidity_usd=_dec(r.get("liquidity")), volume24_usd=_dec(r.get("volume24")),
            holders=_int(r.get("holders")), market_cap_usd=_dec(r.get("marketCap")),
            trending_score=_dec(r.get("trendingScore")), txn24=_int(r.get("txnCount24")),
            buys24=_int(r.get("buyCount24")), sells24=_int(r.get("sellCount24")),
            change24=_dec(r.get("change24")),
        ))
    return out


def parse_events(block: Any) -> EventsPage | None:
    if not isinstance(block, dict):
        return None
    items: list[TokenEvent] = []
    for r in block.get("items") if isinstance(block.get("items"), list) else []:
        if not isinstance(r, dict):
            continue
        tx_hash, ts = _str(r.get("transactionHash")), _int(r.get("timestamp"))
        if tx_hash is None or ts is None:
            continue
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        # `quoteToken` names the pool side being PRICED: priceUsd is that token's price
        # (MEASURED: token0 for PONS/USDG on robinhood, token1 for WETH/KIMCHI on base).
        quote = r.get("quoteToken")
        token = _str(r.get("token0Address")) if quote == "token0" else (
            _str(r.get("token1Address")) if quote == "token1" else None)
        labels = r.get("labels") if isinstance(r.get("labels"), dict) else {}
        wl = r.get("walletLabels")
        items.append(TokenEvent(
            network_id=_int(r.get("networkId")) or 0, pair_address=_str(r.get("address")) or "",
            token_address=token, tx_hash=tx_hash, log_index=_int(r.get("logIndex")),
            block_number=_int(r.get("blockNumber")), ts_s=ts, maker=_str(r.get("maker")),
            event_type=_str(r.get("eventType")), side=_str(r.get("eventDisplayType")),
            price_usd=_dec(data.get("priceUsd")), value_usd=_dec(data.get("priceUsdTotal")),
            token_amount=_dec(data.get("amountNonLiquidityToken")),
            value_base=_dec(data.get("priceBaseTokenTotal")), wallet_age_s=_int(r.get("walletAge")),
            wallet_labels=tuple(x for x in wl if isinstance(x, str)) if isinstance(wl, list) else (),
            sandwich=labels.get("sandwich") is not None, washtrade=labels.get("washtrade") is not None,
        ))
    cursor = block.get("cursor")
    return EventsPage(items=items, cursor=cursor if isinstance(cursor, str) and cursor else None)


def parse_usage_micro(body: Any) -> int | None:
    """``usageData.total.computeUnits`` in micro-CU, or ``None`` when absent."""
    try:
        raw = body["data"]["usageData"]["total"]["computeUnits"]
    except (KeyError, TypeError):
        return None
    value = _int(raw)
    return value if value is not None and value >= 0 else None


# ----------------------------------------------------------------------------- quota


def ceiling_cu() -> Decimal:
    """The CU ceiling over the rolling window: env override, else 80% of the plan."""
    raw = os.environ.get(CEILING_ENV, "").strip()
    if raw:
        val = _dec(raw)
        if val is not None and val >= 0:
            return val
        log.warning("%s=%r is not a non-negative number; using the default", CEILING_ENV, raw)
    return DEFAULT_CU_CEILING


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _window_start_day(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000, UTC) - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")


def _spent_micro(c: sqlite3.Connection, at_ms: int) -> int:
    rows = c.execute(
        "SELECT value FROM kv WHERE key >= ? AND key <= ?",
        (KV_DAY_PREFIX + _window_start_day(at_ms), KV_DAY_PREFIX + _day(at_ms)),
    ).fetchall()
    return sum(_int(r[0]) or 0 for r in rows)


def _add_micro(c: sqlite3.Connection, day: str, delta: int) -> None:
    c.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
        "value = CAST(MAX(0, CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER)) AS TEXT), "
        "updated_ms = excluded.updated_ms",
        (KV_DAY_PREFIX + day, str(int(delta)), now_ms()),
    )


def _paused_until(c: sqlite3.Connection) -> int:
    row = c.execute("SELECT value FROM kv WHERE key=?", (KV_PAUSED_UNTIL,)).fetchone()
    return (_int(row[0]) or 0) if row else 0


def quota_status(conn: sqlite3.Connection | None = None) -> QuotaStatus:
    c = conn or get_conn()
    ts = now_ms()
    return QuotaStatus(
        spent_cu=Decimal(_spent_micro(c, ts)) / MICRO, ceiling_cu=ceiling_cu(),
        plan_cu=PLAN_CU_PER_WINDOW, window_days=WINDOW_DAYS, paused_until_ms=_paused_until(c),
    )


def _reserve(c: sqlite3.Connection, micro: int) -> tuple[str | None, str | None]:
    """Charge ``micro`` against the window in one transaction. ``(day, None)`` or ``(None, why)``."""
    ts = now_ms()
    ceiling_micro = int(ceiling_cu() * MICRO)
    with tx(c):
        paused = _paused_until(c)
        if paused > ts:
            return None, f"quota guard: paused after HTTP 403 until {paused}"
        spent = _spent_micro(c, ts)
        if spent + micro > ceiling_micro:
            return None, (f"quota guard: {spent / MICRO:.3f} CU spent + {micro / MICRO:.3f} estimated "
                          f"> ceiling {ceiling_micro / MICRO:.0f} CU per {WINDOW_DAYS}d")
        day = _day(ts)
        _add_micro(c, day, micro)
        # Housekeeping: day rows that left the window are dead weight in kv.
        c.execute("DELETE FROM kv WHERE key >= ? AND key < ?",
                  (KV_DAY_PREFIX, KV_DAY_PREFIX + _day(ts - (WINDOW_DAYS + 1) * 86_400_000)))
    return day, None


def _settle(c: sqlite3.Connection, day: str, reserved: int, actual: int) -> None:
    if actual == reserved:
        return
    try:
        with tx(c):
            _add_micro(c, day, actual - reserved)
    except sqlite3.Error as exc:  # the reservation stands; over-counting is the safe side
        log.debug("codex-io could not settle usage: %s", exc)


def _pause(c: sqlite3.Connection) -> None:
    try:
        with tx(c):
            c.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                "SET value = excluded.value, updated_ms = excluded.updated_ms",
                (KV_PAUSED_UNTIL, str(now_ms() + PAUSE_AFTER_403_MS), now_ms()),
            )
    except sqlite3.Error as exc:
        log.debug("codex-io could not latch the 403 pause: %s", exc)


def estimate_micro(root: str, rows: int) -> int:
    base = BASE_COST_CU.get(root, Decimal(10))
    return int((base + PER_ROW_BOUND_CU * max(0, int(rows))) * MICRO)


# --------------------------------------------------------------------------- request


def _report(endpoint: str, note: str, conn: Any = None) -> None:
    try:
        from kaiba.core import events as ev
        from kaiba.core.schemas import EventKind

        clean = redact_text(note)[:300]
        ev.emit(
            EventKind.PROVIDER_ERROR,
            {"provider": PROVIDER, "endpoint": endpoint, "detail": clean},
            level="warn",
            dedupe_key=f"provider_error:{PROVIDER}:{endpoint}:{clean[:60]}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a fetch
        log.debug("codex-io could not record provider error: %s", redact_text(str(exc)))


def _api_key() -> str:
    try:
        from kaiba.core.config import get_settings

        return (get_settings().codex_io_api_key or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _query(
    root: str,
    endpoint: str,
    selection: str,
    *,
    rows: int,
    priority: Priority,
    ttl_s: float,
    wait_for_slot_s: float,
    conn: sqlite3.Connection | None,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> _Outcome:
    """Send ``{ <selection> usageData }`` and return the ``root`` field. Never raises.

    ``timeout_s`` is httpx's per-phase timeout (connect, read) for the one attempt; a caller on a
    deadline (protection's corroboration, ``evm_pool_price.codex_backup_quote``) passes its own."""
    try:
        return _query_inner(root, endpoint, selection, rows=rows, priority=priority, ttl_s=ttl_s,
                            wait_for_slot_s=wait_for_slot_s, conn=conn, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - the contract is None, not an exception
        note = redact_text(f"{type(exc).__name__}: {exc}")[:300]
        _report(endpoint, note, conn)
        return _Outcome(note=note)


def _query_inner(root, endpoint, selection, *, rows, priority, ttl_s, wait_for_slot_s, conn,
                 timeout_s: float = HTTP_TIMEOUT_S) -> _Outcome:
    key = _api_key()
    if not key:
        note = "codex_io_api_key is not configured"
        _report(endpoint, note, conn)
        return _Outcome(note=note)
    c = conn or get_conn()
    query = "query { " + selection + " " + _USAGE + " }"
    cache_key = f"codex-io:{digest(query)}"  # never the key: it is a header, not part of the question
    estimate = estimate_micro(root, rows)

    if ttl_s > 0 and cache_read(PROVIDER, cache_key, ttl_s) is not None:
        reserved_day, refusal = None, None  # a fresh cache hit sends nothing and costs nothing
    else:
        reserved_day, refusal = _reserve(c, estimate)
        if refusal:
            _report(endpoint, refusal, c)
            return _Outcome(note=refusal)

    got = post_json(
        PROVIDER, endpoint, GRAPHQL_URL,
        json_body={"query": query},
        headers={"Authorization": key, "Content-Type": "application/json"},
        priority=priority, ttl_s=ttl_s, cache_key=cache_key, timeout_s=timeout_s, retries=1,
        wait_for_slot_s=wait_for_slot_s, conn=c,
    )
    served_from_cache = got.receipt.basis in (EvidenceBasis.CACHED, EvidenceBasis.STALE) and got.data is not None
    if reserved_day is None and not served_from_cache:
        # The cache file expired between the check and the read, so a request DID go out
        # unreserved. Charge it now, at the estimate, rather than lose it from the count.
        reserved_day = _day(now_ms())
        try:
            with tx(c):
                _add_micro(c, reserved_day, estimate)
        except sqlite3.Error as exc:
            log.debug("codex-io could not charge an unreserved call: %s", exc)
    if served_from_cache and reserved_day is not None:
        _settle(c, reserved_day, estimate, 0)

    if not got.ok:
        note = redact_text(got.receipt.note or "unavailable")[:300]
        if note.startswith("rate limited:") and reserved_day is not None:
            _settle(c, reserved_day, estimate, 0)  # our limiter refused: nothing was sent
        elif "403" in note:
            _pause(c)
        # _http already emitted provider_error for a transport failure.
        return _Outcome(note=note)

    body = got.data
    if not served_from_cache and reserved_day is not None:
        billed = parse_usage_micro(body)
        if billed is not None:
            _settle(c, reserved_day, estimate, billed)

    errors = body.get("errors") if isinstance(body, dict) else None
    errors = [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []
    data = body.get("data") if isinstance(body, dict) else None
    value = data.get(root) if isinstance(data, dict) else None
    if value is None:
        codes = sorted({str((e.get("extensions") or {}).get("code") or "") for e in errors} - {""})
        msg = "; ".join(str(e.get("message", ""))[:120] for e in errors[:2]) or "no data"
        note = redact_text(f"graphql {root}: {','.join(codes) or 'error'}: {msg}")[:300]
        if not served_from_cache and reserved_day is not None and parse_usage_micro(body) is None:
            _settle(c, reserved_day, estimate, 0)  # errored root fields are not billed (docs)
        try:  # a 200 carrying an error must not be served from cache for the whole TTL
            cache_path(PROVIDER, cache_key).unlink(missing_ok=True)
        except OSError:
            pass
        _report(endpoint, note, c)
        return _Outcome(note=note, errors=errors)
    return _Outcome(data=value, errors=errors)


# ------------------------------------------------------------------- input literals


def _lit_str(value: str, pattern: re.Pattern[str] = _ADDRESS) -> str:
    """A validated GraphQL string literal. Raises ValueError, caught by the helpers."""
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError("refused an input that is not a plain address/identifier")
    return json.dumps(value)


def _lit_num(value: Any) -> str:
    d = _dec(value)
    if d is None:
        raise ValueError("refused a non-numeric filter bound")
    return repr(float(d))


def _network(chain_or_id: Chain | int) -> int:
    if isinstance(chain_or_id, Chain):
        if chain_or_id not in NETWORK_IDS:
            raise ValueError(f"no codex network for {chain_or_id.value}")
        return NETWORK_IDS[chain_or_id]
    if isinstance(chain_or_id, bool) or not isinstance(chain_or_id, int) or chain_or_id <= 0:
        raise ValueError("network id must be a positive int")
    return chain_or_id


def _refuse_input(endpoint: str, exc: Exception, conn: Any) -> None:
    _report(endpoint, f"input refused: {exc}", conn)


# ----------------------------------------------------------------- (a) prices / pairs


_PRICE_FIELDS = "address networkId priceUsd liquidityUsd timestamp blockNumber priceChange24"


def token_prices(
    tokens: Iterable[tuple[Chain | int, str]],
    *,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: sqlite3.Connection | None = None,
    timeout_s: float | None = None,
) -> dict[tuple[int, str], TokenPrice] | None:
    """Aggregate USD price + pooled liquidity per ``(chain|network_id, address)``.

    Keyed by ``(network_id, address.lower())``. A token Codex does not price is simply
    absent. ``None`` when any chunk failed: half a book priced is reported as a failure,
    not as half a book. Never cached: a price is an observation.
    """
    try:
        wanted = [(_network(n), a) for n, a in tokens]
        for _, a in wanted:
            _lit_str(a)
    except (ValueError, TypeError) as exc:
        _refuse_input(EP_PRICES, exc, conn)
        return None
    out: dict[tuple[int, str], TokenPrice] = {}
    for i in range(0, len(wanted), MAX_PRICE_INPUTS):
        chunk = wanted[i:i + MAX_PRICE_INPUTS]
        inputs = ", ".join(f"{{address: {_lit_str(a)}, networkId: {n}}}" for n, a in chunk)
        got = _query("getTokenPrices", EP_PRICES, f"getTokenPrices(inputs: [{inputs}]) {{ {_PRICE_FIELDS} }}",
                     rows=len(chunk), priority=priority, ttl_s=0.0, wait_for_slot_s=wait_for_slot_s, conn=conn,
                     timeout_s=HTTP_TIMEOUT_S if timeout_s is None else float(timeout_s))
        if got.data is None:
            return None
        for p in parse_prices(got.data):
            out[(p.network_id, p.address.lower())] = p
    return out


def price(chain: Chain | int, address: str, **kw: Any) -> TokenPrice | None:
    """One token's Codex price, or ``None`` (failure, or Codex has no price for it)."""
    got = token_prices([(chain, address)], **kw)
    if not got:
        return None
    try:
        return got.get((_network(chain), address.lower()))
    except ValueError:
        return None


def token_pairs(
    chain: Chain | int,
    token: str,
    *,
    limit: int = 10,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 300.0,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: sqlite3.Connection | None = None,
) -> list[PairInfo] | None:
    """A token's pools, deepest first (Codex itself does not sort them)."""
    try:
        net, addr = _network(chain), _lit_str(token)
    except (ValueError, TypeError) as exc:
        _refuse_input(EP_PAIRS, exc, conn)
        return None
    lim = max(1, min(int(limit), MAX_PAIRS_LIMIT))
    sel = (f"listPairsWithMetadataForToken(tokenAddress: {addr}, networkId: {net}, limit: {lim}) "
           "{ results { volume liquidity quoteToken pair { address id networkId fee protocol token0 token1 "
           "createdAt } exchange { name address } backingToken { address symbol } } }")
    got = _query("listPairsWithMetadataForToken", EP_PAIRS, sel, rows=lim * 3, priority=priority,
                 ttl_s=ttl_s, wait_for_slot_s=wait_for_slot_s, conn=conn)
    return None if got.data is None else parse_pairs(got.data)


# ---------------------------------------------------------------- (b) token screens


_FILTER_FIELDS = (
    "count page results { token { address networkId name symbol decimals createdAt creatorAddress } "
    "priceUSD liquidity volume24 holders marketCap createdAt trendingScore txnCount24 buyCount24 "
    "sellCount24 change24 }"
)


def filter_tokens(
    networks: Sequence[Chain | int],
    *,
    rank_by: str = "trendingScore24",
    descending: bool = True,
    min_liquidity_usd: Any = None,
    min_volume24_usd: Any = None,
    min_holders: Any = None,
    created_after_s: Any = None,
    launchpad_names: Sequence[str] | None = None,
    exclude_scams: bool = True,
    limit: int = 50,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 0.0,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: sqlite3.Connection | None = None,
) -> list[TokenCandidate] | None:
    """``filterTokens``: one screen across one or more networks. 5 CU + ~0.004/row.

    Not cached by default, and a ``ttl_s`` here is in effect ignored: ``trendingScore`` is
    a JSON float, ``_http`` parses floats to ``Decimal``, and its cache writer cannot
    serialise a ``Decimal``, so the write is skipped (MEASURED in the tests). The same
    holds for ``getTokenPrices``. ``token_pairs`` and the event tape are all strings and
    ints, so their ``ttl_s`` does cache.
    """
    try:
        nets = sorted({_network(n) for n in networks})
        if not nets:
            raise ValueError("no networks")
        if rank_by not in RANKINGS:
            raise ValueError(f"unknown ranking {rank_by!r}")
        parts = [f"network: [{', '.join(str(n) for n in nets)}]"]
        if min_liquidity_usd is not None:
            parts.append(f"liquidity: {{gte: {_lit_num(min_liquidity_usd)}}}")
        if min_volume24_usd is not None:
            parts.append(f"volume24: {{gte: {_lit_num(min_volume24_usd)}}}")
        if min_holders is not None:
            parts.append(f"holders: {{gte: {_lit_num(min_holders)}}}")
        if created_after_s is not None:
            parts.append(f"createdAt: {{gte: {_lit_num(created_after_s)}}}")
        if launchpad_names:
            names = ", ".join(_lit_str(n, _LAUNCHPAD) for n in launchpad_names)
            parts.append(f"launchpadName: [{names}]")
        if exclude_scams:
            parts.append("potentialScam: false")
    except (ValueError, TypeError) as exc:
        _refuse_input(EP_FILTER_TOKENS, exc, conn)
        return None
    lim = max(1, min(int(limit), MAX_FILTER_LIMIT))
    direction = "DESC" if descending else "ASC"
    sel = (f"filterTokens(filters: {{{', '.join(parts)}}}, rankings: [{{attribute: {rank_by}, "
           f"direction: {direction}}}], limit: {lim}) {{ {_FILTER_FIELDS} }}")
    got = _query("filterTokens", EP_FILTER_TOKENS, sel, rows=lim, priority=priority, ttl_s=ttl_s,
                 wait_for_slot_s=wait_for_slot_s, conn=conn)
    return None if got.data is None else parse_filter_tokens(got.data)


def new_tokens(chain: Chain | int, *, min_liquidity_usd: Any = 5_000, limit: int = 50, **kw: Any):
    """Newest pairs on one network with at least ``min_liquidity_usd`` (paper-lane candidates)."""
    return filter_tokens([chain], rank_by="createdAt", min_liquidity_usd=min_liquidity_usd, limit=limit, **kw)


def trending_tokens(chain: Chain | int, *, window: str = "24", limit: int = 50, **kw: Any):
    """Codex's trending order on one network. ``window`` in {"", "5m", "1", "4", "12", "24"}."""
    return filter_tokens([chain], rank_by=f"trendingScore{window}", limit=limit, **kw)


# ------------------------------------------------------------------ (c) event tape


_EVENT_FIELDS = (
    "address networkId blockNumber timestamp transactionHash logIndex maker eventType eventDisplayType "
    "quoteToken token0Address token1Address walletAge walletLabels labels { sandwich { label } "
    "washtrade { label } } data { ... on SwapEventData { amountNonLiquidityToken priceUsd priceUsdTotal "
    "priceBaseTokenTotal } }"
)


def _window(from_s: Any, to_s: Any) -> str | None:
    if from_s is None and to_s is None:
        return None
    lo = int(from_s if from_s is not None else 0)
    hi = int(to_s if to_s is not None else now_ms() // 1000 + 60)
    if lo < 0 or hi < lo:
        raise ValueError("bad timestamp window")
    return f"timestamp: {{from: {lo}, to: {hi}}}"


def token_events(
    chain: Chain | int,
    token_or_pair: str,
    *,
    sides: Sequence[str] | None = None,
    from_s: int | None = None,
    to_s: int | None = None,
    oldest_first: bool = False,
    limit: int = 100,
    cursor: str | None = None,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 0.0,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: sqlite3.Connection | None = None,
) -> EventsPage | None:
    """Swaps with maker wallets for a token (its top pair) or a specific pair. 5 CU/page.

    ``oldest_first=True`` with ``sides=["Buy"]`` is the early-buyer read: the first wallets
    into a token. Timestamps are unix SECONDS; Codex compares them against sub-second event
    times, so pad windows by a second or two and dedupe on ``(tx_hash, log_index)``.
    """
    try:
        parts = [f"address: {_lit_str(token_or_pair)}", f"networkId: {_network(chain)}"]
        if sides:
            bad = [s for s in sides if s not in DISPLAY_TYPES]
            if bad:
                raise ValueError(f"unknown event display type {bad[0]!r}")
            parts.append(f"eventDisplayType: [{', '.join(sides)}]")
        win = _window(from_s, to_s)
        if win:
            parts.append(win)
        cur = f", cursor: {_lit_str(cursor, _CURSOR)}" if cursor else ""
    except (ValueError, TypeError) as exc:
        _refuse_input(EP_EVENTS, exc, conn)
        return None
    lim = max(1, min(int(limit), MAX_EVENTS_LIMIT))
    sel = (f"getTokenEvents(query: {{{', '.join(parts)}}}, limit: {lim}, direction: "
           f"{'ASC' if oldest_first else 'DESC'}{cur}) {{ cursor items {{ {_EVENT_FIELDS} }} }}")
    got = _query("getTokenEvents", EP_EVENTS, sel, rows=0, priority=priority, ttl_s=ttl_s,
                 wait_for_slot_s=wait_for_slot_s, conn=conn)
    return None if got.data is None else parse_events(got.data)


def maker_events(
    maker: str,
    *,
    chain: Chain | int | None = None,
    token: str | None = None,
    from_s: int | None = None,
    to_s: int | None = None,
    limit: int = 100,
    cursor: str | None = None,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float = 0.0,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    conn: sqlite3.Connection | None = None,
) -> EventsPage | None:
    """One wallet's swaps, newest first, optionally on one network/token. 10 CU/page.

    This is the free-plan substitute for the wallet PnL endpoints, which are plan-gated:
    a wallet's trades can be read here and priced by the caller.
    """
    try:
        parts = [f"maker: {_lit_str(maker)}", "eventType: Swap"]
        if chain is not None:
            parts.append(f"networkId: {_network(chain)}")
        if token is not None:
            parts.append(f"tokenAddress: {_lit_str(token)}")
        win = _window(from_s, to_s)
        if win:
            parts.append(win)
        cur = f", cursor: {_lit_str(cursor, _CURSOR)}" if cursor else ""
    except (ValueError, TypeError) as exc:
        _refuse_input(EP_MAKER_EVENTS, exc, conn)
        return None
    lim = max(1, min(int(limit), MAX_EVENTS_LIMIT))
    sel = (f"getTokenEventsForMaker(query: {{{', '.join(parts)}}}, limit: {lim}, direction: DESC{cur}) "
           f"{{ cursor items {{ {_EVENT_FIELDS} }} }}")
    got = _query("getTokenEventsForMaker", EP_MAKER_EVENTS, sel, rows=0, priority=priority, ttl_s=ttl_s,
                 wait_for_slot_s=wait_for_slot_s, conn=conn)
    return None if got.data is None else parse_events(got.data)


__all__ = [
    "DEFAULT_CU_CEILING",
    "EXTRA_NETWORK_IDS",
    "NETWORK_IDS",
    "PLAN_CU_PER_WINDOW",
    "PROVIDER",
    "WRAPPED_NATIVE",
    "EventsPage",
    "PairInfo",
    "QuotaStatus",
    "TokenCandidate",
    "TokenEvent",
    "TokenPrice",
    "ceiling_cu",
    "filter_tokens",
    "maker_events",
    "new_tokens",
    "price",
    "quota_status",
    "token_events",
    "token_pairs",
    "token_prices",
    "trending_tokens",
]
