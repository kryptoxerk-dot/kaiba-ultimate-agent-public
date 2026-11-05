"""Robinhood and bsc paper EXITS are filled from the pool at our quantity, and pay the token's sell tax
(design 2026-10-21 step 3b [R]: "exit fills are fills"; the deciding mark only triggers the exit).

MEASURED before (box, 2026-10-21): 54 of 62 robinhood and 19 of 19 bsc paper exits were priced off
DexScreener, and the bsc sell tax (nonzero on 41 of 50 bsc paper positions) was charged on none.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, EvidenceBasis, now_ms
from kaiba.execution import evm_pool_price as m
from kaiba.execution import paper_costs as pc
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import fill_basis, load_position
from tests.test_evm_pool_price import ETH_USD, FakeRpc, rh_ref, v4_quote_calldata
from tests.test_paper_entry_quote_evm import LANDED
from tests.test_watchdog import FakeSource, make_position, risk_file  # noqa: F401

BSC_TOKEN, BSC_PAIR = "0x" + "4c" * 20, "0x" + "4d" * 20
QTY = 1000 * 10**18  # 1,000 whole tokens in the broker's 18-decimal units


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from kaiba.execution import viability

    m.clear_caches()
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.BSC: Decimal("600"), Chain.ROBINHOOD: ETH_USD})
    yield
    m.clear_caches()


def bsc_position(conn, monkeypatch, *, reserves=(10**24, 10**21), rpc_default=None):
    """A bsc paper position on a WBNB v2 pair: 1,000,000 tokens / 1,000 WBNB -> 0.001 BNB = $0.60."""
    make_position(conn, position_id="pos_bx", token=BSC_TOKEN, chain=Chain.BSC, qty=QTY, entry="0.6")
    m.remember_ref(m.PoolRef(Chain.BSC, BSC_TOKEN, BSC_PAIR, "v2", "pancake-v2", True, m.WBNB, 25), conn)
    m._DECIMALS[(Chain.BSC.value, BSC_TOKEN)] = 18
    r0, r1 = reserves
    rpc = FakeRpc({(BSC_PAIR, m.SEL_GET_RESERVES): "0x" + format(r0, "064x") + format(r1, "064x") + "0" * 64},
                  default=rpc_default)
    monkeypatch.setattr(m, "bsc_pool_rpc_url", lambda: ("https://pool.example.invalid", "ok"))
    monkeypatch.setattr(m, "transport_for", lambda chain, conn=None, priority=None: rpc)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (Decimal("600"), "fixture"))
    conn.commit()
    return load_position(conn, "pos_bx"), rpc


def mark(price: str, source: str = "evm-pool:pancake-v2", chain: Chain = Chain.BSC, token: str = BSC_TOKEN,
         liquidity: str | None = "1200000") -> wd.PriceQuote:
    return wd.PriceQuote(price_usd=Decimal(price), liquidity_usd=Decimal(liquidity) if liquidity else None,
                         basis=EvidenceBasis.VERIFIED_ONCHAIN, observed_ms=now_ms(), source=source, chain=chain,
                         token=token, pool_id=BSC_PAIR)


def sell(conn, position, quote, pct=Decimal(100)) -> wd.ExitOutcome:
    return wd.DefaultExitSubmitter(conn, FakeSource({}))._paper(position, pct, quote, "stop_loss")


def last_sell_basis(conn) -> dict:
    order_id = fetch_all(conn, "SELECT order_id FROM orders WHERE side='sell' ORDER BY created_ms DESC")[0]["order_id"]
    return fill_basis(conn, order_id)


def test_a_bsc_exit_fills_from_the_pair_at_our_quantity_and_pays_the_entry_sell_tax(tmp_db, monkeypatch, risk_file):  # noqa: F811
    position, rpc = bsc_position(tmp_db, monkeypatch)
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=300, sell_tax_bps=500, tax_in_price=False, landing=LANDED)
    out = sell(tmp_db, position, mark("0.6"))
    assert out.ok, out.detail
    assert len(rpc.sends) == 1, "one pool read at EXIT for the fill"
    basis = last_sell_basis(tmp_db)
    executable = m.v2_amount_out(QTY, 10**24, 10**21, 25) * Decimal(600) / Decimal(10**18) / Decimal(1000)
    assert Decimal(basis["quote_price_usd"]) == pc.exit_price(Chain.BSC, Decimal("0.6"), tax_bps=500)
    assert Decimal(basis["executable_price_usd"]) == pc.exit_price(Chain.BSC, executable, tax_bps=500)
    assert basis["impact_basis"] == "executable_quote"
    tag = pc.read_basis(tmp_db, "pos_bx")
    assert tag["exits"][-1]["fill"] == "v2_formula" and tag["exits"][-1]["tax_bps"] == 500 and tag["honest"]


def test_without_a_recorded_tax_the_dossier_sell_tax_is_used(tmp_db, monkeypatch, risk_file):  # noqa: F811
    from kaiba.core.schemas import Measure, Receipt
    from tests.test_paper import write_dossier

    position, rpc = bsc_position(tmp_db, monkeypatch)
    dossier = write_dossier(tmp_db, token=BSC_TOKEN, chain=Chain.BSC)
    taxed = dossier.model_copy(update={"sell_tax_bps": Measure(value=Decimal(200), basis=EvidenceBasis.PROVIDER_REPORTED,
                                                               receipt=Receipt(provider="f", endpoint="d"),
                                                               freshness_budget_s=86_400)})
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE address=?", (taxed.model_dump_json(), BSC_TOKEN))
    assert sell(tmp_db, position, mark("0.6")).ok
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == pc.exit_price(Chain.BSC, Decimal("0.6"), tax_bps=200)


def test_when_the_pool_cannot_be_read_the_exit_waits_then_fills_at_the_mark_and_stays_counted(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """2026-10-23 (step 3 review, blocking #5): a read that learned nothing (the send failed, the
    pacer refused) is retried on the next ticks before the exit falls back to the mark, and the row
    stays on the scoreboard (its entry was pool-priced), flagged as an exit-stage row."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    rpc.table.clear()  # every read answers nothing
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    sub = wd.DefaultExitSubmitter(tmp_db, FakeSource({}))  # ONE submitter, as the watchdog holds one
    assert wd.PAPER_POOL_RETRIES >= 1
    quote = mark("0.6", source="prices:dexscreener")
    for attempt in range(wd.PAPER_POOL_RETRIES):
        out = sub._paper(position, Decimal(100), quote, "stop_loss")
        assert not out.ok and out.state is None and "waits for a pool read" in out.detail, (attempt, out)
        assert fetch_all(tmp_db, "SELECT order_id FROM orders WHERE side='sell'") == [], "nothing filled yet"
    out = sub._paper(position, Decimal(100), quote, "stop_loss")
    assert out.ok, "an exit is never refused for want of a pool read, only delayed"
    assert last_sell_basis(tmp_db)["impact_basis"] == "constant_product_approximation"
    tag = pc.read_basis(tmp_db, "pos_bx")
    assert [e["fill"] for e in tag["exits"]] == ["retry"] * wd.PAPER_POOL_RETRIES + ["mark"]
    assert f"after {wd.PAPER_POOL_RETRIES} retries" in tag["exits"][-1]["note"]
    assert not tag["honest"] and pc.entry_counts(tag) and pc.exit_degraded(tag)
    assert len(rpc.sends) == wd.PAPER_POOL_RETRIES + 1


def test_a_drained_pair_books_the_exit_at_zero_proceeds_and_stays_counted(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The pool ANSWERED and cannot pay (no reserves): the leg is booked at zero proceeds, not at the
    deciding mark -- the rug case, which used to fill at the mark and leave the scoreboard."""
    position, rpc = bsc_position(tmp_db, monkeypatch, reserves=(0, 0))
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, landing=LANDED)
    out = sell(tmp_db, position, mark("0.6", source="prices:dexscreener"))
    assert out.ok and len(rpc.sends) == 1
    row = fetch_all(tmp_db, "SELECT closed_ms, cost_native, proceeds_native, realized_native FROM positions "
                            "WHERE position_id='pos_bx'")[0]
    assert row["closed_ms"] is not None
    assert int(row["proceeds_native"]) == 0 and int(row["realized_native"]) == -int(row["cost_native"]), row
    tag = pc.read_basis(tmp_db, "pos_bx")
    assert tag["exits"][-1]["fill"] == pc.NO_FILL_BOUND and "v2_reserves_unreadable" in tag["exits"][-1]["note"]
    assert not tag["honest"] and pc.entry_counts(tag) and pc.exit_degraded(tag)


def test_a_v4_quoter_that_reverts_books_zero_proceeds_on_robinhood(tmp_db, monkeypatch, risk_file):  # noqa: F811
    fx, key, ref, _ = rh_ref(0)
    qty = 40_000 * 10**18
    make_position(tmp_db, position_id="pos_rz", token=fx["token"], chain=Chain.ROBINHOOD, qty=qty, entry="0.0002")
    m.remember_ref(ref, tmp_db)
    # the pool's state reads fine; the quoter's item reverted (None): out of liquidity for this size
    rpc = FakeRpc({tuple(m.mark_calls(ref)[0]): fx["slot0"], tuple(m.mark_calls(ref)[1]): fx["liquidity"],
                   (fx["token"], m.SEL_DECIMALS): fx["decimals"]})
    monkeypatch.setattr(m, "transport_for", lambda chain, conn=None, priority=None: rpc)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (ETH_USD, "fixture"))
    pc.record_entry_basis(tmp_db, "pos_rz", Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter", fill="v4_quoter",
                          buy_tax_bps=None, sell_tax_bps=None, tax_in_price=False, landing=LANDED)
    tmp_db.commit()
    position = load_position(tmp_db, "pos_rz")
    quote = mark("0.0002", source="prices:dexscreener", chain=Chain.ROBINHOOD, token=fx["token"], liquidity=None)
    assert sell(tmp_db, position, quote).ok
    row = fetch_all(tmp_db, "SELECT proceeds_native, realized_native, cost_native FROM positions "
                            "WHERE position_id='pos_rz'")[0]
    assert int(row["proceeds_native"]) == 0 and int(row["realized_native"]) == -int(row["cost_native"]), row
    assert pc.read_basis(tmp_db, "pos_rz")["exits"][-1]["fill"] == pc.NO_FILL_BOUND


def test_with_no_bsc_pool_host_the_exit_fills_at_the_mark_at_once(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Not readable by this module at all (the box today): no retry, the mark, labelled."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    monkeypatch.setattr(m, "bsc_pool_rpc_url", lambda: (None, "bsc_pool_rpc_unset"))
    out = sell(tmp_db, position, mark("0.6", source="prices:dexscreener"))
    assert out.ok and rpc.sends == []
    leg = pc.read_basis(tmp_db, "pos_bx")["exits"][-1]
    assert leg["fill"] == "mark" and "bsc_pool_rpc_unset" in leg["note"]


@pytest.mark.parametrize("why,kind", [
    ("quoter_v2_refused", m.EXIT_NO_FILL), ("v4_quoter_refused", m.EXIT_NO_FILL),
    ("pool_cannot_fill_this_size", m.EXIT_NO_FILL), ("fill_priced_to_zero", m.EXIT_NO_FILL),
    ("v2_reserves_unreadable", m.EXIT_NO_FILL),
    ("no_usable_wbnb_pool:v2:0xaa:reserves_empty,v3:0xbb:no_in_range_liquidity", m.EXIT_NO_FILL),
    ("no_usable_wbnb_pool:v3:0xbb:fee_mismatch (cached)", m.EXIT_AT_MARK),
    ("pool_read_failed", m.EXIT_RETRY), ("bsc_pool_discovery_failed (cached)", m.EXIT_RETRY),
    ("bsc_pool_rpc_circuit_open", m.EXIT_RETRY), ("pool_exit_fill_raised:TimeoutError", m.EXIT_RETRY),
    ("paper_native_usd_missing", m.EXIT_AT_MARK),  # the pool was never asked
    ("bsc_pool_rpc_unset", m.EXIT_AT_MARK), ("pool_unknown", m.EXIT_AT_MARK),
    ("quote_asset_not_native:0xusdg", m.EXIT_AT_MARK), ("something_new", m.EXIT_AT_MARK),
])
def test_exit_refusals_are_classified_by_what_the_pool_said(why, kind):
    assert m.exit_refusal_kind(why) == kind


def test_a_robinhood_batch_item_lost_to_anything_but_a_revert_reads_as_unread(monkeypatch):
    """A quoter item lost to a per-item rate limit must not read as "the pool cannot fill"."""
    import kaiba.ingest.robinhood as rh

    answers = iter([
        rh.RpcResult(["0x" + "0" * 63 + "1", None], False, "eth_call: {'code': -32005, 'message': 'rate limit'}"),
        rh.RpcResult(["0x" + "0" * 63 + "1", None], False, "eth_call: {'code': 3, 'message': 'execution reverted'}"),
    ])
    monkeypatch.setattr(rh, "rpc_batch", lambda calls, **kw: next(answers))
    send = m.robinhood_transport(priority=m.Priority.EXIT)
    calls = [(m.RH_WETH, m.SEL_DECIMALS), (m.RH_V4_QUOTER, m.SEL_QUOTE_V4)]
    assert send(calls) == [None, None], "not answered: retry, never a no-fill"
    assert send(calls) == ["0x" + "0" * 63 + "1", None], "a revert IS the chain's answer"


def test_a_curve_decided_exit_sells_on_the_curve_without_a_pool_read(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """2026-10-23 (hA_): a mark that does not carry the Flap curve's state (this one) still sells without
    a pool read, at the curve MARK -- which the broker fills on its constant-product approximation, so
    the leg is NOT honest and the counted row is an exit-stage row. It used to read honest (MEASURED on
    the box: both bsc rows the scoreboard counted). The curve sale at our size is
    tests/test_paper_broker_basis.py."""
    position, rpc = bsc_position(tmp_db, monkeypatch)
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="curve:flap", fill="curve_quote", buy_tax_bps=100,
                          sell_tax_bps=100, tax_in_price=True, landing=LANDED)
    assert sell(tmp_db, position, mark("0.6", source="evm-venue:flap")).ok
    assert rpc.sends == []
    assert last_sell_basis(tmp_db)["impact_basis"] == "constant_product_approximation"
    tag = pc.read_basis(tmp_db, "pos_bx")
    leg = tag["exits"][-1]
    assert leg["fill"] == "curve_mark" and leg["tax_bps"] == 100 and leg["broker"]["basis"] == "dex"
    assert not leg["honest"] and not tag["honest"] and pc.entry_counts(tag) and pc.exit_degraded(tag)


def test_a_robinhood_exit_fills_from_v4quoter_and_does_not_add_the_flat_hook_fee(tmp_db, monkeypatch, risk_file):  # noqa: F811
    fx, key, ref, _ = rh_ref(0)
    qty = 40_000 * 10**18
    make_position(tmp_db, position_id="pos_rx", token=fx["token"], chain=Chain.ROBINHOOD, qty=qty, entry="0.0002")
    m.remember_ref(ref, tmp_db)
    rpc = FakeRpc({tuple(m.mark_calls(ref)[0]): fx["slot0"], tuple(m.mark_calls(ref)[1]): fx["liquidity"],
                   # from the ABI, not the function under test: a sell pays the TOKEN in
                   (m.RH_V4_QUOTER, v4_quote_calldata(key, pays_in=ref.token, amount_in=qty)):
                       fx["quote_sell_40k_tokens"],
                   (fx["token"], m.SEL_DECIMALS): fx["decimals"]})
    monkeypatch.setattr(m, "transport_for", lambda chain, conn=None, priority=None: rpc)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (ETH_USD, "fixture"))
    tmp_db.commit()
    position = load_position(tmp_db, "pos_rx")
    quote = mark("0.0002", source="prices:dexscreener", chain=Chain.ROBINHOOD, token=fx["token"], liquidity=None)
    assert sell(tmp_db, position, quote).ok
    basis = last_sell_basis(tmp_db)
    assert Decimal(basis["quote_price_usd"]) == pc.exit_price(Chain.ROBINHOOD, Decimal("0.0002"), venue_fee_in_price=True)
    assert basis["impact_basis"] == "executable_quote"
    sold = Decimal(int(fx["quote_sell_40k_tokens"][2:66], 16)) / Decimal(10**18) * ETH_USD / Decimal(40_000)
    assert Decimal(basis["executable_price_usd"]) == pc.exit_price(Chain.ROBINHOOD, sold, venue_fee_in_price=True)
    assert pc.read_basis(tmp_db, "pos_rx")["exits"][-1]["fill"] == "v4_quoter"


def test_a_sol_exit_is_exactly_what_it_was(tmp_db, monkeypatch, risk_file):  # noqa: F811
    def boom(*a, **k):
        raise AssertionError("sol must never reach the evm pool fill")

    monkeypatch.setattr(m, "exit_fill", boom)
    make_position(tmp_db, position_id="pos_sx")
    position = load_position(tmp_db, "pos_sx")
    quote = wd.PriceQuote(price_usd=Decimal("1.0"), liquidity_usd=Decimal("1000000"), basis=EvidenceBasis.PROVIDER_REPORTED,
                          observed_ms=now_ms(), source="fake", chain=Chain.SOL, token=position.token)
    assert sell(tmp_db, position, quote).ok
    assert pc.read_basis(tmp_db, "pos_sx") is None


# ----------------------------------------------------------------------------- 2026-10-23 follow-up


def test_a_bsc_exit_charges_the_higher_of_the_entry_and_the_current_dossier_sell_tax(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The token raised its sell tax after our entry: the dossier now says 900, the entry recorded 200.
    The exit used to prefer the entry's figure and never read the fresher one -- optimistic."""
    from kaiba.core.schemas import Measure, Receipt
    from tests.test_paper import write_dossier

    position, rpc = bsc_position(tmp_db, monkeypatch)
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=200, tax_in_price=False, landing=LANDED)
    dossier = write_dossier(tmp_db, token=BSC_TOKEN, chain=Chain.BSC)
    raised = dossier.model_copy(update={"sell_tax_bps": Measure(value=Decimal(900), basis=EvidenceBasis.PROVIDER_REPORTED,
                                                                receipt=Receipt(provider="f", endpoint="d"),
                                                                freshness_budget_s=86_400)})
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE address=?", (raised.model_dump_json(), BSC_TOKEN))
    assert sell(tmp_db, position, mark("0.6")).ok
    # independent: router 1% + 9% tax off the proceeds
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == Decimal("0.6") * (1 - Decimal(100 + 900) / 10_000)
    assert pc.read_basis(tmp_db, "pos_bx")["exits"][-1]["tax_bps"] == 900


def test_a_lower_current_dossier_tax_never_undercuts_the_tax_recorded_at_the_entry(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    from kaiba.core.schemas import Measure, Receipt
    from tests.test_paper import write_dossier

    position, rpc = bsc_position(tmp_db, monkeypatch)
    pc.record_entry_basis(tmp_db, "pos_bx", Chain.BSC, source="pool:pancake-v2:v2_formula", fill="v2_formula",
                          buy_tax_bps=0, sell_tax_bps=600, tax_in_price=False, landing=LANDED)
    dossier = write_dossier(tmp_db, token=BSC_TOKEN, chain=Chain.BSC)
    lowered = dossier.model_copy(update={"sell_tax_bps": Measure(value=Decimal(0), basis=EvidenceBasis.PROVIDER_REPORTED,
                                                                 receipt=Receipt(provider="f", endpoint="d"),
                                                                 freshness_budget_s=86_400)})
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE address=?", (lowered.model_dump_json(), BSC_TOKEN))
    assert sell(tmp_db, position, mark("0.6")).ok
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == Decimal("0.6") * (1 - Decimal(100 + 600) / 10_000)


RH_CURVE_TOKEN = "0x" + "7c" * 20


def test_a_robinhood_curve_mark_exit_pays_the_250_bps_venue_stand_in(tmp_db, monkeypatch, risk_file):  # noqa: F811
    """A Pons-curve-decided exit sells on the curve the mark read (``curve_mark``): no fee is in that
    price, so at least the 250 bps stand-in is charged on top of the router's 1%. Mutant pinned:
    ``venue_fee_in_price=True`` on every exit must fail here. 2026-10-23: the curve's own take
    (``feeBps 100 + creatorTaxBps 0`` here, recorded at the entry) is charged when higher; a 0 bps token
    keeps the 250 floor (tests/test_paper_pons_curve_take.py has the 500 bps token and the unread case)."""
    make_position(tmp_db, position_id="pos_rc", token=RH_CURVE_TOKEN, chain=Chain.ROBINHOOD, qty=QTY, entry="0.0002")
    pc.record_entry_basis(tmp_db, "pos_rc", Chain.ROBINHOOD, source="curve:pons", fill="curve_mark", buy_tax_bps=0,
                          sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS, curve_take_bps=100, landing=LANDED)
    tmp_db.commit()

    def no_pool(*a, **k):
        raise AssertionError("a curve-decided exit must not read a pool")

    monkeypatch.setattr(m, "exit_fill", no_pool)
    position = load_position(tmp_db, "pos_rc")
    quote = mark("0.0002", source="evm-venue:pons", chain=Chain.ROBINHOOD, token=RH_CURVE_TOKEN, liquidity="5000")
    assert sell(tmp_db, position, quote).ok
    basis = last_sell_basis(tmp_db)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 - Decimal(100 + 250) / 10_000)
    tag = pc.read_basis(tmp_db, "pos_rc")
    # 2026-10-23 (hA_): the broker filled it on its approximation (no executable): not honest.
    assert tag["exits"][-1]["fill"] == "curve_mark" and basis["impact_basis"] == "constant_product_approximation"
    assert not tag["exits"][-1]["honest"] and not tag["honest"]


def test_a_measured_robinhood_sell_tax_is_charged_on_the_pool_exit(tmp_db, monkeypatch, risk_file):  # noqa: F811
    fx, key, ref, _ = rh_ref(0)
    qty = 40_000 * 10**18
    make_position(tmp_db, position_id="pos_rm", token=fx["token"], chain=Chain.ROBINHOOD, qty=qty, entry="0.0002")
    m.remember_ref(ref, tmp_db)
    rpc = FakeRpc({tuple(m.mark_calls(ref)[0]): fx["slot0"], tuple(m.mark_calls(ref)[1]): fx["liquidity"],
                   (m.RH_V4_QUOTER, v4_quote_calldata(key, pays_in=ref.token, amount_in=qty)):
                       fx["quote_sell_40k_tokens"],
                   (fx["token"], m.SEL_DECIMALS): fx["decimals"]})
    monkeypatch.setattr(m, "transport_for", lambda chain, conn=None, priority=None: rpc)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (ETH_USD, "fixture"))
    pc.record_entry_basis(tmp_db, "pos_rm", Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter", fill="v4_quoter",
                          buy_tax_bps=500, sell_tax_bps=700, tax_in_price=False,
                          transfer_tax=pc.TRANSFER_TAX_MEASURED, landing=LANDED)
    tmp_db.commit()
    position = load_position(tmp_db, "pos_rm")
    quote = mark("0.0002", source="prices:dexscreener", chain=Chain.ROBINHOOD, token=fx["token"], liquidity=None)
    assert sell(tmp_db, position, quote).ok
    # quoter fill: router 1% + the measured 7%, no 250 bps stand-in
    assert Decimal(last_sell_basis(tmp_db)["quote_price_usd"]) == Decimal("0.0002") * (1 - Decimal(100 + 700) / 10_000)
    assert pc.read_basis(tmp_db, "pos_rm")["exits"][-1]["tax_bps"] == 700
