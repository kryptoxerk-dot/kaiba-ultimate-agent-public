"""Repeated economic outbox polling and exact archived state migration."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from kaiba.learning import autolearn
from kaiba.learning import journal_outbox as outbox

NOW = 1_790_000_000_000


def revision(number=1):
    return {"position_id": f"fixture-position-{number}", "previous_source_hash": "a" * 64,
            "source_hash": "b" * 64, "status": "captured", "at_ms": NOW,
            "chain": "sol", "mode": "live", "lane": "fixture-lane", "raw_cost_rows": 2,
            "raw_cost_rows_hash": "c" * 64}


def legacy(core, reasons, *, sorted_body=False):
    return {"evidence_id": outbox.PRIMARY_PREFIX + outbox._hash(core), "lane": core["lane"],
            "text": "Revisited real-money evidence changed; unresolved custody and cashflows remain unproven.",
            "body": json.dumps({"type": "economic_evidence_revision", **core, "unknown_reasons": reasons,
                                "funding_allowed": False}, sort_keys=sorted_body)}


def packed(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def migration_fixture():
    first, second = revision(), revision(2)
    other = {"evidence_id": "pending-paper", "outcome": "hit", "body": "paper exact bytes", "marker": [1, 2]}
    pending = [legacy(first, ["missing_fee"]), copy.deepcopy(other),
               legacy(first, ["missing_fee"], sorted_body=True),
               legacy(first, ["missing_fee", "executable_inventory_value_missing"]),
               legacy(second, ["missing_receipt"])]
    state = {"pending_lessons": pending, "provider_admissions": {"consumed": 9, "pending": [7]},
             "last_wallet_review_ms": NOW - 1000, "wallet_review_state": {"fixed_selections": ["fixture"]}}
    economic = {"version": "economic-work/1", "revision_outbox": {outbox._hash(v): v for v in (first, second)},
                "closed_cursor": [NOW, 5], "records": {"unmodified": {"unproven": True}}}
    return state, economic, other


def test_repeated_outbox_one_hundred_cycles_stays_bounded_and_exact():
    state = {"pending_lessons": [], "pending_admissions": {"consumed": 19, "unresolved": [1]}}
    original_admissions = copy.deepcopy(state["pending_admissions"])
    initial_size = None
    for cycle in range(100):
        core = json.loads(json.dumps(revision(), sort_keys=bool(cycle % 2)))
        result = outbox.enqueue_economic_revision(state, core, unknown_reasons=["missing_fee"])
        assert result["added"] == (2 if cycle == 0 else 0)
        raw = outbox.encode_state(json.dumps(state, indent=2))
        initial_size = len(raw) if initial_size is None else initial_size
        assert len(raw) == initial_size
        state = json.loads(raw)
    assert len(state["pending_lessons"]) == 2 and state["pending_admissions"] == original_admissions
    assert all("outcome" not in row for row in state["pending_lessons"])
    assert all(json.loads(row["body"])["funding_allowed"] is False for row in state["pending_lessons"])


def test_changed_diagnostic_is_retained_once_without_mutating_primary():
    state = {"pending_lessons": []}
    outbox.enqueue_economic_revision(state, revision(), unknown_reasons=["missing_fee"])
    primary = copy.deepcopy(state["pending_lessons"][0])
    for _ in range(100):
        outbox.enqueue_economic_revision(state, revision(),
            unknown_reasons=["missing_fee", "executable_inventory_value_missing"])
    assert len(state["pending_lessons"]) == 3 and state["pending_lessons"][0] == primary
    body = json.loads(primary["body"])
    assert body["unknown_unproven"] is True
    assert body["diagnostic_variants"]["revision_evidence_id"] == primary["evidence_id"]
    assert any("executable_inventory_value_missing" in json.loads(row["body"]).get("unknown_reasons", [])
               for row in state["pending_lessons"])


def test_conflicting_core_or_global_pending_identity_still_refuses():
    state = {"pending_lessons": []}
    outbox.enqueue_economic_revision(state, revision(), unknown_reasons=[])
    body = json.loads(state["pending_lessons"][0]["body"])
    body["source_hash"] = "d" * 64
    state["pending_lessons"][0]["body"] = json.dumps(body)
    before = copy.deepcopy(state)
    with pytest.raises(ValueError, match="economic_lesson_identity_conflict"):
        outbox.enqueue_economic_revision(state, revision(), unknown_reasons=[])
    assert state == before
    state = {"pending_lessons": [{"evidence_id": "other", "body": "first"},
                                  {"evidence_id": "other", "body": "different"}]}
    with pytest.raises(ValueError, match="pending_lesson_identity_conflict"):
        outbox.enqueue_economic_revision(state, revision(), unknown_reasons=[])


def test_real_writer_only_compacts_state_and_preserves_redaction_semantics(tmp_path, monkeypatch):
    from kaiba.core import redact
    original = {"secret": "fixture-secret", "pending": [1, 2, 3]}
    monkeypatch.setattr(redact, "redact_text", lambda text: text.replace("fixture-secret", "[fixture-redacted]"))
    old = json.dumps(original, indent=2, sort_keys=True, default=str, allow_nan=False).replace(
        "fixture-secret", "[fixture-redacted]") + "\n"
    state_path, report_path = tmp_path / "state.json", tmp_path / "latest-report.json"
    autolearn.atomic_json(state_path, original)
    autolearn.atomic_json(report_path, original)
    assert json.loads(state_path.read_bytes()) == json.loads(old)
    assert state_path.read_bytes() == outbox.encode_state(old)
    assert report_path.read_text() == old


def test_oversized_output_refuses_before_replacing_last_valid_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"last":"valid"}\n')
    monkeypatch.setattr(outbox, "MAX_STATE_OUTPUT", 40)
    with pytest.raises(ValueError, match="state_still_at_or_above_reader_cap"):
        autolearn.atomic_json(path, {"large": "x" * 100})
    assert path.read_bytes() == b'{"last":"valid"}\n'
    assert not path.with_suffix(".json.partial").exists()


@pytest.mark.parametrize("bad", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'])
def test_serializer_rejects_duplicate_or_nonfinite_values(bad):
    with pytest.raises(ValueError):
        outbox.encode_state(bad)


def test_migration_preserves_every_raw_observation_variant_and_other_state():
    state, economic, other = migration_fixture()
    state_raw, economic_raw = packed(state), packed(economic)
    recipe = outbox.prepare_migration(state_raw, economic_raw, prepared_ms=NOW + 1000)
    restored = json.loads(recipe["state_raw"])
    archive, receipt = json.loads(recipe["archive_raw"]), recipe["receipt"]
    assert recipe["original_state_raw"] == state_raw and recipe["original_economic_work_raw"] == economic_raw
    assert receipt["source_state_sha256"] == hashlib.sha256(state_raw).hexdigest()
    assert receipt["source_economic_work_sha256"] == hashlib.sha256(economic_raw).hexdigest()
    assert receipt["archive_sha256"] == hashlib.sha256(recipe["archive_raw"]).hexdigest()
    for row in archive["observations"]:
        assert row["lesson"] == state["pending_lessons"][row["pending_index"]]
        assert row["raw_body_sha256"] == hashlib.sha256(row["lesson"]["body"].encode()).hexdigest()
    assert receipt["original_observations"] == 4
    assert receipt["independent_revision_count"] == 2 and receipt["distinct_diagnostic_variants"] == 3
    assert receipt["queued_economic_records"] == 5 and receipt["unrelated_pending_records"] == 1
    assert other in restored["pending_lessons"] and len(restored["pending_lessons"]) == 6
    for key in set(state) - {"pending_lessons"}:
        assert restored[key] == state[key]
    assert receipt["profitability_credits"] == receipt["journal_acknowledgments"] == 0
    assert receipt["economic_outbox_unchanged"] is True
    indexed = [i for group in archive["revision_provenance"] for variant in group["variants"]
               for i in variant["pending_indexes"]]
    assert sorted(indexed) == [0, 2, 3, 4]
    for record in restored["pending_lessons"]:
        if record != other:
            assert "outcome" not in record
            assert json.loads(record["body"])["at_ms"] == NOW
    for core in economic["revision_outbox"].values():
        # Existing diagnostic variants remain a no-op after migration.
        reasons = ["missing_fee"] if core["position_id"].endswith("1") else ["missing_receipt"]
        assert outbox.enqueue_economic_revision(restored, core, unknown_reasons=reasons)["added"] == 0


@pytest.mark.parametrize("mutation", [
    lambda state, economic: state["pending_lessons"][0].update(outcome="hit"),
    lambda state, economic: economic["revision_outbox"].pop(next(iter(economic["revision_outbox"]))),
    lambda state, economic: economic["revision_outbox"][next(iter(economic["revision_outbox"]))].update(raw_cost_rows=99),
    lambda state, economic: state["pending_lessons"][0].update(evidence_id=outbox.PRIMARY_PREFIX + "0" * 64),
    lambda state, economic: state.update(journal_outbox_migration={"already": True}),
])
def test_migration_refuses_unbound_revision_credits_or_repeat(mutation):
    state, economic, _ = migration_fixture()
    mutation(state, economic)
    with pytest.raises(ValueError):
        outbox.prepare_migration(packed(state), packed(economic), prepared_ms=NOW + 1000)


def test_migration_keeps_unrelated_exact_duplicate_decisions_and_original_inputs():
    state, economic, other = migration_fixture()
    state["pending_lessons"].append(copy.deepcopy(other))
    original = copy.deepcopy(state)
    recipe = outbox.prepare_migration(packed(state), packed(economic), prepared_ms=NOW + 1000)
    result = json.loads(recipe["state_raw"])
    assert result["pending_lessons"].count(other) == 2
    assert state == original


def test_actual_worker_repolls_100_times_then_journals_and_acknowledges_once(tmp_path, tmp_db, monkeypatch):
    """Actual _once enqueue/state writer and actual _flush/canonical journal/outbox ack.

    Source input, context and empty paper books are controlled; this test does
    not claim to validate the separate deployed allocation receipt graph.
    """
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))
    core = revision()
    now = {"ms": NOW}
    reasons = {"items": ["missing_fee"]}
    learning = tmp_path / "learning"

    class Reader:
        def __init__(self, db_path):
            self.db_path, self.queries, self.wal_waits, self.receipts = db_path, 0, 0, []

        def guard(self):
            return 0

    db_path = next(row[2] for row in tmp_db.execute("PRAGMA database_list") if row[1] == "main")
    worker = autolearn.AutoLearner(db_path, learning / "worker", learning_root=learning,
        groups=[("sm-trenches", "sol")], max_candidates=2, clock=lambda: now["ms"] / 1000,
        reader_factory=Reader)
    manifest = {"campaign_id": "fixture-campaign", "hypotheses": []}

    class Paper:
        def expire_pending(self, *a, **kw):
            return {"expired": 0, "blocked_by_exposure": 0}

        def watched_tokens(self, *a, **kw):
            return []

        def run_candidates(self, *a, **kw):
            return {"outcomes": [], "assessment_rows": [], "arms": [], "errors": []}

    class Context:
        service_upgrade = None
        tax_quote_upgrade = None
        authority = {"generation": 1}

        def manifests(self):
            return [manifest]

        def runner(self):
            return Paper()

        def assess(self, *a, **kw):
            return {"next_state": {}, "assessments": {"fixture-campaign": {"next_state": {}, "arms": []}},
                    "arms": []}

        def _revalidate_bindings(self, **kw):
            return "controlled"

    def economic(*a, **kw):
        return {"economic": {"outcomes": [{"position_id": core["position_id"],
                                           "unknown_reasons": list(reasons["items"])}]},
                "capture": {"revisions": [dict(core)], "selection_deferred": None, "status": "cached"}}

    monkeypatch.setattr("kaiba.learning.allocation_runtime.load_context", lambda *a, **kw: Context())
    monkeypatch.setattr("kaiba.learning.economic_work.capture_work", economic)
    monkeypatch.setattr("kaiba.learning.economic_work.cached_work", economic)
    monkeypatch.setattr(autolearn, "read_opportunities", lambda *a, **kw: {
        "opportunities": [], "skipped": [], "source_rows": 0, "next_rowid": 0})
    monkeypatch.setattr(worker, "_scheduled_reviews", lambda *a: None)
    paper_pending = {"evidence_id": "fixture-unqualified-paper", "outcome": "hit", "lane": "sm-trenches",
                     "text": "Must retain pending paper evidence", "body": '{"type":"prospective_paper_outcome"}'}
    state = {"version": autolearn.VERSION, "assessment": {}, "pending_lessons": [paper_pending],
             "selected_ms": NOW, "source_initialized": True, "decision_cursor": 0,
             "journal_retry_after_ms": NOW + 1, "start_journalled": True,
             "provider_admissions": {"consumed": 7, "pending": ["immutable"]}}
    worker.state_path.write_bytes(packed(state))
    sizes = {}
    for cycle in range(100):
        if cycle == 50:
            reasons["items"].append("executable_inventory_value_missing")
        report = worker.once()
        assert "input_error" not in report
        saved = json.loads(worker.state_path.read_bytes())
        assert len(saved["pending_lessons"]) == (3 if cycle < 50 else 4)
        assert paper_pending in saved["pending_lessons"]
        assert saved["provider_admissions"] == state["provider_admissions"]
        sizes.setdefault(cycle >= 50, worker.state_path.stat().st_size)
        assert worker.state_path.stat().st_size == sizes[cycle >= 50]
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits").fetchone()[0] == 0
    economic_path = worker.state_dir / "economic-work.json"
    economic_path.write_bytes(packed({"version": "economic-work/1", "records": {},
        "revision_outbox": {outbox._hash(core): core}, "closed_cursor": [NOW, 0], "open_cursor": 0,
        "source_observed_ms": NOW}))
    saved["journal_retry_after_ms"] = 0
    original_pending = copy.deepcopy(saved["pending_lessons"])
    report = {}
    assert worker._flush_lessons(saved, report, now_ms=NOW, limit=40) == 3
    assert report["economic_revisions_acknowledged"] == 1
    assert saved["pending_lessons"] == [paper_pending]
    assert json.loads(economic_path.read_bytes())["revision_outbox"] == {}
    lesson_count = "SELECT COUNT(*) FROM journal WHERE substr(subject,1,10)='autolearn:'"
    assert tmp_db.execute(lesson_count).fetchone()[0] == 3
    all_journal_rows = tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits").fetchone()[0] == 0
    # Crash after journal/outbox commit but before parent publication: dedupe is
    # proven by the real persistent receipts, not an in-memory helper cache.
    saved["pending_lessons"] = original_pending
    retry = {}
    assert worker._flush_lessons(saved, retry, now_ms=NOW, limit=40) == 3
    assert retry["economic_revisions_acknowledged"] == 0 and retry["journal"]["repeated"] == 3
    assert retry["journal"]["added"] == [] and saved["pending_lessons"] == [paper_pending]
    assert tmp_db.execute(lesson_count).fetchone()[0] == 3
    assert tmp_db.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == all_journal_rows
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook_hits").fetchone()[0] == 0
