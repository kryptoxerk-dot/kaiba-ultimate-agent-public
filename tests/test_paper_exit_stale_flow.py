"""A paper exit on a curve nobody has traded is not charged drift it cannot have (design
2026-10-21 step 1; ``DefaultExitSubmitter._paper_no_flow``).

MEASURED 2026-10-21 on the box (read-only): sol launch-snipe paper positions silent for
48-53 h were refused ``curve_drained_during_latency`` 104-114 times each. The broker drifts a
curve sell by ``DEFAULT_LATENCY_MS`` at the token's flow, and ``curve_price.measure_flow``
fell back to the two newest ``curve_snapshots`` -- the entry's own reads, seconds apart and
two days old -- with no lower time bound (Codex-pinned; board item in docs/TASKS.md).

The fixtures are three of those positions as the box holds them: the position's qty, the
curve's live virtual reserves (``kv reserve_clock:sol:<token>``, the watchdog's mirror), the
curve's ``virtual_sol - real_sol`` offset (from its snapshots), and its two entry snapshots.
Mints are fake. The other two of the five are not reproducible from the box: one closed
(``abandoned_unpriceable``) before this was written, and the other's mirrored reserves do not
build a valid curve with either offset its snapshots imply (``sol_reserves_inconsistent``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, jload
from kaiba.core.schemas import Chain, Lane, now_ms
from kaiba.execution import curve_price as cp
from kaiba.execution import pumpswap_price as pp
from kaiba.execution import watchdog as wd
from kaiba.execution.paper import DEFAULT_TIP_NATIVE, PaperBroker, fill_basis, load_position
from tests.test_watchdog import FakeSource, make_position, risk_file  # noqa: F401 - fixture by name

HOUR_MS = 3_600_000

#: name -> the box's numbers (2026-10-21 08:xx UTC reads). ``newest`` / ``older`` are the entry
#: snapshots as (ms before the newest, real_sol); ``paid`` is the fill the design's
#: reproduction reported (0.090 / 0.094 / 0.066 SOL) and ``amount_out`` this code's exact one.
STUCK = {
    "stale_no_volume_175024s": {
        "mint": "FakeStuckMintA111111111111111111111111pump", "qty": 3_258_573_209_903,
        "live": (30_000_000_006, 1_073_000_000_000_000), "offset": 30_000_000_000,
        "snaps": [(0, 755_064_214, 30_755_064_214, 766_756_894_669_239, 1_046_656_894_669_239),
                  (12_343, 1_455_281_352, 31_455_281_352, 743_457_560_908_791, 1_023_357_560_908_791)],
        "paid": "0.090", "amount_out": 90_241_649,
    },
    "stale_no_volume_185788s": {
        "mint": "FakeStuckMintB111111111111111111111111pump", "qty": 9_739_163_427_551,
        "live": (10_464_789_248, 1_078_585_819_718_599), "offset": 10_464_789_246,
        "snaps": [(0, 2, 10_464_789_248, 798_685_819_718_599, 1_078_585_819_718_599),
                  (8_127, 197_530_863, 30_197_530_863, 786_081_193_828_046, 1_065_981_193_828_046)],
        "paid": "0.094", "amount_out": 94_161_600,
    },
    "stale_no_volume_189557s": {
        "mint": "FakeStuckMintC111111111111111111111111pump", "qty": 2_391_444_079_798,
        "live": (30_000_000_004, 1_073_000_000_000_000), "offset": 30_000_000_000,
        "snaps": [(0, 2_827_888_074, 32_827_888_074, 700_668_714_285_714, 980_568_714_285_714),
                  (8_154, 5_000_000_000, 35_000_000_000, 639_814_285_714_286, 919_714_285_714_286)],
        "paid": "0.066", "amount_out": 66_174_073,
    },
}


@pytest.fixture(autouse=True)
def _fresh_clock():
    pp.clear_caches()
    yield
    pp.clear_caches()


def live_state(fx: dict) -> cp.CurveState:
    vs, vt = fx["live"]
    state, note = cp.CurveState.build(virtual_sol=vs, virtual_token=vt, real_sol=vs - fx["offset"],
                                      real_token=vt - cp.RESERVED_TOKEN_ATOMS, observed_ms=now_ms(),
                                      source="pump-curve-account")
    assert state is not None, note
    return state


def stuck(conn, monkeypatch, fx: dict, *, silent_ms: int = 50 * HOUR_MS) -> wd.Position:
    """The position, its two entry snapshots ``silent_ms`` ago, and the live curve read."""
    make_position(conn, token=fx["mint"], qty=fx["qty"], lane=Lane.LAUNCH_SNIPE)
    newest = now_ms() - silent_ms
    for back, real_sol, virtual_sol, real_token, virtual_token in fx["snaps"]:
        conn.execute(
            "INSERT INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, virtual_sol_lamports, "
            "real_token_atoms, virtual_token_atoms, source) VALUES (?,?,?,?,?,?,?,?)",
            ("sol", fx["mint"], newest - back, real_sol, virtual_sol, str(real_token), str(virtual_token),
             "pumpfun_account"))
    state = live_state(fx)
    monkeypatch.setattr(pp.PumpCurveSource, "read_state", lambda self, token: state if token == fx["mint"] else None)
    return load_position(conn, "pos_1")


def submit(conn, position) -> wd.ExitOutcome:
    quote = wd.PriceQuote(price_usd=Decimal("0.00001"), liquidity_usd=None, observed_ms=now_ms(), source="fake",
                          chain=Chain.SOL, token=position.token)
    return wd.DefaultExitSubmitter(conn, FakeSource({}))._paper(position, Decimal(100), quote, "stale_no_volume")


def no_flow_events(conn) -> list[dict]:
    rows = fetch_all(conn, "SELECT payload FROM events WHERE kind='system'")
    return [p for p in (jload(r["payload"], {}) for r in rows) if p.get("event") == wd.PAPER_EXIT_NO_FLOW_EVENT]


def sell_basis(conn) -> dict:
    order_id = fetch_all(conn, "SELECT order_id FROM orders WHERE side='sell' ORDER BY created_ms DESC")[0]["order_id"]
    return fill_basis(conn, order_id)


# ------------------------------------------------------------------------- the failure, reproduced

@pytest.mark.parametrize("name", list(STUCK))
def test_as_the_box_runs_today_the_exit_is_refused(tmp_db, risk_file, monkeypatch, name):  # noqa: F811
    """The broker's default latency on the stale snapshot flow: refused, as 104-114 times on the box."""
    fx = STUCK[name]
    position = stuck(tmp_db, monkeypatch, fx)
    held = pp.with_holding(live_state(fx), fx["qty"])
    flow = cp.measure_flow(Chain.SOL, fx["mint"], tmp_db)
    assert flow.basis == "curve_snapshots" and flow.lamports_per_s < 0  # the two-day-old entry reads
    order = PaperBroker(tmp_db).sell(position, 100, price_usd=Decimal("0.00001"), curve=held, exit_reason="x")
    assert order.error == "curve_drained_during_latency"


@pytest.mark.parametrize("name", list(STUCK))
def test_with_no_reserve_clock_the_latency_is_still_charged(tmp_db, risk_file, monkeypatch, name):  # noqa: F811
    """Unknown activity is not silence: no clock -> the old behaviour, refused, no event."""
    out = submit(tmp_db, stuck(tmp_db, monkeypatch, STUCK[name]))
    assert not out.ok and "curve_drained_during_latency" in out.detail
    assert no_flow_events(tmp_db) == []


# ------------------------------------------------------------------------- the fix

@pytest.mark.parametrize("name", list(STUCK))
def test_a_curve_silent_past_the_flow_window_fills_without_latency(tmp_db, risk_file, monkeypatch, name):  # noqa: F811
    fx = STUCK[name]
    position = stuck(tmp_db, monkeypatch, fx)
    quiet = now_ms() - 50 * HOUR_MS
    pp._note_reserves(fx["mint"], fx["live"], quiet)  # last read = last change, 50 h ago
    out = submit(tmp_db, position)
    assert out.ok, out.detail
    basis = sell_basis(tmp_db)
    assert basis["basis"] == "curve" and basis["latency_ms"] == 0 and int(basis["drift_lamports"]) == 0
    assert int(basis["amount_out"]) == fx["amount_out"]
    assert round(fx["amount_out"] / 10**9, 3) == float(fx["paid"])  # the design's reproduction
    filled = fetch_all(tmp_db, "SELECT filled_out FROM orders WHERE side='sell'")[0]["filled_out"]
    assert int(filled) == fx["amount_out"] - DEFAULT_TIP_NATIVE[Chain.SOL]
    [event] = no_flow_events(tmp_db)
    assert event["position_id"] == "pos_1" and event["filled"] is True and event["lane"] == "launch-snipe"
    assert event["reserves_moved_ms"] == quiet and event["quiet_s"] >= 50 * 3600
    assert event["flow_window_s"] == cp.DEFAULT_FLOW_WINDOW_S and event["fresh_read"] is True
    assert event["source"] == wd.SERVICE  # the drain skips its own events


# ------------------------------------------------------------------------- controls

@pytest.mark.parametrize("name", list(STUCK))
def test_a_curve_that_moved_inside_the_window_keeps_the_latency_charge(tmp_db, risk_file, monkeypatch, name):  # noqa: F811
    """Control: same position, but the reserves changed 30 s ago -> the latency is charged."""
    fx = STUCK[name]
    position = stuck(tmp_db, monkeypatch, fx)
    pp._note_reserves(fx["mint"], (1, 1), now_ms() - 50 * HOUR_MS)
    pp._note_reserves(fx["mint"], fx["live"], now_ms() - 30_000)  # a trade 30 s ago
    assert now_ms() - pp.reserve_clock(fx["mint"])[2] < cp.DEFAULT_FLOW_WINDOW_S * 1000
    out = submit(tmp_db, position)
    assert not out.ok and "curve_drained_during_latency" in out.detail
    assert no_flow_events(tmp_db) == []


def test_the_window_edge_is_the_flow_window(tmp_db, risk_file, monkeypatch):  # noqa: F811
    """One second inside the window charges; one second past it does not."""
    fx = STUCK["stale_no_volume_175024s"]
    position = stuck(tmp_db, monkeypatch, fx)
    window_ms = cp.DEFAULT_FLOW_WINDOW_S * 1000
    pp._note_reserves(fx["mint"], fx["live"], now_ms() - window_ms + 1_000)
    sub = wd.DefaultExitSubmitter(tmp_db, FakeSource({}))
    assert sub._paper_no_flow(position, live_state(fx)) is None
    pp.clear_caches()
    pp._note_reserves(fx["mint"], fx["live"], now_ms() - window_ms - 1_000)
    assert sub._paper_no_flow(position, live_state(fx)) is not None


def test_a_fresh_read_that_differs_from_the_clock_keeps_the_latency_charge(tmp_db, risk_file, monkeypatch):  # noqa: F811
    """The clock says quiet, but this exit's own read shows other reserves: it moved since."""
    fx = STUCK["stale_no_volume_175024s"]
    position = stuck(tmp_db, monkeypatch, fx)
    vs, vt = fx["live"]
    pp._note_reserves(fx["mint"], (vs + 1_000_000, vt - 10**9), now_ms() - 50 * HOUR_MS)
    out = submit(tmp_db, position)
    assert not out.ok and "curve_drained_during_latency" in out.detail
    assert no_flow_events(tmp_db) == []


def test_off_sol_the_clock_is_never_consulted(tmp_db, risk_file, monkeypatch):  # noqa: F811
    evm = "0x" + "ab" * 20
    make_position(tmp_db, token=evm, chain=Chain.BSC)
    monkeypatch.setattr(pp, "reserve_clock", lambda token: pytest.fail("reserve clock read off sol"))
    sub = wd.DefaultExitSubmitter(tmp_db, FakeSource({}))
    assert sub._paper_no_flow(load_position(tmp_db, "pos_1"), None) is None
