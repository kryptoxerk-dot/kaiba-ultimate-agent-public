"""Pool-priced marks and paper entries for graduated pump.fun tokens (2026-10-19).

The formula (vault + virtual quote reserves) was validated on the box against six executed
PumpSwap trades: within 0.0-1.3% (the fee); vault-only was off by +5% to +154%. These tests pin
the arithmetic, the account layout, that anything unpriceable REFUSES (so the existing chain
answers), that the source never raises, and that a curve token does not cost an RPC every tick.
The bonding-curve source for tokens that have not graduated is pinned at the bottom.
"""

from __future__ import annotations

import base64
import struct
from decimal import Decimal
from types import SimpleNamespace

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.execution import paper_costs
from kaiba.execution import pumpswap_price as pp
from kaiba.learning import graduation_observer as go
from tests.test_paper import solo_risk  # noqa: F401 - fixture, requested by name
from tests.test_watchdog import risk_file  # noqa: F401 - fixture, requested by name

TOKEN = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
BASE_VAULT = "8psNvWTrdNTiVRNzAgsou9kETXNJm2SXZyaKuJraVRtf"
QUOTE_VAULT = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def pool_bytes(*, token: str = TOKEN, quote_mint: str = pp.WSOL_MINT, virtual_lamports: int = 17_580_000_000) -> bytes:
    data = bytearray(301)
    data[43:75] = go.b58decode(token)
    data[75:107] = go.b58decode(quote_mint)
    data[139:171] = go.b58decode(BASE_VAULT)
    data[171:203] = go.b58decode(QUOTE_VAULT)
    data[245:261] = int(virtual_lamports).to_bytes(16, "little", signed=True)
    return bytes(data)


def token_account(amount: int) -> bytes:
    data = bytearray(165)
    struct.pack_into("<Q", data, 64, amount)
    return bytes(data)


def acct(raw: bytes) -> dict:
    return {"data": [base64.b64encode(raw).decode(), "base64"]}


@pytest.fixture(autouse=True)
def _fresh_caches():
    pp.clear_caches()
    yield
    pp.clear_caches()


def test_price_is_vault_plus_virtual_over_base():
    # A fresh BOOST pool: 67.41 real + 17.58 virtual SOL against 206.9M tokens -> ~4.11e-7 SOL.
    price, depth, virtual = pp.pool_price(pool_bytes(), token_account(206_900_000_000_000),
                                          token_account(67_410_000_000))
    assert depth == Decimal("84.99") and virtual == 17_580_000_000
    assert price == pytest.approx(Decimal("84.99") / Decimal("206900000"))


def test_a_negative_virtual_reserve_lowers_the_price_and_a_drained_pool_is_unpriceable():
    """MEASURED -9.78 SOL virtual on a 138.9 SOL pool. If real + virtual <= 0, refuse."""
    price, depth, _ = pp.pool_price(pool_bytes(virtual_lamports=-9_780_000_000), token_account(10**14),
                                    token_account(138_870_000_000))
    assert depth == Decimal("129.09")
    assert pp.pool_price(pool_bytes(virtual_lamports=-5_000_000_000), token_account(10**14),
                         token_account(4_000_000_000)) is None


def test_a_pool_not_quoted_in_wsol_is_not_priced(monkeypatch):
    other = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert pp.pool_price(pool_bytes(quote_mint=other), token_account(10**14), token_account(10**10)) is None
    rpc = FakeRpc(pool=pool_bytes(quote_mint=other))
    src = source_with(monkeypatch, rpc)
    assert not src.quote(Chain.SOL, TOKEN).usable and not src.quote(Chain.SOL, TOKEN).usable
    assert rpc.calls == ["getAccountInfo"], "refused from the pool account alone, and remembered"


class FakeRpc:
    def __init__(self, *, pool: bytes | None, base: int = 206_900_000_000_000, quote: int = 67_410_000_000,
                 fail: bool = False):
        self.pool, self.base, self.quote, self.fail = pool, base, quote, fail
        self.calls: list[str] = []

    def __call__(self, method, params, endpoint):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError("rpc on fire")
        if method == "getAccountInfo":
            return {"value": acct(self.pool) if self.pool else None}
        return {"value": [acct(self.pool), acct(token_account(self.base)), acct(token_account(self.quote))]}


def source_with(monkeypatch, rpc: FakeRpc, sol_usd: str | None = "100") -> pp.PumpSwapPoolSource:
    src = pp.PumpSwapPoolSource()
    monkeypatch.setattr(src, "_rpc", rpc)
    monkeypatch.setattr(src, "_sol_usd", lambda: Decimal(sol_usd) if sol_usd else None)
    monkeypatch.setattr(go, "pumpswap_pool_address", lambda mint: "PooL1111111111111111111111111111111111111111")
    return src


def test_a_graduated_token_gets_a_verified_native_pool_mark(monkeypatch):
    from kaiba.execution.watchdog import SETTLE_NATIVE_POOL

    rpc = FakeRpc(pool=pool_bytes())
    q = source_with(monkeypatch, rpc).quote(Chain.SOL, TOKEN)
    assert q.usable and q.basis is EvidenceBasis.VERIFIED_ONCHAIN and q.settlement == SETTLE_NATIVE_POOL
    assert q.price_usd == pytest.approx(Decimal("84.99") / Decimal("206900000") * 100)
    assert q.liquidity_usd == Decimal("84.99") * 2 * 100
    assert q.venue == "pumpswap" and q.source.startswith("pumpswap-pool:")
    assert rpc.calls == ["getAccountInfo", "getMultipleAccounts"]
    source_with(monkeypatch, rpc).quote(Chain.SOL, TOKEN)
    assert rpc.calls[-1] == "getMultipleAccounts" and rpc.calls.count("getAccountInfo") == 1, \
        "the vault addresses are learned once"


def test_a_curve_token_refuses_and_is_not_asked_again_every_tick(monkeypatch):
    rpc = FakeRpc(pool=None)
    src = source_with(monkeypatch, rpc)
    assert not src.quote(Chain.SOL, TOKEN).usable
    assert not src.quote(Chain.SOL, TOKEN).usable
    assert rpc.calls == ["getAccountInfo"], "no pool yet is cached for NO_POOL_TTL_S"


def test_other_chains_refuse_without_a_network_call(monkeypatch):
    rpc = FakeRpc(pool=pool_bytes())
    q = source_with(monkeypatch, rpc).quote(Chain.ROBINHOOD, TOKEN)
    assert not q.usable and rpc.calls == []


def test_the_source_never_raises_and_needs_a_fresh_native_price(monkeypatch):
    assert not source_with(monkeypatch, FakeRpc(pool=pool_bytes(), fail=True)).quote(Chain.SOL, TOKEN).usable
    pp.clear_caches()
    rpc = FakeRpc(pool=pool_bytes())
    assert not source_with(monkeypatch, rpc, sol_usd=None).quote(Chain.SOL, TOKEN).usable
    assert rpc.calls == [], "a mark that cannot be put in USD must not spend an RPC call"


def test_protection_asks_the_pool_first_and_keeps_every_other_layer():
    from kaiba.execution import watchdog as wd

    src = wd._venue_price_source()
    names = [getattr(s, "name", type(s).__name__) for s in src.sources]
    assert names == ["pumpswap-pool", "pump-curve", "venue", "gmgn"]


# ---------------------------------------------------------------- paper entry and costs


def test_a_paper_entry_on_sol_asks_the_pool_then_the_curve_then_the_dossier(monkeypatch):
    from kaiba.execution import engine
    from kaiba.execution.watchdog import PriceQuote

    def quote(price, liquidity, source):
        return PriceQuote(price_usd=Decimal(price), liquidity_usd=Decimal(liquidity),
                          basis=EvidenceBasis.VERIFIED_ONCHAIN, source=source)

    def refused(source):
        return PriceQuote.unavailable("nothing here", source=source)

    state = object()  # stands in for the CurveState read with the quote
    decision = SimpleNamespace(chain=Chain.SOL, token=TOKEN)
    dossier = SimpleNamespace(price_usd=SimpleNamespace(value=Decimal("0.00002"), known=True),
                              liquidity_usd=SimpleNamespace(value=Decimal("15000"), known=True))
    pool = quote("0.00004", "17000", "pumpswap-pool:PooL1111")
    monkeypatch.setattr(pp.PumpSwapPoolSource, "quote", lambda self, chain, token: pool)
    monkeypatch.setattr(pp.PumpCurveSource, "quote_with_state",
                        lambda self, chain, token: (quote("0.00003", "9000", "pump-curve:CuRv1111"), state))
    assert engine.paper_entry_quote(decision, dossier, None) == (Decimal("0.00004"), Decimal("17000"),
                                                                "pumpswap-pool:PooL1111", None)
    monkeypatch.setattr(pp.PumpSwapPoolSource, "quote", lambda self, chain, token: refused("pumpswap-pool"))
    assert engine.paper_entry_quote(decision, dossier, None) == (Decimal("0.00003"), Decimal("9000"),
                                                                "pump-curve:CuRv1111", state)
    monkeypatch.setattr(pp.PumpCurveSource, "quote_with_state",
                        lambda self, chain, token: (refused("pump-curve"), None))
    assert engine.paper_entry_quote(decision, dossier, None) == (Decimal("0.00002"), Decimal("15000"),
                                                                "dossier", None)
    bsc = SimpleNamespace(chain=Chain.BSC, token=TOKEN)
    monkeypatch.setattr(pp.PumpSwapPoolSource, "quote", lambda self, chain, token: pool)
    assert engine.paper_entry_quote(bsc, dossier, None)[2] == "dossier", "only sol has these venues"


def test_paper_charges_the_measured_live_costs():
    # 1% router + 0.88% drift net of the pool fee (2026-10-21; it was 2.2%, which contained the fee)
    assert paper_costs.entry_price(Chain.SOL, Decimal("1")) == Decimal("1.0188")
    assert paper_costs.exit_price(Chain.SOL, Decimal("1")) == Decimal("0.99")
    assert paper_costs.entry_price(Chain.ROBINHOOD, Decimal("1")) == Decimal("1.035")  # router + 2.5% hook
    assert paper_costs.exit_price(Chain.ROBINHOOD, Decimal("1")) == Decimal("0.965")
    assert paper_costs.exit_price(Chain.BSC, Decimal("2")) == Decimal("1.98")
    assert paper_costs.entry_price(Chain.BSC, Decimal("2")) == Decimal("2.02")
    assert paper_costs.overcharge_before_fix_bps(Chain.SOL) == 132
    assert paper_costs.overcharge_before_fix_bps(Chain.ROBINHOOD) == 0


#: MEASURED 2026-10-21 on n=112 live PumpSwap buys (fill_costs; VERIFICATION.md section 1 row 12):
#: median allin_vs_decision_bps, everything the wallet paid per token against the decision price --
#: the pool fee, the router fee, our impact and the market's drift, each counted once.
LIVE_SOL_POOL_BUY_ALLIN_MEDIAN_BPS = 322


def test_a_sol_pool_paper_entry_pays_the_pool_fee_once(tmp_db, solo_risk, monkeypatch):  # noqa: F811
    """The entry's latency charge was exec_vs_decision's median (+219 bps), which already holds the
    ~120 bps PumpSwap fee the broker then charged again: a paper pool entry cost ~477 bps all-in
    against a live median of 322. Driven through engine.handoff and the real broker, a sol pool
    entry with no impact must land within the broker's own 30 bps slippage floor of that live
    median, on either side: not above it (the fee twice) and not below it (the favourable MEAN
    drift, which would charge nothing, is not the conservative figure)."""
    import json

    from kaiba.execution import engine
    from kaiba.execution import paper as P
    from tests.test_paper import TOKEN as PAPER_TOKEN
    from tests.test_paper import make_signal, seed_protection_evidence, write_dossier

    write_dossier(tmp_db, price="0.001", liquidity="50000")
    seed_protection_evidence(tmp_db, PAPER_TOKEN)
    # Graduated: the broker must take the pool path and charge its PumpSwap tier-1 fee itself.
    tmp_db.execute("UPDATE tokens SET migrated_ms = first_seen_ms WHERE address = ?", (PAPER_TOKEN,))
    tmp_db.commit()
    # The protection probe reads the stored curve, which a graduated token no longer has; how a
    # graduated token gets its mark is pinned above (test_a_graduated_token_gets_a_verified_...).
    monkeypatch.setattr(engine, "_protection_refusal", lambda decision: None)
    quote = Decimal("0.001")
    monkeypatch.setattr(engine, "paper_entry_quote", lambda decision, dossier, conn: (
        quote, Decimal("1e15"), "pumpswap-pool:PooL1111", None))
    make_signal(tmp_db)
    [decision] = engine.run_once(tmp_db)
    order_id = tmp_db.execute("SELECT order_id FROM orders WHERE side='buy'").fetchone()[0]
    basis = P.fill_basis(tmp_db, order_id)
    assert basis["venue"] == "pumpswap-tier1" and basis["impact_bps"] == 0
    assert basis["fee_bps"] == P.PUMPSWAP_TIER1_TOTAL_FEE_BPS == 120   # the pool fee, charged here
    floor = int(basis["slippage_floor_bps"])
    # The broker takes its fee off the input, then fills the rest at effective_price_usd.
    allin = Decimal(basis["effective_price_usd"]) / quote / (1 - Decimal(basis["fee_bps"]) / 10_000) - 1
    allin_bps = float(allin * 10_000)
    assert LIVE_SOL_POOL_BUY_ALLIN_MEDIAN_BPS - floor <= allin_bps <= LIVE_SOL_POOL_BUY_ALLIN_MEDIAN_BPS + floor, (
        f"paper pool entry costs {allin_bps:.0f} bps all-in against a live median of "
        f"{LIVE_SOL_POOL_BUY_ALLIN_MEDIAN_BPS}")
    # The receipt the lead dates the fix by: 188 from the deploy on, 320 before it.
    priced = [json.loads(p) for (p,) in tmp_db.execute("SELECT payload FROM events WHERE payload LIKE "
                                                         "'%paper_entry_priced%'")]
    assert [p["entry_cost_bps"] for p in priced] == [188]


def curve_bytes(*, virtual_token: int, virtual_sol: int, real_token: int = 0, real_sol: int = 0,
                complete: bool = False) -> bytes:
    data = bytearray(49)
    struct.pack_into("<QQQQQ", data, 8, virtual_token, virtual_sol, real_token, real_sol, 10**15)
    data[48] = 1 if complete else 0
    return bytes(data)


def recorded_curve_bytes(**override) -> tuple[bytes, object]:
    """Account bytes for the real mainnet curve ``tests/test_paper.live_curve`` records."""
    from tests.test_paper import live_curve

    ref = live_curve()
    fields = {"virtual_token": ref.virtual_token, "virtual_sol": ref.virtual_sol,
              "real_token": ref.real_token, "real_sol": ref.real_sol, **override}
    return curve_bytes(**fields), ref


def test_a_curve_price_is_virtual_sol_over_virtual_tokens_and_a_completed_curve_refuses():
    price, depth = pp.curve_price(curve_bytes(virtual_token=1_073_000_000_000_000, virtual_sol=30_000_000_000))
    assert depth == Decimal(30) and price == Decimal(30) / Decimal(1_073_000_000)
    assert pp.curve_price(curve_bytes(virtual_token=10**15, virtual_sol=10**10, complete=True)) is None
    assert pp.curve_price(b"\x00" * 10) is None


def test_the_curve_source_marks_a_live_curve_and_refuses_a_graduated_one(monkeypatch):
    from kaiba.execution.watchdog import SETTLE_CURVE

    live = curve_bytes(virtual_token=800_000_000_000_000, virtual_sol=40_000_000_000)
    calls: list[str] = []
    src = pp.PumpCurveSource()
    monkeypatch.setattr(src, "_rpc", lambda m, p, e: calls.append(m) or {"value": acct(live)})
    monkeypatch.setattr(src, "_sol_usd", lambda: Decimal("100"))
    q = src.quote(Chain.SOL, TOKEN)
    assert q.usable and q.settlement == SETTLE_CURVE and q.basis is EvidenceBasis.VERIFIED_ONCHAIN
    assert q.price_usd == Decimal(40) / Decimal(800_000_000) * 100
    pp.clear_caches()
    done = curve_bytes(virtual_token=10**15, virtual_sol=10**10, complete=True)
    monkeypatch.setattr(src, "_rpc", lambda m, p, e: calls.append(m) or {"value": acct(done)})
    assert not src.quote(Chain.SOL, TOKEN).usable
    n = len(calls)
    assert not src.quote(Chain.SOL, TOKEN).usable and len(calls) == n, "a graduated curve is cached as refused"
    pp.clear_caches()
    monkeypatch.setattr(src, "_sol_usd", lambda: None)
    assert not src.quote(Chain.SOL, TOKEN).usable and len(calls) == n, "no SOL/USD, no RPC call"
    assert not src.quote(Chain.ROBINHOOD, TOKEN).usable and len(calls) == n


# ---------------------------------------------------------------- the broker's curve, read now


def test_a_curve_account_becomes_the_brokers_curve_state():
    data, ref = recorded_curve_bytes()
    state = pp.curve_state(data, observed_ms=123)
    assert (state.virtual_sol, state.virtual_token, state.real_sol, state.real_token) == (
        ref.virtual_sol, ref.virtual_token, ref.real_sol, ref.real_token)
    assert state.observed_ms == 123 and state.source == "pump-curve-account"
    done, _ = recorded_curve_bytes(complete=True)
    assert pp.curve_state(done, observed_ms=1) is None
    odd, _ = recorded_curve_bytes(real_token=ref.real_token + 1)
    assert pp.curve_state(odd, observed_ms=1) is None, "a shape the paper model does not know is refused"


def test_read_state_reads_now_and_only_remembers_a_curve_that_is_not_there():
    data, ref = recorded_curve_bytes()
    calls: list[str] = []
    src = pp.PumpCurveSource()
    src._rpc = lambda m, p, e: calls.append(m) or None  # the read itself failed
    assert src.read_state(TOKEN) is None and src.read_state(TOKEN) is None
    assert len(calls) == 2, "a failed read is retried, never remembered as 'no curve'"
    src._rpc = lambda m, p, e: calls.append(m) or {"value": acct(data)}
    assert src.read_state(TOKEN).virtual_sol == ref.virtual_sol
    src._rpc = lambda m, p, e: calls.append(m) or {"value": None}  # no account: not a pump.fun token
    assert src.read_state(TOKEN) is None
    n = len(calls)
    assert src.read_state(TOKEN) is None and len(calls) == n


def test_a_failed_read_is_not_remembered_as_no_pool_or_no_curve(monkeypatch):
    rpc = FakeRpc(pool=pool_bytes())
    src = source_with(monkeypatch, rpc)
    monkeypatch.setattr(src, "_rpc", lambda m, p, e: rpc.calls.append(m) or None)
    assert not src.quote(Chain.SOL, TOKEN).usable
    monkeypatch.setattr(src, "_rpc", rpc)
    assert src.quote(Chain.SOL, TOKEN).usable, "the next tick asks again and prices"

    data, _ = recorded_curve_bytes()
    curve = pp.PumpCurveSource()
    monkeypatch.setattr(curve, "_sol_usd", lambda: Decimal("100"))
    monkeypatch.setattr(curve, "_rpc", lambda m, p, e: None)
    assert not curve.quote(Chain.SOL, TOKEN).usable
    monkeypatch.setattr(curve, "_rpc", lambda m, p, e: {"value": acct(data)})
    quote, state = curve.quote_with_state(Chain.SOL, TOKEN)
    assert quote.usable and state is not None and state.observed_ms == quote.observed_ms


def test_a_shadow_entry_fills_against_the_curve_read_at_handoff(tmp_db, solo_risk, monkeypatch):  # noqa: F811
    """engine.handoff hands the broker the reserves it read, so the fill cannot fall back to a
    curve_snapshots row up to 60 s old (PaperBroker.buy ignores price_usd for a curve token)."""
    from kaiba.execution import engine
    from kaiba.execution import paper as P
    from tests.test_paper import TOKEN as PAPER_TOKEN
    from tests.test_paper import live_curve, make_signal, seed_protection_evidence, write_dossier

    write_dossier(tmp_db, price="0.001", liquidity="50000")
    seed_protection_evidence(tmp_db, PAPER_TOKEN)
    state = live_curve()
    monkeypatch.setattr(engine, "paper_entry_quote", lambda decision, dossier, conn: (
        Decimal("0.000004"), Decimal("20000"), "pump-curve:CuRv1111", state))
    make_signal(tmp_db)
    [decision] = engine.run_once(tmp_db)
    order_id = tmp_db.execute("SELECT order_id FROM orders WHERE side='buy'").fetchone()[0]
    basis = P.fill_basis(tmp_db, order_id)
    assert basis["basis"] == "curve"
    assert basis["state_decided"]["observed_ms"] == state.observed_ms
    assert basis["state_decided"]["virtual_sol"] == state.virtual_sol


def test_a_paper_exit_sells_into_the_curve_read_at_the_decision(tmp_db, risk_file, monkeypatch):  # noqa: F811
    from kaiba.execution import paper as P
    from kaiba.execution import watchdog as wd
    from tests.test_paper import live_curve
    from tests.test_watchdog import TOKEN as WD_TOKEN
    from tests.test_watchdog import FakeSource, make_position, sell_orders

    make_position(tmp_db)
    state = live_curve()
    asked: list[str] = []
    monkeypatch.setattr(pp.PumpCurveSource, "read_state", lambda self, token: asked.append(token) or state)
    source = FakeSource({WD_TOKEN: "0.6"})
    dog = wd.Watchdog(tmp_db, price_source=source, submitter=wd.DefaultExitSubmitter(tmp_db, source))
    assert dog.tick().exits == 1
    assert asked == [WD_TOKEN]
    basis = P.fill_basis(tmp_db, sell_orders(tmp_db)[0]["order_id"])
    assert basis["basis"] == "curve" and basis["state_decided"]["observed_ms"] == state.observed_ms


def test_a_paper_exit_off_sol_never_reads_a_curve(tmp_db, risk_file, monkeypatch):  # noqa: F811
    from kaiba.execution import watchdog as wd
    from kaiba.execution.paper import load_position
    from tests.test_watchdog import FakeSource, make_position

    evm = "0x" + "ab" * 20
    make_position(tmp_db, token=evm, chain=Chain.BSC)
    asked: list[str] = []
    monkeypatch.setattr(pp.PumpCurveSource, "read_state", lambda self, token: asked.append(token))
    submitter = wd.DefaultExitSubmitter(tmp_db, FakeSource({evm: "0.6"}))
    assert submitter._paper_curve(load_position(tmp_db, "pos_1")) is None
    assert asked == [], "an RPC read for a chain that has no pump.fun curve"


# ---------------------------------------------------------------- reserve activity (stale exits)


def test_reserve_activity_starts_at_the_first_read_and_moves_only_on_a_change(monkeypatch):
    clock = iter([1_000, 2_000, 3_000, 4_000])
    monkeypatch.setattr("kaiba.core.schemas.now_ms", lambda: next(clock))
    rpc = FakeRpc(pool=pool_bytes())
    src = source_with(monkeypatch, rpc)
    assert pp.last_moved_ms(TOKEN) is None, "never read is unknown, not silent"
    src.quote(Chain.SOL, TOKEN)
    assert pp.last_moved_ms(TOKEN) == 1_000
    src.quote(Chain.SOL, TOKEN)
    assert pp.last_moved_ms(TOKEN) == 1_000, "same vaults: no trade seen"
    rpc.quote += 5_000_000  # someone bought
    src.quote(Chain.SOL, TOKEN)
    assert pp.last_moved_ms(TOKEN) == 3_000

    data, ref = recorded_curve_bytes()
    curve = pp.PumpCurveSource()
    monkeypatch.setattr(curve, "_sol_usd", lambda: Decimal("100"))
    monkeypatch.setattr(curve, "_rpc", lambda m, p, e: {"value": acct(data)})
    other = "3GzeyKuMi3QNuQbSWcxASLx3kxmgiYxxYZyFS2SXpump"  # a pump.fun mint (tests/test_paper.py)
    curve.quote(Chain.SOL, other)
    assert pp.last_moved_ms(other) == 4_000


def test_a_sol_position_off_our_tape_goes_stale_on_reserve_silence(tmp_db, risk_file, monkeypatch):  # noqa: F811
    from kaiba.core.schemas import now_ms
    from kaiba.execution import watchdog as wd
    from tests.test_watchdog import TOKEN as WD_TOKEN
    from tests.test_watchdog import FakeSource, make_position, sell_orders

    make_position(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({WD_TOKEN: "1.0"}),
                      submitter=wd.DefaultExitSubmitter(tmp_db, FakeSource({WD_TOKEN: "1.0"})))
    position = wd.open_positions(tmp_db)[0]
    assert dog._last_trade_ms(position) is None, "no tape, never read: unknown, so it never sells"
    quiet = now_ms() - 3_700_000
    monkeypatch.setattr(pp, "reserve_clock", lambda token: ((1, 2), quiet, quiet) if token == WD_TOKEN else None)
    assert dog._last_trade_ms(position) == quiet
    assert dog.tick().exits == 1
    assert sell_orders(tmp_db)[0]["state"] == "filled"
    reasons = [r[0] for r in tmp_db.execute("SELECT exit_reason FROM positions")]
    assert reasons and reasons[0].startswith("stale_no_volume"), reasons


# ---------------------------------------------------------------- our paper holding, put back in


UNTOUCHED = {"virtual_sol": 30_000_000_009, "virtual_token": 1_073_000_000_000_000, "real_sol": 9,
             "real_token": 793_100_000_000_000}  # CX4KK4Ak on chain at its stale exit, 2026-10-19
OURS = 3_513_985_513_130  # the tokens its paper buy got


def test_selling_paper_tokens_into_the_chains_curve_needs_our_buy_put_back():
    from kaiba.execution import curve_price as cp

    untouched, note = cp.CurveState.build(**UNTOUCHED)
    assert untouched is not None, note
    flat = {"sol_usd": Decimal("100"), "decimals": 6, "latency_ms": 0, "drift_mode": cp.DriftMode.NONE}
    bare = cp.quote_sell(untouched, OURS, **flat)
    assert bare.capped_at_reserves and bare.amount_out < 100, "the bug: SOL the chain's curve never held"

    held = pp.with_holding(untouched, OURS)
    assert held.virtual_token == untouched.virtual_token - OURS and held.real_token == untouched.real_token - OURS
    assert held.virtual_sol * held.virtual_token >= untouched.virtual_sol * untouched.virtual_token
    assert held.source == "pumpfun+holding"
    sold = cp.quote_sell(held, OURS, **flat)
    paid_in = held.real_sol - untouched.real_sol
    assert not sold.capped_at_reserves
    assert paid_in * (10_000 - 2 * cp.CURVE_TOTAL_FEE_BPS) // 10_000 <= sold.amount_out <= paid_in
    assert pp.with_holding(untouched, untouched.real_token + 1) is None, "more than the curve has left: refuse"
    assert pp.with_holding(untouched, 0) is untouched and pp.with_holding(None, OURS) is None


def test_the_paper_exit_puts_the_positions_remaining_tokens_back(tmp_db, risk_file, monkeypatch):  # noqa: F811
    from kaiba.execution import curve_price as cp
    from kaiba.execution import watchdog as wd
    from kaiba.execution.paper import load_position
    from tests.test_watchdog import FakeSource, make_position

    untouched, _ = cp.CurveState.build(**UNTOUCHED)
    make_position(tmp_db, qty=OURS)
    monkeypatch.setattr(pp.PumpCurveSource, "read_state", lambda self, token: untouched)
    submitter = wd.DefaultExitSubmitter(tmp_db, FakeSource({}))
    got = submitter._paper_curve(load_position(tmp_db, "pos_1"))
    assert got.virtual_token == untouched.virtual_token - OURS and got.real_sol > untouched.real_sol


def test_the_reserve_clock_survives_a_protection_restart(tmp_db, risk_file, monkeypatch):  # noqa: F811
    """2026-10-19: the 03:40 restart put two 85-min-silent positions back to 5 min."""
    from kaiba.core.schemas import now_ms
    from kaiba.execution import watchdog as wd
    from tests.test_watchdog import TOKEN as WD_TOKEN
    from tests.test_watchdog import FakeSource, make_position

    make_position(tmp_db)
    dog = wd.Watchdog(tmp_db, price_source=FakeSource({WD_TOKEN: "1.0"}), submitter=wd.DefaultExitSubmitter(
        tmp_db, FakeSource({WD_TOKEN: "1.0"})))
    position = wd.open_positions(tmp_db)[0]
    t0 = now_ms() - 5_000_000
    clock = {"v": ((7, 8), t0, t0)}
    monkeypatch.setattr(pp, "reserve_clock", lambda token: clock["v"])
    assert dog._last_trade_ms(position) == t0

    restart = t0 + 4_000_000  # same reserves, a new process: first read == last change
    clock["v"] = ((7, 8), restart, restart)
    assert dog._last_trade_ms(position) == t0, "unchanged since before the restart: the older silence holds"

    clock["v"] = ((9, 8), restart, restart)  # they changed while nobody watched
    assert dog._last_trade_ms(position) == restart, "unknown when: start from this process's first read"

    moved = restart + 60_000
    clock["v"] = ((10, 8), restart, moved)
    assert dog._last_trade_ms(position) == moved
    stored = tmp_db.execute("SELECT value FROM kv WHERE key = ?",
                            (f"{wd.RESERVE_CLOCK_PREFIX}sol:{WD_TOKEN}",)).fetchone()[0]
    assert '"moved_ms":' in stored and str(moved) in stored
