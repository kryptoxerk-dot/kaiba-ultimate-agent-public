"""Bounded-reader and durable-journal controls for autonomous paper research."""

from __future__ import annotations

import inspect
import json
import sqlite3
import time
from pathlib import Path

import pytest

from kaiba.learning import autolearn_io as io
from kaiba.learning.economic_outcomes import project_episode


def _path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_oversized_wal_refuses_before_first_connect_or_sql(tmp_db, monkeypatch):
    reader = io.GuardedReader(_path(tmp_db))
    monkeypatch.setattr(reader, "_wal_bytes", lambda: io.WAL_LIMIT + 1)
    called = []
    monkeypatch.setattr(io.sqlite3, "connect", lambda *a, **k: called.append(True) or (_ for _ in ()).throw(
        AssertionError("opening the source before WAL admission is unsafe")))
    with pytest.raises(io.SourceDeferred, match="wal_over_limit"):
        reader.q("SELECT 1")
    assert called == []
    assert reader.queries == 0
    assert reader.receipts == []


def test_wal_guard_positive_control_within_budget_reaches_query(tmp_db, monkeypatch):
    reader = io.GuardedReader(_path(tmp_db))
    monkeypatch.setattr(reader, "_wal_bytes", lambda: io.WAL_LIMIT)
    assert reader.q("SELECT 7") == [(7,)]
    assert reader.queries == 1
    assert reader.receipts[0]["wal_bytes"] == io.WAL_LIMIT


@pytest.mark.parametrize("cap", [-1, 0, True, 1.5, "100", 50_001])
def test_row_cap_cannot_disable_the_guard_with_negative_bool_fraction_or_oversized_values(tmp_db, cap):
    with pytest.raises(ValueError, match="row cap"):
        io.GuardedReader(_path(tmp_db), max_rows=cap)


@pytest.mark.parametrize("sql,reason", [
    ("SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3", "source_row_cap"),
    ("CREATE TABLE autolearn_illegal_write(a)", "source_query_unavailable"),
    ("SELECT missing_column FROM missing_table", "source_query_unavailable"),
])
def test_failures_close_the_connection_and_refuse_partial_or_mutating_results(tmp_db, monkeypatch, sql, reason):
    real_connect = sqlite3.connect
    connections = []

    class ObservedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            return super().close()

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs, factory=ObservedConnection)
        connections.append(conn)
        return conn

    monkeypatch.setattr(io.sqlite3, "connect", connect)
    reader = io.GuardedReader(_path(tmp_db), max_rows=2)
    with pytest.raises(io.SourceDeferred, match=reason):
        reader.q(sql)
    assert len(connections) == 1 and connections[0].closed
    assert reader.queries == 1 and len(reader.receipts) == 1
    assert reader.q("SELECT 11") == [(11,)]
    assert all(conn.closed for conn in connections)
    assert tmp_db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='autolearn_illegal_write'").fetchone()[0] == 0


def test_query_deadline_interrupts_actual_recursive_work_and_reader_recovers(tmp_db):
    ticks = iter((0.0, .4, .8, 1.2, 1.6, 2.0))
    reader = io.GuardedReader(_path(tmp_db), query_seconds=.5, clock=lambda: next(ticks))
    wall_started = time.monotonic()
    with pytest.raises(io.SourceDeferred, match="source_query_unavailable"):
        reader.q("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n")
    assert time.monotonic() - wall_started < 3
    assert .5 <= reader.receipts[0]["duration_s"] < 3
    reader.clock = time.monotonic
    assert reader.q("SELECT 1") == [(1,)]


def _seed_quote(conn, *, now=1000, migrated=500, decimals=6, native_at=1000, receipt_at=1000):
    conn.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES (?,?,?,?,?)",
                 ("sol", "token", decimals, 10, migrated))
    conn.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)",
                 ("sol", native_at, "100", "fixture-native"))
    def measure(value):
        return {"value": value, "receipt": {"observed_at_ms": receipt_at}}
    dossier = {"price_usd": measure(".001"), "liquidity_usd": measure("100000"),
               "buy_tax_bps": {"value": "0"}, "sell_tax_bps": {"value": "0"}}
    conn.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES (?,?,?,?)",
                 ("sol", "token", now, json.dumps(dossier)))


def test_pool_quote_uses_fresh_receipts_and_explicit_model_label(tmp_db):
    _seed_quote(tmp_db)
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000)
    assert quote["basis"] == "captured_pool_depth"
    assert quote["price_usd"] == "0.001"
    assert quote["buy_tax_bps"] == 0
    assert quote["cost_model_assumed"] and not quote["live_cost_verified"]


def _cache(root, *, chain="sol", token="token", fetched_ms=1000, **data):
    from kaiba.core.schemas import digest
    root.mkdir(parents=True, exist_ok=True)
    key = f"gmgn-cli token info --address {token} --chain {chain} --raw"
    path = root / (digest(key)[:32] + ".json")
    body = {"address": token, "decimals": 6, "price": {"address": token, "price": ".002"},
            "liquidity": "12345", **data}
    path.write_text(json.dumps({"fetched_ms": fetched_ms, "data": body}))
    return path


def test_paid_cached_precision_repairs_missing_metadata_without_writing_source(tmp_db, tmp_path):
    _seed_quote(tmp_db, decimals=None)
    cache = tmp_path / "cache"
    _cache(cache, fetched_ms=900)
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=cache)
    assert quote["decimals"] == 6
    assert quote["diagnostics"]["metadata"]["fetched_ms"] == 900
    assert len(quote["diagnostics"]["metadata"]["sha256"]) == 64
    assert tmp_db.execute("SELECT decimals FROM tokens").fetchone()[0] is None


def test_fresh_coherent_cache_can_price_when_dossier_is_stale(tmp_db, tmp_path):
    _seed_quote(tmp_db, decimals=None, receipt_at=-200000)
    _cache(tmp_path, fetched_ms=1000)
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert quote["basis"] == "captured_gmgn_pool_depth"
    assert quote["price_usd"] == "0.002" and quote["liquidity_usd"] == "12345"
    assert quote["price_ts_ms"] == quote["depth_ts_ms"] == quote["quote_ts_ms"] == 1000
    assert quote["cost_model_assumed"] and not quote["live_cost_verified"]


@pytest.mark.parametrize("changes", [
    {"chain": "bsc"},
    {"price": {"address": "token", "chain": "bsc", "price": ".002"}},
    {"pool": {"chain": "bsc", "address": "LP"}},
    {"pool": {"chain": "sol", "address": "LP", "base_address": "other1", "quote_address": "other2"}},
])
def test_cached_quote_refuses_explicit_wrong_chain_or_unrelated_pool(tmp_db, tmp_path, changes):
    _seed_quote(tmp_db, decimals=None, receipt_at=-200000)
    _cache(tmp_path, fetched_ms=1000, **changes)
    quote = io.quote_evidence(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert quote["unavailable_reason"] == "token_decimals_missing_or_invalid"


@pytest.mark.parametrize("pool", [
    {"chain": "sol", "address": "LP", "base_address": "token", "quote_address": "other"},
    {"chain": "sol", "address": "LP", "base_address": "other", "quote_address": "token"},
    {"address": "LP", "base_address": "other"},
])
def test_pool_lp_address_and_constituent_orientation_do_not_reject_valid_quote(tmp_db, tmp_path, pool):
    _seed_quote(tmp_db, decimals=None, receipt_at=-200000)
    _cache(tmp_path, fetched_ms=1000, pool=pool)
    quote = io.quote_evidence(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert quote["basis"] == "captured_gmgn_pool_depth"
    assert quote["quote_ts_ms"] == 1000 and quote["price_usd"] == "0.002"


@pytest.mark.parametrize("data", [{"decimals": True}, {"decimals": 6.5}, {"decimals": -1},
                                  {"decimals": 31}, {"decimals": "NaN"}, {"address": "other"},
                                  {"price": {"address": "other", "price": "1"}}])
def test_cache_cannot_guess_precision_or_substitute_another_token(tmp_db, tmp_path, data):
    _seed_quote(tmp_db, decimals=None)
    _cache(tmp_path, **data)
    result = io.quote_evidence(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert result["unavailable_reason"] == "token_decimals_missing_or_invalid"


@pytest.mark.parametrize("stamp", [1001, 1000 - io.DAY_MS - 1])
def test_future_or_expired_cache_cannot_supply_metadata(tmp_db, tmp_path, stamp):
    _seed_quote(tmp_db, decimals=None)
    _cache(tmp_path, fetched_ms=stamp)
    assert io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path) is None


def test_refreshing_file_mtime_does_not_make_cached_price_fresh(tmp_db, tmp_path):
    _seed_quote(tmp_db, decimals=None, receipt_at=-200000)
    path = _cache(tmp_path, fetched_ms=-120001)
    path.touch()
    result = io.quote_evidence(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert result["unavailable_reason"] == "pool_price_or_depth_missing_or_stale"
    assert result["diagnostics"]["decimals"] == 6


def test_later_precision_capture_advances_availability_without_refreshing_old_price(tmp_db, tmp_path):
    _seed_quote(tmp_db, decimals=None, receipt_at=900)
    _cache(tmp_path, fetched_ms=1000, price={"address": "token"}, liquidity=None)
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert quote["quote_ts_ms"] == 1000 and quote["price_ts_ms"] == 900


def test_no_quote_retains_explicit_failure_cause(tmp_db, tmp_path):
    _seed_quote(tmp_db, decimals=None)
    result = io.quote_evidence(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000, cache_root=tmp_path)
    assert result["unavailable_reason"] == "token_decimals_missing_or_invalid"
    assert result["diagnostics"]["checked_ms"] == 1000


def test_oversized_journal_body_stays_valid_json_and_acknowledges_omitted_evidence(tmp_db):
    import hashlib
    body = json.dumps({"type": "too_large", "evidence": "x" * 5000})
    result = io.persist_lessons(_path(tmp_db), [{"evidence_id": "oversized-json", "text": "Research only",
                                              "body": body, "lane": None}])
    assert len(result["added"]) == 1
    stored = json.loads(tmp_db.execute("SELECT body FROM journal WHERE subject='autolearn:oversized-json'").fetchone()[0])
    assert stored["details_omitted"] is True
    assert stored["original_body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert stored["original_body_chars"] == len(body)


@pytest.mark.parametrize("kwargs", [{"decimals": None}, {"native_at": 1001}, {"native_at": -300001},
                                    {"receipt_at": 1001}, {"receipt_at": -120001}])
def test_missing_decimals_future_and_stale_prices_do_not_make_a_quote(tmp_db, kwargs):
    _seed_quote(tmp_db, **kwargs)
    assert io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000) is None


def test_fresh_curve_uses_the_captured_curve_invariant_and_refuses_future_curve(tmp_db):
    from kaiba.execution.curve_price import RESERVED_TOKEN_ATOMS

    tmp_db.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms) VALUES (?,?,?,?)", ("sol", "token", 6, 10))
    tmp_db.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)", ("sol", 1000, "100", "native"))
    real = 700_000_000_000_000
    tmp_db.execute("INSERT INTO curve_snapshots(chain,token,observed_ms,real_sol_lamports,virtual_sol_lamports,"
                   "real_token_atoms,virtual_token_atoms,source) VALUES (?,?,?,?,?,?,?,?)",
                   ("sol", "token", 1000, 30_000_000_000, 60_000_000_000, str(real), str(real + RESERVED_TOKEN_ATOMS), "pumpfun"))
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000)
    assert quote["basis"] == "captured_curve"
    assert quote["curve"]["complete"] is False
    tmp_db.execute("UPDATE curve_snapshots SET observed_ms=1001")
    assert io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000) is None


def test_pool_packet_is_available_at_latest_component_clock_with_prior_native_price(tmp_db):
    _seed_quote(tmp_db)
    dossier = json.loads(tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0])
    dossier["price_usd"]["receipt"]["observed_at_ms"] = 900
    dossier["liquidity_usd"]["receipt"]["observed_at_ms"] = 1000
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=?", (json.dumps(dossier),))
    tmp_db.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)",
                   ("sol", 1100, "999", "future-native-relative-to-packet"))
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1100)
    assert quote["quote_ts_ms"] == 1000  # depth at1000 was unavailable at900
    assert quote["price_ts_ms"] == 900 and quote["depth_ts_ms"] == 1000
    assert quote["native_price_ts_ms"] == 1000
    assert quote["native_usd"] == "100"


def test_robinhood_native_price_uses_verified_eth_alias_instead_of_robinhood_sample(tmp_db):
    tmp_db.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES (?,?,?,?,?)",
                   ("robinhood", "token", 18, 1, 500))
    for chain, price in (("eth", "2000"), ("robinhood", "999")):
        tmp_db.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)",
                       (chain, 1000, price, "native-" + chain))
    dossier = {key: {"value": value, "basis": "provider_reported", "receipt": {"observed_at_ms": 1000}}
               for key, value in (("price_usd", "1"), ("liquidity_usd", "100000"))}
    tmp_db.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES (?,?,?,?)",
                   ("robinhood", "token", 1000, json.dumps(dossier)))
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "robinhood", "token", now_ms=1000)
    assert quote["native_usd"] == "2000"
    assert quote["native_source_chain"] == "eth"
    assert quote["native_price_source"] == "native-eth"
    assert quote["chain"] == "robinhood"


@pytest.mark.parametrize("basis_location", ["measure", "receipt"])
@pytest.mark.parametrize("basis", ["unavailable", "stale", "estimated"])
def test_retained_numeric_quote_values_with_explicit_unknown_or_estimated_basis_are_unusable(tmp_db, basis_location, basis):
    _seed_quote(tmp_db)
    dossier = json.loads(tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0])
    target = dossier["price_usd"] if basis_location == "measure" else dossier["price_usd"]["receipt"]
    target["basis"] = basis
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=?", (json.dumps(dossier),))
    assert io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1000) is None


@pytest.mark.parametrize("stamp", [1001, -120001, None])
def test_tax_receipt_cannot_be_future_stale_or_missing_relative_to_captured_packet(tmp_db, stamp):
    _seed_quote(tmp_db)
    dossier = json.loads(tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0])
    tax = {"value": "200", "basis": "provider_reported", "receipt": {}}
    if stamp is not None:
        tax["receipt"]["observed_at_ms"] = stamp
    dossier["buy_tax_bps"] = tax
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=?", (json.dumps(dossier),))
    quote = io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1100)
    assert quote["quote_ts_ms"] == 1000
    assert quote["buy_tax_bps"] is None
    dossier["buy_tax_bps"]["receipt"]["observed_at_ms"] = 1000
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=?", (json.dumps(dossier),))
    assert io.quote_snapshot(io.GuardedReader(_path(tmp_db)), "sol", "token", now_ms=1100)["buy_tax_bps"] == 200


def test_signal_features_are_entry_time_payloads_and_future_features_are_excluded(tmp_db):
    for sid, created, payload in (("past", 100, {"holder_count": {"value": 70}}),
                                  ("future", 201, {"holder_count": 999, "top10_pct": 99})):
        tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES (?,?,?,?,?,?)",
                       (sid, "sm-trenches", "sol", "token", created, json.dumps(payload)))
    for did, at, action, size in (("old", 50, "enter", 100), ("current", 200, "enter", 100),
                                ("future", 501, "enter", 100), ("skipped", 300, "skip", 0)):
        tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (did, at, "sm-trenches", "live", "sol", "token", action, size,
                                                    json.dumps(["past", "future"])))
    reader = io.GuardedReader(_path(tmp_db))
    actual = io.read_opportunities(reader, after_rowid=0, selected_ms=100, now_ms=500,
                                  lanes=["sm-trenches"], chains=["sol"])
    assert actual["source_rows"] == 2
    assert len(actual["opportunities"]) == 1
    assert actual["opportunities"][0]["decision_id"] == "current"
    assert actual["opportunities"][0]["features"] == {"holder_count": 70}
    assert actual["opportunities"][0]["feature_basis"] == "stored_signal_at_decision"
    assert actual["opportunities"][0]["mode"] == "shadow"
    assert actual["skipped"] == [{"decision_id": "skipped", "reason": "incumbent_skipped_or_unsized"}]


@pytest.mark.parametrize("mutation", ["chain", "token", "lane", "future_receipt", "missing_receipt_clock",
                                      "unavailable_measure", "estimated_receipt"])
def test_signal_features_require_matching_identity_and_usable_predecision_receipts(tmp_db, mutation):
    chain, token, lane = "sol", "token", "sm-trenches"
    feature = {"value": 70, "receipt": {"observed_at_ms": 100}}
    if mutation == "chain":
        chain = "bsc"
    elif mutation == "token":
        token = "other-token"
    elif mutation == "lane":
        lane = "trusted-copy"
    elif mutation == "future_receipt":
        feature["receipt"]["observed_at_ms"] = 201
    elif mutation == "missing_receipt_clock":
        feature["receipt"] = {}
    elif mutation == "unavailable_measure":
        feature["basis"] = "unavailable"
    else:
        feature["receipt"]["basis"] = "estimated"
    tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES (?,?,?,?,?,?)",
                   ("signal", lane, chain, token, 100, json.dumps({"holder_count": feature})))
    tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("decision", 200, "sm-trenches", "live", "sol", "token", "enter", 100,
                                                 json.dumps(["signal"])))
    result = io.read_opportunities(io.GuardedReader(_path(tmp_db)), after_rowid=0, selected_ms=0, now_ms=200,
                                  lanes=["sm-trenches"], chains=["sol"])
    assert result["opportunities"][0]["features"] == {}


def test_closed_packets_preserve_main_rows_without_links_and_keep_live_economics_unknown(tmp_db):
    for pid, closed in (("old", 99), ("orphan-main", 200), ("future", 501)):
        tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,"
                       "qty,qty_total,cost_native,proceeds_native,realized_native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, "sol", "token", "sm-trenches", "live", 10, closed, "0", "100", "1000", "900", "-100"))
    result = io.closed_packets(io.GuardedReader(_path(tmp_db)), after_ms=100, now_ms=500)
    assert len(result["packets"]) == 1
    source = result["packets"][0]
    assert source["position"]["position_id"] == "orphan-main"
    assert source["orders"] == [] and source["coverage"] == {}
    economic = project_episode(source)
    assert economic["ledger_pnl_native"] == "-100"
    assert economic["economic_pnl_native"] is None
    assert economic["verdict"] == "UNPROVEN"
    assert io.closed_packets(io.GuardedReader(_path(tmp_db)), after_ms=result["next_closed_ms"],
                             after_rowid=result["next_rowid"], now_ms=500)["packets"] == []


def _lesson():
    return {"evidence_id": "frozen_evidence_1", "text": "Measure the complete net paper return.",
            "lane": "sm-trenches", "outcome": "miss", "body": "A net loss is not a promotion signal."}


@pytest.mark.parametrize("crash_after_append", [False, True])
def test_lesson_credit_and_journal_resume_exactly_once_after_crash(tmp_db, monkeypatch, crash_after_append):
    from kaiba.core import journal
    from kaiba.learning import playbook

    original = journal.append

    def crashing(kind, body, **kwargs):
        if kind == "lesson":
            if crash_after_append:
                original(kind, body, **kwargs)
            raise RuntimeError("injected journal interruption")
        return original(kind, body, **kwargs)

    monkeypatch.setattr(journal, "append", crashing)
    with pytest.raises(RuntimeError, match="injected journal interruption"):
        io.persist_lessons(_path(tmp_db), [_lesson()])
    rid = playbook.rule_id_for(_lesson()["text"], _lesson()["lane"])
    assert tmp_db.execute("SELECT misses FROM playbook WHERE rule_id=?", (rid,)).fetchone()[0] == 1
    monkeypatch.setattr(journal, "append", original)
    resumed = io.persist_lessons(_path(tmp_db), [_lesson()])
    assert len(resumed["added"]) == 1
    assert io.persist_lessons(_path(tmp_db), [_lesson()])["repeated"] == 1
    rule = tmp_db.execute("SELECT hits,misses,status FROM playbook WHERE rule_id=?", (rid,)).fetchone()
    assert tuple(rule) == (0, 1, "proposed")
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (_lesson()["evidence_id"],)).fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", ("autolearn:" + _lesson()["evidence_id"],)).fetchone()[0] == 1
    assert journal.verify(tmp_db) == (True, None)


def test_lesson_writer_honours_wal_admission_before_any_learning_write(tmp_db, monkeypatch):
    monkeypatch.setattr(io.GuardedReader, "_wal_bytes", lambda self: io.WAL_LIMIT + 1)
    with pytest.raises(io.SourceDeferred, match="wal_over_limit"):
        io.persist_lessons(_path(tmp_db), [_lesson()])
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook").fetchone()[0] == 0


def test_the_wal_admission_regression_detects_the_guard_removed_mutation(tmp_db, monkeypatch):
    source = inspect.getsource(io.GuardedReader)
    mutation = source.replace("size = self.guard()", "size = 0", 1)
    assert mutation != source
    namespace = dict(vars(io))
    exec(compile(mutation, "<offline-wal-guard-mutation>", "exec"), namespace)
    monkeypatch.setattr(io, "GuardedReader", namespace["GuardedReader"])
    # Run the real regression against the mutant: opening before admission fails
    # the regression's independent no-connect witness, before any SQL executes.
    with pytest.raises(AssertionError, match="opening the source before WAL admission"):
        test_oversized_wal_refuses_before_first_connect_or_sql(tmp_db, monkeypatch)
