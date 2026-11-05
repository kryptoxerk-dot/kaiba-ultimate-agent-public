"""The Kaiba operator console: one FastAPI app, server-rendered, live over SSE.

Codex coordination edit 2026-11-03: additive read-only unified workspace router,
page navigation and post-login destination (UNIFIED-DASHBOARD-20261103).

Design notes worth keeping in mind when editing this file.

*One process, no build step.* Jinja2 renders every panel on the server. HTMX refreshes the
panels and Cytoscape draws the entity graph, both from a CDN, and the page is fully useful
with neither of them loaded — that matters because this is the surface an operator reaches
for when something is already going wrong.

*The feed is the event table.* ``/events/stream`` polls ``kaiba.core.events.tail`` and sets
the SSE ``id:`` to the event id, so a browser that reconnects sends ``Last-Event-ID`` and
resumes exactly where it stopped. Every event is published on several named channels so a
panel can subscribe to what it cares about.

*Every handler that reads the database is a plain ``def``*, so FastAPI runs it in its
threadpool. MEASURED 2026-10-18: four reads on the command centre took 13-100+ s on the 40 GB
box, and as ``async def`` handlers they blocked the event loop -- every SSE stream and every
other request waited behind them. Only the streams and the login stay ``async``.

*Writes are four buttons and one dial* on the command centre: pause, resume, reduce-only,
kill, and bounded risk sliders. Three more since 2026-10-18, each on its own page: send a
message to the operator agent (``/control/chat``, ``/control/chat-stop``) and run a NAMED
command (``/control/command``; dashboard/commands.py says why there is no shell). They go through ``kaiba.core.config.save_risk``, which re-reads the ``bounds``
block from disk, so neither the agent nor this dashboard can widen the envelope. The dial
additionally rejects (400) anything outside those bounds rather than silently clamping it,
because an operator who typed 40% needs to be told it was refused.

*There is no withdraw route.* Not disabled, not permission-gated — absent. The signer has
no code path that signs a transfer to a non-owned address and GMGN's API has no transfer
endpoint, so there is nothing for a button to call. ``tests/test_dashboard.py`` asserts
over ``app.routes`` that no path or handler name here ever contains the word.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from anyio import sleep as async_sleep
from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from dashboard import agent_chat, auth, commands, data, trading, wallet_export

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TEMPLATE_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

NAV = [
    ("overview", "Overview", "◎"),
    ("wallets", "Wallets", "◈"),
    ("tokens", "Tokens", "◇"),
    ("signals", "Signals", "≋"),
    ("positions", "Positions", "▤"),
    ("agent", "Agent", "✦"),
    ("risk", "Risk", "⛨"),
]

#: Pages, shown on every rail. The command centre's own sections stay in ``NAV`` (anchors).
PAGES = [
    ("/ultimate", "Unified workspace", "◎"),
    ("/terminal", "Terminal", "◉"),
    ("/alpha", "Alpha", "⚡"),
    ("/intel/wallets", "Wallet intel", "◈"),
    ("/autopilot", "Autopilot", "⟳"),
    ("/agent", "Agent", "✦"),
    ("/", "Command Centre", "◎"),
    ("/trades", "Trades", "▤"),
    ("/wallets", "Wallets gathered", "◈"),
    ("/chat", "Agent chat (full)", "✧"),
    ("/commands", "Commands", "⌘"),
]

NO_WITHDRAW_COPY = "Withdrawals are not available to the agent by construction."

#: Login throttle: an internet-facing password in front of an agent that can trade. A client
#: past its limit is refused even with the right password until the window passes.
LOGIN_WINDOW_S = 900
LOGIN_MAX_FAILS_PER_CLIENT = 8
LOGIN_MAX_FAILS_GLOBAL = 60
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Which panel each event kind should nudge. Everything also goes to "feed" and is
# republished under its own kind, so a panel can subscribe either way.
PANEL_BY_KIND: dict[str, tuple[str, ...]] = {
    "position.opened": ("positions", "metrics"),
    "position.updated": ("positions",),
    "position.closed": ("positions", "metrics"),
    "order.submitted": ("positions",),
    "order.filled": ("positions", "metrics"),
    "order.failed": ("positions", "metrics"),
    "protection.set": ("positions",),
    "protection.triggered": ("positions", "metrics"),
    "signal.fired": ("signals",),
    "alpha.signal": ("signals",),
    "alpha.call": ("signals",),
    "alpha.listing": ("signals",),
    "decision": ("decisions",),
    "wallet.graded": ("wallets",),
    "entity.updated": ("wallets",),
    "provider.error": ("providers", "metrics"),
    "provider.budget": ("providers",),
    "risk.halt": ("risk", "metrics"),
    "param.change": ("risk",),
    "system": ("metrics",),
    "reflection": ("journal",),
}

PARTIALS = {
    "feed": "partials/feed.html",
    "metrics": "partials/metrics.html",
    "positions": "partials/positions.html",
    "signals": "partials/signals.html",
    "decisions": "partials/decisions.html",
    "providers": "partials/providers.html",
    "risk": "partials/risk.html",
    "wallets": "partials/wallets.html",
    "entities": "partials/entities.html",
    "tokens": "partials/tokens.html",
    "journal": "partials/journal.html",
}


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _password() -> str:
    try:
        from kaiba.core.config import get_settings

        return get_settings().kaiba_dashboard_password or ""
    except Exception as exc:  # settings must never take the console down
        log.warning("dashboard: settings unreadable, treating auth as unconfigured: %s", exc)
        return ""


def _client_host(request: Request) -> str | None:
    return request.client.host if request.client else None


def _login_blocked(failures: list[tuple[float, str]], host: str | None) -> bool:
    """Too many recent failures from this client, or from everyone at once."""
    cutoff = time.time() - LOGIN_WINDOW_S
    failures[:] = [(t, h) for t, h in failures if t > cutoff]
    mine = sum(1 for _t, h in failures if h == (host or "?"))
    return mine >= LOGIN_MAX_FAILS_PER_CLIENT or len(failures) >= LOGIN_MAX_FAILS_GLOBAL


def _session_value(request: Request) -> str | None:
    return request.cookies.get(auth.COOKIE_NAME)


def _is_authed(request: Request, password: str) -> bool:
    if not auth.auth_enabled(password):
        # No password configured: loopback-only, enforced in the middleware below.
        return auth.is_loopback(_client_host(request))
    return auth.verify_session(_session_value(request), password)


def _csrf_for(request: Request, password: str) -> str:
    return auth.csrf_token(_session_value(request), password)


def _require_operator(request: Request) -> str:
    """Auth gate for every ``POST /control/*``. Returns the password in use."""
    password = _password()
    if not _is_authed(request, password):
        raise HTTPException(status_code=401, detail="authentication required")
    return password


def _require_csrf(request: Request, password: str, token: str | None) -> None:
    given = token or request.headers.get(auth.CSRF_HEADER)
    if not auth.verify_csrf(given, _session_value(request), password):
        raise HTTPException(status_code=403, detail="bad or missing CSRF token")


def _emit(kind: str, payload: dict[str, Any], *, level: str = "info", subject: str | None = None) -> None:
    """Best effort: a control action must still take effect if the bus write fails."""
    try:
        from kaiba.core import db, events

        events.emit(kind, payload, level=level, subject=subject, conn=db.get_conn())
    except Exception as exc:
        log.warning("dashboard: could not emit %s: %s", kind, exc)


def _current_risk():
    from kaiba.core.config import get_risk

    try:
        return get_risk()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"risk config unreadable: {exc}") from exc


def _save_risk(cfg) -> None:
    from dashboard import broker
    from kaiba.core.config import save_risk

    try:
        if broker.active():
            broker.save_risk(cfg.model_dump(mode="json", exclude_none=True))
        else:
            save_risk(cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not write risk config: {exc}") from exc


def _control_response(request: Request, action: str, detail: dict[str, Any]) -> Response:
    """HTMX posts get the refreshed risk panel back; anything else gets JSON."""
    body = {"ok": True, "action": action, **detail}
    if request.headers.get("hx-request"):
        response = _render_partial(request, "risk")
        response.headers["HX-Trigger"] = json.dumps({"kaiba:control": body})
        return response
    return JSONResponse(body)


_TEMPLATES: Jinja2Templates | None = None


def _templates() -> Jinja2Templates:
    global _TEMPLATES
    if _TEMPLATES is None:
        _TEMPLATES = Jinja2Templates(directory=str(TEMPLATE_DIR))
        _TEMPLATES.env.filters["ts"] = _fmt_ts
        _TEMPLATES.env.filters["num"] = _fmt_num
        _TEMPLATES.env.filters["pct"] = _fmt_pct
        _TEMPLATES.env.filters["dur"] = _fmt_dur
        _TEMPLATES.env.filters["when"] = _fmt_when
        _TEMPLATES.env.globals["NO_WITHDRAW_COPY"] = NO_WITHDRAW_COPY
    return _TEMPLATES


def _fmt_ts(ms: int | None) -> str:
    if not ms:
        return "—"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%H:%M:%S")


def _fmt_pct(value: Any, digits: int = 1) -> str:
    """A return stored as a fraction (0.077) shown as a signed percent (+7.7%)."""
    if value is None:
        return "—"
    try:
        return f"{100 * float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_dur(seconds: Any) -> str:
    if seconds is None:
        return "—"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    if s < 172_800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86_400:.1f}d"


def _fmt_when(ms: Any) -> str:
    """Month-day hour:minute UTC, for anything older than today."""
    if not ms:
        return "—"
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "—"


def _fmt_num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}".rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return str(value)


def _panel_context(name: str) -> dict[str, Any]:
    """Data for one partial. Each entry is independently guarded inside ``data``."""
    if name == "feed":
        return {"events": data.recent_events(limit=60)}
    if name == "metrics":
        return {"overview": data.overview()}
    if name == "positions":
        return {"positions": data.open_positions()}
    if name == "signals":
        return {"signals": data.recent_signals()}
    if name == "decisions":
        return {"decisions": data.recent_decisions()}
    if name == "providers":
        return {"providers": data.provider_meters()}
    if name == "risk":
        return {"risk": data.risk_view()}
    if name == "wallets":
        return {"wallets": data.top_wallets(), "watchlist": data.watchlist_summary()}
    if name == "entities":
        return {"graph": data.entity_graph()}
    if name == "tokens":
        return {"tokens": data.recent_tokens()}
    if name == "journal":
        return {"journal": data.journal_entries()}
    return {}


def _render_partial(request: Request, name: str) -> HTMLResponse:
    password = _password()
    ctx = _panel_context(name)
    ctx["csrf"] = _csrf_for(request, password)
    ctx["auth_on"] = auth.auth_enabled(password)
    return _templates().TemplateResponse(request=request, name=PARTIALS[name], context=ctx)


# --------------------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="Kaiba operator console",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    from dashboard.ultimate import ultimate_page, ultimate_snapshot

    app.add_api_route("/ultimate", ultimate_page, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/api/ultimate", ultimate_snapshot, methods=["GET"])
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.login_failures = []  # per instance: (time, client) of recent wrong passwords

    @app.middleware("http")
    async def loopback_when_unauthenticated(request: Request, call_next):
        """With no password set there is nothing to check, so only loopback is served."""
        if not auth.auth_enabled(_password()) and not auth.is_loopback(_client_host(request)):
            return JSONResponse(
                {
                    "error": "no dashboard password configured; refusing non-loopback clients",
                    "fix": "set KAIBA_DASHBOARD_PASSWORD and restart",
                },
                status_code=403,
            )
        return await call_next(request)

    @app.middleware("http")
    async def authenticate_every_read(request: Request, call_next):
        """Require a session for everything that is not explicitly public.

        FOUND EXPOSED 2026-09-21, on an internet-facing vhost. ``GET /`` correctly
        redirected an anonymous client to ``/login``, and that made the console look
        authenticated -- but the page's own data routes carried no check at all. With no
        cookie whatsoever, ``/api/overview``, ``/api/positions``, ``/api/risk``,
        ``/api/wallets/top``, ``/api/journal`` and ``/api/events`` every one returned
        200 and the full payload: live mode, bankroll, open positions, the graded wallet
        list and the agent's journal. The control POSTs were never exposed -- they call
        ``_require_operator`` -- so this leaked state rather than granting control, and
        there is still no withdraw route to reach. It was a read hole, and a total one.

        An allowlist, not a denylist, because the failure above is exactly what a
        denylist produces: a route added later is public until someone remembers.
        ``/healthz`` stays open so a proxy or uptime check can see the process without a
        credential; it returns a bool and an event id and nothing else.
        """
        path = request.url.path
        public = (
            path in ("/healthz", "/login", "/logout")
            or path.startswith("/static/")
            or path == "/favicon.ico"
        )
        if not public and not _is_authed(request, _password()):
            # 401 for anything a *fragment* or a client fetches, and a redirect only for
            # a real page navigation. An HTMX panel that followed a 303 would splice the
            # entire login page into a div; tests/test_dashboard.py pins both halves.
            if path.startswith(("/api/", "/events/", "/partials/", "/control/", "/terminal/", "/agent/")):
                return JSONResponse({"error": "login_required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.middleware("http")
    async def private_headers(request: Request, call_next):
        """The console is reachable on the internet behind its password (2026-10-20, owner: open it
        anywhere, only me). These keep what it shows from leaking sideways:

        * ``Referrer-Policy: no-referrer`` -- the terminal links out to DexScreener, GMGN and the
          explorers; without it each click told those sites the console's address and the token open;
        * no framing (clickjacking a page with SELL and lane-mode buttons), ``nosniff``, no
          camera/microphone/location, HSTS on https, ``X-Robots-Tag`` so nothing indexes the login;
        * ``Cache-Control: no-store`` on every non-static response, so the book, the wallets and the
          agent's conversation are not left in a shared browser's cache.
        """
        response = await call_next(request)
        h = response.headers
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("X-Robots-Tag", "noindex, nofollow")
        if request.url.scheme == "https":
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")
        return response

    # ---------------------------------------------------------------- pages

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "latest_event_id": data.latest_event_id()})

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, bad: int = 0):
        password = _password()
        if not auth.auth_enabled(password):
            return RedirectResponse("/", status_code=303)
        return _templates().TemplateResponse(
            request=request, name="login.html", context={"bad": bool(bad), "throttled": bad == 2},
            status_code=(429 if bad == 2 else 401) if bad else 200
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form("")):
        configured = _password()
        if not auth.auth_enabled(configured):
            return RedirectResponse("/", status_code=303)
        host = _client_host(request)
        failures: list[tuple[float, str]] = request.app.state.login_failures
        if _login_blocked(failures, host):
            _emit("system", {"event": "dashboard_login_throttled", "client": host}, level="warn")
            return RedirectResponse("/login?bad=2", status_code=303)
        if not auth.check_password(password, configured):
            failures.append((time.time(), host or "?"))
            _emit("system", {"event": "dashboard_login_failed", "client": host}, level="warn")
            return RedirectResponse("/login?bad=1", status_code=303)
        token = auth.issue_session(configured)
        response = RedirectResponse("/ultimate", status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=auth.DEFAULT_TTL_S,
            path="/",
        )
        _emit("system", {"event": "dashboard_login", "client": _client_host(request)})
        return response

    @app.post("/logout")
    async def logout(request: Request):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def command_centre(request: Request):
        password = _password()
        if auth.auth_enabled(password) and not _is_authed(request, password):
            return RedirectResponse("/login", status_code=303)
        context = {
            "nav": NAV,
            "pages": PAGES,
            "page": "/",
            "auth_on": auth.auth_enabled(password),
            "csrf": _csrf_for(request, password),
            "overview": data.overview(),
            "events": data.recent_events(limit=60),
            "positions": data.open_positions(),
            "signals": data.recent_signals(),
            "decisions": data.recent_decisions(),
            "wallets": data.top_wallets(),
            "watchlist": data.watchlist_summary(),
            "graph": data.entity_graph(),
            "tokens": data.recent_tokens(),
            "providers": data.provider_meters(),
            "risk": data.risk_view(),
            "journal": data.journal_entries(),
            "opportunities": data.open_opportunities(),
        }
        return _templates().TemplateResponse(request=request, name="index.html", context=context)

    @app.get("/partials/{name}", response_class=HTMLResponse)
    def partial(request: Request, name: str):
        password = _password()
        if auth.auth_enabled(password) and not _is_authed(request, password):
            raise HTTPException(status_code=401, detail="authentication required")
        if name not in PARTIALS:
            raise HTTPException(status_code=404, detail=f"unknown partial {name!r}")
        return _render_partial(request, name)

    # ---------------------------------------------------------------- json api

    @app.get("/api/overview")
    def api_overview() -> JSONResponse:
        return JSONResponse(data.overview())

    @app.get("/api/positions")
    def api_positions() -> JSONResponse:
        rows = data.open_positions()
        return JSONResponse({"count": len(rows), "positions": rows})

    @app.get("/api/signals")
    def api_signals(limit: int = Query(25, ge=1, le=200)) -> JSONResponse:
        rows = data.recent_signals(limit)
        return JSONResponse({"count": len(rows), "signals": rows})

    @app.get("/api/decisions")
    def api_decisions(limit: int = Query(25, ge=1, le=200)) -> JSONResponse:
        rows = data.recent_decisions(limit)
        return JSONResponse({"count": len(rows), "decisions": rows})

    @app.get("/api/wallets/top")
    def api_top_wallets(limit: int = Query(15, ge=1, le=200)) -> JSONResponse:
        rows = data.top_wallets(limit)
        return JSONResponse({"count": len(rows), "wallets": rows})

    @app.get("/api/entities")
    def api_entities() -> JSONResponse:
        return JSONResponse(data.entity_graph())

    @app.get("/api/providers")
    def api_providers() -> JSONResponse:
        return JSONResponse(data.provider_meters())

    @app.get("/api/risk")
    def api_risk() -> JSONResponse:
        return JSONResponse(data.risk_view())

    @app.get("/api/journal")
    def api_journal(limit: int = Query(10, ge=1, le=100)) -> JSONResponse:
        return JSONResponse(data.journal_entries(limit))

    @app.get("/api/events")
    def api_events(
        after_id: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=500),
        kinds: str | None = None,
    ) -> JSONResponse:
        wanted = [k for k in (kinds or "").split(",") if k] or None
        rows = data.tail_events(after_id=after_id, limit=limit, kinds=wanted)
        return JSONResponse({"count": len(rows), "events": rows, "latest_id": data.latest_event_id()})

    # ---------------------------------------------------------------- sse

    @app.get("/events/stream")
    async def events_stream(
        request: Request,
        after_id: int | None = Query(None, ge=0),
        kinds: str | None = None,
        limit: int = Query(0, ge=0, le=10000),
        poll_ms: int = Query(1000, ge=20, le=10000),
    ) -> EventSourceResponse:
        """Live event feed.

        ``id:`` is the row id from the ``events`` table, so a reconnecting browser sends
        ``Last-Event-ID`` and we resume from there rather than replaying or dropping rows.
        ``limit`` closes the stream after that many events, which is what the tests use;
        left at 0 the stream runs until the client goes away.
        """
        wanted = [k for k in (kinds or "").split(",") if k] or None
        resume = request.headers.get("last-event-id")
        if after_id is not None:
            cursor = after_id
        elif resume and resume.isdigit():
            cursor = int(resume)
        else:
            cursor = max(0, data.latest_event_id() - 25)
        poll_s = poll_ms / 1000.0

        async def publish():
            nonlocal cursor
            sent = 0
            yield ServerSentEvent(comment=f"kaiba stream open, resuming after id {cursor}")
            while True:
                if await request.is_disconnected():
                    break
                batch = data.tail_events(after_id=cursor, limit=200, kinds=wanted)
                for ev in batch:
                    cursor = ev["id"] or cursor
                    for frame in _frames_for(request, ev):
                        yield frame
                    sent += 1
                    if limit and sent >= limit:
                        return
                if not batch:
                    await async_sleep(poll_s)

        return EventSourceResponse(publish(), ping=15)

    # ---------------------------------------------------------------- controls
    # Four buttons and one dial. No fifth button exists, by construction.

    @app.post("/control/pause")
    def control_pause(request: Request, csrf_token: str = Form(None, alias=auth.CSRF_FIELD)):
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        cfg = _current_risk()
        cfg.entries_paused = True
        _save_risk(cfg)
        _emit("risk.halt", {"action": "pause", "entries_paused": True, "by": "operator"}, level="warn")
        return _control_response(request, "pause", {"entries_paused": True})

    @app.post("/control/resume")
    def control_resume(request: Request, csrf_token: str = Form(None, alias=auth.CSRF_FIELD), clear_kill: str = Form("")):
        """Clears pause and reduce-only. The kill switch only clears on an explicit ask."""
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        cfg = _current_risk()
        cfg.entries_paused = False
        cfg.reduce_only = False
        cleared_kill = str(clear_kill).lower() in {"1", "true", "on", "yes"}
        if cleared_kill:
            cfg.kill_switch = False
        _save_risk(cfg)
        _emit(
            "system",
            {"action": "resume", "cleared_kill_switch": cleared_kill, "by": "operator"},
        )
        return _control_response(
            request, "resume", {"entries_paused": False, "kill_switch": cfg.kill_switch}
        )

    @app.post("/control/reduce-only")
    def control_reduce_only(request: Request, csrf_token: str = Form(None, alias=auth.CSRF_FIELD), on: str = Form("1")):
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        cfg = _current_risk()
        cfg.reduce_only = str(on).lower() in {"1", "true", "on", "yes"}
        _save_risk(cfg)
        _emit("system", {"action": "reduce_only", "reduce_only": cfg.reduce_only, "by": "operator"},
              level="warn" if cfg.reduce_only else "info")
        return _control_response(request, "reduce-only", {"reduce_only": cfg.reduce_only})

    @app.post("/control/kill")
    def control_kill(request: Request, csrf_token: str = Form(None, alias=auth.CSRF_FIELD)):
        """Hard stop: every lane's ``effective_mode`` becomes OFF for as long as this is set."""
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        cfg = _current_risk()
        cfg.kill_switch = True
        cfg.entries_paused = True
        _save_risk(cfg)
        _emit(
            "risk.halt",
            {"action": "kill", "kill_switch": True, "by": "operator", "scope": "all lanes"},
            level="error",
        )
        return _control_response(request, "kill", {"kill_switch": True, "entries_paused": True})

    @app.post("/control/close-position")
    def control_close_position(
        request: Request, position_id: str = Form(...), csrf_token: str = Form(None, alias=auth.CSRF_FIELD)
    ):
        """Records an intent only. The execution layer owns the actual exit."""
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        match = [p for p in data.open_positions(limit=500) if p["position_id"] == position_id]
        if not match:
            raise HTTPException(status_code=404, detail=f"no open position {position_id!r}")
        pos = match[0]
        _emit(
            "system",
            {
                "intent": "close_position",
                "position_id": position_id,
                "chain": pos["chain"],
                "token": pos["token"],
                "lane": pos["lane"],
                "requested_by": "operator",
                "executed": False,
                "note": "intent only; the execution layer decides and reports the fill",
            },
            level="warn",
            subject=pos["token"],
        )
        if request.headers.get("hx-request"):
            return _render_partial(request, "positions")
        return JSONResponse({"ok": True, "action": "close-position-intent", "position_id": position_id})

    @app.post("/control/risk-dial")
    def control_risk_dial(
        request: Request,
        csrf_token: str = Form(None, alias=auth.CSRF_FIELD),
        lane: str = Form(None),
        size_pct_max: float = Form(None),
        size_pct_min: float = Form(None),
        daily_loss_pct: float = Form(None),
    ):
        """Bounded sliders.

        Out-of-envelope values are refused with 400 and a reason rather than quietly
        clamped: an operator who asked for 40% of bankroll needs to know the answer was no.
        """
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        cfg = _current_risk()
        bounds = cfg.bounds
        changed: dict[str, Any] = {}

        if size_pct_max is not None or size_pct_min is not None:
            if not lane:
                raise HTTPException(status_code=400, detail="lane is required to change a size dial")
            lane_key = _resolve_lane(cfg, lane)
            if lane_key is None:
                raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
            lane_cfg = cfg.lanes[lane_key]
            new_max = lane_cfg.size_pct_max if size_pct_max is None else float(size_pct_max)
            new_min = lane_cfg.size_pct_min if size_pct_min is None else float(size_pct_min)
            if new_max < 0 or new_min < 0:
                raise HTTPException(status_code=400, detail="size percentages cannot be negative")
            if new_max > bounds.max_size_pct_bankroll:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"size_pct_max {new_max} exceeds bounds.max_size_pct_bankroll "
                        f"{bounds.max_size_pct_bankroll}; the envelope is operator-owned and "
                        "cannot be widened from here"
                    ),
                )
            if new_min > new_max:
                raise HTTPException(
                    status_code=400, detail=f"size_pct_min {new_min} is above size_pct_max {new_max}"
                )
            lane_cfg.size_pct_min = new_min
            lane_cfg.size_pct_max = new_max
            changed["lane"] = str(getattr(lane_key, "value", lane_key))
            changed["size_pct_min"] = new_min
            changed["size_pct_max"] = new_max

        if daily_loss_pct is not None:
            value = float(daily_loss_pct)
            if value <= 0:
                raise HTTPException(status_code=400, detail="daily_loss_pct must be greater than zero")
            if value > bounds.max_daily_loss_pct:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"daily_loss_pct {value} exceeds bounds.max_daily_loss_pct "
                        f"{bounds.max_daily_loss_pct}"
                    ),
                )
            protection = dict(cfg.protection or {})
            protection["daily_loss_pct"] = value
            cfg.protection = protection
            # Chains with a real bankroll also get the derived base-unit stop. Chains whose
            # bankroll is still zero are left alone: deriving 0 there would read as "stop at
            # no loss", which is the opposite of true.
            derived: dict[str, int] = {}
            for chain_key, budget in (cfg.chains or {}).items():
                if budget.bankroll_base_units and budget.bankroll_base_units > 0:
                    stop = int(budget.bankroll_base_units * value / 100)
                    budget.daily_loss_stop_base_units = stop
                    derived[str(getattr(chain_key, "value", chain_key))] = stop
            changed["daily_loss_pct"] = value
            changed["derived_stops_base_units"] = derived

        if not changed:
            raise HTTPException(status_code=400, detail="nothing to change")

        _save_risk(cfg)
        _emit("param.change", {"source": "dashboard risk dial", "by": "operator", **changed})
        return _control_response(request, "risk-dial", {"changed": changed})

    _install_console_pages(app)
    from dashboard import terminal_live

    terminal_live.install(app)
    terminal_live.install_agent(app)
    # GMGN account visibility (Codex, TERMINAL-GMGN-SUPPORT-20261020, wired as handed off): GET
    # serves the last explicit check only; POST runs one read-only `gmgn-cli portfolio info`
    # behind the session, the CSRF token and a 30 s cooldown. It accepts no address or credential.
    from dashboard import gmgn_account, gmgn_account_routes

    gmgn_account_routes.install(app, gmgn_account.read_account, authenticate=_require_operator, csrf=_require_csrf)
    return app


# --------------------------------------------------------------------------------------
# trades, wallets, agent chat, commands (2026-10-18)
# --------------------------------------------------------------------------------------


def _slug(value: str | None) -> str | None:
    return value if value and SLUG.match(value) else None


def _page(request: Request, page: str, template: str, **ctx: Any) -> HTMLResponse:
    password = _password()
    context = {"pages": PAGES, "page": page, "auth_on": auth.auth_enabled(password),
               "csrf": _csrf_for(request, password), "mode": data.current_mode(), **ctx}
    return _templates().TemplateResponse(request=request, name=template, context=context)


def _json_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _warm_in_background() -> None:
    """Production only (KAIBA_ENV=prod): fill the slow read caches once after start-up."""
    import os
    import threading

    if os.environ.get("KAIBA_ENV") != "prod":
        return

    def warm() -> None:
        time.sleep(2)
        data.warm_caches()
        for fn in (trading.wallet_grades, trading.paper_scoreboard):
            try:
                fn()
            except Exception as exc:  # pragma: no cover - best effort
                log.info("dashboard: warming failed: %s", exc)
        # The GMGN wallet lists take ~13 s to build cold (graded wallets under idle IO); rebuild them
        # here before the 15-minute cache runs out so the Wallets page never waits on that read.
        while True:
            wallet_export.refresh()
            time.sleep(wallet_export.LIST_CACHE_S - 120)

    threading.Thread(target=warm, name="dash-warm", daemon=True).start()


def _install_console_pages(app: FastAPI) -> None:
    app.state.chat = agent_chat.ChatService(emit=commands._default_emit)
    app.state.runner = commands.Runner()
    _warm_in_background()

    # ---------------------------------------------------------------- pages

    @app.get("/trades", response_class=HTMLResponse)
    def trades_page(
        request: Request,
        days: int = Query(12, ge=1, le=30),
        paper_days: int = Query(2, ge=1, le=12),
        lane: str | None = None,
        chain: str | None = None,
    ):
        lane, chain = _slug(lane), _slug(chain)
        return _page(
            request, "/trades", "trades.html",
            summary=trading.trade_summary(days),
            scoreboard=trading.paper_scoreboard(),
            twin=trading.twin_exits(),
            trials=trading.trial_results(),
            live=trading.closed_trades("live", days=30, limit=100),
            paper=trading.closed_trades("shadow", days=paper_days, limit=200, lane=lane, chain=chain),
            open_positions=data.open_positions(limit=300),
            report=trading.latest_paper_report(),
            filters={"days": days, "paper_days": paper_days, "lane": lane, "chain": chain},
        )

    @app.get("/wallets", response_class=HTMLResponse)
    def wallets_page(request: Request):
        return _page(
            request, "/wallets", "wallets_page.html",
            exports=trading.wallet_exports(),
            hunts=trading.hunt_status(),
            grades=trading.wallet_grades(),
            watchlist=data.watchlist_summary(),
            wallets=data.top_wallets(25),
        )

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, thread: str = "main"):
        chat: agent_chat.ChatService = request.app.state.chat
        thread = _slug(thread) or "main"
        ok, why = agent_chat.available()
        threads = chat.threads()
        if thread not in threads:
            threads.append(thread)
        current = chat.current()
        return _page(
            request, "/chat", "chat.html",
            thread=thread, threads=threads, turns=chat.history(thread), available=ok, why=why,
            running=current.public() if current is not None and current.status == "running" else None,
            max_chars=agent_chat.MAX_MESSAGE_CHARS,
        )

    @app.get("/commands", response_class=HTMLResponse)
    def commands_page(request: Request):
        runner: commands.Runner = request.app.state.runner
        groups: dict[str, list[dict[str, Any]]] = {}
        for cmd in runner.commands():
            groups.setdefault(cmd.group, []).append(cmd.public())
        return _page(
            request, "/commands", "commands.html",
            groups=groups, runs=runner.recent(20), health=trading.system_health(),
        )

    # ---------------------------------------------------------------- json

    @app.get("/api/trades")
    def api_trades(days: int = Query(12, ge=1, le=30), paper_days: int = Query(2, ge=1, le=12)) -> JSONResponse:
        return JSONResponse({
            "summary": trading.trade_summary(days),
            "live": trading.closed_trades("live", days=30, limit=100),
            "paper": trading.closed_trades("shadow", days=paper_days, limit=200),
        })

    @app.get("/api/paper/scoreboard")
    def api_paper_scoreboard() -> JSONResponse:
        return JSONResponse(trading.paper_scoreboard())

    @app.get("/api/twin-exits")
    def api_twin_exits() -> JSONResponse:
        return JSONResponse(trading.twin_exits())

    @app.get("/api/wallets/gathered")
    def api_wallets_gathered() -> JSONResponse:
        return JSONResponse({"exports": trading.wallet_exports(), "hunts": trading.hunt_status(),
                             "grades": trading.wallet_grades()})

    @app.get("/api/wallets/export/{name}")
    def api_wallet_export(name: str):
        path = trading.export_path(name)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="no such export")
        return FileResponse(path, media_type="application/json", filename=name)

    @app.get("/api/system")
    def api_system() -> JSONResponse:
        return JSONResponse(trading.system_health())

    @app.get("/api/chat/history")
    def api_chat_history(request: Request, thread: str = "main") -> JSONResponse:
        chat: agent_chat.ChatService = request.app.state.chat
        thread = _slug(thread) or "main"
        return JSONResponse({"thread": thread, "turns": chat.history(thread)})

    @app.get("/api/commands")
    def api_commands(request: Request) -> JSONResponse:
        runner: commands.Runner = request.app.state.runner
        return JSONResponse({"commands": [c.public() for c in runner.commands()]})

    @app.get("/api/commands/runs")
    def api_command_runs(request: Request) -> JSONResponse:
        runner: commands.Runner = request.app.state.runner
        return JSONResponse({"runs": runner.recent(30)})

    @app.get("/api/commands/runs/{run_id}")
    def api_command_run(request: Request, run_id: str, since: int = Query(0, ge=0)) -> JSONResponse:
        runner: commands.Runner = request.app.state.runner
        run = runner.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return JSONResponse(run.to_dict(since))

    # ---------------------------------------------------------------- writes

    @app.post("/control/chat")
    def control_chat(
        request: Request,
        message: str = Form(""),
        thread: str = Form("main"),
        csrf_token: str = Form(None, alias=auth.CSRF_FIELD),
    ):
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        chat: agent_chat.ChatService = request.app.state.chat
        try:
            turn = chat.send(thread, message)
        except agent_chat.ChatError as exc:
            return _json_error(exc.status, exc.message)
        return JSONResponse({"ok": True, "turn_id": turn.turn_id, "thread": turn.thread})

    @app.post("/control/chat-stop")
    def control_chat_stop(request: Request, csrf_token: str = Form(None, alias=auth.CSRF_FIELD)):
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        chat: agent_chat.ChatService = request.app.state.chat
        return JSONResponse({"ok": True, "stopped": chat.stop()})

    @app.post("/control/command")
    def control_command(
        request: Request,
        name: str = Form(""),
        arg: str = Form(""),
        confirm: str = Form(""),
        csrf_token: str = Form(None, alias=auth.CSRF_FIELD),
    ):
        password = _require_operator(request)
        _require_csrf(request, password, csrf_token)
        runner: commands.Runner = request.app.state.runner
        try:
            run = runner.start(name, arg or None, confirm or None)
        except commands.CommandError as exc:
            return _json_error(exc.status, exc.message)
        return JSONResponse({"ok": True, "run_id": run.run_id, "label": run.label})

    # ---------------------------------------------------------------- streams

    @app.get("/events/chat")
    async def chat_stream(request: Request, turn_id: str = Query(..., max_length=40)) -> EventSourceResponse:
        chat: agent_chat.ChatService = request.app.state.chat

        async def publish():
            sent = 0
            while True:
                if await request.is_disconnected():
                    return
                turn = chat.get(turn_id)
                if turn is None:
                    yield ServerSentEvent(event="chat", data=json.dumps({"status": "unknown", "events": []}))
                    return
                body = turn.public(sent)
                sent = body["event_count"]
                if body["events"] or turn.status != "running":
                    yield ServerSentEvent(event="chat", data=json.dumps(body, default=str))
                if turn.status != "running":
                    return
                await async_sleep(0.4)

        return EventSourceResponse(publish(), ping=15)

    @app.get("/events/command/{run_id}")
    async def command_stream(request: Request, run_id: str) -> EventSourceResponse:
        runner: commands.Runner = request.app.state.runner

        async def publish():
            sent = 0
            while True:
                if await request.is_disconnected():
                    return
                run = runner.get(run_id)
                if run is None:
                    yield ServerSentEvent(event="run", data=json.dumps({"status": "unknown", "lines": []}))
                    return
                body = run.to_dict(sent)
                sent = body["line_count"]
                if body["lines"] or run.status != "running":
                    yield ServerSentEvent(event="run", data=json.dumps(body, default=str))
                if run.status != "running":
                    return
                await async_sleep(0.5)

        return EventSourceResponse(publish(), ping=15)


def _resolve_lane(cfg, lane: str):
    for key in cfg.lanes:
        if str(getattr(key, "value", key)) == lane:
            return key
    return None


def _frames_for(request: Request, ev: dict[str, Any]) -> list[ServerSentEvent]:
    """One event -> several named SSE frames, all carrying the same ``id:``.

    ``feed`` carries a rendered row so HTMX can ``sse-swap`` it straight into the stream
    panel. The kind-named and panel-named frames carry JSON and are what the other panels
    hang an ``hx-trigger="sse:positions"`` off.
    """
    event_id = str(ev.get("id") or "")
    frames: list[ServerSentEvent] = []
    try:
        row = _templates().TemplateResponse(
            request=request, name="partials/feed_row.html", context={"event": ev}
        )
        html = row.body.decode("utf-8").strip()
    except Exception as exc:  # a template slip must not kill the stream
        log.warning("dashboard: feed row render failed: %s", exc)
        html = f"<div class=\"feed-row\"><b>{ev.get('kind')}</b></div>"
    frames.append(ServerSentEvent(id=event_id, event="feed", data=html))

    compact = json.dumps(
        {
            "id": ev.get("id"),
            "kind": ev.get("kind"),
            "level": ev.get("level"),
            "chain": ev.get("chain"),
            "subject": ev.get("subject"),
            "summary": ev.get("summary"),
            "ts_ms": ev.get("ts_ms"),
        }
    )
    channels: list[str] = [str(ev.get("kind"))]
    channels.extend(PANEL_BY_KIND.get(str(ev.get("kind")), ()))
    seen: set[str] = set()
    for channel in channels:
        if channel and channel not in seen:
            seen.add(channel)
            frames.append(ServerSentEvent(id=event_id, event=channel, data=compact))
    return frames


app = create_app()
