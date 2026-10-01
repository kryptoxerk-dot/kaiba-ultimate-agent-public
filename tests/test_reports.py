"""Offline checks for the six-file Hermes report publisher."""

import json

from kaiba.cli import reports


def test_write_reports_replaces_all_six_files_atomically(monkeypatch, tmp_path):
    payload = {name: {"name": name, "value": 1} for name in reports.REPORT_NAMES}
    monkeypatch.setattr(reports, "snapshot", lambda: payload)

    written = reports.write_reports(tmp_path)

    assert [path.name for path in written] == [f"{name}.json" for name in reports.REPORT_NAMES]
    assert not list(tmp_path.glob(".*.tmp"))
    for name in reports.REPORT_NAMES:
        path = tmp_path / f"{name}.json"
        assert json.loads(path.read_text(encoding="utf-8")) == payload[name]


def test_reports_dir_prefers_explicit_environment(monkeypatch, tmp_path):
    configured = tmp_path / "reports"
    monkeypatch.setenv("KAIBA_REPORTS_DIR", str(configured))
    assert reports.reports_dir() == configured
