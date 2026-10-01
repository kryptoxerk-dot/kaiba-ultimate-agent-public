"""Discovery radar: find the next venue before a human has to.

Why this module exists
----------------------

Every venue this system trades was added by hand. pump.fun was hardcoded at the start.
Robinhood Chain and Pons cost a 24-hour research agent. StonkFun cost a research agent
plus a build agent and was found roughly seven weeks after it started trading, by which
time it was 23.9% of the Solana launchpad fee pool. Nothing in the tree would have
noticed any of them: `alpha_signals` has nine source families keyed on tokens, domains,
governance spaces and repos, and not one of them watches a launchpad, a DEX factory or a
chain list.

This is one module, not four features. It sweeps a small number of surfaces for things
that are NEW *relative to what it has already reported*, ranks them by measured money,
and refuses to call anything actionable that we cannot read.

What it does not do, and why
----------------------------

**No NFT anything.** The NFT survey measured the case and it closes: every NFT venue
DefiLlama tracks did $120,806/day in fees ex-Fragment, against StonkFun alone at
$819,168/day; zero new NFT venues reached $1M of 30-day volume in 730 days; and across
24 genuinely new chains exactly one ever got an NFT marketplace, arriving 14 days *after*
its DEX. So there is no NFT feed, no NFT threshold and no NFT code here. What there is
instead is a rule that never filters on category at all: an NFT marketplace that ever
mattered would clear the same measured-volume floor a launchpad clears, and would be
seen by the same three requests. That is the survey's own recommendation.

**No airdrop farming and no participation of any kind.** Farming resolved against
farmers: 0 of 652 DefiLlama rows and 0 of 119 live opportunities cleared a $25 EV floor
at $60/h, $30/h *or* $0/h. Nothing here routes to `ev.py` or `build_plan`. Airdrop
*discovery* survives as one boolean on a chain row — "this chain holds real money and has
no token yet", read off a payload already being fetched — because it costs zero extra
requests and it is intelligence about a chain, not a reason to farm it.

**No Dune, no GeckoTerminal.** Dune's key works and `solana.instruction_calls` is ~5
minutes behind, which would give a hard per-venue launch date; it is deliberately not in
the scheduled sweep because it costs paid credits forever and a single `information_schema`
probe ran >150 s and was abandoned still EXECUTING. GeckoTerminal is disqualified twice
over: its `dex.id` is program-level, so StonkFun is invisible in it by construction, and
its whole pageable window is ~7 minutes of Solana tape.

**No certificate transparency.** `kaiba/hunters/signals.py` owns that surface.

Three layers, and only two of them are allowed to speak
-------------------------------------------------------

``launchlab``  Day 0. The Raydium LaunchLab cross-platform feed with ``platformId``
               OMITTED returns every venue on the shared program, newest-first, with
               ``platformInfo.pubKey`` and ``platformInfo.name`` on every row. Keyless.
               This is the layer that would have seen StonkFun in week one, because
               StonkFun deploys no program of its own -- it is a ``platform_config``
               pubkey on someone else's -- so "watch for new program deploys" sees
               nothing and this sees it on the first pool.

``llama_venues`` DefiLlama per-chain fee tables. Authoritative and late: the
               ``dimension-adapters`` commit that first taught DefiLlama about StonkFun
               is dated 2026-08-18T16:35:59Z (verified through the GitHub API this
               session), 23 days after StonkFun's own fee series starts. DefiLlama is
               the confirmation layer. Any design that makes it the detector reproduces
               the exact failure it is meant to fix.

``llama_chains`` One call returns 30-day DEX volume for 195 volume-bearing chains plus
               the venue roster per chain. This is the new-blockchain layer.

The measurement that changed the design
---------------------------------------

The launchpad survey proposed computing a venue's fees from the LaunchLab payload
directly -- ``platformInfo.feeRate`` times curve volume -- and called the proxy "exact".
**It is not exact, and it was checked here rather than inherited.** A full 24.07-hour
keyless census on 2026-09-21 (67 requests, 6,696 pools, 52 distinct platform configs)
gives StonkFun a proxy of $347,171/day against DefiLlama's $819,168/day -- a ratio of
0.42 -- and gives letsbonk.fun $366/day against DefiLlama's $432,416/day, a ratio of
0.0008. The reason is structural: ``volumeU`` is a pool's LIFETIME volume, so a census of
pools *created* in a window counts only the volume those young pools have accumulated,
and a venue whose flow sits on older pools disappears.

So the proxy is a **lower bound on one venue's fees, comparable only against other
venues measured the same way on the same program**. It is labelled
``usd_per_day_proxy``, it carries its own floor, and it is never mixed with a DefiLlama
figure in the same ranking column. It is still worth having: it is the only number
available for a venue DefiLlama has never heard of, and a lower bound crossing a floor
can only under-report, never over-claim.

Remembering what we already said
--------------------------------

The hard part is not finding candidates, it is not repeating yourself. Three rules, all
in the schema (`kaiba/core/migrations/028_radar.sql`):

1. **The first sweep of a layer reports nothing.** It writes every candidate with
   ``baseline=1`` and with ``reported_tier`` set to whatever tier it is already at, so
   48 Solana launchpads and 195 chains that have existed for years are recorded as known
   rather than announced as discoveries. This is the same rule
   ``signals.diff_assets`` already applies to Hyperliquid and Aevo, for the same reason.
2. **A candidate is announced when its tier exceeds the tier it was last announced at.**
   Tiers are 1x, 10x and 100x the floor. A venue oscillating around the floor is silent
   forever; a venue going from $25k/day to $2.5M/day gets to raise its hand again.
3. **Tier 1 needs three consecutive UTC days above the floor; tier 2 and above report on
   sight.** The confirmation exists to filter a one-day spike sitting on the floor, and a
   candidate arriving at ten times the floor is not that. Counting calendar days rather
   than observations is what stops a two-hourly layer from satisfying a three-day rule
   twelve times faster than a daily one.

Tractability is part of the answer, not a footnote
--------------------------------------------------

A venue we cannot read is not an opportunity for us however big it is, so the output says
so instead of ranking it as actionable. :class:`Tractability` is a tuple, not a boolean:
a gmgn-cli lane, a resolved chain id, a GoPlus answer, a Codex network, an Etherscan v2
route. GoPlus is the binding constraint at 46 chains (measured today) and it is the one
that gates token safety, so its absence downgrades a chain to research-only rather than
disqualifying it. An oracle we could not *ask* yields ``None`` and the verdict
``unknown``, which is not actionable -- missing data is never a pass.

Retrospective proof
-------------------

``tests/test_radar.py`` replays the real daily series for StonkFun, Pons and Robinhood
Chain, recorded live on 2026-09-21, through this module's own logic, and asserts the
dates it would have fired. Summary of that test:

===============  ==================  ====================  ===============
subject          radar would report  found by hand         lead
===============  ==================  ====================  ===============
StonkFun         2026-08-18 *        2026-09-21            34 days
Pons             2026-07-18 *        2026-09-20/21         64 days
Robinhood Chain  2026-07-07          2026-09-20/21         75 days
===============  ==================  ====================  ===============

\\* bounded by DefiLlama's own adapter, not by this rule. On the underlying fee series
StonkFun crosses on 2026-08-05 (day 10) and Pons on 2026-07-14 (day 0); the radar cannot
see either until DefiLlama publishes, which is why the LaunchLab layer exists and why its
lower-bound proxy is kept despite being a lower bound.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, EvidenceBasis, Receipt, digest, now_ms
from kaiba.providers._http import get_json, post_json, redact_text

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# endpoints — every one of these returned 200 through request_json on 2026-09-21
# --------------------------------------------------------------------------------------

LLAMA = "https://api.llama.fi"
#: Per-chain fee table. 315 protocol rows for Solana, 48 of them ``category=="Launchpad"``.
LLAMA_CHAIN_FEES = LLAMA + "/overview/fees/{chain}"
#: Every chain's 30d DEX volume and venue roster in one 2 MB response. 292 chain labels,
#: 1,369 protocols, 195 chains with non-zero volume.
LLAMA_DEXS = LLAMA + "/overview/dexs"
#: One chain's daily DEX volume series. Also the only cheap way to turn a breakdown slug
#: into a display label: an unknown slug answers HTTP 500, not 404.
LLAMA_DEX_CHAIN = LLAMA + "/overview/dexs/{slug}"
#: 467 rows of ``{name, chainId, tvl, gecko_id, tokenSymbol}``. Label-keyed.
LLAMA_CHAINS = LLAMA + "/v2/chains"

#: Raydium LaunchLab, keyless. ``platformId`` is deliberately absent: with it you see one
#: venue, without it you see all of them. ``sort=old|oldest|asc`` all answer HTTP 500, so
#: paging must stay newest-first and stop on a high-water mark.
RAYDIUM_LIST = "https://launch-mint-v1.raydium.io/get/list"

#: Tractability oracles. GoPlus and Etherscan answered without a credential today; Codex
#: needs ``codex_io_api_key``. All three are list endpoints, cached for a day.
GOPLUS_CHAINS = "https://api.gopluslabs.io/api/v1/supported_chains"
ETHERSCAN_CHAINS = "https://api.etherscan.io/v2/chainlist"
CODEX_GRAPHQL = "https://graph.codex.io/graphql"
CODEX_NETWORKS_QUERY = "{getNetworks{id name networkShortName}}"

USER_AGENT = "kaiba-radar/1.0 (+operator-contact-only)"

# --------------------------------------------------------------------------------------
# measurements — the evidence the thresholds below are derived from
# --------------------------------------------------------------------------------------
#
# These are facts read this session, not knobs. They are module constants so that the
# provenance strings can point at something a test can check, and so that nobody has to
# take a docstring's word for a number.

#: Solana launchpad protocol fees per day, summed over 48 ``category=="Launchpad"`` rows
#: of ``/overview/fees/solana?dataType=dailyFees``, 2026-09-21.
SOLANA_LAUNCHPAD_FEE_POOL_USD_2026_09_21 = 3_434_258

#: The next venue below the selected set on that same reading (rapid-launch). The gap
#: between this and Meteora DBC at $37,720/day is the flat the venue floor sits on.
SOLANA_LAUNCHPAD_NEXT_BELOW_USD_2026_09_21 = 5_558

#: Bounds of the band over which the selected set of Solana launchpads is IDENTICAL:
#: $7,500 and $37,500 both select the same six venues holding 99.62% of the pool.
#: Measured, and it is the entire argument for the floor. See `tests/test_radar.py`.
VENUE_FEE_FLAT_LOW_USD = 7_500
VENUE_FEE_FLAT_HIGH_USD = 37_500

#: The LaunchLab proxy's disagreement with ground truth, measured on the two venues where
#: ground truth exists. StonkFun 0.42, letsbonk.fun 0.0008. The proxy is a LOWER BOUND.
LAUNCHLAB_PROXY_RATIO_STONKFUN = Decimal("0.42")
LAUNCHLAB_PROXY_RATIO_BONKFUN = Decimal("0.0008")

#: 24.07-hour keyless census, 2026-09-21: 67 requests, 6,696 pools, 52 platform configs,
#: of which 7 belong to StonkFun. Every figure in the proxy discussion comes from it.
LAUNCHLAB_CENSUS_PAGES_2026_09_21 = 67
LAUNCHLAB_CENSUS_POOLS_2026_09_21 = 6_696
LAUNCHLAB_CENSUS_CONFIGS_2026_09_21 = 52

#: Chain rule firing rate, measured over 70 chains and 72,106 chain-days of daily DEX
#: volume: 40 fires in 7.9 years with the age gate (5.1/yr), 54 without it (6.8/yr).
#: 27 of the 40 went on to a $100M/day seven-day average, so precision is 68%.
CHAIN_RULE_FIRES_WITH_AGE_GATE = 40
CHAIN_RULE_FIRES_WITHOUT_AGE_GATE = 54
CHAIN_RULE_CHAIN_DAYS = 72_106

#: GoPlus is the narrowest oracle we hold and the only one that answers token safety.
GOPLUS_SUPPORTED_CHAINS_2026_09_21 = 46


# --------------------------------------------------------------------------------------
# provenance
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Knob:
    """Where a number came from.

    * ``MEASURED``    — observed by this repository, with the observation quoted.
    * ``REQUIREMENT`` — not a tuning choice: it defines the question being asked.
    * ``INVENTED``    — someone picked it. Says so, and says what would settle it.
    """

    basis: str
    evidence: str


#: Provenance for every field of :class:`RadarConfig`, keyed by field name.
#:
#: ``tests/test_radar.py::test_every_config_knob_has_provenance`` asserts this covers the
#: dataclass exactly in both directions, so a knob cannot arrive without saying where its
#: value came from and a stale entry cannot outlive its field. A threshold with no
#: provenance is a number someone will later defend as though it had been measured.
PROVENANCE: dict[str, Knob] = {
    "venue_fee_floor_usd": Knob(
        "MEASURED",
        "$25,000/day of protocol fees. Chosen because it sits on a FLAT, not a slope: "
        "measured 2026-09-21 on /overview/fees/solana, floors of $7,500, $10,000, "
        "$15,000, $25,000 and $37,500 select the IDENTICAL six venues holding 99.62% of "
        "the $3,434,258/day Solana launchpad pool, because the next venue down "
        "(rapid-launch) is at $5,558 and the one above (Meteora DBC) is at $37,720. You "
        "can be wrong by 3.3x in either direction and get the same answer. Loosening to "
        "$5,000 buys one more venue for +0.16% of the pool; tightening to $50,000 drops "
        "Meteora DBC and costs 1.1%. Back-tested: StonkFun's own daily series crosses it "
        "on 2026-08-04, day 9 of 57, and Pons on 2026-07-14, day 0 of 70.",
    ),
    "venue_proxy_floor_usd": Knob(
        "MEASURED",
        "$25,000/day of the LaunchLab lower-bound fee proxy. A different number from the "
        "DefiLlama floor despite sharing its value, because it is measured on a "
        "different quantity. Justified by an even wider flat: on the 24.07-hour census "
        "of 2026-09-21 the proxy selects exactly one venue (StonkFun, $347,171/day) for "
        "every floor between $8,000 and $347,171 -- a 43x band -- because the next venue "
        "down is at $7,892. The proxy under-reports DefiLlama by 2.4x on StonkFun, so "
        "$25,000 of proxy is roughly $60,000/day of real fees; on StonkFun's series that "
        "is 2026-08-05, one day later than the DefiLlama floor would have fired and 13 "
        "days before DefiLlama had an adapter at all.",
    ),
    "chain_volume_floor_usd_7d": Knob(
        "MEASURED",
        "$50,000,000 of trailing-7d DEX volume. Measured over 70 chains and 72,106 "
        "chain-days: fires 40 times in 7.9 years (5.1/yr) and 27 of the 40 went on to a "
        "$100M/day seven-day average, i.e. 68% precision. Cost curve on the chain that "
        "burned us: Robinhood Chain fires 2026-07-07 at age 12d here, 2026-07-05 at "
        "$25M and 2026-07-03 at $10M -- four days of lead for a materially noisier rule.",
    ),
    "chain_growth_ratio": Knob(
        "MEASURED",
        "3.0x week-over-week, with a zero prior week treated as INFINITE growth rather "
        "than skipped. The zero case is not an edge case: Arc went 0 -> $64,414,148 in a "
        "single day on 2026-09-16, so requiring prev>0 to compute a ratio would have "
        "missed it entirely, and it also pulls Plasma and zkLighter to age 0 and Monad "
        "to age 1.",
    ),
    "chain_max_age_days": Knob(
        "MEASURED",
        "365 days since the chain's first non-zero DEX volume. This gate is what makes "
        "it a NEW-CHAIN detector rather than a market-spike detector: removing it takes "
        "the fire count from 40 to 54 over the same 72,106 chain-days, and every one of "
        "the 14 extra is an established chain caught in a volume surge -- xlayer at 462 "
        "days old, flare at 491, ink at 487, starknet at 1,108.",
    ),
    "confirm_days_required": Knob(
        "MEASURED",
        "3 consecutive UTC days above the floor before a TIER-1 candidate is announced. "
        "Measured cost on the two venues we have series for: it delays StonkFun from "
        "2026-08-04 to 2026-08-06 and Pons from 2026-07-14 to 2026-07-16. Two days of "
        "latency on a venue we were 48 days late to is cheap insurance against a "
        "one-day spike sitting on the floor.",
    ),
    "tier_multipliers": Knob(
        "REQUIREMENT",
        "(1, 10, 100) x the floor. This is the escalation ladder, and it is what makes "
        "the radar report a candidate ONCE: a find is announced only when its tier "
        "exceeds the tier it was last announced at. Decade steps rather than a tuned "
        "sequence because the point is to separate 'crossed' from 'is now a different "
        "thing', not to grade finely. Tier 2 also bypasses the confirmation, which is "
        "the whole reason the ladder is a REQUIREMENT and not a knob: without it a venue "
        "arriving at 10x the floor waits three days to be mentioned.",
    ),
    "tractability_weights": Knob(
        "INVENTED",
        "full 1.0 / partial 0.5 / opaque+unknown 0.1. Nobody measured these. They only "
        "reorder the report; the load-bearing behaviour is the separate `actionable` "
        "flag, which is False for opaque and unknown REGARDLESS of score, so a venue we "
        "cannot read can never be presented as tradeable. What would settle the "
        "magnitudes is a record of how many research-only finds later became tradeable "
        "once an oracle added the chain -- we have no such record yet.",
    ),
    "venue_chains": Knob(
        "MEASURED",
        "('solana', 'robinhood'). Solana leads DEX volume at $90.6B/30d, but Robinhood "
        "Chain leads LAUNCHPAD fees -- 54 launchpads and $191,576,035 of 30d launchpad "
        "fees against Solana's 48 and $75,334,565 -- and it is already a gmgn-cli lane. "
        "Each entry costs one request a day. Chains discovered by the chain layer are "
        "reported, not silently added here: widening the venue sweep is an operator "
        "decision with a request cost.",
    ),
    "launchlab_program": Knob(
        "MEASURED",
        "Raydium LaunchLab, LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj. Not a knob but "
        "a fact about which shared program the day-0 layer watches. StonkFun deploys no "
        "program of its own -- kaiba/ingest/stonkfun.py established chain-verified that "
        "it is a platform_config on this program -- which is why a new-program-deploy "
        "watcher sees nothing and this sees the first pool.",
    ),
    "launchlab_census_window_h": Knob(
        "REQUIREMENT",
        "24 hours, because the proxy is quoted per day and a census over any other "
        "window would be a different quantity wearing the same label. Measured cost: 67 "
        "requests and 40 s of wall clock for 6,696 pools on 2026-09-21.",
    ),
    "launchlab_census_pages": Knob(
        "MEASURED",
        "100 pages of 100. The measured 24 h window needed 67 at 7,825 pools/day, so the "
        "cap is 1.5x headroom. A census that hits the cap is recorded as partial rather "
        "than quietly reported as a full day -- an under-counted proxy that claims to be "
        "a day is worse than an honestly short one.",
    ),
    "launchlab_discover_pages": Knob(
        "MEASURED",
        "12 pages for the incremental discovery poll. At the measured 7,825 pools/day a "
        "two-hour gap is ~652 pools = 7 pages, so 12 covers a missed poll and a burst. "
        "Beyond that the poll gives up and lets the daily census close the gap, rather "
        "than walking the whole feed on someone else's infrastructure.",
    ),
    "intervals": Knob(
        "MEASURED",
        "llama_venues and llama_chains daily, because their inputs are daily aggregates "
        "and polling a daily number hourly buys nothing. launchlab_census daily for the "
        "same reason. launchlab_discover every 2 h: registry-only, ~8 pages a poll, and "
        "it is the only layer that can see a venue before DefiLlama, which took 23 days "
        "on StonkFun. Total measured cost: 3 DefiLlama requests plus ~155 keyless "
        "Raydium pages a day, about 55 s of wall clock at the configured 350 ms "
        "min_interval.",
    ),
    "dead_after_floor_s": Knob(
        "INVENTED",
        "6 hours before a layer that has not succeeded is called dead, copied "
        "deliberately from signals.DEAD_AFTER_FLOOR_S so the two hunters age the same "
        "way. Slow layers get six of their own intervals instead. Nothing measured says "
        "six; what would settle it is the observed distribution of DefiLlama outages, "
        "which we have not collected.",
    ),
    "max_reports_per_sweep": Knob(
        "INVENTED",
        "8. A blast radius, not a threshold: if a provider re-slugs its whole catalogue "
        "the radar should say 'something is wrong' rather than emit 195 discoveries. The "
        "overflow is recorded and re-offered on the next sweep, so nothing is lost. "
        "Measured context: the chain layer fires 5.1 times a YEAR, so 8 in one sweep "
        "already means something is broken. Nobody measured 8 itself; what would settle "
        "it is the observed distribution of simultaneous crossings, which needs a year "
        "of this module running.",
    ),
    "chain_series_budget": Knob(
        "MEASURED",
        "60 per-chain series requests per sweep. The bulk /overview/dexs response carries "
        "only a 30-day total, which cannot answer 'is this a step change this week', so a "
        "chain above the floor needs one series call. Measured on the 2026-09-21 roster: "
        "53 of 195 volume-bearing chains clear $50M/30d, and 60 covers that with headroom. "
        "After the first sweep the registry remembers each chain's age, and only the 14 "
        "that are younger than the age gate are worth asking again -- so the standing cost "
        "is ~15 DefiLlama requests a day, not 53.",
    ),
    "oracle_ttl_s": Knob(
        "INVENTED",
        "86,400 s for the three tractability oracle lists. They are membership lists "
        "that change when a vendor adds a chain, which is a weekly-to-monthly event, and "
        "a stale answer costs at most one sweep of a wrong verdict on one candidate. "
        "Nobody measured how often those lists actually change; what would settle it is "
        "diffing them daily for a quarter, which costs three requests a day to learn.",
    ),
}


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RadarConfig:
    """Every number here is declared in :data:`PROVENANCE`. A test enforces that."""

    #: Daily protocol fees, USD, from DefiLlama. MEASURED flat: $7,500-$37,500 identical.
    venue_fee_floor_usd: Decimal = Decimal("25000")
    #: Daily fee LOWER BOUND from the LaunchLab census. A different quantity, hence its
    #: own floor even though the number coincides.
    venue_proxy_floor_usd: Decimal = Decimal("25000")
    #: Trailing 7-day DEX volume, USD.
    chain_volume_floor_usd_7d: Decimal = Decimal("50000000")
    chain_growth_ratio: Decimal = Decimal("3.0")
    chain_max_age_days: int = 365
    confirm_days_required: int = 3
    tier_multipliers: tuple[int, ...] = (1, 10, 100)
    tractability_weights: tuple[Decimal, ...] = (Decimal("1.0"), Decimal("0.5"), Decimal("0.1"))
    venue_chains: tuple[str, ...] = ("solana", "robinhood")
    launchlab_program: str = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
    launchlab_census_window_h: int = 24
    launchlab_census_pages: int = 100
    launchlab_discover_pages: int = 12
    intervals: tuple[tuple[str, int], ...] = (
        ("llama_venues", 86_400),
        ("llama_chains", 86_400),
        ("launchlab_census", 86_400),
        ("launchlab_discover", 7_200),
    )
    dead_after_floor_s: int = 6 * 3600
    max_reports_per_sweep: int = 8
    chain_series_budget: int = 60
    oracle_ttl_s: int = 86_400

    def interval_for(self, layer: str) -> int:
        return dict(self.intervals).get(layer, 86_400)

    def dead_after_s(self, layer: str) -> int:
        return max(self.dead_after_floor_s, 6 * self.interval_for(layer))

    def floor_for(self, unit: str) -> Decimal:
        return {
            "usd_per_day": self.venue_fee_floor_usd,
            "usd_per_day_proxy": self.venue_proxy_floor_usd,
            "usd_7d": self.chain_volume_floor_usd_7d,
        }[unit]


DEFAULT_CONFIG = RadarConfig()


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def dec(value: Any) -> Decimal | None:
    """Anything -> Decimal, or ``None``.

    ``None`` on purpose, never ``Decimal(0)``: docs/CONTRACT.md rule 2, and here the
    difference decides whether a venue reads as "too small to care" or as "we could not
    price its quote asset". StonkFun quotes only 6.9% of its pools in wrapped SOL, with a
    tail of 501 distinct quote mints, so unpriceable is the common case, not the corner.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if out.is_nan() or out.is_infinite() else out


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def _next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
            + timedelta(days=1)).strftime("%Y-%m-%d")


#: DefiLlama chain slugs that map onto an existing gmgn-cli lane. Deliberately explicit:
#: ``avax`` is "Avalanche", ``xdai`` is "Gnosis" and ``robinhood`` is "Robinhood Chain",
#: so a title-case guess silently mismatches. Only chains we can actually trade belong
#: here, and adding one means adding a ``Chain`` member, which is the trading contract's
#: decision and not the radar's.
NATIVE_LANES: dict[str, Chain] = {
    "solana": Chain.SOL,
    "ethereum": Chain.ETH,
    "bsc": Chain.BSC,
    "base": Chain.BASE,
    "robinhood": Chain.ROBINHOOD,
    "arc": Chain.ARC,
    "stable": Chain.STABLE,
}


def native_lane(chain_slug: str | None) -> Chain | None:
    """The tradeable lane for a slug, when one exists.

    ``Chain`` is a closed StrEnum because it is gmgn-cli's ``--chain`` argument, and it
    must stay closed. This function is the read-only bridge: the radar records what it
    found in free text and reports whether a lane happens to exist, and widening the enum
    stays a human decision. Conflating the two is what made pump.fun, Pons and StonkFun
    hand integrations -- you could not write down a discovery without first editing the
    trading contract, so nobody wrote one down.
    """
    return NATIVE_LANES.get((chain_slug or "").strip().lower())


def tier_for(value: Decimal | None, floor: Decimal, multipliers: Sequence[int]) -> int:
    """0 below the floor, then 1..len(multipliers) as the value climbs the ladder."""
    if value is None or floor <= 0:
        return 0
    ratio = value / floor
    out = 0
    for i, mult in enumerate(multipliers, start=1):
        if ratio >= Decimal(mult):
            out = i
    return out


# --------------------------------------------------------------------------------------
# tractability
# --------------------------------------------------------------------------------------

FULL, PARTIAL, OPAQUE, UNKNOWN = "full", "partial", "opaque", "unknown"


@dataclass(frozen=True, slots=True)
class Tractability:
    """Can we actually read this thing? A tuple, because the answer is not one bit.

    Each oracle field is tri-state. ``True``/``False`` mean we asked and got an answer;
    ``None`` means we could not ask, which yields verdict :data:`UNKNOWN` and is **not**
    actionable. Missing data is never a pass.
    """

    chain_slug: str | None
    chain_id: int | None = None
    lane: Chain | None = None
    goplus: bool | None = None
    codex: bool | None = None
    etherscan: bool | None = None

    @property
    def verdict(self) -> str:
        if self.lane is not None:
            # We trade here today. Nothing an oracle list says overrides that.
            return FULL
        asked = [self.goplus, self.codex, self.etherscan]
        if all(a is None for a in asked) and self.chain_id is None:
            return UNKNOWN
        readable = bool(self.codex) or bool(self.etherscan) or self.chain_id is not None
        if not readable:
            return OPAQUE
        if self.goplus is True:
            return FULL
        if self.goplus is None:
            # Readable but the safety oracle never answered. We do not know whether we
            # could screen a token here, and "do not know" is not "yes".
            return UNKNOWN
        return PARTIAL

    @property
    def actionable(self) -> bool:
        """False for opaque and unknown, whatever the money says.

        This is the guard the task asks for: a venue we cannot read is not an opportunity
        for us however big. ``rank_score`` still orders such a find into the report so a
        human sees it; ``actionable`` is what stops it being presented as tradeable.
        """
        return self.verdict in {FULL, PARTIAL}

    @property
    def weight_index(self) -> int:
        return {FULL: 0, PARTIAL: 1}.get(self.verdict, 2)

    def why(self) -> str:
        if self.lane is not None:
            return f"gmgn-cli lane '{self.lane.value}' already exists"
        bits: list[str] = []
        bits.append(f"chainId={self.chain_id}" if self.chain_id is not None else "chainId=UNRESOLVED")
        for name, val in (("goplus", self.goplus), ("codex", self.codex),
                          ("etherscan", self.etherscan)):
            bits.append(f"{name}={'yes' if val else ('UNAVAILABLE' if val is None else 'no')}")
        if self.verdict == OPAQUE:
            return "no oracle we hold covers this chain: " + ", ".join(bits)
        if self.verdict == UNKNOWN:
            return "could not establish coverage: " + ", ".join(bits)
        if self.verdict == PARTIAL:
            return ("readable but GoPlus does not cover it, so token safety cannot be "
                    "screened here: " + ", ".join(bits))
        return ", ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_slug": self.chain_slug,
            "chain_id": self.chain_id,
            "lane": self.lane.value if self.lane else None,
            "goplus": self.goplus,
            "codex": self.codex,
            "etherscan": self.etherscan,
            "verdict": self.verdict,
            "actionable": self.actionable,
            "why": self.why(),
        }


@dataclass(frozen=True, slots=True)
class OracleSets:
    """Chain membership for each oracle. ``None`` means the list could not be fetched."""

    goplus_ids: frozenset[str] | None = None
    codex_ids: frozenset[str] | None = None
    codex_names: frozenset[str] | None = None
    etherscan_ids: frozenset[str] | None = None
    llama_chain_ids: dict[str, int] = field(default_factory=dict)
    #: Chains in ``/v2/chains`` carrying neither ``gecko_id`` nor ``tokenSymbol``, keyed
    #: by lowercased display label. This is the operator's "new airdrop" question in the
    #: only form the evidence supports: a chain holding real money that has not issued a
    #: token yet is the population airdrops come from. It is a FLAG, never a plan --
    #: farming resolved against farmers (0 of 652 DefiLlama rows and 0 of 119 live
    #: opportunities cleared a $25 EV floor at $60/h, $30/h or $0/h), so nothing here
    #: routes to ev.py. It costs zero extra requests: /v2/chains is already fetched for
    #: the chainId join.
    #:
    #: Read off /v2/chains rather than /protocols on purpose. On /protocols, "Scroll
    #: Bridge", "Linea Bridge", "MegaETH Bridge" and "Hyperliquid Bridge" all report
    #: symbol '-' with a null gecko_id, so a tokenless filter there re-recruits every
    #: post-TGE chain. /v2/chains gets it right: none of Linea, Scroll, Berachain or
    #: Monad appear in its tokenless set, while Base, Arc, Robinhood Chain and Abstract
    #: do.
    tokenless_chains: frozenset[str] | None = None
    requests: int = 0


def _oracle_goplus(cfg: RadarConfig, conn: Any) -> tuple[frozenset[str] | None, int]:
    got = get_json("goplus", "chain.supported", GOPLUS_CHAINS, ttl_s=cfg.oracle_ttl_s,
                   stale_grace_s=6 * cfg.oracle_ttl_s, timeout_s=30.0, retries=2,
                   wait_for_slot_s=20.0, conn=conn)
    if not got.ok:
        return None, 1
    rows = (got.data or {}).get("result") or []
    return frozenset(str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")), 1


def _oracle_etherscan(cfg: RadarConfig, conn: Any) -> tuple[frozenset[str] | None, int]:
    got = get_json("etherscan", "v2.chainlist", ETHERSCAN_CHAINS, ttl_s=cfg.oracle_ttl_s,
                   stale_grace_s=6 * cfg.oracle_ttl_s, timeout_s=30.0, retries=2,
                   wait_for_slot_s=20.0, conn=conn)
    if not got.ok:
        return None, 1
    rows = (got.data or {}).get("result") or []
    return frozenset(str(r.get("chainid")) for r in rows
                     if isinstance(r, dict) and r.get("chainid")), 1


def _oracle_codex(cfg: RadarConfig, conn: Any) -> tuple[frozenset[str] | None, frozenset[str] | None, int]:
    from kaiba.core.config import get_settings

    key = get_settings().codex_io_api_key
    if not key:
        # No credential is not the same as no coverage. Stay None so the verdict says
        # UNKNOWN rather than quietly asserting the chain is unreadable.
        return None, None, 0
    got = post_json("codex", "graphql.networks", CODEX_GRAPHQL,
                    headers={"Authorization": key, "content-type": "application/json"},
                    json_body={"query": CODEX_NETWORKS_QUERY}, ttl_s=cfg.oracle_ttl_s,
                    stale_grace_s=6 * cfg.oracle_ttl_s, timeout_s=30.0, retries=2,
                    wait_for_slot_s=20.0, conn=conn)
    if not got.ok:
        return None, None, 1
    nets = ((got.data or {}).get("data") or {}).get("getNetworks") or []
    ids = frozenset(str(n.get("id")) for n in nets if isinstance(n, dict) and n.get("id") is not None)
    names = frozenset(str(n.get("networkShortName") or "").lower() for n in nets
                      if isinstance(n, dict) and n.get("networkShortName"))
    return ids, names, 1


def load_oracles(conn: Any = None, *, cfg: RadarConfig | None = None,
                 raw: dict[str, Any] | None = None) -> OracleSets:
    """The three membership lists plus the DefiLlama label->chainId table.

    Four requests, all cached for a day and all served from disk on every sweep but the
    first of each day. ``raw`` is the test seam.
    """
    config = cfg or DEFAULT_CONFIG
    if raw is not None:
        return OracleSets(
            goplus_ids=raw.get("goplus"),
            codex_ids=raw.get("codex_ids"),
            codex_names=raw.get("codex_names"),
            etherscan_ids=raw.get("etherscan"),
            llama_chain_ids=raw.get("llama_chain_ids") or {},
            tokenless_chains=raw.get("tokenless"),
            requests=0,
        )
    reqs = 0
    goplus, n = _oracle_goplus(config, conn)
    reqs += n
    etherscan, n = _oracle_etherscan(config, conn)
    reqs += n
    codex_ids, codex_names, n = _oracle_codex(config, conn)
    reqs += n
    labels: dict[str, int] = {}
    tokenless: set[str] = set()
    got = get_json("defillama", "v2.chains", LLAMA_CHAINS, ttl_s=config.oracle_ttl_s,
                   stale_grace_s=6 * config.oracle_ttl_s, timeout_s=60.0, retries=2,
                   wait_for_slot_s=30.0, conn=conn)
    reqs += 1
    tokenless_known = got.ok
    if got.ok:
        for row in got.data if isinstance(got.data, list) else []:
            name = str((row or {}).get("name") or "")
            if not name:
                continue
            cid = (row or {}).get("chainId")
            if cid is not None:
                try:
                    labels[name.lower()] = int(cid)
                except (TypeError, ValueError):
                    pass
            if not (row or {}).get("gecko_id") and not (row or {}).get("tokenSymbol"):
                tokenless.add(name.lower())
    return OracleSets(goplus, codex_ids, codex_names, etherscan, labels,
                      frozenset(tokenless) if tokenless_known else None, reqs)


def assess_tractability(chain_slug: str | None, oracles: OracleSets, *,
                        chain_label: str | None = None,
                        chain_id: int | None = None) -> Tractability:
    """Turn a chain slug into the six-field answer.

    ``chain_id`` is taken from the caller when it already knows it, otherwise resolved
    from DefiLlama's label table. Deriving a chain id by name-matching into chainlist is
    explicitly not done: chainId 999 there is "Wanchain Testnet", not HyperEVM, and its
    RPC answers ``eth_chainId`` = 999, so a naive liveness check passes on the wrong
    chain.
    """
    slug = (chain_slug or "").strip().lower() or None
    lane = native_lane(slug)
    cid = chain_id
    if cid is None and oracles.llama_chain_ids:
        for candidate in (chain_label, slug):
            if candidate and candidate.lower() in oracles.llama_chain_ids:
                cid = oracles.llama_chain_ids[candidate.lower()]
                break
    sid = str(cid) if cid is not None else None
    goplus = None if oracles.goplus_ids is None else (sid is not None and sid in oracles.goplus_ids)
    etherscan = (None if oracles.etherscan_ids is None
                 else (sid is not None and sid in oracles.etherscan_ids))
    if oracles.codex_ids is None and oracles.codex_names is None:
        codex: bool | None = None
    else:
        codex = bool(
            (sid is not None and sid in (oracles.codex_ids or frozenset()))
            or (slug is not None and slug in (oracles.codex_names or frozenset()))
        )
    return Tractability(slug, cid, lane, goplus, codex, etherscan)


# --------------------------------------------------------------------------------------
# the candidate
# --------------------------------------------------------------------------------------

KIND_VENUE = "venue"
KIND_CHAIN = "chain"
KIND_PLATFORM_CONFIG = "platform_config"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One observation of one thing that might be new.

    ``value is None`` with ``basis=UNAVAILABLE`` is a first-class state and it is the
    reason this is not a plain dict: a candidate we could not price must be storable and
    rankable-as-unrankable, not silently zero.
    """

    kind: str
    layer: str
    identity: str
    display_name: str | None = None
    chain_slug: str | None = None
    value: Decimal | None = None
    unit: str = "usd_per_day"
    basis: EvidenceBasis = EvidenceBasis.UNAVAILABLE
    receipt: Receipt | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: Some layers (the LaunchLab discovery poll) exist to populate the registry and must
    #: never raise an alert. 52 platform configs appeared in 24 hours, most of them
    #: one-pool vanity deployments; alerting on existence would be 52 alerts on day one.
    silent: bool = False

    @property
    def radar_key(self) -> str:
        return digest({"k": self.kind, "i": self.identity})[:32]


# --------------------------------------------------------------------------------------
# layer 1 — DefiLlama per-chain fee tables (authoritative, late)
# --------------------------------------------------------------------------------------


def parse_venue_fees(payload: Any, chain_slug: str) -> list[Candidate]:
    """Every protocol on one chain's fee table, as candidates.

    Deliberately **not** filtered by category. The NFT survey's conclusion was that the
    category field is the wrong instrument in both directions -- Fragment sits under "NFT
    Marketplace" while selling Telegram usernames and is 89% of that category's fees,
    Heaven is filed under "Dexs" and moonshot.money under "Trading App" -- and that a
    measured money floor does the discrimination the category label cannot. Category is
    carried as metadata so the report can print it.
    """
    out: list[Candidate] = []
    rows = (payload or {}).get("protocols") or []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or row.get("name") or "").strip()
        if not slug:
            continue
        value = dec(row.get("total24h"))
        out.append(
            Candidate(
                kind=KIND_VENUE,
                layer="llama_venues",
                identity=f"{chain_slug}:{slug}",
                display_name=str(row.get("displayName") or row.get("name") or slug)[:120],
                chain_slug=chain_slug,
                value=value,
                unit="usd_per_day",
                basis=(EvidenceBasis.PROVIDER_REPORTED if value is not None
                       else EvidenceBasis.UNAVAILABLE),
                meta={
                    "category": row.get("category"),
                    "slug": slug,
                    "total7d": str(dec(row.get("total7d"))) if dec(row.get("total7d")) else None,
                    "defillama_id": row.get("defillamaId"),
                    # change_1d is carried and NEVER gated on: the live table holds
                    # +31,328.57% on a $22/day venue and -97.33% on metaplex. Percentage
                    # change on a near-zero base is noise; it is a tie-break at most.
                    "change_1d": str(dec(row.get("change_1d"))) if dec(row.get("change_1d")) else None,
                },
            )
        )
    return out


def poll_venue_fees(conn: Any = None, *, cfg: RadarConfig | None = None,
                    raw: dict[str, Any] | None = None) -> tuple[list[Candidate], int, int, int, str | None]:
    """Returns ``(candidates, endpoints_ok, endpoints_total, requests, last_error)``."""
    config = cfg or DEFAULT_CONFIG
    chains = list(raw) if raw is not None else list(config.venue_chains)
    out: list[Candidate] = []
    ok = 0
    reqs = 0
    err: str | None = None
    for chain_slug in chains:
        if raw is not None:
            payload: Any = raw[chain_slug]
        else:
            got = get_json(
                "defillama", "overview.fees", LLAMA_CHAIN_FEES.format(chain=chain_slug),
                params={"dataType": "dailyFees", "excludeTotalDataChart": "true",
                        "excludeTotalDataChartBreakdown": "true"},
                headers={"user-agent": USER_AGENT},
                ttl_s=3600, stale_grace_s=6 * 3600, timeout_s=90.0, retries=2,
                wait_for_slot_s=30.0, conn=conn,
            )
            reqs += 1
            if not got.ok:
                err = f"{chain_slug}: {got.receipt.note or 'unavailable'}"
                continue
            payload = got.data
        ok += 1
        try:
            out.extend(parse_venue_fees(payload, chain_slug))
        except Exception as exc:  # noqa: BLE001 - a layout change is not an outage
            err = f"{chain_slug}: parse failed: {type(exc).__name__}: {exc}"
            log.warning("radar venue parse: %s", redact_text(err))
    return out, ok, len(chains), reqs, err


# --------------------------------------------------------------------------------------
# layer 2 — DefiLlama chain DEX volume (the new-blockchain layer)
# --------------------------------------------------------------------------------------


def chain_volume_30d(payload: Any) -> dict[str, tuple[Decimal, int, list[str]]]:
    """Sum ``breakdown30d`` across protocols -> ``{chain_slug: (usd_30d, venues, names)}``.

    One 2 MB response covers every chain, which is why the chain layer costs one request
    a day rather than one per chain.
    """
    agg: dict[str, tuple[Decimal, int, list[str]]] = {}
    for row in ((payload or {}).get("protocols") or []):
        bd = (row or {}).get("breakdown30d")
        if not isinstance(bd, dict):
            continue
        for slug, venues in bd.items():
            if not isinstance(venues, dict):
                continue
            total = Decimal(0)
            names: list[str] = []
            for venue_name, usd in venues.items():
                v = dec(usd)
                if v is not None:
                    total += v
                names.append(str(venue_name))
            prev = agg.get(str(slug))
            if prev is None:
                agg[str(slug)] = (total, len(names), names[:6])
            else:
                agg[str(slug)] = (prev[0] + total, prev[1] + len(names),
                                  (prev[2] + names)[:6])
    return agg


def parse_chain_series(chart: Any) -> list[tuple[int, Decimal]]:
    out: list[tuple[int, Decimal]] = []
    for point in chart if isinstance(chart, list) else []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        ts = dec(point[0])
        val = dec(point[1])
        if ts is None:
            continue
        out.append((int(ts), val if val is not None else Decimal(0)))
    out.sort()
    return out


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """The chain rule's answer for one chain, with every input it used."""

    fires: bool
    reason: str
    window_7d: Decimal | None = None
    prior_7d: Decimal | None = None
    age_days: int | None = None
    growth: Decimal | None = None


def chain_rule(series: Sequence[tuple[int, Decimal]], cfg: RadarConfig,
               *, at_index: int | None = None) -> ChainVerdict:
    """Trailing-7d floor, 3x growth (or a zero prior week), and an age gate.

    The zero-prior-week branch is not a nicety. Arc went 0 -> $64,414,148 on a single day
    (2026-09-16), so a rule that needs ``prev > 0`` to compute a ratio never fires on it
    at all; treating a zero base as infinite growth also pulls Plasma and zkLighter to
    age 0 and Monad to age 1.

    The age gate is what makes this a new-chain detector rather than a market-spike
    detector: removing it takes 40 fires to 54 over the same 72,106 chain-days, and every
    extra one is an old chain in a surge.

    **Windows and age are measured in seconds, not in array positions.** DefiLlama's
    ``totalDataChart`` omits days on which a chain did nothing -- Robinhood Chain's
    series jumps from 2026-06-16 straight to 2026-06-25 -- so "the last seven entries"
    and "the last seven days" are different quantities, and on a young chain they differ
    by enough to move both the fire date and the reported age. Robinhood reads as 13
    entries but 21 days old.
    """
    if not series:
        return ChainVerdict(False, "no_series")
    vals = [v for _, v in series]
    i = len(vals) - 1 if at_index is None else at_index
    if i < 0 or i >= len(vals):
        return ChainVerdict(False, "index_out_of_range")
    first_nz = next((k for k, v in enumerate(vals) if v > 0), None)
    if first_nz is None:
        return ChainVerdict(False, "never_traded")
    at_ts = series[i][0]
    age = (at_ts - series[first_nz][0]) // 86_400
    day = 86_400
    cur = sum((v for t, v in series[: i + 1] if t > at_ts - 7 * day), Decimal(0))
    prev = sum((v for t, v in series[: i + 1]
                if at_ts - 14 * day < t <= at_ts - 7 * day), Decimal(0))
    growth = None if prev <= 0 else cur / prev
    if age > cfg.chain_max_age_days:
        return ChainVerdict(False, "older_than_age_gate", cur, prev, age, growth)
    if cur < cfg.chain_volume_floor_usd_7d:
        return ChainVerdict(False, "below_volume_floor", cur, prev, age, growth)
    if prev > 0 and (growth is None or growth < cfg.chain_growth_ratio):
        return ChainVerdict(False, "growth_below_ratio", cur, prev, age, growth)
    return ChainVerdict(True, "zero_base" if prev <= 0 else "growth", cur, prev, age, growth)


def _pseudo_chain(slug: str, venues: int, names: Sequence[str], chain_id: int | None) -> bool:
    """An order-book exchange DefiLlama models as a chain, not a chain.

    74 of 195 volume-bearing slugs have exactly one venue, and 13 entries in ``allChains``
    have no ``/v2/chains`` row at all -- Spark is $2.5B/30d attached to no chain, and
    ``native_core`` fired in the backtest at 1 day old and never became a chain. The
    discriminator, measured: one venue, no chain id, and the venue's name shares a prefix
    with the chain label.
    """
    if venues != 1 or chain_id is not None:
        return False
    head = (names[0] if names else "").strip().lower()
    stem = slug.replace("_", " ").strip().lower()
    return bool(head) and bool(stem) and (head.startswith(stem) or stem.startswith(head.split()[0]))


def _aged_out(conn: Any, cfg: RadarConfig, now: int) -> set[str]:
    """Chains the registry already knows to be older than the age gate.

    This is the memory paying for itself in requests rather than in noise. 53 of 195
    volume-bearing chains clear the volume floor but only 14 are younger than 365 days,
    and a chain only gets older. Asking DefiLlama for ethereum's 1,775-point daily series
    every morning to re-learn that it launched in 2020 is exactly the kind of standing
    cost that makes a scheduled job unwelcome.
    """
    if conn is None:
        return set()
    out: set[str] = set()
    try:
        rows = fetch_all(conn, "SELECT identity, last_seen_ms, meta_json FROM radar_registry "
                               "WHERE kind = ?", (KIND_CHAIN,))
    except Exception as exc:  # noqa: BLE001 - no registry yet is not an error
        log.debug("radar could not read chain ages: %s", exc)
        return out
    for row in rows:
        meta = jload(row.get("meta_json"), {}) or {}
        age = meta.get("age_days")
        if age is None:
            continue
        elapsed_days = max(0, (now - int(row["last_seen_ms"] or now)) // 86_400_000)
        if int(age) + elapsed_days > cfg.chain_max_age_days:
            out.add(str(row["identity"]))
    return out


def poll_chain_volume(conn: Any = None, *, cfg: RadarConfig | None = None,
                      raw: dict[str, Any] | None = None,
                      oracles: OracleSets | None = None, now: int | None = None
                      ) -> tuple[list[Candidate], int, int, int, str | None]:
    """One bulk call for the roster, then one series call per chain that could be new.

    The bulk response carries only a 30-day total, which cannot answer "is this a step
    change this week", so a chain above the floor needs its own series. Two filters keep
    that from being 53 requests every morning: the volume floor (195 chains -> 53) and
    the registry's memory of each chain's age (53 -> 14). A chain only gets older, so an
    aged-out chain never needs asking again.
    """
    config = cfg or DEFAULT_CONFIG
    ts = now if now is not None else now_ms()
    reqs = 0
    if raw is not None and "overview" in raw:
        payload: Any = raw["overview"]
    else:
        got = get_json("defillama", "overview.dexs", LLAMA_DEXS,
                       params={"excludeTotalDataChart": "true",
                               "excludeTotalDataChartBreakdown": "true"},
                       headers={"user-agent": USER_AGENT},
                       ttl_s=3600, stale_grace_s=6 * 3600, timeout_s=120.0, retries=2,
                       wait_for_slot_s=30.0, conn=conn)
        reqs += 1
        if not got.ok:
            return [], 0, 1, reqs, got.receipt.note or "unavailable"
        payload = got.data
    try:
        agg = chain_volume_30d(payload)
    except Exception as exc:  # noqa: BLE001
        return [], 0, 1, reqs, f"parse failed: {type(exc).__name__}: {exc}"

    orc = oracles if oracles is not None else load_oracles(conn, cfg=config)
    reqs += orc.requests
    out: list[Candidate] = []
    err: str | None = None
    skip = _aged_out(conn, config, ts)
    shortlist = [
        (slug, total, venues, names) for slug, (total, venues, names) in agg.items()
        if total >= config.chain_volume_floor_usd_7d and slug not in skip
    ]
    shortlist.sort(key=lambda t: -t[1])
    if len(shortlist) > config.chain_series_budget:
        # Truncation is recorded, not silent. A clipped sweep and a quiet week must not
        # look the same -- that is the exact failure the health table exists to prevent.
        err = (f"shortlist of {len(shortlist)} chains exceeded the "
               f"{config.chain_series_budget}-request budget; "
               f"{len(shortlist) - config.chain_series_budget} not checked this sweep")
        shortlist = shortlist[: config.chain_series_budget]
    for slug, total30, venues, names in shortlist:
        series_raw = (raw or {}).get("series", {}).get(slug) if raw is not None else None
        label = None
        if series_raw is None:
            got = get_json("defillama", "overview.dexs.chain",
                           LLAMA_DEX_CHAIN.format(slug=slug),
                           params={"excludeTotalDataChartBreakdown": "true"},
                           headers={"user-agent": USER_AGENT},
                           ttl_s=3600, stale_grace_s=6 * 3600, timeout_s=90.0, retries=2,
                           wait_for_slot_s=30.0, conn=conn)
            reqs += 1
            if not got.ok:
                # An unknown slug answers HTTP 500, not 404, so this branch is both
                # "provider down" and "slug we cannot resolve". Neither is a chain we can
                # say anything about, and neither is a zero.
                err = f"{slug}: {got.receipt.note or 'unavailable'}"
                continue
            series_raw = (got.data or {}).get("totalDataChart")
            label = (got.data or {}).get("chain")
        series = parse_chain_series(series_raw)
        verdict = chain_rule(series, config)
        chain_id = orc.llama_chain_ids.get(str(label or "").lower()) if label else None
        if chain_id is None:
            chain_id = orc.llama_chain_ids.get(slug.lower())
        pseudo = _pseudo_chain(slug, venues, names, chain_id)
        latest7 = verdict.window_7d
        # The operator's "new airdrop" question, answered as intelligence and nothing
        # more. None when /v2/chains did not answer -- unknown is not "has a token".
        if orc.tokenless_chains is None:
            pre_tge: bool | None = None
        else:
            pre_tge = any(k in orc.tokenless_chains
                          for k in (str(label or "").lower(), slug.lower()) if k)
        out.append(
            Candidate(
                kind=KIND_CHAIN,
                layer="llama_chains",
                identity=slug,
                display_name=str(label or slug)[:120],
                chain_slug=slug,
                value=latest7 if verdict.fires else None,
                unit="usd_7d",
                basis=(EvidenceBasis.PROVIDER_REPORTED if verdict.fires
                       else EvidenceBasis.UNAVAILABLE),
                meta={
                    "rule": verdict.reason,
                    "volume_30d_usd": str(total30),
                    "window_7d_usd": str(verdict.window_7d) if verdict.window_7d is not None else None,
                    "prior_7d_usd": str(verdict.prior_7d) if verdict.prior_7d is not None else None,
                    "age_days": verdict.age_days,
                    "growth": str(verdict.growth) if verdict.growth is not None else None,
                    "venues": venues,
                    "venue_names": list(names),
                    "chain_id": chain_id,
                    "pseudo_chain": pseudo,
                    "label": label,
                    # Intelligence, not a plan. See OracleSets.tokenless_chains.
                    "pre_tge_no_token": pre_tge,
                },
                # A chain that does not fire the rule is still registry-worthy (so its
                # first-seen is ours and not a backfilled adapter's) but must not alert.
                silent=not verdict.fires or pseudo,
            )
        )
    return out, 1, 1, reqs, err


# --------------------------------------------------------------------------------------
# layer 3 — Raydium LaunchLab, the only layer that can beat DefiLlama
# --------------------------------------------------------------------------------------


def parse_launchlab_page(payload: Any) -> tuple[list[dict[str, Any]], str | None]:
    body = (payload or {}).get("data") or {}
    rows = body.get("rows") or []
    return ([r for r in rows if isinstance(r, dict)], body.get("nextPageId"))


def aggregate_launchlab(rows: Iterable[dict[str, Any]], *, window_ms: int,
                        now: int) -> dict[str, dict[str, Any]]:
    """Per-``platform_config`` roll-up of pools created inside the window.

    The fee figure produced here is ``usd_per_day_proxy`` and it is a **lower bound**.
    ``volumeU`` is a pool's LIFETIME volume, so summing it over pools *created* in a
    24-hour window counts only what those young pools have done so far and misses every
    dollar traded on older pools. Measured against ground truth on 2026-09-21: StonkFun
    0.42x DefiLlama, letsbonk.fun 0.0008x. Calling this "the venue's fees" would be a
    lie; calling it a floor-crossing detector for venues DefiLlama has never heard of is
    not.

    A pool whose ``volumeU`` is missing contributes to ``pools_unpriced`` and to nothing
    else. A config with no priced pools at all yields ``fee_proxy_usd = None``, never 0.
    """
    cutoff = now - window_ms
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        created = dec(row.get("createAt"))
        if created is None or int(created) < cutoff:
            continue
        info = row.get("platformInfo") or {}
        pubkey = str(info.get("pubKey") or "").strip()
        if not pubkey:
            continue
        acc = out.setdefault(pubkey, {
            "platform_config": pubkey,
            "name": None,
            "fee_rate_ppm": None,
            "pools": 0,
            "pools_priced": 0,
            "pools_unpriced": 0,
            "volume_usd": Decimal(0),
            "first_pool_ms": None,
            "last_pool_ms": None,
            "sample_mint": None,
        })
        acc["name"] = info.get("name") or acc["name"]
        fee_rate = dec(info.get("feeRate"))
        if fee_rate is not None:
            acc["fee_rate_ppm"] = fee_rate
        acc["pools"] += 1
        vol = dec(row.get("volumeU"))
        if vol is None:
            acc["pools_unpriced"] += 1
        else:
            acc["pools_priced"] += 1
            acc["volume_usd"] += vol
        created_ms = int(created)
        acc["first_pool_ms"] = (created_ms if acc["first_pool_ms"] is None
                                else min(acc["first_pool_ms"], created_ms))
        acc["last_pool_ms"] = (created_ms if acc["last_pool_ms"] is None
                               else max(acc["last_pool_ms"], created_ms))
        acc["sample_mint"] = acc["sample_mint"] or row.get("mint")
    for acc in out.values():
        rate = acc["fee_rate_ppm"]
        if acc["pools_priced"] == 0 or rate is None:
            acc["fee_proxy_usd"] = None
        else:
            acc["fee_proxy_usd"] = acc["volume_usd"] * rate / Decimal(1_000_000)
    return out


def fetch_launchlab(conn: Any = None, *, max_pages: int = 100, stop_before_ms: int | None = None,
                    raw_pages: Sequence[Any] | None = None
                    ) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    """Walk the cross-platform feed newest-first. ``(rows, requests, exhausted, error)``.

    ``platformId`` is omitted on purpose: ``kaiba/ingest/stonkfun.py`` always sends it, so
    it sees one venue. Without it the same route is a census of every venue on the shared
    program. ``sort=old|oldest|asc`` all answer HTTP 500, which is why there is no way to
    walk forward from a cursor and the stop condition has to be a timestamp.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    reqs = 0
    exhausted = False
    for page in range(max_pages):
        if raw_pages is not None:
            if page >= len(raw_pages):
                exhausted = True
                break
            payload: Any = raw_pages[page]
        else:
            params: dict[str, Any] = {"sort": "new", "size": 100, "mintType": "default",
                                      "includeNsfw": "false"}
            if cursor:
                params["nextPageId"] = cursor
            got = get_json("raydium_launchpad", "launch.list", RAYDIUM_LIST, params=params,
                           headers={"user-agent": USER_AGENT}, timeout_s=30.0, retries=2,
                           wait_for_slot_s=30.0, conn=conn)
            reqs += 1
            if not got.ok:
                return rows, reqs, False, got.receipt.note or "unavailable"
            payload = got.data
        page_rows, cursor = parse_launchlab_page(payload)
        if not page_rows:
            exhausted = True
            break
        rows.extend(page_rows)
        oldest = min((int(dec(r.get("createAt")) or 0) for r in page_rows), default=0)
        if stop_before_ms is not None and oldest <= stop_before_ms:
            exhausted = True
            break
        if not cursor:
            exhausted = True
            break
    return rows, reqs, exhausted, None


def poll_launchlab(conn: Any = None, *, cfg: RadarConfig | None = None, census: bool = True,
                   now: int | None = None, raw_pages: Sequence[Any] | None = None,
                   mark_ms: int | None = None
                   ) -> tuple[list[Candidate], int, int, int, str | None]:
    """Day-0 venue discovery on a shared program.

    Two modes, because they answer different questions at different prices.

    ``census=True``  walks a full 24 hours and produces the lower-bound fee proxy. 67
                     requests on the measured feed. This is the only mode that can raise
                     an alert.
    ``census=False`` walks back to the stored high-water mark, ~8 pages at the measured
                     7,825 pools/day, and writes registry rows only. Existence is not
                     news: 52 distinct platform configs appeared in 24 hours and most are
                     one-pool vanity deployments (DegenSafe, DIESEL, COPE Launchpad,
                     LASTSOL, WARRIORZ -- one pool each).
    """
    config = cfg or DEFAULT_CONFIG
    ts = now if now is not None else now_ms()
    window_ms = config.launchlab_census_window_h * 3_600_000
    if census:
        pages = config.launchlab_census_pages
        stop_at = ts - window_ms
    else:
        pages = config.launchlab_discover_pages
        stop_at = mark_ms if mark_ms is not None else ts - window_ms
    rows, reqs, exhausted, err = fetch_launchlab(conn, max_pages=pages,
                                                 stop_before_ms=stop_at, raw_pages=raw_pages)
    if err and not rows:
        return [], 0, 1, reqs, err
    agg = aggregate_launchlab(rows, window_ms=window_ms if census else ts - stop_at, now=ts)
    # A census that ran out of pages did not cover a day. Reporting its proxy as a daily
    # figure would understate every venue by an unknown amount, so the whole pass goes
    # silent rather than producing a confident wrong number.
    partial = census and not exhausted
    out: list[Candidate] = []
    for pubkey, acc in agg.items():
        proxy = acc["fee_proxy_usd"] if census else None
        out.append(
            Candidate(
                kind=KIND_PLATFORM_CONFIG,
                layer="launchlab_census" if census else "launchlab_discover",
                identity=pubkey,
                display_name=str(acc["name"] or pubkey)[:120],
                chain_slug="solana",
                value=proxy if not partial else None,
                unit="usd_per_day_proxy",
                basis=(EvidenceBasis.DERIVED if (proxy is not None and not partial)
                       else EvidenceBasis.UNAVAILABLE),
                meta={
                    "program": config.launchlab_program,
                    "venue_name": acc["name"],
                    "fee_rate_ppm": str(acc["fee_rate_ppm"]) if acc["fee_rate_ppm"] is not None else None,
                    "pools": acc["pools"],
                    "pools_priced": acc["pools_priced"],
                    "pools_unpriced": acc["pools_unpriced"],
                    "new_pool_volume_usd": str(acc["volume_usd"]),
                    "first_pool_ms": acc["first_pool_ms"],
                    "last_pool_ms": acc["last_pool_ms"],
                    "sample_mint": acc["sample_mint"],
                    "census_partial": partial,
                    "proxy_is_lower_bound": True,
                },
                silent=not census or partial,
            )
        )
    note = err
    if partial:
        note = (f"census hit the {pages}-page cap without covering "
                f"{config.launchlab_census_window_h}h; proxy suppressed")
    return out, 1, 1, reqs, note


# --------------------------------------------------------------------------------------
# the memory
# --------------------------------------------------------------------------------------


def _layer_has_baseline(conn: sqlite3.Connection, layer: str) -> bool:
    row = fetch_one(conn, "SELECT baseline_ms FROM radar_layer_health WHERE layer=?", (layer,))
    return bool(row and row["baseline_ms"])


def _advance_confirmation(previous_days: int, previous_day: str | None, day: str,
                          above_floor: bool) -> tuple[int, str | None]:
    """Consecutive UTC days above the floor.

    Counting days rather than observations is what stops a two-hourly layer from
    satisfying a three-day rule in six hours. A gap resets the streak: a layer that was
    down for a day cannot claim it saw three consecutive days.
    """
    if not above_floor:
        return 0, None
    if previous_day == day:
        return max(1, previous_days), day
    if previous_day is not None and _next_day(previous_day) == day:
        return previous_days + 1, day
    return 1, day


@dataclass(frozen=True, slots=True)
class Outcome:
    """What :func:`observe` decided about one candidate."""

    candidate: Candidate
    tier: int
    previous_tier: int
    new_row: bool
    baseline: bool
    reported: bool
    confirm_days: int
    reason: str
    tractability: Tractability | None = None
    headroom: Decimal | None = None
    rank_score: Decimal | None = None


def observe(conn: sqlite3.Connection, cand: Candidate, *, cfg: RadarConfig | None = None,
            oracles: OracleSets | None = None, now: int | None = None,
            baseline_mode: bool = False, may_report: bool = True) -> Outcome:
    """Fold one observation into the registry and decide whether it is news.

    The four gates, in the order they apply:

    1. A baseline row is never announced. The layer's first sweep sets every candidate's
       ``reported_tier`` to whatever tier it is already at, so 48 launchpads that have
       existed for years are recorded as known rather than announced.
    2. ``silent`` candidates never announce, whatever their tier.
    3. Tier must exceed the tier we last announced at. This is what makes it once.
    4. Tier 1 additionally needs :attr:`RadarConfig.confirm_days_required` consecutive
       UTC days above the floor. Tier 2 and above skip that, because the confirmation
       exists to filter floor-adjacent noise and 10x the floor is not that.
    """
    config = cfg or DEFAULT_CONFIG
    ts = now if now is not None else now_ms()
    day = utc_day(ts)
    key = cand.radar_key
    floor = config.floor_for(cand.unit)
    tier = tier_for(cand.value, floor, config.tier_multipliers)

    row = fetch_one(conn, "SELECT * FROM radar_registry WHERE radar_key=?", (key,))
    new_row = row is None
    prev_days = int(row["confirm_days"]) if row else 0
    prev_day = row["confirm_last_day"] if row else None
    prev_reported_tier = int(row["reported_tier"]) if row else 0
    baseline = bool(row["baseline"]) if row else baseline_mode
    confirm_days, confirm_day = _advance_confirmation(prev_days, prev_day, day, tier >= 1)

    best = dec(row["best_value"]) if row and row["best_value"] else None
    best_ms = int(row["best_seen_ms"]) if row and row["best_seen_ms"] else None
    if cand.value is not None and (best is None or cand.value > best):
        best, best_ms = cand.value, ts

    lane = native_lane(cand.chain_slug)
    tract: Tractability | None = None
    if oracles is not None:
        tract = assess_tractability(cand.chain_slug, oracles,
                                    chain_label=cand.meta.get("label"),
                                    chain_id=cand.meta.get("chain_id"))

    reported = False
    if baseline_mode:
        # Seeding. Record where this candidate already stands so only genuine movement
        # past that point can ever be news. This applies to rows another layer already
        # created, not just to new ones: the launchlab discovery poll writes a
        # platform_config row hours before the census first prices it, and the census's
        # own first pass must still be silent.
        prev_reported_tier = max(prev_reported_tier, tier)
        baseline = baseline or new_row
        reason = "baseline"
    elif not may_report:
        reason = "reporting_disabled"
    elif cand.silent:
        reason = "layer_is_registry_only"
    elif cand.value is None:
        reason = "no_evidence"
    elif tier <= prev_reported_tier:
        reason = "already_reported_at_or_above_this_tier"
    elif tier == 1 and confirm_days < config.confirm_days_required:
        reason = f"awaiting_confirmation_{confirm_days}_of_{config.confirm_days_required}_days"
    else:
        reported = True
        reason = "reported"

    fields: dict[str, Any] = {
        "radar_key": key,
        # Always present, because an upsert's INSERT arm has to satisfy every NOT NULL
        # column before SQLite reaches the conflict clause — but held out of `updates`
        # below, so re-observing a candidate can never rewrite when we first saw it.
        # Our own clock is the only first-seen we can trust: DefiLlama backfills a
        # chain's early daily series when the adapter lands, so a first-non-zero date
        # read out of history is the adapter's coverage start, not the venue's birth.
        "first_seen_ms": int(row["first_seen_ms"]) if row else ts,
        "kind": cand.kind,
        "layer": cand.layer,
        "identity": cand.identity,
        "display_name": cand.display_name,
        "chain_slug": cand.chain_slug,
        "native_lane": 1 if lane is not None else 0,
        "last_seen_ms": ts,
        "observations": (int(row["observations"]) if row else 0) + 1,
        "baseline": 1 if baseline else 0,
        "last_value": str(cand.value) if cand.value is not None else None,
        "last_unit": cand.unit,
        "last_basis": cand.basis.value,
        "last_floor": str(floor),
        "best_value": str(best) if best is not None else None,
        "best_seen_ms": best_ms,
        "confirm_days": confirm_days,
        "confirm_last_day": confirm_day,
        "tier": tier,
        "reported_tier": max(prev_reported_tier, tier) if reported else prev_reported_tier,
        "tractability": tract.verdict if tract else (row["tractability"] if row else None),
        "meta_json": jdump(cand.meta),
    }
    if reported:
        fields["reported_ms"] = ts
    updates = [f for f in fields if f not in {"radar_key", "first_seen_ms"}]
    upsert(conn, "radar_registry", fields, conflict=["radar_key"], update=updates)

    headroom = None if cand.value is None or floor <= 0 else cand.value / floor
    weight = config.tractability_weights[tract.weight_index] if tract else config.tractability_weights[2]
    rank = None if headroom is None else headroom * weight
    return Outcome(cand, tier, prev_reported_tier, new_row, baseline, reported,
                   confirm_days, reason, tract, headroom, rank)


def _verdict_sentence(cand: Candidate, tier: int, tract: Tractability | None,
                      headroom: Decimal | None, first_report: bool) -> str:
    what = "NEW" if first_report else "ESCALATION"
    size = "unpriced" if cand.value is None else f"{cand.value:,.0f} {cand.unit}"
    hx = "" if headroom is None else f" ({headroom:.1f}x floor, tier {tier})"
    if cand.unit == "usd_per_day_proxy":
        size += " [LOWER BOUND: new-pool census, measured 0.42x DefiLlama on StonkFun]"
    if cand.meta.get("pre_tge_no_token") is True:
        size += " [no token yet: watch, do not farm -- 0 of 652 rows cleared a $25 EV floor]"
    if tract is None:
        tail = "tractability not assessed"
    elif tract.actionable:
        tail = f"ACTIONABLE ({tract.verdict}): {tract.why()}"
    else:
        tail = f"RESEARCH ONLY, not actionable ({tract.verdict}): {tract.why()}"
    return f"{what} {cand.kind} {cand.display_name or cand.identity} at {size}{hx}. {tail}"


def record_find(conn: sqlite3.Connection, outcome: Outcome, *, cfg: RadarConfig | None = None,
                now: int | None = None) -> int:
    """Write the announcement row and put it on the bus. Returns the ``find_id``."""
    config = cfg or DEFAULT_CONFIG
    ts = now if now is not None else now_ms()
    cand = outcome.candidate
    floor = config.floor_for(cand.unit)
    tract = outcome.tractability
    prior = fetch_one(conn, "SELECT COUNT(*) AS n FROM radar_finds WHERE radar_key=?",
                      (cand.radar_key,))
    first_report = not (prior and int(prior["n"]))
    verdict = _verdict_sentence(cand, outcome.tier, tract, outcome.headroom, first_report)
    actionable = bool(tract.actionable) if tract else False
    cur = conn.execute(
        "INSERT INTO radar_finds (radar_key, reported_ms, kind, layer, identity, display_name, "
        "chain_slug, tier, value, unit, basis, floor, headroom, rank_score, tractability, "
        "actionable, verdict, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cand.radar_key, ts, cand.kind, cand.layer, cand.identity, cand.display_name,
            cand.chain_slug, outcome.tier,
            str(cand.value) if cand.value is not None else None,
            cand.unit, cand.basis.value, str(floor),
            str(outcome.headroom) if outcome.headroom is not None else None,
            str(outcome.rank_score) if outcome.rank_score is not None else None,
            tract.verdict if tract else UNKNOWN,
            1 if actionable else 0, verdict,
            jdump({**cand.meta, "first_report": first_report,
                   "tractability": tract.as_dict() if tract else None,
                   "confirm_days": outcome.confirm_days}),
        ),
    )
    emit(
        EventKind.HUNTER_FOUND,
        {
            "hunter": "radar",
            "radar_key": cand.radar_key,
            "radar_kind": cand.kind,
            "layer": cand.layer,
            "identity": cand.identity,
            "display_name": cand.display_name,
            # Free text, deliberately NOT the Chain enum. A newly discovered chain has no
            # lane and must still be recordable; `native_lane` says whether one exists.
            "chain_slug": cand.chain_slug,
            "native_lane": bool(native_lane(cand.chain_slug)),
            "tier": outcome.tier,
            "value": str(cand.value) if cand.value is not None else None,
            "unit": cand.unit,
            "basis": cand.basis.value,
            "floor": str(floor),
            "headroom": str(outcome.headroom) if outcome.headroom is not None else None,
            "rank_score": str(outcome.rank_score) if outcome.rank_score is not None else None,
            "tractability": tract.as_dict() if tract else None,
            "actionable": actionable,
            "verdict": verdict,
            "first_report": first_report,
        },
        # `chain=` takes a Chain, so a chain with no lane rides as None and the slug
        # travels in the payload. This is the enum blocker, handled rather than worked
        # around: see the module docstring and migration 028.
        chain=native_lane(cand.chain_slug),
        subject=cand.identity,
        level="warn" if actionable else "info",
        dedupe_key=f"radar:{cand.radar_key}:tier{outcome.tier}",
        conn=conn,
    )
    return int(cur.lastrowid or 0)


# --------------------------------------------------------------------------------------
# layers and the sweep
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layer:
    name: str
    kind: str
    poll: Callable[..., tuple[list[Candidate], int, int, int, str | None]]


LAYERS: dict[str, Layer] = {
    "llama_venues": Layer("llama_venues", "venue_fees", poll_venue_fees),
    "llama_chains": Layer("llama_chains", "chain_volume", poll_chain_volume),
    "launchlab_census": Layer(
        "launchlab_census", "shared_program",
        lambda conn=None, **kw: poll_launchlab(conn, census=True, **kw),
    ),
    "launchlab_discover": Layer(
        "launchlab_discover", "shared_program",
        lambda conn=None, **kw: poll_launchlab(conn, census=False, **kw),
    ),
}


def due_layers(conn: sqlite3.Connection, cfg: RadarConfig, *, force: bool = False,
               only: Iterable[str] | None = None) -> list[str]:
    names = [n for n in (list(only) if only else list(LAYERS)) if n in LAYERS]
    if force:
        return names
    ts = now_ms()
    out: list[str] = []
    for name in names:
        row = fetch_one(conn, "SELECT last_poll_ms FROM radar_layer_health WHERE layer=?", (name,))
        last = int(row["last_poll_ms"]) if row and row["last_poll_ms"] else 0
        if ts - last >= cfg.interval_for(name) * 1000:
            out.append(name)
    return out


def record_layer_health(conn: sqlite3.Connection, layer: str, kind: str, *, ok: bool,
                        count: int, new: int, reported: int, requests: int, interval_s: int,
                        error: str | None = None, baseline_ms: int | None = None) -> None:
    """``ok`` is passed in, never inferred from ``count == 0``.

    "DefiLlama answered and no venue crossed the floor this week" is the healthy case and
    the expected one -- the chain rule fires 5.1 times a year. A health table that scores
    that as a failure teaches the operator to ignore the one column that exists so they do
    not have to guess whether silence means anything.
    """
    ts = now_ms()
    row = fetch_one(conn, "SELECT fail_streak, total_reported, requests_total "
                          "FROM radar_layer_health WHERE layer=?", (layer,))
    fields: dict[str, Any] = {
        "layer": layer,
        "kind": kind,
        "interval_s": interval_s,
        "last_poll_ms": ts,
        "last_count": count,
        "last_new": new,
        "last_reported": reported,
        "total_reported": (int(row["total_reported"]) if row else 0) + reported,
        "requests_last": requests,
        "requests_total": (int(row["requests_total"]) if row else 0) + requests,
        "fail_streak": 0 if ok else (int(row["fail_streak"]) if row else 0) + 1,
        "last_error": redact_text(error)[:300] if error else None,
    }
    updates = [f for f in fields if f != "layer"]
    if ok:
        fields["last_ok_ms"] = ts
        updates.append("last_ok_ms")
    if baseline_ms is not None:
        fields["baseline_ms"] = baseline_ms
        updates.append("baseline_ms")
    upsert(conn, "radar_layer_health", fields, conflict=["layer"], update=updates)


def _cursor_mark(conn: sqlite3.Connection, layer: str) -> int | None:
    row = fetch_one(conn, "SELECT mark_ms FROM radar_cursor WHERE layer=?", (layer,))
    return int(row["mark_ms"]) if row and row["mark_ms"] else None


def _set_cursor(conn: sqlite3.Connection, layer: str, mark_ms: int) -> None:
    upsert(conn, "radar_cursor", {"layer": layer, "mark_ms": mark_ms, "updated_ms": now_ms()},
           conflict=["layer"], update=["mark_ms", "updated_ms"])


@dataclass
class SweepResult:
    layers: list[str] = field(default_factory=list)
    seen: int = 0
    new: int = 0
    reported: int = 0
    suppressed: int = 0
    requests: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    finds: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "layers": self.layers, "seen": self.seen, "new": self.new,
            "reported": self.reported, "suppressed": self.suppressed,
            "requests": self.requests, "errors": self.errors,
            "finds": [f["verdict"] for f in self.finds][:8],
        }


def sweep(conn: sqlite3.Connection | None = None, *, cfg: RadarConfig | None = None,
          only: Iterable[str] | None = None, force: bool = False,
          oracles: OracleSets | None = None, now: int | None = None,
          raw: dict[str, Any] | None = None) -> SweepResult:
    """Poll every due layer, fold the results into the registry, announce what is news.

    Nothing in here raises: the caller is the maintenance scheduler and an exception
    there stops every layer, which is the precise failure this is supposed to prevent.
    """
    c = conn or get_conn()
    config = cfg or DEFAULT_CONFIG
    ts = now if now is not None else now_ms()
    result = SweepResult()
    orc = oracles
    for name in due_layers(c, config, force=force, only=only):
        layer = LAYERS[name]
        first_ever = not _layer_has_baseline(c, name)
        kwargs: dict[str, Any] = {"cfg": config}
        if name != "llama_venues":
            kwargs["now"] = ts
        if raw is not None and name in raw:
            kwargs.update(raw[name])
        if name == "launchlab_discover" and "mark_ms" not in kwargs:
            kwargs["mark_ms"] = _cursor_mark(c, name)
        if name == "llama_chains" and "oracles" not in kwargs:
            if orc is None:
                orc = load_oracles(c, cfg=config)
                result.requests += orc.requests
            kwargs["oracles"] = orc
        try:
            cands, ok_n, total_n, reqs, err = layer.poll(c, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a layer must never take down the sweep
            detail = redact_text(f"{type(exc).__name__}: {exc}")[:300]
            log.warning("radar layer %s failed: %s", name, detail)
            record_layer_health(c, name, layer.kind, ok=False, count=0, new=0, reported=0,
                                requests=0, interval_s=config.interval_for(name), error=detail)
            result.errors[name] = detail
            continue
        result.requests += reqs
        if orc is None and any(x.kind == KIND_CHAIN for x in cands):
            orc = load_oracles(c, cfg=config)
            result.requests += orc.requests
        if orc is None:
            orc = oracles if oracles is not None else load_oracles(c, cfg=config)
            result.requests += orc.requests

        newly = 0
        announced = 0
        pending: list[Outcome] = []
        for cand in cands:
            out = observe(c, cand, cfg=config, oracles=orc, now=ts, baseline_mode=first_ever)
            result.seen += 1
            newly += 1 if out.new_row else 0
            if out.reported:
                pending.append(out)
        # Rank before announcing so the blast-radius cap keeps the biggest evidence, not
        # whichever candidate the provider happened to list first.
        pending.sort(key=lambda o: (o.rank_score if o.rank_score is not None else Decimal(0)),
                     reverse=True)
        for out in pending[: config.max_reports_per_sweep]:
            record_find(c, out, cfg=config, now=ts)
            announced += 1
            result.finds.append({
                "radar_key": out.candidate.radar_key,
                "identity": out.candidate.identity,
                "tier": out.tier,
                "rank_score": str(out.rank_score) if out.rank_score is not None else None,
                "actionable": bool(out.tractability.actionable) if out.tractability else False,
                "verdict": _verdict_sentence(out.candidate, out.tier, out.tractability,
                                             out.headroom, True),
            })
        overflow = max(0, len(pending) - config.max_reports_per_sweep)
        if overflow:
            # Rolled back so the next sweep offers them again; nothing is dropped.
            # A plain UPDATE, not an upsert: an upsert's INSERT arm would have to satisfy
            # every NOT NULL column before SQLite ever reaches the conflict clause.
            for out in pending[config.max_reports_per_sweep:]:
                c.execute(
                    "UPDATE radar_registry SET reported_tier = ?, reported_ms = NULL "
                    "WHERE radar_key = ?",
                    (out.previous_tier, out.candidate.radar_key),
                )
            result.suppressed += overflow
            emit(
                EventKind.SYSTEM,
                {"reason": "radar_report_cap", "layer": name, "suppressed": overflow,
                 "cap": config.max_reports_per_sweep,
                 "detail": "more candidates crossed in one sweep than the blast radius "
                           "allows; they are re-offered next sweep, but this many at once "
                           "usually means the provider re-slugged its catalogue"},
                level="warn", subject=name,
                dedupe_key=f"radar_cap:{name}:{ts // 3_600_000}", conn=c,
            )
        newest = max((int(dec(x.meta.get("last_pool_ms")) or 0) for x in cands), default=0)
        if name == "launchlab_discover" and newest:
            _set_cursor(c, name, newest)
        record_layer_health(
            c, name, layer.kind, ok=ok_n > 0, count=len(cands), new=newly,
            reported=announced, requests=reqs, interval_s=config.interval_for(name),
            error=err, baseline_ms=ts if first_ever else None,
        )
        if first_ever:
            log.info("radar baseline established for %s with %d candidates; reporting none",
                     name, len(cands))
        result.layers.append(name)
        result.new += newly
        result.reported += announced
        if err:
            result.errors[name] = err
    check_health(c, config)
    return result


# --------------------------------------------------------------------------------------
# health and reporting
# --------------------------------------------------------------------------------------


def layer_health(conn: sqlite3.Connection | None = None,
                 cfg: RadarConfig | None = None) -> list[dict[str, Any]]:
    """Every configured layer, including ones that have never run."""
    c = conn or get_conn()
    config = cfg or DEFAULT_CONFIG
    rows = {r["layer"]: r for r in fetch_all(c, "SELECT * FROM radar_layer_health")}
    ts = now_ms()
    out: list[dict[str, Any]] = []
    for name, layer in LAYERS.items():
        row = rows.get(name)
        last_ok = int(row["last_ok_ms"]) if row and row["last_ok_ms"] else None
        tolerance = config.dead_after_s(name)
        age = None if last_ok is None else (ts - last_ok) / 1000.0
        if row is None:
            state = "never_run"
        elif last_ok is None or (age is not None and age > tolerance):
            state = "dead"
        elif age is not None and age > tolerance / 2:
            state = "degraded"
        else:
            state = "ok"
        out.append({
            "layer": name, "kind": layer.kind, "state": state,
            "interval_s": config.interval_for(name), "dead_after_s": tolerance,
            "last_poll_ms": (row or {}).get("last_poll_ms"), "last_ok_ms": last_ok,
            "since_ok_s": None if age is None else int(age),
            "last_count": int((row or {}).get("last_count") or 0),
            "last_new": int((row or {}).get("last_new") or 0),
            "last_reported": int((row or {}).get("last_reported") or 0),
            "total_reported": int((row or {}).get("total_reported") or 0),
            "requests_last": int((row or {}).get("requests_last") or 0),
            "requests_total": int((row or {}).get("requests_total") or 0),
            "fail_streak": int((row or {}).get("fail_streak") or 0),
            "baseline_ms": (row or {}).get("baseline_ms"),
            "last_error": (row or {}).get("last_error"),
        })
    return out


def check_health(conn: sqlite3.Connection | None = None,
                 cfg: RadarConfig | None = None) -> list[dict[str, Any]]:
    """Emit for every layer that has stopped working.

    The chain rule fires about five times a year, so silence is the normal state and
    cannot be used as evidence that the radar is alive. DefiLlama paywalled ``/raises``
    and ``/emissions`` between two research passes this month; the same could happen to
    ``/overview/fees`` overnight, and it would look exactly like a quiet quarter.
    """
    c = conn or get_conn()
    bad = [h for h in layer_health(c, cfg) if h["state"] == "dead"]
    for item in bad:
        emit(
            EventKind.SYSTEM,
            {"reason": "radar_layer_dead", "layer": item["layer"],
             "since_ok_s": item["since_ok_s"], "dead_after_s": item["dead_after_s"],
             "fail_streak": item["fail_streak"], "last_error": item["last_error"],
             "detail": "a quiet radar and a market with no new venues look identical; "
                       "this one is quiet because it is broken"},
            level="error", subject=item["layer"],
            dedupe_key=f"radar_layer_dead:{item['layer']}:{now_ms() // 3_600_000}", conn=c,
        )
    return bad


def recent_finds(conn: sqlite3.Connection | None = None, limit: int = 50,
                 kind: str | None = None, actionable: bool | None = None) -> list[dict[str, Any]]:
    c = conn or get_conn()
    sql = "SELECT * FROM radar_finds"
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if actionable is not None:
        where.append("actionable = ?")
        params.append(1 if actionable else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY reported_ms DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(c, sql, params)
    for row in rows:
        row["payload"] = jload(row.pop("payload_json"), {})
    return rows


def watchlist(conn: sqlite3.Connection | None = None, limit: int = 30,
              cfg: RadarConfig | None = None) -> list[dict[str, Any]]:
    """Candidates that could still be announced but have not been, best evidence first.

    Exactly ``tier > reported_tier``: something we have seen move past where we last
    left it, still inside its confirmation window. This is the honest middle, and
    without it the only visible states are "silent" and "alert", so a candidate on day
    two of three looks identical to nothing at all.

    It deliberately does **not** list every venue sitting above the floor. On the live
    roster 492 venue rows are above it, essentially all of them years old and recorded
    at baseline, and a watchlist that included them would be a list of everything -- the
    same noise the baseline rule exists to remove, reappearing one table over.
    """
    c = conn or get_conn()
    rows = fetch_all(
        c,
        "SELECT * FROM radar_registry WHERE tier > reported_tier "
        "ORDER BY tier DESC, CAST(last_value AS REAL) DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["meta"] = jload(row.pop("meta_json"), {})
    return rows


def _ago(ms: int | None) -> str:
    if not ms:
        return "never"
    delta = (now_ms() - ms) / 1000.0
    if delta < 90:
        return f"{delta:.0f}s"
    if delta < 5400:
        return f"{delta / 60:.0f}m"
    if delta < 172_800:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86_400:.1f}d"


def radar_report(conn: sqlite3.Connection | None = None, limit: int = 25,
                 cfg: RadarConfig | None = None) -> str:
    """Health first, then actionable finds, then research-only, then the watchlist.

    Health goes at the top for the same reason it does in the early-alpha report: the
    first question about a silent feed is whether it is silent or broken, and no other
    table can answer that. Actionable and research-only are separated rather than merged
    and sorted, because "we cannot read this chain" is a different statement from "this
    is small" and collapsing them into one score would hide it.
    """
    c = conn or get_conn()
    config = cfg or DEFAULT_CONFIG
    health = layer_health(c, config)
    dead = [h for h in health if h["state"] in {"dead", "never_run"}]
    lines = [
        "# Discovery radar",
        "",
        "## Layer health",
        "",
        "| Layer | Kind | State | Last OK | Every | Saw | New | Reported | Req last | Req total |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in health:
        lines.append(
            "| {l} | {k} | {s} | {ok} | {iv}s | {c} | {n} | {r} | {rq} | {rt} |".format(
                l=item["layer"], k=item["kind"], s=item["state"].upper(),
                ok=_ago(item["last_ok_ms"]), iv=item["interval_s"], c=item["last_count"],
                n=item["last_new"], r=item["total_reported"], rq=item["requests_last"],
                rt=item["requests_total"],
            )
        )
    if dead:
        lines += [
            "",
            f"**{len(dead)} of {len(health)} layers are not producing.** "
            + ", ".join(f"`{d['layer']}` ({d['state']}: {d['last_error'] or 'no error recorded'})"
                        for d in dead)[:600],
            "",
            "The chain rule fires about five times a year, so silence proves nothing on its "
            "own. Treat the layers above as absent evidence, not as evidence of absence.",
        ]

    for title, want in (("Actionable finds", True), ("Research only - we cannot read these", False)):
        rows = recent_finds(c, limit=limit, actionable=want)
        lines += [
            "",
            f"## {title}",
            "",
            "| When | Kind | What | Chain | Tier | Evidence | Floor | Headroom | Tractability |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        if not rows:
            lines.append("| - | - | nothing yet | - | - | - | - | - | - |")
        for row in rows:
            value = row["value"]
            lines.append(
                "| {w} | {k} | {n} | {ch} | {t} | {v} | {f} | {h} | {tr} |".format(
                    w=_ago(row["reported_ms"]), k=row["kind"],
                    n=str(row["display_name"] or row["identity"])[:30],
                    ch=row["chain_slug"] or "-", t=row["tier"],
                    v=("UNPRICED" if value is None
                       else f"{Decimal(value):,.0f} {row['unit']}"),
                    f=f"{Decimal(row['floor']):,.0f}",
                    h="-" if row["headroom"] is None else f"{Decimal(row['headroom']):.1f}x",
                    tr=row["tractability"],
                )
            )
        if want is False and rows:
            lines += [
                "",
                "These are ranked and recorded, not hidden, but the agent has no way to read "
                "them: no gmgn-cli lane, and no safety oracle that answers for the chain. A "
                "venue we cannot screen is not an opportunity for us however large it is.",
            ]

    holding = watchlist(c, limit=limit, cfg=config)
    lines += [
        "",
        "## Watchlist - seen, below the bar or inside the confirmation window",
        "",
        "| Kind | What | Chain | Tier | Last | Floor | Confirm | Seen | Basis |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    if not holding:
        lines.append("| - | - | - | - | - | - | - | - | - |")
    for row in holding:
        lines.append(
            "| {k} | {n} | {ch} | {t} | {v} | {f} | {c}/{req} | {o} | {b} |".format(
                k=row["kind"], n=str(row["display_name"] or row["identity"])[:30],
                ch=row["chain_slug"] or "-", t=row["tier"],
                v=("UNPRICED" if row["last_value"] is None
                   else f"{Decimal(row['last_value']):,.0f}"),
                f=("-" if row["last_floor"] is None else f"{Decimal(row['last_floor']):,.0f}"),
                c=row["confirm_days"], req=config.confirm_days_required,
                o=row["observations"], b=row["last_basis"],
            )
        )
    unpriced = fetch_one(
        c, "SELECT COUNT(*) AS n FROM radar_registry WHERE last_basis = ?",
        (EvidenceBasis.UNAVAILABLE.value,),
    )
    lines += [
        "",
        f"{int((unpriced or {}).get('n') or 0)} registry rows carry no priced evidence. That is "
        "not zero and it is not small: it is a candidate whose quote asset we could not price, "
        "or a chain whose series the provider would not serve. A zero there would read as 'too "
        "small to care', which is exactly how the next StonkFun gets missed.",
    ]
    return "\n".join(lines)


__all__ = [
    "CHAIN_RULE_CHAIN_DAYS",
    "CHAIN_RULE_FIRES_WITHOUT_AGE_GATE",
    "CHAIN_RULE_FIRES_WITH_AGE_GATE",
    "DEFAULT_CONFIG",
    "FULL",
    "GOPLUS_SUPPORTED_CHAINS_2026_09_21",
    "KIND_CHAIN",
    "KIND_PLATFORM_CONFIG",
    "KIND_VENUE",
    "LAUNCHLAB_CENSUS_CONFIGS_2026_09_21",
    "LAUNCHLAB_CENSUS_PAGES_2026_09_21",
    "LAUNCHLAB_CENSUS_POOLS_2026_09_21",
    "LAUNCHLAB_PROXY_RATIO_BONKFUN",
    "LAUNCHLAB_PROXY_RATIO_STONKFUN",
    "LAYERS",
    "OPAQUE",
    "PARTIAL",
    "PROVENANCE",
    "SOLANA_LAUNCHPAD_FEE_POOL_USD_2026_09_21",
    "SOLANA_LAUNCHPAD_NEXT_BELOW_USD_2026_09_21",
    "UNKNOWN",
    "VENUE_FEE_FLAT_HIGH_USD",
    "VENUE_FEE_FLAT_LOW_USD",
    "Candidate",
    "ChainVerdict",
    "Knob",
    "Layer",
    "OracleSets",
    "Outcome",
    "RadarConfig",
    "SweepResult",
    "Tractability",
    "aggregate_launchlab",
    "assess_tractability",
    "chain_rule",
    "chain_volume_30d",
    "check_health",
    "dec",
    "due_layers",
    "fetch_launchlab",
    "layer_health",
    "load_oracles",
    "native_lane",
    "observe",
    "parse_chain_series",
    "parse_launchlab_page",
    "parse_venue_fees",
    "poll_chain_volume",
    "poll_launchlab",
    "poll_venue_fees",
    "radar_report",
    "recent_finds",
    "record_find",
    "record_layer_health",
    "sweep",
    "tier_for",
    "utc_day",
    "watchlist",
]
