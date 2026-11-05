"""Every robinhood / bsc / eth / base paper ENTRY charges Kaiba's own landing drift, or does not count (fd_, 2026-10-23).

fS_ (review of hB_) found that a Pons curve entry with an UNMEASURED landing drift -- ``curve_price.drift_for``
returns 0 when the token's flow is ``unavailable`` (fewer than ``MIN_FLOW_SAMPLES`` swaps in
``DEFAULT_FLOW_WINDOW_S``) -- was booked at the decision's curve and tagged honest, and made it run but not count.
The same defect stayed everywhere else: ``evm_pool_price._flap_entry`` (bsc Flap curve, ``curve_quote``, since
570c5e6) and every pool entry (``entry_fill``: ``v2_formula`` / ``quoter_v2`` / ``v4_quoter``) filled at the
DECISION's state with no drift at all, tagged honest. A live buy lands seconds later (MEASURED decision -> block
median: robinhood 14.6 s, n=19; bsc 10.1 s, n=64). MEASURED on the box (read-only, 2026-10-23): all 10 pool /
curve executable entry tags there (4 bsc Flap, 6 robinhood V4Quoter) were tagged honest with no drift charged, and
not one of their tokens had a single swap on the tape in the 120 s before the decision.

The rule, now one rule for every entry (``paper_costs.entry_drift_unmeasured``): the token's measured flow is
charged against us over the chain's decision -> landing delay (``evm_pool_price.landing_drift``) as a buy that
lands on the curve or pool BEFORE ours; a flow the tape cannot measure is still filled (at the decision's state)
and still runs, but neither the writer nor the reader counts it.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, now_ms
from kaiba.execution import engine
from kaiba.execution import evm_pool_price as m
from kaiba.execution import evm_price as ep
from kaiba.execution import paper_costs as pc
from kaiba.execution.paper import fill_basis
from tests.test_evm_pool_price import FakeRpc, v4_quote_calldata
from tests.test_paper import make_decision, write_dossier
from tests.test_paper_entry_quote_evm import (
    BNB,
    ETH,
    FLAP_K,
    FLAP_R,
    FLAP_TOKEN,
    LANDED,
    _flap_priced,
    priced_event,
    rh_setup,
)

AT_SIZE = {"basis": "router", "impact_basis": "executable_quote"}


@pytest.fixture(autouse=True)
def _clean():
    m.clear_caches()
    engine._POOL_REFUSALS.clear()
    yield
    m.clear_caches()


def tape(conn, chain: Chain, token: str, rows: list[tuple[int, str, int]]) -> None:
    for i, (ts_ms, side, wei) in enumerate(rows):
        conn.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, program, "
                     "source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (chain.value, f"0xld{chain.value}{i}", ts_ms, f"0xw{i}", token.lower(), side, "1", str(wei),
                      "fixture", "fixture"))
    conn.commit()


def three_buys(conn, chain: Chain, token: str, decided: int) -> int:
    """Three buys in the 20 s before ``decided``, 0.04 native net: 0.002 native/s. Returns the net wei."""
    tape(conn, chain, token, [(decided - 20_000, "buy", 10**16), (decided - 10_000, "buy", 2 * 10**16),
                              (decided, "buy", 10**16)])
    return 4 * 10**16


def two_buys(conn, chain: Chain, token: str, decided: int, wei: int) -> None:
    """Under ``curve_price.MIN_FLOW_SAMPLES``: the flow is UNAVAILABLE, whatever these two trades moved."""
    tape(conn, chain, token, [(decided - 2_000, "buy", wei), (decided, "buy", wei)])


# ----------------------------------------------------------------------------- the latency, per chain


def test_the_landing_delay_is_the_measured_one_per_chain_and_derived_only_where_nothing_was_measured():
    assert m.ENTRY_LATENCY_MS == {Chain.ROBINHOOD: 14_600, Chain.BSC: 10_100, Chain.ETH: 26_600, Chain.BASE: 16_600}
    assert m.PONS_ENTRY_LATENCY_MS == m.ENTRY_LATENCY_MS[Chain.ROBINHOOD]
    assert m.ENTRY_LATENCY_BASIS[Chain.BSC].startswith("measured:") and m.ENTRY_LATENCY_BASIS[Chain.ETH].startswith("derived:")
    assert set(m.ENTRY_LATENCY_MS) == set(pc.BASIS_CHAINS), "every chain the tag judges has a delay"


def test_landing_drift_is_the_measured_flow_over_the_delay_and_unavailable_is_zero_and_labelled(tmp_db):
    decided = now_ms()
    net = three_buys(tmp_db, Chain.BSC, FLAP_TOKEN, decided)
    drift, landing = m.landing_drift(Chain.BSC, FLAP_TOKEN, tmp_db, decided)
    assert drift == net * 10_100 // 20_000 and landing["drift_basis"] == "adverse:swaps"
    assert landing["latency_ms"] == 10_100 and landing["flow_samples"] == 3 and landing["decided_ms"] == decided
    assert set(landing) == set(m.LANDING_FIELDS) and pc.landing_drift_charged(landing)
    # a net SELL flow is charged against a buyer too (ADVERSE: the magnitude)
    other = "0x" + "78" * 20
    tape(tmp_db, Chain.BSC, other, [(decided - 20_000, "sell", 10**16), (decided - 10_000, "sell", 2 * 10**16),
                                    (decided, "sell", 10**16)])
    assert m.landing_drift(Chain.BSC, other, tmp_db, decided)[0] == drift
    for conn, token in ((None, FLAP_TOKEN), (tmp_db, "0x" + "79" * 20)):
        none, unmeasured = m.landing_drift(Chain.BSC, token, conn, decided)
        assert none == 0 and unmeasured["drift_basis"].startswith("unavailable:")
        assert not pc.landing_drift_charged(unmeasured)


# ----------------------------------------------------------------------------- the bsc Flap curve entry


def _independent_flap_after_drift(size_bnb: Decimal, tax_bps: int, drift_wei: int, reserve: Decimal) -> Decimal:
    """USD per whole token our buy pays on the synthetic curve AFTER ``drift_wei`` of others' quote reached it
    first: ``(x + h)(y + r) = K`` re-derived here, not taken from evm_price. The drift moves ``y`` by all of it
    (the pessimistic reading: none lost to fee or tax), then our net buys on the moved curve."""
    with localcontext() as ctx:
        ctx.prec = 60
        q1 = FLAP_R + reserve + Decimal(drift_wei) / Decimal(10**18)
        net = size_bnb * (1 - Decimal(100 + tax_bps) / 10_000)
        tokens = FLAP_K / q1 - FLAP_K / (q1 + net)
        return size_bnb * BNB / tokens


def _flap_handoff(conn, monkeypatch, *, tax: int, decided: int):
    from kaiba.execution import viability

    priced, x, reserve, spot = _flap_priced(tax)
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: (priced, "ok"))
    monkeypatch.setattr(viability, "paper_native_usd", lambda conn, at_ms=None, chains=None: {Chain.BSC: BNB})
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    mk = Measure(value=Decimal(tax), basis=EvidenceBasis.PROVIDER_REPORTED,
                 receipt=Receipt(provider="fixture", endpoint="dyor"), freshness_budget_s=86_400)
    dossier = write_dossier(conn, token=FLAP_TOKEN, chain=Chain.BSC, price="0.0006", liquidity="50000")
    dossier = dossier.model_copy(update={"buy_tax_bps": mk, "sell_tax_bps": mk})
    conn.execute("UPDATE token_dossiers SET dossier_json=? WHERE chain='bsc' AND address=?",
                 (dossier.model_dump_json(), FLAP_TOKEN))
    order = engine.handoff(make_decision(chain=Chain.BSC, token=FLAP_TOKEN, size=10**17, ts=decided), None, conn)
    assert order is not None and order.state.value == "filled", getattr(order, "error", None)
    [pid] = [r["position_id"] for r in fetch_all(conn, "SELECT position_id FROM positions")]
    return order, pid, reserve


def test_a_flap_entry_with_its_flow_measured_charges_the_drift_before_our_buy_and_counts(tmp_db, monkeypatch):
    """Mutants pinned: ``_flap_entry`` filling at the decision's curve (the drift computed but not applied); the
    engine not passing ``landing`` to the writer's verdict or to the tag."""
    tax, decided = 500, now_ms()
    net = three_buys(tmp_db, Chain.BSC, FLAP_TOKEN, decided)
    order, pid, reserve = _flap_handoff(tmp_db, monkeypatch, tax=tax, decided=decided)
    drift = net * 10_100 // 20_000  # 0.0202 BNB, independent of the code under test
    basis = fill_basis(tmp_db, order.order_id)
    expected = pc.entry_price(Chain.BSC, _independent_flap_after_drift(Decimal("0.1"), tax, drift, reserve))
    assert abs(Decimal(basis["executable_price_usd"]) / expected - 1) < Decimal("1e-12"), (basis, expected)
    at_decision = pc.entry_price(Chain.BSC, _independent_flap_after_drift(Decimal("0.1"), tax, 0, reserve))
    assert Decimal(basis["executable_price_usd"]) > at_decision, "the drift costs us"
    event = priced_event(tmp_db)
    assert event["landing"]["drift_wei"] == str(drift) and event["landing"]["drift_basis"] == "adverse:swaps"
    assert event["landing"]["latency_ms"] == 10_100 and int(event["landing"]["atoms_at_decision"]) > int(event["landing"]["atoms"])
    assert event["honest"] is True
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["landing"]["drift_basis"] == "adverse:swaps" and tag["entry"]["honest"] is True
    assert pc.entry_counts(tag) and pc.counts_on_scoreboard("bsc", tag)


def test_a_flap_entry_whose_flow_was_not_measured_is_booked_at_the_decisions_curve_and_never_counts(tmp_db, monkeypatch):
    """Two buys that would have moved the curve a long way, measured: under ``MIN_FLOW_SAMPLES``, so the drift is
    ``unavailable`` -- 0 because nothing was measured. The trade still runs, exact at the decision's curve; the
    writer and the reader both refuse to count it. Mutants pinned: the writer's check in ``entry_leg_honest``
    removed; the reader's re-check in ``entry_counts`` removed; the rule's scope back to the robinhood curve
    (``landing_drift_required``)."""
    tax, decided = 500, now_ms()
    two_buys(tmp_db, Chain.BSC, FLAP_TOKEN, decided, 10**18)
    order, pid, reserve = _flap_handoff(tmp_db, monkeypatch, tax=tax, decided=decided)
    basis = fill_basis(tmp_db, order.order_id)
    at_decision = pc.entry_price(Chain.BSC, _independent_flap_after_drift(Decimal("0.1"), tax, 0, reserve))
    assert abs(Decimal(basis["executable_price_usd"]) / at_decision - 1) < Decimal("1e-12")
    assert basis["impact_basis"] == "executable_quote", "the trade itself still runs, filled at our size"
    event = priced_event(tmp_db)
    assert event["landing"]["drift_wei"] == "0" and event["landing"]["drift_basis"].startswith("unavailable:")
    assert event["honest"] is False, "an unmeasured drift is not a zero drift"
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["honest"] is False and not pc.entry_counts(tag) and not pc.counts_on_scoreboard("bsc", tag)
    # the reader alone: the same tag SAYING honest (written before the writer checked) is re-judged
    stale = {**tag, "entry": {**tag["entry"], "honest": True}}
    assert not pc.entry_counts(stale)
    assert pc.entry_counts({**stale, "entry": {**stale["entry"], "landing": dict(LANDED)}})


def test_a_flow_that_would_graduate_the_flap_curve_during_the_delay_is_no_fill(tmp_db, monkeypatch):
    """Mutant pinned: the graduation check removed (the drift folded in as 0 tokens, our buy priced anyway)."""
    priced, x, reserve, spot = _flap_priced(0)
    monkeypatch.setattr(ep, "read_flap", lambda token, rpc: (priced, "ok"))
    decided = now_ms()
    tape(tmp_db, Chain.BSC, FLAP_TOKEN, [(decided - 2_000, "buy", 10 * 10**18), (decided - 1_000, "buy", 10 * 10**18),
                                         (decided, "buy", 10 * 10**18)])
    room = priced.curve.max_size_base_units
    assert 30 * 10**18 * 10_100 // 2_000 > room, "measured, this flow graduates the curve"
    entry, why = m._flap_entry(FLAP_TOKEN, 10**17, tmp_db, BNB, None, at_ms=decided)
    assert entry is None and why == "flap_graduated_during_latency"
    # through paper_entry it is a curve refusal, never a hop onto a pool (the engine then books the dossier)
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (BNB, "fixture"))
    monkeypatch.setattr(m, "entry_fill", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no pool hop")))
    assert m.paper_entry(Chain.BSC, FLAP_TOKEN, 10**17, tmp_db, curve_rpc=object(), at_ms=decided) == (
        None, "curve:flap_graduated_during_latency")


# ----------------------------------------------------------------------------- pool entries


def _v2_mark():
    from tests.test_evm_pool_price import bsc_v2

    _fx, _cands, _dec, [mark], _why = bsc_v2()
    return mark


def test_a_v2_pool_buy_after_the_drift_is_the_pairs_own_formula_in_sequence():
    """The drift's whole input joins the reserves (fee included, as on chain), then our buy. Re-derived here.
    Mutant pinned: the drift's tokens not taken out of the pair."""
    mark = _v2_mark()
    r0, r1 = mark.reserves
    r_token, r_native = (r0, r1) if mark.ref.token_is0 else (r1, r0)
    size, drift = 13 * 10**16, 5 * 10**18
    fill, why = m.fill_from(mark.ref, mark, None, side="buy", amount_in=size, native_usd=BNB, drift_in=drift)
    assert fill is not None, why
    fee_keep = 10_000 - mark.ref.fee
    moved = drift * fee_keep * r_token // (r_native * 10_000 + drift * fee_keep)
    ours = size * fee_keep * (r_token - moved) // ((r_native + drift) * 10_000 + size * fee_keep)
    plain = size * fee_keep * r_token // (r_native * 10_000 + size * fee_keep)
    assert fill.amount_out == ours < plain and fill.landing == {"atoms_at_decision": str(plain), "drift_in_wei": str(drift)}
    # a sell never pays an entry's landing drift
    sell, _ = m.fill_from(mark.ref, mark, None, side="sell", amount_in=10**21, native_usd=BNB, drift_in=drift)
    assert sell.landing is None and sell.amount_out == m.v2_amount_out(10**21, r_token, r_native, mark.ref.fee)
    # a drift that drains the pair leaves no fill at our size
    assert m.fill_from(mark.ref, mark, None, side="buy", amount_in=size, native_usd=BNB,
                       drift_in=10**40) == (None, m.POOL_CANNOT_FILL_AFTER_DRIFT)


def _word2(a: int, b: int = 100_000) -> str:
    return "0x" + format(a, "064x") + format(b, "064x")


def test_a_v3_or_v4_buy_after_the_drift_is_the_quoters_difference_and_a_missing_answer_is_no_fill():
    from tests.test_evm_pool_price import rh_ref

    fx, key, ref, _ = rh_ref(0)
    mark = m.mark_from(ref, [fx["slot0"], fx["liquidity"]], 18)
    first, both = 7 * 10**21, 7 * 10**21 + 3 * 10**21
    fill, why = m.fill_from(ref, mark, fx["quote_buy_0_01_eth"], side="buy", amount_in=10**16, native_usd=ETH,
                            drift_in=10**17, drift_results=[_word2(first), _word2(both)])
    assert fill is not None and fill.amount_out == both - first, why
    assert fill.landing["atoms_at_decision"] == str(m._first(fx["quote_buy_0_01_eth"]))
    for answers in ([None, _word2(both)], [_word2(first), None], [_word2(both), _word2(first)], None):
        assert m.fill_from(ref, mark, fx["quote_buy_0_01_eth"], side="buy", amount_in=10**16, native_usd=ETH,
                           drift_in=10**17, drift_results=answers) == (None, m.POOL_CANNOT_FILL_AFTER_DRIFT)


def _rh_pool_handoff(conn, monkeypatch, *, measured: bool):
    from kaiba.execution import viability

    fx, ref, rpc, dossier, decision = rh_setup(conn, monkeypatch)
    key = ref.v4_key
    first, both = 0, 0
    if measured:
        net = three_buys(conn, Chain.ROBINHOOD, fx["token"], decision.ts_ms)
        drift = net * 14_600 // 20_000
        plain = m._first(fx["quote_buy_0_01_eth"])
        first, both = 3 * plain, 3 * plain + plain * 99 // 100  # 1% fewer tokens for our size after the drift
        # keyed by calldata built from the ABI: a buy of the DRIFT, and of drift + ours, paying ETH in
        rpc.table[(m.RH_V4_QUOTER.lower(), v4_quote_calldata(key, pays_in=None, amount_in=drift).lower())] = _word2(first)
        rpc.table[(m.RH_V4_QUOTER.lower(),
                   v4_quote_calldata(key, pays_in=None, amount_in=drift + 10**16).lower())] = _word2(both)
    monkeypatch.setattr(viability, "paper_native_usd", lambda conn, at_ms=None, chains=None: {Chain.ROBINHOOD: ETH})
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    order = engine.handoff(decision, None, conn)
    assert order is not None and order.state.value == "filled", getattr(order, "error", None)
    [pid] = [r["position_id"] for r in fetch_all(conn, "SELECT position_id FROM positions")]
    return fx, rpc, order, pid, both - first


@pytest.mark.parametrize("measured", [True, False], ids=["flow_measured", "no_tape"])
def test_a_robinhood_pool_entry_fills_after_the_drift_or_runs_uncounted(tmp_db, monkeypatch, measured):
    """A graduated Pons token's V4Quoter entry, end to end. Measured: the quoter asked for the drift and for
    drift + ours in the SAME send, our tokens their difference, and the row counts. Not measured: one quote at our
    size, booked as before, not counted. Mutants pinned: ``entry_fill`` not passing the drift; ``quote_fill``
    asking the wrong amounts; ``fill_from`` taking ``quote(drift + ours)`` whole."""
    fx, rpc, order, pid, ours = _rh_pool_handoff(tmp_db, monkeypatch, measured=measured)
    event = priced_event(tmp_db)
    assert event["fill"] == "v4_quoter" and event["transfer_tax"] == pc.TRANSFER_TAX_PONS
    basis = fill_basis(tmp_db, order.order_id)
    assert basis["impact_basis"] == "executable_quote"
    atoms = ours if measured else m._first(fx["quote_buy_0_01_eth"])
    expected = pc.entry_price(Chain.ROBINHOOD, Decimal(10**16) / Decimal(atoms) * ETH, venue_fee_in_price=True)
    assert abs(Decimal(basis["executable_price_usd"]) / expected - 1) < Decimal("1e-20")
    assert len(rpc.sends[-1]) == (6 if measured else 4), "mark x2 + quoter + decimals (+ the drift's two quotes)"
    assert event["landing"]["drift_basis"].startswith("adverse:" if measured else "unavailable:")
    assert event["landing"]["latency_ms"] == 14_600
    assert event["honest"] is measured
    tag = pc.read_basis(tmp_db, pid)
    assert pc.entry_counts(tag) is measured and pc.counts_on_scoreboard("robinhood", tag) is measured


def test_an_eth_pool_entry_charges_the_derived_delay_on_its_own_pair(tmp_db, monkeypatch):
    """The pool-only chains, through ``entry_fill``: the real eth PEPE/WETH v2 pair, a measured flow on the eth
    tape (Stockereum's rows are the only eth tape the box has), the derived 26.6 s delay."""
    from tests.test_eth_base_paper_foundation import ETH_TOKEN, TICKET, fixture_marks

    chain, r, cands, v4marks, dec, marks, why = fixture_marks("eth_pepe")
    v2 = next(mk for mk in marks if mk.ref.kind == "v2")
    ref = m.PoolRef(Chain.ETH, ETH_TOKEN, v2.ref.pool, "v2", "uniswap-v2", v2.ref.token_is0, m.WETH_ETH, 30)
    m.remember_ref(ref, tmp_db)
    m._DECIMALS[(ref.chain.value, ref.token)] = 18
    monkeypatch.setattr(m, "fill_native_usd", lambda chain, conn, at_ms=None: (Decimal("2390"), "fixture"))
    rpc = FakeRpc({(v2.ref.pool, m.SEL_GET_RESERVES): r["state"][2]})
    decided = now_ms()
    net = three_buys(tmp_db, Chain.ETH, ETH_TOKEN, decided)
    fill, why = m.entry_fill(Chain.ETH, ETH_TOKEN, TICKET, tmp_db, transport=rpc, at_ms=decided)
    assert fill is not None, why
    drift = net * 26_600 // 20_000
    r0, r1 = v2.reserves
    r_token, r_native = (r0, r1) if ref.token_is0 else (r1, r0)
    moved = drift * 9970 * r_token // (r_native * 10_000 + drift * 9970)
    assert fill.amount_out == TICKET * 9970 * (r_token - moved) // ((r_native + drift) * 10_000 + TICKET * 9970)
    assert fill.landing["drift_wei"] == str(drift) and fill.landing["latency_basis"].startswith("derived:")
    assert pc.landing_drift_charged(fill.landing)
    entry, _ = m.paper_entry(Chain.ETH, ETH_TOKEN, TICKET, tmp_db, pool_rpc=rpc, at_ms=decided)
    assert entry.landing == fill.landing, "the PaperEntry carries it to the engine"


# ----------------------------------------------------------------------------- the rule, on the tag


def test_the_rule_covers_every_pool_and_curve_entry_on_every_tagged_chain_and_nothing_else():
    """Mutants pinned: ``landing_drift_charged`` as a deny-list (``not startswith("unavailable")``); the scope
    back to the robinhood curve only."""
    sources = {Chain.BSC: ["curve:flap", "pool:pancake-v2:v2_formula", "pool:pancake-v3:quoter_v2"],
               Chain.ROBINHOOD: ["curve:pons", "pool:uniswap-v4:v4_quoter", "pool:uniswap-v3:quoter_v2"],
               Chain.ETH: ["pool:uniswap-v2:v2_formula", "pool:uniswap-v3:quoter_v2", "pool:uniswap-v4:v4_quoter"],
               Chain.BASE: ["pool:uniswap-v3:quoter_v2"]}
    for chain, srcs in sources.items():
        for source in srcs:
            assert pc.landing_drift_required(chain, source) and pc.landing_drift_required(chain.value, source)
            for basis, counts in [("adverse:swaps", True), ("adverse:curve_snapshots", True),
                                  ("unavailable:no trades and no snapshot pair inside the window", False),
                                  ("none", False), ("observed:swaps", False), (None, False)]:
                landing = {"drift_wei": "0"} if basis is None else {"drift_wei": "0", "drift_basis": basis}
                assert pc.entry_drift_unmeasured(chain, source, landing=landing) is not counts, (chain, source, basis)
            assert pc.entry_drift_unmeasured(chain, source), "no provenance, no count"
    # a dossier entry is judged by its own rule (never counted), and sol is not tagged at all
    assert not pc.landing_drift_required(Chain.BSC, "dossier") and not pc.landing_drift_required(Chain.SOL, "pool:x")
    # a Pons tag written by fS_ / hB_ keeps its verdict from its curve_fill
    assert not pc.entry_drift_unmeasured(Chain.ROBINHOOD, "curve:pons", curve_fill={"drift_basis": "adverse:swaps"})


@pytest.mark.parametrize("chain,source,fill,extra", [
    (Chain.BSC, "curve:flap", "curve_quote", {"tax_in_price": True}),
    (Chain.BSC, "pool:pancake-v2:v2_formula", "v2_formula", {}),
    (Chain.ROBINHOOD, "pool:uniswap-v4:v4_quoter", "v4_quoter", {"transfer_tax": pc.TRANSFER_TAX_PONS}),
    (Chain.ETH, "pool:uniswap-v3:quoter_v2", "quoter_v2", {}),
    (Chain.BASE, "pool:uniswap-v2:v2_formula", "v2_formula", {}),
])
def test_the_writer_and_the_reader_count_an_executable_entry_only_with_its_drift_measured(tmp_db, chain, source,
                                                                                         fill, extra):
    kw = dict(buy_tax_bps=0, sell_tax_bps=0, broker=AT_SIZE, tax_in_price=bool(extra.get("tax_in_price")),
              **{k: v for k, v in extra.items() if k != "tax_in_price"})
    bare = pc.record_entry_basis(tmp_db, f"pos_{chain.value}_bare", chain, source=source, fill=fill, **kw)
    assert bare["entry"]["honest"] is False and not pc.entry_counts(bare), "no landing recorded: not counted"
    unmeasured = pc.record_entry_basis(tmp_db, f"pos_{chain.value}_un", chain, source=source, fill=fill, **kw,
                                       landing={**LANDED, "drift_basis": "unavailable:no flow measured"})
    assert unmeasured["entry"]["honest"] is False and not pc.entry_counts(unmeasured)
    landed = pc.record_entry_basis(tmp_db, f"pos_{chain.value}_ok", chain, source=source, fill=fill, **kw,
                                   landing=dict(LANDED))
    assert landed["entry"]["honest"] is True and pc.entry_counts(landed) and landed["entry"]["landing"] == LANDED
    # a top-up whose earlier leg did not count keeps the row off, whatever the new leg carries
    topped = pc.record_entry_basis(tmp_db, f"pos_{chain.value}_un", chain, source=source, fill=fill, **kw,
                                   landing=dict(LANDED), prior_filled_orders=1)
    assert topped["entry"]["honest"] is True and topped[pc.STICKY_ENTRY_DISHONEST] is True
    assert not pc.entry_counts(topped)


def test_every_pool_or_curve_tag_on_the_box_stops_counting():
    """The box's own tags (kv ``paper_basis:<pos>``, read 2026-10-23, entry shape only): every executable entry
    there was filled at the decision's state. Before this change both shapes counted."""
    box = [
        ("bsc", "curve:flap", "curve_quote", {"tax_in_price": True}),
        ("robinhood", "pool:uniswap-v4:v4_quoter", "v4_quoter", {"transfer_tax": pc.TRANSFER_TAX_PONS}),
    ]
    for chain, source, fill, extra in box:
        entry = {"source": source, "fill": fill, "honest": True, "buy_tax_bps": 0, "sell_tax_bps": 0, **extra}
        tag = {"chain": chain, "entry": entry, "exits": [], "honest": False}
        assert not pc.entry_counts(tag) and not pc.counts_on_scoreboard(chain, tag), (chain, source)
        assert pc.entry_counts({**tag, "entry": {**entry, "landing": dict(LANDED)}}), (chain, source)
