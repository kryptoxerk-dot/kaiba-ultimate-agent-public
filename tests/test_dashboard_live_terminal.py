"""The production trading terminal (2026-10-20): dashboard/intel.py + dashboard/terminal_live.py.

What these guard, beyond "the page renders":

* nothing is invented: an unknown token or wallet renders "no record", a price without a source
  is a dash, and the stream drops a link whose scheme is not http(s);
* the book is marked with protection's own quote memory, and the P&L is mark over entry;
* the lane control cannot arm a lane without the typed confirmation, cannot go above the
  envelope ceiling, and writes exactly one lane;
* the buy button goes through ``engine.submit_agent_intent`` (every gate) and refuses a bad
  address; when the manual lane is live it needs ``BUY LIVE`` typed;
* a sell writes the close intent protection drains (``Watchdog._classify``), with the percent.
"""

from __future__ import annotations

import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from dashboard import auth, intel, terminal_live, trading, wallet_export
from dashboard.app import create_app
from kaiba.core.config import DEFAULT_RISK_PATH, get_settings, load_risk
from kaiba.core.schemas import now_ms

PASSWORD = "terminal-test-password"
TOKEN = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
WALLET = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


@pytest.fixture
def term(tmp_db, tmp_path, monkeypatch):
    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    get_settings.cache_clear()
    trading.clear_caches()
    intel.clear_caches()
    client = TestClient(create_app())
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    client.conn = tmp_db  # type: ignore[attr-defined]
    client.risk_path = risk_path  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        intel.clear_caches()
        trading.clear_caches()
        get_settings.cache_clear()


def csrf(client: TestClient) -> str:
    match = re.search(r'name="csrf-token" content="([^"]*)"', client.get("/autopilot").text)
    assert match and match.group(1)
    return match.group(1)


def seed_token(conn, *, price="0.0000022241", grade="B"):
    now = now_ms()
    conn.execute("INSERT INTO tokens (chain, address, symbol, name, decimals, creator, created_ms, launchpad, first_seen_ms) "
                 "VALUES ('sol', ?, 'KAIBA', 'Kaiba Test', 6, ?, ?, 'pump.fun', ?)", (TOKEN, WALLET, now - 600_000, now))
    dossier = {"price_usd": {"value": price, "basis": "provider_reported",
                             "receipt": {"provider": "gmgn", "observed_at_ms": now}},
               "liquidity_usd": {"value": "15000", "basis": "derived", "receipt": {"provider": "gmgn"}},
               "holder_count": {"value": "321", "basis": "provider_reported", "receipt": None},
               "top10_pct": {"value": None, "basis": "unavailable", "receipt": None},
               "mint_authority_revoked": True, "freeze_authority_revoked": True, "can_sell": True,
               "blockers": [], "warnings": ["low_liquidity"], "unknowns": ["insider_pct"]}
    conn.execute("INSERT INTO token_dossiers (chain, address, built_at_ms, score, grade, blockers_json, warnings_json, "
                 "unknowns_json, dossier_json) VALUES ('sol', ?, ?, 62.8, ?, '[]', '[]', '[]', ?)",
                 (TOKEN, now, grade, json.dumps(dossier)))
    conn.execute("INSERT INTO creators (chain, address, launches, graduated, rugged, updated_ms) VALUES ('sol', ?, 12, 3, 4, ?)",
                 (WALLET, now))


def seed_position(conn, *, pid="pos_t1", mode="shadow", entry="1.0", mark="1.25"):
    now = now_ms()
    conn.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, cost_native, "
                 "proceeds_native, realized_native, entry_price_usd) VALUES (?, 'sol', ?, 'launch-snipe', ?, ?, '1000', "
                 "'1000', '100000000', '0', '0', ?)", (pid, TOKEN, mode, now - 60_000, entry))
    record = {"price": {"price_usd": mark, "source": "pump-curve:3cR34RWc", "settlement": "curve", "observed_ms": now}}
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?)",
                 (f"watchdog.quote:{pid}:sol:{TOKEN}", json.dumps(record), now))
    for i, px in enumerate(("1.0", "1.1", "1.25")):
        conn.execute("INSERT INTO position_marks (position_id, ts_ms, price_usd) VALUES (?, ?, ?)", (pid, now - 50_000 + i, px))


# --------------------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------------------


def test_prices_read_the_way_traders_read_them():
    assert terminal_live.fmt_px("0.0000022241") == "0.0₅2224"
    assert terminal_live.fmt_px("0.00123") == "0.00123"
    assert terminal_live.fmt_px("1.5") == "1.5000" and terminal_live.fmt_px("2500") == "2,500"
    assert terminal_live.fmt_px(None) == "—" and terminal_live.fmt_px("nan") == "—"
    assert terminal_live.fmt_usd(2_224_000) == "$2.22M" and terminal_live.fmt_usd(None) == "—"
    assert terminal_live.fmt_ret(0.25) == "+25.0%" and terminal_live.fmt_pctx(0.59) == "59.0%"
    assert terminal_live.fmt_pctx(59) == "59.0%"


def test_addresses_are_validated_per_chain():
    assert intel.clean_address("sol", TOKEN) == TOKEN
    assert intel.clean_address("bsc", "0x" + "AB" * 20) == "0x" + "ab" * 20
    assert intel.clean_address("sol", "0x" + "ab" * 20) is None
    assert intel.clean_address("sol", "<script>") is None and intel.clean_address("eth", TOKEN) is None
    assert intel.safe_url("javascript:alert(1)") is None and intel.safe_url("https://x.y/z") == "https://x.y/z"


# --------------------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/terminal", "/alpha", "/autopilot", "/intel/wallets", f"/t/sol/{TOKEN}",
                                  f"/w/sol/{WALLET}", "/terminal/stream", "/terminal/book", "/terminal/ticker",
                                  f"/terminal/token/sol/{TOKEN}", "/go?q=%24NOTHING"])
def test_every_terminal_page_renders_on_an_empty_database(term, path):
    response = term.get(path)
    assert response.status_code == 200, response.text[:400]


def test_pages_need_a_session(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    anon = TestClient(create_app())
    for path in ("/terminal", f"/t/sol/{TOKEN}", "/autopilot", "/api/terminal/stream"):
        assert anon.get(path, follow_redirects=False).status_code in (303, 401, 403)


def test_a_bad_address_is_a_404_not_a_query(term):
    assert term.get("/t/sol/not-an-address").status_code == 404
    assert term.get(f"/t/eth/{TOKEN}").status_code == 404
    assert term.get("/w/bsc/0x123").status_code == 404


def test_an_unknown_token_says_so_and_invents_no_price(term):
    html = term.get(f"/t/sol/{TOKEN}").text
    assert "No dossier yet." in html and "No price history we can stand behind yet" in html
    assert "big faint\">—" in html, "no price is shown as a dash, never a plausible number"


def test_a_known_token_shows_its_dossier_with_provenance(term):
    seed_token(term.conn)
    seed_position(term.conn)
    html = term.get(f"/t/sol/{TOKEN}").text
    assert "KAIBA" in html and "$0.0₅2224" in html and "low_liquidity" in html and "? insider_pct" in html
    assert "provider_reported · gmgn" in html, "every number says where it came from"
    assert "<span>Launches</span><b>12</b>" in html and "<span>Rugged</span><b class=\"neg\">4</b>" in html
    series = intel.token_profile("sol", TOKEN)["series"]
    assert series["source"] == "protection marks" and [p[1] for p in series["points"]] == [1.0, 1.1, 1.25]


def test_the_book_is_marked_with_protections_own_quote(term):
    seed_token(term.conn)
    seed_position(term.conn, entry="1.0", mark="1.25")
    book = intel.book()
    [p] = book["open"]
    assert p["ret"] == pytest.approx(0.25) and p["mark"]["source"] == "pump-curve:3cR34RWc" and p["paper"]
    html = term.get("/terminal/book").text
    assert "+25.0%" in html and "KAIBA" in html


def test_the_stream_merges_sources_newest_first_and_drops_unsafe_links(term):
    now = now_ms()
    seed_token(term.conn)
    term.conn.execute("INSERT INTO triage_decisions (ts_ms, chain, token, verdict, score, reasons_json, launchpad) "
                      "VALUES (?, 'sol', ?, 'promote', 71.5, '[\"fresh\"]', 'pump.fun')", (now - 5_000, TOKEN))
    term.conn.execute("INSERT INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, first_seen_ms, "
                      "confidence, payload_json) VALUES ('k1', 'crtsh', 'cert_subdomain', 'claim.example.xyz', "
                      "'claim subdomain', 'javascript:alert(1)', NULL, ?, 0.6, '{}')", (now - 1_000,))
    items = intel.alpha_stream(20)
    assert [i["kind"] for i in items][:2] == ["claim", "launch"]
    assert items[0]["url"] is None, "a javascript: link never becomes an href"
    assert items[1]["symbol"] == "KAIBA" and items[1]["linkable"]
    claims = intel.alpha_stream(20, kinds=("claim",))
    assert [i["kind"] for i in claims] == ["claim"]


def test_search_routes_a_known_token_and_a_symbol(term):
    seed_token(term.conn)
    response = term.get(f"/go?q={TOKEN}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/t/sol/{TOKEN}"
    html = term.get("/go?q=%24KAIBA").text
    assert f"/t/sol/{TOKEN}" in html


# --------------------------------------------------------------------------------------
# controls
# --------------------------------------------------------------------------------------


def test_lane_mode_needs_a_session_and_csrf(term):
    assert term.post("/control/lane-mode", data={"lane": "manual", "mode": "off"}).status_code == 403


def test_paper_and_off_are_one_click_and_change_one_lane(term):
    token = csrf(term)
    before = load_risk(term.risk_path)
    response = term.post("/control/lane-mode", data={"lane": "manual", "mode": "off", "_csrf": token})
    assert response.status_code == 200, response.text
    after = load_risk(term.risk_path)
    assert after.lanes[next(k for k in after.lanes if str(getattr(k, "value", k)) == "manual")].mode.value == "off"
    changed = [str(getattr(k, "value", k)) for k in after.lanes
               if after.lanes[k].mode != before.lanes[k].mode]
    assert changed == ["manual"]


def test_arming_live_needs_the_lane_typed_back(term):
    token = csrf(term)
    refused = term.post("/control/lane-mode", data={"lane": "manual", "mode": "live", "_csrf": token})
    assert refused.status_code == 400 and "ARM manual" in refused.text
    cfg = load_risk(term.risk_path)
    ceiling = str(getattr(cfg.bounds.max_lane_mode, "value", cfg.bounds.max_lane_mode))
    armed = term.post("/control/lane-mode", data={"lane": "manual", "mode": "live", "confirm": "ARM manual",
                                                  "_csrf": token})
    if ceiling == "live":
        assert armed.status_code == 200 and armed.json()["mode"] == "live"
    else:
        assert armed.status_code == 400 and "max_lane_mode" in armed.text


def test_a_mode_above_the_envelope_is_refused_not_clamped(term, monkeypatch):
    from kaiba.core.schemas import LaneMode

    cfg = load_risk(term.risk_path)
    cfg.bounds.max_lane_mode = LaneMode.SHADOW
    from dashboard import app as A

    monkeypatch.setattr(A, "_current_risk", lambda: cfg)
    monkeypatch.setattr(A, "_save_risk", lambda c: pytest.fail("nothing may be written"))
    response = term.post("/control/lane-mode", data={"lane": "manual", "mode": "live", "confirm": "ARM manual",
                                                     "_csrf": csrf(term)})
    assert response.status_code == 400 and "envelope" in response.text


def test_the_buy_button_goes_through_every_gate(term, monkeypatch):
    from kaiba.execution import engine

    calls = []
    monkeypatch.setattr(engine, "submit_agent_intent", lambda chain, token, **kw: calls.append((chain, token, kw)) or {
        "ok": True, "action": "enter", "mode": "shadow", "size_base_units": 1000, "blockers": []})
    monkeypatch.setattr(intel, "manual_lane_mode", lambda: "shadow")
    token = csrf(term)
    assert term.post("/control/paper-buy", data={"chain": "sol", "token": "nope", "_csrf": token}).status_code == 400
    response = term.post("/control/paper-buy", data={"chain": "sol", "token": TOKEN, "_csrf": token})
    assert response.status_code == 200 and response.json()["mode"] == "shadow"
    assert calls == [("sol", TOKEN, {"thesis": "operator: terminal buy"})]


def test_a_live_manual_lane_needs_buy_live_typed(term, monkeypatch):
    from kaiba.execution import engine

    monkeypatch.setattr(engine, "submit_agent_intent", lambda *a, **k: pytest.fail("must not reach the engine"))
    monkeypatch.setattr(intel, "manual_lane_mode", lambda: "live")
    response = term.post("/control/paper-buy", data={"chain": "sol", "token": TOKEN, "_csrf": csrf(term)})
    assert response.status_code == 400 and "BUY LIVE" in response.text


def test_a_sell_writes_the_intent_protection_drains(term):
    seed_token(term.conn)
    seed_position(term.conn)
    token = csrf(term)
    assert term.post("/control/exit", data={"position_id": "pos_t1", "pct": 33, "_csrf": token}).status_code == 400
    assert term.post("/control/exit", data={"position_id": "pos_nope", "pct": 100, "_csrf": token}).status_code == 404
    response = term.post("/control/exit", data={"position_id": "pos_t1", "pct": 50, "_csrf": token})
    assert response.status_code == 200
    from kaiba.core import events as ev
    from kaiba.execution import watchdog as wd

    [intent] = [e for e in ev.recent(limit=50, conn=term.conn)
                if isinstance(e.payload, dict) and e.payload.get("intent") == "close_position"]
    assert intent.payload["pct"] == 50 and intent.payload["requested_by"] == "operator"
    dog = wd.Watchdog(term.conn)
    exits: dict = {}
    dog._classify(intent, exits, {})
    assert exits["pos_t1"].pct == 50, "protection reads it as a 50% exit request"


def test_csrf_meta_is_present_on_terminal_pages(term):
    html = term.get("/terminal").text
    assert re.search(r'name="csrf-token" content="[^"]+"', html)
    assert auth.CSRF_HEADER == "x-kaiba-csrf"


# --------------------------------------------------------------------------------------
# the Hermes agent (2026-10-20)
# --------------------------------------------------------------------------------------

SESSION = {
    "id": "20261020_120000_abcdef", "source": "telegram", "title": "Kaiba chat", "model": "claude-opus-5-5",
    "system_prompt": "SECRET SYSTEM PROMPT", "started_at": 1792467000.0, "last_activity_at": 1792468000.0,
    "message_count": 4, "tool_call_count": 1, "estimated_cost_usd": 0.12, "tool_names": ["kaiba_book"],
    "messages": [
        {"role": "user", "content": "how is the book?", "timestamp": 1792467100.0},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "kaiba_book"}}], "timestamp": 1792467101.0},
        {"role": "tool", "tool_name": "kaiba_book", "content": "x" * 2000, "timestamp": 1792467102.0},
        {"role": "assistant", "content": "All fine. <script>alert(1)</script> **10** open.", "timestamp": 1792467103.0},
    ],
}


def test_a_session_is_condensed_without_its_system_prompt_and_with_html_disabled():
    from dashboard import agent_view

    out = agent_view.condense(SESSION)
    assert "SECRET SYSTEM PROMPT" not in json.dumps(out)
    roles = [m["role"] for m in out["thread"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert out["thread"][1]["calls"] == ["kaiba_book"]
    assert len(out["thread"][2]["text"]) == agent_view.MAX_TOOL_TEXT
    html = out["thread"][3]["html"]
    assert "<script>" not in html and "<strong>10</strong>" in html
    assert out["final"]["text"].startswith("All fine") and out["started_ms"] == 1792467000000


def test_an_export_goes_through_a_private_temp_dir_that_is_removed(monkeypatch):
    from pathlib import Path

    from dashboard import agent_view

    seen = {}

    def fake(args, **kw):
        out = Path(args[-1])
        seen["dir"] = out.parent
        seen["args"] = args
        out.write_text(json.dumps(SESSION) + "\n", encoding="utf-8")
        return 0, "Exported 1 session"

    monkeypatch.setattr(agent_view, "_hermes", fake)
    rows = agent_view._export(["--source", "cron", "--newer-than", "24h"])
    assert rows and rows[0]["id"] == SESSION["id"]
    assert seen["args"][:4] == ["sessions", "export", "--format", "jsonl"] and "--no-redact" not in seen["args"]
    assert not seen["dir"].exists(), "the exported transcript does not outlive the call"


def test_posting_to_telegram_passes_the_text_on_stdin(monkeypatch):
    from dashboard import agent_view

    calls = []
    monkeypatch.setattr(agent_view, "available", lambda: (True, "ok"))
    monkeypatch.setattr(agent_view, "_hermes", lambda args, **kw: calls.append((args, kw.get("stdin"))) or (0, "{}"))
    assert agent_view.send_telegram("  hello; rm -rf /  ")["chars"] == len("hello; rm -rf /")
    assert calls == [(["send", "-t", "telegram", "-f", "-", "--json"], "hello; rm -rf /")]
    with pytest.raises(agent_view.AgentError):
        agent_view.send_telegram("   ")
    with pytest.raises(agent_view.AgentError):
        agent_view.send_telegram("x" * (agent_view.MAX_SEND_CHARS + 1))


def test_the_agent_page_renders_without_hermes(term):
    html = term.get("/agent").text
    assert "Talk to the agent" in html and "data-chat" in html
    for path in ("/agent/telegram", "/agent/background", "/agent/status"):
        assert term.get(path).status_code == 200


def test_pausing_the_agent_needs_it_typed(term, monkeypatch):
    from dashboard import agent_view

    calls = []
    monkeypatch.setattr(agent_view, "set_paused", lambda paused: calls.append(paused) or {"ok": True, "paused": paused})
    token = csrf(term)
    refused = term.post("/control/agent-pause", data={"paused": "1", "confirm": "yes", "_csrf": token})
    assert refused.status_code == 400 and calls == []
    assert term.post("/control/agent-pause", data={"paused": "1", "confirm": "PAUSE AGENT", "_csrf": token}).status_code == 200
    assert term.post("/control/agent-pause", data={"paused": "0", "_csrf": token}).status_code == 200
    assert calls == [True, False]


def test_terminal_pages_carry_the_agent_dock_and_the_token_panel_can_ask(term):
    seed_token(term.conn)
    assert "data-dock" in term.get("/terminal").text
    assert "data-ask=" in term.get(f"/terminal/token/sol/{TOKEN}").text
    assert "data-dock" not in term.get("/agent").text, "the agent page has its own chat"


# --------------------------------------------------------------------------------------
# privacy of an internet-facing console (2026-10-20)
# --------------------------------------------------------------------------------------


def test_every_response_carries_the_privacy_headers(term):
    for path in ("/login", "/terminal", "/api/terminal/stream", "/healthz"):
        h = term.get(path).headers
        assert h["referrer-policy"] == "no-referrer", path
        assert h["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
        assert "noindex" in h["x-robots-tag"]
    assert term.get("/static/live.css").headers.get("cache-control") != "no-store", "static files may cache"


def test_a_live_panel_without_a_session_is_a_401_not_the_login_page(tmp_db, monkeypatch):
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    anon = TestClient(create_app())
    for path in ("/terminal/book", "/terminal/stream", "/agent/telegram", f"/terminal/token/sol/{TOKEN}"):
        assert anon.get(path, follow_redirects=False).status_code == 401, path
    assert anon.get("/terminal", follow_redirects=False).status_code == 303


def test_signing_in_lands_on_the_unified_workspace(tmp_db, monkeypatch):
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()
    client = TestClient(create_app())
    # Codex 2026-11-03: existing terminal remains available from the workspace.
    assert client.post("/login", data={"password": PASSWORD}, follow_redirects=False).headers["location"] == "/ultimate"


# --------------------------------------------------------------------------------------
# the 2026-10-20 redesign: watchlist, agent activity, status bar, GMGN wallet lists
# --------------------------------------------------------------------------------------

OWNED = "Fh1nKwRCyMoLbTKvfRyq3yPvXgdLwhtTxsNKKW9ZdYbQ"
KEEP_A = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
KEEP_B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
COHORT = "HN7cABqLq46Es1jh92dQQisAq662SmxELLLsHHe4YWrH"


def test_the_watchlist_folds_a_tokens_events_into_one_row(term):
    now = now_ms()
    seed_token(term.conn)
    for i, feed in enumerate(("gmgn:trending", "gmgn:smart", "binance:alpha")):
        term.conn.execute("INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, symbol) "
                          "VALUES (?, 'sol', ?, ?, 'KAIBA')", (feed, TOKEN, now - 60_000 + i))
    term.conn.execute("INSERT INTO signals (signal_id, created_ms, lane, chain, token, strength, reasons_json) "
                      "VALUES ('sig1', ?, 'sm-trenches', 'sol', ?, 0.7, '[]')", (now - 10_000, TOKEN))
    rows = [r for r in intel.alpha_watch(60) if r.get("token") == TOKEN]
    assert len(rows) == 1, "one row per token, not one per event"
    (row,) = rows
    assert row["n"] == 4 and row["kinds"][0] == "signal" and "trending" in row["kinds"]
    assert row["market"]["grade"] == "B" and row["market"]["liq_usd"] == 15000.0
    html = term.get("/terminal/watch").text
    assert html.count(f'data-push="sol:{TOKEN}"') == 1 and "4 events" in html


def test_agent_activity_shows_buys_sells_passes_with_reasons_and_notes(term):
    now = now_ms()
    seed_token(term.conn)
    for i, (action, blockers) in enumerate([("enter", "[]"), ("skip", '["size_not_positive:score_below_ladder:61.6"]'),
                                            ("skip", '["size_not_positive:score_below_ladder:57.1"]'),
                                            ("skip", '["shadow_open_cap:5/5"]')]):
        term.conn.execute("INSERT INTO decisions (decision_id, ts_ms, lane, mode, chain, token, action, blockers_json, "
                          "size_base_units) VALUES (?, ?, 'sm-trenches', 'shadow', 'sol', ?, ?, ?, 50000000)",
                          (f"d{i}", now - 30_000 + i, TOKEN, action, blockers))
    term.conn.execute("INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
                      "cost_native, proceeds_native, realized_native, exit_reason) VALUES ('pc1', 'sol', ?, 'sm-trenches', "
                      "'shadow', ?, ?, '0', '1000', '100000000', '112000000', '12000000', 'take_profit')",
                      (TOKEN, now - 600_000, now - 20_000))
    term.conn.execute("INSERT INTO journal (ts_ms, kind, subject, body, prev_hash, entry_hash) "
                      "VALUES (?, 'observation', NULL, ?, '', 'h1')",
                      (now - 5_000, "LEARNING SWEEP: 4 ran, 0 failed\nmore lines"))
    act = intel.activity()
    kinds = [e["type"] for e in act["feed"]]
    assert {"buy", "sell", "pass", "note"} <= set(kinds)
    (passed,) = [e for e in act["feed"] if e["type"] == "pass"]
    assert passed["n"] == 3 and passed["sub"].startswith("score below ladder ×2"), "the reason, not the number"
    (sold,) = [e for e in act["feed"] if e["type"] == "sell"]
    assert sold["ret"] == pytest.approx(0.12) and "take profit" in sold["sub"]
    assert act["counts"] == {"buys": 1, "passes": 3, "sells": 1, "wins": 1, "mean": pytest.approx(0.12)}
    html = term.get("/terminal/activity").text
    assert "Bought KAIBA" in html and "Sold KAIBA" in html and "+12.0%" in html and "LEARNING SWEEP" in html


def test_the_status_bar_says_when_protection_last_priced_the_book(term):
    seed_token(term.conn)
    seed_position(term.conn)
    bar = terminal_live.status_bar()
    assert bar["open"] == 1 and bar["marked"] == 1 and bar["stale"] == 0 and bar["marked_ms"]
    assert "1/1 priced" in term.get("/terminal/status").text


def test_the_terminal_opens_on_the_honest_overview_and_a_token_link_opens_it_in_place(term):
    html = term.get("/terminal").text
    assert "How the agent is doing" in html and "Agent activity" in html and "data-ticket-slot" in html
    seed_token(term.conn)
    html = term.get(f"/t/sol/{TOKEN}").text
    assert 'data-term' in html and f'data-token="sol:{TOKEN}"' in html, "a token page is the terminal, token open"
    assert "Paper buy KAIBA" in html and f"https://gmgn.ai/sol/token/{TOKEN}" in html


def _gmgn_world(term, tmp_path, monkeypatch):
    """Graded and tracked wallets, one of each that must never be exported: an owned address
    written in config, and a trusted_copy cohort wallet."""
    now = now_ms()
    cfg = tmp_path / "repo" / "config"
    cfg.mkdir(parents=True)
    (cfg / "signer-policy.yaml").write_text(f"owned_addresses:\n  sol:\n    - {OWNED}\n", encoding="utf-8")
    monkeypatch.setattr(wallet_export, "_repo_root", lambda: tmp_path / "repo")
    for addr, grade, score in ((KEEP_A, "A", 91.0), (KEEP_B, "B", 72.4), (OWNED, "A", 99.0), (COHORT, "B", 80.0)):
        term.conn.execute("INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
                          "model_version, scored_at_ms) VALUES ('sol', ?, ?, ?, 10, 'sniper', 'v-test', ?)",
                          (addr, score, grade, now))
    term.conn.execute("INSERT INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) "
                      "VALUES ('sol', ?, 'trusted_copy', ?, ?)", (COHORT, now, now))
    term.conn.execute("INSERT INTO tracker_watchlist (chain, address, tier, status, reason, source, added_by, added_ms) "
                      "VALUES ('sol', ?, 'candidate', 'active', 'test', 'operator', 'test', ?)", (KEEP_B, now))
    wallet_export.clear_caches()


def test_gmgn_lists_never_carry_our_own_wallets_and_use_gmgns_exact_shape(term, tmp_path, monkeypatch):
    from kaiba.intelligence.naming import GMGN_ROW_KEYS

    _gmgn_world(term, tmp_path, monkeypatch)
    graded = wallet_export.get("graded-sol")
    assert [r["address"] for r in graded["rows"]] == [KEEP_A, KEEP_B], "owned and trusted_copy wallets are filtered"
    assert graded["excluded"] == 1, "the owned address was dropped by the config rule, and counted"
    body = term.get("/wallets/gmgn/graded-sol.json")
    assert body.status_code == 200 and 'filename="kaiba-graded-sol.json"' in body.headers["content-disposition"]
    rows = json.loads(body.text)
    assert [tuple(r) for r in rows] == [GMGN_ROW_KEYS] * 2, "exactly the keys GMGN round-trips, in its order"
    assert rows[0]["name"] == "Kaiba Sniper A91" and rows[0]["alertsOnToast"] is True
    text = term.get("/wallets/gmgn/graded-sol.txt").text
    assert text.splitlines() == [f"{KEEP_A}:Kaiba Sniper A91", f"{KEEP_B}:Kaiba Sniper B72"]
    tracked = term.get("/wallets/gmgn/tracked-sol.json").json()
    assert [r["address"] for r in tracked] == [KEEP_B]
    for bad in ("/wallets/gmgn/graded-sol.yaml", "/wallets/gmgn/nope-sol.json", "/wallets/gmgn/graded-sol.json?part=2",
                "/wallets/gmgn/..%2F..%2Fconfig%2Frisk.json"):
        assert term.get(bad).status_code == 404, bad
    page = term.get("/intel/wallets").text
    assert "Wallets, ready to import into GMGN" in page and OWNED not in page and COHORT not in page


def test_a_list_longer_than_gmgn_takes_is_served_in_parts(term, tmp_path, monkeypatch):
    _gmgn_world(term, tmp_path, monkeypatch)
    found = wallet_export._finish("graded-sol", "t", "sol", "graded",
                                  [{"address": f"0x{i:040x}", "name": f"w{i}", "emoji": ""} for i in range(4500)],
                                  "test", set())
    assert found["parts"] == 3 and found["count"] == 4500
    assert len(wallet_export.part_rows(found, 1)) == 2000 and len(wallet_export.part_rows(found, 3)) == 500
    assert wallet_export.filename(found, 2, "json") == "kaiba-graded-sol-part2of3.json"


def test_no_list_is_served_when_our_own_addresses_cannot_be_read(term, tmp_path, monkeypatch):
    monkeypatch.setattr(wallet_export, "_repo_root", lambda: tmp_path / "nowhere")
    wallet_export.clear_caches()
    assert wallet_export.lists()["available"] is False
    assert term.get("/wallets/gmgn/graded-sol.json").status_code == 404


def test_all_view_keeps_the_agents_own_alpha_when_trending_feeds_flood_it(term):
    """MEASURED on the box 2026-10-20: 57 of the newest 60 watchlist tokens were trending-only, so
    the launches and signals the agent itself raised were pushed off the list."""
    now = now_ms()
    for i in range(30):
        term.conn.execute("INSERT INTO alpha_feed_first_seen (feed, chain, token_address, first_seen_ms, symbol, first_rank) "
                          "VALUES ('binance:topic_rush:latest', 'bsc', ?, ?, ?, ?)",
                          (f"0x{i + 1:040x}", now - 1_000 * i, f"T{i}", i + 1))
    launches = [f"0x{0xabc000 + i:040x}" for i in range(5)]
    for i, token in enumerate(launches):
        term.conn.execute("INSERT INTO triage_decisions (ts_ms, chain, token, verdict, score, reasons_json, launchpad) "
                          "VALUES (?, 'bsc', ?, 'promote', 70, '[]', 'flap')", (now - 60_000 - i, token))
    rows = intel.alpha_watch(15)
    assert len(rows) == 15
    assert {r["token"] for r in rows if r["lead"] == "launch"} == set(launches), "the agent's launches are not crowded out"
    assert sum(1 for r in rows if r["kinds"] == ["trending"]) == 10
    trending_only = intel.alpha_watch(15, kinds=("trending",))
    assert len(trending_only) == 15 and all(r["lead"] == "trending" for r in trending_only), "the Trending tab is uncapped"
    html = term.get("/terminal/watch").text
    assert "Binance topic rush" in html and "#1<" in html, "a feed reads as a name, with its first rank"


def test_the_header_price_is_the_newest_one_we_hold_and_says_where_it_came_from(term):
    """MEASURED 2026-10-20 (SNAILMAIL): the header showed the dossier's price beside a chart and a
    protection mark half as high. The header now shows the newest price, named."""
    seed_token(term.conn, price="0.0000469")
    term.conn.execute("UPDATE token_dossiers SET dossier_json = json_set(dossier_json, '$.price_usd.receipt.observed_at_ms', ?) "
                      "WHERE address = ?", (now_ms() - 300_000, TOKEN))
    seed_position(term.conn, entry="0.00003", mark="0.0000209")
    tok = intel.token_profile("sol", TOKEN)
    assert tok["quote"]["source"] == "protection mark" and float(tok["quote"]["price"]) == pytest.approx(0.0000209)
    html = term.get(f"/t/sol/{TOKEN}").text
    assert '<span class="big">$0.0₄209</span>' in html and "protection mark ·" in html
    assert "dossier said $0.0₄469" in html, "the older dossier price is still one hover away"


# --------------------------------------------------------------------------------------
# GMGN wiring (TERMINAL-GMGN-WIRING-20261020): the candle embed and Codex's account routes
# --------------------------------------------------------------------------------------

RH_TOKEN = "0x" + "ab" * 20


def test_a_sol_token_offers_gmgns_candles_loaded_only_by_the_page_script(term):
    seed_token(term.conn)
    html = term.get(f"/t/sol/{TOKEN}").text
    url = f"https://www.gmgn.cc/kline/sol/{TOKEN}?theme=dark&amp;interval=1"
    assert f'data-src="{url}"' in html, "a 10-minute-old token gets 1-minute candles"
    assert 'iframe src=' not in html and " src=\"https://www.gmgn" not in html, "the server never auto-loads a frame"
    assert 'referrerpolicy="no-referrer"' in html and "allow-top-navigation" not in html
    assert 'data-csrc="gmgn"' in html and 'data-csrc="kaiba"' in html


def test_gmgn_candle_interval_follows_token_age_and_robinhood_gets_none():
    day = 86_400_000
    assert intel.gmgn_chart_url("sol", TOKEN, now_ms() - 2 * day).endswith("interval=15")
    assert intel.gmgn_chart_url("sol", TOKEN, now_ms() - 9 * day).endswith("interval=60")
    assert intel.gmgn_chart_url("bsc", RH_TOKEN, None).startswith("https://www.gmgn.cc/kline/bsc/")
    assert intel.gmgn_chart_url("robinhood", RH_TOKEN, None) is None, "GMGN does not chart Robinhood Chain"
    assert intel.gmgn_chart_url("sol", "not-an-address", None) is None


class _Receipt:
    provider = "gmgn"
    endpoint = "portfolio.info"
    basis = "provider_reported"
    note = None

    def __init__(self, observed_at_ms):
        self.observed_at_ms = observed_at_ms


class _Result:
    def __init__(self, data):
        self.data = data
        self.receipt = _Receipt(now_ms() - 1_000)


def test_gmgn_account_check_is_session_and_csrf_gated_and_cooled_down(term, monkeypatch):
    from dashboard import gmgn_account

    calls = []

    def fake_fetch():
        calls.append(1)
        return _Result({"wallets": [{"chain": "sol", "address": KEEP_A, "balances": []}]})

    monkeypatch.setattr(gmgn_account, "_default_fetch", fake_fetch)
    anon = TestClient(create_app())
    assert anon.get("/api/gmgn/account").status_code == 401
    assert anon.post("/api/gmgn/account/check").status_code == 401 and not calls
    assert term.get("/api/gmgn/account").json()["status"] == "not_checked", "a page load never calls GMGN"
    assert term.post("/api/gmgn/account/check").status_code == 403 and not calls, "no CSRF, no call"
    token = csrf(term)
    ok = term.post("/api/gmgn/account/check", headers={"x-kaiba-csrf": token})
    body = ok.json()
    assert ok.status_code == 200 and body["status"] == "connected" and len(calls) == 1
    assert [w["address"] for w in body["wallets"]] == [KEEP_A] and body["wallets"][0]["native_balance"] is None
    again = term.post("/api/gmgn/account/check", headers={"x-kaiba-csrf": token})
    assert again.status_code == 429 and len(calls) == 1, "one GMGN read per 30 s"
    assert term.get("/api/gmgn/account").json()["status"] == "connected", "GET serves the last check"
    assert "data-gmgn-account" in term.get("/intel/wallets").text


def test_a_wallet_confluence_reaches_the_alpha_stream_as_a_linkable_token(term):
    """kaiba/ingest/wallet_track.py (2026-10-28) writes alpha_signals rows; the terminal reads them
    with their own source query so other alpha sources cannot push them off the stream."""
    now = now_ms()
    seed_token(term.conn)
    payload = {"level": 3, "tiers": {"S": 0, "A": 1, "B": 2}, "usd": 1234.5, "symbol": "KAIBA",
               "wallets": [{"wallet": "w1", "tier": "A", "sold": True}, {"wallet": "w2", "tier": "B", "sold": False}]}
    term.conn.execute("INSERT INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, first_seen_ms, "
                      "payload_json) VALUES ('wc1', 'kaiba:wallet_track', 'wallet_confluence', ?, 't', NULL, 'sol', ?, ?)",
                      (TOKEN, now - 1_000, json.dumps(payload)))
    for i in range(200):
        term.conn.execute("INSERT INTO alpha_signals (signal_key, source, kind, subject, title, chain, first_seen_ms) "
                          "VALUES (?, 'crtsh', 'cert_subdomain', ?, 'claim', 'sol', ?)", (f"k{i}", f"c{i}.x", now))
    rows = [i for i in intel.alpha_stream(400) if i["kind"] == "confluence"]
    assert len(rows) == 1 and rows[0]["token"] == TOKEN and rows[0]["linkable"]
    assert "A×1 B×2" in rows[0]["detail"] and "$1,234 bought" in rows[0]["detail"] and "1 already sold" in rows[0]["detail"]
    html = term.get("/terminal/watch?tab=confluence").text
    assert "3 tracked wallets" in html and f'data-push="sol:{TOKEN}"' in html
