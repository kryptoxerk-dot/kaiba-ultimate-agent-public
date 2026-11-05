# Codex 2026-09-27: owner-requested repair integration; see docs/TASKS.md REPAIR-INTEGRATION-20260927.
"""Which wallets would have made *us* money if we had copied them?

Not "which wallets are profitable". That question is answered by every vendor leaderboard
and it is the wrong one, for three reasons this module refuses to paper over:

* **We cannot fill at their price.** We learn of a wallet's trade from a feed. The
  ``gmgn:smartmoney`` route is a 60-second sweep (``gmgn_feeds.run``), so a trade can be
  most of a minute old before it reaches our tape, and only then can we act. Our fill is
  therefore the *next print at or after* their trade plus that delay -- never their own
  price. MEASURED 2026-09-22: the median gap between consecutive prints on an active sol
  token is 5.6 s (p25 1.2 s, p75 85.6 s, n=2,000 tokens with >= 10 prints), so a next-print
  fill is usually available on a token with a real tape and usually is not on one without.
* **Costs are ours, not theirs.** GMGN takes 1% per leg. A wallet whose gross round trip
  is +1.5% loses us money.
* **Past profit is mostly luck at this n.** The decisive question is not who ranked well
  but whether ranking well in one period predicts anything in the next. That is
  :func:`persistence`, and it is the only function here whose answer should change a
  decision. Everything above it is bookkeeping.

**Selection bias, stated once.** MEASURED 2026-09-22 by the entry study: tokens our
scanner built a dossier for returned -20.88% mean against +11.30% for tokens it never
looked at. Whatever the scanner selects for is adverse. So the candidate pool here is
**every wallet in the tape with enough trades**, never the wallets our own scoring already
liked -- otherwise a copy-trade ranker inherits that bias and launders it as a new signal.

Nothing here writes to a trading path. It measures and ranks; wiring is a separate,
evidence-gated decision.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, get_conn
from kaiba.core.schemas import Chain, is_quote_asset

__all__ = [
    "COPY_LAG_MS",
    "ROUND_TRIP_FEE_BPS",
    "CopyTrip",
    "WalletCopyReport",
    "next_print",
    "round_trips",
    "evaluate_wallet",
    "rank_wallets",
    "persistence",
]

#: How late we learn of another wallet's trade, in milliseconds. MEASURED: the
#: ``gmgn:smartmoney`` and ``gmgn:kol`` routes are polled on a 60 s sweep
#: (``kaiba.ingest.gmgn_feeds.run``, ``interval_s=60``), so one sweep is the realistic
#: worst case and the honest default. Pass a smaller value to price the counterfactual of
#: a faster feed -- that is what the sensitivity curve in :func:`evaluate_wallet` is for.
COPY_LAG_MS = 60_000

#: Round-trip venue cost in basis points. MEASURED: GMGN takes 1% per leg, both legs.
#: Slippage and gas are NOT included here, so every number this module produces is
#: optimistic by that margin and should be read as an upper bound.
ROUND_TRIP_FEE_BPS = 200


def _dec(value: Any) -> Decimal | None:
    """A positive Decimal, or None. Never 0 as a stand-in for unknown."""
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return out if out > 0 else None


@dataclass(frozen=True)
class CopyTrip:
    """One round trip of theirs, priced as *we* could have copied it."""

    chain: Chain
    token: str
    wallet: str
    their_buy_ms: int
    their_sell_ms: int
    our_entry_price: Decimal | None
    our_exit_price: Decimal | None
    our_entry_ms: int | None
    our_exit_ms: int | None
    #: None when either leg was unfillable. NEVER 0 -- an uncopyable trade is not a
    #: break-even one, and averaging it in as 0 is how a ranker invents an edge.
    net_return: Decimal | None
    reason: str

    @property
    def fillable(self) -> bool:
        return self.net_return is not None


@dataclass
class WalletCopyReport:
    """What copying this wallet would have done to us."""

    wallet: str
    chain: Chain
    trips_seen: int = 0
    trips_fillable: int = 0
    mean_net: Decimal | None = None
    median_net: Decimal | None = None
    win_rate: float | None = None
    total_net: Decimal | None = None
    first_ms: int | None = None
    last_ms: int | None = None
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float | None:
        """Share of their round trips we could actually have copied."""
        if not self.trips_seen:
            return None
        return self.trips_fillable / self.trips_seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "chain": self.chain.value,
            "trips_seen": self.trips_seen,
            "trips_fillable": self.trips_fillable,
            "coverage": self.coverage,
            "mean_net_pct": float(self.mean_net * 100) if self.mean_net is not None else None,
            "median_net_pct": float(self.median_net * 100) if self.median_net is not None else None,
            "win_rate": self.win_rate,
            "total_net_pct": float(self.total_net * 100) if self.total_net is not None else None,
            "reasons": dict(self.reasons),
        }


def next_print(
    conn: sqlite3.Connection,
    chain: Chain,
    token: str,
    at_ms: int,
    *,
    max_wait_ms: int = 600_000,
    until_ms: int | None = None,
) -> tuple[Decimal, int] | None:
    """The first priced trade on ``token`` at or after ``at_ms`` -- our achievable fill.

    ``max_wait_ms`` bounds how long we would sit waiting for a print. Beyond it the copy
    simply did not happen: returning a much later price would be inventing a fill at a
    moment we would never have transacted.
    """
    # A holdout boundary bounds our simulated fills too, not only the leader's
    # trades. Otherwise future prices leak into training ranks. Codex, 2026-09-27.
    latest_ms = int(at_ms) + int(max_wait_ms)
    if until_ms is not None:
        latest_ms = min(latest_ms, int(until_ms))
    if latest_ms < int(at_ms):
        return None
    rows = fetch_all(
        conn,
        "SELECT ts_ms, price_usd FROM swaps "
        "WHERE chain = ? AND token = ? AND ts_ms >= ? AND ts_ms <= ? "
        "  AND price_usd IS NOT NULL AND price_usd != '' "
        "ORDER BY ts_ms ASC LIMIT 1",
        (chain.value, token, int(at_ms), latest_ms),
    )
    if not rows:
        return None
    price = _dec(rows[0]["price_usd"])
    if price is None:
        return None
    return price, int(rows[0]["ts_ms"])


def round_trips(conn: sqlite3.Connection, wallet: str, chain: Chain) -> list[tuple[str, int, int]]:
    """``(token, first_buy_ms, closing_sell_ms)`` for each of this wallet's episodes.

    An episode opens on a buy with no position and closes on the first sell after it. A
    wallet that buys three times and then sells yields one episode, which is the shape a
    copier would actually have traded: we would have entered when we saw the first buy
    and exited when we saw them sell.
    """
    rows = fetch_all(
        conn,
        "SELECT token, ts_ms, side FROM swaps "
        "WHERE wallet = ? AND chain = ? AND token != '' ORDER BY ts_ms ASC, id ASC",
        (wallet, chain.value),
    )
    open_buy: dict[str, int] = {}
    out: list[tuple[str, int, int]] = []
    for row in rows:
        token, ts, side = str(row["token"]), int(row["ts_ms"]), str(row["side"])
        if is_quote_asset(chain, token):
            continue
        if side == "buy":
            open_buy.setdefault(token, ts)
        elif side == "sell" and token in open_buy:
            out.append((token, open_buy.pop(token), ts))
    return out


def evaluate_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    chain: Chain = Chain.SOL,
    *,
    lag_ms: int = COPY_LAG_MS,
    fee_bps: int = ROUND_TRIP_FEE_BPS,
    since_ms: int | None = None,
    until_ms: int | None = None,
) -> WalletCopyReport:
    """Copy every round trip this wallet made in the window, and total the damage."""
    report = WalletCopyReport(wallet=wallet, chain=chain)
    fee = Decimal(fee_bps) / Decimal(10_000)
    nets: list[Decimal] = []
    for token, buy_ms, sell_ms in round_trips(conn, wallet, chain):
        if since_ms is not None and buy_ms < since_ms:
            continue
        if until_ms is not None and sell_ms > until_ms:
            continue
        report.trips_seen += 1
        report.first_ms = buy_ms if report.first_ms is None else min(report.first_ms, buy_ms)
        report.last_ms = sell_ms if report.last_ms is None else max(report.last_ms, sell_ms)

        entry = next_print(conn, chain, token, buy_ms + lag_ms, until_ms=until_ms)
        if entry is None:
            report.reasons["no_entry_print"] = report.reasons.get("no_entry_print", 0) + 1
            continue
        exit_ = next_print(conn, chain, token, max(sell_ms + lag_ms, entry[1]), until_ms=until_ms)
        if exit_ is None:
            report.reasons["no_exit_print"] = report.reasons.get("no_exit_print", 0) + 1
            continue
        gross = exit_[0] / entry[0] - Decimal(1)
        nets.append(gross - fee)
        report.trips_fillable += 1

    if nets:
        report.mean_net = sum(nets) / Decimal(len(nets))
        ordered = sorted(nets)
        mid = len(ordered) // 2
        report.median_net = (
            ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / Decimal(2)
        )
        report.win_rate = sum(1 for n in nets if n > 0) / len(nets)
        report.total_net = sum(nets)
    return report


def _candidate_wallets(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    min_buys: int,
    min_sells: int,
    since_ms: int | None,
    until_ms: int | None,
    limit: int,
) -> list[str]:
    """Every wallet in the tape with enough activity. Deliberately NOT our scored ones."""
    clauses = ["chain = ?", "token != ''"]
    params: list[Any] = [chain.value]
    if since_ms is not None:
        clauses.append("ts_ms >= ?")
        params.append(int(since_ms))
    if until_ms is not None:
        clauses.append("ts_ms <= ?")
        params.append(int(until_ms))
    params.extend([int(min_buys), int(min_sells), int(limit)])
    rows = fetch_all(
        conn,
        f"SELECT wallet FROM swaps WHERE {' AND '.join(clauses)} GROUP BY wallet "
        "HAVING SUM(side = 'buy') >= ? AND SUM(side = 'sell') >= ? LIMIT ?",
        tuple(params),
    )
    return [str(r["wallet"]) for r in rows]


def rank_wallets(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    lag_ms: int = COPY_LAG_MS,
    fee_bps: int = ROUND_TRIP_FEE_BPS,
    min_buys: int = 5,
    min_sells: int = 3,
    min_fillable: int = 3,
    since_ms: int | None = None,
    until_ms: int | None = None,
    limit: int = 2_000,
) -> list[WalletCopyReport]:
    """Rank the tape's wallets by what copying them would have paid us, best first.

    ``min_fillable`` is a floor on evidence, not on quality: a wallet we could only have
    copied twice tells us nothing, however those two went.
    """
    c = conn or get_conn()
    out: list[WalletCopyReport] = []
    for wallet in _candidate_wallets(
        c, chain, min_buys=min_buys, min_sells=min_sells,
        since_ms=since_ms, until_ms=until_ms, limit=limit,
    ):
        report = evaluate_wallet(
            c, wallet, chain, lag_ms=lag_ms, fee_bps=fee_bps,
            since_ms=since_ms, until_ms=until_ms,
        )
        if report.trips_fillable >= min_fillable and report.mean_net is not None:
            out.append(report)
    out.sort(key=lambda r: (r.mean_net or Decimal(0)), reverse=True)
    return out


def persistence(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    split_ms: int | None = None,
    top_n: int = 20,
    lag_ms: int = COPY_LAG_MS,
    fee_bps: int = ROUND_TRIP_FEE_BPS,
    min_fillable: int = 3,
    limit: int = 2_000,
) -> dict[str, Any]:
    """**The only question that matters.** Does ranking well early predict doing well later?

    Rank every eligible wallet on the earlier half of the tape, then measure those same
    wallets on the later half, and compare the top ``top_n`` against the population. If
    the top cohort does not beat the rest out of sample, copy trading this signal is
    picking last period's luck and the honest recommendation is not to wire it.

    Returns the numbers and no verdict: the caller reads ``top_later_mean`` against
    ``all_later_mean`` and decides.
    """
    c = conn or get_conn()
    span = fetch_all(c, "SELECT MIN(ts_ms) lo, MAX(ts_ms) hi FROM swaps WHERE chain = ?", (chain.value,))
    lo, hi = (span[0]["lo"], span[0]["hi"]) if span else (None, None)
    if lo is None or hi is None:
        return {"error": "no swaps for this chain"}
    if split_ms is not None:
        cut, cut_basis = int(split_ms), "caller"
    else:
        # Split on the MEDIAN SWAP, not the midpoint of the clock. MEASURED 2026-09-22:
        # the sol tape nominally spans 14 days but 446,788 of 494,400 swaps landed on a
        # single day, because that is when the feeds began running in earnest and the
        # rest is a thin backfill. A clock midpoint put 1,055 swaps in the early half and
        # 493,426 in the later one, so nothing was rankable early and the test silently
        # returned "no data" for every configuration instead of an answer.
        mid = fetch_all(
            c,
            "SELECT ts_ms FROM swaps WHERE chain = ? ORDER BY ts_ms "
            "LIMIT 1 OFFSET (SELECT COUNT(*) / 2 FROM swaps WHERE chain = ?)",
            (chain.value, chain.value),
        )
        if not mid:
            return {"error": "no swaps for this chain"}
        cut, cut_basis = int(mid[0]["ts_ms"]), "median_swap"

    early = rank_wallets(
        c, chain, lag_ms=lag_ms, fee_bps=fee_bps, min_fillable=min_fillable,
        since_ms=int(lo), until_ms=cut, limit=limit,
    )
    chosen = [r.wallet for r in early[:top_n]]

    def later(wallets: list[str]) -> tuple[Decimal | None, int, int]:
        nets: list[Decimal] = []
        trips = 0
        for w in wallets:
            rep = evaluate_wallet(
                c, w, chain, lag_ms=lag_ms, fee_bps=fee_bps, since_ms=cut, until_ms=int(hi)
            )
            if rep.mean_net is not None:
                nets.append(rep.mean_net)
                trips += rep.trips_fillable
        if not nets:
            return None, 0, 0
        return sum(nets) / Decimal(len(nets)), len(nets), trips

    top_mean, top_n_wallets, top_trips = later(chosen)
    all_early = [r.wallet for r in early]
    all_mean, all_n_wallets, all_trips = later(all_early)

    return {
        "chain": chain.value,
        "split_ms": cut,
        "split_basis": cut_basis,
        "early_span_hours": round((cut - int(lo)) / 3_600_000, 2),
        "later_span_hours": round((int(hi) - cut) / 3_600_000, 2),
        "lag_ms": lag_ms,
        "fee_bps": fee_bps,
        "early_ranked_wallets": len(early),
        "early_top_mean_pct": float(early[0].mean_net * 100) if early else None,
        "top_n": top_n,
        "top_later_mean_pct": float(top_mean * 100) if top_mean is not None else None,
        "top_later_wallets_with_data": top_n_wallets,
        "top_later_trips": top_trips,
        "all_later_mean_pct": float(all_mean * 100) if all_mean is not None else None,
        "all_later_wallets_with_data": all_n_wallets,
        "all_later_trips": all_trips,
        "note": (
            "top_later_mean_pct must beat all_later_mean_pct by more than noise before "
            "copy trading this ranking is worth anything. Both are net of fee_bps and "
            "priced at the next print after the wallet's trade plus lag_ms; neither "
            "includes slippage or gas, so both are optimistic."
        ),
    }
