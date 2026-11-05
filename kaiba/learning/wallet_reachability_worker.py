"""Publish retained-wallet reachability beside the unchanged maintenance result.

The inherited worker lock covers exactly one original maintenance invocation and
the file-only projection. Original pending work is never retried or repaired here.
An interrupted capture may support historical analysis, never a fresh-review or
copy-profit claim. This wrapper changes no original source declaration or state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from pathlib import Path

from kaiba.learning import wallet_maintenance as original
from kaiba.learning import wallet_observation_io as tape_io
from kaiba.learning import wallet_reachability as projector

VERSION = "wallet-reachability-worker/1"
DIRECTORY = "wallet-reachability"
MAX_REPORT_BYTES = 1024 * 1024
POLICY = {"extra_source_queries": 0, "provider_calls": 0, "parent_writes": False,
    "original_invocations": 1, "retry_pending": False, "lock_reclaim": False,
    "capture_clock": "first_retained_opening_raw_capture_ms",
    "canonical_arrival_known": False, "copy_profit_qualified": False,
    "funding_allowed": False}


def require(condition, code):
    if not condition:
        raise ValueError("wallet_reachability:" + code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def sources():
    root = Path(__file__).resolve().parents[1]
    return {**original.source_versions(), **{name: digest((root / name).read_bytes()) for name in
        ("learning/wallet_reachability.py", "learning/wallet_reachability_worker.py")}}


def read(path, cap):
    path = original._safe(Path(path))
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "regular_single_link_required")
    if hasattr(os, "getuid"):
        require(before.st_uid == os.getuid(), "foreign_owner")
    require(before.st_size <= cap, "input_cap")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        raw = handle.read(cap + 1)
        after = os.fstat(handle.fileno())
    current = path.lstat()
    def key(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
    require(len(raw) <= cap and all(key(s) == key(before) for s in (opened, after, current)), "input_changed")
    # Windows path stat exposes birth time while fstat exposes change time.
    require(before.st_ctime_ns == current.st_ctime_ns and opened.st_ctime_ns == after.st_ctime_ns
        and (os.name == "nt" or before.st_ctime_ns == opened.st_ctime_ns), "input_changed")
    value = json.loads(raw, object_pairs_hook=original._object)
    require(type(value) is dict, "object_required")
    original._encoded(value)
    return raw, value


def sync(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, value, *, exclusive=False, precommit=None):
    """Complete owned bytes, fixed exclusive stage, and no foreign-link cleanup."""
    original._safe(path)
    raw = original._encoded(value) + b"\n"
    require(len(raw) <= MAX_REPORT_BYTES, "output_cap")
    temporary = path.with_name(path.name + ".partial")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    held = os.dup(fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if precommit is not None:
            precommit()  # Fresh input/owner/prior checks after the potentially slow fsync.
        stage_info, pinned = temporary.lstat(), os.fstat(held)
        require((stage_info.st_dev, stage_info.st_ino) == (pinned.st_dev, pinned.st_ino)
            and stage_info.st_nlink == pinned.st_nlink == 1
            and read(temporary, MAX_REPORT_BYTES)[0] == raw, "publication_stage_changed")
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
            sync(path.parent)
            # Directory fsync can block. Neither name may have been replaced or
            # given another link before cleanup of our exact staged inode.
            staged, destination, pinned = temporary.lstat(), path.lstat(), os.fstat(held)
            def linked_key(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
            require(linked_key(staged) == linked_key(destination) == linked_key(pinned)
                and pinned.st_nlink == 2 and pinned.st_size == stage_info.st_size
                and pinned.st_mtime_ns == stage_info.st_mtime_ns
                and os.pread(held, len(raw) + 1, 0) == raw
                and linked_key(os.fstat(held)) == linked_key(pinned), "exclusive_publication_links_changed")
            temporary.unlink()
        else:
            if os.path.lexists(path):
                read(path, MAX_REPORT_BYTES)
            os.replace(temporary, path)
        sync(path.parent)
        require(read(path, MAX_REPORT_BYTES)[0] == raw, "publication_unproved")
    finally:
        os.close(held)


def prepare_declaration(state_dir, db_path, *, prepared_ms, at_ms=None):
    root, db = original._safe(Path(state_dir)), original._safe(Path(db_path))
    worker = original.WalletMaintenance(root, db)
    _, parent = read(root / "state.json", original.MAX_PARENT_BYTES)
    observed = prepared_ms if at_ms is None else at_ms
    require(prepared_ms <= observed, "declaration_clock_invalid")
    declaration = worker._validate(parent, observed)
    worker._tape(parent, declaration, observed)
    old_raw, _ = read(root / original.DIRECTORY / "declaration.json", 128 * 1024)
    complete_raw, _ = read(root / original.DIRECTORY / "install-complete.json", 128 * 1024)
    return {"version": VERSION, "prepared_ms": original._integer(prepared_ms, 1),
        "state_dir": str(root), "db_path": str(db), "sources": sources(), "policy": POLICY,
        "original_declaration_sha256": digest(old_raw), "original_completion_sha256": digest(complete_raw),
        "genesis_sha256": declaration["genesis_sha256"],
        "fixed_manifest_sha256": declaration["fixed_manifest_sha256"]}


class ReachabilityMaintenance(original.WalletMaintenance):
    def __init__(self, *args, **kwargs):
        require(os.name == "posix", "posix_publication_required")
        super().__init__(*args, **kwargs)
        self.reachability_root = original._safe(self.root / DIRECTORY)

    def _manifest(self, now):
        own = self.reachability_root
        raw, declaration = read(own / "declaration.json", 128 * 1024)
        intent_raw, intent = read(own / "install-intent.json", 128 * 1024)
        _, complete = read(own / "install-complete.json", 128 * 1024)
        require(set(intent) == {"version", "declaration_sha256", "stage_report_sha256", "source_commit", "funding_allowed"}
            and intent["version"] == VERSION and intent["funding_allowed"] is False
            and original.SHA256.fullmatch(intent["stage_report_sha256"])
            and type(intent["source_commit"]) is str and len(intent["source_commit"]) == 40
            and all(c in "0123456789abcdef" for c in intent["source_commit"]), "install_intent_invalid")
        require(original._sha(complete) == original._sha({"version": VERSION, "status": "prepared", "intent_sha256": digest(intent_raw),
            "declaration_sha256": digest(raw), "funding_allowed": False})
            and intent["declaration_sha256"] == digest(raw), "install_completion_invalid")
        require(original._sha(declaration) == original._sha(prepare_declaration(self.root, self.db,
            prepared_ms=declaration["prepared_ms"], at_ms=now))
            and declaration["prepared_ms"] <= now, "own_declaration_changed")
        return raw, declaration

    def _snapshot(self, now, declaration):
        names = {"state.json": original.MAX_PARENT_BYTES,
            "wallet-maintenance/declaration.json": 128 * 1024,
            "wallet-maintenance/install-complete.json": 128 * 1024,
            "wallet-maintenance/journal.json": original.MAX_JOURNAL_BYTES,
            "wallet-maintenance/status.json": 16 * 1024,
            "latest-wallet-review.json": original.MAX_PARENT_BYTES,
            "wallet-observation-tape/store.json": tape_io.MAX_STORE_BYTES,
            "wallet-observation-tape/genesis.json": 128 * 1024,
            "wallet-observation-tape/report.json": tape_io.MAX_REPORT_BYTES}
        captured = {}
        for name, cap in names.items():
            path = self.root / name
            if not os.path.lexists(path):
                require(name in {"wallet-maintenance/journal.json", "wallet-maintenance/status.json",
                    "latest-wallet-review.json", "wallet-observation-tape/report.json"}, "required_input_missing")
                captured[name] = (None, None)
            else:
                captured[name] = read(path, cap)
        parent = captured["state.json"][1]
        old = self._validate(parent, now)
        require(old["genesis_sha256"] == declaration["genesis_sha256"]
            and old["fixed_manifest_sha256"] == declaration["fixed_manifest_sha256"], "original_anchor_changed")
        store = self._tape(parent, old, now)
        require(tape_io._sha(store) == tape_io._sha(captured["wallet-observation-tape/store.json"][1]), "store_changed")
        journal = captured["wallet-maintenance/journal.json"][1]
        review = captured["latest-wallet-review.json"][1]
        if journal is not None:
            require(journal.get("journal_sha256") == original._sha({k: v for k, v in journal.items() if k != "journal_sha256"})
                and journal.get("version") == original.VERSION
                and journal.get("status") in {"pending", "result", "published", "superseded"}
                and 0 < journal["started_ms"] <= now, "original_journal_unproved")
        if review is not None:
            require(type(review.get("observed_ms")) is int
                and 0 < review["observed_ms"] <= parent["last_wallet_review_ms"] <= now, "published_review_clock_unproved")
        return captured, parent, store

    def _project(self, original_result, now):
        declaration_raw, declaration = self._manifest(now)
        captured, parent, store = self._snapshot(now, declaration)
        self._owns_lock()
        diagnostics = tape_io._Capture(None, self.root / "wallet-observation-tape", store, self.clock, now).unresolved()
        diagnostics += [{"reason": "unresolved_source_admission", "wallet": a["wallet"]}
            for a in store["quota"]["attempts"] if a["status"] != "observed"]
        projection = projector.project(store["engine_state"], now_ms=now, diagnostics=diagnostics)
        journal = captured["wallet-maintenance/journal.json"][1]
        review = captured["latest-wallet-review.json"][1]
        references = {name: digest(raw) if raw is not None else None for name, (raw, _) in captured.items()
            if name not in {"state.json", "wallet-maintenance/status.json"}}
        references["parent_wallet_sha256"] = original._sha(original._wallet(parent))
        report_id = original._sha({"manifest": digest(declaration_raw), "inputs": references,
            "original_status": original_result["status"]})
        path = self.reachability_root / "report.json"
        prior_identity = path.lstat() if os.path.lexists(path) else None
        prior_raw, prior = read(path, MAX_REPORT_BYTES) if prior_identity is not None else (None, None)
        def report_identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
        if prior_identity is not None:
            require(report_identity(path.lstat()) == report_identity(prior_identity), "prior_report_changed")
        if prior is not None:
            require(prior.get("version") == VERSION and prior.get("manifest_sha256") == digest(declaration_raw)
                and prior.get("report_sha256") == original._sha({k: v for k, v in prior.items() if k != "report_sha256"})
                and prior.get("store_updated_ms", 0) <= store["updated_ms"], "prior_report_unproved")
        interrupted = original_result["status"] == "recovery_needed" or journal is not None and journal["status"] in {"pending", "result"}
        linked_review = (journal is not None and journal["status"] == "published" and review is not None
            and original._sha(journal.get("result_wallet")) == original._sha(original._wallet(parent))
            and journal.get("result_tape_store_sha256") == store["store_sha256"]
            and original._sha(journal.get("review")) == original._sha(review)
            and review.get("observed_ms") == journal["started_ms"])
        report = {"version": VERSION, "report_id": report_id, "manifest_sha256": digest(declaration_raw),
            "analysis_at_ms": now, "store_updated_ms": store["updated_ms"], "input_sha256": references,
            "genesis_sha256": declaration["genesis_sha256"], "store_sha256": store["store_sha256"],
            "original_result": {k: original_result[k] for k in
                ("status", "source_queries", "attempt_id", "reason", "observed_ms", "recovered") if k in original_result},
            "original_journal_status": journal["status"] if journal else None,
            "original_attempt_started_ms": journal["started_ms"] if journal else None,
            "original_published_review_ms": review["observed_ms"] if review else None,
            "parent_wallet_review_ms": parent["last_wallet_review_ms"],
            "original_refresh_complete": original_result["status"] == "published"
                and original_result.get("recovered") is False and not interrupted and linked_review,
            "original_retained_review_complete": linked_review,
            "observation_scope": "historical_interrupted_acquisition" if interrupted
                else "retained_publication_recovery" if original_result.get("recovered") is True and linked_review
                else "retained_snapshot" if linked_review else "retained_unlinked_snapshot",
            "prior_interrupted_attempt_queries": None if interrupted else "see_original_journal",
            "capture_triggered_by_projection": False, "extra_source_queries": 0, "provider_calls": 0,
            "copy_profit_qualified": False, "funding_allowed": False, "projection": projection}
        report["report_sha256"] = original._sha(report)
        require(len(original._encoded(report)) <= MAX_REPORT_BYTES, "output_cap")
        def precommit():
            self._owns_lock()
            require(self._manifest(now)[0] == declaration_raw, "manifest_changed_during_projection")
            for name, (raw, _) in captured.items():
                path_input = self.root / name
                require((read(path_input, max(tape_io.MAX_STORE_BYTES, original.MAX_JOURNAL_BYTES))[0]
                    if os.path.lexists(path_input) else None) == raw, "input_changed_during_projection")
            if prior_identity is None:
                require(not os.path.lexists(path), "prior_report_appeared")
            else:
                require(report_identity(path.lstat()) == report_identity(prior_identity)
                    and read(path, MAX_REPORT_BYTES)[0] == prior_raw, "prior_report_changed")
            self._owns_lock()
        precommit()
        if prior is None or prior["report_id"] != report_id:
            publish(path, report, precommit=precommit)
        return {"status": "published" if prior is None or prior["report_id"] != report_id else "unchanged",
            "report_id": report_id, "observation_scope": report["observation_scope"],
            "data_updated_ms": store["updated_ms"], "extra_source_queries": 0}

    def _once(self, now):
        self._manifest(now)  # A missing/changed declaration refuses before original work.
        result = super()._once(now)
        try:
            projection = self._project(result, self._now())
        except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
            # No original call, debit, replay or success-status rewrite on this path.
            projection = {"status": "unavailable", "reason": "projection_unproved", "failure_type": type(exc).__name__,
                "extra_source_queries": 0}
        return {**result, "reachability": projection}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        worker = ReachabilityMaintenance(args.state_dir, args.db_path)
        result = worker.once()
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"version": VERSION, "status": "refused", "reason": "source_or_installation_unproved", "funding_allowed": False}))
        return 1
    print(json.dumps({k: result[k] for k in ("version", "status", "source_queries", "provider_calls", "funding_allowed", "reachability") if k in result}))
    return 0 if result["status"] in {"published", "not_due", "deferred", "locked", "superseded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
