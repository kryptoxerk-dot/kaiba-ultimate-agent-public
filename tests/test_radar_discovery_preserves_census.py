"""LaunchLab discovery is identity evidence, not another daily census sample.

Synthetic fixtures only: every DB is temporary and provider calls are forbidden.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_one, jdump, jload
from kaiba.core.schemas import EvidenceBasis
from kaiba.hunters import radar

DAY = 86_400_000
HOUR = 3_600_000
DAY_ONE = 1_790_035_200_000
IDENTITY = "regression-only-config"


@pytest.fixture(autouse=True)
def _no_provider_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("This regression must not access providers or the default DB")

    monkeypatch.setattr(radar, "get_json", forbidden)
    monkeypatch.setattr(radar, "post_json", forbidden)
    monkeypatch.setattr(radar, "get_conn", forbidden)


def _census(value="30000", **meta):
    return radar.Candidate(
        kind=radar.KIND_PLATFORM_CONFIG,
        layer="launchlab_census",
        identity=IDENTITY,
        display_name="Census venue",
        chain_slug="solana",
        value=None if value is None else Decimal(value),
        unit="usd_per_day_proxy",
        basis=EvidenceBasis.UNAVAILABLE if value is None else EvidenceBasis.DERIVED,
        meta={
            "program": radar.DEFAULT_CONFIG.launchlab_program,
            "venue_name": "Census venue",
            "pools": 20,
            "pools_priced": 18,
            "pools_unpriced": 2,
            "new_pool_volume_usd": "3000000",
            "fee_rate_ppm": "10000",
            "first_pool_ms": DAY_ONE - HOUR,
            "last_pool_ms": DAY_ONE - 1000,
            "sample_mint": "census-only-mint",
            "census_partial": False,
            "proxy_is_lower_bound": True,
            **meta,
        },
    )


def _discovery(**meta):
    return radar.Candidate(
        kind=radar.KIND_PLATFORM_CONFIG,
        layer="launchlab_discover",
        identity=IDENTITY,
        display_name="Discovery venue",
        chain_slug="solana",
        value=None,
        unit="usd_per_day_proxy",
        basis=EvidenceBasis.UNAVAILABLE,
        silent=True,
        meta={
            "program": radar.DEFAULT_CONFIG.launchlab_program,
            "venue_name": "Discovery venue",
            "pools": 1,
            "pools_priced": 1,
            "pools_unpriced": 0,
            "new_pool_volume_usd": "9999999999",
            "fee_rate_ppm": "10000",
            "first_pool_ms": DAY_ONE + HOUR,
            "last_pool_ms": DAY_ONE + HOUR,
            "sample_mint": "discovery-only-mint",
            "census_partial": False,
            "proxy_is_lower_bound": True,
            **meta,
        },
    )


def _row(conn, candidate=None):
    key = (candidate or _census()).radar_key
    row = fetch_one(conn, "SELECT * FROM radar_registry WHERE radar_key=?", (key,))
    assert row is not None
    return row


def test_day_one_census_discovery_day_two_census_keeps_confirmation(tmp_db):
    first = radar.observe(tmp_db, _census(), now=DAY_ONE)
    before = _row(tmp_db)
    discovery = radar.observe(tmp_db, _discovery(), now=DAY_ONE + 2 * HOUR)
    after = _row(tmp_db)
    second = radar.observe(tmp_db, _census(), now=DAY_ONE + DAY)

    assert (first.confirm_days, after["confirm_days"], second.confirm_days) == (1, 1, 2)
    for field in (
        "last_value", "last_unit", "last_basis", "last_floor", "tier", "layer",
        "confirm_days", "confirm_last_day", "best_value", "best_seen_ms",
        "reported_tier", "reported_ms", "baseline", "first_seen_ms", "meta_json",
        "kind", "identity", "display_name", "chain_slug", "native_lane", "tractability",
    ):
        assert after[field] == before[field], field
    assert after["last_seen_ms"] == DAY_ONE + 2 * HOUR
    assert after["observations"] == before["observations"] + 1
    assert not discovery.reported
    assert discovery.candidate.value is None
    assert discovery.headroom is None
    assert discovery.rank_score is None
    assert discovery.confirm_days == 1
    assert not first.reported and not second.reported
    assert jload(after["meta_json"])["pools"] == 20


def test_discovery_does_not_refresh_the_retained_value_sample(tmp_db):
    radar.observe(tmp_db, _census(), now=DAY_ONE)
    for ts in (DAY_ONE + HOUR, DAY_ONE + DAY, DAY_ONE + 2 * DAY):
        out = radar.observe(tmp_db, _discovery(), now=ts)
        row = _row(tmp_db)
        meta = jload(row["meta_json"])
        assert meta.get("value_observed_ms") == DAY_ONE
        assert meta.get("value_sample_day") == radar.utc_day(DAY_ONE)
        assert row["last_seen_ms"] == ts  # Identity liveness is not value freshness.
        assert row["confirm_last_day"] == radar.utc_day(DAY_ONE)
        assert row["confirm_days"] == 1
        assert not out.reported and out.rank_score is None

    # Discovery on intervening days cannot fill a missing priced census day.
    fresh = radar.observe(tmp_db, _census("31000"), now=DAY_ONE + 3 * DAY)
    assert fresh.confirm_days == 1 and not fresh.reported
    meta = jload(_row(tmp_db)["meta_json"])
    assert meta["value_observed_ms"] == DAY_ONE + 3 * DAY
    assert meta["value_sample_day"] == radar.utc_day(DAY_ONE + 3 * DAY)


@pytest.mark.parametrize(
    "first_layer,second_layer,first_pool,second_pool",
    [
        ("discover", "census", DAY_ONE - 3 * HOUR, DAY_ONE - HOUR),
        ("census", "discover", DAY_ONE - HOUR, DAY_ONE - 3 * HOUR),
        ("census", "census", DAY_ONE - 3 * HOUR, DAY_ONE - HOUR),
        ("discover", "discover", DAY_ONE - 3 * HOUR, DAY_ONE - HOUR),
        ("census", "discover", DAY_ONE - HOUR, None),
        ("discover", "census", None, DAY_ONE - HOUR),
        ("census", "census", None, None),
        ("census", "census", DAY_ONE - HOUR, DAY_ONE - HOUR),
    ],
)
def test_earliest_observed_pool_keeps_its_provenance(
    tmp_db, first_layer, second_layer, first_pool, second_pool,
):
    makers = {"discover": _discovery, "census": _census}
    first = makers[first_layer](first_pool_ms=first_pool)
    second = makers[second_layer](first_pool_ms=second_pool)
    first_meta = dict(first.meta)
    second_meta = dict(second.meta)
    radar.observe(tmp_db, first, now=DAY_ONE)
    radar.observe(tmp_db, second, now=DAY_ONE + 2 * HOUR)
    row = _row(tmp_db)
    meta = jload(row["meta_json"])

    observed = [(first_pool, first.layer, DAY_ONE),
                (second_pool, second.layer, DAY_ONE + 2 * HOUR)]
    known = [item for item in observed if item[0] is not None]
    expected = min(known, key=lambda item: item[0]) if known else (None, None, None)
    assert (meta.get("first_pool_ms"), meta.get("first_pool_source_layer"),
            meta.get("first_pool_observed_ms")) == expected
    # Keep the current sample's window boundary separately from the earliest-ever pool.
    census_survives = first_layer == "census" and second_layer == "discover"
    sample = first if census_survives else second
    assert meta.get("sample_first_pool_ms") == sample.meta["first_pool_ms"]
    assert row["layer"] == sample.layer
    assert meta["program"] == radar.DEFAULT_CONFIG.launchlab_program
    assert meta["venue_name"] == sample.meta["venue_name"]
    assert meta["sample_mint"] == sample.meta["sample_mint"]
    assert row["identity"] == IDENTITY
    assert row["first_seen_ms"] == DAY_ONE
    assert row["observations"] == 2
    assert first.meta == first_meta and second.meta == second_meta


def _page(now, volume="30000000"):
    return {"success": True, "data": {"rows": [{
        "createAt": now - 1000,
        "mint": "fixture-only-mint",
        "volumeU": volume,
        "platformInfo": {"pubKey": IDENTITY, "name": "Fixture venue", "feeRate": "10000"},
    }], "nextPageId": None}}


def test_discovery_alone_never_prices_escalates_or_reports(tmp_db):
    for day in range(4):
        ts = DAY_ONE + day * DAY
        result = radar.sweep(
            tmp_db, only=["launchlab_discover"], force=True, now=ts,
            oracles=radar.OracleSets(),
            raw={"launchlab_discover": {"raw_pages": [_page(ts, volume="999999999999")]}},
        )
        row = _row(tmp_db)
        assert result.seen == 1 and result.new == (1 if day == 0 else 0)
        assert result.reported == 0 and result.finds == [] and result.requests == 0
        assert row["layer"] == "launchlab_discover"
        assert row["last_value"] is None and row["best_value"] is None
        assert row["last_basis"] == EvidenceBasis.UNAVAILABLE.value
        assert row["tier"] == 0 and row["reported_tier"] == 0
        assert row["confirm_days"] == 0 and row["confirm_last_day"] is None
        assert row["reported_ms"] is None
        meta = jload(row["meta_json"])
        assert meta.get("value_observed_ms") is None
        assert meta.get("value_sample_day") is None
        assert "independent_source_count" not in meta
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM radar_finds")["n"] == 0


@pytest.mark.parametrize("order", [
    ("launchlab_census", "launchlab_discover"),
    ("launchlab_discover", "launchlab_census"),
])
def test_layer_order_preserves_existing_baseline_semantics(tmp_db, order):
    for day in range(2):
        ts = DAY_ONE + day * DAY
        result = radar.sweep(
            tmp_db, only=order, force=True, now=ts, oracles=radar.OracleSets(),
            raw={name: {"raw_pages": [_page(ts)]} for name in order},
        )
        row = _row(tmp_db)
        assert result.seen == 2 and result.new == (1 if day == 0 else 0)
        assert result.reported == 0 and result.requests == 0 and not result.errors
        assert row["baseline"] == 1
        assert row["layer"] == "launchlab_census"
        assert row["last_value"] == "300000"
        assert row["tier"] == 2 and row["reported_tier"] == 2
        assert row["reported_ms"] is None
        assert row["confirm_days"] == day + 1
        meta = jload(row["meta_json"])
        assert meta["value_observed_ms"] == ts
        assert meta["first_pool_ms"] == DAY_ONE - 1000
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM radar_finds")["n"] == 0


def test_discovery_before_first_census_does_not_count_as_confirmation(tmp_db):
    initial = radar.observe(tmp_db, _discovery(), now=DAY_ONE)
    assert initial.new_row and initial.confirm_days == 0 and not initial.reported
    first = radar.observe(tmp_db, _census(), now=DAY_ONE + 2 * HOUR)
    assert not first.new_row and first.confirm_days == 1 and not first.reported
    second = radar.observe(tmp_db, _census(), now=DAY_ONE + DAY)
    assert second.confirm_days == 2 and not second.reported
    third = radar.observe(tmp_db, _census(), now=DAY_ONE + 2 * DAY)
    assert third.confirm_days == 3 and third.reported
    assert _row(tmp_db)["first_seen_ms"] == DAY_ONE


def test_duplicate_same_day_samples_only_report_on_third_census_day(tmp_db):
    reports = []
    for day in range(4):
        for hour in (0, 2, 4):
            ts = DAY_ONE + day * DAY + hour * HOUR
            sampled = radar.observe(tmp_db, _census(), now=ts)
            if sampled.reported:
                radar.record_find(tmp_db, sampled, now=ts)
                reports.append((day, hour))
            discovered = radar.observe(tmp_db, _discovery(), now=ts + 1)
            assert sampled.confirm_days == discovered.confirm_days == day + 1
            assert not discovered.reported
            assert _row(tmp_db)["confirm_last_day"] == radar.utc_day(ts)
    assert reports == [(2, 0)]
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM radar_finds")["n"] == 1


@pytest.mark.parametrize("value", ["0", "24999"])
def test_fresh_below_floor_census_still_resets_immediately(tmp_db, value):
    radar.observe(tmp_db, _census(), now=DAY_ONE)
    radar.observe(tmp_db, _discovery(), now=DAY_ONE + HOUR)
    radar.observe(tmp_db, _census(), now=DAY_ONE + DAY)
    assert _row(tmp_db)["confirm_days"] == 2

    ts = DAY_ONE + DAY + HOUR
    below = radar.observe(tmp_db, _census(value), now=ts)
    assert below.tier == 0 and below.confirm_days == 0 and not below.reported
    radar.observe(tmp_db, _discovery(), now=ts + HOUR)
    row = _row(tmp_db)
    assert row["last_value"] == value and row["best_value"] == "30000"
    assert row["last_basis"] == EvidenceBasis.DERIVED.value
    assert row["tier"] == 0 and row["confirm_days"] == 0 and row["confirm_last_day"] is None
    meta = jload(row["meta_json"])
    assert meta["value_observed_ms"] == ts and meta["value_sample_day"] == radar.utc_day(ts)
    # A same-day rebound cannot reuse the previous two days' confirmation.
    rebound = radar.observe(tmp_db, _census(), now=ts + 2 * HOUR)
    assert rebound.confirm_days == 1 and not rebound.reported
    next_day = radar.observe(tmp_db, _census(), now=DAY_ONE + 2 * DAY)
    assert next_day.confirm_days == 2 and not next_day.reported


def test_unavailable_census_is_not_mistaken_for_registry_only_discovery(tmp_db):
    radar.observe(tmp_db, _census(), now=DAY_ONE)
    partial = replace(_census(None, census_partial=True), silent=True)
    out = radar.observe(tmp_db, partial, now=DAY_ONE + DAY)
    assert out.confirm_days == 0 and out.tier == 0 and not out.reported
    radar.observe(tmp_db, _discovery(), now=DAY_ONE + DAY + HOUR)
    row = _row(tmp_db)
    assert row["last_value"] is None and row["best_value"] == "30000"
    assert row["last_basis"] == EvidenceBasis.UNAVAILABLE.value
    assert row["confirm_days"] == 0 and row["confirm_last_day"] is None
    meta = jload(row["meta_json"])
    assert meta["census_partial"] is True
    assert meta["value_observed_ms"] is None and meta["value_sample_day"] is None
    assert radar.observe(tmp_db, _census(), now=DAY_ONE + 2 * DAY).confirm_days == 1


@pytest.mark.parametrize("baseline_mode,may_report", [(False, True), (True, True), (False, False)])
def test_discovery_cannot_consume_an_unreported_census_tier(tmp_db, baseline_mode, may_report):
    radar.observe(tmp_db, _census("300000", independent_source_count=1), now=DAY_ONE,
                  may_report=False)
    discovery = _discovery(independent_source_count=2)
    out = radar.observe(tmp_db, discovery, now=DAY_ONE + HOUR,
                        baseline_mode=baseline_mode, may_report=may_report)
    assert not out.reported and out.candidate.value is None and out.rank_score is None
    row = _row(tmp_db)
    assert row["tier"] == 2 and row["reported_tier"] == 0 and row["baseline"] == 0
    assert jload(row["meta_json"])["independent_source_count"] == 1
    fresh = radar.observe(tmp_db, _census("300000"), now=DAY_ONE + DAY)
    assert fresh.reported and fresh.tier == 2


def test_same_display_name_does_not_merge_distinct_config_identities(tmp_db):
    census = _census()
    radar.observe(tmp_db, census, now=DAY_ONE)
    before = _row(tmp_db, census)
    other = replace(_discovery(), identity=IDENTITY.upper(), display_name=census.display_name)
    out = radar.observe(tmp_db, other, now=DAY_ONE + HOUR)
    assert census.radar_key != other.radar_key
    assert out.new_row and out.tier == 0 and not out.reported
    assert _row(tmp_db, census) == before
    assert _row(tmp_db, other)["last_value"] is None
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM radar_registry")["n"] == 2


def test_legacy_missing_provenance_is_not_fabricated_from_new_discovery(tmp_db):
    census = _census()
    radar.observe(tmp_db, census, now=DAY_ONE)
    # An old row has the sampled first-pool timestamp but no retained provenance clock.
    tmp_db.execute("UPDATE radar_registry SET meta_json=? WHERE radar_key=?",
                   (jdump(census.meta), census.radar_key))
    radar.observe(tmp_db, _discovery(), now=DAY_ONE + DAY)
    row = _row(tmp_db)
    meta = jload(row["meta_json"])
    assert row["last_value"] == "30000" and row["confirm_last_day"] == radar.utc_day(DAY_ONE)
    assert meta["first_pool_ms"] == census.meta["first_pool_ms"]
    assert meta.get("first_pool_source_layer") is None
    assert meta.get("first_pool_observed_ms") is None
    assert meta.get("value_observed_ms") is None
    assert meta.get("value_sample_day") is None
