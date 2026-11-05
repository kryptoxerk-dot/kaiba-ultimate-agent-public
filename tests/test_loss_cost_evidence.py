"""Existing decoder evidence enriches the journal without upgrading economic proof."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kaiba.learning import loss_review as review
from kaiba.learning.autolearn_io import SourceDeferred
from kaiba.learning.loss_cost_evidence import position_cost_evidence

CUTOFF = 2_000_000


def fixture():
    position = dict(position_id="loss", chain="sol", token="fixture-token", lane="sm-trenches",
                    mode="live", opened_ms=1_000_000, closed_ms=1_001_000)
    order = {**position, "order_id": "entry", "side": "buy", "tx_hash": "fixture-transaction",
             "state": "filled", "created_ms": 999_500, "updated_ms": 1_000_100}
    row = {k: order[k] for k in ("order_id", "chain", "token", "side", "lane")}
    row.update(signature=order["tx_hash"], computed_ms=1_100_000, block_time_ms=999_900,
               version="fixture-decoder", basis="venue_event", reconciled=1,
               exec_vs_decision_bps=125, exec_native_per_token="0.0010125",
               decision_ref_native_per_token="0.001", decision_ref_basis="min_out_over_slippage_band",
               venue_fee_native="100", router_fee_native="200", network_fee_native="5000",
               priority_fee_native="0", detail_json=json.dumps(dict(native_unit="lamport", wallet_bound=True,
                   exec_fee_net_basis="solana_native_venue_fee_removed", exec_fee_net_vs_decision_bps=-7)))
    return position, order, row


def result(position, order, row):
    return position_cost_evidence(position, [order], {order["order_id"]: row}, cutoff_ms=CUTOFF)


def assert_unknown_economics(out):
    assert out["realized_slippage_bps"] is None and out["fee_complete_cashflow_verified"] is None
    assert all(out[k] is None for k in ("rent_paid_atoms", "rent_recovered_atoms", "token_tax_atoms"))
    assert out["causal_verdict"] == "UNPROVEN"


def test_matched_components_are_partial_evidence_not_landing_slippage_or_full_cashflow():
    p, o, r = fixture()
    out = result(p, o, r)
    leg = out["legs"][0]
    assert out["decision_to_fill_every_filled_leg"] and out["core_components_every_filled_leg"]
    assert leg["matched_decoder_row"] and leg["native_unit"] == "lamport"
    assert leg["decoder_reported_exec_vs_decision_bps"] == "125"
    assert leg["fee_net_exec_vs_decision_bps"] == "-7"
    assert leg["reported_fee_atoms"] == dict(venue_fee_native="100", router_fee_native="200",
                                            network_fee_native="5000", priority_fee_native="0")
    assert o["tx_hash"] not in json.dumps(out) and len(leg["signature_sha256"]) == 64
    assert "priority_fee_is_a_subset_of_network_fee_do_not_add_twice" in out["semantics"]
    assert_unknown_economics(out)


@pytest.mark.parametrize("where,key,value", [
    ("p", "opened_ms", None), ("p", "closed_ms", 999_999), ("p", "closed_ms", CUTOFF + 1),
    ("o", "position_id", "other"), ("o", "mode", "shadow"), ("o", "side", "other"),
    ("o", "chain", "bsc"), ("o", "token", "other"), ("o", "lane", "other"),
    ("o", "created_ms", None), ("o", "updated_ms", 2), ("o", "updated_ms", CUTOFF + 1),
    ("o", "tx_hash", None), ("r", "order_id", "other"), ("r", "side", "sell"),
    ("r", "chain", "base"), ("r", "token", "other"), ("r", "lane", "other"),
    ("r", "signature", "wrong"), ("r", "computed_ms", True), ("r", "computed_ms", CUTOFF + 1),
    ("r", "block_time_ms", None), ("r", "block_time_ms", CUTOFF + 1),
    ("r", "block_time_ms", 1_100_001),
])
def test_identity_and_clock_mismatches_never_bind(where, key, value):
    p, o, r = fixture()
    {"p": p, "o": o, "r": r}[where][key] = value
    out = result(p, o, r)
    leg = out["legs"][0]
    assert not leg["matched_decoder_row"] and not out["core_components_every_filled_leg"]
    assert not out["decision_to_fill_every_filled_leg"] and leg["gaps"]
    assert leg["decoder_reported_exec_vs_decision_bps"] is None
    assert_unknown_economics(out)


@pytest.mark.parametrize("detail", [None, "malformed", "[]", '{"native_unit":"wei","wallet_bound":true}',
    '{"native_unit":"lamport","wallet_bound":1}', '{"native_unit":"lamport","wallet_bound":false}',
    '{"native_unit":"lamport"}'])
def test_units_and_wallet_attribution_are_required_not_coerced(detail):
    p, o, r = fixture()
    r["detail_json"] = detail
    out = result(p, o, r)
    assert out["matched_decoder_legs"] == 1
    assert not out["decision_to_fill_every_filled_leg"] and not out["core_components_every_filled_leg"]


@pytest.mark.parametrize("key,value", [
    ("exec_vs_decision_bps", None), ("exec_vs_decision_bps", float("nan")),
    ("exec_native_per_token", "NaN"), ("exec_native_per_token", "0"),
    ("decision_ref_native_per_token", "-1"), ("decision_ref_native_per_token", "1e9999"),
    ("decision_ref_basis", "other")])
def test_missing_or_invalid_price_reference_never_qualifies_a_leg(key, value):
    p, o, r = fixture()
    r[key] = value
    assert not result(p, o, r)["decision_to_fill_every_filled_leg"]


@pytest.mark.parametrize("value", [None, True, "-1", "1.5", "Infinity", "1e9999", str(2**256)])
def test_fee_unknowns_never_become_zero_or_core_coverage(value):
    p, o, r = fixture()
    r["venue_fee_native"] = value
    out = result(p, o, r)
    assert out["decision_to_fill_every_filled_leg"]
    assert not out["core_components_every_filled_leg"]
    assert out["legs"][0]["reported_fee_atoms"]["venue_fee_native"] is None


def test_known_zero_fees_are_retained_but_unreconciled_is_not_core():
    p, o, r = fixture()
    for key in ("venue_fee_native", "router_fee_native", "network_fee_native"):
        r[key] = "0"
    assert result(p, o, r)["core_components_every_filled_leg"]
    for unknown in (0, None, True, "1"):
        r["reconciled"] = unknown
        assert not result(p, o, r)["core_components_every_filled_leg"]


def test_unpriced_decoder_can_expose_reported_network_fee_with_its_unknowns():
    p, o, r = fixture()
    r["basis"] = "no_venue_event"
    r["detail_json"] = "{}"
    out = result(p, o, r)
    leg = out["legs"][0]
    assert leg["matched_decoder_row"] and leg["reported_fee_atoms"]["network_fee_native"] == "5000"
    assert leg["decoder_reported_exec_vs_decision_bps"] is None
    assert leg["wallet_bound"] is None and leg["native_unit"] is None
    assert not out["core_components_every_filled_leg"]


def test_no_vacuous_coverage_no_failed_tx_cost_or_shadow_chain_proof():
    p, o, r = fixture()
    out = position_cost_evidence(p, [], {}, cutoff_ms=CUTOFF)
    assert out["filled_legs"] == 0 and not out["core_components_every_filled_leg"]
    o["state"] = "failed"
    out = result(p, o, r)
    assert out["filled_legs"] == 0 and out["excluded_nonfilled_orders"] == 1
    p["mode"] = "shadow"
    assert result(p, o, r)["applicable"] is False


def test_all_filled_legs_required_and_duplicate_links_never_qualify():
    p, o, r = fixture()
    sell = {**o, "order_id": "exit", "side": "sell"}
    out = position_cost_evidence(p, [o, sell], {o["order_id"]: r}, cutoff_ms=CUTOFF)
    assert out["filled_legs"] == 2 and out["core_components_bound_legs"] == 1
    assert not out["core_components_every_filled_leg"]
    out = position_cost_evidence(p, [o, o], {o["order_id"]: r}, cutoff_ms=CUTOFF)
    assert out["gap_counts"]["duplicate_filled_order_link"] == 2
    assert not out["decision_to_fill_every_filled_leg"]


def test_evm_unit_is_wei_and_never_gains_sol_fee_net_adjustment():
    p, o, r = fixture()
    p["chain"] = o["chain"] = r["chain"] = "base"
    r["detail_json"] = json.dumps(dict(native_unit="wei", wallet_bound=True,
        exec_fee_net_basis="solana_native_venue_fee_removed", exec_fee_net_vs_decision_bps=-7))
    out = result(p, o, r)
    assert out["core_components_every_filled_leg"] and out["legs"][0]["native_unit"] == "wei"
    assert out["legs"][0]["fee_net_exec_vs_decision_bps"] is None


def seed(conn, *, pid="loss", mode="live", count=2):
    p, o, row = fixture()
    p.update(position_id=pid, mode=mode)
    conn.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,"
        "qty_total,cost_native,proceeds_native,realized_native) VALUES(?,?,?,?,?,?,?,'0','100','100','75','-25')",
        tuple(p[k] for k in ("position_id", "chain", "token", "lane", "mode", "opened_ms", "closed_ms")))
    for i in range(count):
        oid, side = f"{pid}-{i}", "buy" if i == 0 else "sell"
        conn.execute("INSERT INTO orders(order_id,chain,token,side,lane,mode,input_token,output_token,amount_in,"
            "min_out,slippage_bps,state,provider,tx_hash,created_ms,updated_ms) "
            "VALUES(?,?,?,?,?,?,'SOL','fixture-token','100','90',100,'filled','fixture',?,?,?)",
            (oid, "sol", "fixture-token", side, "sm-trenches", mode, o["tx_hash"], o["created_ms"], o["updated_ms"]))
        conn.execute("INSERT INTO position_orders VALUES(?,?,?,?)", (pid, oid, side, 1_000_000))
        cost = {**row, "order_id": oid, "side": side}
        conn.execute(f"INSERT INTO fill_costs({','.join(cost)}) VALUES({','.join('?' for _ in cost)})", tuple(cost.values()))
    conn.commit()


def test_real_report_uses_existing_rows_keeps_strict_gaps_and_changes_fingerprint(tmp_db, tmp_path):
    seed(tmp_db)
    changes = tmp_db.total_changes
    first = review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    r = first["reviews"][0]
    assert r["decoded_fill_cost_evidence"]["core_components_every_filled_leg"]
    assert {"fee_complete_cashflow_unavailable", "realized_slippage_unavailable"} <= set(r["evidence_gaps"])
    assert r["causal_verdict"] == "UNPROVEN" and tmp_db.total_changes == changes
    assert first["decoded_fill_cost_coverage"]["positions_core_components_every_filled_leg"] == 1
    assert review.write_reviews(first, tmp_path)["new"] == 1
    assert review.write_reviews(review.build_reviews(tmp_db, cutoff_ms=CUTOFF + 1), tmp_path)["updated"] == 0
    tmp_db.execute("UPDATE fill_costs SET venue_fee_native='101' WHERE side='buy'")
    second = review.build_reviews(tmp_db, cutoff_ms=CUTOFF + 1)
    assert second["reviews"][0]["evidence_fingerprint"] != r["evidence_fingerprint"]
    assert review.write_reviews(second, tmp_path)["updated"] == 1
    assert json.loads((tmp_path / "manifest.json").read_text())["decoded_fill_cost_coverage"] == second["decoded_fill_cost_coverage"]


def test_paper_payload_and_fingerprint_unchanged_by_decoder_data(tmp_db):
    seed(tmp_db, mode="shadow")
    before = review.build_reviews(tmp_db, cutoff_ms=CUTOFF)
    assert "decoded_fill_cost_evidence" not in before["reviews"][0]
    tmp_db.execute("DELETE FROM fill_costs")
    assert review.build_reviews(tmp_db, cutoff_ms=CUTOFF)["reviews"] == before["reviews"]
    assert before["decoded_fill_cost_coverage"]["positions"] == 0


def test_fill_lookup_is_keyed_live_only_short_guarded_queries_with_original_caps(tmp_db, monkeypatch):
    seed(tmp_db, count=203)
    seed(tmp_db, pid="paper", mode="shadow")
    tmp_db.execute("UPDATE orders SET state='failed' WHERE order_id='loss-202'")
    tmp_db.commit()
    path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    reader = review._ReviewReader(path)
    actual_rows = reader.rows
    calls = []

    def rows(sql, args=()):
        if "FROM fill_costs" in sql:
            calls.append(args)
            assert "order_id IN (" in sql and 0 < len(args) <= 100
            assert all(x.startswith("loss-") and x != "loss-202" for x in args)
        return actual_rows(sql, args)

    monkeypatch.setattr(reader, "rows", rows)
    report = review.build_reviews(reader, cutoff_ms=CUTOFF)
    assert [len(c) for c in calls] == [100, 100, 2]
    assert report["decoded_fill_cost_coverage"]["filled_legs"] == 202
    assert reader.query_seconds == 3 and reader.max_rows == 10_000
    assert review.CAPTURE_SECONDS == 30 and review.PAGE_ROWS == 2000 and review.LOOKUP_IDS == 100


def test_cost_lookup_refusal_defers_whole_capture_and_preserves_previous_outputs(tmp_db, tmp_path, monkeypatch, capsys):
    seed(tmp_db)
    output = tmp_path / "report"
    output.mkdir()
    for name in ("manifest.json", "reviews.json", "reviews.csv"):
        (output / name).write_text("{}")
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    actual_rows = review._ReviewReader.rows

    def rows(self, sql, args=()):
        if "FROM fill_costs" in sql:
            raise SourceDeferred("source_query_unavailable")
        return actual_rows(self, sql, args)

    monkeypatch.setattr(review._ReviewReader, "rows", rows)
    path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    assert review.main(["--db", str(path), "--output", str(output)]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "source_deferred" and status["outputs_written"] is False
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_helper_is_pure_and_does_not_mutate_inputs():
    p, o, r = fixture()
    before = copy.deepcopy((p, o, r))
    result(p, o, r)
    assert (p, o, r) == before
