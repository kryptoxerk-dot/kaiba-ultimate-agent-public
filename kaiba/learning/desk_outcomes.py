"""What a desk verdict may forecast, and how each forecast is settled from data Kaiba already holds.

**A closed menu, not free text.** A desk forecast is one question from :data:`MENU`. Three reasons:

1. An outcome nobody can resolve is not a forecast. Each item names its resolver.
2. Free thresholds would hand the desk fake skill: a model that only knows a +100% move is rarer than a
   +20% move looks calibrated. With fixed questions the base rate is taken per question.
3. Questions accumulate n. Thirty resolved forecasts of one question is a measurement.

**Both answers must be provable.** A yes/no outcome is scored only when "no" is proved as well as "yes".
If only "yes" could be seen, every scored row would be a hit and the Brier score would be meaningless.
So a graduation needs the migration feed proven alive across the window, and a delisting needs that
row's own source proven healthy after the due time. Without the proof the verdict is ``unresolved``,
never a miss. The same applies to a dead token: no print is not -100%.

**MEASURED on the box, 2026-11-04**, which is why the menu looks like this:

* Paper positions close in a median of 1 to 14 minutes by lane (migration-fade 0.02 h, launch-snipe
  0.13 h, sm-trenches 0.10 h, p90 at most 1.35 h). A desk that runs a few times a day would forecast
  trades already over, so no question is about a position.
* Of 100 tokens entered 30-72 h earlier, 9 had a tape print within 3 h after +24 h and 2 had a dossier
  rebuilt after it. Token price questions are therefore ``partial`` and the resolved ones are
  survivors. The report shows the unresolved share beside them.
* Graduation is provable on sol and robinhood (``token.migrated``, 4,946 and 54 events in 134 h); flap
  emits no such event, so bsc is not offered.
* NFT paper marks: no row older than 48 h lacked its +24 h mark.
* DLMM ``fee_tvl_24h`` over the panel: median 0.57%, upper quartile 2.3%, so 1% is an informative line.
* Kaiba holds no airdrop claim or sale data, so whether a programme paid cannot be settled here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kaiba.learning import labs
from kaiba.learning.autolearn_io import load_json
from kaiba.learning.desk_packets import H_MS, Sources, intel, num

GRADUATION_FEED_CHAINS = ("sol", "robinhood")
#: Newest token.migrated rows read per pass. 5,000 took 2.3 s against a 3 s deadline; 2,000 spans ~54 h.
GRADUATION_FEED_ROWS = 2000
#: A mark further than this past due is another horizon's answer.
TOKEN_MARK_TOLERANCE_MS = 3 * H_MS
SUSPECT_MOVE = 100.0      # >100x either way: the bogus-mark screen from the 2026-11-02 review
LP_MARK_TOLERANCE_MS = 36 * H_MS   # the lab writes daily; labs.STALE_S
AIRDROP_SEEN_WINDOW_MS = 48 * H_MS
AIRDROP_CHECK_WINDOW_MS = 48 * H_MS
NATIVE_CHAIN = {"SOLUSDT": "sol", "ETHUSDT": "eth", "BNBUSDT": "bsc"}


class Unresolvable(Exception):
    """This outcome cannot honestly be settled now. The reason is stored; it is never a miss or a hit."""


@dataclass(frozen=True)
class Outcome:
    id: str
    domain: str
    metric: str
    op: str
    threshold: float
    horizon_h: int
    direction: str        # up: favours a long. down: adverse, for short or avoid.
    scoreable: str        # yes | partial | no
    note: str = ""


_SURVIVOR = "resolves only if the token still prints near due: about 9 in 100 did, and those are survivors"
MENU: tuple[Outcome, ...] = (
    Outcome("token_graduates_24h", "token", "graduated", "==", 1, 24, "up", "yes"),
    Outcome("token_up50_24h", "token", "price_return_pct", ">=", 50, 24, "up", "partial", _SURVIVOR),
    Outcome("token_down50_24h", "token", "price_return_pct", "<=", -50, 24, "down", "partial", _SURVIVOR),
    Outcome("perp_up3_24h", "perps", "price_return_pct", ">=", 3, 24, "up", "yes"),
    Outcome("perp_down3_24h", "perps", "price_return_pct", "<=", -3, 24, "down", "yes"),
    Outcome("perp_up5_72h", "perps", "price_return_pct", ">=", 5, 72, "up", "yes"),
    Outcome("perp_down5_72h", "perps", "price_return_pct", "<=", -5, 72, "down", "yes"),
    Outcome("lp_fee_tvl_1pct_24h", "lp", "fee_tvl_24h", ">=", 0.01, 24, "up", "yes",
            "the first daily lab file at or after due, so the 24 h window is wholly after the verdict"),
    Outcome("lp_tvl_down50_72h", "lp", "tvl_change_pct", "<=", -50, 72, "down", "yes"),
    Outcome("nft_real_exit_24h", "nft", "real_exit", "==", 1, 24, "up", "yes", "a real fill near +24 h, never an ask"),
    Outcome("nft_paper_profit_24h", "nft", "paper_profit", "==", 1, 24, "up", "yes"),
    Outcome("nft_no_exit_24h", "nft", "real_exit", "==", 0, 24, "down", "yes"),
    Outcome("airdrop_token_confirmed_168h", "airdrop", "token_confirmed", "==", 1, 168, "up", "yes"),
    Outcome("airdrop_delisted_168h", "airdrop", "delisted", "==", 1, 168, "down", "yes"),
    Outcome("airdrop_value_ge_cost_168h", "airdrop", "claim_value_ge_cost", "==", 1, 168, "up", "no",
            "Kaiba holds no claim or sale data: this stays unresolved and is counted"),
)


def _anchor(item: Outcome, subject: str, facts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """What this question needs from the packet, or None when the packet cannot support it."""
    def value(key: str) -> Any:
        return facts[key]["value"] if key in facts else None

    if item.metric == "graduated":
        return {} if value("token.graduated") is False and subject.split(":")[0] in GRADUATION_FEED_CHAINS else None
    if item.domain == "token":
        price, source = value("tape.last_price_usd"), value("tape.price_source")
        return {"price": price, "source": source} if price and source else None
    if item.domain == "perps":
        return {"price": value("ticker.price")} if value("ticker.price") else None
    if item.metric == "fee_tvl_24h":
        return {} if value("pool.fee_tvl_24h") is not None else None
    if item.metric == "tvl_change_pct":
        return {"tvl": value("pool.tvl")} if value("pool.tvl") else None
    if item.domain == "nft":
        return {} if value("nft.verdict") else None
    if item.metric == "token_confirmed":
        return {} if value("opp.confirmed_token") is False else None
    return {} if value("opp.last_seen_ms") else None


def menu(domain: str, subject: str, facts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The questions this packet can support, with what each needs frozen beside it."""
    out = []
    for item in MENU:
        anchor = _anchor(item, subject, facts) if item.domain == domain else None
        if anchor is not None:
            out.append({"id": item.id, "metric": item.metric, "op": item.op, "threshold": item.threshold,
                        "horizon_h": item.horizon_h, "direction": item.direction, "scoreable": item.scoreable,
                        "note": item.note, "anchor": anchor})
    return out


def hit(op: str, value: float, threshold: float) -> int:
    return int({"==": value == threshold, ">=": value >= threshold, "<=": value <= threshold}[op])


class ScoreContext:
    """One pass's reads. Anything shared between verdicts is read once, and only if a verdict needs it."""

    def __init__(self, src: Sources) -> None:
        self.src, self._cache = src, {}

    def once(self, key: str, read: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = read()
        return self._cache[key]


@dataclass(frozen=True)
class Mark:
    value: float
    ms: int
    source: str


def _token_price(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    chain, token = v["subject"].split(":", 1)
    anchor, due = v["anchor"], v["due_ms"]
    # Same source as the anchor: swaps mixes price scales across sources (5.93 against 0.0000064, same token).
    rows = ctx.src.reader.rows("SELECT ts_ms,price_usd FROM swaps WHERE chain=? AND token=? AND source=? AND ts_ms>=? "
                               "AND ts_ms<=? ORDER BY ts_ms LIMIT 1",
                               (chain, token, anchor["source"], due, due + TOKEN_MARK_TOLERANCE_MS))
    price = num(rows[0]["price_usd"]) if rows else None
    if not price or price <= 0:
        raise Unresolvable("no_print_near_due")
    ratio = price / float(anchor["price"])
    if ratio > SUSPECT_MOVE or ratio < 1 / SUSPECT_MOVE:
        raise Unresolvable(f"suspect_mark:{ratio:.4g}x")
    return Mark(100 * (ratio - 1), rows[0]["ts_ms"], f"swaps:{anchor['source']}")


def _graduated(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    chain, token = v["subject"].split(":", 1)
    if chain not in GRADUATION_FEED_CHAINS:
        raise Unresolvable(f"no_graduation_feed:{chain}")
    feed = ctx.once("migrations", lambda: ctx.src.reader.q(
        "SELECT chain,ts_ms FROM events WHERE kind='token.migrated' ORDER BY id DESC LIMIT ?", (GRADUATION_FEED_ROWS,)))
    if not any(c == chain and v["issued_ms"] <= ts <= v["due_ms"] for c, ts in feed):
        raise Unresolvable("graduation_feed_not_proven_alive")
    rows = ctx.src.reader.q("SELECT migrated_ms FROM tokens WHERE chain=? AND address=?", (chain, token))
    if not rows:
        raise Unresolvable("token_row_gone")
    migrated = rows[0][0]
    return Mark(float(migrated is not None and migrated <= v["due_ms"]), v["due_ms"], "tokens.migrated_ms")


def _perp_price(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    due, symbol = v["due_ms"], v["subject"]
    tolerance = max(2 * H_MS, v["horizon_h"] * H_MS // 10)
    anchor = float(v["anchor"]["price"])
    doc = ctx.once("tickers", lambda: intel(ctx.src, "tickers"))
    ts = num((doc or {}).get("fetched_ts"))
    if doc and doc.get("ok") and not doc.get("stale") and ts is not None and due <= ts * 1000 <= due + tolerance:
        data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
        row = next((r for r in data.get("tickers") or [] if isinstance(r, dict) and r.get("symbol") == symbol), None)
        price = num(row.get("price")) if row else None
        if price and price > 0:
            return Mark(100 * (price / anchor - 1), int(ts * 1000), "market-intel/tickers")
    chain = NATIVE_CHAIN.get(symbol)
    if chain:     # a durable series, so a pass that missed the window can still settle these three
        rows = ctx.src.reader.rows("SELECT ts_ms,price_usd FROM native_prices WHERE chain=? AND ts_ms>=? AND ts_ms<=? "
                                   "ORDER BY ts_ms LIMIT 1", (chain, due, due + tolerance))
        price = num(rows[0]["price_usd"]) if rows else None
        if price and price > 0:
            return Mark(100 * (price / anchor - 1), rows[0]["ts_ms"], f"native_prices:{chain}")
    raise Unresolvable("no_price_mark_near_due")


def _lp_row(v: dict[str, Any], ctx: ScoreContext) -> tuple[dict[str, Any], int]:
    lab = ctx.once("lab", lambda: labs.lp_lab(ctx.src.data_dir))
    at = None if not lab.get("available") or lab.get("at") is None else int(lab["at"]) * 1000
    if at is None:
        raise Unresolvable("lab_unavailable")
    if at < v["due_ms"]:
        raise Unresolvable("lab_not_yet_past_due")
    if at > v["due_ms"] + LP_MARK_TOLERANCE_MS:
        raise Unresolvable("lab_file_too_late")
    rows = lab.get("pools_table") or []
    row = next((p for p in rows if p.get("address") == v["subject"]), None)
    if row is None:
        raise Unresolvable("pool_not_in_panel")
    # "gone" means the API stopped serving the pool. A panel mostly gone means the API failed, not the pools.
    if row.get("gone") and sum(1 for p in rows if p.get("gone")) * 2 > len(rows):
        raise Unresolvable("panel_mostly_gone")
    return row, at


def _lp_fee(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    row, at = _lp_row(v, ctx)
    value = 0.0 if row.get("gone") else num(row.get("fee_tvl_24h"))
    if value is None:
        raise Unresolvable("fee_tvl_missing")
    return Mark(value, at, "labs/lp/result.json")


def _lp_tvl(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    row, at = _lp_row(v, ctx)
    tvl = 0.0 if row.get("gone") else num(row.get("tvl"))
    if tvl is None:
        raise Unresolvable("tvl_missing")
    return Mark(100 * (tvl / float(v["anchor"]["tvl"]) - 1), at, "labs/lp/result.json")


def _nft_mark(v: dict[str, Any], ctx: ScoreContext) -> dict[str, Any]:
    paper_id = int(v["subject"].split(":", 1)[1])
    rows = ctx.src.reader.rows("SELECT mark_ms,window_from_ms,exit_basis,pnl_wei FROM nft_paper_marks "
                               "WHERE paper_id=? AND horizon_h=?", (paper_id, v["horizon_h"]))
    if not rows:
        raise Unresolvable("mark_not_written")
    if rows[0]["window_from_ms"] < v["issued_ms"]:
        raise Unresolvable("mark_window_opened_before_verdict")
    return rows[0]


def _nft_exit(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    mark = _nft_mark(v, ctx)
    return Mark(float(mark["exit_basis"] != "none"), mark["mark_ms"], "nft_paper_marks")


def _nft_profit(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    mark = _nft_mark(v, ctx)
    return Mark(float(int(mark["pnl_wei"]) > 0), mark["mark_ms"], "nft_paper_marks")


def _opportunity(v: dict[str, Any], ctx: ScoreContext) -> dict[str, Any]:
    rows = ctx.src.reader.rows("SELECT source,last_seen_ms,evidence_json FROM opportunities WHERE opportunity_id=?",
                               (v["subject"],))
    if not rows:
        raise Unresolvable("opportunity_row_gone")
    names = [x.strip() for x in str(rows[0]["source"] or "").split(",") if x.strip()]
    health = ctx.once("hunters", lambda: {r["name"]: r for r in ctx.src.reader.rows(
        "SELECT name,last_ok_ms,last_count,fail_streak FROM hunter_sources WHERE kind='airdrop'")})
    # A dead aggregator (airdropalert: 1,140 straight failures) stops refreshing every row it ever fed.
    proven = [health[n] for n in names if n in health and (health[n]["last_ok_ms"] or 0) >= v["due_ms"]
              and not health[n]["fail_streak"] and health[n]["last_count"]]
    if not proven:
        raise Unresolvable("source_not_proven_healthy_after_due")
    return rows[0]


def _token_confirmed(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    row = _opportunity(v, ctx)
    if ctx.src.now_ms > v["due_ms"] + AIRDROP_CHECK_WINDOW_MS:
        raise Unresolvable("checked_too_late")      # the row holds today's state, not the state at due
    if (row["last_seen_ms"] or 0) < v["due_ms"] - AIRDROP_SEEN_WINDOW_MS:
        raise Unresolvable("row_not_refreshed_near_due")
    evidence = load_json(row["evidence_json"], {})
    return Mark(float(evidence.get("confirmed_token") is True), row["last_seen_ms"], "opportunities")


def _delisted(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    row = _opportunity(v, ctx)
    seen = row["last_seen_ms"] or 0
    return Mark(float(seen < v["due_ms"] - AIRDROP_SEEN_WINDOW_MS), seen, "opportunities.last_seen_ms")


def _claim_value(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    raise Unresolvable("no_claim_or_sale_data_in_kaiba")


RESOLVERS: dict[tuple[str, str], Callable[[dict[str, Any], ScoreContext], Mark]] = {
    ("token", "price_return_pct"): _token_price, ("token", "graduated"): _graduated,
    ("perps", "price_return_pct"): _perp_price, ("lp", "fee_tvl_24h"): _lp_fee, ("lp", "tvl_change_pct"): _lp_tvl,
    ("nft", "real_exit"): _nft_exit, ("nft", "paper_profit"): _nft_profit,
    ("airdrop", "token_confirmed"): _token_confirmed, ("airdrop", "delisted"): _delisted,
    ("airdrop", "claim_value_ge_cost"): _claim_value,
}


def resolve(v: dict[str, Any], ctx: ScoreContext) -> Mark:
    """Settle one verdict. Raises Unresolvable, or SourceDeferred when the database is busy."""
    return RESOLVERS[(v["domain"], v["metric"])](v, ctx)
