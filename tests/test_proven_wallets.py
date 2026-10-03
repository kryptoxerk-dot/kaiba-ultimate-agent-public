"""Proven wallets: forward-validated copy performance, frozen per chain.

The tests that matter are the ones a plausible-looking shortcut would break:

* a wallet that won only in sample (luck) is NOT proven -- the out-of-sample half decides;
* a chain whose in-sample winners do not out-earn its losers out of sample publishes an
  EMPTY cohort, however many single wallets pass by chance;
* in-sample pricing never reads a print from after the split (leakage);
* an exit that never prints again is a loss, not a dropped trip;
* the split is the median SWAP, not the midpoint of the clock.
"""

from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, now_ms
from kaiba.learning import proven as P

RH = Chain.ROBINHOOD
DAY = 86_400_000
LAG = 20_000
_seq = itertools.count(1)


def _hex(prefix: str, i: int) -> str:
    return "0x" + (prefix + format(i, "x")).rjust(40, "0")[-40:]


def _swap(conn, *, wallet: str, token: str, side: str, ts: int, price: float | None) -> None:
    n = next(_seq)
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, price_usd, usd_value, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (RH.value, f"0xtx{n}", int(ts), wallet, token, side, str(n),
         None if price is None else str(price), "100", "test"),
    )


def _trip(conn, wallet: str, t0: int, exit_price: float | None, *, hold_ms: int = 600_000,
          sell_price: float = 1.0) -> None:
    """The leader buys at t0 and sells at t0+hold; the market prints after each + lag.

    Entry print 1.0 lands 1 s after their buy + lag, the exit print ``exit_price`` 1 s after
    their sell + lag. ``exit_price=None`` writes no exit print at all: nobody traded after
    the leader's own sell, which prints at ``sell_price``.
    """
    token = _hex("7", next(_seq))
    _swap(conn, wallet=wallet, token=token, side="buy", ts=t0, price=0.9)
    _swap(conn, wallet=_hex("e", next(_seq)), token=token, side="buy", ts=t0 + LAG + 1_000, price=1.0)
    _swap(conn, wallet=wallet, token=token, side="sell", ts=t0 + hold_ms, price=sell_price)
    if exit_price is not None:
        _swap(conn, wallet=_hex("e", next(_seq)), token=token, side="buy",
              ts=t0 + hold_ms + LAG + 1_000, price=exit_price)


def _wallet(conn, name: int, now: int, *, ins: list[float | None], outs: list[float | None]) -> str:
    w = _hex("a", name)
    for i, px in enumerate(ins):  # days 9..6 ago
        _trip(conn, w, now - 9 * DAY + i * 3_600_000, px)
    for i, px in enumerate(outs):  # days 3..1 ago
        _trip(conn, w, now - 3 * DAY + i * 3_600_000, px)
    return w


def _cfg(**kw) -> P.ProvenConfig:
    base = dict(lag_ms=LAG, bootstrap_draws=400, split_samples=5000, discovery_min_swaps=4)
    base.update(kw)
    return P.ProvenConfig(**base)


@pytest.fixture(autouse=True)
def _fresh_cache():
    P.clear_cache()
    yield
    P.clear_cache()


def _world(conn, now: int) -> dict[str, list[str]]:
    """6 persistent winners, 4 lucky (won early, lose late), 8 persistent losers."""
    good = [_wallet(conn, 100 + i, now, ins=[1.4, 1.3, 1.5, 1.2, 1.35, 1.25],
                    outs=[1.3, 1.45, 1.2, 1.4, 1.25, 1.5]) for i in range(6)]
    lucky = [_wallet(conn, 200 + i, now, ins=[1.5, 1.6, 1.4, 1.5, 1.55, 1.45],
                     outs=[0.7, 0.8, 0.6, 0.75, 0.7, 0.65]) for i in range(4)]
    bad = [_wallet(conn, 300 + i, now, ins=[0.7, 0.8, 0.75, 0.6, 0.7, 0.8],
                   outs=[0.75, 0.7, 0.8, 0.65, 0.7, 0.75]) for i in range(8)]
    return {"good": good, "lucky": lucky, "bad": bad}


def test_persistent_winners_are_proven_and_lucky_ones_are_not(tmp_db):
    now = now_ms()
    world = _world(tmp_db, now)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    assert report.error is None
    assert report.persists, report.as_dict()
    proven = {e.wallet for e in report.proven}
    assert proven == set(world["good"])
    assert not proven & set(world["lucky"])  # in-sample winners whose OOS half lost
    assert report.null_passed == 0
    assert len(report.control) == len(report.proven)
    assert not {e.wallet for e in report.control} & proven


def test_a_chain_without_persistence_publishes_an_empty_cohort(tmp_db):
    """Three in-sample winners pass the out-of-sample test on their own, but the chain's
    winners as a group did WORSE out of sample than its losers: the rank does not persist,
    so those three are the luck the gate exists for and nobody is proven."""
    now = now_ms()
    lucky = [_wallet(tmp_db, 400 + i, now, ins=[1.32] * 6, outs=[1.42] * 6) for i in range(3)]
    for i in range(9):
        _wallet(tmp_db, 410 + i, now, ins=[1.32] * 6, outs=[0.52] * 6)
    for i in range(20):
        _wallet(tmp_db, 430 + i, now, ins=[0.72] * 6, outs=[1.0] * 6)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    assert not report.persists
    assert report.passed == 3 and report.null_passed == 0
    assert report.proven == []
    assert any(n.startswith("EMPTY") and "did not out-earn" in n for n in report.notes)

    cohort_id = P.freeze(tmp_db, report)
    assert cohort_id == f"proven:robinhood:{now}"
    row = fetch_one(tmp_db, "SELECT graded_n FROM wallet_cohort_freezes WHERE cohort_id=?", (cohort_id,))
    assert row["graded_n"] == 0
    cohort = P.proven_members(tmp_db, RH, max_age_s=3_600, at_ms=now)
    assert cohort is not None and cohort.members == frozenset()  # EMPTY, not None

    # Positive control: the same world with the gate off publishes exactly those three.
    P.clear_cache()
    ungated = P.build(tmp_db, RH, config=_cfg(require_chain_persistence=False), as_of_ms=now)
    assert {e.wallet for e in ungated.proven} == set(lucky)


def test_in_sample_pricing_never_reads_a_print_from_after_the_split(tmp_db):
    """A trip that sells just before the split, whose next print lands after it, is priced
    from the last print BEFORE the split (the leader's own sell at 1.0) -- never the 50x."""
    now = now_ms()
    world = _world(tmp_db, now)
    split = now - 5 * DAY
    leak = _hex("b", 1)
    token = _hex("7", next(_seq))
    sell_ms = split - 5_000
    _swap(tmp_db, wallet=leak, token=token, side="buy", ts=sell_ms - 600_000, price=0.9)
    _swap(tmp_db, wallet=_hex("e", next(_seq)), token=token, side="buy",
          ts=sell_ms - 600_000 + LAG + 1_000, price=1.0)
    _swap(tmp_db, wallet=leak, token=token, side="sell", ts=sell_ms, price=1.0)
    _swap(tmp_db, wallet=_hex("e", next(_seq)), token=token, side="buy",
          ts=split + 60_000, price=50.0)  # a huge print, but out of sample
    counts: dict[str, int] = {}
    nets = P._price_trips(
        tmp_db, RH, [(token, sell_ms - 600_000, sell_ms)], lag_ms=LAG, fee=Decimal("0.02"),
        until_ms=split, max_wait_ms=600_000, counts=counts,
    )
    assert nets == [Decimal("-0.02")]
    assert counts == {"exit_last_print": 1}
    assert world["good"]  # the world is still there; this test only adds to it


def test_a_leader_who_dumps_into_a_dead_book_is_priced_at_the_dump_not_dropped(tmp_db):
    """Six of this wallet's out-of-sample exits are dumps to 0.1 that nobody trades after.
    Dropping them (copytrade.evaluate_wallet) leaves five clean winners and a 'proven'
    wallet; pricing them where the book was left (-92% each) does not."""
    now = now_ms()
    _world(tmp_db, now)
    ghost = _hex("a", 900)
    for i in range(6):
        _trip(tmp_db, ghost, now - 9 * DAY + i * 3_600_000, 1.5)
    for i in range(5):
        _trip(tmp_db, ghost, now - 3 * DAY + i * 3_600_000, 1.5)
    for i in range(6):
        _trip(tmp_db, ghost, now - 2 * DAY + i * 3_600_000, None, sell_price=0.1)
    priced = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    assert ghost not in {e.wallet for e in priced.proven}
    assert priced.trip_counts_out.get("exit_last_print", 0) >= 6
    P.clear_cache()
    dropped = P.build(tmp_db, RH, config=_cfg(stale_exit="drop"), as_of_ms=now)
    assert ghost in {e.wallet for e in dropped.proven}  # the optimism the default removes


def test_a_quiet_exit_sells_into_the_book_as_it_stood_never_a_later_print(tmp_db):
    now = now_ms()
    token = _hex("7", next(_seq))
    t0 = now - 2 * DAY
    _swap(tmp_db, wallet=_hex("e", 1), token=token, side="buy", ts=t0 + LAG + 1_000, price=1.0)
    _swap(tmp_db, wallet=_hex("e", 2), token=token, side="sell", ts=t0 + 600_000, price=0.6)
    _swap(tmp_db, wallet=_hex("e", 3), token=token, side="buy", ts=t0 + 600_000 + 7_200_000, price=5.0)
    counts: dict[str, int] = {}
    nets = P._price_trips(
        tmp_db, RH, [(token, t0, t0 + 600_000)], lag_ms=LAG, fee=Decimal("0.02"),
        until_ms=now, max_wait_ms=600_000, counts=counts,
    )
    assert nets == [Decimal("0.6") - 1 - Decimal("0.02")]
    assert counts == {"exit_last_print": 1}
    with pytest.raises(ValueError):
        P._price_trips(tmp_db, RH, [], lag_ms=LAG, fee=Decimal(0), until_ms=now,
                       max_wait_ms=1, stale_exit="hope")


def test_trips_that_straddle_the_split_count_on_neither_side(tmp_db):
    """Three ruinous trips bought before the split and sold after it belong to neither half:
    the winner stays proven, with exactly its six trips on each side."""
    now = now_ms()
    world = _world(tmp_db, now)
    split = now - 5 * DAY
    winner = world["good"][0]
    for i in range(3):
        _trip(tmp_db, winner, split - 3_600_000 + i * 60_000, 0.08, hold_ms=2 * 3_600_000)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now, split_ms=split)
    ev = next(e for e in report.proven if e.wallet == winner)
    assert (ev.trips_in, ev.trips_out) == (6, 6)


def test_in_sample_ranks_never_see_a_price_from_after_the_split(tmp_db):
    """Six in-sample trips sell a minute before the split; the next print on each is 50x,
    a minute AFTER it. In sample they are priced at the leader's own sell (-2%), so the
    wallet is an in-sample loser and is not proven however good its later half is."""
    now = now_ms()
    _world(tmp_db, now)
    split = now - 5 * DAY
    leak = _hex("b", 1)
    for i in range(6):
        token = _hex("7", next(_seq))
        sell = split - 60_000 - i * 1_000
        _swap(tmp_db, wallet=leak, token=token, side="buy", ts=sell - 600_000, price=0.9)
        _swap(tmp_db, wallet=_hex("e", next(_seq)), token=token, side="buy",
              ts=sell - 600_000 + LAG + 1_000, price=1.0)
        _swap(tmp_db, wallet=leak, token=token, side="sell", ts=sell, price=1.0)
        _swap(tmp_db, wallet=_hex("e", next(_seq)), token=token, side="buy", ts=split + 60_000, price=50.0)
    for i in range(6):
        _trip(tmp_db, leak, now - 3 * DAY + i * 3_600_000, 1.4)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now, split_ms=split)
    assert report.persists
    assert leak not in {e.wallet for e in report.proven}
    assert report.trip_counts_in.get("exit_last_print", 0) >= 6


def test_the_null_rate_can_empty_a_persistent_chain(tmp_db):
    """The chain persists (in-sample winners beat losers out of sample), but only ONE
    winner passes alone while two of eight in-sample LOSERS pass too: 1 <= the 1.5 the
    losers' pass rate predicts by chance, so the cohort is published empty."""
    now = now_ms()
    _wallet(tmp_db, 600, now, ins=[1.32] * 6, outs=[1.42] * 6)  # passes
    for i in range(5):  # noisy: mean +7.5% out of sample, lower bound far below zero
        _wallet(tmp_db, 610 + i, now, ins=[1.32] * 6, outs=[1.62, 0.57] * 3)
    for i in range(2):  # losers that pass: the chance passes
        _wallet(tmp_db, 620 + i, now, ins=[0.72] * 6, outs=[1.42] * 6)
    for i in range(6):
        _wallet(tmp_db, 630 + i, now, ins=[0.72] * 6, outs=[0.52] * 6)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    assert report.persists, report.as_dict()
    assert (report.passed, report.null_passed, report.null_tested) == (1, 2, 8)
    assert report.expected_false_positives == 1.5
    assert report.chance_p > 0.05
    assert report.proven == []
    assert any("by chance" in n for n in report.notes)


def test_one_pass_against_a_near_zero_null_is_still_chance():
    """MEASURED on the box (robinhood, 2026-10-02): 1 of 73 winners passed, 3 of 282 losers.
    One pass where 0.78 were expected is not a cohort."""
    p0 = (3 + 1) / (282 + 2)
    assert P.binom_tail(1, 73, p0) > 0.6
    assert P.binom_tail(6, 10, 1 / 10) < 0.001
    assert P.binom_tail(0, 5, 0.5) == 1.0 and P.binom_tail(6, 5, 0.5) == 0.0


def test_the_split_is_the_median_swap_not_the_clock_midpoint(tmp_db):
    """90% of the swaps land in the last day of a ten-day window. A clock midpoint would
    sit five days back with almost nothing after... before it; the median swap sits
    inside the busy day."""
    now = now_ms()
    lo, hi = now - 10 * DAY, now
    for i in range(20):
        _swap(tmp_db, wallet=_hex("d", i), token=_hex("7", 1), side="buy", ts=lo + i * DAY // 3, price=1.0)
    for i in range(180):
        _swap(tmp_db, wallet=_hex("d", i), token=_hex("7", 1), side="buy", ts=hi - DAY + i * 400_000, price=1.0)
    split, n = P.estimate_median_swap_ms(tmp_db, RH, lo, hi, samples=1000)
    assert n == 200
    assert split > hi - DAY  # the clock midpoint is hi - 5 days
    assert split > (lo + hi) // 2


def test_a_bot_is_never_a_candidate_and_never_costs_a_full_read(tmp_db):
    now = now_ms()
    _world(tmp_db, now)
    bot = _hex("f", 1)
    for i in range(40):
        _trip(tmp_db, bot, now - 9 * DAY + i * 60_000, 1.5)
        _trip(tmp_db, bot, now - 3 * DAY + i * 60_000, 1.5)
    report = P.build(tmp_db, RH, config=_cfg(max_swaps_per_wallet=100), as_of_ms=now)
    assert bot not in {e.wallet for e in report.proven}
    assert P._episodes(tmp_db, RH, bot, report.lo_ms, report.hi_ms, 100) is None


def test_a_stale_cohort_is_not_served_to_the_lane(tmp_db):
    now = now_ms()
    _world(tmp_db, now)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    P.freeze(tmp_db, report)
    fresh = P.proven_members(tmp_db, RH, max_age_s=3 * 86_400, at_ms=now + 3_600_000)
    assert fresh is not None and fresh.members == frozenset(e.wallet for e in report.proven)
    assert P.proven_members(tmp_db, RH, max_age_s=3 * 86_400, at_ms=now + 4 * DAY) is None
    assert P.proven_members(tmp_db, Chain.SOL, max_age_s=3 * 86_400, at_ms=now) is None  # never frozen


def test_the_newest_freeze_wins_even_when_it_is_empty(tmp_db):
    now = now_ms()
    _world(tmp_db, now)
    P.freeze(tmp_db, P.build(tmp_db, RH, config=_cfg(), as_of_ms=now))
    empty = P.ProvenReport(chain=RH, as_of_ms=now + 1_000, config=_cfg(), split_ms=now - DAY)
    P.freeze(tmp_db, empty)
    cohort = P.proven_members(tmp_db, RH, max_age_s=86_400, at_ms=now + 2_000)
    assert cohort.cohort_id == f"proven:robinhood:{now + 1_000}"
    assert cohort.members == frozenset()


def test_an_errored_build_freezes_nothing(tmp_db):
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now_ms())  # empty tape
    assert report.error
    assert P.freeze(tmp_db, report) is None
    assert fetch_all(tmp_db, "SELECT * FROM wallet_cohort_freezes") == []


def test_the_frozen_cohort_is_readable_by_the_existing_forward_test(tmp_db):
    """Spelled ``graded``/``control`` so validation.control_arm tracks it unchanged."""
    from kaiba.learning import validation

    now = now_ms()
    _world(tmp_db, now)
    cohort_id = P.freeze(tmp_db, P.build(tmp_db, RH, config=_cfg(), as_of_ms=now))
    arms = {r["arm"] for r in fetch_all(tmp_db, "SELECT arm FROM wallet_cohorts WHERE cohort_id=?", (cohort_id,))}
    assert arms == {"graded", "control"}
    report = validation.control_arm(tmp_db, cohort_id=cohort_id)
    assert report.cohort_id == cohort_id


def test_the_job_params_reject_an_unknown_key():
    with pytest.raises(ValueError):
        P.config_from_params({"min_trips_inn": 3})
    cfg = P.config_from_params({"chains": "sol", "min_trips_in": 7, "net_cap": "3"})
    assert cfg.min_trips_in == 7 and cfg.net_cap == Decimal("3")


def test_the_lag_is_per_chain_unless_pinned():
    assert P.ProvenConfig().lag_for(Chain.ROBINHOOD) == 20_000
    assert P.ProvenConfig().lag_for(Chain.SOL) == 120_000
    assert P.ProvenConfig(lag_ms=5_000).lag_for(Chain.SOL) == 5_000


def test_bootstrap_bounds_are_seeded_and_refuse_tiny_samples():
    assert P.bootstrap_lower([0.1, 0.2]) is None
    a = P.bootstrap_lower([0.1, 0.2, 0.3, -0.1, 0.25], seed=7)
    assert a == P.bootstrap_lower([0.1, 0.2, 0.3, -0.1, 0.25], seed=7)
    assert P.cluster_bootstrap_diff([[0.1]] * 2, [[0.0]] * 5) is None


# ---------------------------------------------------------------- the scheduled job


def test_the_job_freezes_one_cohort_per_chain_and_reports_it(tmp_db):
    from kaiba.ops import scheduler as S
    from tests.test_scheduler import ctx_for

    world = _world(tmp_db, now_ms())
    out = S.job_proven_wallets(ctx_for(
        tmp_db, "proven_wallets",
        {"chains": "robinhood,sol", "lag_ms": LAG, "bootstrap_draws": 400, "split_samples": 5000},
        timeout_s=600,
    ))
    rh = out["robinhood"]
    assert rh["persists"] is True
    assert set(rh["proven_wallets"]) == set(world["good"])
    assert rh["cohort_id"].startswith("proven:robinhood:")
    assert out["sol"]["error"] and out["sol"]["cohort_id"] is None  # no sol tape: nothing frozen
    cohort = P.proven_members(tmp_db, RH, max_age_s=3_600)
    assert cohort.members == frozenset(world["good"])


def test_the_job_refuses_a_misspelt_parameter(tmp_db):
    from kaiba.ops import scheduler as S
    from tests.test_scheduler import ctx_for

    with pytest.raises(S.JobFailed):
        S.job_proven_wallets(ctx_for(tmp_db, "proven_wallets", {"min_trip_in": 3}))


def test_a_recent_exit_with_no_print_yet_is_unobserved_not_dead(tmp_db):
    """Under the pessimistic "loss" bound, a trip that sold minutes before the period ended
    has not been watched long enough to call its silence death; it is dropped."""
    now = now_ms()
    token = _hex("7", next(_seq))
    t0 = now - 30 * 60_000
    _swap(tmp_db, wallet=_hex("e", 1), token=token, side="buy", ts=t0 + LAG + 1_000, price=1.0)
    kw = dict(lag_ms=LAG, fee=Decimal("0.02"), max_wait_ms=600_000, stale_exit="loss")
    counts: dict[str, int] = {}
    assert P._price_trips(tmp_db, RH, [(token, t0, t0 + 600_000)], until_ms=now, counts=counts, **kw) == []
    assert counts == {"exit_unobserved": 1}
    counts = {}
    nets = P._price_trips(tmp_db, RH, [(token, t0, t0 + 600_000)], until_ms=now + 3 * 3_600_000,
                          counts=counts, **kw)
    assert nets == [Decimal(-1)] and counts == {"exit_dead": 1}


# ---------------------------------------------------------------- conviction and operators


def test_a_dust_buyer_is_never_proven_however_good_its_copy_record(tmp_db):
    """MEASURED on the box: the first sol cohort was a dust network (median buy $0.04)."""
    now = now_ms()
    world = _world(tmp_db, now)
    dust = world["good"][0]
    tmp_db.execute("UPDATE swaps SET usd_value='0.04' WHERE wallet=? AND side='buy'", (dust,))
    unpriced = world["good"][1]
    tmp_db.execute("UPDATE swaps SET usd_value=NULL WHERE wallet=? AND side='buy'", (unpriced,))
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    proven = {e.wallet for e in report.proven}
    assert dust not in proven and unpriced not in proven  # unknown size is not a pass
    assert set(world["good"][2:]) <= proven
    assert report.below_buy_floor == 2
    P.clear_cache()
    no_floor = P.build(tmp_db, RH, config=_cfg(min_median_buy_usd=Decimal(0)), as_of_ms=now)
    assert {dust, unpriced} <= {e.wallet for e in no_floor.proven}


def test_cotime_clusters_join_wallets_that_keep_buying_together():
    buys = {
        "a": [("t1", 0), ("t2", 10_000), ("t3", 20_000), ("t9", 90_000)],
        "b": [("t1", 500), ("t2", 11_000), ("t3", 21_500)],  # 3 co-buys within 2 s of a
        "c": [("t1", 1_500), ("t2", 10_500)],  # only 2 with a, 2 with b
        "d": [("t9", 95_000)],  # 5 s apart: not co-timed
    }
    out = P.cotime_clusters(buys, within_ms=2_000, min_events=3)
    assert out["a"] == out["b"] == "a"
    assert out["c"] == "c" and out["d"] == "d"


def test_the_frozen_cohort_carries_its_clusters_to_the_lane(tmp_db):
    now = now_ms()
    world = _world(tmp_db, now)
    a, b = world["good"][0], world["good"][1]
    for i in range(4):  # a and b also buy four extra tokens in the same second
        token = _hex("7", next(_seq))
        _swap(tmp_db, wallet=a, token=token, side="buy", ts=now - 2 * DAY + i * 60_000, price=1.0)
        _swap(tmp_db, wallet=b, token=token, side="buy", ts=now - 2 * DAY + i * 60_000 + 500, price=1.0)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    assert report.clusters == len(report.proven) - 1
    P.freeze(tmp_db, report)
    cohort = P.proven_members(tmp_db, RH, max_age_s=3_600, at_ms=now)
    assert cohort.cluster_count([a, b]) == 1
    assert cohort.cluster_count(world["good"]) == len(world["good"]) - 1


# ---------------------------------------------------------------- the draw and the funnel (2026-10-03)


def _thin_wallets(conn, now: int) -> dict[str, str]:
    """Wallets the prescreen must judge exactly: one at the floor, one a trip short of it,
    one whose history is all after the split, one with nothing after it."""
    at_floor = _wallet(conn, 500, now, ins=[1.3] * 5, outs=[1.3] * 6)  # exactly 10 swaps in sample
    short = _wallet(conn, 501, now, ins=[1.3] * 4, outs=[1.3] * 6)  # 8 swaps in sample
    late = _wallet(conn, 502, now, ins=[], outs=[1.3] * 8)  # 0 in sample
    in_only = _wallet(conn, 503, now, ins=[1.4] * 6, outs=[])  # eligible, never tested
    return {"at_floor": at_floor, "short": short, "late": late, "in_only": in_only}


def _outcome(report: P.ProvenReport) -> dict:
    return {
        "eligible": report.eligible,
        "winners": report.in_winners,
        "losers": report.in_losers,
        "tested": report.tested,
        "passed": report.passed,
        "null": (report.null_tested, report.null_passed),
        "proven": sorted(e.wallet for e in report.proven),
        "diff": report.diff_ci_pct,
        "pop": report.population_oos,
    }


def test_the_prescreen_never_changes_who_is_eligible(tmp_db):
    """The prescreen is a NECESSARY condition: it may only skip reads. Same world, same
    split, prescreen on and off -> identical eligibility, statistics and cohort."""
    now = now_ms()
    _world(tmp_db, now)
    thin = _thin_wallets(tmp_db, now)
    split = now - 5 * DAY
    on = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now, split_ms=split)
    P.clear_cache()
    off = P.build(tmp_db, RH, config=_cfg(prescreen=False), as_of_ms=now, split_ms=split)
    assert _outcome(on) == _outcome(off)
    # The boundary: 10 swaps in sample (5 trips) is read and eligible; 8 is skipped.
    eligible_on = on.eligible
    assert eligible_on == 18 + 2  # the world's 18, plus at_floor and in_only
    assert on.prescreen_failed == 2  # short and late; the market-maker wallets have 1-2 swaps
    assert off.prescreen_failed == 0 and off.few_trips_in >= 2
    # at_floor is a persistent winner with exactly min_trips_in trips: a prescreen one swap
    # stricter would drop a proven wallet, which is what this boundary pins.
    assert thin["at_floor"] in {e.wallet for e in on.proven}


def test_the_funnel_accounts_for_every_wallet_it_drew(tmp_db):
    now = now_ms()
    _world(tmp_db, now)
    _thin_wallets(tmp_db, now)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now, split_ms=now - 5 * DAY)
    f = report.funnel()
    assert f["evaluated"] == (
        f["prescreen_failed"] + f["too_active"] + f["few_trips_in"] + f["below_buy_floor"]
        + f["few_fillable_in"] + f["eligible"]
    )
    # seen: every wallet with a swap; active: the 4..max band. The market-maker wallets
    # that print each trip have 1-2 swaps, so they are seen and never drawn.
    assert f["seen"] > f["active"] == f["drawn"] == 18 + 4
    assert f["eligible"] >= f["tested"] >= f["passed"] >= f["proven"] == 6 + 1  # + at_floor
    assert report.as_dict()["seen"] == f["seen"]


def test_the_old_cap_drew_a_sample_and_the_new_one_draws_the_population(tmp_db):
    """With max_candidates under the active population the draw is a sample; the default
    now covers it, so every eligible wallet is scored rather than ~1 in 3 (robinhood) or
    ~1 in 14 (sol) as MEASURED on the box on 2026-10-03."""
    now = now_ms()
    _world(tmp_db, now)
    capped = P.build(tmp_db, RH, config=_cfg(max_candidates=6), as_of_ms=now, split_ms=now - 5 * DAY)
    P.clear_cache()
    full = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now, split_ms=now - 5 * DAY)
    assert capped.funnel()["drawn"] == 6 and capped.eligible <= 6
    assert full.funnel()["drawn"] == full.active == 18 and full.eligible == 18
    assert P.ProvenConfig().max_candidates >= 50_000 and P.ProvenConfig().prescreen is True


def test_an_in_sample_loser_does_not_keep_its_buy_list(tmp_db):
    now = now_ms()
    world = _world(tmp_db, now)
    report = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now)
    # Rebuild the evidence the cohort was chosen from: losers carry no buys, winners do.
    for e in report.proven:
        assert e.buys
    for e in report.control:
        if e.wallet in world["bad"]:
            assert e.buys == []


def test_the_job_result_fits_the_scheduler_cell(tmp_db):
    """MEASURED 2026-10-03: the full two-chain report was 3,700 chars and ops_runs stored
    ``{"truncated": true}`` for every run. The job's result must survive ``_small``."""
    from kaiba.core.db import jdump
    from kaiba.ops import scheduler as S
    from tests.test_scheduler import ctx_for

    _world(tmp_db, now_ms())
    out = S.job_proven_wallets(ctx_for(
        tmp_db, "proven_wallets",
        {"chains": "robinhood,sol", "lag_ms": LAG, "bootstrap_draws": 400, "split_samples": 5000},
        timeout_s=600,
    ))
    assert len(jdump(out)) <= 2000
    assert S._small(out) == out  # stored whole, not as {"truncated": true}
    rh = out["robinhood"]
    assert rh["funnel"]["proven"] == 6 and rh["funnel"]["eligible"] == 18
    assert rh["funnel"]["seen"] >= rh["funnel"]["active"] >= 18


def test_funnel_history_reads_every_freeze_newest_first_and_never_invents_a_zero(tmp_db):
    now = now_ms()
    _world(tmp_db, now)
    # An old-format freeze (written before seen/active existed) and a new one.
    old = P.build(tmp_db, RH, config=_cfg(), as_of_ms=now - 2 * DAY)
    P.freeze(tmp_db, old)
    tmp_db.execute(
        "UPDATE wallet_cohort_freezes SET notes_json = ? WHERE cohort_id = ?",
        (
            '["proven-build {\\"candidates\\": {\\"tape\\": 3000}, \\"evaluated\\": 3000, '
            '\\"eligible\\": 443, \\"tested\\": 89, \\"passed\\": 5}"]',
            P.cohort_id_for(RH, now - 2 * DAY),
        ),
    )
    P.freeze(tmp_db, P.build(tmp_db, RH, config=_cfg(), as_of_ms=now))
    history = P.funnel_history(tmp_db, RH, limit=7)
    assert [h["frozen_ms"] for h in history] == [now, now - 2 * DAY]
    assert history[0]["eligible"] == 18 and history[0]["proven"] == 6 and history[0]["seen"] > 18
    assert history[1]["eligible"] == 443 and history[1]["drawn"] == 3000
    assert history[1]["seen"] is None and history[1]["active"] is None  # unknown, not 0
    assert P.funnel_history(tmp_db, Chain.SOL) == []
