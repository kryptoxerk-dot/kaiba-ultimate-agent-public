"""The live lane must recognise GMGN's OWN spelling for its smart-money cohort.

THE BUG. ``sm_trenches`` counts a buyer as smart through two routes, and both were spelled
in a vocabulary GMGN does not emit:

* ``SMART_TAGS`` = {smart_money, pump_smart, renowned, top_trader};
* ``wallet_scores.archetype in {smart_money, top_trader}``, and ``naming.infer_archetype``
  derives those two *only* from that same tag set.

GMGN labels its smart cohort ``smart_degen`` / ``app_smart_money`` / ``launchpad_smart``
(``discover.GMGN_SMART_COHORT_TAGS``, CITED from the live tape: ``smart_degen`` on 7,315 of
10,334 bsc feed rows). The two sets share NOT ONE label. The bridge was
``tracker.seed_from_cohorts`` writing a bare ``smart_money`` tag on admission, so the lane
saw a wallet only if the tracker had admitted it -- and most feed wallets are refused for
"fewer than 10 observed swaps, so the sell-only filter cannot run".

MEASURED on the live box 2026-09-23. GMGN-sourced wallets carrying a cohort label but no
admission tag: sol 518, robinhood 759, bsc 423. Forward reach-2x for tokens they bought
early, STRATIFIED by token print count (unstratified reach-N is mostly an artefact of
activity -- see the memory note ``reach-n-activity-confound``):

    band        base   ADMITTED   UNTAGGED   UNTAGGED>=2
    sol   5-8    6.1%     2.96x      2.68x     3.69x
    sol  8-16   14.0%     2.64x      2.65x     3.26x
    sol 16-39   18.3%     2.73x      2.63x     3.59x
    rh   17-28  15.0%     2.03x      2.68x     2.59x
    rh   28-69  34.8%     1.22x      1.73x     2.39x

The wallets we ignore predict as well as the ones we count, and two of them together beat
one admitted wallet. On **bsc** neither route predicts (1.29x/1.01x/1.00x/1.07x/1.02x/1.01x
admitted against a base that reaches 80%), which is why the new route is CHAIN-SCOPED
rather than global -- the same lesson as ``solana-rubric-on-evm-chains``.

A CORRECTION, kept because the mistake is the reusable part. The first version of this
change also "fixed" ``_tags`` to strip the vendor namespace, on the reasoning that its
sibling ``_row_tags`` strips it and a literal ``gmgn:smart_money`` therefore matched
nothing. That broke ``tests/test_tracker_tag_leak.py``, which pins the actual rule: the
namespace IS the safety mechanism. ``tracker.seed_from_cohorts`` writes every vendor label
namespaced BEFORE its screen and the bare ``smart_money`` only AFTER a wallet passes, so a
bare tag means "screened" and a namespaced one means "a vendor said so and nobody checked".
Stripping it let every unscreened feed wallet into the smart cohort.

So the cohort route matches the NAMESPACED spelling by name instead
(:data:`lanes.SMART_COHORT_VENDOR_TAGS`). It wants the unscreened claim -- that is the
population measured above, and the screen refuses most of it for want of observed swaps,
not for anything it found -- and asking for ``gmgn:smart_degen`` explicitly keeps that
opt-in from quietly widening every other route.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import HARD_QUARANTINE_TAGS, Chain, WalletTag
from kaiba.execution import lanes
from kaiba.intelligence import discover, tracker
from kaiba.intelligence.naming import vendor_tag
from tests.test_bsc_lane_inputs import BSC, RH, _ctx, _three_smart_buyers, _tok

# ------------------------------------------------------------------ the vocabularies meet


def test_the_lane_knows_gmgns_own_smart_labels():
    """THE REGRESSION: the two vocabularies shared not one label."""
    assert lanes.SMART_COHORT_LABELS, "the lane has no cohort vocabulary again"
    assert discover.GMGN_SMART_COHORT_TAGS <= lanes.SMART_COHORT_LABELS, (
        "the lane does not recognise every label discover.py already calls GMGN's smart "
        "cohort; the two files have drifted apart again"
    )


def test_smart_degen_is_covered_by_name():
    """``smart_degen`` is the label on 7,315 of 10,334 bsc feed rows and names the lane's
    own ``min_smart_degen`` parameter. If anything is in this set, it is this."""
    assert "smart_degen" in lanes.SMART_COHORT_LABELS


def test_the_cohort_route_is_chain_scoped():
    """MEASURED: the label carries 2.6-3.7x on sol/robinhood and ~1.0x on bsc."""
    assert Chain.SOL in lanes.SMART_COHORT_LABEL_CHAINS
    assert Chain.ROBINHOOD in lanes.SMART_COHORT_LABEL_CHAINS
    assert Chain.BSC not in lanes.SMART_COHORT_LABEL_CHAINS, (
        "bsc admitted-wallet lift by activity band was 1.29/1.01/1.00/1.07/1.02/1.01 -- "
        "this label does not predict there and must not count a buyer as smart"
    )


def test_a_cohort_label_is_not_a_wallet_tag():
    """The reason the bug existed: these strings have no WalletTag home."""
    values = {t.value for t in WalletTag}
    assert not (lanes.SMART_COHORT_LABELS & values), (
        "a cohort label became a WalletTag; fold it into SMART_TAGS instead of carrying "
        "it twice"
    )


# ------------------------------------------------------------------ namespace stripping


def test_tags_keeps_the_vendor_namespace():
    """THE LEAK GUARD: a bare tag means screened, a namespaced one means unscreened.

    This is the assertion my first attempt at this change deleted. `_tags` must NOT
    normalise the namespace away, or every unscreened feed wallet carrying
    `gmgn:smart_money` counts toward the smart cohort.
    """
    got = lanes._tags(Chain.SOL, {"tags": ["gmgn:smart_money"], "wallet": "w"}, None)
    assert got == {"gmgn:smart_money"}
    assert "smart_money" not in got


def test_an_unscreened_vendor_smart_tag_does_not_count(tmp_db):
    """The leak itself, at the lane. `gmgn:smart_money` is a vendor claim, not a screen."""
    ctx = _cohort_ctx(tmp_db, RH, token_n=76, tags=["gmgn:smart_money"])
    assert lanes.sm_trenches(ctx) is None, (
        "an unscreened wallet reached the smart cohort through a namespaced SMART_TAG; "
        "see tests/test_tracker_tag_leak.py"
    )


def test_the_cohort_route_matches_the_namespaced_spelling():
    """It reads the unscreened claim ON PURPOSE, and says so by name."""
    assert lanes.SMART_COHORT_VENDOR_TAGS == {
        vendor_tag(label) for label in lanes.SMART_COHORT_LABELS
    }
    assert all(t.startswith("gmgn:") for t in lanes.SMART_COHORT_VENDOR_TAGS)


# ------------------------------------------------------------------ the lane actually fires
#
# These reuse the fixture that GENUINELY FIRES on three seeded buyers, so the tag route is
# the only thing that differs between firing and refusing. A fixture that returns None for
# want of buyers would let every assertion below pass with the route absent.


def _cohort_ctx(conn, chain, *, token_n, tags, seed=True):
    """Three buyers whose ONLY smart evidence is `tags` on the wallets row."""
    token = _tok(token_n)
    _three_smart_buyers(conn, chain, token)
    if seed:
        tracker.seed_from_cohorts(chain, conn)
    conn.execute(
        "UPDATE wallets SET tags_json = ? WHERE chain = ?",
        (__import__("json").dumps(list(tags)), chain.value),
    )
    conn.execute("DELETE FROM wallet_scores WHERE chain = ?", (chain.value,))
    base = _ctx(conn, chain, token, rug_ratio=None)
    return base.model_copy(
        update={"params": {**(base.params or {}), "min_liquidity_usd": 0,
                           "require_launchpad": False}}
    )


def test_the_fixture_fires_on_the_admission_tag(tmp_db):
    """The control. Without this every refusal below could be an absent signal."""
    ctx = _cohort_ctx(tmp_db, BSC, token_n=70, tags=["smart_money"])
    assert lanes.sm_trenches(ctx) is not None, "the fixture does not fire; the tests prove nothing"


@pytest.mark.parametrize("label", sorted(discover.GMGN_SMART_COHORT_TAGS))
def test_a_cohort_label_alone_fires_on_robinhood(tmp_db, label):
    """THE FIX, and the load-bearing test in this file.

    Without this, ``test_a_cohort_label_alone_is_not_enough_on_bsc`` passes when the route
    does not exist at all, and the whole chain-scope pair proves nothing.
    """
    ctx = _cohort_ctx(tmp_db, RH, token_n=74, tags=[f"gmgn:{label}"])
    assert lanes.sm_trenches(ctx) is not None, (
        f"{label} does not count a robinhood buyer as smart; MEASURED 2.68x at 17-28 "
        "prints against a 2.03x admitted route"
    )


def test_a_cohort_label_alone_is_not_enough_on_bsc(tmp_db):
    """CHAIN SCOPE: the label does not predict on bsc, so it must not count there."""
    ctx = _cohort_ctx(tmp_db, BSC, token_n=72, tags=["gmgn:smart_degen"])
    assert lanes.sm_trenches(ctx) is None


@pytest.mark.parametrize("tag", sorted(t.value for t in HARD_QUARANTINE_TAGS))
def test_a_quarantined_wallet_never_counts_on_the_cohort_route(tmp_db, tag):
    """The cohort route bypasses the tracker's screen, so it carries the screen's veto.

    A wallet GMGN calls smart AND calls a sandwich bot is a sandwich bot.

    ON ROBINHOOD, not bsc. The first version of this test ran on bsc, where the cohort
    route is switched off anyway, so it passed whether the guard existed or not and a
    mutation dropping the guard survived the whole file.
    """
    ctx = _cohort_ctx(tmp_db, RH, token_n=73, tags=["gmgn:smart_degen", tag])
    assert lanes.sm_trenches(ctx) is None, f"{tag} did not disqualify the wallet"


def test_the_quarantine_guard_is_what_refuses_it(tmp_db):
    """The control for the test above: the SAME wallet without the bot tag fires.

    Without this, the refusal could be anything -- a bad fixture, a missing buyer -- and
    the guard would still look tested.
    """
    ctx = _cohort_ctx(tmp_db, RH, token_n=75, tags=["gmgn:smart_degen"])
    assert lanes.sm_trenches(ctx) is not None


def test_the_cohort_route_is_additive_not_a_replacement():
    """An admitted wallet must still count through the route it always used."""
    import inspect

    source = inspect.getsource(lanes.sm_trenches)
    assert "SMART_TAGS" in source, "the admission-tag route was removed"
    assert "SMART_COHORT_VENDOR_TAGS" in source, "the cohort route is not wired in"
    assert "SMART_COHORT_LABEL_CHAINS" in source, "the cohort route is not chain-scoped"
    assert "HARD_QUARANTINE_TAGS" in source, "the cohort route lost its bot veto"
