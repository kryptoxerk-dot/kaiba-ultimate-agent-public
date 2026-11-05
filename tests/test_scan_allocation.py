"""Scan capacity goes to the lanes that can act on it.

MEASURED 2026-09-22 over two hours: 68 of 186 signals (37%) were produced for
``migration-fade``, which was in SHADOW and could not act on a single one, while
``smart_flow_share`` reserved half of every batch for the sources that feed it.
``sm-trenches`` was the only live lane, and smart flow is the only source that can
produce an ``sm-trenches`` candidate.

WHY THIS AND NOT MORE WORKERS. Raising scanner concurrency the same day lifted dossier
throughput 1.8x and drove the ``robinhood-rpc`` credit bucket from -2,283 to -19,699,
which broke the Pons curve read and left two live positions with no evaluable stop.
Reallocating a fixed batch cannot do that: the same number of tokens is scanned against
the same providers. Frequency bought this way costs no provider budget at all.
"""

from __future__ import annotations

from kaiba.execution.scanner import DEFAULT_CONFIG, ScanConfig


def test_smart_flow_gets_the_majority_of_a_batch():
    assert DEFAULT_CONFIG.smart_flow_share >= 0.8


def test_the_launch_sources_are_not_starved():
    """The shadow lanes are how the learning studies get their sample. A lane with no
    candidates stops producing the evidence that would promote it."""
    assert DEFAULT_CONFIG.smart_flow_share <= 0.85
    assert (1 - DEFAULT_CONFIG.smart_flow_share) * DEFAULT_CONFIG.batch >= 1


def test_a_batch_still_reserves_at_least_one_slot_for_each_side():
    cfg = ScanConfig()
    smart = max(1, int(cfg.batch * cfg.smart_flow_share))
    assert 1 <= smart < cfg.batch, (smart, cfg.batch)


def test_smart_flow_is_on():
    """Off, sm-trenches -- the only live lane -- can never see a candidate at all."""
    assert DEFAULT_CONFIG.smart_flow is True
