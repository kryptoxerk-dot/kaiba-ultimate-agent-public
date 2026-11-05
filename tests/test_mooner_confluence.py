"""Synthetic-fixture tests for kaiba.learning.mooner_confluence. No database, no network."""

from __future__ import annotations

import math
import random
from decimal import Decimal

import pytest

from kaiba.execution.protection import ProtectionState, evaluate, protection_config
from kaiba.learning import mooner_confluence as mc

#: The box's config/risk.yaml protection block, read 2026-10-04 (moonbag_trail_bps absent
#: there, so the code default of 5000 applies on both sides).
BOX_PROTECTION = {
    "poll_interval_s": 12,
    "stop_loss_bps": 3000,
    "tp_ladder": [[2.0, 50], [5.0, 25], [10.0, 15]],
    "trailing": [[1.2, 2000], [2.0, 3000], [5.0, 2500], [10.0, 2000], [25.0, 1500], [100.0, 1000]],
    "breakeven_after_tp1": True,
    "emergency_loss_bps": 5000,
    "stale_no_volume_exit_s": 3600,
    "moonbag_retain_pct": 20,
}

T0 = 1_790_000_000_000


def _tape(rows, chain="sol", token="TOK", words=()):
    return mc.TokenTape.from_rows(chain, token, rows, words=tuple(words))


def _pump_rows(n=40, step_s=5, start_px=1.0, growth=1.03, source="pumpfun:trades", wallet0=0):
    rows = []
    px = start_px
    for i in range(n):
        side = 1 if i % 3 else -1
        rows.append((T0 + i * step_s * 1000, px, 50.0 + i, side, wallet0 + i % 7, source))
        px *= growth
    return rows


# ------------------------------------------------------------------ ladder parity


def _family(reason: str) -> str:
    return reason.split(":")[0].split("_at_")[0]


@pytest.mark.parametrize("seed", range(60))
def test_float_ladder_matches_protection_evaluate(seed):
    """The simulator's ladder must take the SAME action as the live pure function, step by step."""
    rng = random.Random(seed)
    pcfg = protection_config(BOX_PROTECTION)
    lcfg = mc.LadderConfig.from_protection(BOX_PROTECTION)
    entry = 1.0
    live = ProtectionState(position_id="p", entry_price=Decimal("1"))
    mine = mc.LadderState(entry)
    price = entry
    now = T0
    last_trade = T0
    vol = rng.choice([0.05, 0.12, 0.25])
    drift = rng.choice([-0.01, 0.0, 0.02, 0.05])
    for step in range(400):
        now += 12_000
        if rng.random() < 0.85:
            price = max(1e-6, price * math.exp(rng.gauss(drift, vol)))
            last_trade = now
        elif rng.random() < 0.05:
            now += 3_700_000  # a silence longer than the stale exit
        price = float(Decimal(str(price)).quantize(Decimal("0.000000001")) or Decimal("0.000000001"))
        a = evaluate(live, price_usd=Decimal(str(price)), last_trade_ms=last_trade, now_ms=now, cfg=pcfg)
        kind, frac, why = mc.ladder_step(mine, price, (now - last_trade) / 1000.0, lcfg)
        live_kind = {"hold": "hold", "trim": "trim", "exit_all": "exit"}[a.kind.value]
        assert live_kind == kind, (seed, step, a, kind, why)
        assert _family(a.reason) == _family(why) or (kind == "hold"), (seed, step, a.reason, why)
        if kind != "hold":
            assert float(a.pct) == pytest.approx(frac * 100), (seed, step)
        if live.stop_price is not None or mine.stop is not None:
            assert float(live.stop_price) == pytest.approx(mine.stop, rel=1e-9), (seed, step)
        if kind == "exit":
            break


# ------------------------------------------------------------------ lookahead


def test_features_cannot_see_prints_after_the_decision():
    """FAILS if any feature reads the outcome window (prints after t)."""
    base = _pump_rows(n=60)
    t = T0 + 120_000
    before = mc.features_at(_tape(base), t, wallets=mc.WalletBook(stats={999: (20, 20)}, threshold=0.1),
                            narrative=mc.NarrativeIndex({}, {}))
    # The future: a 100x pump on huge volume, bought by a "good" wallet and GMGN smart money.
    future = list(base) + [
        (t + 1, 100.0, 1e9, 1, 999, "pumpfun:trades"),
        (t + 2_000, 120.0, 1e9, 1, 999, "gmgn:smartmoney"),
        (t + 60_000, 150.0, 1e9, 1, 998, "pumpfun:trades"),
    ]
    after = mc.features_at(_tape(future), t, wallets=mc.WalletBook(stats={999: (20, 20)}, threshold=0.1),
                           narrative=mc.NarrativeIndex({}, {}))
    assert before is not None and after is not None
    assert before == after


def test_source_choice_is_point_in_time():
    """The series source is chosen from counts at t, so a later flood of another feed cannot pick it."""
    rows = [(T0 + i * 1000, 1.0, 10.0, 1, i, "gmgn:kol") for i in range(20)]
    rows += [(T0 + 600_000 + i * 1000, 1.0, 10.0, 1, i, "gmgn:smartmoney") for i in range(500)]
    tape = _tape(rows, chain="bsc")
    assert tape.source_at(T0 + 60_000) == "gmgn:kol"
    assert tape.source_at(T0 + 3_600_000) == "gmgn:smartmoney"


def test_entry_fills_after_the_decision_at_the_pool_price_then():
    rows = _pump_rows(n=100)
    tape = _tape(rows)
    t = T0 + 120_000
    ser = tape.series["pumpfun:trades"]
    res = mc.simulate(ser, t, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9)
    fill_ms = t + mc.LATENCY_S * 1000
    assert res.entry_ms == fill_ms > t
    # entry price is the last print at or before the fill, never one after it
    last = max(i for i, x in enumerate(ser.ts) if x <= fill_ms)
    assert res.max_multiple == pytest.approx(
        max(p for x, p in zip(ser.ts, ser.px) if fill_ms - 5_000 <= x <= fill_ms + mc.HORIZON_S * 1000) / ser.px[last])


def test_a_token_nobody_trades_after_we_buy_pays_both_legs_not_dropped():
    rows = [(T0 + i * 1000, 1.0, 10.0, 1, i, "s") for i in range(5)]
    ser = _tape(rows, chain="bsc").series["s"]
    res = mc.simulate(ser, T0 + 5_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9)
    assert res.status == "priced" and res.reason == "stale_no_volume"
    assert res.net == pytest.approx((1 - mc.LEG_COST) ** 2 - 1)


def test_narrative_heat_excludes_self_and_the_future():
    idx = mc.NarrativeIndex.build([
        (("frog",), T0 - 3_600_000, T0 - 1_800_000),      # hit before t: counts
        (("frog",), T0 - 3_600_000, T0 + 10_000_000),     # hit after t: must not count
        (("frog",), T0 - 100 * 3_600_000, T0 - 99 * 3_600_000),  # outside 24h
        (("frog",), T0, T0 + 1_000),                       # the token itself
    ])
    hits, _ = idx.heat(("frog",), T0 + 5_000, self_t0=T0, self_t3x=T0 + 1_000)
    assert hits == 1


# ------------------------------------------------------------------ volume stratification


def test_volume_flag_is_judged_within_its_print_count_band():
    """Same buy volume: high for a quiet token, NOT high for a busy one."""
    bands = {
        0: mc.BandQuantiles(q50=50.0, q75=100.0, q90=200.0, buyers_q75=2),
        3: mc.BandQuantiles(q50=5_000.0, q75=10_000.0, q90=20_000.0, buyers_q75=40),
    }
    common = dict(t=T0, source="s", price=1.0, buy_usd5=500.0, prev_buy_usd5=100.0, buyers5=3,
                  buys5=3, sells5=1, runup=1.0, retrace=0.0, held_rate=None, retrace_sell_share=None)
    quiet = mc.Features(prints5=4, **common)
    busy = mc.Features(prints5=80, **common)
    assert quiet.band == 0 and busy.band == 3
    assert mc.volume_flags(quiet, bands) & mc.BIT["V_q90"]
    assert not mc.volume_flags(busy, bands) & mc.BIT["V_q50"]
    # A band with no train quantiles yields no volume flag at all, rather than an absolute cut.
    assert mc.volume_flags(mc.Features(prints5=20, **common), bands) == 0


# ------------------------------------------------------------------ outcomes


def test_stale_exit_contradicted_by_another_feed_is_unpriced():
    rows = [(T0 + i * 10_000, 1.0, 10.0, 1, i, "pumpfun:trades") for i in range(30)]
    ser = _tape(rows).series["pumpfun:trades"]
    other = [T0 + 1_800_000]  # the token printed on gmgn while our series was silent
    after_exit = [T0 + 2 * 3_600_000]  # a print AFTER a full hour of silence everywhere
    res = mc.simulate(ser, T0 + 60_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9, other_ts=other)
    assert res.status == "unpriced_gap" and res.net is None
    alone = mc.simulate(ser, T0 + 60_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9,
                        other_ts=after_exit)
    assert alone.status == "priced" and alone.reason == "stale_no_volume"
    assert alone.net == pytest.approx((1 - mc.LEG_COST) ** 2 - 1)


def test_horizon_past_the_data_is_censored_not_zero():
    rows = _pump_rows(n=50)
    ser = _tape(rows).series["pumpfun:trades"]
    res = mc.simulate(ser, T0 + 60_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 3_600_000)
    assert res.status == "censored" and res.net is None


def test_stop_loss_fills_at_the_next_print_and_pays_both_legs():
    rows = [(T0, 1.0, 10.0, 1, 1, "s"), (T0 + 20_000, 1.0, 10.0, 1, 2, "s"),
            (T0 + 30_000, 0.6, 10.0, -1, 3, "s"), (T0 + 40_000, 0.5, 10.0, -1, 4, "s")]
    ser = _tape(rows, chain="bsc").series["s"]
    res = mc.simulate(ser, T0 + 5_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9)
    assert res.status == "priced" and res.reason == "stop_loss"
    # buy lands at T0+15s at 1.0; tick at T0+27s sees 1.0, T0+39s sees 0.6 -> stop;
    # the sell lands at T0+49s, where the pool last printed 0.5
    assert res.gross == pytest.approx(0.5)
    assert res.net == pytest.approx((1 - mc.LEG_COST) ** 2 * 0.5 - 1)


def test_take_profit_ladder_sells_fractions_of_remaining():
    rows = [(T0, 1.0, 10.0, 1, 1, "s"), (T0 + 20_000, 1.0, 10.0, 1, 2, "s"),
            (T0 + 30_000, 2.1, 10.0, 1, 3, "s"), (T0 + 31_000, 2.1, 10.0, 1, 4, "s")]
    rows += [(T0 + 40_000 + i * 12_000, 2.1, 10.0, 1, 5, "s") for i in range(5)]
    rows += [(T0 + 200_000, 1.0, 10.0, -1, 6, "s"), (T0 + 201_000, 1.0, 10.0, -1, 7, "s")]
    ser = _tape(rows, chain="bsc").series["s"]
    res = mc.simulate(ser, T0 + 5_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9)
    assert res.status == "priced"
    # half sold at 2.1 at TP1; the rest is floored at breakeven and exits at 1.0
    assert res.gross == pytest.approx(0.5 * 2.1 + 0.5 * 1.0)


# ------------------------------------------------------------------ wallets


def test_wallet_quality_is_leave_one_token_out_on_train():
    # 9 tokens, 9 hits: the token being scored contributes one of them.
    book_train = mc.WalletBook(stats={7: (9, 9)}, threshold=0.5, own={7: 1})
    assert book_train.is_good(7)          # 8 of 8 without this token
    book_thin = mc.WalletBook(stats={7: (8, 8)}, threshold=0.5, own={7: 1})
    assert not book_thin.is_good(7)       # 7 tokens without this one: below the minimum
    book_test = mc.WalletBook(stats={7: (8, 8)}, threshold=0.5)
    assert book_test.is_good(7)           # a test token is not in the stats at all


def test_wallet_events_only_close_before_the_split():
    rows = _pump_rows(n=40, step_s=600)  # 6.5 hours of prints
    tape = _tape(rows)
    events = mc.wallet_first_buy_events(tape, until_ms=T0 + 7 * 3_600_000)
    assert events and all(isinstance(w, int) and hit in (0, 1) for w, hit in events)
    assert mc.wallet_first_buy_events(tape, until_ms=T0 + 3_600_000) == []


# ------------------------------------------------------------------ stats


def test_cell_below_minimum_is_flagged_unmeasured():
    s = mc.cell_stats([0.1] * 10 + [-0.05] * 5)
    assert not s.measured and "UNMEASURED" in s.line()
    big = mc.cell_stats([0.01, -0.01] * 40)
    assert big.measured and big.p_pos is not None


def test_rule_masks_round_trip():
    m = mc.rule_mask(["V_q75", "W_good1", "D_r2_d35_held", "N_hits3"])
    assert mc.rule_name(m) == "V_q75+W_good1+D_r2_d35_held+N_hits3"
    assert mc.rule_name(0) == "BASELINE_first_active"


def test_a_series_hole_another_feed_printed_through_is_unpriced_not_jumped():
    rows = [(T0 + i * 1000, 1.0, 10.0, 1, i, "pumpfun:trades") for i in range(10)]
    rows += [(T0 + 1_200_000, 13.0, 10.0, 1, 50, "pumpfun:trades")]  # 13x across a 20 min hole
    ser = _tape(rows).series["pumpfun:trades"]
    hole = mc.simulate(ser, T0 + 5_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9,
                       other_ts=[T0 + 600_000])
    assert hole.status == "unpriced_gap"
    quiet = mc.simulate(ser, T0 + 5_000, cfg=mc.LadderConfig(), data_end_ms=T0 + 10 ** 9, other_ts=[])
    assert quiet.status == "priced"


def test_feed_prices_never_enter_a_sol_or_robinhood_series():
    rows = [(T0 + i * 1000, 1.0, 10.0, 1, i, "pumpfun:trades") for i in range(5)]
    rows += [(T0 + 6_000, 23.0, 10.0, 1, 9, "gmgn:smartmoney")]
    tape = _tape(rows)
    assert set(tape.series) == {"pumpfun:trades"}
    # ...but the gmgn print still labels its wallet as smart money
    f = mc.features_at(tape, T0 + 7_000, wallets=mc.WalletBook())
    assert f.smart_buyers == 1


def test_trimmed_mean_drops_the_tails():
    vals = [0.0] * 98 + [100.0, -1.0]
    assert mc.trimmed_mean(vals) == 0.0
