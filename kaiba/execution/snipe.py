"""launch-snipe: enter a launch in its first seconds when what we know about it says so.

What fires, and why these and nothing else (the OWNER DIRECTIVE: no unmeasured filter goes
live, measure on the scanned population first):

* **Who launched it.** ``deployer_stats`` (``intelligence/deployer.py``) is the only entry
  split on our own tape that composes into an edge: MEASURED 2026-09-22 on sol, deployers
  with a prior >=2x launch reach 2x at 17.0% (low volume) / 19.2% (mid) against a 13.2%
  baseline, and spam factories with an all-dud record reach it at 5.3%
  (``deployer-record-edge``). Those labels are params, not code.
* **Who the owner names.** A developer-wallet watchlist and a name/ticker watchlist, the same
  two target kinds as Vanguard's Sniper mode.
* **Where.** Venues are params. MEASURED (``mooner-launchpad-edge``): LaunchLab (GMGN's
  ``ray_launchpad``) lift 2.69 and pons_v2 2.53 against pump.fun 1.02.

What vetoes on Robinhood -- the two exclusions the pons-robinhood research REPLICATED out of
sample (``lanes.PONS_TAX_PROVENANCE``; REFUTATION-pons-tax-entry.md):

* an outside buyer in the launch's first two seconds who paid NO snipe tax (the deployer
  exempted it; tokens with one returned -9.18% / -7.00% train / held out);
* a developer atomic buy under 0.03 ETH with no outside demand in the first 2 s
  (-3.65% / -6.28%).

When: on Robinhood not before the chain itself shows the anti-sniper tax at or under
``max_entry_tax_bps`` (default 0, i.e. +3 s by block time: 9900 / 618 / 19 / 0 bps). The
order goes through GMGN, whose landing time we do not control, so we only ever start once
the tax is already at the limit: time only moves forward and the tax only falls, so the
fill cannot pay more than the limit. It is also the earliest moment
``viability.read_pons_venue`` will price the token for protection.

How it trades: THROUGH THE ENGINE, never around it. A chosen launch gets its DYOR dossier
built here (the engine refuses "no dossier"), waits for the ingest service's ``tokens`` row
(the launchpad allowlist reads it), and is then recorded as an ordinary ``signals`` row
(``lanes.record``). ``engine.run_loop`` decides it under every gate it applies to any lane --
risk, size, daily stop, launchpad allowlist, protectability -- and a LIVE plan is submitted
by ``ops.execute_planned`` through ``executor.submit``. This module never writes ``tokens``,
``decisions``, ``orders`` or ``positions`` and never calls GMGN's swap.

What it measures, on every launch it sees (sol pump.fun sampled), fire or skip: an exact
paper entry at the moment we would enter (``eth_simulateV1`` against the live curve on
Robinhood; the pump.fun curve model on sol) and its value at fixed horizons, in
``snipe_observations``. That table is the evidence the lane's own params are judged on.

Budgets. Robinhood reads go through the ``robinhood-rpc`` limiter (shared with protection's
EVM price reads) at ENTRY / DISCOVERY priority so a live stop (EXIT) always goes first.
Dossiers -- each one spends GMGN limiter capacity -- are capped per hour.

**BSC (Flap), added 2026-10-05, PAPER ONLY.** Owner: "enable bnb snipes", in paper until a
forward result justifies money. Launches come from the ``tokens`` rows the Flap listener
writes (``ingest/flap.py``, tailed by ``launch_feed.tail_bsc``). ~970 launches an hour, so
the chain is read only for a launch with alpha (a watched dev or name, a deployer record)
or a 1-in-``measure_bsc_every`` sample: ONE batched read of the portal
(:func:`flap_calls`) answers every guard. Before any of it reaches the engine:

* the quote must be BNB (``quote_not_native``): MEASURED 45% of launches are quoted in an
  ERC-20 (BNCB, tokenised equities) that protection cannot price;
* the token's own buy and sell tax (``getTokenV8Safe`` words 12/13) within
  ``max_entry_tax_bps`` (``token_tax``): 42% of launches are 8.99%/8.99% spam;
* the Buy Quota (``maxBuyPerOrigin``) at least what the largest size we could send buys
  (``buy_quota_below_size``): Flap REFUNDS the excess of a capped buy instead of reverting,
  so a capped fill would book a size and a cost it never had; and the venue's own
  ``quoteExactInput`` within ``max_quote_shortfall_bps`` of the curve (any cap we do not
  model shows up there);
* protection must be able to price it: the record must pass the same cross-checks
  ``evm_price.read_flap`` applies (``unpriceable``), and a BNB price must be on the
  ``native_prices`` books, which is what an exit's ``min_out`` needs (``no_native_price``;
  2026-09-22: a bsc position that could buy and not sell for 53 minutes).

The venue quote fails CLOSED (``venue_quote_unread``) and is two-sided
(``quote_short_of_curve`` / ``quote_above_curve``): a reverted or unread
``quoteExactInput`` is exactly when a buy would be restricted, and an ACTIVE Buy Quota's
return layout has never been observed on chain, so the quote is the only measured witness
of a cap.

The paper entry is booked as a live buy would land, not at the decision: the curve and the
venue's quote are re-read ``bsc_exec_latency_ms`` after it (:func:`read_flap_fill`), the
tokens are the SMALLER of quote and model, and GMGN's commission
(``bsc_router_bps_per_leg``) comes off the input before the venue sees it. Marks charge it
again on the sell with the Flap fee and the token's sell tax, and a graduated token is
marked on its PancakeSwap V2 pair at every horizon (:func:`mark_bsc`), not frozen at its
curve end.

The result that would justify money is written down before it is read
(:data:`BSC_ARMING_CRITERION`) and is judged on ``snipe_observations``, which no live gate
censors -- never on the paper broker's twins, which exist only when the live gate passed.

And whatever the config says, a bsc snipe is never sent live: :func:`paper_only_refusal`
refuses to signal unless the engine's ``live_launchpads_by_chain`` is exactly ``{bsc: []}``
(every entry a SHADOW twin), and the engine enforces the same rule itself at the money
boundary (``engine.PAPER_ONLY_LANE_CHAINS``), so neither a config edit, nor a signal
recorded before one, nor another producer can put money on one. Arming bsc live is a code
change, after GMGN is seen to route a seconds-old Flap token and a live fill's booked cost
is checked against its receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import ensure_db, fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Lane, Signal, digest, now_ms
from kaiba.execution import evm_price as ep
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import robinhood as rh

log = logging.getLogger(__name__)

LANE_VALUE = "launch-snipe"
#: The limiter bucket Robinhood reads are charged to. Default: the shared ``robinhood-rpc``
#: bucket, where protection's EXIT reads always go first. ``params.rpc_bucket`` can move the
#: lane to its own bucket (an unconfigured name gets the limiter's 1/s default).
RPC_BUCKET: list[str] = [rh.PROVIDER]
#: The limiter bucket BSC (Flap) reads are charged to: NOT ``rpc``, which carries
#: protection's bsc price reads, so a 429 on a snipe read bans this bucket and not the stops.
#: Unconfigured, it gets the limiter's default (1 call/s, 2 in flight). ``params.bsc_rpc_bucket``.
BSC_RPC_BUCKET: list[str] = ["bsc-snipe-rpc"]
#: Chains on which this module never lets an entry go live, whatever the config says
#: (:func:`paper_only_refusal`). bsc: the owner's paper-first rule (2026-10-05), and two things
#: no measurement has settled -- whether GMGN routes a seconds-old Flap token, and whether a
#: live fill's booked cost (``report.input_amount``) is the spend when Flap refunds part of a
#: buy (a quota token would; our fills have never touched one).
PAPER_ONLY_CHAINS: frozenset[Chain] = frozenset({Chain.BSC})
#: The only quote assets a bsc snipe accepts: native BNB (the zero address, read as ``None``)
#: or WBNB. Everything else is refused (``quote_not_native``).
BSC_NATIVE_QUOTES: frozenset[str | None] = frozenset({None, ep.BSC_WRAPPED_NATIVE})
#: The most eth_calls one bsc mark batch sends (review 2026-10-05: an unbounded batch after a
#: backlog was up to 100 calls, ~2.6k CU, in one request on the endpoint's budget).
BSC_MARK_BATCH_MAX = 20

#: The forward result that would justify money on a bsc snipe, WRITTEN BEFORE ANY RESULT IS
#: READ (review 2026-10-05; the owner directive: measure, then arm). Judged on
#: ``snipe_observations`` -- every fired launch is recorded there whether or not a live gate
#: would have let a twin open, so no daily stop, halt or free balance censors the sample --
#: never on the paper broker's twin positions. Changing a number here after results exist
#: is changing the test, and the work log must say so.
BSC_ARMING_CRITERION: dict[str, Any] = {
    "population": ("snipe_observations with chain='bsc', fire=1 and an entry booked by read_flap_fill "
                   "(entry_basis 'flap_fill:*'), i.e. bsc_exec_latency_ms after the decision"),
    "return": ("marks_json['3600'].value / entry_quote_in - 1: the 60-minute mark, net of GMGN's commission "
               "and the Flap fee on both legs and the token's taxes; a graduated row valued on its "
               "PancakeSwap V2 pair; a row with no 60-minute value counts as -100%"),
    "min_n": 100,
    "min_distinct_utc_days": 7,
    "pass": ("mean return > 0 AND the 95% bootstrap lower bound of the mean > 0 "
             "(10,000 resamples of tokens)"),
    "not_evidence": ("peak_ratio (the best of three samples, optimistic by construction), paper-broker twins "
                     "(censored by the live gate), any sampled (fire=0) row"),
}
TABLE = "snipe_observations"

ZERO = "0x" + "0" * 40
#: MEASURED (``lanes.PONS_SNIPE_TAX_RUNGS_BPS``): 9900 / 618 / 19 bps at 0 / 1 / 2 s, 0 after.
PONS_TAX_RUNGS_BPS: dict[int, int] = {0: 9900, 1: 618, 2: 19}
PONS_TAX_SECONDS = 3
#: MEASURED: ``feeBps()`` = 100 on 250/250 curves; CurveBuy's ``fee`` word = protocol fee + snipe toll.
PONS_PROTOCOL_FEE_BPS = 100
PONS_CREATOR_TAX_SELECTOR = "0xc1bb8901"  # creatorTaxBps(), as viability.py reads it
PONS_BUY_SELECTOR = rh.SELECTOR_BUY  # buy(uint256 quoteIn,uint256 minTokensOut,address recipient)
#: A random, never-funded address paper buys are simulated from (balance by state override).
PAPER_WHO = "0x5ab1e0000000000000000000000000000000c0de"

DEFAULT_PARAMS: dict[str, Any] = {
    "chains": ["robinhood", "sol"],
    "venues": {"robinhood": ["pons"], "sol": ["launchlab", "pump.fun"], "bsc": ["flap"]},
    "dev_watchlist": [],
    "name_watchlist": [],
    "fire_on_records": ["low/runner", "mid/runner"],
    "never_records": ["spam/all_dud"],
    "max_entry_tax_bps": 0,
    "exempt_buyer_veto": True,
    "exempt_toll_bps": 100,
    "dev_atomic_veto_wei": 3 * 10**16,
    "veto_demand_window_s": 2,
    "watch_strength": 0.75,
    "record_strength": 0.72,
    "max_snipes_per_day": {"robinhood": 10, "sol": 10, "bsc": 5},
    "max_dossiers_per_hour": 40,
    "dossier_budget_by_chain": {"bsc": 10},
    "token_row_wait_s": 20,
    "measure_sol_every": 20,
    "measure_bsc_every": 20,
    "bsc_max_launch_age_s": 60,
    "max_quote_shortfall_bps": 200,
    "bsc_rpc_bucket": "bsc-snipe-rpc",
    "bsc_exec_latency_ms": 10_000,
    "bsc_router_bps_per_leg": 100,
    "bsc_feed_max_cu_per_day": 2_500_000,
    "bsc_resolve_sender": True,
    "paper_size": {"robinhood": 16_700_000_000_000_000, "sol": 370_000_000, "bsc": 129_420_000_000_000_000},
    "mark_horizons_s": [300, 900, 3600],
    "sol_exec_latency_ms": 4000,
    "chain_wait_max_s": 20,
    "chain_clock_margin_ms": 250,
    "rpc_bucket": "robinhood-rpc",
    "trusted_early_chains": ["robinhood"],
    "trusted_early_min": 1,
    "trusted_signer_window_s": 2,
    "proven_max_cohort_age_s": 259_200,
    "max_inflight": 64,
}

#: Every number above, and what would settle it. ``tests/test_snipe_params.py`` pins that
#: each param has an entry.
PARAMS_PROVENANCE: dict[str, str] = {
    "chains": ("OWNER 2026-10-03: Solana and Robinhood. bsc is SUPPORTED since 2026-10-05 (owner: 'enable bnb "
               "snipes', paper first) and listed by config, not by this default: listing it also switches on the "
               "Flap listener (ingest 'flap', ~38M Alchemy CU/month at the measured launch rate)."),
    "venues": ("MEASURED mooner-launchpad-edge (launchlab 2.69, pons_v2 2.53, flap in the 2.3-2.7x group); pump.fun "
               "kept for record/watch fires only via fire rules. bsc: flap is the only bsc venue read."),
    "dev_watchlist": "OWNER: wallets whose launches to snipe; empty until the owner names some.",
    "name_watchlist": "OWNER: names/tickers to snipe; empty until the owner names some.",
    "fire_on_records": ("A list (all chains) or {chain: [labels]}. RE-MEASURED 2026-10-04 with numeric peaks on one "
                        "price source, newest week, reach-2x observed/expected: sol mid/runner 1.26 (n=1,434), "
                        "low/runner 1.09 (n=337); robinhood low/runner 1.10, mid/runner 0.80 (n=594). The 17.0%/19.2% "
                        "vs 13.2% figures were a TEXT-max artefact."),
    "never_records": "MEASURED deployer-record-edge: spam/all_dud 5.3% reach 2x, 0.4% 5x (n=266).",
    "max_entry_tax_bps": ("A number (all chains) or {chain: bps}. robinhood: DERIVED 0 = start only once the "
                          "anti-sniper toll is gone; GMGN cannot time a +1 s / +2 s landing. bsc: the token's own "
                          "buy AND sell tax (getTokenV8Safe words 12/13) must both be at or under it; default 0. "
                          "MEASURED 2026-10-05 graduate taxes (buy,sell): (0,100) 67, (100,100) 32, (200,200) 13, "
                          "(300,300) 6 of 120; launches 8.99%/8.99% are spam (490 of 1,146 in an hour)."),
    "exempt_buyer_veto": "MEASURED REFUTATION s.4: exempt first-second buyer -> -9.18% / -7.00% train / held out.",
    "exempt_toll_bps": "DERIVED: a buy in second 0 or 1 owes 9900 or 618 bps; under 100 bps it was exempted.",
    "dev_atomic_veto_wei": "MEASURED line4 s.4: dev atomic buy < 0.03 ETH and no demand in 2 s -> -3.65% / -6.28%.",
    "veto_demand_window_s": "MEASURED: the 2 s window of the same screen.",
    "watch_strength": "DERIVED: 0.75 -> score 75, the ladder's bottom rung (flat size, owner directive).",
    "record_strength": "DERIVED: 0.72 -> score 72, the bottom rung; above 0.70 so it sizes at all.",
    "max_snipes_per_day": ("INVENTED: 10 per chain per UTC day while the lane is unmeasured; settled by "
                           "snipe_observations. bsc 5: paper twins, each one a dossier and a protected position."),
    "max_dossiers_per_hour": "INVENTED: each dossier spends GMGN limiter capacity shared with live stops.",
    "dossier_budget_by_chain": ("INVENTED: {chain: n} gives a chain its OWN hourly dossier budget instead of "
                                "max_dossiers_per_hour's shared one, so paper bsc fires cannot spend the dossiers the "
                                "live chains need. bsc 10/h."),
    "token_row_wait_s": "MEASURED: the RH poller records a launch at p50 6.5 s / p90 11.0 s.",
    "measure_sol_every": "INVENTED: 1 in 20 pump.fun launches measured (all LaunchLab); RPC budget.",
    "measure_bsc_every": ("INVENTED: 1 in 20 Flap launches with no alpha is read and paper-measured. DERIVED cost: "
                          "~970 launches/h MEASURED 2026-10-05 -> ~48 reads/h x 5 eth_calls (~26 CU each, ~4.5M CU/month) "
                          "plus three 1-call marks each (~2.7M): ~7M Alchemy CU/month, plus the 2-call fill re-read "
                          "of each priceable one (<= ~1.8M) and a 1-call DEX mark per graduate. Only ~12.7% of launches "
                          "are BNB-quoted under 5% tax, so ~6 tradeable measured an hour. All on BSC_SNIPE_RPC_URL."),
    "bsc_max_launch_age_s": ("DERIVED: the Pons rule (a backfilled launch over 60 s old is not a snipe). Flap launch "
                             "time is the event's block second; the listener writes ~1 s after it."),
    "max_quote_shortfall_bps": ("DERIVED: refuse when the portal's own quoteExactInput for the paper size is more than "
                                "this below the curve model (fee + buy tax off the input). The model matched the venue "
                                "to ~0.01% at 1/3/8.99% tax (3 points, MEASURED 2026-10-05); a 2% Buy Quota cap showed "
                                "as 77%. 200 bps sits between."),
    "bsc_rpc_bucket": "DERIVED: its own limiter bucket, so a 429 on a snipe read never bans protection's 'rpc' bucket.",
    "bsc_exec_latency_ms": ("DERIVED, not measured: the paper entry is re-read this long after the decision, as a live "
                            "buy would land. hand_to_engine builds a dossier first (~8 s MEASURED, this module's "
                            "hand_to_engine doc) and execute_planned runs every 5 s (config/schedule.yaml), so ~10 s. "
                            "Settled by the first live bsc fill's block against its launch block."),
    "bsc_router_bps_per_leg": ("= viability.ROUTER_BPS_PER_LEG: GMGN's commission, 1% a leg per the 2026-09-21 execution "
                               "review, UNVERIFIED. Charged on the paper buy (off the input) and on every mark's sell, "
                               "so the paper result is not flattered by a cost the live trade pays."),
    "bsc_feed_max_cu_per_day": ("DERIVED: the Flap listener (ingest/flap.py) closes its socket when its estimated CU for "
                                "the UTC day reaches this, and reconnects the next day with a cursor backfill. ~2x the "
                                "MEASURED average (~1.27M CU/day = 38M/month at ~970 launches/h, bursts to 2,228/h). "
                                "0 = no ceiling. The lead sets it from the Alchemy plan's real cap."),
    "bsc_resolve_sender": ("DERIVED (review 2026-10-05): the Flap creator word is a launcher contract on ~35% of "
                           "launches; one eth_getTransactionByHash a launch (17 CU, ~12M CU/month) keys tokens.creator "
                           "on the real sender so deployer records are per dev, not per launcher."),
    "paper_size": ("DERIVED: each chain's min position (risk.yaml 10-03: RH 0.0167 ETH, sol 0.37 SOL; bsc 0.12942 BNB, "
                   "the repo's chains.bsc.min_position_base_units)."),
    "mark_horizons_s": "INVENTED: 5 / 15 / 60 min; quiet-token-is-terminal says 88% never trade after 1 h.",
    "sol_exec_latency_ms": "INVENTED: GMGN entry delay assumed for the paper entry's moment; settle from fills.",
    "chain_wait_max_s": "DERIVED: give up on the RH tax wait if the chain head stalls this long.",
    "rpc_bucket": "DERIVED: share protection's bucket (EXIT outranks ENTRY/DISCOVERY); ~5 reads a launch, ~25 launches/h.",
    "chain_clock_margin_ms": "DERIVED: ~10 blocks share each whole-second timestamp, so the head shows the new second within ~100 ms of it starting.",
    "trusted_early_chains": "OWNER 2026-10-04 (via the lead): robinhood first. sol is SUPPORTED since 2026-10-04 (ingest 'sol_wallets', kaiba/ingest/wallet_stream_sol.py, on the owner's Alchemy plan) and is added in the box config by the lead after review; the default stays robinhood. MEASURED: Alchemy logsSubscribe delivered p50 ~22 s after the slot, so a sol fire needs the buy on the tape before the decision moment. bsc is NOT supported: no bsc wallet stream feeds the tape, so a bsc entry here would read an empty early book as 'no trusted buyer'.",
    "trusted_early_min": "OWNER 2026-10-04 (via the lead): at least one trusted_copy or proven wallet among the early buyers. UNMEASURED; settled by rule 'trusted_early:*' in snipe_observations.",
    "trusted_signer_window_s": "DERIVED: wallet_stream books a trusted router trade ~0.7 s after its block (lead, 2026-10-04); signers are read only for buys newer than this. 0 = never.",
    "proven_max_cohort_age_s": "DERIVED: the age limit lanes._proven_cohort applies (72 h), so a stalled refresh job cannot leave a stale cohort trusted.",
    "max_inflight": "INVENTED: launches handled at once; more are dropped and counted (stats 'dropped_backlog'). MEASURED box 2026-10-04: ~190 Pons + ~1.3k pump.fun launches/h, 6 worker threads (nproc 2).",
}


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


def lane() -> Lane:
    """The ``launch-snipe`` Lane value. It is added to ``kaiba/core/schemas.py`` by the lead."""
    try:
        return Lane(LANE_VALUE)
    except ValueError as exc:
        raise RuntimeError(f"Lane {LANE_VALUE!r} is not in kaiba.core.schemas.Lane yet") from exc


def params(cfg: Any = None) -> dict[str, Any]:
    """DEFAULT_PARAMS overlaid with ``config/risk.yaml`` ``lanes.launch-snipe.params``."""
    out = dict(DEFAULT_PARAMS)
    try:
        c = cfg or get_risk()
        out.update(dict(c.lane(lane()).params or {}))
    except Exception as exc:  # noqa: BLE001 - defaults are the conservative reading
        log.debug("launch-snipe params: defaults only (%s)", exc)
    return out


def _per_chain(value: Any, chain: Chain, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(chain.value, default)
    return value if value is not None else default


# --------------------------------------------------------------------------------------
# evidence about the launch
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Record:
    """A deployer's prior record, read by WALLET (the token is seconds old and not in stats)."""

    wallet: str | None
    launches: int
    prior_scored: int
    prior_runners: int
    basis: EvidenceBasis
    note: str = ""

    @property
    def label(self) -> str:
        if self.basis is EvidenceBasis.UNAVAILABLE:
            return "unknown"
        from kaiba.intelligence.deployer import MID_LAUNCHES, SPAM_LAUNCHES

        band = "spam" if self.launches >= SPAM_LAUNCHES else "mid" if self.launches >= MID_LAUNCHES else "low"
        rec = "no_prior" if self.prior_scored <= 0 else "runner" if self.prior_runners > 0 else "all_dud"
        return f"{band}/{rec}"


def record_for(conn: sqlite3.Connection, chain: Chain, wallet: str | None, *, at_ms: int | None = None) -> Record:
    """``deployer_stats`` for ``wallet``. Same staleness rule as ``deployer.lookup``; no
    self-exclusion is needed because a seconds-old token has not been scored."""
    if not wallet:
        return Record(None, 0, 0, 0, EvidenceBasis.UNAVAILABLE, "no creator")
    try:
        from kaiba.intelligence.deployer import STALE_AFTER_S, ensure_table

        ensure_table(conn)
        row = fetch_one(
            conn,
            "SELECT launches, scored, runners, computed_ms FROM deployer_stats WHERE chain=? AND wallet=?",
            (chain.value, wallet),
        )
    except (sqlite3.Error, ImportError) as exc:
        return Record(wallet, 0, 0, 0, EvidenceBasis.UNAVAILABLE, f"stats unreadable: {exc}")
    if row is None:
        # Never launched before, as far as the stats know: a first launch, not "unknown".
        return Record(wallet, 0, 0, 0, EvidenceBasis.DERIVED, "not_in_stats")
    age_s = ((at_ms or now_ms()) - int(row["computed_ms"])) / 1000
    if age_s > STALE_AFTER_S:
        return Record(wallet, 0, 0, 0, EvidenceBasis.UNAVAILABLE, f"stats stale by {age_s:.0f}s")
    return Record(wallet, int(row["launches"]), int(row["scored"]), int(row["runners"]),
                  EvidenceBasis.DERIVED, f"age{age_s:.0f}s")


def name_hits(launch: lf.Launch, watch: Iterable[Any]) -> list[str]:
    """Which name/ticker targets this launch matches (case and surrounding spaces ignored)."""
    hits: list[str] = []
    for t in watch or []:
        if isinstance(t, str):
            t = {"text": t, "exact": True, "field": "either"}
        if not isinstance(t, Mapping):
            continue
        want = str(t.get("text") or "").strip().lower()
        if not want:
            continue
        fld = str(t.get("field") or "either")
        values = [launch.name] if fld == "name" else [launch.symbol] if fld == "symbol" else [launch.name, launch.symbol]
        exact = t.get("exact", True) is not False
        if any((str(v).strip().lower() == want) if exact else (want in str(v).strip().lower()) for v in values if v):
            hits.append(want)
    return hits


@dataclass(slots=True)
class EarlyBook:
    """Robinhood: what happened on the curve in the launch's first seconds."""

    dev: str | None = None
    dev_buy_wei: int | None = None
    outside_buys_window: int = 0
    exempt_buyers: list[str] = field(default_factory=list)
    #: Every address an outside buy names (``CurveBuy`` trader and recipient), lower-case.
    buyers: set[str] = field(default_factory=set)
    #: The outside buys' transactions, for their signers (a router trade names the router),
    #: with each one's block second when known.
    buy_txs: list[str] = field(default_factory=list)
    buy_tx_ts: dict[str, int] = field(default_factory=dict)
    head_ts_s: int | None = None
    read: bool = False
    note: str = ""


def early_book(launch: lf.Launch, logs: Sequence[Mapping[str, Any]], *, tx_from: str | None,
               block_ts: Mapping[int, int], demand_window_s: int, exempt_toll_bps: int) -> EarlyBook:
    """Classify the curve's first buys. Pure: the caller fetched the logs and block times.

    An OUTSIDE buy is any buy not in the launch transaction. It is EXEMPT when it landed in
    second 0 or 1 (where the toll is 9900 / 618 bps) and paid under ``exempt_toll_bps``.
    """
    book = EarlyBook(dev=tx_from, read=True)
    if launch.launched_ms is None:
        book.read = False
        book.note = "launch_time_unknown"
        return book
    launched_s = launch.launched_ms // 1000
    meta = rh.CurveMeta(curve=launch.curve or "", token=launch.token, deployer=launch.creator or "",
                        pair_token=launch.pair_token or ZERO, launch_config_id=0,
                        graduation_threshold=launch.graduation_threshold or 0, launched_block=launch.block or 0)
    for entry in logs:
        trade = rh.parse_curve_trade(entry, meta=meta)
        if trade is None or trade["side"] != "buy":
            continue
        quote_in = int(trade["amount_native"])
        fee = int(trade.get("fee") or 0)
        block = int(trade.get("slot") or 0)
        ts_s = block_ts.get(block)
        if trade["tx"] == launch.tx:
            book.dev_buy_wei = (book.dev_buy_wei or 0) + quote_in
            continue
        book.buyers.update(str(a).lower() for a in (trade.get("wallet"), trade.get("recipient")) if a)
        if trade["tx"] not in book.buy_txs:
            book.buy_txs.append(str(trade["tx"]))
        if ts_s is None:
            continue
        book.buy_tx_ts[str(trade["tx"])] = ts_s
        elapsed = ts_s - launched_s
        if elapsed < demand_window_s:
            book.outside_buys_window += 1
        if elapsed in (0, 1) and quote_in > 0:
            toll_bps = fee * 10_000 // quote_in - PONS_PROTOCOL_FEE_BPS
            if toll_bps < exempt_toll_bps:
                book.exempt_buyers.append(str(trade.get("recipient") or trade["wallet"]))
    return book


TRUSTED_COHORT = "trusted_copy"


@dataclass(frozen=True, slots=True)
class Cohorts:
    """The wallets whose early buy is alpha, AS THE DATABASE HAS THEM (``lanes._cohort``'s
    rule: a feed's claim about a wallet is never trust). Lower-case on EVM chains."""

    trusted: frozenset[str] = frozenset()
    proven: frozenset[str] = frozenset()
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.trusted and not self.proven


def _norm(chain: Chain, address: Any) -> str:
    a = str(address or "").strip()
    return a.lower() if chain is not Chain.SOL else a


def cohorts_for(conn: sqlite3.Connection, chain: Chain, p: Mapping[str, Any], *, at_ms: int | None = None) -> Cohorts:
    """``wallets.cohort = 'trusted_copy'`` rows plus the newest frozen proven cohort
    (``proven.proven_members``, the age limit ``lanes._proven_cohort`` uses). Never raises."""
    notes: list[str] = []
    trusted: frozenset[str] = frozenset()
    try:
        rows = fetch_all(conn, "SELECT address FROM wallets WHERE chain=? AND cohort=?", (chain.value, TRUSTED_COHORT))
        trusted = frozenset(_norm(chain, r["address"]) for r in rows if r["address"])
    except sqlite3.Error as exc:
        notes.append(f"trusted_unreadable:{type(exc).__name__}")
    proven: frozenset[str] = frozenset()
    try:
        from kaiba.learning.proven import proven_members

        cohort = proven_members(conn, chain, max_age_s=float(p.get("proven_max_cohort_age_s") or 259_200), at_ms=at_ms)
        if cohort is None:
            notes.append("proven_none_or_stale")
        else:
            proven = frozenset(_norm(chain, w) for w in cohort.members)
    except Exception as exc:  # noqa: BLE001 - a missing cohort is no cohort, never a crash
        notes.append(f"proven_unreadable:{type(exc).__name__}")
    return Cohorts(trusted, proven, ";".join(notes))


@dataclass(frozen=True, slots=True)
class TrustedEarly:
    """Which known-good wallets had bought this launch by the decision moment."""

    trusted: tuple[str, ...] = ()
    proven: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def rule(self) -> str:
        return f"trusted_early:{len(self.trusted)}/{len(self.proven)}"

    @property
    def count(self) -> int:
        return len(self.trusted) + len(self.proven)


def tape_buyers(conn: sqlite3.Connection, launch: lf.Launch, *, until_ms: int) -> set[str]:
    """Buyers the ``swaps`` tape already holds for this token, up to the decision moment.
    ``ingest/wallet_stream.py`` books a trusted wallet's router trade under the WALLET about
    0.7 s after its block; the Pons poller books it under the router. On sol this is the
    only source (there is no early book): ``ingest/wallet_stream_sol.py`` books trusted and
    proven wallets' swaps with ``ts_ms`` = block time, and a row it has not written by
    ``until_ms`` does not count -- nothing here waits for it."""
    try:
        rows = fetch_all(conn, "SELECT DISTINCT wallet FROM swaps WHERE chain=? AND token=? AND side='buy' AND ts_ms<=?",
                         (launch.chain.value, launch.token, int(until_ms)))
    except sqlite3.Error:
        return set()
    return {_norm(launch.chain, r["wallet"]) for r in rows if r["wallet"]}


def tx_senders(launch: lf.Launch, txs: Sequence[str], *, conn: Any = None, limit: int = 40) -> set[str]:
    """The signers of the early buys: the one identity a router, the v4 PoolManager or an
    EIP-7702 self-call cannot hide (``CurveBuy.trader`` is ``msg.sender``). One batch."""
    want = list(txs)[:limit]
    if not want:
        return set()
    res = rh_rpc([("eth_getTransactionByHash", [t]) for t in want], priority=Priority.ENTRY,
                 endpoint="snipe.book_senders", conn=conn) or []
    return {str(t["from"]).lower() for t in res if isinstance(t, Mapping) and t.get("from")}


def trusted_early(conn: sqlite3.Connection, launch: lf.Launch, book: EarlyBook | None, cohorts: Cohorts, *,
                  at_ms: int, senders: Any = None, signer_window_s: int = 2) -> TrustedEarly:
    """Known-good buyers among this launch's early buys, read at the lane's normal decision
    moment and never waited for. Buyers come from the early book (trader, recipient), the
    ``swaps`` tape up to ``at_ms``, and -- only when those name none of them -- the signers
    of the buys in the last ``signer_window_s`` seconds, the ones too recent for the wallet
    stream (~0.7 s behind its block) to have booked under the wallet yet. Older buys are
    already on the tape, so their signers are not paid for again (~17 CU each, and ~190 Pons
    launches an hour, MEASURED 2026-10-04). The creator and the launch transaction's sender
    are not "early buyers": a watched DEV is ``dev_watchlist``'s rule, not this one's."""
    if cohorts.empty:
        return TrustedEarly()
    dev = {_norm(launch.chain, w) for w in (launch.creator, book.dev if book else None) if w}
    known = (cohorts.trusted | cohorts.proven) - dev
    if not known:
        return TrustedEarly()
    buyers: set[str] = set()
    sources: list[str] = []
    for name, found in (("book", set(book.buyers) if book else set()), ("tape", tape_buyers(conn, launch, until_ms=at_ms))):
        if found & known:
            sources.append(name)
        buyers |= found
    since_s = at_ms // 1000 - max(0, int(signer_window_s))
    recent = [tx for tx in (book.buy_txs if book else []) if book.buy_tx_ts.get(tx, since_s) >= since_s]
    if not buyers & known and recent:
        signed = (senders or tx_senders)(launch, recent, conn=conn)
        if signed & known:
            sources.append("signer")
        buyers |= signed
    buyers &= known
    hit_t = tuple(sorted(buyers & cohorts.trusted))
    hit_p = tuple(sorted((buyers & cohorts.proven) - cohorts.trusted))
    return TrustedEarly(hit_t, hit_p, tuple(sources))


# --------------------------------------------------------------------------------------
# BSC (Flap): one batched read of the portal answers every guard
# --------------------------------------------------------------------------------------


def _words_of(raw: Any) -> list[int]:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return []
    body = raw[2:]
    try:
        return [int(body[i:i + 64], 16) for i in range(0, len(body) - 63, 64)]
    except ValueError:
        return []


def _int_of(raw: Any) -> int | None:
    """``0x…`` -> int; ``None`` for anything else (``0x`` -- an empty return -- is not zero)."""
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 3:
        return None
    try:
        return int(raw, 16)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class FlapLaunch:
    """What the Flap portal says about one launch at our decision moment.

    Built by :func:`flap_launch_from_results` from one batch (:func:`flap_calls`). Every
    field the guards read is here, so :func:`bsc_vetoes` is pure.
    """

    read: bool
    note: str = ""
    record: ep.FlapRecord | None = None
    #: The curve priced and cross-checked exactly as ``evm_price.read_flap`` prices it for
    #: protection (``parse_flap_record``); ``None`` when that refuses -- protection could
    #: not price this token either. :attr:`price_note` says why.
    curve: ep.FlapCurve | None = None
    price_note: str = ""
    #: ``None`` = the quota read failed, which is NOT "no quota".
    quota: ep.FlapBuyQuota | None = None
    #: The largest size the engine could choose (:func:`planned_bsc_size`), and the tokens
    #: it would buy on this curve after GMGN's commission, the fee and the buy tax.
    planned_size: int = 0
    planned_tokens: int | None = None
    #: What the paper buy pays, and what of it reaches the portal after GMGN's commission
    #: (``bsc_router_bps_per_leg``): the quote and the model are both for ``paper_spend``.
    paper_size: int = 0
    paper_spend: int = 0
    paper_tokens_model: int | None = None
    #: The venue's own ``quoteExactInput`` for ``paper_spend`` (fee, tax and any cap in it).
    #: ``None`` = unread or reverted, which REFUSES (``venue_quote_unread``).
    paper_tokens_quoted: int | None = None
    token_decimals: int | None = None
    supply_atoms: int | None = None
    #: When this read was made (epoch ms; 0 = unknown).
    read_ms: int = 0


def _venue_spend(size: int, router_bps: int) -> int:
    """What of a BNB buy reaches the venue after GMGN's commission (off the input)."""
    return max(0, int(size) * (10_000 - max(0, int(router_bps))) // 10_000)


def flap_calls(token: str, *, paper_size: int) -> list[tuple[str, list[Any]]]:
    """The one batch: the portal record, the token's decimals and supply (the record's
    cross-checks need them), its Buy Quota, and the venue's own quote for ``paper_size``
    (pass the SPEND, after GMGN's commission), asked from :data:`PAPER_WHO` (the quota is
    per ``tx.origin``; a never-used origin is in the position our wallet's first buy
    would be)."""
    t = token.lower()
    portal, quote_data = ep.quote_exact_input_call(t, paper_size)
    return [
        ("eth_call", [{"to": ep.FLAP_PORTAL, "data": ep.SEL_GET_TOKEN_V8_SAFE + _addr_word(t)}, "latest"]),
        ("eth_call", [{"to": t, "data": ep.SEL_DECIMALS}, "latest"]),
        ("eth_call", [{"to": t, "data": ep.SEL_TOTAL_SUPPLY}, "latest"]),
        ("eth_call", [{"to": ep.FLAP_PORTAL, "data": ep.SEL_MAX_BUY_PER_ORIGIN + _addr_word(t)}, "latest"]),
        ("eth_call", [{"from": PAPER_WHO, "to": portal, "data": quote_data}, "latest"]),
    ]


def _tokens_for(curve: ep.FlapCurve | None, size: int) -> int | None:
    if curve is None or curve.buy_tax_bps is None or size <= 0:
        return None
    return curve.tokens_out(ep.flap_buy_net_quote(size, curve.buy_tax_bps))


def _price_record(token: str, record: ep.FlapRecord, decimals: int | None,
                  supply: int | None) -> tuple[ep.FlapCurve | None, str]:
    """The record priced exactly as ``evm_price.read_flap`` prices it for protection."""
    if record.quote_token not in BSC_NATIVE_QUOTES:
        return None, f"quote_not_native:{record.quote_token}"
    if decimals is None or supply is None:
        return None, "flap_token_metadata_unreadable"
    priced, price_note = ep.parse_flap_record(
        record, token_decimals=decimals, token_supply_atoms=supply,
        quote_decimals=ep.NATIVE_DECIMALS[Chain.BSC],
        address_note="" if token.lower().endswith(ep.FLAP_ADDRESS_FINGERPRINT) else "; no 7777 fingerprint",
    )
    if priced is None:
        return None, price_note
    curve = priced.curve
    refusal = curve.refusal if curve is not None else "no_curve_state"
    if refusal is not None:
        return None, refusal
    return curve, price_note


def _quoted_tokens(raw: Any) -> int | None:
    """``quoteExactInput``'s answer; ``None`` for a revert, an empty return or no words."""
    words = _words_of(raw)
    return words[0] if words else None


def flap_launch_from_results(token: str, results: Sequence[Any] | None, *, planned_size: int,
                             paper_size: int, router_bps: int = 0, read_ms: int = 0) -> FlapLaunch:
    """:func:`flap_calls`' answers -> :class:`FlapLaunch`. Pure; never raises.

    ``router_bps``: GMGN's commission, taken off every size before the venue sees it; the
    batch must have quoted ``_venue_spend(paper_size, router_bps)``."""
    spend = _venue_spend(paper_size, router_bps)
    blank = {"planned_size": planned_size, "paper_size": paper_size, "paper_spend": spend, "read_ms": read_ms}
    if not results or len(results) < 5:
        return FlapLaunch(False, "flap_rpc_failed", **blank)
    if results[0] is None:
        return FlapLaunch(False, "flap_portal_read_failed", **blank)
    record = ep.flap_record(_words_of(results[0]))
    if record is None:
        return FlapLaunch(False, "flap_no_portal_record", **blank)
    decimals, supply = _int_of(results[1]), _int_of(results[2])
    curve, price_note = _price_record(token, record, decimals, supply)
    return FlapLaunch(
        read=True, note="ok", record=record, curve=curve, price_note=price_note,
        quota=ep.parse_buy_quota(results[3]), planned_size=planned_size,
        planned_tokens=_tokens_for(curve, _venue_spend(planned_size, router_bps)), paper_size=paper_size,
        paper_spend=spend, paper_tokens_model=_tokens_for(curve, spend),
        paper_tokens_quoted=_quoted_tokens(results[4]), token_decimals=decimals, supply_atoms=supply,
        read_ms=read_ms,
    )


def flap_fill_calls(token: str, *, spend: int) -> list[tuple[str, list[Any]]]:
    """The fill re-read: the portal record and the venue's quote for ``spend`` (decimals,
    supply and the quota are the decision read's; the first two are immutable)."""
    t = token.lower()
    portal, quote_data = ep.quote_exact_input_call(t, spend)
    return [
        ("eth_call", [{"to": ep.FLAP_PORTAL, "data": ep.SEL_GET_TOKEN_V8_SAFE + _addr_word(t)}, "latest"]),
        ("eth_call", [{"from": PAPER_WHO, "to": portal, "data": quote_data}, "latest"]),
    ]


def flap_fill_from_results(token: str, results: Sequence[Any] | None, decided: FlapLaunch, *,
                           read_ms: int = 0) -> FlapLaunch:
    """:func:`flap_fill_calls`' answers -> the curve and quote a live buy would have met. Pure."""
    keep = {"planned_size": decided.planned_size, "paper_size": decided.paper_size,
            "paper_spend": decided.paper_spend, "quota": decided.quota, "read_ms": read_ms,
            "token_decimals": decided.token_decimals, "supply_atoms": decided.supply_atoms}
    if not results or len(results) < 2 or results[0] is None:
        return FlapLaunch(False, "fill_unread", **keep)
    record = ep.flap_record(_words_of(results[0]))
    if record is None:
        return FlapLaunch(False, "fill_no_portal_record", **keep)
    if not record.on_curve:
        return FlapLaunch(True, "fill_not_on_curve", record=record, price_note=f"flap_status:{record.status}", **keep)
    curve, price_note = _price_record(token, record, decided.token_decimals, decided.supply_atoms)
    return FlapLaunch(True, "fill", record=record, curve=curve, price_note=price_note,
                      paper_tokens_model=_tokens_for(curve, decided.paper_spend),
                      paper_tokens_quoted=_quoted_tokens(results[1]), **keep)


def planned_bsc_size(p: Mapping[str, Any], *, cfg: Any = None) -> int:
    """The largest bsc size the engine could choose for this lane: the chain's
    ``max_position_base_units`` (every size is clamped under it), or the paper size if that
    is larger. The Buy Quota is checked against THIS, so any size the engine picks fits."""
    paper = int(_per_chain(p.get("paper_size"), Chain.BSC, 0) or 0)
    try:
        top = int((cfg or get_risk()).chain_budget(Chain.BSC).max_position_base_units or 0)
    except Exception as exc:  # noqa: BLE001 - the paper size is still a bound to check
        log.debug("bsc max position unreadable (%s); quota checked at the paper size", exc)
        top = 0
    return max(paper, top)


def bsc_rpc(calls: Sequence[tuple[str, list[Any]]], *, priority: Priority = Priority.DISCOVERY,
            endpoint: str = "snipe.flap", conn: Any = None, timeout_s: float = 10.0, bucket: str | None = None,
            wait_for_slot_s: float = 5.0) -> list[Any] | None:
    """One batched JSON-RPC round trip to the DEDICATED BSC snipe endpoint
    (``launch_feed.bsc_snipe_rpc_url``, never ``BSC_RPC_URL``, whose key protection's price
    reads use), through :data:`BSC_RPC_BUCKET` (or ``bucket``). ``None`` on any failure, or
    when no dedicated endpoint is configured."""
    from kaiba.providers._http import post_json

    url = lf.bsc_snipe_rpc_url()
    if not url or not calls:
        return None
    body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": a} for i, (m, a) in enumerate(calls)]
    got = post_json(bucket or BSC_RPC_BUCKET[0], endpoint, url, json_body=body, priority=priority,
                    wait_for_slot_s=wait_for_slot_s, timeout_s=timeout_s, ttl_s=0.0, conn=conn)
    if not got.ok or not isinstance(got.data, list):
        return None
    by_id = {item.get("id"): item for item in got.data if isinstance(item, Mapping)}
    return [(by_id.get(i + 1) or {}).get("result") for i in range(len(calls))]


def _router_bps(p: Mapping[str, Any]) -> int:
    return max(0, int(p.get("bsc_router_bps_per_leg") or 0))


def read_flap_launch(launch: lf.Launch, p: Mapping[str, Any], *, conn: Any = None, rpc: Any = None,
                     priority: Priority = Priority.DISCOVERY) -> FlapLaunch:
    """Read the portal for ``launch``: one batch of five ``eth_call``s (~130 CU)."""
    paper = int(_per_chain(p.get("paper_size"), Chain.BSC, 0) or 0)
    planned = planned_bsc_size(p)
    router = _router_bps(p)
    spend = _venue_spend(paper, router)
    at = now_ms()
    try:
        res = (rpc or bsc_rpc)(flap_calls(launch.token, paper_size=spend), priority=priority,
                               endpoint="snipe.flap", conn=conn)
    except Exception as exc:  # noqa: BLE001 - an unread launch is vetoed, never a crash
        return FlapLaunch(False, f"flap_rpc_raised:{type(exc).__name__}", planned_size=planned, paper_size=paper,
                          paper_spend=spend, read_ms=at)
    return flap_launch_from_results(launch.token, res, planned_size=planned, paper_size=paper, router_bps=router,
                                    read_ms=at)


def read_flap_fill(launch: lf.Launch, p: Mapping[str, Any], decided: FlapLaunch, *, conn: Any = None,
                   rpc: Any = None) -> FlapLaunch:
    """The curve and the venue's quote NOW -- call it ``bsc_exec_latency_ms`` after the
    decision (:class:`Sniper` waits). Two ``eth_call``s (~52 CU). Never raises."""
    at = now_ms()
    try:
        res = (rpc or bsc_rpc)(flap_fill_calls(launch.token, spend=decided.paper_spend), priority=Priority.DISCOVERY,
                               endpoint="snipe.flap_fill", conn=conn)
    except Exception as exc:  # noqa: BLE001 - an unread fill books nothing
        return replace(decided, read=False, note=f"fill_raised:{type(exc).__name__}", curve=None,
                       paper_tokens_model=None, paper_tokens_quoted=None, read_ms=at)
    return flap_fill_from_results(launch.token, res, decided, read_ms=at)


def bsc_native_price_ok(conn: Any, *, at_ms: int | None = None) -> bool:
    """Is a BNB price on the ``native_prices`` books, inside its tolerance and not stamped in
    the future? That is the sample ``Watchdog._native_usd`` falls back to for an exit's
    ``min_out`` -- MEASURED 2026-09-22, the read that, missing, left a bsc position unsellable
    for 53 minutes. Never raises; any failure is ``False``."""
    try:
        from kaiba.providers.native_price import at as native_price_at

        when = at_ms or now_ms()
        got = native_price_at(Chain.BSC, when, conn)
        sample = got.sample_ts_ms
        return got.price_usd is not None and got.price_usd > 0 and (sample is None or int(sample) <= when)
    except Exception as exc:  # noqa: BLE001 - "could not check" refuses like "cannot see"
        log.debug("bsc native price unreadable: %s", exc)
        return False


def bsc_vetoes(launch: lf.Launch, p: Mapping[str, Any], flap: FlapLaunch | None, *, native_ok: bool | None,
               feats: dict[str, Any]) -> list[str]:
    """Every pre-trade guard for a bsc (Flap) launch; fills ``feats`` with what it read.

    Applied to every rule, watched dev included: none of these is a matter of opinion about
    the launch, each is a reason the paper result would not be a result we could trade.
    """
    out: list[str] = []
    max_age_ms = int(float(p.get("bsc_max_launch_age_s") or 60) * 1000)
    if launch.latency_ms is None or launch.latency_ms > max_age_ms:
        out.append(f"launch_stale:{launch.latency_ms}ms>{max_age_ms}ms")
    if flap is None or not flap.read or flap.record is None:
        feats["quote_native"] = None
        out.append(f"flap_unread:{flap.note if flap is not None else 'not_read'}")
    else:
        rec = flap.record
        feats.update({
            "flap_status": rec.status, "quote_token": rec.quote_token, "quote_native": rec.quote_token is None,
            "buy_tax_bps": rec.buy_tax_bps, "sell_tax_bps": rec.sell_tax_bps, "token_version": rec.token_version,
            "quote_raised_wei": str(rec.quote_raised_base),
            "progress_pct": (str(round(Decimal(rec.tokens_sold_atoms) * 100 / Decimal(rec.graduation_tokens_atoms), 4))
                             if rec.graduation_tokens_atoms else None),
            "quota_bps": flap.quota.bps if flap.quota else None,
            "quota_max_atoms": str(flap.quota.max_buy_atoms) if flap.quota else None,
            "planned_size": str(flap.planned_size),
            "planned_tokens": str(flap.planned_tokens) if flap.planned_tokens is not None else None,
        })
        if not rec.on_curve:
            out.append(f"flap_not_on_curve:status={rec.status}")
        if rec.quote_token not in BSC_NATIVE_QUOTES:
            out.append(f"quote_not_native:{rec.quote_token}")
        limit = int(_per_chain(p.get("max_entry_tax_bps"), Chain.BSC, 0) or 0)
        if rec.buy_tax_bps is None or rec.sell_tax_bps is None:
            out.append("token_tax_unread")
        elif max(rec.buy_tax_bps, rec.sell_tax_bps) > limit:
            out.append(f"token_tax:{rec.buy_tax_bps}/{rec.sell_tax_bps}bps>{limit}")
        if flap.curve is None:
            out.append(f"unpriceable:{flap.price_note}"[:120])
        if flap.quota is None:
            out.append("buy_quota_unread")
        elif flap.quota.active and (flap.planned_tokens is None or flap.quota.max_buy_atoms < flap.planned_tokens):
            out.append(f"buy_quota_below_size:{flap.quota.max_buy_atoms}<{flap.planned_tokens}")
        if flap.paper_tokens_quoted is None:
            # Fails CLOSED, like the tax and quota reads: a reverted or unread quote is exactly
            # when a buy would be restricted or capped, and the venue's quote is the only
            # MEASURED witness of a cap (an active quota's return layout was never observed).
            out.append("venue_quote_unread")
        elif flap.paper_tokens_model:
            ratio_bps = flap.paper_tokens_quoted * 10_000 // flap.paper_tokens_model
            feats["quote_vs_model_bps"] = ratio_bps
            band = int(p.get("max_quote_shortfall_bps") or 0)
            # Two-sided: a quote ABOVE the model is a venue we do not understand either, and the
            # paper entry would otherwise book tokens the curve cannot deliver.
            if band > 0 and ratio_bps < 10_000 - band:
                out.append(f"quote_short_of_curve:{ratio_bps}bps")
            elif band > 0 and ratio_bps > 10_000 + band:
                out.append(f"quote_above_curve:{ratio_bps}bps")
    if native_ok is not True:
        out.append("no_native_price:bsc")
    return out


# --------------------------------------------------------------------------------------
# the verdict
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Verdict:
    fire: bool
    reasons: list[str]
    strength: float = 0.0
    rule: str = ""
    features: dict[str, Any] = field(default_factory=dict)


def evaluate(launch: lf.Launch, p: Mapping[str, Any], record: Record, *, book: EarlyBook | None = None,
             snipes_today: int = 0, trusted: TrustedEarly | None = None, flap: FlapLaunch | None = None,
             native_ok: bool | None = None) -> Verdict:
    """Fire or skip, with every reason. Pure.

    Alpha, first match wins: a watched dev, a watched name, a known-good early buyer
    (``trusted_early:<trusted>/<proven>``, at least ``trusted_early_min`` of them, on the
    chains in ``trusted_early_chains``), a deployer record. Every veto applies to all of them
    except that a watched dev overrides ``never_records``. On bsc, :func:`bsc_vetoes` (the
    portal read ``flap`` and the native-price check ``native_ok``) applies to every rule."""
    feats: dict[str, Any] = {
        "venue": launch.venue, "record": record.label, "record_launches": record.launches,
        "record_scored": record.prior_scored, "record_runners": record.prior_runners,
        "quote_native": launch.quote_is_native, "latency_ms": launch.latency_ms,
    }
    if launch.chain.value not in [str(c) for c in p.get("chains") or []]:
        return Verdict(False, [f"chain_not_enabled:{launch.chain.value}"], features=feats)
    venues = [str(v).lower() for v in _per_chain(p.get("venues"), launch.chain, []) or []]
    wallets = {w.lower() for w in (launch.creator, book.dev if book else None) if w}
    watched = sorted(wallets & {str(w).lower() for w in p.get("dev_watchlist") or []})
    names = name_hits(launch, p.get("name_watchlist") or [])
    feats.update({"dev_watch": watched, "name_watch": names})
    early = trusted if trusted is not None and launch.chain.value in [str(c) for c in p.get("trusted_early_chains") or []] \
        else None
    if early is not None:
        feats.update({"trusted_early": len(early.trusted), "proven_early": len(early.proven),
                      "trusted_early_sources": list(early.sources)})

    reasons: list[str] = []
    if launch.venue.lower() not in venues:
        reasons.append(f"venue_not_enabled:{launch.venue}")
    if record.label in set(_per_chain(p.get("never_records"), launch.chain, []) or []) and not watched:
        reasons.append(f"deployer_record:{record.label}")
    if watched:
        rule, strength = "dev_watchlist", float(p.get("watch_strength", 0.75))
    elif names:
        rule, strength = "name_watchlist", float(p.get("watch_strength", 0.75))
    elif early is not None and early.count >= max(1, int(p.get("trusted_early_min") or 1)):
        rule, strength = early.rule, float(p.get("record_strength", 0.72))
    elif record.label in set(_per_chain(p.get("fire_on_records"), launch.chain, []) or []):
        rule, strength = f"record:{record.label}", float(p.get("record_strength", 0.72))
    else:
        rule, strength = "", 0.0
        reasons.append("no_alpha:not_watched_and_record_" + record.label)

    if launch.chain is Chain.ROBINHOOD:
        if book is None or not book.read:
            reasons.append("early_book_unread")
        else:
            feats.update({"dev_buy_wei": book.dev_buy_wei, "outside_buys_2s": book.outside_buys_window,
                          "exempt_buyers": len(book.exempt_buyers)})
            if p.get("exempt_buyer_veto", True) and book.exempt_buyers:
                reasons.append(f"exempt_buyer:{len(book.exempt_buyers)}")
            veto_wei = int(p.get("dev_atomic_veto_wei") or 0)
            if (launch.quote_is_native and veto_wei > 0 and (book.dev_buy_wei or 0) < veto_wei
                    and book.outside_buys_window == 0):
                reasons.append("dev_atomic_no_demand")
    elif launch.chain is Chain.BSC:
        reasons.extend(bsc_vetoes(launch, p, flap, native_ok=native_ok, feats=feats))

    cap = int(_per_chain(p.get("max_snipes_per_day"), launch.chain, 0) or 0)
    if rule and snipes_today >= cap:
        reasons.append(f"daily_snipe_cap:{snipes_today}/{cap}")
    return Verdict(not reasons, reasons, strength if not reasons else 0.0, rule, feats)


# --------------------------------------------------------------------------------------
# Robinhood reads (Alchemy via the robinhood-rpc limiter)
# --------------------------------------------------------------------------------------


def rh_rpc(calls: Sequence[tuple[str, list[Any]]], *, priority: Priority = Priority.DISCOVERY,
           endpoint: str = "snipe.batch", conn: Any = None, timeout_s: float = 15.0) -> list[Any] | None:
    """One batched JSON-RPC round trip to the configured Robinhood endpoint (Alchemy on the
    box), through the ``robinhood-rpc`` limiter bucket. ``None`` on any failure."""
    from kaiba.providers._http import post_json

    url = get_settings().rpc_for(Chain.ROBINHOOD) or rh.RPC_URL
    bucket = RPC_BUCKET[0]
    body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": a} for i, (m, a) in enumerate(calls)]
    got = post_json(bucket, endpoint, url, json_body=body, priority=priority, wait_for_slot_s=5.0,
                    timeout_s=timeout_s, ttl_s=0.0, conn=conn)
    if not got.ok or not isinstance(got.data, list):
        return None
    by_id = {item.get("id"): item for item in got.data if isinstance(item, Mapping)}
    return [(by_id.get(i + 1) or {}).get("result") for i in range(len(calls))]


def _h(n: int) -> str:
    return hex(int(n))


def _word(n: int) -> str:
    return format(int(n), "064x")


def _addr_word(a: str) -> str:
    return a.lower().removeprefix("0x").rjust(64, "0")


def buy_calldata(quote_in: int, min_out: int, recipient: str) -> str:
    return PONS_BUY_SELECTOR + _word(quote_in) + _word(min_out) + _addr_word(recipient)


#: The four reads that move: quoteReserve, realQuoteReserve, sellableTokens, reservedTokens.
#: Fee, creator tax and graduation threshold are fixed per curve and stored at entry, so a
#: mark costs 4 eth_calls, not the 11 of a full ``CURVE_READS`` + creator-tax read.
RESERVE_SELECTORS: tuple[str, ...] = ("0x9da771f4", "0x4f1f58fd", "0x808bcddc", "0x15a55347")
FEE_SELECTOR = "0x24a9d853"


def reserve_calls(curve: str) -> list[tuple[str, list[Any]]]:
    return [("eth_call", [{"to": curve, "data": sel}, "latest"]) for sel in RESERVE_SELECTORS]


def parse_reserves(results: Sequence[Any]) -> tuple[int, int, int] | None:
    """``(quote_reserve, real_quote_reserve, token_reserve)`` or ``None``."""
    try:
        q, real, sellable, reserved = (int(str(r), 16) for r in results)
    except (TypeError, ValueError):
        return None
    return q, real, sellable + reserved


def wait_for_tax(launch: lf.Launch, p: Mapping[str, Any], *, conn: Any = None, sleep: Any = time.sleep,
                 clock: Any = time.monotonic, wall: Any = time.time) -> tuple[int | None, int | None]:
    """Block until the chain head's timestamp puts the tax at or under the limit.

    Two steps, so the wait costs ONE read of the shared ``robinhood-rpc`` bucket (0.6/s,
    shared with protection's price reads) instead of a poll: sleep on our own clock until
    the target second has begun plus ``chain_clock_margin_ms`` (block timestamps are whole
    seconds and ~10 blocks share each), then CONFIRM on the chain's clock, re-reading at
    most once a second. The chain's clock decides; ours only saves calls.

    Returns ``(head_ts_s, head_block)``, or ``(None, None)`` if the head has not reached the
    target within ``chain_wait_max_s``.
    """
    if launch.launched_ms is None:
        return None, None
    limit = int(_per_chain(p.get("max_entry_tax_bps"), Chain.ROBINHOOD, 0) or 0)
    target_s = launch.launched_ms // 1000 + next(
        (k for k in range(PONS_TAX_SECONDS + 1) if PONS_TAX_RUNGS_BPS.get(k, 0) <= limit), PONS_TAX_SECONDS)
    max_wait = float(p.get("chain_wait_max_s") or 20)
    deadline = clock() + max_wait
    lead = target_s + int(p.get("chain_clock_margin_ms") or 0) / 1000 - wall()
    if lead > 0:
        sleep(min(lead, max_wait))
    while clock() < deadline:
        res = rh_rpc([("eth_getBlockByNumber", ["latest", False])], priority=Priority.ENTRY,
                     endpoint="snipe.head", conn=conn)
        head = res[0] if res else None
        if isinstance(head, Mapping):
            ts = int(str(head.get("timestamp")), 16)
            if ts >= target_s:
                return ts, int(str(head.get("number")), 16)
        sleep(1.0)
    return None, None


def read_early_book(launch: lf.Launch, p: Mapping[str, Any], head_block: int, *, conn: Any = None) -> EarlyBook:
    """The launch tx's sender and every CurveBuy from the launch block to ``head_block``."""
    if not launch.curve or launch.block is None or not launch.tx:
        return EarlyBook(note="launch_incomplete")
    res = rh_rpc([
        ("eth_getTransactionByHash", [launch.tx]),
        ("eth_getLogs", [{"address": launch.curve, "topics": [rh.TOPIC_CURVE_BUY],
                          "fromBlock": _h(launch.block), "toBlock": _h(head_block)}]),
    ], priority=Priority.ENTRY, endpoint="snipe.book", conn=conn)
    if res is None:
        return EarlyBook(note="rpc_failed")
    tx, logs = res
    logs = logs if isinstance(logs, list) else []
    block_ts: dict[int, int] = {}
    missing: set[int] = set()
    for entry in logs:
        b = int(str(entry.get("blockNumber")), 16)
        if entry.get("blockTimestamp") is not None:
            block_ts[b] = int(str(entry["blockTimestamp"]), 16)
        else:
            missing.add(b)
    if missing:
        heads = rh_rpc([("eth_getBlockByNumber", [_h(b), False]) for b in sorted(missing)[:40]],
                       priority=Priority.ENTRY, endpoint="snipe.book_times", conn=conn) or []
        for b, hd in zip(sorted(missing)[:40], heads, strict=False):
            if isinstance(hd, Mapping):
                block_ts[b] = int(str(hd.get("timestamp")), 16)
    tx_from = str(tx.get("from")).lower() if isinstance(tx, Mapping) and tx.get("from") else None
    return early_book(launch, logs, tx_from=tx_from, block_ts=block_ts,
                      demand_window_s=int(p.get("veto_demand_window_s") or 2),
                      exempt_toll_bps=int(p.get("exempt_toll_bps") or 100))


# --------------------------------------------------------------------------------------
# paper measurement: exact entries and marks
# --------------------------------------------------------------------------------------

#: What selling ``tokens`` back to a Pons curve pays (the curve's own formula; no toll on sells).
def pons_sell_quote(quote_reserve: int, token_reserve: int, tokens: int, fee_bps: int, creator_tax_bps: int,
                    real_quote_reserve: int | None = None) -> int:
    if tokens <= 0 or quote_reserve <= 0 or token_reserve <= 0:
        return 0
    gross = tokens * quote_reserve // (token_reserve + tokens)
    out = gross - gross * fee_bps // 10_000 - gross * creator_tax_bps // 10_000
    if real_quote_reserve is not None:
        out = min(out, real_quote_reserve)
    return max(0, out)


def with_holding(quote_reserve: int, token_reserve: int, quote_in: int) -> tuple[int, int]:
    """The curve as it would be had our paper buy happened: buys are priced in quote and
    every trade keeps quote x token fixed, so ``quote_in`` more quote moves it to
    ``(q + s, q*t / (q + s))``. Exact while everyone since has only bought (Vanguard
    sniper, fork-checked 2026-10-03)."""
    if quote_in <= 0:
        return quote_reserve, token_reserve
    q2 = quote_reserve + quote_in
    return q2, quote_reserve * token_reserve // q2


def quote_for_tokens(quote_reserve: int, token_reserve: int, tokens: int) -> int:
    """The quote a buy that received ``tokens`` added to the curve (inverse of the buy formula)."""
    return 0 if tokens >= token_reserve else tokens * quote_reserve // (token_reserve - tokens)


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            obs_id TEXT PRIMARY KEY,
            chain TEXT NOT NULL,
            token TEXT NOT NULL,
            venue TEXT,
            creator TEXT,
            launched_ms INTEGER,
            seen_ms INTEGER NOT NULL,
            latency_ms INTEGER,
            fire INTEGER NOT NULL DEFAULT 0,
            rule TEXT,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            features_json TEXT NOT NULL DEFAULT '{{}}',
            entry_ms INTEGER,
            entry_quote_in TEXT,
            entry_tokens TEXT,
            entry_shadow_quote TEXT,
            entry_basis TEXT,
            marks_json TEXT NOT NULL DEFAULT '{{}}',
            peak_ratio TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            signal_id TEXT,
            dossier_note TEXT,
            created_ms INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL
        )"""
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_status ON {TABLE}(status, entry_ms)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_chain ON {TABLE}(chain, seen_ms)")
    have = {r["name"] for r in fetch_all(conn, f"PRAGMA table_info({TABLE})")}
    for col in ("path_peak_ratio", "path_peak_note"):  # added 2026-10-04; see apply_path_peak
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError as exc:  # another thread added it first
                if "duplicate column" not in str(exc):
                    raise


def obs_id_for(launch: lf.Launch) -> str:
    return "snp_" + digest({"chain": launch.chain.value, "token": launch.token})[:24]


def paper_entry_rh(launch: lf.Launch, p: Mapping[str, Any], *, conn: Any = None) -> dict[str, Any]:
    """Exact on ETH pairs: ``eth_simulateV1`` of the curve buy from :data:`PAPER_WHO` against
    the latest state (the tax is already at the limit when this runs), in the SAME batched
    request as the curve's reserves, fee and creator tax. Token pairs are priced on the
    curve's formula in pair units -- the ratio, not the size, is the evidence."""
    if not launch.curve:
        return {"basis": "no_curve"}
    size = int(_per_chain(p.get("paper_size"), Chain.ROBINHOOD, 0) or 0)
    calls = reserve_calls(launch.curve) + [("eth_call", [{"to": launch.curve, "data": FEE_SELECTOR}, "latest"]),
                                           ("eth_call", [{"to": launch.curve, "data": PONS_CREATOR_TAX_SELECTOR}, "latest"])]
    if launch.quote_is_native:
        calls.append(("eth_simulateV1", [{"blockStateCalls": [{"stateOverrides": {PAPER_WHO: {"balance": _h(size + 10**19)}},
                      "calls": [{"from": PAPER_WHO, "to": launch.curve, "value": _h(size),
                                 "data": buy_calldata(size, 0, PAPER_WHO)}]}], "validation": False}, "latest"]))
    res = rh_rpc(calls, priority=Priority.DISCOVERY, endpoint="snipe.entry", conn=conn)
    if not res or any(r is None for r in res[:6]):
        return {"basis": "curve_unread"}
    reserves = parse_reserves(res[:4])
    if reserves is None:
        return {"basis": "curve_unparsed"}
    q, real, t = reserves
    if launch.graduation_threshold and real >= launch.graduation_threshold:
        return {"basis": "curve_graduated"}
    fee, ctax = int(str(res[4]), 16), int(str(res[5]), 16)
    if launch.quote_is_native:
        blocks = res[6] if isinstance(res[6], list) else []
        sims = blocks[0].get("calls") if blocks and isinstance(blocks[0], Mapping) else None
        call = sims[0] if isinstance(sims, list) and sims and isinstance(sims[0], Mapping) else {}
        if call.get("status") != "0x1":
            return {"basis": "sim_reverted", "note": str(call.get("error"))[:120]}
        tokens = int(str(call.get("returnData")), 16)
        basis = "eth_simulateV1"
    else:
        size = max(1, (launch.graduation_threshold or 0) // 1000)
        net = size - size * (fee + ctax) // 10_000
        tokens = net * t // (q + net)
        basis = "curve_formula_pair_units"
    return {"basis": basis, "quote_in": size, "tokens": tokens, "shadow_quote": quote_for_tokens(q, t, tokens),
            "fee_bps": fee, "creator_tax_bps": ctax}


#: pump.fun ``BondingCurve`` account: 8-byte discriminator, then u64 virtual_token_reserves,
#: virtual_sol_reserves, real_token_reserves, real_sol_reserves, token_total_supply, a bool
#: ``complete``, then the creator. Little-endian.
PUMP_CURVE_MIN_LEN = 49


def decode_pump_curve(data: bytes) -> dict[str, int] | None:
    import struct

    if len(data) < PUMP_CURVE_MIN_LEN:
        return None
    vt, vs, rt, rs, supply = struct.unpack_from("<QQQQQ", data, 8)
    return {"virtual_token": vt, "virtual_sol": vs, "real_token": rt, "real_sol": rs, "supply": supply, "complete": int(data[48] == 1)}


def read_pump_curve_fields(conn: Any, bonding_curve: str | None, *, at_ms: int | None = None) -> tuple[dict[str, int] | None, str]:
    """The raw reserves of a pump.fun curve account, read now over ``SOLANA_RPC_URL``."""
    import base64

    from kaiba.providers._http import post_json

    if not bonding_curve:
        return None, "no_bonding_curve_key"
    url = get_settings().rpc_for(Chain.SOL)
    if not url:
        return None, "no_solana_rpc"
    at = at_ms or now_ms()
    got = post_json("rpc", "sol.getAccountInfo", url, json_body={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                    "params": [bonding_curve, {"encoding": "base64", "commitment": "processed"}]},
                    priority=Priority.ENTRY, ttl_s=0.0, cache_key=f"snipe:curve:{bonding_curve}:{at}",
                    wait_for_slot_s=5.0, timeout_s=10.0, conn=conn)
    value = ((got.data or {}).get("result") or {}).get("value") if got.ok and isinstance(got.data, Mapping) else None
    if not isinstance(value, Mapping) or not value.get("data"):
        return None, "curve_account_unread"
    fields = decode_pump_curve(base64.b64decode(value["data"][0]))
    return (fields, "ok") if fields is not None else (None, "curve_account_short")


def read_pump_curve(conn: Any, bonding_curve: str | None, *, at_ms: int | None = None) -> tuple[Any, str]:
    """The pump.fun curve straight from its account on the configured Solana RPC (Alchemy on
    the box), as a ``curve_price.CurveState``. ``(None, why)`` when it cannot be read.

    Kaiba's ``live_resolver`` reads pump.fun's ``/coins/{mint}`` route instead; MEASURED
    2026-10-03 from the operator's desktop (and 2026-10-04 by the lead from the box) that
    route answers 404, so the account is read first and the route is only the fallback."""
    from kaiba.execution import curve_price as cp

    fields, note = read_pump_curve_fields(conn, bonding_curve, at_ms=at_ms)
    if fields is None:
        return None, note
    if fields["complete"]:
        return None, "curve_complete"
    return cp.CurveState.build(virtual_sol=fields["virtual_sol"], virtual_token=fields["virtual_token"],
                               real_sol=fields["real_sol"], real_token=fields["real_token"],
                               observed_ms=at_ms or now_ms(), source="pumpfun_account")


def publish_curve_snapshot(conn: sqlite3.Connection, launch: lf.Launch, *, at_ms: int | None = None,
                           read: Any = None) -> tuple[int | None, str]:
    """Make the curve visible to the entry's sizing: one ``curve_snapshots`` row, now.

    MEASURED 2026-10-04, first live launch-snipe decision (sol): refused
    ``size_not_positive:below_min_position:104420656<370000000``. ``viability.resolve_depth``
    reads a bonding curve on Solana only from a ``curve_snapshots`` row younger than
    ``DEPTH_MAX_AGE_S`` (60 s); a seconds-old token has none, so the sizing band fell back to
    the dossier's pool liquidity (~$2.7k) and capped the size at 0.104 SOL. On a curve the
    executable size is set by the curve's (virtual) reserves, which this module has already
    read from the account. Persisted through ``token_flow.record_snapshot`` from
    ``scanner.curve_from_payload`` -- the same writer and the same derivation tier 1 uses --
    so the band prices the curve itself. Nothing about the band, the ceiling or the chain
    minimum changes.

    Robinhood needs nothing: ``resolve_depth`` reads a Pons curve live (``read_venue``), and
    the lane only fires once the snipe tax is 0, when that read prices it.
    """
    if launch.chain is not Chain.SOL:
        return None, "not_solana"
    if launch.venue != "pump.fun":
        return None, f"no_curve_reader_for:{launch.venue}"
    at = at_ms or now_ms()
    fields, note = (read or read_pump_curve_fields)(conn, launch.meta.get("bonding_curve"), at_ms=at)
    if fields is None:
        return None, note
    from kaiba.execution.scanner import curve_from_payload
    from kaiba.ingest.token_flow import record_snapshot

    payload = {"virtual_token_reserves": fields["virtual_token"], "virtual_sol_reserves": fields["virtual_sol"],
               "real_token_reserves": fields["real_token"], "real_sol_reserves": fields["real_sol"],
               "complete": bool(fields["complete"]), "created_timestamp": launch.launched_ms}
    curve, why = curve_from_payload(payload, at_ms=at)
    if curve is None:
        return None, why
    curve = {**curve, "observed_ms": at, "created_ms": launch.launched_ms, "source": "pumpfun_account"}
    row_id = record_snapshot(Chain.SOL, launch.token, curve, conn)
    return (row_id, "snapshot") if row_id is not None else (None, "snapshot_not_stored")


def _sol_curve(conn: Any, token: str, bonding_curve: str | None, at_ms: int) -> tuple[Any, str]:
    state, note = read_pump_curve(conn, bonding_curve, at_ms=at_ms)
    derived = lf.pump_curve_address(token)
    if state is None and note in ("curve_account_short", "curve_account_unread") and derived and derived != bonding_curve:
        # an observation recorded before 2026-10-04 carries PumpPortal's key, wrong for ~16% of creates
        state, note = read_pump_curve(conn, derived, at_ms=at_ms)
    if state is not None or note == "curve_complete":
        return state, note
    from kaiba.execution import curve_price as cp

    other, other_note = cp.live_resolver(conn)(Chain.SOL, token, at_ms)
    return (other, other_note) if other is not None else (None, f"{note};{other_note}")


def paper_entry_sol(launch: lf.Launch, p: Mapping[str, Any], *, conn: sqlite3.Connection) -> dict[str, Any]:
    """The pump.fun curve model (``curve_price.quote_buy``) at our entry moment."""
    if launch.venue != "pump.fun":
        return {"basis": "no_reader_for_venue"}
    try:
        from kaiba.execution import curve_price as cp

        state, note = _sol_curve(conn, launch.token, launch.meta.get("bonding_curve"), now_ms())
        sol_usd = cp.sol_usd_from_native_price(conn)
        if state is None or sol_usd is None:
            return {"basis": "curve_unread", "note": str(note if state is None else "no_native_usd")[:120]}
        size = int(_per_chain(p.get("paper_size"), Chain.SOL, 0) or 0)
        fill = cp.quote_buy(state, size, sol_usd=sol_usd, decimals=6,
                            latency_ms=int(p.get("sol_exec_latency_ms") or 0))
        if not fill.ok:
            return {"basis": "fill_refused", "note": str(fill.reason)[:120]}
        # what reached the curve: a paper buy never did, so marks advance the live curve by it
        return {"basis": "pumpfun_curve_model", "quote_in": size, "tokens": int(fill.amount_out),
                "shadow_quote": int(fill.curve_in),
                # the curve we entered on, so a curve that completes before a mark can still be valued
                "entry_curve": {"virtual_sol": state.virtual_sol, "virtual_token": state.virtual_token,
                                "real_sol": state.real_sol, "real_token": state.real_token}}
    except Exception as exc:  # noqa: BLE001 - measurement must never take the lane down
        return {"basis": "error", "note": f"{type(exc).__name__}: {exc}"[:120]}


def _flap_curve_snapshot(curve: ep.FlapCurve) -> dict[str, Any]:
    """The curve we entered on, as JSON: enough to rebuild it for a mark or a curve end."""
    return {"r": curve.r_scaled, "h": curve.h_scaled, "k": curve.k_scaled, "supply": curve.total_supply_atoms,
            "sold": curve.tokens_sold_atoms, "graduation": curve.graduation_tokens_atoms,
            "reserve": curve.quote_reserve_base, "token_decimals": curve.token_decimals,
            "quote_decimals": curve.quote_decimals, "buy_tax_bps": curve.buy_tax_bps, "sell_tax_bps": curve.sell_tax_bps}


def _flap_curve_from_snapshot(snap: Mapping[str, Any]) -> ep.FlapCurve | None:
    try:
        return ep.FlapCurve(
            r_scaled=int(snap["r"]), h_scaled=int(snap["h"]), k_scaled=int(snap["k"]),
            total_supply_atoms=int(snap["supply"]), tokens_sold_atoms=int(snap["sold"]),
            graduation_tokens_atoms=int(snap["graduation"]),
            quote_reserve_base=int(snap["reserve"]) if snap.get("reserve") is not None else None,
            token_decimals=int(snap["token_decimals"]), quote_decimals=int(snap["quote_decimals"]),
            buy_tax_bps=snap.get("buy_tax_bps"), sell_tax_bps=snap.get("sell_tax_bps"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def paper_entry_bsc(launch: lf.Launch, p: Mapping[str, Any], flap: FlapLaunch | None) -> dict[str, Any]:
    """The paper buy, from the read it is given: the fill re-read (:func:`read_flap_fill`,
    basis ``flap_fill:*``) in service, or the decision read (``flap_decision:*``). Pure.

    Tokens: the SMALLER of the portal's own ``quoteExactInput`` (fee, buy tax and any Buy
    Quota cap applied by the venue) and the curve model (``FlapCurve.tokens_out`` after
    ``flap_buy_net_quote``), both for ``paper_spend`` -- what is left after GMGN's
    commission. Never the larger: a quote above the model is a venue we do not understand,
    and booking it would inflate every mark. ``quote_in`` is the full paper size, so the
    commission is in the return. ``shadow_quote`` is the BNB that reached the curve, so a
    mark can put our buy back into the live curve.
    """
    stage = "flap_fill" if flap is not None and flap.note.startswith("fill") else "flap_decision"
    if flap is None or not flap.read:
        return {"basis": f"{stage}:curve_unread", "note": (flap.note if flap is not None else "not_read")[:120]}
    curve = flap.curve
    if curve is None:
        return {"basis": f"{stage}:curve_unpriced", "note": (flap.price_note or flap.note)[:120]}
    if curve.buy_tax_bps is None or curve.sell_tax_bps is None:
        return {"basis": f"{stage}:tax_unread"}
    size, spend = int(flap.paper_size), int(flap.paper_spend or flap.paper_size)
    model, quoted = flap.paper_tokens_model, flap.paper_tokens_quoted
    if quoted is None:
        return {"basis": f"{stage}:quote_unread", "note": "venue_quote_unread"}
    tokens = min(quoted, model) if model else None
    if not size or not tokens:
        return {"basis": f"{stage}:fill_refused", "note": "no_tokens_for_the_paper_size"}
    entry = {"basis": f"{stage}:{'quote' if quoted <= model else 'model'}", "quote_in": size, "tokens": int(tokens),
             "shadow_quote": ep.flap_buy_net_quote(spend, curve.buy_tax_bps),
             "entry_curve": _flap_curve_snapshot(curve), "router_bps": (size - spend) * 10_000 // size}
    if flap.read_ms:
        entry["read_ms"] = flap.read_ms
    return entry


def record_observation(conn: sqlite3.Connection, launch: lf.Launch, verdict: Verdict, entry: Mapping[str, Any],
                       *, at_ms: int | None = None) -> str:
    ensure_table(conn)
    ts = at_ms or now_ms()
    oid = obs_id_for(launch)
    has_entry = entry.get("tokens") is not None and int(entry.get("tokens") or 0) > 0
    conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} (obs_id, chain, token, venue, creator, launched_ms, seen_ms, latency_ms, "
        "fire, rule, reasons_json, features_json, entry_ms, entry_quote_in, entry_tokens, entry_shadow_quote, "
        "entry_basis, status, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, launch.chain.value, launch.token, launch.venue, launch.creator, launch.launched_ms, launch.received_ms,
         launch.latency_ms, int(verdict.fire), verdict.rule, jdump(verdict.reasons), jdump(verdict.features),
         ts if has_entry else None, str(entry.get("quote_in")) if has_entry else None,
         str(entry.get("tokens")) if has_entry else None,
         str(entry.get("shadow_quote")) if has_entry and entry.get("shadow_quote") is not None else None,
         str(entry.get("basis")), "open" if has_entry else "unpriced", ts, ts),
    )
    return oid


def due_marks(conn: sqlite3.Connection, horizons: Sequence[int], *, at_ms: int, limit: int = 100) -> list[dict[str, Any]]:
    """Open observations with at least one horizon reached and not yet marked."""
    ensure_table(conn)
    rows = fetch_all(conn, f"SELECT * FROM {TABLE} WHERE status='open' AND entry_ms IS NOT NULL "
                     "AND entry_ms <= ? ORDER BY entry_ms LIMIT ?", (at_ms - min(horizons) * 1000, limit * 4))
    out = []
    for r in rows:
        marks = jload(r.get("marks_json"), {}) or {}
        if any(str(h) not in marks and at_ms - int(r["entry_ms"]) >= h * 1000 for h in horizons):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def apply_mark(conn: sqlite3.Connection, row: Mapping[str, Any], value: int | None, horizons: Sequence[int], *,
               at_ms: int, status: str | None = None, note: str | None = None) -> None:
    """Record ``value`` (quote units, same unit as ``entry_quote_in``) at every horizon now due.

    ``peak_ratio`` is the best of these MARKS divided by the entry -- three samples at 5, 15
    and 60 minutes, NOT the window's peak: a launch that ran 5x at minute 2 and was dumped by
    minute 5 shows under 1 here. Robinhood rows also get ``path_peak_ratio``, the true
    in-window peak at trade granularity (:func:`mark_rh`); sol rows cannot (:func:`mark_sol`).
    A rule like "peak_ratio >= 1.5" therefore asks "was it up 50% at one of three moments"."""
    marks = jload(row.get("marks_json"), {}) or {}
    age_ms = at_ms - int(row["entry_ms"])
    for h in horizons:
        if str(h) not in marks and age_ms >= h * 1000:
            marks[str(h)] = {"value": str(value) if value is not None else None, "at_ms": at_ms, "note": note}
    peak = Decimal(row.get("peak_ratio") or 0)
    quote_in = int(row["entry_quote_in"])
    if value is not None and quote_in > 0:
        peak = max(peak, Decimal(value) / Decimal(quote_in))
    done = all(str(h) in marks for h in horizons)
    conn.execute(f"UPDATE {TABLE} SET marks_json=?, peak_ratio=?, status=?, updated_ms=? WHERE obs_id=?",
                 (jdump(marks), str(peak), status or ("marked" if done else "open"), at_ms, row["obs_id"]))


def path_min_token_reserve(launch_like: Mapping[str, Any], logs: Sequence[Mapping[str, Any]],
                           token_reserve_now: int) -> tuple[int, int]:
    """``(smallest token reserve the curve passed through, trades)`` over ``logs`` (its
    ``CurveBuy``/``CurveSell`` events after our entry), walking back from the reserve now.
    A buy took ``amount_token`` out, a sell put it back; the quote side follows from
    ``q x t = k`` (both sides of a Pons trade are priced on the constant product, fee and tax
    off the quote leg), so the smallest token reserve is the highest price the window saw,
    at trade granularity. Pure."""
    meta = rh.CurveMeta(curve=str(launch_like.get("curve") or ""), token=str(launch_like.get("token") or ""),
                        deployer="", pair_token=ZERO, launch_config_id=0, graduation_threshold=0, launched_block=0)
    trades = []
    for entry in logs:
        trade = rh.parse_curve_trade(entry, meta=meta)
        if trade is not None:
            trades.append((int(trade.get("slot") or 0), int(trade.get("block_index") or 0), trade["side"],
                           int(trade["amount_token"])))
    t = lowest = int(token_reserve_now)
    for _block, _idx, side, tokens in sorted(trades, reverse=True):
        lowest = min(lowest, t)  # the state just after this trade
        t = t + tokens if side == "buy" else t - tokens
    return min(lowest, t), len(trades)  # ...and the state we entered on


def apply_path_peak(conn: sqlite3.Connection, row: Mapping[str, Any], ratio: Decimal | None, note: str) -> None:
    conn.execute(f"UPDATE {TABLE} SET path_peak_ratio=?, path_peak_note=? WHERE obs_id=?",
                 (str(ratio) if ratio is not None else None, note[:120], row["obs_id"]))


def mark_rh(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], horizons: Sequence[int], *, at_ms: int) -> int:
    """Value due Robinhood observations on the live curve: four reads each, one batched request.

    The observation's LAST mark also asks, in the same batch, for every curve trade since the
    decision block, and records the window's true peak (``path_peak_ratio``, see
    :func:`path_min_token_reserve`): ``peak_ratio`` alone is the best of three sampled marks,
    and a launch that ran 5x at minute 2 and was dumped by minute 5 shows under 1 there."""
    todo = [r for r in rows if r["chain"] == Chain.ROBINHOOD.value]
    if not todo:
        return 0
    feats = [jload(r.get("features_json"), {}) or {} for r in todo]
    calls: list[tuple[str, list[Any]]] = []
    for f in feats:
        calls += reserve_calls(f.get("curve"))
    last = [i for i, r in enumerate(todo) if feats[i].get("decision_block") and feats[i].get("curve")
            and at_ms - int(r["entry_ms"]) >= max(horizons) * 1000]
    for i in last:
        calls.append(("eth_getLogs", [{"address": feats[i]["curve"], "topics": [[rh.TOPIC_CURVE_BUY, rh.TOPIC_CURVE_SELL]],
                                       "fromBlock": _h(int(feats[i]["decision_block"]) + 1), "toBlock": "latest"}]))
    res = rh_rpc(calls, priority=Priority.DISCOVERY, endpoint="snipe.marks", conn=conn)
    if res is None:
        return 0
    n = len(RESERVE_SELECTORS)
    path_logs = dict(zip(last, res[len(todo) * n:], strict=False))
    for i, r in enumerate(todo):
        reserves = parse_reserves(res[i * n:(i + 1) * n])
        if reserves is None:
            continue
        q, real, t = reserves
        f = feats[i]
        fee, ctax = int(f.get("fee_bps", 100)), int(f.get("creator_tax_bps", 0))
        threshold = int(f.get("graduation_threshold") or 0)
        shadow, tokens, quote_in = int(r.get("entry_shadow_quote") or 0), int(r["entry_tokens"]), int(r["entry_quote_in"])
        q2, t2 = with_holding(q, t, shadow)
        value = pons_sell_quote(q2, t2, tokens, fee, ctax)
        graduated = bool(threshold and real >= threshold)
        apply_mark(conn, r, value, horizons, at_ms=at_ms, note="graduated_curve_end" if graduated else None)
        if i not in path_logs and at_ms - int(r["entry_ms"]) >= max(horizons) * 1000:
            apply_path_peak(conn, r, None, "no_decision_block")  # observed before 2026-10-04's build
        elif i in path_logs:
            logs = path_logs[i]
            if not isinstance(logs, list) or quote_in <= 0:
                apply_path_peak(conn, r, None, "logs_unread" if not isinstance(logs, list) else "no_entry")
                continue
            low_t, n_trades = path_min_token_reserve({"curve": f.get("curve"), "token": r["token"]}, logs, t)
            if low_t <= 0 or q <= 0 or t <= 0:
                apply_path_peak(conn, r, None, "path_reserve_nonpositive")
                continue
            q_peak = q * t // low_t
            peak = pons_sell_quote(*with_holding(q_peak, low_t, shadow), tokens, fee, ctax)
            apply_path_peak(conn, r, max(Decimal(peak), Decimal(value)) / Decimal(quote_in), f"curve_path:{n_trades}_trades")
    return len(todo)


def curve_end_value(entry_curve: Mapping[str, Any], shadow: int, tokens: int, *, sol_usd: Decimal) -> int | None:
    """What the paper position sells for at the END of its pump.fun curve: the curve we
    entered on, advanced by our own buy, then by however much buying exhausts its real
    tokens (``CurveState.advance`` stops exactly at that boundary). A curve that completes
    has by definition been bought to there, so this is the least it reached."""
    from kaiba.execution import curve_price as cp

    state, _why = cp.CurveState.build(virtual_sol=int(entry_curve["virtual_sol"]), virtual_token=int(entry_curve["virtual_token"]),
                                      real_sol=int(entry_curve["real_sol"]), real_token=int(entry_curve["real_token"]))
    if state is None:
        return None
    end = state.advance(int(shadow)).advance(10**18)
    fill = cp.quote_sell(end, int(tokens), sol_usd=sol_usd, decimals=6, latency_ms=0)
    return int(fill.amount_out) if fill.ok else None


def mark_sol(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], horizons: Sequence[int], *, at_ms: int) -> int:
    """Value due sol observations on the live curve. ``peak_ratio`` here is ONLY the best of
    the sampled marks (5 / 15 / 60 min): a true in-window peak needs every curve trade, and
    no free stream carries them for a brand-new pump.fun token. A curve that completed
    before a mark is valued at its curve end (:func:`curve_end_value`) and marked
    ``graduated``; until 2026-10-04 it got no value, so its ``peak_ratio`` stayed at the
    marks before (or '0'), and the best outcomes read as the worst."""
    from kaiba.execution import curve_price as cp

    todo = [r for r in rows if r["chain"] == Chain.SOL.value]
    if not todo:
        return 0
    sol_usd = cp.sol_usd_from_native_price(conn)
    for r in todo:
        try:
            feats = jload(r.get("features_json"), {}) or {}
            bc = feats.get("bonding_curve")
            state, note = _sol_curve(conn, r["token"], bc, at_ms)
            if state is None or sol_usd is None:
                graduated = note == "curve_complete" or cp.has_graduated(Chain.SOL, r["token"], conn)
                value = None
                if graduated and sol_usd is not None and isinstance(feats.get("entry_curve"), Mapping):
                    value = curve_end_value(feats["entry_curve"], int(r.get("entry_shadow_quote") or 0),
                                            int(r["entry_tokens"]), sol_usd=sol_usd)
                apply_mark(conn, r, value, horizons, at_ms=at_ms,
                           note=("graduated_curve_end" if value is not None else "graduated") if graduated
                           else f"unread:{note}"[:80],
                           status="graduated" if graduated else None)
                continue
            held = state.advance(int(r.get("entry_shadow_quote") or 0))  # as if the paper buy had landed
            fill = cp.quote_sell(held, int(r["entry_tokens"]), sol_usd=sol_usd, decimals=6, latency_ms=0)
            apply_mark(conn, r, int(fill.amount_out) if fill.ok else None, horizons, at_ms=at_ms,
                       note=None if fill.ok else str(fill.reason)[:80])
        except Exception as exc:  # noqa: BLE001
            log.debug("sol mark failed for %s: %s", r["token"][:10], exc)
    return len(todo)


def _net_of_sell_costs(gross: int | None, sell_tax_bps: int, *, router_bps: int, venue_fee_bps: int) -> int | None:
    """BNB a sell keeps of ``gross``: less the venue's fee (when not already in ``gross``),
    the token's sell tax and GMGN's commission, all off the BNB out (the buy side's measured
    shape, assumed for the sell: UNVERIFIED against a live sell)."""
    if gross is None:
        return None
    keep = 10_000 - int(venue_fee_bps) - int(sell_tax_bps) - max(0, int(router_bps))
    return max(0, int(gross) * max(0, keep) // 10_000)


def _flap_sell_value(curve: ep.FlapCurve | None, tokens: int, sell_tax_bps: int, *, router_bps: int = 0) -> int | None:
    """BNB a sell of ``tokens`` into the curve keeps after the Flap fee, the sell tax and
    GMGN's commission."""
    gross = curve.quote_out(tokens) if curve is not None else None
    return _net_of_sell_costs(gross, sell_tax_bps, router_bps=router_bps, venue_fee_bps=ep.FLAP_PROTOCOL_FEE_BPS)


#: :func:`flap_mark_value`'s status for a graduated token: value it on its DEX pair.
FLAP_MARK_ON_DEX = "dex"


def flap_mark_value(raw_record: Any, entry_curve: Mapping[str, Any], shadow: int,
                    tokens: int, *, router_bps: int = 0) -> tuple[int | None, str | None, str | None]:
    """``(value, note, status)`` of a paper Flap holding from the portal's record now. Pure.

    * on the curve: the LIVE curve with our paper buy put back in (``FlapCurve.with_buy``),
      selling our tokens -- the paper buy never reached the chain, and a fresh curve has
      only our own BNB to buy them back with;
    * graduated (``FLAP_STATUS_DEX``): ``(None, "graduated", FLAP_MARK_ON_DEX)`` -- the
      caller values it on its PancakeSwap V2 pair (:func:`flap_dex_value`). Until
      2026-10-05 it was frozen at the entry curve's graduation point, which is the least a
      graduate reached and ignores what the pair did after;
    * killed: 0;
    * unreadable or any other status: no value, and the note says why.
    """
    snap = _flap_curve_from_snapshot(entry_curve)
    if snap is None:
        return None, "no_entry_curve", None
    sell_tax = int(snap.sell_tax_bps or 0)
    record = ep.flap_record(_words_of(raw_record))
    if record is None:
        return None, "flap_record_unread", None
    if record.status == ep.FLAP_STATUS_DEX:
        return None, "graduated", FLAP_MARK_ON_DEX
    if record.status == ep.FLAP_STATUS_KILLED:
        return 0, "flap_killed", None
    if not record.on_curve:
        return None, f"flap_status:{record.status}", None
    priced, why = ep.parse_flap_record(record, token_decimals=snap.token_decimals,
                                       token_supply_atoms=snap.total_supply_atoms, quote_decimals=snap.quote_decimals)
    if priced is None or priced.curve is None:
        return None, f"unpriced:{why}"[:80], None
    live = priced.curve
    tax = int(live.sell_tax_bps) if live.sell_tax_bps is not None else sell_tax
    value = _flap_sell_value(live.with_buy(shadow, tokens), tokens, tax, router_bps=router_bps)
    return value, None if value is not None else "sell_unpriced", None


def flap_dex_call(token: str, tokens: int) -> tuple[str, list[Any]]:
    """``getAmountsOut(tokens, [token, WBNB])`` on PancakeSwap V2's router: what selling the
    whole paper holding into the pair returns, the pair's 25 bps fee and depth included.
    MEASURED 2026-09-22 (``evm_price``): 5 of 5 graduated Flap tokens live on V2."""
    to, data = ep.encode_amounts_out(int(tokens), [token.lower(), ep.BSC_WRAPPED_NATIVE])
    return ("eth_call", [{"to": to, "data": data}, "latest"])


def flap_dex_value(raw: Any, sell_tax_bps: int, *, router_bps: int = 0) -> int | None:
    """:func:`flap_dex_call`'s answer -> BNB kept after the sell tax and GMGN's commission
    (the pair fee is already in ``getAmountsOut``). ``None`` for a revert or a bad shape."""
    out = ep.decode_amounts_out(raw, hops=1)
    return _net_of_sell_costs(out, sell_tax_bps, router_bps=router_bps, venue_fee_bps=0)


def mark_bsc(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], horizons: Sequence[int], *, at_ms: int,
             rpc: Any = None, router_bps: int | None = None, batch_max: int = BSC_MARK_BATCH_MAX) -> int:
    """Value due bsc observations: ONE ``getTokenV8Safe`` each (supply and decimals are
    immutable and were stored at entry), at most ``batch_max`` per request, and for every
    one that has GRADUATED a second batch prices the holding on its PancakeSwap V2 pair
    (:func:`flap_dex_call`) -- at every horizon, so a graduate keeps being marked. Every
    value is net of the sell tax and GMGN's commission. ``peak_ratio`` here is, as on sol,
    only the best of the sampled marks."""
    todo = [r for r in rows if r["chain"] == Chain.BSC.value][:max(1, int(batch_max))]
    if not todo:
        return 0
    router = _router_bps(params()) if router_bps is None else int(router_bps)
    send = rpc or bsc_rpc
    calls = [("eth_call", [{"to": ep.FLAP_PORTAL, "data": ep.SEL_GET_TOKEN_V8_SAFE + _addr_word(str(r["token"]))},
                           "latest"]) for r in todo]
    res = send(calls, priority=Priority.DISCOVERY, endpoint="snipe.flap_marks", conn=conn)
    if res is None:
        return 0
    on_dex: list[tuple[Mapping[str, Any], int]] = []
    for r, raw in zip(todo, res, strict=False):
        try:
            feats = jload(r.get("features_json"), {}) or {}
            entry_curve = feats.get("entry_curve")
            if not isinstance(entry_curve, Mapping):
                apply_mark(conn, r, None, horizons, at_ms=at_ms, note="no_entry_curve")
                continue
            if raw is None:
                continue  # the read failed this time; the next pass asks again
            value, note, status = flap_mark_value(raw, entry_curve, int(r.get("entry_shadow_quote") or 0),
                                                  int(r["entry_tokens"]), router_bps=router)
            if status == FLAP_MARK_ON_DEX:
                on_dex.append((r, int(entry_curve.get("sell_tax_bps") or 0)))
                continue
            apply_mark(conn, r, value, horizons, at_ms=at_ms, note=note, status=status)
        except Exception as exc:  # noqa: BLE001 - one bad row never stops the marks
            log.debug("bsc mark failed for %s: %s", str(r["token"])[:10], exc)
    if on_dex:
        dex = send([flap_dex_call(str(r["token"]), int(r["entry_tokens"])) for r, _ in on_dex],
                   priority=Priority.DISCOVERY, endpoint="snipe.flap_marks_dex", conn=conn)
        if dex is not None:
            for (r, sell_tax), raw in zip(on_dex, dex, strict=False):
                try:
                    value = flap_dex_value(raw, sell_tax, router_bps=router)
                    apply_mark(conn, r, value, horizons, at_ms=at_ms,
                               note="graduated_pancake_v2" if value is not None else "graduated_dex_unpriced")
                except Exception as exc:  # noqa: BLE001
                    log.debug("bsc dex mark failed for %s: %s", str(r["token"])[:10], exc)
    return len(todo)


# --------------------------------------------------------------------------------------
# into the engine
# --------------------------------------------------------------------------------------


def snipes_today(conn: sqlite3.Connection, chain: Chain, *, at_ms: int | None = None) -> int:
    """Tokens this lane ENTERED on ``chain`` since 00:00 UTC: engine decisions with
    ``action='enter'`` (any mode), one per token.

    MEASURED 2026-10-04 (live box): counting recorded SIGNALS, as this did first, spent the
    whole sol cap by ~01:20 UTC on five signals the engine refused (the sizing bug, every one
    ``size_not_positive``), and every sol launch after that was ``daily_snipe_cap``. A refused
    signal costs nothing, so it is not a snipe."""
    ts = at_ms or now_ms()
    day0 = int(datetime.fromtimestamp(ts / 1000, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    row = fetch_one(conn, "SELECT COUNT(DISTINCT token) AS n FROM decisions WHERE lane=? AND chain=? AND action='enter' "
                    "AND ts_ms>=?", (LANE_VALUE, chain.value, day0))
    return int((row or {}).get("n") or 0)


class DossierBudget:
    """At most ``per_hour`` dossiers in any rolling hour (each one spends GMGN limiter slots)."""

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self.used: deque[float] = deque()

    def take(self, at: float | None = None) -> bool:
        now = at if at is not None else time.time()
        while self.used and now - self.used[0] > 3600:
            self.used.popleft()
        if len(self.used) >= self.per_hour:
            return False
        self.used.append(now)
        return True


def build_signal(launch: lf.Launch, verdict: Verdict) -> Signal:
    return Signal(
        signal_id="sig_snipe_" + digest({"chain": launch.chain.value, "token": launch.token})[:20],
        lane=lane(),
        chain=launch.chain,
        token=launch.token,
        strength=verdict.strength,
        reasons=[verdict.rule, f"venue:{launch.venue}", f"record:{verdict.features.get('record')}"],
        wallets=[w for w in (launch.creator,) if w],
        window_s=None,
        payload={"source": "launch_snipe", "launched_ms": launch.launched_ms, "seen_ms": launch.received_ms,
                 "latency_ms": launch.latency_ms, "tx": launch.tx, "curve": launch.curve,
                 "pair_token": launch.pair_token,
                 **{k: v for k, v in verdict.features.items() if k not in ("latency_ms", "entry_curve")}},
    )


def wait_token_row(conn: sqlite3.Connection, launch: lf.Launch, wait_s: float, *, sleep: Any = time.sleep) -> bool:
    """The engine's launchpad allowlist reads ``tokens``; only the ingest service writes it."""
    deadline = time.monotonic() + wait_s
    while True:
        if fetch_one(conn, "SELECT 1 AS x FROM tokens WHERE chain=? AND address=?", (launch.chain.value, launch.token)):
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)


def gate_size(conn: sqlite3.Connection, launch: lf.Launch, strength: float) -> tuple[int, str]:
    """``RiskGate.position_size`` exactly as the engine calls it for this lane's signal
    (``engine._size_for``: the signal's strength on the risk score scale), and the cause the
    sizer recorded when it answered 0."""
    from kaiba.execution.risk import RiskGate, score_from_strength, zero_size_cause

    size = int(RiskGate().position_size(launch.chain, lane(), score_from_strength(strength), conn, token=launch.token))
    if size > 0:
        return size, "sized"
    return 0, zero_size_cause(conn, launch.chain, lane(), launch.token) or "zero_size_cause_unrecorded"


#: Sizer causes that depend on the dossier, which the precheck runs BEFORE: the launch-wave
#: and creator-share concentration cuts read what the dossier writes. Left to the engine.
DOSSIER_DEPENDENT_SIZE_CAUSES = ("concentration:",)


def band_precheck(conn: sqlite3.Connection, launch: lf.Launch, *, strength: float | None = None,
                  band_for: Any = None, size_for: Any = None) -> tuple[bool, str]:
    """Would the engine size this entry at all? Asked BEFORE a dossier is spent, with the
    functions the engine itself calls, so it can only refuse earlier, never admit what the
    gate would refuse.

    1. The band (``viability.sizing_band``) against the chain minimum.
       MEASURED 2026-10-04 on Robinhood mainnet: an ETH-paired Pons curve with 0% creator
       tax bands 0.00037-0.078 ETH; one with 2% creator tax has no viable size (cheapest
       round trip 8.49% > the 7% ceiling).
    2. The gate's whole sizer at the signal's exact score (:func:`gate_size`): the bankroll,
       the drawdown cut, the exposure cap and the free balance, then the band and the
       minimum. MEASURED 2026-10-04 on the live box: RH 0x2741d56c72 passed the band check
       (its band was ~0.044 ETH, archive replay) and the engine then refused it
       ``below_min_position:8552204939843329<16700000000000000`` -- the prior-day drawdown
       cut (x0.45 on a 0.36 ETH bankroll) shrank the size, not the pool. Every cause but a
       concentration cut is known before the dossier, so every other zero refuses here.

    Each refusal saves a dossier: GMGN limiter capacity shared with live stops.
    """
    if band_for is None:
        from kaiba.execution.viability import sizing_band as band_for
    try:
        band = band_for(launch.chain, conn, token=launch.token)
    except Exception as exc:  # noqa: BLE001 - an unpriceable entry is not an entry
        return False, f"band_unavailable:{type(exc).__name__}"
    hi = getattr(band, "max_viable_base_units", None)
    if hi is None:
        return False, f"no_band:{str(getattr(band, 'reason', ''))[:80]}"
    floor = int(get_risk().chain_budget(launch.chain).min_position_base_units or 0)
    if hi < floor:
        return False, f"band_below_min:{hi}<{floor}"
    if strength is None:
        return True, "band_ok"
    try:
        size, cause = (size_for or gate_size)(conn, launch, strength)
    except Exception as exc:  # noqa: BLE001 - the engine refuses a sizer that raises; so do we
        return False, f"size_unavailable:{type(exc).__name__}"
    if size > 0:
        return True, "size_ok"
    if cause.startswith(DOSSIER_DEPENDENT_SIZE_CAUSES):
        return True, f"size_deferred:{cause[:80]}"
    return False, f"size_zero:{cause[:120]}"


def paper_only_refusal(launch: lf.Launch, *, cfg: Any = None) -> str | None:
    """Why this launch must not be handed to the engine at all, or ``None``.

    On a :data:`PAPER_ONLY_CHAINS` chain the engine may only ever paper-trade it: either the
    lane is not LIVE/CANARY, or its ``live_launchpads_by_chain`` for the chain is EXACTLY
    empty (``bsc: []``), which turns every entry into a SHADOW twin. Exactly empty, not
    "does not name this venue": the engine checks ``tokens.launchpad`` and this checks the
    launch's venue, so ``{bsc: [fourmeme]}`` would admit whatever row reads ``fourmeme``.
    Read with the ENGINE's own parser (``engine._live_launchpads``); a malformed allowlist
    fails closed there, which is paper here. Anything else -- the chain missing from the
    allowlist (unrestricted: live), any launchpad on it, an unreadable config -- refuses,
    and the launch is measured but never signalled. The engine ALSO refuses money on these
    chains whatever the config says (``engine.PAPER_ONLY_LANE_CHAINS``): this guard keeps
    the dossier budget, that one is the money boundary.
    """
    if launch.chain not in PAPER_ONLY_CHAINS:
        return None
    try:
        from kaiba.core.schemas import LaneMode
        from kaiba.execution.engine import _live_launchpads

        c = cfg or get_risk()
        mode = c.effective_mode(lane())
        if mode not in (LaneMode.LIVE, LaneMode.CANARY):
            return None
        allowed = _live_launchpads(c, build_signal(launch, Verdict(True, [], 0.0, "paper_only_check", {})))
    except Exception as exc:  # noqa: BLE001 - what cannot be checked is not paper
        return f"paper_only_unverifiable:{type(exc).__name__}"
    if allowed == frozenset():
        return None
    if allowed is None or launch.venue.lower() in allowed:
        return f"paper_only:{launch.chain.value}:live_allowlist_admits_{launch.venue}"
    return f"paper_only:{launch.chain.value}:live_allowlist_not_empty:{','.join(sorted(allowed))}"[:160]


def hand_to_engine(conn: sqlite3.Connection, launch: lf.Launch, verdict: Verdict, p: Mapping[str, Any],
                   budget: DossierBudget, *, scan: Any = None, record_signal: Any = None,
                   publish: Any = None, precheck: Any = None, paper_guard: Any = None) -> tuple[str | None, str]:
    """Paper-only guard, token row, curve (sol), band precheck, dossier, curve again (sol), signal.

    ``(signal_id, note)``. The token row comes first because the engine's launchpad
    allowlist and the Pons depth read both need it. On Solana the curve is published before
    the precheck (so the band is priced on the curve, not the dossier) and again LAST before
    the signal, so it is the freshest thing the engine reads when it sizes the entry
    (``resolve_depth`` allows 60 s, and the dossier alone takes ~8 s). On bsc nothing is
    published: the band reads the Flap curve live (``viability.read_venue``)."""
    refused = (paper_guard or paper_only_refusal)(launch)
    if refused:
        return None, refused
    if not wait_token_row(conn, launch, float(p.get("token_row_wait_s") or 20)):
        return None, "no_token_row"
    publish = publish or publish_curve_snapshot
    note = ""
    if launch.chain is Chain.SOL:
        _row, depth_note = publish(conn, launch)
        note = f"depth_{depth_note}:"
    ok, why = (precheck or band_precheck)(conn, launch, strength=verdict.strength)
    if not ok:
        return None, note + why
    if not budget.take():
        return None, note + "dossier_budget_exhausted"
    if scan is None:
        from kaiba.intelligence.dyor import scan_token as scan
    try:
        dossier = scan(launch.token, launch.chain)  # opens its own connection (thread-safe)
    except Exception as exc:  # noqa: BLE001
        return None, note + f"dossier_failed:{type(exc).__name__}"
    note += f"dossier:{getattr(getattr(dossier, 'grade', None), 'value', '?')}"
    if getattr(dossier, "blockers", None):
        note += ":blockers"
    if launch.chain is Chain.SOL:
        _row, depth_note = publish(conn, launch)
        note += f":depth_{depth_note}"
    if record_signal is None:
        from kaiba.execution.lanes import record as record_signal
    signal = build_signal(launch, verdict)
    new = record_signal(signal, conn)
    return (signal.signal_id if new else None), note + (":signal" if new else ":signal_exists")


# --------------------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------------------


def memory_probe(*, status_path: str = "/proc/self/status", cgroup_path: str = "/proc/self/cgroup",
                 cgroup_root: str = "/sys/fs/cgroup") -> dict[str, Any]:
    """The process's own memory for the stats line: RSS and threads, plus the cgroup's anon
    vs file split. MEASURED 2026-10-04 on the box: systemd's MemoryCurrent read 412 MB while
    the process RSS was 82 MB -- the rest was page cache from reading the 22 GB database,
    which the kernel reclaims under MemoryMax. Only ``anon`` growing is a leak. Linux only;
    elsewhere the fields are absent. Never raises."""
    import threading

    out: dict[str, Any] = {"threads": threading.active_count()}
    try:
        with open(status_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(cgroup_path, encoding="utf-8") as f:
            rel = next((ln.strip().split("::", 1)[1] for ln in f if ln.startswith("0::")), None)
        if rel is not None:
            with open(f"{cgroup_root.rstrip('/')}/{rel.lstrip('/')}/memory.stat", encoding="utf-8") as f:
                stat = dict(ln.split() for ln in f if len(ln.split()) == 2)
            for key in ("anon", "file"):
                if key in stat:
                    out[f"cg_{key}_mb"] = int(stat[key]) // 2**20
    except (OSError, ValueError, StopIteration):
        pass
    return out


@dataclass(slots=True)
class _BscPending:
    """A bsc launch decided and waiting for its fill re-read (:meth:`Sniper._bsc_finish`)."""

    launch: lf.Launch
    p: Mapping[str, Any]
    verdict: Verdict
    flap: FlapLaunch
    decided_ms: int
    #: When the fill is re-read (epoch ms); 0 = nothing to fill (the read failed or refused).
    fill_due_ms: int


class Sniper:
    """One process: the Pons stream, the sol tail, the marks loop. Never submits an order."""

    def __init__(self, conn: sqlite3.Connection | None = None, *, chains: Sequence[str] | None = None) -> None:
        self.conn = conn or ensure_db()
        ensure_table(self.conn)
        self.p = params()
        self.chains = list(chains or self.p.get("chains") or [])
        self.budget = DossierBudget(int(self.p.get("max_dossiers_per_hour") or 40))
        self._chain_budgets: dict[str, DossierBudget] = {}
        self.stats: dict[str, Any] = {"seen": 0, "fired": 0, "signals": 0, "observed": 0, "marks": 0, "dropped_backlog": 0,
                                      "bsc_unsampled": 0, "bsc_stale": 0}
        RPC_BUCKET[0] = str(self.p.get("rpc_bucket") or rh.PROVIDER)
        BSC_RPC_BUCKET[0] = str(self.p.get("bsc_rpc_bucket") or "bsc-snipe-rpc")
        self._sol_count = 0
        self._bsc_count = 0
        #: Launches being handled. The loop holds only weak references to tasks, so an
        #: unreferenced one can be collected mid-flight; and nothing else bounds how many
        #: wait for the worker threads during a burst (a restart's backlog, a busy minute).
        self._inflight: set[asyncio.Task[None]] = set()

    def spawn(self, launch: lf.Launch) -> asyncio.Task[None] | None:
        """Hand one launch to a worker, or drop it (counted) when ``max_inflight`` are queued."""
        if len(self._inflight) >= max(1, int(self.p.get("max_inflight") or 64)):
            self.stats["dropped_backlog"] += 1
            return None
        task = asyncio.create_task(self.on_launch(launch))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return task

    def refresh(self) -> None:
        self.p = params()
        self.budget.per_hour = int(self.p.get("max_dossiers_per_hour") or 40)
        RPC_BUCKET[0] = str(self.p.get("rpc_bucket") or rh.PROVIDER)
        BSC_RPC_BUCKET[0] = str(self.p.get("bsc_rpc_bucket") or "bsc-snipe-rpc")

    def budget_for(self, chain: Chain) -> DossierBudget:
        """The chain's own dossier budget when ``dossier_budget_by_chain`` names it, else the
        shared one (``max_dossiers_per_hour``) -- the live chains' budget is never spent on a
        chain that has its own."""
        own = self.p.get("dossier_budget_by_chain") or {}
        if not isinstance(own, Mapping) or chain.value not in own:
            return self.budget
        budget = self._chain_budgets.get(chain.value)
        if budget is None:
            budget = self._chain_budgets[chain.value] = DossierBudget(int(own[chain.value] or 0))
        budget.per_hour = int(own[chain.value] or 0)
        return budget

    async def on_launch(self, launch: lf.Launch) -> None:
        self.stats["seen"] += 1
        try:
            if launch.chain is Chain.BSC:
                await self._on_bsc(launch)
            else:
                await asyncio.to_thread(self._handle, launch)
        except Exception:  # noqa: BLE001 - one launch must not stop the feed
            log.exception("launch-snipe: %s failed", launch.key)

    async def _on_bsc(self, launch: lf.Launch) -> None:
        """The bsc path with its fill wait on the event loop, not in a worker thread: the
        ~6 workers also serve the LIVE Robinhood and sol snipes."""
        pending = await asyncio.to_thread(self._bsc_decide, launch)
        if pending is None:
            return
        wait_ms = pending.fill_due_ms - now_ms() if pending.fill_due_ms else 0
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        await asyncio.to_thread(self._bsc_finish, pending)

    def _handle(self, launch: lf.Launch) -> None:
        conn = get_conn()  # this worker thread's own connection; migrated once in __init__
        p = self.p
        if launch.chain is Chain.BSC:
            self._handle_bsc(conn, launch, p)
            return
        if launch.chain is Chain.SOL and launch.venue == "pump.fun":
            self._sol_count += 1
        record = record_for(conn, launch.chain, launch.creator)
        book: EarlyBook | None = None
        early: TrustedEarly | None = None
        head_block = None
        if launch.chain is Chain.ROBINHOOD:
            head_ts, head_block = wait_for_tax(launch, p, conn=conn)
            book = read_early_book(launch, p, head_block, conn=conn) if head_block else EarlyBook(note="chain_stalled")
        if launch.chain.value in [str(c) for c in p.get("trusted_early_chains") or []]:
            # At the normal decision moment, never waited for: what has not bought yet does not count.
            early = trusted_early(conn, launch, book, cohorts_for(conn, launch.chain, p), at_ms=now_ms(),
                                  signer_window_s=int(p.get("trusted_signer_window_s") or 0))
        verdict = evaluate(launch, p, record, book=book, snipes_today=snipes_today(conn, launch.chain), trusted=early)
        verdict.features["curve"] = launch.curve
        if head_block:
            verdict.features["decision_block"] = int(head_block)
        if launch.meta.get("bonding_curve"):
            verdict.features["bonding_curve"] = launch.meta["bonding_curve"]
        if launch.graduation_threshold:
            verdict.features["graduation_threshold"] = str(launch.graduation_threshold)
        measure = (launch.chain is Chain.ROBINHOOD or launch.venue != "pump.fun"
                   or verdict.fire or self._sol_count % max(1, int(p.get("measure_sol_every") or 20)) == 0)
        if measure:
            entry = paper_entry_rh(launch, p, conn=conn) if launch.chain is Chain.ROBINHOOD else paper_entry_sol(launch, p, conn=conn)
            for k in ("fee_bps", "creator_tax_bps", "entry_curve"):
                if k in entry:
                    verdict.features[k] = entry[k]
            oid = record_observation(conn, launch, verdict, entry)
            self.stats["observed"] += 1
        else:
            oid = None
        if not verdict.fire:
            return
        self.stats["fired"] += 1
        signal_id, note = hand_to_engine(conn, launch, verdict, p, self.budget_for(launch.chain))
        if signal_id:
            self.stats["signals"] += 1
        if oid:
            conn.execute(f"UPDATE {TABLE} SET signal_id=?, dossier_note=?, updated_ms=? WHERE obs_id=?",
                         (signal_id, note, now_ms(), oid))
        log.info("launch-snipe %s %s %s -> %s (%s)", launch.chain.value, launch.venue, launch.token[:12], verdict.rule, note)

    def _handle_bsc(self, conn: sqlite3.Connection, launch: lf.Launch, p: Mapping[str, Any], *,
                    sleep: Any = time.sleep) -> None:
        """A Flap launch, synchronously (the service runs :meth:`_on_bsc`, which waits for
        the fill on the event loop instead). The chain is read ONLY for a launch with alpha
        or a 1-in-N sample (~970 launches an hour; module doc), and never for a stale one;
        then the same record / evaluate / measure / hand-off sequence as the other chains."""
        pending = self._bsc_decide(launch, conn=conn, p=p)
        if pending is None:
            return
        wait_ms = pending.fill_due_ms - now_ms() if pending.fill_due_ms else 0
        if wait_ms > 0:
            sleep(wait_ms / 1000)
        self._bsc_finish(pending, conn=conn)

    def _bsc_decide(self, launch: lf.Launch, *, conn: sqlite3.Connection | None = None,
                    p: Mapping[str, Any] | None = None) -> _BscPending | None:
        conn = conn or get_conn()
        p = p if p is not None else self.p
        self._bsc_count += 1
        record = record_for(conn, launch.chain, launch.creator)
        today = snipes_today(conn, launch.chain)
        alpha = evaluate(launch, p, record, snipes_today=today)  # no chain read: is there a rule at all?
        sampled = self._bsc_count % max(1, int(p.get("measure_bsc_every") or 20)) == 0
        if not alpha.rule and not sampled:
            self.stats["bsc_unsampled"] += 1
            return None
        max_age_ms = int(float(p.get("bsc_max_launch_age_s") or 60) * 1000)
        if launch.latency_ms is None or launch.latency_ms > max_age_ms:
            self.stats["bsc_stale"] += 1  # no read spent on what bsc_vetoes would refuse anyway
            return None
        flap = read_flap_launch(launch, p, conn=conn,
                                priority=Priority.ENTRY if alpha.rule else Priority.DISCOVERY)
        verdict = evaluate(launch, p, record, snipes_today=today, flap=flap,
                           native_ok=bsc_native_price_ok(conn))
        decided_ms = now_ms()
        # Only a launch that could be entered at all is re-read for its fill.
        fill_due = (decided_ms + max(0, int(p.get("bsc_exec_latency_ms") or 0))
                    if flap.read and flap.curve is not None else 0)
        return _BscPending(launch=launch, p=p, verdict=verdict, flap=flap, decided_ms=decided_ms,
                           fill_due_ms=fill_due)

    def _bsc_finish(self, pending: _BscPending, *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or get_conn()
        launch, p, verdict = pending.launch, pending.p, pending.verdict
        fill = read_flap_fill(launch, p, pending.flap, conn=conn) if pending.fill_due_ms else pending.flap
        entry = paper_entry_bsc(launch, p, fill)
        if "entry_curve" in entry:
            verdict.features["entry_curve"] = entry["entry_curve"]
        if pending.fill_due_ms:
            verdict.features["fill_latency_ms"] = (fill.read_ms or now_ms()) - pending.decided_ms
            if fill.paper_tokens_model and pending.flap.paper_tokens_model:
                verdict.features["fill_vs_decision_bps"] = (
                    fill.paper_tokens_model * 10_000 // pending.flap.paper_tokens_model)
        oid = record_observation(conn, launch, verdict, entry)
        self.stats["observed"] += 1
        if not verdict.fire:
            return
        self.stats["fired"] += 1
        signal_id, note = hand_to_engine(conn, launch, verdict, p, self.budget_for(launch.chain))
        if signal_id:
            self.stats["signals"] += 1
        conn.execute(f"UPDATE {TABLE} SET signal_id=?, dossier_note=?, updated_ms=? WHERE obs_id=?",
                     (signal_id, note, now_ms(), oid))
        log.info("launch-snipe bsc flap %s -> %s (%s)", launch.token[:12], verdict.rule, note)

    def mark_once(self) -> int:
        conn = get_conn()
        horizons = [int(h) for h in self.p.get("mark_horizons_s") or [300, 900, 3600]]
        at = now_ms()
        rows = due_marks(conn, horizons, at_ms=at)
        n = (mark_rh(conn, rows, horizons, at_ms=at) + mark_sol(conn, rows, horizons, at_ms=at)
             + mark_bsc(conn, rows, horizons, at_ms=at, router_bps=_router_bps(self.p)))
        self.stats["marks"] += n
        return n

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._marks(stop)), asyncio.create_task(self._params(stop))]
        if Chain.ROBINHOOD.value in self.chains:
            tasks.append(asyncio.create_task(self._pons(stop)))
        if Chain.SOL.value in self.chains:
            tasks.append(asyncio.create_task(self._sol(stop)))
        if Chain.BSC.value in self.chains:
            tasks.append(asyncio.create_task(self._flap(stop)))
        await stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pons(self, stop: asyncio.Event) -> None:
        url = get_settings().rpc_for(Chain.ROBINHOOD)
        if not url:
            log.error("launch-snipe: no Robinhood RPC configured; Pons launches are not watched")
            return
        async for launch in lf.stream_pons(url, stop=stop):
            if launch.backfilled and launch.latency_ms is not None and launch.latency_ms > 60_000:
                continue  # a launch a minute old is not a snipe; the gap refill is for the record
            self.spawn(launch)

    async def _sol(self, stop: asyncio.Event) -> None:
        mark = lf.sol_start_mark(self.conn)
        while not stop.is_set():
            for launch in lf.tail_sol(self.conn, mark):
                self.spawn(launch)
            await asyncio.sleep(0.5)

    async def _flap(self, stop: asyncio.Event) -> None:
        """Flap launches from the rows ``ingest/flap.py`` writes (the socket is THAT
        process's; a second subscription here would double ~38M CU a month)."""
        mark = lf.bsc_start_mark()
        while not stop.is_set():
            try:
                launches = lf.tail_bsc(self.conn, mark)
            except sqlite3.Error as exc:  # a locked database is a missed poll, not a dead task
                log.warning("launch-snipe: bsc tail unreadable: %s", exc)
                launches = []
            for launch in launches:
                self.spawn(launch)
            await asyncio.sleep(0.5)

    async def _marks(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.mark_once)
            except Exception:  # noqa: BLE001
                log.exception("launch-snipe marks failed")
            await asyncio.sleep(30)

    async def _params(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(60)
            self.refresh()
            log.info("launch-snipe stats %s", {**self.stats, "inflight": len(self._inflight), **memory_probe()})


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.execution.snipe", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["run", "mark-once"])
    ap.add_argument("--chains", default="", help="comma-separated; default: the lane's params")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    lane()  # fail fast if the Lane value is not wired yet
    sniper = Sniper(chains=[c for c in args.chains.split(",") if c] or None)
    if args.command == "mark-once":
        print(sniper.mark_once())
        return 0
    stop = asyncio.Event()
    try:
        asyncio.run(sniper.run(stop))
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
