"""Lossless state-cap recovery, source/lock races and durable recovery evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kaiba.learning import state_compaction as compaction


def state(tmp_path: Path, value=None):
    value = {"pending_lessons": [{"id": "fixture", "consumed": True, "pending": True}],
             "wallet_review_state": {"selections": ["fixed"]}, "counter": 37,
             "history": [False, None, 0, 1.25, "Unicode \u2603"]} if value is None else value
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    (tmp_path / "state.json").write_bytes(raw)
    return raw, hashlib.sha256(raw).hexdigest()


def test_dry_run_is_default_and_creates_nothing(tmp_path):
    raw, digest = state(tmp_path)
    report = compaction.compact_state(tmp_path, expected_sha256=digest)
    assert report["status"] == "dry_run" and report["parsed_equal"]
    assert report["compacted_bytes"] < len(raw)
    assert (tmp_path / "state.json").read_bytes() == raw
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


def test_apply_preserves_every_value_and_exact_durable_original(tmp_path):
    raw, digest = state(tmp_path)
    report = compaction.compact_state(tmp_path, expected_sha256=digest, apply=True, clock=lambda: 1000)
    repaired = (tmp_path / "state.json").read_bytes()
    assert json.loads(repaired) == json.loads(raw)
    assert hashlib.sha256(repaired).hexdigest() == report["compacted_sha256"]
    artifacts = Path(report["artifact_directory"])
    assert (artifacts / "state.original.json").read_bytes() == raw
    receipt = json.loads((artifacts / "receipt.json").read_text())
    assert receipt["status"] == "committed" and receipt["original_sha256"] == digest
    assert receipt["history_removed"] is False and receipt["funding_allowed"] is False
    assert not (tmp_path / "worker.lock").exists()


@pytest.mark.parametrize("raw,reason", [
    (b'{"nested":{"same":1,"same":2}}', "duplicate_json_key"),
    (b'{"value":NaN}', "nonfinite_json_number"),
    (b'{"value":Infinity}', "nonfinite_json_number"),
    (b'{"value":1e999}', "nonfinite_json_number"),
    (b'[]', "state_object_required"),
    (b'{not-json', "invalid_state_json"),
])
def test_bad_json_cannot_be_rewritten(tmp_path, raw, reason):
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(compaction.CompactionRefused, match=reason):
        compaction.compact_state(tmp_path)
    assert (tmp_path / "state.json").read_bytes() == raw


def test_apply_requires_matching_observed_hash(tmp_path):
    raw, _digest = state(tmp_path)
    with pytest.raises(compaction.CompactionRefused, match="apply_requires_expected_sha256"):
        compaction.compact_state(tmp_path, apply=True)
    with pytest.raises(compaction.CompactionRefused, match="observed_sha256_changed"):
        compaction.compact_state(tmp_path, expected_sha256="0" * 64, apply=True)
    assert (tmp_path / "state.json").read_bytes() == raw


def test_never_reclaims_even_a_dead_worker_lock(tmp_path):
    raw, digest = state(tmp_path)
    lock = tmp_path / "worker.lock"
    lock.write_bytes(b'{"pid":999999999,"started_ms":1}')
    existing = lock.read_bytes()
    with pytest.raises(compaction.CompactionRefused, match="worker_lock_occupied_no_reclaim"):
        compaction.compact_state(tmp_path, expected_sha256=digest, apply=True)
    assert lock.read_bytes() == existing and (tmp_path / "state.json").read_bytes() == raw


def test_never_overwrites_occupied_backup_or_receipt(tmp_path):
    raw, digest = state(tmp_path)
    artifacts = tmp_path / "state-compaction" / digest
    artifacts.mkdir(parents=True)
    (artifacts / "receipt.json").write_bytes(b"existing-evidence")
    with pytest.raises(compaction.CompactionRefused, match="repair_artifacts_occupied_no_overwrite"):
        compaction.compact_state(tmp_path, expected_sha256=digest, apply=True)
    assert (artifacts / "receipt.json").read_bytes() == b"existing-evidence"
    assert (tmp_path / "state.json").read_bytes() == raw


def test_source_change_after_backup_prevents_replace(tmp_path, monkeypatch):
    raw, digest = state(tmp_path)
    write_once = compaction._write_once
    changed = raw + b" "

    def raced(path, value, mode=0o600):
        write_once(path, value, mode)
        if path.name == "receipt.json":
            (tmp_path / "state.json").write_bytes(changed)

    monkeypatch.setattr(compaction, "_write_once", raced)
    with pytest.raises(compaction.CompactionRefused, match="source_CAS_conflict"):
        compaction.compact_state(tmp_path, expected_sha256=digest, apply=True)
    assert (tmp_path / "state.json").read_bytes() == changed
    assert (tmp_path / "state-compaction" / digest / "state.original.json").read_bytes() == raw


def test_changed_lock_is_not_deleted_and_source_stays_original(tmp_path, monkeypatch):
    raw, digest = state(tmp_path)
    write_once = compaction._write_once
    foreign = b'{"pid":42,"owner":"foreign"}'

    def raced(path, value, mode=0o600):
        write_once(path, value, mode)
        if path.name == "receipt.json":
            (tmp_path / "worker.lock").write_bytes(foreign)

    monkeypatch.setattr(compaction, "_write_once", raced)
    with pytest.raises(compaction.CompactionRefused, match="worker_lock_changed"):
        compaction.compact_state(tmp_path, expected_sha256=digest, apply=True)
    assert (tmp_path / "worker.lock").read_bytes() == foreign
    assert (tmp_path / "state.json").read_bytes() == raw


def test_input_cap_and_output_cap_are_not_widened(tmp_path, monkeypatch):
    raw, _digest = state(tmp_path)
    monkeypatch.setattr(compaction, "MAX_INPUT_BYTES", len(raw) - 1)
    with pytest.raises(compaction.CompactionRefused, match="input_byte_cap"):
        compaction.compact_state(tmp_path)
    monkeypatch.setattr(compaction, "MAX_INPUT_BYTES", len(raw))
    canonical = compaction._compact(raw)
    monkeypatch.setattr(compaction, "MAX_OUTPUT_BYTES", len(canonical))
    with pytest.raises(compaction.CompactionRefused, match="compacted_state_still_at_or_above_cap"):
        compaction.compact_state(tmp_path)


def test_failure_after_replace_retains_prepared_receipt_and_exact_backup(tmp_path, monkeypatch):
    raw, digest = state(tmp_path)
    replace = compaction.os.replace

    def fail_receipt(source, destination):
        if Path(destination).name == "receipt.json":
            raise OSError("fixture receipt publication failure")
        return replace(source, destination)

    monkeypatch.setattr(compaction.os, "replace", fail_receipt)
    with pytest.raises(compaction.CompactionRefused, match="state_replaced_but_finalization_failed"):
        compaction.compact_state(tmp_path, expected_sha256=digest, apply=True)
    artifacts = tmp_path / "state-compaction" / digest
    assert (artifacts / "state.original.json").read_bytes() == raw
    assert json.loads((artifacts / "receipt.json").read_text())["status"] == "prepared"
    assert json.loads((tmp_path / "state.json").read_bytes()) == json.loads(raw)
    assert not (tmp_path / "worker.lock").exists()


def test_cli_dry_run_and_missing_apply_hash(tmp_path, capsys):
    raw, _digest = state(tmp_path)
    assert compaction.main(["--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert compaction.main(["--state-dir", str(tmp_path), "--apply"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "apply_requires_expected_sha256"
    assert (tmp_path / "state.json").read_bytes() == raw
