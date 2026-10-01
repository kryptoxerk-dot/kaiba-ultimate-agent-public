"""Telegram call rooms, generalised from the hoodsniper listener.

``reference/hoodsniper/sniper/listener.py`` reads one EVM chain, one regex, and buys on
the call. This module keeps the two parts of that which were actually good — a Telethon
userbot reading N channels/topics, and per-caller reputation — and drops the part that
lost money: the call itself is never a buy trigger here. It is an ``ALPHA_CALL`` event.

The research is blunt about why (``docs/research/05-upgrades-beyond-the-ask.md``): 80% of
promoted coins are down 70% within a week, so **a KOL mention is an exit trigger by
default**. A caller only earns ``follow`` mode by measured positive expectancy over at
least ten resolved calls; the default posture is ``observe``, and ``fade`` is a perfectly
good outcome to discover.

Address extraction is deliberately paranoid. A call room is adversarial text: it contains
program ids, other people's wallets, referral links and English words that happen to be
base58-clean. We accept a Solana candidate only if it survives ``looks_solana``, a
denylist of well-known program/system accounts, a padding heuristic, and a base58 decode
to exactly 32 bytes.

``telethon`` is an optional dependency (``pip install -e .[telegram]``). It is imported
lazily so this module — and everything that imports it, including the runner — works
without it; :func:`run` logs, emits a SYSTEM event and returns.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import base58
from pydantic import BaseModel

from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import fetch_all, get_conn
from kaiba.core.events import emit
from kaiba.core.schemas import Chain, EventKind, Lane, looks_solana, normalize_address, now_ms

log = logging.getLogger(__name__)

PLATFORM = "telegram"

# --------------------------------------------------------------------------------------
# address extraction
# --------------------------------------------------------------------------------------

_EVM_RE = re.compile(r"(?<![0-9a-zA-Z])0x[a-fA-F0-9]{40}(?![0-9a-zA-Z])")
_B58_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
#: Program ids are padded with runs of '1' (base58 zero). No real mint has eight in a row.
_PADDED_RE = re.compile(r"1{8,}")

#: Accounts that appear constantly in call rooms and are never the token being called.
SOLANA_DENYLIST: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",              # System Program
        "So11111111111111111111111111111111111111112",   # wrapped SOL
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",    # SPL Token
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",    # Token-2022
        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",   # Associated Token Account
        "ComputeBudget111111111111111111111111111111",
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",    # Metaplex token metadata
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",    # pump.fun
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",    # PumpSwap
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",   # Raydium AMM v4
        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",   # Raydium CPMM
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",   # Raydium CLMM
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j",    # Raydium authority
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",    # Jupiter v6
        "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",    # Jupiter v4
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",    # Orca Whirlpool
        "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",   # Orca v2
        "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",    # OpenBook
        "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb",    # OpenBook v2
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",    # Meteora DLMM
        "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",    # Meteora DBC
        "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",   # pump.fun fee recipient
    }
)


def _valid_solana(candidate: str) -> bool:
    """A base58 run is a mint only if it decodes to a 32-byte pubkey and is not a program."""
    if not looks_solana(candidate):
        return False
    if candidate in SOLANA_DENYLIST or _PADDED_RE.search(candidate):
        return False
    if len(set(candidate)) <= 4:  # no entropy: padding, repeats, obvious junk
        return False
    try:
        return len(base58.b58decode(candidate)) == 32
    except ValueError:
        return False


def extract_addresses(text: str, evm_chain: Chain = Chain.ETH) -> list[tuple[Chain, str]]:
    """Every contract address in a message, in order of appearance, deduplicated.

    EVM hits are lowercased and tagged with ``evm_chain`` (the per-channel default, since
    ``0x…`` says nothing about which EVM chain it lives on). Solana hits keep their case.
    """
    if not text:
        return []
    found: dict[tuple[Chain, str], None] = {}
    for match in _EVM_RE.finditer(text):
        addr = normalize_address(match.group(0), evm_chain)
        found[(evm_chain, addr)] = None
    for match in _B58_RE.finditer(text):
        candidate = match.group(0)
        if _valid_solana(candidate):
            found[(Chain.SOL, candidate)] = None
    return list(found)


# --------------------------------------------------------------------------------------
# channel specs
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSpec:
    """One entry of ``settings.call_channels``.

    Grammar: ``<chat>[/<topic>][|<evm chain>]`` where ``<chat>`` is ``@name``, a numeric
    chat id, or a ``t.me/c/<chat>/<topic>`` link. The optional chain suffix is the default
    used for bare ``0x…`` addresses in that room (hoodsniper's rooms are Robinhood Chain).
    """

    raw: str
    username: str | None = None
    chat_id: int | None = None
    topic_id: int | None = None
    evm_chain: Chain = Chain.ETH

    @property
    def label(self) -> str:
        base = self.username or (str(self.chat_id) if self.chat_id is not None else self.raw)
        return f"{base}/{self.topic_id}" if self.topic_id else base


def parse_channel_spec(spec: str) -> ChannelSpec | None:
    raw = (spec or "").strip()
    if not raw:
        return None
    body, _, chain_part = raw.partition("|")
    evm_chain = Chain.ETH
    if chain_part.strip():
        try:
            evm_chain = Chain(chain_part.strip().lower())
        except ValueError:
            log.warning("telegram: unknown chain %r in channel spec %r", chain_part, raw)
    body = body.strip()
    for prefix in ("https://", "http://", "t.me/", "telegram.me/", "//"):
        if body.startswith(prefix):
            body = body[len(prefix):]
    if body.startswith("t.me/"):
        body = body[len("t.me/"):]
    if body.startswith("c/"):
        body = body[2:]
    parts = [p for p in body.split("/") if p]
    if not parts:
        return None
    head, topic = parts[0], None
    if len(parts) > 1:
        try:
            topic = int(parts[1])
        except ValueError:
            topic = None
    head = head.lstrip("#")
    if head.startswith("@"):
        return ChannelSpec(raw=raw, username=head[1:].lower(), topic_id=topic, evm_chain=evm_chain)
    try:
        return ChannelSpec(raw=raw, chat_id=int(head), topic_id=topic, evm_chain=evm_chain)
    except ValueError:
        return ChannelSpec(raw=raw, username=head.lower(), topic_id=topic, evm_chain=evm_chain)


def load_channel_specs(entries: Iterable[str] | None = None) -> list[ChannelSpec]:
    raw = list(entries) if entries is not None else get_settings().call_channels
    return [s for s in (parse_channel_spec(e) for e in raw) if s is not None]


def _chat_variants(chat_id: int | None) -> set[int]:
    """``-1001752354955`` / ``1752354955`` / ``1001752354955`` all mean the same supergroup."""
    if chat_id is None:
        return set()
    out = {chat_id, abs(chat_id)}
    text = str(abs(chat_id))
    if text.startswith("100") and len(text) > 3:
        out.add(int(text[3:]))
    out.add(int(f"-100{text}") if not text.startswith("100") else -abs(chat_id))
    return out


def match_channel(
    specs: Sequence[ChannelSpec],
    *,
    chat_id: int | None = None,
    username: str | None = None,
    topic_id: int | None = None,
) -> ChannelSpec | None:
    """The spec this message belongs to, or ``None``. Topic-less specs match any topic."""
    ids = _chat_variants(chat_id)
    uname = (username or "").lstrip("@").lower() or None
    for spec in specs:
        if spec.chat_id is not None and not (_chat_variants(spec.chat_id) & ids):
            continue
        if spec.username is not None and spec.username != uname:
            continue
        if spec.topic_id is not None and spec.topic_id != (topic_id or 1):
            continue
        return spec
    return None


def topic_id(message: Any) -> int:
    """Forum topic of a Telethon message (1 = the General topic). From hoodsniper."""
    reply = getattr(message, "reply_to", None)
    if reply is not None and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None) or 1
    return 1


# --------------------------------------------------------------------------------------
# recording calls
# --------------------------------------------------------------------------------------


def record_call(
    conn: Any,
    *,
    caller_id: str | int,
    chain: Chain,
    token: str,
    ts_ms: int | None = None,
    display_name: str | None = None,
    channel: str | None = None,
    platform: str = PLATFORM,
    message_id: int | None = None,
    text: str | None = None,
) -> bool:
    """Persist one call and emit ``ALPHA_CALL``. ``False`` if we had already seen it.

    The caller row is upserted with ``mode='observe'`` on first sight: a new caller is
    never trusted and never ignored, it is watched until :func:`score_callers` has enough
    resolved calls to say something.
    """
    ts = ts_ms or now_ms()
    cid = str(caller_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO caller_calls "
        "(platform, caller_id, chain, token, ts_ms, channel, outcome) VALUES (?,?,?,?,?,?,'pending')",
        (platform, cid, chain.value, token, ts, channel),
    )
    if not cur.rowcount:
        return False
    conn.execute(
        "INSERT INTO callers (platform, caller_id, display_name, channel, calls, last_call_ms, mode) "
        "VALUES (?,?,?,?,1,?,'observe') "
        "ON CONFLICT(platform, caller_id) DO UPDATE SET "
        "  display_name=COALESCE(excluded.display_name, callers.display_name), "
        "  channel=COALESCE(excluded.channel, callers.channel), "
        "  calls=callers.calls+1, "
        "  last_call_ms=MAX(COALESCE(callers.last_call_ms, 0), excluded.last_call_ms)",
        (platform, cid, display_name, channel, ts),
    )
    row = conn.execute(
        "SELECT mode, calls FROM callers WHERE platform=? AND caller_id=?", (platform, cid)
    ).fetchone()
    emit(
        EventKind.ALPHA_CALL,
        {
            "platform": platform,
            "caller_id": cid,
            "display_name": display_name,
            "channel": channel,
            "token": token,
            "chain": chain.value,
            "called_ms": ts,
            "caller_mode": (row["mode"] if row else "observe"),
            "caller_calls": (row["calls"] if row else 1),
            "message_id": message_id,
            "excerpt": (text or "")[:280] or None,
            # Default posture, per PLAN §13.18: a mention is an exit trigger, not an entry.
            "lane_hint": Lane.KOL_FADE.value,
        },
        chain=chain,
        subject=token,
        dedupe_key=f"{EventKind.ALPHA_CALL.value}:{platform}:{cid}:{chain.value}:{token}:{ts}",
        conn=conn,
    )
    return True


def record_message(
    conn: Any,
    *,
    text: str,
    caller_id: str | int,
    spec: ChannelSpec,
    display_name: str | None = None,
    ts_ms: int | None = None,
    message_id: int | None = None,
) -> list[tuple[Chain, str]]:
    """Extract every CA in one message and record each as a call. Returns what was new."""
    new: list[tuple[Chain, str]] = []
    for chain, address in extract_addresses(text, spec.evm_chain):
        if record_call(
            conn,
            caller_id=caller_id,
            chain=chain,
            token=address,
            ts_ms=ts_ms,
            display_name=display_name,
            channel=spec.label,
            message_id=message_id,
            text=text,
        ):
            new.append((chain, address))
    return new


# --------------------------------------------------------------------------------------
# caller reputation
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerThresholds:
    """Follow/fade thresholds. Defaults come from ``config/risk.yaml`` lane ``kol-fade``."""

    min_resolved: int = 10
    follow_expectancy: float = 0.0
    fade_expectancy: float = -0.20
    min_win_rate: float = 0.20
    win_peak_x: float = 2.0        # "win" = reached the TP1 rung in config/risk.yaml
    rug_peak_x: float = 0.20
    flat_band: float = 0.05
    spam_calls: int = 30
    horizon_s: int = 6 * 3600      # how long after a call we look for the peak
    price_window_s: int = 900      # how far from the call we accept a reference price


def load_thresholds() -> CallerThresholds:
    params = dict(get_risk().lane(Lane.KOL_FADE).params or {})
    base = CallerThresholds()
    return CallerThresholds(
        min_resolved=int(params.get("min_caller_trades", base.min_resolved)),
        follow_expectancy=float(params.get("min_caller_expectancy", base.follow_expectancy)),
        fade_expectancy=float(params.get("fade_caller_expectancy", base.fade_expectancy)),
        min_win_rate=float(params.get("min_caller_win_rate", base.min_win_rate)),
        win_peak_x=float(params.get("caller_win_peak_x", base.win_peak_x)),
        rug_peak_x=float(params.get("caller_rug_peak_x", base.rug_peak_x)),
        flat_band=float(params.get("caller_flat_band", base.flat_band)),
        spam_calls=int(params.get("caller_spam_calls", base.spam_calls)),
        horizon_s=int(params.get("caller_horizon_s", base.horizon_s)),
        price_window_s=int(params.get("caller_price_window_s", base.price_window_s)),
    )


class CallerStats(BaseModel):
    """What a caller's calls actually did. ``expectancy`` is in multiples, not percent."""

    platform: str = PLATFORM
    caller_id: str
    display_name: str | None = None
    channel: str | None = None
    calls: int = 0
    resolved: int = 0
    pending: int = 0
    wins: int = 0
    losses: int = 0
    rugs: int = 0
    avg_peak_x: float | None = None
    expectancy: float | None = None
    win_rate: float | None = None
    last_call_ms: int | None = None
    mode: str = "observe"


def classify_caller(stats: CallerStats, thresholds: CallerThresholds | None = None) -> str:
    """follow / fade / observe / ignore.

    Deliberately conservative: ``observe`` is the default and the only way out of it is
    measured evidence over ``min_resolved`` calls. ``ignore`` is reserved for high-volume
    callers whose calls carry no information in either direction — fading noise costs fees.
    """
    t = thresholds or load_thresholds()
    if stats.resolved < t.min_resolved or stats.expectancy is None:
        return "observe"
    if stats.expectancy > t.follow_expectancy and (stats.win_rate or 0.0) >= t.min_win_rate:
        return "follow"
    if stats.expectancy <= t.fade_expectancy:
        return "fade"
    if stats.calls >= t.spam_calls and abs(stats.expectancy) <= t.flat_band:
        return "ignore"
    return "observe"


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None


def _price_at(conn: Any, chain: Chain, token: str, ts_ms: int, window_ms: int) -> Decimal | None:
    """Reference price at the moment of the call: last print before it, else first after."""
    for sql, params in (
        (
            "SELECT price_usd FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
            "AND ts_ms <= ? AND ts_ms >= ? ORDER BY ts_ms DESC LIMIT 1",
            (chain.value, token, ts_ms, ts_ms - window_ms),
        ),
        (
            "SELECT price_usd FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
            "AND ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms ASC LIMIT 1",
            (chain.value, token, ts_ms, ts_ms + window_ms),
        ),
    ):
        row = conn.execute(sql, params).fetchone()
        price = _dec(row["price_usd"]) if row else None
        if price is not None:
            return price
    return None


def _peak_after(conn: Any, chain: Chain, token: str, ts_ms: int, horizon_ms: int) -> Decimal | None:
    rows = conn.execute(
        "SELECT price_usd FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
        "AND ts_ms >= ? AND ts_ms <= ?",
        (chain.value, token, ts_ms, ts_ms + horizon_ms),
    ).fetchall()
    prices = [p for p in (_dec(r["price_usd"]) for r in rows) if p is not None]
    return max(prices) if prices else None


def resolve_call(conn: Any, call: dict[str, Any], t: CallerThresholds) -> dict[str, Any]:
    """Attach ``peak_x`` and an outcome to one call, if the price data exists.

    No price data means ``pending``. It never means zero — an unresolved call must not be
    counted as a loss, or every caller looks like a fade on day one.
    """
    if call.get("outcome") in {"win", "loss", "rug"} and call.get("peak_x") is not None:
        return call
    chain = Chain(call["chain"])
    entry = _dec(call.get("price_at_call_usd")) or _price_at(
        conn, chain, call["token"], call["ts_ms"], t.price_window_s * 1000
    )
    if entry is None:
        return call
    peak = _peak_after(conn, chain, call["token"], call["ts_ms"], t.horizon_s * 1000)
    if peak is None:
        return call
    peak_x = float(peak / entry)
    outcome = "win" if peak_x >= t.win_peak_x else ("rug" if peak_x <= t.rug_peak_x else "loss")
    conn.execute(
        "UPDATE caller_calls SET peak_x=?, outcome=?, price_at_call_usd=? WHERE id=?",
        (peak_x, outcome, str(entry), call["id"]),
    )
    return {**call, "peak_x": peak_x, "outcome": outcome, "price_at_call_usd": str(entry)}


def score_callers(
    conn: Any = None,
    lookback_days: int = 30,
    *,
    thresholds: CallerThresholds | None = None,
) -> list[CallerStats]:
    """Resolve outcomes, recompute expectancy, and set each caller's mode.

    ``expectancy`` is ``mean(peak multiple) - 1`` over resolved calls: what a follower who
    sold the peak would have made, which is the optimistic bound. It is the right number
    for *fade* decisions (if even the peak is below the call price, the caller is an exit
    signal) and an upper bound for *follow* decisions, which Phase 3 replaces with realised
    journal PnL from the shadow lanes.
    """
    c = conn or get_conn()
    t = thresholds or load_thresholds()
    cutoff = now_ms() - lookback_days * 86_400_000
    calls = fetch_all(
        c,
        "SELECT * FROM caller_calls WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (cutoff,),
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for call in calls:
        resolved = resolve_call(c, call, t)
        grouped.setdefault((resolved["platform"], resolved["caller_id"]), []).append(resolved)

    out: list[CallerStats] = []
    for (platform, caller_id), rows in grouped.items():
        meta = c.execute(
            "SELECT display_name, channel, mode FROM callers WHERE platform=? AND caller_id=?",
            (platform, caller_id),
        ).fetchone()
        peaks = [float(r["peak_x"]) for r in rows if r.get("peak_x") is not None]
        wins = sum(1 for r in rows if r.get("outcome") == "win")
        rugs = sum(1 for r in rows if r.get("outcome") == "rug")
        losses = sum(1 for r in rows if r.get("outcome") == "loss")
        resolved_n = wins + losses + rugs
        stats = CallerStats(
            platform=platform,
            caller_id=caller_id,
            display_name=meta["display_name"] if meta else None,
            channel=meta["channel"] if meta else (rows[-1].get("channel")),
            calls=len(rows),
            resolved=resolved_n,
            pending=len(rows) - resolved_n,
            wins=wins,
            losses=losses,
            rugs=rugs,
            avg_peak_x=round(sum(peaks) / len(peaks), 4) if peaks else None,
            expectancy=round(sum(peaks) / len(peaks) - 1.0, 4) if peaks else None,
            win_rate=round(wins / resolved_n, 4) if resolved_n else None,
            last_call_ms=max(r["ts_ms"] for r in rows),
        )
        stats.mode = classify_caller(stats, t)
        previous = meta["mode"] if meta else "observe"
        c.execute(
            "INSERT INTO callers (platform, caller_id, display_name, channel, calls, wins, losses, "
            " expectancy, avg_peak_x, last_call_ms, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(platform, caller_id) DO UPDATE SET "
            "  wins=excluded.wins, losses=excluded.losses, expectancy=excluded.expectancy, "
            "  avg_peak_x=excluded.avg_peak_x, mode=excluded.mode, "
            "  last_call_ms=MAX(COALESCE(callers.last_call_ms,0), excluded.last_call_ms)",
            (
                platform, caller_id, stats.display_name, stats.channel, stats.calls, wins,
                losses + rugs, stats.expectancy, stats.avg_peak_x, stats.last_call_ms, stats.mode,
            ),
        )
        if previous != stats.mode:
            emit(
                EventKind.PARAM_CHANGE,
                {
                    "component": "caller",
                    "platform": platform,
                    "caller_id": caller_id,
                    "display_name": stats.display_name,
                    "from": previous,
                    "to": stats.mode,
                    "expectancy": stats.expectancy,
                    "resolved": stats.resolved,
                },
                subject=caller_id,
                conn=c,
            )
        out.append(stats)
    out.sort(key=lambda s: (s.expectancy if s.expectancy is not None else -99), reverse=True)
    return out


# --------------------------------------------------------------------------------------
# the userbot
# --------------------------------------------------------------------------------------


class TelethonMissing(RuntimeError):
    """``telethon`` is an optional extra: ``pip install -e .[telegram]``."""


def _telethon() -> Any:
    try:
        import telethon  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise TelethonMissing("telethon is not installed (pip install -e .[telegram])") from exc
    return telethon


def telethon_available() -> bool:
    try:
        _telethon()
    except TelethonMissing:
        return False
    return True


def session_path() -> Path:
    """Session lives outside the repo (``~/.config/kaiba/`` by default). It is a credential."""
    return Path(get_settings().tg_session_path).expanduser()


def _client(api_id: str, api_hash: str) -> Any:
    telethon = _telethon()
    path = session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return telethon.TelegramClient(str(path), int(api_id), api_hash)


async def login(
    *, api_id: str | None = None, api_hash: str | None = None, show_url: bool = False
) -> Path:
    """QR login for the userbot. Stores the session at ``settings.tg_session_path``.

    Nothing secret is printed: not the api hash, not the session string, not the login
    token. The QR is rendered in the terminal when ``qrcode`` is installed; the raw
    ``tg://login`` URL is a bearer credential and is only shown on explicit ``show_url``.
    """
    settings = get_settings()
    api_id = api_id or settings.tg_api_id
    api_hash = api_hash or settings.tg_api_hash
    if not api_id or not api_hash:
        raise RuntimeError("TG_API_ID / TG_API_HASH are not set")
    client = _client(api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            log.info("telegram: already authorised; session at %s", session_path())
            return session_path()
        qr = await client.qr_login()
        while True:
            _render_qr(qr.url, show_url=show_url)
            try:
                await qr.wait(timeout=60)
                break
            except TimeoutError:
                await qr.recreate()
        log.info("telegram: authorised; session stored at %s", session_path())
        return session_path()
    finally:
        await client.disconnect()


def _render_qr(url: str, *, show_url: bool) -> None:
    try:
        import qrcode  # noqa: PLC0415 - optional, only used by the interactive login
    except ImportError:
        qrcode = None
    if qrcode is not None:
        code = qrcode.QRCode(border=1)
        code.add_data(url)
        code.make(fit=True)
        code.print_ascii(invert=True)
        return
    if show_url:
        log.warning("scan this single-use login link in Telegram (do not share it): %s", url)
    else:
        log.warning(
            "install 'qrcode' to render the login QR, or re-run login(show_url=True) "
            "to print the single-use link"
        )


async def run(
    stop: asyncio.Event | None = None,
    *,
    channels: Iterable[str] | None = None,
    conn: Any = None,
    client: Any = None,
) -> None:
    """Watch the configured call rooms until ``stop``. A no-op when telethon is absent."""
    stop = stop or asyncio.Event()
    c = conn or get_conn()
    specs = load_channel_specs(channels)
    if not specs:
        log.warning("telegram: no call channels configured (TG_CALL_CHANNELS); listener idle")
        emit(
            EventKind.SYSTEM,
            {"component": "ingest.telegram", "status": "disabled", "reason": "no channels"},
            level="warn",
            conn=c,
        )
        return
    settings = get_settings()
    if client is None:
        if not telethon_available():
            log.warning("telegram: telethon not installed; listener skipped")
            emit(
                EventKind.SYSTEM,
                {"component": "ingest.telegram", "status": "skipped", "reason": "telethon missing"},
                level="warn",
                conn=c,
            )
            return
        if not settings.tg_api_id or not settings.tg_api_hash:
            log.warning("telegram: TG_API_ID/TG_API_HASH missing; listener skipped")
            emit(
                EventKind.SYSTEM,
                {"component": "ingest.telegram", "status": "skipped", "reason": "no api credentials"},
                level="warn",
                conn=c,
            )
            return
        client = _client(settings.tg_api_id, settings.tg_api_hash)

    telethon = _telethon()

    async def _on_message(event: Any) -> None:
        try:
            message = event.message
            chat = await event.get_chat()
            spec = match_channel(
                specs,
                chat_id=getattr(event, "chat_id", None),
                username=getattr(chat, "username", None),
                topic_id=topic_id(message),
            )
            if spec is None:
                return
            sender_name = None
            try:
                sender = await event.get_sender()
                if sender is not None:
                    sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", None)
            except Exception:  # noqa: BLE001 - a deleted account must not stop ingestion
                sender_name = None
            new = await asyncio.to_thread(
                record_message,
                c,
                text=event.raw_text or "",
                caller_id=getattr(event, "sender_id", None) or "unknown",
                spec=spec,
                display_name=sender_name,
                ts_ms=int(message.date.timestamp() * 1000) if getattr(message, "date", None) else None,
                message_id=getattr(message, "id", None),
            )
            if new:
                log.info("telegram: %d call(s) from %s in %s", len(new), sender_name, spec.label)
        except Exception as exc:  # noqa: BLE001 - one bad message is not an outage
            log.warning("telegram: handler error (%s: %s)", type(exc).__name__, exc)

    client.add_event_handler(_on_message, telethon.events.NewMessage())
    await client.connect()
    if not await client.is_user_authorized():
        log.error("telegram: session is not authorised; run kaiba.ingest.telegram_calls.login()")
        emit(
            EventKind.SYSTEM,
            {"component": "ingest.telegram", "status": "unauthorised"},
            level="error",
            conn=c,
        )
        await client.disconnect()
        return
    emit(
        EventKind.SYSTEM,
        {"component": "ingest.telegram", "status": "running", "channels": [s.label for s in specs]},
        conn=c,
    )
    log.info("telegram: watching %d room(s)", len(specs))
    try:
        await stop.wait()
    finally:
        await client.disconnect()
        log.info("telegram: stopped")
