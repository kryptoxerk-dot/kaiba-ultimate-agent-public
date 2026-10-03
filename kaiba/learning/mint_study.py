"""PAPER NFT mint study: would auto-minting OpenSea drops on Robinhood Chain make money?

SPENDS NOTHING. No wallet, no signer, no transaction, no OpenSea key. Every run:

1. advances the on-chain tape (:mod:`kaiba.ingest.nft_tape`): SeaDrop mints + Seaport sales;
2. for each collection minting on chain in the last hour (plus any OpenSea drop the NFT
   hunter lists as open), once per UTC day, reads its contract and public stage
   (:func:`kaiba.providers.seaport_rh.read_collection`) and applies the pre-registered rule
   :data:`RULE_VERSION`. A pass is a paper ``mint`` row; a mintable collection that fails is
   a ``shadow`` row with the clauses it failed. Both record what one unit would have cost
   AT DECISION TIME (on-chain public price -- the 10% marketplace fee is a split INSIDE that
   price, verified on chain -- plus gas at the frontier block's base fee);
3. scores every row at +24 h and +72 h from REAL fills only (:func:`mark_from`);
4. evaluates the PASS/FAIL gate (:func:`evaluate_gate`) and stores it in ``kv``.

Why the rule reads fills and never asks. Measured 2026-10-02: the four Robinhood drops the
hunter found had ask/mint of 6.2x, 1.5x, 2.8x and 0.73x; the three "profitable" ones had
ZERO sales in 24 h and the one with a market traded below mint. Of 27 collections minting
in a 34-minute window, 2 traded at all and none had a bid accepted. An ask is a hope.

Why shadow rows exist (owner directive, 2026-09-24: "measure on the scanned population
first"): two filters were shipped and pulled within hours that day because they had been
measured on the fills we TOOK, not on what we SCAN. The rule here is scored against the
very population it rejects, and the gate requires it to beat that population.

Pre-registration. The rule thresholds, the scoring policy and the gate are code constants
under version strings, NOT schedule parameters: changing one mid-study would silently
mix two experiments in one sample. Change one by bumping its version.

Pure functions (:func:`decide`, :func:`mark_from`, :func:`evaluate_gate`,
:func:`cluster_bootstrap_ci`) take plain data and are what the tests pin; the I/O wrappers
around them are thin. Money is integer wei; statistics are floats; nothing priced passes
through float on its way into a row.
"""

from __future__ import annotations

import logging
import random
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload, tx
from kaiba.core.schemas import Chain, now_ms
from kaiba.ingest import nft_tape
from kaiba.providers import seaport_rh as sp

log = logging.getLogger(__name__)

RULE_VERSION = "mint-rule-v1"
MARK_VERSION = "mark-v1"
GATE_VERSION = "mint-gate-v1"
STARTED_KEY = "nft_mint_study:started_ms"
GATE_KEY = "nft_mint_study:gate"
SKIPPED_KEY = "nft_mint_study:skipped_today"

H = 3_600_000
DAY = 86_400_000
WEI = 10**18
ETH = Decimal(WEI)


# --------------------------------------------------------------------------------------
# parameters (pre-registered; see the module docstring)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleParams:
    """``mint-rule-v1``: scratchpad/mint-farm-design.md §2.4, written down as code."""

    #: INVENTED: 0.005 ETH (~$13.6 at $2,720). The live phase the design proposes is
    #: $10/day; nothing above one day's budget for one unit is worth measuring.
    max_mint_price_wei: int = 5 * 10**15
    #: Design §2.4 rule 2: >= 5 fills, >= 3 distinct buyers, >= 1 accepted bid in 24 h.
    min_fills_24h: int = 5
    min_buyers_24h: int = 3
    min_offer_fills_24h: int = 1
    #: Rule 3: median net-to-seller per unit >= 1.6 x (mint price + mint gas + sell gas).
    min_resale_multiple: Decimal = Decimal("1.6")
    #: Rule 4: the trailing-6 h median may not sit more than 25% below the 24 h median,
    #: and a market with no fill at all in 6 h is not a market.
    max_6h_drop: Decimal = Decimal("0.25")
    #: MEASURED 2026-10-02 from receipts: a signed qty-1 mint used 137,585 gas (a public
    #: mint skips the signature check); a bid fill 232,932 and an accept-offer match
    #: 284,614. Rounded up so the estimate is never the cheap side.
    mint_gas_units: int = 200_000
    sell_gas_units: int = 300_000
    #: Used only if the tape has no base fee yet. ~3x the measured 35.3 Mwei.
    fallback_gas_price_wei: int = 100_000_000


@dataclass(frozen=True)
class MarkParams:
    """``mark-v1``: how a paper mint is valued at +24 h / +72 h."""

    horizons_h: tuple[int, ...] = (24, 72)
    #: INVENTED: fills in [mark - 6 h, mark + 6 h] are the evidence for the mark.
    window_half_h: float = 6.0
    #: INVENTED depth proxy for "our one unit would have filled": a bid we could hit
    #: existed AND someone else's was hit too.
    min_exit_offer_fills: int = 2


@dataclass(frozen=True)
class GateParams:
    """``mint-gate-v1``. Every clause must hold for PASS; see :func:`evaluate_gate`."""

    horizon_h: int = 24
    robustness_horizon_h: int = 72
    min_scored: int = 30
    min_collections: int = 10
    min_span_days: float = 7.0
    ci_level: float = 0.90
    #: The 90% bootstrap lower bound of mean P&L per paper mint must be ABOVE this (wei).
    #: 0: an interval that reaches below zero cannot be told apart from losing money.
    ci_lower_floor_wei: int = 0
    min_realizable_share: float = 0.5
    min_fillable_share: float = 0.5
    min_shadow_scored: int = 30
    #: Design §2.5: not met in 30 days -> the verdict is "no" and the job is retired.
    final_after_days: float = 30.0
    bootstrap_draws: int = 2000
    seed: int = 20261002


@dataclass(frozen=True)
class StudyParams:
    rule: RuleParams = field(default_factory=RuleParams)
    mark: MarkParams = field(default_factory=MarkParams)
    gate: GateParams = field(default_factory=GateParams)
    tape: nft_tape.TapeConfig = field(default_factory=nft_tape.TapeConfig)
    #: Operational knobs (schedule.yaml may set these; they do not change what is measured).
    candidate_lookback_h: float = 1.0
    evidence_hours: float = 24.0
    trend_hours: float = 6.0
    #: INVENTED: a candidate costs 2 reads, 4 if its public stage is open (seaport_rh).
    max_checks_per_run: int = 6
    #: Minimum gap between this job's own HTTP requests. MEASURED 2026-10-02 from the
    #: workstation: a 7-read batch 2.5 s after a 4-read batch drew an HTTP 429; 3 s is
    #: that gap plus margin, on top of the limiter's 1.5 s provider-wide spacing.
    pace_s: float = 3.0
    max_frontier_lag_min: float = 20.0
    score_batch: int = 500

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None) -> StudyParams:
        p = dict(params or {})
        kw: dict[str, Any] = {"tape": nft_tape.TapeConfig.from_params(p)}
        if p.get("pace_s") is not None:
            kw["pace_s"] = max(0.0, float(p["pace_s"]))
        if p.get("max_checks_per_run") is not None:
            kw["max_checks_per_run"] = max(0, int(p["max_checks_per_run"]))
        if p.get("max_frontier_lag_min") is not None:
            kw["max_frontier_lag_min"] = max(1.0, float(p["max_frontier_lag_min"]))
        if p.get("score_batch") is not None:
            kw["score_batch"] = max(1, int(p["score_batch"]))
        return cls(**kw)


# --------------------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------------------


class EthUsd:
    """ETH/USD at a time, from ``native_prices`` (Robinhood reads ETH's samples).

    Only USDG fills need it (to put them in ETH) and the display of USD figures. Cached per
    hour per run. ``None`` when no sample is within an hour -- a USDG fill is then counted
    as unpriced, never priced at a guess.
    """

    def __init__(self, conn: Any, tolerance_ms: int = H) -> None:
        self.conn = conn
        self.tolerance_ms = tolerance_ms
        self._cache: dict[int, Decimal | None] = {}

    def at(self, ts_ms: int) -> Decimal | None:
        bucket = int(ts_ms) // H
        if bucket not in self._cache:
            value: Decimal | None = None
            try:
                from kaiba.providers import native_price

                got = native_price.at(Chain.ROBINHOOD, int(ts_ms), self.conn, tolerance_ms=self.tolerance_ms)
                value = got.price_usd if got.known and got.price_usd and got.price_usd > 0 else None
            except Exception as exc:  # noqa: BLE001 - an unpriced fill stays unpriced
                log.debug("eth/usd unavailable at %s: %s", ts_ms, exc)
            self._cache[bucket] = value
        return self._cache[bucket]


def to_eth_wei(payment_token: str, amount: int, ts_ms: int, fx: EthUsd | None) -> int | None:
    """An amount in a Seaport payment token -> ETH wei, or ``None`` if unpriceable."""
    token = (payment_token or "").lower()
    if token in (sp.ZERO_ADDRESS, sp.WETH):
        return int(amount)
    if token == sp.USDG and fx is not None:
        usd = fx.at(ts_ms)
        if usd is None or usd <= 0:
            return None
        try:  # atoms / 1e6 USD / (USD per ETH) * 1e18 wei
            return int(Decimal(int(amount)) * Decimal(10) ** (18 - sp.USDG_DECIMALS) / usd)
        except (InvalidOperation, ArithmeticError):
            return None
    return None


def _median(values: Sequence[int]) -> int | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2


# --------------------------------------------------------------------------------------
# evidence (what the tape says about one collection before a decision)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FillPoint:
    ts_ms: int
    kind: str                 # listing | offer
    unit_net_wei: int | None  # what the seller kept per unit, in ETH wei; None = unpriced
    buyer: str | None
    seller: str | None


def load_fill_points(conn: Any, collection: str, from_ms: int, to_ms: int, fx: EthUsd | None,
                     *, limit: int = 20_000) -> list[FillPoint]:
    """Sales of one collection with ``from_ms <= ts_ms <= to_ms``, through the index."""
    rows = fetch_all(
        conn,
        "SELECT ts_ms, kind, payment_token, seller_net, units, buyer, seller FROM nft_fills "
        "WHERE collection=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms LIMIT ?",
        (collection.lower(), int(from_ms), int(to_ms), int(limit)),
    )
    out: list[FillPoint] = []
    for r in rows:
        try:
            net, units = int(r["seller_net"]), max(1, int(r["units"] or 1))
        except (TypeError, ValueError):
            continue
        eth = to_eth_wei(r["payment_token"], net, int(r["ts_ms"]), fx)
        out.append(FillPoint(int(r["ts_ms"]), str(r["kind"]), eth // units if eth is not None else None,
                             r["buyer"], r["seller"]))
    return out


@dataclass(frozen=True)
class MintFlow:
    mints_1h: int = 0
    mints_24h: int = 0
    minters_24h: int = 0
    qty_24h: int = 0
    fee_recipient_proven: bool = False


def load_mint_flow(conn: Any, collection: str, at_ms: int) -> MintFlow:
    c = collection.lower()
    one = fetch_one(conn, "SELECT COUNT(*) AS n FROM nft_mints WHERE collection=? AND ts_ms>? AND ts_ms<=?",
                    (c, at_ms - H, at_ms))
    day = fetch_one(conn, "SELECT COUNT(*) AS n, COUNT(DISTINCT minter) AS m, COALESCE(SUM(quantity),0) AS q "
                          "FROM nft_mints WHERE collection=? AND ts_ms>? AND ts_ms<=?", (c, at_ms - DAY, at_ms))
    proven = fetch_one(conn, "SELECT 1 AS ok FROM nft_mints WHERE collection=? AND fee_recipient=? LIMIT 1",
                       (c, sp.OPENSEA_FEE_RECIPIENT))
    return MintFlow(
        mints_1h=int(one["n"]) if one else 0,
        mints_24h=int(day["n"]) if day else 0,
        minters_24h=int(day["m"]) if day else 0,
        qty_24h=int(day["q"]) if day else 0,
        fee_recipient_proven=proven is not None,
    )


@dataclass(frozen=True)
class Evidence:
    fills_24h: int
    offers_24h: int
    listings_24h: int
    buyers_24h: int
    sellers_24h: int
    unpriced_24h: int
    median_net_24h_wei: int | None
    fills_6h: int
    median_net_6h_wei: int | None
    flow: MintFlow = field(default_factory=MintFlow)


def evidence_from(points: Sequence[FillPoint], *, at_ms: int, trend_hours: float = 6.0,
                  flow: MintFlow | None = None) -> Evidence:
    """Pure: fills in the 24 h before ``at_ms`` -> the numbers the rule reads."""
    window = [p for p in points if at_ms - DAY <= p.ts_ms <= at_ms]
    priced = [p.unit_net_wei for p in window if p.unit_net_wei is not None]
    recent_from = at_ms - int(trend_hours * H)
    recent = [p for p in window if p.ts_ms >= recent_from]
    recent_priced = [p.unit_net_wei for p in recent if p.unit_net_wei is not None]
    return Evidence(
        fills_24h=len(window),
        offers_24h=sum(1 for p in window if p.kind == "offer"),
        listings_24h=sum(1 for p in window if p.kind == "listing"),
        buyers_24h=len({p.buyer for p in window if p.buyer}),
        sellers_24h=len({p.seller for p in window if p.seller}),
        unpriced_24h=len(window) - len(priced),
        median_net_24h_wei=_median(priced),
        fills_6h=len(recent),
        median_net_6h_wei=_median(recent_priced),
        flow=flow or MintFlow(),
    )


# --------------------------------------------------------------------------------------
# the decision rule (pure)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    verdict: str                      # mint | shadow | skip
    failed: tuple[str, ...]
    skip_reason: str | None
    mint_price_wei: int = 0
    mint_fee_wei: int = 0
    mint_gas_wei: int = 0
    sell_gas_wei: int = 0
    gas_price_wei: int = 0
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_wei(self) -> int:
        return self.mint_price_wei + self.mint_gas_wei


def _skip(reason: str, features: dict[str, Any] | None = None) -> Decision:
    return Decision("skip", (), reason, features=features or {})


def decide(check: sp.CollectionCheck, ev: Evidence, *, at_s: int, gas_price_wei: int,
           params: RuleParams | None = None) -> Decision:
    """``mint-rule-v1``. Skip = not mintable now (no row). Otherwise mint or shadow.

    Clauses, in order, each named in ``failed`` when it does not hold:

    * ``mint_price_above_cap``              on-chain public price > cap
    * ``no_code`` / ``upgradeable_proxy`` / ``contract_unrecognised``
                                            not a clone of a known SeaDrop implementation
                                            and not plain code carrying ``mintSeaDrop``
    * ``not_erc721`` / ``soulbound``
    * ``transfer_validator_unreadable`` / ``transfer_validator_unvetted``
    * ``supply_unreadable`` / ``sold_out``
    * ``resale_fills_24h`` / ``resale_buyers_24h`` / ``resale_no_accepted_bid_24h``
    * ``resale_below_cost_multiple``        median net-to-seller < 1.6 x all-in cost
    * ``resale_collapsing_6h``              no fill in 6 h, or 6 h median < 75% of 24 h
    """
    rp = params or RuleParams()
    if not check.transport_ok:
        return _skip("check_unread", {"note": check.note})
    drop = check.public_drop
    if drop is None:
        return _skip("public_drop_unreadable")
    if not drop.open_at(at_s):
        return _skip("no_open_public_stage" if drop.configured else "no_public_drop",
                     {"start_s": drop.start_s, "end_s": drop.end_s})

    price = drop.mint_price_wei
    mint_gas = rp.mint_gas_units * gas_price_wei
    sell_gas = rp.sell_gas_units * gas_price_wei
    failed: list[str] = []
    if price > rp.max_mint_price_wei:
        failed.append("mint_price_above_cap")

    facts = check.facts
    if facts is None:
        failed.append("code_unreadable")
    elif facts.kind == "empty":
        failed.append("no_code")
    elif facts.kind == "eip1967":
        failed.append("upgradeable_proxy")
    elif not facts.recognised:
        failed.append("contract_unrecognised")
    if check.is_erc721 is not True:
        failed.append("not_erc721")
    if facts is not None and facts.soulbound:
        failed.append("soulbound")
    if facts is not None and facts.has_transfer_validator:
        v = check.transfer_validator
        if v is None:
            failed.append("transfer_validator_unreadable")
        elif v != sp.ZERO_ADDRESS and v not in sp.KNOWN_TRANSFER_VALIDATORS:
            failed.append("transfer_validator_unvetted")
    headroom = check.headroom
    if headroom is None:
        failed.append("supply_unreadable")
    elif headroom <= 0:
        failed.append("sold_out")

    if ev.fills_24h < rp.min_fills_24h:
        failed.append("resale_fills_24h")
    if ev.buyers_24h < rp.min_buyers_24h:
        failed.append("resale_buyers_24h")
    if ev.offers_24h < rp.min_offer_fills_24h:
        failed.append("resale_no_accepted_bid_24h")
    all_in = price + mint_gas + sell_gas
    if ev.median_net_24h_wei is None or Decimal(ev.median_net_24h_wei) < rp.min_resale_multiple * all_in:
        failed.append("resale_below_cost_multiple")
    if (ev.fills_6h == 0 or ev.median_net_6h_wei is None or ev.median_net_24h_wei is None
            or Decimal(ev.median_net_6h_wei) < (1 - rp.max_6h_drop) * Decimal(ev.median_net_24h_wei)):
        failed.append("resale_collapsing_6h")

    features = {
        "public_drop": asdict(drop),
        "code_kind": facts.kind if facts else None,
        "code_size": facts.code_size if facts else None,
        "implementation": facts.implementation if facts else None,
        "transfer_validator": check.transfer_validator,
        "is_erc721": check.is_erc721,
        "max_supply": check.max_supply,
        "total_supply": check.total_supply,
        "fills_24h": ev.fills_24h, "offers_24h": ev.offers_24h, "listings_24h": ev.listings_24h,
        "buyers_24h": ev.buyers_24h, "sellers_24h": ev.sellers_24h, "unpriced_24h": ev.unpriced_24h,
        "median_net_24h_wei": ev.median_net_24h_wei, "fills_6h": ev.fills_6h,
        "median_net_6h_wei": ev.median_net_6h_wei,
        "mints_1h": ev.flow.mints_1h, "mints_24h": ev.flow.mints_24h,
        "minters_24h": ev.flow.minters_24h, "qty_24h": ev.flow.qty_24h,
        "fee_recipient_proven": ev.flow.fee_recipient_proven,
        "all_in_cost_wei": all_in,
    }
    return Decision(
        verdict="shadow" if failed else "mint",
        failed=tuple(failed),
        skip_reason=None,
        mint_price_wei=price,
        mint_fee_wei=price * drop.fee_bps // 10_000,
        mint_gas_wei=mint_gas,
        sell_gas_wei=sell_gas,
        gas_price_wei=gas_price_wei,
        features=features,
    )


# --------------------------------------------------------------------------------------
# marks (pure)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mark:
    horizon_h: int
    mark_ms: int
    window_from_ms: int
    window_to_ms: int
    n_offer: int
    n_listing: int
    n_unpriced: int
    exit_basis: str
    exit_wei: int
    net_exit_wei: int
    pnl_wei: int
    best_basis: str
    best_exit_wei: int
    pnl_best_wei: int


def mark_window(decided_ms: int, horizon_h: int, half_window_h: float) -> tuple[int, int, int]:
    mark = decided_ms + horizon_h * H
    half = int(half_window_h * H)
    return mark, mark - half, mark + half


def _net_of_gas(exit_wei: int, sell_gas_wei: int) -> int:
    """We only sell if the exit pays for the sell; otherwise we hold a zero."""
    return exit_wei - sell_gas_wei if exit_wei > sell_gas_wei else 0


def mark_from(points: Iterable[FillPoint], *, decided_ms: int, horizon_h: int, half_window_h: float,
              cost_wei: int, sell_gas_wei: int) -> Mark:
    """``mark-v1``: the realisable exit of ONE unit, from fills in the mark window.

    Gated (conservative) policy, design §2.5:
      * accepted bids in the window -> their median net-to-seller (a bid we could hit);
      * else asks bought in the window -> HALF their median (someone bought an ask; that
        does not prove ours would fill);
      * else 0 -- unsellable ("quiet token is terminal": 88% never trade again).
    Reported beside it, never gated: the BEST net price of any sale in the window, the
    optimistic reading of "best accepted offer or completed sale". If the optimistic
    figure loses money, the conservative one certainly does.
    """
    mark, lo, hi = mark_window(decided_ms, horizon_h, half_window_h)
    inside = [p for p in points if lo <= p.ts_ms <= hi]
    offers = [p.unit_net_wei for p in inside if p.kind == "offer" and p.unit_net_wei is not None]
    listings = [p.unit_net_wei for p in inside if p.kind == "listing" and p.unit_net_wei is not None]
    unpriced = sum(1 for p in inside if p.unit_net_wei is None)
    if offers:
        basis, exit_wei = "offer_median", _median(offers) or 0
    elif listings:
        basis, exit_wei = "listing_half", (_median(listings) or 0) // 2
    else:
        basis, exit_wei = "none", 0
    best_basis, best = "none", 0
    if offers or listings:
        best = max(offers + listings)
        best_basis = "best_offer" if offers and best == max(offers) else "best_sale"
    net = _net_of_gas(exit_wei, sell_gas_wei)
    net_best = _net_of_gas(best, sell_gas_wei)
    return Mark(horizon_h, mark, lo, hi, sum(1 for p in inside if p.kind == "offer"),
                sum(1 for p in inside if p.kind == "listing"), unpriced, basis, exit_wei, net,
                net - cost_wei, best_basis, best, net_best - cost_wei)


# --------------------------------------------------------------------------------------
# statistics and the gate (pure)
# --------------------------------------------------------------------------------------


def _clusters(rows: Sequence[tuple[str, int]]) -> list[tuple[int, int]]:
    """(cluster, value) -> [(sum, count)] per cluster, in a stable order."""
    acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key, value in rows:
        acc[key][0] += int(value)
        acc[key][1] += 1
    return [tuple(acc[k]) for k in sorted(acc)]  # type: ignore[misc]


def _resampled_means(clusters: Sequence[tuple[int, int]], draws: int, rng: random.Random) -> list[float]:
    k = len(clusters)
    out: list[float] = []
    for _ in range(draws):
        total = count = 0
        for _ in range(k):
            s, n = clusters[rng.randrange(k)]
            total += s
            count += n
        out.append(total / count)
    return out


def _percentiles(values: list[float], level: float) -> tuple[float, float]:
    values.sort()
    tail = (1.0 - level) / 2.0
    lo = values[int(tail * len(values))]
    hi = values[min(len(values) - 1, int((1.0 - tail) * len(values)))]
    return lo, hi


def cluster_bootstrap_ci(rows: Sequence[tuple[str, int]], *, level: float = 0.90, draws: int = 2000,
                         seed: int = 20261002) -> tuple[float, float] | None:
    """Percentile CI of the mean, resampling COLLECTIONS (not rows) with replacement.

    One collection decided on several days is one market seen several times; resampling
    rows would treat those as independent and narrow the interval falsely. ``None`` below
    three collections: an interval from two clusters is not an interval.
    """
    clusters = _clusters(rows)
    if len(clusters) < 3:
        return None
    return _percentiles(_resampled_means(clusters, max(100, draws), random.Random(seed)), level)


def cluster_bootstrap_diff_ci(a: Sequence[tuple[str, int]], b: Sequence[tuple[str, int]], *,
                              level: float = 0.90, draws: int = 2000,
                              seed: int = 20261002) -> tuple[float, float] | None:
    """CI of mean(a) - mean(b), each group resampled by collection independently."""
    ca, cb = _clusters(a), _clusters(b)
    if len(ca) < 3 or len(cb) < 3:
        return None
    rng = random.Random(seed)
    n = max(100, draws)
    ma, mb = _resampled_means(ca, n, rng), _resampled_means(cb, n, rng)
    return _percentiles([x - y for x, y in zip(ma, mb, strict=True)], level)


@dataclass(frozen=True)
class ScoredRow:
    paper_id: int
    collection: str
    verdict: str
    decided_ms: int
    pnl_wei: int
    pnl_best_wei: int
    exit_basis: str
    n_offer: int


@dataclass(frozen=True)
class GateCheck:
    name: str
    ok: bool
    value: str
    need: str


@dataclass(frozen=True)
class GateVerdict:
    status: str                       # PASS | PENDING | FAIL
    checks: tuple[GateCheck, ...]
    evaluated_ms: int
    study_age_days: float | None
    version: str = GATE_VERSION

    @property
    def failing(self) -> list[str]:
        return [c.name for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "version": self.version, "evaluated_ms": self.evaluated_ms,
                "study_age_days": self.study_age_days, "failing": self.failing,
                "checks": [asdict(c) for c in self.checks]}


def _mean(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def evaluate_gate(mint: Sequence[ScoredRow], shadow: Sequence[ScoredRow], mint_robust: Sequence[ScoredRow], *,
                  started_ms: int | None, now: int, params: GateParams | None = None,
                  mark: MarkParams | None = None) -> GateVerdict:
    """``mint-gate-v1``. PASS only when EVERY clause holds:

    1. ``scored_mints``     >= 30 paper MINTS scored at +24 h
    2. ``collections``      across >= 10 distinct collections
    3. ``span_days``        decided over >= 7 days
    4. ``mean_pnl``         mean net P&L per paper mint > 0 (ETH wei, gated exit policy)
    5. ``ci_lower``         90% collection-bootstrap lower bound of that mean > 0
    6. ``realizable_share`` >= 50% had any real exit at the mark (not "none")
    7. ``fillable_share``   >= 50% had >= 2 accepted bids in the mark window (our size fills)
    8. ``shadow_scored``    >= 30 shadow rows scored (the comparison exists)
    9. ``beats_shadow``     90% lower bound of mean(mint) - mean(shadow) > 0
    10. ``robust_72h``      >= 30 mints scored at +72 h with mean P&L > 0

    Otherwise PENDING, until the study is ``final_after_days`` old: then FAIL, final.
    """
    gp = params or GateParams()
    mp = mark or MarkParams()
    checks: list[GateCheck] = []

    def add(name: str, ok: bool, value: str, need: str) -> None:
        checks.append(GateCheck(name, bool(ok), value, need))

    n = len(mint)
    cols = len({r.collection for r in mint})
    span = (max(r.decided_ms for r in mint) - min(r.decided_ms for r in mint)) / DAY if mint else 0.0
    pnl = [r.pnl_wei for r in mint]
    mean = _mean(pnl)
    ci = cluster_bootstrap_ci([(r.collection, r.pnl_wei) for r in mint], level=gp.ci_level,
                              draws=gp.bootstrap_draws, seed=gp.seed)
    realizable = sum(1 for r in mint if r.exit_basis != "none") / n if n else 0.0
    fillable = sum(1 for r in mint if r.n_offer >= mp.min_exit_offer_fills) / n if n else 0.0
    diff = cluster_bootstrap_diff_ci([(r.collection, r.pnl_wei) for r in mint],
                                     [(r.collection, r.pnl_wei) for r in shadow],
                                     level=gp.ci_level, draws=gp.bootstrap_draws, seed=gp.seed)
    robust_mean = _mean([r.pnl_wei for r in mint_robust])

    add("scored_mints", n >= gp.min_scored, str(n), f">={gp.min_scored}")
    add("collections", cols >= gp.min_collections, str(cols), f">={gp.min_collections}")
    add("span_days", span >= gp.min_span_days, f"{span:.1f}", f">={gp.min_span_days:g}")
    add("mean_pnl", mean is not None and mean > 0, _wei_text(mean), ">0")
    add("ci_lower", ci is not None and ci[0] > gp.ci_lower_floor_wei,
        _wei_text(ci[0]) if ci else "n/a", f">{gp.ci_lower_floor_wei}")
    add("realizable_share", n > 0 and realizable >= gp.min_realizable_share, f"{realizable:.2f}",
        f">={gp.min_realizable_share:g}")
    add("fillable_share", n > 0 and fillable >= gp.min_fillable_share, f"{fillable:.2f}",
        f">={gp.min_fillable_share:g}")
    add("shadow_scored", len(shadow) >= gp.min_shadow_scored, str(len(shadow)), f">={gp.min_shadow_scored}")
    add("beats_shadow", diff is not None and diff[0] > 0, _wei_text(diff[0]) if diff else "n/a", ">0")
    add("robust_72h", len(mint_robust) >= gp.min_scored and robust_mean is not None and robust_mean > 0,
        f"n={len(mint_robust)} mean={_wei_text(robust_mean)}", f"n>={gp.min_scored}, mean>0")

    age = (now - started_ms) / DAY if started_ms else None
    if all(c.ok for c in checks):
        status = "PASS"
    elif age is not None and age >= gp.final_after_days:
        status = "FAIL"
    else:
        status = "PENDING"
    return GateVerdict(status, tuple(checks), now, round(age, 2) if age is not None else None)


def _wei_text(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{Decimal(str(value)) / ETH:+.6f}ETH"


# --------------------------------------------------------------------------------------
# I/O: candidates, decisions, scoring, gate
# --------------------------------------------------------------------------------------


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def opensea_labels(conn: Any, *, limit: int = 500) -> dict[str, dict[str, Any]]:
    """Contract -> what the NFT hunter knows (a LABEL only: its floor is an ask)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = fetch_all(conn, "SELECT name, url, evidence_json FROM opportunities "
                               "WHERE kind='nft_mint' AND chain='robinhood' LIMIT ?", (limit,))
    except Exception as exc:  # noqa: BLE001 - labels are optional
        log.debug("opensea labels unavailable: %s", exc)
        return out
    for r in rows:
        meta = (jload(r.get("evidence_json")) or {}).get("meta") or {}
        addr = str(meta.get("contract_address") or "").lower()
        if not sp.hex_int(addr) or len(addr) != 42:
            continue
        out[addr] = {"name": r.get("name"), "url": r.get("url"), "stage_state": meta.get("stage_state"),
                     "public_phase": meta.get("public_phase"), "floor_ask": meta.get("floor_price"),
                     "floor_unit": meta.get("floor_unit"), "sales_1d": meta.get("sales_1d")}
    return out


def candidate_collections(conn: Any, *, since_ms: int, until_ms: int,
                          labels: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Collections minting on chain in the window, then open OpenSea-listed drops.

    The scanned population is on chain: SeaDrop showed 27 collections minting in 34
    minutes where OpenSea's lists gave the hunter 4. OpenSea-listed open drops come first
    only because they are the ones an owner would otherwise look at by hand.
    """
    rows = fetch_all(conn, "SELECT collection, COUNT(*) AS n FROM nft_mints WHERE ts_ms>? AND ts_ms<=? "
                           "GROUP BY collection ORDER BY n DESC, collection LIMIT 500", (since_ms, until_ms))
    minted = [str(r["collection"]) for r in rows]
    listed = sorted(a for a, m in labels.items()
                    if m.get("stage_state") == "open" and m.get("public_phase") is True)
    return listed + [a for a in minted if a not in set(listed)]


def _kv_get(conn: Any, key: str) -> Any:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
    return jload(row["value"]) if row else None


def _kv_put(conn: Any, key: str, value: Any, ts: int) -> None:
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value, updated_ms=excluded.updated_ms", (key, jdump(value), ts))


def make_decisions(conn: Any, *, rpc: sp.Rpc, cursor: nft_tape.TapeCursor | None, now: int,
                   params: StudyParams, fx: EthUsd, deadline_monotonic: float | None = None) -> dict[str, Any]:
    """One bounded pass of paper decisions. Returns counts, never raises on RPC."""
    out: dict[str, Any] = {"mint": 0, "shadow": 0, "skip": Counter(), "checks": 0, "reads": 0,
                           "candidates": 0}
    if cursor is None or cursor.through_ts_ms is None:
        out["held"] = "tape_empty"
        return out
    frontier = int(cursor.through_ts_ms)
    if now - frontier > params.max_frontier_lag_min * 60_000:
        out["held"] = f"tape_behind:{(now - frontier) // 60_000}min"
        return out
    evidence_ms = int(params.evidence_hours * H)
    if not cursor.covers(frontier - evidence_ms, frontier):
        covered = (frontier - (cursor.first_ts_ms or frontier)) / H
        out["held"] = f"tape_history_short:{covered:.1f}h/{params.evidence_hours:g}h"
        return out

    day = utc_day(frontier)
    labels = opensea_labels(conn)
    candidates = candidate_collections(conn, since_ms=frontier - int(params.candidate_lookback_h * H),
                                       until_ms=frontier, labels=labels)
    out["candidates"] = len(candidates)
    done = {str(r["collection"]) for r in fetch_all(
        conn, "SELECT collection FROM nft_paper_mints WHERE day_utc=?", (day,))}
    skipped_state = _kv_get(conn, SKIPPED_KEY)
    skipped = (skipped_state.get("collections") or {}) if isinstance(skipped_state, dict) \
        and skipped_state.get("day") == day else {}
    now_s = now // 1000

    def parked(c: str) -> bool:
        # A skip parks a collection for the rest of the UTC day, except an UPCOMING public
        # stage, which is read again once it has opened (a drop opening at 18:00 is a drop).
        entry = skipped.get(c)
        if not isinstance(entry, dict):
            return entry is not None
        until = entry.get("until_s")
        return until is None or int(until) > now_s

    todo = [c for c in candidates if c not in done and not parked(c)]
    gas_price = int(cursor.base_fee_wei or params.rule.fallback_gas_price_wei)
    eth_usd = fx.at(frontier)

    for collection in todo[:params.max_checks_per_run]:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            out["held"] = "deadline"
            break
        check = sp.read_collection(collection, rpc, at_s=now // 1000)
        out["checks"] += 1
        out["reads"] += check.reads
        if not check.transport_ok:
            out["skip"]["check_unread"] += 1
            out["held"] = f"check_unread:{(check.note or '')[:60]}"
            break  # the endpoint is refusing; do not spend the rest of the run on it
        points = load_fill_points(conn, collection, frontier - evidence_ms, frontier, fx)
        ev = evidence_from(points, at_ms=frontier, trend_hours=params.trend_hours,
                           flow=load_mint_flow(conn, collection, frontier))
        decision = decide(check, ev, at_s=now // 1000, gas_price_wei=gas_price, params=params.rule)
        if decision.verdict == "skip":
            out["skip"][decision.skip_reason or "skip"] += 1
            start_s = decision.features.get("start_s")
            upcoming = isinstance(start_s, int) and start_s > now_s
            skipped[collection] = {"reason": decision.skip_reason, "until_s": start_s if upcoming else None}
            continue
        label = labels.get(collection)
        features = {**decision.features, "opensea": label} if label else decision.features
        with tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO nft_paper_mints (collection, day_utc, decided_ms, decided_block, verdict, "
                "rule_version, failed_json, features_json, label, mint_price_wei, mint_fee_wei, mint_gas_wei, "
                "sell_gas_wei, cost_wei, gas_price_wei, eth_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (collection, day, frontier, cursor.through_block, decision.verdict, RULE_VERSION,
                 jdump(list(decision.failed)), jdump(features), (label or {}).get("name"),
                 str(decision.mint_price_wei), str(decision.mint_fee_wei), str(decision.mint_gas_wei),
                 str(decision.sell_gas_wei), str(decision.cost_wei), str(decision.gas_price_wei),
                 str(eth_usd) if eth_usd is not None else None),
            )
            if _kv_get(conn, STARTED_KEY) is None:
                _kv_put(conn, STARTED_KEY, frontier, now)
        out[decision.verdict] += 1
    if skipped:
        with tx(conn):
            _kv_put(conn, SKIPPED_KEY, {"day": day, "collections": dict(list(skipped.items())[-2000:])}, now)
    out["skip"] = dict(out["skip"])
    return out


def score_due(conn: Any, *, cursor: nft_tape.TapeCursor | None, now: int, params: StudyParams,
              fx: EthUsd) -> dict[str, Any]:
    """Score every (paper row, horizon) whose mark window the tape now fully covers."""
    out: dict[str, Any] = {"scored": 0, "unscorable_tape_gap": 0}
    if cursor is None or cursor.through_ts_ms is None:
        return out
    through = int(cursor.through_ts_ms)
    half_ms = int(params.mark.window_half_h * H)
    for horizon in params.mark.horizons_h:
        latest_decision = through - horizon * H - half_ms
        rows = fetch_all(
            conn,
            "SELECT p.paper_id, p.collection, p.decided_ms, p.cost_wei, p.sell_gas_wei FROM nft_paper_mints p "
            "WHERE p.decided_ms <= ? AND NOT EXISTS (SELECT 1 FROM nft_paper_marks m "
            "WHERE m.paper_id = p.paper_id AND m.horizon_h = ?) ORDER BY p.decided_ms LIMIT ?",
            (latest_decision, horizon, params.score_batch),
        )
        for r in rows:
            mark_ms, lo, hi = mark_window(int(r["decided_ms"]), horizon, params.mark.window_half_h)
            if not cursor.covers(lo, hi):
                out["unscorable_tape_gap"] += 1
                continue
            points = load_fill_points(conn, r["collection"], lo, hi, fx)
            m = mark_from(points, decided_ms=int(r["decided_ms"]), horizon_h=horizon,
                          half_window_h=params.mark.window_half_h, cost_wei=int(r["cost_wei"]),
                          sell_gas_wei=int(r["sell_gas_wei"]))
            with tx(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO nft_paper_marks (paper_id, horizon_h, mark_ms, window_from_ms, "
                    "window_to_ms, n_offer, n_listing, n_unpriced, exit_basis, exit_wei, net_exit_wei, pnl_wei, "
                    "best_basis, best_exit_wei, pnl_best_wei, scored_ms, mark_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (int(r["paper_id"]), horizon, m.mark_ms, m.window_from_ms, m.window_to_ms, m.n_offer,
                     m.n_listing, m.n_unpriced, m.exit_basis, str(m.exit_wei), str(m.net_exit_wei),
                     str(m.pnl_wei), m.best_basis, str(m.best_exit_wei), str(m.pnl_best_wei), now, MARK_VERSION),
                )
            out["scored"] += 1
    return out


def load_scored(conn: Any, horizon_h: int, verdict: str, *, limit: int = 50_000) -> list[ScoredRow]:
    rows = fetch_all(
        conn,
        "SELECT p.paper_id, p.collection, p.verdict, p.decided_ms, m.pnl_wei, m.pnl_best_wei, m.exit_basis, "
        "m.n_offer FROM nft_paper_mints p JOIN nft_paper_marks m ON m.paper_id = p.paper_id "
        "WHERE m.horizon_h = ? AND p.verdict = ? AND p.rule_version = ? AND m.mark_version = ? "
        "ORDER BY p.decided_ms LIMIT ?",
        (horizon_h, verdict, RULE_VERSION, MARK_VERSION, limit),
    )
    return [ScoredRow(int(r["paper_id"]), str(r["collection"]), str(r["verdict"]), int(r["decided_ms"]),
                      int(r["pnl_wei"]), int(r["pnl_best_wei"]), str(r["exit_basis"]), int(r["n_offer"]))
            for r in rows]


def gate_now(conn: Any, *, now: int, params: StudyParams | None = None) -> GateVerdict:
    p = params or StudyParams()
    started = _kv_get(conn, STARTED_KEY)
    return evaluate_gate(
        load_scored(conn, p.gate.horizon_h, "mint"),
        load_scored(conn, p.gate.horizon_h, "shadow"),
        load_scored(conn, p.gate.robustness_horizon_h, "mint"),
        started_ms=int(started) if isinstance(started, int | float) else None,
        now=now, params=p.gate, mark=p.mark,
    )


def paced(rpc: sp.Rpc, pace_s: float, *, sleep: Any = time.sleep, clock: Any = time.monotonic) -> sp.Rpc:
    """``rpc`` with at least ``pace_s`` between this job's consecutive HTTP requests.

    The limiter spaces the whole ``robinhood-rpc`` provider at 1.5 s; this spaces the
    study's own bursts further, so a run never fires its batches back to back into a
    public endpoint that already answers ~4 requests an hour from the box with a 429.
    ``calls`` counts the HTTP requests made, for the receipt.
    """
    state = {"last": None}

    def call(calls: Any, endpoint: str) -> Any:
        if state["last"] is not None and pace_s > 0:
            wait = pace_s - (clock() - state["last"])
            if wait > 0:
                sleep(wait)
        try:
            return rpc(calls, endpoint)
        finally:
            state["last"] = clock()
            call.calls += 1  # type: ignore[attr-defined]

    call.calls = 0  # type: ignore[attr-defined]
    return call


def run(conn: Any, *, rpc: sp.Rpc | None = None, now: int | None = None,
        params: StudyParams | None = None, schedule_params: Mapping[str, Any] | None = None,
        deadline_monotonic: float | None = None) -> dict[str, Any]:
    """One scheduled pass: tape -> paper decisions -> marks -> gate. A small receipt."""
    p = params or StudyParams.from_params(schedule_params)
    ts = now if now is not None else now_ms()
    transport = paced(rpc or sp.default_rpc(conn), p.pace_s)
    fx = EthUsd(conn)
    tape = nft_tape.run_tape(conn, rpc=transport, config=p.tape, now=ts, deadline_monotonic=deadline_monotonic)
    decisions = make_decisions(conn, rpc=transport, cursor=tape.cursor, now=ts, params=p, fx=fx,
                               deadline_monotonic=deadline_monotonic)
    scored = score_due(conn, cursor=tape.cursor, now=ts, params=p, fx=fx)
    verdict = gate_now(conn, now=ts, params=p)
    with tx(conn):
        _kv_put(conn, GATE_KEY, verdict.as_dict(), ts)
    return {
        "tape": tape.receipt(),
        "decisions": decisions,
        "scored": scored,
        "gate": {"status": verdict.status, "failing": verdict.failing[:6]},
        "http_calls": transport.calls,  # type: ignore[attr-defined]
    }


# --------------------------------------------------------------------------------------
# read-only summary (daily report, MCP, CLI)
# --------------------------------------------------------------------------------------


def _table_exists(conn: Any, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _ev(rows: Sequence[ScoredRow], gp: GateParams, eth_usd: Decimal | None) -> dict[str, Any]:
    pnl = [r.pnl_wei for r in rows]
    mean = _mean(pnl)
    ci = cluster_bootstrap_ci([(r.collection, r.pnl_wei) for r in rows], level=gp.ci_level,
                              draws=gp.bootstrap_draws, seed=gp.seed)
    def to_eth(v: float | None) -> Decimal | None:
        return None if v is None else Decimal(str(v)) / ETH

    mean_eth = to_eth(mean)
    return {
        "n": len(rows),
        "collections": len({r.collection for r in rows}),
        "mean_eth": mean_eth,
        "ci_eth": (to_eth(ci[0]), to_eth(ci[1])) if ci else None,
        "mean_usd": (mean_eth * eth_usd).quantize(Decimal("0.01")) if mean_eth is not None and eth_usd else None,
        "mean_best_eth": to_eth(_mean([r.pnl_best_wei for r in rows])),
        "realizable": sum(1 for r in rows if r.exit_basis != "none"),
    }


def summary(conn: Any, *, now: int | None = None, params: StudyParams | None = None) -> dict[str, Any]:
    """Everything the daily report prints. SELECT only: safe on a ``mode=ro`` connection."""
    ts = now if now is not None else now_ms()
    p = params or StudyParams()
    if not _table_exists(conn, "nft_paper_mints"):
        return {"state": "absent"}
    decided = {r["verdict"]: int(r["n"]) for r in fetch_all(
        conn, "SELECT verdict, COUNT(*) AS n FROM nft_paper_mints GROUP BY verdict")}
    decided_24h = {r["verdict"]: int(r["n"]) for r in fetch_all(
        conn, "SELECT verdict, COUNT(*) AS n FROM nft_paper_mints WHERE decided_ms > ? GROUP BY verdict",
        (ts - DAY,))}
    cursor = nft_tape.load_cursor(conn)
    latest = fetch_one(conn, "SELECT eth_usd FROM nft_paper_mints WHERE eth_usd IS NOT NULL "
                             "ORDER BY decided_ms DESC LIMIT 1")
    try:
        eth_usd = Decimal(str(latest["eth_usd"])) if latest else None
    except InvalidOperation:
        eth_usd = None
    mint24 = load_scored(conn, p.gate.horizon_h, "mint")
    shadow24 = load_scored(conn, p.gate.horizon_h, "shadow")
    verdict = gate_now(conn, now=ts, params=p)
    return {
        "state": "ok",
        "rule": RULE_VERSION,
        "decided": decided,
        "decided_24h": decided_24h,
        "tape_covered_h": (round((cursor.through_ts_ms - cursor.first_ts_ms) / H, 1)
                           if cursor and cursor.first_ts_ms and cursor.through_ts_ms else None),
        "tape_lag_min": (round((ts - cursor.through_ts_ms) / 60_000, 1)
                         if cursor and cursor.through_ts_ms else None),
        "ev_mint": _ev(mint24, p.gate, eth_usd),
        "ev_shadow": _ev(shadow24, p.gate, eth_usd),
        "gate": verdict.as_dict(),
    }


def render_lines(s: Mapping[str, Any]) -> list[str]:
    """Two lines for the daily report."""
    if s.get("state") == "absent":
        return ["PAPER MINTS not started (migration 034 not applied)"]
    d, d24 = s.get("decided") or {}, s.get("decided_24h") or {}
    tape = (f"tape {s['tape_covered_h']}h, lag {s['tape_lag_min']}m" if s.get("tape_covered_h") is not None
            else "tape not started")
    lines = [f"PAPER MINTS (robinhood, keyless, spends nothing) {s.get('rule')} | decided 24h mint "
             f"{d24.get('mint', 0)} shadow {d24.get('shadow', 0)} | all mint {d.get('mint', 0)} shadow "
             f"{d.get('shadow', 0)} | {tape}"]

    def ev_text(label: str, ev: Mapping[str, Any]) -> str:
        if not ev.get("n"):
            return f"{label} n 0"
        usd = f" (${ev['mean_usd']:+})" if ev.get("mean_usd") is not None else ""
        ci = (f" 90%CI [{ev['ci_eth'][0]:+.6f}, {ev['ci_eth'][1]:+.6f}]" if ev.get("ci_eth") else "")
        return f"{label} n {ev['n']} mean {ev['mean_eth']:+.6f} ETH{usd}{ci}"

    gate = s.get("gate") or {}
    checks = {c["name"]: c for c in gate.get("checks") or []}
    need = ", ".join(f"{name} {checks[name]['value']}/{checks[name]['need']}"
                     for name in (gate.get("failing") or [])[:3] if name in checks)
    lines.append(f"  @24h {ev_text('mint', s.get('ev_mint') or {})} | {ev_text('shadow', s.get('ev_shadow') or {})}"
                 f" | gate {gate.get('status', '?')}{': ' + need if need else ''}")
    return lines


__all__ = [
    "Decision",
    "Evidence",
    "FillPoint",
    "GATE_VERSION",
    "GateParams",
    "GateVerdict",
    "MARK_VERSION",
    "Mark",
    "MarkParams",
    "RULE_VERSION",
    "RuleParams",
    "ScoredRow",
    "StudyParams",
    "cluster_bootstrap_ci",
    "cluster_bootstrap_diff_ci",
    "decide",
    "evaluate_gate",
    "evidence_from",
    "mark_from",
    "render_lines",
    "run",
    "summary",
]
