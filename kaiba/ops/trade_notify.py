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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, tx
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
        for ref, text in pending_alerts(c, now_ms=now):
            if send(text, token=token, chat_id=chat):
                with tx(c) as conn2:
                    conn2.execute(
                        "INSERT OR REPLACE INTO notify_alerts (source, ref, sent_ms) "
                        "VALUES ('journal', ?, ?)",
                        (ref, now),
                    )
                out["sent"] += 1
            else:
                break
    except Exception:  # noqa: BLE001 - an alert failure must never stop the trade feed
        log.exception("alert delivery failed")

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
