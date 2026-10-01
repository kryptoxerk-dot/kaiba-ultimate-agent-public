"""Native-to-USD reference **with a time series**, so a fill is priced at its own moment.

A live fill gives an exact native cost and an exact token quantity. Turning that into the
USD entry the exit watchdog compares against needs one more number: what one SOL (or ETH,
or BNB) was worth *when the fill happened*. :mod:`kaiba.providers.prices` answers "what is
it worth now"; that is the wrong question for a fill that settled two minutes ago in a
market that moves, and the wrong question by a mile for a backfilled trade from last week.
So this module samples the native price on an interval, stores every sample, and answers
"what was it worth at ``ts_ms``" from the nearest stored sample — with the distance to that
sample stated in the receipt, and a refusal when the nearest one is too far away.

Three rules:

1. **Nearest sample within a tolerance, or nothing.** :func:`at` returns ``price_usd=None``
   with an ``UNAVAILABLE`` basis when no sample lies within ``tolerance_ms`` of the asked
   instant. It never interpolates, never extrapolates, and never serves the latest sample
   as if it were contemporaneous. The default tolerance is five minutes; see
   :data:`DEFAULT_TOLERANCE_MS` for why.
2. **A sample is stamped with the provider's answer time**, taken from the receipt and not
   from the wall clock at the moment we wrote the row. The two differ by the request
   latency, and by more when the limiter made us wait.
3. **A reference price comes from a deep pool.** The deepest DexScreener pool that prices
   the wrapped native is used — SOL/USDC on Raydium at ~$40M, WETH/USDT on Uniswap at
   ~$110M when this was written — and anything under :data:`MIN_LIQUIDITY_USD` is refused,
   because a native price from a $500 pool is a number, not a reference.

The sampler is a plain loop (:func:`run_sampler`) meant to run as its own process or
thread. :func:`ensure_recent` is the one-shot form for a caller that is about to trade and
wants a sample no older than a few seconds on the books before the fill lands.

USD is ``Decimal`` from the string the provider sent. Nothing here goes through a float.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn
from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Receipt, now_ms
from kaiba.providers import dexscreener as ds
from kaiba.providers._http import get_json, redact_text
from kaiba.providers.prices import pick_pair

log = logging.getLogger(__name__)

PROVIDER = ds.PROVIDER

#: The wrapped form of each chain's native, which is what a DEX pool actually holds. Every
#: address here was checked live on 2026-09-20: DexScreener returned a deep stable-quoted
#: pool as the deepest pair for each one. Chains missing from this map have no reference
#: source and :func:`sample` says so rather than guessing.
WRAPPED_NATIVE: dict[Chain, str] = {
    Chain.SOL: "So11111111111111111111111111111111111111112",
    Chain.ETH: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    Chain.BSC: "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    Chain.BASE: "0x4200000000000000000000000000000000000006",
}

#: How far from the asked instant a sample may lie and still be called contemporaneous.
#:
#: Five minutes, chosen against the two things it has to balance. The stop it feeds sits
#: 30% below entry (``protection.stop_loss_bps``), so an error in the native leg moves the
#: stop by the same fraction; SOL's typical five-minute move is well under 1% and its bad
#: days are low single digits, which is noise against a 30% stop. Against that, a fill is
#: applied by ``executor.reconcile``, which is a sweep on a timer: a sample taken by the
#: sampler at 30 s intervals is at most 15 s from the fill, and five minutes leaves room
#: for the sampler to miss several ticks before the answer degrades to ``UNAVAILABLE``.
#: Anything wider starts to price a fill with a different market's number.
DEFAULT_TOLERANCE_MS = 300_000

#: Sampler cadence. At 30 s the nearest sample is at most 15 s from any fill, and four
#: chains cost 480 DexScreener calls an hour against a limiter refilling one a second.
DEFAULT_INTERVAL_S = 30.0

#: Required by docs/CONTRACT.md for anything that is not single-shot. The sampler calls
#: once per chain per tick and shares the DexScreener bucket with the scanner, so a refusal
#: must wait for capacity rather than silently drop the tick.
DEFAULT_WAIT_FOR_SLOT_S = 10.0

#: A native reference must come from a pool deep enough that one trade cannot move it.
MIN_LIQUIDITY_USD = Decimal(1_000_000)

#: ``family.name`` for the sampler's own receipts.
ENDPOINT_AT = "native.at"
#: The fallback read, named so a receipt says which source answered.
ENDPOINT_FETCH_GMGN = "native.fetch.gmgn"


# --------------------------------------------------------------------------------------
# shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeSample:
    """One stored observation: what one whole native unit was worth in USD at ``ts_ms``."""

    chain: Chain
    ts_ms: int
    price_usd: Decimal
    source: str
    pair: str | None
    liquidity_usd: Decimal | None
    receipt: Receipt


@dataclass(frozen=True)
class NativePrice:
    """The answer to "what was one native unit worth at ``asked_ts_ms``?".

    ``price_usd is None`` means no sample lay within ``tolerance_ms``. It never means zero,
    and ``distance_ms`` is still filled in when a sample exists outside the tolerance, so
    the caller can say how far off the nearest one was.
    """

    chain: Chain
    asked_ts_ms: int
    price_usd: Decimal | None
    sample_ts_ms: int | None
    distance_ms: int | None
    tolerance_ms: int
    basis: EvidenceBasis
    receipt: Receipt
    source: str | None = None

    @property
    def known(self) -> bool:
        return self.price_usd is not None and self.basis is not EvidenceBasis.UNAVAILABLE


@dataclass
class SamplerReport:
    """What one :func:`run_sampler` run did. Counts, never prices."""

    iterations: int = 0
    recorded: int = 0
    duplicates: int = 0
    failures: int = 0
    per_chain: dict[str, dict[str, int]] = field(default_factory=dict)

    def bump(self, chain: Chain, key: str) -> None:
        self.per_chain.setdefault(chain.value, {"recorded": 0, "duplicates": 0, "failures": 0})[key] += 1
        setattr(self, key, getattr(self, key) + 1)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _s(value: Decimal | None) -> str | None:
    """Plain decimal text: no exponent, so ``1E-7`` never lands in a TEXT column."""
    return None if value is None else format(value, "f")


def _unavailable(endpoint: str, note: str, *, observed_at_ms: int | None = None) -> Receipt:
    kw: dict[str, Any] = {}
    if observed_at_ms is not None:
        kw["observed_at_ms"] = observed_at_ms
    return Receipt(
        provider=PROVIDER,
        endpoint=endpoint,
        basis=EvidenceBasis.UNAVAILABLE,
        note=redact_text(note)[:300],
        **kw,
    )


def _row_to_sample(row: dict[str, Any]) -> NativeSample | None:
    price = _dec(row.get("price_usd"))
    if price is None or price <= 0:
        return None
    try:
        receipt = Receipt.model_validate_json(row.get("receipt_json") or "{}")
    except Exception:  # noqa: BLE001 - a corrupt receipt does not invalidate the sample
        receipt = Receipt(
            provider=str(row.get("source") or PROVIDER),
            endpoint=ds.EP_TOKEN_PAIRS,
            observed_at_ms=int(row["ts_ms"]),
            basis=EvidenceBasis.PROVIDER_REPORTED,
        )
    return NativeSample(
        chain=Chain(row["chain"]),
        ts_ms=int(row["ts_ms"]),
        price_usd=price,
        source=str(row.get("source") or PROVIDER),
        pair=row.get("pair"),
        liquidity_usd=_dec(row.get("liquidity_usd")),
        receipt=receipt,
    )


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------


def _gmgn_token_info(address: str, chain: Chain):
    """The provider call, as its own seam so a test can drive it without a subprocess."""
    from kaiba.providers.gmgn_cli import token_info

    return token_info(address, chain)


def _fetch_via_gmgn(
    chain: Chain, mint: str, *, conn: sqlite3.Connection | None = None
) -> tuple["NativeSample | None", Receipt]:
    """The native price from GMGN, when DexScreener has no capacity left to give.

    MEASURED 2026-09-23 on the live box. ``price.token_pairs`` was running at 60.1 calls a
    minute against a bucket that refills at 60 a minute, so ``dexscreener`` sat at
    ``credit_milli = -18273`` permanently. This sampler asks for three rows a minute and
    got none of them for 1h54m -- and a missing native price is not a missing nicety:
    ``evm_cost_model`` cannot value a round trip without it, so ``sizing_band`` returns
    ``cost_model_unavailable`` and the risk gate refuses EVERY entry with
    ``no_viable_band``. Robinhood took 25 of 37 skips that way in 90 minutes, and sol and
    bsc were sizing against the same hole.

    Priority could not fix it. The sampler already runs at ``Priority.POSITION``, above the
    scanner's ``DISCOVERY``, but priority only orders who takes the NEXT token; when the
    bucket is empty there is no next token, and ``wait_for_slot_s`` waits for a refill that
    a saturating caller consumes the instant it lands.

    So the answer is a second source rather than a bigger share of the first. The native
    asset is the one price in this system that is not token-specific -- four fixed,
    deeply-liquid mints -- and it has no business competing with per-token discovery for a
    bucket. GMGN already answers it (SOL 118.14, ETH 2741.27, BNB 785.29, measured), on its
    own healthy bucket, at three calls a minute.

    It is a FALLBACK and not a replacement: DexScreener is still asked first, and still
    gives the pair and the liquidity this one cannot. The sample records ``source="gmgn"``
    so a reader can always tell which answered, and no liquidity is claimed for a reading
    that has none to report.
    """
    try:
        got = _gmgn_token_info(mint, chain)
    except Exception as exc:  # noqa: BLE001
        return None, _unavailable(ENDPOINT_FETCH_GMGN, f"gmgn:{type(exc).__name__}")
    body = getattr(got, "data", None)
    if not isinstance(body, dict):
        return None, _unavailable(ENDPOINT_FETCH_GMGN, "gmgn returned no payload")
    block = body.get("price")
    raw = block.get("price") if isinstance(block, dict) else block
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None, _unavailable(ENDPOINT_FETCH_GMGN, "gmgn price is unparseable")
    if price <= 0:
        return None, _unavailable(ENDPOINT_FETCH_GMGN, "gmgn reported a non-positive price")
    receipt = Receipt(
        provider="gmgn",
        endpoint=ENDPOINT_FETCH_GMGN,
        observed_at_ms=now_ms(),
        basis=EvidenceBasis.PROVIDER_REPORTED,
        note=f"{chain.value} native {price} USD from gmgn (dexscreener had no capacity)",
    )
    return (
        NativeSample(
            chain=chain,
            ts_ms=now_ms(),
            price_usd=price,
            source="gmgn",
            pair=None,
            liquidity_usd=None,
            receipt=receipt,
        ),
        receipt,
    )


def fetch(
    chain: Chain,
    *,
    conn: sqlite3.Connection | None = None,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    priority: Priority = Priority.POSITION,
    min_liquidity_usd: Decimal = MIN_LIQUIDITY_USD,
) -> tuple[NativeSample | None, Receipt]:
    """One fresh observation of the native price. Never raises; never caches.

    This goes to the DexScreener pair route directly rather than through
    :func:`kaiba.providers.prices.quote`, for one reason: ``quote`` cannot be told to wait
    for limiter capacity, and a sampler that shares a bucket with the scanner would then
    lose exactly the ticks that fall during a busy scan. The pool choice is the same
    :func:`kaiba.providers.prices.pick_pair` the watchdog's price reads use.
    """
    mint = WRAPPED_NATIVE.get(chain)
    slug = ds.slug_for_chain(chain)
    if mint is None or slug is None:
        return None, _unavailable(ds.EP_TOKEN_PAIRS, f"no native reference source for chain {chain.value}")
    got = get_json(
        PROVIDER,
        ds.EP_TOKEN_PAIRS,
        f"{ds.BASE_URL}/token-pairs/v1/{slug}/{mint}",
        priority=priority,
        ttl_s=0.0,  # a sample is an observation; a cached one would be a lie about its time
        timeout_s=6.0,
        retries=2,
        wait_for_slot_s=wait_for_slot_s,
        conn=conn,
    )
    if not got.ok:
        sample, receipt = _fetch_via_gmgn(chain, mint, conn=conn)
        return (sample, receipt) if sample is not None else (None, got.receipt)
    pairs = [p for p in (ds._parse_pair(r) for r in ds._rows(got.data)) if p is not None]
    best = pick_pair(pairs, mint)
    if best is None or best.price_usd is None:
        return None, _unavailable(
            ds.EP_TOKEN_PAIRS,
            f"no pair prices {chain.value} native ({len(pairs)} pair(s) returned)",
            observed_at_ms=got.receipt.observed_at_ms,
        )
    if best.liquidity_usd is None or best.liquidity_usd < min_liquidity_usd:
        depth = "?" if best.liquidity_usd is None else f"{best.liquidity_usd:,.0f}"
        return None, _unavailable(
            ds.EP_TOKEN_PAIRS,
            f"deepest pool {best.label} has liq_usd={depth}, below the {min_liquidity_usd:,.0f} floor",
            observed_at_ms=got.receipt.observed_at_ms,
        )
    receipt = ds.note_receipt(got.receipt, f"native {chain.value} from {best.label}")
    sample = NativeSample(
        chain=chain,
        ts_ms=int(receipt.observed_at_ms),
        price_usd=best.price_usd,
        source=PROVIDER,
        pair=best.label,
        liquidity_usd=best.liquidity_usd,
        receipt=receipt,
    )
    return sample, receipt


def record(sample: NativeSample, conn: sqlite3.Connection | None = None) -> bool:
    """Store one sample. ``False`` when a row for that exact instant already exists."""
    c = conn or get_conn()
    cur = c.execute(
        "INSERT OR IGNORE INTO native_prices (chain, ts_ms, price_usd, source, pair, liquidity_usd, "
        "receipt_json) VALUES (?,?,?,?,?,?,?)",
        (
            sample.chain.value,
            int(sample.ts_ms),
            _s(sample.price_usd),
            sample.source,
            sample.pair,
            _s(sample.liquidity_usd),
            sample.receipt.model_dump_json(),
        ),
    )
    return bool(cur.rowcount)


def sample(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    priority: Priority = Priority.POSITION,
) -> NativeSample | None:
    """Fetch and store one sample. ``None`` when the provider had nothing usable."""
    c = conn or get_conn()
    got, receipt = fetch(chain, conn=c, wait_for_slot_s=wait_for_slot_s, priority=priority)
    if got is None:
        log.warning("native price sample for %s unavailable: %s", chain.value, receipt.note)
        return None
    record(got, c)
    return got


# --------------------------------------------------------------------------------------
# lookups
# --------------------------------------------------------------------------------------


def latest(chain: Chain, conn: sqlite3.Connection | None = None) -> NativeSample | None:
    """The most recent stored sample, whatever its age. Callers must check ``ts_ms``."""
    c = conn or get_conn()
    row = fetch_one(
        c, "SELECT * FROM native_prices WHERE chain=? ORDER BY ts_ms DESC LIMIT 1", (chain.value,)
    )
    return _row_to_sample(row) if row else None


#: Chains whose native asset IS another chain's native asset, so its USD price may be
#: read from that chain's samples.
#:
#: Robinhood Chain's gas and quote asset is ETH. A unit of ETH is worth the same in USD
#: whichever chain it sits on, so this is an IDENTITY, not an approximation or a proxy.
#: It is spelled out here rather than assumed because ``native_price`` cannot sample
#: robinhood directly: DexScreener has no slug for it and it has no ``WRAPPED_NATIVE``
#: entry, so ``native_prices`` held 0 robinhood rows.
#:
#: MEASURED 2026-09-22: that gap made ``at(Chain.ROBINHOOD)`` return UNAVAILABLE, which
#: made every robinhood round-trip cost unpriceable, which made ``sizing_band`` refuse,
#: which is why a funded and armed robinhood chain had never placed an order. The receipt
#: always names the chain the sample actually came from, so the substitution is visible
#: rather than silent.
NATIVE_PRICE_ALIAS: dict[Chain, Chain] = {Chain.ROBINHOOD: Chain.ETH}


def price_source_chain(chain: Chain) -> Chain:
    """Which chain's samples answer for ``chain``'s native asset."""
    return NATIVE_PRICE_ALIAS.get(chain, chain)


def _nearest(chain: Chain, ts_ms: int, c: sqlite3.Connection) -> NativeSample | None:
    """Nearest stored sample on either side of ``ts_ms``. Two indexed reads, no ``ABS()``."""
    before = fetch_one(
        c,
        "SELECT * FROM native_prices WHERE chain=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
        (chain.value, ts_ms),
    )
    after = fetch_one(
        c,
        "SELECT * FROM native_prices WHERE chain=? AND ts_ms>? ORDER BY ts_ms ASC LIMIT 1",
        (chain.value, ts_ms),
    )
    candidates = [s for s in (_row_to_sample(r) for r in (before, after) if r) if s is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(s.ts_ms - ts_ms))


def at(
    chain: Chain,
    ts_ms: int,
    conn: sqlite3.Connection | None = None,
    *,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
) -> NativePrice:
    """What one native unit was worth at ``ts_ms``, from the nearest sample within tolerance.

    The receipt always says how far away the sample was. Outside the tolerance the price
    is ``None`` with an ``UNAVAILABLE`` basis — and the receipt still names the distance,
    so "no sample within 5 min" and "no samples at all" read differently.
    """
    c = conn or get_conn()
    ts_ms = int(ts_ms)
    tolerance_ms = int(tolerance_ms)
    source_chain = price_source_chain(chain)
    via = "" if source_chain is chain else f" via {source_chain.value} (same native asset)"
    found = _nearest(source_chain, ts_ms, c)
    if found is None:
        return NativePrice(
            chain=chain,
            asked_ts_ms=ts_ms,
            price_usd=None,
            sample_ts_ms=None,
            distance_ms=None,
            tolerance_ms=tolerance_ms,
            basis=EvidenceBasis.UNAVAILABLE,
            receipt=Receipt(
                provider=PROVIDER,
                endpoint=ENDPOINT_AT,
                basis=EvidenceBasis.UNAVAILABLE,
                note=f"no {source_chain.value} native price samples stored{via}",
            ),
        )
    distance = abs(found.ts_ms - ts_ms)
    if distance > tolerance_ms:
        return NativePrice(
            chain=chain,
            asked_ts_ms=ts_ms,
            price_usd=None,
            sample_ts_ms=found.ts_ms,
            distance_ms=distance,
            tolerance_ms=tolerance_ms,
            basis=EvidenceBasis.UNAVAILABLE,
            receipt=Receipt(
                provider=PROVIDER,
                endpoint=ENDPOINT_AT,
                observed_at_ms=found.ts_ms,
                basis=EvidenceBasis.UNAVAILABLE,
                note=(
                    f"nearest {source_chain.value} native sample is {distance} ms away "
                    f"(tolerance {tolerance_ms} ms); not contemporaneous{via}"
                ),
            ),
            source=found.source,
        )
    return NativePrice(
        chain=chain,
        asked_ts_ms=ts_ms,
        price_usd=found.price_usd,
        sample_ts_ms=found.ts_ms,
        distance_ms=distance,
        tolerance_ms=tolerance_ms,
        basis=EvidenceBasis.PROVIDER_REPORTED,
        receipt=Receipt(
            provider=found.source,
            endpoint=ENDPOINT_AT,
            observed_at_ms=found.ts_ms,
            basis=EvidenceBasis.PROVIDER_REPORTED,
            note=(
                f"{chain.value} native {found.price_usd} USD{via} from sample {distance} ms away "
                f"(tolerance {tolerance_ms} ms) via {found.pair or found.source}"
            )[:300],
        ),
        source=found.source,
    )


def history(
    chain: Chain,
    since_ms: int,
    until_ms: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[NativeSample]:
    """Stored samples in a window, oldest first. For reports and the tolerance audit."""
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT * FROM native_prices WHERE chain=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms ASC",
        (chain.value, int(since_ms), int(until_ms if until_ms is not None else now_ms())),
    )
    return [s for s in (_row_to_sample(r) for r in rows) if s is not None]


def ensure_recent(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    max_age_ms: int = 60_000,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> NativeSample | None:
    """A sample no older than ``max_age_ms``, taking a fresh one only if needed.

    The one-shot form of the sampler, for a caller about to submit a live order: a sample
    on the books seconds before the fill is what makes the entry price measured rather
    than estimated, without depending on a background process being alive.
    """
    c = conn or get_conn()
    have = latest(chain, c)
    if have is not None and now_ms() - have.ts_ms <= int(max_age_ms):
        return have
    return sample(chain, c, wait_for_slot_s=wait_for_slot_s)


def prune(conn: sqlite3.Connection | None = None, *, keep_days: int = 90) -> int:
    """Drop samples older than ``keep_days``. Returns the rows removed."""
    c = conn or get_conn()
    cutoff = now_ms() - int(keep_days) * 86_400_000
    cur = c.execute("DELETE FROM native_prices WHERE ts_ms < ?", (cutoff,))
    return int(cur.rowcount or 0)


# --------------------------------------------------------------------------------------
# the sampler
# --------------------------------------------------------------------------------------


def run_sampler(
    chains: Sequence[Chain] | None = None,
    *,
    interval_s: float = DEFAULT_INTERVAL_S,
    conn: sqlite3.Connection | None = None,
    stop: threading.Event | None = None,
    max_iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> SamplerReport:
    """Sample every chain on an interval until ``stop`` is set or ``max_iterations`` is hit.

    One failure is a warning in the log; five in a row for a chain is a ``SYSTEM`` event,
    because a fill landing during that gap will be priced ``UNAVAILABLE`` and the position
    will come out without a stop. The operator should know before that happens.
    """
    c = conn or get_conn()
    wanted = list(chains) if chains else [ch for ch in WRAPPED_NATIVE]
    report = SamplerReport()
    streak: dict[Chain, int] = dict.fromkeys(wanted, 0)
    emit(
        EventKind.SYSTEM,
        {
            "event": "native_price_sampler_started",
            "chains": [ch.value for ch in wanted],
            "interval_s": float(interval_s),
        },
        conn=c,
    )
    while True:
        if stop is not None and stop.is_set():
            break
        if max_iterations is not None and report.iterations >= max_iterations:
            break
        started = time.monotonic()
        report.iterations += 1
        for chain in wanted:
            try:
                got, receipt = fetch(chain, conn=c, wait_for_slot_s=wait_for_slot_s)
                if got is None:
                    streak[chain] += 1
                    report.bump(chain, "failures")
                    log.warning("native price sample for %s unavailable: %s", chain.value, receipt.note)
                    if streak[chain] == 5:
                        emit(
                            EventKind.SYSTEM,
                            {
                                "event": "native_price_sampler_starved",
                                "chain": chain.value,
                                "consecutive_failures": streak[chain],
                                "last_note": receipt.note,
                                "impact": "fills on this chain will be priced UNAVAILABLE and "
                                "open without a stop until sampling resumes",
                            },
                            chain=chain,
                            level="warn",
                            dedupe_key=f"native_price:starved:{chain.value}",
                            conn=c,
                        )
                    continue
                streak[chain] = 0
                report.bump(chain, "recorded" if record(got, c) else "duplicates")
            except Exception as exc:  # noqa: BLE001 - one bad tick must not stop the series
                streak[chain] += 1
                report.bump(chain, "failures")
                log.warning("native price sampler error for %s: %s", chain.value, redact_text(str(exc)))
        if max_iterations is not None and report.iterations >= max_iterations:
            break
        if stop is not None and stop.is_set():
            break
        elapsed = time.monotonic() - started
        remaining = max(0.0, float(interval_s) - elapsed)
        if remaining > 0:
            sleep(remaining)
    return report


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_TOLERANCE_MS",
    "DEFAULT_WAIT_FOR_SLOT_S",
    "MIN_LIQUIDITY_USD",
    "PROVIDER",
    "WRAPPED_NATIVE",
    "NativePrice",
    "NativeSample",
    "SamplerReport",
    "at",
    "ensure_recent",
    "fetch",
    "history",
    "latest",
    "prune",
    "record",
    "run_sampler",
    "sample",
]
