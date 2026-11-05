"""LaneConfig.live_admission: live-only limits that paper never reads (SOL-LIVE-ADMISSION, 2026-11-03).

The cases are the ones Codex's review asked for: the lane runs live only on the listed chain
and stays paper elsewhere, its hard per-order cap holds after the pool band (Codex's
counterexample: a 0.056 SOL ticket raised to the 0.079 economic floor and allowed), the
owner's $100k market-cap floor refuses unknown, stale and low caps, and the executor refuses
an off-list chain or an over-cap amount before transport. Each refusal has a positive control.
"""

from __future__ import annotations

import json

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, Order, Side, now_ms
from kaiba.execution.executor import ExecutionRefused, _check_mode
from kaiba.execution.risk import RiskGate, zero_size_cause
from tests.test_risk import TOKEN, seed_depth, seed_dossier, write_risk  # noqa: F401

SOL, BSC = Chain.SOL, Chain.BSC
LANE = Lane.SM_TRENCHES
BANKROLL = 3_294_269_853
CAP = 56_000_000  # the ticket Codex and the lead agreed
WIDE_CAP = 100_000_000  # above the seeded pool's 0.079 SOL economic floor

SOL_BUDGET = {
    "bankroll_base_units": BANKROLL,
    "max_position_base_units": 100_000_000,
    "min_position_base_units": 50_000_000,
    "gas_reserve_base_units": 50_000_000,
    "daily_loss_stop_base_units": 329_426_985,
    "max_exposure_pct": 90.0,
    "drawdown_recovery": False,
    "size_by_market_cap": [
        {"min_market_cap_usd": 0, "size_base_units": 100_000_000},
        {"min_market_cap_usd": 100_000, "size_base_units": 250_000_000},
    ],
}
BOUNDS = {"max_size_pct_bankroll": 30.0, "max_total_exposure_pct": 70.0}


def admission(cap: int | None = CAP, floor: float = 100_000.0, chains=("sol",)) -> dict:
    caps = {} if cap is None else {"sol": cap}
    return {"chains": list(chains), "max_position_base_units": caps,
            "min_market_cap_usd": floor, "market_cap_max_age_s": 600}


def configure(write_risk, *, mode: str = "live", live_admission: dict | None = None,  # noqa: F811
              bsc_enabled: bool = True) -> None:
    lane = {"mode": mode, "size_pct_min": 26.0, "size_pct_max": 29.0, "chains": ["sol", "bsc"]}
    if live_admission is not None:
        lane["params"] = {"live_admission": live_admission}
    write_risk(bounds=BOUNDS, chains={"sol": SOL_BUDGET, "bsc": {"enabled": bsc_enabled}},
               lanes={LANE.value: lane})


def seed_token_info(circulating="1000000000", price="0.0001", *, age_ms: int = 0, token: str = TOKEN,
                    total="1000000000", data: dict | None = None) -> None:
    """A cached GMGN token-info response, shaped like the box's (no usd_market_cap field)."""
    from kaiba.providers import _http

    path = _http.cache_path("gmgn", f"gmgn-cli token info --address {token} --chain sol --raw")
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        data = {"address": token, "price": price, "circulating_supply": circulating, "total_supply": total}
    path.write_text(json.dumps({"data": data, "fetched_ms": now_ms() - age_ms}), encoding="utf-8")


def buy(chain: Chain, amount: int) -> Order:
    return Order(order_id="ord_test", chain=chain, token=TOKEN, side=Side.BUY, lane=LANE,
                 mode=LaneMode.LIVE, input_token="native", output_token=TOKEN,
                 amount_in=amount, min_out=1, slippage_bps=500)


# --- mode: live on the listed chain, paper everywhere else --------------------------------

def test_without_live_admission_the_mode_is_unchanged(write_risk):  # noqa: F811
    configure(write_risk)
    cfg = load_risk()
    assert cfg.effective_mode(LANE, SOL) is LaneMode.LIVE
    assert cfg.effective_mode(LANE, BSC) is LaneMode.LIVE
    assert cfg.live_admission_cap(LANE, SOL) is None


def test_an_unlisted_chain_runs_as_shadow(write_risk):  # noqa: F811
    configure(write_risk, live_admission=admission())
    cfg = load_risk()
    assert cfg.effective_mode(LANE, SOL) is LaneMode.LIVE
    assert cfg.effective_mode(LANE, BSC) is LaneMode.SHADOW
    assert cfg.effective_mode(LANE) is LaneMode.LIVE  # a display without a chain
    assert cfg.effective_mode(Lane.KOL_FADE, BSC) is LaneMode.LIVE  # other lanes untouched


def test_paper_on_the_unlisted_chain_matches_the_lane_in_shadow(write_risk, tmp_db):  # noqa: F811
    """BSC under a live lane with live_admission=[sol] is sized and gated exactly as the same
    lane in SHADOW: its paper record keeps its frozen rules. BSC is switched off for real
    money here so the control can tell the two paths apart: the live path refuses
    `chain_disabled`, the paper path does not."""
    configure(write_risk, mode="shadow", bsc_enabled=False)
    gate = RiskGate()
    want_size = gate.position_size(BSC, LANE, 95.0, tmp_db, token=TOKEN)
    want = gate.check_entry(BSC, LANE, want_size, tmp_db, token=TOKEN)

    configure(write_risk, live_admission=admission(), bsc_enabled=False)
    gate = RiskGate()
    got_size = gate.position_size(BSC, LANE, 95.0, tmp_db, token=TOKEN)
    got = gate.check_entry(BSC, LANE, got_size, tmp_db, token=TOKEN)
    assert got_size == want_size
    assert (got.allowed, got.reason) == (want.allowed, want.reason)
    assert want.reason != "chain_disabled"

    configure(write_risk, bsc_enabled=False)  # control: live, no live_admission
    assert RiskGate().check_entry(BSC, LANE, want_size, tmp_db, token=TOKEN).reason == "chain_disabled"


def test_paper_sizing_on_the_listed_chain_ignores_the_cap(write_risk, tmp_db):  # noqa: F811
    """The cap is live-only: the same lane in SHADOW sizes its paper ticket as before."""
    seed_dossier(tmp_db, 250_000)
    configure(write_risk, mode="shadow")
    before = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    configure(write_risk, mode="shadow", live_admission=admission())
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == before > CAP


# --- the hard cap holds after the band ------------------------------------------------------

def test_codex_counterexample_band_floor_above_the_cap_refuses(write_risk, tmp_db):  # noqa: F811
    """Real seeded pool, no band mock: its economic floor (~0.079 SOL) is above the 0.056 cap.
    The sizer refuses with a named cause instead of returning the floor, and the gate refuses
    the floor-sized ticket."""
    seed_dossier(tmp_db, 250_000)
    configure(write_risk, live_admission=admission())
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    assert zero_size_cause(tmp_db, SOL, LANE, TOKEN).startswith("live_admission_cap_below_band:")
    verdict = gate.check_entry(SOL, LANE, 79_368_186, tmp_db, token=TOKEN)
    assert not verdict.allowed and verdict.reason.startswith("live_admission_size_above_cap:")


def test_positive_control_a_cap_above_the_floor_sizes_and_admits(write_risk, tmp_db):  # noqa: F811
    seed_dossier(tmp_db, 250_000)
    seed_token_info(price="0.00025")  # $250k circulating
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    gate = RiskGate()
    size = gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    assert 0 < size <= WIDE_CAP
    verdict = gate.check_entry(SOL, LANE, size, tmp_db, token=TOKEN)
    assert verdict.allowed, verdict.reason
    over = gate.check_entry(SOL, LANE, WIDE_CAP + 1, tmp_db, token=TOKEN)
    assert not over.allowed and over.reason == f"live_admission_size_above_cap:{WIDE_CAP + 1}>{WIDE_CAP}"


def test_without_live_admission_the_same_lane_sizes_to_the_rung(write_risk, tmp_db):  # noqa: F811
    """The control the cap exists for: unrestricted, this lane sends the 0.25 SOL rung."""
    seed_dossier(tmp_db, 250_000)
    configure(write_risk)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 250_000_000


def test_a_listed_chain_without_a_cap_refuses(write_risk, tmp_db):  # noqa: F811
    seed_dossier(tmp_db, 250_000)
    configure(write_risk, live_admission=admission(cap=None))
    gate = RiskGate()
    assert gate.position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    assert zero_size_cause(tmp_db, SOL, LANE, TOKEN) == "live_admission_no_cap"
    verdict = gate.check_entry(SOL, LANE, 60_000_000, tmp_db, token=TOKEN)
    assert not verdict.allowed and verdict.reason == "live_admission_no_cap:sol"


# --- the owner's $100k circulating market-cap floor ---------------------------------------

@pytest.mark.parametrize("info, reason", [
    (None, "live_admission_market_cap_unverified:no_token_info"),
    ({"age_ms": 601_000}, "live_admission_market_cap_unverified:stale:601s"),
    ({"age_ms": -3_600_000}, "live_admission_market_cap_unverified:future_clock"),
    ({"circulating": None}, "live_admission_market_cap_unverified:no_circulating_supply_or_price"),
    ({"circulating": "0"}, "live_admission_market_cap_unverified:no_circulating_supply_or_price"),
    ({"price": "NaN"}, "live_admission_market_cap_unverified:no_circulating_supply_or_price"),
    ({"price": "0.000099999"}, "live_admission_market_cap_below_floor:99999<100000"),
])
def test_the_floor_refuses_unverified_stale_future_and_low_caps(write_risk, tmp_db, info, reason):  # noqa: F811
    seed_dossier(tmp_db, 2_500_000)  # a dossier that would pass: the floor must not read it
    if info is not None:
        seed_token_info(**info)
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN)
    assert not verdict.allowed and verdict.reason == reason


def test_the_floor_binds_the_response_to_the_token(write_risk, tmp_db):  # noqa: F811
    """Codex: another token's response stored under this token's key must not pass."""
    seed_token_info(data={"address": "OtherMint1111111111111111111111111111111111",
                          "price": "1", "circulating_supply": "1000000000"})
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN)
    assert verdict.reason == "live_admission_market_cap_unverified:token_info_address_mismatch"


def test_the_floor_reads_gmgns_recorded_nested_shape(write_risk, tmp_db):  # noqa: F811
    """The genuine recorded response nests price under price.price; same response, so it counts."""
    import pathlib

    rec = json.loads(pathlib.Path("tests/fixtures/gmgn/token_info.json").read_text(encoding="utf-8"))
    data = json.loads(rec["stdout"])
    data = data.get("data", data)
    token = data["address"]
    seed_token_info(data=data, token=token)
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    from kaiba.execution.risk import verified_circulating_market_cap_usd

    usd, why = verified_circulating_market_cap_usd(SOL, token, max_age_ms=600_000)
    want = float(json.loads(json.dumps(data["circulating_supply"]))) * float(data["price"]["price"])
    assert why == "ok" and usd == pytest.approx(want, rel=1e-9) and usd > 100_000
    bad = dict(data, price=dict(data["price"], address="OtherMint1111111111111111111111111111111111"))
    seed_token_info(data=bad, token=token)
    assert verified_circulating_market_cap_usd(SOL, token, max_age_ms=600_000)[1] == "token_info_address_mismatch"


def test_the_floor_refuses_a_non_finite_cap(write_risk, tmp_db):  # noqa: F811
    seed_token_info(circulating="1e309", price="1")
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN)
    assert verdict.reason == "live_admission_market_cap_unverified:market_cap_not_finite"


def test_the_floor_never_uses_total_supply(write_risk, tmp_db):  # noqa: F811
    """Circulating 1M x $0.05 = $50k is below the floor even though total supply makes $50M."""
    seed_dossier(tmp_db, 2_500_000)
    seed_token_info(circulating="1000000", total="1000000000", price="0.05")
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN)
    assert verdict.reason == "live_admission_market_cap_below_floor:50000<100000"


def test_the_floor_admits_exactly_the_floor(write_risk, tmp_db):  # noqa: F811
    seed_dossier(tmp_db, 100_000)
    seed_token_info(circulating="1000000000", price="0.0001")  # exactly $100,000
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN)
    assert verdict.allowed, verdict.reason


def test_the_floor_never_touches_paper(write_risk, tmp_db):  # noqa: F811
    seed_dossier(tmp_db, 20_000)  # no token-info cache at all
    configure(write_risk, mode="shadow", live_admission=admission(cap=WIDE_CAP))
    verdict = RiskGate().check_entry(SOL, LANE, 100_000_000, tmp_db, token=TOKEN)
    assert not verdict.reason.startswith("live_admission"), verdict.reason


# --- the executor, the last look before transport -------------------------------------------

def test_the_executor_refuses_an_unlisted_chain_and_an_over_cap_amount(write_risk, tmp_db):  # noqa: F811
    seed_token_info()  # $100k circulating: the floor passes, so the cap and chain are tested
    configure(write_risk, live_admission=admission())
    _check_mode(buy(SOL, CAP))  # positive control: at the cap, on the listed chain
    with pytest.raises(ExecutionRefused, match="live_admission_size_above_cap"):
        _check_mode(buy(SOL, CAP + 1))
    with pytest.raises(ExecutionRefused, match="is shadow on bsc"):
        _check_mode(buy(BSC, 1))


def test_the_executor_rechecks_the_floor_at_send(write_risk, tmp_db):  # noqa: F811
    """Codex's decision-to-send case: admitted at $200k, the cap falls to $50k before the
    planned order is sent. The executor refuses it; a stale reading is refused too."""
    seed_dossier(tmp_db, 200_000)
    seed_token_info(price="0.0002")  # $200k
    configure(write_risk, live_admission=admission(cap=WIDE_CAP))
    gate = RiskGate()
    assert gate.check_entry(SOL, LANE, 80_000_000, tmp_db, token=TOKEN).allowed
    _check_mode(buy(SOL, 80_000_000))

    seed_token_info(price="0.00005")  # $50k
    with pytest.raises(ExecutionRefused, match="live_admission_market_cap_below_floor:50000<100000"):
        _check_mode(buy(SOL, 80_000_000))
    seed_token_info(price="0.0002", age_ms=601_000)
    with pytest.raises(ExecutionRefused, match="market_cap_unverified:stale"):
        _check_mode(buy(SOL, 80_000_000))


def test_the_executor_never_checks_a_sell(write_risk, tmp_db):  # noqa: F811
    """Exits are never gated: a sell on an unverifiable token still goes out."""
    configure(write_risk, live_admission=admission())
    sell = buy(SOL, 10 * CAP).model_copy(update={"side": Side.SELL})
    _check_mode(sell)


def test_the_executor_is_unchanged_without_live_admission(write_risk):  # noqa: F811
    configure(write_risk)
    _check_mode(buy(SOL, 10 * CAP))
    _check_mode(buy(BSC, 1))


def test_save_risk_keeps_live_admission(write_risk, tmp_path):  # noqa: F811
    configure(write_risk, live_admission=admission())
    out = save_risk(load_risk(), tmp_path / "round_trip.yaml")
    back = load_risk(out).lane(LANE).live_admission
    assert back is not None and back.chains == [SOL]
    assert back.max_position_base_units == {SOL: CAP} and back.min_market_cap_usd == 100_000.0


def test_an_older_lane_model_cannot_strip_it(write_risk, tmp_path):  # noqa: F811
    """Codex: a long-lived writer on the pre-change LaneConfig must not drop the block on its
    next save. The old model had no live_admission field; params round-trip as a dict."""
    import yaml
    from pydantic import BaseModel, Field

    class OldLaneConfig(BaseModel):  # HEAD f8c656c's LaneConfig, verbatim fields
        mode: LaneMode = LaneMode.SHADOW
        size_pct_min: float = 0.25
        size_pct_max: float = 2.0
        chains: list[Chain] = Field(default_factory=list)
        params: dict = Field(default_factory=dict)

    import os

    configure(write_risk, live_admission=admission())
    raw = yaml.safe_load(open(os.environ["KAIBA_RISK_PATH"], encoding="utf-8"))
    old = OldLaneConfig.model_validate(raw["lanes"][LANE.value])
    raw["lanes"][LANE.value] = old.model_dump(mode="json", exclude_none=True)
    resaved = tmp_path / "old_writer.yaml"
    resaved.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_risk(resaved).lane(LANE).live_admission.chains == [SOL]


def test_a_malformed_block_admits_nothing(write_risk):  # noqa: F811
    configure(write_risk, live_admission={"chains": "sol", "max_position_base_units": "lots"})
    cfg = load_risk()
    assert cfg.effective_mode(LANE, SOL) is LaneMode.SHADOW
    assert cfg.live_admission_cap(LANE, SOL) == 0
