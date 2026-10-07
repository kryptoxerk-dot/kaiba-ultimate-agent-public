"""kaiba.intelligence.lookalike -- synthetic fixtures only; nothing here touches a network.

The three things a wrong answer would hide behind are pinned hardest: the copier exclusion
(a follower must never pass as a leader), the time split (an earlier-period number must not
see one later print), and the lag/fee arithmetic (the entry is the market AFTER the lag, and
both legs pay)."""

from __future__ import annotations

import math
import sqlite3

import pytest

from kaiba.intelligence import lookalike as la
from kaiba.intelligence.lookalike import (
    Config,
    Leg,
    PriceTape,
    Trip,
    buyers_from_token_logs,
    choose_pool,
    cobuy_scan,
    copy_trip,
    episodes,
    grade,
    legs_from_transfers,
    net_return,
    pick_candidates,
    score_in,
    score_out,
    score_wallet,
    summarize,
    summarize_group,
    token_spans,
    v4_index,
    v4_prints,
)

FEE, SLIP = 0.01, 0.015
CFG = Config(boot_draws=400)


def _clock(block: int) -> float:
    return block * 100.0


def _word(v: int) -> str:
    return format(v % (1 << 256), "064x")


# ------------------------------------------------------------------ lag and fee arithmetic


def test_net_return_charges_fee_on_both_legs_and_slippage_against_us():
    got = net_return(1.0, 2.0, fee=FEE, slip=SLIP)
    assert got == pytest.approx(2.0 * 0.99 * 0.99 * (0.985 / 1.015) - 1.0, abs=1e-12)
    # A flat round trip loses exactly the costs -- never zero.
    assert net_return(5.0, 5.0, fee=FEE, slip=0.0) == pytest.approx(0.99 ** 2 - 1.0, abs=1e-12)
    assert net_return(5.0, 5.0, fee=FEE, slip=SLIP) < net_return(5.0, 5.0, fee=FEE, slip=0.0)
    assert net_return(5.0, 5.0, fee=0.0, slip=0.0) == 0.0


def _rally_tape() -> PriceTape:
    # leader buys at t=0 (print 1.0), the copy bots and the market push it up afterwards
    return PriceTape(
        {"T": [(0, 0, 1.0), (500, 1, 1.5), (3_000, 0, 2.0), (10_000, 0, 3.0),
               (60_000, 0, 5.0), (62_000, 0, 4.0), (70_000, 0, 2.5)]},
        continuous=True,
    )


@pytest.mark.parametrize(
    ("lag", "entry", "exit_"),
    [(1_000, 1.5, 5.0), (5_000, 2.0, 4.0), (15_000, 3.0, 2.5)],
)
def test_entry_and_exit_are_the_market_after_the_lag(lag, entry, exit_):
    trip = Trip("w", "T", buy_ms=0, sell_ms=60_000)
    net, how = copy_trip(trip, _rally_tape(), lag, until_ms=10**9, fee=FEE, slip=SLIP)
    assert how == "leader_sell"
    assert net == pytest.approx(net_return(entry, exit_, fee=FEE, slip=SLIP), abs=1e-12)


def test_a_later_copy_is_never_better_on_a_rally_it_chases():
    trip = Trip("w", "T", buy_ms=0, sell_ms=60_000)
    nets = [copy_trip(trip, _rally_tape(), lag, until_ms=10**9, fee=FEE, slip=SLIP)[0] for lag in (1_000, 5_000, 15_000)]
    assert nets[0] > nets[1] > nets[2]
    # and the leader's own fill (lag 0) is out of reach: it would show the 1.0 print
    zero, _ = copy_trip(trip, _rally_tape(), 0, until_ms=10**9, fee=FEE, slip=SLIP)
    assert zero > nets[0]


def test_sparse_tape_takes_the_next_print_within_the_wait_and_never_a_later_one():
    tape = PriceTape({"T": [(0, 0, 1.0), (4_000, 0, 1.2), (200_000, 0, 9.0)]}, continuous=False, max_wait_ms=120_000)
    assert tape.price_at("T", 1_000, until_ms=10**9) == (1.2, 4_000)
    # 5 s after the buy the next print is 195 s away: beyond the wait, so no fill at all
    assert tape.price_at("T", 5_000, until_ms=10**9) is None
    net, how = copy_trip(Trip("w", "T", 0, None), tape, 5_000, until_ms=10**9, fee=FEE, slip=SLIP)
    assert net is None and how == "no_entry_price"


def test_a_dump_into_a_dead_book_is_priced_not_dropped():
    tape = PriceTape({"T": [(0, 0, 1.0), (2_000, 0, 1.0), (59_000, 0, 0.2)]}, continuous=False, max_wait_ms=10_000)
    net, how = copy_trip(Trip("w", "T", 0, 60_000), tape, 1_000, until_ms=10**9, fee=FEE, slip=SLIP)
    assert how == "leader_sell_last_print"
    assert net == pytest.approx(net_return(1.0, 0.2, fee=FEE, slip=SLIP))


def test_time_stop_and_marking():
    tape = PriceTape({"T": [(0, 0, 1.0), (1_000, 0, 1.0), (50_000, 0, 3.0), (90_000, 0, 0.5)]}, continuous=True)
    # leader never sells: the 60 s time stop exits at the state then (3.0), not at the later dump
    net, how = copy_trip(Trip("w", "T", 0, None), tape, 1_000, until_ms=10**9, fee=FEE, slip=SLIP, max_hold_ms=60_000)
    assert how == "time_stop" and net == pytest.approx(net_return(1.0, 3.0, fee=FEE, slip=SLIP))
    # period ends first: marked at the state at the end of the period
    net, how = copy_trip(Trip("w", "T", 0, None), tape, 1_000, until_ms=55_000, fee=FEE, slip=SLIP, max_hold_ms=None)
    assert how == "marked" and net == pytest.approx(net_return(1.0, 3.0, fee=FEE, slip=SLIP))


def test_dark_gap_makes_a_gap_prone_state_unknown_but_not_an_onchain_one():
    prints = {"C": [(0, 0, 1.0, True)], "V": [(0, 0, 1.0, False)]}
    tape = PriceTape(prints, continuous=True, dark=[(10_000, 20_000)])
    assert tape.price_at("C", 15_000, until_ms=10**9) is None
    assert tape.price_at("C", 5_000, until_ms=10**9) == (1.0, 0)
    assert tape.price_at("V", 15_000, until_ms=10**9) == (1.0, 0)


# ------------------------------------------------------------------ the time split


def _split_fixture(after_split_price: float):
    split = 100_000
    legs = []
    prints: dict[str, list] = {}
    for i in range(6):
        tok, t0 = f"E{i}", i * 10_000
        legs += [Leg("w", tok, "buy", t0), Leg("w", tok, "sell", t0 + 3_000)]
        prints[tok] = [(t0, 0, 1.0), (t0 + 2_000, 0, 1.5), (t0 + 6_000, 0, 1.6)]
    # one trip opened before the split and closed after it: in-sample must mark it AT the split
    legs += [Leg("w", "S", "buy", 90_000), Leg("w", "S", "sell", 150_000)]
    prints["S"] = [(90_000, 0, 1.0), (95_000, 0, 1.1), (120_000, 0, after_split_price), (151_000, 0, after_split_price)]
    for i in range(12):
        tok, t0 = f"L{i}", 200_000 + i * 10_000
        legs += [Leg("w", tok, "buy", t0), Leg("w", tok, "sell", t0 + 3_000)]
        prints[tok] = [(t0, 0, 1.0), (t0 + 6_000, 0, after_split_price)]
        # contaminate every earlier token AFTER the split as well
    for i in range(6):
        prints[f"E{i}"].append((130_000 + i, 0, after_split_price))
    return legs, PriceTape(prints, continuous=True), split


def test_earlier_period_numbers_never_see_a_later_print():
    legs_a, tape_a, split = _split_fixture(after_split_price=1.6)
    legs_b, tape_b, _ = _split_fixture(after_split_price=1000.0)
    a = score_in("w", legs_a, tape_a, lo_ms=0, split_ms=split, cfg=CFG)
    b = score_in("w", legs_b, tape_b, lo_ms=0, split_ms=split, cfg=CFG)
    assert a.trips_in == b.trips_in == 7
    for lag in CFG.lags_ms:
        assert a.ins[lag]["mean"] == pytest.approx(b.ins[lag]["mean"], abs=1e-12)
    assert a.selected == b.selected
    # ... while the later period does see them
    a2 = score_out(a, legs_a, tape_a, split_ms=split, hi_ms=10**9, cfg=CFG)
    b2 = score_out(b, legs_b, tape_b, split_ms=split, hi_ms=10**9, cfg=CFG)
    assert a2.trips_out == b2.trips_out == 12
    assert b2.outs[5_000]["mean"] > a2.outs[5_000]["mean"]


def test_a_sparse_tape_never_reaches_past_the_split_for_a_fill():
    # leader sells 10 s before the split; on a sparse tape the next print after that sell is
    # 30 s AFTER the split (well inside max_wait). The earlier period must not take it.
    split = 100_000
    tape = PriceTape({"T": [(0, 0, 1.0), (1_500, 0, 1.0), (50_000, 0, 1.2), (130_000, 0, 50.0)]},
                     continuous=False, max_wait_ms=120_000)
    net, how = copy_trip(Trip("w", "T", 0, 90_000), tape, 1_000, until_ms=split, fee=FEE, slip=SLIP)
    assert how == "leader_sell_last_print"
    assert net == pytest.approx(net_return(1.0, 1.2, fee=FEE, slip=SLIP))
    assert tape.price_at("T", 91_000, until_ms=split) is None
    assert tape.price_at("T", 91_000, until_ms=10**9) == (50.0, 130_000)


def test_a_trip_belongs_to_the_period_of_its_buy_only():
    legs, tape, split = _split_fixture(after_split_price=1.6)
    ws = score_wallet("w", legs, tape, lo_ms=0, split_ms=split, hi_ms=10**9, cfg=CFG)
    assert ws.trips_in == 7 and ws.trips_out == 12
    # the straddling trip is marked at the split in sample, not sold at its later exit
    assert ws.reasons_in.get("marked", 0) == 1


# ------------------------------------------------------------------ co-buy discovery


def _seed_buy(token: str, t: float, seq: int = 50) -> Leg:
    return Leg("seed", token, "buy", t, seq)


def test_cobuy_classifies_before_copier_and_near_against_the_seed():
    sb = _seed_buy("X", 10_000)
    buyers = {"X": [
        Leg("lead", "X", "buy", 9_000),              # 1 s before
        Leg("sameblock_first", "X", "buy", 10_000, 10),  # same block, earlier in it
        Leg("sameblock_after", "X", "buy", 10_000, 90),  # same block, after: a bundled copier
        Leg("bot", "X", "buy", 10_300),              # 0.3 s after: copier
        Leg("near", "X", "buy", 12_000),             # 2 s after
        Leg("late", "X", "buy", 20_000),             # outside the window
        Leg("lead", "X", "buy", 9_500),              # second buy: does not double count
    ]}
    st = cobuy_scan([sb], buyers, CFG)
    assert (st["lead"].before, st["lead"].copier, st["lead"].near) == (1, 0, 0)
    assert st["sameblock_first"].before == 1
    assert st["sameblock_after"].copier == 1
    assert st["bot"].copier == 1
    assert st["near"].near == 1 and "X" in st["near"].tokens_early
    assert "late" not in st
    assert "seed" not in st


def test_copiers_are_excluded_even_when_they_cobuy_every_token():
    seeds, buyers = [], {}
    for i in range(6):
        tok, t = f"T{i}", 100_000 * (i + 1)
        seeds.append(_seed_buy(tok, t))
        buyers[tok] = [
            Leg("follower", tok, "buy", t + 300),     # always 0.3 s behind
            Leg("leader", tok, "buy", t - 2_000),     # always 2 s ahead
        ]
        if i < 2:
            buyers[tok].append(Leg("rare", tok, "buy", t - 1_000))
    st = cobuy_scan(seeds, buyers, CFG)
    cands, funnel = pick_candidates(st, CFG)
    names = [c.wallet for c in cands]
    assert names == ["leader"]
    assert "follower" not in names
    assert funnel == {"cobuyers": 3, "copiers": 1, "few_tokens": 1, "candidates": 1}
    assert st["follower"].copier_share == 1.0 and st["follower"].tokens_early == set()


def test_a_mostly_copying_wallet_with_a_few_early_buys_is_still_a_copier():
    seeds, buyers = [], {}
    for i in range(10):
        tok, t = f"T{i}", 100_000 * (i + 1)
        seeds.append(_seed_buy(tok, t))
        offset = -1_000 if i < 3 else 400   # early on 3 tokens, copying on 7
        buyers[tok] = [Leg("mixed", tok, "buy", t + offset)]
    cands, funnel = pick_candidates(cobuy_scan(seeds, buyers, CFG), CFG)
    assert cands == [] and funnel["copiers"] == 1


def test_cotimed_addresses_collapse_to_one_operator_but_unrelated_snipers_do_not():
    buys = {
        "a1": [(f"T{i}", 1_000.0 * i) for i in range(10)],
        "a2": [(f"T{i}", 1_000.0 * i + 50) for i in range(10)],          # same block as a1 every time
        "s1": [(f"T{i}", 1_000.0 * i + 100) for i in range(3)] + [(f"U{i}", 5.0e6 + i) for i in range(30)],
        "lone": [("T1", 99_999.0)],
    }
    cl = la.cotime_clusters(buys, within_ms=300)
    assert cl["a1"] == cl["a2"] == "a1"
    # s1 shares 3 tokens with a1 (>= min_events) but they are 3 of its 33: not one operator
    assert cl["s1"] == "s1" and cl["lone"] == "lone"
    cands = [la.CoBuy("a2"), la.CoBuy("a1"), la.CoBuy("s1")]
    reps, members = la.representatives(cands, cl, limit=5)
    assert [c.wallet for c in reps] == ["a2", "s1"]
    assert sorted(members["a1"]) == ["a1", "a2"]


def test_seeds_and_excluded_wallets_are_never_candidates():
    seeds = [_seed_buy(f"T{i}", 10_000 * (i + 1)) for i in range(4)]
    buyers = {s.token: [Leg("seed", s.token, "buy", s.t_ms - 1_000), Leg("other_seed", s.token, "buy", s.t_ms - 1_000)]
              for s in seeds}
    st = cobuy_scan(seeds, buyers, CFG, exclude=["OTHER_SEED"])
    assert st == {}


# ------------------------------------------------------------------ EVM parsing


def _tlog(block, txi, li, tx, token, frm, to, amt):
    return [block, txi, li, tx, token, frm, to, "0x" + _word(amt)]


def test_legs_net_inside_each_transaction():
    w, router, pool, other = "0xw", "0xrouter", "0xpool", "0xother"
    logs = [
        _tlog(10, 1, 1, "0xa", "0xtok", pool, w, 100),         # buy
        _tlog(10, 1, 1, "0xa", "0xtok", pool, w, 100),         # same log seen twice (in AND out query)
        _tlog(20, 1, 3, "0xb", "0xtok", w, pool, 100),         # sell
        _tlog(30, 2, 1, "0xc", "0xarb", pool, w, 50),          # pass-through: in and out, same tx
        _tlog(30, 2, 2, "0xc", "0xarb", w, router, 50),
        _tlog(40, 0, 0, "0xd", "0xnew", la.ZERO, w, 10**18),   # mint: never a buy
        _tlog(50, 0, 0, "0xe", "0xq", other, w, 5),            # ignored quote token
    ]
    legs, counts = legs_from_transfers(w, logs, clock=_clock, ignore_tokens=["0xQ"])
    assert [(x.token, x.side, x.t_ms) for x in legs] == [("0xtok", "buy", 1000.0), ("0xtok", "sell", 2000.0)]
    assert counts == {"buy": 1, "sell": 1, "pass_through": 1, "mint": 1}


def test_window_buyers_are_final_recipients_not_router_hops():
    pm, router = la.RH_POOL_MANAGER, "0xrouter"
    logs = [
        [100, 3, 1, "0xa", pm, router, "0x" + _word(500)],       # pool -> router
        [100, 3, 2, "0xa", router, "0xuser", "0x" + _word(500)],  # router -> user
        [101, 0, 1, "0xb", pm, "0xdirect", "0x" + _word(7)],
        [102, 0, 1, "0xc", "0xseller", pm, "0x" + _word(9)],     # a sell is not a buy
        [103, 0, 1, "0xd", la.ZERO, "0xminted", "0x" + _word(9)],
    ]
    out = buyers_from_token_logs("0xtok", logs, clock=_clock, exclude=[pm])
    assert [(x.wallet, x.t_ms) for x in out] == [("0xuser", 10_000.0), ("0xdirect", 10_100.0), ("0xminted", 10_300.0)]


def test_v4_state_price_from_sqrt_price_both_orientations():
    def swap(block, sqrt_p, a0=-5, a1=7):
        return {"blockNumber": hex(block), "transactionIndex": "0x1", "logIndex": "0x2", "transactionHash": f"0x{block}",
                "data": "0x" + _word(a0) + _word(a1) + _word(sqrt_p) + _word(1) + _word(0) + _word(0)}

    logs = [swap(1, 1 << 97), swap(2, 1 << 96), swap(3, 1 << 97, 0, 0)]
    as1 = v4_prints(logs, token_is0=False, clock=_clock)
    as0 = v4_prints(logs, token_is0=True, clock=_clock)
    assert [p for _, _, p in as1] == [0.25, 1.0]          # token is currency1: inverse of token1/token0
    assert [p for _, _, p in as0] == [4.0, 1.0]
    assert as1[0][0] == 100.0


def test_v4_index_and_pool_choice_prefer_the_tokens_own_quote():
    def init(pool, c0, c1, block):
        return {"topics": [la.V4_INIT, pool, "0x" + "0" * 24 + c0[2:], "0x" + "0" * 24 + c1[2:]],
                "data": "0x" + _word(10_000) + _word(200), "blockNumber": hex(block)}

    tok = "0x" + "ab" * 20
    usdc = "0x" + "ff" * 20
    idx = v4_index([init("0xp1", la.ZERO, tok, 5), init("0xp2", tok, usdc, 3)])
    pools = idx[tok]
    assert {(p["pool"], p["token_is0"], p["quote"]) for p in pools} == {("0xp1", False, la.ZERO), ("0xp2", True, usdc)}
    assert choose_pool(pools, {"0xp2": 900, "0xp1": 1}) == "0xp1"          # ETH quote wins over volume
    assert choose_pool(pools, {"0xp2": 900}, quote=usdc) == "0xp2"
    assert choose_pool([], {}) is None


# ------------------------------------------------------------------ grading and baseline


def test_grade_rules():
    def s(n, mean, lo):
        return {"n": n, "mean": mean, "median": mean, "win": 0.5, "ci": (lo, mean + 0.1)}

    assert grade({5_000: s(12, 0.10, 0.01), 1_000: s(12, -0.1, -0.2)}, CFG) == "A"
    assert grade({5_000: s(12, 0.10, -0.01), 1_000: s(12, 0.02, -0.1)}, CFG) == "B"
    assert grade({5_000: s(12, 0.10, -0.01), 1_000: s(12, -0.02, -0.1)}, CFG) is None
    assert grade({5_000: s(9, 0.50, 0.40), 1_000: s(9, 0.5, 0.4)}, CFG) is None


def test_summarize_is_deterministic_and_brackets_the_mean():
    vals = [0.1, -0.05, 0.2, 0.03, -0.1, 0.4, 0.0, 0.05]
    a, b = summarize(vals, seed=7), summarize(vals, seed=7)
    assert a == b
    assert a["n"] == 8 and a["ci"][0] <= a["mean"] <= a["ci"][1]
    assert a["win"] == pytest.approx(5 / 8)
    assert summarize([])["mean"] is None


def _wallet_legs(name: str, n: int, start: float, gap: float = 100_000) -> list[Leg]:
    legs = []
    for i in range(n):
        t = start + i * gap
        legs += [Leg(name, f"{name}{i}", "buy", t), Leg(name, f"{name}{i}", "sell", t + 30_000)]
    return legs


def _tape_for(legs: list[Leg], winner: bool) -> PriceTape:
    prints = {}
    for leg in legs:
        if leg.side == "buy":
            up = 2.0 if winner else 0.6
            prints[leg.token] = [(leg.t_ms, 0, 1.0), (leg.t_ms + 20_000, 0, up)]
    return PriceTape(prints, continuous=True)


def test_end_to_end_a_real_leader_grades_and_a_loser_does_not():
    split = 1_500_000
    good = _wallet_legs("g", 30, 0)
    bad = _wallet_legs("b", 30, 0)
    g = score_wallet("g", good, _tape_for(good, True), lo_ms=0, split_ms=split, hi_ms=10**9, cfg=CFG)
    b = score_wallet("b", bad, _tape_for(bad, False), lo_ms=0, split_ms=split, hi_ms=10**9, cfg=CFG)
    assert g.selected and g.grade == "A" and g.trips_in == 15 and g.trips_out == 15
    assert not b.selected and b.grade is None
    grp = summarize_group([g, b], CFG)
    assert grp["selected"] == 1 and grp["A"] == 1 and grp["ab_rate_of_gradeable"] == 1.0


def test_an_unselected_wallet_is_never_graded_however_its_later_period_went():
    split = 1_500_000
    legs = _wallet_legs("x", 30, 0)
    prints = {}
    for leg in legs:
        if leg.side == "buy":
            up = 0.5 if leg.t_ms < split else 3.0
            prints[leg.token] = [(leg.t_ms, 0, 1.0), (leg.t_ms + 20_000, 0, up)]
    ws = score_wallet("x", legs, PriceTape(prints, continuous=True), lo_ms=0, split_ms=split, hi_ms=10**9, cfg=CFG)
    assert not ws.selected and ws.outs[5_000]["mean"] > 0 and ws.grade is None


def test_token_spans_merge_and_clip():
    legs = [Leg("w", "A", "buy", 100), Leg("w", "A", "sell", 150), Leg("w", "A", "buy", 1_000), Leg("w", "B", "buy", 0)]
    sp = token_spans(legs, pre_ms=10, post_ms=100, lo_ms=0, hi_ms=1_050)
    assert sp == {"A": [(90, 250), (990, 1_050)], "B": [(0, 100)]}


def test_episodes_shape():
    legs = [Leg("w", "A", "buy", 1), Leg("w", "A", "buy", 2), Leg("w", "A", "sell", 3), Leg("w", "A", "sell", 4),
            Leg("w", "A", "buy", 5), Leg("w", "B", "sell", 0)]
    assert episodes(legs) == [Trip("w", "A", 1, 3), Trip("w", "A", 5, None)]


# ------------------------------------------------------------------ adapters (read-only)


def _mini_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE swaps (id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, tx TEXT, slot INTEGER,
          block_index INTEGER, ts_ms INTEGER, wallet TEXT, token TEXT, side TEXT, amount_token TEXT,
          amount_native TEXT, price_usd TEXT, usd_value TEXT, program TEXT, source TEXT);
        CREATE INDEX idx_swaps_wallet ON swaps(chain, wallet, ts_ms);
        CREATE INDEX idx_swaps_token ON swaps(chain, token, ts_ms);
        CREATE INDEX idx_swaps_slot ON swaps(chain, slot);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT);
        """
    )
    rows = [
        ("sol", "tx1", None, None, 1_000, "w", "T", "buy", "gmgn:smartmoney", "5.9"),
        ("sol", "tx1", 10, 1, 1_000, "w", "T", "buy", "pumpfun:trades", "0.0000064"),   # same trade, second feed
        ("sol", "tx2", 11, 1, 2_000, "z", "T", "buy", "pumpfun:trades", "0.0000070"),
        ("sol", "tx3", 12, 1, 3_000, "w", "T", "sell", "pumpfun:trades", "0.0000080"),
    ]
    c.executemany(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, source, price_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    c.execute("INSERT INTO events (ts_ms, kind, payload) VALUES (?, 'system', ?)",
              (int(__import__("time").time() * 1000), '{"event": "heartbeat", "duration_ms": 300}'))
    c.commit()
    c.close()


def test_db_adapters_are_read_only_dedupe_feeds_and_price_from_one_source(tmp_path):
    path = str(tmp_path / "k.db")
    _mini_db(path)
    conn = la.connect_ro(path, min_pause_s=0)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO events (ts_ms, kind, payload) VALUES (1, 'x', '{}')")
    legs = la.db_wallet_legs(conn, "sol", "w", 0, 10_000, max_legs=100)
    assert [(x.side, x.t_ms) for x in legs] == [("buy", 1_000.0), ("sell", 3_000.0)]
    assert la.db_wallet_legs(conn, "sol", "w", 0, 10_000, max_legs=1) is None
    prints, src = la.db_sparse_prints(conn, "sol", "T", [(0, 10_000)])
    assert src == "pumpfun:trades" and [p for _, _, p in prints] == [0.0000064, 0.0000070, 0.0000080]
    assert [x.wallet for x in la.db_token_buyers(conn, "sol", "T", 1_500, 2_500)] == ["z"]
    ok, msg = la.protection_health(conn)
    assert ok, msg


class _FakeRpc(la.Rpc):
    def __init__(self, limit: int) -> None:
        super().__init__("http://x", cache_dir=None, gap_s=0.0)
        self.limit, self.sent = limit, []

    def _send(self, calls, tries=4):
        self.sent.append(len(calls))
        out = []
        for _m, p in calls:
            a, b = int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)
            out.append({"error": {"message": "query returned more than 10000 results"}} if b - a + 1 > self.limit
                       else [{"blockNumber": hex(x)} for x in range(-(-a // 1_000) * 1_000, b + 1, 1_000)])
        return out


def test_rpc_halves_refused_ranges_and_never_sends_more_than_two_per_batch():
    rpc = _FakeRpc(limit=2_500)
    got = rpc.logs({"a": {"fromBlock": hex(0), "toBlock": hex(9_999)}, "b": {"fromBlock": hex(0), "toBlock": hex(999)}})
    assert max(rpc.sent) <= 2
    assert sorted(int(x["blockNumber"], 16) for x in got["a"]) == list(range(0, 10_000, 1_000))
    assert len(got["b"]) == 1


class _SwapRpc(la.Rpc):
    """Answers v4 Swap queries; refuses any query whose answer would exceed ``limit`` logs."""

    def __init__(self, swaps: dict[str, list[int]], limit: int) -> None:
        super().__init__("http://x", cache_dir=None, gap_s=0.0)
        self.swaps, self.limit, self.queries = swaps, limit, []

    def _send(self, calls, tries=4):
        out = []
        for _m, p in calls:
            f = p[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            pools = f["topics"][1]
            self.queries.append((tuple(pools), a, b))
            hits = [(pool, blk) for pool in pools for blk in self.swaps.get(pool, []) if a <= blk <= b]
            if len(hits) > self.limit:
                out.append({"error": {"message": "Log response size exceeded"}})
                continue
            out.append([{"topics": [la.V4_SWAP, pool], "blockNumber": hex(blk), "transactionIndex": "0x0",
                         "logIndex": "0x0", "data": "0x" + _word(-1) + _word(1) + _word(1 << 96) + _word(0) * 3}
                        for pool, blk in hits])
        return out


def test_v4_hot_pool_is_priced_in_windows_around_the_instants_only():
    cold = [100, 5_000, 90_000]
    hot = list(range(0, 200_000, 10))            # 20,000 swaps: no single query may return them
    rpc = _SwapRpc({"0xcold": cold, "0xhot": hot}, limit=10_000)
    src = la.EvmSource(rpc, la.BlockClock(0, 0.0, 100.0))
    rows, hot_pools = src.v4_swaps([("0xcold", 0, 199_999), ("0xhot", 0, 199_999)],
                                   {"0xhot": [50_000, 120_000]}, log=lambda m: None)
    assert hot_pools == {"0xhot"}
    assert [r[0] for r in rows["0xcold"]] == cold
    got = [r[0] for r in rows["0xhot"]]
    assert got and all(49_700 <= b <= 50_160 or 119_700 <= b <= 120_160 for b in got)
    assert all(r[2] == 1.0 for r in rows["0xhot"])           # sqrtP 2^96 -> price 1
    # one refused full-span attempt, then only short windows for the hot pool
    spans = sorted(q[2] - q[1] for q in rpc.queries if q[0] == ("0xhot",))
    assert spans[-1] == 199_999 and all(x <= 460 for x in spans[:-1]) and len(spans) == 3


class _HistRpc(la.Rpc):
    """Transfer logs per wallet; a query whose answer exceeds ``limit`` is refused."""

    def __init__(self, counts: dict[str, int], limit: int = 10_000, dead: str = "") -> None:
        super().__init__("http://x", cache_dir=None, gap_s=0.0)
        self.counts, self.limit, self.dead = counts, limit, dead

    def _send(self, calls, tries=4):
        out = []
        for _m, p in calls:
            topics = p[0]["topics"]
            inbound = len(topics) == 3
            ws = ["0x" + t[-40:] for t in (topics[2] if inbound else topics[1])]
            if self.dead in ws:
                out.append(None)
                continue
            if sum(self.counts.get(w, 0) for w in ws) > self.limit:
                out.append({"error": {"message": "query returned more than 10000 results"}})
                continue
            rows = []
            for w in ws:
                for i in range(self.counts.get(w, 0)):
                    frm, to = ("0x" + "11" * 20, w) if inbound else (w, "0x" + "11" * 20)
                    rows.append({"blockNumber": hex(i + 1), "transactionIndex": "0x0", "logIndex": hex(i),
                                 "transactionHash": f"0x{w[2:8]}{i}{int(inbound)}", "address": "0x" + "22" * 20,
                                 "topics": [la.TRANSFER, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
                                 "data": "0x" + _word(5)})
            out.append(rows)
        return out


def test_a_bot_is_flagged_too_active_without_being_paged_in_and_a_dead_query_is_not_a_bot():
    a, b, bot, dead = ("0x" + c * 40 for c in "abcd")
    rpc = _HistRpc({a: 3, b: 4, bot: 50_000, dead: 1}, dead=dead)
    src = la.EvmSource(rpc, la.BlockClock(0, 0.0, 100.0))
    logs, busy, missing = src.histories([a, b, bot, dead], 0, 1_000_000)
    assert busy == {bot} and missing == {dead}
    assert len(logs[a]) == 6 and len(logs[b]) == 8 and logs[bot] == [] and logs[dead] == []


def test_rpc_refuses_any_method_that_is_not_a_read():
    rpc = _FakeRpc(limit=10)
    with pytest.raises(ValueError):
        rpc.many([("eth_sendRawTransaction", ["0xdead"])])


def test_block_clock_round_trips_exactly():
    clk = la.BlockClock(79_000_000, 1_791_076_956_000.0, 101.5)
    for b in (68_664_626, 72_519_488, 79_547_900):
        assert clk.block(clk(b)) == b
    assert math.isclose(clk(79_000_010) - clk(79_000_000), 1_015.0)


# ------------------------------------------------------------------ sol end to end


def _sol_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE swaps (id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, tx TEXT, slot INTEGER,
          block_index INTEGER, ts_ms INTEGER, wallet TEXT, token TEXT, side TEXT, amount_token TEXT,
          amount_native TEXT, price_usd TEXT, usd_value TEXT, program TEXT, source TEXT);
        CREATE INDEX idx_swaps_wallet ON swaps(chain, wallet, ts_ms);
        CREATE INDEX idx_swaps_token ON swaps(chain, token, ts_ms);
        CREATE INDEX idx_swaps_slot ON swaps(chain, slot);
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts_ms INTEGER, kind TEXT, payload TEXT);
        CREATE TABLE wallet_cohort_freezes (cohort_id TEXT, frozen_ms INTEGER, chain TEXT);
        CREATE TABLE wallet_cohorts (cohort_id TEXT, arm TEXT, chain TEXT, address TEXT);
        CREATE TABLE wallet_scores (chain TEXT, address TEXT, score REAL, grade TEXT);
        CREATE INDEX idx_wallet_scores_grade ON wallet_scores(grade, score DESC);
        """
    )
    rows = []
    n = [0]

    def add(t, wallet, token, side, price, source="pumpfun:trades", tx=None):
        n[0] += 1
        rows.append(("sol", tx or f"tx{n[0]}", int(t), wallet, token, side, str(price), source))

    hour = 3_600_000
    win = [(-2_000, "early", "buy", 0.95), (300, "follower", "buy", 1.05), (1_500, "o1", "buy", 1.3),
           (4_000, "o2", "buy", 1.4), (6_000, "o3", "buy", 1.5), (30_000, "o4", "buy", 1.8),
           (60_000, "leader", "sell", 2.4), (60_500, "early", "sell", 2.35), (61_000, "follower", "sell", 2.3),
           (66_000, "o5", "sell", 2.2), (75_000, "o6", "sell", 2.0), (80_000, "o7", "sell", 1.9)]
    lose = [(1_500, "o1", "buy", 0.9), (6_000, "o2", "sell", 0.8), (30_000, "o3", "sell", 0.7),
            (60_000, "loser", "sell", 0.6), (66_000, "o4", "sell", 0.55), (75_000, "o5", "sell", 0.5)]
    for i in range(30):
        t = hour + i * 7.5 * hour
        tok, bad = f"W{i}", f"L{i}"
        # the leader's buy arrives from two feeds on different price scales; one source must win
        add(t, "leader", tok, "buy", 5.9, "gmgn:smartmoney", tx=f"lb{i}")
        add(t, "leader", tok, "buy", 1.0, tx=f"lb{i}")
        for dt, w, side, px in win:
            add(t + dt, w, tok, side, px)
        add(t + 60_000 - 1, "leader", tok, "sell", 9.9, "gmgn:smartmoney")
        add(t, "loser", bad, "buy", 5.9, "gmgn:smartmoney", tx=f"xb{i}")
        add(t, "loser", bad, "buy", 1.0, tx=f"xb{i}")
        for dt, w, side, px in lose:
            add(t + dt, w, bad, side, px)
        for j in range(3):  # baseline wallets trading flat tokens
            rt = t + 3 * hour + j * 1_000
            add(rt, f"rnd{j}", f"R{i}_{j}", "buy", 1.0)
            add(rt + 5_000, f"x{j}", f"R{i}_{j}", "buy", 1.0)
            add(rt + 40_000, f"rnd{j}", f"R{i}_{j}", "sell", 0.99)
            add(rt + 70_000, f"x{j}", f"R{i}_{j}", "sell", 0.99)
    c.executemany("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, price_usd, source) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.execute("INSERT INTO events (ts_ms, kind, payload) VALUES (?, 'system', ?)",
              (int(__import__("time").time() * 1000), '{"event": "heartbeat", "duration_ms": 300}'))
    c.commit()
    c.close()


def test_sol_end_to_end_finds_the_early_lookalike_and_drops_the_follower(tmp_path):
    path = str(tmp_path / "sol.db")
    _sol_db(path)
    conn = la.connect_ro(path, min_pause_s=0)
    rep = la.run_sol(conn, [], CFG, lo_ms=0, hi_ms=10 * la.DAY_MS, n_baseline=10, log=lambda m: None)
    seeds = {s["wallet"]: s for s in rep["seeds"]}
    assert list(seeds) == ["leader"]                      # chosen on the EARLIER period only
    assert seeds["leader"]["grade"] == "A"
    graded = {g["wallet"]: g for g in rep["graded"]}
    assert "early" in graded and graded["early"]["grade"] == "A"
    assert "follower" not in {c["wallet"] for c in rep["candidates"]}
    assert rep["funnel"]["copiers"] >= 1
    loser = next(c for c in rep["candidates"] if c["wallet"] == "loser")
    assert loser["selected"] is False and loser["grade"] is None
    # the leader is priced on the pumpfun scale, never the 5.9 feed print
    g5 = seeds["leader"]["out_of_sample"]["5s"]
    assert 20 < g5["mean_pct"] < 60
    assert rep["baseline_group"]["A"] == 0


def test_thin_prints_keeps_exactly_what_a_sparse_copy_reads():
    rows = [(float(t), 0, float(t)) for t in range(0, 100_000, 1_000)]
    instants = [10_500.0, 50_000.0]
    kept = sorted(la.thin_prints(rows, instants, (0, 1_000), 2_000))
    assert [r[0] for r in kept] == [10_000.0, 11_000.0, 12_000.0, 50_000.0, 51_000.0]
    full = PriceTape({"T": rows}, continuous=False, max_wait_ms=2_000)
    thin = PriceTape({"T": kept}, continuous=False, max_wait_ms=2_000)
    for x in instants:
        for off in (0, 1_000):
            assert full.price_at("T", x + off, until_ms=10**9) == thin.price_at("T", x + off, until_ms=10**9)
            assert full.last_at("T", x + off, since_ms=0) == thin.last_at("T", x + off, since_ms=0)


def test_trip_reads_cover_only_what_a_copy_reads():
    cfg = Config(max_hold_ms=100_000)
    trips = [Trip("w", "A", 1_000_000, 1_030_000),      # closed by the leader
             Trip("w", "B", 2_000_000, None),            # time stop
             Trip("w", "C", 2_950_000, None)]            # still open at the period end
    spans, inst = la.trip_reads(trips, cfg, lo_ms=0, hi_ms=3_000_000, pre_ms=10_000)
    pad = max(cfg.lags_ms) + 60_000
    assert spans["A"] == [(990_000, 1_030_000 + pad)]
    assert spans["B"] == [(1_990_000, 2_100_000 + pad)]
    assert spans["C"] == [(2_940_000, 3_000_000)]
    assert inst["A"] == [1_000_000, 1_030_000] and inst["B"] == [2_000_000, 2_100_000] and inst["C"] == [2_950_000, 3_000_000]


def test_candidate_pools_skip_base_assets_and_cap_the_rest():
    pl = [{"pool": f"0x{i}", "quote": la.ZERO if i % 2 else "0xq", "block": 100 - i} for i in range(6)]
    got = la.candidate_pools(pl, la.ZERO, keep=2)
    assert [x["pool"] for x in got] == ["0x5", "0x3"]
    assert la.candidate_pools(pl * 5, la.ZERO) == []


def test_exit_rules_bound_a_sparse_tape_from_both_sides():
    # three trips: one observed exit (+), one whose exit only has a stale last print (+, but
    # unobserved), one with no price at all (never bought)
    tape = PriceTape({"A": [(0, 0, 1.0), (1_500, 0, 1.0), (61_500, 0, 1.5)],
                      "B": [(0, 0, 1.0), (1_500, 0, 1.0), (20_000, 0, 3.0)],
                      "C": [(0, 0, 1.0)]}, continuous=False, max_wait_ms=10_000)
    trips = [Trip("w", "A", 0, 60_000), Trip("w", "B", 0, 60_000), Trip("w", "C", 0, 60_000)]
    sets = la._priced_all(trips, tape, 1_000, 10**9, CFG, None)
    a = net_return(1.0, 1.5, fee=FEE, slip=SLIP)
    b = net_return(1.0, 3.0, fee=FEE, slip=SLIP)
    assert sets["state"] == pytest.approx([a, b])
    assert sets["observed"] == pytest.approx([a])
    assert sets["loss"] == pytest.approx([a, -1.0])


def test_one_unsellable_print_cannot_certify_a_wallet():
    # 12 flat trips and one whose exit print is astronomically high: capped at +500%
    prints = {f"T{i}": [(i * 100_000, 0, 1.0), (i * 100_000 + 1_000, 0, 1.0)] for i in range(12)}
    prints["T5"].append((5 * 100_000 + 20_000, 0, 1e40))
    legs = [leg for i in range(12) for leg in (Leg("w", f"T{i}", "buy", i * 100_000), Leg("w", f"T{i}", "sell", i * 100_000 + 40_000))]
    ws = score_wallet("w", legs, PriceTape(prints, continuous=True), lo_ms=-1, split_ms=-0.5, hi_ms=10**9, cfg=CFG)
    assert max(ws.nets_out[5_000]) == CFG.net_cap
    assert ws.reasons_out["capped"] == 1
    assert ws.grade is None or ws.outs[5_000]["ci"][0] <= 0


def test_v4_tick_bound_prints_are_dropped_only_in_the_absurdly_high_direction():
    assert la.at_bound_high(1e-38, token_is0=False)       # token1 per token0 ~ 0 -> token (currency1) astronomically dear
    assert not la.at_bound_high(1e38, token_is0=False)    # drained pool: token ~ 0, kept as the honest -100%
    assert la.at_bound_high(1e38, token_is0=True)
    assert not la.at_bound_high(2.7e7, token_is0=False)   # a live Pons pool


# ------------------------------------------------------------------ box DB rules (2026-10-04)


def _wal_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [(i,) for i in range(5_000)])
    c.commit()
    c.close()


def test_db_leaves_no_reader_behind_so_the_wal_can_always_be_truncated(tmp_path):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    writer = sqlite3.connect(path)
    writer.execute("INSERT INTO t (v) VALUES (1)")
    writer.commit()
    db = la.Db(path, min_pause_s=0)
    assert len(db.execute("SELECT v FROM t WHERE id <= 10")) == 10
    rows = db.execute("SELECT id FROM t")          # a "for row in conn.execute" loop can no longer pin it
    for _ in rows:
        writer.execute("INSERT INTO t (v) VALUES (2)")
        writer.commit()
        break
    busy, _, _ = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert busy == 0
    writer.close()


def test_a_long_statement_is_interrupted_not_left_running(tmp_path):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    db = la.Db(path, max_query_s=0.05, min_pause_s=0)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 50000000) SELECT count(*) FROM c")
    assert db.interrupted == 1


def test_scan_ids_chunks_and_halves_an_interrupted_chunk(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    db = la.Db(path, min_pause_s=0)
    real = la.Db.execute
    spans = []

    def fake(self, sql, params=(), *, cache=False):
        spans.append(params[1] - params[0])
        if params[1] - params[0] > 1_500:
            raise sqlite3.OperationalError("interrupted")
        return real(self, sql, params, cache=cache)

    monkeypatch.setattr(la.Db, "execute", fake)
    got = list(db.scan_ids("SELECT id FROM t WHERE id > ? AND id <= ?", (), 0, 5_000, step=4_000, cache=False))
    assert [r[0] for r in got] == list(range(1, 5_001))
    assert spans[0] == 4_000 and max(s for s in spans[1:]) <= 2_000


def test_wal_gate_waits_while_the_wal_is_over_the_limit(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    wal = path + "-wal"
    with open(wal, "wb") as fh:
        fh.write(b"x" * 4_096)
    slept = []

    def fake_sleep(s):
        slept.append(s)
        with open(wal, "wb"):
            pass                                     # the checkpoint the gate was waiting for

    monkeypatch.setattr(la.time, "sleep", fake_sleep)
    db = la.Db(path, wal_limit_bytes=1_000, wal_soft_bytes=500, wal_wait_s=60, min_pause_s=0, log=lambda m: None)
    assert len(db.execute("SELECT 1")) == 1
    assert slept[0] == 60 and db.wal_waits == 1


def test_db_cache_answers_without_opening_the_database(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    db = la.Db(path, cache_dir=str(tmp_path / "c"), min_pause_s=0)
    first = db.execute("SELECT v FROM t WHERE id <= ?", (3,), cache=True)

    def boom(*a, **k):
        raise AssertionError("opened the database for a cached answer")

    monkeypatch.setattr(la.sqlite3, "connect", boom)
    assert db.execute("SELECT v FROM t WHERE id <= ?", (3,), cache=True) == first
    assert db.cached == 1


def test_split_rows_halves_a_time_range_that_runs_too_long(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    db = la.Db(path, min_pause_s=0)
    real = la.Db.execute

    def fake(self, sql, params=(), *, cache=False):
        if params[1] - params[0] > 1_000:
            raise sqlite3.OperationalError("interrupted")
        return real(self, sql, params, cache=cache)

    monkeypatch.setattr(la.Db, "execute", fake)
    got = la.split_rows(db, "SELECT id FROM t WHERE id BETWEEN ? AND ?", lambda a, b: (a, b), 1, 5_000, min_span=10)
    assert sorted(r[0] for r in got) == list(range(1, 5_001))


def test_db_rests_after_every_statement_so_the_checkpointer_gets_a_reader_free_instant(tmp_path, monkeypatch):
    path = str(tmp_path / "w.db")
    _wal_db(path)
    slept = []
    monkeypatch.setattr(la.time, "sleep", lambda s: slept.append(s))
    db = la.Db(path, min_pause_s=0.05, duty=0.5)
    for _ in range(3):
        db.execute("SELECT count(*) FROM t")
    assert len(slept) == 3 and all(s >= 0.05 for s in slept)
