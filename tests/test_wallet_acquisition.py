"""Paid-dispatch admission is tested offline, including interruption after COMMIT."""
from __future__ import annotations

import ast
import builtins
import json
import os
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain
from kaiba.ingest import backfill
from kaiba.learning import wallet_acquisition as wa
from kaiba.learning.autolearn_io import SourceDeferred
from kaiba.ops import scheduler

NOW = 1_800_000_000_000
WALLET = "F5T3fScUpbET1Xp9bGJDYNjAiNYxVkG2inJQ53KuykSc"
OTHER = "A" * 32


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _nominee(address=WALLET):
    return {"chain": "sol", "address": address, "selected_ms": NOW - 1000,
        "evidence_id": "wallet-observe:fixture", "selection_hash": "a" * 64,
        "nominations_hash": "b" * 64, "registry_checked_ms": NOW,
        "nominations": [{"observed_ms": NOW - 2000, "receipt_hash": "c" * 64}]}


def _review(rows=None):
    rows = [_nominee()] if rows is None else rows
    return {"observed_ms": NOW - 500, "acquisition_learning": {"status": "complete"},
        "acquisition_queue": rows, "acquisition_candidates": [
            {**r, "exclusions": [], "status": "manual_probe_required"} for r in rows]}


def _wallet(conn, address=WALLET, cohort="research", tags="[]", meta="{}"):
    conn.execute("INSERT INTO wallets(chain,address,name,source,tags_json,meta_json,first_seen_ms,last_seen_ms,cohort) "
                 "VALUES ('sol',?,NULL,'codex-autolearn-research',?,?,1,2,?)", (address, tags, meta, cohort))


def _state(conn):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (wa.ATTEMPT_KEY,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.fixture
def setup(tmp_db, tmp_path, monkeypatch):
    _wallet(tmp_db)
    params = {"wallets_per_run": 2, "wallets_per_day": 20, "pages": 1, "fresh": True,
              "cohorts": "research,tracked,trusted_copy", "max_credits_per_run": 250}
    config = scheduler.ScheduleConfig(jobs={"wallet_tracked": scheduler.JobConfig(
        enabled=True, interval_s=600, timeout_s=300, **params)})
    ctx = scheduler.JobContext("wallet_tracked", tmp_db, params, config, NOW, NOW + 300_000,
                               clock=lambda: NOW / 1000)
    tmp_db.execute("INSERT INTO ops_jobs(name,enabled,interval_s,timeout_s,updated_ms) VALUES(?,1,600,300,?)",
                   (ctx.name, NOW))
    lock = scheduler.default_lock_dir() / "wallet_tracked.lock"
    _write(lock, {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat_ms": NOW})
    review = tmp_path / "learning/autolearn/latest-wallet-review.json"
    _write(review, _review())
    # Offline allowance fixture; production still uses the existing configured cap.
    monkeypatch.setattr(wa.helius, "monthly_allowance", lambda: 1_000_000)
    monkeypatch.setattr(wa.helius, "now_ms", lambda: NOW)
    return SimpleNamespace(conn=tmp_db, ctx=ctx, review=review, lock=lock,
        db=Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))


def _run(s, **kw):
    return wa.run_selected_under_job(s.ctx, allow=2, room=SimpleNamespace(credits=250),
        cohorts=["research", "tracked", "trusted_copy"], review_path=s.review, **kw)


def _fake_report(address=WALLET, *, error=None):
    result = backfill.WalletResult(wallet=address, chain=Chain.SOL, pages=1, meta_pages=1,
                                    swaps_written=2, transactions=3, error=error)
    return backfill.BackfillReport(chain=Chain.SOL, wallets=1, pages=1, transactions=3,
        swaps_written=2, credits_spent=110, results=[result], wallets_failed=int(error is not None))


def _backend(monkeypatch, s, *, fail=False):
    calls = []
    def dispatch(conn, chain, **kw):
        assert not conn.in_transaction
        # A SECOND handle sees both debits BEFORE any response/dispatch-side work.
        with sqlite3.connect(s.db) as independent:
            assert independent.execute("SELECT units,credits FROM ops_quota WHERE job='wallet_tracked'").fetchone() == (1, 250)
            state = _state(independent)
            assert len(state["attempts"]) == 1
            assert next(iter(state["attempts"].values()))["status"] == "dispatch_unknown"
        assert chain is Chain.SOL and kw == {"wallets": [WALLET], "pages": 1, "page_limit": 100,
            "fresh": True, "with_meta": True, "rebuild_buyers": False, "max_credits": 250}
        calls.append(kw)
        if fail:
            raise KeyboardInterrupt("offline simulated hard interruption")
        return _fake_report()
    monkeypatch.setattr(backfill, "backfill_wallets", dispatch)
    return calls


def test_commit_before_dispatch_no_double_accounting_and_existing_identity_unchanged(setup, monkeypatch):
    s = setup
    before = dict(s.conn.execute("SELECT * FROM wallets").fetchone())
    lock_before = s.lock.read_bytes()
    calls = _backend(monkeypatch, s)
    result = _run(s)
    assert len(calls) == 1 and result["credits_today"] == 250 and result["used_today"] == 1
    assert result["graded"] == 0 and result["refund"] == 0 and result["ordinary_slots_unused"] == 1
    assert result["acquisition_status"] == "returned" and result["billing_verified"] is False
    assert result["full_history_proven"] is False and result["funding_allowed"] is False
    assert tuple(s.conn.execute("SELECT units,credits FROM ops_quota").fetchone()) == (1, 250)
    assert dict(s.conn.execute("SELECT * FROM wallets").fetchone()) == before
    assert s.lock.read_bytes() == lock_before
    assert s.conn.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 0
    assert _run(s)["reason"] == "selection_already_admitted_no_automatic_retry"
    assert len(calls) == 1


def test_interruption_retains_quota_and_immutable_attempt_across_review_refresh_and_day(setup, monkeypatch):
    s = setup
    calls = _backend(monkeypatch, s, fail=True)
    with pytest.raises(KeyboardInterrupt):
        _run(s)
    state = _state(s.conn)
    attempt = next(iter(state["attempts"].values()))
    assert attempt["status"] == "dispatch_unknown" and attempt["selection"]["selected_ms"] == NOW - 1000
    review = _review()
    review["observed_ms"] = NOW
    _write(s.review, review)
    assert _run(s)["reason"] == "selection_already_admitted_no_automatic_retry"
    # A subsequent UTC day does not create a different ID for the same frozen selection.
    s.ctx.clock = lambda: (NOW + wa.DAY_MS) / 1000
    s.ctx.deadline_ms += wa.DAY_MS
    review["observed_ms"] = NOW + wa.DAY_MS
    review["acquisition_queue"][0]["registry_checked_ms"] = NOW + wa.DAY_MS
    _write(s.review, review)
    _write(s.lock, {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat_ms": NOW + wa.DAY_MS})
    assert _run(s)["reason"] == "selection_already_admitted_no_automatic_retry"
    assert len(calls) == 1 and _state(s.conn) == state
    assert tuple(s.conn.execute("SELECT units,credits FROM ops_quota").fetchone()) == (1, 250)


def test_real_backfill_normalizes_recorded_source_with_no_grades_or_rebuild(setup, monkeypatch):
    s = setup
    fixtures = Path(__file__).parent / "fixtures/backfill"
    enhanced = json.loads((fixtures / "enhanced_page.json").read_text())
    raw = json.loads((fixtures / "raw_page.json").read_text())
    calls = []
    def page(address, **kw):
        assert address == WALLET and kw["limit"] == 100 and not kw["conn"].in_transaction
        calls.append("enhanced")
        wa.helius.record_spend(wa.helius.ROUTE_ENHANCED, 100, conn=kw["conn"], ts_ms=NOW)
        return enhanced, SimpleNamespace(note=None)
    def metadata(address, **kw):
        assert address == WALLET and kw["limit"] == 100 and not kw["conn"].in_transaction
        calls.append("metadata")
        wa.helius.record_spend(wa.helius.ROUTE_TRANSACTIONS, 10, conn=kw["conn"], ts_ms=NOW)
        return raw, SimpleNamespace(note=None)
    monkeypatch.setattr(wa.helius, "get_enhanced_transactions", page)
    monkeypatch.setattr(wa.helius, "get_transactions_for_address", metadata)
    result = _run(s)
    assert calls == ["enhanced", "metadata"] and result["swaps_written"] > 0
    assert result["credits_reserved"] == 250 and result["backfill_shared_ledger_delta"] == 110
    assert s.conn.execute("SELECT COUNT(*) FROM swaps WHERE wallet=?", (WALLET,)).fetchone()[0] > 0
    cursor = json.loads(s.conn.execute("SELECT value FROM kv WHERE key=?", (backfill.cursor_key(Chain.SOL, WALLET),)).fetchone()[0])
    assert cursor["pages"] == 1
    assert s.conn.execute("SELECT COUNT(*) FROM wallet_scores").fetchone()[0] == 0
    assert s.conn.execute("SELECT COUNT(*) FROM first_buyers").fetchone()[0] == 0


@pytest.mark.parametrize("change,reason", [
    ("foreign_lock", "existing_job_lock_not_held"), ("future_lock", "existing_job_lock_not_held"),
    ("disabled_db", "current_job_disabled_or_unknown"), ("count_full", "count_credit_or_day_changed"),
    ("floor", "monthly_floor_or_reserve"), ("cursor_future", "cursor_clock_invalid"),
    ("future_nomination", "nomination_clock_or_hash"), ("source_change", "collector_source_changed"),
    ("config_pages", "existing_job_protocol"), ("active_transaction", "job_transaction_or_deadline"),
])
def test_refusals_prevent_dispatch_and_preserve_no_debit(setup, monkeypatch, change, reason):
    s = setup
    def no_dispatch(*args, **kw):
        pytest.fail("A refused selection dispatched a provider")
    monkeypatch.setattr(backfill, "backfill_wallets", no_dispatch)
    if change == "foreign_lock":
        _write(s.lock, {"pid": os.getpid() + 1, "host": socket.gethostname(), "heartbeat_ms": NOW})
    elif change == "future_lock":
        _write(s.lock, {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat_ms": NOW + 1})
    elif change == "disabled_db":
        s.conn.execute("UPDATE ops_jobs SET enabled=0")
    elif change == "count_full":
        scheduler.quota_add(s.conn, "wallet_tracked", scheduler.utc_day(NOW), units=20, credits=2200)
    elif change == "floor":
        wa.helius.record_spend("fixture", 699_800, conn=s.conn, ts_ms=NOW)
    elif change == "cursor_future":
        s.conn.execute("INSERT INTO kv VALUES(?,?,?)", (backfill.cursor_key(Chain.SOL, WALLET), '{"pages":0}', NOW + 1))
    elif change == "future_nomination":
        review = _review()
        review["acquisition_queue"][0]["nominations"][0]["observed_ms"] = NOW + 1
        _write(s.review, review)
    elif change == "source_change":
        monkeypatch.setattr(wa, "COLLECTOR_SOURCES", {**wa.COLLECTOR_SOURCES, "kaiba/providers/_http.py": "0" * 64})
    elif change == "config_pages":
        s.ctx.params["pages"] = 2
    elif change == "active_transaction":
        s.conn.execute("BEGIN")
    with pytest.raises(wa.AcquisitionRefused, match=reason):
        _run(s)
    assert _state(s.conn) is None
    assert scheduler.quota_used(s.conn, s.ctx.name, scheduler.utc_day(NOW)) == ((20, 2200) if change == "count_full" else (0, 0))


@pytest.mark.parametrize("mutation", ["cohort_null", "blacklist", "owner", "hub", "future_review", "stale_review"])
def test_current_registry_revalidation_and_fresh_selection_scope(setup, monkeypatch, mutation):
    s = setup
    calls = []
    monkeypatch.setattr(backfill, "backfill_wallets", lambda *a, **kw: calls.append(kw))
    if mutation == "cohort_null":
        s.conn.execute("UPDATE wallets SET cohort=NULL")
    elif mutation == "blacklist":
        s.conn.execute("UPDATE wallets SET cohort='blacklist'")
    elif mutation == "owner":
        s.conn.execute('UPDATE wallets SET meta_json=\'{"is_owner":true}\'')
    elif mutation == "hub":
        s.conn.execute("INSERT INTO hub_addresses(chain,address,kind,source) VALUES('sol',?,'router','fixture')", (WALLET,))
    else:
        review = _review()
        review["observed_ms"] = NOW + 1 if mutation == "future_review" else NOW - 3_600_001
        _write(s.review, review)
    before = dict(s.conn.execute("SELECT * FROM wallets").fetchone())
    if mutation == "future_review":
        with pytest.raises(wa.AcquisitionRefused, match="review_clock_invalid"):
            _run(s)
    else:
        assert _run(s) is None
    assert not calls and _state(s.conn) is None
    assert dict(s.conn.execute("SELECT * FROM wallets").fetchone()) == before


def test_transient_wal_or_registry_deferral_never_debits_or_dispatches(setup, monkeypatch):
    s = setup
    calls = []
    monkeypatch.setattr(backfill, "backfill_wallets", lambda *a, **kw: calls.append(kw))
    original = wa._Reader.rows
    def refused(self, sql, args=()):
        if "FROM wallet_feed_tags" in sql:
            raise SourceDeferred("wal_over_limit; retry after at least 60s")
        return original(self, sql, args)
    monkeypatch.setattr(wa._Reader, "rows", refused)
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        _run(s)
    assert calls == [] and _state(s.conn) is None
    assert s.conn.execute("SELECT COUNT(*) FROM ops_quota").fetchone()[0] == 0


def test_source_or_registry_change_after_read_cannot_enter_admission(setup, monkeypatch):
    s = setup
    original = wa._pick
    rounds = []
    def changing(reader, queue, now):
        picked = original(reader, queue, now)
        rounds.append(picked)
        if len(rounds) == 1:
            s.conn.execute("UPDATE wallets SET source='changed_after_selection'")
        return picked
    monkeypatch.setattr(wa, "_pick", changing)
    with pytest.raises(wa.AcquisitionRefused, match="registry_changed_at_admission"):
        _run(s)
    assert len(rounds) == 2 and _state(s.conn) is None
    assert s.conn.execute("SELECT COUNT(*) FROM ops_quota").fetchone()[0] == 0


def test_private_owner_hook_returns_before_legacy_accounting_and_keeps_fallback(setup, monkeypatch):
    """Execute the four owner lines in a private function; never edit scheduler."""
    source = Path(scheduler.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "job_wallet_tracked")
    index = next(i for i, n in enumerate(fn.body) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "report" for t in n.targets))
    hook = ast.parse("from kaiba.learning.wallet_acquisition import run_selected_under_job\n"
                     "selected = run_selected_under_job(ctx, allow=allow, room=room, cohorts=cohorts)\n"
                     "if selected is not None:\n    return selected\n").body
    fn.body[index:index] = hook
    ordinary = []
    def dispatch(*args, **kw):
        ordinary.append(kw)
        return _fake_report()
    imports = builtins.__import__
    def import_proxy(name, *args, **kwargs):
        if name == "kaiba.ingest.backfill":
            return SimpleNamespace(backfill_wallets=dispatch)
        return imports(name, *args, **kwargs)
    namespace = dict(vars(scheduler)) | {"__builtins__": dict(vars(builtins), __import__=import_proxy),
        "_grade_many": lambda *a: {"graded": 17}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "owner-hook-private", "exec"), namespace)
    # Positive selected path: a non-None result reaches neither legacy spend nor grade.
    calls = _backend(monkeypatch, setup)
    result = namespace["job_wallet_tracked"](setup.ctx)
    assert result["graded"] == 0 and len(calls) == 1 and ordinary == []
    assert setup.ctx.quota_used() == (1, 250)
    # No fresh selection: exactly the existing ordinary limit/grade/post-accounting path.
    setup.review.unlink()
    monkeypatch.setattr(wa, "run_selected_under_job", lambda *args, **kw: None)
    result = namespace["job_wallet_tracked"](setup.ctx)
    assert ordinary[0]["limit"] == 2 and result["graded"] == 17
    assert setup.ctx.quota_used() == (2, 360)


def test_returned_attempt_survives_queue_absence_and_reintroduction(setup, monkeypatch):
    s = setup
    _wallet(s.conn, OTHER)
    calls = []
    def dispatch(conn, chain, **kw):
        calls.append(kw["wallets"][0])
        return _fake_report(kw["wallets"][0])
    monkeypatch.setattr(backfill, "backfill_wallets", dispatch)
    assert _run(s)["acquisition_status"] == "returned"
    # Export/review rotation omits the completed first selection while another is admitted.
    review = _review([_nominee(OTHER)])
    review["observed_ms"] = NOW - 250
    _write(s.review, review)
    assert _run(s)["acquisition_status"] == "returned"
    assert len(_state(s.conn)["attempts"]) == 2
    # Reintroducing the SAME frozen original cannot create a fresh attempt/debit.
    review = _review()
    review["observed_ms"] = NOW
    _write(s.review, review)
    assert _run(s)["reason"] == "selection_already_admitted_no_automatic_retry"
    assert calls == [WALLET, OTHER] and s.ctx.quota_used() == (2, 500)


def test_prior_interrupted_selection_cannot_starve_other_unattempted_nominee(setup, monkeypatch):
    s = setup
    _wallet(s.conn, OTHER)
    _backend(monkeypatch, s, fail=True)
    with pytest.raises(KeyboardInterrupt):
        _run(s)
    review = _review([_nominee(), _nominee(OTHER)])
    review["observed_ms"] = NOW
    _write(s.review, review)
    calls = []
    def dispatch(conn, chain, **kw):
        calls.append(kw["wallets"])
        return _fake_report(OTHER)
    monkeypatch.setattr(backfill, "backfill_wallets", dispatch)
    assert _run(s)["acquisition_status"] == "returned" and calls == [[OTHER]]
    assert s.ctx.quota_used() == (2, 500)


def test_attempt_capacity_and_corrupt_state_are_preserved_and_refuse(setup, monkeypatch):
    s = setup
    def no_dispatch(*args, **kw):
        pytest.fail("Attempt capacity/corruption dispatched a provider")
    monkeypatch.setattr(backfill, "backfill_wallets", no_dispatch)
    # All IDs stay inside13days, independent of current queue membership/status.
    attempts = {}
    for index in range(wa.MAX_ATTEMPTS):
        row = {**_nominee(), "selected_ms": NOW - 2000 - index}
        attempts[wa._attempt_id(row)] = {"selection": row, "admitted_ms": NOW - 500,
            "charged_credits": 250, "status": "returned"}
    state = {"version": wa.VERSION, "attempts": attempts, "review_ms": 0, "review_hash": None}
    raw = json.dumps(state)
    s.conn.execute("INSERT INTO kv VALUES(?,?,?)", (wa.ATTEMPT_KEY, raw, NOW))
    with pytest.raises(wa.AcquisitionRefused, match="attempt_state_capacity"):
        _run(s)
    assert s.conn.execute("SELECT value FROM kv WHERE key=?", (wa.ATTEMPT_KEY,)).fetchone()[0] == raw
    assert s.ctx.quota_used() == (0, 0)
    s.conn.execute("UPDATE kv SET value=? WHERE key=?", ('{"version":"broken"}', wa.ATTEMPT_KEY))
    with pytest.raises(wa.AcquisitionRefused, match="attempt_state_invalid"):
        _run(s)
    assert s.conn.execute("SELECT value FROM kv WHERE key=?", (wa.ATTEMPT_KEY,)).fetchone()[0] == '{"version":"broken"}'


def test_query_guard_interrupts_long_sql_and_authorizer_blocks_wallet_mutation(setup, monkeypatch):
    s = setup
    elapsed = [0.0]
    def monotonic():
        elapsed[0] += .02
        return elapsed[0]
    monkeypatch.setattr(wa.time, "monotonic", monotonic)
    with wa._connection(s.db, deadline=100) as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("UPDATE wallets SET cohort='trusted_copy'")
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT SUM(x) FROM n").fetchone()
        assert conn.fault == "source_statement_deadline"
    assert s.conn.execute("SELECT cohort FROM wallets").fetchone()[0] == "research"


def test_symlink_parent_cannot_supply_review(setup, tmp_path):
    s = setup
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(s.review.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This host cannot create directory symlinks")
    with pytest.raises(wa.AcquisitionRefused, match="input_symlink"):
        wa.run_selected_under_job(s.ctx, allow=2, room=SimpleNamespace(credits=250),
            cohorts=["research"], review_path=alias / s.review.name)
    assert s.ctx.quota_used() == (0, 0)


def test_parent_link_guard_and_transaction_deadline_between_small_queries(setup, monkeypatch):
    s = setup
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == s.review.parent or original(path))
    with pytest.raises(wa.AcquisitionRefused, match="input_symlink"):
        _run(s)
    monkeypatch.setattr(Path, "is_symlink", original)
    elapsed = [0.0]
    monkeypatch.setattr(wa.time, "monotonic", lambda: elapsed[0])
    with wa._connection(s.db, deadline=100) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO kv VALUES('offline-rollback-proof','1',1)")
        elapsed[0] = 3.1
        # Each statement is tiny; elapsed TRANSACTION time must still abort it.
        with pytest.raises(SourceDeferred, match="source_transaction_deadline"):
            conn.execute("COMMIT")
    assert s.conn.execute("SELECT value FROM kv WHERE key='offline-rollback-proof'").fetchone() is None
