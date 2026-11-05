"""Packaging/CLI smoke coverage; no provider or credential dependencies."""

from importlib.metadata import version

import pytest

typer = pytest.importorskip("typer")
from typer.testing import CliRunner  # noqa: E402

from kaiba.cli.main import app  # noqa: E402

runner = CliRunner()


def test_installed_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"kaiba {version('kaiba')}"


def test_help_lists_the_command_groups():
    # Updated by Claude 2026-09-20: `probe` is now registered (P0-4 owns the
    # implementation). The contract is no longer "hide it" but "if the module is not
    # there yet, say so clearly instead of crashing" — asserted below.
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "version", "status", "probe", "db", "wallet", "token", "run", "risk",
        "ingest", "engine", "protection", "positions", "orders", "reconcile", "signer",
    ):
        assert command in result.stdout


def test_unimplemented_subsystem_fails_with_a_readable_message(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.startswith("kaiba.cli.probe"):
            raise ImportError("not built yet")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    result = runner.invoke(app, ["probe"])
    assert result.exit_code == 1
    assert "not available yet" in result.stdout


def test_there_is_no_withdraw_command():
    """The owner's single hard rule, asserted at the CLI surface too."""
    result = runner.invoke(app, ["--help"])
    for word in ("withdraw", "transfer", "send-funds"):
        assert word not in result.stdout.lower()


def test_unknown_command_is_an_error():
    assert runner.invoke(app, ["not-a-command"]).exit_code != 0


def test_production_service_entrypoints_are_registered():
    for argv, option in (
        (["ingest", "run", "--help"], "--listeners"),
        (["engine", "run", "--help"], "--once"),
        (["protection", "run", "--help"], "Run the exit watchdog"),
        (["reconcile", "once", "--help"], "--write-reports"),
        (["signer", "serve", "--help"], "--socket-mode"),
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, result.stdout
        assert option in result.stdout


def test_ingest_entrypoint_normalizes_all_and_dispatches(monkeypatch):
    calls = []

    def fake_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr("kaiba.ingest.runner.main", fake_main)
    result = runner.invoke(app, ["ingest", "run", "--listeners", "all"])
    assert result.exit_code == 0, result.stdout
    # Derived from REGISTRY rather than frozen as a literal: "all" means every registered
    # listener, so pinning the names here made adding one a test failure instead of a
    # feature. (Changed by the robinhood/Pons ingest task, 2026-09-20.)
    from kaiba.ingest.runner import REGISTRY

    assert calls == [["--feeds", ",".join(REGISTRY)]]


def test_reconcile_entrypoint_can_publish_reports(monkeypatch, tmp_path):
    monkeypatch.setattr("kaiba.execution.executor.reconcile_all", lambda conn: {"o-1": "filled"})
    monkeypatch.setattr(
        "kaiba.cli.reports.write_reports",
        lambda: [tmp_path / "status.json", tmp_path / "positions.json"],
    )
    result = runner.invoke(app, ["reconcile", "once", "--write-reports"])
    assert result.exit_code == 0, result.stdout
    assert '"o-1": "filled"' in result.stdout
    assert "status.json" in result.stdout


def test_signer_entrypoint_parses_socket_options(monkeypatch, tmp_path):
    calls = []

    def fake_serve(path, *, socket_group=None, socket_mode=0o660):
        calls.append((path, socket_group, socket_mode))

    monkeypatch.setattr("kaiba.execution.signer.serve", fake_serve)
    result = runner.invoke(
        app,
        [
            "signer", "serve", "--socket", str(tmp_path / "signer.sock"),
            "--socket-group", "kaiba-signer-ipc", "--socket-mode", "0660",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert calls == [(tmp_path / "signer.sock", "kaiba-signer-ipc", 0o660)]


def test_ingest_default_excludes_telegram_but_all_includes_it(monkeypatch):
    """The default is the safe subset; typing "all" is the operator choosing the rest."""
    calls = []
    monkeypatch.setattr("kaiba.ingest.runner.main", lambda argv: calls.append(argv) or 0)

    runner.invoke(app, ["ingest", "run", "--listeners", "all"])
    assert "telegram" in calls[-1][1]

    from kaiba.ingest.runner import DEFAULT_FEEDS

    assert "telegram" not in DEFAULT_FEEDS


# --------------------------------------------------------------------------------------
# The six modules wired on 2026-09-20.
#
# Each landed tested and unreachable. These tests are about the *wiring*, not the modules,
# and each is written as the wiring failure it prevents:
#
# * ``test_wallet_cluster_calls_clustering_run_not_the_naive_composition`` — the body this
#   command used to have resolved the whole database into one 876-address "entity".
# * ``test_cli_defaults_are_the_modules_own`` — Typer needs literal defaults, and a CLI
#   default that has quietly drifted from the module's is a silently different command.
# * ``test_a_dry_run_that_would_spend_credits_calls_nothing...`` — the price is quoted and
#   nothing is called, which is the only reason --dry-run is worth typing.
# * ``test_a_replayed_discovery_run_that_does_not_reproduce_is_refused`` — attaching a
#   cohort to a run it did not come from is worse than not freezing at all.
# --------------------------------------------------------------------------------------

WIRED_GROUPS = ("discover", "track", "tape", "paper", "curve", "viability")

#: Every command the wiring added, and whether it must offer a ``--dry-run``.
#:
#: The rule is "a dry run wherever it spends credits or writes". ``track scan`` is the one
#: writer without one: it records the windows it evaluated through
#: ``tracker.scan_recent_tokens``, which exposes no way to suppress that write, and a
#: ``--dry-run`` that wrote anyway would be worse than none. Changing that belongs in the
#: tracker, not here.
WIRED_COMMANDS: tuple[tuple[list[str], bool], ...] = (
    (["discover", "run"], True),
    (["discover", "freeze"], True),
    (["discover", "forward"], False),
    (["discover", "propose"], True),
    (["track", "seed"], True),
    (["track", "list"], False),
    (["track", "audit"], False),
    (["track", "sweep"], True),
    (["track", "poll"], True),
    (["track", "scan"], False),
    (["track", "status"], False),
    (["track", "rescreen"], True),
    (["track", "remove"], True),
    (["tape", "status"], False),
    (["tape", "collect"], True),
    (["paper", "buy"], True),
    (["paper", "close"], True),
    (["paper", "basis"], False),
    (["curve", "compare"], False),
    (["viability", "band"], False),
    (["wallet", "cluster"], True),
    (["wallet", "coverage"], False),
)


def test_the_six_wired_groups_are_registered():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in WIRED_GROUPS:
        assert group in result.stdout


@pytest.mark.parametrize("argv", [argv for argv, _ in WIRED_COMMANDS], ids=lambda a: " ".join(a))
def test_every_wired_command_offers_json(argv):
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0, result.stdout
    assert "--json" in result.stdout


@pytest.mark.parametrize(
    "argv", [argv for argv, spends in WIRED_COMMANDS if spends], ids=lambda a: " ".join(a)
)
def test_every_command_that_spends_or_writes_offers_a_dry_run(argv):
    result = runner.invoke(app, [*argv, "--help"])
    assert result.exit_code == 0, result.stdout
    assert "--dry-run" in result.stdout


def test_cli_defaults_are_the_modules_own():
    """Typer needs literals; a literal that has drifted is a silently different command."""
    import inspect

    from kaiba.cli import main as cli
    from kaiba.intelligence import clustering, discover, tracker

    assert cli.DISCOVER_DEFAULT_ALPHA == discover.DEFAULT_ALPHA
    assert cli.DISCOVER_DEFAULT_MIN_EDGE == discover.MIN_INTERESTING_EDGE
    assert cli.CLUSTER_MAX_ENTITY_SIZE == clustering.MAX_ENTITY_SIZE
    assert cli.TRACKER_POLL_CREDITS == tracker.HELIUS_POLL_CREDITS
    for fn in (tracker.seed_from_grades, tracker.seed_from_discovery):
        assert inspect.signature(fn).parameters["limit"].default == cli.TRACKER_SEED_LIMIT
    assert (
        inspect.signature(tracker.scan_recent_tokens).parameters["limit"].default
        == cli.TRACKER_SCAN_LIMIT
    )


def _fake_clustering_run(clustering, **kw):
    from kaiba.core.schemas import Chain

    return clustering.ClusteringRun(
        run_id=kw.pop("run_id", 1), chain=Chain.SOL, model=clustering.MODEL_ID,
        started_ms=0, finished_ms=1, status="ok", **kw,
    )


def test_wallet_cluster_calls_clustering_run_not_the_naive_composition(tmp_db, monkeypatch):
    """`derive_all` + `build_entities` + `persist_entities` made one 876-member "entity".

    The guards that stop that — hub pruning, service detection, the entity size cap —
    live in ``clustering.run``. Calling the three steps directly is the bug, so this
    fails the moment anything reaches for them again.
    """
    from kaiba.intelligence import cluster, clustering, entity

    seen: list[dict] = []

    def fake_run(conn, chain, **kw):
        seen.append(kw)
        return _fake_clustering_run(clustering, edges_by_rule={"same_funder": 3}, addresses_seen=9)

    def forbidden(*args, **kwargs):
        raise AssertionError("wallet cluster must go through clustering.run")

    monkeypatch.setattr(clustering, "run", fake_run)
    monkeypatch.setattr(cluster, "derive_all", forbidden)
    monkeypatch.setattr(entity, "persist_entities", forbidden)

    result = runner.invoke(app, ["wallet", "cluster", "--chain", "sol", "--json"])
    assert result.exit_code == 0, result.stdout
    assert seen == [{"fund_credits": 0, "max_entity_size": clustering.MAX_ENTITY_SIZE}]
    assert '"edges_total": 3' in result.stdout


def test_wallet_cluster_dry_run_rebuilds_nothing(tmp_db, monkeypatch):
    from kaiba.intelligence import clustering

    def forbidden(*args, **kwargs):
        raise AssertionError("--dry-run must not rebuild")

    monkeypatch.setattr(clustering, "run", forbidden)
    result = runner.invoke(app, ["wallet", "cluster", "--dry-run", "--json"])
    assert result.exit_code == 0, result.stdout
    assert '"dry_run": true' in result.stdout.lower()


@pytest.mark.parametrize(
    ("argv", "entry_point", "expected_credits"),
    [
        (["track", "poll", "--dry-run"], "poll_wallets", 30),
        (["track", "rescreen", "--dry-run"], "rescreen", 30),
        (["track", "seed", "--source", "grades", "--limit", "4", "--dry-run"],
         "seed_from_grades", 40),
    ],
    ids=["poll", "rescreen", "seed"],
)
def test_a_dry_run_that_would_spend_credits_calls_nothing_and_quotes_the_bill(
    tmp_db, monkeypatch, argv, entry_point, expected_credits
):
    from kaiba.intelligence import tracker

    def forbidden(*args, **kwargs):
        raise AssertionError(f"{entry_point} must not be called on a dry run")

    monkeypatch.setattr(tracker, entry_point, forbidden)
    monkeypatch.setattr(tracker, "watched_addresses", lambda chain, conn=None: {"a", "b", "c"})
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout
    assert f"up to {expected_credits} Helius credit" in result.stdout.replace("\n", " ")


def test_track_poll_says_how_many_credits_before_it_spends_them(tmp_db, monkeypatch):
    from kaiba.intelligence import tracker

    called: list[str] = []
    monkeypatch.setattr(tracker, "watched_addresses", lambda chain, conn=None: {"a", "b"})
    monkeypatch.setattr(
        tracker, "poll_wallets", lambda chain, conn=None, **kw: called.append("polled") or []
    )
    result = runner.invoke(app, ["track", "poll", "--json"])
    assert result.exit_code == 0, result.stdout
    assert called == ["polled"]
    assert "up to 20 Helius credit" in result.stdout.replace("\n", " ")


def test_a_replayed_discovery_run_that_does_not_reproduce_is_refused(tmp_db, monkeypatch):
    """``freeze_cohort`` and ``propose_research_cohort`` take a live report and the module
    ships no reader, so ``--run`` re-screens at the stored ``as_of_ms``. If the flow under
    that run moved, the recomputed id differs and the command must refuse rather than
    attach a different candidate set to the named run."""
    from kaiba.core.db import upsert
    from kaiba.intelligence import discover

    upsert(tmp_db, "discovery_runs",
           {"run_id": "disc_old", "chain": "sol", "run_ms": 1, "as_of_ms": 1,
            "model_version": discover.MODEL_ID}, ["run_id"])
    tmp_db.commit()

    def different(conn=None, **kw):
        return discover.DiscoveryReport(
            run_id="disc_new", chain="sol",
            chance=discover.ChanceReport(screened_addresses=0, screened_entities=0),
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("a run that did not reproduce must not be frozen or proposed")

    monkeypatch.setattr(discover, "discover", different)
    monkeypatch.setattr(discover, "freeze_cohort", forbidden)
    monkeypatch.setattr(discover, "propose_research_cohort", forbidden)

    for argv in (["discover", "freeze", "--run", "disc_old"],
                 ["discover", "propose", "--run", "disc_old"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 1, result.stdout
        assert "could not be reproduced" in result.stdout


def test_paper_buy_records_the_decision_behind_the_order(tmp_db, monkeypatch):
    """An order whose ``decision_id`` points at nothing cannot be explained afterwards."""
    from kaiba.core.db import fetch_one
    from kaiba.core.schemas import Chain, Lane, LaneMode, Order, OrderState, Side
    from kaiba.execution import paper

    captured: list = []

    class FakeBroker:
        def __init__(self, conn, **kw):
            self.conn = conn

        def buy(self, decision, **kw):
            captured.append(decision)
            return Order(
                order_id="ord_test", decision_id=decision.decision_id, chain=Chain.SOL,
                token=decision.token, side=Side.BUY, lane=decision.lane, mode=decision.mode,
                input_token="native", output_token=decision.token,
                amount_in=decision.size_base_units or 0, min_out=0, slippage_bps=1500,
                state=OrderState.FILLED, provider="paper", filled_out=42,
                created_ms=1, updated_ms=1,
            )

    monkeypatch.setattr(paper, "PaperBroker", FakeBroker)
    monkeypatch.setattr(paper, "fill_basis", lambda conn, order_id: {"basis": "curve"})
    monkeypatch.setattr(paper, "position_for_order", lambda conn, order_id: "pos_test")

    result = runner.invoke(app, ["paper", "buy", "MINT", "--size-sol", "0.05", "--json"])
    assert result.exit_code == 0, result.stdout
    assert len(captured) == 1
    decision = captured[0]
    # Shadow, always: no flag on this command makes a paper order anything else.
    assert decision.mode is LaneMode.SHADOW
    assert decision.lane is Lane.MANUAL
    assert decision.size_base_units == 50_000_000
    row = fetch_one(tmp_db, "SELECT * FROM decisions WHERE decision_id=?", (decision.decision_id,))
    assert row is not None and row["mode"] == "shadow"


def test_paper_buy_dry_run_places_no_order(tmp_db, monkeypatch):
    from kaiba.core.db import fetch_one
    from kaiba.execution import paper

    class FakeBroker:
        def __init__(self, conn, **kw):
            pass

        def resolve_curve(self, chain, token, ts, **kw):
            return None, "no_curve_snapshot"

        def buy(self, decision, **kw):
            raise AssertionError("--dry-run must not place an order")

    monkeypatch.setattr(paper, "PaperBroker", FakeBroker)
    result = runner.invoke(app, ["paper", "buy", "MINT", "--size-sol", "0.05", "--dry-run"])
    assert result.exit_code == 0, result.stdout
    assert "no_curve_snapshot" in result.stdout
    assert fetch_one(tmp_db, "SELECT COUNT(*) AS n FROM decisions")["n"] == 0


def test_curve_compare_prints_one_stable_line_per_field(tmp_db, monkeypatch):
    """This command is the drift instrument, so two runs have to diff cleanly: a fixed
    field order, one ``key: value`` per line, and a field that became unknown showing up
    as a changed line rather than a vanished one."""
    from kaiba.execution import curve_price

    state = curve_price.CurveState(
        virtual_sol=30_000_000_000, virtual_token=1_073_000_000_000_000,
        real_sol=1_000_000_000, real_token=793_100_000_000_000, observed_ms=1,
    )
    monkeypatch.setattr(curve_price, "live_resolver", lambda conn=None, **kw: (
        lambda chain, token, at_ms: (state, "ok")
    ))
    monkeypatch.setattr(
        curve_price, "compare_with_router",
        lambda st, token, lamports_in, **kw: curve_price.CurveVsRouter(
            token=token, lamports_in=lamports_in, curve_atoms_out=100, router_atoms_out=99,
            gap_bps=101, route_label="Pump.fun", on_bonding_curve=True,
        ),
    )
    result = runner.invoke(app, ["curve", "compare", "MINT", "--size-sol", "0.02"])
    assert result.exit_code == 0, result.stdout
    keys = [line.split(":", 1)[0].strip() for line in result.stdout.splitlines() if ":" in line]
    assert keys[:5] == ["token", "curve_source", "curve_observed_ms", "curve_complete", "known"]
    assert "gap_bps" in keys and "router_round_trip_bps" in keys
    # A missing figure keeps its line.
    assert "note:" in result.stdout

    as_json = runner.invoke(app, ["curve", "compare", "MINT", "--size-sol", "0.02", "--json"])
    assert as_json.exit_code == 0, as_json.stdout
    assert '"gap_bps": 101' in as_json.stdout and '"lamports_in": 20000000' in as_json.stdout


def test_curve_compare_refuses_rather_than_pricing_off_a_stale_curve(tmp_db):
    result = runner.invoke(app, ["curve", "compare", "MINT", "--snapshot"])
    assert result.exit_code == 1
    assert "no curve" in result.stdout


def test_paper_basis_reports_an_unpriced_order_as_unpriced(tmp_db):
    result = runner.invoke(app, ["paper", "basis", "ord_nothing"])
    assert result.exit_code == 1
    assert "no price basis recorded" in result.stdout


def test_paper_buy_refuses_a_non_positive_size(tmp_db):
    result = runner.invoke(app, ["paper", "buy", "MINT", "--size-sol", "0"])
    assert result.exit_code == 1
    assert "must be positive" in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["tape", "status", "--json"],
        ["track", "list", "--json"],
        ["track", "status", "--json"],
        ["track", "audit", "SomeAddress", "--json"],
        ["track", "scan", "--json"],
        ["wallet", "coverage", "--json"],
        ["viability", "band", "SomeMint", "--json"],
    ],
    ids=lambda a: " ".join(a[:2]),
)
def test_read_only_commands_run_against_an_empty_database(tmp_db, argv):
    """No provider, no data, no crash: the empty answer is an answer."""
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.stdout


def test_an_unknown_chain_is_a_readable_error_not_a_traceback(tmp_db):
    result = runner.invoke(app, ["tape", "status", "--chain", "dogecoin"])
    assert result.exit_code == 1
    assert "unknown chain" in result.stdout
