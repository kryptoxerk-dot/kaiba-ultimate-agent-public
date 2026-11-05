"""Tell the operator, in Telegram, every time a trade opens or closes.

DESIGN: a POLLER over the order log, not a hook in the execution path.

The obvious implementation is a call inside ``apply_fill`` or the ``ORDER_FILLED`` event
emitter. This deliberately is not that. A notifier that runs inside the trading path can
fail in three ways that all cost money -- it can raise and unwind a fill, it can block the
protection tick on a network call to Telegram, and it can be slow enough to push a tick
past its budget, which on this box halts entries on every chain. A poller reading
``orders`` after the fact can do none of those: the worst it can do is be late.

The cursor is durable (``notify_cursor``), so a restart resends nothing and skips nothing.
Delivery is best-effort and one message at a time: a send that fails is retried on the next
pass, and a message that can never be delivered is dropped after
:data:`MAX_ATTEMPTS` rather than blocking every later trade behind it.

What the operator asked for, verbatim: "which token they trade how much they buy and their
reasoning". So every message carries the token, the size in native units AND in USD where
we can price it, and the thesis the lane actually recorded -- never a reconstruction.

The same pass also pages journal alerts (``pending_alerts``) and ingest feeds that went
down or silent (``pending_feed_alerts``, added 2026-10-21 after four feeds were dark for
twelve days with no page).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, tx
from kaiba.core.redact import redact_text
from kaiba.core.schemas import NATIVE_DECIMALS, Chain

log = logging.getLogger(__name__)

#: Where the Hermes gateway keeps the bot credentials. Read at send time and never logged.
TELEGRAM_ENV = Path("/etc/kaiba-hermes/telegram.env")

#: Give up on a single message after this many failed passes. A message that cannot be
#: delivered must not wedge the queue behind it.
MAX_ATTEMPTS = 5

#: Seconds between passes. Fast enough to feel live, slow enough to batch a burst.
POLL_INTERVAL_S = 10

#: Never notify about anything older than this on a cold start, so a first run after a
#: long outage does not dump hours of history into the chat.
COLD_START_LOOKBACK_MS = 15 * 60 * 1000

_NATIVE_SYMBOL: dict[Chain, str] = {
    Chain.SOL: "SOL",
    Chain.BSC: "BNB",
    Chain.ROBINHOOD: "ETH",
    Chain.BASE: "ETH",
    Chain.ETH: "ETH",
}


# ---------------------------------------------------------------------------- credentials


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def credentials() -> tuple[str | None, str | None]:
    """``(bot_token, chat_id)``. Environment first, then the gateway's env file.

    Returns ``(None, None)`` rather than raising when unconfigured: a box without a bot
    must still trade, it just does so quietly.
    """
    token = os.environ.get("KAIBA_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("KAIBA_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_HOME_CHANNEL")
    if token and chat:
        return token, chat
    body = _read_env_file(TELEGRAM_ENV)
    return token or body.get("TELEGRAM_BOT_TOKEN"), chat or body.get("TELEGRAM_HOME_CHANNEL")


def send(text: str, *, token: str, chat_id: str, timeout_s: float = 15.0) -> bool:
    """POST one message. ``True`` on delivery. Never raises, never logs the token."""
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        request = urllib.request.Request(url, data=payload)  # noqa: S310 - fixed https host
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
        return bool(body.get("ok"))
    except Exception as exc:  # noqa: BLE001 - a dead chat must never stop the agent
        log.warning("trade notification not delivered: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------- rendering


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _native(chain: Chain, base_units: Any) -> Decimal | None:
    amount = _dec(base_units)
    if amount is None:
        return None
    return amount / (Decimal(10) ** NATIVE_DECIMALS.get(chain, 18))


def _fmt(value: Decimal | None, places: int = 4) -> str:
    if value is None:
        return "?"
    quant = Decimal(1).scaleb(-places)
    try:
        return f"{value.quantize(quant):f}".rstrip("0").rstrip(".") or "0"
    except InvalidOperation:
        return f"{value:f}"


#: The public channel. Trades are relayed there as a SCAN CARD and deliberately WITHOUT a
#: transaction hash: the owner asked for the reasoning, not the receipt, and a public tx
#: points at the wallet that placed it.
CHANNEL_ENV = ("KAIBA_TELEGRAM_CHANNEL_ID", "TELEGRAM_TRADES_CHANNEL")

#: Chain and grade badges. Cosmetic only; nothing here changes what is reported.
_CHAIN_TAG: dict[str, str] = {
    "sol": "\u26a1 SOL",
    "bsc": "\U0001f7e1 BSC",
    "robinhood": "\U0001f7e2 ROBINHOOD",
}
_GRADE_TAG: dict[str, str] = {
    "A": "\U0001f7e2", "B": "\U0001f7e2", "C": "\U0001f7e1",
    "D": "\U0001f7e0", "QUARANTINED": "\U0001f534",
}

#: chain -> (gmgn, dexscreener) link templates.
_LINKS: dict[str, tuple[str, str]] = {
    "sol": ("https://gmgn.ai/sol/token/{t}", "https://dexscreener.com/solana/{t}"),
    "bsc": ("https://gmgn.ai/bsc/token/{t}", "https://dexscreener.com/bsc/{t}"),
    "robinhood": ("https://gmgn.ai/robinhood/token/{t}",
                  "https://dexscreener.com/robinhood/{t}"),
}


def channel_id() -> str | None:
    """The public channel, if configured. Absent means private chat only."""
    for name in CHANNEL_ENV:
        value = os.environ.get(name)
        if value:
            return value
    body = _read_env_file(TELEGRAM_ENV)
    for name in CHANNEL_ENV:
        if body.get(name):
            return body[name]
    return None


def _measure(body: dict, name: str) -> Decimal | None:
    """A dossier field's value, or None. An ``unavailable`` basis is None, never zero.

    This is why the card prints "-" in places. A dossier that could not establish insider
    share has not established that it is 0%, and printing 0% would be this system telling
    the operator something it does not know -- on a live robinhood dossier ``insider_pct``
    and ``volume_24h_usd`` are genuinely unknown.
    """
    measure = body.get(name)
    if not isinstance(measure, dict):
        return _dec(measure)
    if str(measure.get("basis") or "").lower() in {"", "unavailable"}:
        return None
    return _dec(measure.get("value"))


def _usd(value: Decimal | None) -> str:
    if value is None:
        return "-"
    v = float(value)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def _pct(value: Decimal | None) -> str:
    return "-" if value is None else f"{float(value):.1f}%"


def _age(ms: Any) -> str:
    try:
        delta = (time.time() * 1000 - int(ms)) / 60000
    except (TypeError, ValueError):
        return "-"
    if delta < 60:
        return f"{delta:.0f}m"
    if delta < 1440:
        return f"{delta / 60:.1f}h"
    return f"{delta / 1440:.1f}d"


def _deployer_lines(conn: sqlite3.Connection, chain: Chain, token: str) -> list[str]:
    """What this dev shipped before. Silent when unmeasured, never a reassuring zero."""
    try:
        from kaiba.intelligence.deployer import lookup

        record = lookup(conn, chain, token)
    except Exception:  # noqa: BLE001 - an optional section may never break a card
        return []
    if not record.known:
        return ["dev history: not measured"]
    label = {"runner": "has a prior runner", "all_dud": "no prior runner",
             "no_prior": "first launch we have seen"}
    line = (f"dev history: {record.launches} launch(es)/7d, "
            f"{label.get(record.record, record.record)}")
    if record.prior_best_multiple is not None:
        line += f", best {float(record.prior_best_multiple):.1f}x"
    return [line]


#: The lane's thesis carries its full reasoning, which on sm-trenches runs to 400+
#: characters of standing explanation ("rug ratio UNAVAILABLE (ceiling 0.3): not a refusal
#: and not strength; the rug defence is the dossier blockers ..."). That belongs in the
#: journal, not in a card someone reads on a phone. The card takes the first clause, which
#: is the part that differs between trades and is therefore the part worth reading.
THESIS_HEADLINE_CHARS = 180


def _headline(thesis: str) -> str:
    """The first clause of a thesis, which is the bit that is actually about THIS token."""
    text = " ".join(str(thesis).split())
    for stop in (";", ". "):
        head, sep, _rest = text.partition(stop)
        if sep and len(head) >= 20:
            text = head
            break
    if len(text) > THESIS_HEADLINE_CHARS:
        text = text[: THESIS_HEADLINE_CHARS - 1].rstrip() + "..."
    return text


def render_scan_card(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """A token scan and the reason we took it, for the public channel.

    THREE THINGS ARE DELIBERATELY ABSENT, all at the owner's instruction:

    * the transaction hash -- a public tx points straight at the wallet that placed it;
    * the position size and the native amount -- this channel is a signal for readers, not
      a disclosure of the owner's book;
    * anything about the exit -- closes are not posted here at all, because a public
      "SELL, +48%" both reveals the book and hands readers an exit they cannot act on at
      the price we got.

    Every number is measured or "-". Nothing is inferred, and an unavailable field is
    never printed as zero.
    """
    chain = Chain(row["chain"])
    token = str(row["token"])
    _ensure_token_identity(conn, chain, token)
    meta = fetch_one(
        conn,
        "SELECT symbol, name, launchpad, created_ms, first_seen_ms FROM tokens "
        "WHERE chain=? AND address=?",
        (chain.value, token),
    )
    dossier = fetch_one(
        conn,
        "SELECT grade, score, warnings_json, dossier_json FROM token_dossiers "
        "WHERE chain=? AND address=?",
        (chain.value, token),
    )
    body: dict = {}
    if dossier is not None:
        try:
            body = json.loads(dossier["dossier_json"] or "{}")
        except Exception:  # noqa: BLE001
            body = {}

    symbol = str((meta["symbol"] if meta else "") or "").strip()
    name = str((meta["name"] if meta else "") or "").strip()
    head = f"${symbol}" if symbol else f"{token[:8]}..."
    if name and name.lower() != symbol.lower():
        head += f" \u2014 {name[:40]}"

    grade = str(dossier["grade"]) if dossier is not None else "?"
    launchpad = str((meta["launchpad"] if meta else "") or "?")
    thesis, _ = _thesis(conn, row["decision_id"])
    holders = _measure(body, "holder_count")
    age_src = None
    if meta is not None:
        age_src = meta["created_ms"] or meta["first_seen_ms"]
    buy_tax = _measure(body, "buy_tax_bps")
    top10 = _measure(body, "top10_pct")
    dev = _measure(body, "dev_pct")

    lines = [
        f"\U0001f680 {head}",
        f"{_CHAIN_TAG.get(chain.value, chain.value.upper())} \u00b7 {launchpad} "
        f"\u00b7 {_GRADE_TAG.get(grade, '\u26aa')} grade {grade}",
        "",
        f"\U0001f4b0 MC: {_usd(_measure(body, 'market_cap_usd'))}",
        f"\U0001f4a7 Liq: {_usd(_measure(body, 'liquidity_usd'))}   "
        f"\U0001f4ca Vol24h: {_usd(_measure(body, 'volume_24h_usd'))}",
        f"\U0001f465 Holders: {int(holders) if holders is not None else '-'}   "
        f"\u23f1 Age: {_age(age_src)}",
        "",
        f"\U0001f4c8 Top10: {_pct(top10)}   \U0001f6e0 Dev: {_pct(dev)}   "
        f"\U0001f575 Insider: {_pct(_measure(body, 'insider_pct'))}",
        f"\U0001f4e6 Bundles: {_pct(_measure(body, 'bundler_pct'))}   "
        f"\U0001f52b Snipers: {_pct(_measure(body, 'sniper_pct'))}   "
        f"\U0001f4b8 Tax: {_pct(buy_tax / 100 if buy_tax is not None else None)}",
    ]
    for line in _deployer_lines(conn, chain, token):
        lines.append(f"\U0001f9ec {line}")

    signal = fetch_one(
        conn,
        "SELECT wallets_json, entities_json FROM signals WHERE chain=? AND token=? "
        "ORDER BY created_ms DESC LIMIT 1",
        (chain.value, token),
    )
    if signal is not None:
        try:
            wallets = len(json.loads(signal["wallets_json"] or "[]"))
            entities = len(json.loads(signal["entities_json"] or "[]"))
            lines.append(
                f"\U0001f9e0 Smart money: {wallets} wallet(s) \u00b7 {entities} entit(ies)"
            )
        except Exception:  # noqa: BLE001
            pass

    lines.append("")
    if thesis:
        lines.append(f"\U0001f3af Why: {_headline(thesis)}")
    if dossier is not None:
        try:
            warnings = json.loads(dossier["warnings_json"] or "[]")
        except Exception:  # noqa: BLE001
            warnings = []
        if warnings:
            lines.append("\u26a0\ufe0f Flags: " + ", ".join(str(w) for w in warnings[:4]))
    unknown = [k.replace("_usd", "").replace("_pct", "")
               for k in ("market_cap_usd", "insider_pct", "volume_24h_usd")
               if _measure(body, k) is None]
    if unknown:
        lines.append("\u2753 Not measured: " + ", ".join(unknown))

    lines += ["", f"`{token}`"]
    gmgn, dexs = _LINKS.get(chain.value, ("", ""))
    if gmgn:
        lines.append(f"\U0001f517 {gmgn.format(t=token)}")
        lines.append(f"\U0001f4c9 {dexs.format(t=token)}")
    return chr(10).join(lines)


def render_close_card(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """A close, for the channel. Result and reason, still no transaction hash."""
    text = render_close(row, conn)
    return chr(10).join(
        line for line in text.split(chr(10)) if not line.strip().startswith("tx ")
    )


def render_for_channel(row: sqlite3.Row, conn: sqlite3.Connection) -> str | None:
    """The public card, or None when this trade does not belong on the channel.

    Closes return None on purpose. The owner's instruction: "this channel is signal for
    people... don't let them know my current sizing". A public "SELL +48%" discloses the
    book twice over -- the position existed, and it was worth that much -- and hands a
    reader an exit they could not have taken at the price we got.
    """
    if str(row["side"]) != "buy":
        return None
    return render_scan_card(row, conn)


@dataclass(frozen=True)
class Notification:
    order_id: str
    text: str


def render_open(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """A filled BUY, as the operator reads it."""
    chain = Chain(row["chain"])
    symbol = _symbol(conn, chain, row["token"])
    spent = _native(chain, row["amount_in"])
    native = _NATIVE_SYMBOL.get(chain, "")
    thesis, grade = _thesis(conn, row["decision_id"])
    usd = _spent_usd(conn, chain, spent)

    lines = [
        f"BUY  {symbol}  ({chain.value})",
        f"size  {_fmt(spent, 6)} {native}" + (f"  (~${_fmt(usd, 2)})" if usd else ""),
        f"token {row['token']}",
        f"lane  {row['lane']}" + (f"   grade {grade}" if grade else ""),
    ]
    if thesis:
        lines.append(f"why   {thesis}")
    if row["tx_hash"]:
        lines.append(f"tx    {row['tx_hash']}")
    return "\n".join(lines)


def render_close(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    """A filled SELL. Carries the realised result when the ledger has closed the position."""
    chain = Chain(row["chain"])
    symbol = _symbol(conn, chain, row["token"])
    native = _NATIVE_SYMBOL.get(chain, "")
    position = fetch_one(
        conn,
        "SELECT cost_native, proceeds_native, realized_native, exit_reason, opened_ms, "
        "closed_ms, entry_price_usd, peak_price_usd FROM positions "
        "WHERE chain=? AND token=? ORDER BY opened_ms DESC LIMIT 1",
        (row["chain"], row["token"]),
    )
    lines = [f"SELL {symbol}  ({chain.value})"]
    if position is not None:
        cost = _native(chain, position["cost_native"])
        proceeds = _native(chain, position["proceeds_native"])
        realized = _native(chain, position["realized_native"])
        if cost and cost > 0 and realized is not None:
            pct = realized / cost * Decimal(100)
            sign = "+" if realized >= 0 else ""
            lines[0] += f"   {sign}{_fmt(pct, 1)}%"
            lines.append(
                f"pnl   {sign}{_fmt(realized, 6)} {native}"
                f"  (got {_fmt(proceeds, 6)} on {_fmt(cost, 6)})"
            )
        entry = _dec(position["entry_price_usd"])
        peak = _dec(position["peak_price_usd"])
        if entry and entry > 0 and peak:
            lines.append(f"peak  {_fmt(peak / entry, 2)}x from entry")
        if position["exit_reason"]:
            lines.append(f"why   {position['exit_reason']}")
        if position["opened_ms"] and position["closed_ms"]:
            held = (int(position["closed_ms"]) - int(position["opened_ms"])) / 60000
            lines.append(f"held  {held:.0f} min")
    lines.append(f"token {row['token']}")
    if row["tx_hash"]:
        lines.append(f"tx    {row['tx_hash']}")
    return "\n".join(lines)


def render(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    return render_open(row, conn) if str(row["side"]) == "buy" else render_close(row, conn)


def _symbol(conn: sqlite3.Connection, chain: Chain, token: str) -> str:
    row = fetch_one(
        conn, "SELECT symbol FROM tokens WHERE chain=? AND address=?", (chain.value, token)
    )
    symbol = (row["symbol"] if row else None) or ""
    symbol = str(symbol).strip()
    return f"${symbol}" if symbol else f"{token[:10]}…"


def _thesis(conn: sqlite3.Connection, decision_id: Any) -> tuple[str, str]:
    if not decision_id:
        return "", ""
    row = fetch_one(
        conn, "SELECT thesis, dossier_grade FROM decisions WHERE decision_id=?", (decision_id,)
    )
    if row is None:
        return "", ""
    return str(row["thesis"] or "")[:400], str(row["dossier_grade"] or "")


def _spent_usd(conn: sqlite3.Connection, chain: Chain, spent: Decimal | None) -> Decimal | None:
    """Best-effort USD. A missing native price drops the figure, never invents one."""
    if spent is None:
        return None
    row = fetch_one(
        conn,
        "SELECT price_usd FROM native_prices WHERE chain=? ORDER BY ts_ms DESC LIMIT 1",
        (chain.value,),
    )
    price = _dec(row["price_usd"]) if row is not None else None
    return spent * price if price else None


# ---------------------------------------------------------------------------- the loop


#: How long one token holds the channel floor. A second fill of the same token inside
#: this window is the same idea, not a new call, and a reader does not need it twice.
#: MEASURED 2026-09-23: 0xed97b6 filled at 02:38 and again at 02:42, and both would have
#: posted an identical card.
CHANNEL_REPEAT_WINDOW_MS: int = 30 * 60 * 1000


def _token_identity(chain: Chain, token: str) -> dict[str, Any]:
    """Ask GMGN for a token's ticker and launchpad. ``{}`` when it cannot say."""
    try:
        from kaiba.providers.gmgn_cli import token_info

        body = getattr(token_info(token, chain), "data", None)
    except Exception as exc:  # noqa: BLE001 - a missing ticker is cosmetic, never fatal
        log.debug("could not read identity for %s: %s", token[:12], exc)
        return {}
    if not isinstance(body, dict):
        return {}
    return {
        key: str(body[key]).strip()
        for key in ("symbol", "name", "launchpad")
        if body.get(key) not in (None, "")
    }


def _ensure_token_identity(conn: sqlite3.Connection, chain: Chain, token: str) -> None:
    """Fill in a missing ticker so the public card reads ``$UU``, not ``0x8805f8...``.

    MEASURED 2026-09-23 on the live box: every recent robinhood entry rendered as a
    truncated address because ``tokens.symbol`` was NULL, while GMGN's ``token info`` --
    the same call this card already makes for price and liquidity -- carried
    ``symbol: "UU", name: "Unicorn", launchpad: "pons_v2"``.

    The two sources are complementary rather than ranked, which is why this only ever
    fills a hole: ``0x3122b3`` had ``AXON`` stored and nothing at GMGN, so a blind
    overwrite would have DELETED a ticker we already had. COALESCE keeps whichever
    source actually knows.

    One provider call, only for a live entry whose ticker is missing, at roughly ten
    entries an hour. A failure leaves the address in place, which is the status quo.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT symbol, name, launchpad FROM tokens WHERE chain=? AND address=?",
            (chain.value, token),
        )
        if row is not None and str(row["symbol"] or "").strip():
            return
        found = _token_identity(chain, token)
        if not found:
            return
        with tx(conn) as c:
            c.execute(
                # `first_seen_ms` is NOT NULL, so a token we are meeting for the first
                # time cannot be inserted without it. Caught by a test: without this the
                # enrichment raised on exactly the tokens it most needed to enrich.
                "INSERT INTO tokens (chain, address, symbol, name, launchpad, first_seen_ms) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(chain, address) DO UPDATE SET "
                "  symbol=COALESCE(NULLIF(tokens.symbol, ''), excluded.symbol), "
                "  name=COALESCE(NULLIF(tokens.name, ''), excluded.name), "
                "  launchpad=COALESCE(NULLIF(tokens.launchpad, ''), excluded.launchpad)",
                (chain.value, token, found.get("symbol"), found.get("name"),
                 found.get("launchpad"), int(time.time() * 1000)),
            )
    except Exception as exc:  # noqa: BLE001 - cosmetic enrichment must never drop a card
        log.debug("identity backfill failed for %s: %s", token[:12], exc)


def _channel_recently_posted(
    conn: sqlite3.Connection, chain: Chain, token: str, now: int
) -> bool:
    """True when this token already held the channel floor inside the repeat window."""
    try:
        row = fetch_one(
            conn,
            "SELECT sent_ms FROM notify_channel WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except Exception:  # noqa: BLE001 - if we cannot tell, posting twice beats staying silent
        return False
    if row is None:
        return False
    return (now - int(row["sent_ms"] or 0)) < CHANNEL_REPEAT_WINDOW_MS


def _mark_channel(conn: sqlite3.Connection, chain: Chain, token: str, now: int) -> None:
    try:
        with tx(conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO notify_channel (chain, token, sent_ms) "
                "VALUES (?,?,?)",
                (chain.value, token, now),
            )
    except Exception:  # noqa: BLE001 - a lost record costs one duplicate card at most
        log.debug("could not record a channel post for %s", token[:12])


def _ensure_cursor(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notify_channel ("
        "  chain TEXT NOT NULL,"
        "  token TEXT NOT NULL,"
        "  sent_ms INTEGER NOT NULL,"
        "  PRIMARY KEY (chain, token))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notify_cursor ("
        "  name TEXT PRIMARY KEY,"
        "  last_ms INTEGER NOT NULL,"
        "  updated_ms INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notify_sent ("
        "  order_id TEXT PRIMARY KEY,"
        "  sent_ms INTEGER NOT NULL,"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  delivered INTEGER NOT NULL DEFAULT 0)"
    )


def pending(conn: sqlite3.Connection, *, now_ms: int | None = None) -> list[sqlite3.Row]:
    """Filled orders we have not delivered yet, oldest first."""
    _ensure_cursor(conn)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    row = fetch_one(conn, "SELECT last_ms FROM notify_cursor WHERE name='trades'")
    if row is None:
        since = now - COLD_START_LOOKBACK_MS
        with tx(conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO notify_cursor (name, last_ms, updated_ms) VALUES (?,?,?)",
                ("trades", since, now),
            )
    else:
        since = int(row["last_ms"])
    return fetch_all(
        conn,
        # `>=`, not `>`. FOUND 2026-09-22 by the operator agent reviewing this module:
        # with a strict `>` and a cursor advanced to the highest delivered `updated_ms`,
        # a SECOND fill carrying that same millisecond is excluded for good. Exactly-once
        # does not depend on the cursor -- `notify_sent` has a primary key on order_id and
        # the join below is what enforces it -- so the cursor only needs to bound how far
        # back we look, and the safe direction for that bound is inclusive.
        #
        # `provider <> 'paper'` is defence in depth. Measured on the live box every paper
        # fill also carries mode='shadow', so `mode<>'shadow'` already excludes them
        # today; this stops a future paper path that forgets the mode from announcing
        # fictional money as real.
        "SELECT o.* FROM orders o "
        "LEFT JOIN notify_sent s ON s.order_id = o.order_id "
        "WHERE o.state='filled' AND o.mode<>'shadow' "
        "  AND (o.provider IS NULL OR o.provider <> 'paper') "
        "  AND o.updated_ms >= ? "
        "  AND (s.order_id IS NULL OR (s.delivered=0 AND s.attempts < ?)) "
        "ORDER BY o.updated_ms, o.order_id LIMIT 25",
        (since, MAX_ATTEMPTS),
    )


def _mark(conn: sqlite3.Connection, order_id: str, *, delivered: bool, now: int) -> None:
    with tx(conn) as c:
        c.execute(
            "INSERT INTO notify_sent (order_id, sent_ms, attempts, delivered) VALUES (?,?,1,?) "
            "ON CONFLICT(order_id) DO UPDATE SET attempts=attempts+1, sent_ms=excluded.sent_ms, "
            "delivered=excluded.delivered",
            (order_id, now, 1 if delivered else 0),
        )


# ---------------------------------------------------------------------------- alerts

#: Journal kinds delivered one message each. These are decisions and course-corrections --
#: low volume (41 rows in a measured 24h) and each one is something the operator would want
#: to know at the time it happened.
ALERT_JOURNAL_KINDS = ("change", "lesson", "correction")

#: Journal kinds rolled into a digest instead. ``observation`` ran at 340 rows/24h on the
#: live box; one message each would bury the trades.
DIGEST_JOURNAL_KINDS = ("observation",)

#: How often the digests go out.
DIGEST_INTERVAL_S = 3600


def _ensure_alert_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS notify_alerts ("
        "  source TEXT NOT NULL,"
        "  ref TEXT NOT NULL,"
        "  sent_ms INTEGER NOT NULL,"
        "  PRIMARY KEY (source, ref))"
    )


def pending_alerts(conn: sqlite3.Connection, *, now_ms: int | None = None) -> list[tuple[str, str]]:
    """``(ref, text)`` for decisions and halts not yet delivered."""
    _ensure_alert_tables(conn)
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    since = now - COLD_START_LOOKBACK_MS
    out: list[tuple[str, str]] = []
    marks = ",".join("?" for _ in ALERT_JOURNAL_KINDS)
    rows = fetch_all(
        conn,
        f"SELECT j.rowid AS rid, j.kind, j.body, j.ts_ms FROM journal j "
        f"LEFT JOIN notify_alerts a ON a.source='journal' AND a.ref = CAST(j.rowid AS TEXT) "
        f"WHERE j.kind IN ({marks}) AND j.ts_ms > ? AND a.ref IS NULL "
        f"ORDER BY j.ts_ms LIMIT 10",
        (*ALERT_JOURNAL_KINDS, since),
    )
    for row in rows:
        kind = str(row["kind"]).upper()
        body = " ".join(str(row["body"] or "").split())[:900]
        out.append((str(row["rid"]), f"{kind}\n{body}"))
    return out


def _digest_due(conn: sqlite3.Connection, name: str, now: int) -> bool:
    _ensure_cursor(conn)
    row = fetch_one(conn, "SELECT last_ms FROM notify_cursor WHERE name=?", (name,))
    if row is None:
        with tx(conn) as c:
            c.execute(
                "INSERT OR REPLACE INTO notify_cursor (name, last_ms, updated_ms) VALUES (?,?,?)",
                (name, now, now),
            )
        return False
    return now - int(row["last_ms"]) >= DIGEST_INTERVAL_S * 1000


def _mark_digest(conn: sqlite3.Connection, name: str, now: int) -> None:
    with tx(conn) as c:
        c.execute(
            "INSERT OR REPLACE INTO notify_cursor (name, last_ms, updated_ms) VALUES (?,?,?)",
            (name, now, now),
        )


def intelligence_digest(conn: sqlite3.Connection, *, since_ms: int) -> str | None:
    """What the intelligence side gathered and graded. A digest, never one per wallet.

    MEASURED on the live box: 92,268 wallet rows in 24 hours. One message each is not a
    notification channel, it is a denial of service on the operator's attention. Only the
    GRADED wallets (182 in total) carry a judgement worth reading, so the digest counts the
    gathering and names the grading.
    """
    seen = fetch_one(
        conn, "SELECT COUNT(*) AS n FROM wallets WHERE first_seen_ms > ?", (since_ms,)
    )
    graded = fetch_all(
        conn,
        "SELECT chain, address, grade, score, archetype, win_rate, realized_pnl_usd "
        "FROM wallet_scores ORDER BY score DESC LIMIT 5",
    )
    total_graded = fetch_one(conn, "SELECT COUNT(*) AS n FROM wallet_scores")
    lines = ["INTELLIGENCE (last hour)"]
    lines.append(f"wallets seen   {int(seen['n']) if seen else 0}")
    lines.append(f"wallets graded {int(total_graded['n']) if total_graded else 0} total")
    if graded:
        lines.append("top graded:")
        for row in graded:
            win = row["win_rate"]
            lines.append(
                f"  {str(row['grade'] or '?'):2} {str(row['address'])[:16]}… "
                f"{str(row['archetype'] or '')[:14]}"
                + (f" win={float(win) * 100:.0f}%" if win is not None else "")
            )
    # The study table only exists once `entry_study` has run. A digest is best-effort by
    # definition: a missing optional section must not cost the operator the whole message.
    try:
        studies = fetch_all(
            conn,
            "SELECT cell, n, rate, lift FROM entry_study WHERE computed_ms = "
            "(SELECT MAX(computed_ms) FROM entry_study) ORDER BY lift DESC LIMIT 3",
        )
    except sqlite3.Error:
        studies = []
    if studies:
        lines.append("entry study (best cells):")
        for row in studies:
            lines.append(
                f"  {str(row['cell'])[:30]:30} n={int(row['n']):4} "
                f"{float(row['rate']):.1f}% lift={float(row['lift']):.2f}x"
            )
    return "\n".join(lines)


def reports_digest(conn: sqlite3.Connection, *, since_ms: int) -> str | None:
    """The agent's own observations, rolled up. 340 rows/24h is not a per-message stream."""
    marks = ",".join("?" for _ in DIGEST_JOURNAL_KINDS)
    rows = fetch_all(
        conn,
        f"SELECT kind, body, ts_ms FROM journal WHERE kind IN ({marks}) AND ts_ms > ? "
        f"ORDER BY ts_ms DESC LIMIT 4",
        (*DIGEST_JOURNAL_KINDS, since_ms),
    )
    if not rows:
        return None
    total = fetch_one(
        conn,
        f"SELECT COUNT(*) AS n FROM journal WHERE kind IN ({marks}) AND ts_ms > ?",
        (*DIGEST_JOURNAL_KINDS, since_ms),
    )
    lines = [f"REPORTS (last hour, {int(total['n']) if total else len(rows)} entries)"]
    for row in rows:
        body = " ".join(str(row["body"] or "").split())[:260]
        lines.append(f"• {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- feed pages
#
# WHY THIS EXISTS. From 2026-10-09 to 10-21 four ingest feeds (sol_wallets, rh_wallets,
# flap, leader_exit) were dark and nothing paged anyone. Nothing on the box read
# ``ingest_status``: the runner wrote the same "idle" text for a feed that had lost its
# endpoint as for telegram, which was never configured, and from 10-09 to 10-13 three of
# them sat in ``running`` with a frozen ``last_event_ms`` that no check looked at. These
# pages are the first reader of that table. They ride the journal alerts' send path and
# their ``notify_alerts`` dedupe (source ``ingest``); the daily report's RED line
# (``kaiba.mcp.server._red_flags``) repeats the same verdicts every day until fixed.

#: Feeds that are supposed to run on the box. A feed listed here pages when it is down even
#: if it has NEVER produced an event. ``leader_exit`` is why: 0 events in its whole life
#: since it was registered on 10-06, so a rule built only on "it used to produce" cannot
#: tell it from telegram.
EXPECTED_FEEDS: frozenset[str] = frozenset({"leader_exit", "flap", "sol_wallets", "rh_wallets"})

#: States in which a listener is not running at all. ``stopped`` is here because a stopped
#: kaiba-ingest leaves every feed dark exactly like an idle one; the grace below is what
#: keeps the few seconds of ``stopped`` during a restart from paging. ``down`` (2026-10-28):
#: the listener is alive but has stopped trying until something changes -- j7_feed after J7
#: refused its session token, which it will not retry for 900 s. It writes its row only when
#: something changes, so a refusal that stands ages past the grace and pages.
FEED_DOWN_STATES: tuple[str, ...] = ("idle", "crashed", "disabled", "stopped", "down")

#: States in which a listener is meant to be producing. ``reconnecting`` is included because
#: a listener that crashes on start sits in it for its backoff and in ``running`` for under
#: a second, so a poll every 10 s would almost never see ``running``.
FEED_LIVE_STATES: tuple[str, ...] = ("running", "reconnecting", "starting")

#: A down state must last this long before it pages.
FEED_DOWN_GRACE_MS: int = 300_000

#: Per-feed silence limits for a LIVE feed. NOT MEASURED: the healthy-period tape was
#: deleted by retention before anyone looked. Tune per feed after the first week.
#: flap: the measured launch rate (529-916 an hour) makes 30 quiet minutes a dead socket;
#: sol_wallets: the tape showed two healthy zero-row hours on 10-07; rh_wallets: bursty;
#: leader_exit: only counts with :data:`LEADER_EXIT_MIN_POSITIONS` positions to observe.
#: A feed with no entry here never pages for silence (rhscannerr, telegram, ...).
FEED_SILENT_MS: dict[str, int] = {
    "flap": 30 * 60_000,
    "sol_wallets": 6 * 3_600_000,
    "rh_wallets": 24 * 3_600_000,
    "leader_exit": 6 * 3_600_000,
    # j7_feed (2026-10-28): J7's public /api/health showed 9,502 posts in its 12 h cache that
    # day (~13 a minute; a later read the same day: 10,916 posts spanning 4.2 h), so 30 silent
    # minutes while running means the feed is dead. Its `last_event_ms` moves only on a live
    # post, never on the backlog replay or a ping. Our socket carries only the main feed's
    # accounts, so its own rate is not measured yet: tune after the first week.
    "j7_feed": 30 * 60_000,
}

#: Idle reasons that are the owner's own off switch, not an outage: an idle row with one of
#: them never pages, however much the feed produced before. j7_feed idles with
#: ``missing_j7_session`` until the owner writes ~/.config/kaiba/j7.session, and deleting that
#: file is how he turns it off. A feed listed here counts as EXPECTED (it pages even if it
#: never produced) once its row carries any other reason: the switch is on and it is not
#: working.
FEED_OWNER_SWITCH: dict[str, tuple[str, ...]] = {"j7_feed": ("missing_j7_session",)}

#: The rule every j7_feed instruction ends with (2026-10-28 security review): the token is
#: the owner's J7 login, so it is written to the box over SSH and never travels through a chat
#: where it would sit in Telegram, Hermes's session store and a model's context.
J7_TOKEN_RULE = ("Only you turn this on, by writing the file over SSH. Never send the token in Telegram, "
                 "to Hermes, in Discord or in a support ticket; nobody at J7 needs it, and Hermes must not "
                 "ask for it or accept it.")
#: When a fixed token file is picked up (kaiba/ingest/j7_feed.py): a RUNNING listener that
#: found the file unusable is `down` and re-reads it every 30 s; a listener that found it so at
#: start returned idle, and the runner tries it again every 6 h.
J7_FILE_RECHECK = ("Kaiba looks again within 30 seconds while the feed is `down`; while it is `idle`, "
                   "at its next idle check (within 6 hours) or when kaiba-ingest restarts.")
#: The connection itself keeps failing or dropping (`connect_failed:<region>`,
#: `disconnected:<region>`, `stalled:<region>` on a silent or reconnecting row).
J7_CONNECTION_ACTION = (
    "Kaiba keeps reconnecting to J7 and switches between its two servers (nyc, dfw) by itself, "
    "and nothing arrives. Check J7's own status (`curl -s https://nyc.j7tracker.io/api/health`, "
    "public, no login) and the box's network; if J7 is up, the kaiba-ingest log's j7_feed lines "
    "say what fails."
)

#: What the owner should do, per feed: ``(code in last_error, instruction)``, first match wins.
FEED_ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "j7_feed": (
        ("auth_error:too_many_connections",
         "J7 allows 5 feed connections per account, shared with your j7tracker.io browser tabs: "
         "close one; Kaiba tries again within 15 minutes."),
        ("auth_error:refused",
         "J7 refused the connection with a message Kaiba does not recognise (the kaiba-ingest log "
         "shows its text): check at j7tracker.io that your account and plan can use the feed, then "
         "refresh the J7 session token -- log in at j7tracker.io yourself and write the new token as "
         "one line to ~/.config/kaiba/j7.session (docs/runbooks/j7-feed.md); it is picked up within a "
         f"minute. {J7_TOKEN_RULE}"),
        ("auth_error",
         "J7 refused the session token, or it expired: refresh the J7 session token -- log in at "
         "j7tracker.io yourself and write the new token as one line to ~/.config/kaiba/j7.session "
         f"(docs/runbooks/j7-feed.md); it is picked up within a minute. {J7_TOKEN_RULE}"),
        ("j7_session_in_env",
         "J7_SESSION_JWT is set in kaiba-ingest's environment. j7_feed reads its token only from "
         "~/.config/kaiba/j7.session (deleting that file is the off switch, and a variable is handed "
         "on to every program the service starts), so it does not run while the variable is set: put "
         "the token in that file (docs/runbooks/j7-feed.md), remove J7_SESSION_JWT from kaiba-ingest's "
         f"environment (its EnvironmentFile or a drop-in) and restart kaiba-ingest. {J7_TOKEN_RULE}"),
        ("insecure_j7_session",
         "Kaiba does not use ~/.config/kaiba/j7.session: other accounts on the box can read it, or "
         "it is not owned by the user that runs kaiba-ingest. As that user, run `chmod 600 "
         "~/.config/kaiba/j7.session`; `ls -l` must then show -rw------- and that user. A file "
         "written with sudo belongs to root: delete it and write it again as that user "
         f"(docs/runbooks/j7-feed.md). {J7_FILE_RECHECK} {J7_TOKEN_RULE}"),
        ("unreadable_j7_session",
         "kaiba-ingest cannot read ~/.config/kaiba/j7.session: check `ls -l "
         "~/.config/kaiba/j7.session`; it must be a file owned by the user that runs kaiba-ingest, "
         "mode 600 (-rw-------). A file written with sudo belongs to root: delete it and write it "
         f"again as that user (docs/runbooks/j7-feed.md). {J7_FILE_RECHECK} {J7_TOKEN_RULE}"),
        ("invalid_j7_session",
         "~/.config/kaiba/j7.session is empty or not one line: write the J7 session token as one "
         "line, replacing the file in one step (write j7.session.new, then mv it over j7.session; "
         f"docs/runbooks/j7-feed.md). {J7_FILE_RECHECK} {J7_TOKEN_RULE}"),
        ("missing_socketio_dependency",
         "python-socketio's asyncio client cannot run in the Kaiba venv (python-socketio or aiohttp "
         "is missing; J7's own `python-socketio[client]` line does not bring aiohttp): install the "
         "`social` extra (docs/runbooks/j7-feed.md)."),
        ("token_expiring",
         "the J7 session token expires within the hour: refresh the J7 session token before it does "
         "-- log in at j7tracker.io yourself and write the new token as one line to "
         f"~/.config/kaiba/j7.session (docs/runbooks/j7-feed.md). {J7_TOKEN_RULE}"),
        ("store_full",
         "data/j7_feed.db hit its hard ceiling (640 MiB) or the disk is 88 % full: the feed stores "
         "nothing but counters until retention or kaiba-disk-guard frees space; check `df -h`."),
        ("store_capacity",
         "data/j7_feed.db reached its 512 MiB cap: posts are kept without text, and retention is "
         "7 days instead of 14, until the file is back under the cap."),
        ("store_error",
         "data/j7_feed.db refuses writes, so posts arrive but are not kept: check its owner and mode "
         "with `ls -l data/j7_feed.db*` (a -wal or -shm file left by a manual run as root is the usual "
         "cause) and the disk with `df -h`. The file holds at most 14 days of posts; if it is damaged, "
         "stop kaiba-ingest, delete data/j7_feed.db* and start it again (docs/runbooks/j7-feed.md)."),
        ("upstream:disconnected",
         "J7 reports its own X feed disconnected (its status event): nothing to fix on Kaiba's side."),
        ("connect_failed:", J7_CONNECTION_ACTION),
        ("disconnected:", J7_CONNECTION_ACTION),
        ("stalled:", J7_CONNECTION_ACTION),
    ),
}

#: Codes that page while the feed is RUNNING: it works, and the owner still has to act. One
#: page per code per listener run (the ref carries ``started_ms``, which j7_feed also moves when
#: it tries a new token); ``token_expiring`` carries
#: the token's ``exp``, so each token pages once. ``store_error`` (2026-10-28): every write to
#: data/j7_feed.db failing kept posts arriving, `last_event_ms` fresh and the row `running`,
#: so neither the silence nor the down rule fired while nothing was kept (integration review).
FEED_LIVE_PAGES: dict[str, tuple[str, ...]] = {
    "j7_feed": ("token_expiring", "store_full", "store_capacity", "store_error"),
}

#: leader_exit writes an observation row only when an sm-trenches sol position opens, so a
#: quiet stretch with nothing to observe is not a dead feed. It pages for silence only
#: when at least this many such positions opened while it wrote nothing.
LEADER_EXIT_MIN_POSITIONS: int = 3

#: What leader_exit watches (``leader_exit.CHAIN`` / ``LANE`` / ``DEFAULT_PARAMS["modes"]``).
#: Spelled here so the notifier does not import the observer and its socket stack; a test
#: pins the two together.
LEADER_EXIT_SCOPE: tuple[str, str, tuple[str, ...]] = ("sol", "sm-trenches", ("live", "shadow"))

#: The positions count behind the leader_exit rule is cached this long (it is evaluated at
#: most once a minute, and only while leader_exit is already silent past its limit).
POSITION_COUNT_TTL_MS: int = 60_000

#: Standing states that wait on the owner, said plainly so the page cannot read as a crash.
#: rh_wallets has no safe public route (the public Robinhood RPC refuses websockets, and
#: polling would land on the host protection already gets 429s from), so it stays idle
#: until the owner funds a provider.
FEED_IDLE_NOTES: dict[str, str] = {
    "rh_wallets": "idle: needs a websocket provider; owner decision",
}

#: ``(id(conn), since_ms) -> (computed_ms, count)``. Bounded by :func:`_count_positions`.
_POSITION_COUNTS: dict[tuple[int, int], tuple[int, int]] = {}


@dataclass(frozen=True)
class FeedAlert:
    """One feed that needs a human. ``ref`` dedupes the page; ``line`` is the RED entry."""

    feed: str
    state: str
    kind: str  # "down" | "silent"
    ref: str
    text: str
    line: str


def _span(ms: int | float) -> str:
    """A duration for a phone screen: ``45s``, ``12m``, ``9.9h``, ``11.8d``."""
    s = max(0.0, float(ms) / 1000)
    if s < 120:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s / 60:.0f}m"
    if s < 172_800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86_400:.1f}d"


def _utc(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _count_positions(conn: sqlite3.Connection, since_ms: int, now: int) -> int:
    """sol sm-trenches positions (the modes leader_exit watches) opened after ``since_ms``."""
    key = (id(conn), int(since_ms))
    hit = _POSITION_COUNTS.get(key)
    if hit is not None and now - hit[0] < POSITION_COUNT_TTL_MS:
        return hit[1]
    chain, lane, modes = LEADER_EXIT_SCOPE
    marks = ",".join("?" for _ in modes)
    row = fetch_one(
        conn,
        f"SELECT COUNT(*) AS n FROM positions WHERE chain=? AND lane=? AND mode IN ({marks}) "
        f"AND opened_ms > ?",
        (chain, lane, *modes, int(since_ms)),
    )
    n = int(row["n"]) if row else 0
    if len(_POSITION_COUNTS) > 16:
        _POSITION_COUNTS.clear()
    _POSITION_COUNTS[key] = (now, n)
    return n


def _history(row: dict[str, Any], now: int) -> str:
    seen = int(row["events_seen"] or 0)
    if row["last_event_ms"] is None:
        return f"no event recorded; {seen:,} events in its life"
    last = int(row["last_event_ms"])
    return f"last event {_utc(last)} ({_span(now - last)} ago); {seen:,} events in its life"


#: The ``last_error`` elements a silence page takes its "do:" line from first (:func:`_silent_alert`).
SILENT_ACTION_PREFIXES: tuple[str, ...] = ("connect_failed:", "disconnected:", "stalled:", "upstream:")


def _feed_action(feed: str, reason: str) -> str | None:
    """The owner's instruction for a feed's ``last_error`` (:data:`FEED_ACTIONS`), if any."""
    for code, action in FEED_ACTIONS.get(feed, ()):
        if code in reason:
            return action
    return None


def _owner_switch(feed: str, state: str, reason: str) -> bool | None:
    """For a feed with an owner switch (:data:`FEED_OWNER_SWITCH`): ``False`` while it is off
    (idle for a listed reason), ``True`` once its row carries any other reason (on, and not
    working), ``None`` when the row says nothing either way or the feed has no switch."""
    quiet = FEED_OWNER_SWITCH.get(feed)
    if not quiet:
        return None
    if any(code in reason for code in quiet):
        return False if state == "idle" else None
    return True if reason else None


def _down_alert(row: dict[str, Any], now: int) -> FeedAlert | None:
    feed, state = str(row["feed"]), str(row["state"])
    seen = int(row["events_seen"] or 0)
    if now - int(row["updated_ms"] or 0) <= FEED_DOWN_GRACE_MS:
        return None  # a restart passes through `stopped` for seconds; that is not an outage
    since = int(row["updated_ms"] or 0)
    # Redacted: a crash reason is an exception string, and an httpx error renders the full
    # request URL -- for Alchemy that URL is the key. This text goes to Telegram.
    reason = redact_text(" ".join(str(row["last_error"] or "").split()))[:300]
    switch = _owner_switch(feed, state, reason)
    if switch is False:
        return None  # the owner's own off switch (j7_feed with no session token): not an outage
    if seen <= 0 and feed not in EXPECTED_FEEDS and not switch:
        return None  # never produced and nobody expects it to: telegram, never configured
    note = FEED_IDLE_NOTES.get(feed) if state == "idle" else None
    if note:
        head = "INGEST FEED IDLE (owner decision)"
        first = f"{feed} {note}. Not a crash: it stays idle until a provider is funded."
        line = f"{feed} {note}"
    else:
        # "marked", not "since": every restart re-marks an idle feed, so `updated_ms` is the
        # latest marking, not the start of the outage. The last event dates the outage.
        head = "INGEST FEED DOWN"
        first = f"{feed} is {state} (marked {_utc(since)}, {_span(now - since)} ago)."
        line = f"{feed} {state}"
        if seen <= 0:
            first += " It is expected to run and has never produced an event."
            line += ", expected to run, never produced"
        elif row["last_event_ms"] is not None:
            line += f", last event {_span(now - int(row['last_event_ms']))} ago"
        if reason:
            line += f" ({reason[:120]})"
    lines = [head, first, _history(row, now)]
    if reason:
        lines.append(f"reason: {reason}")
    action = _feed_action(feed, reason)
    if action:
        lines.append(f"do: {action}")
        line += f"; {action}"
    ref = f"{feed}:{state}:{row['last_event_ms']}"
    if feed in FEED_ACTIONS:
        # A feed with owner instructions (j7_feed, 2026-10-28) pages once per CODE and per
        # run or token (its listener moves started_ms when it tries a different token), not
        # once per last_event_ms: until it delivered a post, every later failure reused the
        # first page's ref, so a refreshed token that J7 refused too, or a refusal for another
        # reason, never paged again (integration review r3). Only the row's first code counts,
        # so a store flag joining a standing refusal does not repeat it.
        ref += f":{row['started_ms']}:{reason.split(',')[0].strip()}"
    return FeedAlert(feed=feed, state=state, kind="down", ref=ref, text="\n".join(lines), line=line)


def _silent_alert(conn: sqlite3.Connection, row: dict[str, Any], now: int) -> FeedAlert | None:
    feed, state = str(row["feed"]), str(row["state"])
    limit = FEED_SILENT_MS.get(feed)
    if limit is None:
        return None
    if any(code in str(row["last_error"] or "") for code in FEED_OWNER_SWITCH.get(feed, ())):
        # The listener returned because the owner switched it off (j7_feed: the token file is
        # gone) and the supervisor is winding it down through `reconnecting` to `idle`. Silence
        # is the point; "No token at all is never a page" (docs/runbooks/j7-feed.md).
        return None
    # The later of the last event and this run's start. A restart gives a feed one full
    # window: otherwise the first poll after re-enabling a feed that was dark for 12 days
    # pages "silent 12 d" before it has had a chance to receive anything.
    marks = [int(v) for v in (row["last_event_ms"], row["started_ms"]) if v is not None]
    if not marks:
        return None
    since = max(marks)
    if now - since <= limit:
        return None
    extra = ""
    if feed == "leader_exit":
        opened = _count_positions(conn, since, now)
        if opened < LEADER_EXIT_MIN_POSITIONS:
            return None  # nothing (or too little) to observe is not a dead observer
        extra = f"; {opened} sol sm-trenches positions opened in that time"
    first = (f"{feed} is {state} but has written nothing for {_span(now - since)} "
             f"(limit {_span(limit)}{extra}).")
    line = f"{feed} {state}, silent {_span(now - since)} (limit {_span(limit)}{extra})"
    lines = ["INGEST FEED SILENT", first, _history(row, now)]
    # A live row can still carry a reason (j7_feed: `connect_failed:nyc`, J7's own
    # `upstream:disconnected`); redacted for the same reason as the down page's.
    reason = redact_text(" ".join(str(row["last_error"] or "").split()))[:300]
    if reason:
        lines.append(f"reason: {reason}")
    # A silence page is a connection problem first: take its instruction from the connection
    # element of the reason (`connect_failed:nyc,store_capacity`), not from whichever code
    # FEED_ACTIONS happens to list first (final review, 2026-10-28).
    parts = [p.strip() for p in reason.split(",")]
    action = next((a for p in parts if p.startswith(SILENT_ACTION_PREFIXES)
                   for a in (_feed_action(feed, p),) if a), None) or _feed_action(feed, reason)
    if action:
        lines.append(f"do: {action}")
    # `running` in the ref whatever the momentary live state: a crash loop alternates
    # running/reconnecting and must page once, not once per state.
    return FeedAlert(feed=feed, state=state, kind="silent",
                     ref=f"{feed}:running:{row['last_event_ms']}",
                     text="\n".join(lines), line=line)


def _attention_alerts(row: dict[str, Any], now: int) -> list[FeedAlert]:
    """A LIVE feed whose ``last_error`` carries a :data:`FEED_LIVE_PAGES` code: it runs, and
    the owner still has to act (j7_feed: the session token expires within the hour, or its
    store reached its cap or the disk). One page per code per listener run."""
    feed, state = str(row["feed"]), str(row["state"])
    codes = FEED_LIVE_PAGES.get(feed)
    if not codes:
        return []
    reason = redact_text(" ".join(str(row["last_error"] or "").split()))[:300]
    out: list[FeedAlert] = []
    for element in (part.strip() for part in reason.split(",")):
        if not any(element == code or element.startswith(f"{code}:") for code in codes):
            continue
        what = element
        exp_s = element.split(":", 1)[1] if element.startswith("token_expiring:") else ""
        if exp_s.isdigit():
            exp = int(exp_s) * 1000
            what = (f"the J7 session token expires {_utc(exp)} (in {_span(exp - now)})" if exp > now
                    else f"the J7 session token expired {_utc(exp)} ({_span(now - exp)} ago); the next "
                         "reconnect will be refused")
        action = _feed_action(feed, element)
        lines = ["INGEST FEED ATTENTION", f"{feed} is {state}, and: {what}.", _history(row, now)]
        line = f"{feed} {state}: {what}"
        if action:
            lines.append(f"do: {action}")
            line += f"; {action}"
        out.append(FeedAlert(feed=feed, state=state, kind="attention",
                             ref=f"{feed}:attention:{element}:{row['started_ms']}",
                             text="\n".join(lines), line=line))
    return out


def feed_alerts(conn: sqlite3.Connection, *, now_ms: int | None = None) -> list[FeedAlert]:
    """Every ingest feed that is down or silent right now, and every live one that needs the
    owner (:data:`FEED_LIVE_PAGES`). No dedupe; raises on a DB error.

    It raises on purpose: the daily RED line (``kaiba.mcp.server``) turns an exception into
    "ingest: cannot measure", which is the truth. An empty list here would say "all feeds
    healthy" about a table nobody could read -- the silence this exists to end.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    down = ",".join("?" for _ in FEED_DOWN_STATES)
    live = ",".join("?" for _ in FEED_LIVE_STATES)
    watched = tuple(dict.fromkeys((*FEED_SILENT_MS, *FEED_LIVE_PAGES)))
    timed = ",".join("?" for _ in watched) or "''"
    rows = fetch_all(
        conn,
        "SELECT feed, state, started_ms, last_event_ms, events_seen, last_error, updated_ms "
        f"FROM ingest_status WHERE state IN ({down}) OR (state IN ({live}) AND feed IN ({timed})) "
        "ORDER BY feed LIMIT 50",
        (*FEED_DOWN_STATES, *FEED_LIVE_STATES, *watched),
    )
    out: list[FeedAlert] = []
    for row in rows:
        state = str(row["state"])
        if state in FEED_DOWN_STATES:
            alert = _down_alert(row, now)
        else:
            out.extend(_attention_alerts(row, now))
            alert = _silent_alert(conn, row, now)
        if alert is not None:
            out.append(alert)
    return out


def pending_feed_alerts(conn: sqlite3.Connection, *, now_ms: int | None = None) -> list[tuple[str, str]]:
    """``(ref, text)`` for feed pages not yet delivered (source ``ingest`` in ``notify_alerts``).

    One page per outage: the ref carries the feed's ``last_event_ms``, so a feed that
    recovers (its last event moves) and dies again pages again. For a feed in
    :data:`FEED_ACTIONS` (j7_feed) a down ref also carries ``started_ms`` and the row's first
    code: another code, or a new run or token, is another page.
    """
    _ensure_alert_tables(conn)
    alerts = feed_alerts(conn, now_ms=now_ms)
    if not alerts:
        return []
    marks = ",".join("?" for _ in alerts)
    sent = {
        str(r["ref"]) for r in fetch_all(
            conn,
            f"SELECT ref FROM notify_alerts WHERE source='ingest' AND ref IN ({marks})",
            tuple(a.ref for a in alerts),
        )
    }
    return [(a.ref, a.text) for a in alerts if a.ref not in sent]


def _deliver_alerts(c: sqlite3.Connection, source: str, items: list[tuple[str, str]], *,
                    token: str, chat: str, now: int, out: dict[str, int]) -> None:
    """Send ``(ref, text)`` alerts in order, recording each delivered ref under ``source``.

    Stops at the first failed send, so the rest are retried next pass in order.
    """
    for ref, text in items:
        if not send(text, token=token, chat_id=chat):
            break
        with tx(c) as conn2:
            conn2.execute(
                "INSERT OR REPLACE INTO notify_alerts (source, ref, sent_ms) VALUES (?, ?, ?)",
                (source, ref, now),
            )
        out["sent"] += 1


def once(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """One pass. Returns counts; never raises."""
    c = conn or get_conn()
    token, chat = credentials()
    rows = pending(c)
    out = {"seen": len(rows), "sent": 0, "failed": 0}
    if not token or not chat:
        if rows:
            log.warning(
                "trade notifications are not configured; %d trades not announced", len(rows)
            )
            out["failed"] = len(rows)
        return out
    channel = channel_id()
    now = int(time.time() * 1000)
    highest = 0
    # A quiet hour for trades is not a quiet hour for the agent: alerts and digests below
    # must run whether or not anything filled, so this loop is skipped rather than returned
    # from. An early return here silently disabled every non-trade notification.
    for row in rows:
        try:
            text = render(row, c)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the pass
            log.exception("could not render a trade notification: %s", exc)
            _mark(c, str(row["order_id"]), delivered=False, now=now)
            out["failed"] += 1
            continue
        ok = send(text, token=token, chat_id=chat)
        # The public channel gets a SCAN CARD instead, and never a transaction hash. A
        # failure here must not affect the private feed's delivery record: the operator's
        # own copy is the one that has to be reliable.
        if channel and ok:
            try:
                chain = Chain(row["chain"])
                subject = str(row["token"])
                if _channel_recently_posted(c, chain, subject, now):
                    out["channel_skipped"] = out.get("channel_skipped", 0) + 1
                else:
                    card = render_for_channel(row, c)
                    if card:
                        # The result is READ, not discarded. It was discarded until
                        # 2026-09-23, which meant a bot demoted in the channel, a renamed
                        # channel or a rejected message produced exactly the same silence
                        # as a quiet hour: the public feed could have been dead for a day
                        # with nothing anywhere saying so.
                        posted = send(card, token=token, chat_id=channel)
                        if posted:
                            out["channel_sent"] = out.get("channel_sent", 0) + 1
                            _mark_channel(c, chain, subject, now)
                        else:
                            out["channel_failed"] = out.get("channel_failed", 0) + 1
                            log.warning(
                                "the public channel REFUSED the card for %s on %s; the "
                                "private feed is unaffected and this order stays delivered",
                                subject[:12], chain.value,
                            )
            except Exception:  # noqa: BLE001 - the channel is a relay, not the record
                out["channel_failed"] = out.get("channel_failed", 0) + 1
                log.exception("channel relay failed for %s", row["order_id"])
        _mark(c, str(row["order_id"]), delivered=ok, now=now)
        if ok:
            out["sent"] += 1
            highest = max(highest, int(row["updated_ms"] or 0))
        else:
            out["failed"] += 1
            break  # keep order; retry this one next pass
    if highest:
        with tx(c) as conn2:
            conn2.execute(
                "UPDATE notify_cursor SET last_ms=?, updated_ms=? WHERE name='trades' AND last_ms<?",
                (highest, now, highest),
            )

    # Alerts: decisions, halts and course-corrections, one message each. Deliberately after
    # the trades -- a trade is the thing most likely to need acting on, and a burst of
    # alerts must never delay it.
    try:
        _deliver_alerts(c, "journal", pending_alerts(c, now_ms=now), token=token, chat=chat,
                        now=now, out=out)
    except Exception:  # noqa: BLE001 - an alert failure must never stop the trade feed
        log.exception("alert delivery failed")

    # Feed pages: an ingest listener down or silent (see `feed_alerts`). After the journal
    # alerts, on the same send path and the same dedupe table.
    try:
        _deliver_alerts(c, "ingest", pending_feed_alerts(c, now_ms=now), token=token, chat=chat,
                        now=now, out=out)
    except Exception:  # noqa: BLE001 - a feed page failure must never stop the trade feed
        log.exception("feed alert delivery failed")

    # Digests: streams too large to send per row. See `intelligence_digest`.
    for name, builder in (("digest_reports", reports_digest),
                          ("digest_intel", intelligence_digest)):
        try:
            if not _digest_due(c, name, now):
                continue
            text = builder(c, since_ms=now - DIGEST_INTERVAL_S * 1000)
            if text and send(text, token=token, chat_id=chat):
                out["sent"] += 1
            _mark_digest(c, name, now)
        except Exception:  # noqa: BLE001 - a digest is the least important thing here
            log.exception("digest %s failed", name)
    return out


def run(conn: sqlite3.Connection | None = None, *, interval_s: float = POLL_INTERVAL_S,
        max_passes: int | None = None) -> dict[str, int]:
    """Poll until stopped. ``max_passes`` bounds it for tests."""
    c = conn or get_conn()
    totals = {"seen": 0, "sent": 0, "failed": 0}
    passes = 0
    while max_passes is None or passes < max_passes:
        try:
            got = once(c)
            for key in totals:
                totals[key] += got.get(key, 0)
        except Exception:  # noqa: BLE001 - the notifier must outlive any single failure
            log.exception("trade notification pass failed")
        passes += 1
        if max_passes is not None and passes >= max_passes:
            break
        time.sleep(interval_s)
    return totals


def main() -> int:  # pragma: no cover - process entry point
    """``python -m kaiba.ops.trade_notify``. Runs until stopped."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    token, chat = credentials()
    if not token or not chat:
        log.error(
            "no Telegram bot token or chat id; set KAIBA_TELEGRAM_BOT_TOKEN and "
            "KAIBA_TELEGRAM_CHAT_ID, or provide %s",
            TELEGRAM_ENV,
        )
    log.info("trade notifier started (configured=%s)", bool(token and chat))
    run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
