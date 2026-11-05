"""Helius adapter: credit accounting, graceful absence, and webhook authentication.

Offline. Every provider response is replayed from ``tests/fixtures/helius/*.json`` through
a stub that stands in for ``kaiba.providers._http``; the one test that exercises the real
HTTP layer uses ``respx`` so that no socket is opened either. The only test that talks to
Helius is marked ``live`` and is skipped unless ``KAIBA_LIVE_TESTS=1``.

Three properties are load-bearing and each has a test that fails loudly if it regresses:
an absent ``HELIUS_API_KEY`` degrades to ``UNAVAILABLE`` instead of raising, an exhausted
monthly credit budget refuses to call at all rather than sliding into overage, and a
webhook payload without a valid HMAC is never parsed for trading signal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.config import get_settings
from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import EventKind, EvidenceBasis, Receipt, now_ms
from kaiba.providers import helius
from kaiba.providers._http import Fetched

FIXTURES = Path(__file__).parent / "fixtures" / "helius"

WALLET = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
OTHER_WALLET = "GDfnEsia2WLAW5t8yx2X5j2mkfA74i5kwGdDuZHt7XmG"
MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

API_KEY = "hel-fake-api-key-8f21c0b4"
WEBHOOK_SECRET = "hel-fake-webhook-secret-11ffee"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def ok(data: Any, endpoint: str = "test") -> Fetched:
    return Fetched(data, Receipt(provider="helius", endpoint=endpoint,
                                 basis=EvidenceBasis.PROVIDER_REPORTED))


def cached(data: Any, endpoint: str = "test") -> Fetched:
    return Fetched(data, Receipt(provider="helius", endpoint=endpoint, basis=EvidenceBasis.CACHED))


def down(note: str = "ConnectError: connection refused", endpoint: str = "test") -> Fetched:
    return Fetched(None, Receipt(provider="helius", endpoint=endpoint,
                                 basis=EvidenceBasis.UNAVAILABLE, note=note))


@pytest.fixture
def creds(tmp_db, monkeypatch):
    """A configured Helius, on a throwaway database. Ordering matters: after ``tmp_db``."""
    monkeypatch.setenv("HELIUS_API_KEY", API_KEY)
    monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    yield tmp_db
    get_settings.cache_clear()


@pytest.fixture
def no_creds(tmp_db, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "")
    monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    yield tmp_db
    get_settings.cache_clear()


@pytest.fixture
def http(monkeypatch):
    """Stand in for the shared HTTP layer; queue responses, inspect the calls made."""
    state: dict[str, Any] = {"queue": [], "calls": []}

    def fake(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        state["calls"].append({"provider": provider, "endpoint": endpoint, "url": url, **kw})
        if state["queue"]:
            return state["queue"].pop(0)
        return down("no stubbed response", endpoint)

    monkeypatch.setattr(helius, "post_json", fake)
    monkeypatch.setattr(helius, "get_json", fake)
    return state


def track(conn, address: str, cohort: str = "tracked") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, source, first_seen_ms, last_seen_ms, cohort) "
        "VALUES ('sol', ?, 'test', ?, ?, ?)",
        (address, now_ms(), now_ms(), cohort),
    )


def ledger(conn, route: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM provider_budget WHERE provider='helius'"
    params: tuple[Any, ...] = ()
    if route:
        sql += " AND route=?"
        params = (route,)
    return fetch_all(conn, sql, params)


def events_of(conn, kind: EventKind) -> list[dict[str, Any]]:
    return fetch_all(conn, "SELECT * FROM events WHERE kind=? ORDER BY id", (kind.value,))


def sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------- credit cost metering


@pytest.mark.parametrize(
    ("returned", "details", "expected"),
    [
        (0, "full", 10),
        (1, "full", 10),
        (100, "full", 10),
        (101, "full", 20),
        (250, "full", 30),
        (1000, "full", 100),
        (1000, "signatures", 10),
    ],
)
def test_transaction_credits_follow_the_documented_metering(returned, details, expected):
    """10 credits per 100 returned, rounded up, 10 minimum; signatures flat."""
    assert helius.transaction_credits(returned, details) == expected


def test_every_route_declares_whether_its_cost_was_verified():
    for route, cost in helius.CREDIT_COSTS.items():
        assert cost.credits > 0, route
        assert cost.source, route
        assert isinstance(cost.verified, bool), route
    assert helius.CREDIT_COSTS[helius.ROUTE_ENHANCED].verified is False
    assert "ASSUMED" in helius.CREDIT_COSTS[helius.ROUTE_ENHANCED].source


def test_period_reset_is_the_start_of_the_next_utc_month():
    from datetime import UTC, datetime

    assert helius.period_reset_ms("2026-09") == int(datetime(2026, 10, 1, tzinfo=UTC).timestamp() * 1000)
    assert helius.period_reset_ms("2026-12") == int(datetime(2027, 1, 1, tzinfo=UTC).timestamp() * 1000)


def test_monthly_allowance_defaults_to_the_free_tier(creds, monkeypatch):
    monkeypatch.setattr(helius, "get_risk", lambda: type("R", (), {"provider_budgets": {}})())
    assert helius.monthly_allowance() == helius.FREE_MONTHLY_CREDITS


def test_monthly_allowance_honours_the_risk_config_override(creds, monkeypatch):
    cfg = type("R", (), {"provider_budgets": {"helius": {"monthly_credit_cap": 5000}}})()
    monkeypatch.setattr(helius, "get_risk", lambda: cfg)
    assert helius.monthly_allowance() == 5000


def test_record_spend_accumulates_per_route_and_period(creds):
    helius.record_spend(helius.ROUTE_HEALTH, 1, conn=creds)
    helius.record_spend(helius.ROUTE_HEALTH, 1, conn=creds)
    helius.record_spend(helius.ROUTE_TOKEN_ACCOUNTS, 10, conn=creds)
    rows = {r["route"]: r for r in ledger(creds)}
    assert rows[helius.ROUTE_HEALTH]["calls"] == 2
    assert rows[helius.ROUTE_HEALTH]["credits"] == 2
    assert rows[helius.ROUTE_TOKEN_ACCOUNTS]["credits"] == 10
    assert helius.budget_status(conn=creds)["used"] == 12


def test_budget_status_reports_remaining_and_the_reset_boundary(creds):
    helius.record_spend(helius.ROUTE_TRANSACTIONS, 40, conn=creds)
    st = helius.budget_status(conn=creds)
    assert st["allowance"] == helius.FREE_MONTHLY_CREDITS
    assert st["used"] == 40
    assert st["remaining"] == helius.FREE_MONTHLY_CREDITS - 40
    assert st["exhausted"] is False
    assert st["resets_at_ms"] == helius.period_reset_ms(st["period"])
    assert st["resets_in_s"] > 0
    assert st["by_route"][0]["cost_verified"] is True


def test_budget_status_separates_guessed_credits_from_verified_ones(creds):
    helius.record_spend(helius.ROUTE_TRANSACTIONS, 10, conn=creds)
    helius.record_spend(helius.ROUTE_ENHANCED, 100, conn=creds)
    st = helius.budget_status(conn=creds)
    assert st["used"] == 110
    assert st["estimated_credits"] == 100
    assert st["estimated_pct_of_used"] > 90
    enhanced = next(r for r in st["by_route"] if r["route"] == helius.ROUTE_ENHANCED)
    assert enhanced["cost_verified"] is False


def test_spend_in_a_previous_period_does_not_count_against_this_month(creds):
    old = now_ms() - 70 * 86_400_000
    helius.record_spend(helius.ROUTE_TRANSACTIONS, 900_000, conn=creds, ts_ms=old)
    assert helius.budget_status(conn=creds)["used"] == 0


# ------------------------------------------------------------------- no credential at all


def test_reads_without_an_api_key_are_unavailable_not_exceptions(no_creds, http):
    calls = [
        helius.ping(conn=no_creds),
        helius.get_transactions_for_address(WALLET, conn=no_creds),
        helius.get_enhanced_transactions(WALLET, conn=no_creds),
        helius.get_token_accounts_by_mint(MINT, conn=no_creds),
        helius.get_priority_fee_estimate(account_keys=[WALLET], conn=no_creds),
    ]
    for data, receipt in calls:
        assert data is None
        assert receipt.basis is EvidenceBasis.UNAVAILABLE
        assert "HELIUS_API_KEY" in (receipt.note or "")
    assert http["calls"] == [], "a missing credential must not produce a request"


def test_missing_credential_is_reported_on_the_event_bus(no_creds, http):
    helius.ping(conn=no_creds)
    errors = events_of(no_creds, EventKind.PROVIDER_ERROR)
    assert errors, "an unusable provider should be visible to the operator"
    assert "HELIUS_API_KEY" in errors[0]["payload"]


def test_available_reflects_the_credential_without_calling(no_creds, http):
    assert helius.available() is False
    assert http["calls"] == []


# ------------------------------------------------------------------------------ the reads


def test_get_transactions_for_address_returns_the_page_and_charges_by_count(creds, http):
    http["queue"].append(ok(fixture("transactions_full")))
    data, receipt = helius.get_transactions_for_address(WALLET, limit=100, conn=creds)

    assert data is not None
    assert len(data["data"]) == 2
    assert data["paginationToken"]
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    call = http["calls"][0]
    assert call["json_body"]["method"] == "getTransactionsForAddress"
    # Verified live 2026-09-20: positional address string, then a config object. Sending a
    # single object is rejected with "invalid type: map, expected a string".
    assert call["json_body"]["params"][0] == WALLET
    assert call["json_body"]["params"][1]["limit"] == 100
    assert call["params"] == {"api-key": API_KEY}
    assert ledger(creds, helius.ROUTE_TRANSACTIONS)[0]["credits"] == 10


def test_signatures_only_history_is_charged_flat(creds, http):
    http["queue"].append(ok(fixture("transactions_signatures")))
    data, _ = helius.get_transactions_for_address(
        WALLET, limit=1000, transaction_details="signatures", conn=creds
    )
    assert data["data"][0]["confirmationStatus"] == "finalized"
    assert ledger(creds, helius.ROUTE_TRANSACTIONS)[0]["credits"] == 10


def test_a_second_page_is_requested_with_the_pagination_token(creds, http):
    http["queue"].extend([ok(fixture("transactions_full")), ok(fixture("transactions_page2"))])
    first, _ = helius.get_transactions_for_address(WALLET, conn=creds)
    second, _ = helius.get_transactions_for_address(
        WALLET, pagination_token=first["paginationToken"], conn=creds
    )
    assert second["paginationToken"] is None
    assert http["calls"][1]["json_body"]["params"][1]["paginationToken"] == first["paginationToken"]
    assert ledger(creds, helius.ROUTE_TRANSACTIONS)[0]["calls"] == 2


def test_a_bad_address_is_unavailable_rather_than_a_crash(creds, http):
    data, receipt = helius.get_transactions_for_address("not-an-address", conn=creds)
    assert data is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "invalid solana address" in (receipt.note or "")
    assert http["calls"] == []


def test_a_cache_hit_costs_no_credits(creds, http):
    http["queue"].append(cached(fixture("transactions_full")))
    data, receipt = helius.get_transactions_for_address(WALLET, conn=creds)
    assert data is not None
    assert receipt.basis is EvidenceBasis.CACHED
    assert ledger(creds, helius.ROUTE_TRANSACTIONS) == []


def test_token_accounts_by_mint_charges_ten_per_page(creds, http):
    http["queue"].append(ok(fixture("token_accounts_page1")))
    data, _ = helius.get_token_accounts_by_mint(MINT, limit=2, conn=creds)
    assert [a["owner"] for a in data["token_accounts"]] == [WALLET, OTHER_WALLET]
    assert http["calls"][0]["json_body"]["params"]["mint"] == MINT
    assert ledger(creds, helius.ROUTE_TOKEN_ACCOUNTS)[0]["credits"] == 10


def test_token_account_pagination_walks_pages_and_pays_for_each(creds, http):
    http["queue"].extend([ok(fixture("token_accounts_page1")), ok(fixture("token_accounts_page2"))])
    accounts, receipt = helius.iter_token_accounts_by_mint(MINT, limit=2, max_pages=5, conn=creds)
    assert len(accounts) == 3
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    row = ledger(creds, helius.ROUTE_TOKEN_ACCOUNTS)[0]
    assert (row["calls"], row["credits"]) == (2, 20)
    assert http["calls"][1]["json_body"]["params"]["cursor"] == "Y3Vyc29yOjI"


def test_a_truncated_holder_walk_says_so_in_the_receipt(creds, http):
    http["queue"].extend([ok(fixture("token_accounts_page1"))] * 2)
    accounts, receipt = helius.iter_token_accounts_by_mint(MINT, limit=2, max_pages=2, conn=creds)
    assert len(accounts) == 4
    assert "partial" in (receipt.note or "")


def test_priority_fee_estimate_returns_levels_and_costs_one_credit(creds, http):
    http["queue"].append(ok(fixture("priority_fee_estimate")))
    data, _ = helius.get_priority_fee_estimate(
        account_keys=[MINT], include_all_levels=True, conn=creds
    )
    assert data["priorityFeeEstimate"] == 25431.0
    assert data["priorityFeeLevels"]["veryHigh"] == 180000.0
    body = http["calls"][0]["json_body"]["params"][0]
    assert body["accountKeys"] == [MINT]
    assert body["options"]["includeAllPriorityFeeLevels"] is True
    assert ledger(creds, helius.ROUTE_PRIORITY_FEE)[0]["credits"] == 1


def test_priority_fee_needs_something_to_estimate_against(creds, http):
    data, receipt = helius.get_priority_fee_estimate(conn=creds)
    assert data is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert http["calls"] == []


def test_an_rpc_error_body_is_unavailable_and_charged_conservatively(creds, http):
    """Charged, because we may well have been billed — but into the uncertainty row."""
    http["queue"].append(ok(fixture("rpc_error")))
    data, receipt = helius.get_token_accounts_by_mint(MINT, conn=creds)
    assert data is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "could not find mint" in (receipt.note or "")
    assert ledger(creds, helius.ROUTE_TOKEN_ACCOUNTS) == [], "the verified row stays clean"
    row = ledger(creds, helius.ROUTE_TOKEN_ACCOUNTS + helius.ERROR_SUFFIX)[0]
    assert row["credits"] == 10 and row["estimated"] == 1


def test_uncertain_charges_do_not_taint_a_month_of_verified_spend(creds, http):
    http["queue"].extend([ok(fixture("token_accounts_page1")), ok(fixture("rpc_error"))])
    helius.get_token_accounts_by_mint(MINT, limit=2, conn=creds)
    helius.get_token_accounts_by_mint(MINT, limit=3, conn=creds)
    st = helius.budget_status(conn=creds)
    assert st["used"] == 20
    assert st["estimated_credits"] == 10, "only the error charge is a guess"
    uncertain = next(r for r in st["by_route"] if r["route"].endswith(helius.ERROR_SUFFIX))
    assert uncertain["cost_verified"] is False
    assert "ASSUMED" in uncertain["cost_source"]


def test_a_dead_provider_returns_unavailable_and_costs_nothing(creds, http):
    http["queue"].append(down("ConnectTimeout: timed out"))
    data, receipt = helius.get_transactions_for_address(WALLET, conn=creds)
    assert (data, receipt.basis) == (None, EvidenceBasis.UNAVAILABLE)
    assert ledger(creds, helius.ROUTE_TRANSACTIONS) == []


def test_enhanced_history_is_recorded_as_an_estimated_cost(creds, http):
    http["queue"].append(ok(fixture("enhanced_transactions")))
    data, _ = helius.get_enhanced_transactions(WALLET, limit=10, conn=creds)
    assert data[0]["type"] == "SWAP"
    assert http["calls"][0]["url"].endswith(f"/addresses/{WALLET}/transactions")
    row = ledger(creds, helius.ROUTE_ENHANCED)[0]
    assert (row["credits"], row["estimated"]) == (100, 1)


def test_the_api_key_never_survives_into_a_receipt(creds, http):
    leaked = f"HTTPStatusError: 401 for url 'https://mainnet.helius-rpc.com/?api-key={API_KEY}'"
    http["queue"].append(down(leaked))
    _, receipt = helius.get_transactions_for_address(WALLET, conn=creds)
    assert API_KEY not in (receipt.note or "")
    assert "<redacted>" in (receipt.note or "")


# --------------------------------------------------------------------------- budget gate


def test_an_exhausted_budget_refuses_to_call_at_all(creds, http, monkeypatch):
    cfg = type("R", (), {"provider_budgets": {"helius": {"monthly_credit_cap": 20}}})()
    monkeypatch.setattr(helius, "get_risk", lambda: cfg)
    helius.record_spend(helius.ROUTE_TRANSACTIONS, 20, conn=creds)

    data, receipt = helius.get_transactions_for_address(WALLET, conn=creds)

    assert data is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE
    assert "budget exhausted" in (receipt.note or "")
    assert http["calls"] == [], "an exhausted budget must not burn into overage"
    assert ledger(creds, helius.ROUTE_TRANSACTIONS)[0]["denied"] == 1
    budget_events = events_of(creds, EventKind.PROVIDER_BUDGET)
    assert budget_events and "exhausted" in json.dumps(budget_events[-1]["payload"])


def test_the_gate_reserves_the_worst_case_for_the_page(creds, http, monkeypatch):
    """A 1000-row page can cost 100 credits, so 50 left is not enough to start it."""
    cfg = type("R", (), {"provider_budgets": {"helius": {"monthly_credit_cap": 50}}})()
    monkeypatch.setattr(helius, "get_risk", lambda: cfg)
    data, _ = helius.get_transactions_for_address(WALLET, limit=1000, conn=creds)
    assert data is None and http["calls"] == []


def test_the_operator_is_warned_before_the_allowance_runs_out(creds, http, monkeypatch):
    cfg = type("R", (), {"provider_budgets": {"helius": {"monthly_credit_cap": 100}}})()
    monkeypatch.setattr(helius, "get_risk", lambda: cfg)
    helius.record_spend(helius.ROUTE_HEALTH, 85, conn=creds)
    http["queue"].append(ok({"jsonrpc": "2.0", "result": "ok"}))
    helius.ping(conn=creds)
    warnings = events_of(creds, EventKind.PROVIDER_BUDGET)
    assert any(e["payload"] and "threshold_pct" in e["payload"] for e in warnings)


# ------------------------------------------------------------------------------- webhook


@pytest.mark.parametrize("encode", ["hex", "prefixed", "base64"])
def test_a_correctly_signed_delivery_is_accepted(creds, encode):
    body = json.dumps(fixture("webhook_swap")).encode()
    raw = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256)
    signature = {
        "hex": raw.hexdigest(),
        "prefixed": f"sha256={raw.hexdigest()}",
        "base64": base64.b64encode(raw.digest()).decode(),
    }[encode]
    assert helius.verify_signature(body, signature=signature) is True


def test_the_static_authorization_header_helius_actually_sends_is_accepted(creds):
    body = b"{}"
    assert helius.verify_signature(body, signature=None, authorization=WEBHOOK_SECRET) is True
    assert helius.verify_signature(body, signature=None, authorization=f"Bearer {WEBHOOK_SECRET}") is True


def test_a_near_miss_signature_is_rejected(creds):
    body = b'[{"type":"SWAP"}]'
    good = sign(body)
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert len(bad) == len(good)
    assert helius.verify_signature(body, signature=bad) is False


def test_verification_fails_closed_without_a_configured_secret(no_creds):
    body = b'[{"type":"SWAP"}]'
    assert helius.verify_signature(body, signature=sign(body)) is False
    status, response = helius.handle_webhook(body, {"x-helius-signature": sign(body)}, conn=no_creds)
    assert status == 401
    assert response["ok"] is False


def test_an_unsigned_payload_is_rejected_and_recorded(creds):
    track(creds, WALLET)
    body = json.dumps(fixture("webhook_swap")).encode()
    status, response = helius.handle_webhook(body, {}, conn=creds)
    assert status == 401
    assert response["error"] == "signature verification failed"
    assert events_of(creds, EventKind.WALLET_TRADE) == []
    errors = events_of(creds, EventKind.PROVIDER_ERROR)
    assert errors and "verification failed" in errors[-1]["payload"]


def test_a_forged_payload_never_reaches_the_parser(creds):
    track(creds, WALLET)
    body = json.dumps(fixture("webhook_swap")).encode()
    status, _ = helius.handle_webhook(
        body, {"x-helius-signature": sign(body, "the-wrong-secret")}, conn=creds
    )
    assert status == 401
    assert events_of(creds, EventKind.WALLET_TRADE) == []
    assert ledger(creds, helius.ROUTE_WEBHOOK) == []


def test_a_swap_on_a_tracked_wallet_reaches_the_event_bus(creds):
    track(creds, WALLET)
    payload = fixture("webhook_swap")
    body = json.dumps(payload).encode()
    status, response = helius.handle_webhook(body, {"x-helius-signature": sign(body)}, conn=creds)

    assert status == 200
    assert response["emitted"] == 1
    assert response["matched_wallets"] == [WALLET]
    trades = events_of(creds, EventKind.WALLET_TRADE)
    assert len(trades) == 1
    event = json.loads(trades[0]["payload"])
    assert event["type"] == "SWAP"
    assert event["source"] == "helius_webhook"
    assert event["token_transfers"][0]["mint"] == MINT
    assert trades[0]["subject"] == WALLET


def test_transfers_are_kept_and_other_types_are_ignored(creds):
    track(creds, WALLET)
    track(creds, OTHER_WALLET, cohort="trusted_copy")
    body = json.dumps(fixture("webhook_transfer")).encode()
    status, response = helius.handle_webhook(body, {"x-helius-signature": sign(body)}, conn=creds)

    assert status == 200
    assert response["received"] == 2, "both transactions were looked at"
    assert response["emitted"] == 2, "one TRANSFER touching two tracked wallets; the mint is ignored"
    kinds = {json.loads(e["payload"])["type"] for e in events_of(creds, EventKind.WALLET_TRADE)}
    assert kinds == {"TRANSFER"}


def test_a_swap_by_an_untracked_wallet_raises_nothing(creds):
    track(creds, "HN7cABqLq46Es1jh92dQQisAq662SmxELLLsHHe4YWrH")
    body = json.dumps(fixture("webhook_swap")).encode()
    status, response = helius.handle_webhook(body, {"x-helius-signature": sign(body)}, conn=creds)
    assert (status, response["emitted"]) == (200, 0)
    assert events_of(creds, EventKind.WALLET_TRADE) == []


def test_a_redelivered_webhook_does_not_double_count(creds):
    track(creds, WALLET)
    body = json.dumps(fixture("webhook_swap")).encode()
    headers = {"x-helius-signature": sign(body)}
    helius.handle_webhook(body, headers, conn=creds)
    _, second = helius.handle_webhook(body, headers, conn=creds)
    assert second["emitted"] == 0
    assert len(events_of(creds, EventKind.WALLET_TRADE)) == 1


def test_every_verified_push_is_charged_one_credit(creds):
    track(creds, WALLET)
    body = json.dumps(fixture("webhook_swap")).encode()
    headers = {"x-helius-signature": sign(body)}
    helius.handle_webhook(body, headers, conn=creds)
    helius.handle_webhook(body, headers, conn=creds)
    row = ledger(creds, helius.ROUTE_WEBHOOK)[0]
    assert (row["calls"], row["credits"]) == (2, 2)


def test_a_signed_but_unparseable_body_is_a_client_error(creds):
    body = b"{not json"
    status, response = helius.handle_webhook(body, {"x-helius-signature": sign(body)}, conn=creds)
    assert status == 400
    assert response["ok"] is False
    errors = events_of(creds, EventKind.PROVIDER_ERROR)
    assert errors and "unparseable" in errors[-1]["payload"]


def test_the_rejection_event_never_stores_the_payload(creds):
    body = json.dumps([{"type": "SWAP", "secretish": "do-not-store-me"}]).encode()
    helius.handle_webhook(body, {"x-helius-signature": "deadbeef"}, conn=creds)
    stored = json.dumps(events_of(creds, EventKind.PROVIDER_ERROR))
    assert "do-not-store-me" not in stored
    assert hashlib.sha256(body).hexdigest() in stored


def test_the_router_exists_for_mounting_but_mounts_nothing_itself(creds):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = helius.build_router()
    assert api is not None
    assert [r.path for r in api.routes] == ["/webhooks/helius"]

    track(creds, WALLET)
    app = FastAPI()
    app.include_router(api)
    client = TestClient(app)
    payload = fixture("webhook_swap")
    body = json.dumps(payload).encode()

    unsigned = client.post("/webhooks/helius", content=body)
    assert unsigned.status_code == 401

    signed = client.post(
        "/webhooks/helius", content=body, headers={"x-helius-signature": sign(body)}
    )
    assert signed.status_code == 200
    assert signed.json()["matched_wallets"] == [WALLET]


# --------------------------------------------------------------------- the real HTTP path


def test_the_real_http_layer_sends_the_key_as_a_query_parameter(creds):
    """Exercises _http + the limiter without a socket, so the URL shape is actually checked."""
    respx = pytest.importorskip("respx")
    import httpx

    with respx.mock as mock:
        route = mock.post("https://mainnet.helius-rpc.com/").mock(
            return_value=httpx.Response(200, json=fixture("priority_fee_estimate"))
        )
        data, receipt = helius.get_priority_fee_estimate(account_keys=[MINT], conn=creds)

    assert data["priorityFeeEstimate"] == 25431.0
    assert receipt.basis is EvidenceBasis.PROVIDER_REPORTED
    request = route.calls[0].request
    assert request.url.params["api-key"] == API_KEY
    assert json.loads(request.content)["method"] == "getPriorityFeeEstimate"
    assert fetch_one(creds, "SELECT * FROM provider_calls WHERE provider='helius'") is not None
    assert ledger(creds, helius.ROUTE_PRIORITY_FEE)[0]["credits"] == 1


@pytest.mark.live
def test_live_helius_ping_and_budget():
    """Only runs with KAIBA_LIVE_TESTS=1 and a real key; spends 1 credit."""
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    if not helius.available():
        pytest.skip("HELIUS_API_KEY not configured")
    result, receipt = helius.ping(conn=conn)
    assert receipt.provider == "helius"
    assert result == "ok"
    assert helius.budget_status(conn=conn)["used"] >= 1
