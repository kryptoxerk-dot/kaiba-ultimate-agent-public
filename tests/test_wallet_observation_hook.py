"""Routing controls for the narrow scheduled wallet-review integration."""
from __future__ import annotations

import copy
import inspect
import textwrap

import pytest

from kaiba.learning import autolearn, autolearn_wallets, wallet_observation_io
from kaiba.learning.autolearn_io import SourceDeferred

NOW = 1_792_106_328_738


def _case(tmp_path, monkeypatch):
    learner = autolearn.AutoLearner.__new__(autolearn.AutoLearner)
    learner.state_dir = tmp_path / "autolearn"
    learner.state_dir.mkdir()
    learner.db_path = tmp_path / "kaiba.db"
    learner.clock = lambda: NOW / 1000
    prior = {"selections": {"original": {"baseline": {"rows": 833}}}, "round": 19}
    state = {"wallet_review_state": copy.deepcopy(prior), "last_agent_review_ms": NOW}
    calls = []

    def original(reader, runner_root, *, now_ms, prior_state):
        calls.append(("original", reader, runner_root, now_ms, copy.deepcopy(prior_state)))
        return {"review": {"candidates": [], "observation_queue": []},
                "intake": {"status": "unchanged"}, "next_state": copy.deepcopy(prior_state)}

    def tape(review_callable, reader, runner_root, now_ms, prior_state, **kwargs):
        calls.append(("tape", kwargs))
        return review_callable(reader, runner_root, now_ms=now_ms, prior_state=prior_state)

    monkeypatch.setattr(autolearn_wallets, "run_wallet_review", original)
    monkeypatch.setattr(wallet_observation_io, "run_review_with_tape", tape)
    return learner, state, prior, calls


def test_no_installation_anchors_preserves_original_library_review(tmp_path, monkeypatch):
    learner, state, prior, calls = _case(tmp_path, monkeypatch)
    reader = object()
    learner._scheduled_reviews(reader, state, {}, NOW)
    assert [c[0] for c in calls] == ["original"]
    assert calls[0][1:] == (reader, learner.db_path.parent / "claude-runners", NOW, prior)
    assert state["wallet_review_state"] == prior
    assert state["last_wallet_review_ms"] == NOW


def test_installed_pointer_routes_once_with_exact_genesis_and_existing_clocks(tmp_path, monkeypatch):
    learner, state, prior, calls = _case(tmp_path, monkeypatch)
    state["wallet_tape_upgrade"] = {"genesis_sha256": "a" * 64}
    learner._scheduled_reviews(object(), state, {}, NOW)
    assert [c[0] for c in calls] == ["tape", "original"]
    assert calls[0][1] == {"store_dir": learner.state_dir / "wallet-observation-tape",
        "learning_root": learner.state_dir, "expected_genesis": "a" * 64, "clock": learner.clock}
    assert state["wallet_review_state"] == prior
    assert state["wallet_tape_upgrade"] == {"genesis_sha256": "a" * 64}


def _missing_pointer_control(tmp_path, monkeypatch):
    learner, state, _, calls = _case(tmp_path, monkeypatch)
    (learner.state_dir / "wallet-observation-tape").mkdir()
    original = copy.deepcopy(state)
    with pytest.raises(KeyError, match="wallet_tape_upgrade"):
        learner._scheduled_reviews(object(), state, {}, NOW)
    assert state == original and calls == []
    assert not (learner.state_dir / "latest-wallet-review.json").exists()


def test_lost_parent_pointer_with_retained_store_never_falls_back(tmp_path, monkeypatch):
    _missing_pointer_control(tmp_path, monkeypatch)


@pytest.mark.parametrize("pointer", [None, {}, {"unexpected": "value"}])
def test_malformed_installed_pointer_never_uses_original_review(tmp_path, monkeypatch, pointer):
    learner, state, _, calls = _case(tmp_path, monkeypatch)
    state["wallet_tape_upgrade"] = pointer
    with pytest.raises((KeyError, TypeError)):
        learner._scheduled_reviews(object(), state, {}, NOW)
    assert calls == [] and "last_wallet_review_ms" not in state


def test_deferred_tape_preserves_parent_selection_and_review_clock(tmp_path, monkeypatch):
    learner, state, _, calls = _case(tmp_path, monkeypatch)
    state["wallet_tape_upgrade"] = {"genesis_sha256": "a" * 64}
    before = copy.deepcopy(state)

    def unavailable(*args, **kwargs):
        raise SourceDeferred("wallet_tape:artifact_missing_or_symlink:store.json")

    monkeypatch.setattr(wallet_observation_io, "run_review_with_tape", unavailable)
    with pytest.raises(SourceDeferred, match="artifact_missing"):
        learner._scheduled_reviews(object(), state, {}, NOW)
    assert calls == [] and state == before


def test_lost_pointer_control_catches_removed_store_anchor_guard(tmp_path, monkeypatch):
    source = textwrap.dedent(inspect.getsource(autolearn.AutoLearner._scheduled_reviews))
    changed = source.replace(' or os.path.lexists(self.state_dir / "wallet-observation-tape")', "", 1)
    assert changed != source
    namespace = dict(vars(autolearn))
    exec(compile(changed, "<offline-wallet-tape-anchor-mutation>", "exec"), namespace)
    monkeypatch.setattr(autolearn.AutoLearner, "_scheduled_reviews", namespace["_scheduled_reviews"])
    with pytest.raises(BaseException, match="DID NOT RAISE"):
        _missing_pointer_control(tmp_path, monkeypatch)
