"""Allocated observation uses one immutable authority and the existing allowance."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, digest
from kaiba.learning import allocation_runtime as runtime
from kaiba.learning import prospective_quotes as pq
from tests.test_allocation_runtime import packed, provision_runtime, rig, snapshot


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *a, **kw: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("subprocess forbidden"))


class Provider:
    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def token_info(self, token, chain, **kwargs):
        self.calls.append((token, chain, kwargs))
        key = f"gmgn-cli token info --address {token} --chain {chain.value} --raw"
        return SimpleNamespace(ok=False, data=None, receipt=Receipt(provider="gmgn", endpoint="token.info",
            observed_at_ms=self.clock["ms"], basis=EvidenceBasis.UNAVAILABLE,
            request_digest=digest(key), response_digest=digest(None), note="offline fixture unavailable"))


def observer(tmp_path):
    value = provision_runtime(rig(tmp_path))
    provider = Provider(value["clock"])
    collector = pq.ProspectiveQuoteCollector(value["root"], value["learning"] / "prospective-quotes",
        learning_root=value["learning"], clock=lambda: value["clock"]["ms"] / 1000, provider=provider)
    return {**value, "collector": collector, "provider": provider}


def change_snapshot(value):
    """Explicit fixture replacement with a valid, different metadata binding."""
    root, own = value["root"], value["runtime_dir"]
    parent = json.loads((root / "state.json").read_bytes())
    authority = json.loads((own / "authority.json").read_bytes())
    authority["activation_ms"] += 1
    (own / "authority.json").write_bytes(packed(authority))
    metadata = runtime.metadata_for((own / "receipt.json").read_bytes(),
        (own / "authority.json").read_bytes(), (own / "new-books.json").read_bytes(), parent_state=parent)
    raw = packed(metadata)
    (own / "metadata.json").write_bytes(raw)
    (own / "commit-receipt.json").write_bytes(packed({"version": "allocation-commit/1",
        "authority_sha256": hashlib.sha256((own / "authority.json").read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(raw).hexdigest(), "activation_ms": authority["activation_ms"],
        "visible_ms": authority["activation_ms"] - 2, "completed_ms": authority["activation_ms"] - 1}))
    parent[runtime.POINTER] = runtime.pointer_for(raw)
    (root / "state.json").write_bytes(packed(parent))
    return runtime.load_context(root, learning_root=value["learning"], now_ms=value["clock"]["ms"],
                                clock=lambda: value["clock"]["ms"] / 1000).snapshot_token


def context(value):
    return runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)


@pytest.mark.parametrize("flag", [False, True])
def test_targets_scan_twelve_leases_and_keep_both_old_books_without_touching_retired(tmp_path, tmp_db, monkeypatch, flag):
    value = observer(tmp_path)
    parent = copy.deepcopy(value["parent"])
    parent["admission_clock"] = flag
    (value["root"] / "state.json").write_bytes(packed(parent))
    expected = context(value)
    before, calls = snapshot(value["root"]), []
    original = value["collector"]._book
    def traced(manifest, arm, *, now):
        calls.append((manifest["campaign_id"], arm["hypothesis_id"], len(manifest["hypotheses"])))
        return original(manifest, arm, now=now)
    monkeypatch.setattr(value["collector"], "_book", traced)
    targets, errors = value["collector"]._targets(value["clock"]["ms"])
    pairs = expected.leased_pairs()
    assert not errors and len(calls) == len(pairs) == 12
    assert {(cid, hid) for cid, hid, _ in calls} == {
        (p["manifest"]["campaign_id"], p["arm"]["hypothesis_id"]) for p in pairs}
    assert {hid for _, hid, _ in calls} & set(value["retired"]) == set()
    assert set(value["draining"]).issubset({hid for _, hid, _ in calls})
    assert all(size in (12, 10) for _, _, size in calls)
    assert len(targets) == 1 and targets[0]["kind"] == "open"
    assert targets[0]["token"] == value["token"] and targets[0]["allocation_lease"] == "drain_only"
    assert targets[0]["allocation_snapshot_token"] == expected.snapshot_token
    assert snapshot(value["root"]) == before and not value["provider"].calls


def test_existing_attempts_counters_and_typed_provider_policy_are_shared(tmp_path, tmp_db, monkeypatch):
    value = observer(tmp_path)
    collector, now = value["collector"], value["clock"]["ms"]
    day, key = str(now // pq.DAY_MS), "robinhood:" + value["token"]
    old = {"attempt_id": "existing-unresolved-attempt", "status": "unknown", "admitted_ms": now - 30_000}
    state = {"version": pq.VERSION, "policy": copy.deepcopy(pq.POLICY), "days": {day: 99}, "tokens": {},
             "attempts": [old], "retry_after_ms": 0, "updated_ms": now - 1}
    collector.state_path.write_bytes(packed(state))
    result = collector.once()
    assert result["provider_attempts"] == 1 and result["daily_admitted_attempts"] == 100
    saved = json.loads(collector.state_path.read_bytes())
    assert saved["attempts"][0] == old and saved["days"] == {day: 100}
    assert saved["attempts"][-1]["attempt_id"] == digest([day, 100, key])
    assert value["provider"].calls == [(value["token"], Chain.ROBINHOOD,
        {"priority": Priority.RESEARCH, "ttl_s": 15, "stale_grace_s": 0, "timeout_s": 6, "wait_for_slot_s": 0})]
    def forbidden(*args):
        pytest.fail("existing daily cap must refuse before another target scan")
    monkeypatch.setattr(collector, "_targets", forbidden)
    value["clock"]["ms"] += 15_000
    blocked = collector.once()
    assert blocked["provider_attempts"] == 0 and blocked["reason"] == "daily_research_attempt_cap"
    assert len(value["provider"].calls) == 1


@pytest.mark.parametrize("fault", ["parent", "metadata", "new_book", "retired_book"])
def test_invalid_allocation_never_falls_back_or_repairs_a_book(tmp_path, tmp_db, fault):
    value = observer(tmp_path)
    if fault == "parent":
        parent = copy.deepcopy(value["parent"])
        parent.pop(runtime.POINTER)
        (value["root"] / "state.json").write_bytes(packed(parent))
    elif fault == "metadata":
        path = value["runtime_dir"] / "metadata.json"
        path.write_bytes(path.read_bytes() + b"\n")
    elif fault == "new_book":
        path = Path(next(p["book_path"] for p in context(value).leased_pairs() if p["lease"] == "admission"))
        path.unlink()
    else:
        path = Path(context(value).retired_history()[0]["book_path"])
        path.write_bytes(path.read_bytes() + b"external-corruption")
    before = snapshot(value["root"])
    result = value["collector"].once()
    assert result["provider_attempts"] == 0 and not value["provider"].calls
    assert result["status"] in ("partial", "deferred")
    assert result["daily_admitted_attempts"] == 0
    assert snapshot(value["root"]) == before


def test_changed_valid_binding_before_admission_consumes_no_attempt(tmp_path, tmp_db, monkeypatch):
    value = observer(tmp_path)
    collector = value["collector"]
    original, tokens = collector._limiter_preflight, []
    initial = context(value).snapshot_token
    def changed(state):
        result = original(state)
        tokens.append(change_snapshot(value))
        return result
    monkeypatch.setattr(collector, "_limiter_preflight", changed)
    result = collector.once()
    assert tokens and tokens[0] != initial
    assert result["provider_attempts"] == 0 and result["daily_admitted_attempts"] == 0
    assert result["reason"] == "eligibility_or_allocation_changed_before_admission"
    assert not value["provider"].calls


def test_changed_binding_after_reservation_keeps_slot_but_rejects_same_token(tmp_path, tmp_db, monkeypatch):
    value = observer(tmp_path)
    collector, original, changed = value["collector"], pq._save, []
    def save(path, root, state):
        original(path, root, state)
        if path == collector.state_path and state.get("attempts") and not changed:
            changed.append(change_snapshot(value))
    monkeypatch.setattr(pq, "_save", save)
    result = collector.once()
    assert changed and result["provider_attempts"] == 0 and result["daily_admitted_attempts"] == 1
    assert result["attempt"]["status"] == "no_longer_active" and not value["provider"].calls
    saved = json.loads(collector.state_path.read_bytes())
    assert sum(saved["days"].values()) == 1 and len(saved["attempts"]) == 1


def test_dispatch_revalidates_even_if_post_reservation_scan_returned_old_targets(tmp_path, tmp_db, monkeypatch):
    value = observer(tmp_path)
    collector, calls = value["collector"], []
    original = collector._targets
    def changed(now):
        result = original(now)
        calls.append(now)
        if len(calls) == 3:
            change_snapshot(value)
        return result
    monkeypatch.setattr(collector, "_targets", changed)
    result = collector.once()
    assert len(calls) == 3 and result["provider_attempts"] == 0 and not value["provider"].calls
    assert result["daily_admitted_attempts"] == 1
    assert result["reason"] == "allocation_authority_snapshot_changed"
