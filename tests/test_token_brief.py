"""Evidence-boundary tests for the pure token brief; no providers or database calls."""

from __future__ import annotations

import copy
import json

import pytest

from kaiba.intelligence.token_brief import build_brief, main, render_brief, safe_url

NOW = 1_791_346_421_910
TOKEN = "0x1111111111111111111111111111111111111111"
WALLET = "0x2222222222222222222222222222222222222222"


def receipt(**extra):
    return {"basis": "PROVIDER_REPORTED", "observed_at_ms": NOW, "provider": "fixture",
            "endpoint": "token.info", **extra}


def payload(**sections):
    return {"chain": "eth", "address": TOKEN, "as_of_ms": NOW, **sections}


def section(facts, **extra):
    return {"facts": facts, "receipt": receipt(**extra)}


def buyer(**extra):
    return {"chain": "eth", "address": WALLET, "buy_usd": 0, "token_realized_usd": -10,
            "token_unrealized_usd": None, "tags": ["smart_degen"], **extra}


def buyer_section(rows, **extra):
    return section({"rows": rows, "ordering": "current_top_traders", "coverage": "partial", **extra})


def reasons(brief):
    return {w["reason"] for w in brief["warnings"]}


def test_missing_is_not_zero_and_security_false_stays_false():
    result = build_brief(payload(market=section({"liquidity_usd": 0, "price_usd": None}),
                                 security=section({"honeypot": None, "mintable": False})))
    assert result["sections"]["market"]["facts"] == {"liquidity_usd": 0, "price_usd": None}
    assert result["sections"]["security"]["facts"]["honeypot"] is None
    assert result["sections"]["security"]["facts"]["mintable"] is False
    text = render_brief(result)
    assert "liquidity usd: $0" in text and "price usd: unknown" in text
    assert "honeypot: unknown" in text and "mintable: no" in text
    assert result["automatic_trade_verdict"] is None
    assert result["sections"]["buyers"]["facts"]["sample_wallet_count"] is None
    assert "Current top trader sample: unknown" in text


def test_successful_empty_buyer_page_is_known_zero_sample_not_population():
    result = build_brief(payload(buyers=buyer_section([])))
    facts = result["sections"]["buyers"]["facts"]
    assert facts["sample_wallet_count"] == 0
    assert facts.get("total_count") is None
    assert "Current top trader sample: 0" in render_brief(result)


def test_wrong_chain_wallet_history_cannot_appear_as_eth_profit():
    history = {"chain": "bsc", "period": "7d", "realized_usd": 999_999,
               "roi_basis": "reported", "receipt": receipt()}
    result = build_brief(payload(buyers=buyer_section([buyer(wallet_history=history)])))
    assert result["sections"]["buyers"]["facts"]["rows"][0]["wallet_history"] is None
    assert "chain_mismatched_history_excluded" in reasons(result)
    assert "999999" not in render_brief(result)


def test_duplicate_evm_addresses_do_not_inflate_wallet_count_or_buys():
    row = buyer(address="0xabcdefabcdefabcdefabcdefabcdefabcdefabcd", buy_usd=20)
    other = {**row, "address": row["address"].upper(), "buy_usd": 2000}
    result = build_brief(payload(buyers=buyer_section([row, other])))
    facts = result["sections"]["buyers"]["facts"]
    assert facts["sample_wallet_count"] == 1
    assert facts["rows"][0]["buy_usd"] == 20
    assert facts["independent_entity_count"] is None
    assert "duplicate_buyer_excluded" in reasons(result)


def test_sol_addresses_preserve_case_and_separate_wallet_identity():
    result = build_brief({"chain": "sol", "address": "AbC", "as_of_ms": NOW,
                          "buyers": buyer_section([buyer(chain="sol", address="ABC"),
                                                   buyer(chain="sol", address="abc")])})
    assert result["address"] == "AbC"
    assert result["sections"]["buyers"]["facts"]["sample_wallet_count"] == 2


def test_stale_values_are_retained_and_explicitly_labelled_stale():
    result = build_brief(payload(market=section({"price_usd": 2}, observed_at_ms=NOW - 120_001)))
    assert result["sections"]["market"]["facts"]["price_usd"] == 2
    assert result["sections"]["market"]["receipt"]["status"] == "stale"
    assert "stale" in render_brief(result)


def test_future_observation_cannot_supply_market_facts():
    result = build_brief(payload(market=section({"price_usd": 12_345}, observed_at_ms=NOW + 1)))
    assert result["sections"]["market"]["facts"] == {}
    assert "future_observation_excluded" in reasons(result)
    assert "12345" not in render_brief(result)


def test_future_history_is_excluded_while_current_token_facts_survive():
    history = {"chain": "eth", "period": "30d", "realized_usd": 123_456,
               "receipt": receipt(observed_at_ms=NOW + 1)}
    result = build_brief(payload(buyers=buyer_section([buyer(wallet_history=history)])))
    row = result["sections"]["buyers"]["facts"]["rows"][0]
    assert row["wallet_history"] is None and row["token_realized_usd"] == -10
    assert "123456" not in render_brief(result)


def test_both_history_windows_keep_their_distinct_values_and_receipts():
    seven = {"chain": "eth", "period": "7d", "realized_usd": 1200, "roi_pct": 5.5,
             "roi_basis": "reported period cost basis", "receipt": receipt(endpoint="portfolio.stats.7d")}
    thirty = {"chain": "eth", "period": "30d", "realized_usd": 3500, "roi_pct": 8,
              "roi_basis": "reported period cost basis", "receipt": receipt(endpoint="portfolio.stats.30d")}
    result = build_brief(payload(buyers=buyer_section([buyer(
        wallet_history=seven, wallet_histories={"7d": seven, "30d": thirty})])))
    row = result["sections"]["buyers"]["facts"]["rows"][0]
    assert row["wallet_histories"]["7d"]["realized_usd"] == 1200
    assert row["wallet_histories"]["30d"]["realized_usd"] == 3500
    assert row["wallet_histories"]["7d"]["receipt"]["endpoint"] == "portfolio.stats.7d"
    assert row["wallet_histories"]["30d"]["receipt"]["endpoint"] == "portfolio.stats.30d"
    text = render_brief(result)
    assert "Wallet history 7d on eth: realized $1.20K" in text
    assert "Wallet history 30d on eth: realized $3.50K" in text
    assert "portfolio.stats.7d" in text and "portfolio.stats.30d" in text


@pytest.mark.parametrize("bad", ["future", "chain", "period"])
def test_bad_history_window_is_excluded_without_losing_the_other_window(bad):
    seven = {"chain": "eth", "period": "7d", "realized_usd": 1200, "receipt": receipt()}
    thirty = {"chain": "eth", "period": "30d", "realized_usd": 99_999, "receipt": receipt()}
    if bad == "future":
        thirty["receipt"]["observed_at_ms"] = NOW + 1
    elif bad == "chain":
        thirty["chain"] = "bsc"
    else:
        thirty["period"] = "7d"
    result = build_brief(payload(buyers=buyer_section([buyer(
        wallet_histories={"7d": seven, "30d": thirty})])))
    row = result["sections"]["buyers"]["facts"]["rows"][0]
    assert row["wallet_histories"]["7d"]["realized_usd"] == 1200
    assert row["wallet_histories"]["30d"] is None
    assert "Wallet history 30d: unavailable" in render_brief(result)
    assert "$100.00K" not in render_brief(result)
    assert any("wallet_histories.30d" in item["field"] for item in result["warnings"])


def test_stale_30d_history_does_not_borrow_7d_freshness():
    seven = {"chain": "eth", "period": "7d", "realized_usd": 7, "receipt": receipt()}
    thirty = {"chain": "eth", "period": "30d", "realized_usd": 30,
              "receipt": receipt(observed_at_ms=NOW - 900_001)}
    result = build_brief(payload(buyers=buyer_section([buyer(
        wallet_histories={"7d": seven, "30d": thirty})])))
    histories = result["sections"]["buyers"]["facts"]["rows"][0]["wallet_histories"]
    assert histories["7d"]["receipt"]["status"] == "fresh"
    assert histories["30d"]["receipt"]["status"] == "stale"
    assert "stale" in render_brief(result)


def test_positive_realized_week_cannot_hide_large_current_open_loss():
    seven = {"chain": "eth", "period": "7d", "realized_usd": 989, "unrealized_usd": -47_947,
             "roi_pct": 3.1, "roi_basis": "provider reported period cost basis",
             "receipt": receipt(endpoint="portfolio.stats.7d")}
    thirty = {**seven, "period": "30d", "realized_usd": 2000,
              "receipt": receipt(endpoint="portfolio.stats.30d")}
    result = build_brief(payload(buyers=buyer_section([buyer(
        wallet_histories={"7d": seven, "30d": thirty})])))
    text = render_brief(result)
    week_line = next(line for line in text.splitlines() if "Wallet history 7d on eth:" in line)
    month_line = next(line for line in text.splitlines() if "Wallet history 30d on eth:" in line)
    assert "realized $989.00" in week_line and "current open PnL -$47.95K" in week_line
    assert "current open PnL -$47.95K" in month_line
    assert "snapshot, not period flow" in week_line and "snapshot, not period flow" in month_line
    assert "reported ROI 3.1%" in week_line
    assert result["sections"]["buyers"]["facts"]["rows"][0]["wallet_histories"]["7d"][
        "unrealized_usd"] == -47_947


@pytest.mark.parametrize("basis,label", [("derived from cumulative buy cashflow", "derived ROI"),
                                       ("provider reported cost basis", "reported ROI"),
                                       (None, "ROI (basis unknown)")])
def test_roi_label_follows_its_basis_and_open_unknown_is_not_zero(basis, label):
    seven = {"chain": "eth", "period": "7d", "realized_usd": 50, "unrealized_usd": None,
             "roi_pct": 2, "roi_basis": basis, "receipt": receipt()}
    result = build_brief(payload(buyers=buyer_section([buyer(wallet_histories={"7d": seven})])))
    text = render_brief(result)
    line = next(line for line in text.splitlines() if "Wallet history 7d on eth:" in line)
    assert f"{label} 2%" in line
    assert "current open PnL unknown" in line
    if label == "derived ROI":
        assert "reported ROI" not in line


def observation(label, facts, **extra):
    return {"label": label, "facts": facts, "kind": "source claim", "chain": "eth",
            "receipt": receipt(**extra)}


def test_supplemental_fee_claim_and_creator_evidence_keep_separate_sources():
    fee = observation("Advertised fee", {"fee_pct": 2, "scope": "website claim"},
                      provider="website", endpoint="project.website")
    creator = observation("Logical creator", {"creator": WALLET, "factory": TOKEN},
                          basis="ONCHAIN_VERIFIED", provider="rpc", endpoint="creation.trace")
    creator["kind"] = "logical creator and factory observation"
    result = build_brief(payload(
        security=section({"buy_tax_bps": 0, "supplemental_observations": [fee]}),
        developer=section({"supplemental_observations": [creator]})))
    text = render_brief(result)
    assert "Advertised fee" in text and "fee pct: 2" in text
    assert "Source: website / project.website" in text
    assert "Logical creator" in text and "Source: rpc / creation.trace" in text
    assert "ONCHAIN\\_VERIFIED" in text
    assert "buy tax bps: 0" in text
    assert "Token-tax observations are separate from total execution costs." in text
    assert "total fee: 0" not in text


def test_wallet_transfer_context_is_readable_and_sourced_without_inventing_a_trade():
    transfer = observation("Named-wallet context", {"action": "token transfer", "amount_usd": 2500,
                           "trade_direction": None}, provider="rpc", endpoint="transfer.logs")
    result = build_brief(payload(buyers=buyer_section([buyer(supplemental_observations=[transfer])])))
    text = render_brief(result)
    assert "Named-wallet context" in text and "action: token transfer" in text
    assert "amount usd: $2.50K" in text and "trade direction: unknown" in text
    assert "Source: rpc / transfer.logs" in text
    assert result["automatic_trade_verdict"] is None


def test_supplemental_future_and_wrong_chain_are_excluded_individually():
    valid = observation("Visible product claim", {"product": "tooling"}, provider="website")
    future = observation("FUTURE_PRODUCT", {"product": "future"}, observed_at_ms=NOW + 1)
    wrong_chain = observation("WRONG_CHAIN_PRODUCT", {"product": "wrong"})
    wrong_chain["chain"] = "bsc"
    result = build_brief(payload(social=section(
        {"supplemental_observations": [valid, future, wrong_chain]})))
    observations = result["sections"]["social"]["facts"]["supplemental_observations"]
    assert len(observations) == 1 and observations[0]["label"] == "Visible product claim"
    text = render_brief(result)
    assert "FUTURE_PRODUCT" not in text and "WRONG_CHAIN_PRODUCT" not in text
    assert "future_observation_excluded" in reasons(result)
    assert "chain_mismatched_observation_excluded" in reasons(result)


def test_supplemental_source_clock_unknown_stays_unknown_and_does_not_borrow_parent_clock():
    claim = observation("Undated website claim", {"description": "Product claim"}, observed_at_ms=None)
    result = build_brief(payload(social=section({"supplemental_observations": [claim]})))
    obs = result["sections"]["social"]["facts"]["supplemental_observations"][0]
    assert obs["receipt"]["observed_at_ms"] is None and obs["receipt"]["status"] == "undated"
    assert "time unknown" in render_brief(result)


def test_separately_sourced_observation_survives_future_parent_receipt():
    claim = observation("Valid independently sourced claim", {"product": "observed"})
    result = build_brief(payload(social=section({"narrative": "FUTURE_NARRATIVE",
        "supplemental_observations": [claim]}, observed_at_ms=NOW + 1)))
    assert "narrative" not in result["sections"]["social"]["facts"]
    assert len(result["sections"]["social"]["facts"]["supplemental_observations"]) == 1
    assert "FUTURE_NARRATIVE" not in render_brief(result)


def test_protocol_profit_is_visibly_excluded_with_distinct_classification_and_trade_sources():
    protocol = {"address": TOKEN, "label": "LaunchHook", "classification_basis": "vetted_context",
                "token_realized_usd": 9599, "receipt": receipt(basis="ONCHAIN_VERIFIED", endpoint="protocol.code"),
                "trade_receipt": receipt(endpoint="token.traders")}
    result = build_brief(payload(buyers=buyer_section([buyer()], excluded_protocol_buyers=[protocol])))
    text = render_brief(result)
    assert "Excluded protocol accounts (excluded from trader-wallet assessment)" in text
    assert f"LaunchHook · {TOKEN}" in text
    assert "Excluded reported token realized PnL: $9.60K" in text
    assert "protocol.code" in text and "token.traders" in text
    assert result["sections"]["buyers"]["facts"]["sample_wallet_count"] == 1


def test_outside_page_wallet_crosscheck_is_visible_with_unknown_token_pnl_and_both_windows():
    outside = "0x4444444444444444444444444444444444444444"
    seven = {"chain": "eth", "address": outside, "period": "7d", "realized_usd": 6289,
             "unrealized_usd": -13_451, "roi_basis": "derived reported profit / cost",
             "identity": {"handle": "yodacalls", "basis": "vetted_context"},
             "receipt": receipt(endpoint="wallet.7d")}
    thirty = {**seven, "period": "30d", "realized_usd": 10_000, "receipt": receipt(endpoint="wallet.30d")}
    result = build_brief(payload(buyers=buyer_section([buyer()], limit=100, provider_returned_count=100,
        trader_page_cap_hit=True, wallet_crosschecks={"7d": [seven], "30d": [thirty]},
        wallet_crosschecks_not_top_trader_rows={"7d": [outside], "30d": [outside]})))
    facts = result["sections"]["buyers"]["facts"]
    assert facts["sample_wallet_count"] == 1 and len(facts["wallet_only_crosschecks"]) == 1
    crosscheck = facts["wallet_only_crosschecks"][0]
    assert crosscheck["token_realized_usd"] is None and crosscheck["token_unrealized_usd"] is None
    text = render_brief(result)
    assert "Wallet-only crosschecks outside this returned trader page" in text
    assert "token realized PnL unknown; token open PnL unknown" in text
    assert "Identity claim: yodacalls" in text
    assert "realized $6.29K; current open PnL -$13.45K" in text
    assert "Wallet history 30d on eth: realized $10.00K" in text
    assert "wallet.7d" in text and "wallet.30d" in text
    assert "page limit 100" in text and "page cap hit yes" in text


def test_crosschecks_for_existing_sample_wallets_do_not_duplicate_their_history():
    history = {"chain": "eth", "address": WALLET, "period": "7d", "realized_usd": 1234,
               "receipt": receipt(endpoint="wallet.7d")}
    result = build_brief(payload(buyers=buyer_section([buyer(wallet_histories={"7d": history})],
        wallet_crosschecks={"7d": [history]}, wallet_crosschecks_not_top_trader_rows={"7d": []})))
    assert result["sections"]["buyers"]["facts"]["wallet_only_crosschecks"] == []
    assert render_brief(result).count("Wallet history 7d on eth: realized $1.23K") == 1


def test_invalid_crosscheck_is_not_presented_as_success_or_zero_token_profit():
    outside = "0x4444444444444444444444444444444444444444"
    wrong_chain = {"chain": "bsc", "address": outside, "period": "7d", "realized_usd": 999_999,
                   "receipt": receipt()}
    future = {**wrong_chain, "chain": "eth", "period": "30d", "receipt": receipt(observed_at_ms=NOW+1)}
    result = build_brief(payload(buyers=buyer_section([], wallet_crosschecks={"7d": [wrong_chain], "30d": [future]},
        wallet_crosschecks_not_top_trader_rows={"7d": [outside], "30d": [outside]})))
    facts = result["sections"]["buyers"]["facts"]
    assert facts["wallet_crosschecks"] == {"7d": [], "30d": []}
    assert facts["wallet_only_crosschecks"][0]["wallet_histories"] == {"7d": None, "30d": None}
    text = render_brief(result)
    assert "token realized PnL unknown" in text and "token realized PnL $0" not in text
    assert "$1000.00K" not in text and "$1.00M" not in text


def test_researched_wallet_beyond_twenty_is_visible_as_targeted_not_top_twenty():
    rows = [buyer(address=f"0x{i:040x}") for i in range(1, 40)]
    history = {"chain": "eth", "address": rows[38]["address"], "period": "7d", "realized_usd": 989,
               "unrealized_usd": -47_947, "receipt": receipt()}
    rows[38].update({"identity": {"handle": "SlikkneSs", "basis": "provider_reported"},
                     "token_realized_usd": -334.930438, "wallet_histories": {"7d": history}})
    result = build_brief(payload(buyers=buyer_section(rows)))
    text = render_brief(result)
    assert "Researched buyers beyond displayed ranking" in text
    assert "not presented as the top 20 or chronological early buyers" in text
    assert "Identity claim: SlikkneSs" in text
    assert "realized token PnL (reported): -$334.93" in text
    assert "current open PnL -$47.95K" in text
    assert result["sections"]["buyers"]["facts"]["sample_wallet_count"] == 39


def test_nested_samples_are_bounded_in_markdown_and_retained_in_structured_brief():
    samples = [{"address": f"0x{i:040x}", "nested": {"untrusted": "x" * 1000}} for i in range(100)]
    result = build_brief(payload(holder_composition=section({"holder_sample": samples})))
    assert len(result["sections"]["holder_composition"]["facts"]["holder_sample"]) == 100
    text = render_brief(result)
    assert "100 source sample rows (details in structured JSON)" in text
    assert len(text) < 10_000


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity", "not a number", True])
def test_nonfinite_and_invalid_numeric_values_remain_unknown(value):
    result = build_brief(payload(market=section({"market_cap_usd": value})))
    assert result["sections"]["market"]["facts"]["market_cap_usd"] is None
    json.dumps(result, allow_nan=False)
    assert "invalid_number" in reasons(result)


def test_ratio_units_and_total_versus_float_are_distinct():
    facts = {"top10": {"value": .31, "unit": "fraction", "denominator": "total_supply"},
             "insiders": {"value": 60, "unit": "percent", "denominator": "tradable_float"},
             "unqualified": {"value": 50, "unit": "percent"}}
    result = build_brief(payload(holder_composition=section(facts)))
    rendered = render_brief(result)
    assert "31% of total\\_supply" in rendered
    assert "60% of tradable\\_float" in rendered
    assert "50% of denominator unknown" in rendered
    assert "ratio_denominator_unknown" in reasons(result)


def test_explicit_ratio_denominator_is_not_flagged_unknown_by_pct_field_name():
    result = build_brief(payload(holder_composition=section(
        {"top10_pct": {"value": 31, "unit": "percent", "denominator": "total_supply"}})))
    assert "ratio_denominator_unknown" not in reasons(result)
    assert "31% of total\\_supply" in render_brief(result)


@pytest.mark.parametrize("value,unit", [(31, "fraction"), (101, "percent"), (1, "tokens")])
def test_bad_ratio_cannot_be_rendered_as_valid_percentage(value, unit):
    result = build_brief(payload(holder_composition=section(
        {"top10": {"value": value, "unit": unit, "denominator": "total_supply"}})))
    assert result["sections"]["holder_composition"]["facts"]["top10"]["value"] is None


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///secret", "data:text/html,test",
                                "https://user:secret@example.com", "https://example.com\n#oops"])
def test_bad_links_are_removed_without_execution(url):
    assert safe_url(url) is None
    result = build_brief(payload(social=section({"website": url})))
    assert result["sections"]["social"]["facts"]["website"] is None
    assert "unsafe_or_invalid_link" in reasons(result)


def test_url_parentheses_cannot_close_a_markdown_link():
    assert safe_url("https://example.com/a)evil(") == "https://example.com/a%29evil%28"


def test_untrusted_markdown_and_identity_labels_are_escaped_and_claimed():
    hostile = "[CLICK](javascript:alert(1))\n# BUY NOW"
    result = build_brief(payload(social=section({"narrative": hostile, "weighted_score": 31}),
                                 buyers=buyer_section([buyer(identity={"handle": hostile,
                                                                      "basis": "provider label"})])))
    text = render_brief(result)
    assert "[CLICK](javascript" not in text
    assert "\\[CLICK\\]" in text and "\\# BUY NOW" in text
    assert "Identity claim:" in text and "weighted score (source claim): 31" in text
    assert "31%" not in text
    assert "Provider tags (claims)" in text


def test_current_top_traders_are_not_relabelled_the_first_twenty_buyers():
    result = build_brief(payload(early_buyers=buyer_section([buyer()])))
    assert not result["sections"]["early_buyers"]["facts"]["chronological_first_buyers_verified"]
    assert "first 20 chronological buyers have not been established" in render_brief(result)
    assert "early_buyer_ordering_not_verified" in reasons(result)


def test_row_freshness_does_not_borrow_fresh_collection_receipt():
    result = build_brief(payload(buyers=buyer_section([buyer(
        receipt=receipt(observed_at_ms=NOW - 900_001))])))
    assert result["sections"]["buyers"]["receipt"]["status"] == "fresh"
    assert result["sections"]["buyers"]["facts"]["rows"][0]["receipt"]["status"] == "stale"
    assert "stale" in render_brief(result)


def test_entity_mapping_counts_are_not_independence_claims():
    result = build_brief(payload(buyers=buyer_section([buyer(entity_id="entity1"),
        buyer(address="0x3333333333333333333333333333333333333333", entity_id="entity1")],
        total_count=100)))
    facts = result["sections"]["buyers"]["facts"]
    assert facts["sample_wallet_count"] == 2 and facts["total_count"] == 100
    assert facts["mapped_entity_count"] == 1 and facts["entity_mapping_complete"]
    assert facts["independent_entity_count"] is None


def test_input_is_not_mutated_and_input_cli_has_no_actions(tmp_path, capsys):
    raw = payload(market=section({"price_usd": "1.5"}), buyers=buyer_section([buyer()]))
    original = copy.deepcopy(raw)
    built = build_brief(raw)
    assert raw == original
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert main(["--input", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == built


def test_invalid_cli_input_reports_failure_instead_of_fake_evidence(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"chain": "bsc", "address": TOKEN}), encoding="utf-8")
    assert main(["--input", str(path)]) == 2
    assert "as_of_ms" in capsys.readouterr().err
