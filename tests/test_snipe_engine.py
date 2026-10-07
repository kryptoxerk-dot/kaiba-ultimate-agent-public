"""kaiba/execution/snipe.py: measurement, and the hand-off to the engine (never around it)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import robinhood as rh

LANE_WIRED = snipe.LANE_VALUE in {x.value for x in Lane}
needs_lane = pytest.mark.skipif(not LANE_WIRED, reason="Lane 'launch-snipe' not in kaiba.core.schemas yet (lead wires it)")

T0 = 1_791_000_000
CURVE = "0x5575479424e114fafa0cd314115c7897a7a2441b"
TOKEN = "0x9122cbc7a76c8518989aae029148d331538dadb7"


def rh_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.ROBINHOOD, token=TOKEN, venue="pons", creator="0x" + "1d" * 20, curve=CURVE,
                     pair_token="0x" + "0" * 40, graduation_threshold=4 * 10**18, block=1000, launched_ms=T0 * 1000,
                     tx="0x" + "ab" * 32, received_ms=T0 * 1000 + 400)
    return replace(base, **over)


def sol_launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.SOL, token="Mint1111", venue="launchlab", creator="Creator1", launched_ms=T0 * 1000,
                     received_ms=T0 * 1000 + 900)
    return replace(base, **over)


FIRE = snipe.Verdict(True, [], 0.72, "record:low/runner", {"record": "low/runner", "curve": CURVE, "quote_native": True})
SKIP = snipe.Verdict(False, ["no_alpha:not_watched_and_record_low/no_prior"], 0.0, "", {"record": "low/no_prior"})


def counts(conn) -> dict[str, int]:
    return {t: fetch_one(conn, f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("orders", "positions", "decisions", "tokens")}


# ------------------------------------------------------------------------- measurement

def test_an_observation_is_recorded_once_per_token_with_its_verdict_and_entry(tmp_db):
    entry = {"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": 5 * 10**24, "shadow_quote": 99 * 10**14}
    oid = snipe.record_observation(tmp_db, rh_launch(), FIRE, entry, at_ms=T0 * 1000 + 3500)
    again = snipe.record_observation(tmp_db, rh_launch(), SKIP, {"basis": "x"}, at_ms=T0 * 1000 + 9000)
    rows = fetch_all(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    assert oid == again and len(rows) == 1
    r = rows[0]
    assert r["fire"] == 1 and r["status"] == "open" and r["entry_tokens"] == str(5 * 10**24) and r["latency_ms"] == 400
    assert jload(r["features_json"])["curve"] == CURVE


def test_an_unpriced_entry_is_kept_for_the_record_but_never_marked(tmp_db):
    snipe.record_observation(tmp_db, sol_launch(), SKIP, {"basis": "curve_unread"}, at_ms=T0 * 1000)
    assert fetch_one(tmp_db, f"SELECT status FROM {snipe.TABLE}")["status"] == "unpriced"
    assert snipe.due_marks(tmp_db, [300], at_ms=T0 * 1000 + 10**7) == []


def test_marks_value_the_paper_holding_on_the_live_curve_at_each_horizon(tmp_db, monkeypatch):
    q0, t0 = 168 * 10**16, 10**27
    net = 99 * 10**14
    tokens = net * t0 // (q0 + net)
    entry = {"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": tokens, "shadow_quote": snipe.quote_for_tokens(q0, t0, tokens)}
    taxed = snipe.Verdict(True, [], 0.72, "record:low/runner", {**FIRE.features, "fee_bps": 100, "creator_tax_bps": 200,
                                                                   "graduation_threshold": str(4 * 10**18)})
    snipe.record_observation(tmp_db, rh_launch(), taxed, entry, at_ms=T0 * 1000)
    # the live curve has not moved (our paper buy never reached it): selling back loses only fees
    reads = {"quote_reserve": q0, "real_quote_reserve": 0, "sellable_tokens": t0}

    def fake_rpc(calls, **kw):
        out = []
        for _, (call, _tag) in calls:
            sel = call["data"]
            name = next((n for n, s in rh.CURVE_READS if s == sel), None)
            out.append(hex(100 if name == "fee_bps" else reads.get(name, 0)) if name else hex(200))
        return out

    monkeypatch.setattr(snipe, "rh_rpc", fake_rpc)
    assert snipe.mark_rh(tmp_db, snipe.due_marks(tmp_db, [300, 900], at_ms=T0 * 1000 + 301_000), [300, 900], at_ms=T0 * 1000 + 301_000) == 1
    row = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    marks = jload(row["marks_json"])
    assert set(marks) == {"300"} and row["status"] == "open"
    ratio = int(marks["300"]["value"]) / 10**16
    assert 0.95 < ratio < 0.975, ratio  # 1% fee + 2% creator tax out, ~1% fee in
    snipe.mark_rh(tmp_db, snipe.due_marks(tmp_db, [300, 900], at_ms=T0 * 1000 + 901_000), [300, 900], at_ms=T0 * 1000 + 901_000)
    assert fetch_one(tmp_db, f"SELECT status FROM {snipe.TABLE}")["status"] == "marked"


# ------------------------------------------------------------------------- the engine

def test_the_dossier_budget_is_a_rolling_hour():
    b = snipe.DossierBudget(2)
    assert b.take(0) and b.take(10) and not b.take(20)
    assert b.take(3601)


@needs_lane
def test_hand_off_records_one_signal_after_a_dossier_and_the_token_row(tmp_db):
    scans = []
    scan = lambda token, chain: scans.append(token) or SimpleNamespace(grade=SimpleNamespace(value="B"), blockers=[])  # noqa: E731
    p = {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01}
    budget = snipe.DossierBudget(10)
    published = []
    publish = lambda conn, launch: published.append(launch.token) or (1, "snapshot")  # noqa: E731
    sized = lambda conn, launch, strength: (True, "band_ok")  # noqa: E731
    kw = {"scan": scan, "publish": publish, "precheck": sized}
    assert snipe.hand_to_engine(tmp_db, sol_launch(), FIRE, p, budget, **kw) == (None, "no_token_row")
    assert published == [] and scans == []  # nothing spent on a token the engine cannot see yet
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('sol', 'Mint1111', 'pump.fun', 1)")
    sid, note = snipe.hand_to_engine(tmp_db, sol_launch(), FIRE, p, budget, **kw)
    assert note == "depth_snapshot:dossier:B:depth_snapshot:signal", note
    assert published == ["Mint1111", "Mint1111"]
    sig = fetch_one(tmp_db, "SELECT * FROM signals WHERE signal_id=?", (sid,))
    assert sig["lane"] == snipe.LANE_VALUE and sig["chain"] == "sol" and sig["strength"] >= 0.70
    assert jload(sig["payload_json"])["source"] == "launch_snipe"
    assert snipe.hand_to_engine(tmp_db, sol_launch(), FIRE, p, budget, **kw)[0] is None  # recorded once
    assert snipe.snipes_today(tmp_db, Chain.SOL, at_ms=sig["created_ms"]) == 0  # a signal is not a snipe; an entry is


def test_no_dossier_budget_or_a_failed_dossier_means_no_signal(tmp_db):
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('sol', 'Mint1111', 'pump.fun', 1)")
    p = {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01}
    kw = {"publish": lambda conn, launch: (1, "snapshot"), "precheck": lambda conn, launch, strength: (True, "band_ok")}
    assert snipe.hand_to_engine(tmp_db, sol_launch(), FIRE, p, snipe.DossierBudget(0), scan=lambda *a: None, **kw) == (
        None, "depth_snapshot:dossier_budget_exhausted")

    def boom(*a):
        raise TimeoutError("gmgn")

    assert snipe.hand_to_engine(tmp_db, sol_launch(), FIRE, p, snipe.DossierBudget(5), scan=boom, **kw) == (
        None, "depth_snapshot:dossier_failed:TimeoutError")
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM signals")["n"] == 0


def test_a_launch_the_gate_cannot_size_never_spends_a_dossier(tmp_db):
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('robinhood', ?, 'pons', 1)", (TOKEN,))
    scans, budget = [], snipe.DossierBudget(5)
    unsized = lambda conn, launch, strength: (False, "no_band:no_viable_size:cheapest_is_8.49pct>7.0pct")  # noqa: E731
    got = snipe.hand_to_engine(tmp_db, rh_launch(), FIRE, {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01}, budget,
                               scan=lambda *a: scans.append(a), precheck=unsized)
    assert got == (None, "no_band:no_viable_size:cheapest_is_8.49pct>7.0pct")
    assert scans == [] and len(budget.used) == 0
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM signals")["n"] == 0


@needs_lane
def test_the_sniper_never_writes_orders_positions_decisions_or_tokens(tmp_db, monkeypatch):
    monkeypatch.setattr(snipe, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(snipe, "params", lambda cfg=None: {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01,
                                                           "dev_watchlist": ["0x" + "1d" * 20]})
    monkeypatch.setattr(snipe, "wait_for_tax", lambda launch, p, **kw: (T0 + 3, 1030))
    monkeypatch.setattr(snipe, "read_early_book", lambda launch, p, head, **kw: snipe.EarlyBook(dev_buy_wei=10**17, outside_buys_window=3, read=True))
    monkeypatch.setattr(snipe, "paper_entry_rh", lambda launch, p, **kw: {"basis": "eth_simulateV1", "quote_in": 10**16, "tokens": 10**24, "shadow_quote": 10**16})
    def stand_in(conn, launch, verdict, p, budget, **kw):  # the real path minus the dossier scan
        from kaiba.execution.lanes import record

        sig = snipe.build_signal(launch, verdict)
        return (sig.signal_id if record(sig, conn) else None), "dossier:B:signal"

    monkeypatch.setattr(snipe, "hand_to_engine", stand_in)
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('robinhood', ?, 'pons', 1)", (TOKEN,))
    before = counts(tmp_db)
    s = snipe.Sniper(tmp_db, chains=["robinhood"])
    s._handle(rh_launch())
    assert counts(tmp_db) == before
    obs = fetch_one(tmp_db, f"SELECT * FROM {snipe.TABLE}")
    assert obs["fire"] == 1 and obs["rule"] == "dev_watchlist" and obs["signal_id"]
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM signals WHERE lane=?", (snipe.LANE_VALUE,))["n"] == 1


@needs_lane
def test_the_engine_decides_a_snipe_signal_like_any_other(tmp_db):
    """The signal goes through engine.run_once: with no dossier stored, the engine itself
    refuses it (no_dossier) -- proof the lane's entries cannot skip the engine's gates."""
    from kaiba.execution import engine, lanes

    signal = snipe.build_signal(sol_launch(), FIRE)
    assert lanes.record(signal, tmp_db)
    decisions = engine.run_once(tmp_db)
    mine = [d for d in decisions if d.lane.value == snipe.LANE_VALUE]
    assert len(mine) == 1 and mine[0].action.value == "skip"
    assert mine[0].blockers in (["no_dossier"], ["lane_off"])


def test_a_skipped_sol_pumpfun_launch_is_sampled_not_measured_every_time(tmp_db, monkeypatch):
    monkeypatch.setattr(snipe, "get_conn", lambda: tmp_db)
    monkeypatch.setattr(snipe, "params", lambda cfg=None: {**snipe.DEFAULT_PARAMS, "measure_sol_every": 3})
    monkeypatch.setattr(snipe, "paper_entry_sol", lambda launch, p, **kw: {"basis": "pumpfun_curve_model", "quote_in": 1, "tokens": 1})
    monkeypatch.setattr(snipe, "snipes_today", lambda conn, chain, **kw: 0)
    s = snipe.Sniper(tmp_db, chains=["sol"])
    for i in range(6):
        s._handle(sol_launch(token=f"Mint{i}", venue="pump.fun"))
    assert fetch_one(tmp_db, f"SELECT COUNT(*) AS n FROM {snipe.TABLE}")["n"] == 2
    s._handle(sol_launch(token="MintLL", venue="launchlab"))
    assert fetch_one(tmp_db, f"SELECT COUNT(*) AS n FROM {snipe.TABLE}")["n"] == 3



def test_a_pump_curve_account_decodes_to_its_reserves():
    import struct

    raw = b"\x17" * 8 + struct.pack("<QQQQQ", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 0, 10**15) + b"\x00" + b"\x01" * 32
    got = snipe.decode_pump_curve(raw)
    assert got == {"virtual_token": 1_073_000_000_000_000, "virtual_sol": 30_000_000_000, "real_token": 793_100_000_000_000,
                   "real_sol": 0, "supply": 10**15, "complete": 0}
    assert snipe.decode_pump_curve(raw[:40]) is None
    assert snipe.decode_pump_curve(raw[:48] + b"\x01")["complete"] == 1


def test_a_sol_paper_holding_is_valued_as_if_its_buy_had_landed(tmp_db, monkeypatch):
    """A fresh curve with no real SOL cannot buy our tokens back unless our own SOL is put
    back in -- the same trap as Robinhood, on the pump.fun model."""
    from kaiba.execution import curve_price as cp

    fresh, _ = cp.CurveState.build(virtual_sol=30 * 10**9, virtual_token=1_073_000_000 * 10**6,
                                   real_sol=0, real_token=793_100_000 * 10**6, observed_ms=1)
    fill = cp.quote_buy(fresh, 370_000_000, sol_usd=cp.Decimal(120), decimals=6, latency_ms=0)
    assert fill.ok
    entry = {"basis": "pumpfun_curve_model", "quote_in": 370_000_000, "tokens": int(fill.amount_out), "shadow_quote": int(fill.curve_in)}
    snipe.record_observation(tmp_db, sol_launch(venue="pump.fun"), SKIP, entry, at_ms=T0 * 1000)
    monkeypatch.setattr(snipe, "_sol_curve", lambda conn, token, bc, at: (fresh, "ok"))
    monkeypatch.setattr(cp, "sol_usd_from_native_price", lambda conn=None: cp.Decimal(120))
    rows = snipe.due_marks(tmp_db, [300], at_ms=T0 * 1000 + 301_000)
    snipe.mark_sol(tmp_db, rows, [300], at_ms=T0 * 1000 + 301_000)
    m = jload(fetch_one(tmp_db, f"SELECT marks_json FROM {snipe.TABLE}")["marks_json"])["300"]
    ratio = int(m["value"]) / 370_000_000
    assert 0.95 < ratio < 0.99, (m, ratio)  # the curve's fee each way, nothing more
