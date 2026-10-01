"""The feeder's window must admit the candidates the live lane exists to trade.

MEASURED 2026-09-24 on the live box, tokens with >=3 tagged smart buyers on sol:

    window   300s ->  0      <-- what the feeder looked at
    window   900s ->  2
    window  1800s -> 13
    window  3600s -> 16

Over six hours 221 tokens across sol/bsc/robinhood reached the bar and `sm-trenches`
made 26 decisions. Confluence ACCUMULATES over roughly half an hour; a five-minute slice
measures a burst, which is a different and much rarer thing. This is the same sampler
failure AGENTS.md records ("THE SAMPLER WAS THE BUG") -- fixed then for token AGE, still
open for the confluence WINDOW.

The two numbers MUST agree. If the feeder looks wider than the lane it queues work the
lane then refuses; narrower, and the lane never sees a candidate at all.
"""
from kaiba.core.config import get_risk
from kaiba.execution.scanner import DEFAULT_CONFIG


def test_the_feeder_and_the_lane_agree_on_now():
    lane = get_risk().lanes["sm-trenches"].params.get("window_s")
    assert lane is not None, "sm-trenches must set window_s explicitly, not inherit 300"
    assert int(lane) == int(DEFAULT_CONFIG.smart_flow_window_s), (
        f"feeder looks back {DEFAULT_CONFIG.smart_flow_window_s}s but the lane uses {lane}s"
    )


def test_the_window_is_wide_enough_to_hold_an_accumulation():
    assert DEFAULT_CONFIG.smart_flow_window_s >= 900, (
        "at 300s the measured supply of qualifying sol tokens was ZERO"
    )


def test_the_feeder_never_looks_further_back_than_the_rows_it_loads():
    """`recent_buys_window_s` bounds the data; asking beyond it silently finds nothing."""
    assert DEFAULT_CONFIG.smart_flow_window_s <= DEFAULT_CONFIG.recent_buys_window_s


def test_the_feeder_still_reads_its_wallet_count_from_the_lane():
    """The feeder must not drift from the lane on HOW MANY wallets, either."""
    assert DEFAULT_CONFIG.smart_flow_min_wallets is None, (
        "None means 'read sm_trenches.min_smart_degen'; a literal here can drift"
    )
