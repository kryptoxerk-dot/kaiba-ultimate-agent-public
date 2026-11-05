"""Explicit research-only natural drain and durable admission-cohort cutover.

Codex owns this additive controller. The calling worker must already hold its
exclusive PID lock. Old broker histories are retained in place. A prepared
hypothesis is distinct from activation: recovery before the authoritative pointer
commit uses a fresh admission frontier and cannot replay the intervening signals.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from kaiba.learning import admission_paper as ap
from kaiba.learning import candidate_paper as cp
from kaiba.learning import strategy_campaign as sc

VERSION = "autolearn_clock/1"
MAX_JSON = 2 * 1024 * 1024
MAX_RESEARCH_BYTES = 256 * 1024 * 1024


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _path(root: Path, name: str) -> Path:
    path = root / name
    if not isinstance(name, str) or Path(name).name != name or path.is_symlink() or path.resolve().parent != root:
        raise ValueError("clock cohort artifact path refused")
    return path


def _load(path: Path, default=None):
    if path.is_symlink():
        raise ValueError("clock cohort symlink refused")
    if not path.exists():
        return default
    with path.open("rb") as handle:
        raw = handle.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("clock cohort JSON cap")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("clock cohort JSON shape")
    return value


def _atomic(path: Path, value) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    if len(raw) > MAX_JSON or path.is_symlink():
        raise ValueError("clock cohort write cap or symlink")
    temporary = path.with_suffix(path.suffix + ".clock-partial")
    if temporary.is_symlink():
        raise ValueError("clock cohort temporary symlink refused")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sources() -> dict:
    return {"base": sc.source_versions(), "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def _current(learner, lower: int) -> int:
    supplied = learner.clock()
    seconds = Decimal(str(supplied))
    if isinstance(supplied, bool) or not seconds.is_finite() or seconds <= 0:
        raise ValueError("invalid clock cutover wall clock")
    return max(ap._int(lower), int(seconds * 1000))


def _lock(root: Path, now: int) -> None:
    lock = _load(_path(root, "worker.lock"))
    if (not lock or type(lock.get("pid")) is not int or lock["pid"] != os.getpid()
            or not 0 < ap._int(lock["started_ms"]) <= now):
        raise ValueError("clock cutover requires this worker's exclusive PID lock")


def _cas(root: Path, state: dict, expected, caller: dict) -> None:
    try:
        if _hash(_load(_path(root, "state.json"))) != _hash(expected):
            raise ValueError("clock cohort state changed before CAS")
        _atomic(_path(root, "state.json"), state)
    except BaseException:
        # The parent's finally persists caller state. Do not let a failed CAS or
        # a lost ACK/interruption after os.replace roll back the actual durable
        # pointer. Synchronize only, then preserve the original termination.
        durable = _load(_path(root, "state.json"))
        if durable is not None:
            caller.clear()
            caller.update(durable)
        raise
    caller.clear()
    caller.update(copy.deepcopy(state))


def _frozen(root: Path, names, cap: int) -> list[dict]:
    if not isinstance(names, list) or len(names) > cap or len(set(names)) != len(names):
        raise ValueError("clock cohort active manifest list refused")
    manifests = []
    for name in names:
        manifest = _load(_path(root, name))
        if (not manifest or not name.endswith(".json") or name != manifest.get("campaign_id", "") + ".json"
                or manifest.get("campaign_id") != sc._campaign_id(manifest)
                or manifest.get("source_versions") != sc.source_versions()):
            raise ValueError("clock cohort frozen source/identity mismatch")
        manifests.append(manifest)
    if sum(len(m["hypotheses"]) for m in manifests) > cap:
        raise ValueError("clock cohort configured active-arm cap")
    return manifests


def _file_hash(path: Path, budget: list[int]) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("clock archive file missing or symlink")
    before = path.stat()
    if before.st_size + budget[0] > MAX_RESEARCH_BYTES:
        raise ValueError("clock archive research byte cap")
    digest, began = hashlib.sha256(), time.monotonic()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if time.monotonic() - began > 3:
                raise ValueError("clock archive file hashing deadline")
            budget[0] += len(chunk)
            if budget[0] > MAX_RESEARCH_BYTES:
                raise ValueError("clock archive research byte cap")
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("clock archive file changed while hashing")
    return {"file": path.name, "bytes": after.st_size, "sha256": digest.hexdigest()}


def _archive_files(learner, manifests) -> list[dict]:
    budget, result = [0], []
    runner = cp.CandidatePaperRunner(learner.state_dir / "paper", learning_root=learner.learning_root)
    for manifest in manifests:
        item = {"campaign_id": manifest["campaign_id"],
                "manifest": _file_hash(_path(learner.state_dir, manifest["campaign_id"] + ".json"), budget),
                "source_versions": manifest["source_versions"], "books": []}
        for arm in manifest["hypotheses"]:
            book = runner.book_path(manifest["campaign_id"], arm["hypothesis_id"])
            files = [_file_hash(book, budget)]
            wal = Path(str(book) + "-wal")
            if wal.exists():
                files.append(_file_hash(wal, budget))
            item["books"].append({"hypothesis_id": arm["hypothesis_id"], "files": files})
        result.append(item)
    return result


def _prepared(root: Path, transition: dict, policy: dict) -> tuple[dict, dict] | None:
    receipt = _load(_path(root, transition["receipt_file"]))
    if receipt is None:
        return None
    if (receipt.get("version") != VERSION or receipt.get("transition_id") != transition["transition_id"]
            or receipt.get("sources") != _sources() or receipt.get("execution_policy") != policy
            or receipt.get("legacy_campaign_files") != transition["legacy_campaign_files"]):
        raise ValueError("clock prepared receipt identity/source mismatch")
    manifest = _load(_path(root, receipt["new_manifest_file"]))
    ap.validate_manifest(manifest, expected_policy=policy)
    if (_hash(manifest) != receipt["new_manifest_hash"] or manifest["selected_ms"] != receipt["prepared_ms"]
            or receipt["new_manifest_file"] != manifest["campaign_id"] + ".json"):
        raise ValueError("clock prepared manifest mismatch")
    return receipt, manifest


def bootstrap_clock_cohort(learner, state: dict, now: int) -> list[dict]:
    """Drain old books, then atomically commit one new active cohort, under worker lock."""
    caller, state = state, copy.deepcopy(state)
    root = Path(learner.state_dir).resolve()
    if Path(learner.state_dir).is_symlink() or not root.is_relative_to(Path(learner.learning_root).resolve()):
        raise ValueError("clock controller must stay inside owned learning root")
    now = _current(learner, now)
    _lock(root, now)
    cap = learner.max_candidates
    if type(cap) is not int or not 1 <= cap <= ap.MAX_ARMS:
        raise ValueError("clock upgrade cannot increase configured arm capacity")
    policy = ap.execution_policy(max_stake_usd=learner.max_stake_usd, max_open_per_arm=learner.max_open_per_arm)
    disk = _load(_path(root, "state.json"))
    if disk is not None and any(state.get(key) != disk.get(key)
            for key in ("campaign_files", "clock_upgrade", "selected_ms", "assessment")):
        caller.clear()
        caller.update(disk)
        raise ValueError("clock bootstrap input is older than the authoritative state pointer")
    transition = state.get("clock_upgrade")
    if transition is None:
        names = list(state.get("campaign_files", []))
        manifests = _frozen(root, names, cap)
        if any(ap.POLICY_KEY in m.get("assessment_policy", {}) for m in manifests):
            raise ValueError("admission cohort requires its authoritative transition pointer")
        ident = _hash({"legacy": names, "policy": policy, "sources": _sources(), "cap": cap})[:24]
        transition = {"version": VERSION, "phase": "draining", "transition_id": ident,
            "legacy_campaign_files": names, "sealed_ms": now, "configured_arm_cap": cap,
            "receipt_file": "clock-cutover-" + ident + ".json", "sources": _sources(), "execution_policy": policy}
        state["clock_upgrade"] = transition
        _cas(root, state, disk, caller)
        disk = copy.deepcopy(state)
    if (transition.get("version") != VERSION or transition.get("sources") != _sources()
            or transition.get("execution_policy") != policy or transition.get("configured_arm_cap") != cap
            or transition.get("phase") not in ("draining", "active")):
        raise ValueError("clock transition policy/source/configuration mismatch")
    prepared = _prepared(root, transition, policy)
    if transition["phase"] == "active":
        if prepared is None:
            raise ValueError("active clock cohort lost its archive receipt")
        receipt, manifest = prepared
        if (state.get("campaign_files") != [receipt["new_manifest_file"]]
                or transition.get("receipt_hash") != _hash(receipt)
                or not receipt["prepared_ms"] <= transition["activation_ms"] <= now
                or state.get("selected_ms") != transition["activation_ms"]):
            raise ValueError("active clock cohort pointer mismatch")
        return [manifest]
    if state.get("campaign_files", []) != transition["legacy_campaign_files"]:
        raise ValueError("draining cohort pointer changed")
    manifests = _frozen(root, transition["legacy_campaign_files"], cap)
    if manifests:
        inspection = ap.inspect_rollover(manifests, root / "paper", learning_root=learner.learning_root,
            now_ms=now, max_active_arms=cap)
        transition["inspection"] = inspection
        if inspection["status"] != "ready_for_explicit_archive":
            _cas(root, state, disk, caller)
            return manifests
    else:
        inspection = {"status": "fresh_research_state", "checks": [], "writes": 0}
    archive = _archive_files(learner, manifests)
    if prepared is None:
        prepared_ms = _current(learner, now)
        manifest = ap.generate_admission_campaign([], learner._ladder(), now_ms=prepared_ms,
            groups=learner.groups, max_candidates=cap, max_stake_usd=learner.max_stake_usd,
            max_open_per_arm=learner.max_open_per_arm)
        name = manifest["campaign_id"] + ".json"
        existing = _load(_path(root, name))
        if existing is not None and existing != manifest:
            raise ValueError("clock new manifest path conflict")
        if existing is None:
            _atomic(_path(root, name), manifest)
        receipt = {"version": VERSION, "transition_id": transition["transition_id"], "prepared_ms": prepared_ms,
            "sources": _sources(), "execution_policy": policy, "legacy_campaign_files": transition["legacy_campaign_files"],
            "legacy_artifacts": archive, "legacy_assessment": copy.deepcopy(state.get("assessment", {})),
            "inspection": inspection, "new_manifest_file": name, "new_manifest_hash": _hash(manifest),
            "funding_allowed": False, "history_deleted": False}
        _atomic(_path(root, transition["receipt_file"]), receipt)
    else:
        receipt, manifest = prepared
        if archive != receipt["legacy_artifacts"] or state.get("assessment", {}) != receipt["legacy_assessment"]:
            raise ValueError("clock legacy artifacts changed after preparation")
    activation = _current(learner, max(now, receipt["prepared_ms"]))
    _lock(root, activation)
    transition.update(phase="active", activation_ms=activation, receipt_hash=_hash(receipt),
        new_campaign_id=manifest["campaign_id"], prepared_ms=receipt["prepared_ms"],
        registration_scope="signals strictly after activation frontier", legacy_history_retained=True)
    state["campaign_files"] = [receipt["new_manifest_file"]]
    state["selected_ms"] = activation
    state["source_initialized"] = False
    state["start_journalled"] = False
    state.setdefault("assessment", {})
    state.setdefault("retired_campaigns", []).append({"at_ms": activation,
        "campaign_files": transition["legacy_campaign_files"], "receipt": transition["receipt_file"],
        "receipt_hash": transition["receipt_hash"], "reason": "settled_legacy_clock_history_preserved"})
    lesson = {"evidence_id": "clock-cutover:" + transition["transition_id"], "lane": None,
        "text": "Settled legacy paper history is retained; admission-clock evidence starts after a new activation frontier.",
        "body": json.dumps({"type": "admission_clock_cutover", "receipt": transition["receipt_file"],
            "receipt_hash": transition["receipt_hash"], "prepared_ms": receipt["prepared_ms"],
            "activation_ms": activation, "funding_allowed": False})}
    if not any(row.get("evidence_id") == lesson["evidence_id"] for row in state.setdefault("pending_lessons", [])):
        state["pending_lessons"].append(lesson)
    _cas(root, state, disk, caller)
    return [manifest]


def _is_admission(manifest) -> bool:
    return (ap.POLICY_KEY in manifest.get("assessment_policy", {})
            or any(ap.POLICY_KEY in arm.get("spec", {}) for arm in manifest.get("hypotheses", [])))


class CampaignPaper(cp.CandidatePaperRunner):
    """Flagged worker dispatch: legacy registration is sealed, pending/exits continue."""

    def _runner(self, manifest):
        if not _is_admission(manifest):
            return cp.CandidatePaperRunner(self.state_dir, learning_root=self.learning_root,
                max_stake_usd=self.max_stake_usd, max_open_per_arm=self.max_open_per_arm, clock=self.clock)
        return ap.AdmissionPaperRunner(self.state_dir, learning_root=self.learning_root,
            max_stake_usd=self.max_stake_usd, max_open_per_arm=self.max_open_per_arm, clock=self.clock)

    def _legacy(self, manifest) -> None:
        root = self.state_dir.parent
        state = _load(_path(root, "state.json"))
        transition = state.get("clock_upgrade", {}) if state else {}
        name = manifest.get("campaign_id", "") + ".json"
        if (transition.get("phase") != "draining" or transition.get("sources") != _sources()
                or state.get("campaign_files") != transition.get("legacy_campaign_files")
                or name not in state.get("campaign_files", [])
                or _load(_path(root, name)) != manifest):
            raise ValueError("legacy drain dispatch is outside the authoritative active cohort")

    def _activation(self, manifest) -> int:
        root = self.state_dir.parent
        state = _load(_path(root, "state.json"))
        if not state:
            raise ValueError("admission dispatch requires authoritative active state")
        transition = state["clock_upgrade"]
        prepared = _prepared(root, transition, ap.execution_policy(max_stake_usd=self.max_stake_usd,
                                                                 max_open_per_arm=self.max_open_per_arm))
        if (transition["phase"] != "active" or prepared is None
                or prepared[1] != manifest or transition["receipt_hash"] != _hash(prepared[0])
                or state["campaign_files"] != [prepared[0]["new_manifest_file"]]
                or state["selected_ms"] != transition["activation_ms"]
                or transition["activation_ms"] < prepared[0]["prepared_ms"]):
            raise ValueError("admission dispatch pointer or receipt mismatch")
        return ap._int(transition["activation_ms"])

    def run_candidates(self, manifest, opportunities, quotes, *, now_ms):
        runner = self._runner(manifest)
        if not _is_admission(manifest):
            self._legacy(manifest)
            result = runner.run_candidates(manifest, [], quotes, now_ms=now_ms)
            result["clock_drain_only"] = True
            result["new_registrations_authorized"] = 0
            return result
        activation = self._activation(manifest)
        if activation > self._processing_ms(ap._int(now_ms)):
            raise ValueError("admission activation frontier is in the future")
        eligible, excluded = [], []
        for row in opportunities:
            signal = ap._int(row.get("signal_ms", row.get("ts_ms", 0)))
            if signal > activation:
                eligible.append(row)
            else:
                excluded.append({"signal_id": row.get("signal_id", row.get("decision_id")),
                                 "reason": "signal_not_after_activation_frontier"})
        result = runner.run_candidates(manifest, eligible, quotes, now_ms=now_ms)
        result["activation_frontier_ms"], result["activation_excluded"] = activation, excluded
        return result

    def expire_pending(self, manifest, *, now_ms):
        if _is_admission(manifest):
            self._activation(manifest)
        else:
            self._legacy(manifest)
        return self._runner(manifest).expire_pending(manifest, now_ms=now_ms)

    def watched_tokens(self, manifest, *, limit=128, now_ms=None):
        if _is_admission(manifest):
            self._activation(manifest)
        else:
            self._legacy(manifest)
        return self._runner(manifest).watched_tokens(manifest, limit=limit, now_ms=now_ms)


def assess_dispatch(manifest, paper_outcomes, *, now_ms, prior_state=None):
    assess = ap.assess_admission_campaign if _is_admission(manifest) else sc.assess_campaign
    return assess(manifest, paper_outcomes, now_ms=now_ms, prior_state=prior_state)


__all__ = ["bootstrap_clock_cohort", "CampaignPaper", "assess_dispatch"]
