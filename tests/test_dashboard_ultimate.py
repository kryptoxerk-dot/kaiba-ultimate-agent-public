"""Unified workspace integration boundaries; local fixtures, no providers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dashboard import ultimate
from dashboard.app import create_app
from kaiba.core.config import get_settings


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", "workspace-test-password")
    monkeypatch.setenv("KAIBA_DAILY_BRIEF_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(ultimate, "_cached", None)
    monkeypatch.setattr(ultimate, "_cached_at", 0)
    monkeypatch.setattr(ultimate, "build_kaiba_snapshot", lambda: {"status": "unavailable", "metrics": {}})
    monkeypatch.setattr(ultimate, "build_intelligence", lambda: {"bridge": {"status": "unavailable"}})
    monkeypatch.setattr(ultimate, "build_pulse", lambda: {"status": "unavailable", "tokens": [], "wallets": [], "chains": []})
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_both_workspace_routes_require_existing_session(workspace):
    response = workspace.get("/ultimate", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    response = workspace.get("/api/ultimate", follow_redirects=False)
    assert response.status_code == 401
    assert "kaiba" not in response.json()


def test_signed_in_operator_sees_workspace_and_honest_api(workspace):
    response = workspace.post("/login", data={"password": "workspace-test-password"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/ultimate"
    html = workspace.get("/ultimate")
    assert html.status_code == 200
    for label in ("Market overview", "Wallet &amp; whale activity", "Institutional flows", "Daily macro brief", "Aegis perps"):
        assert label in html.text
    assert "no-store" in html.headers["cache-control"]
    assert html.headers["x-frame-options"] == "DENY"
    snapshot = workspace.get("/api/ultimate")
    assert snapshot.status_code == 200
    assert snapshot.json()["kaiba"]["status"] == "unavailable"
    assert snapshot.json()["refresh_interval_s"] == 30


def test_workspace_adds_no_write_routes(workspace):
    routes = [r for r in workspace.app.routes if "ultimate" in r.path]
    assert {r.path for r in routes} == {"/ultimate", "/api/ultimate"}
    assert all(r.methods <= {"GET", "HEAD"} for r in routes)
    assert workspace.post("/api/ultimate").status_code in (401, 405)


def test_snapshot_single_flight_deduplicates_parallel_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(ultimate, "_cached", None)
    monkeypatch.setattr(ultimate, "build_kaiba_snapshot", lambda: calls.append("ledger") or {"status": "available"})
    monkeypatch.setattr(ultimate, "build_intelligence", lambda: calls.append("intel") or {"bridge": {}})
    monkeypatch.setattr(ultimate, "build_pulse", lambda: {"status": "unavailable"})
    monkeypatch.setattr(ultimate, "_brief", lambda: {"status": "unavailable"})
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: ultimate.snapshot(), range(12)))
    assert calls == ["ledger", "intel"]
    assert all(row is rows[0] for row in rows)
    monkeypatch.setattr(ultimate, "_cached_at", -100)
    ultimate.snapshot()
    assert calls == ["ledger", "intel", "ledger", "intel"]


@pytest.mark.parametrize("value", [None, [], {"schema_version": 2}, {"schema_version": 1, "status": "stale"}])
def test_brief_schema_and_missingness(tmp_path, monkeypatch, value):
    monkeypatch.setenv("KAIBA_DAILY_BRIEF_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(json.dumps(value), encoding="utf-8")
    expected = value if isinstance(value, dict) and value.get("schema_version") == 1 else {"status": "unavailable"}
    assert ultimate._brief() == expected


def test_brief_read_has_size_and_depth_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_DAILY_BRIEF_DIR", str(tmp_path))
    path = tmp_path / "latest.json"
    path.write_text(" " * 524_289, encoding="utf-8")
    assert ultimate._brief() == {"status": "unavailable"}
    path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
    assert ultimate._brief() == {"status": "unavailable"}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pass@example.test/", "//example.test/"])
def test_terminal_link_refuses_unsafe_urls(monkeypatch, url):
    monkeypatch.setenv("KAIBA_AEGIS_TERMINAL_URL", url)
    assert ultimate._terminal_url() is None


def test_terminal_link_does_not_publish_query_credentials(monkeypatch):
    monkeypatch.setenv("KAIBA_AEGIS_TERMINAL_URL", "https://aegis.example.test/terminal?token=secret#state")
    assert ultimate._terminal_url() == "https://aegis.example.test/terminal"
