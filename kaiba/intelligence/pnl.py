"""Our own PnL, rebuilt from observed swaps.

Every provider reports wallet PnL differently and none of them show their arithmetic:
GMGN's ``realized_profit`` moves when you ask twice, and none of them say what they did
with tokens that arrived by transfer rather than by purchase. A grade built on those
numbers is a grade built on a stranger's opinion, so this module replays the swaps we
actually observed and derives episodes ourselves.

Two rules carried over from the prior work:

* **Exact arithmetic only.** Base units are ``int``, USD is ``Decimal``, ratios go through
  a fixed :class:`~decimal.Context`. A float never touches money here.
* **Transferred inventory poisons a position.** If tokens entered or left a wallet without
  a price we can see, the cost basis is unknowable, so the episode is marked
  ``contaminated`` and dropped from every aggregate rather than quietly valued at zero.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.schemas import Chain, now_ms

#: Enough precision that a 1e18 wei cost divided into a ratio keeps every meaningful digit.
MATH = Context(prec=40, rounding=ROUND_HALF_EVEN)

ZERO = Decimal(0)

#: Leftover inventory under this share of everything bought counts as dust: token rounding,
#: a burnt remainder, a fee taken in kind. Expressed in basis points so the test is integer.
DUST_BPS = 10

#: "Actual winner" from SCORING-V2: the position returned at least six times its cost.
BIG_WIN_MULTIPLE = 6

BUY_SIDES: frozenset[str] = frozenset({"buy"})
SELL_SIDES: frozenset[str] = frozenset({"sell"})

#: Inventory movements with no observable price. Their presence is what contaminates.
TRANSFER_IN_SIDES: frozenset[str] = frozenset({"transfer_in", "receive", "mint", "airdrop"})
TRANSFER_OUT_SIDES: frozenset[str] = frozenset({"transfer_out", "send", "burn"})


def ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    """Exact-context division. ``None`` when the denominator is zero — not 0, not inf."""
    den = Decimal(denominator)
    if den == 0:
        return None
    return MATH.divide(Decimal(numerator), den)


def _as_int(value: Any) -> int | None:
    """Base units out of whatever SQLite or a provider handed us. ``None`` stays ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return None


def _as_dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _as_chain(value: Any) -> Chain | None:
    if isinstance(value, Chain):
        return value
    try:
        return Chain(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------------------


class Episode(BaseModel):
    """One round trip in a single token: first buy through to inventory back at ~zero.

    ``closed_ms is None`` means the wallet is still holding. Open episodes are reported
    (they are the evidence behind "never sells") but never counted as wins.
    """

    token: str
    chain: Chain | None = None
    opened_ms: int
    closed_ms: int | None = None
    buys: int = 0
    sells: int = 0
    cost_native: int = 0
    proceeds_native: int = 0
    cost_usd: Decimal | None = None
    proceeds_usd: Decimal | None = None
    realized_pnl_native: int = 0
    roi: Decimal | None = None
    hold_s: int = 0
    is_win: bool = False
    is_big_win: bool = False
    contaminated: bool = False
    contamination: list[str] = Field(default_factory=list)
    qty_bought: int = 0
    qty_sold: int = 0
    leftover_qty: int = 0

    @property
    def closed(self) -> bool:
        return self.closed_ms is not None

    @property
    def scorable(self) -> bool:
        """Closed and clean: the only kind of episode that may move a grade."""
        return self.closed and not self.contaminated

    @property
    def realized_pnl_usd(self) -> Decimal | None:
        if self.cost_usd is None or self.proceeds_usd is None:
            return None
        return self.proceeds_usd - self.cost_usd


class WalletPnl(BaseModel):
    """Aggregate over one wallet's episodes. Unknown stays ``None``; nothing defaults to 0
    that a reader could mistake for a measurement."""

    closed_episodes: int = 0
    open_episodes: int = 0
    contaminated_episodes: int = 0
    distinct_tokens: int = 0
    wins: int = 0
    win_rate: float | None = None
    realized_pnl_native: int = 0
    realized_pnl_usd: Decimal | None = None
    roi: Decimal | None = None
    big_wins: int = 0
    big_win_rate: float | None = None
    median_hold_s: int | None = None
    avg_hold_s: int | None = None
    sell_to_buy_ratio: float | None = None
    buys: int = 0
    sells: int = 0
    cost_native: int = 0
    proceeds_native: int = 0
    first_trade_ms: int | None = None
    last_trade_ms: int | None = None


# --------------------------------------------------------------------------------------
# reconstruction
# --------------------------------------------------------------------------------------


@dataclass
class _Builder:
    """Mutable accumulator for the episode currently open on one token."""

    token: str
    chain: Chain | None
    opened_ms: int
    inventory: int = 0
    qty_bought: int = 0
    qty_sold: int = 0
    buys: int = 0
    sells: int = 0
    movements: int = 0
    cost_native: int = 0
    proceeds_native: int = 0
    cost_usd: Decimal = ZERO
    proceeds_usd: Decimal = ZERO
    cost_usd_known: bool = True
    proceeds_usd_known: bool = True
    unknown_qty: bool = False
    flags: list[str] = field(default_factory=list)

    def flag(self, reason: str) -> None:
        if reason not in self.flags:
            self.flags.append(reason)

    @property
    def dust(self) -> int:
        return self.qty_bought * DUST_BPS // 10_000

    @property
    def settled(self) -> bool:
        """Inventory is back to zero (within dust) so the round trip is over."""
        return self.movements > 0 and not self.unknown_qty and self.inventory <= self.dust

    def finish(self, closed_ms: int | None, as_of_ms: int) -> Episode:
        realized = self.proceeds_native - self.cost_native
        end_ms = closed_ms if closed_ms is not None else as_of_ms
        closed = closed_ms is not None
        if self.buys == 0:
            # Inventory we never saw arrive: something funded this position off-book.
            self.flag("sell_without_buy")
        if self.inventory < -self.dust:
            self.flag("oversold")
        return Episode(
            token=self.token,
            chain=self.chain,
            opened_ms=self.opened_ms,
            closed_ms=closed_ms,
            buys=self.buys,
            sells=self.sells,
            cost_native=self.cost_native,
            proceeds_native=self.proceeds_native,
            cost_usd=self.cost_usd if self.cost_usd_known else None,
            proceeds_usd=self.proceeds_usd if self.proceeds_usd_known else None,
            realized_pnl_native=realized if closed else 0,
            roi=ratio(realized, self.cost_native) if closed and self.cost_native > 0 else None,
            hold_s=max(0, (end_ms - self.opened_ms) // 1000),
            is_win=closed and self.proceeds_native > self.cost_native,
            is_big_win=(
                closed
                and self.cost_native > 0
                and self.proceeds_native >= BIG_WIN_MULTIPLE * self.cost_native
            ),
            contaminated=bool(self.flags),
            contamination=list(self.flags),
            qty_bought=self.qty_bought,
            qty_sold=self.qty_sold,
            leftover_qty=max(0, self.inventory),
        )


def reconstruct(swaps: list[dict], *, as_of_ms: int | None = None) -> list[Episode]:
    """Replay one wallet's swaps into closed and open episodes.

    ``swaps`` are rows from the ``swaps`` table (``ts_ms``, ``token``, ``side``,
    ``amount_token``, ``amount_native``, ``usd_value``, ``chain``) for a single wallet.
    Rows are grouped per token and walked chronologically; ties keep input order so the
    result is deterministic. ``as_of_ms`` dates the hold time of still-open episodes.
    """
    as_of = as_of_ms if as_of_ms is not None else now_ms()
    by_token: dict[str, list[tuple[int, int, dict]]] = defaultdict(list)
    for index, raw in enumerate(swaps):
        token = str(raw.get("token") or "")
        if not token:
            continue
        by_token[token].append((_as_int(raw.get("ts_ms")) or 0, index, raw))

    episodes: list[Episode] = []
    for token in sorted(by_token):
        rows = sorted(by_token[token], key=lambda r: (r[0], r[1]))
        episodes.extend(_walk_token(token, rows, as_of))
    episodes.sort(key=lambda e: (e.opened_ms, e.token))
    return episodes


def _walk_token(token: str, rows: Sequence[tuple[int, int, dict]], as_of_ms: int) -> list[Episode]:
    out: list[Episode] = []
    current: _Builder | None = None
    for ts_ms, _, raw in rows:
        if current is None:
            current = _Builder(token=token, chain=_as_chain(raw.get("chain")), opened_ms=ts_ms)
        side = str(raw.get("side") or "").strip().lower()
        qty = _as_int(raw.get("amount_token"))
        native = _as_int(raw.get("amount_native"))
        usd = _as_dec(raw.get("usd_value"))
        current.movements += 1

        if side in BUY_SIDES:
            current.buys += 1
            current.inventory += qty or 0
            current.qty_bought += qty or 0
            if qty is None:
                current.unknown_qty = True
                current.flag("buy_without_token_amount")
            if native is None:
                current.flag("buy_without_native_amount")
            else:
                current.cost_native += native
            if usd is None:
                current.cost_usd_known = False
            else:
                current.cost_usd += usd
        elif side in SELL_SIDES:
            current.sells += 1
            current.inventory -= qty or 0
            current.qty_sold += qty or 0
            if qty is None:
                current.unknown_qty = True
                current.flag("sell_without_token_amount")
            if native is None:
                current.flag("sell_without_native_amount")
            else:
                current.proceeds_native += native
            if usd is None:
                current.proceeds_usd_known = False
            else:
                current.proceeds_usd += usd
        elif side in TRANSFER_IN_SIDES:
            current.inventory += qty or 0
            current.flag("transfer_in")
        elif side in TRANSFER_OUT_SIDES:
            current.inventory -= qty or 0
            current.flag("transfer_out")
        else:
            current.flag(f"unknown_side:{side or 'missing'}")

        if current.settled:
            out.append(current.finish(ts_ms, as_of_ms))
            current = None

    if current is not None:
        out.append(current.finish(None, as_of_ms))
    return out


# --------------------------------------------------------------------------------------
# aggregation
# --------------------------------------------------------------------------------------


def _median_int(values: Sequence[int]) -> int:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def summarize(episodes: Iterable[Episode]) -> WalletPnl:
    """Roll episodes into the shape the grader consumes.

    Contaminated episodes are excluded from every aggregate except the count of how many
    there were — the grader needs to know the sample was dirty, not pretend it was clean.
    """
    eps = list(episodes)
    clean = [e for e in eps if not e.contaminated]
    closed = [e for e in clean if e.closed]

    first_ms = min((e.opened_ms for e in eps), default=None)
    last_ms = max(((e.closed_ms or e.opened_ms) for e in eps), default=None)

    cost = sum(e.cost_native for e in closed)
    proceeds = sum(e.proceeds_native for e in closed)
    realized = proceeds - cost

    usd_rows = [e.realized_pnl_usd for e in closed if e.realized_pnl_usd is not None]
    buys = sum(e.buys for e in clean)
    sells = sum(e.sells for e in clean)
    holds = [e.hold_s for e in closed]
    wins = sum(1 for e in closed if e.is_win)
    big_wins = sum(1 for e in closed if e.is_big_win)

    win_rate = ratio(wins, len(closed))
    big_rate = ratio(big_wins, len(closed))
    s2b = ratio(sells, buys)

    return WalletPnl(
        closed_episodes=len(closed),
        open_episodes=sum(1 for e in clean if not e.closed),
        contaminated_episodes=sum(1 for e in eps if e.contaminated),
        distinct_tokens=len({e.token for e in clean}),
        wins=wins,
        win_rate=float(win_rate) if win_rate is not None else None,
        realized_pnl_native=realized,
        realized_pnl_usd=sum(usd_rows, ZERO) if usd_rows else None,
        roi=ratio(realized, cost) if cost > 0 else None,
        big_wins=big_wins,
        big_win_rate=float(big_rate) if big_rate is not None else None,
        median_hold_s=_median_int(holds) if holds else None,
        avg_hold_s=int(sum(holds) // len(holds)) if holds else None,
        sell_to_buy_ratio=float(s2b) if s2b is not None else None,
        buys=buys,
        sells=sells,
        cost_native=cost,
        proceeds_native=proceeds,
        first_trade_ms=first_ms,
        last_trade_ms=last_ms,
    )


def reconstruct_wallet(swaps: list[dict], *, as_of_ms: int | None = None) -> tuple[list[Episode], WalletPnl]:
    """Convenience for callers that want both halves in one step."""
    episodes = reconstruct(swaps, as_of_ms=as_of_ms)
    return episodes, summarize(episodes)
