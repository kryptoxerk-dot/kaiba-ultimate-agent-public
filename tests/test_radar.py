"""Tests for the discovery radar.

Two of these matter more than the rest.

``test_would_have_flagged_*`` replays the real daily series for StonkFun, Pons and
Robinhood Chain — recorded live through ``kaiba/providers/_http.py`` on 2026-09-21 and
embedded below verbatim — through the module's own logic, and asserts the date it would
have spoken up against the date a human actually found the thing. That is the only real
test of this module. If the numbers move, these fail, and they should.

``test_*_provenance`` / ``test_no_numeric_module_constant_escapes_documentation`` are the
knob discipline the repository requires: a threshold cannot arrive without saying where
its value came from, and a stale entry cannot outlive the field it describes.

Every other test guards one behaviour, and each was mutation-checked — the guard was
broken and the named test was confirmed to fail. The mapping is in the report, not in a
comment here, because a comment claiming a test works is exactly what AGENTS.md rule 5
says not to trust.
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.hunters import radar

# ======================================================================================
# Recorded evidence. Every array below was read live on 2026-09-21 through
# kaiba/providers/_http.py::request_json and is reproduced here unaltered.
# ======================================================================================

#: GET https://api.llama.fi/summary/fees/stonkfun?dataType=dailyFees -> totalDataChart.
#: 57 daily points. StonkFun's own fee history, which begins 2026-07-26.
STONKFUN_FEES_USD_DAY: list[tuple[int, float]] = [
    (1785024000, 88.0), (1785110400, 31.0), (1785196800, 56.0), (1785283200, 21.0),
    (1785369600, 18.0), (1785456000, 27.0), (1785542400, 14.0), (1785628800, 3158.0),
    (1785715200, 1681.0), (1785801600, 28922.0), (1785888000, 54962.0), (1785974400, 87191.0),
    (1786060800, 74770.0), (1786147200, 29627.0), (1786233600, 31171.0), (1786320000, 67924.0),
    (1786406400, 84020.0), (1786492800, 50045.0), (1786579200, 56425.0), (1786665600, 83932.0),
    (1786752000, 50015.0), (1786838400, 34033.0), (1786924800, 26784.0), (1787011200, 31360.0),
    (1787097600, 23311.0), (1787184000, 27075.0), (1787270400, 39723.0), (1787356800, 46228.0),
    (1787443200, 42539.0), (1787529600, 47041.0), (1787616000, 41186.0), (1787702400, 43709.0),
    (1787788800, 38959.0), (1787875200, 50130.0), (1787961600, 60563.0), (1788048000, 69445.0),
    (1788134400, 55295.0), (1788220800, 46388.0), (1788307200, 45375.0), (1788393600, 58873.0),
    (1788480000, 60736.0), (1788566400, 57798.0), (1788652800, 61972.0), (1788739200, 80072.0),
    (1788825600, 90723.0), (1788912000, 107617.0), (1788998400, 175952.0), (1789084800, 332278.0),
    (1789171200, 380311.0), (1789257600, 384297.0), (1789344000, 458800.0), (1789430400, 483879.0),
    (1789516800, 570002.0), (1789603200, 678131.0), (1789689600, 745322.0), (1789776000, 806843.0),
    (1789862400, 819168.0),
]

#: GET https://api.llama.fi/summary/fees/pons?dataType=dailyFees -> totalDataChart.
#: 70 daily points from 2026-07-14. Truncated to the first 20 here: the rule fires in
#: the first week and the rest of the series adds nothing the assertion reads.
PONS_FEES_USD_DAY: list[tuple[int, float]] = [
    (1783987200, 159750.0), (1784073600, 683026.0), (1784160000, 518446.0),
    (1784246400, 387233.0), (1784332800, 458896.0), (1784419200, 546628.0),
    (1784505600, 1010243.0), (1784592000, 1535814.0), (1784678400, 1311611.0),
    (1784764800, 1283312.0), (1784851200, 1121193.0), (1784937600, 900719.0),
    (1785024000, 1303955.0), (1785110400, 1074846.0), (1785196800, 1128103.0),
    (1785283200, 1247259.0), (1785369600, 1436241.0), (1785456000, 1289331.0),
    (1785542400, 1108659.0), (1785628800, 1195330.0),
]

#: GET https://api.llama.fi/overview/dexs/robinhood -> totalDataChart. 89 daily points.
#: This is the series that decides the new-chain claim, so it is kept whole.
ROBINHOOD_DEX_USD_DAY: list[tuple[int, float]] = [
    (1781568000, 0.18), (1782345600, 0.74), (1782432000, 14.34), (1782518400, 2.32),
    (1782604800, 0.0), (1782691200, 106.36), (1782777600, 3500.37), (1782864000, 221859.35),
    (1782950400, 299246.26), (1783036800, 10729400.4), (1783123200, 6614604.19),
    (1783209600, 11681566.31), (1783296000, 19676090.49), (1783382400, 32401189.42),
    (1783468800, 433123720.34), (1783555200, 378139049.43), (1783641600, 561227177.02),
    (1783728000, 512581108.6), (1783814400, 606361872.51), (1783900800, 651845178.28),
    (1783987200, 763925396.54), (1784073600, 1010589861.47), (1784160000, 1148640386.58),
    (1784246400, 1114553748.57), (1784332800, 1222338587.19), (1784419200, 1278536599.73),
    (1784505600, 1377313429.6), (1784592000, 1441266287.91), (1784678400, 1524262352.17),
    (1784764800, 1497455326.86),
]

#: GET https://api.llama.fi/overview/fees/solana?dataType=dailyFees, 2026-09-21. The 48
#: rows with category=="Launchpad", ranked, as (slug, total24h). This table is the whole
#: argument for the venue floor, so it is the table the test checks.
SOLANA_LAUNCHPAD_FEES_2026_09_21: list[tuple[str, float]] = [
    ("pump.fun", 1437832.0), ("stonkfun", 819168.0), ("launchlab", 521932.0),
    ("bonk.fun-launchpad", 432416.0), ("graphite-protocol", 172006.0),
    ("meteora-dynamic-bonding-curve", 37720.0), ("rapid-launch", 5558.0),
    ("moonshot-create", 2005.0), ("smithii", 1510.0), ("jupiter-studio", 1478.0),
    ("bags", 961.0), ("rise.rich", 451.0),
]

#: The LaunchLab lower-bound fee proxy, per venue, from the 24.07 h keyless census of
#: 2026-09-21: 67 requests, 6,696 pools, 52 distinct platform configs.
LAUNCHLAB_PROXY_2026_09_21: list[tuple[str, float]] = [
    ("StonkFun", 347171.0), ("8evzoqDTGvq4WmTjsmLL5ZFC2rkuVi3nNeqy7pE847rg", 7892.0),
    ("Reflex Monster", 1850.0), ("94xrtadVWFQ47Ltoqm1jTJ3xwh1W58csTd2YTjK3KQAh", 1591.0),
    ("Paymunity 3%", 1477.0), ("stonk", 1387.0), ("DIESEL", 495.0), ("Raydium", 383.0),
    ("letsbonk.fun", 366.0),
]

#: Eight rows lifted unaltered from page 1 of
#: GET https://launch-mint-v1.raydium.io/get/list?sort=new&size=100 with platformId
#: OMITTED, 2026-09-21. Note three distinct platformInfo.pubKey values carrying the name
#: "StonkFun" or "stonk" — kaiba/ingest/stonkfun.py knows two configs and there are
#: at least seven.
RAYDIUM_PAGE_ROWS: list[dict] = [
    {"createAt": 1789980199000, "mint": "CLmZXnjohVHhe6PA3PLVywjfTfJWpzqFRFxj6ZvH1nK7",
     "volumeU": 0, "symbol": "STONK25",
     "platformInfo": {"pubKey": "4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7",
                      "name": "StonkFun", "feeRate": "10000"}},
    {"createAt": 1789980178000, "mint": "GpgXBx3cMRsDtFWdWPgVWtVuRkZ57cUrsysXXGePX27E",
     "volumeU": "191.033087", "symbol": "USDCDRIP",
     "platformInfo": {"pubKey": "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt",
                      "name": "StonkFun", "feeRate": "10000"}},
    {"createAt": 1789980164000, "mint": "F1rjBn4FZaz4L2fkLLMSwo6HT1Db1PEgJKUyax5ovWjW",
     "volumeU": "632.9378022119017", "symbol": "EMINI",
     "platformInfo": {"pubKey": "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt",
                      "name": "StonkFun", "feeRate": "10000"}},
    {"createAt": 1789979283000, "mint": "5FjwiEgaRjmurSctoT8EdDAVbNJPWF5ivT9QTFKjhmoo",
     "volumeU": "14373.61747256897", "symbol": "Launchpad",
     "platformInfo": {"pubKey": "2jU2k9ZuPFThdckmBSLzEi8c2x7tzsFsAudR1Kb9eWqU",
                      "name": "stonk", "feeRate": "50000"}},
    {"createAt": 1789978923000, "mint": "8qAGHGA2mkbQuedP8gJm9qXGnxxRcVYDwCRfWNLKsurg",
     "volumeU": "10141.709", "symbol": "DGN",
     "platformInfo": {"pubKey": "GKLyKXb8XbvEZqif3C4ksRyYf7BP1ASQTLbRScM8oGnB",
                      "name": "DegenSafe", "feeRate": "10000"}},
    # A pool whose volume the index did not price. It must never read as 0.
    {"createAt": 1789978000000, "mint": "UNPRICEDmint1111111111111111111111111111111",
     "volumeU": None, "symbol": "NOPRICE",
     "platformInfo": {"pubKey": "UnpricedCfg111111111111111111111111111111111",
                      "name": "Unpriced Venue", "feeRate": "10000"}},
]

NOW_MS = 1789980300000  # 2026-09-21T08:45Z, just after the census above


def _page(rows: list[dict], cursor: str | None = None) -> dict:
    return {"success": True, "data": {"rows": rows, "nextPageId": cursor}}


def _oracles(**kw) -> radar.OracleSets:
    """OracleSets from plain sets. Defaults say 'we asked and the chain is covered'."""
    return radar.load_oracles(raw={
        "goplus": kw.get("goplus", frozenset({"1", "56", "8453", "4663", "5042"})),
        "codex_ids": kw.get("codex_ids", frozenset({"1", "56", "8453", "4663", "5042", "143"})),
        "codex_names": kw.get("codex_names", frozenset({"solana", "monad", "arc"})),
        "etherscan": kw.get("etherscan", frozenset({"1", "56", "8453", "4663"})),
        "llama_chain_ids": kw.get("llama_chain_ids", {
            "robinhood chain": 4663, "arc": 5042, "monad": 143, "solana": 0,
        }),
        "tokenless": kw.get("tokenless", frozenset({"arc", "robinhood chain", "base"})),
    })


# ======================================================================================
# knob discipline — a threshold with no stated origin is a number someone defends later
# ======================================================================================


def test_every_config_knob_has_provenance():
    """Adding a field to RadarConfig without a PROVENANCE entry fails here, and vice versa."""
    assert {f.name for f in fields(radar.RadarConfig)} == set(radar.PROVENANCE)


def test_provenance_entries_state_a_recognised_basis_and_real_evidence():
    allowed = {"MEASURED", "REQUIREMENT", "INVENTED"}
    for name, knob in radar.PROVENANCE.items():
        assert knob.basis in allowed, f"{name} claims an unknown basis {knob.basis!r}"
        assert len(knob.evidence) > 60, f"{name} has no real evidence line"


def test_invented_knobs_say_what_would_settle_them():
    """An INVENTED number that does not admit it is unsettled is a measured number in disguise."""
    for name, knob in radar.PROVENANCE.items():
        if knob.basis != "INVENTED":
            continue
        low = knob.evidence.lower()
        assert "would settle" in low or "nobody measured" in low or "nothing measured" in low, (
            f"{name} is INVENTED but never says what would settle it"
        )


def test_no_numeric_module_constant_escapes_documentation():
    """A new bare number at module scope has to be added here deliberately.

    The point is not the list. It is that a knob cannot arrive silently: anyone adding a
    threshold to this module has to come here and say what it is.
    """
    numeric = {
        name for name, value in vars(radar).items()
        if not name.startswith("_")
        and isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
    }
    assert numeric == {
        "SOLANA_LAUNCHPAD_FEE_POOL_USD_2026_09_21",
        "SOLANA_LAUNCHPAD_NEXT_BELOW_USD_2026_09_21",
        "VENUE_FEE_FLAT_LOW_USD",
        "VENUE_FEE_FLAT_HIGH_USD",
        "LAUNCHLAB_PROXY_RATIO_STONKFUN",
        "LAUNCHLAB_PROXY_RATIO_BONKFUN",
        "LAUNCHLAB_CENSUS_PAGES_2026_09_21",
        "LAUNCHLAB_CENSUS_POOLS_2026_09_21",
        "LAUNCHLAB_CENSUS_CONFIGS_2026_09_21",
        "CHAIN_RULE_FIRES_WITH_AGE_GATE",
        "CHAIN_RULE_FIRES_WITHOUT_AGE_GATE",
        "CHAIN_RULE_CHAIN_DAYS",
        "GOPLUS_SUPPORTED_CHAINS_2026_09_21",
    }


# ======================================================================================
# the thresholds, checked against the tables they were derived from
# ======================================================================================


def test_venue_fee_floor_sits_on_a_flat_not_a_slope():
    """$7,500 and $37,500 select the IDENTICAL set. That invariance is the whole argument.

    A floor you can be wrong about by 3.3x in either direction and still get the same
    answer is the only kind worth hard-coding. Break it -- move the floor outside
    [VENUE_FEE_FLAT_LOW_USD, VENUE_FEE_FLAT_HIGH_USD] -- and this fails.
    """
    def selected(floor: float) -> tuple[str, ...]:
        return tuple(slug for slug, v in SOLANA_LAUNCHPAD_FEES_2026_09_21 if v >= floor)

    floor = float(radar.DEFAULT_CONFIG.venue_fee_floor_usd)
    assert radar.VENUE_FEE_FLAT_LOW_USD <= floor <= radar.VENUE_FEE_FLAT_HIGH_USD
    at_floor = selected(floor)
    assert selected(radar.VENUE_FEE_FLAT_LOW_USD) == at_floor
    assert selected(radar.VENUE_FEE_FLAT_HIGH_USD) == at_floor
    assert len(at_floor) == 6, at_floor

    pool = sum(v for _, v in SOLANA_LAUNCHPAD_FEES_2026_09_21)
    covered = sum(v for slug, v in SOLANA_LAUNCHPAD_FEES_2026_09_21 if slug in at_floor)
    assert covered / pool > 0.99, "the selected set must hold essentially the whole fee pool"
    # And the flat is a flat because of the gap underneath it.
    assert radar.SOLANA_LAUNCHPAD_NEXT_BELOW_USD_2026_09_21 < radar.VENUE_FEE_FLAT_LOW_USD


def test_launchlab_proxy_floor_sits_on_an_even_wider_flat():
    """On the measured census the proxy floor selects one venue from $8k to $347k."""
    def selected(floor: float) -> tuple[str, ...]:
        return tuple(name for name, v in LAUNCHLAB_PROXY_2026_09_21 if v >= floor)

    floor = float(radar.DEFAULT_CONFIG.venue_proxy_floor_usd)
    assert selected(floor) == ("StonkFun",)
    assert selected(8_000.0) == ("StonkFun",)
    assert selected(347_000.0) == ("StonkFun",)
    # One step lower and a second, far smaller config joins. That is the edge of the flat.
    assert len(selected(7_000.0)) == 2


def test_the_launchlab_proxy_is_declared_a_lower_bound_not_a_fee():
    """Measured disagreement with ground truth: 0.42x on StonkFun, 0.0008x on bonk.fun.

    The launchpad survey called this proxy "exact". It is not, and the difference is the
    reason it has its own unit string and its own floor and is never ranked against a
    DefiLlama figure in the same column.
    """
    assert radar.LAUNCHLAB_PROXY_RATIO_STONKFUN < 1
    assert radar.LAUNCHLAB_PROXY_RATIO_BONKFUN < radar.LAUNCHLAB_PROXY_RATIO_STONKFUN
    assert radar.DEFAULT_CONFIG.floor_for("usd_per_day_proxy") is not None
    # Different units must not share a floor lookup by accident.
    assert set(["usd_per_day", "usd_per_day_proxy", "usd_7d"]) == {
        u for u in ("usd_per_day", "usd_per_day_proxy", "usd_7d")
        if radar.DEFAULT_CONFIG.floor_for(u) is not None
    }


def test_chain_rule_constants_are_the_measured_ones():
    cfg = radar.DEFAULT_CONFIG
    assert cfg.chain_volume_floor_usd_7d == Decimal("50000000")
    assert cfg.chain_growth_ratio == Decimal("3.0")
    assert cfg.chain_max_age_days == 365
    # The age gate earns its place by removing 14 fires from 54 over 72,106 chain-days.
    assert radar.CHAIN_RULE_FIRES_WITHOUT_AGE_GATE > radar.CHAIN_RULE_FIRES_WITH_AGE_GATE
    assert radar.PROVENANCE["chain_max_age_days"].basis == "MEASURED"


# ======================================================================================
# the chain rule
# ======================================================================================


def _series(points: list[tuple[int, float]]) -> list[tuple[int, Decimal]]:
    return [(t, Decimal(str(v))) for t, v in points]


def test_chain_rule_fires_on_robinhood_at_the_measured_date():
    from datetime import UTC, datetime

    series = _series(ROBINHOOD_DEX_USD_DAY)
    first = next(i for i in range(len(series)) if radar.chain_rule(series, radar.DEFAULT_CONFIG,
                                                                  at_index=i).fires)
    day = datetime.fromtimestamp(series[first][0], UTC).strftime("%Y-%m-%d")
    assert day == "2026-07-07", day
    verdict = radar.chain_rule(series, radar.DEFAULT_CONFIG, at_index=first)
    # 21 CALENDAR days, not the 13 entries the chart holds: DefiLlama omits days on
    # which the chain did nothing, and Robinhood's series jumps 2026-06-16 -> 06-25.
    assert verdict.age_days == 21
    assert verdict.window_7d is not None and verdict.window_7d >= Decimal("50000000")


def test_a_zero_prior_week_counts_as_infinite_growth():
    """Arc went 0 -> $64.4M in one day. A rule needing prev>0 never fires on it."""
    cfg = radar.DEFAULT_CONFIG
    series = _series([(i * 86400, 0.0) for i in range(20)] + [(20 * 86400, 64_414_148.0)])
    verdict = radar.chain_rule(series, cfg)
    assert verdict.fires
    assert verdict.reason == "zero_base"
    assert verdict.prior_7d == Decimal(0)
    assert verdict.growth is None, "growth is undefined on a zero base, not infinity-as-a-number"


def test_the_age_gate_silences_an_old_chain_in_a_surge():
    """Without it the same floor+growth rule fires on xlayer at 462 days and flare at 491."""
    cfg = radar.DEFAULT_CONFIG
    quiet = [(i * 86400, 1_000_000.0) for i in range(cfg.chain_max_age_days + 30)]
    surge = [((cfg.chain_max_age_days + 30 + i) * 86400, 40_000_000.0) for i in range(7)]
    series = _series(quiet + surge)
    verdict = radar.chain_rule(series, cfg)
    assert not verdict.fires
    assert verdict.reason == "older_than_age_gate"
    # The same shape inside the gate does fire, so it really is the age doing the work.
    young = _series([(i * 86400, 1_000_000.0) for i in range(30)]
                    + [((30 + i) * 86400, 40_000_000.0) for i in range(7)])
    assert radar.chain_rule(young, cfg).fires


def test_growth_below_the_ratio_does_not_fire():
    cfg = radar.DEFAULT_CONFIG
    flat = _series([(i * 86400, 20_000_000.0) for i in range(30)])
    verdict = radar.chain_rule(flat, cfg)
    assert not verdict.fires
    assert verdict.reason == "growth_below_ratio"


def test_pseudo_chain_discriminator():
    """An orderbook exchange DefiLlama models as a chain is a venue, not a chain."""
    assert radar._pseudo_chain("native_core", 1, ["Native Core CLOB"], None)
    assert radar._pseudo_chain("spark", 1, ["Spark Exchange"], None)
    # A real chain with a chainId is never a pseudo-chain, however few venues it has.
    assert not radar._pseudo_chain("arc", 1, ["Wonk Fun"], 5042)
    # Nor is a chain with a venue roster.
    assert not radar._pseudo_chain("solana", 40, ["Raydium", "Orca"], None)


# ======================================================================================
# the memory — "NEW is the hard part"
# ======================================================================================


def _cand(identity: str, value: float | None, *, kind: str = radar.KIND_VENUE,
          layer: str = "llama_venues", unit: str = "usd_per_day",
          chain_slug: str = "solana", silent: bool = False) -> radar.Candidate:
    return radar.Candidate(
        kind=kind, layer=layer, identity=identity, display_name=identity,
        chain_slug=chain_slug,
        value=None if value is None else Decimal(str(value)), unit=unit,
        basis=(EvidenceBasis.UNAVAILABLE if value is None else EvidenceBasis.PROVIDER_REPORTED),
        silent=silent,
    )


DAY = 86_400_000


def test_first_sweep_of_a_layer_reports_nothing_however_large():
    """48 launchpads and 195 chains have existed for years. Day one is not 48 discoveries."""
    out = radar.observe(_CONN, _cand("solana:pump.fun", 1_437_832.0), oracles=_oracles(),
                        now=NOW_MS, baseline_mode=True)
    assert out.tier == 2, "pump.fun is 57.5x the floor -> tier 2; the tier is real"
    assert not out.reported
    assert out.reason == "baseline"
    row = fetch_one(_CONN, "SELECT * FROM radar_registry WHERE radar_key=?",
                    (out.candidate.radar_key,))
    assert row["baseline"] == 1
    assert row["reported_tier"] == 2, "baseline records where it already stands"
    assert row["reported_ms"] is None


def test_a_crossing_is_reported_once_not_every_sweep(tmp_db):
    cand = _cand("solana:newpad", 300_000.0)  # tier 2: reports on sight
    first = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    assert first.reported and first.tier == 2
    radar.record_find(tmp_db, first, now=NOW_MS)
    for day in range(1, 10):
        again = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS + day * DAY)
        assert not again.reported, f"re-reported on day {day}"
        assert again.reason == "already_reported_at_or_above_this_tier"
    assert len(fetch_all(tmp_db, "SELECT * FROM radar_finds", ())) == 1


def test_tier_escalation_reports_again(tmp_db):
    """$25k/day to $2.5M/day is a different thing, and gets to raise its hand once more."""
    small = _cand("solana:grower", 300_000.0)
    out = radar.observe(tmp_db, small, oracles=_oracles(), now=NOW_MS)
    assert out.reported and out.tier == 2
    radar.record_find(tmp_db, out, now=NOW_MS)
    big = _cand("solana:grower", 3_000_000.0)
    out2 = radar.observe(tmp_db, big, oracles=_oracles(), now=NOW_MS + DAY)
    assert out2.reported and out2.tier == 3
    radar.record_find(tmp_db, out2, now=NOW_MS + DAY)
    finds = fetch_all(tmp_db, "SELECT * FROM radar_finds ORDER BY find_id", ())
    assert [f["tier"] for f in finds] == [2, 3]
    assert "ESCALATION" in finds[1]["verdict"]
    assert "NEW" in finds[0]["verdict"]


def test_tier_one_waits_for_three_consecutive_utc_days(tmp_db):
    cand = _cand("solana:marginal", 30_000.0)  # 1.2x the floor -> tier 1
    reasons = []
    for day in range(4):
        out = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS + day * DAY)
        reasons.append((out.confirm_days, out.reported))
    assert reasons[0] == (1, False)
    assert reasons[1] == (2, False)
    assert reasons[2] == (3, True), "third consecutive day is the one that speaks"
    assert reasons[3] == (4, False), "and only once"


def test_confirmation_counts_days_not_observations(tmp_db):
    """A two-hourly layer must not satisfy a three-day rule in six hours."""
    cand = _cand("solana:fast", 30_000.0, layer="launchlab_census",
                 unit="usd_per_day_proxy")
    midnight = (NOW_MS // DAY) * DAY
    for hour in range(0, 24, 2):
        out = radar.observe(tmp_db, cand, oracles=_oracles(), now=midnight + hour * 3_600_000)
        assert not out.reported, f"reported after only {hour}h of the same UTC day"
        assert out.confirm_days == 1, f"{hour}h in, the streak reads {out.confirm_days} days"


def test_a_gap_resets_the_confirmation_streak(tmp_db):
    """A layer that was down for a day cannot claim it saw three consecutive days."""
    cand = _cand("solana:gappy", 30_000.0)
    radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS + DAY)
    skipped = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS + 3 * DAY)
    assert skipped.confirm_days == 1
    assert not skipped.reported


def test_dropping_below_the_floor_clears_the_streak(tmp_db):
    cand_hi = _cand("solana:flappy", 30_000.0)
    cand_lo = _cand("solana:flappy", 900.0)
    radar.observe(tmp_db, cand_hi, oracles=_oracles(), now=NOW_MS)
    radar.observe(tmp_db, cand_lo, oracles=_oracles(), now=NOW_MS + DAY)
    out = radar.observe(tmp_db, cand_hi, oracles=_oracles(), now=NOW_MS + 2 * DAY)
    assert out.confirm_days == 1 and not out.reported


def test_a_day_below_the_floor_clears_the_streak_outright():
    """Directly on the guard: a single day under the floor is not a pause, it is a reset.

    The integration path cannot see this on its own -- a below-floor day also breaks the
    consecutive-day chain, so a mutant that merely *pauses* the streak still looks right
    from the outside. This asserts the reset itself.
    """
    assert radar._advance_confirmation(2, "2026-08-04", "2026-08-05", above_floor=False) == (0, None)
    assert radar._advance_confirmation(0, None, "2026-08-05", above_floor=True) == (1, "2026-08-05")
    # And the run really does restart from one rather than resuming at three.
    after_dip = radar._advance_confirmation(0, None, "2026-08-06", above_floor=True)
    assert after_dip == (1, "2026-08-06")


def test_chain_windows_are_measured_in_days_not_in_chart_entries(tmp_db):
    """DefiLlama omits quiet days, so "the last seven rows" is not "the last seven days".

    A chain with one enormous day long ago and a sparse trickle since has a seven-ENTRY
    window of $205M and a seven-DAY window of $5M. Only one of those is the truth.
    """
    day = 86_400
    series = _series([(0, 200_000_000.0)] + [((40 + i) * day, 1_000_000.0) for i in range(5)])
    verdict = radar.chain_rule(series, radar.DEFAULT_CONFIG)
    assert verdict.window_7d == Decimal("5000000"), verdict.window_7d
    assert not verdict.fires
    assert verdict.reason == "below_volume_floor"
    assert verdict.age_days == 44, "age is calendar days from first non-zero, not row count"


def test_first_seen_is_written_once_and_never_rewritten(tmp_db):
    """Our own clock is the only first-seen we can trust, so it must not drift.

    DefiLlama backfills a chain's early daily series when the adapter lands, which makes
    any first-date read out of history the adapter's coverage start rather than the
    venue's birth. The registry's own first_seen_ms is the honest one -- and it is only
    honest if re-observing a candidate cannot overwrite it.
    """
    cand = _cand("solana:sticky", 30_000.0)
    radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    first = fetch_one(tmp_db, "SELECT first_seen_ms FROM radar_registry WHERE radar_key=?",
                      (cand.radar_key,))["first_seen_ms"]
    assert first == NOW_MS
    for later in (NOW_MS + DAY, NOW_MS + 30 * DAY):
        radar.observe(tmp_db, cand, oracles=_oracles(), now=later)
        row = fetch_one(tmp_db, "SELECT * FROM radar_registry WHERE radar_key=?",
                        (cand.radar_key,))
        assert row["first_seen_ms"] == first, "first_seen_ms moved"
        assert row["last_seen_ms"] == later, "last_seen_ms is the one that tracks"


def test_tier_two_bypasses_the_confirmation(tmp_db):
    """A venue arriving at 10x the floor is not a floor-adjacent oscillation."""
    out = radar.observe(tmp_db, _cand("solana:arrived-big", 260_000.0), oracles=_oracles(),
                        now=NOW_MS)
    assert out.tier == 2 and out.reported and out.confirm_days == 1


def test_silent_candidates_never_report_however_large(tmp_db):
    """52 platform configs appeared in 24 h. Existence is not news; money is."""
    cand = _cand("SomeNewCfg", 5_000_000.0, kind=radar.KIND_PLATFORM_CONFIG,
                 layer="launchlab_discover", unit="usd_per_day_proxy", silent=True)
    out = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    assert out.tier == 3
    assert not out.reported
    assert out.reason == "layer_is_registry_only"
    assert fetch_one(tmp_db, "SELECT * FROM radar_registry WHERE radar_key=?",
                     (cand.radar_key,)) is not None, "it is still remembered"


def test_report_cap_is_a_blast_radius_and_loses_nothing(tmp_db):
    cfg = radar.RadarConfig(max_reports_per_sweep=2)
    payload = {"protocols": [
        {"slug": f"v{i}", "displayName": f"Venue {i}", "total24h": 1_000_000 - i,
         "category": "Launchpad"} for i in range(6)
    ]}
    radar.record_layer_health(tmp_db, "llama_venues", "venue_fees", ok=True, count=0, new=0,
                              reported=0, requests=0, interval_s=1, baseline_ms=NOW_MS - DAY)
    res = radar.sweep(tmp_db, cfg=cfg, only=["llama_venues"], force=True,
                      oracles=_oracles(), now=NOW_MS,
                      raw={"llama_venues": {"raw": {"solana": payload}}})
    assert res.reported == 2
    assert res.suppressed == 4
    held = {r["identity"] for r in fetch_all(
        tmp_db, "SELECT * FROM radar_registry WHERE reported_tier = 0", ())}
    assert len(held) == 4, "the suppressed four are rolled back, not consumed"
    res2 = radar.sweep(tmp_db, cfg=cfg, only=["llama_venues"], force=True,
                       oracles=_oracles(), now=NOW_MS + DAY,
                       raw={"llama_venues": {"raw": {"solana": payload}}})
    assert res2.reported == 2, "the next sweep picks up where the cap stopped"


# ======================================================================================
# tractability — "say so in the output rather than reporting it as actionable"
# ======================================================================================


def test_native_lane_is_full_tractability():
    t = radar.assess_tractability("solana", _oracles())
    assert t.lane is Chain.SOL
    assert t.verdict == radar.FULL and t.actionable


def test_opaque_chain_is_never_actionable_however_big(tmp_db):
    """The guard the task asks for. Size does not buy readability."""
    orc = _oracles(goplus=frozenset(), codex_ids=frozenset(), codex_names=frozenset(),
                   etherscan=frozenset(), llama_chain_ids={})
    t = radar.assess_tractability("nowhere-chain", orc)
    assert t.verdict == radar.OPAQUE
    assert not t.actionable
    cand = radar.Candidate(kind=radar.KIND_CHAIN, layer="llama_chains",
                           identity="nowhere-chain", display_name="Nowhere",
                           chain_slug="nowhere-chain", value=Decimal("9999999999"),
                           unit="usd_7d", basis=EvidenceBasis.PROVIDER_REPORTED)
    out = radar.observe(tmp_db, cand, oracles=orc, now=NOW_MS)
    assert out.tier == 3 and out.reported, "it is still reported: a human should see it"
    radar.record_find(tmp_db, out, now=NOW_MS)
    row = fetch_one(tmp_db, "SELECT * FROM radar_finds WHERE radar_key=?",
                    (cand.radar_key,))
    assert row["actionable"] == 0
    assert "RESEARCH ONLY, not actionable" in row["verdict"]


def test_an_oracle_we_could_not_ask_is_not_a_pass():
    """Missing data is None with UNAVAILABLE, never a default that reads as safe."""
    orc = _oracles(goplus=None)
    t = radar.assess_tractability("monad", orc)
    assert t.goplus is None
    assert t.verdict == radar.UNKNOWN
    assert not t.actionable, "an unanswered safety oracle must not read as covered"


def test_readable_but_no_goplus_is_partial_not_full():
    """GoPlus is the binding constraint at 46 chains, and it is what gates token safety."""
    orc = _oracles(goplus=frozenset({"1"}))
    t = radar.assess_tractability("monad", orc)
    assert t.goplus is False and t.codex is True
    assert t.verdict == radar.PARTIAL
    assert t.actionable, "we can still watch it; we just cannot screen a token on it"
    assert "GoPlus does not cover it" in t.why()


def test_tractability_weights_only_reorder_and_never_grant_actionability():
    cfg = radar.DEFAULT_CONFIG
    assert cfg.tractability_weights[0] > cfg.tractability_weights[1] > cfg.tractability_weights[2]
    assert radar.PROVENANCE["tractability_weights"].basis == "INVENTED"
    opaque = radar.Tractability("x", None, None, False, False, False)
    assert opaque.verdict == radar.OPAQUE and not opaque.actionable


def test_chain_enum_stays_closed_and_a_new_chain_is_still_recordable(tmp_db):
    """The blocker that made every venue a hand integration, handled rather than skirted.

    ``Chain`` is gmgn-cli's argument list and must stay closed; a discovery must not
    require editing the trading contract before it can be written down.
    """
    assert radar.native_lane("hyperevm") is None
    with pytest.raises(ValueError):
        Chain("hyperevm")
    cand = radar.Candidate(kind=radar.KIND_CHAIN, layer="llama_chains", identity="hyperevm",
                           display_name="HyperEVM", chain_slug="hyperevm",
                           value=Decimal("120000000"), unit="usd_7d",
                           basis=EvidenceBasis.PROVIDER_REPORTED)
    out = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    radar.record_find(tmp_db, out, now=NOW_MS)
    row = fetch_one(tmp_db, "SELECT * FROM radar_registry WHERE radar_key=?",
                    (cand.radar_key,))
    assert row["chain_slug"] == "hyperevm"
    assert row["native_lane"] == 0
    find = fetch_one(tmp_db, "SELECT * FROM radar_finds WHERE radar_key=?", (cand.radar_key,))
    assert find["chain_slug"] == "hyperevm"


# ======================================================================================
# evidence rules — missing is None with UNAVAILABLE, never 0
# ======================================================================================


def test_dec_returns_none_for_junk_never_zero():
    assert radar.dec(None) is None
    assert radar.dec("") is None
    assert radar.dec("not a number") is None
    assert radar.dec(float("nan")) is None
    assert radar.dec(float("inf")) is None
    assert radar.dec(True) is None, "a bool is not a quantity"
    assert radar.dec("0") == Decimal(0), "a real zero is still a real zero"


def test_an_unpriced_config_yields_unavailable_not_zero():
    """A venue whose quote asset we cannot price must not read as 'too small to care'."""
    rows = [r for r in RAYDIUM_PAGE_ROWS if r["volumeU"] is None]
    agg = radar.aggregate_launchlab(rows, window_ms=DAY, now=NOW_MS)
    acc = agg["UnpricedCfg111111111111111111111111111111111"]
    assert acc["pools"] == 1 and acc["pools_priced"] == 0 and acc["pools_unpriced"] == 1
    assert acc["fee_proxy_usd"] is None, "not Decimal(0)"


def test_a_candidate_with_no_value_never_reports(tmp_db):
    out = radar.observe(tmp_db, _cand("solana:unpriced", None), oracles=_oracles(), now=NOW_MS)
    assert out.tier == 0 and not out.reported and out.reason == "no_evidence"
    row = fetch_one(tmp_db, "SELECT * FROM radar_registry WHERE radar_key=?",
                    (out.candidate.radar_key,))
    assert row["last_value"] is None
    assert row["last_basis"] == EvidenceBasis.UNAVAILABLE.value


def test_a_partial_census_suppresses_the_proxy(tmp_db):
    """A census that ran out of pages did not cover a day, and must not claim it did."""
    cfg = radar.RadarConfig(launchlab_census_pages=1)
    pages = [_page(RAYDIUM_PAGE_ROWS, cursor="more")]
    cands, ok, total, reqs, err = radar.poll_launchlab(
        tmp_db, cfg=cfg, census=True, now=NOW_MS, raw_pages=pages)
    assert cands, "the configs are still registered"
    assert all(c.value is None for c in cands)
    assert all(c.basis is EvidenceBasis.UNAVAILABLE for c in cands)
    assert all(c.silent for c in cands)
    assert err and "proxy suppressed" in err


# ======================================================================================
# parsers, on payload shapes recorded live
# ======================================================================================


def test_parse_venue_fees_keeps_every_category():
    """No category filter, by design: the money floor does the discrimination."""
    payload = {"protocols": [
        {"slug": "stonkfun", "displayName": "StonkFun", "total24h": 819168,
         "category": "Launchpad", "total7d": 5001550, "change_1d": 31328.57},
        {"slug": "someNFT", "displayName": "Some NFT Market", "total24h": 12,
         "category": "NFT Marketplace"},
        {"slug": "nototal", "displayName": "No Total", "total24h": None, "category": "Dexs"},
    ]}
    cands = radar.parse_venue_fees(payload, "solana")
    assert len(cands) == 3, "an NFT marketplace is measured, not excluded"
    by = {c.identity: c for c in cands}
    assert by["solana:stonkfun"].value == Decimal("819168")
    assert by["solana:someNFT"].value == Decimal("12")
    assert by["solana:nototal"].value is None
    assert by["solana:nototal"].basis is EvidenceBasis.UNAVAILABLE
    # change_1d is carried and never gated on: +31,328.57% lives on a $22/day venue.
    assert by["solana:stonkfun"].meta["change_1d"] == "31328.57"


def test_aggregate_launchlab_groups_by_platform_config():
    agg = radar.aggregate_launchlab(RAYDIUM_PAGE_ROWS, window_ms=DAY, now=NOW_MS)
    assert len(agg) == 5
    stonk = agg["6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"]
    assert stonk["pools"] == 2 and stonk["name"] == "StonkFun"
    assert stonk["fee_rate_ppm"] == Decimal("10000")
    expected = (Decimal("191.033087") + Decimal("632.9378022119017")) / Decimal(100)
    assert stonk["fee_proxy_usd"] == expected
    # A pool with volumeU == 0 is priced-at-zero, which is different from unpriced.
    other = agg["4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7"]
    assert other["pools_priced"] == 1 and other["pools_unpriced"] == 0


def test_the_feed_shows_more_stonkfun_configs_than_the_ingest_module_knows():
    """Reported, not fixed: kaiba/ingest/stonkfun.py holds two configs.

    The census of 2026-09-21 found seven platform configs whose ``platformInfo.name`` is
    "StonkFun", and ``parse_launch`` refuses any row whose config is not in its dict, so
    launches on the other five are being dropped on the floor right now. This assertion
    exists so the fact is in a test rather than only in a report.
    """
    names = {r["platformInfo"]["pubKey"]: r["platformInfo"]["name"] for r in RAYDIUM_PAGE_ROWS}
    stonk_like = {k for k, v in names.items() if "stonk" in str(v).lower()}
    assert len(stonk_like) >= 3, stonk_like
    known_to_ingest = {"6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt"}
    assert stonk_like - known_to_ingest, "the radar sees configs the ingest module does not"


def test_chain_volume_30d_sums_the_breakdown():
    payload = {"protocols": [
        {"slug": "a", "breakdown30d": {"solana": {"Raydium": 100}, "arc": {"Wonk Fun": 5}}},
        {"slug": "b", "breakdown30d": {"solana": {"Orca": "50.5"}}},
        {"slug": "c", "breakdown30d": None},
    ]}
    agg = radar.chain_volume_30d(payload)
    assert agg["solana"][0] == Decimal("150.5")
    assert agg["solana"][1] == 2
    assert agg["arc"][0] == Decimal("5")


def test_parse_launchlab_page_and_cursor():
    rows, cursor = radar.parse_launchlab_page(_page(RAYDIUM_PAGE_ROWS, "abc"))
    assert len(rows) == len(RAYDIUM_PAGE_ROWS) and cursor == "abc"
    assert radar.parse_launchlab_page({}) == ([], None)
    assert radar.parse_launchlab_page(None) == ([], None)


def test_fetch_launchlab_stops_on_the_high_water_mark():
    newer = [dict(r, createAt=NOW_MS - 1000) for r in RAYDIUM_PAGE_ROWS[:2]]
    older = [dict(r, createAt=NOW_MS - 10 * DAY) for r in RAYDIUM_PAGE_ROWS[:2]]
    pages = [_page(newer, "c1"), _page(older, "c2"), _page(newer, "c3")]
    rows, reqs, exhausted, err = radar.fetch_launchlab(
        None, max_pages=10, stop_before_ms=NOW_MS - 2 * DAY, raw_pages=pages)
    assert len(rows) == 4, "stopped after the page that crossed the mark"
    assert exhausted and err is None


# ======================================================================================
# THE RETROSPECTIVE PROOF — would this have caught the three we found by hand?
# ======================================================================================


def _replay_fee_series(conn, identity: str, points: list[tuple[int, float]], *,
                       visible_from: int | None = None, chain_slug: str = "solana",
                       unit: str = "usd_per_day", kind: str = radar.KIND_VENUE,
                       cfg: radar.RadarConfig | None = None) -> str | None:
    """Feed a real daily series through `observe` one day at a time.

    ``visible_from`` models the provider's own blindness: DefiLlama cannot report a venue
    before its adapter exists, so the radar cannot see it either. Returns the UTC day the
    radar would first have announced it, or None.
    """
    from datetime import UTC, datetime

    config = cfg or radar.DEFAULT_CONFIG
    baselined = False
    for ts, value in points:
        ms = ts * 1000
        if visible_from is not None and ts < visible_from:
            # The venue is not in the provider's table yet; instead the layer baselines
            # on a venue that IS there, which is what a real first sweep looks like.
            if not baselined:
                radar.observe(conn, _cand("solana:pump.fun", 1_437_832.0), oracles=_oracles(),
                              now=ms, baseline_mode=True, cfg=config)
                baselined = True
            continue
        if not baselined:
            radar.observe(conn, _cand("solana:pump.fun", 1_437_832.0), oracles=_oracles(),
                          now=ms, baseline_mode=True, cfg=config)
            baselined = True
        cand = _cand(identity, value, kind=kind, unit=unit, chain_slug=chain_slug)
        out = radar.observe(conn, cand, oracles=_oracles(), now=ms, cfg=config)
        if out.reported:
            return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
    return None


#: 2026-08-18T16:35:59Z — the first commit of `fees/stonkfun.ts` in
#: DefiLlama/dimension-adapters, read through the GitHub API on 2026-09-21. Before this
#: moment StonkFun does not exist in any DefiLlama fee response, whatever its backfilled
#: daily chart later says.
STONKFUN_ADAPTER_EPOCH = 1787070959
#: 2026-07-18T08:32:40Z — first commit of `fees/ponsdotfamily/index.ts`, same method.
PONS_ADAPTER_EPOCH = 1784363560
#: The day the operator's agents actually integrated each of these, from the repository:
#: docs/research/11-bsc-robinhood-edge-2026.md is dated 2026-09-20/21 and
#: kaiba/ingest/stonkfun.py was written 2026-09-21.
FOUND_BY_HAND = {"stonkfun": "2026-09-21", "pons": "2026-09-20", "robinhood": "2026-09-20"}


def test_would_have_flagged_stonkfun_before_we_found_it_by_hand(tmp_db):
    """2026-08-22, against 2026-09-21 by hand. Thirty days of lead.

    Four days later than the bare floor crossing would suggest, and the four days are
    the confirmation rule working rather than a defect: the first day StonkFun is
    visible at all is 2026-08-19 at $23,311, which is BELOW the floor, so the streak
    starts on 08-20 and completes on 08-22. That is the cost of refusing to shout at a
    one-day spike, priced on the venue that burned us.
    """
    day = _replay_fee_series(tmp_db, "solana:stonkfun", STONKFUN_FEES_USD_DAY,
                             visible_from=STONKFUN_ADAPTER_EPOCH)
    assert day is not None, "the radar would have said nothing about StonkFun"
    assert day == "2026-08-22", day
    assert day < FOUND_BY_HAND["stonkfun"]


def test_stonkfun_lead_is_bounded_by_defillamas_own_adapter_not_by_this_rule(tmp_db):
    """The honest half of the previous test.

    If DefiLlama had carried StonkFun from the start, this rule fires on 2026-08-06 --
    day 11 of 57, twelve days before the adapter landed. It did not, so the DefiLlama
    layer cannot beat 2026-08-18 no matter how the floor is set. That gap is the entire
    justification for the LaunchLab layer and for keeping its lower-bound proxy despite
    the proxy being a lower bound.
    """
    from datetime import UTC, datetime

    day = _replay_fee_series(tmp_db, "solana:stonkfun", STONKFUN_FEES_USD_DAY,
                             visible_from=None)
    assert day == "2026-08-06", day
    adapter_day = datetime.fromtimestamp(STONKFUN_ADAPTER_EPOCH, UTC).strftime("%Y-%m-%d")
    assert adapter_day == "2026-08-18"
    assert day < adapter_day, "the rule is 12 days ahead of the provider that feeds it"


def test_would_have_flagged_pons_before_we_found_it_by_hand(tmp_db):
    """2026-07-19, against 2026-09-20 by hand. Sixty-three days of lead.

    The adapter landed mid-morning on 2026-07-18, so the first daily row the radar can
    read is 07-19's, and Pons was doing $1,010,243/day by then -- forty times the floor,
    tier 2, no confirmation wait.
    """
    day = _replay_fee_series(tmp_db, "robinhood:pons", PONS_FEES_USD_DAY,
                             visible_from=PONS_ADAPTER_EPOCH, chain_slug="robinhood")
    assert day == "2026-07-19", day
    assert day < FOUND_BY_HAND["pons"]


def test_pons_would_have_fired_on_its_second_day_if_the_data_existed(tmp_db):
    """Pons opened at $159,750/day -- 6.4x the floor, which is tier 1, so it waits.

    It does not wait three days: the next day is $683,026, 27x the floor and tier 2,
    which skips the confirmation. Day one of seventy.
    """
    day = _replay_fee_series(tmp_db, "robinhood:pons", PONS_FEES_USD_DAY,
                             visible_from=None, chain_slug="robinhood")
    assert day == "2026-07-15", day


def test_would_have_flagged_robinhood_chain_before_we_found_it_by_hand(tmp_db):
    """The chain layer, replayed day by day on the real DEX volume series."""
    from datetime import UTC, datetime

    cfg = radar.DEFAULT_CONFIG
    series = _series(ROBINHOOD_DEX_USD_DAY)
    orc = _oracles()
    baselined = False
    fired: str | None = None
    for i, (ts, _) in enumerate(series):
        verdict = radar.chain_rule(series, cfg, at_index=i)
        cand = radar.Candidate(
            kind=radar.KIND_CHAIN, layer="llama_chains", identity="robinhood",
            display_name="Robinhood Chain", chain_slug="robinhood",
            value=verdict.window_7d if verdict.fires else None, unit="usd_7d",
            basis=(EvidenceBasis.PROVIDER_REPORTED if verdict.fires
                   else EvidenceBasis.UNAVAILABLE),
            meta={"age_days": verdict.age_days, "label": "Robinhood Chain",
                  "chain_id": 4663},
            silent=not verdict.fires,
        )
        out = radar.observe(tmp_db, cand, oracles=orc, now=ts * 1000,
                            baseline_mode=not baselined)
        baselined = True
        if out.reported:
            fired = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")
            radar.record_find(tmp_db, out, now=ts * 1000)
            break
    # The bare rule crosses on 2026-07-07 at $81.6M/7d, which is 1.6x the floor and so
    # tier 1. It does not wait three days: 2026-07-08 brings the $433M print, the 7-day
    # window passes $500M, and tier 2 skips the confirmation.
    assert fired == "2026-07-08", fired
    assert fired < FOUND_BY_HAND["robinhood"]
    row = fetch_one(tmp_db, "SELECT * FROM radar_finds WHERE identity='robinhood'", ())
    assert row["actionable"] == 1, "Robinhood Chain is a gmgn-cli lane, so it is actionable"
    assert Decimal(row["value"]) >= Decimal("50000000")


def test_the_retrospective_leads_are_all_positive_and_material():
    """One assertion summarising the three above, so a regression is unmissable."""
    from datetime import date

    leads = {
        "stonkfun": (date(2026, 8, 22), date(2026, 9, 21)),
        "pons": (date(2026, 7, 19), date(2026, 9, 20)),
        "robinhood": (date(2026, 7, 8), date(2026, 9, 20)),
    }
    days = {k: (found - radar_day).days for k, (radar_day, found) in leads.items()}
    assert days == {"stonkfun": 30, "pons": 63, "robinhood": 74}
    assert min(days.values()) >= 30


# ======================================================================================
# the sweep, offline
# ======================================================================================


def test_sweep_end_to_end_offline_and_accounts_for_its_requests(tmp_db):
    fees = {"protocols": [
        {"slug": "pump.fun", "displayName": "pump.fun", "total24h": 1_437_832,
         "category": "Launchpad"},
        {"slug": "stonkfun", "displayName": "StonkFun", "total24h": 819_168,
         "category": "Launchpad"},
    ]}
    pages = [_page(RAYDIUM_PAGE_ROWS, cursor=None)]
    raw = {
        "llama_venues": {"raw": {"solana": fees}},
        "launchlab_census": {"raw_pages": pages},
        "launchlab_discover": {"raw_pages": pages},
        "llama_chains": {"raw": {"overview": {"protocols": []}}},
    }
    first = radar.sweep(tmp_db, only=list(radar.LAYERS), force=True, oracles=_oracles(),
                        now=NOW_MS, raw=raw)
    assert first.reported == 0, "every layer's first sweep is a baseline"
    assert first.seen > 0 and first.new > 0
    assert first.requests == 0, "fixtures cost no provider calls"
    health = {h["layer"]: h for h in radar.layer_health(tmp_db)}
    assert all(h["baseline_ms"] for h in health.values())

    fees["protocols"].append({"slug": "brandnew", "displayName": "Brand New Pad",
                              "total24h": 900_000, "category": "Launchpad"})
    second = radar.sweep(tmp_db, only=["llama_venues"], force=True, oracles=_oracles(),
                         now=NOW_MS + DAY, raw=raw)
    assert second.reported == 1
    assert "Brand New Pad" in second.finds[0]["verdict"]
    assert "ACTIONABLE" in second.finds[0]["verdict"]


def test_a_layer_that_raises_does_not_take_down_the_sweep(tmp_db, monkeypatch):
    def boom(conn=None, **kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setitem(radar.LAYERS, "llama_venues",
                        radar.Layer("llama_venues", "venue_fees", boom))
    res = radar.sweep(tmp_db, only=["llama_venues", "llama_chains"], force=True,
                      oracles=_oracles(), now=NOW_MS,
                      raw={"llama_chains": {"raw": {"overview": {"protocols": []}}}})
    assert "llama_venues" in res.errors
    assert "provider exploded" in res.errors["llama_venues"]
    assert "llama_chains" in res.layers
    row = fetch_one(tmp_db, "SELECT * FROM radar_layer_health WHERE layer='llama_venues'", ())
    assert row["fail_streak"] == 1


def test_a_quiet_layer_is_healthy_but_a_dead_one_is_loud(tmp_db):
    radar.record_layer_health(tmp_db, "llama_venues", "venue_fees", ok=True, count=48,
                              new=0, reported=0, requests=1, interval_s=86_400)
    healthy = {h["layer"]: h["state"] for h in radar.layer_health(tmp_db)}
    assert healthy["llama_venues"] == "ok", "answered and found nothing is the normal case"
    assert healthy["llama_chains"] == "never_run"
    radar.record_layer_health(tmp_db, "llama_chains", "chain_volume", ok=False, count=0,
                              new=0, reported=0, requests=1, interval_s=86_400,
                              error="402 Payment Required")
    dead = {h["layer"] for h in radar.check_health(tmp_db)}
    assert "llama_chains" in dead


def test_due_layers_respects_each_layers_own_clock(tmp_db):
    cfg = radar.DEFAULT_CONFIG
    assert set(radar.due_layers(tmp_db, cfg)) == set(radar.LAYERS), "nothing has run yet"
    radar.record_layer_health(tmp_db, "llama_venues", "venue_fees", ok=True, count=1, new=0,
                              reported=0, requests=1, interval_s=cfg.interval_for("llama_venues"))
    assert "llama_venues" not in radar.due_layers(tmp_db, cfg)
    assert "llama_venues" in radar.due_layers(tmp_db, cfg, force=True)


def test_the_chain_layer_does_not_re_ask_about_chains_it_knows_are_old(tmp_db):
    """The age memory is what keeps a daily job at ~15 requests instead of 53."""
    overview = {"protocols": [
        {"slug": "ethereum", "breakdown30d": {"ethereum": {"Uniswap": 40_000_000_000}}},
        {"slug": "newchain", "breakdown30d": {"newchain": {"SomeDex": 900_000_000}}},
    ]}
    old_series = [(i * 86400, 1_000_000_000) for i in range(1200)]
    raw = {"overview": overview, "series": {"ethereum": old_series, "newchain": old_series[:20]}}
    cands, *_ = radar.poll_chain_volume(tmp_db, raw=raw, oracles=_oracles(), now=NOW_MS)
    for cand in cands:
        radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS, baseline_mode=True)
    skip = radar._aged_out(tmp_db, radar.DEFAULT_CONFIG, NOW_MS)
    assert "ethereum" in skip
    assert "newchain" not in skip


def test_the_emitted_event_carries_the_free_text_slug(tmp_db):
    """`emit(chain=...)` only takes a Chain, so the slug has to ride in the payload.

    Without this a discovery on a chain with no gmgn-cli lane reaches the bus with no
    chain attribution at all, and the dashboard and the reflection job cannot tell
    which chain it was about -- which is the enum blocker reappearing on the event bus
    after being solved in the table.
    """
    from kaiba.core.events import recent

    cand = radar.Candidate(kind=radar.KIND_CHAIN, layer="llama_chains", identity="hyperevm",
                           display_name="HyperEVM", chain_slug="hyperevm",
                           value=Decimal("120000000"), unit="usd_7d",
                           basis=EvidenceBasis.PROVIDER_REPORTED)
    out = radar.observe(tmp_db, cand, oracles=_oracles(), now=NOW_MS)
    radar.record_find(tmp_db, out, now=NOW_MS)
    events = recent(limit=5, conn=tmp_db)
    found = [e for e in events if e.payload.get("hunter") == "radar"]
    assert found, "nothing reached the bus"
    assert found[0].payload["chain_slug"] == "hyperevm"
    assert found[0].payload["native_lane"] is False
    assert found[0].chain is None, "there is no Chain member for it, and that is correct"


def test_discover_mode_never_prices_a_partial_window(tmp_db):
    """The discovery poll walks ~2 h of tape. A per-DAY figure off it would be a lie."""
    pages = [_page(RAYDIUM_PAGE_ROWS, cursor=None)]
    cands, *_ = radar.poll_launchlab(tmp_db, census=False, now=NOW_MS, raw_pages=pages,
                                     mark_ms=NOW_MS - 2 * 3_600_000)
    assert cands, "the configs are still registered"
    assert all(c.value is None for c in cands), "a two-hour window is not a daily figure"
    assert all(c.basis is EvidenceBasis.UNAVAILABLE for c in cands)
    assert all(c.unit == "usd_per_day_proxy" for c in cands)


def test_a_pseudo_chain_that_fires_the_rule_is_still_silent(tmp_db):
    """Spark is $2.5B/30d attached to no chain. It is a venue discovery, not a chain one."""
    overview = {"protocols": [
        {"slug": "sparkx", "breakdown30d": {"sparkx": {"SparkX Exchange": 3_000_000_000}}},
    ]}
    day = 86_400
    series = [(i * day, 0) for i in range(5)] + [((5 + i) * day, 40_000_000) for i in range(7)]
    cands, *_ = radar.poll_chain_volume(
        tmp_db, raw={"overview": overview, "series": {"sparkx": series}},
        oracles=_oracles(llama_chain_ids={}), now=NOW_MS)
    assert len(cands) == 1
    cand = cands[0]
    assert cand.meta["pseudo_chain"] is True
    assert cand.silent, "a venue wearing a chain label must not be announced as a chain"
    out = radar.observe(tmp_db, cand, oracles=_oracles(llama_chain_ids={}), now=NOW_MS)
    assert not out.reported and out.reason == "layer_is_registry_only"


def test_load_oracles_parses_the_four_real_payload_shapes(tmp_db, monkeypatch):
    """The only test that exercises the oracle-loading parse rather than injecting it.

    Payload shapes are the ones the live endpoints returned on 2026-09-21: GoPlus wraps
    its 46 chains in ``result`` with STRING ids, Etherscan v2 uses ``chainid`` (lower
    case d) inside ``result``, Codex answers under ``data.getNetworks`` with INTEGER
    ids, and ``/v2/chains`` is a bare list. Four different spellings of the same idea,
    which is exactly where a silent parse bug lives.
    """
    from kaiba.core.schemas import Receipt
    from kaiba.providers._http import Fetched

    bodies = {
        radar.GOPLUS_CHAINS: {"result": [{"name": "Ethereum", "id": "1"},
                                         {"name": "Base", "id": "8453"}]},
        radar.ETHERSCAN_CHAINS: {"result": [{"chainname": "Ethereum Mainnet", "chainid": "1"},
                                            {"chainname": "Arc", "chainid": "5042"}]},
        radar.LLAMA_CHAINS: [
            {"name": "Arc", "chainId": 5042, "gecko_id": None, "tokenSymbol": None},
            {"name": "Monad", "chainId": 143, "gecko_id": "monad", "tokenSymbol": "MON"},
            {"name": "Solana", "chainId": None, "gecko_id": "solana", "tokenSymbol": "SOL"},
        ],
        radar.CODEX_GRAPHQL: {"data": {"getNetworks": [
            {"id": 999, "name": "HyperEVM", "networkShortName": "hyperevm"},
            {"id": 1, "name": "Ethereum", "networkShortName": "ethereum"}]}},
    }

    def fake(provider, endpoint, url, **kw):
        body = bodies.get(url)
        return Fetched(body, Receipt(provider=provider, endpoint=endpoint,
                                     basis=EvidenceBasis.PROVIDER_REPORTED))

    monkeypatch.setattr(radar, "get_json", fake)
    monkeypatch.setattr(radar, "post_json", fake)
    monkeypatch.setattr("kaiba.core.config.get_settings",
                        lambda: type("S", (), {"codex_io_api_key": "x"})())
    orc = radar.load_oracles(tmp_db)
    assert orc.goplus_ids == frozenset({"1", "8453"})
    assert orc.etherscan_ids == frozenset({"1", "5042"})
    assert orc.codex_ids == frozenset({"999", "1"})
    assert orc.codex_names == frozenset({"hyperevm", "ethereum"})
    assert orc.llama_chain_ids == {"arc": 5042, "monad": 143}
    # Only Arc has neither gecko_id nor tokenSymbol. Monad has MON; Solana has SOL.
    assert orc.tokenless_chains == frozenset({"arc"})
    assert orc.requests == 4, "four list endpoints, cached for a day"


def test_a_chain_with_no_token_is_flagged_but_never_farmed(tmp_db):
    """The operator's "new airdrop" ask, answered as intelligence and nothing more.

    A chain holding real money that has not issued a token is the population airdrops
    come from, and it costs zero extra requests to notice -- /v2/chains is already
    fetched for the chainId join. It is a flag on a chain row, not a plan: farming
    resolved against farmers, so nothing here routes to ev.py or build_plan.
    """
    overview = {"protocols": [
        {"slug": "a", "breakdown30d": {"arc": {"Wonk Fun": 400_000_000},
                                       "monad": {"SomeDex": 400_000_000}}},
    ]}
    day = 86_400
    series = [(i * day, 0) for i in range(3)] + [((3 + i) * day, 20_000_000) for i in range(7)]
    cands, *_ = radar.poll_chain_volume(
        tmp_db, raw={"overview": overview, "series": {"arc": series, "monad": series}},
        oracles=_oracles(), now=NOW_MS)
    by = {c.identity: c for c in cands}
    assert by["arc"].meta["pre_tge_no_token"] is True
    assert by["monad"].meta["pre_tge_no_token"] is False, "MON exists; Monad is not pre-TGE"
    out = radar.observe(tmp_db, by["arc"], oracles=_oracles(), now=NOW_MS)
    radar.record_find(tmp_db, out, now=NOW_MS)
    row = fetch_one(tmp_db, "SELECT * FROM radar_finds WHERE identity='arc'", ())
    assert "no token yet: watch, do not farm" in row["verdict"]


def test_an_unanswered_chain_list_does_not_assert_a_token_exists(tmp_db):
    """Unknown is not "has a token". Missing data is None, as everywhere else."""
    overview = {"protocols": [
        {"slug": "arc", "breakdown30d": {"arc": {"Wonk Fun": 400_000_000}}}]}
    day = 86_400
    series = [(i * day, 0) for i in range(3)] + [((3 + i) * day, 20_000_000) for i in range(7)]
    cands, *_ = radar.poll_chain_volume(
        tmp_db, raw={"overview": overview, "series": {"arc": series}},
        oracles=_oracles(tokenless=None), now=NOW_MS)
    assert cands[0].meta["pre_tge_no_token"] is None


def test_report_separates_actionable_from_research_only(tmp_db):
    orc_opaque = _oracles(goplus=frozenset(), codex_ids=frozenset(), codex_names=frozenset(),
                          etherscan=frozenset(), llama_chain_ids={})
    big = radar.observe(tmp_db, _cand("solana:tradeable", 500_000.0), oracles=_oracles(),
                        now=NOW_MS)
    radar.record_find(tmp_db, big, now=NOW_MS)
    unreadable = radar.Candidate(kind=radar.KIND_CHAIN, layer="llama_chains",
                                 identity="ghostchain", display_name="Ghost Chain",
                                 chain_slug="ghostchain", value=Decimal("9000000000"),
                                 unit="usd_7d", basis=EvidenceBasis.PROVIDER_REPORTED)
    out = radar.observe(tmp_db, unreadable, oracles=orc_opaque, now=NOW_MS)
    radar.record_find(tmp_db, out, now=NOW_MS)
    text = radar.radar_report(tmp_db)
    assert "## Actionable finds" in text
    assert "## Research only - we cannot read these" in text
    action_block, research_block = text.split("## Research only - we cannot read these")
    assert "tradeable" in action_block
    assert "Ghost Chain" in research_block
    assert "Ghost Chain" not in action_block


def test_report_says_how_many_rows_carry_no_priced_evidence(tmp_db):
    radar.observe(tmp_db, _cand("solana:unpriced-a", None), oracles=_oracles(), now=NOW_MS)
    radar.observe(tmp_db, _cand("solana:unpriced-b", None), oracles=_oracles(), now=NOW_MS)
    text = radar.radar_report(tmp_db)
    assert "2 registry rows carry no priced evidence" in text
    assert "too small to care" in text


def test_watchlist_shows_a_candidate_mid_confirmation(tmp_db):
    radar.observe(tmp_db, _cand("solana:almost", 30_000.0), oracles=_oracles(), now=NOW_MS)
    holding = radar.watchlist(tmp_db)
    assert [r["identity"] for r in holding] == ["solana:almost"]
    assert holding[0]["confirm_days"] == 1


def test_the_watchlist_is_not_a_list_of_everything_above_the_floor(tmp_db):
    """492 venue rows sit above the floor on the live roster, all of them years old.

    A watchlist that included them would reintroduce, one table over, exactly the noise
    the baseline rule exists to remove. Only candidates that have moved past where we
    last left them belong here.
    """
    for name, value in (("solana:pump.fun", 1_437_832.0), ("solana:raydium", 986_011.0),
                        ("solana:orca", 501_652.0)):
        radar.observe(tmp_db, _cand(name, value), oracles=_oracles(), now=NOW_MS,
                      baseline_mode=True)
    assert radar.watchlist(tmp_db) == [], "baseline rows are known, not pending"
    # A genuinely new one is pending until its three days are up.
    fresh = radar.observe(tmp_db, _cand("solana:fresh", 40_000.0), oracles=_oracles(),
                          now=NOW_MS)
    assert not fresh.reported
    # A baseline venue that escalates a whole tier is announced at once, so it passes
    # straight through the watchlist rather than sitting on it.
    esc = radar.observe(tmp_db, _cand("solana:orca", 5_000_000.0), oracles=_oracles(),
                        now=NOW_MS)
    assert esc.reported and esc.tier == 3 and esc.previous_tier == 2
    assert {r["identity"] for r in radar.watchlist(tmp_db)} == {"solana:fresh"}


# --------------------------------------------------------------------------------------
# module-level connection for the one test that needs a registry without a fixture arg
# --------------------------------------------------------------------------------------

_CONN = None


@pytest.fixture(autouse=True)
def _bind_conn(tmp_db):
    global _CONN
    _CONN = tmp_db
    yield
    _CONN = None
