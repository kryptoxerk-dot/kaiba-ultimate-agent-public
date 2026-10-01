"""The live position ledger: idempotency, clamping, and the rules that protect a balance.

Coverage is not the point here. Four properties are, and each one is a way this module
could silently destroy money rather than merely fail:

* a replay must be a no-op, because ``executor.reconcile`` is *designed* to run again and
  again against the same order;
* an ``UNKNOWN`` order must move nothing, because the ambiguous-send rule says we do not
  know whether it reached a venue;
* quantities must never go negative and must never pass through a float;
* a live position must come out the same shape as a paper one, or the watchdog, the
  dashboard and the learning loop all have to learn the difference.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import (
    Chain,
    EventKind,
    EvidenceBasis,
    Lane,
    LaneMode,
    Order,
    OrderState,
    Position,
    Receipt,
    Side,
    now_ms,
)
from kaiba.execution import accounting, protection
from kaiba.execution.paper import load_position
from kaiba.execution.protection import protection_config as _pcfg


def _stop_from(entry):
    """The shipped hard stop applied to `entry`.

    Derived, not hardcoded: this broke when the owner moved the stop from 3000 to
    4000 bps on 2026-09-23, which proved only that a number lived in two places.
    """
    return Decimal(str(entry)) * (1 - Decimal(_pcfg().stop_loss_bps) / Decimal(10000))

TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
NATIVE = "So11111111111111111111111111111111111111112"
ONE_SOL = 1_000_000_000
NOW = 1_780_000_000_000


def make_order(
    side: Side = Side.BUY,
    *,
    order_id: str = "ord_live_1",
    decision_id: str | None = "dec_live_1",
    amount_in: int = ONE_SOL,
    filled_out: int | None = 1_000_000,
    state: OrderState = OrderState.FILLED,
    mode: LaneMode = LaneMode.LIVE,
    fee_native: int | None = None,
    token: str = TOKEN,
) -> Order:
    buy = side is Side.BUY
    return Order(
        order_id=order_id,
        decision_id=decision_id,
        chain=Chain.SOL,
        token=token,
        side=side,
        lane=Lane.CONFLUENCE_5,
        mode=mode,
        input_token=NATIVE if buy else token,
        output_token=token if buy else NATIVE,
        amount_in=amount_in,
        min_out=0,
        slippage_bps=300,
        state=state,
        provider="gmgn",
        provider_order_id="gm-1",
        tx_hash="0xfeed",
        filled_out=filled_out,
        fee_native=fee_native,
        created_ms=NOW,
        updated_ms=NOW,
    )





def persist(conn, order: Order) -> Order:
    """Put the order in the table, which is what ``_fees_for_position`` joins against."""
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "provider_order_id, tx_hash, filled_out, fee_native, created_ms, updated_ms, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order.order_id, order.decision_id, order.chain.value, order.token, order.side.value,
            order.lane.value, order.mode.value, order.input_token, order.output_token,
            str(order.amount_in), str(order.min_out), order.slippage_bps, order.state.value,
            order.provider, order.provider_order_id, order.tx_hash,
            None if order.filled_out is None else str(order.filled_out),
            None if order.fee_native is None else str(order.fee_native),
            order.created_ms, order.updated_ms, order.error,
        ),
    )
    return order


def put_fill_context(conn, native_usd: str, *, decimals: int = 6, token: str = TOKEN, ts: int = NOW) -> None:
    """What a live fill needs to be priced: verified decimals and a contemporaneous native sample.

    Replaces the dossier-quote fixture this file used to carry. The entry price no longer
    comes from a pre-trade quote; it is the fill's own native/token ratio converted with
    the native sample nearest the fill's instant (``order.updated_ms`` here, which is
    ``NOW``), so a test that wants a USD entry has to supply exactly those two inputs.
    """
    from kaiba.core.db import jdump

    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms, meta_json) VALUES (?,?,?,?,?)",
        (Chain.SOL.value, token, decimals, ts, jdump({"decimals_source": "verified_onchain"})),
    )
    receipt = Receipt(provider="test", endpoint="native.sample", observed_at_ms=ts,
                      basis=EvidenceBasis.PROVIDER_REPORTED)
    conn.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) VALUES (?,?,?,?,?)",
        (Chain.SOL.value, ts, native_usd, "test", receipt.model_dump_json()),
    )


def only_position(conn) -> Position:
    rows = fetch_all(conn, "SELECT position_id FROM positions", ())
    assert len(rows) == 1, f"expected exactly one position, got {len(rows)}"
    pos = load_position(conn, rows[0]["position_id"])
    assert pos is not None
    return pos


# --------------------------------------------------------------------------------------
# a buy opens a position
# --------------------------------------------------------------------------------------


def test_a_filled_buy_opens_a_position(tmp_db):
    """The whole gap: before this, a live fill left real money with no position row."""
    order = persist(tmp_db, make_order())
    accounting.apply_fill(order, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == 1_000_000
    assert pos.qty_total == 1_000_000
    assert pos.cost_native == ONE_SOL
    assert pos.closed_ms is None


def test_the_fee_is_part_of_the_cost_basis(tmp_db):
    order = persist(tmp_db, make_order(fee_native=7_000_000))
    accounting.apply_fill(order, tmp_db)
    assert only_position(tmp_db).cost_native == ONE_SOL + 7_000_000


def test_the_buy_links_the_order_to_the_position(tmp_db):
    order = persist(tmp_db, make_order())
    pos = accounting.apply_fill(order, tmp_db)
    assert pos is not None
    row = fetch_one(tmp_db, "SELECT * FROM position_orders WHERE order_id=?", (order.order_id,))
    assert row is not None and row["position_id"] == pos.position_id and row["side"] == "buy"


def test_a_buy_emits_position_opened_and_marks_it_not_paper(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    opened = [e for e in ev.recent(conn=tmp_db) if e.kind == EventKind.POSITION_OPENED]
    assert opened and opened[0].payload["paper"] is False


def test_a_partial_buy_uses_what_was_spent_not_what_was_asked(tmp_db):
    """``amount_in`` is the request. A partial fill spends less, and the basis must follow."""
    order = persist(tmp_db, make_order(amount_in=ONE_SOL, filled_out=400_000))
    accounting.apply_fill(order, tmp_db, filled_in="600000000")
    pos = only_position(tmp_db)
    assert pos.cost_native == 600_000_000
    assert pos.qty == 400_000


def test_a_fill_with_no_reported_quantity_opens_nothing_and_shouts(tmp_db):
    order = persist(tmp_db, make_order(filled_out=None))
    assert accounting.apply_fill(order, tmp_db) is None
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []
    errors = [e for e in ev.recent(conn=tmp_db) if e.level == "error"]
    assert any(e.payload.get("event") == "unaccounted_fill" for e in errors)


# --------------------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------------------


def test_applying_the_same_buy_three_times_does_not_triple_the_position(tmp_db):
    """``reconcile`` runs repeatedly by design; a replay has to be a no-op."""
    order = persist(tmp_db, make_order())
    for _ in range(3):
        accounting.apply_fill(order, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == 1_000_000
    assert pos.cost_native == ONE_SOL
    links = fetch_all(tmp_db, "SELECT * FROM position_orders WHERE order_id=?", (order.order_id,))
    assert len(links) == 1


def test_applying_the_same_sell_three_times_does_not_triple_the_reduction(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_live_2", decision_id=None,
                                      amount_in=400_000, filled_out=500_000_000))
    for _ in range(3):
        accounting.apply_fill(sell, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == 600_000
    assert pos.proceeds_native == 500_000_000


def test_a_replay_returns_the_same_position_rather_than_none(tmp_db):
    order = persist(tmp_db, make_order())
    first = accounting.apply_fill(order, tmp_db)
    second = accounting.apply_fill(order, tmp_db)
    assert first is not None and second is not None
    assert first.position_id == second.position_id


# --------------------------------------------------------------------------------------
# an UNKNOWN order is not a fill
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [OrderState.UNKNOWN, OrderState.SUBMITTED, OrderState.PLANNED, OrderState.FAILED,
     OrderState.PARTIAL, OrderState.EXPIRED],
)
def test_only_a_filled_order_moves_the_ledger(tmp_db, state):
    order = persist(tmp_db, make_order(state=state))
    assert accounting.apply_fill(order, tmp_db) is None
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []


def test_an_unknown_sell_leaves_the_position_exactly_as_it_was(tmp_db):
    """We do not know whether an ambiguous send reached the venue. Assume nothing."""
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    before = only_position(tmp_db)
    unknown = persist(tmp_db, make_order(Side.SELL, order_id="ord_live_3", decision_id=None,
                                         amount_in=1_000_000, filled_out=900_000_000,
                                         state=OrderState.UNKNOWN))
    assert accounting.apply_fill(unknown, tmp_db) is None
    after = only_position(tmp_db)
    assert after.model_dump() == before.model_dump()


# --------------------------------------------------------------------------------------
# adding to an open position
# --------------------------------------------------------------------------------------


def test_a_second_buy_adds_rather_than_opening_a_second_position(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(order_id="ord_live_b", decision_id="dec_b",
                                   amount_in=2 * ONE_SOL, filled_out=3_000_000)),
        tmp_db,
    )
    pos = only_position(tmp_db)
    assert pos.qty == 4_000_000
    assert pos.qty_total == 4_000_000
    assert pos.cost_native == 3 * ONE_SOL


def test_adding_produces_a_weighted_average_entry_not_an_overwrite(tmp_db):
    """1,000,000 atoms at $1 plus 3,000,000 at $2 is $1.75, not $2.

    With 6 decimals and SOL at $1: 1 SOL for 1 token is $1/token, 6 SOL for 3 tokens is
    $2/token. Both prices come from the fills themselves, not from a quote.
    """
    put_fill_context(tmp_db, "1.0")
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(order_id="ord_live_b", decision_id="dec_b",
                                   amount_in=6 * ONE_SOL, filled_out=3_000_000)),
        tmp_db,
    )
    assert only_position(tmp_db).entry_price_usd == Decimal("1.75")


def test_a_different_lane_gets_its_own_position(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    other = make_order(order_id="ord_other", decision_id="dec_other")
    other = other.model_copy(update={"lane": Lane.TRUSTED_COPY})
    accounting.apply_fill(persist(tmp_db, other), tmp_db)
    assert len(fetch_all(tmp_db, "SELECT * FROM positions", ())) == 2


def test_a_live_fill_never_merges_into_a_shadow_position(tmp_db):
    """Same decision, different mode. Merging the two would corrupt both records."""
    shadow = make_order(mode=LaneMode.SHADOW, order_id="ord_shadow")
    accounting.apply_fill(persist(tmp_db, shadow), tmp_db)
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    rows = fetch_all(tmp_db, "SELECT mode FROM positions ORDER BY mode", ())
    assert [r["mode"] for r in rows] == ["live", "shadow"]


# --------------------------------------------------------------------------------------
# selling
# --------------------------------------------------------------------------------------


def test_a_partial_sell_reduces_the_quantity_and_banks_the_proceeds(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=250_000, filled_out=400_000_000))
    accounting.apply_fill(sell, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == 750_000
    assert pos.proceeds_native == 400_000_000
    # A quarter of the position is sold, so a quarter of the cost basis is realised.
    assert pos.realized_native == 400_000_000 - ONE_SOL // 4
    assert pos.closed_ms is None


def test_a_sell_uses_the_provider_input_amount_when_it_reports_one(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=500_000, filled_out=400_000_000))
    accounting.apply_fill(sell, tmp_db, filled_in="200000")
    assert only_position(tmp_db).qty == 800_000


def test_selling_everything_closes_the_position(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=1_000_000, filled_out=1_500_000_000))
    accounting.apply_fill(sell, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == 0
    assert pos.closed_ms is not None
    assert pos.exit_reason


def test_closing_writes_a_trade_row_the_learning_loop_can_read(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order(fee_native=5_000_000)), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=1_000_000, filled_out=2_000_000_000,
                                      fee_native=6_000_000))
    accounting.apply_fill(sell, tmp_db)
    trade = fetch_one(tmp_db, "SELECT * FROM trades", ())
    assert trade is not None
    assert int(trade["cost_native"]) == ONE_SOL + 5_000_000
    assert int(trade["proceeds_native"]) == 2_000_000_000
    assert int(trade["pnl_native"]) == 2_000_000_000 - (ONE_SOL + 5_000_000)
    assert int(trade["fees_native"]) == 11_000_000
    assert trade["mode"] == "live"


def test_closing_emits_position_closed(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                   amount_in=1_000_000, filled_out=1_100_000_000)),
        tmp_db,
    )
    closed = [e for e in ev.recent(conn=tmp_db) if e.kind == EventKind.POSITION_CLOSED]
    assert closed and closed[0].payload["paper"] is False


def test_the_exit_reason_comes_from_the_watchdog_when_the_watchdog_sold(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    pos = only_position(tmp_db)
    tmp_db.execute(
        "INSERT INTO watchdog_state (position_id, exit_order_id, exit_reason, updated_ms) "
        "VALUES (?,?,?,?)",
        (pos.position_id, "ord_sell", "trailing_stop", now_ms()),
    )
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                   amount_in=1_000_000, filled_out=900_000_000)),
        tmp_db,
    )
    assert only_position(tmp_db).exit_reason == "trailing_stop"


# --------------------------------------------------------------------------------------
# the ledger never goes negative
# --------------------------------------------------------------------------------------


def test_an_oversell_clamps_to_zero_rather_than_going_negative(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=5_000_000, filled_out=3_000_000_000))
    pos = accounting.apply_fill(sell, tmp_db)
    assert pos is not None and pos.qty == 0
    assert only_position(tmp_db).qty == 0


def test_an_oversell_closes_the_position_and_says_the_books_disagree(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                   amount_in=5_000_000, filled_out=3_000_000_000)),
        tmp_db,
    )
    assert only_position(tmp_db).closed_ms is not None
    loud = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "position_oversold"]
    assert loud and loud[0].level == "error"
    assert loud[0].payload["excess"] == "4000000"


def test_an_oversell_is_journalled_as_a_correction(tmp_db):
    from kaiba.core import journal

    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                   amount_in=9_000_000, filled_out=1_000)),
        tmp_db,
    )
    assert any(e["kind"] == "correction" for e in journal.read(conn=tmp_db))


def test_a_sell_against_no_position_is_loud_and_does_not_raise(tmp_db):
    sell = persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                      amount_in=1_000_000, filled_out=1_000_000_000))
    assert accounting.apply_fill(sell, tmp_db) is None
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []
    loud = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "sell_without_position"]
    assert loud and loud[0].level == "error"


def test_a_sell_against_an_already_closed_position_does_not_reopen_it(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell_1", decision_id=None,
                                   amount_in=1_000_000, filled_out=1_000_000_000)),
        tmp_db,
    )
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell_2", decision_id=None,
                                   amount_in=1, filled_out=1)),
        tmp_db,
    )
    pos = only_position(tmp_db)
    assert pos.closed_ms is not None and pos.qty == 0


# --------------------------------------------------------------------------------------
# money is integers
# --------------------------------------------------------------------------------------


def test_a_quantity_larger_than_two_to_the_sixty_three_survives_a_round_trip(tmp_db):
    """These columns are TEXT for exactly this reason. A float would round it away."""
    huge = 2**70 + 12345
    order = persist(tmp_db, make_order(amount_in=huge, filled_out=huge))
    accounting.apply_fill(order, tmp_db)
    pos = only_position(tmp_db)
    assert pos.qty == huge
    assert pos.cost_native == huge
    assert fetch_one(tmp_db, "SELECT qty FROM positions", ())["qty"] == str(huge)


def test_no_money_value_is_parsed_as_a_float(tmp_db, monkeypatch):
    """A float anywhere in this path is a corrupted balance that never raises.

    Scoped to the open and the partial reduce. ``_close_trade`` uses ``float`` once, for
    ``pnl_pct``, which is a REAL column and a reporting number rather than a balance.
    """
    import kaiba.execution.accounting as mod

    def banned(*a, **kw):
        raise AssertionError("float() must never touch a base-unit amount")

    monkeypatch.setattr(mod, "float", banned, raising=False)
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    accounting.apply_fill(
        persist(tmp_db, make_order(Side.SELL, order_id="ord_sell", decision_id=None,
                                   amount_in=250_000, filled_out=2**68)),
        tmp_db,
    )
    pos = only_position(tmp_db)
    assert pos.qty == 750_000
    assert pos.proceeds_native == 2**68


# --------------------------------------------------------------------------------------
# the row is the same shape the paper broker writes
# --------------------------------------------------------------------------------------


def test_the_live_writer_and_the_paper_writer_produce_identical_rows(tmp_db):
    """Guards the one duplication in this module: two writers of the same row.

    ``accounting._save_position`` is a hand copy of ``PaperBroker._save_position``. If
    either grows a column the other does not, live and paper positions stop being the same
    kind of object and every consumer has to care which is which.
    """
    from kaiba.execution.paper import PaperBroker

    def make(pid: str) -> Position:
        return Position(
            position_id=pid, chain=Chain.SOL, token=TOKEN, lane=Lane.CONFLUENCE_5,
            mode=LaneMode.LIVE, opened_ms=NOW, qty=10, qty_total=20, cost_native=30,
            proceeds_native=40, realized_native=50, entry_price_usd=Decimal("1.5"),
            peak_price_usd=Decimal("2.5"), stop_price_usd=Decimal("1.1"), tp_done=["tp1"],
            protected=True, protection_ids=["x"], mae_pct=-1.0, mfe_pct=2.0, exit_reason="tp1",
        )

    accounting._save_position(tmp_db, make("pos_live"))
    PaperBroker(tmp_db)._save_position(make("pos_paper"))
    live = dict(fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id='pos_live'", ()))
    paper = dict(fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id='pos_paper'", ()))
    live.pop("position_id")
    paper.pop("position_id")
    assert live == paper


# --------------------------------------------------------------------------------------
# protection.arm
# --------------------------------------------------------------------------------------


def arm_fixture(conn, entry: str | None = "2.0", stop: str | None = None) -> str:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, entry_price_usd, stop_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("pos_arm", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "1000", "1000", "1000", entry, stop),
    )
    return "pos_arm"


def test_arm_writes_the_hard_stop_and_the_protected_flag(tmp_db):
    """The shipped hard stop applied to a $2.00 entry, whatever that stop is."""
    pid = arm_fixture(tmp_db)
    assert protection.arm(pid, tmp_db) is True
    row = fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id=?", (pid,))
    assert Decimal(row["stop_price_usd"]) == _stop_from("2.00")
    assert row["protected"] == 1


def test_arm_emits_protection_set(tmp_db):
    protection.arm(arm_fixture(tmp_db), tmp_db)
    assert any(e.kind == EventKind.PROTECTION_SET for e in ev.recent(conn=tmp_db))


def test_arm_is_safe_to_call_twice(tmp_db):
    pid = arm_fixture(tmp_db)
    assert protection.arm(pid, tmp_db) is True
    assert protection.arm(pid, tmp_db) is True
    row = fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id=?", (pid,))
    assert Decimal(row["stop_price_usd"]) == _stop_from("2.00")


def test_arm_never_lowers_a_stop_the_watchdog_already_raised(tmp_db):
    """Re-arming after a ratchet must not hand back the -30% stop."""
    pid = arm_fixture(tmp_db, stop="1.95")
    assert protection.arm(pid, tmp_db) is True
    row = fetch_one(tmp_db, "SELECT * FROM positions WHERE position_id=?", (pid,))
    assert Decimal(row["stop_price_usd"]) == Decimal("1.95")
    assert row["protected"] == 1


def test_arm_without_an_entry_price_refuses_loudly_instead_of_raising(tmp_db):
    pid = arm_fixture(tmp_db, entry=None)
    assert protection.arm(pid, tmp_db) is False
    loud = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "protection_arm_failed"]
    assert loud and loud[0].level == "error"


def test_arm_on_a_missing_position_returns_false(tmp_db):
    assert protection.arm("pos_nope", tmp_db) is False


def test_arm_never_raises_even_when_the_database_is_broken(tmp_db):
    class Broken:
        in_transaction = False

        def execute(self, *a, **kw):
            raise RuntimeError("disk is on fire")

    assert protection.arm("pos_x", Broken()) is False


def test_a_live_fill_comes_out_armed(tmp_db):
    """The point of the whole exercise: a filled buy leaves a position with a stop.

    The stop is cut from the *measured* entry: 1 SOL for 1,000,000 six-decimal atoms with
    SOL at $1 is a $1.00 entry; the stop is whatever the shipped ladder says.
    """
    put_fill_context(tmp_db, "1.0")
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    pos = only_position(tmp_db)
    assert pos.protected is True
    assert pos.stop_price_usd == _stop_from("1.00")
    opened = [e for e in ev.recent(conn=tmp_db) if e.kind == EventKind.POSITION_OPENED]
    assert opened[0].payload["entry_price_basis"] == "fill_ratio"


def test_a_fill_with_no_priceable_entry_is_flagged_as_unprotectable(tmp_db):
    accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    pos = only_position(tmp_db)
    assert pos.entry_price_usd is None
    assert pos.protected is False
    loud = [
        e for e in ev.recent(conn=tmp_db)
        if e.payload.get("event") == "position_without_entry_price"
    ]
    assert loud and loud[0].level == "error"


# --------------------------------------------------------------------------------------
# wiring: executor.reconcile
# --------------------------------------------------------------------------------------


def submitted_buy(tmp_db):
    """An order already at the venue, written straight to the table.

    Deliberately not routed through ``submit_gmgn``: what is under test is the step from a
    reconciled fill to a position row, and going through submission would make these tests
    depend on the shared gmgn rate-limit budget, which is not what they are about.
    """
    from kaiba.execution import executor

    order = persist(
        tmp_db,
        make_order(order_id="ord_wire", decision_id="dec_wire", state=OrderState.SUBMITTED,
                   filled_out=None),
    )
    return executor, order


def test_reconciling_a_fill_writes_the_position(tmp_db, monkeypatch):
    executor, order = submitted_buy(tmp_db)
    monkeypatch.setattr(
        executor, "query_gmgn_order",
        lambda o, c=None: {"data": {"status": "successful", "tx_hash": "0xf", "output_amount": "777"}},
    )
    assert executor.reconcile(order.order_id, tmp_db) is OrderState.FILLED
    assert only_position(tmp_db).qty == 777


def test_reconciling_the_same_fill_repeatedly_does_not_double_the_position(tmp_db, monkeypatch):
    """Reconciliation is a sweep. It will see this order again on every pass."""
    executor, order = submitted_buy(tmp_db)
    monkeypatch.setattr(
        executor, "query_gmgn_order",
        lambda o, c=None: {"data": {"status": "successful", "output_amount": "777"}},
    )
    for _ in range(3):
        executor.reconcile(order.order_id, tmp_db)
    assert only_position(tmp_db).qty == 777


def test_a_pending_reconcile_writes_no_position(tmp_db, monkeypatch):
    executor, order = submitted_buy(tmp_db)
    monkeypatch.setattr(
        executor, "query_gmgn_order", lambda o, c=None: {"data": {"status": "pending"}}
    )
    executor.reconcile(order.order_id, tmp_db)
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []


def test_a_failed_reconcile_writes_no_position(tmp_db, monkeypatch):
    executor, order = submitted_buy(tmp_db)
    monkeypatch.setattr(
        executor, "query_gmgn_order", lambda o, c=None: {"data": {"status": "failed"}}
    )
    executor.reconcile(order.order_id, tmp_db)
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []


def test_a_broken_ledger_does_not_unwind_the_order_transition(tmp_db, monkeypatch):
    """The fill already happened. Bookkeeping failing must not look like a trading failure."""
    executor, order = submitted_buy(tmp_db)
    monkeypatch.setattr(
        executor, "query_gmgn_order",
        lambda o, c=None: {"data": {"status": "successful", "output_amount": "777"}},
    )
    monkeypatch.setattr(
        accounting, "open_or_add",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ledger exploded")),
    )
    assert executor.reconcile(order.order_id, tmp_db) is OrderState.FILLED
    row = fetch_one(tmp_db, "SELECT state FROM orders WHERE order_id=?", (order.order_id,))
    assert row["state"] == OrderState.FILLED.value
    loud = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "ledger_write_failed"]
    assert loud and loud[0].level == "error"


def test_the_decision_is_linked_to_the_position(tmp_db):
    """The learning loop joins decisions to outcomes; a live fill has to close that link."""
    tmp_db.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) "
        "VALUES (?,?,?,?,?,?,?)",
        ("dec_live_1", NOW, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, Chain.SOL.value,
         TOKEN, "buy"),
    )
    pos = accounting.apply_fill(persist(tmp_db, make_order()), tmp_db)
    row = fetch_one(tmp_db, "SELECT * FROM decision_outcomes WHERE decision_id=?", ("dec_live_1",))
    assert pos is not None and row is not None
    assert row["position_id"] == pos.position_id and row["outcome"] == "open"


# --------------------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------------------


def test_the_engine_is_no_longer_on_its_unprotected_fallback(tmp_db):
    """``protection.arm`` did not exist, so every paper fill took the ``log.debug`` stub.

    The engine imports it inside a ``try/except ImportError`` and silently substitutes a
    no-op, which means the symbol going missing again would show up as positions quietly
    coming out unprotected rather than as an error anywhere.
    """
    from kaiba.execution import engine

    assert engine.arm_protection is protection.arm


def test_the_primitives_refuse_the_wrong_side(tmp_db):
    with pytest.raises(ValueError, match="sell order"):
        accounting.open_or_add(make_order(Side.SELL), 1, 1, tmp_db)
    with pytest.raises(ValueError, match="buy order"):
        accounting.reduce(make_order(Side.BUY), 1, 1, tmp_db)
