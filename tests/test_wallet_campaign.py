from kaiba.intelligence import grade
from kaiba.intelligence.wallet_campaign import summarize

NOW = 100_000_000
ADDRESS = "0x" + "a" * 40


def row(chain="bsc", **changes):
    return dict(chain=chain, address=ADDRESS, grade="B", score=45,
                evidence_weight=40, scored_at_ms=NOW-1000,
                model_version="kaiba-wallet-gmgn-v1") | changes


def test_cross_chain_grades_stay_separate_but_address_not_double_counted():
    result = summarize([row(), row("robinhood", address=ADDRESS.upper().replace("0X", "0x"))], [], now_ms=NOW, target=2)
    assert result["eligible_chain_address_pairs"] == 2
    assert result["eligible_unique_addresses"] == 1
    assert not result["inventory_target_reached"]


def test_provider_label_and_history_with_zero_closures_do_not_fill_target():
    for model in (grade.MODEL_ID, "gmgn:A", "unknown"):
        result = summarize([row(model_version=model, closed_trades=0)], [], now_ms=NOW, target=1)
        assert result["stored_ab_chain_address_pairs"] == 1
        assert result["eligible_unique_addresses"] == 0


def test_bad_scores_stale_rows_and_duplicates_cannot_fill_target():
    for changes in ({"score":float("nan")}, {"score":39.99}, {"evidence_weight":29.99},
                    {"scored_at_ms":NOW+1}, {"scored_at_ms":0}, {"grade":"A"}):
        assert summarize([row(**changes)], [], now_ms=NOW)["eligible_unique_addresses"] == 0
    assert summarize([row(), row()], [], now_ms=NOW)["eligible_unique_addresses"] == 0


def test_tape_needs_current_sample_audit_with_real_closed_tokens():
    wallet = row(model_version=grade.MODEL_ID_TAPE)
    audit = dict(chain="bsc", address=ADDRESS, current_tape_grade="B", current_tape_score=45,
                 clean_closed_episodes=8, closed_tokens=4, evidence_weight=40, audit_started_ms=NOW-500)
    assert summarize([wallet], [audit], now_ms=NOW, target=1)["inventory_target_reached"]
    for change in ({"clean_closed_episodes":7}, {"closed_tokens":3},
                   {"current_tape_score":46}, {"audit_started_ms":NOW-2000}):
        assert summarize([wallet], [audit | change], now_ms=NOW)["eligible_unique_addresses"] == 0


def test_solana_case_is_not_evm_normalized():
    from kaiba.intelligence.wallet_campaign import key
    address = "BiTsy" + "1"*39
    assert key("sol", address) == ("sol", address)
    assert key("eth", ADDRESS) is None
