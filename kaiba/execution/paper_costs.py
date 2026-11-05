"""Live costs the paper broker's own model leaves out, charged where a price is handed to it.

``paper.py`` is pinned by Codex's MODEL9 campaign, so these are applied at the two call sites
that give the broker its price: the engine's paper ENTRY and protection's paper EXIT. MEASURED
on live fills (docs/research/claude-profit-research-20261018/box-evidence.md Q3 and the evidence
catalog's cost section, 2026-10-18; the sol entry drift re-measured 2026-10-21):

* GMGN's router fee, about 1% per leg on every chain. The ledger never recorded it.
* Robinhood's pool hook fee, about 2.5% per leg: live robinhood fills cost 4.6% / 4.0% per leg
  against the paper model's 1.5% / 1.3%.
* The market's move between the decision quote and the pool's price when a sol buy lands: a
  median 0.88% against us, net of the pool fee and our impact (:data:`ENTRY_LATENCY_BPS`).
  Paper fills at the decision price; a live buy lands seconds later.

An entry pays more per token (``price x (1 + bps)``, and a token's buy tax as the share of the
tokens it keeps: ``/ (1 - tax)``, :func:`entry_price`) and an exit receives less
(``price x (1 - bps)``); the broker's own fee, slippage and impact model still applies on top,
so nothing charged here may contain any of those three -- a figure that does is a double count
(it happened: see :data:`ENTRY_LATENCY_BPS`). These are measured medians, not worst cases -- a
paper result that clears them has cleared what a live fill paid, not what one might.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.core.schemas import Chain

ROUTER_FEE_BPS: dict[Chain, int] = {Chain.SOL: 100, Chain.BSC: 100, Chain.ROBINHOOD: 100,
                                    Chain.ETH: 100, Chain.BASE: 100}
#: Chains where :data:`ROUTER_FEE_BPS` is an ASSUMPTION, not a measurement (design 6c): GMGN's 1% on
#: eth and base has never been read back off a fill -- no live order has gone out there. Charged
#: anyway (leaving it out would authorise trades that lose money) and labelled on every paper entry
#: (``router_fee_assumed`` in the ``paper_entry_priced`` event and the basis tag).
ASSUMED_ROUTER_FEE_CHAINS: frozenset[Chain] = frozenset({Chain.ETH, Chain.BASE})
#: eth / base (2026-10-23): 40 bps, charged ONLY on a leg priced at a MARK (the pool could not be read
#: at our quantity). Uniswap's top tier is 1% (its published fee schedule), and the broker already
#: charges 60 bps on these chains (``paper.DEFAULT_FEE_BPS``, pinned): 100 - 60 = 40, so a mark leg pays
#: the worst tier. A quoter or v2-formula fill carries its pool's own fee and pays none of this
#: (:data:`FEE_IN_PRICE_FILLS`). Before this an eth / base mark exit paid no pool fee at all (design 6c [R]).
VENUE_EXTRA_BPS: dict[Chain, int] = {Chain.ROBINHOOD: 250, Chain.ETH: 40, Chain.BASE: 40}

#: The market's move between the decision quote and the moment a sol buy lands, NET of the
#: venue fee and of our own impact, because the broker charges both itself: the PumpSwap tier-1
#: fee (120 bps, ``paper.PaperBroker._dex_fee_bps``) and its impact model with a 30 bps floor.
#:
#: MEASURED 2026-10-21 on n=112 live PumpSwap buys from ``fill_costs``
#: (docs/research/claude-profit-ways-2026-10-21/VERIFICATION.md, section 1 row 12 and section 4
#: row 1): the pool's spot price just before our swap against the decision price, median +87.9 bps
#: against us. The MEAN is -173.3 bps, i.e. favourable on average; the median is kept because it
#: is the conservative of the two (the mean would charge nothing).
#:
#: It replaces :data:`SUPERSEDED_ENTRY_LATENCY_BPS` (220), which was the median of
#: ``exec_vs_decision_bps`` (+219 on the same fills): the fill price against the decision price,
#: which already contains the pool fee (median 120 bps of the swap) and our impact. With the
#: broker's own fee on top, a paper pool entry paid 120 + 100 + 220 = 440 bps before impact and
#: the floor, against a live all-in median of +322 bps (fees, router, impact and drift, same 112
#: fills). Now 120 + 100 + 88 = 308. Paper was biased toward FAIL by ~1.3 pp per sol pool entry.
#:
#: Pool entries only, in practice: a curve-priced buy fills off the reserves and the broker
#: ignores the price handed to it (``paper.PaperBroker.buy``), modelling its own adverse drift
#: over the delay instead. The 11 measured curve buys (median -161 bps) are too few for a figure
#: of their own.
ENTRY_LATENCY_BPS: dict[Chain, int] = {Chain.SOL: 88}

#: What :data:`ENTRY_LATENCY_BPS` charged from 2026-10-19 (90da60f) until the fee-net fix was
#: deployed (``kaiba.learning.paper_report.PAPER_COST_FIX_MS``). Not used to price anything; kept
#: so the report can say how much more the earlier paper entries paid, and a re-score can undo it.
SUPERSEDED_ENTRY_LATENCY_BPS: dict[Chain, int] = {Chain.SOL: 220}
_TEN_K = Decimal(10_000)


#: Fills whose price already contains the pool's own fee AND its hook's cut, measured per token at
#: our size: the v2 formula (the pair's fee), QuoterV2 (the tier) and V4Quoter (it runs the swap
#: through the PoolManager, hooks included). On such a fill :data:`VENUE_EXTRA_BPS` -- robinhood's
#: flat stand-in for exactly that hook fee -- is not added again. MEASURED 2026-10-21 on two Pons
#: graduations (box read, ``tests/test_evm_pool_price.py``): V4Quoter at 0.01 ETH sat +113 / +216
#: bps above spot on the buy and -104 / -204 bps below it on a 40k-token sell, against in-range
#: depth that makes our own impact ~12 bps, i.e. a per-token hook fee of ~1-2% that the flat 250
#: would have charged a second time. A curve MARK (Pons) contains no such fee and keeps the 250.
FEE_IN_PRICE_FILLS: frozenset[str] = frozenset({"v2_formula", "quoter_v2", "v4_quoter"})


#: A buy tax at or above 100% delivers nothing; :func:`entry_price` charges at most this, so the
#: price stays finite (and absurdly high: the trade books a near-total loss, never a gain).
MAX_BUY_TAX_BPS = 9_999


def _buy_tax(tax_bps: Any) -> int:
    return min(MAX_BUY_TAX_BPS, max(0, int(tax_bps or 0)))


def _tax_gross_up(tax_bps: Any) -> Decimal:
    """``1 / (1 - t)`` for a buy tax, rounded UP to 20 decimal places. Terminating on purpose: a
    price times it is as exact as ``price x (1 + bps)`` always was, so the mark and the executable
    of one fill keep their exact ratio (the broker floors the impact it reads off that ratio, and a
    repeating quotient would floor an exact 50 bps to 49). Never below the true factor."""
    tax = _buy_tax(tax_bps)
    if not tax:
        return Decimal(1)
    with localcontext() as ctx:
        ctx.prec = 50
        inv = Decimal(1) / (1 - Decimal(tax) / _TEN_K)
    return inv.quantize(Decimal("1e-20"), rounding=ROUND_CEILING)


def venue_charge_bps(chain: Chain, venue_fee_in_price: bool, venue_bps: int | None) -> int:
    """The venue stand-in for one leg: none on a fill that already carries the venue's fee
    (:data:`FEE_IN_PRICE_FILLS`), else ``venue_bps`` when the caller read the venue's own take for
    this token (:func:`pons_curve_venue_bps`), else the chain's flat :data:`VENUE_EXTRA_BPS`."""
    if venue_fee_in_price:
        return 0
    if venue_bps is not None:
        return max(0, int(venue_bps))
    return VENUE_EXTRA_BPS.get(chain, 0)


def entry_bps(chain: Chain, tax_bps: int = 0, *, venue_fee_in_price: bool = False,
              venue_bps: int | None = None, gas_bps: int = 0) -> int:
    """The entry's cost in bps of the quote. With a buy tax this is the EFFECTIVE figure of
    :func:`entry_price` -- ``(1 + costs) / (1 - tax) - 1`` -- rounded up, so the receipt never
    reads cheaper than what was charged; with none it is the plain sum. ``gas_bps`` is the gas a
    live round trip pays beyond the broker's flat per-leg charge (:func:`gas_extra_bps`)."""
    venue = venue_charge_bps(chain, venue_fee_in_price, venue_bps)
    costs = ROUTER_FEE_BPS.get(chain, 100) + venue + ENTRY_LATENCY_BPS.get(chain, 0) + max(0, int(gas_bps or 0))
    if not _buy_tax(tax_bps):
        return costs
    effective = (_TEN_K + costs) * _tax_gross_up(tax_bps) - _TEN_K
    return int(effective.to_integral_value(rounding=ROUND_CEILING))


def exit_bps(chain: Chain, tax_bps: int = 0, *, venue_fee_in_price: bool = False,
             venue_bps: int | None = None) -> int:
    venue = venue_charge_bps(chain, venue_fee_in_price, venue_bps)
    return ROUTER_FEE_BPS.get(chain, 100) + venue + max(0, int(tax_bps or 0))


#: An exit leg whose costs reach this many bps of the quote receives NOTHING.
TOTAL_LOSS_BPS = 10_000


def exit_is_total_loss(chain: Chain, tax_bps: Any = 0, *, venue_fee_in_price: bool = False,
                       venue_bps: int | None = None) -> bool:
    """Whether a paper sell at these costs receives nothing: router + venue + the token's sell tax
    at or above 100% of the quote.

    2026-10-23 (step 3 follow-up, blocking): :func:`exit_price` has no floor, so such a leg priced
    NEGATIVE, ``PaperBroker.sell`` refused it ``no_price`` on every tick, and the position never
    closed -- its ~-100% loss never booked, its shadow-cap slot held. MEASURED shape: GMGN reports a
    honeypot as ``sell_tax`` 1, which dyor scales to 10,000 bps, and protection charges the HIGHER of
    the entry's and the current dossier's figure; a robinhood measured sell tax may read up to 9,999.
    Such a leg is booked at zero proceeds instead (``watchdog.DefaultExitSubmitter._paper``)."""
    try:
        tax = max(0, int(tax_bps or 0))
    except (TypeError, ValueError):
        tax = 0
    return exit_bps(chain, tax, venue_fee_in_price=venue_fee_in_price, venue_bps=venue_bps) >= TOTAL_LOSS_BPS


def overcharge_before_fix_bps(chain: Chain) -> int:
    """How many more bps a paper entry on ``chain`` paid before the fee-net fix than it pays now."""
    return SUPERSEDED_ENTRY_LATENCY_BPS.get(chain, 0) - ENTRY_LATENCY_BPS.get(chain, 0)


def entry_price(chain: Chain, price_usd: Decimal, *, tax_bps: int = 0, venue_fee_in_price: bool = False,
                venue_bps: int | None = None, gas_bps: int = 0) -> Decimal:
    """What a paper buy should pay per token: the quote plus the costs a live buy pays.

    ``tax_bps`` is the token's own buy tax, charged ONLY on a price that excludes it (a reserves
    mark, the v2 formula, a QuoterV2/V4Quoter quote -- none of them runs the token's transfer).
    A Flap curve fill built with ``evm_price.flap_buy_net_quote(..., buy_tax_bps)`` already nets
    it, and its caller passes 0 (:func:`leg_tax_bps` with ``in_price=True``).

    The tax is charged as what a fee-on-transfer buy DELIVERS: the pool sends ``out`` tokens and
    the token keeps ``t`` of them, so the same spend buys ``out x (1 - t)`` and the price per token
    received is ``price / (1 - t)``. 2026-10-23 (step 3 follow-up): it was ``price x (1 + t)``, an
    additive charge that under-costs every taxed buy (10%: +10.0% against +11.1%; 30%: +30.0%
    against +42.9%) -- optimistic, on exactly the tokens the tax makes worst. The router, venue and
    latency bps stay additive (they are charged on the quote); the sell side (:func:`exit_price`)
    already takes the tax off the proceeds, ``1 - costs - t``, which is at least as pessimistic as
    the product form.

    ``venue_bps`` replaces the flat :data:`VENUE_EXTRA_BPS` with the venue's own take read for this
    token (a Pons curve leg: :func:`pons_curve_venue_bps`). ``gas_bps``: :func:`gas_extra_bps`."""
    costs = entry_bps(chain, 0, venue_fee_in_price=venue_fee_in_price, venue_bps=venue_bps, gas_bps=gas_bps)
    return Decimal(price_usd) * (1 + Decimal(costs) / _TEN_K) * _tax_gross_up(tax_bps)


# --------------------------------------------------------------------------------------
# gas: a spike must never make paper cheaper than live (design 6c, 2026-10-23)
# --------------------------------------------------------------------------------------

#: Transactions a live round trip sends, on average: the buy, the sell, and an approval or a retry
#: on some of them. The design's figure (step 6c), INVENTED in the sense that no eth / base live
#: round trip exists to count; above 2 on purpose, because the broker charges exactly 2 legs.
LIVE_TXS_PER_ROUND_TRIP = Decimal("2.3")
#: Gas units per swap leg, the same bound ``viability.EVM_SWAP_GAS_UNITS`` prices (median Pons buy
#: 203,992 / sell 138,646 measured; 250,000 above both).
SWAP_GAS_UNITS = 250_000
#: Chains whose paper entry runs the guard (where gas is read on the pool host).
GAS_GUARD_CHAINS: frozenset[Chain] = frozenset({Chain.ETH, Chain.BASE})


def broker_tip_native(chain: Chain) -> int:
    """The flat per-leg charge ``PaperBroker`` takes for gas on ``chain`` (``paper.DEFAULT_TIP_NATIVE``,
    pinned; read, never edited). 0 when unreadable, so the guard then charges ALL of the live gas."""
    try:
        from kaiba.execution.paper import DEFAULT_TIP_NATIVE

        return max(0, int(DEFAULT_TIP_NATIVE.get(chain, 0)))
    except Exception:  # noqa: BLE001
        return 0


def gas_extra_bps(chain: Chain, gas_price_wei: int, size_base_units: int, *,
                  gas_units: int = SWAP_GAS_UNITS) -> int:
    """bps of ``size_base_units`` the live round trip's gas exceeds the broker's flat charge by, else 0.

    ``gas_units x gas_price x`` :data:`LIVE_TXS_PER_ROUND_TRIP` against ``2 x`` :func:`broker_tip_native`,
    the difference charged on the ENTRY, rounded up. MEASURED 2026-10-23 (box, read-only): eth 0.062
    gwei -> 3.57e13 wei a round trip against the broker's 4e14 (11x over), base 0.006 gwei -> 3.45e12
    against 4e13; the guard is 0 today and exists for the spike. A gas price that is missing or not
    positive is not "no extra": the caller refuses (``paper_gas_unknown``)."""
    size = int(size_base_units or 0)
    if size <= 0 or int(gas_price_wei or 0) <= 0:
        return 0
    live = Decimal(int(gas_units)) * Decimal(int(gas_price_wei)) * LIVE_TXS_PER_ROUND_TRIP
    broker = Decimal(2 * broker_tip_native(chain))
    if live <= broker:
        return 0
    return int(((live - broker) * _TEN_K / Decimal(size)).to_integral_value(rounding=ROUND_CEILING))


def exit_price(chain: Chain, price_usd: Decimal, *, tax_bps: int = 0, venue_fee_in_price: bool = False,
               venue_bps: int | None = None) -> Decimal:
    """What a paper sell should receive per token: the quote less the costs a live sell pays.
    ``tax_bps`` is the token's sell tax, under the same rule as :func:`entry_price`; with
    ``venue_fee_in_price`` (a :data:`FEE_IN_PRICE_FILLS` fill) the venue stand-in is not added.

    NOT floored: at :data:`TOTAL_LOSS_BPS` or more this is zero or negative, which the broker
    refuses. A caller must test :func:`exit_is_total_loss` first and book such a leg at zero."""
    return Decimal(price_usd) * (1 - Decimal(exit_bps(chain, tax_bps, venue_fee_in_price=venue_fee_in_price,
                                                      venue_bps=venue_bps))
                                 / _TEN_K)


# --------------------------------------------------------------------------------------
# a Pons curve leg pays the curve's own take (2026-10-23, step 3 follow-up, blocking)
# --------------------------------------------------------------------------------------
#
# A robinhood Pons CURVE leg (entry ``curve:pons``, exit ``curve_mark``) is a curve MARK: no fee is
# in that price. It used to pay the flat 250 bps :data:`VENUE_EXTRA_BPS` and never read the token's
# own rates. Live, the curve takes ``feeBps()`` (100 on 250/250 curves read, ``lanes.py``) plus
# ``creatorTaxBps()`` (0-500 per token, ``viability.read_pons_venue``) off the quote leg, on top of
# GMGN's router 1%. Paper charged router 100 + venue 250 + the broker's own robinhood fee 60 = 410
# per leg; live charges 100 + 100 + creatorTax, so every token with a creator tax above 210 bps was
# under-charged, by up to ~290 bps a leg -- on rows tagged honest.

#: The broker's own fee on a robinhood leg if :mod:`kaiba.execution.paper` cannot be read: none, so
#: the venue charge then carries the whole take (the pessimistic direction).
_BROKER_FEE_FALLBACK_BPS = 0


def broker_fee_bps(chain: Chain) -> int:
    """The fee ``PaperBroker`` charges on a ``chain`` leg by itself (``paper.DEFAULT_FEE_BPS``,
    what the call sites' default broker uses), so a venue charge does not count it twice."""
    try:
        from kaiba.execution.paper import DEFAULT_FEE_BPS

        return max(0, int(DEFAULT_FEE_BPS.get(chain, _BROKER_FEE_FALLBACK_BPS)))
    except Exception:  # noqa: BLE001 - unreadable: subtract nothing, charge the whole take
        return _BROKER_FEE_FALLBACK_BPS


def pons_curve_take_bps(fee_bps: Any, creator_tax_bps: Any) -> int | None:
    """``feeBps() + creatorTaxBps()`` read off the curve, or ``None`` when either is missing or not
    a sane integer in ``[0, 10000)``. ``None`` is UNKNOWN, never zero."""
    out = 0
    for raw in (fee_bps, creator_tax_bps):
        if raw is None or isinstance(raw, bool):
            return None
        try:
            exact = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return None
        if not exact.is_finite() or exact != exact.to_integral_value() or not 0 <= exact < 10_000:
            return None
        out += int(exact)
    return out if out < 10_000 else None


def pons_curve_venue_bps(take_bps: int | None) -> int | None:
    """The venue charge for a Pons curve leg whose take is ``take_bps``: the take less what the
    broker already charges, never below the flat :data:`VENUE_EXTRA_BPS` (the measured stand-in it
    replaces may only be raised). ``None`` when the take is unknown: the caller then charges the
    flat stand-in AND tags the leg not honest."""
    if take_bps is None:
        return None
    flat = VENUE_EXTRA_BPS.get(Chain.ROBINHOOD, 0)
    return max(flat, int(take_bps) - broker_fee_bps(Chain.ROBINHOOD))


def pons_exact_venue_bps(take_in_price: int | None, *, take_charged: int | None = None) -> int | None:
    """The venue charge for a Pons curve leg FILLED at our size (``curve_quote``), whose executable already
    pays the curve's take ``take_in_price`` (``feeBps + creatorTaxBps`` taken off by the curve's own
    arithmetic, ``evm_price.PonsCurve``).

    The exact fill changes the IMPACT only: such a leg still pays at least what a curve-MARK leg of the
    same token pays in fees (:func:`pons_curve_venue_bps` of the higher of ``take_in_price`` and
    ``take_charged``, plus the broker's own fee), so the flat stand-in's floor is not lowered by this
    change. What is left after the take in the price is added here, never below 0. For a 100 bps take
    that is 150 (100 in the price + 150 + the broker's 60 = the mark leg's 250 + 60); from a 250 bps take
    up, 0. ``None`` when the take is unknown (the caller then charges the flat stand-in and the leg is not
    honest). 2026-10-23 (hB_)."""
    if take_in_price is None:
        return None
    floor = pons_curve_venue_bps(max(int(take_in_price), int(take_charged or 0)))
    if floor is None:
        return None
    return max(0, floor - int(take_in_price))


def curve_take_required(chain: Chain | str, source: str | None) -> bool:
    """Whether a leg must carry the curve's own take to count: a robinhood CURVE entry."""
    value = chain.value if isinstance(chain, Chain) else str(chain or "")
    return value == Chain.ROBINHOOD.value and str(source or "").startswith("curve:")


# --------------------------------------------------------------------------------------
# token tax per leg (design 2026-10-21 step 3b)
# --------------------------------------------------------------------------------------

#: Chains whose paper legs pay the token's own transfer tax at the call site. bsc only:
#: MEASURED on the box 2026-10-21, sell tax is NONZERO on 332 of 499 bsc dossiers built in 48 h
#: and on 41 of 50 bsc paper positions opened in 7 days, and the broker charges none of it
#: (``paper.py`` has no tax read). robinhood dossiers say 0 bps on 836 of 836 tokens
#: (``viability.PONS_CREATOR_TAX_SELECTOR``), which is the field being absent, so robinhood keeps
#: :data:`VENUE_EXTRA_BPS` instead and the dossier tax is not trusted.
#:
#: eth and base too (design step 6, 2026-10-23): fee-on-transfer "tax tokens" are an Ethereum
#: staple, and a Uniswap quoter never runs the token's transfer, so its fill excludes the tax. GMGN
#: reports ``buy_tax`` / ``sell_tax`` for both chains (trending rows 2026-10-23: 40 of 40 "0"; eth
#: trenches 17 of 20 "0", 3 of 20 "-1"; base trenches 20 of 20 absent), which dyor maps into the
#: dossier. Unknown refuses the paper entry (``paper_tax_unknown``), never charged as 0.
TAXED_CHAINS: frozenset[Chain] = frozenset({Chain.BSC, Chain.ETH, Chain.BASE})

PAPER_TAX_UNKNOWN = "paper_tax_unknown"


def leg_tax_bps(chain: Chain, known_bps: Any, *, in_price: bool = False) -> tuple[int | None, str | None]:
    """``(bps to charge at the call site, refusal)`` for one leg.

    * ``in_price``: the fill already carries the tax (a Flap ``flap_buy_net_quote`` fill) -> 0,
      so it is never charged twice.
    * a taxed chain with the tax UNKNOWN -> ``(None, "paper_tax_unknown")``. Never charged as 0:
      MEASURED, tax is known on 473 of 499 scanned bsc dossiers (94.8%), so the refusal costs ~5%
      of the scanned population, not a hidden veto.
    * every other chain -> 0.
    """
    if chain not in TAXED_CHAINS or in_price:
        return 0, None
    if known_bps is None:
        return None, PAPER_TAX_UNKNOWN
    try:
        value = int(Decimal(str(known_bps)))
    except (InvalidOperation, ValueError, TypeError):
        return None, PAPER_TAX_UNKNOWN
    if value < 0:
        return None, PAPER_TAX_UNKNOWN
    return value, None


# --------------------------------------------------------------------------------------
# the honesty tag (design step 3: dossier/DexScreener-priced paper stays running, TAGGED)
# --------------------------------------------------------------------------------------
#
# One ``kv`` row per paper position on a :data:`BASIS_CHAINS` chain, keyed by position, written by
# the engine at the entry and by protection at every exit; ``kaiba.learning.paper_report`` reads it,
# counts a row toward a pass line only when its ENTRY was pool- or curve-priced (:func:`entry_counts`),
# and reports the counted rows whose exit was not pool-filled (:func:`exit_degraded`) separately,
# withholding a PASS that leans on them. A keyed row rather than an event because the
# report reads it per position over a 12-day window, and a primary-key lookup does not scan the
# events table (the DB read rule). The entry also rides on the ``paper_entry_priced`` event.

#: Chains whose paper rows are honest only when their own tag says so. eth / base since step 6.
BASIS_CHAINS: frozenset[Chain] = frozenset({Chain.BSC, Chain.ROBINHOOD, Chain.ETH, Chain.BASE})
BASIS_KV_PREFIX = "paper_basis:"

#: Entry sources that came from the token's own pool or curve at our size.
HONEST_ENTRY_PREFIXES: tuple[str, ...] = ("pool:", "curve:")
#: Protection marks read off the pool or the curve on chain (``evm_pool_price`` leaves, the Flap and
#: Pons curve readers, and the Pancake router's executable read of the pair). Recorded on every exit
#: leg (``mark_pool``) so the design's 3d share can be read; NOT required for honesty -- see below.
HONEST_MARK_PREFIXES: tuple[str, ...] = ("evm-pool:", "onchain-sample:", "evm-venue:", "pancake-v2:")
#: An exit leg booked at ZERO proceeds because the token itself takes the whole sale: its sell tax
#: (with the router and venue charges) is 100% or more of the quote (:func:`exit_is_total_loss`), or
#: its current dossier says it cannot be sold (``can_sell=False``, the honeypot blocker). Exact, not
#: a bound -- a live sell of that token receives nothing -- so it is an HONEST fill: the row keeps
#: its tag and counts its real -100% (2026-10-23, step 3 follow-up, blocking).
TOTAL_LOSS = "total_loss"
#: A paper EXIT sold on a launch curve at OUR quantity, with our holding put back into the curve the
#: deciding mark read (bsc Flap: ``evm_price.flap_sell_quote_out``), handed to the broker as an
#: executable. ``curve_mark`` is the other curve label: a sale at the curve's MARK, which the broker
#: fills on its constant-product approximation (``broker_at_size``). 2026-10-23 (hA_).
CURVE_QUOTE = "curve_quote"
CURVE_QUOTE_EXIT = CURVE_QUOTE
#: A robinhood Pons curve ENTRY is ``curve_quote`` too since 2026-10-23 (hB_): filled at our size on the
#: curve's own arithmetic after the latency drift (``evm_pool_price._pons_exact_fill``), its provenance
#: recorded on the tag as ``entry.curve_fill`` (:data:`CURVE_FILL_FIELDS`) so the exit can put our holding
#: back (``evm_price.pons_with_holding``).
CURVE_FILL_FIELDS: tuple[str, ...] = ("quote_in", "net_wei", "atoms", "qty")
#: The only ``entry.curve_fill.drift_basis`` (``curve_price.drift_for``) under which a Pons curve entry
#: counts: a flow MEASURED on the token's own tape and charged against us over the decision -> landing
#: delay. ``unavailable:<why>`` -- fewer than ``curve_price.MIN_FLOW_SAMPLES`` trades in the window, or
#: the tape behind -- is a ZERO drift because nothing was measured, which ``curve_price.FlowRate`` itself
#: calls the optimistic case; ``none`` / ``observed:`` / a missing basis were not charged against us either.
#: 2026-10-23 (fS_, review of hB_): MEASURED on the box (read-only), 109 of 175 robinhood snipe
#: observations read ``unavailable`` at launch + 30 s, and the next 14.6 s of their own net buy flow
#: moved the price a mean ~335 bps against a buyer (tail above +1000%); the robinhood ``pons_trade``
#: detect lag was p50 134-537 s in the newest windows, so busy tokens (mean ~603 bps) read it too. Those
#: entries were booked at the decision's curve and tagged honest. Unknown is never zero: such an entry is
#: still FILLED (exact at our size, at the decision's state) and runs, but it does not count -- exactly
#: like a Pons entry whose creator tax was not read (:func:`curve_take_required`).
PONS_DRIFT_CHARGED_PREFIX = "adverse:"
#: 2026-10-23 (fd_): the SAME rule for every pool- or curve-priced ENTRY on a :data:`BASIS_CHAINS` chain,
#: not only a robinhood Pons curve fill. A bsc Flap curve entry (``evm_pool_price._flap_entry``,
#: ``curve_quote`` since 570c5e6) and every pool entry (``v2_formula`` / ``quoter_v2`` / ``v4_quoter``,
#: ``evm_pool_price.entry_fill``, robinhood / bsc / eth / base) were an executable at our size on the
#: DECISION's curve or pool state, with no landing drift at all, and were tagged honest: exactly the
#: optimistic case :data:`PONS_DRIFT_CHARGED_PREFIX` keeps off the scoreboard. A live buy lands seconds
#: later (MEASURED decision -> block median: robinhood 14.6 s, n=19; bsc 10.1 s, n=64,
#: ``evm_pool_price.ENTRY_LATENCY_MS``). Such an entry now carries ``entry.landing``
#: (``evm_pool_price.landing_drift``: the token's own flow over the delay, charged against us before our
#: fill) and counts only when that drift was MEASURED; unmeasured, it is still filled and still runs, but it
#: does not count. MEASURED on the box (read-only, 2026-10-23): 0 of the 10 pool / curve executable entry
#: tags there had a single swap of their token in the 120 s before the decision.
LANDING_DRIFT_CHARGED_PREFIX = PONS_DRIFT_CHARGED_PREFIX


def landing_drift_charged(provenance: Any) -> bool:
    """Whether an entry's landing provenance (``entry.landing``, or a Pons ``entry.curve_fill``) says the
    decision -> landing drift was MEASURED and charged against us (:data:`LANDING_DRIFT_CHARGED_PREFIX`).
    Anything else -- no provenance, no basis, ``unavailable:...``, ``none``, ``observed:...`` -- is ``False``."""
    if not isinstance(provenance, dict):
        return False
    basis = provenance.get("drift_basis")
    return isinstance(basis, str) and basis.startswith(LANDING_DRIFT_CHARGED_PREFIX)


def pons_drift_charged(curve_fill: Any) -> bool:
    """Whether a Pons curve entry's ``curve_fill`` says its landing drift was MEASURED and charged
    against us (:data:`PONS_DRIFT_CHARGED_PREFIX`). Anything else -- no ``curve_fill``, no basis,
    ``unavailable:...`` -- is ``False``."""
    return landing_drift_charged(curve_fill)


def landing_drift_required(chain: Chain | str, source: str | None) -> bool:
    """Whether an ENTRY must carry a measured landing drift to count: any pool- or curve-priced entry
    (:func:`entry_is_honest`'s sources) on a :data:`BASIS_CHAINS` chain -- every one of them is priced at
    the decision's state, and none is honest about the seconds before a live buy lands without it."""
    value = chain.value if isinstance(chain, Chain) else str(chain or "")
    return value in {c.value for c in BASIS_CHAINS} and entry_is_honest(source)


def entry_drift_unmeasured(chain: Chain | str, source: str | None, *, landing: Any = None,
                           curve_fill: Any = None) -> bool:
    """A pool / curve ENTRY (:func:`landing_drift_required`) whose landing drift was not measured and charged
    (:func:`landing_drift_charged` of its ``landing``, or of a Pons ``curve_fill`` written before ``landing``
    existed): it does not count."""
    return landing_drift_required(chain, source) and not (landing_drift_charged(landing)
                                                          or landing_drift_charged(curve_fill))


def pons_exact_fill_unmeasured(chain: Chain | str, source: str | None, fill: str | None, curve_fill: Any) -> bool:
    """A robinhood curve entry FILLED at our size (``curve_quote``) whose landing drift was not measured
    (:func:`pons_drift_charged`): it does not count. The Pons case of :func:`entry_drift_unmeasured`, which
    is what the writer and the reader apply (to every pool / curve entry, fd_)."""
    return curve_take_required(chain, source) and fill == CURVE_QUOTE and not pons_drift_charged(curve_fill)


#: Exit labels that sell on the curve the mark read, so the mark must have been read on it.
CURVE_EXIT_FILLS: frozenset[str] = frozenset({"curve_mark", CURVE_QUOTE_EXIT})
#: How an exit was FILLED: from the pool at our quantity, or on the curve the mark read -- or, exactly,
#: at nothing (:data:`TOTAL_LOSS`). A label is necessary, not sufficient: the leg is honest only when the
#: broker's own record says it filled at our size (:func:`leg_at_size`).
HONEST_EXIT_FILLS: frozenset[str] = frozenset({"v2_formula", "quoter_v2", "v4_quoter", "curve_mark",
                                                CURVE_QUOTE_EXIT, TOTAL_LOSS})
#: An exit leg booked at ZERO proceeds because the pool ANSWERED that it cannot pay for this
#: quantity (a quoter that returns 0 or reverts on a drained range, a v2 pair with no reserves).
#: Not a pool fill, so the row is not ``honest``; but it stays on the scoreboard, at the bound
#: (2026-10-23 step 3 review, blocking #5). See ``watchdog.DefaultExitSubmitter._paper``.
NO_FILL_BOUND = "pool_no_fill_bound"
#: Not a fill: the pool could not be READ this tick, so the exit waits for the next one.
RETRY_LEG = "retry"


def _basis_key(position_id: str) -> str:
    return f"{BASIS_KV_PREFIX}{position_id}"


def read_basis(conn: Any, position_id: str) -> dict[str, Any] | None:
    from kaiba.core.db import fetch_one, jload

    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (_basis_key(position_id),))
    except Exception:  # noqa: BLE001 - no tag reads as "not honest", never as a crash
        return None
    value = jload(row["value"], {}) if row else None
    return value if isinstance(value, dict) else None


def read_bases(conn: Any, position_ids: Any) -> dict[str, dict[str, Any]]:
    """Tags for many positions, by primary key, 500 per query."""
    import json

    ids = [str(p) for p in position_ids]
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 500):
        chunk = [_basis_key(p) for p in ids[i:i + 500]]
        for key, value in conn.execute(
            f"SELECT key, value FROM kv WHERE key IN ({','.join('?' * len(chunk))})", chunk
        ):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                out[str(key)[len(BASIS_KV_PREFIX):]] = parsed
    return out


def _write_basis(conn: Any, position_id: str, value: dict[str, Any]) -> None:
    from kaiba.core.db import jdump, upsert
    from kaiba.core.schemas import now_ms

    upsert(conn, "kv", {"key": _basis_key(position_id), "value": jdump(value), "updated_ms": now_ms()}, ["key"])


def entry_is_honest(source: str | None) -> bool:
    return bool(source) and str(source).startswith(HONEST_ENTRY_PREFIXES)


def mark_is_pool(mark_source: str | None) -> bool:
    return bool(mark_source) and str(mark_source).startswith(HONEST_MARK_PREFIXES)


def exit_is_honest(mark_source: str | None, fill: str | None, *, tax_known: bool = True) -> bool:
    """An exit leg's P&L is honest when its FILL came from the pool at our quantity (or the curve
    the mark read), with the token's tax known on a taxed chain.

    Which mark TRIGGERED the exit is recorded but not required. The trigger decides timing -- and a
    live position would be triggered by the same protection chain -- while the fill decides what
    the trade made. A DexScreener-triggered exit filled from QuoterV2 at our size booked what the
    pool paid; one FILLED at the DexScreener mark did not. A ``curve_mark`` fill is a curve mark by
    construction, so it must also have been decided on the curve (and so must a ``curve_quote``).

    This judges the LABEL only. Whether the broker then filled the leg at our size is
    :func:`leg_at_size`'s question, asked by :func:`record_exit_basis` and by every reader."""
    if fill in CURVE_EXIT_FILLS and not (mark_source or "").startswith("evm-venue:"):
        return False
    return fill in HONEST_EXIT_FILLS and tax_known


# --------------------------------------------------------------------------------------
# the broker's own fill basis decides every leg (2026-10-23, hA_; Vanguard review section 6 item 1)
# --------------------------------------------------------------------------------------
#
# The tag was written from the QUOTE (``source="curve:flap"``, ``fill="curve_mark"``) and never
# compared with what ``PaperBroker`` did with it. ``paper.py`` (pinned) fills an EVM leg one of two
# ways, and records which in ``order_events`` (``paper.fill_basis``): ``router`` +
# ``executable_quote`` when the call site handed it a price AT OUR SIZE (a quoter, the v2 formula,
# the Flap curve quote), else ``dex`` -- its own constant-product approximation against whatever
# liquidity figure came with the mark (``constant_product_approximation``), or the worst allowed
# slippage (``worst_allowed_no_depth``). Its ``curve`` path is pump.fun's (``CurveState``) and never
# fills an EVM token.
#
# MEASURED on the box 2026-10-23 (read-only; all 15 ``paper_basis`` tags, every filled leg joined with
# its order's ``fill_basis``): every ``curve_mark`` leg -- 2 bsc Flap exits and 1 robinhood Pons exit,
# all tagged honest -- was a ``dex`` / ``constant_product_approximation`` fill: the two bsc exits
# charged 2,388 and 7,121 bps of impact against $298 and $34.66 of curve reserve and booked -27.0% and
# -75.0%; selling the same tokens on the Flap curve with our holding put back pays -3.0% / -11.6%
# (``evm_price.flap_sell_quote_out``). Every pool-labelled leg after the tag existed was
# ``router`` / ``executable_quote``. And one robinhood row counted at +62.0%
# (``pos_a58d5b5cd0cc8925c636e83e``) had THREE buys: the first, 65% of its tokens, a dossier-priced
# ``dex`` fill before the tag existed; the later top-up rewrote the tag from scratch (``exits: []``),
# so the row read honest.

#: The broker bases that are fills AT OUR SIZE: its own curve arithmetic, or the executable it was
#: handed. Everything else (``dex``, ``refused``, nothing recorded) is its model of a fill.
BROKER_ROUTER, BROKER_CURVE = "router", "curve"
BROKER_EXECUTABLE = "executable_quote"
#: The ``paper.fill_basis`` fields a tag keeps per leg.
BROKER_FIELDS: tuple[str, ...] = ("basis", "impact_basis", "impact_bps", "over_tolerance")
#: A leg no broker order filled: booked at zero proceeds by the ledger (``accounting.write_off_dust``,
#: ``Watchdog._settle_blind_counted_paper``). Honest only as :data:`TOTAL_LOSS` (an exact zero).
BOOKED_AT_ZERO: dict[str, Any] = {"basis": "booked_at_zero"}
#: Labels that never reached the broker with an executable before this check existed, so a leg tagged
#: without a ``broker`` record (written before 2026-10-23 hA_) was a ``dex`` fill: MEASURED 3 of 3 on
#: the box; by construction in the call sites of 570c5e6 / 4d9ffa3 (no executable for a Pons curve
#: entry or for any curve-decided exit).
LEGACY_APPROXIMATED_FILLS: frozenset[str] = frozenset({"curve_mark"})
#: Sticky, top level: an ENTRY leg of this position was not filled at our size (a top-up after an honest
#: first buy, or the reverse). Like :data:`STICKY_EXIT_DISHONEST`, never cleared.
STICKY_ENTRY_DISHONEST = "entry_dishonest"
#: Sticky, top level: the position had FILLED orders this tag never recorded (a buy before the tag
#: existed, a sell on a path that does not tag). Set by the writer at an entry and by the report's
#: reader (:func:`flag_untagged_legs`); the row does not count.
UNTAGGED_LEGS = "untagged_legs"
ENTRY_LEG_COUNT = "entry_legs"


def broker_leg(fill_basis: Any) -> dict[str, Any]:
    """The part of ``paper.fill_basis(conn, order_id)`` a tag keeps for one leg.

    Always a dict, so a call site that passes it always opts in to the check: nothing recorded ->
    ``{"basis": None}`` -> not at our size (refused, never inferred from the label)."""
    if not isinstance(fill_basis, dict):
        return {"basis": None}
    out = {key: fill_basis.get(key) for key in BROKER_FIELDS if key in fill_basis}
    out.setdefault("basis", None)
    return out


def broker_at_size(broker: Any) -> bool:
    """Whether the broker filled the leg at our size: its curve arithmetic, or an executable quote."""
    if not isinstance(broker, dict):
        return False
    basis = broker.get("basis")
    if basis == BROKER_CURVE:
        return True
    return basis == BROKER_ROUTER and broker.get("impact_basis") == BROKER_EXECUTABLE


def leg_at_size(leg: Any) -> bool:
    """Whether one tagged leg (the entry, or an exit leg) was filled at our size.

    * :data:`TOTAL_LOSS`: an exact zero, whoever booked it (the broker at the zero executable, or the
      ledger's write-off) -- yes.
    * a leg carrying its broker record (``broker``, every leg written since 2026-10-23 hA_):
      :func:`broker_at_size`.
    * an older leg: its label, with :data:`LEGACY_APPROXIMATED_FILLS` answering no."""
    if not isinstance(leg, dict):
        return False
    if leg.get("fill") == TOTAL_LOSS:
        return True
    if "broker" in leg:
        return broker_at_size(leg.get("broker"))
    return leg.get("fill") not in LEGACY_APPROXIMATED_FILLS


def _leg_honest(leg: Any) -> bool:
    """An exit leg's stored verdict AND the broker's fill basis (re-checked, so an older tag that says
    honest about a ``dex`` fill is not believed)."""
    return isinstance(leg, dict) and bool(leg.get("honest")) and leg_at_size(leg)


def entry_leg_honest(chain: Chain, source: str | None, *, transfer_tax: str | None = None,
                     curve_take_bps: int | None = None, fill: str | None = None, broker: Any = None,
                     curve_fill: Any = None, landing: Any = None) -> bool:
    """One ENTRY leg: priced from the pool or curve (:func:`entry_is_honest`), with a robinhood pool
    entry's transfer tax accounted for and a robinhood curve entry's take read -- and FILLED at our size
    by the broker (``broker``: :func:`broker_leg` of its ``fill_basis``; ``None`` judges ``fill`` alone,
    for a caller that has no order, see :func:`leg_at_size`).

    ``landing``: the entry's decision -> landing drift provenance (``evm_pool_price.PaperEntry.landing``), and
    ``curve_fill``: a robinhood ``curve_quote`` entry's provenance (``evm_pool_price.PaperEntry.curve_fill``).
    A pool / curve entry counts only when its landing drift was measured and charged
    (:func:`entry_drift_unmeasured`; fS_ for Pons, fd_ for every pool and curve entry)."""
    honest = entry_is_honest(source)
    if honest and transfer_tax_required(chain, source) and transfer_tax not in TRANSFER_TAX_ACCOUNTED:
        honest = False
    if honest and curve_take_required(chain, source) and curve_take_bps is None:
        honest = False
    if honest and entry_drift_unmeasured(chain, source, landing=landing, curve_fill=curve_fill):
        honest = False
    if honest:
        honest = leg_at_size({"fill": fill, **({"broker": broker} if broker is not None else {})})
    return honest


def prior_filled_orders(conn: Any, position_id: str, order_id: str) -> int | None:
    """FILLED orders on ``position_id`` other than ``order_id`` (``None`` when unreadable)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM position_orders po JOIN orders o ON o.order_id = po.order_id "
            "WHERE po.position_id = ? AND po.order_id != ? AND o.state = 'filled'",
            (str(position_id), str(order_id)),
        ).fetchone()
    except Exception:  # noqa: BLE001 - unknown is "not checked", the tag's own rules still apply
        return None
    return int(row[0]) if row else 0


def accounted_legs(basis: Any) -> int:
    """Broker-filled legs a tag accounts for: its entries (1 on a tag written before the count) and its
    filled exits. A ledger write-off leg (:data:`BOOKED_AT_ZERO`) is counted though no order exists,
    which can only make this larger than the orders -- never flag a row that is whole."""
    if not isinstance(basis, dict):
        return 0
    exits = [e for e in basis.get("exits") or [] if isinstance(e, dict) and e.get("filled")]
    return int(basis.get(ENTRY_LEG_COUNT) or 1) + max(int(basis.get(FILLED_LEG_COUNT) or 0), len(exits))


def filled_order_counts(conn: Any, position_ids: Any) -> dict[str, int]:
    """FILLED orders per position, by primary key, 500 per query (``position_orders`` PK)."""
    ids = [str(p) for p in position_ids]
    out: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for pid, n in conn.execute(
            "SELECT po.position_id, COUNT(*) FROM position_orders po JOIN orders o ON o.order_id = po.order_id "
            f"WHERE po.position_id IN ({','.join('?' * len(chunk))}) AND o.state = 'filled' "
            "GROUP BY po.position_id", chunk,
        ):
            out[str(pid)] = int(n)
    return out


def flag_untagged_legs(bases: dict[str, dict[str, Any]], filled_orders: dict[str, int]) -> int:
    """Mark (in memory) every tag whose position has more FILLED orders than the tag accounts for
    (:data:`UNTAGGED_LEGS`); returns how many. The reader's half of the multi-entry fix: a tag written
    before it was overwritten by each top-up and cannot say what it lost."""
    flagged = 0
    for pid, n in filled_orders.items():
        basis = bases.get(pid)
        if isinstance(basis, dict) and int(n) > accounted_legs(basis):
            basis[UNTAGGED_LEGS] = True
            flagged += 1
    return flagged


#: How a robinhood POOL entry's token transfer tax is accounted for (``entry.transfer_tax`` in the
#: tag). 2026-10-23 (step 3 follow-up): a QuoterV2 / V4Quoter / v2-formula fill carries the pool's
#: fee and its hook's cut but never runs the token's own transfer, and on such a fill neither a
#: token tax (robinhood is not in :data:`TAXED_CHAINS`: its dossiers say 0 on 836 of 836 tokens,
#: the field being absent) nor the 250 bps :data:`VENUE_EXTRA_BPS` stand-in is charged. For a Pons
#: launch that is right -- its fee IS the hook, which V4Quoter simulates. For any other launchpad's
#: token (about 55 of 179 robinhood paper closes in 7 days on the box) a fee-on-transfer token
#: would be booked tax-free: optimistic. Such a fill counts only when its tax was MEASURED.
#:
#: * ``pons_launch`` -- a Pons token: its pool was found from the Pons graduation's LP NFT, it
#:   priced on the Pons curve, or ``tokens.launchpad`` names Pons. Nothing more to charge.
#: * ``measured`` -- :func:`measured_transfer_tax` had a figure, and it was charged on the buy and
#:   recorded for the sell.
#: * ``unmeasured`` -- neither: the trade runs, the tag says not-honest, the scoreboard leaves it
#:   out (:func:`entry_counts`).
TRANSFER_TAX_PONS = "pons_launch"
TRANSFER_TAX_MEASURED = "measured"
TRANSFER_TAX_UNMEASURED = "unmeasured"
TRANSFER_TAX_ACCOUNTED: frozenset[str] = frozenset({TRANSFER_TAX_PONS, TRANSFER_TAX_MEASURED})
TRANSFER_TAX_KV_PREFIX = "transfer_tax:"


def transfer_tax_required(chain: Chain | str, source: str | None) -> bool:
    """Whether an entry needs :data:`TRANSFER_TAX_ACCOUNTED` to count: a robinhood POOL entry
    (``pool:<venue>:<basis>``; every one is a :data:`FEE_IN_PRICE_FILLS` fill). A curve entry pays
    the curve's own take, at least the 250 bps stand-in (:func:`pons_curve_venue_bps`); a bsc pool
    entry pays the dossier tax, which is known there."""
    value = chain.value if isinstance(chain, Chain) else str(chain or "")
    return value == Chain.ROBINHOOD.value and str(source or "").startswith("pool:")


def measured_transfer_tax(conn: Any, chain: Chain, token: str) -> tuple[int, int] | None:
    """``(buy_bps, sell_bps)`` MEASURED for ``token``, or ``None``.

    Read from ``kv transfer_tax:<chain>:<token>`` = ``{"buy_bps", "sell_bps", "method",
    "measured_ms"}``. NOTHING WRITES IT YET: until something measures a token, every non-Pons
    robinhood pool fill is tagged :data:`TRANSFER_TAX_UNMEASURED`. How it can be measured without
    spending anything: one ``eth_simulateV1`` (or an ``eth_call`` with a state override) on the
    robinhood RPC that (1) gives a fresh address native ETH and swaps a small amount through the
    token's pool, comparing the token balance the address ends with against the quoter's
    ``amountOut`` (the buy tax), and (2) sells that balance back, comparing the native received
    against the quoter's answer for the same input (the sell tax). A plain ``transfer`` between
    two fresh addresses in the same simulation gives the wallet-to-wallet tax. The writer records
    the method and time; this reader trusts only integers in ``[0, 10000)``.
    """
    from kaiba.core.db import fetch_one, jload

    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?",
                        (f"{TRANSFER_TAX_KV_PREFIX}{chain.value}:{str(token or '').lower()}",))
    except Exception:  # noqa: BLE001 - unreadable is unmeasured
        return None
    value = jload(row["value"], {}) if row else None
    if not isinstance(value, dict):
        return None
    out: list[int] = []
    for side in ("buy_bps", "sell_bps"):
        raw = value.get(side)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw < 10_000:
            return None
        out.append(raw)
    return out[0], out[1]


def robinhood_pons_launch(conn: Any, token: str, *, found_by: str = "") -> bool:
    """Whether ``token`` is a Pons launch: its pool came from the Pons graduation's LP NFT
    (``evm_pool_price.resolve_robinhood`` ``found_by="pons_lp_nft"``), it priced on the Pons curve
    (``found_by="pons_curve"``), or ``tokens.launchpad`` names Pons. Anything else -- including a
    token we know nothing about -- is not."""
    if found_by in ("pons_lp_nft", "pons_curve"):
        return True
    from kaiba.core.db import fetch_one

    try:
        for address in dict.fromkeys((str(token or ""), str(token or "").lower())):
            row = fetch_one(conn, "SELECT launchpad FROM tokens WHERE chain=? AND address=?",
                            (Chain.ROBINHOOD.value, address))
            if row is not None:
                return "pons" in str(row.get("launchpad") or "").lower()
    except Exception:  # noqa: BLE001 - unreadable is "not known to be Pons"
        return False
    return False


def _curve_fill_ints(value: Any) -> dict[str, int] | None:
    """The :data:`CURVE_FILL_FIELDS` of an ``entry.curve_fill`` as non-negative ints, or ``None``."""
    if not isinstance(value, dict):
        return None
    out: dict[str, int] = {}
    for key in CURVE_FILL_FIELDS:
        raw = value.get(key)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            number = int(str(raw))
        except (TypeError, ValueError):
            return None
        if number < 0:
            return None
        out[key] = number
    return out


def merge_curve_fill(previous: Any, new: Any) -> dict[str, Any] | None:
    """A top-up's ``curve_fill``: both legs' quote, net, atoms and quantity summed when BOTH were filled on
    the curve at our size, else ``None`` -- an earlier leg filled anywhere else leaves the quote our
    holding put into the curve unknown, and the exit then uses the token-side counterfactual alone."""
    a, b = _curve_fill_ints(previous), _curve_fill_ints(new)
    if a is None or b is None:
        return None
    return {**new, **{key: str(a[key] + b[key]) for key in CURVE_FILL_FIELDS}, "legs": int(previous.get("legs") or 1) + 1}


def holding_entry_net(entry: Any, held_qty: int) -> int | None:
    """The quote our paper buys put into the curve, pro rata to the ``held_qty`` still held (a partial
    exit took the rest back out), from the tag's ``entry.curve_fill``; ``None`` when not recorded."""
    fill = _curve_fill_ints((entry or {}).get("curve_fill") if isinstance(entry, dict) else None)
    if fill is None or fill["qty"] <= 0 or int(held_qty or 0) <= 0:
        return None
    return fill["net_wei"] * min(int(held_qty), fill["qty"]) // fill["qty"]


def record_entry_basis(conn: Any, position_id: str, chain: Chain, *, source: str, fill: str | None,
                       buy_tax_bps: int | None, sell_tax_bps: int | None, tax_in_price: bool,
                       note: str = "", transfer_tax: str | None = None,
                       curve_take_bps: int | None = None, venue_bps: int | None = None,
                       gas_bps: int | None = None, broker: Any = None,
                       prior_filled_orders: int | None = None,
                       curve_fill: dict[str, Any] | None = None,
                       landing: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Tag a paper entry. Never raises: a tag that cannot be written leaves the row not-honest.

    A robinhood pool entry (:func:`transfer_tax_required`) is honest only with ``transfer_tax`` in
    :data:`TRANSFER_TAX_ACCOUNTED`; a robinhood curve entry (:func:`curve_take_required`) only with
    the curve's own take read (``curve_take_bps``: ``feeBps() + creatorTaxBps()``). ``venue_bps`` is
    the venue charge the leg actually paid.

    ``broker``: :func:`broker_leg` of this order's ``paper.fill_basis`` -- the broker's own record of
    how it filled the leg, which decides it (:func:`entry_leg_honest`); the engine always passes it.
    ``prior_filled_orders``: the position's other FILLED orders. 2026-10-23 (hA_): a top-up used to
    rewrite the tag from scratch, dropping every exit leg and the sticky flags, and a buy made before
    the tag existed left no trace at all. Now a second entry MERGES into the tag, any entry leg not filled
    at our size sets :data:`STICKY_ENTRY_DISHONEST`, and orders the tag never saw set
    :data:`UNTAGGED_LEGS`.

    ``curve_fill``: a Pons curve entry filled at our size (``evm_pool_price.PaperEntry.curve_fill`` plus the
    broker's ``qty``), kept as ``entry.curve_fill`` for the exit; a top-up merges it
    (:func:`merge_curve_fill`). 2026-10-23 (hB_).

    ``landing``: the entry's decision -> landing drift (``evm_pool_price.PaperEntry.landing``), kept as
    ``entry.landing``; a pool / curve entry counts only when it was measured (:func:`entry_drift_unmeasured`).
    2026-10-23 (fd_)."""
    if chain not in BASIS_CHAINS or not position_id:
        return None
    honest = entry_leg_honest(chain, source, transfer_tax=transfer_tax, curve_take_bps=curve_take_bps,
                              fill=fill, broker=broker, curve_fill=curve_fill, landing=landing)
    entry: dict[str, Any] = {
        "source": source, "fill": fill, "buy_tax_bps": buy_tax_bps, "sell_tax_bps": sell_tax_bps,
        "tax_in_price": bool(tax_in_price), "transfer_tax": transfer_tax, "honest": honest,
        "curve_take_bps": curve_take_bps, "venue_bps": venue_bps,
        "router_fee_bps": ROUTER_FEE_BPS.get(chain, 100),
        "router_fee_assumed": chain in ASSUMED_ROUTER_FEE_CHAINS,
        **({"gas_bps": int(gas_bps)} if gas_bps is not None else {}),
        **({"broker": broker_leg(broker)} if broker is not None else {}),
        **({"curve_fill": dict(curve_fill)} if isinstance(curve_fill, dict) else {}),
        **({"landing": dict(landing)} if isinstance(landing, dict) else {}),
        "note": note[:200],
    }
    previous = read_basis(conn, position_id)
    if previous is not None and "curve_fill" in entry:
        merged = merge_curve_fill((previous.get("entry") or {}).get("curve_fill")
                                  if isinstance(previous.get("entry"), dict) else None, entry["curve_fill"])
        if merged is None:
            entry.pop("curve_fill")
        else:
            entry["curve_fill"] = merged
    value: dict[str, Any] = {"chain": chain.value, "entry": entry, "exits": [], ENTRY_LEG_COUNT: 1,
                             "honest": False}  # nothing is honest before it has an exit
    accounted = 0
    if previous is not None:
        # A top-up: keep everything the tag already knows, and remember that an earlier entry did not
        # count (whatever made it so: its fill basis, its tax, an older sticky flag).
        accounted = accounted_legs(previous)
        value.update({k: v for k, v in previous.items() if k not in ("entry", "honest", ENTRY_LEG_COUNT)})
        value["exits"] = [e for e in previous.get("exits") or [] if isinstance(e, dict)]
        value[ENTRY_LEG_COUNT] = int(previous.get(ENTRY_LEG_COUNT) or 1) + 1
        if not entry_counts(previous):
            value[STICKY_ENTRY_DISHONEST] = True
    if prior_filled_orders is not None and int(prior_filled_orders) > accounted:
        value[UNTAGGED_LEGS] = True
    value["honest"] = _tag_honest(value)
    try:
        _write_basis(conn, position_id, value)
    except Exception:  # noqa: BLE001
        return None
    return value


def _tag_honest(value: dict[str, Any]) -> bool:
    """The tag's own verdict: its entry counts, it has a filled exit, and every filled exit leg is honest
    (re-checked against the broker's record), ever (:data:`STICKY_EXIT_DISHONEST`)."""
    return bool(entry_counts(value) and int(value.get(FILLED_LEG_COUNT) or 0) > 0
                and not value.get(STICKY_EXIT_DISHONEST)
                and all(_leg_honest(e) for e in value.get("exits") or [] if isinstance(e, dict) and e.get("filled")))


#: Exit legs kept in the tag (the newest). The verdict does not depend on what is kept: a filled
#: leg that was not honest sets :data:`STICKY_EXIT_DISHONEST` for good, and the filled-leg count is
#: kept apart from the list.
MAX_EXIT_LEGS = 20
#: 2026-10-23 (step 3 follow-up): ``honest`` used to be recomputed from the kept legs only, so a
#: position whose early leg was FILLED at the mark and that later logged twenty more legs (retries,
#: ladder sells) dropped that leg and read honest again. Once set, never cleared.
STICKY_EXIT_DISHONEST = "exit_dishonest"
FILLED_LEG_COUNT = "filled_legs"
#: Sticky: a FILLED exit leg was converted with the broker's placeholder native price
#: (``paper.DEFAULT_NATIVE_USD``: ETH $4,000 against a real ~$2,390) because no native sample was
#: available (design 6a, 2026-10-23). The exit still happens -- a position must close -- but its
#: native proceeds are fiction, so the row leaves every scoreboard (:func:`counts_on_scoreboard`).
NATIVE_PLACEHOLDER = "native_placeholder"


def record_exit_basis(conn: Any, position_id: str, chain: Chain, *, mark_source: str, fill: str,
                      tax_bps: int | None, filled: bool, note: str = "", venue_bps: int | None = None,
                      cost_known: bool = True, native_placeholder: bool = False,
                      broker: Any = None) -> dict[str, Any] | None:
    """Append one exit leg to the tag. ``honest`` is true only when the entry and EVERY filled exit
    were FILLED from the pool or the curve (:func:`exit_is_honest`), with the tax known on a taxed
    chain -- every filled leg ever recorded, not only the :data:`MAX_EXIT_LEGS` kept.

    ``cost_known=False`` (a Pons curve exit whose take could not be read: the flat stand-in was
    charged instead) makes the leg not honest, like an unknown tax. ``venue_bps`` is the venue charge
    the leg paid, recorded.

    ``broker``: :func:`broker_leg` of the sell order's ``paper.fill_basis``, or :data:`BOOKED_AT_ZERO`
    for a ledger write-off. A filled leg is honest only when the broker filled it at our size
    (:func:`leg_at_size`): 2026-10-23 (hA_), a ``curve_mark`` label on a ``dex`` fill read honest."""
    if chain not in BASIS_CHAINS or not position_id:
        return None
    value = read_basis(conn, position_id) or {"chain": chain.value, "entry": None, "exits": []}
    tax_known = (tax_bps is not None or chain not in TAXED_CHAINS) and bool(cost_known)
    leg = {"mark_source": mark_source, "mark_pool": mark_is_pool(mark_source), "fill": fill, "tax_bps": tax_bps,
           "venue_bps": venue_bps, "filled": bool(filled),
           "honest": exit_is_honest(mark_source, fill, tax_known=tax_known), "note": note[:200]}
    if broker is not None:
        leg["broker"] = broker_leg(broker)
    if leg["filled"] and not leg_at_size(leg):
        leg["honest"] = False
    if native_placeholder:
        leg["native_placeholder"] = True
        if leg["filled"]:
            value[NATIVE_PLACEHOLDER] = True
    exits = [e for e in (value.get("exits") or []) if isinstance(e, dict)]
    if any(e.get("filled") and not _leg_honest(e) for e in exits):
        value[STICKY_EXIT_DISHONEST] = True  # also covers a tag written before the flag (or the broker check)
    if leg["filled"]:
        value[FILLED_LEG_COUNT] = max(int(value.get(FILLED_LEG_COUNT) or 0),
                                      sum(1 for e in exits if e.get("filled"))) + 1
        if not _leg_honest(leg):
            value[STICKY_EXIT_DISHONEST] = True
    exits.append(leg)
    value["exits"] = exits[-MAX_EXIT_LEGS:]
    value["honest"] = _tag_honest(value)
    try:
        _write_basis(conn, position_id, value)
    except Exception:  # noqa: BLE001
        return None
    return value


def is_honest(basis: dict[str, Any] | None) -> bool:
    """Entry AND every filled exit leg priced from the pool or the curve at our size -- re-checked
    against the broker's record (a tag written before that check may say honest about a ``dex`` fill)."""
    return bool(basis and basis.get("honest")) and entry_counts(basis) and not exit_degraded(basis)


def entry_counts(basis: dict[str, Any] | None) -> bool:
    """Whether the scoreboard counts this row at all: its ENTRY was filled from the pool or the
    curve at our size -- and, for a robinhood pool entry, with the token's transfer tax accounted
    for (:data:`TRANSFER_TAX_ACCOUNTED`, checked here as well as at the writer, so a tag written by
    any older path cannot count a non-Pons token tax-free).

    The decision is made at the entry, before the outcome exists, so leaving the others out does
    not select on how the trade went. The EXIT is not part of this test, on purpose. 2026-10-23
    (step 3 review, blocking #5): when "honest" also required every exit leg to be pool-filled, a
    position whose exit the pool could not fill -- a drained v3 range (QuoterV2 returns 0 or
    reverts), V4Quoter out of liquidity, a robinhood pacer refusal during a burst of stops -- was
    dropped from the scoreboard, and those are the worst losers. A probe of 40 honest +8% trades
    plus 12 such exits booked at -90% printed PASS +8.0% (n=40) while all 52 together were -14.6%.
    So a row with an honest entry stays; its exit legs are booked at the pool fill, at zero
    proceeds when the pool answered it cannot pay (:data:`NO_FILL_BOUND`), or at the deciding
    mark when the pool could not be read at all, and :func:`exit_degraded` says which rows those
    are so the report can count them and withhold a PASS that leans on them.

    2026-10-23 (hA_): the entry must also have been FILLED at our size by the broker
    (:func:`leg_at_size`: a Pons curve entry the broker filled on its constant-product approximation
    does not count), and every entry leg must have counted (:data:`STICKY_ENTRY_DISHONEST`), with no
    filled order the tag never recorded (:data:`UNTAGGED_LEGS`). All three are decided at the entries,
    before the outcome exists.

    2026-10-23 (fd_): and a pool / curve entry must carry a MEASURED landing drift (``entry.landing``, or a
    Pons ``entry.curve_fill``), whatever its stored verdict says -- a tag written before the writer checked it
    (every one on the box) was filled at the decision's state (:func:`entry_drift_unmeasured`)."""
    entry = basis.get("entry") if isinstance(basis, dict) else None
    if not (isinstance(entry, dict) and entry.get("honest")):
        return False
    if (basis or {}).get(STICKY_ENTRY_DISHONEST) or (basis or {}).get(UNTAGGED_LEGS) or not leg_at_size(entry):
        return False
    chain = (basis or {}).get("chain") or ""
    if entry_drift_unmeasured(chain, entry.get("source"), landing=entry.get("landing"),
                              curve_fill=entry.get("curve_fill")):
        return False
    if transfer_tax_required(chain, entry.get("source")):
        return entry.get("transfer_tax") in TRANSFER_TAX_ACCOUNTED
    if curve_take_required(chain, entry.get("source")):
        # 2026-10-23: a Pons curve entry counts only with the curve's own take read and charged; a tag
        # from before that (the flat 250, under-charging any creator tax above 210 bps) does not.
        if not isinstance(entry.get("curve_take_bps"), int) or isinstance(entry.get("curve_take_bps"), bool):
            return False
        # 2026-10-23 (fS_): nor does one FILLED at our size whose landing drift was not measured -- the
        # ``entry_drift_unmeasured`` re-check above, which since fd_ covers every pool and curve entry.
    return True


def exit_degraded(basis: dict[str, Any] | None) -> bool:
    """A counted row (:func:`entry_counts`) whose exit was NOT all filled from the pool or curve:
    a leg at :data:`NO_FILL_BOUND`, at the mark, with an unknown tax (ever -- the
    :data:`STICKY_EXIT_DISHONEST` flag survives the leg being dropped from the kept list), or no
    filled leg recorded at all (closed by a path that never priced an exit here)."""
    if not entry_counts(basis):
        return False
    basis = basis or {}
    if basis.get(STICKY_EXIT_DISHONEST):
        return True
    legs = [e for e in basis.get("exits") or [] if isinstance(e, dict) and e.get("filled")]
    # 2026-10-23 (hA_): judged against the broker's record too, so an older tag's ``curve_mark`` leg that
    # the broker filled on its approximation is an exit-stage row, whatever the tag said.
    return not legs or not all(_leg_honest(e) for e in legs)


# --------------------------------------------------------------------------------------
# which paper rows a scoreboard may count (one rule, every reader)
# --------------------------------------------------------------------------------------

#: Chains whose paper rows are honest BY DATE (``kaiba.learning.paper_report.PAPER_HONEST_MS``):
#: sol only, whose pool/curve pricing has been on chain since 2026-10-19 04:00 UTC. 2026-10-23
#: (step 3 follow-up): the report used to filter only :data:`BASIS_CHAINS`, so an eth or base row
#: (the manual lane) -- never tagged, never pool-priced -- counted by its date alone. Default-deny:
#: every other chain counts only with its own tag (:func:`entry_counts`), whatever it is.
HONEST_BY_DATE_CHAINS: frozenset[Chain] = frozenset({Chain.SOL})


def counts_on_scoreboard(chain: Chain | str, basis: dict[str, Any] | None) -> bool:
    """The scoreboard's per-chain rule for one paper row (the date cut is the report's): a sol row
    counts; any other chain's only when its tag says its entry was priced from the pool or curve."""
    value = chain.value if isinstance(chain, Chain) else str(chain or "")
    if value in {c.value for c in HONEST_BY_DATE_CHAINS}:
        return True
    if isinstance(basis, dict) and basis.get(NATIVE_PLACEHOLDER):
        return False  # its native proceeds were converted at a placeholder price (design 6a)
    return entry_counts(basis)
