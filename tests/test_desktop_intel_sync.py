"""Offline end-to-end contracts for the fixed three-feed desktop mirror."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from collections import namedtuple
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from deploy import desktop_intel_sync as sync

NOW = 1_793_670_000.0
Disk = namedtuple("Disk", "total used free")


def document(key="markets", *, now=NOW, **overrides):
    data = {
        "markets": {"quotes": [{"symbol": "^GSPC", "price": 100, "time": "2026-11-03T01:39:50Z"}], "groups": [], "feeds": []},
        "etf_flows": {"units": "US$m", "assets": {"BTC": {
            "ok": True, "rows": [{"date": "2026-11-02", "flows": {"IBIT": 12.5}}],
            "latest": {"date": "2026-11-02", "total": 12.5}}}, "feeds": []},
        "cot": {"contracts": {"btc": {"ok": True, "reports": [{"report_date": "2026-10-30"}]}}, "feeds": []},
    }[key]
    doc = {"key": key, "title": "Public fixture", "ok": True, "stale": False, "error": None,
           "fetched_ts": now - 10, "fetched_at": datetime.fromtimestamp(now - 10, UTC).isoformat(),
           "interval_s": 300, "sources": [], "data": data}
    doc.update(overrides)
    return doc


def bundle(*docs):
    result = []
    for doc in docs:
        raw = json.dumps(doc, ensure_ascii=False, allow_nan=False).encode("utf-8")
        result.append({"key": doc["key"], "sha256": hashlib.sha256(raw).hexdigest(),
                       "content_b64": base64.b64encode(raw).decode("ascii")})
    return {"version": 1, "documents": result}


def write_cache(root, doc):
    path = root / "intel" / f"{doc['key']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_dry_run_never_opens_network_process_or_creates_cache(tmp_path, monkeypatch):
    root = tmp_path / "not-created"
    monkeypatch.setattr(sync, "DATA_ROOT", root)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("dry-run spawned a process"))
    out = sync.run()
    assert out["status"] == "dry_run"
    assert out["collection"]["status"] == out["sync"]["status"] == "not_run"
    assert out["sources"]["eligible"] == []
    assert set(out["sources"]["excluded_reasons"].values()) == {"source_missing"}
    assert not root.exists()


def test_build_payload_preserves_exact_source_bytes_and_only_three_files(tmp_path):
    raw = write_cache(tmp_path, document("etf_flows"))
    write_cache(tmp_path, document("markets", ok=False, stale=True))
    write_cache(tmp_path, document("cot", fetched_ts=NOW + 999))
    (tmp_path / "intel" / "networth.json").write_text('{"private":"never push"}')
    payload, report = sync.build_payload(tmp_path, now=NOW)
    assert report["eligible"] == ["etf_flows"]
    assert base64.b64decode(payload["documents"][0]["content_b64"]) == raw
    assert payload["documents"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["excluded_reasons"] == {"cot": "document_clock", "markets": "document_failed_or_stale"}
    assert "never push" not in json.dumps(report)


def test_receiver_writes_exact_bytes_and_does_not_freshen_source_clocks(tmp_path):
    doc = document()
    payload = bundle(doc)
    out = sync.apply_payload(payload, root=tmp_path, now=NOW, disk_usage=lambda p: Disk(100, 50, 50))
    raw = (tmp_path / "markets.json").read_bytes()
    assert raw == base64.b64decode(payload["documents"][0]["content_b64"])
    assert json.loads(raw)["fetched_ts"] == NOW - 10
    assert json.loads(raw)["data"]["quotes"][0]["time"] == doc["data"]["quotes"][0]["time"]
    assert out["sha256"]["markets"] == hashlib.sha256(raw).hexdigest()
    assert out["written"] == ["markets"]
    assert not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert (tmp_path / "markets.json").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("bad", [
    {"ok": False}, {"stale": True}, {"fetched_ts": NOW + 61}, {"fetched_ts": NOW - 601},
    {"data": {"quotes": [], "groups": [], "feeds": []}},
    {"fetched_at": "2026-11-03T01:39:50"}, {"private_key": "fixture-only-do-not-transmit"},
])
def test_receiver_bad_documents_refused_before_writes(tmp_path, bad):
    payload = bundle(document("etf_flows"), document("markets", **bad))
    with pytest.raises((ValueError, TypeError)):
        sync.apply_payload(payload, root=tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_receiver_validates_whole_bundle_before_writes(tmp_path):
    payload = bundle(document("etf_flows"), document("markets"))
    payload["documents"][1]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        sync.apply_payload(payload, root=tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_receiver_rejects_disk_halt_and_duplicate_keys(tmp_path):
    payload = bundle(document())
    with pytest.raises(ValueError, match="disk_halt"):
        sync.apply_payload(payload, root=tmp_path, now=NOW, disk_usage=lambda p: Disk(100, 92, 8))
    payload["documents"] *= 2
    with pytest.raises(ValueError):
        sync.apply_payload(payload, root=tmp_path, now=NOW)
    assert list(tmp_path.iterdir()) == []


def test_receiver_retains_newer_valid_observation(tmp_path):
    previous = json.dumps(document(now=NOW + 5)).encode()
    (tmp_path / "markets.json").write_bytes(previous)
    out = sync.apply_payload(bundle(document()), root=tmp_path, now=NOW)
    assert out["written"] == [] and out["retained"] == ["markets"]
    assert (tmp_path / "markets.json").read_bytes() == previous


def test_collector_context_restored_and_only_approved_three_called(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_TERMINAL_DATA", "fixture-original")
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        assert os.environ["AEGIS_TERMINAL_DATA"] == str(tmp_path)
        return {"status": "ok", "ran": {"markets": {"ok": True, "error": "never report raw URL"}},
                "not_due": ["cot"], "budget_stopped": ["etf_flows"]}

    out = sync.collect_local(tmp_path, runner=runner)
    assert seen["only"] == ("etf_flows", "cot", "markets") and seen["budget_s"] == 120
    assert os.environ["AEGIS_TERMINAL_DATA"] == "fixture-original"
    assert out["ran"] == {"markets": {"ok": True, "stale": False}}
    assert "raw URL" not in json.dumps(out)
    with pytest.raises(RuntimeError):
        sync.collect_local(tmp_path, runner=lambda **k: (_ for _ in ()).throw(RuntimeError("fixture")))
    assert os.environ["AEGIS_TERMINAL_DATA"] == "fixture-original"


def test_transport_deadline_and_request_cap_stop_before_method(tmp_path, monkeypatch):
    class Http:
        calls = 0

        def get_text(self, url, headers=None):
            self.calls += 1
            return "ok"

    http = Http()
    circuit = sync.HostCircuit(tmp_path, deadline=10, clock=lambda: 1, wall=lambda: NOW)
    transport = sync.GuardedHttp(http, circuit)
    monkeypatch.setattr(sync, "MAX_REQUESTS", 2)
    assert transport.get_text("https://farside.co.uk/btc/") == "ok"
    assert transport.get_text("https://publicreporting.cftc.gov/resource/example") == "ok"
    with pytest.raises(RuntimeError, match="request budget"):
        transport.get_text("https://query1.finance.yahoo.com/v8/chart/TEST")
    assert http.calls == 2
    circuit.deadline = 0
    with pytest.raises(RuntimeError, match="time budget"):
        transport.get_text("https://farside.co.uk/btc/")
    assert http.calls == 2


def test_collection_worker_has_hard_timeout_and_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("GMGN_API_KEY", "fixture-no-real-key")
    monkeypatch.setenv("AEGIS_TERMINAL_DATA", "must-not-use-parent-root")

    def spawn(args, **kwargs):
        assert args[-1] == "--_collect-worker"
        assert kwargs["timeout"] == 120
        assert "GMGN_API_KEY" not in kwargs["env"]
        assert "AEGIS_TERMINAL_DATA" not in kwargs["env"]
        raise subprocess.TimeoutExpired(args, 120)

    monkeypatch.setattr(subprocess, "run", spawn)
    assert sync.run_collection_worker() == {"status": "timed_out"}


def test_ssh_content_is_stdin_only_and_fixed_target_no_retry(monkeypatch):
    payload = bundle(document())

    def spawn(args, **kwargs):
        assert args[0] == "ssh" and sync.SSH_TARGET in args and str(sync.KEY_PATH) in args
        assert "BatchMode=yes" in args and "StrictHostKeyChecking=yes" in args
        assert "ConnectionAttempts=1" in args and kwargs["timeout"] == 60
        assert "Public fixture" not in args[-1]
        assert payload["documents"][0]["content_b64"] not in args[-1]
        assert json.loads(kwargs["input"]) == payload
        return SimpleNamespace(returncode=0, stdout=b'{"status":"ok","written":["markets"],"retained":[],"sha256":{}}')

    monkeypatch.setattr(subprocess, "run", spawn)
    assert sync.sync_payload(payload)["written"] == ["markets"]


def test_sync_failure_is_sanitized(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=255, stdout=b"", stderr=b"sensitive fixture not for output"))
    out = sync.sync_payload(bundle(document()))
    assert out == {"status": "failed", "reason": "remote_refused_or_unreachable"}


def test_active_mirrors_before_failed_push_and_durable_receipt_preserves_failure(tmp_path, monkeypatch):
    root, mirror = tmp_path / "source", tmp_path / "mirror"
    doc = document(now=time.time())
    raw = write_cache(root, doc)
    monkeypatch.setattr(sync, "DATA_ROOT", root)
    monkeypatch.setattr(sync, "MIRROR_ROOT", mirror)
    monkeypatch.setattr(sync, "run_collection_worker", lambda: {"status": "failed"})
    monkeypatch.setattr(sync, "sync_payload", lambda p: {"status": "failed", "reason": "ssh_timeout"})
    out = sync.run(collect_and_sync=True)
    assert out["status"] == "partial" and out["local_mirror"]["written"] == ["markets"]
    assert (mirror / "markets.json").read_bytes() == raw
    receipt = json.loads((root / "desktop_sync_state.json").read_bytes())
    assert receipt["last_run"]["sync"]["reason"] == "ssh_timeout"
    assert receipt["successful_hashes"] == {}
    assert "content_b64" not in json.dumps(receipt)


def test_successful_unchanged_sources_make_no_second_ssh_and_keep_original_clocks(tmp_path, monkeypatch):
    root, mirror = tmp_path / "source", tmp_path / "mirror"
    now = time.time()
    for key in sync.KEYS:
        write_cache(root, document(key, now=now))
    monkeypatch.setattr(sync, "DATA_ROOT", root)
    monkeypatch.setattr(sync, "MIRROR_ROOT", mirror)
    monkeypatch.setattr(sync, "run_collection_worker", lambda: {"status": "ok", "not_due": list(sync.KEYS)})
    calls = []

    def push(payload):
        calls.append(payload)
        return {"status": "ok", "written": [d["key"] for d in payload["documents"]], "retained": []}

    monkeypatch.setattr(sync, "sync_payload", push)
    assert sync.run(collect_and_sync=True)["status"] == "ok"
    out = sync.run(collect_and_sync=True)
    assert out["sync"]["status"] == "unchanged" and out["status"] == "ok"
    assert len(calls) == 1
    assert json.loads((mirror / "markets.json").read_bytes())["fetched_ts"] == now - 10


def test_explicit_failed_worker_without_eligible_cache_reports_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(sync, "run_collection_worker", lambda: {"status": "timed_out"})
    monkeypatch.setattr(sync, "sync_payload", lambda p: pytest.fail("empty source push"))
    out = sync.run(collect_and_sync=True)
    assert out["status"] == "partial"
    assert out["collection"]["status"] == "timed_out"
    assert out["sync"]["status"] == "not_run"
    assert (tmp_path / "desktop_sync_state.json").exists()
