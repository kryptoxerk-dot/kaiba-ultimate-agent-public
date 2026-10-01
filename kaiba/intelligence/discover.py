"""Find wallets worth watching from flow we observed ourselves, and refuse to oversell them.

Our 6,236 wallets came from GMGN's leaderboard labels. ``docs/EDGE-AND-VARIABLES.md``
("The wallet leaderboards are advertisements") and ``docs/research/13-validation-and-
copytrading-2026.md`` Part A make that source unusable as evidence:

* GMGN defines "Smart Money" as *"wallet who often earns money"*, with no published
  method, no cost-basis convention and no out-of-sample test.
* Kolscan, the other scoreboard, was bought by pump.fun — the venue earning the fees owns
  the board advertising how profitable trading is — and disclaims its own accuracy.
* A direct check of four leaderboard wallets found two running at 36% and 49% transaction
  failure rates. Jupiter's published sybil filter excludes anything above 50%.
* In arXiv:2602.14860's own Table I, several top-PnL wallets executed **only sells** —
  1,793 trades, zero buys. That is settlement plumbing, not a copyable strategy.

So this module discovers wallets from ``swaps``, ``first_buyers``, ``swap_meta`` and
``tokens`` instead: structural facts we recorded, not a vendor's opinion.

**The trap, which this module exists to not fall into.** Ranking the wallets we happen to
have seen by profit and calling the top ones smart rebuilds exactly the artefact the
research condemns, with our logo on it. The arithmetic is in research 13 §A2: screening
100,000 zero-skill wallets produces about 98 with a perfect 10-for-10 record and about
2,460 at "45% over 100 trades", and the best of the 3,142,559-wallet pump.fun population
shows a 5.09-sigma track record under a pure-noise null. A wallet found by ranking must
clear a bar set by *N*, and N is the number of wallets screened, not the number
shortlisted.

Four consequences are wired into the code rather than written in a caveat:

1. :class:`ChanceReport` is part of every result. It carries the screen size, the
   Bonferroni-corrected alpha, the expected maximum z under the null, and — for each
   candidate — how many zero-skill wallets in a screen this size would match it. A cohort
   cannot be read here without its own deflation.
2. :func:`min_closed_trades` computes the sample a win rate needs before it is
   distinguishable from the population's own base rate, at the screen-corrected alpha.
   Its arithmetic reproduces research 13's published table exactly (40% vs 30% needs 136
   closed trades as a single test, 704 out of a screen of 100,000), which is the check
   that the implementation is the one the research described. Every wallet below the gate
   is labelled ``unvalidated`` however good it looks.
3. The **known-bad shapes are rejected before anything is scored**, reusing
   :mod:`kaiba.intelligence.grade`'s own sell-only and buy-starved rules rather than
   restating them: sell-only and buy/sell-asymmetric addresses, high transaction-failure
   bots, addresses that are one operator's many addresses, wallets whose apparent edge is
   one token, and — added after the first live run handed a nine-minute scalping bot back
   as the best wallet in the database — wallets whose median round trip is shorter than
   any delay we could copy inside (see :data:`MIN_MEDIAN_HOLD_S`).
4. The only thing that could ever validate any of this is forward performance against a
   matched control, which is the test the literature has never run.
   :func:`freeze_cohort` writes a frozen cohort into ``wallet_cohorts`` /
   ``wallet_cohort_freezes`` so :func:`kaiba.learning.validation.control_arm` measures it
   with the same code that measures a graded cohort. There is no second matched-control
   implementation here; :func:`forward_report` is a thin call into that one.

**What this module will not do.** It never writes ``trusted_copy``. The most it may
propose is ``research`` (:func:`propose_research_cohort`), and only for a row that is not
already blacklisted or trusted. Promotion requires measured forward performance and
nothing here measures that.

**Which tokens a wallet traded is a fact about our sampling until proved otherwise.**

``distinct_tokens`` counts the tokens we saw an address in. It is a *floor*, and the two
collection routes give it very different worth. ``helius:backfill`` walks a **wallet's**
own transaction history, so a token count taken from it is a count of that wallet's
tokens. ``pumpfun:trades`` walks a **token's** trade tape, so a wallet that only ever
appears through that route has been seen through the tokens we happened to pull: a wallet
that traded ten of them reads as a one-token wallet when we pulled one, and pump.fun's
trades endpoint serves a hot window, so most mints can never be pulled at all
(:mod:`kaiba.ingest.tape`).

A floor supports "at least N" and can never support "at most N". Three of the reject
rules below are "at most" claims — :attr:`RejectReason.SINGLE_TOKEN`
("one token, indistinguishable from having been in the bundle"),
:attr:`RejectReason.SINGLE_TOKEN_PNL` (a share whose denominator is the token set) and
:attr:`RejectReason.CREATOR_SELF_DEALING` (a ratio whose denominator is the token set) —
so each of them is a statement about the wallet only when the wallet's own history was
walked. Where it was not, the address is reported **unassessed**
(:class:`Unassessed`, :class:`UnassessedReason`) rather than rejected: we did not look, so
we have nothing to say, and saying "one token" would be a finding we never established.
It is not promoted either — an unassessed address is not a candidate.

The tape's own gate, :func:`kaiba.ingest.tape.complete_tokens`, is imported rather than
re-derived here; it answers the *per-token* question (do we hold every trade of this
mint) and travels with the count as :attr:`CandidateFeatures.tokens_complete_tape`. It
cannot answer the *per-wallet* question, because no amount of complete token tapes tells
us about a token we never pulled, and that distinction is the whole of the fix.

**What is weak about it, stated here rather than discovered later.**

* Our tape is a convenience sample twice over. ``helius:backfill`` walked the history of
  wallets we chose; ``pumpfun:trades`` walked the trade tape of tokens we chose. The
  population screened is therefore not a random sample of anything, and a base rate
  computed from it is the base rate of our sampling, not of the venue.
* A wallet walk is the best wallet-scoped evidence we have and is still not a proof: the
  backfill has its own page budget, and nothing records a per-wallet coverage claim the
  way ``token_tape`` records a per-token one. It establishes that we looked at the wallet,
  which is exactly what the "at most" rules need and no more than that.
* Realised-only accounting. :mod:`kaiba.intelligence.pnl` closes an episode when inventory
  returns to zero, so a wallet still holding a dead bag looks flat rather than down. That
  is the same artefact that makes 73% of pump.fun wallets appear profitable.
* Transaction-failure coverage comes from ``swap_meta``, which only the Helius pass
  populates. A wallet with no meta rows has an **unknown** failure rate, which is recorded
  as unknown and blocks the wallet from ever being described as clean. Unknown is not
  good.
* ``first_buyers`` rank 1 dominates our rows because the backfill recorded the earliest
  buyer *it observed*, not the token's true first buyer. "Early" here means early in our
  tape. The feature is reported; it is not evidence of insider access.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.db import ensure_db, fetch_all, fetch_one, get_conn, jdump, jload, tx, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, digest, now_ms
from kaiba.ingest import tape as tape_coverage
from kaiba.intelligence import grade
from kaiba.intelligence.entity import SOLO_PREFIX, _UnionFind, entity_ids_for
from kaiba.intelligence.hubs import safe_normalize
from kaiba.intelligence.pnl import MATH, Episode, reconstruct_wallet
from kaiba.learning import metrics, validation

log = logging.getLogger(__name__)

MODEL_ID = "kaiba-discovery-v1"

# --------------------------------------------------------------------------------------
# thresholds, each with the reason it has the value it has
# --------------------------------------------------------------------------------------

#: Below this many observed swaps we have not seen a wallet, we have glimpsed it. Such an
#: address is UNASSESSABLE, which is not the same as rejected and not the same as average.
MIN_SWAPS_TO_ASSESS = 4

#: One token is one bet. The research's single clearest lesson about leaderboards is that
#: a great record on one launch is indistinguishable from having been in the bundle.
#:
#: The rule only applies to an address whose token count we could establish; see
#: :data:`WALLET_SCOPED_SOURCES` and :class:`TokenCoverage`.
MIN_DISTINCT_TOKENS = 2

#: Values of ``swaps.source`` that enumerate a **wallet's** own history rather than a
#: token's trade tape. A row from one of these means we looked at the wallet; a row from
#: anywhere else means we looked at a token and this wallet happened to be in it.
#:
#: Membership is positive evidence and the list is deliberately short: an unlisted source
#: is treated as token-scoped, which can only ever make us claim *less*. A run that meets
#: a source not listed here says so in :attr:`DiscoveryReport.notes` rather than quietly
#: counting it as a walk. ``kaiba.ingest.tape.WALLET_SOURCE`` is imported rather than
#: spelled out so the two modules cannot drift about what the wallet route is called.
WALLET_SCOPED_SOURCES: frozenset[str] = frozenset({tape_coverage.WALLET_SOURCE})

#: Transaction failure rate. Jupiter's published sybil filter excludes wallets above 50%
#: ("code instead of food"); the audit found leaderboard wallets at 36% and 49% and read
#: both as bot execution. We reject at 30% because our question is narrower than Jupiter's
#: — not "is this a sybil" but "is this a decision a person made that we could copy" — and
#: a third of submissions failing is an execution loop, whichever it is.
TX_FAILURE_REJECT_RATE = 0.30
JUPITER_SYBIL_FAILURE_RATE = 0.50

#: Fewer observed submissions than this and the failure rate is not a rate. The wallet's
#: failure rate is then None with an UNAVAILABLE basis, and it carries a blocker.
TX_FAILURE_MIN_OBSERVED = 10

#: When one token supplies this much of a wallet's positive realised PnL, the wallet does
#: not have an edge, it had a token.
SINGLE_TOKEN_MAX_PNL_SHARE = 0.80

#: Median closed round trip below this is not a decision anybody could act on.
#:
#: This rule is not in the original brief; the first live run put it there. The wallet with
#: the best record in our whole database — 29 wins from 38 closed round trips, a binomial p
#: of 2.8e-6, better than the Bonferroni line — turned out to be 144 swaps inside nine and a
#: half minutes, buying and selling the same mint on a one-second cadence with a 0%
#: transaction failure rate. None of the four rejects in the brief catch it: it is
#: buy/sell symmetric, it lands everything, it traded ten tokens, and its gains are spread
#: across them. It is a scalping bot.
#:
#: The justification for excluding it is stronger than "it looks like a bot", which would
#: be a guess. Research 13 §A3 is a theorem about the bonding curve: the copier's buy lands
#: after the leader's on a strictly convex curve and therefore strictly overpays, the
#: penalty is 6-15 percentage points per coin at *zero* latency, and the blog study's
#: shape — corroborated independently by the curve arithmetic — puts a copier at 2 seconds
#: of delay into guaranteed loss. A wallet whose median round trip is shorter than the
#: delay we could achieve is uncopyable by construction, whether or not it is skilled. So
#: this is an exclusion on mechanics, not on suspicion.
#:
#: 120 seconds is :mod:`kaiba.intelligence.grade`'s own "ultra short holds" figure, reused
#: so the two modules do not disagree about what counts as too fast.
MIN_MEDIAN_HOLD_S = 120

#: Below this many closed round trips a median hold is one or two trades, not a cadence.
CADENCE_MIN_CLOSED = 5

#: Two addresses that buy the same token in the same slot are, once it has happened on
#: this many distinct tokens, one hand. A single co-occurrence is a crowded block.
COENTITY_MIN_SHARED_TOKENS = 2

#: "The token later did well": the peak price observed on our own tape after the wallet's
#: first buy, as a multiple of the price it paid. Two is deliberately modest — it is a
#: descriptive feature, not a threshold anything is promoted on.
TOKEN_WIN_MULTIPLE = Decimal(2)

#: A peak computed from one later print is not a peak. Below this the token outcome is
#: UNAVAILABLE for that buy and the buy is excluded from the denominator, not counted as a
#: miss.
MIN_TAPE_POINTS_AFTER = 2

#: ``first_buyers`` rank counts as "early" only on a token where we saw enough buyers for a
#: rank to mean anything. Same figure as :mod:`kaiba.intelligence.grade` uses.
EARLY_RANK = 3
EARLY_MIN_OBSERVED_BUYERS = 10

#: The uncorrected significance level everything is deflated from.
DEFAULT_ALPHA = 0.05

#: The smallest edge over the population base rate worth sizing a sample for. Ten points
#: is the research's "large" row (40% vs 30%); anything smaller needs thousands of trades
#: and we will not have thousands of trades.
MIN_INTERESTING_EDGE = 0.10

#: Below this many pooled closed trades across the whole screen we will not claim to know
#: the population's base rate, so there is no null, so no wallet can be gated and every
#: wallet stays unvalidated. Never defaulted to 0.5: the true null in this market is below
#: zero once the 1.25% curve fee each way is paid, so 0.5 would be generous as well as
#: invented.
NULL_MIN_POOLED_TRADES = 100

#: How many unassessed addresses are written out in full in ``discovery_runs.payload_json``.
#: The counts are always complete; on this database the list is most of the screen, and a
#: run row is not the place to keep 20,000 of them.
UNASSESSED_PAYLOAD_LIMIT = 200

#: Cohorts this module refuses to overwrite under any circumstance.
PROTECTED_COHORTS: frozenset[str] = frozenset({"trusted_copy", "blacklist"})

#: The only cohort discovery may propose.
PROPOSED_COHORT = "research"

DISCOVERY_SOURCE = "kaiba:discovery"


class CandidateStatus(StrEnum):
    """Three honest outcomes and no fourth.

    ``SAMPLE_GATE_CLEARED`` says the wallet has enough closed trades for its rate to be
    testable against the population base rate at the screen-corrected alpha. It does not
    say the wallet passed, and it is not a grade. Only forward performance against a
    matched control could promote anything, and this module does not measure that.
    """

    UNASSESSABLE = "unassessable"
    UNVALIDATED = "unvalidated"
    SAMPLE_GATE_CLEARED = "sample_gate_cleared"


class UnassessedReason(StrEnum):
    """Why an address was neither rejected nor carried forward.

    A rejection is a finding. This is the absence of one, kept separate so a consumer can
    tell "we looked and it is a one-token wallet" from "we only ever saw this wallet
    through one token's tape". Collapsing those two into a reject is the bug this enum
    exists to end.
    """

    TOO_THIN = "too_thin"
    TOKEN_UNIVERSE_UNESTABLISHED = "token_universe_unestablished"


class RejectReason(StrEnum):
    """The known-bad shapes, each named after the evidence that names it."""

    SELL_ONLY = "sell_only"
    BUY_STARVED = "buy_starved"
    HIGH_TX_FAILURE = "high_tx_failure"
    UNCOPYABLE_CADENCE = "uncopyable_cadence"
    SAME_ENTITY = "same_entity"
    SINGLE_TOKEN = "single_token"
    SINGLE_TOKEN_PNL = "single_token_pnl"
    CREATOR_SELF_DEALING = "creator_self_dealing"
    QUARANTINE_TAG = "quarantine_tag"


# --------------------------------------------------------------------------------------
# the multiple-comparisons arithmetic — first-class, not a docstring
# --------------------------------------------------------------------------------------


def bonferroni_alpha(alpha: float = DEFAULT_ALPHA, screened: int = 1) -> float:
    """``alpha`` split across the number of wallets screened.

    This is the correction that actually applies when a wallet is picked off a board: the
    denominator is every wallet the screen looked at, including the ones it discarded.
    """
    return alpha / max(int(screened), 1)


def min_closed_trades_exact(
    p_alt: float,
    p_null: float,
    *,
    screened: int = 1,
    alpha: float = DEFAULT_ALPHA,
    power_z: float = validation.Z_POWER_80,
) -> float | None:
    """The unrounded sample size: ``(z_a*sqrt(p0*q0) + z_b*sqrt(p1*q1))^2 / (p1-p0)^2``.

    One-sided, 80% power, with ``z_a`` taken at the Bonferroni-corrected alpha — the
    correction that applies when a wallet is picked off a board of ``screened`` wallets.

    Rounded to the nearest integer this reproduces research 13 §A2's published table in
    every cell (40% vs 30% is 136 trades as a single test, 369 / 594 / 704 at screens of
    100 / 10,000 / 100,000; 35% vs 30% is 534 / 1,458 / 2,353 / 2,794; 33% vs 30% is
    1,468 / 4,027 / 6,507 / 7,729). ``tests/test_discover.py`` asserts all twelve, because
    a sample-size function that is merely plausible is the easiest place in this codebase
    to hide a bar that is too low.

    ``None`` when ``p_alt`` is not above ``p_null``: there is no edge to size a sample for,
    and returning a number would read as progress.
    """
    if not 0.0 < p_null < 1.0 or not 0.0 < p_alt < 1.0 or p_alt <= p_null:
        return None
    z_a = metrics.normal_inv_cdf(1.0 - bonferroni_alpha(alpha, screened))
    numerator = z_a * math.sqrt(p_null * (1 - p_null)) + power_z * math.sqrt(p_alt * (1 - p_alt))
    return numerator**2 / (p_alt - p_null) ** 2


def min_closed_trades(
    p_alt: float,
    p_null: float,
    *,
    screened: int = 1,
    alpha: float = DEFAULT_ALPHA,
    power_z: float = validation.Z_POWER_80,
) -> int | None:
    """:func:`min_closed_trades_exact` rounded **up**, which is the gate actually applied.

    The research's table rounds to nearest; we round up. A requirement of 704.34 trades
    met with 704 is 704, and rounding a requirement down — even by two thirds of a trade —
    is the wrong direction to be wrong in when the whole module exists to stop a bar being
    set too low. The difference is never more than one trade and the test pins both.
    """
    exact = min_closed_trades_exact(
        p_alt, p_null, screened=screened, alpha=alpha, power_z=power_z
    )
    return None if exact is None else int(math.ceil(exact))


def binomial_tail_p(wins: int, trades: int, p_null: float) -> float | None:
    """Exact ``P(X >= wins)`` for ``X ~ Binomial(trades, p_null)``.

    Exact rather than normal-approximated because the whole problem is small samples: at
    ten trades the normal approximation is the difference between "one in a thousand" and
    "one in three hundred", and the second is 3.3 wallets in a screen of 1,000.
    """
    if trades <= 0 or not 0.0 < p_null < 1.0:
        return None
    k = max(0, min(int(wins), int(trades)))
    q = 1.0 - p_null
    return sum(math.comb(trades, i) * p_null**i * q ** (trades - i) for i in range(k, trades + 1))


def expected_peers(screened: int, p: float | None) -> float | None:
    """How many of ``screened`` zero-skill wallets would look at least this good."""
    if p is None:
        return None
    return max(int(screened), 1) * p


class ChanceRow(BaseModel):
    """One row of "what a screen this size produces from pure noise"."""

    model_config = ConfigDict(extra="forbid")

    label: str
    p_per_wallet: float
    expected_hits: float


def chance_table(screened: int, p_null: float) -> list[ChanceRow]:
    """Research 13 §A2's noise table, recomputed for *our* screen and *our* base rate.

    The published version uses 100,000 wallets and a 50% null. Reprinting those numbers
    would be citation; recomputing them at the screen size and base rate actually in force
    is the only form in which the table constrains a decision.
    """
    rows: list[tuple[str, int, int]] = [
        ("10 of 10 wins", 10, 10),
        ("at least 15 of 20", 15, 20),
        ("at least 35 of 50", 35, 50),
        ("at least 45 of 100", 45, 100),
    ]
    out: list[ChanceRow] = []
    for label, wins, trades in rows:
        p = binomial_tail_p(wins, trades, p_null)
        if p is None:
            continue
        out.append(
            ChanceRow(label=label, p_per_wallet=p, expected_hits=expected_peers(screened, p) or 0.0)
        )
    return out


class ChanceReport(BaseModel):
    """The deflation that travels with the cohort.

    Every field here answers one question: *given that we looked at this many wallets, how
    many would look this good if none of them had any skill at all?*
    """

    model_config = ConfigDict(extra="forbid")

    screened_addresses: int
    screened_entities: int
    alpha: float = DEFAULT_ALPHA
    bonferroni_alpha: float = DEFAULT_ALPHA
    bonferroni_z: float = 0.0
    expected_max_z: float = 0.0
    expected_false_positives: float = 0.0
    null_win_rate: float | None = None
    null_basis: str = EvidenceBasis.UNAVAILABLE.value
    null_pooled_trades: int = 0
    min_interesting_rate: float | None = None
    min_closed_trades: int | None = None
    gate_cleared: int = 0
    best_p_under_null: float | None = None
    expected_peers_at_best: float | None = None
    table: list[ChanceRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def sentence(self) -> str:
        """The arithmetic in one line, for a reader who will not open the JSON."""
        if self.null_win_rate is None:
            return (
                f"Screened {self.screened_addresses} addresses "
                f"({self.screened_entities} entities) and could not measure a population "
                f"base rate from {self.null_pooled_trades} pooled closed trades "
                f"(needs {NULL_MIN_POOLED_TRADES}). With no null there is no test, so no "
                "wallet here is distinguishable from luck and none is offered as if it were."
            )
        gate = (
            "no gate could be computed"
            if self.min_closed_trades is None
            else f"the gate is {self.min_closed_trades} closed trades"
        )
        return (
            f"Screened {self.screened_addresses} addresses ({self.screened_entities} "
            f"entities) against a base rate of {self.null_win_rate:.1%} from "
            f"{self.null_pooled_trades} pooled closed trades. At alpha={self.alpha} "
            f"uncorrected, {self.expected_false_positives:.1f} zero-skill wallets in a "
            f"screen this size would look significant; the Bonferroni alpha is "
            f"{self.bonferroni_alpha:.2e} (z={self.bonferroni_z:.2f}) and the best of "
            f"{self.screened_addresses} pure-noise wallets is expected at "
            f"z={self.expected_max_z:.2f}. To detect "
            f"{(self.min_interesting_rate or 0):.0%} against {self.null_win_rate:.0%}, "
            f"{gate}; {self.gate_cleared} wallet(s) cleared it."
        )


# --------------------------------------------------------------------------------------
# what we observed about one wallet, inside the window
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenCoverage:
    """How far our sampling lets us speak about *which* tokens an address traded.

    Three states, and the middle one is the whole point:

    * ``walk_rows > 0`` — the wallet's own history was walked, so
      :attr:`CandidateFeatures.distinct_tokens` is a count of its tokens and an "at most"
      claim about it is a claim about the wallet.
    * ``walk_rows == 0`` — every row we hold for it came from a token we chose to pull, so
      the same number is a floor on our sampling. "At least two tokens" still holds;
      "only one token" is not ours to say.
    * ``tokens_observed == 0`` — nothing at all.

    ``tokens_complete_tape`` is the per-token question, answered by
    :func:`kaiba.ingest.tape.complete_tokens` rather than re-derived here. It travels with
    the count because it says how much of what we *did* pull is whole; it cannot promote
    the first state into the second, because a mint we never pulled leaves no trace to
    count.
    """

    tokens_observed: int = 0
    tokens_complete_tape: int = 0
    walk_rows: int = 0
    sources: tuple[str, ...] = ()

    @property
    def universe_established(self) -> bool:
        """Did we look at *this wallet*, rather than at tokens it turned up in?"""
        return self.walk_rows > 0

    @property
    def basis(self) -> EvidenceBasis:
        if self.universe_established:
            return EvidenceBasis.VERIFIED_ONCHAIN
        return EvidenceBasis.UNAVAILABLE

    @property
    def detail(self) -> str:
        tape = (
            f"{self.tokens_complete_tape} of {self.tokens_observed} with a proved-complete "
            "trade tape"
        )
        if self.universe_established:
            return (
                f"{self.walk_rows} row(s) from a wallet-history walk ({tape}), so the token "
                "count is this wallet's, not our sampling's"
            )
        seen = ", ".join(self.sources) or "no rows"
        return (
            f"every row came from a per-token trade tape [{seen}] ({tape}); this wallet's own "
            "history was never walked, so its token count is a floor on what we pulled and "
            "not a fact about the wallet"
        )


class CandidateFeatures(BaseModel):
    """Structural facts, not a score. Every ``None`` here is a thing nobody measured."""

    model_config = ConfigDict(extra="forbid")

    swaps: int = 0
    buys: int = 0
    sells: int = 0
    buy_share: float | None = None
    distinct_tokens: int = 0
    #: Of :attr:`distinct_tokens`, how many hold a tape proved complete back to launch,
    #: per :func:`kaiba.ingest.tape.complete_tokens`. The basis of ``distinct_tokens``
    #: itself is in :attr:`Candidate.bases`; see :class:`TokenCoverage`.
    tokens_complete_tape: int = 0
    #: Swap rows for this address that came from a wallet-history walk. Zero means every
    #: number above was observed through tokens we chose, and the token count is a floor.
    wallet_walk_rows: int = 0
    distinct_token_creators: int | None = None
    distinct_active_days: int = 0
    closed_trades: int = 0
    open_trades: int = 0
    contaminated_trades: int = 0
    wins: int = 0
    win_rate: float | None = None
    big_wins: int = 0
    median_hold_s: int | None = None
    realized_pnl_native: int = 0
    best_token_pnl_share: float | None = None
    first_buyer_tokens: int = 0
    early_rank_tokens: int = 0
    peak_hit_tokens: int = 0
    peak_assessable_tokens: int = 0
    peak_hit_rate: float | None = None
    graduated_tokens: int = 0
    tx_observed: int = 0
    tx_failures: int = 0
    tx_failure_rate: float | None = None
    first_seen_ms: int | None = None
    last_seen_ms: int | None = None


class Candidate(BaseModel):
    """One surviving address, with its own deflation attached."""

    model_config = ConfigDict(extra="forbid")

    address: str
    chain: str
    entity_key: str
    status: CandidateStatus
    features: CandidateFeatures
    bases: dict[str, str] = Field(default_factory=dict)
    unknowns: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    p_under_null: float | None = None
    expected_peers: float | None = None

    @property
    def validated(self) -> bool:
        """Always ``False``. Kept as a property so nothing has to invent the concept.

        Nothing in discovery validates a wallet. The only route is forward performance
        against a matched control (:func:`forward_report`), measured after a freeze.
        """
        return False


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str
    reason: RejectReason
    detail: str


class Unassessed(BaseModel):
    """An address we declined to judge, with the rule we could not apply and why.

    ``would_have_been`` names the reject that the observed numbers would have produced had
    the evidence supported it. It is recorded so the cost of our sampling stays countable:
    an operator can read "n addresses would have been rejected SINGLE_TOKEN on evidence we
    do not have" without that ever having been written down as a rejection.
    """

    model_config = ConfigDict(extra="forbid")

    address: str
    reason: UnassessedReason
    detail: str
    would_have_been: RejectReason | None = None


class DiscoveryReport(BaseModel):
    """The output. Reading the candidate list without :attr:`chance` is a misuse."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    chain: str
    model_version: str = MODEL_ID
    run_ms: int = Field(default_factory=now_ms)
    as_of_ms: int = 0
    screened: list[str] = Field(default_factory=list)
    unassessable: int = 0
    #: Addresses we declined to judge, each with the rule we could not apply. Every
    #: :attr:`unassessable` address appears here too, as :attr:`UnassessedReason.TOO_THIN`.
    unassessed: list[Unassessed] = Field(default_factory=list)
    rejected: list[Rejection] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    chance: ChanceReport
    cohort_id: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def reject_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rejected:
            out[r.reason.value] = out.get(r.reason.value, 0) + 1
        return out

    @property
    def unassessed_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for u in self.unassessed:
            out[u.reason.value] = out.get(u.reason.value, 0) + 1
        return out

    @property
    def withheld_rejects(self) -> dict[str, int]:
        """Rejections the observed numbers implied but the evidence did not support.

        The count of what our sampling costs us, kept countable and kept out of
        :attr:`reject_counts`, where it would read as a set of findings.
        """
        out: dict[str, int] = {}
        for u in self.unassessed:
            if u.would_have_been is not None:
                out[u.would_have_been.value] = out.get(u.would_have_been.value, 0) + 1
        return out

    @property
    def gate_cleared(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status is CandidateStatus.SAMPLE_GATE_CLEARED]

    @property
    def headline(self) -> str:
        cleared = len(self.gate_cleared)
        if cleared == 0:
            tail = (
                "Not one wallet in our data has enough closed trades for its record to be "
                "distinguishable from luck, so the correct output is a cohort labelled "
                "candidate, unvalidated, n too small."
            )
        else:
            expected = self.chance.expected_peers_at_best
            peers = "an unknown number" if expected is None else f"about {expected:.1f}"
            best_p = self.chance.best_p_under_null
            shown = "unmeasured" if best_p is None else f"p={best_p:.2e}"
            tail = (
                f"{cleared} wallet(s) cleared the sample-size gate; the best is {shown} "
                f"under the null, which {peers} of the {self.chance.screened_addresses} "
                "screened wallets would match by chance."
            )
        no_record = sum(1 for c in self.candidates if c.status is CandidateStatus.UNASSESSABLE)
        withheld = self.withheld_rejects
        held = (
            ""
            if not withheld
            else (
                " "
                + ", ".join(f"{n} {name}" for name, n in sorted(withheld.items()))
                + " were withheld for want of coverage rather than rejected;"
            )
        )
        gapped = sum(
            1
            for u in self.unassessed
            if u.reason is UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED
        )
        return (
            f"Screened {len(self.screened)} addresses "
            f"({self.chance.screened_entities} entities); rejected "
            f"{len({r.address for r in self.rejected})} on structure "
            f"({len(self.rejected)} reasons);{held} {gapped} unassessed because their token "
            f"set is our sampling and not a fact; {self.unassessable} too thin to look at; "
            f"{len(self.candidates)} candidates, of which {no_record} have no closed round "
            f"trip at all. {tail}"
        )


# --------------------------------------------------------------------------------------
# loading the window
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _TokenTape:
    """Implied prices for one token, from our own swaps, oldest first.

    Price is ``amount_native / amount_token`` in base units — lamports per atom. It never
    passes through a provider's price field, so it is available for every swap that
    recorded both legs, and it is the same quantity regardless of which pass collected it.
    """

    points: list[tuple[int, Decimal]] = field(default_factory=list)
    migrated_ms: int | None = None
    creator: str | None = None

    def peak_after(self, ts_ms: int) -> tuple[Decimal | None, int]:
        """``(peak price strictly after ts_ms, how many prints that peak rests on)``."""
        later = [p for t, p in self.points if t > ts_ms]
        if len(later) < MIN_TAPE_POINTS_AFTER:
            return None, len(later)
        return max(later), len(later)


@dataclass(slots=True)
class _Flow:
    """Everything the run reads, loaded once. Per-wallet queries would be 674 round trips."""

    chain: Chain
    as_of_ms: int
    swaps: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    first_buyers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    buyers_per_token: dict[str, int] = field(default_factory=dict)
    tapes: dict[str, _TokenTape] = field(default_factory=dict)
    tx_totals: dict[str, int] = field(default_factory=dict)
    tx_failures: dict[str, int] = field(default_factory=dict)
    created_tokens: dict[str, int] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)
    slot_cutoff: int | None = None
    #: Mints whose trade tape is proved complete back to launch. Read once through
    #: :func:`kaiba.ingest.tape.complete_tokens`, which is the shared gate; this module
    #: does not carry its own copy of the completeness rule.
    complete_tape: frozenset[str] = frozenset()
    #: Address -> how many of its rows came from a wallet-history walk.
    walk_rows: dict[str, int] = field(default_factory=dict)
    #: Address -> the distinct ``swaps.source`` values behind its rows.
    sources: dict[str, set[str]] = field(default_factory=dict)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return d


def _load_flow(conn: sqlite3.Connection, chain: Chain, as_of_ms: int) -> _Flow:
    """One pass over every table this module reads, filtered to ``ts_ms <= as_of_ms``.

    The cutoff is the anti-look-ahead device. Everything downstream sees only what existed
    at ``as_of_ms``, which is what makes a freeze at that instant a genuine starting line
    rather than a re-description of the future.

    One reading deliberately escapes the cutoff: ``token_tape`` coverage is read as it
    stands now, because :func:`kaiba.ingest.tape.complete_tokens` answers "do we hold the
    whole tape" and not "did we hold it at ``as_of_ms``". It is used here only as the
    basis attached to a number — never to justify a rejection — so a record written after
    the cutoff can change what a report *says about itself* and cannot change who was
    rejected or carried forward.
    """
    flow = _Flow(chain=chain, as_of_ms=as_of_ms)
    flow.complete_tape = frozenset(tape_coverage.complete_tokens(chain, conn))
    rows = fetch_all(
        conn,
        "SELECT ts_ms, wallet, token, side, amount_token, amount_native, usd_value, chain, "
        "slot, fee_payer, source FROM swaps WHERE chain = ? AND ts_ms <= ? "
        "ORDER BY wallet, ts_ms, id",
        (chain.value, as_of_ms),
    )
    for row in rows:
        wallet = str(row["wallet"])
        flow.swaps.setdefault(wallet, []).append(row)
        source = str(row["source"] or "")
        flow.sources.setdefault(wallet, set()).add(source)
        if source in WALLET_SCOPED_SOURCES:
            flow.walk_rows[wallet] = flow.walk_rows.get(wallet, 0) + 1
        token = str(row["token"])
        tape = flow.tapes.setdefault(token, _TokenTape())
        qty, native = _dec(row["amount_token"]), _dec(row["amount_native"])
        if qty and native and qty > 0 and native > 0:
            tape.points.append((int(row["ts_ms"]), MATH.divide(native, qty)))
        if row["slot"] is not None:
            flow.slot_cutoff = max(flow.slot_cutoff or 0, int(row["slot"]))
    for tape in flow.tapes.values():
        tape.points.sort(key=lambda p: p[0])

    for row in fetch_all(
        conn,
        "SELECT token, wallet, rank, ts_ms, seconds_after_open FROM first_buyers "
        "WHERE chain = ? AND ts_ms <= ?",
        (chain.value, as_of_ms),
    ):
        flow.first_buyers.setdefault(str(row["wallet"]), []).append(row)
        token = str(row["token"])
        flow.buyers_per_token[token] = flow.buyers_per_token.get(token, 0) + 1

    for row in fetch_all(
        conn,
        "SELECT address, creator, migrated_ms FROM tokens WHERE chain = ?",
        (chain.value,),
    ):
        address = str(row["address"])
        tape = flow.tapes.get(address)
        migrated = int(row["migrated_ms"]) if row["migrated_ms"] is not None else None
        if tape is not None:
            tape.creator = str(row["creator"]) if row["creator"] else None
            tape.migrated_ms = migrated if migrated is not None and migrated <= as_of_ms else None
        if row["creator"]:
            creator = str(row["creator"])
            flow.created_tokens[creator] = flow.created_tokens.get(creator, 0) + 1

    _load_failures(conn, flow)

    for row in fetch_all(conn, "SELECT address, tags_json FROM wallets WHERE chain = ?", (chain.value,)):
        raw = row["tags_json"]
        if raw and raw != "[]":
            flow.tags[str(row["address"])] = [str(t) for t in (_json_list(raw))]
    return flow


def _json_list(raw: Any) -> list[Any]:
    from kaiba.core.db import jload

    value = jload(str(raw), [])
    return list(value) if isinstance(value, list) else []


def _load_failures(conn: sqlite3.Connection, flow: _Flow) -> None:
    """Per-submitter transaction failure counts from ``swap_meta``.

    ``swap_meta`` holds a row per transaction the Helius pass walked, including the ones
    that failed and therefore never became a swap — which is the only place in our data a
    failure is visible at all. The submitter is the fee payer, falling back to the first
    signer; that is the address whose execution loop is failing.

    Windowing is by slot rather than by timestamp because a failed transaction has no
    ``swaps`` row to take a timestamp from. ``slot_cutoff`` is the highest slot among the
    swaps inside the window, which is exact at the boundary to within one slot.
    """
    from kaiba.core.db import jload

    for row in fetch_all(conn, "SELECT meta_json FROM swap_meta WHERE chain = ?", (flow.chain.value,)):
        meta = jload(str(row["meta_json"]), {})
        if not isinstance(meta, dict):
            continue
        slot = meta.get("slot")
        if flow.slot_cutoff is not None and isinstance(slot, int) and slot > flow.slot_cutoff:
            continue
        signers = meta.get("signers")
        who = meta.get("fee_payer") or (signers[0] if isinstance(signers, list) and signers else None)
        if not who:
            continue
        who = str(who)
        failed = meta.get("failed")
        if failed is None:
            continue  # a row that never recorded an outcome is not a success
        flow.tx_totals[who] = flow.tx_totals.get(who, 0) + 1
        if failed:
            flow.tx_failures[who] = flow.tx_failures.get(who, 0) + 1


# --------------------------------------------------------------------------------------
# counting entities rather than addresses
# --------------------------------------------------------------------------------------


def entity_keys(
    conn: sqlite3.Connection, chain: Chain, addresses: Sequence[str], flow: _Flow
) -> dict[str, str]:
    """Address -> operator key. Five addresses from one hand must count once.

    Three sources, strongest first:

    1. :mod:`kaiba.intelligence.entity`'s resolved graph, which is the authority.
    2. **Observed co-entity evidence from our own tape**, because on this database the
       entity graph is empty: ``entities`` has no rows, so source 1 currently resolves
       nothing at all and address-counting would silently be entity-counting. Two rules,
       both mechanical: buying the same token in the same slot on at least
       :data:`COENTITY_MIN_SHARED_TOKENS` distinct tokens, and sharing a third-party fee
       payer. Same-slot co-purchase on several launches is a bundle; a third party paying
       for two wallets controls both.
    3. Failing both, the address stands alone, prefixed ``solo:`` exactly as
       :func:`kaiba.intelligence.entity.independent_entity_count` does — absence of
       evidence is not evidence of independence, but it is the only honest default.
    """
    resolved = entity_ids_for(chain, addresses, conn)
    wanted = {safe_normalize(a, chain) for a in addresses}
    uf = _UnionFind()

    shared_tokens: dict[tuple[str, str], set[str]] = defaultdict(set)
    per_slot: dict[tuple[str, int], set[str]] = defaultdict(set)
    payer_wallets: dict[str, set[str]] = defaultdict(set)
    for address, rows in flow.swaps.items():
        if address not in wanted:
            continue
        for row in rows:
            if row["slot"] is not None and str(row["side"]) == "buy":
                per_slot[(str(row["token"]), int(row["slot"]))].add(address)
            payer = row["fee_payer"]
            if payer and str(payer) != address:
                payer_wallets[str(payer)].add(address)

    for (token, _slot), members in per_slot.items():
        ordered = sorted(members)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                shared_tokens[(a, b)].add(token)
    for (a, b), tokens in shared_tokens.items():
        if len(tokens) >= COENTITY_MIN_SHARED_TOKENS:
            uf.union(a, b)
    for members in payer_wallets.values():
        ordered = sorted(members)
        for other in ordered[1:]:
            uf.union(ordered[0], other)

    components = uf.components()
    out: dict[str, str] = {}
    for address in sorted(wanted):
        known = resolved.get(address)
        if known:
            out[address] = known
            continue
        root = uf.find(address) if address in uf.parent else address
        members = components.get(root, [])
        out[address] = f"observed:{root}" if len(members) > 1 else f"{SOLO_PREFIX}{address}"
    return out


# --------------------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------------------


def _episode_pnl_share(episodes: Sequence[Episode]) -> float | None:
    """Share of positive realised PnL coming from the single best token.

    ``None`` when nothing was realised at a profit: a wallet with no gains does not have a
    concentration problem, it has a different problem, and reporting 0.0 here would read
    as diversified.
    """
    per_token: dict[str, int] = {}
    for ep in episodes:
        if ep.scorable and ep.realized_pnl_native > 0:
            per_token[ep.token] = per_token.get(ep.token, 0) + ep.realized_pnl_native
    total = sum(per_token.values())
    if total <= 0:
        return None
    return max(per_token.values()) / total


def coverage_for(address: str, flow: _Flow) -> TokenCoverage:
    """What our two collection routes let us say about this address's token set.

    Public because the answer is the difference between a finding and a guess, and a
    caller that wants to check our arithmetic should not have to reimplement it.
    """
    rows = flow.swaps.get(address, [])
    tokens = {str(r["token"]) for r in rows}
    return TokenCoverage(
        tokens_observed=len(tokens),
        tokens_complete_tape=len(tokens & flow.complete_tape),
        walk_rows=flow.walk_rows.get(address, 0),
        sources=tuple(sorted(flow.sources.get(address, set()))),
    )


def _features(
    address: str, flow: _Flow
) -> tuple[CandidateFeatures, dict[str, str], list[str], TokenCoverage]:
    """Everything we observed about one address, plus the basis of each number."""
    rows = flow.swaps.get(address, [])
    episodes, summary = reconstruct_wallet(list(rows), as_of_ms=flow.as_of_ms)

    buys = sum(1 for r in rows if str(r["side"]) == "buy")
    sells = sum(1 for r in rows if str(r["side"]) == "sell")
    tokens = {str(r["token"]) for r in rows}
    days = {int(r["ts_ms"]) // 86_400_000 for r in rows}
    creators = {flow.tapes[t].creator for t in tokens if t in flow.tapes and flow.tapes[t].creator}

    fb_rows = flow.first_buyers.get(address, [])
    early_rank = sum(
        1
        for r in fb_rows
        if int(r["rank"]) <= EARLY_RANK
        and flow.buyers_per_token.get(str(r["token"]), 0) >= EARLY_MIN_OBSERVED_BUYERS
    )

    hits = assessable = graduated = 0
    # The entry price is this wallet's own print, not whatever else traded in that
    # millisecond. Two wallets buying the same token at the same timestamp paid different
    # prices on a curve, and taking the other one's would invent the result.
    first_buy: dict[str, tuple[int, Decimal | None]] = {}
    for r in rows:
        if str(r["side"]) != "buy":
            continue
        token = str(r["token"])
        if token in first_buy:
            continue
        qty, native = _dec(r["amount_token"]), _dec(r["amount_native"])
        price = MATH.divide(native, qty) if qty and native and qty > 0 and native > 0 else None
        first_buy[token] = (int(r["ts_ms"]), price)
    for token, (ts, entry) in first_buy.items():
        tape = flow.tapes.get(token)
        if tape is None:
            continue
        if tape.migrated_ms is not None and tape.migrated_ms > ts:
            graduated += 1
        peak, _n_after = tape.peak_after(ts)
        if entry is None or entry <= 0 or peak is None:
            continue
        assessable += 1
        if MATH.divide(peak, entry) >= TOKEN_WIN_MULTIPLE:
            hits += 1

    tx_total = flow.tx_totals.get(address, 0)
    tx_failed = flow.tx_failures.get(address, 0)
    failure_rate = tx_failed / tx_total if tx_total >= TX_FAILURE_MIN_OBSERVED else None

    total_trades = buys + sells
    coverage = coverage_for(address, flow)
    features = CandidateFeatures(
        swaps=len(rows),
        buys=buys,
        sells=sells,
        buy_share=(buys / total_trades) if total_trades >= grade.SELL_ONLY_MIN_TRADES else None,
        distinct_tokens=len(tokens),
        tokens_complete_tape=coverage.tokens_complete_tape,
        wallet_walk_rows=coverage.walk_rows,
        distinct_token_creators=len(creators) if creators else None,
        distinct_active_days=len(days),
        closed_trades=summary.closed_episodes,
        open_trades=summary.open_episodes,
        contaminated_trades=summary.contaminated_episodes,
        wins=summary.wins,
        win_rate=summary.win_rate,
        big_wins=summary.big_wins,
        median_hold_s=summary.median_hold_s,
        realized_pnl_native=summary.realized_pnl_native,
        best_token_pnl_share=_episode_pnl_share(episodes),
        first_buyer_tokens=len(fb_rows),
        early_rank_tokens=early_rank,
        peak_hit_tokens=hits,
        peak_assessable_tokens=assessable,
        peak_hit_rate=(hits / assessable) if assessable else None,
        graduated_tokens=graduated,
        tx_observed=tx_total,
        tx_failures=tx_failed,
        tx_failure_rate=failure_rate,
        first_seen_ms=summary.first_trade_ms,
        last_seen_ms=summary.last_trade_ms,
    )

    bases: dict[str, str] = {}
    unknowns: list[str] = []
    for name, value, basis in (
        ("buy_share", features.buy_share, EvidenceBasis.VERIFIED_ONCHAIN),
        ("win_rate", features.win_rate, EvidenceBasis.DERIVED),
        ("peak_hit_rate", features.peak_hit_rate, EvidenceBasis.DERIVED),
        ("tx_failure_rate", features.tx_failure_rate, EvidenceBasis.VERIFIED_ONCHAIN),
        ("best_token_pnl_share", features.best_token_pnl_share, EvidenceBasis.DERIVED),
        ("distinct_token_creators", features.distinct_token_creators, EvidenceBasis.DERIVED),
        ("median_hold_s", features.median_hold_s, EvidenceBasis.DERIVED),
    ):
        if value is None:
            bases[name] = EvidenceBasis.UNAVAILABLE.value
            unknowns.append(name)
        else:
            bases[name] = basis.value

    # The basis travels with the number. `distinct_tokens` is always reported — a floor is
    # worth having — but on a wallet we never walked it is a floor on our sampling, so its
    # basis is UNAVAILABLE and it is named as an unknown. `best_token_pnl_share` shares the
    # denominator, so it degrades from a derived fact to an estimate, and it is an estimate
    # that can only read too high: tokens we never pulled would dilute it.
    bases["distinct_tokens"] = coverage.basis.value
    if not coverage.universe_established:
        unknowns.append("distinct_tokens")
        if features.best_token_pnl_share is not None:
            bases["best_token_pnl_share"] = EvidenceBasis.ESTIMATED.value
    return features, bases, unknowns, coverage


# --------------------------------------------------------------------------------------
# structural rejects, applied before anything is ranked
# --------------------------------------------------------------------------------------


def _evidence_for(address: str, chain: Chain, features: CandidateFeatures, tags: Sequence[str]) -> grade.WalletEvidence:
    """A :class:`grade.WalletEvidence` carrying only what the reject rules read.

    Built here rather than through :func:`grade.build_evidence` for one reason: that
    function reads the whole of ``swaps`` for an address with no time bound, and a
    discovery run that peeked past its own ``as_of_ms`` would contaminate exactly the
    freeze it exists to create. The *rules* below are grade's; only the window is ours.
    """
    return grade.WalletEvidence(
        address=address,
        chain=chain,
        tags=grade._tags_from(tags),
        observed_buys=features.buys,
        observed_sells=features.sells,
    )


@dataclass(frozen=True, slots=True)
class ShapeVerdict:
    """The structural pass over one address: what it was rejected for, and what could not
    be judged. The two lists are disjoint by construction — a rule either fired on
    evidence or it could not be applied — which is what stops an unestablished fact from
    being reported as a finding.
    """

    rejections: list[Rejection] = field(default_factory=list)
    unassessed: list[Unassessed] = field(default_factory=list)


def assess_shape(
    address: str,
    chain: Chain,
    features: CandidateFeatures,
    *,
    coverage: TokenCoverage | None = None,
    tags: Sequence[str] = (),
    created_tokens: int = 0,
    duplicate_of: str | None = None,
) -> ShapeVerdict:
    """Every reason this address is not a candidate, and every one we could not check.

    The sell-only and buy-starved rules are :mod:`kaiba.intelligence.grade`'s own — reused
    rather than restated, so a change to the evidence changes both call sites at once.

    ``coverage`` decides whether the three token-set rules may speak at all. It defaults
    to :class:`TokenCoverage` with nothing in it, which reads as *unestablished*: a caller
    that does not say how the tokens were observed gets the honest answer rather than the
    convenient one, and the address lands in :attr:`ShapeVerdict.unassessed` instead of
    being rejected on a count nobody stood behind.
    """
    ev = _evidence_for(address, chain, features, tags)
    cov = coverage if coverage is not None else TokenCoverage()
    out: list[Rejection] = []
    gaps: list[Unassessed] = []

    quarantine = sorted(t.value for t in ev.tags if t in grade.HARD_QUARANTINE_TAGS)
    if quarantine:
        out.append(
            Rejection(
                address=address,
                reason=RejectReason.QUARANTINE_TAG,
                detail=f"hard quarantine tag: {', '.join(quarantine)}",
            )
        )

    sell_only = grade.sell_only_rejection(ev)
    if sell_only is not None:
        out.append(Rejection(address=address, reason=RejectReason.SELL_ONLY, detail=sell_only))
    else:
        share = grade.buy_share(ev)
        if share is not None and share < grade.BUY_STARVED_MAX_BUY_SHARE:
            out.append(
                Rejection(
                    address=address,
                    reason=RejectReason.BUY_STARVED,
                    detail=(
                        f"{features.buys} buys against {features.sells} sells "
                        f"({share:.2%} buy share, floor "
                        f"{float(grade.BUY_STARVED_MAX_BUY_SHARE):.0%}) — part of this "
                        "wallet's inventory arrived off-book"
                    ),
                )
            )

    rate = features.tx_failure_rate
    if rate is not None and rate >= TX_FAILURE_REJECT_RATE:
        out.append(
            Rejection(
                address=address,
                reason=RejectReason.HIGH_TX_FAILURE,
                detail=(
                    f"{features.tx_failures}/{features.tx_observed} submissions failed "
                    f"({rate:.0%}); reject at {TX_FAILURE_REJECT_RATE:.0%}, Jupiter's own "
                    f"sybil line is {JUPITER_SYBIL_FAILURE_RATE:.0%}. This is an execution "
                    "loop, not a decision we could copy"
                ),
            )
        )

    hold = features.median_hold_s
    if hold is not None and features.closed_trades >= CADENCE_MIN_CLOSED and hold < MIN_MEDIAN_HOLD_S:
        out.append(
            Rejection(
                address=address,
                reason=RejectReason.UNCOPYABLE_CADENCE,
                detail=(
                    f"median closed round trip is {hold}s over {features.closed_trades} "
                    f"episodes (floor {MIN_MEDIAN_HOLD_S}s). On a convex curve the copier "
                    "strictly overpays at zero latency and is in guaranteed loss by two "
                    "seconds of delay, so a round trip this short is uncopyable whether or "
                    "not it is skilled"
                ),
            )
        )

    # --- the three token-set rules. Each is an "at most" claim, and `distinct_tokens` is
    # a floor unless the wallet's own history was walked, so each either fires on evidence
    # or is recorded as unassessed. Never both, and never a rejection on the strength of a
    # floor: that is the difference between "it traded one token" and "we pulled one".
    if features.distinct_tokens < MIN_DISTINCT_TOKENS:
        detail = (
            f"{features.distinct_tokens} distinct token(s) observed; an edge seen on "
            "one token is indistinguishable from having been in the bundle"
        )
        if cov.universe_established:
            out.append(
                Rejection(address=address, reason=RejectReason.SINGLE_TOKEN, detail=detail)
            )
        else:
            gaps.append(
                Unassessed(
                    address=address,
                    reason=UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED,
                    would_have_been=RejectReason.SINGLE_TOKEN,
                    detail=f"{detail} — except that {cov.detail}",
                )
            )

    share_pnl = features.best_token_pnl_share
    if share_pnl is not None and features.distinct_tokens >= MIN_DISTINCT_TOKENS:
        if share_pnl >= SINGLE_TOKEN_MAX_PNL_SHARE:
            detail = (
                f"{share_pnl:.0%} of realised gains came from one token "
                f"(ceiling {SINGLE_TOKEN_MAX_PNL_SHARE:.0%}) — the wallet had a "
                "token, not an edge"
            )
            if cov.universe_established:
                out.append(
                    Rejection(
                        address=address, reason=RejectReason.SINGLE_TOKEN_PNL, detail=detail
                    )
                )
            else:
                gaps.append(
                    Unassessed(
                        address=address,
                        reason=UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED,
                        would_have_been=RejectReason.SINGLE_TOKEN_PNL,
                        detail=(
                            f"{detail} — except that the share is over the tokens we pulled: "
                            f"{cov.detail}"
                        ),
                    )
                )

    if created_tokens > 0.5 * max(1, features.distinct_tokens):
        detail = (
            f"created {created_tokens} of the {features.distinct_tokens} tokens it "
            "traded; it is marking its own homework"
        )
        if cov.universe_established:
            out.append(
                Rejection(
                    address=address, reason=RejectReason.CREATOR_SELF_DEALING, detail=detail
                )
            )
        else:
            gaps.append(
                Unassessed(
                    address=address,
                    reason=UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED,
                    would_have_been=RejectReason.CREATOR_SELF_DEALING,
                    detail=(
                        f"{detail} — except that the denominator is our sampling: {cov.detail}"
                    ),
                )
            )

    if duplicate_of is not None:
        out.append(
            Rejection(
                address=address,
                reason=RejectReason.SAME_ENTITY,
                detail=f"same operator as {duplicate_of}; five addresses from one hand are one opinion",
            )
        )
    return ShapeVerdict(rejections=out, unassessed=gaps)


def rejections_for(
    address: str,
    chain: Chain,
    features: CandidateFeatures,
    *,
    coverage: TokenCoverage | None = None,
    tags: Sequence[str] = (),
    created_tokens: int = 0,
    duplicate_of: str | None = None,
) -> list[Rejection]:
    """The rejections from :func:`assess_shape`. What it *could not* judge is dropped.

    Kept because a caller that only wants the findings should not have to unpack a
    verdict. Anything that acts on the result — the run below, an operator report — wants
    :func:`assess_shape` instead, because the unassessed list is the half of the answer
    this one throws away.
    """
    return assess_shape(
        address,
        chain,
        features,
        coverage=coverage,
        tags=tags,
        created_tokens=created_tokens,
        duplicate_of=duplicate_of,
    ).rejections


def _entity_representative(candidates: Mapping[str, CandidateFeatures], members: Sequence[str]) -> str:
    """The address that speaks for an entity: most closed trades, then most tokens.

    Deterministic on ties by address so a rerun produces the same cohort; a discovery run
    whose membership moves between runs cannot be frozen and tracked.
    """
    def key(address: str) -> tuple[int, int, int, str]:
        f = candidates[address]
        return (-f.closed_trades, -f.distinct_tokens, -f.swaps, address)

    return sorted(members, key=key)[0]


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


def population_null(features: Iterable[CandidateFeatures]) -> tuple[float | None, int, str]:
    """``(base win rate, pooled closed trades, basis)`` for the screened population.

    The null is pooled from the same tape the candidates come from, so a candidate is
    tested against "better than the wallets we watched" rather than against an invented
    coin flip. ``None`` below :data:`NULL_MIN_POOLED_TRADES`, and nothing downstream
    substitutes a number for it.

    This null is still generous. Research 13 §A2: with the 1.25% curve fee each way plus
    priority fees, tips and impact, the true expectation of a random memecoin round trip is
    below zero, so a pooled *observed* win rate already embeds whatever survivorship our
    sampling has. Beating it is necessary, not sufficient.
    """
    wins = sum(f.wins for f in features)
    trades = sum(f.closed_trades for f in features)
    if trades < NULL_MIN_POOLED_TRADES or trades <= 0:
        return None, trades, EvidenceBasis.UNAVAILABLE.value
    return wins / trades, trades, EvidenceBasis.DERIVED.value


def discover(
    conn: sqlite3.Connection | None = None,
    *,
    chain: Chain | str = Chain.SOL,
    as_of_ms: int | None = None,
    alpha: float = DEFAULT_ALPHA,
    min_edge: float = MIN_INTERESTING_EDGE,
) -> DiscoveryReport:
    """Screen every wallet in our own flow, reject the known-bad shapes, deflate the rest.

    The order matters and is the point: reject on structure *before* anything is measured,
    because a large realised profit will otherwise absorb a subtraction, and count entities
    before counting wallets, because the screen size N is the denominator of every claim
    the report makes.
    """
    ch = chain if isinstance(chain, Chain) else Chain(str(chain))
    c = conn or ensure_db()
    t = as_of_ms if as_of_ms is not None else now_ms()
    flow = _load_flow(c, ch, t)

    screened = sorted(set(flow.swaps) | set(flow.first_buyers))
    notes: list[str] = []

    all_features: dict[str, CandidateFeatures] = {}
    all_bases: dict[str, dict[str, str]] = {}
    all_unknowns: dict[str, list[str]] = {}
    all_coverage: dict[str, TokenCoverage] = {}
    for address in screened:
        features, bases, unknowns, coverage = _features(address, flow)
        all_features[address] = features
        all_bases[address] = bases
        all_unknowns[address] = unknowns
        all_coverage[address] = coverage

    keys = entity_keys(c, ch, screened, flow)
    by_entity: dict[str, list[str]] = defaultdict(list)
    for address in screened:
        by_entity[keys[address]].append(address)
    duplicate_of: dict[str, str] = {}
    for members in by_entity.values():
        if len(members) < 2:
            continue
        chosen = _entity_representative(all_features, members)
        for address in members:
            if address != chosen:
                duplicate_of[address] = chosen

    rejected: list[Rejection] = []
    unassessed: list[Unassessed] = []
    survivors: list[str] = []
    unassessable = 0
    for address in screened:
        features = all_features[address]
        verdict = assess_shape(
            address,
            ch,
            features,
            coverage=all_coverage[address],
            tags=flow.tags.get(address, ()),
            created_tokens=flow.created_tokens.get(address, 0),
            duplicate_of=duplicate_of.get(address),
        )
        if verdict.rejections:
            # A finding outranks a gap: an address rejected on evidence is rejected, and
            # noting that some other rule could not be applied to it would be noise.
            rejected.extend(verdict.rejections)
            continue
        if verdict.unassessed:
            # Not rejected and not carried forward. This is the third state: the numbers
            # that would have condemned it rest on tokens we chose, so we have nothing to
            # say about it — which is not the same as having found it clean.
            unassessed.extend(verdict.unassessed)
            continue
        if features.swaps < MIN_SWAPS_TO_ASSESS:
            unassessable += 1
            unassessed.append(
                Unassessed(
                    address=address,
                    reason=UnassessedReason.TOO_THIN,
                    detail=(
                        f"{features.swaps} observed swap(s), below the {MIN_SWAPS_TO_ASSESS} "
                        "needed to have seen a wallet rather than glimpsed it"
                    ),
                )
            )
            continue
        survivors.append(address)

    null_rate, pooled, null_basis = population_null(all_features.values())
    n_screened = len(screened)
    n_entities = len({keys[a] for a in screened})
    gate = (
        min_closed_trades(min(null_rate + min_edge, 0.999), null_rate, screened=n_screened, alpha=alpha)
        if null_rate is not None
        else None
    )

    candidates: list[Candidate] = []
    for address in survivors:
        features = all_features[address]
        blockers: list[str] = []
        p = (
            binomial_tail_p(features.wins, features.closed_trades, null_rate)
            if null_rate is not None and features.closed_trades > 0
            else None
        )
        if null_rate is None:
            blockers.append(
                f"no population base rate: {pooled} pooled closed trades across the screen, "
                f"below the {NULL_MIN_POOLED_TRADES} needed to state one. Nothing here can be "
                "tested against anything"
            )
        status = CandidateStatus.UNVALIDATED
        if features.closed_trades <= 0:
            status = CandidateStatus.UNASSESSABLE
            blockers.append("no closed round trip: nothing realised, so there is no trade record")
        elif gate is not None and features.closed_trades >= gate:
            status = CandidateStatus.SAMPLE_GATE_CLEARED
        elif gate is not None:
            blockers.append(
                f"{features.closed_trades} closed trades against the {gate} needed to "
                f"distinguish {(null_rate or 0) + min_edge:.0%} from {(null_rate or 0):.0%} "
                f"at alpha={bonferroni_alpha(alpha, n_screened):.2e} "
                f"(Bonferroni over {n_screened} screened wallets)"
            )
        if features.tx_failure_rate is None:
            blockers.append(
                f"transaction failure rate unmeasured ({features.tx_observed} submissions "
                f"observed, needs {TX_FAILURE_MIN_OBSERVED}); we cannot tell this wallet from "
                "a bot, and unknown is not clean"
            )
        candidates.append(
            Candidate(
                address=address,
                chain=ch.value,
                entity_key=keys[address],
                status=status,
                features=features,
                bases=all_bases[address],
                unknowns=all_unknowns[address],
                blockers=blockers,
                p_under_null=p,
                expected_peers=expected_peers(n_screened, p),
            )
        )

    candidates.sort(key=lambda x: (x.p_under_null if x.p_under_null is not None else 2.0, x.address))
    cleared = [x for x in candidates if x.status is CandidateStatus.SAMPLE_GATE_CLEARED]
    best = next((x.p_under_null for x in candidates if x.p_under_null is not None), None)

    chance = ChanceReport(
        screened_addresses=n_screened,
        screened_entities=n_entities,
        alpha=alpha,
        bonferroni_alpha=bonferroni_alpha(alpha, n_screened),
        bonferroni_z=metrics.normal_inv_cdf(1.0 - bonferroni_alpha(alpha, n_screened)),
        expected_max_z=validation.expected_max_z(n_screened),
        expected_false_positives=n_screened * alpha,
        null_win_rate=null_rate,
        null_basis=null_basis,
        null_pooled_trades=pooled,
        min_interesting_rate=None if null_rate is None else min(null_rate + min_edge, 0.999),
        min_closed_trades=gate,
        gate_cleared=len(cleared),
        best_p_under_null=best,
        expected_peers_at_best=expected_peers(n_screened, best),
        table=chance_table(n_screened, null_rate) if null_rate is not None else [],
        notes=_chance_notes(n_screened, n_entities, null_rate, gate),
    )

    if n_entities < n_screened:
        notes.append(
            f"{n_screened - n_entities} address(es) collapsed into another operator, so the "
            f"honest N is somewhere between {n_entities} and {n_screened}; the deflation uses "
            "the larger, which is the conservative direction"
        )
    notes.append(
        "the screened population is a convenience sample: helius:backfill walked wallets we "
        "chose and pumpfun:trades walked tokens we chose. A base rate computed from it "
        "describes our sampling, not the venue"
    )
    notes.append(
        "realised-only accounting: a wallet still holding a dead bag has no closed episode "
        "and reads as flat rather than down, which is the same artefact that makes 73% of "
        "pump.fun wallets look profitable"
    )
    notes.extend(_coverage_notes(flow, screened, all_coverage, unassessed))

    run_id = "disc_" + digest({"chain": ch.value, "t": t, "model": MODEL_ID, "n": n_screened})[:20]
    return DiscoveryReport(
        run_id=run_id,
        chain=ch.value,
        as_of_ms=t,
        screened=screened,
        unassessable=unassessable,
        unassessed=unassessed,
        rejected=rejected,
        candidates=candidates,
        chance=chance,
        notes=notes,
    )


def _coverage_notes(
    flow: _Flow,
    screened: Sequence[str],
    coverage: Mapping[str, TokenCoverage],
    unassessed: Sequence[Unassessed],
) -> list[str]:
    """What the run could and could not see, in the run's own output.

    Written as notes rather than a log line because the reach of our sampling is the first
    thing a reader of this report needs and the last thing they would think to ask for.
    """
    walked = sum(1 for a in screened if coverage[a].universe_established)
    gapped = sum(
        1 for u in unassessed if u.reason is UnassessedReason.TOKEN_UNIVERSE_UNESTABLISHED
    )
    notes = [
        f"{walked} of {len(screened)} screened addresses had their own history walked; for "
        f"the other {len(screened) - walked} the token count is a floor on the tokens we "
        "pulled, so the single-token, single-token-PnL and creator-self-dealing rules could "
        f"not be applied and {gapped} address(es) are unassessed rather than rejected",
        f"{len(flow.complete_tape)} mint(s) hold a trade tape proved complete back to launch "
        "(kaiba.ingest.tape.complete_tokens, the shared gate); pump.fun's trades endpoint "
        "serves a hot window, so tape coverage is a going-forward property and the tokens "
        "already cold can never be completed",
    ]
    unclassified = sorted(
        {s for a in screened for s in coverage[a].sources if s and s not in WALLET_SCOPED_SOURCES}
    )
    if unclassified:
        notes.append(
            f"swaps.source values read as token-scoped: {', '.join(unclassified)}. A source "
            "that walks a wallet's own history belongs in discover.WALLET_SCOPED_SOURCES; "
            "until it is listed there it can only make this run claim less"
        )
    return notes


def _chance_notes(screened: int, entities: int, null_rate: float | None, gate: int | None) -> list[str]:
    notes = [
        f"N is {screened}, the number of wallets screened, not the number shortlisted; that "
        "is the correction that applies when a wallet is picked off a board",
        f"the best of {screened} wallets with no skill at all is expected at "
        f"z={validation.expected_max_z(screened):.2f} under the null (Bailey & Lopez de "
        "Prado's expected maximum), so a high z here is a starting point for suspicion",
    ]
    if entities < screened:
        notes.append(
            f"{entities} distinct operators behind {screened} addresses; correlated tests are "
            "not independent tests, so the Bonferroni correction is conservative in the "
            "denominator and optimistic about independence"
        )
    if null_rate is None:
        notes.append("no base rate could be measured, so no wallet was gated and none was tested")
    elif gate is None:
        notes.append("no gate could be computed: the target rate was not above the base rate")
    return notes


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------


def store_report(report: DiscoveryReport, conn: sqlite3.Connection | None = None) -> str:
    """Write the run, its candidates and its rejects. Returns the ``run_id``."""
    c = conn or get_conn()
    with tx(c):
        upsert(
            c,
            "discovery_runs",
            {
                "run_id": report.run_id,
                "chain": report.chain,
                "run_ms": report.run_ms,
                "as_of_ms": report.as_of_ms,
                "model_version": report.model_version,
                "screened_addresses": len(report.screened),
                "screened_entities": report.chance.screened_entities,
                "rejected_addresses": len({r.address for r in report.rejected}),
                "assessable": len(report.candidates),
                "candidates": len(report.candidates),
                "gate_cleared": report.chance.gate_cleared,
                "null_win_rate": report.chance.null_win_rate,
                "null_basis": report.chance.null_basis,
                "null_pooled_trades": report.chance.null_pooled_trades,
                "min_closed_trades": report.chance.min_closed_trades,
                "bonferroni_alpha": report.chance.bonferroni_alpha,
                "expected_max_z": report.chance.expected_max_z,
                "expected_false_positives": report.chance.expected_false_positives,
                "cohort_id": report.cohort_id,
                "summary": report.headline,
                "payload_json": jdump(
                    {
                        "chance": report.chance.model_dump(mode="json"),
                        "notes": report.notes,
                        "reject_counts": report.reject_counts,
                        "unassessable": report.unassessable,
                        # Deliberately not written to `discovery_rejects`: a row in that
                        # table is a finding, and these are the opposite of one. They live
                        # in the run payload so the count is recoverable without a reader
                        # ever mistaking them for rejections.
                        "unassessed_counts": report.unassessed_counts,
                        "withheld_rejects": report.withheld_rejects,
                        "unassessed": [
                            u.model_dump(mode="json")
                            for u in report.unassessed[:UNASSESSED_PAYLOAD_LIMIT]
                        ],
                        "unassessed_truncated": max(
                            0, len(report.unassessed) - UNASSESSED_PAYLOAD_LIMIT
                        ),
                    }
                ),
            },
            ["run_id"],
        )
        for cand in report.candidates:
            upsert(
                c,
                "discovery_candidates",
                {
                    "run_id": report.run_id,
                    "chain": cand.chain,
                    "address": cand.address,
                    "entity_key": cand.entity_key,
                    "status": cand.status.value,
                    "closed_trades": cand.features.closed_trades,
                    "wins": cand.features.wins,
                    "win_rate": cand.features.win_rate,
                    "distinct_tokens": cand.features.distinct_tokens,
                    "p_under_null": cand.p_under_null,
                    "expected_peers": cand.expected_peers,
                    "features_json": jdump(cand.features.model_dump(mode="json")),
                    "bases_json": jdump(cand.bases),
                    "unknowns_json": jdump(cand.unknowns),
                    "blockers_json": jdump(cand.blockers),
                },
                ["run_id", "chain", "address"],
            )
        for rej in report.rejected:
            upsert(
                c,
                "discovery_rejects",
                {
                    "run_id": report.run_id,
                    "chain": report.chain,
                    "address": rej.address,
                    "reason": rej.reason.value,
                    "detail": rej.detail,
                },
                ["run_id", "chain", "address", "reason"],
            )
        emit(
            EventKind.SYSTEM,
            {
                "discovery_run": report.run_id,
                "screened": len(report.screened),
                "entities": report.chance.screened_entities,
                "candidates": len(report.candidates),
                "gate_cleared": report.chance.gate_cleared,
                "expected_false_positives": report.chance.expected_false_positives,
                "summary": report.headline,
            },
            chain=Chain(report.chain),
            conn=c,
        )
    return report.run_id


def propose_research_cohort(
    report: DiscoveryReport, conn: sqlite3.Connection | None = None
) -> tuple[int, list[str]]:
    """Mark candidates ``research`` in ``wallets``. Returns ``(written, skipped)``.

    The only cohort discovery may write. ``trusted_copy`` is a claim that measured forward
    performance beat a matched control, which nothing in this module measures, and
    ``blacklist`` is somebody else's finding. A row already carrying either is left exactly
    as it is and its address is returned in the skipped list.
    """
    c = conn or get_conn()
    written = 0
    skipped: list[str] = []
    stamp = report.run_ms
    with tx(c):
        for cand in report.candidates:
            existing = fetch_one(
                c,
                "SELECT cohort FROM wallets WHERE chain = ? AND address = ?",
                (cand.chain, cand.address),
            )
            current = str(existing["cohort"]) if existing and existing["cohort"] else None
            if current in PROTECTED_COHORTS:
                skipped.append(cand.address)
                continue
            if existing is None:
                c.execute(
                    "INSERT INTO wallets (chain, address, source, tags_json, first_seen_ms, "
                    "last_seen_ms, cohort, meta_json) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        cand.chain,
                        cand.address,
                        DISCOVERY_SOURCE,
                        "[]",
                        cand.features.first_seen_ms or stamp,
                        cand.features.last_seen_ms or stamp,
                        PROPOSED_COHORT,
                        jdump({"discovery_run": report.run_id, "status": cand.status.value}),
                    ),
                )
            else:
                c.execute(
                    "UPDATE wallets SET cohort = ? WHERE chain = ? AND address = ? "
                    "AND (cohort IS NULL OR cohort = ?)",
                    (PROPOSED_COHORT, cand.chain, cand.address, PROPOSED_COHORT),
                )
            written += 1
    return written, skipped


# --------------------------------------------------------------------------------------
# forward tracking — the only thing that could ever validate any of this
# --------------------------------------------------------------------------------------

#: What a discovery cohort is matched to its control on. Same covariates as
#: :data:`kaiba.learning.validation.MATCH_COVARIATES`, read from the discovery features
#: rather than from ``wallet_scores``, because a discovered wallet has no grade.
DISCOVERY_COVARIATES: tuple[str, ...] = (
    "log_closed_trades",
    "log_distinct_tokens",
    "log_swaps",
    "days_since_last_activity",
)

#: Confounders we know we did not match on. Declared with the cohort, because a matched
#: study whose unmatched confounders are undeclared is an unmatched study. The first four
#: are validation.py's own; the last two are specific to discovering from our own tape.
DISCOVERY_UNMATCHED: tuple[str, ...] = (
    *validation.UNMATCHED_CONFOUNDERS,
    "observation intensity — the control arm was screened by the same convenience sample, "
    "so both arms are wallets we happened to collect; a wallet we watched harder has more "
    "closed episodes for reasons that have nothing to do with it",
    "transaction-failure coverage — swap_meta only covers the Helius pass, so most wallets "
    "in both arms have an unmeasured failure rate and a bot could sit in either",
)


def _cov(features: CandidateFeatures, as_of_ms: int) -> dict[str, float]:
    last = features.last_seen_ms
    return {
        "log_closed_trades": math.log10(1 + features.closed_trades),
        "log_distinct_tokens": math.log10(1 + features.distinct_tokens),
        "log_swaps": math.log10(1 + features.swaps),
        "days_since_last_activity": (
            (as_of_ms - last) / validation.DAY_MS if last is not None else float("nan")
        ),
    }


def freeze_cohort(
    report: DiscoveryReport,
    conn: sqlite3.Connection | None = None,
    *,
    caliper_sd: float = validation.CONTROL_CALIPER_SD,
) -> str:
    """Freeze the discovered cohort and a matched control at ``report.as_of_ms``.

    Membership is written into ``wallet_cohorts`` / ``wallet_cohort_freezes`` — migration
    016's tables — with ``arm`` set to ``graded`` and ``control``, precisely so
    :func:`kaiba.learning.validation.control_arm` measures this cohort with the same code
    that measures a graded one. The forward test, the permutation statistic, the minimum
    detectable effect and the attrition check all live there and are not reimplemented
    here.

    What *is* here is the pairing, and only because
    :func:`validation.freeze_cohort` selects its treated arm by wallet grade and a
    discovered wallet does not have one. The design is that function's: 1:1 nearest
    neighbour on standardised covariates inside a ``caliper_sd`` calliper, with a control
    rejected when it shares an operator with its pair.

    The control arm is the set of screened addresses that discovery did **not** select —
    including the ones it rejected on structure. So the contrast measures the selection,
    which is the thing we want to know about, and not some underlying trait.
    """
    c = conn or get_conn()
    t = report.as_of_ms
    chain = report.chain
    treated = {cand.address: cand.features for cand in report.candidates}
    flow_features = _control_features(c, Chain(chain), t, exclude=set(treated))
    notes = list(report.notes)

    cohort_id = "discovery_" + digest({"run": report.run_id, "t": t})[:20]
    report.cohort_id = cohort_id
    if not treated or not flow_features:
        _write_freeze(c, cohort_id, t, chain, 0, 0, notes + ["cohort empty at freeze time"], [], [])
        return cohort_id

    g_cov = {a: _cov(f, t) for a, f in treated.items()}
    p_cov = {a: _cov(f, t) for a, f in flow_features.items()}
    spreads = {
        k: validation._sd([*(v[k] for v in g_cov.values()), *(v[k] for v in p_cov.values())])
        for k in DISCOVERY_COVARIATES
    }

    def distance(a: Mapping[str, float], b: Mapping[str, float]) -> float | None:
        total = 0.0
        for key in DISCOVERY_COVARIATES:
            spread = spreads[key]
            if spread <= 0:
                continue
            av, bv = a[key], b[key]
            if math.isnan(av) or math.isnan(bv):
                return None
            d = abs(av - bv) / spread
            if d > caliper_sd:
                return None
            total += d * d
        return math.sqrt(total)

    used: set[str] = set()
    graded_rows: list[tuple[str, int, dict[str, float]]] = []
    control_rows: list[tuple[str, int, dict[str, float]]] = []
    unmatched = 0
    entity_of = {cand.address: cand.entity_key for cand in report.candidates}
    control_entity = _entity_keys_for_controls(c, Chain(chain), sorted(flow_features))
    for pair_id, address in enumerate(sorted(treated)):
        best: tuple[float, str] | None = None
        for other in sorted(flow_features):
            if other in used or other == address:
                continue
            if entity_of.get(address) and control_entity.get(other) == entity_of.get(address):
                continue
            d = distance(g_cov[address], p_cov[other])
            if d is None:
                continue
            if best is None or d < best[0]:
                best = (d, other)
        if best is None:
            unmatched += 1
            continue
        used.add(best[1])
        graded_rows.append((address, pair_id, g_cov[address]))
        control_rows.append((best[1], pair_id, p_cov[best[1]]))

    if unmatched:
        notes.append(
            f"{unmatched} candidate(s) had no control inside the {caliper_sd}-SD calliper and "
            "were dropped; the frozen arm is therefore the subset of candidates that look like "
            "wallets we did not select"
        )
    notes.append(
        "the control arm is every screened address discovery did not select, including the "
        "structurally rejected ones, so the forward test measures the selection rule itself"
    )
    if len(graded_rows) < validation.CONTROL_MIN_ARM:
        notes.append(
            f"FROZEN COHORT IS TOO SMALL TO EVER CONCLUDE: {len(graded_rows)} matched pair(s) "
            f"against the {validation.CONTROL_MIN_ARM} per arm control_arm() needs before it "
            "will return anything but underpowered. The freeze is still worth writing — it is "
            "a dated starting line and it costs nothing — but the forward test will not reach "
            "a verdict until the screen itself is an order of magnitude larger. Widening the "
            f"{caliper_sd}-SD calliper would produce pairs, not evidence: the arms would then "
            "differ on the covariates the match exists to hold fixed"
        )
    _write_freeze(c, cohort_id, t, chain, len(graded_rows), len(control_rows), notes, graded_rows, control_rows)
    return cohort_id


def _control_features(
    conn: sqlite3.Connection, chain: Chain, as_of_ms: int, *, exclude: set[str]
) -> dict[str, CandidateFeatures]:
    flow = _load_flow(conn, chain, as_of_ms)
    out: dict[str, CandidateFeatures] = {}
    for address in sorted(set(flow.swaps) | set(flow.first_buyers)):
        if address in exclude:
            continue
        # Four values, not three. `_features` grew a `TokenCoverage` return when the
        # coverage gate landed and this call site was missed, so `freeze_cohort` raised
        # `ValueError: too many values to unpack` on any database where at least one
        # screened address is *not* a candidate -- which is every real one. The tests
        # passed because their fixtures make every screened address a candidate, so this
        # loop body never ran.
        features, _bases, _unknowns, _coverage = _features(address, flow)
        out[address] = features
    return out


def _entity_keys_for_controls(
    conn: sqlite3.Connection, chain: Chain, addresses: Sequence[str]
) -> dict[str, str]:
    resolved = entity_ids_for(chain, addresses, conn)
    return {a: resolved.get(a, f"{SOLO_PREFIX}{a}") for a in addresses}


def _write_freeze(
    conn: sqlite3.Connection,
    cohort_id: str,
    frozen_ms: int,
    chain: str,
    graded_n: int,
    control_n: int,
    notes: Sequence[str],
    graded: Sequence[tuple[str, int, Mapping[str, float]]],
    control: Sequence[tuple[str, int, Mapping[str, float]]],
) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO wallet_cohort_freezes (cohort_id, frozen_ms, chain, graded_n, control_n, "
            "matched_on_json, unmatched_json, notes_json) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(cohort_id) DO UPDATE SET graded_n=excluded.graded_n, "
            "control_n=excluded.control_n, notes_json=excluded.notes_json",
            (
                cohort_id,
                frozen_ms,
                chain,
                graded_n,
                control_n,
                jdump(list(DISCOVERY_COVARIATES)),
                jdump(list(DISCOVERY_UNMATCHED)),
                jdump(list(notes)),
            ),
        )
        for arm, rows in (("graded", graded), ("control", control)):
            for address, pair_id, cov in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO wallet_cohorts (cohort_id, arm, chain, address, "
                    "pair_id, frozen_ms, grade, covariates_json) VALUES (?,?,?,?,?,?,?,?)",
                    (cohort_id, arm, chain, address, pair_id, frozen_ms, "", jdump(dict(cov))),
                )


def forward_report(
    cohort_id: str,
    conn: sqlite3.Connection | None = None,
    *,
    end_ms: int | None = None,
    horizon_ms: int = validation.GATE_TTL_MS,
) -> validation.ControlArmReport:
    """Measure a frozen discovery cohort forward against its control.

    A one-line delegation to :func:`kaiba.learning.validation.control_arm` on purpose.
    This is the test the literature has never run — no published autocorrelation study, no
    forward-tracked cohort with a control, no vendor out-of-sample validation — and it
    would be worth nothing if discovery graded its own homework with its own arithmetic.
    Expect ``underpowered`` for a long time: the control arm needs
    :data:`validation.CONTROL_MIN_ARM` wallets with forward data in *each* arm before it
    will return anything else.
    """
    return validation.control_arm(
        conn or get_conn(), cohort_id=cohort_id, end_ms=end_ms, horizon_ms=horizon_ms
    )


# --------------------------------------------------------------------------------------
# vendor cohort feeds: the wallets GMGN labels, gathered as facts about what we recorded
# --------------------------------------------------------------------------------------
#
# Everything above this line finds wallets from *our* tape and refuses to trust a vendor's
# opinion of them. This section is the one place a vendor's opinion is read, and it is
# read as exactly that: "GMGN listed this trade on its smart-money feed and attached these
# labels" is a fact we recorded about GMGN, not a fact about the wallet. Nothing here
# scores, ranks or promotes. The tracker decides whether to *observe* a labelled wallet,
# and the lanes that consume the label (``sm-trenches``) run in shadow with their evidence
# grade recorded on every signal.
#
# Why the labels have to be gathered from ``events`` and not ``swaps``: the ``swaps`` table
# has no tags column. ``kaiba.ingest.gmgn_feeds.write_swap`` writes the trade to ``swaps``
# and the whole row — tags included — to a ``wallet.trade`` event. MEASURED 2026-09-21 on
# the live database: 10,334 bsc feed events over 369 wallets, every one carrying tags,
# while ``wallets`` held zero rows on every chain. The labels were persisted; nothing had
# ever read them back.

#: ``swaps.source`` values written by :mod:`kaiba.ingest.gmgn_feeds` for the two wallet
#: feeds. A row from either says "GMGN listed this trade on that feed" and nothing more.
COHORT_FEED_SOURCES: frozenset[str] = frozenset({"gmgn:smartmoney", "gmgn:kol"})

#: GMGN's own labels for its smart-money cohort, as they arrive in ``maker_info.tags``.
#: CITED from the live tape (2026-09-21): ``smart_degen`` on 7,315 of 10,334 bsc feed
#: rows, ``launchpad_smart`` on 698, ``app_smart_money`` on 647. None of these strings is
#: a :class:`~kaiba.core.schemas.WalletTag`, so a verbatim copy into ``wallets.tags_json``
#: matches nothing the lanes read; the mapping onto ``WalletTag.SMART_MONEY`` is made by
#: the tracker, on admission only, and recorded on the row as a derived tag.
GMGN_SMART_COHORT_TAGS: frozenset[str] = frozenset({"smart_degen", "app_smart_money", "launchpad_smart"})

#: GMGN's KOL label. It *is* a ``WalletTag`` value, so it passes through verbatim.
GMGN_KOL_COHORT_TAGS: frozenset[str] = frozenset({"kol"})

#: Cohort labels the tracker writes into ``tracker_watchlist.source`` after the prefix.
COHORT_LABEL_SMART = "smart_degen"
COHORT_LABEL_KOL = "kol"
COHORT_LABEL_FEED_ONLY = "feed_only"

#: Events are read in address chunks of this size to stay under SQLite's variable limit.
_EVENT_CHUNK = 400


class CohortWallet(BaseModel):
    """One wallet as the GMGN feeds showed it to us. Facts about the feed, not the wallet.

    ``distinct_tokens`` is a floor: the feed is a sample of the wallet's trades, not its
    history, so it carries the same token-scoped basis as any other token-route row (see
    :class:`TokenCoverage`). ``gmgn_tags`` is the union over every feed row's labels; when
    no event carried any it is empty with an ``UNAVAILABLE`` basis, which is *not* the same
    as "GMGN says this wallet has no labels".
    """

    model_config = ConfigDict(frozen=True)

    chain: Chain
    address: str
    sources: list[str]
    feed_rows: int
    buys: int
    sells: int
    distinct_tokens: int
    tokens_basis: EvidenceBasis = EvidenceBasis.PROVIDER_REPORTED
    first_seen_ms: int
    last_seen_ms: int
    gmgn_tags: list[str] = Field(default_factory=list)
    tags_basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    wallet_name: str | None = None

    @property
    def smart_cohort(self) -> bool:
        """GMGN attached one of its smart-money labels. Nothing else makes this true —
        not the feed the row came from, and not an absence of labels."""
        return bool(set(self.gmgn_tags) & GMGN_SMART_COHORT_TAGS)

    @property
    def kol_cohort(self) -> bool:
        return bool(set(self.gmgn_tags) & GMGN_KOL_COHORT_TAGS)

    @property
    def cohort_label(self) -> str:
        if self.smart_cohort:
            return COHORT_LABEL_SMART
        if self.kol_cohort:
            return COHORT_LABEL_KOL
        return COHORT_LABEL_FEED_ONLY


def _feed_tags(
    conn: sqlite3.Connection, chain: Chain, addresses: Sequence[str]
) -> tuple[dict[str, list[str]], dict[str, str | None], set[str]]:
    """``(address -> tag union, address -> first wallet name, addresses with any feed event)``.

    Only events that carry a ``feed`` field are feed rows; the tracker's own detections
    are ``wallet.trade`` events too and carry no vendor labels.
    """
    tags: dict[str, list[str]] = {}
    names: dict[str, str | None] = {}
    seen: set[str] = set()
    wanted = set(addresses)
    ordered = sorted(wanted)
    for i in range(0, len(ordered), _EVENT_CHUNK):
        batch = ordered[i : i + _EVENT_CHUNK]
        rows = fetch_all(
            conn,
            # ``+kind`` keeps the planner off idx_events_kind, which read every
            # wallet.trade event (~2M) per chunk; idx_events_subj seeks the subjects.
            "SELECT subject, payload FROM events WHERE +kind='wallet.trade' AND chain=? "
            f"AND subject IN ({','.join('?' for _ in batch)})",
            [chain.value, *batch],
        )
        for row in rows:
            payload = jload(str(row["payload"]), {})
            if not isinstance(payload, dict) or not payload.get("feed"):
                continue
            addr = safe_normalize(str(payload.get("wallet") or row["subject"] or ""), chain)
            if addr not in wanted:
                continue
            seen.add(addr)
            bucket = tags.setdefault(addr, [])
            for tag in payload.get("tags") or []:
                label = str(tag)
                if label and label not in bucket:
                    bucket.append(label)
            if names.get(addr) is None and payload.get("wallet_name"):
                names[addr] = str(payload["wallet_name"])
    return tags, names, seen


def cohort_wallets(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    sources: Iterable[str] = COHORT_FEED_SOURCES,
    since_ms: int | None = None,
    limit: int | None = None,
) -> list[CohortWallet]:
    """Every wallet the GMGN wallet feeds listed on ``chain``, most feed rows first.

    Reads ``swaps`` for the trades and ``events`` for the labels (see the section note for
    why they live apart). Pure: nothing is written. ``since_ms`` bounds the tape read;
    ``limit`` bounds the result after ranking, so a bounded call still ranks over the
    whole feed rather than over whichever rows happened to come first.
    """
    c = conn or get_conn()
    wanted = sorted({str(s) for s in sources if s})
    if not wanted:
        return []
    sql = (
        "SELECT wallet, GROUP_CONCAT(DISTINCT source) AS sources, COUNT(*) AS n, "
        " SUM(side='buy') AS buys, SUM(side='sell') AS sells, COUNT(DISTINCT token) AS toks, "
        " MIN(ts_ms) AS lo, MAX(ts_ms) AS hi FROM swaps WHERE chain=? "
        f"AND source IN ({','.join('?' for _ in wanted)})"
    )
    params: list[Any] = [chain.value, *wanted]
    if since_ms is not None:
        sql += " AND ts_ms >= ?"
        params.append(int(since_ms))
    sql += " GROUP BY wallet"

    # Group by the normalised address in Python: a feed that mixes address cases would
    # otherwise show one wallet twice, and an EVM address is case-insensitive.
    merged: dict[str, dict[str, Any]] = {}
    for row in fetch_all(c, sql, params):
        addr = safe_normalize(str(row["wallet"]), chain)
        if not addr:
            continue
        acc = merged.setdefault(
            addr, {"sources": set(), "n": 0, "buys": 0, "sells": 0, "toks": 0, "lo": None, "hi": None}
        )
        acc["sources"].update(s for s in str(row["sources"] or "").split(",") if s)
        acc["n"] += int(row["n"] or 0)
        acc["buys"] += int(row["buys"] or 0)
        acc["sells"] += int(row["sells"] or 0)
        # Distinct tokens across two case variants cannot be summed exactly; the max of the
        # two is still a floor, which is all this number ever claims to be.
        acc["toks"] = max(acc["toks"], int(row["toks"] or 0))
        lo, hi = row["lo"], row["hi"]
        acc["lo"] = int(lo) if acc["lo"] is None else min(acc["lo"], int(lo))
        acc["hi"] = int(hi) if acc["hi"] is None else max(acc["hi"], int(hi))
    if not merged:
        return []

    tags, names, seen = _feed_tags(c, chain, list(merged))
    out: list[CohortWallet] = []
    for addr, acc in merged.items():
        out.append(
            CohortWallet(
                chain=chain,
                address=addr,
                sources=sorted(acc["sources"]),
                feed_rows=acc["n"],
                buys=acc["buys"],
                sells=acc["sells"],
                distinct_tokens=acc["toks"],
                first_seen_ms=int(acc["lo"]),
                last_seen_ms=int(acc["hi"]),
                gmgn_tags=list(tags.get(addr, [])),
                tags_basis=EvidenceBasis.PROVIDER_REPORTED if addr in seen else EvidenceBasis.UNAVAILABLE,
                wallet_name=names.get(addr),
            )
        )
    out.sort(key=lambda w: (-w.feed_rows, w.address))
    if limit is not None:
        out = out[: max(0, int(limit))]
    return out


def cohort_summary(wallets: Sequence[CohortWallet]) -> dict[str, Any]:
    """Counts a caller can print without re-deriving them."""
    by_source: dict[str, int] = {}
    for w in wallets:
        for s in w.sources:
            by_source[s] = by_source.get(s, 0) + 1
    return {
        "wallets": len(wallets),
        "by_source": dict(sorted(by_source.items())),
        "smart_cohort": sum(1 for w in wallets if w.smart_cohort),
        "kol_cohort": sum(1 for w in wallets if w.kol_cohort),
        "feed_only": sum(1 for w in wallets if w.cohort_label == COHORT_LABEL_FEED_ONLY),
        "tags_unavailable": sum(1 for w in wallets if w.tags_basis is EvidenceBasis.UNAVAILABLE),
        "feed_rows": sum(w.feed_rows for w in wallets),
    }


__all__ = [
    "COHORT_FEED_SOURCES",
    "COHORT_LABEL_FEED_ONLY",
    "COHORT_LABEL_KOL",
    "COHORT_LABEL_SMART",
    "GMGN_KOL_COHORT_TAGS",
    "GMGN_SMART_COHORT_TAGS",
    "CohortWallet",
    "cohort_summary",
    "cohort_wallets",
    "CADENCE_MIN_CLOSED",
    "COENTITY_MIN_SHARED_TOKENS",
    "DEFAULT_ALPHA",
    "DISCOVERY_COVARIATES",
    "DISCOVERY_UNMATCHED",
    "MIN_DISTINCT_TOKENS",
    "MIN_INTERESTING_EDGE",
    "MIN_MEDIAN_HOLD_S",
    "MIN_SWAPS_TO_ASSESS",
    "MODEL_ID",
    "NULL_MIN_POOLED_TRADES",
    "PROPOSED_COHORT",
    "PROTECTED_COHORTS",
    "SINGLE_TOKEN_MAX_PNL_SHARE",
    "TX_FAILURE_MIN_OBSERVED",
    "TX_FAILURE_REJECT_RATE",
    "Candidate",
    "CandidateFeatures",
    "CandidateStatus",
    "ChanceReport",
    "ChanceRow",
    "DiscoveryReport",
    "RejectReason",
    "Rejection",
    "binomial_tail_p",
    "bonferroni_alpha",
    "chance_table",
    "discover",
    "entity_keys",
    "expected_peers",
    "forward_report",
    "freeze_cohort",
    "min_closed_trades",
    "min_closed_trades_exact",
    "population_null",
    "propose_research_cohort",
    "rejections_for",
    "store_report",
]
