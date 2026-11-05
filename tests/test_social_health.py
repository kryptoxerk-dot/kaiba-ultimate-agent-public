"""Delivery evidence and stable change-triggered alerts; never a provider or production DB."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kaiba.hunters import social_health as health
from kaiba.hunters.social_hunt import Store, classify


def make_post(store, *, at=100_000, ident="123456", text="NFT mint and airdrop claim"):
    event = {"tweet_id": ident, "author": "opensea", "created_ms": at, "received_ms": at,
             "kind": "post", "text": text}
    cfg = {"enabled": True, "accounts": {"nft": ["opensea"], "airdrop": []}}
    store.record(event, classify(event, cfg, now_ms=at), at)


def test_read_only_monitor_does_not_create_missing_store_or_budget(tmp_path):
    db, budget = tmp_path / "absent.db", tmp_path / "absent-budget.json"
    out = health.monitor_snapshot(db, enabled=True, budget=budget, now_ms=100_000)
    assert out["delivery"]["status"] == "store_missing"
    assert out["research_budget"]["status"] == "not_initialized"
    assert not db.exists() and not budget.exists()


def test_empty_store_is_not_healthy_and_poll_clock_does_not_trigger_again(tmp_path):
    store = Store(tmp_path / "inbox.db")
    before = store.path.read_bytes()
    first = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=100_000))
    later = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=200_000))
    assert first == later and first["delivery"]["status"] == "no_retained_receipt"
    assert store.path.read_bytes() == before
    assert not first["financial_execution"]


def test_health_recovers_on_delivery_and_becomes_stale_once(tmp_path):
    store = Store(tmp_path / "inbox.db")
    missing = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=100_000))
    make_post(store)
    recent = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=100_001, max_age_s=10))
    stale = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=110_001, max_age_s=10))
    again = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=120_001, max_age_s=10))
    assert missing != recent != stale and stale == again
    assert recent["delivery"]["status"] == "recent" and stale["delivery"]["status"] == "stale"
    assert recent["pending"] == [{"tweet_id": "123456", "lane": "airdrop"}, {"tweet_id": "123456", "lane": "nft"}]


def test_healthy_receipt_without_new_candidate_does_not_wake_model(tmp_path):
    store = Store(tmp_path / "inbox.db")
    make_post(store, text="An ordinary update")
    before = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=100_001))
    make_post(store, at=101_000, ident="123457", text="Another ordinary update")
    after = health.monitor_payload(health.monitor_snapshot(store.path, enabled=True, now_ms=101_001))
    assert before == after


def test_future_receipt_is_not_fresh_and_disabled_is_distinct(tmp_path):
    store = Store(tmp_path / "inbox.db")
    make_post(store, at=200_000)
    assert health.monitor_snapshot(store.path, enabled=True, now_ms=100_000)["delivery"]["status"] == "receipt_clock_invalid"
    assert health.monitor_snapshot(store.path, enabled=False, now_ms=100_000)["delivery"]["status"] == "disabled"


def test_completed_reviews_leave_pending_queue_and_limits_are_preserved(tmp_path):
    store = Store(tmp_path / "inbox.db")
    make_post(store)
    store.query("INSERT INTO reviews VALUES (?,?,?,?,?,?)", ("123456", "nft", 0, 100_001, "watch", ""))
    out = health.monitor_snapshot(store.path, enabled=True, now_ms=100_002)
    assert out["pending"] == [{"tweet_id": "123456", "lane": "airdrop"}]


def test_wal_budget_stops_before_opening_any_connection(tmp_path, monkeypatch):
    store = Store(tmp_path / "inbox.db")
    Path(str(store.path) + "-wal").write_bytes(b"test")
    monkeypatch.setattr(health, "MAX_WAL_BYTES", 1)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("opened DB over WAL budget"))
    out = health.monitor_snapshot(store.path, enabled=True, now_ms=100_000)
    assert out["delivery"]["status"] == "wal_budget_deferred"


def test_failed_read_closes_connection_and_does_not_return_false_empty(tmp_path, monkeypatch):
    path = tmp_path / "broken.db"
    path.write_bytes(b"fixture")
    closed = []
    class Broken:
        def execute(self, *args):
            if args[0].startswith("PRAGMA"):
                return self
            raise sqlite3.OperationalError("interrupted")
        def set_progress_handler(self, *args):
            pass
        def close(self):
            closed.append(True)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Broken())
    out = health.monitor_snapshot(path, enabled=True, now_ms=100_000)
    assert closed == [True]
    assert out["delivery"]["status"] == "store_read_unavailable"


def test_legacy_budget_lock_is_reported_without_being_reclaimed(tmp_path):
    ledger = tmp_path / "budget.json"
    ledger.write_text('{"version":1,"lifetime_credits":123,"daily_credits":12,"day":"2026-10-29"}')
    lock = ledger.with_name(ledger.name + ".lock")
    lock.mkdir()
    os.utime(lock, (1, 1))
    before = ledger.read_bytes()
    first = health.budget_health(ledger, now_ms=100_000)
    assert first == health.budget_health(ledger, now_ms=200_000)
    assert first["status"] == "persistent_lock_owner_unverified"
    assert lock.exists() and ledger.read_bytes() == before


@pytest.mark.parametrize("case,expected", [("bad", "ledger_unavailable"), ("shape", "ledger_invalid"),
    ("lifetime", "lifetime_limit_reached"), ("day", "daily_limit_reached"), ("ok", "within_limits")])
def test_budget_health_does_not_reset_or_leak_unknown_fields(tmp_path, case, expected):
    path = tmp_path / "budget.json"
    state = {"version": 1, "lifetime_credits": 300, "daily_credits": 300, "day": "1970-01-01", "secret": "never output"}
    if case == "lifetime":
        state["lifetime_credits"] = 2_000_000
    if case == "day":
        state["daily_credits"] = 50_000
    if case == "shape":
        state["lifetime_credits"] = True
    path.write_text("not JSON" if case == "bad" else json.dumps(state))
    before = path.read_bytes()
    out = health.budget_health(path, now_ms=100_000)
    assert out["status"] == expected and "never output" not in json.dumps(out)
    assert path.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="production receiver uses POSIX flock; exercised on Linux stage")
def test_actual_local_datagram_reaches_isolated_store_and_health(tmp_path):
    # No production socket, provider connection, live credentials or financial routes.
    sockpath = tmp_path / "hunt.sock"
    db = tmp_path / "hunt.db"
    config = tmp_path / "config.yaml"
    config.write_text(f"enabled: true\nsocket_path: {sockpath}\ndatabase: {db}\naccounts:\n  nft: [opensea]\n")
    proc = subprocess.Popen([sys.executable, "-m", "kaiba.hunters.social_hunt", "--config", str(config), "run"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while not sockpath.exists() and time.monotonic() < deadline:
            assert proc.poll() is None
            time.sleep(0.02)
        assert sockpath.exists()
        now = int(time.time() * 1000)
        event = {"tweet_id": "123456", "author": "opensea", "created_ms": now, "received_ms": now,
                 "kind": "post", "text": "NFT mint and airdrop eligibility"}
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps(event).encode(), str(sockpath))
        while time.monotonic() < deadline:
            out = health.monitor_snapshot(db, enabled=True)
            if len(out["pending"]) == 2:
                break
            time.sleep(0.02)
        assert out["delivery"]["status"] == "recent"
        assert {r["lane"] for r in out["pending"]} == {"nft", "airdrop"}
    finally:
        proc.terminate()
        proc.communicate(timeout=5)
