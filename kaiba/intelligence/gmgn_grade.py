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

FIXED 2026-10-03 (journal #4987), two defects that together left the pass useless:

* **Casing.** Every answered address was ``.lower()``-ed. EVM hex is case-insensitive;
  base58 is not. MEASURED on the box: all 5,439 ``sol`` rows under this model key on a
  lowercased string, each has a real-case twin in ``wallets``, and the lowercased keys
  were themselves inserted into ``wallets`` (``source='wallet_scores'``) -- junk keys that
  no swap, cohort or lane can ever join. :func:`normalize` now lowercases EVM only, and an
  answer is keyed back to the address WE asked about (:func:`provider_stats_for`).
* **A failed enrichment was stored.** ``gmgn_cli`` never raises; a refused or throttled
  ``portfolio stats`` came back empty and the wallet was graded without its win rate.
  MEASURED: 628 robinhood rows UNSCORED at evidence weight 29.0 (floor 30), win_rate NULL
  on all 628. :func:`enrich_one` now says whether it answered, and an unanswered wallet is
  not stored -- it is retried, never written down a point short.

Nothing here lowers a threshold. :func:`run_budgeted` is the scheduled entry point
(``ops`` job ``gmgn_grade``): DISCOVERY priority, a call budget, and a cursor in ``kv`` so
a pass that runs out of budget resumes where it stopped.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import connect, fetch_one, get_conn, jload
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, WalletTag, now_ms
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

#: ``kv`` key prefix of the budgeted pass's resume point, per chain.
CURSOR_PREFIX = "gmgn_grade:cursor:"

#: How long a grade-pass read may wait out OUR limiter's minimum interval. gmgn's floor is
#: ~1.2 s; without waiting, the second of two back-to-back reads is refused locally and --
#: before 2026-10-03 -- the wallet was then graded without its win rate.
WAIT_FOR_SLOT_S = 5.0


def normalize(chain: Chain, address: Any) -> str:
    """The key a wallet is stored under: EVM lowercased, base58 exactly as given.

    Mirrors ``SignerPolicy.owned`` and ``wallet_campaign.key``. Lowercasing base58 makes a
    different (almost always nonexistent) address.
    """
    a = str(address or "").strip()
    return a if chain is Chain.SOL else a.lower()


def is_junk_key(chain: Chain, address: str) -> bool:
    """A Solana key that can only have come from the pre-2026-10-03 ``.lower()``.

    Base58 has 24 uppercase letters in 58 symbols; a genuine 32-44 character address with
    none has probability ~(34/58)^32 = 4e-8 at the very shortest. MEASURED: the 5,439 such
    ``sol`` keys in ``wallets`` all came from ``source='wallet_scores'`` and every one has a
    real-case twin. Grading one again would only re-store the junk.
    """
    return chain is Chain.SOL and len(address) >= 32 and address == address.lower()


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


def _requested_key(chain: Chain, answered: str, asked: dict[str, str], folded: dict[str, str | None]) -> str | None:
    """Map the provider's spelling of an address back to the address we asked about.

    Exact match first. A case-only difference is accepted only when it is unambiguous among
    the addresses in this batch -- for EVM that is the normal case; for base58 it would be
    a vendor folding case, and two asked addresses folding together means we cannot tell
    which one it answered for.
    """
    exact = normalize(chain, answered)
    if exact in asked:
        return asked[exact]
    return folded.get(exact.lower())


def provider_stats_for(
    chain: Chain, wallets: list[str], *, period: str = "30d",
    priority: Priority = Priority.RESEARCH, wait_for_slot_s: float = 0.0,
    answered: list[bool] | None = None,
) -> dict[str, ProviderStats]:
    """One batched call. Returns only the wallets the provider actually answered for.

    Keyed by the address AS ASKED (normalised per :func:`normalize`), never by the
    provider's spelling. ``answered``, when given, gets one bool appended: did the call
    itself succeed (an empty answer for unknown wallets is still an answer).
    """
    out: dict[str, ProviderStats] = {}
    if not wallets:
        return out
    asked = {normalize(chain, w): normalize(chain, w) for w in wallets}
    folded: dict[str, str | None] = {}
    for a in asked:
        folded[a.lower()] = None if a.lower() in folded else a
    # portfolio_PROFITS, not portfolio_stats. MEASURED 2026-09-24 on the same 100
    # candidate wallets: stats answered for 1 of 100 (it only covers wallets GMGN already
    # tracks) while profits answered for 100 of 100, 88 of them carrying both a buy and a
    # sell count. profits carries no win_rate or token_num, so those stay None -- the
    # rubric already treats a provider field as a fallback "never at full credit", and an
    # absent component is normalised out rather than counted as zero.
    try:
        res = gmgn_cli.portfolio_profits(list(asked), chain, period=period, priority=priority,
                                         wait_for_slot_s=wait_for_slot_s)
    except Exception as exc:  # noqa: BLE001 - one dead batch must not stop the pass
        log.warning("portfolio_profits raised for %d wallets: %s", len(wallets), type(exc).__name__)
        if answered is not None:
            answered.append(False)
        return out
    if answered is not None:
        answered.append(bool(getattr(res, "ok", res is not None and getattr(res, "data", None) is not None)))
    rows = getattr(res, "data", None)
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        addr = _requested_key(chain, str(row.get("wallet_address") or ""), asked, folded)
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
    """Per-wallet ``portfolio_stats``; see :func:`enrich_checked`. Kept for old callers."""
    return enrich_checked(chain, address, base)[0]


def enrich_checked(
    chain: Chain, address: str, base: ProviderStats, *,
    priority: Priority = Priority.RESEARCH, wait_for_slot_s: float = 0.0,
) -> tuple[ProviderStats, bool]:
    """Per-wallet ``portfolio_stats`` for the fields the batched call cannot carry.

    Returns ``(stats, answered)``. ``answered`` is False when the call itself failed --
    refused by our limiter, throttled, timed out -- which ``gmgn_cli`` reports as empty
    data rather than raising. A caller must not store a grade built on an unanswered
    enrichment: that is how 628 robinhood wallets were written down at evidence weight
    29.0 against a floor of 30, permanently, for want of one win rate.

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
        res = gmgn_cli.portfolio_stats(address, chain, period="all", priority=priority,
                                       wait_for_slot_s=wait_for_slot_s)
    except Exception as exc:  # noqa: BLE001 - one dead lookup must not stop the pass
        log.debug("portfolio_stats raised for %s: %s", address[:10], type(exc).__name__)
        return base, False
    row = getattr(res, "data", None)
    answered = bool(getattr(res, "ok", row is not None))
    if isinstance(row, list):
        row = row[0] if row else None
    if not isinstance(row, dict):
        # Answered with nothing (an empty list) is an answer; a failed call is not.
        return base, answered
    pnl = row.get("pnl_stat") if isinstance(row.get("pnl_stat"), dict) else {}
    win = pnl.get("winrate")
    return base.model_copy(update={
        "win_rate": float(win) if isinstance(win, (int, float)) else base.win_rate,
        "token_num": _int(pnl.get("token_num")) or base.token_num,
        "avg_hold_s": _int(pnl.get("avg_holding_period")) or base.avg_hold_s,
    }), answered


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
    out = [a for r in rows if (a := normalize(chain, r[0])) not in ours and not is_junk_key(chain, a)]
    return out[: max(0, int(limit))] if limit is not None else out


def _score(chain: Chain, addr: str, ps: ProviderStats, tags: list[str]) -> Any:
    score = score_wallet(WalletEvidence(address=addr, chain=chain, tags=_grader_tags(tags), provider_stats=ps))
    # MUST override: score_wallet stamps MODEL_ID when there is no tape, and a provider
    # grade wearing the full-history label is the confusion this module exists to avoid.
    return score.model_copy(update={"model_version": MODEL_ID_PROVIDER})


def _drop_ours(conn: sqlite3.Connection, chain: Chain, scores: list[Any]) -> list[Any]:
    """Re-check, right before the write, that no grade of OUR OWN landed meanwhile.

    ``store_scores(protect_full=True)`` already refuses to replace a paid ``MODEL_ID``
    grade at write time; it does not protect a tape grade, and neither may be overwritten.
    """
    if not scores:
        return scores
    marks = ",".join("?" for _ in scores)
    taken = {
        str(r[0]) for r in conn.execute(
            f"SELECT address FROM wallet_scores WHERE chain = ? AND model_version IN (?, ?) "
            f"AND address IN ({marks})",
            (chain.value, MODEL_ID, MODEL_ID_TAPE, *[s.address for s in scores]),
        )
    }
    return [s for s in scores if s.address not in taken]


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
        "too_thin": 0, "enrich_unanswered": 0, "dry_run": bool(dry_run),
    }
    if not todo:
        report["elapsed_s"] = time.perf_counter() - started
        return report

    tags = {
        normalize(chain, r[0]): (jload(r[1], []) or [])
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
                    ps, ok = enrich_checked(chain, addr, ps)
                    report["enriched"] += 1
                    if not ok:
                        # Not stored: a grade one win rate short is a grade written down wrong.
                        report["enrich_unanswered"] += 1
                        continue
                score = _score(chain, addr, ps, tags.get(addr, []))
                report["scored"] += 1
                g = score.grade.value
                report["by_grade"][g] = report["by_grade"].get(g, 0) + 1
                if g in ("A", "B"):
                    report["b_or_better"] += 1
                if writer is not None:
                    pending.append(score)
                    if len(pending) >= 500:
                        ok, bad = store_scores(_drop_ours(writer, chain, pending), writer, protect_full=True)
                        report["stored"] += ok
                        report["failed"] += bad
                        pending.clear()
        if writer is not None and pending:
            ok, bad = store_scores(_drop_ours(writer, chain, pending), writer, protect_full=True)
            report["stored"] += ok
            report["failed"] += bad
    finally:
        if writer is not None:
            writer.close()
    report["elapsed_s"] = time.perf_counter() - started
    return report


# --------------------------------------------------------------------------------------
# the scheduled pass: a candidate pool, a call budget, and a cursor
# --------------------------------------------------------------------------------------


def pool(conn: sqlite3.Connection, chain: Chain) -> list[str]:
    """Who the scheduled pass re-grades, in the order it visits them. Read-only.

    The pool the 2026-10-02 research re-grade drew on (journal #4987; its export lists the
    sources as ``prevB`` and ``feed:smartmoney|tag:...``): every wallet that already holds a
    GMGN provider grade, plus every registry wallet carrying a GMGN vendor tag
    (``gmgn:...`` in ``tags_json``) that holds no grade at all. Never: a wallet with a grade
    of OUR OWN (``MODEL_ID``, ``MODEL_ID_TAPE`` -- this module never overwrites those), a
    ``blacklist`` cohort member, or a lowercased Solana junk key (:func:`is_junk_key`).

    Order: first the provider grades that are UNSCORED with no win rate (the failed
    enrichments), then everything else; by address inside each, so the cursor is stable.
    """
    return [k.split("|", 1)[1] for k in pool_keys(conn, chain)]


def pool_keys(conn: sqlite3.Connection, chain: Chain) -> list[str]:
    """:func:`pool` as sorted ``"<tier>|<address>"`` keys -- the cursor's own order.

    Memory-light on purpose (the ops process has been OOM-killed): only provider rows and
    vendor-tagged registry rows are materialised, never a chain's whole ``wallet_scores``.
    """
    provider: dict[str, int] = {}
    for r in conn.execute(
        "SELECT address, grade, win_rate FROM wallet_scores WHERE chain = ? AND model_version = ?",
        (chain.value, MODEL_ID_PROVIDER),
    ):
        provider[str(r[0])] = 0 if (r[1] == "UNSCORED" and r[2] is None) else 1
    # A tagged wallet joins the pool only while it holds no grade at all. One statement, not
    # a Python round trip per address: MEASURED on the box, ~2.6k robinhood lookups made
    # from Python took ~2 min under idle I/O against ~6 s for each whole-chain scan.
    fresh = [str(r[0]) for r in conn.execute(
        "SELECT w.address FROM wallets w WHERE w.chain = ? AND w.tags_json LIKE '%gmgn:%' "
        "AND COALESCE(w.cohort,'') != 'blacklist' AND NOT EXISTS "
        "(SELECT 1 FROM wallet_scores ws WHERE ws.chain = w.chain AND ws.address = w.address)",
        (chain.value,))]
    blacklisted = {str(r[0]) for r in conn.execute(
        "SELECT address FROM wallets WHERE chain = ? AND cohort = 'blacklist'", (chain.value,))}
    keys = {f"{t}|{a}" for a, t in provider.items()} | {f"1|{a}" for a in fresh}
    if chain is Chain.SOL:
        # The real-case twins of the lowercased junk grades: the wallets the 2026-09-24 pass
        # meant to grade. Never one already holding a grade of our own.
        keys |= {f"1|{r[0]}" for r in conn.execute(
            "SELECT w.address FROM wallets w WHERE w.chain = 'sol' AND w.address != lower(w.address) "
            "AND lower(w.address) IN (SELECT address FROM wallet_scores WHERE chain = 'sol' "
            "  AND model_version = ? AND address = lower(address)) "
            "AND NOT EXISTS (SELECT 1 FROM wallet_scores ws WHERE ws.chain = 'sol' AND ws.address = w.address "
            "  AND ws.model_version IN (?, ?))",
            (MODEL_ID_PROVIDER, MODEL_ID, MODEL_ID_TAPE))}
    return sorted(
        k for k in keys
        if (a := k.split("|", 1)[1]) == normalize(chain, a) and a not in blacklisted and not is_junk_key(chain, a)
    )


#: ``kv`` key prefix of the cached pool, per chain. Building it is three whole-chain scans.
POOL_PREFIX = "gmgn_grade:pool:"
#: How long a built pool is reused. A tier that moves within the day only reorders.
POOL_MAX_AGE_MS = 86_400_000


def cached_pool_keys(conn: sqlite3.Connection, chain: Chain, *, max_age_ms: int = POOL_MAX_AGE_MS,
                     store: bool = True, now: int | None = None) -> tuple[list[str], bool]:
    """``(keys, rebuilt)``: :func:`pool_keys`, rebuilt at most once per ``max_age_ms``."""
    from kaiba.core.db import jdump

    ts = now if now is not None else now_ms()
    row = fetch_one(conn, "SELECT value, updated_ms FROM kv WHERE key=?", (POOL_PREFIX + chain.value,))
    if row is not None and ts - int(row["updated_ms"] or 0) < max_age_ms:
        keys = jload(row["value"], None)
        if isinstance(keys, list):
            return [str(k) for k in keys], False
    keys = pool_keys(conn, chain)
    if store:
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (POOL_PREFIX + chain.value, jdump(keys), ts),
        )
    return keys, True


def load_cursor(conn: sqlite3.Connection, chain: Chain) -> str:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (CURSOR_PREFIX + chain.value,))
    return str(jload(row["value"], "") or "") if row else ""


def save_cursor(conn: sqlite3.Connection, chain: Chain, value: str) -> None:
    from kaiba.core.db import jdump

    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (CURSOR_PREFIX + chain.value, jdump(value), now_ms()),
    )


def run_budgeted(
    conn: sqlite3.Connection,
    chain: Chain,
    *,
    max_calls: int,
    period: str = "all",
    priority: Priority = Priority.DISCOVERY,
    wait_for_slot_s: float = WAIT_FOR_SLOT_S,
    deadline_monotonic: float | None = None,
    store: bool = True,
    pace_s: float = 0.0,
    pool_max_age_ms: int = POOL_MAX_AGE_MS,
) -> dict[str, Any]:
    """Re-grade the next part of :func:`pool`, spending at most ``max_calls`` GMGN reads.

    Resumable: the cursor (``kv`` ``gmgn_grade:cursor:<chain>``, ``"<tier>|<address>"``)
    moves past a wallet only once that wallet's answer is final -- graded and stored, too
    thin, or not answered for by the batched call. A wallet whose enrichment went
    unanswered stops the pass WITHOUT moving past it, so the next run starts there. The
    end of the pool wraps to the start (``wrapped``), so every member is revisited.

    Calls: one ``portfolio profits`` per batch of up to :data:`BATCH`, plus one
    ``portfolio stats`` per wallet that shows a buy AND a sell. The batch call is skipped
    when there is no budget left for at least one enrichment behind it.

    ``pace_s`` spaces the calls. The gmgn bucket is SHARED with swaps (weight 10) and an
    exit may overdraw it by only 2, so a grade pass that drained it could hold a stop-loss
    sell back. On the box (capacity 60, refill 3/s) a 2 s pace spends ~13% of the refill
    and holds at most one of the four inflight slots.
    """
    last_call = [0.0]

    def pace() -> None:
        if pace_s > 0 and last_call[0]:
            gap = pace_s - (time.monotonic() - last_call[0])
            if gap > 0:
                time.sleep(gap)
        last_call[0] = time.monotonic()

    started = time.perf_counter()
    report: dict[str, Any] = {
        "chain": chain.value, "pool": 0, "visited": 0, "calls": 0, "answered": 0, "scored": 0,
        "stored": 0, "failed": 0, "too_thin": 0, "unanswered": 0, "by_grade": {}, "b_or_better": 0,
        "stopped": None, "wrapped": False, "cursor_from": "", "cursor_to": "",
    }
    keys, report["pool_rebuilt"] = cached_pool_keys(conn, chain, max_age_ms=pool_max_age_ms, store=store)
    report["pool"] = len(keys)
    if not keys or max_calls < 2:
        report["stopped"] = "empty_pool" if not keys else "no_budget"
        report["elapsed_s"] = round(time.perf_counter() - started, 2)
        return report
    cursor = load_cursor(conn, chain)
    report["cursor_from"] = cursor
    ahead = [k for k in keys if k > cursor] if cursor else list(keys)
    if not ahead:
        ahead, report["wrapped"] = list(keys), True
    tiers = {k.split("|", 1)[1]: k for k in ahead}
    todo = list(tiers)
    tags = {}
    for i in range(0, len(todo), 500):
        chunk = todo[i : i + 500]
        marks = ",".join("?" for _ in chunk)
        for r in conn.execute(f"SELECT address, tags_json FROM wallets WHERE chain = ? AND address IN ({marks})",
                              (chain.value, *chunk)):
            tags[str(r[0])] = jload(r[1], []) or []

    def out_of_time() -> bool:
        return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic

    done = cursor
    for i in range(0, len(todo), BATCH):
        if report["calls"] + 2 > max_calls:
            report["stopped"] = "budget"
            break
        if out_of_time():
            report["stopped"] = "deadline"
            break
        chunk = todo[i : i + BATCH]
        answered: list[bool] = []
        pace()
        stats = provider_stats_for(chain, chunk, period=period, priority=priority,
                                   wait_for_slot_s=wait_for_slot_s, answered=answered)
        report["calls"] += 1
        if not (answered and answered[0]):
            report["stopped"] = "provider_unavailable"
            break
        report["answered"] += len(stats)
        pending: list[Any] = []
        halted = False
        for addr in chunk:
            ps = stats.get(addr)
            if ps is not None and ps.buy_count and ps.sell_count:
                if report["calls"] + 1 > max_calls:
                    report["stopped"] = "budget"
                    halted = True
                    break
                if out_of_time():
                    report["stopped"] = "deadline"
                    halted = True
                    break
                pace()
                ps, ok = enrich_checked(chain, addr, ps, priority=priority, wait_for_slot_s=wait_for_slot_s)
                report["calls"] += 1
                if not ok:
                    report["unanswered"] += 1
                    report["stopped"] = "enrich_unanswered"
                    halted = True
                    break
                score = _score(chain, addr, ps, tags.get(addr, []))
                report["scored"] += 1
                g = score.grade.value
                report["by_grade"][g] = report["by_grade"].get(g, 0) + 1
                report["b_or_better"] += int(g in ("A", "B"))
                pending.append(score)
            elif ps is not None:
                report["too_thin"] += 1
            done = tiers[addr]
            report["visited"] += 1
        if store and pending:
            ok_n, bad = store_scores(_drop_ours(conn, chain, pending), conn, protect_full=True)
            report["stored"] += ok_n
            report["failed"] += bad
        if store:
            save_cursor(conn, chain, done)
        if halted:
            break
    report["cursor_to"] = done
    report["elapsed_s"] = round(time.perf_counter() - started, 2)
    return report


__all__ = ["BATCH", "CURSOR_PREFIX", "EVM_CHAINS", "MODEL_ID_PROVIDER", "POOL_PREFIX", "cached_pool_keys",
           "candidates", "enrich_checked", "is_junk_key", "normalize", "pool", "pool_keys", "provider_stats_for",
           "run", "run_budgeted"]
