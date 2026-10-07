"""Exit reasons that close a PAPER position without measuring what it was worth.

A return statistic averages outcomes. A row whose outcome was booked rather than observed
is not an outcome, and averaging it in moves the mean by however wrong the booking was.

``abandoned_unpriceable`` is that kind of row. ``watchdog._abandon_unpriceable_shadow``
closes a shadow position through ``accounting.write_off_dust``: quantity to zero, proceeds
whatever had already filled, realised = proceeds - cost. No sell is modelled and no price
is read, so the row says -100% because nothing was sold, not because the token was worth
nothing.

MEASURED 2026-10-04 on the box (read-only), all 73 shadow rows with this reason:

* 72 of 73 have ``proceeds_native = 0``, so they read exactly -100%. 0 of 73 have a
  ``trades`` row (``write_off_dust`` writes none), so every trades-based reader already
  leaves them out, silently; positions-based readers average them in.
* 69 of 73 had an exit DECIDED on a usable price before they were abandoned (first decision
  mean -6.4%, median -5.5% from entry): 18 emergency_loss, 13 stop_loss, 14 moonbag
  trails, 10 trails, 11 tp1, 3 stale. Every one of 709 exit attempts failed with
  "paper exit not modelled: liquidity unavailable", and the hour-old check then wrote the
  position off.
* They are NOT missing at random. The last mark before the write-off was <= -80% on 54 of
  73 (median -93.8%): a paper position that cannot exit keeps riding the dump. Leaving
  them out therefore removes rows whose decided exits were mild AND whose unexited marks
  were catastrophic. Every reader that excludes them must say how many it excluded, so
  the reader of the number can see what was taken out.

What is NOT fabricated, and stays in every statistic:

* The same reasons on a LIVE or CANARY position. That cost was real money and the write-off
  is the loss the wallet took: ``abandoned_unpriceable:operator_2026-09-24`` (bsc,
  ``pos_78901def47ceff45aac2a2b7``) and ``write_off:no_swap_route`` (robinhood, tokens
  still held, no route to sell them). Leaving them out would flatter live expectancy.
* ``dust_written_off``: the sells that happened are real; only an unsellable 1% remainder
  is booked at zero (``pos_ca35d86b03667b5e7a08fd24``: -0.026576 -> -0.027354 SOL).
* ``bookkeeping_correction:*``, ``stale_no_volume:*``, ``rug:*``: each closes on a sell or
  a wallet-signed settlement that carries a price.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

#: Exit-reason families (the text before the first ``:``) whose outcome is booked, not
#: measured, on a position that spent no money.
FABRICATED_EXIT_FAMILIES: frozenset[str] = frozenset({"abandoned_unpriceable"})

#: Modes whose cost left a wallet. A write-off there is a real loss, never excluded.
MONEY_MODES: frozenset[str] = frozenset({"live", "canary"})

#: One line a read-out can carry beside its excluded count.
RULE = (
    "excluded from return statistics: non-live positions closed as "
    + ", ".join(sorted(FABRICATED_EXIT_FAMILIES))
    + " (booked at proceeds-so-far, usually -100%, with no sell modelled); "
    "live/canary write-offs are real losses and stay in"
)


def exit_family(exit_reason: Any) -> str:
    """``"abandoned_unpriceable:operator_2026-09-24"`` -> ``"abandoned_unpriceable"``."""
    return str(exit_reason or "").split(":", 1)[0].strip()


def is_fabricated_outcome(exit_reason: Any, mode: Any) -> bool:
    """True when this closed row's return was booked rather than observed."""
    if str(mode or "").strip().lower() in MONEY_MODES:
        return False
    return exit_family(exit_reason) in FABRICATED_EXIT_FAMILIES


def sql_predicate(alias: str = "") -> str:
    """:func:`is_fabricated_outcome` as a SQL boolean over ``mode`` and ``exit_reason``.

    Built from the module's constants only; it takes no parameters and no caller input.
    ``substr`` rather than ``LIKE`` because ``_`` is a LIKE wildcard.
    """
    p = f"{alias}." if alias else ""
    families = " OR ".join(
        f"{p}exit_reason = '{f}' OR substr({p}exit_reason, 1, {len(f) + 1}) = '{f}:'"
        for f in sorted(FABRICATED_EXIT_FAMILIES)
    )
    modes = ", ".join(f"'{m}'" for m in sorted(MONEY_MODES))
    return f"({p}mode NOT IN ({modes}) AND ({families}))"


def _field(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key] if key in row.keys() else None
    except (AttributeError, IndexError, KeyError):
        return None


def drop(rows: Iterable[Any]) -> tuple[list[Any], int]:
    """``(kept, excluded)``: rows with a fabricated outcome removed, and how many were."""
    kept: list[Any] = []
    excluded = 0
    for row in rows:
        if is_fabricated_outcome(_field(row, "exit_reason"), _field(row, "mode")):
            excluded += 1
        else:
            kept.append(row)
    return kept, excluded


def count_positions(
    conn: sqlite3.Connection,
    *,
    lane: str | None = None,
    mode: str | None = None,
    opened_since_ms: int | None = None,
    closed_since_ms: int | None = None,
    closed_until_ms: int | None = None,
) -> int:
    """Closed positions with a fabricated outcome, in a lane/mode/window. Read-only."""
    sql = f"SELECT COUNT(*) FROM positions WHERE closed_ms IS NOT NULL AND {sql_predicate()}"
    args: list[Any] = []
    for column, op, value in (
        ("lane", "=", lane),
        ("mode", "=", mode),
        ("opened_ms", ">=", opened_since_ms),
        ("closed_ms", ">=", closed_since_ms),
        ("closed_ms", "<=", closed_until_ms),
    ):
        if value is not None:
            sql += f" AND {column} {op} ?"
            args.append(value)
    row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


__all__ = [
    "FABRICATED_EXIT_FAMILIES",
    "MONEY_MODES",
    "RULE",
    "count_positions",
    "drop",
    "exit_family",
    "is_fabricated_outcome",
    "sql_predicate",
]
