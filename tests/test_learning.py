"""The learning loop: metrics, the evolving playbook, reflection, and the promotion gates.

The tests that matter most here are the ones that prove the *refusals*. An agent that can
talk its way past its own evaluator has no evaluator, so this file spends more effort on
rejecting a malformed reflection, refusing a promotion and leaving the operator's bounds
alone than it does on the happy paths.

Fixtures are hand-computable on purpose: every expected number below can be worked out
with a pencil from the rows the helper inserts.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kaiba.core import journal
from kaiba.core.config import load_risk
from kaiba.core.schemas import Lane, now_ms
from kaiba.learning import gates, metrics, playbook, reflect

BASE_MS = 1_700_000_000_000
DAY_MS = 86_400_000
SOL_TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
SOL_TOKEN_B = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"


# ---------------------------------------------------------------------------- fixtures


def insert_trade(
    conn,
    trade_id: str,
    *,
    lane: str = "confluence-5",
    mode: str = "shadow",
    chain: str = "sol",
    token: str = SOL_TOKEN,
    opened_ms: int = BASE_MS,
    closed_ms: int | None = None,
    hold_s: int = 60,
    cost: int = 1000,
    pnl: int = 100,
    decision_id: str | None = None,
    mistakes: tuple[str, ...] = (),
    lesson: str | None = None,
    params_version: str = "v1",
) -> str:
    closed = closed_ms if closed_ms is not None else opened_ms + hold_s * 1000
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, "
        "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct, "
        "fees_native, mistakes_json, lesson, params_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id,
            f"pos_{trade_id}",
            decision_id,
            lane,
            mode,
            chain,
            token,
            opened_ms,
            closed,
            hold_s,
            str(cost),
            str(cost + pnl),
            str(pnl),
            (pnl / cost * 100) if cost else 0.0,
            "0",
            json.dumps(list(mistakes)),
            lesson,
            params_version,
        ),
    )
    return trade_id


def insert_decision(
    conn,
    decision_id: str,
    *,
    lane: str = "confluence-5",
    mode: str = "shadow",
    chain: str = "sol",
    token: str = SOL_TOKEN,
    action: str = "enter",
    ts_ms: int = BASE_MS,
    confidence: float = 0.6,
    signals: tuple[str, ...] = (),
    regime: str | None = "chop",
    thesis: str = "five independent entities bought inside the window",
    blockers: tuple[str, ...] = (),
) -> str:
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, thesis, "
        "confidence, signals_json, regime, blockers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            decision_id,
            ts_ms,
            lane,
            mode,
            chain,
            token,
            action,
            thesis,
            confidence,
            json.dumps(list(signals)),
            regime,
            json.dumps(list(blockers)),
        ),
    )
    return decision_id


def insert_signal(conn, signal_id: str, *, created_ms: int, payload: dict, lane: str = "confluence-5") -> str:
    conn.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms, payload_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (signal_id, lane, "sol", SOL_TOKEN, 0.5, created_ms, json.dumps(payload)),
    )
    return signal_id


def make_experiment(conn, exp_id: str, *, lane: str, key: str, old, new, status: str = "proposed") -> str:
    diff = {"lane": lane, "key": key, "old": old, "new": new, "rationale": "test"}
    conn.execute(
        "INSERT INTO experiments (experiment_id, created_ms, lane, hypothesis, diff_json, status) "
        "VALUES (?,?,?,?,?,?)",
        (exp_id, BASE_MS, lane, "test hypothesis", json.dumps(diff), status),
    )
    return exp_id


@pytest.fixture
def risk_file(tmp_path, monkeypatch) -> Path:
    """An isolated risk envelope so no test can touch the developer's real one."""
    path = tmp_path / "risk.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": "v1",
                "global_mode": "shadow",
                "bounds": {
                    "max_size_pct_bankroll": 5.0,
                    "max_daily_loss_pct": 10.0,
                    "max_slippage_bps": 2500,
                    "max_lane_mode": "shadow",
                    "allow_self_promotion": True,
                },
                "lanes": {"confluence-5": {"mode": "shadow", "params": {"min_entities": 3}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def build_replay_stream(conn, *, pattern: str, lane: str = "confluence-5", blocks: int = 8, per_block: int = 30) -> None:
    """Decisions + point-in-time signals + closed trades, laid out in equal time blocks.

    ``clean``   high-entity entries always win, low-entity ones always lose.
    ``overfit`` the sign flips every block, so whichever configuration looks best
                in-sample is the one that loses out-of-sample — the exact pathology CSCV
                is designed to catch.

    One entry every 10 hours, 240 by default (2026-10-02): a tightening is now judged on a
    day-clustered bound that needs >= 20 distinct days out of sample (the last 30% = 30
    days here) and >= 30 candidate trades there (36). The old 64 hourly rows put the whole
    out-of-sample window inside one day.
    """
    idx = 0
    for b in range(blocks):
        for j in range(per_block):
            entities = 6 if j % 2 == 0 else 3
            if pattern == "clean":
                pnl = 200 if entities == 6 else -250
            else:
                good_block = b % 2 == 0
                high_wins = good_block
                pnl = 300 if (entities == 6) == high_wins else -300
            ts = BASE_MS + idx * 10 * 3_600_000
            sid = f"sig_{idx}"
            insert_signal(conn, sid, created_ms=ts - 1000, payload={"entities": entities}, lane=lane)
            did = insert_decision(conn, f"dec_{idx}", lane=lane, ts_ms=ts, signals=(sid,))
            insert_trade(
                conn,
                f"trd_{idx}",
                lane=lane,
                opened_ms=ts,
                closed_ms=ts + 60_000,
                cost=1000,
                pnl=pnl,
                decision_id=did,
            )
            idx += 1


def build_shadow_arms(conn, exp_id: str, *, lane: str = "confluence-5", n: int = 60, days: int = 20) -> None:
    """Labelled candidate and incumbent shadow streams over the same window."""
    step = (days * DAY_MS) // (n - 1)
    for i in range(n):
        closed = BASE_MS + i * step
        for arm, pnl in (("candidate", 100), ("incumbent", -50)):
            tid = f"{arm}_{i}"
            insert_trade(
                conn, tid, lane=lane, opened_ms=closed - 60_000, closed_ms=closed, cost=1000, pnl=pnl
            )
            conn.execute(
                "INSERT INTO experiment_trades (experiment_id, trade_id, arm, linked_ms) VALUES (?,?,?,?)",
                (exp_id, tid, arm, closed),
            )


# =============================================================== metrics: the numbers


def test_expectancy_is_mean_pnl_in_base_units(tmp_db):
    trades = [{"pnl_native": "300", "cost_native": "1000"}, {"pnl_native": "-100", "cost_native": "1000"}]
    assert metrics.expectancy(trades) == Decimal(100)


def test_expectancy_of_no_trades_is_none_not_zero(tmp_db):
    assert metrics.expectancy([]) is None
    assert metrics.win_rate([]) is None
    assert metrics.profit_factor([]) is None


def test_win_rate_counts_only_positive_pnl(tmp_db):
    trades = [{"pnl_native": "1"}, {"pnl_native": "0"}, {"pnl_native": "-1"}, {"pnl_native": "5"}]
    assert metrics.win_rate(trades) == 0.5


def test_profit_factor_hand_computed(tmp_db):
    trades = [{"pnl_native": "300"}, {"pnl_native": "200"}, {"pnl_native": "-250"}]
    assert metrics.profit_factor(trades) == Decimal(2)


def test_profit_factor_is_capped_not_infinite(tmp_db):
    trades = [{"pnl_native": "500"}, {"pnl_native": "1"}]
    assert metrics.profit_factor(trades) == metrics.PROFIT_FACTOR_CAP == Decimal(10)


def test_trade_return_comes_from_base_units_not_the_float_column(tmp_db):
    trade = {"pnl_native": "1", "cost_native": "3", "pnl_pct": 33.33333}
    assert metrics.trade_return(trade) == Decimal(1) / Decimal(3)


def test_trade_return_falls_back_to_pct_when_no_cost(tmp_db):
    assert metrics.trade_return({"pnl_native": "5", "cost_native": "0", "pnl_pct": 25.0}) == Decimal("0.25")


def test_max_drawdown_hand_computed(tmp_db):
    curve = [(1, 0), (2, 100), (3, 40), (4, 90), (5, 10), (6, 60)]
    assert metrics.max_drawdown(curve) == Decimal(90)  # peak 100 -> trough 10
    assert metrics.max_drawdown_pct(curve) == pytest.approx(0.9)


def test_max_drawdown_of_empty_curve_is_none(tmp_db):
    assert metrics.max_drawdown([]) is None


def test_sharpe_and_sortino_on_a_known_series(tmp_db):
    # returns +10%, -10%, +10%, -10% -> mean 0 -> Sharpe 0, Sortino 0
    trades = [{"pnl_native": str(p), "cost_native": "1000"} for p in (100, -100, 100, -100)]
    assert metrics.sharpe(trades, periods_per_year=1.0) == pytest.approx(0.0)
    assert metrics.sortino(trades, periods_per_year=1.0) == pytest.approx(0.0)


def test_sortino_is_none_when_nothing_went_down(tmp_db):
    trades = [{"pnl_native": "100", "cost_native": "1000"}, {"pnl_native": "200", "cost_native": "1000"}]
    assert metrics.sortino(trades) is None


def test_avg_hold_and_turnover(tmp_db):
    trades = [
        {"hold_s": 60, "cost_native": "1000", "pnl_native": "0"},
        {"hold_s": 180, "cost_native": "3000", "pnl_native": "0"},
    ]
    assert metrics.avg_hold_s(trades) == 120
    assert metrics.turnover(trades) == Decimal(4000)
    assert metrics.turnover(trades, bankroll_base_units=2000) == Decimal(2)


# ------------------------------------------------------------------------- calibration


CALIBRATED = [(0.8, True)] * 8 + [(0.8, False)] * 2 + [(0.2, True)] * 2 + [(0.2, False)] * 8
OVERCONFIDENT = [(0.99, True)] * 8 + [(0.99, False)] * 2 + [(0.01, True)] * 2 + [(0.01, False)] * 8


def test_brier_score_of_a_perfectly_calibrated_agent(tmp_db):
    # 8*(0.2^2) + 2*(0.8^2) + 2*(0.8^2) + 8*(0.2^2) = 3.2 over 20 = 0.16
    assert metrics.brier_score(CALIBRATED) == pytest.approx(0.16)


def test_overconfident_agent_scores_worse_than_calibrated(tmp_db):
    assert metrics.brier_score(OVERCONFIDENT) == pytest.approx(0.1961)
    assert metrics.brier_score(OVERCONFIDENT) > metrics.brier_score(CALIBRATED)


def test_brier_skill_score_rewards_calibration_over_confidence(tmp_db):
    # base rate is 0.5, so the reference Brier score is 0.25
    assert metrics.brier_skill_score(CALIBRATED) == pytest.approx(1 - 0.16 / 0.25)
    assert metrics.brier_skill_score(CALIBRATED) > metrics.brier_skill_score(OVERCONFIDENT)


def test_coin_flip_confidence_has_zero_skill(tmp_db):
    pairs = [(0.5, True)] * 5 + [(0.5, False)] * 5
    assert metrics.brier_score(pairs) == pytest.approx(0.25)
    assert metrics.brier_skill_score(pairs) == pytest.approx(0.0)


def test_brier_on_no_data_is_none(tmp_db):
    assert metrics.brier_score([]) is None
    assert metrics.brier_skill_score([]) is None


def test_calibration_buckets_report_stated_versus_realised(tmp_db):
    buckets = metrics.calibration_buckets(CALIBRATED, bins=5)
    top = [b for b in buckets if b["n"] and b["stated"] == 0.8][0]
    assert top["realised"] == pytest.approx(0.8)


# ------------------------------------------------------------------------- attribution


def _beta_trades(returns: list[Decimal]) -> list[dict]:
    return [
        {"trade_id": f"t{i}", "pnl_native": str(int(r * 1000)), "cost_native": "1000"}
        for i, r in enumerate(returns)
    ]


BENCH = [Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4")]


def test_attribution_finds_pure_beta_and_no_residual(tmp_db):
    trades = _beta_trades([b * 2 for b in BENCH])
    out = metrics.attribution(trades, {f"t{i}": b for i, b in enumerate(BENCH)})
    assert out["beta"] == Decimal(2)
    assert out["residual_component"] == Decimal(0)
    assert out["beta_component"] == Decimal(2)
    assert out["r_squared"] == pytest.approx(1.0)


def test_attribution_separates_a_constant_edge_from_beta(tmp_db):
    trades = _beta_trades([b + Decimal("0.05") for b in BENCH])
    out = metrics.attribution(trades, [b for b in BENCH])
    assert out["beta"] == Decimal(1)
    assert out["alpha_per_trade"] == Decimal("0.05")
    assert out["beta_component"] == Decimal("1.0")
    assert out["residual_component"] == Decimal("0.2")  # 4 trades * 0.05


def test_attribution_refuses_to_invent_beta_without_a_varying_benchmark(tmp_db):
    trades = _beta_trades([Decimal("0.1")] * 4)
    out = metrics.attribution(trades, [Decimal("0.1")] * 4)
    assert out["basis"] == "unavailable"
    assert "beta" not in out


# --------------------------------------------------------------------------- rollups


def test_by_lane_groups_and_scores_each_lane(tmp_db):
    insert_trade(tmp_db, "a", lane="confluence-5", pnl=300, closed_ms=BASE_MS + 1)
    insert_trade(tmp_db, "b", lane="confluence-5", pnl=-100, closed_ms=BASE_MS + 2)
    insert_trade(tmp_db, "c", lane="kol-fade", pnl=-50, closed_ms=BASE_MS + 3)
    stats = metrics.by_lane(tmp_db, 0, "shadow")
    assert stats[Lane.CONFLUENCE_5].trades == 2
    assert stats[Lane.CONFLUENCE_5].expectancy == Decimal(100)
    assert stats[Lane.KOL_FADE].wins == 0


def test_by_regime_reads_the_regime_recorded_at_decision_time(tmp_db):
    d1 = insert_decision(tmp_db, "d1", regime="trend", ts_ms=BASE_MS)
    d2 = insert_decision(tmp_db, "d2", regime="chop", ts_ms=BASE_MS)
    insert_trade(tmp_db, "t1", decision_id=d1, pnl=500, closed_ms=BASE_MS + 1)
    insert_trade(tmp_db, "t2", decision_id=d2, pnl=-200, closed_ms=BASE_MS + 2)
    stats = metrics.by_regime(tmp_db, 0, "shadow")
    assert stats["trend"].expectancy == Decimal(500)
    assert stats["chop"].expectancy == Decimal(-200)


def test_by_mistake_tag_counts_each_tag(tmp_db):
    insert_trade(tmp_db, "m1", pnl=-100, mistakes=("late_entry", "size_too_big"), closed_ms=BASE_MS + 1)
    insert_trade(tmp_db, "m2", pnl=-300, mistakes=("late_entry",), closed_ms=BASE_MS + 2)
    stats = metrics.by_mistake_tag(tmp_db, 0, "shadow")
    assert stats["late_entry"].count == 2
    assert stats["late_entry"].expectancy == Decimal(-200)
    assert stats["size_too_big"].count == 1


def test_equity_curve_is_cumulative_pnl_per_closed_trade(tmp_db):
    insert_trade(tmp_db, "e1", pnl=100, closed_ms=BASE_MS + 1)
    insert_trade(tmp_db, "e2", pnl=-40, closed_ms=BASE_MS + 2)
    insert_trade(tmp_db, "e3", pnl=10, closed_ms=BASE_MS + 3)
    assert metrics.equity_curve(tmp_db, "shadow", "sol") == [
        (BASE_MS + 1, 100),
        (BASE_MS + 2, 60),
        (BASE_MS + 3, 70),
    ]


# ============================================================ playbook: ACE-style rules


def test_add_rule_returns_an_id_and_shows_up_active(tmp_db):
    rid = playbook.add_rule("Skip tokens whose top-10 exceeds 35 percent.", lane=Lane.CONFLUENCE_5, conn=tmp_db)
    rules = playbook.active_rules(Lane.CONFLUENCE_5, tmp_db)
    assert [r["rule_id"] for r in rules] == [rid]
    assert rules[0]["hits"] == 0 and rules[0]["misses"] == 0


def test_add_rule_is_idempotent_by_content(tmp_db):
    a = playbook.add_rule("Never hold through migration.", conn=tmp_db)
    b = playbook.add_rule("Never   hold through migration.", conn=tmp_db)
    assert a == b
    assert len(playbook.active_rules(None, tmp_db)) == 1


def test_hit_and_miss_counters_accumulate(tmp_db):
    rid = playbook.add_rule("Fade KOL calls older than 90 seconds.", conn=tmp_db)
    playbook.record_hit(rid, conn=tmp_db)
    playbook.record_hit(rid, conn=tmp_db)
    playbook.record_miss(rid, conn=tmp_db)
    rule = playbook.get_rule(rid, tmp_db)
    assert (rule["hits"], rule["misses"]) == (2, 1)


def test_active_rules_are_sorted_by_smoothed_hit_rate(tmp_db):
    lucky = playbook.add_rule("One lucky rule.", conn=tmp_db)
    proven = playbook.add_rule("A proven rule.", conn=tmp_db)
    playbook.record_hit(lucky, conn=tmp_db)
    for _ in range(40):
        playbook.record_hit(proven, conn=tmp_db)
    playbook.record_miss(proven, conn=tmp_db)
    order = [r["rule_id"] for r in playbook.active_rules(None, tmp_db)]
    assert order[0] == proven  # 40/41 must outrank 1/1


def test_recording_against_an_unknown_rule_is_refused(tmp_db):
    assert playbook.record_hit("pb_nonexistent", conn=tmp_db) is False


def test_retire_is_a_status_change_with_a_journal_entry(tmp_db):
    rid = playbook.add_rule("A rule that stops working.", conn=tmp_db)
    assert playbook.retire(rid, "superseded", conn=tmp_db) is True
    row = tmp_db.execute("SELECT * FROM playbook WHERE rule_id = ?", (rid,)).fetchone()
    assert row is not None and row["status"] == "retired"
    assert any("retired" in e["body"] for e in journal.read(50, kind="change", conn=tmp_db))
    assert playbook.active_rules(None, tmp_db) == []


def test_expire_stale_retires_rather_than_deletes(tmp_db):
    stale = playbook.add_rule("Stale rule nobody confirmed.", conn=tmp_db)
    fresh = playbook.add_rule("Fresh rule with a recent hit.", conn=tmp_db)
    playbook.record_hit(fresh, conn=tmp_db)
    future = now_ms() + 31 * DAY_MS
    tmp_db.execute("UPDATE playbook_hits SET ts_ms = ? WHERE rule_id = ?", (future - DAY_MS, fresh))

    retired = playbook.expire_stale(tmp_db, max_age_days=30, now=future)

    assert retired == [stale]
    assert tmp_db.execute("SELECT COUNT(*) FROM playbook").fetchone()[0] == 2  # nothing deleted
    assert [r["rule_id"] for r in playbook.active_rules(None, tmp_db)] == [fresh]
    assert tmp_db.execute(
        "SELECT reason FROM playbook_retirements WHERE rule_id = ?", (stale,)
    ).fetchone()["reason"] == "no hit in 30d"


def test_a_retired_rule_can_be_reconfirmed(tmp_db):
    rid = playbook.add_rule("Sell half at 2x.", conn=tmp_db)
    playbook.retire(rid, "stale", conn=tmp_db)
    again = playbook.add_rule("Sell half at 2x.", evidence=["trd_1"], conn=tmp_db)
    assert again == rid
    assert [r["rule_id"] for r in playbook.active_rules(None, tmp_db)] == [rid]


def test_render_for_prompt_shows_counters(tmp_db):
    rid = playbook.add_rule("Require two independent entities.", lane=Lane.SM_TRENCHES, conn=tmp_db)
    playbook.record_hit(rid, conn=tmp_db)
    playbook.record_miss(rid, conn=tmp_db)
    text = playbook.render_for_prompt(Lane.SM_TRENCHES, conn=tmp_db)
    assert "1h/1m" in text and "sm-trenches" in text and text.startswith("PLAYBOOK")


def test_render_for_prompt_respects_the_char_cap(tmp_db):
    for i in range(30):
        playbook.add_rule(f"Rule number {i} " + "x" * 200, conn=tmp_db)
    text = playbook.render_for_prompt(None, max_chars=400, conn=tmp_db)
    assert len(text) <= 400
    assert "omitted (char cap)" in text


def test_render_for_prompt_says_so_when_empty(tmp_db):
    assert "empty" in playbook.render_for_prompt(None, conn=tmp_db)


# ================================================================ reflect: the packet


def test_build_review_includes_every_skip(tmp_db):
    insert_decision(tmp_db, "s1", action="skip", ts_ms=BASE_MS, blockers=("top10_concentration",))
    insert_decision(tmp_db, "s2", action="skip", ts_ms=BASE_MS + 10, token=SOL_TOKEN_B)
    insert_decision(tmp_db, "e1", action="enter", ts_ms=BASE_MS + 20)
    insert_trade(tmp_db, "t1", decision_id="e1", closed_ms=BASE_MS + 30, pnl=50)

    packet = reflect.build_review(tmp_db, 0, BASE_MS + 10_000)

    assert {s.decision_id for s in packet.skips} == {"s1", "s2"}
    assert packet.counts == {"trades": 1, "skips": 2}
    assert packet.skips[0].blockers == ["top10_concentration"]


def test_skip_outcome_is_unavailable_when_nothing_observed_it(tmp_db):
    insert_decision(tmp_db, "s1", action="skip", ts_ms=BASE_MS, token=SOL_TOKEN_B)
    packet = reflect.build_review(tmp_db, 0, BASE_MS + 10_000)
    assert packet.skips[0].outcome_basis == "unavailable"
    assert packet.skips[0].subsequent_return_frac is None


def test_skip_outcome_uses_a_later_recorded_trade_when_we_have_one(tmp_db):
    insert_decision(tmp_db, "s1", action="skip", ts_ms=BASE_MS, token=SOL_TOKEN_B)
    insert_trade(tmp_db, "later", token=SOL_TOKEN_B, opened_ms=BASE_MS + 60_000, cost=1000, pnl=400)
    packet = reflect.build_review(tmp_db, 0, BASE_MS + 10_000)
    assert packet.skips[0].outcome_basis == "observed_trade"
    assert packet.skips[0].subsequent_return_frac == pytest.approx(0.4)


def test_mask_replaces_token_identities_stably(tmp_db):
    insert_decision(tmp_db, "e1", action="enter", ts_ms=BASE_MS)
    insert_trade(tmp_db, "t1", decision_id="e1", token=SOL_TOKEN, closed_ms=BASE_MS + 1)
    insert_trade(tmp_db, "t2", token=SOL_TOKEN_B, closed_ms=BASE_MS + 2)
    packet = reflect.build_review(tmp_db, 0, BASE_MS + 10_000)

    masked = reflect.mask(packet)
    again = reflect.mask(packet)

    tokens = {t.token for t in masked.packet.trades}
    assert tokens == {"TOKEN_A", "TOKEN_B"}
    assert SOL_TOKEN not in json.dumps(masked.packet.model_dump(mode="json"))
    assert masked.mapping == again.mapping  # stable across runs
    assert masked.packet.masked is True


def test_unmask_puts_the_real_address_back(tmp_db):
    insert_trade(tmp_db, "t1", token=SOL_TOKEN, closed_ms=BASE_MS + 1)
    masked = reflect.mask(reflect.build_review(tmp_db, 0, BASE_MS + 10_000))
    assert reflect.unmask_text("TOKEN_A rugged", masked) == f"sol:{SOL_TOKEN} rugged"


def test_packet_strips_control_characters_from_free_text(tmp_db):
    insert_decision(tmp_db, "s1", action="skip", ts_ms=BASE_MS, thesis="line one\x00\nline two")
    packet = reflect.build_review(tmp_db, 0, BASE_MS + 10_000)
    assert packet.skips[0].thesis == "line one line two"


# ---------------------------------------------------- reflect: validating the response


def _valid_payload(**over) -> dict:
    payload = {
        "lessons": [{"text": "Entries after 90s of curve age lost money.", "mistake_tag": "late_entry"}],
        "playbook_deltas": [{"op": "add", "text": "Do not enter after 90s of curve age."}],
        "param_proposals": [
            {"lane": "confluence-5", "key": "min_entities", "old": 3, "new": 5, "rationale": "fewer, better"}
        ],
    }
    payload.update(over)
    return payload


def test_a_valid_reflection_parses(tmp_db):
    result = reflect.parse_reflection(_valid_payload())
    assert result.lessons[0].mistake_tag == "late_entry"
    assert result.param_proposals[0].lane is Lane.CONFLUENCE_5


def test_reflection_rejects_an_out_of_vocabulary_mistake_tag(tmp_db):
    payload = _valid_payload(lessons=[{"text": "We were unlucky.", "mistake_tag": "bad_vibes"}])
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(payload)
    assert any("vocabulary" in r for r in err.value.reasons)


def test_reflection_rejects_an_over_long_lesson(tmp_db):
    payload = _valid_payload(lessons=[{"text": "x" * 281, "mistake_tag": "thesis_wrong"}])
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(payload)
    assert any("281 chars" in r for r in err.value.reasons)


def test_reflection_rejects_more_than_five_lessons(tmp_db):
    lessons = [{"text": f"Lesson {i}.", "mistake_tag": "data_error"} for i in range(6)]
    with pytest.raises(reflect.ReflectionRejected):
        reflect.parse_reflection(_valid_payload(lessons=lessons))


def test_reflection_rejects_a_third_param_proposal(tmp_db):
    props = [
        {"lane": "confluence-5", "key": f"min_thing_{i}", "old": 1, "new": 2, "rationale": "r"}
        for i in range(3)
    ]
    with pytest.raises(reflect.ReflectionRejected):
        reflect.parse_reflection(_valid_payload(param_proposals=props))


def test_reflection_rejects_a_malformed_param_proposal(tmp_db):
    bad = [{"lane": "confluence-5", "new": 5, "rationale": "no key given"}]
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(_valid_payload(param_proposals=bad))
    assert any("key" in r for r in err.value.reasons)


def test_reflection_refuses_to_propose_an_operator_owned_key(tmp_db):
    bad = [
        {
            "lane": "confluence-5",
            "key": "max_size_pct_bankroll",
            "old": 5.0,
            "new": 50.0,
            "rationale": "more size",
        }
    ]
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(_valid_payload(param_proposals=bad))
    assert any("operator-owned" in r for r in err.value.reasons)


def test_reflection_rejects_an_unknown_lane(tmp_db):
    bad = [{"lane": "not-a-lane", "key": "min_entities", "old": 3, "new": 5, "rationale": "r"}]
    with pytest.raises(reflect.ReflectionRejected):
        reflect.parse_reflection(_valid_payload(param_proposals=bad))


def test_a_truncated_response_is_rejected_not_repaired(tmp_db):
    truncated = '{"lessons": [{"text": "We entered la'
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(truncated)
    assert any("not valid JSON" in r for r in err.value.reasons)


def test_unexpected_top_level_keys_are_rejected(tmp_db):
    with pytest.raises(reflect.ReflectionRejected) as err:
        reflect.parse_reflection(_valid_payload(apply_immediately=True))
    assert any("apply_immediately" in r for r in err.value.reasons)


# --------------------------------------------------------- reflect: applying a result


def test_apply_reflection_writes_lessons_rules_and_proposals(tmp_db, risk_file):
    result = reflect.parse_reflection(_valid_payload())
    report = reflect.apply_reflection(result, tmp_db)

    assert report.lessons_written == 1
    assert len(report.rules_added) == 1
    assert len(report.experiments_created) == 1
    lesson = journal.read(10, kind="lesson", conn=tmp_db)[0]
    assert lesson["body"].startswith("[late_entry]")
    row = tmp_db.execute(
        "SELECT * FROM experiments WHERE experiment_id = ?", (report.experiments_created[0],)
    ).fetchone()
    assert row["status"] == "proposed"
    assert json.loads(row["diff_json"])["new"] == 5


def test_apply_reflection_never_writes_to_risk_yaml(tmp_db, risk_file):
    before = risk_file.read_bytes()
    reflect.apply_reflection(reflect.parse_reflection(_valid_payload()), tmp_db)
    assert risk_file.read_bytes() == before
    assert load_risk(risk_file).lane(Lane.CONFLUENCE_5).params["min_entities"] == 3


def test_apply_reflection_requires_a_validated_result(tmp_db):
    with pytest.raises(reflect.ReflectionRejected):
        reflect.apply_reflection({"lessons": []}, tmp_db)  # type: ignore[arg-type]


def test_apply_reflection_retires_a_rule_and_notes_unknown_ones(tmp_db, risk_file):
    rid = playbook.add_rule("A rule the model wants gone.", conn=tmp_db)
    payload = _valid_payload(
        playbook_deltas=[
            {"op": "retire", "rule_id": rid, "reason": "0 hits in 40 trades"},
            {"op": "retire", "rule_id": "pb_ghost", "reason": "never existed"},
        ]
    )
    report = reflect.apply_reflection(reflect.parse_reflection(payload), tmp_db)
    assert report.rules_retired == [rid]
    assert any("pb_ghost" in s for s in report.skipped)


def test_apply_reflection_unmasks_token_pseudonyms(tmp_db, risk_file):
    insert_trade(tmp_db, "t1", token=SOL_TOKEN, closed_ms=BASE_MS + 1)
    masked = reflect.mask(reflect.build_review(tmp_db, 0, BASE_MS + 10_000))
    payload = _valid_payload(
        lessons=[{"text": "TOKEN_A rugged after migration.", "mistake_tag": "held_through_migration"}]
    )
    reflect.apply_reflection(reflect.parse_reflection(payload), tmp_db, masked=masked)
    assert SOL_TOKEN in journal.read(10, kind="lesson", conn=tmp_db)[0]["body"]


def test_rejected_runs_are_recorded_with_their_reason(tmp_db):
    packet = reflect.build_review(tmp_db, 0, BASE_MS)
    run_id = reflect.record_run(packet, None, tmp_db)
    reflect.reject_run(run_id, ["lesson too long"], tmp_db)
    row = tmp_db.execute("SELECT * FROM reflection_runs WHERE run_id = ?", (run_id,)).fetchone()
    assert row["status"] == "rejected" and "too long" in row["reason"]


# ==================================================================== gates: PBO / DSR


def test_pbo_flags_a_matrix_whose_winner_always_flips(tmp_db):
    # config 0 wins in even blocks, config 1 in odd ones: whichever looks best in-sample
    # is the one that loses out-of-sample, every single time.
    matrix = []
    for b in range(8):
        for _ in range(4):
            matrix.append([1.0, -1.0] if b % 2 == 0 else [-1.0, 1.0])
        matrix[-1] = [x + 0.01 for x in matrix[-1]]  # break the zero-variance case
    assert gates.pbo_cscv(matrix, splits=8) == 1.0


def test_pbo_is_low_when_one_config_genuinely_dominates(tmp_db):
    matrix = [[0.2, -0.1] if i % 2 else [0.1, -0.2] for i in range(64)]
    assert gates.pbo_cscv(matrix, splits=8) == 0.0


def test_pbo_is_none_for_a_single_config(tmp_db):
    assert gates.pbo_cscv([[0.1] for _ in range(64)]) is None


def test_deflated_sharpe_says_when_it_could_not_deflate(tmp_db):
    dsr, notes = gates.deflated_sharpe([0.1, 0.2, 0.05, 0.15] * 10, trials=1)
    assert dsr is not None
    assert any("plain PSR" in n for n in notes)


def test_deflated_sharpe_falls_with_more_trials(tmp_db):
    series = [0.1, -0.02, 0.08, 0.01] * 12
    few, _ = gates.deflated_sharpe(series, trials=2, trial_sharpes=[0.4, 0.1])
    many, _ = gates.deflated_sharpe(series, trials=200, trial_sharpes=[0.4, 0.1])
    assert many < few


# --------------------------------------------------------------------- gates: replay


def test_replay_gate_fails_a_candidate_with_high_pbo(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="overfit")
    exp = make_experiment(tmp_db, "exp_overfit", lane="confluence-5", key="min_entities", old=3, new=5)

    result = gates.replay_gate(exp, tmp_db)

    assert result.passed is False
    assert result.metrics["pbo"] >= 0.5
    assert any("PBO" in r for r in result.reasons)


def test_replay_gate_passes_a_candidate_that_holds_up(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="clean")
    exp = make_experiment(tmp_db, "exp_clean", lane="confluence-5", key="min_entities", old=3, new=5)

    result = gates.replay_gate(exp, tmp_db)

    assert result.passed is True, result.reasons
    assert result.metrics["pbo"] < 0.5
    assert result.metrics["dsr"] >= 0.95
    assert result.metrics["candidate_trades"] >= 30


def test_replay_gate_fails_below_min_trades(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="clean", blocks=8, per_block=4)  # only 16 candidate trades
    exp = make_experiment(tmp_db, "exp_small", lane="confluence-5", key="min_entities", old=3, new=5)
    result = gates.replay_gate(exp, tmp_db)
    assert result.passed is False
    assert any("need >= 30" in r for r in result.reasons)


def test_replay_gate_refuses_a_key_it_cannot_re_run(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="clean")
    exp = make_experiment(tmp_db, "exp_bool", lane="confluence-5", key="follow_exits", old=True, new=False)
    result = gates.replay_gate(exp, tmp_db)
    assert result.passed is False
    assert any("no replay rule" in r for r in result.reasons)


def test_replay_gate_records_its_verdict(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="clean")
    exp = make_experiment(tmp_db, "exp_rec", lane="confluence-5", key="min_entities", old=3, new=5)
    gates.replay_gate(exp, tmp_db)
    rows = tmp_db.execute("SELECT * FROM gate_results WHERE experiment_id = ?", (exp,)).fetchall()
    assert len(rows) == 1 and rows[0]["gate"] == "replay"


def test_replay_features_ignore_signals_recorded_after_the_decision(tmp_db):
    early = insert_signal(tmp_db, "sig_early", created_ms=BASE_MS - 1000, payload={"entities": 2})
    late = insert_signal(tmp_db, "sig_late", created_ms=BASE_MS + 5000, payload={"entities": 10})
    insert_decision(tmp_db, "d1", ts_ms=BASE_MS, signals=(early, late))
    row = tmp_db.execute("SELECT * FROM decisions WHERE decision_id = 'd1'").fetchone()
    assert gates._feature_value(tmp_db, dict(row), "min_entities") == Decimal(2)


# --------------------------------------------------------------------- gates: shadow


def test_shadow_gate_refuses_an_unlabelled_stream(tmp_db, risk_file):
    exp = make_experiment(tmp_db, "exp_nolabel", lane="confluence-5", key="min_entities", old=3, new=5)
    result = gates.shadow_gate(exp, tmp_db)
    assert result.passed is False
    assert any("labelled" in r for r in result.reasons)


def test_shadow_gate_requires_enough_calendar_time(tmp_db, risk_file):
    exp = make_experiment(tmp_db, "exp_fast", lane="confluence-5", key="min_entities", old=3, new=5)
    build_shadow_arms(tmp_db, exp, n=60, days=3)
    result = gates.shadow_gate(exp, tmp_db)
    assert result.passed is False
    assert any("days" in r for r in result.reasons)


def test_shadow_gate_passes_a_candidate_that_beats_the_incumbent(tmp_db, risk_file):
    exp = make_experiment(tmp_db, "exp_shadow", lane="confluence-5", key="min_entities", old=3, new=5)
    build_shadow_arms(tmp_db, exp, n=60, days=20)
    result = gates.shadow_gate(exp, tmp_db)
    assert result.passed is True, result.reasons
    assert result.metrics["candidate_trades"] == 60


# ------------------------------------------------------------------ gates: promotion


def _promotable(conn, exp_id: str = "exp_promote") -> str:
    build_replay_stream(conn, pattern="clean")
    exp = make_experiment(conn, exp_id, lane="confluence-5", key="min_entities", old=3, new=5)
    build_shadow_arms(conn, exp, n=60, days=20)
    return exp


def test_promote_writes_the_parameter_and_a_change_entry(tmp_db, risk_file):
    exp = _promotable(tmp_db)

    assert gates.promote(exp, tmp_db) is True

    risk = load_risk(risk_file)
    assert risk.lane(Lane.CONFLUENCE_5).params["min_entities"] == 5
    entry = journal.read(20, kind="change", subject=exp, conn=tmp_db)[0]
    assert "previous_params" in entry["body"] and "replay" in entry["body"]
    assert tmp_db.execute(
        "SELECT status FROM experiments WHERE experiment_id = ?", (exp,)
    ).fetchone()["status"] == "promoted"


def test_promote_refuses_when_self_promotion_is_switched_off(tmp_db, risk_file):
    exp = _promotable(tmp_db)
    raw = yaml.safe_load(risk_file.read_text(encoding="utf-8"))
    raw["bounds"]["allow_self_promotion"] = False
    risk_file.write_text(yaml.safe_dump(raw), encoding="utf-8")

    assert gates.promote(exp, tmp_db) is False

    assert load_risk(risk_file).lane(Lane.CONFLUENCE_5).params["min_entities"] == 3
    assert tmp_db.execute(
        "SELECT status FROM experiments WHERE experiment_id = ?", (exp,)
    ).fetchone()["status"] == "rejected"


def test_promotion_cannot_widen_the_operator_bounds(tmp_db, risk_file):
    before = load_risk(risk_file).bounds.model_dump()
    exp = make_experiment(
        tmp_db, "exp_widen", lane="confluence-5", key="max_size_pct_bankroll", old=5.0, new=50.0
    )
    build_shadow_arms(tmp_db, exp, n=60, days=20)
    build_replay_stream(tmp_db, pattern="clean")

    assert gates.promote(exp, tmp_db) is False

    assert load_risk(risk_file).bounds.model_dump() == before
    assert load_risk(risk_file).bounds.max_size_pct_bankroll == 5.0


def test_a_successful_promotion_leaves_the_bounds_block_untouched(tmp_db, risk_file):
    before = load_risk(risk_file).bounds.model_dump()
    gates.promote(_promotable(tmp_db), tmp_db)
    assert load_risk(risk_file).bounds.model_dump() == before


def test_promote_refuses_a_candidate_that_failed_a_gate(tmp_db, risk_file):
    build_replay_stream(tmp_db, pattern="overfit")
    exp = make_experiment(tmp_db, "exp_bad", lane="confluence-5", key="min_entities", old=3, new=5)
    build_shadow_arms(tmp_db, exp, n=60, days=20)
    assert gates.promote(exp, tmp_db) is False
    assert load_risk(risk_file).lane(Lane.CONFLUENCE_5).params["min_entities"] == 3


def test_rollback_restores_the_previous_parameter_block(tmp_db, risk_file):
    exp = _promotable(tmp_db)
    gates.promote(exp, tmp_db)
    assert load_risk(risk_file).lane(Lane.CONFLUENCE_5).params["min_entities"] == 5

    assert gates.rollback(exp, tmp_db) is True

    assert load_risk(risk_file).lane(Lane.CONFLUENCE_5).params["min_entities"] == 3
    assert journal.verify(tmp_db)[0] is True
    assert any(
        "rolled back" in e["body"] for e in journal.read(20, kind="correction", conn=tmp_db)
    )


def test_rollback_without_a_promotion_entry_does_nothing(tmp_db, risk_file):
    assert gates.rollback("exp_never_promoted", tmp_db) is False


# ------------------------------------------------------------------ gates: allocator


def _stats(wins: int, losses: int) -> metrics.LaneStats:
    return metrics.LaneStats(key="x", trades=wins + losses, wins=wins, losses=losses)


def test_allocator_is_deterministic_and_sums_to_one(tmp_db):
    stats = {Lane.CONFLUENCE_5: _stats(20, 5), Lane.KOL_FADE: _stats(3, 12)}
    a = gates.allocator(stats, draws=500)
    b = gates.allocator(stats, draws=500)
    assert a == b
    assert sum(a.values()) == pytest.approx(1.0)


def test_allocator_favours_the_winning_lane(tmp_db):
    stats = {Lane.CONFLUENCE_5: _stats(20, 5), Lane.KOL_FADE: _stats(3, 12)}
    weights = gates.allocator(stats, draws=2000)
    assert weights[Lane.CONFLUENCE_5] > 0.95
    assert weights[Lane.KOL_FADE] < 0.05


def test_allocator_keeps_exploring_a_lane_that_is_only_slightly_behind(tmp_db):
    # Overlapping posteriors: the weaker lane must still get real paper capital, which is
    # the difference between Thompson sampling and just picking the leader.
    stats = {Lane.CONFLUENCE_5: _stats(12, 8), Lane.KOL_FADE: _stats(9, 11)}
    weights = gates.allocator(stats, draws=2000)
    assert weights[Lane.CONFLUENCE_5] > weights[Lane.KOL_FADE] > 0.15


def test_allocator_explores_an_unproven_lane_against_a_thin_leader(tmp_db):
    # Two wins from two trades is not evidence; the untried lane keeps a real share.
    stats = {Lane.CONFLUENCE_5: _stats(2, 0), Lane.KOL_FADE: _stats(0, 0)}
    weights = gates.allocator(stats, draws=2000)
    assert weights[Lane.KOL_FADE] > 0.2


def test_allocator_spreads_evenly_when_nothing_is_known_yet(tmp_db):
    stats = {Lane.CONFLUENCE_5: _stats(0, 0), Lane.KOL_FADE: _stats(0, 0)}
    weights = gates.allocator(stats, draws=4000)
    assert weights[Lane.CONFLUENCE_5] == pytest.approx(0.5, abs=0.05)


def test_allocator_handles_an_empty_window(tmp_db):
    assert gates.allocator({}) == {}
    assert gates.allocator({Lane.MANUAL: _stats(1, 0)}) == {Lane.MANUAL: 1.0}
