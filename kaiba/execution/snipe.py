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
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import ensure_db, fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EvidenceBasis, Lane, Signal, digest, now_ms
from kaiba.ingest import launch_feed as lf
from kaiba.ingest import robinhood as rh

log = logging.getLogger(__name__)

LANE_VALUE = "launch-snipe"
#: The limiter bucket Robinhood reads are charged to. Default: the shared ``robinhood-rpc``
#: bucket, where protection's EXIT reads always go first. ``params.rpc_bucket`` can move the
#: lane to its own bucket (an unconfigured name gets the limiter's 1/s default).
RPC_BUCKET: list[str] = [rh.PROVIDER]
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
    "venues": {"robinhood": ["pons"], "sol": ["launchlab", "pump.fun"]},
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
    "max_snipes_per_day": {"robinhood": 10, "sol": 10},
    "max_dossiers_per_hour": 40,
    "token_row_wait_s": 20,
    "measure_sol_every": 20,
    "paper_size": {"robinhood": 16_700_000_000_000_000, "sol": 370_000_000},
    "mark_horizons_s": [300, 900, 3600],
    "sol_exec_latency_ms": 4000,
    "chain_wait_max_s": 20,
    "chain_clock_margin_ms": 250,
    "rpc_bucket": "robinhood-rpc",
}

#: Every number above, and what would settle it. ``tests/test_snipe_params.py`` pins that
#: each param has an entry.
PARAMS_PROVENANCE: dict[str, str] = {
    "chains": "OWNER 2026-10-03: Solana and Robinhood.",
    "venues": "MEASURED mooner-launchpad-edge (launchlab 2.69, pons_v2 2.53); pump.fun kept for record/watch fires only via fire rules.",
    "dev_watchlist": "OWNER: wallets whose launches to snipe; empty until the owner names some.",
    "name_watchlist": "OWNER: names/tickers to snipe; empty until the owner names some.",
    "fire_on_records": "MEASURED deployer-record-edge: low/runner 17.0%, mid/runner 19.2% reach 2x vs 13.2% baseline (sol, n=47/125).",
    "never_records": "MEASURED deployer-record-edge: spam/all_dud 5.3% reach 2x, 0.4% 5x (n=266).",
    "max_entry_tax_bps": "DERIVED: 0 = start only once the toll is gone; GMGN cannot time a +1 s / +2 s landing.",
    "exempt_buyer_veto": "MEASURED REFUTATION s.4: exempt first-second buyer -> -9.18% / -7.00% train / held out.",
    "exempt_toll_bps": "DERIVED: a buy in second 0 or 1 owes 9900 or 618 bps; under 100 bps it was exempted.",
    "dev_atomic_veto_wei": "MEASURED line4 s.4: dev atomic buy < 0.03 ETH and no demand in 2 s -> -3.65% / -6.28%.",
    "veto_demand_window_s": "MEASURED: the 2 s window of the same screen.",
    "watch_strength": "DERIVED: 0.75 -> score 75, the ladder's bottom rung (flat size, owner directive).",
    "record_strength": "DERIVED: 0.72 -> score 72, the bottom rung; above 0.70 so it sizes at all.",
    "max_snipes_per_day": "INVENTED: 10 per chain per UTC day while the lane is unmeasured; settled by snipe_observations.",
    "max_dossiers_per_hour": "INVENTED: each dossier spends GMGN limiter capacity shared with live stops.",
    "token_row_wait_s": "MEASURED: the RH poller records a launch at p50 6.5 s / p90 11.0 s.",
    "measure_sol_every": "INVENTED: 1 in 20 pump.fun launches measured (all LaunchLab); RPC budget.",
    "paper_size": "DERIVED: each chain's min position (risk.yaml 10-03: RH 0.0167 ETH, sol 0.37 SOL).",
    "mark_horizons_s": "INVENTED: 5 / 15 / 60 min; quiet-token-is-terminal says 88% never trade after 1 h.",
    "sol_exec_latency_ms": "INVENTED: GMGN entry delay assumed for the paper entry's moment; settle from fills.",
    "chain_wait_max_s": "DERIVED: give up on the RH tax wait if the chain head stalls this long.",
    "rpc_bucket": "DERIVED: share protection's bucket (EXIT outranks ENTRY/DISCOVERY); ~5 reads a launch, ~25 launches/h.",
    "chain_clock_margin_ms": "DERIVED: ~10 blocks share each whole-second timestamp, so the head shows the new second within ~100 ms of it starting.",
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
        if ts_s is None:
            continue
        elapsed = ts_s - launched_s
        if elapsed < demand_window_s:
            book.outside_buys_window += 1
        if elapsed in (0, 1) and quote_in > 0:
            toll_bps = fee * 10_000 // quote_in - PONS_PROTOCOL_FEE_BPS
            if toll_bps < exempt_toll_bps:
                book.exempt_buyers.append(str(trade.get("recipient") or trade["wallet"]))
    return book


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
             snipes_today: int = 0) -> Verdict:
    """Fire or skip, with every reason. Pure."""
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

    reasons: list[str] = []
    if launch.venue.lower() not in venues:
        reasons.append(f"venue_not_enabled:{launch.venue}")
    if record.label in set(p.get("never_records") or []) and not watched:
        reasons.append(f"deployer_record:{record.label}")
    if watched:
        rule, strength = "dev_watchlist", float(p.get("watch_strength", 0.75))
    elif names:
        rule, strength = "name_watchlist", float(p.get("watch_strength", 0.75))
    elif record.label in set(p.get("fire_on_records") or []):
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
    limit = int(p.get("max_entry_tax_bps") or 0)
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


def read_pump_curve(conn: Any, bonding_curve: str | None, *, at_ms: int | None = None) -> tuple[Any, str]:
    """The pump.fun curve straight from its account on the configured Solana RPC (Alchemy on
    the box), as a ``curve_price.CurveState``. ``(None, why)`` when it cannot be read.

    Kaiba's ``live_resolver`` reads pump.fun's ``/coins/{mint}`` route instead; MEASURED
    2026-10-03 from the operator's desktop that route answered 404 for new and old mints
    alike, so the account is read first and the route is only the fallback."""
    import base64

    from kaiba.execution import curve_price as cp
    from kaiba.providers._http import post_json

    if not bonding_curve:
        return None, "no_bonding_curve_key"
    url = get_settings().rpc_for(Chain.SOL)
    if not url:
        return None, "no_solana_rpc"
    at = at_ms or now_ms()
    got = post_json("rpc", "sol.getAccountInfo", url, json_body={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                    "params": [bonding_curve, {"encoding": "base64", "commitment": "processed"}]},
                    priority=Priority.DISCOVERY, ttl_s=0.0, cache_key=f"snipe:curve:{bonding_curve}:{at}",
                    wait_for_slot_s=5.0, timeout_s=10.0, conn=conn)
    value = ((got.data or {}).get("result") or {}).get("value") if got.ok and isinstance(got.data, Mapping) else None
    if not isinstance(value, Mapping) or not value.get("data"):
        return None, "curve_account_unread"
    fields = decode_pump_curve(base64.b64decode(value["data"][0]))
    if fields is None:
        return None, "curve_account_short"
    if fields["complete"]:
        return None, "curve_complete"
    return cp.CurveState.build(virtual_sol=fields["virtual_sol"], virtual_token=fields["virtual_token"],
                               real_sol=fields["real_sol"], real_token=fields["real_token"], observed_ms=at,
                               source="pumpfun_account")


def _sol_curve(conn: Any, token: str, bonding_curve: str | None, at_ms: int) -> tuple[Any, str]:
    state, note = read_pump_curve(conn, bonding_curve, at_ms=at_ms)
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
                "shadow_quote": int(fill.curve_in)}
    except Exception as exc:  # noqa: BLE001 - measurement must never take the lane down
        return {"basis": "error", "note": f"{type(exc).__name__}: {exc}"[:120]}


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
    """Record ``value`` (quote units, same unit as ``entry_quote_in``) at every horizon now due."""
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


def mark_rh(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], horizons: Sequence[int], *, at_ms: int) -> int:
    """Value due Robinhood observations on the live curve: four reads each, one batched request."""
    todo = [r for r in rows if r["chain"] == Chain.ROBINHOOD.value]
    if not todo:
        return 0
    feats = [jload(r.get("features_json"), {}) or {} for r in todo]
    calls: list[tuple[str, list[Any]]] = []
    for f in feats:
        calls += reserve_calls(f.get("curve"))
    res = rh_rpc(calls, priority=Priority.DISCOVERY, endpoint="snipe.marks", conn=conn)
    if res is None:
        return 0
    n = len(RESERVE_SELECTORS)
    for i, r in enumerate(todo):
        reserves = parse_reserves(res[i * n:(i + 1) * n])
        if reserves is None:
            continue
        q, real, t = reserves
        f = feats[i]
        fee, ctax = int(f.get("fee_bps", 100)), int(f.get("creator_tax_bps", 0))
        threshold = int(f.get("graduation_threshold") or 0)
        q2, t2 = with_holding(q, t, int(r.get("entry_shadow_quote") or 0))
        value = pons_sell_quote(q2, t2, int(r["entry_tokens"]), fee, ctax)
        graduated = bool(threshold and real >= threshold)
        apply_mark(conn, r, value, horizons, at_ms=at_ms, note="graduated_curve_end" if graduated else None)
    return len(todo)


def mark_sol(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]], horizons: Sequence[int], *, at_ms: int) -> int:
    from kaiba.execution import curve_price as cp

    todo = [r for r in rows if r["chain"] == Chain.SOL.value]
    if not todo:
        return 0
    sol_usd = cp.sol_usd_from_native_price(conn)
    for r in todo:
        try:
            bc = (jload(r.get("features_json"), {}) or {}).get("bonding_curve")
            state, note = _sol_curve(conn, r["token"], bc, at_ms)
            if state is None or sol_usd is None:
                graduated = note == "curve_complete" or cp.has_graduated(Chain.SOL, r["token"], conn)
                apply_mark(conn, r, None, horizons, at_ms=at_ms, note="graduated" if graduated else f"unread:{note}"[:80],
                           status="graduated" if graduated else None)
                continue
            held = state.advance(int(r.get("entry_shadow_quote") or 0))  # as if the paper buy had landed
            fill = cp.quote_sell(held, int(r["entry_tokens"]), sol_usd=sol_usd, decimals=6, latency_ms=0)
            apply_mark(conn, r, int(fill.amount_out) if fill.ok else None, horizons, at_ms=at_ms,
                       note=None if fill.ok else str(fill.reason)[:80])
        except Exception as exc:  # noqa: BLE001
            log.debug("sol mark failed for %s: %s", r["token"][:10], exc)
    return len(todo)


# --------------------------------------------------------------------------------------
# into the engine
# --------------------------------------------------------------------------------------


def snipes_today(conn: sqlite3.Connection, chain: Chain, *, at_ms: int | None = None) -> int:
    """Signals this lane recorded on ``chain`` since 00:00 UTC (fired, not necessarily filled)."""
    ts = at_ms or now_ms()
    day0 = int(datetime.fromtimestamp(ts / 1000, tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    row = fetch_one(conn, "SELECT COUNT(*) AS n FROM signals WHERE lane=? AND chain=? AND created_ms>=?",
                    (LANE_VALUE, chain.value, day0))
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
                 "pair_token": launch.pair_token, **{k: v for k, v in verdict.features.items() if k != "latency_ms"}},
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


def hand_to_engine(conn: sqlite3.Connection, launch: lf.Launch, verdict: Verdict, p: Mapping[str, Any],
                   budget: DossierBudget, *, scan: Any = None, record_signal: Any = None) -> tuple[str | None, str]:
    """Build the dossier, wait for the token row, record the signal. ``(signal_id, note)``."""
    if not budget.take():
        return None, "dossier_budget_exhausted"
    if scan is None:
        from kaiba.intelligence.dyor import scan_token as scan
    try:
        dossier = scan(launch.token, launch.chain)  # opens its own connection (thread-safe)
    except Exception as exc:  # noqa: BLE001
        return None, f"dossier_failed:{type(exc).__name__}"
    note = f"dossier:{getattr(getattr(dossier, 'grade', None), 'value', '?')}"
    if getattr(dossier, "blockers", None):
        note += ":blockers"
    if not wait_token_row(conn, launch, float(p.get("token_row_wait_s") or 20)):
        return None, note + ":no_token_row"
    if record_signal is None:
        from kaiba.execution.lanes import record as record_signal
    signal = build_signal(launch, verdict)
    new = record_signal(signal, conn)
    return (signal.signal_id if new else None), note + (":signal" if new else ":signal_exists")


# --------------------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------------------


class Sniper:
    """One process: the Pons stream, the sol tail, the marks loop. Never submits an order."""

    def __init__(self, conn: sqlite3.Connection | None = None, *, chains: Sequence[str] | None = None) -> None:
        self.conn = conn or ensure_db()
        ensure_table(self.conn)
        self.p = params()
        self.chains = list(chains or self.p.get("chains") or [])
        self.budget = DossierBudget(int(self.p.get("max_dossiers_per_hour") or 40))
        self.stats: dict[str, Any] = {"seen": 0, "fired": 0, "signals": 0, "observed": 0, "marks": 0}
        RPC_BUCKET[0] = str(self.p.get("rpc_bucket") or rh.PROVIDER)
        self._sol_count = 0

    def refresh(self) -> None:
        self.p = params()
        self.budget.per_hour = int(self.p.get("max_dossiers_per_hour") or 40)
        RPC_BUCKET[0] = str(self.p.get("rpc_bucket") or rh.PROVIDER)

    async def on_launch(self, launch: lf.Launch) -> None:
        self.stats["seen"] += 1
        try:
            await asyncio.to_thread(self._handle, launch)
        except Exception:  # noqa: BLE001 - one launch must not stop the feed
            log.exception("launch-snipe: %s failed", launch.key)

    def _handle(self, launch: lf.Launch) -> None:
        conn = get_conn()  # this worker thread's own connection; migrated once in __init__
        p = self.p
        if launch.chain is Chain.SOL and launch.venue == "pump.fun":
            self._sol_count += 1
        record = record_for(conn, launch.chain, launch.creator)
        book: EarlyBook | None = None
        if launch.chain is Chain.ROBINHOOD:
            head_ts, head_block = wait_for_tax(launch, p, conn=conn)
            book = read_early_book(launch, p, head_block, conn=conn) if head_block else EarlyBook(note="chain_stalled")
        verdict = evaluate(launch, p, record, book=book, snipes_today=snipes_today(conn, launch.chain))
        verdict.features["curve"] = launch.curve
        if launch.meta.get("bonding_curve"):
            verdict.features["bonding_curve"] = launch.meta["bonding_curve"]
        if launch.graduation_threshold:
            verdict.features["graduation_threshold"] = str(launch.graduation_threshold)
        measure = (launch.chain is Chain.ROBINHOOD or launch.venue != "pump.fun"
                   or verdict.fire or self._sol_count % max(1, int(p.get("measure_sol_every") or 20)) == 0)
        if measure:
            entry = paper_entry_rh(launch, p, conn=conn) if launch.chain is Chain.ROBINHOOD else paper_entry_sol(launch, p, conn=conn)
            for k in ("fee_bps", "creator_tax_bps"):
                if k in entry:
                    verdict.features[k] = entry[k]
            oid = record_observation(conn, launch, verdict, entry)
            self.stats["observed"] += 1
        else:
            oid = None
        if not verdict.fire:
            return
        self.stats["fired"] += 1
        signal_id, note = hand_to_engine(conn, launch, verdict, p, self.budget)
        if signal_id:
            self.stats["signals"] += 1
        if oid:
            conn.execute(f"UPDATE {TABLE} SET signal_id=?, dossier_note=?, updated_ms=? WHERE obs_id=?",
                         (signal_id, note, now_ms(), oid))
        log.info("launch-snipe %s %s %s -> %s (%s)", launch.chain.value, launch.venue, launch.token[:12], verdict.rule, note)

    def mark_once(self) -> int:
        conn = get_conn()
        horizons = [int(h) for h in self.p.get("mark_horizons_s") or [300, 900, 3600]]
        at = now_ms()
        rows = due_marks(conn, horizons, at_ms=at)
        n = mark_rh(conn, rows, horizons, at_ms=at) + mark_sol(conn, rows, horizons, at_ms=at)
        self.stats["marks"] += n
        return n

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._marks(stop)), asyncio.create_task(self._params(stop))]
        if Chain.ROBINHOOD.value in self.chains:
            tasks.append(asyncio.create_task(self._pons(stop)))
        if Chain.SOL.value in self.chains:
            tasks.append(asyncio.create_task(self._sol(stop)))
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
            asyncio.create_task(self.on_launch(launch))

    async def _sol(self, stop: asyncio.Event) -> None:
        mark = lf.sol_start_mark(self.conn)
        while not stop.is_set():
            for launch in lf.tail_sol(self.conn, mark):
                asyncio.create_task(self.on_launch(launch))
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
            log.info("launch-snipe stats %s", self.stats)


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
