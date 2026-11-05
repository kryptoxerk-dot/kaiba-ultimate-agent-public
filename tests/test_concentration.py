"""Tests for kaiba.intelligence.concentration.

The behaviour these pin down, in order of how badly getting it wrong would hurt:

1. An empty cluster graph must never produce a number labelled "adjusted".
2. Ten addresses run by one hand must count as one entity, not ten.
3. Missing data is None with UNAVAILABLE, never a reassuring zero.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, ClusterEdge, EdgeType, EvidenceBasis
from kaiba.intelligence import cluster, entity
from kaiba.intelligence import concentration as conc
from kaiba.intelligence.concentration import Holding

SOL = Chain.SOL

# Base58-shaped Solana addresses. The normaliser rejects anything else on this chain.
A = [
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    "AGFEad2et2ZJif9jaGpdMixQqvW5i81aBdvKe7PHNfz3",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S",
    "BXXkv6z8ykpG1yuvUDPgh732wzVHB69RnB9YgSYh3itW",
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
]


def _holdings(*pcts: str) -> list[Holding]:
    return [Holding(A[i], Decimal(p)) for i, p in enumerate(pcts)]


def _link(conn, members: list[str], edge_type: EdgeType = EdgeType.CO_SIGNED) -> None:
    """Build a real entity out of real edges, through the real modules."""
    edges = [
        ClusterEdge(chain=SOL, a=members[0], b=other, edge_type=edge_type, confidence=0.9)
        for other in members[1:]
    ]
    cluster.persist_edges(edges, conn)
    entity.rebuild(conn, SOL)


# ------------------------------------------------------------------------ input parsing


def test_percentages_are_taken_as_percentages():
    out = conc.holdings_from_percentages({A[0]: 30, A[1]: 20}, SOL)
    assert [h.pct for h in out] == [Decimal(30), Decimal(20)]


def test_fractions_summing_to_one_are_read_as_fractions():
    out = conc.holdings_from_percentages({A[0]: "0.6", A[1]: "0.4"}, SOL)
    assert [h.pct for h in out] == [Decimal("60.0"), Decimal("40.0")]


def test_a_gmgn_shaped_fraction_is_not_read_as_a_tenth_of_a_percent():
    """GMGN's amount_percentage of 0.1308 means 13.08%. Reading it as 0.13% hides a whale."""
    rows = [
        {"address": A[0], "amount_percentage": 0.13079018398250383},
        {"address": A[1], "amount_percentage": 0.0512},
        {"address": A[2], "amount_percentage": 0.0301},
    ]
    out = conc.holdings_from_rows(rows, SOL)
    assert out[0].pct > Decimal(13) and out[0].pct < Decimal(14)


def test_small_percents_that_cannot_be_fractions_are_left_alone():
    # Twenty holders of 0.9% each: every value is under 1, but x100 would exceed supply.
    rows = [{"address": a, "pct": "0.9"} for a in A[:12]]
    out = conc.holdings_from_rows(rows, SOL)
    assert sum(h.pct for h in out) == Decimal("10.8")


def test_the_caller_can_override_the_unit_inference():
    out = conc.holdings_from_percentages({A[0]: "0.5"}, SOL, as_fraction=False)
    assert out[0].pct == Decimal("0.5")


def test_amounts_need_a_denominator():
    out = conc.holdings_from_amounts({A[0]: 250, A[1]: 250}, 1000, SOL)
    assert [h.pct for h in out] == [Decimal(25), Decimal(25)]
    assert conc.holdings_from_amounts({A[0]: 250}, 0, SOL) == []


def test_rows_from_a_provider_are_parsed_by_common_key_names():
    rows = [
        {"address": A[0], "amount_percentage": 12.5},
        {"wallet_address": A[1], "percentage": 7.5},
        {"owner": A[2], "pct": 1},
    ]
    out = conc.holdings_from_rows(rows, SOL)
    assert len(out) == 3
    assert out[0].pct == Decimal("12.5")


def test_rows_with_amounts_but_no_supply_return_nothing_rather_than_a_guess():
    rows = [{"address": A[0], "amount": "5000"}, {"address": A[1], "amount": "1000"}]
    assert conc.holdings_from_rows(rows, SOL) == []
    assert len(conc.holdings_from_rows(rows, SOL, total_supply=10_000)) == 2


def test_duplicate_addresses_are_summed_not_double_counted():
    out = conc.holdings_from_percentages([(A[0], 10), (A[0], 5), (A[1], 1)], SOL)
    assert out[0].address == A[0] and out[0].pct == Decimal(15)


def test_junk_values_are_dropped_not_coerced_to_zero():
    out = conc.holdings_from_percentages({A[0]: "not a number", A[1]: 5, A[2]: -3}, SOL)
    assert [h.address for h in out] == [A[1]]


# ------------------------------------------------------- the empty-graph contract


def test_empty_graph_reports_raw_and_refuses_to_call_it_adjusted(tmp_db):
    report = conc.concentration(SOL, A[0], _holdings("30", "20", "10"), tmp_db)
    assert report.raw.known and report.raw_pct == Decimal(60)
    assert report.adjusted_pct is None
    assert report.adjusted.basis is EvidenceBasis.UNAVAILABLE
    assert report.delta_pct is None
    assert report.adjustment_available is False
    assert report.note and "empty" in report.note


def test_best_marks_an_unadjusted_number_as_estimated(tmp_db):
    report = conc.concentration(SOL, A[0], _holdings("30", "20"), tmp_db)
    assert report.best.value == Decimal(50)
    assert report.best.basis is EvidenceBasis.ESTIMATED, "raw standing in for adjusted is not DERIVED"


def test_summary_says_the_adjustment_was_unavailable(tmp_db):
    report = conc.concentration(SOL, A[0], _holdings("30", "20"), tmp_db)
    assert "unavailable" in report.summary()


# ------------------------------------------------------------------- missing data


def test_no_holders_is_unknown_not_zero(tmp_db):
    report = conc.concentration(SOL, A[0], [], tmp_db)
    assert report.raw_pct is None
    assert report.raw.basis is EvidenceBasis.UNAVAILABLE
    assert report.adjusted_pct is None
    assert report.best.value is None
    assert "holders" in report.unknowns
    assert report.note and "not zero" in report.note


def test_everything_excluded_is_also_unknown_not_zero(tmp_db):
    report = conc.concentration(SOL, A[0], _holdings("50", "50"), tmp_db, exclude=[A[0], A[1]])
    assert report.raw_pct is None
    assert report.raw.basis is EvidenceBasis.UNAVAILABLE


# ----------------------------------------------------------------- the adjustment


def test_ten_addresses_one_hand_collapse_to_one_entity(tmp_db):
    """The whole point: raw top-10 says ten holders, the adjustment says one."""
    _link(tmp_db, A[:10])
    holdings = _holdings(*["6"] * 10) + [Holding(A[10], Decimal(5)), Holding(A[11], Decimal(4))]
    report = conc.concentration(SOL, A[0], holdings, tmp_db)
    assert report.adjustment_available is True
    assert report.raw_pct == Decimal(60)
    assert report.entity_count == 3
    assert report.collapsed_addresses == 9
    assert report.largest_entity is not None
    assert report.largest_entity.pct == Decimal(60)
    assert report.largest_entity.size == 10


def test_adjusted_top_n_counts_entities_not_addresses(tmp_db):
    _link(tmp_db, A[:3])
    # Twelve holders of 5% each; three of them are one hand. Raw top-10 = 50%.
    # After collapsing, the top-10 *entities* are the 15% one plus nine 5% ones = 60%.
    holdings = _holdings(*["5"] * 12)
    report = conc.concentration(SOL, A[0], holdings, tmp_db, top_n=10)
    assert report.raw_pct == Decimal(50)
    assert report.adjusted_pct == Decimal(60)
    assert report.delta_pct == Decimal(10)


def test_delta_is_reported_so_the_difference_is_visible(tmp_db):
    _link(tmp_db, A[:4])
    report = conc.concentration(SOL, A[0], _holdings(*["8"] * 12), tmp_db)
    assert report.delta_pct == report.adjusted_pct - report.raw_pct
    assert report.delta_pct > 0


def test_a_populated_graph_that_knows_none_of_these_holders_still_counts_as_adjusted(tmp_db):
    _link(tmp_db, [A[10], A[11]])
    report = conc.concentration(SOL, A[0], _holdings("30", "20", "10"), tmp_db)
    assert report.adjustment_available is True
    assert report.collapsed_addresses == 0
    assert report.adjusted_pct == report.raw_pct
    assert report.delta_pct == Decimal(0)
    assert report.note and "changed nothing" in report.note


def test_unclustered_addresses_remain_their_own_entity(tmp_db):
    _link(tmp_db, A[:2])
    shares, collapsed = conc.resolve_entities(SOL, _holdings("10", "10", "10"), tmp_db)
    assert collapsed == 1
    assert len(shares) == 2
    solo = [s for s in shares if not s.clustered]
    assert len(solo) == 1 and solo[0].size == 1


def test_lead_lag_never_merges_a_copier_into_the_leader(tmp_db):
    """Delegated to entity.py on purpose; this pins that we did not reimplement it."""
    _link(tmp_db, A[:3], edge_type=EdgeType.LEAD_LAG)
    report = conc.concentration(SOL, A[0], _holdings("20", "20", "20"), tmp_db)
    assert report.adjustment_available is False or report.collapsed_addresses == 0


# ------------------------------------------------------------------------ exclusions


def test_burn_addresses_are_dropped_automatically(tmp_db):
    burn = "1nc1nerator11111111111111111111111111111111"
    holdings = [Holding(burn, Decimal(90)), Holding(A[0], Decimal(6)), Holding(A[1], Decimal(4))]
    report = conc.concentration(SOL, A[0], holdings, tmp_db)
    assert report.raw_pct == Decimal(10)
    assert burn in report.excluded


def test_the_caller_can_exclude_the_bonding_curve(tmp_db):
    holdings = _holdings("80", "10", "5")
    report = conc.concentration(SOL, A[0], holdings, tmp_db, exclude=[A[0]])
    assert report.raw_pct == Decimal(15)
    assert report.excluded == (A[0],)
    assert report.holder_count == 2


def test_exclude_hubs_uses_the_hub_table_when_asked(tmp_db):
    from kaiba.intelligence import hubs

    hubs.add_hub(SOL, A[0], "cex", label="test cex", conn=tmp_db)
    without = conc.concentration(SOL, A[1], _holdings("50", "30"), tmp_db)
    with_hubs = conc.concentration(SOL, A[1], _holdings("50", "30"), tmp_db, exclude_hubs=True)
    assert without.raw_pct == Decimal(80)
    assert with_hubs.raw_pct == Decimal(30)


# ------------------------------------------------------------------- evidence hygiene


def test_every_known_figure_carries_a_receipt(tmp_db):
    _link(tmp_db, A[:3])
    report = conc.concentration(SOL, A[0], _holdings(*["10"] * 5), tmp_db)
    for measure in (report.raw, report.adjusted, report.delta):
        assert measure.known
        assert measure.receipt is not None
        assert measure.basis is EvidenceBasis.DERIVED


def test_top_n_is_honoured(tmp_db):
    report = conc.concentration(SOL, A[0], _holdings(*["10"] * 12), tmp_db, top_n=3)
    assert report.raw_pct == Decimal(30)
    assert report.top_n == 3


def test_from_rows_is_a_one_call_wrapper(tmp_db):
    rows = [{"address": A[i], "amount_percentage": 10} for i in range(5)]
    report = conc.from_rows(SOL, A[0], rows, tmp_db)
    assert report.raw_pct == Decimal(50)


def test_holdings_are_sorted_largest_first():
    out = conc.holdings_from_percentages({A[0]: 1, A[1]: 50, A[2]: 10}, SOL)
    assert [h.pct for h in out] == [Decimal(50), Decimal(10), Decimal(1)]


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True])
def test_non_numeric_shares_never_become_a_number(bad):
    assert conc.holdings_from_percentages({A[0]: bad}, SOL) == []
