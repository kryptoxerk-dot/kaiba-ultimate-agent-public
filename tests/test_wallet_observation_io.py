"""Actual fixed collector/normalizer with bounded mock source transport only."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from kaiba.learning import autolearn_behavior as ab
from kaiba.learning import fixed_wallet_study as fs
from kaiba.learning import wallet_observation_io as io
from kaiba.learning import wallet_observation_tape as tape
from kaiba.learning.autolearn_io import SourceDeferred

T0 = 1_792_106_328_738


def raw(rowid, ts, *, text="10"):
    return {"id": rowid, "ts_ms": ts, "token": "TOKEN", "side": "buy" if rowid % 2 else "sell",
            "amount_token": "1", "amount_native": text, "usd_value": text,
            "source": "gmgn:smartmoney", "tx": "tx-" + str(rowid), "oversized_fields": 0}


class Reader:
    def __init__(self, histories):
        self.histories, self.calls, self.failure, self.horizon = histories, [], None, 0

    def rows(self, sql, args=()):
        self.calls.append((sql, tuple(args)))
        if self.failure:
            failure = self.failure(sql, args)
            if failure:
                raise failure
        if sql == io.HORIZON_SQL:
            return [{"value": json.dumps(self.horizon)}] if self.horizon is not None else []
        if sql == "SELECT registry_marker":
            return []
        assert sql in (io.LEGACY_SQL, io.PAGE_SQL), sql
        wallet = tuple(args[:2])
        rows = [r for r in self.histories.get(wallet, []) if args[2] <= r["ts_ms"] <= args[3]]
        if sql == io.PAGE_SQL:
            rows = [r for r in rows if (r["ts_ms"], r["id"]) > tuple(args[4:6])]
        return copy.deepcopy(sorted(rows, key=lambda r: (r["ts_ms"], r["id"]))[:args[-1]])

    def q(self, sql, args=()):
        raise AssertionError("unexpected tuple read")

    @property
    def physical(self):
        return [(s, a) for s, a in self.calls if s in (io.LEGACY_SQL, io.PAGE_SQL)]


def setup(tmp_path, *, baseline_count=2, text="10", delay=1000, eligible_delay=0):
    histories, rowid = {}, 1
    for wallet in tape.MEMBERS:
        count = baseline_count if wallet == tape.MEMBERS[0] else 2
        histories[wallet] = [raw(rowid + i, T0 - 100_000 + i, text=text) for i in range(count)]
        rowid += count
    reader = Reader(histories)
    fixed = fs.collect_fixed_study(reader, [], now_ms=T0, study_state=fs.initial_study_state())["study_next_state"]
    reader.calls.clear()
    root = tmp_path / "learning" / "autolearn"
    root.mkdir(parents=True)
    (root / "worker.lock").write_text(json.dumps({"pid": os.getpid(), "started_ms": T0}))
    now = {"ms": T0 + delay}
    store = root / "wallet-observation-tape"
    genesis = io.prepare_store(fixed, store_dir=store, learning_root=tmp_path / "learning",
                               now_ms=now["ms"], eligible_after_ms=now["ms"] + eligible_delay)
    context = {"store_dir": store, "learning_root": tmp_path / "learning",
               "expected_genesis": genesis["genesis_sha256"], "clock": lambda: now["ms"] / 1000}
    return reader, fixed, now, context


def review(queue=(), *, failure=None, counts=None):
    def run(reader, runner_root, *, now_ms, prior_state):
        if counts is not None:
            counts["review"] = counts.get("review", 0) + 1
        reader.rows("SELECT registry_marker")
        if failure:
            raise failure
        def collector(*args, **kwargs):
            if counts is not None:
                counts["collector"] = counts.get("collector", 0) + 1
            return ab.collect_behavior(*args, **kwargs)
        study = fs.collect_fixed_study(reader, queue, now_ms=now_ms,
            dynamic_state=prior_state.get("behavior_state"), study_state=prior_state["fixed_study_state"], collector=collector)
        return {"review": {"behavior": study["behavior"]}, "next_state": {
            **prior_state, "fixed_study_state": study["study_next_state"], "behavior_state": study["dynamic_next_state"]}}
    return run


def invoke(reader, fixed, now, context, callable_=None, prior=None):
    return io.run_review_with_tape(callable_ or review(), reader, Path("unused-runner-root"), now["ms"],
        prior or {"fixed_study_state": fixed}, **context)


def state(context, now):
    return io.validate_store(**{k: context[k] for k in ("store_dir", "learning_root", "expected_genesis")}, now_ms=now["ms"])


def report(context):
    return json.loads((context["store_dir"] / io.REPORT_FILE).read_text())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket", lambda *a, **k: pytest.fail("network forbidden"))


def test_eager_six_precede_registry_one_original_collector_and_parent_baselines_unchanged(tmp_path):
    reader, fixed, now, context = setup(tmp_path, baseline_count=833)
    counts, frozen = {}, copy.deepcopy(fixed)
    result = invoke(reader, fixed, now, context, review(counts=counts))
    assert counts == {"review": 1, "collector": 1}
    assert len(reader.physical) == 6
    registry = next(i for i, (sql, _) in enumerate(reader.calls) if sql == "SELECT registry_marker")
    assert all(i < registry for i, (sql, _) in enumerate(reader.calls) if sql == io.LEGACY_SQL)
    stored = state(context, now)
    assert stored["engine_state"]["wallets"][":".join(tape.MEMBERS[0])]["seed_status"] == "verified"
    assert sum(len(s["rows"]) for s in stored["engine_state"]["wallets"].values()) == 843
    assert result["next_state"]["fixed_study_state"]["selections"] == frozen["selections"]
    assert fixed == frozen
    assert all(a["started_ms"] == T0 + 1000 and a["captured_ms"] > T0 for a in stored["quota"]["attempts"])
    assert all(args[3] == T0 for _, args in reader.physical)
    profiles = result["review"]["behavior"]["behavior_reports"]
    assert all("routed_fixed" in p["coverage"]["query_status"] for p in profiles)
    assert report(context)["copy_qualified"] is False


def test_dynamic_exact_short_result_after_fixed_payload_over_original_headroom(tmp_path):
    reader, fixed, now, context = setup(tmp_path, baseline_count=833, text="9" * 100)
    queue = [{"chain": "bsc", "address": "0x" + str(i) * 40} for i in range(1, 5)]
    for i, selected in enumerate(queue):
        reader.histories[(selected["chain"], selected["address"])] = [raw(10000 + i, T0 - 100)]
    counts = {}
    result = invoke(reader, fixed, now, context, review(queue, counts=counts))
    assert counts == {"review": 1, "collector": 1}
    assert len(reader.physical) == 10
    stored = state(context, now)
    fixed_bytes = sum(a["charged_bytes"] for a in stored["quota"]["attempts"] if a["kind"] == "fixed")
    assert fixed_bytes > io.MAX_BYTES - 1001 * io.ROW_RESERVATION
    dynamic = [a for a in stored["quota"]["attempts"] if a["kind"] == "dynamic"]
    assert len(dynamic) == 4 and all(a["requested_limit"] < a["original_requested_limit"] == 1001 for a in dynamic)
    assert all(a["status"] == "observed" and a["charged_rows"] == 1 for a in dynamic)
    assert all(s == io.LEGACY_SQL for s, _ in reader.physical[6:])
    assert all(len(args) == 5 for _, args in reader.physical[6:])
    dynamic_reports = [p for p in result["review"]["behavior"]["behavior_reports"] if (p["chain"], p["wallet"]) not in tape.MEMBERS]
    assert len(dynamic_reports) == 4
    assert all(r["query_status"] == "declared_query_complete" for r in result["review"]["behavior"]["receipts"][-4:])


def test_full_dynamic_prefix_even_exact_end_is_unknown_consumes_reservation_and_never_retries(tmp_path):
    reader, fixed, now, context = setup(tmp_path, baseline_count=833, text="9" * 100)
    wallet = ("bsc", "0x" + "a" * 40)
    invoke(reader, fixed, now, context)
    quota = state(context, now)["quota"]
    physical_limit = (io.MAX_BYTES - sum(a["charged_bytes"] for a in quota["attempts"])) // io.ROW_RESERVATION
    assert 0 < physical_limit < 1001
    reader.histories[wallet] = [raw(10000 + i, T0 - 1000 + i) for i in range(physical_limit)]
    result = invoke(reader, fixed, now, context, review([{"chain": wallet[0], "address": wallet[1]}]))
    attempt = state(context, now)["quota"]["attempts"][-1]
    assert attempt["status"] == "failed" and attempt["failure_type"] == "unproven_dynamic_limit_prefix"
    assert attempt["charged_bytes"] == attempt["requested_limit"] * io.ROW_RESERVATION
    profile = next(p for p in result["review"]["behavior"]["behavior_reports"] if p["wallet"] == wallet[1])
    assert "dynamic_prefix_not_proved_exhausted" in profile["coverage"]["query_status"]
    assert result["next_state"]["behavior_state"]["selections"][":".join(wallet)]["baseline"] is None
    prior_calls = len(reader.physical)
    invoke(reader, fixed, now, context, review([{"chain": wallet[0], "address": wallet[1]}]))
    assert len(reader.physical) == prior_calls


def test_review_failure_after_capture_does_not_repeat_six_or_mutate_caller(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    before = copy.deepcopy(fixed)
    with pytest.raises(RuntimeError, match="intake failed"):
        invoke(reader, fixed, now, context, review(failure=RuntimeError("intake failed")))
    assert len(reader.physical) == 6 and fixed == before
    invoke(reader, fixed, now, context)
    assert len(reader.physical) == 6
    assert report(context)["verified_seed_wallets"] == 6


def test_failed_query_keeps_reservation_and_sticky_wal_blocks_every_later_source(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    reader.failure = lambda sql, args: SourceDeferred("wal_over_limit:2000000000") if sql == io.LEGACY_SQL else None
    with pytest.raises(SourceDeferred, match="wal_over_limit"):
        invoke(reader, fixed, now, context)
    stored = state(context, now)
    assert len(reader.physical) == 1
    assert len(stored["quota"]["attempts"]) == 1
    attempt = stored["quota"]["attempts"][0]
    assert attempt["status"] == "failed" and attempt["charged_rows"] == 3
    assert attempt["charged_bytes"] == 3 * io.ROW_RESERVATION
    assert not any(sql == "SELECT registry_marker" for sql, _ in reader.calls)


def test_new_slot_fifteen_minutes_after_last_completed_source_call_not_slot_start(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    original = reader.rows
    def slow(sql, args=()):
        result = original(sql, args)
        if sql in (io.LEGACY_SQL, io.PAGE_SQL):
            now["ms"] += 1000
        return result
    reader.rows = slow
    invoke(reader, fixed, now, context)
    last = state(context, now)["quota"]["last_attempt_ms"]
    count = len(reader.physical)
    now["ms"] = last + io.QUIET_MS - 1
    invoke(reader, fixed, now, context)
    assert len(reader.physical) == count
    now["ms"] = last + io.QUIET_MS
    invoke(reader, fixed, now, context)
    assert len(reader.physical) == count + 6
    assert state(context, now)["quota"]["slot"] == 1


def test_833_seed_succeeds_despite_capped_full_window_then_dense_tuple_page_continues(tmp_path):
    reader, fixed, now, context = setup(tmp_path, baseline_count=833)
    wallet = tape.MEMBERS[0]
    reader.histories[wallet] += [raw(20000 + i, T0 + 500) for i in range(1400)]
    invoke(reader, fixed, now, context)
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    saved = state(context, now)["engine_state"]["wallets"][":".join(wallet)]
    assert len(saved["rows"]) == 1834 and saved["open_page_window"] is not None
    upper = saved["open_page_window"]["requested_upper_ms"]
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    saved = state(context, now)["engine_state"]["wallets"][":".join(wallet)]
    assert len(saved["rows"]) == 2233 and saved["completed_through_ms"] == upper
    assert saved["open_page_window"] is None
    queries = [args for sql, args in reader.physical if sql == io.PAGE_SQL and tuple(args[:2]) == wallet]
    assert queries[1][3] == queries[0][3] and tuple(queries[1][4:6]) == (T0 + 500, 21000)


@pytest.mark.parametrize("fault", ["missing_store", "missing_genesis", "corrupt", "wrong_genesis", "source", "parent_baseline", "lock"])
def test_lost_or_conflicting_store_never_initializes_falls_back_or_reads(tmp_path, fault):
    reader, fixed, now, context = setup(tmp_path)
    store = context["store_dir"]
    if fault == "missing_store":
        (store / io.STORE_FILE).unlink()
    elif fault == "missing_genesis":
        (store / io.GENESIS_FILE).unlink()
    elif fault == "corrupt":
        (store / io.STORE_FILE).write_text("null")
    elif fault == "wrong_genesis":
        context["expected_genesis"] = "f" * 64
    elif fault == "source":
        value = json.loads((store / io.STORE_FILE).read_text())
        value["source_versions"]["learning/wallet_observation_io.py"] = "bad"
        (store / io.STORE_FILE).write_bytes(io._encoded(io._seal(value)))
    elif fault == "parent_baseline":
        fixed["selections"][":".join(tape.MEMBERS[0])]["baseline"]["rows"] += 1
    else:
        (store.parent / "worker.lock").write_text(json.dumps({"pid": os.getpid() + 1, "started_ms": T0}))
    counts = {}
    with pytest.raises(SourceDeferred):
        invoke(reader, fixed, now, context, review(counts=counts))
    assert not counts and not reader.calls


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_interruption_at_packet_commit_preserves_quota_cursor_and_exact_recovery(tmp_path, monkeypatch, committed, exception):
    reader, fixed, now, context = setup(tmp_path)
    original = io._atomic
    fired = []
    def interrupt(path, value, cap=io.MAX_STORE_BYTES):
        if path.name == io.STORE_FILE and value["quota"]["attempts"] and value["quota"]["attempts"][-1]["status"] == "observed" and not fired:
            fired.append(True)
            if committed:
                original(path, value, cap)
            raise exception()
        return original(path, value, cap)
    monkeypatch.setattr(io, "_atomic", interrupt)
    with pytest.raises(exception):
        invoke(reader, fixed, now, context)
    assert len(reader.physical) == 1
    saved = state(context, now)
    slot = saved["engine_state"]["wallets"][":".join(tape.MEMBERS[0])]
    assert slot["seed_status"] == ("verified" if committed else "unseeded")
    attempt = saved["quota"]["attempts"][0]
    assert attempt["status"] == ("observed" if committed else "admitted")
    monkeypatch.setattr(io, "_atomic", original)
    invoke(reader, fixed, now, context)
    assert sum(tuple(args[:2]) == tape.MEMBERS[0] for _, args in reader.physical) == 1


def test_missing_horizon_sets_quiet_frontier_no_wallet_queries_no_synthetic_zero(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    reader.horizon = None
    invoke(reader, fixed, now, context, review(failure=None))
    assert not reader.physical
    stored = state(context, now)
    assert stored["quota"]["metadata_retry_after_ms"] == now["ms"] + io.QUIET_MS
    initial = len([sql for sql, _ in reader.calls if sql == io.HORIZON_SQL])
    invoke(reader, fixed, now, context)
    assert len([sql for sql, _ in reader.calls if sql == io.HORIZON_SQL]) == initial + 1  # original collector fresh metadata only
    assert not reader.physical


def test_prepare_requires_parent_lock_and_never_replaces_existing_genesis(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    before = (context["store_dir"] / io.GENESIS_FILE).read_bytes()
    with pytest.raises(SourceDeferred, match="prepare_requires"):
        io.prepare_store(fixed, store_dir=context["store_dir"], learning_root=context["learning_root"],
                         now_ms=now["ms"], eligible_after_ms=now["ms"])
    assert (context["store_dir"] / io.GENESIS_FILE).read_bytes() == before
    other = context["learning_root"] / "missing-parent" / "wallet-observation-tape"
    with pytest.raises(SourceDeferred, match="artifact_missing"):
        io.prepare_store(fixed, store_dir=other, learning_root=context["learning_root"],
                         now_ms=now["ms"], eligible_after_ms=now["ms"])


def test_initial_quiet_frontier_defers_new_allowance(tmp_path):
    reader, fixed, now, context = setup(tmp_path, eligible_delay=io.QUIET_MS)
    invoke(reader, fixed, now, context)
    assert not reader.physical


def test_store_CAS_conflict_never_overwrites_external_state(tmp_path, monkeypatch):
    reader, fixed, now, context = setup(tmp_path)
    original = io._cas
    fired = []
    def conflict(root, value, expected):
        if value["quota"]["attempts"] and not fired:
            fired.append(True)
            external = copy.deepcopy(expected)
            external["marker"] = "preserve"
            io._atomic(root / io.STORE_FILE, io._seal(external))
        return original(root, value, expected)
    monkeypatch.setattr(io, "_cas", conflict)
    with pytest.raises(SourceDeferred, match="CAS_conflict"):
        invoke(reader, fixed, now, context)
    assert state(context, now)["marker"] == "preserve"
    assert not reader.physical


def test_actual_reserved_byte_budget_bounds_full_fixed_pages_and_dynamic_prefix(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    invoke(reader, fixed, now, context)
    now["ms"] += io.QUIET_MS
    for j, wallet in enumerate(tape.MEMBERS):
        reader.histories[wallet] += [raw(10000 + j * 2000 + i, T0 + 500) for i in range(1500)]
    dynamic = ("bsc", "0x" + "a" * 40)
    reader.histories[dynamic] = [raw(50000 + i, T0 + 500) for i in range(1500)]
    invoke(reader, fixed, now, context, review([{"chain": dynamic[0], "address": dynamic[1]}]))
    quota = state(context, now)["quota"]
    assert len(quota["attempts"]) <= 10
    assert sum(a["charged_rows"] for a in quota["attempts"]) <= 10000
    assert sum(a["charged_bytes"] for a in quota["attempts"]) <= 4 * 1024 * 1024
    assert quota["attempts"][-1]["status"] == "failed"
    assert quota["attempts"][-1]["charged_bytes"] == quota["attempts"][-1]["requested_limit"] * 4096
    assert report(context)["current_query_coverage"] == "UNPROVEN"


def test_late_baseline_backfill_is_unknown_without_replacing_seed(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    wallet = tape.MEMBERS[0]
    reader.histories[wallet].append(raw(90000, T0 - 50))
    invoke(reader, fixed, now, context)
    saved = state(context, now)
    slot = saved["engine_state"]["wallets"][":".join(wallet)]
    assert slot["seed_status"] == "unseeded" and slot["rows"] == []
    assert saved["quota"]["attempts"][0]["failure_type"] == "unproven_seed_limit_prefix"
    assert saved["engine_state"]["fixed_state"]["selections"] == fixed["selections"]


def test_retention_gap_after_partial_page_never_silently_advances_cursor(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    wallet = tape.MEMBERS[0]
    invoke(reader, fixed, now, context)
    reader.histories[wallet] += [raw(20000 + i, T0 + 500) for i in range(1400)]
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    saved = state(context, now)["engine_state"]["wallets"][":".join(wallet)]
    cursor = saved["page_cursor"]
    reader.horizon = now["ms"] + 1
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    after = state(context, now)
    assert after["engine_state"]["wallets"][":".join(wallet)]["page_cursor"] == cursor
    assert any(d["reason"] == "unfinished_interval_aged_out" and d["wallet"] == list(wallet) for d in after["diagnostics"])
    assert report(context)["current_query_coverage"] == "UNPROVEN"


def test_original_seed_older_than_thirteen_days_is_not_read_or_shortened(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    now["ms"] = T0 + 6 * ab.DAY_MS + 1
    invoke(reader, fixed, now, context)
    assert not reader.physical
    saved = state(context, now)
    assert all(s["seed_status"] == "unseeded" for s in saved["engine_state"]["wallets"].values())
    assert len(saved["diagnostics"]) == 6


def test_unexpected_wallet_sql_never_bypasses_budget(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    def rogue(adapter, *_args, **_kwargs):
        adapter.rows("select * from swaps limit 1")
    with pytest.raises(SourceDeferred, match="unexpected_wallet_query_shape"):
        invoke(reader, fixed, now, context, rogue)
    assert len(reader.physical) == 6  # only the permitted eager captures


def test_saturation_preserves_existing_bytes_and_exposes_unresolved_attempt(tmp_path, monkeypatch):
    reader, fixed, now, context = setup(tmp_path)
    original = io._atomic
    def cap_on_packet(path, value, cap=io.MAX_STORE_BYTES):
        if path.name == io.STORE_FILE and value["quota"]["attempts"] and value["quota"]["attempts"][-1]["status"] == "observed":
            raise SourceDeferred("wallet_tape:store_saturated_no_eviction")
        return original(path, value, cap)
    monkeypatch.setattr(io, "_atomic", cap_on_packet)
    with pytest.raises(SourceDeferred, match="saturated"):
        invoke(reader, fixed, now, context)
    monkeypatch.setattr(io, "_atomic", original)
    saved = state(context, now)
    assert len(reader.physical) == 1
    assert saved["quota"]["attempts"][0]["status"] == "admitted"
    assert all(s["rows"] == [] for s in saved["engine_state"]["wallets"].values())
    assert report(context)["current_query_coverage"] == "UNPROVEN"


def test_failed_reservation_cannot_be_refunded_by_resealing_state(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    reader.failure = lambda sql, args: SourceDeferred("offline") if sql == io.LEGACY_SQL else None
    invoke(reader, fixed, now, context)
    saved = state(context, now)
    saved["quota"]["attempts"][0]["charged_bytes"] = 0
    (context["store_dir"] / io.STORE_FILE).write_bytes(io._encoded(io._seal(saved)))
    with pytest.raises(SourceDeferred, match="refunded"):
        state(context, now)


def test_transient_seed_failure_is_repaired_only_by_verified_covering_packet(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    wallet = tape.MEMBERS[0]
    reader.failure = lambda sql, args: SourceDeferred("source_query_unavailable:fixture") if sql == io.LEGACY_SQL and tuple(args[:2]) == wallet else None
    invoke(reader, fixed, now, context)
    before = state(context, now)
    old_diagnostic = next(d for d in before["diagnostics"] if d["wallet"] == list(wallet))
    assert report(context)["current_query_coverage"] == "UNPROVEN"
    now["ms"] += io.QUIET_MS
    reader.failure = None
    invoke(reader, fixed, now, context)
    repaired = state(context, now)
    assert old_diagnostic in repaired["diagnostics"]
    assert any(r["diagnostic_sha256"] == io._sha(old_diagnostic) for r in repaired["resolutions"])
    assert not any(d.get("wallet") == list(wallet) for d in report(context)["diagnostics"])


def test_full_page_does_not_resolve_prior_missing_interval_but_complete_tail_can(tmp_path):
    reader, fixed, now, context = setup(tmp_path)
    wallet = tape.MEMBERS[0]
    invoke(reader, fixed, now, context)
    reader.histories[wallet] += [raw(20000 + i, T0 + 500) for i in range(1400)]
    reader.failure = lambda sql, args: SourceDeferred("source_query_unavailable:fixture") if sql == io.PAGE_SQL and tuple(args[:2]) == wallet else None
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    history = state(context, now)["diagnostics"]
    diagnostic = next(d for d in history if d["wallet"] == list(wallet))
    reader.failure = None
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    partial = state(context, now)
    assert not partial["resolutions"]
    assert diagnostic in report(context)["diagnostics"]
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    assert any(r["diagnostic_sha256"] == io._sha(diagnostic) for r in state(context, now)["resolutions"])


def test_lost_completion_receipt_requires_recovery_quiet_before_new_slot(tmp_path, monkeypatch):
    reader, fixed, now, context = setup(tmp_path)
    original = io._atomic
    def crash(path, value, cap=io.MAX_STORE_BYTES):
        if path.name == io.STORE_FILE and value["quota"]["attempts"] and value["quota"]["attempts"][-1]["status"] == "observed":
            raise SystemExit()
        return original(path, value, cap)
    monkeypatch.setattr(io, "_atomic", crash)
    with pytest.raises(SystemExit):
        invoke(reader, fixed, now, context)
    monkeypatch.setattr(io, "_atomic", original)
    now["ms"] += 2 * io.QUIET_MS
    invoke(reader, fixed, now, context)
    saved = state(context, now)
    assert saved["quota"]["slot"] == 0
    assert saved["quota"]["recovery_quiet_after_ms"] == now["ms"] + io.QUIET_MS
    now["ms"] += io.QUIET_MS
    invoke(reader, fixed, now, context)
    assert state(context, now)["quota"]["slot"] == 1
