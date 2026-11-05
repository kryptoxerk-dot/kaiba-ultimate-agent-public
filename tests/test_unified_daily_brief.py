"""Offline macro brief contracts: no source collection or invented release truth."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from deploy import unified_daily_brief as daily

NOW = int(datetime(2026, 11, 3, 0, 0, tzinfo=UTC).timestamp() * 1000)


def snapshot(*, status="fresh"):
    return {
        "bridge": {"status": "available"},
        "collectors": [{"key": key, "title": key, "status": status, "fetched_at": "2026-11-03T00:00:00Z",
                        "error": None} for key in ("calendar", "news", "macro_fred")],
        "news": [
            {"id": "fed", "title": "Federal Reserve FOMC statement", "published_ms": NOW - 1000,
             "published_at": "2026-11-02T23:59:59Z", "url": "https://www.federalreserve.gov/newsevents/pressreleases.htm",
             "source": "Federal Reserve", "status": status},
            {"id": "old", "title": "Old CPI news", "published_ms": NOW - 2 * daily.DAY_MS, "status": status},
            {"id": "unknown", "title": "PCE without publication date", "published_ms": None, "status": status},
            {"id": "future", "title": "Future payroll news", "published_ms": NOW + 1000, "status": status},
            {"id": "nonmacro", "title": "Token launches", "published_ms": NOW - 1000, "status": status},
        ],
        "macro_events": [
            {"id": "cpi", "title": "US CPI", "event_ms": NOW + daily.DAY_MS,
             "event_at": "2026-11-04T00:00:00Z", "actual": None, "forecast": "0.2%", "previous": "0.3%",
             "source": "Fixture calendar", "status": status},
            {"id": "next_fomc", "title": "FOMC rate decision", "event_ms": NOW + 40 * daily.DAY_MS,
             "event_at": "2026-12-13T00:00:00Z", "actual": None, "status": status},
            {"id": "far_fomc", "title": "FOMC rate decision", "event_ms": NOW + 95 * daily.DAY_MS,
             "status": status},
            {"id": "old", "title": "PCE", "event_ms": NOW - 2 * daily.DAY_MS, "status": status},
            {"id": "funding", "title": "Binance funding", "event_ms": NOW + 1000, "status": status},
        ],
        "macro_series": [
            {"id": "DFF", "name": "Federal funds rate", "value": 3.5, "units": "%",
             "report_date": "2026-11-02", "status": status},
            {"id": "CPIAUCSL", "name": "Consumer price index", "value": 2.0, "units": "% YoY",
             "report_date": "2026-09-01", "status": status},
            {"id": "PCEPILFE", "name": "Core PCE", "value": 2.2, "units": "% YoY",
             "report_date": "2026-09-01", "status": status},
            {"id": "PAYEMS", "name": "Nonfarm payrolls", "value": 100, "units": "thousand",
             "report_date": "2026-10-01", "status": status},
        ],
    }


def test_brief_windows_keep_current_macro_and_next_fomc_without_inventing_actual():
    report = daily.compile_brief(snapshot(), now_ms=NOW)
    assert report["status"] == "fresh"
    assert [row["id"] for row in report["news"]] == ["fed"]
    assert [row["id"] for row in report["events"]] == ["cpi", "next_fomc"]
    assert report["events"][0]["actual"] is None
    assert report["events"][0]["forecast"] == "0.2%"
    assert report["coverage"]["undated_macro_news"] == 1
    assert report["categories"]["PCE"]["series_ids"] == ["PCEPILFE"]
    assert report["categories"]["JOBS"]["series_ids"] == ["PAYEMS"]
    assert report["categories"]["CPI"]["series_ids"] == ["CPIAUCSL"]
    assert report["series"][1]["report_date"] == "2026-09-01"


def test_new_compilation_preserves_old_source_status_and_observation_dates():
    report = daily.compile_brief(snapshot(status="stale"), now_ms=NOW)
    assert report["compiled_at"] == "2026-11-03T00:00:00Z"
    assert report["status"] == "stale"
    assert report["news"][0]["status"] == "stale"
    assert report["series"][1]["report_date"] == "2026-09-01"
    assert report["source_health"][0]["status"] == "stale"
    assert report["categories"]["CPI"]["status"] == "stale"
    assert report["coverage"]["current_news"] == 0
    assert report["coverage"]["news_in_window"] == 1


def test_missing_sources_and_empty_topics_are_unknown_not_zero_news_claim():
    report = daily.compile_brief({}, now_ms=NOW)
    assert report["status"] == "unavailable"
    assert all(category["coverage"] == "unavailable" for category in report["categories"].values())
    rendered = daily.render_markdown(report)
    assert "No current cached macro headlines" in rendered
    assert "does not establish that no macro news occurred" in rendered
    assert "unavailable" in rendered


def test_local_brief_date_uses_jakarta_and_scheduled_time_preserves_utc():
    now = int(datetime(2026, 11, 2, 23, 0, tzinfo=UTC).timestamp() * 1000)
    report = daily.compile_brief(snapshot(), now_ms=now)
    assert report["brief_date"] == "2026-11-03"
    assert report["compiled_at"] == "2026-11-02T23:00:00Z"
    rendered = daily.render_markdown(report)
    assert "2026-11-04 07:00 WIB (2026-11-04T00:00:00Z)" in rendered
    assert "Actual: unknown" in rendered


def test_partial_cache_and_no_current_headlines_cannot_be_reported_fully_fresh():
    data = snapshot()
    data["news"] = []
    assert daily.compile_brief(data, now_ms=NOW)["status"] == "partial"
    data = snapshot()
    data["collectors"][0]["status"] = "error"
    data["collectors"][0]["error"] = "source failed"
    report = daily.compile_brief(data, now_ms=NOW)
    assert report["status"] == "partial"
    assert report["source_health"][0]["error"] == "source failed"


def test_full_400_item_source_finds_macro_behind_crypto_headlines():
    data = snapshot()
    data["news"] = [{"id": f"crypto-{index}", "title": "Token launches", "published_ms": NOW - 500,
                     "status": "fresh"} for index in range(399)] + [data["news"][0]]
    report = daily.compile_brief(data, now_ms=NOW)
    assert [row["id"] for row in report["news"]] == ["fed"]
    assert report["categories"]["FED"]["status"] == "fresh"


def test_fresh_collection_and_headline_cannot_renew_stale_economic_observations():
    data = snapshot()
    data["macro_series"][1]["status"] = "stale"
    report = daily.compile_brief(data, now_ms=NOW)
    assert report["status"] == "partial"
    assert report["categories"]["CPI"]["status"] == "partial"
    assert report["categories"]["PCE"]["status"] == "fresh"
    data["macro_events"] = [row for row in data["macro_events"] if row["id"] != "cpi"]
    report = daily.compile_brief(data, now_ms=NOW)
    assert report["categories"]["CPI"]["status"] == "stale"


def test_missing_category_cannot_be_reported_fully_fresh():
    data = snapshot()
    data["macro_series"] = [row for row in data["macro_series"] if row["id"] != "PCEPILFE"]
    report = daily.compile_brief(data, now_ms=NOW)
    assert report["status"] == "partial"
    assert report["categories"]["PCE"]["status"] == "unavailable"


def test_writes_valid_dated_and_latest_artifacts_only(tmp_path):
    report = daily.compile_brief(snapshot(), now_ms=NOW)
    paths = daily.write_brief(report, tmp_path)
    assert set(path.name for path in tmp_path.iterdir()) == {
        "2026-11-03.json", "2026-11-03.md", "latest.json", "latest.md",
    }
    assert json.loads(paths["latest_json"].read_text(encoding="utf-8")) == report
    assert paths["latest_json"].read_bytes() == paths["dated_json"].read_bytes()
    assert paths["latest_markdown"].read_bytes() == paths["dated_markdown"].read_bytes()
    assert "https://www.federalreserve.gov/" in paths["latest_markdown"].read_text(encoding="utf-8")


def test_atomic_replace_failure_preserves_previous_complete_file_and_removes_tmp(tmp_path, monkeypatch):
    target = tmp_path / "latest.json"
    target.write_bytes(b"previous complete report")

    def refuse_replace(*_args):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(daily.os, "replace", refuse_replace)
    with pytest.raises(OSError, match="interrupted"):
        daily._atomic_write(target, b"new report")
    assert target.read_bytes() == b"previous complete report"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("day", ["../../bad", "2026-11-40", "x", None])
def test_output_date_traversal_and_invalid_dates_refused_before_writes(tmp_path, day):
    report = daily.compile_brief({}, now_ms=NOW)
    report["brief_date"] = day
    with pytest.raises((TypeError, ValueError)):
        daily.write_brief(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_oversize_report_refused_before_artifacts(tmp_path):
    report = daily.compile_brief({}, now_ms=NOW)
    report["summary"] = "x" * daily.MAX_OUTPUT_BYTES
    with pytest.raises(ValueError, match="size limit"):
        daily.write_brief(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_cli_reads_missing_cache_without_network_and_writes_truthful_report(tmp_path, monkeypatch, capsys):
    import socket

    def refuse_network(*_args, **_kwargs):
        raise AssertionError("daily compilation must not access a network")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    output = tmp_path / "briefs"
    result = daily.main(["--cache-root", str(tmp_path / "missing-cache"), "--output-dir", str(output)])
    assert result == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["provider_calls"] == 0
    assert receipt["collection"] == "not_run"
    assert receipt["status"] == "unavailable"
    report = json.loads((output / "latest.json").read_text(encoding="utf-8"))
    assert report["status"] == "unavailable"
    assert report["news"] == []


def test_timer_is_daily_jakarta_equivalent_and_service_is_cache_only():
    root = Path(__file__).resolve().parents[1]
    service = (root / "deploy/systemd/user/kaiba-daily-brief.service").read_text(encoding="utf-8")
    timer = (root / "deploy/systemd/user/kaiba-daily-brief.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 00:00:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "PrivateNetwork=yes" in service
    assert "TimeoutStartSec=60" in service
    assert "-m deploy.unified_daily_brief" in service
    assert "ReadWritePaths=%h/kaiba/data/briefings" in service
