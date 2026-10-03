"""The copy-position manager: manages the owner's GMGN copy trades, touches nothing else.

Owner, 2026-09-30: "dont touch my copy trading but manage them just if they are making
profit like trim them help me and sell help me / cut loss if trend is weak but i already
have auto -50% cut loss". See ``kaiba/execution/copy_manager.py``.

2026-10-01: the owner approved GIVEBACK ONLY for live (trims off, weak cut off). The
policy tests below pin that the switches really switch, that a giveback still fires with
them off, and the guards a LIVE sell needs: a fresh peak per holding cycle, a daily cap
counted durably, no tight retry loop, and no re-send of an ambiguous order.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, jload
from kaiba.core.schemas import Chain
from kaiba.execution import copy_manager as CM

RH = Chain.ROBINHOOD
T0 = 1_790_700_000_000
TOK = "0x96c59a1883aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
#: The block the lead is asked to set on the box, minus ``live``.
GIVEBACK_ONLY = {"tp_rungs": [], "weak_cut_enabled": False}


def row(token=TOK, *, pnl="0.30", price="0.001", liq="50000", value="150", honeypot=False,
        started=1_790_690_000, symbol="COPY", buys=None, cost=None, balance=None):
    r = {"usd_value": value, "unrealized_profit_pnl": pnl, "start_holding_at": started,
         "token": {"token_address": token, "symbol": symbol, "decimals": 18, "price": price,
                   "liquidity": liq, "is_honeypot": honeypot}}
    if buys is not None:
        r["history_total_buys"] = buys
    if cost is not None:
        r["accu_cost"] = cost
    if balance is not None:
        r["balance"] = balance
    return r


def harness(tmp_db, payload, *, live=True, held=10**24, native=Decimal("2700"), cfg=None, on_submit=None):
    """``on_submit(n, token)`` may raise (refused / ambiguous) or return the executor's answer."""
    sent: list[tuple[str, int, int]] = []

    def submit(token, qty, min_out):
        sent.append((token, qty, min_out))
        if on_submit is not None:
            return on_submit(len(sent), token)
        return {"order_id": f"ord:{len(sent)}", "state": "submitted"}

    config = cfg or CM.CopyConfig(live=live)

    def go(now=T0):
        return CM.run(tmp_db, RH, config, fetch_holdings=lambda: payload, wallet_units=lambda t, d: held,
                      native_usd=lambda: native, submit=submit, slippage_bps=2500, now=now)

    return go, sent


def giveback_only(**over):
    return CM.CopyConfig.from_params({"live": True, **GIVEBACK_ONLY, **over})


def put_order(conn, order_id, *, token=TOK, state="unknown", lane="manual"):
    """An ``orders`` row as the executor writes one (``_persist``)."""
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, chain, token, side, lane, mode, input_token, "
        "output_token, amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, RH.value, token, "sell", lane, "live", token,
         "0x0000000000000000000000000000000000000000", "1", "1", 2500, state, "gmgn", T0, T0),
    )


def reconcile_to(conn, order_id, state):
    conn.execute("UPDATE orders SET state=? WHERE order_id=?", (state, order_id))


# --------------------------------------------------------------------------------------
# the rules, as shipped by default
# --------------------------------------------------------------------------------------


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
    assert h.balance == Decimal("342118.215783266336489777") and h.buys is None and h.cost_usd is None


def test_the_job_decides_on_the_live_seams_and_sells_nothing_while_dry(tmp_db, monkeypatch):
    from types import SimpleNamespace

    from kaiba.core.schemas import now_ms
    from kaiba.execution import executor, watchdog
    from kaiba.ops import scheduler as S
    from kaiba.providers import gmgn_cli, native_price

    # 2026-10-03: the job manages the OWNER's named wallet; Kaiba's own is the chain wallet.
    owner, kaiba = "0x72430877378522d1b759ac721561aa9eb9e25c2b", "0xcc4450c80735778e9f5eaa7a4ea47990e57807c2"
    monkeypatch.setattr(watchdog, "exit_wallet_for", lambda chain: kaiba)
    monkeypatch.setattr(watchdog, "wallet_token_units", lambda *a: 10**24)
    monkeypatch.setattr(gmgn_cli, "portfolio_holdings",
                        lambda *a, **k: SimpleNamespace(ok=True, data={"list": [row(pnl="0.30")]}, receipt=None))
    monkeypatch.setattr(gmgn_cli, "account_info", lambda **k: SimpleNamespace(
        ok=True, data={"wallets": [{"chain": "robinhood", "address": kaiba}]}, receipt=None))
    monkeypatch.setattr(native_price, "latest",
                        lambda c, conn=None: SimpleNamespace(ts_ms=now_ms(), price_usd=Decimal("2700")))
    sent = []
    monkeypatch.setattr(executor, "submit", lambda order, conn=None, **kw: sent.append(order))
    ts = now_ms()
    ctx = S.JobContext("copy_manager", tmp_db, {"live": False, "chain": "robinhood", "wallet": owner},
                       S.ScheduleConfig(), ts, ts + 60_000)
    out = S.job_copy_manager(ctx)
    assert out["live"] is False and out["decisions"][0]["kind"] == "trim"
    assert out["sells"] == [] and sent == []
    assert out["wallet"] == owner and out["blocked"] == [CM.BLOCK_NOT_BOUND]


# --------------------------------------------------------------------------------------
# giveback only: the switches really switch
# --------------------------------------------------------------------------------------


def test_an_empty_rung_list_means_no_trims_and_an_absent_key_keeps_the_defaults():
    assert CM.CopyConfig.from_params({"tp_rungs": []}).tp_rungs == ()
    assert CM.CopyConfig.from_params({}).tp_rungs == CM.CopyConfig().tp_rungs != ()
    with pytest.raises(ValueError, match="ambiguous"):
        CM.CopyConfig.from_params({"tp_rungs": None})


def test_with_trims_off_a_plus_30_winner_is_not_trimmed(tmp_db):
    payload = {"list": [row(pnl="0.30")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    rep = go(T0)
    assert rep.decisions == [] and sent == []
    payload["list"] = [row(pnl="1.20")]                      # past every default rung
    assert go(T0 + 120_000).decisions == [] and sent == []


def test_the_weak_cut_can_be_switched_off(tmp_db):
    """Positive control and the switch on the same tape: two tokens, two configs."""
    def tape(token):
        return ({"list": [row(token, pnl="-0.20", price="0.0010")]},
                {"list": [row(token, pnl="-0.30", price="0.0009")]})

    on_first, on_then = tape("0x" + "1" * 40)
    go_on, sent_on = harness(tmp_db, on_first, cfg=CM.CopyConfig(live=True, tp_rungs=()))
    go_on(T0)
    on_first["list"] = on_then["list"]
    assert [d["kind"] for d in go_on(T0 + 660_000).decisions] == ["weak_cut"] and len(sent_on) == 1

    off_first, off_then = tape("0x" + "2" * 40)
    go_off, sent_off = harness(tmp_db, off_first, cfg=giveback_only())
    go_off(T0)
    off_first["list"] = off_then["list"]
    assert go_off(T0 + 660_000).decisions == [] and sent_off == []


def test_the_giveback_still_fires_with_trims_and_weak_cut_off(tmp_db):
    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    assert go(T0).decisions == [] and sent == []              # the peak, observed
    payload["list"] = [row(pnl="0.45")]
    assert go(T0 + 120_000).decisions == []                   # above the +40% floor
    payload["list"] = [row(pnl="0.38")]
    rep = go(T0 + 240_000)
    assert [d["kind"] for d in rep.decisions] == ["giveback"]
    assert len(sent) == 1 and sent[0][1] == 10**24 and sent[0][2] > 0


def test_config_keys_are_read_strictly():
    with pytest.raises(ValueError, match="unknown keys"):
        CM.CopyConfig.from_params({"weak_cut_enable": False})          # the typo that leaves it on
    with pytest.raises(ValueError, match="boolean"):
        CM.CopyConfig.from_params({"live": "maybe"})
    assert CM.CopyConfig.from_params({"live": "false"}).live is False  # bool("false") is True
    assert CM.CopyConfig.from_params({"min_value_usd": 0}).min_value_usd == 0  # was `or default`
    with pytest.raises(ValueError):
        CM.CopyConfig.from_params({"giveback": 0})
    with pytest.raises(ValueError, match="rising"):
        CM.CopyConfig.from_params({"tp_rungs": [[0.5, 0.5], [0.25, 0.33]]})


def test_the_shipped_schedule_block_is_giveback_only():
    """``live`` is deliberately not asserted: the lead flips it in the box's copy, and this
    test runs there too."""
    from kaiba.ops import scheduler as S

    cfg = CM.CopyConfig.from_params(S.load_config().jobs["copy_manager"].params)
    assert cfg.tp_rungs == () and cfg.weak_cut_enabled is False
    assert cfg.giveback == Decimal("0.5") and cfg.giveback_arm_pnl == Decimal("0.25")
    assert cfg.max_sells_per_day == 30 and cfg.max_sells_per_run == 3


# --------------------------------------------------------------------------------------
# holding cycles: a fresh buy never inherits an old peak
# --------------------------------------------------------------------------------------


def test_a_full_giveback_closes_the_cycle_and_is_not_sent_again(tmp_db):
    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(pnl="0.35")]
    go(T0 + 2 * MIN)
    assert len(sent) == 1
    for t in (T0 + 10 * MIN, T0 + HOUR, T0 + 5 * HOUR):       # GMGN still shows it: stale, or dust left
        assert go(t).decisions == []
    assert len(sent) == 1
    st = CM.load_state(tmp_db, RH, TOK)
    assert st.closed and st.closed_kind == "giveback"


def test_a_rebuy_after_the_sell_starts_a_fresh_peak(tmp_db):
    payload = {"list": [row(pnl="0.80", started=100)]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(pnl="0.35", started=100)]
    go(T0 + 2 * MIN)
    assert len(sent) == 1
    payload["list"] = [row(pnl="0.05", started=500)]          # GMGN's copy trader bought it again
    assert go(T0 + 2 * HOUR).decisions == [] and len(sent) == 1, "old +80% peak must not sell a fresh buy"
    st = CM.load_state(tmp_db, RH, TOK)
    assert st.peak_pnl == Decimal("0.05") and not st.closed and st.cycle_reason == "start_holding_at"
    payload["list"] = [row(pnl="0.60", started=500)]          # the new cycle is live, not dead
    go(T0 + 2 * HOUR + 2 * MIN)
    payload["list"] = [row(pnl="0.25", started=500)]
    assert [d["kind"] for d in go(T0 + 2 * HOUR + 4 * MIN).decisions] == ["giveback"]
    assert len(sent) == 2


def test_a_top_up_on_the_same_holding_does_not_inherit_the_old_peak(tmp_db):
    """GMGN buys more of an open copy: the average cost rises, the P&L% drops, and nothing
    else changes -- same ``start_holding_at``. Token A is topped up, B is not; same tape."""
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    payload = {"list": [row(a, pnl="0.80", buys=1), row(b, pnl="0.80", buys=1)]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(a, pnl="0.30", buys=2), row(b, pnl="0.30", buys=1)]
    rep = go(T0 + 2 * MIN)
    assert [s[0] for s in sent] == [b], "B gave back half its peak; A's peak belongs to another cost basis"
    assert [d["token"] for d in rep.decisions] == [b]
    assert CM.load_state(tmp_db, RH, a).cycle_reason == "bought"


def test_a_cost_basis_jump_starts_a_new_cycle_when_gmgn_gives_no_buy_count(tmp_db):
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    payload = {"list": [row(a, pnl="0.80", cost="100"), row(b, pnl="0.80", cost="100")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(a, pnl="0.30", cost="200"), row(b, pnl="0.30", cost="100")]
    go(T0 + 2 * MIN)
    assert [s[0] for s in sent] == [b]
    assert CM.load_state(tmp_db, RH, a).cycle_reason == "cost_basis"


def test_a_balance_rise_starts_a_new_cycle_when_nothing_else_is_given(tmp_db):
    a, b = "0x" + "a" * 40, "0x" + "b" * 40
    payload = {"list": [row(a, pnl="0.80", balance="1000"), row(b, pnl="0.80", balance="1000")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(a, pnl="0.30", balance="2000"), row(b, pnl="0.30", balance="1000")]
    go(T0 + 2 * MIN)
    assert [s[0] for s in sent] == [b]
    assert CM.load_state(tmp_db, RH, a).cycle_reason == "balance"


def test_a_sold_out_token_seen_empty_reopens_on_a_smaller_rebuy(tmp_db):
    """Balance 0 -> >0. The re-buy is SMALLER than the old holding, so only having seen the
    wallet empty (GMGN's dust row) tells it apart from the stale pre-sell sighting."""
    payload = {"list": [row(pnl="0.80", balance="1000")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(pnl="0.35", balance="1000")]
    go(T0 + 2 * MIN)
    assert len(sent) == 1
    payload["list"] = [row(pnl="0.35", balance="0", value="0")]   # the sell landed
    go(T0 + 5 * MIN)
    payload["list"] = [row(pnl="0.02", balance="400")]            # bought again, smaller
    assert go(T0 + HOUR).decisions == []
    st = CM.load_state(tmp_db, RH, TOK)
    assert not st.closed and st.peak_pnl == Decimal("0.02") and st.cycle_reason == "balance"


def test_dry_run_never_closes_a_cycle_so_going_live_acts_on_the_truth(tmp_db):
    payload = {"list": [row(pnl="0.80")]}
    dry, _ = harness(tmp_db, payload, cfg=CM.CopyConfig.from_params({**GIVEBACK_ONLY, "live": False}))
    dry(T0)
    payload["list"] = [row(pnl="0.35")]
    assert [d["action"] for d in dry(T0 + 2 * MIN).decisions] == ["dry_run"]
    assert [d["action"] for d in dry(T0 + 3 * MIN).decisions] == ["dry_run"]  # repeats: nothing advanced
    st = CM.load_state(tmp_db, RH, TOK)
    assert not st.closed and st.last_action_ms == 0 and CM.sells_today(tmp_db, RH, T0) == 0
    live, sent = harness(tmp_db, payload, cfg=giveback_only())
    assert [d["action"] for d in live(T0 + 4 * MIN).decisions] == ["submitted"] and len(sent) == 1


# --------------------------------------------------------------------------------------
# caps, cooldown, refusals, ambiguity
# --------------------------------------------------------------------------------------


def test_the_daily_cap_is_durable_and_resets_on_the_next_utc_day(tmp_db):
    payload = {"list": [row(f"0x{i:040x}", pnl="0.30") for i in range(1, 7)]}  # 0x00..0 is native
    cfg = CM.CopyConfig(live=True, max_sells_per_day=4)
    go, sent = harness(tmp_db, payload, cfg=cfg)
    go(T0)
    assert len(sent) == 3                                     # run cap
    rep = go(T0 + 2 * MIN)
    assert len(sent) == 4 and rep.held == {"daily_cap": 2}
    again, sent_again = harness(tmp_db, payload, cfg=cfg)     # a new process: the count is in kv
    assert again(T0 + 4 * MIN).held == {"daily_cap": 2} and sent_again == []
    assert CM.sells_today(tmp_db, RH, T0) == 4
    again(T0 + DAY)
    assert len(sent_again) == 2 and CM.sells_today(tmp_db, RH, T0 + DAY) == 2


def test_an_unreadable_day_count_fails_closed(tmp_db):
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, 'garbage', 0)", (CM._day_key(RH, T0),))
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]})
    rep = go(T0)
    assert sent == [] and rep.held == {"day_count_unreadable": 1} and rep.errors


def test_a_refused_sell_backs_off_and_does_not_spend_the_day(tmp_db):
    def refuse(n, token):
        raise CM.SellRefused("policy refused", f"ord:r{n}")

    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only(), on_submit=refuse)
    go(T0)
    payload["list"] = [row(pnl="0.35")]
    t1 = T0 + 2 * MIN
    go(t1)
    assert len(sent) == 1 and CM.sells_today(tmp_db, RH, T0) == 0
    go(t1 + 30_000)                                           # inside the 60 s cooldown
    go(t1 + 90_000)                                           # 1 refusal: 120 s now
    assert len(sent) == 1
    go(t1 + 120_000)
    assert len(sent) == 2
    go(t1 + 120_000 + 239_000)                                # 2 refusals: 240 s
    assert len(sent) == 2
    go(t1 + 120_000 + 240_000)
    assert len(sent) == 3 and CM.sells_today(tmp_db, RH, T0) == 0
    assert not CM.load_state(tmp_db, RH, TOK).closed, "nothing was sold; the giveback still stands"


def test_an_ambiguous_sell_waits_for_reconcile_and_is_not_sent_again_once_filled(tmp_db):
    def ambiguous(n, token):
        put_order(tmp_db, "ord:amb", state="unknown")         # what submit_gmgn writes first
        raise CM.SellAmbiguous("gmgn-cli timed out after 45s", "ord:amb")

    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only(), on_submit=ambiguous)
    go(T0)
    payload["list"] = [row(pnl="0.35")]
    go(T0 + 2 * MIN)
    assert len(sent) == 1 and CM.sells_today(tmp_db, RH, T0) == 1, "it may be live: it counts"
    for t in (T0 + HOUR, T0 + 2 * HOUR):
        assert go(t).held == {"in_flight": 1}
    assert len(sent) == 1
    reconcile_to(tmp_db, "ord:amb", "filled")
    go(T0 + 3 * HOUR)
    go(T0 + 4 * HOUR)
    assert len(sent) == 1
    st = CM.load_state(tmp_db, RH, TOK)
    assert st.closed and st.pending_id is None


def test_an_ambiguous_sell_that_reconciles_failed_reopens_and_retries_after_backoff(tmp_db):
    def first_ambiguous(n, token):
        if n == 1:
            put_order(tmp_db, "ord:amb", state="unknown")
            raise CM.SellAmbiguous("gmgn-cli exit 1: network error", "ord:amb")
        return {"order_id": f"ord:{n}", "state": "submitted"}

    payload = {"list": [row(pnl="0.80")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only(), on_submit=first_ambiguous)
    go(T0)
    payload["list"] = [row(pnl="0.35")]
    go(T0 + 2 * MIN)
    go(T0 + HOUR)
    assert len(sent) == 1
    reconcile_to(tmp_db, "ord:amb", "failed")                 # the venue said it created nothing
    rep = go(T0 + 2 * HOUR)
    assert [d["action"] for d in rep.decisions] == ["submitted"] and len(sent) == 2


def test_an_ambiguous_sell_with_no_order_row_stays_blocked(tmp_db):
    def ambiguous_unrecorded(n, token):
        raise CM.SellAmbiguous("gmgn-cli failed unreadably", "ord:ghost")

    payload = {"list": [row(pnl="0.30")]}                     # a trim: it does not close the cycle
    go, sent = harness(tmp_db, payload, cfg=CM.CopyConfig(live=True), on_submit=ambiguous_unrecorded)
    go(T0)
    payload["list"] = [row(pnl="0.60")]                       # rung 2 now due
    rep = go(T0 + HOUR)
    assert len(sent) == 1 and rep.held == {"ambiguous_unrecorded": 1}
    assert any("reconcile by hand" in e for e in rep.errors)


def test_any_unresolved_sell_of_the_token_blocks_another(tmp_db):
    put_order(tmp_db, "ord:operator", state="submitted", lane="manual")
    go, sent = harness(tmp_db, {"list": [row(pnl="0.30")]})
    rep = go(T0)
    assert sent == [] and rep.held == {"in_flight": 1}
    reconcile_to(tmp_db, "ord:operator", "filled")
    go(T0 + 2 * MIN)
    assert len(sent) == 1


def test_the_gmgn_seam_maps_the_executors_outcomes(tmp_db, monkeypatch):
    """Refused -> SellRefused (nothing sent, the day not spent); ambiguous -> the executor's
    UNKNOWN row holds the token, and the next pass does not call the executor again."""
    from kaiba.core.schemas import OrderState
    from kaiba.execution import executor

    calls = []
    owner = "0x72430877378522d1b759ac721561aa9eb9e25c2b"

    def fake_submit(order, conn=None, *, from_wallet=None):
        assert from_wallet == owner, "every copy sell names the owner's wallet"
        calls.append(order)
        if len(calls) == 1:
            raise executor.ExecutionRefused("policy: router not allowed")
        executor._persist(order.model_copy(update={"state": OrderState.UNKNOWN}), conn)
        raise executor.ExecutionAmbiguous("gmgn-cli timed out after 45s")

    monkeypatch.setattr(executor, "submit", fake_submit)
    seam = CM.gmgn_submitter(tmp_db, RH, 2500, wallet=owner)

    def run_at(t, pnl):
        return CM.run(tmp_db, RH, giveback_only(), fetch_holdings=lambda: {"list": [row(pnl=pnl)]},
                      wallet_units=lambda tk, d: 10**24, native_usd=lambda: Decimal("2700"),
                      submit=seam, slippage_bps=2500, now=t)

    run_at(T0, "0.80")
    assert [d["action"] for d in run_at(T0 + 2 * MIN, "0.35").decisions] == ["refused"]
    assert CM.sells_today(tmp_db, RH, T0) == 0
    assert [d["action"] for d in run_at(T0 + 10 * MIN, "0.35").decisions] == ["ambiguous"]
    assert CM.sells_today(tmp_db, RH, T0) == 1
    assert run_at(T0 + 2 * HOUR, "0.30").held == {"in_flight": 1}
    assert len(calls) == 2, "an UNKNOWN order is never re-sent"
    assert all(o.side.value == "sell" and o.lane.value == "manual" and o.min_out > 0 for o in calls)


# --------------------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------------------


def test_every_live_sell_is_recorded_for_scoring(tmp_db):
    from kaiba.core import events as ev

    payload = {"list": [row(pnl="0.80", price="0.0020")]}
    go, sent = harness(tmp_db, payload, cfg=giveback_only())
    go(T0)
    payload["list"] = [row(pnl="0.35", price="0.0014")]
    go(T0 + 2 * MIN)
    rows = [r for r in fetch_all(tmp_db, "SELECT kind, subject, level, payload FROM events WHERE kind='system'")
            if jload(r["payload"], {}).get("action") == CM.SELL_ACTION]
    assert len(rows) == 1 and rows[0]["subject"] == TOK and rows[0]["level"] == "info"
    p = jload(rows[0]["payload"], {})
    assert p["service"] == "copy_manager" and p["outcome"] == "submitted"
    assert (p["token"], p["kind"], p["pnl"], p["price_usd"], p["peak_pnl"]) == (TOK, "giveback", "0.35", "0.0014", "0.80")
    assert p["qty"] == str(10**24) and p["order_id"] == "ord:1" and p["ts_ms"] == T0 + 2 * MIN
    assert int(p["min_out"]) > 0
    assert "position_id" not in p and "intent" not in p, "the watchdog's drain must not read it as an exit request"
    assert any(e.payload.get("action") == CM.SELL_ACTION for e in ev.recent(limit=20, conn=tmp_db))
