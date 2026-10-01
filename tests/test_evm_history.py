"""GRADE-6b provider boundary tests. No swaps, grades or live HTTP in fixtures."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.providers import evm_history as eh
from kaiba.providers._http import Fetched, cache_path

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
TOKEN = "0x" + "c" * 40
SECRET = "fixture-credential-must-never-be-in-receipts"


def row(n=1, **kw):
    return dict(hash="0x" + f"{n:064x}", blockNumber=str(n), timeStamp="1790000000",
                value="123456789012345678901234567890", **{"from": WALLET, "to": OTHER},
                **kw)


def cursor(action="txlist", **kw):
    return eh.HistoryCursor(Chain.ROBINHOOD, WALLET, action, 100, **kw)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(eh, "get_settings", lambda: SimpleNamespace(etherscan_api_key=SECRET))


def reply(monkeypatch, body, *, basis=EvidenceBasis.PROVIDER_REPORTED):
    calls = []
    def fetch(*args, **kw):
        calls.append((args, kw))
        return Fetched(body, Receipt(provider="etherscan", endpoint="account.txlist",
                                    basis=basis, observed_at_ms=1234))
    monkeypatch.setattr(eh, "get_json", fetch)
    return calls


@pytest.mark.parametrize("chain,chainid", [(Chain.BSC, 56), (Chain.ROBINHOOD, 4663)])
@pytest.mark.parametrize("action", eh.ACTIONS)
def test_routes_exact_amounts_failed_calls_and_receipts(monkeypatch, chain, chainid, action):
    payload = row(contractAddress=TOKEN, tokenDecimal="18", isError="1")
    calls = reply(monkeypatch, {"status": "1", "result": [payload]})
    result = eh.history_page(replace(cursor(action), chain=chain, address=WALLET.upper().replace("0X", "0x")))
    assert result.ok and result.data.exhausted
    assert result.data.rows[0]["value"] == payload["value"]
    assert result.data.rows[0]["isError"] == "1"  # ingest must interpret it
    assert result.receipt.observed_at_ms == 1234
    args, kw = calls[0]
    assert args == ("etherscan", "account." + action, eh.BASE)
    assert kw["params"]["chainid"] == chainid and kw["params"]["action"] == action
    assert kw["params"]["sort"] == "asc" and kw["params"]["endblock"] == 100
    assert kw["params"]["address"] == WALLET
    assert kw["priority"] == Priority.RESEARCH and kw["retries"] == 1
    assert SECRET not in kw["cache_key"] and SECRET not in result.receipt.model_dump_json()


@pytest.mark.parametrize("body", [
    {"status": "0", "message": "No transactions found", "result": []},
    {"status": "1", "message": "OK", "result": []},
])
def test_explicit_empty(monkeypatch, body):
    reply(monkeypatch, body)
    result = eh.history_page(cursor())
    assert result.ok and result.data.rows == () and result.data.exhausted


@pytest.mark.parametrize("body,reason", [
    ({"status": "0", "message": "NOTOK", "result": "Free API access not supported; paid plan"}, "plan_restricted"),
    ({"status": "0", "result": "Max rate limit reached"}, "rate_limited"),
    ({"status": "0", "result": "Invalid API Key " + SECRET}, "credential_rejected"),
    ({"status": "0", "message": "NOTOK", "result": []}, "provider_error"),
    ({"status": "1", "result": "bad"}, "invalid_rows"),
    ({"status": "1", "result": [{}]}, "invalid_history_row"),
    ([], "invalid_envelope"),
])
def test_http_200_errors_never_become_empty_and_cache_evicted(monkeypatch, body, reason):
    calls = reply(monkeypatch, body)
    original = eh.get_json
    def fetch(*args, **kw):
        path = cache_path("etherscan", kw["cache_key"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return original(*args, **kw)
    monkeypatch.setattr(eh, "get_json", fetch)
    result = eh.history_page(cursor())
    assert not result.ok and result.data is None
    assert result.receipt.basis == EvidenceBasis.UNAVAILABLE and result.receipt.note == reason
    assert SECRET not in result.receipt.model_dump_json()
    assert not cache_path("etherscan", calls[0][1]["cache_key"]).exists()


@pytest.mark.parametrize("basis", [EvidenceBasis.UNAVAILABLE, EvidenceBasis.STALE])
def test_transport_or_stale_never_exhausts(monkeypatch, basis):
    reply(monkeypatch, {"status": "1", "result": []}, basis=basis)
    assert not eh.history_page(cursor()).ok


@pytest.mark.parametrize("changes", [
    {"value": -1}, {"value": 1.5}, {"value": "-1"},
    {"blockNumber": "101"}, {"hash": "bad"}, {"from": OTHER, "to": TOKEN},
    {"blockNumber": "1" * 5000},
])
def test_corrupt_or_foreign_rows_fail_whole_page(monkeypatch, changes):
    payload = row()
    payload.update(changes)
    reply(monkeypatch, {"status": "1", "result": [row(), payload]})
    result = eh.history_page(cursor())
    assert not result.ok and result.data is None


@pytest.mark.parametrize("changes", [
    {"chain": Chain.SOL}, {"address": "bad"}, {"action": "balance"},
    {"page": 0}, {"page": True}, {"page_size": 1001}, {"start_block": 101},
])
def test_invalid_query_no_transport(monkeypatch, changes):
    calls = reply(monkeypatch, {})
    assert not eh.history_page(replace(cursor(), **changes)).ok
    assert not calls


def test_missing_key_no_transport(monkeypatch):
    monkeypatch.setattr(eh, "get_settings", lambda: SimpleNamespace(etherscan_api_key=""))
    calls = reply(monkeypatch, {})
    assert eh.history_page(cursor()).receipt.note == "credential_missing"
    assert calls == []


def test_multiple_token_events_same_tx_are_preserved(monkeypatch):
    rows = [row(contractAddress=TOKEN, tokenDecimal="18", logIndex=str(n)) for n in (4, 5)]
    reply(monkeypatch, {"status": "1", "result": rows})
    result = eh.history_page(cursor("tokentx", page_size=2))
    assert len(result.data.rows) == 2 and not result.data.exhausted
    assert result.data.next_cursor.page == 2


def test_repeated_page_is_not_progress(monkeypatch):
    reply(monkeypatch, {"status": "1", "result": [row()]})
    first = eh.history_page(cursor(page_size=1))
    second = eh.history_page(first.data.next_cursor)
    assert not second.ok and second.receipt.note == "repeated_page"


def test_confirmation_changes_do_not_hide_repeated_page(monkeypatch):
    reply(monkeypatch, {"status": "1", "result": [row(confirmations="1")]})
    first = eh.history_page(cursor(page_size=1))
    reply(monkeypatch, {"status": "1", "result": [row(confirmations="2")]})
    second = eh.history_page(first.data.next_cursor)
    assert not second.ok and second.receipt.note == "repeated_page"


def test_out_of_order_rows_rejected(monkeypatch):
    reply(monkeypatch, {"status": "1", "result": [row(2), row(1)]})
    assert eh.history_page(cursor()).receipt.note == "rows_out_of_order"


def test_cache_dimensions_are_distinct(monkeypatch):
    calls = reply(monkeypatch, {"status": "1", "result": []})
    for c in [cursor(), replace(cursor(), chain=Chain.BSC), replace(cursor(), page=2),
              replace(cursor(), end_block=99), replace(cursor(), address=OTHER), cursor("tokentx")]:
        eh.history_page(c)
    assert len({kw["cache_key"] for _, kw in calls}) == 6


def test_budget_partial_resume_and_index_completion(monkeypatch):
    calls = []
    def fetch(*args, **kw):
        calls.append(kw["params"])
        rows = [row(contractAddress=TOKEN)] if kw["params"]["page"] == 1 else []
        return Fetched({"status": "1", "result": rows}, Receipt(provider="etherscan", endpoint=args[1]))
    monkeypatch.setattr(eh, "get_json", fetch)
    first = eh.wallet_history(Chain.BSC, WALLET, end_block=100, page_size=1, max_requests=3)
    assert first.requests_attempted == 3 and not first.complete
    assert all(c.page == 2 for c in first.cursors.values())
    second = eh.wallet_history(Chain.BSC, WALLET, end_block=100, page_size=1,
                               cursors=first.cursors, max_requests=3)
    assert second.complete and second.requests_attempted == 3
    assert all(c is None for c in second.cursors.values())
    assert len(calls) == 6


def test_failure_preserves_cursor_and_stops_batch(monkeypatch):
    reply(monkeypatch, {"status": "0", "result": "paid plan required"})
    result = eh.wallet_history(Chain.BSC, WALLET, end_block=100)
    assert result.requests_attempted == 1 and not result.complete
    assert result.errors == {"txlist": "plan_restricted"}
    assert all(c.page == 1 for c in result.cursors.values())


def test_resume_rejects_other_wallet_before_transport(monkeypatch):
    calls = reply(monkeypatch, {})
    pending = {a: replace(cursor(a), address=OTHER) for a in eh.ACTIONS}
    with pytest.raises(ValueError, match="does not belong"):
        eh.wallet_history(Chain.ROBINHOOD, WALLET, end_block=100, cursors=pending)
    assert calls == []


def test_zero_budget_does_not_claim_complete(monkeypatch):
    calls = reply(monkeypatch, {})
    result = eh.wallet_history(Chain.ROBINHOOD, WALLET, end_block=100, max_requests=0)
    assert result.requests_attempted == 0 and not result.complete and not calls


def test_shared_http_cache_and_transport_count(tmp_db, monkeypatch):
    import httpx
    calls = []
    def transport(method, url, **kw):
        calls.append(kw)
        return httpx.Response(200, json={"status": "1", "result": [row()]},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, "request", transport)
    first = eh.history_page(cursor(), conn=tmp_db)
    second = eh.history_page(cursor(), conn=tmp_db)
    assert first.ok and second.ok and len(calls) == 1
    assert first.receipt.basis == EvidenceBasis.PROVIDER_REPORTED
    assert second.receipt.basis == EvidenceBasis.CACHED
    # Shared HTTP writes its cache stamp before constructing the live Receipt.
    assert second.receipt.observed_at_ms <= first.receipt.observed_at_ms
    third = eh.history_page(cursor(), conn=tmp_db)
    assert third.receipt.observed_at_ms == second.receipt.observed_at_ms
    assert first.data.rows == second.data.rows


def test_shared_http_rate_limit_never_empty(tmp_db, monkeypatch):
    import httpx
    calls = []
    def transport(method, url, **kw):
        calls.append(kw)
        return httpx.Response(429, headers={"retry-after": "60"},
                              request=httpx.Request(method, url))
    monkeypatch.setattr(httpx, "request", transport)
    result = eh.wallet_history(Chain.ROBINHOOD, WALLET, end_block=100, conn=tmp_db)
    assert not result.complete and result.errors and result.requests_attempted == 1
    assert len(calls) == 1 and result.pages[0].data is None


def test_success_before_failure_remains_partial_and_resumable(monkeypatch):
    count = 0
    def fetch(*args, **kw):
        nonlocal count
        count += 1
        payload = {"status": "1", "result": []} if count == 1 else {
            "status": "0", "result": "Max rate limit reached"}
        return Fetched(payload, Receipt(provider="etherscan", endpoint=args[1]))
    monkeypatch.setattr(eh, "get_json", fetch)
    result = eh.wallet_history(Chain.ROBINHOOD, WALLET, end_block=100)
    assert result.cursors["txlist"] is None
    assert result.cursors["txlistinternal"].page == 1
    assert result.cursors["tokentx"].page == 1
    assert result.errors == {"txlistinternal": "rate_limited"} and not result.complete
