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
from dataclasses import dataclass, field
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
        try:
            # EXIT is the highest limiter priority. A price read that decides whether to
            # close a position must outrank discovery work competing for the same budget,
            # or a busy scan can make the watchdog blind exactly when it matters.
            from kaiba.core.limiter import Priority

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
    return FallbackPriceSource(routed, gmgn, name="venue+gmgn")


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

        {"balances":[{"wallet_address":"62Ebt...","token_address":"8JVtw...",
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

    def submit_exit(
        self, position: Position, pct: Decimal, *, quote: PriceQuote, reason: str
    ) -> ExitOutcome:
        if position.mode is LaneMode.SHADOW:
            return self._paper(position, pct, quote, reason)
        return self._live(position, pct, quote, reason)

    # ------------------------------------------------------------------ paper

    def _paper(
        self, position: Position, pct: Decimal, quote: PriceQuote, reason: str
    ) -> ExitOutcome:
        if quote.price_usd is None or quote.price_usd <= 0:
            return ExitOutcome(False, None, None, "paper exit needs a price")
        if quote.liquidity_usd is None or quote.liquidity_usd <= 0:
            # The paper broker's whole value is modelling impact against real depth.
            # Filling against an unknown pool would launder a guess into the shadow record
            # the promotion gate reads.
            return ExitOutcome(False, None, None, "paper exit not modelled: liquidity unavailable")
        broker = PaperBroker(self.conn, curve_resolver=_curve_resolver(self.conn))
        order = broker.sell(
            position,
            pct,
            price_usd=quote.price_usd,
            liquidity_usd=quote.liquidity_usd,
            exit_reason=reason[:120],
        )
        return ExitOutcome(
            order.state is OrderState.FILLED, order.state, order.order_id, order.error or ""
        )

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
            held = wallet_token_units(position.chain, wallet, position.token, decimals)
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

        min_out = self._min_out(position, qty, decimals, quote)
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
        try:
            result = executor.submit(order, self.conn)
        except executor.ExecutionAmbiguous as exc:
            return ExitOutcome(False, OrderState.UNKNOWN, order.order_id, str(exc)[:300])
        except Exception as exc:  # noqa: BLE001 - refusals, rate limits and surprises alike
            return ExitOutcome(False, OrderState.FAILED, order.order_id, f"{type(exc).__name__}: {exc}"[:300])
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
    ) -> None:
        self.conn = conn or get_conn()
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

    def _position_cfg(self, cfg: ProtectionConfig, state: WatchdogState) -> ProtectionConfig:
        """Fold ``kaiba_set_protection`` overrides into this position's config.

        ``trail_bps`` is a single number and the config holds a tiered table, so the
        override is applied as "use this distance at every tier". That reading is a guess:
        the MCP tool's payload has no documented semantics beyond the field name.
        """
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

    def tick(self) -> TickReport:
        started = time.monotonic()
        report = TickReport()
        cfg = self.config()
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

        for position in positions:
            report.checked += 1
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
        if budget_ms <= 0 or report.duration_ms <= budget_ms:
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
        except sqlite3.Error:
            return None
        return int(row["ts"]) if row and row["ts"] else None

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
        row = fetch_one(self.conn, "SELECT value FROM kv WHERE key=?", (self._quote_memory_key(position),))
        raw = jload(row["value"], {}) if row else {}
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
        return quotes[0], quotes[1]

    def _remember_quote(
        self, position: Position, state: WatchdogState, quote: PriceQuote,
        previous: PriceQuote | None,
    ) -> None:
        if self._quote_identity(quote) is None:
            return
        if quote.liquidity_usd is not None and self._quote_identity(quote, liquidity=True) is not None:
            previous = quote
            state.prev_liquidity_usd = quote.liquidity_usd
        upsert(self.conn, "kv", {
            "key": self._quote_memory_key(position),
            "value": jdump({"price": quote.model_dump(mode="json"),
                            "liquidity": previous.model_dump(mode="json") if previous else None}),
            "updated_ms": now_ms(),
        }, ["key"])

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
            if decimals is None:
                return False
            size = Decimal(position.qty) / Decimal(10) ** decimals * quote.price_usd
            # Existing impact model and operator slippage bound, not invented dollar dust.
            impact = PaperBroker(self.conn).impact_bps(size, quote.liquidity_usd)
            return impact <= get_risk().bounds.max_slippage_bps
        return True

    def _alternative_sources(self, chain: Chain):
        """Walk configured fallback boundaries, not a new provider stack."""
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

        yield from walk(self.price_source)

    def _validate_quote(
        self, position: Position, state: WatchdogState, cfg: ProtectionConfig,
        quote: PriceQuote, last_price: PriceQuote | None, previous: PriceQuote | None,
    ) -> PriceQuote:
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
                         cfg=self._position_cfg(cfg, state))
        suspect_stop = (not established and probe.kind is ProtectionKind.EXIT_ALL
                        and (identity is not None or last_price is not None))
        if depth_ok and (not suspect_stop or self._comparable(quote, previous)):
            return quote
        for source in self._alternative_sources(position.chain):
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
            if alternative.chain != position.chain or alternative.token != position.token:
                continue
            if self._quote_identity(alternative) == identity:
                continue
            if quote.pool_id and alternative.pool_id == quote.pool_id:
                continue
            if self._credible_depth(position, alternative, previous):
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

        last_price, previous = self._quote_memory(position)
        original = self._quote(position)
        quote = self._validate_quote(position, state, cfg, original, last_price, previous)
        state.quote_evidence = {
            "quote_provenance": quote.model_dump(mode="json"),
            "previous_liquidity_usd": _s(previous.liquidity_usd if previous else state.prev_liquidity_usd),
            "previous_liquidity_provenance": previous.model_dump(mode="json") if previous else None,
            "liquidity_comparable": self._comparable(quote, previous),
            "rejected_quote": original.model_dump(mode="json") if quote is not original else None,
        }
        if not quote.usable:
            # A PAPER position nobody can price is latency, not learning. Checked before
            # the blind bookkeeping so an abandoned one stops costing the rotation a real
            # fetch on the very next tick. Live positions are never eligible -- see
            # `_abandon_unpriceable_shadow`.
            if self._abandon_unpriceable_shadow(position, quote):
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
        action = evaluate(
            protection,
            price_usd=quote.price_usd,
            liquidity_usd=quote.liquidity_usd,
            prev_liquidity_usd=previous.liquidity_usd if comparable else None,
            executable_quote_usd=quote.executable_quote_usd,
            # A token-wide migration waiver cannot hide a SAME established-pool drain.
            migrated_ms=None if comparable else self._migrated_ms(position),
            last_trade_ms=self._last_trade_ms(position),
            cfg=self._position_cfg(cfg, state),
        )
        state.absorb(protection)
        if self._quote_identity(quote) != self._quote_identity(last_price):
            self._emit("quote_source_changed", {
                "position_id": position.position_id, **state.quote_evidence,
            }, chain=position.chain, subject=position.token)
        self._remember_quote(position, state, quote, previous)

        pct, reason = self._combine(action.pct if action.sells else None, action.reason, state)
        if pct is None:
            save_state(self.conn, state)
            self._sync_standing(position, state, cfg, report)
            return
        self._do_exit(position, state, pct, reason, quote, action.kind, report)
        save_state(self.conn, state)
        # After a trim the ladder has changed, so the mirror is stale until this runs.
        self._sync_standing(position, state, cfg, report, force=True)

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

        def one(key: tuple[Chain, str]) -> tuple[tuple[Chain, str], PriceQuote]:
            chain, token = key
            try:
                got = source().quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - one dead quote is blindness for one position
                return key, PriceQuote.unavailable(f"price source raised {type(exc).__name__}")
            if not isinstance(got, PriceQuote):
                return key, PriceQuote.unavailable(f"price source returned {type(got).__name__}")
            return key, got

        def fetch(keys: list[tuple[Chain, str]]) -> None:
            if not keys:
                return
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

    def _abandon_unpriceable_shadow(self, position: Position, quote: PriceQuote) -> bool:
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
        """
        if position.mode is not LaneMode.SHADOW:
            return False
        if quote.usable:
            return False
        age_s = (now_ms() - int(position.opened_ms or now_ms())) / 1000.0
        if age_s < SHADOW_BLIND_ABANDON_S:
            return False
        try:
            from kaiba.execution.accounting import write_off_dust

            write_off_dust(position.position_id, self.conn, reason="abandoned_unpriceable")
        except Exception:  # noqa: BLE001 - tidying paper must never break a live tick
            log.exception("could not abandon an unpriceable shadow position")
            return False
        self._emit(
            "shadow_position_abandoned",
            {
                "position_id": position.position_id,
                "age_s": int(age_s),
                "note": (
                    "no source could price this paper position; abandoned so the shadow "
                    "quote rotation stops spending live protection's tick budget on it"
                ),
            },
            level="warn",
            chain=position.chain,
            subject=position.token,
        )
        return True

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

    def _do_exit(
        self,
        position: Position,
        state: WatchdogState,
        pct: Decimal,
        reason: str,
        quote: PriceQuote,
        kind: ProtectionKind,
        report: TickReport,
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
        outcome = self.submitter.submit_exit(position, pct, quote=quote, reason=reason)
        state.exit_attempts += 1
        state.exit_order_id = outcome.order_id
        state.exit_state = outcome.state.value if outcome.state else None
        state.exit_pct = pct
        state.exit_reason = reason

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
    "load_state",
    "prefetch_source_factory_for",
    "read_cursor",
    "resolve_price_source",
    "run_watchdog",
    "save_state",
    "token_decimals",
    "write_cursor",
]
