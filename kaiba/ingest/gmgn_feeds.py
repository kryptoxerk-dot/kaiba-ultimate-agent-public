"""GMGN feed poller: smart-money and KOL trades, signals, trenches and trending.

GMGN has no push surface on our plan, so this is a scheduler over the CLI wrapper. Three
things make it more than a ``while True: fetch``:

* **Ban discipline.** GMGN locks the whole IP when an account exceeds its allowance and
  support confirmed an upgrade does not lift an active lock, so every call goes through
  ``limiter.guarded("gmgn", ...)`` at :attr:`Priority.DISCOVERY` — behind exits, positions
  and entries. A refusal is a skipped poll, never a retry.
* **Cursors, not diffs.** Each (chain, feed) keeps a watermark in ``kv``: the newest
  timestamp we have accepted plus a bounded ring of recently emitted dedupe keys. A poll
  that returns the same page twice emits nothing, which matters because trending and
  trenches are *state* endpoints that return the same rows for minutes at a time.
* **Error envelopes are data.** ``--raw`` returns the provider envelope, so ``code != 0``
  is a normal response carrying bad news: zero rows and a ``PROVIDER_ERROR`` event, never
  an exception into the scheduler.

The wrapper itself is ``kaiba/providers/gmgn_cli.py`` (owned by Codex, task P0-2). The
import is guarded so this module and its tests work before that lands; the contract we
need is exactly one function::

    run(group: str, command: str, **flags) -> dict     # always --raw

with flag names in snake_case mapped to ``--kebab-case`` on the command line. See
:data:`FEEDS` for the five invocations we expect.

**Money honesty (2026-09-21, amended 2026-09-22).** Three things this module did with
money were dishonest and are now refused -- the third was introduced by the first round
of repairs:

* ``track smartmoney`` / ``track kol`` rows carry ``quote_amount`` as a UI float
  (``6.282213760836256``) and name no quote asset. MEASURED: 0 of 120 cached and 0 of
  50,805 live rows carry ``quote_address``; on sol the quote is often a USD stable (live
  row id 474808: token = SOL at $118, ``quote_amount`` 212.02 == ``usd_value``). Writing
  that number into ``swaps.amount_native`` (lamports / wei) was wrong by up to 1e18 and
  made ``lanes._net_buyers`` raise on ``int()``. The UI value now goes to
  ``swaps.amount_quote`` with ``quote_mint`` when the payload names it, and
  ``amount_native`` is integer base units ONLY when the payload proves the quote is the
  chain native (:func:`native_atoms`), else ``None``. :func:`backfill_amount_native`
  repairs the rows written before this.
* ``swaps.amount_token`` on a ``gmgn:*`` row is the token quantity as the provider's UI
  text (``20530293.283241913``). That is a documented contract, not an oversight:
  ``grade.HUMAN_UNIT_SOURCE_PREFIXES = ("gmgn:",)`` tells the tape grader that gmgn rows
  hold human units and ``grade.normalise_tape_rows`` scales them itself
  (``tests/test_tape_grading.py`` pins it). Round 1 of this work broke it by storing
  ``None``, or base units when the registry knew the decimals: ``pnl`` then flags every
  episode ``unknown_qty`` (or ``oversold`` once units are mixed), no round trip ever
  closes, and no gmgn-only wallet can earn a grade. On bsc the gmgn feed is the ONLY
  swaps source (MEASURED 2026-09-22 on the live box: 13,753 gmgn rows, 436 wallets,
  2,897 buy+sell pairs, zero rows from any other source), so that removed one of
  ``sm_trenches``'s two smart-cohort routes. Restored: the column keeps the UI text
  (13,721 of the 13,753 bsc rows carry a dot; the rest are whole UI numbers such as
  ``50000``, which is why the contract is keyed on ``source``, never on shape). Base
  units, when the registry knows the decimals, ride on the ``wallet.trade`` event as
  ``amount_token_atoms`` / ``token_decimals`` and never enter the column.
* ``market trenches`` returns ``{"new_creation": [...], "near_completion": [...],
  "completed": [...]}`` (the 180 objects the lead measured). :func:`_rows` only knew
  ``list``/``rank``/... so :func:`parse_trenches` returned ZERO rows on every real
  payload: MEASURED on the live box, every ``ingest:gmgn:*:trenches`` cursor is
  ``{"ts_ms": 0, "seen": []}`` and there is no ``feed=trenches`` alpha event on any
  chain. Now flattened, and on chains with no listener each object also becomes a
  ``tokens`` row and is handed to tier-0 triage, which is what lets the scanner see bsc.

**rug_ratio on every alpha feed (2026-09-22).** ``dyor._stored_feed_rug_ratio`` reads
``payload.rug_ratio`` off any ``alpha.signal`` event this module wrote in the last 900 s,
and the ``sm-trenches`` lane gates on it. Only :func:`parse_trenches` stored it; trending
and signal rows carry it too and dropped it. All three now store it through
:func:`_rug_ratio`: the vendor's JSON number exactly as sent, never a string, and absent
(not ``None``) when the row has none. MEASURED on the live box from the running feed's
own cache plus metered calls, and the COVERAGE VARIES BY READ: how many rows of a feed
carry the field is not a stable per-chain number. Two trenches polls a minute apart on
bsc returned 0 of 180 and then 34 of 180 rows with it; a verifier's two reads of the
stored rows counted 8/8/54 on one read and 12/36/0 on the next across the three chains.
No count in this file is therefore a coverage claim. What did NOT vary:

* sol rows carry a real spread in 0..1 (``0.186``, ``0.3``, ``0.5`` ...) on every feed;
* every EVM value ever seen -- bsc and robinhood; trending, trenches and signal alike --
  is the integer ``0``. Not one non-zero EVM score has been observed on any read;
* ``market signal`` (bsc, the first rows this feed ever returned -- see :data:`FEEDS`)
  nests the score at ``data.rug_ratio`` and has no top-level ``chain``, ``symbol`` or
  ``timestamp``: the chain and symbol sit in ``data`` and the time is ``trigger_at``.

What a ``0`` means on an EVM chain is NOT measured: 100 % zeros on bsc and robinhood
across three feeds against a real spread on sol is consistent with "unscored", but the
vendor does not say, so the number is stored as sent and the reader decides. Nothing
here turns an absent score into ``0``.

One more bias the reader must know about. The trenches poller asks for
``--filter-preset smart-money``, and gmgn-cli applies ``max_rug_ratio 0.3`` SERVER-SIDE
under that preset (``dist/commands/market.js``; the same number ``lanes.SM_TRENCHES``
copies). Every stored trenches row therefore has ``rug_ratio < 0.3`` by construction,
and ``dyor``'s stored fallback can only ever say "pass". So every trenches alpha payload
records the preset it came through as ``filter_preset`` (:data:`TRENCHES_FILTER_PRESET`);
a payload without the key came from an unfiltered feed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kaiba.core.config import get_risk
from kaiba.core.db import connect, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit, emit_once
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import (
    EVM_ZERO,
    NATIVE_DECIMALS,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    digest,
    now_ms,
)
from kaiba.providers.native_price import WRAPPED_NATIVE

# The wrapper landed as ``run_read(endpoint, args) -> (data, receipt)``, not the
# ``run(group, command, **flags)`` this module was written against while the two were
# being built in parallel. The names never met, so the import silently failed and **every
# GMGN feed was permanently idle** while logging one line about it at startup. Adapt here
# rather than changing either module's public shape.
try:
    from kaiba.providers.gmgn_cli import run_read as _gmgn_run_read
except ImportError:  # pragma: no cover - exercised by the "wrapper missing" test
    _gmgn_run_read = None  # type: ignore[assignment]


def _flags_to_argv(flags: dict[str, Any]) -> list[str]:
    """``{"chain": "sol", "limit": 100}`` -> ``["--chain", "sol", "--limit", "100"]``."""
    argv: list[str] = []
    for key, value in flags.items():
        if value is None or value is False:
            continue
        opt = "--" + str(key).replace("_", "-")
        if value is True:
            argv.append(opt)
        else:
            argv += [opt, str(value)]
    return argv


def gmgn_run(
    group: str,
    command: str,
    *,
    _priority: Priority = Priority.DISCOVERY,
    _wait_for_slot_s: float = 0.0,
    _conn: Any = None,
    **flags: Any,
) -> Any:
    """Adapter onto the wrapper's real signature. Returns the payload, or None.

    ``run_read`` reserves through the limiter ITSELF. The underscore-prefixed parameters
    are keyword-only and underscored so they can never collide with a GMGN flag, and they
    exist so ``poll_once`` can hand its priority and its wait budget to the reservation
    that actually happens rather than making a second one of its own.
    """
    if _gmgn_run_read is None:
        return None
    data, receipt = _gmgn_run_read(
        f"{group}.{command}",
        [group, command, *_flags_to_argv(flags)],
        priority=_priority,
        wait_for_slot_s=_wait_for_slot_s,
        conn=_conn,
    )
    if data is None:
        log.info("gmgn %s/%s unavailable: %s", group, command, (receipt.note or "")[:160])
    return data


if _gmgn_run_read is None:  # pragma: no cover - the wrapper is present in this tree
    gmgn_run = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PROVIDER = "gmgn"
CURSOR_PREFIX = "ingest:gmgn"
#: How many dedupe keys a cursor remembers. 2,000 covers ~20 polls of a 100-row feed.
SEEN_LIMIT = 2000

# --------------------------------------------------------------------------------------
# money provenance
# --------------------------------------------------------------------------------------

#: ``swaps.amount_native`` basis: the payload named its quote asset and it is the chain
#: native, so UI x 10^NATIVE_DECIMALS is an exact integer. The only basis that writes.
NATIVE_BASIS_PAYLOAD = "payload_quote_address"
#: The payload did not name a quote asset, or named one that is not the native. The UI
#: value is kept in ``amount_quote``; ``amount_native`` is ``None``. Never 0.
NATIVE_BASIS_UNAVAILABLE = "unavailable"

#: Addresses that prove a quote leg is the chain native. Every entry is a repo constant
#: (``schemas.SOL_NATIVE_MINT``, ``schemas.EVM_ZERO``, ``native_price.WRAPPED_NATIVE``);
#: nothing here was typed from memory. MEASURED 2026-09-21 in 1,080 live ``market
#: trenches`` rows: bsc quotes were ``0x000..0`` x342, WBNB x194, USDT x31 and four other
#: assets; sol quotes were wrapped SOL x330. A quote of USDT is why "multiply by 1e18"
#: cannot be the rule.
NATIVE_QUOTE_ADDRESSES: dict[Chain, frozenset[str]] = {
    chain: frozenset(
        a.lower()
        for a in (
            [SOL_NATIVE_MINT] if chain is Chain.SOL
            else [EVM_ZERO, *([WRAPPED_NATIVE[chain]] if chain in WRAPPED_NATIVE else [])]
        )
    )
    for chain in Chain
}

#: Chains whose ``tokens`` rows are written by a live listener: ``pumpportal.py`` (sol,
#: also ``stonkfun.py``) and ``robinhood.py`` (robinhood). DEFINITIONAL: read off the
#: ``record_new_token`` writers in the tree, not measured. Trenches objects on these
#: chains stay alpha events only; the listener saw the launch first and owns the row.
LISTENER_CHAINS: frozenset[Chain] = frozenset({Chain.SOL, Chain.ROBINHOOD})

#: The feeds whose objects this module hands to a listener on :data:`LISTENER_CHAINS`
#: instead of registering itself. DEFINITIONAL, read off the feed registry: ``trenches``
#: is a bonding-curve view of exactly the launchpads those listeners subscribe to, so on
#: their chains every object it returns is theirs to write.
#:
#: ``trending`` is deliberately NOT one of them. It is a market-wide list, so it is the
#: only feed here that can surface a token no launch listener could ever have seen -- a
#: manually deployed contract subscribes to no launchpad socket. Deferring trending to a
#: listener on sol and robinhood is what made a manual deploy invisible to the agent: no
#: ``tokens`` row, no triage decision, and therefore no DYOR.
LISTENER_OWNED_FEEDS: frozenset[str] = frozenset({"trenches"})

#: Prefix shared by every ``tokens.meta_json["source"]`` this module writes. The upsert
#: refreshes only rows carrying it, so a listener-written row is never overwritten.
#: Keyed on the source string rather than on the row's shape, the same way
#: ``grade.HUMAN_UNIT_SOURCE_PREFIXES`` keys the units contract.
TOKEN_SOURCE_PREFIX = f"{PROVIDER}:"

#: ``tokens.meta_json["source"]`` for a trenches row. Kept as a module constant because
#: the rest of the tree cites it by name; every other feed gets :func:`token_source`.
TOKEN_SOURCE = f"{TOKEN_SOURCE_PREFIX}trenches"


def token_source(feed: str) -> str:
    """``tokens.meta_json["source"]`` for a row this module registers from ``feed``.

    One value per feed rather than one per module: the registry then says which feed
    found the token, and ``triage_decisions.source`` says it too, which is the only way
    to tell a launchpad sighting from a market-wide one after the fact.
    """
    return f"{TOKEN_SOURCE_PREFIX}{feed}"


#: Hand every token this module registers to tier-0 triage, exactly as both listeners
#: do after their own ``tokens`` upsert. MEASURED 2026-09-21: the scanner takes its
#: work from ``triage_decisions`` (``scanner._db_queue_work``) and from
#: ``token.migrated`` events, never from the ``tokens`` table; triage in turn is only
#: ever invoked by a listener calling ``screen_launch``. A ``tokens`` row alone would
#: therefore never be scanned. Flip this off to register rows without screening.
SCREEN_NEW_TOKENS = True

#: The category keys ``market trenches`` returns (``near_completion`` is documented to
#: come back as ``pump`` on some plans). MEASURED 2026-09-21: 6 cached and 3 live
#: payloads all carried exactly ``completed`` / ``near_completion`` / ``new_creation``.
TRENCHES_CATEGORIES: tuple[str, ...] = ("new_creation", "near_completion", "pump", "completed")

#: The gmgn-cli preset the trenches poller asks for, stored on every trenches alpha row
#: as ``payload.filter_preset`` so a reader can see the bias. DEFINITIONAL: it is the
#: ``filter_preset`` flag in :data:`FEEDS`, held here so the flag and the payload cannot
#: drift apart. CITED, not measured: gmgn-cli applies ``max_rug_ratio 0.3`` SERVER-SIDE
#: under this preset (``dist/commands/market.js``; ``dyor``'s provenance entry
#: ``lane_threshold_0.3`` and ``lanes.SM_TRENCHES`` copy the number), so every stored
#: trenches row has ``rug_ratio < 0.3`` by construction and the stored fallback in
#: ``dyor._stored_feed_rug_ratio`` can only ever say pass.
TRENCHES_FILTER_PRESET = "smart-money"


def is_native_quote(chain: Chain, quote: str | None) -> bool:
    """True only when ``quote`` is one of :data:`NATIVE_QUOTE_ADDRESSES` for ``chain``."""
    if not quote:
        return False
    return str(quote).strip().lower() in NATIVE_QUOTE_ADDRESSES.get(chain, frozenset())


def ui_to_atoms(ui: str | None, decimals: int | None) -> str | None:
    """UI-unit Decimal text x 10^decimals -> integer text, or ``None``. Never rounds up."""
    if ui is None or decimals is None or decimals < 0:
        return None
    try:
        atoms = (Decimal(str(ui)) * (Decimal(10) ** int(decimals))).to_integral_value(rounding=ROUND_DOWN)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return str(int(atoms))


def native_atoms(chain: Chain, quote_ui: str | None, quote_mint: str | None) -> tuple[str | None, str]:
    """``(amount_native, basis)``: base units only when the payload proves a native quote.

    The proof is the row's own ``quote_address`` matching :data:`NATIVE_QUOTE_ADDRESSES`;
    the chain of the feed is not a proof (see the module docstring for the sol counter
    example). Anything short of that is ``(None, "unavailable")``.
    """
    if quote_ui is None or not is_native_quote(chain, quote_mint):
        return None, NATIVE_BASIS_UNAVAILABLE
    atoms = ui_to_atoms(quote_ui, NATIVE_DECIMALS.get(chain))
    if atoms is None:
        return None, NATIVE_BASIS_UNAVAILABLE
    return atoms, NATIVE_BASIS_PAYLOAD


def _dec_text(value: Any) -> str | None:
    """A provider number as exact Decimal text (``6.282213760836256``, never ``1e-05``)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------------------
# typed rows
# --------------------------------------------------------------------------------------


class SwapRow(BaseModel):
    """A trade seen on a tracked-wallet feed, shaped for the ``swaps`` table.

    Every money field says what it is. ``amount_native`` is integer base units or
    ``None``. ``amount_token`` is the token quantity as the provider's UI text, because
    that is the documented shape of every ``gmgn:*`` row: ``grade.HUMAN_UNIT_SOURCE_PREFIXES``
    names this source and ``grade.normalise_tape_rows`` scales it (module docstring).
    Base units for the token, when the ``tokens`` registry knows its decimals, live in
    ``amount_token_atoms`` and reach the ``wallet.trade`` event only, never the column.
    """

    chain: Chain
    tx: str
    ts_ms: int
    wallet: str
    token: str
    side: str  # buy | sell
    #: The token quantity exactly as the provider sent it, UI units, Decimal text. This
    #: IS the ``swaps.amount_token`` column for a gmgn row (contract in the class doc).
    amount_token: str | None = None
    #: The same UI text, under the name the ``wallet.trade`` event carries it as.
    amount_token_ui: str | None = None
    #: Token atoms as integer text, ONLY once :func:`resolve_token_atoms` found the
    #: decimals in the ``tokens`` registry (no network on the ingest path). Event payload
    #: only: writing this into ``amount_token`` was the round-1 bug.
    amount_token_atoms: str | None = None
    token_decimals: int | None = None
    token_decimals_basis: str | None = None
    #: Lamports / wei as integer text, ONLY when :func:`native_atoms` could prove it.
    amount_native: str | None = None
    amount_native_basis: str = NATIVE_BASIS_UNAVAILABLE
    #: The quote leg in the provider's UI units, Decimal text, whatever the quote asset.
    amount_quote: str | None = None
    #: The quote asset when the payload names it (``quote_address``); else ``None``.
    quote_mint: str | None = None
    price_usd: str | None = None
    usd_value: str | None = None
    slot: int | None = None
    program: str | None = None
    feed: str = "smartmoney"
    wallet_name: str | None = None
    token_symbol: str | None = None
    tags: list[str] = Field(default_factory=list)
    #: The ``tokens``-row-shaped mapping for the token that was traded, when the object
    #: carried enough to build one. Same shape and same builder the alpha feeds use, so a
    #: token's provenance does not depend on which feed happened to see it first.
    token_facts: dict[str, Any] | None = None

    @property
    def dedupe_key(self) -> str:
        return f"{PROVIDER}:{self.feed}:{self.chain.value}:{self.tx}:{self.wallet}:{self.side}"

    def to_swap(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "tx": self.tx,
            "slot": self.slot,
            "ts_ms": self.ts_ms,
            "wallet": self.wallet,
            "token": self.token,
            "side": self.side,
            "amount_token": self.amount_token,
            "amount_native": self.amount_native,
            "amount_quote": self.amount_quote,
            "quote_mint": self.quote_mint,
            "price_usd": self.price_usd,
            "usd_value": self.usd_value,
            "program": self.program,
            "source": f"{PROVIDER}:{self.feed}",
        }


class AlphaRow(BaseModel):
    """A non-trade observation (signal / trenches / trending) destined for ALPHA_SIGNAL."""

    chain: Chain
    feed: str
    token: str
    ts_ms: int
    label: str | None = None
    symbol: str | None = None
    ident: str | None = None  # provider-side id when it has one
    bucket_s: int = 0  # 0 = emit once per token, else one emission per time bucket
    payload: dict[str, Any] = Field(default_factory=dict)
    #: A ``tokens``-row-shaped mapping when the feed object is a full token (trenches):
    #: ``symbol, name, creator, created_ms, migrated_ms, launchpad, pool, meta``. ``None``
    #: for feeds that only reference a token. See :func:`write_token`.
    token_facts: dict[str, Any] | None = None

    @property
    def dedupe_key(self) -> str:
        if self.ident:
            tail = self.ident
        elif self.bucket_s:
            tail = f"{self.token}:{self.ts_ms // (self.bucket_s * 1000)}"
        else:
            tail = self.token
        return f"{PROVIDER}:{self.feed}:{self.chain.value}:{tail}"


# --------------------------------------------------------------------------------------
# envelope handling
# --------------------------------------------------------------------------------------


def envelope_error(payload: Any) -> str | None:
    """``--raw`` gives the provider envelope. Returns an error string, or ``None`` if fine."""
    if payload is None:
        return "empty response"
    if not isinstance(payload, dict):
        return None if isinstance(payload, list) else f"unexpected payload type {type(payload).__name__}"
    code = payload.get("code")
    if code is None:
        return None
    try:
        if int(code) != 0:
            return f"code={code} msg={payload.get('msg') or payload.get('message') or 'unknown'}"
    except (TypeError, ValueError):
        return f"code={code!r}"
    return None


def _category_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the ``market trenches`` shape: one list per lifecycle category.

    Each row is copied with ``trenches_category`` set so the parser can tell a
    ``completed`` (graduated) object from a curve-stage one. Category order is fixed so
    a token present in two lists on the same poll is seen first at its earliest stage.
    """
    out: list[dict[str, Any]] = []
    for key in TRENCHES_CATEGORIES:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                out.append({**row, "trenches_category": row.get("trenches_category") or key})
    return out


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Pull the row list out of an envelope, a bare dict, a bare list or a category map."""
    data: Any = payload
    if isinstance(payload, dict):
        data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("list", "rank", "signals", "items", "trades", "tokens", "data", "result"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                data = candidate
                break
        else:
            # The trenches shape. Before this branch every real trenches payload parsed
            # to [] and the feed was silently empty on every chain (module docstring).
            data = _category_rows(data)
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = row.get(k)
        if v is not None and v != "":
            return v
    return None


def _nested(row: dict[str, Any], parent: str, *keys: str) -> Any:
    """``row[parent][key]`` for the first key present. GMGN nests three fields we want.

    ``base_token.symbol``, ``maker_info.name`` and ``maker_info.tags`` are all one level
    down, and ``_first`` only looks at the top level, so without this the symbol, the
    wallet's name and its smart-money tags were silently dropped from every row.
    """
    child = row.get(parent)
    if not isinstance(child, dict):
        return None
    return _first(child, *keys)


def _ts_ms(row: dict[str, Any], *keys: str) -> int | None:
    raw = _first(row, *keys)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e14:
        return int(value / 1000)
    if value > 1e11:
        return int(value)
    return int(value * 1000)


def _chain_of(row: dict[str, Any], default: Chain) -> Chain:
    raw = _first(row, "chain", "chain_id", "network")
    if isinstance(raw, str):
        try:
            return Chain(raw.strip().lower())
        except ValueError:
            return default
    return default


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _tags(row: dict[str, Any]) -> list[str]:
    """Wallet cohort labels. GMGN nests these under ``maker_info``.

    These are not decoration: ``smart_degen``, ``arbitrager``, ``sniper`` and the rest
    are the cohort evidence the wallet grader and the confluence lanes read. Losing
    them silently is losing the reason a wallet was worth following.
    """
    raw = _first(row, "wallet_tags", "tags", "tag_list")
    if raw is None:
        raw = _nested(row, "maker_info", "tags", "wallet_tags")
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _side(row: dict[str, Any]) -> str | None:
    raw = _first(row, "event_type", "side", "type", "direction", "tx_type")
    if raw is None:
        return None
    side = str(raw).strip().lower()
    if side in {"buy", "b", "in", "swap_in"}:
        return "buy"
    if side in {"sell", "s", "out", "swap_out"}:
        return "sell"
    return None


def _rug_ratio(row: dict[str, Any]) -> int | float | None:
    """GMGN's opaque 0-1 rug score exactly as the row carries it, or ``None``.

    Only a JSON number is a score. A string is not converted (a conversion is a guess
    about what the vendor meant), a bool is not a number even though Python says it is,
    and a non-finite float cannot be compared with the lane's threshold. ``market
    signal`` nests the token object under ``data`` and the score with it (MEASURED
    2026-09-22 on bsc: every signal row read carried it at ``data.rug_ratio``, all
    ``0``; coverage varies by read, module docstring); trending and trenches carry it
    at the top level. The top level wins when both are present.
    """
    value = row.get("rug_ratio")
    if value is None:
        child = row.get("data")
        value = child.get("rug_ratio") if isinstance(child, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _score_fields(row: dict[str, Any], category: str | None) -> dict[str, Any]:
    """The payload keys the readers look up by name, present only when the row has them.

    ``rug_ratio`` is what ``dyor._stored_feed_rug_ratio`` reads back within its 900 s
    budget; ``trenches_category`` is the lifecycle stage ``_category_rows`` stamped. An
    absent key is "not measured"; a ``None`` under the key would be the same fact
    written as a value, and ``0`` would be a lie.
    """
    out: dict[str, Any] = {}
    rug = _rug_ratio(row)
    if rug is not None:
        out["rug_ratio"] = rug
    if category:
        out["trenches_category"] = category
    return out


# --------------------------------------------------------------------------------------
# pure parsers
# --------------------------------------------------------------------------------------


def _parse_trades(payload: Any, chain: Chain, feed: str) -> list[SwapRow]:
    out: list[SwapRow] = []
    if envelope_error(payload):
        return out
    for row in _rows(payload):
        # `transaction_hash` and `base_address` are what GMGN actually sends, and their
        # absence here meant `_parse_trades` discarded EVERY row of the smartmoney and
        # kol feeds on EVERY chain. MEASURED 2026-09-21: both feeds returned 100 rows
        # per call on sol and on bsc and parsed to zero, so the paid smart-money and KOL
        # tape had never reached the database. The other aliases are kept because they
        # cost nothing and the vendor has changed this shape before.
        tx = _first(row, "transaction_hash", "tx_hash", "tx", "signature", "hash")
        wallet = _first(row, "wallet_address", "wallet", "address", "maker")
        token = _first(row, "base_address", "token_address", "token", "mint", "contract_address")
        side = _side(row)
        ts = _ts_ms(row, "timestamp", "ts", "time", "trade_time", "block_time")
        if not (tx and wallet and token and side):
            continue
        row_chain = _chain_of(row, chain)
        # The quote leg, in the provider's UI units. `quote_amount` is what the wire
        # carries (MEASURED); the two aliases are kept for fixtures and older shapes and
        # are treated the same way: a UI number of an asset the row may not even name.
        quote_ui = _dec_text(_first(row, "quote_amount", "native_amount", "amount_native"))
        quote_mint = _text(_first(row, "quote_address", "quote_mint", "quote_token_address"))
        amount_native, native_basis = native_atoms(row_chain, quote_ui, quote_mint)
        token_ui = _dec_text(_first(row, "token_amount", "amount", "base_amount"))
        out.append(
            SwapRow(
                chain=row_chain,
                tx=str(tx),
                ts_ms=ts or now_ms(),
                wallet=str(wallet),
                token=str(token),
                side=side,
                # UI text, by contract: every gmgn row has always carried the token
                # quantity this way and the grader scales it (grade.HUMAN_UNIT_SOURCE_
                # PREFIXES). Base units are resolved at write time onto the EVENT only.
                amount_token=token_ui,
                amount_token_ui=token_ui,
                amount_native=amount_native,
                amount_native_basis=native_basis,
                amount_quote=quote_ui,
                quote_mint=quote_mint,
                price_usd=_text(_first(row, "price_usd", "price")),
                usd_value=_text(_first(row, "volume_usd", "amount_usd", "usd", "value_usd")),
                slot=_int(_first(row, "slot", "block_number", "height")),
                program=_text(_first(row, "pool", "program", "dex", "exchange")),
                feed=feed,
                wallet_name=_text(
                    _first(row, "wallet_name", "name", "kol_name", "nickname")
                    or _nested(row, "maker_info", "name", "nickname")
                ),
                token_symbol=_text(
                    _first(row, "token_symbol", "symbol")
                    or _nested(row, "base_token", "symbol")
                ),
                tags=_tags(row),
                # MEASURED 2026-09-23 on the live box, over 24 h of `swaps`: 40.9% of the
                # Solana tokens a tracked wallet traded, 65.0% of the BSC ones and 19.3%
                # of the Robinhood ones had NO `tokens` row at all -- among them 590 sol
                # and 55 bsc tokens bought by a wallet we had scored. `write_swap` wrote
                # the tape and emitted `wallet.trade`, and that was the end of it: the
                # token never reached tier-0 triage, so the scanner never saw it and no
                # lane could ever act on it.
                #
                # That is the earliest signal we get, discarded. A tracked wallet buying
                # something we have not heard of is the definition of early alpha, and it
                # was reaching us and being dropped for want of one INSERT.
                #
                # The vendor nests the token object under `base_token`; merging it over
                # the trade row gives `_token_facts` the same keys the alpha feeds hand
                # it. Registration stays offline -- no provider call is made here.
                token_facts=_token_facts(
                    {**row, **(row.get("base_token") or {})}, row_chain, feed, str(token)
                ),
            )
        )
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_smartmoney(payload: Any, chain: Chain = Chain.SOL) -> list[SwapRow]:
    """``gmgn-cli track smartmoney --chain <c> --raw`` -> trade rows."""
    return _parse_trades(payload, chain, "smartmoney")


def parse_kol(payload: Any, chain: Chain = Chain.SOL) -> list[SwapRow]:
    """``gmgn-cli track kol --chain <c> --raw`` -> trade rows (same shape, KOL cohort)."""
    return _parse_trades(payload, chain, "kol")


def parse_signal(payload: Any, chain: Chain = Chain.SOL) -> list[AlphaRow]:
    """``gmgn-cli market signal --chain <c> --raw`` -> one row per signal (21 types)."""
    out: list[AlphaRow] = []
    if envelope_error(payload):
        return out
    for row in _rows(payload):
        token = _first(row, "token_address", "address", "token", "contract_address")
        if not token:
            continue
        # The real row (MEASURED 2026-09-22, bsc, the first 50 rows this feed ever
        # returned) has no top-level chain, symbol or timestamp: the token object is
        # nested under ``data`` (chain, symbol, rug_ratio, ...), ``cur_data`` carries the
        # live holder count / liquidity, and the time is ``trigger_at``. Without these
        # fallbacks every signal was stamped with our own clock and the polled chain,
        # and lost its symbol and its rug score.
        nested = row.get("data") if isinstance(row.get("data"), dict) else {}
        ts = (
            _ts_ms(row, "timestamp", "trigger_at", "ts", "time", "created_at", "trigger_time")
            or now_ms()
        )
        sig_type = _first(row, "signal_type", "type", "signal_id_type")
        ident = _first(row, "signal_id", "id")
        symbol = _first(row, "symbol", "token_symbol") or _nested(row, "data", "symbol")
        out.append(
            AlphaRow(
                chain=_chain_of(row, _chain_of(nested, chain)),
                feed="signal",
                token=str(token),
                ts_ms=ts,
                symbol=_text(symbol),
                label=_text(_first(row, "signal_name", "name", "title")) or f"signal_{sig_type}",
                ident=f"{PROVIDER}:signal:{ident}" if ident else None,
                bucket_s=0 if ident else 300,
                payload={
                    "signal_type": sig_type,
                    "signal_name": _first(row, "signal_name", "name"),
                    "description": _first(row, "description", "desc", "content"),
                    "market_cap_usd": _text(_first(row, "market_cap", "market_cap_usd", "mc")),
                    "liquidity_usd": _text(
                        _first(row, "liquidity", "liquidity_usd")
                        or _nested(row, "cur_data", "liquidity")
                        or _nested(row, "data", "liquidity")
                    ),
                    "price_usd": _text(_first(row, "price", "price_usd") or _nested(row, "data", "price")),
                    "symbol": symbol,
                    "timestamp_ms": ts,
                    **_score_fields(row, _text(row.get("trenches_category"))),
                },
                # `write_alpha` registers a token only when the row carries facts, and
                # this parser never built any -- so the signal feed emitted `alpha.signal`
                # for tokens that were never registered and never screened. `trenches` and
                # `trending` have always done this; `signal` was simply missed.
                token_facts=_token_facts(
                    {**row, **nested},
                    _chain_of(row, _chain_of(nested, chain)),
                    "signal",
                    str(token),
                ),
            )
        )
    return out


def _token_facts(row: dict[str, Any], chain: Chain, feed: str, address: str) -> dict[str, Any]:
    """The ``tokens``-row-shaped mapping for one full feed object.

    Shared by :func:`parse_trenches` and :func:`parse_trending` so a token's provenance
    cannot depend on which feed happened to see it first: the same keys are read, from
    the same aliases, and the ``meta`` blob records the launch venue (``launchpad``,
    ``launchpad_platform``, ``launchpad_status``), the trading venue (``exchange``,
    ``program``) and the quote leg for either feed.

    Nothing here is defaulted. A key the object does not carry is ``None`` and is dropped
    from ``meta`` entirely, so "the feed did not say" is never stored as ``0``, as an
    empty string, or as a plausible-looking launchpad name. That matters most on the
    fields the caller reads back as columns: ``launchpad``, ``created_ms`` and
    ``decimals`` on a manually deployed token are all genuinely unknown, and a guess at
    any of them is fabricated provenance that every downstream gate would read as fact.

    * ``created_ms`` <- ``created_timestamp`` (else ``open_timestamp``), never first-seen:
      an age invented from our own clock would read as a fresh launch to every age gate.
    * ``migrated_ms`` <- ``complete_timestamp`` when > 0. That is the bonding-curve
      graduation, which is exactly what ``pumpportal`` stamps into ``migrated_ms``.
    * ``decimals`` is absent from the object (MEASURED 2026-09-21 on trenches: 717/720)
      and is left unknown by the writer rather than assumed.
    """
    quote_address = _text(_first(row, "quote_address"))
    created_ms = _ts_ms(row, "created_timestamp", "creation_timestamp")
    creation_time_basis = "provider_created_timestamp" if created_ms is not None else None
    if created_ms is None:
        created_ms = _ts_ms(row, "open_timestamp")
        if created_ms is not None:
            creation_time_basis = "provider_open_timestamp_unverified"
    meta = {
        "source": token_source(feed),
        "provider": PROVIDER,
        "trenches_category": _text(row.get("trenches_category")),
        "observed_ms": now_ms(),
        "created_timestamp": _first(row, "created_timestamp"),
        "creation_timestamp": _first(row, "creation_timestamp"),
        "creation_time_basis": creation_time_basis,
        "open_timestamp": _first(row, "open_timestamp"),
        "complete_timestamp": _first(row, "complete_timestamp"),
        "quote_address": quote_address,
        "quote_address_type": _first(row, "quote_address_type"),
        "quote_is_native": is_native_quote(chain, quote_address) if quote_address else None,
        "exchange": _first(row, "exchange"),
        # The on-chain program / router the object names. A venue identity, kept beside
        # `exchange` rather than in `tokens.pool`, which holds a bonding-curve address.
        "program": _first(row, "program"),
        "launchpad_platform": _first(row, "launchpad_platform"),
        "launchpad_status": _first(row, "launchpad_status"),
        "logo": _first(row, "logo"),
        "total_supply": _text(_first(row, "total_supply")),
        "holder_count": _int(_first(row, "holder_count", "holders")),
        "smart_degen_count": _int(_first(row, "smart_degen_count", "smart_money_count")),
        "renowned_count": _int(_first(row, "renowned_count")),
        "bot_degen_count": _int(_first(row, "bot_degen_count")),
        "progress": _first(row, "progress", "bonding_curve_progress"),
        "liquidity_usd": _text(_first(row, "liquidity")),
        "market_cap_usd": _text(_first(row, "market_cap", "usd_market_cap")),
        "is_honeypot": _first(row, "is_honeypot"),
        "creator_token_status": _first(row, "creator_token_status"),
        "creator_created_count": _int(_first(row, "creator_created_count")),
        "fund_from_address": _first(row, "fund_from_address"),
        "bundler_trader_amount_rate": _first(row, "bundler_trader_amount_rate"),
        "rat_trader_amount_rate": _first(row, "rat_trader_amount_rate"),
        "fresh_wallet_rate": _first(row, "fresh_wallet_rate"),
        "top_10_holder_rate": _first(row, "top_10_holder_rate"),
        "buys_24h": _int(_first(row, "buys_24h")),
        "sells_24h": _int(_first(row, "sells_24h")),
    }
    return {
        "address": address,
        "symbol": _text(_first(row, "symbol", "token_symbol")),
        "name": _text(_first(row, "name", "token_name")),
        "creator": _text(_first(row, "creator", "creator_address", "dev")),
        "created_ms": created_ms,
        "migrated_ms": _ts_ms(row, "complete_timestamp"),  # 0 -> None: not graduated
        "launchpad": _text(_first(row, "launchpad", "launchpad_platform", "platform")),
        # `exchange` is a venue name (pancake_v2), not a pool address; the column holds
        # addresses (bonding curve key), so it stays unknown.
        "pool": None,
        "meta": {k: v for k, v in meta.items() if v is not None},
    }


def parse_trenches(payload: Any, chain: Chain = Chain.SOL) -> list[AlphaRow]:
    """``gmgn-cli market trenches --filter-preset smart-money --chain <c> --raw``.

    One row per token in the preset. Emitted once per token: entering the smart-money
    trenches list is the event, and the row's own numbers are the evidence.

    Every object is a full token record (MEASURED 2026-09-21: 111 keys, among them
    ``address, chain, symbol, name, creator, launchpad, created_timestamp,
    complete_timestamp, quote_address, holder_count, smart_degen_count``), so each row
    also carries :attr:`AlphaRow.token_facts` shaped for the ``tokens`` table. See
    :func:`_token_facts` for what is read and for what is deliberately left unknown.
    """
    out: list[AlphaRow] = []
    if envelope_error(payload):
        return out
    for row in _rows(payload):
        token = _first(row, "address", "token_address", "mint", "contract_address")
        if not token:
            continue
        row_chain = _chain_of(row, chain)
        facts = _token_facts(row, row_chain, "trenches", str(token))
        category = facts["meta"].get("trenches_category")
        ts = _ts_ms(row, "timestamp") or facts["created_ms"]
        out.append(
            AlphaRow(
                chain=row_chain,
                feed="trenches",
                token=str(token),
                ts_ms=ts or now_ms(),
                symbol=facts["symbol"],
                label=_text(_first(row, "preset", "filter_preset")) or TRENCHES_FILTER_PRESET,
                bucket_s=0,
                payload={
                    "progress": _first(row, "progress", "bonding_curve_progress"),
                    "smart_degen_count": _int(_first(row, "smart_degen_count", "smart_money_count")),
                    "renowned_count": _int(_first(row, "renowned_count")),
                    "holder_count": _int(_first(row, "holder_count", "holders")),
                    "launchpad": facts["launchpad"],
                    "market_cap_usd": _text(_first(row, "market_cap", "usd_market_cap")),
                    "liquidity_usd": _text(_first(row, "liquidity")),
                    "created_ms": facts["created_ms"],
                    "migrated_ms": facts["migrated_ms"],
                    "quote_address": facts["meta"].get("quote_address"),
                    "symbol": facts["symbol"],
                    # The preset this row came through. gmgn-cli filters on
                    # max_rug_ratio 0.3 server-side under it, so the rug_ratio below is
                    # biased by construction (module docstring); a reader must know.
                    "filter_preset": TRENCHES_FILTER_PRESET,
                    # rug_ratio: the raw number when the row has one, absent otherwise.
                    # Coverage varies by read; every EVM value seen is 0 (module doc).
                    **_score_fields(row, category),
                },
                token_facts=facts,
            )
        )
    return out


def parse_trending(payload: Any, chain: Chain = Chain.SOL) -> list[AlphaRow]:
    """``gmgn-cli market trending --chain <c> --raw``.

    Trending is a *state*, not an event, so rows are bucketed hourly: a token that stays
    on the list all afternoon produces one alpha event per hour, not one per poll.

    Each row also carries :attr:`AlphaRow.token_facts`, which is what makes this feed the
    agent's discovery route for a token no launch listener could have seen. Trending is
    the only feed here that is not keyed on a launchpad, so it is the only one that can
    surface a manually deployed contract; a row it returns with no recognised launchpad
    is DATA (register it, record that the launchpad is unknown, let tier 0 defer it into
    the DYOR queue), never a reason to drop the token and never a reason to invent a
    venue for it. ``ts_ms`` still falls back to our own clock because the alpha event is
    a sighting of ours; ``token_facts["created_ms"]`` never does.
    """
    out: list[AlphaRow] = []
    if envelope_error(payload):
        return out
    for row in _rows(payload):
        token = _first(row, "address", "token_address", "mint", "contract_address")
        if not token:
            continue
        row_chain = _chain_of(row, chain)
        facts = _token_facts(row, row_chain, "trending", str(token))
        ts = _ts_ms(row, "timestamp", "open_timestamp", "created_timestamp") or now_ms()
        out.append(
            AlphaRow(
                chain=row_chain,
                feed="trending",
                token=str(token),
                ts_ms=ts,
                symbol=facts["symbol"],
                label="trending",
                bucket_s=3600,
                token_facts=facts,
                payload={
                    "rank": _int(_first(row, "rank", "index")),
                    "volume_usd": _text(_first(row, "volume", "volume_usd", "volume_24h")),
                    "swaps": _int(_first(row, "swaps", "swaps_24h", "txs")),
                    "price_change_pct": _first(
                        row, "price_change_percent1h", "price_change_percent", "price_change_1h"
                    ),
                    "smart_degen_count": _int(_first(row, "smart_degen_count")),
                    "holder_count": _int(_first(row, "holder_count", "holders")),
                    "market_cap_usd": _text(_first(row, "market_cap", "usd_market_cap")),
                    "symbol": _first(row, "symbol", "token_symbol"),
                    # rug_ratio was on every real trending row read (MEASURED 2026-09-22
                    # on sol, bsc and robinhood; coverage varies by read and every EVM
                    # value seen is 0) and was dropped here, so dyor's stored-feed read
                    # could never see a trending token's score.
                    **_score_fields(row, _text(row.get("trenches_category"))),
                },
            )
        )
    return out


# --------------------------------------------------------------------------------------
# feed registry — this table is the contract with the CLI wrapper
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FeedSpec:
    name: str
    group: str
    command: str
    endpoint: str  # limiter endpoint, "family.name"
    parser: Callable[[Any, Chain], list[Any]]
    kind: str  # "swaps" | "alpha"
    flags: dict[str, Any] = field(default_factory=dict)
    chains: frozenset[Chain] = frozenset(Chain)
    time_ordered: bool = True


ALL_CHAINS = frozenset(Chain)
#: `market signal` is documented for sol/bsc/robinhood/arc/stable only.
SIGNAL_CHAINS = frozenset({Chain.SOL, Chain.BSC, Chain.ROBINHOOD, Chain.ARC, Chain.STABLE})
#: trenches is a launchpad view; only the bonding-curve chains carry one.
TRENCHES_CHAINS = frozenset({Chain.SOL, Chain.BSC, Chain.ROBINHOOD})

FEEDS: dict[str, FeedSpec] = {
    "smartmoney": FeedSpec(
        name="smartmoney", group="track", command="smartmoney", endpoint="track.smartmoney",
        parser=parse_smartmoney, kind="swaps", flags={"limit": 100}, chains=ALL_CHAINS,
    ),
    "kol": FeedSpec(
        name="kol", group="track", command="kol", endpoint="track.kol",
        parser=parse_kol, kind="swaps", flags={"limit": 100}, chains=ALL_CHAINS,
    ),
    "signal": FeedSpec(
        name="signal", group="market", command="signal", endpoint="market.signal",
        # No ``--limit``: ``gmgn-cli market signal`` does not take one. MEASURED 2026-09-21
        # on the live box: 129 calls/hour, 129 errors, every one "unknown option '--limit'",
        # for as long as the feed had existed -- the signal feed had never returned a row.
        parser=parse_signal, kind="alpha", flags={}, chains=SIGNAL_CHAINS,
    ),
    "trenches": FeedSpec(
        name="trenches", group="market", command="trenches", endpoint="market.trenches",
        parser=parse_trenches, kind="alpha",
        flags={"filter_preset": TRENCHES_FILTER_PRESET, "limit": 50}, chains=TRENCHES_CHAINS,
        time_ordered=False,
    ),
    "trending": FeedSpec(
        name="trending", group="market", command="trending", endpoint="market.trending",
        parser=parse_trending, kind="alpha",
        flags={"interval": "1h", "limit": 50}, chains=ALL_CHAINS, time_ordered=False,
    ),
}

DEFAULT_FEEDS: tuple[str, ...] = ("smartmoney", "kol", "signal", "trenches", "trending")


def feed_chains() -> list[Chain]:
    """Chains to poll: whatever the operator enabled in ``config/risk.yaml``, else Solana."""
    enabled = [c for c, budget in (get_risk().chains or {}).items() if budget.enabled]
    return enabled or [Chain.SOL]


def invocation(chain: Chain, feed: str) -> tuple[str, str, dict[str, Any]]:
    """The exact ``(group, command, flags)`` we hand the wrapper. Used by tests and docs."""
    spec = FEEDS[feed]
    return spec.group, spec.command, {"chain": chain.value, **spec.flags}


# --------------------------------------------------------------------------------------
# cursors
# --------------------------------------------------------------------------------------


def cursor_key(chain: Chain, feed: str) -> str:
    return f"{CURSOR_PREFIX}:{chain.value}:{feed}"


def load_cursor(conn: Any, chain: Chain, feed: str) -> dict[str, Any]:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (cursor_key(chain, feed),))
    raw = jload(row["value"], {}) if row else {}
    return {
        "ts_ms": int(raw.get("ts_ms") or 0),
        "seen": list(raw.get("seen") or []),
        "updated_ms": int(raw.get("updated_ms") or 0),
    }


def save_cursor(conn: Any, chain: Chain, feed: str, cursor: dict[str, Any]) -> None:
    cursor["seen"] = list(cursor.get("seen") or [])[-SEEN_LIMIT:]
    cursor["updated_ms"] = now_ms()
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (cursor_key(chain, feed), jdump(cursor), cursor["updated_ms"]),
    )


def select_new(rows: Sequence[Any], cursor: dict[str, Any], *, time_ordered: bool) -> list[Any]:
    """Rows we have not accepted before.

    Time-ordered feeds also drop anything older than the watermark, so a provider replaying
    an old page cannot resurrect stale trades once they have aged out of ``seen``.
    """
    seen = set(cursor["seen"])
    watermark = cursor["ts_ms"]
    fresh: list[Any] = []
    for row in rows:
        if row.dedupe_key in seen:
            continue
        if time_ordered and watermark and row.ts_ms < watermark:
            continue
        seen.add(row.dedupe_key)
        fresh.append(row)
    return fresh


def advance_cursor(cursor: dict[str, Any], rows: Sequence[Any]) -> dict[str, Any]:
    if rows:
        cursor["ts_ms"] = max([cursor["ts_ms"], *(r.ts_ms for r in rows)])
        cursor["seen"] = [*cursor["seen"], *(r.dedupe_key for r in rows)][-SEEN_LIMIT:]
    return cursor


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def resolve_token_atoms(conn: Any, row: SwapRow) -> SwapRow:
    """Fill ``amount_token_atoms`` from the UI text when the decimals are already known.

    The only source is the ``tokens`` registry via ``fills.token_decimals(fetch=False)``:
    no chain read on the ingest path, and no guess. ``amount_token`` is NEVER touched:
    the column carries the UI text on every gmgn row by contract (class doc), and a row
    written in base units next to one written in UI text is the mixed-units episode
    ``pnl`` flags ``oversold``. A token the registry has never seen, or has seen without
    decimals, keeps ``amount_token_atoms=None``; the UI value stays in ``amount_token``
    and rides on the ``wallet.trade`` event as ``amount_token_ui``. Never raises.
    """
    if row.amount_token_ui is None:
        return row
    try:
        from kaiba.execution.fills import DECIMALS_UNAVAILABLE, token_decimals  # circular at import

        decimals, basis, _note = token_decimals(row.chain, row.token, conn, fetch=False)
    except Exception as exc:  # noqa: BLE001 - a registry hiccup must not lose the trade
        log.debug("gmgn decimals lookup failed for %s (%s)", row.token[:12], exc)
        return row.model_copy(update={"token_decimals_basis": "unavailable"})
    if decimals is None:
        return row.model_copy(update={"token_decimals_basis": basis or DECIMALS_UNAVAILABLE})
    return row.model_copy(
        update={
            "amount_token_atoms": ui_to_atoms(row.amount_token_ui, int(decimals)),
            "token_decimals": int(decimals),
            "token_decimals_basis": basis,
        }
    )


def write_swap(conn: Any, row: SwapRow) -> bool:
    row = resolve_token_atoms(conn, row)
    # Register the token this trade was in, before the tape row. `write_token` is
    # idempotent, never overwrites a listener's row, emits `token.created` once and hands
    # the token to tier-0 triage -- which is what actually puts it in front of the
    # scanner. Without this the tape knew about a token the rest of the system did not.
    #
    # Deliberately not guarded by whether the swap is new: a token can be traded many
    # times before we ever register it, and an old trade is still the first time we
    # learned the token exists.
    if row.token_facts:
        write_token(conn, row)
    swap = row.to_swap()
    cur = conn.execute(
        "INSERT OR IGNORE INTO swaps "
        "(chain, tx, slot, ts_ms, wallet, token, side, amount_token, amount_native, "
        " amount_quote, quote_mint, price_usd, usd_value, program, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            swap["chain"], swap["tx"], swap["slot"], swap["ts_ms"], swap["wallet"], swap["token"],
            swap["side"], swap["amount_token"], swap["amount_native"], swap["amount_quote"],
            swap["quote_mint"], swap["price_usd"], swap["usd_value"], swap["program"],
            swap["source"],
        ),
    )
    emit_once(
        EventKind.WALLET_TRADE,
        {
            **swap,
            "feed": row.feed,
            "wallet_name": row.wallet_name,
            "token_symbol": row.token_symbol,
            "tags": row.tags,
            "amount_token_ui": row.amount_token_ui,
            "amount_token_atoms": row.amount_token_atoms,
            "token_decimals": row.token_decimals,
            "token_decimals_basis": row.token_decimals_basis,
            "amount_native_basis": row.amount_native_basis,
        },
        chain=row.chain,
        subject=row.wallet,
        dedupe_key=f"{EventKind.WALLET_TRADE.value}:{row.dedupe_key}",
        conn=conn,
    )
    return bool(cur.rowcount)


def write_alpha(conn: Any, row: AlphaRow) -> bool:
    event_id = emit_once(
        EventKind.ALPHA_SIGNAL,
        {
            "provider": PROVIDER,
            "feed": row.feed,
            "label": row.label,
            "token": row.token,
            "symbol": row.symbol,
            "chain": row.chain.value,
            "observed_ms": row.ts_ms,
            **row.payload,
        },
        chain=row.chain,
        subject=row.token,
        dedupe_key=f"{EventKind.ALPHA_SIGNAL.value}:{row.dedupe_key}",
        conn=conn,
    )
    # Independent of whether the event was new: a row registered by an older build that
    # only emitted the event still deserves its tokens row, and the upsert is idempotent.
    if row.token_facts:
        write_token(conn, row)
    return event_id is not None


# --------------------------------------------------------------------------------------
# tokens from full feed objects (chains without a listener)
# --------------------------------------------------------------------------------------


def _screen_token(conn: Any, chain: Chain, facts: dict[str, Any], source: str) -> None:
    """Hand a newly registered token to tier-0 triage, as both listeners do.

    Imported lazily because ``kaiba.execution`` imports back into ``kaiba.ingest``, and
    swallowed because a screening failure must never cost the ``tokens`` row: knowing the
    token exists is worth more than knowing what we thought of it. A dict is passed, as
    ``robinhood._screen`` does, so ``triage.parse_launch`` reads the chain from the
    payload and does not apply the Solana address check to a 0x address.

    ``launchpad`` is forwarded exactly as the feed gave it, ``None`` included. Tier 0
    labels an absent one ``triage.LAUNCHPAD_UNKNOWN`` in its own row; substituting a
    venue here would put a launchpad the vendor never named into the token registry.
    """
    try:
        from kaiba.execution.triage import screen_launch

        meta = facts.get("meta") or {}
        screen_launch(
            {
                "chain": chain.value,
                "mint": facts["address"],
                "creator": facts.get("creator"),
                "name": facts.get("name"),
                "symbol": facts.get("symbol"),
                "launchpad": facts.get("launchpad"),
                "pool": facts.get("pool"),
                "timestamp": facts.get("created_ms"),
                "image": meta.get("logo"),
                "source": source,
                "trenches_category": meta.get("trenches_category"),
            },
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - triage is advisory, ingest is not
        log.debug("gmgn triage skipped %s (%s: %s)", facts.get("address", "")[:12], type(exc).__name__, exc)


def write_token(conn: Any, row: AlphaRow) -> str:
    """Register a full feed object in ``tokens``.

    Returns what happened: ``"inserted"`` (new row, ``token.created`` emitted, screened),
    ``"updated"`` (our own earlier row refreshed), ``"kept"`` (a row this module did not
    write exists and is left untouched) or ``"skipped"`` (nothing to write, or a feed
    whose chain has a listener that owns the row -- see :data:`LISTENER_OWNED_FEEDS`).
    Mirrors ``pumpportal.record_new_token`` column for column: same table, same conflict
    target, same refusal to touch ``first_seen_ms`` on a later sighting. Existing birth
    and migration occurrence times are also retained: a provider pool-open refresh must
    not make an old token young again or restart a migration grace period. Null times
    can be filled once; verified historical corrections use a separate audited path.

    Ownership is decided by the stored row's own ``meta_json["source"]``, not by the
    chain: any row whose source is not one of ours is kept exactly as its writer left it,
    which is what lets a listener chain also carry rows this module discovered.
    """
    facts = row.token_facts
    if not facts or not facts.get("address"):
        return "skipped"
    if row.chain in LISTENER_CHAINS and row.feed in LISTENER_OWNED_FEEDS:
        return "skipped"
    address = str(facts["address"])
    meta = dict(facts.get("meta") or {})
    meta["source"] = token_source(row.feed)
    existing = fetch_one(
        conn, "SELECT meta_json, created_ms FROM tokens WHERE chain=? AND address=?", (row.chain.value, address)
    )
    if existing is not None:
        old_meta = jload(existing["meta_json"], {}) or {}
        if not isinstance(old_meta, dict) or not str(
            old_meta.get("source") or ""
        ).startswith(TOKEN_SOURCE_PREFIX):
            return "kept"
        # Operator lifecycle repair: pool openings can move while token birth cannot.
        # Preserve the provenance of the retained birth, not the incoming pool timestamp.
        merged_meta = {**old_meta, **meta}
        if existing["created_ms"] is not None:
            merged_meta["creation_time_basis"] = old_meta.get("creation_time_basis") or "legacy_unverified"
        conn.execute(
            "UPDATE tokens SET symbol=COALESCE(?, symbol), name=COALESCE(?, name), "
            "creator=COALESCE(?, creator), created_ms=COALESCE(created_ms, ?), "
            "migrated_ms=COALESCE(migrated_ms, ?), launchpad=COALESCE(?, launchpad), "
            "pool=COALESCE(?, pool), meta_json=? WHERE chain=? AND address=?",
            (
                facts.get("symbol"), facts.get("name"), facts.get("creator"), facts.get("created_ms"),
                facts.get("migrated_ms"), facts.get("launchpad"), facts.get("pool"),
                jdump(merged_meta), row.chain.value, address,
            ),
        )
        return "updated"
    try:
        conn.execute(
            "INSERT INTO tokens (chain, address, symbol, name, decimals, creator, created_ms, "
            " launchpad, pool, migrated_ms, first_seen_ms, meta_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.chain.value, address, facts.get("symbol"), facts.get("name"), None,
                facts.get("creator"), facts.get("created_ms"), facts.get("launchpad"),
                facts.get("pool"), facts.get("migrated_ms"), now_ms(), jdump(meta),
            ),
        )
    except sqlite3.IntegrityError:
        # Another writer (e.g. `fills._store_decimals(register=True)`) registered the
        # address between our SELECT and INSERT. Its row wins, exactly as a listener's
        # would; losing the rest of the sweep to a duplicate key would be the real bug.
        return "kept"
    emit_once(
        EventKind.TOKEN_CREATED,
        {
            "mint": address,
            "symbol": facts.get("symbol"),
            "name": facts.get("name"),
            "creator": facts.get("creator"),
            "launchpad": facts.get("launchpad"),
            "created_ms": facts.get("created_ms"),
            "migrated_ms": facts.get("migrated_ms"),
            "trenches_category": meta.get("trenches_category"),
            "source": meta["source"],
        },
        chain=row.chain,
        subject=address,
        dedupe_key=f"{EventKind.TOKEN_CREATED.value}:{row.chain.value}:{address}",
        conn=conn,
    )
    if SCREEN_NEW_TOKENS:
        _screen_token(conn, row.chain, facts, meta["source"])
    return "inserted"


# --------------------------------------------------------------------------------------
# polling
# --------------------------------------------------------------------------------------

_warned_missing_wrapper = False


def _provider_error(conn: Any, chain: Chain, feed: str, detail: str) -> None:
    emit(
        EventKind.PROVIDER_ERROR,
        {"provider": PROVIDER, "feed": feed, "endpoint": FEEDS[feed].endpoint,
         "chain": chain.value, "error": detail},
        chain=chain,
        level="warn",
        dedupe_key=f"{EventKind.PROVIDER_ERROR.value}:{PROVIDER}:{feed}:{chain.value}:{digest(detail)}"
                   f":{now_ms() // 60000}",
        conn=conn,
    )


def poll_once(
    chain: Chain,
    feed: str,
    conn: Any = None,
    *,
    runner: Callable[..., Any] | None = None,
    priority: Priority = Priority.DISCOVERY,
    max_wait_s: float = 0.0,
) -> int:
    """Poll one (chain, feed). Returns how many rows were new.

    ``max_wait_s`` is how long this call may wait out a limiter hint before giving up
    its slot for the sweep. It defaults to 0 -- no waiting -- so a caller that has not
    thought about its own time budget keeps the old fail-fast behaviour.

    Never raises: a provider failure is a ``PROVIDER_ERROR`` event and a zero.
    """
    global _warned_missing_wrapper
    spec = FEEDS.get(feed)
    if spec is None:
        raise KeyError(f"unknown gmgn feed {feed!r}; known: {sorted(FEEDS)}")
    c = conn or get_conn()
    if chain not in spec.chains:
        log.debug("gmgn %s: chain %s not supported, skipping", feed, chain.value)
        return 0

    call = runner or gmgn_run
    if call is None:
        if not _warned_missing_wrapper:
            _warned_missing_wrapper = True
            log.warning("kaiba.providers.gmgn_cli is unavailable; gmgn feeds are idle")
        _provider_error(c, chain, feed, "gmgn_cli wrapper unavailable")
        return 0

    group, command, flags = invocation(chain, feed)
    try:
        if call is gmgn_run:
            # DOUBLE-GUARD, found 2026-09-21, and it had silently emptied every GMGN
            # feed on every chain since the wrapper landed. `run_read` already opens
            # `with guarded(...)` internally (gmgn_cli.py:801). Wrapping it in a second
            # reservation here meant the OUTER one took the slot and the INNER one was
            # refused on `min_interval_ms`, so `run_read` returned `(None, receipt)` and
            # this function parsed None into an empty list and wrote nothing -- without
            # an error, because "no rows" is not a failure. MEASURED: outside the outer
            # guard the same call returns 100 rows; inside it, None.
            #
            # `run_read`'s own docstring describes exactly this and offers the remedy we
            # now use: hand it the wait budget so it can wait for its own slot. This is
            # the module's SECOND silent-idle bug of the same shape -- see the import
            # adapter note at the top of this file -- which is why both now log at INFO.
            payload = call(
                group, command,
                _priority=priority, _wait_for_slot_s=max_wait_s, _conn=c,
                **flags,
            )
        else:
            # An injected runner (tests, offline fixtures) does not meter itself, so it
            # is metered here. `test_poll_once_reserves_through_the_limiter` pins this.
            with guarded(PROVIDER, spec.endpoint, priority, conn=c):
                payload = call(group, command, **flags)
    except RateLimited as exc:
        # A deferral here used to cost the whole (chain, feed) for an entire sweep, and
        # the hint is routinely a tenth of a second. MEASURED 2026-09-21: every one of
        # the five bsc feeds deferred on every sweep with "retry in 0.0-0.1s" while sol
        # and robinhood, earlier in the iteration, drained the bucket ahead of them --
        # bsc ingested nothing at all for its first half hour enabled. Waiting out a
        # hint this small is far cheaper than losing the feed until the next sweep.
        # Bounded and single-shot: if the hint is longer than the caller's budget we
        # still defer, because a long hint means the bucket is genuinely empty.
        if max_wait_s > 0 and exc.retry_after_s <= max_wait_s:
            time.sleep(exc.retry_after_s)
            try:
                with guarded(PROVIDER, spec.endpoint, priority, conn=c):
                    payload = call(group, command, **flags)
            except RateLimited as retry_exc:
                log.info("gmgn %s/%s deferred after waiting %.2fs: %s",
                         chain.value, feed, exc.retry_after_s, retry_exc)
                return 0
            except Exception as retry_exc:  # noqa: BLE001 — a dead provider is data
                _provider_error(c, chain, feed, f"{type(retry_exc).__name__}: {retry_exc}")
                return 0
        else:
            log.info("gmgn %s/%s deferred: %s", chain.value, feed, exc)
            return 0
    except Exception as exc:  # noqa: BLE001 — a dead provider is data, not a crash
        _provider_error(c, chain, feed, f"{type(exc).__name__}: {exc}")
        return 0

    if payload is None:
        # run_read exhausted its wait budget or the CLI failed; it has already recorded
        # the reason on its own receipt. Treat as a deferral, not as an empty answer --
        # parsing None into [] is what made the double-guard invisible for so long.
        log.info("gmgn %s/%s: no payload (deferred or unavailable)", chain.value, feed)
        return 0

    error = envelope_error(payload)
    if error:
        _provider_error(c, chain, feed, error)
        return 0

    rows = spec.parser(payload, chain)
    cursor = load_cursor(c, chain, feed)
    fresh = select_new(rows, cursor, time_ordered=spec.time_ordered)
    written = 0
    fresh_keys: set[str] = set()
    for row in fresh:
        if spec.kind == "swaps":
            write_swap(c, row)
        else:
            write_alpha(c, row)
        fresh_keys.add(row.dedupe_key)
        written += 1
    if spec.kind == "alpha":
        # The event marks the ENTRY into the preset and is emitted once per token; the
        # ``tokens`` row tracks the LIFECYCLE and must follow the object on every poll.
        # A token the cursor already remembers still graduates (near_completion ->
        # completed sets ``migrated_ms``), and the cursor would otherwise hide that for
        # as long as it remembers the token. Refreshing is one SELECT and one UPDATE per
        # object, and it never inserts: an unknown token is inserted only on entry.
        for row in rows:
            if row.dedupe_key not in fresh_keys and getattr(row, "token_facts", None):
                write_token(c, row)
    save_cursor(c, chain, feed, advance_cursor(cursor, fresh))
    if written:
        log.info("gmgn %s/%s: %d new of %d rows", chain.value, feed, written, len(rows))
    return written


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, waking early on stop. Tests patch this."""
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


#: Advances once per sweep so the chain that goes first keeps changing. A plain int
#: rather than a cursor: losing it on restart costs nothing, and starting from sol every
#: boot is exactly as fair as starting anywhere else.
_sweep_turn = 0

#: How long a single feed may wait out a limiter hint before giving up its slot. Sized
#: from the hints actually observed (0.0-0.2 s against ``min_interval_ms: 250``), not
#: from the budget: this is meant to absorb the gap between two calls, never a drained
#: bucket. Fifteen pairs x 0.3 s is 4.5 s worst case inside a 60 s sweep.
FEED_MAX_WAIT_S = 0.3


def sweep_order(chains: Sequence[Chain], feeds: Sequence[str], turn: int) -> list[tuple[Chain, str]]:
    """The order one sweep visits (chain, feed) pairs in. Feed-major, chain-rotated.

    Two separate problems, one ordering.

    *Feed-major* spreads a limited budget ACROSS chains instead of spending it all on
    the first one. Chain-major asks sol for all five feeds before it asks bsc for
    anything, so a bucket that only covers seven calls serves sol completely and bsc not
    at all. Taking one feed from every chain in turn means a short budget degrades into
    "every chain got its most important feeds" rather than "one chain got everything".

    *Rotation* fixes the residual unfairness. Whoever is last still loses the tail of
    the budget, so who is last has to keep moving; over ``len(chains)`` sweeps each
    chain leads once. MEASURED 2026-09-21: with neither of these, bsc was last in
    ``feed_chains()`` and ingested zero rows in its first half hour enabled while sol
    and robinhood took 417 and 117 tokens.

    ``feeds`` order is preserved and is itself a priority: ``DEFAULT_FEEDS`` lists the
    cheap high-signal feeds first, so the pairs that survive a short budget are the ones
    worth keeping.
    """
    chain_list = list(chains)
    feed_list = list(feeds)
    if not chain_list or not feed_list:
        return []
    shift = turn % len(chain_list)
    rotated = chain_list[shift:] + chain_list[:shift]
    return [(chain, feed) for feed in feed_list for chain in rotated]


def pace_s() -> float:
    """Seconds to leave between two feed calls, read from the limiter's own budget.

    MEASURED 2026-09-21, and this was the whole bug. gmgn was running at 0.40 req/s
    against a 4.0 req/s ceiling -- the budget was never close to exhausted -- yet every
    bsc feed was refused on every sweep. ``poll_all`` fired all fifteen (chain, feed)
    pairs back to back in about 150 ms, and ``min_interval_ms: 250`` refuses anything
    that arrives inside a quarter second of the previous call. So the sweep was not out
    of budget, it was out of SPACING: the first call went, the rest bounced, and
    whichever chain sat at the tail of the iteration never got asked at all.

    Fifteen pairs at 250 ms is 3.75 s inside a 60 s sweep, so pacing costs nothing we
    have. Derived from the limiter rather than hardcoded, so raising or lowering the
    gmgn budget moves this with it instead of silently re-breaking the sweep.
    """
    try:
        from kaiba.core.limiter import limits_for

        return max(0.0, limits_for(PROVIDER).min_interval_ms / 1000.0)
    except Exception as exc:  # noqa: BLE001 - an unreadable budget must not stop the sweep
        log.debug("could not read the %s min interval, pacing off: %s", PROVIDER, exc)
        return 0.0


async def poll_all(
    chains: Iterable[Chain], feeds: Iterable[str], conn: Any = None, *, stop: asyncio.Event | None = None
) -> int:
    """One sweep over every (chain, feed) pair, paced to the provider's minimum interval.

    Blocking work is pushed off the loop; the gap between calls is awaited on the loop so
    a stop still lands promptly.
    """
    global _sweep_turn
    pairs = sweep_order(list(chains), list(feeds), _sweep_turn)
    _sweep_turn += 1
    gap = pace_s()
    total = 0
    for index, (chain, feed) in enumerate(pairs):
        if stop is not None and stop.is_set():
            return total
        if index and gap:
            await _wait(stop or asyncio.Event(), gap)
            if stop is not None and stop.is_set():
                return total
        total += await asyncio.to_thread(poll_once, chain, feed, conn, max_wait_s=FEED_MAX_WAIT_S)
    return total


def _note_events(count: int, conn: Any) -> None:
    """Report written rows to ``ingest_status`` so this feed's health is visible.

    MEASURED 2026-09-21 on the live box: ``ingest_status`` showed the gmgn feed as
    ``running`` with ``events_seen 0`` and ``last_event_ms None`` for 1.5 hours while the
    journal showed it writing ~200 rows a sweep on three chains. pumpportal and robinhood
    report; this feed never did, so the one dashboard row that exists to tell a dead feed
    from a quiet one said "dead" about the feed that feeds sm-trenches. Imported lazily:
    ``runner`` imports this module, so a top-level import is a cycle.
    """
    if count <= 0:
        return
    try:
        from kaiba.ingest.runner import note_events

        note_events("gmgn", count, conn)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never stop the sweep
        log.debug("could not report gmgn ingest progress: %s", exc)


async def run(
    interval_s: float = 60.0,
    stop: asyncio.Event | None = None,
    *,
    chains: Iterable[Chain] | None = None,
    feeds: Iterable[str] | None = None,
    conn: Any = None,
) -> None:
    """Poll every enabled (chain, feed) on a fixed interval until ``stop``."""
    stop = stop or asyncio.Event()
    feed_names = list(feeds or DEFAULT_FEEDS)
    while not stop.is_set():
        targets = list(chains) if chains is not None else feed_chains()
        try:
            new_rows = await poll_all(targets, feed_names, conn, stop=stop)
            log.debug("gmgn sweep: %d new rows across %d chains", new_rows, len(targets))
            _note_events(new_rows, conn)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the scheduler outlives any single sweep
            log.exception("gmgn sweep failed: %s", exc)
        await _wait(stop, interval_s)


# --------------------------------------------------------------------------------------
# one-shot repair of the rows written before the money rules above
# --------------------------------------------------------------------------------------

#: Rows the repair touches. Every ``gmgn:*`` row's ``amount_native`` was the UI
#: ``quote_amount`` (MEASURED 2026-09-21: 50,747 of 50,805 carry a dot; the other 58
#: are whole UI numbers, just as wrong), and any dotted value anywhere is UI by
#: definition. ``amount_quote IS NULL`` is what makes a second run a no-op: the move
#: itself fills it.
BACKFILL_PREDICATE = (
    "amount_quote IS NULL AND amount_native IS NOT NULL "
    "AND (source LIKE 'gmgn:%' OR instr(amount_native, '.') > 0)"
)


def backfill_amount_native(
    conn: Any, *, dry_run: bool = False, batch_size: int = 5000
) -> dict[str, Any]:
    """Move UI-unit values out of ``swaps.amount_native`` into ``amount_quote``.

    Idempotent and resumable: each batch is its own transaction, a row is a candidate
    only while ``amount_quote`` is still NULL, and nothing else on the row changes.
    ``quote_mint`` stays NULL because the feed never named the asset (that is the whole
    reason the value cannot be base units). Returns the counts it measured.
    """
    by_source = {
        str(r["source"]): int(r["n"])
        for r in conn.execute(
            f"SELECT source, COUNT(*) AS n FROM swaps WHERE {BACKFILL_PREDICATE} GROUP BY source"
        )
    }
    candidates = sum(by_source.values())
    moved = 0
    if not dry_run and candidates:
        step = max(1, int(batch_size))
        while True:
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    "UPDATE swaps SET amount_quote = amount_native, amount_native = NULL "
                    f"WHERE id IN (SELECT id FROM swaps WHERE {BACKFILL_PREDICATE} LIMIT ?)",
                    (step,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            n = int(cur.rowcount or 0)
            moved += n
            if n < step:
                break
    remaining = int(conn.execute(f"SELECT COUNT(*) FROM swaps WHERE {BACKFILL_PREDICATE}").fetchone()[0])
    dotted_left = int(
        conn.execute("SELECT COUNT(*) FROM swaps WHERE instr(amount_native, '.') > 0").fetchone()[0]
    )
    return {
        "dry_run": bool(dry_run),
        "candidates": candidates,
        "by_source": by_source,
        "moved": moved,
        "remaining": remaining,
        "dotted_amount_native_left": dotted_left,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m kaiba.ingest.gmgn_feeds backfill-amount-native --db PATH [--dry-run]``.

    Run from the project root with the project interpreter. Prints one JSON object.
    """
    parser = argparse.ArgumentParser(prog="kaiba.ingest.gmgn_feeds")
    sub = parser.add_subparsers(dest="command", required=True)
    bf = sub.add_parser("backfill-amount-native", help=backfill_amount_native.__doc__.splitlines()[0])
    bf.add_argument("--db", required=True, help="path to kaiba.db (a copy first; then the real one)")
    bf.add_argument("--dry-run", action="store_true", help="count candidates, change nothing")
    bf.add_argument("--batch", type=int, default=5000, help="rows per transaction")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "backfill-amount-native":
        db_path = Path(args.db)
        if not db_path.exists():
            print(json.dumps({"error": f"no such database: {db_path}"}))
            return 2
        conn = connect(db_path)
        try:
            report = backfill_amount_native(conn, dry_run=args.dry_run, batch_size=args.batch)
        finally:
            conn.close()
        print(json.dumps({"db": str(db_path), **report}, sort_keys=True))
        return 0
    return 2  # pragma: no cover - argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
