"""Creator history — the fact that unstarves tier-0 triage.

## Why this module exists

Tier 0 (``kaiba.execution.triage``) decides in 0.61 ms and, measured on 27 real pump.fun
launches on 2026-09-20, produced **reject 1, defer 26, promote 0**. It was not saturated,
it was *starved*: the ``creators`` table held zero rows, so the only per-launch query it
makes —

    SELECT launches, graduated, rugged FROM creators WHERE chain=? AND address=?

— returned nothing for every launch, and a screen with no distinguishing fact about any
candidate can only defer. Lowering ``promote_threshold`` would have relabelled noise. The
missing input was creator history, so this module goes and gets it.

Creator identity is one of the few things published research says genuinely separates
launches: on all 15.2M pump.fun coins the top 1% of creator *clusters* create 58.6% of
all coins, and three-hop funding clusters cover 68.78% of coins. ``docs/EDGE-AND-VARIABLES``
§1 #4 is the caveat that keeps this honest — a per-*address* dev-history lookup is defeated
55% of the time, which is why an absent creator row is "no opinion" here and never a
penalty, and why we never write a zero row for a creator we have not actually measured.

## How the history is obtained

Everything comes from pump.fun's public frontend API, free, no key. Three facts about it
were measured here on 2026-09-20 and are load-bearing:

* ``?limit=`` is **capped server-side at 70** regardless of what you ask for. Asking for
  100 and stepping the offset by 100 silently skips 30 coins a page.
* ``?offset=`` is **capped at 1000**; 1010 returns an empty list. So any single index is
  at most 1,070 rows deep — about **80 minutes** of the recent-launch index and about
  **25 hours** of the ``complete=true`` (graduated) index.
* ``?creator=<address>`` filters to one creator and is *not* subject to the recency
  window: it returns that creator's whole launch history, oldest launch included, with
  ``complete`` and ``ath_market_cap`` per coin.

That last one is what makes this defensible. The naive design — page the recent index and
count creators inside the window — can only ever report "launches in the last 80 minutes",
which undercounts every creator who spaces their launches out. Instead:

1. **Discovery.** Page the recent-launch index (and optionally the graduated index) to
   learn *which* creators are currently active. This is a sampling step; its counts are
   thrown away.
2. **Per-creator history.** One request per discovered creator against ``?creator=``,
   which returns their lifetime launches. ``launches`` and ``graduated`` are counted from
   that, so they are lifetime figures, not window figures.

Step 2 is also what makes the backfill **idempotent**: every run recomputes an absolute
count from a complete fetch and overwrites. Nothing is ever incremented, so re-running
corrects a count rather than doubling it.

## Pacing

One request per second, held. A prior agent pulled 1,038 launches over 1.26 hours at that
rate without a single 429. The unknown-provider default in ``kaiba.core.limiter`` already
gives ``min_interval_ms=1000``, but this module does not depend on that staying true —
:class:`BackfillConfig.min_interval_s` paces independently and the effective rate is the
slower of the two. A 429 that gets our IP limited costs far more than a slow backfill.

## What this module deliberately does NOT write

**``rugged`` stays 0, meaning "not measured".** See :data:`RUGGED_DECISION`. There is no
rug flag in the data and the obvious proxy — reached liquidity, then collapsed — is
disqualified by our own evidence file: EDGE §2 records that *84% of graduates are down
more than 70% within 20 minutes*. Collapse after a pump is the base rate of the asset
class, not a dev action. A ``rugged`` count built on it would put most active creators
over ``triage.TriageConfig.reject_min_creator_rugs`` (2) and start refusing real launches
on invented evidence. :func:`collapse_diagnostic` measures exactly how bad that would be
so the decision stays checkable rather than asserted.

**``score`` stays NULL.** Triage does not read it, and an unjustified creator score is
another invented threshold in a system that already has too many.

``median_peak_mcap_usd`` *is* written, because ``ath_market_cap`` is a real observed peak
reported by the venue (present on 140/140 records sampled) rather than something we
inferred. It is a Decimal stored as TEXT, per ``docs/CONTRACT.md``.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, upsert
from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import EventKind, Chain, now_ms
from kaiba.providers._http import get_json

log = logging.getLogger(__name__)

PROVIDER = "pumpfun"
BASE_URL = "https://frontend-api-v3.pump.fun/coins"

#: Server-side cap on ``limit``. Measured 2026-09-20: ``limit=100`` and ``limit=200`` both
#: return 70 rows. Stepping the offset by anything larger than this silently drops coins.
PAGE_LIMIT = 70

#: Largest ``offset`` that returns anything. Measured 2026-09-20: 1000 returns 70 rows,
#: 1010 returns an empty list. Any one index is therefore at most 1,070 rows deep.
MAX_OFFSET = 1000

#: Discovery indexes. ``created`` is the recent-launch firehose (~80 minutes deep);
#: ``complete`` is graduated coins only (~25 hours deep) and is the cheapest way to find
#: the creators who will actually make triage promote.
INDEX_CREATED = "created"
INDEX_COMPLETE = "complete"
INDEXES: tuple[str, ...] = (INDEX_CREATED, INDEX_COMPLETE)

RUGGED_DECISION = (
    "NOT MEASURED. pump.fun exposes no rug flag, and the only proxy the listing data "
    "supports is 'reached a market cap, then gave it back'. docs/EDGE-AND-VARIABLES.md "
    "§2 records that 84% of graduates are down more than 70% within 20 minutes, so "
    "collapse is the base rate of the asset class rather than evidence of a dev action, "
    "and it is not a claim about the creator. Measured here on 46,128 launches by 563 "
    "creators (2026-09-20): 57.4% of the coins that reached $10k of market cap now hold "
    "under a fifth of their peak, and writing that into `rugged` would put 121 of 563 "
    "creators (21.5%) over triage's reject_min_creator_rugs=2 — a fifth of the market "
    "refused on a label nothing has validated against an outcome. Run "
    "creators.collapse_diagnostic() to re-measure before reversing this."
)


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackfillConfig:
    """Knobs for one backfill run. Nothing here feeds a trading decision.

    ``min_interval_s`` is the pacing floor this module enforces itself, independent of the
    limiter, because the limiter's 1 req/s for ``pumpfun`` comes from the unknown-provider
    default rather than from an entry someone chose.
    """

    min_interval_s: float = 1.0
    timeout_s: float = 20.0
    retries: int = 3
    #: Wait this long for limiter capacity rather than losing the page. Required: this
    #: adapter makes hundreds of calls per logical operation (docs/CONTRACT.md).
    wait_for_slot_s: float = 120.0
    #: Per-creator history is stable enough to cache between runs; the discovery index is
    #: not cached at all because its whole job is to be current.
    creator_ttl_s: float = 21_600.0
    index_ttl_s: float = 0.0
    #: Skip a creator whose row was refreshed more recently than this. 0 disables.
    refresh_after_s: float = 43_200.0


DEFAULT_CONFIG = BackfillConfig()


# --------------------------------------------------------------------------------------
# pacing
# --------------------------------------------------------------------------------------


class _Pacer:
    """Hold a minimum wall-clock gap between outbound requests, process-wide."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self, min_interval_s: float) -> None:
        if min_interval_s <= 0:
            return
        with self._lock:
            gap = time.monotonic() - self._last
            if self._last and gap < min_interval_s:
                time.sleep(min_interval_s - gap)
            self._last = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._last = 0.0


PACER = _Pacer()


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------


def _text(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _decimal(value: Any) -> Decimal | None:
    """Missing or unparseable is ``None``. Never 0 — see docs/CONTRACT.md rule 2."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _ms(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """One coin as the venue reports it. Absent fields stay ``None``."""

    mint: str
    creator: str | None = None
    name: str | None = None
    symbol: str | None = None
    created_ms: int | None = None
    #: ``None`` means the venue did not say. That is not the same as "did not graduate",
    #: and :func:`summarise_creator` counts it separately rather than as a negative.
    complete: bool | None = None
    usd_market_cap: Decimal | None = None
    #: Venue-reported all-time-high market cap in USD. Present on 140/140 records sampled
    #: 2026-09-20; equals ``usd_market_cap`` exactly when the coin is at its high, which is
    #: how the USD denomination was confirmed.
    ath_market_cap_usd: Decimal | None = None
    ath_ms: int | None = None
    image_uri: str | None = None
    metadata_uri: str | None = None
    pool: str | None = None

    @property
    def graduated(self) -> bool:
        return self.complete is True


def parse_coin(raw: Mapping[str, Any]) -> LaunchRecord | None:
    """Normalise one API record. Returns ``None`` when there is no usable mint."""
    mint = _text(raw.get("mint"))
    if mint is None:
        return None
    complete = raw.get("complete")
    return LaunchRecord(
        mint=mint,
        creator=_text(raw.get("creator")),
        name=_text(raw.get("name")),
        symbol=_text(raw.get("symbol")),
        created_ms=_ms(raw.get("created_timestamp")),
        complete=bool(complete) if isinstance(complete, bool) else None,
        usd_market_cap=_decimal(raw.get("usd_market_cap")),
        ath_market_cap_usd=_decimal(raw.get("ath_market_cap")),
        ath_ms=_ms(raw.get("ath_market_cap_timestamp")),
        image_uri=_text(raw.get("image_uri")),
        metadata_uri=_text(raw.get("metadata_uri")),
        pool=_text(raw.get("pump_swap_pool")) or _text(raw.get("pool_address")),
    )


def parse_page(payload: Any) -> list[LaunchRecord]:
    """Accept the bare list the API returns, or a ``{"coins": [...]}`` envelope."""
    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("coins") or payload.get("data") or []
    if not isinstance(rows, list):
        return []
    out: list[LaunchRecord] = []
    for raw in rows:
        if isinstance(raw, Mapping):
            rec = parse_coin(raw)
            if rec is not None:
                out.append(rec)
    return out


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageResult:
    """One page. ``ok=False`` means the provider did not answer; the rows are not empty,
    they are *unknown*, and the caller must not treat the history as complete."""

    records: tuple[LaunchRecord, ...]
    ok: bool
    note: str | None = None

    def __len__(self) -> int:
        return len(self.records)


def fetch_page(
    *,
    offset: int = 0,
    creator: str | None = None,
    complete: bool | None = None,
    config: BackfillConfig = DEFAULT_CONFIG,
    conn: sqlite3.Connection | None = None,
) -> PageResult:
    """One page of the pump.fun coin index. Never raises.

    ``creator`` filters to a single creator's lifetime history; ``complete=True`` filters
    to graduated coins only.
    """
    params: dict[str, Any] = {
        "sort": "created_timestamp",
        "order": "DESC",
        "limit": PAGE_LIMIT,
        "offset": max(0, int(offset)),
    }
    if creator:
        params["creator"] = creator
    if complete:
        params["complete"] = "true"

    PACER.wait(config.min_interval_s)
    got = get_json(
        PROVIDER,
        "coins.list",
        BASE_URL,
        params=params,
        priority=Priority.RESEARCH,
        ttl_s=config.creator_ttl_s if creator else config.index_ttl_s,
        timeout_s=config.timeout_s,
        retries=config.retries,
        wait_for_slot_s=config.wait_for_slot_s,
        conn=conn,
    )
    if not got.ok:
        return PageResult((), False, got.receipt.note)
    return PageResult(tuple(parse_page(got.data)), True)


def fetch_index(
    *,
    pages: int,
    index: str = INDEX_CREATED,
    config: BackfillConfig = DEFAULT_CONFIG,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[LaunchRecord], int, int]:
    """Page a global index. Returns ``(records, pages_fetched, pages_failed)``.

    Stops at the measured ``offset`` cap rather than looping forever on empty pages.
    """
    wanted = max(0, int(pages))
    seen: dict[str, LaunchRecord] = {}
    fetched = failed = 0
    for i in range(wanted):
        offset = i * PAGE_LIMIT
        if offset > MAX_OFFSET:
            log.info("creators: stopping at the measured offset cap (%d)", MAX_OFFSET)
            break
        page = fetch_page(
            offset=offset,
            complete=(index == INDEX_COMPLETE),
            config=config,
            conn=conn,
        )
        if not page.ok:
            failed += 1
            continue
        fetched += 1
        if not page.records:
            break
        for rec in page.records:
            seen.setdefault(rec.mint, rec)
    return list(seen.values()), fetched, failed


# --------------------------------------------------------------------------------------
# per-creator aggregation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatorHistory:
    """What we measured about one creator. Every field is a count of things we saw."""

    chain: Chain
    address: str
    launches: int
    graduated: int
    #: Coins whose ``complete`` flag the venue did not report. Counted, never assumed.
    complete_unknown: int = 0
    median_peak_mcap_usd: Decimal | None = None
    peaks_observed: int = 0
    first_launch_ms: int | None = None
    last_launch_ms: int | None = None
    #: True when the history hit the provider's offset cap, so ``launches`` is a floor.
    truncated: bool = False
    #: ``rugged`` is not a field. See :data:`RUGGED_DECISION`.

    @property
    def graduation_rate(self) -> float | None:
        return (self.graduated / self.launches) if self.launches else None


def summarise_creator(
    address: str, records: Iterable[LaunchRecord], *, chain: Chain = Chain.SOL, truncated: bool = False
) -> CreatorHistory | None:
    """Fold a creator's launch records into one row. ``None`` when there is nothing to say.

    Returning ``None`` for an empty history matters: a creator with no measured launches
    must stay *absent* from the table, which triage already reads as "no opinion". A zero
    row would be a claim.
    """
    rows = [r for r in records if r.mint]
    if not rows:
        return None
    peaks = [r.ath_market_cap_usd for r in rows if r.ath_market_cap_usd is not None]
    stamps = [r.created_ms for r in rows if r.created_ms is not None]
    return CreatorHistory(
        chain=chain,
        address=address,
        launches=len(rows),
        graduated=sum(1 for r in rows if r.graduated),
        complete_unknown=sum(1 for r in rows if r.complete is None),
        median_peak_mcap_usd=statistics.median(sorted(peaks)) if peaks else None,
        peaks_observed=len(peaks),
        first_launch_ms=min(stamps) if stamps else None,
        last_launch_ms=max(stamps) if stamps else None,
        truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class HistoryFetch:
    """One creator's history plus *why* it is missing when it is.

    "The provider did not answer" and "the provider answered, with nothing" produce the
    same absent row but are different facts about the world, and collapsing them hides a
    dead endpoint behind a plausible-looking count of quiet creators.
    """

    history: CreatorHistory | None
    records: tuple[LaunchRecord, ...] = ()
    #: False only when a page failed. True with ``history is None`` means the venue
    #: genuinely reports no launches for this address.
    ok: bool = True
    pages: int = 0


def fetch_creator_history(
    address: str,
    *,
    chain: Chain = Chain.SOL,
    config: BackfillConfig = DEFAULT_CONFIG,
    conn: sqlite3.Connection | None = None,
) -> HistoryFetch:
    """Page one creator's whole launch history.

    A failed page abandons the creator rather than returning what we got: a partial
    history produces an undercount that looks exactly like a real low count, and an
    undercounted serial launcher is one triage would score as ordinary. Absent beats
    wrong, and absent is what triage already reads as "no opinion".
    """
    collected: dict[str, LaunchRecord] = {}
    offset = 0
    truncated = False
    pages = 0
    while True:
        page = fetch_page(offset=offset, creator=address, config=config, conn=conn)
        pages += 1
        if not page.ok:
            log.warning("creators: history for %s incomplete (%s); skipping", address, page.note)
            return HistoryFetch(None, (), ok=False, pages=pages)
        for rec in page.records:
            collected.setdefault(rec.mint, rec)
        if len(page.records) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset > MAX_OFFSET:
            truncated = True
            break
    records = tuple(collected.values())
    history = summarise_creator(address, records, chain=chain, truncated=truncated)
    return HistoryFetch(history, records, ok=True, pages=pages)


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def upsert_creator(
    history: CreatorHistory, conn: sqlite3.Connection | None = None
) -> None:
    """Write one creator row. Absolute counts, never increments — that is the idempotency.

    ``rugged`` is written as 0 meaning *not measured* (:data:`RUGGED_DECISION`); the column
    is ``NOT NULL`` so ``None`` is not available. ``score`` is left NULL deliberately.
    """
    c = conn or get_conn()
    peak = history.median_peak_mcap_usd
    upsert(
        c,
        "creators",
        {
            "chain": history.chain.value,
            "address": history.address,
            "launches": int(history.launches),
            "graduated": int(history.graduated),
            "rugged": 0,
            "median_peak_mcap_usd": None if peak is None else str(peak),
            "score": None,
            "updated_ms": now_ms(),
        },
        conflict=["chain", "address"],
        update=["launches", "graduated", "rugged", "median_peak_mcap_usd", "score", "updated_ms"],
    )


def read_creator(
    address: str, *, chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    return fetch_one(
        conn or get_conn(),
        "SELECT chain, address, launches, graduated, rugged, median_peak_mcap_usd, score, "
        "updated_ms FROM creators WHERE chain=? AND address=?",
        (chain.value, address),
    )


def _fresh_addresses(
    conn: sqlite3.Connection, chain: Chain, refresh_after_s: float
) -> set[str]:
    if refresh_after_s <= 0:
        return set()
    cutoff = now_ms() - int(refresh_after_s * 1000)
    rows = fetch_all(
        conn,
        "SELECT address FROM creators WHERE chain=? AND updated_ms >= ?",
        (chain.value, cutoff),
    )
    return {str(r["address"]) for r in rows}


# --------------------------------------------------------------------------------------
# distributions and the rug question
# --------------------------------------------------------------------------------------

#: Buckets for the launches-per-creator distribution. 60+ is the paper's one-coin-a-second
#: tier; 5+ is where triage's (invented) serial-launcher penalty starts.
DISTRIBUTION_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("1", 1, 1),
    ("2-4", 2, 4),
    ("5-9", 5, 9),
    ("10-59", 10, 59),
    ("60+", 60, None),
)


def launch_distribution(counts: Sequence[int]) -> dict[str, Any]:
    """Launches-per-creator distribution plus the top-1% concentration share.

    The published claim this is compared against — top 1% of creator *clusters* create
    58.6% of all coins — is measured on funding clusters across all 15.2M coins. This is
    measured on bare addresses in whatever sample was fetched, so a smaller number here is
    the expected result of a narrower definition, not a refutation.
    """
    values = sorted((int(c) for c in counts if c > 0), reverse=True)
    n = len(values)
    total = sum(values)
    if not n:
        return {"creators": 0, "coins": 0, "buckets": {}, "top1pct_share": None}
    buckets: dict[str, int] = {}
    for label, lo, hi in DISTRIBUTION_BUCKETS:
        buckets[label] = sum(1 for v in values if v >= lo and (hi is None or v <= hi))
    top1_n = max(1, -(-n // 100))  # ceil(n/100)
    top5_n = max(1, -(-n * 5 // 100))
    return {
        "creators": n,
        "coins": total,
        "buckets": buckets,
        "max_launches": values[0],
        "mean_launches": round(total / n, 3),
        "median_launches": statistics.median(values),
        "top1pct_creators": top1_n,
        "top1pct_share": round(sum(values[:top1_n]) / total, 4) if total else None,
        "top5pct_share": round(sum(values[:top5_n]) / total, 4) if total else None,
    }


#: Floor below which a coin never had enough liquidity for "collapse" to mean anything.
#: pump.fun coins start life around $3k of notional market cap with no real SOL behind
#: them, so anything under this never left the launch pad. INVENTED.
COLLAPSE_ATH_FLOOR_USD = Decimal("10000")
#: Fraction of the observed peak below which we would have called it a collapse. INVENTED.
COLLAPSE_RETAIN_FRAC = Decimal("0.2")


def collapse_diagnostic(
    records: Iterable[LaunchRecord],
    *,
    ath_floor_usd: Decimal = COLLAPSE_ATH_FLOOR_USD,
    retain_frac: Decimal = COLLAPSE_RETAIN_FRAC,
    reject_bar: int = 2,
) -> dict[str, Any]:
    """Measure the rug proxy we are choosing **not** to store, so the choice is checkable.

    The candidate definition is "reached ``ath_floor_usd`` of market cap and now holds less
    than ``retain_frac`` of that peak". It is reported, never written. The output field
    that matters is ``creators_over_reject_bar_pct``: the share of measured creators who
    would cross ``triage.TriageConfig.reject_min_creator_rugs`` and start being refused.
    EDGE §2 says 84% of *graduates* are down more than 70% within 20 minutes, so this
    proxy is measuring the asset class rather than the creator, and a high number here is
    the evidence for :data:`RUGGED_DECISION`.
    """
    per_creator: dict[str, int] = {}
    measurable = collapsed = reached = 0
    for rec in records:
        ath = rec.ath_market_cap_usd
        cur = rec.usd_market_cap
        if ath is None or cur is None:
            continue
        measurable += 1
        if ath < ath_floor_usd:
            continue
        reached += 1
        if cur < ath * retain_frac:
            collapsed += 1
            if rec.creator:
                per_creator[rec.creator] = per_creator.get(rec.creator, 0) + 1
    creators_seen = {r.creator for r in records if r.creator}
    over_bar = sum(1 for v in per_creator.values() if v >= reject_bar)
    return {
        "definition": (
            f"ath_market_cap >= ${ath_floor_usd} and current usd_market_cap < "
            f"{retain_frac} x ath"
        ),
        "coins_measurable": measurable,
        "coins_reached_floor": reached,
        "coins_collapsed": collapsed,
        "collapse_rate_of_reached": round(collapsed / reached, 4) if reached else None,
        "creators_measured": len(creators_seen),
        "creators_with_any": len(per_creator),
        "creators_over_reject_bar": over_bar,
        "creators_over_reject_bar_pct": (
            round(over_bar / len(creators_seen), 4) if creators_seen else None
        ),
        "stored": False,
        "why_not_stored": RUGGED_DECISION,
    }


# --------------------------------------------------------------------------------------
# the backfill
# --------------------------------------------------------------------------------------


@dataclass
class BackfillReport:
    """Everything one run learned, in the terms someone would ask the question."""

    chain: str = Chain.SOL.value
    pages_requested: int = 0
    index_pages_ok: int = 0
    index_pages_failed: int = 0
    index_coins: int = 0
    index_window_ms: int | None = None
    creators_discovered: int = 0
    creators_skipped_fresh: int = 0
    creators_fetched: int = 0
    creators_failed: int = 0
    creators_no_history: int = 0
    creators_written: int = 0
    creators_truncated: int = 0
    lifetime_coins: int = 0
    lifetime_graduated: int = 0
    creators_with_a_graduate: int = 0
    requests: int = 0
    elapsed_s: float = 0.0
    dry_run: bool = False
    distribution: dict[str, Any] = field(default_factory=dict)
    window_distribution: dict[str, Any] = field(default_factory=dict)
    collapse: dict[str, Any] = field(default_factory=dict)
    rugged_decision: str = RUGGED_DECISION

    @property
    def graduation_rate(self) -> float | None:
        return round(self.lifetime_graduated / self.lifetime_coins, 5) if self.lifetime_coins else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "pages_requested": self.pages_requested,
            "index_pages_ok": self.index_pages_ok,
            "index_pages_failed": self.index_pages_failed,
            "index_coins": self.index_coins,
            "index_window_ms": self.index_window_ms,
            "creators_discovered": self.creators_discovered,
            "creators_skipped_fresh": self.creators_skipped_fresh,
            "creators_fetched": self.creators_fetched,
            "creators_failed": self.creators_failed,
            "creators_no_history": self.creators_no_history,
            "creators_written": self.creators_written,
            "creators_truncated": self.creators_truncated,
            "lifetime_coins": self.lifetime_coins,
            "lifetime_graduated": self.lifetime_graduated,
            "graduation_rate": self.graduation_rate,
            "creators_with_a_graduate": self.creators_with_a_graduate,
            "requests": self.requests,
            "elapsed_s": round(self.elapsed_s, 1),
            "dry_run": self.dry_run,
            "distribution": self.distribution,
            "window_distribution": self.window_distribution,
            "collapse_diagnostic": self.collapse,
            "rugged_decision": self.rugged_decision,
        }


def backfill_creators(
    conn: sqlite3.Connection | None = None,
    *,
    chain: Chain = Chain.SOL,
    pages: int = 4,
    indexes: Sequence[str] = (INDEX_CREATED,),
    max_creators: int | None = None,
    config: BackfillConfig = DEFAULT_CONFIG,
    dry_run: bool = False,
) -> BackfillReport:
    """Discover active creators, measure each one's lifetime history, write the rows.

    Idempotent by construction: each creator's counts are recomputed from a complete
    ``?creator=`` fetch and written absolutely. Running this twice produces the same table.

    Only ``sol`` has a provider. Any other chain returns an empty report rather than
    pretending; there is no cross-chain launch index here to guess from.
    """
    started = time.monotonic()
    report = BackfillReport(chain=chain.value, pages_requested=max(0, int(pages)), dry_run=dry_run)
    if chain is not Chain.SOL:
        log.warning("creators: no launch-history provider for %s", chain.value)
        return report

    c = conn or get_conn()

    # ---- discovery -------------------------------------------------------------------
    index_records: dict[str, LaunchRecord] = {}
    for index in indexes or (INDEX_CREATED,):
        if index not in INDEXES:
            log.warning("creators: unknown index %r, skipping", index)
            continue
        recs, ok, failed = fetch_index(pages=pages, index=index, config=config, conn=c)
        report.index_pages_ok += ok
        report.index_pages_failed += failed
        report.requests += ok + failed
        for rec in recs:
            index_records.setdefault(rec.mint, rec)
    report.index_coins = len(index_records)
    stamps = [r.created_ms for r in index_records.values() if r.created_ms]
    if stamps:
        report.index_window_ms = max(stamps) - min(stamps)

    window_counts: dict[str, int] = {}
    for rec in index_records.values():
        if rec.creator:
            window_counts[rec.creator] = window_counts.get(rec.creator, 0) + 1
    report.creators_discovered = len(window_counts)
    report.window_distribution = launch_distribution(list(window_counts.values()))

    # Busiest first: if a run is cut short by ``max_creators``, the creators we measured
    # are the ones the most launches will hit.
    ordered = sorted(window_counts, key=lambda a: (-window_counts[a], a))
    fresh = _fresh_addresses(c, chain, config.refresh_after_s) if not dry_run else set()
    todo = [a for a in ordered if a not in fresh]
    report.creators_skipped_fresh = len(ordered) - len(todo)
    if max_creators is not None:
        todo = todo[: max(0, int(max_creators))]

    # ---- per-creator history ---------------------------------------------------------
    lifetime: list[LaunchRecord] = []
    counts: list[int] = []
    for address in todo:
        got = fetch_creator_history(address, chain=chain, config=config, conn=c)
        report.requests += got.pages
        history, records = got.history, list(got.records)
        if history is None:
            # Two different facts, kept apart: a provider that did not answer, and a
            # provider that answered with nothing. Both leave the row absent.
            if got.ok:
                report.creators_no_history += 1
            else:
                report.creators_failed += 1
            continue
        report.creators_fetched += 1
        report.creators_truncated += int(history.truncated)
        report.lifetime_coins += history.launches
        report.lifetime_graduated += history.graduated
        report.creators_with_a_graduate += int(history.graduated > 0)
        counts.append(history.launches)
        lifetime.extend(records)
        if not dry_run:
            upsert_creator(history, c)
            report.creators_written += 1

    report.distribution = launch_distribution(counts)
    report.collapse = collapse_diagnostic(lifetime)
    report.elapsed_s = time.monotonic() - started

    try:
        emit(
            EventKind.CREATORS_BACKFILL,
            {
                "chain": chain.value,
                "creators_written": report.creators_written,
                "lifetime_coins": report.lifetime_coins,
                "graduation_rate": report.graduation_rate,
                "dry_run": dry_run,
            },
            chain=chain.value,
            conn=c,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never fail a backfill
        log.debug("creators: could not emit summary (%s)", exc)
    return report


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


def creator_status(
    conn: sqlite3.Connection | None = None, *, chain: Chain = Chain.SOL
) -> dict[str, Any]:
    """What the ``creators`` table currently knows, and what triage can do with it.

    ``triage_promotable`` is the number that matters: triage scores a creator with at
    least one prior graduate at ``unknown_prior + w_creator_prior_graduate`` = 0.65,
    which clears ``promote_threshold`` = 0.55. Those creators — and only those — are the
    ones that can turn a ``defer`` into a ``promote``.
    """
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT launches, graduated, median_peak_mcap_usd, updated_ms FROM creators WHERE chain=?",
        (chain.value,),
    )
    launches = [int(r["launches"] or 0) for r in rows]
    graduated = [int(r["graduated"] or 0) for r in rows]
    peaks = [_decimal(r["median_peak_mcap_usd"]) for r in rows]
    peaks = [p for p in peaks if p is not None]
    updated = [int(r["updated_ms"] or 0) for r in rows]
    tokens = fetch_one(c, "SELECT COUNT(*) AS n FROM tokens WHERE chain=?", (chain.value,))
    total_launches = sum(launches)
    return {
        "chain": chain.value,
        "rows": len(rows),
        "launches_total": total_launches,
        "graduated_total": sum(graduated),
        "graduation_rate": round(sum(graduated) / total_launches, 5) if total_launches else None,
        "triage_promotable": sum(1 for g in graduated if g > 0),
        "serial_launchers_5plus": sum(1 for launch in launches if launch >= 5),
        "serial_launchers_60plus": sum(1 for launch in launches if launch >= 60),
        "median_peak_mcap_usd_rows": len(peaks),
        "median_of_median_peaks_usd": str(statistics.median(sorted(peaks))) if peaks else None,
        "rugged_rows": 0,
        "rugged_decision": RUGGED_DECISION,
        "oldest_update_ms": min(updated) if updated else None,
        "newest_update_ms": max(updated) if updated else None,
        "distribution": launch_distribution(launches),
        "tokens_table_rows": int(tokens["n"]) if tokens else 0,
    }
