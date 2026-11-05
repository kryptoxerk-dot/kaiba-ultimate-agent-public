"""Real migrated sources, late decoder receipts and durable bounded revisits."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.learning import economic_work as work
from kaiba.learning.autolearn_io import DAY_MS, GuardedReader, SourceDeferred

NOW = 1_800_000_000_000


def _reader(conn):
    return GuardedReader(Path(conn.execute("PRAGMA database_list").fetchone()[2]))


def _position(conn, pid, *, chain="sol", mode="live", closed=NOW - 1000, opened=NOW - 10_000):
    conn.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,"
                 "cost_native,proceeds_native,realized_native) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, chain, "token-" + pid, "sm-trenches", mode, opened, closed,
                  "100" if closed is None else "0", "100", "1000", "900", "-100"))


def _buy(conn, pid, *, chain="sol"):
    oid = "buy-" + pid
    conn.execute("INSERT INTO orders(order_id,chain,token,side,lane,mode,input_token,output_token,amount_in,min_out,"
                 "slippage_bps,state,provider,tx_hash,filled_out,created_ms,updated_ms) "
                 "VALUES (?,?,?,'buy','sm-trenches','live','native',?,'1000','1',100,'filled','gmgn',?,'100',?,?)",
                 (oid, chain, "token-" + pid, "token-" + pid, "tx-" + pid, NOW - 10_000, NOW - 9000))
    conn.execute("INSERT INTO position_orders(position_id,order_id,side,ts_ms) VALUES (?,?,'buy',?)",
                 (pid, oid, NOW - 9000))
    return oid


def _state(path):
    return json.loads(path.read_text())


def test_real_late_fee_revises_same_episode_and_unchanged_retry_is_not_new_evidence(tmp_db, tmp_path):
    _position(tmp_db, "p")
    oid = _buy(tmp_db, "p")
    path = tmp_path / "audit.json"
    first = work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    assert first["economic"]["population_count"] == 1
    assert first["economic"]["economic_complete_count"] == 0
    initial = _state(path)["records"]["p"]
    assert initial["revision_count"] == 1
    # Empty close/open scans wrap; the oldest retained record still gets a retry.
    repeat = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    assert "p" in repeat["capture"]["unchanged_ids"]
    assert _state(path)["records"]["p"]["revision_count"] == 1
    assert _state(path)["records"]["p"]["retained_hash"] == initial["retained_hash"]
    tmp_db.execute("INSERT INTO fill_costs(order_id,chain,side,token,signature,basis,router_fee_native,"
                   "total_cost_native,reconciled,computed_ms) VALUES (?,?,'buy',?,?,?,'10','25',1,?)",
                   (oid, "sol", "token-p", "tx-p", "venue_event", NOW + 61_000))
    late = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 120_000)
    assert late["capture"]["changed_ids"] == ["p"]
    assert late["capture"]["revisions"][0]["previous_source_hash"] == initial["source_hash"]
    row = late["economic"]["outcomes"][0]
    assert row["decoded_fill_evidence"][0]["router_fee_native"] == "10"
    assert row["economic_pnl_native"] is None and row["verdict"] == "UNPROVEN"
    assert _state(path)["records"]["p"]["revision_count"] == 2
    assert len(_state(path)["records"]["p"]["recent_source_revisions"]) == 2
    assert tmp_db.execute("SELECT COUNT(*) FROM fill_costs").fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0


def test_new_closes_open_inventory_and_retries_have_independent_rotation(tmp_db, tmp_path):
    for i, chain in enumerate(("sol", "bsc", "robinhood", "sol", "bsc", "robinhood")):
        _position(tmp_db, "c" + str(i), chain=chain, closed=NOW - 100 + i)
    for i, chain in enumerate(("sol", "bsc", "robinhood")):
        _position(tmp_db, "o" + str(i), chain=chain, closed=None)
    _position(tmp_db, "paper", mode="shadow")
    _position(tmp_db, "canary", mode="canary", closed=NOW - 1000)
    _position(tmp_db, "future-close", closed=NOW + DAY_MS, opened=NOW + 1000)
    _position(tmp_db, "future-open", closed=None, opened=NOW + DAY_MS)
    _position(tmp_db, "old", closed=NOW - 13 * DAY_MS)
    path = tmp_path / "audit.json"
    receipts = [work.capture_work(_reader(tmp_db), path, now_ms=NOW + n * 60_000) for n in range(24)]
    rows = _state(path)["records"]
    assert set(rows) == {"canary", *("c" + str(i) for i in range(6)), *("o" + str(i) for i in range(3))}
    assert {r["packet"]["position"]["chain"] for r in rows.values()} == {"sol", "bsc", "robinhood"}
    assert any(r["capture"]["retry_ids"] for r in receipts)
    assert any("o2" in r["capture"]["open_ids"] for r in receipts)
    assert all(not r["capture"]["population_complete"] for r in receipts)
    assert receipts[-1]["economic"]["population_scope"].endswith("not_strategy_validation")
    assert receipts[-1]["economic"]["economic_complete_count"] == 0


def test_failed_capture_cannot_acknowledge_selection_or_retry(tmp_db, tmp_path):
    _position(tmp_db, "old")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    before = path.read_bytes()
    _position(tmp_db, "new", closed=NOW + 10_000)
    class Interrupted(GuardedReader):
        def rows(self, sql, args=()):
            if "position_id IN" in sql or "position_id=?" in sql:
                raise SourceDeferred("fixture_interruption")
            return super().rows(sql, args)
    reader = Interrupted(_reader(tmp_db).db_path)
    with pytest.raises(SourceDeferred):
        work.capture_work(reader, path, now_ms=NOW + 120_000)
    assert path.read_bytes() == before
    restored = [work.capture_work(_reader(tmp_db), path, now_ms=NOW + (i + 2) * 60_000) for i in range(3)]
    assert any("new" in r["capture"]["new_closed_ids"] for r in restored)
    assert set(_state(path)["records"]) == {"new", "old"}


def test_eviction_and_expiry_are_explicit_and_do_not_claim_total_population(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(work, "MAX_RECORDS", 3)
    for i in range(6):
        _position(tmp_db, str(i), closed=NOW - 100 + i)
    path = tmp_path / "audit.json"
    result = [work.capture_work(_reader(tmp_db), path, now_ms=NOW + i * 60_000) for i in range(18)]
    second = result[-1]
    assert len(_state(path)["records"]) == 3
    assert second["capture"]["evicted_total"] >= 3
    assert any(r["capture"]["evicted_ids"] for r in result)
    assert not second["capture"]["population_complete"]
    expired = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 13 * DAY_MS)
    assert expired["capture"]["expired_total"] == 3
    assert expired["economic"]["population_count"] == 0


def test_corrupt_or_tampered_checkpoint_is_retained_as_error(tmp_db, tmp_path):
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    state = _state(path)
    state["records"]["p"]["packet"]["position"]["cost_native"] = "1"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid_economic_work_record"):
        work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    assert path.read_bytes() == before


def test_source_row_deletion_retains_retry_identity_but_not_current_projection(tmp_db, tmp_path):
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    tmp_db.execute("DELETE FROM positions WHERE position_id='p'")
    result = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    assert result["capture"]["missing_ids"] == ["p"]
    assert result["economic"]["population_count"] == 0
    assert result["economic"]["unavailable_retained_count"] == 1
    assert _state(path)["records"]["p"]["status"] == "source_missing"


def test_packet_and_checkpoint_budgets_still_leave_unavailable_retry_receipts(tmp_db, tmp_path, monkeypatch):
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    monkeypatch.setattr(work, "MAX_PACKET_BYTES", 100)
    result = work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    assert result["capture"]["oversized_ids"] == ["p"]
    assert result["economic"]["population_count"] == 0
    assert _state(path)["records"]["p"]["packet"] is None
    assert result["capture"]["checkpoint_bytes"] < result["capture"]["checkpoint_budget_bytes"]
    assert result["capture"]["source_writes"] == 0 and not result["capture"]["funding_allowed"]


def test_checkpoint_cannot_replace_source_database_or_wal(tmp_db):
    reader = _reader(tmp_db)
    for path in (reader.db_path, Path(str(reader.db_path) + "-wal")):
        with pytest.raises(ValueError, match="must_not_replace_source"):
            work.capture_work(reader, path, now_ms=NOW)
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_future_fee_is_raw_audit_evidence_until_its_arrival_frontier(tmp_db, tmp_path):
    _position(tmp_db, "p")
    oid = _buy(tmp_db, "p")
    tmp_db.execute("INSERT INTO fill_costs(order_id,chain,side,token,signature,router_fee_native,computed_ms) "
                   "VALUES (?,'sol','buy','token-p','tx-p','10',?)", (oid, NOW + 100_000))
    path = tmp_path / "audit.json"
    first = work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    raw = _state(path)["records"]["p"]["packet"]["fill_costs"][0]
    assert raw["router_fee_native"] == "10"
    decoded = first["economic"]["outcomes"][0]["decoded_fill_evidence"][0]
    assert not decoded["available_at_projection_frontier"] and decoded["router_fee_native"] is None
    after = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 120_000)
    decoded = after["economic"]["outcomes"][0]["decoded_fill_evidence"][0]
    assert decoded["available_at_projection_frontier"] and decoded["router_fee_native"] == "10"
    assert _state(path)["records"]["p"]["revision_count"] == 2
    assert after["economic"]["economic_complete_count"] == 0


def test_revision_outbox_survives_checkpoint_to_journal_crash_and_full_backpressure(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(work, "MAX_OUTBOX", 1)
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    tmp_db.execute("UPDATE positions SET realized_native='-200' WHERE position_id='p'")
    changed = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    revision = changed["capture"]["revisions"][0]
    # Simulate process loss after the audit checkpoint but before caller state or
    # canonical journal writes. Another tick replays the durable outbox itself.
    _position(tmp_db, "later", closed=NOW + 61_000)
    reader = _reader(tmp_db)
    resumed = work.capture_work(reader, path, now_ms=NOW + 120_000)
    assert resumed["capture"]["revisions"] == [revision]
    assert resumed["capture"]["selection_deferred"] == "economic_journal_outbox_full"
    assert reader.queries == 0
    assert resumed["economic"]["as_of_ms"] == NOW + 60_000
    assert resumed["capture"]["at_ms"] == NOW + 60_000
    assert resumed["capture"]["checked_ms"] == NOW + 120_000
    assert resumed["capture"]["new_source_observation"] is False
    assert _state(path)["source_observed_ms"] == NOW + 60_000
    assert _state(path)["updated_ms"] == NOW + 120_000
    cached = work.cached_work(path, now_ms=NOW + 121_000, reason="economic_interval_not_due")
    assert cached["economic"]["as_of_ms"] == NOW + 60_000
    assert cached["capture"]["at_ms"] == NOW + 60_000
    assert cached["capture"]["checked_ms"] == NOW + 121_000
    assert cached["capture"]["revisions"] == [revision]
    assert "later" not in _state(path)["records"]
    assert work.acknowledge_revisions(path, {"foreign-id"}, now_ms=NOW + 120_000) == 0
    eid = "economic-revision:" + work._hash(revision)
    assert work.acknowledge_revisions(path, {eid}, now_ms=NOW + 120_000) == 1
    assert work.acknowledge_revisions(path, {eid}, now_ms=NOW + 120_000) == 0
    after = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 180_000)
    assert after["capture"]["selection_deferred"] is None
    assert after["capture"]["new_source_observation"] is True
    assert after["economic"]["as_of_ms"] == NOW + 180_000
    work.capture_work(_reader(tmp_db), path, now_ms=NOW + 240_000)
    assert "later" in _state(path)["records"]


def test_previously_open_episode_does_not_skip_an_unseen_preceding_close(tmp_db, tmp_path):
    _position(tmp_db, "later", closed=None)
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    _position(tmp_db, "earlier", closed=NOW + 1000)
    tmp_db.execute("UPDATE positions SET closed_ms=?,qty='0' WHERE position_id='later'", (NOW + 2000,))
    state = _state(path)
    state["selection_turn"] = 2
    path.write_text(json.dumps(state))
    retried = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    assert retried["capture"]["retry_ids"] == []
    assert retried["capture"]["new_closed_ids"] == ["earlier"]
    assert _state(path)["closed_cursor"][0] == NOW + 1000
    assert len(retried["capture"]["changed_ids"]) + len(retried["capture"]["unchanged_ids"]) <= 1


def test_cached_work_preserves_original_frontier_and_unacknowledged_revision_without_source_read(tmp_db, tmp_path):
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    tmp_db.execute("UPDATE positions SET realized_native='-200' WHERE position_id='p'")
    changed = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    before = path.read_bytes()
    cached = work.cached_work(path, now_ms=NOW + 99_000, reason="paper_entries_need_poll")
    assert cached["economic"]["as_of_ms"] == NOW + 60_000
    assert cached["capture"]["at_ms"] == NOW + 60_000
    assert cached["capture"]["checked_ms"] == NOW + 99_000
    assert not cached["capture"]["new_source_observation"] and cached["capture"]["queries"] == []
    assert cached["capture"]["revisions"] == changed["capture"]["revisions"]
    assert path.read_bytes() == before


def test_legacy_outbox_checkpoint_cannot_refresh_source_clock_from_metadata_update(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(work, "MAX_OUTBOX", 1)
    _position(tmp_db, "p")
    path = tmp_path / "audit.json"
    work.capture_work(_reader(tmp_db), path, now_ms=NOW)
    tmp_db.execute("UPDATE positions SET realized_native='-200' WHERE position_id='p'")
    changed = work.capture_work(_reader(tmp_db), path, now_ms=NOW + 60_000)
    legacy = _state(path)
    del legacy["source_observed_ms"]
    legacy["updated_ms"] = NOW + 120_000  # An old zero-read full-outbox pass.
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    cached = work.cached_work(path, now_ms=NOW + 130_000, reason="paper_entries_need_poll")
    assert cached["economic"]["as_of_ms"] == NOW + 60_000
    assert cached["capture"]["revisions"] == changed["capture"]["revisions"]
    assert path.read_bytes() == before
    reader = _reader(tmp_db)
    resumed = work.capture_work(reader, path, now_ms=NOW + 140_000)
    assert reader.queries == 0 and resumed["economic"]["as_of_ms"] == NOW + 60_000
    assert _state(path)["source_observed_ms"] == NOW + 60_000


def test_empty_legacy_checkpoint_has_unknown_source_clock_and_cached_reads_do_not_create_one(tmp_path):
    path = tmp_path / "audit.json"
    fresh = work.cached_work(path, now_ms=NOW, reason="paper_entries_need_poll")
    assert fresh["economic"]["as_of_ms"] is None and fresh["capture"]["at_ms"] is None
    assert not path.exists()
    legacy = work._load(path, NOW)
    del legacy["source_observed_ms"]
    legacy["updated_ms"] = NOW
    path.write_text(json.dumps(legacy))
    cached = work.cached_work(path, now_ms=NOW + 1000, reason="economic_retry_backoff")
    assert cached["economic"]["as_of_ms"] is None and cached["capture"]["at_ms"] is None
    assert cached["capture"]["checked_ms"] == NOW + 1000


@pytest.mark.parametrize("invalid", [True, -1, "1800000000000"])
def test_invalid_explicit_source_clock_is_not_replaced_by_checkpoint_write_clock(tmp_path, invalid):
    path = tmp_path / "audit.json"
    state = work._load(path, NOW)
    state.update(source_observed_ms=invalid, updated_ms=NOW)
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="invalid_economic_work_source_frontier"):
        work.cached_work(path, now_ms=NOW + 1000, reason="economic_interval_not_due")
