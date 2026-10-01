"""Paper broker arithmetic and the decision engine's skip discipline.

Two things are being defended here.

First, that a shadow fill is *modelled* rather than assumed: fee, tip and constant-product
price impact are checked to the base unit, and an order that would move the pool more than
its own slippage tolerance is rejected instead of filled. A paper broker that always fills
makes every promotion decision downstream of it worthless.

Second, that ``decide()`` writes a row for every rejection. Skips are the training data
for the learning loop; a rejection path that returns without persisting is a silent hole
in the record.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.config import load_risk
from kaiba.core.schemas import (
    Action,
    Chain,
    Decision,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Measure,
    OrderState,
    Receipt,
    Signal,
    TokenDossier,
    TokenRisk,
    now_ms,
)
from kaiba.execution import curve_price, engine, lanes
from kaiba.execution.paper import DEFAULT_FEE_BPS, PaperBroker, fill_basis, load_position

TOKEN = "PaperTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NOW = 1_780_000_000_000

# One SOL in, priced at $100/SOL, into a $9,801 pool. Every number below is derived from
# these by hand in the test that uses it, so a change in the model breaks the test loudly.
ONE_SOL = 1_000_000_000


@pytest.fixture
def broker(tmp_db):
    """Deterministic broker: no slippage floor, so impact maths can be asserted exactly."""
    return PaperBroker(
        tmp_db,
        fee_bps={Chain.SOL: 100},
        tip_native={Chain.SOL: 1_000_000},
        native_usd={Chain.SOL: Decimal("100")},
        slippage_floor_bps=0,
        slippage_bps=1500,
    )


def make_decision(
    *,
    decision_id: str = "dec_paper_1",
    size: int = ONE_SOL,
    chain: Chain = Chain.SOL,
    mode: LaneMode = LaneMode.SHADOW,
    token: str = TOKEN,
    ts: int = NOW,
) -> Decision:
    return Decision(
        decision_id=decision_id,
        ts_ms=ts,
        lane=Lane.CONFLUENCE_5,
        mode=mode,
        chain=chain,
        token=token,
        action=Action.ENTER,
        thesis="fixture",
        confidence=0.6,
        size_base_units=size,
    )


def seed_protection_evidence(conn, token: str, *, chain=Chain.SOL) -> None:
    """Give the PROTECTION path something to read for ``token``.

    Added 2026-09-22 with `engine._protection_refusal`, which enforces the rule
    "never open a position we cannot monitor" by probing the price source the WATCHDOG
    will use before an entry is handed off. These tests seed the SIZING path (a dossier
    price) and nothing the protection path can read, which is exactly the state that gate
    exists to refuse -- so the fixture, not the behaviour, is what was stale.

    Why that gate exists: on the live box that day, four funded Solana positions were
    opened and then could not be priced at all, so their stops could not be evaluated;
    and two BSC positions could be bought and not sold for 53 minutes for the same class
    of reason. A test that opens a position nothing can price is describing the bug.

    Seeds the three things the Solana price route needs: a curve snapshot, chain-verified
    decimals, and a contemporaneous native/USD sample. The snapshot must satisfy
    ``virtual_token - real_token == curve_price.RESERVED_TOKEN_ATOMS`` or the resolver
    refuses with ``reserved_token_invariant_violated``.
    """
    from kaiba.execution import curve_price as _cp

    now = now_ms()
    real_token = 700_000_000_000_000
    conn.execute(
        "INSERT OR REPLACE INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (chain.value, token, now, 30_000_000_000, 60_000_000_000,
         str(real_token), str(real_token + _cp.RESERVED_TOKEN_ATOMS), "pumpfun"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
        (chain.value, token, 6, now),
    )
    conn.execute(
        "INSERT INTO native_prices (chain, ts_ms, price_usd, source) VALUES (?,?,?,?)",
        (chain.value, now, "150.0", "test"),
    )
    conn.commit()


def write_dossier(
    conn,
    *,
    token: str = TOKEN,
    chain: Chain = Chain.SOL,
    price: str | None = "0.001",
    liquidity: str | None = "50000",
    blockers: tuple[str, ...] = (),
    built_age_s: int = 5,
    grade: Grade = Grade.B,
) -> TokenDossier:
    def m(value: str | None) -> Measure:
        if value is None:
            return Measure.unknown()
        return Measure(
            value=Decimal(value),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dyor"),
            freshness_budget_s=86_400,
        )

    dossier = TokenDossier(
        address=token,
        chain=chain,
        price_usd=m(price),
        liquidity_usd=m(liquidity),
        blockers=[TokenRisk(b) for b in blockers],
        grade=grade,
        built_at_ms=now_ms() - built_age_s * 1000,
    )
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, token, dossier.built_at_ms, None, grade.value,
         "[]", "[]", "[]", dossier.model_dump_json()),
    )
    return dossier


def make_signal(conn=None, *, lane: Lane = Lane.CONFLUENCE_5, token: str = TOKEN,
                strength: float = 0.6, created_ms: int | None = None) -> Signal:
    signal = Signal(
        signal_id=lanes.signal_id_for(lane, Chain.SOL, token, created_ms or now_ms(), 120),
        lane=lane,
        chain=Chain.SOL,
        token=token,
        strength=strength,
        reasons=["fixture signal"],
        window_s=120,
        created_ms=created_ms or now_ms(),
    )
    if conn is not None:
        lanes.record(signal, conn)
    return signal


@pytest.fixture
def solo_risk(monkeypatch):
    """Neutralise ``risk.py``'s own arithmetic so these tests exercise engine guards.

    This used to set ``engine.RiskGate = None``. That worked only because a missing gate
    was silently treated as permission, which was the bug: the real gate was never
    reached on any decision path. A missing gate now refuses, so the fixture installs a
    permissive stub instead. The stub keeps the original intent — isolate the engine from
    the sizing and exposure rules that ``risk.py`` tests already cover — without
    depending on a fail-open. ``test_a_missing_risk_gate_refuses_the_entry`` pins the
    absent case separately.
    """

    class PermissiveGate:
        def check_entry(self, **kwargs):
            return None

        def position_size(self, chain, lane, score, conn=None, *, token=None):
            # The gate itself answers. Until 2026-09-22 this class had no
            # `position_size`, so `_size_for` raised AttributeError and fell through to
            # the module-level fallback patched below -- i.e. this fixture WAS
            # "depending on a fail-open", the one thing its docstring says it does not
            # do. The engine now refuses instead of falling through, which is what broke
            # these four tests and is exactly the behaviour we want.
            return ONE_SOL

    monkeypatch.setattr(engine, "RiskGate", PermissiveGate)
    # Kept patched so a regression that reaches the fallback is loud rather than silent:
    # the real one returns 0, so a test that starts depending on it fails on the size.
    monkeypatch.setattr(engine, "position_size", lambda chain, lane, score, conn=None: ONE_SOL)


def _position_of(conn, order_id: str) -> str:
    row = conn.execute("SELECT position_id FROM position_orders WHERE order_id=?", (order_id,)).fetchone()
    return row["position_id"]


# ============================================================ fee / impact arithmetic


def test_impact_is_constant_product_and_no_liquidity_is_total_impact(broker):
    assert broker.impact_bps(Decimal(100), Decimal(900)) == 1000  # 100/(900+100) = 10%
    assert broker.impact_bps(Decimal(1), Decimal(999)) == 10
    assert broker.impact_bps(Decimal(100), Decimal(0)) == 10_000
    assert broker.impact_bps(Decimal(0), Decimal(1000)) == 0


def test_buy_fee_impact_and_quantity_are_exact_in_base_units(broker, tmp_db):
    # 1 SOL in, 100 bps fee -> 0.99 SOL spent = $99 at $100/SOL.
    # impact = 99 / (9801 + 99) = 1.00% = 100 bps; effective price = 0.001 * 1.01 = 0.00101.
    # qty = 99 / 0.00101 * 1e6 = 98,019,801,980 atoms (floored).
    order = broker.buy(
        make_decision(),
        price_usd=Decimal("0.001"),
        liquidity_usd=Decimal("9801"),
        now_ms=NOW,
        decimals=6,
    )
    assert order.state is OrderState.FILLED
    assert order.amount_in == ONE_SOL
    assert order.fee_native == 10_000_000 + 1_000_000  # swap fee + fixed tip
    assert order.filled_out == 98_019_801_980
    assert order.min_out == 86_086_956_521  # sized off the 1500 bps tolerance, not the fill

    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    assert position.qty == 98_019_801_980
    assert position.cost_native == ONE_SOL + 1_000_000  # fee-inclusive, tip included


def test_slippage_floor_is_added_on_top_of_impact(tmp_db):
    floored = PaperBroker(
        tmp_db,
        fee_bps={Chain.SOL: 100},
        tip_native={Chain.SOL: 1_000_000},
        native_usd={Chain.SOL: Decimal("100")},
        slippage_floor_bps=50,
        slippage_bps=1500,
    )
    order = floored.buy(
        make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
        now_ms=NOW, decimals=6,
    )
    # 100 bps impact + 50 bps floor -> effective price 0.001 * 1.015
    assert order.filled_out == int(Decimal("99000000") / (Decimal("0.001") * Decimal("1.015")))
    assert order.filled_out < 98_019_801_980


def test_a_large_order_into_a_thin_pool_is_rejected_not_filled(broker, tmp_db):
    order = broker.buy(
        make_decision(),
        price_usd=Decimal("0.001"),
        liquidity_usd=Decimal("1000"),  # $99 into $1k of depth is ~900 bps
        now_ms=NOW,
        decimals=6,
        slippage_bps=500,
    )
    assert order.state is OrderState.FAILED
    assert order.error.startswith("slippage_exceeded")
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_a_rejected_order_still_leaves_an_audit_trail(broker, tmp_db):
    order = broker.buy(
        make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("500"),
        now_ms=NOW, decimals=6, slippage_bps=300,
    )
    states = [r["state"] for r in tmp_db.execute(
        "SELECT state FROM order_events WHERE order_id=? ORDER BY id", (order.order_id,))]
    assert states[0] == OrderState.PLANNED.value
    assert states[-1] == OrderState.FAILED.value
    assert OrderState.SUBMITTING.value in states


def test_zero_size_and_zero_price_fail_rather_than_fill(broker):
    assert broker.buy(make_decision(size=0), price_usd=Decimal("0.001"),
                      liquidity_usd=Decimal("50000"), now_ms=NOW, decimals=6).error == "no_size"
    assert broker.buy(make_decision(decision_id="dec_np"), price_usd=Decimal("0"),
                      liquidity_usd=Decimal("50000"), now_ms=NOW, decimals=6).error == "no_price"


def test_identical_inputs_produce_identical_fills(broker, tmp_db):
    a = broker.buy(make_decision(decision_id="dec_a"), price_usd=Decimal("0.001"),
                   liquidity_usd=Decimal("9801"), now_ms=NOW, decimals=6)
    tmp_db.execute("DELETE FROM positions")
    b = broker.buy(make_decision(decision_id="dec_b"), price_usd=Decimal("0.001"),
                   liquidity_usd=Decimal("9801"), now_ms=NOW, decimals=6)
    assert a.filled_out == b.filled_out
    assert a.fee_native == b.fee_native


def test_a_second_buy_adds_to_the_open_position(broker, tmp_db):
    first = broker.buy(make_decision(decision_id="dec_1"), price_usd=Decimal("0.001"),
                       liquidity_usd=Decimal("9801"), now_ms=NOW, decimals=6)
    second = broker.buy(make_decision(decision_id="dec_2"), price_usd=Decimal("0.001"),
                        liquidity_usd=Decimal("9801"), now_ms=NOW + 5_000, decimals=6)
    assert _position_of(tmp_db, first.order_id) == _position_of(tmp_db, second.order_id)
    position = load_position(tmp_db, _position_of(tmp_db, first.order_id))
    assert position.qty == first.filled_out + second.filled_out
    assert position.cost_native == 2 * (ONE_SOL + 1_000_000)


# ============================================================ round trips


def _round_trip(broker, conn, *, exit_price: str, hold_s: int = 60, pct: float = 100):
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(conn, _position_of(conn, order.order_id))
    sell = broker.sell(position, pct, price_usd=Decimal(exit_price), liquidity_usd=Decimal("200000"),
                       now_ms=NOW + hold_s * 1000, decimals=6, exit_reason="tp1")
    return order, sell, load_position(conn, position.position_id)


def test_a_winning_round_trip_writes_a_trade_with_the_right_sign_and_hold(broker, tmp_db):
    _, sell, position = _round_trip(broker, tmp_db, exit_price="0.002", hold_s=60)
    assert sell.state is OrderState.FILLED
    assert position.qty == 0 and position.closed_ms == NOW + 60_000
    trade = tmp_db.execute("SELECT * FROM trades").fetchone()
    assert int(trade["pnl_native"]) > 0
    assert trade["pnl_pct"] > 0
    assert trade["hold_s"] == 60
    assert trade["exit_reason"] == "tp1"
    assert int(trade["proceeds_native"]) - int(trade["cost_native"]) == int(trade["pnl_native"])


def test_a_losing_round_trip_writes_a_negative_pnl(broker, tmp_db):
    _, _, position = _round_trip(broker, tmp_db, exit_price="0.0004", hold_s=120)
    trade = tmp_db.execute("SELECT * FROM trades").fetchone()
    assert int(trade["pnl_native"]) < 0
    assert trade["pnl_pct"] < 0
    assert trade["hold_s"] == 120
    assert position.realized_native == int(trade["pnl_native"])


def test_fees_on_the_trade_row_include_both_legs_and_both_tips(broker, tmp_db):
    buy, sell, _ = _round_trip(broker, tmp_db, exit_price="0.002")
    trade = tmp_db.execute("SELECT fees_native FROM trades").fetchone()
    assert int(trade["fees_native"]) == buy.fee_native + sell.fee_native


def test_a_partial_exit_leaves_the_right_quantity_and_no_trade_row(broker, tmp_db):
    order, sell, position = _round_trip(broker, tmp_db, exit_price="0.002", pct=50)
    assert sell.amount_in == order.filled_out // 2
    assert position.qty == order.filled_out - sell.amount_in
    assert position.closed_ms is None
    assert tmp_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert position.proceeds_native > 0


def test_the_final_leg_of_a_laddered_exit_closes_the_position(broker, tmp_db):
    order, _, position = _round_trip(broker, tmp_db, exit_price="0.002", pct=50)
    broker.sell(position, 100, price_usd=Decimal("0.003"), liquidity_usd=Decimal("200000"),
                now_ms=NOW + 120_000, decimals=6, exit_reason="trail")
    closed = load_position(tmp_db, position.position_id)
    assert closed.qty == 0
    trade = tmp_db.execute("SELECT * FROM trades").fetchone()
    assert trade["exit_reason"] == "trail"
    assert trade["hold_s"] == 120
    assert int(trade["pnl_native"]) > 0


def test_selling_nothing_fails_instead_of_silently_succeeding(broker, tmp_db):
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    failed = broker.sell(position, 0, price_usd=Decimal("0.002"), liquidity_usd=Decimal("200000"),
                         now_ms=NOW + 1000, decimals=6)
    assert failed.state is OrderState.FAILED and failed.error == "nothing_to_sell"
    assert load_position(tmp_db, position.position_id).qty == order.filled_out


def test_an_exit_into_thin_liquidity_fills_outside_tolerance_rather_than_stranding_us(broker, tmp_db):
    """An exit is never refused on price, however bad the price is.

    This test asserted the opposite until 2026-09-21, and the old behaviour cost us: the
    watchdog retries every five seconds, so two live positions accumulated 320 refused
    sells each and were stranded, while the shadow record showed them *open* rather than
    showing the loss we would really have taken. A slippage cap protects an entry, where
    declining means not opening a position. On the way out it means holding a token whose
    pool is draining -- and 14 of our first 41 closed trades exited to the rug monitor,
    so the pools we most need to leave are exactly the ones that cannot clear a cap.
    """
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    exit_order = broker.sell(position, 100, price_usd=Decimal("0.002"), liquidity_usd=Decimal("100"),
                             now_ms=NOW + 1000, decimals=6, slippage_bps=500)
    assert exit_order.state is OrderState.FILLED
    closed = load_position(tmp_db, position.position_id)
    assert closed.closed_ms is not None, "the position must not be left open"

    # ...and the record has to say what getting out cost, or filling would be the laxer
    # answer rather than the more honest one.
    basis = fill_basis(tmp_db, exit_order.order_id)
    assert basis["over_tolerance"] is True
    assert basis["total_slippage_bps"] > basis["tolerance_bps"]


def test_an_exit_with_no_price_at_all_is_still_refused(broker, tmp_db):
    """The exception that proves the rule: no price is not a bad price.

    Filling outside a tolerance is a real trade at a real cost. Filling with no quote
    would be inventing one, which is the thing the shadow record must never contain.
    """
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    refused = broker.sell(position, 100, price_usd=Decimal("0"), liquidity_usd=Decimal("100"),
                          now_ms=NOW + 1000, decimals=6, slippage_bps=500)
    assert refused.state is OrderState.FAILED
    assert load_position(tmp_db, position.position_id).closed_ms is None


# ============================================================ mark to market


def test_mark_to_market_tracks_peak_and_mfe(broker, tmp_db):
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    marked = broker.mark_to_market(position, Decimal("0.0015"), now_ms=NOW + 10_000)
    assert marked.mfe_pct > 0
    assert marked.mae_pct == 0.0
    assert marked.peak_price_usd == Decimal("0.0015")


def test_mark_to_market_tracks_mae_and_keeps_the_peak(broker, tmp_db):
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    broker.mark_to_market(position, Decimal("0.0015"), now_ms=NOW + 10_000)
    marked = broker.mark_to_market(position, Decimal("0.0006"), now_ms=NOW + 20_000)
    assert marked.mae_pct < 0
    assert marked.mfe_pct > 0
    assert marked.peak_price_usd == Decimal("0.0015")
    assert tmp_db.execute("SELECT COUNT(*) FROM position_marks").fetchone()[0] == 2


def test_excursions_reach_the_trade_row(broker, tmp_db):
    order = broker.buy(make_decision(), price_usd=Decimal("0.001"), liquidity_usd=Decimal("9801"),
                       now_ms=NOW, decimals=6)
    position = load_position(tmp_db, _position_of(tmp_db, order.order_id))
    broker.mark_to_market(position, Decimal("0.004"), now_ms=NOW + 5_000)
    broker.mark_to_market(position, Decimal("0.0005"), now_ms=NOW + 10_000)
    position = load_position(tmp_db, position.position_id)
    broker.sell(position, 100, price_usd=Decimal("0.002"), liquidity_usd=Decimal("200000"),
                now_ms=NOW + 30_000, decimals=6, exit_reason="tp1")
    trade = tmp_db.execute("SELECT mae_pct, mfe_pct FROM trades").fetchone()
    assert trade["mae_pct"] < 0 < trade["mfe_pct"]


# ============================================================ decide(): skips are data


def test_decide_enters_when_everything_passes(tmp_db, solo_risk):
    write_dossier(tmp_db)
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.ENTER
    assert decision.size_base_units == ONE_SOL
    assert decision.invalidation
    row = tmp_db.execute("SELECT * FROM decisions").fetchone()
    assert row["action"] == "enter" and row["dossier_grade"] == "B"


def test_decide_skips_and_persists_when_the_lane_is_off(tmp_db, monkeypatch, solo_risk):
    write_dossier(tmp_db)
    cfg = load_risk().model_copy(update={"kill_switch": True})
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert decision.blockers == ["lane_off"]
    assert decision.mode is LaneMode.OFF
    assert tmp_db.execute("SELECT action FROM decisions").fetchone()["action"] == "skip"


def test_decide_skips_without_a_dossier(tmp_db, solo_risk):
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP and decision.blockers == ["no_dossier"]
    assert tmp_db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_decide_skips_on_a_stale_dossier(tmp_db, solo_risk):
    write_dossier(tmp_db, built_age_s=engine.DOSSIER_MAX_AGE_S + 60)
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP and decision.blockers == ["no_dossier"]
    assert "old" in decision.thesis


def test_decide_skips_on_dossier_blockers_and_records_them(tmp_db, solo_risk):
    write_dossier(tmp_db, blockers=("honeypot", "lp_not_burned"))
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert decision.blockers == ["honeypot", "lp_not_burned"]
    row = tmp_db.execute("SELECT blockers_json FROM decisions").fetchone()
    assert "honeypot" in row["blockers_json"]


def test_decide_skips_on_a_risk_veto(tmp_db, monkeypatch, solo_risk):
    write_dossier(tmp_db)
    monkeypatch.setattr(engine, "_risk_refusal", lambda *a, **k: "daily_loss_stop")
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP and decision.blockers == ["daily_loss_stop"]


def test_decide_honours_entries_paused_without_the_sibling_risk_module(tmp_db, monkeypatch, solo_risk):
    write_dossier(tmp_db)
    cfg = load_risk().model_copy(update={"entries_paused": True})
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    assert engine.decide(make_signal(), tmp_db).blockers == ["entries_paused"]


def _halt_row(conn, day: str, reason: str = "daily_loss_stop") -> None:
    conn.execute(
        "INSERT INTO risk_state (day_key, entries, halted, halt_reason, updated_ms) "
        "VALUES (?,?,?,?,?)",
        (day, 3, 1, reason, now_ms()),
    )


def test_decide_honours_todays_halt_row(tmp_db, solo_risk):
    write_dossier(tmp_db)
    _halt_row(tmp_db, engine.day_key())
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert decision.blockers == ["risk_halt:daily_loss_stop"]


def test_a_halt_from_a_previous_day_does_not_block_today(tmp_db, solo_risk):
    """THE REGRESSION. This test used to insert a row dated 2026-09-20 and assert the
    engine honoured it, which pinned the bug as the contract.

    MEASURED 2026-09-23: the 2026-09-22 row was halted at 07:47 by a protection overrun,
    the UTC day rolled before anything cleared it, and the engine's read was
    `WHERE halted=1 LIMIT 1` with no day at all. From 00:00 UTC every entry on every chain
    was refused with a fault from the previous day that had already been fixed.

    It was invisible from both sides: today's row read `halted=0`, `RiskGate.check_entry`
    allowed the entry, and no `risk.halt` event fired -- because nothing WAS halting. And
    it could not be lifted, because `RiskGate.resume` only ever clears today's row.
    """
    write_dossier(tmp_db)
    _halt_row(tmp_db, "2026-09-20", reason="protection_overrun:15204ms>5000ms x348")
    decision = engine.decide(make_signal(), tmp_db)
    assert not any("risk_halt" in str(b) for b in decision.blockers), decision.blockers


def test_the_engine_and_the_gate_scope_a_halt_to_the_same_day(tmp_db):
    """They must agree, or a halt one can see is one the other cannot clear."""
    from kaiba.execution.risk import day_key as gate_day_key

    assert engine.day_key() == gate_day_key()


def test_decide_skips_when_the_sizer_returns_nothing(tmp_db, monkeypatch, solo_risk):
    write_dossier(tmp_db)

    # Drive the GATE to zero. Patching the module-level `position_size` no longer does
    # anything: since 2026-09-22 `_size_for` refuses instead of falling through to it.
    class ZeroGate:
        def check_entry(self, **kwargs):
            return None

        def position_size(self, chain, lane, score, conn=None, *, token=None):
            return 0

    monkeypatch.setattr(engine, "RiskGate", ZeroGate)
    assert engine.decide(make_signal(), tmp_db).blockers == ["no_size"]


@pytest.mark.parametrize(
    "setup,expected",
    [
        (lambda conn: None, "no_dossier"),
        (lambda conn: write_dossier(conn, built_age_s=9_999), "no_dossier"),
        (lambda conn: write_dossier(conn, blockers=("honeypot",)), "honeypot"),
    ],
)
def test_every_rejection_path_writes_exactly_one_decision_row(tmp_db, solo_risk, setup, expected):
    setup(tmp_db)
    decision = engine.decide(make_signal(), tmp_db)
    rows = tmp_db.execute("SELECT action, blockers_json FROM decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == Action.SKIP.value
    assert expected in rows[0]["blockers_json"]
    assert decision.blockers


def test_a_skip_emits_a_decision_event(tmp_db, solo_risk):
    decision = engine.decide(make_signal(), tmp_db)
    row = tmp_db.execute("SELECT payload FROM events WHERE kind='decision'").fetchone()
    assert decision.decision_id in row["payload"]
    assert "no_dossier" in row["payload"]


def test_re_deciding_a_signal_updates_the_same_row(tmp_db, solo_risk):
    write_dossier(tmp_db)
    signal = make_signal()
    first = engine.decide(signal, tmp_db)
    second = engine.decide(signal, tmp_db)
    assert first.decision_id == second.decision_id
    assert tmp_db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


# ============================================================ run_once and handoff


def test_run_once_decides_new_signals_and_advances_the_watermark(tmp_db, solo_risk):
    write_dossier(tmp_db)
    make_signal(tmp_db, created_ms=now_ms() - 2_000)
    decisions = engine.run_once(tmp_db)
    assert len(decisions) == 1
    assert engine.run_once(tmp_db) == []


def test_run_once_fills_a_shadow_entry_on_paper_and_links_the_outcome(tmp_db, solo_risk):
    write_dossier(tmp_db, price="0.001", liquidity="50000")
    seed_protection_evidence(tmp_db, TOKEN)
    make_signal(tmp_db)
    [decision] = engine.run_once(tmp_db)
    assert decision.action is Action.ENTER and decision.mode is LaneMode.SHADOW
    position = tmp_db.execute("SELECT * FROM positions").fetchone()
    assert position is not None and int(position["qty"]) > 0
    link = tmp_db.execute("SELECT * FROM decision_outcomes WHERE decision_id=?",
                          (decision.decision_id,)).fetchone()
    assert link["outcome"] == "open" and link["position_id"] == position["position_id"]


def test_a_live_entry_only_leaves_a_planned_order_for_the_executor(tmp_db, monkeypatch, solo_risk):
    write_dossier(tmp_db)
    seed_protection_evidence(tmp_db, TOKEN)

    # A planned live order now carries a real floor (engine._plan_min_out, 2026-09-21), so
    # the evidence that floor is derived from has to be on the books: chain-verified token
    # decimals and a contemporaneous native/USD sample. Without them the engine REFUSES to
    # plan the order at all -- which is the correct new behaviour and is pinned in
    # tests/test_engine_min_out.py. This test is about the handoff shape (a live lane
    # writes a PLANNED row and opens no position), so it seeds the evidence rather than
    # asserting the old floorless behaviour.
    from decimal import Decimal

    from kaiba.core.schemas import EvidenceBasis, Receipt, now_ms
    from kaiba.execution import fills
    from kaiba.providers import native_price

    fills._store_decimals(tmp_db, Chain.SOL, TOKEN, 6, register=True)
    native_price.record(
        native_price.NativeSample(
            chain=Chain.SOL, ts_ms=now_ms(), price_usd=Decimal("100"), source="fixture",
            pair="FIXTURE/USDC", liquidity_usd=Decimal(50_000_000),
            receipt=Receipt(provider="fixture", endpoint="native",
                            basis=EvidenceBasis.PROVIDER_REPORTED),
        ),
        tmp_db,
    )
    monkeypatch.setattr(native_price, "ensure_recent", lambda *a, **k: None)

    cfg = load_risk()
    lane_cfg = cfg.lane(Lane.CONFLUENCE_5).model_copy(update={"mode": LaneMode.LIVE})
    cfg = cfg.model_copy(update={
        "global_mode": LaneMode.LIVE,
        "bounds": cfg.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
        "lanes": {**cfg.lanes, Lane.CONFLUENCE_5: lane_cfg},
    })
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    make_signal(tmp_db)
    [decision] = engine.run_once(tmp_db)
    assert decision.mode is LaneMode.LIVE and decision.action is Action.ENTER
    order = tmp_db.execute("SELECT * FROM orders").fetchone()
    assert order["state"] == OrderState.PLANNED.value
    assert order["provider"] == "gmgn"  # the executor routes on this, and never sends a PLANNED row
    assert int(order["min_out"]) > 0, "the executor must never be handed a floorless order"
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_handoff_refuses_to_invent_a_fill_without_a_quote(tmp_db, solo_risk):
    write_dossier(tmp_db, price=None, liquidity=None, grade=Grade.B)
    signal = make_signal(tmp_db)
    decision = engine.decide(signal, tmp_db)
    assert engine.handoff(decision, signal, tmp_db) is None
    link = tmp_db.execute("SELECT outcome FROM decision_outcomes").fetchone()
    assert link["outcome"] == "abandoned"
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_mark_outcome_merges_rather_than_clobbers(tmp_db):
    engine.mark_outcome("dec_x", position_id="pos_1", outcome="open", conn=tmp_db)
    engine.mark_outcome("dec_x", trade_id="trd_1", outcome="closed", pnl_native=-42, conn=tmp_db)
    row = tmp_db.execute("SELECT * FROM decision_outcomes WHERE decision_id='dec_x'").fetchone()
    assert row["position_id"] == "pos_1"
    assert row["trade_id"] == "trd_1"
    assert row["outcome"] == "closed"
    assert int(row["pnl_native"]) == -42


def test_a_missing_risk_gate_refuses_the_entry(tmp_db, monkeypatch):
    """No gate means nothing is enforcing size, exposure or the daily loss stop.

    The engine used to treat an absent or broken gate as no objection. Combined with a
    constructor shim that bound a database connection where a risk provider belonged,
    that meant every entry in the system passed a risk check that never executed.
    """
    write_dossier(tmp_db)
    monkeypatch.setattr(engine, "RiskGate", None)
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert "risk_gate_unavailable" in decision.blockers


def test_a_raising_risk_gate_refuses_the_entry(tmp_db, monkeypatch):
    """A gate that throws is a broken gate, not a permissive one."""

    class BrokenGate:
        def check_entry(self, **kwargs):
            raise RuntimeError("boom")

    write_dossier(tmp_db)
    monkeypatch.setattr(engine, "RiskGate", BrokenGate)
    decision = engine.decide(make_signal(), tmp_db)
    assert decision.action is Action.SKIP
    assert any("risk_gate_error" in b for b in decision.blockers)


def test_the_real_gate_is_reachable_from_decide(tmp_db, monkeypatch):
    """Regression for the constructor shim: the real gate must actually be called."""
    seen = []

    class SpyGate:
        def check_entry(self, **kwargs):
            seen.append(kwargs)
            return None

    write_dossier(tmp_db)
    monkeypatch.setattr(engine, "RiskGate", SpyGate)
    monkeypatch.setattr(engine, "position_size", lambda chain, lane, score, conn=None: ONE_SOL)
    engine.decide(make_signal(), tmp_db)
    assert len(seen) == 1
    assert set(seen[0]) == {"chain", "lane", "size_base_units", "conn", "token"}


# ============================================================ curve-priced fills
#
# Everything above this line is the pool path, which was the only path there was. It was
# also the reason nothing had ever traded: the whole candidate stream is pre-graduation
# pump.fun launches, those have no DEX pair, and so all three paper orders this system
# had ever written failed with `no_price`. What follows is the curve path, and the thing
# it must never do is fill more generously than the pool path would have.


CURVE_TOKEN = "3GzeyKuMi3QNuQbSWcxASLx3kxmgiYxxYZyFS2SXpump"
CURVE_FIXTURES = Path(__file__).parent / "fixtures" / "curve_price"


def live_curve(which: str = "classic_85_sol") -> curve_price.CurveState:
    """A real pump.fun curve recorded from mainnet on 2026-09-20."""
    block = json.loads((CURVE_FIXTURES / "curve_geometry.json").read_text(encoding="utf-8"))[which]
    state, note = curve_price.CurveState.build(
        virtual_sol=int(block["virtual_sol_lamports"]),
        virtual_token=int(block["virtual_token_atoms"]),
        real_sol=int(block["real_sol_lamports"]),
        real_token=int(block["real_token_atoms"]),
        observed_ms=int(block["observed_ms"]),
    )
    assert state is not None, note
    return state


@pytest.fixture
def curve_broker(tmp_db):
    """No slippage floor and no latency, so the curve arithmetic can be asserted exactly."""
    return PaperBroker(
        tmp_db,
        native_usd={Chain.SOL: Decimal("108.89")},
        slippage_floor_bps=0,
        slippage_bps=5000,
        latency_ms=0,
        drift_mode=curve_price.DriftMode.NONE,
    )


def seed_curve_token(conn, token: str = CURVE_TOKEN, *, migrated_ms: int | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms, migrated_ms) "
        "VALUES (?,?,?,?,?)",
        (Chain.SOL.value, token, 6, NOW - 600_000, migrated_ms),
    )


def test_a_token_on_its_curve_fills_where_it_used_to_fail_with_no_price(curve_broker, tmp_db):
    """The single fix this whole module exists for.

    Same order, same broker: with only a DEX quote it is ``no_price`` and always was,
    because DexScreener has no pair for a token that has none. With the curve it fills,
    and the fill is exact rather than invented.
    """
    seed_curve_token(tmp_db)
    decision = make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10)

    refused = curve_broker.buy(decision, price_usd=None, liquidity_usd=None, now_ms=NOW, decimals=6)
    assert refused.state is OrderState.FAILED and refused.error == "no_price"

    filled = curve_broker.buy(
        make_decision(decision_id="dec_curve", token=CURVE_TOKEN, size=ONE_SOL // 10),
        curve=live_curve(), now_ms=NOW, decimals=6,
    )
    assert filled.state is OrderState.FILLED
    assert filled.filled_out > 0
    position = load_position(tmp_db, _position_of(tmp_db, filled.order_id))
    assert position.qty == filled.filled_out
    assert position.entry_price_usd > 0


def test_a_curve_fill_matches_the_curve_arithmetic_to_the_atom(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    budget = ONE_SOL // 10
    order = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=budget), curve=state, now_ms=NOW, decimals=6
    )
    # The fee is three separately rounded-up components, verified against mainnet, so the
    # leg is the exact inverse of that rather than a single division.
    curve_in = curve_price.curve_leg_for_budget(budget)
    fee, _protocol, _creator = curve_price.platform_fee(curve_in)
    expected, _, _ = state.buy_exact_in(curve_in)
    assert order.filled_out == expected
    assert order.fee_native == fee + 1_000_000  # venue fee + the tip
    assert curve_in + fee <= budget


def test_the_price_basis_of_every_fill_is_recorded_and_readable(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    order = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=live_curve(),
        now_ms=NOW, decimals=6,
    )
    basis = fill_basis(tmp_db, order.order_id)
    assert basis["basis"] == "curve"
    assert basis["venue"] == "pumpfun-curve"
    assert basis["fee_bps"] == 125
    assert int(basis["impact_bps"]) > 0
    assert any("adverse_selection" in note for note in basis["not_modelled"])

    event = tmp_db.execute(
        "SELECT payload FROM events WHERE kind='order.filled' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(event["payload"])["fill_basis"] == "curve"


def test_a_refusal_records_why_neither_price_source_worked(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    order = curve_broker.buy(make_decision(token=CURVE_TOKEN), now_ms=NOW, decimals=6)
    assert order.error == "no_price"
    basis = fill_basis(tmp_db, order.order_id)
    assert basis["basis"] == "refused"
    assert basis["curve_note"] == "no_curve_snapshot"


def test_a_graduated_token_prices_on_the_pool_and_says_so(curve_broker, tmp_db):
    """After migration the curve is gone, the pool quote is right, and the fee is PumpSwap's."""
    seed_curve_token(tmp_db, migrated_ms=NOW - 60_000)
    order = curve_broker.buy(
        make_decision(token=CURVE_TOKEN), price_usd=Decimal("0.001"),
        liquidity_usd=Decimal("50000"), now_ms=NOW, decimals=6,
    )
    assert order.state is OrderState.FILLED
    basis = fill_basis(tmp_db, order.order_id)
    assert basis["basis"] == "dex"
    assert basis["venue"] == "pumpswap-tier1"
    assert basis["fee_bps"] == 120  # tier 1, the expensive end of the 25-tier schedule
    assert basis["curve_note"] == "graduated"


def test_a_migrated_token_never_uses_a_stale_curve_snapshot(curve_broker, tmp_db):
    """``migrated_ms`` wins over any reserves we still happen to hold. The curve is gone."""
    seed_curve_token(tmp_db, migrated_ms=NOW - 60_000)
    state, note = curve_broker.resolve_curve(Chain.SOL, CURVE_TOKEN, NOW)
    assert state is None and note == "graduated"


def test_the_broker_finds_a_fresh_snapshot_by_itself(tmp_db):
    """No ``curve=`` argument: the broker resolves the curve out of ``curve_snapshots``."""
    seed_curve_token(tmp_db)
    state = live_curve()
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) VALUES (?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, CURVE_TOKEN, NOW - 5_000, state.real_sol, state.virtual_sol,
         str(state.real_token), str(state.virtual_token), "pumpfun"),
    )
    broker = PaperBroker(tmp_db, native_usd={Chain.SOL: Decimal("108.89")},
                         slippage_floor_bps=0, slippage_bps=5000, latency_ms=0,
                         drift_mode=curve_price.DriftMode.NONE)
    order = broker.buy(make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), now_ms=NOW, decimals=6)
    assert order.state is OrderState.FILLED
    assert fill_basis(tmp_db, order.order_id)["basis"] == "curve"


def test_a_stale_snapshot_is_not_a_price(tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    tmp_db.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) VALUES (?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, CURVE_TOKEN, NOW - 3_600_000, state.real_sol, state.virtual_sol,
         str(state.real_token), str(state.virtual_token), "pumpfun"),
    )
    broker = PaperBroker(tmp_db, native_usd={Chain.SOL: Decimal("108.89")})
    order = broker.buy(make_decision(token=CURVE_TOKEN), now_ms=NOW, decimals=6)
    assert order.error == "no_price"
    assert fill_basis(tmp_db, order.order_id)["curve_note"].startswith("curve_snapshot_stale")


def test_a_curve_round_trip_closes_a_trade_with_realised_pnl(curve_broker, tmp_db):
    """The output the whole validation harness is waiting for: a closed ``trades`` row."""
    seed_curve_token(tmp_db)
    state = live_curve()
    buy = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=state, now_ms=NOW, decimals=6
    )
    assert buy.state is OrderState.FILLED
    position = load_position(tmp_db, _position_of(tmp_db, buy.order_id))

    after_buy = state.buy_exact_in(int(buy.amount_in) * 10_000 // 10_125)[1]
    sell = curve_broker.sell(
        position, 100, curve=after_buy, now_ms=NOW + 90_000, decimals=6, exit_reason="trail_stop"
    )
    assert sell.state is OrderState.FILLED

    trade = tmp_db.execute("SELECT * FROM trades").fetchone()
    assert trade is not None
    assert trade["exit_reason"] == "trail_stop"
    assert trade["hold_s"] == 90
    assert int(trade["proceeds_native"]) - int(trade["cost_native"]) == int(trade["pnl_native"])
    assert int(trade["fees_native"]) == buy.fee_native + sell.fee_native
    # Round-tripping a curve immediately is a guaranteed loss: two 125 bps legs, our own
    # impact both ways, and two tips. A paper broker that showed a profit here is broken.
    assert int(trade["pnl_native"]) < 0
    assert load_position(tmp_db, position.position_id).closed_ms == NOW + 90_000


def test_a_curve_exit_records_its_own_basis(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    buy = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=state, now_ms=NOW, decimals=6
    )
    position = load_position(tmp_db, _position_of(tmp_db, buy.order_id))
    sell = curve_broker.sell(position, 100, curve=state, now_ms=NOW + 1_000, decimals=6)
    basis = fill_basis(tmp_db, sell.order_id)
    assert basis["basis"] == "curve" and basis["side"] == "sell"
    assert int(basis["impact_bps"]) < 0  # a sell moves the price down, and we say so


def test_a_partial_curve_exit_leaves_the_position_open(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    buy = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=state, now_ms=NOW, decimals=6
    )
    position = load_position(tmp_db, _position_of(tmp_db, buy.order_id))
    curve_broker.sell(position, 50, curve=state, now_ms=NOW + 1_000, decimals=6)
    still_open = load_position(tmp_db, position.position_id)
    assert still_open.closed_ms is None
    assert still_open.qty == buy.filled_out - buy.filled_out // 2
    assert tmp_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_latency_makes_the_modelled_entry_worse_not_better(tmp_db):
    """The delay is charged, and it is charged in the direction that costs us."""
    seed_curve_token(tmp_db)
    state = live_curve()
    base = NOW
    for i, (ts, side, lamports) in enumerate(
        [(base - 60_000, "buy", 200_000_000), (base - 30_000, "buy", 200_000_000),
         (base - 1_000, "buy", 200_000_000)]
    ):
        tmp_db.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"tx{i}", ts, f"w{i}", CURVE_TOKEN, side, "1000",
             str(lamports), "fixture"),
        )

    def broker(latency_ms, mode):
        return PaperBroker(tmp_db, native_usd={Chain.SOL: Decimal("108.89")},
                           slippage_floor_bps=0, slippage_bps=5000,
                           latency_ms=latency_ms, drift_mode=mode)

    instant = broker(0, curve_price.DriftMode.NONE).buy(
        make_decision(decision_id="dec_i", token=CURVE_TOKEN, size=ONE_SOL // 10),
        curve=state, now_ms=base, decimals=6,
    )
    delayed = broker(7_600, curve_price.DriftMode.ADVERSE).buy(
        make_decision(decision_id="dec_d", token=CURVE_TOKEN, size=ONE_SOL // 10),
        curve=state, now_ms=base, decimals=6,
    )
    assert delayed.filled_out < instant.filled_out
    assert int(fill_basis(tmp_db, delayed.order_id)["latency_bps"]) > 0
    assert fill_basis(tmp_db, delayed.order_id)["drift_basis"] == "adverse:swaps"
    assert fill_basis(tmp_db, instant.order_id)["latency_bps"] == 0


def test_an_entry_that_cannot_clear_its_own_tolerance_is_refused(tmp_db):
    """A 50 SOL order into a curve holding 0.31 SOL is not a fill, it is a fantasy."""
    seed_curve_token(tmp_db)
    broker = PaperBroker(tmp_db, native_usd={Chain.SOL: Decimal("108.89")},
                         slippage_floor_bps=0, latency_ms=0,
                         drift_mode=curve_price.DriftMode.NONE)
    order = broker.buy(
        make_decision(token=CURVE_TOKEN, size=50 * ONE_SOL), curve=live_curve(),
        now_ms=NOW, decimals=6, slippage_bps=500,
    )
    assert order.state is OrderState.FAILED
    assert order.error.startswith("slippage_exceeded")
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_the_default_solana_fee_is_no_longer_flattering():
    """It was 100 bps. pump.fun's curve is 125 and PumpSwap tier 1 is 120."""
    assert DEFAULT_FEE_BPS[Chain.SOL] == 125
    assert curve_price.CURVE_TOTAL_FEE_BPS == 125
    assert curve_price.PUMPSWAP_TIER1_TOTAL_FEE_BPS == 120


def test_a_curve_fill_is_deterministic(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    a = curve_broker.buy(make_decision(decision_id="dec_a", token=CURVE_TOKEN, size=ONE_SOL // 10),
                         curve=state, now_ms=NOW, decimals=6)
    tmp_db.execute("DELETE FROM positions")
    b = curve_broker.buy(make_decision(decision_id="dec_b", token=CURVE_TOKEN, size=ONE_SOL // 10),
                         curve=state, now_ms=NOW, decimals=6)
    assert a.filled_out == b.filled_out and a.fee_native == b.fee_native


# ============================================================ the loop, with GMGN absent
#
# GMGN Free cannot execute: the window weight is 5 and a quote or a swap costs 10, so
# there is no plan under which it fills an order, and GMGN Plus is not something the
# operator can buy today. Paper is therefore the only measurement this system will have
# for a while, and it has to stand on its own rather than merely happen not to call GMGN
# this week. These tests make every GMGN entry point raise and then drive the whole loop
# through it: signal, entry fill, marks while held, exit fill, closed trade with PnL.


#: GMGN entry points that would mean the paper loop actually *depends* on GMGN. A hit on
#: either is a fail: ``run_read`` is the chokepoint every typed helper (including
#: ``order_quote``) goes through, and ``_spawn`` is the process launch under it.
GMGN_LOAD_BEARING = ("run_read", "_spawn")

#: ``cli_argv`` is not load-bearing. It answers "is the wrapper installed?" and
#: ``standing.py`` reads it behind a try/except to set ``cli_present=False``. Breaking it
#: is how we *prove* that degradation is real rather than assumed, so a hit here is
#: expected — what matters is that the round trip still completes afterwards.
GMGN_CAPABILITY_PROBE = ("cli_argv",)


@pytest.fixture
def no_gmgn(monkeypatch):
    """Make every route into GMGN explode, so a silent dependency cannot survive.

    All three are broken, not just the two that matter, because a future edit could route
    a price through any of them and the test has to notice.
    """
    from kaiba.providers import gmgn_cli

    reached: list[str] = []

    def explode(name):
        def boom(*args, **kwargs):
            reached.append(name)
            raise RuntimeError(f"GMGN is absent ({name} unavailable)")

        return boom

    for name in (*GMGN_LOAD_BEARING, *GMGN_CAPABILITY_PROBE):
        monkeypatch.setattr(gmgn_cli, name, explode(name))
    monkeypatch.delenv("GMGN_API_KEY", raising=False)
    monkeypatch.delenv("GMGN_CLI", raising=False)
    return reached


def risen_curve(raised_sol: int) -> curve_price.CurveState:
    """The recorded classic curve, moved along itself to ``raised_sol`` SOL raised.

    ``advance`` walks the real curve rather than inventing one, so this is a genuine
    point on ``3Gze…pump``'s own geometry: same ``k``, same 30 SOL starting point, same
    reserved-token invariant. A live launch at 1.4% progress cannot fall 30% — there is
    only 2% of curve beneath it — so a stop-loss test needs a curve that has actually
    run, and this is how to get one without fabricating reserves.
    """
    base = live_curve()
    return base.advance(raised_sol * curve_price.LAMPORTS_PER_SOL - base.real_sol)


def put_snapshot(conn, state: curve_price.CurveState, *, at_ms: int, token: str = CURVE_TOKEN) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) VALUES (?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, token, at_ms, state.real_sol, state.virtual_sol,
         str(state.real_token), str(state.virtual_token), "pumpfun"),
    )


def test_the_whole_paper_loop_closes_a_trade_with_gmgn_absent(tmp_db, solo_risk, no_gmgn):
    """Signal in, fill, marks, stop, closed trade — and not one GMGN call anywhere.

    This is the loop the Phase 4 harness has been waiting on, run end to end offline.
    The only network-shaped things in it are the stored curve snapshots, which came from
    pump.fun's free keyless coin route.
    """
    from kaiba.execution import watchdog as wd

    ts = now_ms()
    seed_curve_token(tmp_db)
    entry_curve = risen_curve(50)
    put_snapshot(tmp_db, entry_curve, at_ms=ts - 2_000)

    # The dossier's quote comes from rugcheck, which does price curve tokens. The broker
    # must ignore it in favour of the curve, and the recorded basis is how we know it did.
    write_dossier(tmp_db, token=CURVE_TOKEN, price="0.00004", liquidity="9000")
    make_signal(tmp_db, token=CURVE_TOKEN)

    [decision] = engine.run_once(tmp_db)
    assert decision.action is Action.ENTER and decision.token == CURVE_TOKEN
    position_row = tmp_db.execute("SELECT * FROM positions").fetchone()
    assert position_row is not None and int(position_row["qty"]) > 0
    buy_order = tmp_db.execute("SELECT * FROM orders WHERE side='buy'").fetchone()
    assert fill_basis(tmp_db, buy_order["order_id"])["basis"] == "curve"

    source = curve_price.CurvePriceSource(
        tmp_db, sol_usd=Decimal("200"), resolver=curve_price.snapshot_resolver(tmp_db)
    )
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))

    # Ticks while held: the price is visible, the stop is armed, nothing sells yet. Before
    # this module existed every one of these ticks was a `protection_blind` error, because
    # the only quote stack was DexScreener and the token has no pair.
    for drift in (0, 1):
        put_snapshot(tmp_db, risen_curve(50 - drift), at_ms=now_ms())
        report = dog.tick()
        assert report.blind == 0, "a curve token must never be blind to the watchdog again"
        assert report.exits == 0
    state = tmp_db.execute(
        "SELECT * FROM watchdog_state WHERE position_id=?", (position_row["position_id"],)
    ).fetchone()
    assert state is not None and state["stop_price_usd"] is not None
    assert Decimal(state["peak_price_usd"]) > 0

    # The curve collapses back to 10 SOL raised: a 75% drawdown, well past the 30% stop.
    put_snapshot(tmp_db, risen_curve(10), at_ms=now_ms())
    report = dog.tick()
    assert report.exits == 1, report

    trade = tmp_db.execute("SELECT * FROM trades").fetchone()
    assert trade is not None, "the watchdog decided an exit and the broker must have closed it"
    assert int(trade["pnl_native"]) < 0
    assert trade["pnl_pct"] < 0
    assert int(trade["proceeds_native"]) - int(trade["cost_native"]) == int(trade["pnl_native"])
    assert int(trade["fees_native"]) > 0
    assert trade["mae_pct"] < 0

    sell_order = tmp_db.execute("SELECT * FROM orders WHERE side='sell'").fetchone()
    assert fill_basis(tmp_db, sell_order["order_id"])["basis"] == "curve"

    # Nothing that could price or execute went near GMGN. The only GMGN entry point the
    # loop touched at all was the "is the wrapper installed?" probe in standing.py, which
    # is supposed to fail here and is supposed to change nothing when it does.
    assert [n for n in no_gmgn if n in GMGN_LOAD_BEARING] == []
    assert set(no_gmgn) <= set(GMGN_CAPABILITY_PROBE)


def test_the_curve_price_source_needs_no_gmgn_and_no_key(tmp_db, no_gmgn):
    seed_curve_token(tmp_db)
    put_snapshot(tmp_db, live_curve(), at_ms=now_ms())
    source = curve_price.CurvePriceSource(tmp_db, sol_usd=Decimal("200"))
    quote = source.quote(Chain.SOL, CURVE_TOKEN)
    assert quote.usable and quote.price_usd > 0
    assert no_gmgn == []  # pricing a curve does not touch GMGN at all, not even the probe


def test_a_position_priced_two_ways_records_the_handover(curve_broker, tmp_db):
    """Migration is a first-class event, not a quiet fallback.

    A position opened on the curve and closed on the pool has an entry and an exit that
    did not come from the same source. A shadow record that does not say so invites an
    analysis to compare them as though they did.
    """
    seed_curve_token(tmp_db)
    buy = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=live_curve(),
        now_ms=NOW, decimals=6,
    )
    position = load_position(tmp_db, _position_of(tmp_db, buy.order_id))

    # It graduates mid-trade. The curve is gone; the pool quote is now the right answer.
    tmp_db.execute(
        "UPDATE tokens SET migrated_ms=? WHERE address=?", (NOW + 30_000, CURVE_TOKEN)
    )
    sell = curve_broker.sell(
        position, 100, price_usd=Decimal("0.00004"), liquidity_usd=Decimal("80000"),
        now_ms=NOW + 60_000, decimals=6, exit_reason="migration_fade",
    )
    assert sell.state is OrderState.FILLED

    basis = fill_basis(tmp_db, sell.order_id)
    assert basis["basis"] == "dex"
    assert basis["basis_changed"] == {"from": "curve", "to": "dex"}
    changed = [
        json.loads(r["payload"])
        for r in tmp_db.execute("SELECT payload FROM events WHERE kind='system'")
        if json.loads(r["payload"]).get("event") == "fill_basis_changed"
    ]
    assert changed and changed[0]["from"] == "curve" and changed[0]["to"] == "dex"


def test_a_position_that_never_changes_basis_says_nothing(curve_broker, tmp_db):
    seed_curve_token(tmp_db)
    state = live_curve()
    buy = curve_broker.buy(
        make_decision(token=CURVE_TOKEN, size=ONE_SOL // 10), curve=state, now_ms=NOW, decimals=6
    )
    position = load_position(tmp_db, _position_of(tmp_db, buy.order_id))
    sell = curve_broker.sell(position, 100, curve=state, now_ms=NOW + 1_000, decimals=6)
    assert "basis_changed" not in fill_basis(tmp_db, sell.order_id)


def test_an_injected_executable_quote_replaces_the_depth_approximation(curve_broker, tmp_db):
    """A venue saying what it would give us beats any model of what it might.

    Jupiter's quote route is free and keyless and prices our exact size. When one is
    handed in, the pool path uses it and reports the gap to the mid as the impact rather
    than charging its own approximation on top, which would double-count.
    """
    seed_curve_token(tmp_db, migrated_ms=NOW - 60_000)
    modelled = curve_broker.buy(
        make_decision(decision_id="dec_model", token=CURVE_TOKEN),
        price_usd=Decimal("0.001"), liquidity_usd=Decimal("50000"), now_ms=NOW, decimals=6,
    )
    tmp_db.execute("DELETE FROM positions")
    routed = curve_broker.buy(
        make_decision(decision_id="dec_routed", token=CURVE_TOKEN),
        price_usd=Decimal("0.001"), liquidity_usd=Decimal("50000"),
        executable_price_usd=Decimal("0.00104"), now_ms=NOW, decimals=6,
    )
    modelled_basis = fill_basis(tmp_db, modelled.order_id)
    assert modelled_basis["impact_basis"] == "constant_product_approximation"
    assert modelled_basis["basis"] == "dex"
    routed_basis = fill_basis(tmp_db, routed.order_id)
    assert routed_basis["impact_basis"] == "executable_quote"
    # A routed fill is its own basis, so a later audit can compare the three sources
    # against each other instead of finding them all logged as "dex".
    assert routed_basis["basis"] == "router"
    assert routed_basis["impact_bps"] == 400  # 0.00104 against a 0.001 mid
    assert routed.filled_out < modelled.filled_out
