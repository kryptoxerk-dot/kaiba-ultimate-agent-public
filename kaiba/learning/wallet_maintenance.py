"""Declared wallet-only maintenance using the original retained-tape review.

No PAPER authority is issued here. A durable pending intent is never replayed;
only a durable completed result may recover publication without source work.
The existing learner lock, tape genesis, fixed selections and quotas are reused.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from kaiba.learning import autolearn_wallets as wallets
from kaiba.learning import wallet_observation_io as tape_io
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

VERSION = "wallet-maintenance/1"
INSTALL_VERSION = "wallet-maintenance-install/1"
DIRECTORY = "wallet-maintenance"
POINTER = "wallet_maintenance_upgrade"
MAX_PARENT_BYTES = 2 * 1024 * 1024
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
WAL_LIMIT = 1_500_000_000
QUIET_MS = 60_000
REVIEW_INTERVAL_MS = 15 * 60_000
WALLET_KEYS = frozenset({"wallet_review_state", "last_wallet_review_ms"})
SHA256 = re.compile(r"[0-9a-f]{64}")
POLICY = {"review_interval_ms": REVIEW_INTERVAL_MS, "physical_reads": 10, "dynamic_reads": 4,
          "physical_rows": 10_000, "physical_bytes": 4 * 1024 * 1024,
          "intake_per_day": 20, "history_days": 13, "wal_limit": WAL_LIMIT,
          "wal_quiet_ms": QUIET_MS, "funding_allowed": False, "paper_operations": False,
          "pending_retry": False, "lock_reclaim": False}


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("wallet_maintenance:" + reason)


def _integer(value: Any, minimum: int = 0) -> int:
    _require(type(value) is int and minimum <= value <= 2**63 - 1, "invalid_integer_clock")
    return value


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("wallet_maintenance:invalid_json") from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _safe(path: Path) -> Path:
    absolute = path.absolute()
    _require(not any(p.is_symlink() for p in (absolute, *absolute.parents)), "symlink_refused")
    return absolute.resolve()


def _object(pairs: list[tuple[str, Any]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        _require(key not in value, "duplicate_json_key")
        value[key] = item
    return value


def _read(path: Path, cap: int) -> tuple[bytes, dict]:
    _safe(path)
    _require(path.is_file(), "artifact_missing:" + path.name)
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    _require(len(raw) <= cap, "artifact_cap:" + path.name)
    value = json.loads(raw, object_pairs_hook=_object)
    _require(type(value) is dict, "artifact_object:" + path.name)
    _encoded(value)  # Also reject NaN accepted by the JSON reader.
    return raw, value


def _atomic(path: Path, value: dict, cap: int) -> None:
    _safe(path)
    raw = _encoded(value) + b"\n"
    _require(len(raw) <= cap, "write_cap:" + path.name)
    temporary = path.with_name(path.name + ".partial")
    _safe(temporary)
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def source_versions() -> dict:
    return {**tape_io.source_versions(), "learning/wallet_maintenance.py":
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _wallet(parent: dict) -> dict:
    return {key: copy.deepcopy(parent[key]) for key in WALLET_KEYS if key in parent}


def _validate_parent(parent: dict, now: int) -> None:
    _require(type(parent.get("wallet_review_state")) is dict, "prior_wallet_state_required")
    _require(_integer(parent.get("last_wallet_review_ms", 0)) <= now, "future_wallet_review")
    pointer = parent.get("wallet_tape_upgrade")
    _require(type(pointer) is dict and isinstance(pointer.get("genesis_sha256"), str)
             and len(pointer["genesis_sha256"]) == 64, "existing_tape_pointer_required")


def _digest(value: Any) -> bool:
    return type(value) is str and SHA256.fullmatch(value) is not None


def prepare_upgrade(parent: dict, *, state_dir: Path | str, db_path: Path | str, prepared_ms: int) -> dict:
    """Return an installer recipe without writing or creating any allowance.

    Installer must hold the existing worker lock, durably install declaration_raw
    as DIRECTORY/declaration.json and CAS-add only POINTER to this same parent.
    """
    now = _integer(prepared_ms, 1)
    root, db = _safe(Path(state_dir)), _safe(Path(db_path))
    _require(root.is_dir() and not db.is_relative_to(root), "invalid_roots")
    _, current = _read(root / "state.json", MAX_PARENT_BYTES)
    _require(parent == current and POINTER not in parent, "prepare_parent_conflict_or_already_installed")
    _validate_parent(parent, now)
    stored = tape_io.validate_store(store_dir=root / "wallet-observation-tape", learning_root=root,
        expected_genesis=parent["wallet_tape_upgrade"]["genesis_sha256"], now_ms=now)
    fixed = parent["wallet_review_state"].get("fixed_study_state")
    _require(type(fixed) is dict and fixed.get("manifest_sha256") == stored["engine_state"]["manifest_sha256"]
             and fixed.get("selections") == stored["engine_state"]["fixed_state"]["selections"], "fixed_anchor_conflict")
    declaration = {"version": VERSION, "prepared_ms": now, "state_dir": str(root), "db_path": str(db),
                   "source_versions": source_versions(), "genesis_sha256": stored["genesis_sha256"],
                   "fixed_manifest_sha256": stored["engine_state"]["manifest_sha256"], "policy": dict(POLICY)}
    raw = _encoded(declaration) + b"\n"
    pointer = {"version": VERSION, "declaration_sha256": hashlib.sha256(raw).hexdigest(),
               "prepared_ms": now, "genesis_sha256": declaration["genesis_sha256"], "funding_allowed": False}
    return {"declaration": declaration, "declaration_raw": raw, "pointer": pointer}


class WalletMaintenance:
    def __init__(self, state_dir: Path | str, db_path: Path | str, *, clock=time.time,
                 reader_factory=GuardedReader):
        self.root, self.db = _safe(Path(state_dir)), _safe(Path(db_path))
        _require(self.root.is_dir() and not self.db.is_relative_to(self.root), "invalid_roots")
        self.own = _safe(self.root / DIRECTORY)
        self.clock, self.reader_factory = clock, reader_factory
        self.lock_path = self.root / "worker.lock"
        self.parent_path = self.root / "state.json"
        self.journal_path, self.status_path = self.own / "journal.json", self.own / "status.json"
        self._lock_raw: bytes | None = None
        self._lock_identity: tuple[int, int] | None = None

    def _now(self) -> int:
        value = self.clock()
        _require(type(value) in (int, float), "invalid_clock")
        try:
            return _integer(int(value * 1000), 1)
        except (ValueError, OverflowError) as exc:
            raise ValueError("wallet_maintenance:invalid_clock") from exc

    def _lock_matches(self) -> bool:
        if self._lock_raw is None or self._lock_identity is None:
            return False
        try:
            _safe(self.lock_path)
            with self.lock_path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(len(self._lock_raw) + 1)
                after = os.fstat(stream.fileno())
            current = self.lock_path.stat(follow_symlinks=False)
            return raw == self._lock_raw and all(
                (stat.st_dev, stat.st_ino) == self._lock_identity and stat.st_nlink == 1
                for stat in (before, after, current))
        except (OSError, ValueError):
            return False

    def _owns_lock(self) -> None:
        _require(self._lock_matches(), "worker_lock_changed")

    def _validate_install(self, declaration_raw: bytes, declaration: dict, now: int) -> None:
        raw, intent = _read(self.own / "install-intent.json", 128 * 1024)
        _, completed = _read(self.own / "install-complete.json", 128 * 1024)
        intent_keys = {"version", "stage_report_sha256", "parent_before_sha256", "prepared_ms", "context_snapshot",
                       "worker_sources", "declaration_sha256", "genesis_sha256", "funding_allowed",
                       "canonical_queries", "services_changed"}
        completion_keys = {"status", "completed_ms", "parent_after_sha256", "active_leases", "retired_leases",
                           "family_size", "install_intent_sha256"}
        _require(set(intent) == intent_keys and set(completed) == intent_keys | completion_keys,
                 "installation_receipt_shape")
        _require(_sha({key: completed[key] for key in intent_keys}) == _sha(intent), "installation_receipt_base_conflict")
        _require(intent["version"] == INSTALL_VERSION and completed["status"] == "prepared"
                 and _integer(intent["prepared_ms"], 1) == declaration["prepared_ms"]
                 and intent["prepared_ms"] <= _integer(completed["completed_ms"], 1) <= now,
                 "installation_receipt_clock_or_version")
        _require(all(_digest(intent[key]) for key in ("stage_report_sha256", "parent_before_sha256", "context_snapshot",
                                                       "declaration_sha256", "genesis_sha256"))
                 and _digest(completed["parent_after_sha256"])
                 and completed["install_intent_sha256"] == hashlib.sha256(raw).hexdigest(),
                 "installation_receipt_digest")
        _require(intent["declaration_sha256"] == hashlib.sha256(declaration_raw).hexdigest()
                 and intent["genesis_sha256"] == declaration["genesis_sha256"]
                 and _sha(intent["worker_sources"]) == _sha(declaration["source_versions"]),
                 "installation_receipt_declaration_binding")
        _require(intent["funding_allowed"] is False and intent["services_changed"] is False
                 and type(intent["canonical_queries"]) is int and intent["canonical_queries"] == 0
                 and type(completed["active_leases"]) is int and completed["active_leases"] == 12
                 and type(completed["retired_leases"]) is int and completed["retired_leases"] == 10
                 and type(completed["family_size"]) is int and completed["family_size"] == 22,
                 "installation_receipt_flags_or_population")

    def _validate(self, parent: dict, now: int) -> dict:
        _validate_parent(parent, now)
        raw, declaration = _read(self.own / "declaration.json", 128 * 1024)
        pointer = parent.get(POINTER)
        expected = {"version": VERSION, "declaration_sha256": hashlib.sha256(raw).hexdigest(),
                    "prepared_ms": declaration.get("prepared_ms"),
                    "genesis_sha256": declaration.get("genesis_sha256"), "funding_allowed": False}
        _require(type(pointer) is dict and _sha(pointer) == _sha(expected), "declaration_binding")
        _require(set(declaration) == {"version", "prepared_ms", "state_dir", "db_path", "source_versions",
                                      "genesis_sha256", "fixed_manifest_sha256", "policy"}, "declaration_shape")
        _require(declaration["version"] == VERSION and _integer(declaration["prepared_ms"], 1) <= now
                 and declaration["state_dir"] == str(self.root) and declaration["db_path"] == str(self.db)
                 and declaration["source_versions"] == source_versions()
                 and _sha(declaration["policy"]) == _sha(POLICY), "declaration_source_roots_or_policy")
        _require(declaration["genesis_sha256"] == parent["wallet_tape_upgrade"]["genesis_sha256"], "genesis_changed")
        fixed = parent["wallet_review_state"].get("fixed_study_state", {})
        _require(type(fixed) is dict and fixed.get("manifest_sha256") == declaration["fixed_manifest_sha256"],
                 "fixed_manifest_changed")
        self._validate_install(raw, declaration, now)
        return declaration

    def _journal(self, value: dict) -> None:
        self._owns_lock()
        value = copy.deepcopy(value)
        value.pop("journal_sha256", None)
        value["journal_sha256"] = _sha(value)
        _atomic(self.journal_path, value, MAX_JOURNAL_BYTES)

    def _tape(self, parent: dict, declaration: dict, now: int) -> dict:
        stored = tape_io.validate_store(store_dir=self.root / "wallet-observation-tape", learning_root=self.root,
            expected_genesis=declaration["genesis_sha256"], now_ms=now)
        fixed = parent["wallet_review_state"].get("fixed_study_state", {})
        _require(fixed.get("manifest") == stored["engine_state"]["fixed_state"]["manifest"]
                 and fixed.get("selections") == stored["engine_state"]["fixed_state"]["selections"],
                 "fixed_anchor_conflict")
        return stored

    def _status(self, status: str, now: int, **fields) -> dict:
        result = {"version": VERSION, "status": status, "at_ms": now,
                  "source_queries": 0, "provider_calls": 0, "paper_operations": 0,
                  "funding_allowed": False, **fields}
        _atomic(self.status_path, result, 16 * 1024)
        return result

    def _publish(self, journal: dict, now: int) -> dict:
        self._owns_lock()
        raw, parent = _read(self.parent_path, MAX_PARENT_BYTES)
        declaration = self._validate(parent, now)
        base, updated = journal["base_wallet"], journal["result_wallet"]
        current = _wallet(parent)
        if current not in (base, updated):
            _require(parent.get("last_wallet_review_ms", 0) > base.get("last_wallet_review_ms", 0),
                     "parent_wallet_CAS_conflict")
            self._journal({**journal, "status": "superseded", "resolved_ms": now,
                           "owner_wallet_ms": parent["last_wallet_review_ms"], "owner_wallet_sha256": _sha(current)})
            return self._status("superseded", now, reason="original_wallet_owner_advanced")
        stored = self._tape(parent, declaration, now)
        _require(stored["store_sha256"] == journal["result_tape_store_sha256"], "result_tape_frontier_changed")
        latest = self.root / "latest-wallet-review.json"
        if latest.exists():
            _, existing = _read(latest, MAX_PARENT_BYTES)
            _require(_integer(existing.get("observed_ms", 0)) <= journal["started_ms"], "newer_review_publication_conflict")
        if current == base:
            changed = {**parent, **copy.deepcopy(updated)}
            _require({k: v for k, v in parent.items() if k not in WALLET_KEYS}
                     == {k: v for k, v in changed.items() if k not in WALLET_KEYS}, "paper_or_other_parent_mutation")
            self._owns_lock()
            latest_raw, _ = _read(self.parent_path, MAX_PARENT_BYTES)
            _require(latest_raw == raw, "parent_CAS_conflict")
            _atomic(self.parent_path, changed, MAX_PARENT_BYTES)
        self._owns_lock()
        _, current_parent = _read(self.parent_path, MAX_PARENT_BYTES)
        _require(_wallet(current_parent) == updated, "parent_changed_before_review_publication")
        _atomic(latest, journal["review"], MAX_PARENT_BYTES)
        self._journal({**journal, "status": "published", "resolved_ms": now})
        return self._status("published", now, attempt_id=journal["attempt_id"],
                            observed_ms=journal["started_ms"], recovered=journal.get("recovered", False),
                            source_queries=0 if journal.get("recovered") else journal["original_source_queries"])

    def once(self) -> dict:
        now = self._now()
        _safe(self.lock_path)
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # No PID probe or stale-lock recovery: an operator owns that decision.
            return {"version": VERSION, "status": "locked", "source_queries": 0,
                    "provider_calls": 0, "paper_operations": 0, "funding_allowed": False}
        self._lock_raw = _encoded({"pid": os.getpid(), "started_ms": now, "owner": VERSION})
        created = os.fstat(descriptor)
        self._lock_identity = (created.st_dev, created.st_ino)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self._lock_raw)
                stream.flush()
                os.fsync(stream.fileno())
            self._owns_lock()
            return self._once(now)
        finally:
            if self._lock_matches():
                self.lock_path.unlink()
            self._lock_raw = None
            self._lock_identity = None

    def _once(self, now: int) -> dict:
        _, parent = _read(self.parent_path, MAX_PARENT_BYTES)
        declaration = self._validate(parent, now)
        declaration_hash = _sha(declaration)
        if self.journal_path.exists():
            _, prior = _read(self.journal_path, MAX_JOURNAL_BYTES)
            _require(prior.get("journal_sha256") == _sha({k: v for k, v in prior.items() if k != "journal_sha256"})
                     and prior.get("version") == VERSION and prior.get("declaration_hash") == declaration_hash
                     and _integer(prior.get("started_ms"), 1) <= now, "journal_invalid_or_conflicting")
            if prior.get("status") == "result":
                return self._publish({**prior, "recovered": True}, now)
            if prior.get("status") == "pending":
                if _wallet(parent) != prior["base_wallet"]:
                    _require(parent.get("last_wallet_review_ms", 0) > prior["base_wallet"].get("last_wallet_review_ms", 0),
                             "pending_parent_wallet_conflict")
                    self._journal({**prior, "status": "superseded", "resolved_ms": now,
                                   "owner_wallet_ms": parent["last_wallet_review_ms"],
                                   "owner_wallet_sha256": _sha(_wallet(parent))})
                    return self._status("superseded", now, reason="original_wallet_owner_advanced")
                return self._status("recovery_needed", now, attempt_id=prior["attempt_id"], reason="pending_without_durable_result")
            _require(prior.get("status") in {"published", "superseded"}, "journal_status_invalid")
            if prior["status"] == "published":
                frontier, digest = prior["result_wallet"]["last_wallet_review_ms"], _sha(prior["result_wallet"])
            else:
                frontier, digest = prior.get("owner_wallet_ms"), prior.get("owner_wallet_sha256")
            _require(parent["last_wallet_review_ms"] >= _integer(frontier, 1)
                     and (parent["last_wallet_review_ms"] > frontier or _sha(_wallet(parent)) == digest),
                     "published_wallet_frontier_regressed_or_changed")
        if self.status_path.exists():
            _, status = _read(self.status_path, 16 * 1024)
            _require(_integer(status.get("at_ms"), 1) <= now, "status_clock_regressed")
            if now < _integer(status.get("retry_after_ms", 0)):
                return {**status, "status": "deferred", "source_queries": 0}
        if now - parent.get("last_wallet_review_ms", 0) < REVIEW_INTERVAL_MS:
            return self._status("not_due", now)
        wal = Path(str(self.db) + "-wal")
        if wal.exists() and wal.stat().st_size > WAL_LIMIT:
            return self._status("deferred", now, reason="wal_over_limit", retry_after_ms=now + QUIET_MS)
        reader = self.reader_factory(self.db)
        try:
            reader.guard()
        except SourceDeferred as exc:
            if "wal_over_limit" not in str(exc):
                raise
            return self._status("deferred", now, reason="wal_over_limit", retry_after_ms=now + QUIET_MS)
        self._tape(parent, declaration, now)
        base = _wallet(parent)
        journal = {"version": VERSION, "declaration_hash": declaration_hash, "started_ms": now,
                   "attempt_id": _sha([declaration_hash, now, base]), "base_wallet": base, "status": "pending"}
        self._journal(journal)  # Durable before the original review can debit/read/write.
        try:
            result = tape_io.run_review_with_tape(wallets.run_wallet_review, reader,
                self.db.parent / "claude-runners", now, parent["wallet_review_state"],
                store_dir=self.root / "wallet-observation-tape", learning_root=self.root,
                expected_genesis=declaration["genesis_sha256"], clock=self.clock)
            review = copy.deepcopy(result["review"])
            selected = {(r["chain"], r["address"]) for r in review.get("observation_queue", [])}
            review["candidates"] = [r for r in review.get("candidates", []) if (r["chain"], r["address"]) in selected]
            review["candidate_details_scope"] = "selected_observation_queue_only"
            review["intake"] = result["intake"]
            _require(review.get("observed_ms") == now, "original_review_clock_changed")
            journal.update(status="result", review=review,
                           result_wallet={"wallet_review_state": result["next_state"], "last_wallet_review_ms": now},
                           result_tape_store_sha256=result["wallet_observation_tape"]["store_sha256"],
                           original_source_queries=reader.queries)
            self._journal(journal)  # Result before either public artifact is changed.
        except Exception as exc:
            fields = {"reason": "original_review_failed_before_durable_result", "failure_type": type(exc).__name__}
            if isinstance(exc, SourceDeferred) and "wal_over_limit" in str(exc):
                fields["retry_after_ms"] = max(now, self._now()) + QUIET_MS
            return self._status("recovery_needed", max(now, self._now()),
                                source_queries=reader.queries, **fields)
        return self._publish(journal, max(now, self._now()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--db-path", type=Path)
    args = parser.parse_args(argv)
    state_dir, db_path = args.state_dir, args.db_path
    if state_dir is None or db_path is None:
        from kaiba.core.config import get_settings
        settings = get_settings()
        state_dir = state_dir or settings.kaiba_data_dir / "learning/autolearn"
        db_path = db_path or settings.db_path
    worker = WalletMaintenance(state_dir, db_path)
    result = worker.once()
    print(json.dumps({k: result[k] for k in ("version", "status", "source_queries", "provider_calls", "funding_allowed")}))
    return 0 if result["status"] in {"published", "not_due", "deferred", "locked", "superseded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
