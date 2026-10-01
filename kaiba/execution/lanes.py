"""Entry lanes: one pure evaluator per strategy, all of them cheap and side-effect free.

Why this shape:

* A lane is ``evaluate(ctx: LaneContext) -> Signal | None``. No I/O beyond the read-only
  lookups the context cannot carry (wallet cohorts, entity clustering, caller reputation),
  no order placement, no state mutation. The engine decides; the lane only observes. That
  makes every lane replayable against point-in-time data, which is what the promotion
  gates in PLAN §8 require.
* ``None`` is the normal answer. Most of the time nothing is happening, and a lane that
  always finds something is a lane that has learned to fire on noise.
* **Missing evidence never reads as safe.** If bundler share or rug ratio is unknown, the
  gate that depends on it fails closed and the lane stays silent (CONTRACT rule 2). The
  base rates in ``docs/research/02-memecoin-edge-and-risk.md`` are brutal — 98.6% of
  pump.fun tokens fall below $1k liquidity, 76% of new tokens are rug candidates — so the
  default answer to "we don't know" is "no".
* Counting is done over **independent entities, not addresses**. Five addresses funded by
  one wallet are one opinion, and 1,012 persistent sniper rings were measured across 166k
  launches in Jun 2026 (arXiv 2607.02795). ``confluence_5`` in particular is a lane whose
  whole claim to edge collapses if it counts addresses.

Evidence quality per lane is recorded on the signal (``payload["evidence"]``) because the
lanes differ enormously in how well supported they are: curve velocity is grade A, the
migration dump is grade A, confluence is grade C (a platform feature with no public
backtest — it must earn its keep in shadow).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from statistics import mean
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiba.core.config import get_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import (
    HARD_QUARANTINE_TAGS,
    Chain,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    Signal,
    Token,
    TokenDossier,
    WalletScore,
    WalletTag,
    digest,
    now_ms,
)

# The transfer-in vocabulary lives with the position rebuilder that has to recognise it on
# the tape; a lane that refuses to copy inventory inflow must refuse the same words, so it
# imports them rather than keeping a second list to drift. ``pnl`` imports only
# ``core.schemas``, so this is not a cycle.
from kaiba.intelligence.pnl import TRANSFER_IN_SIDES

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# sibling modules that are landing in parallel
# --------------------------------------------------------------------------------------

try:  # kaiba/intelligence/entity.py — written concurrently by another agent
    from kaiba.intelligence.entity import independent_entity_count
except ImportError:  # sibling module still landing

    def independent_entity_count(chain: Chain, addresses: list[str], conn: Any = None) -> int:
        """Fallback: one address, one entity.

        This is the *permissive* direction — it can only over-count, so a confluence gate
        will fire more readily than it should until the real clustering lands. That is
        deliberate and loud rather than silent: ``tests/test_lanes.py`` stubs this to 1 to
        prove the lane collapses five sybil addresses into one opinion when the real
        implementation says so.
        """
        return len({a for a in addresses if a})


try:  # kaiba/intelligence/grade.py — written concurrently by another agent
    from kaiba.intelligence.grade import load_score
except ImportError:  # sibling module still landing

    def load_score(chain: Chain, address: str, conn: Any = None) -> WalletScore | None:
        """Fallback: read the latest grade straight out of ``wallet_scores``.

        Same table the real loader owns, so behaviour should be identical; it simply does
        not refresh a stale grade.
        """
        return _score_from_table(chain, address, conn)


# --------------------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------------------


class LaneContext(BaseModel):
    """Everything a lane is allowed to look at.

    Assembled once per candidate token by the caller (the ingest loop or a replay), so all
    lanes see exactly the same snapshot and a replay can reproduce a decision byte for
    byte. ``extras`` carries the lane-specific one-offs that do not deserve a first-class
    field: a migration event, an exchange listing, a trenches preset summary.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    chain: Chain
    token: str
    now_ms: int = Field(default_factory=now_ms)
    conn: sqlite3.Connection | None = None
    dossier: TokenDossier | None = None
    recent_buys: list[dict[str, Any]] = Field(default_factory=list)
    token_meta: Token | None = None
    curve: dict[str, Any] | None = None
    caller: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)

    def lane_params(self, lane: Lane) -> dict[str, Any]:
        """Defaults, then ``config/risk.yaml``, then whatever the caller overrode.

        Overrides may be flat (``{"min_entities": 4}``, applied to the lane being
        evaluated) or keyed by lane value (``{"confluence-5": {...}}``) when one context is
        reused for a full ``evaluate_all`` pass.
        """
        merged: dict[str, Any] = dict(DEFAULT_PARAMS.get(lane, {}))
        try:
            merged.update(get_risk().lane(lane).params or {})
        except Exception as exc:  # a malformed risk.yaml must not take the lanes down
            log.warning("risk config unreadable, using lane defaults: %s", exc)
        lane_keys = {m.value for m in Lane}
        for key, value in self.params.items():
            if key in lane_keys:
                if key == lane.value and isinstance(value, dict):
                    merged.update(value)
                continue
            merged[key] = value
        return merged


#: Shipped defaults. ``config/risk.yaml`` is the operator's copy and wins over these; they
#: exist so a lane still behaves sensibly if a key is missing from the file.
DEFAULT_PARAMS: dict[Lane, dict[str, Any]] = {
    Lane.CONFLUENCE_5: {
        "min_entities": 5,
        "window_s": 120,
        "min_buy_usd": 50,
        "max_signal_age_s": 30,
        "require_wallet_grade": "B",
    },
    Lane.TRUSTED_COPY: {
        "max_copy_delay_s": 20,
        "max_price_drift_pct": 12,
        "follow_exits": True,
    },
    Lane.CURVE_VELOCITY: {
        "min_progress_pct": 30,
        "max_progress_pct": 70,
        "min_sol_per_min": 3.0,  # fallback denominator only; see curve_velocity
        # DERIVED, not invented: a median pump.fun graduation raises 85 SOL over ~457
        # trades (docs/research/10-solana-edge-2026.md), so the average graduating token
        # runs at 85/457 = 0.186 SOL per swap. This floor sits just under that, so a
        # token pacing at or above the graduation average clears it. It is a population
        # average standing in for a threshold, which is weaker than a measured cut point,
        # and it should be replaced by a percentile from our own swaps once the backfill
        # has enough tokens to compute one.
        "min_sol_per_swap": 0.18,  # absolute fallback when the target is unknown
        # DERIVED: a median graduation takes ~457 trades, so the average graduating
        # swap carries 1/457 = 0.00219 of the target. The floor sits just *under*
        # that, because a floor set at the population average fails half of all
        # graduating tokens by construction. Scales across curve sizes where the
        # absolute SOL figure above does not.
        "min_swap_fraction_of_graduation": 0.0021,
        "max_bundler_pct": 20,
        "min_graded_wallets": 1,
        "window_s": 60,
    },
    Lane.MIGRATION_FADE: {
        "sell_within_s": 180,
        "max_hold_s": 1200,
        "never_hold_through_migration": True,
    },
    Lane.KOL_FADE: {
        "min_caller_trades": 10,
        "min_caller_expectancy": 0.0,
        # Not in risk.yaml: KOL call value decays in minutes (80% of KOL-promoted coins are
        # down >=70% after a week), so a call nobody acted on inside this window is stale.
        "max_call_age_s": 300,
        "window_s": 300,
    },
    Lane.SM_TRENCHES: {
        # The live lane had NO liquidity floor until 2026-09-23. It is the single largest
        # measured difference in this book's results, on BOTH kinds of evidence:
        #
        #   our own 113 closed live fills, by liquidity at entry
        #       > $15.5k    n=44   mean  +2.1%   win 32%   <- the only profitable band
        #       $5-15.5k    n=53   mean -31.0%   win  9%
        #       <= $5k      n=16   mean -33.9%   win  6%
        #
        #   and 7,837 tokens under an hour old on the tape, reaching 5x after entry
        #       no floor    56% kept    9.6%    1.00x
        #       $5,000      21% kept   17.4%    1.82x
        #       $15,575     11% kept   22.7%    2.37x   <- peak
        #       $25,000      6% kept   19.0%    1.98x
        #       $50,000      4% kept   11.8%    1.23x
        #
        # The PEAK is what makes this a real effect rather than a proxy for size: lift
        # rises to $15.5k and falls above it, because a token that already has $50k of
        # book has already made the move we are trying to catch. This is the
        # early-with-traction band, not a bigger-is-better rule.
        #
        # PER CHAIN since 2026-09-23, because a single number was measured on a sample
        # Solana dominates and then applied to books that are nothing like Solana's.
        # Median liquidity of a token under an hour old: sol $3,576, bsc $3,630, but
        # robinhood only $1,475. The flat $15,000 cost robinhood 88% of its signals
        # (15.3/h -> 1.9/h) on the chain that was producing most of the fills.
        #
        # Re-measured per chain on young tokens, reach-5x lift against that chain's own
        # base rate:
        #
        #   sol        n=3407   $10k -> 1.94x keeping 27%   ($15k was 1.95x keeping 22%)
        #   bsc        n= 236   $10k -> 2.48x keeping 22%   ($15k goes too thin to score)
        #   robinhood  n= 962  $7.5k -> 2.83x keeping 28%   ($15k was 3.76x keeping 16%)
        #
        # sol and bsc at $10k are strictly better than the flat $15k -- MORE candidates at
        # the same or higher lift, so there was nothing to trade off. Robinhood is a real
        # choice: its lift keeps climbing past $15k, but the owner wants entries and 2.83x
        # on 28% of the book beats 3.76x on 16% when the book is what feeds every lane.
        #
        # A scalar still works and applies to every chain; a mapping takes "default" for
        # anything unlisted.
        "min_liquidity_usd": {
            "sol": 10000,
            "bsc": 10000,
            "robinhood": 7500,
            "default": 10000,
        },
        # OWNER POLICY 2026-09-23: "only trade on pump.fun, stonkfun and launchpads",
        # on every chain, and "only trade manual deploy IF really good wallets are in
        # and volume is good".
        #
        # A token with no launchpad is a MANUAL deploy. MEASURED on the tape, reach-5x
        # for a launchpad token against a manual one:
        #
        #     sol         launchpad  8.0%   manual  4.1%     (2.0x worse)
        #     bsc         launchpad 14.7%   manual  5.5%     (2.7x worse)
        #     robinhood   launchpad  6.1%   manual  8.4%     (manual is BETTER here)
        #
        # Robinhood is the exception and it is not a small one, so it is written down
        # rather than smoothed over: 60% of its "launchpad" volume is pons v1, which the
        # mooner study put at 0.81x lift -- below its own chain's base rate. The policy is
        # still applied there because the owner asked for it on all three chains, and
        # because the escape hatch below is what a good manual deploy uses.
        "require_launchpad": True,
        # The escape hatch. Manual deploys are admitted only with MORE smart-money
        # confluence than a launchpad token needs. MEASURED, manual deploys by smart
        # wallets present early:
        #
        #     sol        0 wallets 1.8%   1-2 10.6%   3-4 8.4%   >=5 8.2%
        #     bsc        0 wallets 4.0%   1-2  5.4%   3-4 6.2%   >=5 5.0%
        #     robinhood  0 wallets 5.7%   1-2 19.5%   3-4 5.0%   >=5 5.4%
        #
        # On sol the bar works: a manual deploy with smart money behind it matches the
        # launchpad baseline (8.4% against 8.0%) while one with none is a disaster at
        # 1.8%. On bsc nothing rescues it, and on robinhood the numbers are
        # non-monotonic, which is noise rather than a signal. 5 is the owner's "really
        # good wallets" expressed as the only thing we can measure honestly.
        #
        # NOT IMPLEMENTED: the volume half of the instruction. `volume_24h_usd` is
        # UNKNOWN on 100% of the tokens we have dossiers for -- we do not collect it --
        # and a threshold against a field nobody populates is a gate that never fires or
        # always does, depending on which way the `None` falls. Gating on wallets alone
        # is stated here rather than quietly half-implemented.
        "manual_min_smart_degen": 5,
        # RESTORED 2026-09-24. These five keys lived in a SECOND `Lane.SM_TRENCHES`
        # entry earlier in this same dict literal, so Python kept only the later one
        # and silently discarded them: DEFAULT_PARAMS[Lane.SM_TRENCHES] held just
        # manual_min_smart_degen, min_liquidity_usd and require_launchpad. The lane
        # still ran because config/risk.yaml supplies them, which is exactly what made
        # it invisible -- and it defeated the promise at the top of this dict, that the
        # defaults 'exist so a lane still behaves sensibly if a key is missing from the
        # file'. `sm_trenches` reads p["min_smart_degen"] by direct index, so a
        # risk.yaml that lost the key would have raised rather than fallen back.
        "min_smart_degen": 3,
        "min_independent_entities": 2,
        "max_rug_ratio": 0.3,
        "window_s": 300,
        "min_buy_usd": 0,
        #
        # HOLDER FLOOR. MEASURED 2026-09-24 over 177 closed LIVE fills, chosen on the
        # older 88 and scored on the newer 89 that were never used to pick it:
        #
        #     holders      n     mean       (in-sample buckets, all 177)
        #     1k+         16    +4.2%
        #     200-1k      34    +1.6%
        #     50-200      28   -31.1%
        #     <50         17   -27.4%
        #
        #     rule                older     NEWER
        #     take everything    -17.2%     -9.9%   (sum -8.84)
        #     holders >= 200      +4.0%     +2.5%   (sum +0.72)
        #     + launchpad cut     -1.0%    +14.0%   (sum +2.95)
        #
        # The floor is MONOTONIC on the holdout -- 50: -9.5%, 100: -4.9%, 200: +2.5%,
        # 300: +5.6%, 500: +6.6%, 1000: +8.1% -- which is what separates a real effect
        # from a bucket that happened to win. 200 is taken rather than 500 because the
        # higher floors keep too few fills for this to stay a lane rather than a rarity.
        #
        # An UNKNOWN holder count REFUSES, exactly as liquidity does. holder_count is
        # carried by 54% of the tokens we bought, so this roughly halves the book by
        # construction. That is the cost, and the holdout says it is worth paying.
        # Shipped OFF here and set to 200 in config/risk.yaml, the same way every
        # other tuned number in this system lives there rather than in code. The
        # default must stay 0: a lane default that silently refused every token
        # whose holder count we never read would be a kill switch disguised as a
        # filter for any caller that has not opted in. tests/test_holder_floor.py
        # asserts the SHIPPED CONFIG carries 200, so this is enforced, not declared.
        "min_holder_count": 0,
        # LAUNCHPAD BLOCKLIST, same holdout. Per-launchpad mean over the live fills:
        # pons +16.4% (n13), ray_launchpad +15.5% (n8), pump -9.6% (n15), flap -12.6%
        # (n40), pons_v2 -22.0% (n12), pump.fun -28.1% (n39, sum -10.9, the single
        # biggest bleed in the book), longxyz -29.8% (n13, win rate ZERO).
        #
        # Only the ones that lose ~30% with almost no winners are blocked. flap and
        # pons_v2 are negative too, but they are also most of the venue on bsc, and
        # blocking them is a decision about which chains trade at all, not a filter.
        "blocked_launchpads": ["pump.fun", "pump", "longxyz"],
    },
    Lane.LISTING_POP: {"max_latency_s": 30},
    Lane.PONS_ROBINHOOD: {
        "min_entities": 3,
        "max_age_s": 1800,
        "min_liquidity_usd": 5000,
        # Not in risk.yaml: a dust filter so wallet-count confluence cannot be met by
        # 1-cent buys. Lower than confluence-5 because Pons has lower bot density.
        "min_buy_usd": 25,
    },
}

GRADE_POINTS: dict[Grade, int] = {
    Grade.A: 4,
    Grade.B: 3,
    Grade.C: 2,
    Grade.D: 1,
    Grade.UNSCORED: 0,
    Grade.QUARANTINED: -1,
}

#: ``gmgn:<label>``; imported rather than spelled so one file owns the namespace.
from kaiba.intelligence.naming import vendor_tag  # noqa: E402

SMART_TAGS: frozenset[WalletTag] = frozenset(
    {WalletTag.SMART_MONEY, WalletTag.PUMP_SMART, WalletTag.RENOWNED, WalletTag.TOP_TRADER}
)

#: GMGN's OWN labels for its smart-money cohort, as they arrive on ``maker_info.tags``.
#: None of them is a :class:`WalletTag`, which is exactly why this set has to exist:
#: ``SMART_TAGS`` above and these three shared not one label until 2026-09-23, so a wallet
#: GMGN calls smart counted for nothing unless ``tracker.seed_from_cohorts`` had ADMITTED
#: it and written the bare ``smart_money`` tag. Most feed wallets never are -- the screen
#: refuses them for "fewer than 10 observed swaps, so the sell-only filter cannot run" --
#: and on the live box that left 518 sol, 759 robinhood and 423 bsc labelled wallets whose
#: buys the lane could not see.
#:
#: Kept in step with :data:`kaiba.intelligence.discover.GMGN_SMART_COHORT_TAGS` by a test;
#: spelled here rather than imported because ``discover`` imports this module.
SMART_COHORT_LABELS: frozenset[str] = frozenset(
    {"smart_degen", "app_smart_money", "launchpad_smart"}
)

#: Chains where a raw cohort label may count a buyer as smart ON ITS OWN.
#:
#: MEASURED 2026-09-23, forward reach-2x for tokens these wallets bought early, against
#: that chain's own base rate, STRATIFIED by token print count -- unstratified reach-N is
#: mostly an artefact of how active a token was, and this same comparison showed a
#: spurious 2.81x before stratification:
#:
#:     band          base   ADMITTED   UNTAGGED   UNTAGGED>=2
#:     sol    5-8    6.1%      2.96x      2.68x     3.69x
#:     sol   8-16   14.0%      2.64x      2.65x     3.26x
#:     sol  16-39   18.3%      2.73x      2.63x     3.59x
#:     rh   17-28   15.0%      2.03x      2.68x     2.59x
#:     rh   28-69   34.8%      1.22x      1.73x     2.39x
#:     bsc (every band)        1.29/1.01/1.00/1.07/1.02/1.01x
#:
#: On sol and robinhood the wallets we were ignoring predict as well as the ones we count,
#: and two of them together beat one admitted wallet. On bsc the label is worth nothing at
#: any activity level -- its base rate reaches 80% and the label adds ~0 -- so admitting it
#: there would buy entries with no edge behind them. Chain-scoped for the same reason
#: ``robinhood`` could never grade B on a Solana rubric: these books are not alike.
SMART_COHORT_LABEL_CHAINS: frozenset[Chain] = frozenset({Chain.SOL, Chain.ROBINHOOD})

#: :data:`SMART_COHORT_LABELS` in the spelling they are actually stored in. A vendor label
#: is written ``gmgn:<label>`` before any screen runs, and the cohort route matches this
#: set rather than the bare one so that it cannot be confused with an admitted wallet's
#: screened tag. See :func:`_tags`.
SMART_COHORT_VENDOR_TAGS: frozenset[str] = frozenset(
    vendor_tag(label) for label in SMART_COHORT_LABELS
)

#: Row vocabulary that means inventory ARRIVED, not that a wallet bought anything.
#: The operator's rule: if a tracked wallet *bought*, we may copy; if it was *transferred
#: in*, we DYOR instead. An airdrop, a mint or a receive proves tokens moved and nothing
#: about anyone paying for them, so it can never be a copy source at any size.
#: The spellings are the ones this codebase already writes and reads, not a guessed list:
#: ``pnl.TRANSFER_IN_SIDES`` is what the position rebuilder recognises on tape rows, and
#: ``WalletTag.TRANSFER_IN`` is the GMGN cohort label carried on feed rows. A spelling
#: neither of them knows is still refused, because a copy source has to SAY "buy"
#: (:func:`_not_a_buy_reason`); this set only catches a row that says "buy" somewhere and
#: contradicts itself somewhere else.
TRANSFER_IN_MARKERS: frozenset[str] = frozenset(
    TRANSFER_IN_SIDES | {WalletTag.TRANSFER_IN.value}
)

#: Where a row can state what it is besides ``side``. The same field names
#: ``ingest.gmgn_feeds._side`` reads, because those are the ones the feeds actually use.
#: That decoder maps only the buy/sell vocabulary, so a ``transfer_in`` in any of them
#: decodes to ``None`` and the row travels on with whatever ``side`` its writer defaulted
#: to -- which is exactly how inventory inflow reaches a lane wearing a buy.
SIDE_FIELDS: tuple[str, ...] = ("side", "event_type", "type", "direction", "tx_type")


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _measure(dossier: TokenDossier | None, field: str) -> Decimal | None:
    """A dossier number, or ``None`` when it is missing or past its freshness budget."""
    if dossier is None:
        return None
    m = getattr(dossier, field, None)
    if m is None or not getattr(m, "known", False) or getattr(m, "stale", False):
        return None
    return m.value


def _conn(ctx: LaneContext) -> sqlite3.Connection | None:
    return ctx.conn


def _score_from_table(chain: Chain, address: str, conn: Any = None) -> WalletScore | None:
    c = conn or get_conn()
    row = fetch_one(
        c,
        "SELECT * FROM wallet_scores WHERE chain=? AND address=?",
        (chain.value if isinstance(chain, Chain) else str(chain), address),
    )
    if not row:
        return None
    return WalletScore(
        address=row["address"],
        chain=Chain(row["chain"]),
        score=row["score"],
        grade=Grade(row["grade"]),
        evidence_weight=row["evidence_weight"],
        archetype=row["archetype"],
        blockers=jload(row["blockers_json"], []),
        penalties=jload(row["penalties_json"], []),
        realized_pnl_usd=_dec(row["realized_pnl_usd"]),
        win_rate=row["win_rate"],
        closed_trades=row["closed_trades"],
        distinct_tokens=row["distinct_tokens"],
        median_hold_s=row["median_hold_s"],
        model_version=row["model_version"],
        scored_at_ms=row["scored_at_ms"],
    )


def _score(chain: Chain, address: str, conn: Any) -> WalletScore | None:
    """Grade lookup that survives a half-written sibling module."""
    try:
        return load_score(chain, address, conn)
    except Exception as exc:  # pragma: no cover - only hit while grade.py is landing
        log.debug("load_score failed for %s, falling back to table: %s", address, exc)
        try:
            return _score_from_table(chain, address, conn)
        except Exception:
            return None


def _entities(chain: Chain, addresses: list[str], conn: Any) -> int:
    try:
        return int(independent_entity_count(chain, list(addresses), conn))
    except Exception as exc:  # pragma: no cover - only hit while entity.py is landing
        log.debug("independent_entity_count failed, counting addresses: %s", exc)
        return len(set(addresses))


def _entity_ids(chain: Chain, addresses: list[str], conn: Any) -> list[str]:
    """Best-effort entity ids for the evidence trail. The *count* never comes from here."""
    ids: list[str] = []
    seen: set[str] = set()
    for addr in addresses:
        eid = None
        if conn is not None:
            try:
                row = fetch_one(
                    conn,
                    "SELECT entity_id FROM entity_members WHERE chain=? AND address=?",
                    (chain.value, addr),
                )
                eid = row["entity_id"] if row else None
            except sqlite3.Error:
                eid = None
        eid = eid or f"addr:{addr}"
        if eid not in seen:
            seen.add(eid)
            ids.append(eid)
    return ids


def _wallet_row(chain: Chain, address: str, conn: Any) -> dict[str, Any] | None:
    if conn is None:
        return None
    try:
        return fetch_one(
            conn, "SELECT * FROM wallets WHERE chain=? AND address=?", (chain.value, address)
        )
    except sqlite3.Error:
        return None


def _tags(chain: Chain, buy: dict[str, Any], conn: Any) -> set[str]:
    """This wallet's labels VERBATIM, row first then the wallets table.

    The vendor namespace stays on. It is not noise: ``tracker.seed_from_cohorts`` writes
    every GMGN label as ``gmgn:<label>`` BEFORE its screen runs, and writes the bare
    ``smart_money`` only after a wallet passes. So a bare tag here means "we screened this
    wallet" and a namespaced one means "a vendor said so and nobody checked". Stripping the
    namespace collapses that distinction and lets an unscreened wallet into the smart
    cohort -- the leak ``tests/test_tracker_tag_leak.py`` exists to prevent. A caller that
    deliberately wants the unscreened claim must ask for the namespaced spelling by name,
    the way the cohort route in :func:`sm_trenches` does.
    """
    raw = buy.get("tags")
    if raw is None:
        row = _wallet_row(chain, str(buy.get("wallet", "")), conn)
        raw = jload(row["tags_json"], []) if row else []
    if isinstance(raw, str):
        raw = jload(raw, [])
    return {str(t) for t in (raw or [])}


def _row_tags(row: dict[str, Any]) -> set[str]:
    """The row's OWN labels, vendor namespace stripped. Never the wallets table.

    ``_tags`` falls back to ``wallets.tags_json`` when a row carries none. That is a fact
    about everything the wallet has ever done, across every token, and it must not decide
    what THIS fill was: a wallet that once received an airdrop would otherwise be
    uncopyable forever. GMGN labels are written namespaced (``gmgn:transfer_in``,
    ``naming.GMGN_TAG_PREFIX``), so the namespace comes off before comparing.
    """
    raw = row.get("tags")
    if isinstance(raw, str):
        raw = jload(raw, [])
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    return {str(t).strip().lower().rsplit(":", 1)[-1] for t in raw}


def _not_a_buy_reason(row: dict[str, Any]) -> str | None:
    """Why this row cannot be copied as a buy, or ``None`` when it is an explicit buy.

    Three ways a row fails, in the order a reader wants them:

    * ``side`` absent or empty. This is UNKNOWN. The previous spelling of this gate,
      ``str(row.get("side") or "buy")``, turned exactly that unknown into the single value
      that authorises spending money (CONTRACT rule 2: missing evidence never reads as the
      favourable one).
    * a transfer-in marker anywhere on the row, even alongside ``side="buy"``. Inventory
      that merely arrived is not evidence that anyone bought, and a row that says "buy" in
      one field and "transfer_in" in another has not proved a purchase either -- the
      disagreement is the reason to refuse, not something to settle in our favour.
    * any other ``side``. ``sell``, ``in``, or a spelling nobody has decoded: a copy source
      has to say ``buy``, because that is the only word that means someone paid.
    """
    side = str(row.get("side") or "").strip().lower()
    if not side:
        return "side is absent or empty: UNKNOWN, which is not a buy"
    for field in SIDE_FIELDS:
        value = row.get(field)
        if value is not None and str(value).strip().lower() in TRANSFER_IN_MARKERS:
            return f"{field}={value!r} marks inventory arriving, not a purchase"
    for tag in sorted(_row_tags(row) & TRANSFER_IN_MARKERS):
        return f"row tag {tag!r} marks inventory arriving, not a purchase"
    if side != "buy":
        return f"side={side!r} is not an explicitly decoded buy"
    return None


def _is_decoded_buy(row: dict[str, Any]) -> bool:
    """Only an explicitly decoded BUY, with nothing on the row contradicting it."""
    return _not_a_buy_reason(row) is None


def _min_liquidity_for(params: dict[str, Any], chain: Chain) -> Decimal:
    """The liquidity floor for this chain. Accepts a scalar or a per-chain mapping.

    A scalar applies everywhere, which is what every lane but ``sm_trenches`` still uses.
    A mapping is read by ``chain.value`` and falls back to ``"default"``; a chain that is
    listed nowhere and has no default gets zero, because an unconfigured floor is an
    absent one and must not become a silent refusal of the whole chain.
    """
    raw = params.get("min_liquidity_usd", 0)
    if isinstance(raw, dict):
        raw = raw.get(chain.value, raw.get("default", 0))
    return _dec(raw) or Decimal(0)


def _cohort(chain: Chain, buy: dict[str, Any], conn: Any) -> str | None:
    """The wallet's cohort AS THE DATABASE HAS IT. A feed's own claim is never trust.

    Both callers compare the result against ``"trusted_copy"`` -- they are asking whether
    we trust this wallet enough to copy it with money. The row being examined comes from a
    provider feed, so reading ``buy["cohort"]`` first (which this did until 2026-09-23)
    meant any feed that emitted ``cohort: "trusted_copy"`` promoted an arbitrary wallet
    into the one lane defined by curation, without the database ever agreeing and without
    leaving a trace: the ``wallets`` row keeps whatever cohort it always had, so nothing
    afterwards shows that a promotion happened.

    Cohort is a decision WE make about a wallet. The feed is the subject of that decision,
    not a participant in it.
    """
    row = _wallet_row(chain, str(buy.get("wallet", "")), conn)
    return row["cohort"] if row and row["cohort"] else None


def _grade_of(name: Any, default: Grade = Grade.B) -> Grade:
    try:
        return Grade(str(name).upper())
    except ValueError:
        return default


def _bucket_window_s(params: dict[str, Any]) -> int:
    for key in ("window_s", "max_copy_delay_s", "sell_within_s", "max_latency_s", "max_age_s"):
        value = params.get(key)
        if value:
            return int(value)
    return 60


def signal_id_for(lane: Lane, chain: Chain, token: str, at_ms: int, window_s: int) -> str:
    """Deterministic id: the same lane, token and time bucket always produce the same id.

    Re-evaluating a candidate every few seconds must not multiply into a dozen signals, and
    a replay must reproduce the id exactly, so nothing time-varying below the bucket and
    nothing random goes into the digest.
    """
    bucket = at_ms // (max(int(window_s), 1) * 1000)
    return digest({"lane": lane.value, "chain": chain.value, "token": token, "bucket": bucket})


def _net_buyers(
    rows: list[dict[str, Any]], *, since_ms: int, until_ms: int, min_buy_usd: Decimal
) -> dict[str, dict[str, Any]]:
    """Wallets whose buys inside the window outweigh their sells.

    A wallet whose USD value we could not price contributes 0 and therefore fails
    ``min_buy_usd``: an unpriced fill is not evidence of conviction.
    """
    agg: dict[str, dict[str, Any]] = {}
    for row in rows:
        wallet = str(row.get("wallet") or "")
        if not wallet:
            continue
        ts = int(row.get("ts_ms") or 0)
        if ts < since_ms or ts > until_ms:
            continue
        side = str(row.get("side") or "").strip().lower()
        if not side:
            # UNKNOWN, and therefore neither. Until 2026-09-22 this read
            # `str(row.get("side") or "buy")`, so a missing field became the one word that
            # means somebody paid -- and this function's output IS conviction, read by
            # `confluence-5` and `sm-trenches` as strength. Counting it as a sell instead
            # would be the same mistake pointing the other way, so the row is dropped.
            # MEASURED before this changed: zero null or empty sides across 996,805 swaps
            # in 24 h (sol 502,381, robinhood 469,196, bsc 25,228), so this costs nothing
            # today and guards a feed that changes shape.
            continue
        usd = _dec(row.get("usd_value")) or Decimal(0)
        acc = agg.setdefault(
            wallet,
            {
                "buy_usd": Decimal(0),
                "sell_usd": Decimal(0),
                "max_buy_usd": Decimal(0),
                "last_buy_ms": 0,
                "first_buy_ms": 0,
                "native": 0,
                "buys": 0,
            },
        )
        if side == "buy":
            acc["buy_usd"] += usd
            acc["max_buy_usd"] = max(acc["max_buy_usd"], usd)
            acc["last_buy_ms"] = max(acc["last_buy_ms"], ts)
            acc["first_buy_ms"] = ts if not acc["first_buy_ms"] else min(acc["first_buy_ms"], ts)
            # `amount_native` is integer base units or None (UNAVAILABLE). None means the
            # feed could not prove the quote asset; the wallet's usd_value and its buy
            # still count, it just adds nothing to the native tally. A value with a dot is
            # a UI-unit number in a base-units column (MEASURED 2026-09-21: 50,747 gmgn
            # feed rows on the live box) and used to raise ValueError out of every lane
            # pass; it is UNAVAILABLE too, never int()-truncated and never a zero fill.
            raw_native = row.get("amount_native")
            if raw_native is not None and raw_native != "":
                try:
                    acc["native"] += int(str(raw_native))
                except ValueError:
                    if not getattr(_net_buyers, "_warned_ui_units", False):
                        _net_buyers._warned_ui_units = True  # type: ignore[attr-defined]
                        log.warning(
                            "swaps.amount_native holds a non-integer (%r) for wallet %s; "
                            "treated as UNAVAILABLE. Run kaiba.ingest.gmgn_feeds "
                            "backfill-amount-native. Logged once per process.",
                            str(raw_native)[:24], wallet[:12],
                        )
            acc["buys"] += 1
        else:
            acc["sell_usd"] += usd
    return {
        w: a
        for w, a in agg.items()
        if a["buys"] and a["buy_usd"] - a["sell_usd"] > 0 and a["buy_usd"] >= min_buy_usd
    }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, value)), 4)


# --------------------------------------------------------------------------------------
# lanes
# --------------------------------------------------------------------------------------


def confluence_5(ctx: LaneContext) -> Signal | None:
    """>=N *independent entities* net-buying the same token inside a short window.

    Evidence grade C: this is a platform feature (GMGN/Axiom) with no public backtest, so
    it ships in shadow and has to prove itself. The one thing that makes our version worth
    running is the entity collapse — the sybil rings the research measured would otherwise
    manufacture this signal on demand.
    """
    p = ctx.lane_params(Lane.CONFLUENCE_5)
    window_s = int(p["window_s"])
    min_entities = int(p["min_entities"])
    min_buy_usd = _dec(p["min_buy_usd"]) or Decimal(0)
    max_age_s = int(p["max_signal_age_s"])
    min_grade = _grade_of(p.get("require_wallet_grade", "B"))

    buyers = _net_buyers(
        ctx.recent_buys,
        since_ms=ctx.now_ms - window_s * 1000,
        until_ms=ctx.now_ms,
        min_buy_usd=min_buy_usd,
    )
    if not buyers:
        return None

    qualified: list[str] = []
    points: list[int] = []
    newest = 0
    for wallet, acc in sorted(buyers.items()):
        score = _score(ctx.chain, wallet, _conn(ctx))
        if score is None or GRADE_POINTS.get(score.grade, 0) < GRADE_POINTS[min_grade]:
            continue
        qualified.append(wallet)
        points.append(GRADE_POINTS.get(score.grade, 0))
        newest = max(newest, int(acc["last_buy_ms"]))

    if len(qualified) < min_entities:
        return None
    if newest and (ctx.now_ms - newest) > max_age_s * 1000:
        return None  # the move already happened; entering now is chasing

    entities = _entities(ctx.chain, qualified, _conn(ctx))
    if entities < min_entities:
        return None

    mean_points = mean(points) if points else 0.0
    entity_factor = min(1.0, entities / float(max(1, min_entities * 2)))
    grade_factor = mean_points / 4.0
    strength = _clamp(0.55 * entity_factor + 0.45 * grade_factor)
    age_s = round((ctx.now_ms - newest) / 1000.0, 3) if newest else None

    return Signal(
        signal_id=signal_id_for(Lane.CONFLUENCE_5, ctx.chain, ctx.token, ctx.now_ms, window_s),
        lane=Lane.CONFLUENCE_5,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"{entities} independent entities ({len(qualified)} addresses) net-bought within {window_s}s",
            f"mean grade points {mean_points:.2f} (floor {min_grade.value})",
            f"newest qualifying buy {age_s}s old",
        ],
        wallets=qualified,
        entities=_entity_ids(ctx.chain, qualified, _conn(ctx)),
        window_s=window_s,
        created_ms=ctx.now_ms,
        payload={
            "entity_count": entities,
            "address_count": len(qualified),
            "mean_grade_points": round(mean_points, 4),
            "min_entities": min_entities,
            "newest_buy_age_s": age_s,
            "evidence": "C: platform feature, no public backtest — measure in shadow",
        },
    )


def _log_uncopyable_inflow(ctx: LaneContext) -> None:
    """Say why a trusted wallet's row was refused, instead of going quiet.

    A lane that answers "nothing here" identically whether it saw nothing or refused
    something is the silent idle this codebase keeps rediscovering, and "the wallet holds
    it but did not buy it" is precisely the case the operator wants to hear about: it is
    their cue to DYOR the token by hand.

    Only odd rows are examined — an unknown side or a transfer-in marker. A plain ``sell``
    is not news, and ``_cohort`` costs a query per row it is asked about (``recent_buys``
    reaches 4,000 rows, ``scanner.DEFAULT_CONFIG.recent_buys_limit``), so the loop must not
    ask about the whole tape. DEBUG because the same row is re-examined on every scan tick
    for as long as it stays inside the window; a routine refusal is not an event.
    """
    if not log.isEnabledFor(logging.DEBUG):
        return
    for row in ctx.recent_buys:
        if str(row.get("side") or "").strip().lower() == "sell":
            continue  # an exit is not uncopied inflow
        reason = _not_a_buy_reason(row)
        if reason is None:
            continue  # a real buy; a later gate (cohort, delay, drift) turned it down
        if _cohort(ctx.chain, row, _conn(ctx)) != "trusted_copy":
            continue
        log.debug(
            "trusted_copy: not copying trusted wallet %s on %s (tx %s): %s - DYOR instead",
            str(row.get("wallet") or "?")[:12], ctx.token[:12], str(row.get("tx") or "?")[:16],
            reason,
        )


def trusted_copy(ctx: LaneContext) -> Signal | None:
    """Copy one curated wallet, but only while the copy is still worth making.

    arXiv 2601.08641 measured copy-source *baiting*: sources that front-run their copiers.
    Two defences live here — a hard delay cap and a price-drift cap against the source's
    own fill. If we cannot price the current market we do not copy, because unknown drift
    is exactly the condition a baiting source is engineering.

    The first gate is narrower than either: the source row must be an explicitly decoded
    BUY (:func:`_not_a_buy_reason`). Tokens that merely landed in a trusted wallet — a
    transfer in, an airdrop, a mint — say nothing about anyone paying for them, and the
    operator's rule for that case is DYOR, never copy.
    """
    # Unknown safety is not safe (CONTRACT rule 2), and it bites hardest here: copying is
    # acting on someone else's research, so ours has to be complete before we follow them
    # in. Checked in the lane AND again in the engine, because the two run at different
    # moments against a dossier that can be refreshed in between.
    if getattr(ctx.dossier, "unknowns", None):
        return None
    p = ctx.lane_params(Lane.TRUSTED_COPY)
    max_delay_s = int(p["max_copy_delay_s"])
    max_drift_pct = _num(p["max_price_drift_pct"], 12.0)

    candidates = [
        r
        for r in ctx.recent_buys
        if _is_decoded_buy(r)
        and int(r.get("ts_ms") or 0) <= ctx.now_ms
        and _cohort(ctx.chain, r, _conn(ctx)) == "trusted_copy"
    ]
    if not candidates:
        _log_uncopyable_inflow(ctx)
        return None
    source = max(candidates, key=lambda r: int(r.get("ts_ms") or 0))
    delay_s = (ctx.now_ms - int(source.get("ts_ms") or 0)) / 1000.0
    if delay_s > max_delay_s:
        return None

    source_price = _dec(source.get("price_usd"))
    current_price = _measure(ctx.dossier, "price_usd") or _dec(ctx.extras.get("price_usd"))
    if source_price is None or source_price <= 0 or current_price is None:
        return None  # unknown drift is not zero drift
    drift_pct = float((current_price - source_price) / source_price * 100)
    if drift_pct > max_drift_pct:
        return None

    wallet = str(source.get("wallet"))
    score = _score(ctx.chain, wallet, _conn(ctx))
    grade = score.grade if score else Grade.UNSCORED
    grade_bonus = {Grade.A: 0.25, Grade.B: 0.15, Grade.C: 0.05}.get(grade, 0.0)
    drift_penalty = 0.2 * max(0.0, drift_pct) / max_drift_pct if max_drift_pct else 0.0
    delay_penalty = 0.15 * (delay_s / max_delay_s) if max_delay_s else 0.0
    strength = _clamp(0.6 + grade_bonus - drift_penalty - delay_penalty)

    return Signal(
        signal_id=signal_id_for(Lane.TRUSTED_COPY, ctx.chain, ctx.token, ctx.now_ms, max_delay_s),
        lane=Lane.TRUSTED_COPY,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"trusted_copy wallet {wallet} bought {delay_s:.1f}s ago (cap {max_delay_s}s)",
            f"price drift {drift_pct:+.2f}% vs source fill (cap {max_drift_pct}%)",
            f"source grade {grade.value}",
        ],
        wallets=[wallet],
        entities=_entity_ids(ctx.chain, [wallet], _conn(ctx)),
        window_s=max_delay_s,
        created_ms=ctx.now_ms,
        payload={
            "source_wallet": wallet,
            "source_price_usd": str(source_price),
            "current_price_usd": str(current_price),
            "drift_pct": round(drift_pct, 4),
            "delay_s": round(delay_s, 3),
            "source_tx": source.get("tx"),
            # Carried so `engine.decide` can repeat this lane's own decoded-buy check
            # rather than inherit its verdict. A signal travels through a queue and a
            # database between here and there; re-deciding on the evidence is what stops
            # a rewritten or replayed payload from entering on a transfer that never
            # bought anything. `None` is a real answer and stays in the payload: the
            # ABSENCE of these keys is what tells the engine nothing was decoded at all.
            "source_side": source.get("side"),
            "source_event": source.get("event_type") or source.get("type"),
            "follow_exits": bool(p.get("follow_exits", True)),
            "evidence": "A-: copy-source baiting is measured; vet the source, cap the drift",
        },
    )


def curve_velocity(ctx: LaneContext) -> Signal | None:
    """pump.fun bonding-curve velocity in the mid band, with bots kept out.

    Evidence grade A: SOL/min at a given curve progress is the strongest single graduation
    predictor in arXiv 2602.14860, against a 0.26-0.63% base rate. Bundler share above 20%
    is a documented red flag, and an *unknown* bundler share fails the gate — a bundled
    launch that hides its bundling looks identical to a clean one otherwise.
    """
    p = ctx.lane_params(Lane.CURVE_VELOCITY)
    if ctx.chain is not Chain.SOL:
        return None
    curve = ctx.curve or {}
    progress = _dec(curve.get("progress_pct"))
    if progress is None:
        return None

    # The published predictor is SOL per *swap*, not SOL per minute. The distinction is
    # not pedantic: per-minute rewards a burst of tiny trades, which is exactly what a
    # volume bot produces, while per-swap measures how much real money each participant
    # is committing. Bot-dominated early activity predicts *lower* graduation
    # (docs/research/10-solana-edge-2026.md §8.3e), so the wrong denominator inverts the
    # signal on precisely the launches it most needs to reject. Most implementations get
    # this wrong; ours did too.
    #
    # Per-minute is kept as a fallback so the lane still runs where a caller cannot yet
    # supply a swap count, but it is marked in the payload and held to its own threshold,
    # so a backtest can separate the two populations instead of blending them.
    sol_per_swap = _dec(curve.get("sol_per_swap"))
    if sol_per_swap is None:
        swaps = curve.get("swaps") or curve.get("trade_count")
        sol_in_curve = _dec(curve.get("sol_in_curve"))
        if sol_in_curve is not None and swaps:
            try:
                sol_per_swap = sol_in_curve / Decimal(int(swaps))
            except (ArithmeticError, TypeError, ValueError):
                sol_per_swap = None

    sol_per_min = _dec(curve.get("sol_per_min"))
    basis = "sol_per_swap" if sol_per_swap is not None else "sol_per_min"
    velocity = sol_per_swap if sol_per_swap is not None else sol_per_min
    if velocity is None:
        return None

    lo = _dec(p["min_progress_pct"]) or Decimal(0)
    hi = _dec(p["max_progress_pct"]) or Decimal(100)
    if basis == "sol_per_swap":
        min_velocity = _dec(p.get("min_sol_per_swap")) or Decimal(0)
        # An absolute SOL floor is wrong here and the live scan proved it. pump.fun's
        # curve constants are not constants any more: creators choose a starting market
        # cap, and observed graduation targets ranged from 0.41 SOL to 115 SOL — nearly
        # three orders of magnitude. The same 0.18 SOL/swap is a token racing to
        # graduation on a small curve and a dead token on a large one, so a fixed floor
        # says opposite things about identical behaviour.
        #
        # Normalise against the token's own target instead. The derivation is the same
        # one the absolute number came from: a median graduation is ~457 trades, so the
        # average graduating swap carries 1/457 = 0.0022 of the target. That fraction
        # transfers across curve sizes; the SOL figure does not.
        graduation_sol = _dec(curve.get("graduation_sol"))
        frac = _dec(p.get("min_swap_fraction_of_graduation"))
        if graduation_sol and graduation_sol > 0 and frac:
            min_velocity = graduation_sol * frac
    else:
        min_velocity = _dec(p["min_sol_per_min"]) or Decimal(0)
    max_bundler = _dec(p["max_bundler_pct"])
    min_graded = int(p["min_graded_wallets"])
    window_s = int(p.get("window_s", 60))

    if progress < lo or progress > hi:
        return None
    if velocity < min_velocity:
        return None

    bundler_pct = _measure(ctx.dossier, "bundler_pct")
    if bundler_pct is None or max_bundler is None or bundler_pct >= max_bundler:
        return None  # unknown bot share fails closed

    graded = _graded_wallets(ctx, min_grade=Grade.C)
    if len(graded) < min_graded:
        return None

    velocity_factor = min(1.0, float(velocity) / float(max(min_velocity * 2, Decimal("0.001"))))
    bot_factor = 1.0 - float(bundler_pct / max_bundler) if max_bundler else 0.0
    strength = _clamp(0.55 * velocity_factor + 0.3 * bot_factor + 0.15 * min(1.0, len(graded) / 3.0))

    return Signal(
        signal_id=signal_id_for(Lane.CURVE_VELOCITY, ctx.chain, ctx.token, ctx.now_ms, window_s),
        lane=Lane.CURVE_VELOCITY,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"curve progress {progress}% inside [{lo}, {hi}]",
            f"{velocity} ({basis.replace('_', ' ')}) >= {min_velocity}",
            f"bundler share {bundler_pct}% < {max_bundler}%",
            f"{len(graded)} graded wallets present",
        ],
        wallets=graded,
        entities=_entity_ids(ctx.chain, graded, _conn(ctx)),
        window_s=window_s,
        created_ms=ctx.now_ms,
        payload={
            "progress_pct": str(progress),
            "velocity": str(velocity),
            "velocity_basis": basis,
            "velocity_floor": str(min_velocity),
            "graduation_sol": str(curve.get("graduation_sol"))
            if curve.get("graduation_sol") else None,
            "sol_per_min": str(sol_per_min) if sol_per_min is not None else None,
            "sol_per_swap": str(sol_per_swap) if sol_per_swap is not None else None,
            "sol_in_curve": str(curve.get("sol_in_curve")) if curve.get("sol_in_curve") else None,
            "bundler_pct": str(bundler_pct),
            "graded_wallets": len(graded),
            "evidence": "A: velocity is the strongest graduation predictor (arXiv 2602.14860)",
        },
    )


def sm_trenches(ctx: LaneContext) -> Signal | None:
    """GMGN trenches smart-money preset, re-checked against our own entity graph.

    Smart-money presence is a real but *modest* effect, and the preset counts addresses.
    We re-count as entities, and a rug ratio that EXISTS at or over ``max_rug_ratio``
    refuses -- fresh or stale, since a stale bad number is still a bad number. A stale
    value under the ceiling is read as unavailable (payload ``rug_ratio`` None, basis
    ``"stale"``); only a fresh one is stamped ``"measured"``.

    An UNAVAILABLE rug ratio does not refuse and does not earn strength. MEASURED
    2026-09-21 on the live box: ``rug_ratio`` is not on GMGN token security or token info
    at all (0/4,532 dossiers carried one); it lives on the trenches FEED rows, present
    180/180 on sol (90 exact zeros, median 0.008) and 8/180 on bsc (all 0, 172 null), and
    it moved 0 -> 0.682 on one token within minutes, so a 0 is partly "not scored yet".
    "Block on unknown" was therefore a permanent off switch on bsc, the one chain this
    lane exists for, not a safety check. The rug defence the lane relies on instead is
    the dossier's own blockers -- honeypot, mint/freeze authority, dev_concentration,
    cluster_concentration, already_rugged (rug_history) -> QUARANTINED -- which
    ``engine.decide`` refuses before any size is computed (``tests/test_bsc_lane_inputs.py``
    proves that path holds for every one of them). The 0.3 ceiling is CITED from the
    gmgn-cli smart-money preset (dist/commands/market.js), unvalidated against outcomes.
    """
    p = ctx.lane_params(Lane.SM_TRENCHES)
    window_s = int(p.get("window_s", 300))
    min_smart = int(p["min_smart_degen"])
    min_entities = int(p["min_independent_entities"])
    max_rug = _dec(p["max_rug_ratio"])
    min_liquidity = _min_liquidity_for(p, ctx.chain)

    # Launchpad policy before anything that costs a lookup. A token whose launchpad we
    # cannot name is a manual deploy and needs the higher confluence bar below; see
    # `DEFAULT_PARAMS[Lane.SM_TRENCHES]` for the measurement behind both numbers.
    if bool(p.get("require_launchpad", False)):
        # Attribute access: `token_meta` is a `Token` model, not a dict. A `.get()`
        # here returns nothing for every token and makes the whole book look manual.
        launchpad = str((ctx.token_meta.launchpad if ctx.token_meta else None) or "").strip()
        if not launchpad:
            min_smart = max(min_smart, int(p.get("manual_min_smart_degen", min_smart)))

    # Liquidity first, because it is the cheapest gate and the most predictive one we have
    # (see `DEFAULT_PARAMS[Lane.SM_TRENCHES]` for both measurements). An UNKNOWN liquidity
    # refuses exactly like a low one: this is the number that decides whether we can get
    # out, and "we could not read it" has never been evidence that it is fine.
    # A token still on its bonding curve has no pool, so the POOL floor does not
    # describe it. The curve is the counterparty and it always takes the other side.
    # See the module note on `min_liquidity_usd`: the floor's measurement is about
    # post-migration pools, where thin depth means trapped. MEASURED 2026-09-24 over 6h
    # of confluence: 110 of 165 sol tokens, 45 of 59 bsc and 55 of 72 robinhood carry no
    # migration record, and the registry holds a liquidity number for NONE of the 296.
    #
    # Narrow on purpose: only when the curve is READABLE and carries a positive reserve,
    # because a curve we cannot read is as untradeable as a thin pool; only before
    # migration, because a drained curve leaves the pool as the sole exit; and it waives
    # the POOL floor alone -- smart count, entities, rug ratio, dossier blockers and the
    # sizer's own economic floor are all untouched.
    on_curve = False
    if bool(p.get("waive_liquidity_on_curve", True)):
        migrated = (ctx.token_meta.migrated_ms if ctx.token_meta else None)
        curve = ctx.curve if isinstance(ctx.curve, dict) else {}
        reserve = None
        for key in ("sol_in_curve", "reserve_native", "native_reserve", "virtual_native"):
            reserve = _dec(curve.get(key))
            if reserve is not None:
                break
        on_curve = migrated is None and reserve is not None and reserve > 0

    if min_liquidity > 0 and not on_curve:
        liquidity = _measure(ctx.dossier, "liquidity_usd")
        if liquidity is None or liquidity < min_liquidity:
            return None

    # HOLDERS, straight after liquidity and for the same reason: cheap, measured, and
    # an unknown one is not a pass. See DEFAULT_PARAMS[Lane.SM_TRENCHES] for the
    # holdout. This is the strongest single cut we have found on our own live fills.
    min_holders = int(p.get("min_holder_count", 0) or 0)
    if min_holders > 0:
        holders = _measure(ctx.dossier, "holder_count")
        if holders is None or holders < min_holders:
            return None

    # Launchpads that lose ~30% a fill with almost no winners.
    blocked = {
        str(x).strip().lower()
        for x in (p.get("blocked_launchpads") or [])
        if str(x).strip()
    }
    if blocked:
        # Attribute access, not .get(): `token_meta` is a `Token` model. That exact
        # mistake above once made every token look like a manual deploy.
        lp = str((ctx.token_meta.launchpad if ctx.token_meta else None) or "").strip().lower()
        if lp and lp in blocked:
            return None

    buyers = _net_buyers(
        ctx.recent_buys,
        since_ms=ctx.now_ms - window_s * 1000,
        until_ms=ctx.now_ms,
        min_buy_usd=_dec(p.get("min_buy_usd", 0)) or Decimal(0),
    )
    # Tags ride on the swap rows when the feed supplied them; otherwise fall back to the
    # wallets table. Either way the tag set is per wallet, not per fill.
    row_tags: dict[str, set[str]] = {}
    for row in ctx.recent_buys:
        wallet = str(row.get("wallet") or "")
        if wallet in buyers:
            row_tags.setdefault(wallet, set()).update(_tags(ctx.chain, row, _conn(ctx)))

    smart: list[str] = []
    cohort_ok = ctx.chain in SMART_COHORT_LABEL_CHAINS
    # Both spellings refuse: a bot label is disqualifying whether or not anyone screened it.
    quarantine = {t.value for t in HARD_QUARANTINE_TAGS} | {
        vendor_tag(t.value) for t in HARD_QUARANTINE_TAGS
    }
    for wallet in sorted(buyers):
        tags = row_tags.get(wallet, set())
        if tags & {t.value for t in SMART_TAGS}:
            smart.append(wallet)
            continue
        # GMGN's own cohort spelling, on the chains where it was measured to predict.
        #
        # Matched NAMESPACED and deliberately so. `_tags` keeps the vendor namespace
        # because a bare tag means the tracker screened this wallet and a namespaced one
        # means only that a vendor said so. This route wants the unscreened claim -- that
        # is the population that was measured, and the screen refuses most of it for want
        # of observed swaps, not for anything it found. Asking for `gmgn:smart_degen` by
        # name keeps that opt-in visible instead of quietly widening every other route.
        #
        # It still carries the one part of the screen that is not about sample size: a
        # wallet GMGN calls smart AND calls a sandwich bot is a sandwich bot.
        lowered = {t.strip().lower() for t in tags}
        if cohort_ok and (lowered & SMART_COHORT_VENDOR_TAGS) and not (lowered & quarantine):
            smart.append(wallet)
            continue
        score = _score(ctx.chain, wallet, _conn(ctx))
        if score is not None and str(score.archetype) in {"smart_money", "top_trader"}:
            smart.append(wallet)
    if len(smart) < min_smart:
        return None

    if max_rug is None:
        return None  # a lane whose ceiling is unreadable is misconfigured: fail closed
    # Read the Measure itself, not ``_measure``. That helper folds a STALE measure into
    # the same ``None`` as an UNAVAILABLE one, so a 0.9 whose receipt was 901 s old
    # walked through the gate that refused it at 899 s (round-1 verifier finding; the
    # dossier itself was still inside engine.decide's 300 s budget, so it got sized).
    # The rules, in order:
    #  * a value that EXISTS at or over the ceiling refuses, whatever its basis or age:
    #    a stale bad number is still a bad number;
    #  * a stale value under the ceiling is not evidence of a clean token either -- it is
    #    treated as unavailable (no strength, payload None) and the basis says "stale"
    #    so a reader can tell it from a token GMGN never scored;
    #  * a fresh value under the ceiling passes and is stamped "measured";
    #  * unavailable neither refuses nor earns strength (rule: missing evidence is None
    #    + UNAVAILABLE, never 0, and never a veto in disguise).
    # "Stale" is the Measure's own verdict: receipt older than its freshness budget
    # (``RUG_RATIO_BUDGET_S``, 900 s, on a dyor-built dossier) or a basis of STALE from
    # a provider that answered out of an expired cache. No rug strength term, ever.
    rug_m = getattr(ctx.dossier, "rug_ratio", None) if ctx.dossier is not None else None
    rug_seen = getattr(rug_m, "value", None) if rug_m is not None else None
    if rug_seen is not None and rug_seen >= max_rug:
        return None  # at or over the ceiling refuses, fresh or stale
    rug_known = rug_seen is not None and bool(getattr(rug_m, "known", False))
    rug_stale = rug_known and (
        bool(getattr(rug_m, "stale", False)) or rug_m.basis is EvidenceBasis.STALE
    )
    rug_ratio = rug_seen if rug_known and not rug_stale else None
    rug_basis = "measured" if rug_ratio is not None else ("stale" if rug_stale else "unavailable")
    rug_receipt = getattr(rug_m, "receipt", None) if rug_m is not None else None
    rug_age_s = round(rug_receipt.age_seconds, 1) if rug_receipt is not None else None
    rug_budget_s = int(getattr(rug_m, "freshness_budget_s", 0) or 0) if rug_m is not None else None

    entities = _entities(ctx.chain, smart, _conn(ctx))
    if entities < min_entities:
        return None

    # INVENTED weights (0.35 base, 0.15 smart count, 0.25 entity count): the relative
    # weights are the original, unmeasured ones; the rug-ratio term (0.25 * (1 - rug/max))
    # is gone, because a number that is unavailable on the lane's own chain cannot be
    # allowed to earn or lose strength. The remaining 0.75 is renormalised to 1 so the
    # strength still spans the same [0, 1] range the sizer reads. Strength therefore
    # never depends on the rug ratio at all, measured or not.
    strength = _clamp(
        (
            0.35
            + 0.15 * min(1.0, len(smart) / float(max(1, min_smart * 2)))
            + 0.25 * min(1.0, entities / float(max(1, min_entities * 2)))
        )
        / 0.75
    )
    if rug_stale:
        rug_reason = (
            f"rug ratio {rug_seen} is STALE ({rug_age_s}s old, budget {rug_budget_s}s): under "
            f"the ceiling {max_rug} but not evidence of a clean token; treated as unavailable, "
            "earns no strength (a stale value AT or OVER the ceiling refuses)"
        )
    elif rug_ratio is None:
        rug_reason = (
            f"rug ratio UNAVAILABLE (ceiling {max_rug}): not a refusal and not strength; "
            "the rug defence is the dossier blockers -- honeypot, mint/freeze authority, "
            "dev_concentration, cluster_concentration, already_rugged (rug_history) -> "
            "QUARANTINED -- refused by engine.decide before any size"
        )
    else:
        rug_reason = f"rug ratio {rug_ratio} < {max_rug} (MEASURED; earns no strength)"
    return Signal(
        signal_id=signal_id_for(Lane.SM_TRENCHES, ctx.chain, ctx.token, ctx.now_ms, window_s),
        lane=Lane.SM_TRENCHES,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"{len(smart)} smart wallets in the trenches preset (min {min_smart})",
            f"{entities} independent entities (min {min_entities})",
            rug_reason,
        ],
        wallets=smart,
        entities=_entity_ids(ctx.chain, smart, _conn(ctx)),
        window_s=window_s,
        created_ms=ctx.now_ms,
        payload={
            "smart_wallets": len(smart),
            "entity_count": entities,
            # None when unavailable OR stale: never "0", never "None"-the-string. The
            # basis tells the two apart; the age and budget say why a stale one is stale.
            "rug_ratio": None if rug_ratio is None else str(rug_ratio),
            "rug_ratio_basis": rug_basis,
            "rug_ratio_age_s": rug_age_s,
            "rug_ratio_budget_s": rug_budget_s,
            "max_rug_ratio": str(max_rug),
            "evidence": "A: smart-money presence is a modest positive, also an exit cascade risk",
        },
    )


def migration_fade(ctx: LaneContext) -> Signal | None:
    """Fade the post-migration pump. Short horizon, hard deadline, never hold through.

    Evidence grade A and the strongest base rate we have: 73% of 41,470 migrations traded
    below 40% of the migration price within 20 minutes (MemeTrans, arXiv 2602.13480). The
    signal therefore carries its own exit deadline; any position it opens must be out
    within ``sell_within_s``.
    """
    p = ctx.lane_params(Lane.MIGRATION_FADE)
    sell_within_s = int(p["sell_within_s"])
    max_hold_s = int(p.get("max_hold_s", 1200))

    migrated_ms = _migration_ms(ctx)
    if migrated_ms is None:
        return None
    age_s = (ctx.now_ms - migrated_ms) / 1000.0
    if age_s < 0 or age_s > sell_within_s:
        return None

    decay = 1.0 - 0.3 * (age_s / sell_within_s if sell_within_s else 0.0)
    strength = _clamp(0.73 * decay)
    return Signal(
        signal_id=signal_id_for(Lane.MIGRATION_FADE, ctx.chain, ctx.token, ctx.now_ms, sell_within_s),
        lane=Lane.MIGRATION_FADE,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"migration {age_s:.1f}s ago",
            "73% of migrations trade below 40% of migration price within 20 min",
            f"exit deadline {sell_within_s}s, hard cap {max_hold_s}s",
        ],
        wallets=[],
        entities=[],
        window_s=sell_within_s,
        created_ms=ctx.now_ms,
        payload={
            "bias": "sell",
            "migrated_ms": migrated_ms,
            "age_s": round(age_s, 3),
            "sell_within_s": sell_within_s,
            "max_hold_s": max_hold_s,
            "exit_deadline_ms": ctx.now_ms + sell_within_s * 1000,
            "never_hold_through_migration": True,
            "evidence": "A: MemeTrans arXiv 2602.13480, n=41,470",
        },
    )


def kol_fade(ctx: LaneContext) -> Signal | None:
    """Follow the few callers with measured positive expectancy; fade the rest.

    The default posture is *observe*: a caller we have not measured produces no signal at
    all. 80% of KOL-promoted coins are down >=70% after a week, so "a KOL called it" is on
    its own an exit trigger, not an entry.
    """
    p = ctx.lane_params(Lane.KOL_FADE)
    min_trades = int(p["min_caller_trades"])
    min_expectancy = _num(p["min_caller_expectancy"], 0.0)
    max_call_age_s = int(p.get("max_call_age_s", 300))
    window_s = int(p.get("window_s", 300))

    caller = ctx.caller or _caller_for_token(ctx)
    if not caller:
        return None
    mode = str(caller.get("mode") or "observe").lower()
    calls = int(caller.get("calls") or 0)
    expectancy = _num(caller.get("expectancy"), 0.0) if caller.get("expectancy") is not None else None
    call_ms = caller.get("call_ms") or caller.get("last_call_ms")
    if call_ms is not None:
        age_s = (ctx.now_ms - int(call_ms)) / 1000.0
        if age_s < 0 or age_s > max_call_age_s:
            return None
    else:
        age_s = None
    name = str(caller.get("caller_id") or caller.get("display_name") or "unknown")

    if mode == "follow":
        if expectancy is None or expectancy <= min_expectancy or calls < min_trades:
            return None
        strength = _clamp(0.45 + min(0.4, expectancy) + 0.1 * min(1.0, calls / float(max(1, min_trades * 3))))
        bias, reason = "buy", f"caller {name} expectancy {expectancy:+.3f} over {calls} calls"
        payload_extra: dict[str, Any] = {"exit_trigger": False}
    elif mode == "fade":
        strength = _clamp(0.5 + 0.2 * min(1.0, calls / float(max(1, min_trades * 2))))
        bias, reason = "sell", f"caller {name} is on fade ({calls} measured calls)"
        payload_extra = {"exit_trigger": True}
    else:
        return None  # observe / ignore: watch, do not act

    return Signal(
        signal_id=signal_id_for(Lane.KOL_FADE, ctx.chain, ctx.token, ctx.now_ms, window_s),
        lane=Lane.KOL_FADE,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[reason, f"posture={mode}", f"call age {age_s}s" if age_s is not None else "call age unknown"],
        wallets=[],
        entities=[],
        window_s=window_s,
        created_ms=ctx.now_ms,
        payload={
            "bias": bias,
            "caller_id": caller.get("caller_id"),
            "platform": caller.get("platform"),
            "mode": mode,
            "calls": calls,
            "expectancy": expectancy,
            "call_age_s": age_s,
            "evidence": "B/C: caller reputation measured in-house; KOL calls are an exit trigger",
            **payload_extra,
        },
    )


def listing_pop(ctx: LaneContext) -> Signal | None:
    """An exchange listing announcement we heard fast enough to act on.

    Evidence grade B (event studies only), and the edge is entirely latency: past
    ``max_latency_s`` the pop has been arbitraged and we would be the exit liquidity.
    """
    p = ctx.lane_params(Lane.LISTING_POP)
    max_latency_s = int(p["max_latency_s"])
    listing = ctx.extras.get("listing") or _event_payload(ctx, EventKind.ALPHA_LISTING)
    if not listing:
        return None
    # Only the venue's own publication time will do. Falling back to ``ts_ms`` — the row's
    # own insertion time — made an untimed listing read as "announced zero seconds ago",
    # which is the single highest-strength input this lane can receive. The lane would
    # then score near maximum on a latency nobody had measured, and it would do so most
    # confidently for exactly the sources that failed to report a timestamp.
    announced_ms = listing.get("announced_ms")
    if announced_ms is None:
        return None
    latency_s = (ctx.now_ms - int(announced_ms)) / 1000.0
    if latency_s < 0 or latency_s > max_latency_s:
        return None

    strength = _clamp(0.4 + 0.5 * (1.0 - latency_s / max_latency_s if max_latency_s else 0.0))
    venue = str(listing.get("venue") or listing.get("exchange") or "unknown")
    return Signal(
        signal_id=signal_id_for(Lane.LISTING_POP, ctx.chain, ctx.token, ctx.now_ms, max_latency_s),
        lane=Lane.LISTING_POP,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[f"{venue} listing announced {latency_s:.1f}s ago (cap {max_latency_s}s)"],
        wallets=[],
        entities=[],
        window_s=max_latency_s,
        created_ms=ctx.now_ms,
        payload={
            "venue": venue,
            "announced_ms": int(announced_ms),
            "latency_s": round(latency_s, 3),
            "headline": listing.get("headline"),
            "evidence": "B: event studies only; the edge is latency, nothing else",
        },
    )


# --------------------------------------------------------------------------------------
# pons-robinhood: wait out the anti-sniper tax, then verify the book is actually clean
# --------------------------------------------------------------------------------------
#
# The operator's chosen entry method on Pons is "wait out the anti-sniper tax". This block
# builds exactly that, on what was MEASURED rather than on the parameter's name, because
# the two disagree:
#
# * ``PonsV2LaunchFactory`` (0x7ed598bc...ec7e, chain 4663) answers ``snipeTaxStartBps()``
#   = 0x26ac = 9900 and ``snipeTaxSeconds()`` = 3 (MEASURED 2026-09-21 by eth_call from the
#   local machine; 75/75 sampled curves carry the same pair). The decay is NOT linear:
#   ``currentSnipeTaxBps`` read at every block of a live launch, and the toll recovered
#   from 913k real ``CurveBuy`` events, both give 9900 / 618 / 19 / 0 bps at 0 / 1 / 2 / 3+
#   whole seconds since ``launchedAt`` (docs/research/line4-antisniper-tax-pons.md section
#   1, reproduced in REFUTATION-pons-tax-entry.md section 6, re-sampled in
#   tests/test_pons_tax_entry.py). ``CurveState.snipe_tax_bps_at`` in kaiba/ingest assumes
#   9900 / 6600 / 3300 and is wrong by 10.7x at t=1 s; it is not this file's to fix, so the
#   schedule is carried here and the defect is reported upstream.
# * The wait alone does not buy a clean book. MEASURED: 58.7% of first-second outside buys
#   pay ZERO snipe tax -- the deployer can waive the toll for a wallet set -- and tokens
#   with such an exempt buyer return -9.18% / -7.00% (train / held out) against -2.78% /
#   -1.59% without (REFUTATION section 4). The 9900 rung is essentially never paid
#   voluntarily (~200 of 141k early buys), so an outside buy in the launch second is that
#   exemption in disguise.
# * pons-robinhood as previously written is MEASURED negative (10 signals: 8 stop-outs,
#   strength anti-correlated with outcome). So the gates below are *exclusions* that
#   replicated out of sample, the strength is rebuilt from the two features that predicted
#   graduation (early distinct buyers and early independent entities), and it is capped so
#   an unproven lane cannot claim the size ladder's upper rungs.

#: PonsV2LaunchFactory on Robinhood Chain (4663). Source of the venue-wide tax defaults.
PONS_FACTORY = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"

#: MEASURED 2026-09-21: ``snipeTaxStartBps()`` on the factory and on 75/75 sampled curves.
PONS_SNIPE_TAX_START_BPS = 9900
#: MEASURED 2026-09-21: ``snipeTaxSeconds()`` on the factory and on 75/75 sampled curves.
PONS_SNIPE_TAX_SECONDS = 3
#: MEASURED: the tax by whole elapsed second for the (9900, 3) configuration. Two
#: independent methods agree (``currentSnipeTaxBps`` per block; toll recovered from real
#: buys). Convex, not linear. Past the last rung it is zero.
PONS_SNIPE_TAX_RUNGS_BPS: dict[int, int] = {0: 9900, 1: 618, 2: 19}
#: MEASURED: ``feeBps()`` = 100 on 250/250 sampled curves. The ``fee`` word of ``CurveBuy``
#: carries protocol fee + snipe tax, so the snipe toll a buy paid is fee_bps minus this.
PONS_PROTOCOL_FEE_BPS = 100

#: The lane's tax-entry parameters. Kept beside the lane rather than in ``DEFAULT_PARAMS``
#: because this block is their only owner; ``config/risk.yaml`` and caller overrides still
#: win through :meth:`LaneContext.lane_params`.
PONS_TAX_DEFAULTS: dict[str, Any] = {
    "max_entry_tax_bps": 0,
    "post_tax_window_s": 10,
    "min_post_tax_buy_wei": 10**15,
    "early_window_s": 300,
    "early_buyers_saturation": 20,
    "early_entities_saturation": 10,
    "dev_atomic_veto_wei": 3 * 10**16,
    "veto_demand_window_s": 2,
    "max_strength": 0.79,
    "unverified_strength_cap": 0.69,
}

#: Where each number above comes from. Every INVENTED entry says what would settle it;
#: ``tests/test_pons_tax_entry.py`` enforces the labels.
PONS_TAX_PROVENANCE: dict[str, str] = {
    "max_entry_tax_bps": (
        "DERIVED: 0 bps = enter only once the toll is gone, which is the operator's chosen "
        "rule. MEASURED residuals are 618 bps at t=1 s (a whole 2.3-6.3% round trip) and "
        "19 bps at t=2 s; MEASURED detect latency p50 >= 4.7 s means no entry inside the "
        "3 s window is reachable anyway, so a higher threshold admits only the bundled "
        "first-second book and buys nothing."
    ),
    "post_tax_window_s": (
        "INVENTED: 10 s (~100 blocks at the MEASURED 10 blocks/s) of post-tax book to count "
        "buyers in before acting. Settled by an entry-age dose-response on the tape, held "
        "out by time; the backtest in this line reports it."
    ),
    "min_post_tax_buy_wei": (
        "INVENTED: 0.001 ETH (~$2.7) dust floor for counting a buyer. Settled by the "
        "distribution of outside buy sizes on the tape (MEASURED median taxed buy 0.0179 ETH)."
    ),
    "early_window_s": (
        "MEASURED elsewhere: early distinct buyers and independent entities in the first "
        "300 s were the strongest graduation predictors in the surviving research (AUC "
        "0.82-0.92); the window is theirs."
    ),
    "early_buyers_saturation": (
        "DERIVED: 20 distinct outside buyers in the first 300 s saturates the term. On the "
        "local tape graduated tokens average 42.6 such buyers against 13.7 overall, and "
        "29% of traded tokens reach 10."
    ),
    "early_entities_saturation": (
        "INVENTED: 10 independent entities saturates the term. Robinhood has no "
        "entity_members rows locally, so entities equal addresses today; settled once "
        "clustering runs on this chain and the AUC is re-measured on entities."
    ),
    "dev_atomic_veto_wei": (
        "MEASURED: dev atomic buy < 0.03 ETH AND no outside demand in the first 2 s -> "
        "-3.65% train / -6.28% held out (t=-14.83, n=1,097), the one exclusion that got "
        "worse out of sample (line4 section 4; REFUTATION 5.1: survives a 1 s feature lag)."
    ),
    "veto_demand_window_s": "MEASURED: the 2 s demand window of the same veto screen.",
    "max_strength": (
        "DERIVED: 0.79 -> score 79 -> the ladder's bottom rung (25% of lane max). The lane "
        "is MEASURED negative; until it shows positive held-out expectancy it may not "
        "reach the 80/90/95 rungs that double and quadruple size."
    ),
    "unverified_strength_cap": (
        "DERIVED: 0.69 -> score 69 -> below the ladder's lowest rung, i.e. zero size. "
        "Applied whenever a clean-book input (creator, launch tx) is UNAVAILABLE, so the "
        "shadow record accrues but an unverified book is never sized."
    ),
}


def pons_snipe_tax_bps(
    elapsed_s: int,
    *,
    start_bps: int = PONS_SNIPE_TAX_START_BPS,
    seconds: int = PONS_SNIPE_TAX_SECONDS,
) -> int:
    """Anti-sniper tax in bps at a whole number of seconds since ``launchedAt``.

    Exact for the measured ``(9900, 3)`` configuration. Any other positive configuration
    has an unmeasured shape, so inside its window the answer is the *full* starting toll:
    an unknown decay must not read as a cheap one. A zero configuration is no tax.
    """
    if start_bps <= 0 or seconds <= 0:
        return 0
    if elapsed_s < 0:
        return int(start_bps)
    if elapsed_s >= seconds:
        return 0
    if start_bps == PONS_SNIPE_TAX_START_BPS and seconds == PONS_SNIPE_TAX_SECONDS:
        return PONS_SNIPE_TAX_RUNGS_BPS[int(elapsed_s)]
    return int(start_bps)


class PonsEntryCheck(BaseModel):
    """What the tax-wait rule saw. A ``None`` field is UNAVAILABLE, never zero."""

    ok: bool
    refusals: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)
    age_s: float | None = None
    elapsed_s: int | None = None
    tax_now_bps: int | None = None
    tax_basis: str | None = None
    tax_zero_age_s: float | None = None
    launched_at_ms: int | None = None
    tax_zero_ms: int | None = None
    creator: str | None = None
    launch_tx: str | None = None
    dev_atomic_wei: int | None = None
    dev_buys_post_tax: int = 0
    exempt_buys: int = 0
    exempt_basis: str | None = None
    outside_demand_early_wei: int | None = None
    post_tax_buyers: list[str] = Field(default_factory=list)
    post_tax_entities: int | None = None
    early_buyers: list[str] = Field(default_factory=list)
    early_entities: int | None = None
    known_bundlers: list[str] = Field(default_factory=list)


def _pons_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _pons_native_wei(value: Any) -> int | None:
    """``amount_native`` as integer wei.

    MEASURED on the live box 2026-09-21: provider-sourced robinhood swap rows carry
    ``amount_native`` as a decimal ETH string (``'0.07128'``) next to the curve reader's
    integer wei, and ``_net_buyers`` raises ``ValueError`` on the former, which would drop
    the whole lane for that token. A value with a fractional part is native units on an
    EVM chain -- wei is never fractional -- so it is scaled; an integer is already wei.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    text = str(value).strip()
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if dec != dec.to_integral_value():
        return int(dec * (10**18))
    return int(dec)


def _pons_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The context's rows with ``amount_native`` normalised to wei, for helpers that ``int()`` it."""
    out: list[dict[str, Any]] = []
    for row in rows:
        wei = _pons_native_wei(row.get("amount_native"))
        if wei is None or str(row.get("amount_native")) == str(wei):
            out.append(row)
        else:
            out.append({**row, "amount_native": wei})
    return out


def _pons_tax_schedule(ctx: LaneContext, created_ms: int) -> tuple[int, int, int, str]:
    """``(launched_at_ms, start_bps, seconds, basis)``.

    A curve read (``curve_from_state`` emits ``launched_at_s`` and both tax fields) wins.
    Otherwise the launch block's timestamp -- which is what ``tokens.created_ms`` holds on
    this chain, MEASURED equal to ``launchedAt()`` -- with the factory's measured defaults.
    """
    curve = ctx.curve if isinstance(ctx.curve, dict) else {}
    launched = _pons_int(curve.get("launched_at_s"))
    start = _pons_int(curve.get("snipe_tax_start_bps"))
    seconds = _pons_int(curve.get("snipe_tax_seconds"))
    if launched and launched > 0 and start is not None and seconds is not None:
        return launched * 1000, start, seconds, "curve_read"
    return int(created_ms), PONS_SNIPE_TAX_START_BPS, PONS_SNIPE_TAX_SECONDS, "factory_default"


def _pons_known_bundlers(ctx: LaneContext, wallets: list[str]) -> list[str]:
    """Wallets already known as bundlers: bundle members on this token, tagged wallets, or
    a bundler archetype in ``wallet_scores``. Read-only, never raises."""
    conn = _conn(ctx)
    if conn is None or not wallets:
        return []
    flagged: set[str] = set()
    try:
        rows = fetch_all(
            conn,
            "SELECT address FROM token_bundle_members WHERE chain=? AND token=? "
            "AND role IN ('bundler', 'creator')",
            (ctx.chain.value, ctx.token),
        )
        flagged.update(str(r["address"]).lower() for r in rows)
    except sqlite3.Error:
        pass
    for wallet in wallets:
        if wallet in flagged:
            continue
        try:
            row = fetch_one(
                conn,
                "SELECT tags_json FROM wallets WHERE chain=? AND address=?",
                (ctx.chain.value, wallet),
            )
            tags = {str(t) for t in (jload(row["tags_json"], []) or [])} if row else set()
            if WalletTag.BUNDLER.value in tags:
                flagged.add(wallet)
                continue
            score = fetch_one(
                conn,
                "SELECT archetype FROM wallet_scores WHERE chain=? AND address=?",
                (ctx.chain.value, wallet),
            )
            if score and str(score["archetype"] or "").lower() == "bundler":
                flagged.add(wallet)
        except sqlite3.Error:
            continue
    return sorted(w for w in wallets if w in flagged)


def pons_entry_check(ctx: LaneContext, p: dict[str, Any] | None = None) -> PonsEntryCheck:
    """The tax-wait entry rule as a pure function of the context.

    Refuses: an unknown launch time; an entry before the toll has decayed to
    ``max_entry_tax_bps``; an entry younger than ``post_tax_window_s`` past tax-zero; a dev
    buy after tax-zero; a tax-exempt (deployer-allowlisted) outside buyer inside the tax
    window; a known bundler among the post-tax buyers; the dead-launch veto. Whatever it
    could not verify is listed in ``unverified`` and caps the strength below the ladder.
    """
    params = {**PONS_TAX_DEFAULTS, **(p if p is not None else ctx.lane_params(Lane.PONS_ROBINHOOD))}
    max_tax = int(params["max_entry_tax_bps"])
    window_s = float(params["post_tax_window_s"])
    min_wei = int(params["min_post_tax_buy_wei"])
    early_s = int(params["early_window_s"])
    veto_wei = int(params["dev_atomic_veto_wei"])
    veto_window_s = int(params["veto_demand_window_s"])

    meta = ctx.token_meta
    created_ms = meta.created_ms if meta else None
    if created_ms is None:
        return PonsEntryCheck(ok=False, refusals=["launch_time_unknown"])
    launched_at_ms, start_bps, seconds, basis = _pons_tax_schedule(ctx, int(created_ms))
    age_s = (ctx.now_ms - launched_at_ms) / 1000.0
    elapsed_s = (ctx.now_ms - launched_at_ms) // 1000
    tax_now = pons_snipe_tax_bps(elapsed_s, start_bps=start_bps, seconds=seconds)
    tax_zero_ms = launched_at_ms + max(seconds, 0) * 1000
    tax_zero_age_s = (ctx.now_ms - tax_zero_ms) / 1000.0

    creator = ((meta.creator or "").lower() or None) if meta else None
    launch_tx = None
    if meta and isinstance(meta.meta, dict):
        sig = meta.meta.get("signature") or meta.meta.get("launch_tx")
        launch_tx = str(sig).lower() if sig else None

    check = PonsEntryCheck(
        ok=False,
        age_s=round(age_s, 3),
        elapsed_s=int(elapsed_s),
        tax_now_bps=int(tax_now),
        tax_basis=basis,
        tax_zero_age_s=round(tax_zero_age_s, 3),
        launched_at_ms=launched_at_ms,
        tax_zero_ms=tax_zero_ms,
        creator=creator,
        launch_tx=launch_tx,
    )
    if age_s < 0:
        check.refusals.append("launch_in_the_future")
        return check
    if tax_now > max_tax:
        check.refusals.append(f"pre_decay_tax_{tax_now}bps")
    if tax_zero_age_s < window_s:
        check.refusals.append("post_tax_window_open")
    if creator is None:
        check.unverified.append("creator_unknown")
    if launch_tx is None:
        check.unverified.append("launch_tx_unknown")

    dev_atomic: int | None = None
    dev_post_tax = 0
    exempt = 0
    exempt_exact = False
    outside_early_wei = 0
    post_tax: dict[str, int] = {}
    early: dict[str, int] = {}
    for row in ctx.recent_buys:
        if not _is_decoded_buy(row):
            # Same rule as `trusted_copy`: only an explicitly decoded buy is a purchase.
            # This loop attributes supply to early buyers and to the dev on robinhood, so
            # an unknown side counted here credits a purchase nobody has evidence of.
            continue
        wallet = str(row.get("wallet") or "").lower()
        if not wallet:
            continue
        ts = _pons_int(row.get("ts_ms"))
        amount = _pons_native_wei(row.get("amount_native")) or 0
        if ts is None or ts > ctx.now_ms:
            continue
        tx = str(row.get("tx") or "").lower() or None
        if launch_tx is not None and tx == launch_tx:
            dev_atomic = (dev_atomic or 0) + amount
            continue
        elapsed = (ts - launched_at_ms) // 1000
        if elapsed < 0:
            continue
        recipient = str(row.get("recipient") or "").lower() or None
        if creator is not None and (wallet == creator or recipient == creator):
            if ts >= tax_zero_ms:
                dev_post_tax += 1
            continue
        scheduled = pons_snipe_tax_bps(elapsed, start_bps=start_bps, seconds=seconds)
        if scheduled > 0:
            fee = _pons_int(row.get("fee"))
            if fee is not None and amount > 0:
                exempt_exact = True
                snipe_paid_bps = fee * 10_000 // amount - PONS_PROTOCOL_FEE_BPS
                if snipe_paid_bps < scheduled // 2:
                    exempt += 1
            elif elapsed == 0:
                exempt += 1
        if elapsed <= veto_window_s:
            outside_early_wei += amount
        if amount >= min_wei:
            if ts >= tax_zero_ms:
                post_tax[wallet] = post_tax.get(wallet, 0) + amount
            if ts < launched_at_ms + early_s * 1000:
                early[wallet] = early.get(wallet, 0) + amount

    check.dev_atomic_wei = dev_atomic
    check.dev_buys_post_tax = dev_post_tax
    check.exempt_buys = exempt
    check.exempt_basis = "fee_word" if exempt_exact else "elapsed0_proxy"
    check.outside_demand_early_wei = outside_early_wei
    check.post_tax_buyers = sorted(post_tax)
    check.early_buyers = sorted(early)
    conn = _conn(ctx)
    check.post_tax_entities = _entities(ctx.chain, check.post_tax_buyers, conn) if post_tax else 0
    check.early_entities = _entities(ctx.chain, check.early_buyers, conn) if early else 0
    check.known_bundlers = _pons_known_bundlers(ctx, check.post_tax_buyers)

    if dev_post_tax:
        check.refusals.append("dev_bought_after_tax_zero")
    if exempt:
        check.refusals.append("tax_exempt_buyer_in_window")
    if check.known_bundlers:
        check.refusals.append("known_bundler_in_window")
    if dev_atomic is None:
        check.unverified.append("dev_atomic_buy_unavailable")
    elif dev_atomic < veto_wei and outside_early_wei == 0:
        check.refusals.append("veto_dead_launch")
    check.ok = not check.refusals
    return check


def pons_robinhood(ctx: LaneContext) -> Signal | None:
    """Pons launches on Robinhood Chain, entered only once the anti-sniper tax is gone and
    only when the post-tax book verifiably holds no dev, no exempt bundle and no known
    bundler.

    Strength is rebuilt from the two features that predicted graduation in the surviving
    research (early distinct buyers, early independent entities) and is capped at
    ``max_strength`` so a lane with MEASURED negative edge cannot claim the size ladder's
    upper rungs; anything the check could not verify caps it below the ladder entirely.
    Shadow-only; nothing here changes the lane mode.
    """
    p = {**PONS_TAX_DEFAULTS, **ctx.lane_params(Lane.PONS_ROBINHOOD)}
    if ctx.chain is not Chain.ROBINHOOD:
        return None
    launchpad = (ctx.token_meta.launchpad if ctx.token_meta else None) or ""
    if launchpad and "pons" not in launchpad.lower():
        return None

    max_age_s = int(p["max_age_s"])
    min_entities = int(p["min_entities"])
    min_liquidity = _dec(p["min_liquidity_usd"]) or Decimal(0)

    check = pons_entry_check(ctx, p)
    if not check.ok or check.age_s is None:
        return None
    age_s = check.age_s
    if age_s > max_age_s:
        return None

    liquidity = _measure(ctx.dossier, "liquidity_usd")
    if liquidity is None or liquidity < min_liquidity:
        return None

    # Conviction is counted only after tax-zero: the point of waiting is that these buyers
    # met a book the snipers were priced out of, and the check above has just verified
    # that the dev and the allowlisted wallets are not among them.
    buyers = _net_buyers(
        _pons_rows(ctx.recent_buys),
        since_ms=int(check.tax_zero_ms or 0),
        until_ms=ctx.now_ms,
        min_buy_usd=_dec(p.get("min_buy_usd", 0)) or Decimal(0),
    )
    excluded = set(check.known_bundlers)
    if check.creator:
        excluded.add(check.creator)
    wallets = sorted(w for w in buyers if w.lower() not in excluded)
    if len(wallets) < min_entities:
        return None
    entities = _entities(ctx.chain, wallets, _conn(ctx))
    if entities < min_entities:
        return None

    early_b = len(check.early_buyers)
    early_e = int(check.early_entities or 0)
    sat_b = max(1, int(p["early_buyers_saturation"]))
    sat_e = max(1, int(p["early_entities_saturation"]))
    components = {
        "base": 0.30,
        "early_buyers": round(0.25 * min(1.0, early_b / sat_b), 4),
        "early_entities": round(0.25 * min(1.0, early_e / sat_e), 4),
        "post_tax_entities": round(0.10 * min(1.0, entities / float(max(1, min_entities * 2))), 4),
        "freshness": round(0.10 * (1.0 - age_s / max_age_s if max_age_s else 0.0), 4),
    }
    raw = _clamp(sum(components.values()))
    cap = float(p["max_strength"])
    if check.unverified:
        cap = min(cap, float(p["unverified_strength_cap"]))
    strength = _clamp(min(raw, cap))

    return Signal(
        signal_id=signal_id_for(Lane.PONS_ROBINHOOD, ctx.chain, ctx.token, ctx.now_ms, max_age_s),
        lane=Lane.PONS_ROBINHOOD,
        chain=ctx.chain,
        token=ctx.token,
        strength=strength,
        reasons=[
            f"snipe tax {check.tax_now_bps} bps at t={check.elapsed_s}s ({check.tax_basis}); "
            f"tax-zero {check.tax_zero_age_s:.0f}s ago",
            f"post-tax book: {entities} independent entities bought (min {min_entities}); "
            f"dev buys 0, exempt buys 0, known bundlers 0",
            f"early window: {early_b} outside buyers / {early_e} entities in first "
            f"{int(p['early_window_s'])}s",
            f"token age {age_s:.0f}s (cap {max_age_s}s)",
            f"liquidity ${liquidity} >= ${min_liquidity}",
            f"strength {strength} (raw {raw}, cap {cap})",
        ]
        + [f"UNVERIFIED: {u}" for u in check.unverified],
        wallets=wallets,
        entities=_entity_ids(ctx.chain, wallets, _conn(ctx)),
        window_s=max_age_s,
        created_ms=ctx.now_ms,
        payload={
            "entity_count": entities,
            "age_s": round(age_s, 3),
            "liquidity_usd": str(liquidity),
            "launchpad": launchpad or None,
            "tax": {
                "now_bps": check.tax_now_bps,
                "elapsed_s": check.elapsed_s,
                "basis": check.tax_basis,
                "zero_age_s": check.tax_zero_age_s,
                "schedule_bps": dict(PONS_SNIPE_TAX_RUNGS_BPS),
            },
            "book": {
                "post_tax_buyers": len(check.post_tax_buyers),
                "post_tax_entities": check.post_tax_entities,
                "early_buyers": early_b,
                "early_entities": early_e,
                "dev_atomic_wei": check.dev_atomic_wei,
                "dev_buys_post_tax": check.dev_buys_post_tax,
                "exempt_buys": check.exempt_buys,
                "exempt_basis": check.exempt_basis,
                "outside_demand_early_wei": check.outside_demand_early_wei,
                "known_bundlers": check.known_bundlers,
            },
            "strength_components": components,
            "strength_raw": raw,
            "strength_cap": cap,
            "unverified": list(check.unverified),
            "evidence": (
                "C: lane MEASURED negative at every reachable entry (line4, REFUTATION); "
                "gates are held-out exclusions, strength is capped, no in-house positive base rate"
            ),
        },
    )


# --------------------------------------------------------------------------------------
# lane inputs that come from the database rather than the context
# --------------------------------------------------------------------------------------


def _graded_wallets(ctx: LaneContext, *, min_grade: Grade = Grade.C) -> list[str]:
    """Graded wallets visible on this token: dossier list plus anyone in ``recent_buys``."""
    found: set[str] = set()
    if ctx.dossier:
        found.update(ctx.dossier.graded_wallets or [])
    for row in ctx.recent_buys:
        wallet = str(row.get("wallet") or "")
        if not wallet or wallet in found:
            continue
        score = _score(ctx.chain, wallet, _conn(ctx))
        if score is not None and GRADE_POINTS.get(score.grade, 0) >= GRADE_POINTS[min_grade]:
            found.add(wallet)
    return sorted(found)


def _migration_ms(ctx: LaneContext) -> int | None:
    if ctx.extras.get("migration_ms"):
        return int(ctx.extras["migration_ms"])
    event = ctx.extras.get("migration") or _event_payload(ctx, EventKind.TOKEN_MIGRATED)
    if event and (event.get("migrated_ms") or event.get("ts_ms")):
        return int(event.get("migrated_ms") or event["ts_ms"])
    if ctx.token_meta and ctx.token_meta.migrated_ms:
        return int(ctx.token_meta.migrated_ms)
    return None


def _event_payload(ctx: LaneContext, kind: EventKind) -> dict[str, Any] | None:
    """Newest event of ``kind`` for this token, payload merged with its timestamp."""
    if ctx.conn is None:
        return None
    try:
        rows = fetch_all(
            ctx.conn,
            "SELECT ts_ms, payload FROM events WHERE kind=? AND subject=? ORDER BY id DESC LIMIT 1",
            (kind.value, ctx.token),
        )
    except sqlite3.Error:
        return None
    if not rows:
        return None
    payload = jload(rows[0]["payload"], {}) or {}
    payload.setdefault("ts_ms", rows[0]["ts_ms"])
    return payload


def _caller_for_token(ctx: LaneContext) -> dict[str, Any] | None:
    """Newest call on this token joined to that caller's measured reputation."""
    if ctx.conn is None:
        return None
    try:
        row = fetch_one(
            ctx.conn,
            "SELECT c.platform, c.caller_id, c.display_name, c.calls, c.expectancy, c.mode, "
            "       cc.ts_ms AS call_ms "
            "FROM caller_calls cc JOIN callers c "
            "  ON c.platform = cc.platform AND c.caller_id = cc.caller_id "
            "WHERE cc.chain=? AND cc.token=? ORDER BY cc.ts_ms DESC LIMIT 1",
            (ctx.chain.value, ctx.token),
        )
    except sqlite3.Error:
        return None
    return row


# --------------------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------------------

LANES: dict[Lane, Callable[[LaneContext], Signal | None]] = {
    Lane.CONFLUENCE_5: confluence_5,
    Lane.TRUSTED_COPY: trusted_copy,
    Lane.CURVE_VELOCITY: curve_velocity,
    Lane.SM_TRENCHES: sm_trenches,
    Lane.MIGRATION_FADE: migration_fade,
    Lane.KOL_FADE: kol_fade,
    Lane.LISTING_POP: listing_pop,
    Lane.PONS_ROBINHOOD: pons_robinhood,
}


def chain_enabled(lane: Lane, chain: Chain) -> bool:
    """Whether ``config/risk.yaml`` lets this lane look at this chain at all."""
    try:
        allowed = get_risk().lane(lane).chains
    except Exception:
        return True
    return not allowed or chain in allowed


def evaluate_all(ctx: LaneContext) -> list[Signal]:
    """Run every lane that is configured for this chain, strongest signal first.

    A lane that raises is logged and skipped: one broken evaluator must not silence the
    other seven. Lane *mode* is not consulted here — an OFF lane still produces signals so
    that the shadow record keeps accruing; :mod:`kaiba.execution.engine` is where OFF turns
    into a SKIP decision.
    """
    out: list[Signal] = []
    for lane, fn in LANES.items():
        if not chain_enabled(lane, ctx.chain):
            continue
        try:
            signal = fn(ctx)
        except Exception:  # noqa: BLE001 - one bad lane must not break the pass
            log.exception("lane %s raised on %s:%s", lane.value, ctx.chain.value, ctx.token)
            continue
        if signal is not None:
            out.append(signal)
    return sorted(out, key=lambda s: (-s.strength, s.lane.value))


def record(signal: Signal, conn: sqlite3.Connection | None = None) -> bool:
    """Persist a signal and announce it. Returns False if this bucket already fired."""
    c = conn or get_conn()
    existing = fetch_one(c, "SELECT signal_id FROM signals WHERE signal_id=?", (signal.signal_id,))
    if existing:
        return False
    c.execute(
        "INSERT INTO signals (signal_id, lane, chain, token, strength, reasons_json, wallets_json, "
        "entities_json, window_s, created_ms, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            signal.signal_id,
            signal.lane.value,
            signal.chain.value,
            signal.token,
            signal.strength,
            jdump(signal.reasons),
            jdump(signal.wallets),
            jdump(signal.entities),
            signal.window_s,
            signal.created_ms,
            jdump(signal.payload),
        ),
    )
    emit(
        EventKind.SIGNAL_FIRED,
        {
            "signal_id": signal.signal_id,
            "lane": signal.lane.value,
            "strength": signal.strength,
            "reasons": signal.reasons,
        },
        chain=signal.chain,
        subject=signal.token,
        dedupe_key=f"signal:{signal.signal_id}",
        conn=c,
    )
    return True


def evaluate_and_record(ctx: LaneContext, conn: sqlite3.Connection | None = None) -> list[Signal]:
    """Evaluate every lane and persist whatever is new. Returns the newly recorded signals."""
    c = conn or ctx.conn or get_conn()
    return [s for s in evaluate_all(ctx) if record(s, c)]


def signal_from_row(row: dict[str, Any]) -> Signal:
    return Signal(
        signal_id=row["signal_id"],
        lane=Lane(row["lane"]),
        chain=Chain(row["chain"]),
        token=row["token"],
        strength=row["strength"],
        reasons=jload(row["reasons_json"], []),
        wallets=jload(row["wallets_json"], []),
        entities=jload(row["entities_json"], []),
        window_s=row["window_s"],
        created_ms=row["created_ms"],
        payload=jload(row["payload_json"], {}),
    )
