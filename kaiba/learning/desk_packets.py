"""Evidence packets for the analyst desk: what Kaiba holds about one subject, and what it does not.

The desk has no authority (see :mod:`kaiba.learning.desk`). This module only reads. Every read is one of:

* the live database through :class:`kaiba.learning.autolearn_io.GuardedReader` (WAL guard first, one
  short read-only connection per query, a 3 s deadline, a row cap);
* files already on disk: the market-intel caches, the perps paper reports, the DLMM lab.

It makes no RPC, GMGN, provider or other network call and imports no transport.

**The one door.** Every value reaches a packet through :meth:`Evidence.fact`. A value that is absent,
carries no clock, is older than its budget, or sits behind a deferred read lands in ``unknowns`` with
its reason, never in ``facts``. A missing input is never a zero (the silent-idle pattern: "no rows"
reads as "quiet market" and nobody investigates).

**Wallets stay out.** ``tokens.creator`` is read only to look up the deployer's aggregate record and is
never put in a packet, and no wallet column is selected anywhere. Third-party text (token names, news
titles, airdrop names) has anything address-shaped masked and is listed in ``untrusted_keys``: it is
data, never instructions.

Query shapes are MEASURED on the box, 2026-11-04. ``decisions`` has no token index, so packets read a
bounded recent window; aggregates filtered on ``ts_ms`` walked ``idx_decisions_lane`` and hit the
deadline, while ``ORDER BY ts_ms DESC LIMIT n`` answered in 0.01 s.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kaiba.learning import labs
from kaiba.learning.autolearn_io import GuardedReader, SourceDeferred, load_json

DOMAINS = ("token", "perps", "lp", "nft", "airdrop")
AUTHORITY = "none: nothing here reaches sizing, admission, lanes, risk, the executor or a wallet"

H_MS = 3_600_000
MAX_INTEL_BYTES = 2 * 1024 * 1024
#: Rows per ``decisions`` window: 54 rows/h MEASURED 2026-11-04, so about 37 h.
DECISION_WINDOW = 2000
#: The dossier's own budgets exist for an entry that fills in seconds. A forecast over a day can read an
#: hour-old liquidity number; past that it is unknown, whatever the measure's own budget says.
DOSSIER_MIN_BUDGET_S = 3600
DOSSIER_BUDGET_S = 6 * 3600
DEPLOYER_BUDGET_S = 6 * 3600   # kaiba.intelligence.deployer.STALE_AFTER_S is 3600, for live entries
TAPE_BUDGET_S = 3600
SIGNAL_BUDGET_S = 6 * 3600
#: kaiba.market_intel collector intervals, with room for one missed cycle. Binance positioning is
#: published about two days late (the file says lag_days=2); ETF flows are daily.
INTEL_BUDGET_S = {"tickers": 900, "hl_market": 900, "crypto_macro": 7200, "binance_positioning": 4 * 86_400,
                  "etf_flows": 36 * 3600, "calendar": 6 * 3600, "news": 1800}
PAPER_BUDGET_S = 36 * 3600     # kaiba-perps-paper.timer and kaiba-perps-xs.timer run daily
HUNTER_BUDGET_S = 6 * 3600
OPPORTUNITY_BUDGET_S = 48 * 3600
NFT_MAX_AGE_S = 6 * 3600       # a mark's window opens 18 h after the decision; see desk_outcomes
USABLE_BASIS = {"verified_onchain", "provider_reported", "derived", "cached"}

MEASURES = ("price_usd", "liquidity_usd", "market_cap_usd", "volume_24h_usd", "holder_count", "top10_pct",
            "dev_pct", "insider_pct", "bundler_pct", "sniper_pct", "cluster_pct", "buy_tax_bps",
            "sell_tax_bps", "rug_ratio", "lp_burned_pct")
NFT_FEATURES = ("max_supply", "total_supply", "fills_24h", "offers_24h", "listings_24h", "buyers_24h",
                "sellers_24h", "mints_1h", "mints_24h", "minters_24h", "qty_24h", "fills_6h")
LP_FIELDS = ("positions", "lps", "lp_minus_hodl", "in_range", "open", "open_in_range", "per_sol_day", "tvl",
             "fee_24h", "fee_tvl_24h")

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}")
SUBJECT = {"token": re.compile(r"(sol|bsc|robinhood|eth|base):[0-9A-Za-z]{20,64}\Z"),
           "perps": re.compile(r"[A-Z0-9]{2,16}USDT\Z"),
           "lp": re.compile(r"[0-9A-Za-z]{20,64}\Z"),
           "nft": re.compile(r"paper:[0-9]{1,12}\Z"),
           "airdrop": re.compile(r"[0-9A-Za-z_:.\-]{1,128}\Z")}
WARNINGS = {
    "token": ["Conviction score, smart-wallet count and dossier grade are ANTI-CALIBRATED on 191 live fills: "
              "a high value here is not evidence of a better outcome (AGENTS.md, owner directive 2026-09-24).",
              "Live record: mean -14.2% per trade, 25% wins (same source). A long needs more than a good story."],
    "perps": ["No perps lane trades. The frozen D1 and cross-sectional books are forward paper; archive results "
              "are not proof (docs/research/claude-perps-d1-forward-prereg-20261103.md, "
              "docs/research/claude-xsmom-results-20261103.md)."],
    "lp": ["The panel was picked by fees on 2026-10-28. Pools sampled by creation date lost -0.29% per SOL-day "
           "against holding SOL (docs/research/claude-dlmm-birth-pools-20261028.md).",
           "Kaiba holds no LP position. Bin-array rent and tx fees are not in these numbers."],
    "nft": ["Mint-and-flip failed on every chain measured; 0-20% of mints could be sold into a real buyer within "
            "24 h (docs/research/claude-airdrops-nfts-20261021.md)."],
    "airdrop": ["Of 24 programmes, 22 paid the conservative profile nothing at LOW valuation and 14 at the other "
                "valuation (docs/research/claude-airdrops-nfts-20261021.md).",
                "Collector costs are estimates. A named programme is not proof of eligibility."],
}


def num(value: Any) -> float | None:
    """A finite float, or None. ``None`` stays ``None``: it never becomes 0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def clean(value: Any, limit: int = 160) -> str:
    """Third-party text: printable, bounded, and with anything address-shaped masked."""
    text = "".join(c for c in str(value) if c.isprintable())
    return _ADDRESS.sub("[address]", text)[:limit]


def _strings(value: Any, *, limit: int = 100, keep: int = 6) -> list[str] | None:
    """A list a source really wrote. Not a list is unknown (None); an empty list is a recorded absence."""
    if not isinstance(value, list):
        return None
    return [clean(x, limit) for x in value][:keep] or ["none_recorded"]


class Evidence:
    """The one door into a packet."""

    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms
        self.facts: dict[str, dict[str, Any]] = {}
        self.unknowns: list[dict[str, str]] = []
        self.untrusted: list[str] = []

    def unknown(self, key: str, reason: str, source: str) -> None:
        self.facts.pop(key, None)
        self.unknowns.append({"key": key, "reason": reason, "source": source})

    def fact(self, key: str, value: Any, *, source: str, as_of_ms: Any, budget_s: float | None) -> bool:
        """Admit a value, or record why not. ``budget_s=None`` means a fixed identity, which cannot age."""
        if value is None or value == "" or value == []:
            self.unknown(key, "missing", source)
            return False
        as_of = None if as_of_ms is None else num(as_of_ms)
        if as_of is None:
            self.unknown(key, "no_clock", source)
            return False
        age_s = (self.now_ms - as_of) / 1000
        if age_s < -60:
            self.unknown(key, f"future_clock:{int(age_s)}s", source)
            return False
        if budget_s is not None and age_s > budget_s:
            self.unknown(key, f"stale:{int(age_s)}s>{int(budget_s)}s", source)
            return False
        self.facts[key] = {"value": value, "source": source, "age_s": max(0, int(age_s)), "as_of_ms": int(as_of)}
        return True

    def text(self, key: str, value: Any, *, source: str, as_of_ms: Any, budget_s: float | None) -> bool:
        """Third-party text: masked, and declared untrusted for the model."""
        admitted = self.fact(key, None if value is None else clean(value), source=source, as_of_ms=as_of_ms,
                             budget_s=budget_s)
        if admitted:
            self.untrusted.append(key)
        return admitted


@dataclass
class Sources:
    """Where this run reads from, and its clock. The reader never holds a connection between queries."""

    data_dir: Path
    db_path: Path
    now_ms: int
    reader: GuardedReader = field(init=False)

    def __post_init__(self) -> None:
        self.reader = GuardedReader(self.db_path, query_seconds=3, max_rows=DECISION_WINDOW + 3000)


def sources(now_ms: int | None = None, data_dir: Path | None = None, db_path: Path | None = None) -> Sources:
    from kaiba.core.config import get_settings

    settings = get_settings()
    return Sources(Path(data_dir or settings.kaiba_data_dir), Path(db_path or settings.db_path),
                   int(time.time() * 1000) if now_ms is None else int(now_ms))


def _json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_INTEL_BYTES:
            return None
        out = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def intel(src: Sources, key: str) -> dict[str, Any] | None:
    """One market-intel cache document, or None. Reads the file; never runs a collector."""
    return _json(src.data_dir / "market-intel" / "intel" / f"{key}.json")


def _intel_data(e: Evidence, src: Sources, key: str, prefix: str) -> tuple[dict[str, Any] | None, int | None]:
    doc, source = intel(src, key), f"market-intel/{key}"
    if doc is None:
        e.unknown(prefix, "missing", source)
        return None, None
    if not doc.get("ok") or doc.get("stale"):
        e.unknown(prefix, "source_failed_or_marked_stale", source)
        return None, None
    data, ts = doc.get("data"), num(doc.get("fetched_ts"))
    if not isinstance(data, dict) or ts is None:
        e.unknown(prefix, "unreadable", source)
        return None, None
    age_s = (src.now_ms - ts * 1000) / 1000
    if age_s > INTEL_BUDGET_S[key]:
        e.unknown(prefix, f"stale:{int(age_s)}s>{INTEL_BUDGET_S[key]}s", source)
        return None, None
    return data, int(ts * 1000)


def _parsed(raw: Any) -> Any:
    """JSON from a column, or None when unreadable. ``load_json`` would turn that into an empty container."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _iso_ms(value: Any) -> int | None:
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp() * 1000)


# --------------------------------------------------------------------------------------
# token
# --------------------------------------------------------------------------------------


def _measure(e: Evidence, key: str, raw: Any, built_ms: int) -> None:
    """A dossier measure. An unavailable, stale or estimated basis is never upgraded by its retained value."""
    if not isinstance(raw, dict):
        e.unknown(key, "missing", "token_dossiers")
        return
    if raw.get("basis") not in USABLE_BASIS:
        e.unknown(key, f"basis:{raw.get('basis')}", "token_dossiers")
        return
    receipt = raw.get("receipt") if isinstance(raw.get("receipt"), dict) else {}
    budget = max(num(raw.get("freshness_budget_s")) or 0, DOSSIER_MIN_BUDGET_S)
    e.fact(key, num(raw.get("value")), source="token_dossiers", as_of_ms=receipt.get("observed_at_ms", built_ms),
           budget_s=budget)


def _token_row(e: Evidence, src: Sources, chain: str, token: str) -> None:
    rows = src.reader.rows("SELECT symbol,launchpad,created_ms,migrated_ms,first_seen_ms FROM tokens "
                           "WHERE chain=? AND address=?", (chain, token))
    if not rows:
        e.unknown("token.row", "not_in_tokens_table", "tokens")
        return
    row = rows[0]
    seen = row["first_seen_ms"]
    e.text("token.symbol", row["symbol"], source="tokens", as_of_ms=seen, budget_s=None)
    e.fact("token.launchpad", row["launchpad"], source="tokens", as_of_ms=seen, budget_s=None)
    created = num(row["created_ms"])
    e.fact("token.age_s", None if created is None else int((src.now_ms - created) / 1000), source="tokens",
           as_of_ms=src.now_ms, budget_s=None)
    # The feed stamps migrated_ms; False is "no graduation recorded as of this read".
    e.fact("token.graduated", row["migrated_ms"] is not None, source="tokens", as_of_ms=src.now_ms, budget_s=None)


def _dossier(e: Evidence, src: Sources, chain: str, token: str) -> None:
    rows = src.reader.rows("SELECT built_at_ms,score,grade,dossier_json FROM token_dossiers WHERE chain=? AND address=?",
                           (chain, token))
    if not rows:
        e.unknown("dossier", "no_dossier_row", "token_dossiers")
        return
    row, built = rows[0], rows[0]["built_at_ms"]
    d = load_json(row["dossier_json"], {})
    for name in MEASURES:
        _measure(e, f"dossier.{name}", d.get(name), built)
    for name in ("mint_authority_revoked", "freeze_authority_revoked", "can_sell"):
        e.fact(f"dossier.{name}", d.get(name) if isinstance(d.get(name), bool) else None, source="token_dossiers",
               as_of_ms=built, budget_s=DOSSIER_BUDGET_S)
    for name in ("blockers", "warnings"):
        e.fact(f"dossier.{name}", _strings(d.get(name)), source="token_dossiers", as_of_ms=built,
               budget_s=DOSSIER_BUDGET_S)
    e.fact("dossier.grade", row["grade"], source="token_dossiers", as_of_ms=built, budget_s=DOSSIER_BUDGET_S)
    e.fact("dossier.score", num(row["score"]), source="token_dossiers", as_of_ms=built, budget_s=DOSSIER_BUDGET_S)
    graded = d.get("graded_wallets")
    e.fact("dossier.graded_wallet_count", len(graded) if isinstance(graded, list) else None, source="token_dossiers",
           as_of_ms=built, budget_s=DOSSIER_BUDGET_S)


def _signals(e: Evidence, src: Sources, chain: str, token: str) -> None:
    rows = src.reader.rows("SELECT lane,strength,created_ms,json_array_length(wallets_json) AS wallets FROM signals "
                           "WHERE chain=? AND token=? ORDER BY created_ms DESC LIMIT 20", (chain, token))
    if not rows:
        e.unknown("signals", "no_signal_row", "signals")
        return
    latest = rows[0]
    at = latest["created_ms"]
    e.fact("signals.recent_rows", len(rows), source="signals", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)
    e.fact("signals.latest_lane", latest["lane"], source="signals", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)
    e.fact("signals.latest_strength", num(latest["strength"]), source="signals", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)
    e.fact("signals.latest_wallet_count", latest["wallets"], source="signals", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)


def _decisions(e: Evidence, src: Sources, chain: str, token: str) -> str | None:
    rows = src.reader.rows(f"SELECT ts_ms,lane,mode,action,blockers_json,features_json FROM (SELECT * FROM decisions "
                           f"ORDER BY ts_ms DESC LIMIT {DECISION_WINDOW}) WHERE chain=? AND token=? "
                           "ORDER BY ts_ms DESC LIMIT 5", (chain, token))
    if not rows:
        e.unknown("decision", "no_decision_in_recent_window", "decisions")
        return None
    row, at = rows[0], rows[0]["ts_ms"]
    for name in ("lane", "mode", "action"):
        e.fact(f"decision.{name}", row[name], source="decisions", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)
    e.fact("decision.blockers", _strings(_parsed(row["blockers_json"])), source="decisions", as_of_ms=at,
           budget_s=SIGNAL_BUDGET_S)
    # Read at the decision (migration 048), so no later dossier rebuild can leak into it.
    features = load_json(row["features_json"], {})
    for name in ("top10_pct", "liquidity_usd", "holder_count", "market_cap_usd"):
        e.fact(f"decision.features.{name}", num(features.get(name)) if isinstance(features, dict) else None,
               source="decisions.features_json", as_of_ms=at, budget_s=SIGNAL_BUDGET_S)
    return str(row["lane"])


def _deployer(e: Evidence, src: Sources, chain: str, token: str) -> None:
    rows = src.reader.q("SELECT creator FROM tokens WHERE chain=? AND address=?", (chain, token))
    if not rows or not rows[0][0]:
        e.unknown("deployer", "no_creator_recorded", "tokens")
        return
    stats = src.reader.rows("SELECT launches,scored,runners,best_multiple,computed_ms FROM deployer_stats "
                            "WHERE chain=? AND wallet=?", (chain, rows[0][0]))
    if not stats:
        e.unknown("deployer", "deployer_not_in_stats", "deployer_stats")
        return
    row = stats[0]
    for name in ("launches", "scored", "runners"):
        e.fact(f"deployer.{name}", row[name], source="deployer_stats", as_of_ms=row["computed_ms"],
               budget_s=DEPLOYER_BUDGET_S)
    e.fact("deployer.best_multiple", num(row["best_multiple"]), source="deployer_stats", as_of_ms=row["computed_ms"],
           budget_s=DEPLOYER_BUDGET_S)


def _tape(e: Evidence, src: Sources, chain: str, token: str) -> None:
    """The newest print. ``swaps`` mixes price scales across sources, so its source travels with the price."""
    rows = src.reader.rows("SELECT ts_ms,price_usd,source,side FROM swaps WHERE chain=? AND token=? "
                           "ORDER BY ts_ms DESC LIMIT 300", (chain, token))
    priced = [r for r in rows if (num(r["price_usd"]) or 0) > 0]
    if not priced:
        e.unknown("tape", "no_priced_print", "swaps")
        return
    last, at = priced[0], priced[0]["ts_ms"]
    if e.fact("tape.last_price_usd", num(last["price_usd"]), source="swaps", as_of_ms=at, budget_s=TAPE_BUDGET_S):
        e.fact("tape.price_source", last["source"], source="swaps", as_of_ms=at, budget_s=TAPE_BUDGET_S)
    hour = [r for r in rows if src.now_ms - r["ts_ms"] <= H_MS]
    e.fact("tape.prints_1h", len(hour), source="swaps", as_of_ms=at, budget_s=TAPE_BUDGET_S)
    e.fact("tape.buys_1h", sum(1 for r in hour if r["side"] == "buy"), source="swaps", as_of_ms=at, budget_s=TAPE_BUDGET_S)


def _lane_record(e: Evidence, src: Sources, lane: str) -> None:
    rows = src.reader.q("SELECT CAST(realized_native AS REAL)/NULLIF(CAST(cost_native AS REAL),0) FROM positions "
                        "WHERE lane=? AND closed_ms IS NOT NULL AND opened_ms>?", (lane, src.now_ms - 7 * 24 * H_MS))
    values = sorted(r[0] for r in rows if r[0] is not None)
    if not values:
        e.unknown("lane.record_7d", "no_closed_position", "positions")
        return
    at, source = src.now_ms, "positions"
    e.fact("lane.record_7d.n", len(values), source=source, as_of_ms=at, budget_s=None)
    e.fact("lane.record_7d.win_share", round(sum(1 for v in values if v > 0) / len(values), 4), source=source,
           as_of_ms=at, budget_s=None)
    # The median: one bogus mark flips a paper mean (docs: suspect-jump review, 2026-11-02).
    e.fact("lane.record_7d.median_return_pct", round(100 * values[len(values) // 2], 2), source=source, as_of_ms=at,
           budget_s=None)


def _section(e: Evidence, name: str, source: str, read: Any, *args: Any) -> Any:
    """Run one read. A deferred read is an unknown section, so a busy database never looks like a quiet market."""
    try:
        return read(e, *args)
    except SourceDeferred as exc:
        e.unknown(name, f"deferred:{exc}", source)
        return None


def _evidence_token(e: Evidence, src: Sources, subject: str) -> None:
    chain, token = subject.split(":", 1)
    _section(e, "token.row", "tokens", _token_row, src, chain, token)
    _section(e, "dossier", "token_dossiers", _dossier, src, chain, token)
    _section(e, "signals", "signals", _signals, src, chain, token)
    lane = _section(e, "decision", "decisions", _decisions, src, chain, token)
    _section(e, "deployer", "deployer_stats", _deployer, src, chain, token)
    _section(e, "tape", "swaps", _tape, src, chain, token)
    if lane:
        _section(e, "lane.record_7d", "positions", _lane_record, src, lane)


# --------------------------------------------------------------------------------------
# perps
# --------------------------------------------------------------------------------------


def _row_for(rows: Any, key: str, value: str) -> dict[str, Any] | None:
    return next((r for r in rows or [] if isinstance(r, dict) and r.get(key) == value), None)


def _perps_market(e: Evidence, src: Sources, symbol: str, base: str) -> None:
    data, at = _intel_data(e, src, "tickers", "ticker")
    if data is not None:
        row = _row_for(data.get("tickers"), "symbol", symbol)
        if row is None:
            e.unknown("ticker", "symbol_not_in_tickers", "market-intel/tickers")
        else:
            for key, name in (("price", "price"), ("change_24h_pct", "change_pct"), ("high_24h", "high"),
                              ("low_24h", "low"), ("quote_volume", "quote_volume")):
                e.fact(f"ticker.{key}", num(row.get(name)), source="market-intel/tickers", as_of_ms=at,
                       budget_s=INTEL_BUDGET_S["tickers"])
    data, at = _intel_data(e, src, "hl_market", "hl")
    if data is not None:
        row = _row_for(data.get("rows"), "coin", base)
        if row is None:
            e.unknown("hl", "coin_not_in_top_rows", "market-intel/hl_market")
        else:
            for name in ("funding_ann_pct", "oi_usd", "premium", "change_24h_pct"):
                e.fact(f"hl.{name}", num(row.get(name)), source="market-intel/hl_market", as_of_ms=at,
                       budget_s=INTEL_BUDGET_S["hl_market"])
    data, at = _intel_data(e, src, "binance_positioning", "positioning")
    if data is not None:
        block = (data.get("symbols") or {}).get(symbol)
        latest = block.get("latest") if isinstance(block, dict) else None
        if not isinstance(latest, dict):
            e.unknown("positioning", "symbol_not_covered", "market-intel/binance_positioning")
        else:
            clock = _iso_ms(latest.get("time"))
            for name in ("top_position_ls", "top_account_ls", "taker_buy_sell", "oi_usd"):
                e.fact(f"positioning.{name}", num(latest.get(name)), source="market-intel/binance_positioning",
                       as_of_ms=clock, budget_s=INTEL_BUDGET_S["binance_positioning"])


def _perps_macro(e: Evidence, src: Sources, base: str) -> None:
    data, at = _intel_data(e, src, "crypto_macro", "macro")
    if data is not None:
        budget, source = INTEL_BUDGET_S["crypto_macro"], "market-intel/crypto_macro"
        block = data.get("global") or {}
        e.fact("macro.btc_dominance", num(block.get("btc_dominance")), source=source, as_of_ms=at, budget_s=budget)
        e.fact("macro.mcap_change_24h_pct", num(block.get("mcap_change_24h_pct")), source=source, as_of_ms=at,
               budget_s=budget)
        e.fact("macro.fear_greed", num((data.get("fear_greed") or {}).get("value")), source=source, as_of_ms=at,
               budget_s=budget)
        e.fact("macro.stablecoin_chg_7d_pct", num((data.get("stablecoins") or {}).get("chg_7d_pct")), source=source,
               as_of_ms=at, budget_s=budget)
    data, at = _intel_data(e, src, "etf_flows", "etf")
    if data is not None:
        block = (data.get("assets") or {}).get(base)
        if not isinstance(block, dict) or not block.get("ok"):
            e.unknown("etf", "asset_not_covered_or_failed", "market-intel/etf_flows")
        else:
            for name in ("sum_5d", "sum_20d"):
                e.fact(f"etf.{name}", num(block.get(name)), source="market-intel/etf_flows", as_of_ms=at,
                       budget_s=INTEL_BUDGET_S["etf_flows"])
            e.text("etf.verdict", block.get("verdict"), source="market-intel/etf_flows", as_of_ms=at,
                   budget_s=INTEL_BUDGET_S["etf_flows"])
    data, at = _intel_data(e, src, "calendar", "calendar")
    if data is not None:
        soon = [x for x in data.get("events") or [] if isinstance(x, dict) and x.get("impact") == "High"
                and 0 < (num(x.get("ts")) or 0) * 1000 - src.now_ms <= 72 * H_MS]
        soon.sort(key=lambda x: x["ts"])
        budget, source = INTEL_BUDGET_S["calendar"], "market-intel/calendar"
        e.fact("calendar.high_impact_72h", len(soon) if data.get("events") else None, source=source, as_of_ms=at,
               budget_s=budget)
        if soon:
            e.text("calendar.next_high_impact", soon[0].get("title"), source=source, as_of_ms=at, budget_s=budget)
    data, at = _intel_data(e, src, "news", "news")
    if data is not None:
        budget, source = INTEL_BUDGET_S["news"], "market-intel/news"
        e.fact("news.high_24h", data.get("high_24h"), source=source, as_of_ms=at, budget_s=budget)
        word = re.compile(rf"\b{re.escape(base)}\b", re.IGNORECASE)
        titles = [x.get("title") for x in data.get("items") or [] if isinstance(x, dict) and x.get("high")
                  and word.search(str(x.get("title")))][:3]
        if titles:
            e.text("news.headlines", " | ".join(str(t) for t in titles), source=source, as_of_ms=at, budget_s=budget)


def _perps_paper(e: Evidence, src: Sources, symbol: str) -> None:
    source = "perps/report.json"
    report = _json(src.data_dir / "perps" / "report.json")
    if report is None:
        e.unknown("paper.d1", "missing", source)
    else:
        at = report.get("as_of_ms")
        verdict = report.get("verdict") if isinstance(report.get("verdict"), dict) else {}
        e.fact("paper.d1.verdict", verdict.get("verdict"), source=source, as_of_ms=at, budget_s=PAPER_BUDGET_S)
        e.fact("paper.d1.closed_n", verdict.get("n"), source=source, as_of_ms=at, budget_s=PAPER_BUDGET_S)
        block = (report.get("symbols") or {}).get(symbol)
        if not isinstance(block, dict):
            e.unknown("paper.d1.symbol", "symbol_not_in_frozen_universe", source)
        else:
            row = _row_for(report.get("open_positions"), "symbol", symbol)
            e.fact("paper.d1.symbol_status", block.get("status"), source=source, as_of_ms=at, budget_s=PAPER_BUDGET_S)
            e.fact("paper.d1.open_side", row.get("side") if row else "flat", source=source, as_of_ms=at,
                   budget_s=PAPER_BUDGET_S)
    source = "perps/xs/report.json"
    report = _json(src.data_dir / "perps" / "xs" / "report.json")
    if report is None:
        e.unknown("paper.xs", "missing", source)
    else:
        e.fact("paper.xs.verdict", report.get("verdict"), source=source, as_of_ms=report.get("as_of_ms"),
               budget_s=PAPER_BUDGET_S)


def _evidence_perps(e: Evidence, src: Sources, subject: str) -> None:
    base = subject.removesuffix("USDT")
    _perps_market(e, src, subject, base)
    _perps_macro(e, src, base)
    _perps_paper(e, src, subject)


# --------------------------------------------------------------------------------------
# lp, nft, airdrop
# --------------------------------------------------------------------------------------


def _evidence_lp(e: Evidence, src: Sources, subject: str) -> None:
    source = "labs/lp/result.json"
    lab = labs.lp_lab(src.data_dir)
    if not lab.get("available"):
        e.unknown("lab", "missing", source)
        return
    at = None if lab.get("at") is None else int(lab["at"]) * 1000
    budget = labs.STALE_S
    e.fact("lab.vs_sol_mean", num((lab.get("vs_sol") or {}).get("mean")), source=source, as_of_ms=at, budget_s=budget)
    e.fact("lab.vs_holding_mean", num((lab.get("vs_holding_token") or {}).get("mean")), source=source, as_of_ms=at,
           budget_s=budget)
    e.fact("lab.fees_cover_share", num(lab.get("fees_cover_share")), source=source, as_of_ms=at, budget_s=budget)
    forward = lab.get("forward") or {}
    e.fact("lab.forward_n", forward.get("n"), source=source, as_of_ms=at, budget_s=budget)
    e.fact("lab.forward_per_sol_day", num(forward.get("per_sol_day")), source=source, as_of_ms=at, budget_s=budget)
    row = _row_for(lab.get("pools_table"), "address", subject)
    if row is None:
        e.unknown("pool", "not_in_panel", source)
        return
    e.text("pool.name", row.get("name"), source=source, as_of_ms=at, budget_s=budget)
    e.fact("pool.quote", row.get("quote"), source=source, as_of_ms=at, budget_s=budget)
    e.fact("pool.gone", bool(row.get("gone")), source=source, as_of_ms=at, budget_s=budget)
    e.fact("pool.blacklisted", bool(row.get("blacklisted")), source=source, as_of_ms=at, budget_s=budget)
    for name in LP_FIELDS:
        e.fact(f"pool.{name}", num(row.get(name)), source=source, as_of_ms=at, budget_s=budget)


def _hunters(e: Evidence, src: Sources, kind: str, names: list[str] | None = None) -> None:
    rows = src.reader.rows("SELECT name,last_ok_ms,last_count,fail_streak FROM hunter_sources WHERE kind=?", (kind,))
    for row in rows:
        if names is None or row["name"] in names:
            e.fact(f"hunter.{row['name']}.last_count", row["last_count"], source="hunter_sources",
                   as_of_ms=row["last_ok_ms"], budget_s=HUNTER_BUDGET_S)


def _nft_row(e: Evidence, src: Sources, paper_id: int) -> None:
    source = "nft_paper_mints"
    rows = src.reader.rows("SELECT verdict,decided_ms,rule_version,failed_json,features_json,label,mint_price_wei,"
                           "cost_wei FROM nft_paper_mints WHERE paper_id=?", (paper_id,))
    if not rows:
        e.unknown("nft.row", "no_paper_row", source)
        return
    row, at = rows[0], rows[0]["decided_ms"]
    if not e.fact("nft.verdict", row["verdict"], source=source, as_of_ms=at, budget_s=NFT_MAX_AGE_S):
        return
    e.text("nft.label", row["label"], source=source, as_of_ms=at, budget_s=None)
    e.fact("nft.rule_version", row["rule_version"], source=source, as_of_ms=at, budget_s=None)
    e.fact("nft.rule_failed", _strings(_parsed(row["failed_json"])), source=source, as_of_ms=at, budget_s=None)
    for name in ("mint_price_wei", "cost_wei"):
        e.fact(f"nft.{name.removesuffix('_wei')}_eth", None if num(row[name]) is None else num(row[name]) / 1e18,
               source=source, as_of_ms=at, budget_s=None)
    features = load_json(row["features_json"], {})
    for name in NFT_FEATURES:    # an allow-list: the row also holds fee-recipient and validator addresses
        e.fact(f"nft.feature.{name}", features.get(name) if isinstance(features.get(name), int) else None,
               source=source, as_of_ms=at, budget_s=None)


def _nft_base_rates(e: Evidence, src: Sources, paper_id: int) -> None:
    source = "nft_paper_marks"
    rows = src.reader.q("SELECT m.exit_basis!='none',CAST(m.pnl_wei AS REAL)>0,COUNT(*) FROM nft_paper_marks m "
                        "JOIN nft_paper_mints p USING(paper_id) WHERE m.horizon_h=24 AND m.paper_id BETWEEN ? AND ? "
                        "GROUP BY 1,2", (paper_id - 3000, paper_id))
    total = sum(r[2] for r in rows)
    if not total:
        e.unknown("nft.base", "no_scored_paper_rows", source)
        return
    at = src.now_ms
    e.fact("nft.base.n", total, source=source, as_of_ms=at, budget_s=None)
    e.fact("nft.base.real_exit_share_24h", round(sum(r[2] for r in rows if r[0]) / total, 4), source=source, as_of_ms=at,
           budget_s=None)
    e.fact("nft.base.profit_share_24h", round(sum(r[2] for r in rows if r[1]) / total, 4), source=source, as_of_ms=at,
           budget_s=None)


def _evidence_nft(e: Evidence, src: Sources, subject: str) -> None:
    paper_id = int(subject.split(":", 1)[1])
    _section(e, "nft.row", "nft_paper_mints", _nft_row, src, paper_id)
    _section(e, "nft.base", "nft_paper_marks", _nft_base_rates, src, paper_id)
    _section(e, "hunter", "hunter_sources", _hunters, src, "nft_mint")


def _opportunity(e: Evidence, src: Sources, subject: str) -> list[str]:
    source = "opportunities"
    rows = src.reader.rows("SELECT kind,name,chain,status,ev_score,cost_usd,gross_usd,deadline_ms,evidence_json,"
                           "warnings_json,source,symbol,confidence,last_seen_ms FROM opportunities WHERE opportunity_id=?",
                           (subject,))
    if not rows:
        e.unknown("opp.row", "no_opportunity_row", source)
        return []
    row, at = rows[0], rows[0]["last_seen_ms"]
    if not e.fact("opp.last_seen_ms", at, source=source, as_of_ms=at, budget_s=OPPORTUNITY_BUDGET_S):
        return []
    budget = OPPORTUNITY_BUDGET_S
    e.text("opp.name", row["name"], source=source, as_of_ms=at, budget_s=budget)
    e.text("opp.symbol", row["symbol"], source=source, as_of_ms=at, budget_s=budget)
    for name in ("kind", "chain", "status", "source"):
        e.fact(f"opp.{name}", row[name], source=source, as_of_ms=at, budget_s=budget)
    for name in ("ev_score", "cost_usd", "gross_usd", "confidence", "deadline_ms"):
        e.fact(f"opp.{name}", num(row[name]), source=source, as_of_ms=at, budget_s=budget)
    ev = load_json(row["evidence_json"], {})
    ev = ev if isinstance(ev, dict) else {}
    for name in ("confirmed_token", "points_program"):
        e.fact(f"opp.{name}", ev.get(name) if isinstance(ev.get(name), bool) else None, source=source, as_of_ms=at,
               budget_s=budget)
    e.fact("opp.sybil_risk", ev.get("sybil_risk"), source=source, as_of_ms=at, budget_s=budget)
    e.fact("opp.source_count", ev.get("source_count"), source=source, as_of_ms=at, budget_s=budget)
    for name in ("time_cost_hours", "gas_cost_usd", "capital_required_usd"):   # collector estimates, not measurements
        e.fact(f"opp.estimate.{name}", num(ev.get(name)), source=source, as_of_ms=at, budget_s=budget)
    e.fact("opp.warnings", _strings(_parsed(row["warnings_json"])), source=source, as_of_ms=at, budget_s=budget)
    return [x.strip() for x in str(row["source"] or "").split(",") if x.strip()]


def _evidence_airdrop(e: Evidence, src: Sources, subject: str) -> None:
    names = _section(e, "opp.row", "opportunities", _opportunity, src, subject) or []
    if names:
        _section(e, "hunter", "hunter_sources", _hunters, src, "airdrop", names)


READERS = {"token": _evidence_token, "perps": _evidence_perps, "lp": _evidence_lp, "nft": _evidence_nft,
           "airdrop": _evidence_airdrop}


def evidence(domain: str, subject: str, src: Sources) -> Evidence:
    e = Evidence(src.now_ms)
    READERS[domain](e, src, subject)
    return e


# --------------------------------------------------------------------------------------
# candidates: what is live now. An unreadable source raises, so empty always means empty.
# --------------------------------------------------------------------------------------


def _candidates_token(src: Sources) -> list[dict[str, str]]:
    rows = src.reader.rows("SELECT ts_ms,lane,mode,chain,token FROM (SELECT * FROM decisions ORDER BY ts_ms DESC "
                           f"LIMIT {DECISION_WINDOW}) WHERE action='enter' AND ts_ms>? ORDER BY ts_ms DESC LIMIT 60",
                           (src.now_ms - 2 * H_MS,))
    seen: set[str] = set()
    scored: list[tuple[int, int, dict[str, str]]] = []
    for row in rows:
        subject = f"{row['chain']}:{row['token']}"
        if subject in seen or not SUBJECT["token"].match(subject):
            continue
        seen.add(subject)
        tape = bool(src.reader.q("SELECT 1 FROM swaps WHERE chain=? AND token=? AND ts_ms>? LIMIT 1",
                                 (row["chain"], row["token"], src.now_ms - H_MS)))
        token = src.reader.q("SELECT migrated_ms FROM tokens WHERE chain=? AND address=?", (row["chain"], row["token"]))
        graduated = bool(token and token[0][0])
        if graduated and not tape:     # neither question on the token menu could ever be scored
            continue
        rank = (0 if row["mode"] == "live" else 1, 0 if tape else 1)
        scored.append((rank[0], rank[1], {"subject": subject, "why": f"enter:{row['lane']}:{row['mode']}"}))
    return [item for _, _, item in sorted(scored, key=lambda x: (x[0], x[1]))]


def _candidates_perps(src: Sources) -> list[dict[str, str]]:
    doc = intel(src, "tickers")
    data = (doc or {}).get("data") if doc and doc.get("ok") and not doc.get("stale") else None
    ts = num((doc or {}).get("fetched_ts"))
    if not isinstance(data, dict) or ts is None or (src.now_ms - ts * 1000) / 1000 > INTEL_BUDGET_S["tickers"]:
        raise SourceDeferred("market-intel/tickers missing or stale")
    report = _json(src.data_dir / "perps" / "report.json") or {}
    held = {r.get("symbol") for r in report.get("open_positions") or [] if isinstance(r, dict)}
    universe = set(report.get("symbols") or {})
    rows = [r for r in data.get("tickers") or [] if isinstance(r, dict) and SUBJECT["perps"].match(str(r.get("symbol")))
            and num(r.get("price"))]
    rows.sort(key=lambda r: (r["symbol"] not in held, r["symbol"] not in universe, -abs(num(r.get("change_pct")) or 0),
                             r["symbol"]))
    return [{"subject": r["symbol"], "why": "paper_position" if r["symbol"] in held else "largest_24h_move"} for r in rows]


def _candidates_lp(src: Sources) -> list[dict[str, str]]:
    lab = labs.lp_lab(src.data_dir)
    at = num(lab.get("at"))
    if not lab.get("available") or at is None or (src.now_ms / 1000 - at) > labs.STALE_S:
        raise SourceDeferred("labs/lp/result.json missing or stale")
    rows = [p for p in lab.get("pools_table") or [] if not p.get("gone") and not p.get("blacklisted")
            and num(p.get("fee_tvl_24h")) is not None and (num(p.get("tvl")) or 0) > 0]
    rows.sort(key=lambda p: (-num(p.get("fee_tvl_24h")), str(p.get("address"))))
    return [{"subject": str(p["address"]), "why": "fee_tvl_rank"} for p in rows]


def _candidates_nft(src: Sources) -> list[dict[str, str]]:
    rows = src.reader.rows("SELECT paper_id,verdict,decided_ms,features_json FROM nft_paper_mints "
                           "ORDER BY paper_id DESC LIMIT 200")
    young = [r for r in rows if 0 <= src.now_ms - r["decided_ms"] <= NFT_MAX_AGE_S * 1000]

    def fills(row: dict[str, Any]) -> int:
        features = load_json(row["features_json"], {})
        value = features.get("fills_24h") if isinstance(features, dict) else None
        return value if isinstance(value, int) else 0

    young.sort(key=lambda r: (r["verdict"] != "mint", -fills(r), -r["paper_id"]))
    return [{"subject": f"paper:{r['paper_id']}", "why": f"paper_{r['verdict']}"} for r in young]


def _candidates_airdrop(src: Sources) -> list[dict[str, str]]:
    rows = src.reader.rows("SELECT opportunity_id,kind,ev_score,deadline_ms,last_seen_ms FROM opportunities "
                           "WHERE kind IN ('airdrop','points') AND status='open'")
    live = [r for r in rows if r["last_seen_ms"] and src.now_ms - r["last_seen_ms"] <= 24 * H_MS
            and SUBJECT["airdrop"].match(str(r["opportunity_id"]))]
    live.sort(key=lambda r: (r["ev_score"] is None, r["deadline_ms"] is None, r["deadline_ms"] or 0,
                             str(r["opportunity_id"])))
    return [{"subject": str(r["opportunity_id"]), "why": f"open_{r['kind']}"} for r in live]


CANDIDATES = {"token": _candidates_token, "perps": _candidates_perps, "lp": _candidates_lp, "nft": _candidates_nft,
              "airdrop": _candidates_airdrop}
