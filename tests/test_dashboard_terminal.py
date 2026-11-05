"""Codex visual preview: isolated data, truthful math, safe rendering and no actions."""

from __future__ import annotations

import sqlite3
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from dashboard import terminal_demo


@pytest.fixture
def preview():
    with TestClient(terminal_demo.app) as client:
        yield client


def test_demo_cannot_expose_itself_to_a_non_loopback_client():
    with TestClient(terminal_demo.app, client=("203.0.113.8", 50000)) as client:
        response = client.get("/")
    assert response.status_code == 403
    assert response.json() == {"error": "loopback_only"}


def test_preview_never_reads_a_database_or_production_settings(preview, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Isolated preview called a production dependency")

    from kaiba.core import config, db

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(db, "get_conn", forbidden)
    monkeypatch.setattr(config, "get_settings", forbidden)
    for path in ("/", "/terminal", "/api/terminal", "/static/terminal.js"):
        response = preview.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_no_preview_route_accepts_an_order_or_service_action(preview):
    for path in ("/", "/api/terminal", "/control/paper-buy", "/control/command", "/control/exit"):
        assert preview.post(path, json={"amount": 1}).status_code in {404, 405}
    assert all(not ({"POST", "PUT", "PATCH", "DELETE"} & getattr(route, "methods", set()))
               for route in terminal_demo.app.routes)


def test_every_preview_is_clearly_labeled_and_has_local_assets(preview):
    html = preview.get("/").text
    for text in ("Demo workspace", "Illustrative feed", "Simulated execution", "no funds move",
                 "Fees & actual slippage", "Unavailable", "ILLUSTRATIVE MARKET DATA"):
        assert text in html
    for path in ("/static/terminal.css", "/static/terminal.js", "/static/terminal_connections.js", "/static/kaiba-mark.svg"):
        assert preview.get(path).status_code == 200
    class Assets(HTMLParser):
        sources = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag in {"script", "img", "iframe"}:
                self.sources.append(attributes.get("src", ""))
            if tag == "link":
                self.sources.append(attributes.get("href", ""))

    parser = Assets()
    parser.feed(html)
    assert all(not value or value.startswith("/static/") for value in parser.sources)
    assert 'href="https://gmgn.ai/ai"' in html  # Explicit external action, never auto-loaded.
    assert "Demo session" in html
    assert "No account is connected in this design workspace" in html


def test_synthetic_market_data_has_consistent_ohlc_prices_and_24h_change():
    data = terminal_demo.demo_snapshot()
    assert data["demo"] is True
    assert data["mode"] == "demo"
    assert len(data["tokens"]) == 7
    for token in data["tokens"]:
        candles = token["candles"]
        assert len(candles) == 384
        assert candles[-1]["close"] == pytest.approx(token["price"])
        assert (candles[-1]["close"] / candles[-97]["close"] - 1) * 100 == pytest.approx(token["change"])
        assert all(c["low"] <= min(c["open"], c["close"]) <= max(c["open"], c["close"]) <= c["high"]
                   and c["low"] > 0 and c["volume"] > 0 for c in candles)
        assert all(b["time"] - a["time"] == 900_000 for a, b in zip(candles, candles[1:], strict=False))
        assert token["address"] is None  # never invent a contract address


def test_demo_pnl_and_valuation_are_arithmetically_consistent():
    data = terminal_demo.demo_snapshot()
    for position in data["positions"]:
        assert position["pnl"] == pytest.approx((position["mark"] - position["entry"]) * position["qty"])
        token = next(t for t in data["tokens"] if t["id"] == position["token_id"])
        assert token["price"] == position["mark"]
    cash = sum(b["qty"] * b["price"] for b in data["balances"])
    holdings = sum(p["qty"] * p["mark"] for p in data["positions"])
    assert cash + holdings == pytest.approx(622.103)


def test_sessions_receive_independent_seed_objects():
    first = terminal_demo.demo_snapshot()
    first["balances"][0]["qty"] = -999
    first["positions"].clear()
    second = terminal_demo.demo_snapshot()
    assert second["balances"][0]["qty"] == 1.245
    assert len(second["positions"]) == 3


def test_untrusted_labels_cannot_break_out_of_the_seed_json(preview, monkeypatch):
    payload = terminal_demo.demo_snapshot()
    payload["tokens"][0]["name"] = "</script><script>alert('xss')</script>"
    monkeypatch.setattr(terminal_demo, "demo_snapshot", lambda: payload)
    html = preview.get("/").text
    assert "</script><script>alert" not in html
    assert "\\u003c/script\\u003e" in html
