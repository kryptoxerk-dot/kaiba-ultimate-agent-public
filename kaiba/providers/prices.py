"""USD price and pool depth, for code that is about to risk money on the answer.

The exit watchdog is the caller that matters. It asks "what is this worth now?" on a timer
and sells when the answer crosses a stop. Every rule below exists because of what happens
when that answer is wrong:

* **A missing price is ``Measure.unknown()``, never ``0``.** A zero reads as a 100% loss to
  a stop-loss comparison and liquidates a healthy position on nothing but a timed-out HTTP
  request. Every path out of this module that does not hold a real number returns
  ``unknown``, and the provider layer never raises.
* **Pairs are chosen by liquidity, not by order.** DexScreener will happily list a $200
  pool alongside a $2M one, and the first row is not necessarily the deep one. A price from
  a pool you cannot exit into is a number, not a price — so the chosen pool's address and
  depth go into the receipt note and :attr:`Quote.liquidity_usd` is returned alongside, and
  the caller can refuse it.
* **The price must be the price of the token you asked about.** ``priceUsd`` on a pair is
  the price of its *base* token. DexScreener normalises the token you query into the base
  slot, but a pair where it does not is discarded rather than trusted, because the failure
  mode is quoting WSOL's price for a memecoin.
* **USD is ``Decimal``, parsed from the string the provider sent.** ``priceUsd`` arrives as
  a string and stays exact. (``liquidity.usd`` arrives as a JSON number, so it has already
  been a float before we see it — it is a gate, not an amount, and the docstring on
  :func:`kaiba.providers.dexscreener.to_decimal` says so plainly.)
* **No stale-after-failure grace, and no invented fallback.** A cached price is only served
  inside a seconds-long TTL. When nothing real is available the answer is "unknown" and the
  watchdog can decide to hold, alert, or exit blind — all of which are better than acting
  on a number nobody stands behind.

DexScreener is the only source wired in today. :class:`PriceSource` and
:func:`register_source` exist so a second one (Jupiter, Birdeye, an RPC pool read) can be
added behind these same three functions later; the resolution loop already asks each source
in turn for the tokens still missing. Nothing here fabricates, averages or extrapolates a
price to fill a gap.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, normalize_address
from kaiba.providers import dexscreener as ds

log = logging.getLogger(__name__)

#: Default freshness budget for a price, in seconds. Also the cap a caller may raise.
DEFAULT_MAX_AGE_S = 30.0

#: Liquidity below this is reported but should be treated as untradeable by callers. It is
#: deliberately not enforced here — this module reports, the risk layer decides.
THIN_POOL_USD = Decimal(1_000)

#: The least share of the deepest pool's liquidity a PREFERRED-quote pool must hold before
#: :func:`pick_pair` will choose it over the deepest pool. INVENTED as a number; the need
#: for a floor is MEASURED.
#:
#: The exit watchdog asks for the pool quoted in the asset it sells into (SOL, ETH/WETH,
#: WBNB), because a pool quoted in a third asset (Bonk, a tokenised stock) can be nowhere
#: near the price a sell realises (journal #5048: COZY ``ETXqxf``, 2026-10-02). But the
#: native pool that exists beside a deep foreign one is usually dust, and dust is a WORSE
#: mark than the foreign pool, not a better one. MEASURED 2026-10-03 on DexScreener's pair
#: lists for the 130 tokens our live positions traded in the previous 10 days:
#:
#: * COZY: Bonk CPMM $20,081; SOL pools $190 (DLMM, priced at 0.005x the Bonk pool) and
#:   $1.98. Choosing "the deepest native pool" would have fired an emergency stop on a
#:   price 200x too low.
#: * Of the 49 robinhood/sol/bsc tokens whose deepest pool is NOT native-quoted, exactly one
#:   has a native pool holding >= 25% of the deepest pool's depth (robinhood ``0xd0601c``,
#:   ETH $1.24M beside USDG $3.19M, prices within 0.01%). The next best share was 2.9%;
#:   40 of the 49 have no native pool at all.
#:
#: So the preference almost never fires, and when it does it fires on a pool deep enough to
#: carry the same price. Everything below the floor falls back to the deepest pool, and the
#: caller decides what a foreign-quoted mark is worth.
PREFERRED_PAIR_MIN_SHARE = Decimal("0.25")


@dataclass(frozen=True)
class Quote:
    """One token's price and the pool it came from.

    ``price_usd is None`` means unknown. It never means zero.
    """

    chain: Chain
    token: str
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    receipt: Receipt
    pair_address: str | None = None
    dex_id: str | None = None
    pair_label: str | None = None
    pairs_considered: int = 0
    source: str = ""
    #: The asset the chosen pool prices the token IN (the pair's quote side). ``None`` when
    #: the pool did not report one. A mark is only as good as the route from that asset to
    #: the one we would actually receive on a sell, so callers that sell need to see it.
    quote_address: str | None = None
    quote_symbol: str | None = None

    @property
    def known(self) -> bool:
        return self.price_usd is not None

    def as_measure(self, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> Measure:
        if self.price_usd is None:
            return Measure(
                value=None,
                basis=EvidenceBasis.UNAVAILABLE,
                receipt=self.receipt,
                freshness_budget_s=int(max_age_s),
            )
        return Measure(
            value=self.price_usd,
            basis=self.receipt.basis,
            receipt=self.receipt,
            freshness_budget_s=int(max_age_s),
        )

    def liquidity_measure(self, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> Measure:
        if self.liquidity_usd is None:
            return Measure(
                value=None,
                basis=EvidenceBasis.UNAVAILABLE,
                receipt=self.receipt,
                freshness_budget_s=int(max_age_s),
            )
        return Measure(
            value=self.liquidity_usd,
            basis=self.receipt.basis,
            receipt=self.receipt,
            freshness_budget_s=int(max_age_s),
        )


class PriceSource(Protocol):
    """What a price provider has to offer to be usable here.

    A source returns entries only for tokens it actually resolved. Omitting a token is how
    it says "I do not know", and the loop then asks the next source. Returning a zero, or
    an entry with a made-up price, is a contract violation.

    A source *may* include an entry whose ``price_usd`` is ``None`` purely to explain why
    it could not answer — the loop still treats that token as unresolved and moves on, but
    the receipt survives into the caller's ``Measure``. That is the difference between a
    stop loss that did not fire "for some reason" and one that did not fire because
    DexScreener returned 503.
    """

    name: str

    def quotes(
        self,
        chain: Chain,
        tokens: Sequence[str],
        *,
        max_age_s: float,
        priority: Priority,
        conn: Any,
    ) -> dict[str, Quote]:
        ...


def _unavailable(endpoint: str, note: str, provider: str = ds.PROVIDER) -> Receipt:
    return Receipt(
        provider=provider,
        endpoint=endpoint,
        basis=EvidenceBasis.UNAVAILABLE,
        note=note[:300],
    )


def _depth(pair: ds.PairSnapshot) -> Decimal:
    return pair.liquidity_usd if pair.liquidity_usd is not None else Decimal(-1)


def _quoted_in(pair: ds.PairSnapshot, assets: Collection[str]) -> bool:
    return bool(pair.quote_address) and any(ds.same_address(pair.quote_address, a) for a in assets)


def pick_pair(
    pairs: Iterable[ds.PairSnapshot],
    token: str,
    *,
    prefer_quotes: Collection[str] | None = None,
) -> ds.PairSnapshot | None:
    """The deepest pool that actually prices ``token``.

    Pairs where ``token`` is the quote side are dropped: their ``priceUsd`` belongs to the
    other asset. Pairs with no usable price are dropped. Among the rest, unknown liquidity
    sorts below any known liquidity, so a pool we can measure always beats one we cannot.

    ``prefer_quotes`` names quote assets (e.g. the chain's native and wrapped native) whose
    pools win over a deeper pool quoted in something else -- but only when the preferred
    pool is itself a market: known liquidity of at least :data:`THIN_POOL_USD` AND at least
    :data:`PREFERRED_PAIR_MIN_SHARE` of the deepest pool's. Below that it is ignored and the
    deepest pool is returned, exactly as without a preference. See the floor's docstring
    for why a dust native pool is worse than no preference at all.
    """
    usable = [p for p in pairs if p.price_usd is not None and p.prices_for(token)]
    if not usable:
        return None
    deepest = max(usable, key=_depth)
    if prefer_quotes:
        preferred = [p for p in usable if _quoted_in(p, prefer_quotes)]
        if preferred:
            best = max(preferred, key=_depth)
            depth = best.liquidity_usd
            if depth is not None and depth >= THIN_POOL_USD and (
                deepest.liquidity_usd is None
                or depth >= deepest.liquidity_usd * PREFERRED_PAIR_MIN_SHARE
            ):
                return best
    return deepest


def _quote_from_pairs(
    chain: Chain,
    token: str,
    pairs: list[ds.PairSnapshot],
    receipt: Receipt,
    *,
    source: str,
    prefer_quotes: Collection[str] | None = None,
) -> Quote:
    """Turn a pair list into a :class:`Quote`.

    An unpriceable token still comes back as a ``Quote`` — with ``price_usd=None`` and a
    receipt saying what went wrong — so the reason reaches the caller instead of being
    flattened into a bare "unknown".
    """
    best = pick_pair(pairs, token, prefer_quotes=prefer_quotes)
    if best is None:
        why = (
            f"provider unavailable: {receipt.note}"
            if receipt.basis is EvidenceBasis.UNAVAILABLE
            else f"no pair prices this token ({len(pairs)} pair(s) returned)"
        )
        return Quote(
            chain=chain,
            token=token,
            price_usd=None,
            liquidity_usd=None,
            receipt=_unavailable("price.quote", why, source),
            pairs_considered=len(pairs),
            source=source,
        )
    liq = "?" if best.liquidity_usd is None else f"{best.liquidity_usd:,.0f}"
    noted = ds.note_receipt(
        receipt, f"pair {best.label} liq_usd={liq} chosen from {len(pairs)} pair(s)"
    )
    return Quote(
        chain=chain,
        token=token,
        price_usd=best.price_usd,
        liquidity_usd=best.liquidity_usd,
        receipt=noted,
        pair_address=best.pair_address,
        dex_id=best.dex_id,
        pair_label=best.label,
        pairs_considered=len(pairs),
        source=source,
        quote_address=best.quote_address or None,
        quote_symbol=best.quote_symbol,
    )


class DexScreenerSource:
    """The keyless default. Uses the full pair list for one token and the batch route for
    several, because ``tokens/v1`` answers 30 tokens in one request but only returns
    DexScreener's own pick of pool per token, while ``token-pairs/v1`` returns every pool
    for one token and lets us do the picking."""

    name = ds.PROVIDER

    def quotes(
        self,
        chain: Chain,
        tokens: Sequence[str],
        *,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        priority: Priority = Priority.POSITION,
        conn: Any = None,
        prefer_quotes: Collection[str] | None = None,
    ) -> dict[str, Quote]:
        wanted = list(tokens)
        if not wanted:
            return {}
        if len(wanted) == 1:
            pairs, receipt = ds.token_pairs(
                chain, wanted[0], priority=priority, ttl_s=max_age_s, conn=conn
            )
            return {wanted[0]: _quote_from_pairs(
                chain, wanted[0], pairs, receipt, source=self.name, prefer_quotes=prefer_quotes
            )}

        out: dict[str, Quote] = {}
        for index, chunk in enumerate(_chunks(wanted, ds.MAX_TOKEN_ADDRESSES)):
            if index:
                ds.pace(priority)  # or every chunk after the first is refused unread
            pairs, receipt = ds.tokens(chain, chunk, priority=priority, ttl_s=max_age_s, conn=conn)
            for token in chunk:
                out[token] = _quote_from_pairs(
                    chain, token, pairs, receipt, source=self.name, prefer_quotes=prefer_quotes
                )
        return out


_SOURCES: list[PriceSource] = [DexScreenerSource()]


def sources() -> list[PriceSource]:
    """The resolution order in use. First source that knows a token wins."""
    return list(_SOURCES)


def register_source(source: PriceSource, *, first: bool = False) -> None:
    """Add another price provider behind the same three functions.

    ``first=True`` puts it ahead of DexScreener, which is what a low-latency source such as
    a direct pool read would want. A source that replaces an existing name is swapped in
    place so repeated registration is idempotent.
    """
    global _SOURCES
    _SOURCES = [s for s in _SOURCES if s.name != source.name]
    _SOURCES.insert(0 if first else len(_SOURCES), source)


def reset_sources() -> None:
    """Back to the DexScreener-only default. For tests and the CLI."""
    global _SOURCES
    _SOURCES = [DexScreenerSource()]


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), max(1, size))]


def _only_dexscreener() -> bool:
    """True while DexScreener is the whole roster, so an unmapped chain is simply uncovered."""
    return all(s.name == ds.PROVIDER for s in _SOURCES)


def _normalized(chain: Chain, token: str) -> str | None:
    try:
        return normalize_address(token, chain)
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------------------
# public surface
# --------------------------------------------------------------------------------------


def quote(
    chain: Chain,
    token: str,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    priority: Priority = Priority.POSITION,
    conn: Any = None,
    prefer_quotes: Collection[str] | None = None,
) -> Quote:
    """Price *and* depth in one request. Always returns a :class:`Quote`; check ``.known``.

    Prefer this over calling :func:`price_usd` and :func:`liquidity_usd` back to back when
    you want both — it is one lookup instead of two, and the two numbers are guaranteed to
    describe the same pool at the same instant.

    ``prefer_quotes`` is handed to :func:`pick_pair` (DexScreener only; another registered
    source picks its own pool). ``None`` is the historical deepest-pool behaviour.
    """
    addr = _normalized(chain, token)
    if addr is None:
        return Quote(
            chain=chain,
            token=token,
            price_usd=None,
            liquidity_usd=None,
            receipt=_unavailable("price.quote", f"not a valid {chain.value} address: {token[:64]!r}"),
        )
    if _only_dexscreener() and ds.slug_for_chain(chain) is None:
        return Quote(
            chain=chain,
            token=addr,
            price_usd=None,
            liquidity_usd=None,
            receipt=_unavailable("price.quote", f"no price source covers chain {chain.value}"),
        )

    unresolved: Quote | None = None
    for source in _SOURCES:
        extra: dict[str, Any] = {}
        if prefer_quotes and isinstance(source, DexScreenerSource):
            extra["prefer_quotes"] = prefer_quotes
        try:
            found = source.quotes(
                chain, [addr], max_age_s=max_age_s, priority=priority, conn=conn, **extra
            )
        except Exception as exc:  # noqa: BLE001 - a broken source is data, not a crash
            log.warning("price source %s raised for %s: %s", source.name, addr, exc)
            unresolved = Quote(
                chain=chain,
                token=addr,
                price_usd=None,
                liquidity_usd=None,
                receipt=_unavailable(
                    "price.quote", f"{source.name}: {type(exc).__name__}: {exc}", source.name
                ),
                source=source.name,
            )
            continue
        hit = found.get(addr)
        if hit is not None and hit.price_usd is not None:
            return hit
        if hit is not None:
            unresolved = hit  # keep the explanation, keep looking
    return unresolved or Quote(
        chain=chain,
        token=addr,
        price_usd=None,
        liquidity_usd=None,
        receipt=_unavailable("price.quote", "no source returned a priced pair"),
    )


def price_usd(
    chain: Chain,
    token: str,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    conn: Any = None,
    priority: Priority = Priority.POSITION,
) -> Measure:
    """USD price of one token, or ``Measure.unknown()``.

    ``max_age_s`` is both the caller's freshness budget (recorded on the Measure) and the
    cache TTL, capped at :data:`kaiba.providers.dexscreener.TTL_PAIRS_S` — asking for a
    five-minute-old price does not get you one.

    Pass ``priority=Priority.EXIT`` from the exit watchdog: it shortens the limiter's
    minimum-interval floor so discovery traffic cannot starve a stop loss.
    """
    return quote(chain, token, max_age_s=max_age_s, priority=priority, conn=conn).as_measure(
        max_age_s=max_age_s
    )


def prices_usd(
    chain: Chain,
    tokens: list[str],
    *,
    conn: Any = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    priority: Priority = Priority.POSITION,
) -> dict[str, Measure]:
    """Price several tokens in as few requests as the 30-address cap allows.

    The returned dict is keyed by the exact strings that were passed in, so a caller does
    not have to know about address normalisation. Every input key is present: a token that
    could not be priced maps to ``Measure.unknown()``, so ``result[t].known`` is always a
    valid question and a missing key never silently becomes a zero.
    """
    out: dict[str, Measure] = {t: Measure.unknown(int(max_age_s)) for t in tokens}
    if not tokens:
        return out

    # Preserve order, drop duplicates, and remember which inputs map to which address.
    by_addr: dict[str, list[str]] = {}
    for raw in tokens:
        addr = _normalized(chain, raw)
        if addr is None:
            out[raw] = Measure(
                value=None,
                basis=EvidenceBasis.UNAVAILABLE,
                receipt=_unavailable("price.batch", f"not a valid {chain.value} address: {raw[:64]!r}"),
                freshness_budget_s=int(max_age_s),
            )
            continue
        by_addr.setdefault(addr, []).append(raw)

    if not by_addr:
        return out
    if _only_dexscreener() and ds.slug_for_chain(chain) is None:
        receipt = _unavailable("price.batch", f"no price source covers chain {chain.value}")
        for originals in by_addr.values():
            for raw in originals:
                out[raw] = Measure(
                    value=None,
                    basis=EvidenceBasis.UNAVAILABLE,
                    receipt=receipt,
                    freshness_budget_s=int(max_age_s),
                )
        return out

    pending = list(by_addr)
    reasons: dict[str, Receipt] = {}
    for source in _SOURCES:
        if not pending:
            break
        try:
            found = source.quotes(chain, pending, max_age_s=max_age_s, priority=priority, conn=conn)
        except Exception as exc:  # noqa: BLE001 - a broken source must not fail the batch
            log.warning("price source %s raised for %d tokens: %s", source.name, len(pending), exc)
            continue
        still: list[str] = []
        for addr in pending:
            hit = found.get(addr)
            if hit is None or hit.price_usd is None:
                if hit is not None:
                    reasons[addr] = hit.receipt
                still.append(addr)
                continue
            measure = hit.as_measure(max_age_s=max_age_s)
            for raw in by_addr[addr]:
                out[raw] = measure
        pending = still

    # Whatever is left is unknown, but it should say why rather than just be absent.
    for addr in pending:
        receipt = reasons.get(addr) or _unavailable("price.batch", "no source returned a priced pair")
        for raw in by_addr[addr]:
            out[raw] = Measure(
                value=None,
                basis=EvidenceBasis.UNAVAILABLE,
                receipt=receipt,
                freshness_budget_s=int(max_age_s),
            )
    return out


def liquidity_usd(
    chain: Chain,
    token: str,
    *,
    conn: Any = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    priority: Priority = Priority.POSITION,
) -> Measure:
    """USD depth of the pool that :func:`price_usd` would quote from.

    This is the number that decides whether a price is tradeable. It is intentionally the
    *chosen pool's* depth and not the sum across pools: you exit into one pool.
    """
    return quote(chain, token, max_age_s=max_age_s, priority=priority, conn=conn).liquidity_measure(
        max_age_s=max_age_s
    )


def is_thin(measure: Measure, floor_usd: Decimal = THIN_POOL_USD) -> bool:
    """``True`` when liquidity is known and below the floor.

    Unknown liquidity is *not* thin — it is unknown, and the caller must handle that case
    separately rather than letting a failed lookup read as a safe answer.
    """
    return measure.known and measure.value is not None and measure.value < floor_usd


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "PREFERRED_PAIR_MIN_SHARE",
    "THIN_POOL_USD",
    "DexScreenerSource",
    "PriceSource",
    "Quote",
    "is_thin",
    "liquidity_usd",
    "pick_pair",
    "price_usd",
    "prices_usd",
    "quote",
    "register_source",
    "reset_sources",
    "sources",
]
