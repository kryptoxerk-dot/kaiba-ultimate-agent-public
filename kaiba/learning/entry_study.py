"""Re-measure, on fresh tape, whether an entry feature still separates outcomes.

This runs alongside live and paper trading. Its job is to keep the gates in
:mod:`kaiba.execution.risk` honest as the market changes: a threshold measured once is a
threshold that will eventually be wrong, and nothing else in the system would notice.

THE METHOD, and every part of it exists because the first pass got it wrong:

* **Features come from a window that closes before the outcome opens.** A first pass
  measured features over the same window as the outcome and reported "more unique wallets
  -> 2.3x more likely to reach 2x". That was reverse causation: a token that pumped
  attracted wallets. Re-measured with the decision instant at ``t0 + FEATURE_WINDOW_S``
  and the outcome strictly after it, the same feature showed a lift of 1.0.
* **Every candidate cell is recorded, not just the interesting ones.** Scanning many
  thresholds and reporting the best is how a 13.9% base rate produces a "37.5%" cell on
  n=8. The row count per cell travels with every result so a reader can price that.
* **A minimum cell size is enforced.** Below :data:`MIN_CELL` nothing is reported at all.

WHAT IT DOES NOT DO: it does not change a gate. It writes what it measured; moving a
threshold in ``risk.py`` stays a deliberate act with a diff, because a gate that retunes
itself on its own output is a feedback loop nobody is reading.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from kaiba.core.db import fetch_all, get_conn, tx
from kaiba.core.schemas import Chain

log = logging.getLogger(__name__)

#: The decision instant, in seconds after a token's first priced print. Features may read
#: only inside it; the outcome is measured only after it.
FEATURE_WINDOW_S = 300

#: Minimum priced prints inside the window for the features to mean anything.
MIN_PRINTS_IN_WINDOW = 5

#: Cells smaller than this are not reported. See the docstring: small cells are how a
#: scan over many thresholds manufactures a finding.
MIN_CELL = 60

#: The outcome. A token "ran" if its peak AFTER the decision instant reached this multiple
#: of the price AT the decision instant.
RUNNER_MULTIPLE = 2.0

WINDOW_DAYS = 7


@dataclass
class Cell:
    name: str
    n: int
    runners: int
    median_multiple: float

    @property
    def rate(self) -> float:
        return 100.0 * self.runners / self.n if self.n else 0.0


@dataclass
class StudyResult:
    chain: Chain
    computed_ms: int
    sample: int
    baseline_rate: float
    cells: list[Cell] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lift(self, cell: Cell) -> float:
        return cell.rate / self.baseline_rate if self.baseline_rate else 0.0


def _observations(
    conn: sqlite3.Connection, chain: Chain, *, window_days: int, now_ms: int
) -> dict[str, dict[str, float]]:
    """``token -> features measured in the window, plus the outcome after it``."""
    since = now_ms - window_days * 86_400_000
    firsts = {
        str(r["token"]): int(r["tf"])
        for r in fetch_all(
            conn,
            "SELECT token, MIN(ts_ms) AS tf FROM swaps WHERE chain=? AND price_usd IS NOT NULL "
            "AND ts_ms > ? GROUP BY token",
            (chain.value, since),
        )
    }
    rows = fetch_all(
        conn,
        "SELECT token, ts_ms, price_usd, side, wallet FROM swaps WHERE chain=? "
        "AND price_usd IS NOT NULL AND ts_ms > ? ORDER BY token, ts_ms",
        (chain.value, since),
    )
    out: dict[str, dict[str, float]] = {}
    current: str | None = None
    acc: dict[str, Any] | None = None

    def close(token: str | None, a: dict[str, Any] | None) -> None:
        if token is None or a is None:
            return
        if a["n"] < MIN_PRINTS_IN_WINDOW or not a["px"] or not a["post"]:
            return
        entry = a["px"][-1]
        if entry <= 0 or a["px"][0] <= 0:
            return
        out[token] = {
            "wallets": float(len(a["wallets"])),
            "prints": float(a["n"]),
            "buy_fraction": a["buys"] / a["n"],
            "run_in_window": max(a["px"]) / a["px"][0],
            "multiple": max(a["post"]) / entry,
        }

    for row in rows:
        token = str(row["token"])
        if token != current:
            close(current, acc)
            current = token
            acc = {"t0": firsts.get(token, int(row["ts_ms"])), "n": 0, "buys": 0,
                   "wallets": set(), "px": [], "post": []}
        try:
            price = float(row["price_usd"])
        except (TypeError, ValueError):
            continue
        if price <= 0 or acc is None:
            continue
        if int(row["ts_ms"]) <= acc["t0"] + FEATURE_WINDOW_S * 1000:
            acc["n"] += 1
            acc["px"].append(price)
            acc["wallets"].add(row["wallet"])
            if str(row["side"]) == "buy":
                acc["buys"] += 1
        else:
            acc["post"].append(price)
    close(current, acc)
    return out


#: The features under standing measurement. Each maps an observation to a cell name, or
#: None to leave it out. Keep the cells coarse: a scan over fine thresholds is a scan.
FEATURES: dict[str, Callable[[dict[str, float]], str | None]] = {
    "buy_fraction": lambda o: (
        "below_0.45" if o["buy_fraction"] < 0.45
        else "0.45_to_0.60" if o["buy_fraction"] < 0.60
        else "at_or_above_0.60"
    ),
    "unique_wallets": lambda o: (
        "under_10" if o["wallets"] < 10
        else "10_to_40" if o["wallets"] < 40
        else "40_or_more"
    ),
    "run_in_window": lambda o: (
        "under_1.05x" if o["run_in_window"] < 1.05
        else "1.05_to_1.5x" if o["run_in_window"] < 1.5
        else "1.5x_or_more"
    ),
}


def run_study(
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    *,
    window_days: int = WINDOW_DAYS,
    now_ms: int | None = None,
) -> StudyResult:
    c = conn or get_conn()
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    obs = _observations(c, chain, window_days=window_days, now_ms=now)
    result = StudyResult(chain=chain, computed_ms=now, sample=len(obs), baseline_rate=0.0)
    if not obs:
        result.notes.append("no observations in window")
        return result
    runners = sum(1 for o in obs.values() if o["multiple"] >= RUNNER_MULTIPLE)
    result.baseline_rate = 100.0 * runners / len(obs)

    for feature, keyfn in FEATURES.items():
        groups: dict[str, list[float]] = {}
        for o in obs.values():
            key = keyfn(o)
            if key is not None:
                groups.setdefault(key, []).append(o["multiple"])
        for key, multiples in sorted(groups.items()):
            if len(multiples) < MIN_CELL:
                result.notes.append(
                    f"{feature}/{key}: n={len(multiples)} below MIN_CELL={MIN_CELL}, not reported"
                )
                continue
            result.cells.append(
                Cell(
                    name=f"{feature}/{key}",
                    n=len(multiples),
                    runners=sum(1 for m in multiples if m >= RUNNER_MULTIPLE),
                    median_multiple=statistics.median(multiples),
                )
            )
    return result


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entry_study ("
        "  chain TEXT NOT NULL,"
        "  cell TEXT NOT NULL,"
        "  computed_ms INTEGER NOT NULL,"
        "  sample INTEGER NOT NULL,"
        "  baseline_rate REAL NOT NULL,"
        "  n INTEGER NOT NULL,"
        "  runners INTEGER NOT NULL,"
        "  rate REAL NOT NULL,"
        "  lift REAL NOT NULL,"
        "  median_multiple REAL NOT NULL,"
        "  PRIMARY KEY (chain, cell, computed_ms))"
    )


def record(conn: sqlite3.Connection, result: StudyResult) -> int:
    """Append this run's cells. History is kept: drift is the point."""
    ensure_table(conn)
    if not result.cells:
        return 0
    rows = [
        (result.chain.value, cell.name, result.computed_ms, result.sample,
         result.baseline_rate, cell.n, cell.runners, cell.rate,
         result.lift(cell), cell.median_multiple)
        for cell in result.cells
    ]
    with tx(conn) as c:
        c.executemany(
            "INSERT OR REPLACE INTO entry_study (chain, cell, computed_ms, sample, "
            "baseline_rate, n, runners, rate, lift, median_multiple) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def lines(result: StudyResult) -> list[str]:
    out = [
        f"entry study {result.chain.value}: n={result.sample}, "
        f"baseline {result.baseline_rate:.1f}% reach {RUNNER_MULTIPLE}x "
        f"(decision at t0+{FEATURE_WINDOW_S}s, outcome strictly after)"
    ]
    for cell in sorted(result.cells, key=lambda c: -c.rate):
        out.append(
            f"  {cell.name:34} n={cell.n:5} rate={cell.rate:5.1f}% "
            f"lift={result.lift(cell):4.2f}x median={cell.median_multiple:.2f}x"
        )
    for note in result.notes[:6]:
        out.append(f"  note: {note}")
    return out
