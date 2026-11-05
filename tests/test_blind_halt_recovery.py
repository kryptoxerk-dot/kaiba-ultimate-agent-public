"""A blind-timeout halt must lift when the position can be priced again.

MEASURED on the live box 2026-09-22. A BSC position went unpriceable, correctly tripped
``protection_blind_timeout`` and halted entries agent-wide. A third pricing layer shipped
an hour later and that same position priced in **849 ms**, blind-per-tick fell from an
average of 3.2 to 0.31, and the halt stayed on anyway -- because
``resume_if_reason_starts_with`` was only ever called with the ``protection_overrun:``
prefix. Nothing could lift a ``protection_blind_timeout:`` halt except a human.

This is the same defect, twice: earlier the same day the OVERRUN halt could not lift
either, because recovery demanded consecutive clean ticks while an isolated spike zeroed
the counter. A failsafe that cannot recover is not a failsafe, it is an outage with a
justification.

The recovery is deliberately narrow. It lifts only a halt THIS mechanism set, only for
THIS position, and only on evidence -- a real price for the position the halt names. It
can never lift an operator halt, a daily-loss halt, or a halt raised by a different
position; ``resume_if_reason_starts_with`` matching on the full
``protection_blind_timeout:<position_id>`` string is what makes that safe.
"""

from __future__ import annotations

import pytest

from kaiba.execution import watchdog as W


class _Gate:
    def __init__(self, reason: str | None) -> None:
        self.halted = reason is not None
        self.reason = reason
        self.resumed: list[str] = []

    def resume_if_reason_starts_with(self, prefix, conn=None):
        if self.halted and self.reason and self.reason.startswith(prefix):
            self.halted = False
            self.resumed.append(prefix)
            return True
        return False

    def halt(self, reason, conn=None):
        self.halted, self.reason = True, reason


def test_the_prefix_is_specific_to_one_position():
    """The safety property. A halt for position A must not be lifted by position B."""
    gate = _Gate("protection_blind_timeout:pos_AAA")
    assert gate.resume_if_reason_starts_with("protection_blind_timeout:pos_BBB") is False
    assert gate.halted is True
    assert gate.resume_if_reason_starts_with("protection_blind_timeout:pos_AAA") is True
    assert gate.halted is False


@pytest.mark.parametrize(
    "reason",
    ["operator: stand down", "daily_loss_stop", "protection_overrun:16910ms>5000ms x3"],
)
def test_no_other_halt_is_ever_lifted_by_this(reason):
    """An automatic resume may only ever undo its own mechanism's halt."""
    gate = _Gate(reason)
    assert gate.resume_if_reason_starts_with("protection_blind_timeout:pos_AAA") is False
    assert gate.halted is True


def test_the_watchdog_asks_to_lift_a_blind_halt_when_a_position_prices_again(monkeypatch, tmp_db):
    """THE LIVE BUG: nothing ever called resume with this prefix."""
    import pathlib

    src = pathlib.Path(W.__file__).read_text(encoding="utf-8")
    assert "protection_blind_timeout:" in src
    assert 'protection_blind_timeout:{position.position_id}"' in src, (
        "a blind-timeout halt must have a recovery path; until 2026-09-22 only "
        "protection_overrun: had one and this halt could only be lifted by a human"
    )


def test_recovery_is_driven_by_a_price_not_by_time():
    """The overrun halt recovers on elapsed healthy ticks; this one must not.

    A blind position is blind because nothing can price it. Waiting does not establish
    that it can be priced now -- only a price does. Pinned as a source property because
    the alternative (a tick counter) is the tempting wrong implementation.
    """
    import pathlib

    src = pathlib.Path(W.__file__).read_text(encoding="utf-8")
    i = src.index('protection_blind_timeout:{position.position_id}"')
    window = src[max(0, i - 2000):i]
    assert "protection_restored" in window, (
        "the blind-halt resume must sit in the path a real price takes, so it cannot fire "
        "on elapsed time alone"
    )


def test_recovery_does_not_depend_on_in_memory_blind_state():
    """The bug the first implementation had, and it only showed on the live box.

    Placing the resume inside the blind->priced TRANSITION reads correctly and does not
    work: `blind_since_ms` lives in memory, so a service restart clears it. A position
    that was already healthy when the process came up never transitions, and the halt it
    set -- which IS durable, in the risk state -- outlived every mechanism able to lift it.

    Pinned as a source property: the resume must sit outside the `blind_since_ms is not
    None` branch, so it is attempted on any usable quote.
    """
    import pathlib as _p

    src = _p.Path(W.__file__).read_text(encoding="utf-8").splitlines()
    line = next(i for i, ln in enumerate(src)
                if "protection_blind_timeout:{position.position_id}" in ln)
    # Walk back to the `try:` that owns it and measure its indentation. Inside the
    # `if state.blind_since_ms is not None:` branch it would be 12 spaces; at method
    # level, where it must be, it is 8. Indentation IS the difference between the
    # implementation that worked and the one that only looked right.
    owner = next(i for i in range(line, -1, -1) if src[i].strip() == "try:")
    indent = len(src[owner]) - len(src[owner].lstrip())
    assert indent == 8, (
        f"the blind-halt resume must sit at method level (8 spaces), found {indent}. "
        "Inside the blind->priced transition a restart leaves the halt unliftable, "
        "because blind_since_ms is in memory and the halt is in the database."
    )
