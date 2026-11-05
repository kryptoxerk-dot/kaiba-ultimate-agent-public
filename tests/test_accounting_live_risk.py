"""Live fill -> daily risk-state regression tests.

These tests exercise the accounting seam, not providers or the funded database.  The live
risk row must advance with the same exactly-once claim as ``position_orders``; paper/shadow
fills must never enter it.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
import yaml

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, LaneMode, Side
from kaiba.execution import accounting
from kaiba.execution.risk import RiskGate
from tests.test_accounting import ONE_SOL, TOKEN, make_order, persist


def risk_state(conn):
    row = fetch_one(conn, "SELECT * FROM risk_state", ())
    assert row is not None, "a confirmed live fill must create today's risk_state row"
    return row, json.loads(row["realized_native_json"])


@pytest.fixture
def loss_stop_config(tmp_path, monkeypatch):
    """Use the existing test envelope without touching config/risk.yaml."""
    from tests.test_risk import BASE_RISK

    payload = copy.deepcopy(BASE_RISK)
    payload["chains"]["sol"]["daily_loss_stop_base_units"] = 500_000_000
    path = tmp_path / "risk.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def test_live_first_buy_and_scale_in_count_once_while_shadow_stays_separate(tmp_db):
    first = persist(tmp_db, make_order(order_id="risk_buy_1", filled_out=1_000_000))
    accounting.apply_fill(first, tmp_db, price_usd=Decimal("1"))
    row, realized = risk_state(tmp_db)
    assert row["entries"] == 1
    assert realized == {"sol": 0}

    add = persist(
        tmp_db,
        make_order(order_id="risk_buy_2", decision_id="risk_dec_2", filled_out=3_000_000,
                  amount_in=2 * ONE_SOL),
    )
    accounting.apply_fill(add, tmp_db, price_usd=Decimal("2"))
    row, realized = risk_state(tmp_db)
    assert row["entries"] == 2
    assert realized == {"sol": 0}

    paper = persist(
        tmp_db,
        make_order(order_id="risk_shadow_buy", decision_id="risk_shadow_dec",
                   mode=LaneMode.SHADOW, filled_out=5_000_000),
    )
    accounting.apply_fill(paper, tmp_db, price_usd=Decimal("3"))
    row, realized = risk_state(tmp_db)
    assert row["entries"] == 2
    assert realized == {"sol": 0}


def test_live_partial_exits_record_realized_deltas_then_the_final_cumulative_value(tmp_db):
    accounting.apply_fill(
        persist(tmp_db, make_order(order_id="risk_partial_buy", filled_out=1_000_000)),
        tmp_db,
        price_usd=Decimal("1"),
    )
    partial = persist(
        tmp_db,
        make_order(Side.SELL, order_id="risk_partial_sell", decision_id=None,
                   amount_in=250_000, filled_out=400_000_000),
    )
    accounting.apply_fill(partial, tmp_db)
    _, realized = risk_state(tmp_db)
    assert realized == {"sol": 150_000_000}

    final = persist(
        tmp_db,
        make_order(Side.SELL, order_id="risk_final_sell", decision_id=None,
                   amount_in=750_000, filled_out=300_000_000),
    )
    accounting.apply_fill(final, tmp_db)
    _, realized = risk_state(tmp_db)
    assert realized == {"sol": -300_000_000}

    position = fetch_one(tmp_db, "SELECT realized_native FROM positions", ())
    assert int(position["realized_native"]) == -300_000_000


def test_replaying_live_fills_does_not_double_update_daily_risk_state(tmp_db):
    buy = persist(tmp_db, make_order(order_id="risk_replay_buy", filled_out=1_000_000))
    partial = persist(
        tmp_db,
        make_order(Side.SELL, order_id="risk_replay_sell", decision_id=None,
                   amount_in=400_000, filled_out=250_000_000),
    )
    accounting.apply_fill(buy, tmp_db, price_usd=Decimal("1"))
    accounting.apply_fill(partial, tmp_db)
    before = dict(fetch_one(tmp_db, "SELECT * FROM risk_state", ()))

    for order in (buy, partial, buy, partial):
        accounting.apply_fill(order, tmp_db)

    after = dict(fetch_one(tmp_db, "SELECT * FROM risk_state", ()))
    assert after == before
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM position_orders", ())["n"] == 2


def test_live_risk_realized_keeps_chain_totals_separate(tmp_db):
    sol = persist(tmp_db, make_order(order_id="risk_sol_buy", filled_out=1_000_000))
    accounting.apply_fill(sol, tmp_db, price_usd=Decimal("1"))

    bsc_token = "0x" + "ab" * 20
    bsc = make_order(order_id="risk_bsc_buy", decision_id="risk_bsc_dec", token=bsc_token,
                     filled_out=2_000_000)
    bsc = bsc.model_copy(update={
        "chain": Chain.BSC,
        "input_token": "0x" + "00" * 20,
        "output_token": bsc_token,
    })
    accounting.apply_fill(persist(tmp_db, bsc), tmp_db, price_usd=Decimal("2"))

    row, realized = risk_state(tmp_db)
    assert row["entries"] == 2
    assert realized == {"sol": 0, "bsc": 0}


def test_loss_stop_summary_uses_live_realized_state_and_unknown_fees_stay_unknown(
    tmp_db, loss_stop_config
):
    buy = persist(
        tmp_db,
        make_order(order_id="risk_unknown_fee_buy", filled_out=1_000_000, fee_native=None),
    )
    sell = persist(
        tmp_db,
        make_order(Side.SELL, order_id="risk_unknown_fee_sell", decision_id=None,
                   amount_in=1_000_000, filled_out=500_000_000, fee_native=None),
    )
    accounting.apply_fill(buy, tmp_db, price_usd=Decimal("1"))
    accounting.apply_fill(sell, tmp_db)

    row, realized = risk_state(tmp_db)
    assert realized == {"sol": -500_000_000}

    summary = RiskGate().daily_summary(tmp_db)
    sol = summary["chains"]["sol"]
    assert sol["realized_native"] == -500_000_000
    assert sol["loss_budget_left"] == 0
    assert sol["stopped"] is True

    fee_rows = fetch_all(
        tmp_db,
        "SELECT fee_native FROM orders WHERE order_id IN (?,?) ORDER BY order_id",
        (buy.order_id, sell.order_id),
    )
    assert [row["fee_native"] for row in fee_rows] == [None, None]

    closed = fetch_one(tmp_db, "SELECT payload FROM events WHERE kind='position.closed'", ())
    assert closed is not None
    assert json.loads(closed["payload"])["fees_complete"] is False
