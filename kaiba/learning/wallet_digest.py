"""The owner's daily wallet list: A and B tier wallets on every chain, sent with the daily report.

Owner, 2026-10-21: "I need the agent to send me daily wallet gather, A B tier only, solana bnb
robinhood and all chains possible, make it daily with the daily report."

Where the tiers come from: the runner hunt (``deploy/research/runner_hunt/``) collects the
wallets that made money on tokens that really ran (GMGN trending, all-time-high market cap
>= $300k, created in the last 45 days), and ``hightier.py`` grades each candidate on GMGN's own
30-day record with rules declared before any results (2026-10-13):

* screen: realized >= $10k, realized / cost >= 30%, 10-1,500 buys in 30 days;
* every tier: average hold >= 5 min, created no tokens, >= 10 tokens traded, never tagged
  dev / bundler / rat trader / bot / sandwich bot on a runner;
* **S** realized >= $100k, win >= 55%, return >= 50%, >= 5 trades at 3x or more;
* **A** realized >= $25k, win >= 50%, return >= 40%, >= 2 trades at 3x or more;
* **B** realized >= $10k, win >= 45%, return >= 30%.

S is STRICTER than A, so it is listed with A ("A tier, S = the best of A"): the owner asked for
A and B, and leaving out the wallets that clear A by the widest margin would be the opposite of
what he meant. Nothing below C/unscored is ever sent.

What this list is NOT: a claim that copying these wallets pays. GMGN's realized profit is what
the wallet made for itself; copying measured negative at our lag and fees every way it was tried
(memories no-proven-copy-wallets, copy-exit-early-backtest). The confluence-5 paper lane tests
the runner wallets forward, and its scoreboard is in the same daily report.

The message carries per-chain counts, how many are new since the last list, and the top few per
chain; the full lists are attached as files in GMGN's wallet-import format (the same JSON the
exports already use), one per chain, so they can be imported into GMGN directly.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Export label (the runner hunt's file naming) -> the name the owner reads.
CHAINS: tuple[tuple[str, str], ...] = (
    ("solana", "Solana"), ("bnb", "BNB"), ("robinhood", "Robinhood"), ("base", "Base"), ("eth", "Ethereum"),
)
TIERS = ("S", "A", "B")
KV_SEEN = "wallet_digest:seen"
#: Top wallets shown per tier per chain; the attached files carry every one.
TOP_PER_TIER = 3
BEST_PER_LIST = 5
#: Telegram refuses messages over 4,096 characters.
MAX_CHARS = 3_900


@dataclass
class ChainList:
    label: str
    name: str
    path: Path | None
    items: list[dict[str, Any]] = field(default_factory=list)
    new: set[str] = field(default_factory=set)
    age_h: float | None = None

    def tier(self, item: Mapping[str, Any]) -> str | None:
        for g in item.get("groups") or []:
            if str(g).startswith("kaiba-hightier-"):
                t = str(g).rsplit("-", 1)[-1]
                return t if t in TIERS else None
        return None

    def count(self, tier: str) -> int:
        return sum(1 for i in self.items if self.tier(i) == tier)

    def new_count(self, tier: str) -> int:
        return sum(1 for i in self.items if self.tier(i) == tier and i["address"] in self.new)


def load(export_dir: Path, now_s: float) -> list[ChainList]:
    """Every chain's A/B list (S, A, B rows only), oldest-file age noted."""
    out = []
    for label, name in CHAINS:
        path = Path(export_dir) / f"kaiba-{label}-hightier.txt"
        cl = ChainList(label, name, path if path.exists() else None)
        if cl.path is not None:
            try:
                rows = json.loads(cl.path.read_text(encoding="utf-8"))
                cl.age_h = round((now_s - cl.path.stat().st_mtime) / 3600, 1)
            except (OSError, ValueError) as exc:
                log.warning("wallet_digest: cannot read %s (%s)", path.name, exc)
                rows = []
            cl.items = [r for r in rows if isinstance(r, dict) and r.get("address") and cl.tier(r)]
        out.append(cl)
    return out


def mark_new(lists: Sequence[ChainList], seen: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Fill each list's ``new`` set against the previous list; return the set to remember."""
    remember: dict[str, list[str]] = {}
    for cl in lists:
        before = set(seen.get(cl.label) or [])
        now = {i["address"] for i in cl.items}
        cl.new = now - before if before else set()  # the first list ever: nothing is "new"
        remember[cl.label] = sorted(now | before) if not now else sorted(now)
    return remember


_HOLD = re.compile(r"\b(\d+)m\b")


def _label(name: str) -> str:
    """The grader's label with its average hold in readable units ("58678m" -> "41d hold")."""
    def unit(m: re.Match[str]) -> str:
        mins = int(m.group(1))
        if mins >= 1440:
            return f"{mins // 1440}d hold"
        if mins >= 60:
            return f"{mins // 60}h hold"
        return f"{mins}m hold"
    return _HOLD.sub(unit, name, count=1).replace("x3", " at 3x+")


def _short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}" if len(addr) > 12 else addr


def best_lines(board: Mapping[str, Any] | None) -> list[str]:
    """The daily best-wallets block, ranked on what a copier made. Empty until the first list exists."""
    if not board or not board.get("available") or not board.get("top"):
        return []
    good = [w for w in board["top"] if (w.get("mean") or 0) > 0]
    lines = [f"Wallets a copier made money on, Solana, last {board.get('window_days')} days: {len(good)} of {board.get('eligible')} ranked"]
    for w in good[:BEST_PER_LIST]:
        star = "*" if w.get("new") else " "
        lines.append(f"  {star}{w['rank']}. {_short(w['wallet'])}  {w['mean'] * 100:+.1f}% over {w['n']} trades, {w['win'] * 100:.0f}% won")
    proof = (board.get("forward") or {}).get("proof") or {}
    result = f"mean {proof['mean'] * 100:+.1f}%" if isinstance(proof.get("mean"), int | float) else "no result yet"
    lines += [f"Does the list hold up? Top 5 afterwards: {proof.get('n', 0)} of {proof.get('need_n')} trades, {result}. "
              "Not proven: last week's best usually does not stay best.", ""]
    return lines


def render(lists: Sequence[ChainList], day: str, board: Mapping[str, Any] | None = None) -> str:
    """S, A and B ALWAYS shown as three tiers on every chain (owner 2026-10-21: "get b a s tiers
    always"), zero included, with the top wallets of each tier."""
    lines = [f"Kaiba wallet list {day} - S, A and B tier, every chain we gather",
             "S = strictest, then A, then B. Graded on GMGN's 30-day record of wallets that made money on "
             "real runners. Their own profit, not proof that copying them pays: confluence-5 tests that on "
             "paper (see the daily report).", ""]
    lines += best_lines(board)
    any_rows = False
    for cl in lists:
        if cl.path is None:
            lines.append(f"{cl.name}: S 0 | A 0 | B 0  (no graded list yet: the grader has not finished this chain)")
            continue
        counts = " | ".join(
            f"{t} {cl.count(t)}" + (f" (+{cl.new_count(t)} new)" if cl.new and cl.new_count(t) else "")
            for t in TIERS
        )
        stale = f"  [list {cl.age_h:.0f} h old]" if cl.age_h is not None and cl.age_h > 36 else ""
        lines.append(f"{cl.name}: {counts}{stale}")
        for t in TIERS:
            for i in [x for x in cl.items if cl.tier(x) == t][:TOP_PER_TIER]:
                any_rows = True
                star = "*" if i["address"] in cl.new else " "
                lines.append(f"  {star}{_short(i['address'])}  {_label(str(i.get('name') or ''))[:48]}")
    if any_rows:
        lines += ["", "Format: tier, realized profit 30 d, win rate (w), return (r), trades at 3x+, average hold. "
                      "* = new since the last list. Full S/A/B lists attached (GMGN wallet-import files)."]
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 20].rsplit("\n", 1)[0] + "\n... (truncated; full lists attached)"
    return text


def attachments(lists: Sequence[ChainList], day: str, out_dir: Path) -> list[Path]:
    """One GMGN wallet-import file per chain with rows (S/A/B only), written to ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for cl in lists:
        if not cl.items:
            continue
        p = out_dir / f"kaiba-{cl.label}-SAB-wallets-{day}.txt"
        p.write_text(json.dumps(cl.items, ensure_ascii=False, indent=2), encoding="utf-8")
        paths.append(p)
    return paths


def send_document(path: Path, *, token: str, chat_id: str, caption: str = "", timeout_s: float = 30.0) -> bool:
    """POST one file to the owner's chat (Telegram sendDocument). Never raises, never logs the token."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (("chat_id", chat_id), ("caption", caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
        f"Content-Type: text/plain\r\n\r\n".encode() + path.read_bytes() + b"\r\n"
    )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed https host
            return bool(json.loads(resp.read().decode("utf-8", "replace") or "{}").get("ok"))
    except Exception as exc:  # noqa: BLE001 - a dead chat must never stop the agent
        log.warning("wallet list file not delivered: %s", type(exc).__name__)
        return False


def _best_board(export_dir: Path) -> dict[str, Any] | None:
    """The list lives under the same data directory as the exports. Unreadable means no block."""
    from kaiba.learning import labs

    try:
        return labs.best_wallets(Path(export_dir).parents[1])
    except (OSError, ValueError, IndexError) as exc:
        log.warning("best wallets unreadable: %s", exc)
        return None


def run(conn: sqlite3.Connection, export_dir: Path, out_dir: Path, *, now_ms: int) -> dict[str, Any]:
    """Build today's list, remember it, return the text and the files to attach."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
    lists = load(export_dir, now_ms / 1000)
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (KV_SEEN,)).fetchone()
    try:
        seen = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        seen = {}
    remember = mark_new(lists, seen)
    text = render(lists, day, _best_board(export_dir))
    files = attachments(lists, day, out_dir)
    conn.execute("INSERT OR REPLACE INTO kv (key, value, updated_ms) VALUES (?,?,?)",
                 (KV_SEEN, json.dumps(remember), now_ms))
    counts = {cl.label: {"S": cl.count("S"), "A": cl.count("A"), "B": cl.count("B"),
                         "new": len(cl.new), "listed": cl.path is not None} for cl in lists}
    return {"text": text, "files": [str(p) for p in files], "counts": counts}


__all__ = ["CHAINS", "ChainList", "load", "mark_new", "render", "attachments", "send_document", "run"]
