"""The analyst desk: forecasts only, scored honestly, with no path to money.

Fixtures run on the real migrated schema (``tmp_db``), and reads go through the real GuardedReader
against a real file: a mock here would hide exactly what these tests exist to catch.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import io
import json
import re
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
import yaml

from kaiba.intelligence import deployer
from kaiba.learning import desk, labs
from kaiba.learning import desk_outcomes as outcomes
from kaiba.learning import desk_packets as packets
from kaiba.learning.autolearn_io import WAL_LIMIT, GuardedReader

REPO = Path(__file__).resolve().parents[1]
NOW = 1_793_800_000_000
H = 3_600_000
TOKEN = "TokenFixtureAddress0001"
SUBJECT = f"sol:{TOKEN}"
POOL = "PoolFixtureAddress000001"
CREATOR = "FakeCreatorFixtureWXYZabcdefghij"
SMART_WALLET = "FakeSmartWalletFixtureWXYZabcdefg"
OPP = "abcdef0123456789abcdef0123456789"
ROLE_VIEWS = {role: f"{role} view" for role in desk.ROLES}


def measure(value, age_s=60, basis="provider_reported", budget=600):
    return {"value": str(value), "basis": basis, "freshness_budget_s": budget,
            "receipt": {"observed_at_ms": NOW - age_s * 1000, "basis": basis}}


class World:
    """One small Kaiba: a migrated database and the files the desk reads."""

    def __init__(self, conn, root):
        self.conn, self.root = conn, root
        labs._cache.clear()

    def src(self, now=NOW):
        return packets.Sources(self.root, self.root / "kaiba.db", now)

    def run(self, sql, args=()):
        self.conn.execute(sql, args)
        self.conn.commit()

    def token(self, *, chain="sol", address=TOKEN, migrated_ms=None, creator=CREATOR):
        self.run("INSERT OR REPLACE INTO tokens(chain,address,symbol,name,creator,created_ms,launchpad,migrated_ms,"
                 "first_seen_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                 (chain, address, "FIX", "Fixture", creator, NOW - 2 * H, "pump", migrated_ms, NOW - 2 * H))

    def dossier(self, *, built=NOW - 60_000, **measures):
        body = {"liquidity_usd": measure(5000), "holder_count": measure(120), "mint_authority_revoked": True,
                "blockers": [], "warnings": ["low_liquidity"], "graded_wallets": [], **measures}
        self.run("INSERT OR REPLACE INTO token_dossiers(chain,address,built_at_ms,score,grade,dossier_json) "
                 "VALUES ('sol',?,?,77.0,'B',?)", (TOKEN, built, json.dumps(body)))

    def swap(self, ts, price, *, source="pumpfun", tx=None, chain="sol", token=TOKEN):
        self.run("INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,price_usd,source) VALUES (?,?,?,?,?,'buy',?,?)",
                 (chain, tx or f"tx{ts}{source}", ts, SMART_WALLET, token, str(price), source))

    def decision(self, *, ts=NOW - 60_000, token=TOKEN, chain="sol", lane="launch-snipe", mode="shadow", action="enter"):
        self.run("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,blockers_json,features_json) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (f"d{ts}{token}", ts, lane, mode, chain, token, action, "[]",
                                                json.dumps({"top10_pct": 31.5, "liquidity_usd": None})))

    def graduation_event(self, ts, *, chain="sol"):
        self.run("INSERT INTO events(ts_ms,kind,chain,subject) VALUES (?,'token.migrated',?,'x')", (ts, chain))

    def intel(self, key, data, *, age_s=30, ok=True, stale=False, now=NOW):
        folder = self.root / "market-intel" / "intel"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{key}.json").write_text(json.dumps({"key": key, "ok": ok, "stale": stale, "data": data,
                                                        "fetched_ts": (now - age_s * 1000) / 1000}), encoding="utf-8")

    def tickers(self, price=100.0, *, symbol="BTCUSDT", age_s=30, now=NOW, change=-1.5):
        self.intel("tickers", {"tickers": [{"symbol": symbol, "price": price, "change_pct": change, "high": price * 1.02,
                                            "low": price * 0.97, "quote_volume": 1e9}]}, age_s=age_s, now=now)

    def lab(self, pools, *, at_ms=NOW - H):
        folder = self.root / "labs" / "lp"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "result.json").write_text(json.dumps({"Q1": {"lp_sol": {"all": {"n": 10, "mean": 0.01}}}, "at": at_ms // 1000,
                                                        "forward": {"lp_sol": {"n": 12}}, "pools_table": pools}), encoding="utf-8")
        labs._cache.clear()

    def nft(self, *, decided=NOW - H, verdict="shadow"):
        cursor = self.conn.execute(
            "INSERT INTO nft_paper_mints(collection,day_utc,decided_ms,verdict,rule_version,failed_json,features_json,"
            "mint_price_wei,mint_fee_wei,mint_gas_wei,sell_gas_wei,cost_wei,label) "
            "VALUES (?,?,?,?,'v1','[]',?,'1000000000000000','0','0','0','1000000000000000','Fixture Drop')",
            (f"0xcollection{decided}", f"day{decided}", decided, verdict,
             json.dumps({"fills_24h": 4, "fee_recipient": SMART_WALLET, "max_supply": 100, "unlisted_int": 7})))
        self.conn.commit()
        return cursor.lastrowid

    def nft_mark(self, paper_id, *, decided, exit_basis="offer_median", pnl="5", horizon=24, half=6 * H):
        mark = decided + horizon * H
        self.run("INSERT INTO nft_paper_marks(paper_id,horizon_h,mark_ms,window_from_ms,window_to_ms,n_offer,n_listing,"
                 "n_unpriced,exit_basis,exit_wei,net_exit_wei,pnl_wei,best_basis,best_exit_wei,pnl_best_wei,scored_ms,"
                 "mark_version) VALUES (?,?,?,?,?,1,0,0,?,'9','9',?,'none','0','0',?,'mark-v1')",
                 (paper_id, horizon, mark, mark - half, mark + half, exit_basis, pnl, mark))

    def opportunity(self, *, last_seen=NOW - H, source="airdrops_io", confirmed=False, ev=None):
        self.run("INSERT OR REPLACE INTO opportunities(opportunity_id,kind,name,status,ev_score,evidence_json,source,"
                 "created_ms,updated_ms,last_seen_ms) VALUES (?,'airdrop','Fixture Drop','open',?,?,?,?,?,?)",
                 (OPP, ev, json.dumps({"confirmed_token": confirmed, "sybil_risk": "medium", "source_count": 1}), source,
                  NOW - 24 * H, last_seen, last_seen))

    def hunter(self, name, *, last_ok, fails=0, count=5, kind="airdrop"):
        self.run("INSERT OR REPLACE INTO hunter_sources(name,kind,last_ok_ms,last_count,fail_streak) VALUES (?,?,?,?,?)",
                 (name, kind, last_ok, count, fails))


@pytest.fixture
def w(tmp_db, tmp_path):
    return World(tmp_db, tmp_path)


def question(packet, outcome_id):
    return next(o for o in packet["outcomes"] if o["id"] == outcome_id)


def verdict(packet, outcome_id=None, **over):
    item = question(packet, outcome_id) if outcome_id else packet["outcomes"][0]
    body = {"packet_id": packet["packet_id"], "domain": packet["domain"], "subject": packet["subject"],
            "horizon_h": item["horizon_h"], "stance": "long" if item["direction"] == "up" else "avoid", "probability": 0.4,
            "outcome": {"metric": item["metric"], "op": item["op"], "threshold": item["threshold"]},
            "roles": dict(ROLE_VIEWS), "evidence": [next(iter(packet["facts"]))]}
    body.update(over)
    return json.dumps(body)


def stored(w):
    with closing(desk._connect(w.src())) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM verdicts")]


def perps_verdict(w, outcome_id="perp_up3_24h", **over):
    w.tickers(100.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    return desk.record(verdict(packet, outcome_id, **over), src=w.src())


# ---------------------------------------------------------------------- the one door


def test_a_fresh_input_is_a_fact_with_its_source_and_age():
    e = packets.Evidence(NOW)
    assert e.fact("x", 5, source="tickers", as_of_ms=NOW - 90_000, budget_s=120)
    assert e.facts["x"] == {"value": 5, "source": "tickers", "age_s": 90, "as_of_ms": NOW - 90_000}
    assert e.unknowns == []


def test_a_stale_input_is_unknown_and_its_value_is_gone():
    e = packets.Evidence(NOW)
    assert not e.fact("x", 5, source="tickers", as_of_ms=NOW - 121_000, budget_s=120)
    assert "x" not in e.facts
    assert e.unknowns == [{"key": "x", "reason": "stale:121s>120s", "source": "tickers"}]


@pytest.mark.parametrize("value,as_of,reason", [(None, NOW, "missing"), ("", NOW, "missing"), ([], NOW, "missing"),
                                                (5, None, "no_clock"), (5, NOW + 3_600_000, "future_clock:-3600s")])
def test_a_missing_or_unclocked_input_is_unknown_never_a_zero(value, as_of, reason):
    e = packets.Evidence(NOW)
    assert not e.fact("x", value, source="s", as_of_ms=as_of, budget_s=60)
    assert e.facts == {}
    assert e.unknowns[0]["reason"] == reason


@pytest.mark.parametrize("value", [0, 0.0, False])
def test_a_real_measured_zero_stays_a_fact(value):
    """The control for the test above: the door rejects absence, not smallness."""
    e = packets.Evidence(NOW)
    assert e.fact("x", value, source="s", as_of_ms=NOW, budget_s=60)
    assert e.facts["x"]["value"] == value


def test_address_shaped_text_is_masked_and_declared_untrusted():
    e = packets.Evidence(NOW)
    e.text("t", f"send to {CREATOR} or 0x{'ab' * 20} now\n", source="news", as_of_ms=NOW, budget_s=60)
    assert e.facts["t"]["value"] == "send to [address] or [address] now"
    assert e.untrusted == ["t"]


# ---------------------------------------------------------------------- packets


def test_token_packet_marks_a_stale_measure_unknown_and_keeps_the_fresh_one(w):
    w.token()
    w.dossier(liquidity_usd=measure(5000, age_s=2 * 3600), holder_count=measure(120, age_s=60))
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert "dossier.liquidity_usd" not in packet["facts"]
    reasons = {u["key"]: u["reason"] for u in packet["unknowns"]}
    assert reasons["dossier.liquidity_usd"].startswith("stale:")
    assert packet["facts"]["dossier.holder_count"]["value"] == 120.0
    assert packet["facts"]["dossier.holder_count"]["source"] == "token_dossiers"


@pytest.mark.parametrize("basis", ["unavailable", "stale", "estimated"])
def test_a_retained_value_never_upgrades_a_weak_basis(w, basis):
    w.token()
    w.dossier(liquidity_usd=measure(5000, basis=basis))
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert "dossier.liquidity_usd" not in packet["facts"]
    assert {"key": "dossier.liquidity_usd", "reason": f"basis:{basis}", "source": "token_dossiers"} in packet["unknowns"]


def test_a_field_that_is_not_a_list_is_unknown_not_an_empty_record(w):
    """Silent-idle again: "not readable" must never read as "nothing recorded"."""
    w.token()
    w.dossier(warnings="not a list", blockers=[])
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert "dossier.warnings" not in packet["facts"]
    assert {"key": "dossier.warnings", "reason": "missing", "source": "token_dossiers"} in packet["unknowns"]
    assert packet["facts"]["dossier.blockers"]["value"] == ["none_recorded"]      # the control: a real empty list


def test_an_unreadable_json_column_is_unknown_not_an_empty_record(w):
    w.token()
    w.decision()
    w.run("UPDATE decisions SET blockers_json='{broken'")
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert "decision.blockers" not in packet["facts"]
    assert {"key": "decision.blockers", "reason": "missing", "source": "decisions"} in packet["unknowns"]


def test_token_packet_reads_decision_time_features_and_null_stays_unknown(w):
    w.token()
    w.decision()
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert packet["facts"]["decision.features.top10_pct"]["value"] == 31.5
    assert "decision.features.liquidity_usd" not in packet["facts"]


def test_token_packet_counts_a_deployer_record_without_naming_the_deployer(w):
    w.token()
    deployer.ensure_table(w.conn)
    w.run("INSERT INTO deployer_stats VALUES ('sol',?,12,10,1,'3.5',?)", (CREATOR, NOW - H))
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert packet["facts"]["deployer.launches"]["value"] == 12


def test_packets_never_carry_a_wallet_address(w):
    w.token()
    w.dossier()
    w.decision()
    w.swap(NOW - 1000, 0.5)
    w.run("INSERT INTO signals(signal_id,lane,chain,token,strength,wallets_json,created_ms) VALUES "
          "('s1','launch-snipe','sol',?,0.8,?,?)", (TOKEN, json.dumps([SMART_WALLET]), NOW - 1000))
    deployer.ensure_table(w.conn)
    w.run("INSERT INTO deployer_stats VALUES ('sol',?,12,10,1,'3.5',?)", (CREATOR, NOW - H))
    paper_id = w.nft()
    dump = json.dumps([desk.brief("token", SUBJECT, src=w.src()), desk.brief("nft", f"paper:{paper_id}", src=w.src())])
    assert CREATOR not in dump and SMART_WALLET not in dump
    assert '"signals.latest_wallet_count"' in dump          # the count is there, the wallets are not


def test_a_subject_nobody_holds_is_all_unknown_and_offers_no_question(w):
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert packet["facts"] == {}
    assert packet["outcomes"] == []
    assert {u["key"] for u in packet["unknowns"]} >= {"token.row", "dossier", "signals", "tape"}


def test_perps_packet_stale_ticker_is_unknown_and_withdraws_the_price_questions(w):
    w.tickers(100.0, age_s=2000)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    assert "ticker.price" not in packet["facts"]
    assert any(u["key"] == "ticker" and u["reason"].startswith("stale:") for u in packet["unknowns"])
    assert packet["outcomes"] == []


def test_perps_packet_fresh_inputs_become_facts(w):
    w.tickers(100.0)
    w.intel("hl_market", {"rows": [{"coin": "BTC", "funding_ann_pct": 10.9, "oi_usd": 3e9, "premium": -0.0004,
                                    "change_24h_pct": -0.4}]})
    w.intel("crypto_macro", {"global": {"btc_dominance": 59.2}, "fear_greed": {"value": 34}, "stablecoins": {}})
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    assert packet["facts"]["ticker.price"]["value"] == 100.0
    assert packet["facts"]["hl.funding_ann_pct"]["value"] == 10.9
    assert packet["facts"]["macro.fear_greed"]["value"] == 34.0
    assert "macro.stablecoin_chg_7d_pct" in {u["key"] for u in packet["unknowns"]}
    assert {o["id"] for o in packet["outcomes"]} == {"perp_up3_24h", "perp_down3_24h", "perp_up5_72h", "perp_down5_72h"}


def test_perps_packet_a_failed_collector_is_unknown_even_if_its_file_is_new(w):
    w.intel("hl_market", {"rows": [{"coin": "BTC", "funding_ann_pct": 10.9}]}, ok=False)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    assert "hl.funding_ann_pct" not in packet["facts"]
    assert {"key": "hl", "reason": "source_failed_or_marked_stale", "source": "market-intel/hl_market"} in packet["unknowns"]


def test_perps_packet_a_coin_outside_the_top_rows_is_unknown(w):
    w.intel("hl_market", {"rows": [{"coin": "ETH", "funding_ann_pct": 5.0}]})
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    assert {"key": "hl", "reason": "coin_not_in_top_rows", "source": "market-intel/hl_market"} in packet["unknowns"]


def test_perps_packet_reads_the_paper_books(w):
    (w.root / "perps" / "xs").mkdir(parents=True)
    (w.root / "perps" / "report.json").write_text(json.dumps(
        {"as_of_ms": NOW - H, "verdict": {"verdict": "PENDING", "n": 0}, "symbols": {"BTCUSDT": {"status": "ok"}},
         "open_positions": [{"symbol": "BTCUSDT", "side": "long"}]}), encoding="utf-8")
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    assert packet["facts"]["paper.d1.closed_n"]["value"] == 0          # a measured zero, from a fresh report
    assert packet["facts"]["paper.d1.open_side"]["value"] == "long"
    other = desk.brief("perps", "SUIUSDT", src=w.src())
    assert {"key": "paper.d1.symbol", "reason": "symbol_not_in_frozen_universe", "source": "perps/report.json"} in other["unknowns"]


def test_lp_packet_a_stale_lab_is_unknown(w):
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}], at_ms=NOW - 40 * H)
    packet = desk.brief("lp", POOL, src=w.src())
    assert "pool.tvl" not in packet["facts"]
    assert any(u["key"] == "pool.tvl" and u["reason"].startswith("stale:") for u in packet["unknowns"])
    assert packet["outcomes"] == []


def test_lp_packet_a_pool_outside_the_panel_is_unknown_and_a_panel_pool_is_read(w):
    w.lab([{"address": POOL, "name": "FIX-SOL", "tvl": 1000.0, "fee_tvl_24h": 0.02, "positions": 9}])
    assert {"key": "pool", "reason": "not_in_panel", "source": "labs/lp/result.json"} in desk.brief(
        "lp", "PoolNotInPanelFixture000", src=w.src())["unknowns"]
    packet = desk.brief("lp", POOL, src=w.src())
    assert packet["facts"]["pool.fee_tvl_24h"]["value"] == 0.02
    assert packet["facts"]["lab.forward_n"]["value"] == 12
    assert "pool.per_sol_day" in {u["key"] for u in packet["unknowns"]}


def test_a_missing_lab_is_unknown(w):
    assert {"key": "lab", "reason": "missing", "source": "labs/lp/result.json"} in desk.brief("lp", POOL, src=w.src())["unknowns"]


def test_nft_packet_reads_the_paper_row_and_its_population_base_rates(w):
    old = w.nft(decided=NOW - 100 * H)
    w.nft_mark(old, decided=NOW - 100 * H, exit_basis="none", pnl="-5")
    young = w.nft(decided=NOW - H)
    packet = desk.brief("nft", f"paper:{young}", src=w.src())
    assert packet["facts"]["nft.verdict"]["value"] == "shadow"
    assert packet["facts"]["nft.feature.fills_24h"]["value"] == 4
    assert "nft.feature.fee_recipient" not in packet["facts"]
    assert packet["facts"]["nft.base.real_exit_share_24h"]["value"] == 0.0
    assert packet["facts"]["nft.base.n"]["value"] == 1
    assert {o["id"] for o in packet["outcomes"]} == {"nft_real_exit_24h", "nft_paper_profit_24h", "nft_no_exit_24h"}


def test_nft_features_are_an_allow_list_not_a_filter(w):
    """Pinned directly: the int check beside it also stops a wallet string, which hid this rule from a mutant."""
    packet = desk.brief("nft", f"paper:{w.nft()}", src=w.src())
    named = {k for k in packet["facts"] if k.startswith("nft.feature.")}
    named |= {u["key"] for u in packet["unknowns"] if u["key"].startswith("nft.feature.")}
    assert named == {f"nft.feature.{name}" for name in packets.NFT_FEATURES}


def test_nft_packet_older_than_the_mark_window_allows_offers_no_question(w):
    packet = desk.brief("nft", f"paper:{w.nft(decided=NOW - 7 * H)}", src=w.src())
    assert packet["outcomes"] == []
    assert any(u["key"] == "nft.verdict" and u["reason"].startswith("stale:") for u in packet["unknowns"])


def test_airdrop_packet_an_unpriced_opportunity_is_unknown_not_zero(w):
    w.opportunity(ev=None)
    w.hunter("airdrops_io", last_ok=NOW - H)
    packet = desk.brief("airdrop", OPP, src=w.src())
    assert "opp.ev_score" not in packet["facts"]
    assert "opp.ev_score" in {u["key"] for u in packet["unknowns"]}
    assert packet["facts"]["opp.confirmed_token"]["value"] is False
    assert packet["facts"]["hunter.airdrops_io.last_count"]["value"] == 5
    assert "opp.name" in packet["untrusted_keys"]
    assert question(packet, "airdrop_value_ge_cost_168h")["scoreable"] == "no"


def test_airdrop_packet_a_dead_source_is_unknown(w):
    w.opportunity()
    w.hunter("airdrops_io", last_ok=NOW - 100 * H, fails=9)
    assert "hunter.airdrops_io.last_count" in {u["key"] for u in desk.brief("airdrop", OPP, src=w.src())["unknowns"]}


def test_a_packet_read_under_wal_pressure_is_unknown_not_empty(w, monkeypatch):
    w.token()
    w.dossier()
    monkeypatch.setattr(GuardedReader, "_wal_bytes", lambda self: WAL_LIMIT + 1)
    packet = desk.brief("token", SUBJECT, src=w.src())
    assert packet["facts"] == {}
    reasons = {u["key"]: u["reason"] for u in packet["unknowns"]}
    assert reasons["dossier"].startswith("deferred:wal_over_limit")


def test_every_question_on_the_menu_has_a_resolver():
    assert {(i.domain, i.metric) for i in outcomes.MENU} <= set(outcomes.RESOLVERS)
    assert {i.domain for i in outcomes.MENU} == set(desk.DOMAINS)
    assert len({i.id for i in outcomes.MENU}) == len(outcomes.MENU)


def test_a_malformed_subject_or_domain_is_refused(w):
    with pytest.raises(desk.Refused, match="subject_malformed"):
        desk.brief("token", "not a subject'; DROP TABLE tokens", src=w.src())
    with pytest.raises(desk.Refused, match="unknown_domain"):
        desk.brief("stocks", "AAPL", src=w.src())


# ---------------------------------------------------------------------- queue


def test_queue_takes_each_domain_in_turn_up_to_the_cap(w):
    w.tickers(100.0)
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}])
    w.token()
    w.decision()
    w.swap(NOW - 1000, 0.5)
    paper_id = w.nft()
    w.opportunity()
    out = desk.queue(src=w.src())
    assert [q["domain"] for q in out["queued"]] == ["token", "perps", "lp", "nft", "airdrop"]
    assert {q["subject"] for q in out["queued"]} == {SUBJECT, "BTCUSDT", POOL, f"paper:{paper_id}", OPP}
    assert out["unavailable"] == {}


def test_queue_has_a_hard_ceiling_whatever_is_asked(w):
    for n in range(4):
        w.nft(decided=NOW - H - n)
    w.intel("tickers", {"tickers": [{"symbol": f"C{n}AUSDT", "price": 1.0, "change_pct": n} for n in range(9)]})
    assert len(desk.queue(99, src=w.src())["queued"]) == desk.DEFAULT_CAP == 5
    assert len(desk.queue(2, src=w.src())["queued"]) == 2


def test_queue_skips_a_subject_with_an_open_verdict_and_returns_it_once_due(w):
    perps_verdict(w)
    w.tickers(100.0)
    out = desk.queue(src=w.src())
    assert all(q["subject"] != "BTCUSDT" for q in out["queued"])
    assert out["counts"]["perps"] == {"candidates": 1, "skipped_open": 1}
    later = NOW + 25 * H
    w.tickers(100.0, now=later)
    desk.score(src=w.src(later))
    assert any(q["subject"] == "BTCUSDT" for q in desk.queue(src=w.src(later))["queued"])


def test_queue_reports_an_unreadable_domain_instead_of_an_empty_one(w):
    out = desk.queue(src=w.src())           # no tickers file, no lab file
    assert set(out["unavailable"]) == {"perps", "lp"}
    assert "token" not in out["unavailable"]
    assert out["counts"]["token"] == {"candidates": 0, "skipped_open": 0}


def test_queue_drops_a_token_no_question_could_score(w):
    w.token(migrated_ms=NOW - H)           # graduated already, and no tape to mark a price from
    w.decision()
    assert desk.queue(src=w.src())["counts"]["token"]["candidates"] == 0


def test_queue_puts_a_live_entry_before_a_shadow_one(w):
    other = "TokenFixtureAddress0002"
    w.token()
    w.token(address=other)
    w.decision(ts=NOW - 60_000, token=other, mode="shadow")
    w.decision(ts=NOW - 90_000, mode="live", lane="sm-trenches")
    assert desk.queue(1, src=w.src())["queued"][0]["subject"] == SUBJECT


# ---------------------------------------------------------------------- record


def test_a_valid_verdict_is_stored_in_the_desk_file_only(w):
    out = perps_verdict(w, probability=0.25, evidence=["ticker.price"])
    assert out["recorded"] and out["due_ms"] == NOW + 24 * H and out["authority"].startswith("none")
    row = stored(w)[0]
    assert (row["state"], row["probability"], row["outcome_id"]) == ("open", 0.25, "perp_up3_24h")
    assert json.loads(row["anchor_json"]) == {"price": 100.0}
    assert (w.root / "labs" / "desk" / "desk.db").is_file()
    assert w.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='verdicts'").fetchone()[0] == 0


def test_a_verdict_citing_a_key_not_in_its_packet_is_refused_and_stores_nothing(w):
    with pytest.raises(desk.Refused, match="evidence_key_not_in_packet:ticker.invented"):
        perps_verdict(w, evidence=["ticker.price", "ticker.invented"])
    assert stored(w) == []


def test_a_verdict_citing_a_key_the_packet_lists_as_unknown_is_refused(w):
    with pytest.raises(desk.Refused, match="evidence_key_is_unknown:hl"):
        perps_verdict(w, evidence=["hl"])


def test_a_key_from_another_packet_is_refused(w):
    w.tickers(100.0)
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}])
    lp = desk.brief("lp", POOL, src=w.src())
    with pytest.raises(desk.Refused, match="evidence_key_not_in_packet:ticker.price"):
        desk.record(verdict(lp, "lp_fee_tvl_1pct_24h", evidence=["ticker.price"]), src=w.src())


@pytest.mark.parametrize("probability", [-0.01, 1.01, 2, -1])
def test_a_probability_outside_0_to_1_is_refused(w, probability):
    with pytest.raises(desk.Refused, match="probability_out_of_range"):
        perps_verdict(w, probability=probability)
    assert stored(w) == []


@pytest.mark.parametrize("probability", ["0.5", True, None, [0.5]])
def test_a_probability_that_is_not_a_number_is_refused(w, probability):
    with pytest.raises(desk.Refused, match="probability_not_a_number"):
        perps_verdict(w, probability=probability)


@pytest.mark.parametrize("probability", [0, 1, 0.0, 1.0])
def test_the_ends_of_the_range_are_legal(w, probability):
    assert perps_verdict(w, probability=probability)["recorded"]


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_non_finite_probability_is_refused(w, literal):
    w.tickers(100.0)
    raw = verdict(desk.brief("perps", "BTCUSDT", src=w.src()), "perp_up3_24h", probability=0.123456)
    with pytest.raises(desk.Refused):
        desk.record(raw.replace("0.123456", literal), src=w.src())
    assert stored(w) == []


@pytest.mark.parametrize("over,reason", [
    ({"stance": "maybe"}, "bad_stance"),
    ({"stance": "short"}, "stance_contradicts_outcome"),
    ({"horizon_h": 48}, "horizon_must_be:24"),
    ({"horizon_h": True}, "bad_horizon"),
    ({"horizon_h": 0}, "bad_horizon"),
    ({"outcome": {"metric": "price_return_pct", "op": ">=", "threshold": 2}}, "outcome_not_in_packet_menu"),
    ({"outcome": {"metric": "vibes", "op": ">=", "threshold": 3}}, "outcome_not_in_packet_menu"),
    ({"outcome": {"metric": "price_return_pct", "op": ">="}}, "missing_field:outcome.threshold"),
    ({"outcome": "BTC goes up"}, "outcome_not_an_object"),
    ({"evidence": []}, "forecast_without_evidence"),
    ({"evidence": ["ticker.price", "ticker.price"]}, "bad_evidence_list"),
    ({"roles": {"market": "only one"}}, "missing_field:roles.onchain"),
    ({"roles": {**ROLE_VIEWS, "manager": "  "}}, "bad_role_view:manager"),
    ({"roles": {**ROLE_VIEWS, "manager": "x" * 601}}, "bad_role_view:manager"),
    ({"roles": {**ROLE_VIEWS, "intern": "hi"}}, "unknown_field:roles.intern"),
    ({"confidence": 0.9}, "unknown_field:verdict.confidence"),
    ({"packet_id": "nope"}, "unknown_packet"),
    ({"subject": "ETHUSDT"}, "packet_is_for_another_subject"),
])
def test_a_malformed_verdict_is_refused_with_its_reason(w, over, reason):
    with pytest.raises(desk.Refused, match=reason):
        perps_verdict(w, **over)
    assert stored(w) == []


def test_only_perps_have_a_short_stance(w):
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}])
    packet = desk.brief("lp", POOL, src=w.src())
    with pytest.raises(desk.Refused, match="no_short_venue:lp"):
        desk.record(verdict(packet, "lp_tvl_down50_72h", stance="short"), src=w.src())
    assert perps_verdict(w, "perp_down3_24h", stance="short")["recorded"]


@pytest.mark.parametrize("stance,outcome_id", [("avoid", "perp_up3_24h"), ("long", "perp_down3_24h")])
def test_a_stance_must_agree_with_its_question(w, stance, outcome_id):
    with pytest.raises(desk.Refused, match="stance_contradicts_outcome"):
        perps_verdict(w, outcome_id, stance=stance)


def test_duplicate_keys_and_bad_json_are_refused(w):
    with pytest.raises(desk.Refused, match="invalid_json"):
        desk.record("{not json", src=w.src())
    with pytest.raises(desk.Refused, match="duplicate_key"):
        desk.record('{"stance":"long","stance":"avoid"}', src=w.src())
    with pytest.raises(desk.Refused, match="verdict_not_an_object"):
        desk.record("[1]", src=w.src())


def test_no_view_is_recorded_with_no_forecast_and_is_never_scored(w):
    w.tickers(100.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    desk.record(verdict(packet, "perp_up3_24h", stance="no_view", probability=None, outcome=None, evidence=[]), src=w.src())
    assert stored(w)[0]["state"] == "open" and stored(w)[0]["probability"] is None
    desk.score(src=w.src(NOW + 25 * H))
    assert stored(w)[0]["state"] == "abstained" and stored(w)[0]["brier"] is None
    summary = desk.report(src=w.src(NOW + 25 * H))["by_stance"]["no_view"]
    assert (summary["n_abstained"], summary["n_resolved"]) == (1, 0)


def test_no_view_needs_no_horizon(w):
    w.tickers(100.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    body = verdict(packet, "perp_up3_24h", stance="no_view", probability=None, outcome=None, evidence=[], horizon_h=None)
    assert desk.record(body, src=w.src())["recorded"] is True


def test_a_forecast_still_needs_its_horizon(w):
    w.tickers(100.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    with pytest.raises(desk.Refused, match="bad_horizon"):
        desk.record(verdict(packet, "perp_up3_24h", horizon_h=None), src=w.src())


def test_no_view_may_not_smuggle_in_a_forecast(w):
    with pytest.raises(desk.Refused, match="no_view_carries_no_forecast"):
        perps_verdict(w, stance="no_view", probability=0.7)


def test_a_subject_holds_one_open_verdict(w):
    perps_verdict(w)
    w.tickers(101.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src(NOW + 1000))
    with pytest.raises(desk.Refused, match="open_verdict_exists"):
        desk.record(verdict(packet, "perp_down3_24h"), src=w.src(NOW + 1000))
    assert len(stored(w)) == 1


def test_a_verdict_cannot_rest_on_an_old_packet(w):
    w.tickers(100.0)
    packet = desk.brief("perps", "BTCUSDT", src=w.src())
    with pytest.raises(desk.Refused, match="packet_expired"):
        desk.record(verdict(packet, "perp_up3_24h"), src=w.src(NOW + 2 * H + 1))


def test_a_recorded_forecast_can_never_be_edited_or_deleted(w):
    perps_verdict(w)
    with closing(desk._connect(w.src())) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            conn.execute("UPDATE verdicts SET probability=0.99")
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            conn.execute("UPDATE verdicts SET due_ms=due_ms+1")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            conn.execute("DELETE FROM verdicts")
    assert stored(w)[0]["probability"] == 0.4


# ---------------------------------------------------------------------- score


def test_a_verdict_before_its_horizon_is_left_alone(w):
    perps_verdict(w)
    assert desk.score(src=w.src(NOW + 23 * H))["checked"] == 0
    assert stored(w)[0]["state"] == "open"


@pytest.mark.parametrize("price,y", [(103.0, 1), (102.99, 0)])
def test_perps_resolves_from_the_first_ticker_past_due(w, price, y):
    perps_verdict(w, probability=0.25)
    due = NOW + 24 * H
    w.tickers(price, now=due + 120_000, age_s=60)
    assert desk.score(src=w.src(due + 120_000))["resolved"] == 1
    row = stored(w)[0]
    assert (row["state"], row["y"], row["mark_source"]) == ("resolved", y, "market-intel/tickers")
    assert row["brier"] == pytest.approx((0.25 - y) ** 2)
    assert row["mark_value"] == pytest.approx(price - 100.0)


def test_a_ticker_from_before_due_settles_nothing(w):
    perps_verdict(w)
    due = NOW + 24 * H
    w.tickers(200.0, now=due - 60_000)
    desk.score(src=w.src(due + 1000))
    row = stored(w)[0]
    assert row["state"] == "unresolved" and row["y"] is None
    assert row["reason"] == "no_price_mark_near_due"


def test_a_stale_marked_ticker_settles_nothing(w):
    perps_verdict(w)
    due = NOW + 24 * H
    w.intel("tickers", {"tickers": [{"symbol": "BTCUSDT", "price": 200.0}]}, now=due + 60_000, stale=True)
    desk.score(src=w.src(due + 60_000))
    assert stored(w)[0]["state"] == "unresolved"


def test_a_ticker_past_the_tolerance_settles_nothing(w):
    perps_verdict(w)
    due = NOW + 24 * H
    w.tickers(200.0, now=due + 3 * H)        # 24 h horizon: tolerance is 2.4 h
    desk.score(src=w.src(due + 3 * H))
    assert stored(w)[0]["reason"] == "no_price_mark_near_due"


def test_perps_falls_back_to_the_durable_native_series(w):
    w.tickers(100.0, symbol="SOLUSDT")
    desk.record(verdict(desk.brief("perps", "SOLUSDT", src=w.src()), "perp_down3_24h"), src=w.src())
    due = NOW + 24 * H
    w.run("INSERT INTO native_prices(chain,ts_ms,price_usd,source) VALUES ('sol',?,'96.0','dexscreener')", (due + 60_000,))
    desk.score(src=w.src(due + 5 * H))       # the pass ran late; the series still has the price at due
    row = stored(w)[0]
    assert (row["state"], row["y"], row["mark_source"]) == ("resolved", 1, "native_prices:sol")


def token_verdict(w, outcome_id="token_up50_24h", **over):
    w.token()
    w.swap(NOW - 1000, 2.0)
    packet = desk.brief("token", SUBJECT, src=w.src())
    return desk.record(verdict(packet, outcome_id, **over), src=w.src())


def test_a_token_outcome_with_no_mark_stays_unresolved_with_its_reason(w):
    token_verdict(w)
    desk.score(src=w.src(NOW + 25 * H))
    row = stored(w)[0]
    assert (row["state"], row["reason"]) == ("unresolved", "no_print_near_due")
    assert row["y"] is None and row["brier"] is None


def test_token_price_only_marks_against_its_own_source(w):
    token_verdict(w)
    due = NOW + 24 * H
    w.swap(due + 60_000, 500.0, source="gmgn:smartmoney")     # another scale, same token (AGENTS measured this)
    desk.score(src=w.src(due + H))
    assert stored(w)[0]["reason"] == "no_print_near_due"
    w.swap(due + 120_000, 3.0)
    desk.score(src=w.src(due + H))
    row = stored(w)[0]
    assert (row["state"], row["y"], row["mark_source"]) == ("resolved", 1, "swaps:pumpfun")
    assert row["mark_value"] == pytest.approx(50.0)


def test_a_bogus_mark_is_unresolved_not_a_win(w):
    token_verdict(w)
    due = NOW + 24 * H
    w.swap(due + 60_000, 2.0 * 101)
    desk.score(src=w.src(due + H))
    row = stored(w)[0]
    assert row["state"] == "unresolved" and row["reason"].startswith("suspect_mark")


def graduation_verdict(w, *, chain="sol", subject=SUBJECT, address=TOKEN):
    w.token(chain=chain, address=address)
    packet = desk.brief("token", subject, src=w.src())
    return desk.record(verdict(packet, "token_graduates_24h", evidence=["token.age_s"]), src=w.src())


@pytest.mark.parametrize("migrated,y", [(NOW + 3 * H, 1), (None, 0), (NOW + 25 * H, 0)])
def test_graduation_resolves_both_ways_when_the_feed_is_proven_alive(w, migrated, y):
    graduation_verdict(w)
    w.graduation_event(NOW + H)
    w.run("UPDATE tokens SET migrated_ms=? WHERE address=?", (migrated, TOKEN))
    desk.score(src=w.src(NOW + 26 * H))
    assert (stored(w)[0]["state"], stored(w)[0]["y"]) == ("resolved", y)


@pytest.mark.parametrize("migrated", [NOW + 3 * H, None])
def test_a_silent_graduation_feed_settles_neither_answer(w, migrated):
    """Both directions: if only "yes" could resolve, every scored row would be a hit."""
    graduation_verdict(w)
    w.graduation_event(NOW + H, chain="robinhood")         # another chain's feed proves nothing here
    w.run("UPDATE tokens SET migrated_ms=? WHERE address=?", (migrated, TOKEN))
    desk.score(src=w.src(NOW + 26 * H))
    row = stored(w)[0]
    assert (row["state"], row["reason"], row["y"]) == ("unresolved", "graduation_feed_not_proven_alive", None)


def test_graduation_is_not_offered_where_no_feed_can_prove_it(w):
    w.token(chain="bsc")
    packet = desk.brief("token", f"bsc:{TOKEN}", src=w.src())
    assert all(o["id"] != "token_graduates_24h" for o in packet["outcomes"])


def test_graduation_is_not_offered_on_a_token_already_graduated(w):
    w.token(migrated_ms=NOW - H)
    assert desk.brief("token", SUBJECT, src=w.src())["outcomes"] == []


def lp_verdict(w, outcome_id="lp_fee_tvl_1pct_24h"):
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}, {"address": "PoolFixtureAddress000002", "tvl": 5.0}])
    return desk.record(verdict(desk.brief("lp", POOL, src=w.src()), outcome_id), src=w.src())


@pytest.mark.parametrize("fee,y", [(0.01, 1), (0.0099, 0)])
def test_lp_fee_resolves_from_the_first_file_past_due(w, fee, y):
    lp_verdict(w)
    due = NOW + 24 * H
    w.lab([{"address": POOL, "tvl": 900.0, "fee_tvl_24h": fee}], at_ms=due + H)
    desk.score(src=w.src(due + 2 * H))
    assert (stored(w)[0]["state"], stored(w)[0]["y"]) == ("resolved", y)


def test_lp_keeps_waiting_for_a_file_past_due_then_expires(w):
    lp_verdict(w)
    due = NOW + 24 * H
    desk.score(src=w.src(due + H))           # the file on disk is still the old one
    assert stored(w)[0]["reason"] == "lab_not_yet_past_due"
    w.lab([{"address": POOL, "fee_tvl_24h": 0.5}], at_ms=due + 40 * H)
    desk.score(src=w.src(due + 41 * H))
    assert stored(w)[0]["reason"] == "lab_file_too_late"


def test_a_pool_the_api_dropped_is_a_real_loss_not_a_gap(w):
    lp_verdict(w, "lp_tvl_down50_72h")
    due = NOW + 72 * H
    w.lab([{"address": POOL, "gone": True, "tvl": None}, {"address": "PoolFixtureAddress000002", "tvl": 5.0},
           {"address": "PoolFixtureAddress000003", "tvl": 5.0}], at_ms=due + H)
    desk.score(src=w.src(due + 2 * H))
    row = stored(w)[0]
    assert (row["state"], row["y"], row["mark_value"]) == ("resolved", 1, -100.0)


def test_an_api_outage_is_not_read_as_every_pool_gone(w):
    lp_verdict(w, "lp_tvl_down50_72h")
    due = NOW + 72 * H
    w.lab([{"address": POOL, "gone": True, "tvl": None}, {"address": "PoolFixtureAddress000002", "gone": True}], at_ms=due + H)
    desk.score(src=w.src(due + 2 * H))
    assert stored(w)[0]["reason"] == "panel_mostly_gone"


def nft_verdict(w, outcome_id="nft_real_exit_24h", decided=NOW - H):
    paper_id = w.nft(decided=decided)
    desk.record(verdict(desk.brief("nft", f"paper:{paper_id}", src=w.src()), outcome_id), src=w.src())
    return paper_id


@pytest.mark.parametrize("basis,pnl,outcome_id,y", [
    ("offer_median", "-3", "nft_real_exit_24h", 1), ("none", "-3", "nft_real_exit_24h", 0),
    ("offer_median", "5", "nft_paper_profit_24h", 1), ("offer_median", "-3", "nft_paper_profit_24h", 0),
    ("none", "-3", "nft_no_exit_24h", 1),
])
def test_nft_resolves_from_the_paper_mark(w, basis, pnl, outcome_id, y):
    decided = NOW - H
    paper_id = nft_verdict(w, outcome_id, decided)
    w.nft_mark(paper_id, decided=decided, exit_basis=basis, pnl=pnl)
    desk.score(src=w.src(NOW + 25 * H))
    assert (stored(w)[0]["state"], stored(w)[0]["y"]) == ("resolved", y)


def test_an_nft_mark_not_written_yet_is_retried_and_then_resolved(w):
    decided = NOW - H
    paper_id = nft_verdict(w, decided=decided)
    desk.score(src=w.src(NOW + 25 * H))
    assert stored(w)[0]["reason"] == "mark_not_written"
    w.nft_mark(paper_id, decided=decided)
    desk.score(src=w.src(NOW + 26 * H))
    assert stored(w)[0]["state"] == "resolved"


def test_an_nft_mark_window_that_opened_before_the_verdict_is_look_ahead(w):
    decided = NOW - H
    paper_id = nft_verdict(w, decided=decided)
    w.nft_mark(paper_id, decided=decided, half=24 * H)
    desk.score(src=w.src(NOW + 25 * H))
    assert stored(w)[0]["reason"] == "mark_window_opened_before_verdict"


def airdrop_verdict(w, outcome_id):
    w.opportunity(last_seen=NOW - H)
    w.hunter("airdrops_io", last_ok=NOW - H)
    desk.record(verdict(desk.brief("airdrop", OPP, src=w.src()), outcome_id), src=w.src())


@pytest.mark.parametrize("seen_after,y", [(160, 0), (100, 1)])
def test_a_delisting_resolves_when_its_own_source_is_proven_alive(w, seen_after, y):
    airdrop_verdict(w, "airdrop_delisted_168h")
    due = NOW + 168 * H
    w.opportunity(last_seen=NOW + seen_after * H)
    w.hunter("airdrops_io", last_ok=due + H)
    desk.score(src=w.src(due + 2 * H))
    assert (stored(w)[0]["state"], stored(w)[0]["y"]) == ("resolved", y)


@pytest.mark.parametrize("last_ok,fails", [(NOW + H, 0), (NOW + 170 * H, 9)])
def test_a_dead_source_cannot_delist_what_it_fed(w, last_ok, fails):
    airdrop_verdict(w, "airdrop_delisted_168h")
    due = NOW + 168 * H
    w.hunter("airdrops_io", last_ok=last_ok, fails=fails)
    desk.score(src=w.src(due + 3 * H))
    assert stored(w)[0]["reason"] == "source_not_proven_healthy_after_due"


def test_a_joined_source_name_is_proven_by_any_member(w):
    airdrop_verdict(w, "airdrop_delisted_168h")
    due = NOW + 168 * H
    w.opportunity(last_seen=NOW + 160 * H, source="defillama,alphadrops")
    w.hunter("alphadrops", last_ok=due + H)
    desk.score(src=w.src(due + 2 * H))
    assert stored(w)[0]["state"] == "resolved"


def test_a_token_confirmation_resolves_and_a_late_check_does_not(w):
    airdrop_verdict(w, "airdrop_token_confirmed_168h")
    due = NOW + 168 * H
    w.opportunity(last_seen=due, confirmed=True)
    w.hunter("airdrops_io", last_ok=due + H)
    desk.score(src=w.src(due + 49 * H))
    assert stored(w)[0]["reason"] == "checked_too_late"
    desk.score(src=w.src(due + H))
    assert (stored(w)[0]["state"], stored(w)[0]["y"]) == ("resolved", 1)


def test_whether_an_airdrop_paid_stays_unresolved_with_its_reason(w):
    airdrop_verdict(w, "airdrop_value_ge_cost_168h")
    w.hunter("airdrops_io", last_ok=NOW + 200 * H)
    desk.score(src=w.src(NOW + 169 * H))
    row = stored(w)[0]
    assert (row["state"], row["reason"], row["brier"]) == ("unresolved", "no_claim_or_sale_data_in_kaiba", None)


def test_a_busy_database_defers_scoring_and_settles_nothing(w, monkeypatch):
    token_verdict(w)
    monkeypatch.setattr(GuardedReader, "_wal_bytes", lambda self: WAL_LIMIT + 1)
    out = desk.score(src=w.src(NOW + 25 * H))
    assert (out["deferred"], out["unresolved"], out["resolved"]) == (1, 0, 0)
    assert stored(w)[0]["state"] == "open"


def test_scoring_never_widens_past_the_retry_window(w):
    token_verdict(w)
    desk.score(src=w.src(NOW + 25 * H))
    assert desk.score(src=w.src(NOW + 24 * H + desk.RETRY_MS + 1))["checked"] == 0


# ---------------------------------------------------------------------- report


def seed(w, rows):
    """Resolved history directly in the file: ``rows`` is (domain, stance, question, probability, y|None, reason)."""
    with closing(desk._connect(w.src())) as conn, conn:
        for n, (domain, stance, outcome_id, probability, y, reason) in enumerate(rows):
            state = "resolved" if y is not None else "unresolved"
            conn.execute("INSERT INTO verdicts(verdict_id,packet_id,domain,subject,stance,horizon_h,probability,outcome_id,"
                         "roles_json,evidence_json,issued_ms,due_ms,state,reason,y,brier) "
                         "VALUES (?,'p',?,?,?,24,?,?,'{}','[]',1,2,?,?,?,?)",
                         (f"v{n}", domain, f"s{n}", stance, probability, outcome_id, state, reason, y,
                          None if y is None else (probability - y) ** 2))


def test_no_skill_number_is_shown_below_30_resolved(w):
    seed(w, [("perps", "long", "q", 0.5, n % 2, None) for n in range(29)])
    out = desk.report(src=w.src())["by_domain"]["perps"]
    assert out["n_resolved"] == 29
    assert (out["brier"], out["base_rate"], out["reference_brier"], out["skill"]) == (None, None, None, None)
    assert out["withheld"] == "n=29 below 30"


def test_the_skill_number_appears_at_exactly_30(w):
    seed(w, [("perps", "long", "q", 0.5, n % 2, None) for n in range(30)])
    out = desk.report(src=w.src())["by_domain"]["perps"]
    assert out["brier"] == 0.25 and out["base_rate"] == 0.5 and out["skill"] == 0.0
    assert "withheld" not in out


def test_skill_is_measured_against_the_base_rate(w):
    # 40 forecasts; 10 of 40 happen. Saying 0.2 on the 32 that fail 8 times is better than 0.25 flat.
    rows = [("perps", "long", "q", 0.1, 1 if n < 2 else 0, None) for n in range(20)]
    rows += [("perps", "long", "q", 0.4, 1 if n < 8 else 0, None) for n in range(20)]
    seed(w, rows)
    out = desk.report(src=w.src())["by_domain"]["perps"]
    brier = (2 * 0.81 + 18 * 0.01 + 8 * 0.36 + 12 * 0.16) / 40
    reference = 0.25 * 0.75
    assert out["base_rate"] == 0.25
    assert out["brier"] == pytest.approx(brier, abs=1e-4)
    assert out["reference_brier"] == pytest.approx(reference, abs=1e-4)
    assert out["skill"] == pytest.approx(1 - brier / reference, abs=1e-3)


def test_knowing_which_question_is_rare_earns_no_skill(w):
    """Saying each question's own base rate is zero skill, however different the questions are."""
    rows = [("token", "long", "rare", 0.1, 1 if n < 3 else 0, None) for n in range(30)]
    rows += [("token", "long", "common", 0.9, 1 if n < 27 else 0, None) for n in range(30)]
    seed(w, rows)
    assert desk.report(src=w.src())["by_domain"]["token"]["skill"] == pytest.approx(0.0, abs=1e-3)


def test_an_unresolved_verdict_is_counted_and_shown_but_never_scored(w):
    rows = [("token", "avoid", "q", 0.5, n % 2, None) for n in range(30)]
    clean_brier = desk._group([dict(zip(("state", "reason", "outcome_id", "y", "brier"),
                                        ("resolved", None, "q", n % 2, 0.25), strict=True)) for n in range(30)],
                              {"q": 0.5})["brier"]
    rows += [("token", "avoid", "q", 0.99, None, "no_print_near_due") for _ in range(7)]
    rows += [("token", "avoid", "q", 0.99, None, "suspect_mark:150x")]
    seed(w, rows)
    report = desk.report(src=w.src())
    out = report["by_domain"]["token"]
    assert (out["n_total"], out["n_resolved"], out["n_unresolved"]) == (38, 30, 8)
    assert out["unresolved_reasons"] == {"no_print_near_due": 7, "suspect_mark": 1}
    assert out["brier"] == clean_brier == 0.25            # p=0.99 on eight unresolved rows moved nothing
    assert report["unresolved_share"] == pytest.approx(8 / 38, abs=1e-4)


def test_report_groups_by_domain_stance_and_question(w):
    seed(w, [("perps", "long", "a", 0.5, 1, None), ("perps", "short", "b", 0.5, 0, None),
             ("lp", "long", "c", 0.5, 1, None)])
    report = desk.report(src=w.src())
    assert set(report["by_domain"]) == {"perps", "lp"}
    assert set(report["by_stance"]) == {"long", "short"}
    assert set(report["by_domain_stance"]) == {"perps/long", "perps/short", "lp/long"}
    assert set(report["by_question"]) == {"a", "b", "c"}
    assert report["all"]["n_total"] == 3


def test_the_calibration_table_withholds_a_thin_bin(w):
    seed(w, [("perps", "long", "q", 0.25, 1 if n < 9 else 0, None) for n in range(30)] +
         [("perps", "long", "q", 0.85, 1, None) for _ in range(5)])
    table = {row["p_from"]: row for row in desk.report(src=w.src())["calibration"]}
    assert table[0.2] == {"p_from": 0.2, "p_to": 0.3, "n": 30, "mean_p": 0.25, "hit_rate": 0.3}
    assert table[0.8]["n"] == 5 and table[0.8]["hit_rate"] is None


def test_a_report_prints_aggregates_only(w):
    perps_verdict(w, roles={**ROLE_VIEWS, "bull": "sentinel-role-text"})
    dump = json.dumps(desk.report(src=w.src()))
    assert "sentinel-role-text" not in dump and "BTCUSDT" not in dump


def test_an_empty_desk_reports_cleanly(w):
    report = desk.report(src=w.src())
    assert report["all"]["n_total"] == 0 and report["unresolved_share"] is None and report["calibration"] == []


# ---------------------------------------------------------------------- monitor


EMPTY = '{"authority":"none","batch_id":null,"queued":[]}'


def test_an_empty_queue_prints_the_same_bytes_every_tick(w):
    assert desk.monitor(src=w.src()) == EMPTY
    assert desk.monitor(src=w.src(NOW + 50 * H)) == EMPTY


def test_the_model_is_woken_at_most_once_per_batch_window(w):
    w.tickers(100.0)
    first = desk.monitor(src=w.src())
    assert json.loads(first)["queued"][0]["subject"] == "BTCUSDT"
    w.tickers(101.0, now=NOW + H)
    assert desk.monitor(src=w.src(NOW + H)) == first              # the queue still has work: no wake
    later = NOW + desk.BATCH_EVERY_MS
    w.tickers(102.0, now=later)
    assert desk.monitor(src=w.src(later)) != first


def test_a_queue_draining_to_empty_does_not_change_the_bytes(w):
    """Hermes wakes on any change, so the drain must not read as one."""
    w.tickers(100.0)
    first = desk.monitor(src=w.src())
    later = NOW + desk.BATCH_EVERY_MS + H                         # tickers file is now stale: queue empty
    assert desk.queue(src=w.src(later))["queued"] == []
    assert desk.monitor(src=w.src(later)) == first


def test_the_monitor_output_holds_no_clock_that_would_wake_the_model(w):
    w.tickers(100.0)
    assert str(NOW) not in desk.monitor(src=w.src())


def test_the_monitor_settles_due_verdicts_with_no_model(w):
    perps_verdict(w)
    due = NOW + 24 * H
    w.tickers(104.0, now=due + 60_000)
    desk.monitor(src=w.src(due + 60_000))
    assert stored(w)[0]["state"] == "resolved"


def monitor_module():
    spec = importlib.util.spec_from_file_location("desk_monitor", REPO / "deploy/desk-monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_monitor_script_passes_the_bytes_through_exactly(monkeypatch, capsys):
    module = monitor_module()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=EMPTY, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert module.main() == 0
    assert capsys.readouterr().out == EMPTY
    assert calls[0][:9] == ["timeout", "-k", "2", "100", "nice", "-n", "19", "ionice", "-c3"]
    assert calls[0][-3:] == ["-m", "kaiba.learning.desk", "monitor"]


def test_a_failed_monitor_exits_non_zero_so_hermes_sees_an_error_not_a_change(monkeypatch, capsys):
    module = monitor_module()
    monkeypatch.setattr(subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 3, stdout="", stderr="boom"))
    assert module.main() == 3
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------- no authority


FORBIDDEN_OUTWARD = ("kaiba.execution", "kaiba.providers", "kaiba.ops", "kaiba.mcp", "kaiba.hunters", "kaiba.ingest",
                     "httpx", "requests", "aiohttp", "websockets", "urllib", "http", "socket", "subprocess", "web3")
DESK_FILES = ("kaiba/learning/desk.py", "kaiba/learning/desk_packets.py", "kaiba/learning/desk_outcomes.py")


def imports_of(source: str) -> set[str]:
    """Every name a file reaches by import statement, or by a string passed to any call."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names |= {f"{module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Call):
            names |= {a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    return names


def reaches_desk(source: str) -> bool:
    return any(name.startswith("kaiba.learning.desk") for name in imports_of(source))


@pytest.mark.parametrize("source", [
    "import kaiba.learning.desk", "from kaiba.learning import desk", "from kaiba.learning.desk import record",
    "from kaiba.learning import desk_outcomes", "import importlib\nimportlib.import_module('kaiba.learning.desk')",
    "__import__('kaiba.learning.desk')", "def f():\n    from kaiba.learning import desk_packets as p\n    return p",
])
def test_the_authority_scan_catches_every_way_in(source):
    """A test that passes with the guard removed is not a test: the scan must see each form."""
    assert reaches_desk(source)


def test_the_authority_scan_is_not_fooled_by_a_neighbour():
    assert not reaches_desk("from kaiba.learning import labs\nimport kaiba.learning.gates")


def test_nothing_in_kaiba_imports_the_desk():
    scanned = 0
    offenders = []
    for path in sorted((REPO / "kaiba").rglob("*.py")):
        scanned += 1
        relative = path.relative_to(REPO).as_posix()
        if relative not in DESK_FILES and reaches_desk(path.read_text(encoding="utf-8")):
            offenders.append(relative)
    assert offenders == []
    assert scanned > 100              # a glob that matched nothing would pass this silently


@pytest.mark.parametrize("target", ["kaiba/execution", "kaiba/ops/scheduler.py", "kaiba/mcp/server.py"])
def test_the_paths_the_owner_named_are_really_in_the_scan(target):
    path = REPO / target
    files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
    assert files, target
    assert not any(reaches_desk(f.read_text(encoding="utf-8")) for f in files)


@pytest.mark.parametrize("name", ["config/risk.yaml", "config/schedule.yaml", "config/signer-policy.yaml"])
def test_no_config_runs_or_reads_the_desk(name):
    assert not re.search(r"\bdesk\b", (REPO / name).read_text(encoding="utf-8"), re.IGNORECASE)


def test_the_desk_reaches_no_trade_provider_or_network_module():
    for name in DESK_FILES:
        reached = imports_of((REPO / name).read_text(encoding="utf-8"))
        assert not {r for r in reached for f in FORBIDDEN_OUTWARD if r == f or r.startswith(f + ".")}, name


def test_the_desk_opens_only_its_own_file_for_writing():
    connects = []
    for name in DESK_FILES:
        for node in ast.walk(ast.parse((REPO / name).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "connect":
                connects.append((name, node.args[0].left.id if isinstance(node.args[0], ast.BinOp) else None))
    assert connects == [("kaiba/learning/desk.py", "folder")]


def test_importing_the_desk_loads_no_trade_or_network_code():
    probe = ("import sys, json, kaiba.learning.desk\n"
             "print(json.dumps(sorted(m for m in sys.modules)))\n")
    result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-400:]
    loaded = json.loads(result.stdout)
    assert not [m for m in loaded if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_OUTWARD if f != "http"
                                         and f != "urllib" and f != "socket" and f != "subprocess")]


def test_a_full_cycle_touches_no_network_and_leaves_the_live_database_unchanged(w, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the desk must make no network call")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    w.tickers(100.0)
    w.lab([{"address": POOL, "tvl": 1000.0, "fee_tvl_24h": 0.02}])
    w.token()
    w.decision()
    w.swap(NOW - 1000, 0.5)
    paper_id = w.nft()
    w.opportunity()
    w.hunter("airdrops_io", last_ok=NOW - H)
    w.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    tables = w.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
    live = w.root / "kaiba.db"
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    desk.queue(src=w.src())
    for domain, subject in (("token", SUBJECT), ("perps", "BTCUSDT"), ("lp", POOL), ("nft", f"paper:{paper_id}"), ("airdrop", OPP)):
        packet = desk.brief(domain, subject, src=w.src())
        assert packet["outcomes"], domain
        desk.record(verdict(packet), src=w.src())
    later = NOW + 200 * H
    desk.score(src=w.src(later))
    desk.report(src=w.src(later))
    desk.monitor(src=w.src(later))
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    assert w.conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == tables
    assert len(stored(w)) == 5


def test_a_read_honours_the_wal_guard_before_opening_the_live_database(w, monkeypatch):
    """The rule itself: over the limit, the live database is not even opened. Files still work."""
    w.token()
    w.decision()
    w.tickers(100.0)
    real_connect = sqlite3.connect
    opened = []

    def spy(database, *args, **kwargs):
        opened.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(GuardedReader, "_wal_bytes", lambda self: WAL_LIMIT + 1)
    out = desk.queue(src=w.src())
    assert not [p for p in opened if "kaiba.db" in p]
    assert set(out["unavailable"]) >= {"token", "nft", "airdrop"}
    assert "wal_over_limit" in out["unavailable"]["token"]
    assert out["queued"][0]["subject"] == "BTCUSDT"                 # file-backed domains do not touch the database
    monkeypatch.setattr(GuardedReader, "_wal_bytes", lambda self: WAL_LIMIT)
    opened.clear()
    assert desk.queue(src=w.src())["queued"][0]["domain"] == "token"
    assert [p for p in opened if "kaiba.db" in p]                   # positive control: at the limit it reads


# ---------------------------------------------------------------------- cli


def test_the_cli_runs_the_whole_flow(w, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(packets.time, "time", lambda: NOW / 1000)
    w.tickers(100.0)
    assert desk.main(["queue", "--cap", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["queued"][0]["subject"] == "BTCUSDT"
    assert desk.main(["brief", "perps", "BTCUSDT"]) == 0
    packet = json.loads(capsys.readouterr().out)
    path = tmp_path / "verdict.json"
    path.write_text(verdict(packet, "perp_up3_24h"), encoding="utf-8")
    assert desk.main(["record", "--file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True
    assert desk.main(["score"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == 0
    assert desk.main(["report"]) == 0
    assert json.loads(capsys.readouterr().out)["all"]["n_open"] == 1


def test_the_cli_reports_a_refusal_with_exit_2(w, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{broken"))
    assert desk.main(["record"]) == 2
    assert json.loads(capsys.readouterr().out) == {"recorded": False, "refused": "invalid_json"}


def test_the_cli_monitor_prints_only_the_stable_bytes(w, monkeypatch, capsys):
    monkeypatch.setattr(packets.time, "time", lambda: NOW / 1000)
    assert desk.main(["monitor"]) == 0
    assert capsys.readouterr().out == EMPTY


# ---------------------------------------------------------------------- cron entry and installer


def job():
    jobs = yaml.safe_load((REPO / "hermes/cron/jobs.yaml").read_text(encoding="utf-8"))["jobs"]
    return next(j for j in jobs if j["name"] == "kaiba-analyst-desk")


def installer():
    spec = importlib.util.spec_from_file_location("desk_install", REPO / "deploy/desk-model-install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cron_entry_is_paused_cheap_and_gated():
    entry = job()
    assert entry["paused"] is True and "lead review" in entry["paused_reason"]
    assert entry["model"] == "claude-haiku-4-5" and entry["reasoning_effort"] == "low"
    assert entry["profile"] == "kaiba-operator" and entry["deliver"] == "local"
    assert entry["monitor_script"] == "kaiba-desk-monitor.py"
    assert entry["skills"] == ["analyst-desk"]


def test_the_entry_cannot_fall_through_to_the_generic_installer():
    """install-hermes.sh skips only entries with `installer`, and its generic path ignores `paused`."""
    entry = job()
    assert (REPO / entry["installer"]).is_file()
    script = (REPO / "deploy/install-hermes.sh").read_text(encoding="utf-8")
    assert 'if job.get("installer"):' in script
    module = installer()
    assert module.JOB == entry["name"]
    assert "scripts/" + entry["monitor_script"] in module.SOURCES.values()
    assert set(module.REQUIRES) == set(DESK_FILES)


def install_setup(tmp_path, monkeypatch, *, apply=False, listing="existing unrelated job\n", paused=True):
    module = installer()
    root, home = tmp_path / "kaiba", tmp_path / "home"
    (home / ".hermes/profiles/kaiba-operator").mkdir(parents=True)
    required = {}
    for rel in module.REQUIRES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
        required[rel] = hashlib.sha256(rel.encode()).hexdigest()
    files = {}
    for rel, destination in module.SOURCES.items():
        raw = ("new " + rel).encode()
        files[rel] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest(),
                      "profile_path": destination}
    entry = {**job(), "paused": paused}
    data = {"files": files, "requires": required, "job": entry, "apply": apply, "commit": "fixture"}
    code = module.REMOTE.replace("Path('/home/ubuntu/kaiba')", f"Path({str(root)!r})")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=listing, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return code, root, home / ".hermes/profiles/kaiba-operator", commands


def test_install_check_writes_nothing_and_creates_nothing(tmp_path, monkeypatch, capsys):
    code, root, profile, commands = install_setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        exec(compile(code, "installer", "exec"), {})
    assert stopped.value.code == 0
    assert json.loads(capsys.readouterr().out)["checked_files"] == 4
    assert not (profile / "scripts").exists()
    assert not (root / "deploy").exists()
    assert not any("create" in c for c in commands)


def test_install_apply_creates_the_job_paused_with_its_monitor(tmp_path, monkeypatch, capsys):
    code, root, profile, commands = install_setup(tmp_path, monkeypatch, apply=True)
    exec(compile(code, "installer", "exec"), {})
    assert (profile / "scripts/kaiba-desk-monitor.py").read_bytes() == b"new deploy/desk-monitor.py"
    create = next(c for c in commands if "create" in c)
    assert "--paused" in create
    assert create[create.index("--monitor-script") + 1] == "kaiba-desk-monitor.py"
    assert create[create.index("--model") + 1] == "claude-haiku-4-5"
    assert json.loads(capsys.readouterr().out)["financial_actions"] == 0


def test_install_refuses_an_unpaused_job(tmp_path, monkeypatch):
    code, _, _, commands = install_setup(tmp_path, monkeypatch, apply=True, paused=False)
    with pytest.raises(AssertionError):
        exec(compile(code, "installer", "exec"), {})
    assert commands == []


def test_install_refuses_until_the_desk_code_is_on_the_box(tmp_path, monkeypatch):
    code, root, profile, commands = install_setup(tmp_path, monkeypatch, apply=True)
    (root / "kaiba/learning/desk.py").unlink()
    with pytest.raises(RuntimeError, match="required_module_not_deployed"):
        exec(compile(code, "installer", "exec"), {})
    assert not (profile / "scripts").exists() and commands == []


def test_install_aborts_before_any_write_if_a_target_has_other_bytes(tmp_path, monkeypatch):
    code, root, profile, commands = install_setup(tmp_path, monkeypatch, apply=True)
    path = profile / "skills/analyst-desk/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("another agent's edit")
    with pytest.raises(RuntimeError, match="owned_source_changed"):
        exec(compile(code, "installer", "exec"), {})
    assert path.read_text() == "another agent's edit"
    assert not (root / "deploy").exists()
    assert not any("create" in c for c in commands)


def test_no_fixture_here_looks_like_a_real_address():
    """The repo's secret scan flags 43-44 char base58 strings. Fixtures are shorter on purpose."""
    text = Path(__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b[1-9A-HJ-NP-Za-km-z]{43,44}\b", text)
