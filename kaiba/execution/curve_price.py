"""Exact bonding-curve pricing: the price a pre-graduation token actually has.

Three paper orders existed in this tree and all three failed with ``no_price``. The cause
was structural: the paper broker's only price source was DexScreener, which has **no pair
row for a token still on its bonding curve**, and every candidate this system sees is a
pre-graduation pump.fun launch. DexScreener was right to answer "unknown".

**A correction worth stating plainly, because the first version of this file got it
wrong.** It is *not* true that a bonding-curve token cannot be priced by a router.
Jupiter routes pump.fun curves directly — verified with raw calls on two live
pre-graduation mints from our own ``tokens`` table, one hop, route label ``Pump.fun``,
real impact. The failure was specific to DexScreener, not a general property of quoting.

Curve arithmetic is still the primary pre-graduation source, for three reasons a router
does not cover:

1. **Latency.** A mint seconds old returns HTTP 400 from Jupiter until it is indexed, and
   that window is exactly the one the lanes care about. Curve reserves exist the moment
   the mint does.
2. **Rate and cost.** The measured Jupiter ceiling is a sustained ~1 request/second before
   a *sticky* 429 with no ``retry-after``, and a price probe costs up to two calls per
   token. A watchdog polling several positions every few seconds cannot afford a network
   call per position per tick; it can afford arithmetic on reserves we already hold.
3. **It is the ground truth.** Pre-graduation, the curve *is* the price. A router quote is
   a quote against it.

So the order is: **curve reserves pre-graduation, an executable router quote as a
cross-check where the budget allows, DexScreener or a router after graduation.** Which
one priced a given fill is recorded on the fill (:class:`FillBasis`), so a later analysis
can measure how far curve arithmetic and executable quotes actually run apart.

What this module is
-------------------

Integer arithmetic on the constant product of the curve's *virtual* reserves, plus the
three things that separate a quote from a fill. Nothing here estimates; the only
approximation in the file is the latency drift, and it is labelled as one on every fill.

The formulas are pump.fun's own, and they were checked against reality rather than read
off a blog. Replaying 21 real mainnet trades across 12 recorded ``curve_snapshots``
windows and 8 tokens lands on every window's closing reserves **exactly, to the atom and
to the lamport**, reproduces all 13 sells exactly, and is independently confirmed by
Jupiter: on three live pre-graduation mints an executable Jupiter quote at 0.02 SOL and
this arithmetic return the **same integer**.

    buy : tokens_out = virtual_token - (k // (virtual_sol + sol_in) + 1)
    sell: sol_out    = (token_in * virtual_sol) // (virtual_token + token_in)

The ``+ 1`` is not cosmetic. It is the protocol rounding the reserve up, i.e. rounding
against the trader, and it is why ``k`` creeps upward by about one part in 10^10 per
trade instead of staying constant. Reproducing it is what makes the model non-optimistic
at the last atom.

The constants are not constants
-------------------------------

Creators choose a starting market cap. Observed graduation targets in this repository's
own ``curve_snapshots`` span **0.11 SOL to 115 SOL**. Hard-coding 30 virtual SOL,
793.1e12 real tokens or an 85 SOL target rejects the high-cap launches and silently
*overstates* progress on the low-cap ones. The only invariant that held on every
SOL-quoted coin observed is

    virtual_token_reserves - real_token_reserves = 279,900,000,000,000

so every piece of geometry below is derived per token from that invariant and the
curve's own ``k``. This is the same derivation as
:func:`kaiba.execution.scanner.curve_from_payload`, and :meth:`CurveState.from_payload`
calls that function rather than repeating it — there must be exactly one derivation in
the tree. ``tests/test_curve_price.py`` pins the constant and the derived geometry
against the scanner's so the two cannot drift apart.

What a fill costs, and what we refuse to pretend we know
--------------------------------------------------------

1. **Our own price impact** — exact, from the curve, at the state we would actually hit.
2. **The platform fee** — pump.fun's curve is a flat **125 bps** (95 protocol + 30
   creator). Post-graduation PumpSwap runs a 25-tier schedule whose first tier is **120
   bps** (25 + a 95 bps creator cut that decays with market cap). We charge the first
   tier always: the creator cut only ever falls, so the first tier is the unflattering
   end of the schedule.
3. **Latency** — the measured tier-1 decision path on this machine is 7.6 s. The price
   when we decide is not the price when we fill, so the curve is advanced by the token's
   *own measured* flow rate over that delay and the fill is priced at the advanced state.
   By default the drift is applied in the direction that hurts (``DriftMode.ADVERSE``),
   because the sign of the next 7.6 seconds is exactly what we do not know.

Everything we are **not** modelling is listed in :data:`NOT_MODELLED` and copied onto
every fill, so an analysis reading the shadow record sees the gap without reading this
docstring. The largest item by far is adverse selection — other people's orders landing
between our decision and ours — and the largest *measured* one is the bot-router fee and
token-account rent a real retail fill also pays; see that constant for both figures.

Money is integer base units (lamports, token atoms). USD is ``Decimal``. No ``float``
touches a number in this file.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, Side, now_ms

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------------------

#: The only genuine pump.fun invariant: ``virtual_token - real_token``, the supply held
#: back for the post-graduation pool. Mirrors
#: ``kaiba.execution.scanner.RESERVED_TOKEN_ATOMS``; duplicated rather than imported to
#: keep this module free of the scanner's provider-layer imports (the scanner imports the
#: engine, which imports the paper broker, which imports this file). The duplication is
#: pinned by ``test_reserved_token_atoms_matches_the_scanner``.
RESERVED_TOKEN_ATOMS = 279_900_000_000_000

LAMPORTS_PER_SOL = 1_000_000_000
SOL_DECIMALS = 9
BPS = Decimal(10_000)

#: pump.fun bonding-curve fee: flat, no tiers. **Verified on mainnet**, not read off a
#: doc. ``tests/fixtures/curve_price/onchain_fee_split.json`` holds the raw lamport
#: balance deltas of three real buys, and on all three the split is exact:
#:
#: * one account takes **30.0000 bps** of the curve leg (the creator);
#: * two accounts take **47.5000 bps** each, i.e. **95 bps** (the protocol);
#: * the buyer is debited the curve leg plus the sum of those three, to the lamport.
#:
#: That last line settles the convention: the fee is charged **on top of** the curve leg
#: and never touches the reserves, which is why the ``swaps`` table — whose
#: ``amount_native`` reconciles exactly with ``real_sol_reserves`` — cannot see it.
#:
#: It also settles the rounding. The three components are each rounded **up**
#: independently, so the total is up to 2 lamports more than a single ``ceil`` of 125 bps
#: would give. :func:`platform_fee` reproduces that component-wise rounding, which makes
#: the modelled fee equal to the mainnet fee on all three transactions rather than one
#: or two lamports light. The amount is trivial; being exactly right where being exactly
#: right is free is the point.
CURVE_PROTOCOL_FEE_BPS = 95
CURVE_CREATOR_FEE_BPS = 30
CURVE_TOTAL_FEE_BPS = CURVE_PROTOCOL_FEE_BPS + CURVE_CREATOR_FEE_BPS  # 125

#: How many accounts the protocol cut is split across, each rounded up separately.
#: MEASURED: two, on every observed transaction.
CURVE_PROTOCOL_FEE_SPLITS = 2

#: PumpSwap after graduation: a 25-tier schedule. The creator's cut starts at 95 bps
#: immediately after graduation and decays with market cap, so tier 1 is the *most*
#: expensive tier and therefore the one we charge. Total 120 bps.
PUMPSWAP_TIER1_PROTOCOL_FEE_BPS = 25
PUMPSWAP_TIER1_CREATOR_FEE_BPS = 95
PUMPSWAP_TIER1_TOTAL_FEE_BPS = PUMPSWAP_TIER1_PROTOCOL_FEE_BPS + PUMPSWAP_TIER1_CREATOR_FEE_BPS  # 120

#: Delay between the price we decide on and the price we would fill at. MEASURED: the
#: tier-1 safety scan path on this machine is 7.6 s (``docs/EDGE-AND-VARIABLES.md`` §2).
#: That measurement covers detect -> decide. The decide -> land leg (build, sign, submit,
#: confirm) is *not* measured here and is additional, so this figure is a floor, not an
#: estimate of the whole path.
DEFAULT_LATENCY_MS = 7_600

#: Window over which the token's own net SOL flow rate is measured, and the minimum
#: number of trades that makes the measurement worth using. INVENTED, both of them: 120 s
#: is short enough to describe the current minute of a launch and long enough to contain
#: trades at the observed 14 launches/min arrival rate; three trades is the smallest
#: sample that is not a single event.
DEFAULT_FLOW_WINDOW_S = 120
MIN_FLOW_SAMPLES = 3

#: How stale a stored curve snapshot may be before it stops being a price. DERIVED from
#: the observed reserve movement: ``3Gze…pump`` moved 1.03 SOL out of its curve in the
#: 231 s between two snapshots, so a minute-old snapshot is already a different curve.
DEFAULT_SNAPSHOT_MAX_AGE_S = 60

#: Everything a paper fill from this module does **not** charge. Copied onto every fill.
#:
#: The first item dominates and deserves a number. Between our decision and our fill,
#: other people's orders land: at 250 ms slots, a 7.6 s path is ~30 slots, and on a
#: pump.fun launch in its first minutes that is routinely tens of trades. We model the
#: *magnitude* of that flow (see :func:`measure_flow`) but not its *selection*: the
#: orders that land ahead of ours are not a random draw from the flow, they are
#: preferentially the ones reacting to the same thing we reacted to. Published estimates
#: of the leader-to-copier decay on exactly this mechanism are 14% -> 3%, i.e. most of
#: the move is gone. We charge none of that beyond the symmetric drift, so **a curve fill
#: from this module is still optimistic, and the residual is plausibly worth several
#: hundred bps on a contested entry.**
#: Non-venue costs a real fill pays that this module does not, **measured** on the three
#: mainnet buys in ``tests/fixtures/curve_price/onchain_fee_split.json`` rather than
#: guessed. Each of those trades also paid, beyond the curve leg and the 125 bps platform
#: fee:
#:
#: * a **bot-router fee of 0.001 SOL** on two of the three (GMGN's own recipient on one,
#:   another router's on another) — at a 0.05 SOL order that is **202 bps**;
#: * an extra 0.0005 SOL routing fee on one (101 bps);
#: * the Solana network fee, 5,000 to 106,806 lamports (1 to 21 bps);
#: * about 0.00151 SOL of associated-token-account rent, which is *recoverable* when the
#:   account is closed but is working capital while held (168 to 307 bps of the order).
#:
#: Our model charges a flat 1,000,000-lamport tip, which happens to be the same size as
#: one router fee. Netting the recoverable rent out, the gap between our total cost and
#: the real one on those three trades runs from **8 bps in our favour to 121 bps against
#: us**. Three transactions is not a distribution; it is an order of magnitude.
NOT_MODELLED_ROUTER_NOTE = (
    "router_and_rent: measured on three mainnet buys, a real retail fill also paid a 0.001 SOL "
    "bot-router fee (202 bps at 0.05 SOL), up to 0.0005 SOL of extra routing, the network fee, "
    "and ~0.00151 SOL of recoverable token-account rent. Against our flat 0.001 SOL tip the "
    "measured shortfall was -8 to +121 bps on n=3"
)

NOT_MODELLED: tuple[str, ...] = (
    "adverse_selection: other traders' orders landing between our decision and our fill are "
    "not modelled beyond a symmetric drift; they are not a random draw and likely cost "
    "several hundred bps on a contested entry",
    NOT_MODELLED_ROUTER_NOTE,
    "transaction_failure: a submitted swap that does not land costs the fee and the "
    "opportunity; every fill here succeeds",
    "partial_fill: the curve fills or it does not; a real router can return less",
    "mev_and_tip_competition: no tip auction, no bundle placement, no priority-fee race",
    "creator_fee_decay: the PumpSwap creator cut falls with market cap across 25 tiers; we "
    "always charge tier 1, which is the expensive end (conservative, but not exact)",
    "sol_usd_at_fill_time: USD figures use one SOL price for both legs of a round trip",
    "curve_completion_race: a buy that would cross the graduation boundary is capped at the "
    "remaining reserves, not split across the curve and the new pool",
)


class FillBasis(StrEnum):
    """Where a fill's price came from. Recorded on every order so analysis can split them.

    The three that can fill are deliberately distinct rather than collapsed into "priced":
    the whole point of recording the basis is to be able to ask later how far curve
    arithmetic and an executable router quote actually diverge, and that question is
    unanswerable if both are logged as the same thing.
    """

    #: Exact arithmetic on the bonding curve's reserves. Ground truth pre-graduation.
    CURVE = "curve"
    #: A routed, executable quote at our own size — Jupiter's ``round_trip``/``quote``,
    #: never a provider's ``priceImpactPct`` (see :data:`PRICE_IMPACT_PCT_WARNING`).
    ROUTER = "router"
    #: A pool mid price plus this broker's own constant-product depth approximation.
    DEX = "dex"
    #: We could not price it honestly and did not fill it.
    REFUSED = "refused"


#: Why no fill in this tree may be sized off a provider's own impact figure.
#:
#: MEASURED: in one Jupiter call BONK returned ``priceImpactPct`` of exactly 0 on a split
#: route while a curve token in the same call returned a real figure. Jupiter appears to
#: compute impact against a reference route and return exact zero when that degenerates.
#: An impact of exactly zero at a tradeable size is not a small number, it is a missing
#: one, and a paper fill that believed it would be free. Two quotes in opposite directions
#: — ``jupiter.round_trip`` — is the number that survives scrutiny, and it is also the
#: number a paper fill should be charged, because it contains the spread, both sides'
#: impact and the launchpad fee on each leg.
PRICE_IMPACT_PCT_WARNING = (
    "never size or fill off a provider's priceImpactPct: Jupiter returned exactly 0 for BONK "
    "on a split route in the same call where a curve token returned a real figure. Use a "
    "two-sided round trip (jupiter.round_trip) or exact curve arithmetic"
)


class DriftMode(StrEnum):
    """How the curve is advanced across the decision-to-fill delay."""

    #: Move the curve by the measured flow *magnitude*, in the direction that costs us.
    #: The default, because the sign of the next few seconds is what we do not know and
    #: the rule is to round against ourselves.
    ADVERSE = "adverse"
    #: Move the curve by the measured signed net flow. Truthful about the recent past,
    #: optimistic whenever the recent past happened to favour us.
    OBSERVED = "observed"
    #: No drift. Only honest when the fill is genuinely simultaneous with the quote.
    NONE = "none"


# --------------------------------------------------------------------------------------
# curve state
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurveState:
    """One bonding curve at one instant. Every field is an exact integer base unit.

    ``virtual_*`` are the constant-product reserves the program prices against;
    ``real_*`` are what is actually in the curve and therefore what bounds a fill.
    """

    virtual_sol: int
    virtual_token: int
    real_sol: int
    real_token: int
    observed_ms: int = 0
    source: str = "pumpfun"
    complete: bool = False

    # ------------------------------------------------------------------ construction

    @classmethod
    def build(
        cls,
        *,
        virtual_sol: int,
        virtual_token: int,
        real_sol: int,
        real_token: int,
        observed_ms: int = 0,
        source: str = "pumpfun",
        complete: bool = False,
    ) -> tuple[CurveState | None, str]:
        """Validate then construct. ``(None, reason)`` when the reserves are not a curve.

        The refusals are the scanner's, in the scanner's order, because a curve the
        scanner will not build a velocity from is not a curve we should trade against.
        """
        if complete:
            return None, "curve_complete"
        if virtual_token <= 0 or virtual_sol <= 0 or real_token < 0:
            return None, "reserves_nonpositive"
        if real_sol < 0 or real_sol >= virtual_sol:
            return None, "sol_reserves_inconsistent"
        if virtual_token - real_token != RESERVED_TOKEN_ATOMS:
            # The one invariant that held on every SOL-quoted coin observed. A payload
            # that breaks it is not a pump.fun curve we understand, and pricing against
            # it would be pricing against a shape we have never seen.
            return None, (
                f"reserved_token_invariant_violated:{virtual_token - real_token}"
                f"!={RESERVED_TOKEN_ATOMS}"
            )
        state = cls(
            virtual_sol=int(virtual_sol),
            virtual_token=int(virtual_token),
            real_sol=int(real_sol),
            real_token=int(real_token),
            observed_ms=int(observed_ms),
            source=source,
            complete=False,
        )
        if state.virtual_token_initial <= RESERVED_TOKEN_ATOMS:
            return None, "derived_initial_reserves_nonpositive"
        if state.graduation_sol <= 0:
            return None, "derived_graduation_nonpositive"
        return state, "ok"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, at_ms: int | None = None) -> tuple[
        CurveState | None, str
    ]:
        """Build from a pump.fun coin payload **via the scanner's own derivation**.

        The scanner is imported lazily: it imports the engine, which imports the paper
        broker, which imports this module. Doing it at module scope is an import cycle.
        """
        try:
            from kaiba.execution.scanner import curve_from_payload
        except Exception as exc:  # noqa: BLE001 - an unimportable scanner is not a price
            return None, f"scanner_unavailable:{type(exc).__name__}"
        curve, note = curve_from_payload(payload, at_ms=at_ms)
        if curve is None:
            return None, note
        return cls.build(
            virtual_sol=int(curve["virtual_sol_reserves"]),
            virtual_token=int(curve["virtual_token_reserves"]),
            real_sol=int(curve["sol_in_curve_lamports"]),
            real_token=int(curve["real_token_reserves"]),
            observed_ms=int(curve["observed_ms"]),
            source=str(curve.get("source") or "pumpfun"),
        )

    @classmethod
    def from_snapshot_row(cls, row: Mapping[str, Any]) -> tuple[CurveState | None, str]:
        """Build from a ``curve_snapshots`` row. Atoms are TEXT there; they exceed 2^63."""
        try:
            source = row["source"]
        except (KeyError, IndexError, TypeError):
            source = None
        try:
            return cls.build(
                virtual_sol=int(row["virtual_sol_lamports"]),
                virtual_token=int(row["virtual_token_atoms"]),
                real_sol=int(row["real_sol_lamports"]),
                real_token=int(row["real_token_atoms"]),
                observed_ms=int(row["observed_ms"]),
                source=str(source or "pumpfun"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            return None, f"snapshot_unreadable:{type(exc).__name__}"

    # ------------------------------------------------------------------ geometry

    @property
    def k(self) -> int:
        return self.virtual_sol * self.virtual_token

    @property
    def virtual_sol_initial(self) -> int:
        """This curve's own starting virtual SOL. 30e9 on a classic launch, not always."""
        return self.virtual_sol - self.real_sol

    @property
    def virtual_token_initial(self) -> Decimal:
        return Decimal(self.k) / Decimal(self.virtual_sol_initial)

    @property
    def real_token_initial(self) -> Decimal:
        return self.virtual_token_initial - Decimal(RESERVED_TOKEN_ATOMS)

    @property
    def graduation_sol(self) -> Decimal:
        """SOL raised when ``real_token`` hits zero, derived from this curve's own ``k``."""
        return (
            Decimal(self.k) / Decimal(RESERVED_TOKEN_ATOMS) - Decimal(self.virtual_sol_initial)
        ) / Decimal(LAMPORTS_PER_SOL)

    @property
    def progress_pct(self) -> Decimal:
        initial = self.real_token_initial
        if initial <= 0:
            return Decimal(0)
        pct = (initial - Decimal(self.real_token)) / initial * Decimal(100)
        return max(Decimal(0), min(Decimal(100), pct))

    @property
    def sol_in_curve(self) -> Decimal:
        return Decimal(self.real_sol) / Decimal(LAMPORTS_PER_SOL)

    @property
    def spot_lamports_per_atom(self) -> Decimal:
        """Marginal price. Not what a fill gets — that is worse by our own impact."""
        return Decimal(self.virtual_sol) / Decimal(self.virtual_token)

    def spot_price_usd(self, sol_usd: Decimal, decimals: int) -> Decimal:
        """Marginal price of one whole token in USD."""
        return (
            self.spot_lamports_per_atom
            * (Decimal(10) ** decimals)
            / Decimal(LAMPORTS_PER_SOL)
            * Decimal(sol_usd)
        )

    # ------------------------------------------------------------------ fills

    def sol_cost_for_tokens(self, atoms_out: int) -> int:
        """Lamports the program charges for exactly ``atoms_out`` tokens.

        This is the direction pump.fun's ``buy`` instruction actually runs in: the caller
        names a token amount and a maximum SOL cost, and the program computes the SOL.
        :meth:`buy_exact_in` is its exact inverse — ``buy_exact_in(B)`` returns the
        largest ``t`` with ``sol_cost_for_tokens(t) <= B``, which the algebra confirms and
        the recorded trades confirm again.
        """
        if atoms_out <= 0 or atoms_out >= self.virtual_token:
            return 0
        return self.k // (self.virtual_token - atoms_out) + 1 - self.virtual_sol

    def buy_exact_in(self, lamports_in: int) -> tuple[int, CurveState, bool]:
        """SOL into the curve -> token atoms out. pump.fun's own integer arithmetic.

        Returns ``(atoms_out, state_after, capped)``. ``capped`` means the order consumed
        the rest of the curve; pump.fun truncates such a buy at ``real_token_reserves``
        rather than filling past graduation.

        This is the **best obtainable** fill for the budget, and it is exact rather than
        approximate: it is the algebraic inverse of :meth:`sol_cost_for_tokens`. On the
        21 recorded mainnet trades in ``tests/fixtures/curve_price/real_trade_replay.json``
        it reproduces all 13 sells and 3 of 8 buys to the unit. The other 5 buys received
        up to 2.2e-6 *fewer* tokens than this returns, and the fixture pins why: in each
        case the trader's router asked for a round token amount and the program's own
        rounding charged one lamport more than that amount required. That one lamport
        bought nothing. So the residual is the real router leaving a fraction of a basis
        point on the table, not the model being wrong — but it does mean **a modelled buy
        is up to 0.022 bps better than the best real buy observed**, and that is stated
        rather than fudged, because 0.022 bps is noise beside a 125 bps fee and inventing
        a correction for it would be the more dishonest move.
        """
        if lamports_in <= 0:
            return 0, self, False
        new_virtual_sol = self.virtual_sol + lamports_in
        # ``+ 1`` reproduces the program rounding the remaining reserve *up*, which hands
        # the buyer one atom fewer. Dropping it would make every modelled buy better than
        # the real one by exactly the amount nobody notices.
        out = self.virtual_token - (self.k // new_virtual_sol + 1)
        out = max(0, out)
        capped = out > self.real_token
        if capped:
            out = self.real_token
        return (
            out,
            CurveState(
                virtual_sol=new_virtual_sol,
                virtual_token=self.virtual_token - out,
                real_sol=self.real_sol + lamports_in,
                real_token=self.real_token - out,
                observed_ms=self.observed_ms,
                source=self.source,
            ),
            capped,
        )

    def sell_exact_in(self, atoms_in: int) -> tuple[int, CurveState, bool]:
        """Token atoms into the curve -> SOL out, before fee. ``capped`` drained the curve."""
        if atoms_in <= 0:
            return 0, self, False
        out = (atoms_in * self.virtual_sol) // (self.virtual_token + atoms_in)
        out = max(0, out)
        capped = out > self.real_sol
        if capped:
            out = self.real_sol
        return (
            out,
            CurveState(
                virtual_sol=self.virtual_sol - out,
                virtual_token=self.virtual_token + atoms_in,
                real_sol=self.real_sol - out,
                real_token=self.real_token + atoms_in,
                observed_ms=self.observed_ms,
                source=self.source,
            ),
            capped,
        )

    def advance(self, net_lamports: int) -> CurveState:
        """Move the curve along by somebody else's net SOL flow. Clamped to the curve.

        This is how the latency delay is applied: positive is net buying (price up),
        negative is net selling. The token reserve follows from the constant product, so
        the advanced state is still an exact point on this curve, not an interpolation.
        """
        if net_lamports == 0:
            return self
        new_real_sol = self.real_sol + net_lamports
        if new_real_sol < 0:
            new_real_sol = 0
        new_virtual_sol = self.virtual_sol_initial + new_real_sol
        if new_virtual_sol <= 0:
            return self
        new_virtual_token = self.k // new_virtual_sol
        new_real_token = new_virtual_token - RESERVED_TOKEN_ATOMS
        if new_real_token < 0:
            # The drift would have graduated the token. Stop at the boundary rather than
            # inventing a curve that no longer exists.
            new_real_token = 0
            new_virtual_token = RESERVED_TOKEN_ATOMS
            new_virtual_sol = max(1, self.k // new_virtual_token)
            new_real_sol = max(0, new_virtual_sol - self.virtual_sol_initial)
        return CurveState(
            virtual_sol=new_virtual_sol,
            virtual_token=new_virtual_token,
            real_sol=new_real_sol,
            real_token=new_real_token,
            observed_ms=self.observed_ms,
            source=self.source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "virtual_sol": self.virtual_sol,
            "virtual_token": self.virtual_token,
            "real_sol": self.real_sol,
            "real_token": self.real_token,
            "observed_ms": self.observed_ms,
            "source": self.source,
            "graduation_sol": str(self.graduation_sol),
            "progress_pct": str(self.progress_pct),
        }


# --------------------------------------------------------------------------------------
# flow rate, for the latency drift
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowRate:
    """How fast this token's curve is moving, measured from its own trades.

    ``basis`` is the provenance and is recorded on the fill. ``unavailable`` means no
    drift was applied, which is the optimistic case and must be visible as such.
    """

    lamports_per_s: Decimal | None = None
    basis: str = "unavailable"
    window_s: Decimal | None = None
    samples: int = 0
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.lamports_per_s is not None


def measure_flow(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    at_ms: int | None = None,
    window_s: int = DEFAULT_FLOW_WINDOW_S,
) -> FlowRate:
    """Net lamports per second into this token's curve, from our own ``swaps`` rows.

    Falls back to the delta between two ``curve_snapshots``, which is a coarser but still
    entirely observed figure. Never raises and never guesses: no data is
    ``basis="unavailable"`` and no drift, not a default rate.
    """
    ts = int(at_ms if at_ms is not None else now_ms())
    since = ts - int(window_s) * 1000
    try:
        rows = fetch_all(
            conn,
            "SELECT ts_ms, side, amount_native FROM swaps WHERE chain=? AND token=? "
            "AND ts_ms>=? AND ts_ms<=? AND amount_native IS NOT NULL ORDER BY ts_ms",
            (chain.value, token, since, ts),
        )
    except sqlite3.Error as exc:  # noqa: BLE001 - a missing table is not a rate
        return FlowRate(note=f"swaps_unreadable:{type(exc).__name__}: {exc}"[:120])

    if len(rows) >= MIN_FLOW_SAMPLES:
        first, last = int(rows[0]["ts_ms"]), int(rows[-1]["ts_ms"])
        elapsed_ms = last - first
        if elapsed_ms >= 1000:
            net = 0
            for row in rows:
                try:
                    amount = int(row["amount_native"])
                except (TypeError, ValueError):
                    continue
                net += amount if str(row["side"]) == "buy" else -amount
            return FlowRate(
                lamports_per_s=Decimal(net) / (Decimal(elapsed_ms) / Decimal(1000)),
                basis="swaps",
                window_s=Decimal(elapsed_ms) / Decimal(1000),
                samples=len(rows),
            )

    snaps = _recent_snapshots(chain, token, conn, at_ms=ts, limit=2)
    if len(snaps) == 2:
        newer, older = snaps[0], snaps[1]
        elapsed_ms = int(newer["observed_ms"]) - int(older["observed_ms"])
        if elapsed_ms >= 1000:
            delta = int(newer["real_sol_lamports"]) - int(older["real_sol_lamports"])
            return FlowRate(
                lamports_per_s=Decimal(delta) / (Decimal(elapsed_ms) / Decimal(1000)),
                basis="curve_snapshots",
                window_s=Decimal(elapsed_ms) / Decimal(1000),
                samples=2,
            )
    return FlowRate(note="no trades and no snapshot pair inside the window")


def drift_for(
    flow: FlowRate | None,
    *,
    side: Side,
    latency_ms: int,
    mode: DriftMode = DriftMode.ADVERSE,
) -> tuple[int, str]:
    """Lamports the curve moves between our decision and our fill. ``(delta, basis)``.

    ``ADVERSE`` takes the **magnitude** of the measured flow and applies it in the
    direction that costs us: a buy fills into a curve that has risen, a sell into one
    that has fallen. This is not pessimism for its own sake — it is the only assumption
    that does not quietly credit us for guessing the sign of the next 7.6 seconds.
    """
    if mode is DriftMode.NONE or latency_ms <= 0:
        return 0, "none"
    if flow is None or not flow.known or flow.lamports_per_s is None:
        return 0, f"unavailable:{(flow.note if flow else 'no flow measured')}"[:120]
    seconds = Decimal(int(latency_ms)) / Decimal(1000)
    moved = flow.lamports_per_s * seconds
    if mode is DriftMode.OBSERVED:
        return int(moved.to_integral_value()), f"observed:{flow.basis}"
    magnitude = abs(moved)
    signed = magnitude if side is Side.BUY else -magnitude
    return int(signed.to_integral_value()), f"adverse:{flow.basis}"


def _recent_snapshots(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    at_ms: int,
    limit: int = 1,
) -> list[dict[str, Any]]:
    try:
        return fetch_all(
            conn,
            "SELECT * FROM curve_snapshots WHERE chain=? AND token=? AND observed_ms<=? "
            "ORDER BY observed_ms DESC LIMIT ?",
            (chain.value, token, int(at_ms), int(limit)),
        )
    except sqlite3.Error:
        return []


# --------------------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurveFill:
    """One modelled fill, with every component of its price separated.

    ``ok`` false means we refused. A refusal carries the same provenance as a fill so the
    reason a token could not be traded is as recoverable as the price of one that could.
    """

    ok: bool
    basis: FillBasis
    side: Side
    venue: str = "pumpfun-curve"
    reason: str | None = None

    amount_in: int = 0  # lamports on a buy, atoms on a sell
    amount_out: int = 0  # atoms on a buy, lamports on a sell (net of fee)
    curve_in: int = 0  # what actually reached the curve, after/before fee
    curve_out: int = 0  # what the curve returned, before fee
    capped_at_reserves: bool = False

    fee_native: int = 0  # lamports
    fee_bps: int = 0
    fee_protocol_native: int = 0
    fee_creator_native: int = 0

    decimals: int = 0
    sol_usd: Decimal | None = None
    decision_price_usd: Decimal | None = None  # marginal price at the observed state
    spot_price_usd: Decimal | None = None  # marginal price at the state we fill into
    effective_price_usd: Decimal | None = None  # what we actually paid/received per token

    impact_bps: int = 0  # our own order, exactly
    latency_bps: int = 0  # the curve moving under us during the delay
    total_cost_bps: int = 0  # fee + impact + latency, vs the decision price

    latency_ms: int = 0
    drift_lamports: int = 0
    drift_basis: str = "none"
    flow_basis: str = "unavailable"

    state_decided: CurveState | None = None
    state_filled_from: CurveState | None = None
    state_after: CurveState | None = None
    not_modelled: tuple[str, ...] = field(default_factory=lambda: NOT_MODELLED)

    def as_dict(self) -> dict[str, Any]:
        """Compact, JSON-safe provenance. This is what lands in ``order_events.detail``."""
        return {
            "ok": self.ok,
            "basis": self.basis.value,
            "side": self.side.value,
            "venue": self.venue,
            "reason": self.reason,
            "amount_in": str(self.amount_in),
            "amount_out": str(self.amount_out),
            "curve_in": str(self.curve_in),
            "curve_out": str(self.curve_out),
            "capped_at_reserves": self.capped_at_reserves,
            "fee_native": str(self.fee_native),
            "fee_bps": self.fee_bps,
            "fee_protocol_native": str(self.fee_protocol_native),
            "fee_creator_native": str(self.fee_creator_native),
            "decision_price_usd": _s(self.decision_price_usd),
            "spot_price_usd": _s(self.spot_price_usd),
            "effective_price_usd": _s(self.effective_price_usd),
            "impact_bps": self.impact_bps,
            "latency_bps": self.latency_bps,
            "total_cost_bps": self.total_cost_bps,
            "latency_ms": self.latency_ms,
            "drift_lamports": str(self.drift_lamports),
            "drift_basis": self.drift_basis,
            "flow_basis": self.flow_basis,
            "state_decided": self.state_decided.as_dict() if self.state_decided else None,
            "state_after": self.state_after.as_dict() if self.state_after else None,
            "not_modelled": list(self.not_modelled),
        }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _bps_between(from_price: Decimal | None, to_price: Decimal | None) -> int:
    """Signed bps move from one price to another, truncated toward zero."""
    if from_price is None or to_price is None or from_price <= 0:
        return 0
    try:
        return int((to_price - from_price) / from_price * BPS)
    except (ArithmeticError, InvalidOperation):
        return 0


def platform_fee(
    curve_leg_lamports: int,
    *,
    fee_bps: int = CURVE_TOTAL_FEE_BPS,
    creator_fee_bps: int = CURVE_CREATOR_FEE_BPS,
    protocol_splits: int = CURVE_PROTOCOL_FEE_SPLITS,
) -> tuple[int, int, int]:
    """``(total, protocol, creator)`` lamports on a curve leg. Component-wise ceiling.

    Reproduces what mainnet actually charges: each recipient's share is computed and
    rounded up on its own, so the total is up to ``protocol_splits + 1`` lamports above a
    single rounded 125 bps. Verified to the lamport against all three transactions in
    ``tests/fixtures/curve_price/onchain_fee_split.json``.
    """
    leg = max(0, int(curve_leg_lamports))
    if leg == 0:
        return 0, 0, 0
    protocol_bps = max(0, int(fee_bps) - int(creator_fee_bps))
    n = max(1, int(protocol_splits))
    creator = -(-leg * int(creator_fee_bps) // 10_000)
    protocol = -(-leg * protocol_bps // (n * 10_000)) * n
    return protocol + creator, protocol, creator


def curve_leg_for_budget(
    budget_lamports: int,
    *,
    fee_bps: int = CURVE_TOTAL_FEE_BPS,
    creator_fee_bps: int = CURVE_CREATOR_FEE_BPS,
    protocol_splits: int = CURVE_PROTOCOL_FEE_SPLITS,
) -> int:
    """Largest curve leg whose fee still fits inside ``budget_lamports``.

    The exact inverse of ``leg -> leg + platform_fee(leg)``. It is a search rather than a
    division because the fee's component-wise rounding is not invertible in closed form;
    the search converges in one or two steps from the divided estimate, and it always
    lands on a leg the venue would actually accept for that budget.
    """
    budget = int(budget_lamports)
    if budget <= 0:
        return 0
    kw = {"fee_bps": fee_bps, "creator_fee_bps": creator_fee_bps, "protocol_splits": protocol_splits}
    leg = budget * 10_000 // (10_000 + int(fee_bps))
    for _ in range(64):
        if leg <= 0:
            return 0
        if leg + platform_fee(leg, **kw)[0] > budget:
            leg -= 1
            continue
        break
    for _ in range(64):
        if leg + 1 + platform_fee(leg + 1, **kw)[0] > budget:
            break
        leg += 1
    return max(0, leg)


def refusal(side: Side, reason: str, *, venue: str = "pumpfun-curve") -> CurveFill:
    """A fill we will not price. The whole point of the module is that this stays possible."""
    return CurveFill(ok=False, basis=FillBasis.REFUSED, side=side, venue=venue, reason=reason)


def quote_buy(
    state: CurveState,
    lamports_budget: int,
    *,
    sol_usd: Decimal,
    decimals: int,
    fee_bps: int = CURVE_TOTAL_FEE_BPS,
    creator_fee_bps: int = CURVE_CREATOR_FEE_BPS,
    latency_ms: int = DEFAULT_LATENCY_MS,
    flow: FlowRate | None = None,
    drift_mode: DriftMode = DriftMode.ADVERSE,
    venue: str = "pumpfun-curve",
) -> CurveFill:
    """Buy with a total SOL budget. The fee comes out of the budget; the curve gets the rest.

    ``lamports_budget`` is everything we are willing to spend at the venue, exclusive of
    the network tip (which is not a venue cost and is charged by the broker). The split is
    :func:`curve_leg_for_budget`, the exact inverse of the fee mainnet actually charges,
    so the leg is the largest one the venue would accept for that budget and never a
    lamport more.
    """
    if lamports_budget <= 0:
        return refusal(Side.BUY, "no_size", venue=venue)
    if sol_usd is None or Decimal(sol_usd) <= 0:
        return refusal(Side.BUY, "no_native_usd_price", venue=venue)
    if state.real_token <= 0:
        return refusal(Side.BUY, "curve_has_no_tokens_left", venue=venue)

    sol_usd = Decimal(sol_usd)
    decision_price = state.spot_price_usd(sol_usd, decimals)

    drift, drift_basis = drift_for(flow, side=Side.BUY, latency_ms=latency_ms, mode=drift_mode)
    filled_from = state.advance(drift)
    if filled_from.real_token <= 0:
        return refusal(Side.BUY, "curve_graduated_during_latency", venue=venue)
    spot_price = filled_from.spot_price_usd(sol_usd, decimals)

    curve_in = curve_leg_for_budget(
        lamports_budget, fee_bps=fee_bps, creator_fee_bps=creator_fee_bps
    )
    if curve_in <= 0:
        return refusal(Side.BUY, "fee_exceeds_size", venue=venue)
    fee, protocol_fee, creator_fee = platform_fee(
        curve_in, fee_bps=fee_bps, creator_fee_bps=creator_fee_bps
    )

    atoms_out, after, capped = filled_from.buy_exact_in(curve_in)
    if atoms_out <= 0:
        return refusal(Side.BUY, "fill_rounds_to_zero", venue=venue)

    # The budget the venue actually consumes is leg + fee; any remainder the rounding
    # left over is not spent, so the effective price is computed on what we really paid.
    spent = curve_in + fee
    effective = (
        Decimal(spent)
        / Decimal(LAMPORTS_PER_SOL)
        * sol_usd
        / (Decimal(atoms_out) / (Decimal(10) ** decimals))
    )
    return CurveFill(
        ok=True,
        basis=FillBasis.CURVE,
        side=Side.BUY,
        venue=venue,
        amount_in=spent,
        amount_out=atoms_out,
        curve_in=curve_in,
        curve_out=atoms_out,
        capped_at_reserves=capped,
        fee_native=fee,
        fee_bps=int(fee_bps),
        fee_protocol_native=protocol_fee,
        fee_creator_native=creator_fee,
        decimals=decimals,
        sol_usd=sol_usd,
        decision_price_usd=decision_price,
        spot_price_usd=spot_price,
        effective_price_usd=effective,
        impact_bps=_bps_between(spot_price, effective),
        latency_bps=_bps_between(decision_price, spot_price),
        total_cost_bps=_bps_between(decision_price, effective),
        latency_ms=int(latency_ms),
        drift_lamports=drift,
        drift_basis=drift_basis,
        flow_basis=flow.basis if flow else "unavailable",
        state_decided=state,
        state_filled_from=filled_from,
        state_after=after,
    )


def quote_sell(
    state: CurveState,
    atoms_in: int,
    *,
    sol_usd: Decimal,
    decimals: int,
    fee_bps: int = CURVE_TOTAL_FEE_BPS,
    creator_fee_bps: int = CURVE_CREATOR_FEE_BPS,
    latency_ms: int = DEFAULT_LATENCY_MS,
    flow: FlowRate | None = None,
    drift_mode: DriftMode = DriftMode.ADVERSE,
    venue: str = "pumpfun-curve",
) -> CurveFill:
    """Sell token atoms into the curve. The fee is taken out of the SOL the curve returns."""
    if atoms_in <= 0:
        return refusal(Side.SELL, "nothing_to_sell", venue=venue)
    if sol_usd is None or Decimal(sol_usd) <= 0:
        return refusal(Side.SELL, "no_native_usd_price", venue=venue)
    if state.real_sol <= 0:
        return refusal(Side.SELL, "curve_has_no_sol_left", venue=venue)

    sol_usd = Decimal(sol_usd)
    decision_price = state.spot_price_usd(sol_usd, decimals)

    drift, drift_basis = drift_for(flow, side=Side.SELL, latency_ms=latency_ms, mode=drift_mode)
    filled_from = state.advance(drift)
    if filled_from.real_sol <= 0:
        return refusal(Side.SELL, "curve_drained_during_latency", venue=venue)
    spot_price = filled_from.spot_price_usd(sol_usd, decimals)

    gross, after, capped = filled_from.sell_exact_in(atoms_in)
    if gross <= 0:
        return refusal(Side.SELL, "fill_rounds_to_zero", venue=venue)

    # Component-wise ceiling, the same rounding mainnet applies on the way in.
    fee, protocol_fee, creator_fee = platform_fee(
        gross, fee_bps=fee_bps, creator_fee_bps=creator_fee_bps
    )
    net = gross - fee
    if net <= 0:
        return refusal(Side.SELL, "fee_exceeds_proceeds", venue=venue)

    effective = (
        Decimal(net)
        / Decimal(LAMPORTS_PER_SOL)
        * sol_usd
        / (Decimal(atoms_in) / (Decimal(10) ** decimals))
    )
    return CurveFill(
        ok=True,
        basis=FillBasis.CURVE,
        side=Side.SELL,
        venue=venue,
        amount_in=atoms_in,
        amount_out=net,
        curve_in=atoms_in,
        curve_out=gross,
        capped_at_reserves=capped,
        fee_native=fee,
        fee_bps=int(fee_bps),
        fee_protocol_native=protocol_fee,
        fee_creator_native=creator_fee,
        decimals=decimals,
        sol_usd=sol_usd,
        decision_price_usd=decision_price,
        spot_price_usd=spot_price,
        effective_price_usd=effective,
        # On a sell, "worse" is a lower price, so both moves are negative and the sign is
        # kept rather than flipped: a reader must be able to see the direction.
        impact_bps=_bps_between(spot_price, effective),
        latency_bps=_bps_between(decision_price, spot_price),
        total_cost_bps=_bps_between(decision_price, effective),
        latency_ms=int(latency_ms),
        drift_lamports=drift,
        drift_basis=drift_basis,
        flow_basis=flow.basis if flow else "unavailable",
        state_decided=state,
        state_filled_from=filled_from,
        state_after=after,
    )


# --------------------------------------------------------------------------------------
# resolving a curve for a token
# --------------------------------------------------------------------------------------

#: What the broker calls to find a curve: ``(chain, token, at_ms) -> (state | None, note)``.
CurveResolver = Callable[[Chain, str, int], tuple["CurveState | None", str]]


def snapshot_resolver(
    conn: sqlite3.Connection,
    *,
    max_age_s: int = DEFAULT_SNAPSHOT_MAX_AGE_S,
) -> CurveResolver:
    """Resolve from ``curve_snapshots``. Offline, deterministic, and refuses when stale.

    A stale snapshot is refused rather than used because the reserves are the price: on
    the tokens in this database the curve moved by whole SOL inside a few minutes, so an
    old snapshot is not an old price, it is a different token's price.
    """

    def resolve(chain: Chain, token: str, at_ms: int) -> tuple[CurveState | None, str]:
        rows = _recent_snapshots(chain, token, conn, at_ms=at_ms, limit=1)
        if not rows:
            return None, "no_curve_snapshot"
        row = rows[0]
        age_s = (int(at_ms) - int(row["observed_ms"])) / 1000
        if max_age_s is not None and age_s > max_age_s:
            return None, f"curve_snapshot_stale:{age_s:.0f}s>{max_age_s}s"
        state, note = CurveState.from_snapshot_row(row)
        return state, note

    return resolve


def live_resolver(
    conn: sqlite3.Connection | None = None,
    *,
    fallback: CurveResolver | None = None,
    priority: Any = None,
) -> CurveResolver:
    """Resolve by asking pump.fun for the coin right now, falling back to a snapshot.

    Network, so it is never the default: a paper broker that reaches out on every fill
    would make the shadow record depend on provider availability. This is the resolver
    the live end-to-end proof and a ``kaiba paper buy`` command want.
    """

    def resolve(chain: Chain, token: str, at_ms: int) -> tuple[CurveState | None, str]:
        if chain is not Chain.SOL:
            return (fallback(chain, token, at_ms) if fallback else (None, "curve_is_solana_only"))
        try:
            from kaiba.execution.scanner import fetch_curve_payload
        except Exception as exc:  # noqa: BLE001 - keep the broker alive
            return None, f"scanner_unavailable:{type(exc).__name__}"
        try:
            # ``priority`` is the limiter priority the read competes at. None keeps the
            # scanner's default (DISCOVERY). The exit watchdog passes Priority.EXIT: the
            # read that decides whether to close a position must outrank the discovery
            # work sharing its budget. Before 2026-09-22 every curve read the watchdog made
            # went out at DISCOVERY, behind the scan it was protecting against.
            if priority is None:
                payload, _receipt = fetch_curve_payload(token, conn)
            else:
                payload, _receipt = fetch_curve_payload(token, conn, priority=priority)
        except Exception as exc:  # noqa: BLE001 - a dead provider is not a price
            return None, f"curve_fetch_failed:{type(exc).__name__}"
        if not payload:
            return fallback(chain, token, at_ms) if fallback else (None, "curve_unavailable")
        state, note = CurveState.from_payload(payload, at_ms=at_ms)
        if state is None and fallback is not None:
            other, other_note = fallback(chain, token, at_ms)
            if other is not None:
                return other, other_note
        return state, note

    return resolve


def chain_resolver(*resolvers: CurveResolver) -> CurveResolver:
    """First resolver that produces a curve wins; the last refusal is what gets reported."""

    def resolve(chain: Chain, token: str, at_ms: int) -> tuple[CurveState | None, str]:
        note = "no_curve_resolver"
        for resolver in resolvers:
            state, note = resolver(chain, token, at_ms)
            if state is not None:
                return state, note
        return None, note

    return resolve


def has_graduated(chain: Chain, token: str, conn: sqlite3.Connection) -> bool:
    """``tokens.migrated_ms`` is set. After migration the curve is gone and DEX is right."""
    try:
        row = fetch_one(
            conn,
            "SELECT migrated_ms FROM tokens WHERE chain=? AND address=?",
            (chain.value, token),
        )
    except sqlite3.Error:
        return False
    return bool(row and row["migrated_ms"])


# --------------------------------------------------------------------------------------
# a price source the exit watchdog can use
# --------------------------------------------------------------------------------------


class CurvePriceSource:
    """``kaiba.execution.watchdog.PriceSource`` backed by the bonding curve.

    The watchdog is blind on every pre-graduation token because its only quote stack is
    DexScreener, which correctly has no pair for a token that has none. This closes that
    hole without touching the watchdog: it is the documented injection point.

    ``liquidity_usd`` is reported as the SOL **actually in the curve**, not a two-sided
    pool figure. That is the exact maximum extractable amount, and it is deliberately the
    smaller convention: it makes the DEX fallback path model *more* impact, never less.
    The curve path does not use it at all — it prices from the reserves.
    """

    name = "curve"

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        sol_usd: Decimal | Callable[[], Decimal | None],
        resolver: CurveResolver | None = None,
        decimals_default: int = 6,
        fallback: Any | None = None,
    ) -> None:
        self.conn = conn
        self._sol_usd = sol_usd
        self.resolver = resolver or snapshot_resolver(conn)
        self.decimals_default = int(decimals_default)
        #: Another ``PriceSource`` consulted when there is no curve — a graduated token.
        self.fallback = fallback

    def sol_usd(self) -> Decimal | None:
        value = self._sol_usd() if callable(self._sol_usd) else self._sol_usd
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return parsed if parsed > 0 else None

    def _decimals(self, chain: Chain, token: str) -> int:
        try:
            row = fetch_one(
                self.conn,
                "SELECT decimals FROM tokens WHERE chain=? AND address=?",
                (chain.value, token),
            )
        except sqlite3.Error:
            row = None
        if row and row["decimals"] is not None:
            return int(row["decimals"])
        return self.decimals_default

    def quote(self, chain: Chain, token: str) -> Any:
        from kaiba.core.schemas import EvidenceBasis
        from kaiba.execution.watchdog import PriceQuote

        def _fallback(note: str) -> Any:
            if self.fallback is None:
                return PriceQuote.unavailable(note, self.name)
            try:
                got = self.fallback.quote(chain, token)
            except Exception as exc:  # noqa: BLE001 - a dead fallback is blindness
                return PriceQuote.unavailable(
                    f"{note}; fallback raised {type(exc).__name__}", self.name
                )
            if getattr(got, "usable", False):
                return got
            # The fallback refused too, so this position is blind and the operator is about
            # to read ``quote.note`` as the ``reason`` on ``protection_blind``. Carry BOTH
            # refusals: on 2026-09-22 four live positions reported only the DEX's
            # "provider returned no price" for hours, and the curve's own
            # ``no curve: non_sol_quote:Dz9mQ9NzkBcC`` -- the whole diagnosis -- was thrown
            # away right here. Rebuilt as a PriceQuote rather than returned as-is, because
            # a fallback may hand back a stand-in the watchdog would reject outright.
            beneath = getattr(got, "note", None) or getattr(got, "source", None) or "unavailable"
            return PriceQuote.unavailable(f"{note}; {beneath}"[:300], self.name)

        # A recorded graduation is an immutable routing fact, not a stale price.
        # Re-polling Pump.fun for migrated or known foreign-launchpad tokens on every
        # watchdog tick spends exit capacity without producing a usable curve.
        try:
            route = fetch_one(
                self.conn,
                "SELECT launchpad, migrated_ms FROM tokens WHERE chain=? AND address=?",
                (chain.value, token),
            )
        except sqlite3.Error:
            route = None
        if route and chain is Chain.SOL:
            migrated = route["migrated_ms"]
            if migrated is not None and 0 < int(migrated) <= now_ms():
                return _fallback("recorded graduation; use the pool/router price")
            if str(route["launchpad"] or "").lower() in {
                "meteora_virtual_curve", "meteora_dbc", "ray_launchpad",
                "raydium_launchpad", "stonkfun", "letsbonk.fun",
            }:
                return _fallback("known non-Pump venue; use the pool/router price")

        native = self.sol_usd()
        if native is None:
            return _fallback("no SOL/USD price; a curve in lamports cannot become USD")
        state, note = self.resolver(chain, token, now_ms())
        if state is None:
            return _fallback(f"no curve: {note}")
        decimals = self._decimals(chain, token)
        price = state.spot_price_usd(native, decimals)
        if price <= 0:
            return _fallback("curve priced to zero")
        return PriceQuote(
            price_usd=price,
            liquidity_usd=state.sol_in_curve * native,
            basis=EvidenceBasis.DERIVED,
            observed_ms=state.observed_ms or now_ms(),
            source=f"{self.name}:{state.source}",
            note=f"bonding curve, progress {state.progress_pct:.2f}%",
        )


# --------------------------------------------------------------------------------------
# cross-checking the arithmetic against an executable quote
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurveVsRouter:
    """How far our curve arithmetic ran from a real executable quote at the same size.

    This is the instrument for the open question the basis field exists to answer. It is
    never on the fill path — it costs two network calls per token against a ~1 rps ceiling
    — but it is what an audit runs to find out whether the arithmetic is drifting.
    """

    token: str
    lamports_in: int
    curve_atoms_out: int | None = None
    router_atoms_out: int | None = None
    gap_bps: int | None = None
    router_round_trip_bps: int | None = None
    curve_round_trip_bps: int | None = None
    route_label: str = "none"
    on_bonding_curve: bool | None = None
    note: str | None = None

    @property
    def known(self) -> bool:
        return self.gap_bps is not None


def compare_with_router(
    state: CurveState,
    token: str,
    lamports_in: int,
    *,
    conn: sqlite3.Connection | None = None,
    fee_bps: int = CURVE_TOTAL_FEE_BPS,
) -> CurveVsRouter:
    """Quote the same buy both ways and report the gap. Network; never raises.

    ``curve_atoms_out`` is what our arithmetic says the *curve leg* returns for the same
    SOL the router would put through it, so the two numbers are comparable: Jupiter's
    output is already net of the launchpad fee, so the curve leg is taken from the same
    fee-inclusive budget.
    """
    try:
        from kaiba.providers import jupiter
    except Exception as exc:  # noqa: BLE001 - an absent provider is not a discrepancy
        return CurveVsRouter(token, lamports_in, note=f"jupiter_unavailable:{type(exc).__name__}")
    try:
        trip = jupiter.round_trip(token, lamports_in, conn=conn)
    except Exception as exc:  # noqa: BLE001 - a provider outage is data
        return CurveVsRouter(token, lamports_in, note=f"jupiter_raised:{type(exc).__name__}")

    curve_in = curve_leg_for_budget(lamports_in, fee_bps=fee_bps)
    curve_out, after, _ = state.buy_exact_in(curve_in)
    back, _, _ = after.sell_exact_in(curve_out)
    back_fee = (back * int(fee_bps) + 9_999) // 10_000
    curve_rt = (
        int((Decimal(lamports_in - (back - back_fee)) / Decimal(lamports_in)) * BPS)
        if lamports_in > 0
        else None
    )
    if not trip.buy.ok or trip.buy.amount_out is None:
        return CurveVsRouter(
            token, lamports_in, curve_atoms_out=curve_out, curve_round_trip_bps=curve_rt,
            route_label=trip.buy.route_label, note=f"router:{trip.buy.status.value}:{trip.buy.note}",
        )
    router_out = int(trip.buy.amount_out)
    gap = int((Decimal(curve_out - router_out) / Decimal(router_out)) * BPS) if router_out else None
    return CurveVsRouter(
        token=token,
        lamports_in=lamports_in,
        curve_atoms_out=curve_out,
        router_atoms_out=router_out,
        gap_bps=gap,
        router_round_trip_bps=trip.cost_bps,
        curve_round_trip_bps=curve_rt,
        route_label=trip.buy.route_label,
        on_bonding_curve=trip.buy.on_bonding_curve,
    )


def sol_usd_from_native_price(conn: sqlite3.Connection | None = None) -> Decimal | None:
    """SOL/USD from ``kaiba.providers.native_price``, or ``None``. Never raises.

    Imported lazily and defensively on purpose. That provider is owned by another lane
    and is landing in parallel, and the whole point of this module is that the paper loop
    keeps working when a dependency is absent — absent means *blind*, which the watchdog
    already handles loudly, not *crashed*.
    """
    try:
        from kaiba.core.schemas import Chain as _Chain
        from kaiba.providers import native_price
    except Exception as exc:  # noqa: BLE001 - an absent provider is not an error here
        log.debug("native_price unavailable: %s", exc)
        return None
    for attempt in ("latest", "sample"):
        try:
            found = getattr(native_price, attempt)(_Chain.SOL, conn) if attempt == "latest" else None
        except TypeError:
            try:
                found = getattr(native_price, attempt)(_Chain.SOL)
            except Exception:  # noqa: BLE001
                continue
        except Exception as exc:  # noqa: BLE001 - a provider outage is data, not a crash
            log.debug("native_price.%s raised: %s", attempt, exc)
            continue
        value = getattr(found, "price_usd", None) if found is not None else None
        if value is None:
            continue
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if parsed > 0:
            return parsed
    return None


def curve_price_source(*, priority: Any = None) -> CurvePriceSource:
    """Zero-argument factory for ``kaiba.execution.watchdog.PRICE_SOURCES``.

    **Wiring requested** (one line, in ``watchdog.PRICE_SOURCES``; that file is owned
    elsewhere so it is not edited here)::

        "curve": lambda: __import__(
            "kaiba.execution.curve_price", fromlist=["x"]
        ).curve_price_source(),

    or, with a normal import at the top of ``watchdog.py``::

        from kaiba.execution.curve_price import curve_price_source
        PRICE_SOURCES["curve"] = curve_price_source

    Then ``protection.price_source: curve`` in ``config/risk.yaml`` makes the exit
    watchdog able to see a pre-graduation token for the first time. It prices from the
    bonding curve while the token is on one and hands over to the DEX quote stack
    afterwards, so one setting covers both sides of graduation.

    Nothing here touches GMGN: the reserves come from pump.fun's free keyless coin route
    (or from our own stored ``curve_snapshots``), and the graduated fallback is the
    existing DexScreener adapter.

    **This source is Solana only, and that is now a limitation rather than a scope.**
    ``resolve(chain, ...)`` returns ``curve_is_solana_only`` for every EVM chain, so with
    ``price_source: curve`` the watchdog is still blind on BSC and Robinhood — DexScreener
    priced 1 of 18 live ``robinhood`` tokens on 2026-09-21, and that one at $1 of
    liquidity. :mod:`kaiba.execution.evm_price` closes that hole for Flap (BSC) and Pons
    (Robinhood) with the same design, and
    :func:`kaiba.execution.evm_price.venue_price_source` composes the two behind one
    setting: it routes Solana to *this* source unchanged and EVM to the venue readers.
    Prefer ``price_source: venue`` over ``price_source: curve`` once that is wired —
    ``venue`` is a strict superset and nothing about the Solana path changes.
    """
    from kaiba.core.db import get_conn

    conn = get_conn()
    fallback: Any | None
    try:
        from kaiba.execution.watchdog import ProviderPriceSource

        fallback = ProviderPriceSource()
    except Exception as exc:  # noqa: BLE001 - no fallback is blindness after graduation
        log.warning("no DEX fallback for the curve price source: %s", exc)
        fallback = None
    return CurvePriceSource(
        conn,
        sol_usd=lambda: sol_usd_from_native_price(conn),
        resolver=chain_resolver(live_resolver(conn, priority=priority), snapshot_resolver(conn)),
        fallback=fallback,
    )


__all__ = [
    "BPS",
    "CURVE_CREATOR_FEE_BPS",
    "CURVE_PROTOCOL_FEE_BPS",
    "CURVE_TOTAL_FEE_BPS",
    "DEFAULT_FLOW_WINDOW_S",
    "DEFAULT_LATENCY_MS",
    "DEFAULT_SNAPSHOT_MAX_AGE_S",
    "LAMPORTS_PER_SOL",
    "MIN_FLOW_SAMPLES",
    "NOT_MODELLED",
    "NOT_MODELLED_ROUTER_NOTE",
    "PRICE_IMPACT_PCT_WARNING",
    "PUMPSWAP_TIER1_CREATOR_FEE_BPS",
    "PUMPSWAP_TIER1_PROTOCOL_FEE_BPS",
    "PUMPSWAP_TIER1_TOTAL_FEE_BPS",
    "RESERVED_TOKEN_ATOMS",
    "CurveFill",
    "CurvePriceSource",
    "CurveResolver",
    "CurveState",
    "CurveVsRouter",
    "CURVE_PROTOCOL_FEE_SPLITS",
    "DriftMode",
    "FillBasis",
    "FlowRate",
    "chain_resolver",
    "compare_with_router",
    "curve_price_source",
    "drift_for",
    "has_graduated",
    "live_resolver",
    "curve_leg_for_budget",
    "measure_flow",
    "platform_fee",
    "quote_buy",
    "quote_sell",
    "refusal",
    "snapshot_resolver",
    "sol_usd_from_native_price",
]
