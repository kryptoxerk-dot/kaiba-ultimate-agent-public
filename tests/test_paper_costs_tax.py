"""The token's own tax on both paper legs, charged once (design 2026-10-21 step 3b/3c).

MEASURED on the box 2026-10-21: bsc sell tax is nonzero on 332 of 499 dossiers built in 48 h and on
41 of 50 bsc paper positions opened in 7 days, and the paper broker charged none of it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import paper_costs as pc
from tests.test_paper_entry_quote_evm import LANDED


def test_a_one_percent_sell_tax_lowers_the_exit_by_exactly_100_bps_on_top_of_the_router_fee():
    price = Decimal("2")
    untaxed = pc.exit_price(Chain.BSC, price)
    taxed = pc.exit_price(Chain.BSC, price, tax_bps=100)
    assert untaxed == Decimal("1.98")  # the 1% router fee
    assert untaxed - taxed == price * Decimal("0.01")
    assert taxed == Decimal("1.96")


@pytest.mark.parametrize("tax_bps", [300, 1000, 3000])
def test_a_buy_tax_is_charged_as_the_tokens_it_keeps_not_as_an_added_fee(tax_bps):
    """2026-10-23: a fee-on-transfer buy delivers ``out x (1 - t)`` tokens, so the same spend pays
    ``price / (1 - t)`` per token received. The additive ``price x (1 + t)`` it replaced under-costs
    every taxed buy: 10% -> +10.0% instead of +11.1%; 30% -> +30.0% instead of +42.9%."""
    price, t = Decimal("2"), Decimal(tax_bps) / 10_000
    untaxed = pc.entry_price(Chain.BSC, price)
    assert untaxed == Decimal("2.02")  # the 1% router fee
    # independent of the function: what 1 USD of spend buys after the pool and the token's cut
    tokens_received_per_usd = (1 / untaxed) * (1 - t)
    assert abs(pc.entry_price(Chain.BSC, price, tax_bps=tax_bps) * tokens_received_per_usd - 1) < Decimal("1e-20")
    additive = price * (1 + Decimal(100 + tax_bps) / 10_000)
    assert pc.entry_price(Chain.BSC, price, tax_bps=tax_bps) > additive, "never cheaper than the additive charge"


def test_the_entry_receipt_states_the_effective_cost_rounded_up():
    assert pc.entry_bps(Chain.BSC) == 100 and pc.entry_bps(Chain.BSC, 0) == 100
    assert pc.entry_bps(Chain.BSC, 300) == 413  # (1.01 / 0.97 - 1) = 412.37 bps, rounded up
    assert pc.entry_bps(Chain.BSC, 1000) == 1223  # (1.01 / 0.90 - 1) = 1222.2 bps
    assert pc.entry_price(Chain.BSC, Decimal("1"), tax_bps=10_000) > 0, "a 100% tax is finite and ruinous"


def test_a_filled_leg_at_the_mark_keeps_the_row_dishonest_after_it_leaves_the_kept_legs(tmp_db):
    """2026-10-23: the tag keeps the newest legs only. ``honest`` was recomputed from the kept legs,
    so one early leg FILLED at the mark, followed by enough honest or unfilled legs (retries, ladder
    sells), dropped out and the row read honest again -- optimistic, silently."""
    pc.record_entry_basis(tmp_db, "pos_s", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    pc.record_exit_basis(tmp_db, "pos_s", Chain.BSC, mark_source="prices:dexscreener", fill="mark", tax_bps=0,
                         filled=True)
    for i in range(pc.MAX_EXIT_LEGS + 5):
        pc.record_exit_basis(tmp_db, "pos_s", Chain.BSC, mark_source="evm-pool:pancake-v2",
                             fill="v2_formula" if i % 2 else pc.RETRY_LEG, tax_bps=0, filled=bool(i % 2))
    tag = pc.read_basis(tmp_db, "pos_s")
    assert len(tag["exits"]) == pc.MAX_EXIT_LEGS and all(e["fill"] != "mark" for e in tag["exits"])
    assert not pc.is_honest(tag) and tag[pc.STICKY_EXIT_DISHONEST] is True
    assert pc.exit_degraded(tag), "the report must still count it as an exit-stage row"
    assert tag[pc.FILLED_LEG_COUNT] == 1 + (pc.MAX_EXIT_LEGS + 5) // 2


def test_a_non_pons_robinhood_pool_entry_never_counts_tax_free(tmp_db):
    """2026-10-23: a quoter fill carries the pool and hook fee but never runs the token's transfer,
    and robinhood charges no token tax and (on such a fill) no 250 bps stand-in. Only a Pons launch
    (its fee IS the hook) or a MEASURED tax counts; a tag from any older path is re-checked."""
    for pid, tt, counts in (("p_pons", pc.TRANSFER_TAX_PONS, True), ("p_meas", pc.TRANSFER_TAX_MEASURED, True),
                            ("p_un", pc.TRANSFER_TAX_UNMEASURED, False), ("p_none", None, False)):
        tag = pc.record_entry_basis(tmp_db, pid, Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter",
                                    fill="v4_quoter", buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False,
                                    transfer_tax=tt, landing=LANDED)
        assert pc.entry_counts(tag) is counts and tag["entry"]["honest"] is counts, pid
    legacy = {"chain": "robinhood", "entry": {"source": "pool:uniswap-v4:v4_quoter", "fill": "v4_quoter",
                                              "honest": True}, "exits": []}
    assert not pc.entry_counts(legacy), "a tag written before the field existed does not count"
    # 2026-10-23 (hA_): a curve entry also needs the broker to have FILLED it at our size (an executable
    # handed in); today's Pons curve entry carries none and the broker approximates it (see
    # tests/test_paper_broker_basis.py). The take rule is pinned on an at-size fill.
    at_size = {"basis": "router", "impact_basis": "executable_quote"}
    curve = pc.record_entry_basis(tmp_db, "p_curve", Chain.ROBINHOOD, source="curve:pons", fill="curve_mark",
                                  buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, curve_take_bps=100,
                                  broker=at_size, landing=LANDED)
    assert pc.entry_counts(curve), "a curve entry pays the curve's own take, read with its price"
    unread = pc.record_entry_basis(tmp_db, "p_curve_unread", Chain.ROBINHOOD, source="curve:pons",
                                   fill="curve_mark", buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False,
                                   broker=at_size, landing=LANDED)
    assert not pc.entry_counts(unread), "2026-10-23: an unread creator tax is never charged as the flat 250"
    bsc = pc.record_entry_basis(tmp_db, "p_bsc", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                                buy_tax_bps=100, sell_tax_bps=100, tax_in_price=False, landing=LANDED)
    assert pc.entry_counts(bsc), "bsc pays its known dossier tax"


def test_a_measured_transfer_tax_is_read_only_when_it_is_a_sane_integer(tmp_db):
    from kaiba.core.db import jdump

    def put(token, value):
        tmp_db.execute("INSERT OR REPLACE INTO kv (key, value, updated_ms) VALUES (?,?,0)",
                       (f"transfer_tax:robinhood:{token}", jdump(value)))

    put("0xaa", {"buy_bps": 300, "sell_bps": 500, "method": "eth_simulateV1"})
    put("0xbb", {"buy_bps": 300})
    put("0xcc", {"buy_bps": 300, "sell_bps": 10_000})
    put("0xdd", {"buy_bps": True, "sell_bps": 0})
    assert pc.measured_transfer_tax(tmp_db, Chain.ROBINHOOD, "0xAA") == (300, 500)
    for token in ("0xbb", "0xcc", "0xdd", "0xee"):
        assert pc.measured_transfer_tax(tmp_db, Chain.ROBINHOOD, token) is None, token


def test_only_sol_counts_by_date_every_other_chain_needs_its_tag():
    assert pc.counts_on_scoreboard(Chain.SOL, None) and pc.counts_on_scoreboard("sol", None)
    for chain in ("eth", "base", "bsc", "robinhood", "something-new"):
        assert not pc.counts_on_scoreboard(chain, None), chain


def test_unknown_tax_on_bsc_is_a_refusal_never_zero():
    assert pc.leg_tax_bps(Chain.BSC, None) == (None, pc.PAPER_TAX_UNKNOWN)
    assert pc.leg_tax_bps(Chain.BSC, "not a number") == (None, pc.PAPER_TAX_UNKNOWN)
    assert pc.leg_tax_bps(Chain.BSC, -1) == (None, pc.PAPER_TAX_UNKNOWN)
    assert pc.leg_tax_bps(Chain.BSC, Decimal("250")) == (250, None)


def test_a_flap_fill_that_already_netted_the_tax_is_not_charged_it_again():
    """``evm_price.flap_buy_net_quote(size, buy_tax_bps)`` takes the tax off the input; the fill price
    the curve gives already carries it. ``in_price`` makes the call site charge 0, known or not."""
    assert pc.leg_tax_bps(Chain.BSC, 500, in_price=True) == (0, None)
    assert pc.leg_tax_bps(Chain.BSC, None, in_price=True) == (0, None)


def test_robinhood_does_not_trust_the_dossier_tax_and_keeps_its_venue_extra():
    """robinhood dossiers say 0 on 836 of 836 tokens; the measured VENUE_EXTRA_BPS stands instead."""
    assert pc.leg_tax_bps(Chain.ROBINHOOD, None) == (0, None)
    assert pc.leg_tax_bps(Chain.ROBINHOOD, 900) == (0, None)
    assert pc.exit_bps(Chain.ROBINHOOD) == 100 + 250


def test_a_quoter_fill_carries_the_hook_fee_so_the_flat_venue_extra_is_not_added():
    """The flat 250 bps is robinhood's stand-in for the hook fee; a V4Quoter / QuoterV2 / v2-formula
    fill measured it per token (tests/test_evm_pool_price.py: +113 / +216 bps on two Pons pools)."""
    assert pc.FEE_IN_PRICE_FILLS == {"v2_formula", "quoter_v2", "v4_quoter"}
    assert pc.entry_bps(Chain.ROBINHOOD, venue_fee_in_price=True) == 100
    assert pc.exit_price(Chain.ROBINHOOD, Decimal("1"), venue_fee_in_price=True) == Decimal("0.99")
    assert pc.exit_price(Chain.ROBINHOOD, Decimal("1")) == Decimal("0.965")  # a mark keeps it
    assert pc.entry_price(Chain.BSC, Decimal("1"), venue_fee_in_price=True) == pc.entry_price(Chain.BSC, Decimal("1"))


def test_sol_costs_are_unchanged():
    assert pc.entry_price(Chain.SOL, Decimal("1")) == Decimal("1.0188")
    assert pc.exit_price(Chain.SOL, Decimal("1")) == Decimal("0.99")
    assert pc.leg_tax_bps(Chain.SOL, None) == (0, None)


def test_the_tag_is_honest_only_when_the_entry_and_every_filled_exit_came_from_the_pool(tmp_db):
    pc.record_entry_basis(tmp_db, "pos_t", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=100, sell_tax_bps=100, tax_in_price=False, landing=LANDED)
    assert not pc.is_honest(pc.read_basis(tmp_db, "pos_t")), "no exit yet"
    pc.record_exit_basis(tmp_db, "pos_t", Chain.BSC, mark_source="prices:dexscreener", fill="v2_formula",
                         tax_bps=100, filled=True)
    tag = pc.read_basis(tmp_db, "pos_t")
    assert pc.is_honest(tag) and tag["exits"][0]["mark_pool"] is False  # triggered by the feed, FILLED at the pool
    pc.record_exit_basis(tmp_db, "pos_t", Chain.BSC, mark_source="prices:dexscreener", fill="mark",
                         tax_bps=100, filled=True)
    assert not pc.is_honest(pc.read_basis(tmp_db, "pos_t")), "one leg filled at the DexScreener mark"


def test_a_dossier_entry_or_an_unknown_exit_tax_is_never_honest(tmp_db):
    pc.record_entry_basis(tmp_db, "pos_u", Chain.ROBINHOOD, source="dossier", fill=None, buy_tax_bps=0,
                          sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    pc.record_exit_basis(tmp_db, "pos_u", Chain.ROBINHOOD, mark_source="onchain-sample:v4", fill="v4_quoter",
                         tax_bps=0, filled=True)
    assert not pc.is_honest(pc.read_basis(tmp_db, "pos_u"))
    pc.record_entry_basis(tmp_db, "pos_v", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=None, tax_in_price=False, landing=LANDED)
    pc.record_exit_basis(tmp_db, "pos_v", Chain.BSC, mark_source="evm-pool:pancake-v2", fill="v2_formula",
                         tax_bps=None, filled=True)
    assert not pc.is_honest(pc.read_basis(tmp_db, "pos_v"))
    assert pc.record_entry_basis(tmp_db, "pos_w", Chain.SOL, source="pumpswap-pool:x", fill=None, buy_tax_bps=0,
                                 sell_tax_bps=0, tax_in_price=False, landing=LANDED) is None, "sol is not tagged"


def test_a_curve_mark_fill_must_have_been_decided_on_the_curve():
    assert pc.exit_is_honest("evm-venue:flap", "curve_mark")
    assert not pc.exit_is_honest("prices:dexscreener", "curve_mark")
