"""launch-snipe after its first live day (2026-10-04): the daily cap counts entries, the
service bounds what it holds, and the observations record peaks that are what they say."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_one, jload
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import robinhood as rh

needs_lane = pytest.mark.skipif(snipe.LANE_VALUE not in {x.value for x in Lane}, reason="Lane 'launch-snipe' not wired")

T0 = 1_791_000_000
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
TOKEN = "0x9122cbc7a76c8518989aae029148d331538dadb7"


def sol_launch(token: str = "Mint1111", **over) -> lf.Launch:
    base = lf.Launch(chain=Chain.SOL, token=token, venue="pump.fun", creator="Creator1", launched_ms=T0 * 1000,
                     received_ms=T0 * 1000 + 900)
    return replace(base, **over)


def decision(conn, token: str, action: str, *, ts_ms: int, lane: str = snipe.LANE_VALUE, chain: str = "sol") -> None:
    conn.execute("INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action) VALUES (?,?,?,?,?,?,?)",
                 (f"d_{token}_{action}_{ts_ms}_{lane}_{chain}", ts_ms, lane, "live", chain, token, action))


# ------------------------------------------------------------------------- the daily cap

@needs_lane
def test_the_daily_cap_counts_entries_not_the_signals_the_engine_refused(tmp_db):
    """MEASURED 2026-10-04 on the box: sol hit max_snipes_per_day=5 at ~01:20 UTC on five
    signals the old sizing refused, and every sol launch after that was daily_snipe_cap."""
    from kaiba.execution.lanes import record

    now = int(datetime(2026, 10, 4, 1, 20, tzinfo=UTC).timestamp() * 1000)
    verdict = snipe.Verdict(True, [], 0.72, "record:low/runner", {"record": "low/runner"})
    p = {**snipe.DEFAULT_PARAMS, "max_snipes_per_day": {"sol": 5, "robinhood": 5}}
    runner = snipe.Record("w", 3, 5, 1, snipe.EvidenceBasis.DERIVED)
    for i in range(5):  # today's case: five signals, five refusals
        record(snipe.build_signal(sol_launch(f"Mint{i}"), verdict), tmp_db)
        decision(tmp_db, f"Mint{i}", "skip", ts_ms=now - 60_000 + i)
    assert snipe.snipes_today(tmp_db, Chain.SOL, at_ms=now) == 0
    assert snipe.evaluate(sol_launch("Mint9"), p, runner, snipes_today=snipe.snipes_today(tmp_db, Chain.SOL, at_ms=now)).fire

    yesterday = now - 2 * 3_600_000  # 23:20 UTC the day before
    decision(tmp_db, "Old", "enter", ts_ms=yesterday)
    decision(tmp_db, "Other", "enter", ts_ms=now - 5, lane="confluence-5")
    decision(tmp_db, "0xrh", "enter", ts_ms=now - 5, chain="robinhood")
    for i in range(5):
        decision(tmp_db, f"Mint{i}", "enter", ts_ms=now - 1_000 + i)
    decision(tmp_db, "Mint0", "enter", ts_ms=now - 10)  # a second decision on one token is one snipe
    assert snipe.snipes_today(tmp_db, Chain.SOL, at_ms=now) == 5
    assert snipe.snipes_today(tmp_db, Chain.ROBINHOOD, at_ms=now) == 1
    capped = snipe.evaluate(sol_launch("Mint9"), p, runner, snipes_today=snipe.snipes_today(tmp_db, Chain.SOL, at_ms=now))
    assert not capped.fire and "daily_snipe_cap:5/5" in capped.reasons


# ------------------------------------------------------------------------- bounded service

@needs_lane
def test_launches_in_flight_are_bounded_and_the_overflow_is_counted(tmp_db):
    sniper = snipe.Sniper(tmp_db, chains=[])
    sniper.p = {**sniper.p, "max_inflight": 16}
    gate = asyncio.Event()

    async def slow(launch):
        await gate.wait()

    sniper.on_launch = slow

    async def burst():
        tasks = [sniper.spawn(sol_launch(f"M{i}")) for i in range(200)]
        assert len(sniper._inflight) == 16 and sum(t is not None for t in tasks) == 16
        assert sniper.stats["dropped_backlog"] == 184
        gate.set()
        await asyncio.gather(*[t for t in tasks if t is not None])
        await asyncio.sleep(0)
        assert sniper._inflight == set()  # every finished task let go

    asyncio.run(burst())


def test_the_memory_probe_reports_rss_and_the_cgroups_anon_and_file(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t   84652 kB\nThreads:\t7\n", encoding="utf-8")
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/system.slice/kaiba-snipe.service\n", encoding="utf-8")
    stat_dir = tmp_path / "fs" / "system.slice" / "kaiba-snipe.service"
    stat_dir.mkdir(parents=True)
    (stat_dir / "memory.stat").write_text(f"anon {80 * 2**20}\nfile {330 * 2**20}\nkernel 123\n", encoding="utf-8")
    got = snipe.memory_probe(status_path=str(status), cgroup_path=str(cgroup), cgroup_root=str(tmp_path / "fs"))
    assert got["rss_mb"] == 82 and got["cg_anon_mb"] == 80 and got["cg_file_mb"] == 330 and got["threads"] >= 1
    assert set(snipe.memory_probe(status_path=str(tmp_path / "none"), cgroup_path=str(tmp_path / "none"))) == {"threads"}


# ------------------------------------------------------------------------- peaks

def test_a_sol_curve_that_completes_before_a_mark_is_valued_at_its_curve_end(tmp_db, monkeypatch):
    """Until 2026-10-04 a completed curve got no value, so the best outcomes read as zero."""
    from kaiba.execution import curve_price as cp

    fresh, _ = cp.CurveState.build(virtual_sol=30 * 10**9, virtual_token=1_073_000_000 * 10**6,
                                   real_sol=0, real_token=793_100_000 * 10**6, observed_ms=1)
    fill = cp.quote_buy(fresh, 370_000_000, sol_usd=Decimal(120), decimals=6, latency_ms=0)
    curve = {"virtual_sol": fresh.virtual_sol, "virtual_token": fresh.virtual_token, "real_sol": 0, "real_token": fresh.real_token}
    entry = {"basis": "pumpfun_curve_model", "quote_in": 370_000_000, "tokens": int(fill.amount_out), "shadow_quote": int(fill.curve_in)}
    with_curve = snipe.Verdict(False, [], 0.0, "", {"entry_curve": curve})
    snipe.record_observation(tmp_db, sol_launch("Grad1"), with_curve, entry, at_ms=T0 * 1000)
    snipe.record_observation(tmp_db, sol_launch("Grad2"), snipe.Verdict(False, [], 0.0, "", {}), entry, at_ms=T0 * 1000)
    monkeypatch.setattr(snipe, "_sol_curve", lambda conn, token, bc, at: (None, "curve_complete"))
    monkeypatch.setattr(cp, "sol_usd_from_native_price", lambda conn=None: Decimal(120))
    snipe.mark_sol(tmp_db, snipe.due_marks(tmp_db, [300], at_ms=T0 * 1000 + 301_000), [300], at_ms=T0 * 1000 + 301_000)
    done = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE} WHERE token='Grad1'")
    expected = snipe.curve_end_value(curve, entry["shadow_quote"], entry["tokens"], sol_usd=Decimal(120))
    assert done["status"] == "graduated" and jload(done["marks_json"])["300"]["value"] == str(expected)
    assert Decimal(done["peak_ratio"]) > 10, done["peak_ratio"]  # ~30 SOL of curve -> ~115: a runner, not a zero
    old = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE} WHERE token='Grad2'")  # observed before entry_curve existed
    assert old["status"] == "graduated" and jload(old["marks_json"])["300"]["value"] is None


def trade_log(side: str, *, block: int, tokens: int, quote: int, idx: int = 0) -> dict:
    topic0 = rh.TOPIC_CURVE_BUY if side == "buy" else rh.TOPIC_CURVE_SELL
    words = (quote, tokens) if side == "buy" else (tokens, quote)
    pad = "0x" + "0" * 24 + "55" * 20
    return {"address": CURVE, "topics": [topic0, pad, pad], "data": "0x" + "".join(format(x, "064x") for x in (*words, 0, 0)),
            "blockNumber": hex(block), "logIndex": hex(idx), "transactionHash": "0x" + format(block * 100 + idx, "064x")}


def test_the_path_reserve_walk_finds_the_lowest_token_reserve_in_any_log_order():
    logs = [trade_log("sell", block=12, tokens=250, quote=1), trade_log("buy", block=11, tokens=300, quote=1),
            trade_log("buy", block=13, tokens=40, quote=1)]
    # now 1000 -> before the last buy 1040 -> before the sell 790 -> before the first buy 1090 (our entry)
    assert snipe.path_min_token_reserve({"curve": CURVE, "token": TOKEN}, logs, 1000) == (790, 3)
    assert snipe.path_min_token_reserve({"curve": CURVE, "token": TOKEN}, [], 1000) == (1000, 0)


def test_robinhood_records_the_windows_true_peak_beside_the_sampled_marks(tmp_db, monkeypatch):
    q0, t0 = 168 * 10**16, 10**27
    k = q0 * t0
    tokens = 99 * 10**14 * t0 // (q0 + 99 * 10**14)
    entry = {"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": tokens, "shadow_quote": snipe.quote_for_tokens(q0, t0, tokens)}
    feats = {"curve": CURVE, "fee_bps": 100, "creator_tax_bps": 0, "decision_block": 5000}
    launch = lf.Launch(chain=Chain.ROBINHOOD, token=TOKEN, venue="pons", creator="0xdev", curve=CURVE,
                       launched_ms=T0 * 1000, received_ms=T0 * 1000)
    snipe.record_observation(tmp_db, launch, snipe.Verdict(False, [], 0.0, "", feats), entry, at_ms=T0 * 1000)
    # after our entry: someone bought 30% of the tokens (price ~2x), then most of it was sold back
    t_peak, t_now = 7 * 10**26, 95 * 10**25
    logs = [trade_log("buy", block=5100, tokens=t0 - t_peak, quote=1), trade_log("sell", block=5900, tokens=t_now - t_peak, quote=1)]
    seen: list[dict] = []

    def fake_rpc(calls, **kw):
        out = []
        for method, args in calls:
            if method == "eth_getLogs":
                seen.append(args[0])
                out.append(logs)
                continue
            name = next(n for n, s in rh.CURVE_READS if s == args[0]["data"])
            out.append(hex({"quote_reserve": k // t_now, "sellable_tokens": t_now}.get(name, 0)))
        return out

    monkeypatch.setattr(snipe, "rh_rpc", fake_rpc)
    at = T0 * 1000 + 3_601_000
    snipe.mark_rh(tmp_db, snipe.due_marks(tmp_db, [300, 900, 3600], at_ms=at), [300, 900, 3600], at_ms=at)
    row = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    assert seen == [{"address": CURVE, "topics": [[rh.TOPIC_CURVE_BUY, rh.TOPIC_CURVE_SELL]], "fromBlock": hex(5001), "toBlock": "latest"}]
    q_now = k // t_now
    expected = snipe.pons_sell_quote(*snipe.with_holding(q_now * t_now // t_peak, t_peak, entry["shadow_quote"]), tokens, 100, 0)
    assert row["path_peak_ratio"] == str(Decimal(expected) / Decimal(10**16)) and row["path_peak_note"] == "curve_path:2_trades"
    assert Decimal(row["path_peak_ratio"]) > Decimal("1.8") > Decimal(row["peak_ratio"])  # the marks missed it


def test_an_observation_without_a_decision_block_says_so_instead_of_a_peak(tmp_db, monkeypatch):
    entry = {"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": 10**24, "shadow_quote": 10**16}
    launch = lf.Launch(chain=Chain.ROBINHOOD, token=TOKEN, venue="pons", creator="0xdev", curve=CURVE,
                       launched_ms=T0 * 1000, received_ms=T0 * 1000)
    snipe.record_observation(tmp_db, launch, snipe.Verdict(False, [], 0.0, "", {"curve": CURVE}), entry, at_ms=T0 * 1000)
    monkeypatch.setattr(snipe, "rh_rpc", lambda calls, **kw: [hex(10**18)] * len(calls))
    snipe.mark_rh(tmp_db, snipe.due_marks(tmp_db, [300], at_ms=T0 * 1000 + 301_000), [300], at_ms=T0 * 1000 + 301_000)
    row = fetch_one(tmp_db, f"SELECT path_peak_ratio, path_peak_note FROM {snipe.TABLE}")
    assert row["path_peak_ratio"] is None and row["path_peak_note"] == "no_decision_block"


def test_an_old_observation_with_the_frames_wrong_curve_key_is_read_on_the_derived_curve(monkeypatch):
    mint, wrong, right = ("J8qxPSHY3B5B1bGxPMqG2HaWQ3uzJcHSDgy9EfVXpump", "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",
                          "7vk2QivZqv2HLoUP7qDPGpVbrp5xwYMT5WpxEYFEG3Ra")
    asked: list[str] = []

    def read(conn, key, *, at_ms=None):
        asked.append(key)
        return ("curve", "ok") if key == right else (None, "curve_account_short")

    monkeypatch.setattr(snipe, "read_pump_curve", read)
    assert snipe._sol_curve(None, mint, wrong, T0 * 1000) == ("curve", "ok") and asked == [wrong, right]
    asked.clear()
    assert snipe._sol_curve(None, mint, right, T0 * 1000) == ("curve", "ok") and asked == [right]  # read once


def test_a_sol_paper_entry_keeps_the_curve_it_entered_on(tmp_db, monkeypatch):
    from kaiba.execution import curve_price as cp

    fresh, _ = cp.CurveState.build(virtual_sol=30 * 10**9, virtual_token=1_073_000_000 * 10**6,
                                   real_sol=0, real_token=793_100_000 * 10**6, observed_ms=1)
    monkeypatch.setattr(snipe, "_sol_curve", lambda conn, token, bc, at: (fresh, "ok"))
    monkeypatch.setattr(cp, "sol_usd_from_native_price", lambda conn=None: Decimal(120))
    entry = snipe.paper_entry_sol(sol_launch(), {**snipe.DEFAULT_PARAMS, "sol_exec_latency_ms": 0}, conn=tmp_db)
    assert entry["entry_curve"] == {"virtual_sol": 30 * 10**9, "virtual_token": 1_073_000_000 * 10**6, "real_sol": 0,
                                    "real_token": 793_100_000 * 10**6}
