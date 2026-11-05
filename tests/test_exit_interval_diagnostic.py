"""EXIT-3 diagnostic: demonstrate local unsent deferral asymmetry, no fix asserted."""
import math

import pytest

from kaiba.core import limiter
from kaiba.core.schemas import OrderState, Side
from kaiba.execution import executor
from tests.test_exit_slippage_policy import order

pytest_plugins = ('tests.test_exit_slippage_policy',)


@pytest.mark.parametrize('side,expected_state', [
    (Side.SELL, OrderState.FAILED), (Side.BUY, OrderState.PLANNED),
])
def test_unsent_interval_collision_and_real_time_recovery(book, monkeypatch, side, expected_state):
    db, sent, _ = book
    clock = [1_000_000]
    monkeypatch.setattr(limiter, 'now_ms', lambda: clock[0])
    limiter.reserve('gmgn', 'trade.query_order', limiter.Priority.POSITION, db)
    limiter.release('gmgn', 'trade.query_order', 'ok', conn=db)
    db.execute("UPDATE provider_state SET penalty_level=2 WHERE provider='gmgn'")
    clock[0] += 20
    candidate = order(side=side, bps=8800 if side is Side.SELL else 2500)
    with pytest.raises(limiter.RateLimited) as caught:
        executor.submit_gmgn(candidate, db)
    assert caught.value.reason == 'minimum interval'
    assert 0 < caught.value.retry_after_s < 1
    assert sent == []
    row = db.execute('SELECT state,provider_order_id,tx_hash FROM orders WHERE order_id=?',
                     (candidate.order_id,)).fetchone()
    assert row['state'] == expected_state.value
    assert row['provider_order_id'] is None and row['tx_hash'] is None

    # Same real guard, after its exact requested delay: a positive control proves
    # this was a local timing refusal, not a malformed order or policy rejection.
    clock[0] += math.ceil(caught.value.retry_after_s*1000) + 1
    assert executor.submit_gmgn(candidate, db).state is OrderState.SUBMITTED
    assert len(sent) == 1
