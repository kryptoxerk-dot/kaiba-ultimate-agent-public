"""`kaiba probe`: the diagnostics command that must never leak what it is diagnosing.

The centrepiece is :func:`test_no_injected_credential_ever_appears_in_the_output`. It
injects a fake credential for every provider, makes every provider fail in the worst way
(an error string containing the credential, which is exactly what ``httpx`` produces for a
401 on a URL-keyed API), and then asserts that not one of those values appears in the
result rows, the JSON the CLI prints, or the rendered table. The rest of the file checks
that the probe stays useful while it does that: correct status classification, defensive
imports for adapters other agents are still writing, lowest limiter priority, and no state
written beyond events.

Offline: the HTTP layer is stubbed, so nothing here opens a socket.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from kaiba.cli import probe
from kaiba.core.config import get_settings
from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.providers import helius
from kaiba.providers._http import Fetched

# Obvious fakes, distinctive enough that a substring search cannot produce a false pass.
FAKE = {
    "GMGN_API_KEY": "gmgn-FAKEKEY-aaaaaaaaaaaa",
    "HELIUS_API_KEY": "helius-FAKEKEY-bbbbbbbbbbbb",
    "BIRDEYE_API_KEY": "birdeye-FAKEKEY-cccccccccccc",
    "COINGECKO_API_KEY": "CG-FAKEKEY-dddddddddddd",
    "ETHERSCAN_API_KEY": "etherscan-FAKEKEY-eeeeeeeeeeee",
    "ALCHEMY_API_KEY": "alchemy-FAKEKEY-ffffffffffff",
    "TELEGRAM_BOT_TOKEN": "8123456789:AAFAKEKEY-gggggggggggggggggggggg",
    "SOLANA_RPC_URL": "https://rpc.invalid.test/?api-key=rpc-FAKEKEY-hhhhhhhhhhhh",
    "HELIUS_WEBHOOK_SECRET": "webhook-FAKEKEY-iiiiiiiiiiii",
}


def ok(data: Any, provider: str = "x", endpoint: str = "y") -> Fetched:
    return Fetched(data, Receipt(provider=provider, endpoint=endpoint,
                                 basis=EvidenceBasis.PROVIDER_REPORTED))


def failed(note: str, provider: str = "x", endpoint: str = "y") -> Fetched:
    return Fetched(None, Receipt(provider=provider, endpoint=endpoint,
                                 basis=EvidenceBasis.UNAVAILABLE, note=note))


@pytest.fixture
def fake_creds(tmp_db, monkeypatch):
    """Every credential set to a known fake. Ordering matters: after ``tmp_db``."""
    for name, value in FAKE.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield tmp_db
    get_settings.cache_clear()


@pytest.fixture
def no_creds(tmp_db, monkeypatch):
    for name in FAKE:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("SOLANA_RPC_URL", "")
    get_settings.cache_clear()
    yield tmp_db
    get_settings.cache_clear()


@pytest.fixture
def http(monkeypatch):
    """Stub the shared HTTP layer for both the probe and the Helius adapter it calls."""
    state: dict[str, Any] = {"by_provider": {}, "default": failed("no stub configured"),
                             "calls": []}

    def fake(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        state["calls"].append({"provider": provider, "endpoint": endpoint, "url": url, **kw})
        resp = state["by_provider"].get(provider, state["default"])
        return resp(provider, endpoint, url, kw) if callable(resp) else resp

    for module in (probe, helius):
        monkeypatch.setattr(module, "get_json", fake)
        monkeypatch.setattr(module, "post_json", fake)
    return state


@pytest.fixture
def no_gmgn(monkeypatch):
    """Pretend the sibling GMGN wrapper has not landed yet (it is being written elsewhere)."""
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", None)
    monkeypatch.setitem(sys.modules, "kaiba.providers.dexscreener", None)


def render_text(results: list[dict[str, Any]]) -> str:
    from rich.console import Console

    buffer = io.StringIO()
    probe.render(results, Console(file=buffer, width=400, no_color=True))
    return buffer.getvalue()


# ------------------------------------------------------------------ the point of the task


def test_no_injected_credential_ever_appears_in_the_output(fake_creds, http, no_gmgn):
    """Every provider fails with its own credential quoted back; nothing may escape.

    This is what a real 401 looks like: ``httpx`` puts the full request URL, credential and
    all, into the exception text, and that text lands in the receipt note.
    """
    def leaky(provider: str, endpoint: str, url: str, kw: dict[str, Any]) -> Fetched:
        leaked = " ".join(FAKE.values())
        return failed(f"HTTPStatusError: 401 Unauthorized for url '{url}' [{leaked}]")

    http["default"] = leaky

    results = probe.run_probe()
    haystacks = [json.dumps(results, default=str), render_text(results), repr(results)]

    for name, value in FAKE.items():
        for haystack in haystacks:
            assert value not in haystack, f"{name} leaked into probe output"
        assert name in haystacks[0] or name == "HELIUS_WEBHOOK_SECRET", f"{name} not named"
    assert "<redacted>" in haystacks[0]


def test_credentials_are_reported_by_name_and_presence_only(fake_creds, http, no_gmgn):
    results = {r["provider"]: r for r in probe.run_probe()}
    assert results["helius"]["credential_name"] == "HELIUS_API_KEY"
    assert results["helius"]["credential"] is True
    assert results["dexscreener"]["credential_name"] == "(keyless)"
    for row in results.values():
        assert set(row) >= {"provider", "credential", "reachable", "detail", "status", "latency_ms"}
        assert isinstance(row["credential"], bool)


def test_missing_credentials_are_flagged_without_calling_the_provider(no_creds, http, no_gmgn):
    results = {r["provider"]: r for r in probe.run_probe()}
    for name in ("helius", "birdeye", "etherscan", "alchemy", "telegram", "solana-rpc"):
        assert results[name]["status"] == probe.MISSING_CREDENTIAL, name
        assert results[name]["reachable"] is False
        assert results[name]["latency_ms"] is None
    called = {c["provider"] for c in http["calls"]}
    assert called <= {"dexscreener", "coingecko"}, "keyed providers must not be called unkeyed"


def test_redaction_catches_shapes_it_has_never_seen(no_creds):
    """Exact-value redaction is not enough; a new provider will invent a new parameter."""
    text = (
        "GET https://api.example.com/v1?apikey=NEVER-SEEN-BEFORE-123&x=1 -> 401; "
        "https://api.telegram.org/bot99887766:ZZTOPSECRETTOKEN/getMe; "
        "https://eth-mainnet.g.alchemy.com/v2/UNSEENALCHEMYKEY1234567890"
    )
    out = probe.redact(text, secrets=[])
    assert "NEVER-SEEN-BEFORE-123" not in out
    assert "ZZTOPSECRETTOKEN" not in out
    assert "UNSEENALCHEMYKEY1234567890" not in out
    assert out.count("<redacted>") == 3


def test_rendering_prints_the_credential_name_and_never_a_value(fake_creds, http, no_gmgn):
    http["default"] = ok({"ok": True})
    text = render_text(probe.run_probe())
    assert "HELIUS_API_KEY" in text
    assert "TELEGRAM_BOT_TOKEN" in text
    assert "no value is ever printed" in text
    for value in FAKE.values():
        assert value not in text


# ------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("HTTPStatusError: 401 Unauthorized for url 'https://x'", probe.AUTH_ERROR),
        ("HTTPStatusError: 403 Forbidden", probe.AUTH_ERROR),
        ("rate limited: 429 too many requests", probe.RATE_LIMITED),
        ("ConnectTimeout: timed out", probe.UNREACHABLE),
        ("helius monthly credit budget exhausted: 10/10 used", probe.BUDGET_EXHAUSTED),
    ],
)
def test_failure_notes_map_to_the_status_an_operator_can_act_on(note, expected):
    assert probe.classify(failed(note)).status == expected


def test_a_successful_fetch_is_ok(fake_creds):
    assert probe.classify(ok({"result": 1}), ok_detail="fine") == probe.Outcome(probe.OK, "fine")


def test_every_probe_call_uses_the_lowest_limiter_priority(fake_creds, http, no_gmgn):
    http["default"] = ok({"ok": True, "result": "ok"})
    probe.run_probe()
    assert http["calls"], "nothing was probed"
    for call in http["calls"]:
        assert call["priority"] is Priority.RESEARCH, call["provider"]
    assert Priority.RESEARCH == max(Priority), "RESEARCH must remain the starve-first class"


def test_latency_is_measured_for_every_call_that_happened(fake_creds, http, no_gmgn):
    http["default"] = ok({"ok": True, "result": "ok"})
    for row in probe.run_probe():
        if row["status"] != probe.MISSING_CREDENTIAL:
            assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0


# ------------------------------------------------------------------- per-provider checks


def test_a_missing_sibling_adapter_is_not_built_rather_than_a_crash(fake_creds, http, no_gmgn):
    results = {r["provider"]: r for r in probe.run_probe()}
    assert results["gmgn"]["status"] == probe.NOT_BUILT
    assert "gmgn_cli" in results["gmgn"]["detail"]


def test_the_gmgn_wrapper_is_used_as_soon_as_it_exposes_a_probe(fake_creds, http, monkeypatch):
    import types

    module = types.ModuleType("kaiba.providers.gmgn_cli")
    module.probe = lambda: "gmgn-cli 1.6.1"
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)
    monkeypatch.setitem(sys.modules, "kaiba.providers.dexscreener", None)

    row = next(r for r in probe.run_probe(["gmgn"]))
    assert row["status"] == probe.OK
    assert "1.6.1" in row["detail"]


def test_a_wrapper_that_answers_nothing_is_not_reported_as_ok(fake_creds, http, monkeypatch):
    """``gmgn_cli.cli_argv()`` returns None when the CLI is not installed."""
    import types

    module = types.ModuleType("kaiba.providers.gmgn_cli")
    module.cli_argv = lambda: None
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    row = probe.run_probe(["gmgn"])[0]
    assert row["status"] == probe.UNREACHABLE
    assert "cli_argv() returned None" in row["detail"]


def test_a_wrapper_without_an_entry_point_is_unknown_not_ok(fake_creds, http, monkeypatch):
    import types

    module = types.ModuleType("kaiba.providers.gmgn_cli")
    module.swap = lambda token, amount: None  # present, but not a probe
    monkeypatch.setitem(sys.modules, "kaiba.providers.gmgn_cli", module)

    row = probe.run_probe(["gmgn"])[0]
    assert row["status"] == probe.UNKNOWN


def test_dexscreener_is_probed_directly_while_its_adapter_is_unwritten(fake_creds, http, no_gmgn):
    http["by_provider"]["dexscreener"] = ok([{"tokenAddress": "abc"}])
    row = probe.run_probe(["dexscreener"])[0]
    assert row["status"] == probe.OK
    assert "adapter not built" in row["detail"]
    assert http["calls"][0]["url"].endswith("/token-profiles/latest/v1")


def test_the_dexscreener_adapter_wins_once_it_lands(fake_creds, http, monkeypatch):
    import types

    module = types.ModuleType("kaiba.providers.dexscreener")
    module.health = lambda: "profiles ok"
    monkeypatch.setitem(sys.modules, "kaiba.providers.dexscreener", module)

    row = probe.run_probe(["dexscreener"])[0]
    assert (row["status"], row["detail"]) == (probe.OK, "profiles ok")
    assert http["calls"] == []


def test_helius_is_probed_with_its_cheapest_authenticated_read(fake_creds, http, no_gmgn):
    http["by_provider"]["helius"] = ok({"jsonrpc": "2.0", "result": "ok"})
    row = probe.run_probe(["helius"])[0]
    assert row["status"] == probe.OK
    assert row["detail"] == "getHealth=ok"
    call = http["calls"][0]
    assert call["json_body"]["method"] == "getHealth"


def test_helius_reports_an_exhausted_budget_instead_of_calling(fake_creds, http, monkeypatch):
    cfg = type("R", (), {"provider_budgets": {"helius": {"monthly_credit_cap": 0}}})()
    monkeypatch.setattr(helius, "get_risk", lambda: cfg)
    row = probe.run_probe(["helius"])[0]
    assert row["status"] == probe.BUDGET_EXHAUSTED
    assert http["calls"] == []


def test_etherscan_reads_the_error_out_of_a_200_body(fake_creds, http, no_gmgn):
    http["by_provider"]["etherscan"] = ok(
        {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
    )
    row = probe.run_probe(["etherscan"])[0]
    assert row["status"] == probe.AUTH_ERROR
    assert "Invalid API Key" in row["detail"]


def test_etherscan_success_is_decoded_to_a_block_number(fake_creds, http, no_gmgn):
    http["by_provider"]["etherscan"] = ok({"jsonrpc": "2.0", "result": "0x15af1c0"})
    row = probe.run_probe(["etherscan"])[0]
    assert row["status"] == probe.OK
    assert row["detail"] == f"block {0x15af1c0}"


def test_birdeye_soft_failure_is_an_auth_error(fake_creds, http, no_gmgn):
    http["by_provider"]["birdeye"] = ok({"success": False, "message": "Unauthorized"})
    row = probe.run_probe(["birdeye"])[0]
    assert row["status"] == probe.AUTH_ERROR


def test_telegram_reports_the_bot_username_not_the_token(fake_creds, http, no_gmgn):
    http["by_provider"]["telegram"] = ok({"ok": True, "result": {"username": "kaibatestbot"}})
    row = probe.run_probe(["telegram"])[0]
    assert row["status"] == probe.OK
    assert row["detail"] == "@kaibatestbot"
    assert FAKE["TELEGRAM_BOT_TOKEN"] not in json.dumps(row)


def test_the_solana_rpc_is_probed_with_gethealth(fake_creds, http, no_gmgn):
    http["by_provider"]["rpc"] = ok({"jsonrpc": "2.0", "result": "ok"})
    row = probe.run_probe(["solana-rpc"])[0]
    assert row["status"] == probe.OK
    assert http["calls"][0]["json_body"]["method"] == "getHealth"


def test_coingecko_without_a_key_is_not_a_failure(fake_creds, http, monkeypatch, no_gmgn):
    monkeypatch.setenv("COINGECKO_API_KEY", "")
    get_settings.cache_clear()
    http["by_provider"]["coingecko"] = ok({"gecko_says": "(V3) To the Moon!"})
    row = probe.run_probe(["coingecko"])[0]
    assert row["status"] == probe.OK
    assert "keyless" in row["detail"]
    assert row["credential_optional"] is True


# ------------------------------------------------------------------------- safety to run


def test_a_checker_that_explodes_becomes_a_row_not_a_traceback(fake_creds, http):
    def boom(settings, timeout_s):
        raise RuntimeError("provider module went up in smoke")

    target = probe.Target("wobbly", "HELIUS_API_KEY", "whatever", boom)
    import unittest.mock as mock

    with mock.patch.object(probe, "TARGETS", (target,)):
        row = probe.run_probe()[0]
    assert row["status"] == probe.UNREACHABLE
    assert "RuntimeError" in row["detail"]


def test_probing_writes_no_trading_state(fake_creds, http, no_gmgn):
    http["default"] = ok({"ok": True, "result": "ok"})
    probe.run_probe()
    for table in ("orders", "positions", "decisions", "signals", "trades"):
        row = fake_creds.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        assert row["n"] == 0, f"probe wrote to {table}"


def test_the_cli_call_site_keeps_working(fake_creds, http, no_gmgn):
    """``kaiba probe`` calls ``run_probe()`` with no arguments and prints these keys."""
    import inspect

    signature = inspect.signature(probe.run_probe)
    assert all(p.default is not inspect.Parameter.empty for p in signature.parameters.values())

    http["default"] = ok({"ok": True, "result": "ok"})
    results = probe.run_probe()
    assert len(results) == len(probe.TARGETS)
    json.dumps(results, default=str)  # the CLI does exactly this
    for row in results:
        assert isinstance(row["reachable"], bool)
        assert isinstance(row.get("detail", ""), str)


def test_a_subset_can_be_probed(fake_creds, http, no_gmgn):
    results = probe.run_probe(["helius", "telegram"])
    assert [r["provider"] for r in results] == ["helius", "telegram"]


@pytest.mark.live
def test_live_probe_runs_against_the_real_world():
    """Only with KAIBA_LIVE_TESTS=1. Reads the cheapest endpoint of each configured provider."""
    results = probe.run_probe()
    assert results
    settings = get_settings()
    for row in results:
        assert row["status"] in {
            probe.OK, probe.AUTH_ERROR, probe.RATE_LIMITED, probe.MISSING_CREDENTIAL,
            probe.UNREACHABLE, probe.NOT_BUILT, probe.BUDGET_EXHAUSTED, probe.UNKNOWN,
        }
    blob = json.dumps(results, default=str)
    for value in probe.secret_values(settings):
        assert value not in blob
