"""Lane evaluators: each lane fires on its good case and stays silent on the near miss.

The near-miss half is the half that matters. A lane that fires on the good case and also
on the case that is one entity short, one second late or one point over the bot threshold
has no threshold at all, and the shadow record it produces is noise.

The single most important test in this file is
``test_confluence_five_addresses_one_entity_does_not_fire``: five addresses controlled by
one operator must not read as five opinions.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core import schemas as schemas_mod
from kaiba.core.db import jdump
from kaiba.core.events import emit
from kaiba.core.schemas import (
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    Measure,
    Receipt,
    Token,
    TokenDossier,
    TokenRisk,
    now_ms,
)
from kaiba.execution import lanes
from kaiba.execution.lanes import LANES, LaneContext, evaluate_all
from kaiba.intelligence.dyor import RUG_RATIO_BUDGET_S

FIXTURES = Path(__file__).parent / "fixtures" / "lanes"

#: The test clock, pinned to the start of the current 120s bucket: fixed for the whole
#: run (so signal ids are reproducible and a +1s re-evaluation stays in one bucket) but
#: close enough to wall clock that ``Measure`` freshness budgets behave as they would in
#: production.
NOW = (now_ms() // 120_000) * 120_000


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _measure(value: Any, *, at_ms: int = NOW) -> Measure:
    if value is None:
        return Measure.unknown()
    return Measure(
        value=Decimal(str(value)),
        basis=EvidenceBasis.PROVIDER_REPORTED,
        receipt=Receipt(provider="fixture", endpoint="lanes", observed_at_ms=at_ms),
        freshness_budget_s=86_400,
    )


def build_ctx(
    conn,
    fx: dict[str, Any],
    *,
    now_ms: int = NOW,
    params: dict[str, Any] | None = None,
    caller: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> LaneContext:
    """Write the fixture into the database and hand back the context the lanes see."""
    chain = Chain(fx["chain"])
    token = fx["token"]

    for w in fx.get("wallets", []):
        conn.execute(
            "INSERT OR REPLACE INTO wallets (chain, address, name, source, tags_json, first_seen_ms, "
            "last_seen_ms, cohort, meta_json) VALUES (?,?,?,?,?,?,?,?,'{}')",
            (chain.value, w["address"], w.get("name"), "fixture", jdump(w.get("tags", [])),
             now_ms - 86_400_000, now_ms, w.get("cohort")),
        )
    for s in fx.get("scores", []):
        conn.execute(
            "INSERT OR REPLACE INTO wallet_scores (chain, address, score, grade, evidence_weight, "
            "archetype, factors_json, penalties_json, blockers_json, receipts_json, model_version, "
            "scored_at_ms) VALUES (?,?,?,?,?,?,'[]','[]','[]','[]','kaiba-wallet-v1',?)",
            (chain.value, s["address"], s["score"], s["grade"], 100.0, s.get("archetype", "trader"),
             now_ms - 3_600_000),
        )

    meta = fx.get("token_meta") or {}
    created_ms = now_ms - int(meta["created_age_s"]) * 1000 if meta.get("created_age_s") else None
    migrated_ms = now_ms - int(meta["migrated_age_s"]) * 1000 if meta.get("migrated_age_s") else None
    token_meta = Token(
        address=token,
        chain=chain,
        symbol=meta.get("symbol"),
        decimals=meta.get("decimals"),
        created_ms=created_ms,
        launchpad=meta.get("launchpad"),
        pool=meta.get("pool"),
        migrated_ms=migrated_ms,
    )
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, symbol, name, decimals, creator, created_ms, "
        "launchpad, pool, migrated_ms, first_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,'{}')",
        (chain.value, token, meta.get("symbol"), meta.get("name"), meta.get("decimals"), None,
         created_ms, meta.get("launchpad"), meta.get("pool"), migrated_ms, created_ms or now_ms),
    )

    dfx = fx.get("dossier") or {}
    dossier = TokenDossier(
        address=token,
        chain=chain,
        price_usd=_measure(dfx.get("price_usd")),
        liquidity_usd=_measure(dfx.get("liquidity_usd")),
        # 500 unless the fixture says otherwise: sm-trenches ships a holder floor
        # of 200 (config/risk.yaml, see tests/test_holder_floor.py) and an UNKNOWN
        # count refuses, so a fixture with no holder_count would silently stop
        # testing whatever it was actually about. Set it low to exercise the gate.
        holder_count=_measure(dfx.get("holder_count", 500)),
        bundler_pct=_measure(dfx.get("bundler_pct")),
        rug_ratio=_measure(dfx.get("rug_ratio")),
        graded_wallets=dfx.get("graded_wallets", []),
        blockers=[TokenRisk(b) for b in dfx.get("blockers", [])],
        grade=Grade(dfx.get("grade", "UNSCORED")),
        built_at_ms=now_ms - int(dfx.get("built_age_s", 5)) * 1000,
    )

    for c in fx.get("callers", []):
        conn.execute(
            "INSERT OR REPLACE INTO callers (platform, caller_id, display_name, channel, calls, wins, "
            "losses, expectancy, avg_peak_x, last_call_ms, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (c["platform"], c["caller_id"], c.get("display_name"), None, c.get("calls", 0),
             c.get("wins", 0), c.get("losses", 0), c.get("expectancy"), None,
             now_ms - int(c.get("call_age_s", 0)) * 1000, c.get("mode", "observe")),
        )
        conn.execute(
            "INSERT OR REPLACE INTO caller_calls (platform, caller_id, chain, token, ts_ms, channel, "
            "price_at_call_usd, peak_x, outcome) VALUES (?,?,?,?,?,?,?,?,'pending')",
            (c["platform"], c["caller_id"], chain.value, token,
             now_ms - int(c.get("call_age_s", 0)) * 1000, None, None, None),
        )

    if fx.get("listing"):
        listing = dict(fx["listing"])
        listing["announced_ms"] = now_ms - int(listing.pop("announced_age_s", 0)) * 1000
        emit(EventKind.ALPHA_LISTING, listing, chain=chain, subject=token, conn=conn)

    buys = [
        {**b, "ts_ms": now_ms - int(b.get("age_s", 0)) * 1000, "chain": chain.value, "token": token}
        for b in (fx.get("buys") or [])
    ]
    return LaneContext(
        chain=chain,
        token=token,
        now_ms=now_ms,
        conn=conn,
        dossier=dossier,
        recent_buys=buys,
        token_meta=token_meta,
        curve=fx.get("curve"),
        caller=caller,
        # blocked_launchpads defaults to EMPTY here, and a test that wants the
        # blocklist passes its own. Every lane fixture except pons_robinhood is a
        # pump.fun token, and sm-trenches ships a blocklist containing pump.fun
        # (config/risk.yaml -- it MEASURED -28.1% over 39 live fills). Without this
        # every one of those fixtures would refuse at the blocklist and stop testing
        # the rug-ratio and strength behaviour it was written for. The blocklist has
        # its own tests in tests/test_holder_floor.py.
        #
        # confluence-5 is pinned to the lane's shipped DEFAULT_PARAMS (the grade route,
        # 5 entities / 120 s / 30 s age) for the same reason: these fixtures test that
        # mechanism, and the operator re-pointed the file's copy at the PROVEN cohort on
        # 2026-10-03 (wallet_source: proven, 2 entities, 1800 s), which no fixture
        # freezes -- every confluence test then read None. The proven route and the
        # shipped file's own values are covered by test_confluence_proven*.py. Flat keys
        # the caller passes still win (they are applied after this lane-keyed block).
        params={"blocked_launchpads": [],
                Lane.CONFLUENCE_5.value: dict(lanes.DEFAULT_PARAMS[Lane.CONFLUENCE_5]),
                **(params or {})},
        extras=extras or {},
    )


def caller_from(fx: dict[str, Any], caller_id: str, *, now_ms: int = NOW) -> dict[str, Any]:
    entry = next(c for c in fx["callers"] if c["caller_id"] == caller_id)
    return {**entry, "call_ms": now_ms - int(entry.get("call_age_s", 0)) * 1000}


# ---------------------------------------------------------------- registry


def test_registry_covers_every_entry_lane():
    assert set(LANES) == {
        Lane.CONFLUENCE_5,
        Lane.TRUSTED_COPY,
        Lane.CURVE_VELOCITY,
        Lane.SM_TRENCHES,
        Lane.MIGRATION_FADE,
        Lane.KOL_FADE,
        Lane.LISTING_POP,
        Lane.PONS_ROBINHOOD,
    }
    assert Lane.MANUAL not in LANES
    assert all(callable(fn) for fn in LANES.values())


# ---------------------------------------------------------------- confluence-5


def test_confluence_fires_on_five_independent_entities(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("confluence_5"))
    signal = lanes.confluence_5(ctx)
    assert signal is not None
    assert signal.lane is Lane.CONFLUENCE_5
    assert signal.payload["entity_count"] == 5
    # the C-grade wallet and the $12 dust buy are not opinions
    assert len(signal.wallets) == 5
    assert "wa11etFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF6" not in signal.wallets
    assert "wa11etGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG7" not in signal.wallets
    assert 0.0 < signal.strength <= 1.0


def test_confluence_one_entity_short_is_silent(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("confluence_5"), params={"min_entities": 6})
    assert lanes.confluence_5(ctx) is None


def test_confluence_five_addresses_one_entity_does_not_fire(tmp_db, monkeypatch):
    """Five sybil addresses are one opinion. This is the lane's whole reason to exist."""
    monkeypatch.setattr(lanes, "independent_entity_count", lambda chain, addrs, conn=None: 1)
    ctx = build_ctx(tmp_db, load_fixture("confluence_5"))
    assert lanes.confluence_5(ctx) is None


def test_confluence_rejects_a_stale_newest_buy(tmp_db):
    fx = load_fixture("confluence_5")  # newest qualifying buy is 6s old
    assert lanes.confluence_5(build_ctx(tmp_db, fx, params={"max_signal_age_s": 6})) is not None
    assert lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"),
                                        params={"max_signal_age_s": 5})) is None


def test_confluence_ignores_buys_below_the_usd_floor(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("confluence_5"), params={"min_buy_usd": 200})
    assert lanes.confluence_5(ctx) is None  # only 3 wallets clear $200


def test_confluence_ignores_ungraded_wallets(tmp_db):
    fx = load_fixture("confluence_5")
    fx["scores"] = [s for s in fx["scores"] if s["grade"] != "B"]
    assert lanes.confluence_5(build_ctx(tmp_db, fx)) is None


def test_confluence_requires_a_net_buy_not_a_round_trip(tmp_db):
    fx = load_fixture("confluence_5")
    for wallet in ("wa11etAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                   "wa11etBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"):
        fx["buys"].append(
            {"wallet": wallet, "side": "sell", "age_s": 2, "usd_value": "5000",
             "amount_native": 0, "price_usd": "0.00035", "tx": f"tx-exit-{wallet[:8]}"}
        )
    assert lanes.confluence_5(build_ctx(tmp_db, fx)) is None


def test_confluence_strength_rises_with_entity_count(tmp_db, monkeypatch):
    base = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5")))
    monkeypatch.setattr(lanes, "independent_entity_count", lambda chain, addrs, conn=None: 9)
    richer = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5")))
    assert richer.strength > base.strength


def test_confluence_unpriced_buys_do_not_count(tmp_db):
    """An unpriced fill is not evidence of conviction, so it cannot clear min_buy_usd."""
    fx = load_fixture("confluence_5")
    for buy in fx["buys"]:
        buy["usd_value"] = None
    assert lanes.confluence_5(build_ctx(tmp_db, fx)) is None


# ---------------------------------------------------------------- deterministic ids


def test_signal_id_is_stable_inside_a_window_bucket(tmp_db):
    first = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"), now_ms=NOW))
    again = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"), now_ms=NOW + 1_000))
    assert first.signal_id == again.signal_id


def test_signal_id_changes_across_buckets_and_tokens(tmp_db):
    first = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"), now_ms=NOW))
    later = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5"), now_ms=NOW + 300_000))
    other = load_fixture("confluence_5")
    other["token"] = "OtherTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    elsewhere = lanes.confluence_5(build_ctx(tmp_db, other, now_ms=NOW))
    assert first.signal_id != later.signal_id
    assert first.signal_id != elsewhere.signal_id


def test_record_is_idempotent_for_one_bucket(tmp_db):
    signal = lanes.confluence_5(build_ctx(tmp_db, load_fixture("confluence_5")))
    assert lanes.record(signal, tmp_db) is True
    assert lanes.record(signal, tmp_db) is False
    rows = tmp_db.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert rows == 1


# ---------------------------------------------------------------- trusted-copy


def test_trusted_copy_fires_on_a_curated_source(tmp_db):
    signal = lanes.trusted_copy(build_ctx(tmp_db, load_fixture("trusted_copy")))
    assert signal is not None
    assert signal.payload["source_wallet"] == "srcWa11etTrustedAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    assert signal.payload["drift_pct"] == pytest.approx(6.0)


def test_trusted_copy_is_silent_one_second_past_the_delay_cap(tmp_db):
    fx = load_fixture("trusted_copy")  # source bought 8s ago
    assert lanes.trusted_copy(build_ctx(tmp_db, fx, params={"max_copy_delay_s": 8})) is not None
    assert lanes.trusted_copy(
        build_ctx(tmp_db, load_fixture("trusted_copy"), params={"max_copy_delay_s": 7})
    ) is None


def test_trusted_copy_refuses_when_price_has_run_away(tmp_db):
    fx = load_fixture("trusted_copy")
    fx["dossier"]["price_usd"] = "0.0125"  # +25% against the source fill
    assert lanes.trusted_copy(build_ctx(tmp_db, fx)) is None


def test_trusted_copy_ignores_wallets_outside_the_cohort(tmp_db):
    fx = load_fixture("trusted_copy")
    fx["wallets"][0]["cohort"] = "tracked"
    assert lanes.trusted_copy(build_ctx(tmp_db, fx)) is None


def test_trusted_copy_refuses_when_the_current_price_is_unknown(tmp_db):
    fx = load_fixture("trusted_copy")
    fx["dossier"]["price_usd"] = None
    assert lanes.trusted_copy(build_ctx(tmp_db, fx)) is None


# ---------------------------------------------------------------- curve-velocity


def test_curve_velocity_fires_in_the_mid_band(tmp_db):
    signal = lanes.curve_velocity(build_ctx(tmp_db, load_fixture("curve_velocity")))
    assert signal is not None
    assert signal.payload["sol_per_min"] == "7.5"
    assert signal.payload["graded_wallets"] == 2


def test_curve_velocity_rejects_a_bundler_share_one_point_too_high(tmp_db):
    fx = load_fixture("curve_velocity")
    fx["dossier"]["bundler_pct"] = "21"  # cap is 20
    assert lanes.curve_velocity(build_ctx(tmp_db, fx)) is None


def test_curve_velocity_rejects_an_unknown_bundler_share(tmp_db):
    fx = load_fixture("curve_velocity")
    fx["dossier"]["bundler_pct"] = None
    assert lanes.curve_velocity(build_ctx(tmp_db, fx)) is None


def test_curve_velocity_rejects_slow_curves_and_the_wrong_progress_band(tmp_db):
    fx = load_fixture("curve_velocity")
    assert lanes.curve_velocity(build_ctx(tmp_db, fx, params={"min_sol_per_min": 7.6})) is None
    assert lanes.curve_velocity(
        build_ctx(tmp_db, load_fixture("curve_velocity"), params={"max_progress_pct": 45})
    ) is None
    assert lanes.curve_velocity(
        build_ctx(tmp_db, load_fixture("curve_velocity"), params={"min_progress_pct": 47})
    ) is None


def test_curve_velocity_needs_at_least_one_graded_wallet(tmp_db):
    fx = load_fixture("curve_velocity")
    fx["scores"] = []
    fx["dossier"]["graded_wallets"] = []
    assert lanes.curve_velocity(build_ctx(tmp_db, fx)) is None


# ---------------------------------------------------------------- sm-trenches


def test_sm_trenches_fires_on_three_smart_wallets(tmp_db):
    signal = lanes.sm_trenches(build_ctx(tmp_db, load_fixture("sm_trenches")))
    assert signal is not None
    assert signal.payload["smart_wallets"] == 3
    assert signal.payload["entity_count"] >= 2


def test_sm_trenches_is_silent_one_smart_wallet_short(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("sm_trenches"), params={"min_smart_degen": 4})
    assert lanes.sm_trenches(ctx) is None


def test_sm_trenches_collapses_addresses_into_one_entity(tmp_db, monkeypatch):
    monkeypatch.setattr(lanes, "independent_entity_count", lambda chain, addrs, conn=None: 1)
    assert lanes.sm_trenches(build_ctx(tmp_db, load_fixture("sm_trenches"))) is None


def test_sm_trenches_refuses_a_measured_rug_ratio_at_or_over_the_ceiling(tmp_db):
    for measured in ("0.31", "0.3", "0.9"):
        fx = load_fixture("sm_trenches")
        fx["dossier"]["rug_ratio"] = measured
        assert lanes.sm_trenches(build_ctx(tmp_db, fx)) is None, measured
    fx = load_fixture("sm_trenches")
    fx["dossier"]["rug_ratio"] = "0.29"
    signal = lanes.sm_trenches(build_ctx(tmp_db, fx))
    assert signal is not None
    assert signal.payload["rug_ratio"] == "0.29"
    assert signal.payload["rug_ratio_basis"] == "measured"
    assert signal.payload["max_rug_ratio"] == "0.3"
    assert "MEASURED" in signal.reasons[2] and "earns no strength" in signal.reasons[2]


def test_sm_trenches_fires_on_an_unavailable_rug_ratio_without_earning_strength(tmp_db):
    """MEASURED 2026-09-21 on the live box: GMGN fills ``rug_ratio`` on 8/180 bsc feed rows
    (all 0) and on 0/4,532 dossiers, so "block on unknown" was a permanent off switch on the
    lane's own chain, not a safety check. Unknown is not evidence either way: no refusal, no
    strength, and the payload says ``None`` rather than a zero a downstream reader would
    take for a clean score. The rug defence is the dossier blockers ``engine.decide``
    refuses before sizing; ``tests/test_bsc_lane_inputs.py`` section 8 proves that path.
    """
    fx = load_fixture("sm_trenches")
    fx["dossier"]["rug_ratio"] = None
    unknown = lanes.sm_trenches(build_ctx(tmp_db, fx))
    assert unknown is not None
    assert unknown.payload["rug_ratio"] is None  # None, never "0" and never "None"
    assert unknown.payload["rug_ratio_basis"] == "unavailable"
    rug_reason = unknown.reasons[2]
    assert "UNAVAILABLE" in rug_reason and "not a refusal and not strength" in rug_reason
    for defence in (
        "honeypot",
        "mint/freeze authority",
        "dev_concentration",
        "cluster_concentration",
        "already_rugged",
        "QUARANTINED",
        "engine.decide",
    ):
        assert defence in rug_reason, defence

    # Same tape, a very clean measured ratio and a barely-passing one: identical strength,
    # so the rug ratio is not a strength term for anyone, measured or not.
    strengths = set()
    for measured in ("0.01", "0.29"):
        fx = load_fixture("sm_trenches")
        fx["dossier"]["rug_ratio"] = measured
        strengths.add(lanes.sm_trenches(build_ctx(tmp_db, fx)).strength)
    assert strengths == {unknown.strength}
    assert 0.0 <= unknown.strength <= 1.0


def test_sm_trenches_strength_is_renormalised_after_dropping_the_rug_term(tmp_db):
    """The weights 0.35 / 0.15 / 0.25 (INVENTED, unchanged) sum to 0.75 once the 0.25 rug
    term is gone; dividing by 0.75 keeps the sizer's [0, 1] range. On the fixture: 3 smart
    wallets against min 3 -> 3/6 of the 0.15; 3 entities (no entity rows, so addresses)
    against min 2 -> 3/4 of the 0.25; (0.35 + 0.075 + 0.1875) / 0.75 = 0.8167."""
    signal = lanes.sm_trenches(build_ctx(tmp_db, load_fixture("sm_trenches")))
    assert signal is not None and signal.payload["entity_count"] == 3
    assert signal.strength == pytest.approx((0.35 + 0.075 + 0.1875) / 0.75, abs=1e-4)
    saturated = lanes.sm_trenches(
        build_ctx(
            tmp_db,
            load_fixture("sm_trenches"),
            params={"min_smart_degen": 1, "min_independent_entities": 1},
        )
    )
    assert saturated is not None and saturated.strength == 1.0


def _rug_measure(
    value: str,
    *,
    age_s: int,
    budget_s: int = RUG_RATIO_BUDGET_S,
    basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED,
) -> Measure:
    """A rug Measure as dyor builds it: the feed row's value, a receipt ``age_s`` old and
    the lane's 900 s budget, not the fixture helper's day-long one."""
    return Measure(
        value=Decimal(value),
        basis=basis,
        receipt=Receipt(
            provider="gmgn", endpoint="feed.trenches", observed_at_ms=NOW - age_s * 1000, basis=basis
        ),
        freshness_budget_s=budget_s,
    )


def _trenches_ctx_with_rug(conn, measure: Measure | None) -> LaneContext:
    ctx = build_ctx(conn, load_fixture("sm_trenches"))
    rug = measure if measure is not None else Measure.unknown(RUG_RATIO_BUDGET_S)
    return ctx.model_copy(update={"dossier": ctx.dossier.model_copy(update={"rug_ratio": rug})})


def test_sm_trenches_refuses_a_stale_rug_ratio_at_or_over_the_ceiling(tmp_db, monkeypatch):
    """Round-1 verifier finding, pinned. ``Measure.stale`` is true one second past
    ``RUG_RATIO_BUDGET_S`` and ``lanes._measure`` folds that into the same ``None`` as
    UNAVAILABLE, so a 0.9 with a 901 s receipt fired where a 0.9 at 899 s refused, and
    engine.decide sized it because the dossier itself was inside its 300 s budget. A stale
    bad number is still a bad number: both refuse now, and so does the ceiling exactly."""
    monkeypatch.setattr(schemas_mod, "now_ms", lambda: NOW)  # Measure.stale reads the wall clock
    assert RUG_RATIO_BUDGET_S == 900
    fresh_bad = _rug_measure("0.9", age_s=RUG_RATIO_BUDGET_S - 1)
    stale_bad = _rug_measure("0.9", age_s=RUG_RATIO_BUDGET_S + 1)
    assert fresh_bad.stale is False and stale_bad.stale is True
    # The helper the old gate read: None for the stale case, which is how it slipped through.
    assert lanes._measure(_trenches_ctx_with_rug(tmp_db, stale_bad).dossier, "rug_ratio") is None
    assert lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, fresh_bad)) is None
    assert lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, stale_bad)) is None
    assert lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, _rug_measure("0.3", age_s=901))) is None
    # A provider answering out of an expired cache stamps basis STALE on a fresh receipt:
    # still a number at or over the ceiling, still refused.
    cached_bad = _rug_measure("0.9", age_s=10, basis=EvidenceBasis.STALE)
    assert cached_bad.stale is False
    assert lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, cached_bad)) is None
    # The same tape with the rug ratio unavailable fires, so the refusals above are the rug gate.
    assert lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, None)) is not None


def test_sm_trenches_reads_a_stale_clean_rug_ratio_as_unavailable_and_says_so(tmp_db, monkeypatch):
    """Under the ceiling: 901 s fires with basis "stale" and no number in the payload, 899 s
    fires with basis "measured" and the number, unavailable fires with basis "unavailable".
    None of the three earns strength."""
    monkeypatch.setattr(schemas_mod, "now_ms", lambda: NOW)
    stale = lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, _rug_measure("0.1", age_s=901)))
    assert stale is not None
    assert stale.payload["rug_ratio"] is None and stale.payload["rug_ratio_basis"] == "stale"
    assert stale.payload["rug_ratio_age_s"] == 901.0
    assert stale.payload["rug_ratio_budget_s"] == RUG_RATIO_BUDGET_S
    assert "STALE" in stale.reasons[2] and "treated as unavailable" in stale.reasons[2]
    assert "0.1" in stale.reasons[2] and "901" in stale.reasons[2]

    fresh = lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, _rug_measure("0.1", age_s=899)))
    assert fresh is not None
    assert fresh.payload["rug_ratio"] == "0.1" and fresh.payload["rug_ratio_basis"] == "measured"
    assert fresh.payload["rug_ratio_age_s"] == 899.0
    assert "MEASURED" in fresh.reasons[2]

    unknown = lanes.sm_trenches(_trenches_ctx_with_rug(tmp_db, None))
    assert unknown is not None
    assert unknown.payload["rug_ratio"] is None and unknown.payload["rug_ratio_basis"] == "unavailable"
    assert unknown.payload["rug_ratio_age_s"] is None
    assert "UNAVAILABLE" in unknown.reasons[2]

    # basis STALE on a fresh receipt, under the ceiling: stale too, never "measured".
    cached = lanes.sm_trenches(
        _trenches_ctx_with_rug(tmp_db, _rug_measure("0.1", age_s=10, basis=EvidenceBasis.STALE))
    )
    assert cached is not None
    assert cached.payload["rug_ratio"] is None and cached.payload["rug_ratio_basis"] == "stale"

    # No rug strength term for anyone: stale, fresh, cached or unknown.
    assert stale.strength == fresh.strength == unknown.strength == cached.strength


# ---------------------------------------------------------------- migration-fade


def test_migration_fade_is_sell_biased_and_never_holds_through(tmp_db):
    signal = lanes.migration_fade(build_ctx(tmp_db, load_fixture("migration_fade")))
    assert signal is not None
    assert signal.payload["never_hold_through_migration"] is True
    assert signal.payload["bias"] == "sell"
    assert signal.payload["sell_within_s"] == 180
    assert signal.payload["exit_deadline_ms"] == NOW + 180_000


def test_migration_fade_is_silent_once_the_window_has_passed(tmp_db):
    fx = load_fixture("migration_fade")
    fx["token_meta"]["migrated_age_s"] = 181  # cap is 180
    assert lanes.migration_fade(build_ctx(tmp_db, fx)) is None


def test_migration_fade_needs_a_migration(tmp_db):
    fx = load_fixture("migration_fade")
    fx["token_meta"].pop("migrated_age_s")
    assert lanes.migration_fade(build_ctx(tmp_db, fx)) is None


def test_migration_fade_reads_the_event_bus(tmp_db):
    fx = load_fixture("migration_fade")
    fx["token_meta"].pop("migrated_age_s")
    ctx = build_ctx(tmp_db, fx)
    emit(EventKind.TOKEN_MIGRATED, {"migrated_ms": NOW - 20_000}, chain=Chain.SOL,
         subject=fx["token"], conn=tmp_db)
    assert lanes.migration_fade(ctx) is not None


# ---------------------------------------------------------------- kol-fade


def test_kol_follow_fires_for_a_measured_positive_caller(tmp_db):
    fx = load_fixture("kol_callers")
    ctx = build_ctx(tmp_db, fx, caller=caller_from(fx, "alpha_caller"))
    signal = lanes.kol_fade(ctx)
    assert signal is not None
    assert signal.payload["bias"] == "buy"
    assert signal.payload["exit_trigger"] is False


def test_kol_fade_emits_an_exit_trigger(tmp_db):
    fx = load_fixture("kol_callers")
    signal = lanes.kol_fade(build_ctx(tmp_db, fx, caller=caller_from(fx, "exit_liquidity")))
    assert signal is not None
    assert signal.payload["bias"] == "sell"
    assert signal.payload["exit_trigger"] is True


def test_kol_observe_is_the_default_posture_and_produces_nothing(tmp_db):
    fx = load_fixture("kol_callers")
    assert lanes.kol_fade(build_ctx(tmp_db, fx, caller=caller_from(fx, "unknown_caller"))) is None


def test_kol_follow_needs_enough_measured_calls(tmp_db):
    fx = load_fixture("kol_callers")
    # positive expectancy but only 4 calls against a floor of 10
    assert lanes.kol_fade(build_ctx(tmp_db, fx, caller=caller_from(fx, "thin_record"))) is None


def test_kol_follow_needs_positive_expectancy(tmp_db):
    fx = load_fixture("kol_callers")
    caller = caller_from(fx, "alpha_caller")
    caller["expectancy"] = 0.0
    assert lanes.kol_fade(build_ctx(tmp_db, fx, caller=caller)) is None


def test_kol_ignores_a_stale_call(tmp_db):
    fx = load_fixture("kol_callers")
    caller = caller_from(fx, "alpha_caller")
    caller["call_ms"] = NOW - 301_000  # cap is 300s
    assert lanes.kol_fade(build_ctx(tmp_db, fx, caller=caller)) is None


def test_kol_reads_the_caller_table_when_the_context_is_empty(tmp_db):
    fx = load_fixture("kol_callers")
    fx["callers"] = [c for c in fx["callers"] if c["caller_id"] == "alpha_caller"]
    signal = lanes.kol_fade(build_ctx(tmp_db, fx))
    assert signal is not None
    assert signal.payload["caller_id"] == "alpha_caller"


# ---------------------------------------------------------------- listing-pop


def test_listing_pop_fires_inside_the_latency_window(tmp_db):
    signal = lanes.listing_pop(build_ctx(tmp_db, load_fixture("listing_pop")))
    assert signal is not None
    assert signal.payload["venue"] == "upbit"
    assert signal.payload["latency_s"] == pytest.approx(9.0)


def test_listing_pop_is_silent_one_second_late(tmp_db):
    fx = load_fixture("listing_pop")
    fx["listing"]["announced_age_s"] = 31  # cap is 30
    assert lanes.listing_pop(build_ctx(tmp_db, fx)) is None


def test_listing_pop_needs_an_announcement(tmp_db):
    fx = load_fixture("listing_pop")
    fx.pop("listing")
    assert lanes.listing_pop(build_ctx(tmp_db, fx)) is None


# ---------------------------------------------------------------- pons-robinhood


def test_pons_fires_on_a_fresh_robinhood_launch(tmp_db):
    signal = lanes.pons_robinhood(build_ctx(tmp_db, load_fixture("pons_robinhood")))
    assert signal is not None
    assert signal.chain is Chain.ROBINHOOD
    assert signal.payload["entity_count"] == 4


def test_pons_is_silent_one_entity_short(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("pons_robinhood"), params={"min_entities": 5})
    assert lanes.pons_robinhood(ctx) is None


def test_pons_rejects_a_stale_launch_and_thin_liquidity(tmp_db):
    fx = load_fixture("pons_robinhood")  # 660s old, $12k depth
    assert lanes.pons_robinhood(build_ctx(tmp_db, fx, params={"max_age_s": 600})) is None
    assert lanes.pons_robinhood(
        build_ctx(tmp_db, load_fixture("pons_robinhood"), params={"min_liquidity_usd": 20000})
    ) is None


def test_pons_ignores_other_launchpads_and_other_chains(tmp_db):
    fx = load_fixture("pons_robinhood")
    fx["token_meta"]["launchpad"] = "clanker"
    assert lanes.pons_robinhood(build_ctx(tmp_db, fx)) is None
    sol = load_fixture("confluence_5")
    assert lanes.pons_robinhood(build_ctx(tmp_db, sol)) is None


# ---------------------------------------------------------------- evaluate_all


def test_evaluate_all_returns_only_what_actually_fired(tmp_db):
    # The confluence tape carries four wallets scored smart_money / top_trader and no rug
    # ratio, so since 2026-09-21 sm-trenches fires on it too: an unavailable rug ratio no
    # longer vetoes. A MEASURED ratio at the ceiling still does, and then only confluence-5
    # is left -- which is what "only what actually fired" is checking.
    signals = evaluate_all(build_ctx(tmp_db, load_fixture("confluence_5")))
    assert {s.lane for s in signals} == {Lane.CONFLUENCE_5, Lane.SM_TRENCHES}
    fx = load_fixture("confluence_5")
    fx["dossier"]["rug_ratio"] = "0.3"
    assert [s.lane for s in evaluate_all(build_ctx(tmp_db, fx))] == [Lane.CONFLUENCE_5]


def test_evaluate_all_is_sorted_by_strength_and_survives_a_broken_lane(tmp_db, monkeypatch):
    def explode(ctx):
        raise RuntimeError("lane is broken")

    monkeypatch.setitem(LANES, Lane.SM_TRENCHES, explode)
    signals = evaluate_all(build_ctx(tmp_db, load_fixture("confluence_5")))
    assert [s.lane for s in signals] == [Lane.CONFLUENCE_5]
    assert signals == sorted(signals, key=lambda s: (-s.strength, s.lane.value))


def test_evaluate_and_record_persists_new_signals_only(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture("confluence_5"))
    first = lanes.evaluate_and_record(ctx, tmp_db)
    second = lanes.evaluate_and_record(ctx, tmp_db)
    # confluence-5 and sm-trenches both fire on this tape (see the test above); the second
    # pass inside the same bucket persists nothing new.
    assert {s.lane for s in first} == {Lane.CONFLUENCE_5, Lane.SM_TRENCHES} and second == []
    rows = tmp_db.execute("SELECT lane, token, strength FROM signals").fetchall()
    assert {row["lane"] for row in rows} == {Lane.CONFLUENCE_5.value, Lane.SM_TRENCHES.value}


def test_listing_pop_refuses_a_listing_with_no_venue_timestamp(tmp_db):
    """An untimed listing must not read as "announced zero seconds ago".

    Strength is scored from how recent the announcement is, so falling back to the event
    row's own insertion time gave near-maximum strength on a latency nobody measured —
    and did so most confidently for exactly the sources that failed to report a time.
    """
    ctx = build_ctx(tmp_db, load_fixture("listing_pop"))
    ctx.extras["listing"] = {"venue": "binance", "ts_ms": ctx.now_ms}  # no announced_ms
    assert lanes.listing_pop(ctx) is None

    ctx.extras["listing"] = {"venue": "binance", "announced_ms": ctx.now_ms - 9_000}
    signal = lanes.listing_pop(ctx)
    assert signal is not None
    assert signal.payload["latency_s"] == pytest.approx(9.0)


# --------------------------------------- curve velocity uses the right denominator


def test_curve_velocity_prefers_sol_per_swap_over_sol_per_minute(tmp_db):
    """Per-minute rewards a burst of tiny trades, which is what a volume bot produces.

    Bot-dominated early activity predicts *lower* graduation, so the wrong denominator
    inverts the signal on exactly the launches it most needs to reject.
    """
    fx = load_fixture("curve_velocity")
    ctx = build_ctx(tmp_db, fx)
    ctx.curve = {**(ctx.curve or {}), "sol_per_swap": 0.9, "sol_per_min": 0.01}
    signal = lanes.curve_velocity(ctx)
    assert signal is not None, "a healthy per-swap pace was rejected on the old denominator"
    assert signal.payload["velocity_basis"] == "sol_per_swap"


def test_curve_velocity_derives_per_swap_from_a_trade_count(tmp_db):
    fx = load_fixture("curve_velocity")
    ctx = build_ctx(tmp_db, fx)
    ctx.curve = {**(ctx.curve or {}), "sol_in_curve": 40, "swaps": 100, "sol_per_min": 99}
    signal = lanes.curve_velocity(ctx)
    assert signal is not None
    assert signal.payload["velocity_basis"] == "sol_per_swap"
    assert signal.payload["velocity"] == "0.4"


def test_curve_velocity_falls_back_to_per_minute_and_says_so(tmp_db):
    """The weaker denominator still runs, but it is labelled so a backtest can split them."""
    fx = load_fixture("curve_velocity")
    ctx = build_ctx(tmp_db, fx)
    curve = {k: v for k, v in (ctx.curve or {}).items() if k not in {"sol_per_swap", "swaps"}}
    ctx.curve = {**curve, "sol_per_min": 9.0}
    signal = lanes.curve_velocity(ctx)
    assert signal is not None
    assert signal.payload["velocity_basis"] == "sol_per_min"


def test_curve_velocity_is_silent_with_neither_denominator(tmp_db):
    fx = load_fixture("curve_velocity")
    ctx = build_ctx(tmp_db, fx)
    ctx.curve = {"progress_pct": 50}
    assert lanes.curve_velocity(ctx) is None


def test_curve_velocity_scales_its_floor_to_the_graduation_target(tmp_db):
    """pump.fun curve constants are not constants; observed targets span 0.41-115 SOL.

    The same absolute SOL-per-swap is a token racing to graduation on a small curve and
    a dead one on a large curve, so a fixed floor says opposite things about identical
    behaviour. The floor is 1/457 of the target, which is the average graduating swap.
    """
    fx = load_fixture("curve_velocity")

    # Small curve: 0.41 SOL target -> floor ~0.0009 SOL/swap. A modest pace clears it.
    small = build_ctx(tmp_db, fx)
    small.curve = {**(small.curve or {}), "sol_per_swap": 0.01, "graduation_sol": 0.41}
    signal = lanes.curve_velocity(small)
    assert signal is not None, "a healthy pace on a small curve was judged by a large curve's floor"

    # Large curve: 115 SOL target -> floor ~0.253 SOL/swap. The same pace is far too slow.
    large = build_ctx(tmp_db, fx)
    large.curve = {**(large.curve or {}), "sol_per_swap": 0.01, "graduation_sol": 115}
    assert lanes.curve_velocity(large) is None


def test_curve_velocity_keeps_the_absolute_floor_when_the_target_is_unknown(tmp_db):
    fx = load_fixture("curve_velocity")
    ctx = build_ctx(tmp_db, fx)
    ctx.curve = {**(ctx.curve or {}), "sol_per_swap": 0.5, "graduation_sol": None}
    signal = lanes.curve_velocity(ctx)
    assert signal is not None
    assert signal.payload["velocity_floor"] == "0.18"
