"""The trial registry: the denominator the deflated Sharpe ratio divides by.

Get this number wrong and the statistic is worthless in the flattering direction, which
is the direction nobody notices. These tests exist because the previous count read only
formally-declared experiments, so hand-tuning a threshold twenty times and filing two
experiments deflated as though two attempts had been made.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Lane
from kaiba.learning import registry


def test_a_configuration_is_recorded_the_first_time_it_runs(tmp_db):
    registry.register(Lane.CONFLUENCE_5, {"min_entities": 5}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 1


def test_the_same_configuration_twice_is_one_trial(tmp_db):
    for _ in range(5):
        registry.register(Lane.CONFLUENCE_5, {"min_entities": 5}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 1
    assert registry.trials(Lane.CONFLUENCE_5, tmp_db)[0]["decisions"] == 5


def test_nudging_one_threshold_is_a_new_trial(tmp_db):
    """This is the case the old count missed entirely."""
    for n in (3, 4, 5, 6, 7):
        registry.register(Lane.CONFLUENCE_5, {"min_entities": n}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 5


def test_a_retired_configuration_still_counts(tmp_db):
    """It was still an attempt. The table is append-only for exactly this reason."""
    registry.register(Lane.CONFLUENCE_5, {"min_entities": 3}, tmp_db)
    registry.register(Lane.CONFLUENCE_5, {"min_entities": 9}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 2


def test_lanes_are_counted_separately(tmp_db):
    registry.register(Lane.CONFLUENCE_5, {"a": 1}, tmp_db)
    registry.register(Lane.KOL_FADE, {"a": 1}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 1
    assert registry.trial_count(Lane.KOL_FADE, tmp_db) == 1


def test_key_order_does_not_invent_a_trial(tmp_db):
    registry.register(Lane.CONFLUENCE_5, {"a": 1, "b": 2}, tmp_db)
    registry.register(Lane.CONFLUENCE_5, {"b": 2, "a": 1}, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 1


def test_registration_never_raises_into_the_decision_path(tmp_db, monkeypatch):
    """A measurement failure must not cost us a decision."""

    class Broken:
        def execute(self, *a, **k):
            raise RuntimeError("disk is on fire")

    registry.register(Lane.CONFLUENCE_5, {"a": 1}, Broken())  # type: ignore[arg-type]


def test_deciding_registers_the_configuration_it_ran_under(tmp_db):
    """The point of the whole module: nobody has to remember to file anything."""
    from kaiba.core.schemas import Chain
    from kaiba.execution.engine import submit_agent_intent

    assert registry.trial_count(Lane.MANUAL, tmp_db) == 0
    submit_agent_intent(Chain.SOL, "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb",
                        thesis="t", conn=tmp_db)
    assert registry.trial_count(Lane.MANUAL, tmp_db) == 1


def test_the_gate_uses_the_larger_of_declared_and_run(tmp_db):
    """Each count misses a different thing, so the honest answer is the maximum."""
    from kaiba.learning.gates import _trial_count

    for n in (1, 2, 3, 4):
        registry.register(Lane.CONFLUENCE_5, {"min_entities": n}, tmp_db)
    assert _trial_count(tmp_db, Lane.CONFLUENCE_5.value) == 4


@pytest.mark.parametrize("bad", [None, {}])
def test_an_empty_configuration_is_still_a_trial(tmp_db, bad):
    registry.register(Lane.CONFLUENCE_5, bad, tmp_db)
    assert registry.trial_count(Lane.CONFLUENCE_5, tmp_db) == 1
