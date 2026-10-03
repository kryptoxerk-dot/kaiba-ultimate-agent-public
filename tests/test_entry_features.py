"""Entry features recorded point-in-time, learnable thresholds that default to off, and gates
that can tell "the lane looked and it was unknown" from "this signal predates the feature".

Why this exists (2026-10-03): the replay gate can only re-run a ``min_<x>``/``max_<x>`` over
a feature the decision's own signal payload carried, and sm-trenches recorded three numbers.
The strongest filter ever measured on our fills (holders >= 200) could not even be proposed
in a form the gates could judge.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, Lane
from kaiba.execution import lanes
from kaiba.learning import experiment_loop as loop
from kaiba.learning import gates
from tests.test_experiment_loop import (
    BASE_MS,
    DAY,
    HISTORY,
    HISTORY_SPACING,
    HOUR,
    LANE,
    NOW,
    gate_rows,
    propose,
    status,
    write_risk_file,
)
from tests.test_lanes import build_ctx, load_fixture

FEATURE_LANES = (Lane.SM_TRENCHES, Lane.CONFLUENCE_5)

#: confluence-5 on the GRADE route, pinned: config/risk.yaml now points it at the proven
#: cohort, which these fixtures do not seed (tests/test_lanes.py reads the file as shipped).
GRADE_ROUTE = {"wallet_source": "grade", "min_entities": 5, "window_s": 120,
               "max_signal_age_s": 30, "min_buy_usd": 50, "require_wallet_grade": "B"}


def confluence(tmp_db, **params):
    return lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"),
                                        params={**GRADE_ROUTE, **params}))


def trenches(tmp_db, **params):
    return lanes.sm_trenches(build_ctx(tmp_db, load_fixture("sm_trenches"), params=params or None))


# ====================================================================== the payload


def test_every_feature_is_recorded_and_a_missing_one_is_none_not_a_guess(tmp_db):
    signal = trenches(tmp_db)
    assert signal is not None
    p = signal.payload
    for name in lanes.ENTRY_FEATURES:
        assert name in p, f"{name} is not recorded"
    # the fixture's dossier carries liquidity, holders (500) and price -- nothing else
    assert p["liquidity_usd"] == 90000.0
    assert p["holder_count"] == 500
    for unknown in ("market_cap_usd", "top10_pct", "dev_pct", "insider_pct", "sniper_pct",
                    "cluster_pct", "buy_tax_bps", "sell_tax_bps", "lp_burned_pct", "volume_24h_usd"):
        assert p[unknown] is None, unknown
    # no creator, no deployer_stats, no token_bundles row: unknown, never 0
    for unknown in ("deployer_launches", "deployer_prior_scored", "deployer_runner_rate",
                    "launch_bundled_pct", "launch_sniped_pct", "curve_progress_pct"):
        assert p[unknown] is None, unknown
    assert 7190 <= p["token_age_s"] <= 7210
    assert p["migrated"] == 0 and p["since_migration_s"] is None and p["on_curve"] == 0
    assert p["launchpad"] == "pump.fun"
    # window flow over the lane's own window: four buys, $800+$640+$1200+$70, no sells
    assert p["window_swaps"] == 4 and p["window_buy_usd"] == 2710.0 and p["window_sell_usd"] == 0.0
    assert p["window_buy_share"] == 1.0 and p["window_buyers"] == 4 and p["window_sellers"] == 0
    assert p["price_change_window_pct"] > 0  # 0.002 -> 0.0022
    # the three smart wallets' own buys: $2640, first 120 s ago, last 40 s ago
    assert p["smart_buy_usd"] == 2640.0
    assert p["first_smart_buy_age_s"] == 120.0 and p["last_smart_buy_age_s"] == 40.0
    assert p["features_v"] == lanes.ENTRY_FEATURES_VERSION
    # the lane's own keys are unchanged
    assert p["smart_wallets"] == 3 and p["entity_count"] == 3 and p["rug_ratio"] == "0.12"


def test_a_stale_dossier_measure_is_recorded_as_none(tmp_db):
    """A value past its freshness budget is not evidence of anything at decision time."""
    from kaiba.core.schemas import EvidenceBasis, Measure, Receipt

    ctx = build_ctx(tmp_db, load_fixture("sm_trenches"))
    ctx.dossier.top10_pct = Measure(
        value=Decimal("12"), basis=EvidenceBasis.PROVIDER_REPORTED, freshness_budget_s=60,
        receipt=Receipt(provider="t", endpoint="t", observed_at_ms=ctx.now_ms - 3_600_000),
    )
    ctx.dossier.dev_pct = Measure(
        value=Decimal("3"), basis=EvidenceBasis.PROVIDER_REPORTED, freshness_budget_s=86_400,
        receipt=Receipt(provider="t", endpoint="t", observed_at_ms=ctx.now_ms),
    )
    p = lanes.sm_trenches(ctx).payload
    assert p["top10_pct"] is None and p["dev_pct"] == 3.0


def test_without_a_dossier_or_token_row_every_dependent_feature_is_none():
    from kaiba.execution.lanes import LaneContext

    ctx = LaneContext(chain=Chain.SOL, token="T", now_ms=10_000_000)
    feats = lanes.entry_features(ctx, window_s=300)
    for name in lanes.DOSSIER_FEATURES + ("dossier_score", "token_age_s", "migrated",
                                         "window_buy_usd", "window_buy_share", "smart_buy_usd",
                                         "price_change_window_pct", "deployer_launches"):
        assert feats[name] is None, name
    assert feats["window_swaps"] == 0 and feats["on_curve"] == 0


def test_unpriced_flow_is_unknown_dollars_not_zero():
    from kaiba.execution.lanes import LaneContext

    rows = [{"ts_ms": 9_999_000, "side": "buy", "wallet": "a", "usd_value": None, "price_usd": None}]
    ctx = LaneContext(chain=Chain.SOL, token="T", now_ms=10_000_000, recent_buys=rows)
    f = lanes._window_flow(ctx, 300)
    assert f["window_swaps"] == 1 and f["window_buyers"] == 1
    assert f["window_buy_usd"] is None and f["window_buy_share"] is None


def test_a_deployer_record_newer_than_the_decision_is_lookahead(tmp_db):
    """deployer.lookup refuses a stale record but not one computed AFTER ctx.now_ms (a
    replay). That is a future fact; it must read as unknown."""
    from kaiba.intelligence import deployer

    ctx = build_ctx(tmp_db, load_fixture("sm_trenches"))
    tmp_db.execute("UPDATE tokens SET creator='dev1' WHERE chain='sol' AND address=?", (ctx.token,))
    deployer.ensure_table(tmp_db)
    tmp_db.execute("INSERT INTO deployer_stats VALUES ('sol','dev1',12,6,2,'3.1',?)", (ctx.now_ms - 60_000,))
    f = lanes._deployer_features(ctx)
    assert f == {"deployer_launches": 12, "deployer_prior_scored": 6,
                 "deployer_prior_runners": 2, "deployer_runner_rate": round(2 / 6, 4)}
    tmp_db.execute("UPDATE deployer_stats SET computed_ms=? WHERE wallet='dev1'", (ctx.now_ms + 60_000,))
    assert set(lanes._deployer_features(ctx).values()) == {None}


# ====================================================================== the no-op defaults


def test_every_learnable_key_ships_off_and_none_shadows_a_native_key():
    for lane in FEATURE_LANES:
        keys = lanes.FEATURE_THRESHOLD_KEYS[lane]
        assert keys, lane
        for key in keys:
            assert key in lanes.DEFAULT_PARAMS[lane], key
            assert lanes.DEFAULT_PARAMS[lane][key] is None, key
    sm = lanes.DEFAULT_PARAMS[Lane.SM_TRENCHES]
    # the native keys keep their own defaults and semantics
    assert sm["min_holder_count"] == 0 and isinstance(sm["min_liquidity_usd"], dict)
    assert "min_liquidity_usd" not in lanes.FEATURE_THRESHOLD_KEYS[Lane.SM_TRENCHES]
    assert "min_holder_count" not in lanes.FEATURE_THRESHOLD_KEYS[Lane.SM_TRENCHES]
    assert "max_holder_count" in lanes.FEATURE_THRESHOLD_KEYS[Lane.SM_TRENCHES]


def _decision(signal) -> tuple | None:
    if signal is None:
        return None
    # rug_ratio_age_s is read off the wall clock (Receipt.age_seconds), so two calls a few
    # ms apart can round differently; it is not something the additions could change.
    lane_keys = {k: v for k, v in signal.payload.items()
                 if k not in lanes.ENTRY_FEATURES
                 and k not in ("launchpad", "features_v", "rug_ratio_age_s")}
    return (signal.signal_id, signal.strength, tuple(signal.wallets), tuple(signal.reasons),
            json.dumps(lane_keys, sort_keys=True, default=str))


STREAM: list[tuple[str, dict]] = [
    ("fires", {}),
    ("one smart short", {"min_smart_degen": 4}),
    ("rug over the ceiling", {"max_rug_ratio": 0.1}),
    ("holders floor on", {"min_holder_count": 200}),
    ("holders floor refuses", {"min_holder_count": 600}),
    ("window shrunk", {"window_s": 60}),
    ("liquidity floor refuses", {"min_liquidity_usd": 100000}),
]


@pytest.mark.parametrize("name,params", STREAM, ids=[s[0] for s in STREAM])
def test_defaults_leave_every_decision_identical(tmp_db, monkeypatch, name, params):
    """The SAME stream, once through the lane as shipped and once with the two additions
    stubbed out (what the lane did before 2026-10-03): identical fire/no-fire, id,
    strength, wallets, reasons and lane-owned payload."""
    shipped = _decision(trenches(tmp_db, **params))
    monkeypatch.setattr(lanes, "entry_features", lambda ctx, **kw: {})
    monkeypatch.setattr(lanes, "feature_threshold_refusal", lambda *a, **kw: None)
    before = _decision(trenches(tmp_db, **params))
    assert shipped == before


def test_defaults_leave_confluence_decisions_identical(tmp_db, monkeypatch):
    shipped = _decision(confluence(tmp_db))
    assert shipped is not None
    monkeypatch.setattr(lanes, "entry_features", lambda ctx, **kw: {})
    monkeypatch.setattr(lanes, "feature_threshold_refusal", lambda *a, **kw: None)
    assert _decision(confluence(tmp_db)) == shipped


# ====================================================================== a threshold that bites


def test_a_max_threshold_refuses_above_and_admits_at_or_below(tmp_db):
    assert trenches(tmp_db, max_holder_count=499) is None
    assert trenches(tmp_db, max_holder_count=500) is not None
    assert trenches(tmp_db, max_window_buy_usd=2709) is None
    assert trenches(tmp_db, max_window_buy_usd="2710") is not None  # risk.yaml may hold text


def test_a_min_threshold_refuses_below(tmp_db):
    assert trenches(tmp_db, min_token_age_s=7300) is None
    assert trenches(tmp_db, min_token_age_s=7000) is not None


def test_a_configured_threshold_refuses_an_unknown_feature(tmp_db):
    """The replay assumes exactly this (gates._passes(None, t) is False)."""
    assert trenches(tmp_db, max_top10_pct=99) is None          # top10 unknown in the fixture
    assert trenches(tmp_db, min_deployer_launches=0) is None   # no deployer record


def test_a_per_chain_threshold_reads_this_chain_then_default(tmp_db):
    assert trenches(tmp_db, max_holder_count={"sol": 400, "default": 900}) is None
    assert trenches(tmp_db, max_holder_count={"bsc": 400, "default": 900}) is not None
    assert trenches(tmp_db, max_holder_count={"bsc": 400}) is not None  # nothing for sol: off


def test_an_unreadable_threshold_fails_closed(tmp_db):
    assert trenches(tmp_db, max_holder_count="lots") is None


def test_confluence_reads_its_own_learnable_keys(tmp_db):
    assert confluence(tmp_db, max_entity_count=4) is None      # five entities fired it
    assert confluence(tmp_db, max_entity_count=5) is not None
    assert confluence(tmp_db, min_holder_count=600) is None    # generic min side exists here
    assert confluence(tmp_db, min_holder_count=0) is not None  # and 0 is a real floor, met


# ====================================================================== the gates resolve them


@pytest.mark.parametrize("lane", FEATURE_LANES, ids=lambda x: x.value)
def test_every_learnable_key_resolves_to_a_name_the_lane_writes(tmp_db, lane):
    signal = trenches(tmp_db) if lane is Lane.SM_TRENCHES else confluence(tmp_db)
    payload = signal.payload
    for key in lanes.FEATURE_THRESHOLD_KEYS[lane]:
        names = gates._feature_names(key)
        assert key not in names, key  # never the echoed threshold
        assert any(n in payload for n in names), f"{key} -> {names}: nothing in the payload"


def _signal(conn, sid: str, payload: dict, *, ts: int) -> None:
    conn.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, created_ms, payload_json) "
        "VALUES (?,?,?,?,?,?,?)", (sid, LANE, "robinhood", f"tok_{sid}", 0.6, ts - 1000, json.dumps(payload)),
    )


def test_feature_point_tells_unknown_from_unrecorded(tmp_db):
    _signal(tmp_db, "a", {"holder_count": 350}, ts=BASE_MS)
    _signal(tmp_db, "b", {"holder_count": None}, ts=BASE_MS)
    _signal(tmp_db, "c", {"smart_wallets": 3}, ts=BASE_MS)
    d = lambda sid: {"ts_ms": BASE_MS, "signals_json": json.dumps([sid])}  # noqa: E731
    assert gates._feature_point(tmp_db, d("a"), "max_holder_count") == (Decimal(350), True)
    assert gates._feature_point(tmp_db, d("b"), "max_holder_count") == (None, True)
    assert gates._feature_point(tmp_db, d("c"), "max_holder_count") == (None, False)
    # the old accessor is unchanged
    assert gates._feature_value(tmp_db, d("a"), "max_holder_count") == Decimal(350)
    assert gates._feature_value(tmp_db, d("b"), "max_holder_count") is None


def test_holder_floor_zero_is_no_threshold_in_the_replay(risk_file_live):
    """sm-trenches reads min_holder_count 0 as OFF; the replay must not treat 0 as a floor
    that refused every unknown holder count."""
    assert gates._incumbent_value(LANE, "min_holder_count", {"old": 0}) is None
    assert gates._incumbent_value(LANE, "min_holder_count", {"old": 150}) == Decimal(150)
    assert gates._incumbent_value("confluence-5", "min_holder_count", {"old": 0}) == Decimal(0)


@pytest.fixture
def risk_file_live(tmp_path, monkeypatch):
    return write_risk_file(tmp_path / "risk.yaml", monkeypatch)


@pytest.mark.parametrize("key", ["min_liquidity_usd", "max_rug_ratio"])
def test_thresholds_the_replay_cannot_reproduce_are_refused_with_the_reason(tmp_db, risk_file_live, key):
    propose(tmp_db, "e1", {"key": key, "new": 0.2 if key == "max_rug_ratio" else 20000})
    r = gates.replay_gate("e1", tmp_db, record=False)
    assert not r.passed and "no faithful replay" in r.reasons[0]


def test_a_per_chain_incumbent_is_refused(tmp_db, risk_file_live):
    propose(tmp_db, "e2", {"key": "max_holder_count", "new": 500, "old": {"sol": 900, "default": 800}})
    r = gates.replay_gate("e2", tmp_db, record=False)
    assert not r.passed and "configured per chain" in r.reasons[0]


def _entry(conn, tag: str, *, ts: int, payload: dict | None, pnl: int, action: str = "enter") -> None:
    sid = f"sig_{tag}"
    if payload is not None:
        _signal(conn, sid, payload, ts=ts)
    conn.execute(
        "INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, thesis, "
        "confidence, signals_json, blockers_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"dec_{tag}", ts, LANE, "live", "robinhood", f"tok_{tag}", action, "t", 0.6,
         json.dumps([sid] if payload is not None else []), "[]"),
    )
    if action == "enter":
        conn.execute(
            "INSERT INTO trades (trade_id, position_id, decision_id, lane, mode, chain, token, "
            "opened_ms, closed_ms, hold_s, cost_native, proceeds_native, pnl_native, pnl_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"trd_{tag}", f"pos_{tag}", f"dec_{tag}", LANE, "live", "robinhood", f"tok_{tag}", ts,
             ts + 60_000, 60, "1000", str(1000 + pnl), str(pnl), pnl / 10.0),
        )


def _history(conn, *, unrecorded: int = 0) -> None:
    """HISTORY hourly-ish entries: dev_pct 5 wins +200, dev_pct 30 loses -250; the first
    ``unrecorded`` predate the feature (their payload has no dev_pct key at all)."""
    for i in range(HISTORY):
        win = i % 2 == 0
        payload = {"smart_wallets": 3, "entity_count": 2}
        if i >= unrecorded:
            payload["dev_pct"] = 5.0 if win else 30.0
        _entry(conn, f"h{i}", ts=BASE_MS + i * HISTORY_SPACING, payload=payload, pnl=200 if win else -250)


def test_replay_leaves_out_decisions_that_predate_the_feature(tmp_db, risk_file_live):
    _history(tmp_db, unrecorded=HISTORY)  # nothing recorded it
    propose(tmp_db, "e3", {"key": "max_dev_pct", "new": 10})
    r = gates.replay_gate("e3", tmp_db, record=False)
    assert not r.passed and r.reasons[0].startswith(f"0/{HISTORY} decisions recorded")


def test_a_new_learnable_key_runs_replay_then_a_shadow_from_a_none_incumbent(tmp_db, risk_file_live):
    """The whole loop on a key whose incumbent is None: replay judges it as a tightening of
    "took everything", shadow setup accepts it, and the drift check reads None == None as the
    same configuration rather than as a vanished key."""
    _history(tmp_db, unrecorded=20)
    propose(tmp_db, "e4", {"key": "max_dev_pct", "new": 10})
    loop.run(tmp_db, now=NOW)
    rows = gate_rows(tmp_db, "e4")
    assert status(tmp_db, "e4") == loop.SHADOW, rows
    metrics = json.loads(tmp_db.execute(
        "SELECT metrics_json FROM gate_results WHERE experiment_id='e4' AND gate='replay'").fetchone()[0])
    assert metrics["unrecorded_feature"] == 20 and metrics["decisions_replayed"] == HISTORY - 20
    plan = loop.load_plan(tmp_db, "e4")
    assert plan["incumbent"] is None and plan["candidate"] == "10"

    # forward: a recorded UNKNOWN is a trade the candidate refuses -> the incumbent arm;
    # an unrecorded one is in neither arm
    _entry(tmp_db, "f_ok", ts=NOW + HOUR, payload={"dev_pct": 4.0}, pnl=100)
    _entry(tmp_db, "f_hi", ts=NOW + 2 * HOUR, payload={"dev_pct": 40.0}, pnl=-100)
    _entry(tmp_db, "f_unk", ts=NOW + 3 * HOUR, payload={"dev_pct": None}, pnl=-100)
    _entry(tmp_db, "f_old", ts=NOW + 4 * HOUR, payload={"smart_wallets": 3}, pnl=-100)
    out = loop.run(tmp_db, now=NOW + DAY)
    assert status(tmp_db, "e4") == loop.SHADOW  # not "incumbent changed": None is still None
    assert out["waiting"]["e4"]["candidate"] == 1 and out["waiting"]["e4"]["incumbent"] == 2
    arms = {r["trade_id"]: r["arm"] for r in tmp_db.execute(
        "SELECT trade_id, arm FROM experiment_trades WHERE experiment_id='e4'")}
    assert arms == {"trd_f_ok": "candidate", "trd_f_hi": "incumbent", "trd_f_unk": "incumbent"}
    assert loop.load_plan(tmp_db, "e4")["missing_feature"] == 1


def test_removal_share_counts_a_recorded_unknown_as_removed(tmp_db):
    """The holder-floor trap: 85.9% of scanned tokens had no holder count, and a share that
    divided them away called a kill switch a filter."""
    for i in range(6):
        _entry(tmp_db, f"k{i}", ts=BASE_MS + i * HOUR, payload={"holder_count": 300}, pnl=0, action="skip")
    for i in range(4):
        _entry(tmp_db, f"u{i}", ts=BASE_MS + i * HOUR, payload={"holder_count": None}, pnl=0, action="skip")
    for i in range(5):
        _entry(tmp_db, f"o{i}", ts=BASE_MS + i * HOUR, payload={"smart_wallets": 3}, pnl=0, action="skip")
    share = gates.removal_share(tmp_db, LANE, "max_holder_count", None, Decimal(500), "le",
                                since_ms=BASE_MS - 1)
    assert share["measured"] == 10 and share["removed"] == 4 and share["recorded_unknown"] == 4
    assert share["missing_feature"] == 5 and share["share"] == 0.4


def test_setup_shadow_still_refuses_a_non_numeric_incumbent(tmp_db, risk_file_live):
    from tests.test_experiment_loop import set_param

    set_param(risk_file_live, "max_dev_pct", "lots")
    plan, reasons = loop.setup_shadow(tmp_db, {"experiment_id": "x", "lane": LANE},
                                      {"lane": LANE, "key": "max_dev_pct", "new": 10}, now=NOW)
    assert plan is None and "not numeric" in reasons[0]
