"""Spending the launch-wave concentration measurement: a multiplier, never a veto.

The operator's policy, in his words: *"if its bundled dev buying more than 20% 30% we can
still buy but we need to be careful"*. So these tests pin a size that falls as measured
concentration rises, and four properties that are easy to break by accident:

1. the multiplier can only ever make a position **smaller** -- for every input, on every
   rung, and whichever other clamp happens to be binding;
2. it runs **after** the envelope, the exposure cap, the viability band and the chain
   minimum, so none of them can undo it and an earlier clamp cannot launder it into a
   bigger size;
3. an **unmeasurable** launch is not a clean one. MEASURED on the live box 2026-09-22:
   76 of 78 ENTER decisions (97.4%) had no usable measurement, so an unknown worth full
   size is a mechanism that would never once have fired; and
4. when the multiplier is what takes a size under a floor, the refusal **says so** --
   ``size_not_positive:concentration:...`` on the decision row, not a bare zero.

The evasion these tests are calibrated against is real and reproduces. GIVE
(``12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv``, sol, pump.fun, created 2026-09-18
17:01:18Z) was launched by a friend of the operator to demonstrate it: buy with a few
wallets, sell, then re-buy the same supply through a dozen wallets sharing no funding
edge. Re-measured from GMGN's trader tape on 2026-09-22 (n=100 traders): 5 wallets held
23.695% of supply by t+1s, 16 held 36.693% by t+5s, 43 held 59.418% by t+34s, 31 carry
GMGN's own ``bundler`` tag for 50.550%, and 93 of 100 have fully exited. Today the same
token reports ``top_10_holder_rate`` 0 and 7 holders -- every snapshot metric says clean,
and it has no ``token_bundles`` row at all. So the two cases that matter most here are
the one where the wave was measured and the one where it could not be.

The bankroll is 20 SOL rather than the 10 of ``tests.test_risk`` for one reason: at 10
the 0.1x rung lands under the seeded pool's own economic floor, and every deep-band case
would refuse for the pool's reason instead of exercising the multiplier's arithmetic.
That is not an artefact of the fixture -- it is the live box's problem too, and
``CONCENTRATION_LADDER`` carries the measurement: at a 4.5 SOL bankroll every entry is
sized 1.02-1.79x its own pool floor, so anything below ~0.82x refuses instead of
shrinking.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from kaiba.core.schemas import now_ms
from kaiba.execution import risk as risk_module
from kaiba.execution.risk import (
    CONCENTRATION_ABOVE_LADDER,
    CONCENTRATION_LADDER,
    CONCENTRATION_UNKNOWN,
    RiskGate,
    _concentration_multiplier,
    _concentration_policy,
)
from tests.test_risk import (  # noqa: F401 - fixtures are used by name
    LANE,
    SMALL_LANE,
    SOL,
    TOKEN,
    open_position,
    seed_depth,
    write_risk,
)

#: The mint the operator's friend used to demonstrate the sell-and-rebuy relay.
GIVE = "12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv"

BANKROLL = 20_000_000_000  # 20 SOL in lamports

#: score 95 in ``confluence-5``: 5% of the bankroll, the envelope's own ceiling.
FULL = 1_000_000_000

#: score 95 in ``kol-fade``, a 1% lane.
SMALL = 200_000_000

#: ``seed_depth``'s curve priced by ``viability.sizing_band``: the smallest size at which
#: a round trip in that pool pays for itself, and the largest our order may move.
SEEDED_FLOOR = 79_368_186


@pytest.fixture
def sized(write_risk, tmp_db):  # noqa: F811 - the imported fixture, requested by name
    """A 20 SOL sol budget and a priced pool for both tokens, so the multiplier is reached.

    Without a curve snapshot ``_clamp_to_band`` answers ``no_viable_band`` for any token
    it is given, long before this task's code runs -- which is correct, and is why every
    case here has to say what pool it is entering.
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


def store_wave(
    conn,
    token: str = TOKEN,
    bundled: str | None = "0",
    sniped: str | None = "0",
    *,
    chain: str = "sol",
    coverage: str = "measured",
    reason: str = "measured from the tape",
    supply_basis: str | None = "pumpfun_standard_verified",
    model: str = "kaiba-bundles-v1",
) -> None:
    """Write a ``token_bundles`` row exactly as ``bundles.store`` would."""
    conn.execute(
        "INSERT OR REPLACE INTO token_bundles (chain, token, computed_ms, model, coverage, "
        "reason, supply_basis, bundled_pct, sniped_pct, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (chain, token, now_ms(), model, coverage, reason, supply_basis, bundled, sniped, "{}"),
    )
    conn.commit()


# ------------------------------------------------------------------ the ladder itself


@pytest.mark.parametrize(
    "wave,multiplier",
    [
        ("0", "1.0"),
        ("19.999", "1.0"),
        ("20", "0.85"),  # the operator's own first threshold, inclusive at the bottom
        ("23.695", "0.85"),  # GIVE at t+1s, the age he actually buys at
        ("34.999", "0.85"),
        ("35", "0.5"),
        ("49.999", "0.5"),
        ("50", "0.1"),
        ("59.418", "0.1"),  # GIVE at t+34s, what the launch wave really was
        ("72.875", "0.1"),  # the worst launch in our own corpus
    ],
)
def test_each_band_scales_the_size(sized, tmp_db, wave, multiplier):
    sized()
    store_wave(tmp_db, bundled=wave, sniped="0")
    expected = int(Decimal(FULL) * Decimal(multiplier))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == expected


def test_the_ladder_is_monotone_and_can_never_reward_concentration():
    thresholds = [t for t, _ in CONCENTRATION_LADDER]
    multipliers = [m for _, m in CONCENTRATION_LADDER]
    assert thresholds == sorted(set(thresholds))
    assert multipliers == sorted(multipliers, reverse=True)
    assert all(Decimal(0) <= m <= Decimal(1) for m in [*multipliers, CONCENTRATION_ABOVE_LADDER])
    assert CONCENTRATION_ABOVE_LADDER <= multipliers[-1]


def test_the_multiplier_never_increases_a_size(sized, tmp_db):
    """The property, not an example: no measurement may produce more than a clean one."""
    sized()
    store_wave(tmp_db, bundled="0", sniped="0")
    clean = RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN)
    assert clean == FULL
    for wave in ("0", "5", "19.99", "20", "34.9", "35", "49.9", "50", "80", "100", "250"):
        store_wave(tmp_db, bundled=wave, sniped="0")
        assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) <= clean, wave


def test_bundlers_and_snipers_are_summed(sized, tmp_db):
    """Both hands can dump on us; the operator's 20/30% is about supply, not about how.

    The numbers are the worst launch in our own corpus, MEASURED 2026-09-22: 29.469%
    bundled plus 43.405% sniped. On the bundler arm alone it is a 0.85x token; the wave
    is 72.875% of supply, two rungs lower.
    """
    sized()
    store_wave(tmp_db, bundled="29.469562111685300", sniped="43.405031087461400")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL // 10
    store_wave(tmp_db, bundled="29.469562111685300", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.85)


# ------------------------------------------------------- which detector gets to answer


def stub_report(pct: str | None, *, measured: bool = True, gate: str = "thin"):
    """The shape ``launch_concentration.measure`` returns, with only what the sizer reads.

    Stubbed rather than driven through a real tape on purpose: this file tests which
    detector the sizer believes and what it does with the number, not whether the
    detector is right. ``tests/test_launch_concentration.py`` owns that.
    """
    return SimpleNamespace(
        measured=measured,
        gate=SimpleNamespace(value=gate),
        model_id="kaiba-launch-concentration-v1",
        headline_pct=SimpleNamespace(value=None if pct is None else Decimal(pct)),
    )


def patch_detector(monkeypatch, result):
    from kaiba.intelligence import launch_concentration

    def _measure(_chain, _token, _conn=None, **_kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(launch_concentration, "measure", _measure)


def test_the_relay_aware_detector_outranks_the_stored_row(sized, tmp_db, monkeypatch):
    """The whole point of the piece: the stored row is the one GIVE's relay defeats.

    ``token_bundles`` groups same-slot contiguous runs. Buy, sell, then re-buy the same
    supply through wallets with no shared funding edge and it reads clean -- so when the
    relay-aware headline has a number, it is the number.
    """
    sized()
    store_wave(tmp_db, bundled="0", sniped="0")  # "clean", and wrong
    patch_detector(monkeypatch, stub_report("59.418"))  # GIVE's real launch wave
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal("0.1"), label
    assert "headline/kaiba-launch-concentration-v1" in label
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL // 10


def test_the_stored_row_answers_when_the_detector_refuses(sized, tmp_db, monkeypatch):
    sized()
    store_wave(tmp_db, bundled="40", sniped="0")
    patch_detector(monkeypatch, stub_report(None, measured=False, gate="coverage"))
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == Decimal("0.5"), label
    assert "wave:40.000%" in label


def test_a_detector_that_raises_never_reaches_the_caller(sized, tmp_db, monkeypatch):
    """That module is edited by another task while this one runs; a signature change
    must cost a measurement, not the agent's ability to size anything at all."""
    sized()
    store_wave(tmp_db, bundled="40", sniped="0")
    patch_detector(monkeypatch, TypeError("measure() got an unexpected keyword argument"))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL // 2
    tmp_db.execute("DELETE FROM token_bundles")
    tmp_db.commit()
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == CONCENTRATION_UNKNOWN
    assert "launch_concentration:TypeError" in label


def test_a_headline_of_zero_is_a_measurement_not_an_unknown(sized, tmp_db, monkeypatch):
    """0% measured and 0% assumed must not produce the same size."""
    sized()
    patch_detector(monkeypatch, stub_report("0"))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL
    patch_detector(monkeypatch, stub_report(None, measured=False, gate="thin"))
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(
        Decimal(FULL) * CONCENTRATION_UNKNOWN
    )


def test_a_measured_report_with_no_headline_number_is_still_unknown(sized, tmp_db, monkeypatch):
    """``measured`` is about the tape; the headline can still be one of the unknowns.

    The report carries its components independently and lists the ones it could not put
    a number on, so ``measured=True`` plus ``headline_pct.value is None`` is a shape that
    really occurs -- and reading it as 0% would be the exact fail-open this whole
    mechanism exists to prevent.
    """
    sized()
    patch_detector(monkeypatch, stub_report(None, measured=True))
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == CONCENTRATION_UNKNOWN, label
    assert "no_headline" in label
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(
        Decimal(FULL) * CONCENTRATION_UNKNOWN
    )


# ------------------------------------------------------------------ unknown is not clean


def test_an_unmeasured_token_is_not_a_clean_token(sized, tmp_db):
    """No row at all -- which is what GIVE looks like on the live box today.

    Since 2026-09-22 an unmeasured token is not CHARGED (see CONCENTRATION_UNKNOWN: the
    discount was landing under the per-token viable floor and vetoing 4 of 6 live sol
    candidates, and the outcome study found concentration does not predict the return in
    either direction). What must NOT happen is the thing this test is named for: it must
    never be RECORDED as a clean token. The label is the discipline; the multiplier is a
    policy the evidence no longer supports.
    """
    sized()
    expected = int(Decimal(FULL) * CONCENTRATION_UNKNOWN)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == expected
    multiplier, label = _concentration_multiplier(SOL, GIVE, tmp_db)
    assert label.startswith("unknown:"), label
    assert "wave:" not in label, "an unmeasured token must never carry a measured wave"


def test_unknown_is_never_a_premium():
    """It may be 1.0, never above: this mechanism only ever shrinks a size."""
    assert CONCENTRATION_UNKNOWN <= Decimal(1)
    multiplier, label = _concentration_multiplier(SOL, GIVE, None)
    assert multiplier == CONCENTRATION_UNKNOWN
    assert label.startswith("unknown:")


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"coverage": "unavailable", "bundled": None, "sniped": None}, "coverage_unavailable"),
        ({"bundled": None, "sniped": None, "supply_basis": "unknown"}, "no_supply_basis"),
        ({"bundled": "not a number", "sniped": "0"}, "unparseable_pct"),
        ({"bundled": "-1", "sniped": "0"}, "negative_pct"),
    ],
)
def test_every_shape_of_missing_evidence_is_unknown_not_zero(sized, tmp_db, kwargs, fragment):
    """A row we cannot read is None plus the reason, never 0%."""
    sized()
    store_wave(tmp_db, **kwargs)
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == CONCENTRATION_UNKNOWN, label
    assert fragment in label
    expected = int(Decimal(FULL) * CONCENTRATION_UNKNOWN)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == expected


def test_a_measurement_on_another_chain_is_not_this_token_s(sized, tmp_db):
    """A row is per (chain, token). bsc and robinhood have no bundle rows at all today."""
    sized()
    store_wave(tmp_db, chain="bsc", bundled="0", sniped="0")
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == CONCENTRATION_UNKNOWN
    assert "never_measured" in label


def test_an_unreadable_table_is_unknown_and_never_raises(sized, tmp_db):
    sized()
    tmp_db.execute("DROP TABLE token_bundles")
    tmp_db.commit()
    multiplier, label = _concentration_multiplier(SOL, TOKEN, tmp_db)
    assert multiplier == CONCENTRATION_UNKNOWN
    assert label.startswith("unknown:")
    assert "unreadable:OperationalError" in label


def test_without_a_token_the_size_is_unscaled(sized, tmp_db):
    """The rule ``_clamp_to_band`` already applies to depth: no token, nothing to look up."""
    sized()
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db) == FULL


# ------------------------------------------------------------------ composition order


def test_the_multiplier_runs_after_the_five_percent_envelope(sized, tmp_db):
    """Applied first it would be swallowed by the clamp and the size would not move.

    The lane asks for 20% of the bankroll and the envelope caps it at 5%. 20% * 0.85 is
    still over the cap, so a multiplier applied before the clamp comes out at the full
    1,000,000,000 -- the concentrated launch gets the clean launch's size.
    """
    sized(
        lanes={"confluence-5": {"size_pct_max": 20.0}},
        chains={"sol": {"max_position_base_units": 5_000_000_000}},
    )
    store_wave(tmp_db, bundled="25", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.85)


def test_the_multiplier_runs_after_the_total_exposure_cap(sized, tmp_db):
    """It composes with the 25% basket cap rather than replacing it: 400M room, then 0.85x."""
    sized()
    open_position(tmp_db, "OTHER", 4_600_000_000)  # 23% of the 20 SOL bankroll deployed
    store_wave(tmp_db, bundled="25", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 340_000_000


def test_the_band_floor_cannot_raise_the_multiplier_back(sized, tmp_db):
    """The case that would silently defeat the whole mechanism.

    ``_clamp_to_band`` raises a size up to the pool's economic floor. Run the multiplier
    before it and a 1% lane's 200,000,000 becomes 20,000,000 and is lifted straight back
    to 79,368,186 -- four times what the multiplier asked for, and 40% of what a clean
    token gets in the same pool. Run it after and the honest answer is that we cannot bet
    as small as this launch deserves in this pool, so we do not bet.
    """
    sized()
    store_wave(tmp_db, bundled="60", sniped="0")
    assert RiskGate().position_size(SOL, SMALL_LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, SMALL_LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:concentration:"), reason
    assert "viable_floor" in reason and str(SEEDED_FLOOR) in reason


def test_a_clean_token_in_the_same_pool_still_trades(sized, tmp_db):
    """The counterpart: the refusal above is the concentration's doing, not the pool's."""
    sized()
    store_wave(tmp_db, bundled="0", sniped="0")
    assert RiskGate().position_size(SOL, SMALL_LANE, 95.0, tmp_db, token=TOKEN) == SMALL


def test_a_band_that_cannot_be_computed_is_never_a_refusal(sized, tmp_db, monkeypatch):
    """A floor we could not read is not a floor the size failed: it scales and goes.

    Refusing on a missing band here would turn every unpriceable pool into a
    concentration refusal, which is a lie about the cause and, at 54 of 60 decisions
    with no curve snapshot at all (MEASURED 2026-09-20), would stop the agent trading.
    """
    sized()
    store_wave(tmp_db, bundled="40", sniped="0")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no pool")

    monkeypatch.setattr(risk_module.viability, "sizing_band", _explode)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL // 2


# ------------------------------------------------------------------ refusals name it


def test_a_size_cut_under_the_chain_minimum_names_the_concentration(sized, tmp_db):
    sized(chains={"sol": {"min_position_base_units": 600_000_000}})
    store_wave(tmp_db, bundled="40", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:concentration:wave:40.000%"), reason
    assert "0.5x" in reason and "<600000000" in reason


def test_an_unknown_no_longer_shrinks_the_size_but_is_still_named(sized, tmp_db):
    """CONCENTRATION_UNKNOWN is 1.0: an unknown costs size NOWHERE, and hides NOWHERE.

    Changed from 0.9 on 2026-09-22 after it was measured doing harm. At the live 4.5 SOL
    sol bankroll every entry sizes 1.02-1.79x its own pool floor (see the module
    docstring), so a 0.9x haircut lands a large share of entries UNDER the floor -- and
    97.4% of ENTER decisions are unknowns. The observed effect was sol sizing 0 of 6
    candidates on `concentration:unknown` while bsc and robinhood sized normally.

    A discount that fires on almost everything is not a risk control, it is a bankroll
    cut applied through the wrong knob, and it was silently disabling the chain. The
    LABEL is what does the work here: an unmeasured launch is still never recorded as a
    clean one, so the operator can see how much of the book is running unmeasured.

    Both halves are pinned, because either one alone is the bug: full size with no label
    launders an unknown into a clean token, and a label with a haircut is what we just
    removed.
    """
    sized(chains={"sol": {"min_position_base_units": 1}})
    multiplier, label = _concentration_multiplier(SOL, GIVE, tmp_db)
    assert multiplier == Decimal("1.0"), "an unknown must not be charged a haircut"
    assert multiplier == CONCENTRATION_UNKNOWN
    assert label.startswith("unknown:"), label
    # ...and the size that comes out carries no concentration cut at all.
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == FULL


def test_an_unknown_is_still_weaker_than_every_measured_clean_reading(sized, tmp_db):
    """1.0 is the ceiling, not a reward: nothing may size ABOVE an unknown.

    Guards the direction of the change. Raising the unknown to 1.0 is only safe while it
    is the top of the ladder -- if a rung ever exceeded it, an unmeasured launch would be
    sized more aggressively than a measured-clean one.
    """
    assert CONCENTRATION_UNKNOWN == Decimal("1.0")
    assert all(m <= CONCENTRATION_UNKNOWN for _, m in CONCENTRATION_LADDER), CONCENTRATION_LADDER
    assert CONCENTRATION_ABOVE_LADDER <= CONCENTRATION_UNKNOWN


def test_a_size_already_under_the_minimum_is_not_blamed_on_concentration(sized, tmp_db):
    """False attribution is its own bug: this one was too small before the multiplier ran."""
    sized(chains={"sol": {"min_position_base_units": 1_500_000_000}})
    store_wave(tmp_db, bundled="60", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == 0
    reason = RiskGate().check_entry(SOL, LANE, 0, tmp_db, token=TOKEN).reason
    assert reason.startswith("size_not_positive:below_min_position:"), reason


def test_the_reason_head_still_classifies_as_one_brake():
    assert "size_not_positive:concentration:wave:40.000%".split(":", 1)[0] == "size_not_positive"


# ------------------------------------------------------------------ the policy is config


def test_the_operator_can_retune_the_policy(sized, tmp_db):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = {
        "unknown_multiplier": 0.95,
        "ladder": [[20.0, 1.0], [30.0, 0.6]],
        "above_ladder_multiplier": 0.05,
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    ladder, above, unknown = _concentration_policy()
    assert unknown == Decimal("0.95") and above == Decimal("0.05")
    assert ladder == ((Decimal("20.0"), Decimal("1.0")), (Decimal("30.0"), Decimal("0.6")))
    store_wave(tmp_db, bundled="25", sniped="0")
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == int(FULL * 0.6)


@pytest.mark.parametrize(
    "block",
    [
        {"ladder": [[35.0, 0.5], [20.0, 1.0]]},  # thresholds out of order
        {"ladder": [[20.0, 0.5], [35.0, 1.0]]},  # a multiplier that rises with the wave
        {"ladder": [[20.0, 2.0], [35.0, 1.5]]},  # multipliers above 1.0
        {"ladder": "twenty percent"},
        {"unknown_multiplier": "nonsense"},
    ],
)
def test_an_unreadable_policy_falls_back_to_the_default_not_to_one(sized, block):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = block
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    ladder, above, unknown = _concentration_policy()
    assert (ladder, above, unknown) == (
        CONCENTRATION_LADDER,
        CONCENTRATION_ABOVE_LADDER,
        CONCENTRATION_UNKNOWN,
    )


def test_a_config_that_tries_to_raise_a_size_is_clamped(sized, tmp_db):
    path = sized()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["concentration"] = {"unknown_multiplier": 4.0, "above_ladder_multiplier": 9.0}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    _, above, unknown = _concentration_policy()
    assert unknown == Decimal(1) and above == Decimal(1)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == FULL


def test_a_policy_above_one_is_clamped_at_the_point_of_use(sized, tmp_db, monkeypatch):
    """Belt and braces: the loader clamps, and the multiplier clamps again.

    The loader is one function and one future bug away from letting a number above 1.0
    through, and the whole contract of this mechanism is that it cannot raise a size. So
    the clamp is asserted where the size is actually multiplied, not only where the file
    is parsed.
    """
    sized()
    monkeypatch.setattr(
        risk_module,
        "_concentration_policy",
        lambda: (((Decimal("20"), Decimal("3.0")),), Decimal("5.0"), Decimal("7.0")),
    )
    store_wave(tmp_db, bundled="0", sniped="0")
    assert _concentration_multiplier(SOL, TOKEN, tmp_db)[0] == Decimal(1)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=TOKEN) == FULL
    assert _concentration_multiplier(SOL, GIVE, tmp_db)[0] == Decimal(1)
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == FULL


def test_a_deleted_block_keeps_the_policy(sized, tmp_db):
    """``save_risk`` will delete it: pydantic drops undeclared keys. The code default wins."""
    sized()  # the fixture writes no concentration block at all
    ladder, above, unknown = _concentration_policy()
    assert (ladder, above, unknown) == (
        CONCENTRATION_LADDER,
        CONCENTRATION_ABOVE_LADDER,
        CONCENTRATION_UNKNOWN,
    )
    assert RiskGate().position_size(SOL, LANE, 95.0, tmp_db, token=GIVE) == int(
        Decimal(FULL) * CONCENTRATION_UNKNOWN
    )


def test_the_shipped_file_agrees_with_the_code_default():
    """The operator may retune it, but what ships must parse and must not widen anything."""
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "risk.yaml").read_text(encoding="utf-8")
    )
    block = raw.get("concentration")
    assert isinstance(block, dict), "the shipped risk file must carry the policy explicitly"
    # <= 1: this mechanism only ever shrinks. It is 1.0 since 2026-09-22 -- see
    # CONCENTRATION_UNKNOWN for the two measurements that moved it off 0.9.
    assert Decimal(str(block["unknown_multiplier"])) <= Decimal(1)
    rungs = [(Decimal(str(t)), Decimal(str(m))) for t, m in block["ladder"]]
    assert all(m <= Decimal(1) for _, m in rungs)
    assert [t for t, _ in rungs] == sorted({t for t, _ in rungs})
    assert [m for _, m in rungs] == sorted((m for _, m in rungs), reverse=True)
    assert Decimal(str(block["above_ladder_multiplier"])) <= rungs[-1][1]


def test_the_numbers_are_labelled_invented():
    """Every threshold here is a policy. The word is the contract; a bare number is not."""
    source = Path(risk_module.__file__).read_text(encoding="utf-8")
    head, _, _ = source.partition("CONCENTRATION_LADDER: tuple")
    section = head[head.index("launch-wave concentration -> size multiplier") :]
    assert "INVENTED" in section
    assert "MEASURED" in section
    config_text = (
        Path(__file__).resolve().parents[1] / "config" / "risk.yaml"
    ).read_text(encoding="utf-8")
    block = config_text[config_text.index("Launch-wave concentration") : config_text.index("chains:")]
    assert "INVENTED" in block
