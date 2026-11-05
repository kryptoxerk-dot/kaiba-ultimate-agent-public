"""Curve sell reconciliation accepts version one without converting RPC errors to empty."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from kaiba.execution import fills, tweet_launch
from kaiba.ingest import launch_feed


def test_curve_sells_accepts_version_one_and_keeps_rpc_failure_unknown(monkeypatch):
    wallet, token, signature = "synthetic_wallet", "synthetic_token", "synthetic_signature"
    monkeypatch.setattr(launch_feed, "pump_curve_address", lambda mint: "synthetic_curve")
    monkeypatch.setattr(tweet_launch, "get_settings", lambda: SimpleNamespace(
        rpc_for=lambda chain: "https://example.invalid/rpc"))
    transaction = {"version": 1, "blockTime": 100, "transaction": {"message": {
        "accountKeys": [{"pubkey": wallet}]}}, "meta": {
        "err": None, "preBalances": [1000], "postBalances": [1450],
        "preTokenBalances": [{"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "600"}}],
        "postTokenBalances": [{"owner": wallet, "mint": token, "uiTokenAmount": {"amount": "100"}}]}}
    requests = []

    def post(url, *, json, timeout):
        requests.append(deepcopy(json))
        if json["method"] == "getSignaturesForAddress":
            body = {"result": [{"signature": signature, "blockTime": 100, "err": None}]}
        else:
            assert json["method"] == "getTransaction"
            options = json["params"][1]
            assert options["encoding"] == "jsonParsed" and options["commitment"] == "confirmed"
            if options["maxSupportedTransactionVersion"] < transaction["version"]:
                body = {"error": {"code": -32015, "message": "Transaction version 1 is not supported"}}
            else:
                body = {"result": deepcopy(transaction)}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    supported = fills.MAX_TX_VERSION
    assert supported >= 1
    monkeypatch.setattr(fills, "MAX_TX_VERSION", 0)
    with pytest.raises(RuntimeError, match="-32015"):
        tweet_launch.sol_curve_sells(wallet, token, 99_000)
    monkeypatch.setattr(fills, "MAX_TX_VERSION", supported)
    sells = tweet_launch.sol_curve_sells(wallet, token, 99_000)
    assert sells == [tweet_launch.ChainSell(signature, 100_000, 500, 450)]
    assert requests[-1]["params"][1]["maxSupportedTransactionVersion"] == supported
    assert len(requests) == 4
