"""Independent offline boundaries for the public desktop cache mirror.

These exercise the real collector fallback/thread paths and receiver, not a
reimplementation. Network and SSH are denied throughout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
from collections import namedtuple
from datetime import UTC, datetime

import httpx
import pytest

from deploy import desktop_intel_sync as sync
from kaiba.market_intel.aegis import institutions, macro

NOW = 1_800_000_000.0
Disk = namedtuple("Disk", "total used free")


@pytest.fixture(autouse=True)
def no_network_or_processes(monkeypatch):
    def refused(*_args, **_kwargs):
        raise AssertionError("Independent desktop sync review must stay offline")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(subprocess, "run", refused)


def disk(_root):
    return Disk(200, 20, 180)


def raw_document(key="markets", *, ts=NOW - 10, **changes):
    data = {
        "markets": {"quotes": [{"symbol": "^GSPC", "price": 7000}], "groups": [], "feeds": []},
        "etf_flows": {"units": "US$m", "assets": {"BTC": {"ok": True, "rows": [
            {"date": "2027-01-14", "flows": {"IBIT": 1.0}},
        ]}}, "feeds": []},
        "cot": {"contracts": {"BTC": {"ok": True, "reports": [{"report_date": "2027-01-12"}]}},
                "feeds": []},
    }[key]
    doc = {"key": key, "title": key, "ok": True, "stale": False, "error": None,
           "fetched_ts": ts, "fetched_at": datetime.fromtimestamp(ts, UTC).isoformat(), "data": data}
    doc.update(changes)
    return json.dumps(doc, allow_nan=False).encode()


def payload(*documents):
    return {"version": 1, "documents": [
        {"key": key, "sha256": hashlib.sha256(raw).hexdigest(),
         "content_b64": base64.b64encode(raw).decode("ascii")} for key, raw in documents
    ]}


class RefusedHttp:
    def __init__(self, code):
        self.code = code
        self.calls = []

    def _call(self, url, **_kwargs):
        self.calls.append(url)
        request = httpx.Request("GET", url)
        response = httpx.Response(self.code, request=request, headers={"Retry-After": "36000"})
        response.raise_for_status()

    get_text = _call
    get_json = _call


@pytest.mark.parametrize("code", [403, 429])
@pytest.mark.parametrize("collector,family", [
    (institutions.collect_etf_flows, "farside"),
    (institutions.collect_cot, "cftc"),
    (macro.collect_markets, "yahoo"),
])
def test_actual_collector_fallbacks_and_workers_stop_after_first_refusal(tmp_path, code, collector, family):
    raw = RefusedHttp(code)
    circuit = sync.HostCircuit(tmp_path, deadline=100, clock=lambda: 1, wall=lambda: NOW)
    guarded = sync.GuardedHttp(raw, circuit)
    with pytest.raises(RuntimeError):
        collector(guarded)
    assert len(raw.calls) == 1
    assert circuit.until[family] >= NOW + 36000
    # A new process/circuit must retain the refusal; no partial-envelope success
    # or next five-minute task can reacquire this provider's capacity.
    resumed = sync.HostCircuit(tmp_path, deadline=100, clock=lambda: 1, wall=lambda: NOW + 300)
    with pytest.raises(RuntimeError, match="cooldown"):
        sync.GuardedHttp(raw, resumed).get_json(raw.calls[0])
    assert len(raw.calls) == 1


def test_malformed_known_provider_cooldown_cannot_grant_capacity(tmp_path):
    (tmp_path / "desktop_provider_cooldowns.json").write_text('{"yahoo":"bad clock"}', encoding="utf-8")
    raw = RefusedHttp(429)
    circuit = sync.HostCircuit(tmp_path, deadline=100, clock=lambda: 1, wall=lambda: NOW)
    with pytest.raises(RuntimeError, match="cooldown"):
        sync.GuardedHttp(raw, circuit).get_json("https://query1.finance.yahoo.com/v8/finance/chart/IBIT")
    assert raw.calls == []


def test_partial_yahoo_success_cannot_hide_refusal_from_durable_circuit(tmp_path):
    class PartialHttp(RefusedHttp):
        def get_json(self, url, **kwargs):
            if not self.calls:
                self.calls.append(url)
                return {"chart": {"error": None, "result": [{"meta": {
                    "regularMarketPrice": 7000, "previousClose": 6900, "regularMarketTime": NOW,
                }}]}}
            return self._call(url, **kwargs)

    raw = PartialHttp(429)
    circuit = sync.HostCircuit(tmp_path, deadline=100, clock=lambda: 1, wall=lambda: NOW)
    result = macro.collect_markets(sync.GuardedHttp(raw, circuit))
    assert len(result["quotes"]) == 1  # collector succeeds despite failed feeds
    assert any(feed["ok"] is False for feed in result["feeds"])
    assert len(raw.calls) == 2  # first success, first refusal; no queued job retries
    resumed = sync.HostCircuit(tmp_path, deadline=100, clock=lambda: 1, wall=lambda: NOW + 300)
    assert resumed.until["yahoo"] >= NOW + 36000
    with pytest.raises(RuntimeError, match="cooldown"):
        sync.GuardedHttp(raw, resumed).get_json(raw.calls[0])
    assert len(raw.calls) == 2


def test_receiver_preserves_original_bytes_and_target_age_when_pc_stops(tmp_path):
    raw = raw_document()
    report = sync.apply_payload(payload(("markets", raw)), root=tmp_path, now=NOW, disk_usage=disk)
    assert report["written"] == ["markets"]
    assert (tmp_path / "markets.json").read_bytes() == raw
    original_clock = json.loads(raw)["fetched_ts"]
    with pytest.raises(ValueError, match="clock"):
        sync.apply_payload(payload(("markets", raw)), root=tmp_path, now=NOW + 601, disk_usage=disk)
    assert (tmp_path / "markets.json").read_bytes() == raw
    assert json.loads((tmp_path / "markets.json").read_bytes())["fetched_ts"] == original_clock


def test_valid_newer_target_is_retained_with_its_exact_source_bytes(tmp_path):
    newer = raw_document(ts=NOW - 1)
    (tmp_path / "markets.json").write_bytes(newer)
    report = sync.apply_payload(payload(("markets", raw_document(ts=NOW - 10))),
                                root=tmp_path, now=NOW, disk_usage=disk)
    assert report["retained"] == ["markets"] and report["written"] == []
    assert (tmp_path / "markets.json").read_bytes() == newer


def test_target_disk_halt_refuses_before_any_cache_write(tmp_path):
    with pytest.raises(ValueError, match="disk_halt"):
        sync.apply_payload(payload(("markets", raw_document())), root=tmp_path, now=NOW,
                           disk_usage=lambda _: Disk(100, 92, 8))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", [
    {"fetched_ts": NOW + 10000}, {"ok": False, "stale": True},
    {"key": "networth"}, {"data": {"quotes": []}},
])
def test_invalid_newer_existing_cache_cannot_block_valid_source(tmp_path, change):
    previous = json.loads(raw_document(ts=NOW - 1))
    previous.update(change)
    existing = json.dumps(previous).encode()
    (tmp_path / "markets.json").write_bytes(existing)
    incoming = raw_document(ts=NOW - 10)
    report = sync.apply_payload(payload(("markets", incoming)), root=tmp_path, now=NOW, disk_usage=disk)
    assert report["written"] == ["markets"]
    assert (tmp_path / "markets.json").read_bytes() == incoming


def test_existing_target_size_is_preflighted_before_any_bundle_write(tmp_path):
    old = raw_document("markets", ts=NOW - 30)
    (tmp_path / "markets.json").write_bytes(old)
    (tmp_path / "cot.json").write_bytes(b"x" * (sync.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="oversize"):
        sync.apply_payload(payload(("markets", raw_document()), ("cot", raw_document("cot"))),
                           root=tmp_path, now=NOW, disk_usage=disk)
    assert (tmp_path / "markets.json").read_bytes() == old
    assert not list(tmp_path.glob(".desktop-sync-*.tmp"))


@pytest.mark.parametrize("mutate", ["hash", "unknown", "duplicate", "future", "credential"])
def test_bad_payload_is_refused_before_any_write(tmp_path, mutate):
    doc = raw_document()
    request = payload(("markets", doc))
    if mutate == "hash":
        request["documents"][0]["sha256"] = "0" * 64
    elif mutate == "unknown":
        request["documents"][0]["key"] = "networth"
    elif mutate == "duplicate":
        request["documents"].append(dict(request["documents"][0]))
    elif mutate == "future":
        request = payload(("markets", raw_document(ts=NOW + 10000)))
    else:
        request = payload(("markets", raw_document(credentials="do-not-transfer")))
    with pytest.raises(ValueError):
        sync.apply_payload(request, root=tmp_path, now=NOW, disk_usage=disk)
    assert list(tmp_path.iterdir()) == []


def test_remote_entrypoint_has_fixed_root_and_budget():
    assert sync._RECEIVER["ROOT"] == "/home/ubuntu/kaiba/data/market-intel/intel"
    assert tuple(sync._RECEIVER["KEYS"]) == ("etf_flows", "cot", "markets")
    assert sync._RECEIVER["MAX_INPUT"] == sync.MAX_REQUEST_BYTES
