"""DexScreener adapter: the keyless eyes on every DEX pair we care about.

DexScreener is the only provider in the stack that needs no key, covers every chain we
trade, and will tell us the liquidity of a pool as well as its price. That makes it the
default answer to two very different questions:

* *discovery* — who paid for a profile, who bought boosts, who ran a community takeover;
* *market* — what is this token worth right now, and is the pool deep enough to exit into.

Those two jobs have opposite requirements, so they are deliberately kept apart here.

**Rate limit families.** DexScreener publishes two buckets: 60 rpm for the promotion
endpoints (``token-profiles``, ``token-boosts``, ``orders``) and 300 rpm for the market
endpoints (``tokens``, ``token-pairs``, ``latest/dex/search``). The limiter cools the text
before the first dot of an endpoint string, so the promotion routes all live under
``discovery.`` and share a cooldown, which mirrors the bucket they actually share. The
market routes are split into ``price.`` and ``search.`` rather than sharing one family:
they do sit in one real bucket, but a discovery-driven search must never be able to freeze
the route the exit watchdog reads a stop-loss price from. Our provider-wide budget
(``limiter.DEFAULTS['dexscreener']``) is already 60 rpm in total, so the 300 rpm bucket is
effectively unreachable and that split costs nothing.

**TTLs.** The cache here exists to stop two callers asking the same question in the same
second, not to save quota — the whole discovery loop costs about 5 rpm of the 60. Profiles
and boosts get tens of seconds because a boost that lands is worth finding quickly; the
boost leaderboard and paid-order history get minutes because they barely move. Pair data
gets seconds and never a stale-after-failure grace: a price that is quietly older than it
looks is worse than no price at all (see :mod:`kaiba.providers.prices`).

Everything goes through :mod:`kaiba.providers._http`, which owns the limiter call, the disk
cache, the retry and the "never raise, return an UNAVAILABLE receipt" rule.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, Receipt, now_ms
from kaiba.providers._http import get_json

log = logging.getLogger(__name__)

PROVIDER = "dexscreener"
BASE_URL = "https://api.dexscreener.com"

# -- limiter endpoint strings; the text before the dot is the family that gets cooled ----
EP_PROFILES_LATEST = "discovery.token_profiles_latest"
EP_BOOSTS_LATEST = "discovery.token_boosts_latest"
EP_BOOSTS_TOP = "discovery.token_boosts_top"
EP_ORDERS = "discovery.orders"
EP_TOKEN_PAIRS = "price.token_pairs"
EP_TOKENS = "price.tokens"
EP_SEARCH = "search.pairs"

# -- cache TTLs, seconds ----------------------------------------------------------------
TTL_PROFILES_S = 30.0
TTL_BOOSTS_LATEST_S = 30.0
TTL_BOOSTS_TOP_S = 300.0
TTL_ORDERS_S = 600.0
#: Ceiling for anything price-shaped. Also the worst-case understatement of a cached
#: price's age, because a cache hit stamps its Receipt with the read time, not the fetch
#: time. Keep this far below any caller's freshness budget.
TTL_PAIRS_S = 10.0
TTL_SEARCH_S = 30.0

#: Grace during which a dead provider may serve an expired discovery answer. Never applied
#: to pair data.
STALE_GRACE_DISCOVERY_S = 300.0

#: ``tokens/v1`` accepts at most this many comma-joined addresses per request.
MAX_TOKEN_ADDRESSES = 30

#: DexScreener slug <-> our Chain. Every entry here was observed in a live response while
#: recording ``tests/fixtures/dexscreener/``; ``solana`` and ``ethereum`` are the two whose
#: spelling differs from ours. :attr:`Chain.STABLE` is deliberately absent — DexScreener
#: may or may not use the slug ``stable`` for it and an unverified guess would send a
#: request for the wrong chain's token, so that chain resolves to "no price source".
SLUG_TO_CHAIN: dict[str, Chain] = {
    "solana": Chain.SOL,
    "ethereum": Chain.ETH,
    "bsc": Chain.BSC,
    "base": Chain.BASE,
    "robinhood": Chain.ROBINHOOD,
    "arc": Chain.ARC,
}
CHAIN_TO_SLUG: dict[Chain, str] = {c: s for s, c in SLUG_TO_CHAIN.items()}


def chain_from_slug(slug: str | None) -> Chain | None:
    """Map a DexScreener ``chainId`` to our enum. Unknown chain -> ``None``, never a guess.

    DexScreener indexes dozens of chains we do not model (``near``, ``arbitrum``, ...).
    Guessing one into :attr:`Chain.ETH` would put a token we cannot trade into the alpha
    feed, so an unmapped slug simply produces nothing.
    """
    if not slug:
        return None
    return SLUG_TO_CHAIN.get(slug.strip().lower())


def slug_for_chain(chain: Chain) -> str | None:
    """Map our enum to a DexScreener ``chainId``. ``None`` when DexScreener has no slug."""
    return CHAIN_TO_SLUG.get(chain)


# --------------------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------------------


def to_decimal(value: Any) -> Decimal | None:
    """USD as ``Decimal`` or nothing.

    ``priceUsd`` arrives as a *string*, which is the only lossless path, and that is the
    one that matters: it is the number a stop loss divides by. ``liquidity.usd`` arrives as
    a JSON number and has therefore already been through a float by the time ``httpx``
    hands us the body — ``str()`` on it recovers the shortest round-tripping decimal, which
    is honest for a pool-depth gate but is not exact provider truth. Non-finite values and
    junk return ``None`` rather than a zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        return None
    try:
        out = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    """A price of zero is not a price. It is a provider that has nothing to say."""
    out = to_decimal(value)
    return out if out is not None and out > 0 else None


def _int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _addr(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def same_address(a: str, b: str) -> bool:
    """EVM addresses differ only in case; Solana base58 is case-sensitive but comparing
    case-insensitively cannot produce a false match between two real base58 mints in
    practice, and a false *negative* here would silently drop a price."""
    return a.strip().lower() == b.strip().lower()


# --------------------------------------------------------------------------------------
# typed rows
# --------------------------------------------------------------------------------------


class TokenProfile(BaseModel):
    """An entry from ``token-profiles/latest/v1``: someone paid to describe their token."""

    chain: Chain
    chain_slug: str
    address: str
    url: str | None = None
    icon: str | None = None
    header: str | None = None
    description: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)
    cto: bool = False

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address}"

    @property
    def socials(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for link in self.links:
            kind = str(link.get("type") or link.get("label") or "").strip().lower()
            url = link.get("url")
            if kind and isinstance(url, str):
                out.setdefault(kind, url)
        return out


class Boost(BaseModel):
    """An entry from ``token-boosts/latest/v1`` or ``.../top/v1``.

    ``amount`` is the size of the most recent purchase and is absent from the ``top`` feed;
    ``total_amount`` is the running total and is what both feeds are ranked by.
    """

    chain: Chain
    chain_slug: str
    address: str
    amount: int | None = None
    total_amount: int = 0
    url: str | None = None
    description: str | None = None
    links: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.chain.value}:{self.address}"


class PromoOrder(BaseModel):
    """A paid-promotion record from ``orders/v1/{chain}/{token}``.

    ``type`` is one of ``tokenProfile``, ``tokenAd``, ``communityTakeover`` (DexScreener
    adds new ones without notice, so it is kept as a free string). ``status`` is
    ``approved`` / ``processing`` / ``on-hold`` / ``cancelled`` / ``rejected``.
    """

    chain: Chain
    chain_slug: str
    address: str
    type: str
    status: str
    payment_ms: int | None = None

    @property
    def approved(self) -> bool:
        return self.status.strip().lower() == "approved"


class PairSnapshot(BaseModel):
    """One pool as DexScreener currently sees it.

    ``price_usd`` is always the price of :attr:`base_address`. If the token you asked about
    is the *quote* side of this pair, this number belongs to something else — that is what
    :meth:`prices_for` guards against.
    """

    chain: Chain
    chain_slug: str
    pair_address: str
    dex_id: str | None = None
    labels: list[str] = Field(default_factory=list)
    url: str | None = None
    base_address: str = ""
    base_symbol: str | None = None
    base_name: str | None = None
    quote_address: str = ""
    quote_symbol: str | None = None
    price_usd: Decimal | None = None
    price_native: Decimal | None = None
    liquidity_usd: Decimal | None = None
    liquidity_base: Decimal | None = None
    liquidity_quote: Decimal | None = None
    fdv_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    volume_h24_usd: Decimal | None = None
    volume_h1_usd: Decimal | None = None
    price_change_h24_pct: float | None = None
    price_change_m5_pct: float | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None
    pair_created_ms: int | None = None

    def prices_for(self, token: str) -> bool:
        """True only when :attr:`price_usd` is this token's price."""
        return bool(self.base_address) and same_address(self.base_address, token)

    @property
    def age_s(self) -> float | None:
        if self.pair_created_ms is None:
            return None
        return (now_ms() - self.pair_created_ms) / 1000.0

    @property
    def label(self) -> str:
        """Short human tag for a receipt note."""
        pair = f"{self.base_symbol or '?'}/{self.quote_symbol or '?'}"
        return f"{self.dex_id or 'dex'}:{pair}:{self.pair_address}"


# --------------------------------------------------------------------------------------
# row -> model
# --------------------------------------------------------------------------------------


def _parse_profile(raw: Any) -> TokenProfile | None:
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("chainId") or "")
    chain = chain_from_slug(slug)
    address = _addr(raw.get("tokenAddress"))
    if chain is None or not address:
        return None
    links = raw.get("links")
    return TokenProfile(
        chain=chain,
        chain_slug=slug,
        address=address,
        url=raw.get("url"),
        icon=raw.get("icon"),
        header=raw.get("header"),
        description=raw.get("description") or None,
        links=[x for x in links if isinstance(x, dict)] if isinstance(links, list) else [],
        cto=bool(raw.get("cto")),
    )


def _parse_boost(raw: Any) -> Boost | None:
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("chainId") or "")
    chain = chain_from_slug(slug)
    address = _addr(raw.get("tokenAddress"))
    if chain is None or not address:
        return None
    links = raw.get("links")
    return Boost(
        chain=chain,
        chain_slug=slug,
        address=address,
        amount=_int(raw.get("amount")),
        total_amount=_int(raw.get("totalAmount")) or 0,
        url=raw.get("url"),
        description=raw.get("description") or None,
        links=[x for x in links if isinstance(x, dict)] if isinstance(links, list) else [],
    )


def _parse_order(raw: Any) -> PromoOrder | None:
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("chainId") or "")
    chain = chain_from_slug(slug)
    address = _addr(raw.get("tokenAddress"))
    kind = str(raw.get("type") or "").strip()
    if chain is None or not address or not kind:
        return None
    return PromoOrder(
        chain=chain,
        chain_slug=slug,
        address=address,
        type=kind,
        status=str(raw.get("status") or "unknown").strip(),
        payment_ms=_int(raw.get("paymentTimestamp")),
    )


def _parse_pair(raw: Any) -> PairSnapshot | None:
    if not isinstance(raw, dict):
        return None
    slug = str(raw.get("chainId") or "")
    chain = chain_from_slug(slug)
    pair_address = _addr(raw.get("pairAddress"))
    if chain is None or not pair_address:
        return None
    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), dict) else {}
    quote = raw.get("quoteToken") if isinstance(raw.get("quoteToken"), dict) else {}
    liq = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}
    vol = raw.get("volume") if isinstance(raw.get("volume"), dict) else {}
    chg = raw.get("priceChange") if isinstance(raw.get("priceChange"), dict) else {}
    txns = raw.get("txns") if isinstance(raw.get("txns"), dict) else {}
    h24 = txns.get("h24") if isinstance(txns.get("h24"), dict) else {}
    labels = raw.get("labels")

    def _pct(key: str) -> float | None:
        v = chg.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return PairSnapshot(
        chain=chain,
        chain_slug=slug,
        pair_address=pair_address,
        dex_id=raw.get("dexId"),
        labels=[str(x) for x in labels] if isinstance(labels, list) else [],
        url=raw.get("url"),
        base_address=_addr(base.get("address")),
        base_symbol=base.get("symbol"),
        base_name=base.get("name"),
        quote_address=_addr(quote.get("address")),
        quote_symbol=quote.get("symbol"),
        price_usd=_positive(raw.get("priceUsd")),
        price_native=_positive(raw.get("priceNative")),
        liquidity_usd=to_decimal(liq.get("usd")),
        liquidity_base=to_decimal(liq.get("base")),
        liquidity_quote=to_decimal(liq.get("quote")),
        fdv_usd=to_decimal(raw.get("fdv")),
        market_cap_usd=to_decimal(raw.get("marketCap")),
        volume_h24_usd=to_decimal(vol.get("h24")),
        volume_h1_usd=to_decimal(vol.get("h1")),
        price_change_h24_pct=_pct("h24"),
        price_change_m5_pct=_pct("m5"),
        buys_h24=_int(h24.get("buys")),
        sells_h24=_int(h24.get("sells")),
        pair_created_ms=_int(raw.get("pairCreatedAt")),
    )


def _rows(data: Any, key: str | None = None) -> list[Any]:
    """DexScreener is inconsistent about envelopes: some routes return a bare array, some
    wrap it in ``{"pairs": [...]}``, and ``orders/v1`` currently returns
    ``{"orders": [...], "boosts": [...]}`` although it is documented as a bare array.
    Accept every one of those rather than break when it changes back."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if key and isinstance(data.get(key), list):
            return data[key]
        for candidate in ("pairs", "orders", "data", "results"):
            if isinstance(data.get(candidate), list):
                return data[candidate]
    return []


def note_receipt(receipt: Receipt, note: str) -> Receipt:
    """Receipts are frozen; this is the only way to attach a per-call note."""
    joined = f"{receipt.note}; {note}" if receipt.note else note
    return receipt.model_copy(update={"note": joined[:300]})


# --------------------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------------------


def token_profiles_latest(*, conn: Any = None) -> tuple[list[TokenProfile], Receipt]:
    """Latest paid token profiles. Rows on chains we do not model are dropped."""
    got = get_json(
        PROVIDER,
        EP_PROFILES_LATEST,
        f"{BASE_URL}/token-profiles/latest/v1",
        priority=Priority.DISCOVERY,
        ttl_s=TTL_PROFILES_S,
        stale_grace_s=STALE_GRACE_DISCOVERY_S,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [p for p in (_parse_profile(r) for r in _rows(got.data)) if p is not None]
    return rows, got.receipt


def token_boosts_latest(*, conn: Any = None) -> tuple[list[Boost], Receipt]:
    """Boosts bought most recently. This is the alpha feed; ``top`` is the leaderboard."""
    got = get_json(
        PROVIDER,
        EP_BOOSTS_LATEST,
        f"{BASE_URL}/token-boosts/latest/v1",
        priority=Priority.DISCOVERY,
        ttl_s=TTL_BOOSTS_LATEST_S,
        stale_grace_s=STALE_GRACE_DISCOVERY_S,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [b for b in (_parse_boost(r) for r in _rows(got.data)) if b is not None]
    return rows, got.receipt


def token_boosts_top(*, conn: Any = None) -> tuple[list[Boost], Receipt]:
    """Most-boosted tokens all time. Slow moving, so it is cached for minutes."""
    got = get_json(
        PROVIDER,
        EP_BOOSTS_TOP,
        f"{BASE_URL}/token-boosts/top/v1",
        priority=Priority.RESEARCH,
        ttl_s=TTL_BOOSTS_TOP_S,
        stale_grace_s=STALE_GRACE_DISCOVERY_S,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [b for b in (_parse_boost(r) for r in _rows(got.data)) if b is not None]
    return rows, got.receipt


def token_orders(chain: Chain, token: str, *, conn: Any = None) -> tuple[list[PromoOrder], Receipt]:
    """Paid-promotion history for one token: profile purchases, ads, community takeovers.

    Useful as a DYOR input — a token whose only "community" is a $300 ad order is a
    different proposition from one with an approved takeover behind it.
    """
    slug = slug_for_chain(chain)
    if slug is None:
        return [], _unsupported_chain_receipt(EP_ORDERS, chain)
    got = get_json(
        PROVIDER,
        EP_ORDERS,
        f"{BASE_URL}/orders/v1/{slug}/{token.strip()}",
        priority=Priority.RESEARCH,
        ttl_s=TTL_ORDERS_S,
        stale_grace_s=STALE_GRACE_DISCOVERY_S,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [o for o in (_parse_order(r) for r in _rows(got.data, "orders")) if o is not None]
    return rows, got.receipt


def has_community_takeover(chain: Chain, token: str, *, conn: Any = None) -> tuple[bool | None, Receipt]:
    """``True``/``False`` if we could ask, ``None`` if the provider was unreachable.

    ``False`` and ``None`` are different answers and callers must not conflate them.
    """
    orders, receipt = token_orders(chain, token, conn=conn)
    if not orders and receipt.basis.value == "unavailable":
        return None, receipt
    return any(o.approved and o.type.lower() == "communitytakeover" for o in orders), receipt


def _pair_ttl(ttl_s: float | None) -> float:
    """Never cache pair data longer than :data:`TTL_PAIRS_S`, whatever a caller asks for."""
    if ttl_s is None:
        return TTL_PAIRS_S
    return max(0.0, min(float(ttl_s), TTL_PAIRS_S))


def token_pairs(
    chain: Chain,
    token: str,
    *,
    priority: Priority = Priority.POSITION,
    ttl_s: float | None = None,
    conn: Any = None,
) -> tuple[list[PairSnapshot], Receipt]:
    """Every pool DexScreener indexes for one token. The full list, so a caller can pick.

    Defaults to :attr:`Priority.POSITION` because this is the route a stop loss reads.
    """
    slug = slug_for_chain(chain)
    if slug is None:
        return [], _unsupported_chain_receipt(EP_TOKEN_PAIRS, chain)
    got = get_json(
        PROVIDER,
        EP_TOKEN_PAIRS,
        f"{BASE_URL}/token-pairs/v1/{slug}/{token.strip()}",
        priority=priority,
        ttl_s=_pair_ttl(ttl_s),
        stale_grace_s=0.0,  # a silently old price is worse than no price
        timeout_s=6.0,
        retries=2,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [p for p in (_parse_pair(r) for r in _rows(got.data)) if p is not None]
    return rows, got.receipt


def tokens(
    chain: Chain,
    addresses: list[str],
    *,
    priority: Priority = Priority.POSITION,
    ttl_s: float | None = None,
    conn: Any = None,
) -> tuple[list[PairSnapshot], Receipt]:
    """One request for up to 30 tokens; DexScreener answers with its own pick of pool.

    Anything past :data:`MAX_TOKEN_ADDRESSES` is dropped and recorded in the receipt rather
    than silently truncated or split — chunking is the caller's decision, and
    :func:`kaiba.providers.prices.prices_usd` makes it.
    """
    slug = slug_for_chain(chain)
    if slug is None:
        return [], _unsupported_chain_receipt(EP_TOKENS, chain)
    wanted = [a.strip() for a in addresses if a and a.strip()]
    if not wanted:
        return [], Receipt(provider=PROVIDER, endpoint=EP_TOKENS, note="no addresses requested")
    dropped = max(0, len(wanted) - MAX_TOKEN_ADDRESSES)
    wanted = wanted[:MAX_TOKEN_ADDRESSES]
    got = get_json(
        PROVIDER,
        EP_TOKENS,
        f"{BASE_URL}/tokens/v1/{slug}/{','.join(wanted)}",
        priority=priority,
        ttl_s=_pair_ttl(ttl_s),
        stale_grace_s=0.0,
        timeout_s=6.0,
        retries=2,
        conn=conn,
    )
    receipt = got.receipt
    if dropped:
        receipt = note_receipt(receipt, f"dropped {dropped} address(es) over the {MAX_TOKEN_ADDRESSES} cap")
    if not got:
        return [], receipt
    rows = [p for p in (_parse_pair(r) for r in _rows(got.data)) if p is not None]
    return rows, receipt


def search(query: str, *, limit: int = 30, conn: Any = None) -> tuple[list[PairSnapshot], Receipt]:
    """Free-text pair search. Discovery only — it can return any chain, including ones we
    do not model, and those rows are dropped."""
    q = query.strip()
    if not q:
        return [], Receipt(provider=PROVIDER, endpoint=EP_SEARCH, note="empty query")
    got = get_json(
        PROVIDER,
        EP_SEARCH,
        f"{BASE_URL}/latest/dex/search",
        params={"q": q},
        priority=Priority.RESEARCH,
        ttl_s=TTL_SEARCH_S,
        conn=conn,
    )
    if not got:
        return [], got.receipt
    rows = [p for p in (_parse_pair(r) for r in _rows(got.data, "pairs")) if p is not None]
    rows.sort(key=lambda p: p.liquidity_usd if p.liquidity_usd is not None else Decimal(-1), reverse=True)
    return rows[: max(1, limit)], got.receipt


def _unsupported_chain_receipt(endpoint: str, chain: Chain) -> Receipt:
    from kaiba.core.schemas import EvidenceBasis

    return Receipt(
        provider=PROVIDER,
        endpoint=endpoint,
        basis=EvidenceBasis.UNAVAILABLE,
        note=f"dexscreener has no slug for chain {chain.value}",
    )


# --------------------------------------------------------------------------------------
# alpha events
# --------------------------------------------------------------------------------------


def _boost_dedupe_key(b: Boost) -> str:
    """Re-emit only when the running total moves.

    Deduping on the token alone would mean a token going from 10 to 500 boost — the actual
    signal — never reaches the bus a second time. Deduping on ``(token, total)`` emits once
    per escalation and never once per poll.
    """
    return f"{PROVIDER}:boost:{b.chain.value}:{b.address}:{b.total_amount}"


def _cto_dedupe_key(chain: Chain, address: str) -> str:
    return f"{PROVIDER}:cto:{chain.value}:{address}"


def emit_boost_events(boosts: list[Boost], *, feed: str = "latest", conn: Any = None) -> list[Boost]:
    """Emit :attr:`EventKind.ALPHA_BOOST` for boosts not already on the bus.

    Returns the boosts that were genuinely new, so a poller can act on exactly those.
    """
    fresh: list[Boost] = []
    for b in boosts:
        event_id = emit(
            EventKind.ALPHA_BOOST,
            {
                "provider": PROVIDER,
                "feed": feed,
                "token": b.address,
                "chain": b.chain.value,
                "amount": b.amount,
                "total_amount": b.total_amount,
                "url": b.url,
                "description": (b.description or "")[:280] or None,
                "links": b.links[:6],
            },
            chain=b.chain,
            subject=b.address,
            dedupe_key=_boost_dedupe_key(b),
            conn=conn,
        )
        if event_id is not None:
            fresh.append(b)
    return fresh


def emit_cto_events(profiles: list[TokenProfile], *, conn: Any = None) -> list[TokenProfile]:
    """Emit :attr:`EventKind.ALPHA_CTO` for profiles flagged as a community takeover.

    A takeover happens once per token, so the dedupe key is the token — a poller seeing the
    same CTO profile every 30 seconds emits nothing after the first time.
    """
    fresh: list[TokenProfile] = []
    for p in profiles:
        if not p.cto:
            continue
        event_id = emit(
            EventKind.ALPHA_CTO,
            {
                "provider": PROVIDER,
                "token": p.address,
                "chain": p.chain.value,
                "url": p.url,
                "description": (p.description or "")[:280] or None,
                "socials": p.socials,
            },
            chain=p.chain,
            subject=p.address,
            dedupe_key=_cto_dedupe_key(p.chain, p.address),
            conn=conn,
        )
        if event_id is not None:
            fresh.append(p)
    return fresh


def pace(priority: Priority = Priority.DISCOVERY) -> None:
    """Leave room for the limiter's minimum interval before the next call.

    ``_http.request_json`` does **not** block waiting for capacity — a limiter refusal is
    an immediate ``UNAVAILABLE``. That is the right behaviour for a single call, but it
    means any function issuing several calls in a row has to space them itself or every
    call after the first is a silent no-op. This was not theoretical: the first live run of
    :func:`poll_alpha` fetched boosts and then got ``minimum interval (retry in 0.4s)``
    instead of profiles, and a 45-token price batch would have lost its second chunk the
    same way.

    The gap mirrors :func:`kaiba.core.limiter.reserve`: high-priority work gets a quarter
    of the interval with a 50 ms floor, so a batched price read pays ~0.3 s per extra chunk
    rather than the full 1.1 s. It does not model the penalty multiplier that follows a
    429; in that state a call is refused, recorded ``UNAVAILABLE`` and retried next cycle.
    """
    from kaiba.core.limiter import limits_for

    gap_ms = limits_for(PROVIDER).min_interval_ms
    if priority <= Priority.POSITION:
        gap_ms = max(50, gap_ms // 4)
    if gap_ms > 0:
        time.sleep(min(gap_ms * 1.25 / 1000.0, 5.0))


def poll_alpha(*, include_top: bool = False, conn: Any = None) -> dict[str, Any]:
    """One discovery cycle: boosts and CTO profiles onto the bus, deduped.

    Safe to call on a timer. A provider outage produces zeroes and a ``PROVIDER_ERROR``
    event from the HTTP layer, never an exception. Takes a couple of seconds because it
    paces its own calls (see :func:`pace`), so run it off the hot path.
    """
    boosts, boost_receipt = token_boosts_latest(conn=conn)
    new_boosts = emit_boost_events(boosts, feed="latest", conn=conn)

    top_new: list[Boost] = []
    if include_top:
        pace()
        top, _ = token_boosts_top(conn=conn)
        top_new = emit_boost_events(top, feed="top", conn=conn)

    pace()
    profiles, profile_receipt = token_profiles_latest(conn=conn)
    new_ctos = emit_cto_events(profiles, conn=conn)

    return {
        "boosts_seen": len(boosts),
        "boosts_emitted": len(new_boosts) + len(top_new),
        "profiles_seen": len(profiles),
        "ctos_emitted": len(new_ctos),
        "receipts": [boost_receipt, profile_receipt],
    }


__all__ = [
    "BASE_URL",
    "CHAIN_TO_SLUG",
    "MAX_TOKEN_ADDRESSES",
    "PROVIDER",
    "SLUG_TO_CHAIN",
    "Boost",
    "PairSnapshot",
    "PromoOrder",
    "TokenProfile",
    "chain_from_slug",
    "note_receipt",
    "pace",
    "emit_boost_events",
    "emit_cto_events",
    "has_community_takeover",
    "poll_alpha",
    "search",
    "slug_for_chain",
    "to_decimal",
    "token_boosts_latest",
    "token_boosts_top",
    "token_orders",
    "token_pairs",
    "token_profiles_latest",
    "tokens",
]
