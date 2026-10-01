"""reconcile must read the fill from GMGN's ``report`` object, where it actually is.

FOUND 2026-09-21 ON THE AGENT'S FIRST REAL FILL. GMGN ``order get`` returned::

    {"status": "confirmed", "hash": "5xKsigFake...", "order_id": "od10sol...",
     "report": {"input_amount": "56250000", "output_amount": "155121046952",
                "output_token_decimals": 6, "price_usd": "0.000042366072923267", ...}}

``reconcile`` read ``data.get("output_amount")`` at the top level, got ``None``, wrote
``filled_out=None``, and handed ``apply_fill`` no quantity -- so no position row was
opened, and the watchdog did not know 155,121 tokens were sitting in the wallet. The
same call path handles the SELL side, so a close would not have been accounted either:
proceeds 0, realised PnL unset, daily stop blind to the loss.

The fixture below is that payload, verbatim.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor

#: The real payload, character for character, from the first live fill.
REAL_GMGN_ORDER_GET = {
    "status": "confirmed",
    "hash": "guuBs2h97ou8ac56NXrukcKs2JUTPnUjGmds9vovHb4fRxWqcM1C1ddLyh5Fa1dc91Hhhsb8A1QRTdjXQGpN5oSg",
    "order_id": "od10sold992a43b57ec671d81dbc4b3a8ec0eb67",
    "report": {
        "input_token": "So11111111111111111111111111111111111111112",
        "input_token_decimals": 9,
        "swap_mode": "ExactIn",
        "input_amount": "56250000",
        "output_token": "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump",
        "output_token_decimals": 6,
        "output_amount": "155121046952",
        "quote_token": "So11111111111111111111111111111111111111112",
        "quote_decimals": 9,
        "quote_amount": "56250000",
        "base_token": "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump",
        "base_decimals": 6,
        "base_amount": "155121046952",
        "price": "0.0000003582148721",
        "price_usd": "0.000042366072923267",
        "height": 449101701,
        "order_height": 449101695,
        "gas_native": "",
        "gas_usd": "",
    },
}


def _persist_submitted(conn, *, side: Side) -> str:
    order = executor.build_order(
        decision_id="dec_test", chain=Chain.SOL,
        token="GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump", side=side,
        lane=Lane.MIGRATION_FADE, mode=LaneMode.LIVE,
        amount_in=56_250_000, min_out=87_863_532_877, slippage_bps=2500,
    )
    order = order.model_copy(update={
        "state": OrderState.SUBMITTED,
        "provider_order_id": "od10sold992a43b57ec671d81dbc4b3a8ec0eb67",
    })
    executor._persist(order, conn)
    return order.order_id


def test_a_real_fill_is_read_from_the_report_object(tmp_db, monkeypatch):
    oid = _persist_submitted(tmp_db, side=Side.BUY)
    monkeypatch.setattr(executor, "query_gmgn_order", lambda order, conn=None: REAL_GMGN_ORDER_GET)
    seen: dict = {}

    def fake_apply_fill(order, conn, *, filled_in=None):
        seen["filled_out"] = order.filled_out
        seen["filled_in"] = filled_in

    from kaiba.execution import accounting
    monkeypatch.setattr(accounting, "apply_fill", fake_apply_fill)

    state = executor.reconcile(oid, tmp_db)

    assert state is OrderState.FILLED
    assert seen["filled_out"] == 155_121_046_952, (
        f"filled_out must come from report.output_amount; got {seen.get('filled_out')!r}"
    )
    assert str(seen["filled_in"]) == "56250000", (
        f"apply_fill must receive report.input_amount; got {seen.get('filled_in')!r}"
    )


def test_a_top_level_shape_still_works_as_a_fallback(tmp_db, monkeypatch):
    """The vendor has changed this shape before; do not break the old one to fix the new."""
    oid = _persist_submitted(tmp_db, side=Side.BUY)
    flat = {"status": "successful", "output_amount": "42", "input_amount": "7", "tx_hash": "0xabc"}
    monkeypatch.setattr(executor, "query_gmgn_order", lambda order, conn=None: flat)
    seen: dict = {}
    from kaiba.execution import accounting
    monkeypatch.setattr(accounting, "apply_fill",
                        lambda order, conn, *, filled_in=None: seen.update(out=order.filled_out, inn=filled_in))
    assert executor.reconcile(oid, tmp_db) is OrderState.FILLED
    assert seen["out"] == 42 and str(seen["inn"]) == "7"


def test_the_sell_side_reads_the_same_report_object(tmp_db, monkeypatch):
    """The close path is the same call; an unaccounted close blinds the daily stop."""
    oid = _persist_submitted(tmp_db, side=Side.SELL)
    monkeypatch.setattr(executor, "query_gmgn_order", lambda order, conn=None: REAL_GMGN_ORDER_GET)
    seen: dict = {}
    from kaiba.execution import accounting
    monkeypatch.setattr(accounting, "apply_fill",
                        lambda order, conn, *, filled_in=None: seen.update(out=order.filled_out))
    executor.reconcile(oid, tmp_db)
    assert seen["out"] == 155_121_046_952
