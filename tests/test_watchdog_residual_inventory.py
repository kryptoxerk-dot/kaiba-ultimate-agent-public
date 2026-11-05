"""A filled 100%-of-old-inventory exit must not strand a concurrent, accounted buy."""
from decimal import Decimal

from kaiba.core.schemas import Chain, Lane, LaneMode, Order, OrderState, Side
from kaiba.execution import accounting, executor, watchdog as wd
from tests.test_watchdog import FakeSource, RecordingSubmitter, risk_file

TOKEN = "0x" + "ab" * 20


def fill(conn, name, side, qty, out):
    order = Order(
        order_id=name, chain=Chain.BSC, token=TOKEN, side=side,
        lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE,
        input_token=wd.EVM_ZERO if side is Side.BUY else TOKEN,
        output_token=TOKEN if side is Side.BUY else wd.EVM_ZERO,
        amount_in=qty, filled_out=out, min_out=1, slippage_bps=300,
        state=OrderState.FILLED,
    )
    executor._persist(order, conn)
    if side is Side.BUY:
        return accounting.open_or_add(order, out, qty, conn, price_usd=Decimal('1'))
    return accounting.reduce(order, qty, out, conn, exit_reason='stop_loss')


def latch(conn, position):
    state = wd.load_state(conn, position)
    state.exit_order_id = 'old-sell'
    state.exit_pct = Decimal(100)
    state.exit_state = 'filled'
    state.exit_final = True
    state.exit_reason = 'stop_loss'
    wd.save_state(conn, state)


def test_accounted_new_inventory_remains_exit_eligible_after_full_old_exit(tmp_db, risk_file):
    position = fill(tmp_db, 'first-buy', Side.BUY, 1000, 100)
    fill(tmp_db, 'concurrent-buy', Side.BUY, 750, 75)
    position = fill(tmp_db, 'old-sell', Side.SELL, 100, 500)
    assert position.qty == 75
    latch(tmp_db, position)
    submitter = RecordingSubmitter(wd.ExitOutcome(True, OrderState.SUBMITTED, 'residual-sell'))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: '0.4'}), submitter=submitter)
    report = dog.tick()
    assert report.exits == 1
    assert len(submitter.calls) == 1
    assert submitter.calls[0][0] == position.position_id
    assert submitter.calls[0][1] == Decimal(100)


def test_latch_stays_when_displayed_inventory_does_not_match_applied_fills(tmp_db, risk_file):
    position = fill(tmp_db, 'first-buy', Side.BUY, 1000, 100)
    position = fill(tmp_db, 'old-sell', Side.SELL, 100, 500)
    # Simulate a stale/corrupt position display. There is NO supporting buy left.
    tmp_db.execute('UPDATE positions SET qty=?,closed_ms=NULL WHERE position_id=?',
                   ('100', position.position_id))
    position = wd.open_positions(tmp_db)[0]
    latch(tmp_db, position)
    submitter = RecordingSubmitter()
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: '0.4'}), submitter=submitter)
    dog.tick()
    assert submitter.calls == []
