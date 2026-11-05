from types import SimpleNamespace

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import EvidenceBasis, Receipt
from kaiba.learning import gmgn_forward_quotes as study
from kaiba.providers.gmgn_cli import GmgnResult, digest

MINT = "A" * 32


def quote(in_mint, out_mint, amount, at, *, out=200, minimum=190, sol_cost=5):
    data = {
        "input_token": in_mint, "output_token": out_mint, "input_amount": str(amount),
        "output_amount": str(out), "min_output_amount": str(minimum),
        "tx": {
            "sol_cost": str(sol_cost), "raw_tx": {"prioritizationFeeLamports": 10},
            "quote": {"inputMint": in_mint, "outputMint": out_mint, "inAmount": str(amount),
                      "outAmount": str(out), "otherAmountThreshold": str(minimum),
                      "swapMode": "ExactIn", "slippageBps": 100, "platformFee": "7"},
        },
    }
    return GmgnResult(data, Receipt(provider="gmgn", endpoint="quote", observed_at_ms=at,
                                   request_digest="request", response_digest=digest(data)))


def test_bind_checks_identity_freshness_and_retains_unclassified_costs():
    raw = quote(study.SOL, MINT, 100, 25)
    assert study.bind_quote(raw, study.SOL, MINT, 100, 20, 30)["platform_fee"] == 7
    assert study.bind_quote(raw, study.SOL, MINT, 101, 20, 30)["status"] == "identity_mismatch"
    assert study.bind_quote(raw, study.SOL, MINT, 100, 26, 30)["status"] == "identity_mismatch"
    raw.data["tx"]["quote"]["otherAmountThreshold"] = "100"
    assert study.bind_quote(raw, study.SOL, MINT, 100, 20, 30)["status"] == "identity_mismatch"
    stale = raw.receipt.model_copy(update={"basis": EvidenceBasis.CACHED})
    assert study.bind_quote(GmgnResult(raw.data, stale), study.SOL, MINT, 100, 20, 30) == {
        "status": "unavailable"
    }


def test_late_checkpoint_waits_for_actual_close_even_past_five_minutes():
    row = {"opened": 100, "quotes": {"buy": {}, "timed": {}}}
    assert study.due_stage(row, 500_100, None) is None
    assert study.due_stage(row, 500_100, 499_100) is None
    assert study.due_stage(row, 544_100, 499_100) == ("after_close", 589_100)
    assert study.due_stage(row, 589_100, 499_100) is None


@pytest.mark.parametrize("stop", (False, True))
def test_cycle_uses_both_sizes_and_exact_buy_minimum_after_exit(stop):
    clock = SimpleNamespace(now=1_000_000)
    state = {"day": 0, "attempts": 0, "rows": []}
    calls = []
    saved = []

    def send(**kwargs):
        assert kwargs["priority"] is Priority.RESEARCH
        calls.append(kwargs)
        assert saved[-1] == state["attempts"]
        if stop:
            return GmgnResult(None, Receipt(provider="gmgn", endpoint="quote"))
        return quote(kwargs["input_token"], kwargs["output_token"], kwargs["amount"], clock.now)

    class Reader:
        def q(self, sql, _args=()):
            return [(clock.now - 45_000,)] if "SELECT closed_ms" in sql else []

        def rows(self, _sql, _args):
            return [{"position_id": "a", "token": MINT, "opened_ms": clock.now, "closed_ms": None}]

    def tick():
        return study.cycle(state, Reader(), "wallet", lambda: saved.append(state["attempts"]),
                           clock=lambda: clock.now, quote=send)

    assert tick() == "buy"
    assert [c["amount"] for c in calls] == ([study.SIZES[0]] if stop else list(study.SIZES))
    assert state["attempts"] == (1 if stop else 2)
    assert tick() == "waiting_for_checkpoint"
    clock.now += study.MIN_LATE_EXIT
    assert tick() == "after_close"
    assert [c["amount"] for c in calls][2:] == ([] if stop else [190, 190])


def test_unresolved_orders_and_exhausted_budget_make_zero_provider_calls():
    state = {"day": 0, "attempts": study.DAILY_ATTEMPTS, "rows": []}
    assert study.cycle(state, None, "wallet", lambda: None, clock=lambda: 1,
                       quote=lambda **_: pytest.fail("provider called")) == "daily_quote_cap"
    state["attempts"] = 0
    reader = SimpleNamespace(q=lambda *_: [(1,)])
    assert study.cycle(state, reader, "wallet", lambda: None, clock=lambda: 1,
                       quote=lambda **_: pytest.fail("provider called")) == "unresolved_order"


def test_stress_return_requires_later_quote_for_same_full_minimum_amount():
    buy = {"status": "quoted", "minimum": 80, "sol_cost": 105,
           "receipt": {"observed_at_ms": 100}}
    sell = {"status": "quoted", "input": 80, "minimum": 115, "sol_cost": 3,
            "receipt": {"observed_at_ms": 200}}
    assert study.stress_return(100, buy, sell) == 0.04
    assert study.stress_return(100, buy, dict(sell, input=81)) is None
    assert study.stress_return(100, buy, dict(sell, sol_cost=None)) is None


def test_retests_use_disjoint_frozen_blocks():
    size = study.SIZES[0]
    buy = {"status": "quoted", "minimum": 80, "sol_cost": size,
           "receipt": {"observed_at_ms": 100}}
    sell = {"status": "quoted", "input": 80, "minimum": size * 2, "sol_cost": 3,
            "receipt": {"observed_at_ms": 200}}

    def rows(n):
        return [{"opened": i * study.DAY, "quotes": {
            "buy": {str(size): buy}, "timed": {str(size): dict(sell, minimum=size)},
            "after_close": {str(size): sell},
        }} for i in range(n)]

    def trials(n):
        return study.report({"rows": rows(n), "attempts": 0})["sizes"][str(size)]["trials"]

    assert trials(39) == []
    assert trials(40) == trials(79)
    assert trials(40)[0]["status"] == "quote_stress_pass"
    assert trials(80)[0] == trials(40)[0]
    assert trials(80)[1]["one_sided_alpha"] < trials(40)[0]["one_sided_alpha"]
