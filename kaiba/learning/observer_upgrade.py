"""Additive, source-bound quote scheduling; frozen observer/campaigns stay intact.

Codex AUTOLEARN-BUDGET / AUTOLEARN-QUOTES-CAP, 2026-10-18. This worker uses the
original lock, durable admission and typed provider boundary. Its separate sealed
declaration and ledger must be installed explicitly; missing state is never reset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from kaiba.core.config import get_settings
from kaiba.core.schemas import digest
from kaiba.learning import allocation_runtime, gmgn_observations, quote_budget
from kaiba.learning import prospective_quotes as base
from kaiba.learning.autolearn_io import SourceDeferred

VERSION = "observer-upgrade/1"
PARENT_CAP = allocation_runtime.MAX_JSON
MANIFEST_NAME = "observer-upgrade.json"
LEDGER_NAME = "quote-budget-ledger.json"
DECLARATION_CAP = 32_768
LEDGER_CAP = 262_144


def _require(ok, reason):
    if not ok:
        raise ValueError("observer_upgrade:" + reason)


def _pairs(items):
    out = {}
    for key, value in items:
        _require(key not in out, "duplicate_json_key")
        out[key] = value
    return out


def _bad_number(value):
    raise ValueError("observer_upgrade:nonfinite_json")


def _read(path, root, cap):
    path = base._safe(Path(path), Path(root))
    _require(path.is_file() and path.stat().st_size <= cap, "missing_or_oversized_artifact")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    _require(len(raw) <= cap, "artifact_grew_over_cap")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_bad_number)
    _require(type(value) is dict, "artifact_object_required")
    return raw, value


def source_bindings():
    """Bind selector lineage separately, without changing the capsule schema."""
    root = Path(__file__).resolve().parents[1]
    names = set(allocation_runtime.source_versions()) | set(gmgn_observations.source_bindings())
    names.update(("learning/observer_upgrade.py", "learning/quote_budget.py"))
    _require(len(names) <= 64, "source_file_cap")
    result, total = {}, 0
    for name in sorted(names):
        path = base._safe(root / name, root)
        _require(path.is_file() and path.stat().st_size <= 1_048_576, "source_file_size")
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
        total += len(raw)
        _require(len(raw) <= 1_048_576 and total <= 16_777_216, "source_byte_cap")
        result[name] = hashlib.sha256(raw).hexdigest()
    return result


def prepare_upgrade(observer_state, allocation_snapshot_token, *, now_ms):
    """Installer recipe only; never writes, activates, refills or changes a book."""
    _require(allocation_snapshot_token is None or (
        type(allocation_snapshot_token) is str and len(allocation_snapshot_token) == 64
        and all(c in "0123456789abcdef" for c in allocation_snapshot_token)), "snapshot_identity")
    ledger = quote_budget.reconcile_ledger(None, observer_state, now_ms)
    manifest = {"version": VERSION, "policy": quote_budget.POLICY,
        "base_version": base.VERSION, "base_policy": base.POLICY,
        "parent_cap": PARENT_CAP, "observer_state_cap": base.MAX_JSON_BYTES,
        "allocation_snapshot_token": allocation_snapshot_token,
        "source_bindings": source_bindings(), "funding_allowed": False}
    return {"manifest": manifest, "ledger": ledger}


class BudgetedQuoteCollector(gmgn_observations.CapturingQuoteCollector):
    """Original provider/admission path plus a bounded parent and budget ledger."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        original = self.provider
        if original is None:
            from kaiba.providers import gmgn_cli
            original = gmgn_cli
        self._original_provider = original
        self.provider = SimpleNamespace(token_info=self._token_info)
        self.manifest_path = self.state_dir / MANIFEST_NAME
        self.ledger_path = self.state_dir / LEDGER_NAME
        self._manifest = None
        self._selected = None
        self._baseline_days = None
        self._reservation = None
        self._initial_state = None
        self._original_wrapper_calls = 0

    def _declaration(self):
        _, declared = _read(self.manifest_path, self.root, DECLARATION_CAP)
        expected = {"version": VERSION, "policy": quote_budget.POLICY,
            "base_version": base.VERSION, "base_policy": base.POLICY,
            "parent_cap": PARENT_CAP, "observer_state_cap": base.MAX_JSON_BYTES,
            "allocation_snapshot_token": declared.get("allocation_snapshot_token"),
            "source_bindings": source_bindings(), "funding_allowed": False}
        _require(set(declared) == set(expected) and declared == expected, "declaration_or_source_changed")
        token = declared["allocation_snapshot_token"]
        _require(token is None or (type(token) is str and len(token) == 64
            and all(c in "0123456789abcdef" for c in token)), "snapshot_identity")
        if self._manifest is not None:
            _require(declared == self._manifest, "declaration_changed_during_cycle")
        return declared

    def _observer_state(self):
        return _read(self.state_path, self.root, base.MAX_JSON_BYTES)[1]

    def _persist_ledger(self, ledger):
        _require(len(base._json(ledger).encode()) <= LEDGER_CAP, "budget_ledger_byte_cap")
        base._save(self.ledger_path, self.root, ledger)

    def _reconcile(self, state, now):
        # The installer must preserve this ledger across releases and declarations.
        # Missing/corrupt files never become a new genesis inside a worker.
        previous = _read(self.ledger_path, self.root, LEDGER_CAP)[1]
        ledger = quote_budget.reconcile_ledger(previous, state, now)
        self._persist_ledger(ledger)
        return ledger

    def _current_reservation(self, state, now):
        if self._selected is None:
            return None
        attempts = state.get("attempts")
        _require(type(attempts) is list, "attempt_tail_required")
        if not attempts:
            return None
        attempt = attempts[-1]
        if attempt.get("target") != self._selected or attempt.get("status") != "admitted":
            return None
        admitted = base._integer(attempt.get("admitted_ms"), minimum=1)
        day = str(admitted // base.DAY_MS)
        baseline = self._baseline_days.get(day, 0)
        count = state["days"].get(day, 0)
        if count == baseline:
            return None  # An old crash admission can never authorize a new dispatch.
        _require(count == baseline + 1 and admitted <= now, "unexpected_cycle_admission")
        expected_id = digest([day, count, self._selected["chain"] + ":" + self._selected["token"]])
        _require(attempt.get("attempt_id") == expected_id, "reservation_identity")
        reservation = {k: attempt[k] for k in ("attempt_id", "target", "admitted_ms")}
        if self._reservation is not None:
            _require(reservation == self._reservation, "reservation_changed")
        self._reservation = reservation
        return reservation

    def _load_targets(self, now):
        """Original guarded inventory; only parent cap and retired-lease policy differ."""
        began = time.monotonic()
        _, active = _read(self.research_dir / "state.json", self.research_dir, PARENT_CAP)
        allocation = allocation_runtime.load_context(self.research_dir, learning_root=self.root,
            state=active, now_ms=now, clock=self.clock)
        actual = allocation.snapshot_token if allocation is not None else None
        _require(actual == self._manifest["allocation_snapshot_token"], "allocation_snapshot_changed")
        pairs, targets, errors = [], [], []
        if allocation is not None:
            pairs = [(p["manifest"], p["arm"], p["lease"]) for p in allocation.leased_pairs()]
            _require(len(pairs) <= 12, "allocation_active_lease_cap")
        else:
            names = active.get("campaign_files", [])
            _require(type(names) is list and len(names) <= base.MAX_BOOKS
                and len(set(names)) == len(names), "active_campaign_cap_or_duplicates")
            current = base.source_versions()
            for name in names:
                _require(type(name) is str and Path(name).name == name and name.endswith(".json"), "manifest_path")
                manifest = _read(self.research_dir / name, self.research_dir, base.MAX_JSON_BYTES)[1]
                _require(manifest.get("version") == base.CAMPAIGN_VERSION
                    and manifest.get("source_versions") == current
                    and manifest.get("campaign_id") == base._campaign_id(manifest)
                    and 0 < base._integer(manifest["selected_ms"], minimum=1) <= now, "manifest_not_frozen_current")
                pairs.extend((manifest, arm, None) for arm in manifest["hypotheses"])
                _require(len(pairs) <= base.MAX_BOOKS, "active_book_cap")
        for manifest, arm, lease in pairs:
            if time.monotonic() - began >= base.SCAN_SECONDS:
                raise SourceDeferred("research_metadata_scan_deadline")
            if lease in ("drain_only", "retired"):
                continue  # Full Context validation above still covers these books.
            _require(lease in (None, "admission"), "unknown_allocation_lease")
            _require(arm.get("status") == "PAPER_ONLY" and arm.get("spec_hash") == digest(arm["spec"])
                and arm.get("hypothesis_id") == "ph_" + arm["spec_hash"][:24], "arm_not_frozen_paper")
            try:
                for target in self._book(manifest, arm, now=now):
                    if allocation is not None:
                        target.update(allocation_snapshot_token=actual, allocation_lease=lease)
                    targets.append(target)
            except (sqlite3.Error, ValueError, KeyError, TypeError, OSError):
                errors.append({"hypothesis_id": arm["hypothesis_id"], "reason": "candidate_book_unavailable"})
        if allocation is not None:
            allocation.revalidate(now_ms=self._now())
        _require(len({(t["chain"], t["token"]) for t in targets}) <= base.MAX_TARGETS, "unique_target_cap")
        _require(len(targets) <= base.MAX_BOOKS * base.ROW_LIMIT, "candidate_row_cap")
        return targets, errors

    def _targets(self, now):
        self._declaration()
        state = self._observer_state()
        ledger = self._reconcile(state, self._now())  # Before dispatch, including after base reservation.
        reserved = self._current_reservation(state, self._now())
        targets, errors = self._load_targets(self._now())
        if errors:
            raise SourceDeferred("observer_upgrade_incomplete_book_inventory")
        if self._selected is not None:
            exact = [t for t in targets if t == self._selected]
            if not exact:
                return [], []
            if reserved is None:
                reason = quote_budget.admission_reason(self._selected, state, ledger, self._now())
                if reason:
                    raise SourceDeferred(reason)
            return [self._selected.copy()], []
        # Skip an already usable cache rather than allowing one cached earliest
        # deadline to starve other pending candidates. This makes no provider call.
        unique = {}
        for target in targets:
            key = target["chain"], target["token"]
            rank = (target["kind"] != "pending", target["signal_ms"] if target["kind"] == "pending" else 0,
                    target["campaign_id"], target["hypothesis_id"], target["book"])
            if key not in unique or rank < unique[key][0]:
                unique[key] = rank, target
        remaining = [row[1] for row in unique.values()]
        while remaining:
            selected = quote_budget.select_target(remaining, state, ledger, self._now())
            if selected is None:
                break
            if self._fresh_cache(selected) is None:
                self._selected = selected
                return [selected.copy()], []  # Neutralizes base lexicographic round-robin.
            remaining = [t for t in remaining if (t["chain"], t["token"]) != (selected["chain"], selected["token"])]
            if time.monotonic() - self._scan_started >= base.SCAN_SECONDS:
                raise SourceDeferred("research_metadata_scan_deadline")
        return [], []

    def _limiter_preflight(self, state):
        result = super()._limiter_preflight(state)
        self._declaration()
        current = self._observer_state()
        ledger = self._reconcile(current, self._now())
        reason = quote_budget.admission_reason(self._selected, current, ledger, self._now())
        if reason:
            raise SourceDeferred(reason)
        return result

    def _token_info(self, token, chain, **kwargs):
        """Last boundary: an old/crashed admission never becomes dispatch authority."""
        _require(self._original_wrapper_calls == 0, "duplicate_cycle_dispatch")
        self._declaration()
        state = self._observer_state()
        now = self._now()
        ledger = self._reconcile(state, now)
        _require(self._current_reservation(state, now) is not None, "current_reservation_required")
        targets, errors = self._load_targets(self._now())
        _require(not errors and any(t == self._selected for t in targets), "dispatch_target_no_longer_active")
        now = self._now()
        _require(token == self._selected["token"] and chain.value == self._selected["chain"], "dispatch_target_changed")
        from kaiba.core.limiter import Priority
        _require(kwargs == {"priority": Priority.RESEARCH, "ttl_s": 15, "stale_grace_s": 0,
            "timeout_s": 6, "wait_for_slot_s": 0}, "dispatch_policy_changed")
        day, key = str(now // base.DAY_MS), chain.value + ":" + token
        _require(state["days"].get(day, 0) <= quote_budget.paced_limit(now), "paced_limit_after_admission")
        if self._selected["kind"] == "pending":
            _require(30_000 <= now - self._selected["signal_ms"] <= 120_000, "entry_deadline_before_dispatch")
        else:
            bucket = ledger["days"][str(self._reservation["admitted_ms"] // base.DAY_MS)]
            _require(bucket["open_counts"].get(key, 0) + bucket["unknown_open"] <= 6, "open_daily_limit_after_admission")
            _require(now - self._initial_state["tokens"].get(key, 0) >= 600_000, "open_spacing_before_dispatch")
        self._original_wrapper_calls += 1
        return self._original_provider.token_info(token, chain, **kwargs)

    def _once(self):
        self._manifest, self._selected, self._reservation = None, None, None
        self._original_wrapper_calls = 0
        self._scan_started = time.monotonic()
        try:
            lock = base._load(self.state_dir / "worker.lock", self.root, limit=4096)
            _require(lock == {"pid": os.getpid()}, "worker_lock_not_proved")
            self._manifest = self._declaration()
            state = self._observer_state()
            self._initial_state = state
            self._reconcile(state, self._now())
            self._baseline_days = dict(state["days"])
            try:
                report = super()._once()
            finally:
                # Recovery also accounts for a base admission interrupted before
                # our post-reservation hook, without refunding or resending it.
                self._reconcile(self._observer_state(), self._now())
            report["observer_upgrade"] = {"version": VERSION, "policy": quote_budget.POLICY,
                "declaration_sha256": digest(self._manifest), "funding_allowed": False}
            report["base_provider_attempts"] = report.get("provider_attempts")
            report["provider_attempts"] = self._original_wrapper_calls
            report["provider_attempts_basis"] = "original_typed_wrapper_invocations_not_network_calls"
            base._save(self.state_dir / "upgrade-report.json", self.root, report)
            return report
        except SourceDeferred as exc:
            return {"version": VERSION, "status": "deferred", "reason": str(exc),
                    "provider_attempts": self._original_wrapper_calls, "funding_allowed": False}
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            return {"version": VERSION, "status": "partial", "reason": "observer_upgrade_evidence_invalid",
                    "provider_attempts": self._original_wrapper_calls, "funding_allowed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--research-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--db", type=Path)
    args = parser.parse_args(argv)
    root = get_settings().kaiba_data_dir / "learning"
    report = BudgetedQuoteCollector(args.research_dir or root / "autolearn",
        args.state_dir or root / "prospective-quotes", db_path=args.db).once()
    print(base._json({k: report.get(k) for k in ("status", "reason", "provider_attempts", "funding_allowed")}))
    return 1 if report.get("status") in ("partial", "unknown") else 0


if __name__ == "__main__":
    raise SystemExit(main())
