"""The sprint scoreboard cannot PASS a strategy on the tokens that survived (2026-11-02).

MEASURED on the box: launch-snipe/sol replay PASS (n=900, +3.75%) while the paper broker's 2,004
trades in the same window averaged -20%. The replay drops every signal whose token has no tape
after entry. The guard lives at the call site (kaiba/ops/scheduler.py); sprint.py and
signal_audit.py are hash-pinned by Codex and are not edited.
"""
from __future__ import annotations

from types import SimpleNamespace

from kaiba.learning import sprint
from kaiba.ops import scheduler

DAY = 86_400_000
START = 1_000 * DAY
NOW = START + 12 * DAY


def case(lane, chain, token, t0, outcome):
    return SimpleNamespace(lane=lane, chain=chain, token=token, t0=t0, outcome=outcome)


def winners(n, lane="launch-snipe", chain="sol", outcome=0.10):
    """n replayed winners spread over both halves of the window."""
    return [case(lane, chain, f"w{i}", START + (i * 11 * DAY) // n + 1, outcome) for i in range(n)]


def considered_for(cases, dropped=()):
    return [(c.lane, c.chain, c.token, c.t0) for c in cases] + list(dropped)


def guarded(cases, considered):
    scores = sprint.scorecard(cases, START, NOW)
    assert any(s.verdict == "PASS" for s in scores)  # positive control: the raw replay passes
    return scheduler.survivor_guard(scores, cases, considered, start=START, now=NOW)


def test_a_pass_resting_on_survivors_becomes_inconclusive():
    cases = winners(60)
    lost = [("launch-snipe", "sol", f"dead{i}", START + (i * 11 * DAY) // 80 + 7) for i in range(80)]
    scores, info = guarded(cases, considered_for(cases, lost))
    cell = next(s for s in scores if (s.lane, s.chain) == ("launch-snipe", "sol"))
    assert cell.verdict == "INCONCLUSIVE" and "60 of 140" in cell.why
    c = info[("launch-snipe", "sol")]
    assert (c["replay_considered"], c["replay_dropped_no_exit_evidence"]) == (140, 80)
    assert c["replay_coverage"] == round(60 / 140, 4) and c["verdict_before_survivor_guard"] == "PASS"
    assert c["worst_case_mean"] < 0


def test_a_pass_that_survives_the_dropped_signals_as_losses_stands():
    cases = winners(200, outcome=0.40)
    lost = [("launch-snipe", "sol", "dead", START + 5 * DAY)]  # one dropped signal cannot sink it
    scores, info = guarded(cases, considered_for(cases, lost))
    cell = next(s for s in scores if (s.lane, s.chain) == ("launch-snipe", "sol"))
    assert cell.verdict == "PASS" and info[("launch-snipe", "sol")]["worst_case_lower"] > 0


def test_full_coverage_leaves_the_verdict_alone():
    cases = winners(60)
    scores, info = guarded(cases, considered_for(cases))
    assert next(s for s in scores if s.lane == "launch-snipe").verdict == "PASS"
    assert info[("launch-snipe", "sol")]["replay_coverage"] == 1.0


def test_an_unrecorded_considered_set_blocks_every_pass():
    cases = winners(60)
    scores, info = guarded(cases, None)
    assert all(s.verdict != "PASS" for s in scores)
    assert info[("launch-snipe", "sol")]["replay_considered"] is None


def test_a_variant_cannot_pass_while_its_base_dropped_signals():
    cases = winners(60)
    scores = sprint.scorecard(cases, START, NOW)
    variant = sprint.Score("launch-snipe[buyers<50%]", "sol", 60, 30, 30, 0.1, 0.1, 0.1, 0.05, "PASS", "x")
    lost = [("launch-snipe", "sol", "dead", START + DAY)]
    out, _ = scheduler.survivor_guard([*scores, variant], cases, considered_for(cases, lost), start=START, now=NOW)
    v = next(s for s in out if s.lane.startswith("launch-snipe["))
    assert v.verdict == "INCONCLUSIVE" and "membership is unknown" in v.why


def test_fail_and_pending_are_untouched_and_out_of_window_signals_ignored():
    bad = [case("sm-trenches", "sol", f"l{i}", START + (i * 11 * DAY) // 60 + 1, -0.2) for i in range(60)]
    few = [case("launch-snipe", "bsc", "x", START + DAY, 0.5)]
    old = [("sm-trenches", "sol", "ancient", START - DAY)]
    scores = sprint.scorecard([*bad, *few], START, NOW)
    out, info = scheduler.survivor_guard(scores, [*bad, *few], considered_for([*bad, *few], old), start=START, now=NOW)
    assert {(s.lane, s.chain): s.verdict for s in out} == {("sm-trenches", "sol"): "FAIL", ("launch-snipe", "bsc"): "PENDING"}
    assert info[("sm-trenches", "sol")]["replay_considered"] == 60  # the pre-window signal is not counted


def test_the_recording_reader_keeps_exactly_run_audits_signal_rows(tmp_path):
    from kaiba.learning import signal_audit as sa

    class Fake(sa.Reader):
        def __init__(self, *a, **k):
            pass

    rows = [("launch-snipe", "sol", "tok", 123, "[]", "{}")]
    base = Fake

    def q(self, sql, args=()):
        return rows if sql.startswith(scheduler._SIGNALS_SQL_HEAD) else [("other",)]

    base.q = q
    fake_sa = SimpleNamespace(Reader=base)
    reader = scheduler._recording_reader(fake_sa, tmp_path / "x.db")
    reader.q("SELECT 1")
    assert reader.considered is None
    reader.q(scheduler._SIGNALS_SQL_HEAD + " WHERE rowid IN (...)", ())
    assert reader.considered == [("launch-snipe", "sol", "tok", 123)]


def test_the_head_matches_signal_audits_own_query_text():
    """If signal_audit's query changes, the recording must not silently miss it."""
    import inspect

    from kaiba.learning import signal_audit as sa

    assert scheduler._SIGNALS_SQL_HEAD in inspect.getsource(sa.run_audit)
