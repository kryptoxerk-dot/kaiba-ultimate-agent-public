"""The dev-supply policy the operator asked for: 10-30% is bought carefully, not refused.

His words, twice on 2026-09-22: *"I am okay with trading live pair tokens that went live
1 second / But we need to be able to distinct bundled launch and not / **If its bundled
dev buying more than 20% 30% we can still buy but we need to be careful**"*.

That is one instruction with two halves, and shipping either half alone is a bug:

* **"we can still buy"** -- ``dyor.DEV_PCT_BLOCK`` was 10 and the ``dev_concentration``
  rule was a ``BLOCKER``, so every token whose creator held 10-30% was a hard veto. It is
  30 now. PLAN §5.5's 10% is not deleted: it survives as ``DEV_PCT_WARN`` and the
  ``dev_concentration_review`` WARNING, which is exactly the shape §5.5 already gives
  clusters (>20% review, >30% reject).
* **"we need to be careful"** -- and this is the half that is easy to drop silently.
  ``dev_pct`` fed **nothing** in the sizer: ``_concentration_multiplier`` reads the launch
  **wave** (``token_bundles`` / ``launch_concentration``), which by construction *excludes*
  the creator's own buy. So raising the veto alone would have bought every 10-30% dev
  token at FULL size with no penalty at all. ``DEV_SUPPLY_LADDER`` is the other half.

MEASURED ON THE LIVE BOX 2026-09-22 -- the 546 all-time decisions the 10% veto refused,
joined to each token's stored ``dev_pct``:

     49   9.0%  10-20%      <- admitted, at 0.9x
     29   5.3%  20-30%      <- admitted, at 0.85x
    416  76.2%  >30%        <- still vetoed
     24   4.4%  79.0-79.7%  <- still vetoed
     28   5.1%  <=10%       <- fired for another reason, or the dossier moved since

78 of 546 (14.3%) are admitted at a haircut and 440 (80.6%) stay refused, out of a
category that was ~36% of every refusal the agent made. Across all 9,809 dossiers that
carry a ``dev_pct``: p50 0.35%, p90 13.79%, 58.1% under 1%, so the first rung bites at
roughly the top decile rather than on everything.

These numbers were first taken from ``data/kaiba.db``, the LOCAL scratch copy, which said
6.3% admitted and -- worse -- that 78.8% of refusals sat in 79.0-79.7%, i.e. that the
pump.fun curve's own 79.31% of supply was being read as the creator's balance. On the box
the money is on that band is 4.4% of refusals and 0.2% of dossiers. The local corpus was
not a sample of production; the correction is kept here because the mistake is the
reusable part.

Four properties are pinned because they are the ones a later edit breaks by accident:

1. a dev haircut may only ever make a position **smaller** (the invariant
   ``tests/test_concentration_sizing.py`` already pins for the wave arm);
2. 25% dev must size **strictly smaller** than 2% dev -- the whole point of "careful";
3. above the veto it still **refuses entirely**; and
4. an **unmeasured** ``dev_pct`` is not a clean one: it is worth 1.0 like
   ``CONCENTRATION_UNKNOWN`` (charging for ignorance was measured doing harm) but it is
   never *recorded* as a clean reading.

SUPERSEDED SPEC. Two assertions in ``tests/test_dyor.py`` encoded PLAN §5.5's 10% veto and
could not hold alongside the operator's later instruction: dev_pct=11 had to be a blocker,
and dev_pct=24 had to be in the expected blocker set of the risky-token scan. They were
not worked around and the policy was not bent to keep them green -- the operator moved them
himself on 2026-09-22, so ``test_dev_concentration_rejects_above_thirty_percent_and_warns_below``
now asserts the 30% line and the 25% warning. Recorded here because a threshold that moves
and a test that moves with it should both be able to say why.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from kaiba.core.schemas import Chain, TokenRisk
from kaiba.execution import risk as risk_module
from kaiba.execution.risk import (
    CONCENTRATION_UNKNOWN,
    DEV_SUPPLY_ABOVE_LADDER,
    DEV_SUPPLY_LADDER,
    DEV_SUPPLY_UNKNOWN,
    RiskGate,
    _concentration_multiplier,
    _dev_supply_multiplier,
    _dev_supply_policy,
)
from kaiba.intelligence import dyor
from tests.test_concentration_sizing import BANKROLL, FULL, GIVE, SEEDED_FLOOR, store_wave
from tests.test_dyor import SAFE_BASE, resolution_of
from tests.test_risk import (  # noqa: F401 - fixtures used by name
    LANE,
    SMALL_LANE,
    SOL,
    TOKEN,
    seed_depth,
    write_risk,
)

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


@pytest.fixture
def sized(write_risk, tmp_db):  # noqa: F811 - the imported fixture, requested by name
    """The same 20 SOL budget and priced pool ``tests/test_concentration_sizing`` uses.

    Restated here rather than imported: importing a fixture makes every test signature a
    redefinition of it, and the bankroll matters. At 20 SOL a 0.85x still clears the
    seeded pool's floor, so these tests measure the multiplier's arithmetic instead of
    the pool refusing for its own reasons -- which is the live box's problem and is
    recorded in ``DEV_SUPPLY_LADDER``, not something to hide inside a fixture.
    """

    def _setup(**overrides):
        chains = {"sol": {"bankroll_base_units": BANKROLL, "max_position_base_units": 2_000_000_000}}
        for key, value in (overrides.pop("chains", {}) or {}).items():
            chains.setdefault(key, {}).update(value)
        path = write_risk(chains=chains, **overrides)
        for token in (TOKEN, GIVE):
            seed_depth(tmp_db, token)
        tmp_db.commit()
        return path

    return _setup


def store_dev(
    conn,
    pct: str | None,
    token: str = TOKEN,
    *,
    chain: str = "sol",
    basis: str = "provider_reported",
    grade: str = "B",
) -> None:
    """Write a ``token_dossiers`` row carrying a ``dev_pct`` measure, as ``dyor`` does.

    ``pct=None`` writes the UNAVAILABLE measure ``Measure.unknown()`` serialises to, which
    is the shape a dossier has when no provider could answer -- not a zero.
    """
    measure = (
        {"value": None, "basis": "unavailable", "receipt": None, "freshness_budget_s": 3600}
        if pct is None
        else {
            "value": pct,
            "basis": basis,
            "receipt": {
                "provider": "rugcheck",
                "endpoint": "report",
                "observed_at_ms": 1,
                "basis": basis,
            },
            "freshness_budget_s": 3600,
        }
    )
    body = {"address": token, "chain": chain, "built_at_ms": 1, "grade": grade, "dev_pct": measure}
    conn.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, score, grade, "
        "blockers_json, warnings_json, unknowns_json, dossier_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain, token, 1, None, grade, "[]", "[]", "[]", json.dumps(body)),
    )
    conn.commit()


def blockers_at(dev_pct: Decimal) -> list[TokenRisk]:
    return dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": dev_pct})).blockers


def warnings_at(dev_pct: Decimal) -> list[TokenRisk]:
    return dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": dev_pct})).warnings


# --------------------------------------------------------------------------------------
# 1. the veto moves to 30 and the 10% line becomes a review
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dev_pct,blocked",
    [
        ("0", False),
        ("9", False),
        ("11", False),  # PLAN §5.5 refused this; the operator's later word buys it
        ("23.4195", False),  # a real sol dev_pct the 10% veto refused (local corpus)
        ("25.4233", False),
        ("29.999", False),
        ("30", False),
        ("30.001", False),  # admitted 2026-09-23: this band measured BEST (see dyor)
        ("34", False),
        ("49.999", False),
        ("50", False),  # "more than ... 50%" is strictly more
        ("50.001", True),
        ("79.3328", True),  # the median of the 448 refusals, still refused
        ("100", True),
    ],
)
def test_the_veto_line_is_fifty_not_thirty_and_not_ten(dev_pct, blocked):
    assert (TokenRisk.DEV_CONCENTRATION in blockers_at(Decimal(dev_pct))) is blocked


def test_the_ten_percent_line_survives_as_a_review_rather_than_being_deleted():
    """PLAN §5.5's number is not thrown away: it stops vetoing and starts downgrading.

    Same shape §5.5 already gives clusters -- >20% review, >30% reject -- so the creator
    and one unexplained cluster are now judged on one consistent scale.
    """
    assert dyor.DEV_PCT_WARN == Decimal(10)
    assert TokenRisk.DEV_CONCENTRATION not in warnings_at(Decimal("9.9"))
    assert TokenRisk.DEV_CONCENTRATION in warnings_at(Decimal("10.1"))
    assert TokenRisk.DEV_CONCENTRATION in warnings_at(Decimal("25"))
    assert TokenRisk.DEV_CONCENTRATION not in blockers_at(Decimal("25"))


def test_a_rejected_dev_is_not_also_filed_as_a_review():
    """One problem, one finding -- the rule ``evaluate`` already keeps for clusters."""
    assert TokenRisk.DEV_CONCENTRATION in blockers_at(Decimal("60"))
    assert TokenRisk.DEV_CONCENTRATION not in warnings_at(Decimal("60"))


def test_the_band_is_still_condemned_enough_to_lose_its_A():
    """"Careful" has to be visible in the grade too, not only in the size."""
    clean = dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": Decimal(1)}))
    careful = dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": Decimal(25)}))
    assert careful.tradeable, "the operator said we can still buy in this band"
    assert careful.score < clean.score, "a quarter of supply in dev hands must cost score"
    assert TokenRisk.DEV_CONCENTRATION in careful.warnings


def test_the_dev_rules_still_carry_their_reasons_and_say_what_is_invented():
    block = next(r for r in dyor.RULES if r.name == "dev_concentration")
    review = next(r for r in dyor.RULES if r.name == "dev_concentration_review")
    assert block.severity is dyor.Severity.BLOCKER
    assert review.severity is dyor.Severity.WARNING
    for rule in (block, review):
        assert len(rule.reason) > 40 and rule.reason == rule.reason.strip()
    assert "INVENTED" in review.reason


def test_the_superseded_mandate_is_recorded_not_erased():
    """A threshold that moved must say what it was, who moved it and when."""
    source = Path(dyor.__file__).read_text(encoding="utf-8")
    section = source[
        source.index("# dev-attributable supply") : source.index("CLUSTER_PCT_BLOCK = Decimal")
    ]
    for phrase in ("PLAN §5.5", "SUPERSEDED", "2026-09-22", "INVENTED", "MEASURED", "would settle"):
        assert phrase in section, phrase
    assert "DEV_PCT_WARN = Decimal(10)" in section, "the superseded number must still exist"


def test_the_confluence_mirror_moved_with_the_veto_but_the_scorer_did_not():
    """``test_confluence`` pins that confluence mirrors dyor. The scorer keeps 10%.

    The mirror is about the veto line; the ``dev_supply_threshold`` variable is about
    where a dev holding starts costing score, which is still 10% -- that IS "careful".
    Repointing the scorer at 30 would have widened the score silently on the same commit
    that widened the veto.
    """
    from kaiba.intelligence import confluence as cf

    assert float(dyor.DEV_PCT_BLOCK) == cf.DYOR_DEV_PCT_BLOCK == 50.0
    assert float(dyor.DEV_PCT_WARN) == cf.DYOR_DEV_PCT_WARN == 10.0
    spec = cf.LITERATURE_V1.by_name()["dev_supply_threshold"]
    assert spec.params["limit"] == 10.0
    assert "INVENTED" in spec.provenance


# --------------------------------------------------------------------------------------
# 2. the band costs size -- the half that is easy to drop
# --------------------------------------------------------------------------------------


def test_a_quarter_of_supply_in_dev_hands_sizes_strictly_smaller_than_two_percent(sized, tmp_db):
    """The requirement, stated as the operator stated it: buy it, but buy less of it."""
    sized()
    store_dev(tmp_db, "2")
    clean = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    store_dev(tmp_db, "25")
    careful = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    assert clean == FULL
    assert 0 < careful < clean, (careful, clean)
    assert careful == int(Decimal(FULL) * Decimal("0.85"))


@pytest.mark.parametrize(
    "dev_pct,multiplier",
    [
        ("0", "1.0"),
        ("2", "1.0"),
        ("9.999", "1.0"),
        ("10", "0.9"),  # PLAN §5.5's old veto line: now the first rung that costs
        ("13.6874", "0.9"),  # a real refused dev_pct, 2026-09-22 (local corpus)
        ("19.999", "0.9"),
        ("20", "0.85"),  # the operator's own first number
        ("25.4233", "0.85"),  # a real refused dev_pct, 2026-09-22 (local corpus)
        ("29.999", "0.85"),
        ("30", "0.85"),  # 30-50 admitted 2026-09-23 with no extra cut beyond this rung
        ("49.999", "0.85"),
        ("50", "0.1"),  # at the veto line the sizer is the tighter of the two
        ("60", "0.1"),
    ],
)
def test_each_dev_band_scales_the_size(sized, tmp_db, dev_pct, multiplier):
    sized()
    store_dev(tmp_db, dev_pct)
    expected = int(Decimal(FULL) * Decimal(multiplier))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == expected


def test_the_dev_multiplier_never_increases_a_size(sized, tmp_db):
    """The invariant, as a property rather than an example. It may only ever shrink."""
    sized()
    store_dev(tmp_db, "0")
    clean = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    assert clean == FULL
    for dev in ("0", "1", "9.9", "10", "15", "20", "25", "29.9", "30", "50", "99.9", "100", "250"):
        store_dev(tmp_db, dev)
        size = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
        assert size <= clean, dev
        assert _dev_supply_multiplier(SOL, TOKEN, tmp_db)[0] <= Decimal(1), dev


def test_the_dev_ladder_is_monotone_and_can_never_reward_a_bigger_dev():
    thresholds = [t for t, _ in DEV_SUPPLY_LADDER]
    multipliers = [m for _, m in DEV_SUPPLY_LADDER]
    assert thresholds == sorted(set(thresholds))
    assert multipliers == sorted(multipliers, reverse=True)
    assert all(Decimal(0) <= m <= Decimal(1) for m in [*multipliers, DEV_SUPPLY_ABOVE_LADDER])
    assert DEV_SUPPLY_ABOVE_LADDER <= multipliers[-1]
    assert DEV_SUPPLY_UNKNOWN == CONCENTRATION_UNKNOWN == Decimal("1.0")
    assert all(m <= DEV_SUPPLY_UNKNOWN for _, m in DEV_SUPPLY_LADDER)


def test_the_sizers_deepest_band_edge_is_the_veto_line_itself():
    """Anti-drift. Two files own one boundary; if one moves, this fails rather than a fill.

    ``docs/PLAN.md``: declared is not enforced. The veto lives in ``dyor`` and the
    backstop lives here, so they are pinned to each other rather than to a literal.
    """
    assert DEV_SUPPLY_LADDER[-1][0] == dyor.DEV_PCT_BLOCK == Decimal(50)
    assert DEV_SUPPLY_LADDER[0][0] == dyor.DEV_PCT_WARN == Decimal(10)


def test_above_the_veto_it_still_refuses_entirely(sized, tmp_db):
    """Both arms: ``dyor`` condemns it, and the sizer would not fund it either.

    The dossier blocker is the real veto -- ``engine.decide`` never reaches a size for a
    token carrying one. The sizer's above-ladder rung is the backstop for the day somebody
    retunes the rule: at the live sol bankroll (every entry sized 1.02-1.79x its own pool
    floor) a 0.1x is a refusal, and it refuses NAMING the dev supply rather than silently.
    """
    dossier = dyor.build_dossier("X", Chain.SOL, resolution_of({**SAFE_BASE, "dev_pct": Decimal("51")}))
    assert TokenRisk.DEV_CONCENTRATION in dossier.blockers
    assert not dossier.tradeable

    sized(chains={"sol": {"min_position_base_units": 600_000_000}})
    store_dev(tmp_db, "51")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:concentration:"), reason
    assert "dev:51.000%" in reason and "0.1x" in reason


def test_the_dev_cut_cannot_be_raised_back_by_the_pools_own_floor(sized, tmp_db):
    """The composition bug this mechanism's wave arm was built to avoid, on the dev arm.

    ``_clamp_to_band`` lifts a size up to the pool's economic floor. If the dev cut ran
    before it, a 1% lane's 200,000,000 * 0.85 would be lifted straight back to
    79,368,186 -- and a 25%-dev token would come out the same size as a clean one.
    """
    sized()
    store_dev(tmp_db, "25")
    assert RiskGate().position_size(SOL, SMALL_LANE, 95.0, tmp_db, token=TOKEN) == 170_000_000
    store_dev(tmp_db, "50")
    assert RiskGate().position_size(SOL, SMALL_LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, SMALL_LANE, 0, tmp_db, token=TOKEN).reason
    assert "dev:50.000%" in reason and "viable_floor" in reason and str(SEEDED_FLOOR) in reason


# --------------------------------------------------------------------------------------
# 3. an unmeasured dev_pct is not a clean one
# --------------------------------------------------------------------------------------


def test_an_unmeasured_dev_pct_is_not_charged_but_is_never_recorded_as_clean(sized, tmp_db):
    """Consistent with ``CONCENTRATION_UNKNOWN``, and for the same measured reason.

    1.0 because charging for ignorance was measured disabling a chain (the 0.9 unknown
    landed under the per-token viable floor and refused 4 of 6 live sol candidates), and
    because the concentration outcome study found the sign of concentration -> forward
    return flips between consecutive days. The LABEL is what does the work: an unmeasured
    creator share must be visible as unmeasured, never laundered into a 0%.
    """
    sized()
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == FULL
    multiplier, label = _dev_supply_multiplier(SOL, GIVE, tmp_db)
    assert multiplier == DEV_SUPPLY_UNKNOWN == Decimal("1.0")
    assert label.startswith("dev_unknown:"), label
    assert "no_dossier" in label
    assert "dev:0" not in label, "an unmeasured creator share must never read as 0%"


@pytest.mark.parametrize(
    "pct,fragment",
    [
        (None, "dev_unmeasured"),  # the dossier exists; nobody could answer
        ("-1", "dev_negative"),
        # A NaN would compare False against every rung and land on the above-ladder
        # multiplier by accident. It never gets that far: the model refuses the row, so
        # the whole dossier reads as unknown. ``risk._dossier_dev_pct`` carries a second
        # guard for it anyway, which is unreachable while ``dev_pct`` stays a Decimal.
        ("NaN", "dossier_unparseable"),
        ("not a number", "dossier_unparseable"),  # pydantic refuses the row entirely
    ],
)
def test_every_shape_of_missing_dev_evidence_is_unknown_not_zero(sized, tmp_db, pct, fragment):
    sized()
    store_dev(tmp_db, pct)
    multiplier, label = _dev_supply_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == DEV_SUPPLY_UNKNOWN, label
    assert fragment in label
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL


def test_a_dossier_for_another_chain_is_not_this_tokens(sized, tmp_db):
    sized()
    store_dev(tmp_db, "25", chain="bsc")
    multiplier, label = _dev_supply_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == DEV_SUPPLY_UNKNOWN
    assert "no_dossier" in label


def test_an_unreadable_dossier_table_is_unknown_and_never_raises(sized, tmp_db):
    """A sizer that died here would stop the agent trading; it prices the absence instead."""
    sized()
    tmp_db.execute("DROP TABLE token_dossiers")
    tmp_db.commit()
    multiplier, label = _dev_supply_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == DEV_SUPPLY_UNKNOWN
    assert "dev_unknown:" in label and "OperationalError" in label
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL


def test_a_corrupt_dossier_is_unknown_not_clean(sized, tmp_db):
    sized()
    tmp_db.execute(
        "INSERT OR REPLACE INTO token_dossiers (chain, address, built_at_ms, grade, dossier_json) "
        "VALUES (?,?,?,?,?)",
        ("sol", TOKEN, 1, "B", "{not json"),
    )
    tmp_db.commit()
    multiplier, label = _dev_supply_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == DEV_SUPPLY_UNKNOWN
    assert "dev_unknown:" in label


# --------------------------------------------------------------------------------------
# 4. the two arms compose without compounding
# --------------------------------------------------------------------------------------


def test_the_wave_and_the_dev_arm_do_not_multiply_each_other(sized, tmp_db):
    """The deeper haircut wins; two INVENTED ladders are not multiplied together.

    0.85 * 0.85 = 0.7225, and MEASURED on the live 4.5 SOL sol bankroll every entry is
    sized 1.02-1.79x its own pool floor, so anything below ~0.82x refuses outright. A
    product would therefore be a **veto** for the exact band in which the operator ruled
    a veto out. The two measurements are also disjoint by construction -- the launch wave
    excludes the creator's own buy -- so nothing is being counted twice by taking one.
    """
    sized()
    store_wave(tmp_db, bundled="25", sniped="0")  # 0.85 on the wave arm
    store_dev(tmp_db, "25")  # 0.85 on the dev arm
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal("0.85"), label
    assert "wave:25.000%" in label and "dev:25.000%" in label
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.85)


@pytest.mark.parametrize(
    "wave,dev,expected",
    [
        ("0", "25", "0.85"),  # only the dev arm sees it
        ("25", "0", "0.85"),  # only the wave arm sees it
        ("60", "15", "0.1"),  # the wave is worse
        ("15", "51", "0.1"),  # the dev is worse
        ("0", "0", "1.0"),  # both measured clean
    ],
)
def test_the_deeper_of_the_two_measurements_is_the_one_that_binds(sized, tmp_db, wave, dev, expected):
    sized()
    store_wave(tmp_db, bundled=wave, sniped="0")
    store_dev(tmp_db, dev)
    multiplier, _ = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal(expected)


def test_an_unknown_on_one_arm_never_hides_a_measurement_on_the_other(sized, tmp_db):
    """Both halves of the label always travel, so a refusal can name what it could not see."""
    sized()
    store_dev(tmp_db, "25")  # measured dev, no bundle row at all
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal("0.85")
    assert "unknown:" in label and "dev:25.000%" in label

    store_wave(tmp_db, bundled="25", sniped="0")
    store_dev(tmp_db, None)  # measured wave, unmeasurable dev
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal("0.85")
    assert "wave:25.000%" in label and "dev_unknown:" in label


def test_the_combined_multiplier_is_clamped_at_the_point_of_use(sized, tmp_db, monkeypatch):
    """Belt and braces, as the wave arm already does: no policy may raise a size."""
    sized()
    monkeypatch.setattr(
        risk_module,
        "_dev_supply_policy",
        lambda: (((Decimal("10"), Decimal("4.0")),), Decimal("6.0"), Decimal("8.0")),
    )
    store_dev(tmp_db, "5")
    assert _concentration_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL
    store_dev(tmp_db, None)
    assert _concentration_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)


def test_the_dev_arm_clamps_itself_and_not_only_when_it_is_composed(sized, tmp_db, monkeypatch):
    """Asserted on ``_dev_supply_multiplier`` directly, on all three of its exits.

    Found by mutation: deleting this function's own clamp changed nothing, because
    ``_concentration_multiplier`` clamps again afterwards and every test went through it.
    A guarantee that only holds because of the caller is not this function's guarantee,
    and its docstring makes the promise in its own name -- ``kaiba/execution/protection.py``
    is the obvious next caller, and it would not be clamping anything.
    """
    sized()
    monkeypatch.setattr(
        risk_module,
        "_dev_supply_policy",
        lambda: (((Decimal("10"), Decimal("4.0")),), Decimal("6.0"), Decimal("8.0")),
    )
    store_dev(tmp_db, "5")  # on the ladder
    assert _dev_supply_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)
    store_dev(tmp_db, "50")  # above the ladder
    assert _dev_supply_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)
    store_dev(tmp_db, None)  # unknown
    assert _dev_supply_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)


# --------------------------------------------------------------------------------------
# 5. the policy is retunable and cannot widen
# --------------------------------------------------------------------------------------


def test_the_operator_can_retune_the_dev_policy(sized, tmp_db):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = {"dev_supply": {"ladder": [[10.0, 1.0], [20.0, 0.5]], "unknown_multiplier": 0.95}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    ladder, _, unknown = _dev_supply_policy()
    assert ladder == ((Decimal("10.0"), Decimal("1.0")), (Decimal("20.0"), Decimal("0.5")))
    assert unknown == Decimal("0.95")
    store_dev(tmp_db, "15")
    # The override actually binds: 15% is 1.0x on the shipped ladder and 0.5x on his.
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.5)


@pytest.mark.parametrize(
    "block",
    [
        {"ladder": [[30.0, 0.5], [10.0, 1.0]]},  # thresholds out of order
        {"ladder": [[10.0, 0.5], [30.0, 1.0]]},  # a multiplier that rises with the dev
        {"ladder": [[10.0, 2.0], [30.0, 1.5]]},  # multipliers above 1.0
        {"ladder": "thirty percent"},
        {"unknown_multiplier": "nonsense"},
    ],
)
def test_an_unreadable_dev_policy_falls_back_to_the_default_not_to_one(sized, block):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = {"dev_supply": block}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert _dev_supply_policy() == (DEV_SUPPLY_LADDER, DEV_SUPPLY_ABOVE_LADDER, DEV_SUPPLY_UNKNOWN)


def test_a_deleted_block_keeps_the_dev_policy(sized, tmp_db):
    """``save_risk`` rewrites the file from the pydantic model and drops it. Code wins."""
    sized()  # the fixture writes no concentration block at all
    assert _dev_supply_policy() == (DEV_SUPPLY_LADDER, DEV_SUPPLY_ABOVE_LADDER, DEV_SUPPLY_UNKNOWN)
    store_dev(tmp_db, "25")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.85)


def test_a_dev_policy_that_tries_to_raise_a_size_is_clamped(sized, tmp_db):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = {"dev_supply": {"unknown_multiplier": 4.0, "above_ladder_multiplier": 9.0}}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    _, above, unknown = _dev_supply_policy()
    assert unknown == Decimal(1) and above == Decimal(1)


def test_the_dev_numbers_say_invented_and_say_what_would_settle_them():
    """A policy number without the word is a magic number; without a route it is a guess."""
    source = Path(risk_module.__file__).read_text(encoding="utf-8")
    section = source[source.index("dev supply -> size multiplier") : source.index("DEV_SUPPLY_UNKNOWN =")]
    assert "INVENTED" in section
    assert "MEASURED" in section
    assert "would settle" in section
