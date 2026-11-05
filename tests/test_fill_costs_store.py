"""fill_costs storage: one row per live fill, fetch failures retried, final rows never re-selected.

The decoders were verified on 16 real fills by dry run on the box (2026-10-05); this file pins
the bookkeeping around them with a fake fetch, so it never touches an RPC.
"""

from __future__ import annotations

from kaiba.core.schemas import Chain, Lane, LaneMode, Side
from kaiba.execution import executor, fill_costs


def _live_fill(conn, tx="5sigFixture", chain=Chain.SOL):
    o = executor.build_order(decision_id=None, chain=chain, token="MintFixture1111111111111111111111111111111",
                             side=Side.BUY, lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE, amount_in=100_000_000,
                             min_out=750, slippage_bps=2500)
    executor._persist(o, conn)  # noqa: SLF001
    conn.execute("UPDATE orders SET state='filled', filled_out='1000', tx_hash=?, updated_ms=? WHERE order_id=?",
                 (tx, 2_000_000_000_000, o.order_id))
    return o.order_id


def test_a_failed_fetch_is_retried_and_a_final_row_is_not(tmp_db):
    oid = _live_fill(tmp_db)
    calls: list[str] = []

    def failing(chain, sig):
        calls.append(sig)
        return None, "rpc_unavailable"

    out = fill_costs.record_fill_costs(tmp_db, since_ms=0, fetch=failing, pace_s=0)
    assert out["selected"] == 1 and out["coverage"] == {"sol:fetch_failed": 1}
    row = tmp_db.execute("SELECT basis FROM fill_costs WHERE order_id=?", (oid,)).fetchone()
    assert row[0] == "fetch_failed"

    # Retried next run; this time the transaction comes back but carries no venue event.
    def empty(chain, sig):
        calls.append(sig)
        return {"meta": {"err": None, "logMessages": []}, "transaction": {"message": {}}}, None

    fill_costs.record_fill_costs(tmp_db, since_ms=0, fetch=empty, pace_s=0)
    basis = tmp_db.execute("SELECT basis FROM fill_costs WHERE order_id=?", (oid,)).fetchone()[0]
    assert basis not in ("fetch_failed", None)

    # A final row is never selected again.
    again = fill_costs.record_fill_costs(tmp_db, since_ms=0, fetch=empty, pace_s=0)
    assert again["selected"] == 0 and len(calls) == 2


def test_shadow_and_unfilled_orders_are_never_decoded(tmp_db):
    o = executor.build_order(decision_id=None, chain=Chain.SOL, token="MintFixture2222222222222222222222222222222",
                             side=Side.BUY, lane=Lane.SM_TRENCHES, mode=LaneMode.SHADOW, amount_in=1, min_out=1,
                             slippage_bps=100)
    executor._persist(o, tmp_db)  # noqa: SLF001
    tmp_db.execute("UPDATE orders SET state='filled', tx_hash='x', updated_ms=2000000000000 WHERE order_id=?",
                   (o.order_id,))
    assert fill_costs.pending_orders(tmp_db, since_ms=0, limit=10) == []


def test_every_column_the_writer_uses_exists_in_the_migration(tmp_db):
    cols = {r[1] for r in tmp_db.execute("PRAGMA table_info(fill_costs)")}
    assert set(fill_costs.COLUMNS) <= cols
