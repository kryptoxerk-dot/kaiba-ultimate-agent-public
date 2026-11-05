"""A robinhood Pons paper leg is FILLED at our size on the curve's own arithmetic (2026-10-23, hB_).

Vanguard review (docs/research/claude-vanguard-snipe-review-20261023.md) section 6 item 2. A Pons curve
paper BUY was handed to the broker with no executable (``evm_pool_price._pons_entry``: ``curve_mark``), so
``PaperBroker`` (pinned) approximated it as a constant product against the curve's REAL quote reserve --
leaving out the 1.68 ETH phantom quote that prices the curve. MEASURED on the box, read-only: the one
robinhood launch-snipe decision of 2026-10-23 (02:59:12 UTC, ``ord_a7220ca6cd8683428adfa356``, 0.02808 ETH
of ``0xcd8e…282c``, curve A below) was refused ``slippage_exceeded impact=2252bps`` by exactly that
approximation; it never "left the curve" -- the source was ``curve:pons`` -- the broker just never filled
on it. The exits had the same defect (``curve_mark`` -> the broker's ``dex`` approximation).

The fixture (``tests/fixtures/robinhood/pons_curve_trades.json``) is the chain's own answer: five curves'
complete tapes from the box (``swaps`` rows ``program='pons-curve'``, the ``CurveBuy`` / ``CurveSell``
words) and the ``eth_simulateV1`` of ``curve.buy`` each curve's ``snipe_observations`` row recorded.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, now_ms
from kaiba.execution import engine
from kaiba.execution import evm_pool_price as m
from kaiba.execution import evm_price as ep
from kaiba.execution import paper_costs as pc
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import (
    DEFAULT_FEE_BPS,
    DEFAULT_SLIPPAGE_FLOOR_BPS,
    DEFAULT_TIP_NATIVE,
    PaperBroker,
    fill_basis,
    load_position,
)
from tests.test_paper import make_decision, write_dossier
from tests.test_watchdog import FakeSource, risk_file  # noqa: F401

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "robinhood" / "pons_curve_trades.json").read_text())
CFG = FIXTURE["launch_config"]
CURVES = {c["label"]: c for c in FIXTURE["curves"]}
#: ETH/USD on the box at the refused decision (native_prices, eth, 2026-10-23 02:58:42 UTC).
ETH_USD = Decimal("2410.019")
#: The refused decision: 0.02808 ETH of curve A's token, 28.8 s after its launch.
REFUSED_SIZE = 28_080_000_000_000_000
AT_SIZE = {"basis": "router", "impact_basis": "executable_quote"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from kaiba.execution import viability

    m.clear_caches()
    engine._POOL_REFUSALS.clear()
    ep._PONS_CURVE.clear()
    monkeypatch.setattr(viability, "paper_native_usd",
                        lambda conn, at_ms=None, chains=None: {Chain.ROBINHOOD: ETH_USD})
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    yield
    ep._PONS_CURVE.clear()
    m.clear_caches()


# ----------------------------------------------------------------------------- the curve, from the chain


def launch_curve(c: dict) -> ep.PonsCurve:
    supply, reserved = int(CFG["launch_supply"]), int(CFG["reserved_tokens"])
    return ep.PonsCurve(quote_reserve=int(CFG["phantom_quote_wei"]), token_reserve=supply, real_quote_reserve=0,
                        sellable_tokens=supply - reserved, graduation_threshold=int(CFG["graduation_threshold_wei"]),
                        fee_bps=c["fee_bps"], creator_tax_bps=c["creator_tax_bps"], launched_at_s=c["launched_at_s"],
                        snipe_tax_start_bps=9_900, snipe_tax_seconds=3, token_decimals=18)


def after_sell(curve: ep.PonsCurve, atoms: int, gross: int) -> ep.PonsCurve:
    return replace(curve, quote_reserve=curve.quote_reserve - gross, token_reserve=curve.token_reserve + atoms,
                   real_quote_reserve=curve.real_quote_reserve - gross, sellable_tokens=curve.sellable_tokens + atoms)


def replay(c: dict) -> tuple[list[ep.PonsCurve], dict[str, int]]:
    """Every trade of ``c`` through the code under test, from the launch state, each one asserted equal to
    the chain's own number. Returns the state after each trade (index 0 = launch) and what was exercised."""
    cur = launch_curve(c)
    states, seen = [cur], {"buys": 0, "sells": 0, "rung": 0, "exempt": 0}
    after_window = c["launched_at_s"] + 3
    for tr in c["trades"]:
        quote, tokens = int(tr["quote"]), int(tr["tokens"])
        if tr["side"] == "buy":
            treatment = tr.get("snipe")
            at_s = after_window if treatment == "exempt" else tr["ts_s"]
            atoms, net, why = cur.tokens_out(quote, at_s=at_s)
            assert atoms == tokens, (c["label"], tr, atoms, why)
            if treatment is not None:
                # exactly one of the two treatments reproduces a buy inside the window: the other does not
                other = cur.tokens_out(quote, at_s=tr["ts_s"] if treatment == "exempt" else after_window)[0]
                assert other != tokens, (c["label"], tr)
                seen[treatment] += 1
            cur = cur.after_buy(net, atoms)
            seen["buys"] += 1
        else:
            out, gross = cur.quote_out(tokens)
            assert out == quote, (c["label"], tr, out)
            cur = after_sell(cur, tokens, gross)
            seen["sells"] += 1
        states.append(cur)
    return states, seen


@pytest.mark.parametrize("label", sorted(CURVES))
def test_every_buy_and_sell_on_five_real_curves_reproduces_to_the_atom(label):
    """The contract's arithmetic, not a model of it. Curves B/C/D/E carry creator taxes of 500/200/100/200 bps;
    C, D and E have buys inside the snipe window (618, 19, and 9,600 = 9,900 capped by fee + creator tax).
    Mutants pinned: the two takes floored together, the snipe tax taken off the net, no 9,900 cap, the takes
    left in the curve (``quote_reserve += quote_in``), a sell fee taken once on the combined rate -- each moves
    at least one trade off the chain's number."""
    states, seen = replay(CURVES[label])
    assert seen["buys"] + seen["sells"] == len(CURVES[label]["trades"])


def test_the_fixture_exercises_every_rule_it_pins():
    total = {"buys": 0, "sells": 0, "rung": 0, "exempt": 0}
    rungs = set()
    for c in CURVES.values():
        _, seen = replay(c)
        for k in total:
            total[k] += seen[k]
        cur = launch_curve(c)
        rungs |= {cur.snipe_tax_bps_at(t["ts_s"]) for t in c["trades"] if t.get("snipe") == "rung"}
    assert total["buys"] >= 40 and total["sells"] >= 40 and total["rung"] >= 20 and total["exempt"] >= 3
    assert rungs == {618, 19, 9_600}, rungs
    assert {c["creator_tax_bps"] for c in CURVES.values()} == {0, 100, 200, 500}


def _combined_floor_tokens(curve: ep.PonsCurve, quote_in: int) -> int:
    """The spelling ``viability.PonsCurveDepth`` sizes with: both takes floored TOGETHER."""
    net = quote_in - quote_in * (curve.fee_bps + curve.creator_tax_bps) // 10_000
    return curve.token_reserve * net // (curve.quote_reserve + net)


def test_the_two_takes_are_floored_separately_and_only_the_chain_could_say_so():
    """A test that passes with the rule removed is not a test: on these creator-tax curves the combined floor
    misses at least one real buy, so the fixture can tell the two spellings apart."""
    misses = 0
    for c in CURVES.values():
        if not c["creator_tax_bps"]:
            continue
        states, _ = replay(c)
        for cur, tr in zip(states, c["trades"], strict=False):
            if tr["side"] == "buy" and tr.get("snipe") is None:
                misses += _combined_floor_tokens(cur, int(tr["quote"])) != int(tr["tokens"])
    assert misses >= 1


@pytest.mark.parametrize("label", sorted(CURVES))
def test_the_eth_simulate_quote_at_our_size_is_the_curves_tokens_out(label):
    """``snipe.paper_entry_rh``'s ``eth_simulateV1`` of ``curve.buy(0.0167 ETH)`` from a stand-in address --
    the venue's own answer at our size -- equals :meth:`PonsCurve.tokens_out` on the replayed state the
    simulation saw (A-D: the state at the decision block; E: four trades later, ``latest`` had moved)."""
    c = CURVES[label]
    states, _ = replay(c)
    sim = c["simulate"]
    curve = states[sim["after_trades"]]
    atoms, _net, why = curve.tokens_out(int(sim["quote_in"]), at_s=c["launched_at_s"] + 60)
    assert sim["basis"] == "eth_simulateV1" and atoms == int(sim["tokens"]), (atoms, why)


# ----------------------------------------------------------------------------- the refused decision


def curve_a_at_decision() -> ep.PonsCurve:
    states, _ = replay(CURVES["A"])
    return states[CURVES["A"]["simulate"]["after_trades"]]  # the dev buy and one outside buy


def test_the_refused_decision_was_the_brokers_approximation_against_the_real_reserve(tmp_db):
    """``impact=2252bps``, reproduced: the broker's constant product of our spend (net of its 60 bps fee)
    against the curve's REAL reserve, 0.09602 ETH -- what ``_pons_entry`` handed it as ``liquidity_usd``."""
    curve = curve_a_at_decision()
    assert curve.real_quote_reserve == 96_021_842_640_619_302
    broker = PaperBroker(tmp_db, native_usd={Chain.ROBINHOOD: ETH_USD})
    spend = REFUSED_SIZE - REFUSED_SIZE * DEFAULT_FEE_BPS[Chain.ROBINHOOD] // 10_000
    impact = broker.impact_bps(broker.to_usd(Chain.ROBINHOOD, spend), broker.to_usd(Chain.ROBINHOOD, curve.real_quote_reserve))
    assert impact == 2252
    # The curve itself: its 1% fee, then the order on 1.776 ETH of virtual quote -- independent arithmetic.
    with localcontext() as ctx:
        ctx.prec = 60
        net = Decimal(REFUSED_SIZE) * Decimal("0.99")
        q, t = Decimal(curve.quote_reserve), Decimal(curve.token_reserve)
        exec_over_spot = (Decimal(REFUSED_SIZE) / (t * net / (q + net))) / (q / t)
    assert Decimal("1.024") < exec_over_spot < Decimal("1.028"), exec_over_spot


def priced_from(curve: ep.PonsCurve, *, creator_tax: int | None = "fixture", token: str | None = None):
    """``evm_price.pons_price`` on ``curve``'s state, the way ``read_pons`` builds it."""
    from kaiba.ingest import robinhood as rh

    reserved = int(CFG["reserved_tokens"])
    state = rh.CurveState(curve=CURVES["A"]["curve"], token=token or CURVES["A"]["token"],
                          quote_reserve=curve.quote_reserve, real_quote_reserve=curve.real_quote_reserve,
                          sellable_tokens=curve.token_reserve - reserved, reserved_tokens=reserved,
                          graduation_threshold=curve.graduation_threshold, launch_supply=int(CFG["launch_supply"]),
                          fee_bps=curve.fee_bps, launched_at_s=curve.launched_at_s, quote_is_native=True,
                          observed_ms=now_ms(), snipe_tax_start_bps=9_900, snipe_tax_seconds=3)
    tax = curve.creator_tax_bps if creator_tax == "fixture" else creator_tax
    priced, why = ep.pons_price(state, token_decimals=18, quote_decimals=18, quote_token=None, creator_tax_bps=tax)
    assert priced is not None, why
    return priced


def _a_token() -> str:
    return CURVES["A"]["token"]


def rh_entry(conn, monkeypatch, priced, *, size=REFUSED_SIZE, decision_id="dec_pons_1", ts=None):
    write_dossier(conn, token=_a_token(), chain=Chain.ROBINHOOD, price="0.000004", liquidity="460")
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (priced, "ok"))
    decision = make_decision(decision_id=decision_id, chain=Chain.ROBINHOOD, token=_a_token(), size=size,
                             ts=ts if ts is not None else now_ms())
    return decision, engine.handoff(decision, None, conn)


def _priced_event(conn) -> dict:
    from kaiba.core.db import jload

    rows = [jload(r["payload"], {}) for r in fetch_all(conn, "SELECT payload FROM events WHERE payload LIKE "
                                                             "'%paper_entry_priced%'")]
    return rows[-1]


def _flat_tape(conn, token: str, decided: int) -> None:
    """Four trades in the 30 s before ``decided`` whose net flow is exactly zero: a flow MEASURED on the tape
    (``adverse:swaps``) that moves the curve by nothing, so the fill is the decision's state and counts."""
    _tape(conn, token, [(decided - 30_000, "buy", 10**16), (decided - 20_000, "sell", 10**16),
                        (decided - 10_000, "buy", 10**16), (decided, "sell", 10**16)])


@pytest.mark.parametrize("measured", [False, True], ids=["no_tape", "flow_measured_zero"])
def test_the_same_decision_now_fills_on_the_curve_at_our_size(tmp_db, monkeypatch, measured):
    """The refused order, replayed through the engine on curve A's real state at the decision. Mutants pinned:
    ``_pons_entry`` dropping the executable (the broker approximates and refuses again), the engine charging
    the mark's 250 bps venue stand-in on top of the take already in the price, and the tag ignoring the
    exact fill.

    2026-10-23 (fS_): it COUNTS only when the token's flow was measured. With nothing on the tape the drift
    is ``unavailable`` -- zero, because nothing was measured, not because nothing moved -- and the same
    exact fill runs but is not honest (``paper_costs.pons_drift_charged``). The fill's numbers are the same
    in both cases: a measured net flow of zero moves the curve by nothing."""
    curve = curve_a_at_decision()
    decided = now_ms()
    if measured:
        _flat_tape(tmp_db, _a_token(), decided)
    decision, order = rh_entry(tmp_db, monkeypatch, priced_from(curve), ts=decided)
    assert order is not None and order.state.value == "filled", getattr(order, "error", None)
    basis = fill_basis(tmp_db, order.order_id)
    assert basis["basis"] == "router" and basis["impact_basis"] == "executable_quote"
    # independent: the curve's own atoms for our size (fee 100 off the input, creator tax 0), at the curve's
    # state -- no flow on the tape, so no drift -- turned into USD per whole token
    net = REFUSED_SIZE - REFUSED_SIZE * 100 // 10_000
    atoms = curve.token_reserve * net // (curve.quote_reserve + net)
    exec_usd = Decimal(REFUSED_SIZE) / Decimal(atoms) * ETH_USD
    spot_usd = Decimal(curve.quote_reserve) / Decimal(curve.token_reserve) * ETH_USD
    venue = 150  # the take 100 is in the price; the mark leg's 250 stand-in less it (pc.pons_exact_venue_bps)
    assert abs(Decimal(basis["executable_price_usd"]) / (exec_usd * (1 + Decimal(100 + venue) / 10_000)) - 1) < Decimal("1e-20")
    assert abs(Decimal(basis["quote_price_usd"]) / (spot_usd * (1 + Decimal(100 + venue) / 10_000)) - 1) < Decimal("1e-20")
    assert 240 < basis["impact_bps"] < 280, basis["impact_bps"]  # was 2,252 on the approximation
    event = _priced_event(tmp_db)
    assert event["fill"] == "curve_quote" and event["venue_bps"] == venue
    assert event["curve_fill"]["atoms"] == str(atoms) and event["curve_fill"]["drift_wei"] == "0"
    basis = event["curve_fill"]["drift_basis"]
    assert (basis == "adverse:swaps") if measured else basis.startswith("unavailable:"), basis
    assert event["honest"] is measured
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["fill"] == "curve_quote" and tag["entry"]["broker"]["impact_basis"] == "executable_quote"
    assert tag["entry"]["curve_fill"]["qty"] == str(load_position(tmp_db, pid).qty)
    assert tag["entry"]["honest"] is measured
    assert pc.entry_counts(tag) is measured and pc.counts_on_scoreboard("robinhood", tag) is measured


def _tape(conn, token: str, rows: list[tuple[int, str, int]]) -> None:
    for i, (ts_ms, side, wei) in enumerate(rows):
        conn.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, amount_native, program, "
                     "source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("robinhood", f"0xt{i}", ts_ms, f"0xw{i}", token, side, "1", str(wei), "pons-curve", "robinhood"))
    conn.commit()


def test_kaibas_own_latency_drift_is_charged_before_our_buy(tmp_db, monkeypatch):
    """The token's own net flow over the last 120 s (``curve_price.measure_flow``), taken against us over the
    MEASURED 14.6 s decision -> landing of a live Pons buy, lands on the curve before our order. Mutant
    pinned: ignoring the drift (filling at the decision's state) must fail here."""
    curve = curve_a_at_decision()
    decided = now_ms()
    # three buys in the 20 s before the decision: 0.04 ETH net in 20 s = 0.002 ETH/s
    _tape(tmp_db, _a_token(), [(decided - 20_000, "buy", 10**16), (decided - 10_000, "buy", 2 * 10**16),
                               (decided, "buy", 10**16)])
    decision, order = rh_entry(tmp_db, monkeypatch, priced_from(curve), ts=decided)
    assert order.state.value == "filled"
    fill = _priced_event(tmp_db)["curve_fill"]
    drift = 4 * 10**16 * m.PONS_ENTRY_LATENCY_MS // 20_000  # 0.0292 ETH, independent of the code under test
    assert m.PONS_ENTRY_LATENCY_MS == 14_600 and int(fill["drift_wei"]) == drift
    assert fill["drift_basis"] == "adverse:swaps" and fill["latency_ms"] == 14_600
    moved_q = curve.quote_reserve + drift
    moved_t = curve.token_reserve - curve.token_reserve * drift // (curve.quote_reserve + drift)
    net = REFUSED_SIZE - REFUSED_SIZE * 100 // 10_000
    assert int(fill["atoms"]) == moved_t * net // (moved_q + net)
    assert int(fill["atoms"]) < curve.token_reserve * net // (curve.quote_reserve + net), "drift costs us tokens"


def test_no_exact_fill_without_the_creator_tax_across_graduation_or_in_other_units(tmp_db, monkeypatch):
    """Each refusal leaves the old mark (``curve_mark``), which the broker approximates and the tag keeps off
    the scoreboard -- never a fill claimed exact on an unknown."""
    curve = curve_a_at_decision()
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (priced_from(curve, creator_tax=None), "ok"))
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=REFUSED_SIZE)
    assert entry.fill == "curve_mark" and entry.executable_price_usd is None and "pons_creator_tax_unread" in entry.note
    monkeypatch.setattr(ep, "read_pons", lambda token, rpc: (priced_from(curve), "ok"))
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=5 * 10**18)
    assert entry.fill == "curve_mark" and "crosses_graduation" in entry.note
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=0)
    assert entry.fill == "curve_mark"
    tmp_db.execute("INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
                   ("robinhood", _a_token(), 6, now_ms()))
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=REFUSED_SIZE)
    assert entry.fill == "curve_mark" and "broker_decimals_6_vs_chain_18" in entry.note
    tmp_db.execute("UPDATE tokens SET decimals=18 WHERE chain='robinhood' AND address=?", (_a_token(),))
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=REFUSED_SIZE)
    assert entry.fill == "curve_quote" and entry.curve_fill["snipe_bps"] == 0
    # a drift that would have graduated the curve during the delay: no fill claimed
    decided = now_ms()
    _tape(tmp_db, _a_token(), [(decided - 2_000, "buy", 2 * 10**18), (decided - 1_000, "buy", 10**18),
                               (decided, "buy", 10**18)])
    entry, _ = m._pons_entry(_a_token(), tmp_db, ETH_USD, None, size=REFUSED_SIZE, at_ms=decided)
    assert entry.fill == "curve_mark" and "pons_graduated_during_latency" in entry.note


def _pons_tag(conn, pid: str, curve_fill) -> dict:
    return pc.record_entry_basis(conn, pid, Chain.ROBINHOOD, source="curve:pons", fill="curve_quote",
                                 buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS,
                                 curve_take_bps=100, broker=AT_SIZE, curve_fill=curve_fill)


def test_an_exact_pons_fill_whose_landing_drift_was_not_measured_runs_but_never_counts(tmp_db, monkeypatch):
    """fS_ (2026-10-23), review of hB_. ``curve_price.drift_for`` returns ZERO drift when the token's flow is
    ``unavailable`` (fewer than ``MIN_FLOW_SAMPLES`` trades in the 120 s window, or the ``pons_trade`` tape
    behind: MEASURED p50 detect lag 134-537 s on the box), so such a fill is at the decision's curve with no
    landing drift and no graduation-during-latency check -- and it was tagged honest. MEASURED on the box, the
    next 14.6 s of those tokens' own buying moved the price a mean ~335 bps against a buyer. Unknown is never
    zero: the entry still runs (exact at our size), but neither the writer nor the reader counts it.

    Mutants pinned (each reverted, the test failing, restored byte-identical): the writer's check in
    ``entry_leg_honest`` removed; the reader's re-check in ``entry_counts`` removed; ``pons_drift_charged``
    as a deny-list (``not basis.startswith("unavailable")``) instead of the allow-list."""
    curve = curve_a_at_decision()
    decided = now_ms()
    # Two trades that would have GRADUATED the curve during the delay, had the flow been measurable: under
    # MIN_FLOW_SAMPLES, so the drift is "unavailable", 0 -- and the curve is filled as if nothing moved.
    _tape(tmp_db, _a_token(), [(decided - 2_000, "buy", 2 * 10**18), (decided, "buy", 2 * 10**18)])
    assert 4 * 10**18 * m.PONS_ENTRY_LATENCY_MS // 2_000 >= curve.headroom, "measured, this flow graduates the curve"
    decision, order = rh_entry(tmp_db, monkeypatch, priced_from(curve), ts=decided)
    assert order is not None and order.state.value == "filled", getattr(order, "error", None)
    event = _priced_event(tmp_db)
    assert event["fill"] == "curve_quote" and event["curve_fill"]["drift_wei"] == "0"
    assert event["curve_fill"]["drift_basis"].startswith("unavailable:")
    assert event["honest"] is False, "an unmeasured drift is not a zero drift"
    [pid] = [r["position_id"] for r in fetch_all(tmp_db, "SELECT position_id FROM positions")]
    tag = pc.read_basis(tmp_db, pid)
    assert tag["entry"]["honest"] is False and not pc.entry_counts(tag) and not pc.counts_on_scoreboard("robinhood", tag)
    assert fill_basis(tmp_db, order.order_id)["impact_basis"] == "executable_quote", "the trade itself still runs"

    # the writer, on every basis drift_for can return: only a flow MEASURED and charged against us counts
    for i, (basis, counts) in enumerate([("adverse:swaps", True), ("adverse:curve_snapshots", True),
                                         ("unavailable:no trades and no snapshot pair inside the window", False),
                                         ("unavailable:swaps_unreadable:OperationalError", False),
                                         ("none", False), ("observed:swaps", False), (None, False)]):
        fill = {"quote_in": "100", "net_wei": "99", "atoms": "1000", "qty": "1000", "drift_wei": "0"}
        if basis is not None:
            fill["drift_basis"] = basis
        written = _pons_tag(tmp_db, f"pos_drift_{i}", fill)
        assert written["entry"]["honest"] is counts and pc.entry_counts(written) is counts, basis
    assert _pons_tag(tmp_db, "pos_drift_none", None)["entry"]["honest"] is False, "no provenance, no count"
    # 2026-10-23 (fd_): the same rule now holds for every pool / curve entry, through its ``landing``
    # (tests/test_paper_landing_drift.py); measured, they count, as before
    measured_landing = {"drift_wei": "0", "drift_basis": "adverse:swaps"}
    assert not pc.entry_leg_honest(Chain.BSC, "curve:flap", fill="curve_quote", broker=AT_SIZE)
    assert pc.entry_leg_honest(Chain.BSC, "curve:flap", fill="curve_quote", broker=AT_SIZE, landing=measured_landing)
    assert not pc.entry_leg_honest(Chain.ROBINHOOD, "pool:uniswap-v4:v4_quoter", transfer_tax=pc.TRANSFER_TAX_PONS,
                                   fill="v4_quoter", broker=AT_SIZE)
    assert pc.entry_leg_honest(Chain.ROBINHOOD, "pool:uniswap-v4:v4_quoter", transfer_tax=pc.TRANSFER_TAX_PONS,
                               fill="v4_quoter", broker=AT_SIZE, landing=measured_landing)

    # the reader: a tag that SAYS honest (written before the writer checked) is re-judged from its curve_fill
    stale = {"chain": "robinhood", "honest": True, "filled_legs": 1,
             "entry": {"source": "curve:pons", "fill": "curve_quote", "honest": True, "curve_take_bps": 100,
                       "transfer_tax": pc.TRANSFER_TAX_PONS, "broker": dict(AT_SIZE),
                       "curve_fill": {**event["curve_fill"], "qty": "1"}},
             "exits": []}
    assert not pc.entry_counts(stale) and not pc.counts_on_scoreboard("robinhood", stale)
    measured = {**stale, "entry": {**stale["entry"], "curve_fill": {**stale["entry"]["curve_fill"],
                                                                    "drift_basis": "adverse:swaps"}}}
    assert pc.entry_counts(measured) and pc.counts_on_scoreboard("robinhood", measured)


def test_the_venue_charge_on_an_exact_fill_keeps_the_mark_legs_fees_and_never_double_charges_the_take():
    assert pc.pons_exact_venue_bps(None) is None
    assert pc.pons_exact_venue_bps(100) == 150  # 100 in the price + 150 = the mark leg's 250
    assert pc.pons_exact_venue_bps(250) == 0 and pc.pons_exact_venue_bps(600) == 0
    assert pc.pons_exact_venue_bps(100, take_charged=600) == 440  # the higher take recorded elsewhere
    for take in (0, 100, 200, 300, 500, 1_100):
        mark_leg = pc.pons_curve_venue_bps(take) + pc.broker_fee_bps(Chain.ROBINHOOD)
        exact_leg = take + pc.pons_exact_venue_bps(take) + pc.broker_fee_bps(Chain.ROBINHOOD)
        assert exact_leg >= mark_leg and exact_leg <= mark_leg + pc.broker_fee_bps(Chain.ROBINHOOD), take


# ----------------------------------------------------------------------------- the exit


def test_the_pons_mark_carries_its_curve_state_and_never_serialises_it():
    curve = curve_a_at_decision()
    priced = priced_from(curve)
    usd = ep.QuoteUsd(price_usd=lambda chain, token: ETH_USD)
    source = ep.EvmVenuePriceSource(None, readers={Chain.ROBINHOOD: lambda token, rpc: (priced, "ok")},
                                    transports={Chain.ROBINHOOD: lambda calls: []}, quote_usd=usd)
    quote = source.quote(Chain.ROBINHOOD, _a_token())
    assert quote.usable and quote.source == "evm-venue:pons"
    assert ep.pons_curve_from_snapshot(quote.venue_curve) == priced.pons_curve
    assert priced.pons_curve.quote_reserve == curve.quote_reserve and priced.pons_curve.creator_tax_bps == 0
    assert "venue_curve" not in quote.model_dump(mode="json")
    foreign = replace(priced, quote_token="0x" + "55" * 20)
    gone = ep.EvmVenuePriceSource(None, readers={Chain.ROBINHOOD: lambda token, rpc: (foreign, "ok")},
                                  transports={Chain.ROBINHOOD: lambda calls: []},
                                  quote_usd=ep.QuoteUsd(price_usd=lambda chain, token: ETH_USD))
    assert gone.quote(Chain.ROBINHOOD, _a_token()).venue_curve is None, "a non-native curve is not sold here"
    assert ep.pons_curve_from_snapshot({"r": 1}) is None
    assert ep.pons_curve_from_snapshot({**quote.venue_curve, "venue": "flap"}) is None


def test_selling_straight_back_on_an_unchanged_curve_returns_what_reached_it_less_its_take():
    for label in ("A", "B"):
        curve = launch_curve(CURVES[label]) if label == "B" else curve_a_at_decision()
        atoms, net, _ = curve.tokens_out(10**17, at_s=curve.launched_at_s + 60)
        out, why = ep.pons_sell_quote_out(curve, atoms, atoms, entry_net_quote=net)
        assert out is not None, why
        back = net - net * curve.fee_bps // 10_000 - net * curve.creator_tax_bps // 10_000
        # within the contract's own floors: the sale's gross never exceeds what reached the curve, but the
        # two fee floors on a gross one wei lower can each round one wei in the seller's favour
        assert abs(out - back) <= 3, (label, out, back)
        shifted, _ = ep.pons_with_holding(curve, atoms, entry_net_quote=net)
        assert shifted.quote_out(atoms)[1] <= net, "the gross never exceeds the quote that went in"
        # rounded against us: the shifted curve never holds fewer tokens than the live one less ours (the
        # contract's floor on a buy of ``ceil(cost)`` alone would take out a few hundred million atoms more)
        assert shifted.token_reserve >= curve.token_reserve - atoms, label


def _independent_sale(curve: ep.PonsCurve, held: int, delta: int) -> int:
    """The quote a sale of ``held`` atoms receives after the curve took a buy of ``delta`` (removing at most
    the ``held`` atoms we hold): re-derived here, not taken from evm_price."""
    q2 = curve.quote_reserve + delta
    t2 = curve.token_reserve - min(curve.token_reserve * delta // (curve.quote_reserve + delta), held)
    gross = held * q2 // (t2 + held)
    return gross - gross * curve.fee_bps // 10_000 - gross * curve.creator_tax_bps // 10_000


def test_our_holding_goes_back_at_the_smaller_of_the_two_counterfactuals():
    """Price UP since our entry (others bought): the quote our buy put in is the smaller shift -- Vanguard's
    ``withHolding``; the token-side one would credit us with today's price for tokens we bought cheaper.
    Price DOWN: the token side is the smaller. Mutants pinned: ``max`` for ``min``, and ignoring the entry's
    quote."""
    curve = launch_curve(CURVES["B"])
    atoms, net, _ = curve.tokens_out(10**17, at_s=curve.launched_at_s + 60)
    risen = curve.after_buy(10**18)   # 1 ETH of others' buys after ours (the live curve never had ours)
    cost_now = -(-atoms * risen.quote_reserve // (risen.token_reserve - atoms))
    assert cost_now > net
    out, _ = ep.pons_sell_quote_out(risen, atoms, atoms, entry_net_quote=net)
    assert out == _independent_sale(risen, atoms, net) < _independent_sale(risen, atoms, cost_now)
    fallen = curve.after_buy(10**18)
    gross_dump = 4 * 10**25 * fallen.quote_reserve // (fallen.token_reserve + 4 * 10**25)
    fallen = after_sell(fallen, 4 * 10**25, gross_dump)
    small_net = net // 50  # a tiny entry quote: the quote side is now the smaller
    cost_small = -(-atoms * fallen.quote_reserve // (fallen.token_reserve - atoms))
    out_small, _ = ep.pons_sell_quote_out(fallen, atoms, atoms, entry_net_quote=small_net)
    assert out_small == _independent_sale(fallen, atoms, min(small_net, cost_small))
    out_tokens, _ = ep.pons_sell_quote_out(fallen, atoms, atoms, entry_net_quote=10**19)
    assert out_tokens == _independent_sale(fallen, atoms, cost_small), "the token side binds"
    assert ep.pons_sell_quote_out(fallen, atoms, atoms)[0] == out_tokens, "no entry quote: the token side alone"


@pytest.mark.parametrize("held", [10**25, 3 * 10**25 + 7, 123_456_789 * 10**18, 5 * 10**24 + 1])
def test_the_holding_put_back_never_takes_more_tokens_out_than_we_hold(held):
    """Rounded against us: the token-side shift is a buy of ``ceil(cost)``, and the contract's floor on that
    buy takes out 2.5M-280M atoms MORE than we hold on these amounts, leaving the curve dearer than our
    holding made it. Mutant pinned: dropping the cap at ``held``."""
    curve = curve_a_at_decision()
    shifted, why = ep.pons_with_holding(curve, held)
    assert shifted is not None, why
    assert shifted.token_reserve == curve.token_reserve - held
    cost = -(-held * curve.quote_reserve // (curve.token_reserve - held))
    assert shifted.quote_reserve == curve.quote_reserve + cost
    assert curve.token_reserve * cost // (curve.quote_reserve + cost) > held, "the uncapped shift would overshoot"


def test_a_holding_that_would_have_graduated_the_curve_is_not_priced():
    curve = curve_a_at_decision()
    assert ep.pons_sell_quote_out(curve, 10**18, 2 * 10**18) == (None, "sale_not_within_holding")
    # the atoms the curve's whole headroom buys, and a few more: holding them would have graduated it
    room = curve.token_reserve * curve.headroom // (curve.quote_reserve + curve.headroom)
    assert ep.pons_with_holding(curve, room + 10**18)[1] == "holding_crosses_graduation"
    assert ep.pons_with_holding(curve, room // 2)[1] == "ok"
    assert ep.pons_with_holding(curve, curve.token_reserve)[1] == "holding_exceeds_curve"
    assert ep.pons_with_holding(replace(curve, creator_tax_bps=None), 10**20)[1] == "pons_creator_tax_unread"


def test_the_tag_keeps_the_entrys_curve_quote_and_merges_it_on_a_top_up(tmp_db):
    fill = {"quote_in": "100", "net_wei": "99", "atoms": "1000", "qty": "900", "drift_wei": "0"}
    first = pc.record_entry_basis(tmp_db, "pos_cf", Chain.ROBINHOOD, source="curve:pons", fill="curve_quote",
                                  buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS,
                                  curve_take_bps=100, broker=AT_SIZE, curve_fill=fill)
    assert first["entry"]["curve_fill"] == fill and pc.holding_entry_net(first["entry"], 450) == 49
    second = pc.record_entry_basis(tmp_db, "pos_cf", Chain.ROBINHOOD, source="curve:pons", fill="curve_quote",
                                   buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False,
                                   transfer_tax=pc.TRANSFER_TAX_PONS, curve_take_bps=100, broker=AT_SIZE,
                                   curve_fill=fill, prior_filled_orders=1)
    assert second["entry"]["curve_fill"]["net_wei"] == "198" and second["entry"]["curve_fill"]["qty"] == "1800"
    # an earlier leg filled anywhere else: the quote our holding put in is unknown, so none is kept
    pc.record_entry_basis(tmp_db, "pos_cg", Chain.ROBINHOOD, source="pool:uniswap-v4:v4_quoter", fill="v4_quoter",
                          buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS,
                          broker=AT_SIZE)
    mixed = pc.record_entry_basis(tmp_db, "pos_cg", Chain.ROBINHOOD, source="curve:pons", fill="curve_quote",
                                  buy_tax_bps=0, sell_tax_bps=0, tax_in_price=False, transfer_tax=pc.TRANSFER_TAX_PONS,
                                  curve_take_bps=100, broker=AT_SIZE, curve_fill=fill, prior_filled_orders=1)
    assert "curve_fill" not in mixed["entry"] and pc.holding_entry_net(mixed["entry"], 10) is None
    assert pc.holding_entry_net({"curve_fill": {**fill, "qty": "0"}}, 10) is None


def _round_trip(conn, monkeypatch, *, carry_curve: bool, others_bought_wei: int = 0):
    """A real Pons entry through the engine on curve A's decision state, then the paper exit decided on the
    curve's own mark: the SAME curve, or that curve after ``others_bought_wei`` of other people's buys."""
    curve = curve_a_at_decision()
    priced = priced_from(curve)
    decided = now_ms()
    _flat_tape(conn, _a_token(), decided)  # a measured flow of zero: the entry counts and fills at the decision state
    decision, order = rh_entry(conn, monkeypatch, priced, ts=decided)
    assert order is not None and order.state.value == "filled"
    [pid] = [r["position_id"] for r in fetch_all(conn, "SELECT position_id FROM positions")]
    assert pc.entry_counts(pc.read_basis(conn, pid)), "the round trip below must be a counted row"
    position = load_position(conn, pid)

    def no_pool(*a, **k):
        raise AssertionError("a curve-decided exit must not read a pool")

    monkeypatch.setattr(m, "exit_fill", no_pool)
    if others_bought_wei:
        curve = curve.after_buy(others_bought_wei)
        priced = priced_from(curve)
    source = ep.EvmVenuePriceSource(None, readers={Chain.ROBINHOOD: lambda token, rpc: (priced, "ok")},
                                    transports={Chain.ROBINHOOD: lambda calls: []},
                                    quote_usd=ep.QuoteUsd(price_usd=lambda chain, token: ETH_USD))
    quote = source.quote(Chain.ROBINHOOD, _a_token())
    if not carry_curve:
        quote = quote.model_copy(update={"venue_curve": None})
    out = wd.DefaultExitSubmitter(conn, FakeSource({}))._paper(position, Decimal(100), quote, "stop_loss")
    assert out.ok, out.detail
    return curve, pid, position, load_position(conn, pid)


def test_a_pons_exit_is_sold_on_the_curve_with_our_holding_put_back_and_books_exactly_its_fees(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """Mutants pinned: not routing robinhood to the curve sale (``curve_mark`` again), selling into the live
    curve without our holding (the live curve never got our 0.028 ETH: ~3% less here), and charging the
    mark's venue stand-in on top of the take in the price."""
    curve, pid, before, after = _round_trip(tmp_db, monkeypatch, carry_curve=True)
    tag = pc.read_basis(tmp_db, pid)
    leg = tag["exits"][-1]
    assert leg["fill"] == pc.CURVE_QUOTE and leg["broker"] == {**AT_SIZE, "impact_bps": 0, "over_tolerance": False}
    assert leg["honest"] and leg["venue_bps"] == 150 and pc.is_honest(tag) and not pc.exit_degraded(tag)
    held = before.qty
    entry_net = int(tag["entry"]["curve_fill"]["net_wei"])
    cost_now = -(-held * curve.quote_reserve // (curve.token_reserve - held))
    out = _independent_sale(curve, held, min(entry_net, cost_now))
    native = Decimal(out) * (1 - Decimal(100 + 150) / 10_000) * (1 - Decimal(DEFAULT_SLIPPAGE_FLOOR_BPS) / 10_000)
    expected = native - native * DEFAULT_FEE_BPS[Chain.ROBINHOOD] / 10_000 - DEFAULT_TIP_NATIVE[Chain.ROBINHOOD]
    assert abs(Decimal(after.proceeds_native) - expected) <= Decimal(10**6), (after.proceeds_native, expected)
    # on a curve nobody else touched, the round trip loses its fees and nothing else
    loss = Decimal(after.realized_native) / Decimal(after.cost_native)
    assert Decimal("-0.10") < loss < Decimal("-0.08"), loss
    # selling into the live curve WITHOUT our holding books less, by about our own impact (0.028 of 1.776 ETH)
    without, _gross = curve.quote_out(held)
    assert without is not None and out * 96 // 100 < without < out * 98 // 100


def test_after_others_bought_the_exit_puts_back_only_the_quote_our_buy_put_in(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    """The price rose after our entry (0.5 ETH of other buys): at today's price our tokens would have cost
    more than our buy put in, and crediting that would book other people's buying as ours. The entry's
    recorded quote binds. Mutant pinned: the exit not passing the entry's quote (the token side alone)."""
    curve, pid, before, after = _round_trip(tmp_db, monkeypatch, carry_curve=True, others_bought_wei=5 * 10**17)
    tag = pc.read_basis(tmp_db, pid)
    assert tag["exits"][-1]["fill"] == pc.CURVE_QUOTE and pc.is_honest(tag)
    held = before.qty
    entry_net = int(tag["entry"]["curve_fill"]["net_wei"])
    cost_now = -(-held * curve.quote_reserve // (curve.token_reserve - held))
    assert entry_net < cost_now
    out = _independent_sale(curve, held, entry_net)
    native = Decimal(out) * (1 - Decimal(100 + 150) / 10_000) * (1 - Decimal(DEFAULT_SLIPPAGE_FLOOR_BPS) / 10_000)
    expected = native - native * DEFAULT_FEE_BPS[Chain.ROBINHOOD] / 10_000 - DEFAULT_TIP_NATIVE[Chain.ROBINHOOD]
    assert abs(Decimal(after.proceeds_native) - expected) <= Decimal(10**6), (after.proceeds_native, expected)
    # what the token side alone would have booked, far from it
    assert _independent_sale(curve, held, cost_now) - out > 10**14  # 1.4% more: other people's buying


def test_a_pons_exit_without_the_curve_state_is_the_old_approximation_and_not_honest(
        tmp_db, monkeypatch, risk_file):  # noqa: F811
    curve, pid, before, after = _round_trip(tmp_db, monkeypatch, carry_curve=False)
    leg = pc.read_basis(tmp_db, pid)["exits"][-1]
    assert leg["fill"] == "curve_mark" and leg["broker"]["impact_basis"] == "constant_product_approximation"
    assert not leg["honest"] and leg["venue_bps"] == 250 and "no pons curve state" in leg["note"]
    assert pc.exit_degraded(pc.read_basis(tmp_db, pid))
