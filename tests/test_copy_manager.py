"""The copy-position manager: trims the owner's GMGN copy trades, cuts weak losers, touches nothing else.

Owner, 2026-09-30: "dont touch my copy trading but manage them just if they are making
profit like trim them help me and sell help me / cut loss if trend is weak but i already
have auto -50% cut loss". See ``kaiba/execution/copy_manager.py``.
"""

from __future__ import annotations

from decimal import Decimal

from kaiba.core.schemas import Chain
from kaiba.execution import copy_manager as CM

RH = Chain.ROBINHOOD
T0 = 1_790_700_000_000
TOK = "0x96c59a1883aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def row(token=TOK, *, pnl="0.30", price="0.001", liq="50000", value="150", honeypot=False,
        started=1_790_690_000, symbol="COPY"):
    return {"usd_value": value, "unrealized_profit_pnl": pnl, "start_holding_at": started,
            "token": {"token_address": token, "symbol": symbol, "decimals": 18, "price": price,
                      "liquidity": liq, "is_honeypot": honeypot}}


def harness(tmp_db, payload, *, live=True, held=10**24, native=Decimal("2700"), cfg=None):
    sent: list[tuple[str, int, int]] = []

    def submit(token, qty, min_out):
        sent.append((token, qty, min_out))
        return {"order_id": f"ord:{len(sent)}", "state": "submitted"}

    config = cfg or CM.CopyConfig(live=live)

    def go(now=T0):
        return CM.run(tmp_db, RH, config, fetch_holdings=lambda: payload, wallet_units=lambda t, d: held,
                      native_usd=lambda: native, submit=submit, slippage_bps=2500, now=now)

    return go, sent


def test_a_winner_is_trimmed_a_third_at_plus_25(tmp_db):
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]})
    rep = go()
    assert [d["kind"] for d in rep.decisions] == ["trim"]
    assert len(sent) == 1 and sent[0][1] == int(Decimal(10**24) * Decimal("0.33"))
    assert sent[0][2] > 0, "never min_out=0"


def test_each_rung_fires_once_and_in_order(tmp_db):
    payload = {"list": [row(pnl="0.30")]}
    go, sent = harness(tmp_db, payload)
    go(T0)
    go(T0 + 120_000)                     # same P&L, rung 1 already done: nothing new
    assert len(sent) == 1
    payload["list"] = [row(pnl="1.20")]  # jumps past rungs 2 and 3: one rung per pass
    go(T0 + 240_000)
    go(T0 + 360_000)
    assert len(sent) == 3
    go(T0 + 480_000)
    assert len(sent) == 3, "all three rungs done; the rest rides"


def test_a_winner_that_gives_back_half_its_peak_is_sold(tmp_db):
    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, live=False)          # observe the peak on paper
    go(T0)
    payload["list"] = [row(pnl="0.35")]
    live, sent = harness(tmp_db, payload)
    rep = live(T0 + 120_000)
    assert rep.decisions[0]["kind"] == "giveback" and sent[0][1] == 10**24


def test_a_loser_is_cut_only_while_it_is_still_falling(tmp_db):
    payload = {"list": [row(pnl="-0.20", price="0.0010")]}
    go, sent = harness(tmp_db, payload)
    go(T0)                                                    # price history starts
    payload["list"] = [row(pnl="-0.30", price="0.0009")]      # down 10% over 11 min
    rep = go(T0 + 660_000)
    assert rep.decisions and rep.decisions[0]["kind"] == "weak_cut" and sent[-1][1] == 10**24


def test_a_loser_that_is_recovering_is_held(tmp_db):
    payload = {"list": [row(pnl="-0.35", price="0.0008")]}
    go, sent = harness(tmp_db, payload)
    go(T0)
    payload["list"] = [row(pnl="-0.30", price="0.0009")]      # down on cost, but rising
    rep = go(T0 + 660_000)
    assert rep.decisions == [] and sent == []


def test_no_trend_history_means_no_cut(tmp_db):
    go, sent = harness(tmp_db, {"list": [row(pnl="-0.45", price="0.0005")]})
    assert go().decisions == [] and sent == []


def test_it_never_touches_kaibas_own_positions_honeypots_thin_pools_or_dust(tmp_db):
    tmp_db.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
                   "cost_native) VALUES ('p1', ?, '0xkaiba', 'sm-trenches', 'live', ?, '1', '1', '1')",
                   (RH.value, T0))
    payload = {"list": [row("0xkaiba", pnl="0.9"), row("0xhoney", pnl="0.9", honeypot=True),
                        row("0xthin", pnl="0.9", liq="700"), row("0xdust", pnl="0.9", value="1")]}
    go, sent = harness(tmp_db, payload)
    rep = go()
    assert sent == [] and rep.managed == 0
    assert rep.skipped == {"kaiba_position": 1, "honeypot": 1, "thin_pool": 1, "dust": 1}


def test_dry_run_decides_but_sells_nothing(tmp_db):
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]}, live=False)
    rep = go()
    assert rep.decisions and sent == [] and rep.live is False


def test_it_refuses_rather_than_send_min_out_zero(tmp_db):
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]}, native=None)
    rep = go()
    assert sent == [] and any("min_out" in e for e in rep.errors)


def test_an_empty_wallet_is_not_sold(tmp_db):
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]}, held=0)
    rep = go()
    assert sent == [] and any("holds none" in e for e in rep.errors)


def test_a_new_holding_cycle_starts_with_fresh_rungs(tmp_db):
    payload = {"list": [row(pnl="0.30", started=100)]}
    go, sent = harness(tmp_db, payload)
    go(T0)
    payload["list"] = [row(pnl="0.30", started=200)]         # GMGN sold out and re-bought
    go(T0 + 120_000)
    assert len(sent) == 2


def test_the_sell_cap_per_pass_holds(tmp_db):
    payload = {"list": [row(f"0x{i:040x}", pnl="0.30") for i in range(6)]}
    go, sent = harness(tmp_db, payload)
    go()
    assert len(sent) == 3


def test_the_measured_holdings_shape_parses():
    # Trimmed from the live GMGN response, 2026-09-30.
    payload = {"list": [{"balance": "342118.215783266336489777", "usd_value": "229.89",
                         "unrealized_profit_pnl": "0.8928130707733416", "start_holding_at": 1790701559,
                         "token": {"token_address": "0x53b7ffe8a95cf169e9a9355d333c1b1afbdb1e18",
                                   "symbol": "DOTS", "decimals": 18, "price": "0.00067196226",
                                   "liquidity": "79299.43", "is_honeypot": False}}], "next": ""}
    (h,) = CM.parse_holdings(payload)
    assert h.symbol == "DOTS" and h.pnl == Decimal("0.8928130707733416") and h.liquidity_usd == Decimal("79299.43")


def test_the_job_decides_on_the_live_seams_and_sells_nothing_while_dry(tmp_db, monkeypatch):
    from types import SimpleNamespace

    from kaiba.core.schemas import now_ms
    from kaiba.execution import executor, watchdog
    from kaiba.ops import scheduler as S
    from kaiba.providers import gmgn_cli, native_price

    monkeypatch.setattr(watchdog, "exit_wallet_for", lambda chain: "0x72430877378522d1b759ac721561aa9eb9e25c2b")
    monkeypatch.setattr(watchdog, "wallet_token_units", lambda *a: 10**24)
    monkeypatch.setattr(gmgn_cli, "portfolio_holdings",
                        lambda *a, **k: SimpleNamespace(ok=True, data={"list": [row(pnl="0.30")]}, receipt=None))
    monkeypatch.setattr(native_price, "latest",
                        lambda c, conn=None: SimpleNamespace(ts_ms=now_ms(), price_usd=Decimal("2700")))
    sent = []
    monkeypatch.setattr(executor, "submit", lambda order, conn=None: sent.append(order))
    ts = now_ms()
    ctx = S.JobContext("copy_manager", tmp_db, {"live": False, "chain": "robinhood"},
                       S.ScheduleConfig(), ts, ts + 60_000)
    out = S.job_copy_manager(ctx)
    assert out["live"] is False and out["decisions"][0]["kind"] == "trim"
    assert out["sells"] == [] and sent == []
