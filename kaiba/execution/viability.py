"""Position sizing economics: the band a trade can be economic in, or the refusal.

Round-trip cost has three terms and they do not point the same way:

* **flat** — a router tip plus account rent on Solana, gas on an EVM chain. Does not
  move with size at all, so as a *share* of the position it falls as 1/size. At 0.02 SOL
  it is ~9.9% of a round trip; at 0.1 SOL, 2.0%.
* **proportional** — the venue's bps of notional on each leg. A constant share at every
  size, so it changes nothing about sizing and everything about whether the strategy
  clears its costs at all.
* **impact** — our own order walking the pool. Per *token*, not per chain, and it
  **grows** with size. 0.06 SOL is nothing against a graduated pair and a quarter of a
  thin one.

So total cost as a share of the position is **U-shaped**, and a gate that knows only the
first two terms has a floor and no ceiling. That gate was this module's first version; it
passed 0.06 SOL orders into $25 pools, and the only reason it cost nothing is that the
paper broker refused 137 of 211 of them afterwards on its own slippage cap. A refusal
that arrives after the sizing decision is a bug report, not a brake.

What this module answers
------------------------

Given a chain's measured flat and proportional costs and *this token's* depth:

* ``min_viable`` — below it the flat cost is not amortised;
* ``optimal`` — where total cost per unit of position is lowest;
* ``max_viable`` — above it our own impact costs more than the ceiling;
* ``max_executable`` — above it the order does not fill at all inside the slippage
  tolerance, which is the broker's own criterion, computed here *before* we size.

When those cross — when the smallest size that amortises the flat cost is already too
large for the pool — **there is no size at which the trade is economic**, and that is the
answer, stated in those words rather than resolved by picking the least-bad number.

Where the numbers come from
---------------------------

1. **Flat and proportional are measured** from our own ``trades`` rows per chain
   (:func:`derive_cost_model`), with declared constants only as a fallback for a chain
   that has not traded here.
2. **On an EVM venue they are measured per token, off the chain, in one batch**
   (:func:`read_venue`). A per-chain number is the right shape on Solana, where pump.fun
   charges every token the same 125 bps, and the wrong shape on an EVM launchpad, where
   the cost that decides the trade is written into the token's own contract: three live
   Pons curves read 0, 150 and 180 bps of ``creatorTaxBps()`` on 2026-09-21 and their own
   event logs charged exactly that. Gas — the term an EVM cost model is expected to be
   about — is $0.03 of a $50 round trip there, two orders of magnitude below the tax.
3. **Impact is not modelled here.** ``kaiba.execution.curve_price`` prices a bonding
   curve exactly and is already the broker's pricing path; ``PaperBroker.impact_bps`` is
   the pool approximation the broker will actually charge; ``jupiter.round_trip`` is two
   executable quotes. This module calls those. There is no second impact implementation
   in the tree, and in particular nothing here reads Jupiter's ``priceImpactPct``, which
   returns exact 0 on a split route. The one arithmetic that is here rather than
   delegated is :class:`PonsCurveDepth`, because no other module in the tree prices that
   curve; it was verified against the curve's own fills to one atom. bsc's Flap curve is
   the case that settles the other way and shows the rule is a rule: ``evm_price`` already
   decodes and cross-checks it, so :class:`FlapCurveDepth` is an adapter over that and not
   a second opinion about the same venue.
4. **A size we cannot price refuses.** Unknown depth is not free depth, for the same
   reason unknown flat cost is not free: it is exactly the hole the first version had.
   An unreadable tax is the same hole again — and the provider that would fill it reports
   ``buy_tax_bps=0`` for 836 of 836 robinhood tokens, including ones the chain says charge
   150 and 180 bps, so it is not filled from there.

**Impact is charged once, not twice** (:data:`IMPACT_CHARGED_LEGS`). Buying and selling
back into an *unchanged* constant-product pool returns the input exactly, net of fees —
the displacement our own order causes is recovered on the way out. Into a pool that has
moved, or one whose liquidity has been pulled, none of it is recovered; eleven of our
twenty-five ``migration-fade`` exits were the rug monitor firing. One leg of two is the
midpoint of those bounds, it is declared rather than fitted, and :func:`sizing_band`
reports the band under all three conventions so the choice is visible.

Exits are never gated. Nothing here is reachable from ``RiskGate.check_exit``: a cost
gate on a sell strands the position permanently, which is worse than any fee.

Money is integers: base units ``int``, ratios ``Decimal``. No float touches a cost.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from kaiba.core.config import DEFAULT_RISK_PATH, RiskConfig, get_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jload
from kaiba.core.schemas import (
    EVM_CHAINS,
    EVM_ZERO,
    NATIVE_DECIMALS,
    NATIVE_SYMBOL,
    Chain,
    EvidenceBasis,
    looks_evm,
    now_ms,
)
from kaiba.execution.curve_price import (
    CURVE_TOTAL_FEE_BPS,
    CurveState,
    curve_leg_for_budget,
    has_graduated,
    snapshot_resolver,
    sol_usd_from_native_price,
)

if TYPE_CHECKING:  # the reader is imported where it is called, like every other one here
    from kaiba.execution.evm_price import FlapCurve

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Provenance:
    """Where a constant came from. Every module-level constant here has one.

    ``tests/test_viability.py::test_every_constant_has_provenance`` parses this file and
    fails if a constant is added without a row, because a silently-added knob is how an
    unmeasured number becomes a load-bearing one.
    """

    value: Any
    unit: str
    source: str
    note: str


# ---------------------------------------------------------------------------- constants

#: Operator's ceiling, as a percentage of the position consumed by one round trip: flat +
#: proportional + the charged share of impact. Read from ``config/risk.yaml`` under
#: ``bounds``; this is only the value used when the key is absent. 7.0% is what
#: ``docs/TRADING-METHOD.md`` §5a's "below roughly 0.05 SOL a round trip should not be
#: attempted" works out to on the fee terms alone. With the impact term it is also what
#: separates a tradable pair from a $25 pool.
DEFAULT_MAX_ROUND_TRIP_COST_PCT = Decimal("7.0")

#: The key the operator sets under ``bounds`` in ``config/risk.yaml``.
CEILING_KEY = "max_round_trip_cost_pct"

#: A round trip is a buy and a sell. Trades with a take-profit ladder pay more legs than
#: this and the derivation sees that in the data; the projection for a *prospective* entry
#: assumes the cheapest honest case, two.
LEGS_PER_ROUND_TRIP = 2

#: How many legs of our own price impact a round trip is charged for.
#:
#: 0 is what a static constant-product pool implies: our buy walks the price up, our sell
#: walks it back down the same curve, and the displacement is returned. 2 is what a pool
#: that has moved against us implies, and what a rug implies, where none of it comes back.
#: Our own record is 11 rug exits in 25, so neither bound describes it. 1 is the midpoint,
#: it is a declared convention rather than a fitted number, and :func:`sizing_band`
#: reports the band at 0, 1 and 2 so the sensitivity is never hidden.
IMPACT_CHARGED_LEGS = 1

#: Flat cost per leg, base units, used only when a chain has no usable trade history.
#:
#: Solana: §5a inspected three real mainnet buys — two paid a 0.001 SOL bot-router fee and
#: all three paid ~0.0015 SOL of account rent. Rent is charged once per round trip and the
#: tip on every leg, so ``(2 * 0.001 + 0.0015) / 2 = 0.00175 SOL`` per leg. That is
#: deliberately worse than the 0.00099 SOL/leg our paper record implies, because the paper
#: broker models the tip and not the rent, and a fallback should err toward refusing.
#:
#: No EVM entry, on purpose. An EVM flat cost is gas: it moves by an order of magnitude
#: with the gas price, so one declared number would be wrong most of the time and wrong
#: invisibly. EVM chains stay ``UNAVAILABLE`` until they have their own closed trades.
DECLARED_FLAT_PER_LEG: dict[Chain, int] = {Chain.SOL: 1_750_000}

#: Proportional cost per leg in bps, used with :data:`DECLARED_FLAT_PER_LEG`. pump.fun's
#: bonding curve is a flat 125 bps (95 protocol + 30 creator), read from the on-chain fee
#: config; PumpSwap's first post-graduation tier is 120 bps. Imported from
#: ``curve_price.CURVE_TOTAL_FEE_BPS`` rather than retyped.
DECLARED_PROPORTIONAL_BPS_PER_LEG: dict[Chain, int] = {Chain.SOL: CURVE_TOTAL_FEE_BPS}

#: Closed trades needed before the fit is preferred over the declared constants. Eight is
#: enough for a median-of-pairwise-slopes estimator to have a majority of honest pairs and
#: small enough that a chain becomes self-measuring within a day of trading.
MIN_SAMPLE_TRADES = 8

#: Most recent trades the fit reads. Theil–Sen is O(n²) in pairs; 200 trades is 19,900
#: pairs and ~10 ms, which is affordable on every entry check and keeps the estimate on
#: the current fee regime rather than averaging over a venue's rule change.
MAX_SAMPLE_TRADES = 200

#: The fit needs notional spread to separate flat from proportional at all: if every trade
#: is the same size, any (flat, rate) pair on one line through the point fits equally well.
#: Largest notional must be at least this multiple of the smallest.
MIN_NOTIONAL_SPREAD = Decimal("1.2")

#: A fitted proportional rate above this is not a fee schedule, it is a broken fit (a
#: venue charging 10% a leg would be a headline). Reject the fit and fall back rather than
#: gate on it.
MAX_PLAUSIBLE_PROPORTIONAL_BPS = Decimal("1000")

#: What to do when neither a fit nor a declared constant exists for a chain, or when the
#: token's depth cannot be priced. True refuses the entry. Stated as a constant because it
#: is the most consequential choice in this module and it should be visible in a diff.
UNKNOWN_COST_FAILS_CLOSED = True

#: How old a curve snapshot may be before it is not this token's depth any more. The
#: curves in our own ``curve_snapshots`` moved by whole SOL inside a few minutes;
#: ``curve_price.DEFAULT_SNAPSHOT_MAX_AGE_S`` is 60 for the same reason and this matches
#: it rather than inventing a second freshness rule.
DEPTH_MAX_AGE_S = 60

#: Ratio between grid points in the coarse bracket for the cost-minimising size. The
#: grid only has to find the right valley — the bisection after it lands on the exact
#: base unit — so 25% steps keep a whole entry check inside a few hundred evaluations.
SEARCH_STEP = Decimal("1.25")

#: Paced retries when the limiter refuses the pair probe. One, because the refusal we
#: actually see is the provider's 1.1 s minimum interval ("retry in 0.2s") rather than a
#: 429, and losing a live entry to that would be the same silent-no-op failure
#: ``docs/CONTRACT.md`` warns about for ``wait_for_slot_s``. A second retry would mean a
#: provider in real trouble, and then refusing is the right answer.
PAIR_PROBE_RETRIES = 1

#: Smallest size the search will consider, base units. Below this the flat term dominates
#: so completely that no depth matters, and it keeps the geometric grid finite.
SEARCH_MIN_BASE_UNITS = 1_000

#: Gas one router swap leg burns on an EVM chain. Multiplied by the chain's *live* gas
#: price to get the flat term — the gas price itself is never declared, for the reason
#: :data:`DECLARED_FLAT_PER_LEG` gives.
#:
#: Measured 2026-09-21 by reading ``gasUsed`` off the receipts of 20 consecutive Pons
#: fills on Robinhood Chain: buys 100,683 / 203,992 / 5,635,183 (min / median / max, the
#: max a multicall rather than a plain swap) and sells 78,631 / 138,646 / 424,608. 250,000
#: is above both medians deliberately, the same direction ``DECLARED_FLAT_PER_LEG`` errs
#: in: a flat cost guessed low lets through a position too small to pay for itself.
#:
#: It barely matters on this chain and that is worth knowing: at the 0.0484 gwei read
#: alongside those receipts the whole flat term is 1.21e13 wei, about $0.03 — 0.13% of a
#: 0.0185 ETH round trip. The gas price still moved 3.6x against the 0.174 gwei measured
#: here a day earlier, which is why it is read and not written down.
EVM_SWAP_GAS_UNITS = 250_000

#: Commission the router we submit through takes per leg, in bps of notional.
#:
#: Every live order in this tree goes out as a GMGN swap body (``policy.check_gmgn_swap_body``),
#: and 1%/leg on every chain is what the 2026-09-21 execution review reports. **UNVERIFIED
#: here**: no field of ``gmgn-cli order quote``'s documented response states a commission,
#: so unlike ``feeBps()`` this module cannot read it back off the venue. It is charged
#: anyway — leaving a real 200 bps out of a round trip authorises trades that lose money —
#: and if the figure is wrong it is wrong in the direction of refusing. Measured effect on
#: Pons: it moves the creator tax at which a token stops clearing a 7% ceiling from about
#: 250 bps/leg down to about 150 bps/leg.
ROUTER_BPS_PER_LEG = 100

#: Upper bound on the POOL's own swap fee per leg, by chain, in basis points.
#:
#: This is not the router commission (:data:`ROUTER_BPS_PER_LEG`) and not the token's tax
#: (read per token). It is what the AMM keeps, and on these chains we cannot read the
#: pool's tier: DexScreener returned zero pairs for the live bsc and robinhood tokens
#: probed on 2026-09-22, so there is nothing to read a ``dexId`` or a fee tier off.
#:
#: So it is an UPPER BOUND, not an estimate: the highest published tier of the dominant
#: venue on each chain (PancakeSwap and Uniswap both top out at 1.00%). A bound is honest
#: where a point estimate would not be, and it fails in the direction this module already
#: chose for :data:`ROUTER_BPS_PER_LEG` -- "if the figure is wrong it is wrong in the
#: direction of refusing". A token that clears a 7% ceiling while being charged the worst
#: tier clears it on any real pool.
#:
#: CITED from the venues' published fee schedules, not measured by us. What would settle
#: it: reading the pair's actual fee tier, which needs a pair route that returns something
#: on these chains.
DEX_FEE_BPS_UPPER: dict[Chain, int] = {
    Chain.BSC: 100,
    Chain.ROBINHOOD: 100,
}

#: Pool crossings a native-denominated order must make, per leg, to reach a curve that is
#: quoted in something other than the chain's native asset. Priced at that chain's
#: :data:`DEX_FEE_BPS_UPPER`, which is the same physical quantity — what an AMM keeps.
#:
#: Why there is a crossing at all: every order this tree sends is denominated in the
#: chain's native asset (``engine`` sets ``input_token`` to ``EVM_ZERO`` on every EVM
#: chain), and a Pons curve quoted in GME cannot take ETH. So the router has to buy the
#: quote asset on the way in and sell it on the way out. That is not an assumption about
#: how GMGN routes; it is what the curve's own interface requires.
#:
#: **1 is the MINIMUM, not a bound, and this is the one number here that could be too
#: small.** UNMEASURED: the deepest GME pair DexScreener reported on 2026-09-22 was
#: GME/USDG, not GME/WETH, so the real route may be ETH -> USDG -> GME and cross two. It
#: is charged at one rather than two because one is what is established. What would settle
#: it: ``gmgn-cli order quote`` with ``--input-token`` the zero address and
#: ``--output-token`` a live non-native-quoted token, and reading the route back.
#:
#: Charged for the same reason :data:`ROUTER_BPS_PER_LEG` is charged unverified: leaving a
#: real cost out of a round trip authorises trades that lose money. It is named in the
#: cost model's own source string, so an operator who learns the hop is free can see
#: exactly which term to remove.
ROUTING_HOPS_NON_NATIVE_QUOTE = 1

#: Notes from a curve read that mean "this token has permanently left the curve", as
#: opposed to "the curve read failed this time". Only the former may fall back to the DEX
#: reader: a transient RPC failure must keep refusing, or a flaky node silently downgrades
#: every entry to a looser cost model.
_GRADUATED_NOTES: tuple[str, ...] = ("graduated", "factory_has_no_curve", "no_curve")

#: What a curve read says when OUR OWN rate limiter declined to spend a credit, rather
#: than when the venue failed. `kaiba.core.limiter` emits this exact phrase on a refusal.
#:
#: The distinction is load-bearing. `_GRADUATED_NOTES` above keeps a TRANSIENT curve
#: failure refusing, and that reasoning is right: a node that is timing out is a chain
#: that is misbehaving, which is precisely when a looser cost model is most dangerous.
#: A limiter refusal is not that. The venue was never asked, nothing about the chain is
#: unhealthy, and every input the DEX reader needs -- the token's tax from its dossier,
#: the chain's gas from the CLI, a published fee bound -- is still there and still fresh.
#:
#: MEASURED 2026-09-22: `provider_budgets.robinhood-rpc` is 1500 ms / capacity 3 /
#: refill 0.6, so the bucket sat permanently overdrawn (-15,913 on 25,001 spent) and the
#: Pons factory read returned `pons:factory_unreadable:no attempt made` on every token.
#: `read_venue` was therefore unpriced, `evm_cost_model` had no venue fee and no token
#: tax, `sizing_band` returned `cost_model_unavailable`, and the risk gate refused with
#: `no_viable_band` -- 17 of 28 robinhood skips in three hours, on the only chain that was
#: in profit that day.
#:
#: The throttle is NOT the thing to loosen: the endpoint is the public
#: `rpc.mainnet.chain.robinhood.com` and it produced 9 rate-limit events in 24 hours even
#: at that budget. The limiter is doing its job; what was wrong was reading its refusal as
#: a sick venue.
_LIMITER_REFUSED_NOTES: tuple[str, ...] = (
    # `kaiba.core.limiter` declining to spend a credit.
    "no attempt made",
    # Our own SQLite under write contention. A curve READ takes a WRITE to reserve the
    # limiter credit first, so eleven writers on one database surface here as a venue
    # failure. MEASURED 2026-09-23: this was the dominant robinhood refusal --
    # `pons:factory_unreadable:OperationalError: database is locked` on 4 of the 5 most
    # recent dossiers, 9 of 11 decisions in forty minutes. The database is healthy
    # (a passive checkpoint ran in 11ms, 871 of 872 pages) and so is the chain; we simply
    # could not get the lock inside the 5s busy timeout.
    "database is locked",
)


def _limiter_refused(note: str | None) -> bool:
    """True when the curve read failed because WE could not ASK, not because it broke.

    The distinction this draws is between "the venue answered badly" and "we never got as
    far as asking". Only the first says anything about the chain, and only the first should
    keep an entry refused: a looser cost model is dangerous precisely when the venue is
    misbehaving, and means nothing when the venue was never contacted.
    """
    low = (note or "").lower()
    if _left_the_curve(low):
        return False
    return any(marker in low for marker in _LIMITER_REFUSED_NOTES)

#: Chains whose pre-graduation venue this module probes for exact depth. Flap is the bsc
#: launchpad ``evm_price.read_flap`` decodes, and it is the only non-Pons EVM curve
#: anything in this tree can price.
FLAP_CHAINS: frozenset[Chain] = frozenset({Chain.BSC})

#: Refusals from ``evm_price.read_flap`` that mean "this token is not on a Flap curve",
#: as opposed to "the Flap read failed". The distinction is the same one
#: :data:`_GRADUATED_NOTES` draws and it is drawn for the same reason, one step earlier:
#: only these two may fall through to the DEX rates reader and the pool depth behind it.
#:
#: ``flap_no_portal_record`` is the portal answering with no launch record — the token was
#: never launched there. ``flap_not_on_curve`` is a record whose ``status`` is not 1, which
#: is what a graduated Flap token reads (4 on both graduated samples); it trades on a
#: PancakeSwap pair after that, so a pool really is the right place to price it.
_FLAP_NOT_ON_A_CURVE: tuple[str, ...] = ("flap_no_portal_record", "flap_not_on_curve")

#: Prefix on every refusal from a Flap probe that did not answer, or that answered
#: something failing its own cross-checks. :func:`resolve_depth` stops on it instead of
#: walking on to the dossier and the pair.
#:
#: This is the one place a depth ladder must not be a ladder. A Flap token's fills happen
#: on its curve; the pools a provider reports for it are not the venue we would trade
#: against, and a *failed probe* is exactly the state in which we cannot tell whether
#: there is a curve underneath. Falling through would price a curve token off an unrelated
#: pool — the $25-pool mistake this module exists to have stopped making.
FLAP_PROBE_UNAVAILABLE = "flap_probe_unavailable:"

#: ``creatorTaxBps()`` on a Pons curve. Verified 2026-09-21 against the curves' own logs:
#: the view answers 150 on ``0x3eec6132…`` and 180 on ``0xc719473c…``, and their
#: ``CurveBuy``/``CurveSell`` events charged exactly 150 and 180 bps of the quote leg;
#: ``0xb4dc1a93…`` answers 0 and charged 0. GMGN's own security payload reports
#: ``buy_tax_bps=0`` and ``sell_tax_bps=0`` for **836 of 836** robinhood tokens that carry
#: a value, so the provider number is the field being absent, not the tax being zero —
#: this read is the only one that has ever disagreed with a token, and it is the one the
#: chain agrees with.
PONS_CREATOR_TAX_SELECTOR = "0xc1bb8901"

#: How far the chain head's ``timestamp`` trails this process's wall clock, seconds, on
#: the Pons anti-sniper clock. Only used when the head timestamp itself is unavailable.
#:
#: The curve charges its toll off ``block.timestamp - launchedAt`` in whole seconds
#: (9900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+ s, MEASURED, ``robinhood.SNIPE_TAX_RUNGS_BPS``)
#: and ``launchedAt`` is chain time. ``now_ms() - launchedAt`` is not that number: the
#: head's timestamp is an integer second stamped by the sequencer, so it sits behind a
#: wall clock by the sequencer's own lag plus up to a second of truncation. At wall-clock
#: t=2 s the chain is at rung 0-1, and a read that trusted the wall clock approved a size
#: at 5.13% that the fill would have paid 618 bps a leg on.
#:
#: MEASURED, ``wall_clock - latest.timestamp`` on ``eth_getBlockByNumber('latest')``,
#: four runs: 1.3-2.0 s from this workstation and 0.87-1.55 s from the live box (round-1
#: verifiers, 2026-09-21); re-sampled 2026-09-22, five samples each, stamped at send time
#: the way this reader stamps ``when``: 0.85-1.78 s on the box, 0.90-1.58 s here. At
#: receipt, one 265-373 ms round trip later, the same samples read 1.18-2.05 s and
#: 1.22-1.93 s — the receipt figure brushed the margin once, and it is the send figure
#: the margin has to cover because ``when`` is taken before the batch leaves.
#: ``tests/test_viability_evm.py::LAG_SAMPLES`` carries every run. 2.0 is the
#: **largest lag observed**, not a typical one, because
#: a margin that is too small charges rung 2 (19 bps) for a fill that pays rung 1 (618),
#: and one that is too large only delays an entry that ``lanes`` holds for 10 s past the
#: window anyway. Preferred over it, whenever the batch carries it, is the head's own
#: timestamp, which needs no margin at all.
PONS_CHAIN_LAG_MARGIN_S = Decimal("2.0")

#: How long one venue read is reused inside this process, seconds.
#:
#: A single decision reads the venue twice — ``RiskGate._clamp`` sizes against
#: ``sizing_band`` and then ``check_entry`` gates with ``check_size`` — and
#: ``robinhood.rpc_batch`` caches nothing (``ttl_s=0``). ``kaiba.core.limiter`` has no
#: ``robinhood-rpc`` entry, so that bucket is one call per second and the Pons listener is
#: already spending it every 3 s. 5 s makes a decision cost one round trip instead of two
#: and is still 12x tighter than :data:`DEPTH_MAX_AGE_S`, the freshness rule this module
#: already applies to a Solana curve.
VENUE_READ_TTL_S = 5

#: How long a venue read waits for limiter capacity before refusing, seconds.
#:
#: Measured 2026-09-21: running 30 entry checks back to back against the shared
#: ``robinhood-rpc`` bucket lost 20 of them, three of those to a 60 s family cooldown
#: after the endpoint answered 429 and the rest to "no attempt made" — the limiter
#: refusing outright. ``rpc_batch``'s own default is 5 s, sized for a listener that polls
#: every 3 s and can skip a tick; an entry cannot. 10 s is what
#: ``native_price.DEFAULT_WAIT_FOR_SLOT_S`` gives the same class of caller, "about to
#: submit a live order", and ``docs/CONTRACT.md`` requires the wait to be explicit rather
#: than a silent no-op.
VENUE_PROBE_WAIT_S = 10.0

#: How long one quote asset's native-unit rate is reused inside this process, seconds.
#:
#: The rate is cached per **quote asset**, never per token, and the ratio is why. MEASURED
#: 2026-09-22 over every robinhood token we hold: **59 distinct non-native quote assets
#: cover 2,538 tokens**, and the most-used one is the quote leg of 708 of them. A per-token
#: lookup would therefore spend 2,538 provider calls to learn 59 answers, against a gmgn
#: bucket the scanner is already using.
#:
#: 60 s is not a new judgement: ``evm_price.QUOTE_USD_TTL_S`` is the same quantity (how
#: long a quote token's USD price may be reused without re-asking) and is already 60 s in
#: this tree. Declared here rather than imported because this module must not take a
#: module-level dependency on ``evm_price``, which imports lazily in both directions.
QUOTE_ASSET_RATE_TTL_S = 60.0

#: How far either leg of a quote-asset rate may sit from the instant being priced,
#: seconds. Past it the rate refuses; it is never served anyway.
#:
#: 300 s, and taken rather than invented: ``native_price.DEFAULT_TOLERANCE_MS`` is 300,000
#: and ``evm_price.QUOTE_USD_MAX_AGE_S`` is 300.0, both for the same reason — the stop this
#: eventually feeds sits 30% below entry, so a five-minute move in either leg is noise
#: against it. The native leg already enforces this budget inside ``native_price.at``; this
#: constant applies the same one to the quote leg, so the two halves of a ratio are held to
#: one rule instead of two.
QUOTE_ASSET_PRICE_MAX_AGE_S = 300.0

#: Largest ``decimals`` a quote asset may report and still be believed.
#:
#: Same bound, for the same reason, as ``evm_price._quote_decimals``: the decimals scale
#: the curve's whole quote leg, so a wrong one is a depth reading out by orders of
#: magnitude rather than a rounding error. 36 is past every ERC-20 in the wild and still
#: rejects a field that arrived as text, a float or a sentinel.
QUOTE_ASSET_MAX_DECIMALS = 36

#: How long a quote asset's PROTECTABILITY verdict is trusted, in seconds.
#:
#: Unlike the rate itself this caches BOTH outcomes, because the property is structural
#: rather than momentary: DexScreener cannot price USDG at all -- it is the quote side of
#: every pair it appears in, so ``prices.pick_pair`` correctly discards all 30 of them --
#: and that will be just as true in a minute. MEASURED 2026-09-22: three consecutive
#: probes of USDG through the protection path returned "no pair prices this token".
#:
#: INVENTED as a duration. 15 minutes is long enough that the probe is not on the hot
#: path and short enough that a newly-listed pair is picked up within one trading session.
PROTECTABLE_TTL_S = 900

#: How long a NEGATIVE verdict is trusted, in seconds. Much shorter than the positive one.
#:
#: The two are not symmetric. "The watchdog can see it" stays true; "the watchdog could
#: not see it" may only mean the provider was rate limited for a moment. MEASURED
#: 2026-09-22: GME priced through the protection path on one probe and failed on another
#: taken during a 59-asset sweep that tripped the robinhood-rpc cooldown. Caching that
#: second answer for 15 minutes would lock a perfectly protectable asset out of trading
#: over a transient 429.
#:
#: So a refusal is re-checked within a couple of minutes, and until it is re-checked the
#: token simply does not trade -- which is the safe direction, and the reason this is a
#: shorter TTL rather than no caching at all.
#:
#: INVENTED as a duration; it is roughly the limiter's own family-cooldown scale (60 s at
#: `ban_floor_s`), so a cooled-down provider has recovered before the next probe.
PROTECTABLE_RETRY_S = 120


PROVENANCE: dict[str, Provenance] = {
    "DEFAULT_MAX_ROUND_TRIP_COST_PCT": Provenance(
        DEFAULT_MAX_ROUND_TRIP_COST_PCT, "percent of position",
        "docs/TRADING-METHOD.md §5a, proposed default for config/risk.yaml bounds",
        "Operator-owned. Only used when bounds.max_round_trip_cost_pct is absent.",
    ),
    "CEILING_KEY": Provenance(
        CEILING_KEY, "yaml key", "this module's proposal to the operator",
        "Lives under `bounds` because a cost ceiling is a risk-envelope decision.",
    ),
    "LEGS_PER_ROUND_TRIP": Provenance(
        LEGS_PER_ROUND_TRIP, "legs", "definition of a round trip",
        "Buy plus sell. A TP ladder pays more; the derivation measures that, the "
        "projection does not assume it.",
    ),
    "IMPACT_CHARGED_LEGS": Provenance(
        IMPACT_CHARGED_LEGS, "legs", "declared convention, bracketed by 0 and 2",
        "A static pool returns the displacement (0); a moved or rugged pool returns none "
        "(2). 11 of 25 migration-fade exits were rugs, so the midpoint is charged and "
        "sizing_band() reports all three.",
    ),
    "DECLARED_FLAT_PER_LEG": Provenance(
        DECLARED_FLAT_PER_LEG, "base units per leg",
        "docs/TRADING-METHOD.md §5a: three mainnet buys inspected 2026-09-20",
        "(2 x 0.001 SOL router fee + 0.0015 SOL rent) / 2 legs. Solana only: an EVM flat "
        "cost is gas and does not hold still.",
    ),
    "DECLARED_PROPORTIONAL_BPS_PER_LEG": Provenance(
        DECLARED_PROPORTIONAL_BPS_PER_LEG, "bps per leg",
        "curve_price.CURVE_TOTAL_FEE_BPS: 95 bps protocol + 30 bps creator, on-chain",
        "Imported, not retyped. PumpSwap tier 1 is 120 bps, so this is the worse of the "
        "two Solana venues.",
    ),
    "MIN_SAMPLE_TRADES": Provenance(
        MIN_SAMPLE_TRADES, "closed trades", "chosen for this module",
        "Below this the fit is noise and the declared constants win.",
    ),
    "MAX_SAMPLE_TRADES": Provenance(
        MAX_SAMPLE_TRADES, "closed trades", "chosen for this module",
        "Bounds an O(n^2) estimator and keeps the estimate on the current fee regime.",
    ),
    "MIN_NOTIONAL_SPREAD": Provenance(
        MIN_NOTIONAL_SPREAD, "ratio max/min notional", "identifiability of the fit",
        "Without spread, flat and proportional are not separable.",
    ),
    "MAX_PLAUSIBLE_PROPORTIONAL_BPS": Provenance(
        MAX_PLAUSIBLE_PROPORTIONAL_BPS, "bps per leg", "sanity bound on the fit",
        "A rate above this means the data is not a fee schedule.",
    ),
    "UNKNOWN_COST_FAILS_CLOSED": Provenance(
        UNKNOWN_COST_FAILS_CLOSED, "bool", "docs/CONTRACT.md: missing data is never 0",
        "An unmeasured flat cost is what cost us 15.6% of cost on migration-fade, and an "
        "unmeasured depth is what passed 0.06 SOL into a $25 pool.",
    ),
    "DEPTH_MAX_AGE_S": Provenance(
        DEPTH_MAX_AGE_S, "seconds", "curve_price.DEFAULT_SNAPSHOT_MAX_AGE_S",
        "Matches the resolver's own freshness rule rather than inventing a second one.",
    ),
    "SEARCH_STEP": Provenance(
        SEARCH_STEP, "ratio between grid points", "chosen for this module",
        "Coarse bracket for a unimodal search; bisection refines it to the base unit.",
    ),
    "PAIR_PROBE_RETRIES": Provenance(
        PAIR_PROBE_RETRIES, "retries", "measured: back-to-back probes return "
        "'rate limited: dexscreener: minimum interval (retry in 0.2s)'",
        "_http never blocks for capacity, so without this a contended limiter refuses a "
        "live entry over 200 ms of spacing.",
    ),
    "SEARCH_MIN_BASE_UNITS": Provenance(
        SEARCH_MIN_BASE_UNITS, "base units", "chosen for this module",
        "Lower end of the search grid; no venue fills below it anyway.",
    ),
    "EVM_SWAP_GAS_UNITS": Provenance(
        EVM_SWAP_GAS_UNITS, "gas per leg",
        "measured 2026-09-21: gasUsed on 20 consecutive live Pons fills",
        "Buy median 203,992 / sell median 138,646; 250,000 errs dear, like the Solana "
        "declared flat. The gas PRICE is read live, never declared.",
    ),
    "PROTECTABLE_TTL_S": Provenance(
        PROTECTABLE_TTL_S, "seconds a POSITIVE protectability verdict is trusted",
        "INVENTED as a duration; the need is MEASURED",
        "MEASURED 2026-09-22 on the live box: of 59 non-native quote assets on robinhood "
        "(2,577 tokens), the watchdog's configured source prices 39 (1,662 tokens, 64.5%) "
        "and cannot price 20 (915 tokens, 35.5%) -- USDG alone is 719 of those, and it is "
        "structurally unpriceable there because it is the quote side of all 30 of its own "
        "pairs. That is a property of the pair set, not of the minute, so it is cached. "
        "15 minutes keeps the probe off the hot path while picking up a newly listed pair "
        "within one trading session. Settled by a source that can price a token from the "
        "quote side.",
    ),
    "PROTECTABLE_RETRY_S": Provenance(
        PROTECTABLE_RETRY_S, "seconds a NEGATIVE protectability verdict is trusted",
        "INVENTED as a duration; the asymmetry is MEASURED",
        "Shorter than PROTECTABLE_TTL_S on purpose: 'can see it' stays true, 'could not "
        "see it' may be a 429. MEASURED 2026-09-22: GME priced through the protection "
        "path on one probe and failed on another taken while the robinhood-rpc family was "
        "in cooldown, so caching that answer for the full TTL would lock a protectable "
        "asset out of trading over a blip. 120 s is about the limiter's own family "
        "cooldown floor (60 s), so a cooled-down provider has recovered by the retry. "
        "Settled by measuring how often a negative verdict flips on re-probe.",
    ),
    "DEX_FEE_BPS_UPPER": Provenance(
        DEX_FEE_BPS_UPPER, "bps per leg (upper bound), by chain",
        "CITED from PancakeSwap's and Uniswap's published fee schedules, taken at their "
        "highest tier (1.00%)",
        "An UPPER BOUND, not an estimate: DexScreener returned zero pairs for the live "
        "bsc and robinhood tokens probed 2026-09-22, so the pool's actual tier cannot be "
        "read. Wrong in the direction of refusing, like ROUTER_BPS_PER_LEG. Settled by a "
        "pair route that returns a dexId and fee tier on these chains.",
    ),
    "_GRADUATED_NOTES": Provenance(
        _GRADUATED_NOTES, "substrings of a curve-read note",
        "MEASURED: the notes read_pons_venue emits when a token has permanently left the "
        "curve, as opposed to when the read merely failed",
        "Matched case-insensitively. Only these permit the fall-back to the DEX rates "
        "reader; a transient RPC note must keep refusing, or a flaky node silently "
        "downgrades every entry to a looser cost model.",
    ),
    "_LIMITER_REFUSED_NOTES": Provenance(
        _LIMITER_REFUSED_NOTES, "substrings of a curve-read note",
        "MEASURED 2026-09-22/23: the two ways this process fails to ASK -- the phrase "
        "kaiba.core.limiter emits when it declines to spend a credit (seen on every "
        "robinhood Pons read while that bucket sat at -15,913 on 25,001 spent), and "
        "SQLite write contention, since reserving a limiter credit is a WRITE and eleven "
        "writers share one database (9 of 11 robinhood decisions in forty minutes)",
        "Matched case-insensitively, and only when the note is NOT already a graduated "
        "one. It permits the same fall-back _GRADUATED_NOTES does, for the opposite "
        "reason: the venue was never asked, so nothing about the chain is unhealthy and "
        "the DEX reader's inputs are all still fresh. A genuine transient failure "
        "(timeout, connection reset) still refuses. What would change it: the limiter "
        "wording, which is why the phrase is a constant here rather than inline.",
    ),
    "FLAP_CHAINS": Provenance(
        FLAP_CHAINS, "chains with a readable non-Pons EVM curve",
        "kaiba/execution/evm_price.py: read_flap is the only such reader in the tree and "
        "its portal (FLAP_PORTAL) is a single bsc contract",
        "Not a judgement about which launchpad matters: it is the set of venues something "
        "here can actually price. A chain added to it without a reader would probe "
        "nothing and refuse everything.",
    ),
    "_FLAP_NOT_ON_A_CURVE": Provenance(
        _FLAP_NOT_ON_A_CURVE, "prefixes of an evm_price.read_flap refusal",
        "the two refusals read_flap emits when the PORTAL ANSWERED and the token is not "
        "on a curve there: no launch record, or a record whose status is not 1",
        "The same distinction _GRADUATED_NOTES draws, one venue earlier. Only these fall "
        "through to the DEX reader; every other refusal means the probe did not answer, "
        "and a token we cannot read off its own curve is not one to price off a pool.",
    ),
    "FLAP_PROBE_UNAVAILABLE": Provenance(
        FLAP_PROBE_UNAVAILABLE, "prefix of a depth source and a venue note",
        "this module: the marker resolve_depth stops the depth ladder on",
        "The safety property the Flap arm exists for. A failed probe leaves us unable to "
        "say whether the fill would happen on a curve or a pool, and pricing the first "
        "off the second is the mistake UNKNOWN_COST_FAILS_CLOSED is about.",
    ),
    "ROUTER_BPS_PER_LEG": Provenance(
        ROUTER_BPS_PER_LEG, "bps per leg",
        "2026-09-21 execution review: GMGN takes 1% per leg on every chain",
        "UNVERIFIED by this module - no quote field states a commission. Charged because "
        "omitting a real 200 bps round trip authorises losing trades.",
    ),
    "PONS_CREATOR_TAX_SELECTOR": Provenance(
        PONS_CREATOR_TAX_SELECTOR, "eth_call selector",
        "keccak('creatorTaxBps()')[:4], verified 2026-09-21 against three live curves",
        "Answers 150/180/0 on curves whose own CurveBuy/CurveSell logs charged exactly "
        "150/180/0 bps. GMGN reports 0 for 836 of 836 robinhood tokens.",
    ),
    "PONS_CHAIN_LAG_MARGIN_S": Provenance(
        PONS_CHAIN_LAG_MARGIN_S, "seconds",
        "MEASURED: wall_clock - eth_getBlockByNumber('latest').timestamp at send time, max "
        "observed over four runs (workstation 1.3-2.0 s and 0.90-1.58 s, live box 0.87-1.55 s "
        "and 0.85-1.78 s; 2026-09-21 and 2026-09-22, five samples per re-run)",
        "The max, not the median: too small charges rung 2 for a fill that pays rung 1. "
        "Fallback only; the head's own timestamp is used whenever the batch carries it.",
    ),
    "VENUE_READ_TTL_S": Provenance(
        VENUE_READ_TTL_S, "seconds", "the robinhood-rpc limiter's one call per second",
        "One decision reads the venue twice (sizer, then gate) and rpc_batch caches "
        "nothing. 12x tighter than DEPTH_MAX_AGE_S.",
    ),
    "VENUE_PROBE_WAIT_S": Provenance(
        VENUE_PROBE_WAIT_S, "seconds",
        "measured 2026-09-21: 30 back-to-back entry checks lost 20 reads to the bucket",
        "Matches native_price.DEFAULT_WAIT_FOR_SLOT_S for the same 'about to trade' "
        "caller. rpc_batch's 5 s default is sized for a listener that can skip a tick.",
    ),
    "QUOTE_ASSET_RATE_TTL_S": Provenance(
        QUOTE_ASSET_RATE_TTL_S, "seconds",
        "evm_price.QUOTE_USD_TTL_S, the same quantity's existing answer in this tree",
        "MEASURED 2026-09-22: 59 non-native quote assets cover 2,538 robinhood tokens "
        "(top one 708), so the memo is per ASSET. Per token it would be 2,538 calls for "
        "59 answers.",
    ),
    "QUOTE_ASSET_PRICE_MAX_AGE_S": Provenance(
        QUOTE_ASSET_PRICE_MAX_AGE_S, "seconds",
        "native_price.DEFAULT_TOLERANCE_MS (300,000) and evm_price.QUOTE_USD_MAX_AGE_S "
        "(300.0), both already 5 minutes for this class of price",
        "Applies the native leg's own freshness rule to the quote leg so one ratio is not "
        "held to two budgets. Past it the rate refuses; it is never served stale.",
    ),
    "ROUTING_HOPS_NON_NATIVE_QUOTE": Provenance(
        ROUTING_HOPS_NON_NATIVE_QUOTE, "pool crossings per leg",
        "structural: every order is denominated in the chain's native asset "
        "(engine.input_token = EVM_ZERO) and a curve quoted in an ERC-20 cannot take it, "
        "so the router must cross at least one pool each way",
        "Priced at that chain's DEX_FEE_BPS_UPPER. A MINIMUM, not a bound — the deepest "
        "GME pair on 2026-09-22 was GME/USDG, so the route may cross two. UNMEASURED; "
        "`gmgn-cli order quote` reading the route back would settle it.",
    ),
    "QUOTE_ASSET_MAX_DECIMALS": Provenance(
        QUOTE_ASSET_MAX_DECIMALS, "decimals",
        "evm_price._quote_decimals, which rejects anything outside 0..36",
        "The decimals scale the whole quote leg, so a wrong one is a depth out by orders "
        "of magnitude. Never defaulted to 18.",
    ),
}


# ------------------------------------------------------------------------------- models


@dataclass(frozen=True)
class CostModel:
    """What a round trip's *fees* cost on one chain, and how much that answer is worth."""

    chain: Chain
    flat_per_leg_base_units: int | None
    proportional_bps_per_leg: Decimal | None
    basis: EvidenceBasis
    source: str
    sample_trades: int = 0
    sample_modes: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.flat_per_leg_base_units is not None and self.proportional_bps_per_leg is not None

    @property
    def from_live_trades(self) -> bool:
        """A fit over paper fills inherits the paper broker's fee model, not the venue's."""
        return bool(self.sample_modes) and set(self.sample_modes) == {"live"}


class Depth(Protocol):
    """This token's pool, as much of it as we can establish.

    One method, because one question matters: what does *our own* order cost against it.
    """

    basis: EvidenceBasis
    source: str
    max_size_base_units: int | None

    def entry_impact(self, size_base_units: int) -> int | None:
        """Base units lost on one leg against the pre-trade marginal price, or ``None``."""


@dataclass(frozen=True)
class NoDepth:
    """We could not price this token. Not zero impact — unknown impact."""

    source: str
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    max_size_base_units: int | None = None

    def entry_impact(self, size_base_units: int) -> int | None:
        return None


@dataclass(frozen=True)
class CurveDepth:
    """Exact, from the bonding curve's own reserves.

    Uses ``CurveState``'s integer arithmetic — the same methods ``curve_price.quote_buy``
    calls — one level below the fee, so the venue fee stays in the proportional term and
    is not counted twice. ``curve_leg_for_budget`` is the module's own exact inverse of
    the fee split, so the leg is what the venue would really put on the curve.
    """

    state: CurveState
    source: str = "curve_snapshot"
    basis: EvidenceBasis = EvidenceBasis.VERIFIED_ONCHAIN

    @property
    def max_size_base_units(self) -> int | None:
        """Cost of buying every token the curve has left. Past it a buy is truncated."""
        remaining = self.state.sol_cost_for_tokens(self.state.real_token)
        return remaining if remaining > 0 else None

    def entry_impact(self, size_base_units: int) -> int | None:
        size = int(size_base_units)
        if size <= 0:
            return None
        leg = curve_leg_for_budget(size)
        if leg <= 0:
            return None
        atoms, _after, capped = self.state.buy_exact_in(leg)
        if atoms <= 0 or capped:
            # A capped buy consumed the rest of the curve: the fill is truncated and the
            # size is not one this venue would give us. Not priceable, so not tradable.
            return None
        at_spot = atoms * self.state.virtual_sol // self.state.virtual_token
        return max(0, leg - at_spot)


@dataclass(frozen=True)
class PoolDepth:
    """Constant-product approximation against a pool's own liquidity.

    Delegates the bps to ``PaperBroker.impact_bps`` so this gate refuses exactly what the
    broker would refuse — the 137 rejected orders came from that function, and a gate
    that disagreed with it would be a second opinion rather than a brake.
    """

    liquidity_usd: Decimal
    native_usd: Decimal
    source: str = "dossier_liquidity"
    basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED
    decimals: int = 9

    @property
    def max_size_base_units(self) -> int | None:
        """The whole pool. Impact is 50% there, so every useful answer is far below it."""
        if self.native_usd <= 0:
            return None
        unit = Decimal(10) ** self.decimals
        return int((self.liquidity_usd / self.native_usd * unit).to_integral_value(rounding=ROUND_FLOOR))

    def entry_impact(self, size_base_units: int) -> int | None:
        size = int(size_base_units)
        if size <= 0 or self.liquidity_usd <= 0 or self.native_usd <= 0:
            return None
        from kaiba.execution.paper import PaperBroker

        unit = Decimal(10) ** self.decimals
        size_usd = Decimal(size) / unit * self.native_usd
        bps = PaperBroker().impact_bps(size_usd, self.liquidity_usd)
        return int((Decimal(size) * Decimal(bps) / 10_000).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class PonsCurveDepth:
    """Exact, from a Pons (Robinhood Chain) bonding curve's own reserves.

    The EVM twin of :class:`CurveDepth`, and exact for the same reason: the curve is a
    constant product and every input is an integer read off it in one ``eth_call`` batch.

    Verified 2026-09-21 by replaying live ``CurveBuy`` events against an ``eth_call`` of
    the reserves at ``block - 1``: over **40 of 40** fills that were alone in their block,
    this arithmetic reproduced the event's own ``tokensOut`` **exactly**. It took two
    goes, and the first is why the test that pins it exists. Writing the output as
    ``token_reserve - k // (quote_reserve + leg)`` — algebraically the same thing — was
    one atom *high* on every single fill, because that spelling rounds the division the
    other way and the contract floors ``token_reserve * leg / (quote_reserve + leg)``.
    One atom does not matter; being unable to say which of two spellings the venue uses
    does, because it is the difference between a model and a guess that agrees.

    The mechanics, from ``ingest/robinhood.curve_from_state``, which measured them:

    * ``quote_reserve * token_reserve`` is the constant, and ``token_reserve`` is the
      curve's **whole** balance (``sellableTokens() + reservedTokens()``);
    * the venue takes ``fee_bps`` and ``creatorTaxBps`` off the top of the quote *before*
      the curve sees it, each floored independently — which is exactly how the event
      amounts reconcile;
    * the curve stops at ``graduationThreshold``, so :attr:`headroom_base_units` is how
      much quote it can still take.

    ``take_bps_per_leg`` lives here so the leg put on the curve is the leg the venue would
    really put on it. It is **not** charged here: the same bps are the proportional term
    of :class:`CostModel`, one level up, exactly as ``CurveDepth`` leaves the pump.fun fee
    to ``DECLARED_PROPORTIONAL_BPS_PER_LEG``.
    """

    quote_reserve: int
    token_reserve: int
    headroom_base_units: int
    take_bps_per_leg: int
    source: str = "pons_curve"
    basis: EvidenceBasis = EvidenceBasis.VERIFIED_ONCHAIN

    def _leg(self, size_base_units: int) -> int | None:
        """Quote that reaches the curve after the venue's cut, the way the contract does it."""
        size = int(size_base_units)
        if size <= 0 or self.take_bps_per_leg < 0 or self.take_bps_per_leg >= 10_000:
            return None
        return size - size * self.take_bps_per_leg // 10_000

    @property
    def max_size_base_units(self) -> int | None:
        """Largest order the curve can still take before it graduates, grossed up by the cut.

        Past it the buy is truncated or graduates the pool mid-fill, which is not a fill
        at the size we asked for — the same reason ``CurveDepth`` refuses a capped buy.
        """
        if self.headroom_base_units <= 0 or self.take_bps_per_leg >= 10_000:
            return None
        gross = self.headroom_base_units * 10_000 // (10_000 - self.take_bps_per_leg)
        return gross if gross > 0 else None

    def tokens_out(self, size_base_units: int) -> int | None:
        """Atoms this order would receive, as the contract itself computes them.

        The spelling matters (see the class docstring): floor of
        ``token_reserve * leg / (quote_reserve + leg)`` is what 40 of 40 live fills did.
        ``None`` when the order cannot be priced at this size at all.
        """
        if self.quote_reserve <= 0 or self.token_reserve <= 0:
            return None
        leg = self._leg(size_base_units)
        if leg is None or leg > self.headroom_base_units:
            return None
        return self.token_reserve * leg // (self.quote_reserve + leg)

    def entry_impact(self, size_base_units: int) -> int | None:
        leg = self._leg(size_base_units)
        atoms = self.tokens_out(size_base_units)
        if leg is None or atoms is None:
            return None
        if leg == 0:
            # The venue's cut ate the whole order, so nothing reaches the pool and the
            # displacement really is zero. This is not "unknown treated as free": the cut
            # is charged in full in the proportional term, so a 9,900 bps snipe window
            # prices the round trip near 100% and the band refuses every size.
            return 0
        if atoms <= 0:
            return None
        at_spot = atoms * self.quote_reserve // self.token_reserve
        return max(0, leg - at_spot)


@dataclass(frozen=True)
class FlapCurveDepth:
    """A Flap (bsc) bonding curve's own state, read on chain and cross-checked.

    Every input is an integer read off the venue in one batch, and each is corroborated
    against the curve's own price and reserve words before it reaches here — which is what
    the ``VERIFIED_ONCHAIN`` basis is claiming. The *fill spelling* is one step short of
    that and ``evm_price.FlapCurve`` says so in its own docstring: unlike the Pons twin it
    has not been replayed against a live buy, so it errs toward the venue by an atom.

    The bsc twin of :class:`PonsCurveDepth`, and deliberately a thinner class than it.
    The curve arithmetic is **not** reimplemented here: ``evm_price.FlapCurve`` owns it,
    because that module decoded the portal record word by word and is where the
    cross-checks live. This is the :class:`Depth` adapter over it. ``PonsCurveDepth``
    applies the same test — "no other module in the tree prices that curve" — and gets
    the opposite answer, because for this venue one does.

    Two differences from the Pons twin, both deliberate:

    * **No cut comes off the size before it reaches the curve.** ``PonsCurveDepth._leg``
      subtracts one because that contract's fee is taken off the top and the event
      amounts proved it. Nothing here has established Flap's split, so the whole size
      goes on the curve: the larger leg, hence the larger impact, which is the direction
      this module errs in everywhere else. The fee and the token's tax are still charged
      in full, one level up, in :class:`CostModel`'s proportional term.
    * **The graduation bound lives in the curve**, not here, because
      ``estimateSupply``'s own arithmetic is what says when a fill would cross it.

    ``rate`` is set when the curve is quoted in something other than BNB, which MEASURED
    2026-09-22 is **12 of the 20 most recent bsc dossiers** — the single largest reason
    this chain sizes nothing. It is the same :class:`QuoteAssetRate` the Pons arm uses;
    what differs is *where* it is applied. Pons' curve arithmetic lives in this module, so
    there the reserves are converted once and the depth is genuinely wei-denominated.
    Flap's arithmetic lives in ``evm_price.FlapCurve`` and this class is only the adapter
    over it, so here the **size** is converted into the curve's own currency on the way in
    and the answer back out — which is the same rule the class docstring already states,
    that there must be one derivation of a venue's curve in the tree and it is not this
    one. Every caller of this class still speaks base units of the chain's native asset.
    """

    curve: FlapCurve
    source: str = "flap_curve"
    basis: EvidenceBasis = EvidenceBasis.VERIFIED_ONCHAIN
    #: ``None`` when the curve is quoted in BNB, which needs no conversion at all.
    rate: QuoteAssetRate | None = None

    def _to_curve(self, size_base_units: int) -> int:
        """A size in the chain's native base units, in the curve's own quote units."""
        size = int(size_base_units)
        if self.rate is None:
            return size
        return self.rate.to_quote_base_units(size)

    def _from_curve(self, amount_quote_base: int) -> int:
        if self.rate is None:
            return int(amount_quote_base)
        return self.rate.to_native_base_units(amount_quote_base)

    @property
    def max_size_base_units(self) -> int | None:
        largest = self.curve.max_size_base_units
        if largest is None or self.rate is None:
            return largest
        converted = self._from_curve(largest)
        # Floored to nothing means the curve's whole remaining capacity is worth less than
        # one wei. That is not "no bound", it is a bound of zero, and `None` here would
        # read as the former.
        return converted if converted > 0 else None

    def tokens_out(self, size_base_units: int) -> int | None:
        """Atoms this order would receive, as the venue's own ``estimateSupply`` computes."""
        return self.curve.tokens_out(self._to_curve(size_base_units))

    def entry_impact(self, size_base_units: int) -> int | None:
        size = self._to_curve(size_base_units)
        atoms = self.curve.tokens_out(size)
        quote = self.curve.virtual_quote_base
        tokens = self.curve.virtual_token_atoms
        if atoms is None or quote is None or tokens <= 0:
            return None
        # What those atoms would have cost at the pre-trade marginal price. Same shape as
        # PonsCurveDepth and CurveDepth: the difference is our own order's displacement.
        at_spot = atoms * quote // tokens
        return self._from_curve(max(0, size - at_spot))


@dataclass(frozen=True)
class VenueRead:
    """One round trip to an EVM chain: this token's depth, its rates, and the gas price.

    They arrive together because they *are* together — twelve ``eth_call``s in one batch
    against a chain whose limiter bucket is one call per second. Splitting them into a
    depth resolver and a cost model that each reach the chain would double the cost of
    every decision and let the two answers come from different blocks.

    Any of the three may be ``None`` and each ``None`` refuses: an EVM round trip cannot
    be priced without the token's own tax, and :data:`UNKNOWN_COST_FAILS_CLOSED` is the
    whole argument.

    ``routing_bps_per_leg`` is the fourth term and the only one that is a *bound* rather
    than a read. It is 0 on a curve quoted in the chain's native asset — every order we
    send is denominated in native (``engine`` sets ``input_token`` to ``EVM_ZERO`` on every
    EVM chain), so nothing has to be crossed to reach that curve. It is not 0 on a curve
    quoted in something else; see :data:`ROUTING_HOP_BPS_PER_LEG`.
    """

    depth: Depth
    fee_bps_per_leg: Decimal | None
    tax_bps_per_leg: Decimal | None
    gas_price_wei: int | None
    note: str
    routing_bps_per_leg: Decimal = Decimal(0)

    @property
    def priced(self) -> bool:
        return (
            self.fee_bps_per_leg is not None
            and self.tax_bps_per_leg is not None
            and self.gas_price_wei is not None
            and self.gas_price_wei > 0
        )


@dataclass(frozen=True)
class CostEstimate:
    """The projected round-trip cost of one prospective position, term by term."""

    chain: Chain
    size_base_units: int
    model: CostModel
    depth: Depth
    flat_base_units: int | None
    proportional_base_units: int | None
    impact_base_units: int | None
    impact_bps_one_leg: int | None
    total_base_units: int | None
    pct: Decimal | None
    ceiling_pct: Decimal | None

    @property
    def known(self) -> bool:
        return self.pct is not None

    def findings(self) -> list[str]:
        """Reported on every decision, passing or not. A gate only visible when it fires
        teaches nobody what the trade was expected to cost."""
        out = [f"round_trip_cost_basis:{self.model.basis.value}:{self.model.source}"]
        if self.model.sample_trades:
            modes = "+".join(self.model.sample_modes) or "unknown"
            out.append(f"round_trip_cost_sample:{self.model.sample_trades}:{modes}")
        out.append(f"depth_basis:{self.depth.basis.value}:{self.depth.source}")
        if self.impact_bps_one_leg is not None:
            out.append(f"entry_impact_bps:{self.impact_bps_one_leg}")
        else:
            out.append("entry_impact_bps:unavailable")
        if self.pct is None:
            out.append("round_trip_cost_pct:unavailable")
            return out
        out.append(f"round_trip_cost_pct:{self.pct:.4f}")
        out.append(f"round_trip_cost_native:{self.total_base_units}")
        out.append(f"round_trip_flat_native:{self.flat_base_units}")
        out.append(f"round_trip_impact_native:{self.impact_base_units}")
        if self.ceiling_pct is not None:
            out.append(f"round_trip_cost_ceiling_pct:{self.ceiling_pct}")
        return out


@dataclass(frozen=True)
class SizingBand:
    """Every size at which this trade is worth doing, or the reason there are none."""

    chain: Chain
    model: CostModel
    depth: Depth
    ceiling_pct: Decimal | None
    slippage_tolerance_bps: int
    min_viable_base_units: int | None = None
    optimal_base_units: int | None = None
    optimal_cost_pct: Decimal | None = None
    max_viable_base_units: int | None = None
    max_executable_base_units: int | None = None
    reason: str = "unevaluated"
    #: optimum under IMPACT_CHARGED_LEGS = 0, 1, 2, so the convention's weight is visible.
    optimal_by_impact_legs: tuple[int | None, int | None, int | None] = (None, None, None)

    @property
    def viable(self) -> bool:
        return self.min_viable_base_units is not None and self.upper is not None and (
            self.min_viable_base_units <= self.upper
        )

    @property
    def upper(self) -> int | None:
        """The binding upper bound: economics or executability, whichever is tighter."""
        bounds = [b for b in (self.max_viable_base_units, self.max_executable_base_units)
                  if b is not None]
        return min(bounds) if bounds else None

    def contains(self, size_base_units: int) -> bool:
        upper = self.upper
        return (
            self.min_viable_base_units is not None
            and upper is not None
            and self.min_viable_base_units <= int(size_base_units) <= upper
        )

    def findings(self) -> list[str]:
        out = [f"sizing_band:{self.reason}"]
        for name, value in (
            ("min_viable_base_units", self.min_viable_base_units),
            ("optimal_base_units", self.optimal_base_units),
            ("max_viable_base_units", self.max_viable_base_units),
            ("max_executable_base_units", self.max_executable_base_units),
        ):
            out.append(f"{name}:{value if value is not None else 'none'}")
        if self.optimal_cost_pct is not None:
            out.append(f"optimal_cost_pct:{self.optimal_cost_pct:.4f}")
        return out


@dataclass(frozen=True)
class ViabilityVerdict:
    """``ok`` is never inferred from an empty reason — same contract as PolicyDecision."""

    ok: bool
    reason: str
    estimate: CostEstimate
    band: SizingBand | None = None
    findings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


# -------------------------------------------------------------------------- the ceiling


def _raw_bounds_value(key: str) -> Any:
    """Read one ``bounds`` key straight from the YAML.

    ``EnvelopeBounds`` is a pydantic model that ignores unknown keys, so a ceiling the
    operator adds to ``config/risk.yaml`` never reaches ``cfg.bounds`` until someone adds
    the field to ``kaiba/core/config.py`` — a file this module does not own. Reading the
    raw file means the operator's edit takes effect the moment they save it, and
    :func:`configured_ceiling_pct` still prefers the typed attribute once it exists.
    """
    path = Path(os.environ.get("KAIBA_RISK_PATH", DEFAULT_RISK_PATH))
    try:
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - a hostile file must not crash the gate
        log.warning("risk.yaml unreadable while reading bounds.%s: %s", key, exc)
        return None
    bounds = raw.get("bounds") if isinstance(raw, dict) else None
    return bounds.get(key) if isinstance(bounds, dict) else None


def configured_ceiling_pct(cfg: RiskConfig | None = None) -> Decimal | None:
    """The operator's ceiling. ``None`` means the configured value is unusable.

    ``None`` is not "no ceiling": :func:`check_size` refuses on it. A ceiling of 0 or a
    string that is not a number is a config error, and the way to surface a config error
    in a risk gate is to stop, loudly, rather than to substitute a default the operator
    never wrote.
    """
    cfg = cfg if cfg is not None else get_risk()
    value = getattr(cfg.bounds, CEILING_KEY, None)
    if value is None:
        value = _raw_bounds_value(CEILING_KEY)
    if value is None:
        return DEFAULT_MAX_ROUND_TRIP_COST_PCT
    if isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        log.warning("bounds.%s is not a number: %r", CEILING_KEY, value)
        return None
    if dec <= 0 or not dec.is_finite():
        log.warning("bounds.%s must be positive, got %s", CEILING_KEY, dec)
        return None
    return dec


def slippage_tolerance_bps(cfg: RiskConfig | None = None) -> int:
    """Impact an order may carry and still fill: the operator's existing envelope bound.

    ``bounds.max_slippage_bps`` already exists and is already owned by the operator, so
    the executability arm needs no new knob. The broker's deterministic slippage floor is
    charged on top of impact before it compares against the order's tolerance, so it is
    subtracted here — this returns the impact budget, not the slippage budget.
    """
    from kaiba.execution.paper import DEFAULT_SLIPPAGE_FLOOR_BPS

    cfg = cfg if cfg is not None else get_risk()
    return max(0, int(cfg.bounds.max_slippage_bps) - int(DEFAULT_SLIPPAGE_FLOOR_BPS))


# ----------------------------------------------------------------------- the derivation


def _as_int(value: Any) -> int | None:
    """Base units arrive from SQLite as TEXT to survive 2^63. Never via float."""
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _sample(chain: Chain, conn: sqlite3.Connection | None, limit: int) -> list[dict[str, Any]]:
    c = conn if conn is not None else get_conn()
    rows = fetch_all(
        c,
        "SELECT cost_native, proceeds_native, fees_native, mode FROM trades "
        "WHERE chain = ? ORDER BY closed_ms DESC LIMIT ?",
        (chain.value, int(limit)),
    )
    live = [r for r in rows if str(r["mode"]) == "live"]
    # A live fill and a paper fill do not share a fee model, so never mix them. Live wins
    # as soon as there is enough of it; until then the paper record is what we have, and
    # the basis says so.
    return live if len(live) >= MIN_SAMPLE_TRADES else rows


def derive_cost_model(
    chain: Chain, conn: sqlite3.Connection | None = None, *, limit: int = MAX_SAMPLE_TRADES
) -> CostModel:
    """Fit ``fees = legs * flat + rate * notional`` over our own closed trades.

    Theil–Sen (median of pairwise slopes, then median residual) rather than least squares.
    Our own record shows why: a trade that exits through a take-profit ladder pays three
    tips instead of two, and those rows sit at the top of the notional range where OLS
    weights them hardest — it reads their extra flat cost as a steeper *proportional* rate
    and reports 167 bps/leg against a known 125. The median ignores them and lands on
    131 bps/leg and 0.000989 SOL/leg against a known 0.001.

    Returns an ``UNAVAILABLE`` model when the sample is too small, too narrow in size to
    separate the two components, or fits something implausible. The caller falls back to
    :func:`declared_cost_model`; it does not treat a failed fit as free.
    """
    rows = _sample(chain, conn, limit)
    points: list[tuple[Decimal, Decimal]] = []
    modes: set[str] = set()
    for row in rows:
        cost = _as_int(row["cost_native"])
        proceeds = _as_int(row["proceeds_native"])
        fees = _as_int(row["fees_native"])
        if cost is None or proceeds is None or fees is None:
            continue
        if cost <= 0 or fees <= 0:
            continue
        points.append((Decimal(cost + proceeds), Decimal(fees)))
        modes.add(str(row["mode"]))

    def unavailable(why: str) -> CostModel:
        return CostModel(
            chain=chain, flat_per_leg_base_units=None, proportional_bps_per_leg=None,
            basis=EvidenceBasis.UNAVAILABLE, source=f"trades:{why}",
            sample_trades=len(points), sample_modes=tuple(sorted(modes)),
        )

    if len(points) < MIN_SAMPLE_TRADES:
        return unavailable(f"only_{len(points)}_of_{MIN_SAMPLE_TRADES}")
    smallest = min(x for x, _ in points)
    largest = max(x for x, _ in points)
    if smallest <= 0 or largest / smallest < MIN_NOTIONAL_SPREAD:
        return unavailable("notional_spread_too_narrow")

    slopes = [
        (yj - yi) / (xj - xi)
        for i, (xi, yi) in enumerate(points)
        for xj, yj in points[i + 1:]
        if xj != xi
    ]
    if not slopes:
        return unavailable("no_distinct_notionals")
    rate = _median(slopes)
    if rate < 0 or rate * 10_000 > MAX_PLAUSIBLE_PROPORTIONAL_BPS:
        return unavailable(f"implausible_rate_{(rate * 10_000):.1f}bps")
    round_trip_flat = _median([y - rate * x for x, y in points])
    if round_trip_flat <= 0:
        # No flat component the data can see. That is a claim about the venue we have no
        # right to make from a fit, so defer to the declared constant instead.
        return unavailable("no_flat_component_in_fit")

    per_leg = int((round_trip_flat / LEGS_PER_ROUND_TRIP).to_integral_value(rounding=ROUND_CEILING))
    return CostModel(
        chain=chain,
        flat_per_leg_base_units=per_leg,
        proportional_bps_per_leg=rate * 10_000,
        basis=EvidenceBasis.DERIVED,
        source=f"trades:theil_sen:{len(points)}",
        sample_trades=len(points),
        sample_modes=tuple(sorted(modes)),
    )


def declared_cost_model(chain: Chain) -> CostModel:
    """The documented constants for a chain that has not traded here yet."""
    flat = DECLARED_FLAT_PER_LEG.get(chain)
    bps = DECLARED_PROPORTIONAL_BPS_PER_LEG.get(chain)
    if flat is None or bps is None:
        return CostModel(
            chain=chain, flat_per_leg_base_units=None, proportional_bps_per_leg=None,
            basis=EvidenceBasis.UNAVAILABLE, source="declared:none_for_chain",
        )
    return CostModel(
        chain=chain, flat_per_leg_base_units=int(flat), proportional_bps_per_leg=Decimal(bps),
        basis=EvidenceBasis.ESTIMATED, source="declared:TRADING-METHOD-5a",
    )


def evm_cost_model(chain: Chain, venue: VenueRead) -> CostModel:
    """What one leg costs on an EVM venue: live gas, plus the venue's and the token's bps.

    ``flat`` is :data:`EVM_SWAP_GAS_UNITS` at the gas price read in the same batch as the
    reserves. ``proportional`` is three measured-or-declared parts, and the order of
    magnitude between them is the point:

    ===============================  ==================================================
    ``feeBps()``                     100 bps on every Pons curve read, on chain
    ``creatorTaxBps()`` + snipe tax  0-500 bps, per token, on chain; 9,900 in the window
    :data:`ROUTER_BPS_PER_LEG`       100 bps, declared, unverified
    ``routing_bps_per_leg``          0 on a native-quoted curve; one pool crossing on a
                                     curve quoted in something else
                                     (:data:`ROUTING_HOPS_NON_NATIVE_QUOTE`)
    ===============================  ==================================================

    Gas is the term a naive EVM cost model would contain and it is the one that does not
    matter: 250,000 gas at 0.0484 gwei is $0.03, while a 300 bps creator tax on a 0.0185
    ETH round trip is $1.50. A model that counted only gas would be wrong by two orders of
    magnitude *and* wrong in the direction that authorises the trade.

    No fit. :func:`derive_cost_model` is deliberately not consulted on an EVM chain: it
    fits one flat and one rate per *chain* over our own closed trades, and the term that
    decides an EVM round trip belongs to the token. A fit over other tokens' fills would
    launder a 0% tax onto a 5% token.
    """
    def unavailable(why: str) -> CostModel:
        return CostModel(
            chain=chain, flat_per_leg_base_units=None, proportional_bps_per_leg=None,
            basis=EvidenceBasis.UNAVAILABLE, source=f"evm:{why}",
        )

    if not venue.priced:
        missing = []
        if venue.gas_price_wei is None or venue.gas_price_wei <= 0:
            missing.append("gas_price")
        if venue.fee_bps_per_leg is None:
            missing.append("venue_fee")
        if venue.tax_bps_per_leg is None:
            missing.append("token_tax")
        return unavailable(f"{'+'.join(missing) or 'unpriced'}:{venue.note}")
    assert venue.gas_price_wei is not None
    assert venue.fee_bps_per_leg is not None and venue.tax_bps_per_leg is not None
    flat = EVM_SWAP_GAS_UNITS * int(venue.gas_price_wei)
    routing = venue.routing_bps_per_leg
    bps = venue.fee_bps_per_leg + venue.tax_bps_per_leg + Decimal(ROUTER_BPS_PER_LEG) + routing
    # Only spelled into the source when it is charged, so the string a native-quoted
    # robinhood decision prints is unchanged and a reader who sees `hop` knows the order
    # has to cross a pool before it reaches the curve.
    hop = f"+hop{routing}bps" if routing else ""
    return CostModel(
        chain=chain,
        flat_per_leg_base_units=flat,
        proportional_bps_per_leg=bps,
        # The rates and the gas price are on-chain reads; the gas *units*, the router
        # commission and the routing hop are declared, so the weakest input decides the
        # basis. The source says which part is which rather than letting one word stand
        # for all of them.
        basis=EvidenceBasis.ESTIMATED,
        source=(
            f"evm:{venue.note}:gas{EVM_SWAP_GAS_UNITS}x{venue.gas_price_wei}wei:"
            f"fee{venue.fee_bps_per_leg}+tax{venue.tax_bps_per_leg}+router{ROUTER_BPS_PER_LEG}bps{hop}"
        ),
    )


def cost_model(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    token: str | None = None,
    venue: VenueRead | None = None,
) -> CostModel:
    """Our own trades first, the declared constants second, ``UNAVAILABLE`` third.

    On an EVM chain none of those three apply and :func:`evm_cost_model` answers instead:
    there is no declared constant (gas does not hold still), a per-chain fit cannot see a
    per-token tax, and the venue read is the only thing that can price either. ``venue``
    is passed in by :func:`check_size` so the rates and the depth come from one batch.
    """
    if chain in EVM_CHAINS:
        return evm_cost_model(chain, venue if venue is not None else read_venue(chain, token, conn))
    derived = derive_cost_model(chain, conn)
    if derived.known:
        return derived
    return declared_cost_model(chain)


# ------------------------------------------------------------------------------- depth


def pool_depth_from_round_trip(
    lamports_in: int,
    cost_bps: int,
    *,
    fee_bps_per_leg: int = CURVE_TOTAL_FEE_BPS,
    native_usd: Decimal | None = None,
    source: str = "jupiter_round_trip",
) -> PoolDepth | NoDepth:
    """Back an implied depth out of an executable two-quote round trip.

    ``jupiter.round_trip`` buys and sells straight back and reports what the pair costs
    at that size — the only depth number in the tree that is executable rather than
    reported. It includes the venue fee on both legs, so the fee is removed here before
    the residual is read as impact; otherwise the fee would be counted twice.

    Never derive this from a single quote's ``priceImpactPct``: it returns exact 0 on a
    split route (``curve_price.PRICE_IMPACT_PCT_WARNING``).
    """
    size = int(lamports_in)
    residual = Decimal(int(cost_bps)) - Decimal(LEGS_PER_ROUND_TRIP * int(fee_bps_per_leg))
    if size <= 0 or residual <= 0:
        return NoDepth(source=f"{source}:no_impact_above_fee")
    # impact_frac = size / (L + size)  ->  L = size * (1 - frac) / frac
    frac = residual / 10_000
    if frac >= 1:
        return NoDepth(source=f"{source}:impact_exceeds_size")
    native = native_usd if native_usd is not None else Decimal(1)
    liquidity_native = Decimal(size) * (1 - frac) / frac
    return PoolDepth(
        liquidity_usd=liquidity_native / (Decimal(10) ** 9) * native,
        native_usd=native,
        source=f"{source}:{cost_bps}bps@{size}",
        basis=EvidenceBasis.VERIFIED_ONCHAIN,
    )


# --------------------------------------------------------------------------- EVM venues
#
# Why a *venue* and not a chain. On Solana the fee is the chain's: pump.fun charges 125
# bps and it charges every token 125 bps, so one number per chain is the whole answer and
# `DECLARED_PROPORTIONAL_BPS_PER_LEG` is a `dict[Chain, int]`. On an EVM launchpad the
# dominant cost lives in the *token's own contract*: three live Pons curves read 0, 150
# and 180 bps of creator tax on 2026-09-21 and their own event logs charged exactly that.
# A per-chain number cannot express it, and the provider that claims to
# (`buy_tax_bps`/`sell_tax_bps` from GMGN: 0 on 836 of 836 robinhood tokens) is reporting
# an absent field as a zero. So the EVM arm reads the venue, per token, or refuses.
#
# One batch, twelve calls: ten for `robinhood.CURVE_READS`, one for `creatorTaxBps()`, one
# for `eth_gasPrice`. The depth and the rates must come from the same block or they are
# describing two different curves, and the chain's limiter bucket is one call a second.


def _hex_word_int(value: Any) -> int | None:
    """One ``eth_call`` result as an int, or ``None``. A failed read is never 0."""
    if not isinstance(value, str) or not value.startswith("0x") or value == "0x":
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _abi_address(result: Any, index: int) -> str | None:
    """Word ``index`` of an ABI return, read as an address. ``None`` if it is not one."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return None
    body = result[2:]
    word = body[index * 64:(index + 1) * 64]
    if len(word) != 64:
        return None
    candidate = ("0x" + word[-40:]).lower()
    return candidate if looks_evm(candidate) else None


# ------------------------------------------------------------- curves quoted in a stock
#
# About a fifth of this venue is not quoted in the chain's native asset. MEASURED
# 2026-09-22 over 60 recent robinhood dossiers: 41 native-quoted, **10 non-native**, 6 with
# no curve, 3 graduated. Over the whole local `tokens` table the same day: **1,890 of
# 8,963** robinhood tokens (21%) are quoted in one of **57 distinct** non-native assets,
# the top one being the quote leg of 566 and the next of 468. The live box read the same
# shape one size larger: 59 assets over 2,538 tokens, top one 708.
#
# Those curves take a tokenised equity -- NVDA, GME, TSLA, GOOGL, SPCX, DJT -- or a
# stablecoin, and the operator has authorised trading them. The refusal they used to hit
# was correct as written: a size in wei and a reserve in some other ERC-20's base units
# are not the same currency, and adding them is a unit error wearing a number's clothes.
# What was missing was the rate, and the rate is two prices we already have a path to:
#
#     native units per whole quote unit = quote_usd / native_usd
#
# * ``quote_usd`` -- ``gmgn-cli token info``'s ``price.price``. MEASURED 2026-09-22, it
#   answered for **3 of 3** quote assets probed: NVDA 226.56871951 (18 dp), GME
#   23.59719528 (18 dp), USDG 1.00064189754 (**6 dp**). The DexScreener stack the watchdog
#   uses agreed on the two equities to within 0.33% (226.65 / 23.52) and could not price
#   the stablecoin at all -- it is the quote side of all 30 of its pairs, and
#   ``prices.pick_pair`` correctly discards a pair that does not price the token asked
#   about. That is why this reader goes to GMGN and not to the DEX stack;
# * ``native_usd`` -- our own ``native_prices`` table via :func:`_native_usd`, which for
#   Robinhood Chain reads ETH's samples because its native asset IS ETH
#   (``native_price.NATIVE_PRICE_ALIAS``). Verified live at $2,741.02 the same day.
#
# **The decimals are not 18.** USDG is 6, and it is the quote leg of 468 of those 1,890
# tokens -- a quarter of them. Reading its reserve at 18 would make every one of those
# curves look 1e12 times thinner and refuse every size, silently.
#
# Either leg missing, stale, zero or negative and the read refuses by name. A missing rate
# must never become 1.0, which would price a $23.60 stock as if it were an ETH and size
# the position 116x too large, and never 0, which would read the curve as bottomless.
#
# **Both EVM venues use this one rate**, and on bsc it matters more than on robinhood.
# MEASURED 2026-09-22 over the 20 most recent bsc dossiers with the Flap depth reader live:
# bsc sized 1 of 20, and 12 of the refusals were `quote_not_native` against 5 portal read
# failures and 3 that fell through to the DEX reader. That is 60% of every bsc candidate
# turned away by this one class of refusal.
#
# What differs between the two venues is only WHERE the rate is applied, and it follows
# the rule this module already keeps about who owns a venue's arithmetic:
#
# * Pons' curve arithmetic is here (:class:`PonsCurveDepth`), so the curve's reserves are
#   converted once and the depth is genuinely wei-denominated;
# * Flap's is in ``evm_price.FlapCurve``, so :class:`FlapCurveDepth` converts the SIZE into
#   the curve's own currency on the way in and the answer back out.
#
# The two directions round opposite ways and both toward refusing: reserves down (a
# shallower curve costs our order more), sizes up (a larger order costs more).


@dataclass(frozen=True)
class QuoteAssetRate:
    """One non-native quote asset, priced in the chain's own native asset.

    One object, both EVM venues. The conversion is **exact integer arithmetic**:
    ``quote_usd`` and ``native_usd`` are ``Decimal`` — they are ratios, and they arrive as
    provider strings — but what comes out of the two converters is money, so it is an
    ``int`` in base units produced by integer multiplication and division of the two
    prices' own integer ratios. No float and no rounded intermediate is anywhere in the
    path.

    **The two directions round opposite ways, and both toward refusing.**

    * :meth:`to_native_base_units` **floors**. It converts a curve's own quantities — the
      quote reserve and the remaining headroom — into the units our size is in, and for
      both of those a *smaller* number is the cautious answer: a shallower reserve prices
      our order into more impact, a smaller headroom caps the size lower.
    * :meth:`to_quote_base_units` **ceilings**. It converts our size into the curve's
      units to ask a venue's own arithmetic what our order would do, and there a *larger*
      order is the cautious question.

    So the rounding error can only ever refuse a trade the exact number would have
    allowed, which is the direction every other constant in this module errs in.
    """

    chain: Chain
    token: str
    symbol: str
    decimals: int
    quote_usd: Decimal
    native_usd: Decimal
    observed_ms: int
    source: str

    @property
    def native_per_quote_unit(self) -> Decimal:
        """Whole native units per whole quote unit. For the record, not for the money."""
        return self.quote_usd / self.native_usd

    def to_native_base_units(self, amount_quote_base: int) -> int:
        """``amount`` in the quote asset's base units -> the chain's native base units.

        ``0`` for a non-positive amount, which is not a price claim: every caller checks
        the result before using it, because a reserve that converts to zero is a rate
        problem and has to be reported as one.
        """
        amount = int(amount_quote_base)
        if amount <= 0:
            return 0
        native_decimals = NATIVE_DECIMALS.get(self.chain)
        if native_decimals is None:
            return 0
        try:
            qn, qd = self.quote_usd.as_integer_ratio()
            nn, nd = self.native_usd.as_integer_ratio()
        except (ValueError, OverflowError, InvalidOperation):  # pragma: no cover - guarded
            return 0
        # (quote_usd / native_usd) * 10**(native_decimals - quote_decimals), as one
        # rational, so the scale change and the FX leg are a single floor rather than two.
        num = qn * nd * 10**native_decimals
        den = qd * nn * 10**int(self.decimals)
        if num <= 0 or den <= 0:
            return 0
        return amount * num // den

    def to_quote_base_units(self, amount_native_base: int) -> int:
        """The inverse: the chain's native base units -> the quote asset's base units.

        **Ceiling, and for the mirror image of the reason** :meth:`to_native_base_units`
        floors. This direction is used to ask a venue's own arithmetic what *our* order
        would do to it (``FlapCurveDepth``), so rounding the order UP asks about a slightly
        larger trade: slightly more impact, and a refusal rather than a fill when the size
        is within an atom of the curve's graduation point. Both are the cautious answer.

        The two directions are exact inverses of one rational, not two rates, so they
        cannot drift apart the way two separately-derived numbers would.
        """
        amount = int(amount_native_base)
        if amount <= 0:
            return 0
        native_decimals = NATIVE_DECIMALS.get(self.chain)
        if native_decimals is None:
            return 0
        try:
            qn, qd = self.quote_usd.as_integer_ratio()
            nn, nd = self.native_usd.as_integer_ratio()
        except (ValueError, OverflowError, InvalidOperation):  # pragma: no cover - guarded
            return 0
        num = nn * qd * 10**int(self.decimals)
        den = nd * qn * 10**native_decimals
        if num <= 0 or den <= 0:
            return 0
        return -((-amount * num) // den)


#: ``(chain, quote_asset) -> (expires_ms, QuoteAssetRate)``. Not a constant: process-local
#: memo, see :data:`QUOTE_ASSET_RATE_TTL_S`. **Successes only** — a refusal is not cached,
#: because a provider that was rate-limited one tick is not wrong for the next minute, and
#: a sticky refusal would turn one 429 into a minute of unexplained no-trades.
_quote_rate_cache: dict[tuple[str, str], tuple[int, QuoteAssetRate]] = {}


def reset_quote_asset_cache() -> None:
    """Drop every memoised quote-asset rate. For tests and for a forced re-read."""
    _quote_rate_cache.clear()


def _safe_symbol(value: Any) -> str:
    """A provider-supplied ticker, reduced to something safe to put in a receipt note."""
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "._-")[:12]
    return text or "?"


def _safe_detail(value: Any) -> str:
    """A provider's own note, reduced to something safe to put in a refusal note.

    Colons and the rest are stripped because these notes are read back by substring
    (:data:`_GRADUATED_NOTES`) and concatenated into decision records; provider text is not
    ours to let through unshaped.
    """
    text = "".join(
        ch for ch in str(value or "") if ch.isalnum() or ch in " ._-=+/"
    ).strip()[:60]
    return text.replace(" ", "_") or "no_detail"


def _plain(value: Decimal) -> str:
    """A ``Decimal`` as plain text, so ``1E-7`` never lands in a note. Same rule as
    ``native_price._s``."""
    return format(value, "f")


def _provider_decimal(value: Any) -> Decimal | None:
    """One provider scalar as a ``Decimal``, or ``None``. A bool and ``""`` are ``None``.

    Parsed from the string the provider sent, which is the only lossless path and the same
    rule ``prices``/``native_price`` keep. A non-finite value is ``None`` rather than an
    infinity that would divide a curve into nothing.
    """
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        got = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return got if got.is_finite() else None


#: ``(chain, quote asset) -> (expires_ms, protectable)``. Both outcomes cached; see above.
_protectable_cache: dict[tuple[str, str], tuple[int, bool]] = {}


def reset_protectable_cache() -> None:
    """Forget every protectability verdict. For tests and for an operator probe."""
    _protectable_cache.clear()


def _protectable_key(chain: Chain, token: str | None) -> str:
    """The address as the PROVIDER must receive it, which is also the cache key.

    EVM hex is case-insensitive and is stored lower-cased everywhere in this tree
    (``normalize_address``), so folding it here is both correct and what makes two
    spellings of one asset share a verdict.

    **Base58 is case-SENSITIVE and must not be folded.** This function exists because the
    original spelling of this gate was a bare ``.strip().lower()`` and it handed the
    folded text straight to ``source.quote``. That was harmless while the only callers
    were robinhood and bsc, and it becomes a whole-chain outage the moment Solana calls
    it: a lower-cased mint matches no ``tokens`` row, no ``curve_snapshots`` row, no
    DexScreener pair and no Jupiter route, so every probe would fail and the gate would
    refuse 100% of Solana entries while appearing to work. ``normalize_address`` already
    draws this distinction (``schemas.py:147``); this is the same rule, non-raising,
    because a malformed address here is a refusal and not an exception.
    """
    text = (token or "").strip()
    return text.lower() if chain in EVM_CHAINS else text


def quote_asset_is_protectable(
    chain: Chain, quote_token: str, *, at_ms: int | None = None
) -> tuple[bool, str]:
    """Can the WATCHDOG price this address? ``(protectable, why)``.

    THE RULE THIS ENFORCES: never open a position we cannot monitor.

    Named for its first caller and kept that way, but the question it asks is the general
    one — "does the source the watchdog will actually use return a usable price for this
    address on this chain" — and it has two kinds of caller now:

    * **EVM**, via :func:`quote_asset_rate`, asking about the **quote asset**. There the
      position's own token is priced by the venue reader and it is the quote leg that can
      be invisible; USDG below is the measured case.
    * **Solana**, via ``engine._protectability_probe`` on the entry path, asking about the
      **position's own mint**. There the quote leg is SOL or the entry never got this far
      (``scanner.curve_from_payload`` refuses a non-SOL curve), and it is the token that
      can be unpriceable. Same question, same answer shape, one mechanism.

    The address is passed through :func:`_protectable_key`, NOT lower-cased — see that
    function for why folding a base58 mint would turn this gate into a whole-chain outage.

    Sizing and protection do not share a price path, and an asset can be perfectly
    priceable by one and invisible to the other. :func:`quote_asset_rate` reads the quote
    leg through ``gmgn_cli.token_info``; the watchdog prices a position through
    ``evm_price.QuoteUsd`` -> ``_provider_price_usd`` -> ``prices.price_usd``, which is
    DexScreener. Those disagree, and the disagreement is not rare.

    MEASURED on the live box 2026-09-22 over the 8 most-used non-native quote assets on
    robinhood: 7 priced through both paths (NVDA, META, GME and four unnamed, 496/124/80
    tokens and so on). **USDG -- Global Dollar, 6 decimals, $19.5M liquidity, the quote
    leg of 719 tokens -- priced at $1.00064189754 through gmgn and not at all through the
    protection path**, on three consecutive probes.

    Without this gate every one of those 719 tokens could be entered and would then be
    blind: no working stop, and ``max_blind_halt_entries`` tripping agent-wide after
    ``max_blind_s``. That is the same failure that left two funded bsc positions unable to
    exit for 53 minutes earlier the same day, which is the most expensive defect this
    system has produced. Refusing costs trades we were not making yesterday either.

    Deliberately NOT closed by adding a second source to the exit path: ``gmgn_cli``
    spawns a subprocess with a 45 s timeout against a 5 s protection poll, and a
    protection overrun halts entries on every chain. A faster source could be measured and
    added later; until then the honest answer is to refuse.

    Never raises. An unimportable or raising provider reads as NOT protectable, because
    "we could not check" and "we cannot see it" have the same consequence for a position.
    """
    when = int(at_ms) if at_ms is not None else now_ms()
    token = _protectable_key(chain, quote_token)
    if not token:
        return False, "no_quote_asset"
    key = (chain.value, token)
    for stale in [k for k, (expires, _) in _protectable_cache.items() if expires <= when]:
        _protectable_cache.pop(stale, None)
    cached = _protectable_cache.get(key)
    if cached is not None:
        return cached[1], "cached"
    # Ask the source the WATCHDOG actually uses, resolved the way the watchdog resolves
    # it. `DefaultExitSubmitter._safe_quote` calls `self.price_source.quote(...)`, and
    # `run_watchdog` builds that from `configured_price_source_name()`.
    #
    # This probe was first written against `evm_price._provider_price_usd`, which is
    # DexScreener only, and that was WRONG -- measured on the live box before deploying:
    # the raw provider prices 7 of 59 quote assets (130 of 2,577 tokens), while the
    # configured `venue` source prices all of the top assets except USDG, because it falls
    # back through the curve and the pair. Gating on the raw provider would have refused
    # 95% of the non-native set instead of the ~28% that genuinely cannot be protected.
    # The lesson is the general one: probe the path that will do the work, not a layer
    # underneath it.
    #
    # 2026-10-03: protection now refuses to decide on a pool mark quoted in a third asset
    # (``protection.settlement_mark_chains``, sol by default; journal #5048). A token whose
    # only answer is such a pool would pass here and then sit blind, so the probe settles
    # the mark through the same layers protection asks and refuses if none answers. Solana
    # only: there the probe is asked about the position's OWN mint; on EVM it is asked about
    # the quote asset, whose USD read never goes through the settlement rule.
    try:
        from kaiba.execution.watchdog import (
            PriceQuote,
            configured_price_source_name,
            configured_settlement_mark_chains,
            is_foreign_mark,
            resolve_price_source,
            settle_foreign_mark,
        )
    except Exception as exc:  # noqa: BLE001 - no reader is not a price
        return False, f"protection_reader_unavailable:{type(exc).__name__}"
    try:
        source = resolve_price_source(configured_price_source_name())
        quote = source.quote(chain, token)
        if (chain is Chain.SOL and isinstance(quote, PriceQuote) and quote.usable
                and chain in configured_settlement_mark_chains()
                and is_foreign_mark(quote)):
            quote = settle_foreign_mark(source, chain, token, quote)
            if is_foreign_mark(quote):
                _protectable_cache[key] = (when + int(PROTECTABLE_RETRY_S * 1000), False)
                return False, "protection_mark_not_settled_in_native"
    except Exception as exc:  # noqa: BLE001 - a raising provider is blindness
        return False, f"protection_probe_raised:{type(exc).__name__}"
    price = getattr(quote, "price_usd", None)
    ok = bool(getattr(quote, "usable", False)) and price is not None and price > 0
    ttl = PROTECTABLE_TTL_S if ok else PROTECTABLE_RETRY_S
    _protectable_cache[key] = (when + int(ttl * 1000), ok)
    return ok, ("protectable" if ok else "protection_path_has_no_price")


def quote_asset_rate(
    chain: Chain,
    quote_token: str | None,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
) -> tuple[QuoteAssetRate | None, str]:
    """What one whole unit of ``quote_token`` is worth in ``chain``'s native asset.

    ``(rate, note)``. ``rate is None`` is the only failure shape and the note always says
    which leg was missing:

    ==============================  ==================================================
    ``quote_token_unknown``         no usable quote-asset address to look up
    ``native_usd_unavailable``      no contemporaneous native sample (``native_price``)
    ``quote_price_unavailable``     the provider had no price, or refused
    ``quote_price_not_positive``    a zero or negative price, which is the field absent
    ``quote_price_stale``           outside :data:`QUOTE_ASSET_PRICE_MAX_AGE_S`
    ``quote_decimals_unreadable``   no believable ``decimals``; never defaulted to 18
    ==============================  ==================================================

    Cached per **quote asset** for :data:`QUOTE_ASSET_RATE_TTL_S` — see that constant for
    the 59-assets-2,538-tokens measurement that makes the distinction matter.

    The provider read goes through ``gmgn_cli.token_info``, which is the normal provider
    path: it holds the gmgn limiter bucket, writes a receipt and never raises. It is asked
    at ``Priority.ENTRY`` and told to wait :data:`VENUE_PROBE_WAIT_S` for capacity, because
    this call blocks a live entry and ``docs/CONTRACT.md`` forbids the silent no-op a
    zero-wait refusal would be. Looked up on the module at call time rather than bound at
    import, for the reason :func:`read_flap_venue_depth` gives about its own seam.

    Never raises.
    """
    when = int(at_ms) if at_ms is not None else now_ms()
    token = (quote_token or "").strip().lower()
    if not token or not looks_evm(token) or token == EVM_ZERO:
        # The zero address IS "the chain's native coin" on this venue, so a caller that
        # got here with one has a row that says "not native" and names no other asset.
        # Contradictory evidence is not evidence.
        return None, "quote_token_unknown"

    key = (chain.value, token)
    for stale_key in [k for k, (expires, _) in _quote_rate_cache.items() if expires <= when]:
        _quote_rate_cache.pop(stale_key, None)
    cached = _quote_rate_cache.get(key)
    if cached is not None:
        return cached[1], cached[1].source

    native_usd = _native_usd(chain, conn, at_ms=when)
    if native_usd is None or native_usd <= 0 or not native_usd.is_finite():
        # `native_price.at` already enforces its own five-minute tolerance and already
        # routes robinhood to ETH's samples. Half a ratio is not a rate.
        return None, "native_usd_unavailable"

    try:
        from kaiba.core.limiter import Priority
        from kaiba.providers import gmgn_cli
    except Exception as exc:  # noqa: BLE001 - a missing provider is not a price
        return None, f"quote_price_unavailable:reader_{type(exc).__name__}"
    try:
        got = gmgn_cli.token_info(
            token, chain, priority=Priority.ENTRY,
            wait_for_slot_s=VENUE_PROBE_WAIT_S, conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - a dead provider refuses, it does not raise
        return None, f"quote_price_unavailable:raised_{type(exc).__name__}"
    if not got.ok:
        return None, f"quote_price_unavailable:{_safe_detail(got.receipt.note)}"

    # ``flatten_payload`` is what lifts the nested ``price.price`` to a top-level ``price``
    # — the same flattening ``normalize_security`` already reads this endpoint through.
    flat = gmgn_cli.flatten_payload(got.data)
    price = _provider_decimal(flat.get("price"))
    if price is None:
        return None, f"quote_price_unavailable:{_safe_detail(got.receipt.note or 'no_price_field')}"
    if price <= 0:
        # GMGN reports an absent field as a zero often enough that this module already
        # refuses to read one as an answer (`PONS_CREATOR_TAX_SELECTOR`'s note). A zero
        # here would divide the curve into infinite depth.
        return None, f"quote_price_not_positive:{_plain(price)}"

    raw_decimals = flat.get("decimals")
    decimals: int | None = None
    if isinstance(raw_decimals, int) and not isinstance(raw_decimals, bool):
        decimals = int(raw_decimals)
    elif isinstance(raw_decimals, str) and raw_decimals.strip().isdigit():
        decimals = int(raw_decimals.strip())
    if decimals is None or decimals < 0 or decimals > QUOTE_ASSET_MAX_DECIMALS:
        # Never 18. Most ERC-20s are, and the second-most-used quote asset on this venue
        # is USDG at 6 (MEASURED 2026-09-22, 468 tokens), so "most are" is not an argument
        # about this class of asset. A wrong scale is a depth out by 1e12, silently.
        return None, "quote_decimals_unreadable"

    age_ms = abs(when - int(got.receipt.observed_at_ms))
    budget_ms = int(QUOTE_ASSET_PRICE_MAX_AGE_S * 1000)
    if age_ms > budget_ms:
        # Distance rather than elapsed, the same shape `native_price.at` uses: a replay
        # asking about last week must not be answered with a price read just now.
        return None, f"quote_price_stale:{age_ms // 1000}s>{budget_ms // 1000}s"

    symbol = _safe_symbol(flat.get("symbol"))
    source = (
        f"quote{symbol}({token[:10]}):{decimals}dp:"
        f"usd{_plain(price)}/{_plain(native_usd)}{NATIVE_SYMBOL.get(chain, 'native')}:"
        f"{got.receipt.basis.value}"
    )
    rate = QuoteAssetRate(
        chain=chain, token=token, symbol=symbol, decimals=decimals,
        quote_usd=price, native_usd=native_usd,
        observed_ms=int(got.receipt.observed_at_ms), source=source,
    )
    # LAST, and after everything else has succeeded: can the thing that will have to
    # PROTECT this position see the quote asset at all? See `quote_asset_is_protectable`.
    # Checked here rather than earlier so an asset we cannot price at all refuses for that
    # reason, and so the probe is skipped entirely for the assets that already failed.
    protectable, why = quote_asset_is_protectable(chain, token, at_ms=when)
    if not protectable:
        return None, f"quote_price_unprotectable:{symbol}({token[:10]}):{why}"

    _quote_rate_cache[key] = (when + int(QUOTE_ASSET_RATE_TTL_S * 1000), rate)
    return rate, source


def _pons_quote_refusal(note: str, why: str, gas_price_wei: int | None = None) -> VenueRead:
    """The one shape every non-native-quote refusal takes. Nothing is priced.

    Every field but the gas price is ``None``, so :func:`evm_cost_model` reports
    ``UNAVAILABLE`` and the entry refuses — the depth is left unpriced too rather than
    passed on, because a token on a curve priced off somebody's pool is the mistake
    :data:`FLAP_PROBE_UNAVAILABLE` exists to prevent one venue over.

    The note is checked against :data:`_GRADUATED_NOTES` before it leaves. These notes can
    carry a provider's own words, and one that happened to contain ``no_curve`` would send
    :func:`read_venue` down the graduated fall-back and price this token off a DEX.
    """
    text = f"pons:quote_not_native:{note}:{why}"
    if _left_the_curve(text):  # pragma: no cover - defended, not expected
        text = f"pons:quote_not_native:{note}:refused"
    return VenueRead(NoDepth(source=text), None, None, gas_price_wei, text)


def _pons_curve_address(
    token: str, conn: sqlite3.Connection | None
) -> tuple[str | None, bool | None, str | None, str]:
    """``(curve, quote_is_native, quote_token, note)`` for a Pons token, from our ``tokens`` row.

    ``kaiba.ingest.robinhood`` writes all three out of the ``TokenLaunched`` event it saw,
    so for anything this agent watched launch it is a local read. The quote asset is not a
    detail: 78 of the 400 most recent robinhood launches are quoted in some other ERC-20,
    and for those a size in wei is not the currency the curve takes. Both the flag and the
    address are returned rather than assumed, because the address is what
    :func:`quote_asset_rate` needs to convert one into the other — and because
    ``quote_is_native`` IS ``pair_token == ZERO_ADDRESS`` in the module that writes them
    (``ingest/robinhood.py``), so a row written before the flag existed is still answered
    by its address.
    """
    c = conn if conn is not None else get_conn()
    try:
        row = fetch_one(
            c,
            "SELECT meta_json, migrated_ms FROM tokens WHERE chain=? AND address=?",
            (Chain.ROBINHOOD.value, token),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable row is not a curve
        return None, None, None, f"tokens_row_unreadable:{type(exc).__name__}"
    if row is None:
        return None, None, None, "no_tokens_row"
    if row["migrated_ms"] is not None:
        # Graduated: the curve is complete and the token trades on the Uniswap v4 pool,
        # which this module has no quote path for. Same shape as Solana's `has_graduated`.
        return None, None, None, "graduated"
    try:
        meta = jload(row["meta_json"], {}) or {}
    except Exception:  # noqa: BLE001 - a corrupt meta blob is not a curve
        meta = {}
    if not isinstance(meta, dict):
        return None, None, None, "tokens_meta_not_a_mapping"
    curve = meta.get("curve")
    if not isinstance(curve, str) or not curve:
        return None, None, None, "no_curve_in_tokens_row"
    if not looks_evm(curve):
        # This string becomes the ``to`` of an eth_call. A row we cannot read as an
        # address is a row, not a curve.
        return None, None, None, "curve_in_tokens_row_is_not_an_address"
    native = meta.get("quote_is_native")
    pair = meta.get("pair_token")
    quote = pair.strip().lower() if isinstance(pair, str) and looks_evm(pair.strip()) else None
    return (
        curve.lower(),
        bool(native) if isinstance(native, bool) else None,
        quote,
        "tokens_row",
    )


def _pons_curve_from_factory(
    token: str, conn: sqlite3.Connection | None
) -> tuple[str | None, bool | None, str | None, str]:
    """``getLaunchedToken(token)`` on the Pons factory, for a token we did not watch launch.

    One extra round trip, taken only when the local row has no curve, because the
    alternative is refusing a token purely for having been discovered by a feed rather
    than by our own listener.
    """
    try:
        from kaiba.core.limiter import Priority
        from kaiba.ingest import robinhood as pons
    except Exception as exc:  # noqa: BLE001 - a missing reader is not a curve
        return None, None, None, f"pons_reader_unavailable:{type(exc).__name__}"
    try:
        token_n = pons.normalize(token)
    except ValueError:
        return None, None, None, "not_an_evm_address"
    got = pons.rpc_batch(
        [("eth_call", [{"to": pons.FACTORY_V2,
                        "data": pons.SELECTOR_GET_LAUNCHED_TOKEN + token_n[2:].rjust(64, "0")},
                       "latest"])],
        endpoint="chain.viability_launched",
        priority=Priority.ENTRY,
        wait_for_slot_s=VENUE_PROBE_WAIT_S,
        conn=conn,
    )
    if not got.ok or not got.results:
        return None, None, None, f"factory_unreadable:{(got.note or 'no_response')[:60]}"
    # getLaunchedToken(address) -> (token, curve, deployer, creatorFeeRecipient, pairToken, ...)
    # decoded here rather than through robinhood's own word helpers, which are private to
    # that module and not this one's to depend on.
    curve = _abi_address(got.results[0], 1)
    pair = _abi_address(got.results[0], 4)
    if curve is None or curve == pons.ZERO_ADDRESS:
        return None, None, None, "factory_has_no_curve"
    return curve, (pair or pons.ZERO_ADDRESS) == pons.ZERO_ADDRESS, pair, "factory"


def _block_timestamp_s(header: Any) -> int | None:
    """``timestamp`` off an ``eth_getBlockByNumber`` header as unix seconds, or ``None``.

    ``0x0`` is absent, not the epoch — the rule ``robinhood.log_timestamp_ms`` applies to a
    log's ``blockTimestamp``, for the same reason: a zero here would put the launch 56
    years in the past and read every rung as elapsed. Decoded here because the header is
    one item of this module's own batch and robinhood's word helpers are private to it.
    """
    if not isinstance(header, dict):
        return None
    ts = _hex_word_int(header.get("timestamp"))
    return ts if ts is not None and ts > 0 else None


def _pons_elapsed_s(launched_at_s: int, when_ms: int, head_ts_s: int | None) -> tuple[int, str]:
    """Whole seconds since ``launchedAt`` on an honest clock, and which clock it was.

    The curve prices its toll off ``block.timestamp - launchedAt``; both are chain time.
    Two bases, tried in this order:

    * ``chain_time`` — the head block's own ``timestamp``, read in the same batch as the
      curve. Our order lands in a block at or after that head, so this is a floor on the
      elapsed second the fill will be charged at, and our own clock never enters it.
    * ``wall_clock_minus_lag`` — when the header did not come back: ``when_ms`` less
      :data:`PONS_CHAIN_LAG_MARGIN_S`, the largest head lag observed. Honest as long as
      the lag stays under the margin, which is why the margin is the max and not the mean.

    Floored at 0 on both: before launch, or a clock behind the chain, is the launch second
    and its full toll. Integer milliseconds throughout; nothing here is a float.
    """
    if head_ts_s is not None:
        return max(0, int(head_ts_s) - int(launched_at_s)), "chain_time"
    margin_ms = int(PONS_CHAIN_LAG_MARGIN_S * 1000)
    elapsed_ms = int(when_ms) - int(launched_at_s) * 1000 - margin_ms
    return max(0, elapsed_ms // 1000), "wall_clock_minus_lag"


def read_pons_venue(
    token: str, conn: sqlite3.Connection | None = None, *, at_ms: int | None = None
) -> VenueRead:
    """This Pons token's depth, its two rates and the chain's gas price, in one batch.

    The tax is the sum of two on-chain numbers, and both are charged off the top of the
    quote leg:

    * ``creatorTaxBps()`` — 0 to 500 bps in the wild, verified against the curves' own
      events (:data:`PONS_CREATOR_TAX_SELECTOR`);
    * the anti-sniper tax, ``snipeTaxStartBps()`` over ``snipeTaxSeconds()``, which is
      **9,900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+ s** on every curve read so far
      (MEASURED; ``robinhood.CurveState.snipe_tax_bps_at`` owns the table). It is charged
      for the elapsed second the chain is in, not the one our clock is in: the batch
      carries ``eth_getBlockByNumber('latest')`` and the toll is priced off that header's
      timestamp (``chain_time``); when the header is missing, off the wall clock less
      :data:`PONS_CHAIN_LAG_MARGIN_S` (``wall_clock_minus_lag``). See
      :func:`_pons_elapsed_s`. The basis is named in the note either way.

    **Inside the window the read refuses**, whatever the rung costs. The operator's Pons
    strategy is to wait the toll out, not to buy through it, and the cost gate on its own
    would approve rung 2 (19 bps, 5.13% round trip) — which at wall-clock t=2 s is what a
    chain one second behind us is about to charge 618 bps for. The refusal names the rung,
    the elapsed second and the basis, and still carries the rates it read.

    **A curve quoted in something other than ETH is converted, not refused.** 17% of this
    venue is (MEASURED 2026-09-22: 10 of 60 recent dossiers), the operator authorised
    those pairs, and :func:`quote_asset_rate` supplies the one number that was missing —
    what a whole unit of the quote asset is worth in the chain's own native asset. The
    curve's quote-side integers are converted into native base units with that rate before
    they reach :class:`PonsCurveDepth`, so the depth answers the question the caller is
    really asking: what does *this many wei* cost against *that* curve. Both converted
    quantities are floored, which is the cautious direction for both. The depth's basis
    drops to ``DERIVED`` because half of it is now a provider's opinion about a stock
    price, and the note names the asset, its decimals and both prices. When the rate is
    missing, stale, zero or negative the read refuses by name exactly as it did before.

    Such a curve also carries a ``routing_bps_per_leg``, because our order is denominated
    in ETH and the curve cannot take ETH: the router has to cross a pool each way to get
    into the asset it does take. See :data:`ROUTING_HOPS_NON_NATIVE_QUOTE` — it is the one
    term here that is a minimum rather than a bound, and it is named in the note.

    Never raises. Anything unreadable comes back as ``None`` with a note, which refuses.
    """
    when = int(at_ms) if at_ms is not None else now_ms()
    try:
        from kaiba.core.limiter import Priority
        from kaiba.ingest import robinhood as pons
    except Exception as exc:  # noqa: BLE001 - a missing reader is not a venue
        return VenueRead(NoDepth(source=f"pons_reader_unavailable:{type(exc).__name__}"),
                         None, None, None, f"pons_reader_unavailable:{type(exc).__name__}")

    curve, quote_is_native, quote_token, note = _pons_curve_address(token, conn)
    if curve is None and note in ("no_tokens_row", "no_curve_in_tokens_row"):
        curve, quote_is_native, quote_token, note = _pons_curve_from_factory(token, conn)
    if curve is None:
        return VenueRead(NoDepth(source=f"pons:{note}"), None, None, None, f"pons:{note}")
    rate: QuoteAssetRate | None = None
    routing = Decimal(0)
    if quote_is_native is not True:
        # The curve takes some other ERC-20 and our size is in wei. The two are not the
        # same currency, so before anything is priced we need the one number that relates
        # them: what a whole unit of that asset is worth in this chain's native asset.
        # Resolved BEFORE the curve batch, deliberately — a curve we could not spend on is
        # not worth a round trip, and this is the cheaper of the two reads to lose.
        rate, why = quote_asset_rate(Chain.ROBINHOOD, quote_token, conn, at_ms=when)
        if rate is None:
            return _pons_quote_refusal(note, why)
        # The rate prices the curve. It does not pay for the crossing that gets our ETH
        # into the asset the curve takes — see :data:`ROUTING_HOPS_NON_NATIVE_QUOTE`.
        hop_bps = DEX_FEE_BPS_UPPER.get(Chain.ROBINHOOD)
        if hop_bps is None:  # pragma: no cover - robinhood is in the table
            return _pons_quote_refusal(note, "no_routing_fee_bound")
        routing = Decimal(int(hop_bps) * ROUTING_HOPS_NON_NATIVE_QUOTE)
        note = f"{note}:{rate.source}:hop{routing}bps"

    # Thirteen items, one reservation: ten curve reads, the creator tax, the gas price, and
    # the head block for its timestamp — the clock the curve's own toll runs on. The header
    # is LAST and optional: a failed header falls back to the wall clock less the lag
    # margin, a failed read of anything before it refuses.
    got = pons.rpc_batch(
        [("eth_call", [{"to": curve, "data": selector}, "latest"]) for _, selector in pons.CURVE_READS]
        + [("eth_call", [{"to": curve, "data": PONS_CREATOR_TAX_SELECTOR}, "latest"]),
           ("eth_gasPrice", []),
           ("eth_getBlockByNumber", ["latest", False])],
        endpoint="chain.viability_curve",
        priority=Priority.ENTRY,
        wait_for_slot_s=VENUE_PROBE_WAIT_S,
        conn=conn,
    )
    reads = len(pons.CURVE_READS)
    core = list(got.results[:reads + 2])
    if len(core) < reads + 2 or (not got.ok and any(item is None for item in core)):
        why = (got.note or "no_response")[:80]
        return VenueRead(NoDepth(source=f"pons:unreadable:{why}"), None, None, None,
                         f"pons:unreadable:{why}")
    state = pons.parse_curve_state(
        core[:reads], curve=curve, token=token, quote_is_native=rate is None, observed_ms=when
    )
    creator_tax = _hex_word_int(core[reads])
    gas_price = _hex_word_int(core[reads + 1])
    head_ts_s = _block_timestamp_s(got.results[reads + 2]) if len(got.results) > reads + 2 else None
    if state is None:
        return VenueRead(NoDepth(source="pons:curve_unparseable"), None, None, gas_price,
                         "pons:curve_unparseable")
    if state.graduated:
        return VenueRead(NoDepth(source="pons:graduated"), None, None, gas_price, "pons:graduated")
    if creator_tax is None:
        # The one number no provider has ever got right on this chain. Without it there is
        # no round-trip cost, and a missing tax is not a zero tax.
        return VenueRead(NoDepth(source="pons:creator_tax_unreadable"), None, None, gas_price,
                         "pons:creator_tax_unreadable")
    elapsed_s, basis = _pons_elapsed_s(int(state.launched_at_s), when, head_ts_s)
    at_s = int(state.launched_at_s) + elapsed_s
    snipe = state.snipe_tax_bps_at(at_s)
    timing = f"elapsed{elapsed_s}s:{basis}"
    if state.snipe_tax_start_bps > 0 and state.snipe_tax_seconds > 0 and elapsed_s < state.snipe_tax_seconds:
        # Inside the anti-sniper window on the honest clock. The rung for this second is
        # charged and named, and the depth refuses whatever it costs: the strategy waits the
        # toll out, and rung 2 alone (19 bps) would clear a 7% ceiling while a chain one
        # second behind our clock is still charging rung 1 (618).
        # ``snipe_tax_basis`` says whether the rung is MEASURED or INVENTED. The live box
        # runs an older ``ingest/robinhood.py`` without it, and a reader deployed alone
        # must refuse there too rather than raise.
        shape = getattr(state, "snipe_tax_basis", None)
        shape_note = shape(at_s) if callable(shape) else "shape_basis_unavailable:older_curve_reader"
        why = (
            f"pons:snipe_window:rung{elapsed_s}_{snipe}bps:{timing}:"
            f"window{int(state.snipe_tax_seconds)}s:{shape_note}"
        )
        return VenueRead(NoDepth(source=why), Decimal(int(state.fee_bps)),
                         Decimal(int(creator_tax) + int(snipe)), gas_price, why)
    take = int(state.fee_bps) + int(creator_tax) + int(snipe)
    if take >= 10_000 or take < 0 or state.fee_bps < 0 or creator_tax < 0:
        # 100% of the order to the venue is not a venue. Refuse on the rates rather than
        # let a zero-sized curve leg look like a depth problem.
        return VenueRead(NoDepth(source=f"pons:take_{take}bps_per_leg"), None, None, gas_price,
                         f"pons:take_{take}bps_per_leg")
    headroom = max(0, int(state.graduation_threshold) - int(state.real_quote_reserve))
    quote_reserve = int(state.quote_reserve)
    depth_basis = EvidenceBasis.VERIFIED_ONCHAIN
    if rate is not None:
        # The curve's quote leg is in the stock's base units; our size is in wei. Convert
        # the curve, not the size: the constant product is homogeneous in the quote
        # currency, so a reserve and a headroom expressed in wei make `PonsCurveDepth`
        # answer exactly the question the caller asked, and the token leg — which is
        # already in the token's own atoms — needs no conversion at all.
        quote_reserve = rate.to_native_base_units(quote_reserve)
        headroom = rate.to_native_base_units(headroom)
        if quote_reserve <= 0:
            # The whole reserve floored to nothing: the asset is worth so little against
            # the native unit that the curve cannot be expressed in wei at all. Reported
            # as the rate problem it is, rather than left to surface as "no depth".
            return _pons_quote_refusal(note, "quote_conversion_underflow", gas_price)
        # Exact integers, but half of them now rest on a provider's opinion about a stock
        # price. VERIFIED_ONCHAIN would be a claim about evidence we do not have.
        depth_basis = EvidenceBasis.DERIVED
    depth: Depth = PonsCurveDepth(
        quote_reserve=quote_reserve,
        token_reserve=int(state.token_reserve),
        headroom_base_units=headroom,
        take_bps_per_leg=take,
        basis=depth_basis,
        source=f"pons_curve:{note}:fee{state.fee_bps}:tax{creator_tax}:snipe{snipe}:{basis}",
    )
    return VenueRead(
        depth=depth,
        fee_bps_per_leg=Decimal(int(state.fee_bps)),
        tax_bps_per_leg=Decimal(int(creator_tax) + int(snipe)),
        gas_price_wei=gas_price,
        note=f"pons:{note}:snipe{snipe}bps:{timing}",
        routing_bps_per_leg=routing,
    )


#: ``(chain, token) -> (expires_ms, VenueRead)``. Not a constant: process-local memo, see
#: :data:`VENUE_READ_TTL_S`. Expired rows are dropped on every call, so it is bounded by
#: the tokens one process touches inside the TTL.
_venue_cache: dict[tuple[str, str], tuple[int, VenueRead]] = {}


def reset_venue_cache() -> None:
    """Drop every memoised venue read. For tests and for an operator forcing a re-read."""
    _venue_cache.clear()


def _dossier_tax_bps_per_leg(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal | None, str]:
    """The token's own tax for the WORSE leg, from its dossier. ``(bps, why)``.

    The worse of buy and sell rather than the average: we pay both, and a 0%-in /
    10%-out token costs exactly as much as a 5%/5% one on a round trip while looking
    half as expensive on the mean. Either leg missing is not zero -- a token whose tax we
    could not read is refused, which is the same rule the rest of this module keeps.
    """
    # Read the row here rather than calling `engine.load_dossier`: engine imports this
    # module, so importing it back would be a cycle. Same query, same model.
    try:
        from kaiba.core.schemas import TokenDossier

        row = fetch_one(
            conn if conn is not None else get_conn(),
            "SELECT dossier_json FROM token_dossiers WHERE chain = ? AND address = ?",
            (chain.value, token),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable dossier refuses
        return None, f"dossier_unreadable:{type(exc).__name__}"
    if row is None:
        return None, "no_dossier"
    try:
        dossier = TokenDossier.model_validate_json(row["dossier_json"])
    except Exception as exc:  # noqa: BLE001
        return None, f"dossier_unparseable:{type(exc).__name__}"
    legs: list[Decimal] = []
    for name in ("buy_tax_bps", "sell_tax_bps"):
        measure = getattr(dossier, name, None)
        if measure is None or not getattr(measure, "known", False):
            return None, f"{name}_unavailable"
        try:
            legs.append(Decimal(str(measure.value)))
        except (InvalidOperation, ValueError, TypeError):
            return None, f"{name}_unparseable"
    worst = max(legs)
    if worst < 0:
        return None, f"tax_negative:{worst}"
    return worst, f"tax{worst}bps"


def _evm_gas_price_wei(chain: Chain, conn: sqlite3.Connection | None) -> tuple[int | None, str]:
    """The chain's suggested gas price in wei, via ``gmgn-cli gas-price``. ``(wei, why)``.

    Gas is the smallest term in an EVM round trip by two orders of magnitude (see
    :func:`evm_cost_model`), but ``VenueRead.priced`` requires it, and a reader that
    invented it would be inventing the one number here that is trivially readable.
    """
    try:
        from kaiba.providers.gmgn_cli import gas_price_wei
    except Exception:  # noqa: BLE001 - fall back to the CLI below
        gas_price_wei = None  # type: ignore[assignment]
    if gas_price_wei is not None:
        try:
            got = gas_price_wei(chain, conn=conn)
            if got:
                return int(got), "gas:gmgn"
        except Exception as exc:  # noqa: BLE001
            return None, f"gas_unavailable:{type(exc).__name__}"
    import json as _json
    import shutil as _shutil
    import subprocess as _subprocess

    exe = _shutil.which("gmgn-cli")
    if exe is None:
        return None, "gas_unavailable:no_gmgn_cli"
    try:
        out = _subprocess.run(
            [exe, "gas-price", "--chain", chain.value, "--raw"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        body = _json.loads((out.stdout or "").strip() or "{}")
    except Exception as exc:  # noqa: BLE001 - a dead CLI refuses, it does not raise
        return None, f"gas_unavailable:{type(exc).__name__}"
    for key in ("suggest_base_fee", "average", "low"):
        raw = body.get(key)
        try:
            wei = int(Decimal(str(raw)))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if wei > 0:
            return wei, f"gas:{key}"
    return None, "gas_unavailable:no_usable_field"


def _left_the_curve(note: str | None) -> bool:
    low = (note or "").lower()
    return any(marker in low for marker in _GRADUATED_NOTES)


def read_dex_venue(
    chain: Chain, token: str, conn: sqlite3.Connection | None = None
) -> VenueRead:
    """Rates for a token trading on a plain DEX rather than a curve we can read.

    Returns ``NoDepth`` on purpose. :func:`resolve_depth` already prices these chains from
    the dossier's liquidity and then the pair; this reader exists solely to supply the
    three numbers :func:`evm_cost_model` needs, which is the part that was missing.
    """
    tax, tax_why = _dossier_tax_bps_per_leg(chain, token, conn)
    fee_upper = DEX_FEE_BPS_UPPER.get(chain)
    gas, gas_why = _evm_gas_price_wei(chain, conn)
    fee = Decimal(int(fee_upper)) if fee_upper is not None else None
    missing = []
    if fee is None:
        missing.append(f"no_dex_fee_bound:{chain.value}")
    if tax is None:
        missing.append(tax_why)
    if gas is None:
        missing.append(gas_why)
    note = (
        f"dex:{'+'.join(missing)}" if missing
        else f"dex:fee_upper{fee_upper}bps:{tax_why}:{gas_why}"
    )
    return VenueRead(NoDepth(source=note), fee, tax, gas, note)


def read_flap_venue_depth(
    token: str, conn: sqlite3.Connection | None = None, *, at_ms: int | None = None
) -> tuple[FlapCurveDepth | None, str | None]:
    """Is this token on a Flap curve, and what would a buy cost against it?

    Three answers, and the middle one is the point of the function:

    * ``(depth, None)`` — a checked curve. Exact depth, so nothing below it is consulted.
    * ``(None, None)`` — the portal answered and this token is not on a curve there
      (:data:`_FLAP_NOT_ON_A_CURVE`). It trades on a pool like any other bsc token and
      the caller carries on to the DEX rates reader exactly as before.
    * ``(None, why)`` — the probe did not answer, or answered something that failed its
      own cross-checks. The caller refuses, and :func:`resolve_depth` stops there rather
      than pricing a possible curve token off somebody's pool
      (:data:`FLAP_PROBE_UNAVAILABLE`).

    ``evm_price.read_flap`` is looked up on the module at call time, not bound at import:
    it is the seam a test replaces, and a bound reference would make the real portal the
    only thing this can ever talk to.

    **A curve quoted in something other than BNB is converted, not refused**, by the same
    :func:`quote_asset_rate` the Pons arm uses. MEASURED 2026-09-22 over the 20 most recent
    bsc dossiers: 12 refused with ``quote_not_native``, 5 with ``flap_portal_read_failed``
    and 3 fell through to the DEX rates reader, so **60% of every bsc candidate** was being
    turned away by this one line. When the rate is missing, stale, zero or negative the
    refusal is exactly what it was, still under the :data:`FLAP_PROBE_UNAVAILABLE` prefix
    so the depth ladder still stops here rather than pricing a curve token off a pool.

    Note what is *not* relaxed: ``FlapCurve`` requires 18 decimals on both sides
    (``evm_price.FLAP_CURVE_DECIMALS``) because LibCurve's WAD arithmetic adds a 1e18-scaled
    constant to a base-unit count, and it refuses anything else with
    ``flap_decimals_not_wad``. That gate is upstream of this one and stays. So a
    6-decimal quote asset on this venue is still refused — by the module that owns the
    curve, for a reason about the curve, which is where that judgement belongs.

    Never raises. Anything unreadable becomes a refusal with a note, which refuses.
    """
    try:
        from kaiba.core.limiter import Priority
        from kaiba.execution import evm_price
    except Exception as exc:  # noqa: BLE001 - a missing reader is not a curve
        return None, f"{FLAP_PROBE_UNAVAILABLE}reader_unavailable:{type(exc).__name__}"
    try:
        priced, why = evm_price.read_flap(
            token,
            evm_price.json_rpc_batch(
                Chain.BSC, conn=conn, priority=Priority.ENTRY,
                endpoint="rpc.viability_flap",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a dead endpoint is blindness, not a crash
        return None, f"{FLAP_PROBE_UNAVAILABLE}raised:{type(exc).__name__}"
    if priced is None:
        if any(str(why).startswith(marker) for marker in _FLAP_NOT_ON_A_CURVE):
            return None, None
        return None, f"{FLAP_PROBE_UNAVAILABLE}{why}"
    rate: QuoteAssetRate | None = None
    if priced.quote_token is not None:
        # The curve takes some other ERC-20 and our size is in wei. Get the rate that
        # relates them, or refuse — the two currencies are not addable and never were.
        rate, rate_why = quote_asset_rate(Chain.BSC, priced.quote_token, conn, at_ms=at_ms)
        if rate is None:
            return None, f"{FLAP_PROBE_UNAVAILABLE}quote_not_native:{rate_why}"
    if priced.curve is None:
        return None, f"{FLAP_PROBE_UNAVAILABLE}no_curve_state"
    refusal = priced.curve.refusal
    if refusal is not None:
        return None, f"{FLAP_PROBE_UNAVAILABLE}{refusal}"
    note = priced.note if rate is None else f"{priced.note}:{rate.source}"
    return FlapCurveDepth(
        curve=priced.curve,
        source=f"flap_curve:{note}",
        # Exact integers, but with a rate half of them rest on a provider's opinion about
        # what the quote asset is worth. Same honesty rule as the Pons twin.
        basis=EvidenceBasis.VERIFIED_ONCHAIN if rate is None else EvidenceBasis.DERIVED,
        rate=rate,
    ), None


def _memo(key: tuple[str, str], when_ms: int, read: VenueRead) -> VenueRead:
    """Remember one venue read for :data:`VENUE_READ_TTL_S` and hand it back."""
    _venue_cache[key] = (when_ms + int(VENUE_READ_TTL_S * 1000), read)
    return read


def read_venue(
    chain: Chain,
    token: str | None,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
    allow_network: bool = True,
) -> VenueRead:
    """The venue behind ``token`` on an EVM chain, or a refusal saying which part is missing.

    Two curves have readers in this tree and each one supplies exact depth: Pons on
    Robinhood Chain (``ingest/robinhood.py``, verified against 40 of 40 live fills) and
    Flap on bsc (``evm_price.read_flap``, cross-checked against the curve's own price and
    reserve words). Everything else is a pool, and for a pool this reader supplies only
    the *rates* — :func:`read_dex_venue` — while :func:`resolve_depth` finds the depth.

    A chain with neither returns unpriced — **not** priced-at-zero, which was the shape of
    the hole this module exists to have closed.
    """
    if token is None:
        return VenueRead(NoDepth(source="no_token"), None, None, None, "no_token")
    when = int(at_ms) if at_ms is not None else now_ms()
    key = (chain.value, token)
    for stale_key in [k for k, (expires, _) in _venue_cache.items() if expires <= when]:
        _venue_cache.pop(stale_key, None)
    cached = _venue_cache.get(key)
    if cached is not None:
        return cached[1]
    if chain is not Chain.ROBINHOOD:
        if chain not in DEX_FEE_BPS_UPPER:
            return VenueRead(NoDepth(source=f"no_venue_reader:{chain.value}"), None, None, None,
                             f"no_venue_reader:{chain.value}")
        # No chain-wide curve reader here. Two per-token reads still answer: the token's
        # own launch curve, which is exact depth, and its rates off the dossier.
        curve: FlapCurveDepth | None = None
        probe_note = ""
        if chain in FLAP_CHAINS:
            if allow_network:
                curve, refused = read_flap_venue_depth(token, conn, at_ms=when)
                if refused is not None:
                    return _memo(key, when, VenueRead(
                        NoDepth(source=refused), None, None, None, refused))
                probe_note = f"+{curve.source}" if curve is not None else ""
            else:
                # Not asked, so not answered. Said out loud because a decision record that
                # showed pool depth with no note would read as "the curve said no".
                probe_note = "+flap_probe_disabled"
        got = read_dex_venue(chain, token, conn)
        if curve is not None or probe_note:
            # A curve quoted in something other than the chain's native asset costs a pool
            # crossing each way to reach, on this venue for the same structural reason as
            # on Pons: the order is denominated in BNB and the curve cannot take BNB.
            # See :data:`ROUTING_HOPS_NON_NATIVE_QUOTE`.
            hop = Decimal(0)
            if curve is not None and curve.rate is not None:
                hop = Decimal(
                    int(DEX_FEE_BPS_UPPER[chain]) * ROUTING_HOPS_NON_NATIVE_QUOTE
                )
            got = replace(
                got,
                depth=curve if curve is not None else got.depth,
                note=f"{got.note}{probe_note}{f':hop{hop}bps' if hop else ''}",
                routing_bps_per_leg=hop,
            )
        return _memo(key, when, got)
    if not allow_network:
        return VenueRead(NoDepth(source="pons:probe_disabled"), None, None, None,
                         "pons:probe_disabled")
    got = read_pons_venue(token, conn, at_ms=when)
    if (
        not got.priced
        and (_left_the_curve(got.note) or _limiter_refused(got.note))
        and chain in DEX_FEE_BPS_UPPER
    ):
        # The token GRADUATED off the Pons curve, so there is no curve to read and there
        # never will be again -- but it still trades, on a DEX, and its rates are still
        # readable. Before this, every graduated robinhood token was permanently
        # unpriceable: MEASURED 2026-09-22, all four most recent robinhood signals failed
        # with `pons:graduated`, and robinhood had never placed an order.
        #
        # Only on graduation, never on a curve read that merely failed: a transient RPC
        # error must keep refusing rather than quietly fall back to a looser reader.
        dex = read_dex_venue(chain, token, conn)
        if dex.priced:
            return _memo(key, when, dex)
        got = VenueRead(got.depth, None, None, None, f"{got.note}+{dex.note}")
    return _memo(key, when, got)


def resolve_depth(
    chain: Chain,
    token: str | None,
    conn: sqlite3.Connection | None = None,
    *,
    at_ms: int | None = None,
    allow_network: bool = True,
) -> Depth:
    """This token's depth, cheapest honest source first.

    1. **The bonding curve**, from a fresh ``curve_snapshots`` row on Solana or a live
       read of the curve itself on an EVM venue (:func:`read_venue`). Exact — and only
       possible before graduation. ``has_graduated`` and the ``tokens`` row's
       ``migrated_ms`` decide: a migrated token has no curve and never will, so it is not
       even asked for one.
    2. **The dossier's pool liquidity**, free and local, while it is inside the freshness
       budget its own producer declared.
    3. **The pair itself**, via :func:`pair_depth`. One keyless DexScreener call. This is
       the step whose absence made every post-graduation token unpriceable — which is to
       say every token ``migration-fade`` exists to trade, since it trades the migration.
    4. Nothing, which refuses.

    **A failed curve PROBE stops the ladder at step 1** (:data:`FLAP_PROBE_UNAVAILABLE`).
    Steps 2 and 3 describe a pool; step 1 failing to answer means we do not know whether
    this token even trades on one, and the two are not substitutes.

    Steps 2 and 3 are quoted in USD and need a native price; step 1 is not and does not.
    That ordering is what lets a Pons entry be priced at all today, because
    ``native_prices`` holds no EVM samples and DexScreener could price 1 of 18 robinhood
    tokens (and that one at $1 of liquidity).

    Freshness is measured against ``at_ms`` rather than the wall clock, so replaying a
    past decision asks what we knew *then*. ``Measure.stale`` cannot do that — it ages
    against ``now``, so a replay of yesterday's decisions calls every dossier stale and
    reports the whole chain unpriceable.
    """
    if token is None:
        return NoDepth(source="no_token")
    c = conn if conn is not None else get_conn()
    when = int(at_ms) if at_ms is not None else now_ms()

    curve_note = "curve_is_solana_only"
    if chain is Chain.SOL:
        try:
            if has_graduated(chain, token, c):
                curve_note = "graduated"
            else:
                state, note = snapshot_resolver(c, max_age_s=DEPTH_MAX_AGE_S)(chain, token, when)
                if state is not None:
                    return CurveDepth(state=state, source=f"curve_snapshot:{note}")
                curve_note = note
        except Exception as exc:  # noqa: BLE001 - an unreadable snapshot is not a depth
            curve_note = f"curve_resolver_failed:{type(exc).__name__}"
    elif chain in EVM_CHAINS:
        venue = read_venue(chain, token, c, at_ms=when, allow_network=allow_network)
        if not isinstance(venue.depth, NoDepth):
            return venue.depth
        if venue.depth.source.startswith(FLAP_PROBE_UNAVAILABLE):
            # The one place the ladder must not be a ladder. Everything below this line
            # prices a POOL, and a probe that failed is exactly the state in which we
            # cannot say whether this token's fill would happen on a pool or on a curve
            # we could not read. A curve token priced off an unrelated pool is a number
            # that looks like depth and is about a different venue.
            return venue.depth
        curve_note = venue.note

    native_usd = _native_usd(chain, c, at_ms=when)
    if native_usd is None:
        # The pool's depth is quoted in USD and our size is in base units. Without a
        # native price the two are not comparable, and guessing one is how a $25 pool
        # comes to look like a tradable venue.
        return NoDepth(source=f"{curve_note}:no_native_usd_price")

    liquidity, note = _dossier_liquidity_usd(chain, token, c, at_ms=when)
    if liquidity is not None:
        return PoolDepth(
            liquidity_usd=liquidity, native_usd=native_usd,
            decimals=NATIVE_DECIMALS[chain], source=f"dossier:{curve_note}:{note}",
        )
    if not allow_network:
        return NoDepth(source=f"{curve_note}:{note}:probe_disabled")
    probed = pair_depth(chain, token, c, native_usd=native_usd)
    if isinstance(probed, PoolDepth):
        return replace(probed, source=f"{probed.source}:{curve_note}:{note}")
    return NoDepth(source=f"{curve_note}:{note}:{probed.source}")


def pair_depth(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    native_usd: Decimal | None = None,
) -> Depth:
    """Depth from the token's own pair, for anything past graduation.

    One keyless ``dexscreener.token_pairs`` call, cached by ``_http`` for the 10 s that
    module caps pair data at — so the two depth lookups one decision makes (the sizer's
    and the gate's) cost one request between them. Priced at ``Priority.ENTRY``: it
    blocks an entry, so it outranks discovery and research and yields to exits.

    The **deepest single pool** is used, not the sum across pools: our broker fills one
    route, and adding up pools it will not split across would claim depth we cannot
    reach. ``liquidity_usd`` is the pool's total value, both sides, which is the
    convention ``PaperBroker.impact_bps`` is calibrated on — the point is to agree with
    the fill, not to be independently clever about it.
    """
    try:
        from kaiba.core.limiter import Priority
        from kaiba.providers import dexscreener
    except Exception as exc:  # noqa: BLE001 - a missing provider is not a depth
        return NoDepth(source=f"pair_provider_unavailable:{type(exc).__name__}")
    native = native_usd if native_usd is not None else _native_usd(chain, conn)
    if native is None:
        return NoDepth(source="pair:no_native_usd_price")
    pairs: list[Any] = []
    receipt = None
    for attempt in range(1 + PAIR_PROBE_RETRIES):
        try:
            pairs, receipt = dexscreener.token_pairs(
                chain, token, priority=Priority.ENTRY, conn=conn
            )
        except Exception as exc:  # noqa: BLE001 - a dead provider refuses, it does not raise
            return NoDepth(source=f"pair_probe_failed:{type(exc).__name__}")
        if pairs or attempt >= PAIR_PROBE_RETRIES:
            break
        if "rate limited" not in (getattr(receipt, "note", "") or ""):
            break
        # ``_http`` does not block for capacity: a limiter refusal is an immediate
        # UNAVAILABLE, which here would refuse a live entry over a fraction of a second
        # of spacing. ``pace`` waits out the provider's own minimum interval, and the
        # 10 s pair cache means the retry is the only extra request a decision can make.
        dexscreener.pace(Priority.ENTRY)
    if receipt is None:  # pragma: no cover - the loop always assigns or returns
        return NoDepth(source="pair_probe_no_receipt")
    holding = [
        p for p in pairs
        if p.liquidity_usd is not None and p.liquidity_usd > 0
        and (dexscreener.same_address(p.base_address, token)
             or dexscreener.same_address(p.quote_address, token))
    ]
    if not holding:
        return NoDepth(source=f"pair:none_with_liquidity:{receipt.basis.value}")
    best = max(holding, key=lambda p: p.liquidity_usd or Decimal(0))
    return PoolDepth(
        liquidity_usd=Decimal(best.liquidity_usd or 0), native_usd=native,
        decimals=NATIVE_DECIMALS[chain],
        source=f"pair:{best.dex_id or 'dex'}:{len(holding)}pools",
        basis=EvidenceBasis.PROVIDER_REPORTED,
    )


def _native_pricing_chain(chain: Chain) -> Chain | None:
    """Which chain's stored native samples price ``chain``'s native unit, or ``None``.

    A chain with its own reference pool prices itself. A chain without one is priced by
    another chain holding the *same asset*: Robinhood Chain's native token is ETH, and
    ``ingest/robinhood.py`` (robinhood.py:1053-1055) already argues the case — "ETH's price
    is not chain-specific, so the deep mainnet pools are a better source than anything
    quoted on a one-year-old L2". ARC and STABLE are ETH by the same table.

    Derived from ``NATIVE_SYMBOL`` and ``native_price.WRAPPED_NATIVE`` rather than written
    out, so it cannot drift from the two maps that are the authority. ``engine`` carries
    the same derivation for the ``min_out`` floor; both read the same maps, so they cannot
    disagree without the maps disagreeing, and neither module is the other's to import
    from while both are being edited.
    """
    try:
        from kaiba.providers.native_price import WRAPPED_NATIVE
    except Exception as exc:  # noqa: BLE001 - a missing provider is not a price
        log.debug("native_price unavailable: %s", exc)
        return None
    if chain in WRAPPED_NATIVE:
        return chain
    symbol = NATIVE_SYMBOL.get(chain)
    if not symbol:
        return None
    for candidate in WRAPPED_NATIVE:
        if NATIVE_SYMBOL.get(candidate) == symbol:
            return candidate
    return None


def _native_usd(
    chain: Chain, conn: sqlite3.Connection | None, *, at_ms: int | None = None
) -> Decimal | None:
    """The chain's native token in USD at ``at_ms``, from our own ``native_prices`` table.

    Solana keeps its existing path (``curve_price.sol_usd_from_native_price``) so the two
    modules go on agreeing about SOL. Every other chain asks ``native_price.at`` for the
    nearest stored sample **inside that module's own tolerance** — five minutes, chosen
    there against the stop it feeds — rather than inventing a second freshness rule.
    ``at_ms`` rather than the wall clock for the reason :func:`resolve_depth` gives: a
    replay must be priced with what we knew then.

    ``None`` when no sample is contemporaneous, which refuses. Measured 2026-09-21 on the
    live VPS: ``native_prices`` holds 245 sol rows and **zero** for any other chain,
    because ``config/schedule.yaml``'s ``native_price`` job defaults to ``["sol"]``. So
    every USD-denominated EVM depth is unavailable until that job names more chains — the
    honest state, and one an operator can fix in a line.
    """
    if chain is Chain.SOL:
        try:
            value = sol_usd_from_native_price(conn)
        except Exception as exc:  # noqa: BLE001
            log.debug("native price unavailable: %s", exc)
            return None
        return value if value is not None and value > 0 else None
    source_chain = _native_pricing_chain(chain)
    if source_chain is None:
        return None
    try:
        from kaiba.providers import native_price

        found = native_price.at(source_chain, int(at_ms) if at_ms is not None else now_ms(), conn)
    except Exception as exc:  # noqa: BLE001 - a dead price table is not a price
        log.debug("native price lookup failed for %s: %s", chain.value, exc)
        return None
    value = found.price_usd
    return value if value is not None and value > 0 else None


def _dossier_liquidity_usd(
    chain: Chain, token: str, conn: sqlite3.Connection, *, at_ms: int
) -> tuple[Decimal | None, str]:
    """The pool depth the paper broker will itself be handed, from the same dossier.

    Returns ``(value, note)``. The note says *how* it failed, because "no dossier" and
    "a dossier whose depth had expired" want different fixes and the first version of
    this reported both as the same thing.

    ``load_dossier`` lives in ``engine``, which imports this module's caller, so it is
    imported lazily — the same cycle-avoidance ``curve_price`` uses for the scanner.
    """
    try:
        from kaiba.execution.engine import load_dossier
    except Exception as exc:  # noqa: BLE001 - no dossier loader is no depth
        log.debug("dossier loader unavailable: %s", exc)
        return None, f"dossier_loader_unavailable:{type(exc).__name__}"
    try:
        dossier = load_dossier(chain, token, conn)
    except Exception as exc:  # noqa: BLE001
        log.debug("dossier load failed for %s: %s", token, exc)
        return None, "dossier_unreadable"
    if dossier is None:
        return None, "no_dossier"
    measure = dossier.liquidity_usd
    if not measure.known or measure.value is None or measure.value <= 0:
        return None, "dossier_has_no_liquidity"
    receipt = measure.receipt
    if receipt is None:
        return None, "dossier_liquidity_has_no_receipt"
    age_s = (at_ms - int(receipt.observed_at_ms)) / 1000
    # The producer declared its own budget; honour that rather than invent a second one.
    if age_s > measure.freshness_budget_s or age_s < 0:
        return None, f"dossier_liquidity_stale:{age_s:.0f}s>{measure.freshness_budget_s}s"
    return measure.value, f"age{age_s:.0f}s"


# ------------------------------------------------------------------------ the projection


def estimate_round_trip(
    chain: Chain,
    size_base_units: int,
    conn: sqlite3.Connection | None = None,
    *,
    model: CostModel | None = None,
    depth: Depth | None = None,
    token: str | None = None,
    ceiling_pct: Decimal | None = None,
    impact_legs: int = IMPACT_CHARGED_LEGS,
) -> CostEstimate:
    """Flat + proportional + impact, in base units and as a share of the position.

    The fee terms price the sell leg at the same notional as the buy: the question is
    whether a trade that is *right about nothing* still covers its costs. The impact term
    is one leg's displacement against the pre-trade marginal price, charged ``impact_legs``
    times — see :data:`IMPACT_CHARGED_LEGS` for why that is 1 and not 2.
    """
    size = int(size_base_units)
    if depth is None:
        depth = resolve_depth(chain, token, conn) if token is not None else NoDepth("no_token")
    # Depth first, then the model: on an EVM venue the two come out of one batch and the
    # depth read is what fills the memo the model then hits (:data:`VENUE_READ_TTL_S`).
    model = model if model is not None else cost_model(chain, conn, token=token)

    def blank(impact: int | None, impact_bps: int | None) -> CostEstimate:
        return CostEstimate(
            chain=chain, size_base_units=size, model=model, depth=depth,
            flat_base_units=None, proportional_base_units=None, impact_base_units=impact,
            impact_bps_one_leg=impact_bps, total_base_units=None, pct=None,
            ceiling_pct=ceiling_pct,
        )

    if not model.known or size <= 0:
        return blank(None, None)
    assert model.flat_per_leg_base_units is not None
    assert model.proportional_bps_per_leg is not None

    one_leg_impact: int | None = None
    impact_bps: int | None = None
    if int(impact_legs) > 0:
        one_leg_impact = depth.entry_impact(size)
        if one_leg_impact is None:
            # Unknown impact makes the sum unknown. Never 0 — UNKNOWN_COST_FAILS_CLOSED.
            return blank(None, None)
        impact_bps = int(
            (Decimal(one_leg_impact) * 10_000 / Decimal(size)).to_integral_value(rounding=ROUND_CEILING)
        )

    flat = LEGS_PER_ROUND_TRIP * model.flat_per_leg_base_units
    proportional = int(
        (Decimal(size) * model.proportional_bps_per_leg * LEGS_PER_ROUND_TRIP / 10_000)
        .to_integral_value(rounding=ROUND_CEILING)
    )
    impact = (one_leg_impact or 0) * int(impact_legs)
    total = flat + proportional + impact
    return CostEstimate(
        chain=chain, size_base_units=size, model=model, depth=depth,
        flat_base_units=flat, proportional_base_units=proportional, impact_base_units=impact,
        impact_bps_one_leg=impact_bps, total_base_units=total,
        pct=Decimal(total) * 100 / Decimal(size), ceiling_pct=ceiling_pct,
    )


def _cost_pct(
    chain: Chain, size: int, model: CostModel, depth: Depth, impact_legs: int
) -> Decimal | None:
    est = estimate_round_trip(
        chain, size, model=model, depth=depth, impact_legs=impact_legs
    )
    return est.pct


def _search_ceiling(depth: Depth, model: CostModel) -> int:
    """Upper end of the size search: the pool's own scale, or a flat-cost multiple."""
    declared = depth.max_size_base_units
    if declared is not None and declared > SEARCH_MIN_BASE_UNITS:
        return int(declared)
    flat = model.flat_per_leg_base_units or SEARCH_MIN_BASE_UNITS
    return max(SEARCH_MIN_BASE_UNITS * 2, flat * 100_000)


def _grid(low: int, high: int) -> list[int]:
    out: list[int] = []
    value = Decimal(low)
    while value <= high:
        candidate = int(value)
        if not out or candidate > out[-1]:
            out.append(candidate)
        value *= SEARCH_STEP
    if not out or out[-1] != high:
        out.append(int(high))
    return out


def optimal_size(
    chain: Chain, model: CostModel, depth: Depth, *, impact_legs: int = IMPACT_CHARGED_LEGS
) -> tuple[int | None, Decimal | None]:
    """The size where total round-trip cost per unit of position is lowest.

    Total cost is ``2F/S`` falling plus impact rising, so it is unimodal in ``S``. A
    coarse geometric grid brackets the minimum and a bisection on the bracket refines it
    to the base unit, all on the exact integer arithmetic rather than on the closed form —
    the closed form ``sqrt(2 F L)`` is only right for an idealised constant product and
    the curve rounds against us.
    """
    if not model.known:
        return None, None
    high = _search_ceiling(depth, model)
    if high <= SEARCH_MIN_BASE_UNITS:
        return None, None
    best_size: int | None = None
    best_cost: Decimal | None = None
    for size in _grid(SEARCH_MIN_BASE_UNITS, high):
        cost = _cost_pct(chain, size, model, depth, impact_legs)
        if cost is None:
            continue
        if best_cost is None or cost < best_cost:
            best_cost, best_size = cost, size
    if best_size is None or best_cost is None:
        return None, None

    low = max(SEARCH_MIN_BASE_UNITS, int(Decimal(best_size) / SEARCH_STEP / SEARCH_STEP))
    top = min(high, int(Decimal(best_size) * SEARCH_STEP * SEARCH_STEP))
    while top - low > 1:
        mid = (low + top) // 2
        here = _cost_pct(chain, mid, model, depth, impact_legs)
        there = _cost_pct(chain, mid + 1, model, depth, impact_legs)
        if here is None or there is None:
            break
        if there < here:
            low = mid + 1
        else:
            top = mid
    for candidate in (low, top, best_size):
        cost = _cost_pct(chain, candidate, model, depth, impact_legs)
        if cost is not None and cost < best_cost:
            best_cost, best_size = cost, candidate
    return best_size, best_cost


def _bisect_edge(
    chain: Chain, model: CostModel, depth: Depth, ceiling: Decimal, impact_legs: int,
    inside: int, outside: int,
) -> int:
    """Last size inside the ceiling, walking from ``inside`` toward ``outside``."""
    lo, hi = inside, outside
    while abs(hi - lo) > 1:
        mid = (lo + hi) // 2
        cost = _cost_pct(chain, mid, model, depth, impact_legs)
        if cost is not None and cost <= ceiling:
            lo = mid
        else:
            hi = mid
    return lo


def max_executable_size(
    chain: Chain, depth: Depth, tolerance_bps: int, *, model: CostModel | None = None
) -> int | None:
    """Largest size whose own impact still fits the slippage tolerance.

    This is executability, not economics: above it the broker refuses the order outright
    (it refused 137 of 211 when the size was raised to 0.06 SOL), and a size that cannot
    fill is not a cheap trade, it is no trade.
    """
    if tolerance_bps <= 0:
        return None
    high = _search_ceiling(depth, model or CostModel(
        chain=chain, flat_per_leg_base_units=None, proportional_bps_per_leg=None,
        basis=EvidenceBasis.UNAVAILABLE, source="none",
    ))

    def within(size: int) -> bool:
        impact = depth.entry_impact(size)
        if impact is None or size <= 0:
            return False
        return Decimal(impact) * 10_000 / Decimal(size) <= tolerance_bps

    if not within(SEARCH_MIN_BASE_UNITS):
        return None
    if within(high):
        return high
    lo, hi = SEARCH_MIN_BASE_UNITS, high
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if within(mid):
            lo = mid
        else:
            hi = mid
    return lo


def sizing_band(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    token: str | None = None,
    model: CostModel | None = None,
    depth: Depth | None = None,
    ceiling_pct: Decimal | None = None,
    tolerance_bps: int | None = None,
    cfg: RiskConfig | None = None,
    with_alternates: bool = False,
) -> SizingBand:
    """Every size at which this trade is economic *and* executable, or why there are none.

    This is the answer to "how big should this be", and it is per token: the flat cost
    sets the floor, the token's own depth sets the ceiling, and on a thin enough pool the
    floor is above the ceiling and the honest output is that no size works.
    """
    cfg = cfg if cfg is not None else get_risk()
    depth = depth if depth is not None else resolve_depth(chain, token, conn)
    model = model if model is not None else cost_model(chain, conn, token=token)
    ceiling = ceiling_pct if ceiling_pct is not None else configured_ceiling_pct(cfg)
    tolerance = tolerance_bps if tolerance_bps is not None else slippage_tolerance_bps(cfg)

    band = SizingBand(
        chain=chain, model=model, depth=depth, ceiling_pct=ceiling,
        slippage_tolerance_bps=tolerance,
    )
    if not model.known:
        return replace(band, reason=f"cost_model_unavailable:{model.source}")
    if ceiling is None:
        return replace(band, reason=f"ceiling_invalid:bounds.{CEILING_KEY}")
    if isinstance(depth, NoDepth) or depth.entry_impact(SEARCH_MIN_BASE_UNITS) is None:
        return replace(band, reason=f"depth_unavailable:{depth.source}")

    best, best_cost = optimal_size(chain, model, depth)
    executable = max_executable_size(chain, depth, tolerance, model=model)
    alternates: tuple[int | None, int | None, int | None] = (None, None, None)
    if with_alternates:
        alternates = (
            optimal_size(chain, model, depth, impact_legs=0)[0],
            best,
            optimal_size(chain, model, depth, impact_legs=2)[0],
        )
    band = replace(
        band, optimal_base_units=best, optimal_cost_pct=best_cost,
        max_executable_base_units=executable, optimal_by_impact_legs=alternates,
    )
    if best is None or best_cost is None:
        return replace(band, reason="no_priceable_size")
    if best_cost > ceiling:
        # The bottom of the U is already above the ceiling: no size is economic here.
        return replace(band, reason=f"no_viable_size:cheapest_is_{best_cost:.2f}pct>{ceiling}pct")

    high = _search_ceiling(depth, model)
    lower = _bisect_edge(chain, model, depth, ceiling, IMPACT_CHARGED_LEGS, best, SEARCH_MIN_BASE_UNITS)
    upper = _bisect_edge(chain, model, depth, ceiling, IMPACT_CHARGED_LEGS, best, high)
    band = replace(band, min_viable_base_units=lower, max_viable_base_units=upper)
    if executable is not None and executable < lower:
        return replace(
            band,
            reason=(
                f"no_viable_size:executable_max_{executable}_below_economic_floor_{lower}"
            ),
        )
    return replace(band, reason="band")




def check_size(
    chain: Chain,
    size_base_units: int,
    conn: sqlite3.Connection | None = None,
    *,
    token: str | None = None,
    model: CostModel | None = None,
    depth: Depth | None = None,
    ceiling_pct: Decimal | None = None,
    cfg: RiskConfig | None = None,
) -> ViabilityVerdict:
    """Is this position the right size on this chain, for this token?

    Entries only. Never call this from an exit path: a refusal here means "do not open",
    and the same refusal applied to a sell means "never close", which is how an 8.8% fee
    becomes a 100% loss.

    ``token=None`` is the one case that does not refuse on unknown depth. It means the
    caller has not named an instrument, not that the instrument is unpriceable — the fee
    arm is still enforced and the findings say the impact arm was not evaluated. Every
    production path (``engine._risk_refusal``) passes a token.

    That affordance is Solana-only in effect, and not by exception: on an EVM chain the
    fee arm *is* the token (:func:`evm_cost_model`), so a tokenless EVM call has no fee to
    enforce and refuses on ``cost_model_unavailable`` like any other unpriceable entry.
    """
    cfg = cfg if cfg is not None else get_risk()
    ceiling = ceiling_pct if ceiling_pct is not None else configured_ceiling_pct(cfg)
    if depth is None:
        depth = resolve_depth(chain, token, conn) if token is not None else NoDepth("no_token")
    model = model if model is not None else cost_model(chain, conn, token=token)
    tokenless = token is None and isinstance(depth, NoDepth) and depth.source == "no_token"

    if ceiling is None:
        estimate = estimate_round_trip(
            chain, size_base_units, conn, model=model, depth=depth, impact_legs=0
        )
        return ViabilityVerdict(
            ok=False, reason=f"round_trip_ceiling_invalid:bounds.{CEILING_KEY}",
            estimate=estimate, findings=estimate.findings(),
        )

    if tokenless:
        # Fee arm only. Reported as such so a decision record never reads as if the
        # token's depth had been checked and found fine.
        estimate = estimate_round_trip(
            chain, size_base_units, conn, model=model, depth=depth,
            ceiling_pct=ceiling, impact_legs=0,
        )
        findings = [*estimate.findings(), "impact_arm_skipped:no_token"]
        if estimate.pct is None:
            return _unknown(chain, estimate, findings)
        if estimate.pct > ceiling:
            return ViabilityVerdict(
                ok=False, estimate=estimate, findings=findings,
                reason=f"below_economic_floor:{estimate.pct:.2f}pct>{ceiling}pct:fees_only",
            )
        return ViabilityVerdict(
            ok=True, reason="round_trip_within_ceiling:fees_only", estimate=estimate,
            findings=findings,
        )

    band = sizing_band(
        chain, conn, token=token, model=model, depth=depth, ceiling_pct=ceiling, cfg=cfg
    )
    estimate = estimate_round_trip(
        chain, size_base_units, conn, model=model, depth=depth, ceiling_pct=ceiling
    )
    findings = [*estimate.findings(), *band.findings()]

    if estimate.pct is None:
        return _unknown(chain, estimate, findings, band=band)
    if not band.viable:
        return ViabilityVerdict(
            ok=False, reason=band.reason, estimate=estimate, band=band, findings=findings
        )
    size = int(size_base_units)
    assert band.min_viable_base_units is not None and band.upper is not None
    if size < band.min_viable_base_units:
        return ViabilityVerdict(
            ok=False, estimate=estimate, band=band, findings=findings,
            reason=(
                f"below_economic_floor:{estimate.pct:.2f}pct>{ceiling}pct:"
                f"min={band.min_viable_base_units}"
            ),
        )
    if band.max_executable_base_units is not None and size > band.max_executable_base_units:
        return ViabilityVerdict(
            ok=False, estimate=estimate, band=band, findings=findings,
            reason=(
                f"above_executable_size:{estimate.impact_bps_one_leg}bps_impact:"
                f"max={band.max_executable_base_units}"
            ),
        )
    if size > band.upper:
        return ViabilityVerdict(
            ok=False, estimate=estimate, band=band, findings=findings,
            reason=(
                f"above_economic_ceiling:{estimate.pct:.2f}pct>{ceiling}pct:"
                f"max={band.upper}"
            ),
        )
    return ViabilityVerdict(
        ok=True, reason="round_trip_within_ceiling", estimate=estimate, band=band,
        findings=findings,
    )


def _unknown(
    chain: Chain, estimate: CostEstimate, findings: list[str], band: SizingBand | None = None
) -> ViabilityVerdict:
    if UNKNOWN_COST_FAILS_CLOSED:
        return ViabilityVerdict(
            ok=False, reason=f"round_trip_cost_unknown:{chain.value}:{estimate.depth.source}",
            estimate=estimate, band=band, findings=findings,
        )
    return ViabilityVerdict(
        ok=True, reason="round_trip_cost_unknown_allowed", estimate=estimate, band=band,
        findings=findings,
    )


def describe(
    chain: Chain, size_base_units: int, conn: sqlite3.Connection | None = None,
    *, token: str | None = None, depth: Depth | None = None,
) -> str:
    """One human line, for a report or an operator asking why a size was refused."""
    verdict = check_size(chain, size_base_units, conn, token=token, depth=depth)
    est = verdict.estimate
    unit = Decimal(10) ** NATIVE_DECIMALS[chain]
    size = Decimal(size_base_units) / unit
    if est.pct is None:
        return f"{chain.value} {size} — cost unknown ({est.depth.source}), entry refused"
    band = verdict.band
    window = "none"
    if band is not None and band.viable:
        window = f"{Decimal(band.min_viable_base_units or 0) / unit}..{Decimal(band.upper or 0) / unit}"
    return (
        f"{chain.value} {size} — round trip {est.pct:.2f}% "
        f"(flat {Decimal(est.flat_base_units or 0) / unit}, impact {est.impact_bps_one_leg}bps) "
        f"vs ceiling {est.ceiling_pct}% — band {window} — "
        f"{'ok' if verdict.ok else 'REFUSED: ' + verdict.reason}"
    )


__all__ = [
    "CostEstimate",
    "CostModel",
    "CurveDepth",
    "Depth",
    "FlapCurveDepth",
    "NoDepth",
    "PROVENANCE",
    "PonsCurveDepth",
    "PoolDepth",
    "Provenance",
    "QuoteAssetRate",
    "SizingBand",
    "VenueRead",
    "ViabilityVerdict",
    "check_size",
    "configured_ceiling_pct",
    "cost_model",
    "declared_cost_model",
    "derive_cost_model",
    "describe",
    "estimate_round_trip",
    "evm_cost_model",
    "max_executable_size",
    "optimal_size",
    "pair_depth",
    "pool_depth_from_round_trip",
    "quote_asset_rate",
    "read_dex_venue",
    "read_flap_venue_depth",
    "read_pons_venue",
    "read_venue",
    "reset_quote_asset_cache",
    "reset_venue_cache",
    "resolve_depth",
    "sizing_band",
    "slippage_tolerance_bps",
]
