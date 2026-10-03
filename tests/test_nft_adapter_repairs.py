"""Offline NFT regressions; schema examples are captured, all economics are synthetic.

No network, production registry refresh, webhook registration, or capital action.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.hunters import nft
from kaiba.hunters.ev import HunterConfig

CFG = HunterConfig()
NOW = 1_790_000_000_000

# Exact public example in hunter-magiceden-schema-20260924.json (not a live lookup).
CAPTURED_ETH_ROW = {
    "name": "Hyperliquid Edition DEGN",
    "symbol": "hyperliquid_edition_degn",
    "chainId": "1",
    "chain": None,
    "contractAddress": "0x7a9f91dd6f3158eefd3cd0c2b93d1c018af52a6a",
    "launchDatetime": "2025-12-22T17:00:00.000Z",
    "price": 0.16,
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("NFT regression attempted a network/registration call")

    monkeypatch.setattr(nft, "fetch_json", forbidden)
    monkeypatch.setattr(nft, "register_helius_webhook", forbidden)
    monkeypatch.setattr(nft, "now_ms", lambda: NOW)


def clean_mint(**overrides):
    fields = dict(
        name="SYNTHETIC public mint",
        chain=Chain.SOL,
        candy_machine="SyntheticMachine",
        mint_price_native=Decimal("0.1"),
        mint_price_usd=Decimal("10"),
        recent_floor_usd=Decimal("1000"),
        guards_verified=True,
        public_phase=True,
        start_slot=100,
    )
    fields.update(overrides)
    return nft.MintEvidence(**fields)


def test_captured_chain_id_and_contract_survive_without_sol_currency_conversion(tmp_db):
    mint, = nft.magic_eden_launchpad(
        raw=[CAPTURED_ETH_ROW], sol_price_usd=Decimal("180"), conn=tmp_db
    )
    assert mint.chain is Chain.ETH
    assert mint.chain_hint == "1"
    assert mint.contract_address == CAPTURED_ETH_ROW["contractAddress"]
    assert mint.candy_machine is None, "a generic contract is not a verified Candy Machine"
    assert mint.mint_price_native == Decimal("0.16")
    assert mint.mint_price_usd is None
    assert mint.to_evidence().meta["contract_address"] == CAPTURED_ETH_ROW["contractAddress"]


@pytest.mark.parametrize(
    ("identity", "chain", "hint"),
    [
        ({"chainId": "solana"}, Chain.SOL, "solana"),
        ({"chainId": 8453}, Chain.BASE, "8453"),
        ({"chainId": "56"}, Chain.BSC, "56"),
        ({"chainId": "1329"}, None, "1329"),
        ({"chainId": "143"}, None, "143"),
        ({"chainId": "unrecognised"}, None, "unrecognised"),
        ({"chain": "solana"}, Chain.SOL, "solana"),
        ({"chain": "ethereum"}, Chain.ETH, "ethereum"),
        ({}, None, None),
    ],
)
def test_chain_identity_is_explicit_or_unknown(tmp_db, identity, chain, hint):
    row = {"name": "SYNTHETIC schema row", "price": 1, **identity}
    mint, = nft.magic_eden_launchpad(raw=[row], sol_price_usd=Decimal("180"), conn=tmp_db)
    assert mint.chain is chain
    assert mint.chain_hint == hint
    assert mint.mint_price_usd == (Decimal("180") if chain is Chain.SOL else None)


def test_direct_evidence_without_chain_does_not_assume_solana():
    assert nft.MintEvidence(name="SYNTHETIC ambiguous chain").chain is None


def test_launch_start_is_not_an_expiry_and_actual_end_survives_temp_registry(tmp_db):
    mint, = nft.magic_eden_launchpad(raw=[CAPTURED_ETH_ROW], conn=tmp_db)
    evidence = mint.to_evidence()
    assert evidence.deadline_ms is None, "launchDatetime is a start, not a deadline"
    assert evidence.meta["launch_ms"] == mint.launch_ms
    assert evidence.meta["end_ms"] is None
    assert nft.refresh(tmp_db, mints=[mint], cfg=CFG, force=True) == 1
    row = tmp_db.execute("SELECT * FROM opportunities").fetchone()
    assert row["deadline_ms"] is None

    bounded = clean_mint(launch_ms=NOW - 1000, end_ms=NOW + 1000)
    assert bounded.to_evidence().deadline_ms == NOW + 1000
    assert bounded.to_evidence().meta["launch_ms"] == NOW - 1000
    assert bounded.to_evidence().meta["end_ms"] == NOW + 1000


def test_passthrough_metadata_cannot_overwrite_verified_identity_or_lifecycle():
    mint = clean_mint(
        contract_address="SyntheticContract",
        launch_ms=NOW - 1000,
        end_ms=NOW + 1000,
        meta={"contract_address": "wrong", "launch_ms": 1, "end_ms": 2, "guards_verified": False},
    )
    meta = mint.to_evidence().meta
    assert meta["contract_address"] == "SyntheticContract"
    assert meta["launch_ms"] == NOW - 1000
    assert meta["end_ms"] == NOW + 1000
    assert meta["guards_verified"] is True


@pytest.mark.parametrize("chain", [Chain.ETH, Chain.BASE, Chain.BSC, None])
def test_non_solana_cannot_use_a_solana_guard_verdict_to_authorize_a_plan(chain):
    mint = clean_mint(chain=chain, chain_hint="unsupported-chain")
    score = nft.assess_mint(mint, CFG)
    plan = nft.build_mint_plan(mint, CFG)
    assert score.refused
    assert plan.refused and not plan.steps and not plan.executable_steps
    assert "unsupported" in plan.refusal_reason.lower()
    assert "candy guard" not in " ".join(score.blockers).lower()
    assert "bot tax" not in " ".join(score.blockers).lower()
    assert plan.total_capital_usd == 0


def test_evm_does_not_run_solana_specific_guard_checks():
    mint = clean_mint(
        chain=Chain.ETH,
        guards=[nft.MintGuard.THIRD_PARTY_SIGNER],
        guards_verified=False,
        start_slot=None,
    )
    score = nft.assess_mint(mint, CFG)
    assert score.refused
    assert "candy guard" not in " ".join(score.blockers).lower()
    assert "bot tax" not in " ".join(score.blockers).lower()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"public_phase": None}, "public phase"),
        ({"candy_machine": None}, "candy machine"),
        ({"start_slot": 0}, "start slot"),
        ({"start_slot": -1}, "start slot"),
        ({"end_ms": NOW - 1}, "expired"),
        ({"end_ms": NOW}, "expired"),
        ({"guards": [nft.MintGuard.END_DATE]}, "end time"),
        ({"launch_ms": NOW + 2000, "end_ms": NOW + 1000}, "before"),
    ],
)
def test_route_and_actual_lifecycle_fail_closed(overrides, reason):
    mint = clean_mint(**overrides)
    plan = nft.build_mint_plan(mint, CFG)
    assert plan.refused and not plan.steps and not plan.executable_steps
    assert reason in plan.refusal_reason.lower()
    assert plan.total_capital_usd == 0


def test_past_start_does_not_expire_a_verified_public_phase_or_enable_mint_execution():
    mint = clean_mint(launch_ms=NOW - 1000, end_ms=NOW + 1000)
    plan = nft.build_mint_plan(mint, CFG)
    assert not plan.refused
    mint_step, = [step for step in plan.steps if step.action.value == "mint"]
    assert mint_step.start_slot == mint.start_slot
    assert not mint_step.reversible
    assert not mint_step.executable
    assert mint_step not in plan.executable_steps
    assert not mint_step.requires_operator, "existing MEDIUM risk policy was not changed"


@pytest.mark.parametrize(
    ("mint_price", "floor", "threshold"),
    [("100", "1", "25"), ("10", "200", "25"), ("100", "1", "-1000")],
)
def test_negative_and_below_threshold_ev_cannot_produce_funded_plan(mint_price, floor, threshold):
    mint = clean_mint(mint_price_usd=Decimal(mint_price), recent_floor_usd=Decimal(floor))
    cfg = HunterConfig(ev_threshold_usd=Decimal(threshold))
    score = nft.assess_mint(mint, cfg)
    assert score.ev_usd <= 0 or score.ev_usd < cfg.ev_threshold_usd
    plan = nft.build_mint_plan(mint, cfg)
    assert plan.refused and not plan.steps and not plan.executable_steps
    assert plan.total_capital_usd == 0
    assert "expected value" in plan.refusal_reason.lower()


def test_threshold_is_inclusive_but_zero_ev_is_always_refused():
    mint = clean_mint()
    score = nft.assess_mint(mint, CFG)
    assert not nft.build_mint_plan(mint, HunterConfig(ev_threshold_usd=score.ev_usd)).refused
    assert nft.build_mint_plan(mint, HunterConfig(ev_threshold_usd=score.ev_usd + Decimal("0.01"))).refused
    zero = clean_mint(mint_price_usd=Decimal("0"), recent_floor_usd=Decimal("0"),
                      gas_cost_usd=Decimal("0"), time_cost_hours=0)
    plan = nft.build_mint_plan(zero, HunterConfig(ev_threshold_usd=Decimal("0")))
    assert plan.refused and not plan.steps


@pytest.mark.parametrize("missing", ["mint_price_usd", "recent_floor_usd", "mint_price_native"])
def test_missing_value_is_unknown_not_a_zero_price_allocation(tmp_db, missing):
    mint = clean_mint(**{missing: None})
    meta = mint.to_evidence().meta
    assert meta[missing] is None
    assert meta["valuation_complete"] is False
    plan = nft.build_mint_plan(mint, CFG)
    assert plan.refused and not plan.steps and plan.total_capital_usd == 0
    assert any("diagnostic" in warning.lower() for warning in nft.assess_mint(mint, CFG).warnings)
    nft.refresh(tmp_db, mints=[mint], cfg=CFG, force=True)
    row = tmp_db.execute("SELECT * FROM opportunities").fetchone()
    assert row["status"] == "refused"
    assert row["ev_score"] is None, "an incomplete valuation must not rank as a priced opportunity"
    if missing == "mint_price_usd":
        assert row["cost_usd"] is None
    if missing == "recent_floor_usd":
        assert row["gross_usd"] is None


def test_zero_recent_floor_is_not_replaced_with_a_positive_creator_floor():
    mint = clean_mint(
        recent_floor_usd=Decimal("0"),
        creator=nft.CreatorHistory(prior_collections=2, prior_floor_usd=Decimal("1000")),
    )
    score = nft.assess_mint(mint, CFG)
    assert score.gross_usd == 0
    assert score.ev_usd < 0
    assert nft.build_mint_plan(mint, CFG).refused


def test_explicit_zero_native_price_survives_serialization_and_plan_detail(tmp_db):
    mint, = nft.magic_eden_launchpad(
        raw=[{"name": "SYNTHETIC free mint", "chainId": "solana", "price": 0}],
        sol_price_usd=Decimal("180"), conn=tmp_db,
    )
    assert mint.mint_price_native == 0
    assert mint.mint_price_usd == 0
    assert mint.to_evidence().meta["mint_price_native"] == "0"
    plan = nft.build_mint_plan(clean_mint(mint_price_native=Decimal("0"), mint_price_usd=Decimal("0")), CFG)
    assert not plan.refused
    assert "0 SOL" in plan.steps[0].detail
    assert "? SOL" not in plan.steps[0].detail


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "-1", True, "broken", None])
def test_bad_source_price_does_not_discard_other_rows_or_become_a_free_mint(tmp_db, price):
    rows = [
        {"name": "SYNTHETIC bad price", "chainId": "solana", "price": price},
        {"name": "SYNTHETIC good price", "chainId": "solana", "price": 1},
    ]
    mints = nft.magic_eden_launchpad(raw=rows, sol_price_usd=Decimal("180"), conn=tmp_db)
    assert len(mints) == 2
    assert mints[0].mint_price_native is None
    assert mints[0].mint_price_usd is None
    assert mints[1].mint_price_usd == Decimal("180")


@pytest.mark.parametrize("fx", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_sol_fx_cannot_price_a_mint(tmp_db, fx):
    mint, = nft.magic_eden_launchpad(
        raw=[{"name": "SYNTHETIC invalid FX", "chainId": "solana", "price": 1}],
        sol_price_usd=fx, conn=tmp_db,
    )
    assert mint.mint_price_native == 1
    assert mint.mint_price_usd is None


@pytest.mark.parametrize("field", ["mint_price_usd", "mint_price_native", "recent_floor_usd",
                                   "gas_cost_usd", "time_cost_hours"])
def test_negative_costs_or_values_cannot_authorize_funding(field):
    mint = clean_mint(**{field: -1})
    plan = nft.build_mint_plan(mint, CFG)
    assert plan.refused and not plan.steps and not plan.executable_steps
    assert plan.total_capital_usd == 0


def test_conflicting_source_chain_identity_stays_ambiguous(tmp_db):
    row = {**CAPTURED_ETH_ROW, "chainId": "solana", "chain": "ethereum"}
    mint, = nft.magic_eden_launchpad(raw=[row], sol_price_usd=Decimal("180"), conn=tmp_db)
    assert mint.chain is None
    assert mint.mint_price_usd is None
    assert mint.chain_hint == "solana"
    assert mint.meta["source_chain"] == "ethereum"
    assert mint.meta["source_chain_id"] == "solana"


def test_helius_no_deliveries_is_not_a_provider_failure_or_verified_coverage(tmp_db, caplog):
    with caplog.at_level("INFO"):
        assert nft.helius_mint_watch(tmp_db) == []
    row = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='helius_mint_watch'").fetchone()
    assert row["last_count"] == 0
    assert row["fail_streak"] == 0
    assert row["last_ok_ms"] is None, "no deliveries does not prove webhook coverage"
    assert "no_deliveries" in caplog.text and "unverified" in caplog.text
    assert tmp_db.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0


def test_magic_eden_health_distinguishes_empty_parse_from_schema_failure(tmp_db):
    assert nft.magic_eden_launchpad(raw=[], conn=tmp_db) == []
    empty = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='magiceden'").fetchone()
    assert empty is not None
    assert empty["last_count"] == 0 and empty["fail_streak"] == 0
    assert empty["last_ok_ms"] is not None and empty["last_error"] is None
    assert nft.magic_eden_launchpad(raw={"unexpected": []}, conn=tmp_db) == []
    failed = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='magiceden'").fetchone()
    assert failed["fail_streak"] == 1
    assert "expected list" in failed["last_error"]
    assert failed["last_ok_ms"] == empty["last_ok_ms"]


def test_disabled_refresh_keeps_integer_api_without_db_or_provider_calls(monkeypatch, caplog):
    monkeypatch.delenv("KAIBA_ENABLE_NFT_HUNTER", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled refresh must not call a source or open a database")

    monkeypatch.setattr(nft, "get_conn", forbidden)
    monkeypatch.setattr(nft, "helius_mint_watch", forbidden)
    monkeypatch.setattr(nft, "magic_eden_launchpad", forbidden)
    with caplog.at_level("INFO"):
        result = nft.refresh()
    assert result == 0 and isinstance(result, int)
    assert "disabled" in caplog.text


def test_unrecognized_nonempty_source_is_not_healthy_empty(tmp_db):
    assert nft.magic_eden_launchpad(raw=[{"newProviderShape": "not a mint"}], conn=tmp_db) == []
    row = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='magiceden'").fetchone()
    assert row["fail_streak"] == 1
    assert "no identifiable" in row["last_error"]
    assert row["last_ok_ms"] is None


def test_helius_parse_failure_does_not_masquerade_as_an_empty_success(tmp_db):
    payload = {"type": "NFT_MINT", "signature": "synthetic-bad-payload", "events": {"nft": "bad"}}
    assert nft.helius_mint_watch(tmp_db, payloads=[payload]) == []
    row = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='helius_mint_watch'").fetchone()
    assert row["fail_streak"] == 1
    assert "parse" in row["last_error"]
    assert row["last_ok_ms"] is None


def test_unknown_mint_total_cost_is_not_reported_as_zero(tmp_db):
    mint = clean_mint(mint_price_usd=None)
    nft.refresh(tmp_db, mints=[mint], cfg=CFG, force=True)
    report = nft.weekly_report(tmp_db)
    assert "unknown" in report
    row = next(line for line in report.splitlines() if mint.name in line)
    assert "| unknown |" in row


def test_observed_chain_distribution_and_contracts_survive_without_qualifying_leads(tmp_db):
    """Synthetic rows use the captured count distribution, NOT the full live payload."""
    from kaiba.hunters.qualification import opportunity_report

    counts = {"solana": 82, "1": 8, "1329": 11, "143": 21, "4326": 5,
              "43114": 10, "137": 2, "2741": 26, "6342": 8, "8453": 4,
              "80094": 13, "33139": 9, "56": 1}
    rows = [
        {"name": f"SYNTHETIC {chain}-{index}", "symbol": f"synthetic_{chain}_{index}",
         "chainId": chain, "contractAddress": f"SyntheticContract-{chain}-{index}",
         "launchDatetime": "2026-02-20T09:30:00.000Z", "price": 1}
        for chain, count in counts.items() for index in range(count)
    ]
    mints = nft.magic_eden_launchpad(raw=rows, sol_price_usd=Decimal("180"), conn=tmp_db)
    assert len(mints) == 200
    assert Counter(m.chain_hint for m in mints) == counts
    assert sum(m.chain is Chain.SOL for m in mints) == 82
    assert sum(m.chain is not Chain.SOL for m in mints) == 118
    assert [m.contract_address for m in mints] == [row["contractAddress"] for row in rows]
    assert all(m.to_evidence().deadline_ms is None for m in mints)
    assert all("qualification" not in m.to_evidence().meta for m in mints)
    assert nft.refresh(tmp_db, mints=mints, cfg=CFG, force=True) == 200
    report = opportunity_report(tmp_db, kind="nft", at_ms=NOW)
    assert report["qualified_count"] == 0 and report["opportunities"] == []
    assert report["registry_count"] == 200
    assert tmp_db.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0] == 0
