"""Telegram entry delivery must not drop fills sharing a timestamp or claim live paper."""
from kaiba.core.schemas import LaneMode, Side
from kaiba.ops import trade_notify as notify
from tests.test_accounting import make_order, persist


def test_same_timestamp_over_batch_limit_is_not_lost(tmp_db):
    at = 2_000_000
    notify._ensure_cursor(tmp_db)
    tmp_db.execute("INSERT INTO notify_cursor VALUES('trades',?,?)", (at - 1, at))
    for i in range(27):
        order = make_order(order_id=f"fill_{i:02d}", filled_out=123)
        persist(tmp_db, order)
        tmp_db.execute("UPDATE orders SET updated_ms=? WHERE order_id=?", (at, order.order_id))
    first = notify.pending(tmp_db, now_ms=at + 1)
    assert len(first) == 25
    for row in first:
        notify._mark(tmp_db, row['order_id'], delivered=True, now=at + 1)
    tmp_db.execute("UPDATE notify_cursor SET last_ms=? WHERE name='trades'", (at,))
    second = notify.pending(tmp_db, now_ms=at + 2)
    assert len(second) == 2, "equal-time fills after batch 25 were silently skipped"


def test_paper_provider_never_generates_live_alert(tmp_db):
    at = 2_000_000
    notify._ensure_cursor(tmp_db)
    tmp_db.execute("INSERT INTO notify_cursor VALUES('trades',?,?)", (at - 1, at))
    order = make_order(order_id='canary_paper', mode=LaneMode.CANARY, filled_out=123)
    persist(tmp_db, order)
    tmp_db.execute("UPDATE orders SET provider='paper', updated_ms=? WHERE order_id=?", (at, order.order_id))
    assert notify.pending(tmp_db, now_ms=at + 1) == []
