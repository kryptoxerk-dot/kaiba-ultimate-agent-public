"""The strategy factory (kaiba/learning/strategy_search.py, PROFIT-2).

The main way this module can fail is by finding noise: try a thousand candidates and some will
look profitable on any data. Each test is the failure it prevents:

* pure noise (random outcomes, many features) produces a survivor at the shipped family size --
  while the owner's pass line ALONE would have let noise through (so the correction is the
  load-bearing part, not decoration);
* a real edge planted in one band is NOT found (a search too strict to find anything is useless);
* a band edge is learned from the holdout (leakage) instead of the training data only;
* a candidate's forward record includes signals from before it was selected;
* a re-selected candidate gets a fresh selection time (resetting its forward record);
* a chain's extra cost is not charged, so a robinhood break-even reads as profit;
* an alternative exit is simulated on a different path or with different fills than the audit;
* the job writes anything but its own tables -- risk.yaml, lane config, modes or sizes;
* the owner's report calls a candidate ready before 3 forward passes, or overflows Telegram.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from kaiba.learning import paper_report as PR
from kaiba.learning import signal_audit as sa
from kaiba.learning import sprint
from kaiba.learning import strategy_search as ss
from kaiba.ops import scheduler as S

ROOT = Path(__file__).resolve().parents[1]
DAY = ss.DAY_MS
NOW = 1_800_000_000_000
POLICIES = ss.declared_policies(sa.ExitLadder())
CFG = ss.SearchConfig()  # the shipped defaults ARE the family size under test


def _shipped_cfg() -> ss.SearchConfig:
    return ss.SearchConfig.from_params(S.load_config(ROOT / "config" / "schedule.yaml").jobs["strategy_search"].params)


def _dataset(seed: int, *, groups=(("sm-trenches", "sol"), ("launch-snipe", "sol"), ("trusted-copy", "sol")),
             n: int = 600, base_mean: float = 0.0, edge: float = 0.0, n_features: int = 12,
             chain_override: str | None = None, sd: float = 0.3, policy_sd: float = 0.1) -> list[ss.Obs]:
    """``n`` settled signals per group over 12 days. Outcomes are noise around ``base_mean``;
    with ``edge`` > 0, signals whose ``f_edge`` is in its top quartile get ``edge`` added under
    EVERY exit policy."""
    rng = random.Random(seed)
    end = NOW - ss.SETTLE_MS
    out: list[ss.Obs] = []
    for lane, chain in groups:
        chain = chain_override or chain
        for i in range(n):
            t0 = end - 12 * DAY + int((i + 0.5) * 12 * DAY / n)
            feats = {f"f{k}": rng.random() for k in range(n_features)}
            feats["f_edge"] = rng.random()
            feats["launchpad"] = rng.choice(["pump", "bonk", "flap"])
            shift = edge if feats["f_edge"] >= 0.75 else 0.0
            common = rng.gauss(base_mean + shift, sd)
            raw = {p.key: max(-1.0, min(3.0, common + rng.gauss(0, policy_sd))) for p in POLICIES}
            out.append(ss.Obs(lane, chain, f"T{lane}{i}", t0, feats, raw))
    return out


# --------------------------------------------------------------------------------------
# multiple testing
# --------------------------------------------------------------------------------------


NOISE_SEEDS = (11, 12, 13, 14)


def test_pure_noise_yields_no_survivor_at_the_shipped_family_size():
    """Random outcomes (mean exactly 0), 14 features, 9 exits, 3 groups: ~2,700 candidates tried
    and 60 carried to the holdout per dataset. Nothing may survive.

    The second assertion is what makes this a test of the CORRECTION: on the same holdouts the
    owner's pass line alone passes noise. MEASURED 2026-10-13 on seeds 11-20: pass-line-only
    2, 6, 0, 3, 1, 0, 9, 5, 0, 1 noise candidates; with the correction 0 on all ten."""
    survivors, pass_line_only = [], []
    for seed in NOISE_SEEDS:
        res = ss.search(_dataset(seed), POLICIES, _shipped_cfg(), end_ms=NOW - ss.SETTLE_MS)
        assert res.tried >= 2_500, res.groups
        assert len(res.selected) == 3 * CFG.max_selected_per_group
        survivors += [(seed, c.label, c.hold_p) for c in res.survivors]
        pass_line_only += [c for c in res.selected if c.hold_score and c.hold_score.verdict == "PASS"]
    assert survivors == []
    assert len(pass_line_only) >= 5, "the pass line alone must let noise through, or this proves nothing"


def test_a_planted_edge_in_one_band_is_found_and_nothing_else_is():
    obs = _dataset(21, base_mean=-0.10, edge=0.30)
    res = ss.search(obs, POLICIES, _shipped_cfg(), end_ms=NOW - ss.SETTLE_MS)
    assert res.survivors, [(c.label, c.hold_p, c.hold_score.verdict if c.hold_score else None)
                           for c in res.selected[:5]]
    for c in res.survivors:
        edge = [k for k in c.filt if k.feature == "f_edge"]
        assert edge, f"a survivor without the planted band is noise: {c.label}"
        assert all(k.lo is not None and k.lo >= 0.5 for k in edge), c.label


def test_benjamini_hochberg_and_the_t_tail_match_textbook_values():
    # BH: m=5, q=0.05 -> thresholds .01,.02,.03,.04,.05 on the sorted p (.01,.011,.03,.04,.2):
    # the 4th (.04 <= .04) is the largest that fits, so the four smallest are rejected.
    pv = [0.01, 0.04, 0.03, 0.2, 0.011]
    assert ss.benjamini_hochberg(pv, 0.05) == [True, True, True, False, True]
    # BY divides by 1+1/2+..+1/5 = 2.283: the first threshold is .0044, so nothing is rejected.
    assert ss.benjamini_hochberg(pv, 0.05, method="by") == [False] * 5
    assert ss.benjamini_hochberg([0.001, 0.5], 0.05, method="by") == [True, False]
    assert abs(ss.t_sf(2.0, 10) - 0.036694) < 1e-5
    assert abs(ss.t_sf(1.6839, 40) - 0.05) < 1e-4
    assert abs(ss.t_sf(-2.0, 10) - (1 - 0.036694)) < 1e-5


# --------------------------------------------------------------------------------------
# leakage
# --------------------------------------------------------------------------------------


def test_band_edges_are_learned_on_the_training_part_only():
    """Training values live in [0, 1); holdout values in [100, 200). Any cut above 1 could only
    have come from the holdout."""
    obs = _dataset(31, groups=(("sm-trenches", "sol"),), n=400)
    split = sorted(o.t0 for o in obs)[int(len(obs) * CFG.train_frac)]
    for o in obs:
        if o.t0 >= split:
            o.features = {k: (v + 100.0 if isinstance(v, float) else v) for k, v in o.features.items()}
    res = ss.search(obs, POLICIES, CFG, end_ms=NOW - ss.SETTLE_MS)
    cuts = [x for c in res.selected for k in c.filt for x in (k.lo, k.hi) if x is not None]
    assert cuts, "the search must select banded candidates for this test to mean anything"
    assert max(cuts) < 1.0, max(cuts)
    train = [o for o in obs if o.t0 < split]
    conds = ss.single_conditions(train, CFG)
    assert all((k.lo is None or k.lo < 1) and (k.hi is None or k.hi < 1) for k in conds if k.eq is None)


def test_unsettled_and_lookahead_inputs_never_reach_the_search():
    ok = sa.SignalCase("sm-trenches", "sol", "A", NOW - ss.SETTLE_MS - 1, 0.1, {"x_posts": 9, "late_s": 3})
    young = sa.SignalCase("sm-trenches", "sol", "B", NOW - ss.SETTLE_MS + 1, 0.1, {})
    raw = {("sm-trenches", "sol", "A", ok.t0): {"k": 0.1}, ("sm-trenches", "sol", "B", young.t0): {"k": 0.1}}
    obs = ss.observations([ok, young], raw, settled_before_ms=NOW - ss.SETTLE_MS)
    assert [o.token for o in obs] == ["A"]
    many = [ss.Obs("l", "sol", str(i), i, {"x_posts": i, "gradeAB_now": i, "x_followers": i, "late_s": i}, {})
            for i in range(100)]
    feats = {c.feature for c in ss.single_conditions(many, CFG)}
    assert feats == {"late_s"}


# --------------------------------------------------------------------------------------
# forward tracking
# --------------------------------------------------------------------------------------


def _tracked_row(conn, *, selected_ms: int, filt=(), policy=POLICIES[0], lane="sm-trenches", chain="sol") -> ss.Tracked:
    spec = {"lane": lane, "chain": chain, "filter": [c.spec() for c in filt], "exit": policy.spec()}
    cid = ss.candidate_id(spec)
    import json

    conn.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, "
                 "run_id, selection_json) VALUES (?,?,?,?,?,?,?,?)",
                 (cid, lane, chain, ss.label_of(spec), json.dumps(spec, sort_keys=True), selected_ms, "r0", "{}"))
    return ss.Tracked.from_row(cid, json.dumps(spec), selected_ms)


def test_forward_uses_only_signals_after_selection(tmp_db):
    """60 great signals BEFORE selection, 60 losing ones after. The forward record must be the
    losers only: a candidate cannot carry its selection data into its forward score."""
    sel = NOW - 8 * DAY
    key = POLICIES[0].key
    step = 3 * 3_600_000  # 60 x 3 h = 7.5 d, inside the 8 d since selection: both halves populated
    obs = [ss.Obs("sm-trenches", "sol", f"pre{i}", sel - (i + 1) * step, {}, {key: 0.50}) for i in range(60)]
    obs += [ss.Obs("sm-trenches", "sol", f"post{i}", sel + (i + 1) * step, {}, {key: -0.10}) for i in range(60)]
    obs.append(ss.Obs("sm-trenches", "sol", "at", sel, {}, {key: 0.50}))  # exactly at selection: not forward
    tr = _tracked_row(tmp_db, selected_ms=sel)
    fwd = ss.forward_trades(ss.load_tracked(tmp_db), obs)
    assert len(fwd[tr.candidate_id]) == 60
    ss.record(tmp_db, ss.SearchResult(), fwd, now_ms=NOW, cfg=CFG, summary={})
    (n, lo_t0, mean) = tmp_db.execute(
        "SELECT count(*), min(t0_ms), avg(ret) FROM strategy_forward_trades WHERE candidate_id=?",
        (tr.candidate_id,)).fetchone()
    assert n == 60 and lo_t0 > sel and mean < 0
    row = tmp_db.execute("SELECT fwd_n, fwd_mean, fwd_verdict FROM strategy_candidates WHERE candidate_id=?",
                         (tr.candidate_id,)).fetchone()
    assert row[0] == 60 and row[1] < 0 and row[2] == "FAIL"


def test_a_reselected_candidate_keeps_its_original_selection_time(tmp_db):
    tr = _tracked_row(tmp_db, selected_ms=NOW - 5 * DAY)
    cand = ss.Candidate("sm-trenches", "sol", (), POLICIES[0], 100, 0.1, 3.0, hold_p=1e-6,
                        hold_score=sprint.Score("sm-trenches", "sol", 50, 25, 25, .1, .1, .1, .05, "PASS", ""),
                        significant=True)
    assert cand.candidate_id == tr.candidate_id
    res = ss.SearchResult(selected=[cand])
    out = ss.record(tmp_db, res, {}, now_ms=NOW, cfg=CFG, summary={})
    assert out["added"] == []
    (sel,) = tmp_db.execute("SELECT selected_ms FROM strategy_candidates WHERE candidate_id=?",
                            (tr.candidate_id,)).fetchone()
    assert sel == NOW - 5 * DAY


def test_forward_pass_needs_the_correction_across_the_tracked_family(tmp_db):
    """One modest forward record among many tracked: the pass line holds, the corrected test
    does not -> UNCORRECTED, which does not count toward readiness."""
    sel = NOW - 10 * DAY
    rng = random.Random(5)
    key = POLICIES[0].key
    trs = [_tracked_row(tmp_db, selected_ms=sel, filt=(ss.Cond("f", i, i + 1),)) for i in range(30)]
    step = 10 * DAY // (30 * 50 + 1)
    obs = [ss.Obs("sm-trenches", "sol", f"t{j}", sel + (j + 1) * step, {"f": j % 30 + 0.5},
                  {key: rng.gauss(0.0, 0.3)}) for j in range(30 * 50)]
    lucky = trs[0]
    for o in obs:
        if lucky.filt[0].test(o.features):
            o.raw[key] = rng.gauss(0.10, 0.3)
    fwd = ss.forward_trades(ss.load_tracked(tmp_db), obs)
    out = ss.record(tmp_db, ss.SearchResult(), fwd, now_ms=NOW, cfg=CFG, summary={})
    raw = {tr.candidate_id: sprint.score("sm-trenches", "sol", [(t, r) for _tok, t, r in fwd[tr.candidate_id]],
                                         sel, NOW).verdict for tr in trs}
    assert "PASS" in raw.values(), "the setup must contain a pass-line pass for this test to mean anything"
    for cid, v in raw.items():
        if v == "PASS":
            assert out["forward"][cid] in ("PASS", "UNCORRECTED")
    assert any(out["forward"][cid] == "UNCORRECTED" for cid, v in raw.items() if v == "PASS")


# --------------------------------------------------------------------------------------
# costs and exits
# --------------------------------------------------------------------------------------


def test_chain_costs_are_charged_so_a_robinhood_breakeven_is_not_a_survivor():
    """Identical replayed outcomes (+2% a trade) on sol and robinhood. Robinhood costs 1.25% more
    per side (fill_costs, MEASURED), so the same trades are a loss there."""
    obs = []
    for chain in ("sol", "robinhood"):
        obs += _dataset(41, groups=(("sm-trenches", chain),), base_mean=0.02, sd=0.02, policy_sd=0.0)
    res = ss.search(obs, POLICIES, CFG, end_ms=NOW - ss.SETTLE_MS)
    assert {c.chain for c in res.survivors} == {"sol"}
    rh = [o for o in obs if o.chain == "robinhood"]
    key = POLICIES[0].key
    assert sum(o.raw[key] for o in rh) > 0 > sum(o.net(key) for o in rh)
    o = obs[-1]
    assert o.net(POLICIES[0].key) == sprint.net(o.raw[POLICIES[0].key], "robinhood")


def _rows(seed: int, t0: int, n: int = 400) -> list[tuple]:
    rng = random.Random(seed)
    p, rows, t = 1.0, [], t0 - 600_000
    for _ in range(n):
        t += rng.randint(2_000, 90_000)
        p *= 1 + rng.gauss(0.002, 0.06)
        rows.append((t, p, "tape", f"w{rng.randint(0, 30)}", rng.choice(["buy", "sell"])))
        if rng.random() < 0.15:
            rows.append((t + 1, p * 3, "other", None, None))  # a second source must not mix in
    return rows


@pytest.mark.parametrize("seed", range(25))
def test_the_live_policy_reproduces_the_audit_outcome_exactly(seed):
    t0 = NOW - DAY
    rows = _rows(seed, t0)
    case = sa.build_case(lane="sm-trenches", chain="sol", token="T", t0=t0, rows=rows, wallets=["w1"],
                         payload={}, ladder=sa.ExitLadder(), kol=set(), trusted=set(), grades_now={},
                         grades_pit=None)
    if case is None:
        pytest.skip("tape too thin for this seed")
    col = ss.PathCollector(POLICIES)
    col(case, rows)
    assert col.raw[("sm-trenches", "sol", "T", t0)][POLICIES[0].key] == case.outcome


@pytest.mark.parametrize("seed", range(25))
def test_the_time_exit_loop_is_the_audit_loop_when_the_clock_never_fires(seed):
    rows = _rows(100 + seed, NOW)
    series, i = sa.price_path(rows, NOW)
    entry = series[i][1] * (1 + sa.COST_PER_LEG)
    for pol in POLICIES:
        far = ss._simulate_with_hold(series, i, entry, pol.ladder, sa.HORIZON_MS + 3_600_000)
        assert far == sa.simulate_exit(series, i, entry, pol.ladder), pol.name


def test_a_time_exit_sells_before_a_later_crash():
    t = NOW
    series = [(t + k * 60_000, 1.0 + 0.001 * k) for k in range(20)] + [(t + 20 * 60_000, 0.2)]
    entry = series[0][1] * (1 + sa.COST_PER_LEG)
    hold = next(p for p in POLICIES if p.name == "hold15m")
    out = ss.simulate(series, 0, entry, hold)
    # first print >= 15 min + 12 s is the 16-minute print, 1.016
    assert abs(out - (1.016 * (1 - sa.COST_PER_LEG) / entry - 1)) < 1e-12
    assert ss.simulate(series, 0, entry, POLICIES[0]) < -0.5


# --------------------------------------------------------------------------------------
# the job: records only
# --------------------------------------------------------------------------------------


def _fake_audit(cases_rows):
    def audit(reader, *, ladder, lanes, chains, days, max_signals, now_ms, deadline_ms, clock, on_case,
              tape_cache=None):
        cases = []
        for case, rows in cases_rows:
            cases.append(case)
            on_case(case, rows)
        return sa.AuditRun(cases, sa.score(cases), len(cases), 0, None, 0, 0)
    return audit


def _hash_tree(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_the_job_writes_only_its_own_tables_and_never_risk_or_lane_config(tmp_db, monkeypatch):
    from kaiba.core import config as core_config

    def _refuse(*a, **k):
        raise AssertionError("strategy_search must never save risk.yaml")

    monkeypatch.setattr(core_config, "save_risk", _refuse)
    end = NOW - ss.SETTLE_MS
    cases_rows = []
    for i in range(200):
        t0 = end - 12 * DAY + i * 5_000_000
        rows = _rows(1000 + i, t0, n=120)
        case = sa.build_case(lane="sm-trenches", chain="sol", token=f"T{i}", t0=t0, rows=rows, wallets=["w1"],
                             payload={"holder_count": i}, ladder=sa.ExitLadder(), kol=set(), trusted=set(),
                             grades_now={}, grades_pit=None)
        if case is not None:
            cases_rows.append((case, rows))
    monkeypatch.setattr(sa, "run_audit", _fake_audit(cases_rows))
    _tracked_row(tmp_db, selected_ms=end - 6 * DAY)  # exercises the forward write path
    watched = sorted((ROOT / "config").glob("*.yaml"))
    before = _hash_tree(watched)
    writes: set[str] = set()

    def authorizer(action, arg1, arg2, dbname, source):
        import sqlite3

        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            writes.add(str(arg1))
        return sqlite3.SQLITE_OK

    tmp_db.set_authorizer(authorizer)
    sched = S.load_config(ROOT / "config" / "schedule.yaml")
    ctx = S.JobContext(name="strategy_search", conn=tmp_db, params=dict(sched.jobs["strategy_search"].params),
                       config=sched, started_ms=NOW, deadline_ms=NOW + 1_500_000, clock=lambda: NOW / 1000)
    out = S.job_strategy_search(ctx)
    tmp_db.set_authorizer(None)
    assert out["settled"] > 0 and out["tried"] > 0
    # 2026-11-03: plus ONE kv row, the survivor guard's record (SEARCH-SURVIVOR-GUARD-CLAUDE-20261103)
    assert writes and all(w.startswith("strategy_") or w == "kv" for w in writes), writes
    assert [k for (k,) in tmp_db.execute("SELECT key FROM kv")] == [S.KV_SEARCH_GUARD]
    assert _hash_tree(watched) == before
    assert tmp_db.execute("SELECT count(*) FROM strategy_search_runs").fetchone()[0] == 1
    assert tmp_db.execute("SELECT count(*) FROM strategy_forward_days").fetchone()[0] == 1


def test_the_shipped_schedule_keeps_the_job_bounded_and_the_family_capped():
    sched = S.load_config(ROOT / "config" / "schedule.yaml")
    jc = sched.jobs["strategy_search"]
    assert jc.enabled and jc.interval_s == 86_400
    assert sched.long_timeout_s <= jc.timeout_s < sched.lock_stale_s
    cfg = _shipped_cfg()
    assert cfg.method in ("bh", "by") and cfg.fdr_q <= 0.10
    assert cfg.max_pairs <= cfg.pair_top_k * (cfg.pair_top_k - 1) // 2


# --------------------------------------------------------------------------------------
# the owner's report
# --------------------------------------------------------------------------------------


def _report(conn, streaks: list[int], label_len: int = 40, guard_run: str | None = "s1") -> str:
    """``guard_run``: the run id of the survivor-guard record that clears every PASS (None: no record)."""
    import json

    conn.execute("INSERT INTO strategy_search_runs (run_id, ts_ms, version, tried, selected, survivors, summary_json) "
                 "VALUES (?,?,?,?,?,?,?)", ("s1", NOW - 3_600_000, ss.SEARCH_VERSION, 3105, 60, 2,
                                            json.dumps({"config": {"method": "bh", "fdr_q": 0.1}})))
    for i, st in enumerate(streaks):
        conn.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, "
                     "run_id, selection_json, fwd_n, fwd_mean, fwd_lower, fwd_verdict, fwd_streak) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (f"c{i}", "sm-trenches", "sol", f"cand{i}-" + "x" * label_len, "{}", NOW - 9 * DAY, "s1", "{}",
                      50 + i, 0.05, 0.01, "PASS" if st else "FAIL", st))
    conn.execute("DELETE FROM kv WHERE key = ?", (PR.KV_SEARCH_GUARD,))
    if guard_run is not None:
        verdicts = {f"c{i}": "PASS" if st else "FAIL" for i, st in enumerate(streaks)}
        guard = {"run_id": guard_run, "ts_ms": NOW - 3_600_000,
                 "candidates": {cid: {"verdict_before": v, "verdict": v, "why": None} for cid, v in verdicts.items()}}
        conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                     (PR.KV_SEARCH_GUARD, json.dumps(guard), NOW))
    return PR.run(conn, NOW)["text"]


def test_report_shows_the_search_and_calls_ready_only_after_three_forward_passes(tmp_db):
    text = _report(tmp_db, [2, 0])
    assert "Strategy search" in text and "tried 3105, to holdout 60, survivors 2" in text
    assert "SEARCH CANDIDATE READY" not in text and "Search ready for your decision: none" in text
    tmp_db.execute("DELETE FROM strategy_candidates")
    tmp_db.execute("DELETE FROM strategy_search_runs")
    text = _report(tmp_db, [3])
    assert text.startswith("SEARCH CANDIDATE READY FOR YOUR DECISION: cand0-")
    assert "Search ready for your decision: cand0-" in text


def test_report_with_a_full_search_section_stays_inside_telegrams_limit(tmp_db):
    text = _report(tmp_db, [3, 4, 5, 2, 1, 0, 0, 0, 0, 0], label_len=400)
    assert len(text) < 4096
    assert text.count(" fwd n=") == PR.SEARCH_ROWS


def test_forward_streak_counts_only_calendar_consecutive_pass_days(tmp_db):
    """2026-10-14 (Codex audit): PASS rows on non-consecutive days used to add up to a streak."""
    from kaiba.learning import sprint

    tmp_db.execute("INSERT INTO strategy_candidates (candidate_id, lane, chain, label, spec_json, selected_ms, "
                   "run_id, selection_json) VALUES (?,?,?,?,?,?,?,?)",
                   ("c0", "sm-trenches", "sol", "c0", "{}", NOW - 20 * DAY, "s1", "{}"))
    for d in ("2026-10-11", "2026-10-13"):  # 10-12 missing
        tmp_db.execute("INSERT INTO strategy_forward_days (candidate_id, day, ts_ms, n, mean, mean_old, mean_new, "
                       "lower, p, family, verdict, why) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("c0", d, NOW, 50, 0.05, 0.05, 0.05, 0.01, 0.01, 1, "PASS", ""))
    score = sprint.Score("sm-trenches", "sol", 52, 26, 26, 0.05, 0.05, 0.05, 0.01, "PASS", "")
    tracked = [ss.Tracked("c0", "sm-trenches", "sol", (), None, NOW - 20 * DAY)]
    ss._write_forward(tmp_db, tracked, [ss.ForwardScore("c0", score, 0.01, "PASS", "")],
                      day="2026-10-14", now_ms=NOW, cfg=ss.SearchConfig())
    streak = tmp_db.execute("SELECT fwd_streak FROM strategy_candidates WHERE candidate_id='c0'").fetchone()[0]
    assert streak == 2  # 10-14 and 10-13; the gap at 10-12 ends it
