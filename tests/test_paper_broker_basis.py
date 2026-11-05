"""A non-sol paper leg is honest only when the BROKER filled it at our size (2026-10-23, hA_).

Vanguard review (docs/research/claude-vanguard-snipe-review-20261023.md) section 6 items 1 and 3 and
section 4.2 pricing defect 1. The ``paper_basis`` tag was written from the QUOTE's source and never
compared with what ``PaperBroker`` (pinned) did with the price. MEASURED on the box 2026-10-23,
read-only, all 15 tags joined with their orders' ``paper.fill_basis``:

* every ``curve_mark`` leg (2 bsc Flap exits, 1 robinhood Pons exit), tagged honest, was a ``dex`` /
  ``constant_product_approximation`` fill. The two bsc rows were the only bsc rows the scoreboard
  counted: 2,388 and 7,121 bps of impact against $298 and $34.66 of curve reserve, -27.0% / -75.0%;
* every pool-labelled leg written after the tag existed was ``router`` / ``executable_quote``
  (honest: an on-chain quote at our size, e.g. the first robinhood launch-snipe fill, V4Quoter on the
  graduated Pons pool, 331 bps);
* a robinhood row counted at +62.0% had three buys; the first (65% of its tokens) was a dossier-priced
  ``dex`` fill, and each top-up rewrote the tag from scratch.

The tests below pin the writer (the engine and protection call sites pass the broker's own record),
the reader (an older tag is re-judged), the top-up merge, and the Flap exit priced on the curve at our
quantity with our holding put back.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from kaiba.core.db import fetch_all, jdump
from kaiba.core.schemas import Chain, EvidenceBasis, now_ms
from kaiba.execution import engine
from kaiba.execution import evm_pool_price as m
from kaiba.execution import evm_price as ep
from kaiba.execution import paper_costs as pc
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import (
    DEFAULT_FEE_BPS,
    DEFAULT_SLIPPAGE_FLOOR_BPS,
    DEFAULT_TIP_NATIVE,
    fill_basis,
    load_position,
)
from tests.test_paper import make_decision, write_dossier
from tests.test_paper_entry_quote_evm import (
    BSC_TOKEN,
    FLAP_H,
    FLAP_K,
    FLAP_TOKEN,
    LANDED,
    _flap_priced,
    flat_tape,
    pool_entry,
    priced_event,
)
from tests.test_paper_exit_evm import QTY, last_sell_basis, mark
from tests.test_watchdog import FakeSource, make_position, risk_file  # noqa: F401

BNB = Decimal("600")
AT_SIZE = {"basis": "router", "impact_basis": "executable_quote"}
APPROXIMATED = {"basis": "dex", "impact_basis": "constant_product_approximation"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from kaiba.execution import viability

    m.clear_caches()
    engine._POOL_REFUSALS.clear()
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.BSC: BNB, Chain.ROBINHOOD: Decimal("2500")})
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    yield
    m.clear_caches()


# ----------------------------------------------------------------------------- the reader: older tags


#: The two bsc rows' tags exactly as the box held them (kv ``paper_basis:<pos>``, read 2026-10-23).
BOX_BSC_TAGS = {
    "pos_0335efa5c5b06aa85c9e7223": {
        "chain": "bsc", "entry": {"source": "curve:flap", "fill": "curve_quote", "buy_tax_bps": 0, "sell_tax_bps": 0,
                                  "tax_in_price": True, "transfer_tax": None, "honest": True, "curve_take_bps": None,
                                  "venue_bps": 0, "note": "flap curve, progress 4.81%"},
        "exits": [{"mark_source": "evm-venue:flap", "mark_pool": True, "fill": "curve_mark", "tax_bps": 0,
                   "venue_bps": 0, "filled": True, "honest": True, "note": "sold on the curve the mark read"}],
        "honest": True, "filled_legs": 1},
    "pos_2c20c27806b5802e45de1b4c": {
        "chain": "bsc", "entry": {"source": "curve:flap", "fill": "curve_quote", "buy_tax_bps": 0, "sell_tax_bps": 0,
                                  "tax_in_price": True, "transfer_tax": None, "honest": True, "curve_take_bps": None,
                                  "venue_bps": 0, "note": "flap curve, progress 3.39%"},
        "exits": [{"mark_source": "evm-venue:flap", "mark_pool": True, "fill": "curve_mark", "tax_bps": 0,
                   "venue_bps": 0, "filled": True, "honest": True, "note": "sold on the curve the mark read"}],
        "honest": True, "filled_legs": 1},
}


@pytest.mark.parametrize("pid", sorted(BOX_BSC_TAGS))
def test_the_two_box_bsc_rows_are_exit_stage_rows_not_honest_ones(pid):
    """Their entries were ``router`` / ``executable_quote`` on the Flap curve; their ``curve_mark`` exits were
    the broker's approximation. Mutant pinned: an empty ``LEGACY_APPROXIMATED_FILLS`` (believing the stored
    ``honest``) must fail here.

    2026-10-23 (fd_): and their entries no longer count at all -- filled at the DECISION's curve with no landing
    drift (no ``entry.landing``; their token had no swap on the tape anyway). With a measured landing the same
    tag counts and is an exit-stage row, which is what pins the hA_ mutant."""
    tag = BOX_BSC_TAGS[pid]
    assert tag["honest"] is True, "what the box said"
    assert not pc.entry_counts(tag) and not pc.counts_on_scoreboard("bsc", tag) and not pc.is_honest(tag)
    landed = {**tag, "entry": {**tag["entry"], "landing": dict(LANDED)}}
    assert pc.entry_counts(landed) and pc.counts_on_scoreboard("bsc", landed)
    assert pc.exit_degraded(landed) and not pc.is_honest(landed)


def test_a_legacy_pons_curve_entry_is_not_counted_and_a_legacy_pool_one_still_is():
    """... a legacy pool one still is, with its landing drift measured (fd_: without it, it is not)."""
    pons = {"chain": "robinhood", "entry": {"source": "curve:pons", "fill": "curve_mark", "honest": True,
                                            "transfer_tax": pc.TRANSFER_TAX_PONS, "curve_take_bps": 300,
                                            "landing": dict(LANDED)}, "exits": []}
    pool = {"chain": "robinhood", "entry": {"source": "pool:uniswap-v4:v4_quoter", "fill": "v4_quoter", "honest": True,
                                            "transfer_tax": pc.TRANSFER_TAX_PONS}, "exits": []}
    assert not pc.entry_counts(pons) and not pc.entry_counts(pool)
    assert pc.entry_counts({**pool, "entry": {**pool["entry"], "landing": dict(LANDED)}})


def test_a_tag_with_more_filled_orders_than_legs_does_not_count_in_the_report(tmp_db):
    """The robinhood +62% row (pos_a58d5b5cd0cc8925c636e83e): its tag, as written by the last top-up,
    reads clean -- 1 entry, 1 honest exit -- over 8 filled orders. Mutant pinned: dropping
    ``flag_untagged_legs`` from ``paper_report._tagged_closed`` must fail here."""
    from kaiba.learning import paper_report as PR

    now = now_ms()
    for pid, orders in (("pos_rh_topped", 8), ("pos_rh_whole", 2)):
        make_position(tmp_db, position_id=pid, token="0x" + ("8a" if orders == 8 else "8b") * 20,
                      chain=Chain.ROBINHOOD)
        tmp_db.execute("UPDATE positions SET closed_ms=?, qty='0' WHERE position_id=?", (now - 1000, pid))
        for i in range(orders):
            oid = f"ord_{pid}_{i}"
            tmp_db.execute("INSERT INTO orders (order_id, decision_id, chain, token, side, lane, mode, input_token, "
                           "output_token, amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (oid, None, "robinhood", "0x", "buy" if i == 0 else "sell", "confluence-5", "shadow",
                            "0x", "0x", "1", "0", 1500, "filled", "paper", now - 5000 + i, now - 5000 + i))
            tmp_db.execute("INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
                           (pid, oid, "buy" if i == 0 else "sell", now - 5000 + i))
        clean = {"chain": "robinhood", "entry": {"source": "pool:uniswap-v4:v4_quoter", "fill": "v4_quoter",
                                                 "honest": True, "transfer_tax": pc.TRANSFER_TAX_PONS,
                                                 "landing": dict(LANDED)},
                 "exits": [{"mark_source": "prices:dexscreener", "fill": "v4_quoter", "tax_bps": 0, "filled": True,
                            "honest": True}], "honest": True, "filled_legs": 1}
        tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                       (f"{pc.BASIS_KV_PREFIX}{pid}", jdump(clean), now))
    tmp_db.commit()
    flt = PR.paper_filter(tmp_db, now)
    assert "pos_rh_whole" in flt.counted_off_sol
    assert "pos_rh_topped" not in flt.counted_off_sol and "pos_rh_topped" in flt.not_pool_priced


# ----------------------------------------------------------------------------- the writer: legs and top-ups


def test_an_exit_leg_is_judged_by_the_broker_record_not_by_its_label(tmp_db):
    pc.record_entry_basis(tmp_db, "pos_a", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE, landing=LANDED)
    tag = pc.record_exit_basis(tmp_db, "pos_a", Chain.BSC, mark_source="evm-pool:pancake-v2", fill="v2_formula",
                               tax_bps=0, filled=True, broker=AT_SIZE)
    assert pc.is_honest(tag) and not pc.exit_degraded(tag)
    pc.record_entry_basis(tmp_db, "pos_b", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE, landing=LANDED)
    tag = pc.record_exit_basis(tmp_db, "pos_b", Chain.BSC, mark_source="evm-pool:pancake-v2", fill="v2_formula",
                               tax_bps=0, filled=True, broker=APPROXIMATED)
    assert tag["exits"][-1]["honest"] is False and tag[pc.STICKY_EXIT_DISHONEST] is True
    assert pc.entry_counts(tag) and pc.exit_degraded(tag) and not pc.is_honest(tag)
    # nothing recorded by the broker is refused, never inferred from the label
    assert pc.broker_leg(None) == {"basis": None} and not pc.broker_at_size(pc.broker_leg(None))
    # a ledger write-off at zero is honest only as an exact total loss
    zero = pc.record_exit_basis(tmp_db, "pos_a", Chain.BSC, mark_source="none:unpriceable", fill=pc.TOTAL_LOSS,
                                tax_bps=0, filled=True, broker=pc.BOOKED_AT_ZERO)
    assert zero["exits"][-1]["honest"] is True
    bound = pc.record_exit_basis(tmp_db, "pos_b", Chain.BSC, mark_source="none:unpriceable", fill=pc.NO_FILL_BOUND,
                                 tax_bps=0, filled=True, broker=pc.BOOKED_AT_ZERO)
    assert bound["exits"][-1]["honest"] is False


def test_an_entry_leg_is_judged_by_the_broker_record_not_by_its_source(tmp_db):
    """Mutant pinned: ``entry_leg_honest`` ignoring ``broker`` must fail here."""
    good = pc.record_entry_basis(tmp_db, "pos_g", Chain.BSC, source="curve:flap", fill="curve_quote", buy_tax_bps=0,
                                 sell_tax_bps=0, tax_in_price=True, broker=AT_SIZE, landing=LANDED)
    bad = pc.record_entry_basis(tmp_db, "pos_x", Chain.BSC, source="curve:flap", fill="curve_quote", buy_tax_bps=0,
                                sell_tax_bps=0, tax_in_price=True, broker=APPROXIMATED, landing=LANDED)
    assert pc.entry_counts(good) and good["entry"]["broker"] == AT_SIZE
    assert not pc.entry_counts(bad) and bad["entry"]["honest"] is False


def test_a_top_up_merges_into_the_tag_and_an_earlier_leg_that_did_not_count_sticks(tmp_db):
    """Mutant pinned: the old rewrite (``exits: []``, flags dropped) must fail here."""
    pc.record_entry_basis(tmp_db, "pos_t", Chain.ROBINHOOD, source="dossier", fill=None, buy_tax_bps=0,
                          sell_tax_bps=0, tax_in_price=False, broker=APPROXIMATED, landing=LANDED)
    pc.record_exit_basis(tmp_db, "pos_t", Chain.ROBINHOOD, mark_source="prices:dexscreener", fill="mark",
                         tax_bps=0, filled=True, broker=APPROXIMATED)
    tag = pc.record_entry_basis(tmp_db, "pos_t", Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter",
                                fill="v4_quoter", buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False,
                                transfer_tax=pc.TRANSFER_TAX_PONS, broker=AT_SIZE, prior_filled_orders=2, landing=LANDED)
    assert tag["entry"]["honest"] is True, "this leg on its own is fine"
    assert tag[pc.STICKY_ENTRY_DISHONEST] is True and tag[pc.ENTRY_LEG_COUNT] == 2
    assert len(tag["exits"]) == 1 and tag[pc.STICKY_EXIT_DISHONEST] is True and not tag.get(pc.UNTAGGED_LEGS)
    assert not pc.entry_counts(tag)
    # the reverse order: an honest position topped up by an approximated buy
    pc.record_entry_basis(tmp_db, "pos_r", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE, prior_filled_orders=0, landing=LANDED)
    assert pc.entry_counts(pc.read_basis(tmp_db, "pos_r"))
    pc.record_entry_basis(tmp_db, "pos_r", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=APPROXIMATED,
                          prior_filled_orders=1, landing=LANDED)
    tag = pc.read_basis(tmp_db, "pos_r")
    assert not pc.entry_counts(tag)
    # a third, honest top-up does not wash it out
    tag = pc.record_entry_basis(tmp_db, "pos_r", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                                buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE,
                                prior_filled_orders=2, landing=LANDED)
    assert not pc.entry_counts(tag) and tag[pc.ENTRY_LEG_COUNT] == 3


def test_filled_orders_the_tag_never_saw_keep_the_row_off_the_scoreboard(tmp_db):
    """A top-up of a position whose earlier buy left no tag (opened before the tag, or on a path that
    does not write one). Mutant pinned: ignoring ``prior_filled_orders`` must fail here."""
    tag = pc.record_entry_basis(tmp_db, "pos_u", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                                buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE,
                                prior_filled_orders=1, landing=LANDED)
    assert tag[pc.UNTAGGED_LEGS] is True and not pc.entry_counts(tag)
    whole = pc.record_entry_basis(tmp_db, "pos_w", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                                  buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE,
                                  prior_filled_orders=0, landing=LANDED)
    assert pc.entry_counts(whole) and pc.UNTAGGED_LEGS not in whole


# ----------------------------------------------------------------------------- the call sites


def _bsc_handoff(conn, monkeypatch, entry: m.PaperEntry | None, decision_id: str):
    write_dossier(conn, token=BSC_TOKEN, chain=Chain.BSC, price="0.0006", liquidity="50000")
    from kaiba.core.schemas import Measure, Receipt

    zero = Measure(value=Decimal(0), basis=EvidenceBasis.PROVIDER_REPORTED,
                   receipt=Receipt(provider="fixture", endpoint="dyor"), freshness_budget_s=86_400)
    row = fetch_all(conn, "SELECT dossier_json FROM token_dossiers WHERE chain='bsc' AND address=?", (BSC_TOKEN,))[0]
    from kaiba.core.schemas import TokenDossier

    dossier = TokenDossier.model_validate_json(row["dossier_json"]).model_copy(
        update={"buy_tax_bps": zero, "sell_tax_bps": zero})
    conn.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain='bsc' AND address=?",
                 (dossier.model_dump_json(), BSC_TOKEN))
    monkeypatch.setattr(m, "paper_entry", lambda *a, **k: (entry, "ok") if entry is not None else (None, "no pool"))
    decision = make_decision(decision_id=decision_id, chain=Chain.BSC, token=BSC_TOKEN, size=13 * 10**16,
                             ts=now_ms())
    order = engine.handoff(decision, None, conn)
    assert order is not None and order.state.value == "filled"
    return order


def test_the_engine_tags_the_entry_from_the_broker_record_of_the_fill(tmp_db, monkeypatch):
    """A pool-labelled entry whose executable never reached the broker is filled on the broker's
    approximation, and says so. Mutant pinned: the engine passing ``broker=None`` (judging the label)
    must fail here; the positive control is the same entry with its executable."""
    order = _bsc_handoff(tmp_db, monkeypatch, pool_entry(executable_price_usd=None), "dec_bb_1")
    assert fill_basis(tmp_db, order.order_id)["impact_basis"] == "constant_product_approximation"
    event = priced_event(tmp_db)
    assert event["honest"] is False and event["broker_basis"]["basis"] == "dex"
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["broker"]["basis"] == "dex" and not pc.entry_counts(tag)


def test_the_engine_counts_the_same_entry_with_its_executable(tmp_db, monkeypatch):
    order = _bsc_handoff(tmp_db, monkeypatch, pool_entry(), "dec_bb_2")
    assert fill_basis(tmp_db, order.order_id)["impact_basis"] == "executable_quote"
    assert priced_event(tmp_db)["honest"] is True
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    assert pc.entry_counts(pc.read_basis(tmp_db, pid))


def test_a_dossier_buy_topped_up_at_the_pool_does_not_count(tmp_db, monkeypatch):
    """The +62% robinhood row's shape, end to end through the engine: the second buy adds to the same
    position and its tag keeps the first buy's verdict."""
    _bsc_handoff(tmp_db, monkeypatch, None, "dec_bb_3")  # dossier-priced: the pool reader refused
    _bsc_handoff(tmp_db, monkeypatch, pool_entry(), "dec_bb_4")
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["source"].startswith("pool:") and tag["entry"]["honest"] is True
    assert tag[pc.ENTRY_LEG_COUNT] == 2 and tag[pc.STICKY_ENTRY_DISHONEST] is True and not pc.entry_counts(tag)


def _bsc_position(conn, *, pid="pos_bw", qty=QTY):
    make_position(conn, position_id=pid, token=BSC_TOKEN, chain=Chain.BSC, qty=qty, entry="0.6")
    pc.record_entry_basis(conn, pid, Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, broker=AT_SIZE, landing=LANDED)
    conn.commit()
    return load_position(conn, pid)


@pytest.mark.parametrize("executable,honest", [(None, False), (Decimal("0.59"), True)])
def test_protection_tags_the_exit_from_the_broker_record_of_the_fill(tmp_db, monkeypatch, risk_file,  # noqa: F811
                                                                     executable, honest):
    """A pool-labelled exit leg the broker filled without an executable (its approximation) is not honest.
    Mutant pinned: ``_paper`` passing no ``broker`` to ``record_exit_basis`` must fail the first case."""
    position = _bsc_position(tmp_db)
    monkeypatch.setattr(wd.DefaultExitSubmitter, "_paper_evm_fill",
                        lambda self, position, pct, quote, pool_fill=None: wd._EvmExitFill(
                            0, executable, "v2_formula", "label says pool", Decimal("1200000")))
    assert wd.DefaultExitSubmitter(tmp_db, FakeSource({}))._paper(position, Decimal(100), mark("0.6"), "stop_loss").ok
    basis = last_sell_basis(tmp_db)
    leg = pc.read_basis(tmp_db, "pos_bw")["exits"][-1]
    assert leg["broker"]["impact_basis"] == basis["impact_basis"]
    assert leg["honest"] is honest and pc.exit_degraded(pc.read_basis(tmp_db, "pos_bw")) is (not honest)


# ----------------------------------------------------------------------------- the Flap sale at our size


def _x_whole(curve: ep.FlapCurve) -> Decimal:
    return Decimal(curve.virtual_token_atoms) / Decimal(10**18)


def _independent_sale_bnb(curve: ep.FlapCurve, held: int, sold: int) -> Decimal:
    """BNB a sale of ``sold`` of ``held`` atoms gets with the holding put back, before the Flap fee:
    the integral of the price ``K / X^2`` from ``X - held`` to ``X - held + sold`` -- ``K/X1 - K/X2``,
    re-derived here from the published curve, not taken from evm_price."""
    with localcontext() as ctx:
        ctx.prec = 60
        x_now = _x_whole(curve)
        x1 = x_now - Decimal(held) / Decimal(10**18)
        x2 = x1 + Decimal(sold) / Decimal(10**18)
        return FLAP_K / x1 - FLAP_K / x2


@pytest.mark.parametrize("held,sold", [(10**25, 10**25), (3 * 10**25, 10**25), (2 * 10**26, 2 * 10**26)])
def test_the_flap_sale_is_the_curve_integral_with_our_holding_put_back(held, sold):
    priced, _x, _reserve, _spot = _flap_priced(0)
    out, why = ep.flap_sell_quote_out(priced.curve, held, sold)
    assert out is not None, why
    expected = _independent_sale_bnb(priced.curve, held, sold) * Decimal("0.99") * Decimal(10**18)
    assert abs(Decimal(out) - expected) <= 3 and Decimal(out) <= expected + 1, (out, expected)
    # not the live curve without our holding (what the paper buy never paid for): that pays less, or --
    # when we hold more than the curve has sold -- cannot pay at all
    without = priced.curve.quote_out(sold)
    assert (without is None) if sold > priced.curve.tokens_sold_atoms else (without < out)


def test_buying_then_selling_on_an_unchanged_curve_returns_what_reached_the_curve_less_the_fee():
    """The holding put back is the curve our paper buy would have made: selling the bought atoms
    straight back returns exactly the BNB that reached the curve (to the wei), less the sell-side fee."""
    priced, *_ = _flap_priced(0)
    curve = priced.curve
    net_in = ep.flap_buy_net_quote(10**17, 0)
    atoms = curve.tokens_out(net_in)
    out, why = ep.flap_sell_quote_out(curve, atoms, atoms)
    assert out is not None, why
    back = net_in * (10_000 - ep.FLAP_PROTOCOL_FEE_BPS) // 10_000
    assert back - 3 <= out <= back, "rounded against us at each step, by a few wei"


def test_a_holding_that_would_carry_the_curve_past_graduation_is_not_priced():
    priced, *_ = _flap_priced(0)
    room = priced.curve.graduation_tokens_atoms - priced.curve.tokens_sold_atoms
    assert ep.flap_sell_quote_out(priced.curve, room, 10**18) == (None, "holding_crosses_graduation")
    assert ep.flap_sell_quote_out(priced.curve, 10**18, 2 * 10**18)[0] is None  # more than held
    snap = ep.flap_curve_snapshot(priced.curve)
    assert ep.flap_curve_from_snapshot(snap) == priced.curve and ep.flap_curve_from_snapshot({"r": 1}) is None


def _flap_source(priced: ep.VenuePrice) -> ep.EvmVenuePriceSource:
    return ep.EvmVenuePriceSource(None, readers={Chain.BSC: lambda token, rpc: (priced, "ok")},
                                  transports={Chain.BSC: lambda calls: []},
                                  quote_usd=ep.QuoteUsd(price_usd=lambda chain, token: BNB))


def test_the_flap_mark_carries_its_own_curve_state_and_never_serialises_it():
    priced, *_ = _flap_priced(0)
    quote = _flap_source(priced).quote(Chain.BSC, FLAP_TOKEN)
    assert quote.usable and quote.source == "evm-venue:flap"
    assert ep.flap_curve_from_snapshot(quote.venue_curve) == priced.curve
    assert "venue_curve" not in quote.model_dump(mode="json")
    assert quote.model_copy(update={"note": "x"}).venue_curve == quote.venue_curve
    foreign = ep.VenuePrice(price_quote_per_token=priced.price_quote_per_token, quote_token="0x" + "55" * 20,
                            quote_reserve_base=priced.quote_reserve_base, quote_decimals=18, venue="flap",
                            observed_ms=now_ms(), note="usdt curve", curve=priced.curve)
    assert _flap_source(foreign).quote(Chain.BSC, FLAP_TOKEN).venue_curve is None, "a non-native curve is not sold here"


def _flap_round_trip(conn, monkeypatch, *, carry_curve: bool):
    """A real Flap entry through the engine, then the paper exit on the SAME (unchanged) curve."""
    priced, *_ = _flap_priced(0)
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: (priced, "ok"))
    write_dossier(conn, token=FLAP_TOKEN, chain=Chain.BSC, price="0.0006", liquidity="50000")
    decided = now_ms()
    flat_tape(conn, Chain.BSC, FLAP_TOKEN, decided)  # fd_: a measured flow of zero, so the entry counts
    order = engine.handoff(make_decision(decision_id="dec_flap_rt", chain=Chain.BSC, token=FLAP_TOKEN,
                                         size=10**17, ts=decided), None, conn)
    assert order is not None and order.state.value == "filled"
    [pid] = [r["position_id"] for r in fetch_all(conn, "SELECT position_id FROM positions")]
    position = load_position(conn, pid)

    def no_pool(*a, **k):
        raise AssertionError("a curve-decided exit must not read a pool")

    monkeypatch.setattr(m, "exit_fill", no_pool)
    quote = _flap_source(priced).quote(Chain.BSC, FLAP_TOKEN)
    if not carry_curve:
        quote = quote.model_copy(update={"venue_curve": None})
    out = wd.DefaultExitSubmitter(conn, FakeSource({}))._paper(position, Decimal(100), quote, "stop_loss")
    assert out.ok, out.detail
    return priced, pid, position, load_position(conn, pid)


def test_a_flap_exit_is_sold_on_the_curve_at_our_quantity_and_books_exactly_its_fees(tmp_db, monkeypatch,
                                                                                    risk_file):  # noqa: F811
    """Mutants pinned: not routing bsc to the curve sale (``curve_mark`` again), selling into the live
    curve without our holding, or dropping the Flap fee all move the proceeds off the independent figure."""
    priced, pid, before, after = _flap_round_trip(tmp_db, monkeypatch, carry_curve=True)
    tag = pc.read_basis(tmp_db, pid)
    leg = tag["exits"][-1]
    assert leg["fill"] == pc.CURVE_QUOTE_EXIT and leg["broker"] == {**AT_SIZE, "impact_bps": 0,
                                                                     "over_tolerance": False}
    assert leg["honest"] and pc.is_honest(tag) and not pc.exit_degraded(tag)
    # independent: the curve's BNB for our tokens, less Flap 1%, router 1%, the broker's floor (30 bps),
    # its bsc fee (200 bps) and its tip -- the arithmetic re-done here, not read from the code under test
    gross = _independent_sale_bnb(priced.curve, before.qty, before.qty) * Decimal("0.99")
    after_router = gross * (1 - Decimal(100) / 10_000) * (1 - Decimal(DEFAULT_SLIPPAGE_FLOOR_BPS) / 10_000)
    native = after_router * Decimal(10**18)
    expected = native - native * DEFAULT_FEE_BPS[Chain.BSC] / 10_000 - DEFAULT_TIP_NATIVE[Chain.BSC]
    assert abs(Decimal(after.proceeds_native) - expected) <= Decimal(10**6), (after.proceeds_native, expected)
    # and so, on a curve nobody else touched, the round trip loses its fees and nothing else (~8.9%)
    loss = Decimal(after.realized_native) / Decimal(after.cost_native)
    assert Decimal("-0.095") < loss < Decimal("-0.08"), loss


def test_a_flap_exit_without_the_curve_state_is_the_old_approximation_and_not_honest(tmp_db, monkeypatch,
                                                                                    risk_file):  # noqa: F811
    priced, pid, before, after = _flap_round_trip(tmp_db, monkeypatch, carry_curve=False)
    leg = pc.read_basis(tmp_db, pid)["exits"][-1]
    assert leg["fill"] == "curve_mark" and leg["broker"]["impact_basis"] == "constant_product_approximation"
    assert not leg["honest"] and pc.exit_degraded(pc.read_basis(tmp_db, pid))
    # the approximation against the curve's own reserve books a far worse round trip on the same curve
    assert Decimal(after.realized_native) / Decimal(after.cost_native) < Decimal("-0.15")


# ----------------------------------------------------------------------------- the two box rows, re-priced


#: (cost wei, our tokens in atoms, curve reserve USD and spot USD of the deciding mark, the FX the mark
#: used, booked realized wei) -- read off the box 2026-10-23 (positions, order_events, the
#: ``quote_decision`` event's quote_provenance, native_prices).
BOX_EXITS = {
    "pos_0335efa5c5b06aa85c9e7223": (129520000000000000, 20411587611957514064404380, "298.27710282496443205550",
                                     "734.56", -34956455411002003),
    "pos_2c20c27806b5802e45de1b4c": (129520000000000000, 20845719799028992918380320, "34.66496114247884145344",
                                     "737.92", -97171311382972121),
}


@pytest.mark.parametrize("pid,booked_range", [("pos_0335efa5c5b06aa85c9e7223", ("-0.04", "-0.02")),
                                               ("pos_2c20c27806b5802e45de1b4c", ("-0.13", "-0.10"))])
def test_the_two_box_rows_sold_on_their_curve_lose_a_fraction_of_what_was_booked(pid, booked_range):
    """The published BSC Flap curve (r 6.14, h 107,036,752, K 6,797,205,657.28; the entries' spot matched
    it to 5e-6) at the reserve the deciding mark read, our tokens put back and sold, with every charge the
    new path makes. MEASURED -27.0% and -75.0% booked; about -3.0% and -11.6% on the curve."""
    cost, atoms, reserve_usd, fx, booked = BOX_EXITS[pid]
    wad = 10**18
    with localcontext() as ctx:
        ctx.prec = 60
        reserve = Decimal(reserve_usd) / Decimal(fx)
        x_plus_h = FLAP_K / (Decimal("6.14") + reserve)
        supply = 10**9
        sold = Decimal(supply) + FLAP_H - x_plus_h
    curve = ep.FlapCurve(r_scaled=int(Decimal("6.14") * wad), h_scaled=int(FLAP_H * wad), k_scaled=int(FLAP_K * wad),
                         total_supply_atoms=supply * wad, tokens_sold_atoms=int(sold * wad),
                         graduation_tokens_atoms=800_000_000 * wad, quote_reserve_base=int(reserve * wad),
                         token_decimals=18, quote_decimals=18, buy_tax_bps=0, sell_tax_bps=0)
    out, why = ep.flap_sell_quote_out(curve, atoms, atoms)
    assert out is not None, why
    native = Decimal(out) * (1 - Decimal(100) / 10_000) * (1 - Decimal(DEFAULT_SLIPPAGE_FLOOR_BPS) / 10_000)
    net = native - native * DEFAULT_FEE_BPS[Chain.BSC] / 10_000 - DEFAULT_TIP_NATIVE[Chain.BSC]
    curve_return = (net - cost) / cost
    low, high = (Decimal(v) for v in booked_range)
    assert low < curve_return < high, curve_return
    assert Decimal(booked) / cost < curve_return - Decimal("0.2"), "the approximation booked far below the curve"
