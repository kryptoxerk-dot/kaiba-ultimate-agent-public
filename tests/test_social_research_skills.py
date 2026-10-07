"""Meaningful helper invariants; no live provider calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


x = load("social_x_reader", "skills/twitterapi-research/scripts/research.py")
j7 = load("social_j7_reader", "skills/j7-social-tracking/scripts/collect.py")


def test_budget_reserves_persist_across_calls(tmp_path):
    path = tmp_path / "budget.json"
    x.reserve(path, 300, now=1_790_000_000)
    assert x.reserve(path, 300, now=1_790_000_001)["lifetime_credits"] == 600
    assert not path.with_name("budget.json.lock").exists()


@pytest.mark.parametrize("field,value,reason", [
    ("daily_credits", 49999, "daily_budget_exhausted"),
    ("lifetime_credits", 1999999, "lifetime_budget_exhausted"),
])
def test_exhausted_budget_prevents_request(tmp_path, monkeypatch, field, value, reason):
    path = tmp_path / "budget.json"
    state = x.reserve(path, 15)
    state[field] = value
    path.write_text(json.dumps(state), encoding="utf-8")
    calls = []
    monkeypatch.setattr(x, "get", lambda *args: calls.append(args))
    with pytest.raises(x.Unavailable, match=reason):
        x.search("do-not-log", "from:cz_binance", path, tmp_path / "out.jsonl", 1)
    assert calls == []
    assert json.loads(path.read_text())[field] == value


def test_day_rollover_preserves_lifetime_budget(tmp_path):
    path = tmp_path / "budget.json"
    x.reserve(path, 300, now=1_790_000_000)
    state = x.reserve(path, 300, now=1_790_000_000 + 86400)
    assert state["daily_credits"] == 300
    assert state["lifetime_credits"] == 600


def test_ambiguous_request_keeps_reservation(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    def failed(*args):
        raise x.Unavailable("network_unavailable")
    monkeypatch.setattr(x, "get", failed)
    with pytest.raises(x.Unavailable):
        x.search("secret", "query", path, tmp_path / "posts.jsonl", 1)
    assert json.loads(path.read_text())["lifetime_credits"] == 300


def test_corrupt_or_locked_budget_never_resets(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("not json")
    with pytest.raises(x.Unavailable, match="invalid_budget_ledger"):
        x.reserve(path, 300)
    assert path.read_text() == "not json"
    lock = path.with_name(path.name + ".lock")
    lock.mkdir()
    with pytest.raises(x.Unavailable, match="budget_lock_busy"):
        x.reserve(path, 300)
    assert lock.exists()


def test_search_deduplicates_without_backdating_observation(tmp_path, monkeypatch):
    output = tmp_path / "posts.jsonl"
    monkeypatch.setattr(x.time, "time", lambda: 1_790_000_000)
    page = {"tweets": [{"id": "1", "text": "token", "createdAt": "2025-01-01T00:00:00Z",
                        "author": {"userName": "test"}, "api_key": "secret"}],
            "has_next_page": False, "next_cursor": ""}
    monkeypatch.setattr(x, "get", lambda *args: page)
    result = x.search("secret", "query", tmp_path / "ledger", output, 2)
    assert result["new_posts"] == 1
    row = json.loads(output.read_text())
    assert row["created_ms"] < row["first_seen_ms"]
    assert "secret" not in output.read_text()
    assert x.search("secret", "query", tmp_path / "ledger", output, 1)["new_posts"] == 0


def test_search_refuses_repeated_cursor_and_retains_partial_rows(tmp_path, monkeypatch):
    page = {"tweets": [{"id": "1"}], "has_next_page": True, "next_cursor": "same"}
    monkeypatch.setattr(x, "get", lambda *args: page)
    output = tmp_path / "posts.jsonl"
    with pytest.raises(x.Unavailable, match="repeated_cursor"):
        x.search("secret", "q", tmp_path / "ledger", output, 3)
    assert len(output.read_text().splitlines()) == 1


def test_no_redirect_or_arbitrary_endpoint_can_receive_credentials():
    with pytest.raises(x.Unavailable, match="endpoint_not_allowed"):
        x.get("/twitter/create_tweet", "secret")
    for module in (x, j7):
        with pytest.raises(module.Unavailable, match="redirect_refused"):
            module.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_budget_reservation_rejected(tmp_path, value):
    with pytest.raises(x.Unavailable, match="invalid_reservation"):
        x.reserve(tmp_path / "ledger", value)


def test_j7_enrichment_retains_first_seen_and_does_not_repeat_duplicates():
    posts = j7.Posts()
    initial = {"id": "x", "author": {"handle": "cz_binance"}, "text": "partial"}
    assert posts.upsert(initial, now_ms=100, backlog=True)["revision"] == 1
    assert posts.upsert(initial, now_ms=110) is None
    update = posts.upsert({"id": "x", "text": "full", "author": {"id": "42"}}, now_ms=120)
    assert update["first_seen_ms"] == 100
    assert update["observed_ms"] == 120
    assert update["author"] == "cz_binance"
    assert update["author_id"] == "42"
    assert update["is_backlog"] is True
    assert update["revision"] == 2


def test_j7_platform_ids_do_not_collide_and_html_is_decoded():
    posts = j7.Posts()
    posts.upsert({"id": "1", "text": "X"}, now_ms=1)
    row = posts.upsert({"id": "1", "platform": "INSTAGRAM", "text": "A &amp; B"}, now_ms=2)
    assert row["text"] == "A & B"
    assert row["first_seen_ms"] == 2
    assert len(posts.posts) == 2


def test_j7_roster_distinguishes_global_pool_from_delivered_accounts():
    data = {"success": True, "x": {"accounts": [{"handle": "main"}]},
            "custom": {"accounts": ["custom"], "availableAccounts": ["delivered"]},
            "available": {"accounts": ["notdelivered"]}, "hidden": ["main"],
            "session_id": "secret"}
    rows = j7.account_rows(data)
    assert {r["handle"]: r["j7_kind"] for r in rows} == {
        "main": "main_feed", "custom": "custom", "delivered": "user_available",
        "notdelivered": "available_pool"}
    assert rows[0]["hidden"] is True
    assert "secret" not in json.dumps(rows)


def test_credential_reuses_existing_dotenv_without_rewriting_it(tmp_path, monkeypatch):
    monkeypatch.delenv("TWITTERAPI_IO_KEY", raising=False)
    monkeypatch.setattr(x, "DEFAULT_ROOT", tmp_path)
    content = "UNRELATED=untouched\nexport TWITTERAPI_IO_KEY='fixture-key'\n"
    (tmp_path / ".env").write_text(content)
    assert x.credential() == "fixture-key"
    assert (tmp_path / ".env").read_text() == content
    with pytest.raises(x.Unavailable, match="missing_credential"):
        x.credential(tmp_path / "explicit-missing")
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "environment-fixture")
    assert x.credential() == "environment-fixture"
