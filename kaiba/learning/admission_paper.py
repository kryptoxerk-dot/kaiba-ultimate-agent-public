"""Additive PAPER admission/recognition clocks; legacy execution sources stay frozen.

Codex LEARN-EXECUTION-CLOCK owns this module. It does not activate, archive or
rewrite a cohort. Prices retain their original observation clocks. Real broker
orders use the already revalidated processing frontier; timed holds start there,
and price exits wait for a print after actual recognition plus the frozen delay.
Cost arithmetic and the statistical family remain the existing implementations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core.schemas import Chain, LaneMode, OrderState
from kaiba.execution.paper import load_position
from kaiba.execution.protection import ProtectionState, evaluate
from kaiba.learning import candidate_paper as cp
from kaiba.learning import strategy_campaign as sc
from kaiba.learning import strategy_search as ss

VERSION = "admission_paper/1"
MAX_ARMS = 12
ROW_CAP = 1000
POLICY_KEY = "execution_policy"
SOURCE_KEY = "learning/admission_paper.py"


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("integer clock or count required")
    number = Decimal(str(value))
    if not number.is_finite() or number != int(number):
        raise ValueError("integer clock or count required")
    return int(number)


def execution_policy(*, max_stake_usd: Decimal | str = "30", max_open_per_arm: int = 3) -> dict:
    stake = Decimal(str(max_stake_usd))
    if (not stake.is_finite() or not 0 < stake <= 120
            or type(max_open_per_arm) is not int or not 1 <= max_open_per_arm <= 3):
        raise ValueError("admission policy exceeds the existing paper envelope")
    return {"version": VERSION,
        "sources": {SOURCE_KEY: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        "base_sources_digest": sc._hash(sc.source_versions()),
        "max_arms": MAX_ARMS, "max_stake_usd": format(stake.normalize(), "f"),
        "max_open_per_arm": max_open_per_arm,
        "entry_delay_ms": 30_000, "entry_max_age_ms": 120_000,
        "quote_max_age_ms": 60_000, "native_quote_max_age_ms": 300_000, "poll_ms": 12_000,
        "ledger_clock": "last_revalidated_processing_frontier",
        "hold_clock": "entry_admission_plus_hold_plus_poll",
        "price_trigger_clock": "actual_recognition_plus_poll",
        "pricing_clock": "original_component_observations",
        "fee_policy": cp._fee_policy(), "cost_model_assumed": True, "live_cost_verified": False}


def _assessment_policy(policy: Mapping[str, Any]) -> dict:
    return {"fdr_q": sc.FDR_Q, "method": "bh", "ready_days": sc.READY_DAYS,
        "evidence_max_age_ms": sc.EVIDENCE_MAX_AGE_MS, "min_n": sc.sprint.MIN_N,
        "min_half": sc.sprint.MIN_HALF, POLICY_KEY: dict(policy)}


def validate_manifest(manifest: Mapping[str, Any], *, now_ms: int | None = None,
                      expected_policy: Mapping[str, Any] | None = None) -> dict:
    """Exact composite identity, without weakening the base nine-source equality."""
    try:
        supplied = manifest["assessment_policy"][POLICY_KEY]
        policy = execution_policy(max_stake_usd=supplied["max_stake_usd"],
                                  max_open_per_arm=supplied["max_open_per_arm"])
        if supplied != policy or expected_policy is not None and policy != expected_policy:
            raise ValueError("execution policy/source mismatch")
        selected = _int(manifest["selected_ms"])
        arms = manifest["hypotheses"]
        if (manifest.get("version") != sc.CAMPAIGN_VERSION
                or manifest.get("status") != "FROZEN_PAPER_ONLY" or manifest.get("funding_allowed") is not False
                or manifest["source_versions"] != sc.source_versions()
                or manifest["assessment_policy"] != _assessment_policy(policy)
                or manifest["campaign_id"] != sc._campaign_id(manifest)
                or selected <= 0 or now_ms is not None and selected > _int(now_ms)
                or not isinstance(arms, list) or not 1 <= len(arms) <= MAX_ARMS):
            raise ValueError("invalid frozen admission manifest")
        seen = set()
        for arm in arms:
            spec = arm["spec"]
            digest = sc._hash(spec)
            if (spec.get(POLICY_KEY) != policy or arm.get("spec_hash") != digest
                    or arm.get("hypothesis_id") != "ph_" + digest[:24]
                    or arm["hypothesis_id"] in seen or arm.get("status") != "PAPER_ONLY"):
                raise ValueError("invalid frozen admission arm")
            Chain(spec["chain"])
            cp.Lane(spec["lane"])
            cp._protection(spec["exit"])
            for condition in spec.get("filter", []):
                ss.Cond.from_spec(condition)
            seen.add(arm["hypothesis_id"])
        return policy
    except (KeyError, TypeError, ArithmeticError, OverflowError, AttributeError) as exc:
        raise ValueError("malformed admission manifest") from exc


def generate_admission_campaign(observations, base_ladder, *, now_ms: int,
        max_candidates: int = 12, groups=None, trained_only: bool = False,
        max_stake_usd: Decimal | str = "30", max_open_per_arm: int = 3) -> dict:
    if type(max_candidates) is not int or not 1 <= max_candidates <= MAX_ARMS:
        raise ValueError("admission cohort exceeds configured twelve-arm ceiling")
    policy = execution_policy(max_stake_usd=max_stake_usd, max_open_per_arm=max_open_per_arm)
    manifest = sc.generate_campaign(observations, base_ladder, now_ms=_int(now_ms),
        max_candidates=max_candidates, groups=groups, trained_only=trained_only)
    if not manifest["hypotheses"]:
        raise ValueError("admission campaign needs at least one declared arm")
    manifest["assessment_policy"] = _assessment_policy(policy)
    for arm in manifest["hypotheses"]:
        arm["spec"][POLICY_KEY] = copy.deepcopy(policy)
        arm["spec_hash"] = sc._hash(arm["spec"])
        arm["hypothesis_id"] = "ph_" + arm["spec_hash"][:24]
        if arm["selection"]:
            arm["selection"]["timing_basis"] = "historical_event_print_screen_only"
            arm["selection"]["admission_timing_proven"] = False
    manifest["campaign_id"] = sc._campaign_id(manifest)
    validate_manifest(manifest, now_ms=now_ms)
    return manifest


class _AdmissionBroker:
    """Timestamp adapter only; real PaperBroker owns quantities, fees and fills."""

    def __init__(self, broker, runner):
        self.broker, self.runner = broker, runner

    def __getattr__(self, name):
        return getattr(self.broker, name)

    def buy(self, *args, **kwargs):
        kwargs["now_ms"] = self.runner._admission_frontier
        return self.broker.buy(*args, **kwargs)

    def sell(self, *args, **kwargs):
        kwargs["now_ms"] = self.runner._admission_frontier
        return self.broker.sell(*args, **kwargs)


class AdmissionPaperRunner(cp.CandidatePaperRunner):
    def __init__(self, state_dir: Path | str, *, risk: Any = None,
                 learning_root: Path | str | None = None, max_stake_usd: Decimal | str = "30",
                 max_open_per_arm: int = 3, clock: Callable[[], float] | None = None):
        self.policy = execution_policy(max_stake_usd=max_stake_usd, max_open_per_arm=max_open_per_arm)
        self._admission_frontier = 0
        super().__init__(state_dir, risk=risk, learning_root=learning_root,
            max_stake_usd=max_stake_usd, max_open_per_arm=max_open_per_arm, clock=clock)

    def _processing_ms(self, frontier: int) -> int:
        self._admission_frontier = super()._processing_ms(frontier)
        return self._admission_frontier

    def _identity(self, manifest, arm):
        validate_manifest(manifest, expected_policy=self.policy)
        if arm not in manifest["hypotheses"]:
            raise ValueError("arm is outside the frozen admission manifest")
        identity = super()._identity(manifest, arm)
        identity["runner"]["execution_clock_policy"] = self.policy
        return identity

    def _broker(self, *args, **kwargs):
        return _AdmissionBroker(super()._broker(*args, **kwargs), self)

    def _fill_evidence(self, conn, order_id, snap, chain, side):
        super()._fill_evidence(conn, order_id, snap, chain, side)
        actual = conn.execute("SELECT created_ms,side,state,mode,provider FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if (not actual or actual["created_ms"] != snap["processing_ms"] or actual["side"] != side
                or actual["state"] != "filled" or actual["mode"] != "shadow" or actual["provider"] != "paper"):
            raise ValueError("admission receipt does not match the actual broker order")
        prior = conn.execute("SELECT value FROM candidate_meta WHERE key=?", ("fill_evidence:" + order_id,)).fetchone()
        evidence = json.loads(prior[0])
        evidence.update(execution_policy=self.policy, side=side, order_id=order_id,
                        mode=actual["mode"], provider=actual["provider"], state=actual["state"],
                        admission_ms=snap["processing_ms"], ledger_ms=actual["created_ms"])
        if side == "sell":
            evidence["exit_timing"] = dict(snap["exit_timing"])
        conn.execute("UPDATE candidate_meta SET value=? WHERE key=?",
                     (cp._json(evidence), "fill_evidence:" + order_id))

    def _exits(self, conn, identity, quotes, now):
        # Timing orchestration only; the frozen evaluator and real broker are reused.
        spec = identity["spec"]
        cfg = cp._protection(spec["exit"])
        rows = conn.execute("SELECT p.position_id,e.signal_id,c.state,c.last_quote_ms,c.pending,"
            "COALESCE(c.last_price_ms,c.last_quote_ms) AS last_price_ms FROM positions p "
            "JOIN candidate_entries e ON e.position_id=p.position_id "
            "JOIN candidate_protection c ON c.position_id=p.position_id WHERE p.closed_ms IS NULL").fetchall()
        for row in rows:
            now = self._processing_ms(now)
            chain = Chain(spec["chain"])
            position_row = conn.execute("SELECT token FROM positions WHERE position_id=?", (row["position_id"],)).fetchone()
            captured = self._quote(quotes, chain.value, position_row[0])
            snap, reason = self._snapshot(captured, now)
            if snap is None:
                conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (reason, row["signal_id"]))
                continue
            if snap["quote_ms"] <= row["last_quote_ms"] or snap["price_ms"] <= row["last_price_ms"]:
                continue
            broker = self._broker(conn, chain, snap, side="sell")
            position = load_position(conn, row["position_id"])
            if position is None or position.mode is not LaneMode.SHADOW:
                raise ValueError("candidate inventory is not an owned shadow position")
            stored = conn.execute("SELECT decimals FROM tokens WHERE chain=? AND address=?",
                                  (chain.value, position.token)).fetchone()
            if not stored or stored[0] != snap["decimals"]:
                conn.execute("UPDATE candidate_entries SET reason='token_decimals_changed' WHERE signal_id=?", (row["signal_id"],))
                continue
            bought = conn.execute("SELECT 1 FROM orders o JOIN position_orders po ON po.order_id=o.order_id "
                "WHERE po.position_id=? AND o.side='buy' AND o.state='filled' AND o.mode='shadow' AND o.provider='paper'",
                (position.position_id,)).fetchone()
            if not bought:
                conn.execute("UPDATE candidate_entries SET reason='unmatched_inventory' WHERE signal_id=?", (row["signal_id"],))
                continue
            state = ProtectionState.model_validate_json(row["state"])
            pending = json.loads(row["pending"]) if row["pending"] else None
            # Inventory/state reads may wait on disk. Recognition happens after
            # those reads, with the original market packet revalidated again.
            now = self._processing_ms(now)
            current, reason = self._snapshot(captured, now)
            if current is None:
                conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (reason, row["signal_id"]))
                continue
            snap = current
            hold = spec["exit"].get("hold_ms")
            if pending is None and hold is not None and snap["price_ms"] >= position.opened_ms + int(hold):
                deadline = position.opened_ms + int(hold)
                pending = {"pct": "100", "reason": "time_exit", "trigger_ms": deadline,
                    "recognized_ms": now, "trigger_price_ms": snap["price_ms"],
                    "due_ms": deadline + self.poll_ms, "basis": "fixed_admission_deadline",
                    "state": state.model_dump(mode="json")}
            if pending is None:
                probe = state.model_copy(deep=True)
                action = evaluate(probe, price_usd=snap["price"], liquidity_usd=snap["depth"], now_ms=now, cfg=cfg)
                if action.sells:
                    pending = {"pct": str(action.pct), "reason": action.reason, "trigger_ms": now,
                        "recognized_ms": now, "trigger_price_ms": snap["price_ms"], "due_ms": now + self.poll_ms,
                        "basis": "observed_price_recognition", "state": probe.model_dump(mode="json")}
                else:
                    state = probe
                    broker.mark_to_market(position, snap["price"], now_ms=snap["quote_ms"])
            if (pending is not None and snap["quote_ms"] >= pending["due_ms"]
                    and snap["price_ms"] >= pending["due_ms"]):
                now = self._processing_ms(now)
                current, reason = self._snapshot(captured, now)
                if current is None:
                    conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (reason, row["signal_id"]))
                    conn.execute("UPDATE candidate_protection SET state=?,pending=? WHERE position_id=?",
                                 (state.model_dump_json(), cp._json(pending), position.position_id))
                    continue
                snap = current
                snap["processing_ms"] = now
                snap["exit_timing"] = {k: pending[k] for k in ("reason", "trigger_ms", "recognized_ms", "trigger_price_ms", "due_ms", "basis")}
                order = broker.sell(position, Decimal(pending["pct"]), price_usd=snap["price"],
                    liquidity_usd=snap["depth"], decimals=snap["decimals"], curve=snap["curve"],
                    now_ms=snap["quote_ms"], latency_ms=0, exit_reason=pending["reason"])
                if order.state is OrderState.FILLED:
                    self._fill_evidence(conn, order.order_id, snap, chain, "sell")
                    state = ProtectionState.model_validate(pending["state"])
                    conn.execute("INSERT OR REPLACE INTO candidate_meta(key,value) VALUES (?,?)",
                                 ("exit_latency:" + order.order_id, str(snap["price_ms"] - pending["trigger_ms"])))
                    pending = None
                    conn.execute("UPDATE candidate_entries SET reason=NULL WHERE signal_id=?", (row["signal_id"],))
                else:
                    conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (order.error, row["signal_id"]))
            conn.execute("UPDATE candidate_protection SET state=?,last_quote_ms=?,last_price_ms=?,pending=? WHERE position_id=?",
                (state.model_dump_json(), snap["quote_ms"], snap["price_ms"], cp._json(pending) if pending else None, position.position_id))

    def _outcomes(self, conn, identity, now):
        rows = super()._outcomes(conn, identity, now)
        for row in rows:
            row[POLICY_KEY] = self.policy
            row["timing_complete"] = timing_error(row, identity["spec"], self.policy) is None
            row["timing_error"] = timing_error(row, identity["spec"], self.policy)
            row["coverage"] = "closed_admission_clock_modelled_paper_fills"
            row["limitations"].append("assumed paper execution at a fresh observed quote; live route execution unproven")
        return rows

    def _incomplete_rows(self, conn, identity, now):
        rows = super()._incomplete_rows(conn, identity, now)
        for row in rows:
            row[POLICY_KEY] = self.policy
            row["timing_complete"] = bool(row.get("known_unfilled"))
        return rows

    def run_candidates(self, manifest, opportunities, quotes, *, now_ms):
        validate_manifest(manifest, now_ms=self._processing_ms(_int(now_ms)), expected_policy=self.policy)
        result = super().run_candidates(manifest, opportunities, quotes, now_ms=now_ms)
        result[POLICY_KEY] = self.policy
        result["activation_authorized"] = False
        return result

    def expire_pending(self, manifest, *, now_ms):
        validate_manifest(manifest, now_ms=self._processing_ms(_int(now_ms)), expected_policy=self.policy)
        return super().expire_pending(manifest, now_ms=now_ms)

    def watched_tokens(self, manifest, *, limit=128, now_ms=None):
        validate_manifest(manifest, now_ms=now_ms, expected_policy=self.policy)
        return super().watched_tokens(manifest, limit=limit, now_ms=now_ms)


def timing_error(row: Mapping[str, Any], spec: Mapping[str, Any], policy: Mapping[str, Any]) -> str | None:
    """Independent timing qualification of every actual filled leg, never a legacy relabel."""
    try:
        if row.get(POLICY_KEY) != policy or row.get("mode") != "shadow":
            return "execution policy or paper mode mismatch"
        signal, opened, closed, observed = (_int(row[k]) for k in ("signal_ms", "opened_ms", "closed_ms", "observed_ms"))
        if _int(row["entry_processing_ms"]) != opened or not signal <= opened <= closed <= observed:
            return "admission chronology mismatch"
        legs = row["fill_evidence"]
        if not isinstance(legs, list) or len(legs) < 2 or legs[0].get("side") != "buy":
            return "missing filled timing legs"
        prior = opened
        order_ids = set()
        for index, leg in enumerate(legs):
            admission, processing, ledger = (_int(leg[k]) for k in ("admission_ms", "processing_ms", "ledger_ms"))
            quote, price, depth, native = (_int(leg[k]) for k in ("quote_ms", "price_ts_ms", "depth_ts_ms", "native_price_ts_ms"))
            if (leg.get(POLICY_KEY) != policy or not leg.get("order_id") or leg["order_id"] in order_ids
                    or leg.get("mode") != "shadow" or leg.get("provider") != "paper" or leg.get("state") != "filled"
                    or admission != processing or ledger != admission
                    or not prior <= admission <= observed or not 0 < price <= quote <= admission
                    or not 0 < depth <= quote or not 0 < native <= quote
                    or admission - min(price, depth, quote) > policy["quote_max_age_ms"]
                    or admission - native > policy["native_quote_max_age_ms"]):
                return "invalid component or admission clock"
            order_ids.add(leg["order_id"])
            if index == 0:
                if admission != opened or price < signal + policy["entry_delay_ms"] or admission > signal + policy["entry_max_age_ms"]:
                    return "invalid entry timing"
            else:
                if leg.get("side") != "sell":
                    return "unexpected filled leg side"
                exit_ = leg["exit_timing"]
                trigger, recognized, trigger_price, due = (_int(exit_[k]) for k in ("trigger_ms", "recognized_ms", "trigger_price_ms", "due_ms"))
                if not 0 < trigger_price <= recognized <= admission or due != trigger + policy["poll_ms"] or price < due:
                    return "exit precedes its executable deadline"
                if exit_["basis"] == "fixed_admission_deadline":
                    hold = spec["exit"].get("hold_ms")
                    if hold is None or exit_["reason"] != "time_exit" or trigger != opened + _int(hold) or recognized < trigger:
                        return "invalid admission holding deadline"
                elif exit_["basis"] != "observed_price_recognition" or trigger != recognized or exit_["reason"] == "time_exit":
                    return "invalid trigger recognition"
            prior = admission
        if prior != closed:
            return "last sell does not match the broker close"
        return None
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        return "malformed or missing admission timing evidence"


def assess_admission_campaign(manifest, paper_outcomes, *, now_ms, prior_state=None):
    policy = validate_manifest(manifest, now_ms=now_ms)
    policy_hash = sc._hash(policy)
    if (prior_state and prior_state.get("campaign_id") == manifest["campaign_id"]
            and prior_state.get("execution_policy_hash") != policy_hash):
        raise ValueError("prior assessment lacks the same strict execution policy")
    arms = {arm["hypothesis_id"]: arm for arm in manifest["hypotheses"]}
    qualified, failures = [], []
    for supplied in paper_outcomes:
        row = dict(supplied)
        arm = arms.get(row.get("hypothesis_id"))
        if row.get("campaign_id") == manifest["campaign_id"] and arm is not None:
            reason = "execution policy mismatch" if row.get(POLICY_KEY) != policy else None
            if row.get("status") == "closed":
                reason = reason or timing_error(row, arm["spec"], policy)
                if row.get("timing_complete") is not True:
                    reason = reason or "timing qualification absent"
            if reason:
                failures.append({"outcome_id": row.get("outcome_id"), "reason": reason})
                row["evidence_complete"] = False
        qualified.append(row)
    result = sc.assess_campaign(manifest, qualified, now_ms=now_ms, prior_state=prior_state)
    result[POLICY_KEY] = policy
    result["timing_failures"] = failures
    result["legacy_outcomes_qualified"] = 0
    result["activation_authorized"] = False
    result["next_state"]["execution_policy_hash"] = policy_hash
    return result


def inspect_rollover(prior_manifests: Sequence[Mapping[str, Any]], state_dir: Path | str, *,
        learning_root: Path | str | None = None, now_ms: int, max_active_arms: int = 12,
        guard: Callable[[], None] | None = None) -> dict:
    """Bounded read-only preflight, never an archive, activation or exposure write-off.

    The integrating worker must hold its exclusive lock and recheck before CAS.
    Missing/capped/conflicting evidence remains unknown, even with zero sampled rows.
    """
    if type(max_active_arms) is not int or not 1 <= max_active_arms <= MAX_ARMS:
        raise ValueError("rollover cannot increase configured active-arm capacity")
    now = _int(now_ms)
    runner = cp.CandidatePaperRunner(state_dir, learning_root=learning_root)
    root = Path(state_dir)
    if root.is_symlink() or any(p.is_symlink() for p in root.parents if p != p.parent):
        raise ValueError("rollover research path contains a symlink")
    arms = sum(len(m.get("hypotheses", [])) for m in prior_manifests)
    if not 1 <= arms <= max_active_arms:
        raise ValueError("rollover active-arm cap or missing cohort")
    checks, blockers, seen = [], [], set()
    for manifest in prior_manifests:
        if (manifest.get("source_versions") != sc.source_versions()
                or manifest.get("campaign_id") != sc._campaign_id(manifest)
                or not 0 < _int(manifest["selected_ms"]) <= now):
            raise ValueError("legacy frozen manifest identity changed")
        for arm in manifest["hypotheses"]:
            key = manifest["campaign_id"], arm["hypothesis_id"]
            if key in seen:
                raise ValueError("duplicate rollover book")
            seen.add(key)
            path = runner.book_path(*key)
            check = {"campaign_id": key[0], "hypothesis_id": key[1], "book": path.name, "status": "unknown"}
            conn = None
            try:
                if guard is not None:
                    guard()
                if path.is_symlink() or not path.is_file():
                    raise ValueError("missing_or_symlink_legacy_book")
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.25, isolation_level=None)
                conn.row_factory = sqlite3.Row
                began = time.monotonic()
                conn.set_progress_handler(lambda started=began: int(time.monotonic() - started >= 3), 1000)
                conn.execute("PRAGMA query_only=ON")
                stored = conn.execute("SELECT substr(value,1,32769) FROM candidate_meta WHERE key='identity'").fetchone()
                if not stored or len(stored[0]) > 32768:
                    raise ValueError("legacy_identity_missing_or_capped")
                identity = json.loads(stored[0])
                policy = identity["runner"]
                actual = cp.CandidatePaperRunner(state_dir, learning_root=learning_root,
                    max_stake_usd=policy["max_stake_usd"], max_open_per_arm=policy["max_open_per_arm"],
                    clock=(lambda: now / 1000) if policy["processing_clock_policy"] == "observed_wall_clock/1" else None)
                if actual._identity(manifest, arm) != identity:
                    raise ValueError("legacy_book_identity_mismatch")
                # Reuse the original reader's exact stored-identity/foreign-order
                # guard before interpreting any economic or inventory rows.
                conn.close()
                conn = actual._existing(identity)
                conn.set_progress_handler(lambda started=began: int(time.monotonic() - started >= 3), 1000)
                tick = conn.execute("SELECT value FROM candidate_meta WHERE key='last_tick_ms'").fetchone()
                if tick and not 0 < _int(tick[0]) <= now:
                    raise ValueError("future_or_invalid_legacy_tick")
                columns = {"candidate_entries": "status,position_id",
                    "orders": "order_id,decision_id,mode,provider,side,state,chain,token,created_ms,amount_in,filled_out",
                    "positions": "position_id,mode,chain,token,qty,qty_total,opened_ms,closed_ms",
                    "position_orders": "position_id,order_id"}
                tables = {name: conn.execute("SELECT " + fields + " FROM " + name + " LIMIT ?",
                                             (ROW_CAP + 1,)).fetchall() for name, fields in columns.items()}
                if any(len(rows) > ROW_CAP for rows in tables.values()):
                    raise ValueError("legacy_book_row_cap_unknown")
                entries, orders, positions, links = (tables[name] for name in ("candidate_entries", "orders", "positions", "position_orders"))
                position_map = {row["position_id"]: row for row in positions}
                order_map = {row["order_id"]: row for row in orders}
                if any(row["mode"] != "shadow" or row["provider"] != "paper" for row in orders):
                    raise ValueError("foreign_execution_in_legacy_book")
                if any(row["side"] not in ("buy", "sell") for row in orders):
                    raise ValueError("unknown_legacy_order_side")
                if any(row["mode"] != "shadow" for row in positions):
                    raise ValueError("foreign_inventory_in_legacy_book")
                open_ = sum(row["closed_ms"] is None or _int(row["qty"]) != 0 for row in positions)
                pending = sum(row["status"] == "pending" for row in entries)
                ambiguous = sum(row["state"] not in ("filled", "failed", "expired", "cancelled") for row in orders)
                unmatched = 0
                for order in orders:
                    if not 0 < _int(order["created_ms"]) <= now:
                        raise ValueError("future_or_invalid_legacy_order")
                    if order["state"] == "filled" and not any(link["order_id"] == order["order_id"]
                            and link["position_id"] in position_map
                            and position_map[link["position_id"]]["chain"] == order["chain"]
                            and position_map[link["position_id"]]["token"] == order["token"] for link in links):
                        unmatched += 1
                if any(link["order_id"] not in order_map or link["position_id"] not in position_map for link in links):
                    unmatched += 1
                if any(sum(link["order_id"] == order["order_id"] for link in links) > 1 for order in orders):
                    unmatched += 1
                if any(row["position_id"] is not None and row["position_id"] not in position_map for row in entries):
                    unmatched += 1
                for position in positions:
                    matched = [order_map[link["order_id"]] for link in links
                        if link["position_id"] == position["position_id"] and link["order_id"] in order_map
                        and order_map[link["order_id"]]["state"] == "filled"
                        and order_map[link["order_id"]]["chain"] == position["chain"]
                        and order_map[link["order_id"]]["token"] == position["token"]]
                    buys = [order for order in matched if order["side"] == "buy"]
                    sells = [order for order in matched if order["side"] == "sell"]
                    if not buys:
                        unmatched += 1
                    if position["closed_ms"] is not None:
                        bought = [_int(order["filled_out"]) for order in buys]
                        # PaperBroker FILLED sells consume their exact amount_in
                        # token atoms; filled_out is the native credit on that side.
                        sold = [_int(order["amount_in"]) for order in sells]
                        if (not sells or not any(order["created_ms"] == position["closed_ms"] for order in sells)
                                or any(amount <= 0 for amount in bought + sold)
                                or sum(bought) != sum(sold) or sum(bought) != _int(position["qty_total"])):
                            unmatched += 1
                if any(not 0 < _int(row["opened_ms"]) <= now or row["closed_ms"] is not None
                       and not _int(row["opened_ms"]) <= _int(row["closed_ms"]) <= now for row in positions):
                    raise ValueError("future_or_invalid_legacy_inventory")
                check.update(status="blocked" if open_ or pending or ambiguous or unmatched else "settled",
                    open_inventory=open_, pending_attempts=pending, ambiguous_orders=ambiguous,
                    unmatched_ledger_rows=unmatched, orders=len(orders), positions=len(positions),
                    identity_sha256=hashlib.sha256(stored[0].encode()).hexdigest())
            except (ValueError, KeyError, TypeError, ArithmeticError, sqlite3.Error, OSError) as exc:
                check["reason"] = type(exc).__name__ + ":" + str(exc)[:160]
            finally:
                if conn is not None:
                    conn.close()
            checks.append(check)
            if check["status"] != "settled":
                blockers.append({"book": path.name, "status": check["status"], "reason": check.get("reason", "ledger_not_settled")})
    return {"status": "ready_for_explicit_archive" if not blockers else "blocked", "checks": checks,
        "blockers": blockers, "active_arm_count": arms, "configured_arm_cap": max_active_arms,
        "activation_authorized": False, "archive_performed": False, "writes": 0, "provider_calls": 0,
        "requires_exclusive_worker_lock_and_fresh_CAS": True,
        "history_retention_required": ["books", "manifests", "source_hashes", "assessment", "outcome_ids"]}


__all__ = ["AdmissionPaperRunner", "generate_admission_campaign", "assess_admission_campaign",
           "execution_policy", "validate_manifest", "timing_error", "inspect_rollover"]
