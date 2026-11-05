"""Wallet lists ready to import into GMGN's wallet tracker (2026-10-20).

The owner asked for wallets "immediately, import ready format GMGN". Each list here can be
downloaded as GMGN's follow-list JSON (the exact keys GMGN round-trips, from
:data:`kaiba.intelligence.naming.GMGN_ROW_KEYS`) or as the ``address:name`` text variant, split
into parts of at most :data:`naming.GMGN_CHUNK_SIZE` rows because GMGN silently truncates a
longer import.

Three sources, each labelled with what is actually known about it:

* ``hunt-*``: the wallet-hunt exports (``data/claude-runners/exports``), snipers and early buyers
  of runners, named by the hunt.
* ``graded-<chain>``: wallets the wallet model grades A or B.
* ``tracked-<chain>``: the wallets the agent itself watches for signals (``tracker_watchlist``,
  active).

**No list ever carries an address the owner or the agent owns.** Every row passes
:func:`excluded`: every address written in ``config/signer-policy.yaml`` and ``config/risk.yaml``
(owned addresses, routers, seeds), the wallet hunt's ``excluded.json`` (trusted_copy and
blacklist cohorts, wallets whose source is the owner), and, for rows read from the database, the
row's own ``wallets.cohort``/``wallets.source``. A list whose exclusion set cannot be read at
all is refused rather than served unfiltered.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from dashboard import trading

log = logging.getLogger(__name__)

CHAINS = ("sol", "robinhood", "bsc")
CHAIN_LABEL = {"sol": "Solana", "robinhood": "Robinhood", "bsc": "BNB"}
LIST_ID = re.compile(r"^(hunt|graded|tracked)-[a-z0-9._-]{1,100}$")
_ADDR = re.compile(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}")
EXCLUDED_COHORTS = {"trusted_copy", "blacklist"}

#: What the record says about each kind of list -- shown next to its download button. These are
#: measurements from the research log (memories / docs/research), not marketing.
EVIDENCE = {
    "graded-sol": "Out of sample, Solana B grades did no better than C/D wallets and both trailed random "
                  "wallets (2026-10-02). Use as a watchlist, not as copy signals.",
    "graded-robinhood": "Robinhood B grades held a modest edge out of sample: the one chain where the grade "
                        "has predicted anything.",
    "graded-bsc": "Not yet measured out of sample on BNB.",
    "tracked": "The wallets the agent watches for its signals. Following them on Solana has shown no edge "
               "after costs (signal audit, 2026-10-05).",
    "hunt": "Wallet-hunt export: snipers and early buyers of tokens that ran, named by role and seed token. "
            "Copying them after a lag and fees has not been profitable; they are for watching who is early.",
}

_cache: dict[str, tuple[float, Any]] = {}
LIST_CACHE_S = 900.0


def _cached(key: str, ttl_s: float, build):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < ttl_s:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


def clear_caches() -> None:
    _cache.clear()


def _norm(address: str) -> str:
    return address.lower() if address.startswith("0x") else address


# --------------------------------------------------------------------------------------
# exclusions
# --------------------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _config_addresses() -> set[str] | None:
    """Every address-shaped string in the two config files that name our own wallets."""
    out: set[str] = set()
    read_any = False
    for name in ("signer-policy.yaml", "risk.yaml"):
        path = _repo_root() / "config" / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        read_any = True
        out |= {_norm(a) for a in _ADDR.findall(text)}
    return out if read_any else None


def excluded() -> set[str] | None:
    """The addresses no list may contain (normalised: EVM lower-case, Solana as is), or ``None``
    when neither config file is readable -- the caller then refuses to serve."""

    def build() -> set[str] | None:
        config = _config_addresses()
        if config is None:
            return None
        return config | {_norm(a) for a in trading._excluded_addresses()}

    return _cached("excluded", 300.0, build)


def is_ours(row: dict[str, Any], ban: set[str]) -> bool:
    """A database row that must never be shown or exported: its address is in ``ban``, or the
    wallets table marks it trusted_copy / blacklist / owner-sourced."""
    if _norm(str(row.get("address") or "")) in ban:
        return True
    source = str(row.get("source") or row.get("wsource") or "")
    return str(row.get("cohort") or "") in EXCLUDED_COHORTS or source.startswith("owner")


# --------------------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------------------


def _role(archetype: Any) -> str:
    text = str(archetype or "").strip()
    if not text or text == "unknown":
        return "Wallet"
    try:
        from kaiba.core.schemas import Archetype
        from kaiba.intelligence.naming import role_label

        return role_label(Archetype(text))
    except Exception:  # noqa: BLE001 - a value the enum does not know
        return text.replace("_", " ").title()[:20]


def _name(*parts: Any) -> str:
    """GMGN shows the name in a narrow column: short, no doubled spaces, at most 40 characters."""
    return re.sub(r"\s+", " ", " ".join(str(p) for p in parts if p not in (None, ""))).strip()[:40]


# --------------------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------------------


def _hunt_rows(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("address"), str):
            rows.append({"address": item["address"].strip(), "name": _name(item.get("name") or "Kaiba hunt"),
                         "emoji": str(item.get("emoji") or "")})
        elif isinstance(item, str):
            rows.append({"address": item.strip(), "name": "Kaiba hunt", "emoji": ""})
    return rows


def _graded_rows(chain: str) -> list[dict[str, Any]] | None:
    rows = trading._deadline_rows(
        "SELECT s.address, s.grade, s.score, s.archetype, s.win_rate, s.closed_trades, s.realized_pnl_usd, "
        "w.cohort, w.source FROM wallet_scores s LEFT JOIN wallets w ON w.chain = s.chain AND w.address = s.address "
        "WHERE s.grade IN ('A', 'B') AND s.chain = ? ORDER BY s.grade, s.score DESC LIMIT 6000",
        (chain,), seconds=25.0)
    if rows is None:
        return None
    out = []
    for r in rows:
        if is_ours(r, set()):
            continue
        try:
            score = f"{float(r['score']):.0f}"
        except (TypeError, ValueError):
            score = ""
        out.append({"address": r["address"], "name": _name("Kaiba", _role(r["archetype"]), f"{r['grade']}{score}"),
                    "emoji": "", "grade": r["grade"], "score": r["score"], "win_rate": r["win_rate"],
                    "closed_trades": r["closed_trades"], "realized_pnl_usd": r["realized_pnl_usd"]})
    return out


def _tracked_rows(chain: str) -> list[dict[str, Any]] | None:
    rows = trading._deadline_rows(
        "SELECT t.address, t.tier, t.grade_at_add, t.source, w.cohort, w.source AS wsource FROM tracker_watchlist t "
        "LEFT JOIN wallets w ON w.chain = t.chain AND w.address = t.address "
        "WHERE t.chain = ? AND t.status = 'active' ORDER BY t.tier, t.added_ms DESC LIMIT 6000",
        (chain,), seconds=20.0)
    if rows is None:
        return None
    out = []
    for r in rows:
        if is_ours(r, set()):
            continue
        out.append({"address": r["address"], "name": _name("Kaiba tracked", r["tier"], r["grade_at_add"]),
                    "emoji": "", "tier": r["tier"]})
    return out


def _best_rows() -> list[dict[str, Any]]:
    """Today's reviewed list. Empty until the first list exists, which is not a failed read."""
    from kaiba.learning import labs

    board = labs.best_wallets()
    return [{"address": w["wallet"], "name": _name("Kaiba best", f"#{w['rank']}", w.get("tier")), "emoji": "", "tier": w.get("tier")}
            for w in board.get("top") or [] if (w.get("mean") or 0) > 0] if board.get("available") else []


# --------------------------------------------------------------------------------------
# the lists
# --------------------------------------------------------------------------------------


def _finish(list_id: str, title: str, chain: str | None, kind: str, rows: list[dict[str, Any]] | None,
            source: str, ban: set[str]) -> dict[str, Any]:
    if rows is None:
        return {"id": list_id, "title": title, "chain": chain, "kind": kind, "available": False,
                "reason": "the read timed out; it is retried in 15 minutes", "rows": [], "count": 0, "source": source,
                "evidence": EVIDENCE.get(list_id, EVIDENCE.get(kind, ""))}
    seen: set[str] = set()
    clean = []
    dropped = 0
    for r in rows:
        address = str(r.get("address") or "").strip()
        key = _norm(address)
        if not _ADDR.fullmatch(address) or key in seen:
            continue
        if key in ban:
            dropped += 1
            continue
        seen.add(key)
        clean.append({**r, "address": address})
    from kaiba.intelligence.naming import GMGN_CHUNK_SIZE

    return {"id": list_id, "title": title, "chain": chain, "kind": kind, "available": True, "rows": clean,
            "count": len(clean), "excluded": dropped, "parts": max(1, -(-len(clean) // GMGN_CHUNK_SIZE)),
            "source": source, "evidence": EVIDENCE.get(list_id, EVIDENCE.get(kind, ""))}


def _build_lists() -> dict[str, Any]:
    ban = excluded()
    if ban is None:
        return {"available": False, "reason": "our own addresses could not be read, so no list is served",
                "lists": [], "at_ms": int(time.time() * 1000)}
    out = [_finish("graded-best-sol", "Best wallets, reviewed daily · Solana", "sol", "graded", _best_rows(),
                   "the wallets on the daily list that a copier made money on", ban)]
    for chain in CHAINS:
        out.append(_finish(f"graded-{chain}", f"Graded A/B · {CHAIN_LABEL[chain]}", chain, "graded",
                           _graded_rows(chain), "wallet_scores, grade A or B, best first", ban))
    for chain in CHAINS:
        out.append(_finish(f"tracked-{chain}", f"Agent's watchlist · {CHAIN_LABEL[chain]}", chain, "tracked",
                           _tracked_rows(chain), "tracker_watchlist, active", ban))
    for f in trading.wallet_exports().get("files", []):
        stem = Path(f["name"]).stem.lower()
        list_id = f"hunt-{re.sub(r'[^a-z0-9._-]', '-', stem)}"[:105]
        if not LIST_ID.match(list_id):
            continue
        title = stem.removeprefix("kaiba-").replace("-", " ").strip().capitalize()
        path = trading._runner_dir() / "exports" / f["name"]
        out.append(_finish(list_id, f"Hunt · {title}", f.get("chain"), "hunt", _hunt_rows(path),
                           f"wallet hunt export {f['name']}", ban))
    return {"available": True, "lists": out, "at_ms": int(time.time() * 1000), "excluded_size": len(ban)}


def lists() -> dict[str, Any]:
    """Every list with its rows. Cached 15 minutes: the graded read walks ~2k index entries and
    their table pages, MEASURED 12.8 s cold on the box under idle IO (2026-10-20)."""
    return _cached("lists", LIST_CACHE_S, _build_lists)


def get(list_id: str) -> dict[str, Any] | None:
    if not LIST_ID.match(list_id or ""):
        return None
    return next((x for x in lists().get("lists", []) if x["id"] == list_id and x.get("available")), None)


def part_rows(found: dict[str, Any], part: int) -> list[dict[str, Any]]:
    from kaiba.intelligence.naming import GMGN_CHUNK_SIZE, gmgn_import_rows

    chunk = found["rows"][(part - 1) * GMGN_CHUNK_SIZE: part * GMGN_CHUNK_SIZE]
    rows = gmgn_import_rows([{"address": r["address"], "name": r["name"]} for r in chunk])
    emoji = {r["address"]: r.get("emoji") or "" for r in chunk}
    for r in rows:
        r["emoji"] = emoji.get(r["address"], "")
    return rows


def as_json(found: dict[str, Any], part: int) -> str:
    return json.dumps(part_rows(found, part), indent=2, ensure_ascii=False)


def as_text(found: dict[str, Any], part: int) -> str:
    rows = part_rows(found, part)
    return "\n".join(f"{r['address']}:{r['name']}" for r in rows) + ("\n" if rows else "")


def filename(found: dict[str, Any], part: int, ext: str) -> str:
    stem = f"kaiba-{found['id']}"
    if found["parts"] > 1:
        stem += f"-part{part}of{found['parts']}"
    return f"{stem}.{ext}"


def summary() -> dict[str, Any]:
    """The lists without their rows, plus a five-row preview each, for the page."""
    got = lists()
    out = []
    for x in got.get("lists", []):
        out.append({k: v for k, v in x.items() if k != "rows"} | {"preview": x["rows"][:5]})
    return {**{k: v for k, v in got.items() if k != "lists"}, "lists": out}


def refresh() -> None:
    """Rebuild the lists and swap them in whole (the dashboard's warm-up thread calls this ahead of
    the TTL), so a page view never waits on the cold read and never sees a half-built cache."""
    try:
        value = _build_lists()
    except Exception:  # noqa: BLE001 - best effort, the page builds on demand
        log.info("wallet lists refresh failed", exc_info=True)
        return
    _cache["lists"] = (time.monotonic(), value)
