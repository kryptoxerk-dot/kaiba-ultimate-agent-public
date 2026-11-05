"""The strongest cut we have found on our own money, and the duplicate key that hid it.

THE MEASUREMENT, 2026-09-24, over 177 closed LIVE fills. Chosen on the older 88 and
scored on the newer 89 that were never used to choose:

    rule                                older     NEWER
    take everything                    -17.2%     -9.9%   (sum -8.84)
    holders >= 200                      +4.0%     +2.5%   (sum +0.72)
    holders >= 200, not pump/longxyz    -1.0%    +14.0%   (sum +2.95)

and the floor is MONOTONIC on the holdout, which is what makes it an effect rather than a
bucket that happened to win:

    floor    50   100   200   300   500  1000
    NEWER  -9.5% -4.9% +2.5% +5.6% +6.6% +8.1%

200 is taken over 500 because the higher floors keep too few fills for this to remain a
lane. An UNKNOWN holder count refuses exactly as an unknown liquidity does: holder_count
is carried by 54% of the tokens we bought, so this roughly halves the book on purpose.

THE BUG IT UNCOVERED. ``DEFAULT_PARAMS`` contained TWO ``Lane.SM_TRENCHES`` entries in one
dict literal. Python keeps the later one, so ``min_smart_degen``, ``min_independent_entities``,
``max_rug_ratio``, ``window_s`` and ``min_buy_usd`` were silently discarded -- the shipped
default was only ``manual_min_smart_degen``, ``min_liquidity_usd`` and ``require_launchpad``.
Nothing failed, because config/risk.yaml supplies them, which is precisely what made it
invisible; and ``sm_trenches`` reads ``p["min_smart_degen"]`` by direct index, so a
risk.yaml that ever lost the key would have raised instead of falling back.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Lane
from kaiba.execution.lanes import DEFAULT_PARAMS


# ------------------------------------------------------------------ the duplicate


def test_the_defaults_carry_every_key_the_lane_reads():
    """THE REGRESSION: five of these were discarded by a duplicate dict key."""
    p = DEFAULT_PARAMS[Lane.SM_TRENCHES]
    for key in ("min_smart_degen", "min_independent_entities", "max_rug_ratio",
                "window_s", "min_buy_usd", "min_liquidity_usd", "require_launchpad",
                "manual_min_smart_degen"):
        assert key in p, f"{key} is missing from the shipped defaults"


def test_no_lane_is_declared_twice_in_the_defaults():
    """A duplicate key is silent in Python and cost us an edit that did nothing.

    Counting the source is the only way to see it: by the time the dict exists the
    earlier entry is already gone.
    """
    import pathlib

    import kaiba.execution.lanes as L

    src = pathlib.Path(L.__file__).read_text(encoding="utf-8", errors="replace")
    body = src.split("DEFAULT_PARAMS", 1)[1]
    body = body.split("\n}\n", 1)[0]
    for lane in Lane:
        n = body.count(f"Lane.{lane.name}: {{")
        assert n <= 1, f"Lane.{lane.name} is declared {n} times; Python keeps only the last"


# ------------------------------------------------------------------ the holder floor


def shipped_params() -> dict:
    """The sm-trenches params as config/risk.yaml actually ships them."""
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "risk.yaml").read_text(encoding="utf-8"))
    return cfg["lanes"]["sm-trenches"]["params"]


def test_the_default_is_off_so_it_cannot_become_a_hidden_kill_switch():
    """0 in code, 200 in the file. A lane default that refused every token whose holder
    count we never read would be a kill switch disguised as a filter."""
    assert DEFAULT_PARAMS[Lane.SM_TRENCHES]["min_holder_count"] == 0


def test_the_floor_is_off_in_sm_trenches_and_the_reason_is_recorded():
    """SET TO 0 hours after it shipped. The edge is real; the population is wrong.

    The holdout above measured tokens we BOUGHT -- already selected, already established.
    MEASURED against what the scanner actually sees: of 11,824 tokens first seen in six
    hours, 85.9% carry NO holder_count and only 2.0% would clear 200. sm-trenches is an
    early-entry lane, and a token minutes old does not have 200 holders yet, so the floor
    kept 2% of the book. Live proof while it was on: sm-trenches signals went 3, 3, 1, 1,
    0, 0 per 15 min and tokens graded B logged `lanes=none`.

    A filter that keeps 2% is a kill switch, not an edge -- which is the rule this very
    file states, and it was shipped anyway without checking the SCAN population. Pinned
    here so the next person reads the correction next to the measurement.
    """
    p = shipped_params()
    assert p["min_holder_count"] == 0, (
        f"the floor is {p['min_holder_count']}; it keeps ~2% of scanned tokens and stops "
        "sm-trenches firing. Apply holder count as a sizing input or on a later-entry "
        "lane, not as an entry gate here."
    )


def test_the_measurement_behind_the_floor_is_still_written_down():
    """Turning it off must not erase why it was turned on; a later lane may want it."""
    import pathlib as _p

    src = _p.Path(__file__).read_text(encoding="utf-8")
    assert "+14.0%" in src and "monotonic" in src.lower(), (
        "the holdout result was deleted along with the floor"
    )


def test_the_blocklist_is_empty_by_the_owners_decision():
    """OWNER 2026-09-24: "Dont block long xyz launches / Trade every launchpads".

    Recorded as a DECISION, because the measurements point the other way: pump.fun
    -28.1% over 39 live fills (sum -10.9, the biggest single bleed, win rate 15%) and
    longxyz -29.8% over 13 with a win rate of ZERO.

    The counterweight, and the reason this was the owner's call rather than a cut: of
    2,151 tokens scanned on sol in two hours, 87.5% were pump.fun. Blocking it does not
    filter that chain, it turns it off. The gate stays wired so it can be refilled from
    config without a deploy.
    """
    assert shipped_params()["blocked_launchpads"] == []


def test_the_per_launchpad_measurement_is_not_lost_with_the_blocklist():
    """An empty gate must not erase why it existed."""
    import pathlib as _p

    src = _p.Path(__file__).read_text(encoding="utf-8")
    assert "-28.1%" in src and "longxyz" in src


@pytest.mark.parametrize("lp", ["pons", "ray_launchpad"])
def test_the_profitable_launchpads_are_never_blocked(lp):
    """pons +16.4% (n13) and ray_launchpad +15.5% (n8) are the only positive cells."""
    blocked = {str(x).lower() for x in shipped_params()["blocked_launchpads"]}
    assert lp not in blocked


# ------------------------------------------------------------------ the gate is wired


def test_the_gate_reads_the_param_and_refuses_the_unknown():
    """Pinned on the source: an absent holder count must refuse, not pass.

    This is the same rule liquidity already follows -- "we could not read it" has never
    been evidence that it is fine -- and it is the clause that carries the result, since
    46% of the tokens we bought carry no holder_count at all.
    """
    import inspect

    from kaiba.execution import lanes

    src = inspect.getsource(lanes.sm_trenches)
    assert "min_holder_count" in src, "the param is declared but never read"
    assert 'holders = _measure(ctx.dossier, "holder_count")' in src
    assert "if holders is None or holders < min_holders:" in src, (
        "an unknown holder count does not refuse"
    )
    assert "blocked_launchpads" in src, "the blocklist is declared but never read"
