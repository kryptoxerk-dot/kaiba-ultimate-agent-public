"""Actual worker quote phase and PaperBroker; explicit Context seam unless named otherwise."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.learning import admission_paper as ap
from kaiba.learning import allocation_runtime, autolearn, autolearn_io, cooperative_reader, tax_quote
from kaiba.learning import strategy_campaign as sc
from kaiba.learning.signal_audit import ExitLadder
from tests.test_tax_quote import _seed_quote

T0 = 1_800_000_000_000
REGISTRY = "SELECT * FROM wallets WHERE chain=? AND address IN (?)"


@pytest.fixture(autouse=True)
def offline_and_immutable_leaf_sources(monkeypatch):
    raw9 = sc.source_versions()
    leaf = Path(tax_quote.__file__).read_bytes()
    assert hashlib.sha256(leaf).hexdigest() == "70f93099f7bf77e1f3ae83e3f1d33e817c63b298931529cccab2c95ce804da94"
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("external dispatch forbidden"))
    yield
    assert sc.source_versions() == raw9 and Path(tax_quote.__file__).read_bytes() == leaf


def packed(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def db_path(conn):
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def add_decision(conn, token, *, signal=T0 + 1000):
    conn.execute("INSERT INTO signals(signal_id,lane,chain,token,created_ms,payload_json) VALUES(?,?,?,?,?,?)",
        ("tax-signal", "sm-trenches", "robinhood", token, signal - 1, "{}"))
    conn.execute("INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,signals_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)", ("tax-entry", signal, "sm-trenches", "shadow", "robinhood", token,
                                    "enter", "1", '["tax-signal"]'))


def worker_seam(tmp_path, tmp_db, monkeypatch, *, tax_latch=True, cooperative=False):
    """Substitute only authority/assessment routing; quote, source, runner and broker are real.

    This one-arm disposable fixture is not a new production family or evidence of
    receipt authentication. Full-chain integration is tested separately below.
    """
    clock = {"ms": T0 + 31_000}
    root = tmp_path / "learning"
    learner = autolearn.AutoLearner(db_path(tmp_db), root / "autolearn", learning_root=root,
        groups=[("sm-trenches", "robinhood")], max_candidates=2,
        clock=lambda: clock["ms"] / 1000)
    manifest = ap.generate_admission_campaign([], ExitLadder(), now_ms=T0,
        groups=[("sm-trenches", "robinhood")], max_candidates=2)
    manifest["hypotheses"] = [h for h in manifest["hypotheses"] if h["spec"]["exit"]["hold_ms"] == 60_000]
    assert len(manifest["hypotheses"]) == 1
    manifest["campaign_id"] = sc._campaign_id(manifest)
    paper = ap.AdmissionPaperRunner(learner.state_dir / "paper", learning_root=root,
        max_stake_usd="30", clock=lambda: clock["ms"] / 1000)

    def assess(rows, *, now_ms, prior_state=None):
        local = ap.assess_admission_campaign(manifest, rows[manifest["campaign_id"]], now_ms=now_ms)
        return {"next_state": {}, "assessments": {manifest["campaign_id"]: local},
            "arms": [{**a, "campaign_id": manifest["campaign_id"]} for a in local["arms"]],
            "family_size": 1, "fixture_only": True, "status": "UNPROVEN"}

    context = SimpleNamespace(manifests=lambda: [manifest], runner=lambda: paper, assess=assess,
        authority={"generation": 1}, _revalidate_bindings=lambda **k: "fixture",
        service_upgrade={"fixture": "cooperative-only"} if cooperative else None,
        tax_quote_upgrade={"fixture": "authenticated-loader-contract-seam"} if tax_latch else None)
    monkeypatch.setattr(allocation_runtime, "load_context", lambda *a, **k: context)
    parent = {"version": autolearn.VERSION, "assessment": {}, "pending_lessons": [], "selected_ms": T0,
        "source_initialized": True, "decision_cursor": 0, "last_wallet_review_ms": clock["ms"],
        "last_agent_review_ms": clock["ms"], "last_economic_capture_ms": clock["ms"]}
    learner.state_path.write_bytes(packed(parent))
    calls = []
    original = tax_quote.quote_evidence

    def capture(reader, chain, token, **kwargs):
        assert not isinstance(reader, cooperative_reader.CooperativeReader), "quote tick needs its original bare reader"
        before = reader.queries
        packet = original(reader, chain, token, **kwargs)
        calls.append({"reader": reader, "packet": copy.deepcopy(packet), "queries": reader.queries - before,
                      "now_ms": kwargs["now_ms"]})
        return packet

    monkeypatch.setattr(tax_quote, "quote_evidence", capture)
    return learner, clock, manifest, calls


def quote(conn, clock, condition):
    tax = "100.00" if condition == "hundred" else "0.5" if condition == "nonintegral" else "NaN" if condition == "unsupported" else "0.00"
    stamp = clock["ms"] - 60_001 if condition == "stale" else clock["ms"] + 1 if condition == "future" else clock["ms"]
    return _seed_quote(conn, clock["ms"], tax, stamp=stamp,
                       basis="unavailable" if condition == "unavailable" else "provider_reported")


@pytest.mark.parametrize("condition,known,expected", [
    ("zero", True, 0), ("hundred", True, 100), ("stale", False, None),
    ("future", False, None), ("unavailable", False, None), ("nonintegral", False, None),
    ("unsupported", False, None),
])
def test_worker_captured_phase_through_real_adapter_and_broker_keeps_missing_venue_unknown(
        tmp_path, tmp_db, monkeypatch, condition, known, expected):
    learner, clock, manifest, calls = worker_seam(tmp_path, tmp_db, monkeypatch)
    token, raw = quote(tmp_db, clock, condition)
    add_decision(tmp_db, token)
    frozen = autolearn_io.GuardedReader(db_path(tmp_db))
    old = autolearn_io.quote_evidence(frozen, "robinhood", token, now_ms=clock["ms"], max_age_ms=60_000)
    first = learner.once()
    assert first["status"] == "complete" and not first["paper_errors"], first
    assert len(calls) == 1 and calls[0]["packet"]["buy_tax_bps"] == calls[0]["packet"]["sell_tax_bps"] == expected
    assert calls[0]["queries"] == frozen.queries
    assert {k: v for k, v in calls[0]["packet"].items() if k not in ("buy_tax_bps", "sell_tax_bps")} == {
        k: v for k, v in old.items() if k not in ("buy_tax_bps", "sell_tax_bps")}
    assert any(p["name"] == "captured_quotes" for p in first["phases"])
    assert first["campaigns"][0]["paper"]["arms"][0]["open"] == 1
    assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw
    clock["ms"] += 72_000
    _, raw = quote(tmp_db, clock, condition)
    second = learner.once()
    assert second["status"] == "complete" and not second["paper_errors"], second
    rows = second["campaigns"][0]["paper"]["outcomes"]
    assert len(rows) == 1
    row = rows[0]
    assert row["timing_complete"] and row["inventory_complete"] and row["commission_complete"]
    assert row["tax_complete"] is known and row["venue_complete"] is False
    assert row["cost_complete"] is row["evidence_complete"] is False
    assert row["live_cost_verified"] is False and row["cost_model_assumed"]
    assert all(e["tax_complete"] is known for e in row["fill_evidence"])
    assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    assert second["funding_allowed"] is False and manifest["selected_ms"] == T0


def test_unlatched_context_keeps_original_quote_without_adapter(tmp_path, tmp_db, monkeypatch):
    learner, clock, _, calls = worker_seam(tmp_path, tmp_db, monkeypatch, tax_latch=False)
    token, _ = quote(tmp_db, clock, "hundred")
    add_decision(tmp_db, token)
    report = learner.once()
    assert report["status"] == "complete" and calls == []
    assert report["quote_diagnostics"][0]["buy_tax_bps"] is None
    assert report["quote_diagnostics"][0]["sell_tax_bps"] is None


def test_only_tax_latch_preserves_parent_if_loader_refuses(tmp_path, tmp_db):
    root = tmp_path / "learning/autolearn"
    root.mkdir(parents=True)
    original = packed({"version": autolearn.VERSION, "assessment": {}, "pending_lessons": [],
                       "tax_quote_upgrade": {"invalid": True}})
    (root / "state.json").write_bytes(original)

    learner = autolearn.AutoLearner(db_path(tmp_db), root, learning_root=root.parent)
    result = learner.once()
    assert result["status"] == "partial" and result["source_queries"]["count"] == 0
    assert "parent_pointer_missing_or_stale" in result["input_error"]
    assert learner.state_path.read_bytes() == original and not (root / "paper").exists()


def test_cooperative_tick_uses_same_adapter_and_dossier_read_never_admits_callback(
        tmp_path, tmp_db, monkeypatch):
    learner, clock, _, calls = worker_seam(tmp_path, tmp_db, monkeypatch, cooperative=True)
    token, _ = quote(tmp_db, clock, "hundred")
    add_decision(tmp_db, token)
    original = cooperative_reader.PaperService

    class ClockedService(original):
        def __init__(self, callback, **kwargs):
            kwargs.update(monotonic_clock=lambda: clock["ms"] / 1000, wall_clock=lambda: clock["ms"] / 1000)
            super().__init__(callback, **kwargs)

    monkeypatch.setattr(cooperative_reader, "PaperService", ClockedService)
    inside = {"review": False}
    flush = learner._flush_lessons

    def guarded_flush(*a, **k):
        assert not inside["review"], "paper callback cannot ACK lessons"
        return flush(*a, **k)

    monkeypatch.setattr(learner, "_flush_lessons", guarded_flush)

    def review(reader, state, report, now):
        inside["review"] = True
        try:
            assert len(calls) == 1
            clock["ms"] += 74_000
            quote(tmp_db, clock, "hundred")
            reader.rows(tax_quote.DOSSIER_SQL, ("robinhood", token))
            assert len(calls) == 1, "a dossier query must not become a cooperative callback boundary"
            reader.rows(REGISTRY, ("robinhood", token))
            assert len(calls) == 2 and report["paper_tick_count"] == 2
            assert report["campaigns"][0]["paper"]["outcomes"][0]["tax_complete"] is True
        finally:
            inside["review"] = False

    monkeypatch.setattr(learner, "_scheduled_reviews", review)
    result = learner.once()
    assert result["status"] == "complete" and result["paper_service"]["counts"]["serviced"] == 2, result
    assert len(calls) == 2 and all(c["packet"]["buy_tax_bps"] == 100 for c in calls)
    assert calls[0]["reader"] is not calls[1]["reader"]
    row = result["campaigns"][0]["paper"]["outcomes"][0]
    assert row["cost_complete"] is False and row["venue_complete"] is False


def test_authenticated_tax_chain_real_twenty_two_family_preserves_history_and_cost_unknowns(
        tmp_path, tmp_db, monkeypatch):
    from tests.test_research_service_upgrade import originals
    from tests.test_tax_quote_upgrade import provision_tax_runtime

    value = provision_tax_runtime(tmp_path, monkeypatch)
    (value["root"] / "worker.lock").unlink()
    before = originals(value)
    service = (value["runtime_dir"] / "paper-service-upgrade/receipt.json").read_bytes()
    tax_files = {p: p.read_bytes() for p in value["tax_dir"].rglob("*") if p.is_file()}
    retired = {value["books"] / r["book"]: (value["books"] / r["book"]).read_bytes()
               for r in value["receipt"]["legacy_books"] if r["lease"] == "retired"}
    learner = autolearn.AutoLearner(db_path(tmp_db), value["root"], learning_root=value["learning"],
        clock=lambda: value["clock"]["ms"] / 1000)
    value["clock"]["ms"] = value["activation"] + 40_000
    context = allocation_runtime.load_context(value["root"], learning_root=value["learning"],
        now_ms=value["clock"]["ms"], clock=lambda: value["clock"]["ms"] / 1000)
    assert context.tax_quote_upgrade is not None and len(context.leased_pairs()) == 12
    assert len(context.retired_history()) == 10 and context.authority["activation_ms"] == value["activation"]
    token, raw = quote(tmp_db, value["clock"], "hundred")
    assert token == value["token"]
    add_decision(tmp_db, token, signal=value["activation"] + 1)
    opened = learner.once()
    assert opened["status"] == "complete" and not opened["paper_errors"], opened
    assert opened["allocation_assessment"]["family_size"] == 22
    assert opened["quote_diagnostics"][0]["buy_tax_bps"] == opened["quote_diagnostics"][0]["sell_tax_bps"] == 100
    new = next(c for c in opened["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    assert sum(a["open"] for a in new["paper"]["arms"]) > 0
    assert tmp_db.execute("SELECT dossier_json FROM token_dossiers").fetchone()[0] == raw
    value["clock"]["ms"] += 74_000
    quote(tmp_db, value["clock"], "hundred")
    closed = learner.once()
    assert closed["status"] == "complete" and closed["allocation_assessment"]["family_size"] == 22, closed
    new = next(c for c in closed["campaigns"] if c["campaign_id"] == value["new"]["campaign_id"])
    rows = new["paper"]["outcomes"]
    assert rows and all(r["tax_complete"] is True for r in rows)
    assert all(r["venue_complete"] is False and r["cost_complete"] is False for r in rows)
    assert all(r["live_cost_verified"] is False for r in rows)
    parent = json.loads(learner.state_path.read_bytes())
    for key in ("research_allocation", "paper_service_upgrade", "tax_quote_upgrade"):
        assert parent[key] == value["parent"][key]
    assert parent["selected_ms"] == value["parent"]["selected_ms"]
    assert originals(value) == before
    assert (value["runtime_dir"] / "paper-service-upgrade/receipt.json").read_bytes() == service
    assert all(p.read_bytes() == body for p, body in tax_files.items())
    assert all(p.read_bytes() == body for p, body in retired.items())
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
