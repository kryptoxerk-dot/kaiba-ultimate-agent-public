"""confluence-5 counting PROVEN wallets instead of graded ones.

The grade route is untouched and still covered by ``tests/test_lanes.py``. What has to
hold for the proven route:

* only members of the newest frozen proven cohort count -- a grade is not membership;
* a missing, stale, EMPTY, or not-yet-frozen-at-replay-time cohort means silence;
* strength is FLAT (owner directive: confidence measures are anti-calibrated) and high
  enough to clear the sizer's ladder, while the confluence level rides in the payload;
* the entity collapse and the source vetoes still apply.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane
from kaiba.execution import lanes
from kaiba.learning import proven as P
from tests.test_lanes import NOW, build_ctx, load_fixture

A = "wa11etAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"  # grade A, $420, 40 s ago
B = "wa11etBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB2"  # grade B, $260, 32 s ago
C = "wa11etCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC3"  # grade B, $150, 21 s ago
F = "wa11etFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF6"  # grade C, $600, 9 s ago
G = "wa11etGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG7"  # grade A, $12 dust, 8 s ago

PROVEN = {"wallet_source": "proven", "min_entities": 3, "max_signal_age_s": 60, "min_buy_usd": 50}


@pytest.fixture(autouse=True)
def _fresh_cache():
    P.clear_cache()
    yield
    P.clear_cache()


def _freeze(conn, members, *, at_ms: int = NOW - 3_600_000, chain: Chain = Chain.SOL) -> str:
    report = P.ProvenReport(chain=chain, as_of_ms=at_ms, config=P.ProvenConfig(), split_ms=at_ms - 86_400_000)
    report.proven = [P.WalletEvidence(wallet=w, source="test") for w in members]
    cohort_id = P.freeze(conn, report)
    P.clear_cache()
    return cohort_id


def _ctx(conn, params=None, **kw):
    return build_ctx(conn, load_fixture("confluence_5"), params={**PROVEN, **(params or {})}, **kw)


def test_proven_wallets_count_and_grades_do_not(tmp_db):
    """F is grade C and counts because it is proven; C and D are grade B and do not."""
    cohort_id = _freeze(tmp_db, [A, B, F, G])
    signal = lanes.confluence_5(_ctx(tmp_db))
    assert signal is not None
    assert signal.wallets == sorted([A, B, F])  # G is proven but a $12 dust buy
    assert signal.payload["wallet_source"] == "proven"
    assert signal.payload["confluence_level"] == 3
    assert signal.payload["proven_wallets"] == 3
    assert signal.payload["cohort_id"] == cohort_id
    assert signal.payload["cohort_size"] == 4


def test_one_proven_wallet_short_is_silent(tmp_db):
    _freeze(tmp_db, [A, B, F])
    assert lanes.confluence_5(_ctx(tmp_db, {"min_entities": 4})) is None


def test_graded_wallets_outside_the_cohort_never_make_up_the_number(tmp_db):
    """Five A/B wallets are buying; only two are proven. Under 'grade' this fires."""
    _freeze(tmp_db, [A, B])
    assert lanes.confluence_5(_ctx(tmp_db)) is None
    graded = build_ctx(tmp_db, load_fixture("confluence_5"), params={"wallet_source": "grade"})
    assert lanes.confluence_5(graded) is not None


def test_no_cohort_means_silence(tmp_db):
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_an_empty_cohort_means_silence(tmp_db):
    _freeze(tmp_db, [])
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_a_stale_cohort_means_silence(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW - 4 * 86_400_000)
    assert lanes.confluence_5(_ctx(tmp_db)) is None
    assert lanes.confluence_5(_ctx(tmp_db, {"proven_max_cohort_age_s": 5 * 86_400})) is not None


def test_a_replay_never_counts_a_cohort_frozen_after_it(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW + 60_000)
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_the_newest_cohort_replaces_the_old_one(tmp_db):
    _freeze(tmp_db, [A, B, F], at_ms=NOW - 7_200_000)
    _freeze(tmp_db, [A], at_ms=NOW - 3_600_000)
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_strength_is_flat_and_clears_the_sizers_ladder(tmp_db):
    from kaiba.execution.risk import score_fraction, score_from_strength

    _freeze(tmp_db, [A, B, F])
    base = lanes.confluence_5(_ctx(tmp_db))
    _freeze(tmp_db, [A, B, C, F], at_ms=NOW - 1_800_000)
    richer = lanes.confluence_5(_ctx(tmp_db))
    assert richer.payload["confluence_level"] == 4 > base.payload["confluence_level"] == 3
    assert richer.strength == base.strength == 0.75  # level is recorded, never sized
    assert score_fraction(score_from_strength(base.strength)) > 0


def test_three_addresses_one_entity_do_not_fire(tmp_db, monkeypatch):
    _freeze(tmp_db, [A, B, F])
    monkeypatch.setattr(lanes, "independent_entity_count", lambda chain, addrs, conn=None: 1)
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_a_blacklisted_proven_wallet_does_not_count(tmp_db):
    _freeze(tmp_db, [A, B, F])
    tmp_db.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, source, tags_json, first_seen_ms, last_seen_ms, "
        "cohort, meta_json) VALUES ('sol', ?, 'test', '[]', 0, 0, 'blacklist', '{}')",
        (F,),
    )
    assert lanes.confluence_5(_ctx(tmp_db)) is None


def test_the_age_gate_still_applies(tmp_db):
    _freeze(tmp_db, [A, B, F])
    assert lanes.confluence_5(_ctx(tmp_db, {"max_signal_age_s": 9})) is not None  # F is 9 s old
    assert lanes.confluence_5(_ctx(tmp_db, {"max_signal_age_s": 8})) is None


def test_params_take_a_per_chain_mapping_with_a_default(tmp_db):
    _freeze(tmp_db, [A, B, F])
    fires = {"min_entities": {"sol": 3, "default": 9}, "max_signal_age_s": {"robinhood": 1, "default": 60}}
    assert lanes.confluence_5(_ctx(tmp_db, fires)) is not None
    silent = {"min_entities": {"robinhood": 3, "default": 4}}
    assert lanes.confluence_5(_ctx(tmp_db, silent)) is None


def test_an_unknown_wallet_source_refuses(tmp_db):
    _freeze(tmp_db, [A, B, F])
    assert lanes.confluence_5(_ctx(tmp_db, {"wallet_source": "vibes"})) is None


def test_the_shipped_default_is_still_the_grade_route():
    assert lanes.DEFAULT_PARAMS[Lane.CONFLUENCE_5]["wallet_source"] == "grade"


def test_one_co_timed_operator_counts_once(tmp_db):
    """Three proven addresses that the cohort's co-timing fingerprint calls one operator
    are one opinion, even with no funding link in the entity graph."""
    report = P.ProvenReport(chain=Chain.SOL, as_of_ms=NOW - 3_600_000, config=P.ProvenConfig(),
                            split_ms=NOW - 90_000_000)
    report.proven = [P.WalletEvidence(wallet=w, source="test", cluster=A) for w in (A, B, F)]
    P.freeze(tmp_db, report)
    P.clear_cache()
    assert lanes.confluence_5(_ctx(tmp_db)) is None
    signal = lanes.confluence_5(_ctx(tmp_db, {"min_entities": 1}))
    assert signal.payload["cotime_clusters"] == 1 and signal.payload["confluence_level"] == 1
