"""Which variables actually separate a runner, and which pairs genuinely compose.

The owner asked for more confluences, signals, edge and variables. This is the search, and
it is built around the fact that a search like this is *very good at finding things that
are not there*. Twenty variables in five bins is a hundred hypotheses; at a 26% base rate
several will clear any lift threshold by luck alone, and each one will arrive with a
plausible story attached.

So the discipline here is not the statistics, it is the **holdout**:

    every token is placed by TIME. The older half discovers, the newer half confirms.
    A finding is reported only if it clears its threshold in BOTH halves, in the same
    direction. Everything else is printed as rejected, with its discovery lift intact so a
    reader can see exactly what was tempting about it.

A time split, not a random one, because the question we actually care about is whether a
variable still worked *later* -- which is the only sense in which it can work tomorrow. A
random split would let a fad that lived for six hours pass, since its tokens would land on
both sides.

This module measures the tape. It does NOT measure our P&L, and the gap between those is
where the live lane's -19.3% mean per fill lives: reaching a multiple on the tape is not
the same as capturing it through an exit ladder that pays ~6.5% round trip. A variable
that survives here has earned a place in the sizer's evidence, not a position.

Related: :mod:`kaiba.learning.mooner` supplies the case construction and the no-lookahead
rule (features from the first prints, outcome measured strictly after them).
"""

from __future__ import annotations

import collections
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from kaiba.core.db import fetch_all, get_conn
from kaiba.core.schemas import Chain, now_ms
from kaiba.learning.mooner import MOON_MULTIPLE, TokenCase, build_cases

log = logging.getLogger(__name__)

#: The smallest cell that may be reported, in EACH half. A bin that is thin in the holdout
#: cannot confirm anything, and reporting it would be reporting the discovery half alone.
MIN_CELL: int = 40

#: How far a bin's rate must sit from the base rate to be worth a second look, as a ratio.
#: Deliberately blunt: this is a screen, and the holdout is what does the actual filtering.
MIN_LIFT: float = 1.25

#: A pair is only interesting if the two variables tell us something TOGETHER that neither
#: tells us alone. This is how much the joint lift must beat the better single lift by.
MIN_PAIR_GAIN: float = 1.15

#: Quantile edges for numeric variables. Equal-population bins rather than round numbers,
#: so a bin is never thin merely because the threshold was chosen by a human.
QUANTILES: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)


@dataclass
class Cell:
    """One bin of one variable: how many landed in it and how many ran."""

    n: int = 0
    hits: int = 0

    @property
    def rate(self) -> float:
        return (100.0 * self.hits / self.n) if self.n else 0.0

    def lift(self, base: float) -> float:
        return (self.rate / base) if base else 0.0


@dataclass
class Finding:
    """One variable bin, measured in both halves."""

    variable: str
    bin_label: str
    discovery: Cell
    holdout: Cell
    discovery_base: float
    holdout_base: float

    @property
    def discovery_lift(self) -> float:
        return self.discovery.lift(self.discovery_base)

    @property
    def holdout_lift(self) -> float:
        return self.holdout.lift(self.holdout_base)

    @property
    def confirmed(self) -> bool:
        """Clears the threshold in both halves, in the same direction."""
        if self.discovery.n < MIN_CELL or self.holdout.n < MIN_CELL:
            return False
        high = self.discovery_lift >= MIN_LIFT and self.holdout_lift >= MIN_LIFT
        low = self.discovery_lift <= 1 / MIN_LIFT and self.holdout_lift <= 1 / MIN_LIFT
        return high or low

    @property
    def direction(self) -> str:
        return "favours" if self.holdout_lift >= 1.0 else "avoids"


@dataclass
class PairFinding:
    """Two variables whose combination beats either one alone, in both halves."""

    left: str
    right: str
    label: str
    discovery: Cell
    holdout: Cell
    best_single_lift: float
    discovery_base: float
    holdout_base: float

    @property
    def holdout_lift(self) -> float:
        return self.holdout.lift(self.holdout_base)

    @property
    def discovery_lift(self) -> float:
        return self.discovery.lift(self.discovery_base)

    @property
    def gain(self) -> float:
        """How much the pair beats the better of its two parts."""
        return (self.holdout_lift / self.best_single_lift) if self.best_single_lift else 0.0

    @property
    def confirmed(self) -> bool:
        return (
            self.discovery.n >= MIN_CELL
            and self.holdout.n >= MIN_CELL
            and self.discovery_lift >= MIN_LIFT
            and self.holdout_lift >= MIN_LIFT
            and self.gain >= MIN_PAIR_GAIN
        )


@dataclass
class Study:
    chain: Chain | None
    computed_ms: int
    sample: int
    split_ms: int
    moon_multiple: float
    hypotheses: int = 0
    confirmed: list[Finding] = field(default_factory=list)
    rejected: list[Finding] = field(default_factory=list)
    pairs: list[PairFinding] = field(default_factory=list)
    pair_hypotheses: int = 0


# --------------------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------------------


def _measure(body: dict[str, Any], key: str, *, as_of_ms: int | None = None) -> float | None:
    value = body.get(key)
    if isinstance(value, dict):
        if str(value.get("basis", "")).lower() in {"unavailable", "stale"}:
            return None
        receipt = value.get("receipt")
        if as_of_ms is not None and isinstance(receipt, dict):
            try:
                observed_ms = int(receipt.get("observed_at_ms"))
            except (TypeError, ValueError, OverflowError):
                return None
            if observed_ms <= 0 or observed_ms > as_of_ms:
                return None
        value = value.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: Variables read from the dossier, with the coverage measured on the live box 2026-09-23.
#: Anything under ~80% coverage is left out rather than studied on the minority that has
#: it: a variable present only on the tokens some provider happened to answer for is a
#: statement about the provider, not about the token.
NUMERIC_FIELDS: tuple[str, ...] = (
    "liquidity_usd", "dev_pct", "holder_count", "bundler_pct", "sniper_pct",
    "top10_pct", "lp_burned_pct", "buy_tax_bps", "sell_tax_bps", "cluster_pct",
    "score", "entity_count", "price_usd",
)

BOOL_FIELDS: tuple[str, ...] = (
    "can_sell", "mint_authority_revoked", "freeze_authority_revoked",
)


def _dossiers(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in fetch_all(conn, "SELECT chain, address, built_at_ms, grade, dossier_json FROM token_dossiers", ()):
        try:
            body = json.loads(row["dossier_json"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        body["_grade"] = row["grade"]
        body["built_at_ms"] = row["built_at_ms"]
        out[(str(row["chain"]), str(row["address"]))] = body
    return out


def features_for(case: TokenCase, body: dict[str, Any] | None) -> dict[str, Any]:
    """Everything known about this token BEFORE its outcome window opened.

    Tape features come from :class:`TokenCase`. The dossier table is mutable: a latest
    scan may postdate this early window by hours. It is unavailable here unless its stored
    build time is at or before the feature cutoff; missing history is never backfilled
    with a token's later state.
    """
    out: dict[str, Any] = {
        # Token metadata is mutable and has no as-of provenance in TokenCase.
        "launchpad": None,
        "chain": case.chain.value,
        "early_buy_fraction": case.early_buy_fraction,
        "early_wallets": float(len(case.early_wallets)),
    }
    if body:
        built_at = body.get("built_at_ms")
        try:
            built_at = int(built_at) if not isinstance(built_at, bool) else 0
        except (TypeError, ValueError, OverflowError):
            return out
        if built_at <= 0 or built_at > case.window_end_ms:
            return out
        for key in NUMERIC_FIELDS:
            out[key] = _measure(body, key, as_of_ms=case.window_end_ms)
        for key in BOOL_FIELDS:
            out[key] = _measure(body, key, as_of_ms=case.window_end_ms)
        out["grade"] = body.get("_grade") or "?"
    return out


# --------------------------------------------------------------------------------------
# binning
# --------------------------------------------------------------------------------------


def _quantile_edges(values: Sequence[float]) -> list[float]:
    ordered = sorted(values)
    if len(ordered) < MIN_CELL * 2:
        return []
    return [ordered[max(0, min(len(ordered) - 1, int(q * len(ordered))))] for q in QUANTILES]


def _binner(name: str, rows: Sequence[tuple[dict[str, Any], bool, int]]) -> Callable[[Any], str | None] | None:
    """Return a function mapping this variable's value to a bin label, or ``None``.

    Categorical variables bin to themselves. Numeric quantiles and category eligibility
    are fitted on discovery rows only, then the same frozen mapping is used on holdout.
    Re-fitting on holdout, or including it in the fit, changes the question after seeing it.
    """
    values = [f.get(name) for f, _, _ in rows]
    known = [v for v in values if v is not None]
    if len(known) < MIN_CELL * 2:
        return None
    if isinstance(known[0], str):
        counts = collections.Counter(str(v) for v in known)
        keep = {k for k, n in counts.items() if n >= MIN_CELL * 2}
        if not keep:
            return None
        return lambda v: (str(v) if str(v) in keep else None) if v is not None else None
    edges = _quantile_edges([float(v) for v in known])
    if not edges or len(set(edges)) < 2:
        return None

    def to_bin(value: Any) -> str | None:
        if value is None:
            return None
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        for i, edge in enumerate(edges):
            if x <= edge:
                return f"q{i + 1} (<={edge:g})"
        return f"q{len(edges) + 1} (>{edges[-1]:g})"

    return to_bin


# --------------------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------------------


def build_rows(
    conn: sqlite3.Connection, chain: Chain | None = None, *, since_ms: int = 0
) -> list[tuple[dict[str, Any], bool, int]]:
    """``(features, mooned, window_end_ms)`` per token, ordered oldest first."""
    dossiers = _dossiers(conn)
    rows: list[tuple[dict[str, Any], bool, int]] = []
    for case in build_cases(conn, chain, since_ms=since_ms):
        body = dossiers.get((case.chain.value, case.token))
        rows.append((features_for(case, body), case.mooned, case.window_end_ms))
    rows.sort(key=lambda r: r[2])
    return rows


def _tally(rows: Iterable[tuple[dict[str, Any], bool, int]], name: str, to_bin) -> dict[str, Cell]:
    cells: dict[str, Cell] = collections.defaultdict(Cell)
    for features, mooned, _ in rows:
        label = to_bin(features.get(name))
        if label is None:
            continue
        cell = cells[label]
        cell.n += 1
        cell.hits += 1 if mooned else 0
    return cells


def _base_rate(rows: Sequence[tuple[dict[str, Any], bool, int]]) -> float:
    return (100.0 * sum(1 for _, m, _ in rows if m) / len(rows)) if rows else 0.0


def run(
    conn: sqlite3.Connection | None = None,
    chain: Chain | None = None,
    *,
    since_ms: int = 0,
    pairs: bool = True,
) -> Study:
    """Screen every variable, then every promising pair, confirming each on the holdout."""
    c = conn or get_conn()
    rows = build_rows(c, chain, since_ms=since_ms)
    half = len(rows) // 2
    discovery, holdout = rows[:half], rows[half:]
    study = Study(
        chain=chain,
        computed_ms=now_ms(),
        sample=len(rows),
        split_ms=rows[half][2] if rows and half < len(rows) else 0,
        moon_multiple=MOON_MULTIPLE,
    )
    if len(discovery) < MIN_CELL * 2 or len(holdout) < MIN_CELL * 2:
        return study

    d_base, h_base = _base_rate(discovery), _base_rate(holdout)
    names = ["launchpad", "chain", "early_buy_fraction", "early_wallets", "grade",
             *NUMERIC_FIELDS, *BOOL_FIELDS]
    binners = {n: _binner(n, discovery) for n in names}
    binners = {n: b for n, b in binners.items() if b is not None}

    best_single: dict[tuple[str, str], float] = {}
    for name, to_bin in binners.items():
        d_cells = _tally(discovery, name, to_bin)
        h_cells = _tally(holdout, name, to_bin)
        for label, d_cell in d_cells.items():
            study.hypotheses += 1
            finding = Finding(name, label, d_cell, h_cells.get(label, Cell()), d_base, h_base)
            if finding.confirmed:
                study.confirmed.append(finding)
                best_single[(name, label)] = finding.holdout_lift
            elif d_cell.n >= MIN_CELL and finding.discovery_lift >= MIN_LIFT:
                study.rejected.append(finding)
    study.confirmed.sort(key=lambda f: f.holdout_lift, reverse=True)
    study.rejected.sort(key=lambda f: f.discovery_lift, reverse=True)

    if pairs and study.confirmed:
        study.pairs = _pair_study(discovery, holdout, binners, study, d_base, h_base, best_single)
    return study


def _pair_study(discovery, holdout, binners, study, d_base, h_base, best_single) -> list[PairFinding]:
    """Cross the confirmed single findings and keep only combinations that ADD something.

    A pair that merely restates one of its parts is not a confluence -- it is the same
    signal counted twice, which is precisely how a book talks itself into conviction it
    has not earned.
    """
    top = study.confirmed[:8]
    out: list[PairFinding] = []
    for i, left in enumerate(top):
        for right in top[i + 1:]:
            if left.variable == right.variable:
                continue
            lb, rb = binners[left.variable], binners[right.variable]

            def both(rows, lv=left, rv=right, lb=lb, rb=rb):
                cell = Cell()
                for features, mooned, _ in rows:
                    if lb(features.get(lv.variable)) != lv.bin_label:
                        continue
                    if rb(features.get(rv.variable)) != rv.bin_label:
                        continue
                    cell.n += 1
                    cell.hits += 1 if mooned else 0
                return cell

            study.pair_hypotheses += 1
            pair = PairFinding(
                left=f"{left.variable}={left.bin_label}",
                right=f"{right.variable}={right.bin_label}",
                label=f"{left.variable}={left.bin_label} AND {right.variable}={right.bin_label}",
                discovery=both(discovery),
                holdout=both(holdout),
                best_single_lift=max(left.holdout_lift, right.holdout_lift),
                discovery_base=d_base,
                holdout_base=h_base,
            )
            if pair.confirmed:
                out.append(pair)
    out.sort(key=lambda p: p.holdout_lift, reverse=True)
    return out


def lines(study: Study) -> list[str]:
    where = study.chain.value if study.chain else "all chains"
    out = [
        f"variable study — {where}",
        f"  {study.sample} tokens, split by time into {study.sample // 2} discover / "
        f"{study.sample - study.sample // 2} confirm; outcome is reaching "
        f"{study.moon_multiple:g}x AFTER the feature window",
        f"  {study.hypotheses} single hypotheses tested, {study.pair_hypotheses} pairs",
    ]
    if not study.sample:
        out.append("  not enough tape to split; nothing measured")
        return out
    out.append(f"  CONFIRMED in both halves ({len(study.confirmed)}):")
    if not study.confirmed:
        out.append("    none — every candidate failed its holdout, which is the usual result")
    for f in study.confirmed[:14]:
        out.append(
            f"    {f.direction:7s} {f.variable}={f.bin_label[:26]:26s} "
            f"discover {f.discovery_lift:4.2f}x (n={f.discovery.n:<5d}) "
            f"confirm {f.holdout_lift:4.2f}x (n={f.holdout.n:<5d})"
        )
    out.append(f"  REJECTED by the holdout ({len(study.rejected)}) — what would have fooled us:")
    for f in study.rejected[:8]:
        out.append(
            f"    {f.variable}={f.bin_label[:26]:26s} "
            f"discover {f.discovery_lift:4.2f}x (n={f.discovery.n:<5d}) "
            f"confirm {f.holdout_lift:4.2f}x (n={f.holdout.n:<5d})"
        )
    out.append(f"  CONFLUENCES — pairs that beat both parts ({len(study.pairs)}):")
    if not study.pairs:
        out.append("    none: every pair merely restated one of its halves")
    for p in study.pairs[:8]:
        out.append(
            f"    {p.label[:62]:62s} confirm {p.holdout_lift:4.2f}x "
            f"(n={p.holdout.n}, +{(p.gain - 1) * 100:.0f}% over its best half)"
        )
    return out
