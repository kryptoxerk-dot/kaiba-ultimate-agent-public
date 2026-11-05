"""The 7-day paper sprint's pass line (owner 2026-10-08). Declared before any sprint data."""

from __future__ import annotations

from types import SimpleNamespace

from kaiba.learning import sprint

DAY = 86_400_000
START = 1_800_000_000_000


def case(lane, chain, t0, outcome):
    return SimpleNamespace(lane=lane, chain=chain, t0=t0, outcome=outcome)


def series(lane, chain, n, old_ret, new_ret, now):
    mid = START + (now - START) // 2
    return ([case(lane, chain, START + 1000 + i, old_ret) for i in range(n)]
            + [case(lane, chain, mid + 1000 + i, new_ret) for i in range(n)])


def test_positive_on_both_halves_passes_and_one_bad_half_fails():
    now = START + 7 * DAY
    good = series("sm-trenches", "sol", 25, 0.05, 0.04, now)
    bad = series("sm-trenches", "bsc", 25, 0.10, -0.02, now)
    scores = {(s.lane, s.chain): s for s in sprint.scorecard(good + bad, START, now)}
    assert scores[("sm-trenches", "sol")].verdict == "PASS"
    assert scores[("sm-trenches", "bsc")].verdict == "FAIL" and "newer half" in scores[("sm-trenches", "bsc")].why


def test_too_few_trades_is_pending_never_pass():
    now = START + 3 * DAY
    s = sprint.scorecard(series("trusted-copy", "sol", 10, 0.3, 0.3, now), START, now)[0]
    assert s.verdict == "PENDING"


def test_cases_before_the_sprint_and_other_lanes_are_ignored():
    now = START + 7 * DAY
    cases = series("sm-trenches", "sol", 25, 0.05, 0.05, now) + [case("sm-trenches", "sol", START - 1, -1.0)] * 50 \
        + [case("migration-fade", "sol", START + 5, 2.0)] * 50
    [s] = sprint.scorecard(cases, START, now)
    assert s.n == 50 and s.verdict == "PASS"


def test_robinhood_and_bsc_pay_their_measured_extra_cost():
    assert sprint.net(0.0, "sol") == 0.0
    assert sprint.net(0.0, "robinhood") < sprint.net(0.0, "bsc") < 0.0
    assert sprint.net(10.0, "sol") == sprint.OUTCOME_CAP


def test_the_scorecard_text_is_rolling_and_never_repeats_a_day_7_verdict():
    """2026-10-13: the fixed 7-day sprint never ended in code ("DAY 7 RESULT" repeated daily).
    The scorecard is a rolling replay now; the owner's verdict comes from paper_report."""
    now = START + 9 * DAY + 1
    text = sprint.render(sprint.scorecard(series("sm-trenches", "sol", 25, -0.05, -0.05, now), START, now), START, now)
    assert f"rolling {sprint.ROLLING_DAYS} d" in text and "tweet-launch" in text
    assert "DAY 7" not in text and "Passing now: none yet" in text


def test_a_pre_declared_variant_is_scored_on_its_own_filtered_trades():
    now = START + 7 * DAY
    mid = START + (now - START) // 2
    cases = []
    for half_start in (START, mid):
        for i in range(25):   # in band: buy_share 0.3 wins; out of band: 0.9 loses
            cases.append(SimpleNamespace(lane="launch-snipe", chain="sol", t0=half_start + 10 + i,
                                         outcome=0.08, features={"buy_share_30m": 0.3}))
            cases.append(SimpleNamespace(lane="launch-snipe", chain="sol", t0=half_start + 50 + i,
                                         outcome=-0.20, features={"buy_share_30m": 0.9}))
    scores = {(s.lane, s.chain): s for s in sprint.scorecard(cases, START, now)}
    assert scores[("launch-snipe", "sol")].verdict == "FAIL"
    v = scores[("launch-snipe[buyers<50%]", "sol")]
    assert v.n == 50 and v.verdict == "PASS"
    # an unmeasured feature is never in a band
    assert sprint._in_band(None, None, 0.5) is False
