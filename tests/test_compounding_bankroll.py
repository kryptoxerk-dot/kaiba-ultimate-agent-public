"""The bankroll must track reality, compound both ways, and never invent money.

``config/risk.yaml`` holds a number a human typed. These tests pin the machinery that
turns it into a live figure: realised PnL compounds it, the wallet balance caps it, a
provider outage freezes it instead of zeroing it, and deployed capital is never counted
as spendable.

Every payload shape in here was MEASURED against the live wallets on 2026-09-21 with
``gmgn-cli portfolio token-balance``. Two of those measurements are the reason several of
these tests exist at all:

* ``--token So1111...112`` (the canonical wrapped-SOL mint) returns ``balance="0",
  height=0`` for a wallet holding 4.574456858 SOL. The correct address is
  ``So1111...111``. One character, and the difference between a funded chain and a chain
  the agent silently stops trading.
* ``balance`` is a decimal string in whole coins and the payload's own ``decimal`` field
  is ``0`` on every chain measured. Believing it turns 1.031957 BNB into 1 wei.
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
import yaml

from kaiba.core.config import ChainBudget
from kaiba.core.schemas import Chain, Lane, LaneMode, now_ms
from kaiba.execution.risk import (
    BANKROLL_FRESH_MS,
    BANKROLL_MAX_GROWTH_FACTOR_PER_READ,
    COMPOUNDING_MODE,
    NATIVE_BALANCE_TOKEN,
    WRAPPED_SOL_MINT_READS_ZERO,
    BankrollBasis,
    BankrollTracker,
    RiskGate,
    parse_native_balance,
)

SOL = Chain.SOL
BSC = Chain.BSC
LANE = Lane.CONFLUENCE_5
TOKEN = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: The live Solana wallet, 2026-09-21.
SOL_WALLET = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
EVM_WALLET = "0x72430877378522d1b759ac721561aa9eb9e25c2b"

LAMPORT = 1
SOL_UNIT = 1_000_000_000
BANKROLL = 10 * SOL_UNIT  # 10 SOL, so the arithmetic in the assertions stays readable

BASE_RISK: dict = {
    "version": "v1",
    "global_mode": "live",
    "kill_switch": False,
    "entries_paused": False,
    "reduce_only": False,
    "bounds": {
        "max_size_pct_bankroll": 5.0,
        "max_daily_loss_pct": 10.0,
        "max_slippage_bps": 2500,
        "max_concurrent_positions": None,
        "max_lane_mode": "live",
        "allow_self_promotion": True,
    },
    "chains": {
        "sol": {
            "enabled": True,
            "bankroll_base_units": BANKROLL,
            "max_position_base_units": 5 * SOL_UNIT,
            "min_position_base_units": 5_000_000,
            "gas_reserve_base_units": 50_000_000,
            "daily_loss_stop_base_units": 500_000_000,
            "max_exposure_pct": 100.0,
            "wallet": SOL_WALLET,
        },
    },
    "lanes": {
        "confluence-5": {
            "mode": "live", "size_pct_min": 1.0, "size_pct_max": 5.0,
            "chains": ["sol"], "params": {},
        },
    },
    "protection": {},
}


def _merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@pytest.fixture
def write_risk(tmp_path, monkeypatch):
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**overrides):
        path.write_text(yaml.safe_dump(_merge(BASE_RISK, overrides)), encoding="utf-8")
        return path

    _write()
    return _write


def budget(**overrides) -> ChainBudget:
    base = {
        "enabled": True,
        "bankroll_base_units": BANKROLL,
        "max_position_base_units": 5 * SOL_UNIT,
        "min_position_base_units": 5_000_000,
        "gas_reserve_base_units": 50_000_000,
        "daily_loss_stop_base_units": 500_000_000,
        "max_exposure_pct": 100.0,
        "wallet": SOL_WALLET,
    }
    base.update(overrides)
    return ChainBudget(**base)


class Clock:
    """A clock the tests own, because the read TTL and the hold window are both times."""

    def __init__(self, start: int | None = None) -> None:
        self.t = start if start is not None else now_ms()

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> int:
        self.t += ms
        return self.t


def sol_payload(coins: str, *, height: int = 448948213, wallet: str = SOL_WALLET) -> dict:
    """The exact shape `gmgn-cli portfolio token-balance --chain sol` returned."""
    return {
        "balances": [
            {
                "wallet_address": wallet,
                "token_address": NATIVE_BALANCE_TOKEN[SOL],
                "balance": coins,
                "decimal": 0,
                "height": height,
                "tx_index": 0,
            }
        ]
    }


def close_trade(conn, pnl: int, *, chain: Chain = SOL, mode: LaneMode = COMPOUNDING_MODE,
                closed_ms: int | None = None, cost: int = SOL_UNIT) -> None:
    """One closed trade. Accounting writes exactly one of these per closed position."""
    ts = closed_ms if closed_ms is not None else now_ms()
    conn.execute(
        "INSERT INTO trades (trade_id, position_id, lane, mode, chain, token, opened_ms, "
        "closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"trd-{chain.value}-{mode.value}-{ts}-{pnl}", f"pos-{ts}-{pnl}", LANE.value, mode.value,
         chain.value, TOKEN, ts - 1000, ts, 1, str(cost), str(cost + pnl), str(pnl), 0.0),
    )


def open_position(conn, token: str, cost: int, proceeds: int = 0, chain: Chain = SOL) -> None:
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, "
        "qty_total, cost_native, proceeds_native) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"p-{token}-{cost}", chain.value, token, LANE.value, "live", now_ms(), "1", "1",
         str(cost), str(proceeds)),
    )


def seed_depth(conn, token: str = TOKEN) -> None:
    """Give the token a curve so the viability gate can price its depth."""
    conn.execute(
        "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, created_ms, source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (SOL.value, token, now_ms(), "55780466117", "85780466117",
         "95360268161085", "375260268161085", now_ms(), "pumpfun"),
    )


# ======================================================================================
# parsing GMGN's payload: the measured shapes, including the ones that lie
# ======================================================================================


def test_native_balance_is_scaled_by_the_chain_not_by_the_payloads_decimal_field():
    """MEASURED: every payload came back with ``decimal: 0``. Believing it loses 10**9."""
    got = parse_native_balance(SOL, sol_payload("4.574456858"), wallet=SOL_WALLET)
    assert got.base_units == 4_574_456_858
    assert got.ok


def test_eighteen_decimal_balance_survives_exactly():
    """0.392310583560926627 ETH. A float round trip does not land on this integer."""
    payload = {
        "balances": [
            {
                "wallet_address": EVM_WALLET,
                "token_address": NATIVE_BALANCE_TOKEN[Chain.ROBINHOOD],
                "balance": "0.392310583560926627",
                "decimal": 0,
                "height": 68512561,
            }
        ]
    }
    got = parse_native_balance(Chain.ROBINHOOD, payload, wallet=EVM_WALLET)
    assert got.base_units == 392_310_583_560_926_627
    assert got.base_units != int(float("0.392310583560926627") * 1e18)


def test_fractional_base_units_truncate_downward():
    """Rounding up invents money we do not hold, and that is a failed send, not a trade."""
    payload = sol_payload("1.0000000009")  # 9 decimals of SOL plus one we cannot spend
    assert parse_native_balance(SOL, payload, wallet=SOL_WALLET).base_units == 1_000_000_000


def test_the_wrapped_sol_mint_is_not_the_native_mint():
    """MEASURED: ...112 answered 0 for a wallet holding 4.574456858 SOL, ...111 answered it.

    Pinned as a test because the wrong one is the address anybody would reach for.
    """
    assert NATIVE_BALANCE_TOKEN[SOL] == "So11111111111111111111111111111111111111111"
    assert WRAPPED_SOL_MINT_READS_ZERO == "So11111111111111111111111111111111111111112"
    assert NATIVE_BALANCE_TOKEN[SOL] != WRAPPED_SOL_MINT_READS_ZERO
    wrong = {
        "balances": [
            {
                "wallet_address": SOL_WALLET,
                "token_address": WRAPPED_SOL_MINT_READS_ZERO,
                "balance": "0",
                "decimal": 0,
                "height": 0,
            }
        ]
    }
    got = parse_native_balance(SOL, wrong, wallet=SOL_WALLET)
    assert got.base_units is None
    assert got.basis == BankrollBasis.UNAVAILABLE


def test_a_zero_at_block_zero_is_unavailable_not_empty():
    """MEASURED: all 3 real readings carried a block height; both empty ones carried 0."""
    got = parse_native_balance(SOL, sol_payload("0", height=0), wallet=SOL_WALLET)
    assert got.base_units is None, "a zero we cannot date is not a measurement of zero"
    assert got.basis == BankrollBasis.UNAVAILABLE


def test_a_zero_at_a_real_block_is_a_real_zero():
    got = parse_native_balance(SOL, sol_payload("0", height=448948213), wallet=SOL_WALLET)
    assert got.base_units == 0
    assert got.ok


@pytest.mark.parametrize(
    "payload",
    [None, {}, [], "nope", {"balances": []}, {"balances": [{"balance": "1"}]},
     sol_payload("not-a-number"), sol_payload("-1")],
)
def test_every_unusable_payload_is_none_and_never_zero(payload):
    got = parse_native_balance(SOL, payload, wallet=SOL_WALLET)
    assert got.base_units is None
    assert got.basis == BankrollBasis.UNAVAILABLE
    assert not got


def test_a_payload_about_another_wallet_is_refused():
    """Not a weaker answer to our question. A different question."""
    got = parse_native_balance(SOL, sol_payload("9.9", wallet="SomebodyElse"), wallet=SOL_WALLET)
    assert got.base_units is None


# ======================================================================================
# compounding: up on wins, down on losses
# ======================================================================================


def test_wins_raise_the_bankroll(tmp_db):
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("20.0"), clock=clock)
    before = tracker.reading(SOL, budget(), 0, tmp_db)
    assert before.equity_base_units == BANKROLL

    close_trade(tmp_db, pnl=2 * SOL_UNIT, closed_ms=clock.advance(60_000))
    after = tracker.reading(SOL, budget(), 0, tmp_db)

    assert after.realized_base_units == 2 * SOL_UNIT
    assert after.equity_base_units > before.equity_base_units
    assert after.equity_base_units == 12 * SOL_UNIT
    assert after.compounded == 2 * SOL_UNIT


def test_losses_lower_the_bankroll(tmp_db):
    """The direction that matters. It needs no provider and it cannot be switched off."""
    clock = Clock()
    tracker = BankrollTracker(clock=clock)  # deliberately unwired
    assert tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units == BANKROLL

    close_trade(tmp_db, pnl=-3 * SOL_UNIT, closed_ms=clock.advance(60_000))
    after = tracker.reading(SOL, budget(), 0, tmp_db)

    assert after.realized_base_units == -3 * SOL_UNIT
    assert after.equity_base_units == 7 * SOL_UNIT
    assert after.compounded == -3 * SOL_UNIT
    assert after.onchain_basis == BankrollBasis.UNWIRED


def test_position_size_falls_after_a_loss_and_rises_after_a_win(write_risk, tmp_db):
    """End to end through the real gate: the same conviction, a different number of SOL."""
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("20.0"), clock=clock))
    flat = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert flat == 500_000_000  # 5% of 10 SOL

    close_trade(tmp_db, pnl=-5 * SOL_UNIT, closed_ms=clock.advance(60_000))
    clock.advance(BANKROLL_FRESH_MS + 1)
    after_loss = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert after_loss == 250_000_000, "5% of the 5 SOL that is left, not of the 10 we typed"
    assert after_loss < flat

    close_trade(tmp_db, pnl=2 * SOL_UNIT, closed_ms=clock.advance(60_000))
    clock.advance(BANKROLL_FRESH_MS + 1)
    recovering = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert recovering == 350_000_000, "5% of 7 SOL"
    assert after_loss < recovering < flat

    close_trade(tmp_db, pnl=5 * SOL_UNIT, closed_ms=clock.advance(60_000))
    clock.advance(BANKROLL_FRESH_MS + 1)
    after_win = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert after_win == 600_000_000, "5% of 12 SOL"
    assert after_win > flat, "past the starting point, off a number nobody retyped"


def test_paper_pnl_never_moves_the_live_bankroll(tmp_db):
    """SHADOW and CANARY both fill on the paper broker. A simulated +219% is not money."""
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)
    for mode in (LaneMode.SHADOW, LaneMode.CANARY):
        close_trade(tmp_db, pnl=50 * SOL_UNIT, mode=mode, closed_ms=clock.advance(1000))
    after = tracker.reading(SOL, budget(), 0, tmp_db)
    assert after.realized_base_units == 0
    assert after.equity_base_units == BANKROLL


def test_another_chains_pnl_does_not_compound_this_chain(tmp_db):
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)
    close_trade(tmp_db, pnl=-4 * SOL_UNIT, chain=BSC, closed_ms=clock.advance(1000))
    assert tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units == BANKROLL


def test_pnl_from_before_the_baseline_is_not_counted(tmp_db):
    """The operator retypes the bankroll after depositing. Yesterday's PnL is in it already."""
    clock = Clock(start=now_ms())
    tracker = BankrollTracker(clock=clock)
    close_trade(tmp_db, pnl=-8 * SOL_UNIT, closed_ms=clock.t - 86_400_000)
    assert tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units == BANKROLL


def test_changing_the_configured_bankroll_rebaselines(tmp_db):
    """A new number in the file is the operator saying "this is what it is worth today"."""
    clock = Clock()
    tracker = BankrollTracker(clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)
    close_trade(tmp_db, pnl=-2 * SOL_UNIT, closed_ms=clock.advance(1000))
    assert tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units == 8 * SOL_UNIT

    clock.advance(1000)
    moved = tracker.reading(SOL, budget(bankroll_base_units=20 * SOL_UNIT), 0, tmp_db)
    assert moved.equity_base_units == 20 * SOL_UNIT
    assert moved.realized_base_units == 0
    assert "bankroll_rebaselined_to_config" in moved.findings


# ======================================================================================
# guard 1: never size against money we do not have
# ======================================================================================


def test_the_wallet_caps_the_ledger(tmp_db):
    """The ledger's errors -- unbooked gas, a failed send, a manual withdrawal -- are all
    optimistic, so the chain is the ceiling."""
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("6.0"), clock=clock)
    close_trade(tmp_db, pnl=5 * SOL_UNIT, closed_ms=clock.t)
    got = tracker.reading(SOL, budget(), 0, tmp_db)
    assert got.realized_base_units == 5 * SOL_UNIT, "the ledger says 15 SOL"
    assert got.equity_base_units == 6 * SOL_UNIT, "the chain says 6, and the chain wins"
    assert got.binding == "onchain_cap"


def test_free_never_exceeds_the_onchain_balance_minus_the_gas_reserve(tmp_db):
    reserve = 50_000_000
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("2.0"), clock=clock)
    got = tracker.reading(SOL, budget(gas_reserve_base_units=reserve), 0, tmp_db)
    assert got.onchain_base_units == 2 * SOL_UNIT
    assert got.free_base_units == 2 * SOL_UNIT - reserve


@pytest.mark.parametrize("coins,exposure", [("4.574456858", 0), ("2.0", 3 * SOL_UNIT),
                                            ("0.05", 9 * SOL_UNIT), ("10.0", 1)])
def test_free_is_never_more_than_the_chain_can_pay_whatever_else_is_true(tmp_db, coins, exposure):
    """The literal form of the operator's rule, over a spread of states."""
    reserve = 50_000_000
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload(coins), clock=clock)
    close_trade(tmp_db, pnl=100 * SOL_UNIT, closed_ms=clock.t)  # a wildly optimistic ledger
    got = tracker.reading(SOL, budget(gas_reserve_base_units=reserve), exposure, tmp_db)
    onchain = int(Decimal(coins).scaleb(9))
    assert got.free_base_units <= max(0, onchain - reserve)


def test_an_entry_larger_than_the_wallet_can_pay_is_refused_by_the_gate(write_risk, tmp_db):
    """Config says 10 SOL, the wallet holds 2, and 1.95 of that is the gas reserve.

    0.1 SOL is a legal 5% of the 2 SOL the chain reports, so every percentage check passes
    and only the spendable-balance guard can catch it. Before this change the gate would
    have measured 0.1 SOL against the 10 SOL in the file and sent an order the wallet
    could not fund.
    """
    write_risk(chains={"sol": {"gas_reserve_base_units": 1_950_000_000}})
    seed_depth(tmp_db)
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("2.0"), clock=clock))
    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert decision.reason.startswith("gas_reserve"), decision.reason


def test_the_size_clamp_is_measured_against_what_is_left_not_against_the_file(
    write_risk, tmp_db
):
    """The operator's own words: a halved account bets a dangerously large fraction.

    0.3 SOL is 3% of the 10 SOL in ``risk.yaml`` and comfortably inside the 5% envelope
    bound. Against the 2 SOL actually left it is 15%, and the envelope exists to stop
    exactly that. If this ever regresses, every percentage limit in the gate silently
    triples after a 2/3 drawdown -- the drawdown makes the bets bigger, which is the
    ruin spiral this whole dimension is supposed to remove.
    """
    seed_depth(tmp_db)
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("2.0"), clock=clock))
    close_trade(tmp_db, pnl=-8 * SOL_UNIT, closed_ms=clock.t)

    decision = gate.check_entry(SOL, LANE, 300_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert decision.reason.startswith("size_above_clamp"), decision.reason
    assert "15.0000" in decision.reason, "15% of the 2 SOL left, not 3% of the 10 we typed"


def test_max_exposure_pct_is_measured_against_what_is_left(write_risk, tmp_db):
    """Concentration is a share of the book, and the book is smaller after a loss."""
    write_risk(chains={"sol": {"max_exposure_pct": 10.0}})
    seed_depth(tmp_db)
    open_position(tmp_db, TOKEN, 150_000_000)
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("2.0"), clock=clock))
    close_trade(tmp_db, pnl=-8 * SOL_UNIT, closed_ms=clock.t)

    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert decision.reason.startswith("max_exposure_pct"), decision.reason


def test_a_drained_wallet_reads_as_unfunded(tmp_db):
    """A real zero at a real block is a real answer, and the answer is "you cannot trade"."""
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("0", height=448948213), clock=clock)
    got = tracker.reading(SOL, budget(), 0, tmp_db)
    assert got.onchain_base_units == 0
    assert got.equity_base_units == 0
    assert got.free_base_units == 0


# ======================================================================================
# guard 2: unavailable holds, and is never read as zero
# ======================================================================================


def test_an_unavailable_read_holds_the_last_known_good(tmp_db):
    clock = Clock()
    payloads = [sol_payload("8.0"), None]

    def provider(chain, wallet):
        return payloads.pop(0) if payloads else None

    tracker = BankrollTracker(provider, clock=clock)
    first = tracker.reading(SOL, budget(), 0, tmp_db)
    assert first.equity_base_units == 8 * SOL_UNIT
    assert first.onchain_basis == BankrollBasis.FRESH

    clock.advance(BANKROLL_FRESH_MS + 1)
    second = tracker.reading(SOL, budget(), 0, tmp_db)
    assert second.onchain_base_units == 8 * SOL_UNIT, "held, not re-read and not zeroed"
    assert second.onchain_basis == BankrollBasis.HELD
    assert second.equity_base_units == first.equity_base_units


def test_a_provider_outage_does_not_halve_the_position_size(write_risk, tmp_db):
    """The explicit requirement. An outage changes nothing; only a real reading does."""
    clock = Clock()
    alive = {"ok": True}

    def provider(chain, wallet):
        if not alive["ok"]:
            raise TimeoutError("gmgn-cli did not answer")
        return sol_payload("12.0")

    gate = RiskGate(bankroll=BankrollTracker(provider, clock=clock))
    healthy = gate.position_size(SOL, LANE, 95.0, tmp_db)
    assert healthy == 500_000_000  # 5% of the 10 SOL ledger, under the 12 SOL cap

    alive["ok"] = False
    for _ in range(5):
        clock.advance(BANKROLL_FRESH_MS + 1)
        assert gate.position_size(SOL, LANE, 95.0, tmp_db) == healthy


def test_a_provider_that_raises_is_never_read_as_an_empty_wallet(tmp_db):
    def provider(chain, wallet):
        raise RuntimeError("boom")

    got = BankrollTracker(provider, clock=Clock()).reading(SOL, budget(), 0, tmp_db)
    assert got.onchain_base_units is None
    assert got.equity_base_units == BANKROLL, "falls back to the operator's own number"
    assert got.binding == "config_cap"
    assert got.onchain_basis == BankrollBasis.UNAVAILABLE


def test_while_we_cannot_look_the_bankroll_may_fall_but_not_rise(tmp_db):
    """Both halves of the hold rule in one test, because only holding one is a trap."""
    clock = Clock()
    alive = {"ok": True}

    def provider(chain, wallet):
        return sol_payload("50.0") if alive["ok"] else None

    tracker = BankrollTracker(provider, clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)
    alive["ok"] = False

    close_trade(tmp_db, pnl=5 * SOL_UNIT, closed_ms=clock.advance(BANKROLL_FRESH_MS + 1))
    blind_win = tracker.reading(SOL, budget(), 0, tmp_db)
    assert blind_win.equity_base_units == BANKROLL, "no growth on evidence we do not have"
    assert blind_win.binding == "hold_no_growth"

    close_trade(tmp_db, pnl=-9 * SOL_UNIT, closed_ms=clock.advance(BANKROLL_FRESH_MS + 1))
    blind_loss = tracker.reading(SOL, budget(), 0, tmp_db)
    assert blind_loss.equity_base_units == 6 * SOL_UNIT, "a loss lands even when blind"


def test_an_expired_cache_falls_back_to_the_configured_number_not_to_zero(tmp_db):
    clock = Clock()
    alive = {"ok": True}

    def provider(chain, wallet):
        return sol_payload("30.0") if alive["ok"] else None

    tracker = BankrollTracker(provider, clock=clock, hold_ms=60_000)
    tracker.reading(SOL, budget(), 0, tmp_db)
    alive["ok"] = False
    clock.advance(3_600_000)
    got = tracker.reading(SOL, budget(), 0, tmp_db)
    assert got.onchain_basis == BankrollBasis.EXPIRED
    assert got.onchain_base_units is None
    assert got.equity_base_units == BANKROLL
    assert got.free_base_units > 0, "a day of blindness is not a reason to stop trading"


def test_an_unwired_tracker_behaves_exactly_like_the_old_static_bankroll(tmp_db):
    got = BankrollTracker(clock=Clock()).reading(SOL, budget(), 0, tmp_db)
    assert got.onchain_basis == BankrollBasis.UNWIRED
    assert got.equity_base_units == BANKROLL
    assert got.free_base_units == BANKROLL - 50_000_000


def test_a_chain_with_no_wallet_configured_says_so(tmp_db):
    got = BankrollTracker(lambda c, w: sol_payload("9.0"), clock=Clock()).reading(
        SOL, budget(wallet=None), 0, tmp_db
    )
    assert got.onchain_basis == BankrollBasis.NO_WALLET
    assert got.onchain_base_units is None


def test_the_provider_is_not_called_again_inside_the_ttl(tmp_db):
    """A read costs ~969 ms (MEASURED, n=3). High volume cannot pay that per decision."""
    clock = Clock()
    calls = []

    def provider(chain, wallet):
        calls.append(clock.t)
        return sol_payload("10.0")

    tracker = BankrollTracker(provider, clock=clock)
    for _ in range(20):
        clock.advance(BANKROLL_FRESH_MS // 10)
        tracker.reading(SOL, budget(), 0, tmp_db)
    assert len(calls) <= 3, f"one read per {BANKROLL_FRESH_MS} ms, got {len(calls)}"


# ======================================================================================
# guard 3: deployed capital is capital, but it is not spendable
# ======================================================================================


def test_open_exposure_is_netted_out_of_free_but_not_out_of_equity(tmp_db):
    """The double count that would shrink every bet as the agent opened positions."""
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("6.0"), clock=clock)
    got = tracker.reading(SOL, budget(), 4 * SOL_UNIT, tmp_db)
    assert got.equity_base_units == 10 * SOL_UNIT, "6 in the wallet + 4 deployed"
    assert got.free_base_units == 6 * SOL_UNIT - 50_000_000, "only the wallet is spendable"


def test_size_does_not_collapse_as_positions_open(write_risk, tmp_db):
    """Sized off equity, paid out of the wallet. The two must not be the same number."""
    clock = Clock()
    wallet = {"coins": "10.0"}
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload(wallet["coins"]), clock=clock))
    first = gate.position_size(SOL, LANE, 95.0, tmp_db)

    # Two positions open; the wallet has paid for them, so it is 2 SOL lighter.
    open_position(tmp_db, TOKEN, SOL_UNIT)
    open_position(tmp_db, TOKEN + "2", SOL_UNIT)
    wallet["coins"] = "8.0"
    clock.advance(BANKROLL_FRESH_MS + 1)

    assert gate.position_size(SOL, LANE, 95.0, tmp_db) == first, (
        "equity is unchanged at 8 in the wallet + 2 deployed, so conviction buys the same size"
    )


def test_free_is_floored_at_zero_when_exposure_exceeds_equity(tmp_db):
    got = BankrollTracker(clock=Clock()).reading(SOL, budget(), 99 * SOL_UNIT, tmp_db)
    assert got.free_base_units == 0
    assert got.equity_base_units >= 0


def test_the_gate_refuses_an_entry_that_would_over_commit(write_risk, tmp_db):
    seed_depth(tmp_db)
    open_position(tmp_db, TOKEN, 9_800_000_000)
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("0.2"), clock=clock))
    decision = gate.check_entry(SOL, LANE, 200_000_000, tmp_db, token=TOKEN)
    assert not decision.allowed
    assert decision.reason.startswith("gas_reserve")


# ======================================================================================
# guard 4: the growth clamp
# ======================================================================================


def test_a_balance_read_that_doubles_the_wallet_cannot_double_the_bet(tmp_db):
    """The stated worry. Note it is caught twice: the ledger does not know about the extra
    money either, so a wrong *high* read cannot raise equity at all."""
    clock = Clock()
    wallet = {"coins": "10.0"}
    tracker = BankrollTracker(lambda c, w: sol_payload(wallet["coins"]), clock=clock)
    before = tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units

    wallet["coins"] = "1000000.0"  # a garbage read
    clock.advance(BANKROLL_FRESH_MS + 1)
    after = tracker.reading(SOL, budget(), 0, tmp_db)
    assert after.equity_base_units == before
    assert after.binding == "ledger", "the ledger caps it before the clamp is even reached"


def test_growth_is_clamped_when_the_ledger_and_the_chain_both_jump(tmp_db):
    """Belt and braces: a corrupted PnL row plus a matching balance still cannot run away.

    This is the only way past ``min(ledger, cap)``, and the clamp exists for exactly it.
    """
    clock = Clock()
    wallet = {"coins": "10.0"}
    tracker = BankrollTracker(lambda c, w: sol_payload(wallet["coins"]), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)

    wallet["coins"] = "1000.0"
    close_trade(tmp_db, pnl=990 * SOL_UNIT, closed_ms=clock.advance(BANKROLL_FRESH_MS + 1))
    got = tracker.reading(SOL, budget(), 0, tmp_db)

    assert got.equity_base_units < 1000 * SOL_UNIT
    assert got.equity_base_units <= int(Decimal(BANKROLL) * BANKROLL_MAX_GROWTH_FACTOR_PER_READ)
    assert got.binding == "growth_clamp"


def test_the_clamp_does_not_depend_on_the_clock(tmp_db):
    clock = Clock()
    wallet = {"coins": "10.0"}
    tracker = BankrollTracker(lambda c, w: sol_payload(wallet["coins"]), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)

    wallet["coins"] = "1000.0"
    close_trade(tmp_db, pnl=990 * SOL_UNIT, closed_ms=clock.t)
    clock.advance(365 * 24 * 3_600_000)  # a clock that lurches must not buy headroom
    got = tracker.reading(SOL, budget(), 0, tmp_db)
    assert got.equity_base_units <= int(Decimal(BANKROLL) * BANKROLL_MAX_GROWTH_FACTOR_PER_READ)


def test_falls_are_never_clamped(tmp_db):
    """Asymmetric on purpose: a small bet cannot cause ruin, a large one can."""
    clock = Clock()
    wallet = {"coins": "10.0"}
    tracker = BankrollTracker(lambda c, w: sol_payload(wallet["coins"]), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)

    wallet["coins"] = "0.4"
    clock.advance(BANKROLL_FRESH_MS + 1)
    got = tracker.reading(SOL, budget(), 0, tmp_db)
    assert got.equity_base_units == 400_000_000, "the whole fall lands in one read"


def test_growth_resumes_over_successive_reads(tmp_db):
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("1000.0"), clock=clock)
    tracker.reading(SOL, budget(), 0, tmp_db)
    close_trade(tmp_db, pnl=990 * SOL_UNIT, closed_ms=clock.t)

    seen = []
    for _ in range(12):
        clock.advance(BANKROLL_FRESH_MS + 1)
        seen.append(tracker.reading(SOL, budget(), 0, tmp_db).equity_base_units)
    assert seen == sorted(seen), "monotone"
    assert seen[-1] > seen[0]
    assert seen[-1] <= 1000 * SOL_UNIT


# ======================================================================================
# the state this keeps, and what the gate reports
# ======================================================================================


def test_money_is_persisted_as_text_so_eighteen_decimal_chains_survive(tmp_db):
    """10 BNB is 10**19 base units. SQLite INTEGER tops out at 2**63-1 ~ 9.22 * 10**18."""
    clock = Clock()
    payload = {
        "balances": [
            {
                "wallet_address": EVM_WALLET,
                "token_address": NATIVE_BALANCE_TOKEN[BSC],
                "balance": "40.0",
                "decimal": 0,
                "height": 123117107,
            }
        ]
    }
    tracker = BankrollTracker(lambda c, w: payload, clock=clock)
    big = ChainBudget(enabled=True, bankroll_base_units=40 * 10**18,
                      gas_reserve_base_units=10**16, wallet=EVM_WALLET)
    got = tracker.reading(BSC, big, 0, tmp_db)
    assert got.equity_base_units == 40 * 10**18 > 2**63 - 1
    stored = tracker.state(BSC, tmp_db)
    assert isinstance(stored["onchain_base_units"], str)
    assert int(stored["onchain_base_units"]) == 40 * 10**18


def test_the_daily_summary_reports_the_compounded_figure_and_its_provenance(write_risk, tmp_db):
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock))
    close_trade(tmp_db, pnl=-2 * SOL_UNIT, closed_ms=clock.t)
    sol = gate.daily_summary(tmp_db)["chains"]["sol"]
    assert sol["bankroll_base_units"] == BANKROLL, "the configured baseline keeps its name"
    assert sol["bankroll_equity_base_units"] == 8 * SOL_UNIT
    assert sol["bankroll_compounded_base_units"] == -2 * SOL_UNIT
    assert sol["bankroll_onchain_basis"] == BankrollBasis.FRESH


def test_the_entry_decision_records_where_the_bankroll_came_from(write_risk, tmp_db):
    seed_depth(tmp_db)
    clock = Clock()
    gate = RiskGate(bankroll=BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock))
    decision = gate.check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert decision.allowed, decision.reason
    assert any(f.startswith("bankroll_onchain_basis:") for f in decision.findings)
    assert any(f.startswith("bankroll_realized_since_baseline:") for f in decision.findings)


def test_an_unfunded_config_does_not_compound_into_existence(tmp_db):
    """Missing numeric authorisation is not infinity, and a win does not create it."""
    clock = Clock()
    tracker = BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock)
    close_trade(tmp_db, pnl=5 * SOL_UNIT, closed_ms=clock.t)
    got = tracker.reading(SOL, budget(bankroll_base_units=0), 0, tmp_db)
    assert got.equity_base_units == 0
    assert got.free_base_units == 0
    assert got.binding == "config_unfunded"


def test_compounding_survives_a_restart(tmp_db):
    """The state lives in the database, not in the process."""
    clock = Clock()
    first = BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock)
    first.reading(SOL, budget(), 0, tmp_db)
    close_trade(tmp_db, pnl=-6 * SOL_UNIT, closed_ms=clock.advance(1000))

    reborn = BankrollTracker(lambda c, w: sol_payload("10.0"), clock=clock)
    assert reborn.reading(SOL, budget(), 0, tmp_db).equity_base_units == 4 * SOL_UNIT
