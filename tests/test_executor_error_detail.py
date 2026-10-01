"""A failed gmgn-cli invocation keeps its real complaint, and "nothing was sent" is proven
rather than assumed.

MEASURED 2026-09-22: a live sm-trenches BUY (ord_aff1bbb86edd3d61ced4be6d, 0.0499 SOL) was
stored as "gmgn-cli refused before send (confirmation prompt)" although the executor passes
``--yes`` and GMGN_ALLOW_AUTOMATED_TRADES=1 is set on the ops unit. Cause: the detail was
``stderr[:300]``, gmgn-cli prints its summary banner FIRST, and 300 characters never reached
the actual error. Every distinct failure looked identical in ``orders.error``.

The pre-send classification is safe only as a claim that nothing left us -- it returns the
order to FAILED, which is retryable. For that order it was true (no provider_order_id, no
tx_hash, on-chain balance of the output token 0). It must not be taken on trust when the
output says otherwise, because retrying a live order is the double-send this codebase has
spent real money learning to prevent.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor
from kaiba.execution.executor import (
    ExecutionAmbiguous,
    ExecutionRefused,
    _error_detail,
    _looks_sent,
)

#: The real banner, as gmgn-cli printed it on the live box.
BANNER = (
    "\n⚠️  Swap — confirmation required\n"
    "--------------------------------\n"
    "  Chain:        sol\n"
    "  Wallet:       62EbtFbmJATT94Y6Uguf6a1kdaJuZWAu8ycqbg3xKfQg\n"
    "  Input token:  So11111111111111111111111111111111111111112\n"
    "  Output token: 8jPieFubCsr3KKWzR5z9HbHaNmxmkyHCQoZKf4F7pump\n"
    "  Amount:       49865460 (small)\n"
)


# ----------------------------------------------------------------- the detail itself


def test_the_real_error_survives_a_long_banner():
    """300 characters of banner is exactly what hid the diagnosis."""
    blob = BANNER + "x" * 2000 + "\nError: insufficient balance for rent-exempt account\n"
    detail = _error_detail(blob, "")
    assert "insufficient balance for rent-exempt account" in detail
    assert "Swap" in detail, "the head is still useful context"
    assert "chars omitted" in detail


def test_a_short_error_is_returned_whole():
    assert _error_detail("Error: bad slippage", "") == "Error: bad slippage"


def test_stdout_is_used_when_stderr_is_empty():
    assert "only on stdout" in _error_detail("", "only on stdout")


def test_the_detail_is_bounded():
    assert len(_error_detail("y" * 100_000, "")) <= 1300


# ----------------------------------------------------------------- the send guard


@pytest.mark.parametrize("marker", ["order_id=od10sol888b", "tx_hash: 3avV", "submitted", "signature 2nkX"])
def test_any_sign_of_a_send_is_detected(marker):
    assert _looks_sent(BANNER + marker)


def test_a_bare_banner_is_not_a_send():
    assert not _looks_sent(BANNER)


def _submit(conn, monkeypatch, *, returncode: int, stderr: str):
    class P:
        pass

    proc = P()
    proc.returncode = returncode

    def fake_popen(*a, **k):
        class Fake:
            def __init__(self) -> None:
                self.returncode = returncode

            def communicate(self, timeout=None):
                return "", stderr

            def kill(self):
                pass

        return Fake()

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(executor.shutil, "which", lambda name: "/usr/local/bin/gmgn-cli")


def test_a_banner_with_no_send_marker_is_refused(tmp_db, monkeypatch):
    """Refused == nothing left us == safe to retry. True for the measured order."""
    _submit(tmp_db, monkeypatch, returncode=1, stderr=BANNER)
    with pytest.raises(ExecutionRefused, match="refused before send"):
        executor._run_gmgn(["swap", "--chain", "sol"], mutating=True)


def test_a_banner_that_also_shows_an_order_id_is_AMBIGUOUS_not_refused(tmp_db, monkeypatch):
    """The dangerous case: the banner is echoed but the request DID leave us.

    Refused would invite a retry of an order that may already be filling.
    """
    _submit(tmp_db, monkeypatch, returncode=1,
            stderr=BANNER + "\nsubmitted order_id=od10sol04b99255c5189c49aa\nthen the node timed out\n")
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "sol"], mutating=True)


def test_a_non_mutating_call_is_never_ambiguous(tmp_db, monkeypatch):
    """A read that fails costs nothing and must stay a plain refusal."""
    _submit(tmp_db, monkeypatch, returncode=1, stderr="boom, and an order_id=od1 in the text")
    with pytest.raises(ExecutionRefused):
        executor._run_gmgn(["order", "get", "--chain", "sol"], mutating=False)


def test_the_raised_error_carries_the_tail_end_to_end(tmp_db, monkeypatch):
    """Through ``_run_gmgn``, not just the helper: this is the call site that was wrong.

    The banner plus 2,000 characters of noise, with the real complaint last. A head-only
    slice at the call site puts the banner in ``orders.error`` and loses the cause, which
    is exactly what happened on the live box.
    """
    real = "Error: insufficient SOL for rent-exempt token account"
    _submit(tmp_db, monkeypatch, returncode=1, stderr=BANNER + "n" * 2000 + "\n" + real + "\n")
    with pytest.raises(ExecutionRefused) as caught:
        executor._run_gmgn(["swap", "--chain", "sol"], mutating=True)
    assert real in str(caught.value), "the real cause did not survive into the stored error"
