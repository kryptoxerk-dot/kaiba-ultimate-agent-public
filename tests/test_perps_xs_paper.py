"""Forward paper books for weekly cross-sectional momentum and carry (kaiba/perps/xs_paper.py)."""
from __future__ import annotations

import datetime as dt
import json
import math

import pytest

from kaiba.perps import xs_paper as xs

DAY = xs.DAY_MS
M = xs.FIRST_MONDAY_MS


def test_first_monday_is_the_registered_monday():
    d = dt.datetime.fromtimestamp(M / 1000, dt.UTC)
    assert (d.year, d.month, d.day, d.hour, d.weekday()) == (2026, 11, 9, 0, 0)
    assert xs.monday_of(M + 3 * DAY + 5) == M
    assert xs.monday_of(M - 1) == M - xs.WEEK_MS


def test_universe_rule():
    assert xs.in_universe("BTCUSDT") and xs.in_universe("1000PEPEUSDT")
    for s in ("USDCUSDT", "BTCDOMUSDT", "BTCUSDT_261225", "ETHBTC", "FDUSDUSDT"):
        assert not xs.in_universe(s)


def series(m, closes_by_day, qv=1e8):
    """Bars for days m-70..m (open = close = given or 1.0)."""
    out = {}
    for d in range(-70, 1):
        c = closes_by_day.get(d, 1.0)
        out[m + d * DAY] = (c, c, qv)
    return out


def test_signal_uses_saturday_close_and_ignores_sunday():
    base = series(M, {-2: 1.5, -16: 1.0})
    vol, sig = xs.eligible(base, M)
    assert sig == pytest.approx(0.5)
    sunday = series(M, {-2: 1.5, -16: 1.0, -1: 9.0})
    assert xs.eligible(sunday, M)[1] == pytest.approx(0.5)
    saturday = series(M, {-2: 2.0, -16: 1.0})
    assert xs.eligible(saturday, M)[1] == pytest.approx(1.0)


def test_eligibility_needs_sixty_gap_free_days_and_todays_bar():
    b = series(M, {})
    assert xs.eligible(b, M) is not None
    gap = dict(b)
    del gap[M - 37 * DAY]
    assert xs.eligible(gap, M) is None
    no_today = dict(b)
    del no_today[M]
    assert xs.eligible(no_today, M) is None


def test_funding_is_charged_on_mark_notional_not_entry():
    # Codex's synthetic: entry 100, mark 200 at the print, rate 1% -> 2% of entry notional.
    bars = {M + DAY: (200.0, 200.0, 1.0)}
    owed = xs.funding_owed([(M + DAY, 0.01)], M, M + 7 * DAY, 100.0, bars)
    assert owed == pytest.approx(0.02)
    assert xs.funding_sum([(M + DAY, 0.01)], M, M + 7 * DAY) == pytest.approx(0.01)


def test_funding_uses_the_prints_own_mark_when_binance_returns_one():
    bars = {M + DAY: (200.0, 200.0, 1.0)}
    with_mark = [(M + DAY + 8 * 3600_000, 0.01, 300.0)]
    assert xs.funding_owed(with_mark, M, M + 7 * DAY, 100.0, bars) == pytest.approx(0.03)
    no_mark = [(M + DAY + 8 * 3600_000, 0.01, None)]
    assert xs.funding_owed(no_mark, M, M + 7 * DAY, 100.0, bars) == pytest.approx(0.02)
    assert xs.funding_sum(with_mark, M, M + 7 * DAY) == pytest.approx(0.01)  # the signal ignores marks
    assert xs._mark("") is None and xs._mark("0") is None and xs._mark("64000.5") == 64000.5


def test_turnover_is_against_drifted_holdings():
    # Codex's synthetic: long +1 / short -1 both rise 10%, NAV flat, same targets -> 0.2 traded.
    w = {"A": 1.0, "B": -1.0}
    old = xs.drifted(w, {"A": 0.1, "B": 0.1}, 0.0)
    assert xs.turnover(w, old) == pytest.approx(0.2)
    assert xs.turnover(w, w) == 0.0
    assert xs.turnover(w, {}) == pytest.approx(2.0)


def make_universe(m, n=15):
    bars, funding = {}, {}
    for i in range(n):
        s = f"C{i:02d}USDT"
        bars[s] = series(m, {-2: 1.0 + i / 10, -16: 1.0}, qv=1e8 + i)
        funding[s] = [(m - 3 * DAY, 0.001 * (i - 7))]
    return bars, funding


def test_form_puts_winners_long_and_low_funding_long():
    bars, funding = make_universe(M)
    f = xs.form(M, bars, funding)
    assert f["n"] == 15
    assert f["momentum"]["longs"] == ["C12USDT", "C13USDT", "C14USDT"]
    assert f["momentum"]["shorts"] == ["C00USDT", "C01USDT", "C02USDT"]
    assert f["carry"]["longs"] == [f"C{i:02d}USDT" for i in range(5)]
    assert f["carry"]["shorts"] == [f"C{i:02d}USDT" for i in range(10, 15)]
    assert f["momentum"]["entry"]["C14USDT"] == pytest.approx(1.0)


def test_settle_exit_paths():
    legs = {"longs": ["A"], "shorts": ["B"], "entry": {"A": 10.0, "B": 10.0}}
    r = xs.settle(legs, M, {"A": 11.0, "B": 9.0}, {}, {}, 0.0)
    assert r["status"] == "settled" and r["gross"] == pytest.approx(0.2)
    assert r["rets"] == pytest.approx({"A": 0.1, "B": -0.1})
    gone = xs.settle(legs, M, {"A": 11.0}, {"B": (M + 2 * DAY, 5.0)}, {}, 0.0)
    assert gone["exits"]["B"]["how"] == "last_close" and gone["gross"] == pytest.approx(0.6)
    assert xs.settle(legs, M, {"A": 11.0}, {}, {}, 0.0)["status"] == "unsettled"
    stale = xs.settle(legs, M, {"A": 11.0}, {"B": (M - 3 * DAY, 5.0)}, {}, 0.0)
    assert stale["status"] == "unsettled"  # a mark from before entry never closes a position
    cost = xs.settle(legs, M, {"A": 10.0, "B": 10.0}, {}, {}, 2.0)
    assert cost["net"] == pytest.approx(-2.0 * xs.BPS / 1e4)


class FakeHTTP:
    """Serves exchangeInfo, daily klines and funding from synthetic in-memory data."""

    def __init__(self, n=15, delisted=()):
        self.n, self.delisted, self.calls = n, set(delisted), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        http = self

        class R:
            def raise_for_status(self):
                if getattr(self, "bad", False):
                    raise RuntimeError("400")

            def json(self):
                return http.payload(url, params or {})
        r = R()
        sym = (params or {}).get("symbol")
        r.bad = sym in self.delisted
        return r

    def payload(self, url, params):
        syms = [f"C{i:02d}USDT" for i in range(self.n)]
        if url.endswith("exchangeInfo"):
            def filters(s):
                if int(s[1:3]) % 2:
                    return []  # odd symbols carry no filters: the snapshot must say so, not invent them
                return [{"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"}]
            return {"serverTime": M + 1800_000,
                    "symbols": [{"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING",
                                 "filters": filters(s)} for s in syms if s not in self.delisted]}
        if url.endswith("klines"):
            i = int(params["symbol"][1:3])
            rows = []
            for d in range(-80, 30):
                t = M + d * DAY
                c = (1.0 + i / 100) ** (d / 7)
                rows.append([t, str(c), str(c), str(c), str(c), "0", t + DAY - 1, str(1e8 + i)])
            return rows
        if url.endswith("fundingRate"):
            i = int(params["symbol"][1:3])
            start = int(params["startTime"])
            return [{"fundingTime": t, "fundingRate": str(0.0001 * (i - 7))}
                    for t in range(M - 20 * DAY, M + 30 * DAY, 8 * 3600 * 1000) if t >= start]
        raise AssertionError(url)


def test_run_forms_on_the_registered_monday_and_settles_a_week_later(tmp_path):
    http = FakeHTTP()
    rep = xs.run(now_ms=M - xs.WEEK_MS + 1800_000, http=http, out_dir=tmp_path)
    assert rep["books"]["momentum"]["weeks"] == 0 and not rep["open"]  # nothing before registration
    rep = xs.run(now_ms=M + 1800_000, http=http, out_dir=tmp_path)
    assert rep["open"] and rep["open"][0]["m"] == M
    rep = xs.run(now_ms=M + 3 * DAY, http=http, out_dir=tmp_path)  # mid-week: no new formation
    assert rep["books"]["momentum"]["weeks"] == 0
    rep = xs.run(now_ms=M + xs.WEEK_MS + 1800_000, http=http, out_dir=tmp_path)
    assert rep["books"]["momentum"]["weeks"] == 1 and rep["books"]["carry"]["weeks"] == 1
    assert rep["mix_50_50"]["weeks"] == 1
    assert rep["verdict"] == "PENDING"
    # The winners kept winning in this synthetic market: momentum's first week is positive.
    assert rep["books"]["momentum"]["mean"] > 0


def test_a_missed_monday_is_recorded_not_backfilled(tmp_path):
    http = FakeHTTP()
    rep = xs.run(now_ms=M + 2 * DAY, http=http, out_dir=tmp_path)
    assert rep["missed_formations"] == [M] and not rep["open"]
    rep = xs.run(now_ms=M + 3 * DAY, http=http, out_dir=tmp_path)
    assert rep["missed_formations"] == [M]  # recorded once


def test_report_verdict_lines():
    def ledger(nets_m, nets_c):
        weeks = []
        for i, (a, b) in enumerate(zip(nets_m, nets_c, strict=True)):
            weeks += [{"book": "momentum", "m": i, "status": "settled", "net": a},
                      {"book": "carry", "m": i, "status": "settled", "net": b}]
        return {"weeks": weeks, "formations": [], "missed": []}
    assert xs.report(ledger([0.01] * 25, [0.01] * 25), 0)["verdict"] == "PENDING"
    assert xs.report(ledger([-0.02] * 26, [0.0] * 26), 0)["verdict"] == "RETIRE"
    assert xs.report(ledger([0.01] * 26, [0.01] * 26), 0)["verdict"] == "NOT_FALSIFIED"
    assert xs.report(ledger([0.01] * 52, [0.01] * 52), 0)["verdict"] == "CONTINUE_TO_OWNER"
    assert xs.report(ledger([0.01] * 52, [-0.002] * 52), 0)["verdict"] == "FAIL"
    assert math.isfinite(xs.report(ledger([0.01], [0.01]), 0)["mix_50_50"]["mean"])


def test_formation_keeps_its_own_exchange_info_filters_without_an_extra_call(tmp_path):
    http = FakeHTTP()
    xs.run(now_ms=M + 1800_000, http=http, out_dir=tmp_path)
    info_calls = [u for u, _ in http.calls if u.endswith("exchangeInfo")]
    assert len(info_calls) == 1  # the formation's own call, reused
    snap = json.loads((tmp_path / f"order-filters-{M}.json").read_text())
    assert snap["m"] == M and snap["server_time_ms"] == M + 1800_000
    even = snap["symbols"]["C00USDT"]
    assert even["filters"]["MIN_NOTIONAL"] == {"notional": "5"} and even["missing_filters"] == []
    assert "PRICE_FILTER" not in even["filters"]
    assert snap["symbols"]["C01USDT"]["missing_filters"] == list(xs.ORDER_FILTERS)
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert "filters" not in json.dumps(ledger)  # the ledger itself is unchanged in shape
    before = (tmp_path / f"order-filters-{M}.json").read_bytes()
    xs.run(now_ms=M + 3 * DAY, http=FakeHTTP(), out_dir=tmp_path)
    assert (tmp_path / f"order-filters-{M}.json").read_bytes() == before  # never overwritten


def test_the_filter_companion_keeps_raw_objects_duplicates_and_missing_explicit():
    btc = {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "marginAsset": "USDT",
           "orderTypes": ["MARKET", "LIMIT"],
           "filters": [{"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                       {"filterType": "MIN_NOTIONAL", "notional": "50"},
                       {"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "NONE"},
                       {"filterType": "MIN_NOTIONAL", "notional": "100"}]}
    dup = {"symbol": "ETHUSDT", "filters": []}
    info = {"serverTime": 1, "symbols": [btc, dup, dict(dup, status="TRADING")]}
    snap = xs.order_filters_snapshot(info, ["BTCUSDT", "ETHUSDT", "GONEUSDT"], m=M, run_now_ms=2, received_wall_ms=3)
    b = snap["symbols"]["BTCUSDT"]
    assert b["raw_symbols"] == [btc]  # untouched, filters still a list, every field kept
    assert b["duplicate_filter_types"] == ["MIN_NOTIONAL"] and b["duplicate_symbol_objects"] == 0
    e = snap["symbols"]["ETHUSDT"]
    assert e["duplicate_symbol_objects"] == 1 and len(e["raw_symbols"]) == 2
    assert snap["symbols"]["GONEUSDT"] == {"missing": True, "raw_symbols": []}
    assert snap["schema"] == "kaiba/perps-xs-order-filters/2"
