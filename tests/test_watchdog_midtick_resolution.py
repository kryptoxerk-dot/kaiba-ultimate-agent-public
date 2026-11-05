"""An exit that resolves *during* a tick must not be followed by a second sell in that tick.

MEASURED 2026-09-21 on the agent's second live trade. Sell ord:92508c8d139f6a69ed1e was
reconciled to `filled` at 1790007863153 by the ops process, which closed the position in
the same write. 925 ms later the protection process, holding a position snapshot taken
before that write (qty=399,830,918,258), reached ``_in_flight`` for the same stop, read
`filled` off the orders table, concluded "not in flight", and sent
ord:4b88101f2ae802c03f44 -- a second 100% sell of tokens no longer held. The venue refused
it before send (confirmation prompt); nothing in this codebase guaranteed that.

``_refresh_exit`` runs at the start of the position's check, ``_in_flight`` immediately
before the submit. The window between them is where the other process wrote. This test
flips the order row inside that window.
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.schemas import LaneMode, OrderState
from kaiba.execution import watchdog as wd
from tests.test_watchdog import (  # noqa: F401 - risk_file is a fixture, used by name
    TOKEN, FakeSource, RecordingSubmitter, events_named, make_position, now_ms, risk_file,
)

LIVE_EXIT = "ord_live_exit"


def _insert_submitted_sell(conn, position_token: str) -> None:
    conn.execute(
        "INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, input_token, "
        "output_token, amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (LIVE_EXIT, "dec_x", "sol", position_token, "sell", "confluence-5", "canary", position_token,
         "So11111111111111111111111111111111111111112", "1000000", "1", 2500, "submitted", "gmgn",
         now_ms(), now_ms()),
    )
    conn.commit()


def test_an_exit_resolved_between_refresh_and_submit_is_not_sold_again(tmp_db, risk_file, monkeypatch):
    make_position(tmp_db, mode=LaneMode.CANARY)
    # A live-shaped submitter: the venue accepts, the order is SUBMITTED, not FILLED, so the
    # full-exit latch cannot be set at submission time (that is the real executor's shape).
    submitter = RecordingSubmitter(wd.ExitOutcome(True, OrderState.SUBMITTED, LIVE_EXIT, "submitted"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)

    dog.tick()  # stop breach -> one live sell, now in flight
    assert [c[1] for c in submitter.calls] == [Decimal(100)]
    _insert_submitted_sell(tmp_db, TOKEN)

    # The other process resolves the sell in the window after this tick's refresh.
    original_refresh = dog._refresh_exit

    def refresh_then_other_process_reconciles(state):
        original_refresh(state)
        tmp_db.execute("UPDATE orders SET state='filled' WHERE order_id=?", (LIVE_EXIT,))
        tmp_db.commit()

    monkeypatch.setattr(dog, "_refresh_exit", refresh_then_other_process_reconciles)

    dog.tick()  # same breach, stale snapshot, order now `filled` under our feet

    assert len(submitter.calls) == 1, f"sold twice: {submitter.calls}"
    held = [e for e in events_named(tmp_db, "exit_not_resubmitted") if "stale" in str(e.get("detail"))]
    assert held, "the refusal must say the snapshot is stale, not be silent"

    # And the latch is set, so the tick after that cannot sell it either.
    dog.tick()
    assert len(submitter.calls) == 1
    assert events_named(tmp_db, "exit_suppressed"), "a filled full exit is suppressed, not re-sent"


def test_an_exit_that_failed_before_the_tick_is_still_retried(tmp_db, risk_file):
    """The lock is for a change *during* the tick. A failure recorded earlier stays retryable."""
    make_position(tmp_db, mode=LaneMode.CANARY)
    submitter = RecordingSubmitter(wd.ExitOutcome(False, OrderState.FAILED, LIVE_EXIT, "venue said no"))
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({TOKEN: "0.6"}), submitter=submitter)
    dog.tick()
    assert len(submitter.calls) == 1
    # Past the retry backoff, the same breach is retried: recorded `failed` == row `failed`.
    tmp_db.execute("UPDATE watchdog_state SET exit_retry_after_ms=0")
    tmp_db.commit()
    dog.tick()
    assert len(submitter.calls) == 2
