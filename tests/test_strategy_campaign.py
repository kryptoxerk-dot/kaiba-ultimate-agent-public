"""Prospective learning must reject duplicated, stale, replayed and incomplete profit."""

from __future__ import annotations

import copy
import dataclasses
import json
import random

import pytest

from kaiba.learning import signal_audit as sa
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import strategy_search as ss

DAY = ss.DAY_MS
FROZEN = 1_800_000_000_000 // DAY * DAY
NOW = FROZEN + 10 * DAY + DAY // 2


def _manifest(groups=(("sm-trenches", "sol"),), cap=6):
    return sc.generate_campaign([], sa.ExitLadder(), now_ms=FROZEN, groups=groups, max_candidates=cap)


def _row(manifest, arm, i, signal_ms, *, ret=.20):
    features = {}
    for cond in arm["spec"]["filter"]:
        features[cond["f"]] = (cond["eq"] if cond["eq"] is not None else
                                (cond["lo"] + cond["hi"]) / 2 if cond["lo"] is not None and cond["hi"] is not None
                                else cond["lo"] + .001 if cond["lo"] is not None else cond["hi"] - .001)
    return {"campaign_id": manifest["campaign_id"], "hypothesis_id": arm["hypothesis_id"],
            "spec_hash": arm["spec_hash"], "outcome_id": f"{arm['hypothesis_id']}:p{i}",
            "signal_id": f"signal{i}", "token": f"token{i}",
            "lane": arm["spec"]["lane"], "chain": arm["spec"]["chain"],
            "signal_ms": signal_ms, "opened_ms": signal_ms + 1_000,
            "closed_ms": signal_ms + 2_000, "observed_ms": signal_ms + 2_000,
            "provider": "paper", "source": "PaperBroker", "status": "closed",
            "features": features,
            "evidence_complete": True, "cost_complete": True, "inventory_complete": True,
            "entry_debit_native": 1_000_000, "exit_credit_native": round(1_000_000 * (1 + ret))}


def _outcomes(manifest, *, n=60, now=NOW, ret=.2):
    return [_row(manifest, arm, i, FROZEN + (i + 1) * (now - FROZEN - 4_000) // n, ret=ret)
            for arm in manifest["hypotheses"] for i in range(n)]


def _ready(manifest):
    rows = _outcomes(manifest)
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    for day in (1, 2):
        now = NOW + day * DAY
        rows += [_row(manifest, arm, 60 + day, now - 3_000)
                 for arm in manifest["hypotheses"]]
        result = sc.assess_campaign(manifest, rows, now_ms=now, prior_state=result["next_state"])
    return rows, result


def test_bootstrap_declares_hypotheses_with_zero_survivors_and_no_database():
    manifest = _manifest()
    assert manifest["tried"] == manifest["selected"] == manifest["holdout_survivors"] == 0
    assert {h["spec"]["exit"]["name"] for h in manifest["hypotheses"]} == {
        "incumbent", "hold60s", "hold5m", "tp1.25all"}
    assert all(h["kind"] == "exploratory" and h["status"] == "PAPER_ONLY" for h in manifest["hypotheses"])
    assert sc.generate_campaign([], sa.ExitLadder(), now_ms=FROZEN) != manifest
    assert _manifest() == manifest
    json.dumps(manifest, allow_nan=False)


def test_global_cap_allocates_fair_pairs_and_records_omissions():
    groups = [("sm-trenches", c) for c in ("sol", "bsc", "robinhood")]
    manifest = _manifest(groups, cap=6)
    assert len(manifest["hypotheses"]) == 6
    for lane, chain in groups:
        exits = {h["spec"]["exit"]["name"] for h in manifest["hypotheses"]
                 if h["spec"]["lane"] == lane and h["spec"]["chain"] == chain}
        assert exits == {"incumbent", "hold60s"}
    assert len(manifest["omitted_hypotheses"]) == 6
    small = _manifest(groups, cap=2)
    assert len(small["hypotheses"]) == 2 and len(small["omitted_groups"]) == 2
    assert len(_manifest(groups, cap=12)["hypotheses"]) == 12


def _history(*, edge, seed=32):
    rng = random.Random(seed)
    policies = [*ss.declared_policies(sa.ExitLadder()), *sc.exploration_policies(sa.ExitLadder())]
    end = FROZEN - ss.SETTLE_MS
    out = []
    for i in range(600):
        v = rng.random()
        ret = rng.gauss(-.08 + (edge if v >= .75 else 0), .20)
        feats = {"top10_pct": 100 * v, "buy_share_30m": v,
                 "gradeAB_now": int(ret > 0), "x_posts": int(ret > 0),
                 "kol_pre": int(ret > 0), "trusted": int(ret > 0), "future_profit": ret,
                 "wallet_track_best": ret, "wallet_track_hist": 100, "wallet_track_good": 3}
        out.append(ss.Obs("sm-trenches", "sol", f"T{i}", end - 12 * DAY + (i + 1) * 12 * DAY // 601,
                          feats, {p.key: ret for p in policies}))
    return out


def test_real_training_search_finds_planted_edge_and_freezes_only_entry_time_bands():
    manifest = sc.generate_campaign(_history(edge=.45), sa.ExitLadder(), now_ms=FROZEN, max_candidates=6)
    trained = [h for h in manifest["hypotheses"] if h["kind"] == "trained"]
    assert trained and manifest["holdout_survivors"] > 0
    for h in trained:
        assert h["selection"]["hold_verdict"] == "PASS" and h["selection"]["significant"]
        assert {c["f"] for c in h["spec"]["filter"]} == {"top10_pct"}
    assert set(manifest["excluded_features"]) >= {"gradeAB_now", "x_posts", "kol_pre", "trusted", "future_profit",
                                                 "wallet_track_best", "wallet_track_hist", "wallet_track_good"}
    assert len(manifest["source_versions"]) >= 6


def test_learned_campaign_uses_remaining_global_slots_for_baseline_and_trained_pairs():
    history = _history(edge=.45)
    observations = []
    for chain in ("sol", "bsc", "robinhood"):
        observations += [ss.Obs(o.lane, chain, o.token, o.t0, o.features, o.raw) for o in history]
    baseline = _manifest((("sm-trenches", c) for c in ("sol", "bsc", "robinhood")), cap=12)
    manifest = sc.generate_campaign(observations, sa.ExitLadder(), now_ms=FROZEN, max_candidates=6,
                                    groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")],
                                    trained_only=True)
    assert len(baseline["hypotheses"]) + len(manifest["hypotheses"]) == 18
    assert len(manifest["hypotheses"]) == 6
    for chain in ("sol", "bsc", "robinhood"):
        arms = [h for h in manifest["hypotheses"] if h["spec"]["chain"] == chain]
        assert {h["kind"] for h in arms} == {"control", "trained"}
        assert any(h["spec"]["exit"]["name"] == "incumbent" for h in arms)
    assert not any(h["kind"] == "exploratory" for h in manifest["hypotheses"])
    assert manifest["omitted_hypotheses"]


def test_learned_campaign_with_no_survivor_does_not_create_phony_research_books():
    manifest = sc.generate_campaign([], sa.ExitLadder(), now_ms=FROZEN, groups=[("sm-trenches", "sol")],
                                    max_candidates=6, trained_only=True)
    assert manifest["hypotheses"] == [] and manifest["mode"] == "trained"
    assert manifest["omitted_groups"] == [["sm-trenches", "sol"]]


def test_learned_campaign_below_two_free_slots_cannot_create_an_unpaired_control():
    manifest = sc.generate_campaign(_history(edge=.45), sa.ExitLadder(), now_ms=FROZEN,
                                    max_candidates=1, trained_only=True)
    assert manifest["holdout_survivors"] > 0
    assert manifest["hypotheses"] == []


def test_incumbent_survival_alone_is_not_a_learned_challenger():
    base = sa.ExitLadder()
    key = ss.ExitPolicy("live", base).key
    end = FROZEN - ss.SETTLE_MS
    obs = [ss.Obs("sm-trenches", "sol", f"t{i}", end - 12 * DAY + (i + 1) * 12 * DAY // 101,
                  {}, {key: .2}) for i in range(100)]
    manifest = sc.generate_campaign(obs, base, now_ms=FROZEN, max_candidates=6, trained_only=True)
    assert manifest["holdout_survivors"] == 1
    assert manifest["hypotheses"] == []


def test_unsettled_outcomes_and_unknown_lookahead_features_do_not_change_frozen_search():
    history = _history(edge=.45)
    initial = sc.generate_campaign(history, sa.ExitLadder(), now_ms=FROZEN)
    for obs in history:
        obs.features["future_profit"] = 100000
    history.append(ss.Obs("sm-trenches", "sol", "future", FROZEN,
                          {"buy_share_30m": 1e9}, {"bad": 3.0}))
    again = sc.generate_campaign(history, sa.ExitLadder(), now_ms=FROZEN)
    assert initial["campaign_id"] == again["campaign_id"]


def test_zero_mean_training_noise_is_never_treated_as_proven_profitable():
    history = _history(edge=0)
    for obs in history:
        obs.raw = {key: value + .08 for key, value in obs.raw.items()}  # zero-mean, unplanted noise
    manifest = sc.generate_campaign(history, sa.ExitLadder(), now_ms=FROZEN)
    # This seed really produces false discoveries in the existing q=.10 search.
    # Their existence must not imply profit: every discovered arm is still PAPER_ONLY.
    assert manifest["holdout_survivors"] > 0
    assert all(h["status"] == "PAPER_ONLY" for h in manifest["hypotheses"])
    no_forward = sc.assess_campaign(manifest, [], now_ms=NOW)
    assert all(a["status"] == "UNPROVEN" for a in no_forward["arms"])
    rows = _outcomes(manifest, n=100)
    for i, row in enumerate(rows):
        row["exit_credit_native"] = 900_000 if i % 2 else 1_100_000  # zero-mean prospective noise
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    assert all(a["status"] != "PAPER_EVIDENCE_READY" and a["streak"] == 0 for a in result["arms"])


def test_constant_profitable_training_returns_are_json_serializable_without_repeated_incumbents():
    history = _history(edge=.45)
    for obs in history:
        obs.raw = {key: .2 for key in obs.raw}
    manifest = sc.generate_campaign(history, sa.ExitLadder(), now_ms=FROZEN, max_candidates=6)
    assert manifest["holdout_survivors"] > 0
    json.dumps(manifest, allow_nan=False)
    specs = []
    for h in manifest["hypotheses"]:
        spec = copy.deepcopy(h["spec"])
        spec["exit"].pop("name")
        specs.append(json.dumps(spec, sort_keys=True))
    assert len(set(specs)) == len(specs)


def test_new_monotonic_actual_cashflows_on_three_consecutive_dates_are_ready_for_paper_only():
    manifest = _manifest()
    rows, result = _ready(manifest)
    assert all(a["status"] == "PAPER_EVIDENCE_READY" and a["streak"] == 3 for a in result["arms"])
    assert result["funding_allowed"] is False
    assert all(a["score"]["mean"] == pytest.approx(.2) for a in result["arms"])
    assert all(a["economic_basis"].startswith("PaperBroker") for a in result["arms"])
    assert len(rows) == 4 * 62


def test_fees_are_not_deducted_twice_on_bsc_or_robinhood():
    manifest = _manifest((("sm-trenches", "bsc"), ("sm-trenches", "robinhood")), cap=8)
    result = sc.assess_campaign(manifest, _outcomes(manifest, ret=.01), now_ms=NOW)
    assert all(a["score"]["mean"] == pytest.approx(.01) and a["status"] == "PASS" for a in result["arms"])


@pytest.mark.parametrize("change", [
    {"source": "replay"}, {"provider": "gmgn"}, {"spec_hash": "different"},
    {"signal_ms": FROZEN}, {"status": "open"}, {"status": "partial"},
    {"status": "unfillable"}, {"entry_debit_native": 0}, {"exit_credit_native": None},
    {"cost_complete": False}, {"inventory_complete": False}, {"observed_ms": NOW + 1},
    {"feature_ts_ms": NOW},
])
def test_lookahead_replay_open_partial_missing_denominators_and_costs_remain_unknown(change):
    manifest = _manifest()
    rows = _outcomes(manifest)
    rows[0].update(change)
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    arm = result["arms"][0]
    assert arm["status"] == "UNPROVEN" and arm["unknown_count"] == 1 and arm["streak"] == 0
    assert arm["p"] == 1


def test_identical_duplicates_do_not_raise_n_and_conflicting_ids_poison_the_arm():
    manifest = _manifest()
    rows = _outcomes(manifest)
    result = sc.assess_campaign(manifest, rows + [copy.deepcopy(rows[0])], now_ms=NOW)
    assert result["arms"][0]["score"]["n"] == 60
    assert result["arms"][0]["duplicates_ignored"] == 1
    duplicate = {**rows[0], "exit_credit_native": 1_500_000}
    result = sc.assess_campaign(manifest, rows + [duplicate], now_ms=NOW)
    assert result["arms"][0]["status"] == "UNPROVEN"
    duplicate = {**rows[0], "outcome_id": "different-id-same-signal"}
    result = sc.assess_campaign(manifest, rows + [duplicate], now_ms=NOW)
    assert result["arms"][0]["status"] == "UNPROVEN"


def test_aging_out_complete_rows_and_refreshing_timestamps_cannot_advance_readiness():
    manifest = _manifest()
    rows = _outcomes(manifest)
    initial = sc.assess_campaign(manifest, rows, now_ms=NOW)
    refresh = [{**row, "observed_ms": NOW + DAY - 2_000} for row in rows]
    unchanged = sc.assess_campaign(manifest, refresh, now_ms=NOW + DAY - 2_000,
                                   prior_state=initial["next_state"])
    assert all(a["streak"] == 0 for a in unchanged["arms"])
    missing = [r for r in rows if not r["outcome_id"].endswith(":p0")]
    missing += [_row(manifest, a, 61, NOW + DAY - 3_000) for a in manifest["hypotheses"]]
    result = sc.assess_campaign(manifest, missing, now_ms=NOW + DAY,
                                prior_state=initial["next_state"])
    assert all(a["status"] == "UNPROVEN" and a["streak"] == 0 for a in result["arms"])


def test_stale_ready_rows_revoke_readiness_and_same_date_never_counts_again():
    manifest = _manifest()
    rows, ready = _ready(manifest)
    now = NOW + 2 * DAY
    repeated = sc.assess_campaign(manifest, rows, now_ms=now + 1_000, prior_state=ready["next_state"])
    assert all(a["streak"] == 3 for a in repeated["arms"])
    stale = sc.assess_campaign(manifest, rows, now_ms=now + sc.EVIDENCE_MAX_AGE_MS + 1,
                               prior_state=ready["next_state"])
    assert all(a["status"] == "UNPROVEN" and a["streak"] == 0 for a in stale["arms"])


def test_changed_cashflow_under_same_id_cannot_reuse_readiness():
    manifest = _manifest()
    rows, ready = _ready(manifest)
    rows[0]["exit_credit_native"] += 1
    result = sc.assess_campaign(manifest, rows, now_ms=NOW + 2 * DAY, prior_state=ready["next_state"])
    assert result["arms"][0]["status"] == "UNPROVEN"


def test_backfilled_ids_below_previous_close_frontier_cannot_count_as_a_new_date():
    manifest = _manifest()
    rows = _outcomes(manifest)
    first = sc.assess_campaign(manifest, rows, now_ms=NOW)
    rows += [_row(manifest, a, 999, NOW - 100_000) for a in manifest["hypotheses"]]
    result = sc.assess_campaign(manifest, rows, now_ms=NOW + DAY - 2_000,
                                prior_state=first["next_state"])
    assert all(a["streak"] == 0 for a in result["arms"])


def test_zero_exposure_refusals_preserve_coverage_but_unknown_entered_inventory_blocks():
    manifest = _manifest()
    rows = _outcomes(manifest)
    refusal = _row(manifest, manifest["hypotheses"][0], 888, NOW - 3_000)
    refusal.update(status="refused", no_position_created=True, entry_debit_native=0, exit_credit_native=0)
    result = sc.assess_campaign(manifest, rows + [refusal], now_ms=NOW)
    arm = result["arms"][0]
    assert arm["status"] == "PASS" and arm["known_unfilled"] == 1 and arm["coverage_count"] == 61
    assert arm["returns_conditional_on_filled"] is True and arm["score"]["n"] == 60
    refusal["no_position_created"] = False
    result = sc.assess_campaign(manifest, rows + [refusal], now_ms=NOW)
    assert result["arms"][0]["status"] == "UNPROVEN"


def test_family_correction_is_load_bearing_and_includes_all_frozen_arms(monkeypatch):
    manifest = _manifest()
    # A valid pass-line PASS with p=.06 would pass alone at q=.10, but cannot pass
    # as the only profitable arm among four (.06 > .10/4).
    rows = _outcomes(manifest)
    for row in rows:
        if row["hypothesis_id"] != manifest["hypotheses"][0]["hypothesis_id"]:
            row["exit_credit_native"] = 800_000
    monkeypatch.setattr(ss, "p_mean_positive", lambda xs: .06 if xs and sum(xs) > 0 else 1.)
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    assert result["family_size"] == 4
    assert result["arms"][0]["score"]["verdict"] == "PASS"
    assert result["arms"][0]["status"] == "UNCORRECTED" and result["arms"][0]["streak"] == 0


def test_changed_frozen_spec_or_evaluator_source_invalidates_old_evidence(monkeypatch):
    manifest = _manifest()
    rows = _outcomes(manifest)
    mutated = copy.deepcopy(manifest)
    mutated["hypotheses"][0]["spec"]["exit"]["hold_ms"] = 1
    assert not sc.assess_campaign(mutated, rows, now_ms=NOW)["manifest_valid"]
    monkeypatch.setattr(sc, "source_versions", lambda: {"evaluator": "new"})
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    assert all(a["status"] == "UNPROVEN" for a in result["arms"])


def test_challengers_compare_identical_signals_and_missing_incumbent_is_unproven():
    manifest = _manifest()
    rows = _outcomes(manifest)
    incumbent_id = manifest["hypotheses"][0]["hypothesis_id"]
    for row in rows:
        if row["hypothesis_id"] == incumbent_id:
            row["exit_credit_native"] = 900_000
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    challenger = result["arms"][1]
    assert challenger["comparison"]["paired_closed_count"] == 60
    assert challenger["comparison"]["mean_delta_net"] == pytest.approx(.3)
    assert challenger["status"] == "PASS"
    result = sc.assess_campaign(manifest, [r for r in rows if r["hypothesis_id"] != incumbent_id], now_ms=NOW)
    assert all(a["status"] == "UNPROVEN" for a in result["arms"])
    assert result["arms"][1]["comparison"]["candidate_only_closed_count"] == 60


def test_trained_filter_readiness_uses_the_same_signal_subset_of_the_incumbent():
    manifest = sc.generate_campaign(_history(edge=.45), sa.ExitLadder(), now_ms=FROZEN,
                                    max_candidates=2, trained_only=True)
    assert [h["kind"] for h in manifest["hypotheses"]] == ["control", "trained"]
    rows = _outcomes(manifest, n=100)
    control_id = manifest["hypotheses"][0]["hypothesis_id"]
    # Only half the shared candidates match the learned entry band. The incumbent
    # saw all 100; the candidate saw the same corresponding 50, not unrelated winners.
    rows = [r for r in rows if r["hypothesis_id"] == control_id
            or int(r["signal_id"].removeprefix("signal")) % 2 == 0]
    result = sc.assess_campaign(manifest, rows, now_ms=NOW)
    candidate = result["arms"][1]
    assert candidate["complete_count"] == candidate["comparison"]["paired_closed_count"] == 50
    assert candidate["comparison"]["complete"] and candidate["status"] == "PASS"
    assert candidate["comparison"]["mean_delta_net"] == 0


def test_reconstruction_helpers_reuse_the_existing_policy_and_filter_contract():
    policy = sc.exploration_policies(sa.ExitLadder())[1]
    collector = ss.PathCollector([policy])
    assert policy.key in collector.policies
    assert dataclasses.asdict(ss.ExitPolicy.from_spec(policy.spec()).ladder) == dataclasses.asdict(policy.ladder)
    h = {"spec": {"lane": "sm-trenches", "chain": "sol", "filter": [ss.Cond("buy_share_30m", .5, .75).spec()]}}
    assert sc.matches_hypothesis(h, "sm-trenches", "sol", {"buy_share_30m": .6})
    assert not sc.matches_hypothesis(h, "sm-trenches", "sol", {"buy_share_30m": None})
