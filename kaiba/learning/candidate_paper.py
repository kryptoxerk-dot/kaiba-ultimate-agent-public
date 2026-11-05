"""Prospective, isolated candidate books using the existing shadow broker.

Owned by Codex, AUTOLEARN-IMPLEMENT-20261014. This module consumes supplied
point-in-time snapshots; it never obtains prices, funds, or the production database.
PaperBroker owns every fill and ledger calculation. Protection.evaluate owns exit
arithmetic. Missing quotes leave inventory open and cannot create a measured winner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings
from kaiba.core.db import MIGRATIONS_DIR, connect, migrate
from kaiba.core.schemas import (
    NATIVE_DECIMALS,
    Action,
    Chain,
    Decision,
    Lane,
    LaneMode,
    OrderState,
)
from kaiba.execution.curve_price import (
    CURVE_CREATOR_FEE_BPS,
    CURVE_TOTAL_FEE_BPS,
    PUMPSWAP_TIER1_TOTAL_FEE_BPS,
    CurveState,
)
from kaiba.execution.paper import (
    DEFAULT_FEE_BPS,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_SLIPPAGE_FLOOR_BPS,
    DEFAULT_TIP_NATIVE,
    PaperBroker,
    fill_basis,
    load_position,
)
from kaiba.execution.protection import ProtectionConfig, ProtectionState, evaluate
from kaiba.learning.strategy_search import Cond, ExitPolicy

ASSUMED_GMGN_COMMISSION_BPS = {Chain.SOL: 100, Chain.ROBINHOOD: 100, Chain.BSC: 100}
COMMISSION_IN_DEFAULT_BPS = {Chain.BSC: 100}
COMMISSION_SOURCE = "https://docs.gmgn.ai/index/gmgn-fees-settings"


def _pool_fee_bps(chain: Chain) -> int:
    return DEFAULT_FEE_BPS[chain] + ASSUMED_GMGN_COMMISSION_BPS.get(chain, 0) - COMMISSION_IN_DEFAULT_BPS.get(chain, 0)


def _curve_fee_bps() -> int:
    # Curve fees are applied to the reserve leg, whereas a router can deduct its
    # commission from the entire input budget. Bound that convention by rounding
    # ((1 + venue_fee) / (1 - commission) - 1) upward to a whole basis point.
    # The broker's resulting protocol split is an aggregate MODEL, not a claim
    # that pump.fun's actual protocol fee or recipient allocation changed.
    commission = ASSUMED_GMGN_COMMISSION_BPS[Chain.SOL]
    numerator = (CURVE_TOTAL_FEE_BPS + commission) * 10_000
    denominator = 10_000 - commission
    return -(-numerator // denominator)


def _fee_policy() -> dict[str, Any]:
    return {
        "version": "candidate_assumed_commission/1",
        "commission_bps": {c.value: n for c, n in ASSUMED_GMGN_COMMISSION_BPS.items()},
        "commission_in_broker_default_bps": {c.value: n for c, n in COMMISSION_IN_DEFAULT_BPS.items()},
        "venue_allowance_bps": {c.value: n - COMMISSION_IN_DEFAULT_BPS.get(c, 0) for c, n in DEFAULT_FEE_BPS.items()},
        "curve_venue_fee_bps": CURVE_TOTAL_FEE_BPS,
        "pumpswap_venue_fee_bps": PUMPSWAP_TIER1_TOTAL_FEE_BPS,
        "pool_total_fee_bps_before_token_tax": {c.value: _pool_fee_bps(c) for c in DEFAULT_FEE_BPS},
        "curve_total_fee_bps": _curve_fee_bps(),
        "pumpswap_total_fee_bps": PUMPSWAP_TIER1_TOTAL_FEE_BPS + ASSUMED_GMGN_COMMISSION_BPS[Chain.SOL],
        "commission_source": COMMISSION_SOURCE,
        "commission_basis": "published schedule assumed for this route; account charge and rebate unverified",
        "curve_basis": "ceil(10000*((1+venue_bps/10000)/(1-commission_bps/10000)-1)); conservative aggregate model",
        "curve_protocol_split_is_observed": False,
        "venue_coverage": "supplied SOL curve or explicit timestamped venue_fee_bps within the modeled allowance",
        "cost_model_assumed": True, "live_cost_verified": False,
    }


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _positive(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except Exception:  # malformed provider data is missing evidence
        return None


def _captured_clock(value: Any, *, lower: int, upper: int) -> int | None:
    try:
        n = Decimal(str(value))
        return int(n) if (not isinstance(value, (bool, float)) and n.is_finite()
                          and n == int(n) and 0 < lower <= n <= upper) else None
    except (ValueError, TypeError, ArithmeticError):
        return None


def _protection(spec: Mapping[str, Any]) -> ProtectionConfig:
    """Translate replay's original-position fractions to remaining-position percentages."""
    policy = ExitPolicy.from_spec(spec)
    remaining, rungs = Decimal(1), []
    for multiple, fraction in sorted(policy.ladder.tps):
        fraction = Decimal(str(fraction))
        if fraction <= 0 or fraction > remaining or multiple <= 1:
            raise ValueError("invalid frozen take-profit fractions")
        rungs.append((Decimal(str(multiple)), fraction * 100 / remaining))
        remaining -= fraction
    if not 0 < policy.ladder.stop_frac < 1:
        raise ValueError("invalid frozen stop")
    if any(m <= 1 or not 0 < t < 1 for m, t in policy.ladder.trailing):
        raise ValueError("invalid frozen trailing ladder")
    if policy.hold_ms is not None and policy.hold_ms <= 0:
        raise ValueError("invalid frozen hold deadline")
    return ProtectionConfig(
        stop_loss_bps=int(Decimal(str(policy.ladder.stop_frac)) * 10_000),
        tp_ladder=rungs,
        trailing=[(Decimal(str(m)), int(Decimal(str(t)) * 10_000))
                  for m, t in sorted(policy.ladder.trailing)],
        breakeven_after_tp1=policy.ladder.breakeven_after_tp1,
        # These rules were absent from ExitPolicy.spec(): do not silently add them.
        emergency_loss_bps=10_000,
        rug_liquidity_drop_pct=Decimal(101),
        stale_no_volume_exit_s=0,
        moonbag_retain_pct=0,
        use_provider_orders=False,
    )


class CandidatePaperRunner:
    """A frozen campaign's equal-capacity paper books, on explicit research paths.

    ``risk`` is accepted for integration convenience but never consulted: runtime
    live/canary modes cannot change this runner's hardcoded SHADOW decisions.
    The default learning root is settings.data_dir/learning. An explicit root is
    useful for an owner-designated research directory and offline test isolation.
    """

    def __init__(
        self,
        state_dir: Path | str,
        *,
        risk: Any = None,
        learning_root: Path | str | None = None,
        max_stake_usd: Decimal | str = "120",
        max_open_per_arm: int = 3,
        quote_max_age_ms: int = 60_000,
        entry_delay_ms: int = 30_000,
        poll_ms: int = 12_000,
        entry_max_age_ms: int = 120_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        del risk
        settings = get_settings()
        self.learning_root = Path(learning_root or settings.kaiba_data_dir / "learning").resolve()
        self.state_dir = Path(state_dir).resolve()
        self.main_db = settings.db_path.resolve()
        self.max_stake_usd = Decimal(str(max_stake_usd))
        if not self.max_stake_usd.is_finite() or not 0 < self.max_stake_usd <= 120:
            raise ValueError("paper stake must be positive and at most $120")
        if max_open_per_arm <= 0 or quote_max_age_ms < 0 or entry_delay_ms <= 0 or poll_ms <= 0:
            raise ValueError("invalid paper capacity or latency policy")
        if entry_max_age_ms < entry_delay_ms:
            raise ValueError("entry lifetime is shorter than its delay")
        self.max_open_per_arm = int(max_open_per_arm)
        self.quote_max_age_ms = int(quote_max_age_ms)
        self.entry_delay_ms = int(entry_delay_ms)
        self.poll_ms = int(poll_ms)
        self.entry_max_age_ms = int(entry_max_age_ms)
        self.clock = clock
        self._processing_frontier_ms = 0
        self._migration_names = frozenset(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
        self._check_path(self.state_dir)

    def _processing_ms(self, frontier: int) -> int:
        if self.clock is None:
            return int(frontier)
        try:
            supplied = self.clock()
            seconds = Decimal(str(supplied))
            if isinstance(supplied, bool) or not seconds.is_finite() or seconds <= 0:
                raise ValueError("invalid processing clock")
            self._processing_frontier_ms = max(int(frontier), int(seconds * 1000), self._processing_frontier_ms)
            return self._processing_frontier_ms
        except (TypeError, ArithmeticError) as exc:
            raise ValueError("invalid processing clock") from exc

    def _check_path(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.learning_root) or resolved == self.main_db:
            raise ValueError("candidate book must stay inside the owned learning root")
        if resolved.name.lower() == "kaiba.db":
            raise ValueError("canonical database name is forbidden for candidate books")
        if resolved.exists() and self.main_db.exists() and resolved.samefile(self.main_db):
            raise ValueError("candidate book aliases the production database")

    def book_path(self, campaign_id: str, hypothesis_id: str) -> Path:
        path = self.state_dir / ("candidate-" + _hash([campaign_id, hypothesis_id])[:32] + ".db")
        self._check_path(path)
        return path

    def _open(self, identity: dict[str, Any]) -> sqlite3.Connection:
        path = self.book_path(identity["campaign_id"], identity["hypothesis_id"])
        conn = connect(path)  # never ensure_db/get_conn: their thread-local fallback may be production
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            frozen = _json(identity)
            prior = (conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()
                     if "candidate_meta" in tables else None)
            if prior and prior[0] != frozen:
                raise ValueError("frozen candidate identity or execution policy changed")
            applied = ({r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
                       if "schema_migrations" in tables else set())
            if not self._migration_names.issubset(applied):
                migrate(conn)
            definitions = {
                "candidate_meta": "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "candidate_entries": "(signal_id TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,decision_id TEXT,"
                " position_id TEXT,quote_ms INTEGER,latency_ms INTEGER)",
                "candidate_protection": "(position_id TEXT PRIMARY KEY,"
                " state TEXT NOT NULL,last_quote_ms INTEGER NOT NULL,pending TEXT)",
            }
            for table, definition in definitions.items():
                if table not in tables:
                    conn.execute(f"CREATE TABLE {table} {definition}")
            # Additive research-only migration. Preserve the frozen identity and
            # original attempt payload; an expiry is an event, not a new cohort.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(candidate_entries)")}
            additions = {"expired_ms": "INTEGER", "registered_ms": "INTEGER", "last_checked_ms": "INTEGER",
                         "last_quote_reason": "TEXT", "last_quote_diagnostics_json": "TEXT",
                         "expiry_diagnostics_json": "TEXT", "processing_ms": "INTEGER",
                         "source_observed_ms": "INTEGER", "candidate_received_ms": "INTEGER",
                         "last_admission_ms": "INTEGER", "last_admission_stage": "TEXT",
                         "preparation_started_ms": "INTEGER", "last_usable_quote_json": "TEXT"}
            for column, kind in additions.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE candidate_entries ADD COLUMN {column} {kind}")
            protection_columns = {r[1] for r in conn.execute("PRAGMA table_info(candidate_protection)")}
            if "last_price_ms" not in protection_columns:
                conn.execute("ALTER TABLE candidate_protection ADD COLUMN last_price_ms INTEGER")
            if conn.execute("SELECT 1 FROM orders WHERE mode!='shadow' OR provider!='paper' LIMIT 1").fetchone():
                raise ValueError("candidate book contains foreign execution inventory")
            if prior is None:
                conn.execute("INSERT INTO candidate_meta VALUES ('identity',?)", (frozen,))
            return conn
        except Exception:
            conn.close()
            raise

    def _existing(self, identity: Mapping[str, Any]) -> sqlite3.Connection:
        """An existing owned book read without WAL configuration, DDL or migrations."""
        path = self.book_path(identity["campaign_id"], identity["hypothesis_id"])
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            prior = conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()
            if not prior or prior[0] != _json(identity):
                raise ValueError("frozen candidate identity or execution policy changed")
            if conn.execute("SELECT 1 FROM orders WHERE mode!='shadow' OR provider!='paper' LIMIT 1").fetchone():
                raise ValueError("candidate book contains foreign execution inventory")
            return conn
        except Exception:
            conn.close()
            raise

    def _identity(self, manifest: Mapping[str, Any], arm: Mapping[str, Any]) -> dict[str, Any]:
        if arm.get("status") != "PAPER_ONLY":
            raise ValueError("candidate must be PAPER_ONLY")
        spec = dict(arm["spec"])
        Lane(spec["lane"])
        Chain(spec["chain"])
        _protection(spec["exit"])
        selected = int(manifest.get("selected_ms", manifest.get("freeze_ms", 0)))
        if selected <= 0 or not arm.get("spec_hash") or not arm.get("hypothesis_id"):
            raise ValueError("candidate lacks a frozen identity")
        return {
            "campaign_id": str(manifest["campaign_id"]),
            "hypothesis_id": str(arm["hypothesis_id"]),
            "spec_hash": str(arm["spec_hash"]),
            "spec": spec,
            "selected_ms": selected,
            "runner": {
                "version": "candidate_paper/1", "max_stake_usd": str(self.max_stake_usd),
                "max_open_per_arm": self.max_open_per_arm, "quote_max_age_ms": self.quote_max_age_ms,
                "entry_delay_ms": self.entry_delay_ms, "poll_ms": self.poll_ms,
                "entry_max_age_ms": self.entry_max_age_ms,
                "price_clock_policy": "component_price_after_delay/1",
                "processing_clock_policy": "observed_wall_clock/1" if self.clock is not None else "explicit_frontier/1",
                "native_quote_max_age_ms": 300_000,
                "fee_policy": _fee_policy(),
                "fees": {c.value: _pool_fee_bps(c) for c in DEFAULT_FEE_BPS},
                "tips": {c.value: v for c, v in DEFAULT_TIP_NATIVE.items()},
                "slippage_bps": DEFAULT_SLIPPAGE_BPS,
                "slippage_floor_bps": DEFAULT_SLIPPAGE_FLOOR_BPS,
                "curve_fee_bps": _curve_fee_bps(),
                "curve_creator_fee_bps": CURVE_CREATOR_FEE_BPS,
                "pumpswap_fee_bps": PUMPSWAP_TIER1_TOTAL_FEE_BPS + ASSUMED_GMGN_COMMISSION_BPS[Chain.SOL],
                # A later broker/protection upgrade cannot rewrite an existing policy's forward book.
                "model_source_sha256": {
                    name: hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest()
                    for name in ("kaiba.execution.paper", "kaiba.execution.curve_price",
                                 "kaiba.execution.protection")
                },
            },
        }

    @staticmethod
    def _quote(quotes: Mapping[Any, Any], chain: str, token: str) -> Mapping[str, Any] | None:
        found = quotes.get((chain, token)) or quotes.get(f"{chain}:{token}")
        if found is None and isinstance(quotes.get(chain), Mapping):
            found = quotes[chain].get(token)
        if not isinstance(found, Mapping):
            return None
        if found.get("chain", chain) != chain or found.get("token", token) != token:
            return None
        return found

    def _snapshot(self, raw: Mapping[str, Any] | None, now: int) -> tuple[dict[str, Any] | None, str]:
        if raw is None:
            return None, "quote_unavailable"
        if raw.get("unavailable_reason"):
            return None, str(raw["unavailable_reason"])
        try:
            quote_ms = int(raw.get("quote_ts_ms", raw.get("observed_ms", 0)))
            decimals = int(raw["decimals"])
            if not 0 <= now - quote_ms <= self.quote_max_age_ms or quote_ms <= 0:
                return None, "quote_stale_or_future"
            # Availability may advance with depth or precision alone. Such a
            # packet cannot turn an older market print into a post-delay price.
            # Older coherent callers supplied one clock for the whole packet.
            price_clock = raw.get("price_ts_ms", quote_ms)
            price_ms = int(price_clock)
            if (isinstance(price_clock, bool) or Decimal(str(price_clock)) != price_ms
                    or not 0 < price_ms <= quote_ms):
                return None, "price_timestamp_invalid"
            if not 0 <= now - price_ms <= self.quote_max_age_ms:
                return None, "price_stale_or_future"
            depth_ms = int(raw.get("depth_ts_ms", quote_ms))
            if (isinstance(raw.get("depth_ts_ms"), bool)
                    or Decimal(str(raw.get("depth_ts_ms", quote_ms))) != depth_ms
                    or not 0 < depth_ms <= quote_ms):
                return None, "depth_timestamp_invalid"
            native_clock = raw.get("native_price_ts_ms", quote_ms)
            native_ms = int(native_clock)
            if (isinstance(native_clock, bool) or Decimal(str(native_clock)) != native_ms
                    or not 0 < native_ms <= quote_ms):
                return None, "native_price_timestamp_invalid"
            if not 0 <= now - native_ms <= 300_000:
                return None, "native_price_stale_or_future"
            if not 0 <= decimals <= 30 or not raw.get("source", raw.get("basis")):
                return None, "quote_metadata_missing"
            native = _positive(raw.get("native_usd"))
            price = _positive(raw.get("price_usd"))
            depth = _positive(raw.get("liquidity_usd"))
            curve = None
            if raw.get("curve") is not None:
                curve, reason = CurveState.build(**dict(raw["curve"]))
                if curve is None or not 0 <= now - curve.observed_ms <= self.quote_max_age_ms:
                    return None, "curve_unusable:" + reason
                if curve.observed_ms != quote_ms:
                    return None, "curve_quote_timestamp_mismatch"
                if native is not None:
                    price = curve.spot_price_usd(native, decimals)
            if native is None or price is None or (depth is None and curve is None):
                return None, "price_native_or_depth_missing"
            if curve is None and not 0 <= now - depth_ms <= self.quote_max_age_ms:
                return None, "depth_stale_or_future"
            return {"quote_ms": quote_ms, "price_ms": price_ms, "depth_ms": depth_ms,
                    "native_ms": native_ms, "price": price, "depth": depth, "native": native,
                    "decimals": decimals, "curve": curve,
                    "venue_fee_bps": raw.get("venue_fee_bps"), "venue_fee_source": raw.get("venue_fee_source"),
                    "venue_fee_ts_ms": raw.get("venue_fee_ts_ms", quote_ms),
                    "venue_fee_max_age_ms": self.quote_max_age_ms,
                    "source": str(raw.get("source", raw.get("basis"))),
                    "buy_tax_bps": raw.get("buy_tax_bps", 0 if raw.get("tax_known_zero") is True else None),
                    "sell_tax_bps": raw.get("sell_tax_bps", 0 if raw.get("tax_known_zero") is True else None)}, "ok"
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return None, "quote_malformed"

    @staticmethod
    def _broker(conn: sqlite3.Connection, chain: Chain, snapshot: Mapping[str, Any], *, side: str) -> PaperBroker:
        if snapshot["curve"] is not None and chain is not Chain.SOL:
            raise ValueError("SOL curve reserves cannot price an EVM candidate")
        tax = snapshot.get(side + "_tax_bps")
        if tax is not None and not 0 <= int(tax) < 10_000:
            raise ValueError("invalid supplied token tax")
        return PaperBroker(
            conn, native_usd={chain: snapshot["native"]}, provider="paper",
            fee_bps={chain: _pool_fee_bps(chain) + int(tax or 0)},
            curve_fee_bps=_curve_fee_bps(),
            pumpswap_fee_bps=PUMPSWAP_TIER1_TOTAL_FEE_BPS + ASSUMED_GMGN_COMMISSION_BPS[Chain.SOL],
            curve_resolver=lambda *_: (None, "candidate_supplied_snapshot_only"), latency_ms=0,
        )

    @staticmethod
    def _fill_evidence(conn: sqlite3.Connection, order_id: str, snap: Mapping[str, Any], chain: Chain,
                       side: str) -> None:
        basis = fill_basis(conn, order_id) or {}
        allowance = (PUMPSWAP_TIER1_TOTAL_FEE_BPS if basis.get("venue") == "pumpswap-tier1"
                     else DEFAULT_FEE_BPS[chain] - COMMISSION_IN_DEFAULT_BPS.get(chain, 0))
        venue_complete = snap["curve"] is not None
        try:
            rate, observed = Decimal(str(snap.get("venue_fee_bps"))), Decimal(str(snap.get("venue_fee_ts_ms")))
            venue_complete |= (not isinstance(snap.get("venue_fee_bps"), bool)
                and not isinstance(snap.get("venue_fee_ts_ms"), bool) and rate.is_finite() and observed.is_finite()
                and rate == int(rate) and 0 <= rate <= allowance and observed == int(observed)
                and 0 < observed <= snap["quote_ms"] and bool(snap.get("venue_fee_source"))
                and 0 <= snap.get("processing_ms", snap["quote_ms"]) - observed <= snap["venue_fee_max_age_ms"])
        except (ValueError, TypeError, ArithmeticError):
            pass
        evidence = {"quote_ms": snap["quote_ms"], "price_ts_ms": snap["price_ms"],
                    "depth_ts_ms": snap["depth_ms"], "processing_ms": snap.get("processing_ms"), "source": snap["source"],
                    "native_usd": str(snap["native"]), "native_price_ts_ms": snap["native_ms"], "decimals": snap["decimals"],
                    "tax_bps": snap.get(side + "_tax_bps"),
                    "tax_complete": (snap.get(side + "_tax_bps") is not None
                                     and (snap["curve"] is None or int(snap[side + "_tax_bps"]) == 0)),
                    "commission_complete": chain in ASSUMED_GMGN_COMMISSION_BPS,
                    "venue_complete": bool(venue_complete), "venue_fee_bps": snap.get("venue_fee_bps"),
                    "venue_fee_source": snap.get("venue_fee_source"), "venue_fee_ts_ms": snap.get("venue_fee_ts_ms"),
                    "fee_policy": _fee_policy(),
                    "cost_model_assumed": True}
        conn.execute("INSERT OR REPLACE INTO candidate_meta(key,value) VALUES (?,?)",
                     ("fill_evidence:" + order_id, _json(evidence)))

    def _register(
        self, conn: sqlite3.Connection, identity: Mapping[str, Any], opportunities: Sequence[Mapping[str, Any]],
        now: int, *, received_ms: int,
    ) -> None:
        spec = identity["spec"]
        conditions = [Cond.from_spec(c) for c in spec.get("filter", [])]
        for supplied in opportunities:
            raw = dict(supplied)
            signal_id = str(raw.get("signal_id", raw.get("decision_id", "")))
            if not signal_id or raw.get("lane") != spec["lane"] or raw.get("chain") != spec["chain"]:
                continue
            ts = int(raw.get("signal_ms", raw.get("ts_ms", 0)))
            raw["signal_ms"], raw["signal_id"] = ts, signal_id
            reason = None
            if not identity["selected_ms"] < ts <= now:
                reason = "outside_prospective_window"
            elif int(raw.get("feature_ts_ms", ts)) > ts:
                reason = "future_features"
            elif not all(c.test(raw.get("features", {})) for c in conditions):
                reason = "filter_not_matched"
            received = _captured_clock(received_ms, lower=max(1, ts), upper=now)
            observed = (_captured_clock(raw.get("source_observed_ms"), lower=max(1, ts), upper=received)
                        if received is not None else None)
            conn.execute(
                "INSERT OR IGNORE INTO candidate_entries(signal_id,payload,status,reason,registered_ms,"
                "source_observed_ms,candidate_received_ms) VALUES (?,?,?,?,?,?,?)",
                (signal_id, _json(raw), "rejected" if reason else "pending", reason, now, observed, received),
            )

    def _entries(self, conn: sqlite3.Connection, identity: Mapping[str, Any], quotes: Mapping[Any, Any], now: int):
        spec, chain = identity["spec"], Chain(identity["spec"]["chain"])
        self._expire_pending_rows(conn, identity, now)
        rows = conn.execute("SELECT * FROM candidate_entries WHERE status='pending' ORDER BY signal_id").fetchall()
        for row in rows:
            now = self._processing_ms(now)
            raw = json.loads(row["payload"])
            signal_ms = raw["signal_ms"]
            if now - signal_ms > self.entry_max_age_ms:
                # Existing ledger exposure needs reconciliation, never a fresh buy
                # or a claim of proved zero-exposure refusal.
                self._expire_pending_rows(conn, identity, now)
                continue
            conn.execute("UPDATE candidate_entries SET last_checked_ms=?,last_admission_stage='checking_quote' WHERE signal_id=?",
                         (now, row["signal_id"]))
            now = self._processing_ms(now)
            if now - signal_ms > self.entry_max_age_ms:
                self._expire_pending_rows(conn, identity, now)
                continue
            latest = self._quote(quotes, chain.value, raw["token"])
            captured = latest if latest is not None else raw
            if latest is None and not any(raw.get(key) for key in ("quote_ts_ms", "observed_ms")):
                snap, reason = None, str(raw.get("quote_missing_reason") or "quote_unavailable")
            else:
                snap, reason = self._snapshot(latest if latest is not None else raw, now)
            cutoff = signal_ms + self.entry_delay_ms
            if snap is None or snap["quote_ms"] < cutoff or snap["price_ms"] < cutoff:
                failure = (reason if snap is None else "entry_delay" if snap["quote_ms"] < cutoff
                           else "entry_price_before_delay")
                diagnostics = {"reason": failure, "checked_ms": now,
                               "quote_ms": snap["quote_ms"] if snap else None,
                               "price_ts_ms": snap["price_ms"] if snap else None,
                               "capture": latest.get("diagnostics") if latest is not None else raw.get("quote_diagnostics")}
                conn.execute(
                    "UPDATE candidate_entries SET reason=?,last_quote_reason=?,last_quote_diagnostics_json=?,"
                    "last_checked_ms=?,last_admission_stage='quote_unusable' WHERE signal_id=?",
                    (failure, failure, _json(diagnostics), now, row["signal_id"]),
                )
                continue
            usable = {"checked_ms": now, "quote_ts_ms": snap["quote_ms"], "price_ts_ms": snap["price_ms"],
                      "depth_ts_ms": snap["depth_ms"], "native_price_ts_ms": snap["native_ms"],
                      "source": snap["source"], "entry_cutoff_ms": cutoff,
                      "entry_deadline_ms": signal_ms + self.entry_max_age_ms}
            conn.execute("UPDATE candidate_entries SET last_admission_ms=?,last_admission_stage='preparing_entry',"
                         "preparation_started_ms=?,last_usable_quote_json=? WHERE signal_id=?",
                         (now, now, _json(usable), row["signal_id"]))
            count = conn.execute("SELECT COUNT(*) FROM positions WHERE closed_ms IS NULL").fetchone()[0]
            if count >= self.max_open_per_arm:
                reason = "arm_capacity"
            elif conn.execute("SELECT 1 FROM positions WHERE token=? AND closed_ms IS NULL", (raw["token"],)).fetchone():
                reason = "token_already_open"
            else:
                reason = None
            if reason:
                conn.execute("UPDATE candidate_entries SET status='rejected',reason=? WHERE signal_id=?",
                             (reason, row["signal_id"]))
                continue
            requested = int(raw.get("size_base_units") or 0)
            # Include the flat entry transaction cost in the $120 ceiling.
            scale = Decimal(10) ** NATIVE_DECIMALS[chain]
            ceiling = int(self.max_stake_usd / snap["native"] * scale) - DEFAULT_TIP_NATIVE[chain]
            if "paper_stake_usd" in raw:
                # Pending snapshots retain the USD policy, not an incumbent's native
                # amount. Convert only when the actual fresh fill quote arrives.
                stake = _positive(raw["paper_stake_usd"])
                if stake is None or stake > self.max_stake_usd:
                    conn.execute(
                        "UPDATE candidate_entries SET status='rejected',reason='invalid_flat_paper_stake' WHERE signal_id=?",
                        (row["signal_id"],),
                    )
                    continue
                requested = int(stake / snap["native"] * scale) - DEFAULT_TIP_NATIVE[chain]
            amount = min(requested, ceiling)
            if amount <= 0:
                conn.execute("UPDATE candidate_entries SET status='rejected',reason='size_missing' WHERE signal_id=?",
                             (row["signal_id"],))
                continue
            broker = self._broker(conn, chain, snap, side="buy")
            decision_id = "cp_" + _hash([identity["hypothesis_id"], row["signal_id"]])[:32]
            decision = Decision(
                decision_id=decision_id, ts_ms=signal_ms, lane=Lane(spec["lane"]),
                mode=LaneMode.SHADOW, chain=chain, token=raw["token"], action=Action.ENTER,
                size_base_units=amount, signals=[row["signal_id"]], params_version=identity["spec_hash"],
                thesis="prospective frozen candidate " + identity["hypothesis_id"],
            )
            conn.execute("SAVEPOINT candidate_entry_admission")
            conn.execute(
                "INSERT INTO decisions(decision_id,ts_ms,lane,mode,chain,token,action,size_base_units,"
                "signals_json,params_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (decision_id, signal_ms, spec["lane"], "shadow", chain.value, raw["token"], "enter",
                 str(amount), _json([row["signal_id"]]), identity["spec_hash"]),
            )
            conn.execute(
                "INSERT INTO tokens(chain,address,decimals,first_seen_ms) VALUES (?,?,?,?) "
                "ON CONFLICT(chain,address) DO UPDATE SET decimals=excluded.decimals",
                (chain.value, raw["token"], snap["decimals"], signal_ms),
            )
            # Preparing research rows can itself wait on I/O. Re-admit against
            # actual processing time immediately before PaperBroker, without
            # changing the captured market print or inventing a later quote.
            now = self._processing_ms(now)
            current, failure = self._snapshot(captured, now)
            if now - signal_ms > self.entry_max_age_ms or current is None:
                conn.execute("ROLLBACK TO candidate_entry_admission")
                conn.execute("RELEASE candidate_entry_admission")
                if now - signal_ms > self.entry_max_age_ms:
                    conn.execute("UPDATE candidate_entries SET last_admission_stage='preparation_crossed_deadline',"
                                 "last_checked_ms=? WHERE signal_id=?", (now, row["signal_id"]))
                    self._expire_pending_rows(conn, identity, now)
                else:
                    diagnostics = {"reason": failure, "checked_ms": now, "stage": "entry_preparation",
                                   "capture": captured.get("diagnostics")}
                    conn.execute("UPDATE candidate_entries SET reason=?,last_quote_reason=?,last_checked_ms=?,"
                                 "last_quote_diagnostics_json=?,last_admission_stage='preparation_quote_unusable' WHERE signal_id=?",
                                 (failure, failure, now, _json(diagnostics), row["signal_id"]))
                continue
            snap = current
            snap["processing_ms"] = now
            order = broker.buy(decision, price_usd=snap["price"], liquidity_usd=snap["depth"],
                               decimals=snap["decimals"], curve=snap["curve"], now_ms=snap["quote_ms"],
                               latency_ms=0)  # the supplied post-delay snapshot already includes observed movement
            conn.execute("RELEASE candidate_entry_admission")
            self._fill_evidence(conn, order.order_id, snap, chain, "buy")
            position = broker.open_position(chain, raw["token"], decision.lane, LaneMode.SHADOW)
            filled = order.state is OrderState.FILLED and position is not None
            conn.execute(
                "UPDATE candidate_entries SET status=?,reason=?,decision_id=?,position_id=?,quote_ms=?,"
                "latency_ms=?,processing_ms=?,last_admission_stage=? WHERE signal_id=?",
                ("opened" if filled else "failed", order.error, decision_id,
                 position.position_id if filled else None, snap["quote_ms"], snap["quote_ms"] - signal_ms,
                 now, "broker_filled" if filled else "broker_failed", row["signal_id"]),
            )
            if filled:
                state = ProtectionState(position_id=position.position_id, entry_price=position.entry_price_usd)
                conn.execute("INSERT INTO candidate_protection(position_id,state,last_quote_ms,pending,last_price_ms) "
                             "VALUES (?,?,?,NULL,?)",
                             (position.position_id, state.model_dump_json(), snap["quote_ms"], snap["price_ms"]))

    def _exits(self, conn: sqlite3.Connection, identity: Mapping[str, Any], quotes: Mapping[Any, Any], now: int):
        spec = identity["spec"]
        cfg = _protection(spec["exit"])
        rows = conn.execute(
            "SELECT p.position_id,e.signal_id,c.state,c.last_quote_ms,c.pending,"
            "COALESCE(c.last_price_ms,c.last_quote_ms) AS last_price_ms FROM positions p "
            "JOIN candidate_entries e ON e.position_id=p.position_id "
            "JOIN candidate_protection c ON c.position_id=p.position_id WHERE p.closed_ms IS NULL"
        ).fetchall()
        for row in rows:
            now = self._processing_ms(now)
            chain = Chain(spec["chain"])
            # Connection is always explicitly the candidate research book.
            position_row = conn.execute("SELECT token FROM positions WHERE position_id=?", (row["position_id"],)).fetchone()
            captured = self._quote(quotes, chain.value, position_row[0])
            snap, reason = self._snapshot(captured, now)
            if snap is None:
                conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (reason, row["signal_id"]))
                continue
            if snap["quote_ms"] <= row["last_quote_ms"] or snap["price_ms"] <= row["last_price_ms"]:
                continue  # an unchanged print cannot trigger/fill the next rung
            broker = self._broker(conn, chain, snap, side="sell")
            position = load_position(conn, row["position_id"])
            if position is None or position.mode is not LaneMode.SHADOW:
                raise ValueError("candidate inventory is not an owned shadow position")
            stored_decimals = conn.execute("SELECT decimals FROM tokens WHERE chain=? AND address=?",
                                           (chain.value, position.token)).fetchone()
            if not stored_decimals or stored_decimals[0] != snap["decimals"]:
                conn.execute("UPDATE candidate_entries SET reason='token_decimals_changed' WHERE signal_id=?",
                             (row["signal_id"],))
                continue
            bought = conn.execute(
                "SELECT 1 FROM orders o JOIN position_orders po ON po.order_id=o.order_id "
                "WHERE po.position_id=? AND o.side='buy' AND o.state='filled' AND o.mode='shadow'",
                (position.position_id,),
            ).fetchone()
            if not bought:
                conn.execute("UPDATE candidate_entries SET reason='unmatched_inventory' WHERE signal_id=?", (row["signal_id"],))
                continue
            state = ProtectionState.model_validate_json(row["state"])
            pending = json.loads(row["pending"]) if row["pending"] else None
            hold_ms = spec["exit"].get("hold_ms")
            if pending is None and hold_ms is not None and snap["quote_ms"] >= position.opened_ms + int(hold_ms):
                deadline = position.opened_ms + int(hold_ms)
                pending = {"pct": "100", "reason": "time_exit", "trigger_ms": deadline,
                           "due_ms": deadline + self.poll_ms, "state": state.model_dump(mode="json")}
            if pending is None:
                probe = state.model_copy(deep=True)
                action = evaluate(probe, price_usd=snap["price"], liquidity_usd=snap["depth"],
                                  now_ms=snap["quote_ms"], cfg=cfg)
                if action.sells:
                    pending = {"pct": str(action.pct), "reason": action.reason,
                               "trigger_ms": snap["quote_ms"], "due_ms": snap["quote_ms"] + self.poll_ms,
                               "state": probe.model_dump(mode="json")}
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
                                 (state.model_dump_json(), _json(pending), position.position_id))
                    continue
                snap = current
                snap["processing_ms"] = now
                order = broker.sell(
                    position, Decimal(pending["pct"]), price_usd=snap["price"], liquidity_usd=snap["depth"],
                    decimals=snap["decimals"], curve=snap["curve"], now_ms=snap["quote_ms"], latency_ms=0,
                    exit_reason=pending["reason"],
                )
                if order.state is OrderState.FILLED:
                    self._fill_evidence(conn, order.order_id, snap, chain, "sell")
                    state = ProtectionState.model_validate(pending["state"])
                    conn.execute("INSERT OR REPLACE INTO candidate_meta(key,value) VALUES (?,?)",
                                 ("exit_latency:" + order.order_id, str(snap["quote_ms"] - pending["trigger_ms"])))
                    pending = None
                    conn.execute("UPDATE candidate_entries SET reason=NULL WHERE signal_id=?", (row["signal_id"],))
                else:
                    conn.execute("UPDATE candidate_entries SET reason=? WHERE signal_id=?", (order.error, row["signal_id"]))
            conn.execute("UPDATE candidate_protection SET state=?,last_quote_ms=?,last_price_ms=?,pending=? WHERE position_id=?",
                         (state.model_dump_json(), snap["quote_ms"], snap["price_ms"],
                          _json(pending) if pending else None, position.position_id))

    @staticmethod
    def _outcomes(conn: sqlite3.Connection, identity: Mapping[str, Any], now: int) -> list[dict[str, Any]]:
        out = []
        for row in conn.execute(
            "SELECT t.*,e.signal_id,e.payload,e.latency_ms,e.processing_ms,e.source_observed_ms,"
            "e.candidate_received_ms,e.registered_ms,e.last_admission_ms,e.last_admission_stage,"
            "e.preparation_started_ms,e.last_usable_quote_json FROM trades t "
            "JOIN candidate_entries e ON e.position_id=t.position_id ORDER BY t.closed_ms,t.trade_id"
        ).fetchall():
            payload = json.loads(row["payload"])
            cost, proceeds = int(row["cost_native"]), int(row["proceeds_native"])
            legs = conn.execute(
                "SELECT o.order_id,o.side,o.fee_native FROM orders o JOIN position_orders p ON p.order_id=o.order_id "
                "WHERE p.position_id=? AND o.state='filled' ORDER BY o.created_ms", (row["position_id"],),
            ).fetchall()
            bases = [fill_basis(conn, leg["order_id"]) for leg in legs]
            exit_latencies = [conn.execute("SELECT value FROM candidate_meta WHERE key=?",
                                          ("exit_latency:" + leg["order_id"],)).fetchone()
                              for leg in legs if leg["side"] == "sell"]
            evidence = [conn.execute("SELECT value FROM candidate_meta WHERE key=?",
                                    ("fill_evidence:" + leg["order_id"],)).fetchone() for leg in legs]
            fill_evidence = [json.loads(e[0]) if e else {} for e in evidence]
            complete = cost > 0 and bool(legs) and all(bases)
            tax_complete = all(e.get("tax_complete") for e in fill_evidence)
            venue_complete = all(e.get("venue_complete") for e in fill_evidence)
            commission_complete = all(e.get("commission_complete") for e in fill_evidence)
            outcome_id = "paper_" + _hash([identity["campaign_id"], identity["hypothesis_id"],
                                            identity["spec_hash"], row["trade_id"]])[:32]
            out.append({
                "campaign_id": identity["campaign_id"], "hypothesis_id": identity["hypothesis_id"],
                "candidate_id": identity["hypothesis_id"], "spec_hash": identity["spec_hash"],
                "outcome_id": outcome_id, "evidence_id": outcome_id, "signal_id": row["signal_id"],
                "decision_id": payload.get("decision_id", row["signal_id"]), "position_id": row["position_id"],
                "lane": row["lane"], "chain": row["chain"], "token": row["token"],
                "signal_ms": payload["signal_ms"], "opened_ms": row["opened_ms"], "closed_ms": row["closed_ms"],
                "features": payload.get("features", {}),
                "feature_ts_ms": int(payload.get("feature_ts_ms", payload["signal_ms"])),
                "observed_ms": now, "provider": "paper", "source": "PaperBroker", "mode": "shadow",
                "status": "closed", "evidence_complete": complete and tax_complete and venue_complete and commission_complete,
                "cost_complete": complete and tax_complete and venue_complete and commission_complete, "cost_model_assumed": True,
                "venue_complete": venue_complete, "tax_complete": tax_complete, "fee_policy": _fee_policy(),
                "commission_complete": commission_complete,
                "live_cost_verified": False,
                "inventory_complete": True, "entry_debit_native": cost, "exit_credit_native": proceeds,
                "cost_native": cost, "proceeds_native": proceeds, "pnl_native": proceeds - cost,
                "fees_native": int(row["fees_native"]), "fees_included": True,
                "net_return": float(Decimal(proceeds - cost) / cost) if cost else None,
                "basis": bases, "fill_evidence": fill_evidence, "entry_latency_ms": row["latency_ms"],
                "entry_processing_ms": row["processing_ms"],
                "entry_processing_latency_ms": row["processing_ms"] - payload["signal_ms"] if row["processing_ms"] is not None else None,
                "source_observed_ms": row["source_observed_ms"], "candidate_received_ms": row["candidate_received_ms"],
                "registered_ms": row["registered_ms"], "last_admission_ms": row["last_admission_ms"],
                "last_admission_stage": row["last_admission_stage"], "preparation_started_ms": row["preparation_started_ms"],
                "last_usable_quote": json.loads(row["last_usable_quote_json"]) if row["last_usable_quote_json"] else None,
                "exit_latencies_ms": [int(r[0]) if r else None for r in exit_latencies],
                "coverage": "closed_modelled_paper_fills", "evidence_frontier_ms": row["closed_ms"],
                "exit_reason": row["exit_reason"],
                "limitations": ["paper model fees and impact; live cashflow equivalence unproven",
                                "unobserved between-quote price excursions cannot trigger exits"],
            })
        return out

    @staticmethod
    def _attempt_exposure(conn: sqlite3.Connection, identity: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, int]:
        decision_id = row["decision_id"] or "cp_" + _hash([identity["hypothesis_id"], row["signal_id"]])[:32]
        expected_position = "pos_" + _hash({"decision_id": decision_id})[:24]
        orders = conn.execute("SELECT COUNT(*) FROM orders WHERE decision_id=?", (decision_id,)).fetchone()[0]
        positions = conn.execute(
            "SELECT COUNT(*) FROM positions p WHERE p.position_id=? OR p.position_id=? OR EXISTS ("
            "SELECT 1 FROM position_orders po JOIN orders o ON o.order_id=po.order_id "
            "WHERE po.position_id=p.position_id AND o.decision_id=?)",
            (expected_position, row["position_id"], decision_id),
        ).fetchone()[0]
        return {"attempt_orders": orders, "attempt_positions": positions}

    def _expire_pending_rows(self, conn: sqlite3.Connection, identity: Mapping[str, Any], now: int) -> dict[str, int]:
        """Expire unsent attempts independently of the market/source reader."""
        counts = {"expired": 0, "blocked_by_exposure": 0}
        for row in conn.execute("SELECT * FROM candidate_entries WHERE status='pending'").fetchall():
            raw = json.loads(row["payload"])
            signal_ms = raw["signal_ms"]
            if now - signal_ms <= self.entry_max_age_ms:
                continue
            exposure = self._attempt_exposure(conn, identity, row)
            if row["position_id"] is not None or any(exposure.values()):
                if row["reason"] != "entry_expiry_has_ledger_exposure":
                    conn.execute(
                        "UPDATE candidate_entries SET reason='entry_expiry_has_ledger_exposure' WHERE signal_id=?",
                        (row["signal_id"],),
                    )
                counts["blocked_by_exposure"] += 1
                continue
            registered = row["registered_ms"]
            source, received, deadline = row["source_observed_ms"], row["candidate_received_ms"], signal_ms + self.entry_max_age_ms
            stage, admission, preparation = row["last_admission_stage"], row["last_admission_ms"], row["preparation_started_ms"]
            if source is not None and source > deadline:
                cause = "source_read_after_entry_deadline"
            elif source is not None and received is not None and source <= deadline < received:
                cause = "predispatch_processing_crossed_deadline"
            elif received is not None and registered is not None and received <= deadline < registered:
                cause = "book_processing_crossed_deadline"
            elif (stage == "preparation_crossed_deadline" and admission is not None and preparation is not None
                  and admission <= deadline and preparation <= deadline):
                cause = "preparation_crossed_deadline"
            elif stage == "checking_quote" and row["last_checked_ms"] is not None and row["last_checked_ms"] <= deadline:
                cause = "quote_check_processing_crossed_deadline"
            elif (row["last_quote_reason"] is not None and row["last_checked_ms"] is not None
                  and registered is not None and registered <= deadline and row["last_checked_ms"] <= deadline):
                cause = "entry_quote_not_usable_before_deadline"
            else:
                cause = "arrival_or_processing_cause_unknown"
            arrival_age = source - signal_ms if source is not None else None
            diagnostics = {
                "cause": cause, "registered_ms": registered, "first_arrival_age_ms": arrival_age,
                "first_arrival_basis": "bounded_source_read_receipt" if source is not None else "unknown",
                "source_observed_ms": source, "candidate_received_ms": received,
                "source_observation_age_ms": arrival_age,
                "candidate_receipt_age_ms": received - signal_ms if received is not None else None,
                "registration_age_ms": registered - signal_ms if registered is not None else None,
                "last_admission_ms": admission, "last_admission_stage": stage, "preparation_started_ms": preparation,
                "last_usable_quote": json.loads(row["last_usable_quote_json"]) if row["last_usable_quote_json"] else None,
                "signal_ms": signal_ms, "entry_deadline_ms": deadline,
                "expired_ms": now, "last_checked_ms": row["last_checked_ms"],
                "last_quote_reason": row["last_quote_reason"],
                "last_quote_diagnostics": json.loads(row["last_quote_diagnostics_json"]) if row["last_quote_diagnostics_json"] else None,
                "registration_quote_diagnostics": raw.get("quote_diagnostics"),
                "observation_scope": "supplied quote snapshots only; availability between polls unknown",
            }
            conn.execute(
                "UPDATE candidate_entries SET status='rejected',reason='latency_unreachable',expired_ms=?,expiry_diagnostics_json=? "
                "WHERE signal_id=? AND status='pending'",
                (now, _json(diagnostics), row["signal_id"]),
            )
            counts["expired"] += 1
        return counts

    def expire_pending(self, manifest: Mapping[str, Any], *, now_ms: int) -> dict[str, Any]:
        """Research-only preflight; call before source WAL admission or quote reads.

        It creates no empty books and no orders. An expired entry becomes a refusal
        only after its own ledger proves that no order or inventory was created.
        """
        if len(manifest.get("hypotheses", [])) > 128:
            raise ValueError("candidate batch exceeds bounded research capacity")
        result: dict[str, Any] = {"campaign_id": manifest.get("campaign_id"), "now_ms": int(now_ms),
                                  "expired": 0, "blocked_by_exposure": 0, "arms": [], "errors": []}
        for arm in manifest.get("hypotheses", []):
            conn = None
            try:
                observed = self._processing_ms(int(now_ms))
                identity = self._identity(manifest, arm)
                path = self.book_path(identity["campaign_id"], identity["hypothesis_id"])
                if not path.exists():
                    continue
                # Most polls have no expired unsent entry. Inspect using a fresh
                # readonly connection and close it before obtaining any writer.
                # Never cache this admission: another poll may add an attempt.
                probe = self._existing(identity)
                try:
                    observed = self._processing_ms(observed)
                    prior_tick = probe.execute("SELECT value FROM candidate_meta WHERE key='last_tick_ms'").fetchone()
                    if prior_tick and int(prior_tick[0]) > observed:
                        raise ValueError("candidate expiry must not run backward in time")
                    due = probe.execute(
                        "SELECT 1 FROM candidate_entries WHERE status='pending' "
                        "AND json_extract(payload,'$.signal_ms')<? LIMIT 1",
                        (observed - self.entry_max_age_ms,),
                    ).fetchone()
                finally:
                    probe.close()
                if due is None:
                    result["arms"].append({"hypothesis_id": identity["hypothesis_id"],
                                           "expired": 0, "blocked_by_exposure": 0})
                    continue
                conn = self._open(identity)
                conn.execute("BEGIN IMMEDIATE")
                observed = self._processing_ms(observed)
                stored = conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()
                if not stored or stored[0] != _json(identity):
                    raise ValueError("frozen candidate identity or execution policy changed")
                prior_tick = conn.execute("SELECT value FROM candidate_meta WHERE key='last_tick_ms'").fetchone()
                if prior_tick and int(prior_tick[0]) > observed:
                    raise ValueError("candidate expiry must not run backward in time")
                before = conn.total_changes
                counts = self._expire_pending_rows(conn, identity, observed)
                if conn.total_changes > before:
                    conn.execute("INSERT OR REPLACE INTO candidate_meta VALUES ('last_expiry_ms',?)", (str(observed),))
                conn.execute("COMMIT")
                result["arms"].append({"hypothesis_id": identity["hypothesis_id"], **counts})
                for key in counts:
                    result[key] += counts[key]
            except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                if conn is not None and conn.in_transaction:
                    conn.execute("ROLLBACK")
                result["errors"].append({"hypothesis_id": arm.get("hypothesis_id"), "error": str(exc)})
            finally:
                if conn is not None:
                    conn.close()
        return result

    @staticmethod
    def _incomplete_rows(conn: sqlite3.Connection, identity: Mapping[str, Any], now: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT e.*,p.opened_ms,p.qty,p.qty_total,p.cost_native,p.proceeds_native,p.closed_ms "
            "FROM candidate_entries e LEFT JOIN positions p ON p.position_id=e.position_id "
            "WHERE p.closed_ms IS NULL ORDER BY e.signal_id"
        ).fetchall()
        out = []
        for row in rows:
            raw = json.loads(row["payload"])
            created = row["position_id"] is not None
            excluded = row["reason"] == "filter_not_matched"
            scope_excluded = row["reason"] == "outside_prospective_window"
            # A pre-broker refusal has proved zero exposure only after checking its
            # deterministic attempt identity in the actual research ledger. Broker
            # failures still have order attempts and remain economically uncertain.
            exposure = CandidatePaperRunner._attempt_exposure(conn, identity, row)
            proved_refusal = (not created and not excluded and not scope_excluded and row["status"] == "rejected"
                              and not any(exposure.values())
                              and identity["selected_ms"] < raw["signal_ms"] <= now
                              and row["reason"] not in ("future_features", "outside_prospective_window"))
            evidence_id = "attempt_" + _hash([identity["campaign_id"], identity["hypothesis_id"],
                                             identity["spec_hash"], row["signal_id"]])[:32]
            out.append({
                "campaign_id": identity["campaign_id"], "hypothesis_id": identity["hypothesis_id"],
                "candidate_id": identity["hypothesis_id"], "spec_hash": identity["spec_hash"],
                "signal_id": row["signal_id"], "decision_id": raw.get("decision_id", row["signal_id"]),
                "outcome_id": evidence_id, "evidence_id": evidence_id, "mode": "shadow", "provider": "paper",
                "source": "PaperBroker" if created or proved_refusal else "CandidatePaperRunner",
                "lane": identity["spec"]["lane"], "chain": identity["spec"]["chain"], "token": raw["token"],
                "signal_ms": raw["signal_ms"], "opened_ms": row["opened_ms"], "closed_ms": None,
                "features": raw.get("features", {}), "feature_ts_ms": int(raw.get("feature_ts_ms", raw["signal_ms"])),
                "observed_ms": now,
                "status": "open" if created else "excludedbyfilter" if excluded else "excludedbyscope" if scope_excluded
                else "refused" if proved_refusal else row["status"],
                "reason": row["reason"], "no_position_created": not created,
                "entry_debit_native": int(row["cost_native"]) if created else 0 if proved_refusal else None,
                "exit_credit_native": int(row["proceeds_native"]) if created else 0 if proved_refusal else None,
                "inventory_remaining": int(row["qty"]) if created else 0,
                "inventory_complete": proved_refusal, "evidence_complete": proved_refusal,
                "cost_complete": proved_refusal, "known_unfilled": proved_refusal,
                "exposure_check": exposure, "expired_ms": row["expired_ms"], "registered_ms": row["registered_ms"],
                "last_checked_ms": row["last_checked_ms"], "last_quote_reason": row["last_quote_reason"],
                "last_quote_diagnostics": json.loads(row["last_quote_diagnostics_json"]) if row["last_quote_diagnostics_json"] else None,
                "expiry_diagnostics": json.loads(row["expiry_diagnostics_json"]) if row["expiry_diagnostics_json"] else None,
                "cost_model_assumed": True, "live_cost_verified": False, "net_return": None,
                "entry_latency_ms": row["latency_ms"], "evidence_frontier_ms": row["quote_ms"],
                "entry_processing_ms": row["processing_ms"],
                "entry_processing_latency_ms": row["processing_ms"] - raw["signal_ms"] if row["processing_ms"] is not None else None,
                "source_observed_ms": row["source_observed_ms"], "candidate_received_ms": row["candidate_received_ms"],
                "last_admission_ms": row["last_admission_ms"], "last_admission_stage": row["last_admission_stage"],
                "preparation_started_ms": row["preparation_started_ms"],
                "last_usable_quote": json.loads(row["last_usable_quote_json"]) if row["last_usable_quote_json"] else None,
            })
        return out

    def watched_tokens(self, manifest: Mapping[str, Any], *, limit: int = 128,
                       now_ms: int | None = None) -> list[tuple[str, str]]:
        """Pending and open owned research inventory, without creating an empty book."""
        if not 0 < limit <= 128:
            raise ValueError("watched-token limit must be between 1 and 128")
        if now_ms is not None:
            expired = self.expire_pending(manifest, now_ms=now_ms)
            if expired["errors"]:
                raise ValueError("candidate pending expiry failed: " + _json(expired["errors"]))
        by_arm: list[list[tuple[str, str]]] = []
        for arm in manifest.get("hypotheses", [])[:128]:
            identity = self._identity(manifest, arm)
            path = self.book_path(identity["campaign_id"], identity["hypothesis_id"])
            if not path.exists():
                continue
            # Existing owned research paths only; no implicit application connection.
            conn = self._existing(identity)
            try:
                rows = conn.execute(
                    "SELECT e.payload FROM candidate_entries e LEFT JOIN positions p ON p.position_id=e.position_id "
                    "WHERE e.status='pending' OR (p.position_id IS NOT NULL AND p.closed_ms IS NULL) "
                    "ORDER BY CASE WHEN p.position_id IS NOT NULL THEN 0 ELSE 1 END,e.quote_ms,e.signal_id LIMIT ?",
                    (limit,),
                ).fetchall()
                tokens = []
                for row in rows:
                    raw = json.loads(row[0])
                    tokens.append((identity["spec"]["chain"], str(raw["token"])))
                by_arm.append(tokens)
            finally:
                conn.close()
        # Round robin preserves each arm's chance of receiving fresh quotes at a shared read cap.
        found: set[tuple[str, str]] = set()
        for index in range(limit):
            for tokens in by_arm:
                if index < len(tokens):
                    found.add(tokens[index])
                    if len(found) >= limit:
                        return sorted(found)
        return sorted(found)

    def run_candidates(
        self, manifest: Mapping[str, Any], opportunities: Sequence[Mapping[str, Any]],
        quotes: Mapping[Any, Any], *, now_ms: int,
    ) -> dict[str, Any]:
        """Advance existing books, register frozen features, and return tagged closed fills.

        Repeating an opportunity or quote cannot duplicate its buy or consume a TP rung.
        Results are cumulative and have stable outcome IDs for caller-side deduplication.
        """
        if len(opportunities) > 1000 or len(manifest.get("hypotheses", [])) > 128:
            raise ValueError("candidate batch exceeds bounded research capacity")
        result: dict[str, Any] = {"campaign_id": manifest.get("campaign_id"), "now_ms": int(now_ms),
                                  "processing_frontier_ms": int(now_ms),
                                  "mode": "shadow", "arms": [], "outcomes": [], "assessment_rows": [],
                                  "refusals": [], "excluded_by_filter": [], "excluded_by_scope": [], "errors": []}
        for arm in manifest.get("hypotheses", []):
            conn = None
            try:
                identity = self._identity(manifest, arm)
                conn = self._open(identity)
                conn.execute("BEGIN IMMEDIATE")
                observed = self._processing_ms(int(now_ms))
                stored = conn.execute("SELECT value FROM candidate_meta WHERE key='identity'").fetchone()
                if not stored or stored[0] != _json(identity):
                    raise ValueError("frozen candidate identity or execution policy changed")
                prior_tick = conn.execute("SELECT value FROM candidate_meta WHERE key='last_tick_ms'").fetchone()
                if prior_tick and int(prior_tick[0]) > observed:
                    raise ValueError("candidate tick must not run backward in time")
                self._exits(conn, identity, quotes, observed)
                observed = self._processing_ms(observed)
                self._register(conn, identity, opportunities, observed, received_ms=int(now_ms))
                self._entries(conn, identity, quotes, observed)
                observed = self._processing_ms(observed)
                outcomes = self._outcomes(conn, identity, observed)
                incomplete = self._incomplete_rows(conn, identity, observed)
                statuses = dict(conn.execute("SELECT status,COUNT(*) FROM candidate_entries GROUP BY status").fetchall())
                open_count = conn.execute("SELECT COUNT(*) FROM positions WHERE closed_ms IS NULL").fetchone()[0]
                result["arms"].append({"hypothesis_id": identity["hypothesis_id"], "spec_hash": identity["spec_hash"],
                                       "book_path": str(self.book_path(identity["campaign_id"], identity["hypothesis_id"])),
                                       "processing_frontier_ms": observed,
                                       "open": open_count, "closed": len(outcomes), "entries": statuses,
                                       "incomplete": [dict(r) for r in conn.execute(
                                           "SELECT signal_id,status,reason FROM candidate_entries WHERE reason IS NOT NULL")],
                                       "order_count": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]})
                result["outcomes"].extend(outcomes)
                result["assessment_rows"].extend(outcomes + [r for r in incomplete
                                                              if r["status"] not in ("excludedbyfilter", "excludedbyscope")])
                result["refusals"].extend(r for r in incomplete if r["no_position_created"]
                                          and r["status"] not in ("pending", "excludedbyfilter", "excludedbyscope"))
                result["excluded_by_filter"].extend(r for r in incomplete if r["status"] == "excludedbyfilter")
                result["excluded_by_scope"].extend(r for r in incomplete if r["status"] == "excludedbyscope")
                conn.execute("INSERT OR REPLACE INTO candidate_meta VALUES ('last_tick_ms',?)", (str(observed),))
                conn.execute("COMMIT")
                result["processing_frontier_ms"] = max(result["processing_frontier_ms"], observed)
            except (ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                if conn is not None and conn.in_transaction:
                    conn.execute("ROLLBACK")
                result["errors"].append({"hypothesis_id": arm.get("hypothesis_id"), "error": str(exc)})
            finally:
                if conn is not None:
                    conn.close()
        return result
