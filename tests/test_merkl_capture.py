import copy
import json

import httpx
import pytest

from kaiba.hunters import merkl_capture as c
from kaiba.hunters import merkl_entitlements as m
from tests.test_merkl_entitlements import AT, TOKEN, WALLET, fixture_bundle


def run_capture(tmp_path, *, fail_at=None, exception=None):
    bundle = fixture_bundle()
    called = []
    output = tmp_path / "attempt"
    ticks = iter(range(AT, AT + 1000, 10))

    def transport(request):
        i = len(called)
        assert (output / "intent.json").exists()
        assert (output / f"request-{i + 1}.json").exists()
        called.append(copy.deepcopy(request))
        assert request == bundle["receipts"][i]["request"]
        if i == fail_at:
            if exception:
                raise exception
            return 429, b'{"message":"do not log wallet or secret"}'
        return 200, bytes.fromhex(bundle["receipts"][i]["body_hex"])

    result = c.capture(WALLET, output, transport=transport, clock=lambda: next(ticks), pause=lambda _: None)
    return output, result, called


def test_real_capture_algorithm_persists_before_dispatch_and_replays(tmp_path):
    output, result, called = run_capture(tmp_path)
    assert result["status"] == "allocation_checked"
    assert result["requests"] == len(called) == 5
    assert result["remaining_allocation_count"] == 1
    assert WALLET not in json.dumps(result) and TOKEN not in json.dumps(result)
    assert m.replay(json.loads((output / "bundle.json").read_text()), at_ms=AT + 1000)["rewards"][0]["remaining_atoms"] == "700"
    assert not result["funded_action_authorized"]


@pytest.mark.parametrize("index,exception", [(0, None), (2, None), (3, httpx.ReadTimeout("secret"))])
def test_failure_never_retries_or_invents_zero_or_partial_success(tmp_path, index, exception):
    output, result, called = run_capture(tmp_path, fail_at=index, exception=exception)
    assert result["status"] == "unavailable"
    assert len(called) == index + 1
    assert "secret" not in json.dumps(result)
    assert not (output / "evidence.json").exists()
    assert "remaining_allocation_count" not in result


def test_consumed_attempt_cannot_be_reused(tmp_path):
    output, _, _ = run_capture(tmp_path)
    with pytest.raises(FileExistsError):
        c.capture(WALLET, output, transport=lambda _: pytest.fail("network must not run"))


def test_actual_empty_payload_requires_only_one_request_and_no_qualification(tmp_path):
    calls = []
    def transport(request):
        calls.append(request)
        return 200, b"[]"
    result = c.capture(WALLET, tmp_path / "empty", transport=transport, clock=lambda: AT, pause=lambda _: None)
    assert len(calls) == 1
    assert result["status"] == "no_rewards_reported"
    assert result["coverage"] == "api_wallet_chain_only"
    assert result["net_profit_usd"] is None


def test_oversized_and_schema_errors_are_unavailable(tmp_path):
    for i, raw in enumerate((b"x" * (m.MAX_BODY + 1), b"{}", b"null", b"NaN")):
        result = c.capture(WALLET, tmp_path / str(i), transport=lambda _, body=raw: (200, body), clock=lambda: AT)
        assert result["status"] == "unavailable"
        assert result["requests"] == 1


def test_hard_request_cap_before_an_unexpected_extra_dispatch(tmp_path, monkeypatch):
    actual = m.analyze
    def excessive(wallet, exchange):
        for _ in range(m.MAX_REQUESTS + 1):
            exchange({"url": "test-only", "method": "GET", "json": None})
    monkeypatch.setattr(c, "analyze", excessive)
    calls = []
    def transport(request):
        calls.append(request)
        return 200, b"[]"
    result = c.capture(WALLET, tmp_path / "cap", transport=transport, clock=lambda: AT, pause=lambda _: None)
    assert result["reason"] == "request_budget_or_deadline"
    assert len(calls) == m.MAX_REQUESTS
    assert m.analyze is actual


def test_public_http_disables_redirects_credentials_and_automatic_retry(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid"}, stream=httpx.ByteStream(b"moved"))
    real = httpx.Client
    def client(**kwargs):
        assert kwargs["follow_redirects"] is kwargs["trust_env"] is False
        return real(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(c.httpx, "Client", client)
    status, raw = c.public_read({"url": "https://api.merkl.xyz/", "method": "GET", "json": None})
    assert status == 302 and raw == b"moved" and len(requests) == 1
