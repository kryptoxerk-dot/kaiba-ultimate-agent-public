"""A planned buy must still satisfy live admission when it reaches transport."""

from __future__ import annotations

import json

import pytest
from base58 import b58encode

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side, now_ms
from kaiba.execution import executor
from kaiba.execution.risk import RiskGate
from kaiba.providers import _http
from tests.test_risk import seed_depth, write_risk  # noqa: F401 - shared fixture

# Synthetic public addresses; these are never operator wallets or chosen investments.
TOKEN = b58encode(bytes([3]) * 32).decode()
OTHER_TOKEN = b58encode(bytes([4]) * 32).decode()
WALLET = b58encode(bytes([1]) * 32).decode()
CAP = 100_000_000  # Above the seeded pool's economic floor, so the control can fill.
SIZE = 80_000_000


def token_info_cache(*, price: str = "0.0002", address: str = TOKEN, age_ms: int = 0) -> None:
    """A same-response circulating supply and nested price, with its real cache clock."""
    path = _http.cache_path("gmgn", f"gmgn-cli token info --address {TOKEN} --chain sol --raw")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "fetched_ms": now_ms() - age_ms,
            "data": {
                "address": address,
                "circulating_supply": "1000000000",
                "price": {"address": address, "price": price},
            },
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "degraded, expected_reason",
    [
        ({"price": "0.00005"}, r"live_admission_market_cap_below_floor:50000<100000"),
        ({"age_ms": 601_000}, r"live_admission_market_cap_unverified:stale:\d+s"),
        ({"address": OTHER_TOKEN}, r"live_admission_market_cap_unverified:token_info_address_mismatch"),
    ],
    ids=["market-cap-fell", "market-cap-expired", "different-token-response"],
)
def test_admitted_plan_rechecks_evidence_at_transport(
    tmp_db, write_risk, monkeypatch, degraded, expected_reason,  # noqa: F811 - imported fixture
):
    write_risk(
        bounds={"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0},
        chains={"sol": {
            "wallet": WALLET,
            "bankroll_base_units": 3_294_269_853,
            "max_position_base_units": CAP,
            "min_position_base_units": 50_000_000,
            "gas_reserve_base_units": 50_000_000,
            "daily_loss_stop_base_units": 329_426_985,
            "max_exposure_pct": 90.0,
        }},
        lanes={"sm-trenches": {
            "mode": "live", "chains": ["sol"],
            "params": {"live_admission": {
                "chains": ["sol"], "max_position_base_units": {"sol": CAP},
                "min_market_cap_usd": 100_000, "market_cap_max_age_s": 600,
            }},
        }},
    )
    sent = []

    def fake_transport(args, timeout_s=45, *, mutating=False):
        assert mutating is True
        sent.append(list(args))
        return {"data": {"order_id": "fixture-provider-order"}}

    # Preserve the real admission, signer policy and local limiter. Only the network
    # subprocess is replaced, before either the initial admission or submission.
    monkeypatch.setattr(executor, "_run_gmgn", fake_transport)
    seed_depth(tmp_db, token=TOKEN)
    token_info_cache()
    admitted = RiskGate().check_entry(Chain.SOL, Lane.SM_TRENCHES, SIZE, tmp_db, token=TOKEN)
    assert admitted.allowed, admitted.reason
    plan = executor.build_order(
        decision_id="fixture-live-admission-boundary", chain=Chain.SOL, token=TOKEN,
        side=Side.BUY, lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE,
        amount_in=SIZE, min_out=1000, slippage_bps=300,
    )
    executor._persist(plan, tmp_db)
    original = plan.model_dump()

    token_info_cache(**degraded)
    with pytest.raises(executor.ExecutionRefused, match=rf"^{expected_reason} \(at send\)$"):
        executor.submit_gmgn(plan, tmp_db)
    assert sent == []
    stored = tmp_db.execute("SELECT state FROM orders WHERE order_id=?", (plan.order_id,)).fetchone()
    assert stored["state"] == OrderState.PLANNED.value
    assert plan.model_dump() == original

    # The same plan, with no mode, size, age or identity changes, can proceed once
    # the evidence is valid again. An unconditional refusal would fail this control.
    token_info_cache()
    submitted = executor.submit_gmgn(plan, tmp_db)
    assert submitted.state is OrderState.SUBMITTED
    assert len(sent) == 1
    assert sent[0][sent[0].index("--amount") + 1] == str(SIZE)
    assert sent[0][sent[0].index("--output-token") + 1] == TOKEN
    assert plan.model_dump() == original
