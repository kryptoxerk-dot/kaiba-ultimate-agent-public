"""X narrative: read-only search, budget before network, parking on an unfunded key, no lookahead."""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from kaiba.ingest import x_narrative
from kaiba.providers import x_search

MIGS = Path(__file__).resolve().parents[1] / "kaiba" / "core" / "migrations"
KEY = "test-key-not-real"
ENV = {"twitterapi_io_key": KEY, "x_bearer_token": ""}
NOW = 1_791_170_000_000
MINT = "7MbNW8LdQRJ449w8SiPrmW62bQNA6TBJzM32cmCTpump"

# Shape of a twitterapi.io advanced_search answer (documented fields; the live key returned
# HTTP 402 on 2026-10-05, so this fixture is the documented shape, not a captured body).
TWEETS = {
    "tweets": [
        {"id": "111", "url": "https://x.com/a/status/111", "text": f"new gem $KAIBA CA {MINT} 🚀",
         "createdAt": "Mon Oct 05 03:12:44 +0000 2026", "likeCount": 12, "retweetCount": 3,
         "replyCount": 1, "viewCount": 900, "author": {"userName": "a", "followers": 15000,
                                                         "isBlueVerified": True}},
        {"id": "112", "text": "another one 0x1111111111111111111111111111111111111111 $kaiba",
         "createdAt": "Mon Oct 05 03:00:00 +0000 2026", "likeCount": 0, "viewCount": 10,
         "author": {"userName": "b", "followers": 40}},
    ],
    "has_next_page": False,
}


def client_for(status: int, body: dict, calls: list) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_refs_find_cashtags_and_both_address_kinds():
    refs = x_search.extract_refs(f"buy $kaiba and $PEPE2 at {MINT} or 0xAbCdEf0123456789abcdef0123456789ABCDEF01, not $5")
    assert refs["cashtags"] == ["KAIBA", "PEPE2"]
    assert refs["sol"] == [MINT]
    assert refs["evm"] == ["0xabcdef0123456789abcdef0123456789abcdef01"]


def test_twitterapi_io_posts_are_normalised():
    calls: list = []
    res = x_search.search(MINT, env=ENV, client=client_for(200, TWEETS, calls))
    assert res.ok and res.backend == "twitterapi_io" and len(res.posts) == 2
    p = res.posts[0]
    assert p.author == "a" and p.author_followers == 15000 and p.author_verified is True
    assert p.created_ms is not None and p.views == 900
    assert calls[0].headers["X-API-Key"] == KEY
    assert calls[0].url.params["queryType"] == "Latest"


def test_an_unfunded_key_reports_the_providers_reason_and_never_the_key():
    calls: list = []
    res = x_search.search("x", env=ENV, client=client_for(
        402, {"error": "Unauthorized", "message": "Credits is not enough.Please recharge"}, calls))
    assert not res.ok and res.reason.startswith("http 402") and "Credits" in res.reason
    assert KEY not in res.reason


def test_budget_refuses_before_the_network(tmp_path):
    conn = _db(tmp_path)
    budget = x_search.Budget(conn, daily_cap=2, clock=lambda: NOW / 1000)
    calls: list = []
    c = client_for(200, TWEETS, calls)
    assert x_search.search("a", env=ENV, budget=budget, client=c).ok
    assert x_search.search("b", env=ENV, budget=budget, client=c).ok
    res = x_search.search("c", env=ENV, budget=budget, client=c)
    assert not res.ok and "cap" in res.reason and len(calls) == 2


def test_no_credential_means_no_call():
    res = x_search.search("a", env={"twitterapi_io_key": "", "x_bearer_token": ""})
    assert not res.ok and res.backend is None


def test_the_module_can_only_read():
    src = inspect.getsource(x_search)
    assert ".post(" not in src and ".put(" not in src and ".delete(" not in src


# --------------------------------------------------------------------------------------
# the recorder
# --------------------------------------------------------------------------------------


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "k.db", isolation_level=None)
    conn.executescript("""
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_ms INTEGER);
        CREATE TABLE signals (signal_id TEXT, lane TEXT, chain TEXT, token TEXT, created_ms INT);
    """)
    conn.executescript((MIGS / "038_x_posts.sql").read_text(encoding="utf-8"))
    return conn


def test_recorder_observes_once_per_token_and_records_posts(tmp_path):
    conn = _db(tmp_path)
    conn.execute("INSERT INTO signals VALUES ('s1','sm-trenches','sol',?,?)", (MINT, NOW - 60_000))
    calls: list = []
    c = client_for(200, TWEETS, calls)
    out = x_narrative.observe(conn, env=ENV, now_ms=NOW, client=c)
    assert out["status"] == "ok" and out["observed"] == 1 and out["new_posts"] == 2
    row = conn.execute("SELECT ok, n_posts, n_authors, max_followers FROM x_token_obs").fetchone()
    assert tuple(row) == (1, 2, 2, 15000)
    refs = json.loads(conn.execute("SELECT refs_json FROM x_posts WHERE post_id='111'").fetchone()[0])
    assert refs["sol"] == [MINT]
    # Inside the cooldown the same token is not searched again.
    again = x_narrative.observe(conn, env=ENV, now_ms=NOW + 120_000, client=c)
    assert again["observed"] == 0 and len(calls) == 1


def test_an_unfunded_key_parks_the_recorder(tmp_path):
    conn = _db(tmp_path)
    for i in range(3):
        conn.execute("INSERT INTO signals VALUES (?,?,?,?,?)", (f"s{i}", "sm-trenches", "sol", f"tok{i}", NOW - 1000))
    calls: list = []
    c = client_for(402, {"message": "Credits is not enough.Please recharge"}, calls)
    out = x_narrative.observe(conn, env=ENV, now_ms=NOW, client=c)
    assert out["status"] == "parked" and len(calls) == 1
    later = x_narrative.observe(conn, env=ENV, now_ms=NOW + 60_000, client=c)
    assert later["status"] == "parked" and len(calls) == 1
    later_ms = NOW + x_narrative.PARK_MS + 1
    conn.execute("INSERT INTO signals VALUES ('s9','sm-trenches','sol','tok9',?)", (later_ms - 1000,))
    after = x_narrative.observe(conn, env=ENV, now_ms=later_ms, client=c)
    assert after["status"] == "parked" and len(calls) == 2  # tried once more, refused, parked again


def test_posts_dated_after_the_observation_are_ignored():
    early = x_search.Post("1", NOW - 10_000, "a", 10, None, "", 1, 0, 0, 5, None)
    future = x_search.Post("2", NOW + 10_000, "b", 99999, None, "", 50, 0, 0, 500, None)
    s = x_narrative.summarise([early, future], now_ms=NOW)
    assert s["n_posts"] == 1 and s["max_followers"] == 10


@pytest.mark.parametrize("delay_ok", [True, False])
def test_audit_reads_x_only_inside_the_declared_delay(delay_ok):
    from kaiba.learning import signal_audit as sa
    t0 = NOW
    obs_ms = t0 + (sa.X_DELAY_MS - 1 if delay_ok else sa.X_DELAY_MS + 1)
    rows = [(t0 + s * 1000, 1.0, "f", "w", "buy") for s in range(-60, 900, 30)]
    case = sa.build_case(lane="sm-trenches", chain="sol", token="T", t0=t0, rows=rows,
                         wallets=["w"], payload={}, ladder=sa.ExitLadder(), kol=set(),
                         trusted=set(), grades_now={}, grades_pit=None,
                         x_obs=[(obs_ms, 7, 5000)])
    assert (case.features.get("x_posts") == 7) is delay_ok
