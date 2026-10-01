"""Tests for the early-alpha detection layer.

Three of these exist because of a specific way this kind of module fails silently, and
they matter more than the parser coverage around them:

* ``test_diff_*`` / ``test_poll_prelaunch_first_poll_reports_nothing`` - a diff-based
  source must establish a baseline before it can report, or day one produces two hundred
  "new pre-launch markets" and the operator stops reading the feed.
* ``test_every_poller_waits_for_a_limiter_slot`` - ``wait_for_slot_s`` is the rule two
  earlier adapters in this repo each got wrong, and getting it wrong loses every call
  after the first with no error anywhere.
* ``test_healthy_zero_is_not_a_failure`` and the health tests around it - a quiet
  detector and a quiet market look identical, so the health table has to distinguish
  them without the operator guessing.

Offline by default against fixtures recorded live on 2026-09-20. The ``@pytest.mark.live``
tests hit the real endpoints and are skipped unless ``KAIBA_LIVE_TESTS=1``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import EventKind, EvidenceBasis, Receipt, now_ms
from kaiba.hunters import signals as S
from kaiba.providers._http import Fetched

FIXTURES = Path(__file__).parent / "fixtures" / "signals"

#: Pinned so the certificate-age window is deterministic. The fixtures were recorded on
#: 2026-09-20; a test that used the wall clock would start failing a month later for a
#: reason that has nothing to do with the code.
NOW_MS = 1_789_900_000_000


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def atom(name: str) -> str:
    return fixture(name)["body"]


def ok(data: Any) -> Fetched:
    return Fetched(data, Receipt(provider="x", endpoint="y", basis=EvidenceBasis.PROVIDER_REPORTED))


def down(note: str = "boom") -> Fetched:
    return Fetched(None, Receipt(provider="x", endpoint="y", basis=EvidenceBasis.UNAVAILABLE, note=note))


# ------------------------------------------------------------- certificate transparency


def test_classify_subdomain_strong_weak_and_boring() -> None:
    assert S.classify_subdomain("claim.scroll.io", "scroll.io") == ("strong", 0.90)
    assert S.classify_subdomain("airdrop.app.monad.xyz", "monad.xyz")[0] == "strong"
    assert S.classify_subdomain("rewards.scroll.io", "scroll.io") == ("weak", 0.45)
    assert S.classify_subdomain("rpc.scroll.io", "scroll.io") is None
    assert S.classify_subdomain("scroll.io", "scroll.io") is None
    # A wildcard certificate names no host, so it is not a claim page going live.
    assert S.classify_subdomain("*.scroll.io", "scroll.io") is None
    # Never let a lookalike domain answer for the watched one.
    assert S.classify_subdomain("claim.notscroll.io", "scroll.io") is None


def test_parse_crtsh_finds_the_claim_subdomain_in_the_recorded_response() -> None:
    signals = S.parse_crtsh(fixture("crtsh_scroll"), "scroll.io", max_age_days=3650, now=NOW_MS)
    subjects = {s.subject for s in signals}
    assert "claim.scroll.io" in subjects
    claim = next(s for s in signals if s.subject == "claim.scroll.io")
    assert claim.source == "crtsh"
    assert claim.kind == S.SignalKind.CERT_SUBDOMAIN
    assert claim.payload["strength"] == "strong"
    assert claim.confidence == 0.90
    assert claim.event_at_ms is not None and claim.event_at_ms < NOW_MS
    # rpc./portal./cloak-* are in the same fixture and must not be signals.
    assert not any(s.subject.startswith("rpc.") for s in signals)


def test_parse_crtsh_drops_history_so_the_first_poll_is_not_a_flood() -> None:
    rows = [
        {"name_value": "claim.example.io", "not_before": "2024-10-19T00:00:00", "id": 1},
        {"name_value": "airdrop.example.io", "not_before": "2026-09-19T00:00:00", "id": 2},
    ]
    fresh = S.parse_crtsh(rows, "example.io", max_age_days=30, now=NOW_MS)
    assert [s.subject for s in fresh] == ["airdrop.example.io"]
    everything = S.parse_crtsh(rows, "example.io", max_age_days=3650, now=NOW_MS)
    assert len(everything) == 2


def test_parse_crtsh_lead_estimate_is_stated_or_refused() -> None:
    from datetime import UTC, datetime

    age_ms = 6 * 3_600_000
    iso = datetime.fromtimestamp((NOW_MS - age_ms) / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    fresh = S.parse_crtsh([{"name_value": "claim.a.io", "not_before": iso}], "a.io", now=NOW_MS)
    sig = fresh[0]
    assert sig.lead_ms is not None
    assert sig.lead_basis == "prior_3.5d_median_minus_cert_age"
    # 3.5 days of prior lead minus the six hours the certificate has already been public.
    assert abs(sig.lead_ms - (S.CERT_PRIOR_LEAD_MS - age_ms)) < 2000

    old = S.parse_crtsh(
        [{"name_value": "claim.b.io", "not_before": "2026-09-01T00:00:00"}], "b.io",
        max_age_days=60, now=NOW_MS,
    )
    assert old[0].lead_ms is None
    assert old[0].lead_basis == "cert_older_than_prior_lead_window"


def test_parse_crtsh_keeps_the_first_issuance_not_the_renewal() -> None:
    rows = [
        {"name_value": "claim.c.io", "not_before": "2026-09-19T00:00:00", "id": 2},
        {"name_value": "claim.c.io", "not_before": "2026-09-10T00:00:00", "id": 1},
    ]
    signals = S.parse_crtsh(rows, "c.io", max_age_days=60, now=NOW_MS)
    assert len(signals) == 1
    assert signals[0].payload["crtsh_id"] == 1


def test_parse_crtsh_survives_rubbish() -> None:
    assert S.parse_crtsh(None, "a.io") == []
    assert S.parse_crtsh(["not a dict", {"no_names": 1}], "a.io") == []


# ------------------------------------------------------------------------ venue listings


def test_extract_symbols_ignores_quote_currencies() -> None:
    assert S.extract_symbols("Binance Will List Ondo (ONDO) with (USDT) pairs") == ["ONDO"]
    assert S.extract_symbols("페이팔유에스디(PYUSD), 제이피와이코인(JPYC) 신규 거래지원") == ["PYUSD", "JPYC"]
    assert S.extract_symbols("no tickers here") == []


def test_extract_symbols_reads_the_pair_not_the_project_name() -> None:
    """OKX writes "VVV/USDT (Venice)". "Venice" is not a ticker and cannot be traded."""
    assert S.extract_symbols("OKX to list VVV/USDT (Venice) for spot trading") == ["VVV"]


def test_extract_symbols_handles_binance_concatenated_pairs() -> None:
    """Binance futures writes "PONSUSDT" and puts nothing useful in parentheses."""
    title = "Binance Futures Will Launch USDⓈ-Margined PONSUSDT Perpetual Contract"
    assert S.extract_symbols(title) == ["PONS"]
    assert S.extract_symbols("Binance Will List MarsCoin (MARSCOIN)") == ["MARSCOIN"]


def test_parse_binance_fixture() -> None:
    signals = S.parse_binance(fixture("binance_announcements"))
    assert signals, "the recorded Binance CMS response contained no listing announcements"
    for sig in signals:
        assert sig.source == "binance"
        assert sig.kind == S.SignalKind.VENUE_LISTING
        assert sig.event_at_ms and sig.event_at_ms > 1_500_000_000_000
        assert sig.url and sig.url.startswith("https://www.binance.com/")
        assert sig.lead_ms is None
        assert sig.lead_basis == "venue_publishes_announcement_time_not_listing_time"


def test_parse_okx_fixture_and_error_envelope() -> None:
    signals = S.parse_okx(fixture("okx_announcements"))
    assert signals
    assert all(s.source == "okx" and s.event_at_ms for s in signals)
    assert any("VVV" in s.payload["symbols"] for s in signals)
    # OKX signals failure in the envelope rather than the status code.
    assert S.parse_okx({"code": "50001", "msg": "nope", "data": []}) == []


def test_parse_upbit_fixture_flags_korean_delistings() -> None:
    signals = S.parse_upbit(fixture("upbit_announcements"))
    assert signals
    assert any(s.payload["delisting"] for s in signals), "거래지원 종료 should read as a delisting"
    listings = [s for s in signals if not s.payload["delisting"]]
    assert listings and all(s.confidence == 0.90 for s in listings)
    assert all(s.confidence == 0.80 for s in signals if s.payload["delisting"])


def test_venue_titles_that_are_not_listings_are_dropped() -> None:
    assert S._venue_signal("okx", "Scheduled system maintenance", None, None) is None
    assert S._venue_signal("okx", "OKX to list FOO (FOO) for spot trading", None, None) is not None


def test_venue_parsers_never_raise_on_a_redesign() -> None:
    for parser in (S.parse_binance, S.parse_okx, S.parse_upbit):
        assert parser({}) == []
        assert parser({"data": None}) == []


# ---------------------------------------------------------------------------- governance


def test_parse_snapshot_fixture_and_symbol_bump() -> None:
    signals = S.parse_snapshot(fixture("snapshot_spaces"))
    assert len(signals) >= 5
    assert all(s.kind == S.SignalKind.GOV_SPACE for s in signals)
    with_symbol = [s for s in signals if s.payload["symbol"]]
    without = [s for s in signals if not s.payload["symbol"]]
    assert with_symbol and without
    # This is the noisiest source in the module; a declared ticker is the only cheap tell.
    assert min(s.confidence for s in with_symbol) > max(s.confidence for s in without)


def test_parse_discourse_only_keeps_token_threads() -> None:
    signals = S.parse_discourse(fixture("discourse_uniswap"), "gov.uniswap.org")
    assert signals, "the recorded forum page had at least one tokenomics thread"
    assert all("gov.uniswap.org" in (s.url or "") for s in signals)
    assert all(s.kind == S.SignalKind.GOV_TOPIC for s in signals)
    # Every kept topic must actually match; the filter is the point of this source.
    assert all(S._GOV_KEYWORDS.search(s.title + s.payload["excerpt"]) for s in signals)


def test_parse_discourse_tolerates_an_empty_forum() -> None:
    assert S.parse_discourse({}, "gov.example.org") == []
    assert S.parse_discourse({"topic_list": {"topics": []}}, "gov.example.org") == []


# --------------------------------------------------------------------------- repositories


def test_parse_atom_commits_fixture() -> None:
    commits = S.parse_atom(atom("github_merkle_commits"), "Uniswap/merkle-distributor",
                           S.SignalKind.REPO_COMMIT)
    assert len(commits) >= 2
    assert all(c.source == "github" and c.event_at_ms for c in commits)
    assert any("airdrop" in c.title.lower() or "merkle" in c.title.lower() for c in commits)


def test_parse_atom_release_kind_and_fields() -> None:
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>tag:github.com,2008:Repository/1/v1.0.0</id>"
        "<updated>2026-09-19T10:00:00Z</updated>"
        "<title>v1.0.0 merkle distributor</title>"
        '<link rel="alternate" type="text/html" href="https://github.com/o/r/releases/tag/v1.0.0"/>'
        "<content type=\"html\">ship it</content>"
        "<author><name>dev</name></author>"
        "</entry></feed>"
    )
    sigs = S.parse_atom(feed, "o/r", S.SignalKind.REPO_RELEASE)
    assert len(sigs) == 1
    assert sigs[0].kind == S.SignalKind.REPO_RELEASE
    assert sigs[0].subject == "o/r"
    assert sigs[0].url == "https://github.com/o/r/releases/tag/v1.0.0"
    assert sigs[0].event_at_ms == 1_789_812_000_000  # 2026-09-19T10:00:00Z
    assert sigs[0].payload["matched"] == "merkle"


def test_parse_atom_keyword_filter_returns_nothing_for_ordinary_activity() -> None:
    assert S.parse_atom(atom("github_uniswap_commits"), "Uniswap/governance",
                        S.SignalKind.REPO_COMMIT) == []


def test_infrastructure_changelog_prose_is_not_a_token_event() -> None:
    """Both GitHub "signals" from the first live run were false positives of this shape.

    An op-challenger release matched ``claim`` in "the claim count query is now skipped",
    and a monad release matched ``allocation`` in "past the end of the vector's
    allocation". Two out of two is not a margin problem, it is a source that does not
    work, so the body is filtered far more narrowly than the title.
    """
    assert S.parse_atom(atom("github_optimism_releases"), "ethereum-optimism/optimism",
                        S.SignalKind.REPO_RELEASE) == []
    assert not S._REPO_BODY_KEYWORDS.search("read past the end of the vector's allocation")
    assert not S._REPO_BODY_KEYWORDS.search("the claim count query is now skipped")
    assert S._REPO_BODY_KEYWORDS.search("the token allocation snapshot is final")


def test_repo_keywords_are_word_anchored() -> None:
    """``GetL2UnclaimedWithdrawalsByAddress`` matched a bare ``claim`` in a live feed."""
    assert not S._REPO_KEYWORDS.search("fix bridge history GetL2UnclaimedWithdrawalsByAddress")
    assert S._REPO_KEYWORDS.search("add claim contract")


def test_parse_atom_on_broken_xml_is_empty_not_an_exception() -> None:
    assert S.parse_atom("<not xml", "a/b", S.SignalKind.REPO_COMMIT) == []
    assert S.parse_atom("", "a/b", S.SignalKind.REPO_COMMIT) == []


# ------------------------------------------------------------------- pre-launch markets


def test_parse_hyperliquid_excludes_delisted_assets() -> None:
    assets = S.parse_hyperliquid(fixture("hyperliquid_meta"))
    assert "BTC" in assets and "HYPE" in assets
    assert "MATIC" not in assets, "MATIC is flagged isDelisted in the recorded meta response"


def test_parse_aevo_is_a_bare_list() -> None:
    assets = S.parse_aevo(fixture("aevo_assets"))
    assert "BTC" in assets and len(assets) > 50
    assert S.parse_aevo({"not": "a list"}) == []


def test_diff_assets_first_call_establishes_a_baseline_and_reports_nothing(tmp_db) -> None:
    assert S.diff_assets(tmp_db, "hyperliquid", ["BTC", "ETH", "SOL"]) == []
    rows = fetch_all(tmp_db, "SELECT item FROM alpha_baseline WHERE source='hyperliquid'")
    assert {r["item"] for r in rows} == {"BTC", "ETH", "SOL"}
    health = fetch_one(tmp_db, "SELECT baseline_ms FROM alpha_source_health WHERE source=?",
                       ("hyperliquid",))
    assert health and health["baseline_ms"]


def test_diff_assets_reports_only_what_is_new(tmp_db) -> None:
    S.diff_assets(tmp_db, "aevo", ["BTC", "ETH"])
    assert S.diff_assets(tmp_db, "aevo", ["BTC", "ETH", "NEWCO"]) == ["NEWCO"]
    # Once recorded it is no longer new, however many times we poll.
    assert S.diff_assets(tmp_db, "aevo", ["BTC", "ETH", "NEWCO"]) == []


def test_diff_assets_empty_response_never_arms_the_next_poll(tmp_db) -> None:
    """A source answering 200-with-nothing must not become a baseline of zero assets."""
    assert S.diff_assets(tmp_db, "aevo", []) == []
    assert fetch_all(tmp_db, "SELECT item FROM alpha_baseline WHERE source='aevo'") == []
    # The next real response is therefore still a baseline, not 200 fake discoveries.
    assert S.diff_assets(tmp_db, "aevo", ["BTC", "ETH"]) == []


def test_poll_prelaunch_first_poll_reports_nothing_then_detects_the_new_market(tmp_db) -> None:
    meta = fixture("hyperliquid_meta")
    first, ok_n, total, err = S.poll_prelaunch("hyperliquid", tmp_db, raw=meta)
    assert first == [] and ok_n == 1 and total == 1 and err is None

    universe = list(meta["universe"]) + [{"name": "PRELAUNCHX", "szDecimals": 2, "maxLeverage": 3}]
    second, _, _, _ = S.poll_prelaunch("hyperliquid", tmp_db, raw={"universe": universe})
    assert [s.subject for s in second] == ["PRELAUNCHX"]
    sig = second[0]
    assert sig.kind == S.SignalKind.PRELAUNCH_MARKET
    assert sig.event_at_ms is None
    assert sig.lead_ms is None and sig.lead_basis == "venue_publishes_no_listing_timestamp"


def test_poll_prelaunch_empty_payload_is_a_failure_not_a_baseline(tmp_db) -> None:
    signals, ok_n, _, err = S.poll_prelaunch("aevo", tmp_db, raw=[])
    assert signals == [] and ok_n == 0 and err


# ------------------------------------------------------------------ recording and dedupe


def _signal(**kw: Any) -> S.AlphaSignal:
    base: dict[str, Any] = dict(source="crtsh", kind=S.SignalKind.CERT_SUBDOMAIN,
                                subject="claim.example.io", title="t", confidence=0.9)
    base.update(kw)
    return S.AlphaSignal(**base)


def test_record_signal_fires_once(tmp_db) -> None:
    sig = _signal()
    assert S.record_signal(tmp_db, sig) is True
    assert S.record_signal(tmp_db, sig) is False
    rows = fetch_all(tmp_db, "SELECT * FROM alpha_signals")
    assert len(rows) == 1
    assert rows[0]["source"] == "crtsh"
    assert rows[0]["lead_basis"] == "not_estimable"


def test_record_signal_emits_alpha_signal(tmp_db) -> None:
    S.record_signal(tmp_db, _signal(lead_ms=None))
    events = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?", (EventKind.ALPHA_SIGNAL.value,))
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["subject"] == "claim.example.io"
    # A missing lead must look missing on the dashboard, not like a lead of zero.
    assert payload["lead_ms"] is None and payload["lead_estimable"] is False


def test_venue_listings_emit_alpha_listing(tmp_db) -> None:
    S.record_signal(tmp_db, _signal(source="okx", kind=S.SignalKind.VENUE_LISTING, subject="ONDO",
                                    event_at_ms=1_789_000_000_000))
    rows = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?", (EventKind.ALPHA_LISTING.value,))
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    # The listing-pop lane reads these two keys; give it the real time or nothing at all.
    assert payload["announced_ms"] == 1_789_000_000_000
    assert payload["exchange"] == "okx"
    assert not fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind=?", (EventKind.ALPHA_SIGNAL.value,))


def test_a_listing_with_no_announcement_time_never_reaches_the_latency_lane(tmp_db) -> None:
    """``listing_pop`` falls back to the event's own ``ts_ms`` when ``announced_ms`` is absent.

    That makes an untimed listing look like it was announced zero seconds ago and scores it
    near maximum strength on a latency nobody measured. An untimed listing is therefore
    emitted as a plain ALPHA_SIGNAL, which no lane treats as a latency trigger.
    """
    S.record_signal(tmp_db, _signal(source="binance", kind=S.SignalKind.VENUE_LISTING,
                                    subject="FOO", event_at_ms=None))
    assert not fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind=?", (EventKind.ALPHA_LISTING.value,))
    assert fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind=?", (EventKind.ALPHA_SIGNAL.value,))


def test_signal_key_separates_sources_but_not_repeat_sightings() -> None:
    a = _signal()
    b = _signal()
    c = _signal(source="okx")
    d = _signal(key_seed="https://example.com/1")
    assert a.signal_key == b.signal_key
    assert a.signal_key != c.signal_key != d.signal_key


def test_detect_lag_is_none_when_the_source_publishes_no_timestamp() -> None:
    assert _signal(event_at_ms=None).detect_lag_ms is None
    sig = _signal(event_at_ms=1000, first_seen_ms=4000)
    assert sig.detect_lag_ms == 3000


# ---------------------------------------------------------------------------- health


def test_record_health_tracks_last_success_separately_from_last_attempt(tmp_db) -> None:
    S.record_health(tmp_db, "crtsh", "certificate", ok=True, count=3, new=1, interval_s=3600)
    first = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert first["last_ok_ms"] and first["fail_streak"] == 0 and first["total_new"] == 1

    S.record_health(tmp_db, "crtsh", "certificate", ok=False, count=0, new=0, interval_s=3600,
                    error="502 Bad Gateway")
    second = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert second["last_ok_ms"] == first["last_ok_ms"], "a failure must not move last_ok_ms"
    assert second["last_poll_ms"] >= first["last_poll_ms"]
    assert second["fail_streak"] == 1
    assert "502" in second["last_error"]


def test_healthy_zero_is_not_a_failure(tmp_db) -> None:
    """"crt.sh answered, nothing new this hour" is the expected case, not an outage."""
    S.record_health(tmp_db, "crtsh", "certificate", ok=True, count=0, new=0, interval_s=3600)
    row = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert row["fail_streak"] == 0 and row["last_ok_ms"]


def test_source_health_lists_sources_that_have_never_run(tmp_db) -> None:
    health = {h["source"]: h for h in S.source_health(tmp_db)}
    assert set(health) == set(S.SOURCES)
    assert all(h["state"] == "never_run" for h in health.values())


def test_source_health_states(tmp_db) -> None:
    cfg = S.SignalsConfig()
    S.record_health(tmp_db, "okx", "venue", ok=True, count=5, new=0, interval_s=180)
    assert {h["source"]: h["state"] for h in S.source_health(tmp_db, cfg)}["okx"] == "ok"

    tolerance = cfg.dead_after_s("okx")
    tmp_db.execute("UPDATE alpha_source_health SET last_ok_ms=? WHERE source='okx'",
                   (now_ms() - int(tolerance * 1000 * 0.75),))
    assert {h["source"]: h["state"] for h in S.source_health(tmp_db, cfg)}["okx"] == "degraded"

    tmp_db.execute("UPDATE alpha_source_health SET last_ok_ms=? WHERE source='okx'",
                   (now_ms() - (tolerance + 60) * 1000,))
    assert {h["source"]: h["state"] for h in S.source_health(tmp_db, cfg)}["okx"] == "dead"


def test_check_health_emits_for_a_dead_source_and_dedupes(tmp_db) -> None:
    S.record_health(tmp_db, "crtsh", "certificate", ok=True, count=1, new=0, interval_s=3600)
    tmp_db.execute("UPDATE alpha_source_health SET last_ok_ms=? WHERE source='crtsh'",
                   (now_ms() - 7 * 3_600_000,))
    bad = S.check_health(tmp_db)
    assert [b["source"] for b in bad] == ["crtsh"]
    events = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?", (EventKind.SYSTEM.value,))
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert payload["reason"] == "alpha_source_dead"
    assert payload["since_ok_s"] >= 6 * 3600
    assert events[0]["level"] == "error"

    S.check_health(tmp_db)
    assert len(fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?", (EventKind.SYSTEM.value,))) == 1


def test_a_source_that_has_never_succeeded_is_dead_not_quiet(tmp_db) -> None:
    S.record_health(tmp_db, "crtsh", "certificate", ok=False, count=0, new=0, interval_s=3600,
                    error="timeout")
    states = {h["source"]: h["state"] for h in S.source_health(tmp_db)}
    assert states["crtsh"] == "dead"


# ---------------------------------------------------------------------------- scheduling


def test_due_sources_respects_each_interval(tmp_db) -> None:
    cfg = S.SignalsConfig()
    assert set(S.due_sources(tmp_db, cfg)) == set(S.SOURCES)
    S.record_health(tmp_db, "okx", "venue", ok=True, count=1, new=0, interval_s=180)
    assert "okx" not in S.due_sources(tmp_db, cfg)
    assert "okx" in S.due_sources(tmp_db, cfg, force=True)
    assert S.due_sources(tmp_db, cfg, only=["crtsh"]) == ["crtsh"]
    assert S.due_sources(tmp_db, cfg, only=["nonexistent"]) == []


def test_refresh_records_signals_and_health(tmp_db, monkeypatch) -> None:
    def fake(conn=None, **kw):
        return [_signal(subject="claim.a.io", key_seed="a"),
                _signal(subject="claim.b.io", key_seed="b")], 1, 1, None

    monkeypatch.setitem(S.SOURCES, "crtsh", S.Source("crtsh", "certificate", fake))
    assert S.refresh(tmp_db, only=["crtsh"]) == 2
    assert S.refresh(tmp_db, only=["crtsh"], force=True) == 0, "the same signals must not re-fire"
    row = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert row["last_count"] == 2 and row["total_new"] == 2 and row["fail_streak"] == 0


def test_refresh_survives_a_poller_that_raises(tmp_db, monkeypatch) -> None:
    def explodes(conn=None, **kw):
        raise RuntimeError("crt.sh 502")

    monkeypatch.setitem(S.SOURCES, "crtsh", S.Source("crtsh", "certificate", explodes))
    assert S.refresh(tmp_db, only=["crtsh"]) == 0
    row = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert row["fail_streak"] == 1 and "crt.sh 502" in row["last_error"]
    assert fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind=?", (EventKind.PROVIDER_ERROR.value,))


def test_refresh_marks_a_source_down_without_raising(tmp_db, monkeypatch) -> None:
    monkeypatch.setattr(S, "get_json", lambda *a, **k: down("502 Bad Gateway"))
    monkeypatch.setattr(S, "load_config", lambda *a, **k: S.SignalsConfig(domains=["scroll.io"]))
    assert S.refresh(tmp_db, only=["crtsh"]) == 0
    row = fetch_one(tmp_db, "SELECT * FROM alpha_source_health WHERE source='crtsh'")
    assert row["fail_streak"] == 1 and row["last_ok_ms"] is None
    assert "502" in row["last_error"]


# -------------------------------------------------------------------- the _http contract


def test_every_poller_waits_for_a_limiter_slot(tmp_db, monkeypatch) -> None:
    """The rule two adapters in this repo already broke: several calls, no wait, silent loss."""
    calls: list[dict[str, Any]] = []

    def record(provider, endpoint, url, **kw):
        calls.append({"provider": provider, "endpoint": endpoint, **kw})
        return down("captured")

    monkeypatch.setattr(S, "get_json", record)
    monkeypatch.setattr(S, "post_json", record)
    monkeypatch.setattr(S, "fetch_text", lambda *a, **k: (calls.append({**k, "provider": a[0]}),
                                                          down("captured"))[1])
    cfg = S.SignalsConfig(domains=["a.io", "b.io"], forums=["f.org"], repos=["o/r"])
    for name in S.SOURCES:
        S.SOURCES[name].poll(tmp_db, cfg=cfg)

    assert len(calls) >= 9, "every source must have attempted at least one call"
    missing = [c for c in calls if not c.get("wait_for_slot_s")]
    assert not missing, f"calls without wait_for_slot_s: {missing}"
    assert all(c.get("retries", 1) >= 2 for c in calls), "a flaky free endpoint needs retries"
    crtsh = [c for c in calls if c["provider"] == "crtsh"]
    assert len(crtsh) == 2, "one call per watched domain"
    assert all(c["retries"] >= 3 for c in crtsh), "crt.sh 502s and times out; retries are mandatory"


def test_a_dead_endpoint_is_counted_as_failed_not_as_empty(tmp_db, monkeypatch) -> None:
    monkeypatch.setattr(S, "get_json", lambda *a, **k: down("timeout"))
    cfg = S.SignalsConfig(domains=["a.io", "b.io"])
    signals, ok_n, total, err = S.poll_certificate_transparency(tmp_db, cfg=cfg)
    assert signals == [] and ok_n == 0 and total == 2 and err and "timeout" in err


def test_one_dead_domain_does_not_cost_the_others(tmp_db, monkeypatch) -> None:
    rows = [{"name_value": "claim.b.io", "not_before": "2026-09-19T00:00:00"}]
    monkeypatch.setattr(S, "get_json",
                        lambda p, e, u, **k: down("502") if "a.io" in str(k.get("params")) else ok(rows))
    cfg = S.SignalsConfig(domains=["a.io", "b.io"], cert_max_age_days=3650)
    signals, ok_n, total, err = S.poll_certificate_transparency(tmp_db, cfg=cfg)
    assert [s.subject for s in signals] == ["claim.b.io"]
    assert (ok_n, total) == (1, 2)
    assert err and "a.io" in err


# ---------------------------------------------------------------------------- config


def test_load_config_defaults_when_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KAIBA_SIGNALS_CONFIG", str(tmp_path / "nope.yaml"))
    cfg = S.load_config()
    assert cfg.domains == list(S.DEFAULT_DOMAINS)
    assert cfg.interval_for("crtsh") == 3600
    assert cfg.dead_after_s("okx") == S.DEAD_AFTER_FLOOR_S


def test_load_config_reads_the_operator_watchlist(tmp_path, monkeypatch) -> None:
    path = tmp_path / "signals.yaml"
    path.write_text("domains: [foo.io]\nintervals: {crtsh: 60}\ncert_max_age_days: 7\n",
                    encoding="utf-8")
    monkeypatch.setenv("KAIBA_SIGNALS_CONFIG", str(path))
    cfg = S.load_config()
    assert cfg.domains == ["foo.io"]
    assert cfg.interval_for("crtsh") == 60
    assert cfg.cert_max_age_days == 7
    # An interval the operator did not override still has one.
    assert cfg.interval_for("aevo") == 300


def test_a_broken_config_falls_back_instead_of_stopping_detection(tmp_path, monkeypatch) -> None:
    path = tmp_path / "signals.yaml"
    path.write_text("- this is a list, not a mapping\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_SIGNALS_CONFIG", str(path))
    assert S.load_config().domains == list(S.DEFAULT_DOMAINS)


def test_slow_sources_get_a_longer_death_tolerance() -> None:
    cfg = S.SignalsConfig(intervals={"crtsh": 7200})
    assert cfg.dead_after_s("crtsh") == 6 * 7200


# ---------------------------------------------------------------------------- reporting


def test_weekly_report_with_no_data_still_names_every_source(tmp_db) -> None:
    text = S.weekly_report(tmp_db)
    assert "# Early-alpha signals - weekly" in text
    for name in S.SOURCES:
        assert name in text
    assert "NEVER_RUN" in text
    assert "nothing detected yet" in text


def test_weekly_report_leads_with_a_dead_source(tmp_db) -> None:
    S.record_health(tmp_db, "crtsh", "certificate", ok=True, count=1, new=0, interval_s=3600)
    tmp_db.execute("UPDATE alpha_source_health SET last_ok_ms=?, last_error=? WHERE source='crtsh'",
                   (now_ms() - 9 * 3_600_000, "502 Bad Gateway"))
    S.record_signal(tmp_db, _signal(lead_ms=3 * 86_400_000, lead_basis="prior"))
    text = S.weekly_report(tmp_db)
    assert "sources are not producing" in text
    assert "502 Bad Gateway" in text
    assert "claim.example.io" in text
    assert "3.0d" in text


def test_recent_signals_filters_and_computes_detect_lag(tmp_db) -> None:
    S.record_signal(tmp_db, _signal(subject="a", key_seed="a", event_at_ms=1000,
                                    first_seen_ms=5000))
    S.record_signal(tmp_db, _signal(source="okx", kind=S.SignalKind.VENUE_LISTING, subject="B",
                                    key_seed="b"))
    assert len(S.recent_signals(tmp_db)) == 2
    assert [r["subject"] for r in S.recent_signals(tmp_db, source="okx")] == ["B"]
    assert [r["subject"] for r in S.recent_signals(tmp_db, kind=S.SignalKind.CERT_SUBDOMAIN)] == ["a"]
    row = S.recent_signals(tmp_db, source="crtsh")[0]
    assert row["detect_lag_ms"] == 4000
    assert isinstance(row["payload"], dict)


def test_weekly_report_matches_the_other_hunters_shape(tmp_db) -> None:
    """``kaiba hunt report`` calls ``weekly_report(conn)`` on whatever module it loads."""
    from kaiba.hunters import listings

    assert callable(S.weekly_report) and callable(listings.weekly_report)
    assert isinstance(S.weekly_report(tmp_db), str)
    assert callable(S.refresh)


# -------------------------------------------------------------------------------- live


@pytest.mark.live
def test_live_crtsh() -> None:
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    cfg = S.SignalsConfig(domains=["scroll.io"], cert_max_age_days=3650)
    signals, ok_n, total, err = S.poll_certificate_transparency(conn, cfg=cfg)
    assert ok_n == total, f"crt.sh did not answer: {err}"
    assert any(s.subject.startswith("claim.") for s in signals)


@pytest.mark.live
@pytest.mark.parametrize("venue", ["binance", "okx", "upbit"])
def test_live_venues(venue: str) -> None:
    from kaiba.core.db import ensure_db

    signals, ok_n, _, err = S.poll_venue(venue, ensure_db())
    assert ok_n == 1, f"{venue} did not answer: {err}"
    assert signals, f"{venue} answered but produced no listing signals"


@pytest.mark.live
def test_live_governance() -> None:
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    spaces, ok_n, _, err = S.poll_snapshot(conn)
    assert ok_n == 1 and spaces, f"snapshot: {err}"
    _, ok_forums, total, ferr = S.poll_discourse(conn, cfg=S.SignalsConfig(forums=["gov.uniswap.org"]))
    assert ok_forums == total, f"discourse: {ferr}"


@pytest.mark.live
def test_live_github_atom() -> None:
    got = S.fetch_text("github", "atom.commits",
                       "https://github.com/Uniswap/merkle-distributor/commits.atom",
                       wait_for_slot_s=20.0)
    assert got.ok and "<feed" in (got.data or "")


@pytest.mark.live
@pytest.mark.parametrize("venue", ["hyperliquid", "aevo"])
def test_live_prelaunch(venue: str) -> None:
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    _, ok_n, _, err = S.poll_prelaunch(venue, conn)
    assert ok_n == 1, f"{venue} did not answer: {err}"
