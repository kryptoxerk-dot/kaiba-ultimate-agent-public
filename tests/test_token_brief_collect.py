"""Offline collector/normalizer evidence contracts; every runtime dependency is stubbed."""

from __future__ import annotations

import copy
import json
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from kaiba.intelligence import token_brief_collect as collect
from kaiba.intelligence.token_brief import build_brief

TOKEN = "0x5df882f8788071b67d0283f958ca3cdc17f1ae2b"
WALLET = "0xe86872fb1931ebfda5526a9797b3eed54883bf39"
OTHER = "0xb4d03fc002e298b807432c6ae5ce1c0d603b1fc8"
HOOK = "0x322dcec4958c14e021a9f1cd49df11b9457968cc"
POOL = "0x000000000004444c5dc75cb358380d2e3de08a90"
CREATOR = "0xbb2429424000d2d37bdeea30b07cf74b79c7179a"
NOW = 1_791_348_383_194


def receipt(kind, **extra):
    return {"provider": "gmgn", "endpoint": collect.ENDPOINTS[kind],
            "observed_at_ms": NOW - 1000, "basis": "provider_reported", **extra}


def record(kind, payload, **extra):
    return {"kind": kind, "payload": payload, "receipt": receipt(kind), **extra}


def bundle():
    return {
        "chain": "eth", "address": TOKEN, "as_of_ms": NOW,
        "requested_wallets": [WALLET],
        "calls": [
            record("token_info", {
                "address": TOKEN, "name": "Zevio", "symbol": "ZEVIO",
                "circulating_supply": "1000000000", "total_supply": "2000000000",
                "liquidity": "97000", "holder_count": 774,
                "price": {"address": TOKEN, "price": "0.0007", "buys_1h": 91,
                          "sells_1h": 96, "volume_1h": "68500", "volume_24h": "1500000"},
                "pool": {"address": POOL, "base_address": TOKEN, "fee_ratio": "0"},
                "dev": {"address": TOKEN, "creator_address": CREATOR},
                "stat": {"holder_count": 775, "creator_hold_rate": "0",
                         "dev_team_hold_rate": "0.0252", "top70_sniper_hold_rate": "0.0252",
                         "top_bundler_trader_percentage": "0.43"},
                "wallet_tags_stat": {"smart_wallets": 23, "fresh_wallets": 89, "bundler_wallets": 147},
                "link": {"address": TOKEN, "website": "http://127.0.0.1/private",
                         "twitter_username": "zeviodotsh"},
            }),
            record("token_security", {"address": TOKEN, "is_honeypot": False, "can_not_sell": 0,
                                      "can_sell": 0, "buy_tax": "0", "sell_tax": "0",
                                      "renounced_mint": False, "renounced_freeze_account": False}),
            record("holders", [{"address": POOL, "amount_percentage": ".1165", "addr_type": 2},
                               {"address": WALLET, "amount_percentage": ".02", "usd_value": "20"}]),
            record("traders", [
                {"address": HOOK, "addr_type": 0, "buy_volume_cur": "418778", "realized_profit": "9599"},
                {"address": POOL, "addr_type": 2, "buy_volume_cur": "37000"},
                {"address": WALLET, "addr_type": 0, "buy_volume_cur": "14", "cost_cur": "3",
                 "sell_volume_cur": "7", "realized_profit": "-4", "unrealized_profit": "11",
                 "netflow_usd": "23", "tags": ["gmgn"], "maker_token_tags": ["sniper"]},
            ]),
            record("buyer_profit_7d", [{"wallet_address": WALLET, "realized_profit": "55",
                                      "realized_profit_cost": "500", "total_realized_profit": "-1000",
                                      "total_cost": "100000", "unrealized_profit": "-80"}]),
            record("buyer_profit_30d", [{"wallet_address": WALLET, "realized_profit": "20",
                                       "realized_profit_cost": "200", "unrealized_profit": "-80"}]),
            record("developer_tokens", {"inner_count": 0, "open_count": 1,
                                         "creator_ath_info": {"creator": CREATOR, "ath_token": TOKEN,
                                                              "ath_mc": "827633.14"},
                                         "tokens": [{"chain": "eth", "token_address": TOKEN,
                                                     "token_ath_mc": "827633.14"}]}),
        ],
    }


def context():
    return {
        "chain": "eth", "address": TOKEN,
        "receipt": {"provider": "public explorer", "endpoint": "verified protocols",
                    "basis": "public_chain_record", "observed_at_ms": NOW - 5000},
        "protocol_addresses": [{"address": HOOK, "label": "verified LaunchHook"},
                               {"address": POOL, "label": "verified PoolManager"}],
        "sections": {"security": {
            "facts": {"advertised_fee_percent": 1.5, "inferred_pool_base_fee_percent": 2,
                      "current_executable_fee_verified": False},
            "receipt": {"provider": "public explorer", "endpoint": "launch source",
                        "basis": "source_inference", "observed_at_ms": NOW - 5000},
        }},
        "wallet_context": {WALLET: {
            "facts": {"first_receipt_to_last_return_seconds": 168, "net_profit_verified": False},
            "receipt": {"provider": "public explorer", "endpoint": "transfers",
                        "basis": "public_chain_record", "observed_at_ms": NOW - 5000},
        }},
    }


def test_exact_window_units_and_conflicting_holder_counts():
    normalized = collect.normalize_provider_bundle(bundle(), context())
    facts = normalized["sections"]["market"]["facts"]
    assert Decimal(facts["market_cap_usd"]) == 700000
    assert Decimal(facts["fdv_usd"]) == 1400000
    assert facts["holder_count"] == 774 and facts["stat_holder_count"] == 775
    assert facts["holder_count_conflict"]
    assert facts["volume_1h_usd"] == "68500" and facts["volume_24h_usd"] == "1500000"
    assert facts["buy_1h_count"] == 91 and facts["sell_1h_count"] == 96
    assert facts["ath_market_cap_usd"] == "827633.14"
    assert facts["ath_market_cap_receipt"]["endpoint"] == "portfolio.created_tokens"


def test_protocol_contract_typed_zero_is_excluded_and_other_addresses_unverified():
    normalized = collect.normalize_provider_bundle(bundle(), context())
    buyers = normalized["sections"]["buyers"]["facts"]
    assert [row["address"] for row in buyers["rows"]] == [WALLET]
    assert {row["address"] for row in buyers["excluded_protocol_buyers"]} == {HOOK, POOL}
    row = buyers["rows"][0]
    assert row["contract_classification"] == "not_independently_checked"
    assert row["provider_addr_type"] == 0
    assert row["buy_usd"] == "14" and row["current_position_cost_usd"] == "3"
    assert row["provider_netflow_usd"] == "23" and row["provider_netflow_sign_unchanged"]
    assert row["token_realized_usd"] == "-4" and row["token_unrealized_usd"] == "11"
    assert not row["net_profit_verified"]


def test_without_vetted_protocol_context_no_tag_becomes_contract_proof():
    rows = collect.normalize_provider_bundle(bundle())["sections"]["buyers"]["facts"]["rows"]
    assert HOOK in {row["address"] for row in rows}
    assert all(row["contract_classification"] == "not_independently_checked" for row in rows)


def test_future_or_undated_protocol_classification_cannot_change_population():
    ctx = context()
    ctx["receipt"]["observed_at_ms"] = NOW + 1
    normalized = collect.normalize_provider_bundle(bundle(), ctx)
    assert len(normalized["sections"]["buyers"]["facts"]["rows"]) == 3
    assert any("classification_future_excluded" in reason
               for reason in normalized["sections"]["coverage"]["facts"]["normalization_warnings"])
    ctx["receipt"]["observed_at_ms"] = None
    assert len(collect.normalize_provider_bundle(bundle(), ctx)["sections"]["buyers"]["facts"]["rows"]) == 3
    ctx["receipt"]["observed_at_ms"] = NOW
    assert len(collect.normalize_provider_bundle(bundle(), ctx)["sections"]["buyers"]["facts"]["rows"]) == 1


def test_period_realized_cost_roi_and_open_snapshot_separated():
    row = collect.normalize_provider_bundle(bundle(), context())["sections"]["buyers"]["facts"]["rows"][0]
    seven, thirty = row["wallet_histories"]["7d"], row["wallet_histories"]["30d"]
    assert seven["realized_usd"] == "55" and seven["all_time_realized_usd"] == "-1000"
    assert Decimal(seven["roi_pct"]) == 11
    assert Decimal(thirty["roi_pct"]) == 10
    assert seven["unrealized_usd"] == "-80"
    assert "derived" in seven["roi_basis"] and not seven["net_profit_verified"]
    assert seven["receipt"]["endpoint"] == "portfolio.profits"


def test_transfer_taxes_not_pool_fees_and_current_share_not_launch_cohort():
    sections = collect.normalize_provider_bundle(bundle(), context())["sections"]
    security = sections["security"]["facts"]
    assert security["token_buy_tax_bps"] == "0" and security["token_sell_tax_bps"] == "0"
    assert security["pool_fee_current_route_verified"] is None
    assert security["sol_mint_authority_revoked"] is None
    supplemental = security["supplemental_observations"][0]
    assert supplemental["facts"]["inferred_pool_base_fee_percent"] == 2
    assert supplemental["receipt"]["basis"] == "source_inference"
    composition = sections["holder_composition"]["facts"]
    assert composition["creator_holding_share"]["value"] == "0"
    assert composition["team_holding_share"]["value"] == "0.0252"
    assert composition["bundler_trading_volume_share"]["value"] == "0.43"
    assert composition["bundler_trading_volume_share"]["denominator"] is None
    assert composition["launch_bundle_share"] is None
    assert composition["elite_wallet_count"] is None and composition["weighted_score"] is None
    assert sections["early_buyers"]["facts"]["first20_fresh_count"] is None


@pytest.mark.parametrize(("value", "expected"), [
    ("0.1", "10"), (0.1, "10"), ("0.0001", "0.01"), ("1e-4", "0.01"),
    ("0.000000000000000000000000000001", "0.0000000000000000000000000001"),
    ("0.123456789012345678901234567890123456789", "12.3456789012345678901234567890123456789"),
    ("99.9999", "9999.99"), (0, "0"), ("0.00", "0"),
])
def test_documented_pool_percent_is_exact_reported_bps_not_total_cost(value, expected):
    raw = bundle()
    info = raw["calls"][0]
    info["payload"]["pool"]["fee_ratio"] = value
    info["receipt"]["observed_at_ms"] = NOW - 2345
    info["receipt"]["response_digest"] = "retained-provider-response"
    before = copy.deepcopy(raw)
    security = collect.normalize_provider_bundle(raw)["sections"]["security"]
    facts = security["facts"]
    assert facts["provider_pool_fee_ratio_raw"] == value
    assert facts["provider_pool_fee_bps"] == expected
    assert facts["provider_pool_fee_basis"] == "provider_reported_component"
    assert facts["provider_pool_fee_receipt"] == info["receipt"]
    assert facts["provider_pool_fee_receipt"] is not info["receipt"]
    assert security["receipt"]["endpoint"] == "token.security"
    assert facts["provider_pool_fee_input_unit"] == "percent"
    assert facts["provider_pool_fee_output_unit"] == "basis_points"
    assert facts["provider_pool_fee_units_source"] == (
        "https://github.com/GMGNAI/gmgn-skills/blob/main/skills/gmgn-token/SKILL.md"
    )
    assert facts["provider_pool_fee_zero_reported"] is (expected == "0")
    assert "does not verify zero" in facts["provider_pool_fee_zero_caveat"]
    assert facts["pool_fee_current_route_verified"] is None
    assert facts["pool_fee_total_cost_complete"] is None
    assert facts["fees_and_route_require_separate_receipts"] is True
    assert facts["token_buy_tax_bps"] == facts["token_sell_tax_bps"] == "0"
    assert not any(key in facts for key in ("venue_fee_bps", "cost_complete", "net_profit_verified"))
    assert raw == before


def test_pool_fee_conversion_is_independent_of_decimal_precision_and_traps():
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert collect._reported_pool_fee_bps(
            Decimal("0.123456789012345678901234567890123456789")
        ) == "12.3456789012345678901234567890123456789"


@pytest.mark.parametrize("value", [
    None, True, False, "NaN", "sNaN", "Infinity", "-Infinity", float("nan"), float("inf"),
    -1, "-0.1", "-0", "100", 100, "100.001", "", "0.1%", "0_._1", " 0.1", "0.1 ",
    [], {}, "1e999", "1e-999", "0e999", "0e-999", "0." + "1" * 65, "0" * 129,
    Decimal("0e-999999"), Decimal("NaN"),
])
def test_bad_or_unbounded_pool_fee_stays_unknown_and_preserves_raw(value):
    raw = bundle()
    raw["calls"][0]["payload"]["pool"]["fee_ratio"] = value
    facts = collect.normalize_provider_bundle(raw)["sections"]["security"]["facts"]
    preserved = facts["provider_pool_fee_ratio_raw"]
    if isinstance(value, (float, Decimal)) and not value == value:
        assert str(preserved) == str(value)
    else:
        assert preserved == value
    assert facts["provider_pool_fee_bps"] is None
    assert facts["provider_pool_fee_basis"] == "unavailable"
    assert facts["provider_pool_fee_zero_reported"] is None
    assert facts["pool_fee_current_route_verified"] is facts["pool_fee_total_cost_complete"] is None
    assert facts["token_buy_tax_bps"] == facts["token_sell_tax_bps"] == "0"


def test_missing_pool_fee_does_not_use_royalty_or_cumulative_fee_fields():
    raw = bundle()
    info = raw["calls"][0]["payload"]
    del info["pool"]["fee_ratio"]
    info.update(trade_fee="0.1", total_fee="1", fee_ratio="2")
    info["fee_distribution"] = {"platform_data": {"list": [{"royalty_bps": 10000}]}}
    raw["calls"][1]["payload"]["pool"] = {"fee_ratio": "5"}
    facts = collect.normalize_provider_bundle(raw)["sections"]["security"]["facts"]
    assert facts["provider_pool_fee_ratio_raw"] is facts["provider_pool_fee_bps"] is None
    assert facts["provider_pool_fee_zero_reported"] is None
    assert facts["token_buy_tax_bps"] == facts["token_sell_tax_bps"] == "0"
    info["pool"]["fee_ratio"] = "0.1"
    assert collect.normalize_provider_bundle(raw)["sections"]["security"]["facts"]["provider_pool_fee_bps"] == "10"


@pytest.mark.parametrize("mutation", ["provider", "endpoint", "future", "undated", "chain", "token", "pool_chain", "pool_base"])
def test_pool_fee_requires_its_original_info_identity_and_receipt(mutation):
    raw = bundle()
    call = raw["calls"][0]
    call["payload"]["pool"]["fee_ratio"] = "0.1"
    assert collect.normalize_provider_bundle(raw)["sections"]["security"]["facts"]["provider_pool_fee_bps"] == "10"
    if mutation == "provider":
        call["receipt"]["provider"] = "foreign"
    elif mutation == "endpoint":
        call["receipt"]["endpoint"] = "token.security"
    elif mutation == "future":
        call["receipt"]["observed_at_ms"] = NOW + 1
    elif mutation == "undated":
        call["receipt"]["observed_at_ms"] = None
    elif mutation == "chain":
        call["chain"] = "sol"
    elif mutation == "token":
        call["payload"]["address"] = OTHER
    elif mutation == "pool_chain":
        call["payload"]["pool"]["chain"] = "sol"
    else:
        call["payload"]["pool"]["base_address"] = OTHER
    facts = collect.normalize_provider_bundle(raw)["sections"]["security"]["facts"]
    assert facts["provider_pool_fee_ratio_raw"] is facts["provider_pool_fee_bps"] is None
    assert facts["provider_pool_fee_receipt"]["basis"] == "unavailable"
    assert facts["token_buy_tax_bps"] == facts["token_sell_tax_bps"] == "0"


def test_pool_fee_receipt_does_not_require_or_borrow_security_receipt():
    raw = bundle()
    raw["calls"][0]["payload"]["pool"]["fee_ratio"] = "0.1"
    raw["calls"] = [call for call in raw["calls"] if call["kind"] != "token_security"]
    section = collect.normalize_provider_bundle(raw)["sections"]["security"]
    assert section["receipt"]["basis"] == "unavailable"
    assert section["facts"]["provider_pool_fee_bps"] == "10"
    assert section["facts"]["provider_pool_fee_receipt"] == raw["calls"][0]["receipt"]
    assert section["facts"]["token_buy_tax_bps"] is section["facts"]["token_sell_tax_bps"] is None


def test_supplemental_context_does_not_override_fresh_facts_or_clocks():
    ctx = context()
    ctx["sections"]["market"] = {
        "facts": {"price_usd": "999", "holder_count": 1},
        "receipt": {"provider": "old alert", "endpoint": "post", "basis": "reported",
                    "observed_at_ms": NOW - 1000000},
    }
    raw, ctx_before = bundle(), copy.deepcopy(ctx)
    raw_before = copy.deepcopy(raw)
    normalized = collect.normalize_provider_bundle(raw, ctx)
    market = normalized["sections"]["market"]
    assert market["facts"]["price_usd"] == "0.0007"
    assert market["receipt"]["observed_at_ms"] == NOW - 1000
    assert market["facts"]["supplemental_observations"][0]["receipt"]["observed_at_ms"] == NOW - 1000000
    assert raw == raw_before and ctx == ctx_before
    built = build_brief(normalized)
    assert built["sections"]["buyers"]["facts"]["rows"][0]["supplemental_observations"][0]["facts"]["first_receipt_to_last_return_seconds"] == 168


@pytest.mark.parametrize("mutation", ["chain", "token", "nested_token", "provider", "endpoint", "future", "undated"])
def test_mismatched_or_unclocked_provider_market_is_unavailable(mutation):
    raw = bundle()
    call = raw["calls"][0]
    if mutation == "chain":
        call["payload"]["chain"] = "bsc"
    elif mutation == "token":
        call["payload"]["address"] = OTHER
    elif mutation == "nested_token":
        call["payload"]["price"]["address"] = OTHER
    elif mutation == "provider":
        call["receipt"]["provider"] = "fake_provider"
    elif mutation == "endpoint":
        call["receipt"]["endpoint"] = "portfolio.profits"
    elif mutation == "future":
        call["receipt"]["observed_at_ms"] = NOW + 1
    else:
        call["receipt"]["observed_at_ms"] = None
    normalized = collect.normalize_provider_bundle(raw)
    assert normalized["sections"]["market"]["facts"]["price_usd"] is None
    assert normalized["sections"]["market"]["receipt"]["basis"] == "unavailable"
    assert normalized["sections"]["coverage"]["facts"]["normalization_warnings"]


def test_mismatched_context_and_requested_wallets_rejected():
    ctx = context()
    ctx["chain"] = "bsc"
    with pytest.raises(ValueError, match="identity mismatch"):
        collect.normalize_provider_bundle(bundle(), ctx)
    raw = bundle()
    raw["requested_wallets"] = ["invalid"]
    with pytest.raises(ValueError, match="wallet identity"):
        collect.normalize_provider_bundle(raw)


def test_unsolicited_crosschain_duplicate_and_zero_cost_history_not_profitable_default():
    raw = bundle()
    raw["calls"][4]["payload"] += [
        {"wallet_address": OTHER, "realized_profit": "999"},
        {"wallet_address": WALLET, "chain": "bsc", "realized_profit": "888"},
    ]
    raw["calls"][5]["payload"][0]["realized_profit_cost"] = "0"
    normalized = collect.normalize_provider_bundle(raw)
    row = normalized["sections"]["buyers"]["facts"]["rows"][2]
    assert row["wallet_histories"]["7d"]["realized_usd"] == "55"
    assert row["wallet_histories"]["30d"]["roi_pct"] is None
    raw["calls"][4]["payload"].append(copy.deepcopy(raw["calls"][4]["payload"][0]))
    normalized = collect.normalize_provider_bundle(raw)
    assert "7d" not in normalized["sections"]["buyers"]["facts"]["rows"][2]["wallet_histories"]
    assert normalized["sections"]["buyers"]["facts"]["requested_wallets_unanswered"]["7d"] == [WALLET]


def test_duplicates_and_page_cap_remain_partial_not_first20():
    raw = bundle()
    raw["calls"][3]["payload"] = [copy.deepcopy(raw["calls"][3]["payload"][-1]) for _ in range(100)]
    facts = build_brief(collect.normalize_provider_bundle(raw))["sections"]["buyers"]["facts"]
    assert facts["sample_wallet_count"] == 1 and facts["trader_page_cap_hit"]
    assert facts["total_count"] is None and facts["coverage"] != "complete"
    assert facts["independent_entity_count"] is None
    assert not build_brief(collect.normalize_provider_bundle(raw))["sections"]["early_buyers"]["facts"]["chronological_first_buyers_verified"]


def test_creator_mismatch_and_other_chain_launch_cannot_supply_target_ath():
    raw = bundle()
    raw["calls"][-1]["payload"]["creator_ath_info"]["creator"] = OTHER
    normalized = collect.normalize_provider_bundle(raw)
    assert normalized["sections"]["market"]["facts"]["ath_market_cap_usd"] is None
    raw = bundle()
    raw["calls"][-1]["payload"]["tokens"][0]["chain"] = "bsc"
    assert collect.normalize_provider_bundle(raw)["sections"]["market"]["facts"]["ath_market_cap_usd"] is None


class FakeProvider:
    def __init__(self, fail=None, raises=False):
        self.attempts = []
        self.fail = fail
        self.raises = raises
        self.values = {call["kind"]: call for call in bundle()["calls"]}

    def _read(self, method, *args, **kwargs):
        from kaiba.core.limiter import Priority

        assert kwargs["priority"] is Priority.RESEARCH
        assert kwargs["ttl_s"] == 0 and kwargs["stale_grace_s"] == 0
        assert kwargs["wait_for_slot_s"] == 0 and 0 < kwargs["timeout_s"] <= 12
        self.attempts.append((method, args, kwargs))
        if method == self.fail and self.raises:
            raise RuntimeError("secret-diagnostic-must-never-appear")
        kind = {"token_info": "token_info", "token_security": "token_security",
                "token_holders": "holders", "token_traders": "traders",
                "portfolio_created_tokens": "developer_tokens"}.get(method)
        if method == "portfolio_profits":
            kind = f"buyer_profit_{kwargs['period']}"
        call = self.values[kind]
        failed = method == self.fail
        return SimpleNamespace(
            data=None if failed else copy.deepcopy(call["payload"]), ok=not failed,
            receipt=receipt(kind, basis="unavailable" if failed else "provider_reported"),
        )

    def __getattr__(self, name):
        if name.startswith("token_") or name.startswith("portfolio_"):
            return lambda *args, **kwargs: self._read(name, *args, **kwargs)
        raise AttributeError(name)


@pytest.fixture
def runtime(monkeypatch):
    from kaiba.core import db

    closed, wal_reads = [], []
    monkeypatch.setattr(db, "close_thread_conn", lambda: closed.append(True))
    monkeypatch.setattr(collect, "_config_check", lambda provider, timeout: {"available": True, "exit_code": 0})
    monkeypatch.setattr(collect, "_wal_bytes", lambda path: wal_reads.append(path) or 0)
    monkeypatch.setattr(collect.time, "time", lambda: NOW / 1000)
    return closed, wal_reads


def test_live_dependency_stubs_budget_chain_config_order_and_close(runtime, tmp_path):
    closed, wal_reads = runtime
    provider = FakeProvider()
    raw = collect.collect_provider_bundle("eth", TOKEN, wallets=[WALLET], context=context(),
                                          provider=provider, db_path=tmp_path / "unused.db")
    assert len(raw["calls"]) == 7
    assert [call["kind"] for call in raw["calls"][:4]] == ["token_info", "token_security", "holders", "traders"]
    assert len(closed) >= 7 and len(wal_reads) == 8
    assert all(args[1] == "eth" for _, args, _ in provider.attempts)
    assert provider.attempts[2][2]["limit"] == 100
    assert provider.attempts[3][2]["order_by"] == "buy_volume_cur"
    assert raw["calls"][4]["requested_wallets"] == [WALLET]
    assert raw["calls"][-1]["requested_creator"] == CREATOR


@pytest.mark.parametrize("raises", [False, True])
def test_unavailable_or_raised_provider_stops_next_reads_and_closes(runtime, tmp_path, raises):
    closed, _ = runtime
    provider = FakeProvider(fail="token_security", raises=raises)
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert len(provider.attempts) == 2 and len(raw["calls"]) == 2
    assert raw["stopped_after_unavailable"] and len(closed) >= 2
    assert "secret-diagnostic" not in json.dumps(raw)


def test_config_failure_and_wal_defer_make_no_provider_call_and_close(runtime, monkeypatch, tmp_path):
    closed, _ = runtime
    provider = FakeProvider()
    monkeypatch.setattr(collect, "_config_check", lambda provider, timeout: {"available": False})
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert raw["stop_reason"] == "config_check_unavailable" and closed
    assert not provider.attempts
    monkeypatch.setattr(collect, "_config_check", lambda *args: pytest.fail("config on high WAL"))
    monkeypatch.setattr(collect, "_wal_bytes", lambda path: collect.WAL_LIMIT_BYTES + 1)
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert raw["deferred"]["reason"] == "wal_over_1_5GB" and len(closed) >= 2


def test_actual_config_check_is_exact_read_only_command_and_diagnostics_not_saved(monkeypatch):
    seen = []
    monkeypatch.setenv("GMGN_ALLOW_AUTOMATED_TRADES", "1")
    monkeypatch.setenv("GMGN_DEBUG", "1")

    def run(argv, **kwargs):
        seen.append(argv)
        assert argv == ["node", "fixture-gmgn", "config", "--check"]
        assert kwargs["env"]["GMGN_ALLOW_AUTOMATED_TRADES"] == "0"
        assert "GMGN_DEBUG" not in kwargs["env"]
        assert kwargs["timeout"] == 10 and not kwargs["check"]
        return SimpleNamespace(returncode=1, stdout="secret-config-value", stderr="secret-config-value")

    monkeypatch.setattr(collect.subprocess, "run", run)
    result = collect._config_check(SimpleNamespace(cli_argv=lambda: ["node", "fixture-gmgn"]), 10)
    assert seen and result == {"exit_code": 1, "available": False}
    assert "secret" not in json.dumps(result)


def test_live_identity_error_stops_before_second_provider_call(runtime, tmp_path):
    provider = FakeProvider()
    provider.values["token_info"]["payload"]["address"] = OTHER
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert len(provider.attempts) == 1
    assert raw["stop_reason"] == "token_info:identity_or_receipt_invalid"
    assert raw["stopped_after_unavailable"]


def test_wal_rechecked_before_each_call_and_deadline_stops(runtime, monkeypatch, tmp_path):
    sizes = iter([0, 0, collect.WAL_LIMIT_BYTES + 1])
    monkeypatch.setattr(collect, "_wal_bytes", lambda path: next(sizes))
    provider = FakeProvider()
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert len(provider.attempts) == 1 and raw["deferred"]["reason"] == "wal_over_1_5GB"
    monkeypatch.setattr(collect, "_wal_bytes", lambda path: 0)
    ticks = iter([0, 0, 101])
    monkeypatch.setattr(collect.time, "monotonic", lambda: next(ticks, 102))
    provider = FakeProvider()
    raw = collect.collect_provider_bundle("eth", TOKEN, provider=provider, db_path=tmp_path / "unused.db")
    assert not provider.attempts and raw["deferred"]["reason"] == "command_deadline"


@pytest.mark.parametrize("chain", ["eth", "bsc", "robinhood"])
def test_explicit_chain_kept_on_all_live_reads(runtime, tmp_path, chain):
    provider = FakeProvider()
    provider.values["developer_tokens"]["payload"]["tokens"][0]["chain"] = chain
    raw = collect.collect_provider_bundle(chain, TOKEN, wallets=[WALLET], provider=provider,
                                          db_path=tmp_path / "unused.db")
    assert raw["chain"] == chain and all(args[1] == chain for _, args, _ in provider.attempts)


def test_wallet_bound20_and_profit_bound12_before_provider(runtime, tmp_path):
    addresses = [f"0x{n:040x}" for n in range(1, 21)]
    provider = FakeProvider()
    raw = collect.collect_provider_bundle("eth", TOKEN, wallets=addresses, max_wallets=20,
                                          provider=provider, db_path=tmp_path / "unused.db")
    assert len(raw["requested_wallets"]) == 12
    assert len(provider.attempts[4][1][0]) == 12
    with pytest.raises(ValueError, match="at most20"):
        collect.collect_provider_bundle("eth", TOKEN, wallets=addresses + [OTHER], provider=provider)
    with pytest.raises(ValueError, match="at most100"):
        collect.collect_provider_bundle("eth", TOKEN, deadline_s=101, provider=provider)


def test_offline_cli_does_not_call_live_or_fetch_metadata(tmp_path, monkeypatch, capsys):
    raw_path, ctx_path = tmp_path / "raw.json", tmp_path / "context.json"
    raw_path.write_text(json.dumps(bundle()))
    ctx_path.write_text(json.dumps(context()))
    monkeypatch.setattr(collect, "collect_provider_bundle", lambda *a, **kw: pytest.fail("offline live call"))
    output, markdown = tmp_path / "brief.json", tmp_path / "brief.md"
    assert collect.main(["--chain", "eth", "--address", TOKEN, "--input", str(raw_path),
                         "--context", str(ctx_path), "--output", str(output), "--markdown", str(markdown)]) == 0
    brief = json.loads(output.read_text())
    assert brief["chain"] == "eth"
    assert not brief["sections"]["social"]["facts"]["metadata_urls_fetched"]
    assert "first 20 chronological buyers have not been established" in markdown.read_text()
    assert json.loads(capsys.readouterr().out)["offline"]
    assert collect.main(["--chain", "bsc", "--address", TOKEN, "--input", str(raw_path),
                         "--output", str(output), "--markdown", str(markdown)]) == 2


def test_saved_current_reference_receipts_when_available():
    base = Path(__file__).resolve().parents[1] / "data/codex-token-brief-20261007"
    if not (base / "reference-provider.json").exists() or not (base / "verified-context.json").exists():
        pytest.skip("optional saved source receipts are absent from this isolated checkout")
    raw = json.loads((base / "reference-provider.json").read_text(encoding="utf-8"))
    ctx = json.loads((base / "verified-context.json").read_text(encoding="utf-8"))
    normalized = collect.normalize_provider_bundle(raw, ctx)
    assert normalized["sections"]["coverage"]["facts"]["validated_provider_calls_count"] == 7
    market = normalized["sections"]["market"]["facts"]
    assert Decimal(market["market_cap_usd"]) == Decimal("693193.57000000000")
    assert market["holder_count"] == 774 and market["stat_holder_count"] == 775
    buyers = normalized["sections"]["buyers"]["facts"]
    assert buyers["provider_returned_count"] == 100
    assert {HOOK, POOL}.issubset({row["address"] for row in buyers["excluded_protocol_buyers"]})
    by_wallet = {row["address"]: row for row in buyers["rows"]}
    assert Decimal(by_wallet[WALLET]["token_realized_usd"]) < 0
    assert Decimal(by_wallet[OTHER]["token_realized_usd"]) > 0
    assert Decimal(by_wallet[OTHER]["wallet_histories"]["7d"]["realized_usd"]) > 0
    assert Decimal(by_wallet[OTHER]["wallet_histories"]["7d"]["unrealized_usd"]) < 0
    assert by_wallet[WALLET]["supplemental_observations"][0]["facts"]["first_receipt_to_last_return_seconds"] == 168
    assert by_wallet[OTHER]["supplemental_observations"][0]["facts"]["first_receipt_to_last_return_seconds"] == 144
