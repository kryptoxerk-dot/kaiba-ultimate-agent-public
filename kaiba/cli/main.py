"""The `kaiba` command line.

Subcommands import their modules lazily. A half-finished subsystem must not stop the rest
of the CLI from working — on a fresh checkout `kaiba db init` and `kaiba probe` have to run
before anything else exists.

Owner note: there is no `withdraw` command and there will not be one. Moving funds out is
an operator action taken in the GMGN app or with a hardware wallet, never through this
tool. See `kaiba/execution/policy.py`.
"""

from __future__ import annotations

import json
import re
import sys
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="kaiba",
    help="Kaiba Ultimate Agent — crypto intelligence, wallet graphing and execution.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

db_app = typer.Typer(help="Database and migrations.", no_args_is_help=True)
wallet_app = typer.Typer(help="Wallet intelligence.", no_args_is_help=True)
token_app = typer.Typer(help="Token scanning and dossiers.", no_args_is_help=True)
run_app = typer.Typer(help="Long-running services.", no_args_is_help=True)
risk_app = typer.Typer(help="Risk envelope and controls.", no_args_is_help=True)
hunt_app = typer.Typer(help="Airdrop, NFT mint and listing hunters.", no_args_is_help=True)
ingest_app = typer.Typer(help="Ingestion listeners.", no_args_is_help=True)
backfill_app = typer.Typer(help="Historical wallet backfill.", no_args_is_help=True)
scan_app = typer.Typer(help="Tier-1 lane scanner (triage queue -> lanes).", no_args_is_help=True)
engine_app = typer.Typer(help="Signal and execution engine.", no_args_is_help=True)
protection_app = typer.Typer(help="Position protection watchdog.", no_args_is_help=True)
positions_app = typer.Typer(help="Read-only position inspection.", no_args_is_help=True)
orders_app = typer.Typer(help="Read-only order inspection.", no_args_is_help=True)
reconcile_app = typer.Typer(help="Order and position reconciliation.", no_args_is_help=True)
signer_app = typer.Typer(help="Isolated transaction signer.", no_args_is_help=True)
journal_app = typer.Typer(help="Append-only operator journal.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(wallet_app, name="wallet")
app.add_typer(token_app, name="token")
app.add_typer(run_app, name="run")
app.add_typer(risk_app, name="risk")
app.add_typer(hunt_app, name="hunt")
# These top-level groups mirror the production systemd units.  The older ``kaiba run
# …`` surface remains registered below as a development-friendly alias.
app.add_typer(ingest_app, name="ingest")
app.add_typer(backfill_app, name="backfill")
app.add_typer(scan_app, name="scan")
app.add_typer(engine_app, name="engine")
app.add_typer(protection_app, name="protection")
app.add_typer(positions_app, name="positions")
app.add_typer(orders_app, name="orders")
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(signer_app, name="signer")
app.add_typer(journal_app, name="journal")

# Keep the operator surface identical to the MCP journal tool and the trade-journaling
# skill.  The core journal remains the owner of the hash chain; this is only its validated
# CLI boundary.
JOURNAL_KINDS = frozenset({"observation", "lesson", "experiment", "change", "outcome", "correction"})
MAX_JOURNAL_BODY = 4_000


def _fail(message: str, hint: str | None = None) -> None:
    # Error text is also consumed by service checks and shell operators. Keep a long path
    # or provider name on one logical line instead of Rich folding it at the terminal width
    # and making a stable phrase impossible to grep.
    console.print(f"[red]error:[/red] {message}", overflow="ignore", crop=False)
    if hint:
        console.print(f"[dim]{hint}[/dim]", overflow="ignore", crop=False)
    raise typer.Exit(1)


def _missing(module: str, exc: Exception) -> None:
    _fail(f"{module} is not available yet ({exc})", "This subsystem is still landing; see docs/TASKS.md.")


def _echo_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, default=str))


def _chain_or_fail(value: str) -> Any:
    """``Chain`` or a readable exit. Imported late so `kaiba version` needs no core."""
    from kaiba.core.schemas import Chain

    try:
        return Chain(value)
    except ValueError:
        _fail(f"unknown chain {value!r}", "one of: " + ", ".join(c.value for c in Chain))
        raise  # unreachable; _fail raises typer.Exit


def _helius_notice(credits: int, what: str, conn: Any = None) -> None:
    """Say what a command is about to spend before it spends it.

    An operator typing a command that costs metered credits should see the bill first,
    next to what is left of the month. A ledger that cannot be read is said so plainly
    rather than rendered as a zero.
    """
    remaining: Any = None
    allowance: Any = None
    try:
        from kaiba.providers import helius

        status_ = helius.budget_status(conn=conn)
        remaining, allowance = status_.get("remaining"), status_.get("allowance")
    except Exception as exc:  # noqa: BLE001 - an unreadable ledger must not hide the price
        console.print(f"[dim]helius ledger unreadable ({type(exc).__name__}); price shown anyway[/dim]")
    budget = (
        "budget unknown" if remaining is None
        else f"{remaining} of {allowance} credits left this month"
    )
    console.print(f"[yellow]cost:[/yellow] up to {credits} Helius credit(s) for {what} — {budget}")


def _clustering_view(run: Any) -> dict[str, Any]:
    """Bound one ``ClusteringRun`` to what fits on a screen and in a JSON pipe."""
    quarantined = [
        {
            "component_key": q.component_key,
            "size": q.size,
            "reason": q.reason,
            "edge_types": [e.value for e in q.edge_types],
        }
        for q in run.quarantined
    ]
    return {
        "run_id": run.run_id,
        "chain": run.chain.value,
        "model": run.model,
        "status": run.status,
        "reason": run.reason,
        "started_ms": run.started_ms,
        "finished_ms": run.finished_ms,
        "edges_by_rule": dict(run.edges_by_rule),
        "edges_total": sum(run.edges_by_rule.values()),
        "pruned_edges": run.pruned_edges,
        "addresses_seen": run.addresses_seen,
        "addresses_checked": run.addresses_checked,
        "largest_component": run.largest_component,
        "entities": len(run.entities),
        "entity_members": run.entity_members,
        "largest_entity": run.largest_entity,
        "size_distribution": {str(k): v for k, v in run.size_distribution.items()},
        "quarantined_components": len(quarantined),
        "quarantined_addresses": sum(int(q["size"]) for q in quarantined),
        "quarantined": quarantined,
        "services": len(run.services),
        "helius_credits": run.helius_credits,
        "funding_looked_up": run.funding_looked_up,
        "funding_resolved": run.funding_resolved,
    }


@app.callback()
def main() -> None:
    """Run an implemented Kaiba command."""


@app.command()
def version() -> None:
    """Print the installed package version without loading configuration."""
    typer.echo(f"kaiba {package_version('kaiba')}")


@app.command()
def status(json_out: bool = typer.Option(False, "--json")) -> None:
    """One screen: mode, lanes, positions, providers."""
    from kaiba.core.db import ensure_db

    ensure_db()
    from kaiba.mcp.server import kaiba_status

    data = kaiba_status()
    if json_out:
        return _echo_json(data)

    mode = data["global_mode"].upper()
    colour = {"LIVE": "red", "CANARY": "yellow", "SHADOW": "cyan", "OFF": "dim"}.get(mode, "white")
    console.print(f"\n[bold]Kaiba[/bold]  mode=[{colour}]{mode}[/{colour}]  "
                  f"paused={data['entries_paused']}  reduce_only={data['reduce_only']}  "
                  f"kill={data['kill_switch']}")
    console.print(f"[dim]withdrawals: {data['withdrawals']}[/dim]\n")

    lanes = Table("lane", "configured", "effective", title="Lanes", title_justify="left")
    for name, modes in data["lanes"].items():
        lanes.add_row(name, modes["configured"], modes["effective"])
    console.print(lanes)

    if data["open_positions"]:
        pos = Table("token", "lane", "mode", "qty", "cost", "protected", title="Open positions")
        for p in data["open_positions"]:
            pos.add_row(p["token"][:16], p["lane"], p["mode"], str(p["qty"]),
                        str(p["cost_native"]), "yes" if p["protected"] else "NO")
        console.print(pos)
    else:
        console.print("[dim]no open positions[/dim]")

    if data["providers"]:
        prov = Table("provider", "credit", "inflight", "penalty", "bans", title="Providers")
        for p in data["providers"]:
            prov.add_row(p["provider"], f"{p['credit']}/{p['capacity']}", str(p["inflight"]),
                         str(p["penalty_level"]), str(len(p["family_bans"])))
        console.print(prov)


@app.command()
def probe(json_out: bool = typer.Option(False, "--json")) -> None:
    """Check every provider credential and endpoint. Never prints a secret."""
    try:
        from kaiba.cli.probe import render, run_probe
    except ImportError as exc:
        return _missing("provider probe (task P0-4)", exc)
    results = run_probe()
    if json_out:
        return _echo_json(results)
    # probe.render carries status and latency; the table here used to drop both.
    render(results, console)


@app.command()
def watch(kinds: str = typer.Option("", help="Comma-separated event kinds to filter.")) -> None:
    """Follow the event bus in the terminal."""
    from kaiba.core.db import ensure_db
    from kaiba.core.events import follow

    ensure_db()
    filt = [k.strip() for k in kinds.split(",") if k.strip()] or None
    console.print("[dim]following events; ctrl-c to stop[/dim]")
    try:
        for event in follow(kinds=filt):
            when = event.ts_ms % 86_400_000 // 1000
            console.print(
                f"[dim]{when:>6}[/dim] [cyan]{event.kind:<20}[/cyan] "
                f"{(event.subject or '')[:18]:<18} {json.dumps(event.payload, default=str)[:90]}"
            )
    except KeyboardInterrupt:
        pass


@app.command()
def dashboard(
    host: str = typer.Option("", help="Override the bind host."),
    port: int = typer.Option(0, help="Override the port."),
) -> None:
    """Serve the operator dashboard."""
    from kaiba.core.config import REPO_ROOT, get_settings
    from kaiba.core.db import ensure_db

    # dashboard/ is a sibling of the kaiba package, not an installed distribution, so an
    # editable install started from any other directory cannot import it.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        import uvicorn

        from dashboard.app import app as dash_app
    except ImportError as exc:
        return _missing("dashboard (task P1-9)", exc)

    ensure_db()
    settings = get_settings()
    uvicorn.run(
        dash_app,
        host=host or settings.kaiba_dashboard_host,
        port=port or settings.kaiba_dashboard_port,
        log_level=settings.kaiba_log_level.lower(),
    )


@app.command()
def mcp() -> None:
    """Run the MCP server on stdio (Hermes launches this itself)."""
    from kaiba.mcp.server import main as mcp_main

    mcp_main()


# ---------------------------------------------------------------- db


@db_app.command("init")
def db_init() -> None:
    """Create the database and apply migrations."""
    from kaiba.core.config import get_settings
    from kaiba.core.db import get_conn, migrate

    conn = get_conn()
    applied = migrate(conn, verbose=True)
    console.print(f"[green]ok[/green] {get_settings().db_path}")
    console.print(f"applied {len(applied)} migration(s)" if applied else "already up to date")


@db_app.command("stats")
def db_stats() -> None:
    """Row counts per table."""
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    table = Table("table", "rows")
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    for name in names:
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        if n:
            table.add_row(name, str(n))
    console.print(table)


@db_app.command("journal")
def db_journal(limit: int = 20, verify: bool = typer.Option(False, "--verify")) -> None:
    """Read the learning journal, or verify its hash chain."""
    from kaiba.core import journal
    from kaiba.core.db import ensure_db

    ensure_db()
    if verify:
        ok, err = journal.verify()
        console.print("[green]chain intact[/green]" if ok else f"[red]chain broken:[/red] {err}")
        raise typer.Exit(0 if ok else 1)
    for entry in reversed(journal.read(limit=limit)):
        console.print(f"[dim]{entry['seq']:>4}[/dim] [cyan]{entry['kind']:<12}[/cyan] {entry['body'][:120]}")


@journal_app.command("add")
def journal_add(
    kind: str = typer.Argument(..., help="observation | lesson | experiment | change | outcome | correction"),
    body: str = typer.Argument(..., help="The journal text; it is never echoed by the CLI."),
    subject: str | None = typer.Option(None, "--subject", help="Optional subject identifier."),
) -> None:
    """Append one bounded, hash-chained operator record without changing old entries."""
    normalized_kind = kind.strip().lower()
    if normalized_kind not in JOURNAL_KINDS:
        _fail(
            f"unknown journal kind {kind!r}",
            "choose one of: observation, lesson, experiment, change, outcome, correction",
        )

    normalized_body = body.strip()
    if not normalized_body:
        _fail("journal body is empty", "write the observation or decision you are recording")
    if len(normalized_body) > MAX_JOURNAL_BODY:
        _fail(
            f"journal body exceeds {MAX_JOURNAL_BODY} characters",
            "split the record into separate entries; do not paste credentials or provider prose",
        )

    normalized_subject = subject.strip() if subject else None
    if normalized_subject == "":
        normalized_subject = None

    from kaiba.core import journal
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    entry = journal.append(
        cast(journal.JournalKind, normalized_kind),
        normalized_body,
        subject=normalized_subject,
        conn=conn,
    )
    # Do not print the body: an operator can intentionally journal incident metadata while
    # keeping it out of shell history and terminal transcripts. The full chain remains in
    # SQLite for `kaiba db journal --verify` and the dashboard.
    _echo_json(
        {
            "ok": True,
            "seq": entry["seq"],
            "kind": entry["kind"],
            "subject": entry["subject"],
            "hash": entry["entry_hash"][:16],
        }
    )


# ---------------------------------------------------------------- wallet


@wallet_app.command("import")
def wallet_import(
    path: str = typer.Argument(..., help="GMGN JSON-array export to import."),
    notes: str = typer.Option("", help="Notes file whose leading credential lines are skipped."),
    evm_chain: str = typer.Option("eth", help="Chain for EVM rows whose label names none."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report counts without writing."),
) -> None:
    """Import GMGN wallet exports into the wallets table.

    There is deliberately no cohort option. A label is the operator's hypothesis, so an
    import can only ever land a wallet in ``research``, or in ``blacklist`` when the label
    names a scam. Promotion to a trusted cohort requires measured PnL.
    """
    try:
        from kaiba.intelligence.import_wallets import import_files
    except ImportError as exc:
        return _missing("wallet importer (task P0-3)", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    conn = ensure_db()
    try:
        chain = Chain(evm_chain)
    except ValueError:
        _fail(f"unknown chain {evm_chain!r}", f"one of: {', '.join(c.value for c in Chain)}")
    report = import_files(
        path, notes or None, conn=conn, default_evm_chain=chain, dry_run=dry_run
    )
    _echo_json(report.as_dict())


@wallet_app.command("grade")
def wallet_grade(address: str, chain: str = "sol") -> None:
    """Grade one wallet and print the factor breakdown."""
    try:
        from kaiba.intelligence.grade import grade_address
    except ImportError as exc:
        return _missing("grading (Phase 2)", exc)
    score = grade_address(address, chain)
    table = Table("factor", "points", "max")
    for f in score.factors:
        table.add_row(f.name, f"{f.points:.2f}", f"{f.max_points:.0f}")
    console.print(f"[bold]{address}[/bold] grade=[cyan]{score.grade}[/cyan] "
                  f"score={score.score:.1f} weight={score.evidence_weight:.0f} "
                  f"archetype={score.archetype}")
    console.print(table)
    if score.penalties:
        console.print(f"[yellow]penalties:[/yellow] {', '.join(score.penalties)}")
    if score.blockers:
        console.print(f"[red]blockers:[/red] {', '.join(score.blockers)}")


@wallet_app.command("top")
def wallet_top(limit: int = 20, grade: str = "A") -> None:
    """List the best-graded wallets."""
    from kaiba.core.db import ensure_db, fetch_all

    conn = ensure_db()
    rows = fetch_all(
        conn,
        "SELECT address, chain, grade, score, archetype, closed_trades FROM wallet_scores "
        "WHERE grade = ? ORDER BY score DESC LIMIT ?",
        (grade, limit),
    )
    if not rows:
        console.print(f"[dim]no grade-{grade} wallets yet[/dim]")
        return
    table = Table("address", "chain", "score", "archetype", "trades")
    for r in rows:
        table.add_row(r["address"][:44], r["chain"], f"{r['score']:.1f}",
                      r["archetype"], str(r["closed_trades"] or "-"))
    console.print(table)


#: ``clustering.MAX_ENTITY_SIZE`` at the time this command was wired. Typer needs a
#: literal, and a CLI default that has drifted from the module's own is worse than no
#: default at all, so ``tests/test_cli.py`` pins the two together.
CLUSTER_MAX_ENTITY_SIZE = 25


@wallet_app.command("cluster")
def wallet_cluster(
    chain: str = typer.Option("sol", "--chain"),
    fund_credits: int = typer.Option(
        0, "--fund-credits", help="Helius credits the run may spend buying funding facts (0 = none)."
    ),
    max_entity_size: int = typer.Option(
        CLUSTER_MAX_ENTITY_SIZE, "--max-entity-size",
        help="Components larger than this are quarantined instead of called one operator.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the plan without rebuilding."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Rebuild the entity graph through ``clustering.run`` — the pass that quarantines.

    The previous body called ``cluster.derive_all`` + ``entity.build_entities`` +
    ``persist_entities`` directly, which is the naive composition those modules warn
    against: with no hub pruning, no service detection and no size cap it resolved this
    database into a single 876-address "entity". ``clustering.run`` is the same three
    steps with the guards that make the answer mean something, and it records what it
    could not see rather than asserting a component is one hand.
    """
    try:
        from kaiba.intelligence import clustering
    except ImportError as exc:
        return _missing("clustering (kaiba.intelligence.clustering)", exc)
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    if fund_credits > 0:
        _helius_notice(fund_credits, "funding lookups during the clustering rebuild", conn)
    if dry_run:
        summary = clustering.coverage_summary(ch, conn)
        last = clustering.last_run(ch, conn)
        payload = {
            "dry_run": True,
            "chain": ch.value,
            "would_spend_helius_credits": max(0, fund_credits),
            "max_entity_size": max_entity_size,
            "coverage_now": summary,
            "last_run": last,
        }
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] would rebuild {ch.value} with max_entity_size="
            f"{max_entity_size} and a {max(0, fund_credits)}-credit funding budget"
        )
        _echo_json({"coverage_now": summary, "last_run": last})
        return None

    run = clustering.run(
        conn, ch, fund_credits=max(0, fund_credits), max_entity_size=max_entity_size
    )
    payload = _clustering_view(run)
    if json_out:
        return _echo_json(payload)
    console.print(
        f"[bold]clustering[/bold] run {run.run_id} {run.status}"
        + (f" — {run.reason}" if run.reason else "")
    )
    table = Table("measure", "value")
    for key in (
        "addresses_seen", "addresses_checked", "edges_total", "pruned_edges",
        "largest_component", "entities", "entity_members", "largest_entity",
        "quarantined_components", "quarantined_addresses", "services", "helius_credits",
        "funding_looked_up", "funding_resolved",
    ):
        table.add_row(key, str(payload[key]))
    console.print(table)
    if payload["edges_by_rule"]:
        rules = Table("rule", "edges", title="edges by rule")
        for rule, n in sorted(payload["edges_by_rule"].items(), key=lambda kv: -kv[1]):
            rules.add_row(rule, str(n))
        console.print(rules)
    if payload["quarantined"]:
        quar = Table("component", "size", "why", title="quarantined (not called one operator)")
        for item in payload["quarantined"][:10]:
            quar.add_row(str(item["component_key"])[:18], str(item["size"]), str(item["reason"])[:60])
        console.print(quar)
    return None


@wallet_app.command("coverage")
def wallet_coverage(
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What the entity graph knows per address, and which components it refused to resolve.

    ``unchecked`` is the number that matters: an address with no row is not independent,
    it is unexamined, and reading it as independent is how a confluence count gets
    inflated.
    """
    try:
        from kaiba.intelligence import clustering
    except ImportError as exc:
        return _missing("clustering (kaiba.intelligence.clustering)", exc)
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    summary = clustering.coverage_summary(ch, conn)
    held = clustering.quarantine(ch, conn)
    quarantined = [
        {
            "component_key": q.component_key,
            "size": q.size,
            "reason": q.reason,
            "edge_types": [e.value for e in q.edge_types],
            "members": list(q.members[:10]),
        }
        for q in held
    ]
    if json_out:
        return _echo_json({"chain": ch.value, "coverage": summary, "quarantined": quarantined})
    table = Table("coverage", "addresses", title=f"entity-graph coverage · {ch.value}")
    for key, value in summary.items():
        table.add_row(key, str(value))
    console.print(table)
    if not quarantined:
        console.print("[dim]nothing quarantined[/dim]")
        return None
    held_table = Table("component", "size", "why", "edge types", title="quarantined components")
    for item in quarantined:
        held_table.add_row(
            str(item["component_key"])[:18], str(item["size"]), str(item["reason"])[:54],
            ", ".join(item["edge_types"])[:30] or "-",
        )
    console.print(held_table)
    console.print(
        f"[dim]{sum(int(i['size']) for i in quarantined)} address(es) are held in "
        f"{len(quarantined)} component(s); they count as unresolved, never as independent[/dim]"
    )
    return None


# ---------------------------------------------------------------- token


@token_app.command("scan")
def token_scan(address: str, chain: str = "sol") -> None:
    """Build a DYOR dossier for one token."""
    try:
        from kaiba.intelligence.dyor import scan_token
    except ImportError as exc:
        return _missing("DYOR (task P1-4)", exc)
    from kaiba.core.schemas import Chain

    dossier = scan_token(address, Chain(chain))
    console.print(f"[bold]{address}[/bold] grade=[cyan]{dossier.grade}[/cyan] "
                  f"score={dossier.score}")
    if dossier.blockers:
        console.print(f"[red]blockers:[/red] {', '.join(b.value for b in dossier.blockers)}")
    if dossier.warnings:
        console.print(f"[yellow]warnings:[/yellow] {', '.join(w.value for w in dossier.warnings)}")
    if dossier.unknowns:
        console.print(f"[dim]unknown:[/dim] {', '.join(dossier.unknowns)}")


@token_app.command("verify-providers")
def token_verify_providers(
    live: bool = typer.Option(
        False, "--live", help="Call the providers for real instead of replaying recordings."
    ),
    record: bool = typer.Option(
        False, "--record", help="With --live, overwrite tests/fixtures/dyor_live/ with this run."
    ),
    auth: bool = typer.Option(True, "--auth/--no-auth", help="Also probe the GoPlus auth flow."),
    limit: int = typer.Option(0, "--limit", help="Only the first N reference tokens (0 = all)."),
    verbose: bool = typer.Option(False, "--verbose", help="Print every token, not just the failures."),
) -> None:
    """Reconcile GoPlus and RugCheck against chain truth, property by property.

    Provider schemas drift, so this is meant to be re-run after any provider change. It
    replays recorded responses by default; ``--live`` re-reads the mint accounts and calls
    both providers again, which is the only form that can catch a drift.
    """
    try:
        from kaiba.intelligence import dyor_verify as dv
    except ImportError as exc:
        return _missing("provider verification (kaiba.intelligence.dyor_verify)", exc)

    conn = None
    if live:
        try:
            from kaiba.core.db import ensure_db

            conn = ensure_db()
        except Exception as exc:  # noqa: BLE001 - verification must run before the db does
            console.print(f"[yellow]running without the database ({exc})[/yellow]")

    if live and auth:
        finding = dv.verify_goplus_auth(conn=conn)
        console.print("[bold]GoPlus auth[/bold]")
        for note in finding.render():
            colour = "green" if finding.keyless_ok and "FAIL" not in note else "yellow"
            console.print(f"  [{colour}]-[/{colour}] {note}")

    if live:
        tokens = dv.REFERENCE[:limit] if limit > 0 else dv.REFERENCE
        console.print(f"[dim]calling providers for {len(tokens)} token(s); this respects the limiter[/dim]")
        result = dv.reconcile(tokens, conn=conn, record_dir=dv.RECORDING_DIR if record else None)
    else:
        result = dv.replay()
        if not result.reports:
            return _fail("no recordings found", f"run with --live --record to create {dv.RECORDING_DIR}")
        console.print(f"[dim]replaying {len(result.reports)} recorded token(s); use --live to re-check[/dim]")

    table = Table("property", "provider", "agree", "disagree", "absent", "no truth", title="Agreement with chain truth")
    for row in result.table():
        wrong = row["disagree"] > 0
        name = f"[red]{row['property']}[/red]" if wrong and row["blocker"] else row["property"]
        table.add_row(
            name + (" *" if row["blocker"] else ""),
            row["provider"],
            str(row["agree"]),
            f"[red]{row['disagree']}[/red]" if wrong else "0",
            str(row["absent"]),
            str(row["no_truth"]),
        )
    console.print(table)
    console.print("[dim]* the property backs a blocker; 'no truth' means the mint account cannot settle it[/dim]")

    disagreements = result.disagreements
    if disagreements:
        console.print("\n[red]disagreements with chain truth[/red]")
        for label, check in disagreements:
            console.print(f"  {label}: {check.render()}")
    else:
        console.print("\n[green]no provider disagreed with chain truth[/green]")

    if result.cross_conflicts:
        console.print("\n[yellow]providers disagree with each other (no chain truth to settle it)[/yellow]")
        for label, prop in result.cross_conflicts:
            console.print(f"  {label}: {prop}")

    if verbose:
        verdicts = Table("token", "chain", "grade", "blockers", "warnings", title="Dossier the merge would build")
        for report in result.reports:
            dossier = dv.dossier_preview(report)
            verdicts.add_row(
                report.token.label,
                report.token.chain.value,
                dossier.grade.value,
                ", ".join(b.value for b in dossier.blockers) or "-",
                ", ".join(w.value for w in dossier.warnings) or "-",
            )
        console.print(verdicts)

    if disagreements:
        raise typer.Exit(1)


# ---------------------------------------------------------------- run


def _run_ingest(feeds: str) -> None:
    """Dispatch the supervised listener runner with a normalized feed list."""
    try:
        from kaiba.ingest.runner import main as ingest_main
    except ImportError as exc:
        return _missing("ingest (tasks P1-2/P1-6/P1-8)", exc)
    # `runner.REGISTRY` is the one source of truth for what a listener is. This used to
    # expand "all" from REGISTRY and then validate against a hard-coded triple, so adding
    # any listener made `--listeners all` reject its own expansion. (Noted by the
    # robinhood/Pons ingest task, 2026-09-20, which is the change that surfaced it.)
    from kaiba.ingest.runner import REGISTRY

    selected = [item.strip() for item in feeds.split(",") if item.strip()]
    if not selected:
        _fail("at least one ingest listener is required", "use --listeners all or a comma-separated list")
    if selected == ["all"]:
        # "all" is an explicit request for every listener, including the ones left out of
        # the default set. Running with no --listeners gives DEFAULT_FEEDS, which excludes
        # telegram on content-licensing grounds; typing "all" is the operator choosing it.
        selected = list(REGISTRY)
    unknown = [item for item in selected if item not in REGISTRY]
    if unknown:
        _fail(f"unknown ingest listener(s): {', '.join(unknown)}",
              f"known listeners: {', '.join(sorted(REGISTRY))}, all")
    # runner.main owns signal handling and the asyncio loop.  Passing argv instead of
    # calling run_all directly keeps this command identical to ``python -m
    # kaiba.ingest.runner`` and gives systemd one deterministic process boundary.
    result = ingest_main(["--feeds", ",".join(selected)])
    if result:
        raise typer.Exit(result)


@run_app.command("ingest")
def run_ingest(feeds: str = typer.Option("pumpportal,gmgn", help="Comma-separated listeners.")) -> None:
    """Run the real-time listeners (legacy alias; use ``kaiba ingest run`` in production)."""
    _run_ingest(feeds)


@ingest_app.command("run")
def ingest_run(
    listeners: str = typer.Option("all", "--listeners", help="all or comma-separated listeners."),
) -> None:
    """Run the real-time listeners used by ``kaiba-ingest.service``."""
    _run_ingest(listeners)


def _token_flow():  # noqa: ANN202 - typer must not import the module at definition time
    try:
        from kaiba.ingest import token_flow

        return token_flow
    except ImportError as exc:
        _missing("token flow (kaiba.ingest.token_flow)", exc)
        return None


@ingest_app.command("trades")
def ingest_trades(
    token: str = typer.Argument(..., help="Mint address to collect trade flow for."),
    chain: str = typer.Option("sol", "--chain"),
    pages: int = typer.Option(0, "--pages", help="Page budget (0 = the configured default)."),
    since_ms: int = typer.Option(0, "--since-ms", help="Stop paging at this block time (0 = launch)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Collect per-token trade flow from pump.fun's free route into ``swaps``.

    This is the input ``confluence-5``, ``trusted-copy`` and ``sm-trenches`` are blocked
    on: the wallet backfill walks wallet history, so it never produces a row for a token
    tier 1 actually scanned. One request per second, no API key.
    """
    tf = _token_flow()
    if tf is None:
        return None
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        ch = Chain(chain)
    except ValueError:
        return _fail(f"unknown chain {chain!r}", "one of: " + ", ".join(c.value for c in Chain))

    conn = ensure_db()
    created = None
    row = conn.execute(
        "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (ch.value, token)
    ).fetchone()
    if row is not None:
        created = row[0]
    result = tf.collect_trades(
        ch,
        token,
        conn,
        since_ms=since_ms or None,
        created_ms=created,
        max_pages=pages or None,
    )
    conn.commit()
    if json_out:
        return _echo_json(result.as_dict())
    console.print(
        f"[bold]{token[:16]}[/bold] {result.trades_seen} trades over {result.pages} page(s): "
        f"{result.rows_written} new, {result.rows_duplicate} already held"
    )
    console.print(
        f"[dim]complete={result.complete} reason={result.reason} "
        f"coverage_from_ms={result.coverage_from_ms}[/dim]"
    )
    return None


@ingest_app.command("curve-snapshot")
def ingest_curve_snapshot(
    token: str = typer.Argument(..., help="Mint address to snapshot."),
    chain: str = typer.Option("sol", "--chain"),
    no_collect: bool = typer.Option(False, "--no-collect", help="Snapshot only; skip trade flow."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Take one curve snapshot and report the velocity it can derive.

    A rate needs two observations, so the first run on a mint will say it refused and why.
    Run it twice, a minute or so apart, to see ``sol_per_swap``.
    """
    tf = _token_flow()
    if tf is None:
        return None
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain
    from kaiba.execution.scanner import curve_from_payload, fetch_curve_payload

    try:
        ch = Chain(chain)
    except ValueError:
        return _fail(f"unknown chain {chain!r}", "one of: " + ", ".join(c.value for c in Chain))

    conn = ensure_db()
    payload, receipt = fetch_curve_payload(token, conn)
    if payload is None:
        return _fail(f"no curve payload for {token[:16]}", receipt.note or receipt.basis.value)
    curve, note = curve_from_payload(payload)
    if curve is None:
        return _fail(f"curve unusable for {token[:16]}", note)
    enriched, basis = tf.observe(ch, token, curve, conn, collect=not no_collect)
    conn.commit()
    if enriched is None:
        return _fail(f"no snapshot recorded for {token[:16]}", note)
    if json_out:
        return _echo_json({"basis": basis, **{k: str(v) for k, v in enriched.items()}})
    console.print(
        f"[bold]{token[:16]}[/bold] progress {enriched.get('progress_pct')}% of a "
        f"{enriched.get('graduation_sol')} SOL target, {enriched.get('sol_in_curve')} SOL in curve"
    )
    if basis == "sol_per_swap":
        console.print(
            f"[green]sol_per_swap[/green] {enriched.get('sol_per_swap')} "
            f"over {enriched.get('swaps')} swaps ({enriched.get('velocity_window')})"
        )
    else:
        console.print(
            f"[yellow]no per-swap rate[/yellow] basis={basis} "
            f"refused: {enriched.get('velocity_refusal')}"
        )
    return None


@ingest_app.command("flow-status")
def ingest_flow_status(
    chain: str = typer.Option("sol", "--chain"),
    limit: int = typer.Option(10, "--limit", help="Tokens to list."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """How much per-token trade flow and how many curve snapshots we actually hold."""
    tf = _token_flow()
    if tf is None:
        return None
    from kaiba.core.db import ensure_db, fetch_all, fetch_one
    from kaiba.core.schemas import Chain

    try:
        ch = Chain(chain)
    except ValueError:
        return _fail(f"unknown chain {chain!r}", "one of: " + ", ".join(c.value for c in Chain))

    conn = ensure_db()
    totals = fetch_one(
        conn,
        "SELECT COUNT(*) AS swaps, COUNT(DISTINCT token) AS tokens FROM swaps "
        "WHERE chain=? AND source=?",
        (ch.value, tf.SOURCE),
    ) or {"swaps": 0, "tokens": 0}
    snaps = fetch_one(
        conn,
        "SELECT COUNT(*) AS n, COUNT(DISTINCT token) AS tokens FROM curve_snapshots WHERE chain=?",
        (ch.value,),
    ) or {"n": 0, "tokens": 0}
    paired = fetch_all(
        conn,
        "SELECT token, COUNT(*) AS n, MIN(observed_ms) AS first_ms, MAX(observed_ms) AS last_ms "
        "FROM curve_snapshots WHERE chain=? GROUP BY token HAVING n > 1 "
        "ORDER BY last_ms DESC LIMIT ?",
        (ch.value, max(1, limit)),
    )
    payload = {
        "swap_rows_from_token_flow": totals["swaps"],
        "tokens_with_trade_flow": totals["tokens"],
        "curve_snapshots": snaps["n"],
        "tokens_with_snapshots": snaps["tokens"],
        "tokens_with_two_or_more": [dict(r) for r in paired],
    }
    if json_out:
        return _echo_json(payload)
    console.print(
        f"trade flow: [bold]{totals['swaps']}[/bold] swap rows across "
        f"{totals['tokens']} token(s)"
    )
    console.print(
        f"snapshots:  [bold]{snaps['n']}[/bold] across {snaps['tokens']} token(s); "
        f"{len(paired)} listed with a usable pair"
    )
    table = Table(title="tokens with two or more snapshots")
    table.add_column("token")
    table.add_column("snapshots", justify="right")
    table.add_column("span (s)", justify="right")
    for r in paired:
        table.add_row(str(r["token"])[:16], str(r["n"]), f"{(r['last_ms'] - r['first_ms']) / 1000:.0f}")
    if paired:
        console.print(table)


_POSITION_FIELDS = (
    "position_id", "chain", "token", "lane", "mode", "opened_ms", "closed_ms",
    "qty", "qty_total", "cost_native", "proceeds_native", "realized_native",
    "entry_price_usd", "peak_price_usd", "stop_price_usd", "tp_done", "protected",
    "mae_pct", "mfe_pct", "exit_reason",
)


def _position_view(row: dict[str, Any]) -> dict[str, Any]:
    """Keep the operator view bounded and exclude provider protection identifiers."""
    return {key: row[key] for key in _POSITION_FIELDS if key in row}


@positions_app.command("show")
def positions_show(
    position_id: str | None = typer.Argument(
        None, help="Optional position identifier; omit to list the newest positions."
    ),
    include_closed: bool = typer.Option(
        False, "--all", help="Include closed positions in the read-only snapshot."
    ),
    limit: int = typer.Option(
        30, "--limit", min=1, max=50, help="Maximum positions to inspect (1–50)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit bounded JSON instead of a table."),
) -> None:
    """Inspect positions without changing orders, protection, or account state.

    The current market quote is deliberately absent: this command reports the persisted
    position snapshot and never turns a missing price into zero or an invented value.
    """
    from kaiba.core.db import ensure_db
    from kaiba.mcp.server import kaiba_positions

    ensure_db()
    # The MCP read tool caps a result at 50 rows.  Use that cap for an explicit ID so an
    # incident operator can find a position that is older than the default recent list.
    payload = kaiba_positions(include_closed=include_closed, limit=50 if position_id else limit)
    rows = [_position_view(row) for row in payload.get("positions", []) if isinstance(row, dict)]
    if position_id:
        rows = [row for row in rows if row.get("position_id") == position_id]
        if not rows:
            _fail(
                f"position not found: {position_id}",
                "by default only open positions are shown; use --all for a closed position",
            )

    result = {
        "positions": rows,
        "quote": {
            "status": "unavailable",
            "reason": "persisted position snapshot; no live mark was requested",
        },
    }
    if json_out:
        return _echo_json(result)
    if not rows:
        scope = "open" if not include_closed else ""
        suffix = f" {scope}" if scope else ""
        console.print(f"[dim]no{suffix} positions in the persisted snapshot[/dim]")
        console.print("[dim]current quote: unavailable (read-only snapshot)[/dim]")
        return

    table = Table(
        "position", "chain", "token", "lane", "mode", "qty", "cost", "entry USD",
        "stop USD", "protected", "current quote", "opened", "closed", title="Positions",
    )
    for row in rows:
        table.add_row(
            str(row.get("position_id", ""))[:24],
            str(row.get("chain", "")),
            str(row.get("token", ""))[:18],
            str(row.get("lane", "")),
            str(row.get("mode", "")),
            str(row.get("qty", "")),
            str(row.get("cost_native", "")),
            str(row.get("entry_price_usd") or "-"),
            str(row.get("stop_price_usd") or "-"),
            "yes" if row.get("protected") else "NO",
            "unavailable",
            str(row.get("opened_ms", "")),
            str(row.get("closed_ms") or "-"),
        )
    console.print(table)
    console.print("[dim]current quote: unavailable (read-only snapshot; no live mark inferred)[/dim]")
    return None


_ORDER_FIELDS = (
    "order_id", "decision_id", "chain", "token", "side", "lane", "mode", "input_token",
    "output_token", "amount_in", "min_out", "slippage_bps", "state", "provider",
    "provider_order_id", "tx_hash", "filled_out", "fee_native", "created_ms", "updated_ms",
    "error",
)
_ORDER_TERMINAL_STATES = ("filled", "failed", "expired", "cancelled", "rejected")
_ORDER_MAX_EVENTS = 50
_ORDER_UNTRUSTED = re.compile(
    r"(?i)\b(ignore (all|previous|prior)|disregard (all|previous)|system prompt|"
    r"new instructions?|override|jailbreak|private key|seed phrase|send (funds|sol|eth)|"
    r"withdraw to|transfer funds|authorization\s*:\s*bearer|api key)\b"
)
_ORDER_SECRET_VALUE = re.compile(
    r"(?i)\b(private key|seed phrase|api key|authorization\s*:\s*bearer)\b\s*[:=]?\s*\S+"
)


def _order_text(value: Any, *, limit: int = 400) -> str | None:
    """Bound order errors/events before they reach an operator or JSON consumer."""
    if value is None:
        return None
    text = str(value)
    text = _ORDER_SECRET_VALUE.sub("[removed]", text)
    return _ORDER_UNTRUSTED.sub("[removed]", text)[:limit]


def _order_view(conn: Any, row: dict[str, Any], *, with_events: bool) -> dict[str, Any]:
    """Expose the audit fields needed by the incident runbook, never arbitrary columns."""
    view = {key: row[key] for key in _ORDER_FIELDS if key in row}
    view["error"] = _order_text(row.get("error"))
    link = conn.execute(
        "SELECT position_id FROM position_orders WHERE order_id=? ORDER BY ts_ms DESC LIMIT 1",
        (row["order_id"],),
    ).fetchone()
    view["position_id"] = link["position_id"] if link else None
    if with_events:
        events = [
            dict(event)
            for event in conn.execute(
                "SELECT id, ts_ms, state, detail FROM order_events "
                "WHERE order_id=? ORDER BY id DESC LIMIT ?",
                (row["order_id"], _ORDER_MAX_EVENTS),
            )
        ]
        events.reverse()
        view["events"] = [
            {"id": e["id"], "ts_ms": e["ts_ms"], "state": e["state"],
             "detail": _order_text(e.get("detail"))}
            for e in events
        ]
    else:
        view["events"] = []
    return view


@orders_app.command("show")
def orders_show(
    order_id: str | None = typer.Argument(
        None, help="Optional order identifier; omit to list unresolved recent orders."
    ),
    include_terminal: bool = typer.Option(
        False, "--all", help="Include terminal orders in the read-only snapshot."
    ),
    limit: int = typer.Option(
        30, "--limit", min=1, max=50, help="Maximum orders to inspect (1–50)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit bounded JSON instead of a table."),
) -> None:
    """Inspect the order ledger and append-only state history without mutating it."""
    from kaiba.core.db import ensure_db, fetch_all, fetch_one

    conn = ensure_db()
    if order_id:
        row = fetch_one(conn, "SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not row:
            _fail(f"order not found: {order_id}", "check the recorded order identifier")
        rows = [row]
    else:
        where = "" if include_terminal else (
            " WHERE state NOT IN (?,?,?,?,?)"
        )
        params: list[Any] = [] if include_terminal else list(_ORDER_TERMINAL_STATES)
        rows = fetch_all(
            conn,
            "SELECT * FROM orders" + where + " ORDER BY updated_ms DESC, created_ms DESC LIMIT ?",
            [*params, limit],
        )

    views = [_order_view(conn, row, with_events=bool(order_id)) for row in rows]
    result = {
        "orders": views,
        "scope": "id" if order_id else ("all" if include_terminal else "unresolved"),
    }
    if json_out:
        return _echo_json(result)
    if not views:
        scope = "all" if include_terminal else "unresolved"
        console.print(f"[dim]no {scope} orders in the persisted snapshot[/dim]")
        return

    table = Table(
        "order", "state", "chain", "token", "side", "lane", "mode", "provider",
        "provider id", "tx/signature", "position", "created", "updated", "error",
        title="Orders",
    )
    for row in views:
        table.add_row(
            str(row.get("order_id", ""))[:24],
            str(row.get("state", "")),
            str(row.get("chain", "")),
            str(row.get("token", ""))[:18],
            str(row.get("side", "")),
            str(row.get("lane", "")),
            str(row.get("mode", "")),
            str(row.get("provider", "")),
            str(row.get("provider_order_id") or "-")[:18],
            str(row.get("tx_hash") or "-")[:18],
            str(row.get("position_id") or "-"),
            str(row.get("created_ms", "")),
            str(row.get("updated_ms", "")),
            str(row.get("error") or "")[:32],
        )
    console.print(table)
    if order_id:
        console.print(f"[dim]state history: {len(views[0]['events'])} event(s); read-only[/dim]")
    return None


# ---------------------------------------------------------------- backfill


@backfill_app.command("wallets")
def backfill_wallets_cmd(
    limit: int = typer.Option(25, "--limit", help="How many wallets to touch this run."),
    chain: str = typer.Option("sol", "--chain", help="Only 'sol' has a history provider."),
    pages: int = typer.Option(1, "--pages", help="History pages per wallet per run."),
    page_size: int = typer.Option(100, "--page-size", help="Transactions per page (max 100)."),
    cohort: str = typer.Option("", "--cohort", help="Comma-separated wallet cohorts to include."),
    max_credits: int = typer.Option(
        25_000, "--max-credits", help="Hard ceiling on Helius credits for this run."
    ),
    no_meta: bool = typer.Option(False, "--no-meta", help="Skip the signer/lookup-table pass."),
    fresh: bool = typer.Option(False, "--fresh", help="Fetch only what is newer than the cursor."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify without writing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Pull wallet swap history from Helius into `swaps`, `first_buyers` and `swap_meta`."""
    try:
        from kaiba.ingest.backfill import backfill_wallets
    except ImportError as exc:
        return _missing("wallet backfill (Phase 1)", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        target_chain = Chain(chain)
    except ValueError:
        _fail(f"unknown chain {chain!r}", f"one of: {', '.join(c.value for c in Chain)}")
    conn = ensure_db()
    report = backfill_wallets(
        conn,
        target_chain,
        limit=limit,
        pages=pages,
        page_limit=page_size,
        cohorts=[c.strip() for c in cohort.split(",") if c.strip()] or None,
        with_meta=not no_meta,
        fresh=fresh,
        dry_run=dry_run,
        max_credits=max_credits,
    )
    if json_out:
        return _echo_json(report.as_dict())

    console.print(
        f"[bold]backfill[/bold] {report.wallets} wallet(s), {report.pages} page(s), "
        f"{report.transactions} transaction(s)"
    )
    table = Table("metric", "value")
    table.add_row("swaps written", str(report.swaps_written))
    table.add_row("swaps already present", str(report.swaps_duplicate))
    table.add_row("first_buyers upserted", str(report.first_buyers_written))
    table.add_row("swap_meta rows", str(report.meta_written))
    table.add_row("tip transfers", str(report.transfers_written))
    table.add_row("unclassifiable", f"{report.ambiguous} ({report.classification_skip_rate:.1%})")
    table.add_row("helius credits", str(report.credits_spent))
    table.add_row("wallets failed", str(report.wallets_failed))
    console.print(table)
    if report.skips:
        skips = Table("skip reason", "count")
        for reason, count in sorted(report.skips.items(), key=lambda kv: -kv[1]):
            skips.add_row(reason, str(count))
        console.print(skips)
    if report.stopped:
        console.print(f"[yellow]stopped early:[/yellow] {report.stopped}")


@backfill_app.command("status")
def backfill_status_cmd(
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill progress and Helius credit spend."""
    try:
        from kaiba.ingest.backfill import backfill_status
    except ImportError as exc:
        return _missing("wallet backfill (Phase 1)", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        target_chain = Chain(chain)
    except ValueError:
        _fail(f"unknown chain {chain!r}", f"one of: {', '.join(c.value for c in Chain)}")
    data = backfill_status(ensure_db(), target_chain)
    if json_out:
        return _echo_json(data)

    budget = data["helius"]
    table = Table("metric", "value", title=f"backfill · {data['chain']}")
    for key in (
        "wallets_tracked", "wallets_started", "wallets_exhausted", "swaps", "swaps_buy",
        "swaps_sell", "swap_wallets", "swap_tokens", "first_buyers", "swap_meta",
        "swap_meta_matched_pct", "transfers", "wallets_with_evidence",
    ):
        table.add_row(key, str(data[key]))
    console.print(table)
    console.print(
        f"[bold]helius[/bold] {budget['used']}/{budget['allowance']} credits used "
        f"({budget['pct_used']}%), {budget['remaining']} left, "
        f"{budget['estimated_credits']} of those charged at an unverified rate, "
        f"resets in {budget['resets_in_s'] // 3600}h"
    )


@backfill_app.command("creators")
def backfill_creators_cmd(
    pages: int = typer.Option(4, "--pages", help="Discovery pages (70 launches each, cap 15)."),
    chain: str = typer.Option("sol", "--chain", help="Only 'sol' has a launch-history provider."),
    index: str = typer.Option(
        "created", "--index", help="created | complete | both. 'complete' finds graduating creators."
    ),
    max_creators: int = typer.Option(
        0, "--max-creators", help="Cap the per-creator pass. 0 means every creator discovered."
    ),
    interval: float = typer.Option(1.0, "--interval", help="Seconds between requests. Do not lower."),
    refresh_hours: float = typer.Option(
        12.0, "--refresh-hours", help="Skip creators refreshed more recently than this."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Measure without writing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Backfill pump.fun creator launch history into `creators` — the input tier-0 triage lacks."""
    try:
        from kaiba.intelligence.creators import (
            INDEX_COMPLETE,
            INDEX_CREATED,
            BackfillConfig,
            backfill_creators,
        )
    except ImportError as exc:
        return _missing("creator history", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        target_chain = Chain(chain)
    except ValueError:
        _fail(f"unknown chain {chain!r}", f"one of: {', '.join(c.value for c in Chain)}")
    indexes = {
        "created": (INDEX_CREATED,),
        "complete": (INDEX_COMPLETE,),
        "both": (INDEX_CREATED, INDEX_COMPLETE),
    }.get(index.strip().lower())
    if indexes is None:
        _fail(f"unknown index {index!r}", "one of: created, complete, both")

    report = backfill_creators(
        ensure_db(),
        chain=target_chain,
        pages=pages,
        indexes=indexes,
        max_creators=max_creators or None,
        config=BackfillConfig(min_interval_s=interval, refresh_after_s=refresh_hours * 3600.0),
        dry_run=dry_run,
    )
    if json_out:
        return _echo_json(report.as_dict())

    console.print(
        f"[bold]creators[/bold] {report.creators_written} written, "
        f"{report.creators_failed} unmeasurable, {report.elapsed_s:.0f}s"
    )
    table = Table("metric", "value")
    table.add_row("discovery pages ok/failed", f"{report.index_pages_ok}/{report.index_pages_failed}")
    table.add_row("launches sampled", str(report.index_coins))
    table.add_row("creators discovered", str(report.creators_discovered))
    table.add_row("creators skipped (fresh)", str(report.creators_skipped_fresh))
    table.add_row("lifetime coins measured", str(report.lifetime_coins))
    table.add_row("lifetime graduated", str(report.lifetime_graduated))
    rate = report.graduation_rate
    table.add_row("graduation rate", "unknown" if rate is None else f"{rate:.2%}")
    table.add_row("creators with a graduate", str(report.creators_with_a_graduate))
    table.add_row("histories truncated at cap", str(report.creators_truncated))
    table.add_row("requests", str(report.requests))
    console.print(table)

    dist = report.distribution
    if dist.get("creators"):
        buckets = Table("launches per creator", "creators", title="lifetime distribution")
        for label, count in dist["buckets"].items():
            buckets.add_row(label, str(count))
        console.print(buckets)
        share = dist.get("top1pct_share")
        if share is not None:
            console.print(
                f"[bold]top 1%[/bold] ({dist['top1pct_creators']} creators) produced "
                f"{share:.1%} of {dist['coins']} coins; top 5% produced "
                f"{dist.get('top5pct_share', 0):.1%}"
            )
    collapse = report.collapse
    if collapse.get("coins_reached_floor"):
        console.print(
            f"[yellow]rugged not written.[/yellow] The collapse proxy "
            f"({collapse['definition']}) fires on {collapse['coins_collapsed']} of "
            f"{collapse['coins_reached_floor']} coins that reached the floor and would put "
            f"{collapse['creators_over_reject_bar_pct']:.1%} of measured creators over "
            f"triage's reject bar."
        )
    if report.dry_run:
        console.print("[yellow]dry run:[/yellow] nothing written")


@backfill_app.command("creators-status")
def backfill_creators_status_cmd(
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What `creators` knows, and how much of it tier-0 triage can actually promote on."""
    try:
        from kaiba.intelligence.creators import creator_status
    except ImportError as exc:
        return _missing("creator history", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        target_chain = Chain(chain)
    except ValueError:
        _fail(f"unknown chain {chain!r}", f"one of: {', '.join(c.value for c in Chain)}")
    data = creator_status(ensure_db(), chain=target_chain)
    if json_out:
        return _echo_json(data)

    table = Table("metric", "value", title=f"creators · {data['chain']}")
    for key in (
        "rows", "launches_total", "graduated_total", "graduation_rate", "triage_promotable",
        "serial_launchers_5plus", "serial_launchers_60plus", "median_peak_mcap_usd_rows",
        "median_of_median_peaks_usd", "tokens_table_rows",
    ):
        table.add_row(key, str(data[key]))
    console.print(table)
    dist = data["distribution"]
    if dist.get("creators"):
        buckets = Table("launches per creator", "creators")
        for label, count in dist["buckets"].items():
            buckets.add_row(label, str(count))
        console.print(buckets)
        if dist.get("top1pct_share") is not None:
            console.print(
                f"top 1% ({dist['top1pct_creators']}) produced {dist['top1pct_share']:.1%} "
                f"of {dist['coins']} measured coins"
            )
    console.print(f"[dim]rugged: {data['rugged_decision']}[/dim]")


def _scanner():  # noqa: ANN202 - typer must not import the module at definition time
    try:
        from kaiba.execution import scanner
    except ImportError as exc:
        return _missing("tier-1 scanner", exc)
    return scanner


def _print_scan_results(results: list[Any]) -> None:
    if not results:
        console.print("[dim]nothing to scan; the triage queue was empty[/dim]")
        return
    table = Table("token", "src", "grade", "secs", "dossier", "curve", "buys", "lanes")
    for r in results:
        table.add_row(
            r.token[:16],
            r.source,
            (r.dossier_grade.value if r.dossier_grade else "-"),
            f"{r.elapsed_ms / 1000:.1f}",
            "reused" if r.dossier_reused else f"{r.dossier_ms / 1000:.1f}s",
            # Why a lane could not see the curve matters more than that it could not.
            "ok" if r.curve_ok else (r.curve_note or "-")[:22],
            str(r.recent_buys),
            ", ".join(r.lanes_fired) or ("[red]error[/red]" if not r.ok else "-"),
        )
    console.print(table)
    for r in results:
        if not r.ok:
            console.print(f"[red]{r.token[:16]}[/red] {r.error}")


@scan_app.command("once")
def scan_once(
    token: str = typer.Option("", "--token", help="Scan this mint instead of the queue."),
    chain: str = typer.Option("sol", "--chain"),
    limit: int = typer.Option(0, "--limit", help="Queue items to drain (0 = the configured batch)."),
    force: bool = typer.Option(False, "--force", help="Rebuild the dossier even if a fresh one exists."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run tier 1 once: assemble a LaneContext, evaluate every lane, record the signals.

    With ``--token`` this is the operator's "what would the lanes say about this?" — it
    does not wait for the queue, and it is the only way to interrogate a mint the tier-0
    screen deferred.
    """
    scanner = _scanner()
    if scanner is None:
        return None
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Chain

    try:
        ch = Chain(chain)
    except ValueError:
        return _fail(f"unknown chain {chain!r}", "one of: " + ", ".join(c.value for c in Chain))

    conn = ensure_db()
    if token:
        results = [scanner.scan(ch, token, conn, source="token", force_dossier=force)]
    else:
        results = scanner.run_once(conn, limit=limit or None)
    if json_out:
        return _echo_json(
            {"results": [r.as_dict() for r in results], "summary": scanner.lane_silence_report(results)}
        )
    _print_scan_results(results)
    summary = scanner.lane_silence_report(results)
    if results and not summary["signals"]:
        # Say so plainly. A silent pass is the expected outcome today and the reason is
        # more useful than the zero.
        console.print(
            f"[yellow]no lane fired[/yellow] on {summary['ok']} token(s); "
            f"curve seen on {summary['curve_ok']}, tokens with recent buys "
            f"{summary['with_recent_buys']}, grades {summary['dossier_grades']}"
        )
    return None


@scan_app.command("run")
def scan_run(
    workers: int = typer.Option(1, "--workers", help="Parallel tier-1 passes."),
    batch: int = typer.Option(0, "--batch", help="Queue items drained per cycle (0 = default)."),
    seconds: float = typer.Option(0.0, "--seconds", help="Stop after this long (0 = forever)."),
    tokens: int = typer.Option(0, "--tokens", help="Stop after this many passes (0 = unlimited)."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the tier-1 loop as a service, reporting the rate it actually sustained."""
    scanner = _scanner()
    if scanner is None:
        return None
    from dataclasses import replace

    from kaiba.core.db import ensure_db

    config = scanner.DEFAULT_CONFIG
    if workers > 1 or batch:
        config = replace(
            config,
            workers=max(1, workers),
            batch=batch or config.batch,
        )
    conn = ensure_db()
    seen: list[Any] = []

    def _report(result: Any) -> None:
        seen.append(result)
        if not json_out:
            console.print(
                f"[dim]{result.ts_ms}[/dim] {result.token[:16]} "
                f"{result.elapsed_ms / 1000:.1f}s "
                f"grade={result.dossier_grade.value if result.dossier_grade else '-'} "
                f"lanes={', '.join(result.lanes_fired) or 'none'}"
            )

    try:
        stats = scanner.run_loop(
            conn,
            config=config,
            max_seconds=seconds or None,
            max_tokens=tokens or None,
            on_result=_report,
        )
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        console.print("[dim]stopped[/dim]")
        return None
    payload = {"rate": scanner.measure_rate(stats), "summary": scanner.lane_silence_report(seen)}
    if json_out:
        return _echo_json(payload)
    _echo_json(payload)
    return None


@scan_app.command("status")
def scan_status(json_out: bool = typer.Option(False, "--json")) -> None:
    """Queue depth, tier-0 latency and what tier 1 has recorded so far."""
    scanner = _scanner()
    if scanner is None:
        return None
    from kaiba.core.db import ensure_db, fetch_one
    from kaiba.execution import triage

    conn = ensure_db()
    since = 0
    try:
        from kaiba.core.schemas import now_ms

        since = now_ms() - 24 * 3600 * 1000
    except ImportError:  # pragma: no cover
        pass
    scanned = fetch_one(
        conn, "SELECT COUNT(*) AS n FROM events WHERE kind=? AND ts_ms>=?", (scanner.EVENT_SCANNED, since)
    )
    signals = fetch_one(conn, "SELECT COUNT(*) AS n FROM signals WHERE created_ms>=?", (since,))
    payload = {
        "queue": triage.get_queue().stats().as_dict(),
        "tier0_latency_us": triage.latency_report(),
        "tier0_verdicts_24h": triage.verdict_split(since, conn),
        "tier1_scans_24h": int(scanned["n"]) if scanned else 0,
        "signals_24h": int(signals["n"]) if signals else 0,
    }
    if json_out:
        return _echo_json(payload)
    _echo_json(payload)
    return None


def _run_engine(once: bool = False) -> None:
    """Evaluate lanes and record decisions."""
    try:
        from kaiba.execution.engine import run_loop, run_once
    except ImportError as exc:
        return _missing("signal engine (Phase 3)", exc)
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    if once:
        decisions = run_once(conn)
        console.print(f"{len(decisions)} decision(s)")
        for d in decisions:
            console.print(f"  [cyan]{d.lane}[/cyan] {d.action} {d.token[:16]} {d.thesis[:60]}")
    else:
        run_loop(conn)


@run_app.command("engine")
def run_engine(once: bool = typer.Option(False, "--once")) -> None:
    """Evaluate lanes and record decisions (legacy alias)."""
    _run_engine(once)


@engine_app.command("run")
def engine_run(once: bool = typer.Option(False, "--once")) -> None:
    """Evaluate lanes and record decisions for ``kaiba-engine.service``."""
    _run_engine(once)


def _run_protection() -> None:
    """Run the exit watchdog. This is a service, not a cron prompt."""
    try:
        from kaiba.execution.protection import run_watchdog
    except ImportError as exc:
        return _missing("protection (Phase 4)", exc)
    run_watchdog()


@run_app.command("protection")
def run_protection() -> None:
    """Run the exit watchdog (legacy alias)."""
    _run_protection()


@protection_app.command("run")
def protection_run() -> None:
    """Run the exit watchdog for ``kaiba-protection.service``."""
    _run_protection()


@reconcile_app.command("once")
def reconcile_once(
    write_reports: bool = typer.Option(
        False, "--write-reports", help="Publish the six Hermes read-only reports after the sweep."
    ),
) -> None:
    """Reconcile unresolved orders once, then optionally publish Hermes reports."""
    try:
        from kaiba.execution.executor import reconcile_all
    except ImportError as exc:
        return _missing("order reconciliation (Phase 4)", exc)
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    reconciled = reconcile_all(conn)
    result: dict[str, Any] = {"reconciled": reconciled}
    if write_reports:
        try:
            from kaiba.cli.reports import write_reports as publish_reports

            result["reports"] = [str(path) for path in publish_reports()]
        except Exception as exc:  # noqa: BLE001 - report failure must be visible to systemd
            _fail(f"could not publish reports: {type(exc).__name__}: {exc}")
    _echo_json(result)


@signer_app.command("serve")
def signer_serve(
    socket: str = typer.Option("", "--socket", help="Unix socket path."),
    socket_group: str = typer.Option("", "--socket-group", help="Group allowed to connect."),
    socket_mode: str = typer.Option("0660", "--socket-mode", help="Unix mode in octal."),
) -> None:
    """Run the isolated signer service used by the execution engine."""
    try:
        mode = int(socket_mode, 8)
    except ValueError:
        _fail(f"invalid socket mode {socket_mode!r}", "use an octal value such as 0660")
    if not 0 <= mode <= 0o777:
        _fail("socket mode must be between 0000 and 0777")
    try:
        from kaiba.execution.signer import serve
    except ImportError as exc:
        return _missing("signer (Phase 4)", exc)
    serve(
        Path(socket) if socket else None,
        socket_group=socket_group or None,
        socket_mode=mode,
    )


@run_app.command("reflect")
def run_reflect(days: int = 1) -> None:
    """Build the nightly review packet (the model step happens in Hermes)."""
    try:
        from kaiba.learning.reflect import build_review
    except ImportError as exc:
        return _missing("learning loop (Phase 5)", exc)
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import now_ms

    conn = ensure_db()
    packet = build_review(conn, now_ms() - days * 86_400_000, now_ms())
    _echo_json(packet.model_dump() if hasattr(packet, "model_dump") else packet)


# ---------------------------------------------------------------- risk


@risk_app.command("show")
def risk_show() -> None:
    """Print the current envelope."""
    from kaiba.core.config import get_risk

    risk = get_risk()
    console.print(f"[bold]mode[/bold] {risk.global_mode.value}  kill={risk.kill_switch}  "
                  f"paused={risk.entries_paused}  reduce_only={risk.reduce_only}")
    bounds = Table("bound", "value", title="Operator envelope (agent cannot widen)")
    for k, v in risk.bounds.model_dump().items():
        bounds.add_row(k, str(v))
    console.print(bounds)
    lanes = Table("lane", "mode", "effective", "size %")
    for lane_id, cfg in risk.lanes.items():
        lanes.add_row(lane_id.value, cfg.mode.value, risk.effective_mode(lane_id).value,
                      f"{cfg.size_pct_min}–{cfg.size_pct_max}")
    console.print(lanes)


@risk_app.command("pause")
def risk_pause(reason: str = "operator") -> None:
    """Stop new entries. Exits keep running."""
    from kaiba.core.db import ensure_db
    from kaiba.mcp.server import kaiba_pause

    ensure_db()
    _echo_json(kaiba_pause(reason))


@risk_app.command("resume")
def risk_resume(
    reason: str = "operator",
    clear_kill: bool = typer.Option(
        False,
        "--clear-kill",
        help="Also clear the hard kill switch; this is a deliberate operator action.",
    ),
) -> None:
    """Allow new entries again, optionally clearing the hard kill switch."""
    from kaiba.core.db import ensure_db
    from kaiba.mcp.server import kaiba_resume

    ensure_db()
    if not clear_kill:
        _echo_json(kaiba_resume(reason))
        return

    # The MCP tool intentionally cannot clear the kill switch.  Keep that authority on
    # this local operator command, and write both flags in one config update so a reload
    # cannot observe a half-resumed state.
    from kaiba.core import events as ev
    from kaiba.core import journal
    from kaiba.core.config import get_risk, save_risk
    from kaiba.core.schemas import EventKind

    risk = get_risk()
    risk.entries_paused = False
    risk.kill_switch = False
    save_risk(risk)
    body = f"entries resumed; kill switch cleared: {reason[:200]}"
    conn = ensure_db()
    journal.append("change", body, conn=conn)
    ev.emit(EventKind.SYSTEM, {"action": "resume", "clear_kill": True, "reason": reason[:200]}, conn=conn)
    _echo_json({"entries_paused": False, "kill_switch": False})


@risk_app.command("reduce-only")
def risk_reduce_only(
    on: bool = typer.Option(False, "--on", help="Allow exits while refusing new entries."),
    off: bool = typer.Option(False, "--off", help="Disable reduce-only mode."),
    reason: str = typer.Option("operator", "--reason", help="Why the control changed."),
) -> None:
    """Toggle reduce-only mode; exactly one of ``--on`` or ``--off`` is required."""
    if on == off:
        _fail("choose exactly one of --on or --off")
    from kaiba.core.db import ensure_db
    from kaiba.mcp.server import kaiba_reduce_only

    ensure_db()
    _echo_json(kaiba_reduce_only(on, reason))


@risk_app.command("lane")
def risk_lane(
    lane: str = typer.Argument(..., help="Lane identifier, such as confluence-5."),
    mode: str = typer.Option(..., "--mode", help="off | shadow | canary | live."),
    reason: str = typer.Option("operator", "--reason", help="Why the lane changed."),
) -> None:
    """Set one lane's mode without exceeding the operator envelope ceiling."""
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import Lane, LaneMode
    from kaiba.mcp.server import kaiba_set_lane_mode

    try:
        Lane(lane)
        LaneMode(mode)
    except ValueError as exc:
        _fail(str(exc), "check `kaiba risk show` for valid lanes and modes")
    ensure_db()
    result = kaiba_set_lane_mode(lane, mode, reason)
    if not result.get("ok", False):
        _fail(str(result.get("reason", "lane change refused")))
    _echo_json(result)


@risk_app.command("global")
def risk_global(
    mode: str = typer.Option(..., "--mode", help="off | shadow | canary | live."),
    reason: str = typer.Option("operator", "--reason", help="Why the global posture changed."),
) -> None:
    """Set the global posture; the operator envelope still caps effective modes."""
    from kaiba.core import events as ev
    from kaiba.core import journal
    from kaiba.core.config import get_risk, save_risk
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import EventKind, LaneMode

    try:
        new_mode = LaneMode(mode)
    except ValueError as exc:
        _fail(str(exc), "valid modes: off, shadow, canary, live")
    conn = ensure_db()
    risk = get_risk()
    previous = risk.global_mode.value
    risk.global_mode = new_mode
    save_risk(risk)
    body = f"global mode {previous} -> {new_mode.value}: {reason[:200]}"
    journal.append("change", body, conn=conn)
    ev.emit(
        EventKind.PARAM_CHANGE,
        {"scope": "global", "from": previous, "mode": new_mode.value, "reason": reason[:200]},
        conn=conn,
    )
    _echo_json({"ok": True, "global_mode": new_mode.value})


@risk_app.command("kill")
def risk_kill(
    confirm: bool = typer.Option(False, "--yes", help="Required. Halts every lane."),
) -> None:
    """Hard stop: every lane off. Positions stay open; protection keeps running."""
    if not confirm:
        _fail("refusing without --yes", "kaiba risk kill --yes")
    from kaiba.core import journal
    from kaiba.core.config import get_risk, save_risk
    from kaiba.core.db import ensure_db

    ensure_db()
    risk = get_risk()
    risk.kill_switch = True
    save_risk(risk)
    journal.append("change", "kill switch engaged by operator")
    console.print("[red]kill switch engaged[/red] — all lanes OFF. Protection still runs.")


@risk_app.command("preflight")
def risk_preflight() -> None:
    """Run the pre-arming checks."""
    try:
        from deploy.preflight import run_preflight
    except ImportError as exc:
        return _missing("preflight (Phase 7)", exc)
    raise typer.Exit(run_preflight())


# ---------------------------------------------------------------- hunt


_HUNTERS = {
    "airdrop": "kaiba.hunters.airdrops",
    "nft": "kaiba.hunters.nft",
    "listing": "kaiba.hunters.listings",
    "signals": "kaiba.hunters.signals",
}


@hunt_app.command("refresh")
def hunt_refresh(
    kind: str = typer.Argument("all", help="airdrop | nft | listing | signals | all"),
) -> None:
    """Refresh a hunter's registry. Most results are ranked skips, which is correct."""
    import importlib

    from kaiba.core.db import ensure_db

    conn = ensure_db()
    kinds = list(_HUNTERS) if kind == "all" else [kind]
    table = Table("hunter", "found", "detail")
    for k in kinds:
        if k not in _HUNTERS:
            _fail(f"unknown hunter {k!r}", f"one of: {', '.join(_HUNTERS)} or all")
        try:
            mod = importlib.import_module(_HUNTERS[k])
            table.add_row(k, str(mod.refresh(conn)), "")
        except ImportError as exc:
            table.add_row(k, "-", f"[dim]not available: {exc}[/dim]")
        except Exception as exc:  # noqa: BLE001 - a scraper breaking is normal
            table.add_row(k, "-", f"[yellow]{type(exc).__name__}: {exc}[/yellow]"[:70])
    console.print(table)


@hunt_app.command("report")
def hunt_report(kind: str = typer.Argument("airdrop", help="airdrop | nft | listing | signals")) -> None:
    """Print a hunter's ranked weekly report."""
    import importlib

    from kaiba.core.db import ensure_db

    if kind not in _HUNTERS:
        _fail(f"unknown hunter {kind!r}", f"one of: {', '.join(_HUNTERS)}")
    conn = ensure_db()
    try:
        mod = importlib.import_module(_HUNTERS[kind])
    except ImportError as exc:
        return _missing(f"{kind} hunter", exc)
    console.print(mod.weekly_report(conn))


@hunt_app.command("list")
def hunt_list(kind: str = typer.Option("", help="Filter by kind."), limit: int = 15) -> None:
    """Ranked opportunities across the hunters."""
    from kaiba.core.db import ensure_db, fetch_all

    conn = ensure_db()
    sql = "SELECT kind, name, chain, status, ev_score, cost_usd FROM opportunities"
    params: list[Any] = []
    if kind:
        sql += " WHERE kind = ?"
        params.append(kind)
    sql += " ORDER BY COALESCE(ev_score, -1e9) DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(conn, sql, params)
    if not rows:
        console.print("[dim]no opportunities recorded yet; run `kaiba hunt refresh`[/dim]")
        return
    table = Table("kind", "name", "chain", "status", "EV $", "cost $")
    for r in rows:
        ev_val = r["ev_score"]
        colour = "green" if (ev_val or 0) > 0 else "dim"
        table.add_row(r["kind"], (r["name"] or "")[:40], r["chain"] or "-", r["status"],
                      f"[{colour}]{ev_val:.0f}[/{colour}]" if ev_val is not None else "-",
                      str(r["cost_usd"] or "-"))
    console.print(table)


# ------------------------------------------------- hunt signals (early-alpha detection)
#
# Declared here rather than beside the other Typer groups at the top of the file so the
# whole subsystem is one contiguous block: this file has two authors at the moment.

signals_app = typer.Typer(
    help="Pre-event detection: certificate transparency, venue announcements, governance, "
         "repo activity, pre-launch markets.",
    no_args_is_help=True,
)
hunt_app.add_typer(signals_app, name="signals")


@signals_app.command("refresh")
def signals_refresh(
    source: str = typer.Option("", "--source", help="Comma-separated sources; default all due."),
    force: bool = typer.Option(False, "--force", help="Ignore each source's poll interval."),
) -> None:
    """Poll every due source once and record what is new."""
    from kaiba.core.db import ensure_db

    try:
        from kaiba.hunters import signals
    except ImportError as exc:
        return _missing("signals hunter", exc)
    conn = ensure_db()
    only = [s.strip() for s in source.split(",") if s.strip()] or None
    if only:
        unknown = [s for s in only if s not in signals.SOURCES]
        if unknown:
            _fail(f"unknown source(s): {', '.join(unknown)}",
                  f"one of: {', '.join(signals.SOURCES)}")
    new = signals.refresh(conn, only=only, force=force)
    console.print(f"[bold]{new}[/bold] new signal(s)")
    signals_health()


@signals_app.command("health")
def signals_health() -> None:
    """Per-source last-success. A quiet detector and a quiet market look identical."""
    from kaiba.core.db import ensure_db

    try:
        from kaiba.hunters import signals
    except ImportError as exc:
        return _missing("signals hunter", exc)
    rows = signals.source_health(ensure_db())
    table = Table("source", "kind", "state", "last OK", "every", "found", "new total", "error")
    colours = {"ok": "green", "degraded": "yellow", "dead": "red", "never_run": "dim"}
    for row in rows:
        colour = colours.get(row["state"], "white")
        since = "never" if row["since_ok_s"] is None else f"{row['since_ok_s'] / 3600:.1f}h ago"
        table.add_row(
            row["source"], row["kind"], f"[{colour}]{row['state'].upper()}[/{colour}]", since,
            f"{row['interval_s']}s", str(row["last_count"]), str(row["total_new"]),
            (row["last_error"] or "")[:44],
        )
    console.print(table)
    broken = [r["source"] for r in rows if r["state"] == "dead"]
    if broken:
        console.print(f"[red]not producing:[/red] {', '.join(broken)} — treat as absent evidence, "
                      "not as evidence of absence")


@signals_app.command("report")
def signals_report() -> None:
    """Weekly report: source health first, then what was detected and how early."""
    from kaiba.core.db import ensure_db

    try:
        from kaiba.hunters import signals
    except ImportError as exc:
        return _missing("signals hunter", exc)
    console.print(signals.weekly_report(ensure_db()))


@signals_app.command("list")
def signals_list(
    kind: str = typer.Option("", "--kind", help="cert_subdomain | venue_listing | ..."),
    source: str = typer.Option("", "--source", help="crtsh | okx | hyperliquid | ..."),
    limit: int = typer.Option(25, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Most recent detections, newest first."""
    from kaiba.core.db import ensure_db

    try:
        from kaiba.hunters import signals
    except ImportError as exc:
        return _missing("signals hunter", exc)
    rows = signals.recent_signals(ensure_db(), limit=limit, kind=kind or None,
                                  source=source or None)
    if json_out:
        return _echo_json(rows)
    if not rows:
        console.print("[dim]nothing detected yet; run `kaiba hunt signals refresh --force`[/dim]")
        return
    table = Table("source", "kind", "subject", "lead", "detect lag", "conf", "title")
    for row in rows:
        lead = row["lead_ms"]
        lag = row["detect_lag_ms"]
        table.add_row(
            row["source"], row["kind"], str(row["subject"])[:30],
            # A missing lead prints as "-" and never as zero: unknowable is not on time.
            f"{lead / 86_400_000:.1f}d" if lead is not None else "-",
            f"{lag / 3_600_000:.1f}h" if lag is not None else "-",
            f"{row['confidence']:.2f}" if row["confidence"] is not None else "-",
            str(row["title"] or "")[:46],
        )
    console.print(table)


# ------------------------------------------------------- validate (Phase 4 harness)
#
# Declared here rather than beside the other Typer groups at the top of the file, for the
# same reason the `hunt signals` block above is: this file has more than one author at the
# moment and a contiguous block is easier to merge than a shared header.
#
# The headline command is `kaiba validate power`, and it is the one worth reading. It does
# not say whether the agent has edge; it says whether we could tell, and how long finding
# out would take. For months the answer will be "underpowered", which is a result and not
# an error.

validate_app = typer.Typer(
    help="Phase 4 validation: statistical power, the wallet-grading control arm, the six "
         "promotion gates, and the falsification runs that prove the harness can say no.",
    no_args_is_help=True,
)
app.add_typer(validate_app, name="validate")

_VERDICT_COLOUR = {
    "pass": "green",
    "fail": "red",
    "underpowered": "yellow",
    "blocked": "dim",
}


def _verdict(value: str) -> str:
    return f"[{_VERDICT_COLOUR.get(value, 'white')}]{value.upper()}[/]"


@validate_app.command("power")
def validate_power(
    lane: str = typer.Option("", "--lane", help="Restrict to one lane; default is the book."),
    mode: str = typer.Option("", "--mode", help="shadow | canary | live; default is all."),
    record: bool = typer.Option(False, "--record", help="Persist the run to validation_runs."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """How many closed trades we have, how many we need, and how long that would take.

    The single most decision-relevant output in the project: if the time to significance
    exceeds a regime permanently, the directional book cannot be validated by waiting.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    conn = ensure_db()
    report = validation.power_report(conn, lane=lane or None, mode=mode or None)
    if record:
        report.record(conn)
        conn.commit()
    if json_out:
        return _echo_json(report.model_dump(mode="json"))

    console.print(f"\n{_verdict(report.verdict.value)}  {report.headline}\n")
    table = Table("measure", "value", box=None)
    table.add_row("closed trades", str(report.closed_trades))
    table.add_row("decisions / of them entries",
                  f"{report.total_decisions} / {report.entry_decisions}")
    table.add_row("observation window", f"{report.window_days:.2f} days")
    table.add_row("hit rate", "-" if report.hit_rate is None else f"{report.hit_rate:.1%}")
    table.add_row("return distribution used", f"{report.profile} — {report.profile_basis}")
    table.add_row("trials (registry N)", str(report.trials))
    table.add_row("deflated threshold", f"z = {report.z_deflated:.3f}")
    table.add_row("trades needed (bare t=1.96)", str(report.trades_needed_naive))
    table.add_row("trades needed (deflated)", str(report.trades_needed_deflated))
    table.add_row("still needed", str(report.trades_still_needed))
    table.add_row(
        "closing rate",
        "-" if report.trades_per_week is None else f"{report.trades_per_week:.2f}/week",
    )
    table.add_row(
        "time to significance",
        "unbounded at the current rate" if report.weeks_to_significance is None
        else f"{report.weeks_to_significance:.0f} weeks",
    )
    if report.years_at_achievable_rate:
        lo, hi = report.years_at_achievable_rate
        table.add_row("at an achievable rate", f"{lo:.1f}-{hi:.1f} years")
    if report.years_with_clustering:
        lo, hi = report.years_with_clustering
        table.add_row("…allowing for clustering", f"[red]{lo:.1f}-{hi:.1f} years[/red]")
    table.add_row("working regime length", f"{report.regime_weeks:.0f} weeks")
    console.print(table)
    console.print()
    for note in report.notes:
        console.print(f"[dim]•[/dim] {note}")
    console.print()


@validate_app.command("freeze")
def validate_freeze(
    chain: str = typer.Option("sol", "--chain"),
    grades: str = typer.Option("A,B", "--grades", help="Grades that define the treated arm."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Freeze a graded cohort and its matched control at now, for forward tracking.

    Freezing is an event with a date. It is a separate command rather than something a
    report does for you, because a cohort frozen by accident has a forward window of zero
    and answers nothing.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    conn = ensure_db()
    wanted = [g.strip() for g in grades.split(",") if g.strip()]
    summary = validation.freeze_cohort(conn, chain=chain, graded_grades=wanted)
    conn.commit()
    if json_out:
        return _echo_json(summary.model_dump(mode="json"))
    console.print(f"\nfrozen [bold]{summary.cohort_id}[/bold] at {summary.frozen_ms}")
    console.print(f"  graded arm:  {summary.graded_n}")
    console.print(f"  control arm: {summary.control_n}")
    console.print(f"\n[bold]matched on[/bold]: {', '.join(summary.matched_on)}")
    console.print("[bold]could not match on[/bold]:")
    for item in summary.unmatched:
        console.print(f"  [dim]•[/dim] {item}")
    for note in summary.notes:
        console.print(f"[yellow]•[/yellow] {note}")
    console.print()


@validate_app.command("control")
def validate_control(
    cohort: str = typer.Option("", "--cohort", help="Existing cohort id; default is the latest."),
    chain: str = typer.Option("sol", "--chain"),
    record: bool = typer.Option(False, "--record"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Track a frozen cohort forward against its matched control.

    The test the literature has never run, and the one the whole wallet-grading pillar
    depends on. It must clear p < 0.01 before grading counts as validated.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    conn = ensure_db()
    cohort_id = cohort or validation.latest_cohort(conn, chain=chain)
    if not cohort_id:
        _fail("no cohort has ever been frozen", "run `kaiba validate freeze` first")
    report = validation.control_arm(conn, cohort_id=cohort_id)
    if record:
        report.record(conn)
    conn.commit()
    if json_out:
        return _echo_json(report.model_dump(mode="json"))

    console.print(f"\n{_verdict(report.verdict.value)}  {report.headline}\n")
    table = Table("measure", "graded", "control", box=None)
    table.add_row("frozen members", str(report.graded_frozen), str(report.control_frozen))
    table.add_row("with forward data", str(report.graded_observed), str(report.control_observed))
    table.add_row("forward realised edge", str(report.graded_edge), str(report.control_edge))
    console.print(table)
    console.print(f"\ndifference {report.difference}   p={report.p_value}   "
                  f"Welch t={report.welch_t}")
    if report.minimum_detectable is not None:
        console.print(f"smallest detectable difference: {report.minimum_detectable:.4f}")
    console.print(f"\n[bold]matched on[/bold]: {', '.join(report.matched_on)}")
    console.print("[bold]could not match on[/bold]:")
    for item in report.unmatched:
        console.print(f"  [dim]•[/dim] {item}")
    for note in report.notes:
        console.print(f"[yellow]•[/yellow] {note}")
    console.print()


@validate_app.command("gates")
def validate_gates(
    lane: str = typer.Option("", "--lane"),
    record: bool = typer.Option(False, "--record"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Run the six promotion gates in order, stopping at the first that does not pass."""
    from kaiba.core.db import ensure_db

    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    conn = ensure_db()
    report = validation.run_gates(conn, lane=lane or None, record=record)
    if record:
        conn.commit()
    if json_out:
        return _echo_json(report.model_dump(mode="json"))

    console.print(f"\n{_verdict(report.verdict.value)}  {report.power.headline}\n")
    for gate in report.gates:
        console.print(f"[bold]gate {gate.ordinal} — {gate.gate}[/bold]  "
                      f"{_verdict(gate.verdict.value)}")
        if gate.criteria:
            table = Table("criterion", "verdict", "value", "threshold", box=None, pad_edge=False)
            for crit in gate.criteria:
                table.add_row(
                    crit.name,
                    _verdict(crit.verdict.value),
                    "-" if crit.value is None else f"{crit.value:.4f}",
                    "-" if crit.threshold is None
                    else f"{crit.comparison} {crit.threshold:g}",
                )
            console.print(table)
        for note in gate.notes:
            console.print(f"  [dim]•[/dim] {note}")
        console.print()
    console.print("[dim]BLOCKED means an earlier gate did not pass, so this one was never "
                  "run. It is not a pass.[/dim]\n")


@validate_app.command("falsify")
def validate_falsify(
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Prove the harness can return a negative, and separately that it can return a positive.

    A validation suite that has never said no is not evidence of anything.
    """
    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    report = validation.falsification_suite(trials=validation.honest_trials(conn))
    if json_out:
        return _echo_json(report.model_dump(mode="json"))
    console.print(f"\n{_verdict(report.verdict.value)}  {report.headline}\n")
    table = Table("arm", "verdict", "means", box=None)
    table.add_row("random-entry placebo", _verdict(report.placebo_random_verdict.value),
                  "must NOT pass")
    table.add_row("selection placebo", _verdict(report.placebo_shuffled_verdict.value),
                  f"{report.placebo_selection_hit_rate:.0%} of random subsets cleared the gate")
    table.add_row("positive control", _verdict(report.positive_control_verdict.value),
                  "must pass")
    console.print(table)
    for note in report.notes:
        console.print(f"[yellow]•[/yellow] {note}")
    console.print()


@validate_app.command("status")
def validate_status(
    limit: int = typer.Option(20, "--limit"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Recorded validation runs, newest first, with expiry applied.

    A pass expires after eight weeks. An expired pass is not a pass: the venue changed
    underneath it, and reading a stale verdict as current is how a dead edge keeps funding.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.learning import validation
    except ImportError as exc:
        return _missing("validation harness", exc)
    rows = validation.latest_runs(ensure_db(), limit=limit)
    if json_out:
        return _echo_json(rows)
    if not rows:
        console.print("[dim]no validation runs recorded; try `kaiba validate power "
                      "--record`[/dim]")
        return
    table = Table("kind", "lane", "verdict", "n", "expires", "summary")
    for row in rows:
        # Only a pass can expire. `expired` is True for a run that was never a pass, which
        # is right for "is this a live verdict" and wrong to print as EXPIRED: nothing
        # lapsed, there was never anything there.
        if row["expires_ms"] is None:
            expiry = "[dim]never passed[/dim]"
        elif row["expired"]:
            expiry = "[red]EXPIRED[/red]"
        else:
            expiry = "live"
        table.add_row(row["kind"], row["lane"] or "book", _verdict(str(row["verdict"])),
                      str(row["sample_n"]), expiry, str(row["summary"])[:70])
    console.print(table)


# ------------------------------------------------------- ops (Phase 7 — Operate)
#
# Declared here rather than beside the other Typer groups at the top of the file, for the
# same reason the `hunt signals` and `validate` blocks are: several authors, one contiguous
# block. Two commands only. `ops run` is what kaiba-ops.service and deploy/run-local.py
# start; `ops status` is how you tell whether maintenance is actually happening.

ops_app = typer.Typer(
    help="Deterministic maintenance scheduler: backfills, grades, signal polls, budget checks. "
         "Every interval and budget lives in config/schedule.yaml.",
    no_args_is_help=True,
)
app.add_typer(ops_app, name="ops")

_OPS_STATE_COLOUR = {
    "ok": "green", "running": "cyan", "skipped": "yellow", "stale": "yellow", "late": "red",
    "failing": "red", "backoff": "red", "missing": "magenta", "never_ran": "dim", "disabled": "dim",
}


def _ops_ago(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172_800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


@ops_app.command("run")
def ops_run(
    once: bool = typer.Option(False, "--once", help="Run what is due, wait for it, print outcomes, exit."),
    only: str = typer.Option("", "--only", help="Comma-separated job subset."),
    force: bool = typer.Option(
        False, "--force",
        help="With --once: treat the selected jobs as due now. Does not run a disabled job.",
    ),
    config: str = typer.Option(
        "", "--config", help="Alternate schedule.yaml (default config/schedule.yaml or $KAIBA_SCHEDULE_CONFIG)."
    ),
    json_out: bool = typer.Option(False, "--json", help="With --once: print outcomes as JSON."),
) -> None:
    """Run the maintenance scheduler used by ``kaiba-ops.service``.

    Deterministic, budget-aware and idempotent on restart: it reads its own state table to
    decide what is due, never re-runs everything on boot, and skips any Helius-spending job
    while the month's remaining credits are under the floor in config/schedule.yaml.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.ops import scheduler
    except ImportError as exc:
        return _missing("ops scheduler", exc)
    path = Path(config) if config else scheduler.config_path()
    try:
        cfg = scheduler.load_config(path)
    except scheduler.ScheduleConfigError as exc:
        return _fail(str(exc), "fix the schedule file; the scheduler does not guess intervals")
    selected = [s.strip() for s in only.split(",") if s.strip()] or None
    if selected:
        unknown = [s for s in selected if s not in cfg.jobs]
        if unknown:
            _fail(f"unknown job(s): {', '.join(unknown)}", f"one of: {', '.join(cfg.jobs)}")
    ensure_db()
    sched = scheduler.Scheduler(cfg, config_path=path)
    if once:
        outcomes = sched.run_once(only=selected, force=force)
        if json_out:
            return _echo_json([o.as_dict() for o in outcomes])
        if not outcomes:
            # "--force ran nothing" is confusing unless we say why, and the usual why is
            # that the job is switched off rather than not yet due.
            chosen = selected or list(cfg.jobs)
            off = [n for n in chosen if not cfg.jobs[n].enabled]
            if off:
                console.print(f"[yellow]disabled in {path.name}:[/yellow] {', '.join(off)} — "
                              "--force overrides the clock, not `enabled: false`")
            if len(off) < len(chosen):
                console.print("[dim]nothing due; add --force to run the selected jobs anyway[/dim]")
            return None
        table = Table("job", "status", "took", "credits", "result / error")
        for o in outcomes:
            colour = {"ok": "green", "skipped": "yellow"}.get(o.status, "red")
            detail = o.error if o.error else json.dumps(o.result, default=str)
            table.add_row(o.job, f"[{colour}]{o.status}[/{colour}]", f"{o.duration_ms / 1000:.1f}s",
                          str(o.credits), detail[:80])
        console.print(table)
        return None
    console.print(
        f"[dim]ops scheduler: {len(cfg.jobs)} job(s), tick {cfg.tick_s:g}s, config {path}; ctrl-c to stop[/dim]"
    )
    # Same setup as kaiba.ingest.runner.main. Without it only WARNING and above reach
    # stderr (via logging's lastResort handler), so `journalctl -u kaiba-ops` would show
    # nothing for a scheduler that is working perfectly — which is precisely the "is
    # maintenance happening?" question this service exists to answer. The durable record
    # is ops_runs either way; this is so the operator can see it without SQL.
    import logging

    from kaiba.core.config import get_settings

    logging.basicConfig(
        level=get_settings().kaiba_log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    code = sched.run_forever(only=selected)
    if code:
        _fail("scheduler did not start", "another `kaiba ops run` holds the service lock; see `kaiba ops status`")
    return None


@ops_app.command("status")
def ops_status(
    json_out: bool = typer.Option(False, "--json"),
    runs: int = typer.Option(0, "--runs", help="Also list the N most recent runs."),
    job: str = typer.Option("", "--job", help="With --runs: only this job."),
) -> None:
    """Is maintenance actually happening? One row per job with a derived state.

    LATE means the scheduler is not picking the job up (the service is probably down);
    STALE means it runs but has not succeeded lately; MISSING means the entry point the job
    would call does not exist yet.
    """
    from kaiba.core.db import ensure_db

    try:
        from kaiba.ops import scheduler
    except ImportError as exc:
        return _missing("ops scheduler", exc)
    conn = ensure_db()
    try:
        cfg = scheduler.load_config()
    except scheduler.ScheduleConfigError:
        cfg = scheduler.ScheduleConfig()
    rows = scheduler.status(conn, cfg)
    holder = scheduler.service_holder(stale_after_s=cfg.service_lock_stale_s)
    recent = scheduler.recent_runs(conn, limit=runs, job=job or None) if runs > 0 else []
    if json_out:
        return _echo_json({"service": holder, "jobs": rows, "runs": recent})
    if holder:
        # The heartbeat age is shown because a hard-killed holder still looks alive until
        # it ages out, and on Windows the pid cannot be probed to tell the difference.
        age = holder.get("heartbeat_age_s")
        beat = "" if age is None else f", last heartbeat {_ops_ago(int(age))} ago"
        console.print(f"[green]scheduler running[/green] pid {holder.get('pid')} on "
                      f"{holder.get('host')}{beat}")
    else:
        console.print("[red]no live scheduler holds the service lock[/red] — nothing runs until "
                      "`kaiba ops run` starts")
    if not rows:
        console.print("[dim]no jobs recorded yet; run `kaiba ops run`[/dim]")
        return None
    table = Table("job", "state", "last", "ok ago", "next in", "took", "fails", "credits", "detail")
    for r in rows:
        colour = _OPS_STATE_COLOUR.get(r["state"], "white")
        next_in = r["next_in_s"]
        took = r["last_duration_ms"]
        # last_error is kept after a recovery on purpose (it is what went wrong last
        # time), so show it only when the most recent run is actually the failing one —
        # otherwise a green job reads as broken forever after one bad night.
        failed_now = r["last_status"] in {"error", "timeout", "skipped"}
        detail = (r["last_error"] if failed_now else "") or json.dumps(r["last_result"], default=str)
        table.add_row(
            r["name"], f"[{colour}]{r['state'].upper()}[/{colour}]", str(r["last_status"] or "-"),
            _ops_ago(r["since_success_s"]),
            "-" if next_in is None else ("due" if next_in <= 0 else _ops_ago(next_in)),
            "-" if took is None else f"{took / 1000:.1f}s",
            f"{r['consecutive_failures']}/{r['failures']}", str(r["credits_total"]), detail[:60],
        )
    console.print(table)
    if recent:
        hist = Table("job", "started", "status", "took", "credits", "result / error", title="recent runs")
        for r in recent:
            started = r["started_ms"] % 86_400_000 // 1000
            hh, rem = divmod(started, 3600)
            mm, ss = divmod(rem, 60)
            colour = {"ok": "green", "skipped": "yellow", "running": "cyan"}.get(r["status"], "red")
            hist.add_row(
                r["job"], f"{hh:02d}:{mm:02d}:{ss:02d}Z", f"[{colour}]{r['status']}[/{colour}]",
                "-" if r["duration_ms"] is None else f"{r['duration_ms'] / 1000:.1f}s", str(r["credits"]),
                (r["error"] or json.dumps(r["result"], default=str))[:70],
            )
        console.print(hist)
    return None


# ------------------------------------------- discovery, tracking, tape, paper, curve,
#                                              viability  (wired 2026-09-20)
#
# Six modules landed on 2026-09-20, each tested and each unreachable: their authors could
# not wire them because this file belonged to someone else at the time. Nothing below
# changes a module. Every command is a call into an entry point that already existed, a
# readable rendering of what it returned, and — where the call spends metered credits or
# writes — a price quoted up front and a `--dry-run` that does neither.
#
# Declared here rather than beside the Typer groups at the top of the file for the same
# reason the `hunt signals`, `validate` and `ops` blocks are: several authors, one
# contiguous block is easier to merge than a shared header.

discover_app = typer.Typer(
    help="Find wallets worth watching from flow we observed ourselves, with the deflation "
         "that says how much of it is luck.",
    no_args_is_help=True,
)
track_app = typer.Typer(
    help="Live wallet tracking: the watchlist, the two detection routes, the confluence window.",
    no_args_is_help=True,
)
tape_app = typer.Typer(help="Trade-tape coverage: which mints we hold every trade for.", no_args_is_help=True)
paper_app = typer.Typer(help="Paper broker: shadow fills that are allowed to fail.", no_args_is_help=True)
curve_app = typer.Typer(help="Bonding-curve pricing instruments.", no_args_is_help=True)
viability_app = typer.Typer(help="Position sizing economics.", no_args_is_help=True)
app.add_typer(discover_app, name="discover")
app.add_typer(track_app, name="track")
app.add_typer(tape_app, name="tape")
app.add_typer(paper_app, name="paper")
app.add_typer(curve_app, name="curve")
app.add_typer(viability_app, name="viability")

#: ``discover.DEFAULT_ALPHA`` and ``discover.MIN_INTERESTING_EDGE`` as of wiring. Typer
#: needs literals; ``tests/test_cli.py`` pins these to the module so they cannot drift.
DISCOVER_DEFAULT_ALPHA = 0.05
DISCOVER_DEFAULT_MIN_EDGE = 0.10
#: ``tracker.HELIUS_POLL_CREDITS``: what one paid per-wallet call costs. Also pinned.
TRACKER_POLL_CREDITS = 10
#: ``tracker`` seed/list/scan limits, matching the module's own keyword defaults.
TRACKER_SEED_LIMIT = 50
TRACKER_SCAN_LIMIT = 200


# ---------------------------------------------------------------- discover


def _discovery_module():  # noqa: ANN202 - typer must not import the module at definition time
    try:
        from kaiba.intelligence import discover
    except ImportError as exc:
        _missing("wallet discovery (kaiba.intelligence.discover)", exc)
        return None
    return discover


def _discovery_counts(report: Any) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "chain": report.chain,
        "as_of_ms": report.as_of_ms,
        "screened": len(report.screened),
        "entities": report.chance.screened_entities,
        "rejected": len({r.address for r in report.rejected}),
        "unassessed": len(report.unassessed),
        "candidates": len(report.candidates),
        "gate_cleared": len(report.gate_cleared),
        "reject_counts": report.reject_counts,
        "unassessed_counts": report.unassessed_counts,
    }


def _replay_run(discover: Any, conn: Any, run_id: str, *, alpha: float, min_edge: float) -> Any:
    """Re-derive a stored discovery run so it can be frozen or proposed.

    ``freeze_cohort`` and ``propose_research_cohort`` both take a live ``DiscoveryReport``
    and the module ships no reader that rebuilds one from ``discovery_runs``. Rather than
    invent a half-report from the stored columns, this re-screens at the run's own
    ``as_of_ms`` — the screen is a pure function of the flow up to that instant — and
    refuses unless the recomputed ``run_id`` is identical. A mismatch means the evidence
    under the run moved (a backfill landed older rows), and freezing *that* would attach a
    cohort to a run it did not come from.
    """
    from kaiba.core.db import fetch_one

    row = fetch_one(conn, "SELECT * FROM discovery_runs WHERE run_id=?", (run_id,))
    if row is None:
        _fail(f"no discovery run {run_id!r}", "list them with `kaiba discover run --dry-run` or SQL")
    report = discover.discover(
        conn, chain=row["chain"], as_of_ms=int(row["as_of_ms"]), alpha=alpha, min_edge=min_edge
    )
    if report.run_id != run_id:
        _fail(
            f"run {run_id} could not be reproduced at as_of_ms={row['as_of_ms']} "
            f"(recomputed {report.run_id})",
            "the flow under that run changed; re-run `kaiba discover run` and use the new id",
        )
    return report


@discover_app.command("run")
def discover_run(
    chain: str = typer.Option("sol", "--chain"),
    as_of: int = typer.Option(0, "--as-of", help="Screen as of this epoch-ms (0 = now)."),
    alpha: float = typer.Option(DISCOVER_DEFAULT_ALPHA, "--alpha", help="Family-wise error rate."),
    min_edge: float = typer.Option(
        DISCOVER_DEFAULT_MIN_EDGE, "--min-edge", help="Smallest edge worth powering for."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Screen without writing the run."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Screen our own flow for wallets worth watching, and say how much of it is chance.

    Free and local: no provider is called. Reading the candidate list without the chance
    sentence underneath it is a misuse of the output, so both are always printed.
    """
    discover = _discovery_module()
    if discover is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    report = discover.discover(
        conn, chain=ch, as_of_ms=as_of or None, alpha=alpha, min_edge=min_edge
    )
    stored = False
    if not dry_run:
        discover.store_report(report, conn)
        conn.commit()
        stored = True
    if json_out:
        # Deliberately not `report.model_dump()`. On the live database that is 34 MB —
        # 48,354 screened addresses and 42,740 unassessed records — which no pipe wants
        # and no terminal survives. What is dropped is said here rather than silently,
        # and every dropped row is in the tables `store_report` wrote.
        return _echo_json({
            "stored": stored,
            **_discovery_counts(report),
            "headline": report.headline,
            "chance": report.chance.model_dump(mode="json"),
            "notes": report.notes,
            "candidates": [
                {
                    "address": c.address, "entity_key": c.entity_key, "status": c.status.value,
                    "closed_trades": c.features.closed_trades, "wins": c.features.wins,
                    "win_rate": c.features.win_rate,
                    "distinct_tokens": c.features.distinct_tokens,
                    "p_under_null": c.p_under_null, "expected_peers": c.expected_peers,
                    "blockers": c.blockers,
                }
                for c in report.candidates
            ],
            "omitted": {
                "screened_addresses": len(report.screened),
                "rejections": len(report.rejected),
                "unassessed": len(report.unassessed),
                "candidate_features": len(report.candidates),
                "where": "discovery_runs, discovery_candidates and discovery_rejects",
            },
        })
    console.print(f"\n[bold]{report.run_id}[/bold]  {report.headline}\n")
    console.print(f"[yellow]{report.chance.sentence}[/yellow]\n")
    counts = _discovery_counts(report)
    table = Table("measure", "value", box=None)
    for key in ("screened", "entities", "rejected", "unassessed", "candidates", "gate_cleared"):
        table.add_row(key, str(counts[key]))
    console.print(table)
    if counts["reject_counts"]:
        rejects = Table("reject reason", "addresses", title="rejected on structure")
        for reason, n in sorted(counts["reject_counts"].items(), key=lambda kv: -kv[1]):
            rejects.add_row(reason, str(n))
        console.print(rejects)
    for note in report.notes:
        console.print(f"[dim]•[/dim] {note}")
    console.print(
        f"\n[dim]{'not stored (--dry-run)' if dry_run else 'stored to discovery_runs'}[/dim]\n"
    )
    return None


@discover_app.command("freeze")
def discover_freeze(
    run: str = typer.Option("", "--run", help="Stored run id; omit to screen fresh at now."),
    chain: str = typer.Option("sol", "--chain", help="Only used when --run is omitted."),
    alpha: float = typer.Option(DISCOVER_DEFAULT_ALPHA, "--alpha"),
    min_edge: float = typer.Option(DISCOVER_DEFAULT_MIN_EDGE, "--min-edge"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the arms without freezing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Freeze a discovered cohort and its matched control at the run's own instant.

    Freezing is an event with a date: a cohort frozen by accident has a forward window of
    zero and answers nothing, which is why this is a separate command and not something
    ``discover run`` does for you.
    """
    discover = _discovery_module()
    if discover is None:
        return None
    from kaiba.core.db import ensure_db, fetch_one

    conn = ensure_db()
    if run:
        report = _replay_run(discover, conn, run, alpha=alpha, min_edge=min_edge)
    else:
        report = discover.discover(
            conn, chain=_chain_or_fail(chain), alpha=alpha, min_edge=min_edge
        )
    if dry_run:
        payload = {"dry_run": True, "would_freeze": len(report.candidates), **_discovery_counts(report)}
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] {len(report.candidates)} candidate(s) from "
            f"{report.run_id} would be offered to the matcher; nothing written"
        )
        return None

    cohort_id = discover.freeze_cohort(report, conn)
    conn.commit()
    frozen = fetch_one(
        conn,
        "SELECT cohort_id, frozen_ms, chain, graded_n, control_n, notes_json "
        "FROM wallet_cohort_freezes WHERE cohort_id=?",
        (cohort_id,),
    ) or {}
    graded_n = int(frozen.get("graded_n") or 0)
    control_n = int(frozen.get("control_n") or 0)
    if json_out:
        return _echo_json({
            "cohort_id": cohort_id,
            "run_id": report.run_id,
            "frozen_ms": frozen.get("frozen_ms"),
            "graded_n": graded_n,
            "control_n": control_n,
            "candidates": len(report.candidates),
        })
    console.print(f"\nfrozen [bold]{cohort_id}[/bold] at {frozen.get('frozen_ms')} from {report.run_id}")
    console.print(f"  graded arm:  {graded_n}")
    console.print(f"  control arm: {control_n}")
    unmatched = len(report.candidates) - graded_n
    if unmatched > 0:
        console.print(
            f"[yellow]{unmatched} candidate(s) had no control inside the calliper and were "
            "dropped[/yellow]"
        )
    console.print()
    return None


@discover_app.command("forward")
def discover_forward(
    cohort: str = typer.Option(..., "--cohort", help="A frozen discovery cohort id."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Measure a frozen cohort forward against its matched control. Read-only.

    Expect ``underpowered`` for a long time; that is a result, not an error.
    """
    discover = _discovery_module()
    if discover is None:
        return None
    from kaiba.core.db import ensure_db

    report = discover.forward_report(cohort, ensure_db())
    if json_out:
        return _echo_json(report.model_dump(mode="json"))
    console.print(f"\n{_verdict(report.verdict.value)}  {report.headline}\n")
    table = Table("measure", "graded", "control", box=None)
    table.add_row("frozen members", str(report.graded_frozen), str(report.control_frozen))
    table.add_row("with forward data", str(report.graded_observed), str(report.control_observed))
    table.add_row("forward realised edge", str(report.graded_edge), str(report.control_edge))
    console.print(table)
    console.print(f"\ndifference {report.difference}   p={report.p_value}   Welch t={report.welch_t}")
    if report.minimum_detectable is not None:
        console.print(f"smallest detectable difference: {report.minimum_detectable:.4f}")
    for note in report.notes:
        console.print(f"[yellow]•[/yellow] {note}")
    console.print()
    return None


@discover_app.command("propose")
def discover_propose(
    run: str = typer.Option(..., "--run", help="The stored run whose candidates to propose."),
    alpha: float = typer.Option(DISCOVER_DEFAULT_ALPHA, "--alpha"),
    min_edge: float = typer.Option(DISCOVER_DEFAULT_MIN_EDGE, "--min-edge"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count without writing cohorts."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Mark a run's candidates ``research`` in ``wallets``. The only cohort discovery may write.

    Deliberately not folded into ``discover run``: writing a cohort is a claim about which
    wallets we intend to watch, and it should be a thing somebody typed.
    """
    discover = _discovery_module()
    if discover is None:
        return None
    from kaiba.core.db import ensure_db

    conn = ensure_db()
    report = _replay_run(discover, conn, run, alpha=alpha, min_edge=min_edge)
    if dry_run:
        payload = {
            "dry_run": True,
            "run_id": report.run_id,
            "would_write": len(report.candidates),
            "protected_cohorts": sorted(discover.PROTECTED_COHORTS),
        }
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] {len(report.candidates)} candidate(s) would be marked "
            f"'{discover.PROPOSED_COHORT}'; rows already in "
            f"{sorted(discover.PROTECTED_COHORTS)} would be left alone"
        )
        return None

    written, skipped = discover.propose_research_cohort(report, conn)
    conn.commit()
    payload = {
        "run_id": report.run_id,
        "cohort": discover.PROPOSED_COHORT,
        "written": written,
        "skipped": len(skipped),
        "skipped_addresses": skipped[:20],
    }
    if json_out:
        return _echo_json(payload)
    console.print(
        f"[bold]{written}[/bold] wallet(s) marked '{discover.PROPOSED_COHORT}'; "
        f"[yellow]{len(skipped)}[/yellow] left alone (already trusted_copy or blacklist)"
    )
    for address in skipped[:20]:
        console.print(f"  [dim]skipped[/dim] {address}")
    return None


# ---------------------------------------------------------------- track


def _tracker_module():  # noqa: ANN202 - typer must not import the module at definition time
    try:
        from kaiba.intelligence import tracker
    except ImportError as exc:
        _missing("wallet tracker (kaiba.intelligence.tracker)", exc)
        return None
    return tracker


def _entry_view(entry: Any) -> dict[str, Any]:
    return {
        "chain": entry.chain.value,
        "address": entry.address,
        "tier": entry.tier.value,
        "status": entry.status,
        "reason": entry.reason,
        "source": entry.source,
        "added_by": entry.added_by,
        "added_ms": entry.added_ms,
        "removed_ms": entry.removed_ms,
        "removed_reason": entry.removed_reason,
        "grade_at_add": entry.grade_at_add.value if entry.grade_at_add else None,
        "score_at_add": entry.score_at_add,
        "last_checked_ms": entry.last_checked_ms,
    }


def _poll_view(results: list[Any]) -> dict[str, Any]:
    detections = [d for r in results for d in r.detections]
    return {
        "polls": len(results),
        "ok": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "credits": sum(r.credits for r in results),
        "rows_seen": sum(r.rows_seen for r in results),
        "detections": len(detections),
        "median_rtt_ms": sorted(r.rtt_ms for r in results)[len(results) // 2] if results else None,
        "notes": sorted({r.note for r in results if r.note})[:10],
    }


@track_app.command("seed")
def track_seed(
    source: str = typer.Option(..., "--source", help="grades | discovery"),
    limit: int = typer.Option(TRACKER_SEED_LIMIT, "--limit", help="Wallets to offer to the screen."),
    chain: str = typer.Option("sol", "--chain"),
    run: str = typer.Option("", "--run", help="With --source discovery: a specific run id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Quote the cost; admit nobody."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Offer graded or discovered wallets to the admission screen.

    Every offer costs one Helius signatures call (10 credits) because the screen measures
    the transaction failure rate on chain rather than trusting a label.
    """
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    kind = source.strip().lower()
    if kind not in {"grades", "discovery"}:
        return _fail(f"unknown source {source!r}", "one of: grades, discovery")
    ch = _chain_or_fail(chain)
    conn = ensure_db()
    worst_case = max(0, limit) * TRACKER_POLL_CREDITS
    _helius_notice(worst_case, f"screening up to {limit} wallet(s) from {kind}", conn)
    if dry_run:
        payload = {"dry_run": True, "source": kind, "limit": limit,
                   "would_spend_helius_credits": worst_case}
        if json_out:
            return _echo_json(payload)
        console.print("[yellow]dry run:[/yellow] nothing screened, nothing admitted")
        return None

    if kind == "grades":
        admitted, screens = tracker.seed_from_grades(ch, conn, limit=limit)
    else:
        admitted, screens = tracker.seed_from_discovery(ch, conn, run_id=run or None, limit=limit)
    conn.commit()
    refused = [s for s in screens if not s.admissible]
    payload = {
        "source": kind,
        "offered": len(screens),
        "admitted": len(admitted),
        "refused": len(refused),
        "credits_spent": len(screens) * TRACKER_POLL_CREDITS,
        "entries": [_entry_view(e) for e in admitted],
        "blockers": sorted({b for s in refused for b in s.blockers})[:20],
    }
    if json_out:
        return _echo_json(payload)
    console.print(
        f"[bold]{len(admitted)}[/bold] admitted of {len(screens)} offered "
        f"({len(refused)} refused); ~{payload['credits_spent']} credits"
    )
    if admitted:
        table = Table("address", "tier", "grade", "score", "reason")
        for entry in admitted:
            table.add_row(
                entry.address[:44], entry.tier.value,
                entry.grade_at_add.value if entry.grade_at_add else "-",
                "-" if entry.score_at_add is None else f"{entry.score_at_add:.1f}",
                entry.reason[:40],
            )
        console.print(table)
    if payload["blockers"]:
        console.print(f"[yellow]refusals:[/yellow] {', '.join(payload['blockers'])}")
    return None


@track_app.command("list")
def track_list(
    chain: str = typer.Option("sol", "--chain"),
    status_filter: str = typer.Option("active", "--status", help="active | removed | all"),
    tier: str = typer.Option("", "--tier", help="observe | candidate"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """The watchlist. Read-only."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    want_tier = None
    if tier:
        try:
            want_tier = tracker.Tier(tier)
        except ValueError:
            return _fail(f"unknown tier {tier!r}", "one of: " + ", ".join(t.value for t in tracker.Tier))
    entries = tracker.watchlist(
        ch, ensure_db(), status=None if status_filter == "all" else status_filter, tier=want_tier
    )
    if json_out:
        return _echo_json([_entry_view(e) for e in entries])
    if not entries:
        console.print("[dim]watchlist empty; `kaiba track seed --source grades`[/dim]")
        return None
    table = Table("address", "tier", "status", "grade", "source", "added", "last checked")
    for entry in entries:
        table.add_row(
            entry.address[:44], entry.tier.value, entry.status,
            entry.grade_at_add.value if entry.grade_at_add else "-",
            entry.source[:18], str(entry.added_ms),
            str(entry.last_checked_ms or "-"),
        )
    console.print(table)
    return None


@track_app.command("audit")
def track_audit(
    address: str = typer.Argument(..., help="The watched address."),
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Everything ever decided about one address, oldest first. Read-only."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    rows = tracker.audit_trail(_chain_or_fail(chain), address, ensure_db())
    if json_out:
        return _echo_json(rows)
    if not rows:
        console.print(f"[dim]nothing recorded for {address}[/dim]")
        return None
    table = Table("when", "action", "actor", "detail", title=f"audit · {address[:20]}")
    for row in rows:
        table.add_row(str(row.get("ts_ms")), str(row.get("action")), str(row.get("actor")),
                      str(row.get("detail") or "")[:70])
    console.print(table)
    return None


@track_app.command("sweep")
def track_sweep(
    mints: int = typer.Option(
        0, "--mints", help="Hottest mints to poll this pass (0 = the module's own sweep size)."
    ),
    chain: str = typer.Option("sol", "--chain"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List the mints; poll none, write none."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Route T: one pass of the free per-token tape over the hottest mints.

    Free — pump.fun, keyless — but it writes every trade it sees into ``swaps``, so it has
    a dry run. MEASURED 2.1 s per mint and 87.5% availability, which is the coverage
    ceiling of this route, not a bug in it.
    """
    tracker = _tracker_module()
    if tracker is None:
        return None
    from dataclasses import replace

    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    config = tracker.DEFAULT_CONFIG
    if mints > 0:
        config = replace(config, token_sweep_size=mints)
    if dry_run:
        targets, receipt = tracker.hot_mints(config.token_sweep_size, conn)
        payload = {"dry_run": True, "mints": targets, "basis": receipt.basis.value,
                   "note": receipt.note}
        if json_out:
            return _echo_json(payload)
        console.print(f"[yellow]dry run:[/yellow] would poll {len(targets)} mint(s), write nothing")
        for mint in targets:
            console.print(f"  {mint}")
        return None

    results = tracker.sweep_tokens(ch, conn, config=config)
    conn.commit()
    payload = {"route": tracker.ROUTE_PUMPFUN, **_poll_view(results)}
    if json_out:
        return _echo_json(payload)
    console.print(
        f"[bold]sweep[/bold] {payload['ok']}/{payload['polls']} mint(s) answered, "
        f"{payload['rows_seen']} trade(s) seen, {payload['detections']} by watched wallets, "
        f"{payload['credits']} credits"
    )
    if payload["notes"]:
        console.print(f"[yellow]notes:[/yellow] {', '.join(payload['notes'])}")
    return None


@track_app.command("poll")
def track_poll(
    chain: str = typer.Option("sol", "--chain"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Quote the cost; call nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Route W: one paid Helius page per active watched wallet. 10 credits each."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    watched = sorted(tracker.watched_addresses(ch, conn))
    cost = len(watched) * TRACKER_POLL_CREDITS
    _helius_notice(cost, f"polling {len(watched)} watched wallet(s)", conn)
    if dry_run:
        payload = {"dry_run": True, "wallets": len(watched),
                   "would_spend_helius_credits": cost, "addresses": watched[:25]}
        if json_out:
            return _echo_json(payload)
        console.print("[yellow]dry run:[/yellow] nothing polled")
        for address in watched[:25]:
            console.print(f"  {address}")
        return None
    if not watched:
        console.print("[dim]no active watched wallets; `kaiba track seed --source grades`[/dim]")
        return None

    results = tracker.poll_wallets(ch, conn)
    conn.commit()
    payload = {"route": tracker.ROUTE_HELIUS, **_poll_view(results)}
    if json_out:
        return _echo_json(payload)
    console.print(
        f"[bold]poll[/bold] {payload['ok']}/{payload['polls']} wallet(s) answered, "
        f"{payload['detections']} detection(s), {payload['credits']} credits, "
        f"median rtt {payload['median_rtt_ms']}ms"
    )
    if payload["notes"]:
        console.print(f"[yellow]notes:[/yellow] {', '.join(payload['notes'])}")
    return None


@track_app.command("scan")
def track_scan(
    since_s: int = typer.Option(
        0, "--since-s", help="Lookback in seconds (0 = the module's own scan_lookback_s)."
    ),
    limit: int = typer.Option(TRACKER_SCAN_LIMIT, "--limit", help="Tokens to evaluate."),
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Count entities, not addresses, in every confluence window on recent tape.

    Free and offline. It records the windows it evaluated in ``tracker_windows``;
    ``scan_recent_tokens`` exposes no way to suppress that write, so there is no
    ``--dry-run`` here rather than one that lies about what it did.
    """
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import now_ms

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    since_ms = (now_ms() - since_s * 1000) if since_s > 0 else None
    report = tracker.scan_recent_tokens(ch, conn, since_ms=since_ms, limit=limit)
    conn.commit()
    if json_out:
        return _echo_json(report)
    console.print(
        f"[bold]scan[/bold] {report['tokens_scanned']} token(s), "
        f"{report['windows_evaluated']} window(s) of {report['window_s']}s"
    )
    table = Table("measure", "value", box=None)
    for key, value in report.items():
        if key in {"tokens_scanned", "windows_evaluated", "window_s"}:
            continue
        table.add_row(key, json.dumps(value, default=str) if isinstance(value, dict | list) else str(value))
    console.print(table)
    return None


@track_app.command("status")
def track_status(
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """What is watched, what has been seen, what it cost, and whether a lane can fire."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    data = tracker.status(_chain_or_fail(chain), ensure_db())
    if json_out:
        return _echo_json(data)
    console.print(
        f"[bold]tracker[/bold] {data['watchlist_active']} watched "
        f"{data['watchlist_by_tier']}  detections={data['detections']} "
        f"(buys {data['buys_detected']})"
    )
    table = Table("measure", "value", box=None)
    for key in ("windows_evaluated", "max_entities_in_any_window", "qualifying_windows",
                "entity_table_populated"):
        table.add_row(key, str(data[key]))
    console.print(table)
    _echo_json({"latency": data["latency"], "cost": data["cost"], "webhook": data["webhook"]})
    console.print(f"[dim]{data['never_promotes']}[/dim]")
    return None


@track_app.command("rescreen")
def track_rescreen(
    chain: str = typer.Option("sol", "--chain"),
    actor: str = typer.Option("operator", "--actor", help="Recorded on every audit row."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Quote the cost; screen nobody."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Re-run admission on the active watchlist and evict what now fails. 10 credits each."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    watched = sorted(tracker.watched_addresses(ch, conn))
    cost = len(watched) * TRACKER_POLL_CREDITS
    _helius_notice(cost, f"re-screening {len(watched)} watched wallet(s)", conn)
    if dry_run:
        payload = {"dry_run": True, "wallets": len(watched), "would_spend_helius_credits": cost}
        if json_out:
            return _echo_json(payload)
        console.print("[yellow]dry run:[/yellow] nothing screened, nobody evicted")
        return None

    result = tracker.rescreen(ch, conn, actor=actor)
    conn.commit()
    if json_out:
        return _echo_json(result)
    console.print(
        f"[bold]rescreen[/bold] {result['screened']} screened, {len(result['kept'])} kept, "
        f"{len(result['evicted'])} evicted, {result['credits']} credits"
    )
    for item in result["evicted"]:
        console.print(f"  [red]evicted[/red] {item['address'][:44]} — {item['why'][:60]}")
    return None


@track_app.command("remove")
def track_remove(
    address: str = typer.Argument(..., help="The address to take off the watchlist."),
    reason: str = typer.Option(..., "--reason", help="Why; recorded on the audit trail."),
    chain: str = typer.Option("sol", "--chain"),
    actor: str = typer.Option("operator", "--actor"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Say what would change; change nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Take a wallet off the watchlist. The row stays; the status and the reason change."""
    tracker = _tracker_module()
    if tracker is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    entry = tracker.get_entry(ch, address, conn)
    if dry_run:
        payload = {"dry_run": True, "address": address,
                   "currently": _entry_view(entry) if entry else None}
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] {address} is "
            + (f"{entry.status}/{entry.tier.value}" if entry else "not on the watchlist")
        )
        return None

    removed = tracker.remove(ch, address, reason=reason, actor=actor, conn=conn)
    conn.commit()
    if json_out:
        return _echo_json({"address": address, "removed": removed, "reason": reason})
    if removed:
        console.print(f"[green]removed[/green] {address}: {reason}")
    else:
        console.print(f"[yellow]not active[/yellow] {address} was not on the watchlist")
    return None


# ---------------------------------------------------------------- tape


def _tape_module():  # noqa: ANN202 - typer must not import the module at definition time
    try:
        from kaiba.ingest import tape
    except ImportError as exc:
        _missing("trade-tape coverage (kaiba.ingest.tape)", exc)
        return None
    return tape


@tape_app.command("status")
def tape_status(
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """How many mints we hold a complete tape for, and how many we do not. Read-only."""
    tape = _tape_module()
    if tape is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    summary = tape.coverage_summary(ch, ensure_db())
    if json_out:
        return _echo_json(summary)
    known = int(summary["tokens_known"]) or 1
    table = Table("coverage", "tokens", "share", title=f"trade-tape coverage · {ch.value}")
    for key in ("complete", "partial", "unavailable", "unassessed"):
        n = int(summary[key])
        table.add_row(key, str(n), f"{n / known:.1%}")
    console.print(table)
    console.print(
        f"[bold]{summary['tokens_known']}[/bold] known mint(s); swaps "
        f"{summary['swaps_per_token_route']} from the per-token route, "
        f"{summary['swaps_wallet_walk']} deposited by the wallet walk"
    )
    console.print(
        "[dim]only 'complete' means every trade back to launch; a wallet-walk subset of a "
        "mint is not a tape[/dim]"
    )
    return None


@tape_app.command("collect")
def tape_collect(
    chain: str = typer.Option("sol", "--chain"),
    limit: int = typer.Option(0, "--limit", help="Mints this pass (0 = the module's own cap)."),
    budget_s: float = typer.Option(0.0, "--budget-s", help="Wall-clock budget (0 = module default)."),
    token: list[str] = typer.Option([], "--token", help="Collect these mints instead of the queue."),
    no_seed: bool = typer.Option(False, "--no-seed", help="Skip adopting coverage from snapshots."),
    no_repair: bool = typer.Option(False, "--no-repair", help="Skip the create-flag/fee-payer repair."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List the queue; collect nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """One coverage pass: pull the tapes we are missing and record what that establishes.

    Keyless (pump.fun, one request a second) so it costs no credits, but it writes swaps
    and coverage records. Resumable: run it again and it continues.
    """
    tape = _tape_module()
    if tape is None:
        return None
    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    if dry_run:
        due = list(token) or tape.candidates(ch, conn, limit=limit or 500)
        payload = {"dry_run": True, "due": len(due), "tokens": due[:50],
                   "coverage_now": tape.coverage_summary(ch, conn)}
        if json_out:
            return _echo_json(payload)
        console.print(f"[yellow]dry run:[/yellow] {len(due)} mint(s) due; nothing collected")
        for mint in due[:20]:
            console.print(f"  {mint}")
        return None

    report = tape.run(
        ch, conn,
        limit=limit or None,
        budget_s=budget_s or None,
        seed=not no_seed,
        repair=not no_repair,
        tokens=list(token) or None,
    )
    conn.commit()
    if json_out:
        return _echo_json(report.as_dict())
    console.print(
        f"[bold]tape[/bold] {report.attempted} attempted in {report.elapsed_s:.0f}s: "
        f"{report.completed} complete, {report.partial} partial, "
        f"{report.unavailable} unavailable, {report.skipped} skipped"
    )
    table = Table("measure", "value", box=None)
    table.add_row("rows written", str(report.rows_written))
    table.add_row("pages / requests", f"{report.pages} / {report.requests}")
    table.add_row("rate limited", str(report.rate_limited))
    table.add_row("create flags set", str(report.create_flags_set))
    table.add_row("fee payers set", str(report.fee_payers_set))
    table.add_row("complete before -> after", f"{report.complete_before} -> {report.complete_after}")
    console.print(table)
    if report.reasons:
        reasons = Table("reason", "count", title="why a mint ended where it did")
        for reason, n in sorted(report.reasons.items(), key=lambda kv: -kv[1]):
            reasons.add_row(reason, str(n))
        console.print(reasons)
    return None


# ---------------------------------------------------------------- paper


def _paper_modules():  # noqa: ANN202 - typer must not import the modules at definition time
    try:
        from kaiba.execution import curve_price, paper
    except ImportError as exc:
        _missing("paper broker (kaiba.execution.paper)", exc)
        return None, None
    return paper, curve_price


def _broker(paper: Any, curve_price: Any, conn: Any, *, live_curve: bool) -> Any:
    """A broker whose curve comes from a snapshot by default, or from pump.fun on request."""
    resolver = None
    if live_curve:
        resolver = curve_price.live_resolver(conn, fallback=curve_price.snapshot_resolver(conn))
    return paper.PaperBroker(conn, curve_resolver=resolver)


def _sol(base_units: Any, chain: Any) -> str:
    from decimal import Decimal

    from kaiba.core.schemas import NATIVE_DECIMALS

    if base_units is None:
        return "-"
    return f"{Decimal(int(base_units)) / (Decimal(10) ** NATIVE_DECIMALS[chain]):f}"


def _order_summary(order: Any, chain: Any) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "decision_id": order.decision_id,
        "state": order.state.value,
        "side": order.side.value,
        "lane": order.lane.value,
        "mode": order.mode.value,
        "token": order.token,
        "amount_in": str(order.amount_in),
        "amount_in_native": _sol(order.amount_in, chain),
        "filled_out": str(order.filled_out) if order.filled_out is not None else None,
        "fee_native": str(order.fee_native) if order.fee_native is not None else None,
        "slippage_bps": order.slippage_bps,
        "tx_hash": order.tx_hash,
        "error": order.error,
    }


@paper_app.command("buy")
def paper_buy(
    mint: str = typer.Argument(..., help="The mint to buy."),
    size_sol: float = typer.Option(0.05, "--size-sol", help="Position size in native units."),
    live_curve: bool = typer.Option(
        False, "--live-curve", help="Price against pump.fun now instead of a stored snapshot."
    ),
    lane: str = typer.Option("manual", "--lane", help="Lane recorded on the decision and order."),
    chain: str = typer.Option("sol", "--chain"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve the curve; place no order."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Place one shadow entry through the paper broker.

    Shadow only, always: a paper order carries ``mode=shadow`` and ``provider=paper``, and
    there is no flag here that makes it anything else. A FAILED order is a result — the
    broker refuses a fill it could not price or could not get inside the slippage cap, and
    a broker that always fills makes the whole shadow record a fiction.
    """
    paper, curve_price = _paper_modules()
    if paper is None:
        return None
    from decimal import Decimal

    from kaiba.core.db import ensure_db, upsert
    from kaiba.core.schemas import (
        NATIVE_DECIMALS,
        Action,
        Decision,
        Lane,
        LaneMode,
        digest,
        now_ms,
    )

    ch = _chain_or_fail(chain)
    try:
        want_lane = Lane(lane)
    except ValueError:
        return _fail(f"unknown lane {lane!r}", "one of: " + ", ".join(lit.value for lit in Lane))
    size = Decimal(str(size_sol))
    if size <= 0:
        return _fail("--size-sol must be positive")
    amount_in = int(size * (Decimal(10) ** NATIVE_DECIMALS[ch]))
    conn = ensure_db()
    broker = _broker(paper, curve_price, conn, live_curve=live_curve)
    ts = now_ms()

    if dry_run:
        state, note = broker.resolve_curve(ch, mint, ts)
        payload = {
            "dry_run": True, "token": mint, "chain": ch.value, "lane": want_lane.value,
            "amount_in": amount_in, "amount_in_native": _sol(amount_in, ch),
            "curve_found": state is not None, "curve_note": note,
            "curve": state.as_dict() if state is not None else None,
            "resolver": "live" if live_curve else "snapshot",
        }
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] would buy {_sol(amount_in, ch)} {ch.value} of "
            f"{mint[:16]} on lane {want_lane.value}"
        )
        console.print(
            f"curve: {'resolved' if state is not None else 'NOT resolved'} ({note}) via the "
            f"{'live' if live_curve else 'snapshot'} resolver"
        )
        if state is not None:
            console.print(
                f"  progress {state.progress_pct}% of a {state.graduation_sol} SOL target, "
                f"{state.sol_in_curve} SOL in curve"
            )
        return None

    decision_id = "cli_" + digest({"t": ts, "token": mint, "size": amount_in, "lane": want_lane.value})[:16]
    decision = Decision(
        decision_id=decision_id,
        ts_ms=ts,
        lane=want_lane,
        mode=LaneMode.SHADOW,
        chain=ch,
        token=mint,
        action=Action.ENTER,
        thesis="operator paper entry via `kaiba paper buy`",
        confidence=0.5,
        size_base_units=amount_in,
        invalidation="operator closes it, or the shadow record shows the fill was fiction",
        model="cli",
    )
    # The broker stamps `decision_id` onto the order; without the row the order ledger
    # points at nothing and `kaiba orders show` cannot explain where it came from.
    upsert(
        conn, "decisions",
        {
            "decision_id": decision.decision_id, "ts_ms": decision.ts_ms,
            "lane": decision.lane.value, "mode": decision.mode.value, "chain": decision.chain.value,
            "token": decision.token, "action": decision.action.value, "thesis": decision.thesis,
            "confidence": decision.confidence, "signals_json": "[]",
            "dossier_grade": decision.dossier_grade.value,
            "size_base_units": decision.size_base_units, "size_pct_bankroll": None,
            "expected_return_pct": None, "invalidation": decision.invalidation, "regime": None,
            "blockers_json": "[]", "params_version": decision.params_version,
            "model": decision.model, "trace_id": None,
        },
        ["decision_id"],
    )
    conn.commit()

    order = broker.buy(decision)
    conn.commit()
    basis = paper.fill_basis(conn, order.order_id)
    position = paper.position_for_order(conn, order.order_id)
    payload = {**_order_summary(order, ch), "position_id": position, "basis": basis}
    if json_out:
        return _echo_json(payload)
    filled = order.state.value == "filled"
    colour = "green" if filled else "red"
    console.print(
        f"[{colour}]{order.state.value.upper()}[/{colour}] {order.order_id}  "
        f"{_sol(order.amount_in, ch)} {ch.value} -> "
        f"{order.filled_out if order.filled_out is not None else '-'} atoms of {mint[:16]}"
    )
    if order.error:
        console.print(f"[red]reason:[/red] {order.error}")
    if position:
        console.print(f"position: {position}")
    if basis:
        _echo_json(basis)
    else:
        console.print("[dim]no fill basis recorded (the order never priced)[/dim]")
    return None


@paper_app.command("close")
def paper_close(
    position_id: str = typer.Argument(..., help="The paper position to reduce."),
    pct: float = typer.Option(100.0, "--pct", help="Percentage of the remaining quantity to sell."),
    live_curve: bool = typer.Option(False, "--live-curve", help="Price against pump.fun now."),
    reason: str = typer.Option("manual", "--reason", help="Recorded as the exit reason."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the position; sell nothing."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Sell part or all of a paper position."""
    paper, curve_price = _paper_modules()
    if paper is None:
        return None
    from decimal import Decimal

    from kaiba.core.db import ensure_db

    if not 0 < pct <= 100:
        return _fail("--pct must be in (0, 100]")
    conn = ensure_db()
    position = paper.load_position(conn, position_id)
    if position is None:
        return _fail(f"no position {position_id!r}", "list them with `kaiba positions show --all`")
    ch = position.chain
    if dry_run:
        payload = {
            "dry_run": True, "position_id": position_id, "chain": ch.value,
            "token": position.token, "qty": str(position.qty),
            "would_sell_pct": pct, "cost_native": str(position.cost_native),
            "resolver": "live" if live_curve else "snapshot",
        }
        if json_out:
            return _echo_json(payload)
        console.print(
            f"[yellow]dry run:[/yellow] would sell {pct:g}% of {position.qty} atoms of "
            f"{position.token[:16]} (position {position_id})"
        )
        return None

    broker = _broker(paper, curve_price, conn, live_curve=live_curve)
    order = broker.sell(position, Decimal(str(pct)), exit_reason=reason)
    conn.commit()
    after = paper.load_position(conn, position_id)
    basis = paper.fill_basis(conn, order.order_id)
    payload = {
        **_order_summary(order, ch),
        "position_id": position_id,
        "qty_remaining": str(after.qty) if after else None,
        "realized_native": str(after.realized_native) if after else None,
        "basis": basis,
    }
    if json_out:
        return _echo_json(payload)
    colour = "green" if order.state.value == "filled" else "red"
    console.print(
        f"[{colour}]{order.state.value.upper()}[/{colour}] {order.order_id}  sold {pct:g}% of "
        f"{position.token[:16]}"
    )
    if order.error:
        console.print(f"[red]reason:[/red] {order.error}")
    if after is not None:
        console.print(
            f"position {position_id}: {after.qty} atoms left, realised "
            f"{_sol(after.realized_native, ch)} {ch.value}"
        )
    if basis:
        _echo_json(basis)
    return None


@paper_app.command("basis")
def paper_basis(
    order_id: str = typer.Argument(..., help="The paper order to explain."),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Where one paper fill's price came from. Read-only.

    ``None`` is the honest answer for an order that never priced, and it is reported as
    that rather than as an empty basis.
    """
    paper, _curve = _paper_modules()
    if paper is None:
        return None
    from kaiba.core.db import ensure_db

    basis = paper.fill_basis(ensure_db(), order_id)
    if json_out:
        return _echo_json({"order_id": order_id, "basis": basis})
    if basis is None:
        console.print(f"[yellow]no price basis recorded for {order_id}[/yellow]")
        console.print("[dim]either the order never priced, or it was not a paper order[/dim]")
        raise typer.Exit(1)
    table = Table("field", "value", title=f"fill basis · {order_id}")
    for key in sorted(basis):
        table.add_row(key, str(basis[key])[:90])
    console.print(table)
    return None


# ---------------------------------------------------------------- curve


@curve_app.command("compare")
def curve_compare(
    mint: str = typer.Argument(..., help="The mint to quote both ways."),
    size_sol: float = typer.Option(0.02, "--size-sol", help="Buy size for both quotes."),
    chain: str = typer.Option("sol", "--chain"),
    snapshot: bool = typer.Option(
        False, "--snapshot", help="Use a stored curve snapshot instead of asking pump.fun."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Quote one buy off our curve arithmetic and off the router, and print the gap.

    The instrument for tracking curve-versus-router drift over time, so the text form is
    one stable ``key: value`` per line in a fixed order: two runs diff cleanly, and a
    field that became unknown shows up as a changed line rather than a vanished one.
    Two network calls, no Helius credits, and nothing is written.
    """
    _paper, curve_price = _paper_modules()
    if curve_price is None:
        return None
    from decimal import Decimal

    from kaiba.core.db import ensure_db
    from kaiba.core.schemas import NATIVE_DECIMALS, now_ms

    ch = _chain_or_fail(chain)
    size = Decimal(str(size_sol))
    if size <= 0:
        return _fail("--size-sol must be positive")
    lamports_in = int(size * (Decimal(10) ** NATIVE_DECIMALS[ch]))
    conn = ensure_db()
    resolver = (
        curve_price.snapshot_resolver(conn) if snapshot
        else curve_price.live_resolver(conn, fallback=curve_price.snapshot_resolver(conn))
    )
    state, note = resolver(ch, mint, now_ms())
    if state is None:
        return _fail(f"no curve for {mint}", note)

    result = curve_price.compare_with_router(state, mint, lamports_in, conn=conn)
    fields = (
        "token", "lamports_in", "on_bonding_curve", "route_label", "curve_atoms_out",
        "router_atoms_out", "gap_bps", "curve_round_trip_bps", "router_round_trip_bps", "note",
    )
    payload: dict[str, Any] = {
        "curve_source": "snapshot" if snapshot else "live",
        "curve_observed_ms": state.observed_ms,
        "curve_complete": state.complete,
        "known": result.known,
        **{name: getattr(result, name) for name in fields},
    }
    if json_out:
        return _echo_json(payload)
    for key in ("token", "curve_source", "curve_observed_ms", "curve_complete", "known", *fields[1:]):
        value = payload[key]
        console.print(f"{key + ':':<26}{'-' if value is None else value}")
    if not result.known:
        console.print(
            "\n[yellow]no gap could be measured[/yellow] — the router had no route, so this "
            "run says nothing about drift"
        )
    return None


# ---------------------------------------------------------------- viability


@viability_app.command("band")
def viability_band(
    mint: str = typer.Argument(..., help="The mint to size."),
    chain: str = typer.Option("sol", "--chain"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Every size at which this trade is economic *and* executable, and the cost at each.

    Four numbers and what they cost: the smallest size whose round trip fits the ceiling,
    the size where cost per unit of position is lowest, the largest that still fits the
    ceiling, and the largest whose own impact still fits the slippage tolerance. An
    unknown cost is printed as unknown; it is never rendered as zero, because a cost we
    could not measure is the one that ruins a position.
    """
    try:
        from kaiba.execution import viability
    except ImportError as exc:
        return _missing("sizing economics (kaiba.execution.viability)", exc)
    from decimal import Decimal

    from kaiba.core.db import ensure_db

    ch = _chain_or_fail(chain)
    conn = ensure_db()
    band = viability.sizing_band(ch, conn, token=mint)
    points = (
        ("min viable", band.min_viable_base_units),
        ("optimal", band.optimal_base_units),
        ("max economic", band.max_viable_base_units),
        ("max executable", band.max_executable_base_units),
    )
    rows: list[dict[str, Any]] = []
    for label, size in points:
        row: dict[str, Any] = {"point": label, "size_base_units": size,
                               "size_native": _sol(size, ch) if size else None}
        if size:
            est = viability.estimate_round_trip(
                ch, size, conn, model=band.model, depth=band.depth, token=mint,
                ceiling_pct=band.ceiling_pct,
            )
            row.update({
                "cost_base_units": est.total_base_units,
                "cost_native": _sol(est.total_base_units, ch) if est.total_base_units else None,
                "cost_pct": str(est.pct) if est.pct is not None else None,
                "flat_native": _sol(est.flat_base_units, ch) if est.flat_base_units else None,
                "proportional_native": (
                    _sol(est.proportional_base_units, ch) if est.proportional_base_units else None
                ),
                "impact_native": _sol(est.impact_base_units, ch) if est.impact_base_units else None,
                "impact_bps_one_leg": est.impact_bps_one_leg,
                "known": est.known,
            })
        rows.append(row)

    payload = {
        "chain": ch.value,
        "token": mint,
        "viable": band.viable,
        "reason": band.reason,
        "ceiling_pct": str(band.ceiling_pct) if band.ceiling_pct is not None else None,
        "slippage_tolerance_bps": band.slippage_tolerance_bps,
        "cost_model": {
            "basis": band.model.basis.value, "source": band.model.source,
            "flat_per_leg_base_units": band.model.flat_per_leg_base_units,
            "proportional_bps_per_leg": (
                str(band.model.proportional_bps_per_leg)
                if band.model.proportional_bps_per_leg is not None else None
            ),
            "sample_trades": band.model.sample_trades,
            "from_live_trades": band.model.from_live_trades,
        },
        "depth": {
            "basis": band.depth.basis.value, "source": band.depth.source,
            "max_size_base_units": band.depth.max_size_base_units,
        },
        "points": rows,
        "findings": band.findings(),
    }
    if json_out:
        return _echo_json(payload)

    verdict = "[green]VIABLE[/green]" if band.viable else "[red]NOT VIABLE[/red]"
    console.print(f"\n{verdict}  {mint}  on {ch.value}")
    console.print(f"[dim]{band.reason}[/dim]\n")
    table = Table("point", f"size ({ch.value})", f"cost ({ch.value})", "cost %", "impact bps",
                  title="sizing band")
    for row in rows:
        if row["size_base_units"] is None:
            table.add_row(str(row["point"]), "[dim]none[/dim]", "-", "-", "-")
            continue
        pct = row.get("cost_pct")
        ceiling = band.ceiling_pct
        over = pct is not None and ceiling is not None and Decimal(pct) > ceiling
        # Two decimal places on screen, full precision in --json: nobody reads a cost
        # share to 28 significant figures, and the wide column hid the number that matters.
        rounded = None if pct is None else Decimal(pct).quantize(Decimal("0.01"))
        shown = "[red]unknown[/red]" if pct is None else (
            f"[red]{rounded}% (over)[/red]" if over else f"{rounded}%"
        )
        table.add_row(
            str(row["point"]), str(row["size_native"]), str(row.get("cost_native") or "unknown"),
            shown, str(row.get("impact_bps_one_leg") if row.get("impact_bps_one_leg") is not None else "-"),
        )
    console.print(table)
    console.print(
        f"ceiling {payload['ceiling_pct'] or 'unusable'}% of position · slippage tolerance "
        f"{band.slippage_tolerance_bps} bps"
    )
    console.print(
        f"cost model: {band.model.source} ({band.model.basis.value}, "
        f"{band.model.sample_trades} sample trade(s))"
    )
    console.print(f"depth: {band.depth.source} ({band.depth.basis.value})")
    for note in payload["findings"]:
        console.print(f"[yellow]•[/yellow] {note}")
    console.print()
    return None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
