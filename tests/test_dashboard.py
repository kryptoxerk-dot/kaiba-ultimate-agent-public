"""Operator dashboard: renders honestly when empty, streams, and cannot move money.

Three things these tests are really guarding.

*Honesty on an empty database.* Phase 1 ships this console long before positions, grades
and quotes exist. A dashboard that invents a plausible number is worse than no dashboard,
so the empty-state tests assert that nothing dollar-shaped appears when there is nothing to
report, and that each panel says why it is empty.

*Resumable streaming.* The SSE ``id:`` has to be the ``events`` row id or a browser that
drops its connection silently loses events, which is exactly when an operator is watching.

*No withdraw surface.* The last assertion walks ``app.routes`` and fails if the word ever
appears in a path or a handler name. That is a structural guarantee, not a policy.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil

import pytest
from fastapi.testclient import TestClient

from dashboard import auth
from dashboard.app import PARTIALS, create_app
from kaiba.core import db, events, journal
from kaiba.core.config import DEFAULT_RISK_PATH, get_settings, load_risk
from kaiba.core.schemas import now_ms

PASSWORD = "correct-horse-battery-staple"

SOL_TOKEN = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"
SOL_WALLET = "8psNvWTrdNTiVRNzAgsou9kETXNJm2SXZyaKuJraVRtf"
SOL_WALLET_B = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"

# Numbers and tickers from the visual reference draft. None of them may leak into the page.
FABRICATED = ["1,842,390", "320,118", "522,271", "94,320.11", "$3,212.45", "6 / 6", "$28.4B"]


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def console(tmp_db, tmp_path, monkeypatch):
    """A migrated database, a private copy of the risk envelope and a configured password."""
    risk_path = tmp_path / "risk.yaml"
    if DEFAULT_RISK_PATH.exists():
        shutil.copy(DEFAULT_RISK_PATH, risk_path)
    else:  # pragma: no cover - the repo always ships one
        risk_path.write_text("version: v1\nglobal_mode: shadow\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    get_settings.cache_clear()

    client = TestClient(create_app())
    client.risk_path = risk_path  # type: ignore[attr-defined]
    client.conn = tmp_db  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        get_settings.cache_clear()


@pytest.fixture
def signed_in(console):
    response = console.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    assert auth.COOKIE_NAME in console.cookies
    return console


def csrf_of(client: TestClient) -> str:
    """Whatever the page hands the browser is what the tests post back."""
    html = client.get("/").text
    match = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert match and match.group(1), "the page must publish a CSRF token"
    return match.group(1)


def sse_frames(client: TestClient, **params) -> list[str]:
    params.setdefault("poll_ms", 50)
    headers = params.pop("headers", None)
    with client.stream("GET", "/events/stream", params=params, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    return [chunk for chunk in body.replace("\r\n", "\n").split("\n\n") if chunk.strip()]


def frame_field(frame: str, field: str) -> str | None:
    for line in frame.split("\n"):
        if line.startswith(f"{field}: "):
            return line[len(field) + 2 :]
    return None


# ---------------------------------------------------------------- empty-state rendering


def test_index_renders_with_an_empty_database(signed_in):
    response = signed_in.get("/")
    assert response.status_code == 200
    assert "Command Centre" in response.text
    assert "Agent stream" in response.text


def test_index_says_why_each_panel_is_empty(signed_in):
    html = signed_in.get("/").text
    for expected in [
        "No open positions",
        "No signals recorded yet",
        "No wallet has been graded yet",
        "no provider has been called yet",
        "the journal is empty",
    ]:
        assert expected in html, f"missing honest empty state: {expected!r}"


def test_index_invents_no_money_numbers_on_an_empty_database(signed_in):
    html = signed_in.get("/").text
    assert re.search(r"\$\s?\d", html) is None, "a currency amount appeared with no data behind it"
    for fake in FABRICATED:
        assert fake not in html, f"reference-draft placeholder leaked into the page: {fake}"
    assert "no closed trades today" in html


def test_index_states_that_withdrawals_do_not_exist(signed_in):
    html = signed_in.get("/").text
    assert "Withdrawals are not available to the agent by construction." in html


def test_index_renders_the_left_rail_sections(signed_in):
    html = signed_in.get("/").text
    for label in ["Overview", "Wallets", "Tokens", "Signals", "Positions", "Agent", "Risk"]:
        assert f">{label}</b>" in html


def test_index_is_mobile_first(signed_in):
    html = signed_in.get("/").text
    assert 'name="viewport"' in html and "width=device-width" in html
    css = signed_in.get("/static/app.css").text
    assert "@media (min-width: 900px)" in css, "the wide layout must be the progressive enhancement"


# ---------------------------------------------------------------- json api


API_ROUTES = [
    "/api/overview",
    "/api/positions",
    "/api/signals",
    "/api/decisions",
    "/api/wallets/top",
    "/api/entities",
    "/api/providers",
    "/api/risk",
    "/api/journal",
]


@pytest.mark.parametrize("path", API_ROUTES)
def test_api_returns_valid_json_on_an_empty_database(signed_in, path):
    response = signed_in.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(json.loads(response.text), dict)


def test_api_overview_reports_nothing_rather_than_zero_pnl(signed_in):
    body = signed_in.get("/api/overview").json()
    assert body["open_positions"] == 0
    assert body["pnl_today"]["available"] is False
    assert body["pnl_today"]["reason"]
    assert body["providers"]["available"] is False


def test_api_risk_exposes_the_envelope_and_lane_modes(signed_in):
    body = signed_in.get("/api/risk").json()
    assert body["available"] is True
    assert body["bounds"]["max_size_pct_bankroll"] > 0
    assert body["lanes"], "lanes from config/risk.yaml should be listed"
    assert {"lane", "mode", "effective_mode"} <= set(body["lanes"][0])


def test_api_entities_degrades_to_an_explained_empty_graph(signed_in):
    body = signed_in.get("/api/entities").json()
    assert body["available"] is False
    assert body["nodes"] == [] and body["edges"] == []
    assert body["reason"]


def test_healthz_is_reachable(signed_in):
    assert signed_in.get("/healthz").json()["ok"] is True


# ---------------------------------------------------------------- partials


@pytest.mark.parametrize("name", sorted(PARTIALS))
def test_every_partial_renders_on_an_empty_database(signed_in, name):
    response = signed_in.get(f"/partials/{name}")
    assert response.status_code == 200
    assert response.text.strip()


def test_unknown_partial_is_a_404(signed_in):
    assert signed_in.get("/partials/nope").status_code == 404


def test_feed_says_the_bus_is_empty_before_anything_happens(tmp_db, tmp_path, monkeypatch):
    """Signing in writes a ``system`` event, so the truly-empty feed needs the auth-off app."""
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "risk.yaml"))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        assert "No events on the bus yet" in client.get("/partials/feed").text
    get_settings.cache_clear()


# ---------------------------------------------------------------- populated panels


def test_feed_row_truncates_the_subject_and_offers_a_copy(console, signed_in):
    events.emit("wallet.trade", {"summary": "bought 0.4 SOL"}, subject=SOL_WALLET, conn=console.conn)
    html = signed_in.get("/partials/feed").text
    assert SOL_WALLET not in re.sub(r'data-copy="[^"]*"|title="[^"]*"', "", html)
    assert f'data-copy="{SOL_WALLET}"' in html
    assert "…" in html
    assert "bought 0.4 SOL" in html


def test_positions_panel_shows_protection_state_and_no_invented_price(console, signed_in):
    console.conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, realized_native, entry_price_usd, protected, protection_ids_json) "
        "VALUES ('p1','sol',?,'confluence-5','shadow',?,'1000','1000','20000000','0','0.00031',1,'[\"o1\"]')",
        (SOL_TOKEN, now_ms()),
    )
    html = signed_in.get("/partials/positions").text
    assert "1 order" in html
    assert "unrealized needs a live quote" in html
    body = signed_in.get("/api/positions").json()
    assert body["count"] == 1
    assert body["positions"][0]["current_price_usd"] is None


def test_signal_tape_shows_lane_and_entity_count(console, signed_in):
    console.conn.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, entities_json, "
        "wallets_json, created_ms) VALUES ('s1','confluence-5','sol',?,0.82,'[\"e1\",\"e2\"]',"
        "'[\"w1\",\"w2\",\"w3\"]',?)",
        (SOL_TOKEN, now_ms()),
    )
    html = signed_in.get("/partials/signals").text
    assert "confluence-5" in html
    assert "2e / 3w" in html


def test_entity_graph_returns_cytoscape_shaped_nodes_and_edges(console, signed_in):
    ts = now_ms()
    for address in (SOL_WALLET, SOL_WALLET_B):
        console.conn.execute(
            "INSERT INTO wallet_scores (chain, address, score, grade, evidence_weight, archetype, "
            "model_version, scored_at_ms) VALUES ('sol',?,81.0,'A',0.9,'insider','v1',?)",
            (address, ts),
        )
    a, b = sorted((SOL_WALLET, SOL_WALLET_B))
    console.conn.execute(
        "INSERT INTO cluster_edges (chain, a, b, edge_type, confidence, first_seen_ms, last_seen_ms) "
        "VALUES ('sol',?,?,'funding',0.9,?,?)",
        (a, b, ts, ts),
    )
    body = signed_in.get("/api/entities").json()
    assert body["available"] is True
    assert {n["id"] for n in body["nodes"]} == {f"sol:{SOL_WALLET}", f"sol:{SOL_WALLET_B}"}
    assert body["edges"][0]["source"].startswith("sol:")
    assert body["nodes"][0]["archetype"] == "insider"
    html = signed_in.get("/partials/entities").text
    assert "Table view" in html, "the graph must degrade to a table"


def test_provider_meters_reflect_limiter_state(console, signed_in):
    console.conn.execute(
        "INSERT INTO provider_state (provider, credit_milli, last_refill_ms, last_call_ms, inflight, "
        "banned_until_ms, penalty_level, spent_today, day_key) VALUES ('gmgn',5000,?,0,1,0,0,7,'x')",
        (now_ms(),),
    )
    body = signed_in.get("/api/providers").json()
    assert body["available"] is True
    meter = body["providers"][0]
    assert meter["provider"] == "gmgn"
    assert meter["inflight"] == 1
    assert "gmgn" in signed_in.get("/partials/providers").text


def test_journal_panel_shows_the_latest_lesson(console, signed_in):
    journal.append("lesson", "migration fade needs a tighter clock", conn=console.conn)
    body = signed_in.get("/api/journal").json()
    assert body["available"] is True
    assert "tighter clock" in body["entries"][0]["body"]
    assert "tighter clock" in signed_in.get("/partials/journal").text


# ---------------------------------------------------------------- sse


def test_sse_emits_the_event_with_its_row_id(console, signed_in):
    event_id = events.emit("agent.thought", {"summary": "watching the curve"}, conn=console.conn)
    frames = sse_frames(signed_in, after_id=event_id - 1, limit=1)
    data_frames = [f for f in frames if f.startswith("id: ")]
    assert data_frames, "the stream produced no data frames"
    assert frame_field(data_frames[0], "id") == str(event_id)
    assert frame_field(data_frames[0], "event") == "feed"
    assert "watching the curve" in data_frames[0]


def test_sse_names_a_channel_after_the_event_kind(console, signed_in):
    event_id = events.emit("position.opened", {"summary": "entered"}, conn=console.conn)
    frames = sse_frames(signed_in, after_id=event_id - 1, limit=1)
    names = {frame_field(f, "event") for f in frames if f.startswith("id: ")}
    assert "feed" in names
    assert "position.opened" in names
    assert "positions" in names, "the positions panel needs a channel it can hx-trigger on"


def test_sse_opens_with_a_comment_and_never_a_bare_data_line(console, signed_in):
    event_id = events.emit("system", {"summary": "hello"}, conn=console.conn)
    frames = sse_frames(signed_in, after_id=event_id - 1, limit=1)
    assert frames[0].startswith(":"), "the first frame should be a comment, not fake data"


def test_sse_resumes_from_last_event_id(console, signed_in):
    first = events.emit("agent.thought", {"summary": "one"}, conn=console.conn)
    events.emit("agent.thought", {"summary": "two"}, conn=console.conn)
    events.emit("agent.thought", {"summary": "three"}, conn=console.conn)
    frames = sse_frames(signed_in, limit=2, headers={"Last-Event-ID": str(first)})
    feed = [f for f in frames if frame_field(f, "event") == "feed"]
    assert len(feed) == 2
    assert "two" in feed[0] and "three" in feed[1]
    assert frame_field(feed[0], "id") == str(first + 1)


def test_sse_can_filter_by_kind(console, signed_in):
    events.emit("agent.thought", {"summary": "noise"}, conn=console.conn)
    wanted = events.emit("risk.halt", {"summary": "halted"}, conn=console.conn)
    frames = sse_frames(signed_in, after_id=0, limit=1, kinds="risk.halt")
    feed = [f for f in frames if frame_field(f, "event") == "feed"]
    assert frame_field(feed[0], "id") == str(wanted)
    assert "halted" in feed[0]


# ---------------------------------------------------------------- auth


def test_anonymous_visitor_is_sent_to_the_login_page(console):
    response = console.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_is_refused(console):
    response = console.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?bad=1"
    assert auth.COOKIE_NAME not in console.cookies


def test_right_password_issues_a_signed_cookie(console):
    console.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    token = console.cookies[auth.COOKIE_NAME]
    assert auth.verify_session(token, PASSWORD)
    assert not auth.verify_session(token, "a different password")
    assert not auth.verify_session(token[:-2] + "xx", PASSWORD)


def test_unauthenticated_kill_is_rejected(console):
    response = console.post("/control/kill", data={auth.CSRF_FIELD: "anything"})
    assert response.status_code == 401
    assert load_risk(console.risk_path).kill_switch is False


def test_unauthenticated_partial_is_rejected(console):
    assert console.get("/partials/risk").status_code == 401


def test_control_requires_a_csrf_token(signed_in):
    assert signed_in.post("/control/kill", data={}).status_code == 403
    assert signed_in.post("/control/kill", data={auth.CSRF_FIELD: "forged"}).status_code == 403
    assert load_risk(signed_in.risk_path).kill_switch is False


def test_csrf_token_is_bound_to_the_session(signed_in):
    mine = csrf_of(signed_in)
    assert auth.verify_csrf(mine, signed_in.cookies[auth.COOKIE_NAME], PASSWORD)
    assert not auth.verify_csrf(mine, "someone-elses-cookie", PASSWORD)


def test_loopback_only_when_no_password_is_configured(tmp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "risk.yaml"))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", "")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        html = client.get("/").text
        assert "Auth is off" in html
        remote = client.get("/", headers={"host": "kaiba.example"})
        assert remote.status_code == 200  # TestClient is loopback; the banner is the warning
    with TestClient(create_app(), client=("203.0.113.7", 51234)) as offbox:
        assert offbox.get("/").status_code == 403
    get_settings.cache_clear()


# ---------------------------------------------------------------- controls


def test_authenticated_kill_flips_the_kill_switch_in_the_config_file(signed_in):
    response = signed_in.post("/control/kill", data={auth.CSRF_FIELD: csrf_of(signed_in)})
    assert response.status_code == 200
    saved = load_risk(signed_in.risk_path)
    assert saved.kill_switch is True
    assert saved.entries_paused is True


def test_kill_emits_a_risk_halt_event(signed_in):
    signed_in.post("/control/kill", data={auth.CSRF_FIELD: csrf_of(signed_in)})
    halts = events.recent(limit=20, kinds=["risk.halt"], conn=db.get_conn())
    assert halts, "the kill switch must leave a trace on the bus"
    assert halts[0].payload["action"] == "kill"
    assert halts[0].level == "error"


def test_kill_turns_every_lane_off(signed_in):
    signed_in.post("/control/kill", data={auth.CSRF_FIELD: csrf_of(signed_in)})
    body = signed_in.get("/api/risk").json()
    assert body["kill_switch"] is True
    assert {lane["effective_mode"] for lane in body["lanes"]} == {"off"}


def test_pause_and_resume_move_only_the_entry_gate(signed_in):
    token = csrf_of(signed_in)
    signed_in.post("/control/pause", data={auth.CSRF_FIELD: token})
    assert load_risk(signed_in.risk_path).entries_paused is True
    signed_in.post("/control/resume", data={auth.CSRF_FIELD: token})
    assert load_risk(signed_in.risk_path).entries_paused is False


def test_reduce_only_can_be_set_and_cleared(signed_in):
    token = csrf_of(signed_in)
    signed_in.post("/control/reduce-only", data={auth.CSRF_FIELD: token, "on": "1"})
    assert load_risk(signed_in.risk_path).reduce_only is True
    signed_in.post("/control/reduce-only", data={auth.CSRF_FIELD: token, "on": "0"})
    assert load_risk(signed_in.risk_path).reduce_only is False


def test_resume_clears_the_kill_switch_only_when_asked(signed_in):
    token = csrf_of(signed_in)
    signed_in.post("/control/kill", data={auth.CSRF_FIELD: token})
    signed_in.post("/control/resume", data={auth.CSRF_FIELD: token})
    assert load_risk(signed_in.risk_path).kill_switch is True
    signed_in.post("/control/resume", data={auth.CSRF_FIELD: token, "clear_kill": "1"})
    assert load_risk(signed_in.risk_path).kill_switch is False


def test_close_position_records_an_intent_and_does_not_execute(console, signed_in):
    console.conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total) "
        "VALUES ('p1','sol',?,'confluence-5','shadow',?,'1000','1000')",
        (SOL_TOKEN, now_ms()),
    )
    response = signed_in.post(
        "/control/close-position", data={auth.CSRF_FIELD: csrf_of(signed_in), "position_id": "p1"}
    )
    assert response.status_code == 200
    intents = [
        e
        for e in events.recent(limit=20, kinds=["system"], conn=db.get_conn())
        if e.payload.get("intent") == "close_position"
    ]
    assert intents and intents[0].payload["executed"] is False
    assert signed_in.get("/api/positions").json()["count"] == 1, "nothing may be closed from here"


def test_close_position_rejects_an_unknown_id(signed_in):
    response = signed_in.post(
        "/control/close-position", data={auth.CSRF_FIELD: csrf_of(signed_in), "position_id": "ghost"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- risk dial


def test_risk_dial_rejects_a_size_above_the_envelope(signed_in):
    bounds = load_risk(signed_in.risk_path).bounds
    response = signed_in.post(
        "/control/risk-dial",
        data={
            auth.CSRF_FIELD: csrf_of(signed_in),
            "lane": "confluence-5",
            "size_pct_max": bounds.max_size_pct_bankroll + 1,
        },
    )
    assert response.status_code == 400
    assert "max_size_pct_bankroll" in response.json()["detail"]
    saved = load_risk(signed_in.risk_path)
    assert saved.lanes["confluence-5"].size_pct_max <= bounds.max_size_pct_bankroll


def test_risk_dial_rejects_a_daily_loss_above_the_envelope(signed_in):
    bounds = load_risk(signed_in.risk_path).bounds
    response = signed_in.post(
        "/control/risk-dial",
        data={
            auth.CSRF_FIELD: csrf_of(signed_in),
            "daily_loss_pct": bounds.max_daily_loss_pct + 0.5,
        },
    )
    assert response.status_code == 400
    assert "max_daily_loss_pct" in response.json()["detail"]


def _inside_the_envelope(client) -> float:
    """A confluence-5 ``size_pct_max`` the dial must accept, derived from the copied file.

    These tests used literals (1.0-1.5) that were legal while the lane's band sat at
    1.0-5.0. The operator moved it to 26-29% (envelope 30%) on 2026-10-03, so a literal
    1.5 became an INVERTED range and was (correctly) refused. Use a value strictly between
    the lane's own minimum and the envelope, and different from what is stored, so an
    accepted change is visible.
    """
    cfg = load_risk(client.risk_path)
    lane = cfg.lanes["confluence-5"]
    lo, hi = float(lane.size_pct_min), float(cfg.bounds.max_size_pct_bankroll)
    assert lo < hi, "fixture has no room inside the envelope"
    value = round(lo + (hi - lo) / 4, 2)
    assert lo <= value <= hi and value != float(lane.size_pct_max)
    return value


def test_risk_dial_accepts_a_value_inside_the_envelope(signed_in):
    value = _inside_the_envelope(signed_in)
    response = signed_in.post(
        "/control/risk-dial",
        data={auth.CSRF_FIELD: csrf_of(signed_in), "lane": "confluence-5", "size_pct_max": value},
    )
    assert response.status_code == 200
    assert load_risk(signed_in.risk_path).lanes["confluence-5"].size_pct_max == value


def test_risk_dial_rejects_an_unknown_lane_and_an_inverted_range(signed_in):
    token = csrf_of(signed_in)
    unknown = signed_in.post(
        "/control/risk-dial", data={auth.CSRF_FIELD: token, "lane": "no-such-lane", "size_pct_max": 1}
    )
    assert unknown.status_code == 400
    inverted = signed_in.post(
        "/control/risk-dial",
        data={
            auth.CSRF_FIELD: token,
            "lane": "confluence-5",
            "size_pct_min": 3.0,
            "size_pct_max": 1.0,
        },
    )
    assert inverted.status_code == 400


def test_risk_dial_cannot_widen_the_operator_envelope(signed_in):
    before = load_risk(signed_in.risk_path).bounds.max_size_pct_bankroll
    response = signed_in.post(
        "/control/risk-dial",
        data={auth.CSRF_FIELD: csrf_of(signed_in), "lane": "confluence-5",
              "size_pct_max": _inside_the_envelope(signed_in)},
    )
    # The write must actually happen, or "bounds unchanged" proves nothing about a save.
    assert response.status_code == 200
    assert load_risk(signed_in.risk_path).bounds.max_size_pct_bankroll == before


def test_risk_dial_emits_a_param_change_event(signed_in):
    value = _inside_the_envelope(signed_in)
    signed_in.post(
        "/control/risk-dial",
        data={auth.CSRF_FIELD: csrf_of(signed_in), "lane": "confluence-5", "size_pct_max": value},
    )
    changes = events.recent(limit=20, kinds=["param.change"], conn=db.get_conn())
    assert changes and changes[0].payload["size_pct_max"] == value


# ---------------------------------------------------------------- the structural guarantee


def test_no_route_can_withdraw_anything():
    app = create_app()
    for route in app.routes:
        path = str(getattr(route, "path", ""))
        name = str(getattr(route, "name", ""))
        endpoint = getattr(route, "endpoint", None)
        handler = getattr(endpoint, "__name__", "")
        for candidate in (path, name, handler):
            for forbidden in ("withdraw", "transfer", "send_funds"):
                assert forbidden not in candidate.lower(), f"{forbidden!r} appears in route {path}"


def test_control_surface_is_exactly_the_five_documented_writes():
    app = create_app()
    controls = sorted(
        str(route.path)
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/control/")
    )
    assert controls == [
        "/control/close-position",
        "/control/kill",
        "/control/pause",
        "/control/reduce-only",
        "/control/resume",
        "/control/risk-dial",
    ]


# ------------------------------------------------- the watchlist counter


def test_the_watchlist_counter_distinguishes_tracked_from_graded(tmp_db):
    """An empty graded table with thousands tracked must not look like a failed import."""
    from dashboard import data
    from kaiba.core.db import upsert
    from kaiba.core.schemas import now_ms

    for i in range(3):
        upsert(
            tmp_db, "wallets",
            {"chain": "sol", "address": f"W{i}", "source": "t", "cohort": "research",
             "first_seen_ms": now_ms(), "last_seen_ms": now_ms()},
            conflict=["chain", "address"], update=["cohort"],
        )
    out = data.watchlist_summary()
    assert out["tracked"] == 3
    assert out["graded"] == 0
    assert {r["chain"]: r["n"] for r in out["by_chain"]} == {"sol": 3}


def test_the_watchlist_counter_is_zero_not_missing_on_an_empty_database(tmp_db):
    from dashboard import data

    out = data.watchlist_summary()
    assert out["tracked"] == 0 and out["graded"] == 0


def test_the_empty_wallet_panel_explains_why_rather_than_blaming_a_phase(tmp_db):
    """The old copy said grading lands in Phase 2. It has landed; the reason is evidence."""
    html = (
        pathlib.Path(__file__).resolve().parents[1]
        / "dashboard" / "templates" / "partials" / "wallets.html"
    ).read_text(encoding="utf-8")
    assert "Phase 2" not in html
    assert "on-chain evidence" in html


# --------------------------------------------------------------------------------------
# the read hole found on the live vhost, 2026-09-21
# --------------------------------------------------------------------------------------

#: Every JSON route the console exposes. This list is asserted to be EXHAUSTIVE below,
#: so a route added later either lands here or fails the suite.
_JSON_READ_ROUTES = (
    "/api/overview",
    "/api/positions",
    "/api/signals",
    "/api/decisions",
    "/api/wallets/top",
    "/api/entities",
    "/api/providers",
    "/api/risk",
    "/api/journal",
    "/api/events",
)


@pytest.mark.parametrize("path", _JSON_READ_ROUTES)
def test_no_json_route_answers_without_a_session(console, path):
    """FOUND EXPOSED on the internet-facing vhost 2026-09-21, with no cookie at all.

    ``GET /`` redirected anonymous clients to ``/login``, so the console *looked* shut.
    Its data routes were not: all ten returned 200 with the full payload -- live mode,
    bankroll, open positions, the graded wallet list, the risk envelope and the agent's
    own journal. Controls were never reachable (they call ``_require_operator``) and
    there is no withdraw route to reach, so this leaked state rather than granting
    control. It was nonetheless a complete read compromise of the agent.
    """
    response = console.get(path)
    assert response.status_code == 401, f"{path} served an anonymous client"
    assert "login_required" in response.text


def test_the_json_route_list_above_is_exhaustive(console):
    """A denylist is what produced the hole; this keeps the allowlist honest.

    The middleware is an allowlist, so a new route is private by default and cannot
    reproduce the original bug. This asserts the *test* above keeps pace, so nobody
    concludes from a green suite that a newly added route was actually checked.
    """
    declared = {
        route.path
        for route in console.app.routes
        if getattr(route, "path", "").startswith("/api/")
    }
    assert declared == set(_JSON_READ_ROUTES), (
        "the set of /api routes changed; add it to _JSON_READ_ROUTES and confirm it is "
        f"authenticated. missing={declared - set(_JSON_READ_ROUTES)} "
        f"stale={set(_JSON_READ_ROUTES) - declared}"
    )


def test_healthz_stays_open_so_a_probe_needs_no_credential(console):
    """Deliberate exception: a uptime check must see the process without a password.

    It returns a bool and an event id. Widening it is a decision, and this pins it.
    """
    response = console.get("/healthz")
    assert response.status_code == 200
    assert set(response.json()) == {"ok", "latest_event_id"}
