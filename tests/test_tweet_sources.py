"""Tweet-source league table: which accounts the market launches tokens from."""

from __future__ import annotations

import json

import pytest

from kaiba.ingest import tweet_sources as ts

CID = "Qm" + "a" * 44


@pytest.mark.parametrize("meta, want", [
    ({"twitter": "https://twitter.com/Narra_Creator/status/2107100855908397323"},
     ("tweet", "narra_creator", "2107100855908397323")),
    ({"twitter": "", "website": "https://x.com/elonmusk/status/123456789"}, ("tweet", "elonmusk", "123456789")),
    ({"twitter": "https://x.com/i/status/123456789"}, ("tweet", "i", "123456789")),
    ({"twitter": "https://x.com/SomeDev"}, ("profile", "somedev", None)),
    ({"twitter": "https://x.com/i/communities/1"}, ("none", None, None)),
    ({"website": "https://example.com"}, ("none", None, None)),
])
def test_classify(meta, want):
    assert ts.classify(meta) == want


def test_candidate_urls_skip_blocked_gateways_and_prefer_the_tool_host():
    assert ts.candidate_urls(f"https://ipfs.io/ipfs/{CID}")[0] == f"https://pump.mypinata.cloud/ipfs/{CID}"
    assert ts.candidate_urls(f"https://metadata.j7tracker.io/ipfs/{CID}")[0].startswith("https://metadata.j7tracker.io")
    assert ts.candidate_urls("https://meta.uxento.io/x.json") == ["https://meta.uxento.io/x.json"]
    assert ts.candidate_urls("") == []


def _token(conn, addr, created, uri, migrated=None):
    conn.execute("INSERT INTO tokens (chain, address, symbol, created_ms, first_seen_ms, migrated_ms, meta_json) "
                 "VALUES ('sol', ?, 'X', ?, ?, ?, ?)", (addr, created, created, migrated, json.dumps({"uri": uri})))


def test_run_once_records_sources_and_the_board_ranks_first_launch_graduations(tmp_db):
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, '0', 0)", (ts.WATERMARK,))
    metas = {
        "u1": {"twitter": "https://x.com/alice/status/2107000000000001"},       # alice post 1, first, graduated
        "u2": {"twitter": "https://x.com/alice/status/2107000000000001"},       # alice post 1, copycat
        "u3": {"twitter": "https://x.com/alice/status/2107000000000002"},
        "u4": {"twitter": "https://x.com/alice/status/2107000000000003"},
        "u5": {"twitter": "https://x.com/bob/status/2107000000000007"},         # bob: many posts, nothing graduates
        "u6": {"twitter": "https://x.com/bob/status/2107000000000008"},
        "u7": {"twitter": "https://x.com/bob/status/2107000000000009"},
        "u8": {"twitter": "https://x.com/bob/status/21070000000000010"},
        "u9": None,                                               # unreadable metadata
    }
    for i, uri in enumerate(metas, start=1):
        _token(tmp_db, f"T{i}", 1000 + i, uri, migrated=5000 if uri == "u1" else (6000 if uri == "u2" else None))
    _token(tmp_db, "T0", 900, None)                                  # no uri: not fetched
    tmp_db.commit()
    n = ts.run_once(fetch=lambda uri: (metas[uri], "stub") if metas[uri] else (None, "http_403"))
    assert n == 9
    kinds = dict(tmp_db.execute("SELECT token, kind FROM tweet_sources").fetchall())
    assert kinds["T9"] == "unfetched" and kinds["T1"] == "tweet" and "T0" not in kinds
    assert ts.run_once(fetch=lambda uri: pytest.fail("nothing new")) == 0     # watermark advanced
    b = ts.board(tmp_db, since_ms=0, min_posts=3)
    assert [r["author"] for r in b] == ["alice", "bob"]
    assert b[0] == {"author": "alice", "posts": 3, "tokens": 4, "graduated": 2, "first_graduated": 1,
                    "first_grad_rate": 0.3333, "chains": ["sol"]}
    assert b[1]["posts"] == 4 and b[1]["graduated"] == 0


def test_a_first_start_follows_from_now_and_then_sees_new_launches(tmp_db):
    _token(tmp_db, "Old", 1, "u_old")
    tmp_db.commit()
    assert ts.run_once(fetch=lambda uri: pytest.fail("history is not read")) == 0     # sets the watermark
    _token(tmp_db, "New", 2, "u_new")
    tmp_db.commit()
    seen = []
    assert ts.run_once(fetch=lambda uri: seen.append(uri) or ({"twitter": "https://x.com/a/status/12345678"}, "s")) == 1
    assert seen == ["u_new"]
