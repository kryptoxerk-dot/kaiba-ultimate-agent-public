"""Hunter digest: the few new things the owner could look at today. No model, read-only.

WHY. The hunters ran every 30 minutes and nothing they found reached the owner. Measured
on the box on 2026-10-02: 880 airdrop/points rows and 0 qualified (the EV model's time
cost of 1-2 h at $60/h exceeds the largest gross any scraped lead can carry, $11.52, so
``qualification`` can never pass one); 200 NFT rows, all refused, from a launchpad list
whose newest launch was 2026-02-20; listings and early-alpha signals written to tables
that only a paused, ``deliver: local`` Hermes job ever read. The research existed; the
delivery did not.

WHAT THIS IS. A ranked list per kind -- nft, airdrop (incl. points), listing, alpha -- of
rows first seen in the window, each with the concrete facts that put it on the list and a
link. It is deliberately NOT the funded-action gate: :mod:`kaiba.hunters.qualification`
still refuses every one of these for automated action, and nothing here mints, claims,
signs, bridges or trades. A lead here means "worth the owner's two minutes", and every
line says why in facts the owner can check, never in an adjective.

THE ONE RELAXATION, AND WHY IT IS SAFE. ``qualification`` asks for a research receipt
(eligibility, value, route, all verified) that no collector can produce, which is correct
for money the agent would spend on its own. A human reading a link needs less: a live
programme, on a chain a Kaiba wallet is funded on, with a stated fact (a claim checker, a
points season, a mint below its own secondary floor, a tier-1 exchange listing). Those are
the inclusion rules below; everything else is counted, by reason, not shown.

DEDUPE. ``kv['hunters:digest:sent']`` holds the keys already delivered. Reading it is
read-only; only an explicit ``--mark-sent`` run writes it. The daily report reads the
cursor (so nothing a separate digest push already sent is repeated) and does not write it:
its own 24 h window is what keeps one report from repeating the last.

    python -m kaiba.hunters.digest [--db PATH] [--since-hours 24] [--per-kind 5] [--json] [--mark-sent]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.schemas import now_ms

KINDS = ("nft", "airdrop", "listing", "alpha")
CURSOR_KEY = "hunters:digest:sent"
#: Sent keys older than this are forgotten; every source here re-keys a new event anyway.
CURSOR_KEEP_MS = 30 * 86_400_000
CURSOR_MAX_KEYS = 5000
#: Bounded reads. Each table is read through an index on its time column (opportunities
#: has ~1.3k rows on the box and no time index; the cap is the bound there).
MAX_ROWS = 2000
DEFAULT_PER_KIND = 5
#: Used only when the risk config cannot be read: the three chains with a bankroll.
FALLBACK_FUNDED = frozenset({"sol", "bsc", "robinhood"})
#: Chains the agent's EVM key can use if funds are moved there. Not funded today.
EVM_UNFUNDED = frozenset({"eth", "base"})
#: Same haircut the NFT scorer uses: marketplace fee + royalty, and an ask is not a bid.
NFT_SECONDARY_HAIRCUT = Decimal("0.60")
#: alpha_signals carry the source's prior precision. Below this (a bare new Snapshot space
#: with no token or network, 0.15) a signal is counted, not shown.
MIN_ALPHA_PRIOR = 0.3

_CHECKER_RE = re.compile(
    r"\b(checker|check(?:ing)? eligibility|eligibility (?:check|is live)|claim (?:is )?(?:now )?(?:live|open)"
    r"|claimable|snapshot taken)\b", re.I,
)
_REFERRAL_RE = re.compile(r"(/ref/|[?&](ref|referral|invite|code)=)", re.I)
_LISTING_DELIST_RE = re.compile(r"(delist|removal of|will remove|상장 ?폐지|거래 ?종료)", re.I)
_PERP_RE = re.compile(r"(perpetual|futures|pre-?ipo|tradfi|margined)", re.I)
_ALPHA_ONLY_RE = re.compile(r"binance alpha", re.I)
_STOPWORDS = frozenset({
    "finance", "protocol", "swap", "labs", "network", "exchange", "chain", "perps", "perp",
    "staked", "staking", "capital", "money", "markets", "market", "dex", "fun", "the", "pool",
    "vault", "vaults", "lending", "bridge", "token", "airdrop", "points", "season",
})
#: DefiLlama categories whose "tokenless TVL" belongs to an institution, custodian or chain,
#: not to users who could be farming it: exchanges, canonical bridges, chains, RWA funds,
#: curators, corporate treasuries. 209 of the box's 673 tokenless rows on 2026-10-02 (CEX
#: alone 50, averaging $1.8bn). A display rule for the owner digest, not a scoring change.
_NON_USER_CATEGORIES = frozenset({
    "CEX", "Canonical Bridge", "Chain", "RWA", "Risk Curators", "Onchain Capital Allocator",
    "Stablecoin Wrapper", "Anchor BTC", "Payments", "CeDeFi", "OTC Marketplace",
})
_TIER1_SPOT = frozenset({"binance", "upbit", "coinbase"})
_TIER2_SPOT = frozenset({"okx", "bybit", "bithumb", "kraken"})
_CHAIN_NAMES = {
    "solana": "sol", "sol": "sol", "bsc": "bsc", "binance": "bsc", "bnb": "bsc",
    "robinhood": "robinhood", "robinhood chain": "robinhood", "ethereum": "eth", "eth": "eth",
    "base": "base",
}


@dataclass
class Item:
    """One digest line: what, where, why (facts), and a link."""

    kind: str
    key: str
    title: str
    chain: str | None
    link: str | None
    why: list[str]
    score: float
    first_seen_ms: int


@dataclass
class Section:
    kind: str
    items: list[Item] = field(default_factory=list)
    considered: int = 0
    already_sent: int = 0
    dropped: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1


# ---------------------------------------------------------------- small helpers


def _obj(value: Any) -> dict[str, Any]:
    try:
        out = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _int(value: Any) -> int | None:
    try:
        return None if value is None or isinstance(value, bool) else int(value)
    except (TypeError, ValueError):
        return None


def _ago(ms: int | None, now: int) -> str:
    if ms is None:
        return "time unknown"
    h = (now - int(ms)) / 3_600_000
    return f"{h * 60:.0f}m ago" if h < 1 else f"{h:.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def _usd(value: Decimal | None) -> str:
    if value is None:
        return "?"
    return f"${value:,.0f}" if abs(value) >= 100 else f"${value:,.2f}"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> list[dict[str, Any]]:
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def funded_chains(risk: Any = None) -> frozenset[str]:
    """Chains with a bankroll in ``config/risk.yaml``, whether or not trading is enabled.

    That is where a Kaiba wallet actually holds funds, so it is what "a chain the owner
    can act on" means here. Trading being switched off on a chain does not empty its
    wallet: on 2026-10-02 the box had sol and bsc disabled with 8.76 SOL and 0.71 BNB of
    bankroll. Unreadable config falls back to sol/bsc/robinhood.
    """
    try:
        if risk is None:
            from kaiba.core.config import get_risk

            risk = get_risk()
        chains = {
            (c.value if hasattr(c, "value") else str(c))
            for c, b in (risk.chains or {}).items()
            if int(getattr(b, "bankroll_base_units", 0) or 0) > 0
        }
        return frozenset(chains) or FALLBACK_FUNDED
    except Exception:  # noqa: BLE001 - a digest must not die on a config read
        return FALLBACK_FUNDED


def _chain_note(chain: str | None, funded: frozenset[str]) -> tuple[float, str | None]:
    if chain in funded:
        return 2.0, f"on {chain}, where a Kaiba wallet is funded"
    if chain in EVM_UNFUNDED:
        return 0.5, f"on {chain}: the agent's EVM key works there but holds no funds"
    return 0.0, None


def read_cursor(conn: sqlite3.Connection) -> dict[str, int]:
    """Keys already delivered -> when. Missing or corrupt cursor is an empty one."""
    try:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (CURSOR_KEY,)).fetchone()
    except sqlite3.Error:
        return {}
    sent = _obj(row[0] if row else None).get("sent")
    return {str(k): int(v) for k, v in sent.items() if _int(v) is not None} if isinstance(sent, dict) else {}


# ---------------------------------------------------------------- per kind


def _nft(conn: sqlite3.Connection, since: int, now: int, funded: frozenset[str]) -> Section:
    sec = Section("nft")
    rows = _rows(conn, "SELECT opportunity_id, name, chain, url, evidence_json, created_ms FROM opportunities "
                       "WHERE kind='nft_mint' AND created_ms >= ? ORDER BY created_ms DESC LIMIT ?",
                 (since, MAX_ROWS))
    for row in rows:
        sec.considered += 1
        ev = _obj(row["evidence_json"])
        meta = _obj(ev.get("meta"))
        chain = row.get("chain")
        end = _int(meta.get("end_ms"))
        start = _int(meta.get("launch_ms"))
        if end is not None and end <= now:
            sec.drop("mint_ended")
            continue
        if meta.get("public_phase") is not True:
            sec.drop("no_public_phase")
            continue
        chain_score, chain_why = _chain_note(chain, funded)
        if chain_why is None:
            sec.drop("chain_not_owner_actionable")
            continue
        price = _dec(meta.get("mint_price_usd"))
        floor = _dec(meta.get("recent_floor_usd"))
        sales_1d = _int(meta.get("sales_1d"))
        gas = _dec(ev.get("gas_cost_usd")) or Decimal("0")
        if chain not in funded and not (floor is not None and price is not None and sales_1d):
            # Funding another chain is a bridge plus its gas: only a priced, traded margin
            # justifies showing it. (Live 2026-10-02: 3 of 5 shown were unfloored eth mints.)
            sec.drop("unfunded_chain_without_margin")
            continue
        why = [chain_why]
        score = chain_score
        if price is None:
            why.append("mint price not priced in USD")
        elif price == 0:
            why.append(f"free mint (gas ~{_usd(gas)})")
            score += 1.0
        else:
            why.append(f"mint {_usd(price)}")
        if floor is not None and price is not None:
            net = (floor * NFT_SECONDARY_HAIRCUT - price - gas).quantize(Decimal("0.01"))
            if net <= 0:
                # Minting at or above what the market pays back is a known loss, not a lead.
                sec.drop("no_margin_at_floor")
                continue
            why.append(f"secondary floor {_usd(floor)}, {sales_1d or 0} sales in 24h")
            if sales_1d:
                why.append(f"~{_usd(net)}/mint after 40% haircut and gas")
                score += 3.0 + min(3.0, float(net / max(price + gas, Decimal("1"))))
            else:
                why.append(f"~{_usd(net)}/mint only on an ask nobody filled today: thin market")
                score += 0.5
        elif meta.get("stats_state") not in (None, "ok"):
            why.append(f"no floor yet ({meta.get('stats_state')})")
        if start is not None and start > now:
            hours = (start - now) / 3_600_000
            why.append(f"opens in {hours:.0f}h")
            score += 1.0 if hours <= 48 else 0.0
        elif meta.get("is_minting"):
            score += 0.5
        if end is not None:
            why.append(f"closes in {(end - now) / 3_600_000:.0f}h")
        sec.items.append(Item("nft", f"nft:{row['opportunity_id']}", str(row["name"]), chain,
                              row.get("url"), why, round(score, 3), int(row["created_ms"])))
    return sec


def _radar_names(conn: sqlite3.Connection, funded: frozenset[str]) -> dict[str, dict[str, Any]]:
    """Significant first word of each radar find on a funded chain -> the find."""
    if not _table_exists(conn, "radar_finds"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for f in _rows(conn, "SELECT display_name, chain_slug, value, unit, verdict FROM radar_finds "
                         "ORDER BY reported_ms DESC LIMIT 500", ()):
        if f.get("chain_slug") not in funded:
            continue
        words = [w for w in re.findall(r"[a-z0-9]+", str(f.get("display_name") or "").lower())
                 if len(w) >= 4 and w not in _STOPWORDS]
        if words:
            out.setdefault(words[0], f)
    return out


def _airdrop(conn: sqlite3.Connection, since: int, now: int, funded: frozenset[str]) -> Section:
    sec = Section("airdrop")
    radar = _radar_names(conn, funded)
    rows = _rows(conn, "SELECT opportunity_id, kind, name, chain, url, status, evidence_json, created_ms "
                       "FROM opportunities WHERE kind IN ('airdrop','points') AND created_ms >= ? "
                       "ORDER BY created_ms DESC LIMIT ?", (since, MAX_ROWS))
    for row in rows:
        sec.considered += 1
        if row.get("status") == "refused":
            sec.drop("refused_by_planner")
            continue
        link = row.get("url")
        if not link or not str(link).startswith(("https://", "http://")):
            sec.drop("no_programme_link")
            continue
        ev = _obj(row["evidence_json"])
        meta = _obj(ev.get("meta"))
        if meta.get("category") in _NON_USER_CATEGORIES:
            sec.drop("institutional_tvl_category")
            continue
        text = f"{row['name']}\n{meta.get('excerpt') or ''}"
        chains = {str(row.get("chain") or "")} | {
            _CHAIN_NAMES.get(str(c).lower(), str(c).lower()) for c in (meta.get("chains") or [])
        }
        on_funded = sorted(c for c in chains if c in funded)
        why: list[str] = []
        score = 0.0
        tvl = _dec(meta.get("tvl_usd"))
        checker = bool(_CHECKER_RE.search(text))
        points = bool(ev.get("points_program"))
        if on_funded:
            why.append(f"on {', '.join(on_funded)}, where a Kaiba wallet is funded")
            score += 2.0
        hits = [f for w, f in radar.items() if re.search(rf"\b{re.escape(w)}\b", text.lower())]
        if hits:
            f = hits[0]
            value = _dec(f.get("value"))
            size = (f"{_usd(value)}/day fees" if f.get("unit") == "usd_per_day"
                    else f"{f.get('value')} {f.get('unit')}" if value is not None else "unpriced")
            why.append(f"radar: {f.get('display_name')} on {f.get('chain_slug')}, {size}")
            score += 2.0
        if checker or ev.get("confirmed_token"):
            why.append("claim/eligibility checker or token stated live: check wallets you already use")
            score += 2.0
        elif points:
            why.append("points programme live: accrues from activity")
            score += 1.0
        if tvl is not None and tvl > 0:
            # Speculative (p=0.15 in the EV model): TVL alone never outranks a live programme.
            why.append(f"tokenless {meta.get('category') or 'protocol'}, TVL "
                       f"{_usd(tvl / Decimal(1_000_000))}m (DefiLlama)")
            score += min(1.0, max(0.0, math.log10(float(tvl) / 1e6) / 2))
        if not (on_funded or hits or checker or ev.get("confirmed_token") or points or tvl):
            sec.drop("no_stated_fact")
            continue
        if int(ev.get("source_count") or 1) >= 2:
            why.append(f"{ev.get('source_count')} sources agree")
            score += 1.0
        if ev.get("sybil_risk") == "high":
            why.append("hard sybil/KYC filtering")
            score -= 1.0
        if _REFERRAL_RE.search(str(link)):
            # First, so a truncated Telegram line still carries it.
            why.insert(0, "REFERRAL link, use the project's own site")
        pub = _int(meta.get("publication_ms"))
        why.append(f"{'/'.join(ev.get('sources') or [str(meta.get('source') or '?')])}"
                   f"{', posted ' + _ago(pub, now) if pub else ''}")
        chain = on_funded[0] if on_funded else hits[0].get("chain_slug") if hits else row.get("chain")
        sec.items.append(Item("airdrop", f"airdrop:{row['opportunity_id']}", str(row["name"])[:80],
                              chain, str(link), why, round(score, 3), int(row["created_ms"])))
    return sec


def _listing(conn: sqlite3.Connection, since: int, now: int, funded: frozenset[str]) -> Section:
    sec = Section("listing")
    raw: list[dict[str, Any]] = []
    if _table_exists(conn, "listing_events"):
        raw += [{**r, "seen": r["detected_ms"], "id": r["listing_id"]} for r in _rows(
            conn, "SELECT listing_id, exchange, symbol, title, url, chain, token, announced_ms, detected_ms, "
                  "latency_ms, source FROM listing_events WHERE detected_ms >= ? ORDER BY detected_ms DESC "
                  "LIMIT ?", (since, MAX_ROWS))]
    if _table_exists(conn, "alpha_signals"):
        raw += [{"id": r["signal_key"], "exchange": r["source"], "symbol": r["subject"], "title": r["title"],
                 "url": r["url"], "chain": r["chain"], "token": None, "announced_ms": r["event_at_ms"],
                 "seen": r["first_seen_ms"], "latency_ms": None, "source": r["source"]}
                for r in _rows(conn, "SELECT signal_key, source, subject, title, url, chain, event_at_ms, "
                                     "first_seen_ms FROM alpha_signals WHERE kind='venue_listing' AND "
                                     "first_seen_ms >= ? ORDER BY first_seen_ms DESC LIMIT ?", (since, MAX_ROWS))]
    seen_pairs: set[tuple[str, str]] = set()
    for r in sorted(raw, key=lambda x: int(x["seen"])):
        sec.considered += 1
        title = str(r.get("title") or "")
        exchange = str(r.get("exchange") or "?").lower()
        symbol = str(r.get("symbol") or "").upper()
        pair = (exchange, symbol)
        if pair in seen_pairs:
            sec.drop("duplicate_of_another_source")
            continue
        seen_pairs.add(pair)
        if _LISTING_DELIST_RE.search(title):
            sec.drop("delisting")
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,12}", symbol):
            sec.drop("no_single_ticker")
            continue
        why = [f"{exchange}: {title[:90]}"]
        score = 3.0 if exchange in _TIER1_SPOT else 2.0 if exchange in _TIER2_SPOT else 1.0
        market = "spot"
        if _ALPHA_ONLY_RE.search(title):
            why.append("Binance Alpha, not an official listing")
            score -= 2.0
            market = "alpha"
        elif _PERP_RE.search(title):
            why.append("derivatives/perp listing, not spot")
            score -= 1.5
            market = "perp"
        chain = r.get("chain")
        if r.get("token") and chain in funded:
            why.append(f"trades on-chain on {chain}: {r['token']}")
            score += 2.0
        lat = _int(r.get("latency_ms"))
        if lat is not None:
            why.append(f"seen {lat / 60000:.0f}m after the announcement")
        why.append("listing-pop lane is off: manual only")
        # Keyed by exchange + ticker + market, not by row: the same listing relayed by a
        # second source in a later window is not news, a spot listing after a perp is.
        sec.items.append(Item("listing", f"listing:{exchange}:{symbol}:{market}", f"{symbol} on {exchange}",
                              chain, r.get("url"), why, round(score, 3), int(r["seen"])))
    return sec


def _alpha(conn: sqlite3.Connection, since: int, now: int, funded: frozenset[str]) -> Section:
    sec = Section("alpha")
    if _table_exists(conn, "alpha_signals"):
        for r in _rows(conn, "SELECT signal_key, source, kind, subject, title, url, chain, lead_ms, confidence, "
                             "first_seen_ms FROM alpha_signals WHERE kind != 'venue_listing' AND first_seen_ms >= ? "
                             "ORDER BY first_seen_ms DESC LIMIT ?", (since, MAX_ROWS)):
            sec.considered += 1
            conf = float(r.get("confidence") or 0.0)
            if conf < MIN_ALPHA_PRIOR:
                sec.drop("low_precision_prior")
                continue
            chain_score, chain_why = _chain_note(r.get("chain"), funded)
            why = [f"{r['source']} {r['kind']} (source precision prior {conf:.2f})"]
            if chain_why:
                why.append(chain_why)
            lead = _int(r.get("lead_ms"))
            if lead:
                why.append(f"typical lead {lead / 86_400_000:.1f}d")
            sec.items.append(Item("alpha", f"alpha:{r['signal_key']}", str(r.get("title") or r["subject"])[:90],
                                  r.get("chain"), r.get("url"), why, round(conf * 3 + chain_score, 3),
                                  int(r["first_seen_ms"])))
    if _table_exists(conn, "radar_finds"):
        for r in _rows(conn, "SELECT find_id, radar_key, display_name, chain_slug, kind, value, unit, headroom, "
                             "tractability, actionable, verdict, reported_ms, payload_json FROM radar_finds "
                             "WHERE reported_ms >= ? ORDER BY reported_ms DESC LIMIT ?", (since, MAX_ROWS)):
            sec.considered += 1
            if not r.get("actionable"):
                sec.drop("radar_not_actionable")
                continue
            chain_score, chain_why = _chain_note(r.get("chain_slug"), funded)
            headroom = float(_dec(r.get("headroom")) or 0)
            value = _dec(r.get("value"))
            size = (f"{_usd(value)}/day fees" if r.get("unit") == "usd_per_day" and value is not None
                    else f"{r.get('value')} {r.get('unit')}" if value is not None else "unpriced")
            why = [f"new {r.get('kind')} on DefiLlama, {size}, {headroom:.1f}x the radar floor"]
            if chain_why:
                why.append(chain_why)
            slug = _obj(r.get("payload_json")).get("slug")
            link = f"https://defillama.com/protocol/{slug}" if isinstance(slug, str) and slug else None
            sec.items.append(Item("alpha", f"radar:{r['radar_key']}", f"{r.get('display_name')}",
                                  r.get("chain_slug"), link, why,
                                  round(1.0 + min(2.0, headroom / 2) + chain_score, 3), int(r["reported_ms"])))
    return sec


BUILDERS = {"nft": _nft, "airdrop": _airdrop, "listing": _listing, "alpha": _alpha}


def build(conn: sqlite3.Connection, *, since_hours: float = 24.0, per_kind: int = DEFAULT_PER_KIND,
          now: int | None = None, risk: Any = None, sent: dict[str, int] | None = None) -> dict[str, Any]:
    """Every section as data. Read-only: nothing here writes, including the cursor.

    A kind whose table is missing or unreadable carries ``{"error": ...}`` and the other
    kinds still build. Within a kind: drop what fails an inclusion rule (counted by
    reason), drop what the cursor says was sent, rank by score then recency, keep
    ``per_kind``.
    """
    until = int(now) if now is not None else now_ms()
    since = until - int(since_hours * 3_600_000)
    funded = funded_chains(risk)
    sent = read_cursor(conn) if sent is None else sent
    sections: dict[str, Any] = {}
    for kind in KINDS:
        try:
            sec = BUILDERS[kind](conn, since, until, funded)
        except sqlite3.Error as exc:
            sections[kind] = {"error": f"cannot measure: {type(exc).__name__}: {exc}"[:200]}
            continue
        fresh = [i for i in sec.items if i.key not in sent]
        sec.already_sent = len(sec.items) - len(fresh)
        fresh.sort(key=lambda i: (-i.score, -i.first_seen_ms))
        qualifying = len(fresh)
        sec.items = fresh[:max(0, int(per_kind))]
        sections[kind] = {"items": [asdict(i) for i in sec.items], "qualifying": qualifying,
                          "considered": sec.considered, "already_sent": sec.already_sent,
                          "dropped": dict(sorted(sec.dropped.items(), key=lambda kv: -kv[1]))}
    return {"generated_ms": until, "since_ms": since, "since_hours": since_hours,
            "funded_chains": sorted(funded), "sections": sections,
            "note": "Research leads for the owner. Not verified eligibility, not advice, and Kaiba "
                    "does not mint, claim, sign or trade any of it."}


# ---------------------------------------------------------------- cursor write


def mark_sent(conn: sqlite3.Connection, digest: dict[str, Any], *, now: int | None = None) -> int:
    """Record every item in ``digest`` as delivered. The only write in this module."""
    ts = int(now) if now is not None else now_ms()
    sent = {k: v for k, v in read_cursor(conn).items() if ts - v <= CURSOR_KEEP_MS}
    keys = [i["key"] for s in digest["sections"].values() if isinstance(s, dict) for i in s.get("items", [])]
    for k in keys:
        sent[k] = ts
    if len(sent) > CURSOR_MAX_KEYS:
        sent = dict(sorted(sent.items(), key=lambda kv: kv[1])[-CURSOR_MAX_KEYS:])
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value, updated_ms=excluded.updated_ms",
        (CURSOR_KEY, json.dumps({"v": 1, "sent": sent}, separators=(",", ":")), ts),
    )
    conn.commit()
    return len(keys)


# ---------------------------------------------------------------- rendering

_LABEL = {"nft": "NFT MINTS", "airdrop": "AIRDROPS/POINTS", "listing": "LISTINGS", "alpha": "EARLY ALPHA"}


def render_lines(digest: dict[str, Any], *, per_kind: int | None = None, width: int = 200) -> list[str]:
    """Plain text lines. Empty kinds collapse into one summary line.

    The link goes on its own line: it is the longest part, and inline it truncated the
    facts that are the point of the line (measured on the box report, 2026-10-02).
    """
    lines: list[str] = []
    empty: list[str] = []
    for kind in KINDS:
        sec = digest["sections"].get(kind) or {}
        if "error" in sec:
            lines.append(f"  {_LABEL[kind]}: {sec['error']}")
            continue
        items = sec.get("items", [])[: per_kind if per_kind is not None else None]
        if not items:
            empty.append(f"{kind} 0/{sec.get('considered', 0)}")
            continue
        extra = sec.get("qualifying", len(items)) - len(items)
        lines.append(f"  {_LABEL[kind]} ({sec.get('qualifying', len(items))} new"
                     f"{f', top {len(items)}' if extra > 0 else ''}):")
        for n, it in enumerate(items, 1):
            chain = f" [{it['chain']}]" if it.get("chain") else ""
            text = f"   {n}. {it['title']}{chain}: {'; '.join(it['why'])}"
            lines.append(text if len(text) <= width else text[: max(20, width - 3)] + "...")
            if it.get("link"):
                lines.append(f"      {it['link']}")
    if empty:
        lines.append(f"  nothing new: {', '.join(empty)} (shown/considered)")
    return lines


def render(digest: dict[str, Any], *, per_kind: int | None = None) -> str:
    hours = digest["since_hours"]
    head = (f"HUNTERS last {hours:g}h, funded chains {', '.join(digest['funded_chains'])} "
            "(research leads; Kaiba mints/claims/signs nothing)")
    return "\n".join([head, *render_lines(digest, per_kind=per_kind)])


# ---------------------------------------------------------------- CLI


def _open(path: Path, *, writable: bool) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"no database at {resolved}")
    uri = resolved.as_uri() + ("" if writable else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA busy_timeout=10000")
    if not writable:
        conn.execute("PRAGMA query_only=1")
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.hunters.digest",
                                     description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=None, help="database path (default: settings db_path)")
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--per-kind", type=int, default=DEFAULT_PER_KIND)
    parser.add_argument("--json", action="store_true", help="print the digest as JSON")
    parser.add_argument("--mark-sent", action="store_true",
                        help="record the printed items in the kv cursor (opens the DB writable)")
    args = parser.parse_args(argv)

    started = time.monotonic()
    if args.db is None:
        from kaiba.core.config import get_settings

        path = get_settings().db_path
    else:
        path = args.db
    try:
        conn = _open(path, writable=args.mark_sent)
    except (OSError, sqlite3.Error) as exc:
        print(f"Kaiba hunter digest: cannot open the database ({type(exc).__name__}: {exc})")
        return 1
    try:
        digest = build(conn, since_hours=args.since_hours, per_kind=args.per_kind)
        marked = mark_sent(conn, digest) if args.mark_sent else None
    finally:
        conn.close()
    elapsed = time.monotonic() - started
    if args.json:
        print(json.dumps({**digest, "elapsed_s": round(elapsed, 2), "marked_sent": marked}, default=str, indent=1))
    else:
        print(render(digest))
        tail = f", marked {marked} sent" if marked is not None else ", read-only"
        print(f"({elapsed:.1f}s{tail})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
