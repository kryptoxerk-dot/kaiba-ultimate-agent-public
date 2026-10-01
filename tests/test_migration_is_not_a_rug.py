"""A launchpad token that bonds is not a token that rugged.

THE BUG, found by the operator on 2026-09-23 and confirmed on the live box.

When a launchpad token graduates, its bonding curve is drained to fund the DEX pool. That
is the design, and it reads on any liquidity feed as a large, sudden drop in the venue we
were watching. The rug monitor saw the drop, called it a rug and sold the whole position --
at the single most bullish moment in a launchpad token's life.

MEASURED over every rug-reason exit on the box: **33 of 45 (73%) are on tokens that carry a
migration record**, most of them timestamped within minutes of our exit. The signature is
unmistakable once you look at what we got for the sale:

    genuine rugs      exited at -70%, -82%, -85%, -90%, -95%, -97%, -98%
    migrations        exited at +43%, +51%, +51%, +5%, +2%, +1%

A drained pool cannot fill a sell at +43%. $DOAI (Pons, robinhood) is the worked example:
entered 09:28:52, exited 135 seconds later on ``rug:lp_-100.0pct`` for +42.7% while the
price was up 90%, and the token then ran to **7.87x our entry**. 4 of the 28 rug-exits with
tape afterwards went on to 2x or better.

THE FIX. A liquidity drop inside ``rug_migration_grace_s`` of a known migration is not a
rug and does not exit. It is safe to hold precisely because the price rules are untouched:
if a token really does die after migrating, the trailing stop (armed from +20% since
2026-09-23) or the hard stop or the emergency exit all still fire on PRICE, which is the
evidence a drained pool cannot fake.

What this does NOT do is widen the rug monitor for anything else. No migration record, or a
stale one, and the drop is still a rug.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.execution.protection import (
    ProtectionConfig,
    ProtectionState,
    evaluate,
)

NOW = 1_790_000_000_000


def cfg(**kw) -> ProtectionConfig:
    base = {"stop_loss_bps": 4000, "rug_liquidity_drop_pct": Decimal(40),
            "trailing": [(Decimal("1.2"), 2000), (Decimal("2.0"), 3000)]}
    base.update(kw)
    return ProtectionConfig(**base)


def state(entry="100", peak=None) -> ProtectionState:
    return ProtectionState(
        position_id="pos_x",
        entry_price=Decimal(entry),
        peak_price=Decimal(peak if peak is not None else entry),
    )


def drop(**kw):
    """A 100% liquidity drop, with the price still healthy."""
    args = {
        "price_usd": Decimal("190"),        # +90%, as $DOAI was
        "liquidity_usd": Decimal(0),
        "prev_liquidity_usd": Decimal("50000"),
        "now_ms": NOW,
        "cfg": cfg(),
    }
    args.update(kw)
    return args


# ------------------------------------------------------------------ the bug


def test_a_drained_curve_at_migration_is_not_a_rug():
    """THE REGRESSION: $DOAI sold on rug:lp_-100.0pct and ran to 7.87x."""
    action = evaluate(state(peak="190"), migrated_ms=NOW - 30_000, **drop())
    assert action.kind.value != "exit_all", (
        "a migrating token is still being sold as a rug: %s" % action.reason
    )
    assert "migration" in action.reason


def test_the_same_drop_without_a_migration_is_still_a_rug():
    """The fix must not widen the monitor for anything else."""
    action = evaluate(state(peak="190"), migrated_ms=None, **drop())
    assert action.kind.value == "exit_all"
    assert action.reason.startswith("rug:lp_-")


def test_a_stale_migration_does_not_excuse_a_later_rug():
    """A token that migrated yesterday and drains today HAS rugged."""
    grace = cfg().rug_migration_grace_s
    action = evaluate(state(peak="190"), migrated_ms=NOW - (grace * 1000) - 60_000, **drop())
    assert action.kind.value == "exit_all"
    assert action.reason.startswith("rug:lp_-")


@pytest.mark.parametrize("age_s", [0, 60, 600])
def test_a_recent_migration_is_inside_the_grace_window(age_s):
    action = evaluate(state(peak="190"), migrated_ms=NOW - age_s * 1000, **drop())
    assert action.kind.value != "exit_all"


def test_a_migration_in_the_future_is_not_trusted():
    """A clock skew or a bad feed timestamp must not become a permanent rug waiver."""
    action = evaluate(state(peak="190"), migrated_ms=NOW + 3_600_000, **drop())
    assert action.kind.value == "exit_all"


# ------------------------------------------------------------------ still protected


def test_price_rules_still_fire_while_migrating():
    """THE SAFETY ARGUMENT: we hold through migration because PRICE still protects us.

    A token that genuinely dies after bonding collapses in price, and a drained pool cannot
    fake a price. The emergency exit must still fire on the same tick.
    """
    action = evaluate(
        state(peak="190"),
        migrated_ms=NOW - 30_000,
        **drop(price_usd=Decimal("40")),     # -60%, past emergency_loss_bps
    )
    assert action.kind.value == "exit_all"
    assert action.reason == "emergency_loss"


def test_the_trailing_stop_still_fires_while_migrating():
    s = state(peak="190")
    evaluate(s, migrated_ms=NOW - 30_000, **drop())      # arms the trail off the 1.9x peak
    assert s.stop_price is not None and s.stop_price > Decimal("100")
    action = evaluate(s, migrated_ms=NOW - 30_000, **drop(price_usd=Decimal("120")))
    # SELLS, rather than exit_all specifically. Since 2026-09-23 a profitable trailing stop
    # keeps a moon bag (`moonbag_retain_pct`), so this fires as an 80% TRIM. The point of
    # this test is that the price rules still act while a migration waives the rug monitor,
    # and a trim acts.
    assert action.kind.value in ("exit_all", "trim"), action.reason
    assert "trailing_stop" in action.reason or action.reason == "stop_loss"


def test_the_hard_stop_still_fires_while_migrating():
    action = evaluate(
        state(), migrated_ms=NOW - 30_000, **drop(price_usd=Decimal("55")),
    )
    assert action.kind.value == "exit_all"


# ------------------------------------------------------------------ config


def test_the_grace_window_is_configured_and_sane():
    c = cfg()
    assert c.rug_migration_grace_s > 0
    assert c.rug_migration_grace_s <= 7200, (
        "a grace window over two hours stops being about migration and becomes a hole in "
        "the rug monitor"
    )


@pytest.mark.parametrize("grace", [0, -1])
@pytest.mark.parametrize("age_ms", [0, 30_000])
def test_the_waiver_can_be_switched_off(grace, age_ms):
    """Zero disables it, so the old behaviour can be restored without a deploy.

    ``age_ms=0`` is the case that matters and the one a mutation survived: with a
    zero-width window every OTHER age is refused by the arithmetic anyway, so only a
    migration timestamped exactly now can tell ``grace <= 0`` apart from ``0 <= age <= 0``.
    """
    action = evaluate(
        state(peak="190"), migrated_ms=NOW - age_ms,
        **drop(cfg=cfg(rug_migration_grace_s=grace)),
    )
    assert action.kind.value == "exit_all", (
        f"grace={grace} did not switch the waiver off at age {age_ms}ms"
    )
