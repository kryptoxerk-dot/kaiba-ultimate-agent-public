"""Connected airdrop valuation boundaries, using disposable books and synthetic sources.

These checks exercise the real collector, refresh, persistence, digest, qualification
and planner. Source receipts below are explicit *fixture estimates*, not captures from
an issuer or proof that a live opportunity is profitable. No transport is permitted.

Ownership: Codex token delegate, AIRDROP-EV-SEMANTICS, 2026-10-18.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from kaiba.core import events
from kaiba.core.config import ChainBudget, RiskConfig
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.hunters import airdrops, digest, ev, qualification

FIXTURES = Path(__file__).parent / "fixtures" / "hunters"
CFG = ev.HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("25"))
RISK = RiskConfig(chains={Chain.SOL: ChainBudget(enabled=False, bankroll_base_units=1)})
VERIFY_FIELDS = (
    "official_source_verified", "eligibility_verified", "value_cost_verified",
    "execution_path_verified", "participation_open",
)


@pytest.fixture(autouse=True)
def no_transport(monkeypatch):
    """A fixture path must not silently fall back to a provider or executable."""
    def denied(*args, **kwargs):
        pytest.fail("airdrop integration test attempted external transport or subprocess")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)
    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)


@pytest.fixture
def at(monkeypatch):
    stamp = now_ms()
    monkeypatch.setattr(ev, "now_ms", lambda: stamp)
    monkeypatch.setattr(airdrops, "now_ms", lambda: stamp)
    monkeypatch.setattr(digest, "now_ms", lambda: stamp)
    monkeypatch.setattr(airdrops, "hunter_config", lambda: CFG)
    monkeypatch.setattr(qualification, "hunter_config", lambda: CFG)
    return stamp


def lead(at, **updates):
    body = {
        "kind": "points", "name": "Fixture Program", "chain": "sol",
        "points_program": True, "official_url": "https://issuer.example/program",
        "sources": ["fixture_catalog"], "first_seen_ms": at,
        "gas_cost_usd": "5", "time_cost_hours": 0,
        "meta": {"excerpt": "Points Season 1 is open", "cost_basis": "estimated"},
    }
    body.update(updates)
    return ev.OpportunityEvidence.model_validate(body)


def scenario(opp, at, *, verified=False):
    """50% of 1,000 immediate $1 units, 50% nothing; $5 expected outcome costs.

    Plus the explicit $5 participation gas, this gives $500 gross and $490 net.
    All probability, allocation, price and costs are declared synthetic estimates.
    """
    receipt = {
        "source_id": "fixture-issuer", "url": opp.official_url,
        "sha256": hashlib.sha256(b"synthetic issuer source fixture").hexdigest(),
        "observed_ms": at,
    }
    bundle = {
        "version": "airdrop-scenarios/1", "opportunity_id": opp.key, "chain": "sol",
        "observed_ms": at, "expires_ms": at + 3_600_000,
        "rules_source_id": receipt["source_id"], "cost_source_id": receipt["source_id"],
        "cost_inputs_sha256": ev.valuation_cost_binding(opp, CFG),
        "discount_rate_annual": "0", "sources": [receipt],
        "outcomes": [
            {
                "name": "allocation", "probability": "0.5",
                "probability_source_id": receipt["source_id"], "allocation_units": "1000",
                "allocation_source_id": receipt["source_id"],
                "unlocks": [{
                    "fraction": "1", "delay_days": 0, "price_usd": "1",
                    "price_source_id": receipt["source_id"], "terms_source_id": receipt["source_id"],
                }],
                "claim_cost_usd": "2", "sale_cost_usd": "3", "execution_loss_usd": "5",
                "cost_source_id": receipt["source_id"],
            },
            {
                "name": "no allocation", "probability": "0.5",
                "probability_source_id": receipt["source_id"], "allocation_units": "0",
                "allocation_source_id": receipt["source_id"], "unlocks": [],
                "claim_cost_usd": "0", "sale_cost_usd": "0", "execution_loss_usd": "0",
                "cost_source_id": receipt["source_id"],
            },
        ],
    }
    data = opp.model_dump(mode="json")
    data["valuation_scenarios"] = bundle
    if verified:
        current = ev.OpportunityEvidence.model_validate(data)
        score = ev.score_opportunity(current, CFG, at_ms=at)
        data["meta"]["qualification"] = {
            **dict.fromkeys(VERIFY_FIELDS, True), "observed_ms": at,
            "valuation_scenario_sha256": score.provenance["scenario_sha256"],
        }
    return ev.OpportunityEvidence.model_validate(data)


def refresh(conn, opps):
    report = {}
    written = airdrops.refresh(conn, {"fixture_catalog": lambda conn=None: opps}, CFG, report=report)
    assert written == len({opp.key for opp in opps})
    return report


def stored(conn, opp):
    row = conn.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (opp.key,)).fetchone()
    assert row is not None
    return dict(row)


def verdict(row, at):
    return qualification.qualification(row, at_ms=at, ev_floor=CFG.ev_threshold_usd)


def found(conn):
    return events.recent(kinds=[EventKind.HUNTER_FOUND.value], conn=conn)


def assert_unknown_open(conn, opp, at):
    row = stored(conn, opp)
    assert row["status"] == "open"
    assert row["ev_score"] is None and row["gross_usd"] is None
    assert Decimal(row["cost_usd"]) >= 0
    result = verdict(row, at)
    assert not result["qualified"]
    assert "ev_unavailable" in result["reasons"]
    assert result["funded_action_authorized"] is False
    plan = airdrops.build_plan(opp.key, conn, CFG)
    assert plan.refused and plan.total_capital_usd == 0
    assert plan.steps == [] and plan.executable_steps == []
    assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
    return row


def test_actual_catalog_unknowns_remain_open_and_digest_discoverable(tmp_db, at):
    page = json.loads((FIXTURES / "alphadrops_points.json").read_text(encoding="utf-8"))["body"]
    report = {}
    written = airdrops.refresh(
        tmp_db, {"alphadrops": lambda conn=None: airdrops.from_alphadrops(page, conn=conn)},
        CFG, report=report,
    )
    assert written == 3
    rows = [dict(row) for row in tmp_db.execute("SELECT * FROM opportunities")]
    assert len(rows) == 3 and all(row["status"] == "open" for row in rows)
    assert all(row["ev_score"] is None and row["gross_usd"] is None for row in rows)
    assert report["qualified_count"] == 0 and report["unqualified_count"] == 3
    assert report["refused_count"] == 0 and report["funded_action_authorized"] is False
    normal = qualification.opportunity_report(tmp_db, kind="airdrop", at_ms=at)
    audit = qualification.opportunity_report(tmp_db, kind="airdrop", include_unqualified=True, at_ms=at)
    assert normal["opportunities"] == [] and audit["registry_count"] == 3
    assert all(item["qualification"]["funded_action_authorized"] is False for item in audit["opportunities"])
    result = digest.build(tmp_db, now=at, per_kind=10, risk=RISK, sent={})
    assert {item["title"] for item in result["sections"]["airdrop"]["items"]} == {
        "Aster", "Meteora S2", "Variational",
    }
    for row in rows:
        plan = airdrops.build_plan(row["opportunity_id"], tmp_db, CFG)
        assert plan.refused and not plan.executable_steps and plan.total_capital_usd == 0
    assert found(tmp_db) == []


@pytest.mark.parametrize("hints", [
    {},
    {"confirmed_token": True, "vested_fraction": 0},
    {"expected_value_usd": "1000000", "prior_airdrop_history": ["1000000"]},
    {"source_count": 20, "sybil_risk": "low", "vested_fraction": 0.7},
])
def test_legacy_headlines_and_confidence_cannot_supply_missing_scenarios(tmp_db, at, hints):
    opp = lead(at, **hints)
    score = ev.score_opportunity(opp, CFG)
    assert score.ev_usd is None and score.gross_usd is None and not score.valuation_available
    assert score.model_version == "airdrop-ev/2" and score.missing_inputs
    assert not score.refused and not score.positive
    refresh(tmp_db, [opp])
    assert_unknown_open(tmp_db, opp, at)
    assert found(tmp_db) == []


def test_static_configuration_cannot_manufacture_unknown_dollars(tmp_db, at):
    opp = lead(at, confirmed_token=True, vested_fraction=0)
    arbitrary = CFG.model_copy(update={
        "fallback_value_usd": Decimal("999999999"), "p_confirmed": 1.0,
        "p_points": 1.0, "p_speculative": 1.0, "tge_capture": 1.0,
        "post_tge_survival": 1.0, "default_vested_fraction": 0.0,
    })
    score = ev.score_opportunity(opp, arbitrary)
    assert score.ev_usd is None and score.gross_usd is None and not score.valuation_available
    assert airdrops.refresh(tmp_db, {"fixture_catalog": lambda conn=None: [opp]}, arbitrary) == 1
    assert_unknown_open(tmp_db, opp, at)


def test_explicit_source_scenarios_price_but_do_not_verify_a_lead(tmp_db, at):
    opp = scenario(lead(at), at)
    score = ev.score_opportunity(opp, CFG)
    assert score.model_version == "airdrop-ev/2" and score.valuation_available
    assert score.gross_usd == Decimal("500.00") and score.ev_usd == Decimal("490.00")
    assert score.cost_usd == Decimal("10.00") and score.missing_inputs == []
    refresh(tmp_db, [opp])
    row = stored(tmp_db, opp)
    assert row["status"] == "open" and Decimal(str(row["ev_score"])) == Decimal("490")
    result = verdict(row, at)
    assert not result["qualified"] and result["funded_action_authorized"] is False
    assert "eligibility_verified_missing" in result["reasons"]
    assert airdrops.build_plan(opp.key, tmp_db, CFG).refused
    assert found(tmp_db) == []


def test_source_scenario_dollars_do_not_depend_on_legacy_sell_share_or_price_decline(tmp_db, at):
    opp = scenario(lead(at, sybil_risk="high", confirmed_token=True, vested_fraction=1), at)
    arbitrary = CFG.model_copy(update={
        "fallback_value_usd": Decimal("999999999"), "p_confirmed": 0.0,
        "p_points": 0.0, "p_speculative": 0.0, "tge_capture": 0.0,
        "post_tge_survival": 0.0, "default_vested_fraction": 1.0,
    })
    reference = ev.score_opportunity(opp, CFG)
    changed = ev.score_opportunity(opp, arbitrary)
    assert reference.ev_usd == changed.ev_usd == Decimal("490.00")
    assert reference.gross_usd == changed.gross_usd == Decimal("500.00")
    assert airdrops.refresh(tmp_db, {"fixture_catalog": lambda conn=None: [opp]}, arbitrary) == 1
    assert Decimal(str(stored(tmp_db, opp)["ev_score"])) == Decimal("490")


def test_fully_verified_source_scenario_passes_research_only_positive_control(tmp_db, at):
    opp = scenario(lead(at), at, verified=True)
    assert opp.expected_value_usd is None and not opp.prior_airdrop_history
    report = refresh(tmp_db, [opp])
    row = stored(tmp_db, opp)
    assert verdict(row, at) == {
        "qualified": True, "reasons": [], "classification": "qualified_research",
        "funded_action_authorized": False,
    }
    assert report["qualified_count"] == 1 and report["funded_action_authorized"] is False
    plan = airdrops.build_plan(opp.key, tmp_db, CFG)
    assert not plan.refused and plan.steps
    assert len(found(tmp_db)) == 1
    refresh(tmp_db, [opp])
    assert len(found(tmp_db)) == 1  # re-fetching a source is not another first crossing
    assert tmp_db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT count(*) FROM positions").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("ev_score", 491), ("cost_usd", "9.00")])
def test_stored_dollars_must_match_the_current_source_scenario(tmp_db, at, field, value):
    opp = scenario(lead(at), at, verified=True)
    refresh(tmp_db, [opp])
    assert verdict(stored(tmp_db, opp), at)["qualified"]
    # Field names are the closed literal parametrization above, never external input.
    tmp_db.execute(f"UPDATE opportunities SET {field}=? WHERE opportunity_id=?", (value, opp.key))
    result = verdict(stored(tmp_db, opp), at)
    assert not result["qualified"] and result["funded_action_authorized"] is False
    assert "valuation_result_stale_or_mismatched" in result["reasons"]
    refresh(tmp_db, [opp])
    assert verdict(stored(tmp_db, opp), at)["qualified"]


def test_changed_source_scenario_and_updated_dollars_cannot_reuse_old_verification(tmp_db, at):
    original = scenario(lead(at), at, verified=True)
    refresh(tmp_db, [original])
    assert verdict(stored(tmp_db, original), at)["qualified"]
    data = original.model_dump(mode="json")
    data["valuation_scenarios"]["outcomes"][0]["unlocks"][0]["price_usd"] = "2"
    data["valuation_scenarios"]["sources"][0]["sha256"] = "b" * 64
    changed = ev.OpportunityEvidence.model_validate(data)
    changed_score = ev.score_opportunity(changed, CFG, at_ms=at)
    assert changed_score.ev_usd == Decimal("990.00")
    assert changed.meta["qualification"] == original.meta["qualification"]
    assert changed_score.provenance["scenario_sha256"] != original.meta["qualification"]["valuation_scenario_sha256"]
    refresh(tmp_db, [changed])
    row = stored(tmp_db, changed)
    assert row["ev_score"] == 990 and row["cost_usd"] == "10.00"
    result = verdict(row, at)
    assert not result["qualified"] and result["funded_action_authorized"] is False
    assert "valuation_verification_scenario_mismatched" in result["reasons"]
    plan = airdrops.build_plan(changed.key, tmp_db, CFG)
    assert plan.refused and plan.total_capital_usd == 0 and not plan.steps
    assert len(found(tmp_db)) == 1
    # A fresh receipt for the actual changed scenario restores the positive control.
    data["meta"]["qualification"]["valuation_scenario_sha256"] = changed_score.provenance["scenario_sha256"]
    verified = ev.OpportunityEvidence.model_validate(data)
    refresh(tmp_db, [verified])
    assert verdict(stored(tmp_db, verified), at)["qualified"]
    assert not airdrops.build_plan(verified.key, tmp_db, CFG).refused
    assert tmp_db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0


def test_foreign_persisted_row_id_cannot_borrow_another_opportunitys_qualified_evidence(tmp_db, at):
    opp = scenario(lead(at), at, verified=True)
    refresh(tmp_db, [opp])
    assert verdict(stored(tmp_db, opp), at)["qualified"]
    foreign_id = "0" * 32
    assert foreign_id != opp.key
    tmp_db.execute("UPDATE opportunities SET opportunity_id=? WHERE opportunity_id=?", (foreign_id, opp.key))
    row = dict(tmp_db.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (foreign_id,)).fetchone())
    result = verdict(row, at)
    assert not result["qualified"] and result["funded_action_authorized"] is False
    assert "evidence_opportunity_id_mismatched" in result["reasons"]
    plan = airdrops.build_plan(foreign_id, tmp_db, CFG)
    assert plan.refused and plan.total_capital_usd == 0 and not plan.steps
    assert "evidence_opportunity_id_mismatched" in plan.refusal_reason
    assert tmp_db.execute("SELECT count(*) FROM orders").fetchone()[0] == 0
    assert tmp_db.execute("SELECT count(*) FROM positions").fetchone()[0] == 0


def test_explicit_loss_is_numeric_but_does_not_receive_capital_or_an_alert(tmp_db, at):
    data = scenario(lead(at), at, verified=True).model_dump(mode="json")
    data["valuation_scenarios"]["outcomes"][0]["allocation_units"] = "10"
    opp = ev.OpportunityEvidence.model_validate(data)
    score = ev.score_opportunity(opp, CFG)
    assert score.valuation_available and score.ev_usd == Decimal("-5.00")
    report = refresh(tmp_db, [opp])
    row = stored(tmp_db, opp)
    assert row["status"] == "open" and Decimal(str(row["ev_score"])) == Decimal("-5")
    assert "ev_below_floor" in verdict(row, at)["reasons"]
    assert report["qualified_count"] == 0 and report["funded_action_authorized"] is False
    plan = airdrops.build_plan(opp.key, tmp_db, CFG)
    assert plan.refused and not plan.steps and plan.total_capital_usd == 0
    assert found(tmp_db) == []


def test_bounded_large_decimal_scenario_cannot_crash_refresh_or_self_verify(tmp_db, at):
    data = scenario(lead(at), at).model_dump(mode="json")
    outcome = data["valuation_scenarios"]["outcomes"][0]
    outcome["allocation_units"] = "9999999999999999"
    outcome["unlocks"][0]["price_usd"] = "9999999999999999"
    opp = ev.OpportunityEvidence.model_validate(data)
    score = ev.score_opportunity(opp, CFG)
    assert score.ev_usd is None or score.ev_usd.is_finite()
    assert score.gross_usd is None or score.gross_usd.is_finite()
    refresh(tmp_db, [opp])
    row = stored(tmp_db, opp)
    assert row["status"] == "open" and not verdict(row, at)["qualified"]
    assert airdrops.build_plan(opp.key, tmp_db, CFG).refused
    assert found(tmp_db) == []


@pytest.mark.parametrize("fault", ["expired", "future", "identity", "chain", "cost_binding"])
def test_bound_scenario_replay_faults_stay_open_unqualified_and_unfunded(tmp_db, at, fault):
    opp = scenario(lead(at), at, verified=True)
    data = opp.model_dump(mode="json")
    bundle = data["valuation_scenarios"]
    if fault == "expired":
        bundle["observed_ms"] = at - 7_200_000
        bundle["expires_ms"] = at - 3_600_000
        bundle["sources"][0]["observed_ms"] = at - 7_200_000
    elif fault == "future":
        bundle["observed_ms"] = at + 1_000
        bundle["expires_ms"] = at + 3_600_000
        bundle["sources"][0]["observed_ms"] = at + 1_000
    elif fault == "identity":
        bundle["opportunity_id"] = "0" * 32
    elif fault == "chain":
        bundle["chain"] = "bsc"
    else:
        data["gas_cost_usd"] = "50"
    invalid = ev.OpportunityEvidence.model_validate(data)
    refresh(tmp_db, [invalid])
    assert_unknown_open(tmp_db, invalid, at)
    assert found(tmp_db) == []


@pytest.mark.parametrize("verification_fault", ["stale", "missing", "closed", "wrong_identity"])
def test_numeric_scenario_does_not_bypass_participation_receipt(tmp_db, at, verification_fault):
    opp = scenario(lead(at), at, verified=True)
    data = opp.model_dump(mode="json")
    verification = data["meta"]["qualification"]
    if verification_fault == "stale":
        verification["observed_ms"] = at - 2 * 86_400_000
    elif verification_fault == "missing":
        verification.pop("value_cost_verified")
    elif verification_fault == "closed":
        verification["participation_open"] = False
    else:
        data["chain"] = "bsc"
    invalid = ev.OpportunityEvidence.model_validate(data)
    refresh(tmp_db, [invalid])
    result = verdict(stored(tmp_db, invalid), at)
    assert not result["qualified"] and result["funded_action_authorized"] is False
    plan = airdrops.build_plan(invalid.key, tmp_db, CFG)
    assert plan.refused and not plan.steps and plan.total_capital_usd == 0
    assert found(tmp_db) == []


def seed_legacy(conn, opp, at, *, corrupt=False):
    old = opp.model_dump(mode="json")
    old.pop("valuation_scenarios", None)
    old["expected_value_usd"] = "500"
    old["meta"]["qualification"] = {
        **dict.fromkeys(VERIFY_FIELDS, True), "observed_ms": at - 2 * 86_400_000,
    }
    raw = "{not valid JSON" if corrupt else json.dumps(old, indent=2)
    conn.execute(
        "INSERT INTO opportunities "
        "(opportunity_id,kind,name,chain,url,status,ev_score,cost_usd,gross_usd,confidence,"
        "evidence_json,rationale_json,warnings_json,created_ms,updated_ms,last_seen_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (opp.key, opp.kind.value, opp.name, "sol", opp.link, "open", 98.0, "190.00", "288.00", 0.9,
         raw, '["old formula exact bytes"]', '["old warning exact bytes"]', at - 86_400_000,
         at - 3_600_000, at - 3_600_000),
    )
    return dict(conn.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (opp.key,)).fetchone())


@pytest.mark.parametrize("corrupt", [False, True])
def test_legacy_snapshot_survives_repeated_refresh_without_receipt_resurrection(tmp_db, at, corrupt):
    opp = lead(at)
    before = seed_legacy(tmp_db, opp, at, corrupt=corrupt)
    refresh(tmp_db, [opp])
    row = assert_unknown_open(tmp_db, opp, at)
    current = json.loads(row["evidence_json"])
    assert "qualification" not in current["meta"]
    assert row["created_ms"] == before["created_ms"] and row["updated_ms"] == at
    provenance = current["meta"]["valuation_provenance"]
    assert provenance["model_version"] == "airdrop-ev/2"
    legacy = provenance["legacy_v1"]
    assert legacy["model_version"] == "airdrop-ev/1" and legacy["observed_ms"] == before["updated_ms"]
    assert legacy["inputs"]["evidence_json"] == before["evidence_json"]
    for field in ("ev_score", "cost_usd", "gross_usd", "confidence", "rationale_json", "warnings_json", "status"):
        assert legacy["output"][field] == before[field]
    original_archive = deepcopy(legacy)
    refresh(tmp_db, [opp])
    again = json.loads(stored(tmp_db, opp)["evidence_json"])
    assert again["meta"]["valuation_provenance"]["legacy_v1"] == original_archive
    assert "qualification" not in again["meta"]
    assert not verdict(stored(tmp_db, opp), at)["qualified"] and found(tmp_db) == []


def test_new_scrape_cannot_replace_the_original_legacy_archive(tmp_db, at):
    opp = lead(at)
    before = seed_legacy(tmp_db, opp, at)
    refresh(tmp_db, [opp])
    archive = json.loads(stored(tmp_db, opp)["evidence_json"])["meta"]["valuation_provenance"]["legacy_v1"]
    assert archive["inputs"]["evidence_json"] == before["evidence_json"]
    data = opp.model_dump(mode="json")
    data["meta"]["valuation_provenance"] = {
        "legacy_v1": {"model_version": "airdrop-ev/1", "output": {"ev_score": 999999}},
    }
    forged = ev.OpportunityEvidence.model_validate(data)
    refresh(tmp_db, [forged])
    after = json.loads(stored(tmp_db, opp)["evidence_json"])
    assert after["meta"]["valuation_provenance"]["legacy_v1"] == archive
    assert_unknown_open(tmp_db, forged, at)


def test_refreshed_valuation_does_not_inherit_previous_verification(tmp_db, at):
    first = scenario(lead(at), at, verified=True)
    refresh(tmp_db, [first])
    assert verdict(stored(tmp_db, first), at)["qualified"]
    replacement = scenario(lead(at), at)
    refresh(tmp_db, [replacement])
    row = stored(tmp_db, replacement)
    assert Decimal(str(row["ev_score"])) == Decimal("490")
    assert "qualification" not in json.loads(row["evidence_json"])["meta"]
    assert not verdict(row, at)["qualified"]
    assert airdrops.build_plan(replacement.key, tmp_db, CFG).refused
    assert len(found(tmp_db)) == 1
