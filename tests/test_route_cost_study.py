import copy
import hashlib

import pytest

from kaiba.learning.route_cost_study import DAY_MS, H4_SIZE, VERSION, evaluate

START = 1_793_750_400_000


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def attempt(index, *, size=H4_SIZE, direct_basis="provider_quote", saving_bps=200):
    token = digest(index % 7)
    now = START + index % 3 * DAY_MS + index * 3_000
    common = {"token_sha256": token, "buy_ms": now + 100, "sell_ms": now + 1_000,
              "buy_lamports": size, "bought_atoms": 700, "sold_atoms": 700,
              "outside_cost_lamports": 10_000}
    return {"attempt_ms": now, "size_lamports": size, "token_sha256": token,
            "gmgn": {**common, "basis": "provider_quote", "sell_lamports": size - size // 20,
                     "source_sha256": digest((index, "gmgn"))},
            "direct": {**common, "basis": direct_basis, "source_sha256": digest((index, "direct")),
                       "sell_lamports": size - size // 20 + size * saving_bps // 10_000}}


def capture(rows):
    return {"version": VERSION, "registered_ms": START, "as_of_ms": START + 3 * DAY_MS, "attempts": rows}


def test_registered_cost_line_can_pass_with_comparable_quotes_and_explicit_external_costs():
    rows = [attempt(i) for i in range(41)]
    for row in rows:
        row["direct"]["outside_cost_lamports"] += 50_000  # 10 bps at the registered size
    out = evaluate(capture(rows))
    paired = out["by_size"][str(H4_SIZE)]["paired_quotes"]
    assert paired["median_saving_bps"] == paired["one_sided_90_lower_bps"] == 190
    assert paired["h4_verdict"] == "PASS_QUOTE_COST_LINE"
    assert out["funded_pnl_verified"] is False and out["promotion_authority"] is False


def test_models_larger_sizes_and_missing_legs_never_complete_h4():
    rows = [attempt(i) for i in range(39)]
    rows += [attempt(39, direct_basis="curve_model"), attempt(40, size=100_000_000), attempt(41)]
    rows[-1]["direct"] = None
    sizes = evaluate(capture(rows))["by_size"]
    h4 = sizes[str(H4_SIZE)]
    assert h4["paired_quotes"]["h4_verdict"] == "PENDING"
    assert h4["curve_model_pairs"]["pairs"] == 1
    assert h4["coverage"] == {"attempted": 41, "unpaired": 1, "missing_gmgn": 0, "missing_direct": 1}
    assert sizes["100000000"]["paired_quotes"]["pairs"] == 1


def test_low_savings_fail_and_reusing_one_receipt_is_refused():
    rows = [attempt(i, saving_bps=125) for i in range(40)]
    assert evaluate(capture(rows))["by_size"][str(H4_SIZE)]["paired_quotes"]["h4_verdict"] == "FAIL_QUOTE_COST_LINE"
    rows[1]["direct"]["source_sha256"] = rows[0]["direct"]["source_sha256"]
    with pytest.raises(ValueError, match="paired_source_mismatch"):
        evaluate(capture(rows))


@pytest.mark.parametrize(("field", "replacement", "reason"), [
    ("buy_lamports", H4_SIZE - 1, "route_size_mismatch"),
    ("sold_atoms", 699, "route_size_mismatch"),
    ("sell_ms", START + 2_101, "pair_skew_exceeded"),
    ("token_sha256", "f" * 64, "route_identity_mismatch"),
    ("outside_cost_lamports", None, "invalid_integer"),
])
def test_identity_timing_and_cost_mutations_refuse_the_pair(field, replacement, reason):
    row = copy.deepcopy(attempt(0))
    row["direct"][field] = replacement
    with pytest.raises(ValueError, match=reason):
        evaluate(capture([row]))
