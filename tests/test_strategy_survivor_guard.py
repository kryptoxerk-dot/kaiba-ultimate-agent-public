"""The strategy factory cannot call a candidate ready on the tokens that survived (2026-11-03).

MEASURED on the box: 29 forward-PASS launch-snipe/sol candidates (+24..32% a trade, shown to the
owner as ready) all rest on buyers_30m<1. On EVERY launch-snipe/sol paper position since 10-26 that
rule averaged -18.8% (n=829): the replay scored 39 of them (+30.1%) and dropped 790 whose token had
no tape after entry (-21.2% on paper). The guard lives at the call site (kaiba/ops/scheduler.py)
and in the owner's report (kaiba/learning/paper_report.py); strategy_search.py, signal_audit.py
and sprint.py are hash-pinned by Codex and are not edited.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from kaiba.learning import paper_report as PR
from kaiba.learning import signal_audit as sa
from kaiba.learning import sprint
from kaiba.learning import strategy_search as ss
from kaiba.ops import scheduler as S

ROOT = Path(__file__).resolve().parents[1]
DAY = 86_400_000
NOW = 1_800_000_000_000
SETTLED = NOW - ss.SETTLE_MS
SEL = SETTLED - 6 * DAY
LANE, CHAIN = "launch-snipe", "sol"


def case(token, t0, lane=LANE, chain=CHAIN):
    return SimpleNamespace(lane=lane, chain=chain, token=token, t0=t0)


def spread(n, prefix, offset=0):
    """n signal times spread over the forward window (both halves)."""
    return [(f"{prefix}{i}", SEL + (i + 1) * (6 * DAY // (n + 1)) + offset) for i in range(n)]


def tracked(verdict="PASS", filtered=True, cid="c1", sel=SEL):
    return {"candidate_id": cid, "lane": LANE, "chain": CHAIN, "filtered": filtered, "selected_ms": sel,
            "verdict": verdict}


def fwd(cases, ret=0.30):
    """The persisted forward record of a candidate that traded every one of ``cases``."""
    return {"c1": [(c.token, c.t0, ret) for c in cases]}


def guard(tr, cases, considered, rows=None, complete_from=SEL - DAY):
    rows = fwd(cases) if rows is None else rows
    return S.search_survivor_guard([tr], rows, cases, considered, complete_from_ms=complete_from,
                                   settled_ms=SETTLED, now=NOW)[tr["candidate_id"]]


def test_a_filtered_pass_with_dropped_signals_becomes_inconclusive():
    kept = [case(t, t0) for t, t0 in spread(40, "w")]
    lost = [(LANE, CHAIN, t, t0) for t, t0 in spread(400, "dead", offset=7)]
    considered = [(c.lane, c.chain, c.token, c.t0) for c in kept] + lost
    g = guard(tracked(), kept, considered)
    assert g["verdict"] == "INCONCLUSIVE" and g["verdict_before"] == "PASS"
    assert (g["considered"], g["dropped"]) == (440, 400) and "400 of 440" in g["why"]
    assert "membership is unknown" in g["why"]


def test_a_filtered_pass_cannot_lean_on_the_worst_case_rule():
    """Strong winners and ONE dropped signal: an unfiltered candidate survives that as a total loss,
    but a filtered one cannot, because the dropped token's filter membership is unknown."""
    kept = spread(200, "w")
    rows = {"c1": [(t, t0, 0.40) for t, t0 in kept]}
    considered = [(LANE, CHAIN, t, t0) for t, t0 in kept] + [(LANE, CHAIN, "dead", SEL + 2 * DAY)]
    g = guard(tracked(filtered=True), [case(t, t0) for t, t0 in kept], considered, rows)
    assert g["verdict"] == "INCONCLUSIVE" and "membership is unknown" in g["why"]


def test_full_coverage_leaves_a_filtered_pass_alone():
    """Positive control: the same candidate with nothing dropped keeps its PASS."""
    kept = [case(t, t0) for t, t0 in spread(40, "w")]
    g = guard(tracked(), kept, [(c.lane, c.chain, c.token, c.t0) for c in kept])
    assert g["verdict"] == "PASS" and g["dropped"] == 0 and g["why"] is None


def test_an_unfiltered_pass_must_survive_its_dropped_signals_as_total_losses():
    kept = spread(60, "w")
    rows = {"c1": [(t, t0, 0.10) for t, t0 in kept]}
    lost = [(LANE, CHAIN, t, t0) for t, t0 in spread(80, "dead", offset=7)]
    considered = [(LANE, CHAIN, t, t0) for t, t0 in kept] + lost
    g = guard(tracked(filtered=False), [case(t, t0) for t, t0 in kept], considered, rows)
    assert g["verdict"] == "INCONCLUSIVE" and g["worst_case_mean"] < 0 and "scored as total losses" in g["why"]


def test_an_unfiltered_pass_that_survives_one_dropped_signal_stands():
    kept = spread(200, "w")
    rows = {"c1": [(t, t0, 0.40) for t, t0 in kept]}
    considered = [(LANE, CHAIN, t, t0) for t, t0 in kept] + [(LANE, CHAIN, "dead", SEL + 2 * DAY)]
    g = guard(tracked(filtered=False), [case(t, t0) for t, t0 in kept], considered, rows)
    assert g["verdict"] == "PASS" and g["worst_case_lower"] > 0


def test_unknown_or_short_coverage_blocks_a_pass():
    kept = [case(t, t0) for t, t0 in spread(40, "w")]
    assert guard(tracked(), kept, None)["verdict"] == "INCONCLUSIVE"
    late = [(c.lane, c.chain, c.token, c.t0) for c in kept if c.t0 > SEL + 3 * DAY]
    g = guard(tracked(), [c for c in kept if c.t0 > SEL + 3 * DAY], late, complete_from=SEL + 3 * DAY)
    assert g["verdict"] == "INCONCLUSIVE" and "reach back" in g["why"]


def test_a_pass_the_run_did_not_see_cannot_stand():
    """Codex review 2026-11-03: positive persisted rows with considered=[] (the lane or chain left the
    audit's scope) used to PASS. Every persisted forward trade must be one of this run's signals."""
    kept = [case(t, t0) for t, t0 in spread(200, "w")]
    for considered, cases in (([], []), ([("sm-trenches", CHAIN, c.token, c.t0) for c in kept], [])):
        for filtered in (True, False):
            g = guard(tracked(filtered=filtered), cases, considered, rows=fwd(kept))
            assert g["verdict"] == "INCONCLUSIVE" and "200 of 200 persisted forward trades" in g["why"]
    half = [(c.lane, c.chain, c.token, c.t0) for c in kept[:100]]
    g = guard(tracked(), kept[:100], half, rows=fwd(kept))
    assert g["verdict"] == "INCONCLUSIVE" and "100 of 200" in g["why"]
    g = guard(tracked(), kept, [(c.lane, c.chain, c.token, c.t0) for c in kept], rows={})
    assert g["verdict"] == "INCONCLUSIVE" and "no persisted forward trades" in g["why"]


def test_only_this_lane_chain_after_selection_and_settled_counts_as_dropped():
    kept = [case(t, t0) for t, t0 in spread(40, "w")]
    noise = [("sm-trenches", CHAIN, "other-lane", SEL + DAY), (LANE, "bsc", "other-chain", SEL + DAY),
             (LANE, CHAIN, "before", SEL - DAY), (LANE, CHAIN, "at", SEL), (LANE, CHAIN, "unsettled", SETTLED + 1)]
    g = guard(tracked(), kept, [(c.lane, c.chain, c.token, c.t0) for c in kept] + noise)
    assert g["verdict"] == "PASS" and g["dropped"] == 0


def test_other_verdicts_are_unchanged():
    kept = [case(t, t0) for t, t0 in spread(40, "w")]
    lost = [(LANE, CHAIN, t, t0) for t, t0 in spread(400, "dead", offset=7)]
    for v in ("FAIL", "PENDING", "UNCORRECTED", None):
        g = guard(tracked(verdict=v), kept, [(c.lane, c.chain, c.token, c.t0) for c in kept] + lost)
        assert g["verdict"] == v and g["why"] is None


# --------------------------------------------------------------------------------------
# the job, end to end: the factory's own scoring says PASS, the guard turns it
# --------------------------------------------------------------------------------------


def _rising(t0):
    """A tape that rises steadily for six hours: every exit policy books a profit."""
    return [(t0 + k * 60_000, 1.003 ** max(k, 0), "tape", f"w{k % 7}", "buy" if k % 3 else "sell")
            for k in range(-10, 400)]


def _run_job(tmp_db, monkeypatch, *, dropped: int) -> tuple[dict, str]:
    filt = (ss.Cond("holder_count", 0.0, None),)
    spec = {"lane": LANE, "chain": CHAIN, "filter": [c.spec() for c in filt],
            "exit": ss.declared_policies(sa.ExitLadder())[0].spec()}
    cid = ss.candidate_id(spec)
    tmp_db.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, "
                   "run_id, selection_json) VALUES (?,?,?,?,?,?,?,?)",
                   (cid, LANE, CHAIN, ss.label_of(spec), json.dumps(spec, sort_keys=True), SEL, "r0", "{}"))
    cases_rows = []
    for tok, t0 in spread(60, "win"):
        rows = _rising(t0)
        c = sa.build_case(lane=LANE, chain=CHAIN, token=tok, t0=t0, rows=rows, wallets=["w1"],
                          payload={"holder_count": 50}, ladder=sa.ExitLadder(), kol=set(), trusted=set(),
                          grades_now={}, grades_pit=None)
        assert c is not None
        cases_rows.append((c, rows))
    lost = [(LANE, CHAIN, t, t0) for t, t0 in spread(dropped, "dead", offset=7)]

    def fake_audit(reader, *, on_case, **_kw):
        reader.considered = [(c.lane, c.chain, c.token, int(c.t0)) for c, _r in cases_rows] + lost
        for c, rows in cases_rows:
            on_case(c, rows)
        cases = [c for c, _r in cases_rows]
        return sa.AuditRun(cases, sa.score(cases), len(reader.considered), len(lost), None, 0, 0)

    monkeypatch.setattr(sa, "run_audit", fake_audit)
    sched = S.load_config(ROOT / "config" / "schedule.yaml")
    ctx = S.JobContext(name="strategy_search", conn=tmp_db, params=dict(sched.jobs["strategy_search"].params),
                       config=sched, started_ms=NOW, deadline_ms=NOW + 1_500_000, clock=lambda: NOW / 1000)
    return S.job_strategy_search(ctx), cid


def test_the_job_turns_a_survivor_only_forward_pass_and_resets_its_streak(tmp_db, monkeypatch):
    out, cid = _run_job(tmp_db, monkeypatch, dropped=500)
    verdict, why, streak = tmp_db.execute("SELECT fwd_verdict, fwd_why, fwd_streak FROM strategy_candidates "
                                          "WHERE candidate_id = ?", (cid,)).fetchone()
    assert out["survivor_guarded"] == [cid] and out["forward_pass_after_guard"] == 0
    assert verdict == "INCONCLUSIVE" and streak == 0 and "500 of 560" in why and "membership is unknown" in why
    (day_verdict,) = tmp_db.execute("SELECT verdict FROM strategy_forward_days WHERE candidate_id = ?", (cid,)).fetchone()
    assert day_verdict == "INCONCLUSIVE"
    rec = json.loads(tmp_db.execute("SELECT value FROM kv WHERE key = ?", (S.KV_SEARCH_GUARD,)).fetchone()[0])
    assert rec["run_id"] == out["run_id"] and rec["candidates"][cid]["verdict_before"] == "PASS"


def test_the_job_keeps_a_fully_covered_forward_pass(tmp_db, monkeypatch):
    """Positive control: the factory's own forward scoring passes this candidate."""
    out, cid = _run_job(tmp_db, monkeypatch, dropped=0)
    verdict, streak = tmp_db.execute("SELECT fwd_verdict, fwd_streak FROM strategy_candidates WHERE candidate_id = ?",
                                     (cid,)).fetchone()
    assert out["survivor_guarded"] == [] and verdict == "PASS" and streak == 1


def test_an_inconclusive_day_breaks_the_next_days_streak(tmp_db):
    """The factory recounts streaks from strategy_forward_days, so a turned day must stay turned there."""
    tmp_db.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, "
                   "run_id, selection_json) VALUES (?,?,?,?,?,?,?,?)", ("c0", LANE, CHAIN, "c0", "{}", SEL, "r", "{}"))
    days = ("2026-10-31", "2026-11-01", "2026-11-02")
    for d in days:
        tmp_db.execute("INSERT INTO strategy_forward_days (candidate_id, day, ts_ms, n, mean, mean_old, mean_new, "
                       "lower, p, family, verdict, why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("c0", d, NOW, 50, .3, .3, .3, .2, .01, 1, "PASS", ""))
    now = 1_793_577_600_000  # 2026-11-02 00:00 UTC
    S.apply_search_guard(tmp_db, {"c0": {"verdict_before": "PASS", "verdict": "INCONCLUSIVE", "why": "dropped"}},
                         run_id="r", now=now)
    assert tmp_db.execute("SELECT fwd_streak FROM strategy_candidates").fetchone()[0] == 0
    score = sprint.Score(LANE, CHAIN, 52, 26, 26, .3, .3, .3, .2, "PASS", "")
    ss._write_forward(tmp_db, [ss.Tracked("c0", LANE, CHAIN, (), None, SEL)],
                      [ss.ForwardScore("c0", score, 0.01, "PASS", "")], day="2026-11-03", now_ms=now + DAY,
                      cfg=ss.SearchConfig())
    assert tmp_db.execute("SELECT fwd_streak FROM strategy_candidates").fetchone()[0] == 1  # not 4


# --------------------------------------------------------------------------------------
# the owner's report
# --------------------------------------------------------------------------------------


def _search(guard_record):
    return {"run_id": "s1", "ts_ms": NOW, "tried": 900, "selected": 50, "survivors": 20, "tracked": 1,
            "method": "bh", "fdr_q": 0.1, "guard": guard_record,
            "candidates": [{"candidate_id": "c1", "label": "launch-snipe/sol [buyers_30m<1] exit=trail_half",
                            "selected_ms": NOW - 8 * DAY, "fwd_n": 92, "fwd_mean": 0.31, "fwd_lower": 0.19,
                            "fwd_verdict": "PASS", "fwd_streak": 4}]}


def test_the_report_calls_no_search_candidate_ready_without_a_guard_record():
    lines = "\n".join(PR.search_lines(_search(None)))
    assert "Survivor check: not recorded" in lines and "Search ready for your decision: none" in lines


def test_the_report_calls_a_turned_candidate_not_ready_and_says_why():
    rec = {"run_id": "s1", "candidates": {"c1": {"verdict_before": "PASS", "verdict": "INCONCLUSIVE",
                                                 "why": "the replay dropped 790 of 829 signals"}}}
    lines = "\n".join(PR.search_lines(_search(rec)))
    assert "1 forward PASS turned INCONCLUSIVE: the replay dropped 790 of 829" in lines
    assert "Search ready for your decision: none" in lines


def test_the_report_calls_a_cleared_candidate_ready():
    """Positive control: a guard record that clears the candidate lets a 4-day streak count."""
    rec = {"run_id": "s1", "candidates": {"c1": {"verdict_before": "PASS", "verdict": "PASS", "why": None}}}
    search = _search(rec)
    assert PR.search_ready(search["candidates"][0], search)
    assert "Search ready for your decision: launch-snipe/sol" in "\n".join(PR.search_lines(search))


def test_a_guard_record_from_an_older_run_does_not_clear_the_latest(tmp_db):
    tmp_db.execute("INSERT INTO strategy_search_runs (run_id, ts_ms, version, tried, selected, survivors, summary_json) "
                   "VALUES (?,?,?,?,?,?,?)", ("s2", NOW - 3_600_000, ss.SEARCH_VERSION, 9, 1, 1, "{}"))
    tmp_db.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, run_id, "
                   "selection_json, fwd_n, fwd_mean, fwd_lower, fwd_verdict, fwd_streak) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("c1", LANE, CHAIN, "cand-c1", "{}", NOW - 9 * DAY, "s2", "{}", 60, .3, .2, "PASS", 3))
    old = {"run_id": "s1", "candidates": {"c1": {"verdict_before": "PASS", "verdict": "PASS", "why": None}}}
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)", (PR.KV_SEARCH_GUARD, json.dumps(old), NOW))
    out = PR.run(tmp_db, NOW)
    assert out["search_ready"] == [] and "SEARCH CANDIDATE READY" not in out["text"]
