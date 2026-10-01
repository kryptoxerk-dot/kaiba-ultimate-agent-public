"""Exit logic: when to trim, when to run, and why.

Pure decision logic. No network, no database, no clock of its own — feed it a price and a
liquidity reading and it returns an action. The watchdog that polls prices and the executor
that sends the sell live elsewhere; keeping the decision pure is what makes the ladder, the
ratchet and the anti-wick rule testable without a market.

The precedence is deliberate and is the order of the checks below:

1. **Rug monitor.** A single-interval liquidity collapse beats everything. If the pool is
   leaving, the price you can see is not a price you can get.
2. **Emergency loss.** A drop past ``emergency_loss_bps`` exits whatever the ladder thinks.
3. **Stop.** Before TP1 that is the hard ``stop_loss_bps`` stop; after TP1 it is whatever the
   ratchet has raised it to. The stop only ever rises.
4. **Take-profit ladder.** One rung per call, lowest unfired first, each rung recorded in
   ``tp_done`` so it can never fire twice.
5. **Anti-wick.** A TP rung is held, not sold, when an executable quote says the chart price
   is a wick: ``executable_quote_usd / price_usd < anti_wick_min_ratio``. The chart is not a
   fill, and selling into a wick is how a 3x becomes a 0.7x.
6. **Ratchet.** Breakeven lock after TP1, then the trailing tier table, applied to the peak.

Protection also lives server-side: :func:`to_gmgn_condition_orders` renders the same state
as GMGN ``--condition-orders`` so the ladder survives our process dying
(``docs/PLAN.md`` §6.3).

:func:`arm` is the single exception to the purity above: it writes the opening stop onto a
freshly filled position's row. It lives here rather than in the ledger because the stop it
writes is this module's arithmetic, and it keeps its database access local to itself.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from kaiba.core.config import get_risk
from kaiba.core.schemas import now_ms as _now_ms

if TYPE_CHECKING:  # the runtime re-export is lazy; see __getattr__ at the bottom
    from kaiba.execution.watchdog import run_watchdog as run_watchdog

Number = Decimal | float | int | str


def _dec(value: Number | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _bps(bps: int | float) -> Decimal:
    return Decimal(str(bps)) / Decimal(10000)


class ProtectionKind(StrEnum):
    HOLD = "hold"
    TRIM = "trim"
    EXIT_ALL = "exit_all"


class ProtectionAction(BaseModel):
    """What to do right now, and the one-word reason that goes in the journal."""

    kind: ProtectionKind
    reason: str
    pct: Decimal | None = None  # portion of the remaining position to sell, for TRIM
    tp_tag: str | None = None
    stop_price: Decimal | None = None

    @property
    def sells(self) -> bool:
        return self.kind is not ProtectionKind.HOLD

    @classmethod
    def hold(cls, reason: str, stop_price: Decimal | None = None) -> ProtectionAction:
        return cls(kind=ProtectionKind.HOLD, reason=reason, stop_price=stop_price)

    @classmethod
    def trim(cls, pct: Number, reason: str, tag: str, stop_price: Decimal | None = None) -> ProtectionAction:
        return cls(kind=ProtectionKind.TRIM, reason=reason, pct=_dec(pct), tp_tag=tag, stop_price=stop_price)

    @classmethod
    def exit_all(cls, reason: str) -> ProtectionAction:
        return cls(kind=ProtectionKind.EXIT_ALL, reason=reason, pct=Decimal(100))


class ProtectionConfig(BaseModel):
    """The ``protection:`` block of ``config/risk.yaml``, with the shipped defaults."""

    poll_interval_s: int = 5
    use_provider_orders: bool = True
    stop_loss_bps: int = 3000
    tp_ladder: list[tuple[Decimal, Decimal]] = Field(
        default_factory=lambda: [
            (Decimal("2.0"), Decimal(50)),
            (Decimal("5.0"), Decimal(25)),
            (Decimal("10.0"), Decimal(15)),
        ]
    )
    trailing: list[tuple[Decimal, int]] = Field(
        default_factory=lambda: [
            (Decimal("2.0"), 3000),
            (Decimal("5.0"), 2500),
            (Decimal("10.0"), 2000),
            (Decimal("25.0"), 1500),
            (Decimal("100.0"), 1000),
        ]
    )
    breakeven_after_tp1: bool = True
    anti_wick_min_ratio: Decimal = Decimal("0.7")
    rug_liquidity_drop_pct: Decimal = Decimal(40)
    #: Seconds after a known migration during which a liquidity DROP is not a rug.
    #:
    #: A launchpad token that graduates has its bonding curve drained to seed the DEX pool.
    #: That is the design, and on any liquidity feed it looks exactly like a rug. MEASURED
    #: on the live box 2026-09-23: 33 of 45 rug-reason exits were on tokens carrying a
    #: migration record, and the giveaway is what we were paid for the sale -- genuine rugs
    #: exited at -70% to -98%, migrations at +1% to +51%. A drained pool cannot fill a sell
    #: at +43%. $DOAI exited on ``rug:lp_-100.0pct`` for +42.7% and then ran to 7.87x.
    #:
    #: Holding through it is safe because the price rules are untouched: a token that really
    #: dies after bonding collapses, and the trailing stop, the hard stop and the emergency
    #: exit all fire on PRICE, which a drained pool cannot fake.
    #:
    #: 1800 is INVENTED as a window wide enough to cover a slow migration and its first
    #: minutes on the new venue. Zero disables the waiver and restores the old behaviour.
    rug_migration_grace_s: int = 1800
    #: Sell a position whose token has not traded for this many seconds. 0 disables it.
    #:
    #: MEASURED 2026-09-23 over 135 live-traded tokens, what the NEXT print does after a
    #: silence and how often there is never one:
    #:
    #:     silence   next print up   median move   never trades again
    #:       15m          44%           -0.4%            93%
    #:       60m          42%           -3.1%            88%
    #:      120m          38%           -6.0%            80%
    #:      240m          35%          -11.0%            70%
    #:
    #: Both columns move together: longer silence, worse resumption, less likely there is
    #: one. At an hour the median resumption is -3.1% and 88% of tokens are already dead.
    #:
    #: The tail is given up ON PURPOSE. The MEAN at 60m is +1.7% against that -3.1%
    #: median, so a few silent tokens do rip -- but 88% never print again, and a position
    #: we can neither price nor sell is an unrecorded loss rather than a lottery ticket.
    stale_no_volume_exit_s: int = 3600
    #: Percent of the position to KEEP when a trailing stop fires while in profit.
    #:
    #: OWNER 2026-09-23: "please do not exit fully if we are profitable like leave moon
    #: bag especially up trend". MEASURED on our own closed positions, the token's BEST
    #: price after a profitable trailing-stop exit (n=51-53):
    #:
    #:     after  1h   median 1.29x   higher than our exit 69% of the time   2x+ 18%
    #:     after  6h   median 1.32x   higher 71%                             2x+ 19%
    #:     after 24h   median 1.29x   higher 74%                             2x+ 19%
    #:
    #: Seven times in ten the thing we just sold traded at least 29% higher. The
    #: take-profit ladder already retains ~32%; the trailing stop was the full exit and it
    #: closes most winners.
    #:
    #: The bag is NOT unprotected: it keeps the ratcheted trailing stop, the rungs above
    #: it, `stale_no_volume` and the emergency exit. The only change is that one
    #: trailing-stop hit, in profit, trims instead of closing. 0 disables it.
    moonbag_retain_pct: int = 20
    #: The leash the moon bag rides on, once it has been taken.
    #:
    #: WITHOUT THIS THE BAG DOES NOT EXIST. The trim fires AT the trailing stop, and the
    #: ratchet is monotonic, so the stop stays where it just fired -- at or above the
    #: current price. The next tick therefore satisfies `price <= stop_price` and sells
    #: the bag. MEASURED 2026-09-24 on all 12 live moon bags taken since the protection
    #: fix: median time held after the trim was ONE MINUTE, four were sold on the very
    #: next tick, and one of them had moved +51.5% by the time it was sold.
    #:
    #: A bag is the part deliberately left to run, so it gets the widest trail we allow
    #: rather than the tier its peak unlocked. It is not unprotected: `breakeven_after_tp1`
    #: floors it at entry (the bag cannot go red), and the emergency exit, the rug monitor
    #: and `stale_no_volume` all still apply.
    moonbag_trail_bps: int = 5000
    emergency_loss_bps: int = 5000

    @field_validator("tp_ladder", "trailing", mode="before")
    @classmethod
    def _pairs(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [tuple(item) if isinstance(item, (list, tuple)) else item for item in v]
        return v


def protection_config(raw: dict[str, Any] | None = None) -> ProtectionConfig:
    """Read ``protection:`` fresh, like the rest of the risk file."""
    block = raw if raw is not None else (get_risk().protection or {})
    return ProtectionConfig.model_validate(block)


#: Marker in ``tp_done`` recording that the moon bag has already been taken. Without it a
#: falling price trims 80% of 20% of 20% ... until the position is dust that cannot be sold
#: economically. Taken ONCE; the bag then rides on `moonbag_trail_bps` until a stop,
#: the emergency exit, the rug monitor or `stale_no_volume` closes it.
MOONBAG_TAG = "moonbag"


class ProtectionState(BaseModel):
    """Everything the exit logic remembers about one position.

    Mirrors the ``positions`` columns (``peak_price_usd``, ``stop_price_usd``,
    ``tp_done_json``) so the watchdog can rehydrate it after a restart and carry on with the
    same ladder rather than re-arming rungs that already fired.
    """

    position_id: str
    entry_price: Decimal
    peak_price: Decimal = Decimal(0)
    stop_price: Decimal | None = None
    tp_done: list[str] = Field(default_factory=list)
    activated_trail_bps: int | None = None

    @field_validator("entry_price", "peak_price", "stop_price", mode="before")
    @classmethod
    def _to_decimal(cls, v: Any) -> Any:
        return _dec(v)

    def model_post_init(self, _ctx: Any) -> None:
        if self.peak_price <= 0:
            self.peak_price = self.entry_price


def _trail_bps_for(state: ProtectionState, cfg: ProtectionConfig) -> int | None:
    """Highest trailing tier the peak has unlocked. Tiers tighten as the multiple grows."""
    if state.entry_price <= 0:
        return None
    multiple = state.peak_price / state.entry_price
    unlocked = [bps for mult, bps in sorted(cfg.trailing, key=lambda t: t[0]) if multiple >= mult]
    if not unlocked:
        return None
    if MOONBAG_TAG in state.tp_done:
        # The bag rides on its own leash. `max` because this must never be TIGHTER than
        # the tier the peak already unlocked -- a 25x bag keeps its 1500bps trail.
        return max(unlocked[-1], int(getattr(cfg, "moonbag_trail_bps", 0) or 0))
    return unlocked[-1]


def next_stop(state: ProtectionState, price: Number, cfg: ProtectionConfig | None = None) -> Decimal:
    """The stop this position should carry at ``price``. Monotonic: it never comes down.

    Pure — it does not touch ``state``. :func:`evaluate` calls it and then commits the
    result; the watchdog can call it to preview a ratchet without advancing anything.
    """
    cfg = cfg or protection_config()
    p = _dec(price) or Decimal(0)
    peak = max(state.peak_price, p)
    candidates: list[Decimal] = []
    if state.stop_price is not None:
        candidates.append(state.stop_price)
    if not state.tp_done:
        candidates.append(state.entry_price * (Decimal(1) - _bps(cfg.stop_loss_bps)))
    if state.tp_done and cfg.breakeven_after_tp1:
        candidates.append(state.entry_price)
    probe = state.model_copy(update={"peak_price": peak})
    bps = _trail_bps_for(probe, cfg)
    if bps is not None:
        candidates.append(peak * (Decimal(1) - _bps(bps)))
    return max(candidates) if candidates else Decimal(0)


def _commit_ratchet(state: ProtectionState, price: Decimal, cfg: ProtectionConfig) -> Decimal:
    state.peak_price = max(state.peak_price, price)
    new_stop = next_stop(state, price, cfg)
    if state.stop_price is None or new_stop > state.stop_price:
        state.stop_price = new_stop
    bps = _trail_bps_for(state, cfg)
    if bps is not None:
        state.activated_trail_bps = bps
    return state.stop_price


def _migrating(migrated_ms: int | None, now_ms: int | None, cfg: ProtectionConfig) -> bool:
    """Is this token inside the grace window after a KNOWN migration?

    A future timestamp is refused. Clock skew or a bad feed value would otherwise turn one
    bad row into a permanent rug waiver for that token, which is the most expensive way
    this could fail.
    """
    grace = int(getattr(cfg, "rug_migration_grace_s", 0) or 0)
    if grace <= 0 or migrated_ms is None:
        return False
    now = now_ms if now_ms is not None else _now_ms()
    age_ms = now - int(migrated_ms)
    return 0 <= age_ms <= grace * 1000


def evaluate(
    state: ProtectionState,
    *,
    price_usd: Number | None,
    liquidity_usd: Number | None = None,
    prev_liquidity_usd: Number | None = None,
    executable_quote_usd: Number | None = None,
    migrated_ms: int | None = None,
    last_trade_ms: int | None = None,
    now_ms: int | None = None,
    cfg: ProtectionConfig | None = None,
) -> ProtectionAction:
    """Decide the next protective action and advance ``state``.

    ``executable_quote_usd`` is the **per-unit price an actual sell quote would fill at**,
    not a notional. When it is supplied and falls below ``anti_wick_min_ratio`` of the chart
    price, a take-profit rung is held instead of sold.

    ``state`` is advanced in place: the peak, the stop and ``tp_done`` move forward so the
    caller can persist one object and so a rung cannot fire twice. Nothing outside the state
    is touched.
    """
    cfg = cfg or protection_config()
    price = _dec(price_usd)
    if price is None or price <= 0:
        # Missing price is not a sell signal. The watchdog escalates on staleness instead.
        return ProtectionAction.hold("price_unavailable", state.stop_price)
    entry = state.entry_price
    if entry <= 0:
        return ProtectionAction.hold("entry_price_unknown", state.stop_price)

    # 1. rug monitor
    migrating = False
    liq, prev = _dec(liquidity_usd), _dec(prev_liquidity_usd)
    if liq is not None and prev is not None and prev > 0:
        drop_pct = (prev - liq) * 100 / prev
        if drop_pct >= cfg.rug_liquidity_drop_pct:
            # A graduating launchpad token drains its curve BY DESIGN, and that reads
            # identically to a rug. Selling on it means selling at the most bullish moment
            # the token will have. See `rug_migration_grace_s` for the measurement.
            #
            # This SKIPS the rug exit; it does not return. Returning a hold here would
            # short-circuit the emergency exit and both stops below, so a token that
            # genuinely died while migrating could never be sold -- the most expensive way
            # this fix could fail, and the one its own safety tests caught.
            if _migrating(migrated_ms, now_ms, cfg):
                migrating = True
            else:
                return ProtectionAction.exit_all(
                    f"rug:lp_-{drop_pct.quantize(Decimal('0.1'))}pct"
                )

    # 2. emergency loss
    if price <= entry * (Decimal(1) - _bps(cfg.emergency_loss_bps)):
        return ProtectionAction.exit_all("emergency_loss")

    # 3. stop — the hard one before TP1, the ratcheted one after
    hard_stop = entry * (Decimal(1) - _bps(cfg.stop_loss_bps))
    if not state.tp_done and price <= hard_stop:
        return ProtectionAction.exit_all("stop_loss")
    if state.stop_price is not None and price <= state.stop_price:
        reason = "trailing_stop" if state.activated_trail_bps else "stop_loss"
        # Keep a moon bag when a TRAILING stop closes a position that is IN PROFIT.
        #
        # In profit only: the ratchet can set a stop from a peak above entry and catch the
        # price below it, which is a loss being cut, not a winner being trimmed. Once
        # only: see MOONBAG_TAG. Never on a hard stop, an emergency exit or a rug -- those
        # branches are above this one and return before reaching it.
        retain = int(getattr(cfg, "moonbag_retain_pct", 0) or 0)
        if (
            reason == "trailing_stop"
            and 0 < retain < 100
            and price > entry
            and MOONBAG_TAG not in state.tp_done
        ):
            state.tp_done.append(MOONBAG_TAG)
            # Release the stop that just fired before re-deriving it. The ratchet only
            # ever moves UP, so without this the bag keeps the very stop that triggered
            # this trim -- sitting at or above the current price -- and the next tick
            # sells it. This is the ONLY place the stop is allowed to come down, and it
            # can happen at most once per position because MOONBAG_TAG is latched on the
            # line above and checked in the branch condition.
            state.stop_price = None
            stop = _commit_ratchet(state, price, cfg)
            return ProtectionAction.trim(
                100 - retain, f"trailing_stop_moonbag:keep{retain}%", MOONBAG_TAG, stop
            )
        return ProtectionAction.exit_all(reason)

    # 4. take-profit ladder, lowest unfired rung only
    for index, (multiple, pct) in enumerate(cfg.tp_ladder):
        tag = f"tp{index + 1}"
        if tag in state.tp_done:
            continue
        if price < entry * multiple:
            break  # the ladder ascends; nothing above this rung can be live either
        # 5. anti-wick: the chart says 3x, the book says otherwise
        quote = _dec(executable_quote_usd)
        if quote is not None:
            ratio = quote / price
            if ratio < cfg.anti_wick_min_ratio:
                return ProtectionAction.hold(
                    f"anti_wick:{ratio.quantize(Decimal('0.001'))}", state.stop_price
                )
        state.tp_done.append(tag)
        stop = _commit_ratchet(state, price, cfg)
        return ProtectionAction.trim(pct, f"{tag}_at_{multiple}x", tag, stop)

    # 6. stale volume — the LAST rule, deliberately.
    #
    # Every price-based exit above and every take-profit rung is offered the tick first,
    # because each is better evidence than "nothing has happened lately". Silence only
    # decides the case where we would otherwise have done nothing at all.
    #
    # An UNKNOWN `last_trade_ms` never sells: not having looked is not the same as having
    # observed silence, and a future timestamp is clock skew, not activity.
    quiet_s = int(getattr(cfg, "stale_no_volume_exit_s", 0) or 0)
    if quiet_s > 0 and last_trade_ms is not None:
        now = now_ms if now_ms is not None else _now_ms()
        silent_ms = now - int(last_trade_ms)
        if silent_ms >= quiet_s * 1000:
            return ProtectionAction.exit_all(f"stale_no_volume:{silent_ms // 1000}s")

    # 7. ratchet and wait
    stop = _commit_ratchet(state, price, cfg)
    # Name the waiver in the reason so a held migration is visible in the tape rather than
    # looking like an ordinary quiet tick.
    return ProtectionAction.hold("migration_not_rug" if migrating else "hold", stop)


# --------------------------------------------------------------------------------------
# server-side protection
# --------------------------------------------------------------------------------------


def _s(value: Decimal) -> str:
    """Plain decimal string: no exponent, no trailing zero noise, no float."""
    text = format(value.normalize(), "f")
    return text


#: ``--sell-ratio-type`` the executor must pass alongside these orders. ``config/risk.yaml``
#: documents ``tp_ladder`` as "pct of remaining to sell", which is the ``hold_amount`` base;
#: GMGN's default is ``buy_amount``, so leaving this off silently rescales the whole ladder.
GMGN_SELL_RATIO_TYPE = "hold_amount"

#: Chains where GMGN refuses condition orders outright (gmgn-cli ``validate.js``).
NO_CONDITION_ORDER_CHAINS = frozenset({"arc", "stable"})


def to_gmgn_condition_orders(
    state: ProtectionState, cfg: ProtectionConfig | None = None, *, chain: str | None = None
) -> list[dict[str, Any]]:
    """Render the surviving ladder as GMGN ``--condition-orders`` JSON.

    Verified against gmgn-cli 1.6.1 itself — ``skills/gmgn-swap/SKILL.md`` §condition-orders
    and ``dist/commands/swap.js`` — not just our research digest. Two facts from that source
    drive the shape below:

    * Every element needs ``order_type`` and ``side: "sell"``. ``price_scale``,
      ``sell_ratio`` and ``drawdown_rate`` are **percentages as strings**, not fractions:
      ``"100"`` is +100% (2x) for a ``profit_stop``, and ``"65"`` is a 65% *drop* for a
      ``loss_stop`` — the drop is a positive magnitude, not a negative number.
    * The CLI does no schema validation. It ``JSON.parse``s this array and forwards it. A
      wrong key name does not error; it produces a position with no server-side protection,
      which is the exact failure this function exists to prevent.

    A ratcheted stop that has risen *above* entry cannot be written as a ``loss_stop``,
    because that order type only expresses a drop from entry. We emit the breakeven floor
    (``"0"``) and let ``profit_stop_trace`` carry the upside trail, which is what that order
    type is for.
    """
    cfg = cfg or protection_config()
    if chain is not None and chain in NO_CONDITION_ORDER_CHAINS:
        # Better an empty list the caller must notice than orders GMGN will reject.
        return []
    orders: list[dict[str, Any]] = []

    # Rungs that have not fired yet still need to exist server-side.
    for index, (multiple, pct) in enumerate(cfg.tp_ladder):
        tag = f"tp{index + 1}"
        if tag in state.tp_done:
            continue
        orders.append(
            {
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": _s((Decimal(multiple) - Decimal(1)) * Decimal(100)),
                "sell_ratio": _s(Decimal(pct)),
            }
        )

    if state.stop_price is not None and state.entry_price > 0:
        drop = (state.entry_price - state.stop_price) * Decimal(100) / state.entry_price
        drop = max(drop, Decimal(0))
    else:
        drop = _bps(cfg.stop_loss_bps) * Decimal(100)
    orders.append(
        {"order_type": "loss_stop", "side": "sell", "price_scale": _s(drop), "sell_ratio": "100"}
    )

    bps = state.activated_trail_bps or (cfg.trailing[0][1] if cfg.trailing else None)
    if bps is not None:
        activation = cfg.trailing[0][0] if cfg.trailing else Decimal(2)
        orders.append(
            {
                "order_type": "profit_stop_trace",
                "side": "sell",
                "price_scale": _s((Decimal(activation) - Decimal(1)) * Decimal(100)),
                "drawdown_rate": _s(Decimal(bps) / Decimal(100)),
                "sell_ratio": "100",
            }
        )
    return orders


def initial_state(position_id: str, entry_price: Number, cfg: ProtectionConfig | None = None) -> ProtectionState:
    """A freshly opened position, already carrying its hard stop."""
    cfg = cfg or protection_config()
    entry = _dec(entry_price) or Decimal(0)
    return ProtectionState(
        position_id=position_id,
        entry_price=entry,
        peak_price=entry,
        stop_price=entry * (Decimal(1) - _bps(cfg.stop_loss_bps)),
    )


def arm(position_id: str, conn: Any = None) -> bool:
    """Write this position's opening protection onto its row. Never raises.

    The one function in this module that touches the database, and it is here because
    :mod:`kaiba.execution.engine` and :mod:`kaiba.execution.accounting` both import
    ``protection.arm`` by name the moment a fill lands. Until now that import failed and
    the engine silently took its ``log.debug`` fallback, so **every** paper fill was
    recorded as unprotected and the watchdog started each position from nothing.

    Arming is only the opening state: :func:`initial_state` computes the hard stop from
    ``entry_price_usd`` and the ratchet raises it from there. So this is deliberately
    monotonic — a second call can never lower a stop the watchdog has already raised,
    which is what makes it safe to call twice.

    Returns whether the position now carries a stop. It returns ``False`` rather than
    raising on every failure path, including a missing position and a missing entry price:
    a fill has already happened by the time we get here, and an exception thrown back into
    the caller would unwind bookkeeping for money that has already moved. A protection
    failure is an event and a warning; it is never the fill's problem.

    The imports are local so the decision logic above stays free of the database and the
    event bus, which is what lets the watchdog import this module for pure evaluation.
    """
    try:
        from kaiba.core.db import fetch_one, get_conn
        from kaiba.core.events import emit
        from kaiba.core.schemas import EventKind

        c = conn if conn is not None else get_conn()
        row = fetch_one(
            c,
            "SELECT entry_price_usd, stop_price_usd, peak_price_usd, chain, token, closed_ms "
            "FROM positions WHERE position_id=?",
            (position_id,),
        )
        if row is None:
            emit(
                EventKind.SYSTEM,
                {"event": "protection_arm_failed", "position_id": position_id,
                 "reason": "no such position"},
                level="warn", conn=c,
            )
            return False

        cfg = protection_config()
        entry = _dec(row["entry_price_usd"])
        existing = _dec(row["stop_price_usd"])
        if entry is None or entry <= 0:
            emit(
                EventKind.SYSTEM,
                {"event": "protection_arm_failed", "position_id": position_id,
                 "reason": "entry_price_unavailable",
                 "impact": "this position has no stop; the watchdog will run it blind"},
                chain=row["chain"], subject=row["token"], level="error", conn=c,
            )
            return False

        state = initial_state(position_id, entry, cfg)
        stop = state.stop_price or Decimal(0)
        if existing is not None and existing >= stop:
            # Already armed, and at least as tight. The ratchet never comes down.
            c.execute("UPDATE positions SET protected=1 WHERE position_id=?", (position_id,))
            return True

        peak = _dec(row["peak_price_usd"]) or entry
        c.execute(
            "UPDATE positions SET stop_price_usd=?, peak_price_usd=?, protected=1 WHERE position_id=?",
            (format(stop, "f"), format(max(peak, entry), "f"), position_id),
        )
        emit(
            EventKind.PROTECTION_SET,
            {
                "position_id": position_id,
                "entry_price_usd": format(entry, "f"),
                "stop_price_usd": format(stop, "f"),
                "stop_loss_bps": cfg.stop_loss_bps,
                "tp_ladder": [[str(m), str(p)] for m, p in cfg.tp_ladder],
            },
            chain=row["chain"], subject=row["token"], conn=c,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - a protection failure never unwinds a fill
        import logging

        logging.getLogger(__name__).warning("could not arm protection for %s: %s", position_id, exc)
        return False


def __getattr__(name: str) -> Any:
    """Re-export the watchdog service without importing it at module load.

    ``kaiba.cli.main`` does ``from kaiba.execution.protection import run_watchdog``, and
    :mod:`kaiba.execution.watchdog` imports this module for the decision logic. A plain
    top-level re-export would be a cycle that breaks whenever the watchdog is imported
    first; a module-level ``__getattr__`` (PEP 562) resolves it on first use instead.
    """
    if name == "run_watchdog":
        from kaiba.execution.watchdog import run_watchdog

        return run_watchdog
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GMGN_SELL_RATIO_TYPE",
    "NO_CONDITION_ORDER_CHAINS",
    "ProtectionAction",
    "ProtectionConfig",
    "ProtectionKind",
    "ProtectionState",
    "arm",
    "evaluate",
    "initial_state",
    "next_stop",
    "protection_config",
    "run_watchdog",
    "to_gmgn_condition_orders",
]
