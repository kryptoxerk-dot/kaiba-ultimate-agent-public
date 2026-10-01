"""``engine._plan_live_order`` must never plan a live buy with no floor under it.

Why this file exists, in one paragraph. Until 2026-09-21 the engine wrote ``min_out=0``
onto every planned live order (engine.py:635). That had two effects and both are pinned
here. The visible one: ``policy.check_gmgn_swap_body`` calls ``_positive_amount`` on
``min_output_amount``, so every live buy the engine ever planned was refused at the signer
with ``gmgn_body_amount_invalid:min_output_amount`` -- 110 decisions and 0 live orders, on
every chain. The invisible one, which matters more: had the policy not caught it,
``min_out=0`` tells the venue "any output is acceptable", which is an unbounded-slippage
market order and precisely the shape a sandwich bot is looking for.

So there are exactly three properties worth defending, and the task set them out:

1. ``_plan_live_order`` never yields ``min_out <= 0`` -- it either plans a positive floor
   or plans nothing at all.
2. A LIVE decision survives ``check_gmgn_swap_body`` end to end: engine -> order row ->
   ``executor.gmgn_swap_body`` -> policy, with no hand-written body in between.
3. A decision that cannot be priced is REFUSED, not sent with a zero or an invented price.

Everything here is offline by construction. No test in this file stubs the network out,
because none of them can reach it: the only step in ``_plan_min_out`` that can call a
provider is the native-price top-up, and every test either seeds a fresh
``native_prices`` sample first or refuses before that step is reached. The one test that
must prove the refusal *when there is no sample* installs a ``native_price.ensure_recent``
that raises, which is both offline and a stronger assertion -- it proves the failure of
the top-up is survived rather than propagated.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import (
    EVM_ZERO,
    NATIVE_DECIMALS,
    SOL_NATIVE_MINT,
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
    now_ms,
)
from kaiba.execution import engine, executor, fills
from kaiba.execution.policy import check_gmgn_swap_body
from kaiba.providers import native_price

SOL_TOKEN = "MinOutTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
EVM_TOKEN = "0x1111111111111111111111111111111111111111"
WALLET_SOL = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
WALLET_EVM = "0x00000000000000000000000000000000000000a1"

#: One SOL. The whole of this file is sized on it so every expected number below can be
#: worked out by hand: 1 SOL at $200 is $200, and $200 of a $0.001 token is 200,000
#: tokens, which at 6 decimals is 2e11 atoms before the slippage band.
ONE_SOL = 1_000_000_000
SOL_USD = Decimal("200")
TOKEN_USD = Decimal("0.001")
TOKEN_DECIMALS = 6


# --------------------------------------------------------------------------------------
# fixtures: every input the engine needs, written the way production writes it
# --------------------------------------------------------------------------------------


def write_dossier(conn, *, token=SOL_TOKEN, chain=Chain.SOL, price=TOKEN_USD, age_s=5):
    """A dossier row with a priced (or deliberately unpriced) token."""
    measure = (
        Measure(
            value=Decimal(price),
            basis=EvidenceBasis.PROVIDER_REPORTED,
            receipt=Receipt(provider="fixture", endpoint="dyor"),
            freshness_budget_s=86_400,
        )
        if price is not None
        else Measure.unknown()
    )
    dossier = TokenDossier(
        address=token, chain=chain, price_usd=measure, grade=Grade.B,
        built_at_ms=now_ms() - age_s * 1000,
    )
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, token, dossier.built_at_ms, None, Grade.B.value,
         "[]", "[]", "[]", dossier.model_dump_json()),
    )
    return dossier


def write_decimals(conn, *, token=SOL_TOKEN, chain=Chain.SOL, decimals=TOKEN_DECIMALS):
    """Register the token with chain-verified decimals.

    Deliberately routed through ``fills._store_decimals`` rather than a hand-written
    ``tokens`` row. The engine accepts only ``fills.DECIMALS_VERIFIED``, and that verdict
    is decided by a ``meta_json`` key ``fills`` owns; writing the row by hand here would
    let this fixture and the production check drift apart without either one failing.
    """
    fills._store_decimals(conn, chain, token, decimals, register=True)


def write_unverified_decimals(conn, *, token=SOL_TOKEN, chain=Chain.SOL, decimals=TOKEN_DECIMALS):
    """A ``tokens`` row whose decimals came from a provider payload, not from the chain."""
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?,?)",
        (chain.value, token, int(decimals), now_ms(), "{}"),
    )


def write_native_sample(conn, *, chain=Chain.SOL, usd=SOL_USD, age_ms=0):
    """A native/USD sample on the books, written through ``native_price.record``."""
    sample = native_price.NativeSample(
        chain=chain,
        ts_ms=now_ms() - age_ms,
        price_usd=Decimal(usd),
        source="fixture",
        pair="FIXTURE/USDC",
        liquidity_usd=Decimal(50_000_000),
        receipt=Receipt(provider="fixture", endpoint="native", basis=EvidenceBasis.PROVIDER_REPORTED),
    )
    native_price.record(sample, conn)
    return sample


def live_decision(*, chain=Chain.SOL, token=SOL_TOKEN, size=ONE_SOL, decision_id="dec_minout"):
    return Decision(
        decision_id=decision_id,
        ts_ms=now_ms(),
        lane=Lane.CONFLUENCE_5,
        mode=LaneMode.LIVE,
        chain=chain,
        token=token,
        action=Action.ENTER,
        thesis="fixture",
        confidence=0.6,
        dossier_grade=Grade.B,
        size_base_units=size,
    )


@pytest.fixture
def no_network(monkeypatch):
    """Prove the refusal paths never depend on a provider being reachable.

    ``ensure_recent`` is the only call in ``_plan_min_out`` that can leave the machine.
    Making it raise is stricter than making it a no-op: it asserts that a dead provider
    produces a refusal rather than an exception out of ``handoff``.
    """

    def boom(*a, **kw):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(native_price, "ensure_recent", boom)


@pytest.fixture
def priced(tmp_db, no_network):
    """The full happy-path evidence set: dossier price, verified decimals, native sample."""
    write_dossier(tmp_db)
    write_decimals(tmp_db)
    write_native_sample(tmp_db)
    return tmp_db


# --------------------------------------------------------------------------------------
# property 1: a planned live order always carries a positive floor
# --------------------------------------------------------------------------------------


def test_a_planned_live_buy_carries_a_positive_min_out(priced):
    """The bug, stated as its inverse. This is the single assertion that mattered."""
    order = engine._plan_live_order(live_decision(), priced)

    assert order is not None, "a fully priced live decision must plan an order"
    assert order.min_out > 0, "min_out=0 is an unbounded-slippage market order"
    assert order.state is OrderState.PLANNED
    assert order.side.value == "buy"
    assert order.input_token == SOL_NATIVE_MINT and order.output_token == SOL_TOKEN


def test_the_floor_is_the_quoted_price_less_the_slippage_band(priced):
    """The number itself, worked out by hand rather than recomputed from the code.

    1 SOL in, SOL at $200, token at $0.001 => 200,000 tokens => 2e11 atoms at 6 decimals.
    The shipped band is ``bounds.max_slippage_bps = 2500``, so the floor is 75% of that:
    150,000,000,000 atoms. Written as a literal on purpose -- a test that recomputes the
    formula it is testing agrees with any formula, including a wrong one.
    """
    order = engine._plan_live_order(live_decision(), priced)

    assert order is not None
    assert order.min_out == 150_000_000_000
    assert order.slippage_bps == 2500


def test_the_row_on_disk_carries_the_same_floor_as_the_object(priced):
    """The executor reads the row, not the object. A floor only in memory is no floor."""
    order = engine._plan_live_order(live_decision(), priced)
    assert order is not None

    row = priced.execute("SELECT min_out, slippage_bps, state FROM orders").fetchone()
    # ``min_out`` is stored as TEXT (base units outgrow SQLite's INTEGER for an 18-decimal
    # token), and both readers of the column -- executor.reconcile and the scheduler's
    # planned-order sweep -- do ``int(row["min_out"])``. Same cast here.
    assert int(row["min_out"]) == order.min_out > 0
    assert int(row["slippage_bps"]) == order.slippage_bps
    assert row["state"] == OrderState.PLANNED.value

    # And the provenance of the floor is on the order's own timeline, not only in memory.
    detail = priced.execute(
        "SELECT detail FROM order_events WHERE order_id=?", (order.order_id,)
    ).fetchone()["detail"]
    assert f"min_out={order.min_out}" in detail
    assert EvidenceBasis.DERIVED.value in detail


def test_the_floor_scales_with_the_output_token_decimals(tmp_db, no_network):
    """``min_out`` is in OUTPUT token atoms. Getting the decimals wrong is a 10^d error.

    Same trade, two tokens differing only in decimals: the floors must differ by exactly
    10^(18-6). This is the property that a hardcoded 6 or an 18 guessed from "it's EVM"
    would silently break.
    """
    write_native_sample(tmp_db)
    write_dossier(tmp_db, token=SOL_TOKEN)
    write_decimals(tmp_db, token=SOL_TOKEN, decimals=6)
    six = engine._plan_live_order(live_decision(decision_id="dec_six"), tmp_db)

    other = "MinOutTokenBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    write_dossier(tmp_db, token=other)
    write_decimals(tmp_db, token=other, decimals=18)
    eighteen = engine._plan_live_order(
        live_decision(token=other, decision_id="dec_eighteen"), tmp_db
    )

    assert six is not None and eighteen is not None
    assert eighteen.min_out == six.min_out * 10**12


def test_the_floor_is_rounded_down_never_up():
    """A floor that was rounded up is not a floor: it can refuse a fill we would accept.

    Sized so the exact answer is fractional: 1 lamport at $200/SOL is $2e-7, which at
    $3 a token is 6.666...e-8 tokens, 66.66... atoms at 9 decimals, and 50.0 after the
    25% band -- so this also pins that an exact boundary is not pushed below itself.
    """
    exact = engine._min_out_atoms(
        amount_in=1, native_usd=Decimal("200"), token_usd=Decimal("3"),
        native_decimals=9, token_decimals=9, slippage_bps=2500,
    )
    assert exact == 50  # floor(66.666... * 0.75) == floor(50.0) == 50

    fractional = engine._min_out_atoms(
        amount_in=1, native_usd=Decimal("200"), token_usd=Decimal("3"),
        native_decimals=9, token_decimals=9, slippage_bps=2499,
    )
    assert fractional == 50  # 50.006... floored, not rounded to 51


def test_arithmetic_refuses_rather_than_returning_zero():
    """Every degenerate input returns ``None``. None of them returns 0.

    ``0`` is a value this function is structurally forbidden to produce, because 0 is the
    bug: it reads as a successful answer at every call site and means "no floor".
    """
    base = dict(
        amount_in=ONE_SOL, native_usd=SOL_USD, token_usd=TOKEN_USD,
        native_decimals=9, token_decimals=6, slippage_bps=2500,
    )
    for override in (
        {"amount_in": 0},
        {"amount_in": -1},
        {"native_usd": Decimal(0)},
        {"token_usd": Decimal(0)},
        {"native_usd": Decimal(-200)},
        {"slippage_bps": 10_000},   # a 100% band is min_out=0 by arithmetic
        {"slippage_bps": 10_001},
        {"slippage_bps": -1},
        {"token_decimals": -1},
        # Size too small to buy one atom: $2e-7 of a $1e6 token at 6 decimals.
        {"amount_in": 1, "token_usd": Decimal(1_000_000)},
    ):
        got = engine._min_out_atoms(**{**base, **override})
        assert got is None, f"{override} should refuse, got {got}"


# --------------------------------------------------------------------------------------
# property 2: the planned order survives the signer policy, end to end
# --------------------------------------------------------------------------------------


def test_a_planned_live_order_survives_check_gmgn_swap_body(priced):
    """Engine -> order row -> executor body -> policy, with nothing hand-written between.

    This is the test the blocker needed. ``test_ops_execute_planned.py`` proved the policy
    rejects a zero floor; it built the body by hand, so it could not notice that the
    engine was the thing producing the zero. Here the body comes from the order the engine
    actually planned, so the day the engine regresses this fails.
    """
    order = engine._plan_live_order(live_decision(), priced)
    assert order is not None

    body = executor.gmgn_swap_body(order, WALLET_SOL)
    verdict = check_gmgn_swap_body(body, wallet=WALLET_SOL, chain=Chain.SOL, conn=priced)

    assert verdict.allowed is True, f"the signer refused our own planned order: {verdict.reason}"
    assert int(body["min_output_amount"]) > 0
    assert body["auto_slippage"] is False


def test_an_evm_planned_order_survives_the_policy_too(tmp_db, no_network):
    """Blocker A blocked every chain, so the fix has to be proven on more than Solana.

    Robinhood is the chain the operator asked for by name. Its native token is ETH
    (``NATIVE_SYMBOL[Chain.ROBINHOOD]``) and it has no reference pool of its own, so the
    floor is priced from ETH's sample -- the same reasoning ``kaiba.ingest.robinhood``
    already uses for its own ETH price, and the reason ``_native_pricing_chain`` exists.
    """
    write_native_sample(tmp_db, chain=Chain.ETH, usd=Decimal("4000"))
    write_dossier(tmp_db, token=EVM_TOKEN, chain=Chain.ROBINHOOD, price=Decimal("0.002"))
    write_decimals(tmp_db, token=EVM_TOKEN, chain=Chain.ROBINHOOD, decimals=18)

    decision = live_decision(
        chain=Chain.ROBINHOOD, token=EVM_TOKEN, size=10**16, decision_id="dec_rh"
    )  # 0.01 ETH
    order = engine._plan_live_order(decision, tmp_db)

    assert order is not None, "a priced robinhood decision must plan an order"
    assert order.input_token == EVM_ZERO and order.output_token == EVM_TOKEN
    # 0.01 ETH at $4,000 is $40; $40 of a $0.002 token is 20,000 tokens; 2e22 atoms at
    # 18 decimals; 75% of that after the 2500 bps band.
    assert order.min_out == 15 * 10**21

    body = executor.gmgn_swap_body(order, WALLET_EVM)
    verdict = check_gmgn_swap_body(body, wallet=WALLET_EVM, chain=Chain.ROBINHOOD, conn=tmp_db)
    assert verdict.allowed is True, f"the signer refused a robinhood order: {verdict.reason}"


def test_robinhood_is_priced_from_eth_and_not_from_some_other_asset(tmp_db, no_network):
    """A chain without its own reference pool borrows the price of the *same* asset only.

    Pinned because the failure mode is silent: pricing Robinhood's ETH gas token off a
    BNB sample would produce a plausible-looking floor that is wrong by the ETH/BNB ratio.
    A BSC sample on the books must not satisfy a Robinhood order.
    """
    assert engine._native_pricing_chain(Chain.ROBINHOOD) is Chain.ETH
    assert engine._native_pricing_chain(Chain.SOL) is Chain.SOL
    assert engine._native_pricing_chain(Chain.BASE) is Chain.BASE  # ETH-native, own pool

    write_native_sample(tmp_db, chain=Chain.BSC, usd=Decimal("600"))
    write_dossier(tmp_db, token=EVM_TOKEN, chain=Chain.ROBINHOOD)
    write_decimals(tmp_db, token=EVM_TOKEN, chain=Chain.ROBINHOOD, decimals=18)

    decision = live_decision(chain=Chain.ROBINHOOD, token=EVM_TOKEN, size=10**16)
    assert engine._plan_live_order(decision, tmp_db) is None, "a BNB sample is not an ETH price"


# --------------------------------------------------------------------------------------
# property 3: no price means no order
# --------------------------------------------------------------------------------------


def _assert_refused(conn, decision, *, reason: str):
    """A refusal is: no order row, an ``abandoned`` outcome, and a warn-level event."""
    assert engine._plan_live_order(decision, conn) is None
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0, (
        "a refused order must leave nothing for the executor to pick up"
    )
    link = conn.execute(
        "SELECT outcome, note FROM decision_outcomes WHERE decision_id=?", (decision.decision_id,)
    ).fetchone()
    assert link is not None and link["outcome"] == "abandoned"
    assert reason in (link["note"] or "")
    event = conn.execute(
        "SELECT level, payload FROM events WHERE kind='system' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event is not None and event["level"] == "warn"
    assert reason in event["payload"] and EvidenceBasis.UNAVAILABLE.value in event["payload"]


def test_a_decision_with_no_token_price_is_refused(tmp_db, no_network):
    """The headline rule: no price, no order. Not a zero floor, not a guessed price."""
    write_dossier(tmp_db, price=None)
    write_decimals(tmp_db)
    write_native_sample(tmp_db)

    _assert_refused(tmp_db, live_decision(), reason="token_price_unavailable")


def test_a_decision_with_no_native_price_sample_is_refused(tmp_db, no_network):
    """Half the exchange rate is not the exchange rate.

    ``no_network`` makes the top-up raise, so this also proves an unreachable provider
    produces a refusal and not a traceback out of ``handoff``.
    """
    write_dossier(tmp_db)
    write_decimals(tmp_db)

    _assert_refused(tmp_db, live_decision(), reason="native_price_unavailable")


def test_a_native_sample_outside_the_tolerance_is_not_a_price(tmp_db, no_network):
    """A sample from an hour ago prices an hour-old market. ``at()`` says so; obey it."""
    write_dossier(tmp_db)
    write_decimals(tmp_db)
    write_native_sample(tmp_db, age_ms=native_price.DEFAULT_TOLERANCE_MS + 60_000)

    _assert_refused(tmp_db, live_decision(), reason="native_price_unavailable")


def test_unknown_token_decimals_refuse_the_order(tmp_db, no_network):
    """No decimals, no atoms. There is no safe default for this number."""
    write_dossier(tmp_db)
    write_native_sample(tmp_db)

    _assert_refused(tmp_db, live_decision(), reason="token_decimals_unavailable")


def test_unverified_token_decimals_refuse_the_order(tmp_db, no_network):
    """A provider's decimals is a claim, and a wrong claim is a 10^d error in the floor.

    ``fills.token_decimals`` returns an unverified ``tokens``-row value with a note rather
    than refusing, because its own caller is accounting. This caller is sizing real money
    in the direction that matters: decimals too low manufactures a near-zero floor, which
    is the bug the whole file is about. So only ``verified_onchain`` is accepted.
    """
    write_dossier(tmp_db)
    write_native_sample(tmp_db)
    write_unverified_decimals(tmp_db)

    # Prove the fixture really is the unverified case, so this test cannot pass for the
    # wrong reason if fills' own lookup changes shape.
    decimals, basis, _ = fills.token_decimals(
        Chain.SOL, SOL_TOKEN, tmp_db, fetch=False, register=False
    )
    assert decimals == TOKEN_DECIMALS and basis == fills.DECIMALS_TOKENS_ROW

    _assert_refused(tmp_db, live_decision(), reason="token_decimals_unavailable")


def test_a_stale_dossier_refuses_a_live_order(tmp_db, no_network):
    """A price we would refuse to decide on is a price we must refuse to trade on."""
    write_dossier(tmp_db, age_s=engine.DOSSIER_MAX_AGE_S + 60)
    write_decimals(tmp_db)
    write_native_sample(tmp_db)

    _assert_refused(tmp_db, live_decision(), reason="dossier_stale")


def test_a_zero_sized_decision_is_refused(tmp_db, no_network):
    """Nothing in, nothing out -- and certainly not an order with a zero floor."""
    write_dossier(tmp_db)
    write_decimals(tmp_db)
    write_native_sample(tmp_db)

    _assert_refused(tmp_db, live_decision(size=0), reason="size_base_units_not_positive")


def test_handoff_returns_none_rather_than_raising_when_the_buy_cannot_be_priced(tmp_db, no_network):
    """``run_once`` catches exceptions but does not advance on them. A refusal is a value.

    ``handoff`` is the boundary ``run_once`` and ``submit_agent_intent`` both call, and
    both already handle ``None``. Pinning it here so the refusal path cannot turn into an
    exception that stalls a pass.
    """
    write_dossier(tmp_db, price=None)
    decision = live_decision()

    assert engine.handoff(decision, None, tmp_db) is None  # type: ignore[arg-type]
    assert tmp_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


# --------------------------------------------------------------------------------------
# the structural guarantee
# --------------------------------------------------------------------------------------


def test_no_reachable_input_makes_plan_live_order_emit_a_non_positive_floor(tmp_db, no_network):
    """Sweep the inputs and assert the invariant, rather than asserting one example.

    Prices spanning eleven orders of magnitude, decimals from 0 to 18, sizes from one
    lamport to a thousand SOL: every call either returns ``None`` or returns an order
    with ``min_out > 0``. There is no combination that produces the third outcome.
    """
    write_native_sample(tmp_db)
    seen_orders = 0
    for i, price in enumerate(["0.0000000001", "0.001", "1", "1000", "250000"]):
        for decimals in (0, 6, 9, 18):
            for size in (1, ONE_SOL // 1000, ONE_SOL, 1000 * ONE_SOL):
                token = f"Sweep{i}{decimals}{size}".ljust(43, "z")
                write_dossier(tmp_db, token=token, price=Decimal(price))
                write_decimals(tmp_db, token=token, decimals=decimals)
                order = engine._plan_live_order(
                    live_decision(token=token, size=size, decision_id=f"d{token}"), tmp_db
                )
                if order is None:
                    continue
                seen_orders += 1
                assert order.min_out > 0, f"{price}/{decimals}/{size} planned a zero floor"
    assert seen_orders > 0, "the sweep must actually plan some orders, or it proves nothing"
    assert tmp_db.execute("SELECT COUNT(*) FROM orders WHERE CAST(min_out AS INTEGER) <= 0")\
        .fetchone()[0] == 0


def write_curve_snapshot(conn, token: str = SOL_TOKEN) -> None:
    """The PROTECTION path's first layer for Solana, which `no_network` makes the only one.

    `engine._protection_refusal` (2026-09-22) probes the source the watchdog will use
    before an entry is handed off, enforcing "never open a position we cannot monitor".
    On Solana that source reads `curve_snapshots` first and only then reaches for
    DexScreener and the router -- both of which this test's `no_network` fixture blocks,
    correctly. So the snapshot is the evidence that has to be on the books.

    `virtual_token - real_token` must equal `curve_price.RESERVED_TOKEN_ATOMS` or the
    resolver refuses with `reserved_token_invariant_violated`.
    """
    from kaiba.core.schemas import now_ms as _now
    from kaiba.execution import curve_price as _cp

    real_token = 700_000_000_000_000
    conn.execute(
        "INSERT OR REPLACE INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        "virtual_sol_lamports, real_token_atoms, virtual_token_atoms, source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (Chain.SOL.value, token, _now(), 30_000_000_000, 60_000_000_000,
         str(real_token), str(real_token + _cp.RESERVED_TOKEN_ATOMS), "pumpfun"),
    )
    conn.commit()


def test_a_live_entry_through_run_once_leaves_one_planned_order_and_no_position(tmp_db, monkeypatch, no_network):
    """The whole lane, end to end: signal -> decide -> handoff -> one floored PLANNED row.

    This duplicates the intent of ``test_paper.py::test_a_live_entry_only_leaves_a_planned
    _order_for_the_executor`` on purpose. That test predates the fix and asserts an order
    row exists for a decision with **no** price evidence on the books, which is exactly
    the case the engine must now refuse; it belongs to another module's owner, so the
    coverage it provides is reproduced here with the evidence seeded rather than silently
    lost. What is pinned is unchanged from the original: a LIVE lane writes a PLANNED
    order for the executor and opens no position itself -- plus the floor, which the
    original could not have asserted.
    """
    from kaiba.core.config import load_risk
    from kaiba.execution import lanes

    write_dossier(tmp_db)
    write_decimals(tmp_db)
    write_native_sample(tmp_db)
    write_curve_snapshot(tmp_db)

    cfg = load_risk()
    lane_cfg = cfg.lane(Lane.CONFLUENCE_5).model_copy(update={"mode": LaneMode.LIVE})
    cfg = cfg.model_copy(update={
        "global_mode": LaneMode.LIVE,
        "bounds": cfg.bounds.model_copy(update={"max_lane_mode": LaneMode.LIVE}),
        "lanes": {**cfg.lanes, Lane.CONFLUENCE_5: lane_cfg},
    })
    monkeypatch.setattr(engine, "get_risk", lambda: cfg)
    monkeypatch.setattr(engine, "position_size", lambda chain, lane, score, conn=None: ONE_SOL)

    class PermissiveGate:
        def check_entry(self, **kwargs):
            return None

        def position_size(self, chain, lane, score, conn=None, *, token=None):
            # Added 2026-09-22: `_size_for` no longer falls through to the module-level
            # sizer when the gate cannot size, so the gate itself has to answer.
            return ONE_SOL

    monkeypatch.setattr(engine, "RiskGate", PermissiveGate)

    created = now_ms()
    lanes.record(
        Signal(
            signal_id=lanes.signal_id_for(Lane.CONFLUENCE_5, Chain.SOL, SOL_TOKEN, created, 120),
            lane=Lane.CONFLUENCE_5, chain=Chain.SOL, token=SOL_TOKEN, strength=0.6,
            reasons=["fixture signal"], window_s=120, created_ms=created,
        ),
        tmp_db,
    )

    [decision] = engine.run_once(tmp_db)
    assert decision.mode is LaneMode.LIVE and decision.action is Action.ENTER

    rows = tmp_db.execute("SELECT * FROM orders").fetchall()
    assert len(rows) == 1
    assert rows[0]["state"] == OrderState.PLANNED.value
    assert rows[0]["provider"] == "gmgn"  # the executor routes on this
    assert int(rows[0]["min_out"]) > 0, "the executor must never be handed a floorless order"
    assert tmp_db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0


def test_native_decimals_come_from_the_chain_table_not_from_a_guess(priced):
    """The input leg is native base units, and SOL is 9 where EVM is 18.

    Pinned because the conversion silently uses ``NATIVE_DECIMALS[decision.chain]``; a
    regression to a hardcoded 18 on Solana would understate the spend by 10^9 and produce
    a floor so small it is indistinguishable from the original bug.
    """
    assert NATIVE_DECIMALS[Chain.SOL] == 9 and NATIVE_DECIMALS[Chain.ROBINHOOD] == 18

    order = engine._plan_live_order(live_decision(), priced)
    assert order is not None
    wrong_leg = engine._min_out_atoms(
        amount_in=ONE_SOL, native_usd=SOL_USD, token_usd=TOKEN_USD,
        native_decimals=18, token_decimals=TOKEN_DECIMALS, slippage_bps=2500,
    )
    assert wrong_leg is None or order.min_out != wrong_leg
