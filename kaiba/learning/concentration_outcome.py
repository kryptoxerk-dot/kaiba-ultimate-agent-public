"""Does launch concentration predict the forward return? Measured on our tape, not assumed.

The sizing work being built alongside this module consumes a *measured concentration* and
scales exposure by it. That is a bet on a relationship nobody in this system has ever
checked: that a launch bought heavily by a coordinated wave goes on to perform worse (or
better) than one that was not. This module exists to check it, and to be readable when the
answer is "it does not replicate", because that answer is the one the sizing piece has to
be told.

Nothing here writes to the database, nothing here is wired into a lane, and nothing here
returns a multiplier. It returns numbers and a verdict.

The feature: the launch **wave**, not the launch **bundle**
-----------------------------------------------------------

``kaiba.intelligence.bundles`` measures a bundle: same slot, contiguous ``block_index``,
at least two distinct entities. That definition is defeated by the evasion the operator
described and the lead reproduced on GIVE
(``12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv``, 2026-09-22, ``gmgn-cli token traders``,
99 traders): buy with a few wallets, **sell**, then re-buy the same supply with a dozen
wallets sharing no funding edge. A relay is neither contiguous in one block nor
entity-linked, so a bundle detector sees nothing, and today every holder-snapshot metric
on that mint reports clean — ``top_10_holder_rate`` 0, 7 holders, 99.59% of supply back in
the curve.

What was *not* invisible on GIVE was the wave: **42 wallets bought 59.418% of supply
inside the first 34 seconds**. That is the quantity this module measures, and it is
deliberately the crudest possible reading of it — every buy in the first
:data:`WAVE_WINDOW_S` seconds, over the supply, counting no entities at all. Entity
resolution is where the evasion wins (``entity_members`` has rows on sol and none on
bsc/robinhood, and the relay is designed to share no funding edge), so the feature under
test here does not depend on it. A launch-concentration module that owns this measurement
is being built in parallel; **as of 2026-09-22 nothing in the tree defines one**, so the
launch-wave component is reimplemented here, and :data:`FEATURE_SOURCE` records which of
the two produced a given run.

Two readings of the same window, because the evasion lives in the difference:

* ``gross_pct`` — every buy in the window over supply. A wallet that buys, sells and
  re-buys counts twice. This is the number the lead measured on GIVE and it can exceed
  100%; a value above 100% *is* the relay, stated as a quantity.
* ``net_pct`` — per wallet, buys minus sells inside the window, floored at zero, summed.
  What the wave still held when the window closed. On a clean launch these two are close;
  on a laundered one ``gross`` runs away from ``net``.

The outcome: a tape-only proxy, and everything wrong with it
-------------------------------------------------------------

Our own closed positions are far too few to answer this (this database: 49 closed trades,
of which **3** are on a mint whose launch wave is measurable at all; the live box: 69 and
**1**). So the primary outcome is a proxy computed from the tape:

    ``ret_vwap_pct(K) = mark(t0 + K minutes) / VWAP(launch window) - 1``

where VWAP is volume-weighted over every trade in the launch window, in native units per
token atom (``amount_native / amount_token``, both base units, no USD leg and therefore no
SOL-price noise), and ``mark`` is the volume-weighted price over the
:data:`MARK_BRACKET_S`-second bracket ending at the horizon, falling back to the last print
at or before the horizon when the mint went silent — which on this asset class is the
normal case and is a real price, not a missing one.

Five biases, all of them working in different directions:

1. **Tape horizon, which is our collection policy and not the market.** A token is only
   measurable at horizon K when the tape provably covers ``t0 + K``. ``token_tape`` is
   deepened by a scheduled revisit that prefers perishable tapes
   (``kaiba.ingest.tape``), so which tokens reach K is decided by our scheduler. At
   K=5 minutes this admits a few hundred mints out of ~4,000; at K=60 it is a few dozen.
2. **Survivorship, partially defeated.** A mint that stops trading is *not* dropped: the
   coverage watermark proves we were still looking, so the last print is carried forward
   and the death shows up as a large negative return. A mint whose tape simply stops is
   dropped. The first is the honest case; the second is the hole.
3. **Discovery selection.** The tape only holds mints something already surfaced. This is
   not a sample of pump.fun launches; it is a sample of launches our hunters liked.
4. **The denominator is contaminated by the feature.** On a bonding curve a heavier launch
   wave *mechanically* pushes the launch-window VWAP further up the curve, so a
   high-concentration token is measured from a higher base. Part of any negative slope is
   this, not prediction. :attr:`Sample.ret_post_pct` re-bases on the last print of the
   launch window instead — the price after the wave has finished buying — and the two are
   reported side by side precisely so that a slope that exists only in the first one can
   be seen for what it is.
5. **No execution.** These are print-to-print ratios with no slippage, no fees and no
   liquidity check. Nobody can buy a 59%-concentrated launch at its VWAP in size.

What it answered, 2026-09-22
----------------------------

Run on both databases we hold, which turned out to cover consecutive and non-overlapping
periods with **zero tokens in common** — a real out-of-sample test rather than a split of
one sample:

* **Local corpus, launches 09-20 07:56 → 09-21 08:30 UTC, n=669 at t+5min.** Median return
  by bucket +0.21 / −15.67 / −20.01 / **−36.61**%, share positive 56.3 → 25.2%. Spearman
  **−0.208**, held-out later half −0.241. Internally, it replicates.
* **Live-box corpus, launches 09-21 08:46 → 09-22 01:41 UTC, n=304 at t+5min.** Same code,
  same chain: +0.21 / +0.26 / +3.66 / **+24.37**%, share positive 58.6 → 62.5%. Spearman
  **+0.197**. The sign is inverted.
* **The control wins.** In the local corpus the plain *count* of wallets buying in the first
  30 s scores −0.244, and the concentration figure's partial correlation once that count is
  removed is **+0.004**. The share of supply carried none of the signal; the number of
  buyers carried all of it. Read that with its limit: the two features are ranked almost
  identically (rho between them 0.863 locally, 0.884 on the live box), so a partial
  correlation has little room to work. The defensible claim is *not* "supply share is
  irrelevant" — it is that this data cannot tell the two apart, and the cheaper number is
  already as good as the expensive one.
* **Six-hour blocks** run −0.153, −0.114, −0.347, −0.170, −0.121/−0.011, +0.357: the sign
  turns inside a single day, and the two corpora agree with each other where they meet.
* **Re-run on the number the sizing piece consumes.**
  ``kaiba.intelligence.launch_concentration`` landed at 09:00 on 2026-09-22, so the study
  was re-run on its output (``--feature ext:*``; on the live box the module is not
  deployed and was injected through stdin rather than installed). Its point-in-time wave
  (``in_window_pct``, W=30 s) scores **−0.445** on the local corpus, replicating inside it
  (train −0.476, test −0.436, n=194) — stronger than the reimplementation here. On the
  live-box corpus the same feature scores **−0.053**, with the earlier half at −0.290 and
  the later half at **+0.177**: a sign flip, and bucket medians of −1.12 / +0.93 / −8.27 /
  −13.05 against the local corpus's +1.59 / −9.97 / −21.85 / −37.32. Its ``headline_pct``
  behaves the same way (−0.312 local, −0.043 live, later half +0.162). Whichever
  definition is used, one day's calibration does not describe the next day.

So the recommendation this module exists to produce is: **flat multipliers**. A sizing rule
that scales on launch concentration would have been backwards for a third of the period it
was measured over. What the measurement is good for is disclosure and refusal — saying that
a mint's first 30 seconds bought 59% of supply across 42 wallets, and saying *unavailable*
when it cannot be established — not a coefficient.

Missing is missing
------------------

Every refusal path returns ``None`` with ``EvidenceBasis.UNAVAILABLE`` and a populated
``reason``. A token we cannot measure is not a token with 0% concentration and not a token
with a 0% return, for the same reason ``bundles`` refuses rather than reporting zero: a
false zero here would land in the sizing piece as "clean launch, size up".
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import random
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one
from kaiba.core.schemas import Chain, EvidenceBasis, Measure, Receipt, now_ms
from kaiba.intelligence.bundles import (
    LAUNCH_ANCHOR_TOLERANCE_MS,
    PER_TOKEN_TAPE_SOURCES,
    SupplyBasis,
    launch_coverage_proved,
    resolve_supply,
)

log = logging.getLogger(__name__)

#: Bump when any definition below changes, so a printed number says which rules made it.
MODEL_ID = "kaiba-concentration-outcome-v1"

#: Which implementation produced the feature. ``reimplemented`` means the launch-wave
#: component was computed here because no launch-concentration module existed in the tree
#: when this ran; if one lands, import it and set this to its ``MODEL_ID``.
FEATURE_SOURCE = "reimplemented:launch-wave-only"

ZERO = Decimal(0)
HUNDRED = Decimal(100)

#: Seconds after the launch in which a buy counts as part of the launch wave.
WAVE_WINDOW_S = 30

#: Forward horizons, in minutes.
FORWARD_HORIZONS_MIN: tuple[int, ...] = (5, 20, 60)

#: Width of the volume-weighted bracket ending at a horizon, in seconds.
MARK_BRACKET_S = 60

#: Trades required inside the launch window before its VWAP is a price rather than a
#: single print wearing a average's clothes.
MIN_WINDOW_TRADES = 2

#: Fraction of launch-window buy *rows* that may be missing ``amount_token`` before the
#: wave measurement is refused outright.
MAX_UNPRICED_ROW_FRACTION = Decimal("0.25")

#: Bucket edges on the concentration axis, in percent. The operator's own policy language
#: ("more than 20% 30%") is what these are cut around.
BUCKET_EDGES: tuple[Decimal, ...] = (Decimal(20), Decimal(35), Decimal(50))
BUCKET_LABELS: tuple[str, ...] = ("<20", "20-35", "35-50", ">50")

#: Samples a bucket needs before its mean and median are printed as numbers rather than
#: as a refusal.
MIN_BUCKET_N = 5

#: Permutation test settings. Fixed seed so two runs of the same tape agree.
PERMUTATION_ITERS = 2000
PERMUTATION_SEED = 20260922

#: What "it replicates out of sample" is taken to mean.
REPLICATION_RHO_MIN = Decimal("0.10")
REPLICATION_P_MAX = Decimal("0.05")

#: Every threshold above, and where it came from. A test asserts this dict covers them
#: all and that each entry opens with MEASURED or INVENTED, the convention
#: ``bundles.THRESHOLD_PROVENANCE`` and ``triage.THRESHOLD_PROVENANCE`` already use.
THRESHOLD_PROVENANCE: dict[str, str] = {
    "WAVE_WINDOW_S": (
        "INVENTED, anchored on one MEASURED case. The only launch wave anyone here has "
        "measured end to end is GIVE on 2026-09-22: 42 wallets, 59.418% of supply, "
        "inside 34 seconds (gmgn-cli token traders, n=99 traders). 30 s is that case "
        "rounded down so the window does not enclose its own answer. n=1 is not a "
        "calibration. What would settle it: sweep the window over the corpus this module "
        "collects and pick the width that maximises out-of-sample rank correlation with "
        "the forward return -- which is exactly the sweep this module refuses to run "
        "until the relationship replicates at a fixed width, because sweeping first and "
        "reporting the best width is how 40 tokens become a curve fit."
    ),
    "FORWARD_HORIZONS_MIN": (
        "INVENTED as a set, MEASURED as a constraint. 5/20/60 minutes were specified by "
        "the task. What our tape can actually support was measured on 2026-09-22: of "
        "3,953 mints with a measurable launch wave in the local database, 713 have "
        "coverage to +5 min, 40 to +20 min and 17 to +60 min; on the live box, 308/69/36 "
        "of 2,626. The horizons are kept as given and the thin ones are reported with "
        "their n rather than dropped."
    ),
    "MARK_BRACKET_S": (
        "INVENTED. A single print is a noisy mark on an asset that trades in bursts; a "
        "60 s volume-weighted bracket ending at the horizon averages the burst without "
        "reaching past the horizon. Widening it smooths more and blurs the horizon; "
        "setting it to 0 would mark to the last print, which is the fallback used anyway "
        "when the bracket is empty. Nobody has measured the variance this removes."
    ),
    "MIN_WINDOW_TRADES": (
        "DEFINITIONAL, not a threshold. A volume-weighted average of one trade is that "
        "trade, and a return measured against it is a return against one print. It gates "
        "the baseline price and therefore the return; it never gates the feature, because "
        "a launch with one buy that took 40% of supply is a measurement and dropping it "
        "would thin the low-concentration control group."
    ),
    "MAX_UNPRICED_ROW_FRACTION": (
        "INVENTED, and copied deliberately from bundles.MAX_UNINDEXED_ATOM_FRACTION "
        "(0.25) so the two refusals agree. A buy row with no amount_token contributes "
        "nothing to the numerator, which understates concentration -- the fail-open "
        "direction for a sizing input -- so above a quarter of window rows the "
        "measurement is refused rather than reported low. The right value is whatever "
        "keeps the understatement under the buckets' own width; nobody has measured that."
    ),
    "BUCKET_EDGES": (
        "INVENTED, and they are the operator's words rather than a finding: 'if its "
        "bundled dev buying more than 20% 30% we can still buy but we need to be "
        "careful'. 20/35/50 brackets that language. No published study sets a launch-wave "
        "threshold; MELT (arXiv:2602.13480) reports a 24pp discriminative gap for "
        "bundle-adjusted concentration but cuts it differently and on a different "
        "population. What would settle it: the bucket boundary that maximises separation "
        "in the held-out half, once a held-out half shows separation at all."
    ),
    "MIN_BUCKET_N": (
        "INVENTED, and copied from bundles.MIN_SEPARATION_ARM (5) for the same reason: "
        "with five samples a median is one observation wide and a between-bucket gap is "
        "noise. It exists so a number is not emitted at all on single digits, because "
        "EDGE section 4 #17 records that every temporal generalisation test in this "
        "literature failed."
    ),
    "PERMUTATION_ITERS": (
        "INVENTED. 2,000 shuffles resolve a p-value to about 0.02 at the 5% line, which "
        "is enough to separate 'obvious' from 'not obvious' and is not enough to defend "
        "a p of 0.04. More iterations would sharpen the number and not the inference."
    ),
    "PERMUTATION_SEED": (
        "DETERMINISM, not a threshold. A fixed seed makes two runs over the same tape "
        "print the same p-value; without one the verdict would wobble run to run."
    ),
    "REPLICATION_RHO_MIN": (
        "INVENTED. |rho| >= 0.10 on the held-out half is a deliberately low bar -- it is "
        "asking for a detectable slope, not a tradable one -- because the honest "
        "outcome here is expected to be a null and a low bar makes a null harder to "
        "reach, not easier. With n=300 a rho of 0.10 is around the 5% line, so the two "
        "criteria are roughly the same test at the sample sizes we have."
    ),
    "STABILITY_BLOCK_HOURS": (
        "INVENTED. Six hours is small enough that a block sits inside one session's "
        "market regime and large enough to hold ~150 measurable mints at the rate this "
        "tape fills, which is the n a rank correlation needs before it says anything. "
        "It is a display choice: no verdict depends on it, and the same drift is visible "
        "at 4 h and at 12 h."
    ),
    "REPLICATION_P_MAX": (
        "CONVENTION, not evidence. 0.05 is the customary line and carries no authority "
        "over one held-out split of a few hundred correlated observations from one "
        "chain, one launchpad and four days."
    ),
}


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


MEASURED = "measured"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LaunchWave:
    """How much supply the first :data:`WAVE_WINDOW_S` seconds bought, and on what basis."""

    chain: Chain
    token: str
    coverage: str
    reason: str
    t0_ms: int | None = None
    created_ms: int | None = None
    window_s: int = WAVE_WINDOW_S
    gross_pct: Decimal | None = None
    net_pct: Decimal | None = None
    top1_pct: Decimal | None = None
    hhi: Decimal | None = None
    gross_atoms: int | None = None
    net_atoms: int | None = None
    wallets: int | None = None
    buys: int | None = None
    supply_atoms: int | None = None
    supply_basis: str | None = None
    window_trades: int = 0
    window_vwap: Decimal | None = None
    window_last_price: Decimal | None = None
    horizon_ms: int | None = None

    @property
    def measured(self) -> bool:
        return self.coverage == MEASURED and self.gross_pct is not None

    @property
    def share_of_total_supply(self) -> bool:
        """True only when the denominator really is total supply.

        ``SupplyBasis.CURVE_LAUNCH_RESERVES`` is 79.31% of a pump.fun mint's supply, so a
        percentage over it is 1.26x the share-of-supply figure and the two must not be
        pooled in one bucket table.
        """
        return self.supply_basis in {
            SupplyBasis.PUMPFUN_STANDARD_VERIFIED.value,
            SupplyBasis.CALLER.value,
        }

    def measure(self) -> Measure:
        """The gross figure as a :class:`Measure`, so a caller cannot read it as zero."""
        if not self.measured:
            return Measure.unknown(3600)
        return Measure(
            value=self.gross_pct,
            basis=EvidenceBasis.DERIVED,
            receipt=Receipt(
                provider="kaiba",
                endpoint=f"learning.concentration_outcome/{MODEL_ID}",
                basis=EvidenceBasis.DERIVED,
                note=self.reason,
            ),
            freshness_budget_s=86_400,
        )


@dataclass(frozen=True, slots=True)
class ForwardReturn:
    """The proxy outcome at one horizon, or a refusal that says why."""

    k_min: int
    coverage: str
    reason: str
    mark: Decimal | None = None
    mark_basis: str | None = None
    ret_vwap_pct: Decimal | None = None
    ret_post_pct: Decimal | None = None
    trades_in_bracket: int = 0

    @property
    def measured(self) -> bool:
        return self.coverage == MEASURED and self.ret_vwap_pct is not None


@dataclass(frozen=True, slots=True)
class Sample:
    """One mint: its wave, its forward returns keyed by horizon, and any external feature.

    ``external`` holds numbers produced by
    :mod:`kaiba.intelligence.launch_concentration` — the module that owns this measurement
    and that landed while this study was being run. A key is absent when that module
    refused the mint, and an absent key drops the mint from that feature's analysis; it is
    never read as zero.
    """

    wave: LaunchWave
    returns: Mapping[int, ForwardReturn] = field(default_factory=dict)
    external: Mapping[str, Decimal | None] = field(default_factory=dict)

    @property
    def token(self) -> str:
        return self.wave.token

    @property
    def t0_ms(self) -> int | None:
        return self.wave.t0_ms

    def usable_at(self, k_min: int) -> bool:
        ret = self.returns.get(k_min)
        return bool(
            self.wave.measured
            and self.wave.share_of_total_supply
            and ret is not None
            and ret.measured
        )


# --------------------------------------------------------------------------------------
# the feature
# --------------------------------------------------------------------------------------


#: Feature names carrying this prefix come from the owning module rather than from here.
EXTERNAL_PREFIX = "ext:"


def external_features(
    chain: Chain, token: str, conn: sqlite3.Connection
) -> dict[str, Decimal | None]:
    """The owning module's numbers for this mint, or ``{}`` when it refused.

    The task this study was given said to import the launch-concentration module if it had
    landed and to reimplement only the launch-wave component if it had not. It had not when
    the corpus was first built, and it did at 09:00 on 2026-09-22, so both exist here: the
    local wave is the axis the study was designed around, and these are the same question
    asked of the number the sizing piece will actually consume.

    The import is deliberately not wrapped in a fallback. If that module is missing or its
    API moved, this raises, because quietly substituting the local reimplementation would
    produce a table labelled with someone else's definition.

    **Hindsight, and which of these numbers has it.** That module is built to run at
    decision time, when the tape ends at "now" and cumulative buying and in-window buying
    are the same thing. Re-run later over a completed tape they are not: ``WavePoint.pct``
    is the cohort's *lifetime* buying and ``headline_pct`` is a peak taken over every
    event we hold, both of which can include trades that happen after the horizon this
    study scores. Used as a predictor of a t+5min return they leak the future and will
    flatter the relationship. ``WavePoint.in_window_pct`` is the point-in-time figure —
    what an observer standing at ``t0 + W`` could have computed — so the ``_iw`` keys are
    the leak-free ones and the others are reported beside them to show the size of the
    difference.
    """
    from kaiba.intelligence import launch_concentration as lc

    report = lc.measure(chain, token, conn)
    if not report.measured:
        return {}
    out: dict[str, Decimal | None] = {
        "headline": report.headline_pct.value,
        "cotimed": report.cotimed_pct.value,
        "relay": report.relay_pct.value,
    }
    for window in (30, 60):
        point = report.wave_at(window)
        out[f"wave{window}"] = None if point is None else point.pct.value
        out[f"wave{window}_iw"] = None if point is None else point.in_window_pct.value
    return out


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _atoms(value: Any) -> int | None:
    """Money is integers in base units; a float here would be a rounding bug later."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        out = int(text)
    except ValueError:
        try:
            out = int(Decimal(text))
        except Exception:  # noqa: BLE001 - the value is provider text; any failure is None
            return None
    return out if out >= 0 else None


def _unavailable(chain: Chain, token: str, reason: str, **extra: Any) -> LaunchWave:
    return LaunchWave(chain=chain, token=token, coverage=UNAVAILABLE, reason=reason, **extra)


def tape_anchor(
    chain: Chain, token: str, conn: sqlite3.Connection, *, created_ms: int | None
) -> tuple[int | None, str]:
    """When the launch window starts, and the evidence that the tape holds it.

    Two proofs are accepted, the same two ``bundles`` accepts and for the same reasons.
    The strong one is ``curve_snapshots.coverage_from_ms <= created_ms``: a backwards walk
    that terminated. The weak one is a **per-token** route's first row sitting within
    :data:`LAUNCH_ANCHOR_TOLERANCE_MS` of the creation; ``pumpfun:trades`` paginates
    backwards through one mint's whole history, so a row at the launch implies the walk
    passed through everything after it. A ``helius:backfill`` row is walked per *wallet*
    and proves nothing about the rest of the slot, so it cannot anchor a tape here.

    ``t0`` is the earlier of ``created_ms`` and that first route print. Provider creation
    timestamps and block times disagree by a second or two, and anchoring on ``created_ms``
    alone would drop the creator's own buy out of the window on exactly the launches where
    it matters most.
    """
    if created_ms is None:
        return None, "tokens.created_ms is NULL, so there is no launch to window"
    clauses = " OR ".join("source = ? OR source LIKE ?" for _ in PER_TOKEN_TAPE_SOURCES)
    params: list[Any] = [chain.value, token]
    for prefix in PER_TOKEN_TAPE_SOURCES:
        params.extend((prefix, f"{prefix}:%"))
    try:
        row = fetch_one(
            conn,
            f"SELECT MIN(ts_ms) AS m FROM swaps WHERE chain=? AND token=? AND ({clauses})",
            params,
        )
    except sqlite3.Error as exc:
        log.warning("concentration_outcome: tape lookup failed for %s (%s)", token[:12], exc)
        return None, f"swaps unreadable for this mint ({exc})"
    first_ms = _int_or_none(row["m"]) if row else None
    if first_ms is None:
        return None, "no per-token trade route in the tape, so the launch cannot be anchored"
    anchored = (first_ms - int(created_ms)) <= LAUNCH_ANCHOR_TOLERANCE_MS
    proved = launch_coverage_proved(chain, token, conn, created_ms=created_ms)
    if not anchored and not proved:
        lag_s = (first_ms - int(created_ms)) / 1000.0
        return None, f"tape starts {lag_s:.0f}s after the launch and no coverage proof stands"
    t0 = min(int(created_ms), first_ms)
    proof = "coverage_from_ms" if proved else "first_route_print"
    return t0, f"launch anchored by {proof}"


def launch_wave(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection,
    *,
    created_ms: int | None = None,
    covered_to_ms: int | None = None,
    window_s: int = WAVE_WINDOW_S,
) -> LaunchWave:
    """Measure the launch wave, or refuse and say why.

    Refusals are the point as much as the measurements are: a mint whose tape does not
    reach its launch has an *unknown* wave, and the difference between that and a 0% wave
    is the difference between "we did not look" and "we looked and the launch was clean".
    """
    if created_ms is None:
        row = fetch_one(
            conn,
            "SELECT created_ms FROM tokens WHERE chain=? AND address=?",
            (chain.value, token),
        )
        if row is None:
            # Distinct from a row with a NULL creation time: this mint has never been
            # ingested at all, which is a fact about our collection and is the answer for
            # GIVE itself on both databases as of 2026-09-22.
            return _unavailable(chain, token, "mint is not in tokens; nothing was ever ingested")
        created_ms = _int_or_none(row["created_ms"])
    t0, anchor_reason = tape_anchor(chain, token, conn, created_ms=created_ms)
    if t0 is None:
        return _unavailable(chain, token, anchor_reason, created_ms=created_ms)

    supply_atoms, supply_basis, supply_note = resolve_supply(
        chain, token, conn, created_ms=created_ms
    )
    if supply_atoms is None or supply_atoms <= 0:
        return _unavailable(
            chain,
            token,
            supply_note or "no supply denominator",
            t0_ms=t0,
            created_ms=created_ms,
            supply_basis=supply_basis.value,
        )

    end = t0 + window_s * 1000
    try:
        rows = fetch_all(
            conn,
            "SELECT wallet, side, amount_token, amount_native FROM swaps "
            "WHERE chain=? AND token=? AND ts_ms >= ? AND ts_ms <= ? "
            "ORDER BY ts_ms ASC, slot ASC, block_index ASC, id ASC",
            (chain.value, token, t0, end),
        )
    except sqlite3.Error as exc:
        return _unavailable(chain, token, f"window query failed ({exc})", t0_ms=t0)

    gross = 0
    per_wallet: dict[str, int] = {}
    gross_by_wallet: dict[str, int] = {}
    buyers: set[str] = set()
    buys = 0
    unpriced_buys = 0
    native_total = 0
    atom_total = 0
    trades = 0
    last_price: Decimal | None = None
    for row in rows:
        atoms = _atoms(row["amount_token"])
        native = _atoms(row["amount_native"])
        side = str(row["side"] or "").lower()
        wallet = str(row["wallet"] or "")
        if side == "buy":
            buys += 1
            if atoms is None or atoms <= 0:
                unpriced_buys += 1
            else:
                gross += atoms
                buyers.add(wallet)
                per_wallet[wallet] = per_wallet.get(wallet, 0) + atoms
                gross_by_wallet[wallet] = gross_by_wallet.get(wallet, 0) + atoms
        elif side == "sell" and atoms is not None and atoms > 0:
            per_wallet[wallet] = per_wallet.get(wallet, 0) - atoms
        if atoms and native and atoms > 0 and native > 0:
            trades += 1
            atom_total += atoms
            native_total += native
            last_price = Decimal(native) / Decimal(atoms)

    if buys == 0:
        return _unavailable(
            chain,
            token,
            f"no buys in the first {window_s}s of a tape that reaches the launch",
            t0_ms=t0,
            created_ms=created_ms,
            supply_atoms=supply_atoms,
            supply_basis=supply_basis.value,
        )
    if Decimal(unpriced_buys) / Decimal(buys) > MAX_UNPRICED_ROW_FRACTION:
        return _unavailable(
            chain,
            token,
            f"{unpriced_buys}/{buys} launch buys carry no amount_token, over the "
            f"{MAX_UNPRICED_ROW_FRACTION} refusal line",
            t0_ms=t0,
            created_ms=created_ms,
            supply_atoms=supply_atoms,
            supply_basis=supply_basis.value,
        )
    net = sum(v for v in per_wallet.values() if v > 0)
    # Two shape numbers, so "59% of supply" can be told apart from "59% of supply spread
    # over 500 strangers". They are what distinguishes a concentration claim from a
    # volume claim, and the study reports both rather than choosing.
    top1 = max(gross_by_wallet.values()) if gross_by_wallet else 0
    hhi = (
        sum((Decimal(v) / Decimal(gross)) ** 2 for v in gross_by_wallet.values())
        if gross > 0
        else None
    )
    horizon = _tape_horizon(chain, token, conn, covered_to_ms=covered_to_ms)
    # A thin window still has a concentration -- one buy that takes 40% of supply is the
    # measurement, not a missing one. It is the *baseline price* that needs more than one
    # print, so MIN_WINDOW_TRADES gates the VWAP and therefore the return, and never the
    # feature. Gating the feature here would drop the quiet launches, which are the
    # low-concentration control group this study needs most.
    vwap = (
        Decimal(native_total) / Decimal(atom_total)
        if atom_total > 0 and trades >= MIN_WINDOW_TRADES
        else None
    )
    note = anchor_reason if supply_note is None else f"{anchor_reason}; {supply_note}"
    return LaunchWave(
        chain=chain,
        token=token,
        coverage=MEASURED,
        reason=note,
        t0_ms=t0,
        created_ms=created_ms,
        window_s=window_s,
        gross_pct=HUNDRED * Decimal(gross) / Decimal(supply_atoms),
        net_pct=HUNDRED * Decimal(net) / Decimal(supply_atoms),
        top1_pct=HUNDRED * Decimal(top1) / Decimal(supply_atoms),
        hhi=None if hhi is None else HUNDRED * hhi,
        gross_atoms=gross,
        net_atoms=net,
        wallets=len(buyers),
        buys=buys,
        supply_atoms=supply_atoms,
        supply_basis=supply_basis.value,
        window_trades=trades,
        window_vwap=vwap,
        window_last_price=last_price,
        horizon_ms=horizon,
    )


def _tape_horizon(
    chain: Chain, token: str, conn: sqlite3.Connection, *, covered_to_ms: int | None
) -> int | None:
    """The last instant we can claim to know this mint's price.

    Two pieces of evidence, and the later one wins. ``token_tape.covered_to_ms`` is a
    watermark written by a walk that terminated, so it covers silence as well as trades —
    that is what lets a dead mint keep a price instead of leaving the sample. The newest
    swap row is the other: ``pumpfun:trades`` paginates backwards from the present, so
    holding a row at time T with an anchored tape means holding everything up to T.
    """
    newest = None
    try:
        row = fetch_one(
            conn,
            "SELECT MAX(ts_ms) AS m FROM swaps WHERE chain=? AND token=?",
            (chain.value, token),
        )
        newest = _int_or_none(row["m"]) if row else None
        if covered_to_ms is None:
            # A caller measuring one mint should not have to know that the watermark
            # lives in another table; without it a dead mint looks unmeasurable rather
            # than dead, which is the whole distinction this module is built on.
            mark = fetch_one(
                conn,
                "SELECT covered_to_ms FROM token_tape WHERE chain=? AND token=?",
                (chain.value, token),
            )
            covered_to_ms = _int_or_none(mark["covered_to_ms"]) if mark else None
    except sqlite3.Error as exc:
        log.warning("concentration_outcome: horizon query failed for %s (%s)", token[:12], exc)
    candidates = [v for v in (newest, covered_to_ms) if v is not None]
    return max(candidates) if candidates else None


# --------------------------------------------------------------------------------------
# the outcome
# --------------------------------------------------------------------------------------


def forward_return(
    wave: LaunchWave,
    conn: sqlite3.Connection,
    *,
    k_min: int,
    bracket_s: int = MARK_BRACKET_S,
) -> ForwardReturn:
    """Price at ``t0 + k_min`` against the launch-window VWAP, or a refusal.

    The horizon test is the honest half of this function. If the tape cannot be shown to
    cover ``t0 + k_min``, there is no return at that horizon — not a flat one.
    """
    if not wave.measured or wave.t0_ms is None:
        return ForwardReturn(k_min=k_min, coverage=UNAVAILABLE, reason="no measured launch wave")
    if wave.window_vwap is None:
        return ForwardReturn(
            k_min=k_min,
            coverage=UNAVAILABLE,
            reason=(
                f"launch window holds {wave.window_trades} priced trades, under "
                f"MIN_WINDOW_TRADES={MIN_WINDOW_TRADES}, so there is no baseline price"
            ),
        )
    target = wave.t0_ms + k_min * 60_000
    if wave.horizon_ms is None or wave.horizon_ms < target:
        short_s = None if wave.horizon_ms is None else (target - wave.horizon_ms) / 1000.0
        detail = "no coverage watermark" if short_s is None else f"{short_s:.0f}s short"
        return ForwardReturn(
            k_min=k_min,
            coverage=UNAVAILABLE,
            reason=f"tape does not reach t+{k_min}min ({detail})",
        )
    try:
        rows = fetch_all(
            conn,
            "SELECT amount_token, amount_native FROM swaps "
            "WHERE chain=? AND token=? AND ts_ms > ? AND ts_ms <= ? "
            "ORDER BY ts_ms ASC",
            (wave.chain.value, wave.token, target - bracket_s * 1000, target),
        )
    except sqlite3.Error as exc:
        return ForwardReturn(k_min=k_min, coverage=UNAVAILABLE, reason=f"mark query failed ({exc})")

    atom_total = 0
    native_total = 0
    for row in rows:
        atoms = _atoms(row["amount_token"])
        native = _atoms(row["amount_native"])
        if atoms and native and atoms > 0 and native > 0:
            atom_total += atoms
            native_total += native
    if atom_total > 0:
        mark = Decimal(native_total) / Decimal(atom_total)
        basis = f"bracket_vwap_{bracket_s}s"
        in_bracket = len(rows)
    else:
        mark, basis = _last_print(wave, conn, at_or_before_ms=target)
        in_bracket = 0
        if mark is None:
            return ForwardReturn(
                k_min=k_min,
                coverage=UNAVAILABLE,
                reason=f"no priced print at or before t+{k_min}min despite coverage",
            )
    ret_vwap = HUNDRED * (mark / wave.window_vwap - 1)
    ret_post = (
        HUNDRED * (mark / wave.window_last_price - 1)
        if wave.window_last_price and wave.window_last_price > 0
        else None
    )
    return ForwardReturn(
        k_min=k_min,
        coverage=MEASURED,
        reason=f"mark by {basis}",
        mark=mark,
        mark_basis=basis,
        ret_vwap_pct=ret_vwap,
        ret_post_pct=ret_post,
        trades_in_bracket=in_bracket,
    )


def _last_print(
    wave: LaunchWave, conn: sqlite3.Connection, *, at_or_before_ms: int
) -> tuple[Decimal | None, str]:
    """The price of the newest priced trade at or before an instant.

    Used when the bracket is empty, which on this asset class means the mint went quiet —
    a real price for a dead token, not a missing one, and only reachable at all because
    the coverage watermark already proved we were still looking.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT amount_token, amount_native FROM swaps "
            "WHERE chain=? AND token=? AND ts_ms <= ? AND amount_token IS NOT NULL "
            " AND amount_native IS NOT NULL "
            "ORDER BY ts_ms DESC, id DESC LIMIT 1",
            (wave.chain.value, wave.token, at_or_before_ms),
        )
    except sqlite3.Error as exc:
        log.warning("concentration_outcome: last-print query failed (%s)", exc)
        return None, "query failed"
    if row is None:
        return None, "no print"
    atoms = _atoms(row["amount_token"])
    native = _atoms(row["amount_native"])
    if not atoms or not native or atoms <= 0 or native <= 0:
        return None, "unpriced print"
    return Decimal(native) / Decimal(atoms), "last_print"


# --------------------------------------------------------------------------------------
# corpus
# --------------------------------------------------------------------------------------


def candidates(chain: Chain, conn: sqlite3.Connection, *, limit: int | None = None) -> list[dict]:
    """Every mint in the tape with a creation time, refusals included.

    Deliberately not filtered to ``coverage='complete'``: the count of mints that fall out
    at each refusal is part of the answer, and filtering here would hide it.
    """
    sql = (
        "SELECT tt.token AS token, tk.created_ms AS created_ms, tt.covered_to_ms AS covered_to_ms, "
        "       tt.coverage AS coverage, tt.route AS route "
        "FROM token_tape tt JOIN tokens tk ON tk.chain = tt.chain AND tk.address = tt.token "
        "WHERE tt.chain = ? AND tk.created_ms IS NOT NULL "
        "ORDER BY tk.created_ms ASC"
    )
    params: list[Any] = [chain.value]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return fetch_all(conn, sql, params)


def collect(
    chain: Chain,
    conn: sqlite3.Connection,
    *,
    horizons: Sequence[int] = FORWARD_HORIZONS_MIN,
    window_s: int = WAVE_WINDOW_S,
    limit: int | None = None,
    external: bool = False,
) -> tuple[list[Sample], dict[str, int]]:
    """Measure every candidate. Returns the samples and a census of why the rest failed."""
    census: dict[str, int] = {"candidates": 0, "wave_measured": 0, "wave_refused": 0}
    samples: list[Sample] = []
    for row in candidates(chain, conn, limit=limit):
        census["candidates"] += 1
        wave = launch_wave(
            chain,
            str(row["token"]),
            conn,
            created_ms=_int_or_none(row["created_ms"]),
            covered_to_ms=_int_or_none(row["covered_to_ms"]),
            window_s=window_s,
        )
        if not wave.measured:
            census["wave_refused"] += 1
            key = "refused:" + _reason_class(wave.reason)
            census[key] = census.get(key, 0) + 1
            continue
        census["wave_measured"] += 1
        if not wave.share_of_total_supply:
            census["denominator_not_total_supply"] = (
                census.get("denominator_not_total_supply", 0) + 1
            )
        returns = {k: forward_return(wave, conn, k_min=k) for k in horizons}
        for k, ret in returns.items():
            if ret.measured:
                census[f"ret_measured_k{k}"] = census.get(f"ret_measured_k{k}", 0) + 1
            else:
                key = f"ret_refused_k{k}:{_ret_reason_class(ret.reason)}"
                census[key] = census.get(key, 0) + 1
        ext: dict[str, Decimal | None] = {}
        if external:
            ext = external_features(chain, str(row["token"]), conn)
            key = "external_measured" if ext else "external_refused"
            census[key] = census.get(key, 0) + 1
        samples.append(Sample(wave=wave, returns=returns, external=ext))
    return samples, census


def _ret_reason_class(reason: str) -> str:
    """Why a horizon has no return. The two causes are different problems."""
    text = reason.lower()
    if "does not reach" in text:
        return "tape_too_short"
    if "baseline price" in text or "min_window_trades" in text:
        return "no_baseline_price"
    if "no measured launch wave" in text:
        return "no_wave"
    return "other"


def _reason_class(reason: str) -> str:
    """Bucket a refusal string so the census is readable without being lossy."""
    text = reason.lower()
    if "created_ms" in text:
        return "no_created_ms"
    if "anchor" in text or "tape starts" in text or "per-token trade route" in text:
        return "tape_not_at_launch"
    if "supply" in text or "denominator" in text or "curve" in text:
        return "no_supply_denominator"
    if "no buys" in text:
        return "no_launch_buys"
    if "amount_token" in text:
        return "unpriced_launch_buys"
    if "min_window_trades" in text or "priced trades" in text:
        return "too_few_window_trades"
    return "other"


# --------------------------------------------------------------------------------------
# statistics, all pure
# --------------------------------------------------------------------------------------


def bucket_of(pct: Decimal, edges: Sequence[Decimal] = BUCKET_EDGES) -> str:
    """Which concentration bucket a percentage falls in. Edges are exclusive upper bounds."""
    for edge, label in zip(edges, BUCKET_LABELS, strict=False):
        if pct < edge:
            return label
    return BUCKET_LABELS[len(edges)]


@dataclass(frozen=True, slots=True)
class BucketStat:
    label: str
    n: int
    mean: Decimal | None
    median: Decimal | None
    hit_rate: Decimal | None
    reason: str | None = None


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def bucket_stats(
    points: Sequence[tuple[Decimal, Decimal]], *, min_n: int = MIN_BUCKET_N
) -> list[BucketStat]:
    """Mean, median and hit rate per bucket. Under ``min_n`` the numbers are refused.

    The refusal is not decoration. A bucket of two tokens has a median that is one token,
    and quoting it later without its denominator is exactly how a small-sample gap becomes
    a threshold in a config file.
    """
    grouped: dict[str, list[Decimal]] = {label: [] for label in BUCKET_LABELS}
    for x, y in points:
        grouped[bucket_of(x)].append(y)
    out: list[BucketStat] = []
    for label in BUCKET_LABELS:
        values = grouped[label]
        if len(values) < min_n:
            out.append(
                BucketStat(
                    label=label,
                    n=len(values),
                    mean=None,
                    median=None,
                    hit_rate=None,
                    reason=f"under MIN_BUCKET_N={min_n}",
                )
            )
            continue
        wins = sum(1 for v in values if v > 0)
        out.append(
            BucketStat(
                label=label,
                n=len(values),
                mean=_mean(values),
                median=_median(values),
                hit_rate=HUNDRED * Decimal(wins) / Decimal(len(values)),
            )
        )
    return out


def _ranks(values: Sequence[Decimal]) -> list[float]:
    """Average ranks, so ties do not invent an ordering."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: Sequence[Decimal], ys: Sequence[Decimal]) -> float | None:
    """Rank correlation, or None when it is undefined rather than zero."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def partial_spearman(
    xs: Sequence[Decimal], ys: Sequence[Decimal], zs: Sequence[Decimal]
) -> float | None:
    """Rank correlation of x with y once z is taken out of both.

    The question it answers here is the only one that decides whether the sizing piece
    should scale on concentration at all: a wave's share of supply and its number of
    buyers move together, so if the share carries nothing once the count is removed, then
    "concentration" was a proxy for "how many wallets piled in" and the simpler number is
    the honest one to size on.
    """
    r_xy = spearman(xs, ys)
    r_xz = spearman(xs, zs)
    r_yz = spearman(ys, zs)
    if r_xy is None or r_xz is None or r_yz is None:
        return None
    denom = math.sqrt(max(0.0, 1 - r_xz**2) * max(0.0, 1 - r_yz**2))
    if denom == 0:
        return None
    return (r_xy - r_xz * r_yz) / denom


def permutation_p(
    xs: Sequence[Decimal],
    ys: Sequence[Decimal],
    *,
    iters: int = PERMUTATION_ITERS,
    seed: int = PERMUTATION_SEED,
) -> float | None:
    """Two-sided p for the observed rank correlation, by shuffling one axis.

    A permutation test rather than a t-approximation because the return distribution on
    this asset class is neither normal nor light-tailed, and the approximation's p-value
    would be a statement about a distribution we do not have.
    """
    observed = spearman(xs, ys)
    if observed is None:
        return None
    rng = random.Random(seed)
    shuffled = list(ys)
    hits = 0
    for _ in range(iters):
        rng.shuffle(shuffled)
        rho = spearman(xs, shuffled)
        if rho is not None and abs(rho) >= abs(observed):
            hits += 1
    return (hits + 1) / (iters + 1)


def time_split(samples: Sequence[Sample]) -> tuple[list[Sample], list[Sample]]:
    """Earlier half, later half, split on ``t0``. Not random: the question is temporal.

    A random split would leak — two mints launched in the same minute share the market
    regime, the SOL price and often the same hunter. EDGE section 4 #17 records that every
    temporal generalisation test in this literature failed, which is the result this split
    exists to be able to reproduce.
    """
    ordered = sorted((s for s in samples if s.t0_ms is not None), key=lambda s: s.t0_ms or 0)
    half = len(ordered) // 2
    return ordered[:half], ordered[half:]


@dataclass(frozen=True, slots=True)
class SelectionCheck:
    """Is the half of the corpus that reaches a horizon a biased half?

    The inclusion rule is "the tape provably covers t+K", and the tape is deepened by our
    own scheduler. If included mints are systematically more (or less) concentrated than
    excluded ones, the bucket table is a statement about the mints we happened to keep
    watching, and this is the number that says how far that goes.
    """

    k_min: int
    n_included: int
    n_excluded: int
    median_included: Decimal | None
    median_excluded: Decimal | None
    share_over_50_included: Decimal | None
    share_over_50_excluded: Decimal | None


def selection_check(samples: Sequence[Sample], k_min: int) -> SelectionCheck:
    inc = [s.wave.gross_pct for s in samples if s.usable_at(k_min) and s.wave.gross_pct is not None]
    exc = [
        s.wave.gross_pct
        for s in samples
        if not s.usable_at(k_min) and s.wave.measured and s.wave.gross_pct is not None
    ]

    def over50(values: Sequence[Decimal]) -> Decimal | None:
        if not values:
            return None
        return HUNDRED * Decimal(sum(1 for v in values if v > 50)) / Decimal(len(values))

    return SelectionCheck(
        k_min=k_min,
        n_included=len(inc),
        n_excluded=len(exc),
        median_included=_median(inc),
        median_excluded=_median(exc),
        share_over_50_included=over50(inc),
        share_over_50_excluded=over50(exc),
    )


#: Width of a stability block, in hours.
STABILITY_BLOCK_HOURS = 6


@dataclass(frozen=True, slots=True)
class StabilityBlock:
    """The relationship inside one wall-clock block, so drift is visible as drift."""

    start_ms: int
    n: int
    rho: float | None
    median_return: Decimal | None
    median_feature: Decimal | None


def stability_blocks(
    samples: Sequence[Sample],
    k_min: int,
    *,
    feature: str = "gross",
    block_hours: int = STABILITY_BLOCK_HOURS,
) -> list[StabilityBlock]:
    """Rank correlation per block of wall-clock time.

    A single train/test split can hide a sign that wanders: one half averages over
    whatever happened inside it. This is the same data cut by the clock, and it is the
    cheapest way to see whether a relationship is an edge or a regime.
    """
    width = block_hours * 3_600_000
    grouped: dict[int, list[tuple[Decimal, Decimal]]] = {}
    for s in samples:
        if not s.usable_at(k_min) or s.t0_ms is None:
            continue
        x = _feature_value(s, feature)
        y = s.returns[k_min].ret_vwap_pct
        if x is None or y is None:
            continue
        grouped.setdefault((s.t0_ms // width) * width, []).append((x, y))
    out: list[StabilityBlock] = []
    for start in sorted(grouped):
        pts = grouped[start]
        out.append(
            StabilityBlock(
                start_ms=start,
                n=len(pts),
                rho=spearman([a for a, _ in pts], [b for _, b in pts]),
                median_return=_median([b for _, b in pts]),
                median_feature=_median([a for a, _ in pts]),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class StratumRow:
    """One cell of the wallet-count x concentration table."""

    wallet_quartile: int
    wallet_lo: int
    wallet_hi: int
    bucket: str
    n: int
    median: Decimal | None


def stratified_medians(samples: Sequence[Sample], k_min: int) -> list[StratumRow]:
    """Median return by concentration bucket *within* quartiles of wallet count.

    The partial correlation says whether concentration adds a smooth slope once the
    number of buyers is removed. This says where whatever is left actually lives, which
    is not the same question: a rank correlation of zero is compatible with a large
    effect confined to one corner, and on this data that is what happens.
    """
    rows = [
        s
        for s in samples
        if s.usable_at(k_min) and s.wave.gross_pct is not None and s.wave.wallets is not None
    ]
    if not rows:
        return []
    counts = sorted(int(s.wave.wallets or 0) for s in rows)
    cuts = [counts[int(len(counts) * q)] for q in (0.25, 0.5, 0.75)]

    def quartile(n: int) -> int:
        for i, cut in enumerate(cuts):
            if n <= cut:
                return i
        return 3

    out: list[StratumRow] = []
    for q in range(4):
        members = [s for s in rows if quartile(int(s.wave.wallets or 0)) == q]
        if not members:
            continue
        lo = min(int(s.wave.wallets or 0) for s in members)
        hi = max(int(s.wave.wallets or 0) for s in members)
        for label in BUCKET_LABELS:
            cell = [
                s.returns[k_min].ret_vwap_pct
                for s in members
                if bucket_of(s.wave.gross_pct or ZERO) == label
                and s.returns[k_min].ret_vwap_pct is not None
            ]
            out.append(
                StratumRow(
                    wallet_quartile=q,
                    wallet_lo=lo,
                    wallet_hi=hi,
                    bucket=label,
                    n=len(cell),
                    median=_median([c for c in cell if c is not None]),
                )
            )
    return out


@dataclass(frozen=True, slots=True)
class TailContrast:
    """The one comparison the sizing piece would actually act on, held out in time.

    EXPLORATORY. This contrast was chosen after looking at the stratified table, which
    means the in-sample p-value on it is not evidence of anything. The held-out half is,
    and only barely: it is one split of one chain on four days.
    """

    k_min: int
    min_wallets: int
    edge_pct: Decimal
    n_high: int
    n_low: int
    median_high: Decimal | None
    median_low: Decimal | None
    diff_pp: Decimal | None
    p_value: float | None
    n_test_high: int
    n_test_low: int
    test_diff_pp: Decimal | None
    test_p: float | None


def _median_diff_p(
    high: Sequence[Decimal],
    low: Sequence[Decimal],
    *,
    iters: int = PERMUTATION_ITERS,
    seed: int = PERMUTATION_SEED,
) -> tuple[Decimal | None, float | None]:
    """Difference of medians and its permutation p, or (None, None) when undefined."""
    if len(high) < MIN_BUCKET_N or len(low) < MIN_BUCKET_N:
        return None, None
    mh, ml = _median(high), _median(low)
    if mh is None or ml is None:
        return None, None
    observed = mh - ml
    pool = list(high) + list(low)
    rng = random.Random(seed)
    hits = 0
    for _ in range(iters):
        rng.shuffle(pool)
        a, b = pool[: len(high)], pool[len(high) :]
        ma, mb = _median(a), _median(b)
        if ma is not None and mb is not None and abs(ma - mb) >= abs(observed):
            hits += 1
    return observed, (hits + 1) / (iters + 1)


def tail_contrast(
    samples: Sequence[Sample],
    k_min: int,
    *,
    min_wallets: int,
    edge_pct: Decimal = Decimal(50),
) -> TailContrast:
    """Within crowded launches only, does taking more than ``edge_pct`` of supply matter?"""
    rows = [
        s
        for s in samples
        if s.usable_at(k_min)
        and s.wave.gross_pct is not None
        and (s.wave.wallets or 0) >= min_wallets
        and s.returns[k_min].ret_vwap_pct is not None
    ]
    high = [s for s in rows if (s.wave.gross_pct or ZERO) > edge_pct]
    low = [s for s in rows if (s.wave.gross_pct or ZERO) <= edge_pct]
    diff, p = _median_diff_p(
        [s.returns[k_min].ret_vwap_pct for s in high if s.returns[k_min].ret_vwap_pct is not None],
        [s.returns[k_min].ret_vwap_pct for s in low if s.returns[k_min].ret_vwap_pct is not None],
    )
    _, test_rows = time_split(rows)
    t_high = [
        s.returns[k_min].ret_vwap_pct
        for s in test_rows
        if (s.wave.gross_pct or ZERO) > edge_pct and s.returns[k_min].ret_vwap_pct is not None
    ]
    t_low = [
        s.returns[k_min].ret_vwap_pct
        for s in test_rows
        if (s.wave.gross_pct or ZERO) <= edge_pct and s.returns[k_min].ret_vwap_pct is not None
    ]
    test_diff, test_p = _median_diff_p(t_high, t_low)
    return TailContrast(
        k_min=k_min,
        min_wallets=min_wallets,
        edge_pct=edge_pct,
        n_high=len(high),
        n_low=len(low),
        median_high=_median(
            [
                s.returns[k_min].ret_vwap_pct
                for s in high
                if s.returns[k_min].ret_vwap_pct is not None
            ]
        ),
        median_low=_median(
            [
                s.returns[k_min].ret_vwap_pct
                for s in low
                if s.returns[k_min].ret_vwap_pct is not None
            ]
        ),
        diff_pp=diff,
        p_value=p,
        n_test_high=len(t_high),
        n_test_low=len(t_low),
        test_diff_pp=test_diff,
        test_p=test_p,
    )


@dataclass(frozen=True, slots=True)
class HorizonResult:
    k_min: int
    n: int
    buckets: list[BucketStat]
    rho: float | None
    p_value: float | None
    train: list[BucketStat]
    test: list[BucketStat]
    train_rho: float | None
    test_rho: float | None
    test_p: float | None
    n_train: int
    n_test: int
    replicates: bool
    verdict: str
    post_wave_rho: float | None = None
    feature: str = "gross"
    marks_last_print: int = 0
    marks_bracket: int = 0
    selection: SelectionCheck | None = None
    control_feature: str = "wallets"
    control_rho: float | None = None
    partial_rho: float | None = None


#: The four readings of the same window this study compares. ``gross`` and ``net`` are
#: shares of supply; ``top1`` is the largest single buyer's share of supply; ``hhi`` is
#: the Herfindahl index of the wave's buys across wallets, on a 0..100 scale. The bucket
#: edges only carry the operator's meaning for the first three.
#: ``wallets`` is a **control**, not a candidate: it is the count of distinct buyers in
#: the window with no supply in it at all. If the control carries the same signal as
#: ``gross``, then what predicts is participation and not concentration, and the sizing
#: piece would be scaling on the wrong axis. It is reported for exactly that reason.
FEATURES: tuple[str, ...] = (
    "gross",
    "net",
    "top1",
    "hhi",
    "wallets",
    "ext:headline",
    "ext:wave60",
    "ext:wave30",
    "ext:wave60_iw",
    "ext:wave30_iw",
    "ext:cotimed",
    "ext:relay",
)


def _feature_value(sample: Sample, feature: str) -> Decimal | None:
    if feature == "net":
        return sample.wave.net_pct
    if feature == "gross":
        return sample.wave.gross_pct
    if feature == "top1":
        return sample.wave.top1_pct
    if feature == "hhi":
        return sample.wave.hhi
    if feature == "wallets":
        return None if sample.wave.wallets is None else Decimal(sample.wave.wallets)
    if feature.startswith(EXTERNAL_PREFIX):
        if not sample.external:
            return None
        return sample.external.get(feature[len(EXTERNAL_PREFIX) :])
    raise ValueError(f"unknown feature {feature!r}")


def analyse_horizon(
    samples: Sequence[Sample],
    k_min: int,
    *,
    min_n: int = MIN_BUCKET_N,
    feature: str = "gross",
) -> HorizonResult:
    """Everything this module claims about one horizon, including that it claims nothing."""
    usable = [s for s in samples if s.usable_at(k_min)]

    def points(rows: Sequence[Sample]) -> list[tuple[Decimal, Decimal]]:
        out = []
        for s in rows:
            ret = s.returns[k_min]
            x = _feature_value(s, feature)
            if x is not None and ret.ret_vwap_pct is not None:
                out.append((x, ret.ret_vwap_pct))
        return out

    all_points = points(usable)
    xs = [x for x, _ in all_points]
    ys = [y for _, y in all_points]
    rho = spearman(xs, ys)
    p = permutation_p(xs, ys) if rho is not None else None

    post = [
        (_feature_value(s, feature), s.returns[k_min].ret_post_pct)
        for s in usable
        if _feature_value(s, feature) is not None and s.returns[k_min].ret_post_pct is not None
    ]
    post_rho = spearman([a for a, _ in post], [b for _, b in post]) if len(post) >= 3 else None

    control = "gross" if feature == "wallets" else "wallets"
    triples = [
        (
            _feature_value(s, feature),
            s.returns[k_min].ret_vwap_pct,
            _feature_value(s, control),
        )
        for s in usable
    ]
    triples = [t for t in triples if all(v is not None for v in t)]
    control_rho = (
        spearman([t[2] for t in triples], [t[1] for t in triples]) if len(triples) >= 3 else None
    )
    partial_rho = (
        partial_spearman(
            [t[0] for t in triples], [t[1] for t in triples], [t[2] for t in triples]
        )
        if len(triples) >= 3
        else None
    )

    train_rows, test_rows = time_split(usable)
    train_points, test_points = points(train_rows), points(test_rows)
    train_rho = spearman([a for a, _ in train_points], [b for _, b in train_points])
    test_rho = spearman([a for a, _ in test_points], [b for _, b in test_points])
    test_p = permutation_p([a for a, _ in test_points], [b for _, b in test_points])

    replicates, verdict = _verdict(train_rho, test_rho, test_p, len(test_points))
    return HorizonResult(
        k_min=k_min,
        n=len(all_points),
        buckets=bucket_stats(all_points, min_n=min_n),
        rho=rho,
        p_value=p,
        train=bucket_stats(train_points, min_n=min_n),
        test=bucket_stats(test_points, min_n=min_n),
        train_rho=train_rho,
        test_rho=test_rho,
        test_p=test_p,
        n_train=len(train_points),
        n_test=len(test_points),
        replicates=replicates,
        verdict=verdict,
        post_wave_rho=post_rho,
        feature=feature,
        marks_last_print=sum(1 for s in usable if s.returns[k_min].mark_basis == "last_print"),
        marks_bracket=sum(
            1 for s in usable if (s.returns[k_min].mark_basis or "").startswith("bracket")
        ),
        selection=selection_check(samples, k_min),
        control_feature=control,
        control_rho=control_rho,
        partial_rho=partial_rho,
    )


def _verdict(
    train_rho: float | None, test_rho: float | None, test_p: float | None, n_test: int
) -> tuple[bool, str]:
    """Does the relationship survive the held-out half? Stated so it can fail."""
    if train_rho is None or test_rho is None or test_p is None:
        return False, "NOT TESTABLE: too few measurable tokens to split"
    if n_test < 3 * MIN_BUCKET_N:
        return False, f"NOT TESTABLE: held-out half has {n_test} tokens"
    if (train_rho > 0) != (test_rho > 0):
        return False, (
            f"DOES NOT REPLICATE: sign flips out of sample "
            f"(train rho {train_rho:+.3f}, test rho {test_rho:+.3f})"
        )
    if abs(test_rho) < float(REPLICATION_RHO_MIN):
        return False, (
            f"DOES NOT REPLICATE: held-out |rho| {abs(test_rho):.3f} under "
            f"REPLICATION_RHO_MIN {REPLICATION_RHO_MIN}"
        )
    if test_p > float(REPLICATION_P_MAX):
        return False, (
            f"DOES NOT REPLICATE: held-out p {test_p:.3f} over REPLICATION_P_MAX "
            f"{REPLICATION_P_MAX}"
        )
    return True, (
        f"REPLICATES at this horizon: train rho {train_rho:+.3f}, test rho {test_rho:+.3f}, "
        f"p {test_p:.3f}, n_test {n_test}"
    )


# --------------------------------------------------------------------------------------
# the realized arm: our own closed positions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RealizedRow:
    trade_id: str
    lane: str
    mode: str
    token: str
    pnl_pct: Decimal
    opened_ms: int
    wave: LaunchWave


def realized_arm(chain: Chain, conn: sqlite3.Connection) -> tuple[list[RealizedRow], dict[str, int]]:
    """Closed trades whose launch wave we can measure. Expected to be tiny; say so."""
    rows = fetch_all(
        conn,
        "SELECT trade_id, lane, mode, token, pnl_pct, opened_ms FROM trades WHERE chain=? "
        "ORDER BY closed_ms ASC",
        (chain.value,),
    )
    census = {"closed_trades": len(rows), "measurable": 0}
    out: list[RealizedRow] = []
    for row in rows:
        wave = launch_wave(chain, str(row["token"]), conn)
        if not wave.measured:
            continue
        census["measurable"] += 1
        out.append(
            RealizedRow(
                trade_id=str(row["trade_id"]),
                lane=str(row["lane"]),
                mode=str(row["mode"]),
                token=str(row["token"]),
                pnl_pct=Decimal(str(row["pnl_pct"])),
                opened_ms=int(row["opened_ms"]),
                wave=wave,
            )
        )
    return out, census


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def _utc(ms: int) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%d %H:%M")


def _fmt(value: Decimal | float | None, places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.{places}f}{suffix}"


def render(
    results: Sequence[HorizonResult],
    census: Mapping[str, int],
    realized: Sequence[RealizedRow],
    realized_census: Mapping[str, int],
    *,
    label: str,
    window_s: int = WAVE_WINDOW_S,
    strata: Mapping[int, Sequence[StratumRow]] | None = None,
    tails: Mapping[int, TailContrast] | None = None,
    blocks: Mapping[int, Sequence[StabilityBlock]] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"=== concentration -> forward return | {label} | {MODEL_ID} ===")
    feature = results[0].feature if results else "gross"
    lines.append(f"feature source: {FEATURE_SOURCE}, feature {feature}_pct, window {window_s}s")
    lines.append("")
    lines.append("corpus census")
    for key in sorted(census):
        lines.append(f"  {key:38s} {census[key]}")
    lines.append("")
    for res in results:
        lines.append(f"--- horizon t+{res.k_min}min, n={res.n} ---")
        lines.append(f"  {'bucket':8s} {'n':>5s} {'mean%':>10s} {'median%':>10s} {'hit%':>7s}")
        for b in res.buckets:
            if b.mean is None:
                lines.append(f"  {b.label:8s} {b.n:5d} {'refused':>10s} {'refused':>10s} {'-':>7s}")
            else:
                lines.append(
                    f"  {b.label:8s} {b.n:5d} {_fmt(b.mean):>10s} {_fmt(b.median):>10s} "
                    f"{_fmt(b.hit_rate, 1):>7s}"
                )
        lines.append(
            f"  spearman rho {_fmt(res.rho, 3)} (p {res.p_value if res.p_value is None else round(res.p_value, 4)}), "
            f"post-wave-baseline rho {_fmt(res.post_wave_rho, 3)}"
        )
        lines.append(f"  time split: train n={res.n_train} rho {_fmt(res.train_rho, 3)} | "
                     f"test n={res.n_test} rho {_fmt(res.test_rho, 3)} "
                     f"p {res.test_p if res.test_p is None else round(res.test_p, 4)}")
        for name, table in (("train", res.train), ("test", res.test)):
            cells = " ".join(
                f"{b.label}:n={b.n}" + ("" if b.median is None else f",med={_fmt(b.median)}")
                for b in table
            )
            lines.append(f"    {name:5s} {cells}")
        lines.append(
            f"  control {res.control_feature}: rho {_fmt(res.control_rho, 3)}; "
            f"{res.feature} partial rho (control removed) {_fmt(res.partial_rho, 3)}"
        )
        lines.append(
            f"  marks: {res.marks_bracket} bracket VWAP, {res.marks_last_print} last print "
            f"(mint had gone quiet)"
        )
        sel = res.selection
        if sel is not None:
            lines.append(
                f"  selection: included n={sel.n_included} median conc "
                f"{_fmt(sel.median_included)}% ({_fmt(sel.share_over_50_included, 1)}% over 50) | "
                f"excluded n={sel.n_excluded} median conc {_fmt(sel.median_excluded)}% "
                f"({_fmt(sel.share_over_50_excluded, 1)}% over 50)"
            )
        lines.append(f"  VERDICT: {res.verdict}")
        rows = (strata or {}).get(res.k_min) or ()
        if rows:
            lines.append("  EXPLORATORY, wallet-count quartile x concentration bucket (median %):")
            for q in sorted({r.wallet_quartile for r in rows}):
                cells = " ".join(
                    f"{r.bucket}:n={r.n}" + ("" if r.median is None else f",med={_fmt(r.median)}")
                    for r in rows
                    if r.wallet_quartile == q
                )
                span = next(r for r in rows if r.wallet_quartile == q)
                lines.append(f"    wallets {span.wallet_lo:>3d}-{span.wallet_hi:<3d} {cells}")
        block_rows = (blocks or {}).get(res.k_min) or ()
        if block_rows:
            lines.append(f"  stability, {STABILITY_BLOCK_HOURS}h blocks of launch time:")
            for b in block_rows:
                stamp = _utc(b.start_ms)
                lines.append(
                    f"    {stamp}  n={b.n:4d}  rho {_fmt(b.rho, 3):>7s}  "
                    f"median ret {_fmt(b.median_return):>9s}%  "
                    f"median feature {_fmt(b.median_feature):>8s}"
                )
        tail = (tails or {}).get(res.k_min)
        if tail is not None:
            lines.append(
                f"  EXPLORATORY tail contrast at >={tail.min_wallets} wallets: "
                f">{tail.edge_pct}% n={tail.n_high} med {_fmt(tail.median_high)}% vs "
                f"<={tail.edge_pct}% n={tail.n_low} med {_fmt(tail.median_low)}% "
                f"=> {_fmt(tail.diff_pp)}pp, p "
                f"{tail.p_value if tail.p_value is None else round(tail.p_value, 4)}"
            )
            lines.append(
                f"    held-out half: n={tail.n_test_high}/{tail.n_test_low}, "
                f"{_fmt(tail.test_diff_pp)}pp, p "
                f"{tail.test_p if tail.test_p is None else round(tail.test_p, 4)}"
            )
        lines.append("")
    lines.append("realized arm (our own closed trades)")
    for key in sorted(realized_census):
        lines.append(f"  {key:38s} {realized_census[key]}")
    for row in realized:
        lines.append(
            f"  {row.trade_id[:18]:18s} {row.lane:14s} {row.mode:7s} "
            f"wave {_fmt(row.wave.gross_pct)}% pnl {_fmt(row.pnl_pct)}%"
        )
    return "\n".join(lines)


def to_json(
    results: Sequence[HorizonResult],
    census: Mapping[str, int],
    realized: Sequence[RealizedRow],
    realized_census: Mapping[str, int],
    samples: Sequence[Sample] | None = None,
) -> dict[str, Any]:
    def bucket_json(b: BucketStat) -> dict[str, Any]:
        return {
            "label": b.label,
            "n": b.n,
            "mean": None if b.mean is None else float(b.mean),
            "median": None if b.median is None else float(b.median),
            "hit_rate": None if b.hit_rate is None else float(b.hit_rate),
            "reason": b.reason,
        }

    out: dict[str, Any] = {
        "model": MODEL_ID,
        "feature_source": FEATURE_SOURCE,
        "window_s": WAVE_WINDOW_S,
        "generated_ms": now_ms(),
        "census": dict(census),
        "horizons": [
            {
                "k_min": r.k_min,
                "n": r.n,
                "buckets": [bucket_json(b) for b in r.buckets],
                "rho": r.rho,
                "p_value": r.p_value,
                "post_wave_rho": r.post_wave_rho,
                "n_train": r.n_train,
                "n_test": r.n_test,
                "train_rho": r.train_rho,
                "test_rho": r.test_rho,
                "test_p": r.test_p,
                "train_buckets": [bucket_json(b) for b in r.train],
                "test_buckets": [bucket_json(b) for b in r.test],
                "replicates": r.replicates,
                "verdict": r.verdict,
                "feature": r.feature,
                "marks_bracket": r.marks_bracket,
                "marks_last_print": r.marks_last_print,
                "stability_blocks": [
                    {
                        "start_ms": b.start_ms,
                        "n": b.n,
                        "rho": b.rho,
                        "median_return": None if b.median_return is None else float(b.median_return),
                        "median_feature": None
                        if b.median_feature is None
                        else float(b.median_feature),
                    }
                    for b in stability_blocks(samples or (), r.k_min, feature=r.feature)
                ]
                if samples
                else [],
                "control_feature": r.control_feature,
                "control_rho": r.control_rho,
                "partial_rho": r.partial_rho,
                "selection": None
                if r.selection is None
                else {
                    "n_included": r.selection.n_included,
                    "n_excluded": r.selection.n_excluded,
                    "median_included": None
                    if r.selection.median_included is None
                    else float(r.selection.median_included),
                    "median_excluded": None
                    if r.selection.median_excluded is None
                    else float(r.selection.median_excluded),
                    "share_over_50_included": None
                    if r.selection.share_over_50_included is None
                    else float(r.selection.share_over_50_included),
                    "share_over_50_excluded": None
                    if r.selection.share_over_50_excluded is None
                    else float(r.selection.share_over_50_excluded),
                },
            }
            for r in results
        ],
        "realized": {
            "census": dict(realized_census),
            "rows": [
                {
                    "trade_id": r.trade_id,
                    "lane": r.lane,
                    "mode": r.mode,
                    "token": r.token,
                    "pnl_pct": float(r.pnl_pct),
                    "gross_pct": None if r.wave.gross_pct is None else float(r.wave.gross_pct),
                    "net_pct": None if r.wave.net_pct is None else float(r.wave.net_pct),
                }
                for r in realized
            ],
        },
    }
    if samples is not None:
        out["samples"] = [
            {
                "token": s.token,
                "t0_ms": s.t0_ms,
                "gross_pct": None if s.wave.gross_pct is None else float(s.wave.gross_pct),
                "net_pct": None if s.wave.net_pct is None else float(s.wave.net_pct),
                "top1_pct": None if s.wave.top1_pct is None else float(s.wave.top1_pct),
                "hhi": None if s.wave.hhi is None else float(s.wave.hhi),
                "wallets": s.wave.wallets,
                "buys": s.wave.buys,
                "supply_basis": s.wave.supply_basis,
                "returns": {
                    str(k): {
                        "coverage": r.coverage,
                        "ret_vwap_pct": None if r.ret_vwap_pct is None else float(r.ret_vwap_pct),
                        "ret_post_pct": None if r.ret_post_pct is None else float(r.ret_post_pct),
                        "mark_basis": r.mark_basis,
                    }
                    for k, r in s.returns.items()
                },
            }
            for s in samples
        ]
    return out


def open_readonly(path: str) -> sqlite3.Connection:
    """Read-only by URI, because this module must never be able to write to a live box."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--db", required=True, help="path to a kaiba.db; opened READ-ONLY")
    parser.add_argument("--chain", default="sol")
    parser.add_argument("--window-s", type=int, default=WAVE_WINDOW_S)
    parser.add_argument("--horizons", default=",".join(str(k) for k in FORWARD_HORIZONS_MIN))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--block-hours", type=int, default=STABILITY_BLOCK_HOURS)
    parser.add_argument(
        "--external",
        action="store_true",
        help="also compute kaiba.intelligence.launch_concentration's numbers per mint",
    )
    parser.add_argument(
        "--feature",
        default="gross",
        choices=FEATURES,
        help="gross counts every buy in the window (re-buys twice); net is buys minus sells",
    )
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--dump-samples", action="store_true")
    parser.add_argument("--label", default=None)
    args = parser.parse_args(argv)

    chain = Chain(args.chain)
    horizons = tuple(int(k) for k in str(args.horizons).split(",") if k.strip())
    conn = open_readonly(args.db)
    samples, census = collect(
        chain,
        conn,
        horizons=horizons,
        window_s=args.window_s,
        limit=args.limit,
        external=args.feature.startswith(EXTERNAL_PREFIX) or args.external,
    )
    results = [analyse_horizon(samples, k, feature=args.feature) for k in horizons]
    realized, realized_census = realized_arm(chain, conn)
    label = args.label or args.db
    strata = {k: stratified_medians(samples, k) for k in horizons}
    blocks = {
        k: stability_blocks(samples, k, feature=args.feature, block_hours=args.block_hours)
        for k in horizons
    }
    tails = {}
    for k in horizons:
        rows = strata[k]
        if not rows:
            continue
        cut = max(r.wallet_lo for r in rows if r.wallet_quartile == 3)
        tails[k] = tail_contrast(samples, k, min_wallets=cut)
    print(
        render(
            results,
            census,
            realized,
            realized_census,
            label=label,
            window_s=args.window_s,
            strata=strata,
            tails=tails,
            blocks=blocks,
        )
    )
    if args.json_path:
        payload = to_json(
            results, census, realized, realized_census, samples if args.dump_samples else None
        )
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"\njson written to {args.json_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
