"""Hunters: EV arithmetic, defensive collectors, plan safety, listing latency.

Everything here is offline. Fixtures under ``tests/fixtures/hunters/`` are hand-built to
the documented shape of each source (they are not live captures — each file says so in its
``note``), and every collector is additionally run against a mangled body to prove that a
layout change degrades to ``[]`` plus a ``PROVIDER_ERROR`` rather than an exception.

The EV cases are hand-computed in the docstrings so that a change to a base rate breaks a
test with an explanation attached, instead of silently re-pricing the agent's decisions.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from kaiba.core import events
from kaiba.core.schemas import Chain, EventKind, now_ms
from kaiba.hunters import airdrops, listings, nft
from kaiba.hunters.airdrops import PlanAction, RiskLevel
from kaiba.hunters.ev import (
    EvScore,
    HunterConfig,
    OpportunityEvidence,
    OpportunityKind,
    SybilRisk,
    dedupe,
    haircut_for,
    hunter_config,
    normalise_name,
    score_opportunity,
)

FIXTURES = Path(__file__).parent / "fixtures" / "hunters"

#: $50/h keeps the hand arithmetic readable; the shipped default is $60.
CFG = HunterConfig(operator_hourly_usd=Decimal("50"))


def fixture(name: str):
    """Fixtures wrap their body so the file can record where the shape came from."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["body"]


def mangled(key: str):
    return json.loads((FIXTURES / "mangled.json").read_text(encoding="utf-8"))[key]


def provider_errors(conn) -> list:
    return events.recent(limit=50, kinds=[EventKind.PROVIDER_ERROR.value], conn=conn)


def base_opportunity(**kw) -> OpportunityEvidence:
    """Confirmed token, $500 median comparable, unvested, $1000 locked for a year."""
    defaults = dict(
        kind=OpportunityKind.AIRDROP,
        name="Confirmed Co",
        confirmed_token=True,
        prior_airdrop_history=[Decimal("400"), Decimal("600")],
        vested_fraction=0.0,
        capital_required_usd=Decimal("1000"),
        capital_lockup_days=365,
        time_cost_hours=2.0,
        gas_cost_usd=Decimal("10"),
        sybil_risk=SybilRisk.LOW,
        official_url="https://confirmed.example",
    )
    defaults.update(kw)
    return OpportunityEvidence(**defaults)


# ============================================================== 1. EV arithmetic


def test_haircut_encodes_the_published_base_rates():
    """Unvested keeps the 64% TGE-sell rate; fully vested keeps the 12% survival rate."""
    assert haircut_for(0.0, CFG) == pytest.approx(0.64)
    assert haircut_for(1.0, CFG) == pytest.approx(0.12)
    assert haircut_for(0.7, CFG) == pytest.approx(0.276)


def test_ev_is_hand_computable():
    """0.9 x $500 x 0.64 x 1.0 = $288 gross; $10 gas + $80 carry + $100 time = $190.

    $1000 locked for 365 days at 8% is $80. Two operator hours at $50 is $100.
    """
    score = score_opportunity(base_opportunity(), CFG)
    assert score.gross_usd == Decimal("288.00")
    assert score.cost_usd == Decimal("190.00")
    assert score.ev_usd == Decimal("98.00")


def test_p_token_tiers_are_the_specified_ones():
    common = dict(
        name="X",
        prior_airdrop_history=[Decimal("100")],
        vested_fraction=0.0,
        sybil_risk=SybilRisk.LOW,
    )
    confirmed = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.AIRDROP, confirmed_token=True, **common), CFG
    )
    points = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.POINTS, points_program=True, **common), CFG
    )
    speculative = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.AIRDROP, **common), CFG
    )
    assert (confirmed.p_token, points.p_token, speculative.p_token) == (0.9, 0.5, 0.15)


def test_vesting_discounts_further():
    unvested = score_opportunity(base_opportunity(), CFG)
    vested = score_opportunity(base_opportunity(vested_fraction=0.7), CFG)
    assert vested.gross_usd == Decimal("124.20")  # 0.9 x 500 x 0.276
    assert vested.ev_usd == Decimal("-65.80")
    assert vested.ev_usd < unvested.ev_usd


def test_unstated_vesting_assumes_the_70_percent_base_rate():
    score = score_opportunity(base_opportunity(vested_fraction=None), CFG)
    assert score.ev_usd == score_opportunity(base_opportunity(vested_fraction=0.7), CFG).ev_usd
    assert any("70% vested base rate" in r for r in score.rationale)


def test_capital_carry_and_time_are_both_charged():
    free = score_opportunity(
        base_opportunity(capital_required_usd=Decimal("0"), capital_lockup_days=0,
                         time_cost_hours=0.0, gas_cost_usd=Decimal("0")),
        CFG,
    )
    assert free.cost_usd == Decimal("0.00")
    assert free.ev_usd == Decimal("288.00")
    half_year = score_opportunity(base_opportunity(capital_lockup_days=182), CFG)
    # 1000 * 182/365 * 0.08 = 39.89
    assert half_year.cost_usd == Decimal("149.89")


def test_operator_hourly_rate_comes_from_config(tmp_path, monkeypatch):
    risk = tmp_path / "risk.yaml"
    risk.write_text("version: v1\nhunters:\n  operator_hourly_usd: 120\n  ev_threshold_usd: 5\n",
                    encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk))
    cfg = hunter_config()
    assert cfg.operator_hourly_usd == Decimal("120")
    assert cfg.ev_threshold_usd == Decimal("5")
    # two hours now costs $240 instead of $100, so EV drops by exactly $140
    assert score_opportunity(base_opportunity(), cfg).ev_usd == Decimal("-42.00")


def test_env_var_overrides_the_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("KAIBA_OPERATOR_HOURLY_USD", "10")
    assert hunter_config().operator_hourly_usd == Decimal("10")


def test_broken_config_falls_back_to_defaults(tmp_path, monkeypatch):
    bad = tmp_path / "risk.yaml"
    bad.write_text("hunters:\n  operator_hourly_usd: not-a-number\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(bad))
    monkeypatch.delenv("KAIBA_OPERATOR_HOURLY_USD", raising=False)
    assert hunter_config().operator_hourly_usd == Decimal("60")


# ============================================================== 2. the Sybil penalty


def test_multi_wallet_plan_is_penalised_and_warned():
    """Three wallets: gross 288 x 0.2 = 57.60, costs unchanged at 190."""
    score = score_opportunity(base_opportunity(wallet_count=3), CFG)
    assert score.gross_usd == Decimal("57.60")
    assert score.cost_usd == Decimal("190.00")
    assert score.ev_usd == Decimal("-132.40")
    assert any("does not Sybil" in w for w in score.warnings)
    assert any("sybil penalty" in r.lower() for r in score.rationale)


def test_sybil_penalty_hits_gross_so_it_is_harsher_than_scaling_net():
    single = score_opportunity(base_opportunity(), CFG)
    multi = score_opportunity(base_opportunity(wallet_count=2), CFG)
    assert multi.ev_usd < single.ev_usd * Decimal("0.2")


def test_single_wallet_organic_is_not_penalised():
    score = score_opportunity(base_opportunity(wallet_count=1), CFG)
    assert score.gross_usd == Decimal("288.00")
    assert not any("Sybil" in w for w in score.warnings)


def test_hard_filtering_programmes_lose_eligibility():
    low = score_opportunity(base_opportunity(sybil_risk=SybilRisk.LOW), CFG)
    high = score_opportunity(base_opportunity(sybil_risk=SybilRisk.HIGH), CFG)
    assert high.gross_usd == (low.gross_usd * Decimal("0.6")).quantize(Decimal("0.01"))
    assert any("filters hard" in w for w in high.warnings)


# ============================================================== 3. ranking and honesty


def test_confirmed_token_outranks_speculative_with_identical_costs():
    common = dict(
        prior_airdrop_history=[Decimal("300")],
        vested_fraction=0.0,
        capital_required_usd=Decimal("500"),
        capital_lockup_days=30,
        time_cost_hours=1.0,
        gas_cost_usd=Decimal("5"),
        sybil_risk=SybilRisk.LOW,
    )
    confirmed = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Sure Thing",
                            confirmed_token=True, **common), CFG
    )
    speculative = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Maybe", **common), CFG
    )
    assert confirmed.cost_usd == speculative.cost_usd
    assert confirmed.ev_usd > speculative.ev_usd
    assert confirmed.gross_usd == (speculative.gross_usd * 6).quantize(Decimal("0.01"))
    assert confirmed.confidence > speculative.confidence


def test_no_comparable_uses_the_published_floor_and_says_so():
    score = score_opportunity(
        OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Unknown Thing"), CFG
    )
    assert any("$20 low end" in w for w in score.warnings)
    assert any("no comparable" in r for r in score.rationale)


def test_negative_ev_is_warned_not_hidden():
    score = score_opportunity(base_opportunity(time_cost_hours=40.0), CFG)
    assert score.ev_usd < 0
    assert any("negative expected value" in w for w in score.warnings)


def test_rationale_cites_every_number_used():
    score = score_opportunity(base_opportunity(), CFG)
    blob = " ".join(score.rationale)
    for needle in ("0.9", "500", "0.64", "$10", "$80", "$100", "$288.00", "$190.00", "$98.00"):
        assert needle in blob, f"{needle} missing from the audit trail"


def test_deadline_warnings():
    from kaiba.core.schemas import now_ms

    soon = score_opportunity(base_opportunity(deadline_ms=now_ms() + 3_600_000), CFG)
    assert any("not enough time" in w for w in soon.warnings)
    passed = score_opportunity(base_opportunity(deadline_ms=now_ms() - 1000), CFG)
    assert any("deadline has passed" in w for w in passed.warnings)


def test_long_lockups_are_called_a_bet():
    score = score_opportunity(base_opportunity(capital_lockup_days=180), CFG)
    assert any("locked 180 days" in w for w in score.warnings)


def test_confidence_rises_with_evidence():
    thin = score_opportunity(OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Thin"), CFG)
    thick = score_opportunity(base_opportunity(source_count=4), CFG)
    assert thick.confidence > thin.confidence
    assert 0.05 <= thin.confidence <= 0.95


# ============================================================== 4. dedupe / merge


def test_dedupe_merges_by_normalised_name_and_url():
    a = OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="LayerZero (Season 2)",
                            url="https://LayerZero.network/", sources=["airdrops_io"])
    b = OpportunityEvidence(kind=OpportunityKind.POINTS, name="layerzero season 2",
                            url="http://www.layerzero.network?ref=x", sources=["defillama"],
                            confirmed_token=True)
    merged = dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].confirmed_token is True
    assert merged[0].sources == ["airdrops_io", "defillama"]
    assert merged[0].source_count == 2


def test_dedupe_keeps_genuinely_different_programmes():
    a = OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Aster", url="https://asterdex.com")
    b = OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Meteora", url="https://meteora.ag")
    assert len(dedupe([a, b])) == 2
    assert normalise_name("Meteora S2!") == "meteoras2"


def test_merge_takes_the_worse_cost_and_the_more_definite_claim():
    a = OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="Dup", time_cost_hours=1.0,
                            gas_cost_usd=Decimal("1"), sybil_risk=SybilRisk.LOW)
    b = OpportunityEvidence(kind=OpportunityKind.AIRDROP, name="dup", time_cost_hours=4.0,
                            gas_cost_usd=Decimal("9"), sybil_risk=SybilRisk.HIGH,
                            confirmed_token=True)
    merged = dedupe([a, b])[0]
    assert merged.time_cost_hours == 4.0
    assert merged.gas_cost_usd == Decimal("9")
    assert merged.sybil_risk is SybilRisk.HIGH
    assert merged.confirmed_token is True


# ============================================================== 5. airdrop collectors


def test_airdrops_io_telegram_parses_its_fixture(tmp_db):
    items = airdrops.from_airdrops_io_telegram(fixture("airdrops_io_telegram.json"), conn=tmp_db)
    names = [i.name for i in items]
    assert names == ["Hyperliquid Season 3", "Polymarket", "Backpack S4"]  # "gm" is not a programme
    by_name = {i.name: i for i in items}
    assert by_name["Polymarket"].confirmed_token is True
    assert by_name["Hyperliquid Season 3"].points_program is True
    assert by_name["Hyperliquid Season 3"].chain_hint == "hyperliquid"
    assert by_name["Backpack S4"].chain is Chain.SOL
    assert by_name["Backpack S4"].sybil_risk is SybilRisk.HIGH  # the post mentions KYC and sybil
    assert all(i.gas_cost_usd > 0 for i in items), "a free opportunity is a lie"


def test_airdrops_io_tolerates_a_layout_change(tmp_db):
    assert airdrops.from_airdrops_io_telegram(mangled("html"), conn=tmp_db) == []


def test_airdropalert_rss_parses_its_fixture(tmp_db):
    items = airdrops.from_airdropalert_rss(fixture("airdropalert_rss.json"), conn=tmp_db)
    assert [i.name for i in items] == ["Variational Airdrop", "MetaMask Rewards", "Monad Testnet Quests"]
    assert items[1].confirmed_token is True
    assert items[0].chain_hint == "arbitrum"
    assert items[2].sybil_risk is SybilRisk.HIGH
    assert items[0].first_seen_ms is not None


def test_airdropalert_rss_reports_broken_xml(tmp_db):
    assert airdrops.from_airdropalert_rss(mangled("xml"), conn=tmp_db) == []
    assert any("invalid rss" in e.payload["detail"] for e in provider_errors(tmp_db))


def test_defillama_keeps_only_tokenless_protocols_with_tvl(tmp_db):
    items = airdrops.from_defillama(fixture("defillama_protocols.json"), conn=tmp_db)
    names = {i.name for i in items}
    assert names == {"Variational", "Polymarket", "Backpack Exchange"}
    assert "Hyperliquid" not in names  # already has a token
    assert "Dust Protocol" not in names  # below the TVL floor
    assert all(not i.confirmed_token for i in items)  # tokenless is speculative, p=0.15


def test_defillama_rejects_the_wrong_shape(tmp_db):
    assert airdrops.from_defillama(mangled("json_object"), conn=tmp_db) == []
    assert any("expected a list" in e.payload["detail"] for e in provider_errors(tmp_db))


def test_alphadrops_reads_the_next_data_blob(tmp_db):
    items = airdrops.from_alphadrops(fixture("alphadrops_points.json"), conn=tmp_db)
    assert [i.name for i in items] == ["Aster", "Meteora S2", "Variational"]
    assert all(i.points_program for i in items)
    assert items[0].confirmed_token is True and items[0].chain is Chain.BSC
    assert items[1].chain is Chain.SOL
    assert items[2].capital_required_usd == Decimal("500")
    assert items[2].deadline_ms is not None


def test_alphadrops_falls_back_to_the_html_table(tmp_db):
    items = airdrops.from_alphadrops(fixture("alphadrops_table.json"), conn=tmp_db)
    assert [i.name for i in items] == ["Backpack", "Kraken Ink"]
    assert items[0].url == "https://backpack.exchange"


def test_alphadrops_tolerates_a_layout_change(tmp_db):
    assert airdrops.from_alphadrops(mangled("html"), conn=tmp_db) == []


def test_a_dead_provider_returns_empty_and_emits_provider_error(tmp_db, monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(airdrops.httpx, "stream", boom)
    assert airdrops.from_airdrops_io_telegram(conn=tmp_db) == []
    errs = provider_errors(tmp_db)
    assert errs and errs[0].payload["provider"] == "airdrops_io"


def test_collect_dedupes_across_collectors_and_records_source_health(tmp_db):
    collectors = {
        "airdrops_io": lambda conn=None: airdrops.from_airdrops_io_telegram(
            fixture("airdrops_io_telegram.json"), conn=conn
        ),
        "defillama": lambda conn=None: airdrops.from_defillama(
            fixture("defillama_protocols.json"), conn=conn
        ),
    }
    merged = airdrops.collect(tmp_db, collectors)
    polymarket = [m for m in merged if m.name == "Polymarket"]
    assert len(polymarket) == 1, "the same programme from two sources is one opportunity"
    assert polymarket[0].source_count == 2
    assert sorted(polymarket[0].sources) == ["airdrops_io", "defillama"]
    health = {r["name"]: r for r in tmp_db.execute("SELECT * FROM hunter_sources").fetchall()}
    assert health["airdrops_io"]["last_count"] == 3
    assert health["defillama"]["fail_streak"] == 0


def test_a_broken_collector_does_not_stop_the_others(tmp_db):
    def broken(conn=None):
        raise RuntimeError("scraper exploded")

    merged = airdrops.collect(
        tmp_db,
        {
            "broken": broken,
            "defillama": lambda conn=None: airdrops.from_defillama(
                fixture("defillama_protocols.json"), conn=conn
            ),
        },
    )
    assert len(merged) == 3
    row = tmp_db.execute("SELECT * FROM hunter_sources WHERE name='broken'").fetchone()
    assert row["fail_streak"] == 1 and "scraper exploded" in row["last_error"]


# ============================================================== 6. refresh + storage


def _verified_terms():
    """Synthetic, current research receipt for positive planner/alert controls only."""
    return {"observed_ms": now_ms(), "official_source_verified": True,
            "eligibility_verified": True, "value_cost_verified": True,
            "execution_path_verified": True, "participation_open": True}


def _rich_collector(ev_usd: str = "5000"):
    def collector(conn=None):
        return [
            OpportunityEvidence(
                kind=OpportunityKind.AIRDROP,
                name="Rich Programme",
                confirmed_token=True,
                expected_value_usd=Decimal(ev_usd),
                vested_fraction=0.0,
                sybil_risk=SybilRisk.LOW,
                url="https://rich.example",
                official_url="https://rich.example",
                chain=Chain.SOL,
                meta={"qualification": _verified_terms()},
                sources=["test"],
            )
        ]

    return collector


def test_refresh_stores_scores_and_emits_hunter_found_once(tmp_db):
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("25"))
    assert airdrops.refresh(tmp_db, {"test": _rich_collector()}, cfg) == 1
    row = tmp_db.execute("SELECT * FROM opportunities").fetchone()
    assert row["kind"] == "airdrop" and row["status"] == "open"
    assert row["ev_score"] == pytest.approx(2880.0)  # 0.9 x 5000 x 0.64
    # Positive fixture now supplies official_url; the existing scorer adds 0.10.
    assert row["source"] == "test" and row["confidence"] == pytest.approx(0.55)
    assert json.loads(row["rationale_json"])
    found = events.recent(kinds=[EventKind.HUNTER_FOUND.value], conn=tmp_db)
    assert len(found) == 1 and found[0].payload["name"] == "Rich Programme"

    airdrops.refresh(tmp_db, {"test": _rich_collector()}, cfg)
    assert len(events.recent(kinds=[EventKind.HUNTER_FOUND.value], conn=tmp_db)) == 1
    assert tmp_db.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"] == 1


def test_refresh_stays_quiet_below_the_threshold(tmp_db):
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("10000"))
    airdrops.refresh(tmp_db, {"test": _rich_collector()}, cfg)
    assert events.recent(kinds=[EventKind.HUNTER_FOUND.value], conn=tmp_db) == []


def test_weekly_report_is_markdown_and_shows_warnings(tmp_db):
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"))
    airdrops.refresh(
        tmp_db,
        {
            "test": _rich_collector(),
            "llama": lambda conn=None: airdrops.from_defillama(
                fixture("defillama_protocols.json"), conn=conn
            ),
        },
        cfg,
    )
    report = airdrops.weekly_report(tmp_db)
    assert report.startswith("# Airdrop hunter")
    assert "| # | Opportunity |" in report
    assert "Rich Programme" in report
    assert "64% of recipients sell at TGE" in report
    assert "non-positive EV" in report  # the tokenless protocols do not clear their costs


# ============================================================== 7. participation plans


def _programme(**kw) -> OpportunityEvidence:
    meta = {"requires_bridge": True, "kyc_required": True, "governance": True,
            "qualification": _verified_terms()}
    meta.update(kw.pop("meta", {}))
    return OpportunityEvidence(
        kind=OpportunityKind.POINTS,
        name="Points Co",
        points_program=True,
        confirmed_token=True,
        capital_required_usd=Decimal("250"),
        capital_lockup_days=30,
        chain=Chain.BASE,
        chain_hint="base",
        expected_value_usd=Decimal("5000"),
        official_url="https://points.example",
        url="https://points.example",
        meta=meta,
        **kw,
    )


def test_plan_steps_use_the_fixed_vocabulary_and_are_ordered(tmp_db):
    plan = airdrops.build_plan(_programme(), tmp_db)
    assert [s.index for s in plan.steps] == list(range(1, len(plan.steps) + 1))
    assert all(isinstance(s.action, PlanAction) for s in plan.steps)
    actions = [s.action for s in plan.steps]
    assert actions[0] is PlanAction.WALLET_CONNECT
    assert PlanAction.SIGN_MESSAGE in actions
    assert PlanAction.BRIDGE in actions
    assert PlanAction.CLAIM in actions
    assert PlanAction.KYC in actions


def test_risk_classification_of_the_dangerous_actions():
    assert airdrops.ACTION_RISK[PlanAction.SIGN_MESSAGE] is RiskLevel.CRITICAL
    assert airdrops.ACTION_RISK[PlanAction.CLAIM] is RiskLevel.CRITICAL
    assert airdrops.ACTION_RISK[PlanAction.KYC] is RiskLevel.CRITICAL
    assert airdrops.ACTION_RISK[PlanAction.BRIDGE] is RiskLevel.HIGH
    assert airdrops.ACTION_RISK[PlanAction.WALLET_CONNECT] is RiskLevel.LOW


def test_every_critical_step_requires_the_operator(tmp_db):
    plan = airdrops.build_plan(_programme(), tmp_db)
    criticals = [s for s in plan.steps if s.risk is RiskLevel.CRITICAL]
    assert criticals, "this fixture is supposed to contain critical steps"
    assert all(s.requires_operator for s in criticals)
    assert all(not s.reversible for s in criticals)
    assert plan.requires_operator


def test_executor_only_ever_sees_safe_steps(tmp_db):
    plan = airdrops.build_plan(_programme(), tmp_db)
    for step in plan.executable_steps:
        assert step.risk in {RiskLevel.LOW, RiskLevel.MEDIUM}
        assert step.reversible and not step.requires_operator
    assert PlanAction.CLAIM not in {s.action for s in plan.executable_steps}


def test_capital_is_recorded_per_step_but_not_double_counted(tmp_db):
    plan = airdrops.build_plan(_programme(), tmp_db)
    assert plan.total_capital_usd == Decimal("250")
    funded = [s for s in plan.steps if s.capital_usd > 0]
    assert funded and all(s.capital_usd == Decimal("250") for s in funded)
    assert sum(s.capital_usd for s in funded) > plan.total_capital_usd  # same dollars, several steps


def test_multi_wallet_plans_are_refused_outright(tmp_db):
    plan = airdrops.build_plan(_programme(wallet_count=5), tmp_db)
    assert plan.refused and plan.steps == []
    assert plan.executable_steps == []
    assert plan.requires_operator
    assert "does not Sybil" in (plan.refusal_reason or "")


def test_plan_can_be_built_from_a_stored_row(tmp_db):
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"))
    airdrops.refresh(tmp_db, {"test": _rich_collector()}, cfg)
    oid = tmp_db.execute("SELECT opportunity_id FROM opportunities").fetchone()["opportunity_id"]
    plan = airdrops.build_plan(oid, tmp_db)
    assert plan.name == "Rich Programme"
    assert plan.steps


# ============================================================== 8. NFT mints


def _clean_mint(**kw) -> nft.MintEvidence:
    defaults = dict(
        name="Sol Cats",
        symbol="SOLCAT",
        chain=Chain.SOL,
        candy_machine="CandyMach1ne111111111111111111111111111111",
        mint_price_native=Decimal("0.5"),
        mint_price_usd=Decimal("90"),
        supply=3333,
        start_slot=298_000_000,
        guards=[nft.MintGuard.SOL_PAYMENT, nft.MintGuard.START_DATE, nft.MintGuard.BOT_TAX],
        guards_verified=True,
        public_phase=True,
        recent_floor_usd=Decimal("400"),
        creator=nft.CreatorHistory(address="Creator1", prior_collections=2),
    )
    defaults.update(kw)
    return nft.MintEvidence(**defaults)


def test_magic_eden_launchpad_parses_its_fixture(tmp_db):
    mints = nft.magic_eden_launchpad(
        fixture("magiceden_launchpad.json"), sol_price_usd=Decimal("180"), conn=tmp_db
    )
    assert [m.name for m in mints] == ["Monad Apes", "Sol Cats"]
    cats = mints[1]
    assert cats.chain is Chain.SOL and cats.supply == 3333
    assert cats.mint_price_native == Decimal("0.5") and cats.mint_price_usd == Decimal("90.00")
    assert cats.launch_ms and cats.url.endswith("sol_cats")
    assert mints[0].mint_price_usd is None, "a non-SOL price must not be invented"
    assert all(m.guards_verified is False for m in mints)


def test_magic_eden_tolerates_the_wrong_shape(tmp_db):
    assert nft.magic_eden_launchpad(mangled("json_object"), conn=tmp_db) == []
    assert any("expected list" in e.payload["detail"] for e in provider_errors(tmp_db))


def test_a_clean_public_mint_is_priced(tmp_db):
    """0.6 x ($400 x 0.6) = $144 gross; $90 mint + $0.50 gas + $12.50 time = $103."""
    score = nft.assess_mint(_clean_mint(), CFG)
    assert not score.refused
    assert score.gross_usd == Decimal("144.00")
    assert score.cost_usd == Decimal("103.00")
    assert score.ev_usd == Decimal("41.00")


def test_a_third_party_signer_mint_is_refused(tmp_db):
    score = nft.assess_mint(_clean_mint(guards=[nft.MintGuard.THIRD_PARTY_SIGNER]), CFG)
    assert score.refused
    assert any("thirdPartySigner" in b for b in score.blockers)
    plan = nft.build_mint_plan(_clean_mint(guards=[nft.MintGuard.THIRD_PARTY_SIGNER]), CFG)
    assert plan.refused and plan.steps == []


def test_a_gatekeeper_mint_is_refused(tmp_db):
    score = nft.assess_mint(_clean_mint(guards=[nft.MintGuard.GATEKEEPER]), CFG)
    assert score.refused and any("gatekeeper" in b for b in score.blockers)


def test_unread_guards_are_treated_as_blocking(tmp_db):
    score = nft.assess_mint(_clean_mint(guards_verified=False), CFG)
    assert score.refused
    assert any("not read" in b for b in score.blockers)


def test_allowlist_only_phase_is_refused(tmp_db):
    score = nft.assess_mint(
        _clean_mint(guards=[nft.MintGuard.ALLOW_LIST], public_phase=None), CFG
    )
    assert score.refused and any("not on the list" in b for b in score.blockers)


def test_a_mint_without_a_start_slot_is_refused_because_of_the_bot_tax(tmp_db):
    score = nft.assess_mint(_clean_mint(start_slot=None), CFG)
    assert score.refused
    assert any("Bot Tax" in b for b in score.blockers)


def test_an_unpriceable_mint_is_refused(tmp_db):
    assert nft.assess_mint(_clean_mint(recent_floor_usd=None), CFG).refused
    assert nft.assess_mint(_clean_mint(mint_price_usd=None), CFG).refused


def test_mint_above_the_discounted_floor_warns(tmp_db):
    score = nft.assess_mint(_clean_mint(mint_price_usd=Decimal("250")), CFG)
    assert score.ev_usd < 0
    assert any("at or above the discounted floor" in w for w in score.warnings)


def test_unknown_creators_get_the_base_rate(tmp_db):
    unknown = nft.assess_mint(_clean_mint(creator=nft.CreatorHistory()), CFG)
    flagged = nft.assess_mint(
        _clean_mint(creator=nft.CreatorHistory(prior_collections=3, rugged=True)), CFG
    )
    proven = nft.assess_mint(_clean_mint(), CFG)
    assert proven.ev_usd > unknown.ev_usd > flagged.ev_usd
    assert any("rug" in w for w in flagged.warnings)


def test_mint_plan_carries_the_start_slot_for_the_executor(tmp_db):
    plan = nft.build_mint_plan(_clean_mint(), CFG)
    mint_step = [s for s in plan.steps if s.action is PlanAction.MINT][0]
    assert mint_step.start_slot == 298_000_000
    assert "never send earlier" in mint_step.detail
    assert plan.steps[-1].action is PlanAction.HOLD


def test_helius_mint_watch_parses_and_groups_by_candy_machine(tmp_db):
    import base58
    payloads = fixture("helius_nft_mint.json")
    machine = base58.b58encode(bytes([7]) * 32).decode()
    # The original hand-built fixture omitted the instruction discriminator and
    # used placeholder account strings. Add the documented Core mint layout.
    for payload in payloads[:2]:
        payload["instructions"] = [{
            "programId": nft.CORE_CANDY_MACHINE_PROGRAM,
            "accounts": [machine] * 11,
            "data": base58.b58encode(bytes([84, 175, 211, 156, 56, 250, 104, 118]) + bytes(4)).decode(),
        }]
    mints = nft.helius_mint_watch(tmp_db, payloads=payloads)
    assert len(mints) == 2, "two mints from one machine are one opportunity"
    by_machine = {m.candy_machine: m for m in mints}
    cats = by_machine[machine]
    assert cats.observed_mints == 2
    assert cats.start_slot is None  # another wallet's mint is not a verified public start
    assert cats.meta["observed_slot"] == 298_000_123
    assert cats.launch_ms is None
    assert cats.mint_price_native == Decimal("0.5")
    assert cats.mint_price_usd is None
    assert all(not m.guards_verified for m in mints), "a webhook cannot see the guards"
    unprocessed = tmp_db.execute(
        "SELECT COUNT(*) c FROM hunter_webhook_events WHERE processed_ms IS NULL"
    ).fetchone()["c"]
    assert unprocessed == 0
    assert tmp_db.execute("SELECT COUNT(*) c FROM hunter_webhook_events").fetchone()["c"] == 4


def test_helius_observations_are_refused_until_the_guards_are_read(tmp_db):
    mints = nft.helius_mint_watch(tmp_db, payloads=fixture("helius_nft_mint.json"))
    assert all(nft.assess_mint(m, CFG).refused for m in mints)


def test_the_nft_hunter_is_off_by_default(tmp_db):
    """Measured: 8% of 75 paid Solana launchpad mints trade at or above mint price, and a
    mint-everything portfolio returns -85.4%. The hunter is kept but not run."""
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("25"))
    assert nft.enabled() is False
    assert nft.refresh(tmp_db, mints=[_clean_mint()], cfg=cfg) == 0
    assert tmp_db.execute("SELECT COUNT(*) n FROM opportunities").fetchone()["n"] == 0


def test_the_operator_can_still_force_it_on(tmp_db, monkeypatch):
    monkeypatch.setenv("KAIBA_ENABLE_NFT_HUNTER", "1")
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("25"))
    assert nft.enabled() is True
    assert nft.refresh(tmp_db, mints=[_clean_mint()], cfg=cfg) == 1


def test_nft_refresh_and_report(tmp_db):
    cfg = HunterConfig(operator_hourly_usd=Decimal("50"), ev_threshold_usd=Decimal("25"))
    assert nft.refresh(tmp_db, mints=[_clean_mint()], cfg=cfg, force=True) == 1
    row = tmp_db.execute("SELECT * FROM opportunities WHERE kind='nft_mint'").fetchone()
    assert row["status"] == "open"
    assert json.loads(row["plan_json"])["steps"]
    assert len(events.recent(kinds=[EventKind.HUNTER_FOUND.value], conn=tmp_db)) == 1

    nft.refresh(tmp_db, mints=[_clean_mint(name="Gated", guards=[nft.MintGuard.GATEKEEPER])],
                cfg=cfg, force=True)
    report = nft.weekly_report(tmp_db)
    assert "REFUSED" in report and "Sol Cats" in report
    assert "never a revenue line" in report


# ============================================================== 9. listings


def test_bwenews_parses_listings_and_skips_noise(tmp_db):
    items = listings.bwenews_feed(fixture("bwenews_telegram.json"), conn=tmp_db)
    assert [(i.exchange, i.symbol) for i in items] == [
        ("binance", "ONDO"),
        ("upbit", "JUP"),
        ("bithumb", "XYZ"),
    ]
    assert items[2].delisting is True
    assert all(i.measurable for i in items)


def test_latency_is_measured_against_the_announcement(tmp_db):
    item = listings.ListingItem(
        exchange="binance", symbol="ONDO", title="Binance Will List Ondo (ONDO)",
        announced_ms=1_700_000_000_000, detected_ms=1_700_000_004_200, source="bwenews",
    )
    assert item.latency_ms == 4200 and item.measurable
    assert listings.record_listing(tmp_db, item) is True
    row = tmp_db.execute("SELECT * FROM listing_events").fetchone()
    assert row["latency_ms"] == 4200 and row["announced_ms"] == 1_700_000_000_000
    ev = events.recent(kinds=[EventKind.ALPHA_LISTING.value], conn=tmp_db)[0]
    assert ev.payload["latency_ms"] == 4200 and ev.payload["latency_measurable"] is True
    assert ev.payload["exchange"] == "binance" and ev.payload["symbol"] == "ONDO"


def test_unmeasurable_latency_is_null_not_zero(tmp_db):
    item = listings.ListingItem(exchange="bithumb", symbol="ABC", title="listing", source="bithumb")
    assert item.latency_ms is None and not item.measurable
    listings.record_listing(tmp_db, item)
    row = tmp_db.execute("SELECT * FROM listing_events").fetchone()
    assert row["latency_ms"] is None
    ev = events.recent(kinds=[EventKind.ALPHA_LISTING.value], conn=tmp_db)[0]
    assert ev.payload["latency_measurable"] is False


def test_the_same_announcement_is_recorded_once(tmp_db):
    item = listings.ListingItem(exchange="binance", symbol="ONDO", title="Will list ONDO",
                                announced_ms=1_700_000_000_000, source="bwenews")
    assert listings.record_listing(tmp_db, item) is True
    assert listings.record_listing(tmp_db, item) is False
    assert tmp_db.execute("SELECT COUNT(*) c FROM listing_events").fetchone()["c"] == 1


def test_binance_announcements_parse(tmp_db):
    items = listings.exchange_announcements({"binance": fixture("binance_announcements.json")},
                                            conn=tmp_db)
    assert len(items) == 1, "a system-upgrade notice is not a listing"
    assert items[0].symbol == "ONDO"
    assert items[0].announced_ms == 1789819200000
    assert items[0].url.endswith("a1b2c3d4")


def test_upbit_and_bithumb_announcements_parse(tmp_db):
    items = listings.exchange_announcements(
        {"upbit": fixture("upbit_announcements.json"), "bithumb": fixture("bithumb_notice.json")},
        conn=tmp_db,
    )
    by_exchange = {i.exchange: i for i in items}
    assert by_exchange["upbit"].symbol == "JUP" and by_exchange["upbit"].announced_ms
    assert by_exchange["bithumb"].symbol == "JUP" and by_exchange["bithumb"].announced_ms


def test_one_exchange_breaking_does_not_stop_the_others(tmp_db):
    items = listings.exchange_announcements(
        {"binance": mangled("json_wrong_shape"), "upbit": fixture("upbit_announcements.json")},
        conn=tmp_db,
    )
    assert [i.exchange for i in items] == ["upbit"]


def test_cryptolisting_free_tier_parses(tmp_db):
    items = listings.cryptolisting_feed(fixture("cryptolisting_feed.json"), conn=tmp_db)
    assert [i.symbol for i in items] == ["ONDO", "JUP"]  # the row with no symbol is dropped
    assert items[0].chain is Chain.ETH and items[0].token.startswith("0x")
    assert items[0].announced_ms is not None


def test_symbol_extraction_ignores_quote_currencies():
    assert listings.extract_symbol("Binance Will List Ondo (ONDO)") == "ONDO"
    assert listings.extract_symbol("Market Support for Jupiter (JUP) (KRW Market)") == "JUP"
    assert listings.extract_symbol("New pair (USDT) for $WIF") == "WIF"
    assert listings.extract_symbol("Nothing here") is None


def test_resolve_token_maps_a_unique_symbol(tmp_db):
    tmp_db.execute(
        "INSERT INTO tokens (chain,address,symbol,first_seen_ms) VALUES ('eth','0xabc','ONDO',1)"
    )
    assert listings.resolve_token("ondo", None, tmp_db) == "0xabc"
    assert listings.resolve_token("ONDO", Chain.ETH, tmp_db) == "0xabc"


def test_resolve_token_refuses_an_ambiguous_symbol(tmp_db):
    tmp_db.execute(
        "INSERT INTO tokens (chain,address,symbol,first_seen_ms) VALUES ('sol','Tok1','JUP',1)"
    )
    tmp_db.execute(
        "INSERT INTO tokens (chain,address,symbol,first_seen_ms) VALUES ('sol','Tok2','JUP',1)"
    )
    assert listings.resolve_token("JUP", Chain.SOL, tmp_db) is None
    warned = [
        e for e in events.recent(kinds=[EventKind.SYSTEM.value], conn=tmp_db)
        if e.payload.get("reason") == "ambiguous_symbol"
    ]
    assert warned and sorted(warned[0].payload["candidates"]) == ["Tok1", "Tok2"]


def test_resolve_token_returns_none_for_an_unknown_symbol(tmp_db):
    assert listings.resolve_token("NOPE", None, tmp_db) is None
    assert listings.resolve_token("", None, tmp_db) is None


def test_listing_refresh_and_latency_stats(tmp_db):
    feeds = {
        "bwenews": lambda conn=None: listings.bwenews_feed(
            fixture("bwenews_telegram.json"), conn=conn
        ),
        "cryptolisting": lambda conn=None: listings.cryptolisting_feed(
            fixture("cryptolisting_feed.json"), conn=conn
        ),
    }
    assert listings.refresh(tmp_db, feeds) == 5
    assert listings.refresh(tmp_db, feeds) == 0, "a re-poll of the same items is not news"
    stats = listings.latency_stats(tmp_db)
    assert stats["bwenews"]["n"] == 3 and stats["bwenews"]["measured_n"] == 3
    assert stats["bwenews"]["median_ms"] is not None
    report = listings.weekly_report(tmp_db)
    assert "Median latency" in report and "ONDO" in report


# ============================================================== 10. schema


def test_migration_009_adds_the_hunter_tables(tmp_db):
    tables = {
        r["name"]
        for r in tmp_db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"opportunities", "listing_events", "hunter_webhook_events", "hunter_sources"} <= tables
    cols = {r["name"] for r in tmp_db.execute("PRAGMA table_info(opportunities)")}
    assert {"confidence", "warnings_json", "rationale_json", "source", "gross_usd"} <= cols


def test_ev_score_serialises_for_storage():
    score = score_opportunity(base_opportunity(), CFG)
    blob = score.model_dump(mode="json")
    assert blob["ev_usd"] == "98.00"
    assert EvScore.model_validate(blob).ev_usd == Decimal("98.00")
