"""Actual migrated-database capture, with no network or financial writers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.learning import economic_capture as capture
from kaiba.learning.autolearn_io import WAL_LIMIT, GuardedReader, SourceDeferred
from kaiba.learning.economic_outcomes import project_episode

NOW = 1_800_000_000_000


def _put(conn, table, row):
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))


def _reader(conn, **kwargs):
    return GuardedReader(Path(conn.execute("PRAGMA database_list").fetchone()[2]), **kwargs)


def _position(conn, pid="p1", *, chain="sol", qty="0", opened=None):
    cost = str(2**75)
    row = {"position_id": pid, "chain": chain, "token": "token", "lane": "sm-trenches", "mode": "live",
           "opened_ms": NOW - 1000 if opened is None else opened, "closed_ms": NOW - 100 if qty == "0" else None,
           "qty": qty, "qty_total": "100", "cost_native": cost, "proceeds_native": str(2**75 - 123),
           "realized_native": "-123"}
    _put(conn, "positions", row)
    return row


def _order(conn, oid="buy", *, chain="sol", side="buy", state="filled", token="token", mode="live"):
    row = {"order_id": oid, "chain": chain, "token": token, "side": side, "lane": "sm-trenches", "mode": mode,
           "input_token": "native" if side == "buy" else token, "output_token": token if side == "buy" else "native",
           "amount_in": str(2**75) if side == "buy" else "100", "min_out": "1", "slippage_bps": 100,
           "state": state, "provider": "gmgn", "tx_hash": "tx-" + oid, "filled_out": "100",
           "created_ms": NOW - 900, "updated_ms": NOW - 200}
    _put(conn, "orders", row)
    return row


def _applied(conn, pid, oid, *, chain="sol", side="buy"):
    row = _order(conn, oid, chain=chain, side=side)
    _put(conn, "position_orders", {"position_id": pid, "order_id": oid, "side": side, "ts_ms": NOW - 200})
    return row


def _event(conn, pid, oid, *, suffix="100:1", chain="sol", token="token", body_changes=None):
    body = {"service": "watchdog", "event": "exit_timing", "position_id": pid, "order_id": oid,
            "mode": "live", "lane": "sm-trenches", "outcome": "failed", "order_state": "failed"}
    body.update(body_changes or {})
    _put(conn, "events", {"ts_ms": NOW - 300, "kind": "system", "chain": chain, "subject": token,
                         "payload": json.dumps(body), "dedupe_key": f"exit_timing:{pid}:{suffix}"})


def _packet(conn, pid="p1", **kwargs):
    return capture.capture_positions(_reader(conn), [pid], now_ms=NOW, **kwargs)["packets"][0]


@pytest.mark.parametrize("chain", ["sol", "bsc", "robinhood"])
def test_existing_all_chain_rows_are_joined_without_certifying_economic_profit(tmp_db, chain):
    position = _position(tmp_db, chain=chain)
    _applied(tmp_db, "p1", "buy", chain=chain)
    _put(tmp_db, "trades", {"trade_id": "trade", "position_id": "p1", "chain": chain, "token": "token",
        "lane": "sm-trenches", "mode": "live", "opened_ms": NOW - 1000, "closed_ms": NOW - 100,
        "hold_s": 1, "cost_native": position["cost_native"], "proceeds_native": position["proceeds_native"],
        "pnl_native": "-123", "pnl_pct": -1, "fees_native": "200"})
    _put(tmp_db, "fill_costs", {"order_id": "buy", "chain": chain, "signature": "tx-buy", "version": "fill-costs-v1",
        "basis": "venue_event", "wallet_native_delta": "-" + position["cost_native"], "reconciled": 1,
        "detail_json": '{"wallet_bound":false,"notes":["source qualifier retained"]}', "computed_ms": NOW - 10})
    _put(tmp_db, "fill_prices", {"order_id": "buy", "chain": chain, "token": "token", "side": "buy",
        "fill_ts_ms": NOW - 200, "fill_ts_basis": "order_updated", "native_atoms": position["cost_native"],
        "token_atoms": "100", "decimals_basis": "unavailable", "basis": "fill_ratio_native_only", "computed_ms": NOW - 10})
    _put(tmp_db, "fill_reconciliations", {"order_id": "buy", "chain": chain, "signature": "tx-buy",
        "wallet": "observed-wallet", "token": "token", "side": "buy", "verdict": "agree", "checked_ms": NOW - 5,
        "onchain_native_all_in": position["cost_native"], "receipt_json": '{"basis":"provider_reported"}'})
    before = tmp_db.total_changes
    packet = _packet(tmp_db)
    assert tmp_db.total_changes == before
    assert packet["position"]["cost_native"] == str(2**75)
    assert packet["fill_costs"][0]["detail_json"] == '{"wallet_bound":false,"notes":["source qualifier retained"]}'
    assert packet["fill_prices"][0]["native_atoms"] == str(2**75)
    assert packet["fill_reconciliations"][0]["wallet"] == "observed-wallet"
    assert packet["trades"][0]["fees_native"] == "200"
    assert packet["identity"]["verified"] is False and "wallet" not in packet["identity"]
    assert all(value is False for value in packet["coverage"].values())
    projected = project_episode(packet)
    assert projected["ledger_pnl_native"] == "-123" and projected["ledger_cost_native"] == str(2**75)
    assert projected["economic_pnl_native"] is None and projected["verdict"] == "UNPROVEN"
    assert projected["decoded_fill_evidence"][0]["reported_reconciled"] == 1


def test_future_row_arrival_and_link_clocks_are_explicit_not_as_of_evidence(tmp_db):
    position = _position(tmp_db)
    _applied(tmp_db, "p1", "buy")
    tmp_db.execute("UPDATE position_orders SET ts_ms=?", (NOW + 1,))
    tmp_db.execute("UPDATE orders SET updated_ms=?", (NOW + 2,))
    _put(tmp_db, "fill_costs", {"order_id": "buy", "chain": "sol", "signature": "tx-buy", "computed_ms": NOW + 3,
                              "network_fee_native": "42", "basis": "venue_event"})
    _put(tmp_db, "fill_reconciliations", {"order_id": "buy", "chain": "sol", "signature": "tx-buy", "wallet": "wallet",
        "token": "token", "side": "buy", "verdict": "agree", "checked_ms": NOW + 4})
    _put(tmp_db, "trades", {"trade_id": "future-trade", "position_id": "p1", "chain": "sol", "token": "token",
        "lane": "sm-trenches", "mode": "live", "opened_ms": NOW - 1000, "closed_ms": NOW + 5,
        "hold_s": 1, "cost_native": position["cost_native"], "proceeds_native": position["proceeds_native"],
        "pnl_native": "-123", "pnl_pct": -1})
    _put(tmp_db, "fill_prices", {"order_id": "buy", "chain": "sol", "token": "token", "side": "buy",
        "fill_ts_ms": NOW - 200, "fill_ts_basis": "order_updated", "native_atoms": position["cost_native"],
        "token_atoms": "100", "decimals_basis": "unavailable", "basis": "fill_ratio_native_only", "computed_ms": NOW + 6})
    packet = _packet(tmp_db)
    expected = {"position_order_links": "ts_ms", "orders": "updated_ms", "fill_costs": "computed_ms",
                "fill_reconciliations": "checked_ms", "trades": "closed_ms", "fill_prices": "computed_ms"}
    for section, field in expected.items():
        row = packet[section][0]
        assert row[field] > NOW
        assert row["_capture_qualifications"]["available_at_frontier"] is False
        assert field in row["_capture_qualifications"]["future_timestamps"]
    assert packet["orders"][0]["_capture_qualifications"]["position_link_available_at_frontier"] is False
    assert packet["fill_costs"][0]["network_fee_native"] == "42"
    assert packet["capture_coverage"]["status"] == "partial"
    assert project_episode(packet)["verdict"] == "UNPROVEN"
    later = capture.capture_positions(_reader(tmp_db), ["p1"], now_ms=NOW + 10)["packets"][0]
    assert later["source_hash"] != packet["source_hash"]
    assert all("_capture_qualifications" not in later[section][0] for section in expected)


@pytest.mark.parametrize("change,field", [({"chain": "bsc"}, "chain"), ({"token": "foreign"}, "token"),
    ({"mode": "shadow"}, "mode"), ({"side": "sell"}, "side")])
def test_applied_order_identity_contradictions_are_preserved_and_qualified(tmp_db, change, field):
    _position(tmp_db)
    _order(tmp_db, "applied", **change)
    _put(tmp_db, "position_orders", {"position_id": "p1", "order_id": "applied", "side": "buy", "ts_ms": NOW - 100})
    packet = _packet(tmp_db)
    row = packet["orders"][0]
    assert row[field] == change[field]
    assert field in row["_capture_qualifications"]["identity_mismatches"]
    assert row["_capture_qualifications"]["position_identity_verified"] is False
    assert packet["capture_coverage"]["status"] == "partial"
    assert packet["capture_coverage"]["sections"]["referenced_orders"]["identity_mismatch_rows"] == 1


def test_cost_row_identity_contradictions_are_not_hidden_by_its_order_id(tmp_db):
    _position(tmp_db)
    _applied(tmp_db, "p1", "buy")
    _put(tmp_db, "fill_costs", {"order_id": "buy", "chain": "bsc", "token": "foreign", "side": "sell",
                              "signature": "another-tx", "network_fee_native": "42"})
    packet = _packet(tmp_db)
    row = packet["fill_costs"][0]
    assert row["network_fee_native"] == "42" and row["chain"] == "bsc"
    assert set(row["_capture_qualifications"]["identity_mismatches"]) == {"chain", "token", "side", "signature"}
    assert packet["capture_coverage"]["status"] == "partial"


def test_rereading_explicit_closed_id_collects_late_cost_and_changes_stable_source_hash(tmp_db):
    _position(tmp_db)
    _applied(tmp_db, "p1", "buy")
    first = _packet(tmp_db)
    repeated = capture.capture_positions(_reader(tmp_db), ["p1"], now_ms=NOW + 1000)["packets"][0]
    assert first["source_hash"] == repeated["source_hash"]
    assert first["fill_costs"] == []
    _put(tmp_db, "fill_costs", {"order_id": "buy", "chain": "sol", "signature": "tx-buy", "basis": "venue_event",
                              "network_fee_native": "42", "computed_ms": NOW + 500})
    later = capture.capture_positions(_reader(tmp_db), ["p1"], now_ms=NOW + 1000)["packets"][0]
    assert later["fill_costs"][0]["network_fee_native"] == "42"
    assert later["source_hash"] != first["source_hash"]
    assert project_episode(later)["verdict"] == "UNPROVEN"


def test_exact_watchdog_and_dedupe_event_links_capture_failed_and_unknown_exits(tmp_db):
    _position(tmp_db)
    _applied(tmp_db, "p1", "buy")
    _order(tmp_db, "failed", side="sell", state="failed")
    _order(tmp_db, "unknown", side="sell", state="unknown")
    _order(tmp_db, "nearby", side="sell", state="failed")
    _put(tmp_db, "watchdog_state", {"position_id": "p1", "exit_order_id": "unknown", "updated_ms": NOW - 50})
    _event(tmp_db, "p1", "failed")
    _put(tmp_db, "order_events", {"order_id": "failed", "ts_ms": NOW - 200, "state": "failed",
                                  "detail": "refused-looking complaint is not proof of no submission"})
    _put(tmp_db, "fill_costs", {"order_id": "failed", "chain": "sol", "signature": "tx-failed", "basis": "decode_refused"})
    packet = _packet(tmp_db)
    assert {o["order_id"] for o in packet["orders"]} == {"buy", "failed", "unknown"}
    assert {a["order_id"]: a["state"] for a in packet["attempts"]} == {"failed": "failed", "unknown": "unknown"}
    assert all(a["position_id"] == "p1" and a["mapping_proof"] for a in packet["attempts"])
    assert all("no_submission_proven" not in a for a in packet["attempts"])
    assert packet["order_events"][0]["detail"].startswith("refused-looking")
    assert packet["fill_costs"][0]["basis"] == "decode_refused"
    assert "failed_attempt_cost_missing" in project_episode(packet)["unknown_reasons"]


@pytest.mark.parametrize("change", [{"position_id": "other"}, {"service": "foreign"}, {"event": "other"},
                                   {"mode": "shadow"}, {"lane": "manual"}])
def test_event_prefix_without_matching_payload_never_authorizes_attempt_link(tmp_db, change):
    _position(tmp_db)
    _order(tmp_db, "failed", side="sell", state="failed")
    _event(tmp_db, "p1", "failed", body_changes=change)
    packet = _packet(tmp_db)
    assert packet["orders"] == []
    assert packet["attempts"] == []
    assert packet["capture_coverage"]["rejected_attempt_link_counts"]["exit_timing_identity_or_payload_unproved"] == 1


@pytest.mark.parametrize("change", [{"chain": "bsc"}, {"token": "other"}, {"mode": "shadow"}, {"side": "buy"}])
def test_exact_reference_cannot_attach_order_from_another_identity(tmp_db, change):
    _position(tmp_db)
    values = {"oid": "foreign", "side": "sell", "state": "failed", **change}
    _order(tmp_db, **values)
    _event(tmp_db, "p1", "foreign")
    packet = _packet(tmp_db)
    assert packet["orders"] == []
    assert packet["attempts"] == []
    assert packet["referenced_orders"][0]["order_id"] == "foreign"
    assert packet["capture_coverage"]["rejected_attempt_link_counts"]["attempt_order_identity_or_position_conflict"] == 1


def test_order_already_applied_to_another_position_cannot_be_borrowed_by_watchdog(tmp_db):
    _position(tmp_db)
    _position(tmp_db, "other")
    _applied(tmp_db, "other", "foreign", side="sell")
    _put(tmp_db, "watchdog_state", {"position_id": "p1", "exit_order_id": "foreign", "updated_ms": NOW - 50})
    packet = _packet(tmp_db)
    assert packet["orders"] == []
    assert packet["attempts"] == []
    assert packet["foreign_position_links"][0]["position_id"] == "other"


def test_open_inventory_retains_ledger_quantity_without_inventing_custody_or_exit(tmp_db):
    _position(tmp_db, qty="50", opened=NOW - 14 * capture.DAY_MS)
    _applied(tmp_db, "p1", "buy")
    _order(tmp_db, "old-attempt", side="sell", state="failed")
    _event(tmp_db, "p1", "old-attempt")
    tmp_db.execute("UPDATE events SET ts_ms=?", (NOW - 14 * capture.DAY_MS,))
    packet = _packet(tmp_db)
    assert packet["inventory"]["qty_remaining"] == "50"
    assert packet["inventory"]["opening_quantity"] is None
    assert packet["inventory"]["quantity_reconciled"] is False
    assert packet["inventory"]["transfers_complete"] is False and "quote" not in packet["inventory"]
    assert packet["capture_coverage"]["history_clipped"] is True
    assert packet["attempts"] == []
    result = project_episode(packet)
    assert result["inventory"]["liquidation_value_native"] is None
    assert "executable_inventory_value_missing" in result["unknown_reasons"]


def test_order_row_cap_is_explicit_and_never_turns_into_full_population_coverage(tmp_db):
    _position(tmp_db)
    for n in range(capture.ORDER_LIMIT + 1):
        _applied(tmp_db, "p1", f"buy-{n:03}")
    packet = _packet(tmp_db)
    section = packet["capture_coverage"]["sections"]["position_order_links"]
    assert section["truncated"] is True and section["observed_rows_lower_bound"] == capture.ORDER_LIMIT + 1
    assert len(packet["position_order_links"]) <= capture.ORDER_LIMIT
    assert packet["coverage"]["orders_complete"] is False
    assert packet["capture_coverage"]["status"] == "partial"


def test_sql_bounds_oversized_text_before_it_can_authorize_an_event_link(tmp_db):
    _position(tmp_db)
    _order(tmp_db, "failed", side="sell", state="failed")
    _event(tmp_db, "p1", "failed", body_changes={"padding": "x" * (capture.TEXT_LIMIT + 1)})
    packet = _packet(tmp_db)
    row = packet["exit_timing_events"][0]
    assert row["payload"] is None
    assert row["_field_qualifications"]["payload"]["status"] == "oversized_unavailable"
    assert packet["attempts"] == []
    assert packet["capture_coverage"]["sections"]["exit_timing_events"]["oversized_fields"] == 1


def test_total_packet_budget_marks_dropped_raw_rows_instead_of_silently_losing_them(tmp_db):
    _position(tmp_db)
    _applied(tmp_db, "p1", "buy")
    for n in range(80):
        _put(tmp_db, "order_events", {"order_id": "buy", "ts_ms": NOW - 200, "state": "filled",
                                      "detail": str(n) + "x" * 3000})
    packet = _packet(tmp_db)
    assert len(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()) <= capture.MAX_PACKET_BYTES
    section = packet["capture_coverage"]["sections"]["order_events"]
    assert section["packet_budget_truncated"] is True
    assert section["observed_rows_lower_bound"] == 80 and section["retained_rows"] < 80
    assert packet["capture_coverage"]["status"] == "partial"


def test_missing_and_duplicate_ids_are_explicit_and_repeat_does_not_duplicate_packet(tmp_db):
    _position(tmp_db)
    report = capture.capture_positions(_reader(tmp_db), ["p1", "missing", "p1"], now_ms=NOW)
    assert len(report["packets"]) == 1 and report["missing_position_ids"] == ["missing"]
    assert report["capture_coverage"]["requested"] == 2


def test_capture_is_read_only_and_uses_indexed_exact_event_prefix_without_money_cast(tmp_db):
    _position(tmp_db)
    sqls = []
    reader = _reader(tmp_db)
    original = reader.rows
    reader.rows = lambda sql, args=(): (sqls.append((sql, args)), original(sql, args))[1]
    before = tmp_db.total_changes
    capture.capture_positions(reader, ["p1"], now_ms=NOW)
    assert tmp_db.total_changes == before
    assert not any("CAST(" in sql.upper() for sql, _ in sqls)
    event_sql, args = next((sql, args) for sql, args in sqls if "FROM events WHERE" in sql)
    plan = original("EXPLAIN QUERY PLAN " + event_sql, args)
    assert any("idx_events_dedupe" in row["detail"] for row in plan)
    assert "exit_timing:p1:" in args and "exit_timing:p1;" in args


def test_large_event_population_uses_native_index_order_without_case_alias_sort(tmp_db):
    _position(tmp_db)
    _order(tmp_db, "failed", side="sell", state="failed")
    body = json.dumps({"service": "watchdog", "event": "exit_timing", "position_id": "p1", "order_id": "failed",
                       "mode": "live", "lane": "sm-trenches"})
    related = [(NOW - 300, "system", "sol", "token", body, f"exit_timing:p1:{NOW - 100_000 + n}:1")
               for n in range(10_000)]
    unrelated = [(NOW - 300, "system", "sol", "other", "{}", f"exit_timing:unrelated-{n}:1:1")
                 for n in range(30_000)]
    tmp_db.executemany("INSERT INTO events(ts_ms,kind,chain,subject,payload,dedupe_key) VALUES(?,?,?,?,?,?)",
                       related + unrelated)
    reader, observed = _reader(tmp_db), []
    original = reader.rows
    def rows(sql, args=()):
        observed.append((sql, args))
        return original(sql, args)
    reader.rows = rows
    packet = capture.capture_positions(reader, ["p1"], now_ms=NOW)["packets"][0]
    event_sql, args = next((sql, args) for sql, args in observed if "FROM events WHERE" in sql)
    plan = original("EXPLAIN QUERY PLAN " + event_sql, args)
    assert any("idx_events_dedupe" in row["detail"] for row in plan)
    assert all("TEMP B-TREE" not in row["detail"] for row in plan)
    # Matching the index name alone missed the regression: sorting the protected
    # CASE output alias forced SQLite to visit/sort the entire matching prefix.
    event_rows = packet["exit_timing_events"]
    assert event_rows and all(row["dedupe_key"].startswith("exit_timing:p1:") for row in event_rows)
    assert [row["id"] for row in event_rows] == sorted(row["id"] for row in event_rows)
    assert [row["order_id"] for row in packet["attempts"]] == ["failed"]
    assert packet["capture_coverage"]["sections"]["exit_timing_events"]["truncated"] is True


def test_native_event_order_preserves_oversized_key_qualification_without_sorting_null_alias_first(tmp_db):
    _position(tmp_db)
    _order(tmp_db, "failed", side="sell", state="failed")
    _event(tmp_db, "p1", "failed")
    body = tmp_db.execute("SELECT payload FROM events").fetchone()[0]
    long_key = "exit_timing:p1:" + "z" * (capture.TEXT_LIMIT + 1)
    _put(tmp_db, "events", {"ts_ms": NOW - 300, "kind": "system", "chain": "sol", "subject": "token",
                           "payload": body, "dedupe_key": long_key})
    packet = _packet(tmp_db)
    first, last = packet["exit_timing_events"]
    assert first["dedupe_key"] == "exit_timing:p1:100:1"
    assert last["dedupe_key"] is None
    assert last["_field_qualifications"]["dedupe_key"]["characters"] == len(long_key)
    assert packet["capture_coverage"]["sections"]["exit_timing_events"]["oversized_fields"] == 1


def test_wal_denial_precedes_any_source_query_or_connection(tmp_db, monkeypatch):
    reader = _reader(tmp_db)
    monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
    import kaiba.learning.autolearn_io as io
    monkeypatch.setattr(io.sqlite3, "connect", lambda *a, **kw: pytest.fail("source opened before WAL guard"))
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        capture.capture_positions(reader, ["p1"], now_ms=NOW)
    assert reader.queries == 0


def test_source_refusal_propagates_without_returning_an_acknowledgeable_partial_batch(tmp_db):
    _position(tmp_db)
    reader = _reader(tmp_db)
    original = reader.rows
    def rows(sql, args=()):
        if "FROM trades WHERE" in sql:
            raise SourceDeferred("source_query_unavailable:deadline")
        return original(sql, args)
    reader.rows = rows
    with pytest.raises(SourceDeferred, match="deadline"):
        capture.capture_positions(reader, ["p1"], now_ms=NOW)


@pytest.mark.parametrize("ids,now", [([""], NOW), ("p1", NOW), ([True], NOW), (["x" * 257], NOW),
                                      (["x"] * 31, NOW), (["p1"], True), (["p1"], 0), (["p1"], 1.5)])
def test_invalid_batch_is_rejected_before_source_admission(tmp_db, ids, now, monkeypatch):
    reader = _reader(tmp_db)
    monkeypatch.setattr(reader, "guard", lambda: pytest.fail("invalid request reached source admission"))
    with pytest.raises(ValueError):
        capture.capture_positions(reader, ids, now_ms=now)


def test_reader_deadline_cannot_exceed_three_seconds(tmp_db):
    with pytest.raises(ValueError, match="deadline"):
        capture.capture_positions(_reader(tmp_db, query_seconds=4), ["p1"], now_ms=NOW)


def test_whole_command_deadline_refuses_after_one_slow_completed_query(tmp_db, monkeypatch):
    _position(tmp_db)
    reader = _reader(tmp_db)
    original, elapsed = reader.rows, {"s": 0.0}
    monkeypatch.setattr(capture.time, "monotonic", lambda: elapsed["s"])
    def rows(sql, args=()):
        result = original(sql, args)
        elapsed["s"] += capture.COMMAND_SECONDS + 0.1
        return result
    reader.rows = rows
    with pytest.raises(SourceDeferred, match="command_budget"):
        capture.capture_positions(reader, ["p1"], now_ms=NOW)
    assert reader.queries == 1


def test_whole_command_query_budget_never_starts_an_extra_query(tmp_db, monkeypatch):
    _position(tmp_db)
    reader = _reader(tmp_db)
    monkeypatch.setattr(capture, "COMMAND_QUERIES", 1)
    with pytest.raises(SourceDeferred, match="command_budget"):
        capture.capture_positions(reader, ["p1"], now_ms=NOW)
    assert reader.queries == 1


def test_eight_real_positions_fit_the_worker_batch_and_query_budget(tmp_db):
    ids = [f"p{n}" for n in range(8)]
    for pid in ids:
        position = _position(tmp_db, pid)
        buy, failed = pid + "-buy", pid + "-failed"
        _applied(tmp_db, pid, buy)
        _applied(tmp_db, pid, pid + "-sell", side="sell")
        _order(tmp_db, failed, side="sell", state="failed")
        _event(tmp_db, pid, failed)
        _put(tmp_db, "watchdog_state", {"position_id": pid, "exit_order_id": failed, "updated_ms": NOW - 50})
        _put(tmp_db, "fill_costs", {"order_id": failed, "chain": "sol", "signature": "tx-" + failed,
                                  "basis": "decode_refused", "network_fee_native": "42", "computed_ms": NOW - 1})
        _put(tmp_db, "fill_prices", {"order_id": buy, "chain": "sol", "token": "token", "side": "buy",
            "fill_ts_ms": NOW - 200, "fill_ts_basis": "order_updated", "native_atoms": position["cost_native"],
            "token_atoms": "100", "decimals_basis": "unavailable", "basis": "fill_ratio_native_only", "computed_ms": NOW - 1})
        _put(tmp_db, "fill_reconciliations", {"order_id": buy, "chain": "sol", "signature": "tx-" + buy,
            "wallet": "wallet", "token": "token", "side": "buy", "verdict": "agree", "checked_ms": NOW - 1})
        _put(tmp_db, "order_events", {"order_id": failed, "ts_ms": NOW - 200, "state": "failed", "detail": "observed"})
        _put(tmp_db, "trades", {"trade_id": pid + "-trade", "position_id": pid, "chain": "sol", "token": "token",
            "lane": "sm-trenches", "mode": "live", "opened_ms": NOW - 1000, "closed_ms": NOW - 100,
            "hold_s": 1, "cost_native": position["cost_native"], "proceeds_native": position["proceeds_native"],
            "pnl_native": "-123", "pnl_pct": -1})
    report = capture.capture_positions(_reader(tmp_db), ids, now_ms=NOW)
    assert [p["position"]["position_id"] for p in report["packets"]] == ids
    assert all(len(p["orders"]) == 3 and len(p["attempts"]) == 1 and p["fill_costs"] and p["fill_reconciliations"]
               and p["fill_prices"] and p["trades"] and p["order_events"] for p in report["packets"])
    assert 8 < report["query_receipt"]["count"] < capture.COMMAND_QUERIES
    assert all(len(json.dumps(p, separators=(",", ":")).encode()) <= capture.MAX_PACKET_BYTES
               for p in report["packets"])
