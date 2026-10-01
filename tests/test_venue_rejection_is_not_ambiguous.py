"""A venue that ANSWERS "400" has not sent anything. That is a refusal, not an ambiguity.

MEASURED 2026-09-22 on the live box, and it cost about as much as the trade made.

`0x6863b3fd8caa85d6ca8f80bf10083a94de467777` (flap/bsc) was entered by sm-trenches at
$0.0000200682 and ran to $0.000189 -- 8.75x. The trailing stop fired at $0.0001317 and the
exit died like this, verbatim from ``orders.error``::

    gmgn-cli exit 1: WARNING  Swap - confirmation required
    --------------------------------
      Chain:        bsc
      ...
    [gmgn-cli] Proceeding non-interactively (--yes + GMGN_ALLOW_AUTOMATED_TRADES=1).
    [gmgn-cli] POST /v1/trade/swap failed: HTTP 400 code=400 error=40003...

That is the venue rejecting the request. No order was created and no provider_order_id was
issued. But the order went to UNKNOWN, and UNKNOWN is load-bearing: the watchdog's rule is
"an ambiguous or in-flight send is resolved by reconciliation, never retried", and
:func:`reconcile` returns early unless there is a ``provider_order_id`` to query. There
never was one. The exit was held shut for 23 minutes, emitting ``exit_not_resubmitted``
every tick, while the position fell from 8.75x to 3.79x. Realised +0.0333 BNB; roughly
another 0.033 BNB was left in the market by the deadlock.

WHY THE CLASSIFIER MISSED IT. ``_SENT_MARKERS`` had grown a ``"post /v1/trade/"`` entry,
with the sound observation that the CLI now prints its confirmation banner even for
approved automated sends, so the banner alone no longer proves a pre-send stop. True -- but
that marker matches the FAILURE line itself, ``POST /v1/trade/swap failed: HTTP 400``. "We
attempted a POST" was read as "it was sent", and the one token on that line that actually
settles the question -- the status code -- was ignored.

THE DISCRIMINATORS, all three measured from the real CLI on 2026-09-22:

* ``Proceeding non-interactively`` -- the CLI did NOT stop to ask. Printed whenever
  ``--yes`` and the env flag are both present, which is always, for us.
* the banner WITHOUT that line -- it genuinely stopped and asked. Nothing was sent.
* ``POST <path> failed: HTTP 4xx`` -- the venue received it, parsed it and refused it.
  Nothing exists to reconcile. A 5xx, a timeout or a connection error is NOT this: the
  request may have been accepted and the answer lost, and those stay ambiguous.

The asymmetry the old comment invoked is real -- a false REFUSED can double-send, a false
AMBIGUOUS costs one reconcile -- but it was priced wrong. A false AMBIGUOUS on an order
with no provider_order_id is not "one manual reconcile", it is a position the machine can
never exit. Both branches cost real money, so the rule has to be right rather than merely
conservative.
"""

from __future__ import annotations

import pytest

from kaiba.execution import executor
from kaiba.execution.executor import (
    ExecutionAmbiguous,
    ExecutionRefused,
    _looks_sent,
    _venue_rejected,
)

#: The banner, verbatim from gmgn-cli 1.6.1 on the live box.
BANNER = (
    "\n⚠️  Swap — confirmation required\n"
    "--------------------------------\n"
    "  Chain:        bsc\n"
    "  Wallet:       0x72430877378522d1b759ac721561aa9eb9e25c2b\n"
    "  Input token:  0x6863b3fd8caa85d6ca8f80bf10083a94de467777\n"
    "  Output token: 0x0000000000000000000000000000000000000000\n"
    "  Amount:       467754762292813431665841 (smallest unit)\n"
    "  Slippage:     25\n"
)
PROCEEDED = (
    "[gmgn-cli] Proceeding non-interactively (--yes + GMGN_ALLOW_AUTOMATED_TRADES=1).\n"
)
REJECTED = (
    "[gmgn-cli] POST /v1/trade/swap failed: HTTP 400 code=400 error=40000300 "
    "message=GEvmInvalidArgument"
)

#: The exact stderr of the exit that deadlocked, reproduced from the live CLI.
LIVE_STUCK_EXIT = BANNER + PROCEEDED + REJECTED


def _run(monkeypatch, *, stderr: str, returncode: int = 1) -> None:
    class Fake:
        def __init__(self) -> None:
            self.returncode = returncode

        def communicate(self, timeout=None):
            return "", stderr

        def kill(self):
            pass

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: Fake())
    monkeypatch.setattr(executor.shutil, "which", lambda name: "/usr/local/bin/gmgn-cli")


# ------------------------------------------------------- the status code is the evidence


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_4xx_on_the_trade_post_is_a_rejection(status):
    rejected, why = _venue_rejected(f"POST /v1/trade/swap failed: HTTP {status} code={status}")
    assert rejected and str(status) in why


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_5xx_is_not_a_rejection(status):
    """The venue may have accepted it and failed to answer. That is the ambiguous case."""
    rejected, _ = _venue_rejected(f"POST /v1/trade/swap failed: HTTP {status}")
    assert not rejected


@pytest.mark.parametrize(
    "blob",
    [
        "POST /v1/trade/swap failed: ETIMEDOUT",
        "POST /v1/trade/swap failed: socket hang up",
        "POST /v1/trade/swap failed: ECONNRESET",
        "",
        BANNER,
    ],
)
def test_no_status_line_is_not_a_rejection(blob):
    """Absence of an answer is exactly what we cannot resolve. It must stay ambiguous."""
    assert not _venue_rejected(blob)[0]


def test_a_4xx_on_some_other_endpoint_is_not_a_trade_rejection():
    """Only the trade POST speaks to whether an ORDER exists."""
    assert not _venue_rejected("POST /v1/wallet/info failed: HTTP 400")[0]


# ------------------------------------------------------- end to end through _run_gmgn


def test_the_live_stuck_exit_is_now_refused(tmp_db, monkeypatch):
    """THE REGRESSION. This exact stderr produced UNKNOWN and deadlocked a live exit."""
    _run(monkeypatch, stderr=LIVE_STUCK_EXIT)
    with pytest.raises(ExecutionRefused) as caught:
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)
    assert "400" in str(caught.value)
    assert "40000300" in str(caught.value), "the venue's own code must reach orders.error"


def test_a_5xx_after_the_banner_stays_ambiguous(tmp_db, monkeypatch):
    """The case the ``post /v1/trade/`` marker was added to protect. It still holds."""
    _run(
        monkeypatch,
        stderr=BANNER + PROCEEDED + "[gmgn-cli] POST /v1/trade/swap failed: HTTP 502 Bad Gateway",
    )
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_a_timeout_after_the_banner_stays_ambiguous(tmp_db, monkeypatch):
    _run(
        monkeypatch,
        stderr=BANNER + PROCEEDED + "[gmgn-cli] POST /v1/trade/swap failed: ETIMEDOUT",
    )
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_a_4xx_is_still_ambiguous_when_an_order_id_is_present(tmp_db, monkeypatch):
    """Hard evidence of a send outranks the status line. Never retry something live."""
    _run(monkeypatch, stderr=BANNER + PROCEEDED + "order_id=od10bsc0000001a0c8adcaa\n" + REJECTED)
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_the_genuine_prompt_stop_is_still_refused(tmp_db, monkeypatch):
    """No ``Proceeding`` line means the CLI really did stop and ask. Nothing was sent."""
    _run(monkeypatch, stderr=BANNER)
    with pytest.raises(ExecutionRefused, match="refused before send"):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_a_banner_that_proceeded_and_said_nothing_else_is_ambiguous(tmp_db, monkeypatch):
    """It went past the prompt and we have no answer. We do not know. Say so."""
    _run(monkeypatch, stderr=BANNER + PROCEEDED)
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


# ------------------------------------------------------- the marker that caused it


def test_the_status_code_not_the_post_path_decides_a_rejection():
    """The FIRST fix here deleted ``post /v1/trade/`` from the send markers outright. That
    made the 400 case right and the NETWORK-ERROR case wrong: a socket that died with the
    request in flight stopped looking sent, so an order that may well exist would have been
    classified REFUSED and retried -- the double-send this module exists to prevent.

    The marker is back. What separates the two cases is not the path, it is whether the
    venue answered: a status code means it received, parsed and refused; no status code
    means we do not know. So the path reads as sent, and only :func:`_venue_rejected`
    downgrades it.
    """
    assert _looks_sent("[gmgn-cli] POST /v1/trade/swap failed: network error")
    assert _venue_rejected("[gmgn-cli] POST /v1/trade/swap failed: HTTP 400")[0]
    assert not _venue_rejected("[gmgn-cli] POST /v1/trade/swap failed: network error")[0]


def test_hard_evidence_outranks_a_venue_rejection(monkeypatch):
    """An order id plus a 4xx is a LATER request failing, not an order never created.

    Precedence is the whole safety property here, and it is checked in one direction on
    purpose: when both readings are available, the one that keeps us from resending wins.
    """
    blob = chr(10).join((
        "[gmgn-cli] order_id=od_991",
        "[gmgn-cli] POST /v1/trade/swap failed: HTTP 400",
    ))
    assert executor._hard_send_evidence(blob)
    _run(monkeypatch, stderr=blob)
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_a_bare_four_hundred_without_hard_evidence_is_refused(monkeypatch):
    _run(monkeypatch, stderr="[gmgn-cli] POST /v1/trade/swap failed: HTTP 400")
    with pytest.raises(ExecutionRefused):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_a_network_error_on_the_trade_post_is_ambiguous(monkeypatch):
    """THE REGRESSION the restored marker prevents: in flight, no answer, may exist."""
    _run(monkeypatch, stderr="[gmgn-cli] POST /v1/trade/swap failed: network error")
    with pytest.raises(ExecutionAmbiguous):
        executor._run_gmgn(["swap", "--chain", "bsc"], mutating=True)


def test_real_send_markers_still_work():
    for marker in ("order_id=od10", "tx_hash: 0xabc", "submitted", "signature 2nkX"):
        assert _looks_sent(BANNER + marker), marker
