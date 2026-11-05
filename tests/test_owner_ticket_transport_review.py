"""Owner tickets survive actual planning/admission/transport, including tier drift."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from base58 import b58encode

from kaiba.core.config import get_risk, save_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side, now_ms
from kaiba.execution import executor
from kaiba.execution.risk import RiskGate
from kaiba.providers import _http
from tests.test_risk import seed_dossier, write_risk  # noqa: F401

TOKEN = b58encode(bytes([3]) * 32).decode()
WALLET = b58encode(bytes([1]) * 32).decode()
SOL, LANE = Chain.SOL, Lane.SM_TRENCHES


def cached_cap(value: str) -> None:
    """Original-clock, same-token raw provider shape; never a provider request."""
    price = str(Decimal(value) / Decimal(1_000_000_000))
    path = _http.cache_path("gmgn", f"gmgn-cli token info --address {TOKEN} --chain sol --raw")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_ms": now_ms(), "data": {
        "address": TOKEN, "circulating_supply": "1000000000",
        "price": {"address": TOKEN, "price": price},
    }}), encoding="utf-8")


def configure(writer, *, reserve: int = 50_000_000):
    return writer(
        bounds={"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0},
        chains={"sol": {
            "wallet": WALLET, "bankroll_base_units": 3_294_269_853,
            "max_position_base_units": 100_000_000, "min_position_base_units": 50_000_000,
            "gas_reserve_base_units": reserve, "daily_loss_stop_base_units": 0,
            "max_exposure_pct": 90.0, "drawdown_recovery": False,
            "size_by_market_cap": [{"min_market_cap_usd": 0, "size_base_units": 100_000_000},
                                   {"min_market_cap_usd": 100_000, "size_base_units": 250_000_000}],
        }},
        lanes={"sm-trenches": {
            "mode": "live", "chains": ["sol", "bsc"], "size_pct_min": 0.25,
            "size_pct_max": 29.0, "params": {"live_admission": {
                "chains": ["sol"], "max_position_base_units": {"sol": 300_000_000},
                "min_market_cap_usd": 10_000, "market_cap_max_age_s": 600,
                "tickets": {"sol": [{"min_market_cap_usd": 10_000, "ticket_base_units": 100_000_000},
                                    {"min_market_cap_usd": 100_000, "ticket_base_units": 300_000_000}]},
            }},
        }},
    )


@pytest.mark.parametrize("original_cap, changed_cap, ticket", [
    ("10000", "100000", 100_000_000),
    ("100000", "99999.99", 300_000_000),
])
def test_tier_crossing_blocks_real_transport_then_same_plan_can_proceed(
    tmp_db, write_risk, monkeypatch, original_cap, changed_cap, ticket,  # noqa: F811
):
    configure(write_risk)
    # Deliberately conflicting legacy dossier/rung: its 0.25 SOL must not resize or
    # reject the owner's 0.3 SOL at the exact $100k boundary.
    seed_dossier(tmp_db, 250_000, token=TOKEN)
    cached_cap(original_cap)
    gate = RiskGate()
    assert [gate.position_size(SOL, LANE, score, tmp_db, token=TOKEN)
            for score in (70.0, 85.0, 99.0)] == [ticket] * 3
    assert gate.position_size(SOL, LANE, 69.9, tmp_db, token=TOKEN) == 0
    admitted = gate.check_entry(SOL, LANE, ticket, tmp_db, token=TOKEN)
    assert admitted.allowed, admitted.reason
    plan = executor.build_order(
        decision_id="fixture-owner-ticket-tier-drift", chain=SOL, token=TOKEN,
        side=Side.BUY, lane=LANE, mode=LaneMode.LIVE, amount_in=ticket,
        min_out=1000, slippage_bps=300,
    )
    executor._persist(plan, tmp_db)
    before = plan.model_dump()
    sent = []

    def fake_transport(args, timeout_s=45, *, mutating=False):
        assert mutating is True
        sent.append(list(args))
        return {"data": {"order_id": "fixture-owner-ticket-order"}}

    # No gate/policy/limiter mock: only the subprocess that could reach the network.
    monkeypatch.setattr(executor, "_run_gmgn", fake_transport)
    cached_cap(changed_cap)
    with pytest.raises(executor.ExecutionRefused, match="ticket_mismatch"):
        executor.submit_gmgn(plan, tmp_db)
    assert sent == []
    assert plan.model_dump() == before
    assert tmp_db.execute("SELECT state FROM orders WHERE order_id=?", (plan.order_id,)).fetchone()["state"] == "planned"

    cached_cap(original_cap)
    result = executor.submit_gmgn(plan, tmp_db)
    assert result.state is OrderState.SUBMITTED
    assert len(sent) == 1
    assert sent[0][sent[0].index("--amount") + 1] == str(ticket)
    assert plan.model_dump() == before


def test_insufficient_cash_refuses_instead_of_sending_a_smaller_ticket(tmp_db, write_risk):  # noqa: F811
    path = configure(write_risk, reserve=3_000_000_000)
    seed_dossier(tmp_db, 250_000, token=TOKEN)
    cached_cap("100000")
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    verdict = gate.check_entry(SOL, LANE, 300_000_000, tmp_db, token=TOKEN)
    assert not verdict.allowed and verdict.reason.startswith("gas_reserve:"), verdict

    # Restore only available cash. The exact ticket, not a smaller percentage, returns.
    cfg = get_risk()
    cfg.chains[SOL].gas_reserve_base_units = 50_000_000
    save_risk(cfg, path)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 300_000_000
    assert RiskGate().check_entry(SOL, LANE, 300_000_000, tmp_db, token=TOKEN).allowed
