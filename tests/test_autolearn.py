"""Real orchestrator, PaperBroker, assessment, canonical journal, and isolated books."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import textwrap
from pathlib import Path

import pytest

from kaiba.learning import autolearn
from kaiba.learning.autolearn import AutoLearner
from kaiba.learning.autolearn_io import WAL_LIMIT, GuardedReader, read_opportunities

T0 = 1_800_000_000_000


def _path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def worker(tmp_db, tmp_path, monkeypatch):
    # Historical discovery is the only replaced execution component. PaperBroker,
    # assessments, source reads, playbook writes and journal hash chaining are real.
    monkeypatch.setattr(AutoLearner, "_discovery", lambda *a: {"status": "test_discovery_omitted"})
    now = {"ms": T0}
    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root,
                          groups=[("sm-trenches", "sol")], max_candidates=2,
                          clock=lambda: now["ms"] / 1000)
    return learner, now


def _quote(conn, at, *, price=".001", future=False, missing_decimals=False):
    conn.execute("INSERT INTO tokens(chain,address,decimals,first_seen_ms,migrated_ms) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(chain,address) DO UPDATE SET decimals=excluded.decimals,migrated_ms=excluded.migrated_ms",
                 ("sol", "token", None if missing_decimals else 6, T0 - 1000, T0 - 1000))
    conn.execute("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES (?,?,?,?)",
                 ("sol", at, "100", "fixture-native"))
    clock = at + 1 if future else at
    def measure(value):
        return {"value": value, "receipt": {"observed_at_ms": clock}}
    dossier = {"price_usd": measure(price), "liquidity_usd": measure("1000000"),
               "buy_tax_bps": {"value": "0"}, "sell_tax_bps": {"value": "0"}}
    conn.execute("INSERT INTO token_dossiers(chain,address,built_at_ms,dossier_json) VALUES (?,?,?,?) "
                 "ON CONFLICT(chain,address) DO UPDATE SET built_at_ms=excluded.built_at_ms,dossier_json=excluded.dossier_json",
                 ("sol", "token", at, json.dumps(dossier)))


def _decision(conn, *, did="entry", at=T0 + 1000, stake=1, action="enter"):
    sid = "signal-" + did
    conn.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES (?,?,?,?,?,?)",
                 (sid, "sm-trenches", "sol", "token", at - 1, json.dumps({"holder_count": 75})))
    conn.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (did, at, "sm-trenches", "live", "sol", "token", action, stake,
                                              json.dumps([sid])))


def _run_tick(worker, conn, ms, **quote_kwargs):
    learner, clock = worker
    clock["ms"] = ms
    _quote(conn, ms, **quote_kwargs)
    return learner.once()


def _arms(report):
    return report["campaigns"][0]["assessment"]["arms"]


def _assert_unproven(report):
    assert report["funding_allowed"] is False
    assert all(arm["status"] not in {"PASS", "PAPER_EVIDENCE_READY"} for arm in _arms(report))


def test_admission_clock_worker_uses_actual_admission_before_timed_hold(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(AutoLearner, "_discovery", lambda *args: None)
    clock = {"ms": T0}
    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root,
        groups=[("sm-trenches", "sol")], max_candidates=2, admission_clock=True,
        clock=lambda: clock["ms"] / 1000)
    first = learner.once()
    assert first["status"] == "complete" and first["clock_upgrade"]
    state = _json(learner.state_path)
    manifest = _json(learner.state_dir / state["campaign_files"][0])
    assert manifest["assessment_policy"]["execution_policy"]["ledger_clock"] == "last_revalidated_processing_frontier"
    _decision(tmp_db)
    _quote(tmp_db, T0 + 31_000)
    clock["ms"] = T0 + 81_000
    opened = learner.once()
    assert not opened["paper_errors"] and opened["funding_allowed"] is False
    timed = next(arm for arm in manifest["hypotheses"] if arm["spec"]["exit"]["name"] == "hold60s")
    path = learner._paper().book_path(manifest["campaign_id"], timed["hypothesis_id"])
    def rows(sql):
        conn = sqlite3.connect(path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    assert rows("SELECT opened_ms FROM positions") == [(T0 + 81_000,)]
    assert rows("SELECT side,created_ms FROM orders") == [("buy", T0 + 81_000)]
    clock["ms"] = T0 + 104_000
    _quote(tmp_db, clock["ms"])
    assert not learner.once()["paper_errors"]
    assert rows("SELECT side FROM orders") == [("buy",)]
    clock["ms"] = T0 + 154_000
    _quote(tmp_db, clock["ms"])
    closed = learner.once()
    assert not closed["paper_errors"]
    assert rows("SELECT side,created_ms FROM orders ORDER BY created_ms") == [
        ("buy", T0 + 81_000), ("sell", T0 + 154_000)]
    _assert_unproven(closed)


def test_admission_clock_cannot_exceed_configured_cohort_capacity(tmp_db, tmp_path):
    root = tmp_path / "learning"
    with pytest.raises(ValueError, match="twelve-arm"):
        AutoLearner(_path(tmp_db), root / "worker", learning_root=root,
                    max_candidates=13, admission_clock=True)


def test_empty_bootstrap_freezes_pair_without_inventing_observations_and_journals_once(worker, tmp_db):
    learner, clock = worker
    report = learner.once()
    assert report["status"] == "complete"
    _assert_unproven(report)
    state = _json(learner.state_path)
    assert len(state["campaign_files"]) == 1
    manifest = _json(learner.state_dir / state["campaign_files"][0])
    assert manifest["observation_count"] == 0
    assert manifest["holdout_survivors"] == 0
    assert len(manifest["hypotheses"]) == 2
    assert [arm["spec"]["exit"]["name"] for arm in manifest["hypotheses"]] == ["incumbent", "hold60s"]
    assert all(arm["status"] == "PAPER_ONLY" for arm in manifest["hypotheses"])
    assert state["pending_lessons"] == [] and state["start_journalled"]
    subject = "autolearn:start:" + manifest["campaign_id"]
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", (subject,)).fetchone()[0] == 1
    clock["ms"] += 10_000
    second = learner.once()
    assert _json(learner.state_path)["campaign_files"] == state["campaign_files"]
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", (subject,)).fetchone()[0] == 1
    _assert_unproven(second)
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_default_groups_generate_a_globally_bounded_twelve_arm_campaign(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setattr(AutoLearner, "_discovery", lambda *a: None)
    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root, clock=lambda: T0 / 1000)
    report = learner.once()
    state = _json(learner.state_path)
    manifest = _json(learner.state_dir / state["campaign_files"][0])
    assert len(manifest["hypotheses"]) == 12
    assert manifest["observation_count"] == 0
    assert {arm["spec"]["chain"] for arm in manifest["hypotheses"]} == {"sol", "bsc", "robinhood"}
    assert len(report["campaigns"][0]["paper"]["arms"]) == 12
    assert all(arm["order_count"] == 0 for arm in report["campaigns"][0]["paper"]["arms"])
    _assert_unproven(report)


def test_wal_deferral_precedes_queries_and_persists_manifest_and_sixty_second_backoff(worker):
    learner, clock = worker
    readers = []
    class OversizedWalReader(GuardedReader):
        def _wal_bytes(self):
            return WAL_LIMIT + 1
    def factory(path):
        reader = OversizedWalReader(path)
        readers.append(reader)
        return reader
    learner.reader_factory = factory
    report = learner.once()
    assert report["status"] == "deferred"
    assert report["source_queries"]["count"] == 0
    assert readers[0].queries == 0
    assert not (learner.state_dir / "paper").exists()
    state = _json(learner.state_path)
    assert len(state["campaign_files"]) == 1
    assert (learner.state_dir / state["campaign_files"][0]).is_file()
    assert state["retry_after_ms"] >= T0 + 60_000
    learner.reader_factory = GuardedReader
    clock["ms"] = T0 + 59_000
    again = learner.once()
    assert again["status"] == "deferred" and again["deferred"] == "source_retry_backoff"
    assert again["source_queries"]["count"] == 0
    assert _json(learner.state_path)["retry_after_ms"] == state["retry_after_ms"]
    clock["ms"] = state["retry_after_ms"] + 1
    recovered = learner.once()
    assert recovered["status"] == "complete"
    assert _json(learner.state_path)["campaign_files"] == state["campaign_files"]


def test_real_delayed_entry_and_delayed_hold_exit_remain_isolated_and_journal_idempotently(worker, tmp_db, monkeypatch):
    from kaiba.core import journal

    # The canonical price adapter correctly supplies no unproved venue rate.
    # Supply explicit synthetic venue evidence only for this complete-model
    # journalling positive control; the real adapter/broker/cashflows still run.
    original_quote = autolearn.quote_evidence
    def synthetic_venue(*args, **kwargs):
        quote = original_quote(*args, **kwargs)
        if not quote.get("unavailable_reason"):
            quote.update(venue_fee_bps=125, venue_fee_source="synthetic-test-venue",
                         venue_fee_ts_ms=quote["quote_ts_ms"])
        return quote
    monkeypatch.setattr(autolearn, "quote_evidence", synthetic_venue)
    learner, _ = worker
    learner.once()
    _decision(tmp_db, stake=1)  # Original stake is not enlarged in the canonical decision.
    pending = _run_tick(worker, tmp_db, T0 + 1000)
    assert pending["new_opportunities"] == 1
    assert all(arm["open"] == 0 for arm in pending["campaigns"][0]["paper"]["arms"])
    opened = _run_tick(worker, tmp_db, T0 + 31_000)
    assert opened["new_opportunities"] == 0
    assert all(arm["open"] == 1 for arm in opened["campaigns"][0]["paper"]["arms"])
    assert all(arm["order_count"] == 1 for arm in opened["campaigns"][0]["paper"]["arms"])
    triggered = _run_tick(worker, tmp_db, T0 + 91_000)
    assert triggered["campaigns"][0]["paper"]["outcomes"] == []
    closed = _run_tick(worker, tmp_db, T0 + 103_000)
    outcomes = closed["campaigns"][0]["paper"]["outcomes"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["status"] == "closed" and outcome["source"] == "PaperBroker"
    assert outcome["opened_ms"] == T0 + 31_000 and outcome["closed_ms"] == T0 + 103_000
    assert outcome["entry_latency_ms"] == 30_000 and outcome["exit_latencies_ms"] == [12_000]
    assert outcome["exit_reason"] == "time_exit"
    assert int(outcome["pnl_native"]) == outcome["exit_credit_native"] - outcome["entry_debit_native"] < 0
    assert outcome["cost_model_assumed"] and not outcome["live_cost_verified"]
    # The original main decision and every canonical financial table remain untouched.
    assert tmp_db.execute("SELECT size_base_units FROM decisions WHERE decision_id='entry'").fetchone()[0] == 1
    for table in ("orders", "positions", "trades"):
        assert tmp_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    arms = closed["campaigns"][0]["paper"]["arms"]
    for arm in arms:
        with sqlite3.connect(arm["book_path"]) as conn:
            assert conn.execute("SELECT COUNT(*) FROM orders WHERE mode!='shadow' OR provider!='paper'").fetchone()[0] == 0
            costs = conn.execute("SELECT cost_native FROM positions").fetchall()
            assert costs and all(int(cost[0]) == 300_000_000 for cost in costs)
    subject = "autolearn:" + outcome["outcome_id"]
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", (subject,)).fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (outcome["outcome_id"],)).fetchone()[0] == 1
    repeated = _run_tick(worker, tmp_db, T0 + 104_000)
    assert repeated["campaigns"][0]["paper"]["outcomes"][0]["outcome_id"] == outcome["outcome_id"]
    assert repeated["journal"]["repeated"] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (outcome["outcome_id"],)).fetchone()[0] == 1
    assert journal.verify(tmp_db) == (True, None)
    _assert_unproven(closed)
    _assert_unproven(repeated)


def test_real_adapter_without_venue_fee_retains_paper_fill_without_learning_profit_credit(worker, tmp_db):
    learner, _ = worker
    learner.once()
    _decision(tmp_db)
    _run_tick(worker, tmp_db, T0 + 1000)
    _run_tick(worker, tmp_db, T0 + 31_000)
    _run_tick(worker, tmp_db, T0 + 91_000)
    result = _run_tick(worker, tmp_db, T0 + 103_000)
    outcome = result["campaigns"][0]["paper"]["outcomes"][0]
    assert outcome["status"] == "closed" and outcome["pnl_native"] is not None
    assert not outcome["cost_complete"] and not outcome["evidence_complete"]
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (outcome["outcome_id"],)).fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?",
                          ("autolearn:" + outcome["outcome_id"],)).fetchone()[0] == 0
    _assert_unproven(result)


def test_economic_revision_replays_after_capture_checkpoint_before_caller_ack_crash(worker, tmp_db, monkeypatch):
    from kaiba.learning import economic_work
    learner, clock = worker
    tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,"
                   "cost_native,proceeds_native,realized_native) "
                   "VALUES ('audit-p','sol','token','sm-trenches','live',?,?,'0','100','1000','900','-100')",
                   (T0 - 10_000, T0 - 1000))
    learner.once()
    tmp_db.execute("UPDATE positions SET realized_native='-200' WHERE position_id='audit-p'")
    original = economic_work.capture_work
    def lost_caller(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("simulated_process_loss_after_economic_checkpoint")
    monkeypatch.setattr(economic_work, "capture_work", lost_caller)
    clock["ms"] = T0 + 60_000
    with pytest.raises(SystemExit):
        learner.once()
    checkpoint = learner.state_dir / "economic-work.json"
    keys = list(_json(checkpoint)["revision_outbox"])
    assert len(keys) == 1
    assert not any(r["evidence_id"].startswith("economic-revision:")
                   for r in _json(learner.state_path)["pending_lessons"])
    monkeypatch.setattr(economic_work, "capture_work", original)
    clock["ms"] += 60_000
    result = learner.once()
    assert result["status"] == "complete" and result["economic_revisions_acknowledged"] == 1
    eid = "economic-revision:" + keys[0]
    subject = "autolearn:" + eid
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", (subject,)).fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (eid,)).fetchone()[0] == 0
    assert _json(checkpoint)["revision_outbox"] == {}
    clock["ms"] += 60_000
    learner.once()
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", (subject,)).fetchone()[0] == 1


def test_optional_review_deferral_cannot_prevent_durable_economic_journal_drain(worker, tmp_db, monkeypatch):
    from kaiba.learning import economic_work
    from kaiba.learning.autolearn_io import SourceDeferred
    learner, clock = worker
    monkeypatch.setattr(economic_work, "MAX_OUTBOX", 1)
    tmp_db.execute("INSERT INTO positions(position_id,chain,token,lane,mode,opened_ms,closed_ms,qty,qty_total,"
                   "cost_native,proceeds_native,realized_native) "
                   "VALUES ('audit-p','sol','token','sm-trenches','live',?,?,'0','100','1000','900','-100')",
                   (T0 - 10_000, T0 - 1000))
    learner.once()
    tmp_db.execute("UPDATE positions SET realized_native='-200' WHERE position_id='audit-p'")
    def unavailable_optional(*args, **kwargs):
        raise SourceDeferred("fixture_optional_review_timeout")
    monkeypatch.setattr(AutoLearner, "_scheduled_reviews", unavailable_optional)
    clock["ms"] = T0 + 60_000
    result = learner.once()
    assert result["status"] == "complete" and result["economic_revisions_acknowledged"] == 1
    assert result["review_schedule"]["reason"] == "fixture_optional_review_timeout"
    assert _json(learner.state_dir / "economic-work.json")["revision_outbox"] == {}
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject LIKE 'autolearn:economic-revision:%'").fetchone()[0] == 1
    assert _json(learner.state_path)["review_retry_after_ms"] >= T0 + 120_000
    assert _json(learner.state_path)["retry_after_ms"] == 0


@pytest.mark.parametrize("maintenance", ["economic", "review"])
@pytest.mark.parametrize("failure", ["query_deferred", "write_contention"])
def test_optional_maintenance_backoff_does_not_stop_fresh_paper_entry(worker, tmp_db, monkeypatch, maintenance, failure):
    from kaiba.learning import economic_work
    from kaiba.learning.autolearn_io import SourceDeferred
    learner, _ = worker
    def unavailable(*args, **kwargs):
        if failure == "write_contention":
            raise sqlite3.OperationalError("database is locked")
        raise SourceDeferred("fixture_optional_lookup_timeout")
    if maintenance == "economic":
        monkeypatch.setattr(economic_work, "capture_work", unavailable)
    else:
        monkeypatch.setattr(AutoLearner, "_scheduled_reviews", unavailable)
    first = learner.once()
    assert first["status"] in {"complete", "partial"}
    assert _json(learner.state_path)["retry_after_ms"] == 0
    _decision(tmp_db)
    pending = _run_tick(worker, tmp_db, T0 + 1000)
    assert pending["maintenance"]["deferred_for_pending"]
    assert pending["economic_capture"]["reason"] == "paper_entries_need_poll"
    opened = _run_tick(worker, tmp_db, T0 + 31_000)
    assert all(a["open"] == 1 and a["order_count"] == 1 for a in opened["campaigns"][0]["paper"]["arms"])
    assert all(a["source_observed_ms"] == T0 + 1000 for a in opened["campaigns"][0]["paper"]["assessment_rows"])
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_actual_optional_wallet_intake_lock_cannot_block_next_paper_quote(worker, tmp_db, monkeypatch):
    from kaiba.learning.autolearn_wallets import _intake
    learner, _ = worker
    learner.once()
    def locked_intake(self, reader, state, report, now_ms):
        with sqlite3.connect(reader.db_path, timeout=.25, isolation_level=None) as other:
            other.execute("BEGIN IMMEDIATE")
            try:
                _intake(reader, [], day="2027-01-15", prior_count=0)
            finally:
                other.rollback()
    monkeypatch.setattr(AutoLearner, "_scheduled_reviews", locked_intake)
    failed = _run_tick(worker, tmp_db, T0 + 60_000)
    assert failed["review_schedule"] == {"status": "deferred", "reason": "database is locked"}
    assert _json(learner.state_path)["retry_after_ms"] == 0
    assert _json(learner.state_path)["review_retry_after_ms"] == T0 + 120_000
    _decision(tmp_db, at=T0 + 61_000)
    _run_tick(worker, tmp_db, T0 + 61_000)
    opened = _run_tick(worker, tmp_db, T0 + 91_000)
    assert all(a["open"] == 1 and a["order_count"] == 1 for a in opened["campaigns"][0]["paper"]["arms"])
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.parametrize("maintenance", ["economic", "review", "discovery"])
def test_wal_growth_during_optional_work_keeps_global_sixty_second_pause(worker, monkeypatch, maintenance):
    from kaiba.learning import economic_work
    learner, clock = worker
    learner.once()
    clock["ms"] = T0 + 60_000
    def wal_grows(reader, *args, **kwargs):
        monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
        reader.guard()
    if maintenance == "economic":
        monkeypatch.setattr(economic_work, "capture_work", wal_grows)
    else:
        target = "_scheduled_reviews" if maintenance == "review" else "_discovery"
        monkeypatch.setattr(AutoLearner, target, lambda self, reader, *a, **kw: wal_grows(reader))
    deferred = learner.once()
    assert deferred["status"] == "deferred" and "wal_over_limit:" in deferred["deferred"]
    assert _json(learner.state_path)["retry_after_ms"] == T0 + 120_000
    clock["ms"] += 15_000
    next_tick = learner.once()
    assert next_tick["deferred"] == "source_retry_backoff"
    assert next_tick["source_queries"]["count"] == 0


@pytest.mark.parametrize("maintenance", ["operating_review", "discovery"])
def test_actual_collectors_cannot_hide_wal_backoff_in_result_packets(tmp_db, tmp_path, monkeypatch, maintenance):
    from kaiba.learning import autolearn_discovery, autolearn_review
    clock = {"ms": T0}
    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root,
        groups=[("sm-trenches", "sol")], max_candidates=2, clock=lambda: clock["ms"] / 1000)
    learner.once()
    clock["ms"] = T0 + 60_000
    state = _json(learner.state_path)
    state["last_wallet_review_ms"] = clock["ms"]
    state["last_agent_review_ms"] = 0 if maintenance == "operating_review" else clock["ms"]
    state["discovery_pending"] = maintenance == "discovery"
    autolearn.atomic_json(learner.state_path, state)
    module = autolearn_review if maintenance == "operating_review" else autolearn_discovery
    name = "collect_review" if maintenance == "operating_review" else "collect"
    original = getattr(module, name)
    def wal_grows(reader, *args, **kwargs):
        monkeypatch.setattr(reader, "_wal_bytes", lambda: WAL_LIMIT + 1)
        return original(reader, *args, **kwargs)
    monkeypatch.setattr(module, name, wal_grows)
    report = learner.once()
    assert report["status"] == "deferred" and "wal_over_limit:" in report["deferred"]
    assert _json(learner.state_path)["retry_after_ms"] == T0 + 120_000
    clock["ms"] += 15_000
    next_tick = learner.once()
    assert next_tick["deferred"] == "source_retry_backoff" and next_tick["source_queries"]["count"] == 0


def test_actual_busy_journal_retains_lessons_without_stopping_fresh_paper_poll(worker, tmp_db, monkeypatch):
    learner, clock = worker
    original = autolearn.persist_lessons
    def locked_journal(path, lessons):
        with sqlite3.connect(path, timeout=.25, isolation_level=None) as other:
            other.execute("BEGIN IMMEDIATE")
            try:
                return original(path, lessons)
            finally:
                other.rollback()
    monkeypatch.setattr(autolearn, "persist_lessons", locked_journal)
    failed = learner.once()
    state = _json(learner.state_path)
    pending_ids = {r["evidence_id"] for r in state["pending_lessons"]}
    assert failed["status"] == "partial" and pending_ids
    assert failed["journal_schedule"]["status"] == "deferred"
    assert state["retry_after_ms"] == 0 and state["journal_retry_after_ms"] == T0 + 60_000
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject LIKE 'autolearn:start:%'").fetchone()[0] == 0
    _decision(tmp_db)
    _run_tick(worker, tmp_db, T0 + 1000)
    opened = _run_tick(worker, tmp_db, T0 + 31_000)
    assert all(a["open"] == 1 and a["order_count"] == 1 for a in opened["campaigns"][0]["paper"]["arms"])
    assert pending_ids <= {r["evidence_id"] for r in _json(learner.state_path)["pending_lessons"]}
    monkeypatch.setattr(autolearn, "persist_lessons", original)
    clock["ms"] = T0 + 60_000
    recovered = learner.once()
    assert recovered["journal_schedule"]["status"] == "complete"
    assert _json(learner.state_path)["pending_lessons"] == []
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject LIKE 'autolearn:start:%'").fetchone()[0] == 1
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_pending_deadline_defers_slow_maintenance_but_not_forever(worker, tmp_db, monkeypatch):
    learner, _ = worker
    learner.once()
    reviews = []
    monkeypatch.setattr(AutoLearner, "_scheduled_reviews", lambda *args: reviews.append(args[-1]))
    _decision(tmp_db)
    pending = _run_tick(worker, tmp_db, T0 + 1000)
    assert pending["maintenance"]["deferred_for_pending"] and reviews == []
    # A second decision keeps a new attempt pending after the first deadline.
    _decision(tmp_db, did="second", at=T0 + 110_000)
    second = _run_tick(worker, tmp_db, T0 + 121_001, future=True)
    assert second["maintenance"]["pending_entries"] and not second["maintenance"]["deferred_for_pending"]
    assert reviews == [T0 + 121_001]


def test_source_and_phase_clocks_preserve_actual_read_completion_after_io(worker, tmp_db, monkeypatch):
    learner, clock = worker
    learner.once()
    _decision(tmp_db)
    original = autolearn.read_opportunities
    def slow_read(*args, **kwargs):
        result = original(*args, **kwargs)
        clock["ms"] += 7000
        return result
    monkeypatch.setattr(autolearn, "read_opportunities", slow_read)
    report = _run_tick(worker, tmp_db, T0 + 1000)
    assert report["source_observed_ms"] == T0 + 8000
    source = next(p for p in report["phases"] if p["name"] == "source_opportunities")
    assert source["elapsed_ms"] == 7000 and source["completed_ms"] == T0 + 8000
    for arm in report["campaigns"][0]["paper"]["arms"]:
        with sqlite3.connect(arm["book_path"]) as conn:
            row = conn.execute("SELECT source_observed_ms,candidate_received_ms FROM candidate_entries").fetchone()
            assert row == (T0 + 8000, T0 + 8000)


@pytest.mark.parametrize("quote_changes", [{"future": True}, {"missing_decimals": True}])
def test_unusable_source_quote_cannot_create_inventory_or_pass_assessment(worker, tmp_db, quote_changes):
    learner, _ = worker
    learner.once()
    _decision(tmp_db)
    result = _run_tick(worker, tmp_db, T0 + 31_000, **quote_changes)
    assert result["captured_quote_count"] == 0
    assert all(arm["order_count"] == 0 for arm in result["campaigns"][0]["paper"]["arms"])
    _assert_unproven(result)


def test_missing_initial_quote_recovers_to_declared_flat_stake_on_fresh_delayed_quote(worker, tmp_db):
    learner, _ = worker
    learner.once()
    _decision(tmp_db, stake=1)
    pending = _run_tick(worker, tmp_db, T0 + 1000, missing_decimals=True)
    assert pending["captured_quote_count"] == 0
    assert all(arm["order_count"] == 0 for arm in pending["campaigns"][0]["paper"]["arms"])
    recovered = _run_tick(worker, tmp_db, T0 + 31_000)
    arms = recovered["campaigns"][0]["paper"]["arms"]
    assert all(arm["open"] == 1 for arm in arms)
    for arm in arms:
        with sqlite3.connect(arm["book_path"]) as conn:
            assert conn.execute("SELECT cost_native FROM positions").fetchone()[0] == "300000000"
    assert tmp_db.execute("SELECT size_base_units FROM decisions WHERE decision_id='entry'").fetchone()[0] == 1
    _assert_unproven(recovered)


def test_partial_source_read_is_deferred_and_not_used_as_a_training_sample(worker, tmp_db):
    learner, clock = worker
    learner.once()
    _decision(tmp_db, did="entry1")
    _decision(tmp_db, did="entry2", at=T0 + 2000)
    learner.reader_factory = lambda path: GuardedReader(path, max_rows=1)
    clock["ms"] = T0 + 31_000
    result = learner.once()
    assert result["status"] == "deferred"
    assert "source_row_cap" in result["deferred"]
    assert result["campaigns"] == []
    assert result["funding_allowed"] is False
    assert _json(learner.state_path)["decision_cursor"] == 0


def _historical_boundary_case(worker, conn):
    learner, _ = worker
    conn.executemany(
        "INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(f"historical-{i}", T0 - 1000, "sm-trenches", "live", "sol", "token", "enter", 1)
         for i in range(500)],
    )
    historical_max = conn.execute("SELECT MAX(rowid) FROM decisions").fetchone()[0]
    starts = []
    class BoundaryWitnessReader(GuardedReader):
        def rows(self, sql, args=()):
            if "FROM decisions WHERE rowid>?" in sql:
                starts.append(args[0])
                assert args[0] >= historical_max, "opportunity query scanned the pre-campaign prefix"
            return super().rows(sql, args)
    learner.reader_factory = BoundaryWitnessReader
    initial = learner.once()
    assert initial["status"] == "complete" and initial["new_opportunities"] == 0
    assert _json(learner.state_path)["decision_cursor"] >= historical_max
    # A late-written old decision is still historical despite its newer rowid.
    _decision(conn, did="backfilled-old", at=T0 - 5)
    _decision(conn, did="fresh-entry", at=T0 + 1000)
    pending = _run_tick(worker, conn, T0 + 1000)
    assert pending["new_opportunities"] == 1 and pending["source_skips"] == []
    opened = _run_tick(worker, conn, T0 + 31_000)
    assert all(arm["open"] == 1 for arm in opened["campaigns"][0]["paper"]["arms"])
    for arm in opened["campaigns"][0]["paper"]["arms"]:
        with sqlite3.connect(arm["book_path"]) as book:
            assert book.execute("SELECT signal_id FROM candidate_entries").fetchall() == [("fresh-entry",)]
    assert len(starts) == 3 and all(start >= historical_max for start in starts)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_source_freeze_boundary_skips_large_old_prefix_and_rejects_late_historical_backfills(worker, tmp_db):
    _historical_boundary_case(worker, tmp_db)


def test_source_boundary_regression_catches_the_removed_initialization_mutation(worker, tmp_db, monkeypatch):
    source = textwrap.dedent(inspect.getsource(AutoLearner._once))
    mutation = source.replace('if not state.get("source_initialized"):', "if False:", 1)
    assert mutation != source
    namespace = dict(vars(autolearn))
    exec(compile(mutation, "<offline-source-boundary-mutation>", "exec"), namespace)
    monkeypatch.setattr(AutoLearner, "_once", namespace["_once"])
    # The independent reader witness fails before the historical-prefix query;
    # no slow database or fabricated broker result is needed to catch this defect.
    with pytest.raises(AssertionError, match="scanned the pre-campaign prefix"):
        _historical_boundary_case(worker, tmp_db)


def test_default_polling_reader_seeks_rowid_and_preserves_eligible_population(tmp_db, tmp_path, monkeypatch):
    tmp_db.executemany(
        "INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(f"old-plan-{i}", T0 - 1000, "sm-trenches", "live", "sol", "token", "enter", 1)
         for i in range(500)],
    )
    boundary = tmp_db.execute("SELECT MAX(rowid) FROM decisions").fetchone()[0]
    for did, chain, action, size in (("fresh-one", "sol", "enter", 1), ("other-chain", "bsc", "enter", 1),
                                   ("fresh-skip", "sol", "skip", 0), ("fresh-two", "sol", "enter", 1)):
        tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units) "
                       "VALUES (?,?,?,?,?,?,?,?)", (did, T0 + 1000, "sm-trenches", "live", chain, "token", action, size))
    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root)
    reader = learner.reader_factory(_path(tmp_db))
    assert isinstance(reader, autolearn.PollingReader)
    sql = ("SELECT rowid AS source_rowid,decision_id,ts_ms,lane,chain,token,action,size_base_units,signals_json "
           "FROM decisions WHERE rowid>? AND ts_ms>? AND ts_ms<=? AND lane IN (?) AND chain IN (?) "
           "ORDER BY rowid LIMIT ?")
    args = (boundary, T0, T0 + 2000, "sm-trenches", "sol", 40)
    def require_cursor_seek():
        plan = " ".join(row[3] for row in reader.q("EXPLAIN QUERY PLAN " + sql, args))
        assert "INTEGER PRIMARY KEY" in plan.upper(), "opportunity polling did not seek the rowid cursor"
        assert "idx_decisions_lane" not in plan.lower()
        assert "TEMP B-TREE" not in plan.upper()
    require_cursor_seek()
    baseline = GuardedReader(_path(tmp_db))
    assert reader.rows(sql, args) == baseline.rows(sql, args)
    selection = {"after_rowid": boundary, "selected_ms": T0, "now_ms": T0 + 2000,
                 "lanes": ["sm-trenches"], "chains": ["sol"]}
    actual = read_opportunities(reader, **selection)
    assert actual == read_opportunities(baseline, **selection)
    assert [row["decision_id"] for row in actual["opportunities"]] == ["fresh-one", "fresh-two"]
    assert actual["skipped"] == [{"decision_id": "fresh-skip", "reason": "incumbent_skipped_or_unsized"}]
    # Removing only the SQL rewrite retains all row/price logic yet reintroduces
    # the expensive lane-index/temp-sort plan, which the independent EXPLAIN
    # witness detects. No timing benchmark or large database copy is required.
    with monkeypatch.context() as mutation:
        mutation.setattr(autolearn.PollingReader, "_read", GuardedReader._read)
        with pytest.raises(AssertionError, match="did not seek the rowid cursor"):
            require_cursor_seek()


def test_changed_frozen_manifest_returns_partial_and_never_pass(worker):
    learner, clock = worker
    learner.once()
    state = _json(learner.state_path)
    path = learner.state_dir / state["campaign_files"][0]
    manifest = _json(path)
    manifest["hypotheses"][0]["spec_hash"] = "changed-frozen-identity"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    clock["ms"] += 1000
    result = learner.once()
    assert result["status"] == "partial"
    assert result.get("error") or result.get("paper_errors") or result.get("deferred")
    assert result["funding_allowed"] is False
    for campaign in result["campaigns"]:
        assert all(arm["status"] not in {"PASS", "PAPER_EVIDENCE_READY"}
                   for arm in campaign["assessment"]["arms"])


def test_journal_acknowledgment_crash_resumes_pending_state_without_duplicate_start_lesson(worker, tmp_db, monkeypatch):
    from kaiba.core import journal

    learner, clock = worker
    original = autolearn.persist_lessons
    def after_durable_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("injected acknowledgment interruption")
    monkeypatch.setattr(autolearn, "persist_lessons", after_durable_write)
    first = learner.once()
    assert first["status"] == "partial"
    state = _json(learner.state_path)
    assert state["pending_lessons"]
    assert not state.get("start_journalled")
    eid = state["pending_lessons"][0]["evidence_id"]
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", ("autolearn:" + eid,)).fetchone()[0] == 1
    expected = {row["evidence_id"] for row in state["pending_lessons"]}
    already_written = {key for key in expected if tmp_db.execute(
        "SELECT 1 FROM journal WHERE subject=?", ("autolearn:" + key,)).fetchone()}
    monkeypatch.setattr(autolearn, "persist_lessons", original)
    assert state["retry_after_ms"] == 0
    clock["ms"] = state["journal_retry_after_ms"] + 1
    resumed = learner.once()
    assert resumed["status"] == "complete"
    assert resumed["journal"]["repeated"] == len(already_written)
    assert {r["evidence_id"] for r in resumed["journal"]["added"]} == expected - already_written
    for evidence_id in expected:
        assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?",
                              ("autolearn:" + evidence_id,)).fetchone()[0] == 1
    assert _json(learner.state_path)["pending_lessons"] == []
    assert tmp_db.execute("SELECT COUNT(*) FROM journal WHERE subject=?", ("autolearn:" + eid,)).fetchone()[0] == 1
    assert journal.verify(tmp_db) == (True, None)


@pytest.mark.parametrize("lock_contents", [
    {},
    {"pid": os.getpid()},
])
def test_existing_exclusive_lock_prevents_even_manifest_or_journal_writes(worker, tmp_db, lock_contents):
    learner, _ = worker
    lock = learner.state_dir / "worker.lock"
    lock.write_text(json.dumps(lock_contents), encoding="utf-8")
    result = learner.once()
    assert result == {"status": "locked", "funding_allowed": False}
    assert not learner.state_path.exists()
    assert lock.exists()
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0


def test_real_process_liveness_probe_reports_current_process_alive_without_terminating_it():
    assert autolearn._pid_alive(os.getpid()) is True
    with pytest.raises(ValueError, match="invalid worker PID"):
        autolearn._pid_alive(0)


def test_pending_and_open_paper_inventory_defer_expensive_discovery(worker, tmp_db, monkeypatch):
    learner, _ = worker
    calls = []
    def discovery(self, reader, state, now):
        calls.append(now)
        return {"status": "test_idle_discovery"}
    monkeypatch.setattr(AutoLearner, "_discovery", discovery)
    idle = learner.once()
    assert calls == [T0] and idle["discovery"]["status"] == "test_idle_discovery"
    _decision(tmp_db)
    pending = _run_tick(worker, tmp_db, T0 + 1000)
    assert pending["discovery"] == {"status": "deferred", "reason": "prospective_inventory_needs_poll"}
    assert calls == [T0]
    opened = _run_tick(worker, tmp_db, T0 + 31_000)
    assert all(arm["open"] == 1 for arm in opened["campaigns"][0]["paper"]["arms"])
    assert opened["discovery"] == {"status": "deferred", "reason": "prospective_inventory_needs_poll"}
    assert calls == [T0]


@pytest.mark.parametrize("candidate_cap,groups,expected_search_cap", [
    (12, [("sm-trenches", chain) for chain in ("sol", "bsc", "robinhood")], 6),
    (18, [(lane, chain) for lane in ("sm-trenches", "trusted-copy")
          for chain in ("sol", "bsc", "robinhood")], 1),
])
def test_discovery_factory_uses_only_remaining_global_capacity_without_adding_empty_evidence(
    tmp_db, tmp_path, monkeypatch, candidate_cap, groups, expected_search_cap,
):
    from kaiba.learning import strategy_campaign

    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root,
                          groups=groups, max_candidates=candidate_cap, clock=lambda: T0 / 1000)
    state = {"assessment": {}, "pending_lessons": []}
    frozen = learner._bootstrap(state, T0)
    assert sum(len(manifest["hypotheses"]) for manifest in frozen) == candidate_cap
    # The real resumable collector reads the empty migrated source; the real
    # empty-data campaign generator then obeys the remaining global capacity.
    original = strategy_campaign.generate_campaign
    limits = []
    def observe_factory(*args, **kwargs):
        limits.append((kwargs["max_candidates"], kwargs["trained_only"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(strategy_campaign, "generate_campaign", observe_factory)
    discovery = learner._discovery(GuardedReader(_path(tmp_db)), state, T0 + 7 * autolearn.DAY_MS)
    assert limits == [(expected_search_cap, True)]
    assert discovery["observations"] == 0 and discovery["funding_allowed"] is False
    assert len(state["campaign_files"]) == 1
    assert _json(learner.state_dir / "latest-discovery.json")["candidate_manifest"]["hypotheses"] == []


def test_discovery_collection_resumes_before_factory_and_queues_one_complete_trial_journal(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning import autolearn_discovery, strategy_campaign

    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root, clock=lambda: T0 / 1000)
    state = {"assessment": {}, "pending_lessons": []}
    learner._bootstrap(state, T0)
    at = T0 + 7 * autolearn.DAY_MS
    collections = [
        {"status": "collecting", "terminal": False, "cursor": 10, "roster_hash": "frozen-roster",
         "observations": []},
        {"status": "complete", "terminal": False, "cursor": 10, "roster_hash": "frozen-roster",
         "observations": []},
    ]
    collector_calls = []
    def collect(*args, **kwargs):
        collector_calls.append(kwargs["now_ms"])
        return collections.pop(0)
    monkeypatch.setattr(autolearn_discovery, "collect", collect)
    original = strategy_campaign.generate_campaign
    factory_calls = []
    def factory(*args, **kwargs):
        factory_calls.append((kwargs["max_candidates"], kwargs["trained_only"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(strategy_campaign, "generate_campaign", factory)
    reader = GuardedReader(_path(tmp_db))
    partial = learner._discovery(reader, state, at)
    assert partial["status"] == "collecting" and "observations" not in partial
    assert state["discovery_pending"] is True
    assert "last_discovery_ms" not in state
    assert factory_calls == [] and state["pending_lessons"] == []
    complete = learner._discovery(reader, state, at + 30_000)
    assert complete["status"] == "complete" and complete["observations"] == 0
    assert factory_calls == [(6, True)]
    assert state["discovery_pending"] is False and state["last_discovery_ms"] == at + 30_000
    assert state["discovery_version"] == autolearn_discovery.VERSION
    assert len(state["pending_lessons"]) == 1
    lesson = state["pending_lessons"][0]
    assert lesson["evidence_id"].startswith("discovery:")
    body = json.loads(lesson["body"])
    assert body["type"] == "autolearn_discovery" and body["roster_hash"] == "frozen-roster"
    assert body["funding_allowed"] is False and body["observations"] == 0
    assert learner._discovery(reader, state, at + 31_000) is None
    assert collector_calls == [at, at + 30_000] and factory_calls == [(6, True)]
    assert len(state["pending_lessons"]) == 1


def test_terminal_discovery_collection_does_not_release_samples_to_the_factory(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning import autolearn_discovery, strategy_campaign

    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root, clock=lambda: T0 / 1000)
    state = {"assessment": {}, "pending_lessons": []}
    learner._bootstrap(state, T0)
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs["now_ms"])
        return {"status": "unavailable", "terminal": True, "reason": "source_evidence_changed",
                "observations": [], "verdict": "UNPROVEN", "funding_allowed": False}
    def forbidden_factory(*args, **kwargs):
        raise AssertionError("terminal discovery must not create hypotheses")
    monkeypatch.setattr(autolearn_discovery, "collect", collect)
    monkeypatch.setattr(strategy_campaign, "generate_campaign", forbidden_factory)
    reader = GuardedReader(_path(tmp_db))
    result = learner._discovery(reader, state, T0)
    assert result["terminal"] and result["reason"] == "source_evidence_changed"
    assert "observations" not in result
    assert state["discovery_pending"] is False and state["last_discovery_ms"] == T0
    assert len(state["pending_lessons"]) == 1
    body = json.loads(state["pending_lessons"][0]["body"])
    assert body["type"] == "autolearn_discovery_invalidated"
    assert body["verdict"] == "UNPROVEN" and body["funding_allowed"] is False
    assert "observations" not in body
    assert learner._discovery(reader, state, T0 + 1000) is None
    assert calls == [T0]


def test_code_changed_discovery_preserves_invalidated_work_and_refreezes_next_idle_tick(tmp_db, tmp_path, monkeypatch):
    from kaiba.learning import autolearn_discovery, strategy_campaign

    root = tmp_path / "learning"
    learner = AutoLearner(_path(tmp_db), root / "worker", learning_root=root, clock=lambda: T0 / 1000)
    state = {"assessment": {}, "pending_lessons": []}
    learner._bootstrap(state, T0)
    original_campaign_files = list(state["campaign_files"])
    state.update(discovery_pending=True, discovery_version=autolearn_discovery.VERSION)
    work_path = learner.state_dir / "discovery-work.json"
    old_observations = [{"old_unvalidated_observation": 1}, {"old_unvalidated_observation": 2}]
    old_work = {"old_frozen_policy": "old-version", "observations": old_observations}
    work_path.write_text(json.dumps(old_work), encoding="utf-8")
    collector_calls = []
    def collect(reader, path, **kwargs):
        collector_calls.append(kwargs["now_ms"])
        if len(collector_calls) == 1:
            assert _json(path) == old_work
            return {"status": "collecting", "terminal": True, "verdict": "UNPROVEN",
                    "error": "frozen_discovery_code_or_policy_changed", "counts": {"priced": 2},
                    "observations": old_observations, "funding_allowed": False}
        assert not path.exists()
        assert _json(learner.state_dir / "previous-discovery-work.json") == old_work
        return {"status": "complete", "terminal": False, "observations": [],
                "roster_hash": "separately-frozen-new-roster", "funding_allowed": False}
    monkeypatch.setattr(autolearn_discovery, "collect", collect)
    original = strategy_campaign.generate_campaign
    factory_calls = []
    def factory(observations, *args, **kwargs):
        assert observations == [], "invalidated observations were released to the strategy factory"
        factory_calls.append((kwargs["max_candidates"], kwargs["trained_only"]))
        return original(observations, *args, **kwargs)
    monkeypatch.setattr(strategy_campaign, "generate_campaign", factory)
    reader = GuardedReader(_path(tmp_db))
    invalid = learner._discovery(reader, state, T0)
    assert invalid["terminal"] and "observations" not in invalid
    assert factory_calls == []
    assert state["discovery_pending"] is False and state["discovery_version"] is None
    assert _json(work_path) == old_work
    assert len(state["pending_lessons"]) == 1
    invalidation = dict(state["pending_lessons"][0])
    invalid_body = json.loads(invalidation["body"])
    assert invalid_body["type"] == "autolearn_discovery_invalidated"
    assert invalid_body["funding_allowed"] is False and "observations" not in invalid_body
    assert invalid_body["counts"]["priced"] == 2 and invalid_body["verdict"] == "UNPROVEN"
    fresh = learner._discovery(reader, state, T0 + 1000)
    assert fresh["status"] == "complete" and fresh["observations"] == 0
    assert collector_calls == [T0, T0 + 1000] and factory_calls == [(6, True)]
    assert len(state["pending_lessons"]) == 2 and state["pending_lessons"][0] == invalidation
    assert json.loads(state["pending_lessons"][1]["body"])["type"] == "autolearn_discovery"
    assert state["campaign_files"] == original_campaign_files
    assert learner._discovery(reader, state, T0 + 2000) is None
    assert len(collector_calls) == 2 and len(factory_calls) == 1


def test_corrupt_durable_state_is_not_silently_reset_and_lock_is_released(worker):
    learner, _ = worker
    learner.state_path.write_text("not valid JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        learner.once()
    assert learner.state_path.read_text() == "not valid JSON"
    assert not (learner.state_dir / "worker.lock").exists()


def test_state_path_cannot_escape_learning_root_or_contain_the_source_database(tmp_db, tmp_path):
    root = tmp_path / "learning"
    with pytest.raises(ValueError, match="explicit research learning root"):
        AutoLearner(_path(tmp_db), tmp_path / "elsewhere", learning_root=root)
    with pytest.raises(ValueError, match="source database must not be inside"):
        AutoLearner(root / "state" / "source" / "main.db", root / "state", learning_root=root)
