"""The exit watchdog: the service that turns :mod:`kaiba.execution.protection` into sells.

``protection.evaluate`` is pure and already tested. This module is everything around it —
the loop, the persistence, the deduplication and the honesty about missing data.

Several things here are not obvious and every one of them exists because getting it wrong
loses money:

* **A blind watchdog is the worst failure mode.** There is no quote source in this tree
  yet (P1-3, dexscreener). Rather than invent a price, the loop treats a missing quote as
  :class:`~kaiba.core.schemas.EvidenceBasis.UNAVAILABLE`: it does not evaluate stops, it
  does not exit, and it says so on the bus every tick. A watchdog that reports healthy
  while it cannot see a price is a watchdog that lets a position go to zero quietly, so
  the blind count is in every heartbeat and each blind position gets a throttled
  ``level="error"`` event of its own.
* **Warning forever is not a response.** Repeating the same error every 60 s for eight
  hours is indistinguishable from a healthy service to anyone who is not reading the bus
  line by line, so blindness has a *budget*: ``protection.max_blind_s``. Past it the
  watchdog pages once per blind episode (``protection_blind_timeout``), the heartbeat
  turns from ``warn`` to ``error`` and carries ``blind_over_budget`` and
  ``longest_blind_s``, and a position holding real tokens also stops new entries via
  :meth:`kaiba.execution.risk.RiskGate.halt`. It does **not** force an exit; the reasons
  are in :meth:`Watchdog._blind_timeout` and the first one is that such an exit cannot
  physically be sent.
* **Exits are never gated.** The kill switch, ``reduce_only``, ``entries_paused`` and the
  daily loss stop block entries. :meth:`kaiba.execution.risk.RiskGate.check_exit` is the
  authority on that and is called before every exit; its findings are recorded on the
  event so an operator can see the brakes that were on when we sold anyway. The rule is
  not reimplemented here. (See the module note below: ``executor._check_mode`` does *not*
  honour this for live sells, and the watchdog reports that loudly rather than routing
  around it.)
* **An ambiguous send is never resubmitted.** ``OrderState.UNKNOWN`` latches the position
  until reconciliation resolves the order. A duplicate exit is not a smaller mistake than
  a missed one; it is a short position in a token you do not have.
* **One bad position must not stop the loop.** Every position is checked inside its own
  try/except and a failure is an event, not an outage.

* **This loop is not the only owner of the ladder.** When
  ``protection.use_provider_orders`` is *effectively* on,
  :mod:`kaiba.execution.standing` mirrors the price-triggered rungs onto GMGN so they
  survive this process dying. That creates a second party that can sell the same tokens,
  so every exit first calls :func:`kaiba.execution.standing.claim_for_exit` to take the
  overlapping rungs back. The claim **never refuses and never blocks**: a contested claim
  is an error event, not a reason to hold a position the ladder says to exit. When the
  feature is not effective — which is the shipped default — none of this runs, makes a
  database write or spawns a process.

State lives in ``watchdog_state`` (migration ``007_watchdog.sql``) and is mirrored into
the ``positions`` columns the protection ladder was designed around, so the trailing
ratchet survives a restart and the dashboard still sees a stop.

Run it with ``kaiba run protection`` (``kaiba.cli.main`` imports :func:`run_watchdog` from
``kaiba.execution.protection``, which re-exports it from here).
"""

from __future__ import annotations

import logging
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kaiba.core import events as ev
from kaiba.core.config import get_risk
from kaiba.core.db import ensure_db, fetch_all, fetch_one, get_conn, jdump, jload, upsert
from kaiba.core.schemas import (
    EVM_ZERO,
    NATIVE_DECIMALS,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    EvidenceBasis,
    LaneMode,
    OrderState,
    Position,
    Side,
    now_ms,
)
from kaiba.execution import executor
from kaiba.execution.paper import PaperBroker, open_positions
from kaiba.execution.protection import (
    ProtectionConfig,
    ProtectionKind,
    ProtectionState,
    evaluate,
    protection_config,
)
from kaiba.execution.risk import RiskGate

log = logging.getLogger(__name__)

#: Payload discriminator on every event this service writes. There is no PROTECTION_BLIND
#: event kind in ``kaiba.core.schemas`` and adding one is a core-owned change, so the
#: watchdog rides on SYSTEM and tags itself. Filter with
#: ``kaiba_events(kinds=["system"])`` and look for ``payload.service == "watchdog"``.
SERVICE = "watchdog"
#: ``payload.event`` of the SYSTEM event a paper exit writes when it skipped the latency
#: charge on a curve with no flow (:meth:`DefaultExitSubmitter._paper_no_flow`). A payload
#: discriminator, not an event kind: the kind is SYSTEM.
PAPER_EXIT_NO_FLOW_EVENT = "paper_exit_no_flow"

#: A quote from a NEW identity (another source, venue or pool than the last accepted mark)
#: priced at least this many times the last accepted mark -- or the entry, when nothing has
#: been accepted yet -- must be corroborated by another leaf before it decides a tick, as a
#: suspect stop already is (:meth:`Watchdog._validate_quote`). 2026-11-02: shadow sm-trenches
#: ``pos_d7dd91df`` (sol, opened 10-28 01:53 UTC) took tp1/tp2/tp3 off a DexScreener Meteora
#: DLMM pool chosen as "deepest" (liq_usd 352,214) at $0.85-0.99 while the token's other pool
#: and GMGN said $0.00066; GMGN's 1m kline high in that window was $0.000817. Booked +63,876%,
#: it alone moved the lane's 14-day mean from -17.2% to +191.6%. MEASURED on the box, all
#: 73,016 ``quote_source_changed`` events since 10-01: 12 jumped >= 10x (0.016%), 2 >= 100x
#: (both that position). The factor is INVENTED; the population it touches is measured.
SUSPECT_JUMP_X = Decimal(10)

#: How long a position may be blind before we repeat the warning. Loud, but not 5 s loud.
BLIND_WARN_INTERVAL_S = 60

#: How long one position may stay unpriceable before repeating the warning stops counting
#: as a response and the watchdog escalates. Read from ``protection.max_blind_s``; this is
#: the fallback so the failsafe works before the config key lands. ``<= 0`` disables it.
#:
#: The floor is MEASURED. Every blind episode that has ever recovered on the live box
#: recovered fast: ``protection_restored.blind_for_s`` was 4, 5, 5 and 17 s (n=4, read
#: 2026-09-21 from ~/kaiba/data/kaiba.db, ``price_source: curve``, ``poll_interval_s: 5``).
#: 300 s is 17x the longest transient we have ever observed and 60 consecutive failed
#: ticks, so provider noise cannot trip it.
#:
#: The value itself is INVENTED. Nothing here measures how long a token stays unpriceable
#: before it is *permanently* unpriceable, and nothing measures how far a pool drains
#: while we cannot see it, so the gap between "clear of the noise" (>17 s) and 300 s is a
#: judgement. It is deliberately at the impatient end of that gap: the action this budget
#: triggers is a page and an entry brake, never a sell, so a premature trip costs operator
#: attention and a manual resume, while a late one costs a position with no stop.
MAX_BLIND_S_DEFAULT = 300

#: Whether crossing :data:`MAX_BLIND_S_DEFAULT` on a position holding real tokens also
#: stops new entries. Read from ``protection.max_blind_halt_entries``. Default on, because
#: the one thing we know for certain when a held position cannot be priced is that the
#: protection layer does not cover it -- and opening more positions under a protection
#: layer we have just watched fail is how one blind position becomes five.
MAX_BLIND_HALT_ENTRIES_DEFAULT = True

#: Order states that mean "we may have an exit on the wire". ``planned`` is deliberately
#: absent: a planned order was never sent, so retrying it is correct.
UNRESOLVED_ORDER_STATES: frozenset[str] = frozenset(
    {
        OrderState.RESERVED.value,
        OrderState.SUBMITTING.value,
        OrderState.SUBMITTED.value,
        OrderState.PARTIAL.value,
        OrderState.UNKNOWN.value,
    }
)

#: Exit retry backoff after a *failed* (definitely not sent) exit, in milliseconds.
RETRY_BASE_MS = 5_000
RETRY_MAX_MS = 60_000

#: Consecutive failures after which the 60 s ceiling lifts.
#:
#: MEASURED 2026-09-23: a rugged robinhood token refused every sell from 11:20 onward. The
#: backoff reached RETRY_MAX_MS on the fifth attempt and then retried once a minute
#: forever -- 33 failed sells in 33 minutes, which were 33 of the 50 orders placed in that
#: hour. ONE unsellable position was 66% of the entire order pipeline, draining the same
#: limiter bucket the price reads use, and so was a direct cause of
#: `pons_factory_read_failed`, blind positions and `no_viable_band` refusals on every
#: chain. A stuck exit does not merely fail to sell; it takes protection down with it.
#:
#: Eight is roughly five minutes of fast retries, which covers a 429, a venue blip or a
#: dropped connection. `exit_attempts` resets to 0 on any accepted submit, so only
#: CONSECUTIVE failures on one position ever reach this.
RETRY_ESCALATE_AFTER = 8
#: The ceiling once escalation starts. Still a retry, not an abandonment: a token can
#: become sellable again when liquidity returns, and we want to be there when it does.
RETRY_CEILING_MS = 1_800_000


def _exit_backoff(attempts: int) -> int:
    """Milliseconds to wait before retrying a failed exit.

    Doubling, capped at :data:`RETRY_MAX_MS` for the first
    :data:`RETRY_ESCALATE_AFTER` attempts and at :data:`RETRY_CEILING_MS` after that.
    """
    n = max(0, int(attempts) - 1)
    raw = RETRY_BASE_MS * (2 ** min(n, 20))
    cap = RETRY_MAX_MS if int(attempts) <= RETRY_ESCALATE_AFTER else RETRY_CEILING_MS
    return int(min(cap, max(RETRY_BASE_MS, raw)))

#: kv key holding the id of the last event this service consumed.
CURSOR_KEY = "watchdog.event_cursor"

#: Events the drain reads. PROTECTION_TRIGGERED from the agent, SYSTEM close intents from
#: the dashboard, PROTECTION_SET from the agent.
DRAIN_KINDS: tuple[str, ...] = (
    EventKind.PROTECTION_TRIGGERED.value,
    EventKind.PROTECTION_SET.value,
    EventKind.SYSTEM.value,
)

DRAIN_BATCH = 500
DRAIN_MAX_BATCHES = 20


def _dec(value: Any) -> Decimal | None:
    """Parse money. Returns ``None`` rather than 0 for anything unreadable (CONTRACT 2)."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------------------
# price source
# --------------------------------------------------------------------------------------


class PriceQuote(BaseModel):
    """One observation of a token's price, and what it is worth as evidence.

    ``liquidity_usd`` and ``executable_quote_usd`` are optional because the rug monitor
    and the anti-wick rule are allowed to be unavailable — the stop is not. A source that
    can only answer the price still makes the watchdog useful; a source that answers
    ``None`` makes it blind, which is a state with its own handling and its own events.
    """

    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    executable_quote_usd: Decimal | None = None
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    observed_ms: int = Field(default_factory=now_ms)
    source: str = "none"
    note: str | None = None
    chain: Chain | None = None
    token: str | None = None
    pool_id: str | None = None
    venue: str | None = None
    liquidity_kind: str | None = None
    freshness_budget_s: float | None = None
    #: For a pool mark: the asset the pool prices the token in, as the provider reported
    #: it. ``None`` for curve, router and aggregator reads, which have no such leg.
    quote_token: str | None = None
    quote_symbol: str | None = None
    #: What this mark settles in -- see :func:`settlement_of`. Set by the producer when it
    #: knows; derived from ``source`` otherwise.
    settlement: str | None = None
    #: For a Pons curve mark: the curve's own per-leg take (``feeBps() + creatorTaxBps()``) read in
    #: the same batch as the reserves (``evm_price.read_pons``). ``None`` = not read. Only the paper
    #: exit reads it (``DefaultExitSubmitter._paper``); it decides nothing about a stop.
    curve_take_bps: int | None = None
    #: For a native-quoted Flap curve mark: the curve's own state from the SAME read
    #: (``evm_price.flap_curve_snapshot``), so a paper exit is sold on that curve at our quantity with
    #: our holding put back (``evm_price.flap_sell_quote_out``) at no extra call. Only the paper exit
    #: reads it; never serialised into an event or the quote memory (``exclude``), so a quote rebuilt
    #: from either has none and its exit is booked not-honest. 2026-10-23 (hA_). A native-quoted Pons
    #: curve mark carries ``evm_price.pons_curve_snapshot`` here the same way (hB_).
    venue_curve: dict[str, Any] | None = Field(default=None, exclude=True)

    @property
    def usable(self) -> bool:
        """A price we are willing to evaluate a stop against."""
        return self.invalid_reason is None

    @property
    def invalid_reason(self) -> str | None:
        from kaiba.providers.prices import DEFAULT_MAX_AGE_S

        if self.basis in {EvidenceBasis.UNAVAILABLE, EvidenceBasis.STALE}:
            return f"quote_basis:{self.basis.value}"
        if self.price_usd is None or not self.price_usd.is_finite() or self.price_usd <= 0:
            return "quote_price_invalid"
        for value in (self.liquidity_usd, self.executable_quote_usd):
            if value is not None and (not value.is_finite() or value < 0):
                return "quote_measurement_invalid"
        budget = DEFAULT_MAX_AGE_S if self.freshness_budget_s is None else self.freshness_budget_s
        if self.observed_ms <= 0 or self.observed_ms > now_ms():
            return "quote_observation_invalid"
        if not 0 <= now_ms() - self.observed_ms <= min(budget, DEFAULT_MAX_AGE_S) * 1000:
            return "quote_stale"
        return None

    @classmethod
    def unavailable(cls, note: str, source: str = "none") -> PriceQuote:
        return cls(basis=EvidenceBasis.UNAVAILABLE, note=note, source=source)


# --------------------------------------------------------------------------------------
# settlement: which marks may decide an exit
# --------------------------------------------------------------------------------------
#
# Journal #5048 (2026-10-02). An exit is a sell INTO the chain's native asset. A pool mark
# quoted in a third asset (Bonk, a tokenised stock, a stablecoin) is that pool's price times
# that asset's USD price, and nothing makes it the price a sell realises. MEASURED on the
# box, every watchdog exit with a recorded fill, |fill / trigger mark - 1| (the fill lands
# 3-120 s after the mark, so part of every gap is the market moving):
#
#     sol  DexScreener pool quoted in SOL        n=24  median 16.8%   >25%: 9
#     sol  DexScreener pool quoted in a 3rd asset n=11  median 32.9%   >25%: 6
#     sol  Jupiter executable quote              n=15  median  6.4%   >25%: 0
#
# COZY (ETXqxf, Bonk CPMM) is the case in the journal: all three of its exits filled 33-48%
# below the marks they fired on. The 80% moon-bag trim fired on a +23% mark and filled at
# -17% vs entry; `tp1_at_2.0x` fired on a +141% mark and filled at +48%. That first sell DID
# route through the same pool (COZY->Bonk on CPMM, Bonk->SOL on Orca, read from the
# transaction) and still realised 15.4 Bonk/COZY against a mark that implied 23.
#
# ROBINHOOD DOES NOT SHOW IT, which is why the rule is per chain and starts on sol only:
#
#     robinhood  pool quoted in ETH/WETH         n=25  median  9.7%   >25%: 5
#     robinhood  pool quoted in a tokenised stock n=25  median  5.1%   >25%: 2
#     robinhood  pool quoted in USDG             n=12  median 14.5%   >25%: 2
#
# and robinhood has no executable quote to fall back to: past DexScreener its only layer is
# GMGN, which in the four days to 2026-10-03 priced 777 live robinhood marks while 1,355
# `protection_blind` events (each up to 60 s of ticks) record it refusing as the last
# layer. 28% of robinhood live marks in those four days were foreign-pool marks, and a
# blind position past `max_blind_s` halts entries on every chain. Turn robinhood on with
# `protection.settlement_mark_chains` once that is measured, not before.

#: A pool quoted in the asset an exit sells into.
SETTLE_NATIVE_POOL = "native_pool"
#: A pool quoted in anything else. NOT decision-grade on an enforcing chain.
SETTLE_FOREIGN_POOL = "foreign_pool"
#: A launch curve read off its own reserves -- the venue the sell would go to.
SETTLE_CURVE = "curve"
#: A router quote for an actual swap into the native asset.
SETTLE_EXECUTABLE = "executable"
#: A provider's own price with no pool we can see (GMGN). Kept decision-grade: it is the
#: last layer that prices many positions, and vetoing it is a blindness decision this
#: change does not have the measurement to make.
SETTLE_AGGREGATOR = "aggregator"

#: What an exit receives, per chain: the native asset as DexScreener spells a pool's quote
#: side. MEASURED 2026-10-03 from DexScreener pair lists of our own positions' tokens:
#: sol pools quote ``So111...112``; robinhood "ETH" pools quote the zero address (Uniswap v4
#: native ETH) and "WETH" pools quote ``0x0bd7...ad73``; bsc pools quote WBNB ``0xbb4c...095c``.
#: EVM entries are lower case; comparison folds case.
NATIVE_SETTLEMENT_ASSETS: dict[Chain, frozenset[str]] = {
    Chain.SOL: frozenset({SOL_NATIVE_MINT}),
    Chain.ROBINHOOD: frozenset({EVM_ZERO, "0x0bd7d308f8e1639fab988df18a8011f41eacad73"}),
    Chain.BSC: frozenset({EVM_ZERO, "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"}),
}

#: Chains on which an exit may only be decided on a mark that settles in the native asset,
#: when ``protection.settlement_mark_chains`` is absent. See the measurement above.
SETTLEMENT_MARK_CHAINS_DEFAULT: frozenset[Chain] = frozenset({Chain.SOL})

_SOURCE_SETTLEMENT: tuple[tuple[str, str], ...] = (
    ("curve:", SETTLE_CURVE),
    ("evm-venue:", SETTLE_CURVE),
    ("jupiter:", SETTLE_EXECUTABLE),
    ("pancake-v2:", SETTLE_EXECUTABLE),
    ("gmgn:", SETTLE_AGGREGATOR),
)


def is_native_settlement_asset(chain: Chain | None, address: str | None) -> bool:
    if chain is None or not address:
        return False
    known = NATIVE_SETTLEMENT_ASSETS.get(chain, frozenset())
    text = address.strip()
    return text in known or text.lower() in known


def settlement_of(quote: PriceQuote | None) -> str | None:
    """What ``quote`` settles in. ``None`` when nothing says (an injected source)."""
    if quote is None:
        return None
    if quote.settlement:
        return quote.settlement
    for prefix, kind in _SOURCE_SETTLEMENT:
        if quote.source.startswith(prefix):
            return kind
    return None


def is_foreign_mark(quote: PriceQuote | None) -> bool:
    """A pool mark quoted in something other than what an exit sells into."""
    return settlement_of(quote) == SETTLE_FOREIGN_POOL


def configured_settlement_mark_chains(
    default: frozenset[Chain] = SETTLEMENT_MARK_CHAINS_DEFAULT,
) -> frozenset[Chain]:
    """``protection.settlement_mark_chains``: chain names, ``[]`` for none, absent = default.

    An unreadable value falls back to the default rather than to "none": a typo must not
    quietly put foreign-pool marks back in charge of a stop.
    """
    value = _protection_setting("settlement_mark_chains")
    if value is None:
        return frozenset(default)
    if isinstance(value, str):
        value = [part for part in value.replace(",", " ").split() if part]
    if not isinstance(value, (list, tuple, set, frozenset)):
        log.warning("protection.settlement_mark_chains=%r is not a list; using the default", value)
        return frozenset(default)
    chains: set[Chain] = set()
    for item in value:
        try:
            chains.add(Chain(str(item).strip().lower()))
        except ValueError:
            log.warning("protection.settlement_mark_chains: unknown chain %r; using the default", item)
            return frozenset(default)
    return frozenset(chains)


def corroboration_only(source: Any) -> bool:
    """A leaf that may CORROBORATE a stop (``Watchdog._validate_quote``) but never price a tick.

    2026-10-23 (step 3 review, blocking #3): the sampled Codex.io backup
    (``evm_pool_price.BackupPriceSource``, about one call an hour per process) sat in the bsc and
    robinhood routes AHEAD of the box's GMGN leaf. For a position GMGN normally prices, Codex
    answered one tick an hour and became the remembered quote identity, so GMGN's next stop was
    a "suspect stop" on a new identity; Codex then refused (token gap / hourly budget), GMGN was
    skipped as the rejected leaf, and the position went BLIND until Codex answered again -- a
    shadow one older than an hour was written off ``abandoned_unpriceable`` and dropped from the
    scoreboard. A leaf that answers one tick in hundreds must not decide or set identity; it is
    asked only for corroboration, after every ordinary leaf, and what it answers is never
    remembered (``Watchdog._check_position``).
    """
    return bool(getattr(source, "corroboration_only", False))


def source_leaves(root: Any, chain: Chain):
    """The leaf sources of a configured price chain, in the order ``chain`` would ask them."""
    seen: set[int] = set()

    def walk(source):
        if source is None or id(source) in seen:
            return
        seen.add(id(source))
        if isinstance(source, FallbackPriceSource):
            for child in source.sources:
                yield from walk(child)
        elif isinstance(source, ChainFirstPriceSource):
            yield from walk(source.first.get(chain))
            for child in source.rest:
                yield from walk(child)
        elif isinstance(getattr(source, "routes", None), dict):
            yield from walk(source.routes.get(chain) or getattr(source, "fallback", None))
        else:
            yield source
            yield from walk(getattr(source, "fallback", None))

    yield from walk(root)


def settle_foreign_mark(
    source: Any, chain: Chain, token: str, quote: PriceQuote, *, deadline: float | None = None,
) -> PriceQuote:
    """The first native-settled answer after the pool reader, or ``quote`` itself.

    The rule behind ``Watchdog._settle``, shared so the entry probe
    (``viability.quote_asset_is_protectable``) asks the same layers protection will: a token
    whose only mark is a foreign pool would be admitted and then sit blind. The layers after
    the pool reader are asked in order; a layer that would route back to the pool reader is
    skipped. ``deadline`` (``time.monotonic()``) stops asking once passed.
    """
    leaves = list(source_leaves(source, chain))
    start = next(
        (i + 1 for i, leaf in enumerate(leaves) if isinstance(leaf, ProviderPriceSource)), 0
    )
    for leaf in leaves[start:]:
        if isinstance(leaf, ProviderPriceSource) or getattr(leaf, "fallback", None) is not None:
            continue  # it would hand the question straight back to the pool reader
        if corroboration_only(leaf):
            continue  # a sampled backup never supplies the mark an exit is decided on
        if deadline is not None and time.monotonic() >= deadline:
            break
        try:
            got = leaf.quote(chain, token)
        except Exception as exc:  # noqa: BLE001 - a dead layer is a refusal, not a crash
            log.debug("settlement layer %r raised on %s: %s", leaf, token, exc)
            continue
        if not isinstance(got, PriceQuote) or not got.usable or is_foreign_mark(got):
            continue
        if (got.chain is not None and got.chain != chain) or (got.token is not None and got.token != token):
            continue
        return got
    return quote


@runtime_checkable
class PriceSource(Protocol):
    """One method, so P1-3 (dexscreener) can satisfy it without knowing this module.

    Implementations must never raise — return :meth:`PriceQuote.unavailable` instead — and
    must go through ``kaiba.core.limiter.guarded`` for any network call. The watchdog
    still wraps every call defensively, because "must never raise" is not a guarantee.
    """

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        """Current price for ``token`` on ``chain``, or an UNAVAILABLE quote."""


class NullPriceSource:
    """The shipped default: there is no quote source in this tree yet.

    It exists so the watchdog runs, and so the blind path is the *normal* path until a
    real source is injected — which means the blind path is exercised every day rather
    than being the branch nobody ever took.
    """

    name = "null"

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        return PriceQuote.unavailable("no price source configured (P1-3 dexscreener)", source=self.name)


class ProviderPriceSource:
    """Adapter onto ``kaiba.providers.prices`` — the P1-3 quote stack.

    Opt-in rather than default, and imported lazily, because the provider layer is landing
    in parallel: the watchdog must keep running (loudly blind) if it is absent or broken.
    Turn it on by adding ``price_source: dexscreener`` to the ``protection:`` block of
    ``config/risk.yaml``, or by passing an instance to :func:`run_watchdog`.
    """

    name = "prices"

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        try:
            from kaiba.providers import prices
        except Exception as exc:  # noqa: BLE001 - absent provider layer is blindness
            return PriceQuote.unavailable(f"price provider unimportable: {type(exc).__name__}", self.name)
        native = NATIVE_SETTLEMENT_ASSETS.get(chain) or None
        try:
            # EXIT is the highest limiter priority. A price read that decides whether to
            # close a position must outrank discovery work competing for the same budget,
            # or a busy scan can make the watchdog blind exactly when it matters.
            from kaiba.core.limiter import Priority

            try:
                # The pool quoted in what a sell receives, when one is deep enough to be a
                # market (`prices.pick_pair`); the deepest pool otherwise.
                found = prices.quote(chain, token, priority=Priority.EXIT, prefer_quotes=native)
            except TypeError:
                found = prices.quote(chain, token, priority=Priority.EXIT)
        except TypeError:
            found = prices.quote(chain, token)
        except Exception as exc:  # noqa: BLE001 - a provider outage is data, not a crash
            return PriceQuote.unavailable(f"price provider raised {type(exc).__name__}", self.name)
        price = _dec(getattr(found, "price_usd", None))
        basis = getattr(getattr(found, "receipt", None), "basis", EvidenceBasis.UNAVAILABLE)
        if price is None or not price.is_finite() or price <= 0:
            note = getattr(getattr(found, "receipt", None), "note", None)
            return PriceQuote.unavailable(str(note or "provider returned no price"), self.name)
        quote_token = getattr(found, "quote_address", None) or None
        quote_symbol = getattr(found, "quote_symbol", None) or None
        settlement: str | None = None
        if getattr(found, "pair_address", None) or getattr(found, "source", "") == "dexscreener":
            # A POOL mark. It settles natively only when the pool's other side is what an
            # exit receives -- or when the token IS that asset (pricing SOL itself off
            # SOL/USDC is the right pool, and `_native_usd` depends on it). A pool that
            # did not say what it is quoted in is not evidence that it is native.
            if is_native_settlement_asset(chain, token) or is_native_settlement_asset(chain, quote_token):
                settlement = SETTLE_NATIVE_POOL
            else:
                settlement = SETTLE_FOREIGN_POOL
        return PriceQuote(
            price_usd=price,
            liquidity_usd=_dec(getattr(found, "liquidity_usd", None)),
            basis=basis,
            source=f"{self.name}:{getattr(found, 'source', '') or 'unknown'}",
            observed_ms=getattr(getattr(found, "receipt", None), "observed_at_ms", 0),
            note=getattr(getattr(found, "receipt", None), "note", None),
            chain=getattr(found, "chain", chain),
            token=getattr(found, "token", token),
            pool_id=getattr(found, "pair_address", None),
            venue=getattr(found, "dex_id", None),
            liquidity_kind="dex_tvl_usd" if getattr(found, "pair_address", None) else None,
            quote_token=quote_token,
            quote_symbol=quote_symbol,
            settlement=settlement,
        )


class FallbackPriceSource:
    """Ask each source in order, take the first USABLE quote, and refuse if none is.

    This is the composition primitive the Solana branch was missing. Every source in this
    module already has one private fallback -- ``CurvePriceSource.fallback``,
    ``ChainRoutedPriceSource.fallback`` -- and one is not enough: on 2026-09-22 four of
    eight live positions were blind because the curve refused (correctly) *and* the DEX
    pair did not exist (correctly), and there was nowhere else to ask. See the header of
    ``tests/test_watchdog_router_fallback.py`` for the measurement.

    Three properties, and the last one is the point:

    1. **First usable wins.** A later source is never called once one has answered, so a
       last resort that costs a network call and a limiter reservation costs neither on the
       positions that were already priceable.
    2. **A layer that fails is not a layer that stops the chain.** A source that raises, or
       that returns something which is not a :class:`PriceQuote`, is recorded as a refusal
       and the next source is tried. "Implementations must never raise" is a promise this
       module already says it does not treat as a guarantee.
    3. **It never guesses.** When every source refuses, the result is
       :meth:`PriceQuote.unavailable` carrying every layer's refusal in order, which
       ``Watchdog._blind`` emits as the ``reason`` on ``protection_blind``. A wrong price on
       a stop sells at the wrong moment and calls it protection; being blind at least says
       so out loud and has a budget, a page and an entry halt attached to it.
    """

    #: Overridden per instance so the ``price_source`` field of ``protection_blind`` says
    #: which chain of sources was asked, not just "fallback".
    name = "fallback"

    def __init__(self, *sources: Any, name: str | None = None) -> None:
        #: ``None`` entries are dropped rather than rejected: a source whose module failed
        #: to import arrives as ``None`` from the factories below, and losing the whole
        #: chain to that would be the blindness this class exists to prevent.
        self.sources: tuple[Any, ...] = tuple(s for s in sources if s is not None)
        if name:
            self.name = name

    #: Per-layer cap on the refusal text. Capped per layer rather than only on the join,
    #: because one chatty provider must not truncate the layers after it: the LAST
    #: refusal is usually the one that says why nothing could answer.
    NOTE_CHARS_PER_SOURCE = 110

    @staticmethod
    def _label(source: Any) -> str:
        return str(getattr(source, "name", None) or type(source).__name__)

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        if not self.sources:
            return PriceQuote.unavailable("no price sources configured", self.name)
        notes: list[str] = []

        def note(text: str) -> None:
            notes.append(text[: self.NOTE_CHARS_PER_SOURCE])

        for source in self.sources:
            if corroboration_only(source):
                # Listed so ``source_leaves`` reaches it for ``_validate_quote``; never asked
                # here. See :func:`corroboration_only`.
                continue
            label = self._label(source)
            try:
                got = source.quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - a dead layer is data, not a crash
                log.warning("%s price source raised for %s: %s", label, token, exc)
                note(f"{label} raised {type(exc).__name__}")
                continue
            if not isinstance(got, PriceQuote):
                note(f"{label} returned {type(got).__name__}")
                continue
            if got.usable:
                return got
            note(f"{label}: {got.note or got.source or 'unavailable'}")
        return PriceQuote.unavailable("; ".join(notes), self.name)


def _curve_resolver(conn: sqlite3.Connection) -> Any:
    """Live curve state first, last snapshot second. ``None`` if the module is absent.

    An exit priced off a stale snapshot is priced off a curve that has moved, which is
    the difference between a modelled fill and a fiction. ``PaperBroker`` treats ``None``
    as "no curve resolver" and falls back to its own default.
    """
    try:
        from kaiba.execution import curve_price
    except Exception as exc:  # noqa: BLE001 - absent module means the default resolver
        log.warning("curve resolver unavailable: %s", exc)
        return None
    try:
        # Snapshot first, live second, deliberately against the wiring the authoring agent
        # asked for. `curve_price.live_resolver` says in its own docstring that network is
        # "never the default: a paper broker that reaches out on every fill would make the
        # shadow record depend on provider availability". The staleness that ordering was
        # meant to avoid is already handled inside `snapshot_resolver`, which *refuses* a
        # snapshot past its age limit rather than returning it -- so a fresh snapshot is
        # used, a stale one falls through to the network, and the offline path stays
        # deterministic.
        from kaiba.core.limiter import Priority

        return curve_price.chain_resolver(
            curve_price.snapshot_resolver(conn),
            curve_price.live_resolver(conn, priority=Priority.EXIT),
        )
    except Exception as exc:  # noqa: BLE001 - a resolver we cannot build is not a crash
        log.warning("could not build curve resolver: %s", exc)
        return None


def _curve_price_source() -> PriceSource:
    """Curve reserves pre-graduation, falling through to the pair after.

    A DEX pair does not exist while a token is on its bonding curve, so
    :class:`ProviderPriceSource` returns nothing for exactly the tokens we hold first --
    the watchdog ticked, reported healthy, and no stop could ever fire. Imported lazily
    because a missing module here must be blindness we can see, not an import crash.
    """
    try:
        from kaiba.execution.curve_price import curve_price_source
    except Exception as exc:  # noqa: BLE001 - an absent source is blindness, not a crash
        log.warning("curve price source unavailable: %s", exc)
        return NullPriceSource()
    from kaiba.core.limiter import Priority

    return curve_price_source(priority=Priority.EXIT)


def _jupiter_price_source() -> PriceSource:
    """Executable router quotes. Routes pump.fun curves directly, verified 2026-09-20.

    Costs a network call per tick against a sustained ceiling of roughly one request a
    second, so it is the cross-check rather than the default; ``curve`` is arithmetic on
    data we already hold.
    """
    try:
        from kaiba.providers.jupiter import JupiterPriceSource
    except Exception as exc:  # noqa: BLE001 - an absent provider is blindness, not a crash
        log.warning("jupiter price source unavailable: %s", exc)
        return NullPriceSource()
    return JupiterPriceSource()


#: ``protection.price_source`` in ``config/risk.yaml`` -> implementation.
def _evm_price_source() -> PriceSource:
    """EVM launch-venue reserves (Flap on BSC, Pons on Robinhood), DEX after graduation.

    Blind on Solana by construction; prefer ``venue``. Imported lazily because a missing
    module here must be blindness we can see, not an import crash.
    """
    try:
        from kaiba.execution.evm_price import evm_price_source
    except Exception as exc:  # noqa: BLE001 - an absent source is blindness, not a crash
        log.warning("evm price source unavailable: %s", exc)
        return NullPriceSource()
    return evm_price_source()


def _gmgn_token_info(address: str, chain: Chain = Chain.SOL, **kw):
    """The provider call, as its own seam so a test can drive it without a network."""
    from kaiba.providers.gmgn_cli import token_info

    return token_info(address, chain, **kw)


class GmgnPriceSource:
    """GMGN's own mid-price. The last thing asked before a position is called blind.

    MEASURED 2026-09-23: a live robinhood position (CASHVLAD, pons_v2 at 10.7% of its
    curve) could not be priced by the venue reader (a launchpad revision we do not
    decode), by DexScreener (still on its curve, so no pair to index) or by Jupiter
    (Solana). Both refusals were correct. It stayed blind for 585 s against a 300 s budget,
    tripped ``protection_blind_timeout`` -- which halts entries on EVERY chain -- and could
    not even be exited, because ``_min_out`` needs a price. Blind, unsellable and blocking
    all trading, for half an hour, on a $17 position.

    GMGN had the price the whole time: ``token info`` carries ``price.price`` beside the
    liquidity and supply we already read from that same call.

    **No liquidity is offered, on purpose.** This is a price, not a depth. The paper broker
    and the anti-wick check both need real depth and must keep refusing rather than sizing
    against a number this cannot supply.

    **Position in the chain matters.** :class:`FallbackPriceSource` stops at the first
    usable quote, so this layer spends no request and no limiter credit on any position the
    venue or the pair already prices. It is reached only when the alternative is blindness.
    """

    name = "gmgn"

    #: Cache budget for THIS read, strictly under every poll interval protection runs (5 s
    #: default, 12 s on the box), so each tick that reaches this layer gets a fresh number.
    #:
    #: MEASURED 2026-10-01: in 1,695 of 1,967 robinhood `protection_blind` events (86%) this
    #: layer had a price and served it STALE. `token.info` caches 15 s with a 120 s stale
    #: grace, and inside the grace `run_read` returns the stale copy WITHOUT trying the
    #: network -- the cache file for 0xc777 aged 26 s -> 95 s across six ticks with no
    #: refresh. A stale quote is unusable, so the position was blind for up to 120 s of every
    #: 135, on the one layer that could price it (DexScreener has no pair for it).
    EXIT_TTL_S = 4.0

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        from kaiba.core.limiter import Priority  # noqa: PLC0415 - module-local, like the rest

        try:
            # EXIT, not the helper's RESEARCH default: this read decides a stop. And no
            # stale grace: a stale answer here is blindness, so ask the network instead.
            got = _gmgn_token_info(
                token, chain, priority=Priority.EXIT, ttl_s=self.EXIT_TTL_S, stale_grace_s=0.0
            )
        except Exception as exc:  # noqa: BLE001 - a dead provider is blindness, not a crash
            return PriceQuote.unavailable(f"gmgn:{type(exc).__name__}")
        body = getattr(got, "data", None)
        if not isinstance(body, dict):
            return PriceQuote.unavailable("gmgn:no_payload")
        block = body.get("price")
        raw = block.get("price") if isinstance(block, dict) else block
        try:
            price = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return PriceQuote.unavailable("gmgn:unparseable_price")
        if not price.is_finite() or price <= 0:
            return PriceQuote.unavailable("gmgn:non_positive_price")
        from kaiba.providers.gmgn_cli import _TTL

        receipt = getattr(got, "receipt", None)
        return PriceQuote(
            price_usd=price,
            liquidity_usd=None,
            basis=getattr(receipt, "basis", EvidenceBasis.UNAVAILABLE),
            observed_ms=getattr(receipt, "observed_at_ms", 0),
            note=getattr(receipt, "note", None),
            freshness_budget_s=_TTL["token.info"][0],
            source="gmgn:token.info",
            chain=chain,
            token=token,
        )


class ChainFirstPriceSource:
    """A fallback chain whose FIRST source depends on the chain.

    Exists for one measured reason (see :func:`_venue_price_source`): on robinhood the most
    accurate reader is also the slowest-rationed one, and asking it first set the protection
    tick and halted the whole book. Every source stays in the chain for every chain; only
    the order changes, and only where a provider budget makes the order matter.

    ``routes`` is delegated so anything that introspects the underlying chain-routed source
    keeps working -- a wrapper that hides an attribute breaks callers silently.
    """

    def __init__(
        self,
        *,
        first: dict[Chain, Any],
        default: Any,
        rest: tuple[Any, ...],
        name: str = "chain-first",
    ) -> None:
        self.first = dict(first)
        self.default = default
        self.rest = tuple(rest)
        self.name = name

    def __getattr__(self, item: str) -> Any:
        # Only reached for attributes this object does not define, so it cannot shadow
        # anything above. `routes` is the one that matters in practice.
        return getattr(self.__dict__["default"], item)

    def quote(self, chain: Chain, token: str) -> PriceQuote:
        ordered = []
        head = self.first.get(chain)
        if head is not None:
            ordered.append(head)
        for source in self.rest:
            if source is not head:
                ordered.append(source)
        for source in ordered:
            try:
                got = source.quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - a dead source is not a dead chain
                log.debug("price source %r raised on %s: %s", source, chain.value, exc)
                continue
            if got is not None and got.usable:
                return got
        return PriceQuote.unavailable(f"no source priced {token[:12]} on {chain.value}")


def _venue_price_source() -> PriceSource:
    """Curve reserves on every venue we can read, DEX after graduation, all chains.

    A strict superset of ``curve``: Solana is routed to the same
    ``curve_price.curve_price_source()`` object it uses today, and BSC and Robinhood stop
    being blind. Measured 2026-09-21: DexScreener priced 0 of 18 live ``robinhood``
    tokens; this priced 18 of 18 from the Pons curve.

    REGISTERED 2026-09-21 after an incident: config was switched to ``venue`` before this
    entry existed, ``resolve_price_source`` fell through to ``NullPriceSource``, and the
    watchdog ran BLIND on every chain on a live-armed box for several minutes. Nothing
    filled in the window. ``tests/test_watchdog_sources.py`` now pins that every name the
    shipped config could carry resolves to a real source.
    """
    try:
        from kaiba.execution.evm_price import venue_price_source
    except Exception as exc:  # noqa: BLE001 - an absent source is blindness, not a crash
        log.warning("venue price source unavailable: %s", exc)
        return GmgnPriceSource()
    # GMGN last on sol and bsc. See `GmgnPriceSource`: it is reached only when the venue
    # reader and the pair have both refused, which on 2026-09-23 was the difference between
    # a stop that could be evaluated and a halt on every chain.
    #
    # ROBINHOOD IS THE EXCEPTION, and it is a throughput decision rather than an accuracy
    # one. MEASURED 2026-09-23: the Pons curve read is the better NUMBER -- it reads the
    # curve's own reserves -- but it goes through `robinhood-rpc`, a public endpoint
    # configured `max_inflight: 1`, `min_interval_ms: 1500`, `refill_per_s: 0.6`, and those
    # limits are not timidity: that host returned 9 rate-limit events in 24 h and has
    # family-banned us before.
    #
    # The arithmetic is unforgiving. Every open robinhood position needs a price every
    # tick, and at 0.6 calls a second ten positions take 16.7 s to sweep. The calls were
    # not failing -- 6,481 of them returned ok at a p50 of 351 ms -- they were simply
    # RATIONED. That set the protection tick, the tick exceeded its budget, and
    # `protection_overrun` halted entries on every chain. The book could not trade because
    # the book was large, and it got worse with every position we opened.
    #
    # GMGN prices the same tokens on a healthy bucket (credit +52,476 against
    # robinhood-rpc's -19,753) at `max_inflight: 4` and `min_interval_ms: 250`. The curve
    # reader stays in the chain, one place lower, so a token GMGN cannot price still gets
    # the better number. What changes is only which one we ask FIRST, on the one chain
    # whose venue reader cannot keep up with its own book.
    routed = venue_price_source()
    gmgn = GmgnPriceSource()
    # REVERTED 2026-09-23, same day it shipped. Putting gmgn first on robinhood did what it
    # was meant to -- robinhood-rpc recovered from credit_milli -19,753 to +2,000 -- but it
    # also made the thing protection exists for WORSE: robinhood `protection_blind` events
    # went from 40.3/h to 59.0/h across the same open book. The operator agent paused
    # entries over it within three minutes, correctly.
    #
    # The likely mechanism is that the curve reader's 1-in-flight path WAITS for its slot
    # while a refused gmgn call simply raises and falls through, so a prefetch burst that
    # used to serialise now partly misses. That is a hypothesis, not a measurement, which
    # is exactly why this is a revert and not a tweak: a protection gap is not the place to
    # test a theory.
    #
    # The tick cost this was meant to fix is being carried by `poll_interval_s: 12` in the
    # meantime, which is measured and holding at 0 ticks over budget. See
    # `ChainFirstPriceSource` for what to try next: make the fallback WAIT for a limiter
    # slot rather than treat a refusal as an inability to price.
    # 2026-10-19: the token's own PumpSwap pool FIRST (kaiba/execution/pumpswap_price.py). For a
    # graduated pump.fun token the routed sol source fell back to DexScreener, which lags the pool
    # ~45 s and served the CURVE pair for ~30 s after migration; stops fired late and paper marks
    # were wrong. The pool source answers only sol PumpSwap pools (vault + virtual reserves,
    # validated within 0-1.3% of executed trades); the curve source answers only sol pump.fun
    # bonding curves that have not completed (virtual SOL / virtual tokens; matched the last
    # trade at +0.0% and -1.9% on the box). Both refuse everything else at once, so robinhood,
    # bsc and every other sol venue are priced exactly as before.
    try:
        from kaiba.core.limiter import Priority
        from kaiba.execution.pumpswap_price import PumpCurveSource, PumpSwapPoolSource

        pool = PumpSwapPoolSource(priority=Priority.EXIT)
        curve = PumpCurveSource(priority=Priority.EXIT)
    except Exception as exc:  # noqa: BLE001 - an absent source must not blind the chain
        log.warning("pumpswap pool source unavailable: %s", exc)
        pool = curve = None
    return FallbackPriceSource(pool, curve, routed, gmgn, name="pumpswap+curve+venue+gmgn")


PRICE_SOURCES: dict[str, Callable[[], Any]] = {
    "none": NullPriceSource,
    "null": NullPriceSource,
    "off": NullPriceSource,
    "prices": ProviderPriceSource,
    "provider": ProviderPriceSource,
    "dexscreener": ProviderPriceSource,
    "curve": _curve_price_source,
    "jupiter": _jupiter_price_source,
    "evm": _evm_price_source,
    "venue": _venue_price_source,
}


def resolve_price_source(name: str | None) -> PriceSource:
    """Map a configured name to a source. Anything unrecognised is blindness, not a guess."""
    key = (name or "none").strip().lower()
    factory = PRICE_SOURCES.get(key)
    if factory is None:
        log.warning("unknown protection.price_source %r; running blind", name)
        return NullPriceSource()
    return factory()


def prefetch_source_factory_for(name: str | None) -> Callable[[], PriceSource]:
    """A zero-argument builder the quote prefetch calls once per worker thread.

    Several of the sources in :data:`PRICE_SOURCES` capture ``db.get_conn()`` when they
    are built -- ``curve``, ``evm`` and ``venue`` all keep it as ``self.conn`` -- so
    *where* they are built decides which connection they use. Built here, on the calling
    thread, and the whole pool shares one; built inside the worker, each worker gets the
    thread-local connection that is already private to it.
    """

    def build() -> PriceSource:
        return resolve_price_source(name)

    return build


def configured_price_source_name() -> str | None:
    """``protection.price_source`` from the risk file, if the operator set one."""
    try:
        value = (get_risk().protection or {}).get("price_source")
    except Exception as exc:  # noqa: BLE001 - an unreadable risk file must not stop the service
        log.warning("could not read protection.price_source: %s", exc)
        return None
    return str(value) if value else None


def _protection_setting(key: str) -> Any:
    """One key out of the raw ``protection:`` block, or ``None`` if it cannot be read.

    Raw, not via :class:`~kaiba.execution.protection.ProtectionConfig`: that model is
    owned by ``protection.py`` and does not carry these keys, and pydantic drops unknown
    ones. An unreadable risk file is missing data, not an outage — the caller supplies the
    failsafe default so protection keeps working while the config is being edited.
    """
    try:
        return (get_risk().protection or {}).get(key)
    except Exception as exc:  # noqa: BLE001 - an unreadable risk file must not stop the service
        log.warning("could not read protection.%s: %s", key, exc)
        return None


def configured_max_blind_s(default: int = MAX_BLIND_S_DEFAULT) -> int:
    """``protection.max_blind_s``, or :data:`MAX_BLIND_S_DEFAULT` when it is absent.

    Anything unparseable falls back to the default rather than to "disabled": a typo in
    the risk file must not quietly switch a failsafe off. ``<= 0`` *is* a valid answer and
    does switch it off, because that has to be sayable on purpose.
    """
    value = _protection_setting("max_blind_s")
    if value is None:
        return int(default)
    if isinstance(value, bool):  # `max_blind_s: true` is not a duration
        log.warning("protection.max_blind_s is a boolean; using the default %ss", default)
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("protection.max_blind_s=%r is not a number; using the default %ss", value, default)
        return int(default)


def configured_max_blind_halt_entries(default: bool = MAX_BLIND_HALT_ENTRIES_DEFAULT) -> bool:
    """``protection.max_blind_halt_entries``, or the failsafe default when it is absent."""
    value = _protection_setting("max_blind_halt_entries")
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    log.warning(
        "protection.max_blind_halt_entries=%r is not a boolean; using the default %s",
        value,
        default,
    )
    return bool(default)


# --------------------------------------------------------------------------------------
# persisted state
# --------------------------------------------------------------------------------------


#: Distinguishes "cached as absent" from "not cached" in ``Watchdog._migrated_ms``.
_MISSING = object()


#: kv key prefix for "this token's curve last moved at <ms>", written by the tweet launcher
#: for tokens it created (they never appear in ``swaps``). Read by :meth:`Watchdog._last_trade_ms`.
TOKEN_ACTIVITY_PREFIX = "token_activity:"
#: kv key prefix for a sol token's reserve clock ({reserves, moved_ms}), written by
#: :meth:`Watchdog._reserves_moved_ms` only when it changes, so a restart keeps the silence.
RESERVE_CLOCK_PREFIX = "reserve_clock:"


def _reserve_clock(chain: Chain, token: str) -> tuple[tuple[int, ...], int, int] | None:
    """``(reserves or pool state last read, this process's first read, last change)`` for a chain
    whose price reader keeps one, else None: sol (``pumpswap_price.reserve_clock``, the pool and
    curve readers) and the pool-only chains eth / base (``evm_pool_price.pool_clock``, 2026-10-23)."""
    if chain is Chain.SOL:
        from kaiba.execution.pumpswap_price import reserve_clock

        return reserve_clock(token)
    from kaiba.execution.evm_pool_price import POOL_ONLY_CHAINS, pool_clock

    if chain in POOL_ONLY_CHAINS:
        return pool_clock(chain, token)
    return None


_LANE_BLOCKS: tuple[float, dict[str, Any]] = (0.0, {})
_LANE_BLOCKS_TTL_S = 5.0


def _lane_blocks() -> dict[str, Any]:
    """``protection.lanes`` from risk.yaml, re-read at most every 5 s.

    ``get_risk()`` parses the YAML on every call and this is consulted up to three times per
    position per tick; protection tick time is the budget that halts entries when it overruns.
    """
    global _LANE_BLOCKS
    at, blocks = _LANE_BLOCKS
    if time.monotonic() - at > _LANE_BLOCKS_TTL_S:
        blocks = dict((get_risk().protection or {}).get("lanes") or {})
        _LANE_BLOCKS = (time.monotonic(), blocks)
    return blocks


def _lane_cfg(cfg: ProtectionConfig, lane: str) -> ProtectionConfig:
    """``cfg`` with ``protection.lanes.<lane>`` from risk.yaml applied, or ``cfg`` unchanged."""
    try:
        block = _lane_blocks().get(lane)
        if not block:
            return cfg
        return ProtectionConfig.model_validate({**cfg.model_dump(), **dict(block)})
    except Exception as exc:  # noqa: BLE001 - a bad override must never blind the ladder
        log.warning("protection.lanes.%s ignored: %s", lane, exc)
        return cfg


class WatchdogState(BaseModel):
    """One position's memory, persisted in ``watchdog_state``.

    Wraps :class:`~kaiba.execution.protection.ProtectionState` rather than subclassing it:
    the protection model is the pure decision input and must stay that way, while this
    carries the service-level facts (what is on the wire, what we have not priced yet).
    """

    position_id: str
    entry_price_usd: Decimal | None = None
    peak_price_usd: Decimal | None = None
    stop_price_usd: Decimal | None = None
    tp_done: list[str] = Field(default_factory=list)
    activated_trail_bps: int | None = None
    prev_liquidity_usd: Decimal | None = None
    stop_loss_bps: int | None = None
    trail_bps: int | None = None
    exit_order_id: str | None = None
    exit_state: str | None = None
    exit_pct: Decimal | None = None
    exit_reason: str | None = None
    exit_attempts: int = 0
    exit_retry_after_ms: int | None = None
    exit_final: bool = False
    pending_pct: Decimal | None = None
    pending_reason: str | None = None
    pending_source: str | None = None
    pending_ms: int | None = None
    blind_since_ms: int | None = None
    last_blind_warn_ms: int | None = None
    updated_ms: int = 0
    # Per-decision evidence, not a schema column. Durable quote baselines live in kv.
    quote_evidence: dict[str, Any] = Field(default_factory=dict, exclude=True)

    def to_protection(self) -> ProtectionState:
        return ProtectionState(
            position_id=self.position_id,
            entry_price=self.entry_price_usd or Decimal(0),
            peak_price=self.peak_price_usd or Decimal(0),
            stop_price=self.stop_price_usd,
            tp_done=list(self.tp_done),
            activated_trail_bps=self.activated_trail_bps,
        )

    def absorb(self, state: ProtectionState) -> None:
        """Take back what ``evaluate`` advanced in place."""
        self.peak_price_usd = state.peak_price
        self.stop_price_usd = state.stop_price
        self.tp_done = list(state.tp_done)
        self.activated_trail_bps = state.activated_trail_bps

    def clear_pending(self) -> None:
        self.pending_pct = None
        self.pending_reason = None
        self.pending_source = None
        self.pending_ms = None


def load_state(conn: sqlite3.Connection, position: Position) -> WatchdogState:
    """Rehydrate, preferring our own row and falling back to the ``positions`` columns.

    The fallback matters on the first tick after a paper fill: the broker wrote the entry
    and peak, and re-arming a hard stop from scratch there would throw away a ratchet the
    position already earned.
    """
    row = fetch_one(conn, "SELECT * FROM watchdog_state WHERE position_id=?", (position.position_id,))
    if row is None:
        return WatchdogState(
            position_id=position.position_id,
            entry_price_usd=position.entry_price_usd,
            peak_price_usd=position.peak_price_usd or position.entry_price_usd,
            stop_price_usd=position.stop_price_usd,
            tp_done=list(position.tp_done or []),
        )
    state = WatchdogState(
        position_id=row["position_id"],
        entry_price_usd=_dec(row["entry_price_usd"]) or position.entry_price_usd,
        peak_price_usd=_dec(row["peak_price_usd"]),
        stop_price_usd=_dec(row["stop_price_usd"]),
        tp_done=list(jload(row["tp_done_json"], []) or []),
        activated_trail_bps=row["activated_trail_bps"],
        prev_liquidity_usd=_dec(row["prev_liquidity_usd"]),
        stop_loss_bps=row["stop_loss_bps"],
        trail_bps=row["trail_bps"],
        exit_order_id=row["exit_order_id"],
        exit_state=row["exit_state"],
        exit_pct=_dec(row["exit_pct"]),
        exit_reason=row["exit_reason"],
        exit_attempts=int(row["exit_attempts"] or 0),
        exit_retry_after_ms=row["exit_retry_after_ms"],
        exit_final=bool(row["exit_final"]),
        pending_pct=_dec(row["pending_pct"]),
        pending_reason=row["pending_reason"],
        pending_source=row["pending_source"],
        pending_ms=row["pending_ms"],
        blind_since_ms=row["blind_since_ms"],
        last_blind_warn_ms=row["last_blind_warn_ms"],
        updated_ms=int(row["updated_ms"] or 0),
    )
    # A stop the paper broker or an earlier process raised is still a stop.
    if state.peak_price_usd is None:
        state.peak_price_usd = position.peak_price_usd or state.entry_price_usd
    return state


def save_state(conn: sqlite3.Connection, state: WatchdogState, *, mirror: bool = True) -> None:
    """Persist the row, and mirror the ladder into ``positions`` for everyone else."""
    state.updated_ms = now_ms()
    upsert(
        conn,
        "watchdog_state",
        {
            "position_id": state.position_id,
            "entry_price_usd": _s(state.entry_price_usd),
            "peak_price_usd": _s(state.peak_price_usd),
            "stop_price_usd": _s(state.stop_price_usd),
            "tp_done_json": jdump(state.tp_done),
            "activated_trail_bps": state.activated_trail_bps,
            "prev_liquidity_usd": _s(state.prev_liquidity_usd),
            "stop_loss_bps": state.stop_loss_bps,
            "trail_bps": state.trail_bps,
            "exit_order_id": state.exit_order_id,
            "exit_state": state.exit_state,
            "exit_pct": _s(state.exit_pct),
            "exit_reason": state.exit_reason,
            "exit_attempts": int(state.exit_attempts),
            "exit_retry_after_ms": state.exit_retry_after_ms,
            "exit_final": 1 if state.exit_final else 0,
            "pending_pct": _s(state.pending_pct),
            "pending_reason": state.pending_reason,
            "pending_source": state.pending_source,
            "pending_ms": state.pending_ms,
            "blind_since_ms": state.blind_since_ms,
            "last_blind_warn_ms": state.last_blind_warn_ms,
            "updated_ms": state.updated_ms,
        },
        ["position_id"],
    )
    if not mirror:
        return
    try:
        conn.execute(
            "UPDATE positions SET peak_price_usd=COALESCE(?, peak_price_usd), "
            "stop_price_usd=COALESCE(?, stop_price_usd), tp_done_json=?, protected=? "
            "WHERE position_id=?",
            (
                _s(state.peak_price_usd),
                _s(state.stop_price_usd),
                jdump(state.tp_done),
                1 if state.stop_price_usd is not None else 0,
                state.position_id,
            ),
        )
    except sqlite3.Error as exc:  # bookkeeping must not break the loop
        log.warning("watchdog could not mirror state to positions for %s: %s", state.position_id, exc)


# --------------------------------------------------------------------------------------
# exit requests drained from the bus
# --------------------------------------------------------------------------------------


@dataclass
class ExitRequest:
    """Somebody asked for this position to be closed. Coalesced per position per tick."""

    position_id: str
    pct: Decimal
    reason: str
    sources: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)

    def merge(self, other: ExitRequest) -> None:
        # Two sources asking for different sizes is one exit at the larger size, not two.
        self.pct = max(self.pct, other.pct)
        self.sources.extend(s for s in other.sources if s not in self.sources)
        self.event_ids.extend(other.event_ids)
        if other.reason and other.reason not in self.reason:
            self.reason = f"{self.reason}; {other.reason}"[:200]

    @property
    def source_label(self) -> str:
        return "+".join(self.sources) or "unknown"


@dataclass
class ProtectionUpdate:
    """A ``kaiba_set_protection`` request waiting to be folded into a position's state."""

    position_id: str
    stop_loss_bps: int | None = None
    trail_bps: int | None = None
    event_ids: list[int] = field(default_factory=list)


def read_cursor(conn: sqlite3.Connection) -> int:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (CURSOR_KEY,))
    if not row:
        return 0
    data = jload(row["value"], {}) or {}
    try:
        return int(data.get("after_id", 0))
    except (TypeError, ValueError):
        return 0


def write_cursor(conn: sqlite3.Connection, after_id: int) -> None:
    upsert(
        conn,
        "kv",
        {"key": CURSOR_KEY, "value": jdump({"after_id": int(after_id)}), "updated_ms": now_ms()},
        ["key"],
    )


def _pct(value: Any, default: Decimal = Decimal(100)) -> Decimal:
    parsed = _dec(value)
    if parsed is None or parsed <= 0:
        return default
    return min(parsed, Decimal(100))


# --------------------------------------------------------------------------------------
# exit submission
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitOutcome:
    """What came back from trying to sell. ``state`` is the order's state, not a verdict."""

    ok: bool
    state: OrderState | None
    order_id: str | None
    detail: str = ""
    #: The wallet reported NONE of this token while the ledger still shows a balance.
    #: Structural rather than prose so the tick handler can act on it without matching a
    #: sentence. Only ever set when the balance read SUCCEEDED and returned zero -- a read
    #: we could not make leaves this false, because "we could not look" and "there is
    #: nothing there" must never collapse into the same answer.
    wallet_empty: bool = False
    #: Milliseconds the submitter spent in each step it took (wallet read, native price for
    #: ``min_out``, the executor call). Exit timing only; empty for a submitter that does
    #: not measure itself.
    timing: dict[str, int] = field(default_factory=dict, compare=False)

    @property
    def ambiguous(self) -> bool:
        return self.state is OrderState.UNKNOWN


class ExitSubmitter(Protocol):
    def submit_exit(
        self, position: Position, pct: Decimal, *, quote: PriceQuote, reason: str
    ) -> ExitOutcome:
        """Sell ``pct`` percent of ``position``."""


def token_decimals(conn: sqlite3.Connection, chain: Chain, token: str) -> int | None:
    """Decimals are a fact, not an estimate. ``None`` means we must not size a live sell.

    ``kaiba.execution.paper`` guesses 6/18 when the token is unknown, which is fine for a
    simulation and catastrophic for ``min_out`` on a real order — a wrong guess is a
    factor of 10^12.
    """
    row = fetch_one(conn, "SELECT decimals FROM tokens WHERE chain=? AND address=?", (chain.value, token))
    if row and row["decimals"] is not None:
        return int(row["decimals"])
    return None


def _paper_qty_decimals(conn: sqlite3.Connection, position: Position) -> int | None:
    """The decimals a SHADOW position's ``qty`` is denominated in. ``None`` for anything else.

    Not a guess about the token: a paper position's quantity was minted by
    ``PaperBroker.buy`` in ``paper._token_decimals`` units (the recorded decimals, else that
    module's 6/18 convention), and ``PaperBroker.sell`` converts it back with the same
    function. Sizing the position with any other number would disagree with the broker that
    fills it. A live position never comes here: its decimals are a fact or the sell refuses.
    Imported lazily so a rename in ``paper`` degrades to "unknown", never to a dead watchdog.
    """
    if position.mode is not LaneMode.SHADOW:
        return None
    try:
        from kaiba.execution.paper import _token_decimals  # noqa: PLC0415

        return int(_token_decimals(position.chain, position.token, conn, None))
    except Exception:  # noqa: BLE001 - unknown units are "cannot size", never a crash
        log.debug("paper decimals unavailable for %s", position.token, exc_info=True)
        return None


def _native_mint(chain: Chain) -> str:
    return SOL_NATIVE_MINT if chain is Chain.SOL else EVM_ZERO


def exit_wallet_for(chain: Chain) -> str | None:
    """The wallet an exit on this chain sells from, per ``config/risk.yaml``."""
    try:
        return get_risk().chain_budget(chain).wallet or None
    except Exception:  # noqa: BLE001 - an unreadable config must not stop an exit
        log.exception("could not read the exit wallet for %s", chain.value)
        return None


def _token_balance_payload(chain: Chain, wallet: str, token: str) -> Any:
    """Raw ``gmgn-cli portfolio token-balance`` payload. Seam for tests."""
    from kaiba.core.limiter import Priority  # noqa: PLC0415 - module-local, like the rest
    from kaiba.providers.gmgn_cli import _read  # noqa: PLC0415 - avoid an import cycle

    got = _read(
        "portfolio.token_balance",
        ["portfolio", "token-balance", "--wallet", wallet, "--token", token],
        chain,
        Priority.EXIT,
        {},
    )
    # `GmgnResult.data` is None on any refusal or provider error; returning the RESULT
    # object in that case would read as an unparseable payload rather than "unknown", and
    # both end at None -- but only this way does the reason survive into the log.
    data = getattr(got, "data", None)
    if data is None:
        note = getattr(getattr(got, "receipt", None), "note", None)
        if note:
            log.warning("token balance unavailable: %s", note)
    return data


def wallet_token_units(
    chain: Chain, wallet: str, token: str, decimals: int
) -> int | None:
    """What the WALLET holds, in base units. ``None`` when we cannot say.

    MEASURED payload, 2026-09-22::

        {"balances":[{"wallet_address":"6a1UR...","token_address":"8JVtw...",
                      "balance":"47876.517476461","decimal":0,"height":449306003}]}

    Same three traps as :func:`kaiba.execution.risk.parse_native_balance`, and for the same
    reason -- it is the same endpoint:

    * ``balance`` is a decimal string in WHOLE TOKENS, scaled here by the token's own
      decimals through :class:`~decimal.Decimal`. Floats lose base units at 18 decimals
      and the error is not guaranteed to round down.
    * ``decimal`` is a placeholder and reads 0 on every chain measured. Trusting it turns
      47,876 tokens into 47,876 base units. It is ignored.
    * ``height`` is the block read at, and is 0 exactly when GMGN has nothing. A zero with
      no height is UNAVAILABLE, not empty -- reporting it as empty would refuse an exit on
      a position we really hold.

    Truncates toward zero: a fractional base unit rounded up is money we do not have, and
    money we do not have is a rejected send.
    """
    try:
        payload = _token_balance_payload(chain, wallet, token)
    except Exception as exc:  # noqa: BLE001 - a dead CLI is "unknown", never a number
        log.warning("token balance unavailable for %s: %s", token, exc)
        return None
    # The provider unwraps this endpoint to the ``balances`` list; the raw body is
    # accepted too so a change to that registry degrades to "unknown" rather than to a
    # wrong number.
    rows = payload.get("balances") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict):
        return None
    raw = row.get("balance")
    if raw is None:
        return None
    try:
        whole = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    height = row.get("height")
    try:
        height = int(height)
    except (TypeError, ValueError):
        height = 0
    if whole == 0 and height == 0:
        return None
    if whole < 0:
        return None
    return int(whole * (Decimal(10) ** int(decimals)))


def _exit_slippage_bps() -> int:
    """What an exit may give up. Falls back to the entry bound if the key is absent.

    See ``EnvelopeBounds.max_exit_slippage_bps``. The fallback is deliberate: a config
    that predates the key must keep exiting at the old tolerance rather than raise.
    """
    bounds = get_risk().bounds
    value = getattr(bounds, "max_exit_slippage_bps", None)
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return int(bounds.max_slippage_bps)
    # Never TIGHTER than an entry: an exit that gave up less than a buy would be the
    # wrong way round, and a mis-set key must not silently make stops harder to fill.
    return max(value, int(bounds.max_slippage_bps))


#: A robinhood / bsc paper exit whose pool could not be READ (transport, the robinhood pacer, the
#: bsc host's breaker) is re-decided on later ticks this many times, within this long, before it
#: falls back to the deciding mark. 2026-10-23 (step 3 review, blocking #5): a pacer refusal in a
#: burst of stops used to fill at the mark at once and drop the row from the scoreboard. INVENTED
#: sizes: two retries ride the watchdog's own exit backoff (5 s, 10 s) across the next 12 s ticks,
#: so a stop is filled within about half a minute either way.
PAPER_POOL_RETRIES = 2
PAPER_POOL_RETRY_MAX_MS = 60_000

#: A COUNTED robinhood / bsc paper position no mark source has priced for an hour is asked ONE
#: pool read at EXIT priority (``DefaultExitSubmitter.paper_blind_exit``). When that read learns
#: nothing (transport, the robinhood pacer, the bsc host's breaker), the position stays OPEN and is
#: asked again -- at most :data:`PAPER_POOL_RETRIES` more times, no sooner than this long after the
#: last read, so a pacer burst can pass and a blind position never costs a read per tick -- and only
#: then abandoned. 2026-10-23 (step 3 follow-up, blocking). INVENTED as a spacing: three reads span
#: at least two minutes.
PAPER_BLIND_RETRY_GAP_MS = 60_000

#: What :meth:`DefaultExitSubmitter.paper_blind_exit` found (``_BlindPaperExit.action``).
BLIND_ZERO = "zero"  # the token takes the whole sale, or the pool answered it cannot pay: book zero
BLIND_SELL = "sell"  # the pool PAYS: sell the remainder at that fill, an honest pool leg
BLIND_RETRY = "retry"  # nothing could be read: keep the position open, ask again (bounded)
BLIND_ABANDON = "abandon"  # this module cannot price the token at all: the old abandon path


@dataclass(frozen=True)
class _BlindPaperExit:
    """What a robinhood / bsc paper position no mark source prices gets (``paper_blind_exit``)."""

    action: str  # BLIND_ZERO | BLIND_SELL | BLIND_RETRY | BLIND_ABANDON
    #: The exit leg's fill label (``total_loss``, ``pool_no_fill_bound``, the pool fill's basis) or,
    #: for a retry / abandon, the refusal kind.
    label: str
    note: str
    tax_bps: int | None = None
    #: The pool's fill for the whole remainder (``evm_pool_price.Fill``) when ``action`` is a sell.
    fill: Any = None


@dataclass(frozen=True)
class _EvmExitFill:
    """How a robinhood / bsc paper exit is filled (``DefaultExitSubmitter._paper_evm_fill``)."""

    tax_bps: int | None
    executable: Decimal | None
    basis: str  # v2_formula | quoter_v2 | v4_quoter | curve_mark | mark | pool_no_fill_bound | total_loss | retry
    note: str
    depth: Decimal | None
    #: The pool could not be read: fill nothing this tick, let the watchdog re-decide.
    retry: bool = False
    #: The pool answered that it cannot pay for this quantity, or the token takes the whole sale
    #: (``basis`` total_loss): book the leg at zero proceeds.
    no_fill: bool = False
    #: The venue charge for this leg when it is not the chain's flat stand-in: a Pons curve exit pays
    #: the curve's own take (``paper_costs.pons_curve_venue_bps``). ``None`` = the flat stand-in.
    venue_bps: int | None = None
    #: False when a cost this leg must pay could not be read (a Pons curve exit's take): the flat
    #: stand-in is charged and the leg is tagged not honest.
    cost_known: bool = True
    #: A curve sale priced at our quantity in NATIVE base units (``basis`` curve_quote: the Flap curve with
    #: our holding put back, net of its protocol fee, before the router and the sell tax). Converted to a
    #: per-token USD executable at the broker's own native price in :meth:`DefaultExitSubmitter._paper`,
    #: so the broker books exactly this many base units before the call site's costs and its own.
    executable_native: int | None = None
    #: The quantity ``executable_native`` is for (the broker sells the same: ``paper.PaperBroker.sell``).
    qty_sold: int | None = None


class DefaultExitSubmitter:
    """Paper for shadow positions, the executor's own submit path for everything else.

    A shadow position never had real tokens, so selling it anywhere but the paper broker
    would be fiction in the other direction. Canary and live positions go through
    :func:`kaiba.execution.executor.submit`, which owns reserve-before-submit and the
    ambiguous-send rule; nothing here duplicates that.
    """

    def __init__(self, conn: sqlite3.Connection, price_source: PriceSource | None = None) -> None:
        self.conn = conn
        self.price_source = price_source or NullPriceSource()
        #: Per-call step durations, filled by ``_live``. The watchdog calls this from its
        #: one tick thread, so one dict is enough.
        self._timing: dict[str, int] = {}
        #: position_id -> (first unread pool fill ms, retries so far). See PAPER_POOL_RETRIES.
        self._pool_retry: dict[str, tuple[int, int]] = {}

    def submit_exit(
        self, position: Position, pct: Decimal, *, quote: PriceQuote, reason: str
    ) -> ExitOutcome:
        self._timing = {}
        started = time.monotonic()
        if position.mode is LaneMode.SHADOW:
            outcome = self._paper(position, pct, quote, reason)
        else:
            outcome = self._live(position, pct, quote, reason)
        timing = {**self._timing, "total_ms": int((time.monotonic() - started) * 1000)}
        try:
            return replace(outcome, timing=timing)
        except Exception:  # noqa: BLE001 - timing is diagnostics; the outcome is not
            return outcome

    def _step(self, name: str, started: float) -> None:
        self._timing[name] = int((time.monotonic() - started) * 1000)

    # ------------------------------------------------------------------ paper

    def _paper(
        self, position: Position, pct: Decimal, quote: PriceQuote, reason: str, *, pool_fill: Any = None
    ) -> ExitOutcome:
        """``pool_fill``: an ``evm_pool_price.Fill`` already read for exactly this sale
        (:meth:`paper_sell_at_fill`), used instead of a second pool read."""
        if quote.price_usd is None or quote.price_usd <= 0:
            return ExitOutcome(False, None, None, "paper exit needs a price")
        # 2026-10-13: no longer refused when the quote carries no liquidity figure. The
        # broker prices off the bonding curve when there is one (exact, needs no depth) and
        # otherwise fills at the worst slippage an exit may give up, labelled
        # `worst_allowed_no_depth` (see PaperBroker.sell). Refusing here meant curve-priced
        # quotes, which carry no liquidity number, never reached the curve path, and 96 sol
        # paper positions in a week rode to a -100% write-off after their exit had been
        # decided on a usable price.
        # The broker's own cap (DEFAULT_SLIPPAGE_BPS, 15%) bounds that fill, NOT the live
        # exit tolerance: max_exit_slippage_bps is 8,800 on the box so a live stop always
        # fills, and a paper fill at -88% would be the -100% write-off by another name.
        liquidity = quote.liquidity_usd if quote.liquidity_usd and quote.liquidity_usd > 0 else None
        # Real native prices, not paper.DEFAULT_NATIVE_USD (2026-10-18; viability.paper_native_usd).
        from kaiba.execution.viability import paper_native_usd

        native_usd = paper_native_usd(self.conn)
        # A chain missing here is converted at paper.DEFAULT_NATIVE_USD. The exit still goes -- a
        # position must close -- but a tagged row says so and leaves the scoreboard (design 6a).
        native_placeholder = position.chain not in native_usd
        broker = PaperBroker(self.conn, curve_resolver=_curve_resolver(self.conn), native_usd=native_usd)
        # Live sell costs the broker's model leaves out (router fee, robinhood hook fee):
        # kaiba/execution/paper_costs.py, 2026-10-19.
        from kaiba.execution import paper_costs

        raw_curve, curve = self._paper_curve_read(position)
        no_flow = self._paper_no_flow(position, raw_curve)
        # Robinhood / bsc (2026-10-21, design step 3b): the token's sell tax, and a FILL from the
        # pool at this quantity -- the deciding mark only triggers the exit. See _paper_evm_fill.
        evm = self._paper_evm_fill(position, pct, quote, pool_fill=pool_fill)
        tax_bps, executable, fill_basis, fill_note, depth = evm.tax_bps, evm.executable, evm.basis, evm.note, evm.depth
        if evm.retry:
            # The pool could not be READ (transport, pacer, breaker): nothing is filled this tick,
            # and the watchdog re-decides the exit on the next one. Bounded: PAPER_POOL_RETRIES.
            paper_costs.record_exit_basis(
                self.conn, position.position_id, position.chain, mark_source=quote.source,
                fill=paper_costs.RETRY_LEG, tax_bps=tax_bps, filled=False, note=fill_note,
            )
            return ExitOutcome(False, None, None, f"paper exit waits for a pool read: {fill_note}"[:300])
        if liquidity is None and depth is not None:
            liquidity = depth
        no_fill = evm.no_fill
        # The broker measures an executable against this mark; it is the deciding quote's price except on
        # a curve sale priced at our quantity (``curve_quote``). There the live curve's spot is not a price
        # we could sell at -- the curve never received our paper buy, and the sale with our holding put
        # back can pay more or less per token than it -- so the broker is handed the sale's own price as
        # its mark too: impact 0, and the leg books exactly the curve's answer less the broker's floor and
        # fee (the broker floors impact to whole bps, which against a different mark rounds in our favour).
        mark_usd = Decimal(quote.price_usd)
        if executable is None and not no_fill and evm.executable_native is not None:
            executable = self._paper_native_executable(position, evm, broker)
            if executable is None:
                fill_basis, fill_note = "curve_mark", f"curve sale unconvertible, the broker approximates: {fill_note}"
            else:
                mark_usd = executable
        costs: dict[str, Any] = {"tax_bps": tax_bps or 0,
                                 "venue_fee_in_price": (executable is not None and not no_fill
                                                        and fill_basis in paper_costs.FEE_IN_PRICE_FILLS),
                                 "venue_bps": evm.venue_bps}
        if not no_fill and paper_costs.exit_is_total_loss(position.chain, **costs):
            # 2026-10-23 (step 3 follow-up, blocking): router + venue + the token's sell tax take the
            # whole sale. exit_price would be <= 0, the broker would refuse it `no_price` on every
            # tick and the position would never close, its ~-100% never booked. A live sell of this
            # token receives nothing, so the leg is booked at exactly that: zero, honestly.
            no_fill, executable, depth = True, None, None
            fill_note = (f"router + venue + sell tax {tax_bps} bps >= 100% of the quote, booked at zero "
                         f"proceeds (was: {fill_basis} {fill_note})")[:200]
            fill_basis = paper_costs.TOTAL_LOSS
        price_usd = paper_costs.exit_price(position.chain, mark_usd, **costs)
        if no_fill:
            # The leg books ZERO proceeds: the pool ANSWERED that it cannot pay for this quantity,
            # or the token takes the whole sale. The broker has no "zero" input, so hand it the
            # executable whose gap to the mark is the whole price less its own slippage floor (half
            # a bp inside it, so Decimal rounding cannot leave a sliver): impact + floor == 10,000
            # bps -> effective price 0. Pinned by tests/test_paper_exit_evm.py, which reads the
            # booked proceeds, not this arithmetic. The broker refuses a price <= 0 (`no_price`),
            # which is exactly what a total tax makes, so the mark it is handed then carries the
            # router and venue charges but not the tax: what it receives is zero either way.
            if price_usd <= 0:
                price_usd = paper_costs.exit_price(position.chain, quote.price_usd, **{**costs, "tax_bps": 0})
            if price_usd <= 0:
                price_usd = Decimal(quote.price_usd)
            floor_bps = max(1, int(getattr(broker, "slippage_floor_bps", 0) or 0))
            broker_executable: Decimal | None = price_usd * (Decimal(floor_bps) - Decimal("0.5")) / Decimal(10_000)
        else:
            broker_executable = (paper_costs.exit_price(position.chain, executable, **costs)
                                 if executable is not None else None)
        order = broker.sell(
            position,
            pct,
            price_usd=price_usd,
            liquidity_usd=liquidity,
            exit_reason=reason[:120],
            curve=curve,
            # None keeps the broker's own latency (curve_price.DEFAULT_LATENCY_MS).
            latency_ms=0 if no_flow is not None else None,
            executable_price_usd=broker_executable,
        )
        if no_flow is not None:
            self._journal_no_flow(position, order, reason, no_flow)
        if position.chain in paper_costs.BASIS_CHAINS:
            # 2026-10-23 (hA_): the leg is judged by the BROKER's record of this fill, not by the label
            # this method chose: a sale the broker filled on its constant-product approximation (no
            # executable at our size) is not honest whatever priced the mark.
            from kaiba.execution.paper import fill_basis as _broker_fill_basis

            filled = order.state is OrderState.FILLED
            paper_costs.record_exit_basis(
                self.conn, position.position_id, position.chain, mark_source=quote.source,
                fill=fill_basis, tax_bps=tax_bps, filled=filled, note=fill_note,
                venue_bps=paper_costs.venue_charge_bps(position.chain, bool(costs["venue_fee_in_price"]),
                                                 evm.venue_bps),
                cost_known=evm.cost_known, native_placeholder=native_placeholder,
                broker=paper_costs.broker_leg(_broker_fill_basis(self.conn, order.order_id)) if filled else None,
            )
        return ExitOutcome(
            order.state is OrderState.FILLED, order.state, order.order_id, order.error or ""
        )

    def _paper_evm_fill(self, position: Position, pct: Decimal, quote: PriceQuote, *,
                        pool_fill: Any = None) -> _EvmExitFill:
        """The sell tax, and how a robinhood / bsc paper exit is filled (:class:`_EvmExitFill`).

        Robinhood and bsc only; everywhere else ``(0, None, "mark", "", None)`` and the exit is
        exactly what it was. MEASURED before (box, 2026-10-21): 54 of 62 robinhood and 19 of 19
        bsc paper exits were priced off DexScreener, and the bsc sell tax (nonzero on 41 of 50
        bsc paper positions) was charged on none of them.

        * Tax (bsc): the HIGHER of the sell tax recorded at the entry (``paper_costs`` tag) and the
          dossier's current one. Unknown on both is charged as 0 and the leg is tagged not-honest
          -- an exit is never refused. Robinhood: the measured sell tax when the entry measured one
          (``paper_costs.TRANSFER_TAX_MEASURED``), else 0 as before.
        * Fill: a curve-decided exit (``evm-venue:``) sells on the curve the mark read (the broker
          models it against the curve's reserve). Otherwise ONE pool read at ``Priority.EXIT``
          (``evm_pool_price.exit_fill``): the v2 formula, QuoterV2 or V4Quoter at this quantity.
        * When the pool cannot fill (2026-10-23, step 3 review blocking #5 --
          ``evm_pool_price.exit_refusal_kind``): the pool ANSWERED that it cannot pay -> the leg is
          booked at zero proceeds (``no_fill``); nothing could be READ -> ``retry`` on the next
          tick, at most :data:`PAPER_POOL_RETRIES` times within :data:`PAPER_POOL_RETRY_MAX_MS`,
          then the mark; the module cannot price the token at all -> the mark, labelled ``mark``.
          Every one of these rows stays on the scoreboard when its entry was pool- or
          curve-priced, and the report counts them (``paper_costs.exit_degraded``).
        * When the token takes the whole sale (2026-10-23, blocking -- :meth:`_paper_total_loss`:
          sell tax + router >= 100%, or the current dossier says it cannot be sold): booked at zero
          proceeds BEFORE any pool read, labelled ``total_loss`` -- an exact fill, so honest.
        * A robinhood Pons curve sell (``curve_mark``) pays the curve's own take, ``feeBps +
          creatorTaxBps`` (read with the mark, ``PriceQuote.curve_take_bps``, or at the entry -- the
          higher), as ``venue_bps``; unread on both, the flat stand-in and a not-honest leg.
        """
        from kaiba.execution import paper_costs

        if position.chain not in paper_costs.BASIS_CHAINS:
            return _EvmExitFill(0, None, "mark", "", None)
        tax, entry, dossier = self._paper_exit_tax(position)
        total = self._paper_total_loss(position, tax, dossier)
        if total is not None:
            # 2026-10-23 (step 3 follow-up, blocking): the token takes the whole sale. Decided BEFORE
            # any pool read: no pool answer changes what a live sell of it receives, and a pool that
            # says "no fill" or cannot be read must not relabel an exact zero as a bound or a retry.
            self._pool_retry.pop(position.position_id, None)
            return _EvmExitFill(tax, None, paper_costs.TOTAL_LOSS, total, None, no_fill=True)
        if (quote.source or "").startswith("evm-venue:"):
            self._pool_retry.pop(position.position_id, None)
            if position.chain is Chain.BSC:
                return self._paper_flap_curve_fill(position, pct, quote, tax)
            venue_bps, cost_known, note = None, True, "sold on the curve the mark read"
            if position.chain is Chain.ROBINHOOD:
                # A Pons curve sell pays the curve's own take (feeBps + creatorTaxBps), read with this
                # very mark, or recorded at the entry -- the HIGHER of the two; unknown on both
                # charges the flat stand-in and tags the leg not honest (2026-10-23, blocking).
                takes = [t for t in (paper_costs.pons_curve_take_bps(quote.curve_take_bps, 0),
                                     paper_costs.pons_curve_take_bps(entry.get("curve_take_bps"), 0))
                         if t is not None]
                take = max(takes) if takes else None
                # 2026-10-23 (hB_): SOLD on the curve at our quantity, our holding put back, when the mark
                # carries the curve's state; otherwise the mark below, which the broker approximates.
                exact, why_not = self._paper_pons_curve_fill(position, pct, quote, tax, entry, take)
                if exact is not None:
                    return exact
                venue_bps = paper_costs.pons_curve_venue_bps(take)
                cost_known = take is not None
                note = (f"sold on the curve the mark read; curve take {take} bps -> venue {venue_bps} bps"
                        if take is not None else "sold on the curve the mark read; curve take UNREAD, flat stand-in")
                note = f"{note}; mark only, the broker approximates: {why_not}"
            return _EvmExitFill(tax, None, "curve_mark", note, None, venue_bps=venue_bps, cost_known=cost_known)
        return self._paper_pool_fill(position, pct, quote, tax, pool_fill=pool_fill)

    def _paper_flap_curve_fill(self, position: Position, pct: Decimal, quote: PriceQuote,
                               tax: int | None) -> _EvmExitFill:
        """A bsc paper exit decided on the Flap curve, SOLD on that curve at our quantity.

        2026-10-23 (hA_; Vanguard review section 6 item 3). It used to be labelled ``curve_mark`` and
        handed to the broker with no executable, so the broker filled it on its constant-product
        approximation against the curve's quote reserve -- MEASURED on the box: 2,388 and 7,121 bps of
        impact against $298 and $34.66, booking -27.0% and -75.0% on the only two bsc rows the
        scoreboard counted, where the curve itself pays -3.0% and -11.6% for the same tokens with our
        holding put back. Now the curve state from the deciding mark's OWN read
        (``PriceQuote.venue_curve``, no extra call) prices the sale exactly
        (``evm_price.flap_sell_quote_out``: our whole remaining holding put back, this sale taken out,
        the Flap protocol fee off the proceeds), and the leg is an executable at our size
        (``curve_quote``). The sell tax charged is the HIGHEST of the entry's, the dossier's and the
        curve record's own (word 13). Without the curve state (a mark rebuilt from memory, a non-native
        curve, a sale the curve cannot price) the old ``curve_mark`` path runs and the broker's own
        record books the leg not-honest."""
        from kaiba.execution import evm_price as ep
        from kaiba.execution import paper_costs

        same_token = str(quote.token or position.token).lower() == str(position.token).lower()
        snap = quote.venue_curve if (quote.venue == "flap" and same_token) else None
        curve = ep.flap_curve_from_snapshot(snap) if isinstance(snap, dict) else None
        why = "the mark carries no flap curve state" if curve is None else ""
        out = qty = None
        if curve is not None:
            try:
                from kaiba.execution.paper import load_position

                live = load_position(self.conn, position.position_id) or position
                held = int(live.qty or 0)
                # The broker's own quantity rule (paper.PaperBroker.sell), so the price is for what it sells.
                qty = held if pct >= 100 else int(Decimal(held) * Decimal(str(pct)) / Decimal(100))
                decimals = _paper_qty_decimals(self.conn, position)
                if decimals != curve.token_decimals:
                    why = f"paper qty decimals {decimals} != curve token decimals {curve.token_decimals}"
                else:
                    out, why = ep.flap_sell_quote_out(curve, held, qty)
            except Exception as exc:  # noqa: BLE001 - an unpriceable sale falls back, never a crash
                out, why = None, f"flap_sale_raised:{type(exc).__name__}"
        if out is None or out <= 0 or not qty:
            return _EvmExitFill(tax, None, "curve_mark",
                                f"sold at the flap curve mark, the broker approximates: {why or 'curve pays nothing'}"[:200],
                                None)
        known = [t for t in (tax, paper_costs.leg_tax_bps(Chain.BSC, curve.sell_tax_bps)[0]
                             if curve.sell_tax_bps is not None else None) if t is not None]
        tax = max(known) if known else None
        note = (f"sold on the flap curve at our quantity, holding put back: {out} wei for {qty} atoms "
                f"(progress {curve.tokens_sold_atoms * 100 // max(1, curve.graduation_tokens_atoms)}%)")
        return _EvmExitFill(tax, None, paper_costs.CURVE_QUOTE_EXIT, note[:200], None, executable_native=int(out),
                            qty_sold=int(qty))

    def _paper_pons_curve_fill(self, position: Position, pct: Decimal, quote: PriceQuote, tax: int | None,
                               entry: dict[str, Any], take: int | None) -> tuple[_EvmExitFill | None, str]:
        """A robinhood paper exit decided on the Pons curve, SOLD on that curve at our quantity.

        2026-10-23 (hB_; Vanguard review section 6 item 2). It was labelled ``curve_mark`` and handed to the
        broker with no executable, so the broker approximated the sale against the curve's REAL reserve
        (MEASURED: the only Pons exit on the box, ``pos_eb3b7baf1425d27f38f27d72``, was such a ``dex`` fill).
        Now the curve state of the deciding mark's OWN read (``PriceQuote.venue_curve``, no extra call) prices
        the sale on the contract's sell arithmetic (``evm_price.pons_sell_quote_out``, MEASURED to the wei on
        3,478 chain sells) with our whole remaining holding put back: the smaller of the two counterfactual
        curves, the token side and the quote our entries put in (``paper_costs.holding_entry_net``). The
        proceeds are net of ``feeBps + creatorTaxBps``; the venue charge is only what keeps the leg's fees
        at a curve-mark leg's (``paper_costs.pons_exact_venue_bps``). No latency drift: a live sell lands
        0.08 s after its order (MEASURED, median of 19 live Pons sells), against 14.6 s for a buy.

        ``(None, why)`` -- the caller then books the mark, which the broker's record keeps off the
        scoreboard -- without the curve state (a mark rebuilt from memory, a non-native curve), with the
        broker's token decimals disagreeing with the chain's, or a sale the curve cannot price."""
        from kaiba.execution import evm_price as ep
        from kaiba.execution import paper_costs

        same_token = str(quote.token or position.token).lower() == str(position.token).lower()
        snap = quote.venue_curve if (quote.venue == "pons" and same_token) else None
        curve = ep.pons_curve_from_snapshot(snap) if isinstance(snap, dict) else None
        if curve is None:
            return None, "the mark carries no pons curve state"
        try:
            from kaiba.execution.paper import load_position

            live = load_position(self.conn, position.position_id) or position
            held = int(live.qty or 0)
            # The broker's own quantity rule (paper.PaperBroker.sell), so the price is for what it sells.
            qty = held if pct >= 100 else int(Decimal(held) * Decimal(str(pct)) / Decimal(100))
            decimals = _paper_qty_decimals(self.conn, position)
            if decimals != curve.token_decimals:
                return None, f"paper qty decimals {decimals} != curve token decimals {curve.token_decimals}"
            out, why = ep.pons_sell_quote_out(curve, held, qty,
                                              entry_net_quote=paper_costs.holding_entry_net(entry, held))
        except Exception as exc:  # noqa: BLE001 - an unpriceable sale falls back, never a crash
            return None, f"pons_sale_raised:{type(exc).__name__}"
        if out is None or out <= 0 or not qty:
            return None, why or "the curve pays nothing for this sale"
        venue_bps = paper_costs.pons_exact_venue_bps(curve.take_bps, take_charged=take)
        note = (f"sold on the pons curve at our quantity, holding put back: {out} wei for {qty} atoms; take "
                f"{curve.take_bps} bps in the price, venue {venue_bps} bps")
        return _EvmExitFill(tax, None, paper_costs.CURVE_QUOTE, note[:200], None, venue_bps=venue_bps,
                            cost_known=venue_bps is not None, executable_native=int(out), qty_sold=int(qty)), "ok"

    @staticmethod
    def _paper_native_executable(position: Position, evm: _EvmExitFill, broker: Any) -> Decimal | None:
        """``evm.executable_native`` as a per-token USD executable at the BROKER's own native price and
        token decimals, so the broker's ``proceeds_usd / native_usd`` gives back exactly those base units
        (before the call site's router and tax, and its own fee and floor). ``None`` when unconvertible."""
        try:
            from kaiba.core.schemas import NATIVE_DECIMALS
            from kaiba.execution.paper import _token_decimals  # noqa: PLC0415 - the broker's own units

            qty = int(evm.qty_sold or 0)
            native_usd = Decimal(broker.native_usd[position.chain])
            if qty <= 0 or native_usd <= 0 or evm.executable_native is None or evm.executable_native <= 0:
                return None
            decimals = int(_token_decimals(position.chain, position.token, broker.conn, None))
            whole_native = Decimal(int(evm.executable_native)) / (Decimal(10) ** NATIVE_DECIMALS[position.chain])
            return whole_native * native_usd / (Decimal(qty) / (Decimal(10) ** decimals))
        except Exception:  # noqa: BLE001 - unconvertible is "approximated", never a crash
            log.debug("paper exit: native executable unconvertible for %s", position.token[:12], exc_info=True)
            return None

    def _paper_exit_tax(self, position: Position) -> tuple[int | None, dict[str, Any], Any]:
        """``(sell tax to charge, the entry tag, the current dossier or None)`` for a robinhood / bsc
        paper exit (:meth:`_paper_evm_fill`)."""
        from kaiba.execution import paper_costs

        tax: int | None = 0
        basis = paper_costs.read_basis(self.conn, position.position_id) or {}
        entry = basis.get("entry") if isinstance(basis.get("entry"), dict) else {}
        recorded = entry.get("sell_tax_bps") if isinstance(entry, dict) else None
        dossier = self._paper_dossier(position)
        if position.chain in paper_costs.TAXED_CHAINS:
            # 2026-10-23 (step 3 follow-up): the HIGHER of the tax recorded at the entry and the
            # dossier's current figure. It used to prefer the entry's and read the dossier only when
            # the entry had none, so a tax raised after the entry (a fresher dossier read) was never
            # charged -- optimistic on exactly the tokens that raise their sell tax.
            current = None
            try:
                if dossier is not None and dossier.sell_tax_bps.known:
                    current = dossier.sell_tax_bps.value
            except Exception:  # noqa: BLE001 - unknown tax is labelled, never a crash
                current = None
            known = [t for t in (paper_costs.leg_tax_bps(position.chain, v)[0] for v in (recorded, current))
                     if t is not None]
            tax = max(known) if known else None
        elif isinstance(entry, dict) and entry.get("transfer_tax") == paper_costs.TRANSFER_TAX_MEASURED:
            # A robinhood token whose transfer tax was MEASURED at the entry pays it on the sell too
            # (paper_costs.measured_transfer_tax); every other robinhood leg keeps tax 0, as before.
            try:
                tax = max(0, int(recorded or 0))
            except (TypeError, ValueError):
                tax = None
        return tax, (entry if isinstance(entry, dict) else {}), dossier

    def _paper_dossier(self, position: Position) -> Any:
        """The token's current dossier, or ``None`` (never a crash)."""
        try:
            from kaiba.execution.engine import load_dossier

            return load_dossier(position.chain, position.token, self.conn)
        except Exception:  # noqa: BLE001 - an unreadable dossier is "no evidence", not an error
            return None

    def _paper_total_loss(self, position: Position, tax: int | None, dossier: Any) -> str | None:
        """Why a robinhood / bsc paper sell of this token receives NOTHING, or ``None``.

        2026-10-23 (step 3 follow-up, blocking). Two facts make the leg an exact zero, whatever the
        pool would quote -- and no quoter here can see either, because none of them runs the token's
        own transfer (QuoterV2 and V4Quoter revert before the token is paid in; the v2 formula is
        arithmetic on reserves):

        * the sell tax alone, with the router fee, is 100% or more of the quote
          (``paper_costs.exit_is_total_loss`` with no venue charge: true on any fill). GMGN reports a
          honeypot as ``sell_tax`` 1, which dyor scales to 10,000 bps;
        * the CURRENT dossier says the token cannot be sold (``can_sell=False``, the ``honeypot``
          blocker) -- a transfer that was switched off after the entry (an entry is refused with it).
        """
        from kaiba.core.schemas import TokenRisk
        from kaiba.execution import paper_costs

        if dossier is not None:
            try:
                if getattr(dossier, "can_sell", None) is False or TokenRisk.HONEYPOT in (dossier.blockers or []):
                    return "current dossier says the token cannot be sold (can_sell=False / honeypot): zero proceeds"
            except Exception:  # noqa: BLE001 - an odd dossier is no evidence
                pass
        if tax is not None and paper_costs.exit_is_total_loss(position.chain, tax, venue_fee_in_price=True):
            return f"sell tax {tax} bps with the router fee is >= 100% of the quote: zero proceeds"
        return None

    def paper_blind_exit(self, position: Position) -> _BlindPaperExit:
        """What the evidence says about a robinhood / bsc paper position no mark source has priced
        for an hour (:meth:`Watchdog._settle_blind_counted_paper`). At most ONE pool read, at EXIT
        priority, for the whole remainder -- after the checks that need none.

        The alternative is ``abandoned_unpriceable``, a booked write-off every scoreboard LEAVES OUT
        (``fabricated.FABRICATED_EXIT_FAMILIES``): right when nothing is known, optimistic when the
        loss is real -- a drained pool is exactly the token no mark source prices any more.
        2026-10-23 (step 3 follow-up, blocking; verifier probe ``v3c_probe_dust_pool.py``): this used
        to answer only "total loss or not", so a pool that PAID anything sent the row to that
        write-off. A rugged v2 pair keeps MINIMUM_LIQUIDITY dust in both reserves and always pays a
        few wei: reserves (1e18 tokens, 1,000 wei WBNB) paid 998 wei for a 5,000,000-wei position,
        -99.98%, and the row left the record. Now:

        * the token takes the whole sale (:meth:`_paper_total_loss`) -> :data:`BLIND_ZERO`,
          ``total_loss`` (no pool read);
        * the pool ANSWERS it cannot pay (``evm_pool_price.EXIT_NO_FILL``) -> :data:`BLIND_ZERO`,
          ``pool_no_fill_bound``;
        * the pool PAYS, whatever it pays -> :data:`BLIND_SELL` with that fill: the remainder is sold
          AT it (:meth:`paper_sell_at_fill`), an honest pool leg, counted;
        * nothing could be READ (``EXIT_RETRY``: transport, pacer, breaker, a raising reader) ->
          :data:`BLIND_RETRY`: the watchdog keeps the position open and asks again, bounded;
        * this module cannot price the token at all (``EXIT_AT_MARK``: no pool known, no bsc pool
          host, a foreign quote asset) -> :data:`BLIND_ABANDON`, the old path.
        """
        from kaiba.execution import evm_pool_price, paper_costs

        if position.chain not in paper_costs.BASIS_CHAINS:
            return _BlindPaperExit(BLIND_ABANDON, evm_pool_price.EXIT_AT_MARK, "not a pool-priced chain")
        tax, _entry, dossier = self._paper_exit_tax(position)
        total = self._paper_total_loss(position, tax, dossier)
        if total is not None:
            return _BlindPaperExit(BLIND_ZERO, paper_costs.TOTAL_LOSS, total, tax)
        fill, why = self._paper_pool_read(position, Decimal(100), None)
        if fill is not None:
            return _BlindPaperExit(BLIND_SELL, fill.basis,
                                   f"no source priced it; the pool pays {fill.amount_out} base units for the "
                                   f"remainder ({fill.ref.kind} pool {fill.ref.pool})"[:200], tax, fill)
        kind = evm_pool_price.exit_refusal_kind(why)
        if kind == evm_pool_price.EXIT_NO_FILL:
            return _BlindPaperExit(BLIND_ZERO, paper_costs.NO_FILL_BOUND,
                                   f"no source priced it and the pool cannot pay: {why}"[:200], tax)
        if kind == evm_pool_price.EXIT_RETRY:
            return _BlindPaperExit(BLIND_RETRY, kind, f"pool unread: {why}"[:200], tax)
        return _BlindPaperExit(BLIND_ABANDON, kind, f"pool exit fill unavailable: {why}"[:200], tax)

    def paper_sell_at_fill(self, position: Position, fill: Any, *, reason: str) -> ExitOutcome:
        """Sell a robinhood / bsc paper position's WHOLE remainder at ``fill`` -- the pool's own answer
        for exactly that quantity (:meth:`paper_blind_exit`) -- through the normal paper exit path.

        No second pool read: the fill is handed to :meth:`_paper`, which charges the router, the
        token's sell tax and the broker's slippage floor as on any exit and tags the leg with the
        fill's basis (an honest pool leg). The "mark" the broker measures the fill against is the
        pool's spot from the same read, or the fill itself when there is none -- the HIGHER of the
        two, so the floor is never charged on less than the fill.
        """
        executable = Decimal(fill.executable_price_usd)
        spot = fill.spot_price_usd
        price = max(Decimal(spot), executable) if spot is not None and Decimal(spot) > 0 else executable
        quote = PriceQuote(
            price_usd=price,
            liquidity_usd=fill.liquidity_usd if fill.liquidity_usd and fill.liquidity_usd > 0 else None,
            basis=EvidenceBasis.VERIFIED_ONCHAIN,
            observed_ms=int(fill.observed_ms),
            source=f"evm-pool:{fill.ref.venue}:blind_exit",
            chain=position.chain,
            token=position.token,
            pool_id=fill.ref.pool,
        )
        return self._paper(position, Decimal(100), quote, reason, pool_fill=fill)

    def _paper_pool_read(self, position: Position, pct: Decimal, pool_hint: str | None) -> tuple[Any, str]:
        """``evm_pool_price.exit_fill`` for ``pct`` of the live remainder; a raise reads as unread."""
        from kaiba.execution import evm_pool_price

        try:
            from kaiba.execution.paper import load_position

            live = load_position(self.conn, position.position_id) or position
            qty = int(live.qty or 0) if pct >= 100 else int(Decimal(int(live.qty or 0)) * pct / Decimal(100))
            return evm_pool_price.exit_fill(
                position, qty, self.conn, qty_decimals=_paper_qty_decimals(self.conn, position) or 18,
                # A HINT only: resolve_robinhood verifies it on chain (the v4 key must hash to it).
                pool_hint=pool_hint,
            )
        except Exception as exc:  # noqa: BLE001 - a raising reader read nothing: retry, then the mark
            return None, f"pool_exit_fill_raised:{type(exc).__name__}"

    def _paper_pool_fill(self, position: Position, pct: Decimal, quote: PriceQuote, tax: int | None, *,
                         pool_fill: Any = None) -> _EvmExitFill:
        """ONE pool read at EXIT priority (:meth:`_paper_evm_fill`'s pool half); none when ``pool_fill``
        already holds this sale's fill (:meth:`paper_sell_at_fill`)."""
        from kaiba.execution import evm_pool_price, paper_costs

        fill, why = (pool_fill, "ok") if pool_fill is not None else self._paper_pool_read(position, pct, quote.pool_id)
        if fill is not None:
            self._pool_retry.pop(position.position_id, None)
            note = f"{fill.ref.kind} pool {fill.ref.pool}"
            if pool_fill is not None:
                note += ", read when no mark source had priced it for an hour"
            return _EvmExitFill(tax, fill.executable_price_usd, fill.basis, note, fill.liquidity_usd)
        kind = evm_pool_price.exit_refusal_kind(why)
        if kind == evm_pool_price.EXIT_NO_FILL:
            self._pool_retry.pop(position.position_id, None)
            return _EvmExitFill(tax, None, paper_costs.NO_FILL_BOUND,
                                f"pool cannot fill, booked at zero proceeds: {why}"[:200], None, no_fill=True)
        if kind == evm_pool_price.EXIT_RETRY:
            ts = now_ms()
            first, tries = self._pool_retry.get(position.position_id, (ts, 0))
            if tries < PAPER_POOL_RETRIES and ts - first < PAPER_POOL_RETRY_MAX_MS:
                self._pool_retry[position.position_id] = (first, tries + 1)
                return _EvmExitFill(tax, None, paper_costs.RETRY_LEG,
                                    f"pool unread ({tries + 1}/{PAPER_POOL_RETRIES}), retry next tick: {why}"[:200],
                                    None, retry=True)
            self._pool_retry.pop(position.position_id, None)
            return _EvmExitFill(tax, None, "mark", f"pool unread after {tries} retries, at the mark: {why}"[:200], None)
        self._pool_retry.pop(position.position_id, None)
        return _EvmExitFill(tax, None, "mark", f"pool exit fill unavailable: {why}"[:200], None)

    def _paper_no_flow(self, position: Position, raw_curve: Any) -> dict[str, Any] | None:
        """Evidence that nobody has traded the token for the flow window, or None.

        The broker charges a curve sell ``DEFAULT_LATENCY_MS`` of drift at the token's measured
        flow, and ``curve_price.measure_flow`` falls back to the two newest ``curve_snapshots``
        with no lower time bound. MEASURED 2026-10-21 (box): five pump.fun launch-snipe paper
        positions, silent 48-53 h, were refused ``curve_drained_during_latency`` 104-114 times
        each, because the flow was the entry's own seconds-apart snapshots (-24M to -266M
        lamports/s); 837 failed paper sells over 120 tokens in 7 days, and the five held the
        lane's shadow cap. A curve whose reserves have not moved for
        ``curve_price.DEFAULT_FLOW_WINDOW_S`` has no flow to drift with, so the exit fills at
        ``latency_ms=0``. The root cause (the unbounded fallback) is in ``curve_price.py``, which
        is Codex-pinned: board item, docs/TASKS.md. This stays as defence in depth.

        Quiet is :func:`pumpswap_price.reserve_clock`: its last change at least the window ago
        (before any change, its first read: silence is only claimed for time watched). When this
        exit's own fresh read is available and its reserves differ from the clock's, the curve
        moved since the clock last looked, and the latency is charged. Sol only (the clock is
        sol's); anything unreadable keeps the latency.
        """
        if position.chain is not Chain.SOL:
            return None
        try:
            from kaiba.execution.curve_price import DEFAULT_FLOW_WINDOW_S
            from kaiba.execution.pumpswap_price import reserve_clock

            clock = reserve_clock(position.token)
            if clock is None:
                return None
            reserves, _first_ms, moved_ms = clock
            quiet_ms = now_ms() - int(moved_ms)
            if quiet_ms < int(DEFAULT_FLOW_WINDOW_S) * 1000:
                return None
            if raw_curve is not None and (
                (int(raw_curve.virtual_sol), int(raw_curve.virtual_token)) != tuple(int(x) for x in reserves)
            ):
                return None
            return {"reserves_moved_ms": int(moved_ms), "quiet_s": quiet_ms // 1000,
                    "flow_window_s": int(DEFAULT_FLOW_WINDOW_S), "fresh_read": raw_curve is not None}
        except Exception as exc:  # noqa: BLE001 - unknown activity keeps the latency charge
            log.debug("paper exit: reserve clock unreadable for %s: %s", position.token[:12], exc)
            return None

    def _journal_no_flow(self, position: Position, order: Any, reason: str, no_flow: dict[str, Any]) -> None:
        """One SYSTEM event per bypassed exit, so the bypass can be counted. There is no event
        kind for it in ``kaiba.core.schemas`` and adding one is core-owned, so it rides on
        SYSTEM tagged ``service=watchdog`` like every event this module writes; ``source`` is
        SERVICE too, which the drain (:meth:`Watchdog._classify`) skips."""
        try:
            ev.emit(
                EventKind.SYSTEM,
                {"service": SERVICE, "source": SERVICE, "event": PAPER_EXIT_NO_FLOW_EVENT,
                 "position_id": position.position_id, "lane": position.lane.value,
                 "order_id": order.order_id, "filled": order.state is OrderState.FILLED,
                 "error": (order.error or "")[:200], "exit_reason": reason[:120], **no_flow},
                chain=position.chain, subject=position.token, level="info", conn=self.conn,
            )
        except Exception as exc:  # noqa: BLE001 - the journal must never fail an exit
            log.warning("paper exit: no-flow event not written for %s: %s", position.position_id, exc)

    def _paper_curve(self, position: Position) -> Any:
        """:meth:`_paper_curve_read`'s curve with our holding put back (what the broker sells into)."""
        return self._paper_curve_read(position)[1]

    def _paper_curve_read(self, position: Position) -> tuple[Any, Any]:
        """``(curve as read, curve with our holding put back)``; ``(None, None)`` off sol or on
        a failed read. The second is for ``PaperBroker.sell(curve=)``; the first lets
        :meth:`_paper_no_flow` compare this read with the reserve clock.

        The token's bonding curve read now, for ``PaperBroker.sell(curve=)``; None otherwise.

        The broker prices a curve token off reserves and ignores the quote's price. Left to its
        resolver it took the newest ``curve_snapshots`` row up to 60 s old, which on a stop is a
        curve from BEFORE the fall: the first paper exit priced this way (2026-10-19, launch-snipe,
        19 s hold) sold against a snapshot 21 s old while the chain was 64% lower, and booked
        -26.7% on a token whose on-chain price had fallen 43% since the entry read, before fees
        and impact. One ``getAccountInfo`` at EXIT priority per paper exit. None (graduated, no
        curve, failed read) leaves the broker's resolution as it was.
        """
        if position.chain is not Chain.SOL:
            return None, None
        state = None
        try:
            from kaiba.core.limiter import Priority
            from kaiba.execution.paper import load_position
            from kaiba.execution.pumpswap_price import PumpCurveSource, with_holding

            state = PumpCurveSource(self.conn, priority=Priority.EXIT).read_state(position.token)
            if state is None:
                return None, None
            # The paper buy never reached the chain: put our remaining tokens back into the curve
            # before selling them (pumpswap_price.with_holding; without it a launch nobody else
            # bought sold at -100%, 2026-10-19).
            live = load_position(self.conn, position.position_id) or position
            return state, with_holding(state, int(live.qty or 0))
        except Exception as exc:  # noqa: BLE001 - the broker's own resolution is the fallback
            log.debug("paper exit: curve read failed for %s: %s", position.token[:12], exc)
            return state, None

    # ------------------------------------------------------------------ live

    def _live(self, position: Position, pct: Decimal, quote: PriceQuote, reason: str) -> ExitOutcome:
        _ = reason
        decimals = token_decimals(self.conn, position.chain, position.token)
        if decimals is None:
            return ExitOutcome(
                False, None, None, f"token decimals unknown for {position.token}; refusing to size a sell"
            )
        qty = position.qty if pct >= 100 else int(Decimal(position.qty) * pct / Decimal(100))
        if qty <= 0:
            return ExitOutcome(False, None, None, "nothing to sell")

        # `position.qty` is our ledger's arithmetic -- the buy's `filled_out` less what we
        # have sold. On any token that takes a cut on transfer, that is MORE than the
        # wallet holds, and the venue rejects the whole exit with HTTP 400. MEASURED
        # 2026-09-22: two sol positions retried for over an hour at +6.4% and +1.0% over
        # balance, and the retries took protection ticks to 16.8 s against a 5 s budget,
        # which halted entries on every chain. Clamp DOWN only: a balance we cannot read
        # changes nothing, because an exit must not become contingent on a second network
        # call.
        wallet = exit_wallet_for(position.chain)
        if wallet:
            step = time.monotonic()
            held = wallet_token_units(position.chain, wallet, position.token, decimals)
            self._step("wallet_read_ms", step)
            if held is not None and held < qty:
                if held <= 0:
                    # Flagged structurally, not by prose: the tick handler has to tell
                    # "the wallet is empty" apart from every other exit failure, and
                    # matching on a sentence is how that stops working silently.
                    return ExitOutcome(
                        False, None, None,
                        f"wallet holds none of {position.token}; refusing a zero-size sell",
                        wallet_empty=True,
                    )
                log.warning(
                    "clamping exit for %s from %d to the wallet's %d base units",
                    position.token, qty, held,
                )
                qty = held

        step = time.monotonic()
        min_out = self._min_out(position, qty, decimals, quote)
        self._step("min_out_ms", step)
        if min_out is None:
            return ExitOutcome(
                False,
                None,
                None,
                "native price unavailable; cannot compute min_out and will not send min_out=0",
            )
        slippage_bps = int(_exit_slippage_bps())
        order = executor.build_order(
            decision_id=None,
            chain=position.chain,
            token=position.token,
            side=Side.SELL,
            lane=position.lane,
            mode=position.mode,
            amount_in=qty,
            min_out=min_out,
            slippage_bps=slippage_bps,
        )
        step = time.monotonic()
        try:
            result = executor.submit(order, self.conn)
        except executor.ExecutionAmbiguous as exc:
            self._step("executor_ms", step)
            return ExitOutcome(False, OrderState.UNKNOWN, order.order_id, str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - refusals, rate limits and surprises alike
            self._step("executor_ms", step)
            return ExitOutcome(False, OrderState.FAILED, order.order_id, f"{type(exc).__name__}: {exc}"[:300])
        self._step("executor_ms", step)
        ok = result.state in {OrderState.SUBMITTED, OrderState.FILLED, OrderState.PARTIAL}
        return ExitOutcome(ok, result.state, result.order_id, result.detail or "")

    def _min_out(
        self, position: Position, qty: int, decimals: int, quote: PriceQuote
    ) -> int | None:
        """Native base units we insist on receiving. ``None`` when we cannot price it.

        Requires the native token's own USD price, which is why the price source is asked
        for ``So111…``/``0x0`` and not only for the position's token. Sending an exit with
        ``min_out=0`` because we could not do the arithmetic is an open invitation to a
        sandwich; refusing and shouting is the lesser harm.
        """
        if quote.price_usd is None or quote.price_usd <= 0:
            return None
        native_usd = self._native_usd(position.chain)
        if native_usd is None:
            return None
        tokens = Decimal(qty) / (Decimal(10) ** decimals)
        proceeds_usd = tokens * quote.price_usd
        slippage_bps = Decimal(int(_exit_slippage_bps()))
        native_units = (
            proceeds_usd / native_usd * (Decimal(10) ** NATIVE_DECIMALS[position.chain])
        )
        floor = int(native_units * (Decimal(10_000) - slippage_bps) / Decimal(10_000))
        # A WIDE tolerance must not round a real position into the refusal below. At
        # 2,500 bps the multiplier is 0.75 and only a position worth under one base
        # unit rounded away; at 8,800 it is 0.12, so positions worth several base units
        # round away too -- and the guard then refuses the exit entirely, which is the
        # opposite of what a wide exit tolerance is for. One base unit is still a real
        # floor and still not zero, so this leaves the 'never min_out=0' rule intact:
        # a position genuinely worth less than one base unit falls through and refuses.
        if floor <= 0 and native_units >= 1:
            floor = 1
        if floor <= 0:
            # `max(0, ...)` used to live here, which turned "the arithmetic rounded away"
            # into `min_out=0` -- the exact thing this function's docstring says it refuses
            # to send. A floor of zero is not a weak floor, it is NO floor: it instructs the
            # venue to accept any proceeds at all, on a sell we chose to make.
            #
            # It rounds to zero when the position is worth less than one base unit of the
            # native asset, so refusing costs us only dust, and what it buys is that the
            # one path that can sign away an unbounded amount of slippage cannot be reached
            # by accident.
            log.warning(
                "exit floor for %s on %s rounds to zero native units (qty=%s); refusing "
                "rather than sending an unprotected sell",
                position.token, position.chain.value, qty,
            )
            return None
        return floor

    def _native_usd(self, chain: Chain) -> Decimal | None:
        """What one native unit is worth, in USD. ``None`` only when we truly cannot say.

        The venue first, because it is the freshest read and it is the same source the
        rest of this exit is priced against. Then the ``native_prices`` sampler, which
        exists for exactly this question and runs every 30 s.

        WHY THE FALLBACK EXISTS. MEASURED on the live box 2026-09-22 with the shipped
        ``venue`` source: it answers the native asset on sol ($116.87) and robinhood
        ($2729.12) and NOT on bsc ("no venue curve: flap_portal_read_failed"). That is not
        a bsc quirk -- the venue source prices a token off its launch curve and the native
        asset has no launch curve, so on sol and robinhood a pool happens to answer and on
        bsc nothing does. Whether an exit can be priced was therefore decided by which
        chain it was on.

        The cost was two funded bsc positions that could be bought and not sold: 41
        consecutive ticks of ``exit_failed ... native price unavailable``, while
        ``native_prices`` held bsc at $787.98, 36 seconds old, the whole time.

        Still refuses when both are silent. ``min_out=0`` is an invitation to a sandwich
        and no amount of wanting to exit makes it safe; what changed is only that we now
        look in the place that knew the answer.
        """
        venue = self._safe_quote(chain, _native_mint(chain))
        if venue.usable and venue.price_usd is not None and venue.price_usd > 0:
            return venue.price_usd
        try:
            from kaiba.providers.native_price import at as native_price_at

            sampled = native_price_at(chain, now_ms(), self.conn)
        except Exception as exc:  # noqa: BLE001 - a missing sampler is not a price
            log.debug("native price sampler unavailable for %s: %s", chain.value, exc)
            return None
        # A sample stamped after THIS MOMENT cannot be evidence of this moment.
        #
        # The guard belongs here and not in `native_price.at`, which is a historical
        # lookup: asking what the native asset was worth at a past fill legitimately uses
        # the nearest sample on either side of that instant, and both sides are in the
        # past. Only this caller asks about *now*, and only for this caller does "later
        # than the question" mean "later than the present".
        #
        # `at` measures distance with `abs()`, so without this a row a second in the
        # future reads as a second fresh, and a stepped clock or a bad write could
        # authorize a sell floor from a price nothing has observed. Refusing costs us the
        # sampler for one tick; the venue chain above is asked first and still answers.
        sample_ts = sampled.sample_ts_ms
        if sample_ts is not None and int(sample_ts) > now_ms():
            log.warning(
                "native price sample for %s is stamped %d ms in the future; refusing it "
                "as evidence of the present",
                chain.value, int(sample_ts) - now_ms(),
            )
            return None
        price = sampled.price_usd
        if price is None or price <= 0:
            # `at` already refuses outside its tolerance AND already returns None for a
            # stored zero or negative (verified 2026-09-22), so the `<= 0` half of this is
            # redundant today and a mutation of it survives. It is kept deliberately: this
            # is the boundary that decides whether real money can leave a position, and a
            # second reader of that table should not be able to reintroduce a zero here.
            return None
        log.info(
            "%s: native price from the sampler ($%s, %sms old); the venue could not quote it",
            chain.value, price, sampled.distance_ms,
        )
        return price

    def _safe_quote(self, chain: Chain, token: str) -> PriceQuote:
        try:
            return self.price_source.quote(chain, token)
        except Exception as exc:  # noqa: BLE001 - a provider being down is not an error here
            return PriceQuote.unavailable(f"price source raised {type(exc).__name__}")


# --------------------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------------------


@dataclass
class TickReport:
    """What one pass did. This is the heartbeat payload, and it is the only way to tell a
    working watchdog from a stalled one."""

    checked: int = 0
    #: Of ``checked``, how many were LIVE/CANARY (real money). ``None`` = not counted, which
    #: :meth:`Watchdog._note_overrun` treats as live; ``tick`` always sets it.
    live_checked: int | None = None
    blind: int = 0
    #: Of the blind ones, how many are past ``protection.max_blind_s``, and the worst
    #: blind duration this tick. These two are what separates "a provider hiccuped" from
    #: "this position has had no stop since breakfast" on a single heartbeat, without
    #: anyone having to correlate events by hand.
    blind_over_budget: int = 0
    longest_blind_s: int = 0
    #: Open positions NOT examined this tick because the venue reports the wallet holds
    #: none of the token and they are on the hourly recheck. Open, owned, encumbered --
    #: just not re-priced every tick. On the heartbeat so a deferred position can never
    #: become invisible; `checked + stranded_deferred` is the whole live book.
    #:
    #: NOT `deferred`: that one already exists below and counts SHADOW QUOTE requests
    #: deferred while live protection has priority. Two different things, and folding a
    #: live position into a paper-work counter is how a stranded position would have gone
    #: unnoticed on the very heartbeat meant to keep it visible.
    stranded_deferred: int = 0
    exits: int = 0
    exit_failures: int = 0
    trims: int = 0
    errors: int = 0
    deferred: int = 0
    requests: int = 0
    duration_ms: int = 0
    #: venue-side protection (kaiba.execution.standing). `standing_unprotected` counts open
    #: positions with no live venue stop; it is only meaningful when the feature is on.
    standing_writes: int = 0
    standing_contested: int = 0
    standing_unprotected: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "live_checked": self.live_checked,
            "blind": self.blind,
            "blind_over_budget": self.blind_over_budget,
            "longest_blind_s": self.longest_blind_s,
            "exits": self.exits,
            "exit_failures": self.exit_failures,
            "trims": self.trims,
            "errors": self.errors,
            "deferred_requests": self.deferred,
            "stranded_deferred": self.stranded_deferred,
            "requests_drained": self.requests,
            "duration_ms": self.duration_ms,
            "standing_writes": self.standing_writes,
            "standing_contested": self.standing_contested,
            "standing_unprotected": self.standing_unprotected,
        }


#: Concurrent price reads per tick. 8 covers the book sizes this agent runs at while
#: keeping the socket count and the limiter's inflight cap comfortable; the limiter still
#: paces every call underneath. INVENTED as a width; the need is MEASURED (0.55 s serial
#: per position, 5.3-11.4 s ticks at 4-5 positions against a 5 s interval, 2026-09-22).
QUOTE_PREFETCH_WORKERS = 8

#: The largest remainder that may be written off as dust, as a fraction of what we bought.
#: MEASURED 2026-09-23: the position this was built for had exactly 1.0% left after a 99%
#: exit. Two percent leaves room for a taxed token's rounding without ever reaching a
#: remainder that could represent real money. INVENTED, and deliberately small: everything
#: above this line keeps retrying and pages instead.
DUST_FRACTION_MAX: float = 0.02

#: How many exit attempts must have failed before a remainder may be written off. Persisted
#: on the watchdog state, so a restart cannot reset it to zero and close something early.
DUST_MIN_ATTEMPTS: int = 3

#: How many failed exits on a WALLET-EMPTY position before it stops being serviced every
#: tick. Not a write-off: see `_defer_stranded`.
STRANDED_MIN_ATTEMPTS: int = 12

#: How often a deferred position is fully re-examined. One hour, so a token that becomes
#: sellable again is picked up within an hour at a cost of 24 ticks a day instead of 7,200.
STRANDED_RECHECK_S: int = 3600

#: Maximum shadow-only quote keys fetched on a tick that also has live inventory.
#: Live protection owns the provider budget; paper work is rotated rather than allowed to
#: stretch the tick past the protection interval.
MAX_SHADOW_QUOTE_KEYS_PER_TICK = 2

#: How long a SHADOW position may stay unpriceable before it is abandoned.
#:
#: MEASURED 2026-09-23. Three shadow `migration-fade` positions aged 20.6 h, 29.2 h and
#: 31.6 h could not be priced by any source. Each went blind and "restored" on every tick
#: -- 118 cycles in 30 minutes apiece -- and the shadow rotation kept paying for two real
#: fetches a tick, each walking the whole fallback chain to a timeout. Ticks carrying three
#: blind positions took 9,637 ms against a 5,000 ms budget; ticks carrying one took 4,722.
#: That pushed 118 of 400 ticks over budget, which re-armed `protection_overrun`, which
#: halts entries on EVERY chain. Paper positions were stopping real trading.
#:
#: An hour is far past the point of usefulness: `migration-fade`'s own lane parameters cap
#: a hold at 1,200 s, so these were 60-93x past the horizon the lane itself defines. A
#: paper position that cannot be priced is producing no learning -- only latency.
#:
#: LIVE positions are deliberately excluded. A live position that cannot be priced is
#: exactly what `protection_blind_timeout` exists to shout about, and money at risk that we
#: cannot see is never quietly written off. INVENTED as a duration.
#:
#: 2026-10-23: for a COUNTED robinhood / bsc paper position the hour is also how long it must have
#: been BLIND (the persisted blind clock), not only how old it is, and what follows is its pool's
#: answer rather than the write-off -- see ``Watchdog._settle_blind_counted_paper``. Sol and
#: uncounted rows keep the age rule, unchanged (an uncounted row is off the scoreboard either way).
SHADOW_BLIND_ABANDON_S: int = 3600

#: Consecutive IN-BUDGET ticks before a protection-overrun halt is lifted automatically.
#: 20 at the shipped 5 s interval is ~100 s of sustained health, long enough that a spike
#: has demonstrably passed and short enough that a blip does not cost a trading session.
#: MEASURED need 2026-09-22: the halt fired correctly and then held entries down for 40
#: minutes across all three chains while ticks were back at 0.8-2.1 s. INVENTED as a count.
OVERRUN_RECOVER_TICKS = 20

#: Consecutive over-interval ticks (at >= 2x the interval) before entries are halted.
#: INVENTED as a count; the need is MEASURED (see ``Watchdog._note_overrun``).
OVERRUN_HALT_TICKS = 3

#: Recovery progress an ISOLATED overrun costs, in ticks. One: it cancels one good tick.
#:
#: So recovery needs :data:`OVERRUN_RECOVER_TICKS` MORE in-budget ticks than late ones,
#: which is a ratio rule written as a counter and is the property actually wanted: a
#: watchdog that is mostly keeping up recovers, one that is late as often as not never
#: does, and no number has to be tuned to the day's latency.
#:
#: Why not zero: the halt ignores an isolated spike, so symmetry alone would make it free.
#: A late tick is still a stop checked late, and letting it cost nothing would let a
#: watchdog late on 49% of ticks clear its own halt.
#:
#: Why not more, MEASURED on the live box 2026-09-22 across two attempts at this rule:
#: 52 ticks in six minutes, 9 of them (17%) over the 5 s budget at up to 16.2 s, none
#: chronic. Resetting to zero (the original) and a penalty of 5 both net NEGATIVE at that
#: rate -- 43 - 45 = -2 -- so the entry halt on all three chains could never lift while
#: protection was healthy by its own definition. At 1 the same six minutes net +34.
#:
#: The 17% spike rate is itself a defect and is not fixed by this; it is the watchdog
#: spending a provider timeout on positions it cannot price. That is tracked separately.
OVERRUN_SPIKE_PENALTY = 1


class Watchdog:
    """One tick = load positions, drain requests, price, evaluate, act, heartbeat."""

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        price_source: PriceSource | None = None,
        prefetch_source_factory: Callable[[], PriceSource] | None = None,
        submitter: ExitSubmitter | None = None,
        risk_gate: RiskGate | None = None,
        cfg_provider: Callable[[], ProtectionConfig] = protection_config,
        blind_warn_interval_s: int = BLIND_WARN_INTERVAL_S,
        max_blind_s: int | None = None,
        halt_entries_on_blind_timeout: bool | None = None,
        standing_runner: Any = None,
        settlement_chains: Any = None,
    ) -> None:
        self.conn = conn or get_conn()
        #: Chains whose exits may only be decided on a native-settled mark. ``None`` means
        #: "re-read ``protection.settlement_mark_chains`` every tick"; a value pins it.
        self._settlement_pinned: frozenset[Chain] | None = (
            None if settlement_chains is None else frozenset(settlement_chains)
        )
        self._settlement_chains: frozenset[Chain] = (
            self._settlement_pinned
            if self._settlement_pinned is not None
            else configured_settlement_mark_chains()
        )
        #: This tick's foreign-pool marks that a native-settled mark replaced, keyed like
        #: ``_quote_cache``. Diagnostics only; cleared with the cache.
        self._set_aside: dict[tuple[Chain, str], PriceQuote] = {}
        #: When this tick's quote for a key reached the watchdog (epoch ms). Exit timing.
        self._quote_fetched_ms: dict[tuple[Chain, str], int] = {}
        #: position_id -> (last ``mark_set_aside`` event ms, the sale reason it carried).
        self._set_aside_noted: dict[str, tuple[int, str | None]] = {}
        #: Epoch ms the current tick started. Exit timing.
        self._tick_started_ms: int | None = None
        self.price_source: PriceSource = price_source or NullPriceSource()
        #: Builds one price source per prefetch WORKER THREAD. ``None`` means the workers
        #: share ``price_source``, which is only safe for a source that holds no SQLite
        #: connection of its own -- see ``_prefetch_quotes``.
        self.prefetch_source_factory = prefetch_source_factory
        self.submitter: ExitSubmitter = submitter or DefaultExitSubmitter(self.conn, self.price_source)
        self.gate = risk_gate or RiskGate()
        self._cfg_provider = cfg_provider
        #: Migration timestamps we have already looked up, keyed (chain, token). Only
        #: POSITIVE results are cached: a token that has not migrated yet may migrate while
        #: we hold it, and that is the case the cache must not hide. See ``_migrated_ms``.
        self._migration_cache: dict[tuple[str, str], int] = {}
        #: Consecutive ticks that ran longer than the poll interval. See ``_note_overrun``.
        self._overruns = 0
        #: Consecutive ticks that came in UNDER the interval. Feeds the automatic recovery
        #: from a halt this watchdog set; see ``_note_overrun``.
        self._healthy_ticks = 0
        #: This tick's prefetched prices, keyed (chain, token). Cleared every tick, so a
        #: quote can never outlive the tick that fetched it. See ``_prefetch_quotes``.
        self._quote_cache: dict[tuple[Chain, str], PriceQuote] = {}
        #: Round-robin cursor for shadow-only quote work when live positions are present.
        self._shadow_quote_cursor = 0
        #: Keys whose quote was DEFERRED this tick (never asked). Cleared with the cache.
        self._deferred_keys: set[tuple[Chain, str]] = set()
        #: Positions whose accepted quote THIS check came from a corroboration-only leaf; it is
        #: not written to quote memory. Reset per position by ``_validate_quote``.
        self._unremembered: set[str] = set()
        self._quote_deadline: float | None = None
        self.blind_warn_interval_s = int(blind_warn_interval_s)
        #: ``None`` means "re-read ``protection.max_blind_s`` every tick", like the rest of
        #: the config: an operator widening the budget during an outage should not need a
        #: restart. An explicit value pins it, which is what a caller or a test wants.
        self._max_blind_s = None if max_blind_s is None else int(max_blind_s)
        self._halt_entries_on_blind_timeout = (
            None if halt_entries_on_blind_timeout is None else bool(halt_entries_on_blind_timeout)
        )
        #: ``(position_id, blind_since_ms)`` we have already paged for. The durable latch
        #: is the event's ``dedupe_key``; this is the hot-path guard that keeps a
        #: five-second loop from attempting a doomed unique-index insert every tick. One
        #: entry per blind episode, discarded the moment a price comes back.
        self._blind_paged: set[tuple[str, int]] = set()
        #: position_id -> (blind episode ``blind_since_ms``, last pool read ms, reads so far) for a
        #: counted paper position whose blind-exit pool read learned nothing. Bounds the re-asks:
        #: :data:`PAPER_POOL_RETRIES`, :data:`PAPER_BLIND_RETRY_GAP_MS`. One entry per such position,
        #: dropped when it closes.
        self._blind_exit_reads: dict[str, tuple[int, int, int]] = {}
        #: Injection point for :mod:`kaiba.execution.standing`'s venue transport. ``None``
        #: means the real gmgn-cli runner, which is only ever reached when the feature is
        #: effective; a test passes a double.
        self.standing_runner = standing_runner

    # ------------------------------------------------------------------ helpers

    def config(self) -> ProtectionConfig:
        """Re-read every tick: ``config/risk.yaml`` is edited while this runs."""
        try:
            return self._cfg_provider()
        except Exception as exc:  # noqa: BLE001 - a bad config file must not stop protection
            log.warning("protection config unreadable, using defaults: %s", exc)
            self._emit("config_unreadable", {"error": str(exc)[:200]}, level="error")
            return ProtectionConfig()

    def _emit(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        kind: EventKind = EventKind.SYSTEM,
        level: str = "info",
        chain: Chain | None = None,
        subject: str | None = None,
        dedupe_key: str | None = None,
    ) -> int | None:
        """Returns the new event's id, or ``None`` — including when ``dedupe_key`` already
        exists, which is how the blind-timeout page stays one-per-episode across a restart."""
        body = {"service": SERVICE, "event": event, **payload}
        return ev.emit(
            kind, body, chain=chain, subject=subject, level=level, dedupe_key=dedupe_key, conn=self.conn
        )

    def blind_budget_s(self) -> int:
        """The max-blind budget in force this tick. ``<= 0`` means the failsafe is off."""
        if self._max_blind_s is not None:
            return self._max_blind_s
        return configured_max_blind_s()

    def _halt_entries_on_timeout(self) -> bool:
        if self._halt_entries_on_blind_timeout is not None:
            return self._halt_entries_on_blind_timeout
        return configured_max_blind_halt_entries()

    def _position_cfg(
        self, cfg: ProtectionConfig, state: WatchdogState, position: Position | None = None
    ) -> ProtectionConfig:
        """Fold the lane's ladder and ``kaiba_set_protection`` overrides into this position's config.

        Lane overrides (2026-10-06, owner, for ``tweet-launch``: "sell in increments, then sell
        all if there's no volume after 1 minute") live in ``risk.yaml`` under
        ``protection.lanes.<lane>`` and replace any ``ProtectionConfig`` field for that lane's
        positions only. An unreadable or invalid block leaves the global ladder in place.

        ``trail_bps`` is a single number and the config holds a tiered table, so the
        override is applied as "use this distance at every tier". That reading is a guess:
        the MCP tool's payload has no documented semantics beyond the field name.
        """
        if position is not None:
            cfg = _lane_cfg(cfg, position.lane.value)
        if state.stop_loss_bps is None and state.trail_bps is None:
            return cfg
        update: dict[str, Any] = {}
        if state.stop_loss_bps is not None:
            update["stop_loss_bps"] = int(state.stop_loss_bps)
        if state.trail_bps is not None:
            update["trailing"] = [(mult, int(state.trail_bps)) for mult, _ in cfg.trailing]
        return cfg.model_copy(update=update)

    # ------------------------------------------------------------------ drain

    def drain(self) -> tuple[dict[str, ExitRequest], dict[str, ProtectionUpdate], int, int]:
        """Read every exit request and protection change since the cursor.

        The cursor starts at 0 on a fresh install, so a close request made while the
        service was down is still honoured — the position is re-checked for being open
        before anything is sold, which is what makes replaying safe.
        """
        cursor = read_cursor(self.conn)
        exits: dict[str, ExitRequest] = {}
        updates: dict[str, ProtectionUpdate] = {}
        seen = 0
        for _ in range(DRAIN_MAX_BATCHES):
            batch = ev.tail(after_id=cursor, limit=DRAIN_BATCH, kinds=list(DRAIN_KINDS), conn=self.conn)
            if not batch:
                break
            for event in batch:
                cursor = max(cursor, int(event.id or cursor))
                seen += 1
                self._classify(event, exits, updates)
            if len(batch) < DRAIN_BATCH:
                break
        # The cursor is advanced by the caller, after the requests are durable. Losing an
        # exit request to a crash between the two is the failure this ordering prevents.
        return exits, updates, seen, cursor

    def _classify(
        self,
        event: Any,
        exits: dict[str, ExitRequest],
        updates: dict[str, ProtectionUpdate],
    ) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        source = str(payload.get("source") or "")
        if source == SERVICE:
            return  # our own exits are not requests to re-exit
        position_id = payload.get("position_id")
        kind = str(event.kind)

        if kind == EventKind.PROTECTION_TRIGGERED.value and position_id:
            request = ExitRequest(
                position_id=str(position_id),
                pct=_pct(payload.get("pct")),
                reason=str(payload.get("reason") or "agent exit request")[:200],
                sources=[source or "agent"],
                event_ids=[int(event.id or 0)],
            )
        elif (
            kind == EventKind.SYSTEM.value
            and payload.get("intent") == "close_position"
            and not payload.get("executed")
            and position_id
        ):
            request = ExitRequest(
                position_id=str(position_id),
                pct=_pct(payload.get("pct")),
                reason=str(payload.get("note") or "operator close-position intent")[:200],
                sources=[str(payload.get("requested_by") or "operator")],
                event_ids=[int(event.id or 0)],
            )
        elif kind == EventKind.PROTECTION_SET.value and position_id:
            existing = updates.get(str(position_id)) or ProtectionUpdate(str(position_id))
            if payload.get("stop_loss_bps") is not None:
                existing.stop_loss_bps = int(payload["stop_loss_bps"])
            if payload.get("trail_bps") is not None:
                existing.trail_bps = int(payload["trail_bps"])
            existing.event_ids.append(int(event.id or 0))
            updates[str(position_id)] = existing
            return
        else:
            return

        current = exits.get(request.position_id)
        if current is None:
            exits[request.position_id] = request
        else:
            current.merge(request)

    # ------------------------------------------------------------------ tick

    def settlement_chains(self) -> frozenset[Chain]:
        """Chains whose exits are decided only on native-settled marks, this tick."""
        return self._settlement_chains

    def tick(self) -> TickReport:
        started = time.monotonic()
        self._tick_started_ms = now_ms()
        report = TickReport()
        cfg = self.config()
        if self._settlement_pinned is None:
            self._settlement_chains = configured_settlement_mark_chains()
        try:
            exits, updates, seen, cursor = self.drain()
        except Exception as exc:  # noqa: BLE001 - a bad cursor must not stop protection
            log.exception("watchdog drain failed")
            self._emit("drain_failed", {"error": f"{type(exc).__name__}: {exc}"[:300]}, level="error")
            exits, updates, seen, cursor = {}, {}, 0, None
        report.requests = seen

        positions = [p for p in self._open_positions() if p.qty > 0]
        known = {p.position_id: p for p in positions}
        for position_id in set(exits) | set(updates):
            if position_id not in known:
                self._emit(
                    "exit_request_orphaned",
                    {"position_id": position_id, "reason": "no open position with that id"},
                    level="warn",
                )
        self._absorb(known, exits, updates, cfg)
        if cursor is not None:
            write_cursor(self.conn, cursor)

        # Positions the venue says we hold NONE of, already retried past
        # STRANDED_MIN_ATTEMPTS, are examined hourly instead of every tick. They stay open
        # and encumbered; this only stops them spending the provider budget live stops
        # need. See `_defer_stranded`.
        #
        # Filtered HERE, after `_absorb`, and never for a position carrying a request:
        # `known` above feeds the orphaned-exit-request check, and an operator asking for
        # an exit must reach the normal path on the very next tick, not in an hour.
        asked_for = set(exits) | set(updates)
        if positions:
            still_due = []
            for candidate in positions:
                if candidate.position_id in asked_for:
                    still_due.append(candidate)
                elif self._stranded_until(candidate) is None:
                    still_due.append(candidate)
                else:
                    report.stranded_deferred += 1
            positions = still_due

        # One concurrent round of price reads for the whole book, then the decision loop
        # runs on parked quotes. See _prefetch_quotes: serial quoting made tick latency
        # linear in position count, and a late tick is a late stop.
        try:
            interval_s = max(0.0, float(cfg.poll_interval_s or 0))
            self._quote_deadline = started + interval_s if interval_s > 0 else None
            self._prefetch_quotes(positions)
        except Exception as exc:  # noqa: BLE001 - an optimisation may never stop a tick
            log.exception("quote prefetch raised; pricing serially this tick")
            self._quote_cache = {}
            self._emit(
                "quote_prefetch_failed",
                {"error": f"{type(exc).__name__}: {exc}"[:300],
                 "note": "each position is priced serially this tick; stops still evaluate"},
                level="warn",
            )

        report.live_checked = 0
        for position in positions:
            report.checked += 1
            if position.mode is not LaneMode.SHADOW:
                report.live_checked += 1
            try:
                self._check_position(position, cfg, report)
            except Exception as exc:  # noqa: BLE001 - one bad position never stops the loop
                report.errors += 1
                log.exception("watchdog failed on position %s", position.position_id)
                self._emit(
                    "position_error",
                    {
                        "position_id": position.position_id,
                        "token": position.token,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                        "note": "position skipped this tick; protection may be stale for it",
                    },
                    level="error",
                    chain=position.chain,
                    subject=position.token,
                )

        # LOG-ONLY, after every stop has been decided: hand the RH book to the on-chain
        # pool reader, which records the pool price beside the quote just used. It never
        # feeds a decision, does its I/O on its own thread, and is a no-op unless
        # `protection.onchain_price_log` is on. See kaiba.execution.onchain_pool.
        try:
            from kaiba.execution import onchain_pool

            onchain_pool.after_tick(self, positions, cfg)
        except Exception:  # noqa: BLE001 - a log-only reader must never break a tick
            log.debug("onchain price log hook raised", exc_info=True)

        report.duration_ms = int((time.monotonic() - started) * 1000)
        self._note_overrun(report, cfg)
        self._heartbeat(report)
        return report

    def _note_overrun(self, report: TickReport, cfg: ProtectionConfig) -> None:
        """Say so when a tick took longer than the interval it was supposed to keep.

        MEASURED 2026-09-21: with curve reads at discovery priority the 5 s poll inflated
        silently as positions were added (24.95 s at N=25) and nothing paged -- every stop
        in such a tick is checked later than the operator configured, which is a late stop
        by construction. ``duration_ms`` had been in the heartbeat all along; nothing read
        it. One overrun is a warning. OVERRUN_HALT_TICKS consecutive overruns at twice the
        interval or worse means protection cannot keep up, and -- under the same operator
        switch that halts entries when a position goes blind -- new entries are halted
        until an operator resumes. Exits are never gated by this; they are what it guards.
        """
        budget_ms = int(float(cfg.poll_interval_s) * 1000)
        # A tick that checked NO live position cannot have made a stop late, and with no
        # live inventory `_prefetch_quotes` gives the paper book the whole interval BY
        # DESIGN. MEASURED 2026-10-03 12:16 CST: 8 open positions, all shadow, 19-26 s ticks
        # against 12 s -> `protection_overrun` halted live entries on every chain; halted
        # entries meant no live position, so every later tick was paper-only again and the
        # halt renewed itself. Such a tick counts as healthy, so the halt can lift; a live
        # position opened later bounds paper work again and is judged on its own ticks.
        paper_only = report.live_checked == 0
        if budget_ms <= 0 or report.duration_ms <= budget_ms or paper_only:
            if paper_only and budget_ms > 0 and report.duration_ms > budget_ms:
                self._emit(
                    "tick_overrun",
                    {
                        "duration_ms": report.duration_ms,
                        "poll_interval_ms": budget_ms,
                        "positions": report.checked,
                        "live_positions": 0,
                        "consecutive": 0,
                        "halting_entries": False,
                        "note": "paper-only tick: no live stop was checked, so none was late; "
                                "not counted toward the entry halt",
                    },
                    level="info",
                )
            self._overruns = 0
            self._healthy_ticks += 1
            if self._healthy_ticks >= OVERRUN_RECOVER_TICKS:
                # Only ever lifts a halt THIS mechanism set -- the prefix match in
                # `resume_if_reason_starts_with` is what makes that safe.
                try:
                    if self.gate.resume_if_reason_starts_with("protection_overrun:", self.conn):
                        self._emit(
                            "overrun_halt_cleared",
                            {"healthy_ticks": self._healthy_ticks,
                             "duration_ms": report.duration_ms,
                             "poll_interval_ms": budget_ms,
                             "note": "protection kept its interval again; entries resume"},
                            level="warn",
                        )
                        self._healthy_ticks = 0
                except Exception:  # noqa: BLE001 - recovery must never break a tick
                    log.exception("could not clear the protection-overrun halt")
            return
        self._overruns += 1
        chronic = self._overruns >= OVERRUN_HALT_TICKS and report.duration_ms >= 2 * budget_ms
        # Only the CHRONIC condition -- the one that halts -- wipes recovery. An isolated
        # spike costs `OVERRUN_SPIKE_PENALTY` ticks of progress; see that constant for the
        # measurement of what resetting to zero here actually did on the live box.
        if chronic:
            self._healthy_ticks = 0
        else:
            self._healthy_ticks = max(0, self._healthy_ticks - OVERRUN_SPIKE_PENALTY)
        self._emit(
            "tick_overrun",
            {
                "duration_ms": report.duration_ms,
                "poll_interval_ms": budget_ms,
                "positions": report.checked,
                "consecutive": self._overruns,
                "halting_entries": bool(chronic and self._halt_entries_on_timeout()),
                "note": "every stop in this tick was checked later than configured",
            },
            level="error" if chronic else "warn",
        )
        if chronic and self._halt_entries_on_timeout():
            try:
                self.gate.halt(
                    f"protection_overrun:{report.duration_ms}ms>{budget_ms}ms x{self._overruns}", self.conn
                )
            except Exception as exc:  # noqa: BLE001 - failing to halt entries must not stop exits
                log.exception("could not halt entries on protection overrun: %s", exc)

    def _migrated_ms(self, position: Position) -> int | None:
        """When this token migrated, or None. Cached: a migration happens once.

        A graduating launchpad token drains its bonding curve by design, which the rug
        monitor reads as a 100% liquidity drop. MEASURED 2026-09-23: 33 of 45 rug-reason
        exits were on tokens carrying a migration record. See
        ``ProtectionConfig.rug_migration_grace_s``.

        Read-only and never raises: a lookup failure means "we do not know of a migration",
        which leaves the rug monitor exactly as it was.
        """
        key = (position.chain.value, position.token)
        cached = self._migration_cache.get(key, _MISSING)
        if cached is not _MISSING:
            return cached

        def occurrence(value: Any) -> int | None:
            # Payload fields are already milliseconds; never guess units or substitute
            # ingestion time for missing occurrence evidence.
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                return None
            if isinstance(value, str) and not value.isascii():
                return None
            try:
                parsed = int(value)
            except (ValueError, TypeError):
                return None
            return parsed if 0 < parsed <= now_ms() else None

        found: int | None = None
        try:
            row = fetch_one(
                self.conn,
                "SELECT migrated_ms FROM tokens WHERE chain=? AND address=?",
                (position.chain.value, position.token),
            )
            found = occurrence(row["migrated_ms"]) if row else None
            if found is None:
                ev_rows = fetch_all(
                    self.conn,
                    "SELECT payload FROM events WHERE kind=? AND chain=? AND subject=? "
                    "ORDER BY id DESC",
                    (EventKind.TOKEN_MIGRATED.value, position.chain.value, position.token),
                )
                for ev_row in ev_rows:
                    payload = jload(ev_row["payload"], {})
                    found = occurrence(payload.get("migrated_ms")) if isinstance(payload, dict) else None
                    if found is not None:
                        break
        except sqlite3.Error:
            return None
        # Only a POSITIVE result is cached. A token we have not seen migrate yet may
        # migrate while we hold it, which is the whole case this exists for.
        if found is not None:
            self._migration_cache[key] = found
        return found

    def _last_trade_ms(self, position: Position) -> int | None:
        """When this token last traded on our tape, or None if we could not look.

        NOT cached: the whole point is to notice that trading STOPPED, and a cache of the
        last answer would freeze the very number the rule reads.

        Returning None on a failure is deliberate. `evaluate` never sells on an unknown
        last trade, so a database hiccup cannot liquidate the book -- it just leaves the
        position under the price rules it already had.
        """
        try:
            row = fetch_one(
                self.conn,
                "SELECT MAX(ts_ms) AS ts FROM swaps WHERE chain=? AND token=?",
                (position.chain.value, position.token),
            )
            # A token we launched ourselves is on nobody's tape: the swaps table only holds
            # what tracked wallets traded, so for our own fresh curve MAX(ts_ms) is NULL and
            # the silence rule could never fire. The tweet launcher watches its curves and
            # writes the last time each one moved here (kaiba/execution/tweet_launch.py).
            act = fetch_one(
                self.conn, "SELECT value FROM kv WHERE key = ?",
                (f"{TOKEN_ACTIVITY_PREFIX}{position.chain.value}:{position.token}",),
            )
        except sqlite3.Error:
            return None
        seen = [int(row["ts"])] if row and row["ts"] else []
        try:
            if act and act["value"]:
                seen.append(int(act["value"]))
        except (TypeError, ValueError):
            pass
        # A launch nobody we track trades is on no tape either. The sol pool and curve readers
        # see every reserve change, so they know when it last traded -- or, before the first
        # change, since when they have been watching (2026-10-19; pumpswap_price.last_moved_ms).
        # eth / base (2026-10-23 review, blocking #1): no wallet feed, so the tape is empty for
        # nearly every token, and a pool's mark only moves when someone trades -- a quiet token's
        # position sat between the stop and TP1 for good, holding a shadow slot every chain draws
        # from. The pool protection reads every tick keeps the same clock
        # (evm_pool_price.pool_clock).
        try:
            moved = self._reserves_moved_ms(position)
            if moved:
                seen.append(int(moved))
        except Exception as exc:  # noqa: BLE001 - unknown activity never sells
            log.debug("reserve activity unreadable for %s: %s", position.token[:12], exc)
        return max(seen) if seen else None

    def _reserves_moved_ms(self, position: Position) -> int | None:
        """When the token's pool or curve reserves last changed, surviving a restart.

        ``pumpswap_price``'s clock lives in the process, so on its own every protection restart
        started the silence over: MEASURED 2026-10-19, two launch-snipe positions 85 min silent
        went back to 5 min at the 03:40 restart and held the lane's shadow cap another hour. It
        is mirrored to ``kv`` only when it changes. Reserves identical to the stored ones and
        unchanged since this process first read them carry the stored (older) time; reserves that
        changed while nobody watched start from this process's first read -- later, never earlier.

        sol reads ``pumpswap_price.reserve_clock``; eth / base read ``evm_pool_price.pool_clock``
        (the pool protection primes every tick); every other chain has no clock and answers None.
        """
        clock = _reserve_clock(position.chain, position.token)
        if clock is None:
            return None
        reserves, first_ms, moved_ms = clock
        key = f"{RESERVE_CLOCK_PREFIX}{position.chain.value}:{position.token}"
        row = fetch_one(self.conn, "SELECT value FROM kv WHERE key = ?", (key,))
        stored = jload(row["value"], None) if row and row["value"] else None
        here = [int(x) for x in reserves]
        answer = int(moved_ms)
        if (isinstance(stored, dict) and moved_ms == first_ms and stored.get("reserves") == here
                and stored.get("moved_ms")):
            answer = min(answer, int(stored["moved_ms"]))
        record = {"reserves": here, "moved_ms": answer}
        if stored != record:
            upsert(self.conn, "kv", {"key": key, "value": jdump(record), "updated_ms": now_ms()}, ["key"])
        return answer

    def _open_positions(self) -> list[Position]:
        return open_positions(self.conn)

    def _heartbeat(self, report: TickReport) -> None:
        # A position blind for 40 minutes and a position blind for one tick both used to
        # produce the same `warn` heartbeat with the same `blind: 1`. The budget is what
        # separates them, and the separation belongs on the continuous signal as well as
        # on the one-shot page: a page can be missed, the heartbeat is always the latest
        # thing an operator or a dashboard looks at.
        if report.blind_over_budget:
            level = "error"
        elif report.blind or report.errors or report.exit_failures:
            level = "warn"
        else:
            level = "info"
        if report.blind_over_budget:
            note = (
                f"{report.blind_over_budget} position(s) unpriceable for longer than "
                f"max_blind_s; protection is NOT evaluating stops and has paged"
            )
        elif report.blind:
            note = "protection is NOT evaluating stops for these positions"
        else:
            note = None
        self._emit(
            "heartbeat",
            {
                **report.as_payload(),
                "price_source": getattr(self.price_source, "name", type(self.price_source).__name__),
                "max_blind_s": self.blind_budget_s(),
                "blind_note": note,
            },
            level=level,
        )

    # ------------------------------------------------------------------ requests

    def _absorb(
        self,
        positions: dict[str, Position],
        exits: dict[str, ExitRequest],
        updates: dict[str, ProtectionUpdate],
        cfg: ProtectionConfig,
    ) -> None:
        """Make drained requests durable before the cursor moves past them.

        A request that only lived in this tick's local variables would be lost if the
        process died before the position was processed — and a lost exit request is the
        one kind of data loss this service exists to prevent.
        """
        for position_id in set(exits) | set(updates):
            position = positions.get(position_id)
            if position is None:
                continue
            try:
                state = load_state(self.conn, position)
                update = updates.get(position_id)
                if update is not None:
                    self._apply_update(position, state, update, cfg)
                request = exits.get(position_id)
                if request is not None:
                    self._record_request(position, state, request)
                save_state(self.conn, state, mirror=False)
            except Exception as exc:  # noqa: BLE001 - one bad row never stops the drain
                log.exception("watchdog could not absorb requests for %s", position_id)
                self._emit(
                    "request_absorb_failed",
                    {"position_id": position_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
                    level="error",
                )

    def _refresh_exit(self, state: WatchdogState) -> None:
        """Fold the order table's current view of our last exit back into the state.

        This is how an ``UNKNOWN`` send is released (reconciliation resolved it) and how a
        live full exit latches: ``executor.submit`` returns ``SUBMITTED``, not ``FILLED``,
        so the latch cannot be set at submission time.
        """
        if not state.exit_order_id or state.exit_final:
            return
        row = fetch_one(self.conn, "SELECT state FROM orders WHERE order_id=?", (state.exit_order_id,))
        if row is None:
            return
        state.exit_state = str(row["state"])
        if state.exit_state == OrderState.FILLED.value and (state.exit_pct or Decimal(0)) >= 100:
            state.exit_final = True

    # ------------------------------------------------------------------ one position

    @staticmethod
    def _quote_memory_key(position: Position) -> str:
        return f"watchdog.quote:{position.position_id}:{position.chain.value}:{position.token}"

    @staticmethod
    def _quote_identity(quote: PriceQuote | None, *, liquidity: bool = False) -> tuple | None:
        if quote is None or quote.chain is None or not quote.token or quote.source == "none":
            return None
        if liquidity and (not quote.pool_id or not quote.liquidity_kind):
            return None
        return (quote.chain, quote.token, quote.source, quote.venue, quote.pool_id,
                quote.liquidity_kind if liquidity else None)

    def _quote_memory(self, position: Position) -> tuple[PriceQuote | None, PriceQuote | None]:
        price, liquidity, _ = self._read_quote_memory(position)
        return price, liquidity

    def _read_quote_memory(
        self, position: Position,
    ) -> tuple[PriceQuote | None, PriceQuote | None, dict[str, Any]]:
        """The last accepted price, the liquidity baseline, and the raw record (timing keys)."""
        row = fetch_one(self.conn, "SELECT value FROM kv WHERE key=?", (self._quote_memory_key(position),))
        raw = jload(row["value"], {}) if row else {}
        if not isinstance(raw, dict):
            raw = {}
        quotes: list[PriceQuote | None] = []
        for key in ("price", "liquidity"):
            try:
                quote = PriceQuote.model_validate(raw[key])
                # A restart baseline may be old, not future/invalid or another token.
                # A legacy liquidity scalar has NO pool identity.
                valid = (
                    quote.chain == position.chain and quote.token == position.token
                    and quote.basis not in {EvidenceBasis.UNAVAILABLE, EvidenceBasis.STALE}
                    and 0 < quote.observed_ms <= now_ms()
                    and quote.price_usd is not None and quote.price_usd.is_finite()
                    and quote.price_usd > 0
                    and (quote.liquidity_usd is None or (
                        quote.liquidity_usd.is_finite() and quote.liquidity_usd >= 0))
                )
                quotes.append(quote if valid else None)
            except (KeyError, TypeError, ValueError):
                quotes.append(None)
        return quotes[0], quotes[1], raw

    @staticmethod
    def _price_since(
        quote: PriceQuote, last_price: PriceQuote | None, memory: dict[str, Any],
    ) -> tuple[int, int | None]:
        """``(price_since_ms, prev_price_since_ms)`` for exit timing.

        When the mark's PRICE first appeared, and when the price before it first appeared.
        A feed that repeats one number for 36 s and then jumps is a late stop that no poll
        interval can fix (``onchain_pool``'s header: 61% of consecutive robinhood marks
        identical); this is how each exit records how stale its trigger price was.
        """
        def stamp(key: str) -> int | None:
            try:
                value = int(memory.get(key))
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        if last_price is not None and last_price.price_usd == quote.price_usd:
            return (stamp("price_since_ms") or int(last_price.observed_ms)), stamp("prev_price_since_ms")
        before = stamp("price_since_ms") or (int(last_price.observed_ms) if last_price else None)
        return int(quote.observed_ms), before

    def _remember_quote(
        self, position: Position, state: WatchdogState, quote: PriceQuote,
        previous: PriceQuote | None, *, price_since_ms: int | None = None,
        prev_price_since_ms: int | None = None,
    ) -> None:
        if self._quote_identity(quote) is None:
            return
        if quote.liquidity_usd is not None and self._quote_identity(quote, liquidity=True) is not None:
            previous = quote
            state.prev_liquidity_usd = quote.liquidity_usd
        record: dict[str, Any] = {
            "price": quote.model_dump(mode="json"),
            "liquidity": previous.model_dump(mode="json") if previous else None,
        }
        if price_since_ms is not None:
            record["price_since_ms"] = int(price_since_ms)
        if prev_price_since_ms is not None:
            record["prev_price_since_ms"] = int(prev_price_since_ms)
        upsert(self.conn, "kv", {
            "key": self._quote_memory_key(position),
            "value": jdump(record),
            "updated_ms": now_ms(),
        }, ["key"])

    def _note_set_aside(
        self, position: Position, state: WatchdogState, cfg: ProtectionConfig,
        foreign: PriceQuote, used: PriceQuote | None,
    ) -> None:
        """Record a foreign-pool mark we refused to decide on. Never raises, never decides.

        Written the first time the foreign mark ALONE would have sold for a given reason
        (that is the counterfactual the operator needs to count), and otherwise at most once
        a minute per position.
        """
        try:
            probe = evaluate(state.to_protection(), price_usd=foreign.price_usd,
                             executable_quote_usd=foreign.executable_quote_usd,
                             cfg=self._position_cfg(cfg, state, position))
            ts = now_ms()
            would = probe.reason if probe.sells else None
            last, last_would = self._set_aside_noted.get(position.position_id, (None, None))
            fresh_sale = would is not None and would != last_would
            if not fresh_sale and last is not None and ts - last < self.blind_warn_interval_s * 1000:
                return
            self._set_aside_noted[position.position_id] = (ts, would)
            gap = None
            if used is not None and used.price_usd and foreign.price_usd:
                gap = f"{(foreign.price_usd / used.price_usd - 1) * 100:.2f}"
            self._emit(
                "mark_set_aside",
                {
                    "position_id": position.position_id,
                    "mode": position.mode.value,
                    "foreign": {
                        "source": foreign.source, "price_usd": _s(foreign.price_usd),
                        "quote_symbol": foreign.quote_symbol, "quote_token": foreign.quote_token,
                        "pool_id": foreign.pool_id, "liquidity_usd": _s(foreign.liquidity_usd),
                        "observed_ms": foreign.observed_ms,
                    },
                    "used": None if used is None else {
                        "source": used.source, "price_usd": _s(used.price_usd),
                        "settlement": settlement_of(used), "observed_ms": used.observed_ms,
                    },
                    "foreign_vs_used_pct": gap,
                    "foreign_alone_would_sell": bool(probe.sells),
                    "foreign_alone_reason": probe.reason if probe.sells else None,
                    "blind": used is None,
                    "rule": "exits are decided only on marks that settle in the native asset",
                },
                level="warn" if probe.sells or used is None else "info",
                chain=position.chain,
                subject=position.token,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never break a tick
            log.debug("could not note a set-aside mark", exc_info=True)

    def _comparable(self, quote: PriceQuote, previous: PriceQuote | None) -> bool:
        identity = self._quote_identity(quote, liquidity=True)
        return identity is not None and identity == self._quote_identity(previous, liquidity=True)

    def _credible_depth(self, position: Position, quote: PriceQuote, previous: PriceQuote | None) -> bool:
        # Do not filter same-pool drains through a tradeability test: the rug signal
        # is precisely that the established pool may no longer be tradeable.
        if self._comparable(quote, previous):
            return True
        if quote.liquidity_usd is None:
            return True  # price-only evidence cannot establish a liquidity baseline
        if quote.liquidity_usd <= 0:
            return False
        if quote.liquidity_kind == "dex_tvl_usd":
            decimals = token_decimals(self.conn, position.chain, position.token)
            if decimals is None and position.mode is LaneMode.SHADOW:
                # A paper position's qty is denominated in the paper broker's units, not the
                # chain's: see `_paper_qty_decimals`. MEASURED 2026-10-03: 23 of 27 shadow
                # positions opened in 72 h had no recorded decimals (live: 0 of 34), so every
                # DexScreener depth was refused here, GMGN's depthless price decided their
                # exits, and the paper exit refused -- 18 of 18 then closed at -100% as
                # `abandoned_unpriceable` while the 4 with decimals exited normally.
                decimals = _paper_qty_decimals(self.conn, position)
            if decimals is None:
                return False
            size = Decimal(position.qty) / Decimal(10) ** decimals * quote.price_usd
            # Existing impact model and operator slippage bound, not invented dollar dust.
            impact = PaperBroker(self.conn).impact_bps(size, quote.liquidity_usd)
            return impact <= get_risk().bounds.max_slippage_bps
        return True

    def _alternative_sources(self, chain: Chain, root: Any = None):
        """Walk configured fallback boundaries, not a new provider stack.

        Yields in the order the chain itself would ask. ``root`` defaults to this
        watchdog's source; a prefetch worker passes its own per-thread copy.
        """
        yield from source_leaves(self.price_source if root is None else root, chain)

    def _settle(
        self, source: Any, chain: Chain, token: str, quote: PriceQuote, *, may_overrun: bool = True
    ) -> PriceQuote:
        """A native-settled mark for ``token`` when the chain's answer was a foreign pool.

        Only on a chain in :meth:`settlement_chains`, and only for a usable foreign-pool
        mark; anything else is returned untouched. The rest of the configured chain is then
        asked IN ORDER from the layer after the pool reader -- on sol that is Jupiter's
        executable quote and then GMGN; on bsc the Pancake router and then GMGN; on
        robinhood GMGN. Everything before the pool reader already refused (that is why the
        pool answered), so it is not asked twice, and no layer that would route back to
        the pool reader is asked at all.

        Returns the first usable, same-scope, non-foreign answer, remembering the foreign
        mark for diagnostics. Returns the foreign mark itself when nothing else priced;
        :meth:`_check_position` then refuses to decide on it (the position is blind this
        tick, and says why). ``may_overrun=False`` stops asking once the tick's quote
        deadline has passed -- used for shadow positions, which must never spend live
        protection's interval.
        """
        if chain not in self._settlement_chains or not quote.usable or not is_foreign_mark(quote):
            return quote
        self._set_aside[(chain, token)] = quote
        return settle_foreign_mark(
            source, chain, token, quote,
            deadline=None if may_overrun else self._quote_deadline,
        )

    def _settlement_veto(self, chain: Chain, quote: PriceQuote) -> PriceQuote | None:
        """The UNAVAILABLE stand-in for a foreign-pool mark on an enforcing chain, or None."""
        if chain not in self._settlement_chains or not quote.usable or not is_foreign_mark(quote):
            return None
        asset = quote.quote_symbol or quote.quote_token or "unreported"
        return quote.model_copy(update={
            "basis": EvidenceBasis.UNAVAILABLE,
            "note": (f"mark_not_settled_in_native: pool quoted in {asset}, and no executable, "
                     f"curve or native-pool layer priced it; " + (quote.note or ""))[:400],
        })

    def _validate_quote(
        self, position: Position, state: WatchdogState, cfg: ProtectionConfig,
        quote: PriceQuote, last_price: PriceQuote | None, previous: PriceQuote | None,
    ) -> PriceQuote:
        self._unremembered.discard(position.position_id)
        if not quote.usable:
            return quote
        if ((quote.chain is not None and quote.chain != position.chain)
                or (quote.token is not None and quote.token != position.token)):
            return quote.model_copy(update={"basis": EvidenceBasis.UNAVAILABLE, "note": "quote_scope_mismatch"})
        identity = self._quote_identity(quote)
        established = identity is not None and identity == self._quote_identity(last_price)
        depth_ok = self._credible_depth(position, quote, previous)
        probe = evaluate(state.to_protection(), price_usd=quote.price_usd,
                         executable_quote_usd=quote.executable_quote_usd,
                         cfg=self._position_cfg(cfg, state, position))
        suspect_stop = (not established and probe.kind is ProtectionKind.EXIT_ALL
                        and (identity is not None or last_price is not None))
        # The mirror of a suspect stop, and NOT excused by comparable liquidity: the incident
        # pool's depth figure rose with its own wrong price (352,214 at $0.8525, 392,723 at
        # $0.9948), so "deep" was no evidence (see SUSPECT_JUMP_X). It guards the peak and the
        # trailing stop as well as the ladder, since both read this mark.
        reference = last_price.price_usd if last_price is not None else state.entry_price_usd
        suspect_jump = (not established and reference is not None and reference > 0
                        and quote.price_usd >= reference * SUSPECT_JUMP_X)
        if depth_ok and not suspect_jump and (not suspect_stop or self._comparable(quote, previous)):
            return quote
        # Ordinary leaves first, in the chain's own order; a corroboration-only leaf (the sampled
        # Codex backup, about one call an hour) last, so its scarce call is spent only when no
        # free independent leaf could corroborate. See `corroboration_only`.
        leaves = list(self._alternative_sources(position.chain))
        ordered = ([s for s in leaves if not corroboration_only(s)]
                   + [s for s in leaves if corroboration_only(s)])
        for source in ordered:
            # Re-asking the rejected leaf is not independent corroboration.
            if isinstance(source, ProviderPriceSource) and quote.source.startswith("prices:"):
                continue
            if isinstance(source, GmgnPriceSource) and quote.source.startswith("gmgn:"):
                continue
            try:
                alternative = source.quote(position.chain, position.token)
            except Exception:  # noqa: BLE001 - a refusal, never a sell signal
                continue
            if not isinstance(alternative, PriceQuote) or not alternative.usable:
                continue
            if position.chain in self._settlement_chains and is_foreign_mark(alternative):
                continue  # corroboration must meet the same bar as the mark it replaces
            if alternative.chain != position.chain or alternative.token != position.token:
                continue
            if self._quote_identity(alternative) == identity:
                continue
            if quote.pool_id and alternative.pool_id == quote.pool_id:
                continue
            if self._credible_depth(position, alternative, previous):
                if corroboration_only(source):
                    self._unremembered.add(position.position_id)
                return alternative
        return quote.model_copy(update={
            "basis": EvidenceBasis.UNAVAILABLE,
            "note": "quote_uncorroborated_source_transition; " + (quote.note or ""),
        })

    def _check_position(
        self,
        position: Position,
        cfg: ProtectionConfig,
        report: TickReport,
    ) -> None:
        state = load_state(self.conn, position)
        self._refresh_exit(state)

        entry = state.entry_price_usd or position.entry_price_usd
        state.entry_price_usd = entry
        if entry is None or entry <= 0:
            report.blind += 1
            self._blind(position, state, "entry_price_unavailable", report)
            save_state(self.conn, state)
            return

        last_price, previous, memory = self._read_quote_memory(position)
        original = self._quote(position)
        # A foreign-pool mark never reaches `evaluate` on an enforcing chain: when no
        # settled layer priced it, the position is blind this tick and says why.
        vetoed = self._settlement_veto(position.chain, original)
        quote = (
            vetoed if vetoed is not None
            else self._validate_quote(position, state, cfg, original, last_price, previous)
        )
        set_aside = self._set_aside.get((position.chain, position.token))
        state.quote_evidence = {
            "quote_provenance": quote.model_dump(mode="json"),
            "previous_liquidity_usd": _s(previous.liquidity_usd if previous else state.prev_liquidity_usd),
            "previous_liquidity_provenance": previous.model_dump(mode="json") if previous else None,
            "liquidity_comparable": self._comparable(quote, previous),
            "rejected_quote": original.model_dump(mode="json") if quote is not original else None,
            "settlement": settlement_of(original if vetoed is not None else quote),
        }
        if set_aside is not None and set_aside is not original:
            state.quote_evidence["set_aside_quote"] = set_aside.model_dump(mode="json")
        if set_aside is not None:
            self._note_set_aside(position, state, cfg, set_aside, None if vetoed is not None else quote)
        if not quote.usable:
            # A PAPER position nobody can price is latency, not learning. Checked before
            # the blind bookkeeping so an abandoned one stops costing the rotation a real
            # fetch on the very next tick. Live positions are never eligible -- see
            # `_abandon_unpriceable_shadow`.
            #
            # NOT for a settlement veto: that position HAS a price, just not one an exit
            # may be decided on, and a shadow walk cut short by the tick deadline must not
            # close a paper position that the next tick would have priced.
            if vetoed is None and self._abandon_unpriceable_shadow(position, quote, state):
                return
            report.blind += 1
            self._blind(position, state, (quote.invalid_reason or "price_unavailable") + "; "
                        + (quote.note or ""), report, quote=quote)
            save_state(self.conn, state)
            # An hourly recheck that cannot price must stay hourly. The deferral used to be
            # renewed only from `_do_exit`, which needs a usable quote, so an unpriceable
            # recheck dropped the position back onto every tick.
            self._extend_stranded(position)
            # Being blind is exactly when a venue-side stop earns its keep, and the mirror
            # needs no price of its own: the ladder's triggers come from the entry and the
            # ratchet, both of which are already in `state`.
            self._sync_standing(position, state, cfg, report)
            return

        # AUDIT-INTEGRATE delegated hook: only the accepted quote is a sample.
        from kaiba.execution.excursions import record_live_quote

        record_live_quote(self.conn, position, quote)

        if state.blind_since_ms is not None:
            blind_for_s = max(0, (now_ms() - state.blind_since_ms) // 1000)
            budget_s = self.blind_budget_s()
            # A price resets the clock, and resetting the clock has to reset the page as
            # well: the next blind episode gets a new `blind_since_ms`, so it is a new
            # dedupe key and a new page. Without this discard the in-process latch would
            # leak one tuple per episode for the life of the service.
            self._blind_paged.discard((position.position_id, int(state.blind_since_ms)))
            self._emit(
                "protection_restored",
                {
                    "position_id": position.position_id,
                    "blind_for_s": blind_for_s,
                    "max_blind_s": budget_s,
                    "was_over_budget": bool(budget_s > 0 and blind_for_s >= budget_s),
                    "basis": quote.basis.value,
                },
                chain=position.chain,
                subject=position.token,
            )
            state.blind_since_ms = None
            state.last_blind_warn_ms = None


        # Lift the blind halt whenever this position prices, not only on the tick it
        # stops being blind. MEASURED 2026-09-22: placing it inside the blind->priced
        # transition looked right and did not work. `blind_since_ms` lives in memory, so a
        # service restart clears it; a position that was already healthy when the process
        # came up never transitions, and the halt it set outlived it with nothing able to
        # lift it. The halt is durable and the state that would clear it is not.
        #
        # Cheap to attempt every tick: `resume_if_reason_starts_with` returns False
        # immediately unless the stored reason names THIS position, so it is one string
        # comparison on the overwhelming majority of ticks.
        #
        # Narrow on purpose. The prefix carries the position id, so this can only undo a
        # halt this mechanism set FOR THIS POSITION -- never an operator halt, never the
        # daily loss stop, never another position's.
        try:
            if self.gate.resume_if_reason_starts_with(
                f"protection_blind_timeout:{position.position_id}", self.conn
            ):
                self._emit(
                    "blind_halt_cleared",
                    {
                        "position_id": position.position_id,
                        "basis": quote.basis.value,
                        "note": "this position can be priced again; entries resume",
                    },
                    level="warn",
                    chain=position.chain,
                    subject=position.token,
                )
        except Exception:  # noqa: BLE001 - recovery must never break a tick
            log.exception("could not clear the protection-blind halt")

        protection = state.to_protection()
        comparable = self._comparable(quote, previous)
        price_since_ms, prev_price_since_ms = self._price_since(quote, last_price, memory)
        action = evaluate(
            protection,
            price_usd=quote.price_usd,
            liquidity_usd=quote.liquidity_usd,
            prev_liquidity_usd=previous.liquidity_usd if comparable else None,
            executable_quote_usd=quote.executable_quote_usd,
            # A token-wide migration waiver cannot hide a SAME established-pool drain.
            migrated_ms=None if comparable else self._migrated_ms(position),
            last_trade_ms=self._last_trade_ms(position),
            cfg=self._position_cfg(cfg, state, position),
        )
        decided_ms = now_ms()
        state.absorb(protection)
        if self._quote_identity(quote) != self._quote_identity(last_price):
            self._emit("quote_source_changed", {
                "position_id": position.position_id, **state.quote_evidence,
            }, chain=position.chain, subject=position.token)
        # A corroboration-only answer (the sampled Codex backup) decides THIS tick and is never
        # remembered: it cannot answer the next tick, so as the identity it would make the next
        # ordinary stop a suspect one with nothing left to corroborate it (`corroboration_only`).
        if position.position_id not in self._unremembered:
            self._remember_quote(position, state, quote, previous, price_since_ms=price_since_ms,
                                 prev_price_since_ms=prev_price_since_ms)

        pct, reason = self._combine(action.pct if action.sells else None, action.reason, state)
        if pct is None:
            save_state(self.conn, state)
            self._sync_standing(position, state, cfg, report)
            return
        timing = {
            "tick_started_ms": self._tick_started_ms,
            "mark_observed_ms": int(quote.observed_ms),
            "mark_fetched_ms": self._quote_fetched_ms.get((position.chain, position.token)),
            "previous_mark_observed_ms": int(last_price.observed_ms) if last_price else None,
            "price_since_ms": price_since_ms,
            "prev_price_since_ms": prev_price_since_ms,
            "decided_ms": decided_ms,
            "poll_interval_ms": int(float(cfg.poll_interval_s or 0) * 1000),
        }
        # Paper only: the price that decided stays the price; depth comes from a measured
        # pool read of this tick when the deciding quote carries none. Live passes `quote`.
        exit_quote = self._with_paper_depth(position, state, quote, original, previous)
        self._do_exit(position, state, pct, reason, exit_quote, action.kind, report, timing=timing)
        save_state(self.conn, state)
        # After a trim the ladder has changed, so the mirror is stale until this runs.
        self._sync_standing(position, state, cfg, report, force=True)

    def _with_paper_depth(
        self, position: Position, state: WatchdogState, quote: PriceQuote,
        measured: PriceQuote | None, previous: PriceQuote | None,
    ) -> PriceQuote:
        """``quote`` carrying this tick's MEASURED pool depth, for a paper exit only.

        MEASURED 2026-10-03 on the box: all 255 ``exit_failed`` events in an 18.4 h read were
        paper exits refused for depth, each decided on GMGN's price (no depth by design),
        and in 239 of 251 (an earlier read) the same tick's DexScreener read of the token --
        e.g. HOOKR's uniswap pool, $1,137,821 -- sat in ``rejected_quote``. ``_validate_quote`` swaps the pool mark for a corroborating source
        when a stop first appears on a source the position was not already following, and
        the corroboration is what decides. That is right for the decision; it left the paper
        broker with nothing to model impact against, and the position retried forever.

        The depth lent here is not a guess, and every condition is load-bearing:

        * **shadow only, and only when the deciding quote has no depth.** A live exit is
          sized from the wallet and ``min_out``; it never reads this.
        * **this tick's read of this token.** ``measured`` is the tick's own quote for the
          position (``_quote``), never memory, and must name its chain, token and pool.
        * **fresh by the bar every stop uses.** ``measured.usable`` -- its own freshness
          budget, capped at ``prices.DEFAULT_MAX_AGE_S`` -- is re-checked at the moment of
          the exit, and its age is recorded.
        * **credible for our size.** ``_credible_depth`` -- the same test a pool mark must
          pass to decide anything: our size trades through it inside the slippage bound.

        The price is unchanged; only ``liquidity_usd`` and the pool's identity are attached,
        and ``quote_evidence["paper_exit_depth"]`` records where they came from.
        """
        if position.mode is not LaneMode.SHADOW or quote.liquidity_usd is not None:
            return quote
        if measured is None or not measured.usable:
            return quote
        if measured.liquidity_usd is None or measured.liquidity_usd <= 0:
            return quote
        if not measured.pool_id or not measured.liquidity_kind:
            return quote
        if measured.chain != position.chain or measured.token != position.token:
            return quote
        if not self._credible_depth(position, measured, previous):
            return quote
        state.quote_evidence["paper_exit_depth"] = {
            "source": measured.source,
            "venue": measured.venue,
            "pool_id": measured.pool_id,
            "liquidity_kind": measured.liquidity_kind,
            "liquidity_usd": _s(measured.liquidity_usd),
            "observed_ms": int(measured.observed_ms),
            "age_ms": now_ms() - int(measured.observed_ms),
            "price_source": quote.source,
            "rule": "paper impact modelled against this tick's measured pool depth; "
                    "the deciding price is unchanged",
        }
        return quote.model_copy(update={
            "liquidity_usd": measured.liquidity_usd,
            "liquidity_kind": measured.liquidity_kind,
            "pool_id": measured.pool_id,
            "venue": measured.venue,
        })

    def _combine(
        self, action_pct: Decimal | None, action_reason: str, state: WatchdogState
    ) -> tuple[Decimal | None, str]:
        """One exit per position per tick, whether one source asked or three did."""
        pending = state.pending_pct
        if pending is None and action_pct is None:
            return None, action_reason
        if pending is None:
            return action_pct, action_reason
        if action_pct is None:
            return pending, f"request:{state.pending_source}:{state.pending_reason or ''}"[:200]
        return (
            max(pending, action_pct),
            f"{action_reason}+request:{state.pending_source}"[:200],
        )

    def _record_request(
        self, position: Position, state: WatchdogState, request: ExitRequest
    ) -> None:
        if state.pending_pct is None:
            state.pending_pct = request.pct
            state.pending_ms = now_ms()
        else:
            state.pending_pct = max(state.pending_pct, request.pct)
        state.pending_reason = request.reason
        state.pending_source = request.source_label
        self._emit(
            "exit_request_accepted",
            {
                "position_id": position.position_id,
                "pct": str(request.pct),
                "sources": request.sources,
                "event_ids": request.event_ids,
                "reason": request.reason,
            },
            chain=position.chain,
            subject=position.token,
        )

    def _apply_update(
        self,
        position: Position,
        state: WatchdogState,
        update: ProtectionUpdate,
        cfg: ProtectionConfig,
    ) -> None:
        applied: dict[str, Any] = {}
        if update.stop_loss_bps is not None:
            state.stop_loss_bps = update.stop_loss_bps
            applied["stop_loss_bps"] = update.stop_loss_bps
        if update.trail_bps is not None:
            state.trail_bps = update.trail_bps
            applied["trail_bps"] = update.trail_bps
        if not applied:
            return
        entry = state.entry_price_usd or position.entry_price_usd
        ignored = None
        if update.stop_loss_bps is not None and entry and state.stop_price_usd is not None:
            requested = entry * (Decimal(1) - Decimal(update.stop_loss_bps) / Decimal(10_000))
            if requested < state.stop_price_usd:
                # protection.next_stop only ever raises a stop, so a wider stop is a no-op.
                # Saying so is better than letting the caller believe it took effect.
                ignored = (
                    f"stop_loss_bps={update.stop_loss_bps} implies {requested}, below the "
                    f"current ratcheted stop {state.stop_price_usd}; the ratchet does not lower a stop"
                )
        self._emit(
            "protection_updated",
            {
                "position_id": position.position_id,
                "applied": applied,
                "event_ids": update.event_ids,
                "ignored": ignored,
                "cfg_stop_loss_bps": cfg.stop_loss_bps,
            },
            level="warn" if ignored else "info",
            chain=position.chain,
            subject=position.token,
        )

    # ------------------------------------------------------------- venue protection

    def _standing(self) -> Any:
        """Import :mod:`kaiba.execution.standing` lazily, or ``None`` if it is absent.

        Lazy and guarded for the same reason the price provider is: this loop must keep
        running, loudly, when a neighbouring module is missing or broken. A protection
        mirror that can take the watchdog down is worse than no mirror.
        """
        try:
            from kaiba.execution import standing
        except Exception as exc:  # noqa: BLE001 - an absent mirror is not an outage
            log.debug("standing module unavailable: %s", type(exc).__name__)
            return None
        return standing

    def _sync_standing(
        self,
        position: Position,
        state: WatchdogState,
        cfg: ProtectionConfig,
        report: TickReport,
        *,
        force: bool = False,
    ) -> None:
        """Keep the venue's standing orders in line with this position's ladder.

        A no-op — no database write, no subprocess — whenever ``use_provider_orders`` is
        not *effectively* on, which is the shipped default. ``standing.sync_position``
        does its own rate limiting; this is called every tick and writes far less often.
        """
        standing = self._standing()
        if standing is None:
            return
        if state.exit_final or (state.exit_state or "") in UNRESOLVED_ORDER_STATES:
            # An exit we have already sent owns these tokens. Re-placing a venue order on
            # top of it would recreate the double-sell the claim just removed, and the
            # quantity it would be sized against is about to change. The next tick after
            # the order resolves re-arms whatever the ladder still wants.
            return
        try:
            result = standing.sync_position(
                position,
                state.to_protection(),
                conn=self.conn,
                runner=self.standing_runner,
                cfg=cfg,
                # Lazy: the native price is only fetched if a placement is actually going
                # to happen and the operator declared the venue speaks quote-token prices.
                native_price_usd=lambda: self._native_price(position.chain),
                force=force,
            )
        except Exception as exc:  # noqa: BLE001 - protection bookkeeping never stops the loop
            log.exception("standing sync failed for %s", position.position_id)
            self._emit(
                "standing_sync_error",
                {
                    "position_id": position.position_id,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "impact": "venue-side protection may be stale for this position",
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return
        report.standing_writes += result.writes
        if result.unprotected_reason:
            report.standing_unprotected += 1

    def _claim_standing(
        self,
        position: Position,
        pct: Decimal,
        quote: PriceQuote,
        reason: str,
        report: TickReport,
    ) -> None:
        """Take ownership of the quantity we are about to sell. Never blocks the exit."""
        standing = self._standing()
        if standing is None:
            return
        try:
            claim = standing.claim_for_exit(
                position.position_id,
                chain=position.chain,
                token=position.token,
                pct=pct,
                price_usd=quote.price_usd,
                reason=reason,
                conn=self.conn,
                runner=self.standing_runner,
            )
        except Exception as exc:  # noqa: BLE001 - an exit is never held up by the mirror
            log.exception("standing claim failed for %s", position.position_id)
            self._emit(
                "standing_claim_error",
                {
                    "position_id": position.position_id,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "note": "selling anyway; exits are never gated",
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return
        if claim.contested:
            report.standing_contested += len(claim.contested)

    def _native_price(self, chain: Chain) -> Decimal | None:
        """The chain's own token in USD, or ``None``. Never 0, never a guess."""
        try:
            found = self.price_source.quote(chain, _native_mint(chain))
        except Exception as exc:  # noqa: BLE001 - a dead provider is missing data
            log.warning("native price source raised for %s: %s", chain.value, exc)
            return None
        if not isinstance(found, PriceQuote) or not found.usable:
            return None
        return found.price_usd

    # ------------------------------------------------------------------ price

    def _prefetch_quotes(self, positions: list[Position]) -> None:
        """Fetch live protection quotes first, then bounded paper work.

        Live/canary positions own the protection budget. Shadow positions remain useful for
        paper learning, but they must not start network work before a live stop has priced or
        consume the remaining tick budget after a slow live route. Deferred shadow keys are
        cached as unavailable for this tick, so ``_quote`` cannot silently fall back to a
        second serial provider call.
        """
        self._quote_cache = {}
        self._set_aside = {}
        self._quote_fetched_ms = {}
        self._deferred_keys = set()
        unique = {(pos.chain, pos.token) for pos in positions}
        if len(unique) < 2:
            return

        live_keys = sorted(
            {(pos.chain, pos.token) for pos in positions if pos.mode is not LaneMode.SHADOW},
            key=lambda key: (key[0].value, key[1]),
        )
        shadow_keys = sorted(
            unique - set(live_keys),
            key=lambda key: (key[0].value, key[1]),
        )
        factory = self.prefetch_source_factory
        worker = threading.local()

        def source() -> PriceSource:
            if factory is None:
                return self.price_source
            built = getattr(worker, "source", None)
            if built is None:
                built = factory()
                worker.source = built
            return built

        live_set = set(live_keys)

        def one(key: tuple[Chain, str]) -> tuple[tuple[Chain, str], PriceQuote]:
            chain, token = key
            here = source()
            try:
                got = here.quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - one dead quote is blindness for one position
                return key, PriceQuote.unavailable(f"price source raised {type(exc).__name__}")
            if not isinstance(got, PriceQuote):
                return key, PriceQuote.unavailable(f"price source returned {type(got).__name__}")
            # Same worker, same per-thread source: the settlement layers run concurrently
            # with the rest of the book instead of serially in the decision loop.
            got = self._settle(here, chain, token, got, may_overrun=key in live_set)
            self._quote_fetched_ms[key] = now_ms()
            return key, got

        def fetch(keys: list[tuple[Chain, str]]) -> None:
            if not keys:
                return
            self._prime_sources(keys)
            if len(keys) == 1:
                key, quote = one(keys[0])
                self._quote_cache[key] = quote
                return
            workers = min(QUOTE_PREFETCH_WORKERS, len(keys))
            try:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kaiba-quote") as pool:
                    for key, quote in pool.map(one, keys):
                        self._quote_cache[key] = quote
            except Exception as exc:  # noqa: BLE001 - optimization failure must not stop protection
                log.warning("quote prefetch failed, falling back to serial: %s", exc)
                for key in keys:
                    if key not in self._quote_cache:
                        key, quote = one(key)
                        self._quote_cache[key] = quote

        def defer(keys: list[tuple[Chain, str]]) -> None:
            for key in keys:
                self._deferred_keys.add(key)
                self._quote_cache[key] = PriceQuote.unavailable(
                    "shadow quote deferred while live protection has priority", source="watchdog"
                )

        # No live inventory: preserve the full shadow stream for paper learning.
        if not live_keys:
            fetch(shadow_keys)
            return

        # The live phase completes before any shadow quote can start.
        fetch(live_keys)
        if not shadow_keys:
            return

        # A slow live route consumed the tick budget: defer all paper work this tick.
        if self._quote_deadline is not None and time.monotonic() >= self._quote_deadline:
            defer(shadow_keys)
            return

        count = min(MAX_SHADOW_QUOTE_KEYS_PER_TICK, len(shadow_keys))
        start_shadow = self._shadow_quote_cursor % len(shadow_keys)
        selected = [shadow_keys[(start_shadow + i) % len(shadow_keys)] for i in range(count)]
        self._shadow_quote_cursor = (start_shadow + count) % len(shadow_keys)
        selected_set = set(selected)
        defer([key for key in shadow_keys if key not in selected_set])
        fetch(selected)

    def _prime_sources(self, keys: list[tuple[Chain, str]]) -> int:
        """Let every routed leaf that can batch read this tick's tokens in ONE call per chain.

        2026-10-21 (design step 3a [R]): ``_prefetch_quotes`` quotes each ``(chain, token)`` on
        its own worker, so a source that batches internally still received N calls. A leaf with
        a ``prime(chain, tokens)`` method (``evm_pool_price.PoolPriceSource``) is handed the
        tick's tokens for its chain first, reads them in one ``aggregate3``, and answers the
        workers' ``quote`` calls from that read. Leaves without ``prime`` are untouched. Returns
        how many leaves were primed. Never raises: a failed prime leaves each ``quote`` to read
        alone, which is today's behaviour.
        """
        by_chain: dict[Chain, list[str]] = {}
        for chain, token in keys:
            by_chain.setdefault(chain, []).append(token)
        root: Any = self.price_source
        if isinstance(root, NullPriceSource) and self.prefetch_source_factory is not None:
            try:
                root = self.prefetch_source_factory()
            except Exception:  # noqa: BLE001 - no root, no prime
                return 0
        primed = 0
        for chain, tokens in by_chain.items():
            try:
                leaves = list(source_leaves(root, chain))
            except Exception:  # noqa: BLE001
                continue
            seen: set[int] = set()
            for leaf in leaves:
                prime = getattr(leaf, "prime", None)
                if not callable(prime) or id(leaf) in seen:
                    continue
                seen.add(id(leaf))
                try:
                    prime(chain, list(tokens))
                    primed += 1
                except Exception:  # noqa: BLE001 - an optimisation may never stop a tick
                    log.debug("price source prime raised on %s", chain.value, exc_info=True)
        return primed

    def _quote(self, position: Position) -> PriceQuote:
        cached = self._quote_cache.get((position.chain, position.token))
        if cached is not None:
            return cached
        try:
            quote = self.price_source.quote(position.chain, position.token)
        except Exception as exc:  # noqa: BLE001 - a dead provider is blindness, not a crash
            log.warning("price source raised for %s: %s", position.token, exc)
            return PriceQuote.unavailable(f"price source raised {type(exc).__name__}")
        if not isinstance(quote, PriceQuote):
            return PriceQuote.unavailable(f"price source returned {type(quote).__name__}")
        quote = self._settle(
            self.price_source, position.chain, position.token, quote,
            may_overrun=position.mode is not LaneMode.SHADOW,
        )
        self._quote_fetched_ms[(position.chain, position.token)] = now_ms()
        return quote

    def _blind(
        self,
        position: Position,
        state: WatchdogState,
        reason: str,
        report: TickReport,
        *,
        quote: PriceQuote | None = None,
    ) -> None:
        """Say it loudly, on a throttle, escalate once past the budget, and never pretend
        the position is protected."""
        ts = now_ms()
        if state.blind_since_ms is None:
            state.blind_since_ms = ts
        if state.pending_pct is not None:
            report.deferred += 1

        blind_for_s = int(max(0, (ts - state.blind_since_ms) // 1000))
        budget_s = self.blind_budget_s()
        over_budget = budget_s > 0 and blind_for_s >= budget_s
        # Counted on every tick, deliberately *above* the warn throttle. The throttle is
        # about not spamming identical events; the heartbeat's counters are about the
        # operator who looks at one tick and has to be told the truth on that tick.
        report.longest_blind_s = max(report.longest_blind_s, blind_for_s)
        if over_budget:
            report.blind_over_budget += 1
            self._blind_timeout(position, state, reason, blind_for_s, budget_s, quote=quote)

        due = (
            state.last_blind_warn_ms is None
            or (ts - state.last_blind_warn_ms) >= self.blind_warn_interval_s * 1000
        )
        if not due:
            return
        state.last_blind_warn_ms = ts
        self._emit(
            "protection_blind",
            {
                "position_id": position.position_id,
                "token": position.token,
                "lane": position.lane.value,
                "mode": position.mode.value,
                "basis": EvidenceBasis.UNAVAILABLE.value,
                "reason": reason,
                "price_source": getattr(
                    self.price_source, "name", type(self.price_source).__name__
                ),
                "quote_source": quote.source if quote else None,
                "blind_for_s": blind_for_s,
                "max_blind_s": budget_s,
                "over_budget": over_budget,
                "stops_evaluated": False,
                **state.quote_evidence,
                "deferred_exit_pct": _s(state.pending_pct),
                "impact": "this position has NO working stop loss right now",
            },
            level="error",
            chain=position.chain,
            subject=position.token,
        )

    def _blind_timeout(
        self,
        position: Position,
        state: WatchdogState,
        reason: str,
        blind_for_s: int,
        budget_s: int,
        *,
        quote: PriceQuote | None = None,
    ) -> None:
        """Past the budget, warning again is not a response. Page once, and stop taking on
        exposure the protection layer has just been proved not to cover.

        **Why this pages rather than forcing an exit.** A forced exit on a token we cannot
        price is not the safe option, for three reasons, and the first one settles it:

        1. *It cannot be sent.* Both exit paths need the price we do not have.
           :meth:`DefaultExitSubmitter._paper` refuses without a price *and* a liquidity
           reading; :meth:`DefaultExitSubmitter._live` computes ``min_out`` from the token
           and native quotes and returns ``None`` rather than send ``min_out=0``. A
           "forced" exit from here would be an ``exit_failed`` event every tick behind an
           exponential backoff — the same warning loop under a more reassuring name, plus
           a burnt ``exit_attempts`` counter.
        2. *If it could be sent it would be the thing we refuse elsewhere.* A market sell
           with no floor, into a book nobody can see, is exactly the sandwich ``min_out``
           exists to prevent. Fail closed means refusing the trade, not inventing a price
           to justify one.
        3. *Blind is a shipped state.* :class:`NullPriceSource` is a supported setting and
           ``price_source: curve`` is blind on every EVM chain today, so "past the budget,
           sell" would make one misconfigured key liquidate the whole book on a timer.

        So the action is the one that lowers future harm without trading blind: page the
        operator once per blind episode, and — only for a position holding real tokens —
        pull the entry brake. The brake is :meth:`kaiba.execution.risk.RiskGate.halt`,
        which stops entries and leaves exits alone (``check_exit`` always allows, and
        returns ``exit_never_blocked``), so it can never strand the position it was
        raised to protect. The operator clears it with ``kaiba risk resume`` once the
        price source actually covers the token.
        """
        if state.blind_since_ms is None:  # pragma: no cover - the caller always sets it
            return
        key = (position.position_id, int(state.blind_since_ms))
        if key in self._blind_paged:
            return

        # Anything the watchdog would route to the real executor is treated as real
        # tokens, which is `DefaultExitSubmitter.submit_exit`'s own rule: shadow goes to
        # the paper broker, everything else goes to `executor.submit`. A shadow position
        # never had tokens, so its blindness is a data-quality problem and must not be
        # allowed to brake a live book.
        real_tokens = position.mode is not LaneMode.SHADOW
        # A position the venue has already reported as wallet-empty holds nothing a stop
        # could protect. MEASURED 2026-10-01: pos_9cb9's hourly recheck could not price a
        # dead token, the blind clock ran out, and entries stopped on EVERY chain for 5 h
        # -- the fourth such halt from that one position. It still pages; it only stops
        # braking the book.
        venue_empty = real_tokens and self._stranded_marker(position) is not None
        halt_wanted = real_tokens and not venue_empty and self._halt_entries_on_timeout()

        event_id = self._emit(
            "protection_blind_timeout",
            {
                "position_id": position.position_id,
                "token": position.token,
                "lane": position.lane.value,
                "mode": position.mode.value,
                "basis": EvidenceBasis.UNAVAILABLE.value,
                "reason": reason,
                "blind_for_s": blind_for_s,
                "max_blind_s": budget_s,
                "blind_since_ms": state.blind_since_ms,
                "price_source": getattr(
                    self.price_source, "name", type(self.price_source).__name__
                ),
                "quote_source": quote.source if quote else None,
                "stop_price_usd": _s(state.stop_price_usd),
                "deferred_exit_pct": _s(state.pending_pct),
                "real_tokens": real_tokens,
                "venue_reported_empty": venue_empty,
                "forced_exit": False,
                "forced_exit_refused": (
                    "we cannot price this token, so a sell would be a market order with no "
                    "min_out into a book we cannot see; the submitter would refuse it anyway"
                ),
                "entries_halt_requested": halt_wanted,
                "impact": (
                    f"this position has had NO working stop loss for {blind_for_s}s "
                    f"(budget {budget_s}s) and the watchdog cannot close it"
                ),
                "operator_action": (
                    "give protection.price_source coverage for this token or close the "
                    "position by hand, then `kaiba risk resume` if entries were halted"
                ),
            },
            level="error",
            chain=position.chain,
            subject=position.token,
            # Durable, one page per blind episode, across restarts: `dedupe_key` is a
            # unique index and `blind_since_ms` is persisted with the episode.
            dedupe_key=f"{SERVICE}:blind_timeout:{position.position_id}:{state.blind_since_ms}",
        )
        self._blind_paged.add(key)
        if event_id is None:
            # The row did not land: either an earlier process already paged for this exact
            # episode — in which case it already pulled the brake — or the bus refused the
            # write. Re-halting on the first guess would fight an operator who has since
            # resumed, so this stops here and says so in the log.
            log.warning(
                "blind-timeout page for %s was not recorded (duplicate or bus error); "
                "not re-applying the entry brake",
                position.position_id,
            )
            return
        if not halt_wanted:
            return
        try:
            self.gate.halt(f"protection_blind_timeout:{position.position_id}", self.conn)
        except Exception as exc:  # noqa: BLE001 - a brake we cannot pull is an event, not an outage
            log.exception("could not halt entries after a blind timeout on %s", position.position_id)
            self._emit(
                "blind_timeout_halt_failed",
                {
                    "position_id": position.position_id,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "impact": (
                        "entries are NOT halted and a held position has no working stop; "
                        "halt by hand with `kaiba risk halt`"
                    ),
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )

    # ------------------------------------------------------------------ exit

    def _in_flight(self, state: WatchdogState) -> tuple[bool, str]:
        if not state.exit_order_id:
            return False, ""
        row = fetch_one(self.conn, "SELECT state FROM orders WHERE order_id=?", (state.exit_order_id,))
        if row is None:
            # We recorded a submit but there is no order row. Trust our own record and
            # stay locked if it was unresolved; guessing "it never happened" is how you
            # sell twice.
            locked = (state.exit_state or "") in UNRESOLVED_ORDER_STATES
            return locked, f"no order row for {state.exit_order_id}, recorded {state.exit_state}"
        order_state = str(row["state"])
        recorded = state.exit_state or ""
        if recorded in UNRESOLVED_ORDER_STATES and order_state != recorded:
            # The exit we sent resolved between this tick's refresh and now -- in another
            # process, since reconcile_all runs under ops -- and the `position` this tick
            # is holding predates that resolution: its qty is what we held BEFORE the fill.
            # MEASURED 2026-09-21 on the second live trade: sell ord:92508c8d139f6a69ed1e
            # went `filled` at 1790007863153 and the position closed in that same write;
            # 925 ms later this tick, holding a snapshot with qty=399,830,918,258, read
            # `filled` here, took "not in flight" to mean "free to sell", and sent
            # ord:4b88101f2ae802c03f44 -- a second 100% sell of tokens no longer held. The
            # venue refused it before send; nothing guaranteed that. A state change under
            # our feet means the snapshot is stale: stay locked for this tick and let the
            # next one read the position as it now is. Latch the full-exit here too, so
            # the next tick cannot sell it again either.
            state.exit_state = order_state
            if order_state == OrderState.FILLED.value and (state.exit_pct or Decimal(0)) >= 100:
                state.exit_final = True
            return True, (f"order {state.exit_order_id} resolved to {order_state} during this "
                          f"tick; position snapshot is stale, re-evaluating next tick")
        return order_state in UNRESOLVED_ORDER_STATES, f"order {state.exit_order_id} is {order_state}"

    def _accounted_inventory(self, position: Position) -> int | None:
        """Tokens this position holds according to the APPLIED-FILL ledger. ``None`` if unreadable.

        ``position_orders`` is the exactly-once record of which fills accounting actually
        applied, so summing the orders it names answers a question the ``positions`` row
        cannot: is the quantity on display backed by fills that really happened?

        Buys add what they received (``filled_out``); sells subtract what they sent
        (``amount_in``). Anything the ledger has not claimed is not counted, which is the
        entire point -- a stale or corrupted display contributes nothing here.
        """
        try:
            rows = fetch_all(
                self.conn,
                "SELECT po.side AS side, o.filled_out AS filled_out, o.amount_in AS amount_in "
                "FROM position_orders po JOIN orders o ON o.order_id = po.order_id "
                "WHERE po.position_id = ?",
                (position.position_id,),
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is not an answer
            log.warning("could not read the applied-fill ledger: %s", exc)
            return None
        held = 0
        for row in rows:
            side = str(row["side"] or "").lower()
            if side == Side.BUY.value:
                held += int(row["filled_out"] or 0)
            elif side == Side.SELL.value:
                held -= int(row["amount_in"] or 0)
        return held

    def _residual_is_backed_by_fills(self, position: Position) -> tuple[bool, int | None]:
        """Does the ledger support the quantity still on display? ``(backed, held)``.

        MEASURED 2026-09-23. A 100%-of-inventory exit fills, the final-exit latch closes,
        and a buy that was already in flight when the exit was planned lands afterwards.
        The ledger applies it, the position legitimately holds those tokens again, and the
        latch -- which exists to stop us selling the SAME tokens twice -- refuses every
        exit for tokens it has never sold. The stop cannot fire. Nothing can close it.

        The latch is still right in the case it was written for, so the two must be told
        apart, and the applied-fill ledger is what tells them apart: real inventory has
        buys behind it, a stale display does not. Backed means the ledger shows tokens
        held AND the display does not claim more than the ledger can support -- selling
        beyond what the fills prove is the phantom sell the latch was protecting us from.
        """
        held = self._accounted_inventory(position)
        if held is None or held <= 0:
            return False, held
        return int(position.qty) <= held, held

    def _abandon_unpriceable_shadow(self, position: Position, quote: PriceQuote,
                                    state: WatchdogState | None = None) -> bool:
        """Close a SHADOW position no source can price. ``True`` when it closed.

        See :data:`SHADOW_BLIND_ABANDON_S` for the measurement. The rule is narrow on
        purpose and each clause is load-bearing:

        * **shadow only.** Money at risk that we cannot see must halt and page, never be
          tidied away. This path must never be reachable for a live position.
        * **unpriceable now.** A position we can price is doing its job whatever its age.
        * **old enough.** An hour, so a provider having a bad few minutes never costs a
          paper position that would have priced again.

        Nothing is invented: the position closes at the proceeds it actually has, with a
        reason that says why, so the paper record reads as "we stopped being able to
        follow this" rather than as an outcome we measured.

        A COUNTED robinhood / bsc paper position (``paper_costs.entry_counts``) is not abandoned
        this way while there is evidence to book instead: see :meth:`_settle_blind_counted_paper`
        (2026-10-23) -- it must also have been BLIND for an hour, not merely opened an hour ago, and
        its pool is asked what the remainder is worth. ``state`` is this tick's watchdog state (its
        persisted ``blind_since_ms``); read from the database when not given.
        """
        if position.mode is not LaneMode.SHADOW:
            return False
        if quote.usable:
            return False
        if (position.chain, position.token) in self._deferred_keys:
            # Deferred is "not asked this tick", not "nobody can price it". MEASURED
            # 2026-10-03: 1,553 of 1,557 shadow `protection_blind` events in 17 h were
            # deferrals, and 18 shadow positions were abandoned in that window, each at
            # -100% of cost, every one of them after its exit had been decided on a usable
            # price. A paper twin closed as a total loss for want of a fetch we chose not to
            # make is a fabricated result on the record the promotion gate reads.
            return False
        age_s = (now_ms() - int(position.opened_ms or now_ms())) / 1000.0
        if age_s < SHADOW_BLIND_ABANDON_S:
            return False
        evidence: str | None = None
        if self._counted_paper(position):
            settled, evidence = self._settle_blind_counted_paper(
                position, age_s, state if state is not None else load_state(self.conn, position))
            if settled is not None:
                return settled
        try:
            from kaiba.execution.accounting import write_off_dust

            write_off_dust(position.position_id, self.conn, reason="abandoned_unpriceable")
        except Exception:  # noqa: BLE001 - tidying paper must never break a live tick
            log.exception("could not abandon an unpriceable shadow position")
            return False
        payload: dict[str, Any] = {
            "position_id": position.position_id,
            "age_s": int(age_s),
            "note": (
                "no source could price this paper position; abandoned so the shadow "
                "quote rotation stops spending live protection's tick budget on it"
            ),
        }
        if evidence is not None:
            payload["evidence"] = evidence
        self._emit(
            "shadow_position_abandoned",
            payload,
            level="warn",
            chain=position.chain,
            subject=position.token,
        )
        return True

    #: The exit-reason family of a counted paper position closed at zero by
    #: :meth:`_settle_blind_counted_paper`. NOT an ``abandoned_*`` family: those are booked write-offs
    #: every scoreboard leaves out (``kaiba.learning.fabricated``); this one is a measured loss.
    PAPER_TOTAL_LOSS_REASON = "paper_total_loss"
    #: The exit reason of a counted paper position no mark source priced for an hour, SOLD at its
    #: pool's own fill for the whole remainder (:meth:`_settle_blind_counted_paper`). Not an
    #: ``abandoned_*`` family either: a fill the pool gave, counted at what it paid.
    PAPER_BLIND_EXIT_REASON = "paper_blind_pool_exit"

    def _counted_paper(self, position: Position) -> bool:
        """A SHADOW robinhood / bsc position whose entry the paper scoreboard counts."""
        from kaiba.execution import paper_costs

        if position.mode is not LaneMode.SHADOW or position.chain not in paper_costs.BASIS_CHAINS:
            return False
        return paper_costs.entry_counts(paper_costs.read_basis(self.conn, position.position_id))

    def _settle_blind_counted_paper(self, position: Position, age_s: float,
                                    state: WatchdogState | None) -> tuple[bool | None, str | None]:
        """Settle a COUNTED robinhood / bsc paper position no source can price. ``(True, None)`` when it
        closed on the evidence, ``(False, None)`` when it stays OPEN this tick, ``(None, why)`` when the
        old abandon path is to write it off.

        The abandon path books ``abandoned_unpriceable``, which every scoreboard leaves out as a booked
        write-off. For a row whose entry counts (``paper_costs.entry_counts``) that is outcome-dependent
        exclusion of exactly the worst losers: a drained pool is the token no mark source prices any
        more, and a honeypot cannot be sold at all. 2026-10-23 (step 3 follow-up, blocking):

        * **blind for an hour.** :data:`SHADOW_BLIND_ABANDON_S` of the persisted blind clock
          (``WatchdogState.blind_since_ms``, which any usable price resets), not "opened an hour ago and
          blind for one tick": one missed quote on an old position must not end it. Until then the
          position stays open and nothing is read.
        * **then ONE pool read** for the remainder (:meth:`DefaultExitSubmitter.paper_blind_exit`):
          the token takes the whole sale, or the pool answers it cannot pay -> written off at ZERO under
          :data:`PAPER_TOTAL_LOSS_REASON` (``proceeds_native`` is what earlier fills paid; the leg is
          recorded, so the row stays counted -- ``total_loss`` honest, ``pool_no_fill_bound`` flagged
          ``exit_degraded``); the pool PAYS, dust included -> SOLD at that fill through the normal paper
          exit (:meth:`DefaultExitSubmitter.paper_sell_at_fill`) under :data:`PAPER_BLIND_EXIT_REASON`,
          an honest pool leg at its real, often ~-100%, result.
        * **a read that learned nothing** (transport, pacer, breaker) keeps the position open and is
          asked again, at most :data:`PAPER_POOL_RETRIES` more times and no sooner than
          :data:`PAPER_BLIND_RETRY_GAP_MS` after the last read -- never a read per tick -- and only then
          abandoned. So is a token this module cannot price at all (no pool known, no bsc pool host).
        """
        from kaiba.execution import paper_costs

        if position.mode is not LaneMode.SHADOW:
            return False, None  # unreachable from the abandon path; a guard for direct callers
        blind_since = state.blind_since_ms if state is not None else None
        ts = now_ms()
        if blind_since is None or ts - int(blind_since) < SHADOW_BLIND_ABANDON_S * 1000:
            return False, None
        episode = int(blind_since)
        pid = position.position_id
        seen, last_ms, reads = self._blind_exit_reads.get(pid, (episode, 0, 0))
        if seen != episode:
            last_ms, reads = 0, 0  # a new blind episode starts its own count
        if reads and ts - last_ms < PAPER_BLIND_RETRY_GAP_MS:
            return False, None
        submitter = self.submitter if isinstance(self.submitter, DefaultExitSubmitter) \
            else DefaultExitSubmitter(self.conn, self.price_source)
        try:
            found = submitter.paper_blind_exit(position)
        except Exception as exc:  # noqa: BLE001 - nothing was learned: a retry, bounded like any other
            log.exception("paper blind-exit evidence unavailable for %s", pid)
            found = _BlindPaperExit(BLIND_RETRY, "raised", f"paper_blind_exit raised {type(exc).__name__}")
        reads += 1
        if found.action == BLIND_SELL:
            out = submitter.paper_sell_at_fill(position, found.fill, reason=self.PAPER_BLIND_EXIT_REASON)
            if out.ok:
                self._blind_exit_reads.pop(pid, None)
                self._emit(
                    "shadow_position_blind_exit",
                    {"position_id": pid, "age_s": int(age_s), "blind_s": int((ts - episode) / 1000),
                     "fill": found.label, "note": found.note, "order_id": out.order_id,
                     "exit_reason": self.PAPER_BLIND_EXIT_REASON},
                    level="warn", chain=position.chain, subject=position.token,
                )
                return True, None
            # A pool fill the broker could not book: nothing was sold. Asked again, bounded.
            found = _BlindPaperExit(BLIND_RETRY, found.label, f"sell at the pool fill failed: {out.detail}"[:200],
                                    found.tax_bps)
        if found.action == BLIND_ZERO:
            try:
                from kaiba.execution.accounting import write_off_dust

                write_off_dust(pid, self.conn, reason=f"{self.PAPER_TOTAL_LOSS_REASON}:{found.label}")
            except Exception:  # noqa: BLE001 - tidying paper must never break a live tick
                log.exception("could not close a counted paper position at zero")
                self._blind_exit_reads[pid] = (episode, ts, reads)
                return False, None
            self._blind_exit_reads.pop(pid, None)
            paper_costs.record_exit_basis(self.conn, pid, position.chain, mark_source="none:unpriceable",
                                          fill=found.label, tax_bps=found.tax_bps, filled=True, note=found.note,
                                          broker=paper_costs.BOOKED_AT_ZERO)
            self._emit(
                "shadow_position_total_loss",
                {"position_id": pid, "age_s": int(age_s), "fill": found.label, "note": found.note,
                 "exit_reason": f"{self.PAPER_TOTAL_LOSS_REASON}:{found.label}"},
                level="warn", chain=position.chain, subject=position.token,
            )
            return True, None
        if found.action == BLIND_RETRY and reads <= PAPER_POOL_RETRIES:
            self._blind_exit_reads[pid] = (episode, ts, reads)
            self._emit(
                "shadow_position_blind_exit_retry",
                {"position_id": pid, "reads": reads, "max_reads": PAPER_POOL_RETRIES + 1,
                 "next_read_after_ms": ts + PAPER_BLIND_RETRY_GAP_MS, "note": found.note},
                level="info", chain=position.chain, subject=position.token,
            )
            return False, None
        self._blind_exit_reads.pop(pid, None)
        why = (f"pool unread after {reads} reads: {found.note}" if found.action == BLIND_RETRY
               else found.note)
        return None, why[:200]

    @staticmethod
    def _stranded_key(position_id: str) -> str:
        return f"watchdog.stranded:{position_id}"

    def _stranded_until(self, position: Position) -> int | None:
        """When this position is next due a full examination, or ``None`` if it is due now."""
        try:
            row = fetch_one(
                self.conn, "SELECT value FROM kv WHERE key=?",
                (self._stranded_key(position.position_id),),
            )
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            due = int((jload(row["value"]) or {}).get("recheck_after_ms") or 0)
        except (TypeError, ValueError):
            return None
        return due if due > now_ms() else None

    def _stranded_marker(self, position: Position) -> dict | None:
        """The wallet-empty evidence for this position, whether or not its recheck is due.

        Only the schedule in the marker expires; the evidence does not. The marker is only
        ever written from `_do_exit`'s `wallet_empty` branch and is removed once a sell is
        accepted, so it cannot describe a position the venue says still holds tokens.
        """
        try:
            row = fetch_one(
                self.conn, "SELECT value FROM kv WHERE key=?",
                (self._stranded_key(position.position_id),),
            )
        except sqlite3.Error:
            return None
        value = jload(row["value"]) if row else None
        if isinstance(value, dict) and value.get("reason") == "wallet_empty":
            return value
        return None

    def _extend_stranded(self, position: Position) -> None:
        """Keep an EXISTING wallet-empty deferral on its hourly cadence. Never creates one,
        so a deferral can still only originate in `_do_exit`'s wallet_empty branch."""
        marker = self._stranded_marker(position)
        if marker is None:
            return
        marker["recheck_after_ms"] = now_ms() + STRANDED_RECHECK_S * 1000
        try:
            upsert(
                self.conn, "kv",
                {
                    "key": self._stranded_key(position.position_id),
                    "value": jdump(marker),
                    "updated_ms": now_ms(),
                },
                ["key"],
            )
        except sqlite3.Error:
            log.exception("could not extend a stranded deferral")

    def _defer_stranded(self, position: Position, state: WatchdogState) -> None:
        """Stop servicing a WALLET-EMPTY position every tick. NOT a write-off.

        MEASURED 2026-09-24: five live positions -- four robinhood, one sol -- had a
        ledger quantity in the hundreds of trillions of base units and an on-chain
        balance of exactly ZERO, stable across reads twenty seconds apart. The same
        reader returned the exact non-zero balance for the two positions that do hold
        their tokens, which is what rules out a lying reader. Their sells were failing on
        `error=40002700 message=GetSwapRouteError` -- no swap route exists -- and they had
        gone 21-46 hours without a trade or a usable price from any of the three sources.

        `_write_off_dust` correctly refuses these: a FULL position is not dust, and
        closing one on a zero balance would book a total loss on evidence that does not
        support it. So they retried forever, and every tick still spent three provider
        calls trying to price each of them. That is a protection-budget leak, and this
        book has already halted entries on EVERY chain once through
        `protection_overrun`.

        What this does is ONLY rate limiting, and every part of that is deliberate:

        * The position stays OPEN, owned, and its cost stays encumbered against the
          exposure cap. Nothing is closed, no proceeds are invented, no loss is realised
          and no win/loss counter moves.
        * It is re-examined in full every `STRANDED_RECHECK_S`. A balance that reappears,
          a route that returns or a price that resolves puts it straight back on the
          normal path -- the recheck runs the ordinary tick, so nothing special is needed
          to recover.
        * It cannot hide money we can still see: the deferral is only ever set from the
          `wallet_empty` branch, which means the venue itself reports we hold none.
        """
        attempts = int(state.exit_attempts or 0)
        if attempts < STRANDED_MIN_ATTEMPTS:
            return
        due = now_ms() + STRANDED_RECHECK_S * 1000
        first = self._stranded_until(position) is None
        try:
            upsert(
                self.conn, "kv",
                {
                    "key": self._stranded_key(position.position_id),
                    "value": jdump({
                        "recheck_after_ms": due,
                        "attempts": attempts,
                        "reason": "wallet_empty",
                        "note": "open and owned; rate limited, not written off",
                    }),
                    "updated_ms": now_ms(),
                },
                ["key"],
            )
        except sqlite3.Error:
            log.exception("could not defer a stranded position")
            return
        if first:
            self._emit(
                "exit_stranded_deferred",
                {
                    "position_id": position.position_id,
                    "attempts": attempts,
                    "recheck_s": STRANDED_RECHECK_S,
                    "note": (
                        "the venue reports the wallet holds none of this token and no sell "
                        "route exists; the position stays OPEN and encumbered, but it is "
                        "examined hourly instead of every tick so it stops spending the "
                        "protection budget that live stops need"
                    ),
                },
                level="warn",
                chain=position.chain,
                subject=position.token,
            )

    def _write_off_dust(self, position: Position, state: WatchdogState) -> bool:
        """Close a position whose UNSELLABLE REMAINDER IS DUST. ``True`` when it closed.

        MEASURED 2026-09-23: a position 99% exited, its last 1% refused by the venue with
        HTTP 400 and reported as zero by the wallet, retried once a minute for hours --
        29 failures in 30 minutes, each one work inside a protection tick already over its
        5 s budget, which kept `protection_overrun` armed and halted entries on EVERY
        chain. One dust line stopped all trading.

        Two guards, and the fraction one is the important half:

        * **Only dust.** A remainder over ``DUST_FRACTION_MAX`` of what we bought is NOT
          dust, whatever the wallet says. A whole position reading zero means the tokens
          were moved, the account is wrong, or the balance read is lying -- and quietly
          closing it would book a total loss on evidence that cannot support one. That
          case pages and keeps retrying, exactly as it does today.
        * **Only after it has been tried.** ``exit_attempts`` is persisted, so this
          survives a restart: we do not write anything off the first time a balance reads
          zero, because a single read is not a fact about the chain.
        """
        total = int(position.qty_total or 0)
        remainder = int(position.qty or 0)
        fraction = (remainder / total) if total else 1.0
        if fraction > DUST_FRACTION_MAX:
            self._emit(
                "exit_unsellable_not_dust",
                {
                    "position_id": position.position_id,
                    "remaining_units": str(remainder),
                    "fraction_of_entry": f"{fraction:.4f}",
                    "rule": (
                        "the wallet reports none of this token but the remainder is too "
                        "large to be dust; refusing to write it off and still retrying"
                    ),
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return False
        if int(state.exit_attempts or 0) < DUST_MIN_ATTEMPTS:
            return False
        try:
            from kaiba.execution.accounting import write_off_dust

            write_off_dust(position.position_id, self.conn)
        except Exception:  # noqa: BLE001 - a failed write-off must not break the tick
            log.exception("could not write off the dust remainder")
            return False
        state.exit_retry_after_ms = None
        state.exit_final = True
        state.clear_pending()
        save_state(self.conn, state)
        self._emit(
            "exit_dust_written_off",
            {
                "position_id": position.position_id,
                "remaining_units": str(remainder),
                "fraction_of_entry": f"{fraction:.4f}",
                "attempts": state.exit_attempts,
                "note": (
                    "the wallet holds none of this token and the remainder is dust; the "
                    "position is closed at its real proceeds with no sell invented"
                ),
            },
            level="warn",
            chain=position.chain,
            subject=position.token,
        )
        return True

    def _emit_exit_timing(
        self,
        position: Position,
        state: WatchdogState,
        *,
        pct: Decimal,
        reason: str,
        kind: ProtectionKind,
        quote: PriceQuote,
        outcome: ExitOutcome,
        timing: dict[str, Any] | None,
        submit_started_ms: int,
        submit_returned_ms: int,
    ) -> None:
        """One ``exit_timing`` event per exit attempt that reached the submitter.

        Journal #4997: robinhood stops closed a median 83 s after the tape's first -30%
        print against a 12 s poll, and nothing recorded where the time went. This stamps
        every step this process owns -- when the trigger mark was observed, when it reached
        us, when the tick started, when it was decided, when the submit started and
        returned, and what the submitter spent inside -- and carries ``order_id`` so
        confirmation can be joined from ``orders`` later. ``kaiba.learning.exit_latency``
        reads it back. ``dedupe_key`` is ``exit_timing:<position>:<submit ms>:<attempt>``,
        which also makes the events findable by index rather than by scanning ``system``.

        Diagnostics only: it never raises and nothing reads it to decide anything.
        """
        try:
            t = dict(timing or {})
            observed = t.get("mark_observed_ms") or int(quote.observed_ms)
            decided = t.get("decided_ms") or submit_started_ms

            def gap(later: Any, earlier: Any) -> int | None:
                if later is None or earlier is None:
                    return None
                return int(later) - int(earlier)

            price_since = t.get("price_since_ms")
            prev_since = t.get("prev_price_since_ms")
            outcome_label = (
                "ambiguous" if outcome.ambiguous else ("submitted" if outcome.ok else "failed")
            )
            set_aside = self._set_aside.get((position.chain, position.token))
            payload = {
                "version": 1,
                "position_id": position.position_id,
                "order_id": outcome.order_id,
                "outcome": outcome_label,
                "order_state": outcome.state.value if outcome.state else None,
                "mode": position.mode.value,
                "lane": position.lane.value,
                "reason": reason,
                "kind": kind.value,
                "pct": str(pct),
                "attempt": int(state.exit_attempts),
                "mark": {
                    "source": quote.source,
                    "settlement": settlement_of(quote),
                    "price_usd": _s(quote.price_usd),
                    "observed_ms": observed,
                    "fetched_ms": t.get("mark_fetched_ms"),
                    "previous_observed_ms": t.get("previous_mark_observed_ms"),
                    "price_since_ms": price_since,
                    "prev_price_since_ms": prev_since,
                    "set_aside_source": set_aside.source if set_aside is not None else None,
                },
                "ms": {
                    "tick_started": t.get("tick_started_ms"),
                    "decided": decided,
                    "submit_started": submit_started_ms,
                    "submit_returned": submit_returned_ms,
                },
                "segments_ms": {
                    "mark_age_at_decision": gap(decided, observed),
                    "fetch_to_decision": gap(decided, t.get("mark_fetched_ms")),
                    "tick_to_decision": gap(decided, t.get("tick_started_ms")),
                    "previous_mark_to_mark": gap(observed, t.get("previous_mark_observed_ms")),
                    "price_unchanged_at_decision": gap(decided, price_since),
                    # Only meaningful when THIS mark changed the price: how long the price
                    # before it had been showing. The audit's "held 36 s, then jumped".
                    "prev_price_held": (
                        gap(observed, prev_since)
                        if price_since is not None and int(price_since) == int(observed) else None
                    ),
                    "decision_to_submit": gap(submit_started_ms, decided),
                    "submit_call": gap(submit_returned_ms, submit_started_ms),
                },
                "submitter_ms": dict(outcome.timing or {}),
                "poll_interval_ms": t.get("poll_interval_ms"),
            }
            self._emit(
                "exit_timing",
                payload,
                chain=position.chain,
                subject=position.token,
                dedupe_key=(
                    f"exit_timing:{position.position_id}:{submit_started_ms}:{int(state.exit_attempts)}"
                ),
            )
        except Exception:  # noqa: BLE001 - timing is diagnostics, never an exit failure
            log.debug("could not emit exit timing", exc_info=True)

    def _do_exit(
        self,
        position: Position,
        state: WatchdogState,
        pct: Decimal,
        reason: str,
        quote: PriceQuote,
        kind: ProtectionKind,
        report: TickReport,
        *,
        timing: dict[str, Any] | None = None,
    ) -> None:
        ts = now_ms()
        if state.exit_final:
            backed, held = self._residual_is_backed_by_fills(position)
            if backed:
                state.exit_final = False
                state.exit_order_id = None
                state.exit_state = None
                state.exit_pct = None
                state.exit_reason = None
                save_state(self.conn, state)
                self._emit(
                    "exit_latch_rearmed",
                    {
                        "position_id": position.position_id,
                        "accounted_units": str(held),
                        "position_units": str(position.qty),
                        "note": (
                            "a full exit filled, and the applied-fill ledger shows tokens "
                            "held again; this is new inventory, not the inventory that was "
                            "sold, so the final-exit latch is stale and protection re-arms"
                        ),
                    },
                    level="warn",
                    chain=position.chain,
                    subject=position.token,
                )
        if state.exit_final:
            self._emit(
                "exit_suppressed",
                {
                    "position_id": position.position_id,
                    "reason": "a full exit for this position already filled",
                    "note": (
                        "the positions row is still open, which means live fill accounting "
                        "has not written back; refusing to sell it again"
                    ),
                    "wanted_pct": str(pct),
                },
                level="warn",
                chain=position.chain,
                subject=position.token,
            )
            return

        locked, detail = self._in_flight(state)
        if locked:
            self._emit(
                "exit_not_resubmitted",
                {
                    "position_id": position.position_id,
                    "detail": detail,
                    "wanted_pct": str(pct),
                    "rule": "an ambiguous or in-flight send is resolved by reconciliation, never retried",
                },
                level="warn" if state.exit_state == OrderState.UNKNOWN.value else "info",
                chain=position.chain,
                subject=position.token,
            )
            return

        if state.exit_retry_after_ms and ts < state.exit_retry_after_ms:
            return

        decision = self.gate.check_exit(position.chain, position.lane, self.conn)
        if not getattr(decision, "allowed", True):
            # risk.check_exit is documented to always allow. If it ever does not, something
            # deliberate changed and silently overriding it would be worse than refusing.
            self._emit(
                "exit_refused_by_gate",
                {
                    "position_id": position.position_id,
                    "reason": getattr(decision, "reason", "unknown"),
                    "note": "RiskGate.check_exit refused an exit; exits are supposed to be ungated",
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return
        brakes = list(getattr(decision, "findings", []) or [])

        # Take the overlapping venue rungs back before we sell them ourselves. This is the
        # double-sell guard and it is deliberately placed *here*, after every refusal check
        # and immediately before the submit, so that nothing between it and the sell can
        # turn it into a gate. It returns on every path and the exit proceeds regardless.
        self._claim_standing(position, pct, quote, reason, report)

        self._emit("quote_decision", {
            "position_id": position.position_id, "reason": reason, "pct": str(pct),
            **state.quote_evidence,
        }, chain=position.chain, subject=position.token)
        submit_started_ms = now_ms()
        outcome = self.submitter.submit_exit(position, pct, quote=quote, reason=reason)
        submit_returned_ms = now_ms()
        state.exit_attempts += 1
        state.exit_order_id = outcome.order_id
        state.exit_state = outcome.state.value if outcome.state else None
        state.exit_pct = pct
        state.exit_reason = reason
        self._emit_exit_timing(
            position, state, pct=pct, reason=reason, kind=kind, quote=quote, outcome=outcome,
            timing=timing, submit_started_ms=submit_started_ms,
            submit_returned_ms=submit_returned_ms,
        )

        if outcome.ambiguous:
            state.exit_retry_after_ms = None
            state.clear_pending()
            report.exits += 1
            self._emit(
                "exit_ambiguous",
                {
                    "position_id": position.position_id,
                    **state.quote_evidence,
                    "order_id": outcome.order_id,
                    "pct": str(pct),
                    "detail": outcome.detail,
                    "rule": "left to reconciliation; this position will not be re-armed until it resolves",
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return

        if not outcome.ok and outcome.wallet_empty and self._write_off_dust(position, state):
            return

        if not outcome.ok and outcome.wallet_empty:
            self._defer_stranded(position, state)

        if not outcome.ok:
            report.exit_failures += 1
            # OUR limiter refused before anything was sent (minimum interval, max inflight,
            # bucket exhausted): it is not a venue failure and must not be backed off like
            # one. MEASURED 2026-09-30 (robinhood 0x2aa4...6262): stop_loss decided, sell
            # refused "gmgn: minimum interval (retry in 0.1s)", re-sent on the next 12 s
            # tick, closed -42.3% against a -30% stop. (`limiter.reserve` now waits a gap
            # that short out at EXIT; this is the backstop for whatever still refuses.)
            # Retry on the limiter's own hint, capped at the first rung, and do not
            # count the attempt: `exit_attempts` drives escalation to the 30-minute ceiling
            # and the dust write-off, and self-throttling is evidence of neither. A
            # provider's own 429 and our cooldowns parse as None and keep the full backoff.
            from kaiba.core.limiter import local_refusal_retry_s  # noqa: PLC0415

            limiter_retry_s = local_refusal_retry_s(outcome.detail)
            if limiter_retry_s is not None:
                state.exit_attempts = max(0, state.exit_attempts - 1)
                backoff = min(RETRY_BASE_MS, int(limiter_retry_s * 1000) + 1)
            else:
                backoff = _exit_backoff(state.exit_attempts)
            state.exit_retry_after_ms = ts + backoff
            # A failed TRIM stays pending, so it is offered again after the backoff. `evaluate`
            # spends a take-profit rung when it fires (tp_done), so unlike a stop it is never
            # decided twice: MEASURED 2026-10-19, Cx6qfd3c's tp1 at 2.0x failed once and sat
            # 13 min with the rung spent and no retry. Full exits are re-decided every tick and
            # a wallet-empty sell has its own path, so neither is touched.
            if pct < 100 and not outcome.wallet_empty:
                if state.pending_pct is None:
                    state.pending_ms = ts
                state.pending_pct = max(state.pending_pct or Decimal(0), pct)
                state.pending_reason = reason[:120]
                state.pending_source = "retry"
            self._emit(
                "exit_failed",
                {
                    "position_id": position.position_id,
                    **state.quote_evidence,
                    "order_id": outcome.order_id,
                    "pct": str(pct),
                    "detail": outcome.detail,
                    "attempts": state.exit_attempts,
                    "retry_in_ms": backoff,
                    "limiter_refusal": limiter_retry_s is not None,
                    "gated_exit_bug": _looks_like_an_entry_gate(outcome.detail),
                },
                level="error",
                chain=position.chain,
                subject=position.token,
            )
            return

        state.exit_retry_after_ms = None
        state.clear_pending()
        # The provider accepted this submit, so the failures that came before it are
        # history. Until 2026-09-21 this counter was incremented on every attempt and
        # reset NOWHERE, so a position that had once failed to exit carried its
        # exponential backoff (RETRY_BASE_MS * 2**(attempts-1), capped at RETRY_MAX_MS)
        # forever -- including into the next, unrelated stop. A burnt counter on a live
        # position is a stop that fires late by construction. Found by adversarial
        # review of the high-volume design; the fourth of four exit-protection gaps.
        state.exit_attempts = 0
        # The venue accepted a sell, so the wallet holds this token after all: the
        # wallet-empty marker no longer describes it and must not exempt it from the brake.
        try:
            self.conn.execute(
                "DELETE FROM kv WHERE key=?", (self._stranded_key(position.position_id),)
            )
        except sqlite3.Error:
            log.exception("could not clear a stranded marker")
        report.exits += 1
        if kind is ProtectionKind.TRIM and pct < 100:
            report.trims += 1
        if pct >= 100 and outcome.state is OrderState.FILLED:
            state.exit_final = True

        ev.emit(
            EventKind.PROTECTION_TRIGGERED,
            {
                "source": SERVICE,
                "service": SERVICE,
                "event": "exit_submitted",
                "position_id": position.position_id,
                **state.quote_evidence,
                "token": position.token,
                "pct": str(pct),
                "reason": reason,
                "kind": kind.value,
                "order_id": outcome.order_id,
                "order_state": outcome.state.value if outcome.state else None,
                "price_usd": _s(quote.price_usd),
                "price_basis": quote.basis.value,
                "stop_price_usd": _s(state.stop_price_usd),
                # Recorded, not obeyed: exits are never gated. This is the audit trail for
                # "we sold while the kill switch was on", which is correct behaviour.
                "risk_findings": brakes,
            },
            chain=position.chain,
            subject=position.token,
            level="warn",
            conn=self.conn,
        )


def _looks_like_an_entry_gate(detail: str) -> bool:
    """True when an exit was refused by something that is only supposed to block entries.

    ``executor._check_mode`` refuses every submission when the kill switch is engaged or
    the lane's effective mode is OFF/SHADOW, including sells. That contradicts
    ``risk.check_exit`` and ``docs/PLAN.md``; the watchdog cannot fix it from here (the
    executor is owned elsewhere), so it flags it on the event instead of hiding it.
    """
    text = (detail or "").lower()
    return "kill switch" in text or "is off" in text or "is shadow" in text


# --------------------------------------------------------------------------------------
# service entry point
# --------------------------------------------------------------------------------------


def _install_signal_handlers(stop: threading.Event) -> list[tuple[int, Any]]:
    """SIGINT/SIGTERM set the stop event so the current tick finishes cleanly."""
    if threading.current_thread() is not threading.main_thread():
        return []
    previous: list[tuple[int, Any]] = []

    def handler(signum: int, _frame: Any) -> None:
        log.info("watchdog received signal %s, stopping after this tick", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous.append((sig, signal.signal(sig, handler)))
        except (ValueError, OSError, AttributeError, RuntimeError):  # pragma: no cover
            continue
    return previous


def _restore_signal_handlers(previous: list[tuple[int, Any]]) -> None:
    for sig, old in previous:
        try:
            signal.signal(sig, old)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover
            continue


def run_watchdog(
    conn: sqlite3.Connection | None = None,
    *,
    price_source: PriceSource | None = None,
    prefetch_source_factory: Callable[[], PriceSource] | None = None,
    submitter: ExitSubmitter | None = None,
    interval_s: float | None = None,
    max_ticks: int | None = None,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> dict[str, Any]:
    """Run the exit watchdog until stopped. Called by ``kaiba run protection``.

    ``price_source`` is the injection point for P1-3: pass anything with a ``quote``
    method and the loop starts evaluating stops. When nothing is passed, the source named
    by ``protection.price_source`` in ``config/risk.yaml`` is used, which is unset by
    default — so out of the box it runs, checks nothing, and says so once a tick.

    ``prefetch_source_factory`` is the same injection point for the quote workers, which
    must not share one source's SQLite connection (see ``Watchdog._prefetch_quotes``).
    When this function resolves the source itself it wires the matching factory; a caller
    that injected a ``price_source`` owns that decision and gets nothing it did not ask
    for.
    """
    c = conn if conn is not None else ensure_db()
    if price_source is None:
        configured = configured_price_source_name()
        price_source = resolve_price_source(configured)
        if prefetch_source_factory is None:
            # The service builds its own source, so it also knows how to build a second
            # one -- and the prefetch needs one per worker thread rather than eight
            # threads on this thread's connection. A caller that injected its own
            # ``price_source`` owns that question and is left alone.
            prefetch_source_factory = prefetch_source_factory_for(configured)
    dog = Watchdog(
        c,
        price_source=price_source,
        prefetch_source_factory=prefetch_source_factory,
        submitter=submitter,
    )
    stop = stop or threading.Event()
    handlers = _install_signal_handlers(stop) if install_signals else []

    totals = TickReport()
    ticks = 0
    dog._emit(
        "started",
        {
            "price_source": getattr(dog.price_source, "name", type(dog.price_source).__name__),
            "interval_s": interval_s if interval_s is not None else dog.config().poll_interval_s,
            "blind_by_default": isinstance(dog.price_source, NullPriceSource),
            "max_blind_s": dog.blind_budget_s(),
            "max_blind_halt_entries": dog._halt_entries_on_timeout(),
        },
        level="warn" if isinstance(dog.price_source, NullPriceSource) else "info",
    )
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                report = dog.tick()
                totals.checked += report.checked
                totals.blind += report.blind
                totals.blind_over_budget += report.blind_over_budget
                totals.longest_blind_s = max(totals.longest_blind_s, report.longest_blind_s)
                totals.exits += report.exits
                totals.exit_failures += report.exit_failures
                totals.trims += report.trims
                totals.errors += report.errors
            except Exception as exc:  # noqa: BLE001 - the loop outlives any single failure
                totals.errors += 1
                log.exception("watchdog tick failed")
                dog._emit("tick_failed", {"error": f"{type(exc).__name__}: {exc}"[:300]}, level="error")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            period = float(
                interval_s if interval_s is not None else (dog.config().poll_interval_s or 5)
            )
            stop.wait(max(0.05, period - (time.monotonic() - started)))
    finally:
        _restore_signal_handlers(handlers)
        dog._emit("stopped", {"ticks": ticks, **totals.as_payload()})
    return {"ticks": ticks, **totals.as_payload()}


__all__ = [
    "BLIND_WARN_INTERVAL_S",
    "CURSOR_KEY",
    "MAX_BLIND_HALT_ENTRIES_DEFAULT",
    "MAX_BLIND_S_DEFAULT",
    "NATIVE_SETTLEMENT_ASSETS",
    "SETTLEMENT_MARK_CHAINS_DEFAULT",
    "DefaultExitSubmitter",
    "ExitOutcome",
    "ExitRequest",
    "ExitSubmitter",
    "FallbackPriceSource",
    "PRICE_SOURCES",
    "NullPriceSource",
    "PriceQuote",
    "PriceSource",
    "ProtectionUpdate",
    "ProviderPriceSource",
    "SERVICE",
    "TickReport",
    "Watchdog",
    "WatchdogState",
    "configured_max_blind_halt_entries",
    "configured_max_blind_s",
    "configured_price_source_name",
    "configured_settlement_mark_chains",
    "is_foreign_mark",
    "settlement_of",
    "load_state",
    "prefetch_source_factory_for",
    "read_cursor",
    "resolve_price_source",
    "run_watchdog",
    "save_state",
    "token_decimals",
    "write_cursor",
]
