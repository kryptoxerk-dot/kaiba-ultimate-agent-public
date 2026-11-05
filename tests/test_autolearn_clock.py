"""Durable natural drain, actual recovery frontier and strict paper dispatch."""
from __future__ import annotations

import copy
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from kaiba.learning import admission_paper as ap
from kaiba.learning import autolearn_clock as ac
from kaiba.learning import candidate_paper as cp
from kaiba.learning import strategy_campaign as sc
from kaiba.learning.signal_audit import ExitLadder

T0 = 1_000_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*a, **kw):
        raise AssertionError("clock cutover must not use provider transport")
    monkeypatch.setattr("socket.socket", refused)
    monkeypatch.setattr("subprocess.Popen", refused)


def setup(tmp_path, *, legacy=True):
    root = tmp_path / "learning/autolearn"
    root.mkdir(parents=True)
    clock = {"ms": T0 + 1000}
    learner = SimpleNamespace(state_dir=root, learning_root=tmp_path / "learning",
        max_candidates=2, max_stake_usd="30", max_open_per_arm=3,
        groups=[("sm-trenches", "sol")], clock=lambda: clock["ms"] / 1000, _ladder=ExitLadder)
    state = {"version": "fixture", "campaign_files": [], "assessment": {}, "pending_lessons": []}
    old = None
    if legacy:
        old = sc.generate_campaign([], ExitLadder(), now_ms=T0,
            groups=learner.groups, max_candidates=2)
        old["hypotheses"] = [a for a in old["hypotheses"] if a["spec"]["exit"]["hold_ms"] == 60_000]
        old["campaign_id"] = sc._campaign_id(old)
        name = old["campaign_id"] + ".json"
        ac._atomic(root / name, old)
        state["campaign_files"] = [name]
        state["assessment"] = {old["campaign_id"]: {"campaign_id": old["campaign_id"], "legacy_evidence": "retained"}}
    ac._atomic(root / "state.json", state)
    ac._atomic(root / "worker.lock", {"pid": os.getpid(), "started_ms": T0})
    return learner, state, clock, old


def op(name="source", at=T0 + 1000):
    return {"decision_id": name, "ts_ms": at, "lane": "sm-trenches", "chain": "sol",
        "token": "token", "mode": "live", "paper_stake_usd": "30", "features": {}}


def quotes(at, price="1"):
    return {("sol", "token"): {"quote_ts_ms": at, "price_usd": price, "liquidity_usd": "1000000",
        "native_usd": "100", "decimals": 6, "source": "synthetic", "tax_known_zero": True,
        "venue_fee_bps": 125, "venue_fee_source": "fixture-assumption"}}


def paper(learner, cls=ac.CampaignPaper):
    return cls(learner.state_dir / "paper", learning_root=learner.learning_root,
        max_stake_usd=learner.max_stake_usd, max_open_per_arm=learner.max_open_per_arm, clock=learner.clock)


def count(path, table):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


def old_entered(bundle):
    learner, _, clock, old = bundle
    clock["ms"] = T0 + 31_000
    result = paper(learner, cp.CandidatePaperRunner).run_candidates(old, [op()], quotes(clock["ms"]), now_ms=clock["ms"])
    assert not result["errors"] and result["arms"][0]["order_count"] == 1


def test_natural_drain_seals_only_new_registrations_and_keeps_actual_old_targets(tmp_path):
    bundle = setup(tmp_path)
    learner, state, clock, old = bundle
    old_entered(bundle)
    original = (learner.state_dir / state["campaign_files"][0]).read_bytes()
    source_hashes = sc.source_versions()
    old_assessment = copy.deepcopy(state["assessment"])
    manifests = ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert manifests == [old] and state["clock_upgrade"]["phase"] == "draining"
    assert paper(learner).watched_tokens(old) == [("sol", "token")]
    clock["ms"] = T0 + 40_000
    drained = paper(learner).run_candidates(old, [op("new", clock["ms"])], quotes(clock["ms"]), now_ms=clock["ms"])
    assert drained["clock_drain_only"] and drained["arms"][0]["entries"] == {"opened": 1}
    path = paper(learner).book_path(old["campaign_id"], old["hypotheses"][0]["hypothesis_id"])
    assert count(path, "orders") == 1
    clock["ms"] = T0 + 104_000
    closed = paper(learner).run_candidates(old, [], quotes(clock["ms"]), now_ms=clock["ms"])
    assert len(closed["outcomes"]) == 1 and closed["outcomes"][0]["opened_ms"] == T0 + 31_000
    current = ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert len(current) == 1 and current[0]["campaign_id"] != old["campaign_id"]
    assert state["clock_upgrade"]["phase"] == "active" and len(current[0]["hypotheses"]) <= learner.max_candidates
    assert count(path, "orders") == 2 and path.exists()
    assert (learner.state_dir / (old["campaign_id"] + ".json")).read_bytes() == original
    receipt = ac._load(learner.state_dir / state["clock_upgrade"]["receipt_file"])
    assert receipt["legacy_assessment"] == old_assessment
    assert receipt["legacy_artifacts"][0]["books"][0]["files"][0]["sha256"]
    assert receipt["history_deleted"] is False and state["assessment"] == old_assessment
    assert sc.source_versions() == source_hashes


def test_existing_pending_fills_normally_and_later_expires_without_fake_cancellation(tmp_path):
    learner, state, clock, old = setup(tmp_path)
    runner = paper(learner, cp.CandidatePaperRunner)
    runner.run_candidates(old, [op()], {}, now_ms=clock["ms"])
    assert ac.bootstrap_clock_cohort(learner, state, clock["ms"]) == [old]
    clock["ms"] = T0 + 31_000
    filled = paper(learner).run_candidates(old, [op("ignored")], quotes(clock["ms"]), now_ms=clock["ms"])
    assert filled["arms"][0]["entries"] == {"opened": 1}
    assert filled["arms"][0]["order_count"] == 1
    # Separate actual unsent entry must reach its ordinary deadline first.
    learner2, state2, clock2, old2 = setup(tmp_path / "second")
    paper(learner2, cp.CandidatePaperRunner).run_candidates(old2, [op()], {}, now_ms=clock2["ms"])
    clock2["ms"] = T0 + 125_000
    assert ac.bootstrap_clock_cohort(learner2, state2, clock2["ms"]) == [old2]
    expired = paper(learner2).expire_pending(old2, now_ms=clock2["ms"])
    assert expired["expired"] == 1
    assert ac.bootstrap_clock_cohort(learner2, state2, clock2["ms"])[0]["campaign_id"] != old2["campaign_id"]


@pytest.mark.parametrize("lost_ack", [False, True])
def test_pointer_crash_recovery_never_duplicates_or_replays_pre_activation_signals(tmp_path, monkeypatch, lost_ack):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    original = ac._atomic
    fired = []
    def crash(path, value):
        active = path.name == "state.json" and value.get("clock_upgrade", {}).get("phase") == "active"
        if active and not fired:
            fired.append(True)
            if lost_ack:
                original(path, value)
            raise RuntimeError("fixture pointer acknowledgement lost")
        original(path, value)
    monkeypatch.setattr(ac, "_atomic", crash)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    durable = ac._load(learner.state_dir / "state.json")
    assert state == durable  # The parent's finally cannot roll back a committed pointer.
    receipts = list(learner.state_dir.glob("clock-cutover-*.json"))
    assert len(receipts) == 1
    prepared = ac._load(receipts[0])
    prepared_ms = prepared["prepared_ms"]
    clock["ms"] += 30_000
    active = ac.bootstrap_clock_cohort(learner, state, clock["ms"])[0]
    activation = state["clock_upgrade"]["activation_ms"]
    assert activation == (prepared_ms if lost_ack else clock["ms"])
    assert active["selected_ms"] == prepared_ms
    assert len(state["retired_campaigns"]) == 1 and len(state["pending_lessons"]) == 1
    assert len(list(learner.state_dir.glob("pc_*.json"))) == 1
    clock["ms"] = activation + 31_001
    opportunities = [op("before", activation), op("after", activation + 1000)]
    result = paper(learner).run_candidates(active, opportunities, quotes(clock["ms"]), now_ms=clock["ms"])
    assert result["activation_excluded"] == [{"signal_id": "before", "reason": "signal_not_after_activation_frontier"}]
    assert [r["entries"] for r in result["arms"]] == [{"opened": 1}] * 2
    if not lost_ack:
        assert prepared_ms < activation


def test_state_CAS_conflict_is_preserved_even_when_parent_persists_caller_on_failure(tmp_path, monkeypatch):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    original = ac._atomic
    def external_write(path, value):
        original(path, value)
        if path.name.startswith("pc_"):
            competing = ac._load(learner.state_dir / "state.json") | {"external_marker": "preserve"}
            original(learner.state_dir / "state.json", competing)
    monkeypatch.setattr(ac, "_atomic", external_write)
    with pytest.raises(ValueError, match="before CAS"):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert state["external_marker"] == "preserve" and state["clock_upgrade"]["phase"] == "draining"
    assert ac._load(learner.state_dir / "state.json") == state


def test_legacy_prepared_archive_recovers_exact_books_and_fresh_activation(tmp_path, monkeypatch):
    bundle = setup(tmp_path)
    learner, state, clock, old = bundle
    old_entered(bundle)
    clock["ms"] = T0 + 104_000
    paper(learner, cp.CandidatePaperRunner).run_candidates(old, [], quotes(clock["ms"]), now_ms=clock["ms"])
    original = ac._atomic
    def crash(path, value):
        if path.name == "state.json" and value.get("clock_upgrade", {}).get("phase") == "active":
            raise RuntimeError("before active pointer")
        original(path, value)
    monkeypatch.setattr(ac, "_atomic", crash)
    with pytest.raises(RuntimeError):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    receipt_path = learner.state_dir / state["clock_upgrade"]["receipt_file"]
    prepared = receipt_path.read_bytes()
    clock["ms"] += 60_000
    monkeypatch.setattr(ac, "_atomic", original)
    current = ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert receipt_path.read_bytes() == prepared
    assert current[0]["selected_ms"] < state["selected_ms"] == clock["ms"]
    assert len(state["retired_campaigns"]) == 1


def test_stale_in_memory_input_never_rolls_back_an_already_committed_cohort(tmp_path):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    stale = copy.deepcopy(state)
    active = ac.bootstrap_clock_cohort(learner, state, clock["ms"])[0]
    with pytest.raises(ValueError, match="older than"):
        ac.bootstrap_clock_cohort(learner, stale, clock["ms"])
    assert stale == state
    again = ac.bootstrap_clock_cohort(learner, stale, clock["ms"])
    assert again == [active] and len(stale["retired_campaigns"]) == 1


@pytest.mark.parametrize("termination", [KeyboardInterrupt("after active commit"), SystemExit(23)])
def test_process_termination_after_pointer_commit_cannot_be_rolled_back_by_parent_finally(
        tmp_path, monkeypatch, termination):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    original = ac._atomic
    fired = []
    def interrupt_after_commit(path, value):
        original(path, value)
        if path.name == "state.json" and value.get("clock_upgrade", {}).get("phase") == "active" and not fired:
            fired.append(True)
            raise termination
    monkeypatch.setattr(ac, "_atomic", interrupt_after_commit)
    with pytest.raises(type(termination)) as caught:
        try:
            ac.bootstrap_clock_cohort(learner, state, clock["ms"])
        finally:
            # Match the actual worker's unconditional final state persistence.
            original(learner.state_dir / "state.json", state)
    assert caught.value is termination
    durable = ac._load(learner.state_dir / "state.json")
    assert durable == state and durable["clock_upgrade"]["phase"] == "active"
    committed_files = list(state["campaign_files"])
    committed_frontier = state["clock_upgrade"]["activation_ms"]
    clock["ms"] += 30_000
    result = ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert result[0]["campaign_id"] + ".json" == committed_files[0]
    assert state["campaign_files"] == committed_files
    assert state["clock_upgrade"]["activation_ms"] == committed_frontier
    assert len(state["retired_campaigns"]) == len(state["pending_lessons"]) == 1


def test_unsettled_or_missing_history_never_mints_a_new_cohort(tmp_path):
    learner, state, clock, old = setup(tmp_path)
    assert ac.bootstrap_clock_cohort(learner, state, clock["ms"]) == [old]
    assert state["clock_upgrade"]["inspection"]["status"] == "blocked"
    assert len(list(learner.state_dir.glob("pc_*.json"))) == 1
    assert list(learner.state_dir.glob("clock-cutover-*.json")) == []


@pytest.mark.parametrize("fault", ["module_policy", "manifest", "pointer", "lock"])
def test_tampered_prepared_or_active_artifacts_fail_closed(tmp_path, fault):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    active = ac.bootstrap_clock_cohort(learner, state, clock["ms"])[0]
    if fault == "module_policy":
        receipt_path = learner.state_dir / state["clock_upgrade"]["receipt_file"]
        receipt = ac._load(receipt_path)
        receipt["execution_policy"]["sources"][ap.SOURCE_KEY] = "bad"
        ac._atomic(receipt_path, receipt)
    elif fault == "manifest":
        path = learner.state_dir / state["campaign_files"][0]
        changed = ac._load(path)
        changed["hypotheses"][0]["spec_hash"] = "bad"
        ac._atomic(path, changed)
    elif fault == "pointer":
        state["selected_ms"] += 1
        ac._atomic(learner.state_dir / "state.json", state)
    else:
        ac._atomic(learner.state_dir / "worker.lock", {"pid": os.getpid() + 1, "started_ms": T0})
    with pytest.raises(ValueError):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert len(list(learner.state_dir.glob("pc_*.json"))) == 1
    if fault != "lock":
        with pytest.raises((ValueError, KeyError)):
            paper(learner).run_candidates(active, [], {}, now_ms=clock["ms"])


def test_dispatch_uses_strict_new_assessment_and_keeps_legacy_statistics_separate(tmp_path):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    active = ac.bootstrap_clock_cohort(learner, state, clock["ms"])[0]
    result = ac.assess_dispatch(active, [], now_ms=clock["ms"])
    assert result["execution_policy"] == ap.execution_policy()
    assert result["next_state"]["execution_policy_hash"]
    legacy = sc.generate_campaign([], ExitLadder(), now_ms=T0, groups=learner.groups, max_candidates=2)
    old = ac.assess_dispatch(legacy, [], now_ms=clock["ms"])
    assert "execution_policy" not in old
    with pytest.raises(ValueError, match="prior assessment"):
        ac.assess_dispatch(active, [], now_ms=clock["ms"], prior_state=sc.assess_campaign(active, [], now_ms=clock["ms"])["next_state"])


def test_no_capacity_widening_and_no_controller_work_without_current_worker_lock(tmp_path):
    learner, state, clock, _ = setup(tmp_path, legacy=False)
    learner.max_candidates = 13
    with pytest.raises(ValueError, match="capacity"):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    learner.max_candidates = 2
    (learner.state_dir / "worker.lock").unlink()
    with pytest.raises(ValueError, match="exclusive PID lock"):
        ac.bootstrap_clock_cohort(learner, state, clock["ms"])
    assert list(learner.state_dir.glob("pc_*.json")) == []
