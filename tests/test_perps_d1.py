"""The frozen D1 breakout on Binance USD-M perps (PERPS-D1-PAPER-CLAUDE-20261103).

Each test is a way the forward paper book could stop being the strategy that was measured: a replay
that no longer reproduces the research's 127 trades, a data source whose daily bars differ from the
research's, a gap or bad price passed through as data, a signal from before the registration
counted forward, an entry or time exit at the wrong price, funding charged over the wrong window, or
a pass line that passes on too little. No test touches the network.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import math
from pathlib import Path

import pytest

from kaiba.perps import binance_public as bp
from kaiba.perps import paper as P
from kaiba.perps.d1_breakout import DAY_MS, Bar, Trade, funding_paid, replay

F = Path(__file__).parent / "fixtures" / "perps"


def load(sym):
    rows = list(csv.DictReader(io.StringIO(gzip.decompress((F / f"{sym}_1d.csv.gz").read_bytes()).decode())))
    bars = [Bar(float(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])) for r in rows]
    fr = list(csv.DictReader(io.StringIO(gzip.decompress((F / f"{sym}_funding.csv.gz").read_bytes()).decode())))
    return bars, [float(r["t"]) for r in fr], [float(r["r"]) for r in fr]


# ------------------------------------------------------------------ parity with the research


def test_replay_reproduces_the_research_trade_list_exactly():
    """AEGIS OMNI research/prop_perps_2026_10: 127 trades, +0.41R (fixture built by the research code)."""
    exp = json.loads((F / "expected_trades.json").read_text())["trades"]
    got = []
    for sym in ("BTCUSDT", "ETHUSDT"):
        got += replay(sym, *load(sym))[0]
    assert len(got) == len(exp) == 127
    for e, g in zip(exp, got, strict=True):
        assert (e["symbol"], e["side"], e["entry_t"], e["exit_t"], e["reason"]) == (g.symbol, g.side, g.entry_t, g.exit_t, g.reason)
        assert e["entry"] == g.entry and math.isclose(e["exit"], g.exit, rel_tol=1e-12)
        assert abs(e["r"] - g.r) < 1e-9 and abs(e["cost_r"] - g.cost_r) < 1e-9
    assert round(sum(t.r for t in got) / len(got), 4) == 0.4084


def test_binance_daily_klines_equal_the_research_bars():
    """The forward book reads fapi 1d klines; the research bucketed 1h klines on UTC days. 30 days of
    September 2026 (fetched once from the box) agree on every open, high, low and close."""
    bars, _ = bp.parse_klines(json.loads((F / "fapi_btcusdt_1d_202609.json").read_text()), now_ms=1_800_000_000_000)
    research = {b.t: b for b in load("BTCUSDT")[0]}
    assert len(bars) == 30
    for b in bars:
        assert research[b.t] == b


# ------------------------------------------------------------------ data refusal


def kline(t, o, h, lo, c):
    return [int(t), str(o), str(h), str(lo), str(c), "1", int(t) + DAY_MS - 1]


def test_parse_klines_splits_closed_days_from_the_day_in_progress():
    t0 = 1_793_664_000_000  # 2026-11-03 00:00 UTC
    rows = [kline(t0 - 2 * DAY_MS, 10, 11, 9, 10.5), kline(t0 - DAY_MS, 10.5, 12, 10, 11), kline(t0, 11, 11.2, 10.8, 11.1)]
    bars, nxt = bp.parse_klines(rows, now_ms=t0 + 5 * 3_600_000)
    assert [b.t for b in bars] == [t0 - 2 * DAY_MS, t0 - DAY_MS] and nxt == 11.0


@pytest.mark.parametrize("rows", [
    [kline(0, 10, 11, 9, 10), kline(2 * DAY_MS, 10, 11, 9, 10)],         # a missing day
    [kline(0, 10, 11, 9, 12)],                                            # close above the high
    [kline(0, 10, 11, -1, 10)],                                           # negative low
    [[0, "10", "11", "9", "10", "1", DAY_MS - 2]],                       # not a whole UTC day
    {"code": -1121, "msg": "Invalid symbol."},                           # an error body
])
def test_a_series_that_cannot_be_trusted_is_refused(rows):
    with pytest.raises(bp.DataRefused):
        bp.parse_klines(rows, now_ms=10 * DAY_MS)


def test_funding_rows_are_parsed_and_absurd_rates_refused():
    t, r = bp.parse_funding([{"fundingTime": 2, "fundingRate": "-0.0001"}, {"fundingTime": 1, "fundingRate": "0.0001"}])
    assert t == [1.0, 2.0] and r == [0.0001, -0.0001]
    with pytest.raises(bp.DataRefused):
        bp.parse_funding([{"fundingTime": 1, "fundingRate": "0.5"}])


# ------------------------------------------------------------------ forward rules


def test_forward_counts_only_signals_on_bars_closing_after_registration():
    bars, ft, fr = load("BTCUSDT")
    cutoff = 1_719_792_000_000  # 2024-07-01 00:00 UTC
    trades, _ = replay("BTCUSDT", bars, ft, fr, first_signal_close_ms=cutoff)
    assert trades and all(t.entry_t >= cutoff for t in trades)
    full, _ = replay("BTCUSDT", bars, ft, fr)
    # from the first research trade entered after the cutoff while flat, the paths are identical
    tail = [t for t in full if t.entry_t >= trades[0].entry_t]
    assert tail == trades


def rising(n=260):
    out, p = [], 100.0
    for i in range(n):
        o, p = p, p * 1.001
        out.append(Bar(float(i * DAY_MS), o, p * 1.01, o * 0.99, p))
    return out


def test_a_last_bar_breakout_enters_at_todays_open_and_only_once_today_has_started():
    bars = rising()
    last = bars[-1]
    jump = Bar(last.t + DAY_MS, last.c, last.c * 1.10, last.c * 0.999, last.c * 1.08)
    bars = [*bars, jump]
    trades, pos = replay("X", bars, [], [], next_open=123.0)
    assert trades == [] and pos is not None and pos.side == 1 and pos.entry == 123.0 and pos.bars_held == 0
    assert replay("X", bars, [], [])[1] is None  # today's open not known yet: no entry


def test_a_thirty_bar_time_exit_fills_at_todays_open():
    bars = rising()
    last = bars[-1]
    bars.append(Bar(last.t + DAY_MS, last.c, last.c * 1.10, last.c * 0.999, last.c * 1.08))  # signal bar
    entry = bars[-1].c
    for _ in range(30):  # 30 quiet bars after the entry: neither stop nor target
        t = bars[-1].t + DAY_MS
        bars.append(Bar(t, entry, entry * 1.001, entry * 0.999, entry))
    trades, pos = replay("X", bars, [], [], next_open=entry * 1.002)
    assert pos is None and len(trades) == 1
    t = trades[0]
    assert t.reason == "time" and t.exit == entry * 1.002 and t.exit_t == bars[-1].t + DAY_MS


def entered(test_bar):
    """A long entered at E on the bar after a breakout, one quiet bar, then ``test_bar(E, stop, target)``."""
    bars = rising()
    last = bars[-1]
    bars.append(Bar(last.t + DAY_MS, last.c, last.c * 1.10, last.c * 0.999, last.c * 1.08))  # signal bar
    e = bars[-1].c
    pos = replay("X", bars, [], [], next_open=e)[1]
    bars.append(Bar(bars[-1].t + DAY_MS, e, e * 1.0001, e * 0.9999, e))                     # entry bar, quiet
    bars.append(test_bar(e, pos.stop, pos.target, bars[-1].t + DAY_MS))
    trades, _ = replay("X", bars, [], [])
    return trades[0], pos


def test_a_bar_touching_both_stop_and_target_is_a_stop():
    """Research rule: the stop is assumed first. The parity data never has such a daily bar."""
    tr, pos = entered(lambda e, s, tg, t: Bar(t, e, tg * 1.01, s * 0.99, e))
    assert tr.reason == "stop" and tr.exit == pos.stop and tr.r < 0


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    """Research rule: a gap below a long's stop fills at that bar's open (worse than the stop)."""
    tr, pos = entered(lambda e, s, tg, t: Bar(t, s * 0.95, s * 0.96, s * 0.90, s * 0.92))
    assert tr.reason == "stop" and tr.exit == pos.stop * 0.95 and tr.exit < pos.stop


def test_funding_is_charged_after_entry_up_to_and_including_exit():
    ft, fr = [0.0, 8.0, 16.0, 24.0], [0.1, 0.2, 0.3, 0.4]
    assert funding_paid(ft, fr, 0.0, 16.0, 1) == pytest.approx(0.5)      # (0, 16]
    assert funding_paid(ft, fr, 0.0, 16.0, -1) == pytest.approx(-0.5)    # shorts receive
    assert funding_paid(ft, fr, 24.0, 99.0, 1) == 0.0


# ------------------------------------------------------------------ pass line


def trades_with(rs, syms=("A", "B", "C", "D", "E")):
    return [Trade(syms[i % len(syms)], 1, float(i), float(i + 1), 1.0, 1.0, 0.9, r, r, 0.0, "target")
            for i, r in enumerate(rs)]


def test_the_pass_line():
    assert P.verdict(trades_with([1.0] * 39), P.REGISTERED_MS + DAY_MS)["verdict"] == "PENDING"
    assert P.verdict(trades_with([0.5] * 39), P.REGISTERED_MS + 400 * DAY_MS)["verdict"] == "INCONCLUSIVE"
    assert P.verdict(trades_with([0.4, -0.2] * 20), P.REGISTERED_MS)["verdict"] == "PASS"
    assert P.verdict(trades_with([1.0] * 20 + [-0.5] * 20), P.REGISTERED_MS)["verdict"] == "FAIL"  # second half < 0
    assert P.verdict(trades_with([-0.1] * 40), P.REGISTERED_MS)["verdict"] == "FAIL"


# ------------------------------------------------------------------ a whole run, offline


class FakeHttp:
    def __init__(self, bars, now_ms):
        self.bars, self.now_ms, self.calls = bars, now_ms, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        body = ([kline(b.t, b.o, b.h, b.l, b.c) for b in self.bars] if url.endswith("/klines") else [])
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: body})()


def test_a_run_writes_the_report_and_counts_nothing_before_registration(tmp_path):
    hist = load("BTCUSDT")[0][-300:]
    now = P.REGISTERED_MS + 3 * 3_600_000
    shift = (now // DAY_MS) * DAY_MS - hist[-1].t
    bars = [Bar(b.t + shift, b.o, b.h, b.l, b.c) for b in hist]  # the last bar is the day in progress
    http = FakeHttp(bars, now)
    rep = P.run(now_ms=now, http=http, out_dir=tmp_path)
    assert (tmp_path / "report.json").exists() and json.loads((tmp_path / "report.json").read_text())["version"] == P.VERSION
    assert rep["pooled"] == {"n": 0} and rep["verdict"]["verdict"] == "PENDING" and rep["open_positions"] == []
    assert all(v["status"] == "ok" for v in rep["symbols"].values()) and len(rep["symbols"]) == 10
    assert all("fapi.binance.com" in u for u, _ in http.calls) and len(http.calls) == 20
