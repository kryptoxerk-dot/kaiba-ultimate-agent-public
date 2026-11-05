"""The runner-hunt wallet cohort (kaiba/learning/runner_cohort.py) and confluence-5 following it.

Owner 2026-10-20: "gather really good wallets and see what the agent can do after". The lists
were chosen on runners these wallets ALREADY bought, so the only honest test is forward. What
must hold, each written as the failure it prevents:

* a replay never counts a wallet listed after the moment it replays (lookahead);
* a stale freeze (the refresh stopped) means silence, not an old list;
* a chain with no export is reported, never frozen EMPTY ("nobody is good" is not what a
  missing file says);
* EVM addresses are normalised, so a checksummed export still matches the lower-case tape;
* the proven route is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import lanes
from kaiba.learning import proven as P
from kaiba.learning import runner_cohort as RC
from tests.test_confluence_proven import A, B, C, F, G
from tests.test_lanes import NOW, build_ctx, load_fixture

RUNNERS = {"wallet_source": "runners", "min_entities": 3, "max_signal_age_s": 60, "min_buy_usd": 50}


@pytest.fixture(autouse=True)
def _fresh_cache():
    RC.clear_cache()
    P.clear_cache()
    yield
    RC.clear_cache()
    P.clear_cache()


def _members(*addrs: str) -> dict[str, dict]:
    return {a: {"labels": ["SNIPE test"], "lists": ["kaiba-solana-x.txt"]} for a in addrs}


def _freeze(conn, addrs, *, at_ms: int = NOW - 3_600_000, chain: Chain = Chain.SOL) -> str:
    cid = RC.freeze(conn, chain, _members(*addrs), frozen_ms=at_ms, source="test")
    RC.clear_cache()
    return cid


def _ctx(conn, params=None, **kw):
    return build_ctx(conn, load_fixture("confluence_5"), params={**RUNNERS, **(params or {})}, **kw)


def test_runner_wallets_fire_the_lane_and_say_where_they_came_from(tmp_db):
    cid = _freeze(tmp_db, [A, B, F, G])
    signal = lanes.confluence_5(_ctx(tmp_db))
    assert signal is not None and signal.wallets == sorted([A, B, F])  # G is a $12 dust buy
    assert signal.payload["wallet_source"] == "runners" and signal.payload["cohort_id"] == cid
    assert "runner-hunt" in signal.payload["evidence"] and "forward" in signal.payload["evidence"]


def test_a_replay_never_counts_a_wallet_listed_after_it(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW + 60_000)
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_an_older_freeze_is_used_for_a_moment_before_the_newest(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW - 7_200_000)
    _freeze(tmp_db, [A], at_ms=NOW + 60_000)  # listed later: invisible to NOW
    assert lanes.confluence_5(_ctx(tmp_db)) is not None


def test_a_stale_freeze_means_silence(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW - 4 * 86_400_000)
    assert lanes.confluence_5(_ctx(tmp_db)) is None
    assert lanes.confluence_5(_ctx(tmp_db, {"runners_max_cohort_age_s": 5 * 86_400})) is not None


def test_proven_and_runner_cohorts_never_mix(tmp_db):
    _freeze(tmp_db, [A, B, F])
    proven = build_ctx(tmp_db, load_fixture("confluence_5"),
                       params={**RUNNERS, "wallet_source": "proven"})
    assert lanes.confluence_5(proven) is None, "a runner freeze is not a proven cohort"


def test_exports_are_read_per_chain_and_evm_is_lower_cased(tmp_path):
    (tmp_path / "kaiba-solana-snipers.txt").write_text(json.dumps(
        [{"address": A, "name": "SNIPE 3run"}, {"address": B, "name": "EARLY 2run"}]), encoding="utf-8")
    (tmp_path / "kaiba-solana-hightier.txt").write_text(json.dumps([{"address": A, "name": "A tier"}]),
                                                         encoding="utf-8")
    (tmp_path / "kaiba-bnb-snipers.txt").write_text(json.dumps(
        [{"address": "0xAbCdEf0000000000000000000000000000000001", "name": "SNIPE"}]), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a list", encoding="utf-8")
    got = RC.read_exports(tmp_path)
    assert set(got[Chain.SOL]) == {A, B} and len(got[Chain.SOL][A]["lists"]) == 2
    assert set(got[Chain.BSC]) == {"0xabcdef0000000000000000000000000000000001"}
    assert Chain.ROBINHOOD not in got


def test_refresh_reports_a_missing_chain_instead_of_freezing_it_empty(tmp_db, tmp_path):
    (tmp_path / "kaiba-solana-snipers.txt").write_text(json.dumps([{"address": A}, {"address": C}]),
                                                       encoding="utf-8")
    out = RC.refresh(tmp_db, tmp_path, now_ms=NOW - 1_000)
    assert out["sol"]["members"] == 2 and out["sol"]["new_since_last"] == 2
    assert out["bsc"] == {"skipped": "no export list"} and out["robinhood"] == {"skipped": "no export list"}
    assert RC.latest_runner_cohort(tmp_db, Chain.BSC) is None
    again = RC.refresh(tmp_db, tmp_path, now_ms=NOW)
    assert again["sol"]["new_since_last"] == 0


def test_the_shipped_config_runs_the_freeze_daily_and_scores_the_lane():
    from kaiba.ops import scheduler as S

    root = Path(__file__).resolve().parents[1]
    cfg = S.load_config(root / "config" / "schedule.yaml")
    assert cfg.jobs["runner_cohort"].enabled and cfg.jobs["runner_cohort"].interval_s == 86_400
    assert "runner_cohort" in S.JOBS
    assert "confluence-5" in cfg.jobs["sprint_scorecard"].params.get("extra_lanes", "")
    assert "confluence-5" in cfg.jobs["strategy_search"].params.get("lanes", "")


def test_extra_lanes_are_scored_through_the_same_pass_line():
    from types import SimpleNamespace

    from kaiba.learning import sprint
    from kaiba.ops import scheduler as S

    start, now = NOW - 12 * 86_400_000, NOW
    cases = [SimpleNamespace(lane="confluence-5", chain="sol", t0=start + i * 3_600_000, outcome=0.2)
             for i in range(60)]
    cases += [SimpleNamespace(lane="sm-trenches", chain="sol", t0=start + 10, outcome=-0.5)]
    (score,) = S._extra_lane_scores(cases, ["confluence-5", "sm-trenches"], start, now)
    assert score.lane == "confluence-5" and score.n == 60
    assert abs(score.mean - sprint.net(0.2, "sol")) < 1e-9
