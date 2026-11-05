"""Tests for :mod:`kaiba.intelligence.launch_concentration`.

Three things are being defended here and they are not the same thing.

**1. The GIVE corpus, pinned to the atom.** The operator's friend launched
``12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv`` on 2026-09-18 and described the evasion:
buy with a few wallets, sell, re-buy the same supply with a dozen wallets that share no
funding edge. :data:`GIVE_TRADERS` is that launch's real trader table -- 99 wallets with a
first-buy second, a last-sell second and their bought and sold amounts, read from
``gmgn-cli token traders`` on the live box on 2026-09-22 and converted to atoms at the
mint's 6 decimals. Every number the lead measured by hand is asserted here:

* the largest wave in the first 34 s is **59.418%** of supply across **42** wallets;
* the exact co-timed cohorts are **17** wallets holding **7.372%**, in **six** cohorts,
  and each cohort's entry and exit second is pinned individually;
* the t+12 s relay is **4.743%** out and **21.799%** back in within 120 s;
* GMGN's own ``bundler`` tag is **50.550%**, which our own measurement does not use.

Those assertions are the mutation tests the brief asks for. Drop the exit-second half of
the co-timed conjunction and the six cohorts collapse. Order buys before sells inside a
second and the t+12 relay stops matching. Widen or narrow the wave window and 59.418%
moves.

**2. Missing evidence is ``None``, never 0.** Every refusal path in
:func:`~kaiba.intelligence.launch_concentration.measure` has a test that asserts
``value is None`` and ``basis is UNAVAILABLE`` rather than merely "falsy", because a false
0% here is a token that looks clean and is not.

**3. The headline cannot double-count and cannot exceed 100%.** Those are the two
properties its combination rule is chosen for, and both are asserted directly rather than
inspected: a wallet that appears in all three components contributes once, and the
headline is bounded above by the all-wallet peak, which the ledger gate bounds by supply.

A note on what :data:`GIVE_TRADERS` is *not*. It is a per-wallet vendor aggregate, so
reconstructing it gives every wallet exactly one buy and one sell. That is enough to
reproduce the wave, the cohorts and the relay pair -- all three are functions of entry
second, exit second and amount -- and it is deliberately **not** enough to pass
:func:`~kaiba.intelligence.launch_concentration.ledger_closes`, whose failure on this very
data is asserted below. GIVE has zero rows in our ``swaps`` table, so
:func:`~kaiba.intelligence.launch_concentration.measure` refuses it outright, which is the
correct answer and not a workaround.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Side
from kaiba.intelligence import launch_concentration as lc
from kaiba.intelligence.bundles import (
    PUMPFUN_CURVE_INVARIANT_ATOMS,
    PUMPFUN_LAUNCH_CURVE_ATOMS,
    PUMPFUN_TOTAL_SUPPLY_ATOMS,
)

SOL = Chain.SOL
SUPPLY = PUMPFUN_TOTAL_SUPPLY_ATOMS
#: 1% of supply, so every expectation below is readable by eye.
PCT = SUPPLY // 100

CREATOR = "Creator1111111111111111111111111111111111111"
TOKEN = "Mint1111111111111111111111111111111111111111"
CREATED_MS = 1_789_891_020_000


# --------------------------------------------------------------------------------------
# the GIVE corpus
# --------------------------------------------------------------------------------------

#: ``(first buy second, last sell second or None, bought atoms, sold atoms, GMGN bundler
#: tag)``, one row per trader, seconds relative to the launch's first trade. Read from the
#: live box on 2026-09-22; amounts are the vendor's UI figures at 6 decimals, exact.
GIVE_TRADERS: tuple[tuple[int, int | None, int, int, bool], ...] = (
    (0, 3229, 80000000000000, 80000000000000, True),
    (0, 113, 110000000000000, 45827957103646, True),
    (0, None, 0, 0, False),
    (1, 4, 29732038735758, 29732038735758, True),
    (1, 115, 17220297241700, 17220297241700, True),
    (2, 1641, 13338886119789, 13338886119789, True),
    (2, 120, 20946908811025, 20946908811025, True),
    (2, 17, 3160417581930, 3160417581930, True),
    (2, 247, 10500528571923, 10500528571923, True),
    (2, 21, 11927705716703, 11927705716703, True),
    (3, 12, 10452927304528, 10452927304528, True),
    (3, 5, 14465070330124, 14465070330124, True),
    (3, 14, 11761257961153, 11761257961153, True),
    (3, 21, 22610772729144, 22610772729144, False),
    (3, 13, 1864588781356, 1864588781356, True),
    (5, 23, 8944724306842, 8944724306842, True),
    (8, 14, 10469372118703, 10469372118703, True),
    (8, 14, 14301036917323, 14301036917323, True),
    (8, 14, 8566719628669, 8566719628669, True),
    (8, 19, 647882917260, 647882917260, False),
    (9, 14, 1934285122406, 1934285122406, False),
    (10, 12, 36977506604777, 36977506604777, True),
    (11, 69, 1477474686440, 1477474686440, False),
    (13, 16, 685613636032, 685613636032, False),
    (14, 21, 459073000000, 459073000000, False),
    (14, 52, 8623165630787, 8623165630787, True),
    (14, 21, 1377219000000, 1377219000000, True),
    (14, 21, 1377219000000, 1377219000000, True),
    (14, 21, 1377219000000, 1377219000000, True),
    (15, 37, 3221882577, 3221882577, False),
    (17, 95, 996046965496, 996046965496, False),
    (17, 3008, 1739714756333, 1739714756333, False),
    (20, 6515, 425028269896, 425028269896, False),
    (22, 47, 3261396598898, 3261396598898, True),
    (22, 37, 7556427628644, 7556427628644, True),
    (24, 86, 1754963722063, 1754963722063, False),
    (25, 1023, 25106151341962, 25106151341962, False),
    (25, 3229, 20501568252000, 20501568252000, True),
    (26, 121, 11881816732745, 11881816732745, True),
    (27, 106, 97374522036, 97374522036, False),
    (28, 3229, 23119667298000, 23119667298000, False),
    (31, 3229, 21551713458000, 21551713458000, False),
    (34, 3229, 20980833600000, 20980833600000, False),
    (38, 86, 5668669036600, 5668669036600, False),
    (39, 59, 957547162082, 957547162082, False),
    (44, 99, 972449719698, 972449719698, False),
    (46, 118, 21853495985846, 21853495985846, False),
    (47, None, 837910017637, 0, False),
    (47, 65, 919779729137, 919779729137, False),
    (49, 3487, 1700246977501, 1700246977501, False),
    (50, None, 143797306152, 0, False),
    (58, 1094, 874207562117, 874207562117, False),
    (69, 1093, 785244778365, 785244778365, False),
    (72, 1335, 21398769670976, 21398769670976, False),
    (78, None, 96586525405, 0, False),
    (88, 3229, 0, 24736011081654, False),
    (114, 950, 5631557965746, 5631557965746, False),
    (126, 309, 92955987656, 92955987656, False),
    (132, 494, 3181606447591, 3181606447591, False),
    (144, 628, 1737490675207, 1737490675207, False),
    (150, 363, 2346974082096, 2346974082096, False),
    (155, 1044, 3486201257829, 3486201257829, False),
    (155, 4949, 69399228739, 69399228739, False),
    (170, 4298, 2187532389119, 2187532389119, False),
    (175, 330, 2360799980745, 2360799980745, False),
    (197, 288, 272009860622, 272009860622, False),
    (206, 4082, 0, 6838328100850, False),
    (211, 5649, 1414192437743, 1414192437743, False),
    (220, 883, 1938199351027, 1938199351027, False),
    (221, 370, 275340865093, 275340865093, False),
    (221, 321, 22131861060319, 22131861060319, True),
    (227, 633, 10756827536008, 10756827536008, False),
    (236, 607, 5260894225564, 5260894225564, True),
    (402, 736, 1287716620166, 1287716620166, False),
    (558, None, 125078636761, 0, False),
    (884, None, 0, 0, False),
    (1101, 1121, 2506923000000, 2506923000000, True),
    (1101, 1121, 1671282000000, 1671282000000, True),
    (1101, 1121, 3342565000000, 3342565000000, True),
    (1102, 3230, 5064552628939, 5064552628939, False),
    (1130, 3440, 4563670760656, 4563670760656, False),
    (1144, 3436, 1538298623641, 1538298623641, False),
    (1144, 2938, 1545004418831, 1545004418831, False),
    (1163, 1435, 6926549039605, 6926549039605, False),
    (1163, 1435, 7949943510605, 7949943510605, False),
    (1165, 3230, 5401700638322, 5401700638322, False),
    (1165, 3230, 2978951449538, 2978951449538, False),
    (1206, 1325, 8278676358597, 8278676358597, False),
    (1208, 3230, 1393925703195, 1393925703195, False),
    (1208, 3230, 2230281125113, 2230281125113, False),
    (1208, 3230, 1393925703195, 1393925703195, False),
    (1212, 1435, 51194192320341, 51194192320341, False),
    (1214, 1436, 2318522596373, 2318522596373, False),
    (1472, 1514, 1480921307897, 1480921307897, False),
    (1507, 2703, 676171947962, 676171947962, False),
    (1867, 3427, 6545554898199, 6545554898199, False),
    (1970, None, 2819225938912, 0, False),
    (2186, 4017, 2047165566185, 2047165566185, False),
    (2785, 3089, 4552714415467, 4552714415467, False),
)


def give_events() -> tuple[lc.TradeEvent, ...]:
    """The GIVE launch as a trade sequence, one buy and one sell per wallet."""
    events: list[lc.TradeEvent] = []
    for index, (entry, exit_s, bought, sold, _tag) in enumerate(GIVE_TRADERS):
        wallet = f"give{index:02d}"
        if bought > 0:
            events.append(lc.TradeEvent(wallet, entry, Side.BUY, bought, f"b{index}"))
        if exit_s is not None and sold > 0:
            events.append(lc.TradeEvent(wallet, exit_s, Side.SELL, sold, f"s{index}"))
    return lc.normalise_events(events)


def give_vendor() -> lc.VendorCrossCheck:
    tagged = [row for row in GIVE_TRADERS if row[4]]
    return lc.VendorCrossCheck(
        provider="gmgn",
        tag="bundler",
        wallets=len(tagged),
        atoms=sum(row[2] for row in tagged),
        traders_seen=len(GIVE_TRADERS),
        note="gmgn-cli token traders --limit 100, read 2026-09-22",
    )


def pct_of_supply(atoms: int) -> Decimal:
    return Decimal(atoms) / Decimal(SUPPLY) * Decimal(100)


# --------------------------------------------------------------------------------------
# GIVE: the launch wave
# --------------------------------------------------------------------------------------


def test_give_wave_at_34s_is_the_measured_59_418_percent() -> None:
    """The lead's headline finding, to three decimals and to the wallet.

    This is the assertion the whole module exists to make true: 42 wallets, 59.418% of
    supply, inside 34 seconds, on a token whose ``top_10_holder_rate`` reads 0 today.
    """
    point = next(iter(lc.launch_wave(give_events(), SUPPLY, (34,))))
    assert point.wallet_count == 42
    assert point.pct.value is not None
    assert round(point.pct.value, 3) == Decimal("59.418")


def test_give_wave_curve_is_monotone_and_w60_clears_the_34s_figure() -> None:
    """Both properties at once: the curve never falls, and W=60 must contain W=34.

    Monotonicity is structural -- widening ``W`` can only add wallets to the cohort and can
    only add buying to each member -- so a fall here means the cohort rule has been broken,
    not that the token changed.
    """
    curve = lc.launch_wave(give_events(), SUPPLY, (*lc.WAVE_WINDOWS_S, 34))
    values = [p.pct.value for p in curve]
    assert all(v is not None for v in values)
    assert values == sorted(values)
    at34 = next(p for p in curve if p.window_s == 34).pct.value
    at60 = next(p for p in curve if p.window_s == 60).pct.value
    assert at60 >= at34
    assert round(at60, 3) == Decimal("62.810")


def test_give_wave_at_one_second_is_what_the_operator_sees_when_he_buys() -> None:
    """t+1 s is the operator's stated entry latency, and it already shows 23.695%.

    This is why the answer is a curve and not a number: at the instant he buys, a quarter
    of supply is already in wallets that arrived in the first second. He guessed 30% for
    the whole launch.
    """
    point = next(iter(lc.launch_wave(give_events(), SUPPLY, (1,))))
    assert round(point.pct.value, 3) == Decimal("23.695")
    assert point.wallet_count == 4


def test_wave_in_window_share_never_exceeds_the_lifetime_share() -> None:
    """The truncated companion is a floor under the retrospective figure, never above it."""
    for point in lc.launch_wave(give_events(), SUPPLY, lc.WAVE_WINDOWS_S):
        assert point.in_window_atoms <= point.bought_atoms
        assert point.in_window_pct.value <= point.pct.value


def test_wave_cohort_membership_is_by_first_buy_not_by_any_buy() -> None:
    """A wallet that arrives late and then trades hard is not part of the launch wave."""
    events = lc.normalise_events(
        [
            lc.TradeEvent("early", 0, Side.BUY, 5 * PCT, "a"),
            lc.TradeEvent("late", 500, Side.BUY, 40 * PCT, "b"),
            lc.TradeEvent("late", 501, Side.BUY, 40 * PCT, "c"),
        ]
    )
    point = next(iter(lc.launch_wave(events, SUPPLY, (60,))))
    assert point.wallets == ("early",)
    assert point.pct.value == Decimal(5)


# --------------------------------------------------------------------------------------
# GIVE: the co-timed cohorts -- the part a funding graph cannot see
# --------------------------------------------------------------------------------------

#: ``(entry second, exit second, wallets)`` for every cohort on GIVE, as measured.
GIVE_COHORTS = (
    (8, 14, 3),
    (14, 21, 4),
    (1101, 1121, 3),
    (1163, 1435, 2),
    (1165, 3230, 2),
    (1208, 3230, 3),
)


def test_give_cotimed_cohorts_are_exactly_the_six_measured_ones() -> None:
    """Miss any of these and the laundering is invisible. This is the mutation test.

    Each cohort is pinned by entry second, exit second and size. Relaxing the conjunction
    to entry-only, or quantising the timestamps differently, changes this set.
    """
    found = sorted((c.entry_s, c.exit_s, c.size) for c in lc.cotimed_cohorts(give_events()))
    assert found == sorted(GIVE_COHORTS)


def test_give_cotimed_share_is_the_measured_7_372_percent() -> None:
    cohorts = lc.cotimed_cohorts(give_events())
    assert sum(c.size for c in cohorts) == 17
    assert round(pct_of_supply(lc.cotimed_atoms(cohorts)), 3) == Decimal("7.372")


def test_give_largest_cohort_is_the_3_334_percent_t8_to_t14_group() -> None:
    """The lead named this one: 3 wallets in at t+8, all out at t+14."""
    cohorts = lc.cotimed_cohorts(give_events())
    biggest = cohorts[0]
    assert (biggest.entry_s, biggest.exit_s, biggest.size) == (8, 14, 3)
    assert round(pct_of_supply(biggest.atoms), 3) == Decimal("3.334")


def test_cotimed_requires_both_seconds_to_match() -> None:
    """Same entry, different exit is not a cohort. Neither is the reverse.

    Without this conjunction the component fires on every busy second of every launch and
    carries no information, which is the failure mode ``bundles.MIN_BUNDLE_ENTITIES``
    exists to prevent for the block-adjacency definition.
    """
    same_entry_only = lc.normalise_events(
        [
            lc.TradeEvent("a", 10, Side.BUY, PCT, "1"),
            lc.TradeEvent("b", 10, Side.BUY, PCT, "2"),
            lc.TradeEvent("a", 90, Side.SELL, PCT, "3"),
            lc.TradeEvent("b", 91, Side.SELL, PCT, "4"),
        ]
    )
    assert lc.cotimed_cohorts(same_entry_only) == ()
    both = lc.normalise_events(
        [
            lc.TradeEvent("a", 10, Side.BUY, PCT, "1"),
            lc.TradeEvent("b", 10, Side.BUY, PCT, "2"),
            lc.TradeEvent("a", 90, Side.SELL, PCT, "3"),
            lc.TradeEvent("b", 90, Side.SELL, PCT, "4"),
        ]
    )
    assert [(c.entry_s, c.exit_s, c.size) for c in lc.cotimed_cohorts(both)] == [(10, 90, 2)]


def test_cotimed_is_blind_until_exits_exist_and_says_so_by_being_empty() -> None:
    """A token nobody has dumped yet has no cohorts. That is absence, not a low number."""
    holding = lc.normalise_events(
        [lc.TradeEvent(f"w{i}", 3, Side.BUY, PCT, str(i)) for i in range(6)]
    )
    assert lc.cotimed_cohorts(holding) == ()
    assert lc.cotimed_atoms(()) == 0


# --------------------------------------------------------------------------------------
# GIVE: the relay
# --------------------------------------------------------------------------------------


def test_give_relay_at_t12_reproduces_the_measured_pair() -> None:
    """4.743% exits at t+12 s and 21.799% is re-bought inside 120 s. Both, exactly."""
    event = next(r for r in lc.relay_events(give_events()) if r.exit_s == 12)
    assert round(pct_of_supply(event.exit_atoms), 3) == Decimal("4.743")
    assert round(pct_of_supply(event.rebuy_atoms), 3) == Decimal("21.799")


def test_give_relay_events_exist_in_quantity_and_carry_both_sides() -> None:
    """The events are the evidence; the total is only a summary of them."""
    relays = lc.relay_events(give_events())
    assert len(relays) >= 16
    assert all(r.matched_atoms > 0 for r in relays)
    assert all(r.sellers and r.buyers for r in relays)


def test_relay_matching_cannot_count_an_atom_twice() -> None:
    """FIFO matching is a partition: the total is bounded by both sides of the tape."""
    events = give_events()
    matched = lc.relay_total(lc.relay_events(events))
    sold = sum(e.atoms for e in events if not e.is_buy)
    bought = sum(e.atoms for e in events if e.is_buy)
    assert 0 < matched <= min(sold, bought)


def test_relay_ignores_a_rebuy_outside_the_window() -> None:
    events = lc.normalise_events(
        [
            lc.TradeEvent("a", 0, Side.BUY, 10 * PCT, "1"),
            lc.TradeEvent("a", 10, Side.SELL, 10 * PCT, "2"),
            lc.TradeEvent("b", 10 + lc.RELAY_WINDOW_S + 1, Side.BUY, 10 * PCT, "3"),
        ]
    )
    assert lc.relay_events(events) == ()
    assert lc.relay_total(()) == 0


def test_sells_are_ordered_before_buys_inside_the_same_second() -> None:
    """A relay's two legs routinely land in one second, and the order decides the answer.

    Ordering buys first would make the re-buy arrive before the exit it consumes, and the
    largest same-second relays would silently vanish.
    """
    events = lc.normalise_events(
        [
            lc.TradeEvent("a", 0, Side.BUY, 10 * PCT, "1"),
            lc.TradeEvent("b", 7, Side.BUY, 10 * PCT, "2"),
            lc.TradeEvent("a", 7, Side.SELL, 10 * PCT, "3"),
        ]
    )
    assert [(e.ts_s, e.side) for e in events][1] == (7, Side.SELL)
    relays = lc.relay_events(events)
    assert [r.exit_s for r in relays] == [7]
    assert relays[0].matched_atoms == 10 * PCT


# --------------------------------------------------------------------------------------
# the vendor cross-check, kept off the decision path
# --------------------------------------------------------------------------------------


def test_give_vendor_bundler_tag_is_the_measured_50_550_percent() -> None:
    vendor = give_vendor()
    assert vendor.wallets == 31
    assert round(lc.vendor_share(vendor, SUPPLY).value, 3) == Decimal("50.550")


def test_vendor_share_is_provider_reported_and_ours_is_derived() -> None:
    """The two must never be confusable by basis, because they are not the same evidence."""
    vendor = lc.vendor_share(give_vendor(), SUPPLY)
    assert vendor.basis is EvidenceBasis.PROVIDER_REPORTED
    ours = next(iter(lc.launch_wave(give_events(), SUPPLY, (34,)))).pct
    assert ours.basis is EvidenceBasis.DERIVED


def test_vendor_and_our_measurement_disagree_on_give_and_both_are_reported() -> None:
    """50.550% against 59.418%: an 8.9pp gap that blending would have hidden."""
    vendor = lc.vendor_share(give_vendor(), SUPPLY).value
    ours = next(iter(lc.launch_wave(give_events(), SUPPLY, (34,)))).pct.value
    assert abs(ours - vendor) > Decimal(8)


def test_vendor_share_is_unknown_without_a_denominator_or_a_cross_check() -> None:
    assert lc.vendor_share(give_vendor(), None).value is None
    assert lc.vendor_share(None, SUPPLY).value is None


# --------------------------------------------------------------------------------------
# the headline: no double counting, no number above 100%
# --------------------------------------------------------------------------------------


def _overlapping_tape() -> tuple[lc.TradeEvent, ...]:
    """One wallet deliberately placed in all three components at once.

    ``both`` buys in the first second (wave), enters and exits on the same seconds as
    ``twin`` (co-timed), and its exit is re-bought by a fresh wallet inside the relay
    window (relay). If any arithmetic path counts a wallet per component rather than once,
    this tape is where it shows.
    """
    return lc.normalise_events(
        [
            lc.TradeEvent("both", 0, Side.BUY, 20 * PCT, "1"),
            lc.TradeEvent("twin", 0, Side.BUY, 20 * PCT, "2"),
            lc.TradeEvent("both", 40, Side.SELL, 20 * PCT, "3"),
            lc.TradeEvent("twin", 40, Side.SELL, 20 * PCT, "4"),
            lc.TradeEvent("fresh", 41, Side.BUY, 40 * PCT, "5"),
        ]
    )


def test_headline_counts_a_wallet_in_three_components_exactly_once() -> None:
    """The union is a set, so the wallet contributes one term. Asserted, not assumed."""
    events = _overlapping_tape()
    wave = lc.launch_wave(events, SUPPLY, lc.WAVE_WINDOWS_S)
    cohorts = lc.cotimed_cohorts(events)
    relays = lc.relay_events(events)
    value, union, _ = lc.headline(events, SUPPLY, wave_points=wave, cohorts=cohorts)

    assert "both" in union
    assert len(union) == len(set(union))
    component_sum = (
        wave[-1].pct.value
        + pct_of_supply(lc.cotimed_atoms(cohorts))
        + pct_of_supply(lc.relay_total(relays))
    )
    assert component_sum > Decimal(100)
    assert value.value == Decimal(40)


def test_headline_is_unchanged_when_a_component_repeats_a_wallet() -> None:
    """Feeding the same wallet twice through two cohorts must not move the number."""
    events = _overlapping_tape()
    wave = lc.launch_wave(events, SUPPLY, lc.WAVE_WINDOWS_S)
    honest = lc.cotimed_cohorts(events)
    duplicated = (*honest, *honest)
    first, _, _ = lc.headline(events, SUPPLY, wave_points=wave, cohorts=honest)
    second, _, _ = lc.headline(events, SUPPLY, wave_points=wave, cohorts=duplicated)
    assert first.value == second.value


def test_headline_never_exceeds_the_all_wallet_peak() -> None:
    """The bound that makes 100% a proof: the union is a subset of everybody."""
    events = give_events()
    wave = lc.launch_wave(events, SUPPLY, lc.WAVE_WINDOWS_S)
    cohorts = lc.cotimed_cohorts(events)
    value, union, _ = lc.headline(events, SUPPLY, wave_points=wave, cohorts=cohorts)
    everyone, _ = lc.peak_concurrent_atoms(events, {e.wallet for e in events})
    assert set(union) <= {e.wallet for e in events}
    assert value.value <= pct_of_supply(everyone)


def test_headline_stays_at_100_percent_when_supply_passes_through_three_hands() -> None:
    """Supply recycled through three wallets is 300% of turnover and 100% of supply.

    This is exactly the relay. A headline built on cumulative buying would read 300%; the
    union's peak simultaneous holding reads 100%, which is the quantity that can actually
    be dumped on the operator.
    """
    events = lc.normalise_events(
        [
            lc.TradeEvent("a", 0, Side.BUY, 100 * PCT, "1"),
            lc.TradeEvent("a", 10, Side.SELL, 100 * PCT, "2"),
            lc.TradeEvent("b", 11, Side.BUY, 100 * PCT, "3"),
            lc.TradeEvent("b", 20, Side.SELL, 100 * PCT, "4"),
            lc.TradeEvent("c", 21, Side.BUY, 100 * PCT, "5"),
        ]
    )
    ok, _ = lc.ledger_closes(events, SUPPLY)
    assert ok
    wave = lc.launch_wave(events, SUPPLY, lc.WAVE_WINDOWS_S)
    value, _, _ = lc.headline(events, SUPPLY, wave_points=wave, cohorts=lc.cotimed_cohorts(events))
    assert wave[-1].pct.value == Decimal(300)
    assert value.value == Decimal(100)


def test_peak_concurrent_floors_each_wallet_at_zero() -> None:
    """A wallet cannot be given a phantom short by selling what we never saw it buy."""
    events = lc.normalise_events(
        [
            lc.TradeEvent("ghost", 0, Side.SELL, 50 * PCT, "1"),
            lc.TradeEvent("real", 1, Side.BUY, 10 * PCT, "2"),
        ]
    )
    peak, at = lc.peak_concurrent_atoms(events, {"ghost", "real"})
    assert peak == 10 * PCT
    assert at == 1


def test_peak_concurrent_of_an_empty_set_is_zero_atoms_and_no_instant() -> None:
    assert lc.peak_concurrent_atoms(give_events(), ()) == (0, None)


# --------------------------------------------------------------------------------------
# the ledger gate
# --------------------------------------------------------------------------------------


def test_ledger_rejects_a_tape_whose_wallets_hold_more_than_supply() -> None:
    """An identity, not a tolerance. This is what bounds the headline at 100%."""
    events = lc.normalise_events(
        [
            lc.TradeEvent("a", 0, Side.BUY, 80 * PCT, "1"),
            lc.TradeEvent("b", 1, Side.BUY, 80 * PCT, "2"),
        ]
    )
    ok, reason = lc.ledger_closes(events, SUPPLY)
    assert not ok
    assert "impossible" in reason


def test_ledger_rejects_a_tape_with_large_unexplained_sells() -> None:
    events = lc.normalise_events(
        [
            lc.TradeEvent("a", 0, Side.BUY, PCT, "1"),
            lc.TradeEvent("a", 5, Side.SELL, 30 * PCT, "2"),
        ]
    )
    ok, reason = lc.ledger_closes(events, SUPPLY)
    assert not ok
    assert "sold" in reason
    assert lc.oversold_atoms(events) == 29 * PCT


def test_give_vendor_reconstruction_does_not_pass_the_ledger_gate() -> None:
    """Stated in the module docstring and asserted here rather than left as a caveat.

    A per-wallet vendor aggregate is not a tape. It reproduces the wave, the cohorts and
    the relay pair, and it cannot support a per-wallet attribution, so the gate refuses it.
    """
    ok, reason = lc.ledger_closes(give_events(), SUPPLY)
    assert not ok
    assert "sold" in reason or "impossible" in reason


def test_ledger_refuses_without_a_denominator() -> None:
    ok, reason = lc.ledger_closes(give_events(), 0)
    assert not ok
    assert "supply" in reason


# --------------------------------------------------------------------------------------
# reading the tape: a full measured token, end to end
# --------------------------------------------------------------------------------------

#: The synthetic launch every database test below uses, as
#: ``(second, wallet, side, percent of supply)``.
#:
#: Built to exercise all three components at once and to reconcile against the curve
#: exactly: 24% of supply is bought and 9% is sold back, so the curve must report 15% of
#: supply gone. ``w1`` is relayed into ``w6``; ``w7`` and ``w8`` are a co-timed pair;
#: ``w5`` arrives at t+200 s and is outside every wave window.
TAPE: tuple[tuple[int, str, Side, int], ...] = (
    (0, "w1", Side.BUY, 5),
    (1, "w2", Side.BUY, 4),
    (2, "w3", Side.BUY, 3),
    (5, "w7", Side.BUY, 2),
    (5, "w8", Side.BUY, 2),
    (10, "w1", Side.SELL, 5),
    (11, "w6", Side.BUY, 5),
    (30, "w7", Side.SELL, 2),
    (30, "w8", Side.SELL, 2),
    (40, "w4", Side.BUY, 2),
    (200, "w5", Side.BUY, 1),
)

NET_OUT_PCT = sum(p if s is Side.BUY else -p for _, _, s, p in TAPE)


def _token(conn: sqlite3.Connection, *, created_ms: int | None = CREATED_MS) -> None:
    conn.execute(
        "INSERT INTO tokens (chain, address, creator, created_ms, launchpad, first_seen_ms) "
        "VALUES (?,?,?,?,?,?)",
        (SOL.value, TOKEN, CREATOR, created_ms, "pump.fun", created_ms or 0),
    )


def _tape_row(conn: sqlite3.Connection, *, coverage: str = "complete") -> None:
    proof = "walked back past creation" if coverage == "complete" else None
    conn.execute(
        "INSERT OR REPLACE INTO token_tape (chain, token, model, coverage, route, proof, "
        " reason, covered_from_ms, covered_to_ms, created_ms, first_seen_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            TOKEN,
            "test",
            coverage,
            "pumpfun:trades",
            proof,
            "fixture",
            CREATED_MS - 1_000,
            CREATED_MS + 300_000,
            CREATED_MS if coverage == "complete" else None,
            CREATED_MS,
            CREATED_MS,
        ),
    )


def _snapshot(
    conn: sqlite3.Connection,
    *,
    net_out_pct: int = NET_OUT_PCT,
    observed_ms: int = CREATED_MS + 200_000,
    invariant: int = PUMPFUN_CURVE_INVARIANT_ATOMS,
    coverage_from_ms: int | None = CREATED_MS - 1_000,
) -> None:
    real = PUMPFUN_LAUNCH_CURVE_ATOMS - net_out_pct * PCT
    conn.execute(
        "INSERT OR REPLACE INTO curve_snapshots (chain, token, observed_ms, real_sol_lamports, "
        " virtual_sol_lamports, real_token_atoms, virtual_token_atoms, coverage_from_ms, "
        " created_ms, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            TOKEN,
            observed_ms,
            0,
            30_000_000_000,
            str(real),
            str(real + invariant),
            coverage_from_ms,
            CREATED_MS,
            "pumpfun",
        ),
    )


def _swap(conn: sqlite3.Connection, wallet: str, second: int, side: Side, percent: int) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, slot, block_index, ts_ms, wallet, token, side, "
        " amount_token, amount_native, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            SOL.value,
            f"tx{wallet}{second}{side.value}",
            1_000 + second,
            second,
            CREATED_MS + second * 1_000,
            wallet,
            TOKEN,
            side.value,
            str(percent * PCT),
            "1000000",
            "pumpfun:trades",
        ),
    )


@pytest.fixture
def launch(tmp_db: sqlite3.Connection) -> sqlite3.Connection:
    """A mint whose tape is anchored, reconciles exactly, and closes its ledger."""
    _token(tmp_db)
    _tape_row(tmp_db)
    _snapshot(tmp_db)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    return tmp_db


def test_measure_reports_every_component_on_a_clean_tape(launch: sqlite3.Connection) -> None:
    report = lc.measure(SOL, TOKEN, launch)
    assert report.basis is lc.Basis.MEASURED
    assert report.gate is lc.Gate.NONE
    assert report.reason  # populated on success too, not only on refusal
    assert report.supply_atoms == SUPPLY
    assert report.coverage.value == Decimal(1)
    assert report.wave_curve() == (
        (1, Decimal(9)),  # w1 + w2
        (5, Decimal(16)),  # + w3, w7, w8
        (15, Decimal(21)),  # + w6, the relay's fresh re-buyer
        (30, Decimal(21)),
        (60, Decimal(23)),  # + w4; w5 at t+200 is outside every window
    )
    assert report.launch_wave_pct.value == Decimal(23)
    assert report.cotimed_pct.value == Decimal(4)
    assert report.relay_pct.value == Decimal(7)
    assert report.headline_pct.value == Decimal(16)


def test_measure_headline_is_below_the_sum_of_its_components(launch: sqlite3.Connection) -> None:
    """23 + 4 + 7 = 34 if they were added. The union scores 16."""
    report = lc.measure(SOL, TOKEN, launch)
    total = report.launch_wave_pct.value + report.cotimed_pct.value + report.relay_pct.value
    assert total == Decimal(34)
    assert report.headline_pct.value == Decimal(16)
    assert len(report.headline_wallets) == len(set(report.headline_wallets)) == 7


def test_measure_finds_the_relay_and_the_cohort_on_the_tape(launch: sqlite3.Connection) -> None:
    report = lc.measure(SOL, TOKEN, launch)
    assert [(c.entry_s, c.exit_s, c.size) for c in report.cotimed] == [(5, 30, 2)]
    assert [(r.exit_s, r.matched_atoms // PCT) for r in report.relays] == [(10, 5), (30, 2)]
    relay = report.relays[0]
    assert relay.sellers == ("w1",)
    assert relay.buyers == ("w6",)


def test_measure_does_not_read_the_vendor(launch: sqlite3.Connection) -> None:
    """Supplying a cross-check must change the record and nothing that computes a number."""
    plain = lc.measure(SOL, TOKEN, launch)
    crossed = lc.measure(SOL, TOKEN, launch, vendor=give_vendor())
    assert crossed.vendor_bundler_pct.value is not None
    assert plain.vendor_bundler_pct.value is None
    for name in ("launch_wave_pct", "cotimed_pct", "relay_pct", "headline_pct"):
        assert getattr(plain, name).value == getattr(crossed, name).value
    assert plain.headline_wallets == crossed.headline_wallets


def test_measure_returns_decimals_not_floats(launch: sqlite3.Connection) -> None:
    """Money and shares of it never round-trip through a float."""
    report = lc.measure(SOL, TOKEN, launch)
    for measure in (report.headline_pct, report.cotimed_pct, report.relay_pct, report.coverage):
        assert isinstance(measure.value, Decimal)
    assert all(isinstance(point.bought_atoms, int) for point in report.wave)


def test_measure_says_counts_are_addresses_not_entities(launch: sqlite3.Connection) -> None:
    """The one caveat a reader must not miss, carried on the report rather than in a doc."""
    report = lc.measure(SOL, TOKEN, launch)
    assert any("ADDRESSES, not entities" in note for note in report.notes)


# --------------------------------------------------------------------------------------
# every refusal is None and UNAVAILABLE, never 0
# --------------------------------------------------------------------------------------


def _assert_refused(report: lc.LaunchConcentrationReport, gate: lc.Gate) -> None:
    """One helper so no refusal path can quietly start returning a number."""
    assert report.basis is lc.Basis.UNAVAILABLE
    assert report.gate is gate
    assert report.reason
    for measure in (
        report.launch_wave_pct,
        report.cotimed_pct,
        report.relay_pct,
        report.headline_pct,
    ):
        assert measure.value is None, "a refusal must be None, never 0"
        assert measure.basis is EvidenceBasis.UNAVAILABLE
        assert not measure.known
    assert report.wave == ()
    assert set(report.unknowns) >= {"launch_wave_pct", "headline_pct"}


def test_a_token_with_no_tape_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    """GIVE's real state in our database: zero swap rows. The answer is not 0% bundled."""
    _token(tmp_db)
    tmp_db.commit()
    _assert_refused(lc.measure(SOL, TOKEN, tmp_db), lc.Gate.THIN)


def test_an_unanchored_tape_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    _token(tmp_db)
    _tape_row(tmp_db, coverage="partial")
    _snapshot(tmp_db, coverage_from_ms=CREATED_MS + 60_000)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    _assert_refused(report, lc.Gate.ANCHORED)
    assert "complete back to launch" in report.reason


def test_a_tape_that_does_not_reconcile_is_unavailable_not_zero(
    tmp_db: sqlite3.Connection,
) -> None:
    """The gate that catches present rows carrying wrong amounts.

    On the live corpus this is 18 of 400 mints, one disagreeing by 6,696x. Without it each
    would have produced a confident three-digit launch share.
    """
    _token(tmp_db)
    _tape_row(tmp_db)
    _snapshot(tmp_db, net_out_pct=NET_OUT_PCT * 3)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    _assert_refused(report, lc.Gate.RECONCILED)
    assert report.coverage.value is None or report.coverage.value < lc.MIN_COVERAGE


def test_reconciliation_survives_the_snapshot_clock_skew(tmp_db: sqlite3.Connection) -> None:
    """``observed_ms`` is our read clock, not the chain's, and the gap is measured.

    Cutting the tape exactly at ``observed_ms`` reconciles 138 of 400 live sol mints;
    searching within +/-15 s reconciles 304. Here the snapshot lands five seconds after the
    last trade, which is the ordinary case and which a zero tolerance refuses outright.
    """
    _token(tmp_db)
    _tape_row(tmp_db)
    _snapshot(tmp_db, observed_ms=CREATED_MS + 205_000)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    assert report.basis is lc.Basis.MEASURED
    assert report.coverage.value == Decimal(1)
    assert report.reconciled_to_s == 200


def test_no_curve_snapshot_means_coverage_is_unknown_not_full(
    tmp_db: sqlite3.Connection,
) -> None:
    """"We could not look" and "we looked and the tape is complete" must differ."""
    _token(tmp_db)
    _tape_row(tmp_db)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    assert report.basis is lc.Basis.UNAVAILABLE
    assert report.coverage.value is None
    assert report.gate in (lc.Gate.SUPPLY, lc.Gate.RECONCILED)


def test_an_impossible_ledger_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    """Amounts that reconcile in aggregate and are impossible per wallet."""
    _token(tmp_db)
    _tape_row(tmp_db)
    big = (
        (0, "a", Side.BUY, 80),
        (1, "b", Side.BUY, 80),
        (2, "a", Side.SELL, 80),
        (3, "b", Side.SELL, 65),
    )
    for second, wallet, side, percent in big:
        _swap(tmp_db, wallet, second, side, percent)
    for index in range(1, 8):
        _swap(tmp_db, f"f{index}", 10 + index, Side.BUY, index)
    net = sum(p if s is Side.BUY else -p for _, _, s, p in big) + sum(range(1, 8))
    _snapshot(tmp_db, net_out_pct=net, observed_ms=CREATED_MS + 17_000)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    _assert_refused(report, lc.Gate.LEDGER)
    assert "impossible" in report.reason


def test_a_non_standard_curve_is_unavailable_not_zero(tmp_db: sqlite3.Connection) -> None:
    """Without the invariant there is no launch reserve, so nothing to reconcile against."""
    _token(tmp_db)
    _tape_row(tmp_db)
    _snapshot(tmp_db, invariant=PUMPFUN_CURVE_INVARIANT_ATOMS + 1)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    assert report.basis is lc.Basis.UNAVAILABLE
    assert report.coverage.value is None


def test_a_refusal_still_reports_what_it_saw(tmp_db: sqlite3.Connection) -> None:
    """A refusal is a claim about our collection, so it carries the collection facts."""
    _token(tmp_db)
    _tape_row(tmp_db, coverage="partial")
    _snapshot(tmp_db, coverage_from_ms=CREATED_MS + 60_000)
    for second, wallet, side, percent in TAPE:
        _swap(tmp_db, wallet, second, side, percent)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    assert report.events_seen == len(TAPE)
    assert report.wallets_seen == 8
    assert report.first_trade_ms == CREATED_MS
    assert "unavailable" in report.summary()


# --------------------------------------------------------------------------------------
# tape parsing
# --------------------------------------------------------------------------------------


def test_from_rows_quantises_to_seconds_and_drops_unreadable_amounts() -> None:
    rows = [
        {"ts_ms": 1_000_500, "wallet": "a", "side": "buy", "amount_token": "5", "tx": "1"},
        {"ts_ms": 1_000_900, "wallet": "b", "side": "sell", "amount_token": "3", "tx": "2"},
        {"ts_ms": 1_001_000, "wallet": "c", "side": "buy", "amount_token": None, "tx": "3"},
        {"ts_ms": None, "wallet": "d", "side": "buy", "amount_token": "9", "tx": "4"},
        {"ts_ms": 1_002_000, "wallet": "", "side": "buy", "amount_token": "9", "tx": "5"},
    ]
    events = lc.from_rows(rows)
    assert [(e.wallet, e.ts_s, e.side, e.atoms) for e in events] == [
        ("b", 1_000, Side.SELL, 3),
        ("a", 1_000, Side.BUY, 5),
    ]


def test_normalise_is_stable_and_first_trade_is_explicit_about_emptiness() -> None:
    events = give_events()
    assert lc.normalise_events(events) == events
    assert lc.first_trade_second(events) == 0
    assert lc.first_trade_second(()) is None


def test_rebase_puts_the_launch_at_second_zero_and_preserves_every_gap() -> None:
    """"t+12" has to mean twelve seconds after the launch, whatever epoch the tape is in."""
    shifted = lc.normalise_events(
        [
            lc.TradeEvent("a", 1_700_000_000, Side.BUY, PCT, "1"),
            lc.TradeEvent("a", 1_700_000_012, Side.SELL, PCT, "2"),
        ]
    )
    rebased = lc.rebase_to_first_trade(shifted)
    assert [e.ts_s for e in rebased] == [0, 12]
    assert lc.rebase_to_first_trade(rebased) == rebased
    assert lc.rebase_to_first_trade(()) == ()


def test_components_are_frame_independent_so_rebasing_only_moves_the_labels() -> None:
    base = _overlapping_tape()
    shifted = lc.normalise_events(
        [lc.TradeEvent(e.wallet, e.ts_s + 1_700_000_000, e.side, e.atoms, e.tx) for e in base]
    )
    assert [c.size for c in lc.cotimed_cohorts(base)] == [
        c.size for c in lc.cotimed_cohorts(shifted)
    ]
    assert lc.relay_total(lc.relay_events(base)) == lc.relay_total(lc.relay_events(shifted))
    assert lc.cotimed_cohorts(lc.rebase_to_first_trade(shifted)) == lc.cotimed_cohorts(base)


def test_no_rows_at_all_is_reported_as_a_fact_about_our_collection(
    tmp_db: sqlite3.Connection,
) -> None:
    """GIVE's real state: the refusal must not read as a statement about the token."""
    _token(tmp_db)
    tmp_db.commit()
    report = lc.measure(SOL, TOKEN, tmp_db)
    assert "no swap rows at all" in report.reason
    assert "says nothing whatever about the token" in report.reason


# --------------------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------------------

#: Constants that are not decision boundaries: identifiers, arithmetic helpers, and the
#: freshness budget, which is a cache policy.
_NOT_THRESHOLDS = {"ZERO", "ONE", "HUNDRED", "FRESHNESS_BUDGET_S"}


def test_every_threshold_declares_its_provenance() -> None:
    """A silently added knob fails the build, the way bundles.py and triage.py do it."""
    numeric = {
        name
        for name, value in vars(lc).items()
        if name.isupper()
        and not isinstance(value, bool)
        and (
            isinstance(value, int | Decimal)
            or (isinstance(value, tuple) and value and all(isinstance(v, int) for v in value))
        )
    }
    undeclared = numeric - set(lc.THRESHOLD_PROVENANCE) - _NOT_THRESHOLDS
    assert not undeclared, f"thresholds with no provenance entry: {sorted(undeclared)}"
    stale = set(lc.THRESHOLD_PROVENANCE) - numeric
    assert not stale, f"provenance entries for constants that no longer exist: {sorted(stale)}"


def test_every_threshold_says_measured_or_invented() -> None:
    """Anything unmeasured must use the word, so nobody quotes it as evidence later."""
    for name, text in lc.THRESHOLD_PROVENANCE.items():
        assert any(
            word in text
            for word in ("INVENTED", "MEASURED", "DERIVED", "DEFINITIONAL", "STRUCTURAL")
        ), f"{name} does not classify its own provenance"


def test_the_invented_thresholds_say_what_would_settle_them() -> None:
    """An INVENTED number with no route to a measured one is a magic number with a label."""
    for name, text in lc.THRESHOLD_PROVENANCE.items():
        if "INVENTED" not in text:
            continue
        assert any(
            word in text
            for word in ("Raising", "Lowering", "Widening", "swept", "measured", "correct value")
        ), f"{name} is INVENTED and does not say what would settle it"


def test_model_id_is_versioned() -> None:
    assert lc.MODEL_ID.startswith("kaiba-launch-concentration-v")
