"""The on-chain cashflow beside the books for closed LIVE sol positions (kaiba/ops/live_cashflow.py).

Every linked order must be known and terminal; every filled leg must bind order/position/token/lane/side
and its decoder row (identity, finalized signature == tx_hash, wallet_bound, the decoder's own "lamport"
unit, signed token delta); a position needs a buy and a sell, no shared transaction and exact token
closure; deltas are summed once; books and chain are compared on the same positions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.ops import live_cashflow as LC

NOW = 1_793_800_000_000
TOKEN, LANE = "TOK", "sm-trenches"
DETAIL = json.dumps({"wallet_bound": True, "native_unit": "lamport"})


def leg(pid, oid, side, delta, *, tokens=None, sig=None, basis="venue_event", detail=DETAIL, state="filled",
        order_side=None, tx=None, fc_side=None, mode="live", fc_token=TOKEN, order_token=TOKEN, with_fc=True):
    sig = sig or f"sig-{oid}"
    tokens = tokens if tokens is not None else (1000 if side == "buy" else -1000)
    link = {"position_id": pid, "order_id": oid, "side": side}
    order = {"order_id": oid, "chain": "sol", "token": order_token, "lane": LANE, "side": order_side or side,
             "mode": mode, "state": state, "tx_hash": tx if tx is not None else sig}
    fc = None
    if with_fc:
        fc = {"order_id": oid, "chain": "sol", "token": fc_token, "lane": LANE, "side": fc_side or side,
              "signature": sig, "basis": basis, "wallet_native_delta": str(delta), "wallet_token_delta": str(tokens),
              "detail_json": detail,
              # fee columns exist on real rows; they must NEVER be added to the wallet delta
              "router_fee_native": "999999", "network_fee_native": "5000"}
    return link, order, fc


def world(*spec):
    positions, links, orders, fcs = [], {}, {}, {}
    for pid, cost, books, legs in spec:
        positions.append({"position_id": pid, "token": TOKEN, "lane": LANE, "cost_native": str(cost),
                          "realized_native": str(books)})
        for link, order, fc in legs:
            links.setdefault(pid, []).append(link)
            if order is not None:
                orders[order["order_id"]] = order
            if fc is not None:
                fcs[fc["order_id"]] = fc
    return positions, links, orders, fcs


def rt(pid, *, buy=-102_000_000, sell=78_000_000, **kw):
    return [leg(pid, f"b-{pid}", "buy", buy), leg(pid, f"s-{pid}", "sell", sell, **kw)]


def test_a_bound_round_trip_is_covered_and_summed_once():
    rep = LC.assess(*world(("p1", 100_000_000, -20_000_000, rt("p1"))))
    assert rep["covered"] == 1 and rep["not_covered"] == {}
    assert rep["chain_same_cohort"]["net_native"] == -24_000_000  # deltas only; fee columns never added
    assert rep["books_same_cohort"]["net_native"] == -20_000_000
    assert rep["books_same_cohort"]["mean_pct"] == pytest.approx(-20.0)
    assert rep["chain_same_cohort"]["mean_pct"] == pytest.approx(-24_000_000 * 100 / 102_000_000, abs=1e-4)


@pytest.mark.parametrize("bad, reason", [
    (dict(basis="no_venue_event"), "leg_basis:no_venue_event"),
    (dict(basis="fetch_failed"), "leg_basis:fetch_failed"),
    (dict(tx="other-signature"), "signature_unbound"),
    (dict(detail=json.dumps({"wallet_bound": False, "native_unit": "lamport"})), "wallet_unbound"),
    (dict(detail=json.dumps({"wallet_bound": True, "native_unit": "lamports"})), "native_unit_mismatch"),
    (dict(detail="not json"), "wallet_unbound"),
    (dict(detail=None), "wallet_unbound"),
    (dict(order_side="buy"), "side_mismatch"),
    (dict(fc_side="buy"), "decoder_order_identity_mismatch"),
    (dict(fc_token="OTHER"), "decoder_order_identity_mismatch"),
    (dict(order_token="OTHER"), "order_position_identity_mismatch"),
    (dict(mode="shadow"), "order_not_live_sol"),
    (dict(tokens=500), "token_delta_sign_contradicts_side"),
    (dict(tokens="--1"), "wallet_token_delta_unparseable"),
    (dict(with_fc=False), "no_fill_costs_row"),
])
def test_an_unbound_leg_excludes_the_position_and_is_counted(bad, reason):
    rep = LC.assess(*world(("p1", 100, -10, rt("p1", **bad))))
    assert rep["covered"] == 0
    assert rep["not_covered"] == {"incomplete_legs": 1} and rep["leg_problems"] == {reason: 1}


def test_malformed_numbers_never_raise():
    assert LC._int("--1") is None and LC._int("1.5") is None and LC._int(True) is None and LC._int(None) is None
    assert LC._int("-0") is None and LC._int("-7") == -7 and LC._int(12) == 12 and LC._int("012") is None
    rep = LC.assess(*world(("p1", "abc", -10, rt("p1"))))
    assert rep["not_covered"] == {"book_or_chain_basis_unusable": 1}


def test_a_missing_or_unresolved_linked_order_makes_the_position_unknown():
    link, _, fc = leg("p1", "s-p1", "sell", 90)
    rep = LC.assess(*world(("p1", 100, -10, [leg("p1", "b-p1", "buy", -100), (link, None, fc)])))
    assert rep["not_covered"] == {"unknown:linked_order_missing": 1}
    for state in ("submitted", "unknown", "partial", "pending"):
        rep = LC.assess(*world(("p1", 100, -10, rt("p1") + [leg("p1", "x", "sell", 0, state=state)])))
        assert rep["not_covered"] == {"unknown:unresolved_linked_order": 1}, state


def test_terminal_non_fills_are_disclosed_never_legs():
    rep = LC.assess(*world(("p1", 100_000_000, -20_000_000,
                            rt("p1") + [leg("p1", "x", "sell", 0, state="failed", with_fc=False)])))
    assert rep["covered"] == 1 and rep["terminal_nonfill_orders_linked"] == 1


def test_buy_and_sell_are_both_required():
    rep = LC.assess(*world(("p1", 100, -10, [leg("p1", "b1", "buy", -100)])))
    assert rep["not_covered"] == {"missing_buy_or_sell": 1}


def test_inventory_that_did_not_close_is_partial_not_a_closed_return():
    rep = LC.assess(*world(("p1", 100_000_000, -20_000_000,
                            [leg("p1", "b1", "buy", -100_000_000, tokens=1000),
                             leg("p1", "s1", "sell", 80_000_000, tokens=-990)])))
    assert rep["covered"] == 0 and rep["not_covered"] == {"partial_inventory": 1}
    assert rep["partial_inventory"]["n"] == 1


def test_a_shared_transaction_is_found_by_tx_hash_even_without_a_decoder_row():
    rep = LC.assess(*world(
        ("p1", 100, -10, [leg("p1", "b1", "buy", -100, sig="S"), leg("p1", "s1", "sell", 90)]),
        # p2's buy reuses p1's transaction and has NO decoder row at all
        ("p2", 100, -10, [leg("p2", "b2", "buy", -100, sig="S", with_fc=False), leg("p2", "s2", "sell", 90)]),
        ("p3", 100, -10, rt("p3", buy=-100, sell=90)),
    ))
    assert rep["covered"] == 1
    assert rep["not_covered"] == {"incomplete_legs": 1, "shared_transaction": 1}
    assert "every order linked" in rep["duplicate_scope"]


def test_the_line_shows_the_same_cohort_and_never_calls_the_gap_a_fee():
    rep = LC.assess(*world(("p1", 100_000_000, -20_000_000, rt("p1")),
                           ("p2", 100, -10, [leg("p2", "b2", "buy", -100)])))
    line = LC.render_lines("7d ", rep)[0]
    assert "same 1 of 2 closed" in line and "not covered 1" in line
    assert "books mean -20.0%" in line and "fee" not in line.lower()
    assert LC.render_lines("7d ", {"status": "not_applicable"}) == []


def test_an_empty_covered_cohort_has_no_net_figure():
    """A sum over zero positions is not a zero P&L: net is None / n/a, never 0."""
    rep = LC.assess(*world(("p1", 100, -10, [leg("p1", "b1", "buy", -100)])))
    assert rep["covered"] == 0
    assert rep["books_same_cohort"]["net_native"] is None and rep["chain_same_cohort"]["net_native"] is None
    assert rep["native_difference_same_positions"] is None and rep["partial_inventory"]["chain_net_native"] is None
    line = LC.render_lines("7d ", rep)[0]
    assert "net n/a SOL" in line and "+0.0000" not in line
    assert LC.assess([], {}, {}, {})["chain_same_cohort"]["net_native"] is None


def _order(oid, state="filled", side="buy", tx="sig"):
    return {"order_id": oid, "chain": "sol", "token": TOKEN, "lane": LANE, "side": side, "mode": "live",
            "state": state, "tx_hash": tx}


def test_failed_and_unlinked_live_orders_are_disclosed_never_summed_into_a_figure():
    window = [_order("linked-fill", tx="t1"),
              _order("unlinked-bound", tx="t2"),
              _order("unlinked-wrong-unit", tx="t3"),
              _order("unlinked-no-row", tx="t4"),
              _order("failed-landed", state="failed", side="sell", tx="t5"),
              _order("failed-linked-nohash", state="cancelled", tx=None),
              _order("unlinked-pending", state="submitted", tx=None)]
    fcs = {"unlinked-bound": leg("x", "unlinked-bound", "buy", -50, sig="t2")[2],
           "unlinked-wrong-unit": leg("x", "unlinked-wrong-unit", "buy", -70, sig="t3",
                                      detail=json.dumps({"wallet_bound": True, "native_unit": "lamports"}))[2]}
    d = LC.disclose(window, {"linked-fill", "failed-linked-nohash"}, fcs)
    assert d["orders_in_window"] == 7
    assert d["failed"] == {**d["failed"], "n": 2, "with_tx_hash": 1, "linked_to_a_position": 1}
    u = d["unlinked"]
    assert u["n"] == 5 and u["filled"] == 3 and u["filled_decoded_bound"] == 1
    assert u["by_state"] == {"failed": 1, "filled": 3, "submitted": 1}
    # counts only: two unlinked orders may share one transaction, so no sum is ever shown
    assert u["cashflow_native"] == "unknown" and not any("net" in k for k in u)
    twins = [_order("u1", tx="same"), _order("u2", tx="same")]
    rows = {o: leg("x", o, "buy", -50, sig="same")[2] for o in ("u1", "u2")}
    assert LC.disclose(twins, set(), rows)["unlinked"]["cashflow_native"] == "unknown"


def test_a_real_decoder_row_binds_as_a_buy_leg():
    """The buy leg is a real compute_fill_cost output (observed RPC capture fixture); the sell is synthetic."""
    from kaiba.execution import fill_costs

    w = json.loads((Path(__file__).parent / "fixtures/fill_costs/ord_48e208c580717ec2f1d0c057_transaction.json")
                   .read_text(encoding="utf-8"))
    order = {**w["order"], "chain": "sol", "lane": LANE, "amount_in": "1000000000", "min_out": "1",
             "slippage_bps": 0, "filled_out": "0", "mode": "live", "state": "filled"}
    row = fill_costs.compute_fill_cost(order, w["result"], None)
    assert json.loads(row["detail_json"])["native_unit"] == LC.NATIVE_UNIT
    pos = {"position_id": "p1", "token": order["token"], "lane": LANE, "cost_native": "1072486472",
           "realized_native": "-100000000"}
    sell_order = {"order_id": "sell-synthetic", "chain": "sol", "token": order["token"], "lane": LANE,
                  "side": "sell", "mode": "live", "state": "filled", "tx_hash": "synthetic-sell-signature"}
    sell_fc = {"order_id": "sell-synthetic", "chain": "sol", "token": order["token"], "lane": LANE, "side": "sell",
               "signature": "synthetic-sell-signature", "basis": "venue_event",
               "wallet_native_delta": "970000000", "wallet_token_delta": str(-int(row["wallet_token_delta"])),
               "detail_json": DETAIL}
    rep = LC.assess([pos], {"p1": [{"order_id": order["order_id"], "side": "buy"},
                                   {"order_id": "sell-synthetic", "side": "sell"}]},
                    {order["order_id"]: order, "sell-synthetic": sell_order},
                    {order["order_id"]: row, "sell-synthetic": sell_fc})
    assert rep["leg_problems"] == {} and rep["covered"] == 1
    assert rep["chain_same_cohort"]["net_native"] == int(row["wallet_native_delta"]) + 970_000_000


def _insert(conn, *, pid, closed_ms, cost, books, legs):
    conn.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, cost_native, "
                 "realized_native) VALUES (?, 'sol', ?, ?, 'live', ?, ?, ?, ?)",
                 (pid, TOKEN, LANE, closed_ms - 60_000, closed_ms, str(cost), str(books)))
    for oid, side, delta, tokens, sig in legs:
        conn.execute("INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, amount_in, "
                     "min_out, slippage_bps, state, provider, tx_hash, created_ms, updated_ms) VALUES "
                     "(?, 'sol', ?, ?, ?, 'live', 'a', 'b', '1', '1', 100, 'filled', 'gmgn', ?, ?, ?)",
                     (oid, TOKEN, side, LANE, sig, closed_ms, closed_ms))
        conn.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?, ?, ?, ?)",
                     (pid, oid, side, closed_ms))
        conn.execute("INSERT INTO fill_costs (order_id, chain, token, lane, side, signature, basis, wallet_native_delta, "
                     "wallet_token_delta, detail_json) VALUES (?, 'sol', ?, ?, ?, ?, 'venue_event', ?, ?, ?)",
                     (oid, TOKEN, LANE, side, sig, str(delta), str(tokens), DETAIL))
    conn.commit()


def test_report_reads_by_key_in_the_window(tmp_db):
    _insert(tmp_db, pid="in", closed_ms=NOW - 3_600_000, cost=100, books=-10,
            legs=[("o1", "buy", -102, 5, "s1"), ("o2", "sell", 88, -5, "s2")])
    _insert(tmp_db, pid="old", closed_ms=NOW - 9 * 86_400_000, cost=100, books=-10,
            legs=[("o3", "buy", -100, 5, "s3"), ("o4", "sell", 80, -5, "s4")])
    # an unlinked live buy in the window, decoded and bound: disclosed, in neither figure
    tmp_db.execute("INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, amount_in, "
                   "min_out, slippage_bps, state, provider, tx_hash, created_ms, updated_ms) VALUES "
                   "('lone', 'sol', ?, 'buy', ?, 'live', 'a', 'b', '1', '1', 100, 'filled', 'gmgn', 'sl', ?, ?)",
                   (TOKEN, LANE, NOW - 60_000, NOW - 60_000))
    tmp_db.execute("INSERT INTO fill_costs (order_id, chain, token, lane, side, signature, basis, wallet_native_delta, "
                   "wallet_token_delta, detail_json) VALUES ('lone', 'sol', ?, ?, 'buy', 'sl', 'venue_event', '-33', "
                   "'7', ?)", (TOKEN, LANE, DETAIL))
    tmp_db.commit()
    rep = LC.report(tmp_db, days=7, chain="sol", now=NOW)
    assert rep["closed_positions"] == 1 and rep["covered"] == 1
    assert rep["chain_same_cohort"]["net_native"] == -14 and rep["books_same_cohort"]["net_native"] == -10
    fu = rep["failed_and_unlinked"]
    assert fu["orders_in_window"] == 3 and fu["unlinked"]["n"] == 1 and fu["unlinked"]["filled_decoded_bound"] == 1
    assert fu["unlinked"]["cashflow_native"] == "unknown"
    assert rep["reads"]["queries"] >= 6 and rep["reads"]["wal_waits"] == 0
    assert "network fees of failed transactions that landed (not measured)" in rep["scope"]["in_neither_figure"]
    assert LC.report(tmp_db, chain="robinhood", now=NOW)["status"] == "not_applicable"


def test_every_chunk_is_wal_admitted_on_its_own_closed_connection(tmp_db, monkeypatch):
    """AGENTS.md read rule: WAL over 1.5 GB waits 60 s and re-checks before EACH chunk; a fresh read-only
    connection per chunk, closed before the next; three over-limit checks defer the section."""
    import sqlite3

    from kaiba.learning import autolearn_io

    _insert(tmp_db, pid="in", closed_ms=NOW - 3_600_000, cost=100, books=-10,
            legs=[("o1", "buy", -102, 5, "s1"), ("o2", "sell", 88, -5, "s2")])
    path = LC._db_path(tmp_db, None)
    real_connect = sqlite3.connect
    opened = []

    def counting_connect(*a, **kw):
        c = real_connect(*a, **kw)
        opened.append(c)
        return c

    monkeypatch.setattr(autolearn_io.sqlite3, "connect", counting_connect)
    reader = autolearn_io.GuardedReader(path, query_seconds=5)
    wal = iter([2_000_000_000, 2_000_000_000])  # the first chunk waits twice, then the WAL is small
    monkeypatch.setattr(reader, "_wal_bytes", lambda: next(wal, 0))
    slept = []
    rep = LC.report(tmp_db, days=7, now=NOW, reader=reader, sleep=slept.append)
    assert rep["covered"] == 1 and slept == [60.0, 60.0]
    assert len(opened) == rep["reads"]["queries"] >= 6  # one connection per chunk
    for c in opened:  # every one closed
        with pytest.raises(sqlite3.ProgrammingError):
            c.execute("SELECT 1")

    stuck = autolearn_io.GuardedReader(path, query_seconds=5)
    monkeypatch.setattr(stuck, "_wal_bytes", lambda: 2_000_000_000)
    slept.clear()
    with pytest.raises(autolearn_io.SourceDeferred):
        LC.report(tmp_db, days=7, now=NOW, reader=stuck, sleep=slept.append)
    assert slept == [60.0, 60.0] and stuck.queries == 0  # deferred before any read


def test_cli_prints_aggregates_only(tmp_db, capsys):
    _insert(tmp_db, pid="POSITION-ID-MUST-NOT-PRINT", closed_ms=int(__import__("time").time() * 1000) - 3_600_000,
            cost=100, books=-10, legs=[("ORDER-ID-MUST-NOT-PRINT", "buy", -102, 5, "SIGNATURE-MUST-NOT-PRINT"),
                                       ("o2", "sell", 88, -5, "s2")])
    assert LC.main(["--db", str(LC._db_path(tmp_db, None)), "--days", "7", "--days", "365", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data) == {"7d", "365d"} and data["7d"]["covered"] == 1 and data["365d"]["days"] == 365
    assert "MUST-NOT-PRINT" not in out
    assert LC.main(["--db", str(LC._db_path(tmp_db, None))]) == 0
    text = capsys.readouterr().out
    assert "7d on-chain, same 1 of 1 closed" in text and "in neither figure" in text and "MUST-NOT" not in text
    assert "cashflow unknown, not summed" in text


def test_daily_report_adds_the_line_for_sol_only(tmp_db):
    from kaiba.ops import daily_report

    _insert(tmp_db, pid="in", closed_ms=NOW - 3_600_000, cost=100_000_000, books=-10_000_000,
            legs=[("o1", "buy", -102_000_000, 5, "s1"), ("o2", "sell", 88_000_000, -5, "s2")])
    sol = daily_report.render(daily_report.build(tmp_db, chain="sol", now=NOW))
    assert "on-chain, same 1 of 1 closed" in sol
    rh = daily_report.render(daily_report.build(tmp_db, chain="robinhood", now=NOW))
    assert "on-chain" not in rh
