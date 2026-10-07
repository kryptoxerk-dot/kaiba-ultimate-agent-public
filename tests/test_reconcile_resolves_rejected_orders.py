"""An order the venue REFUSED must not sit in UNKNOWN forever.

:func:`reconcile` is documented as "the only function permitted to move an order out of
UNKNOWN", and it can only do that by asking the provider about a ``provider_order_id``.
An order that was rejected on the POST never got one. So the function falls straight
through to ``return order.state`` and the order stays UNKNOWN for good.

That is not a cosmetic leak. The watchdog will not resubmit an exit while the previous one
is unresolved -- "an ambiguous or in-flight send is resolved by reconciliation, never
retried" -- so an unresolvable order is a position the machine can never close.

MEASURED 2026-09-22, minutes apart, on the live box: four exits in this exact shape
(UNKNOWN, no provider_order_id, no tx_hash, ``POST /v1/trade/swap failed: HTTP 400``),
three of them still holding open positions after 1.2-1.8 hours. The fourth had held a bsc
position through a fall from 8.75x to 3.79x before it was resolved by hand.

THE RULE. Resolve to FAILED only on all four together:

* the order is UNKNOWN, and
* we hold no ``provider_order_id`` -- nothing to query, and
* we hold no ``tx_hash`` -- nothing reached a chain, and
* the recorded error shows the venue answered the trade POST with a 4xx.

The fourth is what makes it evidence rather than a guess. Absence of an id is not proof of
absence of an order -- a POST that timed out may have created one whose answer we lost.
A 4xx is the venue stating it created nothing.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor
from kaiba.execution.executor import reconcile

TOKEN = "0x6863b3fd8caa85d6ca8f80bf10083a94de467777"
REJECTION = (
    "gmgn-cli exit 1: Swap - confirmation required\n"
    "[gmgn-cli] Proceeding non-interactively (--yes + GMGN_ALLOW_AUTOMATED_TRADES=1).\n"
    "[gmgn-cli] POST /v1/trade/swap failed: HTTP 400 code=400 error=40000300"
)


def put_order(
    conn,
    *,
    order_id: str = "ord:stuck",
    state: str = "unknown",
    provider_order_id: str | None = None,
    tx_hash: str | None = None,
    error: str | None = REJECTION,
    side: str = "sell",
) -> None:
    conn.execute(
        "INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "provider_order_id, tx_hash, created_ms, updated_ms, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order_id, "dec:x", Chain.BSC.value, TOKEN, side, Lane.SM_TRENCHES.value,
            LaneMode.LIVE.value, TOKEN, "0x0000000000000000000000000000000000000000",
            "467754762292813431665841", "5981072728932474", 2500, state, "gmgn",
            provider_order_id, tx_hash, 1_790_073_436_554, 1_790_073_437_172, error,
        ),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def _never_call_the_provider(monkeypatch):
    """These orders have nothing to query. A call here is itself the bug."""
    def boom(*a, **k):
        raise AssertionError("reconcile queried the provider for an order with no id")

    monkeypatch.setattr(executor, "query_gmgn_order", boom)


# --------------------------------------------------------------------- the resolution


def test_a_rejected_order_resolves_to_failed(tmp_db):
    """THE REGRESSION: this order deadlocked a live exit for 23 minutes."""
    put_order(tmp_db)
    assert reconcile("ord:stuck", tmp_db) is OrderState.FAILED
    row = tmp_db.execute("SELECT state FROM orders WHERE order_id='ord:stuck'").fetchone()
    assert row["state"] == OrderState.FAILED.value


def test_the_reason_is_written_to_the_order_history(tmp_db):
    """Whoever reads this later must see WHY, not just a state change."""
    put_order(tmp_db)
    reconcile("ord:stuck", tmp_db)
    details = [
        str(r["detail"])
        for r in tmp_db.execute(
            "SELECT detail FROM order_events WHERE order_id='ord:stuck'"
        )
    ]
    assert any("400" in d for d in details), details


def test_the_original_error_is_preserved_in_history(tmp_db):
    """`_transition` overwrites orders.error; the evidence must survive somewhere."""
    put_order(tmp_db)
    tmp_db.execute(
        "INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
        ("ord:stuck", 1, "unknown", REJECTION),
    )
    tmp_db.commit()
    reconcile("ord:stuck", tmp_db)
    blob = " ".join(
        str(r["detail"])
        for r in tmp_db.execute("SELECT detail FROM order_events WHERE order_id='ord:stuck'")
    )
    assert "40000300" in blob


# --------------------------------------------------------------------- what it must NOT do


def test_a_timeout_is_not_resolved(tmp_db):
    """No answer from the venue is the genuinely ambiguous case. It stays UNKNOWN."""
    put_order(tmp_db, error="[gmgn-cli] POST /v1/trade/swap failed: ETIMEDOUT")
    assert reconcile("ord:stuck", tmp_db) is OrderState.UNKNOWN


def test_a_5xx_is_not_resolved(tmp_db):
    """The venue may have accepted it and failed to answer."""
    put_order(tmp_db, error="[gmgn-cli] POST /v1/trade/swap failed: HTTP 502 Bad Gateway")
    assert reconcile("ord:stuck", tmp_db) is OrderState.UNKNOWN


def test_an_order_with_a_tx_hash_is_not_resolved(tmp_db):
    """Something reached a chain. Whatever the API said, this is not ours to declare dead."""
    put_order(tmp_db, tx_hash="0x95fa7ce8a1d1bb7c5a1669ff58ee870be037a983")
    assert reconcile("ord:stuck", tmp_db) is OrderState.UNKNOWN


def test_an_order_with_a_provider_id_is_not_resolved_this_way(tmp_db, monkeypatch):
    """It has an id, so the provider is the authority; it must take the query path."""
    asked: list[str] = []

    def fake_query(order, conn=None):
        asked.append(order.order_id)
        return {"data": {"status": "pending"}}

    monkeypatch.setattr(executor, "query_gmgn_order", fake_query)
    put_order(tmp_db, provider_order_id="od10bsc57184193193fdc5c")
    reconcile("ord:stuck", tmp_db)
    assert asked == ["ord:stuck"], "an order with an id must still be queried"


def test_an_order_with_no_error_recorded_is_not_resolved(tmp_db):
    """No evidence is not evidence of no send."""
    put_order(tmp_db, error=None)
    assert reconcile("ord:stuck", tmp_db) is OrderState.UNKNOWN


@pytest.mark.parametrize("state", ["submitted", "reserved", "submitting", "partial"])
def test_only_unknown_orders_are_resolved_this_way(tmp_db, state):
    """A SUBMITTED order is in flight by definition; this rule must not touch it."""
    put_order(tmp_db, state=state)
    assert reconcile("ord:stuck", tmp_db) is not OrderState.FAILED


def test_a_4xx_on_another_endpoint_is_not_resolved(tmp_db):
    put_order(tmp_db, error="[gmgn-cli] POST /v1/wallet/info failed: HTTP 400")
    assert reconcile("ord:stuck", tmp_db) is OrderState.UNKNOWN
