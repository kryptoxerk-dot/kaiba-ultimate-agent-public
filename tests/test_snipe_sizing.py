"""launch-snipe entries are sized on the bonding curve, not on the dossier's pool liquidity.

MEASURED 2026-10-04, the lane's first live decision (sol, a pump.fun token 26 s old):
``size_not_positive:below_min_position:104420656<370000000``. ``viability.resolve_depth`` reads
a Solana curve only from a ``curve_snapshots`` row younger than ``DEPTH_MAX_AGE_S`` (60 s); a
fresh token had none, so the band was priced on the dossier's ~$2.7k pool liquidity and its
ceiling (0.104 SOL) fell below the chain minimum (0.37 SOL). The lane now publishes the curve
it read from the account, through tier 1's own writer, just before it records the signal.
Nothing about the band, its ceiling or the chain minimum changes.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, Lane
from kaiba.execution import snipe, viability
from kaiba.execution.risk import RiskGate
from kaiba.ingest import launch_feed as lf
from tests import test_risk
from tests.test_risk import LANE, SOL
from tests.test_viability import seed_dossier, seed_native_price

MINT = "6mCFMSPPCEt6VhdhEPKAoucqLsZS2Gddwu8b3y7kAiKo"
MIN_POSITION = 370_000_000  # the box's sol minimum, 2026-10-04
#: A pump.fun curve seconds after launch: ~30 SOL virtual, a 0.5 SOL dev buy in it.
FRESH = {"virtual_token": 1_055_350_000_000_000, "virtual_sol": 30_500_000_000, "real_token": 775_450_000_000_000,
         "real_sol": 500_000_000, "supply": 10**15, "complete": 0}


def launch(**over) -> lf.Launch:
    base = lf.Launch(chain=Chain.SOL, token=MINT, venue="pump.fun", creator="Creator1",
                     launched_ms=snipe.now_ms() - 26_000, received_ms=snipe.now_ms(),
                     meta={"bonding_curve": "BGUwFkTw6nBL33ug4BWReD9hypx8VmmB7oXy6bqjWzT4"})
    return replace(base, **over)


@pytest.fixture
def risk(tmp_path, monkeypatch):
    """``tests.test_risk.write_risk``'s envelope, written for this module."""
    path = tmp_path / "risk.yaml"
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))

    def _write(**overrides):
        path.write_text(yaml.safe_dump(test_risk._merge(test_risk.BASE_RISK, overrides)), encoding="utf-8")
        return path

    return _write


def box_like(risk, conn) -> None:
    """The sol envelope the live decision ran under, and the token's thin dossier."""
    risk(chains={"sol": {"min_position_base_units": MIN_POSITION, "max_position_base_units": 830_000_000,
                         "bankroll_base_units": 8_760_000_000}},
         bounds={"max_round_trip_cost_pct": 7.0, "max_slippage_bps": 2500})  # as shipped in config/risk.yaml
    seed_native_price(conn, "119")
    # $1,000 of dossier liquidity reproduces the live refusal's kind and minimum with this
    # tree's cost model (band ceiling 0.285 SOL < 0.37). The box's model (GMGN's 1% a leg)
    # put the same token's ceiling at 0.104 SOL; the lamports differ, the refusal does not.
    seed_dossier(conn, MINT, liquidity_usd="1000")


def test_a_fresh_pump_token_sized_on_its_dossier_is_refused_below_the_minimum(risk, tmp_db):
    """The live refusal, reproduced: no curve snapshot, so the band is the dossier's pool."""
    box_like(risk, tmp_db)
    depth = viability.resolve_depth(SOL, MINT, tmp_db, allow_network=False)
    assert depth.source.startswith("dossier:no_curve_snapshot"), depth.source
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=MINT) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=MINT).reason
    assert reason.startswith("size_not_positive:below_min_position:") and reason.endswith(f"<{MIN_POSITION}"), reason


def test_publishing_the_curve_sizes_the_same_entry_on_the_curve(risk, tmp_db):
    box_like(risk, tmp_db)
    row, note = snipe.publish_curve_snapshot(tmp_db, launch(), read=lambda conn, bc, at_ms: (FRESH, "ok"))
    assert row is not None and note == "snapshot"
    depth = viability.resolve_depth(SOL, MINT, tmp_db, allow_network=False)
    assert depth.source.startswith("curve_snapshot"), depth.source
    size = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=MINT)
    assert size >= MIN_POSITION, size
    verdict = RiskGate().check_entry(SOL, LANE, size, tmp_db, token=MINT)
    assert verdict.allowed, verdict.reason
    snap = fetch_all(tmp_db, "SELECT * FROM curve_snapshots WHERE token=?", (MINT,))
    assert len(snap) == 1 and snap[0]["source"] == "pumpfun_account"
    assert int(snap[0]["real_sol_lamports"]) == FRESH["real_sol"] and int(snap[0]["virtual_sol_lamports"]) == FRESH["virtual_sol"]


def test_a_snapshot_older_than_the_depth_budget_does_not_count(risk, tmp_db):
    """resolve_depth's 60 s budget holds: an old curve is not today's depth."""
    box_like(risk, tmp_db)
    old = snipe.now_ms() - (viability.DEPTH_MAX_AGE_S + 30) * 1000
    snipe.publish_curve_snapshot(tmp_db, launch(), at_ms=old, read=lambda conn, bc, at_ms: (FRESH, "ok"))
    assert viability.resolve_depth(SOL, MINT, tmp_db, allow_network=False).source.startswith("dossier:curve_snapshot_stale")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=MINT) == 0


def test_nothing_is_published_that_is_not_a_live_pump_curve(tmp_db):
    read = lambda conn, bc, at_ms: (FRESH, "ok")  # noqa: E731
    assert snipe.publish_curve_snapshot(tmp_db, launch(chain=Chain.ROBINHOOD), read=read) == (None, "not_solana")
    assert snipe.publish_curve_snapshot(tmp_db, launch(venue="launchlab"), read=read) == (None, "no_curve_reader_for:launchlab")
    done = lambda conn, bc, at_ms: ({**FRESH, "complete": 1}, "ok")  # noqa: E731
    assert snipe.publish_curve_snapshot(tmp_db, launch(), read=done) == (None, "curve_complete")
    unread = lambda conn, bc, at_ms: (None, "curve_account_unread")  # noqa: E731
    assert snipe.publish_curve_snapshot(tmp_db, launch(), read=unread) == (None, "curve_account_unread")
    assert fetch_all(tmp_db, "SELECT * FROM curve_snapshots") == []


def test_the_curve_goes_in_before_the_band_check_and_again_last_before_the_signal(tmp_db):
    """Before the band check, so the band is priced on the curve; last before the signal, so
    it is the freshest thing the engine reads when it sizes (60 s budget)."""
    order: list[str] = []
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('sol', ?, 'pump.fun', 1)", (MINT,))
    verdict = snipe.Verdict(True, [], 0.72, "record:low/runner", {"record": "low/runner"})
    scan = lambda token, chain: order.append("dossier") or type("D", (), {"grade": None, "blockers": []})()  # noqa: E731
    publish = lambda conn, lnch: order.append("curve") or (1, "snapshot")  # noqa: E731
    band = lambda conn, lnch, strength: order.append(("band", strength)) or (True, "band_ok")  # noqa: E731
    record = lambda sig, conn: order.append("signal") or True  # noqa: E731
    p = {**snipe.DEFAULT_PARAMS, "token_row_wait_s": 0.01}
    snipe.hand_to_engine(tmp_db, launch(), verdict, p, snipe.DossierBudget(5), scan=scan, publish=publish,
                         record_signal=record, precheck=band)
    assert order == ["curve", ("band", 0.72), "dossier", "curve", "signal"]  # the gate is asked at the signal's own strength
    order.clear()
    rh_token = "0x" + "77" * 20
    tmp_db.execute("INSERT INTO tokens (chain, address, launchpad, first_seen_ms) VALUES ('robinhood', ?, 'pons', 1)", (rh_token,))
    snipe.hand_to_engine(tmp_db, replace(launch(), chain=Chain.ROBINHOOD, token=rh_token, venue="pons"), verdict, p,
                         snipe.DossierBudget(5), scan=scan, publish=publish, record_signal=record, precheck=band)
    assert order == [("band", 0.72), "dossier", "signal"]  # robinhood: the curve is read live, never a sol snapshot


def test_the_band_check_is_the_gates_own_band_and_minimum(risk, tmp_db):
    box_like(risk, tmp_db)
    none = type("B", (), {"max_viable_base_units": None, "reason": "no_viable_size:cheapest_is_8.49pct>7.0pct"})()
    thin = type("B", (), {"max_viable_base_units": MIN_POSITION - 1, "reason": "band"})()
    wide = type("B", (), {"max_viable_base_units": 5 * MIN_POSITION, "reason": "band"})()
    check = lambda band: snipe.band_precheck(tmp_db, launch(), band_for=lambda *a, **k: band)  # noqa: E731
    assert check(none) == (False, "no_band:no_viable_size:cheapest_is_8.49pct>7.0pct")
    assert check(thin) == (False, f"band_below_min:{MIN_POSITION - 1}<{MIN_POSITION}")
    assert check(wide) == (True, "band_ok")
    # and for real: the thin dossier alone is refused, the published curve is not
    assert snipe.band_precheck(tmp_db, launch())[0] is False
    snipe.publish_curve_snapshot(tmp_db, launch(), read=lambda conn, bc, at_ms: (FRESH, "ok"))
    assert snipe.band_precheck(tmp_db, launch()) == (True, "band_ok")


@pytest.mark.parametrize("liquidity_usd", ["400", "1000", "2700"])
def test_with_the_curve_published_the_dossier_no_longer_decides_the_size(risk, tmp_db, liquidity_usd):
    """Whatever the dossier says about a pool -- even one too thin for any band -- a
    bonding-curve token is sized on its curve once the curve is visible."""
    box_like(risk, tmp_db)
    tmp_db.execute("DELETE FROM token_dossiers")
    seed_dossier(tmp_db, MINT, liquidity_usd=liquidity_usd)
    snipe.publish_curve_snapshot(tmp_db, launch(), read=lambda conn, bc, at_ms: (FRESH, "ok"))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=MINT) >= MIN_POSITION


def test_the_precheck_asks_the_gates_whole_sizer_at_the_signals_strength(risk, tmp_db):
    box_like(risk, tmp_db)
    snipe.publish_curve_snapshot(tmp_db, launch(), read=lambda conn, bc, at_ms: (FRESH, "ok"))
    asked: list[float] = []

    def sizer(answer):
        def size_for(conn, lnch, strength):
            asked.append(strength)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return size_for

    check = lambda answer: snipe.band_precheck(tmp_db, launch(), strength=0.72, size_for=sizer(answer))  # noqa: E731
    assert check((MIN_POSITION, "sized")) == (True, "size_ok") and asked == [0.72]
    cut = "below_min_position:8552204939843329<16700000000000000"
    assert check((0, cut)) == (False, f"size_zero:{cut}")
    # the concentration cuts read what the dossier writes, and the dossier comes after: the engine decides
    assert check((0, "concentration:launch_wave:0.5x:1<2")) == (True, "size_deferred:concentration:launch_wave:0.5x:1<2")
    assert check(RuntimeError("boom")) == (False, "size_unavailable:RuntimeError")
    assert snipe.band_precheck(tmp_db, launch()) == (True, "band_ok")  # no strength: the band alone, as before


@pytest.mark.skipif(snipe.LANE_VALUE not in {x.value for x in Lane}, reason="Lane 'launch-snipe' not wired")
def test_a_drawdown_cut_below_the_minimum_is_refused_before_any_dossier(risk, tmp_db):
    """MEASURED 2026-10-04 on the box: RH 0x2741d56c72 passed the band check (its band was
    ~0.044 ETH), then the engine refused it below_min_position at 0.00855 ETH -- the prior
    day's drawdown (x0.45 on a 0.36 ETH bankroll) cut the SIZE, not the pool. The same cut,
    through the real gate, on the box's sol envelope."""
    from datetime import UTC, datetime, timedelta

    risk(chains={"sol": {"min_position_base_units": MIN_POSITION, "max_position_base_units": 830_000_000,
                         "bankroll_base_units": 8_760_000_000}},
         bounds={"max_round_trip_cost_pct": 7.0, "max_slippage_bps": 2500, "max_size_pct_bankroll": 30.0},
         lanes={snipe.LANE_VALUE: {"mode": "live", "size_pct_min": 6.0, "size_pct_max": 7.0, "chains": ["sol"], "params": {}}})
    seed_native_price(tmp_db, "119")
    snipe.publish_curve_snapshot(tmp_db, launch(), read=lambda conn, bc, at_ms: (FRESH, "ok"))
    assert snipe.band_precheck(tmp_db, launch(), strength=0.72) == (True, "size_ok")  # no drawdown: sized
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    tmp_db.execute("INSERT INTO risk_state (day_key, realized_native_json, updated_ms) VALUES (?, ?, 1)",
                   (yesterday, '{"sol": -2650000000}'))  # 30.3% of the bankroll: recovery x0.45
    ok, why = snipe.band_precheck(tmp_db, launch(), strength=0.72)
    assert not ok and why.startswith("size_zero:below_min_position:") and why.endswith(f"<{MIN_POSITION}"), why
