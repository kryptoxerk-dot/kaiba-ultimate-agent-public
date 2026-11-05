"""The trading terminal: pages, live partials and three controls (2026-10-20).

Pages: ``/terminal`` (alpha watchlist | token or overview over the book | agent activity and the trade
ticket), ``/t/{chain}/{address}`` (the terminal with that token open), ``/w/{chain}/{address}`` (a
wallet), ``/alpha`` (every early-signal source), ``/lp`` (the LP desk), ``/autopilot`` (the lanes that trade on their own)
and ``/intel/wallets`` (wallet lists ready to import into GMGN, served at
``/wallets/gmgn/<list>.json``, and the graded wallets). Reads come from :mod:`dashboard.intel` and
:mod:`dashboard.wallet_export`; nothing here touches the database directly. Redesigned 2026-10-20
in the visual system of Codex's design study (docs/design/kaiba-terminal); every page extends
``term_layout.html``.

Controls, each behind the session and the CSRF token like every other write on this console:

* ``/control/lane-mode`` sets one lane's mode. ``off`` and ``shadow`` (paper) are one click.
  ``canary`` and ``live`` need the lane's name typed back as ``ARM <lane>``, and a mode above the
  envelope's ``max_lane_mode`` is refused rather than silently clamped -- the operator has to know
  the answer was no. Nothing here can widen ``bounds``.
* ``/control/paper-buy`` asks for an entry through ``engine.submit_agent_intent``: the MANUAL
  lane, every gate an automated signal passes (kill switch, pause, budgets, dossier freshness,
  the risk gate, the size ladder). On the box MANUAL is shadow, so it is a paper trade; if the
  operator ever arms MANUAL, the button says LIVE and needs ``BUY LIVE`` typed.
* ``/control/exit`` records an operator close request that protection drains and executes
  (``Watchdog._classify``: SYSTEM ``intent: close_position``). The owner rule of 2026-10-06 --
  the agent never sells on its own -- is unaffected: this is the owner's request.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from dashboard import intel, trading, wallet_export
from kaiba.hunters import baw_status, grok_alpha_processor
from kaiba.learning import labs
from kaiba.ops import network_status

log = logging.getLogger(__name__)

STREAM_FILTERS = [
    ("all", "All", ()),
    ("confluence", "Confluence", ("confluence",)),
    ("launch", "Launches", ("launch",)),
    ("graduation", "Graduations", ("graduation",)),
    ("signal", "Signals", ("signal", "entry")),
    ("trending", "Trending", ("trending",)),
    ("listing", "Listings", ("listing",)),
    ("claim", "Airdrop/claims", ("claim",)),
    ("governance", "Governance", ("governance",)),
]
LIVE_MODES = {"canary", "live"}


# --------------------------------------------------------------------------------------
# formatting filters
# --------------------------------------------------------------------------------------

_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def fmt_px(value: Any) -> str:
    """Prices the way traders read them: 0.0₅2224 for 0.0000022241, never 2.2e-06."""
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return "—"
    if not d.is_finite() or d <= 0:
        return "—" if d != 0 else "0"
    if d >= 1000:
        return f"{d:,.0f}"
    if d >= 1:
        return f"{d:,.2f}" if d >= 100 else f"{d:,.4f}"
    text = f"{d:.20f}".split(".")[1]
    zeros = len(text) - len(text.lstrip("0"))
    digits = text[zeros:zeros + 4].rstrip("0") or "0"
    if zeros >= 4:
        return f"0.0{str(zeros).translate(_SUB)}{digits}"
    return f"0.{'0' * zeros}{digits}"


def fmt_usd(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if v >= size:
            return f"{sign}${v / size:.2f}{unit}" if v / size < 100 else f"{sign}${v / size:.0f}{unit}"
    return f"{sign}${v:,.2f}" if v < 100 else f"{sign}${v:,.0f}"


def fmt_ret(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v * 100:+.1f}%"


def fmt_ret2(value: Any) -> str:
    """A small return (LP positions earn tenths of a percent): two decimals."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v * 100:+.2f}%"


def fmt_ago(ms: Any) -> str:
    try:
        sec = max(0.0, (intel.now_ms() - int(ms)) / 1000)
    except (TypeError, ValueError):
        return "—"
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m"
    if sec < 86400:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def fmt_hms(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def fmt_field(field: dict[str, Any]) -> str:
    value, kind = field.get("value"), field.get("fmt")
    if value is None:
        return "—"
    if kind == "px":
        return "$" + fmt_px(value)
    if kind == "usd":
        return fmt_usd(value)
    if kind == "int":
        return f"{int(value):,}"
    if kind == "pct":
        v = float(value)
        return f"{v * 100:.1f}%" if v <= 1 else f"{v:.1f}%"
    if kind == "bps":
        return f"{float(value) / 100:.1f}%"
    return str(value)


def fmt_pctx(value: Any) -> str:
    """A share stored either as a fraction (0.59) or as a percent (59)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v * 100:.1f}%" if abs(v) <= 1 else f"{v:.1f}%"


def fmt_n0(value: Any) -> str:
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return "—"


def short(addr: Any, head: int = 4, tail: int = 4) -> str:
    text = str(addr or "")
    return text if len(text) <= head + tail + 1 else f"{text[:head]}…{text[-tail:]}"


def _series_json(series: dict[str, Any] | None) -> str:
    return json.dumps(series or {"points": []}, separators=(",", ":"), default=str)


def feed_label(feed: Any) -> str:
    """``binance:topic_rush:latest`` -> ``Binance topic rush``: a feed id as a person reads it."""
    parts = [p for p in str(feed or "").split(":") if p and p not in ("latest", "new", "v1", "v2")]
    text = " ".join(p.replace("_", " ") for p in parts).strip()
    names = {"gmgn": "GMGN", "binance": "Binance", "bwe": "BWE", "dexscreener": "DexScreener"}
    if not text:
        return "feed"
    head, _, rest = text.partition(" ")
    return (names.get(head.lower(), head.capitalize()) + (" " + rest if rest else ""))[:40]


def fmt_pct100(value: Any) -> float:
    """A share stored as a fraction (0.59) or a percent (59), as 0-100 for a bar width."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    v = v * 100 if abs(v) <= 1 else v
    return round(max(0.0, min(100.0, v)), 1)


def entry_marks(tok: dict[str, Any]) -> str:
    """Our entries on a token, as chart marks -- only on a USD price line, where they mean something."""
    if (tok.get("series") or {}).get("unit") != "USD":
        return "[]"
    marks = []
    for p in tok.get("positions") or []:
        price = intel._f(p.get("entry_price_usd"))
        if price and p.get("opened_ms"):
            marks.append({"t": int(p["opened_ms"]), "p": price, "live": not p.get("paper", True)})
    return json.dumps(marks, separators=(",", ":"))


def status_bar() -> dict[str, Any]:
    """The bottom line of every terminal page: is protection pricing the book, are the agent's
    gateways up, is anything live. A mark older than two minutes counts as stale."""
    from dashboard import agent_view

    book = intel.book()
    now = intel.now_ms()
    marks = [int(p["mark"]["updated_ms"]) for p in book["open"] if p.get("mark") and p["mark"].get("updated_ms")]
    stale = sum(1 for p in book["open"]
                if not p.get("mark") or now - int(p["mark"].get("updated_ms") or 0) > 120_000)
    gateways = agent_view.gateways()
    return {"open": len(book["open"]), "marked": len(marks), "marked_ms": max(marks) if marks else None,
            "stale": stale, "gateways": gateways,
            "gateways_up": sum(1 for g in gateways if g["state"] == "active"),
            "live_lanes": intel.ticker().get("live_lanes") or []}


# --------------------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------------------


def install(app: FastAPI) -> None:
    from dashboard import app as A  # noqa: N812 - the console's helpers, loaded by now

    env = A._templates().env
    env.filters.update({"px": fmt_px, "usd": fmt_usd, "ret": fmt_ret, "ret2": fmt_ret2, "ago": fmt_ago, "hms": fmt_hms,
                        "field": fmt_field, "short": short, "series": _series_json, "pctx": fmt_pctx,
                        "n0": fmt_n0, "pct100": fmt_pct100, "entry_marks": entry_marks,
                        "feed": feed_label})
    env.globals["STREAM_KINDS"] = intel.STREAM_KINDS

    def partial(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return A._templates().TemplateResponse(request=request, name=f"partials/{name}", context=ctx)

    def page(request: Request, path: str, template: str, **ctx: Any) -> HTMLResponse:
        return A._page(request, path, template, ticker=intel.ticker(), status_bar=status_bar(), **ctx)

    def terminal(request: Request, selected: dict[str, Any] | None, tab: str, chain: str | None) -> HTMLResponse:
        return page(
            request, "/terminal", "live_terminal.html",
            watch=intel.alpha_watch(60, kinds=kinds_of(tab) or None, chain=chain), activity=intel.activity(),
            book=intel.book(), lanes=intel.lane_matrix(), scoreboard=trading.paper_scoreboard(),
            filters=STREAM_FILTERS, tab=tab, chain=chain, selected=selected,
        )

    def token_ctx(chain: str, address: str) -> dict[str, Any]:
        mode = intel.manual_lane_mode()
        return {"tok": intel.token_profile(chain, address), "manual_mode": mode,
                "manual_live": mode in LIVE_MODES}

    def kinds_of(tab: str) -> tuple[str, ...]:
        return next((k for slug, _label, k in STREAM_FILTERS if slug == tab), ())

    # ---------------------------------------------------------------- pages

    @app.get("/terminal", response_class=HTMLResponse)
    def terminal_page(request: Request, t: str | None = None, tab: str = "all", chain: str | None = None):
        selected = None
        if t and ":" in t:
            c, a = t.split(":", 1)
            c = intel.valid_chain(c)
            a = intel.clean_address(c, a) if c else None
            if c and a:
                selected = token_ctx(c, a)
        tab = tab if any(tab == slug for slug, _l, _k in STREAM_FILTERS) else "all"
        return terminal(request, selected, tab, intel.valid_chain(chain))

    @app.get("/t/{chain}/{address}", response_class=HTMLResponse)
    def token_page(request: Request, chain: str, address: str):
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, address) if c else None
        if not c or not a:
            raise HTTPException(status_code=404, detail="not a token address on a chain we watch")
        return terminal(request, token_ctx(c, a), "all", None)

    @app.get("/w/{chain}/{address}", response_class=HTMLResponse)
    def wallet_page(request: Request, chain: str, address: str):
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, address) if c else None
        if not c or not a:
            raise HTTPException(status_code=404, detail="not a wallet address on a chain we watch")
        return page(request, "/intel/wallets", "wallet.html", w=intel.wallet_profile(c, a),
                    behavior=labs.wallet_behavior(a) if c == "sol" else None)

    @app.get("/intel/wallets", response_class=HTMLResponse)
    def wallets_intel(request: Request, chain: str | None = None):
        chain = intel.valid_chain(chain)
        return page(request, "/intel/wallets", "wallets_intel.html", wallets=intel.top_wallets(chain, 40),
                    chain=chain, lists=wallet_export.summary(), best=labs.best_wallets())

    @app.get("/wallets/gmgn/{name}")
    def gmgn_list(name: str, part: int = Query(1, ge=1, le=50)):
        """One list as GMGN follow-list JSON (``.json``) or ``address:name`` lines (``.txt``)."""
        stem, _dot, ext = name.rpartition(".")
        if ext not in ("json", "txt"):
            raise HTTPException(status_code=404, detail="ask for .json or .txt")
        found = wallet_export.get(stem)
        if found is None or part > found["parts"]:
            raise HTTPException(status_code=404, detail="no such list")
        body = wallet_export.as_json(found, part) if ext == "json" else wallet_export.as_text(found, part)
        media = "application/json" if ext == "json" else "text/plain; charset=utf-8"
        disposition = f'attachment; filename="{wallet_export.filename(found, part, ext)}"'
        return Response(body, media_type=media, headers={"Content-Disposition": disposition})

    @app.get("/alpha", response_class=HTMLResponse)
    def alpha_page(request: Request, tab: str = "all", chain: str | None = None):
        tab = tab if any(tab == slug for slug, _l, _k in STREAM_FILTERS) else "all"
        chain = intel.valid_chain(chain)
        return page(
            request, "/alpha", "alpha.html",
            stream=intel.alpha_stream(200, kinds=kinds_of(tab) or None, chain=chain),
            health=intel.source_health(), launches=intel.launches(40, chain), filters=STREAM_FILTERS,
            tab=tab, chain=chain, grok=grok_alpha_processor.overview(12), baw=baw_status.status(),
            networks=network_status.status(),
        )

    @app.get("/labs", response_class=HTMLResponse)
    def labs_page(request: Request, ir: float | None = Query(None, ge=0, le=100),
                  h: float | None = Query(None, ge=0, le=24 * 365), oor: bool = False,
                  pool: str = Query("", max_length=60)):
        """The two research labs; ``ir``/``h``/``oor``/``pool`` run the LP position check."""
        pool = (intel.clean_address("sol", pool) or "") if pool else ""
        check = labs.lp_decision(ir / 100, h, oor, pool) if ir is not None and h is not None else None
        return page(request, "/labs", "labs.html", lab=labs.overview(), check=check,
                    form={"ir": ir, "h": h, "oor": oor, "pool": pool})

    @app.get("/lp", response_class=HTMLResponse)
    def lp_page(request: Request) -> HTMLResponse:
        """The LP desk: the paper tracks, their pass lines and the live path's state."""
        return page(request, "/lp", "lp.html", board=labs.lp_board())

    @app.get("/lp/body", response_class=HTMLResponse)
    def lp_body(request: Request) -> HTMLResponse:
        return partial(request, "lp_body.html", board=labs.lp_board())

    @app.get("/autopilot", response_class=HTMLResponse)
    def autopilot_page(request: Request):
        return page(request, "/autopilot", "autopilot.html", lanes=intel.lane_matrix(), book=intel.book())

    @app.get("/go")
    def go(request: Request, q: str = Query("", max_length=120)):
        found = intel.resolve(q)
        if found["kind"] == "token":
            return RedirectResponse(f"/t/{found['chain']}/{found['address']}", status_code=303)
        if found["kind"] == "wallet":
            return RedirectResponse(f"/w/{found['chain']}/{found['address']}", status_code=303)
        return page(request, "/terminal", "search.html", found=found, q=q)

    # ---------------------------------------------------------------- live partials

    @app.get("/terminal/ticker", response_class=HTMLResponse)
    def ticker_partial(request: Request):
        return partial(request, "t_ticker.html", ticker=intel.ticker())

    @app.get("/terminal/status", response_class=HTMLResponse)
    def status_partial(request: Request):
        return partial(request, "t_status.html", status_bar=status_bar())

    @app.get("/terminal/watch", response_class=HTMLResponse)
    def watch_partial(request: Request, tab: str = "all", chain: str | None = None):
        return partial(request, "t_watch.html", watch=intel.alpha_watch(60, kinds=kinds_of(tab) or None,
                                                                          chain=intel.valid_chain(chain)))

    @app.get("/terminal/activity", response_class=HTMLResponse)
    def activity_partial(request: Request):
        return partial(request, "t_activity.html", activity=intel.activity())

    @app.get("/terminal/stream", response_class=HTMLResponse)
    def stream_partial(request: Request, tab: str = "all", chain: str | None = None, n: int = Query(70, ge=10, le=200)):
        return partial(request, "t_stream.html", stream=intel.alpha_stream(n, kinds=kinds_of(tab) or None,
                                                                            chain=intel.valid_chain(chain)))

    @app.get("/terminal/book", response_class=HTMLResponse)
    def book_partial(request: Request):
        return partial(request, "t_book.html", book=intel.book())

    @app.get("/terminal/token/{chain}/{address}", response_class=HTMLResponse)
    def token_partial(request: Request, chain: str, address: str):
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, address) if c else None
        if not c or not a:
            raise HTTPException(status_code=404, detail="not a token address on a chain we watch")
        return partial(request, "t_token.html", csrf=A._csrf_for(request, A._password()), **token_ctx(c, a))

    @app.get("/api/terminal/token/{chain}/{address}")
    def token_api(chain: str, address: str) -> JSONResponse:
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, address) if c else None
        if not c or not a:
            return JSONResponse({"ok": False, "error": "bad address"}, status_code=404)
        return JSONResponse(json.loads(json.dumps(intel.token_profile(c, a), default=str)))

    @app.get("/api/terminal/wallet/{chain}/{address}")
    def wallet_api(chain: str, address: str) -> JSONResponse:
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, address) if c else None
        if not c or not a:
            return JSONResponse({"ok": False, "error": "bad address"}, status_code=404)
        return JSONResponse(json.loads(json.dumps(intel.wallet_profile(c, a), default=str)))

    @app.get("/api/terminal/stream")
    def stream_api(tab: str = "all", chain: str | None = None, n: int = Query(100, ge=1, le=300)) -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(
            intel.alpha_stream(n, kinds=kinds_of(tab) or None, chain=intel.valid_chain(chain)), default=str)))

    # ---------------------------------------------------------------- controls

    @app.post("/control/lane-mode")
    def control_lane_mode(
        request: Request,
        lane: str = Form(...),
        mode: str = Form(...),
        confirm: str = Form(""),
        csrf_token: str = Form(None, alias="_csrf"),
    ) -> JSONResponse:
        password = A._require_operator(request)
        A._require_csrf(request, password, csrf_token)
        mode = str(mode).strip().lower()
        if mode not in intel.MODE_ORDER:
            raise HTTPException(status_code=400, detail=f"unknown mode {mode!r}")
        cfg = A._current_risk()
        key = A._resolve_lane(cfg, lane)
        if key is None:
            raise HTTPException(status_code=400, detail=f"unknown lane {lane!r}")
        name = str(getattr(key, "value", key))
        if mode in LIVE_MODES and confirm.strip() != f"ARM {name}":
            raise HTTPException(status_code=400, detail=f"type ARM {name} to put real money behind this lane")
        ceiling = str(getattr(getattr(cfg.bounds, "max_lane_mode", None), "value", "live"))
        if intel.MODE_ORDER.index(mode) > intel.MODE_ORDER.index(ceiling):
            raise HTTPException(status_code=400, detail=f"{mode} is above the envelope's max_lane_mode {ceiling}; "
                                                        "the envelope is operator-owned and is not widened from here")
        from kaiba.core.schemas import LaneMode

        lane_cfg = cfg.lanes[key]
        before = str(getattr(lane_cfg.mode, "value", lane_cfg.mode))
        lane_cfg.mode = LaneMode(mode)
        A._save_risk(cfg)
        A._emit("param.change", {"source": "terminal lane mode", "by": "operator", "lane": name,
                                 "mode_before": before, "mode_after": mode},
                level="warn" if mode in LIVE_MODES else "info")
        intel.clear_caches()
        return JSONResponse({"ok": True, "lane": name, "mode_before": before, "mode": mode})

    @app.post("/control/paper-buy")
    def control_paper_buy(
        request: Request,
        chain: str = Form(...),
        token: str = Form(...),
        confirm: str = Form(""),
        csrf_token: str = Form(None, alias="_csrf"),
    ) -> JSONResponse:
        password = A._require_operator(request)
        A._require_csrf(request, password, csrf_token)
        c = intel.valid_chain(chain)
        a = intel.clean_address(c, token) if c else None
        if not c or not a:
            raise HTTPException(status_code=400, detail="not a token address on a chain we watch")
        mode = intel.manual_lane_mode()
        if mode in LIVE_MODES and confirm.strip() != "BUY LIVE":
            raise HTTPException(status_code=400, detail="the manual lane is live: type BUY LIVE to spend real money")
        from kaiba.execution import engine

        result = engine.submit_agent_intent(c, a, thesis="operator: terminal buy")
        A._emit("system", {"action": "terminal_buy", "by": "operator", "chain": c, "token": a,
                           "mode": result.get("mode"), "decision": result.get("action"),
                           "blockers": result.get("blockers")}, subject=a)
        intel.clear_caches()
        return JSONResponse(json.loads(json.dumps(result, default=str)))

    @app.post("/control/exit")
    def control_exit(
        request: Request,
        position_id: str = Form(...),
        pct: int = Form(100),
        csrf_token: str = Form(None, alias="_csrf"),
    ) -> JSONResponse:
        password = A._require_operator(request)
        A._require_csrf(request, password, csrf_token)
        if pct not in (25, 50, 100):
            raise HTTPException(status_code=400, detail="sell 25, 50 or 100 percent")
        match = [p for p in intel.book()["open"] if p["position_id"] == position_id]
        if not match:
            intel.clear_caches()
            match = [p for p in intel.book()["open"] if p["position_id"] == position_id]
        if not match:
            raise HTTPException(status_code=404, detail=f"no open position {position_id!r}")
        pos = match[0]
        A._emit("system", {"intent": "close_position", "position_id": position_id, "pct": pct,
                           "chain": pos["chain"], "token": pos["token"], "lane": pos["lane"],
                           "requested_by": "operator", "executed": False,
                           "note": f"operator: terminal sell {pct}%"},
                level="warn", subject=pos["token"])
        intel.clear_caches()
        return JSONResponse({"ok": True, "position_id": position_id, "pct": pct,
                             "note": "protection executes it on its next tick"})


# --------------------------------------------------------------------------------------
# the Hermes operator agent (2026-10-20): watch it, prompt it, reach Telegram
# --------------------------------------------------------------------------------------


def install_agent(app: FastAPI) -> None:
    """``/agent`` and its partials, plus two owner actions. Prompting reuses the console's chat
    (``/control/chat`` + ``/events/chat``: one ``hermes chat`` turn on the same profile)."""
    from dashboard import agent_view
    from dashboard import app as A  # noqa: N812

    def partial(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return A._templates().TemplateResponse(request=request, name=f"partials/{name}", context=ctx)

    @app.get("/agent", response_class=HTMLResponse)
    def agent_page(request: Request):
        chat = request.app.state.chat
        ok, why = agent_view.available()
        return A._page(request, "/agent", "agent.html", ticker=intel.ticker(), status_bar=status_bar(),
                       status=agent_view.status(), telegram=agent_view.telegram(),
                       background=agent_view.background(), cron_text=agent_view.cron_jobs(), journal=_journal(),
                       threads=chat.threads(), available=ok, why=why, no_dock=True)

    @app.get("/agent/telegram", response_class=HTMLResponse)
    def agent_telegram(request: Request):
        return partial(request, "a_telegram.html", telegram=agent_view.telegram())

    @app.get("/agent/background", response_class=HTMLResponse)
    def agent_background(request: Request):
        return partial(request, "a_background.html", background=agent_view.background())

    @app.get("/agent/status", response_class=HTMLResponse)
    def agent_status(request: Request):
        return partial(request, "a_status.html", status=agent_view.status())

    @app.post("/control/agent-telegram")
    def control_agent_telegram(request: Request, text: str = Form(""),
                               csrf_token: str = Form(None, alias="_csrf")) -> JSONResponse:
        password = A._require_operator(request)
        A._require_csrf(request, password, csrf_token)
        try:
            result = agent_view.send_telegram(text)
        except agent_view.AgentError as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)
        A._emit("system", {"event": "dashboard_telegram_send", "by": "operator", "chars": result["chars"]})
        return JSONResponse(result)

    @app.post("/control/agent-pause")
    def control_agent_pause(request: Request, paused: str = Form("1"), confirm: str = Form(""),
                            csrf_token: str = Form(None, alias="_csrf")) -> JSONResponse:
        password = A._require_operator(request)
        A._require_csrf(request, password, csrf_token)
        want = str(paused).lower() in {"1", "true", "on", "yes"}
        if want and confirm.strip() != "PAUSE AGENT":
            raise HTTPException(status_code=400, detail="type PAUSE AGENT to stop the agent's jobs and new turns")
        try:
            result = agent_view.set_paused(want)
        except agent_view.AgentError as exc:
            return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status)
        A._emit("system", {"event": "dashboard_agent_pause", "by": "operator", "paused": want},
                level="warn" if want else "info")
        return JSONResponse(result)


def _journal(limit: int = 25) -> list[dict[str, Any]]:
    """What the agent changed or noted through the Kaiba tools (kaiba.core.journal)."""
    return intel._q("SELECT ts_ms, kind, subject, body FROM journal ORDER BY seq DESC LIMIT ?", (limit,))
