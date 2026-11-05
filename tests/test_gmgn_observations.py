"""Original collector/books/provider wrapper, with disposable DB/cache only.

Reuses the existing prospective-observer fixture rather than inventing admission
or paper-book semantics. Fixture provider boundaries are explicitly identified.
"""
from __future__ import annotations

import copy
import json
import time

import pytest

from kaiba.learning import gmgn_observations as capture
from kaiba.learning import prospective_quotes as base
from kaiba.learning.gmgn_capture_store import CaptureStoreError
from kaiba.providers import gmgn_cli
from tests.test_prospective_quotes import TOKEN, actual_epoch, at, cached_market, limiter_state, read, write
from tests.test_prospective_quotes import setup as observer_setup  # noqa: F401


@pytest.fixture
def captured(request):
    s = request.getfixturevalue("observer_setup")
    s.collector = capture.CapturingQuoteCollector(s.research, s.root / "prospective-quotes",
        learning_root=s.root, clock=lambda: s.clock["ms"] / 1000, provider=s.provider)
    return s


def retained(s, report):
    result = report["attempt"]["cache"]["original_capture"]
    raw = s.collector.capture_store.get(result["reference"])
    return result, capture.validate_capsule(raw), raw


def test_real_pending_books_admission_and_original_payload_are_retained(captured):
    s = captured
    s.provider.change_data.update(buy_tax="0.0123", sell_tax="0", extra={"original": [1, False, None]})
    report = at(s)
    marker, capsule, raw = retained(s, report)
    assert report["status"] == "captured" and report["daily_admitted_attempts"] == 1
    assert len(s.provider.calls) == 1 and marker["status"] == "retained"
    assert capsule["admission"]["attempt"]["status"] == "admitted"
    assert capsule["admission"]["attempt"]["attempt_id"] == report["attempt"]["attempt_id"]
    assert capsule["target"] == report["attempt"]["target"]
    assert capsule["data"]["extra"] == {"original": [1, False, None]}
    assert capsule["data"]["buy_tax"] == "0.0123" and capsule["data"]["sell_tax"] == "0"
    assert set(capsule["receipt"]) == {"provider", "endpoint", "basis", "observed_at_ms", "request_digest", "response_digest", "note"}
    assert capsule["cache"]["fetched_ms"] == capsule["receipt"]["observed_at_ms"] == s.clock["ms"]
    assert capsule["sources"] == capture.source_bindings()
    # Ref-only metadata, not the private provider body/note, enters ordinary state.
    assert "extra" not in json.dumps(marker) and "0.0123" not in s.collector.state_path.read_text()
    capsule["data"]["extra"]["original"].append("changed")
    assert capture.validate_capsule(raw)["data"]["extra"] == {"original": [1, False, None]}
    assert s.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_unavailable_full_receipt_is_private_and_does_not_become_zero(captured):
    s = captured
    s.provider.available = False
    s.provider.receipt_changes["note"] = "private-fixture-note-do-not-export"
    report = at(s)
    marker, capsule, _ = retained(s, report)
    assert report["status"] == "unavailable" and marker["status"] == "retained_unqualified"
    assert capsule["receipt"]["basis"] == "unavailable"
    assert capsule["receipt"]["note"] == "private-fixture-note-do-not-export"
    assert capsule["cache"] is None
    assert "private-fixture-note-do-not-export" not in s.collector.state_path.read_text()
    assert len(s.provider.calls) == 1 and report["daily_admitted_attempts"] == 1


def test_store_failure_preserves_base_result_and_consumed_slot(captured, monkeypatch):
    s = captured
    def failed(raw):
        raise CaptureStoreError("storage_io_unknown")
    monkeypatch.setattr(s.collector.capture_store, "put", failed)
    report = at(s)
    assert report["status"] == "captured" and report["attempt"]["cache"]["verified"] is True
    assert report["attempt"]["cache"]["original_capture"]["status"] == "not_retained"
    assert report["daily_admitted_attempts"] == 1 and len(s.provider.calls) == 1
    again = s.collector.once()
    assert again["provider_attempts"] == 0 and again["daily_admitted_attempts"] == 1


def test_interrupt_after_provider_keeps_original_admission_without_resend(captured, monkeypatch):
    s = captured
    def interrupted(raw):
        raise KeyboardInterrupt
    monkeypatch.setattr(s.collector.capture_store, "put", interrupted)
    with pytest.raises(KeyboardInterrupt):
        at(s)
    state = read(s.collector.state_path)
    assert list(state["days"].values()) == [1] and state["attempts"][-1]["status"] == "admitted"
    assert not (s.collector.state_dir / "worker.lock").exists()
    report = s.collector.once()
    assert report["provider_attempts"] == 0 and len(s.provider.calls) == 1


def test_original_day_cap_precedes_targets_or_store_work(captured, monkeypatch):
    s = captured
    day = str(s.clock["ms"] // base.DAY_MS)
    write(s.collector.state_path, {"version": base.VERSION, "policy": base.POLICY, "days": {day: 100},
          "tokens": {}, "attempts": [], "updated_ms": 0})
    def forbidden(*args, **kwargs):
        pytest.fail("exhausted day must not scan, retain or call")
    monkeypatch.setattr(s.collector, "_targets", forbidden)
    monkeypatch.setattr(s.collector.capture_store, "put", forbidden)
    report = s.collector.once()
    assert report["reason"] == "daily_research_attempt_cap" and report["provider_attempts"] == 0
    assert not s.provider.calls and report["daily_admitted_attempts"] == 100


@pytest.mark.parametrize("change", ["ordinal", "target", "lock"])
def test_capture_requires_exact_original_durable_admission(captured, change):
    s = captured
    def race():
        if change == "lock":
            write(s.collector.state_dir / "worker.lock", {"pid": 1})
            return
        value = read(s.collector.state_path)
        if change == "ordinal":
            value["days"][str(s.clock["ms"] // base.DAY_MS)] += 1
        else:
            value["attempts"][-1]["target"]["book"] = "foreign.db"
        write(s.collector.state_path, value)
    s.provider.after = race  # Explicit concurrent-file corruption boundary, not a new producer.
    report = at(s)
    assert report["attempt"]["cache"]["original_capture"]["status"] == "not_retained"
    assert report["status"] == "captured" and len(s.provider.calls) == 1
    assert s.collector.capture_store.stats()["files"] == 0


def test_actual_gmgn_wrapper_one_spawn_same_kwargs_and_private_original_receipt(captured, monkeypatch):
    s = captured
    actual_epoch(s)
    limiter_state(s, monkeypatch)
    s.collector.provider, s.collector.clock = None, time.time
    calls = []
    data = {"address": TOKEN, "chain": "sol", "decimals": 6, "liquidity": "1000",
            "price": {"address": TOKEN, "price": "1"}, "buy_tax": "0.01", "sell_tax": "0"}
    monkeypatch.setattr(gmgn_cli, "cli_argv", lambda: ["offline-boundary"])
    def spawn(argv, timeout):
        state = read(s.collector.state_path)
        assert state["attempts"][-1]["status"] == "admitted" and list(state["days"].values()) == [1]
        assert read(s.collector.state_dir / "worker.lock")["pid"] > 0
        calls.append((argv, timeout))
        return gmgn_cli._Raw(0, json.dumps({"code": 0, "data": data}), "")
    monkeypatch.setattr(gmgn_cli, "_spawn", spawn)  # Only transport execution is doubled.
    report = s.collector.once()
    marker, capsule, _ = retained(s, report)
    assert marker["status"] == "retained" and report["provider_attempts"] == 1 and len(calls) == 1
    assert calls[0] == (["offline-boundary", "token", "info", "--address", TOKEN, "--chain", "sol", "--raw"], 6)
    assert capsule["data"] == data and capsule["receipt"]["basis"] == "provider_reported"
    assert capsule["receipt"]["observed_at_ms"] >= capsule["cache"]["fetched_ms"]
    assert capsule["admission"]["attempt"]["admitted_ms"] <= capsule["received_ms"] <= capsule["captured_ms"]
    assert s.db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='gmgn'").fetchone()[0] == 1


def test_actual_cached_race_preserves_old_receipt_and_has_no_spawn(captured, monkeypatch):
    s = captured
    actual = actual_epoch(s)
    limiter_state(s, monkeypatch)
    s.collector.provider, s.collector.clock = None, time.time
    original = s.collector._fresh_cache
    def race(target):
        assert original(target) is None
        cached_market(s, fetched=actual - 1000)
        return None  # Another ordinary caller populated cache after our first check.
    monkeypatch.setattr(s.collector, "_fresh_cache", race)
    monkeypatch.setattr(gmgn_cli, "_spawn", lambda *a: pytest.fail("actual cache hit must not spawn"))
    report = s.collector.once()
    marker, capsule, _ = retained(s, report)
    assert marker["status"] == "retained" and capsule["receipt"]["basis"] == "cached"
    assert capsule["cache"]["fetched_ms"] == capsule["receipt"]["observed_at_ms"] == actual - 1000
    assert capsule["receipt"]["observed_at_ms"] < capsule["admission"]["attempt"]["admitted_ms"]
    assert report["provider_attempts"] == report["daily_admitted_attempts"] == 1
    assert s.db.execute("SELECT COUNT(*) FROM provider_calls WHERE provider='gmgn'").fetchone()[0] == 0


@pytest.mark.parametrize("field", ["captured_ms", "ordinal", "cache_hash", "response_hash", "request_hash", "source", "bool_clock"])
def test_codec_rejects_broken_lineage_with_same_bad_input_positive_control(captured, field):
    s = captured
    _, valid, raw = retained(s, at(s))
    assert capture.validate_capsule(raw) == valid
    bad = copy.deepcopy(valid)
    if field == "captured_ms":
        bad["captured_ms"] = bad["received_ms"] - 1
    elif field == "ordinal":
        bad["admission"]["ordinal"] += 1
    elif field == "cache_hash":
        bad["cache"]["cache_sha256"] = "x" * 64
    elif field == "response_hash":
        bad["receipt"]["response_digest"] = "0" * 64
    elif field == "request_hash":
        bad["receipt"]["request_digest"] = "0" * 64
    elif field == "source":
        bad["sources"].pop(next(iter(bad["sources"])))
    else:
        bad["receipt"]["observed_at_ms"] = True
    with pytest.raises(capture.CapsuleError):
        capture.validate_capsule(json.dumps(bad, sort_keys=True, separators=(",", ":")).encode())


def test_codec_has_strict_bounded_json_and_no_raw_error_payload(captured):
    s = captured
    _, valid, raw = retained(s, at(s))
    for bad in (raw + b" ", b'{"version":1,"version":2}', b'{"x":NaN}', b"x" * (128 * 1024 + 1)):
        with pytest.raises(capture.CapsuleError) as error:
            capture.validate_capsule(bad)
        assert TOKEN not in str(error.value)
    valid["data"]["huge"] = "private-sentinel" * 20_000
    with pytest.raises(capture.CapsuleError) as error:
        capture.build_capsule(**{k: v for k, v in valid.items() if k != "version"})
    assert "private-sentinel" not in str(error.value)


def test_base_verifier_remains_the_final_cache_authority(captured, monkeypatch):
    s = captured
    original_put = s.collector.capture_store.put
    def changed_after_capture(raw):
        reference = original_put(raw)
        cached_market(s, data={"address": TOKEN, "decimals": 6, "liquidity": "99", "price": {"price": "2"}})
        return reference
    monkeypatch.setattr(s.collector.capture_store, "put", changed_after_capture)
    report = at(s)
    marker, capsule, _ = retained(s, report)
    assert report["status"] == "unavailable" and marker["status"] == "retained_unqualified"
    assert capsule["data"]["price"]["price"] == "1"


class QuoteRows:
    """Only the canonical quote reader boundary is doubled, not the quote adapter."""
    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def rows(self, sql, args):
        self.calls.append((sql, args))
        if sql.startswith("SELECT decimals,migrated_ms FROM tokens"):
            return [{"decimals": 6, "migrated_ms": 1}]
        if sql.startswith("SELECT built_at_ms,dossier_json FROM token_dossiers"):
            return []
        if sql.startswith("SELECT price_usd,ts_ms,source FROM native_prices"):
            return [{"price_usd": "100", "ts_ms": self.clock["ms"] - 1000, "source": "existing_fixture_native"}]
        raise AssertionError("unexpected quote source query")


@pytest.mark.parametrize("failure", [None, "foreign_reference", "allocation", "duplicate", "source"])
def test_bound_original_state_to_real_quote_path(captured, monkeypatch, failure):
    s = captured
    s.provider.change_data.update(buy_tax="0.01", sell_tax="0")
    report = at(s)
    marker, capsule, raw = retained(s, report)
    state = read(s.collector.state_path)
    if failure == "foreign_reference":
        state["attempts"][-1]["target"]["book"] = "another.db"
    elif failure == "duplicate":
        state["attempts"].append(copy.deepcopy(state["attempts"][-1]))
    elif failure == "source":
        monkeypatch.setattr(capture, "source_bindings", lambda: {**capsule["sources"], "extra": "0" * 64})
    if failure in ("foreign_reference", "duplicate"):
        write(s.collector.state_path, state)
    rows = QuoteRows(s.clock)
    quote = capture.quote_evidence(rows, "sol", TOKEN, now_ms=s.clock["ms"] + 10,
        max_age_ms=60_000, learning_root=s.root, observer_state_dir=s.collector.state_dir,
        expected_allocation_snapshot="foreign" if failure == "allocation" else None)
    assert quote["basis"] == "captured_gmgn_pool_depth" and quote["price_usd"] == "1"
    assert quote["price_ts_ms"] == quote["depth_ts_ms"] == capsule["cache"]["fetched_ms"]
    assert len(rows.calls) == 3
    if failure is None:
        assert quote["buy_tax_bps"] == 100 and quote["sell_tax_bps"] == 0
        assert quote["diagnostics"]["gmgn_original_capture"]["capsule_sha256"] == marker["reference"]["sha256"]
        assert s.collector.capture_store.get(marker["reference"]) == raw
    else:
        assert quote["buy_tax_bps"] is quote["sell_tax_bps"] is None


def test_genuine_source32_context_and_all_twenty_two_books_remain_bound(tmp_db, tmp_path, monkeypatch):
    from kaiba.providers import _http
    from tests.test_allocation_runtime import context, snapshot
    from tests.test_lesson_timing_upgrade import provision_lesson_runtime
    from tests.test_prospective_quotes import Provider

    value = provision_lesson_runtime(tmp_path, monkeypatch)
    clock = value["clock"]
    monkeypatch.setattr(_http, "now_ms", lambda: clock["ms"])
    before, authority = snapshot(value["root"]), context(value)
    assert len(authority.leased_pairs()) == 12 and len(authority.retired_history()) == 10
    provider = Provider(clock)
    collector = capture.CapturingQuoteCollector(value["root"], value["learning"] / "prospective-quotes",
        learning_root=value["learning"], clock=lambda: clock["ms"] / 1000, provider=provider)
    report = collector.once()
    assert report["status"] == "captured" and report["provider_attempts"] == 1 and len(provider.calls) == 1
    marker = report["attempt"]["cache"]["original_capture"]
    capsule = capture.validate_capsule(collector.capture_store.get(marker["reference"]))
    assert marker["status"] == "retained" and capsule["target"]["allocation_snapshot_token"] == authority.snapshot_token
    assert capsule["target"]["allocation_lease"] == "drain_only"
    assert context(value).snapshot_token == authority.snapshot_token
    after = snapshot(value["root"])
    assert {k: v for k, v in before.items() if not k.endswith("-shm")} == {
        k: v for k, v in after.items() if not k.endswith("-shm")}
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_original_tax_availability_cannot_turn_old_price_into_delayed_entry(captured, monkeypatch):
    from kaiba.providers import _http

    s = captured
    # Original market print is one millisecond before the real entry cutoff;
    # original tax Receipt arrives at the cutoff. Both clocks remain distinct.
    monkeypatch.setattr(_http, "now_ms", lambda: s.clock["ms"] - 1)
    s.provider.change_data.update(buy_tax="0", sell_tax="0")
    report = at(s)
    _, capsule, _ = retained(s, report)
    quote = capture.quote_evidence(QuoteRows(s.clock), "sol", TOKEN, now_ms=s.clock["ms"],
        max_age_ms=60_000, learning_root=s.root, observer_state_dir=s.collector.state_dir)
    cutoff = s.manifest["selected_ms"] + 1000 + 30_000
    assert capsule["cache"]["fetched_ms"] == quote["price_ts_ms"] == cutoff - 1
    assert quote["quote_ts_ms"] == capsule["receipt"]["observed_at_ms"] == cutoff
    result = s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): quote}, now_ms=s.clock["ms"])
    assert not result["errors"] and sum(arm["open"] for arm in result["arms"]) == 0
    # Explicit independent fresh-price control, not a forged capture reference.
    fresh = {**quote, "price_ts_ms": cutoff, "depth_ts_ms": cutoff,
             "source": "independent_fresh_price_fixture_control"}
    result = s.runner.run_candidates(s.manifest, [], {("sol", TOKEN): fresh}, now_ms=s.clock["ms"])
    assert not result["errors"] and sum(arm["open"] for arm in result["arms"]) > 0
