"""Real original wallet review/tape over a migrated disposable SQLite database."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.learning import fixed_wallet_study as fs
from kaiba.learning import wallet_maintenance as wm
from kaiba.learning import wallet_observation_io as io
from kaiba.learning import wallet_observation_tape as tape
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value))


def install_receipts(root, recipe, parent_before_raw, parent_after_raw, *, context_snapshot="2" * 64):
    """Explicit fixture-only installer completion; runtime never creates these."""
    declaration = recipe["declaration"]
    intent = {"version": wm.INSTALL_VERSION, "stage_report_sha256": "1" * 64,
              "parent_before_sha256": hashlib.sha256(parent_before_raw).hexdigest(),
              "prepared_ms": declaration["prepared_ms"], "context_snapshot": context_snapshot,
              "worker_sources": declaration["source_versions"],
              "declaration_sha256": recipe["pointer"]["declaration_sha256"],
              "genesis_sha256": declaration["genesis_sha256"],
              "funding_allowed": False, "canonical_queries": 0, "services_changed": False}
    raw = wm._encoded(intent) + b"\n"
    own = root / wm.DIRECTORY
    (own / "install-intent.json").write_bytes(raw)
    completed = {**intent, "status": "prepared", "completed_ms": declaration["prepared_ms"],
                 "parent_after_sha256": hashlib.sha256(parent_after_raw).hexdigest(),
                 "active_leases": 12, "retired_leases": 10, "family_size": 22,
                 "install_intent_sha256": hashlib.sha256(raw).hexdigest()}
    (own / "install-complete.json").write_bytes(wm._encoded(completed) + b"\n")
    return intent, completed


@pytest.fixture
def case(tmp_path, tmp_db, monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *a, **k: pytest.fail("network forbidden"))
    baseline = int(time.time() * 1000) - 60_000
    db = tmp_path / "kaiba.db"
    root = tmp_path / "learning" / "autolearn"
    root.mkdir(parents=True)
    tmp_db.execute("INSERT INTO kv(key,value,updated_ms) VALUES(?,?,?)",
                   ("retention:swaps:horizon_ms", "0", baseline))
    for chain, wallet in tape.MEMBERS:
        for i, side in enumerate(("buy", "sell")):
            tmp_db.execute("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,usd_value,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", (chain, f"fixture-{chain}-{wallet[:5]}-{i}", baseline - 20_000 + i,
                wallet, "T" * 32, side, "1", "10" if not i else "12", "10" if not i else "12", "gmgn:fixture"))
    tmp_db.commit()
    original_reader = GuardedReader(db)
    fixed = fs.collect_fixed_study(original_reader, [], now_ms=baseline,
                                   study_state=fs.initial_study_state())["study_next_state"]
    lock = root / "worker.lock"
    write(lock, {"pid": os.getpid(), "started_ms": baseline})
    genesis = io.prepare_store(fixed, store_dir=root / "wallet-observation-tape", learning_root=root,
                              now_ms=baseline + 1, eligible_after_ms=baseline + 1)
    lock.unlink()
    now = {"ms": baseline + 60_000}
    parent = {"version": "autolearn/fixture", "wallet_tape_upgrade": {"genesis_sha256": genesis["genesis_sha256"]},
              "wallet_review_state": {"fixed_study_state": fixed}, "last_wallet_review_ms": baseline - 900_000,
              "research_allocation": {"generation": 1, "activation_ms": baseline - 1_000_000},
              "paper_service_upgrade": {"unchanged": True}, "campaign_files": ["frozen.json"],
              "decision_cursor": 1234, "assessment": {"frozen": "keep"}, "pending_lessons": [{"old": "keep"}]}
    write(root / "state.json", parent)
    before_raw = (root / "state.json").read_bytes()
    recipe = wm.prepare_upgrade(parent, state_dir=root, db_path=db, prepared_ms=now["ms"])
    own = root / wm.DIRECTORY
    own.mkdir()
    (own / "declaration.json").write_bytes(recipe["declaration_raw"])
    parent[wm.POINTER] = recipe["pointer"]
    write(root / "state.json", parent)
    install_receipts(root, recipe, before_raw, (root / "state.json").read_bytes())
    readers = []

    class Reader(GuardedReader):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sql = []
            readers.append(self)

        def rows(self, sql, args=()):
            self.sql.append((sql, tuple(args)))
            return super().rows(sql, args)

    worker = wm.WalletMaintenance(root, db, clock=lambda: now["ms"] / 1000, reader_factory=Reader)
    return SimpleNamespace(root=root, db=db, parent=parent, own=own, now=now, worker=worker,
                           readers=readers, conn=tmp_db, genesis=genesis, recipe=recipe)


def test_installer_recipe_is_readonly_and_bound_to_actual_sources_and_genesis(case):
    c = case
    assert c.recipe["declaration"]["source_versions"] == wm.source_versions()
    assert c.recipe["declaration"]["genesis_sha256"] == c.genesis["genesis_sha256"]
    assert c.recipe["declaration"]["policy"]["intake_per_day"] == 20
    parent = copy.deepcopy(c.parent)
    parent.pop(wm.POINTER)
    write(c.root / "state.json", parent)
    before = {str(p): p.read_bytes() for p in c.root.rglob("*") if p.is_file()}
    wm.prepare_upgrade(parent, state_dir=c.root, db_path=c.db, prepared_ms=c.now["ms"])
    assert before == {str(p): p.read_bytes() for p in c.root.rglob("*") if p.is_file()}


def test_explicit_cli_paths_do_not_load_settings_or_credentials(case, monkeypatch, capsys):
    c = case
    monkeypatch.setattr("kaiba.core.config.get_settings", lambda: pytest.fail("settings forbidden with explicit paths"))
    write(c.root / "worker.lock", {"pid": os.getpid(), "started_ms": 1})
    assert wm.main(["--once", "--state-dir", str(c.root), "--db-path", str(c.db)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "locked" and result["source_queries"] == result["provider_calls"] == 0


@pytest.mark.parametrize("fault", ["missing_intent", "missing_complete", "partial_complete", "foreign_intent",
    "foreign_declaration", "foreign_genesis", "foreign_sources", "future", "regressed_clock", "bool_clock",
    "population", "funding_int", "canonical_bool", "services_int", "stage_digest", "context_digest", "extra_field"])
def test_incomplete_or_foreign_installer_receipts_never_admit_source_work(case, fault):
    c = case
    intent_path, complete_path = c.own / "install-intent.json", c.own / "install-complete.json"
    intent, complete = read(intent_path), read(complete_path)
    if fault == "missing_intent":
        intent_path.unlink()
    elif fault == "missing_complete":
        complete_path.unlink()
    elif fault == "partial_complete":
        complete_path.write_text('{"status":')
    else:
        if fault == "foreign_intent":
            complete["install_intent_sha256"] = "f" * 64
        elif fault == "foreign_declaration":
            intent["declaration_sha256"] = "f" * 64
        elif fault == "foreign_genesis":
            intent["genesis_sha256"] = "f" * 64
        elif fault == "foreign_sources":
            intent["worker_sources"] = {}
        elif fault == "future":
            complete["completed_ms"] = c.now["ms"] + 1
        elif fault == "regressed_clock":
            complete["completed_ms"] = intent["prepared_ms"] - 1
        elif fault == "bool_clock":
            complete["completed_ms"] = True
        elif fault == "population":
            complete["active_leases"] = 13
        elif fault == "funding_int":
            intent["funding_allowed"] = 0
        elif fault == "canonical_bool":
            intent["canonical_queries"] = False
        elif fault == "services_int":
            intent["services_changed"] = 0
        elif fault == "stage_digest":
            intent["stage_report_sha256"] = "not-a-digest"
        elif fault == "context_digest":
            intent["context_snapshot"] = "not-a-digest"
        elif fault == "extra_field":
            intent["unrecognized_authority"] = True
        if fault.startswith("foreign_") and fault != "foreign_intent" or fault in {
            "funding_int", "canonical_bool", "services_int", "stage_digest", "context_digest", "extra_field"}:
            # Bind corrupt receipts to each other so their actual semantic checks fire.
            intent_path.write_bytes(wm._encoded(intent) + b"\n")
            complete.update(intent)
            complete["install_intent_sha256"] = hashlib.sha256(intent_path.read_bytes()).hexdigest()
        write(complete_path, complete)
    with pytest.raises(ValueError):
        c.worker.once()
    assert not c.readers and not (c.own / "journal.json").exists()
    assert not (c.root / "latest-wallet-review.json").exists()


def test_real_original_wrapper_review_preserves_paper_and_fixed_anchors(case, monkeypatch):
    c = case
    from kaiba.learning import allocation_runtime
    monkeypatch.setattr(allocation_runtime, "load_context", lambda *a, **k: pytest.fail("PAPER context forbidden"))
    before = read(c.root / "state.json")
    result = c.worker.once()
    assert result["status"] == "published" and result["source_queries"] > 0
    after = read(c.root / "state.json")
    assert {k: v for k, v in after.items() if k not in wm.WALLET_KEYS} == {
        k: v for k, v in before.items() if k not in wm.WALLET_KEYS}
    assert after["wallet_review_state"]["fixed_study_state"]["selections"] == before["wallet_review_state"]["fixed_study_state"]["selections"]
    stored = io.validate_store(store_dir=c.root / "wallet-observation-tape", learning_root=c.root,
                              expected_genesis=c.genesis["genesis_sha256"], now_ms=c.now["ms"])
    assert stored["genesis_sha256"] == c.genesis["genesis_sha256"]
    assert 0 < len(stored["quota"]["attempts"]) <= 10
    assert sum(a["kind"] == "dynamic" for a in stored["quota"]["attempts"]) <= 4
    assert sum(a["charged_rows"] for a in stored["quota"]["attempts"]) <= 10_000
    assert sum(a["charged_bytes"] for a in stored["quota"]["attempts"]) <= 4 * 1024 * 1024
    assert read(c.root / "latest-wallet-review.json")["observed_ms"] == c.now["ms"]
    calls = sum(r.queries for r in c.readers)
    assert c.worker.once()["status"] == "not_due"
    assert sum(r.queries for r in c.readers) == calls


def test_real_original_dynamic_review_shares_ten_reads_and_four_dynamic_slots(case):
    c = case
    from tests.test_autolearn_wallets import _caches
    # Keep the injected collector clock beyond the original wall-clock clamp.
    c.now["ms"] += 300_000
    addresses = _caches(c.db.parent / "claude-runners", n=8, chain="bsc", profile_at=c.now["ms"])
    assert c.worker.once()["status"] == "published"
    stored = io.validate_store(store_dir=c.root / "wallet-observation-tape", learning_root=c.root,
                              expected_genesis=c.genesis["genesis_sha256"], now_ms=c.now["ms"])
    attempts = stored["quota"]["attempts"]
    assert len(attempts) == 10 and sum(a["kind"] == "dynamic" for a in attempts) == 4
    physical = [(sql, args) for reader in c.readers for sql, args in reader.sql
                if sql in (io.LEGACY_SQL, io.PAGE_SQL)]
    assert len(physical) == 10
    assert sum(a["charged_rows"] for a in attempts) <= io.MAX_ROWS
    assert sum(a["charged_bytes"] for a in attempts) <= io.MAX_BYTES
    review = read(c.root / "latest-wallet-review.json")
    assert len(review["observation_queue"]) == len(addresses) == 8
    assert review["intake"]["daily_count"] == 8
    assert c.conn.execute("SELECT COUNT(*) FROM wallets WHERE cohort='research'").fetchone()[0] == 8
    before = stored["quota"]
    c.now["ms"] += 899_999
    assert c.worker.once()["status"] == "not_due"
    assert io.validate_store(store_dir=c.root / "wallet-observation-tape", learning_root=c.root,
        expected_genesis=c.genesis["genesis_sha256"], now_ms=c.now["ms"])["quota"] == before


@pytest.mark.parametrize("pid", [os.getpid(), 999_999_999])
def test_any_existing_owner_lock_is_preserved_without_source_io_or_reclaim(case, pid):
    c = case
    lock = c.root / "worker.lock"
    write(lock, {"pid": pid, "started_ms": 1})
    before = lock.read_bytes()
    assert c.worker.once()["status"] == "locked"
    assert not c.readers and lock.read_bytes() == before
    assert not (c.own / "journal.json").exists()


def test_foreign_same_bytes_lock_replacement_is_refused_and_never_removed(case, monkeypatch):
    c = case
    lock = c.root / "worker.lock"
    foreign = c.root / "foreign.lock"
    def replace_lock(now):
        foreign.write_bytes(lock.read_bytes())
        os.replace(foreign, lock)
        c.worker._owns_lock()
    monkeypatch.setattr(c.worker, "_once", replace_lock)
    with pytest.raises(ValueError, match="worker_lock_changed"):
        c.worker.once()
    assert lock.exists() and not c.readers
    assert read(lock)["owner"] == wm.VERSION


def test_hardlinked_owned_lock_is_refused_and_never_removed(case, monkeypatch):
    c = case
    lock, alias = c.root / "worker.lock", c.root / "lock-alias"
    def add_link(now):
        os.link(lock, alias)
        c.worker._owns_lock()
    monkeypatch.setattr(c.worker, "_once", add_link)
    with pytest.raises(ValueError, match="worker_lock_changed"):
        c.worker.once()
    assert lock.exists() and alias.exists() and not c.readers


def test_wal_pressure_defers_before_source_and_retains_quiet_after_pressure_clears(case):
    c = case
    # Test stat admission without making the real SQLite WAL invalid.
    original = Path.stat
    wal = str(c.db) + "-wal"
    def stat(path, *args, **kwargs):
        if str(path) == wal:
            return SimpleNamespace(st_size=wm.WAL_LIMIT + 1)
        return original(path, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        result = c.worker.once()
    assert result["status"] == "deferred" and result["retry_after_ms"] >= c.now["ms"] + 60_000
    assert not c.readers and not (c.root / "latest-wallet-review.json").exists()
    c.now["ms"] += 59_999
    assert c.worker.once()["status"] == "deferred" and not c.readers


def test_crash_after_pending_never_reinvokes_original_review(case, monkeypatch):
    c = case
    calls = []
    def crash(*args, **kwargs):
        calls.append(1)
        raise KeyboardInterrupt("crash before completed result")
    monkeypatch.setattr(io, "run_review_with_tape", crash)
    with pytest.raises(KeyboardInterrupt):
        c.worker.once()
    assert read(c.own / "journal.json")["status"] == "pending"
    assert c.worker.once()["status"] == "recovery_needed" and calls == [1]
    assert not (c.root / "latest-wallet-review.json").exists()


def test_durable_result_recovers_publication_without_repeating_real_review(case, monkeypatch):
    c = case
    original_publish = c.worker._publish
    monkeypatch.setattr(c.worker, "_publish", lambda *args: (_ for _ in ()).throw(SystemExit("after result")))
    with pytest.raises(SystemExit):
        c.worker.once()
    assert read(c.own / "journal.json")["status"] == "result"
    queries = sum(r.queries for r in c.readers)
    monkeypatch.setattr(c.worker, "_publish", original_publish)
    monkeypatch.setattr(io, "run_review_with_tape", lambda *a, **k: pytest.fail("no replay"))
    result = c.worker.once()
    assert result["status"] == "published" and result["recovered"] and result["source_queries"] == 0
    assert sum(r.queries for r in c.readers) == queries


def test_parent_committed_report_crash_recovers_idempotently(case, monkeypatch):
    c = case
    atomic = wm._atomic
    def crash(path, value, cap):
        if path.name == "latest-wallet-review.json":
            raise SystemExit("between publications")
        return atomic(path, value, cap)
    monkeypatch.setattr(wm, "_atomic", crash)
    with pytest.raises(SystemExit):
        c.worker.once()
    assert read(c.root / "state.json")["last_wallet_review_ms"] == c.now["ms"]
    monkeypatch.setattr(wm, "_atomic", atomic)
    assert c.worker.once()["status"] == "published"


def test_original_owner_advancement_supersedes_unknown_pending_without_retry(case, monkeypatch):
    c = case
    monkeypatch.setattr(io, "run_review_with_tape", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        c.worker.once()
    parent = read(c.root / "state.json")
    parent["last_wallet_review_ms"] = c.now["ms"] + 1
    parent["wallet_review_state"]["owner_advanced"] = True
    write(c.root / "state.json", parent)
    c.now["ms"] += 1
    assert c.worker.once()["status"] == "superseded"
    assert read(c.root / "state.json") == parent
    assert not (c.root / "latest-wallet-review.json").exists()


def test_recovery_merges_only_wallet_keys_preserving_current_other_parent_values(case, monkeypatch):
    c = case
    publish = c.worker._publish
    monkeypatch.setattr(c.worker, "_publish", lambda *a: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        c.worker.once()
    parent = read(c.root / "state.json")
    parent["decision_cursor"] += 100
    parent["new_legitimate_owner_field"] = {"keep": True}
    write(c.root / "state.json", parent)
    monkeypatch.setattr(c.worker, "_publish", publish)
    assert c.worker.once()["status"] == "published"
    after = read(c.root / "state.json")
    assert {k: v for k, v in after.items() if k not in wm.WALLET_KEYS} == {
        k: v for k, v in parent.items() if k not in wm.WALLET_KEYS}


def test_parent_cas_race_refuses_publication_then_recovers_without_source_replay(case, monkeypatch):
    c = case
    owns = c.worker._owns_lock
    raced = []
    def advance_unrelated_parent():
        owns()
        journal_path = c.own / "journal.json"
        if not raced and journal_path.exists() and read(journal_path)["status"] == "result":
            # _publish checks the lock once before its parent read and again at CAS.
            raced.append(1)
        elif raced == [1]:
            parent = read(c.root / "state.json")
            parent["decision_cursor"] += 100
            write(c.root / "state.json", parent)
            raced.append(2)
    monkeypatch.setattr(c.worker, "_owns_lock", advance_unrelated_parent)
    with pytest.raises(ValueError, match="parent_CAS_conflict"):
        c.worker.once()
    assert read(c.own / "journal.json")["status"] == "result"
    assert not (c.root / "latest-wallet-review.json").exists()
    queries = sum(reader.queries for reader in c.readers)
    monkeypatch.setattr(c.worker, "_owns_lock", owns)
    monkeypatch.setattr(io, "run_review_with_tape", lambda *a, **k: pytest.fail("no review replay"))
    assert c.worker.once()["status"] == "published"
    assert read(c.root / "state.json")["decision_cursor"] == c.parent["decision_cursor"] + 100
    assert sum(reader.queries for reader in c.readers) == queries


def test_result_journal_cap_failure_preserves_pending_and_never_replays(case, monkeypatch):
    c = case
    journal = c.worker._journal
    def fail_large_result(value):
        if value["status"] == "result":
            raise ValueError("wallet_maintenance:write_cap:journal.json")
        return journal(value)
    monkeypatch.setattr(c.worker, "_journal", fail_large_result)
    result = c.worker.once()
    assert result["status"] == "recovery_needed"
    assert read(c.own / "journal.json")["status"] == "pending"
    queries = sum(reader.queries for reader in c.readers)
    monkeypatch.setattr(io, "run_review_with_tape", lambda *a, **k: pytest.fail("no review replay"))
    assert c.worker.once()["status"] == "recovery_needed"
    assert sum(reader.queries for reader in c.readers) == queries
    assert not (c.root / "latest-wallet-review.json").exists()


@pytest.mark.parametrize("rollback", [True, False])
def test_published_wallet_frontier_cannot_regress_or_change_at_same_clock(case, monkeypatch, rollback):
    c = case
    assert c.worker.once()["status"] == "published"
    parent = read(c.root / "state.json")
    if rollback:
        parent.update({key: c.parent[key] for key in wm.WALLET_KEYS})
    else:
        parent["wallet_review_state"]["changed_after_publication"] = True
    write(c.root / "state.json", parent)
    queries = sum(reader.queries for reader in c.readers)
    monkeypatch.setattr(io, "run_review_with_tape", lambda *a, **k: pytest.fail("rollback must not create new work"))
    with pytest.raises(ValueError, match="published_wallet_frontier_regressed_or_changed"):
        c.worker.once()
    assert sum(reader.queries for reader in c.readers) == queries


def test_missing_original_tape_has_no_plain_review_fallback(case, monkeypatch):
    c = case
    (c.root / "wallet-observation-tape" / io.STORE_FILE).unlink()
    monkeypatch.setattr(wm.wallets, "run_wallet_review", lambda *a, **k: pytest.fail("plain fallback forbidden"))
    with pytest.raises(SourceDeferred, match="artifact_missing"):
        c.worker.once()
    assert sum(reader.queries for reader in c.readers) == 0
    assert not (c.root / "latest-wallet-review.json").exists()


@pytest.mark.parametrize("kind", ["declaration", "genesis", "journal", "parent_cap", "duplicate_key", "pointer_bool"])
def test_corrupt_bindings_and_caps_fail_closed(case, kind):
    c = case
    if kind == "declaration":
        path = c.own / "declaration.json"
        value = read(path)
        value["source_versions"]["learning/wallet_maintenance.py"] = "0" * 64
        write(path, value)
    elif kind == "genesis":
        parent = read(c.root / "state.json")
        parent["wallet_tape_upgrade"]["genesis_sha256"] = "0" * 64
        write(c.root / "state.json", parent)
    elif kind == "journal":
        write(c.own / "journal.json", {"status": "published"})
    elif kind == "parent_cap":
        with (c.root / "state.json").open("wb") as stream:
            stream.write(b" " * (wm.MAX_PARENT_BYTES + 1))
    elif kind == "duplicate_key":
        path = c.root / "state.json"
        path.write_text(path.read_text()[:-1] + ',"last_wallet_review_ms":0}')
    else:
        parent = read(c.root / "state.json")
        parent[wm.POINTER]["funding_allowed"] = 0
        write(c.root / "state.json", parent)
    with pytest.raises(ValueError):
        c.worker.once()
    assert not c.readers and not (c.root / "latest-wallet-review.json").exists()


def test_real_intake_counter_is_preserved_not_reset(case):
    c = case
    from datetime import UTC, datetime
    day = datetime.fromtimestamp(c.now["ms"] / 1000, UTC).date().isoformat()
    key = "autolearn:wallet-intake:" + day
    c.conn.execute("INSERT INTO kv(key,value,updated_ms) VALUES(?,?,?)", (key, json.dumps({"count": 20}), c.now["ms"]))
    c.conn.commit()
    assert c.worker.once()["status"] == "published"
    assert read(c.root / "latest-wallet-review.json")["intake"]["daily_count"] == 20
    assert json.loads(c.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])["count"] == 20


def test_wal_refusal_from_original_guard_retains_pending_without_fresh_review(case, monkeypatch):
    c = case
    original = io.run_review_with_tape
    def fail_after_admission(*args, **kwargs):
        # Real wrapper remains the path; its canonical reader sees the actual refusal.
        args[1].rows = lambda *a, **k: (_ for _ in ()).throw(SourceDeferred("wal_over_limit:2000000000"))
        return original(*args, **kwargs)
    monkeypatch.setattr(io, "run_review_with_tape", fail_after_admission)
    result = c.worker.once()
    assert result["status"] == "recovery_needed" and result["retry_after_ms"] >= c.now["ms"] + 60_000
    assert not (c.root / "latest-wallet-review.json").exists()
    assert c.worker.once()["status"] == "recovery_needed"
