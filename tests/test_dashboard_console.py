"""The console pages added 2026-10-18: trades, wallets gathered, agent chat, commands.

What these guard, beyond "the page renders":

* the commands page is not a shell -- unknown names, arguments off the list, a missing typed
  confirmation and a restart with an order in flight are all refused, and a script runs only
  if it opted in with a header;
* the agent chat passes the message on stdin, never with --yolo, one turn at a time, and a
  reply is rendered with raw HTML disabled (a reply can quote anything the agent read);
* wallet exports never show an excluded address and a download serves only a listed file;
* a client that keeps guessing the password is locked out even when it finally guesses right.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import agent_chat, auth, commands, procs, trading
from dashboard.app import LOGIN_MAX_FAILS_PER_CLIENT, create_app
from kaiba.core.config import DEFAULT_RISK_PATH, get_settings
from kaiba.core.schemas import now_ms

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def console(tmp_db, tmp_path, monkeypatch):
    risk_path = tmp_path / "risk.yaml"
    shutil.copy(DEFAULT_RISK_PATH, risk_path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(risk_path))
    monkeypatch.setenv("KAIBA_DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("KAIBA_DASHBOARD_ISOLATE", "0")
    get_settings.cache_clear()
    trading.clear_caches()
    client = TestClient(create_app())
    client.conn = tmp_db  # type: ignore[attr-defined]
    client.data_dir = tmp_path  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        client.close()
        trading.clear_caches()
        get_settings.cache_clear()


@pytest.fixture
def signed_in(console):
    response = console.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303 and auth.COOKIE_NAME in console.cookies
    return console


def csrf_of(client: TestClient, path: str = "/commands") -> str:
    match = re.search(r'name="csrf-token" content="([^"]*)"', client.get(path).text)
    assert match and match.group(1)
    return match.group(1)


def wait_for(predicate, timeout: float = 15.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _position(conn, pid, *, mode, lane, chain, ret, closed_ago_ms=3_600_000, cost=1_000_000_000):
    now = now_ms()
    conn.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, closed_ms, qty, qty_total, "
        "cost_native, proceeds_native, realized_native, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, chain, f"TOK{pid}", lane, mode, now - closed_ago_ms - 60_000, now - closed_ago_ms, "0", "100",
         str(cost), str(int(cost * (1 + ret))), str(int(cost * ret)), "test"),
    )


# ---------------------------------------------------------------- pages


@pytest.mark.parametrize("path,heading", [
    ("/trades", "Trades"), ("/wallets", "Wallets"), ("/chat", "Agent chat"), ("/commands", "Commands"),
])
def test_every_page_renders_on_an_empty_database(signed_in, path, heading):
    response = signed_in.get(path)
    assert response.status_code == 200
    assert f"<h1>{heading}</h1>" in response.text
    assert "Withdrawals are not available to the agent by construction." in response.text


def test_the_trades_page_invents_no_money_on_an_empty_database(signed_in):
    html = signed_in.get("/trades").text
    assert re.search(r"\$\s?\d", html) is None
    assert "no live closes" in html


@pytest.mark.parametrize("path", ["/trades", "/wallets", "/chat", "/commands"])
def test_pages_send_an_anonymous_visitor_to_login(console, path):
    response = console.get(path, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/events/chat?turn_id=x", "/events/command/0101-000000-abcdef"])
def test_streams_refuse_an_anonymous_client(console, path):
    assert console.get(path).status_code == 401


def test_writes_need_a_session_and_a_csrf_token(console, signed_in):
    anon = TestClient(console.app)
    assert anon.post("/control/command", data={"name": "status"}).status_code == 401
    assert anon.post("/control/chat", data={"message": "hi"}).status_code == 401
    assert signed_in.post("/control/command", data={"name": "status"}).status_code == 403
    assert signed_in.post("/control/chat", data={"message": "hi"}).status_code == 403


# ---------------------------------------------------------------- trades


def test_trade_summary_caps_outcomes_and_keeps_modes_apart(console, monkeypatch):
    # Codex 2026-11-03: test caps/modes independently of the ambient randomized exit trial.
    monkeypatch.setattr(trading, "_trials", lambda: {})
    conn = console.conn
    _position(conn, "a", mode="shadow", lane="migration-fade", chain="sol", ret=0.10)
    _position(conn, "b", mode="shadow", lane="migration-fade", chain="sol", ret=-0.20)
    _position(conn, "c", mode="shadow", lane="migration-fade", chain="sol", ret=9.0)  # capped at +300%
    _position(conn, "d", mode="live", lane="sm-trenches", chain="sol", ret=-0.30)
    conn.commit()
    summary = trading.trade_summary(12)
    rows = {(r["mode"], r["lane"]): r for r in summary["rows"]}
    paper = rows[("shadow", "migration-fade")]
    assert paper["n"] == 3
    assert paper["mean"] == pytest.approx((0.10 - 0.20 + 3.0) / 3)
    assert paper["win_rate"] == pytest.approx(2 / 3)
    assert rows[("live", "sm-trenches")]["n"] == 1
    assert summary["live_realized"][0]["chain"] == "sol"
    assert summary["live_realized"][0]["native"] == pytest.approx(-0.3)

    listed = trading.closed_trades("shadow", days=1)
    flagged = [t for t in listed if t["position_id"] == "c"][0]
    assert any("above +300% cap" in f for f in flagged["flags"])
    assert trading.closed_trades("bogus") == []


def test_the_trades_page_shows_the_numbers(signed_in):
    _position(signed_in.conn, "a", mode="shadow", lane="migration-fade", chain="sol", ret=0.25)
    signed_in.conn.commit()
    trading.clear_caches()
    html = signed_in.get("/trades").text
    assert "migration-fade" in html and "+25.0%" in html


# ---------------------------------------------------------------- wallets gathered


def _exports(data_dir: Path, entries: list, excluded: list | None = None) -> Path:
    folder = data_dir / "claude-runners" / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "kaiba-solana-snipers-early-runners.txt"
    path.write_text(json.dumps(entries), encoding="utf-8")
    if excluded is not None:
        (data_dir / "claude-runners" / "excluded.json").write_text(json.dumps(excluded), encoding="utf-8")
    return path


def test_exports_leave_out_excluded_addresses(console):
    _exports(console.data_dir, [
        {"address": "WalletKeep111", "name": "SNIPE 3run 2s $1.0k 30d$9k", "emoji": "x", "groups": ["default", "kaiba-sniper"]},
        {"address": "WalletOwned222", "name": "EARLY 2run 30s $0.5k", "emoji": "y", "groups": ["default"]},
    ], excluded=["walletowned222"])
    out = trading.wallet_exports()
    (f,) = out["files"]
    assert f["chain"] == "sol" and f["count"] == 1
    assert [w["address"] for w in f["preview"]] == ["WalletKeep111"]
    assert f["preview"][0]["groups"] == ["kaiba-sniper"]


def test_an_export_download_serves_only_a_listed_file(signed_in):
    _exports(signed_in.data_dir, [{"address": "WalletKeep111", "name": "n", "groups": []}])
    ok = signed_in.get("/api/wallets/export/kaiba-solana-snipers-early-runners.txt")
    assert ok.status_code == 200 and "WalletKeep111" in ok.text
    assert signed_in.get("/api/wallets/export/excluded.json").status_code == 404
    assert signed_in.get("/api/wallets/export/..%2F..%2Frisk.yaml").status_code == 404
    assert trading.export_path("../risk.yaml") is None


def test_the_wallets_page_lists_exports_and_hunts(signed_in):
    _exports(signed_in.data_dir, [{"address": "WalletKeep111", "name": "SNIPE 3run 2s $1.0k", "groups": []}])
    hunt = signed_in.data_dir / "claude-runners" / "sol"
    hunt.mkdir(parents=True, exist_ok=True)
    (hunt / "last.json").write_text(json.dumps({"at": time.time(), "runners": 12, "exported": 7,
                                                "winning_wallets": 40, "kept": 9}), encoding="utf-8")
    html = signed_in.get("/wallets").text
    assert "kaiba-solana-snipers-early-runners.txt" in html
    assert "7 exported" in html and "SNIPE 3run 2s $1.0k" in html


# ---------------------------------------------------------------- commands


def _run(signed_in, **form):
    return signed_in.post("/control/command", data={"_csrf": csrf_of(signed_in), **form})


def _finished(runner: commands.Runner, run_id: str) -> commands.Run:
    return wait_for(lambda: (lambda r: r if r and r.status != "running" else None)(runner.get(run_id)))


def test_unknown_commands_and_off_list_arguments_are_refused(signed_in):
    assert _run(signed_in, name="rm -rf /").status_code == 400
    assert _run(signed_in, name="restart", arg="hermes-gateway-kaiba-operator", confirm="x").status_code == 400
    assert _run(signed_in, name="logs", arg="/etc/passwd").status_code == 400
    assert _run(signed_in, name="status", arg="anything").status_code == 400


def test_a_restart_needs_the_service_name_typed(signed_in):
    response = _run(signed_in, name="restart", arg="kaiba-scan")
    assert response.status_code == 400 and "to confirm" in response.json()["error"]


def test_a_restart_is_refused_while_an_order_is_in_flight(signed_in, monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "_run_capture", lambda argv, timeout=30.0: calls.append(argv) or (0, ""))
    now = now_ms()
    signed_in.conn.execute(
        "INSERT INTO orders (order_id, chain, token, side, lane, mode, input_token, output_token, amount_in, "
        "min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES ('o1','sol','T','buy','sm-trenches','live','SOL','T','1','1',100,'submitted','gmgn',?,?)",
        (now, now),
    )
    signed_in.conn.commit()
    response = _run(signed_in, name="restart", arg="kaiba-scan", confirm="kaiba-scan")
    assert response.status_code == 200
    run = _finished(signed_in.app.state.runner, response.json()["run_id"])
    assert run.status == "refused"
    assert "1 order(s) in flight" in "\n".join(run.lines)
    assert not any("restart" in argv for argv in calls), "a refused restart must not call systemctl"


def test_a_restart_with_nothing_in_flight_restarts_exactly_that_unit(signed_in, monkeypatch):
    calls = []

    def fake(argv, timeout=30.0):
        calls.append(argv)
        return 0, "ActiveState=active\nSubState=running" if "show" in argv else ""

    monkeypatch.setattr(commands, "_run_capture", fake)
    monkeypatch.setattr(commands.time, "sleep", lambda s: None)
    response = _run(signed_in, name="restart", arg="kaiba-scan", confirm="kaiba-scan")
    run = _finished(signed_in.app.state.runner, response.json()["run_id"])
    assert run.status == "ok"
    assert ["systemctl", "--user", "restart", "kaiba-scan.service"] in calls


def test_the_status_command_runs_and_is_recorded(signed_in):
    response = _run(signed_in, name="status")
    run_id = response.json()["run_id"]
    run = _finished(signed_in.app.state.runner, run_id)
    assert run.status == "ok" and any("orders in flight" in line for line in run.lines)
    stored = signed_in.get(f"/api/commands/runs/{run_id}").json()
    assert stored["status"] == "ok" and stored["lines"]
    assert any(r["run_id"] == run_id for r in signed_in.get("/api/commands/runs").json()["runs"])


def _scripts(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "scripts"
    folder.mkdir()
    (folder / "hello.py").write_text(
        "# dashboard: label = Say hello\n# dashboard: about = test\n# dashboard: timeout = 30\n"
        "import time\nprint('hello from a script')\ntime.sleep(0.5)\n", encoding="utf-8")
    (folder / "unlisted.py").write_text("print('no header, never listed')\n", encoding="utf-8")
    (folder / "Bad Name.py").write_text("# dashboard: label = bad\n", encoding="utf-8")
    monkeypatch.setattr(commands, "SCRIPTS_DIR", folder)
    return folder


def test_only_scripts_with_a_header_and_a_clean_name_are_listed(tmp_path, monkeypatch):
    _scripts(tmp_path, monkeypatch)
    names = [c.name for c in commands.script_commands()]
    assert names == ["script-hello"]


def test_a_script_runs_and_the_same_one_cannot_run_twice_at_once(signed_in, tmp_path, monkeypatch):
    _scripts(tmp_path, monkeypatch)
    first = _run(signed_in, name="script-hello")
    assert first.status_code == 200
    second = _run(signed_in, name="script-hello")
    assert second.status_code == 409
    run = _finished(signed_in.app.state.runner, first.json()["run_id"])
    assert run.status == "ok" and "hello from a script" in "\n".join(run.lines)


def test_procs_wraps_children_in_their_own_unit_without_a_shell(monkeypatch):
    monkeypatch.setattr(procs, "isolated", lambda: True)
    argv = procs.wrap(["python", "x.py"], unit="kaiba-dash-run-abcd", memory_max="1G", runtime_max_s=90,
                      cwd="/repo", env={"A": "1"})
    assert argv[:2] == ["systemd-run", "--user"] and "--pipe" in argv and "--wait" in argv
    assert "--unit=kaiba-dash-run-abcd" in argv and "MemoryMax=1G" in argv and "RuntimeMaxSec=90" in argv
    assert "--setenv=A=1" in argv and argv[-3:] == ["--", "python", "x.py"]
    assert not any(a in ("sh", "bash", "-c") for a in argv)


# ---------------------------------------------------------------- agent chat

FAKE_AGENT = r'''
import json, sys, time
prompt = sys.stdin.read()
delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
print(json.dumps({"type": "system", "subtype": "init", "model": "fake-model", "session_id": "s1"}), flush=True)
time.sleep(delay)
print(json.dumps({"type": "tool_use", "name": "kaiba_status", "input": {"chain": "sol"}}), flush=True)
reply = "You said: " + prompt.split("\n\n", 1)[-1]
print(json.dumps({"type": "text", "text": reply}), flush=True)
print(json.dumps({"type": "result", "session_id": "s1", "exit_code": 0, "text": reply,
                  "tokens": {"total": 42}, "duration_ms": 1234}), flush=True)
'''


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")

    def use(delay: float = 0.0):
        monkeypatch.setenv("KAIBA_CHAT_ARGV", json.dumps([sys.executable, str(script), str(delay)]))

    use()
    return use


def _say(signed_in, message, thread="main"):
    return signed_in.post("/control/chat", data={"_csrf": csrf_of(signed_in, "/chat"), "message": message,
                                                 "thread": thread})


def _turn_done(signed_in, thread="main"):
    def check():
        turns = signed_in.get(f"/api/chat/history?thread={thread}").json()["turns"]
        return turns if turns and turns[-1]["status"] != "running" else None

    return wait_for(check)


def test_a_message_reaches_the_agent_on_stdin_and_the_reply_is_kept(signed_in, fake_agent):
    response = _say(signed_in, "how are the paper lanes doing?")
    assert response.status_code == 200 and response.json()["turn_id"]
    turns = _turn_done(signed_in)
    last = turns[-1]
    assert last["status"] == "done"
    assert last["reply"] == "You said: how are the paper lanes doing?"
    assert last["model"] == "fake-model" and last["tokens"]["total"] == 42
    assert any(e["kind"] == "tool" and "kaiba_status" in e["text"] for e in last["events"])
    html = signed_in.get("/chat").text
    assert "You said: how are the paper lanes doing?" in html


def test_a_reply_cannot_inject_html_into_the_page(signed_in, fake_agent):
    _say(signed_in, "<script>alert(1)</script> **bold**")
    last = _turn_done(signed_in)[-1]
    assert "<script>" not in last["reply_html"]
    assert "&lt;script&gt;" in last["reply_html"] and "<strong>bold</strong>" in last["reply_html"]
    assert "<script>alert(1)</script>" not in signed_in.get("/chat").text


def test_one_turn_at_a_time(signed_in, fake_agent):
    fake_agent(delay=2.0)
    assert _say(signed_in, "first").status_code == 200
    second = _say(signed_in, "second")
    assert second.status_code == 409 and "still answering" in second.json()["error"]
    _turn_done(signed_in)


def test_bad_threads_and_messages_are_refused(signed_in, fake_agent):
    assert _say(signed_in, "hi", thread="../../etc").status_code == 400
    assert _say(signed_in, "   ").status_code == 400
    assert _say(signed_in, "x" * (agent_chat.MAX_MESSAGE_CHARS + 1)).status_code == 400


def test_the_operator_can_stop_a_turn(signed_in, fake_agent):
    fake_agent(delay=30.0)
    assert _say(signed_in, "slow one").status_code == 200
    stop = signed_in.post("/control/chat-stop", data={"_csrf": csrf_of(signed_in, "/chat")})
    assert stop.json()["stopped"] is True
    last = _turn_done(signed_in)[-1]
    assert last["status"] == "failed" and "stopped by the operator" in last["error"]


def test_the_real_agent_command_never_auto_approves(monkeypatch):
    monkeypatch.delenv("KAIBA_CHAT_ARGV", raising=False)
    argv, env, cwd = agent_chat.command_for("main")
    assert "--yolo" not in argv
    assert argv[argv.index("--query-file") + 1] == "-"
    assert argv[argv.index("--continue") + 1] == "dashboard-main" and "--create-if-missing" in argv
    assert argv[argv.index("--profile") + 1] == "kaiba-operator"
    assert env["HERMES_HOME"].endswith("kaiba-operator") and cwd.endswith("kaiba-operator")


# ---------------------------------------------------------------- login throttle


def test_a_client_that_keeps_guessing_is_locked_out_even_with_the_right_password(console):
    for _ in range(LOGIN_MAX_FAILS_PER_CLIENT):
        bad = console.post("/login", data={"password": "nope"}, follow_redirects=False)
        assert bad.headers["location"] == "/login?bad=1"
    blocked = console.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert blocked.headers["location"] == "/login?bad=2"
    assert auth.COOKIE_NAME not in console.cookies
    assert console.get("/login?bad=2").status_code == 429
    fresh = TestClient(create_app())
    # Codex 2026-11-03: the unified workspace is now the post-login destination.
    assert fresh.post("/login", data={"password": PASSWORD}, follow_redirects=False).headers["location"] == "/ultimate"


# ---------------------------------------------------------------- the event loop stays free


def test_every_handler_that_reads_the_database_runs_off_the_event_loop():
    """2026-10-18: four command-centre reads took 13-100+ s on the box, and as ``async def``
    handlers they froze every SSE stream behind them. Only streams and login may be async."""
    import inspect

    # /api/gmgn/account/check (Codex, wired 2026-10-20) is async only to read its tiny form body;
    # the GMGN call itself runs in the threadpool (run_in_threadpool) and it touches no database.
    allowed_async = {"/healthz", "/login", "/logout", "/events/stream", "/events/chat", "/events/command/{run_id}",
                     "/api/gmgn/account/check"}
    for route in create_app().routes:
        path = str(getattr(route, "path", ""))
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or path.startswith("/static") or path in allowed_async:
            continue
        assert not inspect.iscoroutinefunction(endpoint), f"{path} blocks the event loop"


def test_top_wallets_ranks_only_graded_wallets(console):
    from dashboard import data
    from kaiba.core.db import upsert

    for address, grade, score in (("W-unscored", "UNSCORED", 99.0), ("W-b", "B", 61.0), ("W-c", "C", 44.0),
                                  ("W-d", "D", 70.0)):
        upsert(console.conn, "wallet_scores",
               {"chain": "sol", "address": address, "score": score, "grade": grade, "evidence_weight": 0.5,
                "archetype": "trader", "model_version": "t", "scored_at_ms": now_ms()},
               conflict=["chain", "address"], update=["score"])
    data.clear_cache()
    assert [w["address"] for w in data.top_wallets(10)] == ["W-b", "W-c"]


def test_the_rail_mode_comes_from_the_risk_file(console):
    from dashboard import data

    assert data.current_mode() == data.overview()["mode"]


def test_recent_tokens_are_the_newest_across_every_grade(console):
    from dashboard import data

    base = now_ms()
    for i, grade in enumerate(("B", "QUARANTINED", "A", "B", "C", "D", "B")):
        console.conn.execute(
            "INSERT INTO token_dossiers (chain, address, built_at_ms, score, grade, dossier_json) VALUES (?,?,?,?,?,?)",
            ("sol", f"T{i}", base - i * 1000, 50.0, grade, "{}"),
        )
    console.conn.commit()
    data.clear_cache()
    assert [t["address"] for t in data.recent_tokens(4)] == ["T0", "T1", "T2", "T3"]
    assert len(data.recent_tokens(20)) == 7


def test_an_expired_value_is_served_while_it_refreshes_in_the_background(monkeypatch):
    from dashboard import data

    calls = []

    @data._ttl(10)
    def slow(x):
        calls.append(x)
        return len(calls)

    data.clear_cache()
    clock = [1000.0]
    monkeypatch.setattr(data.time, "monotonic", lambda: clock[0])
    assert slow(1) == 1                      # first load computes
    clock[0] += 5
    assert slow(1) == 1 and calls == [1]     # fresh: served from memory
    clock[0] += 10
    assert slow(1) == 1                      # expired: the old value comes back at once ...
    wait_for(lambda: len(calls) == 2)        # ... while one background refresh recomputes it
    wait_for(lambda: slow(1) == 2)
    clock[0] += 10 * data.STALE_LIMIT        # far too old to serve: the caller waits
    assert slow(1) == 3
