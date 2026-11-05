"""A mutating CLI request that reached the trade POST is never a safe FAILED retry."""
from kaiba.execution.executor import _looks_sent


def test_trade_post_failure_is_treated_as_a_possible_send():
    blob = "[gmgn-cli] POST /v1/trade/swap failed: network error"
    assert _looks_sent(blob) is True
