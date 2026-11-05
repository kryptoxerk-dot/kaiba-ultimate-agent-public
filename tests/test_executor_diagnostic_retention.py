"""Persist a provider's failure diagnosis, not only its CLI confirmation banner."""
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor


def test_failed_order_keeps_venue_error_tail_in_bounded_history(tmp_db):
    order = executor.build_order(
        decision_id=None, chain=Chain.ROBINHOOD,
        token="0xc777f874a7350cfa2bf123ed09253e225dc254d9",
        side=Side.SELL, lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE,
        amount_in=100, min_out=1, slippage_bps=2500,
    )
    reason = ("gmgn-cli: venue answered HTTP 400 on /v1/trade/swap, "
              "so no order was created: " + "confirmation banner " * 60 +
              "POST /v1/trade/swap failed: HTTP 400 code=400 "
              "error=40003702 message=GEvmInsufficientSlippage")
    executor._transition(order, OrderState.FAILED, tmp_db, reason)
    row = tmp_db.execute("SELECT error FROM orders WHERE order_id=?", (order.order_id,)).fetchone()
    history = tmp_db.execute("SELECT detail FROM order_events WHERE order_id=?", (order.order_id,)).fetchone()
    for detail in (row[0], history[0]):
        assert "GEvmInsufficientSlippage" in detail
        assert "venue answered HTTP 400" in detail
        assert len(detail) <= 500


def test_short_error_is_preserved_verbatim(tmp_db):
    order = executor.build_order(
        decision_id=None, chain=Chain.SOL, token="fixture",
        side=Side.SELL, lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE,
        amount_in=100, min_out=1, slippage_bps=100,
    )
    reason = "gmgn: minimum interval (retry in 0.2s)"
    executor._transition(order, OrderState.FAILED, tmp_db, reason)
    assert tmp_db.execute("SELECT error FROM orders WHERE order_id=?", (order.order_id,)).fetchone()[0] == reason
