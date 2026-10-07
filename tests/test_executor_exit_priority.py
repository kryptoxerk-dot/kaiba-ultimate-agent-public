"""A sell must reserve the GMGN bucket at Priority.EXIT, never at ENTRY.

Found 2026-09-21 by adversarial review of the high-volume design: ``submit_gmgn``
hardcoded ``Priority.ENTRY`` on every ``trade.swap`` with no branch on ``order.side``, so
every stop-loss, trailing stop and rug escape the watchdog fired was queued at the same
priority as a speculative buy. The watchdog reads the price at EXIT and then handed the
sell to a queue that discarded that priority. This pins the branch.
"""

from __future__ import annotations

import pytest

from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, Lane, LaneMode, Side
from kaiba.execution import executor


@pytest.fixture
def captured_priority(monkeypatch):
    seen: list[tuple[str, str, Priority]] = []

    class _Guard:
        def __init__(self, provider, endpoint, priority, **_):
            seen.append((provider, endpoint, priority))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(executor, "guarded", _Guard)
    monkeypatch.setattr(executor, "_check_mode", lambda order: None)
    monkeypatch.setattr(executor, "_authorize", lambda order, body, wallet: None)
    monkeypatch.setattr(executor, "_transition", lambda order, state, conn, note=None: order.model_copy(update={"state": state}))
    monkeypatch.setattr(executor, "_run_gmgn", lambda args, mutating=False: {"data": {"order_id": "p1", "tx_hash": "0xabc"}})

    class _Budget:
        wallet = "62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg"
    class _Risk:
        def chain_budget(self, chain): return _Budget()
    monkeypatch.setattr(executor, "get_risk", lambda: _Risk())
    return seen


def _order(side: Side):
    return executor.build_order(
        decision_id="d1", chain=Chain.SOL, token="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        side=side, lane=Lane.MIGRATION_FADE, mode=LaneMode.LIVE,
        amount_in=50_000_000, min_out=1_000, slippage_bps=2500,
    )


def test_a_sell_reserves_the_swap_bucket_at_exit_priority(captured_priority, tmp_db, kaiba_bought):
    o = _order(Side.SELL)
    kaiba_bought(tmp_db, o.chain, o.token)
    executor.submit_gmgn(o, tmp_db)
    swaps = [p for prov, ep, p in captured_priority if ep == "trade.swap"]
    assert swaps == [Priority.EXIT], f"a SELL must reserve at EXIT, got {swaps}"


def test_a_buy_still_reserves_at_entry_priority(captured_priority, tmp_db):
    executor.submit_gmgn(_order(Side.BUY), tmp_db)
    swaps = [p for prov, ep, p in captured_priority if ep == "trade.swap"]
    assert swaps == [Priority.ENTRY], f"a BUY must reserve at ENTRY, got {swaps}"


def test_exit_outranks_entry_in_the_priority_order():
    """The whole point: lower runs first when capacity is scarce."""
    assert Priority.EXIT < Priority.POSITION < Priority.ENTRY < Priority.DISCOVERY


# --------------------------------------------------------------------------------------
# the confirmation-prompt deadlock, 2026-09-21 16:00:24
# --------------------------------------------------------------------------------------

REAL_CONFIRMATION_STDERR = (
    "\u26a0\ufe0f  Swap \u2014 confirmation required\n"
    "--------------------------------\n"
    "  Chain:        sol\n"
    "  Wallet:       62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg\n"
)


def test_a_confirmation_prompt_is_a_refusal_not_an_ambiguous_send(monkeypatch):
    """The first live stop-loss died on this exact stderr and was classified AMBIGUOUS.

    Ambiguous means "may already have sent, resolve by reconcile, never resend". But the
    prompt fires before any request is built, so nothing was sent, and reconcile had no
    provider id to ask about -- a deadlock that held a -30% stop open to -92%. It must be
    a plain refusal so the watchdog resubmits on its normal backoff.
    """
    import subprocess

    class _Proc:
        returncode = 1
        def communicate(self, timeout=None):
            return "", REAL_CONFIRMATION_STDERR  # text mode: Popen runs with errors="replace"
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: ["gmgn-cli"])
    with pytest.raises(executor.ExecutionRefused, match="confirmation prompt"):
        executor._run_gmgn(["swap", "--yes"], mutating=True)


def test_an_unrecognised_nonzero_exit_on_a_send_is_still_ambiguous(monkeypatch):
    """The narrowing must not widen: an unknown failure after a send may have sent."""
    class _Proc:
        returncode = 1
        def communicate(self, timeout=None):
            return "", "Error: socket hang up"
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: _Proc())
    monkeypatch.setattr(executor, "_gmgn_cli_path", lambda: ["gmgn-cli"])
    with pytest.raises(executor.ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--yes"], mutating=True)
