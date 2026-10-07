"""Paper broker: shadow fills that are allowed to fail.

The shadow lane is the evidence a promotion gate reads (PLAN §8). If the paper broker
fills every order at the quoted price, the shadow record is a fiction and every promotion
decision made from it is worthless. So this broker models what actually separates a quote
from a fill:

1. **Fee** — a per-chain bps cut of the notional, taken out of the input amount.
2. **Priority fee / tip** — a fixed native amount per transaction, charged on top. It does
   not scale with size, which is exactly why small orders are disproportionately bad.
3. **Price impact** — exact from the bonding curve where there is one, and a
   constant-product approximation ``impact = size / (liquidity + size)`` against a pool's
   USD depth where there is not, plus a deterministic slippage floor. No randomness: two
   identical inputs must produce identical fills or a replay proves nothing.
4. **Latency** — on the curve path only, the delay between deciding and filling, applied
   as movement of the curve. See :mod:`kaiba.execution.curve_price`.

An order whose modelled slippage exceeds its own ``slippage_bps`` is **rejected** and
marked FAILED. That is the point: 98.6% of pump.fun tokens fall below $1k liquidity, so a
paper broker that fills a 1 SOL order into a $400 pool is lying about the only risk that
matters at this size.

Price bases, and why that is the whole story
--------------------------------------------

This broker priced only from a DexScreener quote until 2026-09-20, and every order it had
ever written failed with ``no_price``. **DexScreener has no pair row for a token still on
its bonding curve**, and pre-graduation pump.fun launches are the entire candidate
stream. Zero positions had opened, zero trades had closed, and the Phase 4 validation
harness had no input.

That is a fact about DexScreener, not about quoting in general: Jupiter routes pump.fun
curves directly, one hop, with real impact. The curve is still the primary pre-graduation
source — a mint seconds old is not yet indexed by any router, and a router costs a
network call per position per tick against a ~1 rps ceiling — but the ordering is a
choice about latency and budget, not a claim that nothing else can price it.
:mod:`kaiba.execution.curve_price` has the full argument.

A fill resolves its price in this order and **records which one it used**:

* ``FillBasis.CURVE`` — the token is on a bonding curve. Exact arithmetic on the
  reserves, by :mod:`kaiba.execution.curve_price`. Ground truth pre-graduation.
* ``FillBasis.ROUTER`` — an executable quote at **our own size** was handed in
  (``executable_price_usd``). A venue saying what it would actually give us beats any
  model of what it might, so it replaces the depth approximation rather than stacking on
  top of it. Derive it from a two-sided round trip, never from a provider's
  ``priceImpactPct`` — see ``curve_price.PRICE_IMPACT_PCT_WARNING`` for the measurement
  behind that rule.
* ``FillBasis.DEX`` — a pool mid price plus this broker's own constant-product depth
  approximation. The original path, unchanged, and the weakest of the three.
* ``FillBasis.REFUSED`` — none of them. Still ``no_price``, still FAILED. **The fix was to
  price more things correctly, never to invent a price so the trade goes through.**

The basis, the impact, the latency cost and the fee of every fill are written to
``order_events.detail`` as JSON and onto the ``ORDER_FILLED`` event, so a later analysis
can separate curve fills from router fills from pool fills without re-deriving anything —
which is how the open question "how far apart do curve arithmetic and executable quotes
actually run?" gets answered from the record rather than from opinion.
:func:`fill_basis` reads it back.

Everything on-chain is integer base units (lamports / wei / token atoms). USD is
``Decimal``. Floats never touch money, per CONTRACT.
"""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import (
    EVM_ZERO,
    NATIVE_DECIMALS,
    SOL_NATIVE_MINT,
    Chain,
    Decision,
    EventKind,
    Lane,
    LaneMode,
    Order,
    OrderState,
    Position,
    Side,
    TradeOutcome,
    digest,
    now_ms,
)
from kaiba.execution.curve_price import (
    CURVE_CREATOR_FEE_BPS,
    CURVE_TOTAL_FEE_BPS,
    DEFAULT_LATENCY_MS,
    PUMPSWAP_TIER1_TOTAL_FEE_BPS,
    CurveFill,
    CurveResolver,
    CurveState,
    DriftMode,
    FillBasis,
    measure_flow,
    quote_buy,
    quote_sell,
    snapshot_resolver,
)

log = logging.getLogger(__name__)

#: Venue fee in bps for a fill this broker could not attribute to a specific venue.
#:
#: The Solana figure was 100 and that was **flattering**, which is the one direction it
#: must not be. Read from the on-chain fee config: pump.fun's bonding curve is a flat 125
#: bps (95 protocol + 30 creator) and PumpSwap's first post-graduation tier is 120 bps.
#: A Solana fill whose venue we cannot name is charged the worse of the two. A fill whose
#: venue we *can* name is charged that venue's own rate instead — see
#: ``PaperBroker.curve_fee_bps`` and ``PaperBroker.pumpswap_fee_bps``. EVM venues are
#: cheaper on fee and dearer on gas, which the tip covers.
#:
#: bsc is 200, not 60 (review 2026-10-05, launch-snipe on Flap): every bsc order goes out
#: through GMGN, whose commission ``viability.ROUTER_BPS_PER_LEG`` charges at 100 bps a leg
#: (UNVERIFIED, 2026-09-21 execution review), on top of the venue's own fee -- Flap's curve
#: 100 bps a side (MEASURED on our 8 on-curve fills, ``evm_price.FLAP_PROTOCOL_FEE_BPS``) or
#: a PancakeSwap pool bounded at 100 (``viability.DEX_FEE_BPS_UPPER``). At 60 a bsc paper
#: twin was charged ~0.6% a leg against ~2% real, so a paper result flattered by ~2.8% a
#: round trip would have been the evidence for arming money. The token's own tax is still
#: not charged here (this broker has no tax read); the bsc snipe lane admits 0-tax tokens
#: only, and its ``snipe_observations`` marks charge the tax.
DEFAULT_FEE_BPS: dict[Chain, int] = {
    Chain.SOL: CURVE_TOTAL_FEE_BPS,
    Chain.ETH: 60,
    Chain.BSC: 200,
    Chain.BASE: 60,
    Chain.ROBINHOOD: 60,
    Chain.ARC: 60,
    Chain.STABLE: 60,
}

#: Priority fee / tip per transaction, native base units. 0.001 SOL is the Helius Sender
#: floor; the EVM number is a plausible L2 priority fee. Fixed, not proportional.
DEFAULT_TIP_NATIVE: dict[Chain, int] = {
    Chain.SOL: 1_000_000,
    Chain.ETH: 200_000_000_000_000,
    Chain.BSC: 100_000_000_000_000,
    Chain.BASE: 20_000_000_000_000,
    Chain.ROBINHOOD: 20_000_000_000_000,
    Chain.ARC: 20_000_000_000_000,
    Chain.STABLE: 20_000_000_000_000,
}

#: Native price in USD, used only to convert an order's native size into the pool's USD
#: depth. A placeholder, not a price feed — callers that care pass their own.
DEFAULT_NATIVE_USD: dict[Chain, Decimal] = {
    Chain.SOL: Decimal("200"),
    Chain.ETH: Decimal("4000"),
    Chain.BSC: Decimal("900"),
    Chain.BASE: Decimal("4000"),
    Chain.ROBINHOOD: Decimal("4000"),
    Chain.ARC: Decimal("4000"),
    Chain.STABLE: Decimal("4000"),
}

#: Floor charged on every fill on top of modelled impact: routing spread, tick rounding and
#: the fact that the top of book is never actually ours. Deterministic.
DEFAULT_SLIPPAGE_FLOOR_BPS = 30

#: Default tolerance. Research says 10-25% on curve snipes; 15% sits inside that.
DEFAULT_SLIPPAGE_BPS = 1500

BPS = Decimal(10_000)


def _token_decimals(chain: Chain, token: str, conn: sqlite3.Connection, override: int | None) -> int:
    if override is not None:
        return int(override)
    row = fetch_one(conn, "SELECT decimals FROM tokens WHERE chain=? AND address=?", (chain.value, token))
    if row and row["decimals"] is not None:
        return int(row["decimals"])
    return 6 if chain is Chain.SOL else 18


def _native_mint(chain: Chain) -> str:
    """Same convention as kaiba.execution.executor, so order rows read alike."""
    return SOL_NATIVE_MINT if chain is Chain.SOL else EVM_ZERO


class PaperBroker:
    """Deterministic shadow execution against a bonding curve, or a quoted pool depth."""

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        fee_bps: dict[Chain, int] | None = None,
        tip_native: dict[Chain, int] | None = None,
        native_usd: dict[Chain, Decimal] | None = None,
        slippage_floor_bps: int = DEFAULT_SLIPPAGE_FLOOR_BPS,
        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
        provider: str = "paper",
        curve_resolver: CurveResolver | None = None,
        curve_fee_bps: int = CURVE_TOTAL_FEE_BPS,
        curve_creator_fee_bps: int = CURVE_CREATOR_FEE_BPS,
        pumpswap_fee_bps: int = PUMPSWAP_TIER1_TOTAL_FEE_BPS,
        latency_ms: int = DEFAULT_LATENCY_MS,
        drift_mode: DriftMode = DriftMode.ADVERSE,
    ) -> None:
        self.conn = conn or get_conn()
        self.fee_bps = {**DEFAULT_FEE_BPS, **(fee_bps or {})}
        self.tip_native = {**DEFAULT_TIP_NATIVE, **(tip_native or {})}
        self.native_usd = {**DEFAULT_NATIVE_USD, **(native_usd or {})}
        self.slippage_floor_bps = int(slippage_floor_bps)
        self.slippage_bps = int(slippage_bps)
        self.provider = provider
        #: How a curve is found for a token. Defaults to the stored ``curve_snapshots``
        #: rows: offline and deterministic, so a replay of the shadow record reproduces
        #: it. Pass ``curve_price.live_resolver(conn)`` to price against pump.fun now.
        self.curve_resolver: CurveResolver = curve_resolver or snapshot_resolver(self.conn)
        self.curve_fee_bps = int(curve_fee_bps)
        self.curve_creator_fee_bps = int(curve_creator_fee_bps)
        self.pumpswap_fee_bps = int(pumpswap_fee_bps)
        self.latency_ms = int(latency_ms)
        self.drift_mode = drift_mode

    # ---------------------------------------------------------------- conversions

    def to_usd(self, chain: Chain, base_units: int) -> Decimal:
        return Decimal(base_units) / (Decimal(10) ** NATIVE_DECIMALS[chain]) * self.native_usd[chain]

    def from_usd(self, chain: Chain, usd: Decimal) -> int:
        return int(usd / self.native_usd[chain] * (Decimal(10) ** NATIVE_DECIMALS[chain]))

    def impact_bps(self, size_usd: Decimal, liquidity_usd: Decimal) -> int:
        """Constant-product approximation, floored to whole bps.

        No depth at all is 100% impact, not 0 — an unpriceable pool is untradeable.
        """
        if liquidity_usd is None or liquidity_usd <= 0:
            return int(BPS)
        if size_usd <= 0:
            return 0
        return int(size_usd * BPS / (liquidity_usd + size_usd))

    # ---------------------------------------------------------------- price basis

    def resolve_curve(
        self, chain: Chain, token: str, ts: int, *, curve: CurveState | None = None
    ) -> tuple[CurveState | None, str]:
        """Find the bonding curve behind a fill, or say why there is not one.

        A token whose ``tokens.migrated_ms`` is set has graduated: its curve is complete
        and the pool quote is the right answer, so we do not go looking for reserves that
        no longer price anything.
        """
        if curve is not None:
            return curve, "supplied"
        try:
            row = fetch_one(
                self.conn,
                "SELECT migrated_ms FROM tokens WHERE chain=? AND address=?",
                (chain.value, token),
            )
        except sqlite3.Error:
            row = None
        if row and row["migrated_ms"]:
            return None, "graduated"
        try:
            return self.curve_resolver(chain, token, ts)
        except Exception as exc:  # noqa: BLE001 - a broken resolver is a missing curve
            log.warning("curve resolver failed for %s: %s", token[:16], exc)
            return None, f"curve_resolver_failed:{type(exc).__name__}"

    def _dex_fee_bps(self, chain: Chain, curve_note: str) -> int:
        """A DEX fill of a token we watched graduate is a PumpSwap tier-1 fill."""
        if chain is Chain.SOL and curve_note in {"graduated", "curve_complete"}:
            return self.pumpswap_fee_bps
        return int(self.fee_bps.get(chain, DEFAULT_FEE_BPS.get(chain, 125)))

    def _dex_venue(self, chain: Chain, curve_note: str) -> str:
        if chain is Chain.SOL and curve_note in {"graduated", "curve_complete"}:
            return "pumpswap-tier1"
        return f"dex:{chain.value}"

    def prior_basis(self, position_id: str) -> str | None:
        """The price basis this position's earlier legs filled on, if any."""
        rows = fetch_all(
            self.conn,
            "SELECT o.order_id FROM orders o JOIN position_orders po ON po.order_id=o.order_id "
            "WHERE po.position_id=? AND o.state=? ORDER BY o.created_ms",
            (position_id, OrderState.FILLED.value),
        )
        for row in rows:
            recorded = fill_basis(self.conn, row["order_id"])
            if recorded and recorded.get("basis") in {FillBasis.CURVE.value, FillBasis.DEX.value}:
                return str(recorded["basis"])
        return None

    def _note_basis_change(
        self, position_id: str, order: Order, basis: FillBasis, payload: dict[str, Any]
    ) -> None:
        """Migration is a first-class event, not a silent fallback.

        A position opened on the bonding curve and closed on the pool has been priced two
        different ways inside one trade, and the entry and the exit are therefore not
        directly comparable. The curve and the pair also disagree *during* migration, so
        the handover is exactly where a paper record would quietly flatter itself. It is
        written onto the fill and shouted on the bus instead.
        """
        previous = self.prior_basis(position_id)
        if previous is None or previous == basis.value:
            return
        payload["basis_changed"] = {"from": previous, "to": basis.value}
        payload["basis_changed_note"] = (
            "this position was opened on one price basis and is being filled on another; "
            "entry and exit prices in this trade are not from the same source"
        )
        emit(
            EventKind.SYSTEM,
            {
                "service": "paper",
                "event": "fill_basis_changed",
                "position_id": position_id,
                "order_id": order.order_id,
                "from": previous,
                "to": basis.value,
                "token": order.token,
                "note": payload["basis_changed_note"],
                "paper": True,
            },
            chain=order.chain,
            subject=order.token,
            level="warn",
            conn=self.conn,
        )

    # ---------------------------------------------------------------- buy

    def buy(
        self,
        decision: Decision,
        *,
        price_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        now_ms: int | None = None,
        decimals: int | None = None,
        slippage_bps: int | None = None,
        curve: CurveState | None = None,
        latency_ms: int | None = None,
        executable_price_usd: Decimal | None = None,
    ) -> Order:
        """Model one entry fill. Returns the order; FAILED means we did not get filled.

        ``price_usd``/``liquidity_usd`` are the *pool* quote and are optional: a token on
        a bonding curve has neither, and supplying them does not override the curve.
        ``curve`` short-circuits resolution, which is what a test or a replay wants.

        ``executable_price_usd`` is a real routed quote for **our size** — a Jupiter
        quote, for instance. When it is supplied the pool path uses it instead of the
        ``size/(depth+size)`` approximation, because a venue saying what it would actually
        give us beats any model of what it might. It is injected rather than imported so
        the wiring order between this module and whatever provides it does not matter.
        """
        ts = int(now_ms if now_ms is not None else _now())
        chain = decision.chain
        price_usd = Decimal(price_usd) if price_usd is not None else None
        liquidity_usd = Decimal(liquidity_usd) if liquidity_usd is not None else None
        amount_in = int(decision.size_base_units or 0)
        cap_bps = int(slippage_bps if slippage_bps is not None else self.slippage_bps)
        dec = _token_decimals(chain, decision.token, self.conn, decimals)
        tip = int(self.tip_native.get(chain, 0))

        order = Order(
            order_id=self._order_id(decision.decision_id, Side.BUY, ts),
            decision_id=decision.decision_id,
            chain=chain,
            token=decision.token,
            side=Side.BUY,
            lane=decision.lane,
            mode=decision.mode,
            input_token=_native_mint(chain),
            output_token=decision.token,
            amount_in=amount_in,
            min_out=0,
            slippage_bps=cap_bps,
            state=OrderState.PLANNED,
            provider=self.provider,
            created_ms=ts,
            updated_ms=ts,
        )
        self._write_order(order, detail="paper buy planned")

        if amount_in <= 0:
            return self._fail(order, "no_size", ts)

        curve_state, curve_note = self.resolve_curve(chain, decision.token, ts, curve=curve)
        if curve_state is not None:
            return self._buy_on_curve(
                order, decision, curve_state, ts=ts, dec=dec, tip=tip, cap_bps=cap_bps,
                latency_ms=latency_ms,
            )

        if price_usd is None or price_usd <= 0 or liquidity_usd is None or liquidity_usd <= 0:
            # Unchanged behaviour and unchanged error string: no curve and no pool is no
            # price. The fix for `no_price` was to price curves correctly, not to relax
            # this branch.
            return self._fail(
                order,
                "no_price",
                ts,
                detail=jdump(
                    {
                        "basis": FillBasis.REFUSED.value,
                        "curve_note": curve_note,
                        "price_usd": str(price_usd) if price_usd is not None else None,
                        "liquidity_usd": str(liquidity_usd) if liquidity_usd is not None else None,
                    }
                ),
            )

        fee_bps = self._dex_fee_bps(chain, curve_note)
        fee = amount_in * fee_bps // 10_000
        spend = amount_in - fee
        if spend <= 0:
            return self._fail(order, "fee_exceeds_size", ts)

        size_usd = self.to_usd(chain, spend)
        executable = Decimal(executable_price_usd) if executable_price_usd is not None else None
        if executable is not None and executable > 0:
            # A routed quote for our size already contains the impact. Re-charging the
            # approximation on top would double-count it; reporting the gap to the mid as
            # the impact is what the quote actually tells us.
            impact = max(0, int((executable - price_usd) / price_usd * BPS))
            impact_basis = "executable_quote"
        else:
            impact = self.impact_bps(size_usd, liquidity_usd)
            impact_basis = "constant_product_approximation"
        total_bps = impact + self.slippage_floor_bps
        order.min_out = int(size_usd * (Decimal(10) ** dec) / (price_usd * (BPS + cap_bps) / BPS))
        self._transition(order, OrderState.SUBMITTING, ts, detail=f"impact={impact}bps total={total_bps}bps")

        if total_bps > cap_bps:
            # A paper broker that always fills is a lie. This is the branch that keeps the
            # shadow record honest about thin pools.
            return self._fail(
                order,
                f"slippage_exceeded impact={impact}bps floor={self.slippage_floor_bps}bps cap={cap_bps}bps",
                ts,
            )

        effective_price = price_usd * (BPS + total_bps) / BPS
        qty = int(size_usd * (Decimal(10) ** dec) / effective_price)
        if qty <= 0:
            return self._fail(order, "fill_rounds_to_zero", ts)

        basis = FillBasis.ROUTER if impact_basis == "executable_quote" else FillBasis.DEX
        basis_payload: dict[str, Any] = {
            "basis": basis.value,
            "venue": self._dex_venue(chain, curve_note),
            "curve_note": curve_note,
            "fee_bps": fee_bps,
            "fee_native": str(fee),
            "impact_bps": impact,
            "impact_basis": impact_basis,
            "slippage_floor_bps": self.slippage_floor_bps,
            "total_slippage_bps": total_bps,
            "quote_price_usd": str(price_usd),
            "executable_price_usd": str(executable) if executable is not None else None,
            "liquidity_usd": str(liquidity_usd),
            "effective_price_usd": str(effective_price),
            "latency_ms": 0,
            "latency_note": "not modelled on the pool path; only the curve path re-prices after a delay",
        }
        existing = self.open_position(chain, decision.token, decision.lane, decision.mode)
        if existing is not None:
            self._note_basis_change(existing.position_id, order, basis, basis_payload)
        order.filled_out = qty
        order.fee_native = fee + tip
        order.tx_hash = f"paper:{order.order_id}"
        self._transition(
            order,
            OrderState.FILLED,
            ts,
            detail=f"qty={qty} eff_price={effective_price} " + jdump(basis_payload),
        )

        position = self._open_or_add(
            decision=decision,
            qty=qty,
            cost_native=amount_in + tip,
            price_usd=effective_price,
            dec=dec,
            ts=ts,
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
            (position.position_id, order.order_id, Side.BUY.value, ts),
        )
        emit(
            EventKind.ORDER_FILLED,
            {
                "order_id": order.order_id,
                "position_id": position.position_id,
                "qty": str(qty),
                "amount_in": str(amount_in),
                "fee_native": str(order.fee_native),
                "impact_bps": impact,
                "total_slippage_bps": total_bps,
                "effective_price_usd": str(effective_price),
                "fill_basis": basis.value,
                "venue": basis_payload["venue"],
                "paper": True,
            },
            chain=chain,
            subject=decision.token,
            conn=self.conn,
        )
        return order

    # ---------------------------------------------------------------- buy, on the curve

    def _buy_on_curve(
        self,
        order: Order,
        decision: Decision,
        state: CurveState,
        *,
        ts: int,
        dec: int,
        tip: int,
        cap_bps: int,
        latency_ms: int | None,
    ) -> Order:
        """Entry priced exactly off the reserves, including our own impact and the delay."""
        chain = decision.chain
        delay = int(latency_ms if latency_ms is not None else self.latency_ms)
        flow = measure_flow(chain, decision.token, self.conn, at_ms=ts)
        fill = quote_buy(
            state,
            order.amount_in,
            sol_usd=self.native_usd[chain],
            decimals=dec,
            fee_bps=self.curve_fee_bps,
            creator_fee_bps=self.curve_creator_fee_bps,
            latency_ms=delay,
            flow=flow,
            drift_mode=self.drift_mode,
        )
        # ``min_out`` is sized off the tolerance against the *undrifted* curve, because a
        # real order's min_out is written before the delay happens, not after it.
        reference = quote_buy(
            state,
            order.amount_in,
            sol_usd=self.native_usd[chain],
            decimals=dec,
            fee_bps=self.curve_fee_bps,
            creator_fee_bps=self.curve_creator_fee_bps,
            latency_ms=0,
            flow=None,
            drift_mode=DriftMode.NONE,
        )
        if reference.ok:
            order.min_out = reference.amount_out * 10_000 // (10_000 + cap_bps)

        charged = fill.impact_bps + fill.latency_bps + self.slippage_floor_bps
        self._transition(
            order,
            OrderState.SUBMITTING,
            ts,
            detail=f"curve impact={fill.impact_bps}bps latency={fill.latency_bps}bps charged={charged}bps",
        )
        if not fill.ok:
            return self._fail(order, fill.reason or "curve_refused", ts, detail=jdump(fill.as_dict()))
        if charged > cap_bps:
            return self._fail(
                order,
                f"slippage_exceeded impact={fill.impact_bps}bps latency={fill.latency_bps}bps "
                f"floor={self.slippage_floor_bps}bps cap={cap_bps}bps",
                ts,
                detail=jdump(fill.as_dict()),
            )

        basis_payload = fill.as_dict()
        existing = self.open_position(chain, decision.token, decision.lane, decision.mode)
        if existing is not None:
            self._note_basis_change(existing.position_id, order, FillBasis.CURVE, basis_payload)

        order.filled_out = fill.amount_out
        order.fee_native = fill.fee_native + tip
        order.tx_hash = f"paper:{order.order_id}"
        self._transition(
            order,
            OrderState.FILLED,
            ts,
            detail=f"qty={fill.amount_out} eff_price={fill.effective_price_usd} " + jdump(basis_payload),
        )

        position = self._open_or_add(
            decision=decision,
            qty=fill.amount_out,
            cost_native=order.amount_in + tip,
            price_usd=fill.effective_price_usd or Decimal(0),
            dec=dec,
            ts=ts,
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
            (position.position_id, order.order_id, Side.BUY.value, ts),
        )
        emit(
            EventKind.ORDER_FILLED,
            {
                "order_id": order.order_id,
                "position_id": position.position_id,
                "qty": str(fill.amount_out),
                "amount_in": str(order.amount_in),
                "fee_native": str(order.fee_native),
                "impact_bps": fill.impact_bps,
                "latency_bps": fill.latency_bps,
                "total_slippage_bps": charged,
                "effective_price_usd": str(fill.effective_price_usd),
                "fill_basis": FillBasis.CURVE.value,
                "venue": fill.venue,
                "paper": True,
            },
            chain=chain,
            subject=decision.token,
            conn=self.conn,
        )
        return order

    # ---------------------------------------------------------------- sell

    def sell(
        self,
        position: Position,
        pct: float | Decimal,
        *,
        price_usd: Decimal | None = None,
        liquidity_usd: Decimal | None = None,
        now_ms: int | None = None,
        decimals: int | None = None,
        slippage_bps: int | None = None,
        exit_reason: str = "manual",
        decision_id: str | None = None,
        curve: CurveState | None = None,
        latency_ms: int | None = None,
        executable_price_usd: Decimal | None = None,
    ) -> Order:
        """Model one exit fill of ``pct`` percent (0-100) of the remaining quantity.

        This is the method the whole validation harness is waiting on: it is what turns a
        watchdog exit decision into a closed ``trades`` row with realised PnL. It prices
        against the bonding curve when there is one and the pool quote when there is not,
        and records which.
        """
        ts = int(now_ms if now_ms is not None else _now())
        chain = position.chain
        price_usd = Decimal(price_usd) if price_usd is not None else None
        liquidity_usd = Decimal(liquidity_usd) if liquidity_usd is not None else None
        cap_bps = int(slippage_bps if slippage_bps is not None else self.slippage_bps)
        dec = _token_decimals(chain, position.token, self.conn, decimals)
        tip = int(self.tip_native.get(chain, 0))

        live = self._load_position(position.position_id) or position
        pct_d = Decimal(str(pct))
        qty_sold = live.qty if pct_d >= 100 else int(Decimal(live.qty) * pct_d / Decimal(100))

        order = Order(
            order_id=self._order_id(live.position_id, Side.SELL, ts),
            decision_id=decision_id,
            chain=chain,
            token=live.token,
            side=Side.SELL,
            lane=live.lane,
            mode=live.mode,
            input_token=live.token,
            output_token=_native_mint(chain),
            amount_in=qty_sold,
            min_out=0,
            slippage_bps=cap_bps,
            state=OrderState.PLANNED,
            provider=self.provider,
            created_ms=ts,
            updated_ms=ts,
        )
        self._write_order(order, detail=f"paper sell {pct_d}% planned")

        if qty_sold <= 0:
            return self._fail(order, "nothing_to_sell", ts)

        curve_state, curve_note = self.resolve_curve(chain, live.token, ts, curve=curve)
        if curve_state is not None:
            return self._sell_on_curve(
                order, live, curve_state, qty_sold=qty_sold, ts=ts, dec=dec, tip=tip,
                cap_bps=cap_bps, latency_ms=latency_ms, exit_reason=exit_reason,
                decision_id=decision_id,
            )

        if price_usd is None or price_usd <= 0 or liquidity_usd is None or liquidity_usd <= 0:
            return self._fail(
                order,
                "no_price",
                ts,
                detail=jdump(
                    {
                        "basis": FillBasis.REFUSED.value,
                        "curve_note": curve_note,
                        "price_usd": str(price_usd) if price_usd is not None else None,
                        "liquidity_usd": str(liquidity_usd) if liquidity_usd is not None else None,
                    }
                ),
            )

        fee_bps = self._dex_fee_bps(chain, curve_note)
        gross_usd = Decimal(qty_sold) / (Decimal(10) ** dec) * price_usd
        executable = Decimal(executable_price_usd) if executable_price_usd is not None else None
        if executable is not None and executable > 0:
            # A routed sell quote already prices our size. On a sell, worse is lower.
            impact = max(0, int((price_usd - executable) / price_usd * BPS))
            impact_basis = "executable_quote"
        else:
            impact = self.impact_bps(gross_usd, liquidity_usd)
            impact_basis = "constant_product_approximation"
        total_bps = impact + self.slippage_floor_bps
        self._transition(order, OrderState.SUBMITTING, ts, detail=f"impact={impact}bps total={total_bps}bps")
        # An exit is never refused on price. See `_exit_over_tolerance`.
        over_tolerance = total_bps > cap_bps
        if over_tolerance:
            self._exit_over_tolerance(order, total_bps, cap_bps, ts)

        effective_price = price_usd * (BPS - total_bps) / BPS
        proceeds_usd = Decimal(qty_sold) / (Decimal(10) ** dec) * effective_price
        gross_native = self.from_usd(chain, proceeds_usd)
        fee = gross_native * fee_bps // 10_000
        net_native = max(0, gross_native - fee - tip)

        order.min_out = int(
            Decimal(qty_sold) / (Decimal(10) ** dec) * price_usd * (BPS - cap_bps) / BPS
            / self.native_usd[chain] * (Decimal(10) ** NATIVE_DECIMALS[chain])
        )
        basis = FillBasis.ROUTER if impact_basis == "executable_quote" else FillBasis.DEX
        basis_payload: dict[str, Any] = {
            "basis": basis.value,
            "side": Side.SELL.value,
            "venue": self._dex_venue(chain, curve_note),
            "curve_note": curve_note,
            "fee_bps": fee_bps,
            "fee_native": str(fee),
            "impact_bps": impact,
            "impact_basis": impact_basis,
            "slippage_floor_bps": self.slippage_floor_bps,
            "total_slippage_bps": total_bps,
            "over_tolerance": over_tolerance,
            "tolerance_bps": cap_bps,
            "quote_price_usd": str(price_usd),
            "executable_price_usd": str(executable) if executable is not None else None,
            "liquidity_usd": str(liquidity_usd),
            "effective_price_usd": str(effective_price),
            "latency_ms": 0,
        }
        self._note_basis_change(live.position_id, order, basis, basis_payload)

        order.filled_out = net_native
        order.fee_native = fee + tip
        order.tx_hash = f"paper:{order.order_id}"
        self._transition(
            order, OrderState.FILLED, ts, detail=f"net_native={net_native} " + jdump(basis_payload)
        )

        return self._settle_sell(
            order,
            live,
            qty_sold=qty_sold,
            net_native=net_native,
            mark_price_usd=price_usd,
            ts=ts,
            total_bps=total_bps,
            exit_reason=exit_reason,
            decision_id=decision_id,
            extra={"impact_bps": impact, "fill_basis": basis.value},
        )

    # ---------------------------------------------------------------- sell, on the curve

    def _sell_on_curve(
        self,
        order: Order,
        live: Position,
        state: CurveState,
        *,
        qty_sold: int,
        ts: int,
        dec: int,
        tip: int,
        cap_bps: int,
        latency_ms: int | None,
        exit_reason: str,
        decision_id: str | None,
    ) -> Order:
        """Exit priced exactly off the reserves. This is what produces a closed trade."""
        chain = live.chain
        delay = int(latency_ms if latency_ms is not None else self.latency_ms)
        flow = measure_flow(chain, live.token, self.conn, at_ms=ts)
        fill = quote_sell(
            state,
            qty_sold,
            sol_usd=self.native_usd[chain],
            decimals=dec,
            fee_bps=self.curve_fee_bps,
            creator_fee_bps=self.curve_creator_fee_bps,
            latency_ms=delay,
            flow=flow,
            drift_mode=self.drift_mode,
        )
        # Both moves are negative on a sell; the cost is their magnitude.
        charged = abs(fill.impact_bps) + abs(fill.latency_bps) + self.slippage_floor_bps
        self._transition(
            order,
            OrderState.SUBMITTING,
            ts,
            detail=f"curve impact={fill.impact_bps}bps latency={fill.latency_bps}bps charged={charged}bps",
        )
        if not fill.ok:
            return self._fail(order, fill.reason or "curve_refused", ts, detail=jdump(fill.as_dict()))
        if charged > cap_bps:
            # Was a refusal, with the reasoning that a position we cannot exit inside our
            # tolerance should show in the shadow record rather than be quietly filled.
            # The intent was right and the consequence was not: the watchdog retries every
            # five seconds, so two positions accumulated 320 refused sells each and were
            # stranded, while the record showed an *open position* rather than the loss we
            # would really have taken. Live, the tolerance is our own setting -- nothing
            # stops us widening it to exit a token whose pool is draining, and with a 55%
            # rug rate that is exactly what an operator does. So the exit fills at the
            # price the pool actually gives and the record carries what it cost.
            self._exit_over_tolerance(order, charged, cap_bps, ts)

        reference = quote_sell(
            state,
            qty_sold,
            sol_usd=self.native_usd[chain],
            decimals=dec,
            fee_bps=self.curve_fee_bps,
            creator_fee_bps=self.curve_creator_fee_bps,
            latency_ms=0,
            flow=None,
            drift_mode=DriftMode.NONE,
        )
        if reference.ok:
            order.min_out = reference.amount_out * (10_000 - cap_bps) // 10_000

        basis_payload = fill.as_dict()
        self._note_basis_change(live.position_id, order, FillBasis.CURVE, basis_payload)

        net_native = max(0, fill.amount_out - tip)
        order.filled_out = net_native
        order.fee_native = fill.fee_native + tip
        order.tx_hash = f"paper:{order.order_id}"
        self._transition(
            order, OrderState.FILLED, ts, detail=f"net_native={net_native} " + jdump(basis_payload)
        )
        return self._settle_sell(
            order,
            live,
            qty_sold=qty_sold,
            net_native=net_native,
            mark_price_usd=fill.decision_price_usd,
            ts=ts,
            total_bps=charged,
            exit_reason=exit_reason,
            decision_id=decision_id,
            extra={
                "impact_bps": fill.impact_bps,
                "latency_bps": fill.latency_bps,
                "fill_basis": FillBasis.CURVE.value,
                "venue": fill.venue,
            },
        )

    # ---------------------------------------------------------------- settle

    def _settle_sell(
        self,
        order: Order,
        live: Position,
        *,
        qty_sold: int,
        net_native: int,
        mark_price_usd: Decimal | None,
        ts: int,
        total_bps: int,
        exit_reason: str,
        decision_id: str | None,
        extra: dict[str, Any],
    ) -> Order:
        """The ledger half of a sell, shared by both price bases.

        Unchanged from the single-basis version: the average-cost accounting, the close
        rule and the event shape are exactly what they were. It is a method rather than
        inline code only so the curve path and the pool path cannot drift apart.
        """
        # The exit price is itself an excursion point; record it before the position shrinks.
        if mark_price_usd is not None:
            self._apply_mark(live, mark_price_usd, ts)
        live.qty -= qty_sold
        live.proceeds_native += net_native
        sold_total = live.qty_total - live.qty
        cost_basis_sold = (
            int(Decimal(live.cost_native) * Decimal(sold_total) / Decimal(live.qty_total))
            if live.qty_total
            else 0
        )
        live.realized_native = live.proceeds_native - cost_basis_sold
        closed = live.qty <= 0
        if closed:
            live.closed_ms = ts
            live.exit_reason = exit_reason
        self._save_position(live)
        self.conn.execute(
            "INSERT OR IGNORE INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
            (live.position_id, order.order_id, Side.SELL.value, ts),
        )
        emit(
            EventKind.ORDER_FILLED,
            {
                "order_id": order.order_id,
                "position_id": live.position_id,
                "qty_sold": str(qty_sold),
                "net_native": str(net_native),
                "total_slippage_bps": total_bps,
                "paper": True,
                **extra,
            },
            chain=live.chain,
            subject=live.token,
            conn=self.conn,
        )
        if closed:
            self._close(live, exit_reason=exit_reason, slippage_bps=total_bps, decision_id=decision_id)
        else:
            emit(
                EventKind.POSITION_UPDATED,
                {"position_id": live.position_id, "qty": str(live.qty), "paper": True},
                chain=live.chain,
                subject=live.token,
                conn=self.conn,
            )
        return order

    # ---------------------------------------------------------------- marking

    def mark_to_market(self, position: Position, price_usd: Decimal, now_ms: int | None = None) -> Position:
        """Update peak / MAE / MFE from a new observed price and log the mark."""
        ts = int(now_ms if now_ms is not None else _now())
        live = self._load_position(position.position_id) or position
        self._apply_mark(live, Decimal(price_usd), ts)
        self._save_position(live)
        return live

    def _apply_mark(self, position: Position, price_usd: Decimal, ts: int) -> None:
        entry = position.entry_price_usd
        if entry is None or entry <= 0 or price_usd <= 0:
            return
        return_pct = float((price_usd - entry) / entry * 100)
        position.peak_price_usd = max(position.peak_price_usd or entry, price_usd)
        position.mfe_pct = max(position.mfe_pct if position.mfe_pct is not None else 0.0, return_pct)
        position.mae_pct = min(position.mae_pct if position.mae_pct is not None else 0.0, return_pct)
        self.conn.execute(
            "INSERT INTO position_marks (position_id, ts_ms, price_usd, return_pct, mae_pct, mfe_pct) "
            "VALUES (?,?,?,?,?,?)",
            (
                position.position_id,
                ts,
                str(price_usd),
                round(return_pct, 6),
                position.mae_pct,
                position.mfe_pct,
            ),
        )

    # ---------------------------------------------------------------- positions

    def _open_or_add(
        self,
        *,
        decision: Decision,
        qty: int,
        cost_native: int,
        price_usd: Decimal,
        dec: int,
        ts: int,
    ) -> Position:
        existing = self.open_position(decision.chain, decision.token, decision.lane, decision.mode)
        if existing is None:
            position = Position(
                position_id="pos_" + digest({"decision_id": decision.decision_id})[:24],
                chain=decision.chain,
                token=decision.token,
                lane=decision.lane,
                mode=decision.mode,
                opened_ms=ts,
                qty=qty,
                qty_total=qty,
                cost_native=cost_native,
                entry_price_usd=price_usd,
                peak_price_usd=price_usd,
                # NOT 0.0: a zero is a measurement claiming the position never moved.
                # `_mark` fills these in from real marks; until then they are unknown.
                mae_pct=None,
                mfe_pct=None,
            )
        else:
            position = existing
            position.qty += qty
            position.qty_total += qty
            position.cost_native += cost_native
            position.peak_price_usd = max(position.peak_price_usd or price_usd, price_usd)
        # Entry price is the fee-inclusive VWAP, recomputed from totals so it stays exact
        # across adds rather than drifting with each average.
        if position.qty_total:
            position.entry_price_usd = self.to_usd(position.chain, position.cost_native) / (
                Decimal(position.qty_total) / (Decimal(10) ** dec)
            )
        self._save_position(position)
        emit(
            EventKind.POSITION_OPENED if existing is None else EventKind.POSITION_UPDATED,
            {
                "position_id": position.position_id,
                "qty": str(position.qty),
                "cost_native": str(position.cost_native),
                "entry_price_usd": str(position.entry_price_usd),
                "paper": True,
            },
            chain=position.chain,
            subject=position.token,
            conn=self.conn,
        )
        return position

    def open_position(self, chain: Chain, token: str, lane: Lane, mode: LaneMode) -> Position | None:
        row = fetch_one(
            self.conn,
            "SELECT * FROM positions WHERE chain=? AND token=? AND lane=? AND mode=? AND closed_ms IS NULL",
            (chain.value, token, lane.value, mode.value),
        )
        return _row_to_position(row) if row else None

    def _load_position(self, position_id: str) -> Position | None:
        row = fetch_one(self.conn, "SELECT * FROM positions WHERE position_id=?", (position_id,))
        return _row_to_position(row) if row else None

    def _save_position(self, p: Position) -> None:
        self.conn.execute(
            "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, "
            "qty_total, cost_native, proceeds_native, realized_native, entry_price_usd, peak_price_usd, "
            "stop_price_usd, tp_done_json, protected, protection_ids_json, mae_pct, mfe_pct, exit_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(position_id) DO UPDATE SET closed_ms=excluded.closed_ms, qty=excluded.qty, "
            "qty_total=excluded.qty_total, cost_native=excluded.cost_native, "
            "proceeds_native=excluded.proceeds_native, realized_native=excluded.realized_native, "
            "entry_price_usd=excluded.entry_price_usd, peak_price_usd=excluded.peak_price_usd, "
            "stop_price_usd=excluded.stop_price_usd, tp_done_json=excluded.tp_done_json, "
            "protected=excluded.protected, protection_ids_json=excluded.protection_ids_json, "
            "mae_pct=excluded.mae_pct, mfe_pct=excluded.mfe_pct, exit_reason=excluded.exit_reason",
            (
                p.position_id,
                p.chain.value,
                p.token,
                p.lane.value,
                p.mode.value,
                p.opened_ms,
                p.closed_ms,
                str(p.qty),
                str(p.qty_total),
                str(p.cost_native),
                str(p.proceeds_native),
                str(p.realized_native),
                str(p.entry_price_usd) if p.entry_price_usd is not None else None,
                str(p.peak_price_usd) if p.peak_price_usd is not None else None,
                str(p.stop_price_usd) if p.stop_price_usd is not None else None,
                jdump(p.tp_done),
                1 if p.protected else 0,
                jdump(p.protection_ids),
                p.mae_pct,
                p.mfe_pct,
                p.exit_reason,
            ),
        )

    # ---------------------------------------------------------------- close

    def _close(
        self,
        position: Position,
        *,
        exit_reason: str,
        slippage_bps: int | None,
        decision_id: str | None,
    ) -> TradeOutcome:
        fees_row = fetch_one(
            self.conn,
            "SELECT COALESCE(SUM(CAST(o.fee_native AS INTEGER)), 0) AS fees FROM orders o "
            "JOIN position_orders po ON po.order_id = o.order_id WHERE po.position_id=?",
            (position.position_id,),
        )
        fees = int(fees_row["fees"]) if fees_row else 0
        pnl = position.proceeds_native - position.cost_native
        pnl_pct = float(Decimal(pnl) / Decimal(position.cost_native) * 100) if position.cost_native else 0.0
        hold_s = int(max(0, (position.closed_ms or _now()) - position.opened_ms) // 1000)
        if decision_id is None:
            link = fetch_one(
                self.conn,
                "SELECT o.decision_id FROM orders o JOIN position_orders po ON po.order_id = o.order_id "
                "WHERE po.position_id=? AND o.decision_id IS NOT NULL ORDER BY o.created_ms LIMIT 1",
                (position.position_id,),
            )
            decision_id = link["decision_id"] if link else None

        outcome = TradeOutcome(
            trade_id="trd_" + digest({"position_id": position.position_id, "closed": position.closed_ms})[:24],
            position_id=position.position_id,
            decision_id=decision_id,
            lane=position.lane,
            mode=position.mode,
            chain=position.chain,
            token=position.token,
            opened_ms=position.opened_ms,
            closed_ms=int(position.closed_ms or _now()),
            hold_s=hold_s,
            cost_native=position.cost_native,
            proceeds_native=position.proceeds_native,
            pnl_native=pnl,
            pnl_pct=round(pnl_pct, 6),
            fees_native=fees,
            slippage_bps=slippage_bps,
            mae_pct=position.mae_pct,
            mfe_pct=position.mfe_pct,
            exit_reason=exit_reason,
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, "
            "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, fees_native, "
            "slippage_bps, mae_pct, mfe_pct, exit_reason, mistakes_json, lesson, params_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                outcome.trade_id,
                outcome.position_id,
                outcome.decision_id,
                outcome.lane.value,
                outcome.mode.value,
                outcome.chain.value,
                outcome.token,
                outcome.opened_ms,
                outcome.closed_ms,
                outcome.hold_s,
                str(outcome.cost_native),
                str(outcome.proceeds_native),
                str(outcome.pnl_native),
                outcome.pnl_pct,
                str(outcome.fees_native),
                outcome.slippage_bps,
                outcome.mae_pct,
                outcome.mfe_pct,
                outcome.exit_reason,
                jdump(outcome.mistakes),
                outcome.lesson,
                outcome.params_version,
            ),
        )
        emit(
            EventKind.POSITION_CLOSED,
            {
                "position_id": position.position_id,
                "trade_id": outcome.trade_id,
                "pnl_native": str(pnl),
                "pnl_pct": outcome.pnl_pct,
                "hold_s": hold_s,
                "exit_reason": exit_reason,
                "paper": True,
            },
            chain=position.chain,
            subject=position.token,
            conn=self.conn,
        )
        if outcome.decision_id:
            self.conn.execute(
                "INSERT INTO decision_outcomes (decision_id, position_id, trade_id, outcome, pnl_native, "
                "pnl_pct, linked_ms) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(decision_id) DO UPDATE SET position_id=excluded.position_id, "
                "trade_id=excluded.trade_id, outcome=excluded.outcome, pnl_native=excluded.pnl_native, "
                "pnl_pct=excluded.pnl_pct, linked_ms=excluded.linked_ms",
                (
                    outcome.decision_id,
                    position.position_id,
                    outcome.trade_id,
                    "closed",
                    str(pnl),
                    outcome.pnl_pct,
                    outcome.closed_ms,
                ),
            )
        return outcome

    # ---------------------------------------------------------------- orders

    def _order_id(self, seed: str, side: Side, ts: int) -> str:
        n = fetch_one(
            self.conn,
            "SELECT COUNT(*) AS n FROM orders WHERE order_id LIKE ?",
            ("ord_%",),
        )
        return "ord_" + digest({"seed": seed, "side": side.value, "ts": ts, "n": n["n"] if n else 0})[:24]

    def _write_order(self, order: Order, *, detail: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
            "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
            "provider_order_id, tx_hash, filled_out, fee_native, created_ms, updated_ms, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.order_id,
                order.decision_id,
                order.chain.value,
                order.token,
                order.side.value,
                order.lane.value,
                order.mode.value,
                order.input_token,
                order.output_token,
                str(order.amount_in),
                str(order.min_out),
                order.slippage_bps,
                order.state.value,
                order.provider,
                order.provider_order_id,
                order.tx_hash,
                str(order.filled_out) if order.filled_out is not None else None,
                str(order.fee_native) if order.fee_native is not None else None,
                order.created_ms,
                order.updated_ms,
                order.error,
            ),
        )
        self.conn.execute(
            "INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
            (order.order_id, order.updated_ms, order.state.value, detail),
        )

    def _transition(self, order: Order, state: OrderState, ts: int, *, detail: str = "") -> Order:
        order.state = state
        order.updated_ms = ts
        self._write_order(order, detail=detail)
        return order

    def _exit_over_tolerance(self, order: Order, charged_bps: int, cap_bps: int, ts: int) -> None:
        """Record that a sell is filling outside its slippage tolerance, and fill it anyway.

        **An exit is never refused on price.** The slippage cap protects an *entry*, where
        declining simply means not opening a position. On the way out it means the
        opposite: refusing to sell leaves us holding the token, and the pool that is too
        thin to sell into today is thinner tomorrow. Of our first 41 closed trades, 14
        exited to the rug monitor -- the pools we most need to leave are exactly the ones
        that will not clear a tolerance.

        This is the same invariant ``RiskGate.check_exit`` already enforces ("a brake that
        also stops you selling is not a brake, it is a way to lose the whole position").
        The paper broker contradicted it, and nobody noticed because the attention went to
        the risk gate: 659 sells were refused, two positions retried 320 times each, and
        the shadow record showed them open when in reality we would have taken the loss.

        Filling is therefore the *more* honest record, not the laxer one -- provided the
        cost is visible, which is what this writes. The order still carries a normal fill;
        ``over_tolerance`` and ``tolerance_bps`` in its basis payload, plus this event, say
        what it cost to get out.
        """
        emit(
            EventKind.ORDER_FAILED,
            {
                "order_id": order.order_id,
                "token": order.token,
                "side": order.side.value,
                "outcome": "filled_over_tolerance",
                "charged_bps": charged_bps,
                "tolerance_bps": cap_bps,
                "reason": (
                    f"exit filled outside tolerance: {charged_bps}bps > {cap_bps}bps. "
                    "Exits are never refused on price; holding is the worse outcome."
                ),
            },
            level="warning",
            chain=order.chain,
            subject=order.token,
        )
        self._transition(
            order,
            OrderState.SUBMITTING,
            ts,
            detail=f"exit_over_tolerance charged={charged_bps}bps cap={cap_bps}bps; filling anyway",
        )

    def _fail(self, order: Order, reason: str, ts: int, *, detail: str | None = None) -> Order:
        """Refuse the fill. ``order.error`` stays the short reason other code matches on;
        ``detail`` carries the provenance of the refusal into ``order_events``."""
        order.error = reason
        self._transition(order, OrderState.FAILED, ts, detail=f"{reason} {detail}" if detail else reason)
        emit(
            EventKind.ORDER_FAILED,
            {
                "order_id": order.order_id,
                "reason": reason,
                "fill_basis": FillBasis.REFUSED.value,
                "detail": detail,
                "paper": True,
            },
            chain=order.chain,
            subject=order.token,
            level="warn",
            conn=self.conn,
        )
        return order


# --------------------------------------------------------------------------------------
# row helpers
# --------------------------------------------------------------------------------------


def _now() -> int:
    return now_ms()


def _row_to_position(row: dict[str, Any]) -> Position:
    return Position(
        position_id=row["position_id"],
        chain=Chain(row["chain"]),
        token=row["token"],
        lane=Lane(row["lane"]),
        mode=LaneMode(row["mode"]),
        opened_ms=row["opened_ms"],
        closed_ms=row["closed_ms"],
        qty=int(row["qty"] or 0),
        qty_total=int(row["qty_total"] or 0),
        cost_native=int(row["cost_native"] or 0),
        proceeds_native=int(row["proceeds_native"] or 0),
        realized_native=int(row["realized_native"] or 0),
        entry_price_usd=Decimal(row["entry_price_usd"]) if row["entry_price_usd"] else None,
        peak_price_usd=Decimal(row["peak_price_usd"]) if row["peak_price_usd"] else None,
        stop_price_usd=Decimal(row["stop_price_usd"]) if row["stop_price_usd"] else None,
        tp_done=jload(row["tp_done_json"], []),
        protected=bool(row["protected"]),
        protection_ids=jload(row["protection_ids_json"], []),
        mae_pct=row["mae_pct"],
        mfe_pct=row["mfe_pct"],
        exit_reason=row["exit_reason"],
    )


def load_position(conn: sqlite3.Connection, position_id: str) -> Position | None:
    row = fetch_one(conn, "SELECT * FROM positions WHERE position_id=?", (position_id,))
    return _row_to_position(row) if row else None


def position_for_order(conn: sqlite3.Connection, order_id: str) -> str | None:
    row = fetch_one(conn, "SELECT position_id FROM position_orders WHERE order_id=?", (order_id,))
    return row["position_id"] if row else None


def open_positions(conn: sqlite3.Connection, mode: LaneMode | None = None) -> list[Position]:
    sql = "SELECT * FROM positions WHERE closed_ms IS NULL"
    params: list[Any] = []
    if mode is not None:
        sql += " AND mode=?"
        params.append(mode.value)
    return [_row_to_position(r) for r in fetch_all(conn, sql, params)]


def fill_basis(conn: sqlite3.Connection, order_id: str) -> dict[str, Any] | None:
    """Read back the price basis of a paper order. ``None`` when nothing recorded one.

    The basis is written as JSON inside the terminal ``order_events.detail`` rather than
    into a new column, because ``orders`` is a core-owned table and a migration is not
    this module's to add. Every fill and every refusal writes one, so an analysis can
    split curve fills from pool fills, and both from what we refused to price at all.
    """
    rows = fetch_all(
        conn,
        "SELECT detail FROM order_events WHERE order_id=? ORDER BY id DESC",
        (order_id,),
    )
    for row in rows:
        detail = row["detail"] or ""
        start = detail.find("{")
        if start < 0:
            continue
        parsed = jload(detail[start:], None)
        if isinstance(parsed, dict) and "basis" in parsed:
            return parsed
    return None


__all__ = [
    "DEFAULT_FEE_BPS",
    "DEFAULT_NATIVE_USD",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_SLIPPAGE_FLOOR_BPS",
    "DEFAULT_TIP_NATIVE",
    "CurveFill",
    "CurveState",
    "DriftMode",
    "FillBasis",
    "PaperBroker",
    "fill_basis",
    "load_position",
    "open_positions",
    "position_for_order",
]
