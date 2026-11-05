"""Observed networks (HyperEVM, Kinetiq Elysium): canonical ids, probe job and readiness view.

Fake transport only: no live RPC, provider or financial call.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import OBSERVED_NETWORKS, Chain, EvidenceBasis, ObservedNetwork, Receipt
from kaiba.ops import network_status as ns
from kaiba.providers import hyper_rpc
from kaiba.providers._http import Fetched

AT = 1_793_616_000_000
URL = "https://rpc.hyperliquid.xyz/evm"


def settings(hyperevm=URL, elysium=""):
    return SimpleNamespace(hyperevm_rpc_url=hyperevm, elysium_rpc_url=elysium)


@pytest.fixture
def rpc(monkeypatch):
    """The real hyper_rpc.probe_network over a fake transport (Codex's adapter, unchanged)."""
    calls = []
    state = {"chain": "0x3e7"}

    def fetch(provider, endpoint, url, **kw):
        calls.append((url, kw["json_body"]["method"]))
        body = kw["json_body"]
        result = state["chain"] if body["method"] == "eth_chainId" else {
            "number": "0x2fdbc2d", "hash": "0x" + "b" * 64, "timestamp": hex(AT // 1000 - 1),
            "gasLimit": "0x2dc6c0", "baseFeePerGas": "0x5f5e100"}
        receipt = Receipt(provider="rpc", endpoint=endpoint, basis=EvidenceBasis.PROVIDER_REPORTED,
                          observed_at_ms=AT, response_digest="fixture")
        return Fetched({"jsonrpc": "2.0", "id": body["id"], "result": result}, receipt)

    monkeypatch.setattr(hyper_rpc, "request_json", fetch)
    monkeypatch.setattr(hyper_rpc, "now_ms", lambda: AT)
    return calls, state


def by_network(out):
    return {n["network"]: n for n in out["networks"]}


def test_observed_networks_are_not_gmgn_chains_and_keep_unknown_ids_unknown():
    chains = {c.value for c in Chain}
    assert {n.value for n in ObservedNetwork} == {"hyperevm", "elysium"}
    assert not chains & {n.value for n in ObservedNetwork}  # no GMGN core call can be built for them
    hyper, elysium = OBSERVED_NETWORKS[ObservedNetwork.HYPEREVM], OBSERVED_NETWORKS[ObservedNetwork.ELYSIUM]
    assert (hyper.chain_id, hyper.native_symbol, hyper.native_decimals) == (999, "HYPE", 18)
    assert 998 in hyper.refused_chain_ids
    assert elysium.chain_id is None and elysium.native_decimals is None  # unverified stays unknown
    assert {1339, 99801} <= elysium.refused_chain_ids
    assert all(s.chain_id not in s.refused_chain_ids for s in OBSERVED_NETWORKS.values())


def test_every_observed_network_has_a_settings_key():
    from kaiba.core.config import Settings

    fields = Settings.model_fields
    assert all(f"{n.value}_rpc_url" in fields for n in ObservedNetwork)
    assert Settings.model_fields["elysium_rpc_url"].default == ""


def test_the_job_probes_only_a_verified_configured_network_and_stores_the_view(tmp_db, rpc):
    calls, _ = rpc
    out = ns.probe_all(tmp_db, interval_s=900, settings=settings(elysium="https://elysium.invalid/rpc"),
                       at_ms=AT)
    # Elysium has an RPC configured here, but no verified mainnet id: zero requests.
    assert calls == [(URL, "eth_chainId"), (URL, "eth_getBlockByNumber")]
    assert out["method_attempts_upper_bound"] == 2 and out["networks"]["hyperevm"]["available"] is True
    assert out["networks"]["hyperevm"]["zero_call_branch"] is False
    assert out["networks"]["elysium"] == {"available": False, "blockers": ["mainnet_identity_unverified"],
                                          "method_attempts_upper_bound": 0, "zero_call_branch": True}
    stored = tmp_db.execute("SELECT value FROM kv WHERE key='network:probe:hyperevm'").fetchone()[0]
    assert URL not in stored and '"execution_ready":false' in stored


def test_status_labels_identity_and_every_capability_without_a_request(tmp_db, rpc, monkeypatch):
    ns.probe_all(tmp_db, interval_s=900, settings=settings(), at_ms=AT)
    monkeypatch.setattr(hyper_rpc, "probe_network", lambda *a, **k: pytest.fail("status() made a request"))
    out = by_network(ns.status(tmp_db, settings=settings(), at_ms=AT + 60_000))
    hyper, elysium = out["hyperevm"], out["elysium"]
    assert hyper["identity"]["state"] == "verified" and hyper["identity"]["block_number"] == 50183213
    assert hyper["identity"]["age_s"] == 60 and hyper["identity"]["block_age_at_read_s"] == 1.0
    assert elysium["identity"]["state"] == "mainnet_identity_unverified" and elysium["chain_id"] is None
    common = {"gmgn_core": "unsupported", "gmgn_search": "unverified", "executable_market": "not_built",
              "wallet_intelligence": "not_built", "trade_execution": "unsupported",
              "grok_research": "recorded_not_tradeable"}
    for n, partial in ((hyper, "partial_on_demand"), (elysium, "unavailable")):
        caps = {k: v["state"] for k, v in n["capabilities"].items()}
        # measured partial reads (Codex hyper_intelligence) only where the chain can be proved
        assert caps == {**common, "token_metadata": partial, "wallet_account_facts": partial,
                        "token_market_observation": partial}
        assert n["execution_ready"] is False
    assert "kaiba_network_token_facts" in hyper["capabilities"]["token_metadata"]["why"]
    assert "zero calls" in elysium["capabilities"]["wallet_account_facts"]["why"]
    # an Elysium RPC alone does not make its reads available: the mainnet id is still unverified
    configured = by_network(ns.status(tmp_db, settings=settings(elysium="https://elysium.invalid/rpc"),
                                      at_ms=AT))["elysium"]["capabilities"]
    assert configured["token_metadata"] == {
        "state": "unavailable", "why": "mainnet identity unverified: the reader makes zero calls"}


def test_on_demand_reads_are_unavailable_without_an_rpc(tmp_db):
    caps = by_network(ns.status(tmp_db, settings=settings(hyperevm=""), at_ms=AT))["hyperevm"]["capabilities"]
    assert caps["token_metadata"]["state"] == caps["wallet_account_facts"]["state"] == "unavailable"
    # the vendor market read needs no RPC: still available, and it says what it is not
    assert caps["token_market_observation"]["state"] == "partial_on_demand"
    assert "not executable depth or a quote" in caps["token_market_observation"]["why"]


def test_a_wrong_chain_is_unavailable_and_an_old_probe_is_stale(tmp_db, rpc):
    _, state = rpc
    state["chain"] = hex(998)  # the testnet answering where mainnet was configured
    ns.probe_all(tmp_db, interval_s=900, settings=settings(), at_ms=AT)
    ident = by_network(ns.status(tmp_db, settings=settings(), at_ms=AT))["hyperevm"]["identity"]
    assert ident["state"] == "unavailable" and ident["blockers"] == ["rpc_chain_id_mismatch"]
    state["chain"] = "0x3e7"
    ns.probe_all(tmp_db, interval_s=900, settings=settings(), at_ms=AT)
    late = AT + ns.STALE_INTERVALS * 900 * 1000 + 1000
    assert by_network(ns.status(tmp_db, settings=settings(), at_ms=late))["hyperevm"]["identity"]["state"] == "stale"


def test_unconfigured_unprobed_and_unreadable_states(tmp_db):
    out = by_network(ns.status(tmp_db, settings=settings(hyperevm=""), at_ms=AT))
    assert out["hyperevm"]["identity"]["state"] == "rpc_not_configured"
    assert by_network(ns.status(tmp_db, settings=settings(), at_ms=AT))["hyperevm"]["identity"]["state"] == "not_probed_yet"
    closed = sqlite3.connect(":memory:")
    closed.close()
    ident = by_network(ns.status(closed, settings=settings(), at_ms=AT))["hyperevm"]["identity"]
    assert ident == {"state": "unavailable", "blockers": ["probe_store_unreadable:ProgrammingError"]}


def test_scheduler_registers_the_job_with_its_configured_interval():
    from kaiba.ops import scheduler

    assert "network_probe" in scheduler.JOBS
    cfg = scheduler.load_config().jobs["network_probe"]
    assert cfg.enabled and cfg.interval_s == 900 and cfg.timeout_s == 60


def test_alpha_page_shows_which_intelligence_and_execution_paths_are_unavailable(tmp_db, tmp_path, monkeypatch, rpc):
    import shutil

    from fastapi.testclient import TestClient

    from dashboard import intel, trading
    from dashboard.app import create_app
    from kaiba.core.config import DEFAULT_RISK_PATH, get_settings
    from kaiba.hunters import baw_status

    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", "network-panel-test")
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    monkeypatch.setattr(baw_status, "CLIENT_CONFIG", tmp_path / "no-baw-client.json")
    get_settings.cache_clear()
    trading.clear_caches()
    intel.clear_caches()
    ns.probe_all(tmp_db, interval_s=900, settings=settings(), at_ms=AT)
    client = TestClient(create_app())
    try:
        assert client.post("/login", data={"password": "network-panel-test"}, follow_redirects=False).status_code == 303
        page = client.get("/alpha").text
    finally:
        client.close()
        get_settings.cache_clear()
    assert "Observed networks" in page and "execution: unsupported" in page
    assert "stage 1" not in page and "token and account facts on request" in page
    assert page.count("What Kaiba can and cannot do on") == 2  # one native details per network
    assert "no GMGN chain, signer-policy entry, lane or risk budget" in page  # the why, not only a tooltip
    assert "HyperEVM" in page and "chain id 999" in page and "block 50183213" in page
    assert "Kinetiq Elysium" in page and "chain id unverified" in page
    assert "identity: mainnet identity unverified" in page
    for chip in ("gmgn core: unsupported", "gmgn search: unverified", "executable market: not built",
                 "wallet intelligence: not built", "trade execution: unsupported"):
        assert page.count(chip) == 2, chip  # once per network
    for chip in ("token metadata: partial on demand", "wallet account facts: partial on demand",
                 "token market observation: partial on demand", "token market observation: unavailable",
                 "token metadata: unavailable", "wallet account facts: unavailable"):
        assert page.count(chip) == 1, chip  # HyperEVM partial, Elysium unavailable
    assert URL not in page


def _stored(conn, **override):
    """A kv probe record as the job writes it, valid unless overridden."""
    import json

    view = {"available": True, "expected_chain_id": 999, "observed_chain_id": 999, "block_number": 7,
            "block_timestamp_ms": AT - 1000, "blockers": [], "execution_ready": False}
    view.update(override.pop("view", {}))
    record = {"probed_ms": AT, "interval_s": 900, "view": view, **override}
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES ('network:probe:hyperevm', ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(record), AT))
    conn.commit()
    return by_network(ns.status(conn, settings=settings(), at_ms=AT + 1000))["hyperevm"]["identity"]


def test_a_valid_record_verifies_the_positive_control(tmp_db):
    ident = _stored(tmp_db)
    assert ident["state"] == "verified" and ident["age_s"] == 1 and ident["block_number"] == 7


@pytest.mark.parametrize("override, state, blocker", [
    # a successful probe of ANOTHER spec (an edited id, the testnet, a foreign row) verifies nothing
    ({"view": {"expected_chain_id": 998, "observed_chain_id": 998}}, "evidence_spec_mismatch",
     "probe_expected_chain_id_not_current_spec"),
    ({"view": {"expected_chain_id": None}}, "evidence_spec_mismatch", "probe_expected_chain_id_not_current_spec"),
    ({"view": {"expected_chain_id": True}}, "evidence_spec_mismatch", "probe_expected_chain_id_not_current_spec"),
    ({"view": {"observed_chain_id": 998}}, "evidence_spec_mismatch", "probe_observed_chain_id_not_current_spec"),
    ({"view": {"observed_chain_id": None}}, "evidence_spec_mismatch", "probe_observed_chain_id_not_current_spec"),
    # never made fresh by clamping
    ({"probed_ms": AT + 60_000}, "probe_record_invalid", "probe_time_in_future"),
    ({"interval_s": 0}, "probe_record_invalid", "probe_interval_invalid"),
    ({"interval_s": -900}, "probe_record_invalid", "probe_interval_invalid"),
    ({"interval_s": "900"}, "probe_record_invalid", "probe_interval_invalid"),
    ({"interval_s": None}, "probe_record_invalid", "probe_interval_invalid"),
])
def test_records_that_do_not_bind_to_the_current_spec_never_verify(tmp_db, override, state, blocker):
    ident = _stored(tmp_db, **override)
    assert ident["state"] == state and blocker in ident["blockers"]


def test_a_changed_spec_does_not_inherit_the_old_verification(tmp_db, monkeypatch):
    from dataclasses import replace

    assert _stored(tmp_db)["state"] == "verified"
    spec = ns.OBSERVED_NETWORKS[ObservedNetwork.HYPEREVM]
    monkeypatch.setitem(ns.OBSERVED_NETWORKS, ObservedNetwork.HYPEREVM, replace(spec, chain_id=4242))
    ident = by_network(ns.status(tmp_db, settings=settings(), at_ms=AT + 1000))["hyperevm"]["identity"]
    assert ident["state"] == "evidence_spec_mismatch"


def test_mcp_fact_tools_prove_the_chain_and_never_reach_gmgn_or_an_executor(rpc, monkeypatch):
    """Codex's real reader over the fake transport: chain proof first, then fixed reads only."""
    from kaiba.mcp import server
    from kaiba.providers import hyper_intelligence as hi

    calls, _ = rpc
    monkeypatch.setattr(hi, "now_ms", lambda: AT)
    answers = {"eth_getCode": "0x60006000", "eth_getBalance": hex(10**18), "eth_getTransactionCount": "0x3"}

    def facts_fetch(provider, endpoint, url, **kw):
        body = kw["json_body"]
        calls.append((url, body["method"]))
        result = answers.get(body["method"], "0x" + format(18, "064x"))
        receipt = Receipt(provider=provider, endpoint=endpoint, basis=EvidenceBasis.PROVIDER_REPORTED,
                          observed_at_ms=AT, response_digest="fixture")
        return Fetched({"jsonrpc": "2.0", "id": body["id"], "result": result}, receipt)

    monkeypatch.setattr(hi, "request_json", facts_fetch)
    monkeypatch.setattr(hi, "get_settings", lambda: settings())
    import kaiba.providers.gmgn_cli as gmgn

    monkeypatch.setattr(gmgn, "run_read", lambda *a, **k: pytest.fail("observed network reached GMGN"))
    wallet = server.kaiba_network_wallet_facts("hyperevm", "0x" + "2" * 40)
    assert wallet["facts"]["native_balance_base_units"] == str(10**18) and wallet["native_decimals"] == 18
    assert wallet["facts"]["wallet_grade"] is None and wallet["execution_ready"] is False
    assert [m for _, m in calls[:2]] == ["eth_chainId", "eth_getBlockByNumber"]
    calls.clear()
    token = server.kaiba_network_token_facts("hyperevm", "0x" + "3" * 40)
    assert token["facts"]["decimals"] == 18 and token["facts"]["liquidity_usd"] is None
    assert token["atomic_snapshot"] is False and URL not in str(token)
    for refused in ("bsc", "elysium"):  # a GMGN chain and an unverified network: zero reads
        calls.clear()
        out = server.kaiba_network_token_facts(refused, "0x" + "3" * 40)
        assert calls == [] and out["unknowns"] and out["execution_ready"] is False


def test_mcp_registers_both_fact_tools_on_existing_read_operations():
    from kaiba.execution.policy import ALLOWED_READ_OPERATIONS
    from kaiba.mcp import server

    for name, operation in (("kaiba_network_token_facts", "token_info"), ("kaiba_network_wallet_facts", "balance")):
        assert name in server.TOOLS and server.TOOL_OPERATIONS[name] == operation
        assert operation in ALLOWED_READ_OPERATIONS


def test_market_observation_is_unavailable_where_the_vendor_slug_is_unverified(monkeypatch):
    """A network with a verified id but no verified vendor mapping makes zero vendor calls."""
    from dataclasses import replace

    spec = ns.OBSERVED_NETWORKS[ObservedNetwork.ELYSIUM]
    monkeypatch.setitem(ns.OBSERVED_NETWORKS, ObservedNetwork.ELYSIUM, replace(spec, chain_id=4242, refused_chain_ids=frozenset()))
    closed = sqlite3.connect(":memory:")
    caps = by_network(ns.status(closed, settings=settings(elysium="https://elysium.invalid/rpc"), at_ms=AT))["elysium"]["capabilities"]
    assert caps["token_metadata"]["state"] == "partial_on_demand"  # id verified + RPC: the reader may run
    assert caps["token_market_observation"] == {"state": "unavailable", "why": "vendor network mapping unverified: zero calls"}


def test_mcp_market_tool_uses_codexs_provider_and_refuses_unverified_networks(monkeypatch):
    from kaiba.mcp import server
    from kaiba.providers import observed_market as market

    calls = []
    token = "0x" + "4" * 40

    def fetch(provider, endpoint, url, **kw):
        calls.append(url)
        pair = {"chainId": "hyperevm", "dexId": "fixture", "pairAddress": "0x" + "5" * 40,
                "baseToken": {"address": token, "symbol": "TKN"}, "quoteToken": {"address": "0x" + "6" * 40, "symbol": "WHYPE"},
                "priceUsd": "1.25", "fdv": 1000, "liquidity": {"usd": 5000}, "volume": {"h24": 10}, "txns": {}}
        return Fetched([pair], Receipt(provider=provider, endpoint=endpoint, basis=EvidenceBasis.PROVIDER_REPORTED,
                                       observed_at_ms=AT, response_digest="d", note="private upstream"))

    monkeypatch.setattr(market, "get_json", fetch)
    monkeypatch.setattr(market, "now_ms", lambda: AT)
    out = server.kaiba_network_token_market("hyperevm", token)
    assert len(calls) == 1 and out["selected_price_pair"]["requested_token_price_usd"] == "1.25"
    assert out["execution_ready"] is False and out["executable_quote"] is None and "private" not in str(out)
    for refused in ("elysium", "bsc", "sol"):
        calls.clear()
        out = server.kaiba_network_token_market(refused, token)
        assert calls == [] and out["unknowns"] and out["execution_ready"] is False
    from kaiba.execution.policy import ALLOWED_READ_OPERATIONS

    assert server.TOOL_OPERATIONS["kaiba_network_token_market"] == "token_pool" in ALLOWED_READ_OPERATIONS
