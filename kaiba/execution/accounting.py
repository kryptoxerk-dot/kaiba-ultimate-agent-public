"""Position ledger: the one writer that turns a **live** fill into a position row.

Until this module existed the live path had a hole that no test could catch, because
nothing on the live path wrote ``positions`` at all — :mod:`kaiba.execution.paper` was the
only writer in the tree. ``engine._plan_live_order`` stops at the order row and
``executor.reconcile`` moves the order state and the journal. The two consequences were
both fatal:

* a live buy that filled left real tokens in the wallet and **no position for the exit
  watchdog to protect**, so real money sat with no stop;
* a live sell that filled never reduced anything, so the row stayed open forever and the
  next take-profit rung would size its trim off a quantity we no longer held.

The rows written here are deliberately the *same shape* as the paper broker's, down to the
weighted-average entry price and the ``trades`` row on close, so the watchdog, the
dashboard and the learning loop never have to ask whether a position was simulated or
real. The only intended difference is the ``paper`` flag on the emitted events.

Four rules, each of which is why a particular line is written the way it is:

1. **Money is integers in base units.** ``qty``, ``cost_native``, ``proceeds_native`` and
   ``realized_native`` are TEXT in SQLite precisely so a wei figure may exceed 2^63. They
   are parsed with ``int()`` and never with ``float()``: a float here corrupts a balance
   quietly and permanently (CONTRACT §1). That is also why the fee total is summed in
   Python rather than with SQLite's ``SUM(CAST(x AS INTEGER))``, which is a 64-bit cast.
2. **Idempotency is the whole job.** ``reconcile`` is *designed* to run repeatedly against
   the same order; that is its purpose. ``position_orders`` is the applied-fill ledger: a
   row there means this order has already moved the position and a replay is a no-op. It
   is the same table everything else reads as the order→position link, so one row does
   both jobs and there is no second bookkeeping table to fall out of step.
3. **A position never goes negative.** A sell larger than the recorded quantity means our
   books and the chain disagree. We clamp to zero, close the position and say so at error
   level. Absorbing it silently would hide a real discrepancy; raising would unwind
   bookkeeping for a fill that has already happened and cannot be undone.
4. **Only a confirmed FILLED order moves the ledger.** ``UNKNOWN`` is the ambiguous-send
   state and means we genuinely do not know whether the venue ever saw the order. It must
   leave the position exactly as it was.

:func:`apply_fill` is the entry point ``executor.reconcile`` calls, and it never raises:
the order has already filled by then, and an exception thrown back into the reconciler
would be a bookkeeping failure pretending to be a trading failure. A ledger write that
fails is an error-level event plus a journal entry, and the next reconcile pass retries
it, because the absence of the ``position_orders`` row is what makes the retry safe.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from kaiba.core import journal
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, tx
from kaiba.core.events import emit
from kaiba.core.schemas import (
    Chain,
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
from kaiba.execution import fills
from kaiba.execution.paper import load_position

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Save the position and claim the order in one transaction, or neither.

    The two writes must not be separable. Claim-then-save loses a fill on a crash between
    them; save-then-claim double-counts one, which is worse. ``kaiba.core.db`` connections
    run in autocommit, so this opens a real transaction — unless the caller already owns
    one, in which case ``BEGIN IMMEDIATE`` would raise and the caller's transaction is
    already the atom we need.
    """
    if conn.in_transaction:
        yield conn
        return
    with tx(conn):
        yield conn


def _int(value: Any, default: int = 0) -> int:
    """Base units, parsed as an integer. Never ``float()`` — see CONTRACT §1."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):  # bool is an int subclass; it is never a money value
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:  # noqa: BLE001 - an unparseable price is simply absent
        return None


def applied(conn: sqlite3.Connection, order_id: str) -> str | None:
    """Position this order has already been applied to, if any. The replay guard."""
    row = fetch_one(conn, "SELECT position_id FROM position_orders WHERE order_id=?", (order_id,))
    return row["position_id"] if row else None


def open_position(
    conn: sqlite3.Connection, chain: Chain, token: str, lane: Lane, mode: LaneMode
) -> Position | None:
    """The one open position for this key, using the same key the paper broker uses."""
    row = fetch_one(
        conn,
        "SELECT position_id FROM positions WHERE chain=? AND token=? AND lane=? AND mode=? "
        "AND closed_ms IS NULL",
        (chain.value, token, lane.value, mode.value),
    )
    return load_position(conn, row["position_id"]) if row else None


def _position_id(order: Order) -> str:
    """Derived from the opening order, not the decision.

    The paper broker seeds its id from the decision id alone. Reusing that seed here would
    let a live fill collide with a shadow position for the same decision and silently merge
    the two through ``ON CONFLICT DO UPDATE``, which is the one accident this table cannot
    survive. Adds to an existing position are found by the ``(chain, token, lane, mode)``
    lookup above, so the seed only ever has to be unique for the position's first fill.
    """
    return "pos_" + digest(
        {"order_id": order.order_id, "decision_id": order.decision_id, "mode": order.mode.value}
    )[:24]


def _entry_from_fill(
    conn: sqlite3.Connection, order: Order, native_leg: int, qty: int
) -> tuple[Decimal | None, str, fills.FillPrice]:
    """The measured entry price, or ``None`` and the reason.

    This replaced the dossier-quote estimate that used to live here. The dossier price was
    the pre-trade quote, not the realised fill: it excluded the slippage actually paid,
    which on a curve snipe is routinely 10%+, so the stop cut from it sat too low and
    exited late. :mod:`kaiba.execution.fills` builds the price from the fill's own two
    legs, converts it with the native/USD sample nearest the fill's instant, and refuses
    to produce a USD figure when no contemporaneous sample exists. The refusal is
    deliberate: a position without an entry price is loud (``position_without_entry_price``
    at error level, and the watchdog reports it blind on every pass), whereas a stop cut
    from a stale or estimated number is silent and wrong on real money.
    """
    fill = fills.derive(order, native_atoms=native_leg, token_atoms=qty, conn=conn)
    price = fill.price_usd if fill.usable_for_stop else None
    return price, fill.basis, fill


def excursion_pct(entry: Decimal | None, peak: Decimal | None) -> float | None:
    """Maximum favourable excursion as a percentage, or None when it is not derivable.

    MEASURED 2026-09-23: 236 of 269 rows carried ``mfe_pct = 0`` and one was NULL, because
    ``open_position`` seeded both excursions at ``0.0`` and nothing ever wrote them again.
    A ``trailing_stop`` fill whose peak was 0.0000283 against an entry of 0.0000115 -- a
    +146% excursion -- was stored as zero, and the study asking whether our losses come
    from entries or from exits read "0.0% for every group" and learned nothing.

    ``None`` and not ``0.0`` when either side is missing or the entry is non-positive: a
    zero here is a MEASUREMENT claiming the position never moved, and an entry price of
    zero is a broken record rather than an infinite gain (CONTRACT rule 2).
    """
    if entry is None or peak is None or entry <= 0:
        return None
    return float((peak / entry - 1) * 100)


def _save_position(conn: sqlite3.Connection, p: Position) -> None:
    """Write the row.

    This is a deliberate twin of ``PaperBroker._save_position``: same columns, same
    conflict clause, same TEXT-encoded integers. It is duplicated rather than shared
    because ``paper.py`` is owned elsewhere and extracting a helper from it was out of
    scope; ``tests/test_accounting.py`` asserts the two writers still produce identical
    rows so the duplication cannot drift unnoticed.
    """
    conn.execute(
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
            # AUDIT-INTEGRATE: an entry-initialized peak is NOT a measured zero.
            # Preserve explicit samples and positive legacy peaks; only the live quote
            # sampler can establish a flat observation. Never infer MAE from a peak.
            p.mae_pct,
            p.mfe_pct if p.mfe_pct is not None else (
                excursion_pct(p.entry_price_usd, p.peak_price_usd)
                if p.entry_price_usd is not None and p.entry_price_usd.is_finite()
                and p.entry_price_usd > 0 and p.peak_price_usd is not None
                and p.peak_price_usd.is_finite() and p.peak_price_usd > p.entry_price_usd
                else None
            ),
            p.exit_reason,
        ),
    )


def _link(conn: sqlite3.Connection, position_id: str, order: Order, ts: int) -> None:
    """Record that this order's fill has been applied. Also the order→position link."""
    conn.execute(
        "INSERT OR IGNORE INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
        (position_id, order.order_id, order.side.value, ts),
    )


def _record_risk_fill(
    conn: sqlite3.Connection,
    order: Order,
    *,
    pnl_native: int = 0,
    is_entry: bool = False,
) -> None:
    """Fold one newly applied non-paper fill into today's risk row.

    ``position_orders`` is the exactly-once claim for the surrounding transaction: callers
    invoke this only after the fill has passed the replay check and link it before the
    transaction can commit. Shadow fills are paper evidence, not live risk.
    """
    if order.mode is LaneMode.SHADOW:
        return
    from kaiba.execution.risk import RiskGate

    RiskGate().record_fill(order.chain, int(pnl_native), conn, is_entry=is_entry)


def _mark_outcome(decision_id: str | None, **fields: Any) -> None:
    """Link the decision to what happened. Imported late; the learning loop is optional."""
    if not decision_id:
        return
    try:
        from kaiba.execution.engine import mark_outcome

        mark_outcome(decision_id, **fields)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never unwind a fill
        log.warning("could not link decision %s to its outcome: %s", decision_id, exc)


def _arm(position_id: str, conn: sqlite3.Connection) -> None:
    """Give the new position a stop. A failure here is loud but never fatal."""
    try:
        from kaiba.execution.protection import arm

        arm(position_id, conn)
    except Exception as exc:  # noqa: BLE001 - protection.arm already swallows its own
        log.warning("protection arm failed for %s: %s", position_id, exc)


def _fees_for_position(conn: sqlite3.Connection, position_id: str) -> int:
    """Sum the fees of every order linked to the position.

    Summed in Python on purpose. ``SUM(CAST(fee_native AS INTEGER))`` is what the paper
    broker does and it is a 64-bit cast, so an EVM gas figure in wei can overflow it.
    """
    return _fee_summary(conn, position_id)[0]


def _fee_summary(conn: sqlite3.Connection, position_id: str) -> tuple[int, bool]:
    """Return ``(known_fee_total, complete)`` without turning unknown into a claim of zero."""
    rows = fetch_all(
        conn,
        "SELECT o.fee_native AS fee FROM orders o JOIN position_orders po ON po.order_id = o.order_id "
        "WHERE po.position_id=?",
        (position_id,),
    )
    total = 0
    complete = True
    for row in rows:
        raw = row["fee"]
        if raw is None or str(raw).strip() == "":
            complete = False
            continue
        try:
            total += int(str(raw).strip())
        except (TypeError, ValueError):
            complete = False
    return total, complete


def _exit_reason_for(conn: sqlite3.Connection, position_id: str, order_id: str) -> str:
    """Why the watchdog sold, if it was the watchdog. Otherwise an honest placeholder."""
    try:
        row = fetch_one(
            conn,
            "SELECT exit_order_id, exit_reason FROM watchdog_state WHERE position_id=?",
            (position_id,),
        )
    except sqlite3.Error:  # the watchdog table is not a hard dependency of the ledger
        return "exit"
    if row and row["exit_order_id"] == order_id and row["exit_reason"]:
        return str(row["exit_reason"])[:120]
    return "exit"


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


def open_or_add(
    order: Order,
    filled_qty: int,
    cost_native: int,
    conn: sqlite3.Connection | None = None,
    *,
    price_usd: Decimal | None = None,
    ts: int | None = None,
    spent_native: int | None = None,
) -> Position | None:
    """A filled BUY opens a position, or adds to the open one for the same key.

    ``filled_qty`` is token atoms actually received — never the amount requested, because a
    partial fill is normal. ``cost_native`` is native base units actually spent, fees
    included, matching the paper broker's fee-inclusive cost basis. ``spent_native`` is the
    fee-excluded native leg when the caller knows it; it is what the fill price is built
    from, and without it the leg is ``cost_native`` less the order's recorded fee.

    ``price_usd`` is the venue's own realised USD price when the caller has one (basis
    ``fill``). Otherwise the entry comes from :func:`_entry_from_fill`: the fill's native
    per-token ratio converted with a contemporaneous native sample (basis ``fill_ratio``),
    or no entry at all. Adding recomputes a quantity-weighted entry price rather than
    overwriting the old one. The native-denominated cost basis
    (``cost_native / qty_total``) is exact by construction.

    Returns the position, or ``None`` when the fill could not be accounted for.
    """
    c = conn or get_conn()
    at = ts if ts is not None else now_ms()

    if order.side is not Side.BUY:
        raise ValueError(f"open_or_add got a {order.side.value} order: {order.order_id}")

    existing_id = applied(c, order.order_id)
    if existing_id:
        log.debug("order %s already applied to %s; replay ignored", order.order_id, existing_id)
        return load_position(c, existing_id)

    filled_qty = _int(filled_qty)
    cost_native = _int(cost_native)
    if filled_qty <= 0:
        _unaccounted(c, order, "buy filled with no reported output quantity")
        return None

    price = _dec(price_usd)
    basis = fills.BASIS_FILL
    fill: fills.FillPrice | None = None
    if price is None or price <= 0:
        native_leg = _int(spent_native) if spent_native is not None else cost_native - _int(order.fee_native)
        price, basis, fill = _entry_from_fill(c, order, native_leg, filled_qty)
    position: Position | None = None
    with _atomic(c):
        # Re-checked under the write lock. The test above is only a fast path; this is the
        # guarantee. ``BEGIN IMMEDIATE`` is what serialises two reconcile passes racing on
        # the same order, and without it both could read "not applied" and both apply.
        existing_id = applied(c, order.order_id)
        if existing_id is None:
            position = _apply_buy(c, order, filled_qty, cost_native, price, basis, fill, at)
    if existing_id is not None or position is None:
        return load_position(c, existing_id) if existing_id else None

    if position.entry_price_usd is None:
        # No entry price means no stop, and the watchdog will run this position blind.
        emit(
            EventKind.SYSTEM,
            {
                "event": "position_without_entry_price",
                "position_id": position.position_id,
                "order_id": order.order_id,
                "entry_price_basis": basis,
                "why": list(fill.notes) if fill is not None else [],
                "price_native_per_token": (
                    format(fill.price_native_per_token, "f")
                    if fill is not None and fill.price_native_per_token is not None
                    else None
                ),
                "impact": "the watchdog cannot arm a stop for this position",
            },
            chain=position.chain,
            subject=position.token,
            level="error",
            conn=c,
        )
    _mark_outcome(
        order.decision_id,
        position_id=position.position_id,
        order_id=order.order_id,
        outcome="open",
        conn=c,
    )
    _arm(position.position_id, c)
    return position


def _apply_buy(
    c: sqlite3.Connection,
    order: Order,
    filled_qty: int,
    cost_native: int,
    price: Decimal | None,
    basis: str,
    fill: fills.FillPrice | None,
    at: int,
) -> Position:
    """The read-modify-write half of :func:`open_or_add`. Runs inside the transaction."""
    position = open_position(c, order.chain, order.token, order.lane, order.mode)
    opened = position is None

    if position is None:
        position = Position(
            position_id=_position_id(order),
            chain=order.chain,
            token=order.token,
            lane=order.lane,
            mode=order.mode,
            opened_ms=at,
            qty=filled_qty,
            qty_total=filled_qty,
            cost_native=cost_native,
            entry_price_usd=price,
            peak_price_usd=price,
            # NOT 0.0. A zero is a measurement claiming the position never moved; these are
            # unknown until there is a peak to derive one from. See `excursion_pct`.
            mae_pct=None,
            mfe_pct=None,
        )
    else:
        prior_total = position.qty_total
        position.qty += filled_qty
        position.qty_total += filled_qty
        position.cost_native += cost_native
        if price is not None:
            if position.entry_price_usd is not None and prior_total > 0 and position.qty_total > 0:
                # Quantity-weighted, so an add cannot erase what the first fill cost.
                position.entry_price_usd = (
                    position.entry_price_usd * Decimal(prior_total) + price * Decimal(filled_qty)
                ) / Decimal(position.qty_total)
            elif position.entry_price_usd is None:
                position.entry_price_usd = price
            position.peak_price_usd = max(position.peak_price_usd or price, price)

    _save_position(c, position)
    _link(c, position.position_id, order, at)
    if fill is not None:
        # The provenance of the entry, in the same transaction as the entry.
        fills.record(c, fill)
    _record_risk_fill(c, order, is_entry=True)
    payload: dict[str, Any] = {
        "position_id": position.position_id,
        "order_id": order.order_id,
        "tx_hash": order.tx_hash,
        "qty": str(position.qty),
        "filled_qty": str(filled_qty),
        "cost_native": str(position.cost_native),
        "entry_price_usd": str(position.entry_price_usd) if position.entry_price_usd else None,
        "fill_price_usd": str(price) if price is not None else None,
        "entry_price_basis": basis,
        "paper": False,
    }
    if fill is not None:
        payload.update(fill.event_fields())
    emit(
        EventKind.POSITION_OPENED if opened else EventKind.POSITION_UPDATED,
        payload,
        chain=position.chain,
        subject=position.token,
        conn=c,
    )
    return position


def write_off_dust(
    position_id: str,
    conn: sqlite3.Connection | None = None,
    *,
    reason: str = "dust_written_off",
) -> Position | None:
    """Close a position whose remainder the wallet no longer holds. No invented sell.

    MEASURED 2026-09-23. ``pos_ca35d86b03667b5e7a08fd24`` bought 11,778,922,406 units for
    0.0778 SOL and sold 11,661,133,181 of them for 0.0504 SOL -- a 99% exit. The remaining
    117,789,225 units are EXACTLY 1.0% of the original, the wallet reported none of them,
    and six attempts to sell that 1% were refused by the venue with HTTP 400. The watchdog
    then retried once a minute for hours: 29 failures in 30 minutes, each one work inside a
    protection tick that was already over its 5 s budget, which kept ``protection_overrun``
    armed and halted entries on EVERY chain.

    The position was economically finished; only a dust line kept it open.

    This does not fabricate a sell. No order is created and no proceeds are recorded --
    ``proceeds_native`` is exactly what came back from the fills that really happened. What
    changes is that the remainder is acknowledged as worth nothing: ``qty`` goes to zero,
    so the realised figure carries the FULL cost basis rather than the basis of the part we
    managed to sell. For the position above that is -0.026576 SOL becoming -0.027354 SOL:
    the 0.000778 SOL difference is the dust, and booking it is the honest end of the trade
    rather than a number that waits forever for a sale that cannot happen.
    """
    c = conn or get_conn()
    position = load_position(c, position_id)
    if position is None or position.closed_ms is not None:
        return position
    position.qty = 0
    position.realized_native = position.proceeds_native - position.cost_native
    position.closed_ms = now_ms()
    position.exit_reason = reason[:120]
    _save_position(c, position)
    journal.append(
        "outcome",
        f"position {position_id} closed by writing off a dust remainder the wallet no "
        f"longer holds; realised {position.realized_native} base units, no sell invented",
        subject=position.token,
        conn=c,
    )
    emit(
        EventKind.POSITION_UPDATED,
        {
            "position_id": position_id,
            "chain": position.chain.value,
            "token": position.token,
            "closed": True,
            "exit_reason": position.exit_reason,
            "realized_native": position.realized_native,
            "note": "dust the wallet does not hold; written off rather than retried",
        },
        chain=position.chain,
        subject=position.token,
        conn=c,
    )
    return position


def reduce(
    order: Order,
    filled_qty: int,
    proceeds_native: int,
    conn: sqlite3.Connection | None = None,
    *,
    exit_reason: str | None = None,
    ts: int | None = None,
) -> Position | None:
    """A filled SELL decrements the position and closes it when nothing is left.

    ``filled_qty`` is token atoms that actually left us; ``proceeds_native`` is native base
    units actually received. ``realized_native`` is computed the same way the paper broker
    computes it — total proceeds minus the cost basis of everything sold so far — so a
    partial exit reads identically whichever broker produced it.
    """
    c = conn or get_conn()
    at = ts if ts is not None else now_ms()

    if order.side is not Side.SELL:
        raise ValueError(f"reduce got a {order.side.value} order: {order.order_id}")

    existing_id = applied(c, order.order_id)
    if existing_id:
        log.debug("order %s already applied to %s; replay ignored", order.order_id, existing_id)
        return load_position(c, existing_id)

    filled_qty = _int(filled_qty)
    proceeds_native = _int(proceeds_native)

    # The realised exit price, recorded for the same reason the entry is: it is what the
    # fill established, and the learning loop should read that rather than a quote.
    fill: fills.FillPrice | None = None
    if filled_qty > 0 and proceeds_native > 0:
        fill = fills.derive(order, native_atoms=proceeds_native, token_atoms=filled_qty, conn=c)

    position: Position | None = None
    oversold = 0
    with _atomic(c):
        # Re-checked under the write lock, for the same reason as the buy side.
        existing_id = applied(c, order.order_id)
        if existing_id is None:
            position, oversold = _apply_sell(
                c, order, filled_qty, proceeds_native, exit_reason, fill, at
            )
    if existing_id is not None or position is None:
        return load_position(c, existing_id) if existing_id else None

    if oversold > 0:
        # Clamped, not absorbed. Our books said we held less than the chain just sold.
        emit(
            EventKind.SYSTEM,
            {
                "event": "position_oversold",
                "position_id": position.position_id,
                "order_id": order.order_id,
                "sold_reported": str(filled_qty),
                "held_on_our_books": str(filled_qty - oversold),
                "excess": str(oversold),
                "impact": "the ledger and the chain disagree; position clamped to zero and closed",
            },
            chain=position.chain,
            subject=position.token,
            level="error",
            conn=c,
        )
        journal.append(
            "correction",
            f"position {position.position_id} oversold by {oversold} atoms on order "
            f"{order.order_id}: the chain filled more than our ledger said we held. "
            "Clamped to zero and closed; reconcile the wallet balance by hand.",
            subject=position.token,
            refs=[order.order_id, order.tx_hash or ""],
            conn=c,
        )
    return position


def _apply_sell(
    c: sqlite3.Connection,
    order: Order,
    filled_qty: int,
    proceeds_native: int,
    exit_reason: str | None,
    fill: fills.FillPrice | None,
    at: int,
) -> tuple[Position | None, int]:
    """The read-modify-write half of :func:`reduce`. Runs inside the transaction.

    Returns the position and how many atoms the chain sold beyond what we had recorded.
    """
    position = open_position(c, order.chain, order.token, order.lane, order.mode)
    if position is None:
        # Selling something we have no record of holding. Never invent a position to hang
        # it on: say so once and leave the discrepancy visible.
        emit(
            EventKind.SYSTEM,
            {
                "event": "sell_without_position",
                "order_id": order.order_id,
                "token": order.token,
                "lane": order.lane.value,
                "mode": order.mode.value,
                "filled_qty": str(filled_qty),
                "impact": "a live sell filled against no open position; the ledger and the chain disagree",
            },
            chain=order.chain,
            subject=order.token,
            level="error",
            dedupe_key=f"accounting:sell_without_position:{order.order_id}",
            conn=c,
        )
        return None, 0

    if filled_qty <= 0:
        _unaccounted(c, order, "sell filled with no reported quantity")
        return None, 0

    prior_realized = position.realized_native
    oversold = filled_qty - position.qty
    sold = min(filled_qty, position.qty)
    position.qty -= sold
    position.proceeds_native += proceeds_native
    sold_total = position.qty_total - position.qty
    cost_basis_sold = (
        int(Decimal(position.cost_native) * Decimal(sold_total) / Decimal(position.qty_total))
        if position.qty_total
        else 0
    )
    position.realized_native = position.proceeds_native - cost_basis_sold

    closed = position.qty <= 0
    if closed:
        position.closed_ms = at
        position.exit_reason = (
            exit_reason or _exit_reason_for(c, position.position_id, order.order_id)
        )[:120]

    _save_position(c, position)
    _link(c, position.position_id, order, at)
    if fill is not None:
        fills.record(c, fill)
    _record_risk_fill(
        c,
        order,
        pnl_native=position.realized_native - prior_realized,
    )
    emit(
        EventKind.POSITION_UPDATED,
        {
            "position_id": position.position_id,
            "order_id": order.order_id,
            "tx_hash": order.tx_hash,
            "qty_sold": str(sold),
            "qty": str(position.qty),
            "proceeds_native": str(position.proceeds_native),
            "realized_native": str(position.realized_native),
            "exit_price_usd": (
                format(fill.price_usd, "f") if fill is not None and fill.price_usd is not None else None
            ),
            "exit_price_basis": fill.basis if fill is not None else fills.BASIS_UNAVAILABLE,
            "exit_price_native_per_token": (
                format(fill.price_native_per_token, "f")
                if fill is not None and fill.price_native_per_token is not None
                else None
            ),
            "paper": False,
        },
        chain=position.chain,
        subject=position.token,
        conn=c,
    )
    if closed:
        # Inside the transaction on purpose: a closed position with no ``trades`` row is
        # invisible to the learning loop, and the two must not be able to disagree.
        _close_trade(c, position, order, slippage_bps=None)
    return position, max(0, oversold)


def _close_trade(
    conn: sqlite3.Connection, position: Position, order: Order, *, slippage_bps: int | None
) -> TradeOutcome:
    """Write the ``trades`` row for a closed live position.

    A position that closes without a trade row is invisible to the learning loop, which
    reads ``trades`` and not ``positions``. The paper broker writes one on every close;
    the live path has to as well or every promotion comparison is shadow-only.
    """
    fees, fees_complete = _fee_summary(conn, position.position_id)
    pnl = position.proceeds_native - position.cost_native
    pnl_pct = float(Decimal(pnl) / Decimal(position.cost_native) * 100) if position.cost_native else 0.0
    closed_ms = int(position.closed_ms or now_ms())
    hold_s = int(max(0, closed_ms - position.opened_ms) // 1000)

    decision_id = order.decision_id
    if decision_id is None:
        link = fetch_one(
            conn,
            "SELECT o.decision_id FROM orders o JOIN position_orders po ON po.order_id = o.order_id "
            "WHERE po.position_id=? AND o.decision_id IS NOT NULL ORDER BY o.created_ms LIMIT 1",
            (position.position_id,),
        )
        decision_id = link["decision_id"] if link else None

    outcome = TradeOutcome(
        trade_id="trd_" + digest({"position_id": position.position_id, "closed": closed_ms})[:24],
        position_id=position.position_id,
        decision_id=decision_id,
        lane=position.lane,
        mode=position.mode,
        chain=position.chain,
        token=position.token,
        opened_ms=position.opened_ms,
        closed_ms=closed_ms,
        hold_s=hold_s,
        cost_native=position.cost_native,
        proceeds_native=position.proceeds_native,
        pnl_native=pnl,
        pnl_pct=round(pnl_pct, 6),
        fees_native=fees,
        slippage_bps=slippage_bps,
        mae_pct=position.mae_pct,
        mfe_pct=position.mfe_pct,
        exit_reason=position.exit_reason,
    )
    conn.execute(
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
            "exit_reason": position.exit_reason,
            "fees_native": str(fees),
            "fees_complete": fees_complete,
            "paper": False,
        },
        chain=position.chain,
        subject=position.token,
        conn=conn,
    )
    _mark_outcome(
        outcome.decision_id,
        position_id=position.position_id,
        trade_id=outcome.trade_id,
        outcome="closed",
        pnl_native=pnl,
        pnl_pct=outcome.pnl_pct,
        conn=conn,
    )
    return outcome


def _unaccounted(conn: sqlite3.Connection, order: Order, why: str) -> None:
    """A fill we cannot size. Never guess a quantity; make it impossible to miss."""
    emit(
        EventKind.SYSTEM,
        {
            "event": "unaccounted_fill",
            "order_id": order.order_id,
            "side": order.side.value,
            "tx_hash": order.tx_hash,
            "reason": why,
            "impact": "a live order filled and the position ledger could not record it",
        },
        chain=order.chain,
        subject=order.token,
        level="error",
        dedupe_key=f"accounting:unaccounted:{order.order_id}",
        conn=conn,
    )
    journal.append(
        "correction",
        f"order {order.order_id} filled but could not be applied to the ledger: {why}. "
        "Check the wallet balance against `positions` by hand.",
        subject=order.token,
        refs=[order.order_id, order.tx_hash or ""],
        conn=conn,
    )


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------


def apply_fill(
    order: Order,
    conn: sqlite3.Connection | None = None,
    *,
    filled_in: Any = None,
    price_usd: Decimal | None = None,
    ts: int | None = None,
) -> Position | None:
    """Move the ledger for one **confirmed** fill. Never raises.

    ``filled_in`` is the input amount the provider says was actually consumed, when it
    reports one. On a buy that is the native actually spent; on a sell it is the token
    quantity that actually left us, which cannot be read off ``filled_out`` because
    ``filled_out`` on a sell is the native received. Without a provider figure we fall
    back to ``amount_in``, which is what we *asked* for — an approximation that is wrong
    exactly when an order partially fills, and the reason this argument exists.

    Anything other than :attr:`OrderState.FILLED` is refused, ``UNKNOWN`` above all: the
    ambiguous-send rule exists because we do not know whether that order reached a venue,
    and moving a position on a maybe is how a ledger starts lying.
    """
    c = conn or get_conn()
    try:
        if order.state is not OrderState.FILLED:
            log.debug("apply_fill ignoring %s in state %s", order.order_id, order.state.value)
            return None
        if order.side is Side.BUY:
            qty = _int(order.filled_out)
            spent = _int(filled_in) or _int(order.amount_in)
            return open_or_add(
                order, qty, spent + _int(order.fee_native), c, price_usd=price_usd, ts=ts, spent_native=spent
            )
        qty = _int(filled_in) or _int(order.amount_in)
        proceeds = _int(order.filled_out)
        return reduce(order, qty, proceeds, c, ts=ts)
    except Exception as exc:  # noqa: BLE001 - the fill already happened; never unwind it
        log.exception("ledger could not apply fill for %s", order.order_id)
        try:
            emit(
                EventKind.SYSTEM,
                {
                    "event": "ledger_write_failed",
                    "order_id": order.order_id,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "impact": "the order is FILLED but the position ledger did not move; "
                    "the next reconcile pass retries",
                },
                chain=order.chain,
                subject=order.token,
                level="error",
                conn=c,
            )
        except Exception:  # noqa: BLE001 - nothing left to do if the bus is down too
            log.exception("could not even report the ledger failure for %s", order.order_id)
        return None


__all__ = [
    "apply_fill",
    "applied",
    "open_or_add",
    "open_position",
    "reduce",
]
