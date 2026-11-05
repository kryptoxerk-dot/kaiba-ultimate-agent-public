"""Standalone partial PAPER lease allocation; no worker integration or activation.

Preparation reads existing research books and returns an immutable receipt. An
independent integrating owner must durably commit the authority pointer under the
parent worker lock. This module never writes that pointer, retires a live position,
changes a frozen manifest, or authorizes funding. Archived arms remain in the
lifetime testing family after their execution leases are released.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import os
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from kaiba.learning import admission_paper as ap
from kaiba.learning import candidate_paper as cp
from kaiba.learning import strategy_campaign as sc

VERSION = "research-allocation/1"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_NEW_ARMS = 10


def _joint_policy(family_size):
    return {"version": "joint-lifetime-family/1", "method": "BH", "fdr_q": sc.FDR_Q,
            "family_size": family_size, "ready_days": sc.READY_DAYS,
            "correction_scope": "all_legacy_and_new_declared_arms",
            "local_corrections": "diagnostic_only",
            "raw_p_policy": "frozen_raw_including_pending;invalid_unknown_stale_or_changed_prefix=1",
            "inventory_policy": "merge_actual_active_books_and_immutable_retired_rows",
            "legacy_timing_qualified_for_admission": False}


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _require(condition, reason):
    if not condition:
        raise ValueError("research_allocation:" + reason)


def _int(value, minimum=0):
    _require(type(value) is int and value >= minimum, "integer_clock_or_count_required")
    return value


def _key(campaign, hypothesis):
    return campaign + ":" + hypothesis


def source_versions():
    return {**sc.source_versions(), "learning/admission_paper.py": hashlib.sha256(Path(ap.__file__).read_bytes()).hexdigest(),
            "learning/research_allocation.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _path(path, learning_root):
    path, root = Path(path), Path(learning_root).resolve()
    _require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_refused")
    _require(path.resolve().is_relative_to(root), "outside_owned_learning_root")
    return path.resolve()


def _load(path, cap=MAX_RECEIPT_BYTES):
    _require(path.is_file() and not path.is_symlink(), "missing_owned_artifact:" + path.name)
    with path.open("rb") as handle:
        raw = handle.read(cap + 1)
    _require(len(raw) <= cap, "owned_artifact_byte_cap")
    value = json.loads(raw)
    _require(isinstance(value, dict), "owned_artifact_shape")
    return raw, value


def _immutable(path, value):
    raw = _encoded(value) + b"\n"
    _require(len(raw) <= MAX_RECEIPT_BYTES and not path.is_symlink(), "immutable_artifact_cap_or_symlink")
    if path.exists():
        _require(path.read_bytes() == raw, "immutable_preparation_conflict")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    _require(not temporary.is_symlink(), "immutable_temporary_symlink")
    if temporary.exists():
        _require(temporary.read_bytes() == raw, "immutable_partial_conflict")
    else:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    _require(not path.exists(), "immutable_target_changed")
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _worker_lock(paper_dir, now):
    _, lock = _load(paper_dir.parent / "worker.lock", 4096)
    _require(lock.get("pid") == os.getpid() and 0 < _int(lock.get("started_ms"), 1) <= now, "current_parent_worker_lock_required")


def _artifact(path):
    _require(not path.is_symlink(), "archive_symlink")
    if not path.exists():
        return {"file": path.name, "bytes": 0, "sha256": None}
    _require(path.is_file() and path.stat().st_size <= MAX_FILE_BYTES, "archive_file_cap")
    before, began, count, digest = path.stat(), time.monotonic(), 0, hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            count += len(chunk)
            _require(count <= MAX_FILE_BYTES and time.monotonic() - began <= 3, "archive_hash_deadline_or_cap")
            digest.update(chunk)
    after = path.stat()
    _require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "archive_changed_during_hash")
    return {"file": path.name, "bytes": count, "sha256": digest.hexdigest()}


def _manifest(manifest, now, *, admission=False, policy=None):
    _require(isinstance(manifest, Mapping) and len(_encoded(manifest)) <= MAX_RECEIPT_BYTES, "full_manifest_required")
    if admission:
        ap.validate_manifest(manifest, now_ms=now, expected_policy=policy)
    else:
        _require(manifest.get("source_versions") == sc.source_versions()
                 and manifest.get("version") == sc.CAMPAIGN_VERSION
                 and manifest.get("campaign_id") == sc._campaign_id(manifest)
                 and manifest.get("status") == "FROZEN_PAPER_ONLY"
                 and manifest.get("funding_allowed") is False
                 and ap.POLICY_KEY not in manifest.get("assessment_policy", {}), "legacy_full_manifest_identity")
    _require(0 < _int(manifest["selected_ms"], 1) <= now
             and isinstance(manifest.get("hypotheses"), list) and 1 <= len(manifest["hypotheses"]) <= 12,
             "manifest_clock_or_arm_cap")
    seen = set()
    for arm in manifest["hypotheses"]:
        _require(arm.get("status") == "PAPER_ONLY" and arm.get("spec_hash") == sc._hash(arm["spec"])
                 and arm.get("hypothesis_id") == "ph_" + arm["spec_hash"][:24]
                 and arm["hypothesis_id"] not in seen, "frozen_arm_identity")
        if not admission:
            _require(ap.POLICY_KEY not in arm["spec"], "mixed_legacy_execution_policy")
        seen.add(arm["hypothesis_id"])


def _runner(paper_dir, learning_root, policy, clock):
    return cp.CandidatePaperRunner(paper_dir, learning_root=learning_root,
        max_stake_usd=policy["max_stake_usd"], max_open_per_arm=policy["max_open_per_arm"],
        clock=clock if policy["processing_clock_policy"] == "observed_wall_clock/1" else None)


def _ledger_complete(conn, now):
    """Bounded reverse joins: real inventory must not disappear behind an entry join."""
    fields = {"candidate_entries": "signal_id,position_id,decision_id",
        "orders": "order_id,decision_id,mode,provider,side,state,chain,token,created_ms,amount_in,filled_out",
        "positions": "position_id,mode,chain,token,qty,qty_total,opened_ms,closed_ms",
        "position_orders": "position_id,order_id,side", "trades": "trade_id,position_id,closed_ms",
        "candidate_protection": "position_id"}
    tables = {name: conn.execute("SELECT " + columns + " FROM " + name + " LIMIT 1001").fetchall()
              for name, columns in fields.items()}
    _require(all(len(rows) <= 1000 for rows in tables.values()), "allocated_ledger_row_cap_unknown")
    entries, orders, positions, links, trades, protection = (tables[name] for name in fields)
    ps = {r["position_id"]: r for r in positions}
    os_by_id = {r["order_id"]: r for r in orders}
    entry_by_position, link_by_order = {}, {}
    for entry in entries:
        pid = entry["position_id"]
        if pid is not None:
            _require(pid in ps and pid not in entry_by_position, "allocated_entry_position_join_unknown")
            entry_by_position[pid] = entry
    for link in links:
        _require(link["position_id"] in ps and link["order_id"] in os_by_id
                 and link["order_id"] not in link_by_order, "allocated_order_position_join_unknown")
        link_by_order[link["order_id"]] = link
    for order in orders:
        _require(order["mode"] == "shadow" and order["provider"] == "paper"
                 and order["side"] in ("buy", "sell") and order["state"] in ("filled", "failed", "expired", "cancelled")
                 and 0 < ap._int(order["created_ms"]) <= now, "allocated_order_unknown")
        link = link_by_order.get(order["order_id"])
        if order["state"] == "filled":
            _require(link is not None, "allocated_filled_order_join_unknown")
        if link is not None:
            position = ps[link["position_id"]]
            _require(link["side"] == order["side"] and position["chain"] == order["chain"]
                     and position["token"] == order["token"], "allocated_order_inventory_mismatch")
    protected = {r["position_id"] for r in protection}
    _require(protected == set(ps), "allocated_protection_inventory_join_unknown")
    for pid, position in ps.items():
        _require(pid in entry_by_position and position["mode"] == "shadow"
                 and 0 < ap._int(position["opened_ms"]) <= now, "allocated_inventory_entry_join_unknown")
        matched = [os_by_id[link["order_id"]] for link in links if link["position_id"] == pid
                   and os_by_id[link["order_id"]]["state"] == "filled"]
        buys, sells = ([r for r in matched if r["side"] == side] for side in ("buy", "sell"))
        _require(buys and all(r["decision_id"] == entry_by_position[pid]["decision_id"] for r in buys),
                 "allocated_buy_entry_join_unknown")
        bought, sold = sum(ap._int(r["filled_out"]) for r in buys), sum(ap._int(r["amount_in"]) for r in sells)
        _require(bought > 0 and all(ap._int(r["filled_out"]) > 0 for r in buys)
                 and all(ap._int(r["amount_in"]) > 0 for r in sells)
                 and bought == ap._int(position["qty_total"]) and bought - sold == ap._int(position["qty"]),
                 "allocated_inventory_quantity_unknown")
        closed = position["closed_ms"]
        if closed is not None:
            _require(ap._int(position["qty"]) == 0 and position["opened_ms"] <= closed <= now
                     and any(r["created_ms"] == closed for r in sells)
                     and any(t["position_id"] == pid and t["closed_ms"] == closed for t in trades),
                     "allocated_closed_inventory_unknown")
        else:
            _require(ap._int(position["qty"]) > 0 and not any(t["position_id"] == pid for t in trades),
                     "allocated_open_inventory_unknown")
    _require(all(t["position_id"] in ps and ps[t["position_id"]]["closed_ms"] == t["closed_ms"] for t in trades),
             "allocated_trade_inventory_join_unknown")


def _read_ledger_guard(runner, identity, now):
    conn = runner._existing(identity)
    began = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - began >= 3), 1000)
    try:
        _ledger_complete(conn, now)
    finally:
        conn.close()


def prepare_receipt(legacy_manifests: Sequence[Mapping], new_manifest: Mapping, legacy_assessments: Mapping, *,
                    paper_dir, learning_root, now_ms, max_active_arms=12,
                    max_stake_usd="30", max_open_per_arm=3):
    """Read-only preparation. No artifact, authority or activation is written."""
    now = _int(now_ms, 1)
    _require(type(max_active_arms) is int and 1 <= max_active_arms <= 12
             and Decimal(str(max_stake_usd)) == 30 and max_open_per_arm == 3, "configured_envelope_required")
    paper_dir = _path(paper_dir, learning_root)
    _require(isinstance(legacy_manifests, (list, tuple)) and legacy_manifests
             and isinstance(legacy_assessments, Mapping), "legacy_history_required")
    policy = ap.execution_policy(max_stake_usd=max_stake_usd, max_open_per_arm=max_open_per_arm)
    for manifest in legacy_manifests:
        _manifest(manifest, now)
    _manifest(new_manifest, now, admission=True, policy=policy)
    _require(new_manifest["selected_ms"] == now, "prepared_manifest_clock_must_match")
    inspection = ap.inspect_rollover(legacy_manifests, paper_dir, learning_root=learning_root,
                                     now_ms=now, max_active_arms=max_active_arms)
    checks = {_key(c["campaign_id"], c["hypothesis_id"]): c for c in inspection["checks"]}
    books, manifests, retired_rows, total_bytes = [], [], {}, 0
    for manifest in legacy_manifests:
        cid = manifest["campaign_id"]
        disk_path = _path(paper_dir.parent / (cid + ".json"), learning_root)
        raw_manifest, stored_manifest = _load(disk_path)
        _require(stored_manifest == manifest, "legacy_manifest_disk_conflict")
        manifests.append({"manifest": copy.deepcopy(manifest), "file": disk_path.name,
                          "raw_sha256": hashlib.sha256(raw_manifest).hexdigest()})
        for arm in manifest["hypotheses"]:
            key = _key(cid, arm["hypothesis_id"])
            check = checks[key]
            base = cp.CandidatePaperRunner(paper_dir, learning_root=learning_root)
            book = base.book_path(cid, arm["hypothesis_id"])
            # Missing/capped/invalid evidence occupies a lease. The expected stored
            # policy is still required before this module can dispatch that book.
            identity = None
            if book.is_file() and not book.is_symlink():
                import sqlite3
                conn = sqlite3.connect(book.as_uri() + "?mode=ro", uri=True, timeout=.25)
                try:
                    conn.execute("PRAGMA query_only=ON")
                    row = conn.execute("SELECT substr(value,1,32769) FROM candidate_meta WHERE key='identity'").fetchone()
                    if row and len(row[0]) <= 32768:
                        identity = json.loads(row[0])
                finally:
                    conn.close()
            if identity is not None:
                configured = identity["runner"]
                _require(Decimal(configured["max_stake_usd"]) <= 30
                         and 1 <= configured["max_open_per_arm"] <= 3, "legacy_stored_envelope_widened")
                actual = _runner(paper_dir, learning_root, configured, lambda: now / 1000)
                _require(actual._identity(manifest, arm) == identity, "legacy_stored_identity_conflict")
            retired = check["status"] == "settled"
            artifacts, rows = [], []
            if retired:
                _require(identity is not None, "settled_identity_missing")
                conn = actual._existing(identity)
                began = time.monotonic()
                conn.set_progress_handler(lambda started=began: int(time.monotonic() - started >= 3), 1000)
                try:
                    _ledger_complete(conn, now)
                    rows = actual._outcomes(conn, identity, now) + actual._incomplete_rows(conn, identity, now)
                finally:
                    conn.close()
                artifacts = [_artifact(book), _artifact(Path(str(book) + "-wal"))]
                total_bytes += sum(a["bytes"] for a in artifacts)
                _require(total_bytes <= MAX_ARCHIVE_BYTES, "archive_total_cap")
                retired_rows[key] = rows
            books.append({"campaign_id": cid, "hypothesis_id": arm["hypothesis_id"], "book": book.name,
                "lease": "retired" if retired else "drain_only", "identity": identity,
                "inspection": check, "artifacts": artifacts})
    drain = sum(b["lease"] == "drain_only" for b in books)
    retired = len(books) - drain
    _require(0 < len(new_manifest["hypotheses"]) <= min(MAX_NEW_ARMS, retired, max_active_arms - drain),
             "no_proven_free_leases_or_duplicate_allowance")
    _require(new_manifest["campaign_id"] not in {m["manifest"]["campaign_id"] for m in manifests}, "new_campaign_identity_reused")
    receipt = {"version": VERSION, "prepared_ms": now, "source_versions": source_versions(),
        "envelope": {"max_active_arms": max_active_arms, "max_stake_usd": "30", "max_open_per_arm": 3},
        "legacy_manifests": manifests, "legacy_assessments": copy.deepcopy(dict(legacy_assessments)),
        "legacy_books": books, "retired_rows": retired_rows, "new_manifest": copy.deepcopy(dict(new_manifest)),
        "active_lease_count": drain + len(new_manifest["hypotheses"]),
        "lifetime_family_size": len(books) + len(new_manifest["hypotheses"]),
        "joint_assessment_policy": _joint_policy(len(books) + len(new_manifest["hypotheses"])),
        "refill_authorized": False, "history_deleted": False, "funding_allowed": False,
        "activation_authorized": False}
    receipt["receipt_sha256"] = _sha(receipt)
    _require(len(_encoded(receipt)) <= MAX_RECEIPT_BYTES, "receipt_byte_cap")
    return receipt


def validate_receipt(receipt, *, now_ms):
    now = _int(now_ms, 1)
    _require(isinstance(receipt, Mapping) and len(_encoded(receipt)) <= MAX_RECEIPT_BYTES, "receipt_missing_or_capped")
    _require(receipt.get("receipt_sha256") == _sha({k: v for k, v in receipt.items() if k != "receipt_sha256"})
             and receipt.get("version") == VERSION and receipt.get("source_versions") == source_versions(), "receipt_source_or_hash_conflict")
    _require(0 < _int(receipt["prepared_ms"], 1) <= now, "receipt_clock")
    envelope = receipt["envelope"]
    _require(envelope == {"max_active_arms": envelope["max_active_arms"], "max_stake_usd": "30", "max_open_per_arm": 3}
             and 1 <= _int(envelope["max_active_arms"], 1) <= 12, "receipt_envelope")
    legacy = receipt["legacy_manifests"]
    _require(isinstance(legacy, list) and legacy, "legacy_manifest_inventory")
    for item in legacy:
        _manifest(item["manifest"], now)
    new = receipt["new_manifest"]
    _manifest(new, now, admission=True, policy=ap.execution_policy(max_stake_usd="30", max_open_per_arm=3))
    _require(new["selected_ms"] == receipt["prepared_ms"], "new_prepared_clock")
    keys = {_key(m["manifest"]["campaign_id"], a["hypothesis_id"]) for m in legacy for a in m["manifest"]["hypotheses"]}
    books = receipt["legacy_books"]
    _require(isinstance(books, list) and len(keys) == len(books)
             and {_key(b["campaign_id"], b["hypothesis_id"]) for b in books} == keys,
             "legacy_book_inventory_changed")
    _require(all(b["lease"] in ("retired", "drain_only") for b in books), "invalid_lease")
    retired = [b for b in books if b["lease"] == "retired"]
    drain = len(books) - len(retired)
    _require(all(b["inspection"]["status"] == "settled" and b["identity"] is not None and len(b["artifacts"]) == 2 for b in retired),
             "retired_without_settlement")
    _require(set(receipt["retired_rows"]) == {_key(b["campaign_id"], b["hypothesis_id"]) for b in retired}, "retired_history_dropped")
    _require(len(books) <= envelope["max_active_arms"] and 0 < len(new["hypotheses"]) <= min(MAX_NEW_ARMS, len(retired), envelope["max_active_arms"] - drain)
             and receipt["active_lease_count"] == drain + len(new["hypotheses"])
             and receipt["lifetime_family_size"] == len(books) + len(new["hypotheses"]), "lease_or_lifetime_capacity")
    _require(receipt.get("joint_assessment_policy") == _joint_policy(receipt["lifetime_family_size"]), "joint_policy_changed")
    _require(all(receipt.get(k) is False for k in ("refill_authorized", "history_deleted", "funding_allowed", "activation_authorized")),
             "unapproved_authority_claim")
    return receipt


def _history_guard(receipt, paper_dir, learning_root, now):
    """Every allocated old book must still exist; retired artifacts never mutate."""
    for saved in receipt["legacy_manifests"]:
        raw, disk = _load(_path(paper_dir.parent / saved["file"], learning_root))
        _require(disk == saved["manifest"] and hashlib.sha256(raw).hexdigest() == saved["raw_sha256"], "legacy_manifest_artifact_changed")
    _, new_disk = _load(_path(paper_dir.parent / (receipt["new_manifest"]["campaign_id"] + ".json"), learning_root))
    _require(new_disk == receipt["new_manifest"], "new_full_manifest_artifact_changed")
    for book in receipt["legacy_books"]:
        if book["lease"] == "retired":
            for artifact in book["artifacts"]:
                _require(_artifact(_path(paper_dir / artifact["file"], learning_root)) == artifact, "retired_book_changed_no_new_allowance")
    inspected = ap.inspect_rollover([m["manifest"] for m in receipt["legacy_manifests"]], paper_dir,
        learning_root=learning_root, now_ms=now, max_active_arms=receipt["envelope"]["max_active_arms"])
    checks = {_key(c["campaign_id"], c["hypothesis_id"]): c for c in inspected["checks"]}
    for book in receipt["legacy_books"]:
        if book["lease"] == "drain_only":
            check = checks[_key(book["campaign_id"], book["hypothesis_id"])]
            _require(book["identity"] is not None and check["status"] in ("blocked", "settled")
                     and check.get("identity_sha256") == book["inspection"].get("identity_sha256")
                     and check.get("unmatched_ledger_rows") == 0 and check.get("ambiguous_orders") == 0,
                     "allocated_legacy_book_missing_or_unproved")
            runner = _runner(paper_dir, learning_root, book["identity"]["runner"], lambda: now / 1000)
            _read_ledger_guard(runner, book["identity"], now)


def _new_book_records(receipt, paper_dir, learning_root, clock):
    runner = ap.AdmissionPaperRunner(paper_dir, learning_root=learning_root, max_stake_usd="30", max_open_per_arm=3, clock=clock)
    manifest = receipt["new_manifest"]
    return runner, [{"hypothesis_id": arm["hypothesis_id"], "campaign_id": manifest["campaign_id"],
                     "book": runner.book_path(manifest["campaign_id"], arm["hypothesis_id"]).name,
                     "identity": runner._identity(manifest, arm)} for arm in manifest["hypotheses"]]


def _empty_new_book(runner, record):
    conn = runner._existing(record["identity"])
    began = time.monotonic()
    conn.set_progress_handler(lambda: int(time.monotonic() - began >= 3), 1000)
    try:
        _require(all(not conn.execute("SELECT 1 FROM " + table + " LIMIT 1").fetchone()
                     for table in ("signals", "decisions", "orders", "order_events", "positions", "trades",
                                   "position_orders", "decision_outcomes", "position_marks", "fill_prices",
                                   "fill_reconciliations", "fill_costs", "candidate_entries", "candidate_protection")),
                 "new_book_preparation_has_history")
    finally:
        conn.close()


def prepare_new_books(receipt, *, paper_dir, learning_root, authority_path, seal_path, now_ms, clock=time.time):
    """Explicit empty-book preparation before any authority exists; no orders.

    A durable immutable intent permits only empty-schema preparation recovery.
    Once the final seal exists, any missing book refuses; it is never recreated.
    The integrating owner must pin the returned seal hash in the later pointer.
    """
    now = _int(now_ms, 1)
    validate_receipt(receipt, now_ms=now)
    paper = _path(paper_dir, learning_root)
    authority, seal_path = _path(authority_path, learning_root), _path(seal_path, learning_root)
    _worker_lock(paper, now)
    _require(not authority.exists(), "authority_already_exists_no_new_book_initialization")
    _history_guard(receipt, paper, learning_root, now)
    runner, records = _new_book_records(receipt, paper, learning_root, clock)
    intent = {"version": VERSION, "receipt_sha256": receipt["receipt_sha256"], "new_books": records,
              "generation": 1, "orders_allowed": False, "funding_allowed": False}
    intent_path = seal_path.with_name(seal_path.name + ".intent")
    _immutable(intent_path, intent)
    if seal_path.exists():
        _, sealed = _load(seal_path)
        _require(sealed.get("receipt_sha256") == receipt["receipt_sha256"]
                 and sealed.get("new_books") == records and sealed.get("seal_sha256") == _sha({k: v for k, v in sealed.items() if k != "seal_sha256"}),
                 "new_book_seal_conflict")
        _require(all(sealed.get(k) == v for k, v in intent.items())
                 and receipt["prepared_ms"] <= _int(sealed.get("prepared_ms"), 1) <= now,
                 "new_book_seal_policy_or_clock")
        for record in records:
            _require((paper / record["book"]).is_file(), "initialized_new_book_missing_no_genesis")
            _empty_new_book(runner, record)
        return sealed
    # Validate ALL existing prefix books before creating any absent suffix.
    for record in records:
        path = _path(paper / record["book"], learning_root)
        if path.exists():
            _empty_new_book(runner, record)
    artifacts = []
    for record in records:
        path = _path(paper / record["book"], learning_root)
        if not path.exists():
            conn = runner._open(record["identity"])
            conn.close()
        _empty_new_book(runner, record)
        artifacts.append(_artifact(path))
    sealed = {**intent, "prepared_ms": now, "initial_artifacts": artifacts}
    sealed["seal_sha256"] = _sha(sealed)
    _immutable(seal_path, sealed)
    return sealed


class _LegacyRouting(cp.CandidatePaperRunner):
    def __init__(self, owner, full, record):
        self.owner, self.full, self.record = owner, full, record
        policy = record["identity"]["runner"]
        super().__init__(owner.paper_dir, learning_root=owner.learning_root,
            max_stake_usd=policy["max_stake_usd"], max_open_per_arm=policy["max_open_per_arm"],
            clock=owner.clock if policy["processing_clock_policy"] == "observed_wall_clock/1" else None)

    def _identity(self, view, arm):
        _require(arm in self.full["hypotheses"] and arm["hypothesis_id"] == self.record["hypothesis_id"]
                 and self.record["lease"] == "drain_only", "internal_route_not_allocated")
        full_identity = super()._identity(self.full, arm)
        _require(_encoded(full_identity) == _encoded(super()._identity(view, arm)) == _encoded(self.record["identity"]),
                 "internal_view_changed_frozen_identity")
        return full_identity


class AllocationRunner:
    """Authority-guarded full-manifest dispatch; never commits an activation."""
    def __init__(self, receipt, *, paper_dir, learning_root, authority_path,
                 new_books_seal_path, expected_new_books_sha256,
                 expected_receipt_sha256, activation_ms, generation=1, clock=time.time):
        self.paper_dir = _path(paper_dir, learning_root)
        self.learning_root = Path(learning_root).resolve()
        self.authority_path = _path(authority_path, learning_root)
        self.new_books_seal_path = _path(new_books_seal_path, learning_root)
        self.receipt = copy.deepcopy(receipt)
        self.expected = {"version": VERSION, "receipt_sha256": expected_receipt_sha256,
                         "new_books_sha256": expected_new_books_sha256,
                         "activation_ms": _int(activation_ms, 1), "generation": _int(generation, 1), "funding_allowed": False}
        _require(generation == 1 and receipt.get("receipt_sha256") == expected_receipt_sha256
                 and activation_ms >= receipt["prepared_ms"], "authority_generation_or_frontier")
        self.clock = clock

    def _now(self, lower):
        supplied = self.clock()
        _require(type(supplied) in (int, float) and math.isfinite(supplied) and supplied > 0, "invalid_wall_clock")
        return max(_int(lower, 1), int(supplied * 1000))

    def _guard(self, manifest, now, *, writes):
        validate_receipt(self.receipt, now_ms=now)
        _, authority = _load(self.authority_path, 8192)
        _require(authority == self.expected and self.expected["activation_ms"] <= now, "authoritative_pointer_missing_or_changed")
        if writes:
            _worker_lock(self.paper_dir, now)
        originals = [m["manifest"] for m in self.receipt["legacy_manifests"]]
        _require(manifest == self.receipt["new_manifest"] or manifest in originals, "full_original_manifest_required")
        _history_guard(self.receipt, self.paper_dir, self.learning_root, now)
        _, seal = _load(self.new_books_seal_path)
        _require(seal.get("receipt_sha256") == self.receipt["receipt_sha256"]
                 and seal.get("seal_sha256") == self.expected["new_books_sha256"] == _sha({k: v for k, v in seal.items() if k != "seal_sha256"}),
                 "new_book_initialization_seal_missing_or_changed")
        _require(seal.get("version") == VERSION and seal.get("generation") == 1
                 and seal.get("orders_allowed") is False and seal.get("funding_allowed") is False
                 and self.receipt["prepared_ms"] <= _int(seal.get("prepared_ms"), 1) <= self.expected["activation_ms"],
                 "new_book_preparation_policy_or_clock")
        runner, expected = _new_book_records(self.receipt, self.paper_dir, self.learning_root, self.clock)
        _require(seal.get("new_books") == expected, "initialized_book_inventory_changed")
        for record in expected:
            path = _path(self.paper_dir / record["book"], self.learning_root)
            _require(path.is_file(), "initialized_new_book_missing_no_genesis")
            _read_ledger_guard(runner, record["identity"], now)
        self._admission(self.receipt["new_manifest"], now)
        return manifest == self.receipt["new_manifest"]

    def _admission(self, manifest, now):
        runner = ap.AdmissionPaperRunner(self.paper_dir, learning_root=self.learning_root,
                                         max_stake_usd="30", max_open_per_arm=3, clock=self.clock)
        for arm in manifest["hypotheses"]:
            identity = runner._identity(manifest, arm)
            path = runner.book_path(manifest["campaign_id"], arm["hypothesis_id"])
            if not path.exists():
                continue
            conn = runner._existing(identity)
            began = time.monotonic()
            conn.set_progress_handler(lambda started=began: int(time.monotonic() - started >= 3), 1000)
            try:
                rows = conn.execute("SELECT substr(payload,1,32769),registered_ms FROM candidate_entries LIMIT 1001").fetchall()
                _require(len(rows) <= 1000, "admission_scope_row_cap")
                for payload, registered in rows:
                    _require(len(payload) <= 32768, "admission_scope_payload_cap")
                    row = json.loads(payload)
                    _require(self.expected["activation_ms"] < _int(row["signal_ms"], 1) <= now
                             and registered is not None and self.expected["activation_ms"] <= _int(registered, 1) <= now,
                             "existing_admission_attempt_before_activation")
                _require(not conn.execute("SELECT 1 FROM orders WHERE created_ms<=? LIMIT 1", (self.expected["activation_ms"],)).fetchone()
                         and not conn.execute("SELECT 1 FROM positions WHERE opened_ms<=? LIMIT 1", (self.expected["activation_ms"],)).fetchone(),
                         "existing_admission_ledger_before_activation")
            finally:
                conn.close()
        return runner

    def _legacy(self, manifest):
        result = []
        for record in self.receipt["legacy_books"]:
            if record["campaign_id"] == manifest["campaign_id"] and record["lease"] == "drain_only":
                _require(record["identity"] is not None, "unknown_legacy_identity_remains_allocated")
                arm = next(a for a in manifest["hypotheses"] if a["hypothesis_id"] == record["hypothesis_id"])
                # This transient routing view is never an artifact or an evaluator input.
                result.append((_LegacyRouting(self, manifest, record), {**manifest, "hypotheses": [arm]}))
        return result

    def _assessment_rows(self, manifest, now):
        """Read actual allocated inventory without creating or ticking any book."""
        rows = []
        if manifest == self.receipt["new_manifest"]:
            runner = self._admission(manifest, now)
            routes = [(runner, runner._identity(manifest, arm)) for arm in manifest["hypotheses"]]
        else:
            routes = [(runner, runner._identity(view, view["hypotheses"][0]))
                      for runner, view in self._legacy(manifest)]
            for book in self.receipt["legacy_books"]:
                if book["campaign_id"] == manifest["campaign_id"] and book["lease"] == "retired":
                    rows.extend(copy.deepcopy(self.receipt["retired_rows"][_key(book["campaign_id"], book["hypothesis_id"])]))
        for runner, identity in routes:
            conn = runner._existing(identity)
            began = time.monotonic()
            conn.set_progress_handler(lambda started=began: int(time.monotonic() - started >= 3), 1000)
            try:
                rows.extend(runner._outcomes(conn, identity, now))
                rows.extend(runner._incomplete_rows(conn, identity, now))
            finally:
                conn.close()
        return rows

    def run_candidates(self, manifest, opportunities, quotes, *, now_ms):
        now = self._now(now_ms)
        if self._guard(manifest, now, writes=True):
            eligible = [r for r in opportunities if _int(r.get("signal_ms", r.get("ts_ms", 0)), 1) > self.expected["activation_ms"]]
            result = self._admission(manifest, now).run_candidates(manifest, eligible, quotes, now_ms=now)
            result["activation_excluded_count"] = len(opportunities) - len(eligible)
            result["activation_frontier_ms"] = self.expected["activation_ms"]
            return result
        result = {"campaign_id": manifest["campaign_id"], "arms": [], "outcomes": [], "assessment_rows": [],
                  "errors": [], "refusals": [], "excluded_by_filter": [], "excluded_by_scope": [],
                  "clock_drain_only": True, "new_registrations_authorized": 0, "processing_frontier_ms": now}
        for runner, view in self._legacy(manifest):
            observed = runner.run_candidates(view, [], quotes, now_ms=now)
            for key in ("arms", "outcomes", "assessment_rows", "errors", "refusals", "excluded_by_filter", "excluded_by_scope"):
                result[key].extend(observed[key])
            result["processing_frontier_ms"] = max(result["processing_frontier_ms"], observed["processing_frontier_ms"])
        for record in self.receipt["legacy_books"]:
            if record["campaign_id"] == manifest["campaign_id"] and record["lease"] == "retired":
                retained = copy.deepcopy(self.receipt["retired_rows"][_key(record["campaign_id"], record["hypothesis_id"])])
                result["assessment_rows"].extend(retained)
                result["outcomes"].extend(r for r in retained if r["status"] == "closed")
                result["arms"].append({"hypothesis_id": record["hypothesis_id"], "allocation": "retired_read_only", "open": 0})
        return result

    def expire_pending(self, manifest, *, now_ms):
        now = self._now(now_ms)
        if self._guard(manifest, now, writes=True):
            return self._admission(manifest, now).expire_pending(manifest, now_ms=now)
        results = [runner.expire_pending(view, now_ms=now) for runner, view in self._legacy(manifest)]
        return {"expired": sum(r["expired"] for r in results), "blocked_by_exposure": sum(r["blocked_by_exposure"] for r in results),
                "errors": [e for r in results for e in r["errors"]], "arms": [a for r in results for a in r["arms"]]}

    def watched_tokens(self, manifest, *, limit=128, now_ms=None):
        _require(type(limit) is int and 0 < limit <= 128, "watched_token_limit")
        now = self._now(now_ms or self.expected["activation_ms"])
        admission = self._guard(manifest, now, writes=now_ms is not None)
        if now_ms is not None:
            expired = self.expire_pending(manifest, now_ms=now)
            _require(not expired["errors"], "pending_expiry_failed")
        if admission:
            return self._admission(manifest, now).watched_tokens(manifest, limit=limit)
        found = set()
        for runner, view in self._legacy(manifest):
            found.update(runner.watched_tokens(view, limit=limit))
        return sorted(found)[:limit]


def assess_family(receipt, outcomes_by_campaign, *, now_ms, activation_ms, paper_dir, learning_root,
                  authority_path, new_books_seal_path, expected_new_books_sha256, generation=1, prior_state=None):
    """Preserve frozen scorer results, then apply an authoritative joint gate."""
    now, activation = _int(now_ms, 1), _int(activation_ms, 1)
    validate_receipt(receipt, now_ms=now)
    _require(generation == 1 and receipt["prepared_ms"] <= activation <= now, "assessment_activation_frontier")
    authority = AllocationRunner(receipt, paper_dir=paper_dir, learning_root=learning_root,
        authority_path=authority_path, expected_receipt_sha256=receipt["receipt_sha256"],
        new_books_seal_path=new_books_seal_path, expected_new_books_sha256=expected_new_books_sha256,
        activation_ms=activation, generation=generation, clock=lambda: now / 1000)
    authority._guard(receipt["new_manifest"], now, writes=False)
    prior = copy.deepcopy(prior_state) if prior_state is not None else {}
    if prior:
        _require(prior.get("version") == VERSION and prior.get("receipt_sha256") == receipt["receipt_sha256"]
                 and prior.get("activation_ms") == activation and prior.get("generation") == generation,
                 "prior_family_identity_conflict")
        _require(prior.get("state_sha256") == _sha({k: v for k, v in prior.items() if k != "state_sha256"})
                 and activation <= _int(prior.get("evaluated_ms"), 1) <= now, "prior_family_seal_or_clock")
        expected_arms = {_key(receipt["new_manifest"]["campaign_id"], a["hypothesis_id"]) for a in receipt["new_manifest"]["hypotheses"]}
        _require(set(prior.get("arms", {})) == expected_arms, "prior_family_arm_inventory")
        for previous in prior["arms"].values():
            _require(isinstance(previous.get("complete_ids"), dict) and _int(previous.get("streak")) >= 0
                     and activation <= _int(previous.get("close_frontier_ms"), 1) <= now,
                     "prior_family_arm_state")
            if previous["streak"]:
                _require(len(previous["complete_ids"]) >= sc.sprint.MIN_N
                         and isinstance(previous.get("counted_day"), str)
                         and datetime.date.fromisoformat(previous["counted_day"]) <= datetime.datetime.fromtimestamp(previous["evaluated_ms"] / 1000, datetime.UTC).date(),
                         "prior_streak_without_complete_evidence")
    manifests = [m["manifest"] for m in receipt["legacy_manifests"]] + [receipt["new_manifest"]]
    results, inputs, declarations = {}, {}, []
    new_cid = receipt["new_manifest"]["campaign_id"]
    for manifest in manifests:
        cid = manifest["campaign_id"]
        supplied = copy.deepcopy(list(outcomes_by_campaign.get(cid, [])))
        # Observed time can legitimately advance on an unchanged read. Every other
        # difference remains a conflicting duplicate for the unchanged scorer.
        existing = {_sha({k: v for k, v in row.items() if k != "observed_ms"}) for row in supplied}
        for row in authority._assessment_rows(manifest, now):
            fingerprint = _sha({k: v for k, v in row.items() if k != "observed_ms"})
            if fingerprint not in existing:
                supplied.append(row)
                existing.add(fingerprint)
        if cid == new_cid:
            for row in supplied:
                if not activation < _int(row.get("signal_ms", 0)) <= now:
                    row["evidence_complete"] = False
        inputs[cid] = supplied
        previous = prior.get("scorer_states", {}).get(cid, receipt["legacy_assessments"].get(cid))
        scorer = ap.assess_admission_campaign if cid == new_cid else sc.assess_campaign
        result = scorer(manifest, supplied, now_ms=now, prior_state=previous)
        results[cid] = result
        for arm in result["arms"]:
            unknown = not result["manifest_valid"] or arm["unknown_count"] or not arm["fresh"] or arm["status"] == "UNPROVEN"
            raw_p = arm.get("p")
            p = raw_p if (not unknown and type(raw_p) in (int, float)
                           and math.isfinite(raw_p) and 0 <= raw_p <= 1) else 1.0
            declarations.append({"campaign_id": cid, "hypothesis_id": arm["hypothesis_id"], "p": p})
    _require(len(declarations) == receipt["lifetime_family_size"], "lifetime_family_dropped")
    corrected = sc.ss.benjamini_hochberg([d["p"] for d in declarations], sc.FDR_Q)
    day = datetime.datetime.fromtimestamp(now / 1000, datetime.UTC).date()
    today, yesterday = day.isoformat(), (day - datetime.timedelta(days=1)).isoformat()
    states, arms = {}, []
    for declaration, significant in zip(declarations, corrected, strict=True):
        cid, hid = declaration["campaign_id"], declaration["hypothesis_id"]
        key = _key(cid, hid)
        local = next(a for a in results[cid]["arms"] if a["hypothesis_id"] == hid)
        previous = prior.get("arms", {}).get(key, {})
        if cid != new_cid:
            arms.append({**declaration, "joint_significant": significant, "status": "LEGACY_EVIDENCE_RETAINED",
                         "legacy_timing_qualified_for_admission": False, "funding_allowed": False})
            continue
        manifest_arm = next(a for a in receipt["new_manifest"]["hypotheses"] if a["hypothesis_id"] == hid)
        complete = {}
        for row in inputs[cid]:
            if row.get("campaign_id") != cid or row.get("hypothesis_id") != hid:
                continue
            valid, _, _ = sc._validate_outcome(row, manifest_arm, receipt["prepared_ms"], now)
            if (valid is not None and row.get(ap.POLICY_KEY) == ap.execution_policy(max_stake_usd="30", max_open_per_arm=3)
                    and row.get("timing_complete") is True
                    and ap.timing_error(row, manifest_arm["spec"], row[ap.POLICY_KEY]) is None
                    and row["signal_ms"] > activation):
                complete[valid["outcome_id"]] = valid
        signatures = {oid: sc._hash({k: v for k, v in row.items() if k != "observed_ms"}) for oid, row in complete.items()}
        old_ids = previous.get("complete_ids", {})
        prefix = all(signatures.get(oid) == digest for oid, digest in old_ids.items())
        new_ids = set(signatures) - old_ids.keys()
        frontier = max((r["closed_ms"] for r in complete.values()), default=activation)
        fresh = bool(complete) and 0 <= now - frontier <= sc.EVIDENCE_MAX_AGE_MS
        monotonic = bool(new_ids) and all(complete[oid]["closed_ms"] > previous.get("close_frontier_ms", activation) for oid in new_ids)
        eligible = bool(significant and prefix and fresh and local["score"]["verdict"] == "PASS"
                        and not local["unknown_count"] and local["comparison"]["complete"]
                        and local["status"] in ("PASS", "PAPER_EVIDENCE_READY", "UNCORRECTED"))
        streak, counted = previous.get("streak", 0), previous.get("counted_day")
        if not eligible:
            streak, why = 0, "joint_gate_or_complete_evidence_unproven"
        elif counted == today:
            why = "same_date_does_not_advance"
        elif not monotonic:
            streak, why = 0, "strictly_new_complete_cashflows_required"
        else:
            streak = streak + 1 if counted == yesterday and streak > 0 else 1
            counted, why = today, "joint_gate_new_complete_consecutive_date"
        states[key] = {"complete_ids": {**signatures, **old_ids}, "close_frontier_ms": max(frontier, previous.get("close_frontier_ms", activation)),
                       "streak": streak, "counted_day": counted, "evaluated_ms": now, "why": why}
        arms.append({**declaration, "joint_significant": significant, "streak": streak, "why": why,
            "status": "PAPER_EVIDENCE_READY" if eligible and streak >= sc.READY_DAYS else "PASS" if eligible else "UNPROVEN",
            "fresh": fresh, "new_complete_ids": sorted(new_ids), "complete_count": len(complete), "funding_allowed": False})
    next_state = {"version": VERSION, "receipt_sha256": receipt["receipt_sha256"], "activation_ms": activation,
                  "generation": generation, "evaluated_ms": now,
                  "arms": states, "scorer_states": {cid: r["next_state"] for cid, r in results.items()}}
    next_state["state_sha256"] = _sha(next_state)
    return {"version": VERSION, "receipt_sha256": receipt["receipt_sha256"], "assessments": results,
            "legacy_assessments_at_preparation": copy.deepcopy(receipt["legacy_assessments"]),
            "family_size": len(declarations), "fdr_q": sc.FDR_Q, "arms": arms, "next_state": next_state,
            "status": "PAPER_EVIDENCE_READY" if any(a["status"] == "PAPER_EVIDENCE_READY" for a in arms) else "UNPROVEN",
            "activation_authorized": False, "funding_allowed": False}


__all__ = ["prepare_receipt", "prepare_new_books", "validate_receipt", "AllocationRunner", "assess_family", "source_versions"]
