"""Deterministic promotion gates. The evaluator, kept outside the agent's write scope.

Every function in this module takes data and returns a verdict. None of them calls a
model, reads a prompt, or asks anything for an opinion. That is not a style preference:
the Darwin Godel Machine work documented self-improving agents *fabricating test logs*
once they could edit the thing that judged them. So the judge is arithmetic, it runs on
recorded rows, and the only write it is ever allowed is a promotion that already passed.

What is implemented exactly, and what is an approximation, stated plainly because a gate
that overstates its own rigour is worse than no gate:

* **PBO via CSCV** — implemented as published (Bailey, Borwein, Lopez de Prado, Zhu):
  even block split, all C(S, S/2) in-sample combinations, logit of the in-sample winner's
  out-of-sample relative rank, PBO = P(logit <= 0). Exact, given the trial matrix.
* **Deflated Sharpe Ratio** — implemented as published (Bailey & Lopez de Prado 2014):
  expected maximum Sharpe under the null from the trial count and the cross-trial Sharpe
  variance, then the PSR of the observed Sharpe against it with skew and kurtosis
  adjustment. When fewer than two comparable trials exist, the deflation term collapses to
  zero and the number degrades to a plain Probabilistic Sharpe Ratio against zero; the
  result says so in ``reasons`` rather than pretending it deflated anything.
* **The replay itself is a *selection* replay, not a market simulation.** It re-runs the
  candidate parameter over the recorded decision stream and keeps the *recorded* outcome
  of the trades that would still have been taken. It does not model the fills we would
  have got on trades we never took, and it cannot: there is no counterfactual price path
  in our own records, and fetching one tonight is not what we would have seen then. Skips
  therefore contribute nothing to the replay. This makes the gate a test of "would a
  tighter filter have kept the good trades", which is the honest question our data can
  answer.
* **Point-in-time** is enforced where it can be: a decision's features are read only from
  signals whose ``created_ms`` is at or before that decision's timestamp. Everything else
  in the replay is the decision's own recorded columns, which by construction existed at
  decision time.

The literature the plan cites is not encouraging about LLM trading agents — StockBench's
best agent beat buy-and-hold by 1.5 points and every agent underperformed in a downturn;
LiveTradeBench found arena score uncorrelated with trading skill. These gates exist to
make sure that if we have no edge, we find out from our own numbers instead of from the
bankroll.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiba.core import journal
from kaiba.core.config import EnvelopeBounds, LaneConfig, get_risk, save_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import EventKind, Lane, now_ms
from kaiba.learning import metrics

log = logging.getLogger(__name__)

EULER_MASCHERONI = 0.5772156649015329

DEFAULT_MIN_TRADES = 30
DEFAULT_SPLITS = 8
DEFAULT_OOS_FRACTION = 0.30
PBO_MAX = 0.50
DSR_MIN = 0.95
MDD_TOLERANCE = Decimal("1.1")
DAY_MS = 86_400_000

#: Decision columns that are legitimate replay features: recorded at decision time, in the
#: decision's own row, so there is no lookahead in reading them.
DECISION_FEATURE_COLUMNS = frozenset(
    {"confidence", "expected_return_pct", "size_pct_bankroll"}
)

#: Actions that put capital at risk. HOLD and SKIP have no recorded outcome to replay.
ENTRY_ACTIONS = ("enter", "scale_in")


class GateResult(BaseModel):
    """A verdict plus the numbers it was reached on. Persisted append-only."""

    model_config = ConfigDict(extra="forbid")

    gate: str
    experiment_id: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    checked_ms: int = Field(default_factory=now_ms)

    def record(self, conn: sqlite3.Connection | None = None) -> None:
        c = conn or get_conn()
        c.execute(
            "INSERT INTO gate_results (experiment_id, gate, passed, reasons_json, metrics_json, "
            "created_ms) VALUES (?,?,?,?,?,?)",
            (
                self.experiment_id,
                self.gate,
                1 if self.passed else 0,
                jdump(self.reasons),
                jdump(self.metrics),
                self.checked_ms,
            ),
        )


# --------------------------------------------------------------------------------------
# statistics: PBO via CSCV, and the deflated Sharpe ratio
# --------------------------------------------------------------------------------------


def _sharpe_raw(values: Sequence[float]) -> float | None:
    """Per-period Sharpe, not annualised. ``None`` if undefined."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def _ranks(values: Sequence[float | None]) -> list[float]:
    """Ascending ranks 1..N with average ranks for ties; ``None`` sorts worst."""
    scored = [(-math.inf if v is None else v, i) for i, v in enumerate(values)]
    order = sorted(range(len(scored)), key=lambda i: (scored[i][0], i))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scored[order[j + 1]][0] == scored[order[i]][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pbo_cscv(matrix: Sequence[Sequence[float]], splits: int = DEFAULT_SPLITS) -> float | None:
    """Probability of Backtest Overfitting by combinatorially symmetric cross-validation.

    ``matrix`` is ``T`` periods by ``N`` configurations. The series are split into ``S``
    contiguous blocks; for every way of choosing ``S/2`` blocks as in-sample, the
    configuration with the best in-sample Sharpe is found and its out-of-sample rank
    computed. PBO is the fraction of splits where that winner lands at or below the
    out-of-sample median — i.e. how often "best in backtest" means nothing.

    ``None`` when the shape cannot support the procedure (fewer than two configurations,
    or fewer periods than blocks).
    """
    rows = [list(r) for r in matrix]
    if not rows:
        return None
    n_configs = len(rows[0])
    t = len(rows)
    if n_configs < 2 or any(len(r) != n_configs for r in rows):
        return None
    s = splits if splits % 2 == 0 else splits - 1
    if s < 2 or t < s:
        return None

    block = t // s
    blocks: list[list[list[float]]] = []
    for i in range(s):
        start = i * block
        end = t if i == s - 1 else (i + 1) * block
        blocks.append(rows[start:end])

    logits: list[float] = []
    for combo in combinations(range(s), s // 2):
        inside = set(combo)
        is_rows = [r for i in combo for r in blocks[i]]
        oos_rows = [r for i in range(s) if i not in inside for r in blocks[i]]
        if len(is_rows) < 2 or len(oos_rows) < 2:
            continue
        is_sr = [_sharpe_raw([r[n] for r in is_rows]) for n in range(n_configs)]
        oos_sr = [_sharpe_raw([r[n] for r in oos_rows]) for n in range(n_configs)]
        best = max(
            range(n_configs), key=lambda n: (-math.inf if is_sr[n] is None else is_sr[n], -n)
        )
        rank = _ranks(oos_sr)[best]
        omega = rank / (n_configs + 1)
        omega = min(max(omega, 1e-9), 1 - 1e-9)
        logits.append(math.log(omega / (1 - omega)))

    if not logits:
        return None
    return sum(1 for x in logits if x <= 0) / len(logits)


def deflated_sharpe(
    returns: Sequence[float],
    trials: int,
    *,
    trial_sharpes: Sequence[float] | None = None,
) -> tuple[float | None, list[str]]:
    """Deflated Sharpe Ratio and any caveats about how it was computed.

    Returns ``(dsr, notes)``. ``dsr`` is a probability: the chance the observed Sharpe is
    genuinely above the expected maximum of ``trials`` independent attempts at nothing.
    """
    notes: list[str] = []
    vals = [float(v) for v in returns]
    n = len(vals)
    if n < 3:
        return None, ["fewer than three observations; DSR not computable"]
    sr = _sharpe_raw(vals)
    if sr is None:
        return None, ["return series has no variance; Sharpe undefined"]

    sr0 = 0.0
    usable_trials = max(int(trials), 1)
    variance = None
    if trial_sharpes and len(trial_sharpes) >= 2:
        ts = [float(v) for v in trial_sharpes]
        mean_ts = sum(ts) / len(ts)
        variance = sum((v - mean_ts) ** 2 for v in ts) / (len(ts) - 1)
    if usable_trials >= 2 and variance and variance > 0:
        nd_hi = metrics.normal_inv_cdf(1 - 1 / usable_trials)
        nd_lo = metrics.normal_inv_cdf(1 - 1 / (usable_trials * math.e))
        sr0 = math.sqrt(variance) * ((1 - EULER_MASCHERONI) * nd_hi + EULER_MASCHERONI * nd_lo)
    else:
        notes.append(
            "fewer than two comparable trials (or no cross-trial variance): the deflation "
            "term is zero, so this is a plain PSR against zero, not a deflated Sharpe"
        )

    skew = metrics.skewness(vals)
    kurt = metrics.kurtosis(vals)
    denom_sq = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom_sq <= 0:
        return None, notes + ["higher-moment denominator is non-positive; DSR not computable"]
    z = (sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom_sq)
    return metrics.normal_cdf(z), notes


# --------------------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------------------


def _num(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            return None
    return None


def _direction(key: str) -> str | None:
    """``ge`` for ``min_*`` thresholds, ``le`` for ``max_*``. Anything else is unsupported."""
    if key.startswith("min_"):
        return "ge"
    if key.startswith("max_"):
        return "le"
    return None


#: Lane thresholds whose feature the lane records under a DIFFERENT name in its signal
#: payload. Without these the replay finds no value and refuses (sm-trenches), or -- worse
#: -- finds the wrong one (below). Each alias is the exact payload key the lane compares
#: against that threshold in ``kaiba/execution/lanes.py``:
#:   sm_trenches:     ``len(smart) < min_smart_degen``        -> payload ``smart_wallets``
#:                    ``entities < min_independent_entities`` -> payload ``entity_count``
#:   confluence_5 / pons_robinhood: ``entities < min_entities`` -> payload ``entity_count``
FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "min_smart_degen": ("smart_wallets",),
    "min_independent_entities": ("entity_count",),
    "min_entities": ("entity_count",),
}
# The entry features sm-trenches and confluence-5 record since 2026-10-03
# (``lanes.ENTRY_FEATURES``) need no alias: every generic key is ``min_<f>``/``max_<f>``
# over the payload key ``<f>`` itself (``lanes.FEATURE_THRESHOLD_KEYS``), which
# :func:`_feature_names` resolves by stripping the prefix. tests/test_entry_features.py
# asserts every lane key resolves to a name the lane actually writes.

#: (lane, key) pairs where the lane reads ``0`` as "not enforced". The replay compares
#: with :func:`_passes`, where 0 is a real floor that refuses an unknown feature, so the
#: incumbent would appear to have refused signals the lane took. Normalised to ``None``.
#:   sm-trenches  ``if min_holders > 0:`` (lanes.sm_trenches); risk.yaml ships 0.
ZERO_IS_OFF: frozenset[tuple[str, str]] = frozenset({("sm-trenches", "min_holder_count")})

#: (lane, key) thresholds whose live rule the replay CANNOT reproduce. Judging them would
#: score a configuration that does not exist, so the replay refuses and says why.
REPLAY_SEMANTICS_DIFFER: dict[tuple[str, str], str] = {
    ("sm-trenches", "min_liquidity_usd"): (
        "sm-trenches waives its pool-liquidity floor on a readable bonding curve and reads "
        "it per chain; a scalar replay over liquidity_usd would refuse signals the lane "
        "admits. Propose max_liquidity_usd, or change the floor by hand"
    ),
    ("sm-trenches", "max_rug_ratio"): (
        "sm-trenches ADMITS an unavailable or stale rug ratio (most signals carry none); "
        "the replay refuses an unknown under any threshold, so it would score a stricter "
        "rule than the one that would run"
    ),
}


def _feature_names(key: str) -> list[str]:
    """Payload / decision names that hold the feature a ``min_*``/``max_*`` key filters on.

    FIXED 2026-10-02: this used to include ``key`` itself. Lanes ECHO their threshold into
    the payload -- confluence-5 and pons-robinhood write ``"min_entities": <threshold>``,
    sm-trenches writes ``"max_rug_ratio": <threshold>`` -- so the replay read the threshold
    in force as every decision's feature, and a candidate either kept all trades or none.
    A payload field named like the threshold IS the threshold, never the measurement.
    """
    names: list[str] = []
    for prefix in ("min_", "max_"):
        if key.startswith(prefix):
            names.append(key[len(prefix) :])
    names.extend(FEATURE_ALIASES.get(key, ()))
    return names


def _feature_value(conn: sqlite3.Connection, decision: Mapping[str, Any], key: str) -> Decimal | None:
    """The feature a threshold applies to, read point-in-time.

    Signals recorded *after* the decision are ignored outright — that is the whole
    lookahead guard, and it is why the gate can only test filters over features the
    decision actually had in front of it.
    """
    return _feature_point(conn, decision, key)[0]


def _feature_point(
    conn: sqlite3.Connection, decision: Mapping[str, Any], key: str
) -> tuple[Decimal | None, bool]:
    """``(value, recorded)`` -- the feature, and whether the decision RECORDED it at all.

    ADDED 2026-10-03 with the lanes' entry features (``lanes.entry_features``). Two kinds
    of "no value" mean opposite things and used to be one ``None``:

    * ``recorded=False`` -- no point-in-time signal payload carries any of the key's
      feature names. The signal PREDATES the feature. Nothing can be said about it; the
      replay leaves it out of both arms rather than letting the candidate "refuse" it.
    * ``recorded=True`` with ``value None`` -- the lane looked and the feature was unknown
      or stale (the payload holds the key with ``null``). Under a configured threshold the
      lane REFUSES such a signal (``lanes.feature_threshold_refusal``), so the replay must
      count it as refused by the candidate -- which is what :func:`_passes` already does.

    Decision columns (``confidence`` ...) and ``strength`` always count as recorded.
    """
    direction = _direction(key)
    names = _feature_names(key)
    ts = int(decision["ts_ms"])
    values: list[Decimal] = []
    recorded = False

    for sid in jload(decision.get("signals_json"), []):
        row = fetch_one(
            conn,
            "SELECT payload_json, strength, created_ms FROM signals WHERE signal_id = ?",
            (str(sid),),
        )
        if row is None or int(row["created_ms"]) > ts:
            continue
        payload = jload(row["payload_json"], {})
        for name in names:
            if isinstance(payload, Mapping) and name in payload:
                recorded = True
                v = _num(payload[name])
                if v is not None:
                    values.append(v)
            if name == "strength":
                recorded = True
                v = _num(row["strength"])
                if v is not None:
                    values.append(v)

    for name in names:
        if name in DECISION_FEATURE_COLUMNS:
            recorded = True
            v = _num(decision.get(name))
            if v is not None:
                values.append(v)

    if not values:
        return None, recorded
    return (max(values) if direction != "le" else min(values)), True


def _passes(feature: Decimal | None, threshold: Decimal | None, direction: str | None) -> bool:
    if threshold is None:
        return True  # "no threshold configured" means the lane took everything
    if feature is None:
        return False
    return feature >= threshold if direction == "ge" else feature <= threshold


def _replay_rows(conn: sqlite3.Connection, lane: str, key: str) -> list[dict[str, Any]]:
    """Recorded entry decisions in a lane that produced a closed trade, with their feature."""
    sql = (
        "SELECT d.decision_id, d.ts_ms, d.signals_json, d.confidence, d.expected_return_pct, "
        "d.size_pct_bankroll, t.trade_id, t.closed_ms, t.pnl_native, t.cost_native, t.pnl_pct "
        "FROM decisions d JOIN trades t ON t.decision_id = d.decision_id "
        f"WHERE d.lane = ? AND d.action IN ({','.join('?' * len(ENTRY_ACTIONS))}) "
        "ORDER BY d.ts_ms ASC, d.decision_id ASC"
    )
    rows = fetch_all(conn, sql, [lane, *ENTRY_ACTIONS])
    out: list[dict[str, Any]] = []
    for r in rows:
        ret = metrics.trade_return(r)
        if ret is None:
            continue
        r["feature"], r["recorded"] = _feature_point(conn, r, key)
        r["return"] = float(ret)
        out.append(r)
    return out


def _incumbent_value(lane: str, key: str, diff: Mapping[str, Any]) -> Decimal | None:
    """The incumbent threshold: the diff's ``old`` if numeric, else the lane's configured one.

    A value the lane reads as "off" (:data:`ZERO_IS_OFF`) is ``None``: no threshold.
    """
    incumbent = _num(diff.get("old"))
    if incumbent is None:
        try:
            incumbent = _num(get_risk().lane(Lane(lane)).params.get(key))
        except (ValueError, KeyError):
            incumbent = None
    if incumbent is not None and incumbent == 0 and (lane, key) in ZERO_IS_OFF:
        return None
    return incumbent


def _mapping_incumbent(lane: str, key: str, diff: Mapping[str, Any]) -> bool:
    """True when the incumbent is a per-chain mapping: one scalar replay cannot stand for it."""
    if isinstance(diff.get("old"), Mapping):
        return True
    try:
        return isinstance(get_risk().lane(Lane(lane)).params.get(key), Mapping)
    except (ValueError, KeyError):
        return False


def _lane_thresholds(conn: sqlite3.Connection, lane: str, key: str, diff: Mapping[str, Any]) -> dict[str, Decimal | None]:
    """Every configuration in the sweep: the incumbent plus each proposed candidate value."""
    configs: dict[str, Decimal | None] = {}
    configs["incumbent"] = _incumbent_value(lane, key, diff)
    for row in fetch_all(
        conn,
        "SELECT experiment_id, diff_json FROM experiments WHERE lane = ? ORDER BY created_ms ASC",
        (lane,),
    ):
        d = jload(row["diff_json"], {})
        if not isinstance(d, Mapping) or d.get("key") != key:
            continue
        v = _num(d.get("new"))
        if v is not None:
            configs[str(row["experiment_id"])] = v
    return configs


def _trial_count(conn: sqlite3.Connection, lane: str) -> int:
    """How many parameter attempts this lane has seen. Deflation needs the honest number.

    Takes the larger of two counts, because each misses a different thing. `experiments`
    holds configurations somebody formally proposed; `lane_trials` holds configurations
    that were actually *run*, recorded automatically at decision time. A lane whose
    thresholds were nudged twenty times in `config/risk.yaml` and proposed twice appears
    as two experiments and twenty-one trials, and deflating by two would inflate the
    result in exactly the flattering direction.
    """
    row = fetch_one(conn, "SELECT COUNT(*) AS n FROM experiments WHERE lane = ?", (lane,))
    declared = int(row["n"]) if row else 0
    try:
        from kaiba.learning.registry import trial_count

        return max(declared, trial_count(lane, conn))
    except Exception as exc:  # noqa: BLE001 - a missing registry must not block a gate
        log.debug("trial registry unavailable, using declared experiments only: %s", exc)
        return declared


def _as_experiment(experiment: Any, conn: sqlite3.Connection) -> dict[str, Any] | None:
    if isinstance(experiment, Mapping):
        return dict(experiment)
    return fetch_one(conn, "SELECT * FROM experiments WHERE experiment_id = ?", (str(experiment),))


def _equity(series: Sequence[float]) -> list[float]:
    out, running = [], 0.0
    for v in series:
        running += v
        out.append(running)
    return out


def _mdd(series: Sequence[float]) -> Decimal:
    curve = _equity(series)
    if not curve:
        return Decimal(0)
    peak, worst = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return Decimal(str(worst))


# --------------------------------------------------------------------------------------
# the relative criterion, for tightenings
#
# OWNER OBJECTIVE (2026-09-24, decided by the lead 2026-10-02): judge a change by whether
# it moves expected value PER TRADE. The absolute rule (DSR >= 0.95 on the candidate's own
# returns) asks whether the candidate is profitable on its own, so on a losing lane NO
# tightening can pass however much it cuts the loss -- MEASURED on the box 2026-10-02:
# min_independent_entities 2 -> 3 moved out-of-sample EV from -8.0% to -5.1% per trade and
# failed on DSR 0.0024. A tightening keeps a SUBSET of the incumbent's decisions, so the
# question it can answer is relative: on the same decisions, does the subset earn more
# per trade than the whole? Anything that is not a tightening keeps the absolute rule.
# --------------------------------------------------------------------------------------

#: Seeded so the same rows always give the same verdict; a gate that flips on re-run is
#: not auditable.
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20261002

#: Distinct UTC days the day-cluster bootstrap needs before it returns a bound at all. The
#: same number as ``kaiba.learning.validation.MIN_BOOTSTRAP_BLOCKS`` (not imported:
#: validation imports this module; a test pins the two equal), for the reason given there:
#: with few blocks the bootstrap sees almost no between-block spread, reports a tiny
#: interval, and so fails towards confidence -- the direction that moves capital.
MIN_CLUSTER_DAYS = 20

#: Default ceiling on the share of the lane's decided signals a tightening may remove
#: (``experiment_gates.max_auto_refused_share`` passes its own value).
MAX_REMOVAL_SHARE = 0.5


def is_tightening(candidate: Decimal | None, incumbent: Decimal | None, direction: str | None) -> bool:
    """True when ``candidate`` admits a strict subset of what ``incumbent`` admits.

    ``None`` incumbent means "no threshold configured; the lane took everything", so any
    threshold is a tightening of it. Equal is not a tightening: it changes nothing.
    """
    if candidate is None or direction not in ("ge", "le"):
        return False
    if incumbent is None:
        return True
    return candidate > incumbent if direction == "ge" else candidate < incumbent


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))]


def paired_improvement(
    observations: Sequence[tuple[int, float, bool]],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
    min_days: int = MIN_CLUSTER_DAYS,
) -> dict[str, Any]:
    """EV-per-trade improvement of a subset over its whole stream, with a bootstrap bound.

    ``observations`` is the INCUMBENT's stream: ``(ts_ms, return, kept_by_candidate)`` per
    decision. ``delta = mean(return | kept) - mean(return)``.

    The bound is a PAIRED, DAY-CLUSTERED percentile bootstrap: each draw resamples whole UTC
    days of the incumbent's stream with replacement -- kept and refused decisions of a day
    together -- and recomputes both means from that one resample, so the two arms are never
    resampled independently (paired), and decisions sharing a day are never treated as
    independent (clustered). Same-chain returns share a market factor (rho +0.22, measured
    2026-09-21) and our trades bunch: 101 sm-trenches fills landed on 09-22 alone. Not
    stratified: the replay stream mixes chains, but in RETURN terms, which are comparable.
    ``ci_lo``/``ci_hi`` are the 2.5th/97.5th percentiles (two-sided 95%). Fewer than
    ``min_days`` days: no bound (``unevaluable``), never a narrow one.

    ``ci_lo_trade_level`` -- the same paired bootstrap with the single decision as the
    unit -- is recorded as INFORMATION only: it ignores the clustering, so it is narrower.
    """
    days: dict[int, list[float]] = {}
    flat: list[tuple[float, bool]] = []
    for ts_ms, value, kept in observations:
        v = float(value)
        block = days.setdefault(int(ts_ms) // DAY_MS, [0.0, 0.0, 0.0, 0.0])
        block[0] += v
        block[1] += 1
        if kept:
            block[2] += v
            block[3] += 1
        flat.append((v, bool(kept)))
    n_all = int(sum(b[1] for b in days.values()))
    n_kept = int(sum(b[3] for b in days.values()))
    out: dict[str, Any] = {
        "method": "paired bootstrap, UTC-day clusters, percentile 95%",
        "n_incumbent": n_all, "n_candidate": n_kept, "days": len(days),
        "delta": None, "ci_lo": None, "ci_hi": None,
        "ci_lo_trade_level": None, "ci_hi_trade_level": None, "unevaluable": None,
    }
    if n_all == 0 or n_kept == 0:
        out["unevaluable"] = "the candidate keeps no decision of the incumbent's stream"
        return out
    out["delta"] = sum(b[2] for b in days.values()) / n_kept - sum(b[0] for b in days.values()) / n_all

    rng_trade = random.Random(seed + 1)
    trade_stats: list[float] = []
    for _ in range(max(draws, 2)):
        sa = na = sk = nk = 0.0
        for _ in range(len(flat)):
            v, k = flat[rng_trade.randrange(len(flat))]
            sa += v
            na += 1
            if k:
                sk += v
                nk += 1
        if nk:
            trade_stats.append(sk / nk - sa / na)
    if trade_stats:
        trade_stats.sort()
        out["ci_lo_trade_level"] = _percentile(trade_stats, 0.025)
        out["ci_hi_trade_level"] = _percentile(trade_stats, 0.975)

    if len(days) < min_days:
        out["unevaluable"] = (f"{len(days)} distinct UTC days; the day-cluster bootstrap needs "
                              f">= {min_days} before it will bound anything")
        return out
    blocks = [days[k] for k in sorted(days)]
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(max(draws, 2)):
        sa = na = sk = nk = 0.0
        for _ in range(len(blocks)):
            b = blocks[rng.randrange(len(blocks))]
            sa += b[0]
            na += b[1]
            sk += b[2]
            nk += b[3]
        if nk:
            stats.append(sk / nk - sa / na)
    if len(stats) < max(draws, 2) // 2:
        out["unevaluable"] = f"only {len(stats)} of {draws} resamples kept any candidate decision"
        return out
    stats.sort()
    out["ci_lo"] = _percentile(stats, 0.025)
    out["ci_hi"] = _percentile(stats, 0.975)
    return out


def removal_share(
    conn: sqlite3.Connection,
    lane: str,
    key: str,
    incumbent: Decimal | None,
    candidate: Decimal,
    direction: str,
    *,
    since_ms: int,
    limit: int = 20_000,
) -> dict[str, Any]:
    """Share of the lane's DECIDED SIGNALS (skips included) a tightening would remove.

    Owner directive 2026-09-24: measure a filter on the population we scan, not only on
    the fills we took -- a holder floor and a launchpad blocklist judged on fills removed
    98% and 87.5% of the scanned population. Denominator: distinct signals the lane decided
    on since ``since_ms`` that have a point-in-time feature AND pass the incumbent. One row
    per signal: a launchpad twin repeats its skip's signal and is counted once.
    """
    rows = fetch_all(
        conn,
        "SELECT decision_id, ts_ms, signals_json, confidence, expected_return_pct, size_pct_bankroll "
        "FROM decisions WHERE lane = ? AND ts_ms >= ? ORDER BY ts_ms DESC LIMIT ?",
        (lane, int(since_ms), int(limit)),
    )
    seen: set[str] = set()
    measured = removed = missing = unknown = 0
    for r in rows:
        sig = str(r.get("signals_json") or "")
        ident = sig if sig not in ("", "[]") else str(r["decision_id"])
        if ident in seen:
            continue
        seen.add(ident)
        feature, recorded = _feature_point(conn, r, key)
        if not recorded:
            missing += 1  # the signal predates the feature: nothing to say about it
            continue
        if not _passes(feature, incumbent, direction):
            continue  # the incumbent refuses it too; the candidate removes nothing here
        measured += 1
        if feature is None:
            # RECORDED as unknown. A configured threshold refuses it live
            # (lanes.feature_threshold_refusal), so it counts as removed: this is the
            # holder-floor trap -- 85.9% of scanned tokens had no holder count -- and the
            # share must show it rather than quietly dividing it away.
            unknown += 1
        if not _passes(feature, candidate, direction):
            removed += 1
    return {"signals": len(seen), "measured": measured, "removed": removed,
            "missing_feature": missing, "recorded_unknown": unknown,
            "share": (removed / measured) if measured else None}


def replay_gate(
    experiment: Any,
    conn: sqlite3.Connection | None = None,
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    splits: int = DEFAULT_SPLITS,
    oos_fraction: float = DEFAULT_OOS_FRACTION,
    max_removal_share: float = MAX_REMOVAL_SHARE,
    record: bool = True,
) -> GateResult:
    """Offline replay of a candidate parameter over the recorded decision stream.

    Pass requires all of: PBO < 0.5, DSR >= 0.95, out-of-sample expectancy at or above the
    incumbent's, max drawdown no worse than 1.1x the incumbent's, and at least
    ``min_trades`` candidate trades in the window. Any statistic that cannot be computed is
    a failure, not a pass — an unmeasurable candidate does not get capital.
    """
    c = conn or get_conn()
    row = _as_experiment(experiment, c)
    if row is None:
        return GateResult(gate="replay", experiment_id=str(experiment), passed=False,
                          reasons=["experiment not found"])
    exp_id = str(row["experiment_id"])
    diff = jload(row.get("diff_json"), {})
    reasons: list[str] = []
    m: dict[str, Any] = {}

    lane = str(diff.get("lane") or row.get("lane") or "")
    key = str(diff.get("key") or "")
    new_value = _num(diff.get("new"))
    direction = _direction(key)

    if not lane or not key:
        return GateResult(gate="replay", experiment_id=exp_id, passed=False,
                          reasons=["diff does not name a lane and a key"])
    if new_value is None:
        return GateResult(
            gate="replay", experiment_id=exp_id, passed=False,
            reasons=[f"candidate value {diff.get('new')!r} is not numeric; no replay rule exists"],
        )
    if direction is None:
        return GateResult(
            gate="replay", experiment_id=exp_id, passed=False,
            reasons=[
                f"no replay rule for key {key!r}: only min_*/max_* thresholds can be re-run "
                "over the recorded stream"
            ],
        )

    differ = REPLAY_SEMANTICS_DIFFER.get((lane, key))
    if differ:
        return GateResult(gate="replay", experiment_id=exp_id, passed=False,
                          reasons=[f"no faithful replay for {lane}.{key}: {differ}"])
    if _mapping_incumbent(lane, key, diff):
        return GateResult(
            gate="replay", experiment_id=exp_id, passed=False,
            reasons=[f"{lane}.{key} is configured per chain; a replay compares one scalar "
                     "threshold, so it would judge a configuration that is not running"],
        )

    all_rows = _replay_rows(c, lane, key)
    # Decisions whose signal predates the feature (``recorded`` False) are left out of BOTH
    # arms: the candidate did not "refuse" them, nobody measured them. Before 2026-10-03
    # they were counted as missing; for a feature the lanes only started recording that
    # day, every older decision would otherwise read as refused by any threshold.
    rows = [r for r in all_rows if r.get("recorded", True)]
    m["decisions_replayed"] = len(rows)
    m["unrecorded_feature"] = len(all_rows) - len(rows)
    missing = sum(1 for r in rows if r["feature"] is None)
    m["missing_feature"] = missing
    if not all_rows:
        return GateResult(gate="replay", experiment_id=exp_id, passed=False,
                          reasons=["no recorded entry decisions with closed trades for this lane"])
    if not rows:
        return GateResult(
            gate="replay", experiment_id=exp_id, passed=False,
            reasons=[f"0/{len(all_rows)} decisions recorded a point-in-time value for {key!r} "
                     f"(feature names {_feature_names(key)}): the lane did not record it when "
                     "these trades were decided"],
        )
    if missing / len(rows) > 0.5:
        return GateResult(
            gate="replay", experiment_id=exp_id, passed=False,
            reasons=[f"{missing}/{len(rows)} decisions have no point-in-time value for {key!r}"],
        )

    configs = _lane_thresholds(c, lane, key, diff)
    configs[exp_id] = new_value
    names = list(configs)
    cand_idx = names.index(exp_id)
    inc_idx = names.index("incumbent")

    matrix: list[list[float]] = []
    taken: dict[str, list[float]] = {n: [] for n in names}
    for r in rows:
        line: list[float] = []
        for n in names:
            if _passes(r["feature"], configs[n], direction):
                line.append(r["return"])
                taken[n].append(r["return"])
            else:
                line.append(0.0)
        matrix.append(line)

    cand_series = [line[cand_idx] for line in matrix]
    inc_series = [line[inc_idx] for line in matrix]
    m["candidate_trades"] = len(taken[exp_id])
    m["incumbent_trades"] = len(taken["incumbent"])
    m["configs"] = len(names)

    # ---- out-of-sample split: the tail of the window, walk-forward style
    cut = max(1, int(len(rows) * (1 - oos_fraction)))
    oos_cand = [r["return"] for r in rows[cut:] if _passes(r["feature"], new_value, direction)]
    oos_inc = [
        r["return"] for r in rows[cut:] if _passes(r["feature"], configs["incumbent"], direction)
    ]
    cand_exp = sum(oos_cand) / len(oos_cand) if oos_cand else None
    inc_exp = sum(oos_inc) / len(oos_inc) if oos_inc else None
    m["oos_candidate_trades"] = len(oos_cand)
    m["oos_candidate_expectancy"] = cand_exp
    m["oos_incumbent_expectancy"] = inc_exp

    cand_mdd = _mdd(cand_series)
    inc_mdd = _mdd(inc_series)
    m["candidate_max_drawdown"] = float(cand_mdd)
    m["incumbent_max_drawdown"] = float(inc_mdd)

    pbo = pbo_cscv(matrix, splits)
    m["pbo"] = pbo

    trial_sharpes = [s for s in (_sharpe_raw([line[i] for line in matrix]) for i in range(len(names))) if s is not None]
    trials = max(len(names), _trial_count(c, lane))
    dsr, dsr_notes = deflated_sharpe(cand_series, trials, trial_sharpes=trial_sharpes)
    m["dsr"] = dsr
    m["trials"] = trials
    m["candidate_sharpe"] = _sharpe_raw(cand_series)
    m["incumbent_sharpe"] = _sharpe_raw(inc_series)

    if is_tightening(new_value, configs["incumbent"], direction):
        m["criterion"] = "relative"
        reasons.extend(f"info: {n}" for n in dsr_notes)
        ok = _relative_replay_verdict(
            c, rows[cut:], lane=lane, key=key, incumbent=configs["incumbent"], candidate=new_value,
            direction=direction, since_ms=int(rows[0]["ts_ms"]), pbo=pbo, dsr=dsr,
            cand_mdd=cand_mdd, inc_mdd=inc_mdd, min_trades=min_trades,
            max_removal_share=max_removal_share, reasons=reasons, m=m,
        )
        result = GateResult(gate="replay", experiment_id=exp_id, passed=ok, reasons=reasons,
                            metrics=json.loads(jdump(m)))
        if record:
            result.record(c)
        return result

    m["criterion"] = "absolute"
    reasons.extend(dsr_notes)
    ok = True
    if len(taken[exp_id]) < min_trades:
        ok = False
        reasons.append(f"candidate took {len(taken[exp_id])} trades, need >= {min_trades}")
    if pbo is None:
        ok = False
        reasons.append("PBO not computable for this shape; treated as a failure")
    elif pbo >= PBO_MAX:
        ok = False
        reasons.append(f"PBO {pbo:.3f} >= {PBO_MAX}: the in-sample winner does not hold up")
    if dsr is None:
        ok = False
        reasons.append("DSR not computable; treated as a failure")
    elif dsr < DSR_MIN:
        ok = False
        reasons.append(f"DSR {dsr:.4f} < {DSR_MIN}")
    if cand_exp is None:
        ok = False
        reasons.append("candidate took no out-of-sample trades")
    elif inc_exp is not None and cand_exp < inc_exp:
        ok = False
        reasons.append(f"OOS expectancy {cand_exp:.6f} below incumbent {inc_exp:.6f}")
    if cand_mdd > inc_mdd * MDD_TOLERANCE:
        ok = False
        reasons.append(
            f"max drawdown {float(cand_mdd):.6f} exceeds 1.1x incumbent {float(inc_mdd):.6f}"
        )
    if ok:
        reasons.append("all replay criteria met")

    result = GateResult(gate="replay", experiment_id=exp_id, passed=ok, reasons=reasons, metrics=m)
    if record:
        result.record(c)
    return result


def _relative_replay_verdict(
    conn: sqlite3.Connection,
    oos_rows: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    key: str,
    incumbent: Decimal | None,
    candidate: Decimal,
    direction: str,
    since_ms: int,
    pbo: float | None,
    dsr: float | None,
    cand_mdd: Decimal,
    inc_mdd: Decimal,
    min_trades: int,
    max_removal_share: float,
    reasons: list[str],
    m: dict[str, Any],
) -> bool:
    """The tightening verdict. All must hold; absolute DSR is recorded, never required.

    (a) PBO < 0.5 over the sweep;
    (b) out-of-sample EV per trade of the candidate's subset minus the incumbent's, on the
        SAME out-of-sample decisions, is > 0 and its paired day-cluster bootstrap 95% lower
        bound is > 0 (:func:`paired_improvement`);
    (c) the candidate keeps >= ``min_trades`` out-of-sample trades;
    (d) it removes <= ``max_removal_share`` of the lane's decided signals (:func:`removal_share`);
    and, kept from the absolute rule because only DSR was replaced: the candidate's max
    drawdown is no worse than 1.1x the incumbent's on the same stream.
    """
    ok = True
    if pbo is None:
        ok = False
        reasons.append("PBO not computable for this shape; treated as a failure")
    elif pbo >= PBO_MAX:
        ok = False
        reasons.append(f"PBO {pbo:.3f} >= {PBO_MAX}: the in-sample winner does not hold up")

    stream = [
        (int(r["ts_ms"]), float(r["return"]), _passes(r["feature"], candidate, direction))
        for r in oos_rows
        if _passes(r["feature"], incumbent, direction)
    ]
    imp = paired_improvement(stream)
    m["oos_improvement"] = imp
    delta, lo = imp["delta"], imp["ci_lo"]
    if delta is None:
        ok = False
        reasons.append(f"OOS improvement not computable: {imp['unevaluable']}")
    elif delta <= 0:
        ok = False
        reasons.append(f"OOS EV per trade {delta:+.6f} vs the incumbent: the subset does not earn more")
    elif lo is None:
        ok = False
        reasons.append(f"OOS improvement {delta:+.6f} cannot be bounded: {imp['unevaluable']}")
    elif lo <= 0:
        ok = False
        reasons.append(
            f"OOS improvement {delta:+.6f} per trade, but its 95% lower bound {lo:+.6f} is not "
            "above zero (paired day-cluster bootstrap)"
        )

    n_oos = imp["n_candidate"]
    if n_oos < min_trades:
        ok = False
        reasons.append(f"candidate took {n_oos} out-of-sample trades, need >= {min_trades}")

    share = removal_share(conn, lane, key, incumbent, candidate, direction, since_ms=since_ms)
    m["removal"] = share
    if share["share"] is None:
        ok = False
        reasons.append("removal share not measurable: no decided signal carries the feature")
    elif share["share"] > max_removal_share:
        ok = False
        reasons.append(
            f"removes {share['removed']}/{share['measured']} ({share['share']:.0%}) of the lane's "
            f"decided signals, above the {max_removal_share:.0%} ceiling"
        )

    if cand_mdd > inc_mdd * MDD_TOLERANCE:
        ok = False
        reasons.append(
            f"max drawdown {float(cand_mdd):.6f} exceeds 1.1x incumbent {float(inc_mdd):.6f}"
        )

    reasons.append(
        f"info: absolute DSR {dsr:.4f}" if dsr is not None else "info: absolute DSR not computable"
    )
    reasons[-1] += " -- recorded, not a pass condition for a tightening (judged on EV per trade)"
    if ok:
        reasons.append("all relative replay criteria met")
    return ok


# --------------------------------------------------------------------------------------
# shadow
# --------------------------------------------------------------------------------------


def _arm_trades(conn: sqlite3.Connection, experiment_id: str, arm: str) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        "SELECT t.* FROM trades t JOIN experiment_trades x ON x.trade_id = t.trade_id "
        "WHERE x.experiment_id = ? AND x.arm = ? ORDER BY t.closed_ms ASC, t.trade_id ASC",
        (experiment_id, arm),
    )
    if rows:
        return rows
    if arm != "candidate":
        return []
    # Fallback: a shadow runner that tagged params_version with the experiment id.
    return fetch_all(
        conn,
        "SELECT * FROM trades WHERE params_version = ? ORDER BY closed_ms ASC, trade_id ASC",
        (experiment_id,),
    )


def _incumbent_over_span(
    conn: sqlite3.Connection, lane: str, start_ms: int, end_ms: int, exclude: set[str]
) -> list[dict[str, Any]]:
    rows = fetch_all(
        conn,
        "SELECT * FROM trades WHERE lane = ? AND closed_ms >= ? AND closed_ms <= ? "
        "ORDER BY closed_ms ASC, trade_id ASC",
        (lane, start_ms, end_ms),
    )
    return [r for r in rows if str(r["trade_id"]) not in exclude]


def shadow_gate(
    experiment: Any,
    conn: sqlite3.Connection | None = None,
    *,
    min_days: int = 14,
    min_trades: int = 50,
    record: bool = True,
) -> GateResult:
    """Compare the candidate's shadow trades against the incumbent's over the same stream.

    Shadow fills are optimistic by nature — no queue position, no market impact, and a
    memecoin's book is thin enough that this matters. The gate therefore asks only for a
    *relative* verdict over the same candidate stream and the same window, and demands
    enough calendar time that one lucky day cannot carry it.
    """
    c = conn or get_conn()
    row = _as_experiment(experiment, c)
    if row is None:
        return GateResult(gate="shadow", experiment_id=str(experiment), passed=False,
                          reasons=["experiment not found"])
    exp_id = str(row["experiment_id"])
    diff = jload(row.get("diff_json"), {})
    lane = str(diff.get("lane") or row.get("lane") or "")

    cand = _arm_trades(c, exp_id, "candidate")
    reasons: list[str] = []
    m: dict[str, Any] = {"candidate_trades": len(cand)}
    if not cand:
        return GateResult(
            gate="shadow", experiment_id=exp_id, passed=False,
            reasons=["no shadow trades are labelled for this candidate; nothing to compare"],
            metrics=m,
        )

    start_ms = min(int(t["closed_ms"]) for t in cand)
    end_ms = max(int(t["closed_ms"]) for t in cand)
    span_days = (end_ms - start_ms) / DAY_MS
    m["span_days"] = round(span_days, 3)

    key = str(diff.get("key") or "")
    direction = _direction(key)
    new_value = _num(diff.get("new"))
    if lane and is_tightening(new_value, _incumbent_value(lane, key, diff), direction):
        return _relative_shadow_verdict(
            c, exp_id, cand, span_days=span_days, min_days=min_days, min_trades=min_trades,
            m=m, record=record,
        )

    m["criterion"] = "absolute"
    inc = _arm_trades(c, exp_id, "incumbent")
    if not inc and lane:
        inc = _incumbent_over_span(c, lane, start_ms, end_ms, {str(t["trade_id"]) for t in cand})
    m["incumbent_trades"] = len(inc)

    cand_exp = metrics.expectancy(cand)
    inc_exp = metrics.expectancy(inc)
    cand_mdd = metrics.max_drawdown(
        [(int(t["closed_ms"]), v) for t, v in zip(cand, metrics.cumulative_pnl(cand), strict=True)]
    )
    inc_mdd = metrics.max_drawdown(
        [(int(t["closed_ms"]), v) for t, v in zip(inc, metrics.cumulative_pnl(inc), strict=True)]
    ) if inc else None
    m["candidate_expectancy"] = cand_exp
    m["incumbent_expectancy"] = inc_exp
    m["candidate_max_drawdown"] = cand_mdd
    m["incumbent_max_drawdown"] = inc_mdd
    m["candidate_win_rate"] = metrics.win_rate(cand)
    m["incumbent_win_rate"] = metrics.win_rate(inc)

    ok = True
    if len(cand) < min_trades:
        ok = False
        reasons.append(f"{len(cand)} shadow trades, need >= {min_trades}")
    if span_days < min_days:
        ok = False
        reasons.append(f"shadow ran {span_days:.1f} days, need >= {min_days}")
    if not inc:
        ok = False
        reasons.append("no incumbent trades over the same span; a one-armed comparison is not one")
    else:
        if cand_exp is None or inc_exp is None:
            ok = False
            reasons.append("expectancy not computable for one of the arms")
        elif cand_exp < inc_exp:
            ok = False
            reasons.append(f"candidate expectancy {cand_exp} below incumbent {inc_exp}")
        if cand_mdd is not None and inc_mdd is not None and cand_mdd > inc_mdd * MDD_TOLERANCE:
            ok = False
            reasons.append(f"candidate drawdown {cand_mdd} exceeds 1.1x incumbent {inc_mdd}")
    if ok:
        reasons.append("all shadow criteria met")

    result = GateResult(
        gate="shadow", experiment_id=exp_id, passed=ok, reasons=reasons,
        metrics=json.loads(jdump(m)),
    )
    if record:
        result.record(c)
    return result


def _relative_shadow_verdict(
    conn: sqlite3.Connection,
    exp_id: str,
    cand: list[dict[str, Any]],
    *,
    span_days: float,
    min_days: int,
    min_trades: int,
    m: dict[str, Any],
    record: bool,
) -> GateResult:
    """The tightening verdict on forward trades: the replay's relative criterion, forward.

    The candidate arm is the forward trades the candidate would have taken; the incumbent
    arm (as ``kaiba.learning.experiment_loop`` labels it) is the ones it would have REFUSED.
    The incumbent's stream is their union, and the test is the same paired day-cluster
    bound on ``mean(candidate) - mean(stream)`` per trade, in RETURN terms. Only labelled
    arms count: the unlabelled-span fallback of the absolute path would mix in trades of
    other chains and modes, and a relative claim needs to know which decisions were refused.
    Kept minimums: >= ``min_trades`` candidate trades over >= ``min_days`` days.
    """
    m["criterion"] = "relative"
    reasons: list[str] = []
    refused = fetch_all(
        conn,
        "SELECT t.* FROM trades t JOIN experiment_trades x ON x.trade_id = t.trade_id "
        "WHERE x.experiment_id = ? AND x.arm = 'incumbent' ORDER BY t.closed_ms ASC, t.trade_id ASC",
        (exp_id,),
    )
    m["incumbent_trades"] = len(refused)
    ok = True
    if len(cand) < min_trades:
        ok = False
        reasons.append(f"{len(cand)} shadow trades, need >= {min_trades}")
    if span_days < min_days:
        ok = False
        reasons.append(f"shadow ran {span_days:.1f} days, need >= {min_days}")
    if not refused:
        ok = False
        reasons.append("no refused trades are labelled: a relative comparison needs both arms")

    def _obs(rows: list[dict[str, Any]], kept: bool) -> list[tuple[int, float, bool]]:
        out = []
        for t in rows:
            ret = metrics.trade_return(t)
            if ret is not None:
                out.append((int(t["opened_ms"]), float(ret), kept))
        return out

    stream = _obs(cand, True) + _obs(refused, False)
    imp = paired_improvement(stream)
    m["improvement"] = imp
    delta, lo = imp["delta"], imp["ci_lo"]
    if delta is None:
        ok = False
        reasons.append(f"improvement not computable: {imp['unevaluable']}")
    elif delta <= 0:
        ok = False
        reasons.append(f"forward EV per trade {delta:+.6f} vs the incumbent's stream: no improvement")
    elif lo is None:
        ok = False
        reasons.append(f"forward improvement {delta:+.6f} cannot be bounded: {imp['unevaluable']}")
    elif lo <= 0:
        ok = False
        reasons.append(
            f"forward improvement {delta:+.6f} per trade, but its 95% lower bound {lo:+.6f} is "
            "not above zero (paired day-cluster bootstrap)"
        )

    # Drawdown in return terms against the WHOLE incumbent stream (candidate + refused,
    # time-ordered) -- not against the refused complement, which is a different question.
    by_time = sorted(cand + refused, key=lambda t: (int(t["closed_ms"]), str(t["trade_id"])))
    cand_ids = {str(t["trade_id"]) for t in cand}
    cand_series = [float(metrics.trade_return(t) or 0) for t in by_time if str(t["trade_id"]) in cand_ids]
    all_series = [float(metrics.trade_return(t) or 0) for t in by_time]
    cand_mdd, inc_mdd = _mdd(cand_series), _mdd(all_series)
    m["candidate_return_drawdown"] = float(cand_mdd)
    m["incumbent_return_drawdown"] = float(inc_mdd)
    if cand_mdd > inc_mdd * MDD_TOLERANCE:
        ok = False
        reasons.append(f"candidate drawdown {float(cand_mdd):.6f} exceeds 1.1x the incumbent "
                       f"stream's {float(inc_mdd):.6f}")
    if ok:
        reasons.append("all relative shadow criteria met")
    result = GateResult(gate="shadow", experiment_id=exp_id, passed=ok, reasons=reasons,
                        metrics=json.loads(jdump(m)))
    if record:
        result.record(conn)
    return result


# --------------------------------------------------------------------------------------
# promotion and rollback
# --------------------------------------------------------------------------------------


def _set_status(
    conn: sqlite3.Connection, exp_id: str, status: str, *, by: str, notes: str,
    replay: GateResult | None = None, shadow: GateResult | None = None,
) -> None:
    conn.execute(
        "UPDATE experiments SET status = ?, decided_ms = ?, decided_by = ?, notes = ?, "
        "replay_json = COALESCE(?, replay_json), shadow_json = COALESCE(?, shadow_json) "
        "WHERE experiment_id = ?",
        (
            status,
            now_ms(),
            by,
            notes[:2000],
            jdump(replay.model_dump(mode="json")) if replay else None,
            jdump(shadow.model_dump(mode="json")) if shadow else None,
            exp_id,
        ),
    )


def promote(
    experiment: Any,
    conn: sqlite3.Connection | None = None,
    *,
    max_removal_share: float = MAX_REMOVAL_SHARE,
) -> bool:
    """Write a candidate's parameters into ``config/risk.yaml`` — if, and only if.

    Both gates must pass *in this call*: they are re-run here rather than read from a
    stored verdict, because a stored verdict is a row the agent could write. The operator's
    ``bounds.allow_self_promotion`` must be true. The parameter lands in the lane's
    ``params`` block and nowhere else; ``save_risk`` then re-reads ``bounds`` from disk, so
    even a diff that names a bounds key by its own name cannot widen the envelope.

    The previous parameter block is written into the journal ``change`` entry, which is
    what :func:`rollback` restores from.
    """
    c = conn or get_conn()
    row = _as_experiment(experiment, c)
    if row is None:
        log.warning("promote: experiment %r not found", experiment)
        return False
    exp_id = str(row["experiment_id"])
    diff = jload(row.get("diff_json"), {})
    lane_str = str(diff.get("lane") or row.get("lane") or "")
    key = str(diff.get("key") or "")
    new_value = diff.get("new")

    try:
        lane = Lane(lane_str)
    except ValueError:
        _set_status(c, exp_id, "rejected", by="gates", notes=f"unknown lane {lane_str!r}")
        return False

    if key in EnvelopeBounds.model_fields or key in {"bounds", "mode"}:
        reason = f"key {key!r} is operator-owned; the envelope is not self-promotable"
        _set_status(c, exp_id, "rejected", by="gates", notes=reason)
        journal.append("change", f"promotion refused for {exp_id}: {reason}", subject=exp_id, conn=c)
        return False

    risk = get_risk()
    if not risk.bounds.allow_self_promotion:
        reason = "bounds.allow_self_promotion is false; the operator has withheld promotion"
        _set_status(c, exp_id, "rejected", by="gates", notes=reason)
        journal.append("change", f"promotion refused for {exp_id}: {reason}", subject=exp_id, conn=c)
        return False

    replay = replay_gate(row, c, max_removal_share=max_removal_share)
    shadow = shadow_gate(row, c)
    if not (replay.passed and shadow.passed):
        reason = "; ".join(replay.reasons + shadow.reasons)
        _set_status(c, exp_id, "rejected", by="gates", notes=reason, replay=replay, shadow=shadow)
        journal.append(
            "change", f"promotion refused for {exp_id}: {reason}"[:4000], subject=exp_id, conn=c
        )
        return False

    lane_cfg = risk.lanes.get(lane) or LaneConfig()
    previous = dict(lane_cfg.params)
    updated = dict(previous)
    updated[key] = new_value
    risk.lanes[lane] = lane_cfg.model_copy(update={"params": updated})
    save_risk(risk)

    evidence = {
        "experiment_id": exp_id,
        "lane": lane.value,
        "key": key,
        "old": previous.get(key),
        "new": new_value,
        "previous_params": previous,
        "new_params": updated,
        "replay": {"passed": replay.passed, "metrics": replay.metrics, "reasons": replay.reasons},
        "shadow": {"passed": shadow.passed, "metrics": shadow.metrics, "reasons": shadow.reasons},
    }
    journal.append(
        "change",
        f"promoted {exp_id}: {lane.value}.{key} {previous.get(key)!r} -> {new_value!r}\n"
        + jdump(evidence),
        subject=exp_id,
        refs=[exp_id],
        conn=c,
    )
    _set_status(c, exp_id, "promoted", by="gates", notes="replay+shadow passed",
                replay=replay, shadow=shadow)
    emit(
        EventKind.PARAM_CHANGE,
        {"experiment_id": exp_id, "lane": lane.value, "key": key, "new": new_value},
        conn=c,
    )
    return True


def rollback(experiment_id: str, conn: sqlite3.Connection | None = None) -> bool:
    """Restore the promoted key's previous value, as recorded in the promotion's journal entry.

    The journal is the source of truth here rather than a mutable "previous value" column,
    because the journal is hash-chained: a rollback target that was tampered with fails
    :func:`kaiba.core.journal.verify`.

    KEY-SCOPED since 2026-10-02. It used to write the whole recorded ``previous_params``
    block back, which silently reverted every OTHER parameter of the lane changed since the
    promotion -- e.g. an operator edit of ``live_launchpads_by_chain`` (the Pons-only live
    allowlist) would vanish under a rollback of an unrelated threshold. Now only the
    promoted key is restored, and only while it still holds the promoted value: if someone
    has changed it since, the promotion is already superseded and a rollback would
    overwrite a newer decision, so it refuses.
    """
    c = conn or get_conn()
    entry = None
    for row in journal.read(limit=500, kind="change", subject=str(experiment_id), conn=c):
        if "previous_params" in row["body"]:
            entry = row
            break
    if entry is None:
        log.warning("rollback: no promotion entry for %s", experiment_id)
        return False
    brace = entry["body"].find("{")
    if brace < 0:
        return False
    try:
        evidence = json.loads(entry["body"][brace:])
    except json.JSONDecodeError:
        return False

    try:
        lane = Lane(str(evidence["lane"]))
    except (KeyError, ValueError):
        return False

    risk = get_risk()
    lane_cfg = risk.lanes.get(lane) or LaneConfig()
    previous = dict(evidence.get("previous_params") or {})
    key = str(evidence.get("key") or "")
    if key:
        params = dict(lane_cfg.params)
        if params.get(key) != evidence.get("new"):
            log.warning(
                "rollback: %s.%s is %r, not the promoted %r; superseded, not rolling back",
                lane.value, key, params.get(key), evidence.get("new"),
            )
            return False
        if key in previous:
            params[key] = previous[key]
        else:
            params.pop(key, None)
    else:  # a promotion entry written before entries named their key
        params = previous
    risk.lanes[lane] = lane_cfg.model_copy(update={"params": params})
    save_risk(risk)

    journal.append(
        "correction",
        f"rolled back {experiment_id}: restored {lane.value}.{key or 'params'} from journal "
        f"seq {entry['seq']}",
        subject=str(experiment_id),
        refs=[str(entry["entry_hash"])],
        conn=c,
    )
    _set_status(c, str(experiment_id), "rejected", by="rollback", notes="rolled back")
    emit(
        EventKind.PARAM_CHANGE,
        {"experiment_id": str(experiment_id), "lane": lane.value, "rollback": True},
        conn=c,
    )
    return True


# --------------------------------------------------------------------------------------
# exploration budget
# --------------------------------------------------------------------------------------


def allocator(
    lanes_stats: Mapping[Any, Any],
    *,
    draws: int = 2000,
    seed: int = 20260920,
    prior_wins: float = 1.0,
    prior_losses: float = 1.0,
) -> dict[Lane, float]:
    """Sliding-window Thompson sampling over lane outcomes -> paper-capital weights.

    Each lane's win probability is modelled Beta(prior + wins, prior + losses) from the
    window of stats handed in (the window is the caller's choice — pass ``by_lane`` over
    the last N days). We draw from every posterior ``draws`` times and weight each lane by
    how often it came out on top. A lane with few trades has a wide posterior and still
    wins draws sometimes, which is the point: exploration is proportional to uncertainty
    rather than uniform.

    A lane whose posterior barely overlaps the leader's can fall to a weight of zero, and
    should: that is Thompson sampling working. What brings it back is the *sliding window*
    — old losses age out of the stats the caller passes in, the posterior widens again,
    and the lane starts winning draws. A permanent floor would instead pay rent forever to
    a lane we have already measured.

    The RNG is seeded so the same stats always produce the same allocation — a
    non-reproducible capital allocation is not auditable.
    """
    entries: list[tuple[Lane, float, float]] = []
    for raw_lane, stats in lanes_stats.items():
        try:
            lane = raw_lane if isinstance(raw_lane, Lane) else Lane(str(raw_lane))
        except ValueError:
            continue
        wins = float(getattr(stats, "wins", 0) if not isinstance(stats, Mapping) else stats.get("wins", 0))
        losses = float(
            getattr(stats, "losses", 0) if not isinstance(stats, Mapping) else stats.get("losses", 0)
        )
        entries.append((lane, max(wins, 0.0), max(losses, 0.0)))

    if not entries:
        return {}
    if len(entries) == 1:
        return {entries[0][0]: 1.0}

    rng = random.Random(seed)
    counts = dict.fromkeys((e[0] for e in entries), 0)
    for _ in range(max(draws, 1)):
        best_lane, best_draw = None, -1.0
        for lane, wins, losses in entries:
            sample = rng.betavariate(prior_wins + wins, prior_losses + losses)
            if sample > best_draw:
                best_draw, best_lane = sample, lane
        if best_lane is not None:
            counts[best_lane] += 1
    total = sum(counts.values()) or 1
    return {lane: n / total for lane, n in counts.items()}


__all__ = [
    "DSR_MIN",
    "GateResult",
    "MDD_TOLERANCE",
    "PBO_MAX",
    "allocator",
    "deflated_sharpe",
    "is_tightening",
    "paired_improvement",
    "pbo_cscv",
    "promote",
    "removal_share",
    "replay_gate",
    "rollback",
    "shadow_gate",
]
