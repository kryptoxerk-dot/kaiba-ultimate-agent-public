"""Never open a SOLANA position the watchdog cannot price.

`viability.quote_asset_is_protectable` already enforces that rule -- "never open a
position we cannot monitor" -- by probing the source the watchdog will actually use
(`watchdog.resolve_price_source(configured_price_source_name())`). It was reached only
through `quote_asset_rate`, which is called from exactly two places
(`viability.py:2083` for robinhood and `viability.py:2383` for bsc). **There was no
Solana caller**, which is the structural reason unpriceable Solana positions were opened.

MEASURED on the live box 2026-09-22 (operator brief, not re-measured here):

* direct probe of every live position right after the router fallback landed:
  **4 of 4 priceable, 0 blind**;
* ten minutes later, 10 open positions: **blind per tick min 0, max 7, average 3.2**.

Blindness that rises while the backlog is being cleared is new entries arriving
unpriceable, not a queue draining. The four originally-blind positions were
letsbonk.fun / Raydium Launchpad launches quoted in BONK rather than SOL:
`scanner.curve_from_payload` refuses them with `non_sol_quote:<mint>` and DexScreener has
no pair while they are on a curve. `evm_price._sol_price_source` now puts an executable
Jupiter router quote behind those two, so this gate is expected to refuse only the
residue the router also cannot route -- see
`test_a_mint_the_router_can_price_is_not_refused`, which is the control that stops this
from becoming a filter.

WHERE THE CHECK LIVES, and why: `engine.handoff`. It is the single funnel every entry
passes -- `run_once` calls it for each `Action.ENTER` decision and `submit_agent_intent`
calls it for an agent-requested one -- and it is reached ONLY on ENTER, so it runs at the
entry rate rather than the scan rate. `decide()` would have been the wrong place: it runs
once per signal, and a provider call per candidate is what the operator brief rules out.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.schemas import (
    Action,
    Chain,
    Decision,
    Grade,
    Lane,
    LaneMode,
    Signal,
)
from kaiba.execution import engine
from kaiba.execution import viability as V

#: The live mint from the operator brief: a ``letsbonk.fun`` / ``raydium_launchpad`` coin
#: whose quote mint is BONK, which is why both the curve reader and DexScreener refuse it.
BONK_QUOTED_MINT = "FnkzzU3t55RQNbjc6Jynn7LHrPEbRTnBenvHQwJCebYB"
#: An ordinary pump.fun mint, used as the control.
PRICEABLE_MINT = "GMFCWQv8CfnjGR2xCJebyxqSsCmyMo31pXcCEnfDpump"
BSC_TOKEN = "0x18f91c1d14b0d4c528fb746836948944e6486666"


@pytest.fixture(autouse=True)
def _clean():
    V.reset_protectable_cache()
    yield
    V.reset_protectable_cache()


def protection_path(monkeypatch, answers: dict[str, Decimal | None], calls: list | None = None):
    """Stand in for the source the watchdog resolves, patched where the gate reads it.

    Patched at ``watchdog.resolve_price_source`` for the reason
    ``tests/test_quote_asset_protectable.py`` gives: a provider underneath it is a
    different answer, because the configured ``venue`` source falls back through the
    curve, the pair and now the router.

    ``answers`` is keyed by the mint **exactly as written**. That is the point of
    ``test_the_mint_reaches_the_protection_path_unchanged``: base58 is case-sensitive and
    a case-folded key would silently miss every Solana token.
    """
    from kaiba.execution import watchdog as wd

    class _Q:
        def __init__(self, price):
            self.price_usd = price
            self.usable = price is not None and price > 0
            self.note = None

    class _Src:
        def quote(self, chain, token):
            if calls is not None:
                calls.append((chain, token))
            return _Q(answers.get(token))

    monkeypatch.setattr(wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Src())


def _decision(
    chain: Chain = Chain.SOL,
    token: str = BONK_QUOTED_MINT,
    mode: LaneMode = LaneMode.LIVE,
) -> Decision:
    return Decision(
        decision_id="dec_" + token[:16],
        ts_ms=1_700_000_000_000,
        lane=Lane.SM_TRENCHES,
        mode=mode,
        chain=chain,
        token=token,
        action=Action.ENTER,
        thesis="test entry",
        confidence=0.9,
        dossier_grade=Grade.B,
        size_base_units=50_000_000,
        invalidation="the stop, which is the thing this gate is about",
    )


def _signal(decision: Decision) -> Signal:
    return Signal(
        signal_id="sig_" + decision.token[:16],
        lane=decision.lane,
        chain=decision.chain,
        token=decision.token,
        strength=0.9,
        reasons=["test"],
        window_s=300,
        created_ms=decision.ts_ms,
        payload={},
    )


def _outcome(conn, decision_id: str) -> dict:
    row = conn.execute(
        "SELECT outcome, note, order_id, position_id FROM decision_outcomes WHERE decision_id=?",
        (decision_id,),
    ).fetchone()
    return dict(row) if row else {}


def _trip(monkeypatch):
    """Make everything downstream of the gate explode, so only the gate can pass a token.

    Without this a refusal test could pass because the paper broker happened to refuse
    for its own reasons, which is the failure mode ``test_quote_asset_protectable`` calls
    out with its "the control" comment.
    """
    def _boom(*a, **k):  # pragma: no cover - reached only when the gate fails open
        raise AssertionError("handoff continued past the protectability gate")

    monkeypatch.setattr(engine, "_plan_live_order", _boom)
    monkeypatch.setattr(engine, "load_dossier", _boom)


# ------------------------------------------------------------------- the refusal, first


@pytest.mark.parametrize("mode", [LaneMode.LIVE, LaneMode.CANARY, LaneMode.SHADOW])
def test_a_mint_the_protection_path_cannot_price_is_refused(tmp_db, monkeypatch, mode):
    """THE POINT OF THIS FILE. The entry stops before an order or a paper fill exists.

    Every mode, not just live: ``watchdog._open_positions`` selects on
    ``closed_ms IS NULL`` with no mode filter, so a shadow position is polled, counted in
    ``blind`` and counted again in ``blind_over_budget`` exactly like a funded one.
    """
    protection_path(monkeypatch, {BONK_QUOTED_MINT: None})
    _trip(monkeypatch)
    decision = _decision(mode=mode)

    order = engine.handoff(decision, _signal(decision), tmp_db)

    assert order is None
    got = _outcome(tmp_db, decision.decision_id)
    assert got["outcome"] == "abandoned", got
    assert got["order_id"] is None and got["position_id"] is None


def test_the_refusal_names_the_token_and_the_reason(tmp_db, monkeypatch):
    """An operator reading the outcome row must be able to act without a debugger."""
    protection_path(monkeypatch, {BONK_QUOTED_MINT: None})
    _trip(monkeypatch)
    decision = _decision()

    engine.handoff(decision, _signal(decision), tmp_db)

    note = _outcome(tmp_db, decision.decision_id)["note"]
    assert BONK_QUOTED_MINT in note, note
    assert "protect" in note.lower(), note
    assert "no_price" in note or "protection" in note, note


def test_the_refusal_is_on_the_event_bus(tmp_db, monkeypatch):
    """Countable, not just logged: "how many entries did this cost us" is the question."""
    protection_path(monkeypatch, {BONK_QUOTED_MINT: None})
    _trip(monkeypatch)
    decision = _decision()

    engine.handoff(decision, _signal(decision), tmp_db)

    rows = tmp_db.execute("SELECT kind, payload, subject FROM events ORDER BY id DESC").fetchall()
    hit = [r for r in rows if "entry_unprotectable" in (r["payload"] or "")]
    assert hit, [dict(r) for r in rows]
    assert hit[0]["subject"] == BONK_QUOTED_MINT
    assert BONK_QUOTED_MINT in hit[0]["payload"]


@pytest.mark.parametrize("bad", [None, Decimal(0), Decimal("-1")])
def test_a_missing_zero_or_negative_price_is_not_protection(tmp_db, monkeypatch, bad):
    protection_path(monkeypatch, {BONK_QUOTED_MINT: bad})
    _trip(monkeypatch)
    decision = _decision()
    assert engine.handoff(decision, _signal(decision), tmp_db) is None


# ------------------------------------------------------------------- it must not fail open


def test_a_raising_protection_source_refuses_the_entry(tmp_db, monkeypatch):
    """"We could not check" and "we cannot see it" have the same consequence."""
    from kaiba.execution import watchdog as wd

    class _Boom:
        def quote(self, chain, token):
            raise RuntimeError("provider down")

    monkeypatch.setattr(wd, "configured_price_source_name", lambda *a, **k: "venue")
    monkeypatch.setattr(wd, "resolve_price_source", lambda name: _Boom())
    _trip(monkeypatch)
    decision = _decision()

    assert engine.handoff(decision, _signal(decision), tmp_db) is None
    assert _outcome(tmp_db, decision.decision_id)["outcome"] == "abandoned"


def test_an_unimportable_probe_refuses_the_entry(tmp_db, monkeypatch):
    """A gate that cannot run is not permission to trade."""
    def _explode(*a, **k):
        raise ImportError("viability is being edited")

    monkeypatch.setattr(engine, "_protectability_probe", _explode)
    _trip(monkeypatch)
    decision = _decision()

    assert engine.handoff(decision, _signal(decision), tmp_db) is None
    note = _outcome(tmp_db, decision.decision_id)["note"]
    assert "ImportError" in note, note


def test_a_probe_that_returns_nonsense_refuses_the_entry(tmp_db, monkeypatch):
    """Only an explicit True is a pass. ``None``/truthy-junk must not read as one."""
    monkeypatch.setattr(engine, "_protectability_probe", lambda *a, **k: (None, "shrug"))
    _trip(monkeypatch)
    decision = _decision()
    assert engine.handoff(decision, _signal(decision), tmp_db) is None


@pytest.mark.parametrize("empty", ["", "   "])
def test_an_entry_with_no_token_at_all_is_refused(tmp_db, monkeypatch, empty):
    """No address is not an address the watchdog can price.

    Found by mutation 2026-09-22: flipping ``quote_asset_is_protectable``'s empty-address
    branch from ``False`` to ``True`` survived this file. An empty ``Decision.token``
    should be impossible upstream, but "impossible" is how the blind positions got opened
    in the first place, and the failure mode here is a position with no stop.
    """
    calls: list = []
    protection_path(monkeypatch, {}, calls)
    _trip(monkeypatch)
    decision = _decision().model_copy(update={"token": empty})

    assert engine.handoff(decision, _signal(decision), tmp_db) is None
    assert _outcome(tmp_db, decision.decision_id)["outcome"] == "abandoned"
    assert calls == [], "an empty address must be refused without spending a provider call"


# ------------------------------------------------------------------- it refuses nothing else


def test_a_mint_the_router_can_price_is_not_refused(tmp_db, monkeypatch):
    """The control. A gate that refuses everything is as wrong as one that refuses nothing.

    The router fallback prices most of the previously-blind BONK-quoted launches, so this
    is the case that must still go through -- otherwise the gate is duplicating work the
    fallback already did and is costing entries for nothing.
    """
    protection_path(monkeypatch, {BONK_QUOTED_MINT: Decimal("0.0000412")})
    seen: list = []
    monkeypatch.setattr(engine, "_plan_live_order", lambda d, c: seen.append(d) or "ORDER")
    decision = _decision()

    got = engine.handoff(decision, _signal(decision), tmp_db)

    assert got == "ORDER" and seen == [decision]
    assert _outcome(tmp_db, decision.decision_id) == {}, "a pass must write no refusal"


def test_an_evm_entry_is_probed_here_too(tmp_db, monkeypatch):
    """Updated 2026-09-23: this asserted EVM was NOT probed, and that was the bug.

    The original reasoning was that "EVM already has this gate, inside
    ``quote_asset_rate`` on its sizing path". True, and a different question:
    ``quote_asset_rate`` probes the QUOTE ASSET, and for a robinhood token quoted in
    native ETH the quote asset is ETH, which is always priceable. Nothing asked whether
    the watchdog could price the position's own token.

    MEASURED: a live robinhood sm-trenches position filled, went blind, stayed blind for
    585 s against a 300 s budget and tripped ``protection_blind_timeout``, which halts
    entries on EVERY chain. ``quote_asset_is_protectable`` answered ``(False,
    "protection_path_has_no_price")`` for that token at that moment while
    ``_protection_refusal`` answered ``None``.

    The second provider call the old note was avoiding costs one request at the entry
    rate. Not making it cost the agent all trading on all chains.
    """
    calls: list = []
    # The token must be PRICEABLE for the entry to proceed now. The old fixture passed
    # `{}` -- nothing priceable -- and still expected an order, which is precisely the
    # behaviour that let an unpriceable robinhood position open.
    protection_path(monkeypatch, {BSC_TOKEN: Decimal("0.00031")}, calls)
    monkeypatch.setattr(engine, "_plan_live_order", lambda d, c: "ORDER")
    decision = _decision(chain=Chain.BSC, token=BSC_TOKEN)

    assert engine.handoff(decision, _signal(decision), tmp_db) == "ORDER"
    assert calls == [(Chain.BSC, BSC_TOKEN)], f"an EVM entry went unprobed: {calls}"


def test_an_unpriceable_evm_entry_is_refused(tmp_db, monkeypatch):
    """The other half: EVM must now REFUSE what it cannot price, as Solana already did."""
    calls: list = []
    protection_path(monkeypatch, {}, calls)
    monkeypatch.setattr(engine, "_plan_live_order", lambda d, c: "ORDER")
    decision = _decision(chain=Chain.BSC, token=BSC_TOKEN)

    assert engine.handoff(decision, _signal(decision), tmp_db) is None
    assert calls == [(Chain.BSC, BSC_TOKEN)]


# ------------------------------------------------------- base58 is case-sensitive


def test_the_mint_reaches_the_protection_path_unchanged(tmp_db, monkeypatch):
    """The trap that would have made this gate refuse every Solana entry.

    ``quote_asset_is_protectable`` lower-cased its argument before handing it to
    ``source.quote`` -- correct for EVM hex, catastrophic for base58. A lower-cased mint
    matches no ``tokens`` row, no ``curve_snapshots`` row, no DexScreener pair and no
    Jupiter route, so the probe would have failed for every mint and the gate would have
    blocked 100% of Solana entries while looking like it was working.
    """
    calls: list = []
    protection_path(monkeypatch, {BONK_QUOTED_MINT: Decimal("0.0000412")}, calls)
    monkeypatch.setattr(engine, "_plan_live_order", lambda d, c: "ORDER")
    decision = _decision()

    engine.handoff(decision, _signal(decision), tmp_db)

    assert calls == [(Chain.SOL, BONK_QUOTED_MINT)], calls
    assert any(c.isupper() for c in calls[0][1]), "the fixture mint must exercise the case"


def test_the_verdict_itself_preserves_base58_and_still_folds_evm_hex():
    """Both halves, at the function rather than through the engine."""
    assert V._protectable_key(Chain.SOL, BONK_QUOTED_MINT) == BONK_QUOTED_MINT
    assert V._protectable_key(Chain.BSC, BSC_TOKEN.upper().replace("0X", "0x")) == BSC_TOKEN


def test_two_case_spellings_of_one_evm_asset_share_a_verdict(monkeypatch):
    """The EVM cache behaviour the old ``.lower()`` provided must survive the fix."""
    calls: list = []
    protection_path(monkeypatch, {BSC_TOKEN: Decimal("1.0")}, calls)
    V.quote_asset_is_protectable(Chain.BSC, BSC_TOKEN)
    V.quote_asset_is_protectable(Chain.BSC, BSC_TOKEN.upper().replace("0X", "0x"))
    assert len(calls) == 1, calls


# ------------------------------------------------------------------- entry path, not scan


def test_deciding_a_signal_never_probes_the_protection_path(tmp_db, monkeypatch):
    """Entries run 4-10 an hour; scanning runs hundreds. The probe belongs to the entry.

    ``decide`` is called once per signal by ``run_once``; ``handoff`` is called only for
    an ``Action.ENTER`` decision. This pins that the probe is on the second one.
    """
    calls: list = []
    protection_path(monkeypatch, {PRICEABLE_MINT: None}, calls)
    decision = _decision(token=PRICEABLE_MINT)

    engine.decide(_signal(decision), tmp_db)

    assert calls == [], f"decide() reached the protection probe: {calls}"


def test_the_verdict_is_probed_once_per_mint_across_entries(tmp_db, monkeypatch):
    """Two entries on the same mint cost one probe: the gate reuses the shared cache."""
    calls: list = []
    protection_path(monkeypatch, {BONK_QUOTED_MINT: None}, calls)
    _trip(monkeypatch)
    for i in range(3):
        d = _decision()
        d = d.model_copy(update={"decision_id": f"dec_{i}"})
        engine.handoff(d, _signal(d), tmp_db)
    assert len(calls) == 1, calls


def test_a_refusal_is_rechecked_sooner_than_a_pass():
    """Inherited, and it is load-bearing here: a 429 must not lock a mint out for 15 min."""
    assert V.PROTECTABLE_RETRY_S < V.PROTECTABLE_TTL_S
