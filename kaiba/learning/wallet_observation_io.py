"""Durable physical-read admission for the existing fixed-wallet research review.

Only ``prepare_store`` may create genesis. The ongoing wrapper calls the original
review once and never changes its selections, baselines, rounds or intake rules.
Fixed legacy query intents are explicitly unavailable: retained tuple pages are
not a replacement full history. No provider or canonical database writer exists.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path

from kaiba.learning import autolearn_behavior as ab
from kaiba.learning import wallet_observation_tape as tape
from kaiba.learning.autolearn_io import SourceDeferred

VERSION = "wallet-observation-io/1"
QUIET_MS = 15 * 60_000
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
MAX_READS, MAX_ROWS, MAX_BYTES = 10, 10_000, 4 * 1024 * 1024
ROW_RESERVATION = 4096
MAX_DIAGNOSTICS = 128
STORE_FILE, GENESIS_FILE, REPORT_FILE = "store.json", "genesis.json", "report.json"
HORIZON_SQL = "SELECT value FROM kv WHERE key='retention:swaps:horizon_ms'"
_FIELDS = ("token", "side", "amount_token", "amount_native", "usd_value", "source", "tx")
_PROJECTED = ",".join(f"CASE WHEN length(CAST({f} AS BLOB))<=512 THEN {f} ELSE NULL END AS {f}" for f in _FIELDS)
_OVERSIZED = " OR ".join(f"length(CAST({f} AS BLOB))>512" for f in _FIELDS)
_SELECT = f"SELECT id,ts_ms,{_PROJECTED},({_OVERSIZED}) AS oversized_fields "
_FROM = "FROM swaps INDEXED BY idx_swaps_wallet WHERE chain=? AND wallet=? AND ts_ms>=? AND ts_ms<=? "
LEGACY_SQL = _SELECT + _FROM + "ORDER BY ts_ms,id LIMIT ?"
PAGE_SQL = _SELECT + _FROM + "AND (swaps.ts_ms,swaps.id)>(?,?) ORDER BY swaps.ts_ms,swaps.id LIMIT ?"


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _require(condition, reason):
    if not condition:
        raise SourceDeferred("wallet_tape:" + reason)


def source_versions():
    root = Path(__file__).resolve().parents[1]
    return {**tape.source_versions(), **ab.source_versions(),
            **{name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in (
                "learning/wallet_observation_io.py", "learning/fixed_wallet_study.py",
                "learning/autolearn_wallets.py")}}


def _root(store_dir, learning_root):
    path, learning = Path(store_dir), Path(learning_root).resolve()
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "store_symlink")
    path = path.resolve()
    _require(path != learning and path.is_relative_to(learning), "store_outside_learning_root")
    return path


def _read(path, cap=MAX_STORE_BYTES):
    _require(not path.is_symlink() and path.is_file(), "artifact_missing_or_symlink:" + path.name)
    with path.open("rb") as handle:
        raw = handle.read(cap + 1)
    _require(len(raw) <= cap, "artifact_byte_cap:" + path.name)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SourceDeferred("wallet_tape:artifact_invalid:" + path.name) from exc
    _require(isinstance(value, dict), "artifact_shape:" + path.name)
    return value


def _atomic(path, value, cap=MAX_STORE_BYTES):
    raw = _encoded(value) + b"\n"
    _require(len(raw) <= cap and not path.is_symlink(), "write_byte_cap_or_symlink")
    temporary = path.with_name(path.name + ".partial")
    _require(not temporary.is_symlink(), "temporary_symlink")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _lock(root, now):
    lock = _read(root.parent / "worker.lock", 4096)
    _require(lock.get("pid") == os.getpid() and _integer(lock.get("started_ms"), 1)
             and lock["started_ms"] <= now, "exclusive_parent_worker_lock_required")


def _seal(value, field="store_sha256"):
    value = copy.deepcopy(value)
    value.pop(field, None)
    value[field] = _sha(value)
    return value


def _check_seal(value, field):
    _require(value.get(field) == _sha({k: v for k, v in value.items() if k != field}), field + "_mismatch")


def _cas(root, state, expected):
    """Keep caller synchronized even if an interruption loses os.replace's ACK."""
    try:
        _require(_sha(_read(root / STORE_FILE)) == _sha(expected), "store_CAS_conflict")
        candidate = _seal(state)
        _require(len(_encoded(candidate)) + 1 <= MAX_STORE_BYTES, "store_saturated_no_eviction")
        _atomic(root / STORE_FILE, candidate)
    except BaseException:
        durable = _read(root / STORE_FILE)
        state.clear()
        state.update(durable)
        raise
    state.clear()
    state.update(candidate)


def prepare_store(fixed_state, *, store_dir, learning_root, now_ms,
                  eligible_after_ms):
    """Explicit installer-only absent-store initialization; no source reads.

    ``eligible_after_ms`` must be the prior review's mtime plus fifteen minutes,
    or install time plus fifteen minutes when that prior frontier is unavailable.
    Existing artifacts always refuse; this API is never called by the wrapper.
    """
    _require(_integer(now_ms, 1) and _integer(eligible_after_ms, 1), "invalid_initial_quiet_frontier")
    root = _root(store_dir, learning_root)
    _lock(root, now_ms)
    _require(not root.exists() or not any(root.iterdir()), "prepare_requires_absent_empty_store")
    initialized = tape.initialize(fixed_state, now_ms=now_ms)
    _require(initialized.get("status") == "initialized", "fixed_genesis_invalid")
    genesis = _seal({"version": VERSION, "created_ms": now_ms,
        "eligible_after_ms": eligible_after_ms, "source_versions": source_versions(),
        "fixed_manifest_sha256": initialized["state"]["manifest_sha256"],
        "fixed_state_sha256": _sha(fixed_state), "initial_engine_sha256": initialized["state"]["state_sha256"],
        "baseline_references": copy.deepcopy(fixed_state["selections"]),
        "funding_allowed": False}, "genesis_sha256")
    state = _seal({"version": VERSION, "genesis_sha256": genesis["genesis_sha256"],
        "source_versions": genesis["source_versions"], "engine_state": initialized["state"],
        "quota": {"slot": 0, "eligible_after_ms": eligible_after_ms, "last_attempt_ms": None,
                  "attempts": [], "lifetime_attempts": 0, "previous_slot_sha256": None,
                  "metadata_retry_after_ms": 0, "preflight_after_ms": 0},
        "diagnostics": [], "resolutions": [], "updated_ms": now_ms})
    root.mkdir(parents=True, exist_ok=True)
    _atomic(root / GENESIS_FILE, genesis)
    _atomic(root / STORE_FILE, state)
    return genesis


def validate_store(*, store_dir, learning_root, expected_genesis, now_ms):
    root = _root(store_dir, learning_root)
    _require(isinstance(expected_genesis, str) and len(expected_genesis) == 64, "expected_genesis_required")
    genesis, state = _read(root / GENESIS_FILE, 128 * 1024), _read(root / STORE_FILE)
    _check_seal(genesis, "genesis_sha256")
    _check_seal(state, "store_sha256")
    _require(genesis.get("genesis_sha256") == expected_genesis == state.get("genesis_sha256"), "genesis_conflict")
    _require(genesis.get("version") == state.get("version") == VERSION, "version_conflict")
    _require(genesis.get("source_versions") == state.get("source_versions") == source_versions(), "source_conflict")
    _require(_integer(now_ms, 1) and _integer(state.get("updated_ms"), 1)
             and state["updated_ms"] <= now_ms, "store_clock_invalid")
    try:
        engine = tape._state(state.get("engine_state"), now_ms)
    except ValueError as exc:
        raise SourceDeferred("wallet_tape:engine_state_invalid:" + str(exc)) from exc
    _require(engine["manifest_sha256"] == genesis["fixed_manifest_sha256"]
             and _sha(engine["fixed_state"]) == genesis["fixed_state_sha256"], "fixed_anchor_conflict")
    quota = state.get("quota")
    _require(isinstance(quota, dict) and _integer(quota.get("slot"))
             and _integer(quota.get("eligible_after_ms"), 1)
             and _integer(quota.get("lifetime_attempts"))
             and isinstance(quota.get("attempts"), list) and len(quota["attempts"]) <= MAX_READS,
             "quota_invalid")
    attempts = quota["attempts"]
    _require(len({a.get("intent_sha256") for a in attempts}) == len(attempts), "duplicate_attempt")
    for attempt in attempts:
        _require(isinstance(attempt, dict) and attempt.get("status") in ("admitted", "observed", "failed")
                 and _integer(attempt.get("started_ms"), 1) and attempt["started_ms"] <= now_ms
                 and _integer(attempt.get("charged_rows")) and _integer(attempt.get("charged_bytes"))
                 and _integer(attempt.get("requested_limit"), 1)
                 and attempt["requested_limit"] <= tape.MAX_INPUT_ROWS, "attempt_invalid")
        _require(_integer(attempt.get("original_requested_limit"), 1)
                 and attempt["requested_limit"] <= attempt["original_requested_limit"] <= 1001
                 and attempt.get("kind") in ("fixed", "dynamic")
                 and isinstance(attempt.get("wallet"), list) and len(attempt["wallet"]) == 2,
                 "attempt_scope_invalid")
        if attempt["kind"] == "fixed":
            _require(tuple(attempt["wallet"]) in tape.MEMBERS
                     and attempt["intent_sha256"] == _sha({"fixed_wallet": attempt["wallet"]}), "fixed_attempt_identity_invalid")
        if "completed_ms" in attempt:
            _require(_integer(attempt["completed_ms"], 1)
                     and attempt["started_ms"] <= attempt["completed_ms"] <= now_ms, "attempt_completion_invalid")
        if attempt["status"] != "observed":
            _require(attempt["charged_rows"] == attempt["requested_limit"]
                     and attempt["charged_bytes"] == attempt["requested_limit"] * ROW_RESERVATION,
                     "unresolved_reservation_refunded")
        else:
            _require(attempt["charged_rows"] <= attempt["requested_limit"]
                     and attempt["charged_bytes"] <= attempt["requested_limit"] * ROW_RESERVATION
                     and _integer(attempt.get("captured_ms"), 1)
                     and attempt["started_ms"] <= attempt["captured_ms"] <= now_ms,
                     "observed_attempt_invalid")
    _require(sum(a["charged_rows"] for a in attempts) <= MAX_ROWS
             and sum(a["charged_bytes"] for a in attempts) <= MAX_BYTES, "quota_exceeded")
    _require(sum(a["kind"] == "dynamic" for a in attempts) <= 4, "dynamic_quota_exceeded")
    _require(quota.get("last_attempt_ms") == (max(a.get("completed_ms", a["started_ms"]) for a in attempts) if attempts else None),
             "attempt_frontier_invalid")
    _require(quota["lifetime_attempts"] >= len(attempts), "lifetime_attempts_invalid")
    _require(quota["eligible_after_ms"] >= genesis["eligible_after_ms"], "initial_quiet_frontier_widened")
    _require(_integer(quota.get("metadata_retry_after_ms")) and _integer(quota.get("preflight_after_ms")),
             "metadata_frontier_invalid")
    _require(isinstance(state.get("diagnostics"), list) and len(state["diagnostics"]) <= MAX_DIAGNOSTICS,
             "diagnostics_cap")
    resolutions = state.get("resolutions")
    _require(isinstance(resolutions, list) and len(resolutions) <= len(state["diagnostics"]), "resolutions_invalid")
    diagnostic_ids = {_sha(d) for d in state["diagnostics"]}
    _require(len({r.get("diagnostic_sha256") for r in resolutions}) == len(resolutions)
             and all(r.get("diagnostic_sha256") in diagnostic_ids for r in resolutions), "resolution_identity_invalid")
    for resolution in resolutions:
        diagnostic = next(d for d in state["diagnostics"] if _sha(d) == resolution["diagnostic_sha256"])
        wallet = tuple(resolution.get("wallet", ()))
        _require(diagnostic.get("transient") is True and diagnostic.get("wallet") == list(wallet)
                 and wallet in tape.MEMBERS, "permanent_diagnostic_resolved")
        _require(any(r["chain_sha256"] == resolution.get("repair_receipt_sha256")
                     for r in engine["wallets"][":".join(wallet)]["receipts"]), "resolution_receipt_missing")
    return state


class _Capture:
    def __init__(self, reader, root, state, clock, now):
        self.reader, self.root, self.state, self.clock = reader, root, state, clock
        self.frontier, self.wal_error, self.collector_horizon, self.fatal_error = now, None, None, None

    def now(self):
        supplied = self.clock()
        _require(type(supplied) in (int, float) and math.isfinite(supplied) and supplied > 0, "invalid_capture_clock")
        self.frontier = max(self.frontier, int(supplied * 1000))
        return self.frontier

    def save(self, before):
        try:
            self.state["updated_ms"] = self.now()
            _lock(self.root, self.state["updated_ms"])
            _cas(self.root, self.state, before)
        except SourceDeferred as exc:
            self.fatal_error = exc
            raise

    def diagnostic(self, reason, wallet=None, *, scope=None, transient=False):
        before = copy.deepcopy(self.state)
        value = {"reason": str(reason)[:240], "wallet": list(wallet) if wallet else None,
                 "scope": copy.deepcopy(scope), "transient": transient}
        if value not in self.state["diagnostics"]:
            _require(len(self.state["diagnostics"]) < MAX_DIAGNOSTICS, "diagnostics_saturated_no_eviction")
            self.state["diagnostics"].append(value)
            self.save(before)

    def unresolved(self):
        resolved = {r["diagnostic_sha256"] for r in self.state["resolutions"]}
        return [d for d in self.state["diagnostics"] if _sha(d) not in resolved]

    def resolve_covered(self, wallet, start, upper, *, complete):
        if not complete:
            return
        slot = self.state["engine_state"]["wallets"][":".join(wallet)]
        for diagnostic in self.unresolved():
            scope = diagnostic.get("scope")
            if (not diagnostic.get("transient") or diagnostic.get("wallet") != list(wallet)
                    or not isinstance(scope, dict) or start > scope["start_ms"] or upper < scope["end_ms"]
                    or any(g["start_ms"] <= scope["end_ms"] and g["end_ms"] >= scope["start_ms"]
                           for g in slot["unobserved_intervals"])):
                continue
            self.state["resolutions"].append({"diagnostic_sha256": _sha(diagnostic), "wallet": list(wallet),
                "repair_engine_sha256": self.state["engine_state"]["state_sha256"],
                "repair_receipt_sha256": slot["receipts"][-1]["chain_sha256"],
                "captured_ms": slot["latest_capture_ms"]})

    def remaining(self):
        attempts = self.state["quota"]["attempts"]
        return MAX_READS - len(attempts), MAX_ROWS - sum(a["charged_rows"] for a in attempts), \
            MAX_BYTES - sum(a["charged_bytes"] for a in attempts)

    def open_slot(self):
        quota, now = self.state["quota"], self.now()
        # An admitted-only receipt can survive a parent crash after slow fsync or
        # dispatch. Its start clock cannot prove when the last source call ended.
        unresolved = [a for a in quota["attempts"] if a["status"] == "admitted"]
        if unresolved and not quota.get("recovery_quiet_after_ms"):
            before = copy.deepcopy(self.state)
            quota["recovery_quiet_after_ms"] = now + QUIET_MS
            self.save(before)
        if now < quota.get("metadata_retry_after_ms", 0):
            return False
        due = (quota["last_attempt_ms"] + QUIET_MS if quota["last_attempt_ms"] is not None
               else quota["eligible_after_ms"])
        due = max(due, quota.get("recovery_quiet_after_ms", 0))
        if now < due:
            return bool(quota["attempts"])
        if quota["attempts"]:
            before = copy.deepcopy(self.state)
            prior = _sha(quota)
            self.state["quota"] = {**quota, "slot": quota["slot"] + 1,
                "eligible_after_ms": now, "last_attempt_ms": None, "attempts": [],
                "previous_slot_sha256": prior, "recovery_quiet_after_ms": 0}
            self.save(before)
        return True

    def physical(self, sql, args, *, intent, wallet, fixed, original_args=None):
        _require(self.wal_error is None, "sticky_WAL_refusal")
        quota = self.state["quota"]
        _require(self.now() >= max(quota["eligible_after_ms"], quota.get("metadata_retry_after_ms", 0)),
                 "initial_or_metadata_quiet_frontier")
        digest = _sha(intent)
        _require(all(a["intent_sha256"] != digest for a in quota["attempts"]), "attempt_already_admitted_no_retry")
        reads, rows_left, bytes_left = self.remaining()
        limit = args[-1]
        _require(reads > 0 and rows_left >= limit and bytes_left >= limit * ROW_RESERVATION,
                 "physical_reservation_exhausted")
        if not fixed:
            _require(sum(a["kind"] == "dynamic" for a in quota["attempts"]) < 4, "dynamic_four_read_cap")
        before = copy.deepcopy(self.state)
        attempt = {"intent_sha256": digest, "wallet": list(wallet), "kind": "fixed" if fixed else "dynamic",
            "started_ms": self.now(), "requested_limit": limit, "status": "admitted",
            "original_requested_limit": (original_args or args)[-1],
            "charged_rows": limit, "charged_bytes": limit * ROW_RESERVATION,
            "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(), "args_sha256": _sha(list(args)),
            "original_args_sha256": _sha(list(original_args or args))}
        quota["attempts"].append(attempt)
        quota["last_attempt_ms"] = attempt["started_ms"]
        quota["lifetime_attempts"] += 1
        self.save(before)  # fsynced admission survives source failure and caller interruption
        try:
            result = self.reader.rows(sql, args)
        except BaseException as exc:
            if isinstance(exc, SourceDeferred) and "wal_over_limit:" in str(exc):
                self.wal_error = exc
            before = copy.deepcopy(self.state)
            attempt = self.state["quota"]["attempts"][-1]
            attempt["status"] = "failed"
            attempt["failure_type"] = type(exc).__name__
            attempt["completed_ms"] = self.now()
            self.state["quota"]["last_attempt_ms"] = attempt["completed_ms"]
            self.save(before)
            raise
        _require(isinstance(result, list) and len(result) <= limit, "physical_result_cap_or_shape")
        payload = sum(sum(len(str(v).encode()) if v is not None else 4 for v in r.values()) for r in result)
        _require(payload <= limit * ROW_RESERVATION, "physical_projection_bound_exceeded")
        observed = {"status": "observed", "charged_rows": len(result), "charged_bytes": payload,
                    "captured_ms": self.now(), "completed_ms": self.now(), "result_sha256": _sha(result)}
        if not fixed:
            if len(result) == limit:
                observed.update(status="failed", charged_rows=limit, charged_bytes=limit * ROW_RESERVATION,
                                failure_type="unproven_dynamic_limit_prefix")
            before = copy.deepcopy(self.state)
            self.settle(observed)
            self.save(before)
        return result, observed

    def settle(self, observed):
        self.state["quota"]["attempts"][-1].update(observed)
        self.state["quota"]["last_attempt_ms"] = observed["completed_ms"]

    def eager(self):
        if not self.open_slot():
            return
        if all(any(a["kind"] == "fixed" and a["wallet"] == list(wallet)
                   for a in self.state["quota"]["attempts"]) for wallet in tape.MEMBERS):
            return
        if self.now() < self.state["quota"].get("preflight_after_ms", 0):
            return
        before = copy.deepcopy(self.state)
        self.state["quota"]["preflight_after_ms"] = self.now() + QUIET_MS
        self.save(before)
        try:
            horizon_rows = self.reader.rows(HORIZON_SQL)
            _require(len(horizon_rows) == 1, "retention_watermark_missing")
            raw = horizon_rows[0]["value"]
            value = json.loads(raw) if isinstance(raw, str) else raw
            _require(_integer(value) and value <= self.now(), "retention_watermark_invalid")
            horizon = value
        except (SourceDeferred, ValueError, KeyError, TypeError) as exc:
            before = copy.deepcopy(self.state)
            self.state["quota"]["metadata_retry_after_ms"] = self.now() + QUIET_MS
            self.save(before)
            if isinstance(exc, SourceDeferred) and "wal_over_limit:" in str(exc):
                self.wal_error = exc
            for wallet in tape.MEMBERS:
                name = ":".join(wallet)
                engine = self.state["engine_state"]
                slot = engine["wallets"][name]
                baseline = engine["fixed_state"]["selections"][name]["baseline"]
                pending = slot.get("open_page_window")
                start = (baseline["window_start_ms"] if slot["seed_status"] == "unseeded" else
                         pending["window_start_ms"] if pending else slot["completed_through_ms"] + 1)
                upper = (engine["fixed_state"]["manifest"]["selected_ms"] if slot["seed_status"] == "unseeded" else
                         pending["requested_upper_ms"] if pending else self.now())
                self.diagnostic("retention_preflight_unavailable:" + type(exc).__name__, wallet,
                                scope={"start_ms": start, "end_ms": upper}, transient=True)
            if self.wal_error is not None:
                raise self.wal_error from exc
            return
        for wallet in tape.MEMBERS:
            name = ":".join(wallet)
            now, engine = self.now(), self.state["engine_state"]
            slot = engine["wallets"][name]
            if any(a["kind"] == "fixed" and a["wallet"] == list(wallet) for a in self.state["quota"]["attempts"]):
                continue
            lower = max(now - ab.MAX_READ_AGE_MS, horizon)
            if slot["seed_status"] == "unseeded":
                baseline = engine["fixed_state"]["selections"][name]["baseline"]
                start, upper, after = baseline["window_start_ms"], engine["fixed_state"]["manifest"]["selected_ms"], None
                if start < lower or upper < start:
                    self.diagnostic("original_seed_interval_unavailable", wallet)
                    continue
                _, rows_left, bytes_left = self.remaining()
                limit = min(1001, baseline["rows"] + 1, rows_left, bytes_left // ROW_RESERVATION)
                if limit <= baseline["rows"]:
                    self.diagnostic("original_seed_reservation_unprovable", wallet,
                                    scope={"start_ms": start, "end_ms": upper}, transient=True)
                    continue
                sql = LEGACY_SQL
                args = (*wallet, start, upper, limit)
                kind = "seed"
            else:
                pending = slot["open_page_window"]
                start = max(pending["window_start_ms"] if pending else slot["completed_through_ms"] + 1, lower)
                upper = pending["requested_upper_ms"] if pending else now
                after = pending["cursor"] if pending else slot["page_cursor"]
                if start > upper:
                    self.diagnostic("unfinished_interval_aged_out" if pending else "source_interval_unavailable", wallet)
                    continue
                reads, rows_left, bytes_left = self.remaining()
                limit = min(1001, rows_left, bytes_left // ROW_RESERVATION)
                if not reads or limit <= 0:
                    self.diagnostic("physical_reservation_exhausted", wallet,
                                    scope={"start_ms": start, "end_ms": upper}, transient=True)
                    continue
                sql = PAGE_SQL
                args = (*wallet, start, upper, *(after or (0, 0)), limit)
                kind = "page"
            intent = {"fixed_wallet": list(wallet)}  # one physical query/member/slot, including failed queries
            try:
                rows, observed = self.physical(sql, args, intent=intent, wallet=wallet, fixed=True)
                captured = observed["captured_ms"]
                if kind == "seed":
                    receipt = {"query_status": "declared_query_complete", "window_start_ms": start,
                        "as_of_ms": upper, "captured_ms": captured, "horizon_ms": horizon}
                    if len(rows) >= limit:
                        result = {"state": self.state["engine_state"],
                                  "diagnostic": {"reason": "seed_prefix_not_proved_exhausted"}}
                        observed.update(status="failed", charged_rows=limit, charged_bytes=limit * ROW_RESERVATION,
                                        failure_type="unproven_seed_limit_prefix")
                    else:
                        result = tape.seed(self.state["engine_state"], wallet, rows, receipt, now_ms=captured)
                else:
                    receipt = {"page_version": tape.PAGE_VERSION, "query_status": "observed_page",
                        "window_start_ms": start, "requested_upper_ms": upper, "captured_ms": captured,
                        "horizon_ms": horizon, "after": after, "returned_limit": limit, "has_more": len(rows) == limit}
                    result = tape.ingest_page(self.state["engine_state"], wallet, rows, receipt, now_ms=captured)
                before = copy.deepcopy(self.state)
                self.settle(observed)
                self.state["engine_state"] = result["state"]
                if result.get("diagnostic") is not None:
                    value = {"reason": str(result["diagnostic"].get("reason", "engine_rejected"))[:240],
                             "wallet": list(wallet), "scope": {"start_ms": start, "end_ms": upper}, "transient": False}
                    if value not in self.state["diagnostics"]:
                        _require(len(self.state["diagnostics"]) < MAX_DIAGNOSTICS, "diagnostics_saturated_no_eviction")
                        self.state["diagnostics"].append(value)
                else:
                    self.resolve_covered(wallet, start, upper,
                                         complete=kind == "seed" or not receipt["has_more"])
                self.save(before)
            except SourceDeferred as exc:
                if self.fatal_error is not None:
                    raise self.fatal_error from exc
                self.diagnostic(str(exc), wallet, scope={"start_ms": start, "end_ms": upper}, transient=True)
                if self.wal_error is not None:
                    raise self.wal_error from exc

    def rows(self, sql, args=()):
        if self.fatal_error is not None:
            raise self.fatal_error
        if self.wal_error is not None:
            raise self.wal_error
        if sql == HORIZON_SQL or "swaps" not in sql.lower():
            try:
                rows = self.reader.rows(sql, args)
            except SourceDeferred as exc:
                if "wal_over_limit:" in str(exc):
                    self.wal_error = exc
                raise
            if sql == HORIZON_SQL:
                self.collector_horizon = ab.load_json(rows[0]["value"], rows[0]["value"]) if rows else 0
            return rows
        _require(sql == LEGACY_SQL and len(args) == 5, "unexpected_wallet_query_shape")
        chain, address, start, end, limit = args
        _require(_integer(start) and _integer(end, 1) and _integer(limit, 1) and limit <= 1001,
                 "legacy_query_arguments_invalid")
        wallet = (chain, address)
        if wallet in tape.MEMBERS:
            baseline = self.state["engine_state"]["fixed_state"]["selections"][":".join(wallet)]
            _require(type(self.collector_horizon) is int
                     and start == max(baseline["window_start_ms"], end - ab.MAX_READ_AGE_MS, self.collector_horizon),
                     "fixed_legacy_interval_conflict")
            raise SourceDeferred("wallet_tape:routed_fixed_to_durable_observation; original_full_window_unavailable")
        _require(self.collector_horizon is not None and type(self.collector_horizon) is int
                 and start >= max(self.now() - ab.MAX_READ_AGE_MS, self.collector_horizon)
                 and end <= self.now() and start <= end, "dynamic_interval_or_horizon_invalid")
        _, rows_left, bytes_left = self.remaining()
        physical_limit = min(limit, rows_left, bytes_left // ROW_RESERVATION)
        _require(physical_limit > 0, "physical_reservation_exhausted")
        physical_args = (*args[:-1], physical_limit)
        result, _ = self.physical(sql, physical_args, intent={"dynamic_sql": sql, "args": list(args)},
                                  wallet=wallet, fixed=False, original_args=tuple(args))
        _require(len(result) < physical_limit, "dynamic_prefix_not_proved_exhausted_no_retry")
        return result

    def q(self, sql, args=()):
        _require("swaps" not in sql.lower(), "unexpected_tuple_wallet_query")
        if self.wal_error is not None:
            raise self.wal_error
        if self.fatal_error is not None:
            raise self.fatal_error
        try:
            return self.reader.q(sql, args)
        except SourceDeferred as exc:
            if "wal_over_limit:" in str(exc):
                self.wal_error = exc
            raise

    def __getattr__(self, name):
        return getattr(self.reader, name)


def run_review_with_tape(review_callable, reader, runner_root, now_ms, prior_state, *,
                         store_dir, learning_root, expected_genesis, clock=time.time):
    """Capture eagerly, then run exactly one unmodified review with guarded reads."""
    root = _root(store_dir, learning_root)
    _lock(root, now_ms)
    state = validate_store(store_dir=root, learning_root=learning_root,
                           expected_genesis=expected_genesis, now_ms=max(now_ms, int(clock() * 1000)))
    fixed = (prior_state or {}).get("fixed_study_state")
    try:
        tape.fixed._study(fixed, max(now_ms, int(clock() * 1000)))
    except (ValueError, TypeError, KeyError) as exc:
        raise SourceDeferred("wallet_tape:parent_fixed_study_invalid") from exc
    _require(isinstance(fixed, Mapping) and fixed.get("manifest_sha256") == state["engine_state"]["manifest_sha256"]
             and fixed.get("manifest") == state["engine_state"]["fixed_state"]["manifest"]
             and fixed.get("selections") == state["engine_state"]["fixed_state"]["selections"],
             "parent_fixed_study_anchor_conflict")
    capture = _Capture(reader, root, state, clock, now_ms)
    try:
        capture.eager()
        result = review_callable(capture, runner_root, now_ms=now_ms, prior_state=prior_state)
        if capture.wal_error is not None:
            raise capture.wal_error
        if capture.fatal_error is not None:
            raise capture.fatal_error
    finally:
        diagnostics = capture.unresolved() + [
            {"reason": "unresolved_source_admission", "wallet": a["wallet"]}
            for a in capture.state["quota"]["attempts"] if a["status"] != "observed"]
        report = tape.report(capture.state["engine_state"], now_ms=capture.now(), diagnostics=diagnostics)
        report["physical_quota"] = copy.deepcopy(capture.state["quota"])
        report["physical_quota"]["remaining"] = dict(zip(("reads", "rows", "bytes"), capture.remaining(), strict=True))
        report["genesis_sha256"] = expected_genesis
        report["transport_version"] = VERSION
        report["diagnostic_history"] = {"count": len(capture.state["diagnostics"]),
            "sha256": _sha(capture.state["diagnostics"]), "resolved": len(capture.state["resolutions"]),
            "resolutions_sha256": _sha(capture.state["resolutions"])}
        _atomic(root / REPORT_FILE, report, MAX_REPORT_BYTES)
    pointer = {"version": VERSION, "genesis_sha256": expected_genesis,
               "store_sha256": capture.state["store_sha256"], "report_file": REPORT_FILE,
               "report_sha256": _sha(report), "status": report["status"], "funding_allowed": False}
    result["wallet_observation_tape"] = pointer
    result["review"]["wallet_observation_tape"] = pointer
    return result


__all__ = ["prepare_store", "validate_store", "run_review_with_tape", "source_versions"]
