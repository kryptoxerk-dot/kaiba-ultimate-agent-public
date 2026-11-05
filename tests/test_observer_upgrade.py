"""Actual frozen collector/books/admissions; only transport is a fixture provider."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from kaiba.learning import observer_upgrade as upgrade
from kaiba.learning import prospective_quotes as base
from kaiba.learning import quote_budget
from tests.test_prospective_quotes import T0, at, cached_market, read, write
from tests.test_prospective_quotes import setup as observer_setup  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external transport forbidden"))


def install(s, *, snapshot=None, state=None):
    s.collector = upgrade.BudgetedQuoteCollector(s.research, s.root / "prospective-quotes",
        learning_root=s.root, clock=lambda: s.clock["ms"] / 1000, provider=s.provider)
    if state is None:
        state = {"version": base.VERSION, "policy": base.POLICY, "days": {}, "tokens": {},
                 "attempts": [], "retry_after_ms": 0, "updated_ms": 0}
    write(s.collector.state_path, state)
    plan = upgrade.prepare_upgrade(state, snapshot, now_ms=s.clock["ms"])
    write(s.collector.manifest_path, plan["manifest"])
    write(s.collector.ledger_path, plan["ledger"])
    return s


@pytest.fixture
def upgraded(request):
    return install(request.getfixturevalue("observer_setup"))


def test_real_large_parent_old_reader_refuses_new_reader_captures_original_admission(request):
    s = request.getfixturevalue("observer_setup")
    parent = read(s.research / "state.json")
    parent["behavior_padding"] = "x" * 1_188_103
    write(s.research / "state.json", parent)
    assert (s.research / "state.json").stat().st_size > base.MAX_JSON_BYTES
    old = at(s)
    assert old["status"] == "partial" and old["provider_attempts"] == 0 and not s.provider.calls
    state = read(s.collector.state_path)
    install(s, state=state)
    result = at(s)
    assert result["status"] == "captured" and result["provider_attempts"] == 1
    assert len(s.provider.calls) == 1 and result["daily_admitted_attempts"] == 1
    assert result["attempt"]["cache"]["original_capture"]["status"] == "retained"
    assert read(s.research / "state.json") == parent
    assert base.MAX_JSON_BYTES == 1_000_000
    ledger = read(s.collector.ledger_path)
    assert ledger["days"][str(s.clock["ms"] // base.DAY_MS)]["observed_total"] == 1
    assert s.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_parent_over_existing_allocation_cap_does_not_admit(upgraded):
    s = upgraded
    parent = read(s.research / "state.json")
    parent["padding"] = "x" * upgrade.PARENT_CAP
    write(s.research / "state.json", parent)
    result = at(s)
    assert result["status"] == "partial" and result["provider_attempts"] == 0
    assert read(s.collector.state_path)["days"] == {} and not s.provider.calls


@pytest.mark.parametrize("artifact", ["manifest", "ledger"])
def test_missing_upgrade_evidence_never_bootstraps(upgraded, artifact):
    s = upgraded
    path = s.collector.manifest_path if artifact == "manifest" else s.collector.ledger_path
    path.unlink()
    result = at(s)
    assert result["status"] == "partial" and not path.exists()
    assert result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.state_path)["days"] == {}


@pytest.mark.parametrize("artifact", ["manifest", "ledger", "parent"])
def test_duplicate_or_nonfinite_json_is_not_silently_accepted(upgraded, artifact):
    s = upgraded
    path = {"manifest": s.collector.manifest_path, "ledger": s.collector.ledger_path,
            "parent": s.research / "state.json"}[artifact]
    path.write_text('{"version":NaN,"version":"other"}', encoding="utf-8")
    result = at(s)
    assert result["status"] == "partial" and result["provider_attempts"] == 0
    assert not s.provider.calls


def test_bad_source_declaration_refuses_before_original_boundary(upgraded):
    s = upgraded
    manifest = read(s.collector.manifest_path)
    manifest["source_bindings"]["learning/observer_upgrade.py"] = "0" * 64
    write(s.collector.manifest_path, manifest)
    result = at(s)
    assert result["status"] == "partial" and result["provider_attempts"] == 0
    assert read(s.collector.state_path)["days"] == {} and not s.provider.calls


def test_successful_final_slot_is_not_rejected_by_its_own_reservation(request):
    s = request.getfixturevalue("observer_setup")
    s.clock["ms"] = T0 + 31_000
    day = str(s.clock["ms"] // base.DAY_MS)
    state = {"version": base.VERSION, "policy": base.POLICY,
        "days": {day: quote_budget.paced_limit(s.clock["ms"]) - 1},
        "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": s.clock["ms"]}
    install(s, state=state)
    result = s.collector.once()
    assert result["status"] == "captured" and result["provider_attempts"] == 1
    assert read(s.collector.state_path)["days"][day] == quote_budget.paced_limit(s.clock["ms"])
    assert len(s.provider.calls) == 1


def test_pacing_and_original_daily_cap_do_not_reset_on_new_declaration(request):
    s = request.getfixturevalue("observer_setup")
    s.clock["ms"] = T0 + 31_000
    day = str(s.clock["ms"] // base.DAY_MS)
    state = {"version": base.VERSION, "policy": base.POLICY, "days": {day: 100},
        "tokens": {}, "attempts": [], "retry_after_ms": 0, "updated_ms": s.clock["ms"]}
    install(s, state=state)
    result = s.collector.once()
    assert result["provider_attempts"] == 0 and result["reason"] == "daily_research_attempt_cap"
    assert read(s.collector.state_path)["days"][day] == 100 and not s.provider.calls


def test_revalidation_keeps_exact_book_not_just_same_token(upgraded, monkeypatch):
    s = upgraded
    original, changed = base._save, []
    def save(path, root, value):
        original(path, root, value)
        if path == s.collector.state_path and value.get("attempts") and not changed:
            attempt = value["attempts"][-1]
            if attempt["status"] == "admitted":
                changed.append(attempt["target"])
                with sqlite3.connect(s.research / "paper" / attempt["target"]["book"]) as conn:
                    conn.execute("UPDATE candidate_entries SET status='expired'")
    monkeypatch.setattr(base, "_save", save)
    result = at(s)
    assert changed and result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.state_path)["days"][str(s.clock["ms"] // base.DAY_MS)] == 1
    assert read(s.collector.ledger_path)["days"][str(s.clock["ms"] // base.DAY_MS)]["observed_total"] == 1


def test_repeated_cycle_never_resends_old_crash_admission(upgraded, monkeypatch):
    s = upgraded
    original = s.collector._persist_ledger
    def interrupted(ledger):
        if any(day["observed_total"] for day in ledger["days"].values()):
            raise KeyboardInterrupt
        return original(ledger)
    monkeypatch.setattr(s.collector, "_persist_ledger", interrupted)
    with pytest.raises(KeyboardInterrupt):
        at(s)
    state = read(s.collector.state_path)
    day = str(s.clock["ms"] // base.DAY_MS)
    assert state["days"][day] == 1 and state["attempts"][-1]["status"] == "admitted"
    assert not s.provider.calls
    monkeypatch.setattr(s.collector, "_persist_ledger", original)
    result = s.collector.once()
    assert result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 1
    assert read(s.collector.state_path)["days"][day] == 1


def test_existing_fresh_cache_does_not_create_an_admission(upgraded):
    s = upgraded
    s.clock["ms"] = T0 + 31_000
    cached_market(s)
    result = s.collector.once()
    assert result["provider_attempts"] == 0 and not s.provider.calls
    assert read(s.collector.state_path)["days"] == {}


def test_after_call_ledger_failure_reports_original_wrapper_attempt(upgraded, monkeypatch):
    s = upgraded
    original = s.collector._persist_ledger
    def failed(ledger):
        if s.provider.calls:
            raise OSError("fixture disk failure after actual wrapper")
        return original(ledger)
    monkeypatch.setattr(s.collector, "_persist_ledger", failed)
    result = at(s)
    assert result["status"] == "partial" and result["provider_attempts"] == 1
    assert len(s.provider.calls) == 1
    assert read(s.collector.state_path)["days"][str(s.clock["ms"] // base.DAY_MS)] == 1


def test_genuine_source32_full_inventory_large_parent_preserved(tmp_db, tmp_path, monkeypatch):
    from kaiba.providers import _http
    from tests.test_allocation_runtime import context, snapshot
    from tests.test_lesson_timing_upgrade import provision_lesson_runtime
    from tests.test_prospective_quotes import Provider

    value = provision_lesson_runtime(tmp_path, monkeypatch)
    clock = value["clock"]
    monkeypatch.setattr(_http, "now_ms", lambda: clock["ms"])
    parent = json.loads((value["root"] / "state.json").read_bytes())
    parent["padding"] = "x" * 1_188_103
    write(value["root"] / "state.json", parent)
    authority, before = context(value), snapshot(value["root"])
    assert len(authority.leased_pairs()) == 12 and len(authority.retired_history()) == 10
    provider = Provider(clock)
    from types import SimpleNamespace
    s = SimpleNamespace(research=value["root"], root=value["learning"], clock=clock, provider=provider)
    install(s, snapshot=authority.snapshot_token)
    result = s.collector.once()
    # This fixture has only drain-only inventory: validate it all, enrich none.
    assert result["status"] == "idle" and result["provider_attempts"] == 0 and not provider.calls
    assert read(s.collector.state_path)["days"] == {}
    after = snapshot(value["root"])
    assert {k: v for k, v in before.items() if not k.endswith("-shm")} == {
        k: v for k, v in after.items() if not k.endswith("-shm")}
    assert context(value).snapshot_token == authority.snapshot_token
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_genuine_source32_pending_admission_captures_without_retired_book_changes(tmp_db, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kaiba.providers import _http
    from tests.test_allocation_runtime import context, snapshot
    from tests.test_lesson_timing_upgrade import provision_lesson_runtime
    from tests.test_prospective_quotes import Provider
    from tests.test_research_allocation import _op

    value = provision_lesson_runtime(tmp_path, monkeypatch)
    clock = value["clock"]
    monkeypatch.setattr(_http, "now_ms", lambda: clock["ms"])
    parent = read(value["root"] / "state.json")
    parent["padding"] = "x" * 1_188_103
    write(value["root"] / "state.json", parent)
    authority = context(value)
    token = "0x" + "b" * 40
    result = authority.runner().run_candidates(value["new"], [_op(value, token=token)], {}, now_ms=clock["ms"])
    assert not result["errors"]
    pending = 0
    for pair in authority.leased_pairs():
        with sqlite3.connect(pair["book_path"]) as conn:
            pending += conn.execute("SELECT COUNT(*) FROM candidate_entries WHERE status='pending'").fetchone()[0]
    assert pending > 0
    before = snapshot(value["root"])
    provider = Provider(clock)
    s = SimpleNamespace(research=value["root"], root=value["learning"], clock=clock, provider=provider)
    install(s, snapshot=authority.snapshot_token)
    report = s.collector.once()
    assert report["status"] == "captured" and report["provider_attempts"] == 1 and len(provider.calls) == 1
    assert report["attempt"]["target"]["kind"] == "pending"
    assert report["attempt"]["target"]["allocation_lease"] == "admission"
    assert report["attempt"]["target"]["token"] == token
    marker = report["attempt"]["cache"]["original_capture"]
    assert marker["status"] == "retained"
    capsule = upgrade.gmgn_observations.validate_capsule(s.collector.capture_store.get(marker["reference"]))
    assert capsule["target"]["allocation_snapshot_token"] == authority.snapshot_token
    after = snapshot(value["root"])
    assert {k: v for k, v in before.items() if not k.endswith("-shm")} == {
        k: v for k, v in after.items() if not k.endswith("-shm")}
    assert context(value).snapshot_token == authority.snapshot_token
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_prepare_is_detached_and_not_a_counter_reset(upgraded):
    s = upgraded
    state = read(s.collector.state_path)
    original = copy.deepcopy(state)
    plan = upgrade.prepare_upgrade(state, None, now_ms=s.clock["ms"])
    plan["ledger"]["days"]["0"] = {}
    assert state == original and upgrade.prepare_upgrade(state, None, now_ms=s.clock["ms"])["ledger"]["days"] == {}
