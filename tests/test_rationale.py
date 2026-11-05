"""Every entry and every exit has to say WHY, in terms a person can audit later.

The agent recorded which gate passed and what it did. It did not record the evidence a
post-mortem needs: which wallets, what the book looked like, why that size, what the plan
was -- and on the way out, what the price actually did while we held.

The load-bearing rule in this file is the contract one: an absent measurement reads as
UNKNOWN in the sentence, never as zero. A narrative that says "liquidity $0" when we could
not read the book is worse than no narrative, because it looks like evidence.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution import rationale


class FakeMeasure:
    def __init__(self, value, basis="provider_reported"):
        self.value = value
        self.basis = type("B", (), {"value": basis})()


class FakeDossier:
    def __init__(self, liquidity=None):
        self.liquidity_usd = FakeMeasure(liquidity) if liquidity is not None else None


class FakeSignal:
    def __init__(self, **kw):
        self.signal_id = kw.get("signal_id", "sig")
        self.lane = type("L", (), {"value": kw.get("lane", "sm-trenches")})()
        self.chain = kw.get("chain", "sol")
        self.token = kw.get("token", "tok")
        self.strength = kw.get("strength", 0.77)
        self.reasons = kw.get("reasons", ["4 smart wallets"])
        self.wallets = kw.get("wallets", ["walletAAAA1111", "walletBBBB2222"])
        self.entities = kw.get("entities", ["e1", "e2"])
        self.window_s = kw.get("window_s", 300)
        self.payload = kw.get("payload", {})


class FakePosition:
    def __init__(self, **kw):
        self.entry_price_usd = kw.get("entry")
        self.peak_price_usd = kw.get("peak")
        self.cost_native = kw.get("cost")
        self.proceeds_native = kw.get("proceeds")
        self.realized_native = kw.get("realized")


# ------------------------------------------------------------------ entry


def test_entry_names_the_wallets_and_the_book():
    r = rationale.entry_rationale(
        signal=FakeSignal(),
        dossier=FakeDossier(liquidity=Decimal("12500")),
        token_meta=type("T", (), {"launchpad": "pump.fun"})(),
        size_base_units=4_684_802_856_259_036,
    )
    why = r["why_entered"]
    assert "2 smart wallet(s)" in why
    assert "walletAAAA" in why, "the wallets behind the buy are not named"
    assert "2 independent entit" in why
    assert "pump.fun" in why
    assert "$12,500" in why
    assert r["evidence"]["smart_wallets"] == 2
    assert r["evidence"]["deploy"] == "launchpad"


def test_entry_says_manual_deploy_when_there_is_no_launchpad():
    r = rationale.entry_rationale(signal=FakeSignal(), dossier=FakeDossier(Decimal("9000")))
    assert "manual deploy" in r["why_entered"]
    assert r["evidence"]["deploy"] == "manual"


def test_unknown_liquidity_reads_as_unknown_not_zero():
    """THE CONTRACT RULE. `$0` would look like a measured empty book."""
    r = rationale.entry_rationale(signal=FakeSignal(), dossier=FakeDossier(None))
    assert "UNKNOWN" in r["why_entered"]
    assert "$0" not in r["why_entered"]
    assert r["evidence"]["liquidity_usd"] is None


def test_entry_states_the_plan_including_the_unprotected_band():
    r = rationale.entry_rationale(
        signal=FakeSignal(), dossier=FakeDossier(Decimal("20000")),
        plan={"stop_price_usd": Decimal("0.00001"), "first_tp_x": 2.0, "trail_arms_at_x": 2.0},
    )
    assert "hard stop" in r["plan"]
    assert "ONLY protection" in r["plan"], (
        "the plan does not say that nothing protects the position below the trailing rung, "
        "which is the single largest measured hole in this book"
    )


def test_a_lane_with_no_plan_says_so_rather_than_inventing_one():
    r = rationale.entry_rationale(signal=FakeSignal(), dossier=FakeDossier(Decimal("20000")))
    assert "not stated" in r["plan"]


def test_conviction_is_labelled_as_conviction_not_probability():
    """`strength` is lane conviction on 0..1 and has been mistaken for a score before."""
    r = rationale.entry_rationale(signal=FakeSignal(strength=0.77), dossier=FakeDossier(Decimal("1")))
    assert "not a win probability" in r["why_entered"]


def test_format_entry_is_readable():
    r = rationale.entry_rationale(signal=FakeSignal(), dossier=FakeDossier(Decimal("1000")),
                                  invalidation="source wallet sells")
    text = rationale.format_entry(r)
    assert text.startswith("ENTERED because ")
    assert "WRONG IF: source wallet sells" in text


# ------------------------------------------------------------------ exit


@pytest.mark.parametrize("reason,kind", [
    ("trailing_stop", "planned"),
    ("take_profit_2x", "planned"),
    ("trim", "planned"),
    ("stop_loss", "stopped"),
    ("emergency_loss", "fault"),
    ("rug:lp_-100.0pct", "fault"),
    ("dust_written_off", "fault"),
    (None, "unknown"),
])
def test_exit_is_classified(reason, kind):
    assert rationale.classify_exit(reason) == kind


def test_exit_reports_the_price_path():
    r = rationale.exit_rationale(
        position=FakePosition(entry=Decimal("0.0001"), peak=Decimal("0.00025"),
                              cost=100, proceeds=187, realized=87),
        reason="trailing_stop", exit_price_usd=Decimal("0.000187"), pct=100,
    )
    o = r["outcome"]
    assert o["classification"] == "planned"
    assert round(o["mfe_pct"]) == 150, "peak/entry is not being reported"
    assert round(o["return_pct_on_cost"]) == 87
    assert "peak" in r["why_exited"] and "exit" in r["why_exited"]


def test_a_stop_that_ran_first_says_the_move_was_given_back():
    """THE MEASURED HOLE: 27% of stopped-out fills reached +25% with nothing armed."""
    r = rationale.exit_rationale(
        position=FakePosition(entry=Decimal("100"), peak=Decimal("147"),
                              cost=100, proceeds=65, realized=-35),
        reason="stop_loss", exit_price_usd=Decimal("65"), pct=100,
    )
    assert "47.0%" in r["lesson"]
    assert "given back" in r["lesson"]


def test_a_stop_that_never_ran_says_it_was_the_entry():
    r = rationale.exit_rationale(
        position=FakePosition(entry=Decimal("100"), peak=Decimal("101"),
                              cost=100, proceeds=65, realized=-35),
        reason="stop_loss", exit_price_usd=Decimal("65"),
    )
    assert "never moved up" in r["lesson"]
    assert "not a stop set too tight" in r["lesson"]


def test_a_missing_price_record_does_not_invent_a_path():
    r = rationale.exit_rationale(position=FakePosition(), reason="stop_loss")
    assert "no usable price record" in r["why_exited"]
    assert r["outcome"]["mfe_pct"] is None
    assert r["outcome"]["return_pct_on_cost"] is None


def test_a_zero_entry_price_is_not_an_infinite_return():
    """A broken record must not read as a trade that made infinite money."""
    r = rationale.exit_rationale(
        position=FakePosition(entry=Decimal("0"), peak=Decimal("5"), cost=0, realized=0),
        reason="stop_loss", exit_price_usd=Decimal("3"),
    )
    assert r["outcome"]["mfe_pct"] is None
    assert r["outcome"]["return_pct_on_cost"] is None


def test_format_exit_is_readable():
    r = rationale.exit_rationale(
        position=FakePosition(entry=Decimal("100"), peak=Decimal("300"),
                              cost=100, proceeds=180, realized=80),
        reason="trailing_stop", exit_price_usd=Decimal("180"), pct=100,
    )
    text = rationale.format_exit(r)
    assert text.startswith("EXITED because ")
    assert "RESULT: 80.0% on cost" in text


def test_nothing_here_touches_the_database():
    """This module turns evidence into sentences; it must never be able to trade."""
    import inspect

    source = inspect.getsource(rationale)
    for forbidden in ("execute(", "commit(", "requests.", "httpx", "INSERT", "UPDATE "):
        assert forbidden not in source, f"rationale.py reaches for {forbidden!r}"
