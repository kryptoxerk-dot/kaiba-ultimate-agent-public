"""kaiba.hunters.baw_status: a typed, redacted, read-only view of the local BAW worker's /status."""

from __future__ import annotations

import json
import socket
import threading
from http.server import ThreadingHTTPServer

import pytest

from kaiba.hunters import baw_status as bs

NOW_MS = 1_793_600_000_000
SOL_WALLET = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
EVM_WALLET = "0x1111111111111111111111111111111111111111"
GAS = {"day_wib": "2026-11-02", "pending_micro_usd": 0, "spent_micro_usd": 0, "remaining_micro_usd": 10_000_000,
       "halted": False, "per_mint_micro_usd": 100_000, "daily_micro_usd": 10_000_000,
       "scope": "this_worker_ledger", "account_spend_reconciled": False}


def worker_status(connection, **extra):
    return {"service": "kaiba-baw-preflight/1", "automatic_minting_requested": True, "execution_enabled": False,
            "connection": connection, "connection_checked_ms": NOW_MS - 5000, "candidates": {"staged": 2},
            "gas": GAS, "blockers": ["wallet_pairing_required"] if not connection.get("connected") else
            ["raw_mint_effect_and_locked_fee_bound_decoder_required", "execution_adapter_not_armed"], **extra}


def connected(dev_enabled=True, expires_s=NOW_MS // 1000 + 3600, supported=True):
    return {"connected": True, "minting_enabled": False,
            "chains": {"sol": {"supported": supported, "wallet": SOL_WALLET},
                       "bsc": {"supported": supported, "wallet": EVM_WALLET},
                       "robinhood": {"supported": False, "wallet": None}},
            "settings": {"dailyLimit": 50, "quotaLeft": 50, "sessionExpireTime": 123,
                         "devMode": {"enabled": dev_enabled, "expiresAt": expires_s, "dailyLimit": 10}}}


def test_unavailable_states_are_named_not_guessed(tmp_path):
    assert bs.view(bs.fetch(tmp_path / "absent.json"))["reason"] == "client_config_missing"
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert bs.fetch(tmp_path / "bad.json") == {"_unavailable": "client_config_unreadable"}
    closed = tmp_path / "closed.json"
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    closed.write_text(json.dumps({"port": free_port, "token": "x" * 40}), encoding="utf-8")
    assert bs.fetch(tmp_path / "bad.json")["_unavailable"] == "client_config_unreadable"
    (tmp_path / "lowport.json").write_text(json.dumps({"port": 9, "token": "x" * 40}), encoding="utf-8")
    assert bs.fetch(tmp_path / "lowport.json") == {"_unavailable": "client_config_invalid"}
    out = bs.view(bs.fetch(closed, timeout=0.5))
    assert out["available"] is False and out["stage"] == "worker_unavailable"
    assert out["reason"] in ("worker_unreachable", "worker_timeout") and out["can_execute"] is False


def test_the_real_worker_handler_over_loopback_and_a_refused_token(tmp_path):
    # the worker module ships with its own release (Codex); skip where this tree has no copy
    handler = pytest.importorskip("kaiba.hunters.baw_mint_worker").handler

    class Worker:
        def status(self):
            return worker_status({"connected": False, "reason": "wallet_unconnected"})

        def refresh(self):  # must never be called by the readiness view
            raise AssertionError("refresh called")

    token = "t" * 40
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(Worker(), token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        good, bad = tmp_path / "good.json", tmp_path / "bad.json"
        good.write_text(json.dumps({"port": server.server_address[1], "token": token}), encoding="utf-8")
        bad.write_text(json.dumps({"port": server.server_address[1], "token": "u" * 40}), encoding="utf-8")
        out = bs.view(bs.fetch(good), now_ms=NOW_MS)
        assert out["available"] and out["stage"] == "wallet_unpaired" and out["worker"]["recognised"]
        assert "wallet_pairing_required" in out["blockers"]
        assert bs.fetch(bad) == {"_unavailable": "worker_auth_refused"}
    finally:
        server.shutdown()


def test_an_old_or_unknown_worker_is_flagged_and_never_executable():
    out = bs.view({"execution_enabled": False, "connection": {"connected": False}}, now_ms=NOW_MS)
    assert out["stage"] == "worker_version_unrecognised" and out["can_execute"] is False
    assert "worker_version_unrecognised" in out["blockers"]
    assert out["envelope"]["per_mint_usd"] is None  # unknown stays unknown


def test_connected_but_unverified_redacts_wallets_and_session_fields():
    out = bs.view(worker_status(connected(dev_enabled=False)), now_ms=NOW_MS)
    assert out["stage"] == "connected_unverified" and out["can_preview"] is False
    assert "developer_mode_required" in out["blockers"]
    text = json.dumps(out)
    assert SOL_WALLET not in text and EVM_WALLET not in text and "sessionExpireTime" not in text
    assert out["connection"]["chains"]["sol"] == {"supported": True, "wallet_present": True}
    expired = bs.view(worker_status(connected(expires_s=NOW_MS // 1000 - 1)), now_ms=NOW_MS)
    assert expired["stage"] == "connected_unverified" and expired["connection"]["dev_mode"]["active"] is False
    no_chain = bs.view(worker_status(connected(supported=False)), now_ms=NOW_MS)
    assert "runtime_chain_or_wallet_unverified" in no_chain["blockers"]


def test_the_chain_set_matches_the_worker_policy_where_both_exist():
    policy = pytest.importorskip("kaiba.execution.nft_mint_policy")
    assert set(bs.CHAINS) == set(policy.CHAINS)


def test_preview_capable_still_cannot_execute_and_shows_the_envelope():
    out = bs.view(worker_status(connected()), now_ms=NOW_MS)
    assert out["stage"] == "preview_capable" and out["can_preview"] is True and out["can_execute"] is False
    assert "execution_adapter_not_armed" in out["blockers"]
    assert out["envelope"]["per_mint_usd"] == 0.1 and out["envelope"]["daily_usd"] == 10.0
    assert out["envelope"]["account_spend_reconciled"] is False
    assert any(limit.startswith("evm_total_fee_ceiling_unsupported") for limit in out["cli_limits"])
    sol = next(limit for limit in out["cli_limits"] if limit.startswith("solana_fee_ceiling"))
    assert sol.startswith("solana_fee_ceiling_unverified") and "ComputeBudget" in sol  # not claimed impossible
    claimed = bs.view(worker_status(connected(), execution_enabled=True), now_ms=NOW_MS)
    assert claimed["can_execute"] is False and "execution_flag_not_false_reported" in claimed["blockers"]


@pytest.mark.parametrize("worker, checked, reason, blockers, stage, blocker", [
    # Codex's worker (2026-11-02): explicit blockers per state
    ("new", 0, "connection_not_checked", ["wallet_connection_not_checked"], "connection_not_checked",
     "wallet_connection_not_checked"),
    ("new", NOW_MS - 5000, "wallet_unconnected", ["wallet_pairing_required"], "wallet_unpaired",
     "wallet_pairing_required"),
    ("new", NOW_MS - 5000, "cli_timeout", ["wallet_connection_unavailable"], "connection_unavailable",
     "wallet_connection_unavailable"),
    # the old worker said wallet_pairing_required in all three: the reason and clock decide instead
    ("old", 0, "connection_not_checked", ["wallet_pairing_required"], "connection_not_checked",
     "wallet_connection_not_checked"),
    ("old", NOW_MS - 5000, "wallet_unconnected", ["wallet_pairing_required"], "wallet_unpaired",
     "wallet_pairing_required"),
    ("old", NOW_MS - 5000, "cli_timeout", ["wallet_pairing_required"], "connection_unavailable",
     "wallet_connection_unavailable"),
])
def test_three_connection_states_on_new_and_old_workers(worker, checked, reason, blockers, stage, blocker):
    raw = worker_status({"connected": False, "reason": reason}, connection_checked_ms=checked, blockers=blockers)
    if worker == "old":
        raw.pop("last_preview", None)
    out = bs.view(raw, now_ms=NOW_MS)
    assert out["stage"] == stage and blocker in out["blockers"] and out["can_execute"] is False
    if stage != "wallet_unpaired":  # pairing is not claimed unless the worker saw an unconnected wallet
        assert not any(b.startswith("wallet_pairing_required") for b in out["blockers"])


def test_last_preview_evidence_is_whitelisted_and_never_authorizes():
    evidence = {"schema": "baw-preview-evidence/1", "raw_sha256": "a" * 64, "local_request_sha256": "b" * 64,
                "cli_version": "1.12.0", "provider_simulation": "succeeded", "request_id_present": True,
                "complete_asset_effects_verified": False, "locked_fee_ceiling_verified": False,
                "change_counts": {"balanceChanges": 2, "allowanceChanges": 0}, "risk_counts": {"riskDetails": 0},
                "blockers": ["locked_fee_bound_unverified"],
                "recipient": SOL_WALLET, "raw": {"to": EVM_WALLET}}  # a future field must not leak
    last = {"candidate_id": "c" * 64, "observed_ms": NOW_MS - 1000, "evidence": evidence}
    out = bs.view(worker_status(connected(), last_preview=last), now_ms=NOW_MS)
    lp = out["last_preview"]
    assert lp["evidence_available"] and lp["evidence"]["provider_simulation"] == "succeeded"
    assert lp["evidence"]["change_counts"] == {"balanceChanges": 2, "allowanceChanges": 0}
    assert SOL_WALLET not in json.dumps(out) and EVM_WALLET not in json.dumps(out)
    assert out["can_execute"] is False and lp["evidence_blockers"] == ["locked_fee_bound_unverified"]
    legacy = bs.view(worker_status(connected(), last_preview={"candidate_id": "d" * 64, "observed_ms": None,
                     "evidence": {"blockers": ["legacy_preview_evidence_unavailable"]}}), now_ms=NOW_MS)
    assert legacy["last_preview"]["evidence_available"] is False
    assert bs.view(worker_status(connected()), now_ms=NOW_MS)["last_preview"] is None  # old worker / none yet


def test_reads_are_cached_so_page_loads_do_not_hammer_the_worker():
    calls = []

    def fetcher(path):
        calls.append(path)
        return worker_status({"connected": False, "reason": "wallet_unconnected"})

    clock = iter([1000.0, 1010.0, 1031.0]).__next__
    first = bs.status("cfg-a", clock=clock, fetcher=fetcher)
    second = bs.status("cfg-a", clock=clock, fetcher=fetcher)
    third = bs.status("cfg-a", clock=clock, fetcher=fetcher)
    assert len(calls) == 2 and second["cached_age_s"] == 10.0
    assert first["stage"] == third["stage"] == "wallet_unpaired"


@pytest.fixture(autouse=True)
def _clear_cache():
    bs._cache.clear()
    yield
    bs._cache.clear()


# --------------------------------------------------------------------------------------
# dashboard: the readiness panel on /alpha
# --------------------------------------------------------------------------------------

PASSWORD = "baw-panel-test-password"


@pytest.fixture
def console(tmp_db, tmp_path, monkeypatch):
    import shutil

    from fastapi.testclient import TestClient

    from dashboard import intel, trading
    from dashboard.app import create_app
    from kaiba.core.config import DEFAULT_RISK_PATH, get_settings

    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    monkeypatch.setattr(bs, "CLIENT_CONFIG", tmp_path / "no-baw-client.json")
    get_settings.cache_clear()
    trading.clear_caches()
    intel.clear_caches()
    client = TestClient(create_app())
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    try:
        yield client
    finally:
        client.close()
        get_settings.cache_clear()


def test_alpha_panel_says_the_worker_is_unavailable(console):
    page = console.get("/alpha")
    assert page.status_code == 200 and "BAW mint readiness" in page.text
    assert "worker unavailable" in page.text and "client config missing" in page.text
    assert "execution: off" in page.text


def test_alpha_panel_shows_paired_but_unverified_without_addresses(console, monkeypatch):
    monkeypatch.setattr(bs, "status", lambda *a, **k: bs.view(worker_status(connected(dev_enabled=False)), now_ms=NOW_MS))
    page = console.get("/alpha")
    assert "connected unverified" in page.text and "developer mode off" in page.text
    assert "$0.1/mint" in page.text and "evm_total_fee_ceiling_unsupported" in page.text
    assert SOL_WALLET not in page.text and EVM_WALLET not in page.text


def test_alpha_panel_shows_not_checked_and_the_last_preview(console, monkeypatch):
    last = {"candidate_id": "c" * 64, "observed_ms": NOW_MS - 1000,
            "evidence": {"raw_sha256": "a" * 64, "provider_simulation": "succeeded",
                         "complete_asset_effects_verified": False, "locked_fee_ceiling_verified": False}}
    raw = worker_status({"connected": False, "reason": "connection_not_checked"}, connection_checked_ms=0,
                        blockers=["wallet_connection_not_checked"], last_preview=last)
    monkeypatch.setattr(bs, "status", lambda *a, **k: bs.view(raw, now_ms=NOW_MS))
    page = console.get("/alpha")
    assert "connection not checked" in page.text and "wallet pairing required" not in page.text
    assert "simulation succeeded" in page.text and "fee ceiling verified: no" in page.text


def test_the_live_iso_expiry_shape_makes_developer_mode_active():
    """The live CLI reports devMode.expiresAt as "2026-11-10T00:05:22+08:00" (Codex, 2026-11-02); the
    reader used to reject every string, so an enabled developer mode read as inactive."""
    before = 1_794_200_000_000  # 2026-11-09, before the reported expiry
    live = connected(expires_s="2026-11-10T00:05:22+08:00")
    out = bs.view(worker_status(live), now_ms=before)
    assert out["stage"] == "preview_capable" and out["can_preview"] is True and out["can_execute"] is False
    assert "developer_mode_required" not in out["blockers"] and "execution_adapter_not_armed" in out["blockers"]
    assert bs._ms("2026-11-10T00:05:22+08:00") == 1_794_240_322_000 == bs._ms("2026-11-09T16:05:22Z")
    after = bs.view(worker_status(live), now_ms=1_794_240_322_000 + 1)                 # expired
    assert after["can_preview"] is False and "developer_mode_required" in after["blockers"]


@pytest.mark.parametrize("expires", ["2026-11-10T00:05:22", "tomorrow", "", float("inf"), float("nan"), -5, True, None])
def test_naive_unparseable_or_nonfinite_expiry_is_not_active(expires):
    out = bs.view(worker_status(connected(expires_s=expires)), now_ms=NOW_MS)
    assert out["can_preview"] is False and "developer_mode_required" in out["blockers"] and out["can_execute"] is False


def test_unknown_chain_flags_stay_unknown():
    raw = worker_status({"connected": True, "chains": {"sol": {"wallet": SOL_WALLET}, "bsc": {"supported": True},
                                                       "robinhood": {"supported": "yes", "wallet": None}},
                         "settings": {"devMode": {"enabled": True, "expiresAt": NOW_MS // 1000 + 3600}}})
    chains = bs.view(raw, now_ms=NOW_MS)["connection"]["chains"]
    assert chains["sol"] == {"supported": None, "wallet_present": True}
    assert chains["bsc"] == {"supported": True, "wallet_present": None}
    assert chains["robinhood"] == {"supported": None, "wallet_present": False}
    out = bs.view(raw, now_ms=NOW_MS)
    assert out["stage"] == "connected_unverified" and "runtime_chain_or_wallet_unverified" in out["blockers"]


def test_alpha_panel_shows_unknown_chain_flags_as_unknown(console, monkeypatch):
    raw = worker_status({"connected": True, "chains": {"bsc": {"supported": True}},
                         "settings": {"devMode": {"enabled": False}}})
    monkeypatch.setattr(bs, "status", lambda *a, **k: bs.view(raw, now_ms=NOW_MS))
    page = console.get("/alpha")
    assert "bsc: supported, wallet unknown" in page.text


@pytest.mark.parametrize("connection, checked, wallet_chip, dev_chip", [
    # the live box state 2026-11-02 after Codex's release: nothing checked, dev mode unknown
    ({"connected": False, "reason": "connection_not_checked"}, 0, "wallet status unknown", "developer mode status unknown"),
    ({"connected": False, "reason": "cli_timeout"}, NOW_MS - 5000, "wallet check unavailable", "developer mode status unknown"),
    ({"connected": False, "reason": "wallet_unconnected"}, NOW_MS - 5000, "wallet not paired", "developer mode status unknown"),
    ({**connected(dev_enabled=False)}, NOW_MS - 5000, "wallet paired", "developer mode off"),
    ({**connected()}, NOW_MS - 5000, "wallet paired", "developer mode active"),
    ({**connected(expires_s=NOW_MS // 1000 - 60)}, NOW_MS - 5000, "wallet paired", "developer mode expired"),
    ({**connected(), "settings": {"devMode": {"enabled": True}}}, NOW_MS - 5000, "wallet paired",
     "developer mode status unknown"),  # enabled but expiry unknown
])
def test_alpha_panel_chips_follow_the_known_state(console, monkeypatch, connection, checked, wallet_chip, dev_chip):
    raw = worker_status(connection, connection_checked_ms=checked)
    monkeypatch.setattr(bs, "status", lambda *a, **k: bs.view(raw, now_ms=NOW_MS))
    page = console.get("/alpha").text
    assert wallet_chip in page and dev_chip in page
    for wrong in {"wallet paired", "wallet not paired", "wallet status unknown", "wallet check unavailable"} - {wallet_chip}:
        assert f">{wrong}<" not in page
    for wrong in {"developer mode off", "developer mode active", "developer mode expired",
                  "developer mode status unknown"} - {dev_chip}:
        assert f">{wrong}<" not in page
