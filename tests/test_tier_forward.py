"""B-tier forward paper evaluator (kaiba/learning/tier_forward.py), against
docs/research/claude-tier-forward-prereg-20261105.md. Fakes only: no network, no DB."""
from __future__ import annotations

import hashlib
import json

import pytest

from kaiba.learning import tier_forward as T
from kaiba.learning.h5_reentry import Ledger, Read

MIN = T.MIN
T0 = T.T0_MS
START = T0 + 60 * MIN
E = 1e-4  # $100k cap


def ladder(*quotes, entry=E):
    lad = T.Ladder(entry, START)
    for i, q in enumerate(quotes, 1):
        lad.step(q * entry, START + i * 30_000)
    return lad


# ---------------------------------------------------------------- the ladder

def test_first_leg_stop_books_the_measured_minus_40_at_least():
    lad = ladder(0.95, 0.70)
    assert (lad.reason, lad.net) == ("stop", -40.0)


def test_a_stop_that_gapped_books_the_quote_when_that_is_worse():
    assert ladder(0.40).net == pytest.approx(-65.5)


def test_above_the_stop_nothing_happens():
    assert ladder(0.71).net is None


def test_take_profit_then_breakeven_floor_on_the_rest():
    lad = ladder(2.0, 1.0)
    # 50% sold at 2x is +50; the other half closes at the floor for 0; less the round trip.
    assert (lad.reason, lad.net) == ("breakeven", pytest.approx(50 - T.COST))


def test_trail_from_1_2x_fires_at_the_quote_read():
    lad = ladder(1.25, 0.99)  # 20% off 1.25 is 1.00
    assert lad.reason == "trail"
    assert lad.net == pytest.approx(-1 - T.COST)


def test_the_trail_is_not_armed_below_1_2x():
    assert ladder(1.19, 0.80).net is None


def test_the_trail_tightens_with_the_peak():
    # Peak 10x trails 20%, not the 30% of the 2x rung. 8.1 holds, 7.9 sells.
    assert ladder(10.0, 8.1).net is None
    assert ladder(10.0, 7.9).reason == "trail"


def test_stale_needs_an_hour_inside_one_percent():
    lad = T.Ladder(E, START)
    lad.step(E * 1.009, START + 59 * MIN)
    assert lad.net is None
    lad.step(E * 1.005, START + 60 * MIN)
    assert lad.reason == "stale"


def test_a_move_outside_the_band_restarts_the_stale_clock():
    lad = T.Ladder(E, START)
    lad.step(E * 1.02, START + 59 * MIN)
    lad.step(E * 1.02, START + 60 * MIN)
    assert lad.net is None
    lad.step(E * 1.02, START + 119 * MIN)
    assert lad.reason == "stale"


def test_max_hold_is_200_minutes():
    lad = T.Ladder(E, START)
    for i in range(1, 200):
        lad.step(E * (1.02 + 0.02 * (i % 2)), START + i * MIN)
        assert lad.net is None
    lad.step(E * 1.02, START + 200 * MIN)
    assert lad.reason == "max_hold"


def test_a_closed_ladder_does_not_move():
    lad = ladder(0.5)
    net = lad.net
    lad.step(5 * E, START + 10 * MIN)
    assert lad.net == net


# ---------------------------------------------------------------- the evaluator

class Rig:
    def __init__(self, tmp_path, rows=None, price=E, liq=50_000.0):
        self.t = START
        self.rows = rows or []
        self.price, self.liq = price, liq
        self.ok, self.no_venue = True, False
        self.reads = 0
        self.path = tmp_path / "ledger.jsonl"
        self.ledger = Ledger(self.path, now_ms=lambda: self.t)
        self.ev = self.make()

    def make(self):
        def fetch(_members, since):
            return [r for r in self.rows if r["ts_ms"] >= since]

        def read(_token):
            self.reads += 1
            if not self.ok:
                return Read(False, no_venue=self.no_venue)
            return Read(True, self.price, self.liq, 100.0, "pumpswap", "pool")

        return T.Evaluator(self.ledger, ["W"], fetch_new=fetch, read_price=read, now=lambda: self.t)

    def kinds(self):
        return [r["type"] for r in self.ledger.lines]


def buy(token="Apump", wallet="W", ts=None, tx="t1", side="buy"):
    return {"wallet": wallet, "token": token, "side": side, "ts_ms": START - 2 * MIN if ts is None else ts, "tx": tx,
            "source": "gmgn:tracked"}


def started(tmp_path, rows=None, **kw):
    rig = Rig(tmp_path, rows, **kw)
    rig.ev.start()
    return rig


def test_a_first_buy_triggers_once_per_token(tmp_path):
    rig = started(tmp_path, [buy(), buy(wallet="X", tx="t2")])
    assert rig.ev.poll() == 1
    rig.t += 30_000
    assert rig.ev.poll() == 0
    assert rig.ledger.lines[-1]["token"] == "Apump"


def test_seen_ms_is_the_read_and_lag_is_recorded(tmp_path):
    rig = started(tmp_path, [buy()])
    rig.ev.poll()
    trig = rig.ledger.lines[-1]
    assert trig["seen_ms"] == START
    assert trig["lag_ms"] == 2 * MIN


@pytest.mark.parametrize("row", [buy(side="sell"), buy(token="Abonk"), buy(ts=T0 - 1),
                                 buy(token="So11111111111111111111111111111111111111112")])
def test_not_a_trigger(tmp_path, row):
    rig = started(tmp_path, [row])
    assert rig.ev.poll() == 0


def test_a_row_before_t0_is_refused_even_if_the_reader_returns_it(tmp_path):
    rig = started(tmp_path)
    rig.rows = [buy(ts=T0 - 1)]
    rig.ev.fetch_new = lambda _members, _since: rig.rows
    assert rig.ev.poll() == 0


def test_a_token_bought_before_t0_is_not_a_first_buy(tmp_path):
    rig = started(tmp_path, [buy(ts=T0 - 2 * T.DAY, tx="old"), buy(tx="new")])
    assert rig.ev.poll() == 0


def test_a_buy_older_than_the_lookback_does_not_count(tmp_path):
    rig = started(tmp_path, [buy(ts=T0 - 14 * T.DAY, tx="old"), buy(tx="new")])
    assert rig.ev.poll() == 1


def test_a_row_over_an_hour_old_is_counted_too_late_and_never_entered(tmp_path):
    rig = started(tmp_path, [buy(ts=T0 + MIN)])
    rig.t = T0 + 62 * MIN
    rig.ev.poll()
    assert rig.ledger.lines[-1]["status"] == "too_late"
    rig.t += 2 * MIN
    rig.ev.step()
    assert "entry" not in rig.kinds()
    assert rig.reads == 0


def test_entry_waits_60_seconds_from_seen(tmp_path):
    rig = started(tmp_path, [buy()])
    rig.ev.poll()
    rig.t += 59_000
    rig.ev.step()
    assert rig.reads == 0
    rig.t += 1_000
    rig.ev.step()
    assert rig.kinds()[-1] == "entry"


@pytest.mark.parametrize(("price", "liq"), [(9.9e-6, 50_000.0), (3.1e-3, 50_000.0), (E, 9_999.0)])
def test_outside_the_live_universe_is_counted_not_traded(tmp_path, price, liq):
    rig = started(tmp_path, [buy()], price=price, liq=liq)
    rig.ev.poll()
    rig.t += 60_000
    rig.ev.step()
    assert rig.kinds()[-1] == "excluded_universe"
    assert not rig.ev.pending


@pytest.mark.parametrize(("no_venue", "kind"), [(True, "unpriced_venue"), (False, "entry_unpriced")])
def test_no_price_in_ten_minutes_is_counted(tmp_path, no_venue, kind):
    rig = started(tmp_path, [buy()])
    rig.ok, rig.no_venue = False, no_venue
    rig.ev.poll()
    rig.t += 60_000
    rig.ev.step()
    assert "entry" not in rig.kinds()
    rig.t += 10 * MIN
    rig.ev.step()
    assert rig.kinds()[-1] == kind


def open_trade(tmp_path, **kw):
    rig = started(tmp_path, [buy()], **kw)
    rig.ev.poll()
    rig.t += 60_000
    rig.ev.step()
    assert rig.kinds()[-1] == "entry"
    return rig


def test_quotes_wait_for_the_interval(tmp_path):
    rig = open_trade(tmp_path)
    before = rig.reads
    rig.t += 29_000
    rig.ev.step()
    assert rig.reads == before
    rig.t += 1_000
    rig.ev.step()
    assert rig.reads == before + 1


def test_a_stop_writes_the_exit_on_the_same_pass(tmp_path):
    rig = open_trade(tmp_path)
    rig.price = E * 0.68
    rig.t += 30_000
    rig.ev.step()
    assert rig.kinds()[-2:] == ["obs", "exit"]
    assert rig.ledger.lines[-1]["net"] == -40.0
    assert not rig.ev.opened


def test_a_restart_rebuilds_the_ladder_from_the_ledger(tmp_path):
    rig = open_trade(tmp_path)
    rig.price = E * 2
    rig.t += 30_000
    rig.ev.step()
    ledger = Ledger(rig.path, now_ms=lambda: rig.t)
    rig.ledger = ledger
    ev = rig.make()
    tid = next(iter(ev.opened))
    assert ev.ladders[tid].taken == 1
    assert ev.ladders[tid].floor == 1.0
    assert ev.known == {"Apump"}


def test_a_death_between_the_obs_and_the_exit_closes_without_another_read(tmp_path):
    rig = open_trade(tmp_path)
    tid = next(iter(rig.ev.opened))
    rig.t += 30_000
    rig.ledger.append({"type": "obs", "id": f"{tid}:{rig.t}", "entry_id": tid, "ts": rig.t, "price_usd": E * 0.5,
                       "liquidity_usd": 1.0, "venue": "pumpswap"})
    rig.ledger = Ledger(rig.path, now_ms=lambda: rig.t)
    ev = rig.make()
    before = rig.reads
    ev.step()
    assert rig.reads == before
    assert rig.ledger.lines[-1]["type"] == "exit"


def test_a_token_is_not_triggered_again_after_a_restart(tmp_path):
    rig = open_trade(tmp_path)
    rig.ledger = Ledger(rig.path, now_ms=lambda: rig.t)
    ev = rig.make()
    ev.start()
    assert ev.poll() == 0


def test_no_quote_for_30_minutes_is_unresolved_with_a_full_loss_worst_case(tmp_path):
    rig = open_trade(tmp_path)
    rig.ok = False
    rig.t += 30 * MIN
    rig.ev.step()
    last = rig.ledger.lines[-1]
    assert last["type"] == "unresolved"
    assert last["net"] == pytest.approx(-T.COST)
    assert last["net_worst"] == pytest.approx(-100 - T.COST)
    assert not rig.ev.opened


# ---------------------------------------------------------------- the report

def closed(i, net, lag=5 * MIN, cap=1e5):
    tok, tid = f"T{i}pump", f"id{i}"
    ts = START + i * MIN
    return [{"type": "entry", "id": tid, "token": tok, "trigger_ts": ts, "lag_ms": lag, "cap_usd": cap, "ts": ts},
            {"type": "exit", "id": tid, "token": tok, "ts": ts + MIN, "net": net, "reason": "trail"}]


def test_under_40_is_pending_and_decides_nothing():
    lines = [{"type": "heartbeat_first", "ts": START}] + [x for i in range(39) for x in closed(i, 50.0)]
    assert T.build_report(lines, START)["F1"]["verdict"] == "PENDING"


def test_a_profitable_40_passes_and_a_losing_one_fails():
    head = [{"type": "heartbeat_first", "ts": START}]
    win = [x for i in range(40) for x in closed(i, 50.0 + (i % 7))]
    assert T.build_report(head + win, START)["F1"]["verdict"] == "PASS"
    lose = [x for i in range(40) for x in closed(i, -5.0 + (i % 3))]
    assert T.build_report(head + lose, START)["F1"]["verdict"] == "FAIL"


def test_the_lag_split_and_bands_are_context_only():
    lines = [{"type": "heartbeat_first", "ts": START}] + closed(1, 10.0, lag=14 * MIN) + closed(2, -20.0, lag=16 * MIN, cap=2e6)
    ctx = T.build_report(lines, START)["context"]
    assert ctx["lag_15m_or_less"] == {"n": 1, "mean_pct": 10.0}
    assert ctx["lag_over_15m"] == {"n": 1, "mean_pct": -20.0}
    assert ctx["bands"]["$1000k-3000k"]["n"] == 1


def test_an_unresolved_trade_counts_at_its_worst_case():
    lines = [{"type": "heartbeat_first", "ts": START},
             {"type": "entry", "id": "x", "token": "Xpump", "trigger_ts": START, "lag_ms": 0, "cap_usd": 1e5, "ts": START},
             {"type": "unresolved", "id": "x", "token": "Xpump", "ts": START + MIN, "net": -5.5, "net_worst": -105.5}]
    row = T._settled(lines)[0]
    assert row["net_base"] == pytest.approx(-0.055)
    assert row["net_worst"] == pytest.approx(-1.055)


def test_the_report_carries_no_authority():
    assert T.build_report([], START)["authority"] == {"financial_execution": False, "promotion": False}


# ---------------------------------------------------------------- frozen inputs

def test_a_changed_wallet_file_refuses(tmp_path, monkeypatch):
    raw = json.dumps({"members": ["W"]}).encode()
    (tmp_path / T.MEMBERS_FILE).write_bytes(raw)
    with pytest.raises(T.FrozenListMismatch):
        T.load_members(tmp_path)
    monkeypatch.setattr(T, "MEMBERS_SHA", hashlib.sha256(raw).hexdigest())
    assert T.load_members(tmp_path) == ["W"]
