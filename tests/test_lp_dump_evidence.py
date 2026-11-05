from __future__ import annotations

import pytest

from kaiba.execution.lp_model import dump_setup
from kaiba.learning.lp_dump_evidence import HOUR_S, assess

START = 500_000 * HOUR_S


def candle(index, opening, high, low, close, volume):
    return {"timestamp": START + index * HOUR_S, "open": opening, "high": high,
            "low": low, "close": close, "volume": volume}


def setup():
    return [candle(0, 1.1, 1.3, 1, 1.2, 10), candle(1, 1.2, 2.1, 1.2, 2, 20),
            candle(2, 2, 3, 1.9, 2.9, 40), candle(3, 2.9, 5, 2.8, 4, 100),
            candle(4, 4, 4, 2.4, 2.7, 65), candle(5, 2.7, 2.8, 2, 2.1, 20),
            candle(6, 2.1, 2.2, 1.9, 2, 10)]


def test_completed_contiguous_hours_with_earlier_low_can_qualify_in_reverse_input_order():
    rows = setup()
    result = assess(list(reversed(rows)), as_of_s=START + 7 * HOUR_S)
    assert result["status"] == "paper_candidate"
    assert result["prior_bar_run_x"] == 5
    assert result["closed_candles"] == 7
    assert result["volume_basis"] == "total_ohlcv_volume"
    assert result["financial_execution"] is False


def test_developing_hour_cannot_create_drop_or_volume_fade():
    rows = setup()[:5] + [candle(5, 2.7, 3.1, 2.7, 3.0, 20),
                           candle(6, 3.0, 3.1, 2.9, 3.0, 10), candle(7, 3.0, 3.1, 1.7, 1.8, 1)]
    assert dump_setup(rows)[0] is None
    result = assess(rows, as_of_s=START + 8 * HOUR_S - 1)
    assert (result["status"], result["reason"]) == ("not_candidate", "setup_drop_out_of_band")
    assert (result["closed_candles"], result["incomplete_candles"]) == (7, 1)
    assert assess(rows, as_of_s=START + 8 * HOUR_S)["status"] == "paper_candidate"


def test_peak_candle_low_does_not_prove_the_run_preceded_the_peak():
    rows = [candle(i, 2.1, 2.4, 2, 2.2, 15) for i in range(3)] + setup()[3:]
    rows[3]["low"] = 1.0
    assert dump_setup(rows)[0] is None
    result = assess(rows, as_of_s=START + 7 * HOUR_S)
    assert (result["status"], result["reason"]) == ("evidence_unavailable", "pre_peak_run_unproved")


def test_missing_or_stale_closed_hours_withhold_the_setup():
    rows = setup()
    for sample, clock in ((rows[:1] + rows[2:], START + 7 * HOUR_S), (rows, START + 8 * HOUR_S)):
        assert assess(sample, as_of_s=clock)["reason"] == "closed_history_gap_or_stale"
    assert assess(rows[:5], as_of_s=START + 5 * HOUR_S)["reason"] == "closed_history_short"


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", -1])
def test_invalid_ohlcv_fails_closed(bad):
    rows = setup()
    rows[3]["volume"] = bad
    assert assess(rows, as_of_s=START + 7 * HOUR_S)["reason"] == "hourly_values_invalid"


def test_duplicate_future_and_non_hourly_timestamps_are_rejected():
    rows = setup()
    clock = START + 7 * HOUR_S
    for extra in (rows[1], candle(8, 2, 2, 2, 2, 0), {**rows[0], "timestamp": START + 60}):
        assert assess(rows + [extra], as_of_s=clock)["reason"] == "hourly_clock_invalid_or_duplicate"
