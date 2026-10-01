"""Archetype inference and the names we hang on wallets.

Names are the operator's interface to the wallet graph: when 5,792 addresses land in a
GMGN follow list, `Early Buyer MCAT A` is the difference between a usable list and noise.
So naming has to be **deterministic** — the same evidence must produce the same string
every run, or diffs between exports become unreadable and the follow list churns.

Archetype precedence is ordered by how much the label constrains what we should do with
the wallet. Disqualifying labels come first (a bot that also holds bluechips is still a
bot), then origin labels (bundler, dev), then edge labels (insider, sniper, early buyer),
then behavioural ones. The first rule that matches wins; nothing further is consulted.

Two naming surfaces live here, and the difference between them is why the second exists.

1. :func:`build_name` — ``"<Role> <SEED> <Grade>"``, the GMGN follow-list name. It takes a
   :class:`WalletScore`, so a wallet cannot be named until it has been graded, and grading
   is paid Helius backfill throttled to 15 wallets a day. MEASURED 2026-09-21 on the live
   box: 27 ``wallet_scores`` rows ever, 0 rows in ``wallets``, and no call site for
   :func:`build_name` anywhere outside the tests. Nothing was ever named because the only
   name needed a grade first.
2. The **registry name** (:func:`registry_name`, :func:`name_wallets`) — built from the
   evidence the database already holds for tens of thousands of wallets that will never be
   paid for: GMGN cohort tags on the ``wallet.trade`` events, the entity graph, the
   launch-window roles in ``token_bundle_members``, buy/sell asymmetry in ``swaps`` and a
   grade when one exists. It is written into ``wallets`` — the canonical registry, empty on
   the live box until this ran — and is what ``kaiba_wallet`` / ``kaiba_wallets`` show.

The registry name never asserts quality we did not grade: ``smart_degen`` in a name is
always attributed to GMGN in the ``[gmgn:...]`` bracket, and ``grade X`` appears only when
``wallet_scores`` holds an A-D. The same rule holds for ``tags_json``: a GMGN label is
written as ``gmgn:<label>`` and never as a word the lanes or the grader read as a finding
(``smart_money``, ``pump_smart``, ``kol`` ...). Only a path that screened the wallet —
``tracker.seed_from_cohorts`` — may spell those, and naming is not that path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload, tx
from kaiba.core.schemas import Archetype, Chain, Grade, WalletScore, WalletTag, now_ms
from kaiba.intelligence.hubs import safe_normalize

if TYPE_CHECKING:  # avoids a runtime cycle: grade imports naming for the archetype
    from kaiba.intelligence.grade import WalletEvidence

log = logging.getLogger(__name__)

#: GMGN accepts at most this many rows per imported follow list.
GMGN_CHUNK_SIZE = 2000

#: Exactly the keys GMGN round-trips in its follow-list export, in its own order.
GMGN_ROW_KEYS: tuple[str, ...] = (
    "address",
    "name",
    "emoji",
    "alertsOnToast",
    "alertsOnFeed",
    "alertsOnBubble",
    "sound",
)

ROLE_LABELS: dict[Archetype, str] = {
    Archetype.BOT: "Bot",
    Archetype.BUNDLER: "Bundler",
    Archetype.DEV: "Dev",
    Archetype.KOL: "KOL",
    Archetype.INSIDER: "Insider",
    Archetype.SNIPER: "Sniper",
    Archetype.EARLY_BUYER: "Early Buyer",
    Archetype.SIDE_WALLET: "Side Wallet",
    Archetype.COPYBOT: "Copybot",
    Archetype.TOP_TRADER: "Top Trader",
    Archetype.TOP_HOLDER: "Top Holder",
    Archetype.SMART_MONEY: "Smart Money",
    Archetype.DIAMOND: "Diamond",
    Archetype.FOMO: "FOMO",
    Archetype.POSITION_HOLDER: "Position Holder",
    Archetype.TRADER: "Trader",
}

BOT_TAGS: frozenset[WalletTag] = frozenset(
    {WalletTag.MEV_BOT, WalletTag.SANDWICH_BOT, WalletTag.DEX_BOT}
)

KOL_FOLLOWER_FLOOR = 5_000
INSIDER_TOKEN_FLOOR = 3
SNIPER_TOKEN_FLOOR = 5
SNIPER_HOLD_S = 300
SNIPER_TOKEN_BREADTH = 40
EARLY_BUYER_RANK = 10
DIAMOND_HOLD_S = 86_400
DIAMOND_WIN_RATE = 0.5
POSITION_HOLDER_SELL_RATIO = 0.2

_HANDLE_RE = re.compile(r"[^A-Za-z0-9_]")
_SPACES_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------------------
# archetype
# --------------------------------------------------------------------------------------


def infer_archetype(evidence: WalletEvidence, *, lead_lag_of: str | None = None) -> Archetype:
    """First matching rule wins. ``lead_lag_of`` comes from the clustering pass.

    Passing ``lead_lag_of`` explicitly overrides the value carried on the evidence, so a
    caller that has just computed a lead/lag edge does not have to rebuild the evidence.
    """
    ev = evidence
    tags = set(ev.tags)
    rep = ev.reputation
    em = ev.early_metrics
    pnl = ev.pnl
    follows = lead_lag_of if lead_lag_of is not None else ev.lead_lag_of
    followers = (rep.followers if rep is not None else None) or 0
    verified = bool(rep is not None and rep.verified)

    if tags & BOT_TAGS:
        return Archetype.BOT
    if WalletTag.BUNDLER in tags:
        return Archetype.BUNDLER
    if ev.created_token_count > 0 or WalletTag.DEV in tags:
        return Archetype.DEV
    if (verified and followers > KOL_FOLLOWER_FLOOR) or WalletTag.KOL in tags or bool(rep and rep.kol):
        return Archetype.KOL
    if (em is not None and em.insider_tokens >= INSIDER_TOKEN_FLOOR) or tags & {
        WalletTag.INSIDER,
        WalletTag.SUSPECTED_INSIDER,
    }:
        return Archetype.INSIDER
    if _is_sniper(ev, tags, em):
        return Archetype.SNIPER
    if em is not None and em.best_entry_rank is not None and em.best_entry_rank <= EARLY_BUYER_RANK:
        return Archetype.EARLY_BUYER
    if WalletTag.EARLY_BUYER in tags:
        return Archetype.EARLY_BUYER
    if follows:
        return Archetype.SIDE_WALLET
    if WalletTag.COPYBOT in tags:
        return Archetype.COPYBOT
    if WalletTag.TOP_TRADER in tags:
        return Archetype.TOP_TRADER
    if WalletTag.TOP_HOLDER in tags:
        return Archetype.TOP_HOLDER
    if tags & {WalletTag.SMART_MONEY, WalletTag.PUMP_SMART, WalletTag.RENOWNED}:
        return Archetype.SMART_MONEY
    if _is_diamond(ev, tags, pnl):
        return Archetype.DIAMOND
    if ev.fomo_flag or WalletTag.FOMO in tags:
        return Archetype.FOMO
    if _is_position_holder(ev, tags):
        return Archetype.POSITION_HOLDER
    return Archetype.TRADER


def _is_sniper(ev: WalletEvidence, tags: set[WalletTag], em: Any) -> bool:
    if WalletTag.SNIPER in tags:
        return True
    if em is not None and em.sniper_tokens >= SNIPER_TOKEN_FLOOR:
        return True
    hold = ev.median_hold_s
    tokens = ev.distinct_tokens
    return hold is not None and hold < SNIPER_HOLD_S and tokens is not None and tokens > SNIPER_TOKEN_BREADTH


def _is_diamond(ev: WalletEvidence, tags: set[WalletTag], pnl: Any) -> bool:
    hold = ev.median_hold_s
    win_rate = pnl.win_rate if pnl is not None else None
    if hold is not None and hold > DIAMOND_HOLD_S and win_rate is not None and win_rate > DIAMOND_WIN_RATE:
        return True
    return WalletTag.DIAMOND_HAND in tags and (win_rate or 0) > DIAMOND_WIN_RATE


def _is_position_holder(ev: WalletEvidence, tags: set[WalletTag]) -> bool:
    if WalletTag.POSITION_HOLDER in tags:
        return True
    ratio = ev.sell_to_buy_ratio
    buys, _ = ev.trade_counts
    return ratio is not None and ratio < POSITION_HOLDER_SELL_RATIO and (buys or 0) > 3


# --------------------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------------------


def role_label(archetype: Archetype) -> str:
    return ROLE_LABELS.get(archetype, "Trader")


def _handle(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = _HANDLE_RE.sub("", raw.strip().lstrip("@"))
    return f"@{cleaned}" if cleaned else None


def build_name(evidence: WalletEvidence, score: WalletScore, seed_symbol: str | None = None) -> str:
    """``"<Role> <SEED> <Grade>"``, e.g. ``"Early Buyer MCAT A"``.

    A verified KOL is named by their handle instead of a seed token — ``"KOL @handle A"`` —
    because the handle is the thing an operator recognises. Pure function of its inputs.
    """
    archetype = score.archetype
    label = role_label(archetype)
    handle = _handle(evidence.reputation.twitter if evidence.reputation is not None else None)
    verified = bool(evidence.reputation is not None and evidence.reputation.verified)

    if archetype is Archetype.KOL and handle and verified:
        middle = handle
    elif seed_symbol:
        middle = seed_symbol.strip().upper()
    else:
        middle = ""

    grade = score.grade.value if isinstance(score.grade, Grade) else str(score.grade)
    return _SPACES_RE.sub(" ", f"{label} {middle} {grade}").strip()


# --------------------------------------------------------------------------------------
# GMGN follow-list export
# --------------------------------------------------------------------------------------


def _address_and_name(item: Any) -> tuple[str, str]:
    """Accept a WalletScore, a ``(score_or_address, name)`` pair, or a mapping."""
    if isinstance(item, Mapping):
        return str(item.get("address") or "").strip(), str(item.get("name") or "").strip()
    if isinstance(item, WalletScore):
        return item.address, f"{role_label(item.archetype)} {item.grade.value}"
    if isinstance(item, Sequence) and not isinstance(item, str | bytes) and len(item) == 2:
        first, name = item
        address = first.address if isinstance(first, WalletScore) else str(first)
        return address.strip(), str(name).strip()
    raise TypeError(f"cannot turn {type(item).__name__} into a GMGN import row")


def gmgn_import_rows(scores: Iterable[Any], *, group: str | None = None) -> list[dict[str, Any]]:
    """GMGN's follow-list round-trip shape, one row per wallet.

    Items may be :class:`WalletScore` objects, ``(score_or_address, name)`` pairs, or
    mappings that already carry ``address``/``name``. Duplicate addresses keep their first
    occurrence so re-exporting a superset never reorders the file.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in scores:
        address, name = _address_and_name(item)
        if not address or address in seen:
            continue
        seen.add(address)
        row: dict[str, Any] = {
            "address": address,
            "name": name,
            "emoji": "",
            "alertsOnToast": True,
            "alertsOnFeed": True,
            "alertsOnBubble": True,
            "sound": "default",
        }
        if group is not None:
            row["group"] = group
        rows.append(row)
    return rows


def write_gmgn_import(
    rows: Sequence[Mapping[str, Any]],
    out_dir: str | Path,
    *,
    basename: str = "gmgn-import",
    chunk_size: int = GMGN_CHUNK_SIZE,
) -> list[Path]:
    """Write the JSON follow list plus the ``address:name`` text variant.

    Lists longer than ``chunk_size`` are split into ``<basename>-part1of3.json`` and so on,
    because GMGN silently truncates a larger import.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    size = max(1, chunk_size)
    chunks = [list(rows[i : i + size]) for i in range(0, len(rows), size)] or [[]]
    total = len(chunks)

    written: list[Path] = []
    for index, chunk in enumerate(chunks, start=1):
        stem = basename if total == 1 else f"{basename}-part{index}of{total}"
        json_path = out / f"{stem}.json"
        json_path.write_text(json.dumps(chunk, indent=2, ensure_ascii=False), encoding="utf-8")
        lines = [f"{r['address']}:{r.get('name', '')}" for r in chunk]
        text_path = out / f"{stem}.txt"
        text_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        written.extend((json_path, text_path))
    return written


# --------------------------------------------------------------------------------------
# registry naming: a handle for every wallet we hold evidence on, graded or not
# --------------------------------------------------------------------------------------
#
# Shape: ``<archetype>#<id> [gmgn:<tags>] (entity <id>, <n> members) grade <G> "<prior>"``
#
#   sniper#a3f2c1 [gmgn:smart_degen,arbitrager] (entity ec0941, 3 members) grade B
#   unknown#0c1d2e
#
# * ``archetype`` is the most constraining thing we can say, first match in
#   ARCHETYPE_PRECEDENCE. Our own measurements (swaps asymmetry, launch-window roles,
#   token creation, a stored grade's archetype) claim a word before a provider tag does,
#   and ``archetype_basis`` in meta says which one did.
# * ``[gmgn:...]`` is GMGN's cohort claim, verbatim and attributed. A quality word such as
#   ``smart_degen`` never appears in a name without this attribution.
# * ``(entity ...)`` reflects ``entity_members``; it disappears when membership does.
# * ``grade`` is present only for an A-D in ``wallet_scores``; UNSCORED says nothing.
# * ``"<prior>"`` keeps the operator's own label when the registry already had one.

#: v2: GMGN labels land in ``tags_json`` as ``gmgn:<label>``. v1 mapped smart_degen /
#: app_smart_money / launchpad_smart onto ``smart_money`` / ``pump_smart`` — the exact
#: words ``lanes.SMART_TAGS`` and ``grade.POSITIVE_REPUTATION_TAGS`` read — and wrote every
#: other label bare, so ``kol`` or ``renowned`` off a feed would have scored reputation.
#: v1 never ran against the live registry (``wallets`` had 0 rows on 2026-09-21).
NAMING_VERSION = "naming-v2"
#: Hex characters of the (chain, address) digest that make the handle.
SHORT_ID_LEN = 6
#: Hex characters of the entity id shown in a name.
ENTITY_ID_LEN = 6
#: GMGN cohort tags shown in a name; the full list is in ``tags_json`` and meta.
MAX_COHORT_TAGS_IN_NAME = 3
#: Characters of an operator's prior label carried into the name.
PRIOR_NAME_MAX = 40
#: Launch-window roles on at least this many tokens carry the role tag and archetype.
BUNDLE_ROLE_MIN_TOKENS = 1

REGISTRY_ARCHETYPE_UNKNOWN = "unknown"
REGISTRY_ARCHETYPE_SELL_ONLY = "sell-only"
SELL_ONLY_TAG = "sell_only"

#: Most constraining first. A wallet that is a launch sniper and a GMGN smart_degen is
#: named sniper; the smart_degen claim stays visible in the bracket.
ARCHETYPE_PRECEDENCE: tuple[str, ...] = (
    REGISTRY_ARCHETYPE_SELL_ONLY,
    "bot",
    "wash",
    "creator",
    "bundler",
    "sniper",
    "insider",
    "early_buyer",
    "kol",
    "side_wallet",
    "copybot",
    "arbitrager",
    "smart_degen",
    "top_trader",
    "top_holder",
    "position_holder",
    "diamond",
    "fomo",
    REGISTRY_ARCHETYPE_UNKNOWN,
)

#: GMGN labels that say which client the wallet trades through. Kept in meta, never in a
#: name: "trades via Trojan" is not a cohort.
GMGN_APP_TAGS: frozenset[str] = frozenset(
    {
        "gmgn", "gmgn_go", "gmgn_app", "axiom", "padre", "photon", "trojan", "bullx",
        "pepeboost", "bonkbot", "maestro", "banana", "nova", "bloom", "unibot",
        "sol_trading_bot", "telegram_bot",
    }
)

#: GMGN cohort labels in display order. A label GMGN sends that is not listed here and is
#: not an app tag is kept after these, alphabetically — a new cohort must not be lost.
GMGN_COHORT_ORDER: tuple[str, ...] = (
    "smart_degen", "launchpad_smart", "app_smart_money", "pump_smart", "renowned", "kol",
    "top_followed", "top_renamed", "arbitrager", "sniper", "bluechip_owner", "fresh_wallet",
    "fomo", "wash_trader", "sandwich_bot", "mev_bot", "dex_bot", "rat_trader", "scammer",
    "transfer_in", "bundler", "dev", "insider", "suspected_insider", "paper_hand",
    "diamond_hand",
)

#: Every GMGN label the registry writes into ``tags_json`` carries this prefix:
#: ``gmgn:smart_degen``, ``gmgn:wash_trader``, ``gmgn:kol``. Nothing here maps a label onto
#: the bare vocabulary the money path reads — ``lanes.SMART_TAGS`` and
#: ``grade.POSITIVE_REPUTATION_TAGS`` (smart_money, pump_smart, renowned, top_trader, kol,
#: bluechip_owner) — because a label is a vendor's opinion with no published method, not a
#: screen. MEASURED 2026-09-21: the feeds had labelled 1,741 wallets, 18 of them
#: wash_trader / sandwich_bot. ``grade._tags_from`` reads the namespace back and admits a
#: label only when it can lower the wallet (``grade.VENDOR_ADMITTED_TAGS``).
GMGN_TAG_PREFIX = "gmgn:"

#: GMGN label -> archetype candidate. Anything absent contributes a tag but no archetype.
GMGN_ARCHETYPE: dict[str, str] = {
    "sandwich_bot": "bot",
    "mev_bot": "bot",
    "dex_bot": "bot",
    "wash_trader": "wash",
    "dev": "creator",
    "bundler": "bundler",
    "sniper": "sniper",
    "insider": "insider",
    "suspected_insider": "insider",
    "kol": "kol",
    "arbitrager": "arbitrager",
    "smart_degen": "smart_degen",
    "launchpad_smart": "smart_degen",
    "app_smart_money": "smart_degen",
    "fomo": "fomo",
}

BUNDLE_ROLE_ARCHETYPE: dict[str, str] = {"creator": "creator", "bundler": "bundler", "sniper": "sniper"}
BUNDLE_ROLE_TAG: dict[str, WalletTag] = {
    "creator": WalletTag.DEV,
    "bundler": WalletTag.BUNDLER,
    "sniper": WalletTag.SNIPER,
}

#: ``wallet_scores.archetype`` -> registry word. ``trader`` is the grader's "nothing
#: else matched" and carries no information, so it maps to nothing.
SCORE_ARCHETYPE: dict[str, str | None] = {
    **{a.value: a.value for a in Archetype},
    Archetype.DEV.value: "creator",
    Archetype.SMART_MONEY.value: "smart_degen",
    Archetype.TRADER.value: None,
}

#: Words that claim the wallet is good at this. A name may carry one only when it is
#: attributed to GMGN in the bracket or accompanied by a grade we computed.
QUALITY_WORDS: frozenset[str] = frozenset(
    {
        "smart_degen", "smart_money", "launchpad_smart", "app_smart_money", "pump_smart",
        "renowned", "top_trader", "top_holder", "top_followed", "top_renamed",
        "bluechip_owner", "diamond",
    }
)

GRADED_LETTERS: frozenset[str] = frozenset({Grade.A.value, Grade.B.value, Grade.C.value, Grade.D.value})

#: Every numeric knob in this module says where it came from. A test asserts coverage.
THRESHOLD_PROVENANCE: dict[str, str] = {
    "GMGN_CHUNK_SIZE": "OPERATIONAL. GMGN silently truncates a follow-list import above 2,000 rows.",
    "KOL_FOLLOWER_FLOOR": "INVENTED, inherited from the grade.mjs scorer. Never swept against outcomes.",
    "INSIDER_TOKEN_FLOOR": "INVENTED, inherited from wallet-grading/05_metrics.py. Never swept.",
    "SNIPER_TOKEN_FLOOR": "INVENTED, inherited from wallet-grading/05_metrics.py. Never swept.",
    "SNIPER_HOLD_S": "INVENTED. Five minutes is the same window 05_metrics.py used for sniper_tokens.",
    "SNIPER_TOKEN_BREADTH": "INVENTED, inherited from the grade.mjs scorer. Never swept.",
    "EARLY_BUYER_RANK": "INVENTED. Top-10 entrant; the 300 s early-count edge (AUC 0.82-0.92) is a "
    "token-level finding, not a per-wallet rank cut.",
    "DIAMOND_HOLD_S": "DEFINITIONAL. One day.",
    "DIAMOND_WIN_RATE": "INVENTED, inherited from the copyability rubric. Never swept.",
    "POSITION_HOLDER_SELL_RATIO": "INVENTED, inherited from the copyability rubric. Never swept.",
    "SHORT_ID_LEN": "INVENTED length. Six hex characters is 16.7M values; MEASURED 69,594 distinct "
    "(chain, wallet) pairs on the live box 2026-09-21 give an expected ~145 colliding pairs. A handle "
    "is a label; (chain, address) stays the identity, and resolve_handle returns every match.",
    "ENTITY_ID_LEN": "INVENTED length. The entity id is already a 16-hex digest of its member set; six "
    "characters keeps a Telegram line short and entity_id in meta is exact.",
    "MAX_COHORT_TAGS_IN_NAME": "INVENTED. Three keeps a line readable; tags_json holds all of them.",
    "PRIOR_NAME_MAX": "INVENTED. Operator labels in the import run to whole sentences; forty characters "
    "keeps the handle readable and the full label stays in meta.",
    "BUNDLE_ROLE_MIN_TOKENS": "STRUCTURAL basis, INVENTED count. A launch-window role is a buy inside "
    "bundles.LAUNCH_WINDOW_SLOTS (1.0 s at the 250 ms slot time), which is not a hand-placed trade, so "
    "one token is enough to carry the role tag; the per-role token count is in meta for anyone who "
    "wants a higher bar.",
}


@dataclass
class WalletFacts:
    """Everything the registry namer looks at for one wallet. ``None`` means unmeasured."""

    chain: Chain
    address: str
    #: GMGN label -> number of ``wallet.trade`` events carrying it (apps and cohorts alike).
    gmgn_tags: dict[str, int] = field(default_factory=dict)
    gmgn_feeds: dict[str, int] = field(default_factory=dict)
    #: launch-window role -> distinct tokens it was seen on.
    bundle_roles: dict[str, int] = field(default_factory=dict)
    entity_id: str | None = None
    entity_size: int | None = None
    score_archetype: str | None = None
    grade: str | None = None
    observed_buys: int | None = None
    observed_sells: int | None = None
    observed_tokens: int | None = None
    #: ``swaps.source`` -> rows.
    sources: dict[str, int] = field(default_factory=dict)
    first_seen_ms: int | None = None
    last_seen_ms: int | None = None
    created_tokens: int | None = None
    #: The ``wallets`` row as it stands, or ``None`` when the registry has never seen it.
    existing: dict[str, Any] | None = None


@dataclass
class NamingReport:
    dry_run: bool
    wallets_before: int
    wallets_after: int
    considered: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_unknown_chain: int = 0
    by_archetype: dict[str, int] = field(default_factory=dict)
    by_chain: dict[str, int] = field(default_factory=dict)
    by_basis: dict[str, int] = field(default_factory=dict)
    with_gmgn_cohort: int = 0
    in_entity: int = 0
    graded: int = 0
    sell_only: int = 0
    samples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": NAMING_VERSION,
            "dry_run": self.dry_run,
            "wallets_before": self.wallets_before,
            "wallets_after": self.wallets_after,
            "considered": self.considered,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped_unknown_chain": self.skipped_unknown_chain,
            "by_archetype": dict(sorted(self.by_archetype.items())),
            "by_chain": dict(sorted(self.by_chain.items())),
            "by_basis": dict(sorted(self.by_basis.items())),
            "with_gmgn_cohort": self.with_gmgn_cohort,
            "in_entity": self.in_entity,
            "graded": self.graded,
            "sell_only": self.sell_only,
            "samples": list(self.samples),
        }


# ------------------------------------------------------------------ pure pieces


def short_id(chain: Chain, address: str) -> str:
    """Six hex characters of sha256(``chain:address``). Stable, chain-aware, never reused."""
    key = f"{chain.value}:{safe_normalize(address, chain)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:SHORT_ID_LEN]


def entity_handle(entity_id: str) -> str:
    """``sol:ent:ec0941ded8a7ebec`` -> ``ec0941``."""
    return entity_id.rsplit(":", 1)[-1][:ENTITY_ID_LEN]


def cohort_tags(gmgn_tags: Mapping[str, Any]) -> list[str]:
    """GMGN cohort labels in display order, app labels removed, unknown labels kept last."""
    known = [t for t in GMGN_COHORT_ORDER if t in gmgn_tags]
    extra = sorted(t for t in gmgn_tags if t not in GMGN_COHORT_ORDER and t not in GMGN_APP_TAGS)
    return known + extra


def app_tags(gmgn_tags: Mapping[str, Any]) -> list[str]:
    return sorted(t for t in gmgn_tags if t in GMGN_APP_TAGS)


def vendor_tag(label: str) -> str:
    """``smart_degen`` -> ``gmgn:smart_degen``: the only spelling a GMGN label gets in ``tags_json``."""
    return f"{GMGN_TAG_PREFIX}{label}"


def _sell_only_thresholds() -> tuple[int, Decimal]:
    # Lazy: grade imports this module for infer_archetype, and the thresholds must stay
    # the grader's own so the registry never calls sell-only what the rubric would score.
    from kaiba.intelligence.grade import SELL_ONLY_MAX_BUY_SHARE, SELL_ONLY_MIN_TRADES

    return SELL_ONLY_MIN_TRADES, SELL_ONLY_MAX_BUY_SHARE


def is_sell_only(buys: int | None, sells: int | None) -> bool | None:
    """``True`` on the grader's sell-only rule, ``False`` when it does not fire, ``None`` unmeasured.

    ``None`` covers both "no swaps observed" and "fewer than the minimum trades": a wallet
    with one sell and no buy is a thin sample, not a settlement address.
    """
    if buys is None or sells is None:
        return None
    min_trades, max_share = _sell_only_thresholds()
    total = int(buys) + int(sells)
    if total < min_trades:
        return None
    return (Decimal(int(buys)) / Decimal(total)) <= max_share


def infer_registry_archetype(facts: WalletFacts) -> tuple[str, str]:
    """``(archetype, basis)``. Our measurements claim a word before a provider tag does."""
    candidates: dict[str, str] = {}

    if is_sell_only(facts.observed_buys, facts.observed_sells):
        candidates.setdefault(REGISTRY_ARCHETYPE_SELL_ONLY, "swaps")
    for role in sorted(facts.bundle_roles):
        if facts.bundle_roles[role] >= BUNDLE_ROLE_MIN_TOKENS and role in BUNDLE_ROLE_ARCHETYPE:
            candidates.setdefault(BUNDLE_ROLE_ARCHETYPE[role], "bundles")
    if facts.created_tokens:
        candidates.setdefault("creator", "tokens")
    if facts.score_archetype:
        mapped = SCORE_ARCHETYPE.get(facts.score_archetype)
        # A quality word out of wallet_scores (smart_money, diamond, top_trader) is only
        # worth saying when the same row carries a grade; an UNSCORED row's archetype is
        # an inference over provider tags, and the bracket already attributes those.
        if mapped and (mapped not in QUALITY_WORDS or facts.grade in GRADED_LETTERS):
            candidates.setdefault(mapped, "wallet_scores")
    for tag in sorted(facts.gmgn_tags):
        word = GMGN_ARCHETYPE.get(tag)
        if word:
            candidates.setdefault(word, "gmgn")

    for word in ARCHETYPE_PRECEDENCE:
        if word in candidates:
            return word, candidates[word]
    return REGISTRY_ARCHETYPE_UNKNOWN, "none"


_PRIOR_WS_RE = re.compile(r"\s+")


def prior_name(facts: WalletFacts) -> str | None:
    """The operator's own label for this wallet, if the registry ever held one.

    A name this module generated is recognised by ``meta_json.naming.name`` matching the
    stored name, so a re-run never mistakes its own output for an operator label.
    """
    existing = facts.existing
    if not existing:
        return None
    meta = jload(existing.get("meta_json"), {})
    naming_meta = meta.get("naming") if isinstance(meta, dict) else None
    if isinstance(naming_meta, dict) and naming_meta.get("prior_name"):
        raw = str(naming_meta["prior_name"])
    else:
        stored = existing.get("name")
        generated = isinstance(naming_meta, dict) and naming_meta.get("name") == stored
        raw = "" if not stored or generated else str(stored)
    cleaned = _PRIOR_WS_RE.sub(" ", raw.replace('"', "'")).strip()
    if len(cleaned) > PRIOR_NAME_MAX:
        cut = cleaned[:PRIOR_NAME_MAX]
        cleaned = (cut[: cut.rfind(" ")] if " " in cut else cut).rstrip()
    return cleaned or None


def registry_name(facts: WalletFacts) -> str:
    """The handle. Pure function of the facts; see the section comment for the shape."""
    archetype, _basis = infer_registry_archetype(facts)
    parts = [f"{archetype}#{short_id(facts.chain, facts.address)}"]
    tags = cohort_tags(facts.gmgn_tags)
    if tags:
        shown = ",".join(tags[:MAX_COHORT_TAGS_IN_NAME])
        more = "+" if len(tags) > MAX_COHORT_TAGS_IN_NAME else ""
        parts.append(f"[gmgn:{shown}{more}]")
    if facts.entity_id:
        if facts.entity_size is not None:
            parts.append(f"(entity {entity_handle(facts.entity_id)}, {int(facts.entity_size)} members)")
        else:
            parts.append(f"(entity {entity_handle(facts.entity_id)})")
    if facts.grade in GRADED_LETTERS:
        parts.append(f"grade {facts.grade}")
    elif facts.grade == Grade.QUARANTINED.value:
        parts.append(Grade.QUARANTINED.value)
    prior = prior_name(facts)
    if prior:
        parts.append(f'"{prior}"')
    return " ".join(parts)


def registry_tags(facts: WalletFacts) -> list[str]:
    """``tags_json`` content: what the registry held, plus GMGN's labels and our roles.

    A GMGN cohort label is written as ``gmgn:<label>`` (:func:`vendor_tag`) and nothing
    else: not the bare label, and never a :class:`WalletTag` it might "mean". Our own
    measurements — a launch-window role out of ``token_bundle_members``, the sell-only
    rule over ``swaps`` — are written bare, because they are ours. Union with the existing
    tags, so an operator's own labels survive and a re-run is a no-op.
    """
    out: set[str] = set()
    existing = facts.existing or {}
    prior = jload(existing.get("tags_json"), []) if existing else []
    if isinstance(prior, list):
        out.update(str(t) for t in prior if t)
    for tag in facts.gmgn_tags:
        if tag in GMGN_APP_TAGS:
            continue
        out.add(vendor_tag(tag))
    for role, count in facts.bundle_roles.items():
        if count >= BUNDLE_ROLE_MIN_TOKENS and role in BUNDLE_ROLE_TAG:
            out.add(BUNDLE_ROLE_TAG[role].value)
    if is_sell_only(facts.observed_buys, facts.observed_sells):
        out.add(SELL_ONLY_TAG)
    return sorted(out)


def registry_meta(facts: WalletFacts, name: str, tags: Sequence[str]) -> dict[str, Any]:
    """The existing ``meta_json`` with a ``naming`` block; nothing else is touched.

    Everything this module records lives under ``meta["naming"]`` because the grader reads
    top-level keys (``buy_count``, ``created_token_count``, ``kol`` ...) as provider claims,
    and an observed buy count is not a provider claim.
    """
    existing = facts.existing or {}
    meta = jload(existing.get("meta_json"), {}) if existing else {}
    if not isinstance(meta, dict):
        meta = {}
    archetype, basis = infer_registry_archetype(facts)
    observed_basis = "derived" if facts.observed_buys is not None else "unavailable"
    meta["naming"] = {
        "version": NAMING_VERSION,
        "name": name,
        "short_id": short_id(facts.chain, facts.address),
        "archetype": archetype,
        "archetype_basis": basis,
        "gmgn_tags": {t: int(facts.gmgn_tags[t]) for t in cohort_tags(facts.gmgn_tags)},
        "gmgn_apps": {t: int(facts.gmgn_tags[t]) for t in app_tags(facts.gmgn_tags)},
        "gmgn_feeds": dict(sorted(facts.gmgn_feeds.items())),
        "bundle_roles": dict(sorted(facts.bundle_roles.items())),
        "entity_id": facts.entity_id,
        "entity_size": facts.entity_size,
        "grade": facts.grade,
        "score_archetype": facts.score_archetype,
        "observed": {
            "buys": facts.observed_buys,
            "sells": facts.observed_sells,
            "tokens": facts.observed_tokens,
            "sources": dict(sorted(facts.sources.items())),
            "basis": observed_basis,
        },
        "sell_only": is_sell_only(facts.observed_buys, facts.observed_sells),
        # Transaction failure rate is a real filter (two leaderboard wallets ran 36% and
        # 49%) and `swaps` holds only the successes, so it is UNAVAILABLE here, not zero.
        "failure_rate": None,
        "failure_rate_basis": "unavailable",
        "created_tokens": facts.created_tokens,
        "prior_name": prior_name(facts),
        "tag_count": len(tags),
    }
    return meta


def registry_source(facts: WalletFacts) -> str:
    """Where the registry first learned of the wallet, for a row that has no source yet."""
    if facts.gmgn_feeds:
        feed = sorted(facts.gmgn_feeds.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return f"gmgn:{feed}"
    if facts.sources:
        return sorted(facts.sources.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    if facts.bundle_roles:
        return "bundles"
    if facts.entity_id:
        return "entities"
    if facts.grade:
        return "wallet_scores"
    return "unknown"


def name_violates_quality_rule(name: str) -> str | None:
    """Why a name asserts ungraded quality, or ``None`` when it does not.

    The rule: a word from :data:`QUALITY_WORDS` may appear outside the quoted operator
    label only when the ``[gmgn:...]`` bracket attributes it or the name carries a grade.
    A bracketed label attributes both itself and the archetype word it maps to in
    :data:`GMGN_ARCHETYPE`: ``smart_degen#.. [gmgn:app_smart_money]`` is GMGN's claim
    spelled through the one word the precedence table uses for that cohort.
    """
    unquoted = name.split('"', 1)[0]
    graded = bool(re.search(r"\bgrade [A-D]\b", unquoted)) or Grade.QUARANTINED.value in unquoted
    bracket = re.search(r"\[gmgn:([^\]]*)\]", unquoted)
    labels = {t for t in (bracket.group(1) if bracket else "").rstrip("+").split(",") if t}
    attributed = labels | {GMGN_ARCHETYPE[t] for t in labels if t in GMGN_ARCHETYPE}
    for word in sorted(set(re.findall(r"[a-z_]+", unquoted)) & QUALITY_WORDS):
        if word not in attributed and not graded:
            return f"{word!r} appears without gmgn attribution or a grade"
    return None


# ------------------------------------------------------------------ gathering


def _chain_or_none(raw: Any) -> Chain | None:
    try:
        return Chain(str(raw))
    except ValueError:
        return None


def _key(chain: Chain, address: Any) -> tuple[str, str]:
    return chain.value, safe_normalize(str(address), chain)


def gather_facts(
    conn: sqlite3.Connection, chain: Chain | None = None
) -> tuple[dict[tuple[str, str], WalletFacts], int]:
    """Bulk-read every evidence source into one :class:`WalletFacts` per (chain, address).

    A handful of GROUP BY queries rather than a per-wallet loop: the live box holds ~70k
    distinct wallets and the local history ~160k, and a query per wallet would be a job
    that never finishes. Returns ``(facts, rows_skipped_for_an_unknown_chain)``.
    """
    facts: dict[tuple[str, str], WalletFacts] = {}
    skipped = 0
    where = " WHERE chain = ?" if chain is not None else ""
    params: tuple[Any, ...] = (chain.value,) if chain is not None else ()

    def get(raw_chain: Any, address: Any) -> WalletFacts | None:
        nonlocal skipped
        ch = _chain_or_none(raw_chain)
        if ch is None or not address:
            skipped += 1
            return None
        key = _key(ch, address)
        found = facts.get(key)
        if found is None:
            found = WalletFacts(chain=ch, address=key[1])
            facts[key] = found
        return found

    # 0. what the registry already holds — every row stays a row.
    for row in fetch_all(conn, f"SELECT * FROM wallets{where}", params):
        f = get(row["chain"], row["address"])
        if f is not None:
            f.existing = dict(row)

    # 1. swaps: asymmetry, breadth, first/last seen, per-source counts.
    for row in fetch_all(
        conn,
        "SELECT chain, wallet, COUNT(*) AS n, SUM(side = 'buy') AS buys, SUM(side = 'sell') AS sells, "
        "COUNT(DISTINCT token) AS tokens, MIN(ts_ms) AS first_ms, MAX(ts_ms) AS last_ms "
        f"FROM swaps{where} GROUP BY chain, wallet",
        params,
    ):
        f = get(row["chain"], row["wallet"])
        if f is None:
            continue
        # Accumulate, never assign: two spellings of one EVM address are two GROUP BY
        # rows that normalise to one wallet. Distinct tokens cannot be summed across
        # them without double counting, so the larger row's count stands as a floor.
        f.observed_buys = (f.observed_buys or 0) + int(row["buys"] or 0)
        f.observed_sells = (f.observed_sells or 0) + int(row["sells"] or 0)
        f.observed_tokens = max(f.observed_tokens or 0, int(row["tokens"] or 0))
        first = int(row["first_ms"]) if row["first_ms"] is not None else None
        last = int(row["last_ms"]) if row["last_ms"] is not None else None
        f.first_seen_ms = _min_opt(f.first_seen_ms, first)
        f.last_seen_ms = _max_opt(f.last_seen_ms, last)
    for row in fetch_all(
        conn,
        f"SELECT chain, wallet, source, COUNT(*) AS n FROM swaps{where} GROUP BY chain, wallet, source",
        params,
    ):
        f = get(row["chain"], row["wallet"])
        if f is not None:
            source = str(row["source"])
            f.sources[source] = f.sources.get(source, 0) + int(row["n"])

    # 2. GMGN cohort labels. They ride on the wallet.trade events (``feed`` marks a GMGN
    #    row); the swaps table never stored them.
    event_where = " AND chain = ?" if chain is not None else ""
    for row in fetch_all(
        conn,
        "SELECT chain, subject, payload FROM events WHERE kind = 'wallet.trade' "
        f"AND payload LIKE '%\"feed\":\"%'{event_where}",
        params,
    ):
        payload = jload(row["payload"], {})
        if not isinstance(payload, dict):
            continue
        f = get(row["chain"] or payload.get("chain"), row["subject"] or payload.get("wallet"))
        if f is None:
            continue
        feed = payload.get("feed")
        if feed:
            f.gmgn_feeds[str(feed)] = f.gmgn_feeds.get(str(feed), 0) + 1
        tags = payload.get("tags") or []
        if isinstance(tags, list):
            for tag in tags:
                label = str(tag).strip().lower()
                if label:
                    f.gmgn_tags[label] = f.gmgn_tags.get(label, 0) + 1

    # 3. entity membership.
    member_where = " WHERE m.chain = ?" if chain is not None else ""
    for row in fetch_all(
        conn,
        "SELECT m.chain, m.address, m.entity_id, e.size FROM entity_members m "
        f"JOIN entities e ON e.entity_id = m.entity_id{member_where}",
        params,
    ):
        f = get(row["chain"], row["address"])
        if f is not None:
            f.entity_id = str(row["entity_id"])
            f.entity_size = int(row["size"]) if row["size"] is not None else None

    # 4. launch-window roles.
    for row in fetch_all(
        conn,
        "SELECT chain, address, role, COUNT(DISTINCT token) AS n FROM token_bundle_members"
        f"{where} GROUP BY chain, address, role",
        params,
    ):
        f = get(row["chain"], row["address"])
        if f is not None:
            f.bundle_roles[str(row["role"])] = int(row["n"])

    # 5. grades, where they exist.
    for row in fetch_all(conn, f"SELECT chain, address, archetype, grade FROM wallet_scores{where}", params):
        f = get(row["chain"], row["address"])
        if f is not None:
            f.score_archetype = str(row["archetype"]) if row["archetype"] else None
            f.grade = str(row["grade"]) if row["grade"] else None

    # 6. token creators.
    creator_where = " AND chain = ?" if chain is not None else ""
    for row in fetch_all(
        conn,
        "SELECT chain, creator, COUNT(*) AS n FROM tokens WHERE creator IS NOT NULL AND creator != ''"
        f"{creator_where} GROUP BY chain, creator",
        params,
    ):
        f = get(row["chain"], row["creator"])
        if f is not None:
            f.created_tokens = int(row["n"])

    return facts, skipped


# ------------------------------------------------------------------ writing


def _min_opt(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None


def _max_opt(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def name_wallets(
    conn: sqlite3.Connection,
    chain: Chain | None = None,
    *,
    dry_run: bool = False,
    now: int | None = None,
    samples_per_archetype: int = 3,
) -> NamingReport:
    """Name every wallet we hold evidence on and write the result into ``wallets``.

    Idempotent: a second run over unchanged evidence writes nothing. Existing rows keep
    their ``source``, ``cohort``, ``twitter`` and funder columns untouched; only ``name``,
    ``tags_json``, ``meta_json`` and the seen timestamps move, and tags only ever grow.
    ``dry_run`` computes everything and writes nothing, which is how it runs against the
    live database.
    """
    stamp = now if now is not None else now_ms()
    facts, skipped = gather_facts(conn, chain)
    where = " WHERE chain = ?" if chain is not None else ""
    params: tuple[Any, ...] = (chain.value,) if chain is not None else ()
    before_row = fetch_one(conn, f"SELECT COUNT(*) AS n FROM wallets{where}", params)
    before = int(before_row["n"]) if before_row else 0

    report = NamingReport(dry_run=dry_run, wallets_before=before, wallets_after=before)
    report.skipped_unknown_chain = skipped
    inserts: list[tuple[Any, ...]] = []
    updates: list[tuple[Any, ...]] = []
    sample_buckets: dict[str, list[str]] = {}

    for key in sorted(facts):
        f = facts[key]
        name = registry_name(f)
        tags = registry_tags(f)
        meta = registry_meta(f, name, tags)
        archetype, basis = infer_registry_archetype(f)
        tags_json = jdump(tags)
        meta_json = jdump(meta)

        report.considered += 1
        report.by_archetype[archetype] = report.by_archetype.get(archetype, 0) + 1
        report.by_chain[f.chain.value] = report.by_chain.get(f.chain.value, 0) + 1
        report.by_basis[basis] = report.by_basis.get(basis, 0) + 1
        if cohort_tags(f.gmgn_tags):
            report.with_gmgn_cohort += 1
        if f.entity_id:
            report.in_entity += 1
        if f.grade in GRADED_LETTERS:
            report.graded += 1
        if is_sell_only(f.observed_buys, f.observed_sells):
            report.sell_only += 1
        bucket = sample_buckets.setdefault(archetype, [])
        if len(bucket) < samples_per_archetype:
            bucket.append(name)

        if f.existing is None:
            first = f.first_seen_ms if f.first_seen_ms is not None else stamp
            last = f.last_seen_ms if f.last_seen_ms is not None else first
            inserts.append(
                (f.chain.value, f.address, name, registry_source(f), tags_json, first, last, meta_json)
            )
            report.inserted += 1
            continue

        ex = f.existing
        first = _min_opt(ex.get("first_seen_ms"), f.first_seen_ms)
        last = _max_opt(ex.get("last_seen_ms"), f.last_seen_ms)
        first = first if first is not None else stamp
        last = last if last is not None else first
        same = (
            ex.get("name") == name
            and (ex.get("tags_json") or "[]") == tags_json
            and (ex.get("meta_json") or "{}") == meta_json
            and int(ex.get("first_seen_ms") or 0) == first
            and int(ex.get("last_seen_ms") or 0) == last
        )
        if same:
            report.unchanged += 1
            continue
        updates.append((name, tags_json, meta_json, first, last, f.chain.value, f.address))
        report.updated += 1

    report.samples = [n for word in ARCHETYPE_PRECEDENCE for n in sample_buckets.get(word, [])]

    if dry_run:
        report.wallets_after = before + report.inserted
        return report

    with tx(conn):
        if inserts:
            conn.executemany(
                "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, "
                "last_seen_ms, meta_json) VALUES (?,?,?,?,?,?,?,?)",
                inserts,
            )
        if updates:
            conn.executemany(
                "UPDATE wallets SET name = ?, tags_json = ?, meta_json = ?, first_seen_ms = ?, "
                "last_seen_ms = ? WHERE chain = ? AND address = ?",
                updates,
            )
    after_row = fetch_one(conn, f"SELECT COUNT(*) AS n FROM wallets{where}", params)
    report.wallets_after = int(after_row["n"]) if after_row else before + report.inserted
    log.info(
        "named %d wallets (%d new, %d updated, %d unchanged) chain=%s",
        report.considered, report.inserted, report.updated, report.unchanged,
        chain.value if chain else "all",
    )
    return report


# ------------------------------------------------------------------ reading


def wallet_name(conn: sqlite3.Connection, chain: Chain, address: str) -> str | None:
    """The registry name, or ``None`` when the wallet was never named."""
    row = fetch_one(
        conn,
        "SELECT name FROM wallets WHERE chain = ? AND address = ?",
        (chain.value, safe_normalize(address, chain)),
    )
    return str(row["name"]) if row and row["name"] else None


_HANDLE_ID_RE = re.compile(r"^[0-9a-f]{4,64}$")


def resolve_handle(conn: sqlite3.Connection, handle: str) -> list[dict[str, Any]]:
    """``sniper#a3f2c1`` or ``a3f2c1`` -> every registry row whose short id matches.

    A list, because the id is a label rather than a key: two wallets can share one.
    """
    raw = handle.strip().rsplit("#", 1)[-1].strip().lower()
    if not _HANDLE_ID_RE.match(raw):
        return []
    rows = fetch_all(
        conn,
        "SELECT chain, address, name, cohort, tags_json FROM wallets WHERE meta_json LIKE ? "
        "ORDER BY chain, address",
        (f'%"short_id":"{raw}"%',),
    )
    return [
        {
            "chain": r["chain"],
            "address": r["address"],
            "name": r["name"],
            "cohort": r["cohort"],
            "tags": jload(r["tags_json"], []),
        }
        for r in rows
    ]


def archetype_counts(conn: sqlite3.Connection, chain: Chain | None = None) -> dict[str, int]:
    """Registry archetype -> wallets, read back from ``meta_json``. Cheap enough for a status line."""
    where = " WHERE chain = ?" if chain is not None else ""
    params: tuple[Any, ...] = (chain.value,) if chain is not None else ()
    counts: Counter[str] = Counter()
    for row in fetch_all(conn, f"SELECT meta_json FROM wallets{where}", params):
        meta = jload(row["meta_json"], {})
        naming_meta = meta.get("naming") if isinstance(meta, dict) else None
        if isinstance(naming_meta, dict) and naming_meta.get("archetype"):
            counts[str(naming_meta["archetype"])] += 1
    return dict(sorted(counts.items()))


# ------------------------------------------------------------------ command line


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m kaiba.intelligence.naming --db data/kaiba.db [--chain sol] [--dry-run]``.

    ``--dry-run`` opens the file read-only at the SQLite level, so it cannot write even by
    accident; that is the mode for the live box.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Name every wallet the database holds evidence on.")
    parser.add_argument("--db", required=True, help="path to kaiba.db")
    parser.add_argument("--chain", default=None, help="restrict to one chain (sol, bsc, robinhood, ...)")
    parser.add_argument("--dry-run", action="store_true", help="compute and report; write nothing")
    parser.add_argument("--samples", type=int, default=3, help="sample names per archetype in the report")
    args = parser.parse_args(list(argv) if argv is not None else None)

    chain = Chain(args.chain) if args.chain else None
    if args.dry_run:
        conn = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        report = name_wallets(conn, chain, dry_run=args.dry_run, samples_per_archetype=args.samples)
    finally:
        conn.close()
    print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised on the live box by hand
    raise SystemExit(main())


__all__ = [
    "ARCHETYPE_PRECEDENCE",
    "BUNDLE_ROLE_MIN_TOKENS",
    "GMGN_APP_TAGS",
    "GMGN_ARCHETYPE",
    "GMGN_CHUNK_SIZE",
    "GMGN_COHORT_ORDER",
    "GMGN_ROW_KEYS",
    "GMGN_TAG_PREFIX",
    "NAMING_VERSION",
    "QUALITY_WORDS",
    "ROLE_LABELS",
    "THRESHOLD_PROVENANCE",
    "NamingReport",
    "WalletFacts",
    "app_tags",
    "archetype_counts",
    "build_name",
    "cohort_tags",
    "entity_handle",
    "gather_facts",
    "gmgn_import_rows",
    "infer_archetype",
    "infer_registry_archetype",
    "is_sell_only",
    "main",
    "name_violates_quality_rule",
    "name_wallets",
    "prior_name",
    "registry_meta",
    "registry_name",
    "registry_source",
    "registry_tags",
    "resolve_handle",
    "role_label",
    "short_id",
    "vendor_tag",
    "wallet_name",
    "write_gmgn_import",
]
