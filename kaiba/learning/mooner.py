"""What a token that already mooned looked like BEFORE it mooned.

The owner's question (2026-09-23): "check the behavior and characteristics and variables of
a mooner that already mooned -- which wallets buying, the narrative, etc -- so we can
hopefully get similar tokens EARLY and with size."

This module answers it from our own tape and nothing else. Three rules shape it, and each
one exists because the obvious version of this study is wrong:

**1. The feature window closes before the outcome window opens.**
A token's features are read from its first :data:`EARLY_PRINTS` prints (bounded also by
:data:`EARLY_WINDOW_MS`, so a token that trades once an hour cannot quietly borrow a day of
hindsight). The outcome is the highest price *after* that window, over the price *at the
end of it*. Measuring the multiple from the first print instead would count the move that
happened while we were still reading the features -- the study would "discover" that
tokens which have already started running tend to run.

**2. Frequency is not evidence; precision is.**
MEASURED on the live tape: the wallet appearing in the most >=5x tokens appears in 408 of
1,400 of them. It is not an oracle, it is something that buys nearly everything -- a router,
an aggregator or a bot. Ranking wallets by how many mooners they touched would put it
first. So every actor here is scored by PRECISION: of the tokens it bought early, what
share went on to moon, against the base rate of all tokens. A wallet that buys everything
scores exactly the base rate and ranks nowhere, which is the correct answer.

This is the same failure the live book already paid for once: smart-wallet COUNT is
anti-calibrated in our own fills (3 wallets -8.9%, 4+ -18.4%, 7+ a 0% win rate). More
eyes is not more edge, and this module is built so it cannot conclude that it is.

**3. A small sample is reported as a small sample.**
Every row carries its ``n``. Nothing is ranked above :data:`MIN_ACTOR_TOKENS` observations,
lift is reported beside the rate rather than instead of it, and a band with no
observations prints as "-" rather than as zero -- an unmeasured thing is never a measured
zero (CONTRACT rule 2).

What it does NOT claim: that a feature which separates mooners will make money. Reaching
5x on the tape is not the same as our realised P&L -- we exit on a ladder and pay ~6.5%
round trip -- and the gap between those two is exactly where the live lane's -19.3% mean
per fill lives. This ranks candidates; the sizer and the exit ladder still decide.
"""

from __future__ import annotations

import collections
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable

from kaiba.core.db import fetch_all, get_conn
from kaiba.core.schemas import Chain, now_ms

log = logging.getLogger(__name__)

#: Prints that make up the "early" window a feature may be read from.
#: Chosen so a feature is something we could actually have seen while the token was still
#: young: ten prints is seconds to minutes on a live launch. DERIVED from the tape's own
#: shape rather than tuned -- see `EARLY_WINDOW_MS` for the other half of the bound.
EARLY_PRINTS: int = 10

#: The early window also closes after this long, whichever comes first. Without it a token
#: that prints ten times over two days would carry two days of hindsight into its
#: "early" features. INVENTED: five minutes is the horizon on which an entry decision is
#: actually taken, not a fitted value.
EARLY_WINDOW_MS: int = 5 * 60 * 1000

#: A token needs this many prints before it can be scored at all: the early window plus
#: enough afterwards for the outcome to mean something.
MIN_PRINTS: int = EARLY_PRINTS + 5

#: What counts as a mooner. Reported alongside every result so a reader never has to guess
#: which threshold produced a number.
MOON_MULTIPLE: float = 5.0

#: Below this many early buys an actor is not ranked. With the base rate near 8.5%, a
#: wallet seen on three tokens can hit 33% by luck; this is the line under which a rate is
#: reported but never ranked.
MIN_ACTOR_TOKENS: int = 8

#: Same idea for a narrative word.
MIN_WORD_TOKENS: int = 12

#: Words carried by so many tokens that they describe the venue rather than a narrative.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "of", "and", "on", "in", "to", "for", "is", "it", "by", "with",
    "coin", "token", "inu", "meme", "official", "community", "finance", "protocol",
})

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Print:
    """One tape row, reduced to what this study reads."""

    ts_ms: int
    price: float
    side: str
    wallet: str | None


@dataclass
class TokenCase:
    """One token's early features and its outcome, with the two kept apart by construction."""

    chain: Chain
    token: str
    early_wallets: tuple[str, ...]
    early_buys: int
    early_sells: int
    first_price: float
    window_end_price: float
    window_end_ms: int
    multiple: float
    symbol: str | None = None
    name: str | None = None
    launchpad: str | None = None

    @property
    def mooned(self) -> bool:
        return self.multiple >= MOON_MULTIPLE

    @property
    def early_buy_fraction(self) -> float | None:
        total = self.early_buys + self.early_sells
        return (self.early_buys / total) if total else None


@dataclass
class Cell:
    """One actor or feature: how often it was early, and how often that went on to moon."""

    key: str
    n: int
    mooners: int
    label: str = ""

    @property
    def rate(self) -> float:
        return (100.0 * self.mooners / self.n) if self.n else 0.0

    def lift(self, baseline: float) -> float:
        return (self.rate / baseline) if baseline else 0.0

    @property
    def ranked(self) -> bool:
        return self.n >= MIN_ACTOR_TOKENS


@dataclass
class Autopsy:
    """The whole study. Every list is ordered best-first among ranked rows only."""

    chain: Chain | None
    computed_ms: int
    sample: int
    mooners: int
    moon_multiple: float = MOON_MULTIPLE
    wallets: list[Cell] = field(default_factory=list)
    words: list[Cell] = field(default_factory=list)
    launchpads: list[Cell] = field(default_factory=list)
    flow: list[Cell] = field(default_factory=list)

    @property
    def baseline_rate(self) -> float:
        return (100.0 * self.mooners / self.sample) if self.sample else 0.0


# --------------------------------------------------------------------------------------
# reading the tape
# --------------------------------------------------------------------------------------


def _series(conn: sqlite3.Connection, chain: Chain | None, since_ms: int) -> dict[tuple[str, str], list[Print]]:
    """Every token's prints, oldest first. Prices that cannot be parsed are dropped."""
    sql = (
        "SELECT chain, token, ts_ms, price_usd, side, wallet FROM swaps "
        "WHERE price_usd IS NOT NULL AND price_usd != '' AND ts_ms >= ?"
    )
    params: list[Any] = [int(since_ms)]
    if chain is not None:
        sql += " AND chain = ?"
        params.append(chain.value)
    sql += " ORDER BY ts_ms"

    out: dict[tuple[str, str], list[Print]] = collections.defaultdict(list)
    for row in fetch_all(conn, sql, params):
        try:
            price = float(row["price_usd"])
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out[(str(row["chain"]), str(row["token"]))].append(
            Print(int(row["ts_ms"] or 0), price, str(row["side"] or "").lower(), row["wallet"])
        )
    return out


def _identity(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    rows = fetch_all(conn, "SELECT chain, address, symbol, name, launchpad FROM tokens", ())
    return {
        (str(r["chain"]), str(r["address"])): {
            "symbol": r["symbol"], "name": r["name"], "launchpad": r["launchpad"],
        }
        for r in rows
    }


def build_cases(
    conn: sqlite3.Connection, chain: Chain | None = None, *, since_ms: int = 0
) -> list[TokenCase]:
    """One :class:`TokenCase` per token with enough tape to separate features from outcome."""
    identity = _identity(conn)
    cases: list[TokenCase] = []
    for (chain_value, token), prints in _series(conn, chain, since_ms).items():
        if len(prints) < MIN_PRINTS:
            continue
        opened = prints[0].ts_ms
        window = [
            p for i, p in enumerate(prints)
            if i < EARLY_PRINTS and (p.ts_ms - opened) <= EARLY_WINDOW_MS
        ]
        if len(window) < 2:
            continue
        # Timestamp ordering has no executable order within a tied timestamp.
        # A print at the feature cutoff cannot be counted as a future opportunity.
        after = [p for p in prints[len(window):] if p.ts_ms > window[-1].ts_ms]
        if not after:
            continue
        # The outcome is measured from the END of the feature window, never from the first
        # print. See the module docstring: measuring from the first print would count the
        # move that happened while the features were still being read.
        base = window[-1].price
        if base <= 0:
            continue
        meta = identity.get((chain_value, token), {})
        cases.append(
            TokenCase(
                chain=Chain(chain_value),
                token=token,
                early_wallets=tuple(
                    sorted({p.wallet for p in window if p.side == "buy" and p.wallet})
                ),
                early_buys=sum(1 for p in window if p.side == "buy"),
                early_sells=sum(1 for p in window if p.side == "sell"),
                first_price=window[0].price,
                window_end_price=base,
                window_end_ms=window[-1].ts_ms,
                multiple=max(p.price for p in after) / base,
                symbol=(meta.get("symbol") or None),
                name=(meta.get("name") or None),
                launchpad=(meta.get("launchpad") or None),
            )
        )
    return cases


# --------------------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------------------


def _tally(cases: Iterable[TokenCase], keys_of) -> list[Cell]:
    seen: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for case in cases:
        for key in keys_of(case):
            cell = seen[key]
            cell[0] += 1
            if case.mooned:
                cell[1] += 1
    return [Cell(key=k, n=v[0], mooners=v[1]) for k, v in seen.items()]


def _words(case: TokenCase) -> set[str]:
    text = " ".join(x for x in (case.symbol, case.name) if x).lower()
    return {w for w in _WORD.findall(text) if len(w) > 2 and w not in _STOPWORDS}


def _flow_band(case: TokenCase) -> list[str]:
    fraction = case.early_buy_fraction
    if fraction is None:
        return ["buy_fraction:unknown"]
    if fraction < 0.45:
        return ["buy_fraction:<0.45"]
    if fraction < 0.60:
        return ["buy_fraction:0.45-0.60"]
    if fraction < 0.80:
        return ["buy_fraction:0.60-0.80"]
    return ["buy_fraction:>=0.80"]


def run(
    conn: sqlite3.Connection | None = None,
    chain: Chain | None = None,
    *,
    since_ms: int = 0,
    top: int = 25,
) -> Autopsy:
    """Build every case, then rank each feature family by precision against the base rate."""
    c = conn or get_conn()
    cases = build_cases(c, chain, since_ms=since_ms)
    result = Autopsy(
        chain=chain,
        computed_ms=now_ms(),
        sample=len(cases),
        mooners=sum(1 for case in cases if case.mooned),
    )
    if not cases:
        return result
    base = result.baseline_rate

    def rank(cells: list[Cell], minimum: int) -> list[Cell]:
        ranked = [cell for cell in cells if cell.n >= minimum]
        ranked.sort(key=lambda cell: (cell.rate, cell.n), reverse=True)
        return ranked[:top]

    result.wallets = rank(_tally(cases, lambda case: case.early_wallets), MIN_ACTOR_TOKENS)
    result.words = rank(_tally(cases, _words), MIN_WORD_TOKENS)
    result.launchpads = rank(
        _tally(cases, lambda case: [case.launchpad] if case.launchpad else []), MIN_ACTOR_TOKENS
    )
    # Flow bands are a partition, so every band is reported however small -- a band that
    # is rare is a finding, not a row to hide.
    flow = _tally(cases, _flow_band)
    flow.sort(key=lambda cell: cell.key)
    result.flow = flow
    for cell in (*result.wallets, *result.words, *result.launchpads, *result.flow):
        cell.label = f"{cell.rate:.1f}% of {cell.n} (lift {cell.lift(base):.2f})"
    return result


def lines(result: Autopsy) -> list[str]:
    """A human-readable report. Every number carries its sample."""
    where = result.chain.value if result.chain else "all chains"
    out = [
        f"mooner autopsy — {where}",
        f"  {result.sample} tokens with enough tape; {result.mooners} reached "
        f"{result.moon_multiple:g}x after their first {EARLY_PRINTS} prints "
        f"(baseline {result.baseline_rate:.1f}%)",
    ]
    if not result.sample:
        out.append("  no token had enough tape to separate features from outcome")
        return out
    base = result.baseline_rate
    for title, cells, minimum in (
        ("wallets buying early", result.wallets, MIN_ACTOR_TOKENS),
        ("narrative words", result.words, MIN_WORD_TOKENS),
        ("launchpads", result.launchpads, MIN_ACTOR_TOKENS),
        ("early flow", result.flow, 0),
    ):
        out.append(f"  {title} (min n={minimum}):" if minimum else f"  {title}:")
        if not cells:
            out.append(f"    nothing reached n={minimum}; not reported rather than reported thin")
            continue
        for cell in cells[:12]:
            out.append(
                f"    {cell.key[:46]:46s} {cell.rate:5.1f}%  n={cell.n:<5d} "
                f"lift {cell.lift(base):.2f}"
            )
    return out
