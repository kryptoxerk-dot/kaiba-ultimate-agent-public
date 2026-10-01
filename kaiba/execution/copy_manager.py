"""Manage the owner's GMGN copy-trade positions on the agent wallet: trim winners, cut weak losers.

THE OWNER'S ASK, 2026-09-30, verbatim: "dont touch my copy trading but manage them just if
they are making profit like trim them help me and sell help me / cut loss if trend is weak
but i already have auto -50% cut loss".

The owner runs GMGN's own copy trading on the agent's Robinhood wallet. MEASURED the same
day from GMGN's stats for that wallet: -$3,754 realized over 30 days on $49k bought, 26%
win rate, 1 token in 545 above 2x -- while the 59 wallets it follows were +$3.5M between
them. The leaders win and the copies lose, so the copies' EXITS are where money leaks.

What this does, every run:

* Reads the wallet's holdings from GMGN (one call; GMGN's own cost basis and P&L).
* Leaves alone anything it must not touch: tokens Kaiba's own ledger holds (the watchdog
  manages those), honeypots, pools too thin to sell into, dust.
* Trims a winner in steps (33% at +25%, half the rest at +50%, half the rest at +100%,
  ~17% left to ride), sells the rest when a winner gives back half its peak gain, and cuts
  a loser at -25% only while its price is still falling. The owner's GMGN -50% stays the
  hard floor; this acts before it only on a weak trend.

It never buys and never changes GMGN's copy settings. Every sell goes through
:func:`kaiba.execution.executor.submit` exactly as a watchdog exit does -- sized from the
wallet's on-chain balance, with a min_out floor, never ``min_out=0`` -- and lands in
``orders``, so the Telegram notifier reports it. A sell of a token with no Kaiba position
does not move Kaiba's ledger (``accounting.reduce`` finds no position).

The rungs follow ``learning.exit_study``'s best out-of-sample policy on 2026-09-29
(``tp-25/50/100``: -1.14% mean against -20.09% for the shipped exits) -- the best we have,
NOT a significant result (``any_policy_significant: false``). ``live`` ships false.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlite3

from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.schemas import NATIVE_DECIMALS, Chain, Lane, LaneMode, Side, now_ms

log = logging.getLogger(__name__)

STATE_PREFIX = "copy_mgr:"
#: ERC-20s that ARE the native asset, never a position to manage.
NATIVE_WRAPPERS = {
    Chain.ROBINHOOD: {"0x0000000000000000000000000000000000000000"},
}


def _d(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


@dataclass(frozen=True)
class CopyConfig:
    live: bool = False
    min_liquidity_usd: Decimal = Decimal(5000)
    min_value_usd: Decimal = Decimal(5)
    #: ``(unrealized P&L at or above, fraction of the CURRENT balance to sell)``.
    tp_rungs: tuple[tuple[Decimal, Decimal], ...] = (
        (Decimal("0.25"), Decimal("0.33")),
        (Decimal("0.50"), Decimal("0.50")),
        (Decimal("1.00"), Decimal("0.50")),
    )
    #: Once a winner has reached ``giveback_arm_pnl``, sell the rest if it gives back this
    #: fraction of its peak gain.
    giveback: Decimal = Decimal("0.5")
    giveback_arm_pnl: Decimal = Decimal("0.25")
    #: Cut a loser at or below this P&L -- but only while the trend is weak.
    weak_cut_pnl: Decimal = Decimal("-0.25")
    #: "Weak": the price is at least this much below where it was ``trend_window_s`` ago.
    weak_drop: Decimal = Decimal("0.05")
    trend_window_s: int = 600
    max_sells_per_run: int = 3
    cooldown_s: int = 60

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> CopyConfig:
        base = cls()
        rungs = params.get("tp_rungs")
        return cls(
            live=bool(params.get("live", base.live)),
            min_liquidity_usd=_d(params.get("min_liquidity_usd")) or base.min_liquidity_usd,
            min_value_usd=_d(params.get("min_value_usd")) or base.min_value_usd,
            tp_rungs=tuple((Decimal(str(a)), Decimal(str(b))) for a, b in rungs) if rungs else base.tp_rungs,
            giveback=_d(params.get("giveback")) or base.giveback,
            giveback_arm_pnl=_d(params.get("giveback_arm_pnl")) or base.giveback_arm_pnl,
            weak_cut_pnl=_d(params.get("weak_cut_pnl")) or base.weak_cut_pnl,
            weak_drop=_d(params.get("weak_drop")) or base.weak_drop,
            trend_window_s=int(params.get("trend_window_s", base.trend_window_s)),
            max_sells_per_run=int(params.get("max_sells_per_run", base.max_sells_per_run)),
            cooldown_s=int(params.get("cooldown_s", base.cooldown_s)),
        )


@dataclass(frozen=True)
class Holding:
    token: str
    symbol: str
    decimals: int
    value_usd: Decimal
    pnl: Decimal | None
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    honeypot: bool
    started_s: int


def parse_holdings(payload: Any) -> list[Holding]:
    """GMGN ``portfolio holdings`` rows. MEASURED shape 2026-09-30: ``{"list": [...], "next"}``."""
    rows: Any = payload
    if isinstance(rows, Mapping):
        rows = rows.get("list") or rows.get("holdings") or []
    out: list[Holding] = []
    for r in rows if isinstance(rows, Sequence) else []:
        if not isinstance(r, Mapping):
            continue
        tok = r.get("token") if isinstance(r.get("token"), Mapping) else {}
        addr = str(tok.get("token_address") or "").lower()
        if not addr:
            continue
        out.append(Holding(
            token=addr,
            symbol=str(tok.get("symbol") or "")[:24],
            decimals=int(tok.get("decimals") or 18),
            value_usd=_d(r.get("usd_value")) or Decimal(0),
            pnl=_d(r.get("unrealized_profit_pnl")),
            price_usd=_d(tok.get("price")),
            liquidity_usd=_d(tok.get("liquidity")),
            honeypot=bool(tok.get("is_honeypot")),
            started_s=int(r.get("start_holding_at") or 0),
        ))
    return out


@dataclass
class TokenState:
    started_s: int = 0
    rungs_done: list[int] = field(default_factory=list)
    peak_pnl: Decimal | None = None
    prices: list[tuple[int, str]] = field(default_factory=list)
    last_action_ms: int = 0

    @classmethod
    def load(cls, raw: Mapping[str, Any] | None) -> TokenState:
        raw = raw or {}
        return cls(
            started_s=int(raw.get("started_s") or 0),
            rungs_done=[int(x) for x in raw.get("rungs_done") or []],
            peak_pnl=_d(raw.get("peak_pnl")),
            prices=[(int(t), str(p)) for t, p in raw.get("prices") or []],
            last_action_ms=int(raw.get("last_action_ms") or 0),
        )

    def dump(self) -> dict[str, Any]:
        return {"started_s": self.started_s, "rungs_done": self.rungs_done,
                "peak_pnl": str(self.peak_pnl) if self.peak_pnl is not None else None,
                "prices": self.prices, "last_action_ms": self.last_action_ms}


@dataclass(frozen=True)
class Decision:
    kind: str  # trim | giveback | weak_cut
    fraction: Decimal
    reason: str
    rung: int | None = None


def observe(state: TokenState, h: Holding, now: int, cfg: CopyConfig) -> TokenState:
    """Fold one sighting into the token's state. A new holding cycle starts afresh."""
    if h.started_s and h.started_s != state.started_s:
        state = TokenState(started_s=h.started_s)
    if h.pnl is not None and (state.peak_pnl is None or h.pnl > state.peak_pnl):
        state.peak_pnl = h.pnl
    if h.price_usd is not None and h.price_usd > 0:
        state.prices.append((now, str(h.price_usd)))
    horizon = now - (cfg.trend_window_s * 2 + 120) * 1000
    state.prices = [(t, p) for t, p in state.prices if t >= horizon]
    return state


def _price_then(state: TokenState, now: int, window_s: int) -> Decimal | None:
    """The newest sample at least ``window_s`` old: 'where it was a window ago'."""
    cutoff = now - window_s * 1000
    older = [(t, p) for t, p in state.prices if t <= cutoff]
    return _d(older[-1][1]) if older else None


def decide(h: Holding, state: TokenState, now: int, cfg: CopyConfig) -> Decision | None:
    """Pure: what to do with one holding, given what we have seen of it. None = hold."""
    if h.pnl is None:
        return None
    if state.last_action_ms and now - state.last_action_ms < cfg.cooldown_s * 1000:
        return None
    peak = state.peak_pnl
    if peak is not None and peak >= cfg.giveback_arm_pnl:
        floor = peak * (Decimal(1) - cfg.giveback)
        if h.pnl <= floor:
            return Decision("giveback", Decimal(1),
                            f"peak {peak:+.0%} gave back to {h.pnl:+.0%} (floor {floor:+.0%})")
    for i, (at, frac) in enumerate(cfg.tp_rungs):
        if i in state.rungs_done:
            continue
        if h.pnl >= at:
            return Decision("trim", frac, f"{h.pnl:+.0%} reached rung {i + 1} ({at:+.0%})", rung=i)
        break  # rungs are in order: an unreached rung blocks the ones above it
    if h.pnl <= cfg.weak_cut_pnl and h.price_usd is not None:
        then = _price_then(state, now, cfg.trend_window_s)
        if then is not None and then > 0 and h.price_usd <= then * (Decimal(1) - cfg.weak_drop):
            drop = h.price_usd / then - 1
            return Decision("weak_cut", Decimal(1),
                            f"{h.pnl:+.0%} and still falling ({drop:+.0%} in {cfg.trend_window_s // 60} min)")
    return None


def excluded(h: Holding, kaiba_tokens: set[str], chain: Chain, cfg: CopyConfig) -> str | None:
    """Why a holding is not ours to manage, or None."""
    if h.token in kaiba_tokens:
        return "kaiba_position"
    if h.token in NATIVE_WRAPPERS.get(chain, set()):
        return "native"
    if h.honeypot:
        return "honeypot"
    if h.liquidity_usd is None or h.liquidity_usd < cfg.min_liquidity_usd:
        return "thin_pool"
    if h.value_usd < cfg.min_value_usd:
        return "dust"
    return None


def kaiba_open_tokens(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    return {str(r["token"]).lower() for r in fetch_all(
        conn, "SELECT token FROM positions WHERE chain=? AND closed_ms IS NULL", (chain.value,))}


def load_state(conn: sqlite3.Connection, chain: Chain, token: str) -> TokenState:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (f"{STATE_PREFIX}{chain.value}:{token}",))
    return TokenState.load(jload(row["value"], {}) if row else {})


def save_state(conn: sqlite3.Connection, chain: Chain, token: str, state: TokenState, now: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (f"{STATE_PREFIX}{chain.value}:{token}", jdump(state.dump()), now),
    )


def min_out_units(tokens_units: int, decimals: int, price_usd: Decimal, native_usd: Decimal,
                  chain: Chain, slippage_bps: int) -> int | None:
    """Native base units we insist on. Same arithmetic as the watchdog's exit; never 0."""
    if price_usd <= 0 or native_usd <= 0:
        return None
    proceeds_usd = Decimal(tokens_units) / (Decimal(10) ** decimals) * price_usd
    native_units = proceeds_usd / native_usd * (Decimal(10) ** NATIVE_DECIMALS[chain])
    floor = int(native_units * (Decimal(10_000) - Decimal(slippage_bps)) / Decimal(10_000))
    if floor <= 0 and native_units >= 1:
        floor = 1
    return floor if floor > 0 else None


@dataclass
class RunReport:
    live: bool
    holdings: int = 0
    managed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    sells: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"live": self.live, "holdings": self.holdings, "managed": self.managed,
                "skipped": self.skipped, "decisions": self.decisions[:10],
                "sells": self.sells[:10], "errors": self.errors[:5]}


def run(
    conn: sqlite3.Connection,
    chain: Chain,
    cfg: CopyConfig,
    *,
    fetch_holdings: Callable[[], Any],
    wallet_units: Callable[[str, int], int | None],
    native_usd: Callable[[], Decimal | None],
    submit: Callable[[str, int, int], dict[str, Any]],
    slippage_bps: int,
    now: int | None = None,
) -> RunReport:
    """One pass. Every side effect is injected so the policy is testable without a venue."""
    ts = now if now is not None else now_ms()
    report = RunReport(live=cfg.live)
    holdings = parse_holdings(fetch_holdings())
    report.holdings = len(holdings)
    ours = kaiba_open_tokens(conn, chain)
    for h in holdings:
        why = excluded(h, ours, chain, cfg)
        if why:
            report.skipped[why] = report.skipped.get(why, 0) + 1
            continue
        report.managed += 1
        state = observe(load_state(conn, chain, h.token), h, ts, cfg)
        d = decide(h, state, ts, cfg)
        if d is not None:
            report.decisions.append({"token": h.token, "symbol": h.symbol, "kind": d.kind,
                                     "fraction": str(d.fraction), "pnl": str(h.pnl), "reason": d.reason})
            if cfg.live and len(report.sells) < cfg.max_sells_per_run:
                sold = _sell(h, d, chain, wallet_units, native_usd, submit, slippage_bps, report)
                if sold:
                    state.last_action_ms = ts
                    if d.rung is not None:
                        state.rungs_done.append(d.rung)
        save_state(conn, chain, h.token, state, ts)
    return report


def _sell(h: Holding, d: Decision, chain: Chain, wallet_units: Callable[[str, int], int | None],
          native_usd: Callable[[], Decimal | None], submit: Callable[[str, int, int], dict[str, Any]],
          slippage_bps: int, report: RunReport) -> bool:
    held = wallet_units(h.token, h.decimals)
    if not held or held <= 0:
        report.errors.append(f"{h.symbol}: wallet holds none")
        return False
    qty = held if d.fraction >= 1 else int(Decimal(held) * d.fraction)
    if qty <= 0:
        return False
    usd = native_usd()
    floor = min_out_units(qty, h.decimals, h.price_usd or Decimal(0), usd or Decimal(0), chain, slippage_bps)
    if floor is None:
        report.errors.append(f"{h.symbol}: cannot price min_out; not sending min_out=0")
        return False
    try:
        result = submit(h.token, qty, floor)
    except Exception as exc:  # noqa: BLE001 - one refused sell must not stop the pass
        report.errors.append(f"{h.symbol}: {type(exc).__name__}: {exc}"[:200])
        return False
    report.sells.append({"token": h.token, "symbol": h.symbol, "kind": d.kind, "qty": str(qty),
                         "min_out": str(floor), **result})
    return True


def gmgn_submitter(conn: sqlite3.Connection, chain: Chain, slippage_bps: int) -> Callable[[str, int, int], dict[str, Any]]:
    """The live seam: build and submit a SELL exactly as a watchdog exit does."""
    from kaiba.execution import executor

    def _submit(token: str, qty: int, min_out: int) -> dict[str, Any]:
        order = executor.build_order(
            decision_id=None, chain=chain, token=token, side=Side.SELL, lane=Lane.MANUAL,
            mode=LaneMode.LIVE, amount_in=qty, min_out=min_out, slippage_bps=slippage_bps,
        )
        res = executor.submit(order, conn)
        return {"order_id": res.order_id, "state": getattr(res.state, "value", str(res.state))}

    return _submit


def summarize(decisions: Iterable[Mapping[str, Any]]) -> str:
    return "; ".join(f"{d['symbol']} {d['kind']} {d['fraction']} ({d['reason']})" for d in decisions)
