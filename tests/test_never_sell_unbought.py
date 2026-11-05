"""OWNER RULE 2026-10-05: "NEVER SELL ANYTHING THEY DON'T BUY" -- enforced at the executor.

A live sell must sell tokens Kaiba itself bought, never more than it bought, and never from
another wallet. Every refusal happens before an order row exists or the CLI is spawned.
All addresses are synthetic.
"""

from __future__ import annotations

import pytest

from kaiba.core.config import load_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor

RH = Chain.ROBINHOOD
WALLET = "0x2222222222222222222222222222222222222222"
TOKEN = "0x3333333333333333333333333333333333333333"
OTHER = "0x4444444444444444444444444444444444444444"
BIG = 5_681_287_464_350_799_837_512_446  # a real-sized EVM amount, > 2**63


@pytest.fixture
def live(monkeypatch):
    risk = load_risk()
    risk.kill_switch = risk.entries_paused = risk.reduce_only = False
    risk.chains[RH].wallet = WALLET
    monkeypatch.setattr(executor, "get_risk", lambda: risk)
    monkeypatch.setattr(executor, "_authorize", lambda order, body, wallet: None)
    sent: list[list[str]] = []

    def cli(args, timeout_s=45, *, mutating=False):
        sent.append(list(args))
        return {"data": {"order_id": f"fx-{len(sent)}", "tx_hash": None}}

    monkeypatch.setattr(executor, "_run_gmgn", cli)
    return sent


def sell(amount, token=TOKEN, mode=LaneMode.LIVE):
    return executor.build_order(decision_id=None, chain=RH, token=token, side=Side.SELL,
                                lane=Lane.SM_TRENCHES, mode=mode, amount_in=amount, min_out=1,
                                slippage_bps=300)


def _rows(conn, oid):
    return conn.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchall()


def test_a_token_kaiba_never_bought_is_never_sold(tmp_db, live):
    o = sell(1000)
    with pytest.raises(executor.ExecutionRefused, match="never bought"):
        executor.submit(o, tmp_db)
    assert live == [] and _rows(tmp_db, o.order_id) == []


def test_a_buy_of_another_token_does_not_license_this_one(tmp_db, live, kaiba_bought):
    kaiba_bought(tmp_db, RH, OTHER, qty=10**9)
    with pytest.raises(executor.ExecutionRefused, match="never bought"):
        executor.submit(sell(1000), tmp_db)
    assert live == []


def test_never_more_than_kaiba_bought(tmp_db, live, kaiba_bought):
    kaiba_bought(tmp_db, RH, TOKEN, qty=1000)
    with pytest.raises(executor.ExecutionRefused, match="remaining 1000"):
        executor.submit(sell(1001), tmp_db)
    assert executor.submit(sell(1000), tmp_db).state is OrderState.SUBMITTED
    assert len(live) == 1


def test_an_in_flight_sell_counts_so_a_second_full_exit_is_refused(tmp_db, live, kaiba_bought):
    kaiba_bought(tmp_db, RH, TOKEN, qty=1000)
    executor.submit(sell(600), tmp_db)            # submitted, not yet filled
    with pytest.raises(executor.ExecutionRefused, match="remaining 400"):
        executor.submit(sell(1000), tmp_db)
    assert executor.submit(sell(400), tmp_db).state is OrderState.SUBMITTED


def test_a_failed_sell_spent_nothing(tmp_db, live, kaiba_bought):
    kaiba_bought(tmp_db, RH, TOKEN, qty=1000)
    o = sell(1000)
    executor.submit(o, tmp_db)
    tmp_db.execute("UPDATE orders SET state='failed' WHERE order_id=?", (o.order_id,))
    assert executor.submit(sell(1000), tmp_db).state is OrderState.SUBMITTED


def test_wei_sized_amounts_are_compared_exactly(tmp_db, live, kaiba_bought):
    # SQLite's CAST AS INTEGER would clamp both to 2**63-1 and call them equal.
    kaiba_bought(tmp_db, RH, TOKEN, qty=BIG)
    with pytest.raises(executor.ExecutionRefused, match="never_sell_unbought"):
        executor.submit(sell(BIG + 1), tmp_db)
    assert executor.submit(sell(BIG), tmp_db).state is OrderState.SUBMITTED


def test_the_position_ledger_is_proof_of_kaibas_buy(tmp_db, live):
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native) VALUES ('pos_fx', 'robinhood', ?, 'sm-trenches', 'live', 1, '700', '700', '1')",
        (TOKEN,),
    )
    assert executor.submit(sell(700), tmp_db).state is OrderState.SUBMITTED


def test_buys_and_shadow_sells_are_untouched(tmp_db, live):
    buy = executor.build_order(decision_id=None, chain=RH, token=TOKEN, side=Side.BUY,
                               lane=Lane.MANUAL, mode=LaneMode.SHADOW, amount_in=1, min_out=1,
                               slippage_bps=300)
    assert executor.submit(buy, tmp_db).state is OrderState.PLANNED
    assert executor.submit(sell(10**9, mode=LaneMode.SHADOW), tmp_db).state is OrderState.PLANNED
    assert live == []
