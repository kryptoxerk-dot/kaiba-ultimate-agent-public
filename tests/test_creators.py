"""Tests for creator-history backfill.

Offline by default. The fixtures under ``tests/fixtures/creators/`` are real pump.fun
frontend-API records captured on 2026-09-20 with the fields this module reads, so the
parser is tested against the shape the venue actually emits rather than one we imagined.

The three things worth protecting here, in order:

1. **Idempotency.** Re-running must correct counts, not double them. A creator backfill
   that doubles is worse than one that does not run, because the number it produces is
   plausible.
2. **No zero rows.** A creator we could not measure must stay absent from the table;
   triage reads absent as "no opinion" and a zero row as a fact.
3. **``rugged`` is never written from a collapse proxy.** The diagnostic exists precisely
   to show why, and a future change that starts storing it should fail here first.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt
from kaiba.intelligence import creators as cr
from kaiba.providers._http import Fetched

FIXTURES = Path(__file__).parent / "fixtures" / "creators"
TEST_CONFIG = cr.BackfillConfig(min_interval_s=0.0, refresh_after_s=0.0, creator_ttl_s=0.0)


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_pacing():
    cr.PACER.reset()
    yield
    cr.PACER.reset()


def _ok(data: Any) -> Fetched:
    return Fetched(data, Receipt(provider=cr.PROVIDER, endpoint="coins.list"))


def _down(note: str = "boom") -> Fetched:
    return Fetched(
        None,
        Receipt(provider=cr.PROVIDER, endpoint="coins.list", basis=EvidenceBasis.UNAVAILABLE, note=note),
    )


class FakeApi:
    """Stands in for ``get_json``: serves an index and per-creator histories from memory."""

    def __init__(
        self,
        index: list[dict[str, Any]] | None = None,
        histories: dict[str, list[dict[str, Any]]] | None = None,
        complete_index: list[dict[str, Any]] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.index = index or []
        self.complete_index = complete_index or []
        self.histories = histories or {}
        self.fail = fail or set()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        params = dict(kw.get("params") or {})
        self.calls.append({"provider": provider, "endpoint": endpoint, "url": url, **params, "kw": kw})
        offset = int(params.get("offset", 0))
        creator = params.get("creator")
        if creator is not None:
            if creator in self.fail:
                return _down(f"history for {creator} unavailable")
            rows = self.histories.get(creator, [])
        elif params.get("complete"):
            rows = self.complete_index
        else:
            if "index" in self.fail:
                return _down("index unavailable")
            rows = self.index
        return _ok(rows[offset : offset + cr.PAGE_LIMIT])


# ------------------------------------------------------------------------------ parsing


def test_parse_coin_reads_the_fields_we_depend_on():
    raw = load("creator_history.json")[0]
    rec = cr.parse_coin(raw)
    assert rec is not None
    assert rec.mint == raw["mint"]
    assert rec.creator == raw["creator"]
    assert rec.created_ms == raw["created_timestamp"]
    assert isinstance(rec.usd_market_cap, Decimal)
    assert isinstance(rec.ath_market_cap_usd, Decimal)


def test_parse_coin_without_a_mint_is_dropped():
    assert cr.parse_coin({"creator": "abc"}) is None
    assert cr.parse_coin({"mint": "", "creator": "abc"}) is None


def test_missing_numbers_are_none_never_zero():
    rec = cr.parse_coin({"mint": "m1", "creator": "c1"})
    assert rec is not None
    assert rec.usd_market_cap is None
    assert rec.ath_market_cap_usd is None
    assert rec.created_ms is None
    # `complete` absent means the venue did not say, which is not "did not graduate".
    assert rec.complete is None
    assert rec.graduated is False


def test_parse_page_accepts_a_bare_list_and_an_envelope():
    rows = load("recent_page.json")
    assert len(cr.parse_page(rows)) == len(rows)
    assert len(cr.parse_page({"coins": rows})) == len(rows)
    assert cr.parse_page(None) == []
    assert cr.parse_page({"coins": "nope"}) == []


def test_graduated_fixture_is_actually_graduated():
    recs = cr.parse_page(load("graduated_page.json"))
    assert recs and all(r.graduated for r in recs)
    assert any(r.ath_market_cap_usd is not None for r in recs)


# ----------------------------------------------------------------------------- fetching


def test_fetch_page_sends_the_measured_page_limit_and_waits_for_a_slot(monkeypatch):
    api = FakeApi(index=load("recent_page.json"))
    monkeypatch.setattr(cr, "get_json", api)
    page = cr.fetch_page(offset=0, config=TEST_CONFIG)
    assert page.ok and len(page) == len(load("recent_page.json"))
    call = api.calls[0]
    assert call["limit"] == cr.PAGE_LIMIT == 70
    # Contract: an adapter that makes more than one call per logical operation must wait
    # for limiter capacity, or it silently loses every page after the first.
    assert call["kw"]["wait_for_slot_s"] > 0


def test_fetch_page_returns_not_ok_rather_than_empty_when_the_provider_is_down(monkeypatch):
    monkeypatch.setattr(cr, "get_json", FakeApi(fail={"index"}))
    page = cr.fetch_page(offset=0, config=TEST_CONFIG)
    assert page.ok is False
    assert len(page) == 0


def test_fetch_index_stops_at_the_measured_offset_cap(monkeypatch):
    api = FakeApi(index=[{"mint": f"m{i}", "creator": "c"} for i in range(5000)])
    monkeypatch.setattr(cr, "get_json", api)
    _recs, ok, failed = cr.fetch_index(pages=200, config=TEST_CONFIG)
    assert failed == 0
    offsets = [c["offset"] for c in api.calls]
    assert max(offsets) <= cr.MAX_OFFSET
    assert ok == len(offsets)


def test_fetch_index_steps_by_page_limit_so_nothing_is_skipped(monkeypatch):
    api = FakeApi(index=[{"mint": f"m{i}", "creator": "c"} for i in range(cr.PAGE_LIMIT * 3)])
    monkeypatch.setattr(cr, "get_json", api)
    recs, ok, _failed = cr.fetch_index(pages=3, config=TEST_CONFIG)
    assert [c["offset"] for c in api.calls] == [0, cr.PAGE_LIMIT, cr.PAGE_LIMIT * 2]
    assert ok == 3
    assert len({r.mint for r in recs}) == cr.PAGE_LIMIT * 3


def test_fetch_index_uses_the_complete_filter_for_the_graduated_index(monkeypatch):
    api = FakeApi(complete_index=load("graduated_page.json"))
    monkeypatch.setattr(cr, "get_json", api)
    recs, _ok, _failed = cr.fetch_index(pages=1, index=cr.INDEX_COMPLETE, config=TEST_CONFIG)
    assert api.calls[0]["complete"] == "true"
    assert recs and all(r.graduated for r in recs)


def test_fetch_creator_history_pages_until_a_short_page(monkeypatch):
    rows = [{"mint": f"m{i}", "creator": "serial", "complete": False} for i in range(cr.PAGE_LIMIT + 5)]
    api = FakeApi(histories={"serial": rows})
    monkeypatch.setattr(cr, "get_json", api)
    got = cr.fetch_creator_history("serial", config=TEST_CONFIG)
    assert got.history is not None
    assert got.history.launches == cr.PAGE_LIMIT + 5
    assert len(got.records) == cr.PAGE_LIMIT + 5
    assert got.pages == 2
    assert [c["offset"] for c in api.calls] == [0, cr.PAGE_LIMIT]


def test_a_partial_history_is_discarded_rather_than_undercounted(monkeypatch):
    monkeypatch.setattr(cr, "get_json", FakeApi(fail={"ghost"}))
    got = cr.fetch_creator_history("ghost", config=TEST_CONFIG)
    assert got.history is None
    assert got.records == ()
    # A provider that did not answer is a different fact from one that answered "nothing".
    assert got.ok is False


def test_an_empty_history_is_distinguished_from_a_dead_endpoint(monkeypatch):
    monkeypatch.setattr(cr, "get_json", FakeApi(histories={}))
    got = cr.fetch_creator_history("quiet", config=TEST_CONFIG)
    assert got.history is None
    assert got.ok is True


# -------------------------------------------------------------------------- aggregation


def test_summarise_creator_counts_lifetime_launches_and_graduates():
    rows = load("creator_history.json")
    recs = cr.parse_page(rows)
    history = cr.summarise_creator(rows[0]["creator"], recs)
    assert history is not None
    assert history.launches == len(rows)
    assert history.graduated == sum(1 for r in rows if r.get("complete"))
    assert 0.0 < (history.graduation_rate or 0.0) <= 1.0
    assert history.median_peak_mcap_usd is not None
    assert history.first_launch_ms is not None and history.last_launch_ms is not None


def test_summarise_creator_of_nothing_is_none_not_a_zero_row():
    assert cr.summarise_creator("nobody", []) is None


def test_unknown_complete_flags_are_counted_not_assumed():
    recs = [cr.LaunchRecord(mint="a"), cr.LaunchRecord(mint="b", complete=True)]
    history = cr.summarise_creator("c", recs)
    assert history is not None
    assert history.complete_unknown == 1
    assert history.graduated == 1


def test_median_peak_is_none_when_no_peak_was_reported():
    history = cr.summarise_creator("c", [cr.LaunchRecord(mint="a"), cr.LaunchRecord(mint="b")])
    assert history is not None
    assert history.median_peak_mcap_usd is None
    assert history.peaks_observed == 0


# ------------------------------------------------------------------------- distributions


def test_launch_distribution_buckets_and_concentration():
    counts = [100] + [1] * 99
    dist = cr.launch_distribution(counts)
    assert dist["creators"] == 100
    assert dist["coins"] == 199
    assert dist["buckets"]["1"] == 99
    assert dist["buckets"]["60+"] == 1
    assert dist["top1pct_creators"] == 1
    assert dist["top1pct_share"] == pytest.approx(100 / 199, abs=1e-4)


def test_launch_distribution_of_nothing_is_not_a_crash():
    assert cr.launch_distribution([])["creators"] == 0
    assert cr.launch_distribution([])["top1pct_share"] is None


# ---------------------------------------------------------------------------- the rug bar


def test_collapse_diagnostic_measures_but_never_stores():
    recs = [
        # reached $50k, holds $1k -> collapsed
        cr.LaunchRecord(mint="a", creator="x", ath_market_cap_usd=Decimal("50000"),
                        usd_market_cap=Decimal("1000")),
        cr.LaunchRecord(mint="b", creator="x", ath_market_cap_usd=Decimal("60000"),
                        usd_market_cap=Decimal("500")),
        # never left the pad -> not measurable as a collapse
        cr.LaunchRecord(mint="c", creator="y", ath_market_cap_usd=Decimal("3100"),
                        usd_market_cap=Decimal("3000")),
        # reached and held -> not a collapse
        cr.LaunchRecord(mint="d", creator="z", ath_market_cap_usd=Decimal("40000"),
                        usd_market_cap=Decimal("39000")),
    ]
    out = cr.collapse_diagnostic(recs, reject_bar=2)
    assert out["coins_reached_floor"] == 3
    assert out["coins_collapsed"] == 2
    assert out["creators_over_reject_bar"] == 1
    assert out["stored"] is False
    assert "84%" in out["why_not_stored"]


def test_rugged_is_written_as_zero_meaning_not_measured(tmp_db):
    history = cr.CreatorHistory(chain=Chain.SOL, address="w1", launches=9, graduated=2)
    cr.upsert_creator(history, tmp_db)
    row = cr.read_creator("w1", conn=tmp_db)
    assert row is not None
    assert row["rugged"] == 0
    # score stays NULL: triage does not read it and an invented one is another guess.
    assert row["score"] is None


# ------------------------------------------------------------------------------ database


def test_upsert_is_idempotent_and_corrects_rather_than_doubles(tmp_db):
    history = cr.CreatorHistory(
        chain=Chain.SOL, address="w1", launches=5, graduated=1,
        median_peak_mcap_usd=Decimal("12345.5"),
    )
    cr.upsert_creator(history, tmp_db)
    cr.upsert_creator(history, tmp_db)
    rows = fetch_all(tmp_db, "SELECT * FROM creators WHERE address=?", ("w1",))
    assert len(rows) == 1
    assert rows[0]["launches"] == 5
    assert rows[0]["graduated"] == 1
    assert rows[0]["median_peak_mcap_usd"] == "12345.5"

    cr.upsert_creator(replace(history, launches=7, graduated=2), tmp_db)
    rows = fetch_all(tmp_db, "SELECT * FROM creators WHERE address=?", ("w1",))
    assert len(rows) == 1
    assert rows[0]["launches"] == 7
    assert rows[0]["graduated"] == 2


def test_median_peak_is_stored_as_text_not_a_float(tmp_db):
    cr.upsert_creator(
        cr.CreatorHistory(
            chain=Chain.SOL, address="w2", launches=1, graduated=0,
            median_peak_mcap_usd=Decimal("0.1"),
        ),
        tmp_db,
    )
    row = cr.read_creator("w2", conn=tmp_db)
    assert row is not None
    assert isinstance(row["median_peak_mcap_usd"], str)
    assert Decimal(row["median_peak_mcap_usd"]) == Decimal("0.1")


# ------------------------------------------------------------------------------ backfill


def _index_rows(creator_counts: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    i = 0
    for creator, n in creator_counts.items():
        for _ in range(n):
            rows.append({"mint": f"m{i}", "creator": creator, "complete": False,
                         "created_timestamp": 1_700_000_000_000 + i})
            i += 1
    return rows


def test_backfill_writes_lifetime_counts_not_window_counts(monkeypatch, tmp_db):
    index = _index_rows({"alpha": 2, "beta": 1})
    histories = {
        # alpha appears twice in the window but has eleven lifetime launches, one graduated
        "alpha": [
            {"mint": f"a{i}", "creator": "alpha", "complete": i == 0,
             "created_timestamp": 1_600_000_000_000 + i,
             "ath_market_cap_usd": None, "ath_market_cap": 50_000 if i == 0 else 4_000,
             "usd_market_cap": 45_000 if i == 0 else 1_000}
            for i in range(11)
        ],
        "beta": [{"mint": "b0", "creator": "beta", "complete": False,
                  "created_timestamp": 1_600_000_000_001}],
    }
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories))
    report = cr.backfill_creators(tmp_db, pages=1, config=TEST_CONFIG)

    assert report.creators_discovered == 2
    assert report.creators_written == 2
    assert report.lifetime_coins == 12
    assert report.lifetime_graduated == 1
    assert report.creators_with_a_graduate == 1

    alpha = cr.read_creator("alpha", conn=tmp_db)
    assert alpha is not None
    assert alpha["launches"] == 11  # not 2: the window count is thrown away
    assert alpha["graduated"] == 1


def test_backfill_is_idempotent(monkeypatch, tmp_db):
    index = _index_rows({"alpha": 3})
    histories = {"alpha": [{"mint": f"a{i}", "creator": "alpha", "complete": False} for i in range(6)]}
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories))
    first = cr.backfill_creators(tmp_db, pages=1, config=TEST_CONFIG)
    second = cr.backfill_creators(tmp_db, pages=1, config=TEST_CONFIG)
    rows = fetch_all(tmp_db, "SELECT * FROM creators", ())
    assert len(rows) == 1
    assert rows[0]["launches"] == 6
    assert first.lifetime_coins == second.lifetime_coins == 6


def test_backfill_skips_creators_refreshed_recently(monkeypatch, tmp_db):
    index = _index_rows({"alpha": 1})
    histories = {"alpha": [{"mint": "a0", "creator": "alpha", "complete": False}]}
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories))
    warm = cr.BackfillConfig(min_interval_s=0.0, refresh_after_s=3600.0, creator_ttl_s=0.0)
    cr.backfill_creators(tmp_db, pages=1, config=warm)
    again = cr.backfill_creators(tmp_db, pages=1, config=warm)
    assert again.creators_skipped_fresh == 1
    assert again.creators_fetched == 0


def test_a_creator_we_could_not_measure_is_absent_not_zero(monkeypatch, tmp_db):
    index = _index_rows({"alpha": 1, "ghost": 1})
    histories = {"alpha": [{"mint": "a0", "creator": "alpha", "complete": True}]}
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories, fail={"ghost"}))
    report = cr.backfill_creators(tmp_db, pages=1, config=TEST_CONFIG)
    assert report.creators_failed == 1
    assert report.creators_no_history == 0
    assert cr.read_creator("ghost", conn=tmp_db) is None
    assert cr.read_creator("alpha", conn=tmp_db) is not None


def test_dry_run_writes_nothing(monkeypatch, tmp_db):
    index = _index_rows({"alpha": 1})
    histories = {"alpha": [{"mint": "a0", "creator": "alpha", "complete": True}]}
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories))
    report = cr.backfill_creators(tmp_db, pages=1, config=TEST_CONFIG, dry_run=True)
    assert report.creators_fetched == 1
    assert report.creators_written == 0
    assert fetch_all(tmp_db, "SELECT * FROM creators", ()) == []


def test_max_creators_takes_the_busiest_first(monkeypatch, tmp_db):
    index = _index_rows({"quiet": 1, "busy": 4})
    histories = {
        "busy": [{"mint": f"b{i}", "creator": "busy"} for i in range(4)],
        "quiet": [{"mint": "q0", "creator": "quiet"}],
    }
    monkeypatch.setattr(cr, "get_json", FakeApi(index=index, histories=histories))
    cr.backfill_creators(tmp_db, pages=1, max_creators=1, config=TEST_CONFIG)
    assert cr.read_creator("busy", conn=tmp_db) is not None
    assert cr.read_creator("quiet", conn=tmp_db) is None


def test_non_solana_chain_reports_nothing_rather_than_guessing(monkeypatch, tmp_db):
    monkeypatch.setattr(cr, "get_json", FakeApi())
    report = cr.backfill_creators(tmp_db, chain=Chain.BSC, pages=1, config=TEST_CONFIG)
    assert report.creators_discovered == 0
    assert report.requests == 0


# -------------------------------------------------------------------------------- status


def test_status_counts_what_triage_can_actually_promote_on(tmp_db):
    cr.upsert_creator(cr.CreatorHistory(chain=Chain.SOL, address="g", launches=8, graduated=2), tmp_db)
    cr.upsert_creator(cr.CreatorHistory(chain=Chain.SOL, address="n", launches=3, graduated=0), tmp_db)
    status = cr.creator_status(tmp_db)
    assert status["rows"] == 2
    assert status["launches_total"] == 11
    assert status["graduated_total"] == 2
    assert status["triage_promotable"] == 1
    assert status["serial_launchers_5plus"] == 1
    assert status["rugged_rows"] == 0


def test_status_on_an_empty_table_is_honest(tmp_db):
    status = cr.creator_status(tmp_db)
    assert status["rows"] == 0
    assert status["graduation_rate"] is None
    assert status["triage_promotable"] == 0


# ---------------------------------------------------------------- the point of the task


def test_a_backfilled_graduate_turns_a_defer_into_a_promote(tmp_db):
    """The whole task in one test: creator history is what lets tier 0 promote.

    Triage scores a launch with no distinguishing fact at ``unknown_prior`` (0.20), which
    is below ``promote_threshold`` (0.55). One prior graduate adds
    ``w_creator_prior_graduate`` (0.45) and clears the bar. With an empty ``creators``
    table that addition can never happen, which is exactly the 0-promote result measured
    on live launches.
    """
    from kaiba.execution.triage import Verdict, reset_dedup, triage

    reset_dedup()
    event = {"mint": "So11111111111111111111111111111111111111112", "traderPublicKey": "devA",
             "name": "x", "symbol": "X", "uri": "ipfs://x"}
    before = triage(event, conn=tmp_db)
    assert before.verdict is Verdict.DEFER

    cr.upsert_creator(cr.CreatorHistory(chain=Chain.SOL, address="devA", launches=6, graduated=1), tmp_db)
    after = triage(event, conn=tmp_db)
    assert after.verdict is Verdict.PROMOTE
    assert after.score > before.score
    reset_dedup()


@pytest.mark.live
def test_live_pumpfun_index_still_has_the_shape_we_parse(tmp_db):
    """PAGE_LIMIT, the creator field and ath_market_cap are all measured constants.

    ``tmp_db`` is required, not cosmetic: the limiter keeps its budget in the database,
    so without a migrated one every provider call fails before it reaches the network.
    """
    page = cr.fetch_page(offset=0, config=cr.BackfillConfig(), conn=tmp_db)
    assert page.ok
    assert len(page) == cr.PAGE_LIMIT
    assert all(r.creator for r in page.records)
    assert any(r.ath_market_cap_usd is not None for r in page.records)


@pytest.mark.live
def test_live_offset_cap_is_still_where_we_measured_it(tmp_db):
    beyond = cr.fetch_page(offset=cr.MAX_OFFSET + 10, config=cr.BackfillConfig(), conn=tmp_db)
    assert beyond.ok
    assert len(beyond) == 0


@pytest.mark.live
def test_live_creator_filter_returns_that_creators_history(tmp_db):
    """``?creator=`` is the whole design: lifetime history, not a recency window."""
    index = cr.fetch_page(offset=0, config=cr.BackfillConfig(), conn=tmp_db)
    address = next(r.creator for r in index.records if r.creator)
    got = cr.fetch_creator_history(address, config=cr.BackfillConfig(), conn=tmp_db)
    assert got.ok
    assert got.history is not None
    assert got.history.launches >= 1
    assert all(r.creator == address for r in got.records)
