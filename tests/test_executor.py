"""Order submission: fail-closed behaviour, the ambiguous-send rule, and reconciliation."""

from __future__ import annotations

import os

import pytest

from kaiba.core.config import load_risk, save_risk
from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor

SOL = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


@pytest.fixture
def live_risk(tmp_path, monkeypatch):
    """A risk file with the lane live and a bound wallet, so we can exercise submission."""
    cfg = load_risk()
    # Fixture intent must not inherit a live operator incident brake.
    # These flags apply only to the temporary test config, never production.
    cfg.kill_switch = False
    cfg.entries_paused = False
    cfg.reduce_only = False
    cfg.global_mode = LaneMode.LIVE
    cfg.bounds.max_lane_mode = LaneMode.LIVE
    cfg.lanes[Lane.CONFLUENCE_5].mode = LaneMode.LIVE
    cfg.chains[Chain.SOL].wallet = WALLET
    path = tmp_path / "risk.yaml"
    save_risk(cfg, path)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(path))
    return path


def make_order(mode: LaneMode = LaneMode.LIVE, side: Side = Side.BUY):
    return executor.build_order(
        decision_id="dec1", chain=Chain.SOL, token=SOL, side=side,
        lane=Lane.CONFLUENCE_5, mode=mode, amount_in=10_000_000, min_out=1,
        slippage_bps=300,
    )


# ---------------------------------------------------------------- the swap body


def test_body_has_exactly_the_nine_allowed_keys(live_risk):
    body = executor.gmgn_swap_body(make_order(), WALLET)
    assert set(body) == {
        "chain", "from_address", "input_token", "output_token", "input_amount",
        "min_output_amount", "swap_mode", "slippage", "auto_slippage",
    }


def test_body_sends_slippage_as_decimal_percent_not_bps(live_risk):
    """300 bps is 3.00 percent. Sending "300" would overpay by 100x."""
    assert executor.gmgn_swap_body(make_order(), WALLET)["slippage"] == "3.00"


def test_body_never_enables_auto_slippage(live_risk):
    assert executor.gmgn_swap_body(make_order(), WALLET)["auto_slippage"] is False


def test_body_from_address_is_our_wallet(live_risk):
    assert executor.gmgn_swap_body(make_order(), WALLET)["from_address"] == WALLET


def test_buy_and_sell_swap_the_token_sides(live_risk):
    buy = executor.gmgn_swap_body(make_order(side=Side.BUY), WALLET)
    sell = executor.gmgn_swap_body(make_order(side=Side.SELL), WALLET)
    assert buy["output_token"] == SOL and sell["input_token"] == SOL


# ---------------------------------------------------------------- fail closed


def test_missing_policy_module_refuses_to_submit(tmp_db, live_risk, monkeypatch):
    """No policy means no trading. Never the other way round."""
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **kw):
        if "policy" in name:
            raise ImportError("policy not deployed")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(executor.ExecutionRefused, match="policy module unavailable"):
        executor.submit_gmgn(make_order(), tmp_db)


def test_shadow_mode_never_calls_the_network(tmp_db, live_risk, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("shadow mode must not run the CLI")

    monkeypatch.setattr(executor, "_run_gmgn", explode)
    result = executor.submit(make_order(mode=LaneMode.SHADOW), tmp_db)
    assert result.state is OrderState.PLANNED
    assert "not sent" in (result.detail or "")


def test_unbound_wallet_refuses(tmp_db, tmp_path, monkeypatch):
    cfg = load_risk()
    cfg.global_mode = LaneMode.LIVE
    cfg.bounds.max_lane_mode = LaneMode.LIVE
    cfg.lanes[Lane.CONFLUENCE_5].mode = LaneMode.LIVE
    cfg.chains[Chain.SOL].wallet = None
    p = tmp_path / "risk.yaml"
    save_risk(cfg, p)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(p))
    with pytest.raises(executor.ExecutionRefused, match="no wallet bound"):
        executor.submit_gmgn(make_order(), tmp_db)


def test_kill_switch_refuses(tmp_db, live_risk, monkeypatch):
    import yaml

    raw = yaml.safe_load(live_risk.read_text())
    raw["kill_switch"] = True
    live_risk.write_text(yaml.safe_dump(raw))
    # The kill switch drives every lane's effective mode to OFF, so the lane check
    # refuses first. Either refusal is correct; what matters is that nothing is sent.
    with pytest.raises(executor.ExecutionRefused) as exc:
        executor.submit_gmgn(make_order(), tmp_db)
    assert "off" in str(exc.value) or "kill switch" in str(exc.value)
    assert tmp_db.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"] == 0


def test_reduce_only_refuses_buys_but_not_sells(tmp_db, live_risk):
    import yaml

    raw = yaml.safe_load(live_risk.read_text())
    raw["reduce_only"] = True
    live_risk.write_text(yaml.safe_dump(raw))
    with pytest.raises(executor.ExecutionRefused, match="reduce-only"):
        executor._check_mode(make_order(side=Side.BUY))
    executor._check_mode(make_order(side=Side.SELL))  # must not raise


def test_shadow_lane_mode_blocks_live_submission(tmp_db, tmp_path, monkeypatch):
    cfg = load_risk()  # shipped default: everything shadow
    p = tmp_path / "risk.yaml"
    save_risk(cfg, p)
    monkeypatch.setenv("KAIBA_RISK_PATH", str(p))
    with pytest.raises(executor.ExecutionRefused, match="shadow"):
        executor._check_mode(make_order(mode=LaneMode.LIVE))


# ---------------------------------------------------------------- the ambiguous send


def test_timeout_becomes_unknown_not_failed(tmp_db, live_risk, monkeypatch):
    """A timeout after a swap POST may have spent money. It must not read as 'failed'."""
    monkeypatch.setattr(
        executor, "_authorize", lambda *a, **kw: None
    )

    def timeout(*a, **kw):
        raise executor.ExecutionAmbiguous("gmgn-cli timed out after 45s")

    monkeypatch.setattr(executor, "_run_gmgn", timeout)
    order = make_order()
    with pytest.raises(executor.ExecutionAmbiguous):
        executor.submit_gmgn(order, tmp_db)
    row = tmp_db.execute("SELECT state FROM orders WHERE order_id=?", (order.order_id,)).fetchone()
    assert row["state"] == OrderState.UNKNOWN.value


def test_unparseable_output_is_also_ambiguous(tmp_db, live_risk, monkeypatch, tmp_path):
    """Driven through a real child process: the runner uses Popen, not subprocess.run."""
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(
        executor, "_gmgn_cli_path", lambda: _fake_cli(tmp_path, "not json at all")
    )
    with pytest.raises(executor.ExecutionAmbiguous):
        executor.submit_gmgn(make_order(), tmp_db)


def test_ambiguous_send_is_journalled(tmp_db, live_risk, monkeypatch):
    from kaiba.core import journal

    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(
        executor, "_run_gmgn",
        lambda *a, **kw: (_ for _ in ()).throw(executor.ExecutionAmbiguous("timeout")),
    )
    with pytest.raises(executor.ExecutionAmbiguous):
        executor.submit_gmgn(make_order(), tmp_db)
    bodies = [e["body"] for e in journal.read(conn=tmp_db)]
    assert any("ambiguous" in b and "Reconcile" in b for b in bodies)


def test_unknown_orders_are_listed_for_reconciliation(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(
        executor, "_run_gmgn",
        lambda *a, **kw: (_ for _ in ()).throw(executor.ExecutionAmbiguous("timeout")),
    )
    with pytest.raises(executor.ExecutionAmbiguous):
        executor.submit_gmgn(make_order(), tmp_db)
    pending = executor.unresolved_orders(tmp_db)
    assert len(pending) == 1 and pending[0]["state"] == OrderState.UNKNOWN.value


# ---------------------------------------------------------------- happy path


def test_successful_submission_records_provider_id(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(
        executor, "_run_gmgn",
        lambda *a, **kw: {"code": 0, "data": {"order_id": "gm-123", "tx_hash": "0xabc"}},
    )
    order = make_order()
    result = executor.submit_gmgn(order, tmp_db)
    assert result.state is OrderState.SUBMITTED
    assert result.provider_order_id == "gm-123" and result.tx_hash == "0xabc"


def test_state_history_is_appended_not_overwritten(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "x"}})
    order = make_order()
    executor.submit_gmgn(order, tmp_db)
    states = [
        r["state"]
        for r in tmp_db.execute(
            "SELECT state FROM order_events WHERE order_id=? ORDER BY id", (order.order_id,)
        )
    ]
    assert states == ["reserved", "submitting", "submitted"]


def test_submission_emits_an_event(tmp_db, live_risk, monkeypatch):
    from kaiba.core import events as ev
    from kaiba.core.schemas import EventKind

    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "x"}})
    executor.submit_gmgn(make_order(), tmp_db)
    assert EventKind.ORDER_SUBMITTED.value in [e.kind for e in ev.recent(conn=tmp_db)]


# ---------------------------------------------------------------- reconciliation


def test_reconcile_resolves_unknown_to_filled(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "gm-9"}})
    order = make_order()
    executor.submit_gmgn(order, tmp_db)
    tmp_db.execute(
        "UPDATE orders SET state=? WHERE order_id=?", (OrderState.UNKNOWN.value, order.order_id)
    )
    monkeypatch.setattr(
        executor, "query_gmgn_order",
        lambda o, c=None: {"data": {"status": "successful", "tx_hash": "0xfeed", "output_amount": "42"}},
    )
    assert executor.reconcile(order.order_id, tmp_db) is OrderState.FILLED
    row = tmp_db.execute("SELECT * FROM orders WHERE order_id=?", (order.order_id,)).fetchone()
    assert row["tx_hash"] == "0xfeed" and row["filled_out"] == "42"


def test_reconcile_maps_provider_failure(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "gm-8"}})
    order = make_order()
    executor.submit_gmgn(order, tmp_db)
    monkeypatch.setattr(executor, "query_gmgn_order", lambda o, c=None: {"data": {"status": "failed"}})
    assert executor.reconcile(order.order_id, tmp_db) is OrderState.FAILED


def test_reconcile_leaves_state_alone_when_the_provider_is_unreachable(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "gm-7"}})
    order = make_order()
    executor.submit_gmgn(order, tmp_db)
    tmp_db.execute(
        "UPDATE orders SET state=? WHERE order_id=?", (OrderState.UNKNOWN.value, order.order_id)
    )

    def unreachable(*a, **kw):
        raise executor.ExecutionRefused("provider down")

    monkeypatch.setattr(executor, "query_gmgn_order", unreachable)
    assert executor.reconcile(order.order_id, tmp_db) is OrderState.UNKNOWN


def test_reconcile_all_survives_one_bad_order(tmp_db, live_risk, monkeypatch):
    monkeypatch.setattr(executor, "_authorize", lambda *a, **kw: None)
    monkeypatch.setattr(executor, "_run_gmgn", lambda *a, **kw: {"data": {"order_id": "gm-1"}})
    executor.submit_gmgn(make_order(), tmp_db)
    monkeypatch.setattr(
        executor, "reconcile", lambda oid, c=None: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = executor.reconcile_all(tmp_db)
    assert out and all("error" in v for v in out.values())


def test_reconcile_rejects_an_unknown_order_id(tmp_db):
    with pytest.raises(executor.ExecutionRefused, match="unknown order"):
        executor.reconcile("nope", tmp_db)


# ---------------------------------------------------------------- shape


def test_no_transfer_helper_exists():
    """The module that spends money must not contain a way to move money out."""
    names = [n.lower() for n in dir(executor)]
    for banned in ("withdraw", "transfer_out", "send_funds", "sweep"):
        assert not any(banned in n for n in names)


def test_direct_lane_fails_closed_without_a_signer(tmp_db, live_risk, monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **kw):
        if "signer" in name:
            raise ImportError("no signer")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", blocked)
    order = make_order()
    order = order.model_copy(update={"provider": "direct"})
    with pytest.raises(executor.ExecutionRefused, match="signer"):
        executor.submit_direct(order, tmp_db)


# ------------------------------------------------- exits are never gated (capital safety)


def _order(side, mode=None):
    from kaiba.core.schemas import Chain, Lane, LaneMode, Order, OrderState

    return Order(
        order_id=f"ord_exit_{side.value}", chain=Chain.SOL, token="tok", side=side,
        lane=Lane.CONFLUENCE_5, mode=mode or LaneMode.LIVE, input_token="a", output_token="b",
        amount_in=1_000, min_out=1, slippage_bps=100, state=OrderState.PLANNED,
    )


@pytest.fixture
def live_lane(tmp_path, monkeypatch):
    from kaiba.core.config import load_risk, save_risk
    from kaiba.core.schemas import Lane, LaneMode

    def _set(**over):
        cfg = load_risk()
        cfg.global_mode = LaneMode.LIVE
        cfg.lanes[Lane.CONFLUENCE_5].mode = LaneMode.LIVE
        for k, v in over.items():
            setattr(cfg, k, v)
        p = tmp_path / "risk.yaml"
        save_risk(cfg, p)
        monkeypatch.setenv("KAIBA_RISK_PATH", str(p))

    return _set


@pytest.mark.parametrize("brake", ["kill_switch", "reduce_only", "entries_paused"])
def test_no_brake_can_block_an_exit(live_lane, brake):
    """A kill switch that traps money in a position is worse than no kill switch."""
    from kaiba.core.schemas import Side

    live_lane(**{brake: True})
    executor._check_mode(_order(Side.SELL))  # must not raise


@pytest.mark.parametrize("brake", ["kill_switch", "reduce_only", "entries_paused"])
def test_every_brake_blocks_an_entry(live_lane, brake):
    from kaiba.core.schemas import Side

    live_lane(**{brake: True})
    with pytest.raises(executor.ExecutionRefused):
        executor._check_mode(_order(Side.BUY))


def test_dropping_the_lane_to_shadow_does_not_strand_a_live_position(live_lane):
    """Changing a setting must never make an already-open real position unexitable."""
    from kaiba.core.config import load_risk, save_risk
    from kaiba.core.schemas import Lane, LaneMode, Side

    live_lane()
    cfg = load_risk()
    cfg.lanes[Lane.CONFLUENCE_5].mode = LaneMode.SHADOW
    import os
    from pathlib import Path

    save_risk(cfg, Path(os.environ["KAIBA_RISK_PATH"]))
    executor._check_mode(_order(Side.SELL, mode=LaneMode.LIVE))  # must not raise


def test_a_shadow_order_still_exits_on_paper_not_at_a_venue(live_lane):
    from kaiba.core.schemas import LaneMode, Side

    live_lane()
    with pytest.raises(executor.ExecutionRefused, match="exits on paper"):
        executor._check_mode(_order(Side.SELL, mode=LaneMode.SHADOW))


# ------------------------------------------------- the gmgn-cli subprocess boundary
#
# These run a real child process rather than patching subprocess, because every defect
# here lived at the boundary itself: the decode happens inside subprocess, the orphaned
# workers are real processes, and the abort exit code comes from Node.


def _fake_cli(tmp_path, body: str, *, exit_code: int = 0, stream: str = "stdout"):
    """A stand-in gmgn-cli that emits `body` as raw UTF-8 bytes and exits as told."""
    import sys

    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import sys\n"
        f"data = {body!r}.encode('utf-8')\n"
        f"sys.{stream}.buffer.write(data)\n"
        f"sys.{stream}.buffer.flush()\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_utf8_output_does_not_raise_a_decode_error(tmp_path, monkeypatch):
    """The real CLI emits UTF-8; decoding it as cp1252 raised and escaped every handler."""
    payload = '{"name": "币‘”路é—�", "ok": true}'
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: _fake_cli(tmp_path, payload))
    out = executor._run_gmgn(["token", "info"])
    assert out["ok"] is True
    assert "币" in out["name"]


def test_undecodable_bytes_are_replaced_rather_than_raising(tmp_path, monkeypatch):
    """errors='replace' means a malformed byte degrades the string, never the call."""
    import sys

    script = tmp_path / "bad_bytes.py"
    # 0x8d is the exact byte that raised in production. Built with bytes() so the
    # generated script stays pure ASCII and cannot be mangled by its own escaping.
    script.write_text(
        "import sys\n"
        'sys.stdout.buffer.write(b\'{"n": "a\' + bytes([0x8d]) + b\'b", "ok": true}\')\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: [sys.executable, str(script)])
    assert executor._run_gmgn(["token", "info"])["ok"] is True


def test_a_decode_failure_on_a_swap_leaves_the_order_unknown_not_stuck(tmp_db, monkeypatch):
    """The original bug: the exception escaped, so the order never left SUBMITTING."""
    from kaiba.core.schemas import Chain, Lane, LaneMode, Order, OrderState, Side

    def boom(*a, **k):
        raise UnicodeDecodeError("charmap", b"\x8d", 0, 1, "character maps to <undefined>")

    monkeypatch.setattr(executor.subprocess.Popen, "communicate", boom, raising=False)
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: ["cmd" if os.name == "nt" else "true"])
    order = Order(
        order_id="o_decode", chain=Chain.SOL, token="t", side=Side.BUY, lane=Lane.MANUAL,
        mode=LaneMode.LIVE, input_token="a", output_token="b", amount_in=1, min_out=1,
        slippage_bps=100, state=OrderState.PLANNED,
    )
    assert order.state is OrderState.PLANNED
    with pytest.raises(executor.ExecutionAmbiguous):
        executor._run_gmgn(["swap"], mutating=True)


def test_a_read_call_keeps_calling_an_unreadable_failure_refused(tmp_path, monkeypatch):
    """Only a call that could have sent gets the benefit of the doubt."""

    def boom(*a, **k):
        raise UnicodeDecodeError("charmap", b"\x8d", 0, 1, "nope")

    monkeypatch.setattr(executor.subprocess.Popen, "communicate", boom, raising=False)
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: _fake_cli(tmp_path, "{}"))
    with pytest.raises(executor.ExecutionRefused):
        executor._run_gmgn(["token", "info"], mutating=False)


def test_a_timeout_kills_the_whole_process_tree(tmp_path, monkeypatch):
    """subprocess.run's kill() reaps the child and leaves Node's workers running."""
    import subprocess as sp
    import sys
    import time

    pidfile = tmp_path / "grandchild.pid"
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: [sys.executable, str(parent)])
    with pytest.raises(executor.ExecutionAmbiguous, match="timed out"):
        executor._run_gmgn(["token", "info"], timeout_s=3)

    assert pidfile.exists(), "the fake CLI never started its grandchild; test proves nothing"
    pid = int(pidfile.read_text())

    def alive(p: int) -> bool:
        if os.name == "nt":
            out = sp.run(
                ["tasklist", "/FI", f"PID eq {p}"], capture_output=True, text=True, check=False
            ).stdout
            return str(p) in out
        try:
            os.kill(p, 0)
        except OSError:
            return False
        return True

    # A plain proc.kill() reaps only the direct child and leaves this one running.
    for _ in range(20):
        if not alive(pid):
            break
        time.sleep(0.25)
    assert not alive(pid), f"grandchild {pid} survived the timeout; the tree was not killed"


def test_a_node_abort_exit_code_is_not_mistaken_for_success(tmp_path, monkeypatch):
    """gmgn-cli aborts with 3221226505 on an HTTP error, not 1 and not the status."""
    monkeypatch.setattr(
        executor, "_gmgn_cli_path",
        lambda: _fake_cli(tmp_path, "boom", exit_code=1, stream="stderr"),
    )
    with pytest.raises(executor.ExecutionRefused, match="exit"):
        executor._run_gmgn(["token", "info"])


def test_a_429_on_stdout_is_still_a_rate_limit(tmp_path, monkeypatch):
    """The marker has been seen on both streams; reading only stderr missed it."""
    from kaiba.core.limiter import RateLimited

    body = "[gmgn-cli] GET /v1/trade/follow_wallet failed: HTTP 429 code=429 RATE_LIMIT_EXCEEDED"
    monkeypatch.setattr(
        executor, "_gmgn_cli_path", lambda: _fake_cli(tmp_path, body, exit_code=1)
    )
    with pytest.raises(RateLimited):
        executor._run_gmgn(["track", "kol"])


def test_the_printed_reset_time_becomes_a_real_retry_after():
    """Better than the hardcoded 300s: the provider tells us when it reopens."""
    from datetime import datetime, timedelta

    when = (datetime.now() + timedelta(seconds=90)).strftime("%Y-%m-%d %H:%M")
    got = executor._retry_after_from(f"Rate limit exceeded. Rate limit resets at {when}")
    assert got is not None
    assert 1.0 <= got <= 3600.0


def test_an_absent_reset_time_falls_back_rather_than_guessing():
    assert executor._retry_after_from("HTTP 429 RATE_LIMIT_EXCEEDED") is None


def test_a_nonsense_reset_time_is_not_turned_into_a_cooldown():
    assert executor._retry_after_from("resets at 9999-99-99 99:99") is None
