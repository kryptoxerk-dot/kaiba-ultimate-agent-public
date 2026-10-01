"""``CurveState.snipe_tax_bps_at`` carries the MEASURED anti-sniper schedule, not a linear guess.

What is MEASURED and where it comes from:

* 9900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+ whole seconds since ``launchedAt`` for the
  ``(9900, 3)`` configuration every curve carries. The pons-tax line read it two independent
  ways (``currentSnipeTaxBps`` at every block of a live launch; the toll recovered from 913k
  real ``CurveBuy`` events, docs/research/line4-antisniper-tax-pons.md section 1) and
  re-sampled it from 1,210 early buys in
  ``tests/test_pons_tax_entry.py::MEASURED_TOLL_SAMPLE``. Nothing here re-derives it: this
  file pins ``robinhood.SNIPE_TAX_RUNGS_BPS`` to ``lanes.PONS_SNIPE_TAX_RUNGS_BPS`` and to
  that sample, so the ingest reader and the entry rule cannot drift apart.
* The value is constant within each whole second -- every block inside second ``k`` read
  rung ``k`` -- so between the rungs the schedule steps; it does not slide.

What is INVENTED: the inside of any other ``(start, seconds)`` configuration. The reader
fails closed there (the full starting toll inside the window) and labels the reading.

The consumer is ``viability.read_pons_venue``, which charges ``snipe_tax_bps_at`` per leg.
The mutation this file exists to catch is the linear decay it replaced: 9900 / 6600 / 3300,
10.7x high at t=1 s, which priced a 6.18% toll as 66%.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kaiba.core.schemas import Chain
from kaiba.execution import lanes, viability
from kaiba.ingest import robinhood as rh
from tests.test_pons_tax_entry import MEASURED_TOLL_SAMPLE
from tests.test_viability_evm import (
    BASE_RISK,
    MAX_POSITION,
    TOKEN,
    batch_results,
    register_token,
)

LAUNCHED_AT_S = 1_789_991_064  # a real ``launchedAt()``; the schedule is relative to it
FIXTURE = Path(__file__).parent / "fixtures" / "robinhood" / "launch.json"


def curve_state(start_bps: int = 9900, seconds: int = 3, *, launched_at_s: int = LAUNCHED_AT_S) -> rh.CurveState:
    return rh.CurveState(
        curve="0x3ccc7163beec0f8396b85847950e54de5507221f",
        token=TOKEN,
        quote_reserve=1_717_050_000_000_000_000,
        real_quote_reserve=37_050_000_000_000_000,
        sellable_tokens=692_708_008_336_557_301_351_570_594,
        reserved_tokens=285_714_285_714_285_714_285_714_285,
        graduation_threshold=4_200_000_000_000_000_000,
        launch_supply=10**27,
        fee_bps=100,
        launched_at_s=launched_at_s,
        quote_is_native=True,
        observed_ms=launched_at_s * 1000,
        snipe_tax_start_bps=start_bps,
        snipe_tax_seconds=seconds,
    )


# ---------------------------------------------------------------- the four measured points


@pytest.mark.parametrize("elapsed_s,toll_bps", [(0, 9900), (1, 618), (2, 19), (3, 0), (300, 0)])
def test_each_measured_point_reads_back_exactly(elapsed_s, toll_bps):
    state = curve_state()
    assert state.snipe_tax_shape_measured
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + elapsed_s) == toll_bps


def test_the_schedule_is_convex_not_linear():
    """The linear model said 6600 / 3300 at 1 / 2 s. Every measured block said 618 / 19."""
    state = curve_state()
    t0, t1, t2 = (state.snipe_tax_bps_at(LAUNCHED_AT_S + k) for k in range(3))
    assert (t1, t2) != (6600, 3300)
    assert t1 < t0 // 10 and t2 < t1 // 10  # each second sheds more than 90% of the toll
    # Convex: the drop ratio between rungs grows. Linear gives 1.5 then 2.0; measured gives
    # 16.0 then 32.5.
    assert t1 / t2 > t0 / t1 > 1


def test_between_the_rungs_the_schedule_steps_and_does_not_slide():
    """MEASURED: constant within each whole second. A blend at 1.5 s would say 318."""
    state = curve_state()
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + 1.5) == 618
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + 0.99) == 9900
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + 2.999) == 19
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + 1.5) != (618 + 19) // 2


def test_before_launch_the_full_toll_stands():
    state = curve_state()
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S - 1) == 9900
    assert state.snipe_tax_basis(LAUNCHED_AT_S - 1) == "pre_launch_full_toll"


# ---------------------------------------------------------------- one schedule, not two


def test_reader_and_lane_carry_the_same_measured_table():
    assert rh.SNIPE_TAX_RUNGS_BPS == lanes.PONS_SNIPE_TAX_RUNGS_BPS == {0: 9900, 1: 618, 2: 19}
    assert rh.SNIPE_TAX_MEASURED_CONFIG == (lanes.PONS_SNIPE_TAX_START_BPS, lanes.PONS_SNIPE_TAX_SECONDS)
    state = curve_state()
    for elapsed in range(0, 8):
        assert state.snipe_tax_bps_at(LAUNCHED_AT_S + elapsed) == lanes.pons_snipe_tax_bps(elapsed), elapsed


def test_reader_matches_every_paid_toll_in_the_measured_sample():
    """1,210 real early buys: the reader never under-charges one, and over-charges by at
    most the creator's pro-rata share."""
    state = curve_state()
    for elapsed, toll, _ in MEASURED_TOLL_SAMPLE:
        if toll == 0 and elapsed < state.snipe_tax_seconds:
            continue  # zero inside the window is the deployer's exemption, not the schedule
        scheduled = state.snipe_tax_bps_at(LAUNCHED_AT_S + elapsed)
        assert scheduled >= toll, (elapsed, toll, scheduled)
        assert scheduled - toll <= 500, (elapsed, toll, scheduled)


def test_a_recorded_live_launch_parses_onto_the_measured_schedule():
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    state = rh.parse_curve_state(fx["curve_state_raw"], curve=fx["curve"], token=fx["token"], quote_is_native=True)
    assert state is not None
    assert (state.snipe_tax_start_bps, state.snipe_tax_seconds) == rh.SNIPE_TAX_MEASURED_CONFIG
    assert state.snipe_tax_shape_measured
    assert [state.snipe_tax_bps_at(state.launched_at_s + k) for k in range(4)] == [9900, 618, 19, 0]


# ---------------------------------------------------------------- beyond the four points


@pytest.mark.parametrize("start_bps,seconds", [(5000, 10), (9900, 4), (9900, 2), (100, 3)])
def test_an_unmeasured_configuration_fails_closed_and_says_so(start_bps, seconds):
    """Only (9900, 3) was measured. Anything else charges the full toll inside its window."""
    state = curve_state(start_bps, seconds)
    assert not state.snipe_tax_shape_measured
    for elapsed in range(seconds):
        assert state.snipe_tax_bps_at(LAUNCHED_AT_S + elapsed) == start_bps, elapsed
        assert state.snipe_tax_basis(LAUNCHED_AT_S + elapsed).startswith("INVENTED")
    assert state.snipe_tax_bps_at(LAUNCHED_AT_S + seconds) == 0
    assert state.snipe_tax_basis(LAUNCHED_AT_S + seconds) == "window_elapsed"


@pytest.mark.parametrize("start_bps,seconds", [(0, 0), (0, 3), (9900, 0), (-1, 3)])
def test_a_zero_configuration_is_no_tax(start_bps, seconds):
    state = curve_state(start_bps, seconds)
    for at_s in (LAUNCHED_AT_S - 1, LAUNCHED_AT_S, LAUNCHED_AT_S + 1, LAUNCHED_AT_S + 60):
        assert state.snipe_tax_bps_at(at_s) == 0, at_s  # before launch too: no window, no wall
        assert state.snipe_tax_basis(at_s) == "no_tax_configured"


def test_the_measured_case_is_labelled_measured():
    state = curve_state()
    assert state.snipe_tax_basis(LAUNCHED_AT_S + 1) == "MEASURED_rungs"
    assert state.snipe_tax_basis(LAUNCHED_AT_S + 3) == "window_elapsed"


def test_the_curve_dict_carries_the_toll_and_its_basis():
    state = curve_state()
    curve, _ = rh.curve_from_state(state, at_ms=(LAUNCHED_AT_S + 1) * 1000 + 500)
    assert curve is not None
    assert curve["snipe_tax_bps_now"] == 618
    assert curve["snipe_tax_basis"] == "MEASURED_rungs"
    later, _ = rh.curve_from_state(state, at_ms=(LAUNCHED_AT_S + 60) * 1000)
    assert later is not None
    assert later["snipe_tax_bps_now"] == 0 and later["snipe_tax_basis"] == "window_elapsed"


# ---------------------------------------------------------------- the consumer


@pytest.fixture
def pons_venue(tmp_db, tmp_path, monkeypatch):
    """``read_pons_venue`` against a canned curve batch and the operator's risk file."""
    risk_path = tmp_path / "risk.yaml"
    risk_path.write_text(yaml.safe_dump(copy.deepcopy(BASE_RISK)), encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    canned: dict = {"results": batch_results()}

    def fake(calls, **kwargs):
        return rh.RpcResult(list(canned["results"]), True, None, 1)

    monkeypatch.setattr(rh, "rpc_batch", fake)
    register_token(tmp_db)
    viability.reset_venue_cache()
    yield canned, tmp_db
    viability.reset_venue_cache()


def test_read_pons_venue_charges_the_measured_toll_per_leg(pons_venue):
    """The toll at chain t=1 s is 618 bps a leg, not the 6,600 the linear decay charged --
    and inside the window the read refuses, carrying the rung it would have paid.

    The rungs are walked on the HEAD BLOCK's timestamp, the clock the curve itself uses.
    Wall-clock would price a buy a rung or two late: chain head lag is 0.87-2.0 s MEASURED.
    """
    canned, conn = pons_venue
    rh_chain = Chain.ROBINHOOD

    def at_chain(elapsed_s: int) -> None:
        viability.reset_venue_cache()
        canned["results"] = batch_results(
            creator_tax_bps=0, launched_at_s=LAUNCHED_AT_S, head_ts_s=LAUNCHED_AT_S + elapsed_s
        )

    at_chain(1)
    venue = viability.read_venue(rh_chain, TOKEN, conn)
    assert venue.tax_bps_per_leg == Decimal(618)
    assert "rung1_618bps" in venue.note and "chain_time" in venue.note, venue.note
    assert venue.tax_bps_per_leg / 10_000 == Decimal("0.0618")
    # Still not an entry, and the fee arm alone says why: 2 x (100 fee + 618 toll + 100
    # router) is a 16.4% round trip before gas, against the operator's 7% ceiling. The fix
    # corrects the price of the wall; it does not open it.
    verdict = viability.check_size(rh_chain, MAX_POSITION, conn, token=TOKEN)
    assert not verdict.ok and "rung1_618bps" in verdict.reason, verdict.reason

    at_chain(2)
    venue = viability.read_venue(rh_chain, TOKEN, conn)
    assert venue.tax_bps_per_leg == Decimal(19) and "rung2_19bps" in venue.note, venue.note

    at_chain(0)
    venue = viability.read_venue(rh_chain, TOKEN, conn)
    assert venue.tax_bps_per_leg == Decimal(9_900) and "rung0_9900bps" in venue.note, venue.note

    at_chain(3)
    venue = viability.read_venue(rh_chain, TOKEN, conn)
    assert venue.tax_bps_per_leg == Decimal(0)
    assert venue.depth.source.endswith(":snipe0:chain_time"), venue.depth.source


