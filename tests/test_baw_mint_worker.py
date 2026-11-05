import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from kaiba.execution.nft_mint_policy import MintRefused
from kaiba.hunters.baw_mint_worker import MintPreflightWorker, call_worker, handler


class Client:
    calls = 0

    def query(self, operation):
        self.calls += 1
        return {"status": "UNCONNECTED"}

    def preview(self, **kwargs):
        self.calls += 1
        return {"requestId": "fixture-request", "requireConfirmation": False,
                "simulationResult": {"balanceChanges": []}}


def make_worker(tmp_path):
    client = Client()
    return MintPreflightWorker({"state_dir": str(tmp_path)}, client=client)


def stage(worker, **extra):
    return worker.stage({"chain": "bsc", "opportunity_id": "fixture-opportunity",
                         "contract": "fixture-contract", **extra})


def connected(worker):
    worker.connection = {"connected": True, "chains": {"bsc": {"supported": True, "wallet": "fixture-wallet"}},
                         "settings": {"devMode": {"enabled": True, "expiresAt": time.time()+300}}}


@pytest.mark.parametrize("chain", ["bsc", "base", "eth", "robinhood"])
def test_actual_iso_developer_expiry_admits_only_nonbroadcast_preview(tmp_path, monkeypatch, chain):
    worker = make_worker(tmp_path)
    monkeypatch.setattr(time, "time", lambda: 1793635600)
    worker.connection = {"connected": True, "chains": {chain: {"supported": True, "wallet": "fixture-wallet"}},
        "settings": {"devMode": {"enabled": True, "expiresAt": "2026-11-10T00:05:22+08:00"}}}
    key = stage(worker, chain=chain, calldata="fixture-calldata")["candidate_id"]
    result = worker.preview_one(key)
    assert worker.client.calls == 1
    assert result["execution_enabled"] is False
    assert worker.status()["last_preview"]["evidence"]["financial_execution"] is False
    assert worker.status()["last_preview"]["evidence"]["locked_fee_ceiling_verified"] is False


@pytest.mark.parametrize("expiry", [None, True, "2026-11-10T00:05:22", "invalid", "NaN", "2026-11-02T00:05:22+08:00", float("inf"), float("nan"), 0, -1, 10**400])
def test_unknown_naive_expired_or_nonfinite_developer_expiry_never_dispatches(tmp_path, monkeypatch, expiry):
    worker = make_worker(tmp_path)
    connected(worker)
    monkeypatch.setattr(time, "time", lambda: 1793635600)
    worker.connection["settings"]["devMode"]["expiresAt"] = expiry
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]
    with pytest.raises(MintRefused, match="developer_mode_required"):
        worker.preview_one(key)
    assert worker.client.calls == 0


@pytest.mark.parametrize("expiry", [1793635601, 1793635601000, "2026-11-02T16:06:41Z"])
def test_supported_epoch_and_zulu_expiry_remain_valid(tmp_path, monkeypatch, expiry):
    worker = make_worker(tmp_path)
    connected(worker)
    monkeypatch.setattr(time, "time", lambda: 1793635600)
    worker.connection["settings"]["devMode"]["expiresAt"] = expiry
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]
    worker.preview_one(key)
    assert worker.client.calls == 1


def test_new_chain_staging_does_not_invent_runtime_chain_support(tmp_path):
    worker = make_worker(tmp_path)
    connected(worker)  # BSC only, despite Base being an understood chain.
    key = stage(worker, chain="base", calldata="fixture-calldata")["candidate_id"]
    with pytest.raises(MintRefused, match="runtime_chain_or_wallet_unverified"):
        worker.preview_one(key)
    assert worker.client.calls == 0


def test_startup_and_status_do_not_poll_provider_or_claim_live_minting(tmp_path):
    worker = make_worker(tmp_path)
    out = worker.status()
    assert worker.client.calls == 0
    assert out["execution_enabled"] is False
    assert out["gas"]["per_mint_micro_usd"] == 100_000
    assert out["gas"]["daily_micro_usd"] == 10_000_000
    assert out["blockers"] == ["wallet_connection_not_checked"]


def test_refresh_caches_and_disconnected_stops_further_calls(tmp_path):
    worker = make_worker(tmp_path)
    worker.refresh()
    worker.refresh()
    assert worker.client.calls == 1
    assert worker.status()["blockers"] == ["wallet_pairing_required"]


def test_failed_connection_check_does_not_invent_a_pairing_requirement(tmp_path):
    from kaiba.providers.binance_wallet_cli import WalletUnavailable

    worker = make_worker(tmp_path)

    def unavailable(*args):
        raise WalletUnavailable("cli_request_unavailable")

    worker.client.query = unavailable
    result = worker.refresh()
    assert result["blockers"] == ["wallet_connection_unavailable"]
    assert result["connection"]["reason"] == "cli_request_unavailable"


def test_stage_is_deduplicated_and_never_executes_untrusted_payload(tmp_path):
    worker = make_worker(tmp_path)
    a, b = stage(worker), stage(worker)
    assert a["candidate_id"] == b["candidate_id"]
    assert worker.status()["candidates"]["staged"] == 1
    assert worker.client.calls == 0
    with pytest.raises(MintRefused, match="candidate_fields_refused"):
        stage(worker, execute=True)
    with pytest.raises(MintRefused, match="chain_refused"):
        stage(worker, chain="unknown-chain")


def test_preview_hash_matches_exact_retained_bytes(tmp_path):
    worker = make_worker(tmp_path)
    connected(worker)
    worker.client.preview = lambda **_: {"simulationResult": {"balanceChanges": []},
                                        "requestId": "fixture-request", "requireConfirmation": False}
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]
    result = worker.preview_one(key)
    raw = (tmp_path / ("preview-" + key + ".json")).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result["preview_sha256"]
    evidence = worker.status()["last_preview"]["evidence"]
    assert evidence["raw_sha256"] == result["preview_sha256"]
    assert evidence["complete_asset_effects_verified"] is False
    assert evidence["locked_fee_ceiling_verified"] is False
    assert evidence["financial_execution"] is False


def test_existing_artifact_is_never_overwritten_or_repreviewed(tmp_path):
    worker = make_worker(tmp_path)
    connected(worker)
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]
    file = tmp_path / ("preview-" + key + ".json")
    file.write_bytes(b"original evidence")
    result = worker.preview_one(key)
    assert result["state"] == "preview_unavailable" and result["preview_sha256"] is None
    assert file.read_bytes() == b"original evidence"
    assert worker.client.calls == 0


def test_interrupted_publication_has_no_final_or_claimed_receipt(tmp_path, monkeypatch):
    import kaiba.hunters.baw_mint_worker as module

    worker = make_worker(tmp_path)
    connected(worker)
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]

    def unavailable(*args):
        raise OSError("fixture interrupted publication")

    monkeypatch.setattr(module.os, "link", unavailable)
    result = worker.preview_one(key)
    assert result["state"] == "preview_unavailable" and result["preview_sha256"] is None
    assert not (tmp_path / ("preview-" + key + ".json")).exists()
    assert (tmp_path / ("preview-" + key + ".json.partial")).is_file()
    assert worker.status()["last_preview"] is None


def test_legacy_preview_is_retained_but_not_relabelled_as_verified(tmp_path):
    import sqlite3

    path = tmp_path / "candidates.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE candidates(key TEXT PRIMARY KEY,chain TEXT,created_ms INTEGER,payload TEXT,"
                    "state TEXT,preview_day TEXT,preview_sha TEXT,blockers TEXT)")
        con.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)", ("a"*64, "bsc", 1, "{}",
                    "preview_retained_blocked", "2026-11-02", "b"*64, "[]"))
    worker = make_worker(tmp_path)
    last = worker.status()["last_preview"]
    assert last["observed_ms"] is None
    assert last["evidence"]["blockers"] == ["legacy_preview_evidence_unavailable"]
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT preview_sha FROM candidates").fetchone()[0] == "b"*64


def test_preview_needs_wallet_dev_mode_and_verified_builder(tmp_path):
    worker = make_worker(tmp_path)
    key = stage(worker)["candidate_id"]
    with pytest.raises(MintRefused, match="wallet_pairing"):
        worker.preview_one(key)
    connected(worker)
    worker.connection["settings"]["devMode"]["enabled"] = False
    with pytest.raises(MintRefused, match="developer_mode"):
        worker.preview_one(key)
    connected(worker)
    with pytest.raises(MintRefused, match="transaction_builder"):
        worker.preview_one(key)
    assert worker.client.calls == 0


def test_preview_is_capture_only_not_a_fabricated_complete_fee_receipt(tmp_path):
    worker = make_worker(tmp_path)
    connected(worker)
    key = stage(worker, calldata="fixture-calldata")["candidate_id"]
    out = worker.preview_one(key)
    assert out["state"] == "preview_retained_blocked"
    assert "locked_fee_bound_unverified" in out["blockers"]
    assert out["execution_enabled"] is False
    assert len(out["preview_sha256"]) == 64
    worker.preview_one(key)
    assert worker.client.calls == 1
    assert worker.status()["gas"]["pending_micro_usd"] == 0


def test_path_traversal_and_arbitrary_signing_methods_refused(tmp_path):
    worker = make_worker(tmp_path)
    with pytest.raises(MintRefused, match="candidate_identity"):
        worker.preview_one("../../outside")
    with pytest.raises(MintRefused, match="operation_refused"):
        call_worker({}, "execute")
    with pytest.raises(MintRefused, match="worker_token_invalid"):
        handler(worker, "")


def test_real_http_auth_and_no_execute_route(tmp_path):
    worker = make_worker(tmp_path)
    token = "fixture_" + "x"*40
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(worker, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {"port": server.server_port, "token": token}
    try:
        assert call_worker(config, "status")["execution_enabled"] is False
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/status",
                                         data=b"{}", headers={"Authorization": "Bearer wrong"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 403
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/execute",
                                         data=b"{}", headers={"Authorization": "Bearer "+token})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert json.loads(exc.value.read())["reason"] == "operation_refused"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
