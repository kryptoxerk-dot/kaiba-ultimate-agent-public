"""Paper EV by confluence level -- and the rule a size increase has to clear first.

Entry size is FLAT by owner directive (2026-09-24) because every confidence measure we
have tried -- conviction score, GMGN smart-wallet count, dossier grade -- measured
ANTI-calibrated on live fills. "More proven wallets agree, so bet more" is the same claim
a fourth time. This module is how it gets tested instead of assumed: it reads the paper
trades ``confluence-5`` opens when it counts PROVEN wallets (``kaiba.learning.proven``),
groups them by the confluence level recorded on each signal, and reports EV per level
with an interval. It never changes a size. Nothing reads its verdict automatically.

The pre-registered rule (:data:`RULE`, registered 2026-10-02, before a single proven
signal existed -- so no data informed it):

    A size increase for confluence level >= k on chain C may be PROPOSED to the owner only
    when, over closed paper trades of confluence-5 / wallet_source=proven on C, opened
    after the rule was registered, at level >= k:
      1. n >= 30;
      2. mean return, after a 5-percentage-point paper-to-live haircut, > 0;
      3. the one-sided 95% bootstrap lower bound of that haircut mean > 0;
      4. both chronological halves (split at the median open time) have a positive
         haircut mean -- the later half is out of sample for the earlier one;
      5. level >= k beats level < k on the same chain (else the level adds nothing over
         simply taking the lane's signals, and the proposal is "keep it flat").
    Anything less is NOT_YET (too few trades) or REJECT (enough trades, rule failed).
    Each chain is judged alone; a pooled line is reported and never decides.

Why each number: 30 is the usual floor below which a bootstrap of fat-tailed returns is
unstable; the 5 pp haircut is INVENTED -- an allowance for what paper fills omit (MEV,
failed sells, the live/paper gap this book has already shown) -- and is applied BEFORE
the interval, not after. ``k`` is cumulative (``>= k``) so a level with few trades of its
own still contributes to every threshold below it.

A second table, :func:`signal_forward`, prices EVERY proven confluence signal from the
tape at fixed horizons, including the ones the risk gate skipped. Paper entries are
gated by live-money state (daily loss stop, total exposure, chain enabled -- MEASURED
2026-10-02: pons-robinhood shadow entries refused ``daily_loss_stop`` 6 times in 24 h), so
the paper sample is not every signal. The forward table says whether that selection
matters. It is reported beside the rule and is not part of it.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jload
from kaiba.core.schemas import Chain, Lane, digest
from kaiba.learning import fabricated
from kaiba.learning.copytrade import next_print
from kaiba.learning.metrics import trade_return

__all__ = [
    "RULE",
    "RULE_REGISTERED_MS",
    "LevelStat",
    "PaperTrade",
    "SizeRule",
    "collect_paper_trades",
    "level_table",
    "report",
    "signal_forward",
]


@dataclass(frozen=True)
class SizeRule:
    """The pre-registered rule. Changing a field changes :func:`rule_digest`, on purpose."""

    lane: str = Lane.CONFLUENCE_5.value
    wallet_source: str = "proven"
    min_trades: int = 30
    alpha: float = 0.05
    live_haircut: Decimal = Decimal("0.05")
    require_both_halves_positive: bool = True
    require_beats_lower_levels: bool = True
    levels: tuple[int, ...] = (2, 3, 4, 5)
    bootstrap_draws: int = 2000
    seed: int = 20_261_002

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "wallet_source": self.wallet_source,
            "min_trades": self.min_trades,
            "alpha": self.alpha,
            "live_haircut": str(self.live_haircut),
            "require_both_halves_positive": self.require_both_halves_positive,
            "require_beats_lower_levels": self.require_beats_lower_levels,
            "levels": list(self.levels),
            "bootstrap_draws": self.bootstrap_draws,
            "seed": self.seed,
        }


RULE = SizeRule()
#: 2026-10-02 00:00 UTC. Trades opened before this cannot count toward the rule.
RULE_REGISTERED_MS = int(datetime(2026, 10, 2, tzinfo=UTC).timestamp() * 1000)


def rule_digest(rule: SizeRule = RULE) -> str:
    """A fingerprint of the rule, printed on every report so an edit cannot hide."""
    return digest(rule.as_dict())[:16]


@dataclass(frozen=True)
class PaperTrade:
    trade_id: str
    chain: str
    token: str
    opened_ms: int
    closed_ms: int
    level: int
    ret: Decimal
    exit_reason: str | None
    cohort_id: str | None


def _signal_payload(conn: sqlite3.Connection, signals_json: str | None) -> dict[str, Any]:
    ids = jload(signals_json, []) or []
    if not ids:
        return {}
    row = fetch_one(conn, "SELECT payload_json FROM signals WHERE signal_id=?", (str(ids[0]),))
    return (jload(row["payload_json"], {}) or {}) if row else {}


def collect_paper_trades(
    conn: sqlite3.Connection,
    *,
    rule: SizeRule = RULE,
    since_ms: int = RULE_REGISTERED_MS,
    until_ms: int | None = None,
) -> tuple[list[PaperTrade], dict[str, int]]:
    """Closed SHADOW trades of the rule's lane whose signal counted ``wallet_source``.

    Returns ``(trades, skipped)``; ``skipped`` names every reason a trade was left out,
    so a sample that shrank says why. A trade with no recorded cost or no level is
    skipped, never scored as 0.
    """
    sql = (
        "SELECT t.trade_id, t.chain, t.token, t.opened_ms, t.closed_ms, t.cost_native, "
        "t.pnl_native, t.pnl_pct, t.exit_reason, d.signals_json "
        "FROM trades t LEFT JOIN decisions d ON d.decision_id = t.decision_id "
        "WHERE t.lane = ? AND t.mode = 'shadow' AND t.opened_ms >= ?"
    )
    args: list[Any] = [rule.lane, int(since_ms)]
    if until_ms is not None:
        sql += " AND t.closed_ms <= ?"
        args.append(int(until_ms))
    out: list[PaperTrade] = []
    skipped: dict[str, int] = {}

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for row in fetch_all(conn, sql + " ORDER BY t.opened_ms", tuple(args)):
        if fabricated.is_fabricated_outcome(row.get("exit_reason"), "shadow"):
            skip("fabricated_outcome")
            continue
        payload = _signal_payload(conn, row.get("signals_json"))
        if not payload:
            skip("no_signal_payload")
            continue
        if str(payload.get("wallet_source") or "grade") != rule.wallet_source:
            skip(f"wallet_source_{payload.get('wallet_source') or 'grade'}")
            continue
        level = payload.get("confluence_level", payload.get("entity_count"))
        if level is None:
            skip("no_level")
            continue
        ret = trade_return(row)
        if ret is None:
            skip("no_return")
            continue
        out.append(PaperTrade(
            trade_id=str(row["trade_id"]), chain=str(row["chain"]), token=str(row["token"]),
            opened_ms=int(row["opened_ms"]), closed_ms=int(row["closed_ms"]), level=int(level),
            ret=ret, exit_reason=row.get("exit_reason"), cohort_id=payload.get("cohort_id"),
        ))
    # A paper position written off with a booked result (kaiba.learning.fabricated) has no
    # trades row, so the loop above never sees it. Counted lane-wide: with no trade there is
    # no decision, so no signal to read a wallet_source or a level from.
    no_trade = (
        "SELECT COUNT(*) AS n FROM positions p WHERE p.lane = ? AND p.mode = 'shadow' "
        f"AND p.closed_ms IS NOT NULL AND p.opened_ms >= ? AND {fabricated.sql_predicate('p')} "
        "AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.position_id = p.position_id)"
    )
    no_trade_args: list[Any] = [rule.lane, int(since_ms)]
    if until_ms is not None:
        no_trade += " AND p.closed_ms <= ?"
        no_trade_args.append(int(until_ms))
    unbooked = fetch_one(conn, no_trade, tuple(no_trade_args))
    if unbooked and int(unbooked["n"] or 0):
        skipped["fabricated_outcome"] = skipped.get("fabricated_outcome", 0) + int(unbooked["n"])
    return out, skipped


# --------------------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------------------


def _boot(values: Sequence[float], *, draws: int, seed: int) -> list[float]:
    n = len(values)
    rng = random.Random(seed)
    return sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(max(draws, 100)))


@dataclass
class LevelStat:
    chain: str
    level: int
    n: int = 0
    mean_pct: float | None = None
    median_pct: float | None = None
    win_rate: float | None = None
    haircut_mean_pct: float | None = None
    ci95_pct: tuple[float, float] | None = None
    lower95_pct: float | None = None
    first_half_pct: float | None = None
    second_half_pct: float | None = None
    below_level_mean_pct: float | None = None
    verdict: str = "NOT_YET"
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100.0, 3)


def _stat(chain: str, level: int, trades: Sequence[PaperTrade], below: Sequence[PaperTrade],
          rule: SizeRule) -> LevelStat:
    st = LevelStat(chain=chain, level=level, n=len(trades))
    if below:
        st.below_level_mean_pct = _pct(sum(float(t.ret) for t in below) / len(below))
    if not trades:
        st.reasons.append("no trades at this level")
        return st
    raw = [float(t.ret) for t in trades]
    cut = [r - float(rule.live_haircut) for r in raw]
    s = sorted(raw)
    st.mean_pct = _pct(sum(raw) / len(raw))
    st.median_pct = _pct(s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2)
    st.win_rate = round(sum(1 for r in raw if r > 0) / len(raw), 4)
    st.haircut_mean_pct = _pct(sum(cut) / len(cut))
    ordered = sorted(trades, key=lambda t: t.opened_ms)
    half = len(ordered) // 2
    if half:
        first = [float(t.ret) - float(rule.live_haircut) for t in ordered[:half]]
        second = [float(t.ret) - float(rule.live_haircut) for t in ordered[half:]]
        st.first_half_pct = _pct(sum(first) / len(first))
        st.second_half_pct = _pct(sum(second) / len(second))
    if len(cut) >= 3:
        means = _boot(cut, draws=rule.bootstrap_draws, seed=rule.seed + level)
        st.ci95_pct = (_pct(means[int(0.025 * len(means))]), _pct(means[int(0.975 * len(means)) - 1]))
        st.lower95_pct = _pct(means[int(rule.alpha * len(means))])

    if st.n < rule.min_trades:
        st.verdict = "NOT_YET"
        st.reasons.append(f"{st.n} closed paper trades < {rule.min_trades}")
        return st
    failures: list[str] = []
    if not (st.haircut_mean_pct is not None and st.haircut_mean_pct > 0):
        failures.append(f"mean after {rule.live_haircut} haircut is {st.haircut_mean_pct}%, not > 0")
    if not (st.lower95_pct is not None and st.lower95_pct > 0):
        failures.append(f"one-sided {1 - rule.alpha:.0%} lower bound {st.lower95_pct}% is not > 0")
    if rule.require_both_halves_positive and not (
        (st.first_half_pct or 0) > 0 and (st.second_half_pct or 0) > 0
    ):
        failures.append(f"halves {st.first_half_pct}% / {st.second_half_pct}% are not both > 0")
    if rule.require_beats_lower_levels and below and st.below_level_mean_pct is not None:
        if not (st.mean_pct is not None and st.mean_pct > st.below_level_mean_pct):
            failures.append(
                f"level >= {level} ({st.mean_pct}%) does not beat level < {level} "
                f"({st.below_level_mean_pct}%): the level adds nothing, keep it flat"
            )
    st.verdict = "REJECT" if failures else "PROPOSE"
    st.reasons.extend(failures or ["every pre-registered condition holds; propose to the owner"])
    return st


def level_table(trades: Iterable[PaperTrade], *, rule: SizeRule = RULE) -> dict[str, list[LevelStat]]:
    """``{chain: [LevelStat for k in rule.levels]}`` plus ``"pooled"`` (never decides)."""
    items = list(trades)
    out: dict[str, list[LevelStat]] = {}
    for chain in sorted({t.chain for t in items}) + ["pooled"]:
        pool = items if chain == "pooled" else [t for t in items if t.chain == chain]
        rows = []
        for k in rule.levels:
            at = [t for t in pool if t.level >= k]
            below = [t for t in pool if t.level < k]
            st = _stat(chain, k, at, below, rule)
            if chain == "pooled" and st.verdict == "PROPOSE":
                st.verdict = "INFO_ONLY"
                st.reasons.append("pooled across chains: reported, never a proposal")
            rows.append(st)
        out[chain] = rows
    return out


# --------------------------------------------------------------------------------------
# every signal, priced from the tape
# --------------------------------------------------------------------------------------


def signal_forward(
    conn: sqlite3.Connection,
    *,
    rule: SizeRule = RULE,
    since_ms: int = RULE_REGISTERED_MS,
    until_ms: int | None = None,
    horizons_s: Sequence[int] = (3_600, 14_400),
    entry_lag_ms: int = 5_000,
    fee: Decimal = Decimal("0.02"),
    limit: int = 5_000,
) -> dict[str, Any]:
    """Fixed-horizon tape returns for every proven confluence signal, by chain and level.

    Entry is the first print at or after the signal plus ``entry_lag_ms`` (within 600 s);
    the horizon mark is the first print at or after ``created + h`` (within 30 min), else
    the LAST print before it, counted as ``stale``. A token with no print at all after
    entry is scored -100% (it went quiet; 88% of those never trade again). Read-only:
    ``idx_signals_lane`` plus two ``idx_swaps_token`` seeks per signal and horizon.
    """
    sql = "SELECT signal_id, chain, token, created_ms, payload_json FROM signals WHERE lane = ? AND created_ms >= ?"
    args: list[Any] = [rule.lane, int(since_ms)]
    if until_ms is not None:
        sql += " AND created_ms <= ?"
        args.append(int(until_ms))
    rows = fetch_all(conn, sql + " ORDER BY created_ms LIMIT ?", (*args, int(limit)))
    cells: dict[tuple[str, int, int], list[float]] = {}
    counts = {"signals": 0, "no_entry": 0, "stale_mark": 0, "dead": 0}
    for row in rows:
        payload = jload(row["payload_json"], {}) or {}
        if str(payload.get("wallet_source") or "grade") != rule.wallet_source:
            continue
        level = payload.get("confluence_level", payload.get("entity_count"))
        if level is None:
            continue
        counts["signals"] += 1
        chain = Chain(str(row["chain"]))
        token = str(row["token"])
        created = int(row["created_ms"])
        entry = next_print(conn, chain, token, created + entry_lag_ms)
        if entry is None:
            counts["no_entry"] += 1
            continue
        for h in horizons_s:
            at = created + int(h) * 1000
            mark = next_print(conn, chain, token, at, max_wait_ms=1_800_000)
            if mark is None:
                last = fetch_one(
                    conn,
                    "SELECT ts_ms, price_usd FROM swaps WHERE chain=? AND token=? AND ts_ms > ? "
                    "AND ts_ms < ? AND price_usd IS NOT NULL AND price_usd != '' "
                    "ORDER BY ts_ms DESC LIMIT 1",
                    (chain.value, token, entry[1], at),
                )
                price = Decimal(str(last["price_usd"])) if last else None
                if price is None or price <= 0:
                    counts["dead"] += 1
                    ret = -1.0
                else:
                    counts["stale_mark"] += 1
                    ret = float(price / entry[0] - 1 - fee)
            else:
                ret = float(mark[0] / entry[0] - 1 - fee)
            for k in rule.levels:
                if int(level) >= k:
                    cells.setdefault((chain.value, int(h), k), []).append(ret)
    table = []
    for (chain, h, k), vals in sorted(cells.items()):
        s = sorted(vals)
        means = _boot(vals, draws=rule.bootstrap_draws, seed=rule.seed + k + h) if len(vals) >= 3 else None
        table.append({
            "chain": chain, "horizon_s": h, "level_ge": k, "n": len(vals),
            "mean_pct": _pct(sum(vals) / len(vals)),
            "median_pct": _pct(s[len(s) // 2]),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
            "ci95_pct": [_pct(means[int(0.025 * len(means))]), _pct(means[int(0.975 * len(means)) - 1])]
            if means else None,
        })
    return {"counts": counts, "table": table}


def report(
    conn: sqlite3.Connection,
    *,
    rule: SizeRule = RULE,
    since_ms: int = RULE_REGISTERED_MS,
    until_ms: int | None = None,
    with_forward: bool = True,
) -> dict[str, Any]:
    """Everything a reader needs to judge a sizing proposal, and the rule it is judged by."""
    trades, skipped = collect_paper_trades(conn, rule=rule, since_ms=since_ms, until_ms=until_ms)
    open_row = fetch_one(
        conn,
        "SELECT COUNT(*) AS n FROM positions WHERE lane = ? AND mode = 'shadow' AND closed_ms IS NULL",
        (rule.lane,),
    )
    out: dict[str, Any] = {
        "rule": rule.as_dict(),
        "rule_digest": rule_digest(rule),
        "rule_registered_ms": RULE_REGISTERED_MS,
        "since_ms": since_ms,
        "closed_paper_trades": len(trades),
        "open_paper_positions": int(open_row["n"]) if open_row else 0,
        "skipped": skipped,
        "levels": {c: [s.as_dict() for s in rows] for c, rows in level_table(trades, rule=rule).items()},
        "proposals": [
            {"chain": s.chain, "level_ge": s.level}
            for c, rows in level_table(trades, rule=rule).items() if c != "pooled"
            for s in rows if s.verdict == "PROPOSE"
        ],
        "note": (
            "Nothing here changes a size. A PROPOSE is a recommendation to put in front of "
            "the owner with this table; sizing stays flat until he decides."
        ),
    }
    if with_forward:
        out["signal_forward"] = signal_forward(conn, rule=rule, since_ms=since_ms, until_ms=until_ms)
    return out


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - thin CLI
    import argparse
    import json

    from kaiba.core.db import get_conn

    ap = argparse.ArgumentParser(description="Paper EV of confluence-5 (proven) by confluence level")
    ap.add_argument("--since-ms", type=int, default=RULE_REGISTERED_MS)
    ap.add_argument("--no-forward", action="store_true")
    args = ap.parse_args(argv)
    print(json.dumps(report(get_conn(), since_ms=args.since_ms, with_forward=not args.no_forward),
                     indent=1, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
