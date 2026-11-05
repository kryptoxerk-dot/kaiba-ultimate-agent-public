import pytest

from kaiba.learning.gmgn_reclaim_replay import Mark, close, evaluate, fill, marks, reclaim, run


def tick(seconds, price):
    return Mark(seconds * 1000, price, 100_000)


def test_only_timely_observations_from_one_pool_are_visible():
    row = {"pool": "a", "venue": "pump", "px": 1, "liq": 10}
    tape = [
        dict(row, obs=0, upd=15_001),
        dict(row, obs=12_000, upd=13_000),
        dict(row, obs=10_000, upd=14_000),
        dict(row, obs=14_000, upd=15_000, pool="b"),
        dict(row, obs=15_000, upd=14_000),
        dict(row, obs=16_000, upd=16_000, px="nan"),
        dict(row, obs=17_000, upd=17_000),
    ]
    assert marks(tape, 0) == [Mark(13_000, 1, 10), Mark(17_000, 1, 10)]


def test_reclaim_fills_after_recovery_and_keeps_gap_distinct_from_cash():
    first = tick(15, 100)
    tape = [first, tick(30, 80), tick(40, 85), tick(50, 150), tick(55, 77)]
    assert reclaim(tape, first, 90_000) == ("filled", tape[-1])
    assert reclaim(tape[:-1], first, 90_000) == ("unknown", None)
    assert reclaim([first, tick(46, 80), tick(50, 100)], first, 90_000) == ("unknown", None)
    assert reclaim([tick(s, 100) for s in (15, 45, 75, 93)], first, 90_000) == ("cash", None)
    assert fill([tick(30, 100)], 0) is not None
    assert fill([tick(31, 100)], 0) is None


def test_stop_requires_two_breaches_and_uses_the_later_fill():
    tape = [tick(s, p) for s, p in ((15, 100), (25, 90), (36, 99), (45, 90),
                                   (55, 89), (60, 50), (67, 80), (90, 90),
                                   (120, 99), (150, 104), (180, 105), (192, 110))]
    buy = tape[0]
    stop = close(tape, buy, 180_000, 30, 0.0275, True)
    hold = close(tape, buy, 180_000, 30, 0.0275, False)
    assert stop == pytest.approx(-0.244, abs=0.001)
    assert hold == pytest.approx(0.040, abs=0.001)
    assert close(tape, buy, 180_000, 30, 0.0375, True) < stop
    assert close(tape[:-1], buy, 180_000, 30, 0.0275, False) is None


def test_cash_is_counted_but_never_fabricates_a_return():
    tape = [tick(s, 100) for s in (15, 40, 65, 90, 115, 140, 165, 180, 192)]
    result = evaluate({"opened": 0, "fx": 150}, tape, 0.3, 375)
    assert result["reclaim_hold"] == ("cash", 0)
    assert result["immediate_hold"][1] < 0
    assert evaluate({"opened": 0, "fx": 0}, tape, 0.3, 375)["reclaim_hold"] == ("unknown", None)
    item = {"opened": 0, "position": "a", "mint": "a", "fx": 150, "tape": []}
    report = run([item, dict(item, opened=1, position="b")])
    assert report["paper_positions"] == 2
    assert report["unique_mints"] == 1
    policy = report["scenarios"]["0.3sol_375bps_per_side"]["policies"]["immediate_hold"]
    assert policy["common_mean"] is None and policy["unknown_at_full_loss"] == -1
