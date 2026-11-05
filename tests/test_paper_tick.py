"""Pinned original-function equivalence and genuine disposable PAPER boundaries."""
from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import sqlite3
import textwrap
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.learning import paper_tick
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred

GOLDEN = Path(__file__).parent / "fixtures" / "paper_tick" / "f71b0a04_tick.py.txt"
GOLDEN_SHA256 = "80d4dc60a792969ad6efc3ed924331a0f4c1811960300bcad697901937fbb11d"
GOLDEN_AST_SHA256 = "ebfc71bc419f6a501b7ec39a16bdfa56c59df8dc4974796ecf75425c9fa0f598"
ORIGINAL_WORKER_SHA256 = "f71b0a04b19bfb08798d7adde4f068556ecb30c7f0d1d62fc24627821583abce"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))


def baseline(case):
    """Compile only the preserved original function, across future worker upgrades."""
    raw = GOLDEN.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_SHA256
    tick = ast.parse(raw).body[0]
    assert hashlib.sha256(ast.dump(tick, include_attributes=False).encode()).hexdigest() == GOLDEN_AST_SHA256
    wrapper = '''def factory():
    frontier_ms = case.frontier
    errors = case.initial_errors
    defer_maintenance = case.initial_defer
    qualified_lessons_by_campaign = case.qualified
    def current_ms():
        nonlocal frontier_ms
        frontier_ms = max(frontier_ms, case.raw_clock())
        case.events.append(("clock", frontier_ms))
        return frontier_ms
    def values():
        return frontier_ms, errors, defer_maintenance, qualified_lessons_by_campaign
'''
    wrapper += textwrap.indent(ast.unparse(tick), "    ")
    wrapper += "\n    return new_paper_tick, values, current_ms\n"
    env = {"case": case, "self": case.self_view(), "now": case.cycle_started,
        "state": case.state, "report": case.report, "paper": case.paper, "manifests": case.manifests,
        "allocated": case.allocated, "phase": case.phase, "read_opportunities": case.read,
        "quote_evidence": case.base_quote, "record_phase": case.record_phase,
        "atomic_json": case.atomic, "SourceDeferred": SourceDeferred, "Decimal": Decimal,
        "GuardedReader": GuardedReader, "time": SimpleNamespace(monotonic=case.monotonic)}
    exec(compile(wrapper, str(GOLDEN) + ":exact-nested-baseline", "exec"), env)
    function, values, clock = env["factory"]()
    case.active_clock = clock
    return function, values


def test_golden_original_fullworker_and_ast_provenance():
    raw = GOLDEN.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == GOLDEN_SHA256
    assert paper_tick.BASELINE_WORKER_SHA256 == ORIGINAL_WORKER_SHA256
    assert ("# Full worker SHA256 (LF): " + ORIGINAL_WORKER_SHA256).encode() in raw
    golden_node = ast.parse(raw).body[0]
    golden_ast = ast.dump(golden_node, include_attributes=False)
    assert hashlib.sha256(golden_ast.encode()).hexdigest() == GOLDEN_AST_SHA256
    current = Path(paper_tick.__file__).with_name("autolearn.py").read_bytes().replace(b"\r\n", b"\n")
    # While the predecessor is available, prove this is its exact nested AST.
    # Once a separately declared upgrade changes it, the pinned golden remains
    # the equivalence authority; never silently substitute the new worker body.
    if hashlib.sha256(current).hexdigest() == ORIGINAL_WORKER_SHA256:
        tree = ast.parse(current)
        worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AutoLearner")
        once = next(n for n in worker.body if isinstance(n, ast.FunctionDef) and n.name == "_once")
        nested = next(n for n in once.body if isinstance(n, ast.FunctionDef) and n.name == "new_paper_tick")
        assert ast.dump(nested, include_attributes=False) == golden_ast


class Case:
    def __init__(self, scenario="normal", *, allocated=False, tax=False, checkpoint=True):
        self.scenario, self.checkpoint = scenario, checkpoint
        self.wall = self.frontier = 2_000_000
        self.cycle_started = 1_990_000
        self.events, self.committed, self.writes = [], [], []
        self.initial_errors, self.initial_defer = ["previous_tick_error"], True
        self.qualified = {"stale-campaign": {"old": {"evidence_id": "old"}}}
        self.state = {"selected_ms": 1_900_000, "decision_cursor": 3, "assessment": {},
                      "pending_lessons": [], "last_paper_service_ms": 123,
                      "foreign_state": {"retained": ["original"]}}
        self.report = {"new_opportunities": 4, "phases": [], "paper_tick_count": 40,
                       "paper_ticks": [{"old": i} for i in range(32)]}
        self.manifests = [{"campaign_id": "first", "hypotheses": [{"hypothesis_id": "arm-first"}]},
                          {"campaign_id": "second", "hypotheses": [{"hypothesis_id": "arm-second"}]}]
        self.groups = [("sm-trenches", "sol"), ("migration-fade", "bsc"), ("sm-trenches", "sol")]
        self.reader = SimpleNamespace(guard=self.guard, q=self.query)
        self.paper = SimpleNamespace(expire_pending=self.expire, watched_tokens=self.watched, run_candidates=self.run)
        self.allocated = SimpleNamespace(assess=self.family, tax_quote_upgrade={} if tax else None) if allocated else None
        self.active_clock = self.current_ms
        self.tick = paper_tick.TickState(self.frontier, self.initial_errors, self.initial_defer, self.qualified)
        if scenario == "retry":
            self.state["retry_after_ms"] = self.wall + 10_000
        elif scenario in {"checkpoint_duplicate", "checkpoint_conflict"}:
            self.state["pending_lessons"] = [{"evidence_id": "old", "text": "same"},
                {"evidence_id": "old", "text": "same" if scenario == "checkpoint_duplicate" else "different"}]
        elif scenario == "lesson_conflict":
            self.state["pending_lessons"] = [{"evidence_id": "lesson-first", "text": "old incompatible body"}]
        elif scenario == "deferral_elapsed":
            self.state["maintenance_deferred_since_ms"] = self.wall - 120_000
        elif scenario == "already_initialized":
            self.state["source_initialized"] = True

    def raw_clock(self):
        self.wall += 7
        return self.wall

    def current_ms(self):
        self.frontier = max(self.frontier, self.raw_clock())
        self.events.append(("clock", self.frontier))
        return self.frontier

    def advance(self, value):
        self.frontier = max(self.frontier, value)

    def monotonic(self):
        self.events.append(("monotonic", self.wall / 1000))
        return self.wall / 1000

    @contextmanager
    def phase(self, name):
        self.events.append(("phase_begin", name, self.active_clock()))
        try:
            yield
        finally:
            self.events.append(("phase_end", name, self.active_clock()))

    def record_phase(self, value):
        self.report["phases"].append(copy.deepcopy(value))

    def guard(self):
        self.events.append(("guard",))
        if self.scenario == "guard_failure":
            raise SourceDeferred("wal_over_limit:1500000001")

    def query(self, sql, args):
        self.events.append(("query", sql, args))
        return [(7,)]

    def expire(self, manifest, *, now_ms):
        self.events.append(("expire", manifest["campaign_id"], now_ms))
        if self.scenario == "expiry_failure":
            raise ValueError("fixture_expiry_failure")
        return {"campaign_id": manifest["campaign_id"], "expired": 1}

    def read(self, reader, **kwargs):
        assert reader is self.reader
        self.events.append(("source", kwargs))
        if self.scenario == "source_failure":
            raise SourceDeferred("fixture_source_failure")
        return {"opportunities": [{"decision_id": "new", "chain": "sol", "token": "new-token"},
                                  {"decision_id": "old", "chain": "bsc", "token": "watch-bsc"}],
                "skipped": [{"decision_id": "skip", "reason": "old_reason"}],
                "next_rowid": 19, "source_rows": 3}

    def storage(self):
        self.events.append(("storage",))
        return 256 * 1024 * 1024 if self.scenario == "storage_full" else 100

    def watched(self, manifest, *, limit):
        cid = manifest["campaign_id"]
        self.events.append(("watched", cid, limit))
        if self.scenario == "many_watches":
            return [("sol", f"{cid}-{i}") for i in range(64)]
        return [("sol", "same-watch"), ("bsc", "watch-bsc")] if cid == "first" else [
            ("sol", "same-watch"), ("robinhood", "watch-rh")]

    def base_quote(self, reader, chain, token, **kwargs):
        return self.quote("base", reader, chain, token, **kwargs)

    def tax_quote(self, reader, chain, token, **kwargs):
        return self.quote("tax", reader, chain, token, **kwargs)

    def quote(self, adapter, reader, chain, token, **kwargs):
        assert reader is self.reader
        self.events.append(("quote", adapter, chain, token, kwargs))
        if self.scenario == "quote_failure" and token == "watch-rh":
            raise SourceDeferred("fixture_quote_failure")
        return {"native_usd": "100", "source": adapter, "quote_ts_ms": kwargs["now_ms"],
                "unavailable_reason": "fixture_missing" if self.scenario == "missing_quote" else None}

    def run(self, manifest, opportunities, quotes, *, now_ms):
        cid = manifest["campaign_id"]
        self.events.append(("run", cid, copy.deepcopy(opportunities), copy.deepcopy(quotes), now_ms))
        if self.scenario == "second_book_failure" and cid == "second":
            raise ValueError("fixture_second_book_failure")
        self.committed.append(cid)
        return {"outcomes": [{"outcome_id": cid}], "assessment_rows": [{"status":
            "pending" if self.scenario in {"pending", "deferral_elapsed"} else "closed", "row": cid}],
            "processing_frontier_ms": now_ms + (100_000 if cid == "first" else 0),
            "errors": ["fixture_partial"] if self.scenario == "partial" and cid == "first" else []}

    def assess(self, manifest, rows, **kwargs):
        self.events.append(("assess", manifest["campaign_id"], copy.deepcopy(rows), kwargs))
        return {"next_state": {"at_ms": kwargs["now_ms"]}, "status": "UNPROVEN",
                "arms": [{"hypothesis_id": "arm-" + manifest["campaign_id"], "status": "UNPROVEN"}]}

    def family(self, rows, **kwargs):
        self.events.append(("family", copy.deepcopy(rows), kwargs))
        if self.scenario == "family_failure":
            raise ValueError("fixture_family_failure")
        return {"next_state": {"family_size": 22}, "family_size": 22, "retired": 10,
                "assessments": {m["campaign_id"]: {"next_state": {"retained": True},
                    "arms": [{"hypothesis_id": "arm-" + m["campaign_id"], "status": "local_only"}]}
                    for m in self.manifests},
                "arms": [{"campaign_id": m["campaign_id"], "hypothesis_id": "arm-" + m["campaign_id"],
                          "status": "PAPER_EVIDENCE_READY" if i else "UNPROVEN"}
                         for i, m in enumerate(self.manifests)]}

    def lessons(self, manifest, result):
        self.events.append(("lessons", manifest["campaign_id"]))
        return [{"evidence_id": "lesson-" + manifest["campaign_id"], "text": "current body"}]

    def atomic(self, path, state):
        self.events.append(("atomic", str(path), copy.deepcopy(state)))
        if self.scenario == "checkpoint_failure":
            raise OSError("fixture_publication_failure")
        self.writes.append(copy.deepcopy(state))

    def self_view(self):
        return SimpleNamespace(groups=self.groups, max_stake_usd=Decimal("30"), _storage_bytes=self.storage,
            _assess=self.assess, _lessons=self.lessons, state_path=Path("fixture-state.json"))

    def arguments(self):
        return dict(state=self.state, report=self.report, tick=self.tick, paper=self.paper,
            manifests=self.manifests, allocated=self.allocated, groups=self.groups, max_stake_usd=Decimal("30"),
            cycle_started_ms=self.cycle_started, state_path=Path("fixture-state.json"),
            current_ms=self.current_ms, advance_frontier=self.advance, phase=self.phase, record_phase=self.record_phase,
            peek_frontier=lambda: self.frontier,
            read_opportunities=self.read, quote_source=self.base_quote, tax_quote_source=self.tax_quote,
            storage_bytes=self.storage, assess=self.assess, lessons=self.lessons, atomic_json=self.atomic,
            monotonic_clock=self.monotonic, checkpoint=self.checkpoint)


def execute(case, monkeypatch, *, extracted=False, function=None):
    if not extracted:
        from kaiba.learning import tax_quote
        monkeypatch.setattr(tax_quote, "quote_evidence", case.tax_quote)
        run, values = baseline(case)
        def call():
            return run(case.reader, checkpoint=case.checkpoint)
    else:
        def values():
            return (case.tick.frontier_ms, case.tick.errors, case.tick.defer_maintenance,
                    case.tick.qualified_lessons_by_campaign)

        def call():
            return (function or paper_tick.run_paper_tick)(case.reader, **case.arguments())
    try:
        result = call()
        outcome = ("return", result["status"])
    except (ValueError, OSError, SourceDeferred) as exc:
        result = None
        outcome = ("error", type(exc).__name__, str(exc))
    return {"outcome": outcome, "state": case.state, "report": case.report, "events": case.events,
            "committed": case.committed, "writes": case.writes, "closure": values()}, result


@pytest.mark.parametrize("scenario", [
    "normal", "retry", "expiry_failure", "guard_failure", "source_failure", "quote_failure",
    "storage_full", "partial", "missing_quote", "pending", "deferral_elapsed", "already_initialized",
    "many_watches", "checkpoint_duplicate", "checkpoint_conflict", "lesson_conflict", "second_book_failure",
    "checkpoint_failure", "family_failure",
])
@pytest.mark.parametrize(("allocated", "tax", "checkpoint"), [(False, False, False), (False, False, True),
                                                              (True, False, True), (True, True, True)])
def test_exact_old_function_equivalence(monkeypatch, scenario, allocated, tax, checkpoint):
    before, _ = execute(Case(scenario, allocated=allocated, tax=tax, checkpoint=checkpoint), monkeypatch)
    after, result = execute(Case(scenario, allocated=allocated, tax=tax, checkpoint=checkpoint), monkeypatch, extracted=True)
    assert after == before
    if result is not None:
        assert result["frontier_ms"] == after["closure"][0]
        assert result["errors"] is after["closure"][1]
        assert result["defer_maintenance"] is after["closure"][2]
        assert result["qualified_lessons_by_campaign"] is after["closure"][3]


def test_expiry_before_backoff_and_exception_visible_original_values(monkeypatch):
    case = Case("retry")
    before, _ = execute(case, monkeypatch, extracted=True)
    assert before["outcome"] == ("error", "SourceDeferred", "source_retry_backoff")
    assert [e[1] for e in case.events if e[0] == "expire"] == ["first", "second"]
    assert not any(e[0] in {"guard", "query", "source", "quote", "atomic"} for e in case.events)
    assert case.tick.errors == ["previous_tick_error"] and case.tick.defer_maintenance
    assert case.tick.qualified_lessons_by_campaign is case.qualified and not case.qualified


def test_partial_book_failure_keeps_prior_commit_and_current_qualification(monkeypatch):
    case = Case("second_book_failure", allocated=True)
    packet, _ = execute(case, monkeypatch, extracted=True)
    assert packet["outcome"] == ("error", "ValueError", "fixture_second_book_failure")
    assert case.committed == ["first"] and not case.writes
    assert case.state["decision_cursor"] == 7
    assert case.tick.qualified_lessons_by_campaign == {"first": {
        "lesson-first": {"evidence_id": "lesson-first", "text": "current body"}}}
    assert case.tick.frontier_ms > 2_090_000 and case.tick.errors == []


@pytest.mark.parametrize(("needle", "replacement", "scenario"), [
    ("    tick_reader.guard()", "    pass", "normal"),
    ("    if not tick.errors:\n        state[\"decision_cursor\"]", "    if True:\n        state[\"decision_cursor\"]", "partial"),
    ("        advance_frontier(tick.frontier_ms)", "        pass", "normal"),
    ("    qualified.clear()", "    pass", "normal"),
    ("            selected_quote = tax_quote_source", "            selected_quote = quote_source", "normal"),
    ("        tick.frontier_ms = max(tick.frontier_ms, peek_frontier())", "        pass", "expiry_failure"),
])
def test_removed_guard_or_semantic_mutation_is_detected(monkeypatch, needle, replacement, scenario):
    source = inspect.getsource(paper_tick)
    assert source.count(needle) == 1
    namespace = {"__name__": paper_tick.__name__}
    exec(compile(source.replace(needle, replacement, 1), "mutated-paper-tick", "exec"), namespace)
    expected, _ = execute(Case(scenario, allocated=True, tax=True), monkeypatch)
    altered, _ = execute(Case(scenario, allocated=True, tax=True), monkeypatch,
                          extracted=True, function=namespace["run_paper_tick"])
    assert altered != expected


def test_required_tax_adapter_cannot_silently_fall_back_to_base():
    case = Case(allocated=True, tax=True)
    arguments = case.arguments()
    arguments["tax_quote_source"] = None
    with pytest.raises(ValueError, match="required_tax_quote_source_missing"):
        paper_tick.run_paper_tick(case.reader, **arguments)
    assert not any(event[0] == "quote" for event in case.events)
    assert case.tick.errors == ["previous_tick_error"] and case.tick.qualified_lessons_by_campaign == {}
    # The same missing optional adapter is harmless when the original condition
    # selects the base source; source selection is Context-driven, not a flag.
    case = Case(allocated=True, tax=False)
    arguments = case.arguments()
    arguments["tax_quote_source"] = None
    assert paper_tick.run_paper_tick(case.reader, **arguments)["status"] == "complete"
    assert {event[1] for event in case.events if event[0] == "quote"} == {"base"}


def genuine_tick_fixture(tmp_path, tmp_db, monkeypatch):
    """Use the original installer/Context/runner; replace no validation function."""
    from kaiba.learning import autolearn, autolearn_io, tax_quote
    from tests.test_allocation_runtime import context
    from tests.test_lesson_timing_upgrade import provision_lesson_runtime

    value = provision_lesson_runtime(tmp_path, monkeypatch)
    state = json.loads((value["root"] / "state.json").read_bytes())
    clock = {"frontier": value["clock"]["ms"]}
    tick = paper_tick.TickState(clock["frontier"], qualified_lessons_by_campaign={"stale": {"old": {}}})
    allocated = context(value)
    token = allocated.snapshot_token
    readers, writes, faults = [], [], {"checkpoint": False, "complete_fee": False}

    def current_ms():
        clock["frontier"] = max(clock["frontier"], value["clock"]["ms"])
        return clock["frontier"]

    def advance_frontier(frontier):
        clock["frontier"] = max(clock["frontier"], frontier)

    def atomic(path, packet):
        if faults["checkpoint"]:
            raise OSError("fixture_after_book_commit_before_parent")
        autolearn.atomic_json(path, packet)
        writes.append(copy.deepcopy(packet))

    def fixture_quote(adapter, *args, **kwargs):
        quote = adapter(*args, **kwargs)
        if faults["complete_fee"] and not quote.get("unavailable_reason"):
            # Explicit synthetic positive-control component; never infer it from
            # a provider label, liquidity, raw fee ratio or missing field. This
            # establishes only the unchanged PAPER model's lesson eligibility.
            quote.update(venue_fee_bps=5, venue_fee_source="synthetic-test-venue-receipt",
                         venue_fee_ts_ms=quote["quote_ts_ms"])
        return quote

    def run():
        report = {"new_opportunities": 0, "phases": []}

        @contextmanager
        def phase(name):
            began = current_ms()
            try:
                yield
            finally:
                report["phases"].append({"name": name, "started_ms": began, "completed_ms": current_ms()})

        database = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        reader = autolearn.PollingReader(database)
        readers.append(reader)
        result = paper_tick.run_paper_tick(reader, state=state, report=report, tick=tick,
            paper=allocated.runner(), manifests=allocated.manifests(), allocated=allocated,
            groups=[("sm-trenches", c) for c in ("sol", "bsc", "robinhood")], max_stake_usd=Decimal("30"),
            cycle_started_ms=current_ms(), state_path=value["root"] / "state.json", current_ms=current_ms,
            advance_frontier=advance_frontier, peek_frontier=lambda: clock["frontier"], phase=phase,
            record_phase=lambda packet: report["phases"].append(packet),
            read_opportunities=autolearn_io.read_opportunities,
            quote_source=lambda *a, **k: fixture_quote(autolearn_io.quote_evidence, *a, **k),
            tax_quote_source=lambda *a, **k: fixture_quote(tax_quote.quote_evidence, *a, **k),
            storage_bytes=lambda: sum(p.stat().st_size for p in value["root"].rglob("*")
                                      if p.is_file() and not p.is_symlink()),
            assess=lambda *a, **k: pytest.fail("allocated tick must use joint family assessment"),
            lessons=lambda manifest, packet: autolearn.AutoLearner._lessons(None, manifest, packet),
            atomic_json=atomic, checkpoint=True)
        return result, report

    return SimpleNamespace(value=value, state=state, allocated=allocated, token=token, tick=tick,
                           readers=readers, writes=writes, faults=faults, run=run)


def test_real_source32_books_admit_then_close_with_original_family_and_checkpoint(tmp_path, tmp_db, monkeypatch):
    from tests.test_allocation_runtime import snapshot
    from tests.test_autolearn_allocation import canonical_quote
    from tests.test_research_service_upgrade import originals

    case = genuine_tick_fixture(tmp_path, tmp_db, monkeypatch)
    value = case.value
    artifacts = originals(value)
    retired = {record["book"]: (value["books"] / record["book"]).read_bytes()
               for record in value["receipt"]["legacy_books"] if record["lease"] == "retired"}
    assert len(retired) == 10 and len(case.allocated.leased_pairs()) == 12
    assert sum(len(m["hypotheses"]) for m in case.allocated.manifests()) == 22
    sources = snapshot(value["code"])
    parent_before = copy.deepcopy(case.state)
    signal = value["clock"]["ms"] + 1
    value["clock"]["ms"] = signal + 40_000
    tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES(?,?,?,?,?,?)",
        ("tick-signal", "sm-trenches", "robinhood", value["token"], signal - 1, "{}"))
    tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)", ("tick-entry", signal, "sm-trenches", "shadow", "robinhood", value["token"],
                                    "enter", "1", '["tick-signal"]'))
    canonical_quote(tmp_db, value)
    result, opened = case.run()
    assert result["status"] == "complete", opened
    assert opened["new_opportunities"] == 1 and case.state["decision_cursor"] > 0
    assert opened["allocation_assessment"]["family_size"] == 22
    new_rows = next(c["paper"]["assessment_rows"] for c in opened["campaigns"]
                    if c["campaign_id"] == value["new"]["campaign_id"])
    assert any(r["status"] == "open" and r["chain"] == "robinhood" for r in new_rows)
    assert "stale" not in case.tick.qualified_lessons_by_campaign
    assert json.loads((value["root"] / "state.json").read_bytes()) == case.state == case.writes[-1]

    value["clock"]["ms"] += 74_000
    canonical_quote(tmp_db, value)
    result, closed = case.run()
    assert result["status"] == "complete", closed
    assert closed["new_opportunities"] == 0
    assert closed["allocation_assessment"]["family_size"] == 22
    assert any(r["status"] == "closed" for c in closed["campaigns"]
               if c["campaign_id"] == value["new"]["campaign_id"] for r in c["paper"]["assessment_rows"])
    assert not case.tick.qualified_lessons_by_campaign[value["new"]["campaign_id"]]
    assert len(case.readers) == 2 and case.readers[0] is not case.readers[1]
    assert all(reader.queries > 0 for reader in case.readers)
    assert case.allocated.revalidate() == case.token
    assert originals(value) == artifacts and snapshot(value["code"]) == sources
    assert all((value["books"] / name).read_bytes() == raw for name, raw in retired.items())
    assert all(case.state[k] == v for k, v in parent_before.items() if k in {
        "campaign_files", "selected_ms", "research_allocation", "paper_service_upgrade", "tax_quote_upgrade",
        "configured_receipts_upgrade", "lesson_timing_upgrade", "clock_upgrade"})
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["retired", "lock", "parent", "source"])
def test_real_bound_runner_refuses_corruption_before_source_read_or_checkpoint(tmp_path, tmp_db, monkeypatch, failure):
    from tests.test_allocation_runtime import snapshot

    case = genuine_tick_fixture(tmp_path, tmp_db, monkeypatch)
    value = case.value
    if failure == "retired":
        record = next(r for r in value["receipt"]["legacy_books"] if r["lease"] == "retired")
        path = value["books"] / record["book"]
        path.write_bytes(path.read_bytes() + b" ")
    elif failure == "lock":
        (value["root"] / "worker.lock").unlink()
    elif failure == "parent":
        parent = copy.deepcopy(case.state)
        del parent["research_allocation"]
        (value["root"] / "state.json").write_text(json.dumps(parent), encoding="utf-8")
    else:
        path = value["code"] / "kaiba" / "learning" / "candidate_paper.py"
        path.write_bytes(path.read_bytes() + b"\n# foreign source\n")
    before = snapshot(value["root"])
    with pytest.raises(ValueError):
        case.run()
    assert len(case.readers) == 1 and case.readers[0].queries == 0
    assert not case.writes and snapshot(value["root"]) == before
    assert case.tick.qualified_lessons_by_campaign == {}


def test_real_book_commit_before_parent_failure_replays_without_duplicate_orders(tmp_path, tmp_db, monkeypatch):
    from tests.test_autolearn_allocation import canonical_quote

    case = genuine_tick_fixture(tmp_path, tmp_db, monkeypatch)
    case.faults["complete_fee"] = True
    value = case.value
    original_parent = (value["root"] / "state.json").read_bytes()
    signal = value["clock"]["ms"] + 1
    value["clock"]["ms"] = signal + 40_000
    tmp_db.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES(?,?,?,?,?,?)",
        ("crash-signal", "sm-trenches", "robinhood", value["token"], signal - 1, "{}"))
    tmp_db.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)", ("crash-entry", signal, "sm-trenches", "shadow", "robinhood", value["token"],
                                    "enter", "1", '["crash-signal"]'))
    canonical_quote(tmp_db, value)

    def ledger_orders():
        result = {}
        for record in case.allocated.leased_pairs():
            path = Path(record["book_path"])
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                result[path.name] = conn.execute("SELECT order_id,side,created_ms FROM orders ORDER BY order_id").fetchall()
            finally:
                conn.close()
        return result

    before = ledger_orders()
    case.faults["checkpoint"] = True
    with pytest.raises(OSError, match="after_book_commit_before_parent"):
        case.run()
    committed = ledger_orders()
    assert sum(map(len, committed.values())) > sum(map(len, before.values()))
    assert (value["root"] / "state.json").read_bytes() == original_parent and not case.writes
    assert case.state["decision_cursor"] > json.loads(original_parent)["decision_cursor"]
    # Simulate restart from the durable parent, while retaining real committed books.
    case.state.clear()
    case.state.update(json.loads(original_parent))
    case.faults["checkpoint"] = False
    value["clock"]["ms"] += 1000
    canonical_quote(tmp_db, value)
    result, report = case.run()
    assert result["status"] == "complete", report
    assert ledger_orders() == committed
    assert report["allocation_assessment"]["family_size"] == 22
    assert case.allocated.revalidate() == case.token
    assert json.loads((value["root"] / "state.json").read_bytes()) == case.state == case.writes[-1]
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    # Close commits are distinct from entry idempotency: the current actual
    # _lessons output must survive a lost parent publication via reconstruction.
    durable_before_close = (value["root"] / "state.json").read_bytes()
    value["clock"]["ms"] += 74_000
    canonical_quote(tmp_db, value)
    case.faults["checkpoint"] = True
    with pytest.raises(OSError, match="after_book_commit_before_parent"):
        case.run()
    closed_orders = ledger_orders()
    assert sum(map(len, closed_orders.values())) > sum(map(len, committed.values()))
    new_id = value["new"]["campaign_id"]
    current_lessons = copy.deepcopy(case.tick.qualified_lessons_by_campaign[new_id])
    assert current_lessons
    assert all(json.loads(lesson["body"])["type"] == "prospective_paper_outcome"
               for lesson in current_lessons.values())
    assert (value["root"] / "state.json").read_bytes() == durable_before_close
    case.state.clear()
    case.state.update(json.loads(durable_before_close))
    case.faults["checkpoint"] = False
    value["clock"]["ms"] += 1000
    canonical_quote(tmp_db, value)
    result, report = case.run()
    assert result["status"] == "complete", report
    assert ledger_orders() == closed_orders
    assert case.tick.qualified_lessons_by_campaign[new_id] == current_lessons
    pending = case.state["pending_lessons"]
    for eid, lesson in current_lessons.items():
        assert [row for row in pending if row["evidence_id"] == eid] == [lesson]
    assert json.loads((value["root"] / "state.json").read_bytes()) == case.state == case.writes[-1]
    assert case.allocated.revalidate() == case.token
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
