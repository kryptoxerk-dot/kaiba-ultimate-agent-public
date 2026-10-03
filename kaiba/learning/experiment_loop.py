"""The experiment loop: the scheduled runner that turns a proposal into a verdict.

MEASURED 2026-10-02 on the box: 4 experiments, all ``proposed``; ``gate_results`` held 2
FAIL rows from one hand-run on 09-24; ``experiment_trades`` held **0** rows, and nothing in
the tree wrote one. Nothing scheduled the gates, and nothing produced the candidate arm the
shadow gate reads -- so even a perfect proposal could never have been promoted.

This module DECIDES NOTHING. Every verdict is :mod:`kaiba.learning.gates` (arithmetic on
recorded rows); every write here is a status transition that verdict already implies, an
arm label, or the runner's own bookkeeping in ``kv``. The one write that changes trading --
``gates.promote`` -- is only reached after both gates passed AND the promotion policy below
allows it, and ``promote`` re-runs both gates itself before it writes anything.

State machine (``experiments.status``)::

    proposed --replay FAIL---------------------------------------> rejected
    proposed --replay PASS, shadow setup refused------------------> rejected   (gate shadow_setup)
    proposed --replay PASS, shadow setup ok-----------------------> shadow     (plan in kv)
    shadow   --the incumbent value changed under it----------------> rejected   (gate shadow_setup)
    shadow   --the gate's minimums met (>= 50 candidate trades over >= 14 days on >= 20
               distinct UTC days), or shadow_max_days elapsed-------> gates.shadow_gate
               FAIL ----------------------------------------------> rejected
               PASS, promotion policy refuses ---------------------> awaiting_owner
               PASS, policy allows -------------------------------> gates.promote -> promoted
    promoted --pre-registered rollback rule breached---------------> gates.rollback -> rejected
    promoted --monitor_until_trades reached / key superseded-------> promoted (monitor closed)

HOW A TIGHTENING IS JUDGED (lead's decision 2026-10-02, owner objective = EV per trade): both
gates judge a tightening RELATIVELY -- on the same decisions, does the candidate's subset earn
more per trade than the incumbent's whole stream, with a paired day-cluster bootstrap 95% lower
bound above zero (``gates.paired_improvement``). Absolute DSR is recorded, not required, so a
loss-cutting filter on a losing lane can pass. Everything else keeps the absolute rule.

HOW THE CANDIDATE ARM GETS FORWARD TRADES -- a selection shadow, not a second paper book.

The replay gate can only judge ``min_*``/``max_*`` thresholds over features the decision
recorded, and shadow setup admits only TIGHTENINGS of those. A tightening's forward trades
are, by construction, a subset of the incumbent's: every decision the candidate would take,
the incumbent did take. So the candidate's outcome on such a decision is the incumbent's
RECORDED fill -- the real fill on a live lane, the paper broker's fill on a shadow lane --
never a second simulated one. The labeller tags each forward trade of the lane on the plan's
chain and mode ``candidate`` when its point-in-time feature passes the candidate threshold,
``incumbent`` when it does not (the trades the candidate would have refused). No order, no
quote, no provider call, so nothing here can draw on the protection quote budget.

* The incumbent arm is the COMPLEMENT, not the whole stream, because ``experiment_trades``
  is keyed ``(experiment_id, trade_id)`` -- a trade carries one arm. For expectancy that is
  the same verdict (mean(S) >= mean(T\\S) iff mean(S) >= mean(T)). For drawdown it is a
  different comparison (candidate against the trades it refuses); the replay gate already
  compared drawdown against the full incumbent series.
* A LOOSENING is refused at setup: it would admit signals the lane never emits under the
  incumbent, so the forward stream holds no outcome for them. Measure those on the scanned
  population (``kaiba.learning.replay``), not by selection.
* ONE chain per experiment: ``shadow_gate`` compares expectancy in base units, and lamports
  against wei is not a comparison. The chain is the diff's ``chain`` or the lane's busiest
  chain over ``chain_lookback_days``. ONE mode family: the lane's effective mode at setup
  (live+canary fills, or shadow fills) -- a launchpad twin's paper fill is not a live fill.
* A trade whose decision has no point-in-time feature is labelled into neither arm.

PROMOTION POLICY (all must hold, else ``awaiting_owner`` with the reasons):

* job param ``auto_promote`` true AND ``bounds.allow_self_promotion`` true;
* the key is a ``min_*``/``max_*`` threshold the lane ALREADY reads (defaults + risk.yaml);
* the key is not a field of ``EnvelopeBounds``/``LaneConfig``/``RiskConfig``/``ChainBudget``
  and contains none of :data:`NEVER_AUTO_SUBSTRINGS` -- sizing, bankroll, exposure, loss
  limits, slippage, modes, chains, launchpads, wallets and switches are never automatic.
  Owner directive 2026-09-24: confidence measures are anti-calibrated and entry size is
  FLAT, so no promotion may move size on a score, a wallet count or a grade;
* the candidate is a tightening and, on an integer parameter, an integer (the lane reads
  ``int(p[key])``: a fractional candidate would be truncated, so the value tested would
  not be the value promoted);
* the candidate refuses at most ``max_auto_refused_share`` of the lane's forward SIGNALS on
  the plan's chain -- the population the lane scans and decides on, not only the fills we
  took (owner directive 2026-09-24: a holder floor and a launchpad blocklist measured on
  fills removed 98% and 87.5% of the scanned population).

PRE-REGISTERED ROLLBACK RULE (journalled before ``promote`` is called):

    baseline     = per-trade returns (pnl/cost) of every labelled shadow-window trade, both
                   arms -- i.e. the incumbent's own forward stream on that chain and mode;
    baseline_lo  = mean - 1.96 * sd / sqrt(n)      (normal approximation, 95%);
    live sample  = the lane's closed trades on that chain and mode whose DECISION was made
                   at or after the promotion, oldest first, at most monitor_until_trades;
    ROLL BACK    once n >= rollback_min_trades (20) and mean(live) < baseline_lo.

  Checked every run until ``monitor_until_trades`` (100), then monitoring closes with the
  verdict recorded. If the promoted key no longer holds the promoted value, monitoring
  closes as SUPERSEDED and nothing is rolled back -- that would overwrite a newer decision.
  The rule is deliberately quick to fire: with a promoted value truly equal to the
  incumbent, one look at n=20 against a bound from 100 baseline trades fires ~19% of the
  time, and repeated looks more. Rollback restores the measured incumbent, which is the
  safe direction; the rule protects the bankroll, not the promotion.
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from kaiba.core import journal
from kaiba.core.config import ChainBudget, EnvelopeBounds, LaneConfig, RiskConfig, get_risk
from kaiba.core.db import fetch_all, fetch_one, jdump, jload, tx, upsert
from kaiba.core.schemas import Chain, Lane, LaneMode, now_ms
from kaiba.learning import gates, metrics

log = logging.getLogger(__name__)

BY = "experiment_gates"
PLAN_KEY_PREFIX = "experiment_gates:plan:"
DAY_MS = 86_400_000

PROPOSED = "proposed"
SHADOW = "shadow"
PROMOTED = "promoted"
REJECTED = "rejected"
AWAITING_OWNER = "awaiting_owner"

#: Verdicts this runner records itself, beside the gates' ``replay`` / ``shadow``. Neither
#: is a judgement on the candidate's merit: ``shadow_setup`` says the shadow could not be run
#: (or stopped meaning anything), ``live_monitor`` is the pre-registered rule's outcome.
GATE_SHADOW_SETUP = "shadow_setup"
GATE_LIVE_MONITOR = "live_monitor"

DEFAULTS: dict[str, Any] = {
    "max_experiments_per_run": 25,
    "max_label_rows_per_run": 2000,
    "shadow_max_days": 45,
    "max_auto_refused_share": 0.5,
    "rollback_min_trades": 20,
    "monitor_until_trades": 100,
    "chain_lookback_days": 7,
    "auto_promote": True,
}

#: The shadow gate's own minimums, read from its signature so the runner cannot drift from
#: the judge. Calling the gate before they are met would record a FAIL for "too few trades"
#: and reject a candidate that had simply not run long enough.
_SHADOW_PARAMS = inspect.signature(gates.shadow_gate).parameters
SHADOW_MIN_TRADES = int(_SHADOW_PARAMS["min_trades"].default)
SHADOW_MIN_DAYS = int(_SHADOW_PARAMS["min_days"].default)
#: Distinct UTC days the relative criterion's day-cluster bound needs (gates' own constant).
SHADOW_MIN_CLUSTER_DAYS = int(gates.MIN_CLUSTER_DAYS)

Z95 = Decimal("1.96")

#: Keys a structured diff carries besides the parameter itself.
_DIFF_META = frozenset({"lane", "chain", "key", "old", "new", "rationale", "hypothesis", "reason"})

#: Every field of the operator's configuration models. Never self-promotable.
OPERATOR_OWNED_KEYS: frozenset[str] = (
    frozenset(EnvelopeBounds.model_fields)
    | frozenset(LaneConfig.model_fields)
    | frozenset(RiskConfig.model_fields)
    | frozenset(ChainBudget.model_fields)
)

#: A key containing any of these is never promoted automatically, whatever the gates said.
NEVER_AUTO_SUBSTRINGS: tuple[str, ...] = (
    "size", "bankroll", "exposure", "loss", "slippage", "position", "budget", "bound",
    "mode", "chain", "launchpad", "wallet", "kill", "pause", "reduce", "leverage",
    "promotion",
)

LOOSENING_REASON = (
    "candidate LOOSENS the threshold: it would admit signals the lane never emits under the "
    "incumbent, so the forward stream holds no outcome for them. A loosening has to be "
    "measured on the scanned population (kaiba.learning.replay), not by selection"
)


# --------------------------------------------------------------------------------------
# diffs
# --------------------------------------------------------------------------------------


def normalize_diff(row: Mapping[str, Any]) -> dict[str, Any]:
    """The diff in the shape the gates read: ``{lane, key, old, new[, chain]}``.

    ``kaiba_propose_experiment`` takes a free-form dict, and its own test proposes the flat
    ``{"min_entities": 4}``. The gates only read ``diff["key"]``/``diff["new"]``, so a flat
    proposal used to fail as "diff does not name a lane and a key" -- true, but useless to
    the proposer. A flat diff with exactly ONE scalar parameter is rewritten; anything else
    is passed through unchanged and the gate says precisely why it cannot judge it.
    """
    raw = jload(row.get("diff_json"), {})
    if not isinstance(raw, Mapping):
        return {}
    diff = dict(raw)
    if "key" in diff:
        if not diff.get("lane") and row.get("lane"):
            diff["lane"] = row["lane"]
        return diff
    params = [(k, v) for k, v in diff.items() if k not in _DIFF_META]
    if len(params) != 1:
        return diff
    key, value = params[0]
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return diff
    out: dict[str, Any] = {"key": str(key), "new": value, "old": diff.get("old"),
                           "normalized_from": "flat"}
    lane = diff.get("lane") or row.get("lane")
    if lane:
        out["lane"] = lane
    if diff.get("chain"):
        out["chain"] = diff["chain"]
    return out


def _gate_row(row: Mapping[str, Any], diff: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(row), "diff_json": jdump(dict(diff))}


def with_true_incumbent(diff: Mapping[str, Any]) -> dict[str, Any]:
    """The diff with ``old`` set to the value the lane is RUNNING, not the one remembered.

    ``replay_gate`` takes ``diff["old"]`` as the incumbent when present. A proposer who
    misremembers it (or proposes against a value since changed) would have the candidate
    judged against a configuration that does not exist. A differing claim is kept as
    ``old_claimed`` so the record shows both.
    """
    out = dict(diff)
    try:
        lane = Lane(str(out.get("lane") or ""))
    except ValueError:
        return out
    key = str(out.get("key") or "")
    params = _effective_params(lane, get_risk())
    if not key or key not in params:
        return out
    claimed = out.get("old")
    if claimed is not None and gates._num(claimed) != gates._num(params[key]):
        out["old_claimed"] = claimed
    out["old"] = params[key]
    return out


def judgeability(lane: str | None, diff: Mapping[str, Any]) -> dict[str, Any]:
    """What the scheduled gates will be able to do with a proposal. Advisory, no I/O."""
    d = normalize_diff({"lane": lane, "diff_json": jdump(dict(diff or {}))})
    key = str(d.get("key") or "")
    if not key or not d.get("lane"):
        return {"judgeable": False, "why": "name a lane and ONE parameter: "
                "{'key': 'min_<x>'|'max_<x>', 'new': <number>} or {'min_<x>': <number>}"}
    if gates._direction(key) is None:
        return {"judgeable": False, "why": f"{key!r} is not a min_*/max_* threshold; the replay "
                "can only re-run entry thresholds over recorded decisions"}
    if gates._num(d.get("new")) is None:
        return {"judgeable": False, "why": f"candidate {d.get('new')!r} is not numeric"}
    return {"judgeable": True, "why": "replay, then a forward shadow if it is a tightening",
            "normalized": d}


# --------------------------------------------------------------------------------------
# the shadow plan (kv)
# --------------------------------------------------------------------------------------


def plan_key(experiment_id: str) -> str:
    return PLAN_KEY_PREFIX + str(experiment_id)


def load_plan(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any] | None:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (plan_key(experiment_id),))
    if not row:
        return None
    plan = jload(row["value"], {})
    return plan if isinstance(plan, dict) and plan else None


def save_plan(conn: sqlite3.Connection, plan: Mapping[str, Any]) -> None:
    upsert(conn, "kv", {"key": plan_key(str(plan["experiment_id"])), "value": jdump(dict(plan)),
                        "updated_ms": now_ms()}, ["key"])


def _effective_params(lane: Lane, risk: RiskConfig) -> dict[str, Any]:
    """What the lane actually reads: shipped defaults, then risk.yaml (``lane_params``)."""
    merged: dict[str, Any] = {}
    try:
        from kaiba.execution.lanes import DEFAULT_PARAMS

        merged.update(DEFAULT_PARAMS.get(lane, {}))
    except Exception as exc:  # noqa: BLE001 - defaults unreadable: risk.yaml still decides
        log.warning("lane defaults unreadable for %s: %s", lane.value, exc)
    merged.update(risk.lane(lane).params or {})
    return merged


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plan_chain(
    conn: sqlite3.Connection, lane: Lane, diff: Mapping[str, Any], modes: list[str],
    risk: RiskConfig, now: int, lookback_days: int,
) -> tuple[Chain | None, str | None]:
    raw = diff.get("chain")
    if raw:
        try:
            chain = Chain(str(raw).strip().lower())
        except ValueError:
            return None, f"unknown chain {raw!r}"
    else:
        marks = ",".join("?" * len(modes))
        row = fetch_one(
            conn,
            f"SELECT chain, COUNT(*) AS n FROM trades WHERE lane = ? AND mode IN ({marks}) "
            "AND closed_ms >= ? GROUP BY chain ORDER BY n DESC, chain ASC LIMIT 1",
            (lane.value, *modes, now - lookback_days * DAY_MS),
        )
        if not row:
            return None, (f"{lane.value} closed no {'/'.join(modes)} trades on any chain in the "
                          f"last {lookback_days} days: a forward shadow would never fill")
        chain = Chain(str(row["chain"]))
    chains = risk.lane(lane).chains
    if chains and chain not in chains:
        return None, f"{chain.value} is not one of {lane.value}'s chains"
    return chain, None


def setup_shadow(
    conn: sqlite3.Connection, row: Mapping[str, Any], diff: Mapping[str, Any], *,
    now: int, lookback_days: int = DEFAULTS["chain_lookback_days"],
) -> tuple[dict[str, Any] | None, list[str]]:
    """A shadow plan for a replay-passed candidate, or the reasons none can be run."""
    exp_id = str(row["experiment_id"])
    lane_raw = str(diff.get("lane") or row.get("lane") or "")
    try:
        lane = Lane(lane_raw)
    except ValueError:
        return None, [f"unknown lane {lane_raw!r}: no decision stream to shadow"]
    key = str(diff.get("key") or "")
    direction = gates._direction(key)
    candidate = gates._num(diff.get("new"))
    if direction is None or candidate is None or not candidate.is_finite():
        return None, [f"{key!r} = {diff.get('new')!r} is not a numeric min_*/max_* threshold"]

    risk = get_risk()
    mode = risk.effective_mode(lane)
    if mode is LaneMode.OFF:
        return None, [f"{lane.value} is off: it makes no decisions to shadow"]
    params = _effective_params(lane, risk)
    if key not in params:
        return None, [f"{lane.value} reads no parameter {key!r}: a key the lane never reads "
                      "changes nothing it does"]
    incumbent_raw = params[key]
    # ``None`` is "not enforced" -- the shipped default of every learnable entry-feature
    # threshold (lanes.FEATURE_THRESHOLD_KEYS). The lane takes everything, so ANY numeric
    # candidate is a tightening of it (gates.is_tightening says the same). Anything else
    # that is not a number (a per-chain mapping, a typo) still refuses.
    incumbent = gates._num(incumbent_raw)
    if incumbent is None and incumbent_raw is not None:
        return None, [f"incumbent {lane.value}.{key} = {incumbent_raw!r} is not numeric"]
    if incumbent is not None and candidate == incumbent:
        return None, [f"candidate {candidate} equals the incumbent: nothing to test"]
    if not gates.is_tightening(candidate, incumbent, direction):
        return None, [LOOSENING_REASON]
    value: Any = diff.get("new")
    if _is_int(incumbent_raw):
        if candidate != candidate.to_integral_value():
            return None, [f"{lane.value}.{key} is an integer the lane reads with int(); a "
                          f"fractional candidate {candidate} would be truncated, so the value "
                          "tested would not be the value promoted"]
        value = int(candidate)
    elif isinstance(value, str):
        value = float(candidate)

    modes = ["live", "canary"] if mode in (LaneMode.LIVE, LaneMode.CANARY) else ["shadow"]
    chain, refusal = _plan_chain(conn, lane, diff, modes, risk, now, lookback_days)
    if chain is None:
        return None, [refusal or "no chain"]

    cursor = fetch_one(conn, "SELECT COALESCE(MAX(rowid), 0) AS m FROM trades")
    return {
        "v": 1,
        "experiment_id": exp_id,
        "lane": lane.value,
        "key": key,
        "direction": direction,
        "candidate": str(candidate),
        "candidate_value": value,
        # None (JSON null) when the lane enforces no threshold today; see _same_incumbent.
        "incumbent": None if incumbent is None else str(incumbent),
        "chain": chain.value,
        "modes": modes,
        "started_ms": int(now),
        # Trades inserted before the shadow started cannot belong to a decision made after
        # it, so the labeller starts past them. A rowid cursor, not closed_ms: a trade booked
        # late with an older closed_ms would slip behind a time cursor.
        "cursor_rowid": int(cursor["m"]) if cursor else 0,
        "missing_feature": 0,
    }, []


# --------------------------------------------------------------------------------------
# arms
# --------------------------------------------------------------------------------------


def label_arms(conn: sqlite3.Connection, plan: dict[str, Any], *, now: int, limit: int) -> dict[str, int]:
    """Tag forward trades into the candidate / incumbent arm. Idempotent and bounded."""
    modes = list(plan["modes"])
    entry_marks = ",".join("?" * len(gates.ENTRY_ACTIONS))
    rows = fetch_all(
        conn,
        "SELECT t.rowid AS rid, t.trade_id, d.decision_id, d.ts_ms, d.signals_json, d.confidence, "
        "d.expected_return_pct, d.size_pct_bankroll "
        "FROM trades t JOIN decisions d ON d.decision_id = t.decision_id "
        f"WHERE t.rowid > ? AND t.lane = ? AND t.chain = ? AND t.mode IN ({','.join('?' * len(modes))}) "
        f"AND d.ts_ms >= ? AND d.action IN ({entry_marks}) ORDER BY t.rowid ASC LIMIT ?",
        (int(plan.get("cursor_rowid", 0)), plan["lane"], plan["chain"], *modes,
         int(plan["started_ms"]), *gates.ENTRY_ACTIONS, int(limit)),
    )
    if not rows:
        return {"seen": 0, "candidate": 0, "incumbent": 0, "missing_feature": 0}
    threshold = Decimal(str(plan["candidate"]))
    inserts: list[tuple[str, str, str, int]] = []
    counts = {"seen": len(rows), "candidate": 0, "incumbent": 0, "missing_feature": 0}
    for r in rows:
        feature, recorded = gates._feature_point(conn, r, str(plan["key"]))
        if not recorded:
            # The signal predates the feature (gates._feature_point): no arm can be said.
            counts["missing_feature"] += 1
            continue
        # RECORDED as unknown (None): the candidate threshold refuses it live
        # (lanes.feature_threshold_refusal), so it is a trade the candidate would not have
        # taken -- the incumbent arm, exactly what gates._passes(None, threshold) returns.
        arm = "candidate" if gates._passes(feature, threshold, plan["direction"]) else "incumbent"
        counts[arm] += 1
        inserts.append((str(plan["experiment_id"]), str(r["trade_id"]), arm, int(now)))
    plan["cursor_rowid"] = max(int(r["rid"]) for r in rows)
    plan["missing_feature"] = int(plan.get("missing_feature", 0)) + counts["missing_feature"]
    with tx(conn) as c:
        c.executemany(
            "INSERT OR IGNORE INTO experiment_trades (experiment_id, trade_id, arm, linked_ms) "
            "VALUES (?,?,?,?)",
            inserts,
        )
        save_plan(c, plan)
    return counts


def arm_days(conn: sqlite3.Connection, experiment_id: str) -> int:
    """Distinct UTC days (by ``opened_ms``, as the gate clusters them) across both arms."""
    row = fetch_one(
        conn,
        "SELECT COUNT(DISTINCT t.opened_ms / ?) AS d FROM experiment_trades x "
        "JOIN trades t ON t.trade_id = x.trade_id WHERE x.experiment_id = ?",
        (DAY_MS, str(experiment_id)),
    )
    return int(row["d"]) if row else 0


def arm_summary(conn: sqlite3.Connection, experiment_id: str) -> dict[str, dict[str, int]]:
    out = {"candidate": {"n": 0, "first_ms": 0, "last_ms": 0},
           "incumbent": {"n": 0, "first_ms": 0, "last_ms": 0}}
    for r in fetch_all(
        conn,
        "SELECT x.arm, COUNT(*) AS n, MIN(t.closed_ms) AS first_ms, MAX(t.closed_ms) AS last_ms "
        "FROM experiment_trades x JOIN trades t ON t.trade_id = x.trade_id "
        "WHERE x.experiment_id = ? GROUP BY x.arm",
        (str(experiment_id),),
    ):
        out[str(r["arm"])] = {"n": int(r["n"]), "first_ms": int(r["first_ms"] or 0),
                              "last_ms": int(r["last_ms"] or 0)}
    return out


def _returns_stats(values: list[Decimal]) -> dict[str, Any]:
    n = len(values)
    if n < 2:
        return {"n": n, "mean": str(values[0]) if values else None, "sd": None, "lo": None}
    mean = sum(values, Decimal(0)) / n
    var = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (n - 1)
    sd = var.sqrt()
    lo = mean - Z95 * sd / Decimal(n).sqrt()
    return {"n": n, "mean": str(mean), "sd": str(sd), "lo": str(lo), "method": "normal_95"}


def baseline(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any]:
    """Per-trade returns of the whole labelled shadow window -- the incumbent's own stream."""
    trades = fetch_all(
        conn,
        "SELECT t.* FROM trades t JOIN experiment_trades x ON x.trade_id = t.trade_id "
        "WHERE x.experiment_id = ? ORDER BY t.closed_ms ASC, t.trade_id ASC",
        (str(experiment_id),),
    )
    return _returns_stats(metrics.returns_of(trades))


def refused_share(conn: sqlite3.Connection, plan: Mapping[str, Any], *, limit: int = 5000) -> dict[str, Any]:
    """Share of the lane's forward SIGNALS (every decision, skips included) the candidate refuses.

    The fills are a filtered subset of what the lane scans and decides on; a filter judged
    only on fills can cut most of the book and still look good on the trades that remain.
    """
    modes = list(plan["modes"])
    rows = fetch_all(
        conn,
        "SELECT decision_id, ts_ms, signals_json, confidence, expected_return_pct, size_pct_bankroll "
        f"FROM decisions WHERE lane = ? AND mode IN ({','.join('?' * len(modes))}) AND chain = ? "
        "AND ts_ms >= ? ORDER BY ts_ms DESC LIMIT ?",
        (plan["lane"], *modes, plan["chain"], int(plan["started_ms"]), int(limit)),
    )
    threshold = Decimal(str(plan["candidate"]))
    measured = refused = 0
    for r in rows:
        feature, recorded = gates._feature_point(conn, r, str(plan["key"]))
        if not recorded:
            continue  # predates the feature; a recorded unknown is refused below
        measured += 1
        if not gates._passes(feature, threshold, plan["direction"]):
            refused += 1
    return {"decisions": len(rows), "measured": measured, "refused": refused,
            "share": (refused / measured) if measured else None}


# --------------------------------------------------------------------------------------
# promotion policy
# --------------------------------------------------------------------------------------


def key_refusal(key: str) -> str | None:
    """Why ``key`` may never be promoted automatically, or ``None``."""
    if gates._direction(key) is None:
        return f"{key!r} is not a min_*/max_* entry threshold; only those are self-promotable"
    if key in OPERATOR_OWNED_KEYS:
        return f"{key!r} is an operator-owned configuration field"
    hit = next((s for s in NEVER_AUTO_SUBSTRINGS if s in key), None)
    if hit:
        return (f"{key!r} touches {hit!r}: sizing, bankroll, risk limits, modes, chains and "
                "launchpads are never promoted automatically")
    return None


def promotion_refusals(
    conn: sqlite3.Connection, plan: Mapping[str, Any], *, risk: RiskConfig, params: Mapping[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Everything that stops an automatic promotion; empty means promote."""
    reasons: list[str] = []
    if not bool(params.get("auto_promote", True)):
        reasons.append("job param auto_promote is false")
    if not risk.bounds.allow_self_promotion:
        reasons.append("bounds.allow_self_promotion is false; the operator withholds promotion")
    refusal = key_refusal(str(plan["key"]))
    if refusal:
        reasons.append(refusal)
    candidate = Decimal(str(plan["candidate"]))
    incumbent = _plan_incumbent(plan)
    if not gates.is_tightening(candidate, incumbent, plan["direction"]):
        reasons.append(LOOSENING_REASON)
    share = refused_share(conn, plan)
    cap = float(params.get("max_auto_refused_share", DEFAULTS["max_auto_refused_share"]))
    if share["share"] is None:
        reasons.append("the candidate's cut of the lane's forward signals could not be measured")
    elif share["share"] > cap:
        reasons.append(
            f"candidate refuses {share['refused']}/{share['measured']} "
            f"({share['share']:.0%}) of the lane's forward signals, above the {cap:.0%} an "
            "automatic promotion may cut"
        )
    return reasons, {"refused_share": share}


# --------------------------------------------------------------------------------------
# transitions
# --------------------------------------------------------------------------------------


def _record_own_verdict(
    conn: sqlite3.Connection, experiment_id: str, gate: str, passed: bool,
    reasons: list[str], metrics_: Mapping[str, Any] | None = None,
) -> gates.GateResult:
    result = gates.GateResult(gate=gate, experiment_id=str(experiment_id), passed=passed,
                              reasons=list(reasons), metrics=dict(metrics_ or {}))
    result.record(conn)
    return result


def _reject(
    conn: sqlite3.Connection, experiment_id: str, notes: str, *,
    replay: gates.GateResult | None = None, shadow: gates.GateResult | None = None,
) -> None:
    gates._set_status(conn, experiment_id, REJECTED, by=BY, notes=notes, replay=replay, shadow=shadow)
    journal.append("experiment", f"rejected {experiment_id}: {notes}"[:3900], subject=experiment_id, conn=conn)


def _promotion_entry(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any] | None:
    for entry in journal.read(limit=50, kind="change", subject=str(experiment_id), conn=conn):
        if str(entry["body"]).startswith(f"promoted {experiment_id}:"):
            return entry
    return None


def _current_value(plan: Mapping[str, Any]) -> Decimal | None:
    lane = Lane(str(plan["lane"]))
    return gates._num(_effective_params(lane, get_risk()).get(str(plan["key"])))


def _plan_incumbent(plan: Mapping[str, Any]) -> Decimal | None:
    """The incumbent the plan was set up against; ``None`` = no threshold was enforced."""
    raw = plan.get("incumbent")
    return None if raw is None else Decimal(str(raw))


def _same_incumbent(current: Decimal | None, plan: Mapping[str, Any]) -> bool:
    """Is the lane still running the value the arms were labelled against?

    ``None == None`` is the same configuration: a learnable threshold that was off when the
    shadow started and is still off. Before 2026-10-03 every incumbent was numeric, and a
    ``None`` current value always meant the key had vanished.
    """
    incumbent = _plan_incumbent(plan)
    if incumbent is None:
        return current is None
    return current is not None and current == incumbent


def handle_proposed(
    conn: sqlite3.Connection, row: Mapping[str, Any], *, now: int, params: Mapping[str, Any],
) -> str:
    exp_id = str(row["experiment_id"])
    diff = with_true_incumbent(normalize_diff(row))
    gate_row = _gate_row(row, diff)
    # record=False, then record HERE: the gate persists only verdicts that reach its full
    # computation -- its early refusals (unparseable diff, non-numeric candidate, no replay
    # rule, missing features) return unrecorded, and a rejection with no row behind it is
    # exactly the "never judged" state this runner exists to end.
    replay = gates.replay_gate(gate_row, conn, record=False,
                               max_removal_share=float(params["max_auto_refused_share"]))
    replay.record(conn)
    if not replay.passed:
        _reject(conn, exp_id, "replay: " + "; ".join(replay.reasons), replay=replay)
        return REJECTED
    plan, refusals = setup_shadow(conn, row, diff, now=now,
                                  lookback_days=int(params["chain_lookback_days"]))
    if plan is None:
        _record_own_verdict(conn, exp_id, GATE_SHADOW_SETUP, False, refusals)
        _reject(conn, exp_id, "replay passed; shadow cannot run: " + "; ".join(refusals), replay=replay)
        return REJECTED
    save_plan(conn, plan)
    gates._set_status(
        conn, exp_id, SHADOW, by=BY, replay=replay,
        notes=(f"replay passed; selection shadow on {plan['chain']} {'/'.join(plan['modes'])} "
               f"{plan['lane']}.{plan['key']} {plan['incumbent']} -> {plan['candidate']}"),
    )
    journal.append(
        "experiment",
        f"shadow started {exp_id}: {plan['lane']}.{plan['key']} {plan['incumbent']} -> "
        f"{plan['candidate']} on {plan['chain']} ({'/'.join(plan['modes'])}); gate when "
        f">= {SHADOW_MIN_TRADES} candidate trades over >= {SHADOW_MIN_DAYS} days and "
        f">= {SHADOW_MIN_CLUSTER_DAYS} distinct trading days, or at "
        f"{params['shadow_max_days']} days",
        subject=exp_id, conn=conn,
    )
    return SHADOW


def handle_shadow(
    conn: sqlite3.Connection, row: Mapping[str, Any], *, now: int, params: Mapping[str, Any],
    out: dict[str, Any],
) -> str:
    exp_id = str(row["experiment_id"])
    diff = with_true_incumbent(normalize_diff(row))
    plan = load_plan(conn, exp_id)
    if plan is None:
        # In `shadow` without a plan (set by hand, or the kv row lost): start the clock now.
        # Never backdated -- a shadow window is only out of sample from when it began.
        plan, refusals = setup_shadow(conn, row, diff, now=now,
                                      lookback_days=int(params["chain_lookback_days"]))
        if plan is None:
            _record_own_verdict(conn, exp_id, GATE_SHADOW_SETUP, False, refusals)
            _reject(conn, exp_id, "shadow cannot run: " + "; ".join(refusals))
            return REJECTED
        save_plan(conn, plan)
        journal.append("experiment", f"shadow plan (re)started {exp_id} at {now}", subject=exp_id, conn=conn)

    current = _current_value(plan)
    if not _same_incumbent(current, plan):
        entry = (_promotion_entry(conn, exp_id)
                 if current is not None and current == Decimal(str(plan["candidate"])) else None)
        if entry is not None:
            # `promote` wrote risk.yaml and its journal entry, then the run died before the
            # status update. Finish the transition; the rollback monitor takes over.
            plan.setdefault("baseline", baseline(conn, exp_id))
            plan["promoted_ms"] = int(entry.get("ts_ms") or now)
            save_plan(conn, plan)
            gates._set_status(conn, exp_id, PROMOTED, by=BY,
                              notes="promotion completed after an interrupted run "
                                    "(risk.yaml and the journal already held it)")
            return PROMOTED
        reasons = [f"incumbent changed during the shadow: {plan['lane']}.{plan['key']} was "
                   f"{plan['incumbent']}, is now {current}; the arms no longer compare the "
                   "candidate with what is running"]
        _record_own_verdict(conn, exp_id, GATE_SHADOW_SETUP, False, reasons)
        _reject(conn, exp_id, "; ".join(reasons))
        return REJECTED

    labelled = label_arms(conn, plan, now=now, limit=int(params["max_label_rows_per_run"]))
    out["labelled"] += labelled["candidate"] + labelled["incumbent"]
    arms = arm_summary(conn, exp_id)
    cand, inc = arms["candidate"], arms["incumbent"]
    span_days = (cand["last_ms"] - cand["first_ms"]) / DAY_MS if cand["n"] else 0.0
    age_days = (now - int(plan["started_ms"])) / DAY_MS
    days = arm_days(conn, exp_id)
    # The gate's own minimums, ALL of them: a tightening is judged by a day-cluster bound
    # that does not exist below MIN_CLUSTER_DAYS distinct days, so calling it at 14 days
    # with 15 distinct days would record a FAIL for a shadow that had not finished.
    ready = (cand["n"] >= SHADOW_MIN_TRADES and span_days >= SHADOW_MIN_DAYS
             and days >= SHADOW_MIN_CLUSTER_DAYS)
    expired = age_days >= float(params["shadow_max_days"])
    progress = {"candidate": cand["n"], "incumbent": inc["n"], "span_days": round(span_days, 2),
                "days": days, "age_days": round(age_days, 2),
                "missing_feature": int(plan.get("missing_feature", 0))}
    if not (ready or expired):
        out["waiting"][exp_id] = progress
        return SHADOW

    if inc["n"] == 0:
        # Never hand the gate a one-armed stream: with no labelled incumbent it falls back to
        # every unlabelled trade of the lane, which mixes chains and modes.
        reasons = [
            (f"after {age_days:.1f} days the candidate refused none of its {cand['n']} forward "
             "trades: identical to the incumbent on this stream, nothing to promote")
            if cand["n"] else f"no forward trade was labelled in {age_days:.1f} days"
        ]
        _record_own_verdict(conn, exp_id, GATE_SHADOW_SETUP, False, reasons, progress)
        _reject(conn, exp_id, "; ".join(reasons))
        return REJECTED

    # The incumbent the arms were labelled against, not whatever `old` is now: the drift
    # check above has just confirmed the two are the same value.
    gate_row = _gate_row(row, {**diff, "old": plan["incumbent"], "new": plan["candidate_value"]})
    shadow = gates.shadow_gate(gate_row, conn, record=False)  # see handle_proposed
    shadow.record(conn)
    if not shadow.passed:
        _reject(conn, exp_id, "shadow: " + "; ".join(shadow.reasons), shadow=shadow)
        return REJECTED

    risk = get_risk()
    refusals, evidence = promotion_refusals(conn, plan, risk=risk, params=params)
    plan["promotion_evidence"] = evidence
    if refusals:
        save_plan(conn, plan)
        notes = "replay+shadow passed; not auto-promoted: " + "; ".join(refusals)
        gates._set_status(conn, exp_id, AWAITING_OWNER, by=BY, notes=notes, shadow=shadow)
        journal.append(
            "change",
            f"promotion withheld for the owner: {exp_id} {plan['lane']}.{plan['key']} "
            f"{plan['incumbent']} -> {plan['candidate_value']} on {plan['chain']} passed replay "
            f"and shadow ({cand['n']} candidate / {inc['n']} refused trades). Not applied: "
            + "; ".join(refusals),
            subject=exp_id, conn=conn,
        )
        return AWAITING_OWNER

    # Pre-register the rollback rule BEFORE the promotion exists, from pre-promotion data.
    plan["baseline"] = baseline(conn, exp_id)
    plan["rollback_rule"] = {
        "min_trades": int(params["rollback_min_trades"]),
        "until_trades": int(params["monitor_until_trades"]),
        "rule": "roll back when mean(live return) < baseline lo",
    }
    plan["promotion_requested_ms"] = int(now)
    save_plan(conn, plan)
    journal.append(
        "experiment",
        f"pre-registered rollback rule for {exp_id}: after >= {params['rollback_min_trades']} "
        f"{plan['chain']} {'/'.join(plan['modes'])} trades decided after the promotion, roll "
        f"back if their mean return < {plan['baseline'].get('lo')} (baseline mean "
        f"{plan['baseline'].get('mean')}, n={plan['baseline'].get('n')}, normal 95% lower "
        f"bound); checked each run until {params['monitor_until_trades']} trades",
        subject=exp_id, conn=conn,
    )
    # promote() re-runs both gates (same removal ceiling) and sets `rejected` itself on refusal.
    if not gates.promote(gate_row, conn, max_removal_share=float(params["max_auto_refused_share"])):
        return REJECTED
    plan["promoted_ms"] = int(now)
    save_plan(conn, plan)
    return PROMOTED


def _close_monitor(
    conn: sqlite3.Connection, plan: dict[str, Any], verdict: str, passed: bool,
    reasons: list[str], stats: Mapping[str, Any], *, now: int,
) -> None:
    exp_id = str(plan["experiment_id"])
    _record_own_verdict(conn, exp_id, GATE_LIVE_MONITOR, passed, reasons, stats)
    plan["monitor"] = {"closed": verdict, "closed_ms": int(now), **dict(stats)}
    save_plan(conn, plan)


def handle_promoted(
    conn: sqlite3.Connection, row: Mapping[str, Any], *, now: int, params: Mapping[str, Any],
    out: dict[str, Any],
) -> str:
    exp_id = str(row["experiment_id"])
    plan = load_plan(conn, exp_id)
    if plan is None or "promoted_ms" not in plan or not (plan.get("baseline") or {}).get("lo"):
        out["unmonitored"].append(exp_id)  # no pre-registered rule: none is invented after the fact
        return PROMOTED
    if (plan.get("monitor") or {}).get("closed"):
        return PROMOTED

    current = _current_value(plan)
    if current is None or current != Decimal(str(plan["candidate"])):
        _close_monitor(conn, plan, "superseded", True,
                       [f"{plan['lane']}.{plan['key']} is now {current}, not the promoted "
                        f"{plan['candidate']}: superseded, monitoring closed, no rollback"], {},
                       now=now)
        return PROMOTED

    until = int(plan.get("rollback_rule", {}).get("until_trades", params["monitor_until_trades"]))
    min_n = int(plan.get("rollback_rule", {}).get("min_trades", params["rollback_min_trades"]))
    modes = list(plan["modes"])
    trades = fetch_all(
        conn,
        "SELECT t.* FROM trades t JOIN decisions d ON d.decision_id = t.decision_id "
        f"WHERE t.lane = ? AND t.chain = ? AND t.mode IN ({','.join('?' * len(modes))}) "
        "AND d.ts_ms >= ? ORDER BY t.closed_ms ASC, t.trade_id ASC LIMIT ?",
        (plan["lane"], plan["chain"], *modes, int(plan["promoted_ms"]), until),
    )
    live = _returns_stats(metrics.returns_of(trades))
    lo = Decimal(str(plan["baseline"]["lo"]))
    stats = {"live_n": live["n"], "live_mean": live["mean"], "baseline_lo": str(lo),
             "baseline_mean": plan["baseline"].get("mean")}
    out["monitoring"][exp_id] = stats
    if live["n"] < min_n:
        return PROMOTED
    mean = Decimal(str(live["mean"]))
    if mean < lo:
        reasons = [f"live mean {mean:.4f} over {live['n']} trades < baseline lower bound "
                   f"{lo:.4f}: pre-registered rollback rule breached"]
        _record_own_verdict(conn, exp_id, GATE_LIVE_MONITOR, False, reasons, stats)
        if gates.rollback(exp_id, conn):
            plan["monitor"] = {"closed": "rolled_back", "closed_ms": int(now), **stats}
            save_plan(conn, plan)
            return REJECTED
        plan["monitor"] = {"closed": "rollback_refused", "closed_ms": int(now), **stats}
        save_plan(conn, plan)
        return PROMOTED
    if live["n"] >= until:
        _close_monitor(conn, plan, "confirmed", True,
                       [f"live mean {mean:.4f} over {live['n']} trades held above the baseline "
                        f"lower bound {lo:.4f}; monitoring closed"], stats, now=now)
    return PROMOTED


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------


def _params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(params or {})
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        # A misspelt switch must not silently run on defaults (e.g. `auto_promot: false`).
        raise ValueError(f"unknown experiment_gates parameter(s): {unknown}")
    out = dict(DEFAULTS)
    out.update({k: v for k, v in raw.items() if v is not None})
    return out


def run(
    conn: sqlite3.Connection, *, now: int | None = None, params: Mapping[str, Any] | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """One pass over every open experiment. Idempotent: a re-run repeats nothing it finished."""
    p = _params(params)
    ts = int(now if now is not None else now_ms())
    out: dict[str, Any] = {
        "seen": 0, "rejected": [], "shadow_started": [], "promoted": [], "awaiting_owner": [],
        "rolled_back": [], "waiting": {}, "monitoring": {}, "unmonitored": [], "labelled": 0,
        "deferred": 0, "errors": {},
    }
    rows = fetch_all(
        conn,
        "SELECT * FROM experiments WHERE status = ? ORDER BY created_ms ASC, experiment_id ASC LIMIT ?",
        (PROPOSED, int(p["max_experiments_per_run"])),
    ) + fetch_all(
        conn,
        "SELECT * FROM experiments WHERE status IN (?, ?) ORDER BY created_ms ASC, experiment_id ASC "
        "LIMIT 200",
        (SHADOW, PROMOTED),
    )
    for row in rows:
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            out["deferred"] += 1
            continue
        exp_id = str(row["experiment_id"])
        status = str(row["status"])
        out["seen"] += 1
        try:
            if status == PROPOSED:
                new = handle_proposed(conn, row, now=ts, params=p)
            elif status == SHADOW:
                new = handle_shadow(conn, row, now=ts, params=p, out=out)
            else:
                new = handle_promoted(conn, row, now=ts, params=p, out=out)
        except Exception as exc:  # noqa: BLE001 - one experiment must not stall the others
            log.exception("experiment_gates: %s failed", exp_id)
            out["errors"][exp_id] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        if new == status:
            continue
        if new == REJECTED:
            (out["rolled_back"] if status == PROMOTED else out["rejected"]).append(exp_id)
        elif new == SHADOW:
            out["shadow_started"].append(exp_id)
        elif new == PROMOTED:
            out["promoted"].append(exp_id)
        elif new == AWAITING_OWNER:
            out["awaiting_owner"].append(exp_id)
    return out


def progress(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any] | None:
    """The runner's state for one experiment, for read-outs. ``None`` when it has none."""
    plan = load_plan(conn, experiment_id)
    if plan is None:
        return None
    arms = arm_summary(conn, experiment_id)
    out = {k: plan.get(k) for k in ("lane", "key", "incumbent", "candidate", "chain", "modes",
                                     "started_ms", "promoted_ms", "missing_feature")}
    out["arms"] = {arm: v["n"] for arm, v in arms.items()}
    out["days"] = arm_days(conn, experiment_id)
    if plan.get("baseline"):
        out["baseline"] = {k: plan["baseline"].get(k) for k in ("n", "mean", "lo")}
    if plan.get("monitor"):
        out["monitor"] = plan["monitor"]
    out["shadow_minimums"] = {"candidate_trades": SHADOW_MIN_TRADES, "span_days": SHADOW_MIN_DAYS,
                              "distinct_days": SHADOW_MIN_CLUSTER_DAYS}
    return out


__all__ = [
    "AWAITING_OWNER",
    "DEFAULTS",
    "GATE_LIVE_MONITOR",
    "GATE_SHADOW_SETUP",
    "SHADOW_MIN_CLUSTER_DAYS",
    "SHADOW_MIN_DAYS",
    "SHADOW_MIN_TRADES",
    "arm_days",
    "arm_summary",
    "baseline",
    "judgeability",
    "key_refusal",
    "label_arms",
    "normalize_diff",
    "progress",
    "promotion_refusals",
    "with_true_incumbent",
    "refused_share",
    "run",
    "setup_shadow",
]
