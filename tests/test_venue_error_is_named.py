"""The venue's own error must reach the refusal reason, not gmgn-cli's banner.

MEASURED 2026-09-24: 272 of 291 failed sells stored a reason beginning "⚠️  Swap —
confirmation required", because gmgn-cli prints that banner FIRST and the venue's real
answer LAST. The robinhood positions stuck for 21-46 hours were actually failing on
`error=40002700 message=GetSwapRouteError` -- no swap route exists. Two separate
sessions chased the banner instead of the error.

Classification is NOT changed by naming the error: a 4xx stays REFUSED.
"""
import pytest

from kaiba.execution.executor import _venue_error_name, _venue_rejected

# The real payload, trimmed. Banner first, diagnosis last -- that ordering is the point.
REAL = (
    "⚠️  Swap — confirmation required\n"
    "--------------------------------\n"
    "  Chain:        robinhood\n"
    "  Input token:  0xc777f874a7350cfa2bf123ed09253e225dc254d9\n"
    " non-interactively (--yes + GMGN_ALLOW_AUTOMATED_TRADES=1).\n"
    "[gmgn-cli] POST /v1/trade/swap failed: HTTP 400 code=400 error=40002700 "
    "message=GetSwapRouteError"
)


def test_the_venue_error_is_extracted():
    assert _venue_error_name(REAL) == " (GetSwapRouteError/40002700)"


def test_the_refusal_reason_names_the_real_error_not_the_banner():
    rejected, why = _venue_rejected(REAL)
    assert rejected is True
    assert "GetSwapRouteError" in why, why
    assert "40002700" in why, why


def test_a_4xx_is_still_refused_which_is_the_safety_property():
    """Naming the error must not reclassify it: refused means no order was created."""
    rejected, _ = _venue_rejected(REAL)
    assert rejected is True


def test_a_5xx_is_still_not_a_rejection_even_when_named():
    blob = (
        "[gmgn-cli] POST /v1/trade/swap failed: HTTP 502 code=502 error=40002700 "
        "message=GetSwapRouteError"
    )
    rejected, why = _venue_rejected(blob)
    assert rejected is False, "a 5xx may have been accepted; it must stay ambiguous"
    assert "GetSwapRouteError" in why


def test_an_unnamed_failure_still_works():
    blob = "[gmgn-cli] POST /v1/trade/swap failed: HTTP 400 code=400"
    rejected, why = _venue_rejected(blob)
    assert rejected is True
    assert why == "venue answered HTTP 400 on /v1/trade/swap"


def test_no_trade_post_at_all_is_not_a_rejection():
    assert _venue_rejected("something else entirely") == (False, "")


@pytest.mark.parametrize("name", ["GetSwapRouteError", "GEvmInsufficientSlippage"])
def test_the_errors_we_actually_see_are_named(name):
    blob = f"POST /v1/trade/swap failed: HTTP 400 code=400 error=40003702 message={name}"
    assert name in _venue_rejected(blob)[1]
