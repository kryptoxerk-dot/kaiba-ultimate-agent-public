"""Robinhood and bsc paper entries price from the token's own pool or curve (design 2026-10-21 3b/3c).

MEASURED before (box, 2026-10-21): 89 of 89 robinhood and 45 of 45 bsc post-cut paper entries were
dossier-priced, a median +5.0% / +4.8% above the tape. The dossier stays as the fallback and the
trade is then tagged not-honest, so nothing that worked stops working.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, jdump, jload
from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import engine
from kaiba.execution import evm_pool_price as m
from kaiba.execution import paper_costs as pc
from kaiba.execution.paper import fill_basis
from tests.test_evm_pool_price import ETH_USD, FX, FakeRpc, rh_ref, v4_quote_calldata
from tests.test_paper import make_decision, write_dossier

BSC_TOKEN = "0x" + "5b" * 20


@pytest.fixture(autouse=True)
def _clean():
    m.clear_caches()
    engine._POOL_REFUSALS.clear()
    yield
    m.clear_caches()


def rh_setup(tmp_db, monkeypatch, *, rpc_default=None, broken=False):
    from kaiba.execution import evm_price as ep

    fx, key, ref, _ = rh_ref(0)
    tmp_db.execute("INSERT INTO events (ts_ms, kind, payload, dedupe_key) VALUES (?,?,?,?)",
                   (now_ms(), "token.migrated", jdump({"position_id": fx["lp_token_id"]}),
                    f"token.migrated:robinhood:{fx['token']}"))
    table = {} if broken else {
        (m.RH_POSITION_MANAGER, m.SEL_POOL_AND_POSITION + m._word(fx["lp_token_id"])): fx["pool_and_position"],
        tuple(m.mark_calls(ref)[0]): fx["slot0"], tuple(m.mark_calls(ref)[1]): fx["liquidity"],
        # keyed by calldata built from the ABI, not by the function under test: a buy pays ETH in
        (m.RH_V4_QUOTER, v4_quote_calldata(key, pays_in=None, amount_in=10**16)): fx["quote_buy_0_01_eth"],
        (fx["token"], m.SEL_DECIMALS): fx["decimals"],
    }
    rpc = FakeRpc(table, default=rpc_default)
    monkeypatch.setattr(m, "robinhood_transport", lambda conn=None, priority=None: rpc)
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (None, "pons_curve_complete"))
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (ETH_USD, "fixture"))
    monkeypatch.setattr(m, "dex_pair_hint", lambda chain, token: None)
    dossier = write_dossier(tmp_db, token=fx["token"], chain=Chain.ROBINHOOD, price="0.5", liquidity="9999")
    decision = make_decision(chain=Chain.ROBINHOOD, token=fx["token"], size=10**16, ts=now_ms())
    return fx, ref, rpc, dossier, decision


def test_a_robinhood_token_with_a_pool_is_priced_from_the_pool_at_our_size(tmp_db, monkeypatch):
    fx, ref, rpc, dossier, decision = rh_setup(tmp_db, monkeypatch)
    price, liquidity, source, fill = engine.paper_entry_quote(decision, dossier, tmp_db)
    assert source.startswith("pool:"), source
    assert source == "pool:uniswap-v4:v4_quoter"
    assert isinstance(fill, m.PaperEntry) and fill.fill == "v4_quoter" and fill.pool_id == ref.pool
    mark = m.mark_from(ref, [fx["slot0"], fx["liquidity"]], 18)
    assert price == mark.price_native * ETH_USD and price != Decimal("0.5")  # never the dossier's
    assert fill.executable_price_usd > price and liquidity > 0
    assert len(rpc.sends) == 2, "LP key, then one send for mark + quoter + decimals"


def test_a_pool_read_failure_falls_back_to_the_dossier_and_says_why(tmp_db, monkeypatch):
    fx, ref, rpc, dossier, decision = rh_setup(tmp_db, monkeypatch, broken=True)
    price, liquidity, source, fill = engine.paper_entry_quote(decision, dossier, tmp_db)
    assert (price, liquidity, source, fill) == (Decimal("0.5"), Decimal("9999"), "dossier", None)
    assert "pons_lp_key_unreadable" in engine._POOL_REFUSALS[decision.decision_id]


def test_a_token_still_on_its_curve_is_priced_from_the_curve_not_a_pool(tmp_db, monkeypatch):
    from kaiba.execution import evm_price as ep

    fx, ref, rpc, dossier, decision = rh_setup(tmp_db, monkeypatch)
    priced = ep.VenuePrice(price_quote_per_token=Decimal("1e-7"), quote_token=None, quote_reserve_base=2 * 10**18,
                           quote_decimals=18, venue="pons", observed_ms=now_ms(), note="pons curve")
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (priced, "ok"))
    price, liquidity, source, fill = engine.paper_entry_quote(decision, dossier, tmp_db)
    assert source == "curve:pons" and fill.fill == "curve_mark" and rpc.sends == []
    assert price == Decimal("1e-7") * ETH_USD and liquidity == 2 * ETH_USD


def test_a_curve_read_that_failed_does_not_move_the_token_onto_a_pool(tmp_db, monkeypatch):
    """A flaky node is not graduation: only an off-curve answer may fall through to the pool."""
    from kaiba.execution import evm_price as ep

    fx, ref, rpc, dossier, decision = rh_setup(tmp_db, monkeypatch)
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (None, "pons_curve_read_failed"))
    assert engine.paper_entry_quote(decision, dossier, tmp_db)[2] == "dossier"
    assert rpc.sends == []


# ----------------------------------------------------------------------------- the bsc entry, through handoff


def bsc_handoff(tmp_db, monkeypatch, *, entry: m.PaperEntry | None, buy_tax=None, sell_tax=None):
    from kaiba.execution import viability

    dossier = write_dossier(tmp_db, token=BSC_TOKEN, chain=Chain.BSC, price="0.0006", liquidity="50000")
    if buy_tax is not None or sell_tax is not None:
        from kaiba.core.schemas import EvidenceBasis, Measure, Receipt

        def measure(v):
            return Measure(value=Decimal(v), basis=EvidenceBasis.PROVIDER_REPORTED,
                           receipt=Receipt(provider="fixture", endpoint="dyor"), freshness_budget_s=86_400) \
                if v is not None else Measure.unknown()
        dossier = dossier.model_copy(update={"buy_tax_bps": measure(buy_tax), "sell_tax_bps": measure(sell_tax)})
        tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain='bsc' AND address=?",
                       (dossier.model_dump_json(), BSC_TOKEN))
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    monkeypatch.setattr(viability, "paper_native_usd", lambda conn, at_ms=None, chains=None: {Chain.BSC: Decimal("600")})
    monkeypatch.setattr(m, "paper_entry", lambda *a, **k: (entry, "ok") if entry is not None else (None, "no pool"))
    decision = make_decision(chain=Chain.BSC, token=BSC_TOKEN, size=13 * 10**16, ts=now_ms())
    return decision, engine.handoff(decision, None, tmp_db)


#: A landing drift MEASURED and charged (``evm_pool_price.landing_drift``): what a pool / curve entry must carry to
#: count since 2026-10-23 (fd_, ``paper_costs.entry_drift_unmeasured``). Here a measured net flow of zero, so a
#: test whose subject is something else (a tax, the broker's basis, an exit) keeps its numbers. The rule itself
#: is pinned in tests/test_paper_landing_drift.py.
LANDED: dict = {"drift_wei": "0", "drift_basis": "adverse:swaps", "flow_basis": "swaps", "flow_samples": 4,
                "latency_ms": 10_100, "latency_basis": "fixture", "decided_ms": 0}


def flat_tape(conn, chain: Chain, token: str, decided: int) -> None:
    """Four trades in the 30 s before ``decided`` whose net flow is exactly zero: a flow MEASURED on the tape
    (``adverse:swaps``) that moves the curve or pool by nothing, so the fill is the decision's state and counts."""
    for i, (ts_ms, side) in enumerate([(decided - 30_000, "buy"), (decided - 20_000, "sell"),
                                       (decided - 10_000, "buy"), (decided, "sell")]):
        conn.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, program, "
                     "source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (chain.value, f"0xflat{i}", ts_ms, f"0xw{i}", token.lower(), side, "1", str(10**16), "fixture",
                      "fixture"))
    conn.commit()


def pool_entry(**kw) -> m.PaperEntry:
    base = dict(source="pool:pancake-v2:v2_formula", price_usd=Decimal("0.0006"), liquidity_usd=Decimal("60000"),
                executable_price_usd=Decimal("0.000603"), fill="v2_formula", pool_id="0x" + "6" * 40,
                landing=dict(LANDED))
    base.update(kw)
    return m.PaperEntry(**base)


def priced_event(conn) -> dict:
    rows = [jload(r["payload"], {}) for r in fetch_all(conn, "SELECT payload FROM events WHERE payload LIKE "
                                                             "'%paper_entry_priced%'")]
    assert len(rows) == 1, rows
    return rows[0]


def test_a_bsc_pool_entry_pays_its_buy_tax_and_records_the_sell_tax_for_the_exit(tmp_db, monkeypatch):
    decision, order = bsc_handoff(tmp_db, monkeypatch, entry=pool_entry(), buy_tax=300, sell_tax=500)
    assert order is not None and order.state.value == "filled"
    basis = fill_basis(tmp_db, order.order_id)
    # the broker got the pool mark plus router + tax, and our size's executable plus the same costs
    assert Decimal(basis["quote_price_usd"]) == pc.entry_price(Chain.BSC, Decimal("0.0006"), tax_bps=300)
    assert Decimal(basis["executable_price_usd"]) == pc.entry_price(Chain.BSC, Decimal("0.000603"), tax_bps=300)
    assert basis["impact_basis"] == "executable_quote" and basis["impact_bps"] == 50
    # 2026-10-23: the buy tax keeps 3% of the TOKENS, so the mark costs 1.01 / 0.97 of itself
    # (independent arithmetic, not the function under test), never the additive 1.04.
    assert abs(Decimal(basis["quote_price_usd"]) / (Decimal("0.0006") * Decimal("1.01") / Decimal("0.97")) - 1) \
        < Decimal("1e-18")
    event = priced_event(tmp_db)
    assert event["entry_cost_bps"] == 413 and event["honest"] is True and event["fill"] == "v2_formula"
    [position_id] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["sell_tax_bps"] == 500 and tag["entry"]["honest"] is True and tag["honest"] is False


def test_an_unknown_bsc_tax_refuses_the_paper_entry(tmp_db, monkeypatch):
    decision, order = bsc_handoff(tmp_db, monkeypatch, entry=pool_entry(), buy_tax=None, sell_tax=None)
    assert order is None
    assert fetch_all(tmp_db, "SELECT * FROM orders") == []
    refused = [jload(r["payload"], {}) for r in fetch_all(tmp_db, "SELECT payload FROM events")
               if "paper_tax_unknown" in r["payload"]]
    assert refused and refused[0]["reason"] == pc.PAPER_TAX_UNKNOWN


def test_a_flap_fill_that_netted_its_tax_is_not_charged_it_again(tmp_db, monkeypatch):
    flap = pool_entry(source="curve:flap", fill="curve_quote", buy_tax_bps=300, sell_tax_bps=300, tax_in_price=True)
    decision, order = bsc_handoff(tmp_db, monkeypatch, entry=flap, buy_tax=None, sell_tax=None)
    assert order is not None and order.state.value == "filled", "the curve's own record knew the tax"
    basis = fill_basis(tmp_db, order.order_id)
    assert Decimal(basis["quote_price_usd"]) == pc.entry_price(Chain.BSC, Decimal("0.0006"))  # router only
    event = priced_event(tmp_db)
    assert event["entry_cost_bps"] == 100 and event["tax_in_price"] is True and event["buy_tax_bps"] == 0
    [position_id] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    assert pc.read_basis(tmp_db, position_id)["entry"]["sell_tax_bps"] == 300  # charged at the exit


def test_a_dossier_priced_bsc_entry_still_runs_and_is_tagged_not_honest(tmp_db, monkeypatch):
    decision, order = bsc_handoff(tmp_db, monkeypatch, entry=None, buy_tax=0, sell_tax=0)
    assert order is not None and order.state.value == "filled"
    event = priced_event(tmp_db)
    assert event["source"] == "dossier" and event["honest"] is False and event["pool_refusal"] == "no pool"
    [position_id] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    assert pc.read_basis(tmp_db, position_id)["entry"]["honest"] is False
    assert json.dumps(FX)  # fixture module loaded (shared with test_evm_pool_price)


# ----------------------------------------------------------------------------- the Flap curve entry, unmocked
#
# 2026-10-23 (step 3 review, blocking #1): the Flap test above hands the engine a hand-built
# PaperEntry(tax_in_price=True), so it checks only the call-site half. ``_flap_entry`` itself was
# untested: dropping ``flap_buy_net_quote`` (the buy tax AND the 100 bps protocol fee charged ZERO
# times while ``tax_in_price=True`` still blocks the call-site charge) passed every test, on rows
# tagged honest. These run the real ``_flap_entry`` on a synthetic curve -- the r/h/K of the
# 2026-10-05 derivation in ``evm_price.flap_buy_net_quote`` -- with the curve formula re-derived
# here, not taken from evm_price.

FLAP_R, FLAP_H, FLAP_K = Decimal("6.14"), Decimal("107036752"), Decimal("6797205657.28")
FLAP_SOLD = Decimal("100000000")  # 100M tokens sold
FLAP_TOKEN = "0x" + "77" * 20
BNB = Decimal("600")


def _flap_priced(tax_bps: int):
    """A VenuePrice for a Flap token on the synthetic curve, and the independent numbers."""
    from decimal import localcontext

    from kaiba.execution import evm_price as ep

    wad = 10**18
    with localcontext() as ctx:
        ctx.prec = 60
        supply = FLAP_K / FLAP_R - FLAP_H  # the curve identity S = K/r - h
        x = supply - FLAP_SOLD
        reserve = FLAP_K * (1 / (x + FLAP_H) - 1 / (supply + FLAP_H))  # quote raised, whole BNB
        spot = FLAP_K / (x + FLAP_H) ** 2  # BNB per token
    curve = ep.FlapCurve(
        r_scaled=int(FLAP_R * wad), h_scaled=int(FLAP_H * wad), k_scaled=int(FLAP_K * wad),
        total_supply_atoms=int(supply * wad), tokens_sold_atoms=int(FLAP_SOLD * wad),
        graduation_tokens_atoms=int(Decimal(800_000_000) * wad), quote_reserve_base=int(reserve * wad),
        token_decimals=18, quote_decimals=18, buy_tax_bps=tax_bps, sell_tax_bps=tax_bps)
    priced = ep.VenuePrice(price_quote_per_token=spot, quote_token=None, quote_reserve_base=int(reserve * wad),
                           quote_decimals=18, venue="flap", observed_ms=now_ms(), note="synthetic flap curve",
                           curve=curve)
    return priced, x, reserve, spot


def _independent_flap_exec_usd(size_bnb: Decimal, tax_bps: int, x: Decimal, reserve: Decimal) -> Decimal:
    """USD per whole token our buy pays: the protocol fee (100 bps) and the buy tax come off the
    INPUT, and the rest buys on the constant product (r + reserve) * (x + h) = K."""
    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = 60
        net = size_bnb * (1 - Decimal(100 + tax_bps) / 10_000)
        tokens = (x + FLAP_H) - FLAP_K / ((FLAP_R + reserve) + net)
        return size_bnb * BNB / tokens


@pytest.mark.parametrize("tax_bps", [0, 500])
def test_the_flap_entry_nets_the_protocol_fee_and_the_buy_tax_exactly_once(monkeypatch, tax_bps):
    from kaiba.execution import evm_price as ep

    priced, x, reserve, spot = _flap_priced(tax_bps)
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: (priced, "ok"))
    entry, why = m._flap_entry(FLAP_TOKEN, 10**17, None, BNB, None)
    assert entry is not None, why
    expected = _independent_flap_exec_usd(Decimal("0.1"), tax_bps, x, reserve)
    assert abs(entry.executable_price_usd / expected - 1) < Decimal("1e-12"), (entry.executable_price_usd, expected)
    assert entry.price_usd == spot * BNB and entry.source == "curve:flap" and entry.fill == "curve_quote"
    # ... and because the fill already netted the tax, the engine charges it 0 more times:
    assert entry.tax_in_price is True and entry.buy_tax_bps == tax_bps
    assert pc.leg_tax_bps(Chain.BSC, entry.buy_tax_bps, in_price=entry.tax_in_price) == (0, None)


def test_a_real_flap_entry_through_handoff_pays_the_router_fee_and_no_second_tax(tmp_db, monkeypatch):
    """End to end, with nothing between the curve and the broker mocked. The dossier says 700 bps
    each way and the curve's own record 500: a fill that forgot it netted the tax would be charged
    the dossier's 700 again here (or refused, were the dossier silent)."""
    from kaiba.execution import evm_price as ep
    from kaiba.execution import viability

    tax = 500
    priced, x, reserve, spot = _flap_priced(tax)
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: (priced, "ok"))
    monkeypatch.setattr(viability, "paper_native_usd", lambda conn, at_ms=None, chains=None: {Chain.BSC: BNB})
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    from kaiba.core.schemas import EvidenceBasis, Measure, Receipt

    def measure(v):
        return Measure(value=Decimal(v), basis=EvidenceBasis.PROVIDER_REPORTED,
                       receipt=Receipt(provider="fixture", endpoint="dyor"), freshness_budget_s=86_400)

    dossier = write_dossier(tmp_db, token=FLAP_TOKEN, chain=Chain.BSC, price="0.0006", liquidity="50000")
    dossier = dossier.model_copy(update={"buy_tax_bps": measure(700), "sell_tax_bps": measure(700)})
    tmp_db.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain='bsc' AND address=?",
                   (dossier.model_dump_json(), FLAP_TOKEN))
    decided = now_ms()
    flat_tape(tmp_db, Chain.BSC, FLAP_TOKEN, decided)  # fd_: a measured flow of zero, so the entry counts
    decision = make_decision(chain=Chain.BSC, token=FLAP_TOKEN, size=10**17, ts=decided)
    order = engine.handoff(decision, None, tmp_db)
    assert order is not None and order.state.value == "filled"
    basis = fill_basis(tmp_db, order.order_id)
    expected = pc.entry_price(Chain.BSC, _independent_flap_exec_usd(Decimal("0.1"), tax, x, reserve))
    assert abs(Decimal(basis["executable_price_usd"]) / expected - 1) < Decimal("1e-12"), (basis, expected)
    assert Decimal(basis["quote_price_usd"]) == pc.entry_price(Chain.BSC, spot * BNB)  # router fee only
    event = priced_event(tmp_db)
    assert event["source"] == "curve:flap" and event["entry_cost_bps"] == 100 and event["buy_tax_bps"] == 0
    [position_id] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, position_id)["entry"]
    assert tag["tax_in_price"] is True and tag["buy_tax_bps"] == 0 and tag["sell_tax_bps"] == tax and tag["honest"]


# ----------------------------------------------------------------------------- robinhood, through handoff
#
# 2026-10-23 (step 3 follow-up). A robinhood POOL fill (QuoterV2 / V4Quoter / v2 formula) carries the
# pool's fee and its hook's cut but never runs the token's transfer: robinhood charges no token tax
# (dossiers say 0 on 836 of 836) and, on such a fill, not the 250 bps stand-in either. Right for a
# Pons launch (its fee IS the hook); optimistic for any other launchpad's fee-on-transfer token. And
# the Pons CURVE path (a curve MARK, no fee in it) must keep the 250 bps: neither had a handoff test.

RH_TOKEN = "0x" + "6a" * 20
ETH = Decimal("2500")


def rh_handoff(tmp_db, monkeypatch, *, entry: m.PaperEntry):
    from kaiba.execution import viability

    write_dossier(tmp_db, token=RH_TOKEN, chain=Chain.ROBINHOOD, price="0.0002", liquidity="50000")
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.ROBINHOOD: ETH})
    monkeypatch.setattr(m, "paper_entry", lambda *a, **k: (entry, "ok"))
    decision = make_decision(chain=Chain.ROBINHOOD, token=RH_TOKEN, size=10**16, ts=now_ms())
    order = engine.handoff(decision, None, tmp_db)
    assert order is not None and order.state.value == "filled"
    [position_id] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    return order, position_id


def rh_pool_entry(found_by: str = "pool_hint") -> m.PaperEntry:
    return m.PaperEntry(source="pool:uniswap-v3:quoter_v2", price_usd=Decimal("0.0002"),
                        liquidity_usd=Decimal("80000"), executable_price_usd=Decimal("0.000201"), fill="quoter_v2",
                        pool_id="0x" + "6b" * 20, found_by=found_by, landing=dict(LANDED))


def _launchpad(conn, launchpad: str) -> None:
    conn.execute("INSERT OR REPLACE INTO tokens (chain, address, launchpad, first_seen_ms) VALUES (?,?,?,?)",
                 ("robinhood", RH_TOKEN, launchpad, now_ms()))


@pytest.mark.parametrize("launchpad", [None, "zora_like"])
def test_a_non_pons_robinhood_pool_fill_runs_but_is_tagged_not_honest(tmp_db, monkeypatch, launchpad):
    if launchpad:
        _launchpad(tmp_db, launchpad)
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=rh_pool_entry())
    event = priced_event(tmp_db)
    assert event["transfer_tax"] == pc.TRANSFER_TAX_UNMEASURED and event["honest"] is False
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["transfer_tax"] == pc.TRANSFER_TAX_UNMEASURED and not pc.entry_counts(tag)


@pytest.mark.parametrize("how", ["launchpad", "lp_nft"])
def test_a_pons_launch_pool_fill_counts_because_its_fee_is_the_hook(tmp_db, monkeypatch, how):
    if how == "launchpad":
        _launchpad(tmp_db, "pons_v2")
    entry = rh_pool_entry(found_by="pons_lp_nft" if how == "lp_nft" else "pool_hint")
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=entry)
    basis = fill_basis(tmp_db, order.order_id)
    # a quoter fill: router 1% only, no 250 bps stand-in (the hook fee is in the quote)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * Decimal("1.01")
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["transfer_tax"] == pc.TRANSFER_TAX_PONS and pc.entry_counts(tag)
    assert priced_event(tmp_db)["honest"] is True


def test_a_measured_robinhood_tax_is_charged_on_the_buy_recorded_for_the_sell_and_counts(tmp_db, monkeypatch):
    tmp_db.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                   (f"transfer_tax:robinhood:{RH_TOKEN}", jdump({"buy_bps": 500, "sell_bps": 700,
                                                                 "method": "eth_simulateV1"}), now_ms()))
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=rh_pool_entry())
    basis = fill_basis(tmp_db, order.order_id)
    expected = Decimal("0.0002") * Decimal("1.01") / Decimal("0.95")  # 5% of the tokens kept
    assert abs(Decimal(basis["quote_price_usd"]) / expected - 1) < Decimal("1e-18")
    tag = pc.read_basis(tmp_db, position_id)["entry"]
    assert tag["transfer_tax"] == pc.TRANSFER_TAX_MEASURED and tag["sell_tax_bps"] == 700 and tag["honest"]
    assert priced_event(tmp_db)["buy_tax_bps"] == 500


def test_a_pons_curve_entry_pays_the_250_bps_venue_stand_in(tmp_db, monkeypatch):
    """A Pons CURVE entry is a curve MARK (``fill="curve_mark"``): no fee is in that price, so the
    flat 250 bps stand-in must be charged on top of the router's 1%. Mutant pinned:
    ``fee_in_price = bool(evm_fill)`` (every evm fill treated as fee-in-price) must fail here.
    2026-10-23: the curve's own take is read with the price (``feeBps 100 + creatorTaxBps 0`` here);
    a token whose take is higher pays it (tests/test_paper_pons_curve_take.py), this one keeps the 250."""
    curve = m.PaperEntry(source="curve:pons", price_usd=Decimal("0.0002"), liquidity_usd=Decimal("5000"),
                         executable_price_usd=None, fill="curve_mark", pool_id="0x" + "6c" * 20,
                         found_by="pons_curve", curve_take_bps=100)
    order, position_id = rh_handoff(tmp_db, monkeypatch, entry=curve)
    basis = fill_basis(tmp_db, order.order_id)
    assert Decimal(basis["quote_price_usd"]) == Decimal("0.0002") * (1 + Decimal(100 + 250) / 10_000)
    event = priced_event(tmp_db)
    assert event["entry_cost_bps"] == 350 and event["fill"] == "curve_mark"
    tag = pc.read_basis(tmp_db, position_id)
    assert tag["entry"]["transfer_tax"] == pc.TRANSFER_TAX_PONS
    # 2026-10-23 (hA_): no executable at our size is handed in, so the broker fills it on its own
    # constant-product approximation against the curve's reserve -- recorded, and NOT counted.
    assert basis["impact_basis"] == "constant_product_approximation"
    assert event["honest"] is False and event["broker_basis"]["basis"] == "dex" and not pc.entry_counts(tag)
