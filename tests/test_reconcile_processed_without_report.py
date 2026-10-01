"""A provider status of ``processed``/``confirmed`` with no fill quantity is not a fill yet.

FOUND 2026-09-21 ON THE AGENT'S SECOND REAL FILL (od10sol583185031bd06360f06e74416ec9dadf3).
Thirty seconds after the send, ``order get`` said ``processed`` and carried no ``report``;
``reconcile`` wrote FILLED with ``filled_out=None`` (order_events: "reconciled: processed"),
``apply_fill`` had no quantity and opened no position, and 399,830,918,258 tokens sat in
the wallet unprotected until an operator booked them by hand from the ``confirmed`` read
three minutes later -- which is the second payload below, verbatim.

The first payload is RECONSTRUCTED: the raw body of that ``processed`` read was not
logged. What is MEASURED is that its status was ``processed`` and that no fill quantity
came out of it. That is all this test needs to reproduce.
"""

from __future__ import annotations

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor

PROVIDER_ORDER_ID = "od10sol583185031bd06360f06e74416ec9dadf3"
TX = "rsyCHWPPa8fs3jdqieZRo622tpvHya1xs4kci2RKnx5hV9cxVSRvsa14HHMdzMZH8RBVY9sv8kd5RMaHXoJcLfwr"

#: Reconstructed (see module docstring): status only, no report.
PROCESSED_NO_REPORT = {"status": "processed", "hash": TX, "order_id": PROVIDER_ORDER_ID}

#: The real ``confirmed`` read, character for character.
CONFIRMED_WITH_REPORT = {
    "status": "confirmed", "hash": TX, "order_id": PROVIDER_ORDER_ID,
    "report": {
        "input_token": "So11111111111111111111111111111111111111112", "input_token_decimals": 9,
        "swap_mode": "ExactIn", "input_amount": "55606590",
        "output_token": "EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq", "output_token_decimals": 6,
        "output_amount": "399830918258",
        "quote_token": "So11111111111111111111111111111111111111112", "quote_decimals": 9,
        "quote_amount": "55606590",
        "base_token": "EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq", "base_decimals": 6,
        "base_amount": "399830918258",
        "price": "0.0000001373857536", "price_usd": "0.000016208771209728",
        "height": 449107365, "order_height": 449107364, "gas_native": "", "gas_usd": "",
    },
}


def _persist_submitted(conn) -> str:
    order = executor.build_order(
        decision_id="dec_2995bf02359df5feb5dcf97b", chain=Chain.SOL,
        token="EfcmyGs6M8auzbHmyW3tki8WbZh2rnbb3bS4eiyUHedq", side=Side.BUY,
        lane=Lane.MIGRATION_FADE, mode=LaneMode.LIVE,
        amount_in=55_606_590, min_out=144_137_319_474, slippage_bps=2500,
    )
    order = order.model_copy(update={"state": OrderState.SUBMITTED, "provider_order_id": PROVIDER_ORDER_ID})
    executor._persist(order, conn)
    return order.order_id


def test_processed_without_a_quantity_is_not_booked_as_a_fill(tmp_db, monkeypatch):
    oid = _persist_submitted(tmp_db)
    monkeypatch.setattr(executor, "query_gmgn_order", lambda order, conn=None: PROCESSED_NO_REPORT)
    from kaiba.execution import accounting
    calls: list = []
    monkeypatch.setattr(accounting, "apply_fill", lambda order, conn, *, filled_in=None: calls.append(order))

    state = executor.reconcile(oid, tmp_db)

    assert state is OrderState.SUBMITTED, f"must stay unresolved, got {state}"
    assert calls == [], "apply_fill must not be handed a fill with no quantity"
    row = tmp_db.execute("SELECT state, filled_out FROM orders WHERE order_id=?", (oid,)).fetchone()
    assert row["state"] == "submitted" and row["filled_out"] is None
    assert oid in {r["order_id"] for r in executor.unresolved_orders(tmp_db)}, "reconcile_all must retry it"


def test_the_next_pass_books_the_fill_once_the_report_arrives(tmp_db, monkeypatch):
    """The whole point of staying unresolved: the second read carries the quantity."""
    oid = _persist_submitted(tmp_db)
    reads = iter([PROCESSED_NO_REPORT, CONFIRMED_WITH_REPORT])
    monkeypatch.setattr(executor, "query_gmgn_order", lambda order, conn=None: next(reads))
    from kaiba.execution import accounting
    seen: dict = {}
    monkeypatch.setattr(accounting, "apply_fill",
                        lambda order, conn, *, filled_in=None: seen.update(out=order.filled_out, inn=filled_in))

    assert executor.reconcile(oid, tmp_db) is OrderState.SUBMITTED
    assert executor.reconcile(oid, tmp_db) is OrderState.FILLED
    assert seen["out"] == 399_830_918_258 and str(seen["inn"]) == "55606590"


def test_a_failed_status_still_resolves_without_a_quantity(tmp_db, monkeypatch):
    """Only a *success* without a quantity is held back. A failure has nothing to quantify."""
    oid = _persist_submitted(tmp_db)
    monkeypatch.setattr(executor, "query_gmgn_order",
                        lambda order, conn=None: {"status": "failed", "order_id": PROVIDER_ORDER_ID})
    assert executor.reconcile(oid, tmp_db) is OrderState.FAILED
