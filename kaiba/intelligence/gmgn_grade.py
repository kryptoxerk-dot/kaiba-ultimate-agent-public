"""Grade EVM wallets from GMGN's measured PnL, under a model version of its own.

WHY THIS EXISTS. Until now a wallet could only be graded from evidence we had gathered
ourselves, and on bsc we had gathered almost none: 833 wallets ever observed trading
against 13,991 in the registry. The two routes that could have fixed that are shut --
Helius is Solana-only, and Etherscan V2 refuses bsc outright ("Free API access is not
supported for this chain. Please upgrade your api plan"), though it does serve robinhood
and eth.

MEASURED 2026-09-24: ``gmgn_cli.portfolio_stats`` takes up to 100 ARBITRARY wallet
addresses and answered for 100 of 100 bsc wallets in 1.7 s. It is not credential-gated to
our own wallet the way ``portfolio holdings`` is. 48 of those 100 carried at least three
buys and three sells, which is the round-trip activity the B gate counts.

WHAT IT IS, AND WHAT IT IS NOT.

``grade.py``'s own docstring records that the rubric "swaps provider PnL for our own
reconstruction". This does not undo that. It reuses the EXISTING seam the rubric already
has for exactly this case -- ``WalletEvidence.provider_stats``, whose docstring says
"What a provider claims. Used only as a fallback, and never at full credit." The rubric
already discounts these numbers; this module only fills the slot.

It is a MEASUREMENT, not a vendor label, so it is not the failure the ``gmgn:`` tag
namespace exists to prevent (a label entering the money path as a grade). But it is still
somebody else's arithmetic over somebody else's definition of a round trip, so:

* It lands under :data:`MODEL_ID_PROVIDER`, distinct from ``kaiba-wallet-v1`` (our full
  reconstruction) and ``kaiba-wallet-tape-v1`` (our own partial tape). Without this
  override ``score_wallet`` would stamp it ``kaiba-wallet-v1``, because that is what it
  uses when there is no tape -- and a provider grade wearing the full-history label is
  precisely the confusion that must not happen.
* It NEVER overwrites a grade built on evidence we gathered. A wallet already carrying
  our own verdict keeps it.

See also the reason not to trust a vendor's own GRADE: a supplied list of 434 externally
A/B-rated wallets produced 2 B under our rubric, because 73 of 157 were "Position Holders"
who have not closed anything. Their PnL is real; it answers a different question. What
this module takes from GMGN is the raw buy/sell/profit arithmetic, and lets OUR rubric
decide what it means.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import connect, get_conn, jload
from kaiba.core.schemas import Chain
from kaiba.core.schemas import WalletTag
from kaiba.intelligence.grade import (
    MODEL_ID,
    MODEL_ID_TAPE,
    ProviderStats,
    WalletEvidence,
    score_wallet,
    store_scores,
)
from kaiba.providers import gmgn_cli

log = logging.getLogger(__name__)

#: The model version a provider-sourced grade carries. Deliberately not MODEL_ID: that
#: one means "our own full reconstruction" and readers act on the difference.
MODEL_ID_PROVIDER = "kaiba-wallet-gmgn-v1"

#: GMGN accepts 1-100 wallets per call and answered 100/100 in 1.7 s.
BATCH = 100

#: Grades we refuse to overwrite, because they rest on evidence WE gathered.
OUR_OWN_MODELS = frozenset({MODEL_ID, MODEL_ID_TAPE})

#: Chains this is for. sol has Helius and needs no provider fallback.
EVM_CHAINS = (Chain.BSC, Chain.ROBINHOOD)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _grader_tags(raw: Any) -> list[str]:
    """Only tags the rubric's own vocabulary accepts. Vendor-namespaced ones are DROPPED.

    ``WalletEvidence.tags`` is an enum, and it refuses `gmgn:smart_degen` by design: the
    namespace exists so a vendor label can never enter the money path as a grade (1,741
    wallets once carried one, 18 of them also wash_trader or sandwich_bot). Dropping them
    here keeps that guarantee -- this module takes GMGN's ARITHMETIC and none of its
    opinions. Stripping the prefix to make them fit would defeat the whole mechanism.
    """
    out: list[str] = []
    for tag in raw or []:
        name = str(tag)
        if ":" in name:
            continue
        try:
            out.append(WalletTag(name).value)
        except ValueError:
            continue
    return out


def provider_stats_for(
    chain: Chain, wallets: list[str], *, period: str = "30d"
) -> dict[str, ProviderStats]:
    """One batched call. Returns only the wallets the provider actually answered for."""
    out: dict[str, ProviderStats] = {}
    if not wallets:
        return out
    # portfolio_PROFITS, not portfolio_stats. MEASURED 2026-09-24 on the same 100
    # candidate wallets: stats answered for 1 of 100 (it only covers wallets GMGN already
    # tracks) while profits answered for 100 of 100, 88 of them carrying both a buy and a
    # sell count. profits carries no win_rate or token_num, so those stay None -- the
    # rubric already treats a provider field as a fallback "never at full credit", and an
    # absent component is normalised out rather than counted as zero.
    try:
        res = gmgn_cli.portfolio_profits(wallets, chain, period=period)
    except Exception as exc:  # noqa: BLE001 - one dead batch must not stop the pass
        log.warning("portfolio_profits raised for %d wallets: %s", len(wallets), type(exc).__name__)
        return out
    rows = getattr(res, "data", None)
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        addr = str(row.get("wallet_address") or "").strip().lower()
        if not addr:
            continue
        # pnl_stat only rides on portfolio_stats, which barely answers; profits gives the
        # arithmetic and nothing else. Prefer the lifetime figure where present.
        pnl = row.get("pnl_stat") if isinstance(row.get("pnl_stat"), dict) else {}
        realized = _dec(row.get("total_realized_profit"))
        if realized is None:
            realized = _dec(row.get("realized_profit"))
        win = pnl.get("winrate")
        out[addr] = ProviderStats(
            realized_profit_usd=realized,
            win_rate=float(win) if isinstance(win, (int, float)) else None,
            token_num=_int(pnl.get("token_num")),
            avg_hold_s=_int(pnl.get("avg_holding_peroid") or pnl.get("avg_hold_s")),
            buy_count=_int(row.get("buy")),
            sell_count=_int(row.get("sell")),
        )
    return out


def enrich_one(chain: Chain, address: str, base: ProviderStats) -> ProviderStats:
    """Per-wallet ``portfolio_stats`` for the fields the batched call cannot carry.

    MEASURED 2026-09-24: ``portfolio_stats`` answers for ONE wallet per call regardless of
    how many are passed -- a batch of 100 returned a single row, which is why the first
    version of this module scored 300 wallets and graded none. Called singly it returns
    ``pnl_stat`` with winrate, token_num, avg_holding_period and the big-win buckets.

    That one field is the difference between grading and not. `_win_rate` explicitly
    accepts ``provider_stats.win_rate`` as a fallback ("basis=provider_reported"), and
    without it the evidence weight lands at 29.0 against a floor of 30 -- the richest bsc
    wallet in a 100-sample, 9,411 buys and 11,128 sells, was refused by one point.

    Never raises: an enrichment we could not fetch leaves the batched stats untouched.
    """
    try:
        res = gmgn_cli.portfolio_stats(address, chain, period="all")
    except Exception as exc:  # noqa: BLE001 - one dead lookup must not stop the pass
        log.debug("portfolio_stats raised for %s: %s", address[:10], type(exc).__name__)
        return base
    row = getattr(res, "data", None)
    if isinstance(row, list):
        row = row[0] if row else None
    if not isinstance(row, dict):
        return base
    pnl = row.get("pnl_stat") if isinstance(row.get("pnl_stat"), dict) else {}
    win = pnl.get("winrate")
    return base.model_copy(update={
        "win_rate": float(win) if isinstance(win, (int, float)) else base.win_rate,
        "token_num": _int(pnl.get("token_num")) or base.token_num,
        "avg_hold_s": _int(pnl.get("avg_holding_period")) or base.avg_hold_s,
    })


def candidates(
    conn: sqlite3.Connection, chain: Chain, *, limit: int | None = None
) -> list[str]:
    """Registry wallets on ``chain`` that do not already hold a grade of our own."""
    ours = {
        str(r[0]) for r in conn.execute(
            "SELECT address FROM wallet_scores WHERE chain = ? AND model_version IN (?, ?)",
            (chain.value, MODEL_ID, MODEL_ID_TAPE),
        )
    }
    rows = conn.execute(
        "SELECT address FROM wallets WHERE chain = ? AND COALESCE(cohort,'') != 'blacklist'",
        (chain.value,),
    )
    out = [str(r[0]) for r in rows if str(r[0]) not in ours]
    return out[: max(0, int(limit))] if limit is not None else out


def run(
    chain: Chain,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    period: str = "all",
    store: bool = True,
    dry_run: bool = False,
    enrich: bool = True,
) -> dict[str, Any]:
    """Grade every ungraded registry wallet on ``chain`` from GMGN's numbers."""
    c = conn or get_conn()
    started = time.perf_counter()
    todo = candidates(c, chain, limit=limit)
    report: dict[str, Any] = {
        "chain": chain.value, "candidates": len(todo), "answered": 0,
        "scored": 0, "stored": 0, "failed": 0, "by_grade": {}, "b_or_better": 0, "enriched": 0,
        "too_thin": 0, "dry_run": bool(dry_run),
    }
    if not todo:
        report["elapsed_s"] = time.perf_counter() - started
        return report

    tags = {
        str(r[0]): (jload(r[1], []) or [])
        for r in c.execute("SELECT address, tags_json FROM wallets WHERE chain = ?", (chain.value,))
    }
    # Its own connection: the candidate scan above may still be streaming, and a write
    # through the connection being read loses the lock. See grade.TAPE_STORE_BATCH.
    writer = connect() if (store and not dry_run) else None
    pending = []
    try:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i : i + BATCH]
            stats = provider_stats_for(chain, chunk, period=period)
            report["answered"] += len(stats)
            for addr, ps in stats.items():
                # No round trips, nothing our rubric can weigh. Counted, not scored.
                if not (ps.buy_count and ps.sell_count):
                    report["too_thin"] += 1
                    continue
                # Two stages on purpose: the batched call is the cheap triage (100 a call,
                # 88% carry buy AND sell) and this per-wallet call is spent only on the
                # ones that survived it.
                if enrich:
                    ps = enrich_one(chain, addr, ps)
                    report["enriched"] += 1
                ev = WalletEvidence(
                    address=addr, chain=chain,
                    tags=_grader_tags(tags.get(addr, [])),
                    provider_stats=ps,
                )
                score = score_wallet(ev)
                # MUST override: score_wallet stamps MODEL_ID when there is no tape, and a
                # provider grade wearing the full-history label is the confusion this
                # module exists to avoid.
                score = score.model_copy(update={"model_version": MODEL_ID_PROVIDER})
                report["scored"] += 1
                g = score.grade.value
                report["by_grade"][g] = report["by_grade"].get(g, 0) + 1
                if g in ("A", "B"):
                    report["b_or_better"] += 1
                if writer is not None:
                    pending.append(score)
                    if len(pending) >= 500:
                        ok, bad = store_scores(pending, writer)
                        report["stored"] += ok
                        report["failed"] += bad
                        pending.clear()
        if writer is not None and pending:
            ok, bad = store_scores(pending, writer)
            report["stored"] += ok
            report["failed"] += bad
    finally:
        if writer is not None:
            writer.close()
    report["elapsed_s"] = time.perf_counter() - started
    return report


__all__ = ["BATCH", "EVM_CHAINS", "MODEL_ID_PROVIDER", "candidates", "provider_stats_for", "run"]
