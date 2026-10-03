"""Deterministic daily operator report: no model, bounded queries, Telegram-ready text.

WHY. The model-written daily report failed on 6 of the 7 days before 2026-10-01 (HTTP 429
on the Codex fallback, then a provider timeout), and a success cost 3-4M tokens and 25-81
minutes (``docs/research/audit-20261001-hermes.md`` §2). This prints the same numbers from
the database alone, so a report arrives even when every model is down. Commentary is for
the operator to ask for on demand, not something the report waits on.

ONE SOURCE. Every number comes from the read-out builders behind the MCP tools in
:mod:`kaiba.mcp.server` (``health_report``, ``live_ev``, ``copy_manager_report``,
``wallet_grade_counts``). The report and ``kaiba_health`` / ``kaiba_live_ev`` /
``kaiba_copy_manager`` / ``kaiba_wallet_grade_counts`` cannot disagree, because nothing is
computed twice. The HUNTERS section is :func:`kaiba.hunters.digest.build`, the same builder
``python -m kaiba.hunters.digest`` prints; the report reads its sent-cursor and never
writes it.

READ-ONLY. The database is opened ``mode=ro`` with ``PRAGMA query_only``. Every query is
bounded (index range, LIMIT, or a measured row cap); the whole report is designed to run
well under 30 s on the 19 GB production database.

    python -m kaiba.ops.daily_report [--db PATH] [--chain robinhood] [--json]

Exit 0 with the report on stdout (a section it cannot read says "cannot measure: ...").
Exit 1, with one line on stdout, only if the database itself cannot be opened -- Hermes
turns a non-zero ``no_agent`` script into an error alert, which is what that deserves.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kaiba.core.config import get_risk, get_settings
from kaiba.core.schemas import now_ms


def open_readonly(path: Path) -> sqlite3.Connection:
    """A read-only connection: ``mode=ro`` in the URI and ``query_only`` on top."""
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"no database at {resolved}")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def build(conn: sqlite3.Connection, *, chain: str = "robinhood", now: int | None = None,
          risk: Any = None) -> dict[str, Any]:
    """Every section of the report as data. A section that fails carries ``{"error": ...}``."""
    from kaiba.mcp import server as readouts

    until = int(now) if now is not None else now_ms()
    cfg = risk if risk is not None else readouts._section(get_risk)
    if isinstance(cfg, dict):  # the risk file itself could not be read
        return {"generated_ms": until, "chain": chain, "risk_error": cfg["error"]}
    section = readouts._section
    return {
        "generated_ms": until,
        "chain": chain,
        "health": section(readouts.health_report, conn, hours=24, now=until, risk=cfg),
        "ev_24h": section(readouts.live_ev, conn, days=1, chain=chain, now=until),
        "ev_7d": section(readouts.live_ev, conn, days=7, chain=chain, now=until),
        "copy": section(readouts.copy_manager_report, conn, hours=24, chain=chain, now=until),
        "wallets": section(readouts.wallet_grade_counts, conn, hours=24, now=until),
        "hunters": section(_hunters, conn, now=until, risk=cfg),
        "paper_mints": section(_paper_mints, conn, now=until),
        "wallet_funnel": section(_wallet_funnel, conn),
        "losses": section(_losses, conn, now=until),
    }


def _losses(conn: sqlite3.Connection, *, now: int) -> dict[str, Any]:
    """The newest loss attribution (kaiba/learning/loss_attribution.py): entry vs exit split,
    top leak, top separating entry feature. SELECT-only: one ``kv`` read, computed by the
    scheduled ``loss_attribution`` job, never here."""
    from kaiba.learning import loss_attribution

    return loss_attribution.summary(conn, now=now)


#: Chains the proven_wallets job freezes a cohort for (config/schedule.yaml).
FUNNEL_CHAINS: tuple[str, ...] = ("robinhood", "sol")
#: Freezes read per chain: today's and the one a week earlier, for the growth figure.
FUNNEL_DAYS = 8


def _wallet_funnel(conn: sqlite3.Connection) -> dict[str, Any]:
    """Per chain, the proven_wallets funnel of the newest freezes (kaiba/learning/proven.py).

    SELECT-only; one ``LIMIT`` read of ``wallet_cohort_freezes`` per chain.
    """
    from kaiba.core.schemas import Chain
    from kaiba.learning import proven

    return {ch: proven.funnel_history(conn, Chain(ch), limit=FUNNEL_DAYS) for ch in FUNNEL_CHAINS}


def _paper_mints(conn: sqlite3.Connection, *, now: int) -> dict[str, Any]:
    """The paper NFT mint study (kaiba/learning/mint_study.py): n, scored, EV so far, gate.

    SELECT-only, so it is safe on this report's ``mode=ro`` connection. Before migration
    034 is applied it says "not started" rather than failing.
    """
    from kaiba.learning import mint_study

    return mint_study.summary(conn, now=now)


#: The daily report's slice of the hunter digest: two per kind, each a fact line plus its
#: link, so the section stays under ~20 lines. ``python -m kaiba.hunters.digest`` prints
#: the rest.
HUNTER_PER_KIND = 2
HUNTER_LINE_WIDTH = 160


def _hunters(conn: sqlite3.Connection, *, now: int, risk: Any) -> dict[str, Any]:
    """Hunter digest for the last 24 h. Reads the sent-cursor, never writes it."""
    from kaiba.hunters import digest

    return digest.build(conn, since_hours=24, per_kind=HUNTER_PER_KIND, now=now, risk=risk)


# ---------------------------------------------------------------- rendering


def _onoff(value: Any) -> str:
    return "?" if value is None else ("ON" if value else "off")


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def _signed(text: Any) -> str:
    """'0.200000' -> '+0.2000'; keeps 4 decimals for a native amount."""
    if text is None:
        return "n/a"
    value = float(text)
    return f"{value:+.4f}"


def _age(seconds: Any) -> str:
    if seconds is None:
        return "never"
    s = float(seconds)
    if s < 120:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s / 60:.0f}m"
    if s < 172_800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86_400:.1f}d"


def _size(n: Any) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _err(section: Any) -> str | None:
    return section["error"] if isinstance(section, dict) and "error" in section else None


def _ev_line(label: str, ev: Any, unit: str) -> str:
    if _err(ev):
        return f"  {label}: {ev['error']}"
    net_usd = ev.get("net_usd")
    usd = f" ({'-' if net_usd < 0 else '+'}${abs(net_usd):,.0f})" if net_usd is not None else ""
    return (f"  {label}: n {ev['n']} | wins {ev['wins']} | mean {_pct(ev['mean_pct'])} | "
            f"median {_pct(ev['median_pct'])} | net {_signed(ev['net_native'])} {unit}{usd}")


def _n(value: Any) -> str:
    """A count with thousands separators; '?' for a stage an older freeze did not record."""
    return "?" if value is None else f"{int(value):,}"


def _day(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%m-%d")


def _funnel_lines(funnel: Any) -> list[str]:
    """One line per chain: the newest freeze's funnel, then where it stood a week before."""
    if _err(funnel):
        return [f"WALLET FUNNEL {funnel['error']}"]
    if not isinstance(funnel, dict):
        return ["WALLET FUNNEL cannot measure: no data"]
    lines = ["WALLET FUNNEL (proven_wallets: tape -> eligible -> tested -> copy-proven)"]
    for chain in sorted(funnel):
        history = funnel[chain] or []
        if not history:
            lines.append(f"  {chain}: never frozen")
            continue
        new = history[0]
        line = (
            f"  {chain} {_day(new['frozen_ms'])}: seen {_n(new.get('seen'))} | active {_n(new.get('active'))} | "
            f"drawn {_n(new.get('drawn'))} | eligible {_n(new.get('eligible'))} | tested "
            f"{_n(new.get('tested'))} | proven {_n(new.get('proven'))}"
        )
        if new.get("truncated"):
            line += " | cut by deadline"
        if len(history) > 1:
            old = history[-1]
            line += f" (was {_day(old['frozen_ms'])}: eligible {_n(old.get('eligible'))}, proven {_n(old.get('proven'))})"
        lines.append(line)
    return lines


def render(report: dict[str, Any]) -> str:
    """Short plain text for Telegram. Numbers only; every unreadable section says so."""
    stamp = datetime.fromtimestamp(report["generated_ms"] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    chain = report["chain"]
    unit = {"sol": "SOL", "bsc": "BNB"}.get(chain, "ETH")
    lines = [f"Kaiba daily report, {stamp}"]
    if "risk_error" in report:
        lines.append(f"risk config unreadable: {report['risk_error']}")
        return "\n".join(lines)

    health = report["health"] if isinstance(report.get("health"), dict) else {}
    if _err(health):
        lines.append(f"HEALTH {health['error']}")
        health = {}

    # -- controls
    ctl = health.get("controls") or {}
    if _err(ctl):
        lines.append(f"CONTROLS {ctl['error']}")
    elif ctl:
        lines.append(
            f"CONTROLS kill switch {_onoff(ctl['kill_switch'])} | entries "
            f"{'PAUSED' if ctl['entries_paused'] else 'open'} | reduce-only {_onoff(ctl['reduce_only'])} | "
            f"mode {ctl['global_mode']}"
        )
        lines.append(
            f"  chains on: {', '.join(ctl['enabled_chains']) or 'none'} | live lanes: "
            f"{', '.join(ctl['live_lanes']) or 'none'} | execute_planned {_onoff(ctl['execute_planned_enabled'])}"
        )
    red = health.get("red")
    lines.append("RED " + ("; ".join(red) if red else "none") if red is not None else "RED cannot measure")

    # -- live expectancy
    lines.append(f"{chain.upper()} LIVE (closed positions)")
    lines.append(_ev_line("24h", report.get("ev_24h") or {}, unit))
    ev7 = report.get("ev_7d") or {}
    lines.append(_ev_line("7d ", ev7, unit))
    if not _err(ev7) and ev7.get("by_exit_reason"):
        worst = ", ".join(
            f"{r['exit_reason']} {r['n']}x {_signed(r['net_native'])}" for r in ev7["by_exit_reason"][:4]
        )
        lines.append(f"  7d by exit (costliest first): {worst}")

    # -- where the losses came from: entry vs exit, and what would have told them apart
    losses = report.get("losses")
    if _err(losses):
        lines.append(f"LOSSES {losses['error']}")
    elif isinstance(losses, dict):
        from kaiba.learning import loss_attribution

        lines.extend(loss_attribution.render_lines(losses))

    # -- today vs the daily stop
    today = health.get("risk_today") or {}
    for row in health.get("daily_loss") if isinstance(health.get("daily_loss"), list) else []:
        used = f" ({row['stop_used_pct']:.0f}% used)" if row.get("stop_used_pct") is not None else ""
        lines.append(
            f"TODAY {row['chain']}: realized {_signed(row['realized_today'])} of "
            f"-{float(row['daily_stop']):.4f} stop{used}"
            f"{' STOPPED' if row['stopped'] else ''} | entries {today.get('entries', '?')} | "
            f"halted {'YES: ' + str(today.get('halt_reason')) if today.get('halted') else 'no'}"
        )
    if _err(health.get("daily_loss")):
        lines.append(f"TODAY {health['daily_loss']['error']}")

    # -- open positions
    positions = health.get("open_positions")
    if isinstance(positions, list):
        lines.append(f"OPEN {len(positions)} live: id, age, cost, exit tries, state")
        for p in positions[:8]:
            blind = f", blind {_age(p['blind_s'])}" if p.get("blind_s") is not None else ""
            lines.append(
                f"  {str(p['position_id'])[-8:]} {p['chain']} {p['age_h']}h {p['cost_native']} "
                f"{p['exit_attempts']} {p['exit_state'] or '-'}{blind}"
            )
        if len(positions) > 8:
            lines.append(f"  ...and {len(positions) - 8} more")
    elif _err(positions):
        lines.append(f"OPEN {positions['error']}")

    # -- watchdog
    wd = health.get("watchdog") or {}
    if _err(wd):
        lines.append(f"WATCHDOG {wd['error']}")
    elif wd.get("found"):
        lines.append(
            f"WATCHDOG beat {_age(wd['age_s'])} ago | checked {wd.get('checked')} | blind {wd.get('blind')} | "
            f"stranded {wd.get('stranded_deferred')} | unprotected {wd.get('standing_unprotected')} | "
            f"longest blind {_age(wd.get('longest_blind_s'))}"
        )
    elif wd:
        lines.append(f"WATCHDOG {wd.get('note')}")

    # -- copy manager
    copy = report.get("copy") or {}
    if _err(copy):
        lines.append(f"COPY MGR {copy['error']}")
    else:
        runs = copy["runs"]["by_status"]
        mode = {True: "LIVE", False: "dry-run"}.get(copy.get("live"), "mode unknown")
        lines.append(
            f"COPY MGR ({mode}) runs ok {runs.get('ok', 0)} | error {runs.get('error', 0)} | timeout "
            f"{runs.get('timeout', 0)} | decisions on {copy['tokens_with_decisions']} token(s) "
            f"{json.dumps(copy['decisions_by_kind'], separators=(',', ':'))} | live sells {len(copy['live_sells'])}"
        )
        if copy.get("decisions_by_action"):
            lines.append(f"  outcomes: {json.dumps(copy['decisions_by_action'], separators=(',', ':'))}")
        for t in [t for t in copy["tokens"] if t["last_decision"]][:5]:
            d = t["last_decision"]
            action = f" ({d['action']})" if d.get("action") else ""
            lines.append(
                f"  {t['symbol'] or t['token'][:10]} {d['kind']}{action} | pnl {_pct(d['pnl_pct'])} | peak "
                f"{_pct(t['peak_pct'])} | {t['decision_runs']} runs"
            )
        for s in copy["live_sells"][:3]:
            lines.append(f"  sell {s.get('symbol') or str(s.get('token'))[:10]} {s.get('kind') or ''} "
                         f"{s.get('outcome') or s.get('state') or ''} ({s['source']})")
        if copy["run_failures"]:
            top = copy["run_failures"][0]
            lines.append(f"  top run failure ({top['n']}x): {top['error'][:100]}")

    # -- wallets
    wallets = report.get("wallets") or {}
    if _err(wallets):
        lines.append(f"WALLETS {wallets['error']}")
    else:
        recent = wallets["a_b_scored_in_window"]
        new = ", ".join(f"{ch} A{c['A']} B{c['B']}" for ch, c in sorted(recent.items())) or "none"
        totals = ", ".join(
            f"{ch} A{c.get('A', 0)} B{c.get('B', 0)}" for ch, c in sorted(wallets["by_chain"].items())
        ) or "none"
        lines.append(f"WALLETS A/B scored 24h: {new}")
        lines.append(f"  A/B held: {totals}")

    # -- the proven-wallet funnel: is "more wallets" actually growing?
    lines.extend(_funnel_lines(report.get("wallet_funnel")))

    # -- health
    storage = health.get("storage") or {}
    if _err(storage):
        lines.append(f"DISK {storage['error']}")
    elif storage:
        lines.append(
            f"DISK {storage['disk_used_pct']}% used ({_size(storage['disk_free_bytes'])} free) | "
            f"DB {_size(storage['db_bytes'])} | WAL {_size(storage['wal_bytes'])}"
        )
    jobs = health.get("jobs")
    if isinstance(jobs, list):
        failing = [j for j in jobs if j["error"] + j["timeout"] > 0]
        summary = ", ".join(
            f"{j['job']} {j['error'] + j['timeout']}/{j['ok'] + j['error'] + j['timeout'] + j['other']}"
            for j in failing[:6]
        )
        lines.append(f"JOBS with errors 24h: {summary or 'none'}"
                     f"{f' (+{len(failing) - 6} more)' if len(failing) > 6 else ''}")
    elif _err(jobs):
        lines.append(f"JOBS {jobs['error']}")

    # -- paper NFT mint study (spends nothing; the gate that must PASS before any live mint)
    paper = report.get("paper_mints")
    if _err(paper):
        lines.append(f"PAPER MINTS {paper['error']}")
    elif isinstance(paper, dict):
        from kaiba.learning import mint_study

        lines.extend(mint_study.render_lines(paper))

    # -- hunters (research leads for the owner; nothing here is acted on by the agent)
    hunters = report.get("hunters")
    if _err(hunters):
        lines.append(f"HUNTERS {hunters['error']}")
    elif isinstance(hunters, dict) and "sections" in hunters:
        from kaiba.hunters import digest

        lines.append("HUNTERS new in 24h (leads to check yourself; Kaiba mints/claims/signs nothing)")
        lines.extend(digest.render_lines(hunters, per_kind=HUNTER_PER_KIND, width=HUNTER_LINE_WIDTH))
    else:
        lines.append("HUNTERS cannot measure: no digest")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.ops.daily_report", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None, help="database path (default: settings db_path)")
    parser.add_argument("--chain", default="robinhood", help="chain for the live P&L rows (default robinhood)")
    parser.add_argument("--json", action="store_true", help="print the sections as JSON instead of text")
    args = parser.parse_args(argv)

    started = time.monotonic()
    path = args.db or get_settings().db_path
    try:
        conn = open_readonly(path)
    except (OSError, sqlite3.Error) as exc:
        print(f"Kaiba daily report: cannot open the database read-only ({type(exc).__name__}: {exc})")
        return 1
    try:
        report = build(conn, chain=args.chain)
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps({**report, "elapsed_s": round(elapsed, 2)}, default=str, indent=1))
    else:
        print(render(report))
        print(f"(read-only, {elapsed:.1f}s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
