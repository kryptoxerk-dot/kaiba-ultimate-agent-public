"""Track the graded sol wallets and flag the moment two of them buy the same token (2026-10-28).

Owner, 2026-10-28: "Find more and more grade A and B solana wallets i really need profitable
wallets right now on solana that I can track and make it a confluence".

**Who is tracked.** The S/A/B wallets of the runner hunt's high-tier list
(``data/claude-runners/exports/kaiba-solana-hightier.txt``: GMGN's 30-day record graded by the
rules declared 2026-10-13 in ``deploy/research/runner_hunt/hightier.py``). That export already
leaves out the owner's, the agent's, trusted-copy and blacklisted wallets.

**Why this module exists.** MEASURED 2026-10-27: our tape saw 6% of the high-tier wallets' trades
(10 of 174), so a confluence of them had fired on paper exactly twice. Their buys never reached
``swaps``: the realtime wallet feeds have been idle since 10-09 and the pump.fun trade tape has
been blocked since 10-23. Two GMGN reads close that gap, both read-only and inside the limiter:

* ``track follow-wallet`` -- every trade of every wallet the agent's GMGN account follows, newest
  first, 100 a call (MEASURED 10-28: newest row 2-4 s old). Rows of wallets we track are kept;
  the rest of the follow list is not written. A wallet is "followed" once a row of it appears.
* ``portfolio activity --wallet`` -- the last trades of ONE wallet, for tracked wallets the follow
  list does not cover, ``poll_per_run`` of them per run, least recently polled first.

Both go through :func:`kaiba.ingest.gmgn_feeds.write_swap` exactly like the smart-money and KOL
feeds (source ``gmgn:follow`` / ``gmgn:tracked``): the tape row, the ``wallet.trade`` event, the
token registration that puts the token in front of triage and the scanner. That is also what lets
``confluence-5`` (PAPER, ``wallet_source: runners``) count these wallets at all.

**Confluence.** At least ``min_wallets`` tracked wallets with a buy of one token inside
``window_s`` is written once per level (2, 3, 5, 8 wallets) to ``alpha_signals``
(``kind = wallet_confluence``; the terminal's alpha stream shows it) and sent to the owner's
Telegram, at most ``max_alerts`` per run.

**The reviewed list** (owner 2026-11-04: "1 smart wallet tracking and confluence of really good wallets
that are reviewed daily"). ``deploy/research/wallet_lab/best.py`` ranks the tracked wallets each day on what a
copier would have made and writes the top 10 to ``data/labs/wallet/best.json``. Those wallets are read first:
at 4 reads a run, 2 are theirs, so each is read about every 2 minutes rather than every 35. When a top
:data:`BEST_SINGLE_RANKS` wallet buys, or when 2 or more of the top 10 buy one token inside the window, that is an
``alpha_signals`` row (kind ``best_wallet``) and a Telegram alert. A list over 2 days old is ignored rather than
trusted. Importing the list into the GMGN follow list brings these reads down to seconds.

**What this is NOT.** A trading signal. The wallets' profit is what they made for themselves; copying
them after our lag and fees has measured negative every way it was tried (no-proven-copy-wallets,
copy-exit-early-backtest, the 10-27 wallet review: B tier ran LESS often than random buys). The
alert says so. Entries stay with confluence-5 on paper until its record passes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.schemas import Chain, is_quote_asset, now_ms
from kaiba.ingest import gmgn_feeds as gf

log = logging.getLogger(__name__)

FOLLOW_FEED = "follow"
POLL_FEED = "tracked"
TIERS = ("S", "A", "B")
#: The runner hunt's high-tier export per chain (hightier.py LABEL).
EXPORTS: dict[Chain, str] = {Chain.SOL: "kaiba-solana-hightier.txt"}
WINDOW_S = 1800
MIN_WALLETS = 2
#: An alert fires once per token per level; the next one only when the count reaches the next.
LEVELS = (2, 3, 5, 8)
POLL_PER_RUN = 2
MAX_ALERTS = 3
#: A wallet seen on the follow feed this recently is not polled on its own.
FOLLOW_FRESH_MS = 6 * 3_600_000
#: Rows written per run, at most: a runaway page can never flood the tape.
MAX_ROWS = 400
STATE_PREFIX = "wallet_track:state"
ALERT_SOURCE = "kaiba:wallet_track"
ALERT_KIND = "wallet_confluence"
BEST_KIND = "best_wallet"
#: Of each run's activity reads, this many go to the reviewed list.
BEST_POLL_PER_RUN = 2
#: A single wallet alerts only at these ranks: 10 wallets alerting alone would bury the confluences.
BEST_SINGLE_RANKS = 3
BEST_MAX_AGE_MS = 48 * 3_600_000
MAX_BEST_ALERTS = 3

Fetch = Callable[..., Any]
Send = Callable[[str], bool]


# --------------------------------------------------------------------------------------
# who is tracked
# --------------------------------------------------------------------------------------


def _tier_of(item: dict[str, Any]) -> str | None:
    for group in item.get("groups") or []:
        if str(group).startswith("kaiba-hightier-"):
            tier = str(group).rsplit("-", 1)[-1]
            return tier if tier in TIERS else None
    return None


def tracked_wallets(chain: Chain, data_dir: Path) -> dict[str, dict[str, str]]:
    """``{address: {"tier": "A", "name": "A $93k w54% ..."}}`` from the chain's high-tier export.

    Empty when the export is missing or unreadable: nothing is tracked rather than a guess."""
    name = EXPORTS.get(chain)
    if name is None:
        return {}
    path = Path(data_dir) / "claude-runners" / "exports" / name
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("address"), str):
            continue
        tier = _tier_of(item)
        if tier:
            out[item["address"]] = {"tier": tier, "name": str(item.get("name") or tier)}
    return out


def best_list(data_dir: Path, now: int) -> dict[str, int]:
    """``{address: rank}`` of the listed wallets a copier made money on; nothing if missing or over 2 days old.

    The list keeps its top 10 so it can be scored fairly, but a wallet a copier lost on is not a good wallet, so
    it is not read first and it does not alert.
    """
    try:
        board = json.loads((Path(data_dir) / "labs" / "wallet" / "best.json").read_text(encoding="utf-8"))
        if now - int(board["at"]) * 1000 > BEST_MAX_AGE_MS:
            return {}
        return {str(w["wallet"]): int(w["rank"]) for w in board.get("top") or [] if float(w["mean"]) > 0}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


# --------------------------------------------------------------------------------------
# parsing: both reads become the same SwapRow the smart-money feed writes
# --------------------------------------------------------------------------------------


def parse_follow(payload: Any, chain: Chain, tracked: dict[str, Any]) -> list[gf.SwapRow]:
    """``track follow-wallet`` rows of TRACKED wallets only; the rest of the follow list is dropped."""
    return [r for r in gf._parse_trades(payload, chain, FOLLOW_FEED) if r.wallet in tracked]


def _activity_rows(payload: Any) -> list[dict[str, Any]]:
    """``portfolio activity`` rows reshaped to the trade-feed keys :func:`gf._parse_trades` reads.

    MEASURED shape (2026-10-28): ``{wallet, tx_hash, timestamp (s), event_type, token: {address,
    symbol, total_supply}, token_amount, quote_amount, cost_usd, price_usd, quote_address,
    launchpad, launchpad_platform}``. Transfers, adds and removes are not trades and are skipped."""
    items = payload.get("activities") if isinstance(payload, dict) else payload
    out: list[dict[str, Any]] = []
    for a in items if isinstance(items, list) else []:
        if not isinstance(a, dict) or a.get("event_type") not in ("buy", "sell"):
            continue
        token = a.get("token") if isinstance(a.get("token"), dict) else {"address": a.get("token")}
        out.append({
            "transaction_hash": a.get("tx_hash"), "maker": a.get("wallet"), "chain": a.get("chain"),
            "base_address": token.get("address"), "side": a["event_type"], "timestamp": a.get("timestamp"),
            "token_amount": a.get("token_amount"), "quote_amount": a.get("quote_amount"),
            "quote_address": a.get("quote_address"), "amount_usd": a.get("cost_usd"),
            "price_usd": a.get("price_usd"), "launchpad": a.get("launchpad"),
            "launchpad_platform": a.get("launchpad_platform"),
            "base_token": {k: token.get(k) for k in ("address", "symbol", "name", "total_supply") if token.get(k)},
        })
    return out


def parse_activity(payload: Any, chain: Chain, wallet: str) -> list[gf.SwapRow]:
    """One wallet's activity as trade rows; a row naming another wallet is dropped."""
    return [r for r in gf._parse_trades({"list": _activity_rows(payload)}, chain, POLL_FEED) if r.wallet == wallet]


# --------------------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------------------


def _state_key(chain: Chain) -> str:
    return f"{STATE_PREFIX}:{chain.value}"


def load_state(conn: Any, chain: Chain) -> dict[str, dict[str, int]]:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (_state_key(chain),))
    raw = jload(row["value"], {}) if row else {}
    return {k: {str(a): int(b) for a, b in (raw.get(k) or {}).items()} for k in ("followed", "polled", "alerted", "best_alerted")}


def save_state(conn: Any, chain: Chain, state: dict[str, dict[str, int]], now: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
        (_state_key(chain), jdump(state), now),
    )


# --------------------------------------------------------------------------------------
# confluence
# --------------------------------------------------------------------------------------


def confluences(conn: Any, chain: Chain, tracked: dict[str, dict[str, str]], now: int, *,
                window_s: int = WINDOW_S, min_wallets: int = MIN_WALLETS) -> list[dict[str, Any]]:
    """Tokens at least ``min_wallets`` tracked wallets bought inside the window, most wallets first.

    One indexed read per wallet (``idx_swaps_wallet``: chain, wallet, ts_ms) over the window only.
    A wallet that has since sold the token is still listed (``sold: True``): the alert says so
    rather than hiding it, because "they already exited" is the most useful thing to know."""
    since = now - window_s * 1000
    by_token: dict[str, dict[str, dict[str, Any]]] = {}
    for wallet in tracked:
        for r in fetch_all(conn, "SELECT token, side, ts_ms, usd_value FROM swaps WHERE chain = ? AND wallet = ? "
                                 "AND ts_ms > ? ORDER BY ts_ms", (chain.value, wallet, since)):
            if is_quote_asset(chain, r["token"]):
                continue
            entry = by_token.setdefault(r["token"], {}).get(wallet)
            if r["side"] == "buy":
                if entry is None:
                    entry = {"wallet": wallet, "tier": tracked[wallet]["tier"], "first_buy_ms": int(r["ts_ms"]),
                             "usd": 0.0, "sold": False}
                    by_token[r["token"]][wallet] = entry
                try:
                    entry["usd"] += float(r["usd_value"] or 0)
                except (TypeError, ValueError):
                    pass
                entry["last_buy_ms"] = int(r["ts_ms"])
            elif entry is not None:
                entry["sold"] = True
    out = []
    for token, wallets in by_token.items():
        buyers = list(wallets.values())
        if len(buyers) < min_wallets:
            continue
        buyers.sort(key=lambda b: b["first_buy_ms"])
        out.append({"token": token, "level": len(buyers), "wallets": buyers,
                    "first_ms": buyers[0]["first_buy_ms"], "last_ms": max(b.get("last_buy_ms", 0) for b in buyers),
                    "usd": round(sum(b["usd"] for b in buyers), 2),
                    "tiers": {t: sum(1 for b in buyers if b["tier"] == t) for t in TIERS}})
    out.sort(key=lambda c: (-c["level"], -c["last_ms"]))
    return out


def _level_reached(level: int) -> int:
    reached = 0
    for step in LEVELS:
        if level >= step:
            reached = step
    return reached


def _symbol(conn: Any, chain: Chain, token: str) -> str | None:
    row = fetch_one(conn, "SELECT symbol FROM tokens WHERE chain = ? AND address = ?", (chain.value, token))
    return (row or {}).get("symbol")


def render_alert(chain: Chain, hit: dict[str, Any], symbol: str | None) -> str:
    tiers = " · ".join(f"{t} ×{n}" for t, n in hit["tiers"].items() if n)
    minutes = max(1, round((hit["last_ms"] - hit["first_ms"]) / 60_000))
    sold = sum(1 for w in hit["wallets"] if w["sold"])
    name = f"${symbol}" if symbol else hit["token"][:6] + "…"
    lines = [
        f"Wallet confluence ({chain.value}): {hit['level']} tracked wallets bought {name} within {minutes} min",
        f"{tiers} · ${hit['usd']:,.0f} bought" + (f" · {sold} already sold" if sold else ""),
        f"CA: {hit['token']}",
        f"GMGN: https://gmgn.ai/{chain.value}/token/{hit['token']}",
        "Watch, not a buy signal: copying these wallets after our lag and fees has not been "
        "profitable yet; confluence-5 measures it on paper.",
    ]
    return "\n".join(lines)


def _record(conn: Any, chain: Chain, hit: dict[str, Any], level: int, symbol: str | None, now: int) -> bool:
    payload = {"level": hit["level"], "tiers": hit["tiers"], "usd": hit["usd"], "symbol": symbol,
               "wallets": [{k: w.get(k) for k in ("wallet", "tier", "first_buy_ms", "usd", "sold")}
                           for w in hit["wallets"]]}
    cur = conn.execute(
        "INSERT OR IGNORE INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, event_at_ms, "
        "first_seen_ms, lead_ms, lead_basis, confidence, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{ALERT_KIND}:{chain.value}:{hit['token']}:{level}", ALERT_SOURCE, ALERT_KIND, hit["token"],
         f"{hit['level']} tracked S/A/B wallets bought {symbol or hit['token'][:6]}",
         f"https://gmgn.ai/{chain.value}/token/{hit['token']}", chain.value, hit["last_ms"], now, None,
         "not estimated: a wallet confluence has no public event to lead", None, jdump(payload)),
    )
    return bool(cur.rowcount)


# --------------------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------------------


def _default_follow(chain: Chain) -> Any:
    from kaiba.providers import gmgn_cli

    return gmgn_cli.track_follow_wallet(chain, limit=100)


def _default_activity(wallet: str, chain: Chain) -> Any:
    from kaiba.providers import gmgn_cli

    return gmgn_cli.portfolio_activity(wallet, chain, limit=20)


def _default_send(text: str) -> bool:
    from kaiba.ops import trade_notify

    token, chat = trade_notify.credentials()
    if not token or not chat:
        return False
    return trade_notify.send(text, token=token, chat_id=chat)


def _data(result: Any) -> Any:
    """A ``GmgnResult`` or a bare payload (tests)."""
    return getattr(result, "data", result)


def render_best(chain: Chain, hit: dict[str, Any], ranks: list[int], symbol: str | None, now: int) -> str:
    name = f"${symbol}" if symbol else hit["token"][:6] + "…"
    who = " and ".join(f"#{r}" for r in ranks)
    sold = sum(1 for w in hit["wallets"] if w["sold"])
    minutes = max(0, round((now - hit["last_ms"]) / 60_000))
    head = (f"Best-wallet confluence ({chain.value}): {hit['level']} of today's top wallets ({who}) bought {name}"
            if hit["level"] > 1 else f"Best wallet {who} ({chain.value}) bought {name}")
    return "\n".join([
        head,
        f"${hit['usd']:,.0f} bought, latest {minutes} min ago" + (f" · {sold} already sold" if sold else ""),
        f"CA: {hit['token']}",
        f"GMGN: https://gmgn.ai/{chain.value}/token/{hit['token']}",
        "Tracking only: this list is not proven, and Kaiba does not trade it. The Wallets page shows how it has done.",
    ])


def _record_best(conn: Any, chain: Chain, hit: dict[str, Any], ranks: list[int], symbol: str | None, now: int) -> bool:
    payload = {"level": hit["level"], "ranks": ranks, "usd": hit["usd"], "symbol": symbol,
               "wallets": [{k: w.get(k) for k in ("wallet", "first_buy_ms", "usd", "sold")} for w in hit["wallets"]]}
    cur = conn.execute(
        "INSERT OR IGNORE INTO alpha_signals (signal_key, source, kind, subject, title, url, chain, event_at_ms, "
        "first_seen_ms, lead_ms, lead_basis, confidence, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"{BEST_KIND}:{chain.value}:{hit['token']}:{hit['level']}:{hit['first_ms']}", ALERT_SOURCE, BEST_KIND, hit["token"],
         f"{hit['level']} of the daily best wallets bought {symbol or hit['token'][:6]}",
         f"https://gmgn.ai/{chain.value}/token/{hit['token']}", chain.value, hit["last_ms"], now, None,
         "not estimated: a wallet buy has no public event to lead", None, jdump(payload)),
    )
    return bool(cur.rowcount)


def _alert_best(conn: Any, chain: Chain, tracked: dict[str, dict[str, str]], best: dict[str, int], state: dict[str, dict[str, int]],
                now: int, window_s: int, send: Send, out: dict[str, Any]) -> None:
    """Once per token per level: a top wallet alone, or two or more of the list together."""
    watched = {w: tracked[w] for w in best if w in tracked}
    hits = confluences(conn, chain, watched, now, window_s=window_s, min_wallets=1) if watched else []
    alerts = 0
    for hit in hits:
        ranks = sorted(best[b["wallet"]] for b in hit["wallets"])
        if hit["level"] == 1 and ranks[0] > BEST_SINGLE_RANKS:
            continue
        if hit["level"] <= state["best_alerted"].get(hit["token"], 0):
            continue
        symbol = _symbol(conn, chain, hit["token"])
        new = _record_best(conn, chain, hit, ranks, symbol, now)
        state["best_alerted"][hit["token"]] = hit["level"]
        if new and alerts < MAX_BEST_ALERTS:
            alerts += 1
            if not send(render_best(chain, hit, ranks, symbol, now)):
                out["alert_undelivered"] = out.get("alert_undelivered", 0) + 1
    active = {h["token"] for h in hits}
    state["best_alerted"] = {t: lvl for t, lvl in state["best_alerted"].items() if t in active}
    out.update(best_watched=len(watched), best_buys=len(hits), best_alerts=alerts)


def run(
    conn: Any,
    chain: Chain = Chain.SOL,
    *,
    data_dir: Path | None = None,
    now: int | None = None,
    fetch_follow: Fetch | None = None,
    fetch_activity: Fetch | None = None,
    send: Send | None = None,
    poll_per_run: int = POLL_PER_RUN,
    window_s: int = WINDOW_S,
    min_wallets: int = MIN_WALLETS,
    max_alerts: int = MAX_ALERTS,
) -> dict[str, Any]:
    if data_dir is None:
        from kaiba.core.config import get_settings

        data_dir = Path(get_settings().kaiba_data_dir)
    now = now if now is not None else now_ms()
    tracked = tracked_wallets(chain, data_dir)
    if not tracked:
        return {"chain": chain.value, "tracked": 0, "reason": "no high-tier export to track"}
    state = load_state(conn, chain)
    written = 0
    out: dict[str, Any] = {"chain": chain.value, "tracked": len(tracked)}

    # 1. the follow list: one call covers every tracked wallet the GMGN account follows
    follow_rows: list[gf.SwapRow] = []
    try:
        follow_rows = parse_follow(_data((fetch_follow or _default_follow)(chain)), chain, tracked)
    except Exception as exc:  # noqa: BLE001 - one failed read must not stop the poll or the alerts
        out["follow_error"] = type(exc).__name__
    cursor = gf.load_cursor(conn, chain, FOLLOW_FEED)
    fresh = gf.select_new(follow_rows, cursor, time_ordered=True)[:MAX_ROWS]
    for row in fresh:
        written += bool(gf.write_swap(conn, row))
    gf.save_cursor(conn, chain, FOLLOW_FEED, gf.advance_cursor(cursor, fresh))
    for row in follow_rows:
        state["followed"][row.wallet] = now
    out["follow_rows"] = len(follow_rows)

    # 2. the rest, a few wallets a run, least recently polled first
    due = [w for w in tracked if now - state["followed"].get(w, 0) > FOLLOW_FRESH_MS]
    due.sort(key=lambda w: state["polled"].get(w, 0))
    best = best_list(data_dir, now)
    first = [w for w in due if w in best][:BEST_POLL_PER_RUN]
    due = first + [w for w in due if w not in first]
    polled = 0
    for wallet in due[:max(0, poll_per_run)]:
        state["polled"][wallet] = now
        polled += 1
        try:
            rows = parse_activity(_data((fetch_activity or _default_activity)(wallet, chain)), chain, wallet)
        except Exception as exc:  # noqa: BLE001
            out["poll_error"] = type(exc).__name__
            continue
        for row in rows[:max(0, MAX_ROWS - written)]:
            written += bool(gf.write_swap(conn, row))
    out.update(polled=polled, written=written,
               followed=sum(1 for w in tracked if now - state["followed"].get(w, 0) <= FOLLOW_FRESH_MS))

    # 3. confluence
    hits = confluences(conn, chain, tracked, now, window_s=window_s, min_wallets=min_wallets)
    alerts = 0
    for hit in hits:
        level = _level_reached(hit["level"])
        if level < min_wallets or level <= state["alerted"].get(hit["token"], 0):
            continue
        symbol = _symbol(conn, chain, hit["token"])
        new = _record(conn, chain, hit, level, symbol, now)
        state["alerted"][hit["token"]] = level
        if new and alerts < max_alerts:
            alerts += 1
            if not (send or _default_send)(render_alert(chain, hit, symbol)):
                out["alert_undelivered"] = out.get("alert_undelivered", 0) + 1
    out.update(confluences=len(hits), alerts=alerts)
    _alert_best(conn, chain, tracked, best, state, now, window_s, send or _default_send, out)

    # A token's alert level lives as long as its confluence does; once every buy has left the
    # window it is forgotten, and a later, separate episode alerts again from level 2.
    active = {h["token"] for h in hits}
    state["alerted"] = {t: lvl for t, lvl in state["alerted"].items() if t in active}
    state["polled"] = {w: t for w, t in state["polled"].items() if w in tracked}
    state["followed"] = {w: t for w, t in state["followed"].items() if w in tracked}
    save_state(conn, chain, state, now)
    return out

