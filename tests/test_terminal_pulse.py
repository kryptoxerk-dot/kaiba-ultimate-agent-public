"""The terminal pulse producer (TERMINAL-PULSE-BACKEND-CLAUDE-20261103).

Each test is a way the "Bloomberg terminal for shitcoins" could mislead: a sample shown as the world's
best, a PnL given a window nobody proved, a "found today" on the wrong day, a trending volume given a
window GMGN never stated, a chain total built by summing multi-chain protocols, DefiLlama asked more than
five times an hour, a failure filled in, or a read that breaks the database rule. No test touches the
network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kaiba.market_intel import terminal_pulse as T

NOW = 1_793_707_200_000          # 2026-11-03 12:00 UTC = 19:00 WIB
TODAY_WIB_START = 1_793_638_800_000  # 2026-11-03 00:00 WIB = 2026-11-02 17:00 UTC


def make_db(path: Path, n: int = 450) -> Path:
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE wallet_score_history(id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, address TEXT, score REAL,
      grade TEXT, scored_at_ms INTEGER, model_version TEXT);
    CREATE TABLE wallet_scores(chain TEXT, address TEXT, score REAL, grade TEXT, evidence_weight REAL, archetype TEXT,
      realized_pnl_usd TEXT, win_rate REAL, closed_trades INTEGER, distinct_tokens INTEGER, median_hold_s INTEGER,
      factors_json TEXT, penalties_json TEXT, blockers_json TEXT, receipts_json TEXT, model_version TEXT,
      scored_at_ms INTEGER, PRIMARY KEY(chain, address));
    CREATE TABLE wallets(chain TEXT, address TEXT, name TEXT, source TEXT, tags_json TEXT, first_seen_ms INTEGER,
      last_seen_ms INTEGER, PRIMARY KEY(chain, address));
    """)
    for i in range(n):
        chain, addr = ("sol" if i % 3 else "robinhood"), f"W{i:04d}"
        scored = NOW - (n - i) * 60_000
        first_seen = TODAY_WIB_START + 3_600_000 if i % 10 == 0 else TODAY_WIB_START - 5 * 86_400_000
        c.execute("INSERT INTO wallet_score_history(chain, address, score, grade, scored_at_ms, model_version) "
                  "VALUES (?,?,?,?,?,?)", (chain, addr, 50.0, "C", scored, "kaiba-wallet-tape-v1"))
        c.execute("INSERT INTO wallet_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (chain, addr, 50.0, "C", 1.0, "trader", str(i * 10.0) if i % 7 else None, 0.55, 12, 9, 600,
                   "[]", "[]", json.dumps(["thin_evidence"]),
                   json.dumps([{"provider": "kaiba", "endpoint": "swaps", "basis": "verified_onchain"}]),
                   "kaiba-wallet-tape-v1", scored))
        c.execute("INSERT INTO wallets VALUES (?,?,?,?,?,?,?)", (chain, addr, None, "gmgn:smartmoney", "[]", first_seen, scored))
    # a wallet graded twice keeps only its latest row; a history row with no score row is skipped
    c.execute("INSERT INTO wallet_score_history(chain, address, score, grade, scored_at_ms, model_version) "
              "VALUES ('sol','W0449',60,'B',?, 'kaiba-wallet-tape-v1')", (NOW,))
    c.execute("INSERT INTO wallet_score_history(chain, address, score, grade, scored_at_ms, model_version) "
              "VALUES ('sol','ORPHAN',60,'B',?, 'kaiba-wallet-tape-v1')", (NOW,))
    c.commit()
    c.close()
    return path


def trending(chains=("sol", "eth", "base")):
    def cache(key):
        chain = key.split("--chain ")[1].split(" ")[0]
        if chain not in chains:
            return None
        rows = [{"address": f"T{chain}{i}", "symbol": f"S{i}", "name": f"N{i}", "rank": i + 1, "market_cap": 1e5 + i,
                 "liquidity": 2e4, "volume": 233058, "swaps": 10, "buys": 6, "sells": 4,
                 "price_change_percent1h": 12.5, "holder_count": 99, "launchpad": "pump", "creation_timestamp": 1_793_700_000}
                for i in range(50)]
        return rows, NOW - 120_000
    return cache


class Llama:
    def __init__(self, fail=()):
        self.urls, self.fail = [], set(fail)

    def __call__(self, url):
        self.urls.append(url)
        chain = url.split("/overview/dexs/")[1].split("?")[0]
        if chain in self.fail:
            raise RuntimeError("HTTP 502")
        # a protocols list is present in the real body; a chain total must never be built from it
        return {"total24h": 1_000_000.0 * (len(chain)), "change_1d": -3.5,
                "protocols": [{"name": "multichain-dex", "total24h": 9e12}]}


@pytest.fixture
def env(tmp_path):
    return make_db(tmp_path / "kaiba.db"), tmp_path / "terminal-pulse"


def test_the_snapshot_has_the_agreed_schema_and_is_written_atomically(env):
    db, out = env
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=Llama(), cache=trending())
    on_disk = json.loads((out / "latest.json").read_text())
    assert on_disk == json.loads(json.dumps(doc))
    assert set(doc) == {"schema", "observed_ms", "wallets", "tokens", "chains", "coverage", "source_health"}
    assert doc["schema"] == 1 and doc["observed_ms"] == NOW
    assert not list(out.glob(".*.tmp"))


def test_wallets_are_a_labelled_sample_with_an_unknown_pnl_window(env):
    db, out = env
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=trending())
    w = doc["wallets"]
    assert len(w) <= 300 and len({(r["chain"], r["address"]) for r in w}) == len(w)
    assert all(r["pnl_window"] == "unknown" and r["basis"].startswith("Kaiba swap tape") for r in w)
    assert all(r["win_rate"] is None or 0.0 <= r["win_rate"] <= 1.0 for r in w)
    cov = doc["coverage"]["wallets"]
    assert "Not a ranking of the best wallets" in cov["label"] and "not a signal to copy" in cov["label"]
    assert cov["history_rows_read"] == 452 and cov["unique_wallets_in_sample"] == 451 and cov["with_score_row"] == 450
    w1 = next(r for r in w if r["address"] == "W0449")
    assert w1["grade"] == "C"  # the score row is the current one; history only selects the wallet
    assert not any(r["address"] == "ORPHAN" for r in w)


def test_the_export_is_the_union_of_pnl_leaders_recent_and_found_today(env):
    db, out = env
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=trending())
    w = doc["wallets"]
    top_pnl = sorted((float(i * 10.0) for i in range(450) if i % 7), reverse=True)[:100]
    exported = sorted((r["realized_pnl_usd"] for r in w if "pnl_top100" in r["selected_by"]), reverse=True)
    assert exported == top_pnl
    assert sum("recent100" in r["selected_by"] for r in w) == 100
    found = [r for r in w if "found_today100" in r["selected_by"]]
    assert found and all(r["found_wib_day"] == "2026-11-03" for r in found) and len(found) == 45


def test_found_today_uses_the_wib_day_not_utc():
    assert T.wib_day(TODAY_WIB_START) == "2026-11-03"          # 17:00 UTC on 11-02 is 00:00 WIB on 11-03
    assert T.wib_day(TODAY_WIB_START - 1) == "2026-11-02"


def test_trending_tokens_come_only_from_the_existing_cache_with_volume_window_unknown(env):
    db, out = env
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=trending(("sol", "base")))
    t = doc["tokens"]
    assert len(t) == 100 and {r["chain"] for r in t} == {"sol", "base"}
    assert all(r["volume_window"] == "unknown" and r["volume_1h_usd"] is None and r["volume_24h_usd"] is None for r in t)
    assert all(r["trend_window"] == "1h" and r["observed_ms"] == NOW - 120_000 for r in t)
    h = doc["source_health"]
    assert h["gmgn_trending:sol"]["status"] == "ok" and h["gmgn_trending:sol"]["fetched_ms"] == NOW - 120_000
    assert h["gmgn_trending:eth"]["status"] == "missing" and h["gmgn_trending:robinhood"]["status"] == "missing"


def test_the_trending_cache_key_is_the_feeds_own_invocation():
    assert T._trending_key("sol") == "gmgn-cli market trending --chain sol --interval 1h --limit 50 --raw"


def test_a_cache_older_than_fifteen_minutes_is_marked_stale(env):
    db, out = env

    def old(key):
        rows, _ = trending()(key) or (None, None)
        return (rows, NOW - 16 * 60_000) if rows else None

    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=old)
    assert doc["source_health"]["gmgn_trending:sol"]["status"] == "stale"


def test_chain_volume_uses_the_chain_total_and_asks_each_chain_at_most_hourly(env):
    db, out = env
    llama = Llama()
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=llama, cache=trending())
    assert len(llama.urls) == 5 and all("excludeTotalDataChart=true" in u for u in llama.urls)
    assert {r["chain"] for r in doc["chains"]} == set(T.DEX_CHAINS)
    assert all(r["volume_24h_usd"] < 1e12 for r in doc["chains"])  # never the protocols' multi-chain rows
    T.build(now_ms=NOW + 59 * 60_000, db=db, out_dir=out, http_get=llama, cache=trending())
    assert len(llama.urls) == 5  # inside the hour: no request
    T.build(now_ms=NOW + 60 * 60_000, db=db, out_dir=out, http_get=llama, cache=trending())
    assert len(llama.urls) == 10


def test_a_failed_chain_keeps_its_last_value_with_its_original_clock_and_says_so(env):
    db, out = env
    T.build(now_ms=NOW, db=db, out_dir=out, http_get=Llama(), cache=trending())
    doc = T.build(now_ms=NOW + 3_600_000, db=db, out_dir=out, http_get=Llama(fail={"bsc"}), cache=trending())
    bsc = next(r for r in doc["chains"] if r["chain"] == "bsc")
    assert bsc["observed_ms"] == NOW and doc["source_health"]["defillama:bsc"]["status"] == "error"
    assert "502" in doc["source_health"]["defillama:bsc"]["error"]


def test_a_never_fetched_chain_is_missing_not_zero(env):
    db, out = env
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=Llama(fail={"arbitrum"}), cache=trending())
    assert not any(r["chain"] == "arbitrum" for r in doc["chains"])
    assert doc["source_health"]["defillama:arbitrum"]["status"] == "error"


def test_a_wal_over_the_admission_line_defers_the_wallets_and_nothing_else(env, monkeypatch):
    db, out = env
    Path(str(db) + "-wal").write_bytes(b"x" * 64)
    monkeypatch.setattr(T, "WAL_MAX_BYTES", 32)
    doc = T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=trending())
    assert doc["wallets"] == [] and doc["source_health"]["wallet_db"]["status"] == "deferred"
    assert len(doc["tokens"]) == 150


def test_reads_are_read_only_bounded_and_keyed(env):
    db, _ = env
    traced = []
    real = sqlite3.connect

    def spy(*a, **k):
        conn = real(*a, **k)
        conn.set_trace_callback(traced.append)
        return conn

    import kaiba.market_intel.terminal_pulse as mod
    mod.sqlite3.connect, saved = spy, mod.sqlite3.connect
    try:
        T.read_wallets(db, NOW)
    finally:
        mod.sqlite3.connect = saved
    sql = [s for s in traced if s.lstrip().upper().startswith("SELECT")]
    assert sql and all("LIMIT" in s or "IN (VALUES" in s for s in sql)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE")) for s in traced)


def test_an_oversized_snapshot_is_refused(env, monkeypatch):
    db, out = env
    monkeypatch.setattr(T, "MAX_BYTES", 1000)
    with pytest.raises(ValueError):
        T.build(now_ms=NOW, db=db, out_dir=out, http_get=None, cache=trending())
    assert not (out / "latest.json").exists()


# ------------------------------------------------------------------ provider refusal (Codex review)


class Refusing:
    """Answers ``status`` with ``headers`` for every chain in ``refuse``, a normal body otherwise."""

    def __init__(self, status, headers=None, refuse=("solana",)):
        self.status, self.headers, self.refuse, self.urls = status, headers or {}, set(refuse), []

    def __call__(self, url):
        import httpx

        self.urls.append(url)
        chain = url.split("/overview/dexs/")[1].split("?")[0]
        if chain in self.refuse:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("refused", request=req, response=httpx.Response(self.status, request=req,
                                                                                      headers=self.headers))
        return {"total24h": 5e8, "change_1d": 1.0}


def test_a_429_stops_every_remaining_chain_and_persists_its_retry_after(tmp_path):
    state = tmp_path / "dex.json"
    http = Refusing(429, {"Retry-After": "21600"})
    rows, cov, health = T.read_chains(NOW, state, http)
    assert len(http.urls) == 1 and rows == []
    assert cov["provider_cooldown_until_ms"] == NOW + 21_600_000
    assert all(h["status"] == "cooldown" for h in health.values())
    T.read_chains(NOW + 4_000_000, state, http)  # a later process, past the hourly refresh
    assert len(http.urls) == 1
    T.read_chains(NOW + 21_600_000, state, Refusing(200, refuse=()))  # the cooldown is over
    assert json.loads(state.read_text())["solana"]["ok"] is True


@pytest.mark.parametrize("headers, until", [
    ({}, NOW + T.DEX_REFUSED_BACKOFF_MS),                                         # 403, no header: the floor
    ({"Retry-After": "60"}, NOW + T.DEX_REFUSED_BACKOFF_MS),                      # shorter than the floor
    ({"Retry-After": "Tue, 03 Nov 2026 20:00:00 GMT"}, 1_793_736_000_000),        # an HTTP date beyond it
])
def test_a_403_or_retry_after_sets_the_provider_cooldown(tmp_path, headers, until):
    _, cov, _ = T.read_chains(NOW, tmp_path / "dex.json", Refusing(403, headers))
    assert cov["provider_cooldown_until_ms"] == until


def test_a_server_error_is_per_chain_not_a_provider_refusal(tmp_path):
    http = Refusing(502)
    _, cov, health = T.read_chains(NOW, tmp_path / "dex.json", http)
    assert len(http.urls) == 5 and cov["provider_cooldown_until_ms"] is None
    assert health["defillama:solana"]["status"] == "error" and health["defillama:base"]["status"] == "ok"


def test_last_values_keep_their_clock_through_a_cooldown(tmp_path):
    state = tmp_path / "dex.json"
    T.read_chains(NOW, state, Refusing(200, refuse=()))
    rows, _, health = T.read_chains(NOW + 3_600_000, state, Refusing(429, {"Retry-After": "21600"}))
    assert {r["chain"] for r in rows} == set(T.DEX_CHAINS) and all(r["observed_ms"] == NOW for r in rows)
    assert health["defillama:ethereum"]["status"] == "cooldown"


def test_an_unreadable_state_file_cannot_grant_capacity(tmp_path):
    state = tmp_path / "dex.json"
    state.write_text("{not json")
    http = Refusing(200, refuse=())
    _, cov, health = T.read_chains(NOW, state, http)
    assert http.urls == [] and cov["provider_cooldown_until_ms"] == NOW + T.DEX_REFUSED_BACKOFF_MS
    assert json.loads(state.read_text())["_provider"]["reason"] == "state_unreadable"
