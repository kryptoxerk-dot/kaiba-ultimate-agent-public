"""The offline replay: extraction from the recorded tables, scoring against the market, report."""

from __future__ import annotations

import json
import types

from kaiba.execution import tweet_launch as tl
from kaiba.execution import tweet_replay as tr

NOW = 1_791_300_000_000


def _seed(conn):
    conn.execute("INSERT INTO tweet_launch_tweets (tweet_id, author, kind, text, media_json, created_ms, received_ms, "
                 "backend, raw_json) VALUES ('1', 'MarioNawfal', 'post', ?, '[]', ?, ?, 'test', '{}')",
                 ('Sec. Rubio: "It\'s called SUPER intelligence." <script>x</script>', NOW, NOW))
    conn.execute("INSERT INTO tweet_launch_tweets (tweet_id, author, kind, text, media_json, created_ms, received_ms, "
                 "backend, raw_json) VALUES ('2', 'elonmusk', 'post', 'nothing launched', '[]', ?, ?, 'test', '{}')",
                 (NOW, NOW))
    for tid, state, sym, token in (("1", "confirmed", "SECRUBIO", "OurMint"), ("2", None, "NOPE", None)):
        conn.execute("INSERT INTO tweet_launches (launch_id, tweet_id, author, chain, dex, mode, verdict, name, symbol, "
                     "decided_ms, state, token) VALUES (?, ?, 'a', 'sol', 'pump', 'live', 'launch', ?, ?, ?, ?, ?)",
                     (f"tl:{tid}:sol", tid, sym, sym, NOW, state, token))
    for tok, sym, created in (("M1", "SUPERINTEL", NOW + 9_000), ("M2", "RUBIO", NOW + 30_000)):
        conn.execute("INSERT INTO tweet_sources (chain, token, kind, author, tweet_id, token_created_ms, fetched_ms, "
                     "via, name, symbol, launchpad) VALUES ('sol', ?, 'tweet', 'MarioNawfal', '1', ?, ?, 'test', ?, ?, "
                     "'pump')", (tok, created, created, sym, sym))
    conn.execute("INSERT INTO tokens (chain, address, first_seen_ms, migrated_ms) VALUES ('sol', 'M1', ?, ?)",
                 (NOW, NOW + 600_000))
    conn.commit()


def test_extract_joins_our_launch_and_the_markets_tokens(tmp_db, tmp_path):
    _seed(tmp_db)
    db = tmp_path / "kaiba.db"
    assert tr.extract(tmp_path / "out", db=db) == 1                 # post 2: no live launch, no market
    [row] = [json.loads(x) for x in (tmp_path / "out" / "posts.jsonl").read_text().splitlines()]
    assert row["tweet_id"] == "1" and row["old"][0]["symbol"] == "SECRUBIO"
    assert [(m["symbol"], m["graduated"]) for m in row["market"]] == [("SUPERINTEL", True), ("RUBIO", False)]


def test_extract_refuses_while_the_wal_is_over_the_limit(tmp_path, monkeypatch):
    db = tmp_path / "x.db"
    (tmp_path / "x.db-wal").write_bytes(b"0" * 10)
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)
    try:
        tr._wait_for_wal(db, limit=5, tries=2)
    except SystemExit as exc:
        assert "WAL" in str(exc)
    else:
        raise AssertionError("must refuse")


def _row(market, old="SECRUBIO"):
    return {"tweet_id": "1", "author": "a", "kind": "post", "text": "<b>x</b>", "media": [], "created_ms": NOW,
            "old": [{"chain": "sol", "state": "confirmed", "symbol": old, "token": "OurMint", "outcome": None}],
            "market": market}


def test_score_counts_agreement_with_the_market_first_and_graduated():
    market = [{"token": "M1", "symbol": "SUPERINTEL", "graduated": True, "name": "", "launchpad": "pump", "chain": "sol"},
              {"token": "M2", "symbol": "RUBIO", "graduated": False, "name": "", "launchpad": "pump", "chain": "sol"},
              # our own launch also links the post; it must not count as the market agreeing with us
              {"token": "OurMint", "symbol": "SECRUBIO", "graduated": False, "name": "", "launchpad": "pump",
               "chain": "sol"}]
    picks = {"1": {"heuristic": {"symbol": "RUBIO"},
                   "picks": {"opus:minimal": {"symbol": "superintel", "launch": True}, "opus:minimal#s": 8.8}}}
    t = tr.score([_row(market)], picks)
    assert {k: t["old picker (as shipped)"][k] for k in ("posts", "named", "any", "first", "launch")} == {"posts": 1, "named": 1, "any": 0, "first": 0, "launch": 1}
    assert t["word heuristic (today)"]["any"] == 1 and t["word heuristic (today)"]["first"] == 0
    assert {k: t["opus:minimal"][k] for k in ("any", "first", "graduated", "launch", "launch_on_grad", "median_s")} == {"any": 1, "first": 1, "graduated": 1, "launch": 1, "launch_on_grad": 1, "median_s": 8.8}


def test_pick_runs_every_variant_and_the_logo_once(tmp_path, monkeypatch):
    out = tmp_path / "o"
    out.mkdir()
    (out / "posts.jsonl").write_text(json.dumps(_row([])) + "\n")
    seen = []

    def namer(text, author, **kw):
        seen.append((kw["model"], kw["reasoning"]))
        return types.SimpleNamespace(launch=True, name="Super", symbol="SUPER", logo_prompt="brain", reason="r")

    logos = []

    def maker(media, name, sym, prompt, **kw):
        logos.append((name, sym, prompt, kw["backends"]))
        return types.SimpleNamespace(source="codex", url=None, note="", b64="aGk=")

    assert tr.pick(out, variants=["claude-opus-5-5:minimal", "claude-sonnet-5-5:minimal"], workers=1,
                   namer=namer, logo_maker=maker) == 1
    assert seen == [("claude-opus-5-5", "minimal"), ("claude-sonnet-5-5", "minimal")]
    assert len(logos) == 1 and logos[0][:3] == ("Super", "SUPER", "brain")
    assert (out / "logos" / "1.jpg").read_bytes() == b"hi"
    # resumable: a second run does nothing
    assert tr.pick(out, variants=["claude-opus-5-5:minimal"], workers=1, namer=namer, logo_maker=maker) == 0


def test_report_escapes_the_untrusted_post_text(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "posts.jsonl").write_text(json.dumps(_row([], old="X")) + "\n")
    page = tr.report(out).read_text(encoding="utf-8")
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_the_heuristic_is_the_shipped_derive_identity():
    assert tl.derive_identity("It's called SUPER intelligence.") is not None
