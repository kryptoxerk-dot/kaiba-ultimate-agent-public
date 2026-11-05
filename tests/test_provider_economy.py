"""No provider traffic: meaningful dispatch controls for the owner's spend rule."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.ingest import alchemy_ws, feed_endpoints
from kaiba.providers import _http, provider_policy


@pytest.mark.parametrize("url", [
    "https://eth-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY",
    "wss://solana-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY",
    "https://ETH-MAINNET.G.ALCHEMY.COM./v2/TEST_NOT_A_KEY",
    "https://eth-mainnet.alchemyapi.io/v2/TEST_NOT_A_KEY",
])
def test_alchemy_host_blocked_without_disclosing_key(url):
    assert provider_policy.is_alchemy(url)
    reason = provider_policy.refusal(url)
    assert reason.startswith("alchemy_disabled_")
    assert "TEST_NOT_A_KEY" not in reason and "https" not in reason


@pytest.mark.parametrize("url", [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.mainnet.chain.robinhood.com",
    "https://alchemy.com.example.org/v2/example",
    "https://public.example.org/alchemy.com",
])
def test_public_provider_remains_allowed(url):
    assert provider_policy.refusal(url) is None


def test_explicit_alchemy_provider_cannot_hide_under_a_custom_host():
    assert provider_policy.refusal("https://custom.example.org", provider="alchemy")


def test_cutoff_is_jakarta_midnight_and_boolean_cannot_authorize_spend(monkeypatch):
    monkeypatch.setattr(provider_policy, "ALCHEMY_NETWORK_ENABLED", True)
    url = "https://eth-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY"
    assert provider_policy.refusal(url, at=datetime.fromisoformat("2026-11-30T16:59:59+00:00")) == "alchemy_verified_allowance_required"
    assert provider_policy.refusal(url, at=datetime.fromisoformat("2026-11-30T17:00:00+00:00")) == "alchemy_disabled_after_2026_11_owner_budget"
    assert provider_policy.refusal(url, at=datetime(2026, 11, 2)) == "alchemy_policy_clock_unavailable"


def test_http_denied_before_network_and_limiter(monkeypatch):
    import httpx
    dispatch = Mock(side_effect=AssertionError("network spent"))
    reserve = Mock(side_effect=AssertionError("limiter spent"))
    monkeypatch.setattr(httpx, "request", dispatch)
    monkeypatch.setattr(_http, "guarded", reserve)
    for provider in ("rpc", "alchemy", "bsc-snipe-rpc"):
        result = _http.post_json(provider, "rpc.test", "https://eth-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY",
                                 json_body={"method": "eth_blockNumber"})
        assert result.data is None
        assert result.receipt.basis == EvidenceBasis.UNAVAILABLE
        assert result.receipt.note.startswith("alchemy_disabled_")
    dispatch.assert_not_called()
    reserve.assert_not_called()


def test_existing_fresh_cache_spends_nothing(monkeypatch):
    monkeypatch.setattr(_http, "cache_read", lambda *a: ({"result": "0x2"}, False, 1234))
    monkeypatch.setattr(_http, "guarded", Mock(side_effect=AssertionError("spent")))
    result = _http.post_json("alchemy", "rpc.test", "https://eth-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY", ttl_s=5)
    assert result.data == {"result": "0x2"}
    assert result.receipt.basis == EvidenceBasis.CACHED
    assert result.receipt.observed_at_ms == 1234


def test_websocket_denied_and_public_positive_control(monkeypatch):
    import websockets
    sentinel = object()
    connect = Mock(return_value=sentinel)
    monkeypatch.setattr(websockets, "connect", connect)
    with pytest.raises(PermissionError, match="alchemy_disabled_"):
        alchemy_ws._default_connect("wss://solana-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY")
    connect.assert_not_called()
    assert alchemy_ws._default_connect("wss://api.mainnet-beta.solana.com") is sentinel
    connect.assert_called_once()


def test_probe_refuses_without_claiming_quota_exhausted(monkeypatch):
    import httpx
    post = Mock(side_effect=AssertionError("probe spent"))
    monkeypatch.setattr(httpx, "post", post)
    assert feed_endpoints.probe_capacity(Chain.SOL, "https://solana-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY") == feed_endpoints.VERDICT_UNKNOWN
    post.assert_not_called()


def test_resolver_skips_paid_endpoint_and_uses_existing_public_fallback(tmp_db):
    probe = Mock(side_effect=AssertionError("paid probe"))
    env = {
        "SOLANA_FEED_ALCHEMY_URL": "https://solana-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY",
        "KAIBA_FEED_PUBLIC_FALLBACK": "leader_exit",
    }
    settings = SimpleNamespace(rpc_for=lambda _: "https://solana-rpc.publicnode.com", bsc_rpc_url="")
    result = feed_endpoints.resolve("leader_exit", env=env, settings=settings, probe=probe, conn=tmp_db)
    assert isinstance(result, feed_endpoints.FeedEndpoint)
    assert result.kind == feed_endpoints.KIND_PUBLIC
    assert result.ws_host == "api.mainnet-beta.solana.com"
    probe.assert_not_called()


def test_resolver_does_not_enable_unconfigured_fallback(tmp_db):
    settings = SimpleNamespace(rpc_for=lambda _: "https://solana-rpc.publicnode.com", bsc_rpc_url="")
    result = feed_endpoints.resolve("sol_wallets", env={
        "SOLANA_FEED_ALCHEMY_URL": "https://solana-mainnet.g.alchemy.com/v2/TEST_NOT_A_KEY",
    }, settings=settings, conn=tmp_db, probe=Mock(side_effect=AssertionError("spent")))
    assert isinstance(result, feed_endpoints.Refusal)
    assert "alchemy_disabled_" in result.reason
