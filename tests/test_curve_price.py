"""Bonding-curve pricing: exactness first, honesty second.

The reason this module exists is that every paper order this system had ever written
failed with ``no_price``, because the broker priced from a DEX quote and a token on its
bonding curve has no DexScreener pair row. (It is *not* true that no router can price
one — Jupiter routes pump.fun curves directly, and ``test_curve_arithmetic_reproduces_
jupiters_executable_quote_to_the_atom`` uses that as an independent oracle.) So the first
thing these tests defend is that the
replacement is **exact** rather than merely available: the replay tests take 12 recorded
``curve_snapshots`` windows across 8 mainnet tokens, and the 21 real trades inside them,
and require the model to land on the next snapshot's reserves to the atom and to
reproduce every real ``amount_token`` and ``amount_native``. A pricing model that is
approximately right is worse than none here, because it produces a track record that
cannot be reproduced live.

Where the model and mainnet differ, the tests pin the difference rather than widening a
tolerance until it passes: all 13 sells are exact, 3 of 8 buys are exact, and each of the
other 5 is shown to be one lamport of pump.fun's own rounding that the trader's router
paid and got nothing for — 0.022 bps at worst, and in our favour, which is stated.

The second thing they defend is that the model is never *flattering*. A paper fill that
beats a live one is the most dangerous output this repository can produce, so there are
explicit tests that the fee comes off the top, that rounding goes against us, that the
latency drift moves the price the wrong way for us on both sides, and that an unpriceable
fill is still refused.

Fixtures under ``tests/fixtures/curve_price/`` are recorded from this machine's own
database on 2026-09-20. Nothing here is synthetic except the deliberately broken payloads.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.schemas import Chain, Side, now_ms
from kaiba.execution import curve_price as cp

FIXTURES = Path(__file__).parent / "fixtures" / "curve_price"
TOKEN = "3GzeyKuMi3QNuQbSWcxASLx3kxmgiYxxYZyFS2SXpump"
SOL_USD = Decimal("108.89")


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def state_from(block: dict[str, Any]) -> cp.CurveState:
    state, note = cp.CurveState.build(
        virtual_sol=int(block["virtual_sol_lamports"]),
        virtual_token=int(block["virtual_token_atoms"]),
        real_sol=int(block["real_sol_lamports"]),
        real_token=int(block["real_token_atoms"]),
        observed_ms=int(block["observed_ms"]),
    )
    assert state is not None, note
    return state


@pytest.fixture
def classic() -> cp.CurveState:
    return state_from(load("curve_geometry.json")["classic_85_sol"])


@pytest.fixture
def low_cap() -> cp.CurveState:
    return state_from(load("curve_geometry.json")["low_cap"])


# ============================================================ the exactness proof


#: Replaying a recorded window must land on the next recorded snapshot exactly. Anything
#: less and the model is not reproducing mainnet.
def _replay(window: dict[str, Any]) -> tuple[cp.CurveState, list[tuple[str, int, int]]]:
    """Walk a window's real trades. Returns the end state and every (side, model, actual)."""
    state = state_from(window["start"])
    seen: list[tuple[str, int, int]] = []
    for trade in window["trades"]:
        atoms, lamports = int(trade["amount_token"]), int(trade["amount_native"])
        if trade["side"] == "buy":
            model, _, capped = state.buy_exact_in(lamports)
            seen.append(("buy", model, atoms))
        else:
            model, _, capped = state.sell_exact_in(atoms)
            seen.append(("sell", model, lamports))
        assert not capped
        # Advance on what *actually* happened, so one modelled trade cannot drag the rest.
        if trade["side"] == "buy":
            state = cp.CurveState(
                virtual_sol=state.virtual_sol + lamports,
                virtual_token=state.virtual_token - atoms,
                real_sol=state.real_sol + lamports,
                real_token=state.real_token - atoms,
            )
        else:
            state = cp.CurveState(
                virtual_sol=state.virtual_sol - lamports,
                virtual_token=state.virtual_token + atoms,
                real_sol=state.real_sol - lamports,
                real_token=state.real_token + atoms,
            )
    return state, seen


def test_replaying_real_trades_lands_exactly_on_the_next_recorded_snapshot():
    """Every recorded window, walked trade by trade, must end on the observed reserves.

    This is the ground truth the whole module rests on. These are mainnet trades: the
    window selection in the fixture only keeps pairs whose swap rows reconcile exactly
    with the reserve deltas, so a failure here is arithmetic, never coverage.
    """
    doc = load("real_trade_replay.json")
    assert len(doc["windows"]) == 12
    for window in doc["windows"]:
        state, _ = _replay(window)
        end = window["end"]
        assert state.virtual_sol == int(end["virtual_sol_lamports"]), window["token"]
        assert state.virtual_token == int(end["virtual_token_atoms"]), window["token"]
        assert state.real_sol == int(end["real_sol_lamports"]), window["token"]
        assert state.real_token == int(end["real_token_atoms"]), window["token"]


def test_every_real_sell_is_reproduced_to_the_lamport():
    """13 of 13. The sell side has no router rounding to hide behind."""
    doc = load("real_trade_replay.json")
    sells = [s for w in doc["windows"] for s in _replay(w)[1] if s[0] == "sell"]
    assert len(sells) == 13
    assert all(model == actual for _, model, actual in sells)


def test_real_buys_are_reproduced_or_beaten_by_under_a_basis_point():
    """8 real buys: 3 exact, 5 where the trader's own router left a sliver on the table.

    The model must never be *worse* than reality by more than rounding, and where it is
    better the margin must be negligible. It is: the worst case is 2.21e-06 relative,
    which is 0.022 bps, against a 125 bps fee.
    """
    doc = load("real_trade_replay.json")
    buys = [s for w in doc["windows"] for s in _replay(w)[1] if s[0] == "buy"]
    assert len(buys) == 8
    exact = [b for b in buys if b[1] == b[2]]
    assert len(exact) == 3
    for _, model, actual in buys:
        assert model >= actual, "the model must never under-deliver against a real fill"
        assert (model - actual) / actual < Decimal("0.000003")


def test_every_buy_the_model_beats_is_explained_by_one_lamport_of_program_rounding():
    """Not a modelling error: the trader paid one lamport that bought nothing.

    pump.fun's ``buy`` takes a token amount and computes the SOL, adding one lamport as
    it rounds the reserve up. A router that asks for a round token amount therefore pays
    ``cost + 1``. Our model sees the full amount paid and returns what it would buy.
    """
    doc = load("real_trade_replay.json")
    for window in doc["windows"]:
        state = state_from(window["start"])
        for trade in window["trades"]:
            atoms, lamports = int(trade["amount_token"]), int(trade["amount_native"])
            if trade["side"] == "buy":
                model, _, _ = state.buy_exact_in(lamports)
                if model != atoms:
                    assert state.sol_cost_for_tokens(atoms) == lamports - 1
                state = cp.CurveState(
                    virtual_sol=state.virtual_sol + lamports,
                    virtual_token=state.virtual_token - atoms,
                    real_sol=state.real_sol + lamports,
                    real_token=state.real_token - atoms,
                )
            else:
                state = cp.CurveState(
                    virtual_sol=state.virtual_sol - lamports,
                    virtual_token=state.virtual_token + atoms,
                    real_sol=state.real_sol - lamports,
                    real_token=state.real_token + atoms,
                )


def test_buy_exact_in_is_the_exact_inverse_of_the_programs_cost_function(classic):
    """``buy_exact_in(B)`` is the largest token amount the program would sell for ``B``."""
    for budget in (1_000, 1_000_000, 50_000_000, 1_000_000_000):
        atoms, _, _ = classic.buy_exact_in(budget)
        assert classic.sol_cost_for_tokens(atoms) <= budget
        assert classic.sol_cost_for_tokens(atoms + 1) > budget


def test_the_reserved_token_invariant_holds_through_every_replayed_trade():
    doc = load("real_trade_replay.json")
    for window in doc["windows"]:
        state = state_from(window["start"])
        for trade in window["trades"]:
            if trade["side"] == "buy":
                _, state, _ = state.buy_exact_in(int(trade["amount_native"]))
            else:
                _, state, _ = state.sell_exact_in(int(trade["amount_token"]))
            assert state.virtual_token - state.real_token == cp.RESERVED_TOKEN_ATOMS


def test_the_protocol_rounding_is_reproduced_and_costs_the_buyer(classic):
    """``k`` must creep *up*, never stay level. That creep is the trader's loss."""
    before = classic.k
    state = classic
    for _ in range(5):
        _, state, _ = state.buy_exact_in(100_000_000)
    assert state.k > before

    # The pure constant-product answer, without the program's `+ 1`, is one atom better.
    naive = classic.virtual_token - classic.k // (classic.virtual_sol + 100_000_000)
    exact, _, _ = classic.buy_exact_in(100_000_000)
    assert exact == naive - 1


# ============================================================ geometry is per launch


def test_reserved_token_atoms_matches_the_scanner():
    """One derivation in the tree. The constant is duplicated to break an import cycle."""
    from kaiba.execution import scanner

    assert cp.RESERVED_TOKEN_ATOMS == scanner.RESERVED_TOKEN_ATOMS


def test_derived_geometry_matches_the_scanners_on_the_same_reserves():
    """``CurveState`` and ``curve_from_payload`` must not drift apart on any real curve."""
    from kaiba.execution import scanner

    for block in (load("curve_geometry.json")["classic_85_sol"], load("curve_geometry.json")["low_cap"]):
        state = state_from(block)
        payload = {
            "complete": False,
            "quote_mint": "11111111111111111111111111111111",
            "quote_decimals": 9,
            "virtual_sol_reserves": state.virtual_sol,
            "real_sol_reserves": state.real_sol,
            "virtual_token_reserves": state.virtual_token,
            "real_token_reserves": state.real_token,
            "created_timestamp": block["created_ms"],
        }
        curve, _ = scanner.curve_from_payload(payload, at_ms=state.observed_ms)
        assert curve is not None
        assert state.graduation_sol == curve["graduation_sol"]
        assert state.progress_pct == curve["progress_pct"]
        assert state.sol_in_curve == curve["sol_in_curve"]


def test_two_live_curves_graduate_hundreds_of_times_apart(classic, low_cap):
    """The reason nothing here may hard-code 85 SOL: both of these were live at once."""
    assert Decimal("84") < classic.graduation_sol < Decimal("86")
    assert Decimal("0.10") < low_cap.graduation_sol < Decimal("0.12")
    assert classic.graduation_sol / low_cap.graduation_sol > 700


def test_a_curve_that_breaks_the_reserved_invariant_is_refused():
    state, note = cp.CurveState.build(
        virtual_sol=30_000_000_000,
        virtual_token=1_073_000_000_000_000,
        real_sol=0,
        real_token=1_073_000_000_000_000 - 279_900_000_000_001,  # one atom out
    )
    assert state is None and note.startswith("reserved_token_invariant_violated")


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"virtual_sol": 0}, "reserves_nonpositive"),
        ({"virtual_token": 0}, "reserves_nonpositive"),
        ({"real_sol": 30_000_000_000}, "sol_reserves_inconsistent"),
        ({"real_sol": -1}, "sol_reserves_inconsistent"),
        ({"complete": True}, "curve_complete"),
    ],
)
def test_a_payload_that_is_not_a_curve_is_refused_not_guessed(kwargs, expected):
    base = {
        "virtual_sol": 30_000_000_000,
        "virtual_token": 1_073_000_000_000_000,
        "real_sol": 0,
        "real_token": 793_100_000_000_000,
    }
    state, note = cp.CurveState.build(**{**base, **kwargs})
    assert state is None and note == expected


def test_a_graduated_payload_is_refused_because_the_curve_is_gone(classic):
    payload = {
        "complete": True,
        "quote_mint": "11111111111111111111111111111111",
        "quote_decimals": 9,
        "virtual_sol_reserves": classic.virtual_sol,
        "real_sol_reserves": classic.real_sol,
        "virtual_token_reserves": classic.virtual_token,
        "real_token_reserves": classic.real_token,
    }
    state, note = cp.CurveState.from_payload(payload)
    assert state is None and note == "curve_complete"


# ============================================================ quoting a fill


def test_a_buy_charges_the_fee_off_the_top_and_the_curve_gets_the_rest(classic):
    budget = 1_000_000_000  # 1 SOL
    fill = cp.quote_buy(
        classic, budget, sol_usd=SOL_USD, decimals=6, latency_ms=0, drift_mode=cp.DriftMode.NONE
    )
    assert fill.ok and fill.basis is cp.FillBasis.CURVE
    assert fill.curve_in + fill.fee_native == fill.amount_in <= budget
    assert fill.fee_bps == 125
    assert fill.fee_protocol_native + fill.fee_creator_native == fill.fee_native
    # 30 bps of the 125 goes to the creator, rounded up on its own.
    assert fill.fee_creator_native == -(-fill.curve_in * 30 // 10_000)
    # The leg is the largest one whose fee still fits the budget, and no larger: one more
    # lamport on the curve would take the venue total past what we were willing to spend.
    assert fill.curve_in == cp.curve_leg_for_budget(budget)
    bigger = fill.curve_in + 1
    assert bigger + cp.platform_fee(bigger)[0] > budget
    expected_out, _, _ = classic.buy_exact_in(fill.curve_in)
    assert fill.amount_out == expected_out


def test_a_sell_takes_the_fee_out_of_the_proceeds_and_rounds_it_up(classic):
    atoms = 1_000_000_000_000
    fill = cp.quote_sell(
        classic, atoms, sol_usd=SOL_USD, decimals=6, latency_ms=0, drift_mode=cp.DriftMode.NONE
    )
    assert fill.ok
    gross, _, _ = classic.sell_exact_in(atoms)
    assert fill.curve_out == gross
    assert fill.fee_native == (gross * 125 + 9_999) // 10_000
    assert fill.fee_native * 10_000 >= gross * 125  # rounded up, against us
    assert fill.amount_out == gross - fill.fee_native


def test_the_effective_price_is_worse_than_spot_on_a_buy_and_on_a_sell(classic):
    buy = cp.quote_buy(
        classic, 500_000_000, sol_usd=SOL_USD, decimals=6, latency_ms=0, drift_mode=cp.DriftMode.NONE
    )
    sell = cp.quote_sell(
        classic, 10_000_000_000_000, sol_usd=SOL_USD, decimals=6, latency_ms=0,
        drift_mode=cp.DriftMode.NONE,
    )
    spot = classic.spot_price_usd(SOL_USD, 6)
    assert buy.effective_price_usd > spot and buy.impact_bps > 0
    assert sell.effective_price_usd < spot and sell.impact_bps < 0


def test_impact_grows_with_size_and_is_exact_rather_than_approximated(classic):
    sizes = [10_000_000, 100_000_000, 1_000_000_000, 5_000_000_000]
    fills = [
        cp.quote_buy(classic, s, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                     drift_mode=cp.DriftMode.NONE)
        for s in sizes
    ]
    impacts = [f.impact_bps for f in fills]
    assert impacts == sorted(impacts)
    assert impacts[0] < impacts[-1]
    # The approximation the pool path uses, size/(depth+size), is not what this is: on a
    # curve the depth is virtual, not the SOL in the pool, so a 5 SOL order into a curve
    # holding 0.31 SOL is a few hundred bps, not 94%.
    assert impacts[-1] < 2_000


def test_a_round_trip_at_the_same_instant_loses_fees_and_impact(classic):
    """Buy then immediately sell everything back: we must be down, always."""
    budget = 200_000_000
    buy = cp.quote_buy(
        classic, budget, sol_usd=SOL_USD, decimals=6, latency_ms=0, drift_mode=cp.DriftMode.NONE
    )
    assert buy.ok and buy.state_after is not None
    sell = cp.quote_sell(
        buy.state_after, buy.amount_out, sol_usd=SOL_USD, decimals=6, latency_ms=0,
        drift_mode=cp.DriftMode.NONE,
    )
    assert sell.ok
    assert sell.amount_out < budget
    # The loss is at least both fee legs; impact is on top of that.
    assert budget - sell.amount_out >= buy.fee_native + sell.fee_native
    # 125 bps off the top plus 125 bps off the proceeds is ~247 bps before impact.
    assert sell.amount_out < budget * 9_755 // 10_000


def test_a_buy_that_would_cross_graduation_is_capped_at_the_reserves(low_cap):
    huge = 1_000 * cp.LAMPORTS_PER_SOL
    out, after, capped = low_cap.buy_exact_in(huge)
    assert capped
    assert out == low_cap.real_token
    assert after.real_token == 0


def test_a_fill_that_cannot_be_priced_is_refused_not_invented(classic):
    assert not cp.quote_buy(classic, 0, sol_usd=SOL_USD, decimals=6).ok
    assert cp.quote_buy(classic, 0, sol_usd=SOL_USD, decimals=6).reason == "no_size"
    no_native = cp.quote_buy(classic, 1_000_000, sol_usd=Decimal(0), decimals=6)
    assert not no_native.ok and no_native.reason == "no_native_usd_price"
    assert no_native.basis is cp.FillBasis.REFUSED
    assert not cp.quote_sell(classic, 0, sol_usd=SOL_USD, decimals=6).ok


def test_every_fill_carries_what_it_does_not_model(classic):
    fill = cp.quote_buy(classic, 100_000_000, sol_usd=SOL_USD, decimals=6)
    assert fill.not_modelled == cp.NOT_MODELLED
    assert any("adverse_selection" in item for item in fill.not_modelled)
    payload = fill.as_dict()
    assert payload["basis"] == "curve"
    assert json.dumps(payload)  # it has to survive going into order_events


def test_identical_inputs_produce_identical_fills(classic):
    a = cp.quote_buy(classic, 123_456_789, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                     drift_mode=cp.DriftMode.NONE)
    b = cp.quote_buy(classic, 123_456_789, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                     drift_mode=cp.DriftMode.NONE)
    assert a.amount_out == b.amount_out and a.fee_native == b.fee_native
    assert a.effective_price_usd == b.effective_price_usd


# ============================================================ latency


def test_the_adverse_drift_moves_the_price_against_us_on_both_sides(classic):
    flow = cp.FlowRate(lamports_per_s=Decimal("-50000000"), basis="swaps", samples=9)
    buy_drift, buy_basis = cp.drift_for(flow, side=Side.BUY, latency_ms=7_600)
    sell_drift, sell_basis = cp.drift_for(flow, side=Side.SELL, latency_ms=7_600)
    # The measured flow is *negative* (net selling) and the buy still drifts up: adverse
    # takes the magnitude, because the sign of the next 7.6 s is what we do not know.
    assert buy_drift > 0 and sell_drift < 0
    assert buy_basis == "adverse:swaps" and sell_basis == "adverse:swaps"
    assert abs(buy_drift) == abs(sell_drift) == 380_000_000


def test_the_observed_drift_keeps_the_measured_sign(classic):
    flow = cp.FlowRate(lamports_per_s=Decimal("-50000000"), basis="swaps", samples=9)
    drift, basis = cp.drift_for(flow, side=Side.BUY, latency_ms=1_000, mode=cp.DriftMode.OBSERVED)
    assert drift == -50_000_000 and basis == "observed:swaps"


def test_no_flow_measurement_means_no_drift_and_says_so(classic):
    drift, basis = cp.drift_for(cp.FlowRate(), side=Side.BUY, latency_ms=7_600)
    assert drift == 0 and basis.startswith("unavailable")
    fill = cp.quote_buy(classic, 100_000_000, sol_usd=SOL_USD, decimals=6, flow=cp.FlowRate())
    assert fill.ok and fill.latency_bps == 0
    assert fill.drift_basis.startswith("unavailable")
    assert fill.flow_basis == "unavailable"


def test_latency_makes_a_buy_strictly_worse(classic):
    flow = cp.FlowRate(lamports_per_s=Decimal("20000000"), basis="swaps", samples=20)
    instant = cp.quote_buy(classic, 200_000_000, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                           drift_mode=cp.DriftMode.NONE)
    delayed = cp.quote_buy(classic, 200_000_000, sol_usd=SOL_USD, decimals=6, latency_ms=7_600,
                           flow=flow)
    assert delayed.amount_out < instant.amount_out
    assert delayed.latency_bps > 0
    assert delayed.total_cost_bps > instant.total_cost_bps
    assert delayed.drift_lamports == 152_000_000


def test_latency_makes_a_sell_strictly_worse(classic):
    flow = cp.FlowRate(lamports_per_s=Decimal("20000000"), basis="swaps", samples=20)
    instant = cp.quote_sell(classic, 5_000_000_000_000, sol_usd=SOL_USD, decimals=6,
                            latency_ms=0, drift_mode=cp.DriftMode.NONE)
    delayed = cp.quote_sell(classic, 5_000_000_000_000, sol_usd=SOL_USD, decimals=6,
                            latency_ms=7_600, flow=flow)
    assert delayed.amount_out < instant.amount_out
    assert delayed.latency_bps < 0


def test_advancing_the_curve_stays_on_the_curve(classic):
    moved = classic.advance(500_000_000)
    assert moved.virtual_token - moved.real_token == cp.RESERVED_TOKEN_ATOMS
    assert moved.real_sol == classic.real_sol + 500_000_000
    assert moved.spot_lamports_per_atom > classic.spot_lamports_per_atom
    # The drift cannot make SOL appear from before the launch.
    drained = classic.advance(-10 * cp.LAMPORTS_PER_SOL)
    assert drained.real_sol == 0


def test_a_drift_that_would_graduate_the_token_stops_at_the_boundary(low_cap):
    moved = low_cap.advance(1_000 * cp.LAMPORTS_PER_SOL)
    assert moved.real_token == 0
    fill = cp.quote_buy(
        low_cap,
        1_000_000,
        sol_usd=SOL_USD,
        decimals=6,
        flow=cp.FlowRate(lamports_per_s=Decimal("1000000000000"), basis="swaps", samples=5),
        latency_ms=7_600,
    )
    assert not fill.ok and fill.reason == "curve_graduated_during_latency"


# ============================================================ measuring flow from the db


def _seed_token(conn, token: str = TOKEN) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms) VALUES (?,?,?,?)",
        (Chain.SOL.value, token, 6, 1_789_893_000_000),
    )


def _seed_swaps(conn, rows: list[tuple[int, str, int]], token: str = TOKEN) -> None:
    for i, (ts, side, lamports) in enumerate(rows):
        conn.execute(
            "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, "
            "amount_native, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, f"tx{i}", ts, f"w{i}", token, side, "1000", str(lamports), "fixture"),
        )


def test_flow_is_measured_from_our_own_swaps(tmp_db):
    _seed_token(tmp_db)
    base = 1_789_893_400_000
    _seed_swaps(
        tmp_db,
        [(base, "buy", 100_000_000), (base + 5_000, "buy", 100_000_000),
         (base + 10_000, "sell", 50_000_000)],
    )
    flow = cp.measure_flow(Chain.SOL, TOKEN, tmp_db, at_ms=base + 10_000)
    assert flow.basis == "swaps" and flow.samples == 3
    # net +150_000_000 lamports over 10 s
    assert flow.lamports_per_s == Decimal("15000000")


def test_too_few_swaps_falls_back_to_snapshots_then_to_nothing(tmp_db):
    _seed_token(tmp_db)
    base = 1_789_893_400_000
    _seed_swaps(tmp_db, [(base, "buy", 100_000_000)])
    assert cp.measure_flow(Chain.SOL, TOKEN, tmp_db, at_ms=base).basis == "unavailable"

    window = load("real_trade_replay.json")["windows"][0]
    for block in (window["start"], window["end"]):
        tmp_db.execute(
            "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
            "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (Chain.SOL.value, TOKEN, block["observed_ms"], block["real_sol_lamports"],
             block["virtual_sol_lamports"], str(block["real_token_atoms"]),
             str(block["virtual_token_atoms"]), "pumpfun"),
        )
    flow = cp.measure_flow(Chain.SOL, TOKEN, tmp_db, at_ms=int(window["end"]["observed_ms"]))
    assert flow.basis == "curve_snapshots" and flow.samples == 2
    assert flow.lamports_per_s is not None


def test_a_flow_measurement_never_raises_on_a_missing_table(tmp_db):
    tmp_db.execute("DROP TABLE swaps")
    tmp_db.execute("DROP TABLE curve_snapshots")
    flow = cp.measure_flow(Chain.SOL, TOKEN, tmp_db, at_ms=1_789_893_400_000)
    assert not flow.known and flow.basis == "unavailable"


# ============================================================ resolvers


def _seed_snapshot(conn, *, observed_ms: int, token: str = TOKEN) -> None:
    block = load("real_trade_replay.json")["windows"][0]["start"]
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, token, observed_ms, block["real_sol_lamports"],
         block["virtual_sol_lamports"], str(block["real_token_atoms"]),
         str(block["virtual_token_atoms"]), "pumpfun"),
    )


def test_the_snapshot_resolver_finds_a_fresh_curve(tmp_db):
    now = 1_789_893_500_000
    _seed_snapshot(tmp_db, observed_ms=now - 10_000)
    state, note = cp.snapshot_resolver(tmp_db)(Chain.SOL, TOKEN, now)
    assert state is not None and note == "ok"
    assert state.graduation_sol > 0


def test_a_stale_snapshot_is_refused_because_the_reserves_are_the_price(tmp_db):
    now = 1_789_893_500_000
    _seed_snapshot(tmp_db, observed_ms=now - 600_000)
    state, note = cp.snapshot_resolver(tmp_db)(Chain.SOL, TOKEN, now)
    assert state is None and note.startswith("curve_snapshot_stale")


def test_no_snapshot_is_a_named_refusal(tmp_db):
    state, note = cp.snapshot_resolver(tmp_db)(Chain.SOL, TOKEN, 1_789_893_500_000)
    assert state is None and note == "no_curve_snapshot"


def test_chain_resolver_takes_the_first_that_answers(tmp_db):
    now = 1_789_893_500_000
    _seed_snapshot(tmp_db, observed_ms=now - 10_000)
    def empty(chain: Chain, token: str, at_ms: int) -> tuple[cp.CurveState | None, str]:
        return None, "nope"

    state, _ = cp.chain_resolver(empty, cp.snapshot_resolver(tmp_db))(Chain.SOL, TOKEN, now)
    assert state is not None
    state, note = cp.chain_resolver(empty, empty)(Chain.SOL, TOKEN, now)
    assert state is None and note == "nope"


def test_has_graduated_reads_the_tokens_table(tmp_db):
    _seed_token(tmp_db)
    assert not cp.has_graduated(Chain.SOL, TOKEN, tmp_db)
    tmp_db.execute("UPDATE tokens SET migrated_ms=? WHERE address=?", (1_789_893_000_000, TOKEN))
    assert cp.has_graduated(Chain.SOL, TOKEN, tmp_db)


# ============================================================ the watchdog price source


def test_the_curve_price_source_makes_a_pre_graduation_token_quotable(tmp_db):
    """The watchdog was blind on every curve token. This is the injection that fixes it."""
    now = now_ms()
    _seed_token(tmp_db)
    _seed_snapshot(tmp_db, observed_ms=now - 5_000)
    source = cp.CurvePriceSource(tmp_db, sol_usd=SOL_USD)
    quote = source.quote(Chain.SOL, TOKEN)
    assert quote.usable
    assert quote.price_usd > 0
    assert quote.liquidity_usd is not None and quote.liquidity_usd > 0
    assert quote.source.startswith("curve:")
    # Liquidity is the SOL actually in the curve, not a two-sided pool figure.
    state, _ = cp.snapshot_resolver(tmp_db)(Chain.SOL, TOKEN, now)
    assert quote.liquidity_usd == state.sol_in_curve * SOL_USD


def test_the_curve_price_source_is_blind_rather_than_wrong_without_a_curve(tmp_db):
    source = cp.CurvePriceSource(tmp_db, sol_usd=SOL_USD)
    quote = source.quote(Chain.SOL, TOKEN)
    assert not quote.usable
    assert "no curve" in (quote.note or "")


def test_the_curve_price_source_refuses_without_a_sol_price(tmp_db):
    now = now_ms()
    _seed_snapshot(tmp_db, observed_ms=now - 5_000)
    source = cp.CurvePriceSource(tmp_db, sol_usd=None)
    assert not source.quote(Chain.SOL, TOKEN).usable


def test_the_curve_price_source_hands_a_graduated_token_to_its_fallback(tmp_db):
    from kaiba.core.schemas import EvidenceBasis
    from kaiba.execution.watchdog import PriceQuote

    class Pool:
        def quote(self, chain, token):
            return PriceQuote(price_usd=Decimal("0.5"), liquidity_usd=Decimal("40000"),
                              basis=EvidenceBasis.PROVIDER_REPORTED, source="pool")

    source = cp.CurvePriceSource(tmp_db, sol_usd=SOL_USD, fallback=Pool())
    quote = source.quote(Chain.SOL, TOKEN)
    assert quote.usable and quote.source == "pool"


# ============================================================ the fee, read off mainnet


def test_the_fee_split_is_the_one_mainnet_actually_charges():
    """95 bps protocol across two accounts, 30 bps creator, 125 bps total — measured.

    The ``swaps`` table cannot verify this: ``amount_native`` reconciles exactly with the
    curve's ``real_sol_reserves``, which means it is the curve leg and the fee is charged
    separately and never touches the reserves. So the fee is pinned here against raw
    lamport balance deltas read off three real pump.fun buys, and
    :func:`curve_price.platform_fee` has to reproduce each recipient's share **to the
    lamport**, component-wise ceiling and all.
    """
    doc = load("onchain_fee_split.json")
    assert len(doc["transactions"]) == 3
    for tx in doc["transactions"]:
        leg = int(tx["curve_leg_lamports"])
        deltas = tx["lamport_deltas"]
        assert deltas[doc["curve_vault"]] == leg, "the vault receives the curve leg itself"

        def share(v: int, leg: int = leg) -> Decimal:
            return Decimal(v) / Decimal(leg) * cp.BPS

        creators = [v for v in deltas.values() if v > 0 and abs(share(v) - 30) < Decimal("0.001")]
        halves = [v for v in deltas.values() if v > 0 and abs(share(v) - Decimal("47.5")) < Decimal("0.001")]
        assert len(creators) == 1, f"{tx['signature'][:12]}: expected one 30 bps creator leg"
        assert len(halves) == cp.CURVE_PROTOCOL_FEE_SPLITS, (
            f"{tx['signature'][:12]}: protocol cut is not {cp.CURVE_PROTOCOL_FEE_SPLITS} x 47.5 bps"
        )

        total, protocol, creator = cp.platform_fee(leg)
        assert creator == creators[0]
        assert protocol == sum(halves)
        assert total == creator + protocol
        # And the composite really is ~125 bps of the leg, within the rounding.
        assert abs(share(total) - cp.CURVE_TOTAL_FEE_BPS) < Decimal("0.001")


def test_the_component_rounding_costs_us_and_is_not_a_single_ceiling():
    """Each share is rounded up on its own, so the total exceeds one ceil of 125 bps.

    Two lamports, which is nothing — but it is nothing *against* us, and a model that
    rounded the other way would be a model that is quietly optimistic by construction.
    """
    doc = load("onchain_fee_split.json")
    strictly_greater = 0
    for tx in doc["transactions"]:
        leg = int(tx["curve_leg_lamports"])
        total, _, _ = cp.platform_fee(leg)
        single_ceiling = -(-leg * cp.CURVE_TOTAL_FEE_BPS // 10_000)
        assert total >= single_ceiling
        if total > single_ceiling:
            strictly_greater += 1
        assert total - single_ceiling <= cp.CURVE_PROTOCOL_FEE_SPLITS + 1
    assert strictly_greater >= 1, "the fixture must contain a case where the rounding differs"


def test_the_fee_is_charged_on_top_of_the_curve_leg_not_taken_out_of_it():
    """The buyer is debited the curve leg **plus** the fee, and the split inverts exactly.

    Getting this backwards would understate every entry price by 125 bps, which is larger
    than most of the edge anything in this repository claims to have.
    """
    doc = load("onchain_fee_split.json")
    for tx in doc["transactions"]:
        leg = int(tx["curve_leg_lamports"])
        total, _, _ = cp.platform_fee(leg)
        venue_total = leg + total
        debits = [-v for v in tx["lamport_deltas"].values() if v < 0]
        assert any(d >= venue_total for d in debits), (
            f"{tx['signature'][:12]}: nobody was debited the venue total {venue_total}"
        )
        # Budget -> leg is the exact inverse of leg -> budget.
        assert cp.curve_leg_for_budget(venue_total) == leg
        assert cp.curve_leg_for_budget(venue_total - 1) < leg


def test_a_modelled_buy_spends_the_leg_and_the_fee_and_nothing_else(classic):
    budget = 50_000_000
    fill = cp.quote_buy(classic, budget, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                        drift_mode=cp.DriftMode.NONE)
    assert fill.ok
    assert fill.curve_in + fill.fee_native == fill.amount_in <= budget
    assert budget - fill.amount_in < 10  # only the rounding remainder is left unspent
    assert fill.fee_protocol_native + fill.fee_creator_native == fill.fee_native
    total, protocol, creator = cp.platform_fee(fill.curve_in)
    assert (fill.fee_native, fill.fee_protocol_native, fill.fee_creator_native) == (
        total, protocol, creator
    )


def test_the_unmodelled_router_cost_is_recorded_with_its_measurement():
    """An honest gap is one a reader can size. This one carries its own numbers."""
    assert cp.NOT_MODELLED_ROUTER_NOTE in cp.NOT_MODELLED
    assert "0.001 SOL" in cp.NOT_MODELLED_ROUTER_NOTE
    assert "n=3" in cp.NOT_MODELLED_ROUTER_NOTE


# ============================================================ against a real router


def test_curve_arithmetic_reproduces_jupiters_executable_quote_to_the_atom():
    """Jupiter routes pump.fun curves directly, so the two can be compared head to head.

    The first version of this module claimed a bonding-curve token could not be priced by
    a router. That was wrong, and it matters: it means there *is* an independent oracle
    for the arithmetic here, and it agrees exactly. On three live pre-graduation mints at
    0.02 SOL, one hop, route label ``Pump.fun``, our output and Jupiter's are the **same
    integer** — which also validates the fee split, because the leg we put through the
    curve is derived from :func:`curve_price.curve_leg_for_budget` and any error there
    would show up as a different output.
    """
    doc = load("curve_vs_router.json")
    assert len(doc["comparisons"]) == 3
    budget = int(doc["probe_lamports"])
    for row in doc["comparisons"]:
        c = row["curve"]
        state, note = cp.CurveState.build(
            virtual_sol=int(c["virtual_sol"]), virtual_token=int(c["virtual_token"]),
            real_sol=int(c["real_sol"]), real_token=int(c["real_token"]),
            observed_ms=int(c["observed_ms"]))
        assert state is not None, note
        leg = cp.curve_leg_for_budget(budget)
        assert leg == int(row["curve_leg_lamports"])
        atoms, _, _ = state.buy_exact_in(leg)
        assert atoms == int(row["curve_atoms_out"])
        assert atoms == int(row["jupiter_atoms_out"]), (
            f"{row['token'][:12]}: curve says {atoms}, Jupiter says {row['jupiter_atoms_out']}"
        )
        assert row["jupiter_route"] == ["Pump.fun"]
        assert row["jupiter_on_bonding_curve"] is True


def test_the_module_does_not_claim_a_router_cannot_price_a_curve():
    """A regression test on the docstring, because the wrong version of that claim is the
    kind of thing that gets repeated into a design decision."""
    text = cp.__doc__ or ""
    assert "Jupiter routes pump.fun curves directly" in text
    assert "correction" in text.lower()


def test_price_impact_pct_is_documented_as_untrustworthy():
    """Jupiter returned exactly 0 impact for BONK on a split route. Zero at a tradeable
    size is a missing number, not a small one, and nothing here may fill off it."""
    assert "priceImpactPct" in cp.PRICE_IMPACT_PCT_WARNING
    assert "round_trip" in cp.PRICE_IMPACT_PCT_WARNING
    # And the module never reads that field: the only impact it reports is its own,
    # computed from reserves, or the gap implied by an executable price we were handed.
    source = Path(cp.__file__).read_text(encoding="utf-8")
    assert "price_impact_pct" not in source.replace("PRICE_IMPACT_PCT_WARNING", "")


def test_the_modelled_round_trip_is_in_the_range_a_router_actually_charges(classic):
    """MEASURED independently: a 0.02 SOL round trip on a fresh curve token costs ~260 bps.

    Our two legs are 2 x 125 bps of fee plus our own impact each way, which lands at 246
    bps of pure venue cost on the recorded curve. The broker then adds its 30 bps
    per-side floor, putting the modelled round trip at ~306 bps — **above** the 259 bps
    Jupiter's own two-sided quote returned on the same probe. That ordering is the one we
    want: the paper fill costs more than the executable quote, not less.
    """
    budget = 20_000_000
    buy = cp.quote_buy(classic, budget, sol_usd=SOL_USD, decimals=6, latency_ms=0,
                       drift_mode=cp.DriftMode.NONE)
    assert buy.ok and buy.state_after is not None
    sell = cp.quote_sell(buy.state_after, buy.amount_out, sol_usd=SOL_USD, decimals=6,
                         latency_ms=0, drift_mode=cp.DriftMode.NONE)
    assert sell.ok
    cost_bps = (budget - sell.amount_out) * 10_000 // budget
    # Dominated by the fee, not by impact, exactly as the independent measurement found.
    assert 240 <= cost_bps <= 280, cost_bps
    assert cost_bps > 2 * cp.CURVE_TOTAL_FEE_BPS - 5
