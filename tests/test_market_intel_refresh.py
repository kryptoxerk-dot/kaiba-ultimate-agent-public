"""The box-side refresher for the vendored Aegis market-intel collectors (MARKET-INTEL-REFRESH-CLAUDE-20261103).

Each test is a way the refresher could mislead the dashboard or overstep: an edited vendored file
passed off as Aegis's, a dropped collector going silent, a refresh cadence different from Aegis's
own, last good data shown as fresh after a failure, writing into the source tree, filling a disk in
its HALT band, an unbounded cache, or a run that never ends. No test touches the network.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import namedtuple
from pathlib import Path

import pytest

from kaiba.market_intel import refresh as R
from kaiba.market_intel.aegis import base

ROOT = Path(__file__).resolve().parents[1]
AEGIS = ROOT / "kaiba" / "market_intel" / "aegis"
Disk = namedtuple("Disk", "total used free")
NOW = 1_800_000_000.0


def disk(pct):
    free = 10_000
    used = int(free * pct / (100 - pct))
    return lambda path: Disk(used + free, used, free)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGIS_TERMINAL_DATA", str(tmp_path))
    return tmp_path / "intel"


def synthetic(monkeypatch, behaviour):
    """Replace the real collectors with stubs whose run() does ``behaviour[key]``."""
    calls = []

    def make(key, interval):
        def run(http):
            calls.append(key)
            out = behaviour[key]
            if isinstance(out, Exception):
                raise out
            return out
        return base.Collector(key, key.title(), interval, run, ("https://example.invalid/",))

    table = {k: make(k, i) for k, i in (("calendar", 3600), ("news", 300), ("etf_flows", 10800))}
    monkeypatch.setattr(R, "collectors", lambda: table)
    monkeypatch.setattr(R, "SELECTED", tuple(table))
    return calls


# ------------------------------------------------------------------ provenance and selection


def test_every_vendored_file_matches_the_hash_its_source_note_records():
    note = (AEGIS / "SOURCE.md").read_text(encoding="utf-8")
    pins = dict(re.findall(r"\| `([a-z_]+\.py)` \| `([0-9a-f]{64})` \|", note))
    assert set(pins) == {p.name for p in AEGIS.glob("*.py")}
    for name, digest in pins.items():
        assert hashlib.sha256((AEGIS / name).read_bytes()).hexdigest() == digest, name


def test_every_selected_collector_exists_and_nothing_excluded_is_selected():
    table = R.collectors()
    assert tuple(table) == R.SELECTED
    assert not set(R.SELECTED) & set(R.EXCLUDED)
    # what the dashboard reads (dashboard/ultimate_intel.py SPECS) is selected, or excluded with a reason
    for key in ("calendar", "macro_fred", "news", "etf_flows", "btc_whale_tx", "smart_money", "tickers", "markets"):
        assert key in R.SELECTED or key in R.EXCLUDED, key


@pytest.mark.parametrize("doc, now, due", [
    (None, NOW, True),                                                     # never ran
    ({"ok": True, "fetched_ts": NOW - 3599}, NOW, False),                  # inside the interval
    ({"ok": True, "fetched_ts": NOW - 3600}, NOW, True),
    ({"ok": False, "fetched_ts": NOW - 9999, "attempted_ts": NOW - 719}, NOW, False),  # failure: 3600 // 5
    ({"ok": False, "fetched_ts": NOW - 9999, "attempted_ts": NOW - 720}, NOW, True),
])
def test_due_is_aegis_own_rule(cache, doc, now, due):
    c = base.Collector("calendar", "Calendar", 3600, lambda http: {}, ())
    assert R.is_due(c, doc, now) is due
    if doc is not None:  # parity with Aegis's Refresher.due on the same cache file
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "calendar.json").write_text(json.dumps(doc))
    ref = base.Refresher([c])
    try:
        assert (("calendar" in ref.due(now)) is due)
    finally:
        ref._pool.shutdown(wait=False)


@pytest.mark.parametrize("error, after, due", [
    ("RuntimeError: all feeds failed: ^GSPC: HTTPStatusError: Client error '429 Too Many Requests' for url", 3600, False),
    ("RuntimeError: farside_btc: HTTPStatusError: Client error '403 Forbidden' for url", 6 * 3600 - 1, False),
    ("RuntimeError: farside_btc: HTTPStatusError: Client error '403 Forbidden' for url", 6 * 3600, True),
    ("RuntimeError: timeout", 60, True),   # any other failure keeps Aegis's interval // 5 (min 60 s)
])
def test_a_source_that_refused_us_is_not_hammered(error, after, due):
    """Box, 2026-11-03: Yahoo 429 and farside/CFTC 403 from the datacenter IP. Aegis would retry
    ``markets`` every 60 s; a refusal now waits six hours."""
    c = base.Collector("markets", "Global markets", 300, lambda http: {}, ())
    doc = {"ok": False, "fetched_ts": None, "attempted_ts": NOW - after, "error": error}
    assert R.is_due(c, doc, NOW) is due


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>FOMC holds rates steady</title><link>https://example.invalid/fomc</link>
<pubDate>Thu, 05 Nov 2026 19:00:00 GMT</pubDate></item>
<item><title>CPI rises 0.3% in October</title><link>https://example.invalid/cpi</link>
<pubDate>Tue, 03 Nov 2026 03:00:00 GMT</pubDate></item>
</channel></rss>"""
FETCH = 1_793_678_400.0  # 2026-11-03 04:00 UTC: the FOMC item is ~2.6 days in the future


def test_a_future_dated_headline_keeps_its_clock_and_stays_out_of_current_news(cache):
    """Deviation 1 (Codex review of 8e85fb5): Aegis re-dated it to the fetch time, so a future-dated
    Fed headline read as current news. Now it is quarantined with its ORIGINAL clocks."""
    from kaiba.market_intel.aegis import news

    first_feed = news.FEEDS[0][2]
    out = news.collect(base.FixtureHttp({first_feed: RSS}), now=FETCH)
    titles = [it["title"] for it in out["items"]]
    assert titles == ["CPI rises 0.3% in October"]
    assert [it["title"] for it in out["future_dated"]] == ["FOMC holds rates steady"] and out["future_dated_count"] == 1
    fut = out["future_dated"][0]
    assert fut["published"] == "2026-11-05T19:00:00Z" and fut["ts"] == 1_793_905_200.0  # never re-dated
    assert out["high_24h"] == sum(1 for it in out["items"] if it["high"])
    archived = (cache / "news_archive.jsonl").read_text(encoding="utf-8")
    assert "fomc" not in archived and "cpi" in archived


def test_merge_never_re_dates_a_future_item():
    from kaiba.market_intel.aegis import news

    fut = {"title": "Future", "ts": FETCH + 86400, "published": "2026-11-04T04:00:00Z"}
    cur = {"title": "Now", "ts": FETCH - 60, "published": "2026-11-03T03:59:00Z"}
    out = news.merge([fut, cur], FETCH)
    assert out == [cur] and fut["ts"] == FETCH + 86400 and fut["published"] == "2026-11-04T04:00:00Z"
    assert all(it["ts"] != FETCH for it in out)


# ------------------------------------------------------------------ a run


def test_it_refuses_to_run_without_a_cache_directory(monkeypatch):
    """Without it, Aegis's base falls back to <repo>/runtime/terminal: the source tree. Stubs and a dead
    http make sure a broken refusal can never reach the network from a test (it once did, in a mutant)."""
    monkeypatch.delenv("AEGIS_TERMINAL_DATA", raising=False)
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})

    def dead():
        raise AssertionError("no http may be built when the run is refused")

    assert R.run(disk_usage=disk(50), http_factory=dead)["status"] == "refused" and calls == []


def test_due_collectors_run_once_and_a_second_run_finds_nothing_due(cache, monkeypatch):
    calls = synthetic(monkeypatch, {"calendar": {"events": []}, "news": {"items": []}, "etf_flows": {"rows": []}})
    first = R.run(disk_usage=disk(50))
    assert first["status"] == "ok" and sorted(first["ran"]) == ["calendar", "etf_flows", "news"]
    assert calls == ["calendar", "news", "etf_flows"]
    second = R.run(disk_usage=disk(50))
    assert second["ran"] == {} and sorted(second["not_due"]) == ["calendar", "etf_flows", "news"]
    assert {p.name for p in cache.glob("*.json")} == {"calendar.json", "news.json", "etf_flows.json"}


def test_a_failure_keeps_the_last_good_data_marked_stale(cache, monkeypatch):
    cache.mkdir(parents=True)
    (cache / "news.json").write_text(json.dumps({"key": "news", "ok": True, "stale": False, "fetched_ts": 1.0,
                                                  "fetched_at": "old", "data": {"items": ["kept"]}}))
    synthetic(monkeypatch, {"calendar": {}, "news": RuntimeError("feed down"), "etf_flows": {}})
    out = R.run(disk_usage=disk(50), only=["news"])
    doc = json.loads((cache / "news.json").read_text())
    assert out["ran"]["news"]["ok"] is False and out["ran"]["news"]["stale"] is True
    assert doc["data"] == {"items": ["kept"]} and doc["fetched_at"] == "old" and "feed down" in doc["error"]


def test_the_disk_halt_band_skips_the_whole_run(cache, monkeypatch):
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})
    out = R.run(disk_usage=disk(92.5))
    assert out["status"] == "skipped" and out["reason"].startswith("disk_halt") and calls == []
    assert R.run(disk_usage=disk(87.0))["status"] == "ok"  # the ACT band does not stop a few KB of caches


def test_a_cache_over_its_cap_skips_the_whole_run(cache, monkeypatch):
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})
    cache.mkdir(parents=True)
    (cache / "big.bin").write_bytes(b"x" * 64)
    monkeypatch.setattr(R, "CACHE_CAP_BYTES", 32)
    out = R.run(disk_usage=disk(50))
    assert out["status"] == "skipped" and out["reason"].startswith("cache_over_cap") and calls == []


def test_the_time_budget_stops_before_a_collector_would_start_past_it(cache, monkeypatch):
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})
    ticks = iter([0.0, 0.0, 100.0, 300.0, 300.0])
    out = R.run(disk_usage=disk(50), budget_s=240.0, clock=lambda: next(ticks))
    assert calls == ["calendar", "news"] and out["budget_stopped"] == ["etf_flows"]


def test_unset_external_keys_change_nothing(cache, monkeypatch):
    monkeypatch.delenv(R.EXTERNAL_ENV, raising=False)
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})
    out = R.run(disk_usage=disk(50))
    assert out["external"] == [] and calls == ["calendar", "news", "etf_flows"]


def test_an_external_key_is_never_run_and_its_mirror_never_overwritten(cache, monkeypatch):
    """DESKTOP-INTEL-SYNC: the owner's PC pushes etf_flows (farside refuses the box). A box-side
    failure doc must never replace the mirrored cache."""
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": RuntimeError("403 Forbidden")})
    cache.mkdir(parents=True)
    mirror = json.dumps({"key": "etf_flows", "ok": True, "fetched_ts": 1.0, "data": {"rows": ["from the PC"]}}).encode()
    (cache / "etf_flows.json").write_bytes(mirror)
    monkeypatch.setenv(R.EXTERNAL_ENV, " etf_flows , cot ")
    out = R.run(disk_usage=disk(50))
    assert out["status"] == "ok" and out["external"] == ["etf_flows"] and "etf_flows" not in out["ran"]
    assert calls == ["calendar", "news"] and (cache / "etf_flows.json").read_bytes() == mirror


@pytest.mark.parametrize("value", ["news", "etf_flows,news", "ETF_FLOWS", "smart_money", "etf_flows;cot"])
def test_a_name_that_may_not_be_external_refuses_the_whole_run(cache, monkeypatch, value):
    calls = synthetic(monkeypatch, {"calendar": {}, "news": {}, "etf_flows": {}})
    monkeypatch.setenv(R.EXTERNAL_ENV, value)
    out = R.run(disk_usage=disk(50))
    assert out["status"] == "refused" and R.EXTERNAL_ENV in out["reason"] and calls == []


@pytest.mark.parametrize("value, keys", [("", set()), (",", set()), ("markets", {"markets"}),
                                         ("etf_flows,cot,markets", {"etf_flows", "cot", "markets"})])
def test_external_keys_parse(value, keys):
    assert R.external_keys({R.EXTERNAL_ENV: value}) == (frozenset(keys), None)


def test_the_external_dropin_names_only_allowed_keys():
    conf = ROOT / "deploy" / "systemd" / "kaiba-market-intel.service.d" / "external-keys.conf"
    line = next(ln for ln in conf.read_text().splitlines() if ln.startswith("Environment="))
    value = line.split("=", 2)[2]
    assert R.external_keys({R.EXTERNAL_ENV: value}) == (R.EXTERNAL_ALLOWED, None)


def test_the_unit_never_loads_kaiba_credentials():
    unit = (ROOT / "deploy" / "systemd" / "kaiba-market-intel.service").read_text()
    directives = [ln.strip() for ln in unit.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert not any(d.startswith("EnvironmentFile") for d in directives)
    assert "Environment=AEGIS_TERMINAL_DATA=%h/kaiba/data/market-intel" in directives
    assert "MemoryMax=" in unit and "ionice -c3" in unit
