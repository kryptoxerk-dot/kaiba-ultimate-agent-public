"""H5 slower-wallet re-entry evaluator (kaiba/learning/h5_reentry.py), against the pre-registration
(claude-h5-reentry-prereg-20261103.md) and its amendment A1-A11. Fakes only: no network, no DB."""
from __future__ import annotations

import hashlib
import json

import pytest

from kaiba.learning import h5_reentry as H

MIN, DAY = H.MIN, H.DAY
T0 = H.T0_MS
START = T0 + 60 * MIN  # the evaluator's first heartbeat in these tests


# ---------------------------------------------------------------- the trigger rule

def test_first_buy_reentry_and_the_ten_minute_gap():
    t = START
    assert H.classify_buy([], t) == "first"
    assert H.classify_buy([("buy", t - 60 * MIN), ("sell", t - 30 * MIN)], t) == "reentry"
    assert H.classify_buy([("buy", t - 60 * MIN), ("sell", t - 9 * MIN)], t) is None  # gap < 10 min
    assert H.classify_buy([("buy", t - 60 * MIN)], t) is None  # still holding as far as we saw


def test_a_quick_rebuy_consumes_the_sell_so_the_next_buy_needs_a_new_sell():
    t = START
    hist = [("buy", t - 90 * MIN), ("sell", t - 60 * MIN), ("buy", t - 55 * MIN)]
    assert H.classify_buy(hist, t) is None


def test_partial_sells_count_and_the_last_one_sets_the_gap():
    t = START
    hist = [("buy", t - 90 * MIN), ("sell", t - 60 * MIN), ("sell", t - 5 * MIN)]
    assert H.classify_buy(hist, t) is None
    hist = [("buy", t - 90 * MIN), ("sell", t - 60 * MIN), ("sell", t - 20 * MIN)]
    assert H.classify_buy(hist, t) == "reentry"


def test_only_the_thirteen_days_before_the_buy_are_read():
    t = START + 20 * DAY
    old = [("buy", t - 14 * DAY), ("sell", t - 13 * DAY - 1)]
    assert H.classify_buy(old, t) == "first"
    inside = [("buy", t - 12 * DAY), ("sell", t - 11 * DAY)]
    assert H.classify_buy(inside, t) == "reentry"


def test_rows_are_walked_in_time_order_not_side_order():
    t = START
    hist = [("sell", t - 30 * MIN), ("buy", t - 60 * MIN)]  # given out of order
    assert H.classify_buy(hist, t) == "reentry"


def test_arms():
    slow, fast = frozenset({"S"}), frozenset({"F"})
    assert H.arm_for("reentry", "S", slow, fast) == "H5"
    assert H.arm_for("reentry", "F", slow, fast) == "B2"
    assert H.arm_for("first", "S", slow, fast) == "B1"
    assert H.arm_for("first", "F", slow, fast) is None
    assert H.arm_for("reentry", "X", slow, fast) is None


# ---------------------------------------------------------------- frozen inputs

def _frozen(tmp_path, slow, fast, monkeypatch):
    shas = {}
    for name, members in ((H.SLOW_FILE, slow), (H.FAST_FILE, fast)):
        raw = json.dumps({"cohort_id": H.COHORT_ID, "members": sorted(members)}).encode()
        (tmp_path / name).write_bytes(raw)
        shas[name] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(H, "SLOW_SHA", shas[H.SLOW_FILE])
    monkeypatch.setattr(H, "FAST_SHA", shas[H.FAST_FILE])


def test_frozen_files_are_refused_on_any_byte_difference(tmp_path, monkeypatch):
    _frozen(tmp_path, {"S"}, {"F"}, monkeypatch)
    assert H.load_members(tmp_path) == (frozenset({"S"}), frozenset({"F"}))
    (tmp_path / H.FAST_FILE).write_bytes((tmp_path / H.FAST_FILE).read_bytes() + b" ")
    with pytest.raises(H.FrozenInputMismatch):
        H.load_members(tmp_path)


def test_the_registered_constants():
    assert H.T0_MS == 1_793_721_068_000 and H.COHORT_ID == "runners:sol:1793713163992"
    assert H.SLOW_SHA.startswith("c806b29d") and H.FAST_SHA.startswith("eb890dce")


# ---------------------------------------------------------------- the evaluator, end to end

class World:
    """A fake tape, clock and price book."""

    def __init__(self):
        self.now = START
        self.rows: list[dict] = []
        self.prices: dict[str, H.Read] = {}

    def add(self, wallet, token, side, ts, tx):
        self.rows.append({"wallet": wallet, "token": token, "side": side, "ts_ms": ts, "tx": tx, "source": "gmgn:smartmoney"})

    def fetch_new(self, members, since):
        return [r for r in self.rows if r["wallet"] in members and r["ts_ms"] >= since and r["ts_ms"] <= self.now]

    def fetch_history(self, wallet, token, start, end):
        return [(r["side"], r["ts_ms"]) for r in self.rows
                if r["wallet"] == wallet and r["token"] == token and start <= r["ts_ms"] < end]

    def read(self, token):
        return self.prices.get(token, H.Read(False, note="rpc failed"))

    def price(self, token, usd, liq_sol=500, venue="pumpswap"):
        self.prices[token] = H.Read(True, usd, liq_sol * 150.0, 150.0, venue, "pumpswap-pool:x")


def make(tmp_path, world, slow=("S",), fast=("F",)):
    ev = H.Evaluator(H.Ledger(tmp_path / "ledger.jsonl"), frozenset(slow), frozenset(fast),
                     fetch_new=world.fetch_new, fetch_history=world.fetch_history,
                     read_price=world.read, now=lambda: world.now)
    ev.start()
    return ev


def kinds(ev, kind):
    return [r for r in ev.ledger.lines if r["type"] == kind]


def reentry(world, wallet, token, at, tx):
    world.add(wallet, token, "buy", at - 90 * MIN, tx + "-b0")
    world.add(wallet, token, "sell", at - 40 * MIN, tx + "-s0")
    world.add(wallet, token, "buy", at, tx)


def test_a_rebuy_before_the_first_heartbeat_is_pre_evaluator_and_never_entered(tmp_path):
    w = World()
    reentry(w, "S", "TOK", START - 5 * MIN, "tx1")
    ev = make(tmp_path, w)
    ev.poll()
    [trig] = [t for t in kinds(ev, "trigger") if t["arm"] == "H5"]
    assert trig["status"] == "pre_evaluator"
    w.price("TOK", 1.0)
    w.now += 2 * MIN
    ev.step()
    assert not kinds(ev, "entry")


def test_entry_waits_30s_after_seen_then_prices_and_exits_at_take_profit(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now - 5_000, "tx1")
    ev.poll()
    trig = [t for t in kinds(ev, "trigger") if t["arm"] == "H5"][0]
    assert trig["status"] == "pending" and trig["seen_ms"] == w.now
    assert trig["cycle"] == "observed_buy_sell_rebuy"
    w.price("TOK", 1.0)
    w.now += 20_000
    ev.step()
    assert not kinds(ev, "entry")  # 20 s < 30 s
    w.now += 15_000
    ev.step()
    [ent] = kinds(ev, "entry")
    assert ent["delay_ms"] == 35_000 and ent["price_usd"] == 1.0
    w.price("TOK", 1.6)
    w.now += 31_000
    ev.step()
    [ex] = kinds(ev, "exit")
    assert ex["reason"] == "take_profit" and ex["price_usd"] == 1.6


def test_stop_and_time_exits(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "A", w.now, "txa")
    reentry(w, "S", "B", w.now, "txb")
    ev.poll()
    w.price("A", 1.0)
    w.price("B", 1.0)
    w.now += 31_000
    ev.step()
    w.price("A", 0.69)
    w.price("B", 1.1)
    w.now += 31_000
    ev.step()
    assert {e["token"]: e["reason"] for e in kinds(ev, "exit")} == {"A": "stop"}
    for _ in range(130):
        w.now += 31_000
        ev.step()
    assert {e["token"]: e["reason"] for e in kinds(ev, "exit")} == {"A": "stop", "B": "time"}


def test_first_entries_and_fast_reentries_are_their_own_arms(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    w.add("S", "NEW", "buy", w.now, "txs")       # B1
    reentry(w, "F", "FT", w.now, "txf")          # B2
    w.add("F", "FNEW", "buy", w.now, "txf2")     # a fast member's first buy: no arm
    ev.poll()
    arms = sorted((t["arm"], t["token"]) for t in kinds(ev, "trigger"))
    assert ("B1", "NEW") in arms and ("B2", "FT") in arms
    assert not any(tok == "FNEW" for _, tok in arms)
    assert [t for t in kinds(ev, "trigger") if t["arm"] == "B1"][0]["cycle"] == "observed_first_buy"


def test_the_quote_asset_is_never_a_trigger(tmp_path):
    from kaiba.core.schemas import QUOTE_ASSETS, Chain

    assert QUOTE_ASSETS[Chain.SOL] == H.SOL_QUOTE_ASSETS
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "So11111111111111111111111111111111111111112", w.now, "txq")
    ev.poll()
    assert not kinds(ev, "trigger")


def test_one_open_entry_per_token_per_arm(tmp_path):
    w = World()
    ev = make(tmp_path, w, slow=("S", "S2"))
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    w.now += MIN
    reentry(w, "S2", "TOK", w.now, "tx2")
    ev.poll()
    statuses = sorted(t["status"] for t in kinds(ev, "trigger") if t["arm"] == "H5")
    assert statuses == ["overlap_skipped", "pending"]


def test_no_price_for_ten_minutes_is_unpriced_venue_or_entry_unpriced(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "RAY", w.now, "tx1")
    reentry(w, "S", "FLAKY", w.now, "tx2")
    ev.poll()
    w.prices["RAY"] = H.Read(False, no_venue=True, note="no pumpswap pool; no live pump.fun curve")
    for _ in range(25):
        w.now += 30_000
        ev.step()
    assert {r["token"] for r in kinds(ev, "unpriced_venue")} == {"RAY"}
    assert {r["token"] for r in kinds(ev, "entry_unpriced")} == {"FLAKY"}
    assert not kinds(ev, "entry")


def test_liquidity_floor_excludes(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "THIN", w.now, "tx1")
    ev.poll()
    w.prices["THIN"] = H.Read(True, 1.0, 99 * 150.0, 150.0, "pumpswap", "x")  # 99 SOL-equivalent
    w.now += 31_000
    ev.step()
    assert kinds(ev, "excluded_universe") and not kinds(ev, "entry")


def test_unresolved_after_seventy_minutes_without_a_read_keeps_the_last_price(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    w.price("TOK", 1.0)
    w.now += 31_000
    ev.step()
    w.price("TOK", 1.2)
    w.now += 31_000
    ev.step()
    del w.prices["TOK"]
    for _ in range(150):
        w.now += 31_000
        ev.step()
    [u] = kinds(ev, "unresolved")
    assert u["last"]["price_usd"] == 1.2


def test_a_restart_rebuilds_from_the_ledger_without_duplicates(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    w.price("TOK", 1.0)
    w.now += 31_000
    ev.step()
    ev2 = make(tmp_path, w)  # a new process on the same ledger
    assert ev2.first_heartbeat == ev.first_heartbeat
    assert kinds(ev2, "restart")
    ev2.poll()  # the same tape rows again
    assert len(kinds(ev2, "trigger")) == len(kinds(ev, "trigger"))
    assert "TOK" in {e["token"] for e in ev2.opened.values()}
    w.price("TOK", 2.0)
    w.now += 31_000
    ev2.step()
    assert len(kinds(ev2, "entry")) == 1 and kinds(ev2, "exit")


def test_ledger_is_append_only_and_idempotent(tmp_path):
    led = H.Ledger(tmp_path / "l.jsonl")
    assert led.append({"type": "x", "id": "1", "ts": 1})
    assert not led.append({"type": "x", "id": "1", "ts": 2})
    assert len(H.Ledger(tmp_path / "l.jsonl").lines) == 1


# ---------------------------------------------------------------- costs and verdicts

def test_cost_model_base_and_flat():
    ent = {"price_usd": 1.0, "venue": "pumpswap", "liquidity_usd": 1e12, "sol_usd": 150.0}
    ex = {**ent, "price_usd": 1.0}
    base = H.net_return(ent, ex, "base")   # 120 + 100 bps + 0.001/0.1 SOL tip = 3.2% a leg, no impact
    assert base == pytest.approx((1 - 0.032) ** 2 - 1, abs=1e-9)
    flat = H.net_return(ent, ex, "flat")
    assert flat == pytest.approx((1 - 0.0325) ** 2 - 1, abs=1e-9)
    thin = {**ent, "liquidity_usd": 150.0 * 100 * 2}  # 100 SOL each side: 0.1 SOL is 0.1% impact
    assert H.net_return(thin, thin, "base") < base


def test_verdict_lines():
    def s(n, r, token=None):
        return [{"token": token or f"T{i}", "trigger_ts": i, "net_base": r, "net_flat": r, "net_worst": r}
                for i in range(n)]
    assert H.verdict(s(39, 0.1), first_heartbeat_ms=0, now_ms=DAY)["verdict"] == "PENDING"
    assert H.verdict(s(39, 0.1), first_heartbeat_ms=0, now_ms=43 * DAY)["verdict"] == "INCONCLUSIVE"
    assert H.verdict(s(40, 0.1), first_heartbeat_ms=0, now_ms=DAY)["verdict"] == "PASS"
    assert H.verdict(s(40, -0.1), first_heartbeat_ms=0, now_ms=DAY)["verdict"] == "FAIL"
    mixed = s(20, -0.2) + [{**x, "trigger_ts": x["trigger_ts"] + 100} for x in s(20, 0.3)]
    assert H.verdict(mixed, first_heartbeat_ms=0, now_ms=DAY)["verdict"] == "FAIL"  # first half negative
    worst = [{**x, "net_worst": -1.0} for x in s(40, 0.1)]
    assert H.verdict(worst, first_heartbeat_ms=0, now_ms=DAY)["verdict"] == "FAIL"


def test_bootstrap_resamples_tokens_not_entries():
    one_token_winner = [("WIN", 1.0)] * 30 + [(f"L{i}", -0.1) for i in range(10)]
    lb = H.token_lb90(one_token_winner)
    assert lb is not None and lb < 0  # one lucky token cannot carry the bound


def test_report_counts_every_exclusion(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    w.price("TOK", 1.0)
    w.now += 31_000
    ev.step()
    w.price("TOK", 0.5)
    w.now += 31_000
    ev.step()
    rep = H.build_report(ev.ledger.lines, w.now)
    h5 = rep["arms"]["H5"]
    assert h5["triggers"] == 1 and h5["entered"] == 1 and h5["settled"] == 1
    assert h5["verdict"]["verdict"] == "PENDING"
    assert any("partial" in s for s in rep["scope"]) and rep["authority"]["financial_execution"] is False


# ---------------------------------------------------------------- review fixes (Codex, 2026-11-03)

def test_seen_ms_is_the_clock_after_the_slow_tape_and_history_reads(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now - 1_000, "tx1")
    slow_new, slow_hist = w.fetch_new, w.fetch_history

    def fetch_new(members, since):
        rows = slow_new(members, since)
        w.now += 60_000  # the query takes a minute
        return rows

    def fetch_history(*a):
        out = slow_hist(*a)
        w.now += 10_000
        return out

    ev.fetch_new, ev.fetch_history = fetch_new, fetch_history
    started = w.now
    ev.poll()
    [trig] = [t for t in kinds(ev, "trigger") if t["arm"] == "H5"]
    assert trig["seen_ms"] == started + 70_000  # after both reads completed
    w.price("TOK", 1.0)
    ev.step()
    assert not kinds(ev, "entry")  # no entry at query completion
    w.now += 30_000
    ev.step()
    [ent] = kinds(ev, "entry")
    assert ent["ts"] >= trig["seen_ms"] + H.ENTRY_DELAY_MS


def test_entry_and_observation_times_are_the_clock_after_a_slow_rpc(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    w.price("TOK", 1.0)
    real = w.read

    def slow_read(token):
        got = real(token)
        w.now += 20_000
        return got

    ev.read_price = slow_read
    w.now += 31_000
    before = w.now
    ev.step()
    [ent] = kinds(ev, "entry")
    assert ent["ts"] == before + 20_000 and ent["delay_ms"] == ent["ts"] - ent["seen_ms"]


def test_a_read_completing_after_the_entry_window_does_not_enter(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    seen = kinds(ev, "trigger")[0]["seen_ms"]
    w.price("TOK", 1.0)
    real = w.read

    def very_slow(token):
        got = real(token)
        w.now = seen + H.ENTRY_WINDOW_MS + 1
        return got

    ev.read_price = very_slow
    w.now = seen + H.ENTRY_WINDOW_MS - 5_000
    ev.step()
    assert not kinds(ev, "entry")
    ev.step()
    assert kinds(ev, "entry_unpriced")


def test_wal_admission_comes_before_every_connection(tmp_path, monkeypatch):
    db = tmp_path / "kaiba.db"
    db.write_bytes(b"")
    (tmp_path / "kaiba.db-wal").write_bytes(b"x" * 11)
    monkeypatch.setattr(H, "WAL_LIMIT", 10)
    opened = []
    fetch_new, fetch_history = H.box_fetchers(db, connect=lambda p: opened.append(p) or pytest.fail("opened"))
    with pytest.raises(H.ReadDeferred):
        fetch_new(["S"], 0)
    with pytest.raises(H.ReadDeferred):
        fetch_history("S", "TOK", 0, 1)
    assert opened == []


def test_a_deferred_history_read_is_never_a_first_buy_and_is_retried(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    w.add("S", "NEW", "buy", w.now, "txs")  # would be B1 with an empty history
    real = w.fetch_history
    state = {"refuse": True}

    def gated(*a):
        if state["refuse"]:
            raise H.ReadDeferred("wal_over_limit")
        return real(*a)

    ev.fetch_history = gated
    ev.poll()
    assert not kinds(ev, "trigger") and kinds(ev, "read_deferred_begin")
    state["refuse"] = False
    w.now += 30_000
    ev.poll()
    assert [t["arm"] for t in kinds(ev, "trigger")] == ["B1"] and kinds(ev, "read_deferred_end")


def test_a_torn_last_line_is_preserved_and_published_before_continuing(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    tid = kinds(ev, "trigger")[0]["id"]
    path = tmp_path / "ledger.jsonl"
    valid = path.read_bytes()
    torn = ('{"arm": "H5", "delay_ms": 31000, "id": "' + tid + '", "price_usd": 1.0, "so').encode()
    path.write_bytes(valid + torn)
    ev2 = make(tmp_path, w)  # must not crash
    assert path.read_bytes().startswith(valid)
    sidecars = list(tmp_path.glob("ledger.jsonl.torn-*"))
    assert len(sidecars) == 1 and sidecars[0].read_bytes() == torn
    [rec] = kinds(ev2, "recovery")
    assert rec["torn_sha256"] == hashlib.sha256(torn).hexdigest() and rec["torn_id"] == tid
    assert rec["state"] == "unknown_interval"
    assert [r["id"] for r in kinds(ev2, "recovery_unknown")] == [tid]
    assert tid not in ev2.pending and tid not in ev2.opened
    order = [r["type"] for r in ev2.ledger.lines]
    assert order.index("recovery") < order.index("restart")
    rep = H.build_report(ev2.ledger.lines, w.now)
    assert rep["unknown_intervals"]["recoveries"] and rep["unknown_intervals"]["recovery_unknown_ids"] == 1


def test_a_bad_line_that_is_not_the_tail_refuses(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_bytes(b'{"type": "x", "id": "1", "ts": 1}\nnot json\n{"type": "x", "id": "2", "ts": 2}\n')
    with pytest.raises(H.LedgerCorrupt):
        H.Ledger(path)


# ---------------------------------------------------------------- root follow-up (A14)

def _synthetic(n_known_win, n_lost, *, lost_kind="entered"):
    """Ledger lines: n_known_win H5 entries exiting at +50%, and n_lost whose outcome a recovery lost."""
    lines = [{"type": "heartbeat_first", "id": "first", "ts": 0}]
    for i in range(n_known_win + n_lost):
        tid = f"t{i:03d}"
        lines.append({"type": "trigger", "id": tid, "arm": "H5", "token": f"TK{i}", "status": "pending",
                      "trigger_ts": i * MIN, "seen_ms": i * MIN})
        ent = {"type": "entry", "id": tid, "arm": "H5", "token": f"TK{i}", "trigger_ts": i * MIN, "ts": i * MIN,
               "seen_ms": i * MIN, "delay_ms": 30_000, "price_usd": 1.0, "liquidity_usd": 1e12, "sol_usd": 150.0,
               "venue": "pumpswap"}
        if i < n_known_win:
            lines.append(ent)
            lines.append({"type": "exit", "id": tid, "arm": "H5", "token": f"TK{i}", "ts": i * MIN + 1,
                          "reason": "take_profit", "price_usd": 1.5, "liquidity_usd": 1e12, "sol_usd": 150.0,
                          "venue": "pumpswap"})
        else:
            if lost_kind == "entered":
                lines.append(ent)
            lines.append({"type": "recovery_unknown", "id": tid, "ts": i * MIN + 1, "torn_sha256": "x"})
    return lines


def test_recovery_unknown_exposures_can_never_produce_a_false_pass():
    rep = H.build_report(_synthetic(40, 40), DAY)
    v = rep["arms"]["H5"]["verdict"]
    assert v["verdict"] == "INCONCLUSIVE" and v["n"] == 80 and v["n_known"] == 40
    assert v["worst_case"] < 0  # the 40 unknowns sit in the worst case at -100%
    assert rep["arms"]["H5"]["recovery_unknown"] == {"entered": 40, "pending": 0}
    control = H.build_report(_synthetic(40, 0), DAY)["arms"]["H5"]["verdict"]
    assert control["verdict"] == "PASS"
    pending = H.build_report(_synthetic(40, 5, lost_kind="pending"), DAY)["arms"]["H5"]
    assert pending["recovery_unknown"] == {"entered": 0, "pending": 5}
    assert pending["verdict"]["verdict"] == "INCONCLUSIVE"


def _torn_ledger(tmp_path):
    w = World()
    ev = make(tmp_path, w)
    w.now += MIN
    reentry(w, "S", "TOK", w.now, "tx1")
    ev.poll()
    tid = kinds(ev, "trigger")[0]["id"]
    path = tmp_path / "ledger.jsonl"
    valid = path.read_bytes()
    torn = ('{"arm": "H5", "id": "' + tid + '", "price_usd": 1.0, "so').encode()
    path.write_bytes(valid + torn)
    return w, path, valid, torn, tid


def test_a_death_after_truncation_but_before_publication_is_reconciled(tmp_path):
    w, path, valid, torn, tid = _torn_ledger(tmp_path)
    H.Ledger(path)  # recovers the tail, then the process "dies" before Evaluator.start
    assert path.read_bytes() == valid
    again = H.Ledger(path)
    assert len(again.recovered) == 1 and again.recovered[0]["torn_sha256"] == hashlib.sha256(torn).hexdigest()
    ev = make(tmp_path, w)
    assert [r["id"] for r in kinds(ev, "recovery_unknown")] == [tid]
    assert kinds(ev, "recovery") and H.Ledger(path).recovered == []  # applied once, never again
    assert len(list(tmp_path.glob("ledger.jsonl.torn-*"))) == 1


def test_a_death_between_intent_and_truncation_reuses_the_intent(tmp_path, monkeypatch):
    w, path, valid, torn, tid = _torn_ledger(tmp_path)
    real_open = H.Path.open

    def no_truncate(self, mode="r", *a, **k):
        if mode == "r+b":
            raise RuntimeError("died before truncation")
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(H.Path, "open", no_truncate)
    with pytest.raises(RuntimeError):
        H.Ledger(path)
    monkeypatch.setattr(H.Path, "open", real_open)
    assert path.read_bytes() == valid + torn and len(list(tmp_path.glob("ledger.jsonl.recovery-*.json"))) == 1
    led = H.Ledger(path)
    assert path.read_bytes() == valid and len(led.recovered) == 1
    assert len(list(tmp_path.glob("ledger.jsonl.torn-*"))) == 1  # no second sidecar


def test_the_report_reader_never_writes_or_truncates(tmp_path):
    w, path, valid, torn, tid = _torn_ledger(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    led = H.Ledger(path, readonly=True)
    assert path.read_bytes() == valid + torn and led.torn_tail_present
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_wal_admission_at_the_real_sqlite_boundary(tmp_path, monkeypatch):
    db = tmp_path / "kaiba.db"
    db.write_bytes(b"")
    (tmp_path / "kaiba.db-wal").write_bytes(b"x")
    monkeypatch.setattr(H, "WAL_LIMIT", 0)
    calls = []
    monkeypatch.setattr(H.sqlite3, "connect", lambda *a, **k: calls.append(a) or pytest.fail("connected"))
    fetch_new, fetch_history = H.box_fetchers(db)  # the default connector, not an injected one
    with pytest.raises(H.ReadDeferred):
        fetch_new(["S"], 0)
    with pytest.raises(H.ReadDeferred):
        fetch_history("S", "TOK", 0, 1)
    assert calls == []
