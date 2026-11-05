"""A smart-money lane may only count wallets that are actually in the tape it reads.

These are chain-agnostic properties of ``lanes.sm_trenches``, ``scanner._smart_flow_work``
and ``discover.cohort_wallets``. The chain below is ``robinhood`` only because it gives a
convenient EVM address shape; nothing here is a claim about robinhood's real data.

CORRECTION, 2026-09-22. An earlier version of this file told a robinhood story that is
FALSE ON THE PRODUCTION BOX. It was written from ``data/kaiba.db``, which is stale and
partial. Recording both columns, because the gap is the lesson:

                                              data/kaiba.db      production
    robinhood smart-tagged wallets in tape        3 of 820    1,476 of 1,476 (100%)
    max smart wallets buying one token                   1                      111
    robinhood swaps, source ``gmgn:smartmoney``          0                   40,494
    robinhood swaps, source ``gmgn:kol``                 0                    3,843
    robinhood swaps, source ``robinhood``          449,595                  427,918

The local copy holds none of the GMGN-sourced rows at all, which made
``discover.cohort_wallets`` look like it had nothing to offer on robinhood and made the
cohort read as disconnected. **It is not disconnected**: the maximum is 111 against a
threshold of 3, and robinhood traded live the same day (2 buys and 3 sells filled inside
one hour). Do not re-derive chain-level facts from ``data/kaiba.db``; two agents reached
confident wrong conclusions from it on the same day.

What survives that correction, and is what this file actually pins:

* a cohort that is large but absent from THIS token's buys contributes no conviction, and
  no lower threshold may rescue it -- conviction is what wallets did here, not how many
  names we hold;
* the lane honours its own ``min_smart_degen`` even when every downstream gate would pass;
* the feeder's SQL ``LIKE`` and the lane's set-membership select the same wallet, so the
  work source and the lane cannot disagree about who counts;
* ``cohort_wallets`` is keyed on ``swaps.source``, so a chain is screenable exactly when
  its tape carries :data:`discover.COHORT_FEED_SOURCES` rows -- and a pass that considered
  nothing must SAY so rather than read as "screened, admitted no one".

One hypothesis that must stay closed: making the feeder or ``lanes._tags`` strip the
``gmgn:`` namespace is NOT a fix. That namespace is a deliberate quarantine --
``tracker._write_wallet_tags``: "a bare label there would be read as a screen result" --
so stripping it would feed unscreened vendor labels straight into the smart count of a
lane that is ``mode: live``. It would raise the signal rate by inventing conviction.

Every test below builds its own synthetic rows, so none of them depends on any database
on any box.
"""

from __future__ import annotations

from kaiba.core.schemas import Chain, Lane, now_ms
from kaiba.execution import lanes as lanes_mod
from kaiba.execution import scanner
from kaiba.intelligence import discover, tracker

#: An EVM token and wallets, lowercase the way both tables store them. The chain is a
#: vehicle for the property, not a claim about that chain's data.
CHAIN = Chain.ROBINHOOD
TOKEN = "0xf2f1669c5f0dd0e4e97a1578cf7d90389017b039"
SMART = [f"0x{i:040x}" for i in range(1, 6)]
CROWD = [f"0x{i:040x}" for i in range(100, 106)]

#: A source that is NOT one the cohort screen reads. On production robinhood's tape
#: carries this AND ``gmgn:smartmoney``; here it stands for "a tape the screen cannot see".
NON_COHORT_SOURCE = "robinhood"
#: What :data:`discover.COHORT_FEED_SOURCES` requires.
COHORT_SOURCE = "gmgn:smartmoney"

SM_PARAMS = {
    "min_smart_degen": 3,
    "min_independent_entities": 2,
    "max_rug_ratio": 0.3,
    "window_s": 300,
    "min_buy_usd": 0,
    # Explicitly off, because this file is about whether the smart cohort's trades REACH
    # the lane at all -- these contexts carry no dossier by design. The liquidity floor
    # added 2026-09-23 is real and gates live entries; it has its own tests in
    # tests/test_liquidity_floor.py rather than silently refusing every case here.
    "min_liquidity_usd": 0,
    # Off for the same reason, and it is the same shape of gate: sm-trenches ships a
    # holder floor of 200 (config/risk.yaml) and an UNKNOWN holder count refuses, so
    # with no dossier here every case would refuse before reaching the tag logic this
    # file exists to test. The floor has its own tests in tests/test_holder_floor.py.
    "min_holder_count": 0,
    # Same reasoning: this file is about whether the smart cohort's trades REACH the lane,
    # and its contexts carry no token_meta. Without this the launchpad policy would read
    # every fixture as a manual deploy and raise the bar to 5, refusing every case for a
    # reason that has nothing to do with what is being tested. The policy has its own
    # tests in tests/test_launchpad_policy.py.
    "require_launchpad": False,
}


def _tag(conn, address: str, *, chain: Chain = CHAIN, tag: str = "top_trader") -> None:
    """A smart-tagged wallets row, the shape a wallet import writes."""
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, first_seen_ms, last_seen_ms, "
        "tags_json, source, cohort) VALUES (?,?,?,?,?,?,?)",
        (chain.value, address, now_ms(), now_ms(), f'["{tag}"]', "operator:import", "research"),
    )


def _swap(
    conn,
    wallet: str,
    token: str = TOKEN,
    *,
    chain: Chain = CHAIN,
    side: str = "buy",
    age_s: int = 10,
    usd: str = "500",
    source: str = NON_COHORT_SOURCE,
) -> None:
    conn.execute(
        "INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, "
        "source) VALUES (?,?,?,?,?,?,?,?,?)",
        (chain.value, f"tx_{wallet}_{token}_{side}_{age_s}", now_ms() - age_s * 1000, wallet,
         token, side, "1000", usd, source),
    )


def _ctx(conn, buys: list[dict]) -> lanes_mod.LaneContext:
    return lanes_mod.LaneContext(
        chain=CHAIN, token=TOKEN, conn=conn, recent_buys=buys, params=dict(SM_PARAMS)
    )


def _recent(conn) -> list[dict]:
    return scanner.load_recent_buys(CHAIN, TOKEN, conn, since_ms=now_ms() - 1800 * 1000)


# ---------------------------------------------------------------- the lane itself


def test_sm_trenches_fires_when_the_smart_cohort_actually_trades_the_tape(tmp_db):
    """Positive control. Without this, the refusals below could be a broken fixture."""
    for address in SMART[:3]:
        _tag(tmp_db, address)
        _swap(tmp_db, address)
    tmp_db.commit()

    signal = lanes_mod.sm_trenches(_ctx(tmp_db, _recent(tmp_db)))

    assert signal is not None
    assert signal.lane is Lane.SM_TRENCHES
    assert len(signal.wallets) == 3


def test_sm_trenches_refuses_when_the_smart_cohort_never_appears_in_the_tape(tmp_db):
    """A cohort that is large but absent from this token's buys earns nothing.

    The lane counts smart wallets *among this token's net buyers*. Holding more names than
    the positive control does not substitute for any of them having bought: conviction is
    what wallets did here.
    """
    for address in SMART:  # a bigger cohort than the positive control has
        _tag(tmp_db, address)
    for address in CROWD:  # real volume on the token, from untagged wallets
        _swap(tmp_db, address)
    tmp_db.commit()

    assert tmp_db.execute(
        "SELECT COUNT(*) FROM wallets WHERE tags_json LIKE '%top_trader%'"
    ).fetchone()[0] == len(SMART)
    assert tmp_db.execute("SELECT COUNT(*) FROM swaps").fetchone()[0] >= 6

    assert lanes_mod.sm_trenches(_ctx(tmp_db, _recent(tmp_db))) is None


def test_the_lane_honours_its_own_minimum_when_nothing_else_would_refuse(tmp_db):
    """Two smart buyers, ``min_independent_entities`` already satisfied at 2: the only
    thing left between this token and a signal is ``min_smart_degen``, and it must refuse.

    Isolating the gate matters because the entity count is derived from the SAME list, so
    in every other arrangement an empty or short smart set is also refused downstream. A
    mutant that disables this gate alone survives those tests and is caught here.
    """
    for address in SMART[:2]:
        _tag(tmp_db, address)
        _swap(tmp_db, address)
    tmp_db.commit()

    ctx = lanes_mod.LaneContext(
        chain=CHAIN, token=TOKEN, conn=tmp_db, recent_buys=_recent(tmp_db),
        params={**SM_PARAMS, "min_smart_degen": 3, "min_independent_entities": 2},
    )
    assert lanes_mod.sm_trenches(ctx) is None

    # ... and the same two wallets DO fire once the lane's own minimum is met, so the
    # refusal above is the threshold and not a broken fixture.
    relaxed = lanes_mod.LaneContext(
        chain=CHAIN, token=TOKEN, conn=tmp_db, recent_buys=_recent(tmp_db),
        params={**SM_PARAMS, "min_smart_degen": 2, "min_independent_entities": 2},
    )
    assert lanes_mod.sm_trenches(relaxed) is not None


def test_no_lower_threshold_manufactures_conviction_from_an_absent_cohort(tmp_db):
    """With no tagged wallet among the buyers, 3, 2 and 1 must all refuse.

    Not a claim that any chain is in this state -- it is the guard that a future retune
    cannot turn "nobody we track bought this" into a signal by lowering a number.
    """
    for address in SMART:
        _tag(tmp_db, address)
    for address in CROWD:
        _swap(tmp_db, address)
    tmp_db.commit()

    buys = _recent(tmp_db)
    for threshold in (3, 2, 1):
        ctx = lanes_mod.LaneContext(
            chain=CHAIN, token=TOKEN, conn=tmp_db, recent_buys=buys,
            params={**SM_PARAMS, "min_smart_degen": threshold, "min_independent_entities": 1},
        )
        assert lanes_mod.sm_trenches(ctx) is None, f"fired at min_smart_degen={threshold}"


# ---------------------------------------------------------------- the feeder agrees


def test_the_smart_flow_feeder_offers_nothing_when_no_tracked_wallet_bought(tmp_db):
    """The work source built to find sm-trenches candidates agrees with the lane."""
    for address in SMART:
        _tag(tmp_db, address)
    for address in CROWD:
        _swap(tmp_db, address)
    tmp_db.commit()

    assert scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG) == []


def test_the_feeder_and_the_lane_read_the_same_tag_spelling(tmp_db):
    """The feeder's SQL ``LIKE`` and the lane's set-membership must select the same wallet.

    A bare ``top_trader`` is seen by both. (``lanes._tags`` does not strip a vendor
    namespace while ``lanes._row_tags`` does. That asymmetry is deliberate -- see the
    module docstring on the ``gmgn:`` quarantine -- and is not changed here.)
    """
    for address in SMART[:3]:
        _tag(tmp_db, address)
        _swap(tmp_db, address)
    tmp_db.commit()

    offered = scanner._smart_flow_work(tmp_db, 10, config=scanner.DEFAULT_CONFIG)
    assert [item.token for item in offered] == [TOKEN]

    signal = lanes_mod.sm_trenches(_ctx(tmp_db, _recent(tmp_db)))
    assert signal is not None
    assert sorted(signal.wallets) == sorted(SMART[:3])


# ------------------------------------------------- what makes a chain screenable at all


def test_the_cohort_screen_is_keyed_on_the_swap_source(tmp_db):
    """``cohort_wallets`` reads ``source IN COHORT_FEED_SOURCES`` and nothing else.

    So a chain can be screened exactly when its tape carries those rows. This is the
    property; whether a given chain carries them is a question for the production box, not
    for this test (and on production, robinhood does -- 40,494 ``gmgn:smartmoney`` rows).
    """
    assert NON_COHORT_SOURCE not in discover.COHORT_FEED_SOURCES

    for address in SMART:
        _swap(tmp_db, address, source=NON_COHORT_SOURCE)
    tmp_db.commit()

    assert discover.cohort_wallets(CHAIN, tmp_db) == []


def test_the_same_rows_under_a_cohort_source_are_seen(tmp_db):
    """Discriminator: the gate is ``source``, not the chain and not the addresses."""
    for address in SMART:
        _swap(tmp_db, address, source=COHORT_SOURCE)
    tmp_db.commit()

    found = discover.cohort_wallets(CHAIN, tmp_db)
    assert sorted(w.address for w in found) == sorted(SMART)


def test_seeding_says_it_had_nothing_to_offer_rather_than_reporting_a_clean_pass(tmp_db):
    """The silent-idle guard.

    A pass that considers nothing must SAY nothing was there. An empty report with no note
    reads as "screened, found no one worth admitting" -- indistinguishable from a real
    screen that admitted nobody, which is how a chain with no cohort feed can look healthy.
    """
    for address in SMART:
        _swap(tmp_db, address, source=NON_COHORT_SOURCE)
    tmp_db.commit()

    report = tracker.seed_from_cohorts(CHAIN, tmp_db, dry_run=True)

    assert report.considered == 0
    assert report.admitted == []
    assert report.smart_tags_written == 0
    note = " ".join(report.notes).lower()
    assert "nothing to offer" in note
    assert CHAIN.value in note
