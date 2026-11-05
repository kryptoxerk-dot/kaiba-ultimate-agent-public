"""Real-broker admission timing and strict additive-cohort qualification, offline."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from kaiba.core.config import get_settings
from kaiba.learning import admission_paper as ap
from kaiba.learning import candidate_paper as cp
from kaiba.learning import strategy_campaign as sc
from kaiba.learning.signal_audit import ExitLadder

T0 = 1_000_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("admission fixtures may not use transport")
    monkeypatch.setattr("socket.socket", refused)
    monkeypatch.setattr("subprocess.Popen", refused)


def manifest(*, hold=60_000, legacy=False, at=T0):
    factory = sc.generate_campaign if legacy else ap.generate_admission_campaign
    value = factory([], ExitLadder(), now_ms=at, groups=[("sm-trenches", "sol")], max_candidates=2)
    value["hypotheses"] = [a for a in value["hypotheses"] if a["spec"]["exit"]["hold_ms"] == hold]
    value["campaign_id"] = sc._campaign_id(value)
    return value


def quote(at, price="1", **extra):
    return {"quote_ts_ms": at, "price_usd": price, "liquidity_usd": "1000000",
        "native_usd": "100", "decimals": 6, "source": "synthetic-fixture",
        "tax_known_zero": True, "venue_fee_bps": 125,
        "venue_fee_source": "synthetic-assumption-not-observed-live", **extra}


def opportunity():
    return {"decision_id": "source", "ts_ms": T0 + 1000, "lane": "sm-trenches",
        "chain": "sol", "token": "token", "mode": "live", "features": {}, "paper_stake_usd": "30"}


def rig(tmp_path, *, hold=60_000, legacy=False, name="paper"):
    clock = {"ms": T0 + 81_000}
    cls = cp.CandidatePaperRunner if legacy else ap.AdmissionPaperRunner
    runner = cls(tmp_path / "learning" / name, learning_root=tmp_path / "learning",
                 max_stake_usd="30", clock=lambda: clock["ms"] / 1000)
    return runner, manifest(hold=hold, legacy=legacy), clock


def poll(rig_, at, *, price_at=None, price="1", ops=(), q=None):
    runner, m, clock = rig_
    clock["ms"] = T0 + at
    quotes = {} if q is False else {("sol", "token"): q or quote(T0 + (at if price_at is None else price_at), price)}
    return runner.run_candidates(m, list(ops), quotes, now_ms=clock["ms"])


def book(rig_):
    runner, m, _ = rig_
    return runner.book_path(m["campaign_id"], m["hypotheses"][0]["hypothesis_id"])


def rows(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, args).fetchall()


def write(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(sql, args)
        conn.commit()


def entered(tmp_path, **kwargs):
    value = rig(tmp_path, **kwargs)
    result = poll(value, 81_000, price_at=31_000, ops=[opportunity()])
    assert not result["errors"] and result["arms"][0]["order_count"] == 1
    return value


def closed(tmp_path, **kwargs):
    value = entered(tmp_path, **kwargs)
    result = poll(value, 154_000, price="1.5")
    assert not result["errors"] and len(result["outcomes"]) == 1
    return value, result["outcomes"][0]


def rehash(m):
    for arm in m["hypotheses"]:
        arm["spec_hash"] = sc._hash(arm["spec"])
        arm["hypothesis_id"] = "ph_" + arm["spec_hash"][:24]
    m["campaign_id"] = sc._campaign_id(m)


def test_real_broker_admission_hold_blocks_23_seconds_and_closes_after_73(tmp_path):
    sources = sc.source_versions()
    old = entered(tmp_path, legacy=True, name="old")
    old_identity = rows(book(old), "SELECT value FROM candidate_meta WHERE key='identity'")
    new = entered(tmp_path, name="new")
    old_early = poll(old, 104_000)
    new_early = poll(new, 104_000)
    assert old_early["arms"][0]["order_count"] == 2
    assert new_early["arms"][0]["order_count"] == 1 and new_early["assessment_rows"][0]["status"] == "open"
    final = poll(new, 154_000)
    row = final["outcomes"][0]
    assert row["opened_ms"] == row["entry_processing_ms"] == T0 + 81_000
    assert row["fill_evidence"][0]["quote_ms"] == T0 + 31_000
    assert row["closed_ms"] - row["opened_ms"] == 73_000
    assert row["timing_complete"] is True and row["timing_error"] is None
    assert rows(book(new), "SELECT mode,provider,state FROM orders") == [("shadow", "paper", "filled")] * 2
    assert rows(book(new), "SELECT side,created_ms FROM orders ORDER BY created_ms") == [
        ("buy", T0 + 81_000), ("sell", T0 + 154_000)]
    assert rows(book(old), "SELECT value FROM candidate_meta WHERE key='identity'") == old_identity
    assert sc.source_versions() == sources and not get_settings().db_path.exists()


def test_late_price_trigger_uses_actual_recognition_and_preserves_frozen_stop_math(tmp_path):
    value = entered(tmp_path, hold=None)
    first = poll(value, 145_000, price_at=100_000, price="0.5")
    assert first["arms"][0]["order_count"] == 1
    pending = json.loads(rows(book(value), "SELECT pending FROM candidate_protection")[0][0])
    assert pending["trigger_price_ms"] == T0 + 100_000
    assert pending["trigger_ms"] == pending["recognized_ms"] == T0 + 145_000
    assert pending["due_ms"] == T0 + 157_000
    early = poll(value, 150_000, price_at=120_000, price="0.5")
    assert early["arms"][0]["order_count"] == 1
    final = poll(value, 158_000, price="0.5")
    assert final["arms"][0]["order_count"] == 2
    assert final["outcomes"][0]["timing_complete"] is True
    assert final["outcomes"][0]["pnl_native"] < 0


def test_slow_inventory_read_cannot_backdate_price_trigger_recognition(tmp_path, monkeypatch):
    value = entered(tmp_path, hold=None)
    original = ap.load_position
    def slow(conn, identity):
        result = original(conn, identity)
        value[2]["ms"] += 10_000
        return result
    monkeypatch.setattr(ap, "load_position", slow)
    poll(value, 135_000, price_at=100_000, price="0.5")
    pending = json.loads(rows(book(value), "SELECT pending FROM candidate_protection")[0][0])
    assert pending["recognized_ms"] == T0 + 145_000 and pending["due_ms"] == T0 + 157_000


@pytest.mark.parametrize("component", ["price_ts_ms", "depth_ts_ms", "native_price_ts_ms"])
def test_inherited_freshness_remains_enforced_after_book_delay(tmp_path, monkeypatch, component):
    value = rig(tmp_path)
    runner = value[0]
    original = runner._open
    def slow(identity):
        conn = original(identity)
        value[2]["ms"] += 10_000
        return conn
    monkeypatch.setattr(runner, "_open", slow)
    quote_at = T0 + 81_000
    old = quote_at - (299_000 if component == "native_price_ts_ms" else 59_000)
    result = poll(value, 81_000, ops=[opportunity()], q=quote(quote_at, **{component: old}))
    assert result["arms"][0]["order_count"] == 0
    assert result["assessment_rows"][0]["status"] == "pending"


def test_metadata_after_due_cannot_supply_a_price_before_due(tmp_path):
    value = entered(tmp_path)
    early = poll(value, 160_000, q=quote(T0 + 160_000, price_ts_ms=T0 + 152_999))
    assert early["arms"][0]["order_count"] == 1
    final = poll(value, 161_000)
    assert len(final["outcomes"]) == 1 and final["outcomes"][0]["timing_complete"] is True


def test_fee_and_unknown_venue_tax_evidence_is_unchanged(tmp_path):
    old = entered(tmp_path, legacy=True, name="old")
    new = entered(tmp_path, name="new")
    legacy = poll(old, 154_000)["outcomes"][0]
    row = poll(new, 154_000)["outcomes"][0]
    assert (row["entry_debit_native"], row["exit_credit_native"], row["fees_native"]) == (
        legacy["entry_debit_native"], legacy["exit_credit_native"], legacy["fees_native"])
    assert row["fee_policy"] == legacy["fee_policy"]
    assert row["cost_model_assumed"] is True and row["live_cost_verified"] is False
    missing = rig(tmp_path, name="missing-costs")
    q = quote(T0 + 31_000)
    for key in ("tax_known_zero", "venue_fee_bps", "venue_fee_source"):
        q.pop(key)
    poll(missing, 81_000, ops=[opportunity()], q=q)
    q["quote_ts_ms"] = T0 + 154_000
    row = poll(missing, 154_000, q=q)["outcomes"][0]
    assert row["timing_complete"] is True and row["cost_complete"] is False


@pytest.mark.parametrize("fault", ["legacy", "module", "policy", "base_source", "arm_hash", "campaign_id", "fdr", "duplicate", "malformed_spec"])
def test_tampered_or_malformed_wrappers_are_rejected_before_any_book_open(tmp_path, fault):
    value = rig(tmp_path)
    m = value[1]
    if fault == "legacy":
        m = manifest(legacy=True)
    elif fault in ("module", "policy"):
        key = "sources" if fault == "module" else "poll_ms"
        replacement = {ap.SOURCE_KEY: "0" * 64} if fault == "module" else 1
        m["assessment_policy"][ap.POLICY_KEY][key] = replacement
        for arm in m["hypotheses"]:
            arm["spec"][ap.POLICY_KEY][key] = replacement
        rehash(m)
    elif fault == "base_source":
        m["source_versions"]["execution/paper.py"] = "0" * 64
        rehash(m)
    elif fault == "arm_hash":
        m["hypotheses"][0]["spec_hash"] = "bad"
        m["campaign_id"] = sc._campaign_id(m)
    elif fault == "campaign_id":
        m["campaign_id"] = "pc_bad"
    elif fault == "fdr":
        m["assessment_policy"]["fdr_q"] = 1
        rehash(m)
    elif fault == "malformed_spec":
        m["hypotheses"][0]["spec"] = None
        m["campaign_id"] = sc._campaign_id(m)
    else:
        m["hypotheses"] *= 2
        m["campaign_id"] = sc._campaign_id(m)
    with pytest.raises(ValueError):
        value[0].run_candidates(m, [opportunity()], {}, now_ms=T0 + 81_000)
    assert not list((tmp_path / "learning").rglob("*.db"))


def test_new_identity_never_opens_or_relabels_an_existing_legacy_book(tmp_path):
    legacy = entered(tmp_path, legacy=True)
    prior = rows(book(legacy), "SELECT value FROM candidate_meta WHERE key='identity'")
    prior_orders = rows(book(legacy), "SELECT * FROM orders")
    new = ap.AdmissionPaperRunner(tmp_path / "learning/paper", learning_root=tmp_path / "learning")
    with pytest.raises(ValueError):
        new.run_candidates(legacy[1], [], {}, now_ms=T0 + 90_000)
    assert rows(book(legacy), "SELECT * FROM orders") == prior_orders
    assert rows(book(legacy), "SELECT value FROM candidate_meta WHERE key='identity'") == prior
    assert legacy[0].watched_tokens(legacy[1]) == [("sol", "token")]


def test_only_new_manifest_has_new_ids_and_empty_data_never_fills(tmp_path):
    legacy, new = manifest(legacy=True), manifest()
    assert legacy["source_versions"] == new["source_versions"] == sc.source_versions()
    assert legacy["campaign_id"] != new["campaign_id"]
    assert legacy["hypotheses"][0]["hypothesis_id"] != new["hypotheses"][0]["hypothesis_id"]
    result = poll(rig(tmp_path), 81_000, q=False)
    assert result["outcomes"] == [] and result["arms"][0]["order_count"] == 0


@pytest.mark.parametrize("fault", ["missing_leg", "entry_open", "native_future", "price_before_due", "trigger", "missing_policy", "qualification"])
def test_independent_timing_validation_blocks_economically_complete_bad_rows(tmp_path, fault):
    value, good = closed(tmp_path)
    bad = copy.deepcopy(good)
    if fault == "missing_leg":
        bad["fill_evidence"].pop()
    elif fault == "entry_open":
        bad["opened_ms"] = bad["fill_evidence"][0]["quote_ms"]
    elif fault == "native_future":
        bad["fill_evidence"][-1]["native_price_ts_ms"] = bad["closed_ms"] + 1
    elif fault == "price_before_due":
        bad["fill_evidence"][-1]["price_ts_ms"] = bad["fill_evidence"][-1]["exit_timing"]["due_ms"] - 1
    elif fault == "trigger":
        bad["fill_evidence"][-1]["exit_timing"]["trigger_ms"] += 1
    elif fault == "missing_policy":
        bad.pop(ap.POLICY_KEY)
    else:
        bad.pop("timing_complete")
    # Positive control: the old validator accepts this complete cashflow shape.
    assert sc._validate_outcome(bad, value[1]["hypotheses"][0], T0, T0 + 154_000)[0] is not None
    before = copy.deepcopy(bad)
    result = ap.assess_admission_campaign(value[1], [bad], now_ms=T0 + 154_000)
    assert result["arms"][0]["complete_count"] == 0 and result["arms"][0]["unknown_count"] == 1
    assert result["timing_failures"] and bad == before
    valid = ap.assess_admission_campaign(value[1], [good], now_ms=T0 + 154_000)
    assert valid["arms"][0]["complete_count"] == 1 and valid["timing_failures"] == []


def test_prior_base_assessment_cannot_seed_strict_wrapper_streak(tmp_path):
    value, row = closed(tmp_path)
    prior = sc.assess_campaign(value[1], [row], now_ms=T0 + 154_000)["next_state"]
    with pytest.raises(ValueError, match="prior assessment"):
        ap.assess_admission_campaign(value[1], [row], now_ms=T0 + 154_000, prior_state=prior)


def test_duplicate_stream_and_refused_zero_exposure_preserve_denominators(tmp_path):
    value = entered(tmp_path)
    duplicate = poll(value, 90_000, ops=[opportunity()])
    assert duplicate["arms"][0]["order_count"] == 1
    empty = rig(tmp_path, name="refusal")
    poll(empty, 81_000, ops=[opportunity()], q=False)
    expired = poll(empty, 125_000, q=False)
    row = expired["assessment_rows"][0]
    assert row["known_unfilled"] and row["timing_complete"] and row["no_position_created"]
    assessed = ap.assess_admission_campaign(empty[1], [row], now_ms=T0 + 125_000)
    assert assessed["arms"][0]["known_unfilled"] == 1 and assessed["arms"][0]["complete_count"] == 0


def test_rollover_keeps_real_open_inventory_and_closed_history_without_writes(tmp_path):
    value = entered(tmp_path, legacy=True)
    runner, m, _ = value
    before = rows(book(value), "SELECT * FROM orders")
    watched = runner.watched_tokens(m)
    args = dict(learning_root=tmp_path / "learning", now_ms=T0 + 90_000)
    check = ap.inspect_rollover([m], runner.state_dir, **args)
    assert check["status"] == "blocked" and check["checks"][0]["open_inventory"] == 1
    assert not check["activation_authorized"] and not check["archive_performed"]
    assert runner.watched_tokens(m) == watched and rows(book(value), "SELECT * FROM orders") == before
    poll(value, 104_000)
    check = ap.inspect_rollover([m], runner.state_dir, **(args | {"now_ms": T0 + 104_000}))
    assert check["status"] == "ready_for_explicit_archive" and check["checks"][0]["orders"] == 2
    assert check["writes"] == 0 and book(value).exists() and check["requires_exclusive_worker_lock_and_fresh_CAS"]


@pytest.mark.parametrize("fault", ["pending", "missing_book", "ambiguous", "unmatched", "foreign", "future_tick", "mismatch_token"])
def test_rollover_unknown_or_unsettled_evidence_never_means_zero_exposure(tmp_path, fault):
    value = rig(tmp_path, legacy=True)
    if fault == "pending":
        poll(value, 81_000, ops=[opportunity()], q=False)
    elif fault != "missing_book":
        poll(value, 81_000, price_at=31_000, ops=[opportunity()])
        poll(value, 104_000)
        if fault == "ambiguous":
            write(book(value), "UPDATE orders SET state='unknown'")
        elif fault == "unmatched":
            write(book(value), "DELETE FROM position_orders")
        elif fault == "foreign":
            write(book(value), "UPDATE orders SET mode='live'")
        elif fault == "future_tick":
            write(book(value), "UPDATE candidate_meta SET value=? WHERE key='last_tick_ms'", (str(T0 + 999_000),))
        else:
            write(book(value), "UPDATE orders SET token='different'")
    check = ap.inspect_rollover([value[1]], value[0].state_dir,
        learning_root=tmp_path / "learning", now_ms=T0 + 125_000)
    assert check["status"] == "blocked" and check["blockers"]
    assert check["writes"] == 0 and check["activation_authorized"] is False


def test_no_extra_arm_or_open_inventory_allowance_and_WAL_guard_bubbles(tmp_path):
    with pytest.raises(ValueError):
        ap.generate_admission_campaign([], ExitLadder(), now_ms=T0, max_candidates=13)
    with pytest.raises(ValueError):
        ap.AdmissionPaperRunner(tmp_path / "learning/paper", learning_root=tmp_path / "learning", max_open_per_arm=4)
    value = rig(tmp_path, legacy=True)
    with pytest.raises(ValueError):
        ap.inspect_rollover([value[1]], value[0].state_dir, learning_root=tmp_path / "learning", now_ms=T0, max_active_arms=18)
    from kaiba.learning.autolearn_io import SourceDeferred
    def guard():
        raise SourceDeferred("wal_over_limit; retry after at least 60s")
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        ap.inspect_rollover([value[1]], value[0].state_dir, learning_root=tmp_path / "learning", now_ms=T0, guard=guard)


def test_base_three_day_statistics_and_family_correction_remain_unchanged():
    day = ss_day = 86_400_000
    frozen = 1_800_000_000_000 // day * day
    m = ap.generate_admission_campaign([], ExitLadder(), now_ms=frozen,
        groups=[("sm-trenches", "sol")], max_candidates=2)
    policy = m["assessment_policy"][ap.POLICY_KEY]
    evidence = []
    def add(i, signal):
        for arm in m["hypotheses"]:
            opened, closed = signal + 31_000, signal + 104_000
            hold = arm["spec"]["exit"].get("hold_ms")
            trigger = opened + hold if hold is not None else opened + 1000
            basis = "fixed_admission_deadline" if hold is not None else "observed_price_recognition"
            recognized = closed if hold is not None else trigger
            buy = {ap.POLICY_KEY: policy, "side": "buy", "quote_ms": opened, "price_ts_ms": opened,
                "depth_ts_ms": opened, "native_price_ts_ms": opened, "admission_ms": opened,
                "processing_ms": opened, "ledger_ms": opened, "order_id": "buy" + str(i),
                "mode": "shadow", "provider": "paper", "state": "filled"}
            sell = {ap.POLICY_KEY: policy, "side": "sell", "quote_ms": closed, "price_ts_ms": closed,
                "depth_ts_ms": closed, "native_price_ts_ms": closed, "admission_ms": closed,
                "processing_ms": closed, "ledger_ms": closed, "exit_timing": {"basis": basis,
                    "reason": "time_exit" if hold is not None else "stop_loss", "trigger_ms": trigger,
                    "recognized_ms": recognized, "trigger_price_ms": recognized, "due_ms": trigger + 12_000},
                "order_id": "sell" + str(i), "mode": "shadow", "provider": "paper", "state": "filled"}
            evidence.append({"campaign_id": m["campaign_id"], "hypothesis_id": arm["hypothesis_id"],
                "spec_hash": arm["spec_hash"], "outcome_id": arm["hypothesis_id"] + str(i),
                "signal_id": str(i), "token": str(i), "lane": "sm-trenches", "chain": "sol",
                "signal_ms": signal, "opened_ms": opened, "closed_ms": closed, "observed_ms": closed,
                "entry_processing_ms": opened, "provider": "paper", "source": "PaperBroker", "mode": "shadow",
                "status": "closed", "features": {}, "evidence_complete": True, "cost_complete": True,
                "inventory_complete": True, "entry_debit_native": 1000, "exit_credit_native": 1200,
                "timing_complete": True, ap.POLICY_KEY: policy, "fill_evidence": [buy, sell]})
    now = frozen + 10 * day + day // 2
    for i in range(60):
        add(i, frozen + (i + 1) * (now - frozen - 200_000) // 60)
    prior = None
    for offset in range(3):
        at = now + offset * ss_day
        if offset:
            add(60 + offset, at - 200_000)
        result = ap.assess_admission_campaign(m, evidence, now_ms=at, prior_state=prior)
        base = sc.assess_campaign(m, evidence, now_ms=at, prior_state=prior)
        assert result["arms"] == base["arms"] and result["family_size"] == 2
        assert result["timing_failures"] == []
        prior = result["next_state"]
    assert result["status"] == "PAPER_EVIDENCE_READY" and result["funding_allowed"] is False


def test_real_curve_arithmetic_and_original_quote_clocks_remain_in_use(tmp_path):
    value = rig(tmp_path)
    def curve_quote(at):
        return quote(T0 + at, liquidity_usd=None, curve={
            "virtual_sol": 31_000_000_000, "virtual_token": 1_000_000_000_000_000,
            "real_sol": 1_000_000_000, "real_token": 720_100_000_000_000,
            "observed_ms": T0 + at, "source": "synthetic-curve", "complete": False})
    entered_ = poll(value, 81_000, ops=[opportunity()], q=curve_quote(31_000))
    assert not entered_["errors"] and entered_["arms"][0]["order_count"] == 1
    result = poll(value, 154_000, q=curve_quote(154_000))
    assert not result["errors"] and result["outcomes"][0]["timing_complete"]
    row = result["outcomes"][0]
    assert row["basis"][0]["basis"] == "curve"
    assert row["fill_evidence"][0]["quote_ms"] == T0 + 31_000
    assert row["opened_ms"] == T0 + 81_000 and row["fee_policy"] == cp._fee_policy()


@pytest.mark.parametrize("chain,venue", [("bsc", 100), ("robinhood", 60)])
def test_evm_real_broker_fees_and_timing_are_qualified_without_new_financial_path(tmp_path, chain, venue):
    m = ap.generate_admission_campaign([], ExitLadder(), now_ms=T0, groups=[("sm-trenches", chain)], max_candidates=2)
    m["hypotheses"] = [a for a in m["hypotheses"] if a["spec"]["exit"]["hold_ms"] == 60_000]
    m["campaign_id"] = sc._campaign_id(m)
    runner = ap.AdmissionPaperRunner(tmp_path / "learning/paper", learning_root=tmp_path / "learning")
    op = opportunity() | {"chain": chain}
    first = runner.run_candidates(m, [op], {(chain, "token"): quote(T0 + 31_000, decimals=18, venue_fee_bps=venue)}, now_ms=T0 + 81_000)
    assert not first["errors"] and first["arms"][0]["order_count"] == 1
    final = runner.run_candidates(m, [], {(chain, "token"): quote(T0 + 154_000, decimals=18, venue_fee_bps=venue)}, now_ms=T0 + 154_000)
    assert not final["errors"]
    row = final["outcomes"][0]
    assert row["timing_complete"] and row["cost_complete"] and row["pnl_native"] < 0


def test_partial_ladder_quantities_reuse_original_inventory_fractions(tmp_path):
    value = entered(tmp_path, hold=None)
    poll(value, 100_000, price="2.5")
    trimmed = poll(value, 113_000, price="2.5")
    assert trimmed["arms"][0]["order_count"] == 2 and not trimmed["outcomes"]
    qty, total = map(int, rows(book(value), "SELECT qty,qty_total FROM positions")[0])
    assert qty == total - total // 2
    repeated = poll(value, 113_000, price="2.5")
    assert repeated["arms"][0]["order_count"] == 2
    poll(value, 130_000, price="0.5")
    final = poll(value, 143_000, price="0.5")
    assert final["outcomes"][0]["timing_complete"]
    assert len(final["outcomes"][0]["fill_evidence"]) == 3


def test_rollover_cap_is_unknown_and_never_deletes_history(tmp_path, monkeypatch):
    value = entered(tmp_path, legacy=True)
    poll(value, 104_000)
    before = rows(book(value), "SELECT * FROM orders")
    monkeypatch.setattr(ap, "ROW_CAP", 1)
    checked = ap.inspect_rollover([value[1]], value[0].state_dir,
        learning_root=tmp_path / "learning", now_ms=T0 + 125_000)
    assert checked["status"] == "blocked" and "row_cap" in checked["blockers"][0]["reason"]
    assert rows(book(value), "SELECT * FROM orders") == before


@pytest.mark.parametrize("fault", ["fabricated_close", "removed_sell_and_link", "removed_sell_link", "sell_quantity", "close_clock"])
def test_closed_row_cannot_prove_flat_without_actual_exit_and_reconciled_atoms(tmp_path, fault):
    value = entered(tmp_path, legacy=True)
    if fault == "fabricated_close":
        write(book(value), "UPDATE positions SET qty='0',closed_ms=?", (T0 + 104_000,))
    else:
        poll(value, 104_000)
        if fault == "removed_sell_and_link":
            write(book(value), "DELETE FROM position_orders WHERE order_id IN (SELECT order_id FROM orders WHERE side='sell')")
            write(book(value), "DELETE FROM orders WHERE side='sell'")
        elif fault == "removed_sell_link":
            write(book(value), "DELETE FROM position_orders WHERE order_id IN (SELECT order_id FROM orders WHERE side='sell')")
        elif fault == "sell_quantity":
            write(book(value), "UPDATE orders SET amount_in='1' WHERE side='sell'")
        else:
            write(book(value), "UPDATE positions SET closed_ms=closed_ms+1")
    checked = ap.inspect_rollover([value[1]], value[0].state_dir,
        learning_root=tmp_path / "learning", now_ms=T0 + 125_000)
    assert checked["status"] == "blocked"
    assert checked["checks"][0]["unmatched_ledger_rows"] > 0
    assert checked["writes"] == 0 and not checked["archive_performed"]

