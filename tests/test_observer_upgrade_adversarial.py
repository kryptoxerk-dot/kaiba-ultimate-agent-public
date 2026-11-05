"""Offline adversarial controls at the real observer admission/dispatch seams.

Only disposable fixture books and transport doubles are used. Inventory selection,
base durable reservations, capture, Context and source checks remain real.
"""
from __future__ import annotations

import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.learning import observer_upgrade as upgrade
from kaiba.learning import prospective_quotes as base
from tests.test_observer_upgrade import install, observer_setup, offline, upgraded  # noqa: F401
from tests.test_prospective_quotes import (
    SECOND,
    T0,
    TOKEN,
    at,
    cached_market,
    opportunity,
    read,
    snapshot,
    write,
)


def _register(s, opportunities, *, quotes=None, at_ms=None):
    if at_ms is not None:
        s.clock["ms"] = at_ms
    result = s.runner.run_candidates(
        s.manifest, opportunities, quotes or {}, now_ms=s.clock["ms"]
    )
    assert not result["errors"]
    return result


def _inventory(s):
    return [
        target
        for arm in s.manifest["hypotheses"]
        for target in s.collector._book(s.manifest, arm, now=s.clock["ms"])
    ]


def _after_adapter_fsync(s, monkeypatch, mutate):
    """Mutate only after the adapter's own ledger has actually been saved.

    The frame check selects the final provider adapter, after the unchanged
    base's last Context check; earlier reservation/reconciliation saves are real.
    """
    original = s.collector._persist_ledger
    changed = []

    def persist(ledger):
        original(ledger)
        frame = inspect.currentframe()
        try:
            while frame is not None:
                if (
                    frame.f_code.co_name == "_token_info"
                    and frame.f_locals.get("self") is s.collector
                ):
                    if not changed:
                        state = read(s.collector.state_path)
                        attempt = state["attempts"][-1]
                        assert attempt["status"] == "admitted"
                        assert s.collector._original_wrapper_calls == 0
                        changed.append(attempt)
                        mutate(attempt)
                    break
                frame = frame.f_back
        finally:
            del frame

    monkeypatch.setattr(s.collector, "_persist_ledger", persist)
    return changed


def test_genuine_duplicate_book_rows_over_raw_target_cap_still_admit_once(request):
    s = request.getfixturevalue("upgraded")
    opportunities = [
        {**opportunity(), "decision_id": f"repeat-signal-{i}"}
        for i in range(17)
    ]
    _register(s, opportunities)
    s.clock["ms"] = T0 + 31_000
    inventory = _inventory(s)
    assert len(inventory) > base.MAX_TARGETS
    assert len({(t["chain"], t["token"]) for t in inventory}) == 1
    assert len({t["book"] for t in inventory}) == 2

    report = s.collector.once()
    assert report["status"] == "captured" and report["provider_attempts"] == 1
    assert len(s.provider.calls) == 1 and s.provider.calls[0][0] == TOKEN
    assert read(s.collector.state_path)["days"][str(s.clock["ms"] // base.DAY_MS)] == 1
    assert report["attempt"]["cache"]["original_capture"]["status"] == "retained"


def test_cached_earliest_pending_does_not_starve_later_token(request):
    s = request.getfixturevalue("upgraded")
    _register(s, [opportunity(SECOND, at=T0 + 2_000)], at_ms=T0 + 2_000)
    s.clock["ms"] = T0 + 32_000
    cache_path = cached_market(s)
    before = cache_path.read_bytes()

    report = s.collector.once()
    assert report["status"] == "captured" and report["provider_attempts"] == 1
    assert len(s.provider.calls) == 1 and s.provider.calls[0][0] == SECOND
    assert report["attempt"]["target"]["kind"] == "pending"
    assert cache_path.read_bytes() == before


def test_real_pending_deadline_priority_beats_open_and_lexicographic_order(request):
    s = request.getfixturevalue("upgraded")
    lexically_first = "1" * 32
    now = T0 + 32_000
    _register(
        s,
        [opportunity(SECOND, at=T0 + 500), opportunity(lexically_first, at=T0 + 2_000)],
        quotes={"sol:" + TOKEN: snapshot(now)},
        at_ms=now,
    )
    inventory = _inventory(s)
    assert any(t["kind"] == "open" and t["token"] == TOKEN for t in inventory)
    assert any(t["kind"] == "pending" and t["token"] == lexically_first for t in inventory)
    assert lexically_first < SECOND

    report = s.collector.once()
    assert report["status"] == "captured" and report["provider_attempts"] == 1
    assert len(s.provider.calls) == 1 and s.provider.calls[0][0] == SECOND
    assert report["attempt"]["target"]["kind"] == "pending"
    assert report["attempt"]["target"]["signal_ms"] == T0 + 500


def test_reentrant_provider_at_still_admitted_boundary_cannot_dispatch_twice(request):
    s = request.getfixturevalue("upgraded")
    refusals, admission_states = [], []

    def reenter():
        s.provider.after = lambda: None
        admission_states.append(read(s.collector.state_path)["attempts"][-1]["status"])
        token, chain, kwargs = s.provider.calls[0]
        try:
            s.collector.provider.token_info(token, chain, **kwargs)
        except ValueError as exc:
            refusals.append(str(exc))

    s.provider.after = reenter
    report = at(s)
    assert admission_states == ["admitted"]
    assert len(s.provider.calls) == 1
    assert refusals == ["observer_upgrade:duplicate_cycle_dispatch"]
    assert report["status"] == "captured" and report["provider_attempts"] == 1


def test_book_expires_after_adapter_ledger_fsync_blocks_final_dispatch(request, monkeypatch):
    s = request.getfixturevalue("upgraded")

    def expire(attempt):
        with sqlite3.connect(s.research / "paper" / attempt["target"]["book"]) as conn:
            changed = conn.execute("UPDATE candidate_entries SET status='expired'").rowcount
            assert changed > 0

    changed = _after_adapter_fsync(s, monkeypatch, expire)
    report = at(s)
    assert len(changed) == 1
    assert report["provider_attempts"] == 0 and not s.provider.calls
    state = read(s.collector.state_path)
    assert state["attempts"][-1]["status"] == "unknown"
    day = str(s.clock["ms"] // base.DAY_MS)
    assert state["days"][day] == 1
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 1


def test_full_context_changes_after_adapter_ledger_fsync_blocks_dispatch(
    request, tmp_db, tmp_path, monkeypatch
):
    # Register offline transport guards contributed by the imported fixture.
    request.getfixturevalue("offline")
    from kaiba.providers import _http
    from tests.test_allocation_runtime import context
    from tests.test_lesson_timing_upgrade import provision_lesson_runtime
    from tests.test_prospective_quotes import Provider
    from tests.test_research_allocation import _op

    value = provision_lesson_runtime(tmp_path, monkeypatch)
    clock = value["clock"]
    monkeypatch.setattr(_http, "now_ms", lambda: clock["ms"])
    authority = context(value)
    token = "0x" + "b" * 40
    registered = authority.runner().run_candidates(
        value["new"], [_op(value, token=token)], {}, now_ms=clock["ms"]
    )
    assert not registered["errors"]
    s = SimpleNamespace(
        research=value["root"], root=value["learning"], clock=clock, provider=Provider(clock)
    )
    install(s, snapshot=authority.snapshot_token)

    def invalidate(_attempt):
        parent = read(s.research / "state.json")
        parent[upgrade.allocation_runtime.POINTER]["metadata_file_sha256"] = "0" * 64
        write(s.research / "state.json", parent)

    changed = _after_adapter_fsync(s, monkeypatch, invalidate)
    report = s.collector.once()
    assert len(changed) == 1
    assert report["provider_attempts"] == 0 and not s.provider.calls
    state = read(s.collector.state_path)
    day = str(clock["ms"] // base.DAY_MS)
    assert state["days"][day] == 1 and state["attempts"][-1]["status"] == "unknown"
    assert read(s.collector.ledger_path)["days"][day]["observed_total"] == 1
    with pytest.raises(ValueError):
        context(value)
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
