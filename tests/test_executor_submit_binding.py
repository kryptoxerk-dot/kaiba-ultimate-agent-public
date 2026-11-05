"""Prepared-wallet intent at the actual executor transport boundary; all funds/config are fixtures."""

from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from base58 import b58encode

from kaiba.core import config, db
from kaiba.core.config import ChainBudget, LaneConfig, RiskConfig
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side, digest
from kaiba.execution import executor

# Deterministic synthetic public keys, never operator/provider wallet identities.
SOL_WALLET = b58encode(bytes([1]) * 32).decode()
OTHER_WALLET = b58encode(bytes([2]) * 32).decode()
SOL_TOKEN = b58encode(bytes([3]) * 32).decode()
SENTINEL = "unit-only-api-credential-never-retain"


def _load_mutant(name, source):
    path = Path(executor.__file__)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    for name, value in {
        "KAIBA_DATA_DIR": str(tmp_path), "KAIBA_DB_PATH": str(tmp_path / "book.db"),
        "KAIBA_RISK_PATH": str(tmp_path / "risk.yaml"), "KAIBA_CONFIG_DIR": str(tmp_path / "config"),
        "GMGN_API_KEY": SENTINEL, "GMGN_PRIVATE_KEY": "unit-only-private-key-never-retain",
    }.items():
        monkeypatch.setenv(name, value)
    cli = tmp_path / "fake-cli.js"
    cli.write_text("// never executed", encoding="utf-8")
    monkeypatch.setenv("GMGN_CLI_PATH", str(cli))
    config.get_settings.cache_clear()
    db.close_thread_conn()
    conn = db.get_conn()
    db.migrate(conn)
    risk = RiskConfig(global_mode=LaneMode.LIVE)
    risk.bounds.max_lane_mode = LaneMode.LIVE
    risk.lanes[Lane.SM_TRENCHES] = LaneConfig(mode=LaneMode.LIVE)
    risk.chains[Chain.SOL] = ChainBudget(wallet=SOL_WALLET)
    risk.chains[Chain.ROBINHOOD] = ChainBudget(wallet="0x" + "1" * 40)
    config.save_risk(risk, tmp_path / "risk.yaml")
    candidate = executor

    def deny_network(*args, **kwargs):
        raise AssertionError("network is forbidden in executor binding tests")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(candidate.subprocess, "Popen", deny_network)
    yield candidate, conn, risk, tmp_path
    db.close_thread_conn()
    config.get_settings.cache_clear()


def order(candidate, *, chain=Chain.SOL, side=Side.BUY, mode=LaneMode.LIVE):
    return candidate.build_order(
        decision_id="review-d1", chain=chain,
        token=SOL_TOKEN if chain is Chain.SOL else "0x" + "2" * 40,
        side=side, lane=Lane.SM_TRENCHES, mode=mode,
        amount_in=10_000_000, min_out=1, slippage_bps=300,
    )


def bindings(conn, oid):
    rows = conn.execute("SELECT id,state,detail FROM order_events WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    return [(r["state"], json.loads(r["detail"])) for r in rows
            if r["detail"] and r["detail"].startswith("{")
            and json.loads(r["detail"]).get("event") == "gmgn_submit_prepared"]


def install_transport(env, monkeypatch, oid, *, stderr="", stdout=None, returncode=0, spawn_error=False):
    candidate, conn, _, path = env
    seen = []

    def spawn(argv, **kwargs):
        # The real _run_gmgn reaches this seam only after the history row was written.
        with closing(sqlite3.connect(path / "book.db")) as observer:
            observer.row_factory = sqlite3.Row
            stored = bindings(observer, oid)
        assert len(stored) == 1, "binding must be durable before external transport"
        state, receipt = stored[0]
        args = argv[argv.index("swap"):]
        assert state == "submitting"
        assert receipt["from_address"] == args[args.index("--from") + 1]
        assert receipt["chain"] == args[args.index("--chain") + 1]
        assert receipt["request_digest"] == digest({"provider": "gmgn", "argv": args})
        assert receipt["request_basis"] == "public_argv"
        assert receipt["dispatch"] == "unproven"
        assert receipt["order_id"] == oid
        assert receipt["prepared_ms"] > 0
        seen.append((list(argv), receipt))
        if spawn_error:
            raise OSError("unit spawn unavailable")

        class Process:
            def __init__(self):
                self.returncode = returncode

            def communicate(self, timeout=None):
                return (stdout if stdout is not None else json.dumps({
                    "data": {"order_id": "provider-unit-1", "tx_hash": "unit-tx"},
                })), stderr

        return Process()

    monkeypatch.setattr(candidate.subprocess, "Popen", spawn)
    return seen


@pytest.mark.parametrize("chain,side,mode", [
    (Chain.SOL, Side.BUY, LaneMode.LIVE),
    (Chain.SOL, Side.SELL, LaneMode.LIVE),
    (Chain.ROBINHOOD, Side.BUY, LaneMode.CANARY),
    (Chain.ROBINHOOD, Side.SELL, LaneMode.LIVE),
])
def test_binding_matches_actual_public_command_before_transport(env, monkeypatch, chain, side, mode):
    candidate, conn, _, _ = env
    current = order(candidate, chain=chain, side=side, mode=mode)
    if side is Side.SELL:
        bought = order(candidate, chain=chain).model_copy(update={"state": OrderState.FILLED, "filled_out": 10**15})
        candidate._persist(bought, conn)
    seen = install_transport(env, monkeypatch, current.order_id)
    result = candidate.submit(current, conn)
    assert result.state is OrderState.SUBMITTED
    assert len(seen) == 1
    assert seen[0][1]["token"] == current.token
    assert seen[0][1]["side"] == side.value
    # Existing lifecycle and provider charging are preserved; no extra history state.
    assert [r[0] for r in conn.execute("SELECT state FROM order_events WHERE order_id=? ORDER BY id", (current.order_id,))] == [
        "reserved", "submitting", "submitted"]
    assert conn.execute("SELECT weight FROM provider_calls WHERE provider='gmgn'").fetchone()[0] == 10


@pytest.mark.parametrize("failure", ["shadow", "policy", "mode", "limiter"])
def test_pre_dispatch_denials_never_claim_a_prepared_binding(env, failure):
    candidate, conn, risk, path = env
    current = order(candidate, mode=LaneMode.SHADOW if failure == "shadow" else LaneMode.LIVE)
    if failure == "policy":
        current = current.model_copy(update={"slippage_bps": 9900})
    elif failure == "mode":
        risk.kill_switch = True
        config.save_risk(risk, path / "risk.yaml")
    elif failure == "limiter":
        conn.execute("INSERT INTO provider_family_bans VALUES (?,?,?,?)", (
            "gmgn", "trade", candidate.now_ms() + 600_000, "unit cooldown"))
    if failure == "shadow":
        assert candidate.submit(current, conn).detail == "shadow: not sent"
    else:
        with pytest.raises((candidate.ExecutionRefused, candidate.RateLimited)):
            candidate.submit(current, conn)
    assert bindings(conn, current.order_id) == []
    assert conn.execute("SELECT COUNT(*) FROM provider_calls WHERE status='ok'").fetchone()[0] == 0


@pytest.mark.parametrize("kind,final", [
    ("ambiguous", "unknown"), ("venue_refused", "failed"), ("spawn_failed", "failed"),
])
def test_prepared_binding_does_not_claim_dispatch_or_settlement_after_failure(env, monkeypatch, kind, final):
    candidate, conn, _, _ = env
    current = order(candidate)
    kwargs = {
        "ambiguous": {"returncode": 1, "stdout": "", "stderr": "Error: socket hang up"},
        "venue_refused": {"returncode": 1, "stdout": "", "stderr": "POST /v1/trade/swap failed: HTTP 400 bad request"},
        "spawn_failed": {"spawn_error": True},
    }[kind]
    seen = install_transport(env, monkeypatch, current.order_id, **kwargs)
    with pytest.raises((candidate.ExecutionAmbiguous, candidate.ExecutionRefused)):
        candidate.submit(current, conn)
    assert len(seen) == 1
    receipt = bindings(conn, current.order_id)[0][1]
    assert receipt["dispatch"] == "unproven"
    assert "filled" not in receipt and "economic_complete" not in receipt
    assert conn.execute("SELECT state FROM orders WHERE order_id=?", (current.order_id,)).fetchone()[0] == final


def test_binding_uses_prepared_from_even_when_current_config_changes(env):
    candidate, _, risk, path = env
    current = order(candidate)
    args = ["swap", "--chain", "sol", "--from", SOL_WALLET,
            "--input-token", current.input_token, "--output-token", current.output_token,
            "--amount", str(current.amount_in), "--min-output", "1", "--slippage", "3.00", "--anti-mev", "--yes"]
    risk.chains[Chain.SOL].wallet = OTHER_WALLET
    config.save_risk(risk, path / "risk.yaml")
    receipt = json.loads(candidate._gmgn_submit_binding(current, args))
    assert candidate.get_risk().chains[Chain.SOL].wallet == OTHER_WALLET
    assert receipt["from_address"] == SOL_WALLET
    assert receipt["request_digest"] == digest({"provider": "gmgn", "argv": [*args, "--raw"]})
    args[args.index("--from") + 1] = OTHER_WALLET
    assert json.loads(candidate._gmgn_submit_binding(current, args))["from_address"] == OTHER_WALLET


@pytest.mark.parametrize("invalid", ["duplicate_from", "secret_address", "long_order", "wrong_token"])
def test_unavailable_binding_is_explicit_and_never_retains_unsafe_public_fields(env, invalid):
    candidate, _, _, _ = env
    current = order(candidate)
    args = ["swap", "--chain", "sol", "--from", SOL_WALLET,
            "--input-token", current.input_token, "--output-token", current.output_token]
    if invalid == "duplicate_from":
        args += ["--from", OTHER_WALLET]
    elif invalid == "secret_address":
        args[args.index("--from") + 1] = SENTINEL
    elif invalid == "long_order":
        current = current.model_copy(update={"order_id": "ord_" + "a" * 500})
    else:
        args[-1] = OTHER_WALLET
    detail = candidate._gmgn_submit_binding(current, args)
    receipt = json.loads(detail)
    assert receipt["binding"] == "unavailable"
    assert "from_address" not in receipt and "request_digest" not in receipt
    assert SENTINEL not in detail and len(detail) <= 500


def test_credentials_and_successful_raw_provider_payload_never_enter_binding_or_logs(env, monkeypatch, caplog):
    candidate, conn, _, _ = env
    current = order(candidate)
    install_transport(env, monkeypatch, current.order_id, stdout=json.dumps({
        "data": {"order_id": "provider-unit-1", "unused_private_payload": SENTINEL},
    }))
    candidate.submit(current, conn)
    text = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM order_events"))
    text += "\n" + "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM events"))
    text += "\n" + caplog.text
    assert SENTINEL not in text
    assert "unit-only-private-key-never-retain" not in text
    assert "unused_private_payload" not in text


def test_oversized_binding_does_not_add_an_exit_gate(env, monkeypatch):
    candidate, conn, _, _ = env
    bought = order(candidate).model_copy(update={"state": OrderState.FILLED, "filled_out": 10**15})
    candidate._persist(bought, conn)
    current = order(candidate, side=Side.SELL).model_copy(update={"order_id": "ord_" + "a" * 500})
    calls = []

    def spawn(argv, **kwargs):
        assert bindings(conn, current.order_id)[0][1]["binding"] == "unavailable"
        calls.append(argv)

        class Process:
            returncode = 0

            def communicate(self, timeout=None):
                return '{"data":{"order_id":"unit-exit"}}', ""

        return Process()

    monkeypatch.setattr(candidate.subprocess, "Popen", spawn)
    assert candidate.submit(current, conn).state is OrderState.SUBMITTED
    assert len(calls) == 1


def test_inherited_caller_transaction_is_not_committed_to_create_evidence(env, monkeypatch):
    candidate, conn, _, path = env
    current = order(candidate)
    conn.execute("BEGIN IMMEDIATE")

    def spawn(argv, **kwargs):
        assert conn.in_transaction
        assert len(bindings(conn, current.order_id)) == 1
        with closing(sqlite3.connect(path / "book.db")) as observer:
            assert observer.execute("SELECT COUNT(*) FROM order_events").fetchone()[0] == 0

        class Process:
            returncode = 0

            def communicate(self, timeout=None):
                return '{"data":{"order_id":"unit-caller-transaction"}}', ""

        return Process()

    monkeypatch.setattr(candidate.subprocess, "Popen", spawn)
    assert candidate.submit(current, conn).state is OrderState.SUBMITTED
    assert conn.in_transaction  # Caller-owned transaction: durable intent requires caller commit.
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_removing_binding_hook_is_caught_at_the_real_transport_boundary(env, monkeypatch):
    candidate, conn, _, _ = env
    source = Path(executor.__file__).read_text(encoding="utf-8")
    old = "_transition(order, OrderState.SUBMITTING, c, _gmgn_submit_binding(order, args))"
    assert source.count(old) == 1
    mutant = _load_mutant("kaiba.execution.executor_binding_removed", source.replace(
        old, "_transition(order, OrderState.SUBMITTING, c)"))
    current = order(mutant)
    install_transport(env, monkeypatch, current.order_id)
    with pytest.raises(AssertionError, match="binding must be durable"):
        mutant.submit(current, conn)
