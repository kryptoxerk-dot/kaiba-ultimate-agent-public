"""Import the operator's GMGN wallet exports into the tracked-wallet table.

## What this data actually is, which is not what the task sheet assumed

`docs/TASKS.md` P0-3 describes a 52k-line file of wallets whose names follow a convention
like ``SOL_5.2KUSD_30dPnL_wrNA``. That convention does not appear in the data even once.
What is actually there:

* ``gmgn wallets september 2026.txt`` — 5,792 records, **every one an EVM address**, with
  keys ``address``, ``name``, ``emoji``, ``sound`` and three ``alertsOn*`` booleans. The
  emoji field is empty on all 5,792.
* ``GMGN.txt`` — a notes file whose first lines are credentials, followed by 444 embedded
  ``{address, emoji, name}`` objects. It is not a single JSON document.

The names are free-form notes the operator typed while browsing, in mixed English and
Indonesian: ``FreshInsider?``, ``100 BNB PNL``, ``DEV SCAMMER``, ``Sniper GG juga``,
``55%WR``. Of 3,292 distinct labels, 99 mention PnL, 16 mention a sniper, 63 mention an
insider, and 2 mention a winrate. So structured extraction covers a small minority, and
the honest design is to keep the raw label, extract what is genuinely parseable, and say
plainly how much was parsed.

## The rule that matters

**An operator label is a hypothesis, not evidence.** A hand-typed "100 BNB PNL" is not an
on-chain measurement, and if it were allowed to feed the grader the agent would grade a
wallet highly on the strength of a note the operator wrote from memory, then copy-trade
it. So parsed values land in ``meta_json`` under an explicit ``operator_label`` basis and
in the ``research`` cohort by default. Promotion to ``tracked`` requires measured PnL from
`kaiba.intelligence.pnl`, which this module never writes.

Negative labels are the exception and they act immediately: a wallet the operator marked
as a scammer or a rug is routed to ``blacklist`` on import. Getting that wrong in the
cautious direction costs nothing; getting it wrong the other way means copy-trading a
known scammer.

## Credential handling

``GMGN.txt`` opens with credential lines. This module locates the first JSON object and
discards everything before it without storing, logging, emitting or returning any of it.
The skipped text never leaves the function that reads the file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core.schemas import (
    Chain,
    WalletTag,
    normalize_address,
    now_ms,
)

log = logging.getLogger(__name__)

SOURCE_EXPORT = "gmgn_export"
SOURCE_NOTES = "gmgn_notes"

#: Cohorts this module is allowed to assign. It may never write ``trusted_copy``: that
#: requires measured performance, not a label.
COHORT_RESEARCH = "research"
COHORT_BLACKLIST = "blacklist"

# ----------------------------------------------------------------- label vocabulary

#: Marker -> tag. Matched as whole words against a lowercased label. Ordered longest-first
#: at match time so ``suspected insider`` cannot be shadowed by ``insider``.
TAG_MARKERS: dict[str, WalletTag] = {
    "insider": WalletTag.INSIDER,
    "sniper": WalletTag.SNIPER,
    "snipe": WalletTag.SNIPER,
    "fresh": WalletTag.FRESH_WALLET,
    "dev": WalletTag.DEV,
    "deployer": WalletTag.DEV,
    "whale": WalletTag.TOP_HOLDER,
    "bundler": WalletTag.BUNDLER,
    "bundle": WalletTag.BUNDLER,
    "kol": WalletTag.KOL,
    "top trader": WalletTag.TOP_TRADER,
    "top_trader": WalletTag.TOP_TRADER,
    "toptrader": WalletTag.TOP_TRADER,
    "top holder": WalletTag.TOP_HOLDER,
    "early": WalletTag.EARLY_BUYER,
    "copybot": WalletTag.COPYBOT,
    "copy bot": WalletTag.COPYBOT,
    "smart money": WalletTag.SMART_MONEY,
    "smartmoney": WalletTag.SMART_MONEY,
    "side wallet": WalletTag.SIDE_WALLET,
    "side_wallet": WalletTag.SIDE_WALLET,
    "bot": WalletTag.DEX_BOT,
    "mev": WalletTag.MEV_BOT,
    "sandwich": WalletTag.SANDWICH_BOT,
    "exchange": WalletTag.EXCHANGE,
    "cex": WalletTag.EXCHANGE,
}

#: Markers that route a wallet straight to the blacklist. English and the Indonesian the
#: operator actually used in these files.
NEGATIVE_MARKERS: dict[str, WalletTag] = {
    "scammer": WalletTag.SCAMMER,
    "scam": WalletTag.SCAMMER,
    "rug": WalletTag.SCAMMER,
    "rugger": WalletTag.SCAMMER,
    "rugpull": WalletTag.SCAMMER,
    "wash": WalletTag.WASH_TRADER,
    "jangan": WalletTag.SCAMMER,  # id: "don't"
    "penipu": WalletTag.SCAMMER,  # id: "fraudster"
}

#: A trailing question mark, or one of these hedges, downgrades a claimed insider to a
#: suspected one and lowers the confidence we record.
HEDGE_MARKERS = ("?", "maybe", "mungkin", "kayak", "sepertinya", "harusnya", "probably")

#: Chain hints inside a label. An EVM address is valid on every EVM chain, so the label is
#: often the only thing telling us where the operator was looking.
CHAIN_HINTS: dict[str, Chain] = {
    "bnb": Chain.BSC,
    "bsc": Chain.BSC,
    "binance": Chain.BSC,
    "eth": Chain.ETH,
    "ethereum": Chain.ETH,
    "rh": Chain.ROBINHOOD,
    "robinhood": Chain.ROBINHOOD,
    "base": Chain.BASE,
    "sol": Chain.SOL,
    "solana": Chain.SOL,
}

_AMOUNT = re.compile(
    r"(?<![\w.])(?P<sign>-)?\$?\s*(?P<num>\d+(?:[.,]\d+)?)\s*"
    r"(?P<suffix>[km])?\s*(?P<unit>usd|\$|bnb|eth|e\+|e\b|sol)?",
    re.I,
)
_WINRATE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:wr|winrate|win\s*rate)?", re.I)
_PNL_NEARBY = re.compile(r"pnl|profit|gain", re.I)
_CAMEL_SPLIT = re.compile(r"([a-z0-9])([A-Z])")

#: GMGN's own export convention, which appears 277 times in the notes file and never in
#: the big export: ``SOL_5.2KUSD_30dPnL_wrNA`` or ``SOL_12.4KUSD_7dPnL_63wr``. Unlike a
#: hand-typed note this is a machine-written figure, so it is worth reading exactly rather
#: than letting the fuzzy parser approximate it.
_STRUCTURED = re.compile(
    r"^(?P<chain>SOL|ETH|BSC|BASE|RH|ARB|BNB)_"
    r"(?P<amt>\d+(?:\.\d+)?)(?P<mult>[KMB]?)USD_"
    r"(?P<days>\d+)d(?:PnL|Pnl|PNL)_"
    r"(?:wr(?P<wrna>NA)|(?P<wr>\d+(?:\.\d+)?)wr)$",
    re.I,
)
_MULT = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


@dataclass(frozen=True)
class LabelFacts:
    """Everything we could honestly extract from one operator label."""

    raw: str
    tags: frozenset[WalletTag] = frozenset()
    chain_hint: Chain | None = None
    pnl_amount: Decimal | None = None
    pnl_unit: str | None = None
    winrate_pct: Decimal | None = None
    hedged: bool = False
    negative: bool = False
    #: Set only by the exact GMGN convention, never by the fuzzy parser.
    pnl_window_days: int | None = None
    structured: bool = False

    @property
    def parsed_anything(self) -> bool:
        return bool(
            self.tags or self.chain_hint or self.pnl_amount or self.winrate_pct
        )


def _scale(num: str, suffix: str | None) -> Decimal:
    value = Decimal(num.replace(",", "."))
    if suffix and suffix.lower() == "k":
        value *= 1000
    elif suffix and suffix.lower() == "m":
        value *= 1_000_000
    return value


def _parse_structured(text: str) -> LabelFacts | None:
    """Read GMGN's exact export convention, or return None so the fuzzy parser runs.

    This is the one place in the importer where a number is machine-written rather than
    remembered, so it is read strictly: a label that does not match the whole pattern is
    handed on rather than half-parsed.
    """
    m = _STRUCTURED.match(text.strip())
    if m is None:
        return None
    amount = Decimal(m.group("amt")) * _MULT[(m.group("mult") or "").upper()]
    winrate = None if m.group("wrna") else Decimal(m.group("wr"))
    if winrate is not None and winrate > 100:
        winrate = None
    return LabelFacts(
        raw=text,
        chain_hint=CHAIN_HINTS.get(m.group("chain").lower()),
        pnl_amount=amount,
        pnl_unit="usd",
        pnl_window_days=int(m.group("days")),
        winrate_pct=winrate,
        structured=True,
    )


def parse_label(raw: str | None) -> LabelFacts:
    """Extract tags, a chain hint and any PnL/winrate figure from a free-form label.

    Deliberately conservative. A label we cannot read returns empty facts rather than a
    guess, because a wrong tag here propagates into cohort membership and archetype.
    """
    text = (raw or "").strip()
    if not text:
        return LabelFacts(raw="")
    # GMGN's own export convention is exact, so read it exactly before guessing.
    exact = _parse_structured(text)
    if exact is not None:
        return exact
    # The operator runs words together (``FreshInsider?``, ``ChineseInsider?``), and a
    # word-boundary guard would otherwise miss the second word entirely. Split on the case
    # transition before matching, but keep ``text`` intact for the numeric parsing below.
    low = _CAMEL_SPLIT.sub(r"\1 \2", text).lower()

    negative = any(m in low for m in NEGATIVE_MARKERS)
    tags: set[WalletTag] = set()
    for marker, tag in NEGATIVE_MARKERS.items():
        if marker in low:
            tags.add(tag)

    hedged = any(h in low for h in HEDGE_MARKERS)
    for marker in sorted(TAG_MARKERS, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(marker)}", low):
            tag = TAG_MARKERS[marker]
            if tag is WalletTag.INSIDER and hedged:
                tag = WalletTag.SUSPECTED_INSIDER
            tags.add(tag)

    chain_hint: Chain | None = None
    for marker, chain in CHAIN_HINTS.items():
        if re.search(rf"(?<![a-z]){re.escape(marker)}(?![a-z])", low):
            chain_hint = chain
            break

    winrate: Decimal | None = None
    wr = _WINRATE.search(text)
    if wr:
        candidate = Decimal(wr.group(1))
        if candidate <= 100:
            winrate = candidate

    pnl_amount: Decimal | None = None
    pnl_unit: str | None = None
    if _PNL_NEARBY.search(low) or "$" in text:
        for m in _AMOUNT.finditer(text):
            unit = (m.group("unit") or "").lower().rstrip("+")
            if not unit and "$" not in m.group(0):
                continue
            if wr is not None and m.group("num") == wr.group(1):
                continue  # that number is the winrate we already took, not a PnL
            pnl_amount = _scale(m.group("num"), m.group("suffix"))
            if m.group("sign"):
                pnl_amount = -pnl_amount
            pnl_unit = {"$": "usd", "e": "eth", "": "usd"}.get(unit, unit)
            break

    return LabelFacts(
        raw=text,
        tags=frozenset(tags),
        chain_hint=chain_hint,
        pnl_amount=pnl_amount,
        pnl_unit=pnl_unit,
        winrate_pct=winrate,
        hedged=hedged,
        negative=negative,
    )


# --------------------------------------------------------------------- file readers


def load_export(path: str | Path) -> list[dict[str, Any]]:
    """Read a plain JSON array export. Raises only if the file is not that."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a JSON array")
    return [d for d in data if isinstance(d, dict) and d.get("address")]


_EMBEDDED = re.compile(r'\{[^{}]*"address"\s*:\s*"[^"]+"[^{}]*\}')


def load_notes(path: str | Path) -> list[dict[str, Any]]:
    """Read a notes file whose opening lines are credentials.

    Everything before the first JSON object is dropped here and never returned, logged,
    stored or emitted. Do not change this to report what it skipped.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        return []
    body = text[start:]
    del text  # the credential preamble does not outlive this call

    out: list[dict[str, Any]] = []
    for match in _EMBEDDED.finditer(body):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("address"):
            out.append(obj)
    return out


# ------------------------------------------------------------------------- importing


@dataclass
class ImportReport:
    """What the import actually did, in numbers an operator can check."""

    seen: int = 0
    imported: int = 0
    updated: int = 0
    skipped_bad_address: int = 0
    blacklisted: int = 0
    #: EVM rows whose chain is the default, not something the label actually said.
    chain_defaulted: int = 0
    by_chain: dict[str, int] = field(default_factory=dict)
    by_tag: dict[str, int] = field(default_factory=dict)
    labels_total: int = 0
    labels_parsed: int = 0
    labels_structured: int = 0

    @property
    def parse_rate(self) -> float:
        return (self.labels_parsed / self.labels_total) if self.labels_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "imported": self.imported,
            "updated": self.updated,
            "skipped_bad_address": self.skipped_bad_address,
            "blacklisted": self.blacklisted,
            "chain_defaulted": self.chain_defaulted,
            "chain_defaulted_note": (
                "EVM addresses are valid on every EVM chain; these rows took the default "
                "because no label named a chain. Treat their chain as unverified."
            ),
            "by_chain": dict(sorted(self.by_chain.items())),
            "by_tag": dict(sorted(self.by_tag.items(), key=lambda kv: -kv[1])),
            "labels_total": self.labels_total,
            "labels_parsed": self.labels_parsed,
            "labels_structured": self.labels_structured,
            "parse_rate": round(self.parse_rate, 4),
        }


_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _chain_for(address: str, hint: Chain | None, default_evm: Chain) -> Chain | None:
    """EVM addresses are chain-ambiguous, so a label hint beats the default."""
    if _EVM_RE.match(address):
        if hint is not None and hint is not Chain.SOL:
            return hint
        return default_evm
    if _B58_RE.match(address):
        return Chain.SOL
    return None


def import_records(
    records: list[dict[str, Any]],
    *,
    source: str,
    conn: Any = None,
    default_evm_chain: Chain = Chain.ETH,
    dry_run: bool = False,
) -> ImportReport:
    """Normalise and upsert wallet records. Idempotent: re-running updates, never duplicates."""
    from kaiba.core.db import ensure_db, fetch_one, jdump, upsert

    c = conn if conn is not None else ensure_db()
    rep = ImportReport()
    ts = now_ms()

    for rec in records:
        rep.seen += 1
        raw_addr = str(rec.get("address") or "").strip()
        label = (rec.get("name") or "").strip()
        facts = parse_label(label)
        if label:
            rep.labels_total += 1
            if facts.parsed_anything:
                rep.labels_parsed += 1
            if facts.structured:
                rep.labels_structured += 1

        chain = _chain_for(raw_addr, facts.chain_hint, default_evm_chain)
        if chain is not None and _EVM_RE.match(raw_addr) and facts.chain_hint is None:
            rep.chain_defaulted += 1
        if chain is None:
            rep.skipped_bad_address += 1
            continue
        try:
            address = normalize_address(raw_addr, chain)
        except Exception:  # noqa: BLE001 - a malformed address is data, not a crash
            rep.skipped_bad_address += 1
            continue

        cohort = COHORT_BLACKLIST if facts.negative else COHORT_RESEARCH
        if facts.negative:
            rep.blacklisted += 1
        rep.by_chain[chain.value] = rep.by_chain.get(chain.value, 0) + 1
        for tag in facts.tags:
            rep.by_tag[tag.value] = rep.by_tag.get(tag.value, 0) + 1

        # Parsed figures are the operator's recollection, not a measurement. They are kept
        # for triage and are deliberately not written anywhere the grader reads.
        # A machine-written GMGN figure is a better basis than a remembered one, but it is
        # still an export from an unknown moment, so neither ever reaches the grader.
        basis = "gmgn_export_label" if facts.structured else "operator_label"
        meta: dict[str, Any] = {"operator_label": label, "label_basis": basis}
        if facts.pnl_amount is not None:
            meta["label_pnl"] = {
                "amount": str(facts.pnl_amount),
                "unit": facts.pnl_unit,
                "window_days": facts.pnl_window_days,
            }
        if facts.winrate_pct is not None:
            meta["label_winrate_pct"] = str(facts.winrate_pct)
        if facts.hedged:
            meta["label_hedged"] = True
        if facts.chain_hint:
            meta["label_chain_hint"] = facts.chain_hint.value
        elif _EVM_RE.match(raw_addr):
            meta["chain_is_default_not_observed"] = True

        if dry_run:
            continue

        existing = fetch_one(
            c, "SELECT address, tags_json, cohort FROM wallets WHERE chain=? AND address=?",
            (chain.value, address),
        )
        tags = sorted({t.value for t in facts.tags})
        if existing:
            prior = set(json.loads(existing["tags_json"] or "[]"))
            tags = sorted(prior | set(tags))
            # A blacklisting sticks; a later neutral import must not quietly clear it.
            if existing["cohort"] == COHORT_BLACKLIST:
                cohort = COHORT_BLACKLIST
            rep.updated += 1
        else:
            rep.imported += 1

        upsert(
            c,
            "wallets",
            {
                "chain": chain.value,
                "address": address,
                "name": label or None,
                "source": source,
                "tags_json": jdump(tags),
                "first_seen_ms": ts,
                "last_seen_ms": ts,
                "cohort": cohort,
                "meta_json": jdump(meta),
            },
            conflict=["chain", "address"],
            update=["name", "source", "tags_json", "last_seen_ms", "cohort", "meta_json"],
        )

    return rep


def import_files(
    export_path: str | Path | None = None,
    notes_path: str | Path | None = None,
    *,
    conn: Any = None,
    default_evm_chain: Chain = Chain.ETH,
    dry_run: bool = False,
) -> ImportReport:
    """Import both known export shapes, merging their reports."""
    total = ImportReport()
    jobs: list[tuple[list[dict[str, Any]], str]] = []
    if export_path:
        jobs.append((load_export(export_path), SOURCE_EXPORT))
    if notes_path:
        jobs.append((load_notes(notes_path), SOURCE_NOTES))

    for records, source in jobs:
        rep = import_records(
            records, source=source, conn=conn,
            default_evm_chain=default_evm_chain, dry_run=dry_run,
        )
        total.seen += rep.seen
        total.imported += rep.imported
        total.updated += rep.updated
        total.skipped_bad_address += rep.skipped_bad_address
        total.blacklisted += rep.blacklisted
        total.chain_defaulted += rep.chain_defaulted
        total.labels_total += rep.labels_total
        total.labels_parsed += rep.labels_parsed
        total.labels_structured += rep.labels_structured
        for k, v in rep.by_chain.items():
            total.by_chain[k] = total.by_chain.get(k, 0) + v
        for k, v in rep.by_tag.items():
            total.by_tag[k] = total.by_tag.get(k, 0) + v
    return total
