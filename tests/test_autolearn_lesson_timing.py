"""Timing-qualified lesson preparation and retained outbox eligibility, offline."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import textwrap
from pathlib import Path

import pytest

from kaiba.learning import admission_paper as ap
from kaiba.learning import autolearn, cooperative_reader
from kaiba.learning import strategy_campaign as sc
from kaiba.learning.signal_audit import ExitLadder

T0 = 1_800_000_000_000
REGISTRY = "SELECT * FROM wallets WHERE chain=? AND address IN (?)"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def supplied_outcome(*, legacy=False):
    factory = sc.generate_campaign if legacy else ap.generate_admission_campaign
    manifest = factory([], ExitLadder(), now_ms=T0, groups=[("sm-trenches", "sol")], max_candidates=2)
    arm = next(a for a in manifest["hypotheses"] if a["spec"]["exit"].get("hold_ms") == 60_000)
    signal, opened, closed = T0 + 1000, T0 + 32_000, T0 + 104_000
    row = {
        "campaign_id": manifest["campaign_id"],
        "hypothesis_id": arm["hypothesis_id"],
        "spec_hash": arm["spec_hash"],
        "outcome_id": "paper-synthetic-outcome",
        "signal_id": "synthetic-signal",
        "token": "synthetic-token",
        "lane": "sm-trenches",
        "chain": "sol",
        "signal_ms": signal,
        "opened_ms": opened,
        "closed_ms": closed,
        "observed_ms": closed,
        "entry_processing_ms": opened,
        "provider": "paper",
        "source": "PaperBroker",
        "mode": "shadow",
        "status": "closed",
        "features": {},
        "evidence_complete": True,
        "cost_complete": True,
        "inventory_complete": True,
        "entry_debit_native": 1000,
        "exit_credit_native": 1200,
        "pnl_native": 200,
        "cost_model_assumed": True,
    }
    if not legacy:
        policy = manifest["assessment_policy"][ap.POLICY_KEY]
        buy = {
            ap.POLICY_KEY: policy,
            "side": "buy",
            "quote_ms": opened,
            "price_ts_ms": opened,
            "depth_ts_ms": opened,
            "native_price_ts_ms": opened,
            "admission_ms": opened,
            "processing_ms": opened,
            "ledger_ms": opened,
            "order_id": "synthetic-buy",
            "mode": "shadow",
            "provider": "paper",
            "state": "filled",
        }
        sell = {
            **buy,
            "side": "sell",
            "quote_ms": closed,
            "price_ts_ms": closed,
            "depth_ts_ms": closed,
            "native_price_ts_ms": closed,
            "admission_ms": closed,
            "processing_ms": closed,
            "ledger_ms": closed,
            "order_id": "synthetic-sell",
            "exit_timing": {
                "basis": "fixed_admission_deadline",
                "reason": "time_exit",
                "trigger_ms": opened + 60_000,
                "recognized_ms": closed,
                "trigger_price_ms": closed,
                "due_ms": closed,
            },
        }
        row.update(
            timing_complete=True, timing_error=None, execution_policy=policy, fill_evidence=[buy, sell]
        )
    return manifest, row


def lessons(manifest, row):
    return autolearn.AutoLearner._lessons(None, manifest, {"outcomes": [row]})


@pytest.mark.parametrize("legacy", [False, True])
def test_valid_ladder_and_admission_keep_original_body_and_identity(legacy):
    manifest, row = supplied_outcome(legacy=legacy)
    (lesson,) = lessons(manifest, row)
    assert lesson["evidence_id"] == row["outcome_id"] and lesson["outcome"] == "hit"
    assert lesson["body"] == json.dumps(
        {
            "type": "prospective_paper_outcome",
            "outcome_id": row["outcome_id"],
            "campaign_id": row["campaign_id"],
            "hypothesis_id": row["hypothesis_id"],
            "spec_hash": row["spec_hash"],
            "token": "synthetic-token",
            "chain": "sol",
            "signal_ms": T0 + 1000,
            "opened_ms": T0 + 32_000,
            "closed_ms": T0 + 104_000,
            "entry_debit_native": 1000,
            "exit_credit_native": 1200,
            "pnl_native": 200,
            "entry_latency_ms": None,
            "exit_latencies_ms": None,
            "cost_model_assumed": True,
            "funding_allowed": False,
        }
    )
    row.update(pnl_native=-200, exit_credit_native=800)
    assert lessons(manifest, row)[0]["outcome"] == "miss"


@pytest.mark.parametrize("qualification", [False, None, 1, "true", "missing"])
def test_admission_requires_actual_strict_timing_not_row_marker_downgrade(qualification):
    manifest, row = supplied_outcome()
    if qualification == "missing":
        for key in ("timing_complete", "timing_error", "execution_policy"):
            row.pop(key)
    else:
        row["timing_complete"] = qualification
    assert lessons(manifest, row) == []


def test_positive_boolean_cannot_mask_actual_early_entry_clock():
    manifest, row = supplied_outcome()
    row["fill_evidence"][0]["price_ts_ms"] = row["signal_ms"] + 1000
    arm = next(a for a in manifest["hypotheses"] if a["hypothesis_id"] == row["hypothesis_id"])
    assert ap.timing_error(row, arm["spec"], row["execution_policy"]) == "invalid entry timing"
    assert lessons(manifest, row) == []


def learner(tmp_path, tmp_db):
    db_path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
    root = tmp_path / "research"
    return autolearn.AutoLearner(
        db_path,
        root / "worker",
        learning_root=root,
        groups=[("sm-trenches", "sol")],
        max_candidates=2,
        clock=lambda: (T0 + 104_000) / 1000,
    )


def count_hits(conn, ref):
    return conn.execute("SELECT COUNT(*) FROM playbook_hits WHERE ref=?", (ref,)).fetchone()[0]


def test_retained_unapproved_credit_is_held_before_limit_without_starving_operating(tmp_path, tmp_db):
    worker = learner(tmp_path, tmp_db)
    manifest, row = supplied_outcome()
    (valid,) = lessons(manifest, row)
    held = {**valid, "evidence_id": "old-unqualified"}
    operating = {
        "evidence_id": "operating",
        "lane": None,
        "text": "A research observation.",
        "body": '{"type":"operating","funding_allowed":false}',
    }
    state = {"pending_lessons": [copy.deepcopy(held), copy.deepcopy(valid), operating]}
    approved = {manifest["campaign_id"]: {valid["evidence_id"]: valid}}
    report = {}
    assert (
        worker._flush_lessons(
            state, report, now_ms=T0 + 104_000, limit=1, qualified_lessons_by_campaign=approved
        )
        == 1
    )
    assert state["pending_lessons"] == [held, operating]
    assert count_hits(tmp_db, held["evidence_id"]) == 0 and count_hits(tmp_db, valid["evidence_id"]) == 1
    assert (
        worker._flush_lessons(
            state, report, now_ms=T0 + 104_000, limit=39, qualified_lessons_by_campaign=approved
        )
        == 1
    )
    assert state["pending_lessons"] == [held] and report["lesson_credit_validation"]["held_pending"] == 1
    assert report["lesson_credit_validation"]["historical_credit_status"] == "unmeasured"


@pytest.mark.parametrize("credit", [True, False])
def test_missing_approval_holds_paper_body_or_any_hit_miss(tmp_path, tmp_db, credit):
    worker = learner(tmp_path, tmp_db)
    _, row = supplied_outcome()
    pending = {
        "evidence_id": "held",
        "lane": "sm-trenches",
        "text": "Existing lesson.",
        "body": '{"type":"operating"}' if credit else '{"type":"prospective_paper_outcome"}',
    }
    if credit:
        pending["outcome"] = "hit"
    state = {"pending_lessons": [copy.deepcopy(pending)]}
    assert worker._flush_lessons(state, {}, now_ms=row["closed_ms"], limit=40) == 0
    assert state["pending_lessons"] == [pending] and count_hits(tmp_db, "held") == 0


def test_duplicate_conflicts_refuse_before_writer_and_exact_duplicates_credit_once(
    tmp_path, tmp_db, monkeypatch
):
    worker = learner(tmp_path, tmp_db)
    manifest, row = supplied_outcome()
    (good,) = lessons(manifest, row)
    approved = {manifest["campaign_id"]: {good["evidence_id"]: good}}
    state = {"pending_lessons": [good, {**good, "body": "different"}]}
    original = autolearn.persist_lessons
    monkeypatch.setattr(autolearn, "persist_lessons", lambda *a: pytest.fail("writer reached conflict"))
    with pytest.raises(ValueError, match="pending_lesson_identity_conflict"):
        worker._flush_lessons(
            state, {}, now_ms=row["closed_ms"], limit=40, qualified_lessons_by_campaign=approved
        )
    monkeypatch.setattr(autolearn, "persist_lessons", original)
    state["pending_lessons"] = [good, copy.deepcopy(good)]
    assert (
        worker._flush_lessons(
            state, {}, now_ms=row["closed_ms"], limit=40, qualified_lessons_by_campaign=approved
        )
        == 1
    )
    assert count_hits(tmp_db, good["evidence_id"]) == 1 and state["pending_lessons"] == []


def test_same_ID_changed_body_or_conflicting_campaign_approval_cannot_authorize(tmp_path, tmp_db):
    worker = learner(tmp_path, tmp_db)
    manifest, row = supplied_outcome()
    (good,) = lessons(manifest, row)
    changed = {**good, "body": "changed"}
    state = {"pending_lessons": [copy.deepcopy(good)]}
    assert (
        worker._flush_lessons(
            state,
            {},
            now_ms=row["closed_ms"],
            limit=40,
            qualified_lessons_by_campaign={manifest["campaign_id"]: {good["evidence_id"]: changed}},
        )
        == 0
    )
    assert state["pending_lessons"] == [good] and count_hits(tmp_db, good["evidence_id"]) == 0
    with pytest.raises(ValueError, match="qualified_lesson_identity_conflict"):
        worker._flush_lessons(
            state,
            {},
            now_ms=row["closed_ms"],
            limit=40,
            qualified_lessons_by_campaign={
                "one": {good["evidence_id"]: good},
                "two": {good["evidence_id"]: changed},
            },
        )


def test_historical_credit_before_missing_journal_ACK_is_preserved_not_reversed(tmp_path, tmp_db):
    worker = learner(tmp_path, tmp_db)
    manifest, row = supplied_outcome()
    (held,) = lessons(manifest, row)
    from kaiba.learning import playbook

    rid = playbook.add_rule(held["text"], lane=held["lane"], status="proposed", conn=tmp_db)
    playbook.record_hit(rid, ref=held["evidence_id"], conn=tmp_db)
    key = "autolearn:evidence:" + autolearn.hashlib.sha256(held["evidence_id"].encode()).hexdigest()
    tmp_db.execute(
        "INSERT INTO kv(key,value,updated_ms) VALUES(?,?,?)",
        (key, json.dumps({"rule_id": rid, "journal_seq": None}), T0),
    )
    state = {"pending_lessons": [copy.deepcopy(held)]}
    assert worker._flush_lessons(state, {}, now_ms=row["closed_ms"], limit=40) == 0
    assert count_hits(tmp_db, held["evidence_id"]) == 1 and state["pending_lessons"] == [held]
    assert (
        json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()[0])["journal_seq"]
        is None
    )


@pytest.mark.parametrize("pointer", ["lesson_timing_upgrade", "configured_receipts_upgrade"])
def test_orphan_upgrade_pointer_never_rewrites_parent(tmp_path, tmp_db, pointer):
    worker = learner(tmp_path, tmp_db)
    raw = json.dumps(
        {"version": autolearn.VERSION, "pending_lessons": [], pointer: {"invalid": True}}
    ).encode()
    worker.state_path.write_bytes(raw)
    result = worker.once()
    assert result["status"] == "partial" and worker.state_path.read_bytes() == raw


def test_removed_flush_qualification_guard_exposes_retained_bad_credit(tmp_path, tmp_db):
    worker = learner(tmp_path, tmp_db)
    manifest, row = supplied_outcome()
    (held,) = lessons(manifest, row)

    def must_hold(method):
        state = {"pending_lessons": [copy.deepcopy(held)]}
        assert method(worker, state, {}, now_ms=row["closed_ms"], limit=40) == 0
        assert state["pending_lessons"] == [held] and count_hits(tmp_db, held["evidence_id"]) == 0

    must_hold(autolearn.AutoLearner._flush_lessons)
    source = textwrap.dedent(inspect.getsource(autolearn.AutoLearner._flush_lessons))
    tree = ast.parse(source)
    predicate = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and "approved.get(eid) != lesson" in ast.unparse(n.test)
    )
    predicate.test = ast.Constant(False)
    ast.fix_missing_locations(tree)
    namespace = dict(vars(autolearn))
    exec(compile(tree, "removed-pending-qualification-guard", "exec"), namespace)
    with pytest.raises(AssertionError):
        must_hold(namespace["_flush_lessons"])
    assert count_hits(tmp_db, held["evidence_id"]) == 1


def test_same_once_replaces_campaign_approvals_after_review_callback(tmp_path, tmp_db, monkeypatch):
    """Actual _once/flush/canonical writer; Context/runner/assessment rows controlled here.

    The separate root integration suite owns genuine 22-book authority coverage.
    """
    worker = learner(tmp_path, tmp_db)
    manifest, valid = supplied_outcome()
    (old_pending,) = lessons(manifest, valid)
    clock = {"ms": valid["closed_ms"]}
    worker.clock = lambda: clock["ms"] / 1000
    state = {
        "version": autolearn.VERSION,
        "assessment": {},
        "pending_lessons": [old_pending],
        "selected_ms": T0,
        "source_initialized": True,
        "decision_cursor": 0,
        "journal_retry_after_ms": clock["ms"] + 1,
    }
    worker.state_path.write_text(json.dumps(state))
    calls = []

    class ControlledPaper:
        def expire_pending(self, *a, **kw):
            return {"expired": 0, "blocked_by_exposure": 0}

        def watched_tokens(self, *a, **kw):
            return []

        def run_candidates(self, *a, **kw):
            row = copy.deepcopy(valid)
            if calls:
                row["timing_complete"] = False
                row["timing_error"] = "invalid entry timing"
            calls.append(True)
            return {
                "outcomes": [row],
                "assessment_rows": [row],
                "arms": [],
                "errors": [],
                "processing_frontier_ms": clock["ms"],
            }

    class ControlledContext:
        service_upgrade = {"controlled": True}
        tax_quote_upgrade = None
        authority = {"generation": 1}

        def manifests(self):
            return [manifest]

        def runner(self):
            return ControlledPaper()

        def assess(self, *a, **kw):
            return {
                "next_state": {},
                "assessments": {manifest["campaign_id"]: {"next_state": {}, "arms": []}},
                "arms": [],
            }

        def _revalidate_bindings(self, **kw):
            return "controlled"

    monkeypatch.setattr(
        "kaiba.learning.allocation_runtime.load_context", lambda *a, **kw: ControlledContext()
    )
    original_service = cooperative_reader.PaperService

    class Clocked(original_service):
        def __init__(self, callback, **kwargs):
            kwargs.update(monotonic_clock=lambda: clock["ms"] / 1000, wall_clock=worker.clock)
            super().__init__(callback, **kwargs)

    monkeypatch.setattr(cooperative_reader, "PaperService", Clocked)
    approvals = []
    original_flush = worker._flush_lessons

    def flush(*a, **kw):
        approvals.append(copy.deepcopy(kw.get("qualified_lessons_by_campaign")))
        return original_flush(*a, **kw)

    monkeypatch.setattr(worker, "_flush_lessons", flush)

    def review(reader, state, report, now):
        clock["ms"] += 16_000
        reader.rows(REGISTRY, ("sol", "synthetic-wallet"))

    monkeypatch.setattr(worker, "_scheduled_reviews", review)
    result = worker.once()
    assert len(calls) >= 2 and len(approvals) == 2
    assert approvals[0][manifest["campaign_id"]] == {old_pending["evidence_id"]: old_pending}
    assert approvals[1][manifest["campaign_id"]] == {}
    saved = json.loads(worker.state_path.read_bytes())
    assert old_pending in saved["pending_lessons"] and count_hits(tmp_db, old_pending["evidence_id"]) == 0
    assert result["lesson_credit_validation"]["held_pending"] == 1 and result["status"] == "partial"
    assert result["journal"]["added"]  # the independent zero-credit start lesson drained
