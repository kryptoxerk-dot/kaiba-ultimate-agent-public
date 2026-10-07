"""Manage the owner's GMGN copy-trade positions on the agent wallet: sell a winner that gives back.

THE OWNER'S ASK, 2026-09-30, verbatim: "dont touch my copy trading but manage them just if
they are making profit like trim them help me and sell help me / cut loss if trend is weak
but i already have auto -50% cut loss".

The owner runs GMGN's own copy trading on the agent's Robinhood wallet. MEASURED the same
day from GMGN's stats for that wallet: a net realized LOSS over 30 days, 26%
win rate, 1 token in 545 above 2x -- while the 59 wallets it follows were strongly profitable
between them. The leaders win and the copies lose, so the copies' EXITS are where money leaks.

POLICY, 2026-10-01: GIVEBACK ONLY. MEASURED on 35.7 h of dry-run decisions (48 tokens):
selling everything once a position has given back half its peak gain (armed when the peak
reached +25%) was net positive on the capital at stake after a 5pp execution haircut (CI -0.5% to
+36.8%; 25 of 30 decisions right). All three rules together made less, because the 33/50/50
trims sold runners early (OFY: +14% trimmed against +383% held). The weak cut was neutral
(+$34 at a 5pp haircut, -$62 at 10pp). The owner approved giveback only, so the box sets
``tp_rungs: []`` and ``weak_cut_enabled: false``. The trim and cut code stays, and stays the
default for a config that does not name the keys, because removing it was not measured.

Where the trim rungs came from: ``learning.exit_study``'s best out-of-sample policy on
2026-09-29 (``tp-25/50/100``, -1.14% mean). The -20.09% it was compared against there is
that study's STOP-ONLY STRAW MAN, not the exit ladder Kaiba actually ships, and no policy
in it was significant (``any_policy_significant: false``).

What this does, every run:

* Reads the wallet's holdings from GMGN (one call; GMGN's own cost basis and P&L).
* Leaves alone anything it must not touch: tokens Kaiba's own ledger holds (the watchdog
  manages those), honeypots, pools too thin to sell into, dust.
* Sells everything when a winner gives back ``giveback`` of its peak gain, and -- only
  when configured -- trims at ``tp_rungs`` and cuts a loser at ``weak_cut_pnl`` while its
  price is still falling. The owner's GMGN -50% stays the hard floor.

It never buys and never changes GMGN's copy settings. Every sell goes through
:func:`kaiba.execution.executor.submit` exactly as a watchdog exit does -- sized from the
wallet's on-chain balance, with a min_out floor, never ``min_out=0`` -- and lands in
``orders``, so the Telegram notifier reports it. A sell of a token with no Kaiba position
does not move Kaiba's ledger (``accounting.reduce`` finds no position).

What keeps a live sell from going wrong:

* **Holding cycles.** A full sell CLOSES the token's cycle: nothing more is decided for it.
  A new cycle starts only on evidence of a buy -- ``start_holding_at`` changed,
  ``history_total_buys`` rose, ``accu_cost`` rose, or (with no buy count) the balance
  rose -- and it starts with a FRESH peak. A peak from before a re-buy or a top-up is in a
  different cost basis; carried over, it would sell the new buy on sight.
* **Never twice at once.** No sell is sent while any sell of that token is unresolved in
  ``orders``. An ambiguous send (``ExecutionAmbiguous``: it may be live) blocks the token
  until ``executor.reconcile`` resolves it, and is never re-sent blind.
* **No tight loops.** Every attempt starts ``cooldown_s``; each consecutive refusal doubles
  it (up to 32x). A refused full sell that reconcile later reports failed re-opens the
  cycle so the giveback can try again -- after the backoff.
* **Caps.** ``max_sells_per_run`` per pass and ``max_sells_per_day`` per UTC day. The day
  count lives in ``kv`` and is taken BEFORE the send, so a crash mid-send still counts;
  only a clean refusal (nothing sent) gives it back.
* **A record.** Every live attempt writes a ``system`` event with ``payload.service ==
  "copy_manager"`` and ``payload.action == "copy_manager_sell"``: token, kind, P&L and price
  at decision, peak, quantity, min_out, order id, outcome. Enough to score it later.

Dry run (``live: false``) decides and records, and NEVER advances sell state: no rung is
marked done and no cycle closes on paper. Flipping live therefore acts on the current truth,
not on sells that never happened -- and a dry-run decision repeats each pass until its
condition clears. ``live`` ships false.

THE OWNER'S WALLET, 2026-10-03 (owner: "do not buy on the kaiba wallet of 0x41.. but control
the copy trade sell before it rug"). Kaiba now trades from its own wallet (risk.yaml
``chains.<chain>.wallet``) and the owner copy-trades on 0x7243.... So:

* **An explicit wallet, never a default.** :func:`wallet_refusal` refuses a missing wallet,
  Kaiba's own chain wallet, a wallet the operator has not declared in signer-policy
  ``owned_addresses``, and a Solana chain (token keys here are lowercased). Holdings, the
  balance read and the sell all use that one wallet; the sell names it through the
  executor's narrow ``from_wallet`` override (Lane.MANUAL SELL only).
* **Never a buy.** The only order this module can build is a ``Side.SELL``
  (:func:`gmgn_submitter`), and the executor refuses ``from_wallet`` on a buy as well.
* **API binding.** GMGN only swaps ``--from`` a wallet bound to the API key. The job reads
  ``portfolio info`` (cached for an hour, DISCOVERY priority) and, until the wallet is
  bound, runs dry and reports ``wallet_not_api_bound`` instead of failing noisily.

"SELL BEFORE IT RUGS": three sell-everything rules, each a named decision kind with its own
``off | dry | live`` mode, MEASURED read-only 2026-10-03 on the copy book's last 30 days
(GMGN holdings incl. closed for 0x7243: 595 tokens; 302 holding windows with Kaiba swap tape,
outcome = GMGN's own total_profit; 10pp execution haircut):

* ``fast_crash`` -- price down ``crash_drop`` from its high inside ``crash_window_s``.
  Fifteen variants, none with a CI clear of zero on the right side; most negative. Y=50%
  in 15 min: 85 fires, -$304 (CI -$1,460..+$996), 25 of them sold a token that went on to
  1.5x within 2 h. Y=25% in 5 min: -$2,368. The tokens are this volatile in normal life.
* ``liquidity_collapse`` -- pool liquidity down ``liq_drop`` from its high inside
  ``liq_window_s``, CORROBORATED by price down ``liq_price_drop`` over the same window, and
  never inside ``migration_grace_s`` of a graduation. Replayed on the Pons curve reserve
  (cumulative net native flow): -$32 (CI -$823..+$631) at X=40%/15 min, -$212 at 60%/30
  min. On a curve, liquidity is a function of price, so this is a price rule in disguise.
  Graduation is NOT a rug: GMGN's ``open_timestamp`` equals Kaiba's ``tokens.migrated_ms``
  to the second on all 169 graduated copy tokens, so the payload itself marks it; both
  sources are honoured. (A Kaiba rule once sold 33 of 45 graduations as rugs.)
* ``risk_flags`` -- GMGN's ``is_show_alert`` turning on while held (a level, not a flip,
  was set on 163 of 302 windows: -16.4% mean vs +8.6% unflagged -- but read at the END, so
  it may be a consequence of the fall). A ``is_honeypot`` flip is recorded and never sent:
  a honeypot cannot be sold. The holdings payload carries no sell tax, blacklist or creator
  balance; those need ``token security`` per token and are not wired.

So all three ship ``dry``: they decide, and every firing is recorded once per holding cycle
(``system`` event, ``payload.action == "copy_manager_rule"``, price/liquidity/P&L at the
decision) so the next measurement is forward and out of sample. Giveback stays live.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core import events as ev
from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.schemas import (
    NATIVE_DECIMALS,
    Chain,
    EventKind,
    Lane,
    LaneMode,
    OrderState,
    Side,
    looks_evm,
    now_ms,
)

log = logging.getLogger(__name__)

STATE_PREFIX = "copy_mgr:"
#: ``copy_mgr_sells:<chain>:<YYYY-MM-DD>`` -> live sends that day. Outside the ``copy_mgr:``
#: range on purpose, so a prefix scan of token states never meets it.
DAY_PREFIX = "copy_mgr_sells:"
SERVICE = "copy_manager"
#: ``payload.action`` of the ``system`` event every live sell attempt writes. Not an
#: ``EventKind``: ``Event.kind`` is strict and the enum lives in ``kaiba/core``.
SELL_ACTION = "copy_manager_sell"
#: ERC-20s that ARE the native asset, never a position to manage.
NATIVE_WRAPPERS = {
    Chain.ROBINHOOD: {"0x0000000000000000000000000000000000000000"},
}
#: An order in one of these states may still move tokens. Never send a second sell beside it.
UNRESOLVED_STATES: tuple[str, ...] = tuple(s.value for s in (
    OrderState.PLANNED, OrderState.RESERVED, OrderState.SUBMITTING, OrderState.SUBMITTED,
    OrderState.PARTIAL, OrderState.UNKNOWN,
))
#: Consecutive refusals double the cooldown, at most this many times (60 s -> 32 min).
MAX_BACKOFF_DOUBLINGS = 5
#: A rise this large in cost basis, or in balance when GMGN gives no buy count, is a buy.
COST_JUMP = Decimal("0.02")
BALANCE_JUMP = Decimal("0.02")
#: Keys a ``copy_manager`` job block may carry besides :class:`CopyConfig`'s own.
OTHER_JOB_KEYS = frozenset({"chain", "wallet", "binding_ttl_s"})
#: ``payload.action`` of the ``system`` event a rug rule writes when it fires and is NOT
#: sent (its mode is dry, the job is dry, or the wallet is not API-bound). Once per cycle.
RULE_ACTION = "copy_manager_rule"
#: A rug rule's mode: not evaluated, decided and recorded only, or allowed to sell.
RULE_MODES = ("off", "dry", "live")
#: The sell-everything rules that guard against a rug, in the order they are offered.
RUG_KINDS = ("liquidity_collapse", "risk_flags", "fast_crash")
#: Why :func:`run` held a token back that is not a sell-state reason.
BLOCK_NOT_BOUND = "wallet_not_api_bound"
BLOCK_BINDING_UNKNOWN = "binding_unknown"


def _d(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _int_or_none(value: Any) -> int | None:
    out = _d(value)
    return int(out) if out is not None else None


# --------------------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------------------


def _p_decimal(params: Mapping[str, Any], key: str, default: Decimal) -> Decimal:
    """Absent or null means the default. An explicit 0 is 0 (``x or default`` made it the default)."""
    if params.get(key) is None:
        return default
    out = _d(params[key])
    if out is None:
        raise ValueError(f"copy_manager.{key}: not a number: {params[key]!r}")
    return out


def _p_int(params: Mapping[str, Any], key: str, default: int) -> int:
    out = _p_decimal(params, key, Decimal(default))
    if out != out.to_integral_value():
        raise ValueError(f"copy_manager.{key}: not a whole number: {params[key]!r}")
    return int(out)


def _p_bool(params: Mapping[str, Any], key: str, default: bool) -> bool:
    """Strict: ``bool("false")`` is True, and this flag can sell."""
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "on", "1"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "off", "0"}:
        return False
    raise ValueError(f"copy_manager.{key}: not a boolean: {value!r}")


def _p_mode(params: Mapping[str, Any], key: str, default: str) -> str:
    """``off | dry | live``, spelled out. A bool is refused: ``true`` would not say which."""
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str) and value.strip().lower() in RULE_MODES:
        return value.strip().lower()
    raise ValueError(f"copy_manager.{key}: one of {list(RULE_MODES)}, not {value!r}")


def _p_rungs(params: Mapping[str, Any], default: tuple[tuple[Decimal, Decimal], ...]) -> tuple[tuple[Decimal, Decimal], ...]:
    """Absent key: the default rungs. ``[]``: NO trims. ``null``: refused as ambiguous.

    The old reading (``... if rungs else default``) turned ``[]`` into the defaults, so trims
    could not be switched off by config -- and the measured policy is trims off.
    """
    if "tp_rungs" not in params:
        return default
    raw = params["tp_rungs"]
    if raw is None:
        raise ValueError("copy_manager.tp_rungs: null is ambiguous; use [] for no trims, "
                         "or remove the key for the default rungs")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError(f"copy_manager.tp_rungs: not a list: {raw!r}")
    out: list[tuple[Decimal, Decimal]] = []
    for item in raw:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
            raise ValueError(f"copy_manager.tp_rungs: each rung is [pnl, fraction]: {item!r}")
        at, frac = _d(item[0]), _d(item[1])
        if at is None or frac is None or at <= 0 or not (0 < frac <= 1):
            raise ValueError(f"copy_manager.tp_rungs: need pnl > 0 and 0 < fraction <= 1: {item!r}")
        out.append((at, frac))
    if [a for a, _ in out] != sorted({a for a, _ in out}):
        raise ValueError("copy_manager.tp_rungs: rungs must be in strictly rising order")
    return tuple(out)


@dataclass(frozen=True)
class CopyConfig:
    live: bool = False
    min_liquidity_usd: Decimal = Decimal(5000)
    min_value_usd: Decimal = Decimal(5)
    #: ``(unrealized P&L at or above, fraction of the CURRENT balance to sell)``. Empty = no trims.
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
    weak_cut_enabled: bool = True
    weak_cut_pnl: Decimal = Decimal("-0.25")
    #: "Weak": the price is at least this much below where it was ``trend_window_s`` ago.
    weak_drop: Decimal = Decimal("0.05")
    trend_window_s: int = 600
    max_sells_per_run: int = 3
    #: Live sends per UTC day, counted in ``kv`` before each send. A bug cannot loop the
    #: book out of the wallet faster than this.
    max_sells_per_day: int = 30
    cooldown_s: int = 60
    #: Rug rules. Each ships ``dry``: decided and recorded, never sent (see the module
    #: docstring for the measurement). ``live`` lets it sell when the job is live too.
    liquidity_collapse: str = "dry"
    liq_drop: Decimal = Decimal("0.4")
    liq_window_s: int = 900
    #: A liquidity drop counts only when the price fell this much over the same window: a
    #: graduation drains the curve without crashing the price, a rug does both.
    liq_price_drop: Decimal = Decimal("0.3")
    #: A liquidity drop within this long after a graduation is the migration, not a rug.
    migration_grace_s: int = 1800
    risk_flags: str = "dry"
    fast_crash: str = "dry"
    crash_drop: Decimal = Decimal("0.5")
    crash_window_s: int = 900
    #: No crash cut in the first minutes of a holding: a copy that buys a spike and sees it
    #: retrace is the normal case on these tokens, not a rug.
    crash_min_hold_s: int = 300

    def __post_init__(self) -> None:
        if not (0 < self.giveback <= 1):
            raise ValueError(f"copy_manager.giveback must be in (0, 1]: {self.giveback}")
        if self.giveback_arm_pnl <= 0:
            raise ValueError(f"copy_manager.giveback_arm_pnl must be > 0: {self.giveback_arm_pnl}")
        if self.weak_cut_pnl >= 0:
            raise ValueError(f"copy_manager.weak_cut_pnl must be < 0: {self.weak_cut_pnl}")
        if not (0 < self.weak_drop < 1):
            raise ValueError(f"copy_manager.weak_drop must be in (0, 1): {self.weak_drop}")
        if self.trend_window_s <= 0:
            raise ValueError(f"copy_manager.trend_window_s must be > 0: {self.trend_window_s}")
        for name in ("max_sells_per_run", "max_sells_per_day", "cooldown_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"copy_manager.{name} must be >= 0")
        if self.min_liquidity_usd < 0 or self.min_value_usd < 0:
            raise ValueError("copy_manager.min_liquidity_usd / min_value_usd must be >= 0")
        for name in RUG_KINDS:
            if getattr(self, name) not in RULE_MODES:
                raise ValueError(f"copy_manager.{name} must be one of {list(RULE_MODES)}")
        for name in ("liq_drop", "crash_drop"):
            if not (0 < getattr(self, name) < 1):
                raise ValueError(f"copy_manager.{name} must be in (0, 1): {getattr(self, name)}")
        if not (0 <= self.liq_price_drop < 1):
            raise ValueError(f"copy_manager.liq_price_drop must be in [0, 1): {self.liq_price_drop}")
        for name in ("liq_window_s", "crash_window_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"copy_manager.{name} must be > 0")
        if self.migration_grace_s < 0 or self.crash_min_hold_s < 0:
            raise ValueError("copy_manager.migration_grace_s / crash_min_hold_s must be >= 0")

    def mode_of(self, kind: str) -> str:
        """A rug rule's mode; the three policy kinds are governed by ``live`` and their keys."""
        return str(getattr(self, kind)) if kind in RUG_KINDS else ("live" if self.live else "dry")

    def any_rug_rule(self) -> bool:
        return any(getattr(self, k) != "off" for k in RUG_KINDS)

    def history_s(self) -> int:
        """How far back the price and liquidity samples must reach for every rule."""
        return max(self.trend_window_s * 2, self.crash_window_s, self.liq_window_s) + 120

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> CopyConfig:
        """Read a job block. An unknown key is an error, not a silent no-op: a misspelt
        ``weak_cut_enabled`` would otherwise leave a sell rule on."""
        known = set(cls.__dataclass_fields__) | OTHER_JOB_KEYS
        unknown = sorted(set(params) - known)
        if unknown:
            raise ValueError(f"copy_manager: unknown keys {unknown}; known: {sorted(known)}")
        base = cls()
        return cls(
            live=_p_bool(params, "live", base.live),
            min_liquidity_usd=_p_decimal(params, "min_liquidity_usd", base.min_liquidity_usd),
            min_value_usd=_p_decimal(params, "min_value_usd", base.min_value_usd),
            tp_rungs=_p_rungs(params, base.tp_rungs),
            giveback=_p_decimal(params, "giveback", base.giveback),
            giveback_arm_pnl=_p_decimal(params, "giveback_arm_pnl", base.giveback_arm_pnl),
            weak_cut_enabled=_p_bool(params, "weak_cut_enabled", base.weak_cut_enabled),
            weak_cut_pnl=_p_decimal(params, "weak_cut_pnl", base.weak_cut_pnl),
            weak_drop=_p_decimal(params, "weak_drop", base.weak_drop),
            trend_window_s=_p_int(params, "trend_window_s", base.trend_window_s),
            max_sells_per_run=_p_int(params, "max_sells_per_run", base.max_sells_per_run),
            max_sells_per_day=_p_int(params, "max_sells_per_day", base.max_sells_per_day),
            cooldown_s=_p_int(params, "cooldown_s", base.cooldown_s),
            liquidity_collapse=_p_mode(params, "liquidity_collapse", base.liquidity_collapse),
            liq_drop=_p_decimal(params, "liq_drop", base.liq_drop),
            liq_window_s=_p_int(params, "liq_window_s", base.liq_window_s),
            liq_price_drop=_p_decimal(params, "liq_price_drop", base.liq_price_drop),
            migration_grace_s=_p_int(params, "migration_grace_s", base.migration_grace_s),
            risk_flags=_p_mode(params, "risk_flags", base.risk_flags),
            fast_crash=_p_mode(params, "fast_crash", base.fast_crash),
            crash_drop=_p_decimal(params, "crash_drop", base.crash_drop),
            crash_window_s=_p_int(params, "crash_window_s", base.crash_window_s),
            crash_min_hold_s=_p_int(params, "crash_min_hold_s", base.crash_min_hold_s),
        )


# --------------------------------------------------------------------------------------
# holdings
# --------------------------------------------------------------------------------------


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
    #: Human units (GMGN ``balance``), cost basis still held (``accu_cost``), lifetime buy
    #: count (``history_total_buys``). Each is None when GMGN leaves it out.
    balance: Decimal | None = None
    cost_usd: Decimal | None = None
    buys: int | None = None
    #: GMGN ``is_show_alert``; None when the payload leaves it out.
    alert: bool | None = None
    #: GMGN ``open_timestamp`` / ``creation_timestamp`` (seconds). MEASURED 2026-10-03: on a
    #: graduated launchpad token ``open_timestamp`` IS the graduation (equal to Kaiba's
    #: ``tokens.migrated_ms`` on 169 of 169); on one that never left its curve it equals
    #: the creation time.
    open_s: int = 0
    created_s: int = 0


def parse_holdings(payload: Any) -> list[Holding]:
    """GMGN ``portfolio holdings`` rows. MEASURED shape 2026-09-30: ``{"list": [...], "next"}``."""
    rows: Any = payload
    if isinstance(rows, Mapping) and isinstance(rows.get("data"), Mapping):
        rows = rows["data"]
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
            started_s=int(_d(r.get("start_holding_at")) or 0),
            balance=_d(r.get("balance")),
            cost_usd=_d(r.get("accu_cost")),
            buys=_int_or_none(r.get("history_total_buys")),
            alert=bool(tok["is_show_alert"]) if tok.get("is_show_alert") is not None else None,
            open_s=_int_or_none(tok.get("open_timestamp")) or 0,
            created_s=_int_or_none(tok.get("creation_timestamp")) or 0,
        ))
    return out


# --------------------------------------------------------------------------------------
# per-token state
# --------------------------------------------------------------------------------------


@dataclass
class TokenState:
    started_s: int = 0
    #: When this holding cycle began (our first sighting of it), and why it began.
    cycle_ms: int = 0
    cycle_reason: str = ""
    rungs_done: list[int] = field(default_factory=list)
    peak_pnl: Decimal | None = None
    prices: list[tuple[int, str]] = field(default_factory=list)
    #: Last live attempt to sell, whatever its outcome: the cooldown clock.
    last_action_ms: int = 0
    #: Consecutive refusals; each doubles the cooldown.
    failures: int = 0
    #: Everything was sold this cycle. Nothing more is decided until a buy opens a new one.
    closed: bool = False
    closed_kind: str = ""
    closed_ms: int = 0
    #: Baselines a buy moves (see :func:`_cycle_break`).
    buys: int | None = None
    cost_usd: Decimal | None = None
    balance: Decimal | None = None
    #: The last sell we sent, until ``orders`` says how it ended.
    pending_id: str | None = None
    pending_kind: str = ""
    pending_rung: int | None = None
    #: True for an ExecutionAmbiguous send: a missing order row then proves nothing.
    pending_ambiguous: bool = False
    pending_cycle_ms: int = 0
    #: Liquidity samples, like ``prices``: ``(ts_ms, usd)`` inside :meth:`CopyConfig.history_s`.
    liqs: list[tuple[int, str]] = field(default_factory=list)
    #: Last ``is_show_alert`` / ``is_honeypot`` seen this cycle. A flag is a rug signal when it
    #: TURNS ON while held; half the book carries ``is_show_alert`` from its first sighting.
    alert: bool | None = None
    honeypot: bool = False
    #: The flag that turned on this cycle, and when. Latched until the cycle ends.
    flag_reason: str = ""
    flag_ms: int = 0
    #: Rug rule kinds already recorded this cycle (one record per cycle per rule).
    logged: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, raw: Mapping[str, Any] | None) -> TokenState:
        raw = raw or {}
        rung = raw.get("pending_rung")
        alert = raw.get("alert")
        return cls(
            started_s=int(raw.get("started_s") or 0),
            cycle_ms=int(raw.get("cycle_ms") or 0),
            cycle_reason=str(raw.get("cycle_reason") or ""),
            rungs_done=[int(x) for x in raw.get("rungs_done") or []],
            peak_pnl=_d(raw.get("peak_pnl")),
            prices=[(int(t), str(p)) for t, p in raw.get("prices") or []],
            last_action_ms=int(raw.get("last_action_ms") or 0),
            failures=int(raw.get("failures") or 0),
            closed=bool(raw.get("closed")),
            closed_kind=str(raw.get("closed_kind") or ""),
            closed_ms=int(raw.get("closed_ms") or 0),
            buys=_int_or_none(raw.get("buys")),
            cost_usd=_d(raw.get("cost_usd")),
            balance=_d(raw.get("balance")),
            pending_id=str(raw["pending_id"]) if raw.get("pending_id") else None,
            pending_kind=str(raw.get("pending_kind") or ""),
            pending_rung=int(rung) if rung is not None else None,
            pending_ambiguous=bool(raw.get("pending_ambiguous")),
            pending_cycle_ms=int(raw.get("pending_cycle_ms") or 0),
            liqs=[(int(t), str(v)) for t, v in raw.get("liqs") or []],
            alert=bool(alert) if alert is not None else None,
            honeypot=bool(raw.get("honeypot")),
            flag_reason=str(raw.get("flag_reason") or ""),
            flag_ms=int(raw.get("flag_ms") or 0),
            logged=[str(k) for k in raw.get("logged") or []],
        )

    def dump(self) -> dict[str, Any]:
        def s(x: Decimal | None) -> str | None:
            return str(x) if x is not None else None

        return {"started_s": self.started_s, "cycle_ms": self.cycle_ms, "cycle_reason": self.cycle_reason,
                "rungs_done": self.rungs_done, "peak_pnl": s(self.peak_pnl), "prices": self.prices,
                "last_action_ms": self.last_action_ms, "failures": self.failures,
                "closed": self.closed, "closed_kind": self.closed_kind, "closed_ms": self.closed_ms,
                "buys": self.buys, "cost_usd": s(self.cost_usd), "balance": s(self.balance),
                "pending_id": self.pending_id, "pending_kind": self.pending_kind,
                "pending_rung": self.pending_rung, "pending_ambiguous": self.pending_ambiguous,
                "pending_cycle_ms": self.pending_cycle_ms, "liqs": self.liqs, "alert": self.alert,
                "honeypot": self.honeypot, "flag_reason": self.flag_reason, "flag_ms": self.flag_ms,
                "logged": self.logged}


@dataclass(frozen=True)
class Decision:
    kind: str  # trim | giveback | weak_cut | liquidity_collapse | risk_flags | fast_crash
    fraction: Decimal
    reason: str
    rung: int | None = None


def _cycle_break(state: TokenState, h: Holding) -> str | None:
    """Why this sighting belongs to a NEW holding cycle, or None.

    Each signal is evidence of a buy. A false break only forgets a peak (a sell comes later);
    a missed one lets a peak from another cost basis sell a fresh buy at once. So any one of
    them is enough.
    """
    if h.started_s and h.started_s != state.started_s:
        return "start_holding_at" if state.started_s else "first_seen"
    if h.buys is not None and state.buys is not None and h.buys > state.buys:
        return "bought"
    if (h.cost_usd is not None and state.cost_usd is not None and h.cost_usd > 0
            and h.cost_usd > state.cost_usd * (1 + COST_JUMP)):
        return "cost_basis"
    if ((h.buys is None or state.buys is None) and h.balance is not None and state.balance is not None
            and h.balance > 0 and h.balance > state.balance * (1 + BALANCE_JUMP)):
        return "balance"
    return None


def observe(state: TokenState, h: Holding, now: int, cfg: CopyConfig) -> TokenState:
    """Fold one sighting into the token's state. A new holding cycle starts afresh."""
    why = _cycle_break(state, h)
    if why:
        # Fresh peak, rungs and close. The cooldown clock and the last send's tracking carry
        # over: that order is still out there, whichever cycle it belonged to. Flags start
        # from this sighting: a flag already on at the buy did not TURN on while held.
        state = TokenState(
            started_s=h.started_s or state.started_s, cycle_ms=now, cycle_reason=why,
            last_action_ms=state.last_action_ms, pending_id=state.pending_id,
            pending_kind=state.pending_kind, pending_rung=state.pending_rung,
            pending_ambiguous=state.pending_ambiguous, pending_cycle_ms=state.pending_cycle_ms,
            buys=state.buys, cost_usd=state.cost_usd, balance=state.balance,
            honeypot=h.honeypot,
        )
    if h.buys is not None:
        state.buys = h.buys if state.buys is None else max(state.buys, h.buys)
    if h.cost_usd is not None:
        state.cost_usd = h.cost_usd
    if h.balance is not None:
        state.balance = h.balance
    if h.pnl is not None and (state.peak_pnl is None or h.pnl > state.peak_pnl):
        state.peak_pnl = h.pnl
    if h.price_usd is not None and h.price_usd > 0:
        state.prices.append((now, str(h.price_usd)))
    if h.liquidity_usd is not None and h.liquidity_usd >= 0:
        state.liqs.append((now, str(h.liquidity_usd)))
    if h.alert is not None:
        if state.alert is False and h.alert and not state.flag_reason:
            state.flag_reason, state.flag_ms = "show_alert", now
        state.alert = h.alert
    if h.honeypot and not state.honeypot:
        state.honeypot = True
        if not state.flag_reason:
            state.flag_reason, state.flag_ms = "honeypot", now
    horizon = now - cfg.history_s() * 1000
    state.prices = [(t, p) for t, p in state.prices if t >= horizon]
    state.liqs = [(t, v) for t, v in state.liqs if t >= horizon]
    return state


def _price_then(state: TokenState, now: int, window_s: int) -> Decimal | None:
    """The newest sample at least ``window_s`` old: 'where it was a window ago'."""
    cutoff = now - window_s * 1000
    older = [(t, p) for t, p in state.prices if t <= cutoff]
    return _d(older[-1][1]) if older else None


def cooldown_ms(state: TokenState, cfg: CopyConfig) -> int:
    return cfg.cooldown_s * 1000 * (2 ** min(max(state.failures, 0), MAX_BACKOFF_DOUBLINGS))


def decide(h: Holding, state: TokenState, now: int, cfg: CopyConfig) -> Decision | None:
    """Pure: what to do with one holding, given what we have seen of it. None = hold."""
    if h.pnl is None or state.closed:
        return None
    if state.last_action_ms and now - state.last_action_ms < cooldown_ms(state, cfg):
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
    if cfg.weak_cut_enabled and h.pnl <= cfg.weak_cut_pnl and h.price_usd is not None:
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


# --------------------------------------------------------------------------------------
# rug rules: sell everything before it is gone. Pure; see the module docstring.
# --------------------------------------------------------------------------------------


def _window_max(samples: Sequence[tuple[int, str]], since: int) -> Decimal | None:
    vals = [v for t, raw in samples if t >= since and (v := _d(raw)) is not None]
    return max(vals) if vals else None


def migrating(h: Holding, now: int, cfg: CopyConfig, migrated_ms: int | None = None) -> str | None:
    """Why a liquidity drop now may be a graduation rather than a rug, or None.

    Two sources, either is enough: GMGN's ``open_timestamp`` when it differs from the
    creation time (the graduation, measured equal to Kaiba's record on 169 of 169), and
    Kaiba's own ``tokens.migrated_ms``. A timestamp in the future is ignored, as
    ``protection._migrating`` does: one bad value must not become a permanent waiver.
    """
    grace = cfg.migration_grace_s * 1000
    if grace <= 0:
        return None
    if h.open_s and h.created_s and h.open_s != h.created_s and 0 <= now - h.open_s * 1000 <= grace:
        return "gmgn_open_timestamp"
    if migrated_ms is not None and 0 <= now - int(migrated_ms) <= grace:
        return "kaiba_migrated_ms"
    return None


def _liquidity_collapse(h: Holding, state: TokenState, now: int, cfg: CopyConfig,
                        migrated_ms: Callable[[], int | None]) -> Decision | None:
    if h.liquidity_usd is None or h.price_usd is None or h.price_usd <= 0:
        return None
    since = now - cfg.liq_window_s * 1000
    liq_peak = _window_max(state.liqs, since)
    if liq_peak is None or liq_peak <= 0 or h.liquidity_usd > liq_peak * (1 - cfg.liq_drop):
        return None
    px_peak = _window_max(state.prices, since)
    if px_peak is None or h.price_usd > px_peak * (1 - cfg.liq_price_drop):
        return None  # liquidity left without the price falling: a migration's shape, not a rug's
    if migrating(h, now, cfg, migrated_ms()):
        return None
    return Decision("liquidity_collapse", Decimal(1),
                    f"liquidity {h.liquidity_usd / liq_peak - 1:+.0%} and price {h.price_usd / px_peak - 1:+.0%} "
                    f"in {cfg.liq_window_s // 60} min")


def _risk_flag(h: Holding, state: TokenState) -> Decision | None:
    if not state.flag_reason:
        return None
    return Decision("risk_flags", Decimal(1), f"GMGN {state.flag_reason} turned on while held")


def _fast_crash(h: Holding, state: TokenState, now: int, cfg: CopyConfig) -> Decision | None:
    if h.price_usd is None or h.price_usd <= 0:
        return None
    held_since = h.started_s * 1000 if h.started_s else state.cycle_ms
    if now - held_since < cfg.crash_min_hold_s * 1000:
        return None
    peak = _window_max(state.prices, now - cfg.crash_window_s * 1000)
    if peak is None or peak <= 0 or h.price_usd > peak * (1 - cfg.crash_drop):
        return None
    return Decision("fast_crash", Decimal(1),
                    f"price {h.price_usd / peak - 1:+.0%} from its high in {cfg.crash_window_s // 60} min")


def rug_decisions(h: Holding, state: TokenState, now: int, cfg: CopyConfig,
                  migrated_ms: Callable[[], int | None] = lambda: None) -> list[Decision]:
    """Every rug rule that fires on this sighting, in :data:`RUG_KINDS` order. Pure.

    Same gates as :func:`decide`: nothing for a closed cycle, nothing inside the cooldown.
    ``migrated_ms`` is called only when a liquidity collapse would otherwise fire.
    """
    if state.closed:
        return []
    if state.last_action_ms and now - state.last_action_ms < cooldown_ms(state, cfg):
        return []
    out: list[Decision] = []
    if cfg.liquidity_collapse != "off" and (d := _liquidity_collapse(h, state, now, cfg, migrated_ms)):
        out.append(d)
    if cfg.risk_flags != "off" and (d := _risk_flag(h, state)):
        out.append(d)
    if cfg.fast_crash != "off" and (d := _fast_crash(h, state, now, cfg)):
        out.append(d)
    return out


# --------------------------------------------------------------------------------------
# the wallet: explicit, the owner's, never Kaiba's, and bound to the API key before a sell
# --------------------------------------------------------------------------------------


def wallet_refusal(chain: Chain, wallet: str | None, *, kaiba_wallet: str | None,
                   owned: Iterable[str]) -> str | None:
    """Why this job may not manage ``wallet`` at all (dry or live), or None.

    ``kaiba_wallet`` is the chain wallet in ``config/risk.yaml``; unknown means we cannot
    prove the two differ, so it refuses. ``owned`` is signer-policy ``owned_addresses``
    for the chain (lowercased EVM), the operator's own list of what is his.
    """
    if chain is Chain.SOL:
        return "chain_unsupported: copy_manager lowercases token keys (EVM only)"
    w = (wallet or "").strip().lower()
    if not w:
        return "wallet_missing: copy_manager never defaults to Kaiba's wallet; name one"
    if not looks_evm(w):
        return "wallet_invalid"
    if not kaiba_wallet:
        return "kaiba_wallet_unknown: cannot prove the copy wallet is not Kaiba's"
    if w == kaiba_wallet.strip().lower():
        return "wallet_is_kaibas: copy_manager must never manage Kaiba's own wallet"
    if w not in {str(a).strip().lower() for a in owned}:
        return "wallet_not_owned: not in signer-policy owned_addresses"
    return None


@dataclass(frozen=True)
class Binding:
    """What ``gmgn-cli portfolio info`` says the API key may trade from.

    MEASURED shape 2026-10-03: ``{"wallets": [{"chain", "address", "balances"}, ...]}`` --
    a flat list of (chain, address) rows, one per chain today. ``bound`` is None when the
    read failed: unknown is never treated as bound.
    """

    bound: bool | None
    kaiba_bound: bool | None
    wallets_on_chain: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"bound": self.bound, "kaiba_bound": self.kaiba_bound,
                "wallets_on_chain": list(self.wallets_on_chain), "detail": self.detail}


def parse_binding(payload: Any, chain: Chain, wallet: str, kaiba_wallet: str | None) -> Binding:
    rows: Any = payload
    if isinstance(rows, Mapping) and isinstance(rows.get("data"), Mapping):
        rows = rows["data"]
    if isinstance(rows, Mapping):
        rows = rows.get("wallets")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return Binding(None, None, detail="portfolio info: no wallets list")
    on_chain = tuple(sorted({
        str(r.get("address") or "").strip().lower() for r in rows
        if isinstance(r, Mapping) and str(r.get("chain") or "") == chain.value and r.get("address")
    }))
    kaiba = (kaiba_wallet or "").strip().lower()
    return Binding(
        bound=wallet.strip().lower() in on_chain,
        kaiba_bound=(kaiba in on_chain) if kaiba else None,
        wallets_on_chain=on_chain,
    )


# --------------------------------------------------------------------------------------
# durable state
# --------------------------------------------------------------------------------------


def kaiba_open_tokens(conn: sqlite3.Connection, chain: Chain) -> set[str]:
    return {str(r["token"]).lower() for r in fetch_all(
        conn, "SELECT token FROM positions WHERE chain=? AND closed_ms IS NULL", (chain.value,))}


def kaiba_migrated_ms(conn: sqlite3.Connection, chain: Chain, token: str) -> int | None:
    """Kaiba's own graduation record for the token, if any (``tokens.migrated_ms``)."""
    row = fetch_one(conn, "SELECT migrated_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token))
    return int(row["migrated_ms"]) if row and row["migrated_ms"] else None


def _state_key(chain: Chain, token: str) -> str:
    return f"{STATE_PREFIX}{chain.value}:{token}"


def _load(conn: sqlite3.Connection, chain: Chain, token: str) -> TokenState | None:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (_state_key(chain, token),))
    return TokenState.load(jload(row["value"], {})) if row else None


def load_state(conn: sqlite3.Connection, chain: Chain, token: str) -> TokenState:
    return _load(conn, chain, token) or TokenState()


def save_state(conn: sqlite3.Connection, chain: Chain, token: str, state: TokenState, now: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (_state_key(chain, token), jdump(state.dump()), now),
    )


def _day_key(chain: Chain, ts: int) -> str:
    return f"{DAY_PREFIX}{chain.value}:{datetime.fromtimestamp(ts / 1000, tz=UTC):%Y-%m-%d}"


def sells_today(conn: sqlite3.Connection, chain: Chain, ts: int) -> int | None:
    """Live sends counted today (UTC). None when the counter is unreadable -- callers fail closed."""
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (_day_key(chain, ts),))
    if not row:
        return 0
    try:
        return int(str(row["value"]).strip().strip('"'))
    except ValueError:
        return None


def _count_send(conn: sqlite3.Connection, chain: Chain, ts: int, delta: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "value=CAST(MAX(CAST(value AS INTEGER) + ?, 0) AS TEXT), updated_ms=excluded.updated_ms",
        (_day_key(chain, ts), str(max(delta, 0)), ts, delta),
    )


def open_sell_orders(conn: sqlite3.Connection, chain: Chain, token: str) -> list[str]:
    """Unresolved sells of this token from ANY lane. ``idx_orders_token`` serves it."""
    marks = ",".join("?" for _ in UNRESOLVED_STATES)
    rows = fetch_all(
        conn,
        f"SELECT order_id FROM orders WHERE chain=? AND token=? AND side=? AND state IN ({marks})",
        (chain.value, token, Side.SELL.value, *UNRESOLVED_STATES),
    )
    return [str(r["order_id"]) for r in rows]


def settle_pending(conn: sqlite3.Connection, state: TokenState) -> str | None:
    """Fold our last send's outcome into the state. Returns why the token must wait, or None.

    ``executor.reconcile`` is the only thing that moves an order out of UNKNOWN; until it
    does, the token waits. A send that ended failed/expired/cancelled sold nothing, so what
    the send assumed is undone -- a closed cycle re-opens, a trimmed rung is un-done -- and
    the refusal counts toward the backoff.
    """
    if not state.pending_id:
        return None
    row = fetch_one(conn, "SELECT state FROM orders WHERE order_id=?", (state.pending_id,))
    if row is None:
        if state.pending_ambiguous:
            return "ambiguous_unrecorded"
        _clear_pending(state)  # the executor never persisted it: nothing was sent
        return None
    st = str(row["state"])
    if st in UNRESOLVED_STATES:
        return "in_flight"
    if st == OrderState.FILLED.value:
        state.failures = 0
    else:
        state.failures += 1
        if state.pending_cycle_ms == state.cycle_ms:
            if state.pending_rung is not None and state.pending_rung in state.rungs_done:
                state.rungs_done.remove(state.pending_rung)
            if state.closed and state.closed_kind == state.pending_kind:
                state.closed, state.closed_kind, state.closed_ms = False, "", 0
    _clear_pending(state)
    return None


def _clear_pending(state: TokenState) -> None:
    state.pending_id, state.pending_kind, state.pending_rung = None, "", None
    state.pending_ambiguous, state.pending_cycle_ms = False, 0


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


# --------------------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------------------


class SellRefused(Exception):
    """Nothing was sent (the executor's ExecutionRefused / a limiter refusal). Retry is safe -- later."""

    def __init__(self, message: str, order_id: str | None = None) -> None:
        super().__init__(message)
        self.order_id = order_id


class SellAmbiguous(Exception):
    """The sell may be live. Wait for ``orders`` to resolve it; never re-send it blind.

    ``blind``: the executor raised ExecutionAmbiguous, so even a missing order row proves
    nothing. False for an unclassified error, where the executor's own row is the truth.
    """

    def __init__(self, message: str, order_id: str | None = None, *, blind: bool = True) -> None:
        super().__init__(message)
        self.order_id = order_id
        self.blind = blind


@dataclass
class RunReport:
    live: bool
    holdings: int = 0
    managed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    held: dict[str, int] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    sells: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    sells_today: int = 0
    wallet: str | None = None
    #: Rug rules that fired this pass, sent or not: ``{token, symbol, kind, mode, action}``.
    rules: list[dict[str, Any]] = field(default_factory=list)
    #: Why the job ran dry although configured live (``wallet_not_api_bound`` ...).
    blocked: list[str] = field(default_factory=list)
    binding: dict[str, Any] | None = None

    def hold(self, why: str) -> None:
        self.held[why] = self.held.get(why, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {"live": self.live, "wallet": self.wallet, "blocked": self.blocked,
                "binding": self.binding, "holdings": self.holdings, "managed": self.managed,
                "skipped": self.skipped, "held": self.held, "attempts": self.attempts,
                "sells_today": self.sells_today, "decisions": self.decisions[:10],
                "rules": self.rules[:10], "sells": self.sells[:10], "errors": self.errors[:5]}


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
    wallet: str | None = None,
    blocked: Iterable[str] = (),
    binding: Mapping[str, Any] | None = None,
    migrated_ms: Callable[[str], int | None] | None = None,
) -> RunReport:
    """One pass. Every side effect is injected so the policy is testable without a venue.

    ``wallet``, ``blocked`` and ``binding`` are reported, not acted on: the job resolves the
    wallet, and a wallet that is not API-bound reaches here as ``cfg.live == False``.
    ``migrated_ms(token)`` is Kaiba's graduation record (default: ``tokens.migrated_ms``).
    """
    ts = now if now is not None else now_ms()
    report = RunReport(live=cfg.live, wallet=wallet, blocked=list(blocked),
                       binding=dict(binding) if binding is not None else None)
    holdings = parse_holdings(fetch_holdings())
    report.holdings = len(holdings)
    ours = kaiba_open_tokens(conn, chain)
    counted = sells_today(conn, chain, ts)
    if counted is None:
        report.errors.append(f"{_day_key(chain, ts)} unreadable; no sells this pass")
    report.sells_today = counted if counted is not None else 0
    lookup_migrated = migrated_ms or (lambda token: kaiba_migrated_ms(conn, chain, token))
    for h in holdings:
        why = excluded(h, ours, chain, cfg)
        rug_only = False
        prior: TokenState | None = None
        if why:
            report.skipped[why] = report.skipped.get(why, 0) + 1
            if why in ("dust", "honeypot", "thin_pool"):
                prior = _load(conn, chain, h.token)
            if why == "dust":
                # A sold-out token shows here at ~0. Seeing it empty is what lets a later
                # re-buy register as a new cycle when GMGN gives no buy count.
                if prior is not None:
                    save_state(conn, chain, h.token, observe(prior, h, ts, cfg), ts)
                continue
            if why == "honeypot":
                # Never sold (a honeypot cannot be), but a flag that TURNED ON while held is
                # recorded: it is the risk_flags rule's evidence.
                if prior is not None and not prior.closed and cfg.risk_flags != "off":
                    state = observe(prior, h, ts, cfg)
                    if state.flag_reason == "honeypot":
                        d = Decision("risk_flags", Decimal(1), "GMGN honeypot turned on while held")
                        report.rules.append({"token": h.token, "symbol": h.symbol, "kind": d.kind,
                                             "mode": cfg.risk_flags, "action": "unsellable", "reason": d.reason})
                        _record_rule(conn, chain, h, d, state, ts, cfg.risk_flags, "unsellable")
                    save_state(conn, chain, h.token, state, ts)
                continue
            if not (why == "thin_pool" and prior is not None and prior.liqs and not prior.closed
                    and cfg.any_rug_rule()):
                continue
            # Managed while its pool was above the floor: a collapse BELOW the floor is the
            # rug these rules exist for, so they still look. The policy rules do not.
            rug_only = True
        else:
            report.managed += 1
        state = observe(prior if rug_only and prior is not None else load_state(conn, chain, h.token), h, ts, cfg)
        wait = settle_pending(conn, state)
        if wait:
            report.hold(wait)
            if wait == "ambiguous_unrecorded":
                report.errors.append(f"{h.symbol}: ambiguous sell {state.pending_id} has no order row; "
                                     f"reconcile by hand before this token can sell again")
            save_state(conn, chain, h.token, state, ts)
            continue
        rugs = rug_decisions(h, state, ts, cfg, lambda token=h.token: lookup_migrated(token))
        # Only a rule whose mode is ``live`` may become the sell; a dry one is recorded below
        # and must never mask a live giveback.
        d = next((r for r in rugs if cfg.mode_of(r.kind) == "live"), None)
        if d is None and not rug_only:
            d = decide(h, state, ts, cfg)
        entry: dict[str, Any] | None = None
        if d is not None:
            entry = {"token": h.token, "symbol": h.symbol, "kind": d.kind, "fraction": str(d.fraction),
                     "pnl": str(h.pnl), "peak_pnl": str(state.peak_pnl), "reason": d.reason}
            report.decisions.append(entry)
            if not cfg.live:
                entry["action"] = "dry_run"
            elif counted is None:
                entry["action"] = "held:day_count_unreadable"
            elif report.attempts >= cfg.max_sells_per_run:
                entry["action"] = "held:run_cap"
            elif report.sells_today >= cfg.max_sells_per_day:
                entry["action"] = "held:daily_cap"
            elif open_sell_orders(conn, chain, h.token):
                entry["action"] = "held:in_flight"
            else:
                entry["action"] = _sell(conn, chain, h, d, state, ts, wallet_units, native_usd,
                                        submit, slippage_bps, report)
            if entry["action"].startswith("held:"):
                report.hold(entry["action"][5:])
        for r in rugs:
            mode = cfg.mode_of(r.kind)
            if r is d and entry is not None:
                action = str(entry["action"])
            else:
                action = "dry_rule" if mode == "dry" else "superseded"
            report.rules.append({"token": h.token, "symbol": h.symbol, "kind": r.kind, "mode": mode,
                                 "action": action, "reason": r.reason})
            if action not in SENT_OUTCOMES:  # a send writes its own copy_manager_sell record
                _record_rule(conn, chain, h, r, state, ts, mode, action)
        save_state(conn, chain, h.token, state, ts)
    return report


#: ``_sell`` outcomes after which something may have left: each wrote a ``copy_manager_sell``.
SENT_OUTCOMES = frozenset({"submitted", "refused", "ambiguous", "error"})


def _record_rule(conn: sqlite3.Connection, chain: Chain, h: Holding, d: Decision, state: TokenState,
                 ts: int, mode: str, why_not: str) -> None:
    """One durable record per cycle per rule that fired and was not sent: the forward evidence.

    Carries what a later study needs to score it against the tape: price, liquidity and P&L
    at the decision, the graduation timestamps, and the cycle. Deduplicated in the state AND
    by ``dedupe_key``, so neither a lost state row nor a dry pass every 30 s can repeat it.
    """
    if d.kind in state.logged:
        return
    state.logged.append(d.kind)
    ev.emit(
        EventKind.SYSTEM,
        {"service": SERVICE, "action": RULE_ACTION, "kind": d.kind, "mode": mode, "sent": False,
         "why_not": why_not, "chain": chain.value, "token": h.token, "symbol": h.symbol,
         "reason": d.reason, "pnl": _s(h.pnl), "price_usd": _s(h.price_usd),
         "liquidity_usd": _s(h.liquidity_usd), "value_usd": _s(h.value_usd), "cost_usd": _s(h.cost_usd),
         "peak_pnl": _s(state.peak_pnl), "alert": h.alert, "open_s": h.open_s, "created_s": h.created_s,
         "started_s": h.started_s, "cycle_ms": state.cycle_ms, "ts_ms": ts},
        chain=chain, subject=h.token, level="info",
        dedupe_key=f"{RULE_ACTION}:{chain.value}:{h.token}:{state.cycle_ms}:{d.kind}", conn=conn,
    )


def _sell(conn: sqlite3.Connection, chain: Chain, h: Holding, d: Decision, state: TokenState, ts: int,
          wallet_units: Callable[[str, int], int | None], native_usd: Callable[[], Decimal | None],
          submit: Callable[[str, int, int], dict[str, Any]], slippage_bps: int, report: RunReport) -> str:
    """Send one sell and fold the outcome into ``state``. Returns the outcome word."""
    held = wallet_units(h.token, h.decimals)
    if not held or held <= 0:
        report.errors.append(f"{h.symbol}: wallet holds none")
        state.last_action_ms = ts  # nothing sent; the cooldown keeps this from repeating every pass
        return "skipped:wallet_empty"
    qty = held if d.fraction >= 1 else int(Decimal(held) * d.fraction)
    if qty <= 0:
        return "skipped:qty_zero"
    usd = native_usd()
    floor = min_out_units(qty, h.decimals, h.price_usd or Decimal(0), usd or Decimal(0), chain, slippage_bps)
    if floor is None:
        report.errors.append(f"{h.symbol}: cannot price min_out; not sending min_out=0")
        state.last_action_ms = ts
        return "skipped:no_min_out"

    # Counted BEFORE the send: a crash mid-send must still count against the day.
    _count_send(conn, chain, ts, +1)
    report.sells_today += 1
    report.attempts += 1
    peak_at_decision = state.peak_pnl
    order_id: str | None = None
    order_state: str | None = None
    error: str | None = None
    state.last_action_ms = ts
    try:
        result = submit(h.token, qty, floor)
    except SellRefused as exc:
        _count_send(conn, chain, ts, -1)  # nothing left the building
        report.sells_today -= 1
        state.failures += 1
        outcome, order_id, error = "refused", exc.order_id, str(exc)
    except SellAmbiguous as exc:
        outcome, order_id, error = "ambiguous", exc.order_id, str(exc)
        _mark_sent(state, d, order_id, ts, ambiguous=exc.blind)
    except Exception as exc:  # noqa: BLE001 - one bad sell must not stop the pass
        # Unclassified and without an order id: count it, back off, and let the
        # orders-table check stand guard over anything it may have left behind.
        state.failures += 1
        outcome, error = "error", f"{type(exc).__name__}: {exc}"
    else:
        order_id = str(result.get("order_id")) if result.get("order_id") else None
        order_state = str(result.get("state")) if result.get("state") is not None else None
        outcome = "submitted"
        state.failures = 0
        _mark_sent(state, d, order_id, ts, ambiguous=False)
        report.sells.append({"token": h.token, "symbol": h.symbol, "kind": d.kind, "qty": str(qty),
                             "min_out": str(floor), **result})
    if error:
        report.errors.append(f"{h.symbol}: {outcome}: {error}"[:200])

    ev.emit(
        EventKind.SYSTEM,
        {"service": SERVICE, "action": SELL_ACTION, "outcome": outcome, "chain": chain.value,
         "token": h.token, "symbol": h.symbol, "kind": d.kind, "rung": d.rung,
         "fraction": str(d.fraction), "reason": d.reason,
         "pnl": _s(h.pnl), "price_usd": _s(h.price_usd), "peak_pnl": _s(peak_at_decision),
         "value_usd": _s(h.value_usd), "liquidity_usd": _s(h.liquidity_usd), "cost_usd": _s(h.cost_usd),
         "qty": str(qty), "decimals": h.decimals, "min_out": str(floor), "native_usd": _s(usd),
         "slippage_bps": slippage_bps, "order_id": order_id, "order_state": order_state,
         "error": error[:300] if error else None, "started_s": h.started_s,
         "cycle_ms": state.cycle_ms, "ts_ms": ts},
        chain=chain, subject=h.token, level="info" if outcome == "submitted" else "warn",
        dedupe_key=f"{SELL_ACTION}:{order_id}:{outcome}" if order_id else None, conn=conn,
    )
    return outcome


def _mark_sent(state: TokenState, d: Decision, order_id: str | None, ts: int, *, ambiguous: bool) -> None:
    """The sell may be live: advance the cycle as if it filled; settle_pending undoes it if not."""
    state.pending_id = order_id
    state.pending_kind = d.kind
    state.pending_rung = d.rung
    state.pending_ambiguous = ambiguous
    state.pending_cycle_ms = state.cycle_ms
    if d.rung is not None and d.rung not in state.rungs_done:
        state.rungs_done.append(d.rung)
    if d.fraction >= 1:
        state.closed, state.closed_kind, state.closed_ms = True, d.kind, ts


def gmgn_submitter(conn: sqlite3.Connection, chain: Chain, slippage_bps: int, *,
                   wallet: str) -> Callable[[str, int, int], dict[str, Any]]:
    """The live seam: build and submit a SELL from ``wallet`` exactly as a watchdog exit does.

    ``wallet`` is required, with no default: this module never sells from Kaiba's wallet by
    omission. It reaches GMGN as ``--from`` through the executor's ``from_wallet`` override,
    which itself refuses anything but a Lane.MANUAL SELL from an owned wallet.

    Translates the executor's outcomes into this module's two: ExecutionRefused and a
    limiter refusal mean nothing was sent (:class:`SellRefused`); ExecutionAmbiguous means
    it may be live and is now UNKNOWN in ``orders`` (:class:`SellAmbiguous`). Anything else
    is ambiguous too, but judged by the executor's own order row.
    """
    from kaiba.core.limiter import RateLimited
    from kaiba.execution import executor

    if not (wallet or "").strip():
        raise ValueError("copy_manager.gmgn_submitter: a wallet is required")

    def _submit(token: str, qty: int, min_out: int) -> dict[str, Any]:
        order = executor.build_order(
            decision_id=None, chain=chain, token=token, side=Side.SELL, lane=Lane.MANUAL,
            mode=LaneMode.LIVE, amount_in=qty, min_out=min_out, slippage_bps=slippage_bps,
        )
        if order.side is not Side.SELL:  # structural: this module has no buy to send
            raise SellRefused("copy_manager only sells", order.order_id)
        try:
            res = executor.submit(order, conn, from_wallet=wallet)
        except executor.ExecutionAmbiguous as exc:
            raise SellAmbiguous(str(exc), order.order_id, blind=True) from exc
        except (executor.ExecutionRefused, RateLimited) as exc:
            raise SellRefused(str(exc), order.order_id) from exc
        except Exception as exc:  # noqa: BLE001 - classified by the order row, see settle_pending
            raise SellAmbiguous(f"{type(exc).__name__}: {exc}", order.order_id, blind=False) from exc
        return {"order_id": res.order_id, "state": getattr(res.state, "value", str(res.state))}

    return _submit


def summarize(decisions: Iterable[Mapping[str, Any]]) -> str:
    return "; ".join(f"{d['symbol']} {d['kind']} {d['fraction']} ({d['reason']})" for d in decisions)
