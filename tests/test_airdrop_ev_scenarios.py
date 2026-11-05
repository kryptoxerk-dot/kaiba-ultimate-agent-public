"""Offline, hand-computed source scenarios; no claims, wallets or provider calls."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from kaiba.core.config import ChainBudget, RiskConfig
from kaiba.core.schemas import Chain
from kaiba.hunters import airdrops, digest
from kaiba.hunters.ev import (
    AIRDROP_EV_VERSION,
    AirdropOutcome,
    AirdropSource,
    AirdropUnlock,
    AirdropValuation,
    EvScore,
    HunterConfig,
    OpportunityEvidence,
    OpportunityKind,
    rank,
    score_opportunity,
    valuation_cost_binding,
)

AT = 2_000_000_000_000
CFG = HunterConfig(operator_hourly_usd=Decimal(50))


def lead(**changes) -> OpportunityEvidence:
    fields = {
        "kind": OpportunityKind.AIRDROP, "name": "Fixture organic allocation",
        "chain": Chain.ETH, "official_url": "https://protocol.example/rules",
        "gas_cost_usd": Decimal(10), "capital_required_usd": Decimal(1000),
        "capital_lockup_days": 365, "time_cost_hours": 2,
    }
    fields.update(changes)
    return OpportunityEvidence(**fields)


def scenario(opp: OpportunityEvidence, cfg: HunterConfig = CFG) -> AirdropValuation:
    source = AirdropSource(source_id="rules", url="https://protocol.example/rules",
                           sha256="a" * 64, observed_ms=AT - 1000)
    paid = AirdropOutcome(
        name="allocation and sale", probability="0.5", probability_source_id="rules",
        allocation_units=1000, allocation_source_id="rules",
        unlocks=[
            AirdropUnlock(fraction="0.5", delay_days=0, price_usd=2,
                          price_source_id="rules", terms_source_id="rules"),
            AirdropUnlock(fraction="0.5", delay_days=365, price_usd=1,
                          price_source_id="rules", terms_source_id="rules"),
        ],
        claim_cost_usd=2, sale_cost_usd=3, execution_loss_usd=5, cost_source_id="rules",
    )
    zero = AirdropOutcome(
        name="zero allocation", probability="0.5", probability_source_id="rules",
        allocation_units=0, allocation_source_id="rules", unlocks=[],
        claim_cost_usd=0, sale_cost_usd=0, execution_loss_usd=0, cost_source_id="rules",
    )
    return AirdropValuation(
        version="airdrop-scenarios/1", opportunity_id=opp.key, chain=opp.chain,
        observed_ms=AT, expires_ms=AT + 3_600_000, rules_source_id="rules",
        cost_source_id="rules", cost_inputs_sha256=valuation_cost_binding(opp, cfg),
        discount_rate_annual="0.1", sources=[source], outcomes=[paid, zero],
    )


def valued(**changes) -> OpportunityEvidence:
    opp = lead(**changes)
    return opp.model_copy(update={"valuation_scenarios": scenario(opp)})


def test_explicit_joint_outcomes_unlock_prices_and_all_costs_have_hand_computed_ev():
    # .5*(500*$2 + 500*$1/1.1) = $727.2727 proceeds.
    # $10 gas + $80 carry + $100 hours + .5*($2+$3+$5) = $195.
    score = score_opportunity(valued(), CFG, at_ms=AT)
    assert score.gross_usd == Decimal("727.27")
    assert score.cost_usd == Decimal("195.00")
    assert score.ev_usd == Decimal("532.27")
    assert score.valuation_available and score.positive
    assert score.model_version == AIRDROP_EV_VERSION
    assert score.p_token is None and score.haircut is None
    assert score.missing_inputs == []
    assert score.provenance["scenario_version"] == "airdrop-scenarios/1"
    assert score.provenance["assumptions"] == "declared estimates"
    assert any("not promised payouts" in line for line in score.rationale)


@pytest.mark.parametrize("hints", [
    {}, {"confirmed_token": True}, {"points_program": True},
    {"expected_value_usd": Decimal(1_000_000)},
    {"prior_airdrop_history": [Decimal(400), Decimal(600)], "vested_fraction": 0},
])
def test_legacy_headlines_and_labels_cannot_supply_missing_dollar_proceeds(hints):
    score = score_opportunity(lead(**hints), CFG, at_ms=AT)
    assert score.ev_usd is None and score.gross_usd is None
    assert score.cost_usd == Decimal("190.00")
    assert not score.refused and not score.positive and not score.valuation_available
    assert "source_bound_scenarios_missing" in score.missing_inputs


def test_legacy_sell_share_decline_incidence_and_static_probabilities_are_not_money():
    opp = valued(confirmed_token=True, points_program=True, vested_fraction=1)
    obsolete_changes = CFG.model_copy(update={
        "tge_capture": 0.01, "post_tge_survival": 0.99, "p_confirmed": 0,
        "p_points": 0, "p_speculative": 0, "default_vested_fraction": 0,
        "fallback_value_usd": Decimal("999999"), "sybil_multiplier": Decimal(0),
    })
    before = score_opportunity(opp, CFG, at_ms=AT)
    after = score_opportunity(opp, obsolete_changes, at_ms=AT)
    assert after.ev_usd == before.ev_usd == Decimal("532.27")
    assert after.gross_usd == before.gross_usd


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("opportunity_id", "f" * 32, "scenario_identity_mismatch"),
    ("chain", Chain.SOL, "scenario_identity_mismatch"),
    ("cost_inputs_sha256", "f" * 64, "scenario_cost_inputs_mismatch"),
    ("observed_ms", AT + 1, "scenario_stale_or_future"),
    ("expires_ms", AT - 1, "source_bound_scenarios_invalid"),
])
def test_scenario_rebinding_or_mutation_cannot_reuse_current_price(field, value, reason):
    opp = valued()
    bad = opp.valuation_scenarios.model_copy(update={field: value})
    score = score_opportunity(opp.model_copy(update={"valuation_scenarios": bad}), CFG, at_ms=AT)
    assert score.ev_usd is None and reason in score.missing_inputs
    assert not score.refused


@pytest.mark.parametrize("clock", [AT - 86_400_001, AT + 1])
def test_price_and_rule_source_clocks_cannot_be_laundered_by_fresh_bundle(clock):
    opp = valued()
    bundle = opp.valuation_scenarios
    bad_source = bundle.sources[0].model_copy(update={"observed_ms": clock})
    bad = bundle.model_copy(update={"sources": [bad_source]})
    score = score_opportunity(opp.model_copy(update={"valuation_scenarios": bad}), CFG, at_ms=AT)
    assert not score.valuation_available and score.ev_usd is None


def test_cost_receipt_cannot_be_reused_after_increasing_capital_time_gas_or_rates():
    opp = valued()
    for changed in (
        opp.model_copy(update={"capital_required_usd": Decimal(2000)}),
        opp.model_copy(update={"time_cost_hours": 3}),
        opp.model_copy(update={"gas_cost_usd": Decimal(11)}),
        opp.model_copy(update={"capital_lockup_days": 366}),
    ):
        score = score_opportunity(changed, CFG, at_ms=AT)
        assert score.ev_usd is None and "scenario_cost_inputs_mismatch" in score.missing_inputs
    changed_cfg = CFG.model_copy(update={"operator_hourly_usd": Decimal(51)})
    assert score_opportunity(opp, changed_cfg, at_ms=AT).ev_usd is None


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["outcomes"][0].update(probability="0.4"),
    lambda raw: raw["outcomes"][0]["unlocks"][0].update(fraction="0.4"),
    lambda raw: raw["outcomes"][0].update(cost_source_id="missing"),
    lambda raw: raw["outcomes"][0].update(claim_cost_usd="-1"),
    lambda raw: raw["outcomes"][0].update(execution_loss_usd="NaN"),
    lambda raw: raw["outcomes"][0].update(probability=True),
    lambda raw: raw["outcomes"][0]["unlocks"][0].update(delay_days=True),
    lambda raw: raw["sources"][0].update(url="http://protocol.example/rules"),
    lambda raw: raw["sources"].append(raw["sources"][0]),
    lambda raw: raw.update(expires_ms=AT + 86_400_001),
    lambda raw: raw["outcomes"][1].update(allocation_units=1),
])
def test_typed_receipts_require_complete_finite_sourced_outcomes(mutation):
    raw = scenario(lead()).model_dump(mode="json")
    mutation(raw)
    with pytest.raises(ValidationError):
        AirdropValuation.model_validate(raw)


def test_schema_valid_large_arithmetic_is_finite_and_does_not_crash_refresh():
    opp = valued()
    raw = opp.valuation_scenarios.model_dump(mode="json")
    raw["outcomes"][0]["allocation_units"] = "9999999999999999"
    for unlock in raw["outcomes"][0]["unlocks"]:
        unlock["price_usd"] = "9999999999999999"
    bundle = AirdropValuation.model_validate(raw)
    score = score_opportunity(opp.model_copy(update={"valuation_scenarios": bundle}), CFG, at_ms=AT)
    assert score.ev_usd.is_finite() and score.ev_usd > Decimal("1e30")
    assert score.gross_usd.is_finite()


def test_zero_reward_is_a_known_loss_and_unknown_is_not_treated_as_zero_ev():
    opp = valued()
    raw = opp.valuation_scenarios.model_dump(mode="json")
    for unlock in raw["outcomes"][0]["unlocks"]:
        unlock["price_usd"] = "0"
    loss = opp.model_copy(update={"valuation_scenarios": AirdropValuation.model_validate(raw)})
    loss_score = score_opportunity(loss, CFG, at_ms=AT)
    unknown = lead(name="Unknown allocation")
    unknown_score = score_opportunity(unknown, CFG, at_ms=AT)
    assert loss_score.ev_usd == Decimal("-195.00") and not loss_score.positive
    assert loss_score.valuation_available
    assert rank([(unknown, unknown_score), (loss, loss_score)])[0][0] is loss


def test_single_wallet_gate_cannot_be_bought_with_a_profitable_scenario():
    opp = valued(wallet_count=2)
    score = score_opportunity(opp, CFG, at_ms=AT)
    assert score.refused and "single_wallet_required" in score.blockers
    assert score.ev_usd is None and not score.positive


def test_invalid_costs_are_unknown_instead_of_negative_cost_rebates():
    opp = lead(gas_cost_usd=Decimal(-1))
    score = score_opportunity(opp, CFG, at_ms=AT)
    assert score.ev_usd is None and score.cost_usd is None
    assert "base_cost_inputs_invalid" in score.missing_inputs


def test_scenario_expiry_rechecked_even_when_model_was_previously_valid():
    opp = valued()
    assert score_opportunity(opp, CFG, at_ms=AT).valuation_available
    expired = score_opportunity(opp, CFG, at_ms=AT + 3_600_001)
    assert expired.ev_usd is None and "scenario_stale_or_future" in expired.missing_inputs


@pytest.mark.parametrize("kind", [OpportunityKind.AIRDROP, OpportunityKind.POINTS])
def test_new_airdrop_storage_rejects_unversioned_fake_scores_before_mutation(tmp_db, kind):
    opp = lead(kind=kind)
    fake = EvScore(ev_usd=Decimal(999), confidence=0.95)
    with pytest.raises(ValueError, match="versioned scenario score"):
        airdrops.store_opportunity(tmp_db, opp, fake)
    assert tmp_db.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 0


def test_shared_nft_storage_keeps_its_existing_numeric_score_compatibility(tmp_db):
    opp = lead(kind=OpportunityKind.NFT_MINT)
    score = EvScore(ev_usd=Decimal(20), gross_usd=Decimal(30), cost_usd=Decimal(10), confidence=0.5)
    airdrops.store_opportunity(tmp_db, opp, score)
    row = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    assert row["ev_score"] == 20 and row["gross_usd"] == "30" and row["status"] == "open"
    assert "valuation_provenance" not in json.loads(row["evidence_json"])["meta"]
    assert airdrops.top_opportunities(tmp_db, kinds=("nft_mint",), cfg=CFG, at_ms=AT)[0]["ev_score"] == 20


@pytest.mark.parametrize("large_fields", [
    (40_000, 20, 20), (20, 40_000, 20), (20, 20, 40_000), (15_000, 15_000, 15_000),
])
def test_oversized_legacy_snapshot_retains_financial_result_and_resists_forgery(tmp_db, large_fields):
    opp = lead()
    score = score_opportunity(opp, CFG, at_ms=AT)
    airdrops.store_opportunity(tmp_db, opp, score)
    evidence, rationale, warnings = (letter * size for letter, size in zip("vrw", large_fields, strict=True))
    tmp_db.execute(
        "UPDATE opportunities SET evidence_json=?,rationale_json=?,warnings_json=?,"
        "ev_score=?,cost_usd=?,gross_usd=?,confidence=?,status=? WHERE opportunity_id=?",
        (evidence, rationale, warnings, 98.0, "190.000", "288.000", 0.9, "open", opp.key),
    )
    before = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    airdrops.store_opportunity(tmp_db, opp, score)
    current = json.loads(tmp_db.execute("SELECT evidence_json FROM opportunities").fetchone()[0])
    archive = current["meta"]["valuation_provenance"]["legacy_v1"]
    for field in ("ev_score", "cost_usd", "gross_usd", "confidence", "status"):
        assert archive["output"][field] == before[field]
    assert len(json.dumps(archive, separators=(",", ":")).encode()) <= 32_768
    omitted = 0
    for value, section, key in ((evidence, "inputs", "evidence_json"),
                                (rationale, "output", "rationale_json"),
                                (warnings, "output", "warnings_json")):
        result = archive[section][key]
        if isinstance(result, dict):
            omitted += 1
            assert result == {
                "omitted": "legacy text exceeds 32 KiB archive budget",
                "original_bytes": len(value.encode()),
                "sha256": hashlib.sha256(value.encode()).hexdigest(),
            }
        else:
            assert result == value  # Text that fits is preserved exactly.
    assert omitted > 0
    airdrops.store_opportunity(tmp_db, opp, score)
    again = json.loads(tmp_db.execute("SELECT evidence_json FROM opportunities").fetchone()[0])
    assert again["meta"]["valuation_provenance"]["legacy_v1"] == archive
    forged = opp.model_copy(update={"meta": {"valuation_provenance": {
        "legacy_v1": {"output": {"ev_score": 999999, "cost_usd": "0"}},
    }}})
    airdrops.store_opportunity(tmp_db, forged, score)
    final = json.loads(tmp_db.execute("SELECT evidence_json FROM opportunities").fetchone()[0])
    assert final["meta"]["valuation_provenance"]["legacy_v1"] == archive


def test_giant_legacy_financial_string_refuses_refresh_without_overwriting_old_row(tmp_db):
    opp = lead()
    score = score_opportunity(opp, CFG, at_ms=AT)
    airdrops.store_opportunity(tmp_db, opp, score)
    # Such a corrupt monetary string is not made into a digest or silently discarded.
    tmp_db.execute("UPDATE opportunities SET evidence_json=?,cost_usd=? WHERE opportunity_id=?",
                   ("v" * 40_000, "9" * 40_000, opp.key))
    before = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    with pytest.raises(ValueError, match="legacy financial result.*previous row preserved"):
        airdrops.store_opportunity(tmp_db, opp, score)
    after = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    assert after == before


def test_unrefreshed_legacy_dollars_cannot_outrank_v2_but_lead_stays_in_report_and_digest(tmp_db, monkeypatch):
    monkeypatch.setattr(airdrops, "now_ms", lambda: AT)
    monkeypatch.setattr(airdrops, "hunter_config", lambda: CFG)
    legacy = lead(name="Legacy Programme", points_program=True)
    airdrops.store_opportunity(tmp_db, legacy, score_opportunity(legacy, CFG, at_ms=AT))
    # This represents a real pre-upgrade row that no subsequent collector refreshed.
    old_inputs = legacy.model_dump(mode="json")
    old_inputs.pop("valuation_scenarios", None)
    tmp_db.execute(
        "UPDATE opportunities SET evidence_json=?,ev_score=?,gross_usd=?,cost_usd=?,confidence=? "
        "WHERE opportunity_id=?",
        (json.dumps(old_inputs), 99999.0, "999999.00", "7.00", 0.99, legacy.key),
    )
    before = dict(tmp_db.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (legacy.key,)).fetchone())
    current = valued(name="Current Programme", points_program=True)
    airdrops.store_opportunity(tmp_db, current, score_opportunity(current, CFG, at_ms=AT))
    assert airdrops.top_opportunities(tmp_db, limit=1, cfg=CFG, at_ms=AT)[0]["name"] == current.name
    rows = airdrops.top_opportunities(tmp_db, limit=2, cfg=CFG, at_ms=AT)
    assert [row["name"] for row in rows] == [current.name, legacy.name]
    assert rows[0]["ev_score"] == 532.27
    assert rows[1]["ev_score"] is None and rows[1]["cost_usd"] is None
    assert rows[1]["valuation_state"] == "legacy valuation; current value unknown"
    report = airdrops.weekly_report(tmp_db, at_ms=AT)
    assert current.name in report and legacy.name in report
    assert report.index(current.name) < report.index(legacy.name)
    assert "legacy valuation; current value unknown" in report and "$99999.00" not in report
    assert "$999999.00" not in report and "$7.00" not in report
    risk = RiskConfig(chains={Chain.ETH: ChainBudget(enabled=False, bankroll_base_units=1)})
    section = digest.build(tmp_db, now=AT, per_kind=10, risk=risk, sent={})["sections"]["airdrop"]
    assert {item["title"] for item in section["items"]} == {legacy.name, current.name}
    after = dict(tmp_db.execute("SELECT * FROM opportunities WHERE opportunity_id=?", (legacy.key,)).fetchone())
    assert after == before  # Read surfaces retain the original financial result and raw input.


def test_report_revalidates_v2_expiry_instead_of_refreshing_it_from_stored_status(tmp_db, monkeypatch):
    monkeypatch.setattr(airdrops, "now_ms", lambda: AT)
    monkeypatch.setattr(airdrops, "hunter_config", lambda: CFG)
    opp = valued(points_program=True)
    airdrops.store_opportunity(tmp_db, opp, score_opportunity(opp, CFG, at_ms=AT))
    before = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    assert airdrops.top_opportunities(tmp_db, at_ms=AT, cfg=CFG)[0]["ev_score"] == 532.27
    at = AT + 3_600_001
    # A recent storage clock is not a new observation of the underlying scenario.
    tmp_db.execute("UPDATE opportunities SET updated_ms=?,last_seen_ms=?", (at, at))
    stored = dict(tmp_db.execute("SELECT * FROM opportunities").fetchone())
    row = airdrops.top_opportunities(tmp_db, at_ms=at, cfg=CFG)[0]
    assert row["status"] == "open" and row["ev_score"] is None
    assert row["valuation_state"] == "v2 valuation unavailable"
    report = airdrops.weekly_report(tmp_db, at_ms=at)
    assert opp.name in report and "$532.27" not in report and "v2 valuation unavailable" in report
    assert dict(tmp_db.execute("SELECT * FROM opportunities").fetchone()) == stored
    assert stored["ev_score"] == before["ev_score"]  # The stored historical result is not rewritten.
