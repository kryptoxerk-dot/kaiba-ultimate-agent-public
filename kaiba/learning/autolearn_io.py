"""Bounded source reads and short idempotent learning writes; no provider transport.

Codex AUTOLEARN-IMPLEMENT-20261014. Existing core and financial writers are reused,
not modified. Source connections always close before any wait or other work.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.learning.signal_audit import Reader

WAL_LIMIT = 1_500_000_000
DAY_MS = 86_400_000


class SourceDeferred(RuntimeError):
    """The source is busy, truncated, or outside the safe query budget."""


class GuardedReader(Reader):
    def __init__(self, db_path: Path | str, *, query_seconds: float = 3,
                 max_rows: int = 50_000, clock: Callable[[], float] = time.monotonic):
        super().__init__(db_path, pause_s=0)
        if not 0 < query_seconds <= 30:
            raise ValueError("source query deadline must be positive and <=30s")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 50_000:
            raise ValueError("source row cap must be an integer between 1 and 50000")
        self.query_seconds, self.max_rows, self.clock = query_seconds, max_rows, clock
        self.receipts: list[dict[str, Any]] = []

    def guard(self) -> int:
        size = self._wal_bytes()
        if size > WAL_LIMIT:
            self.wal_waits += 1
            raise SourceDeferred(f"wal_over_limit:{size}; retry after at least 60s")
        return size

    def _read(self, sql: str, args: Sequence[Any], *, dictionaries: bool) -> list:
        size = self.guard()
        started = self.clock()
        conn = None
        try:
            conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(self.clock() - started >= self.query_seconds), 1000)
            cur = conn.execute(sql, tuple(args))
            rows = cur.fetchmany(self.max_rows + 1)
            if len(rows) > self.max_rows:
                raise SourceDeferred("source_row_cap; no partial sample accepted")
            if dictionaries:
                names = [x[0] for x in cur.description]
                return [dict(zip(names, row, strict=True)) for row in rows]
            return rows
        except sqlite3.OperationalError as exc:
            raise SourceDeferred("source_query_unavailable:" + type(exc).__name__) from exc
        finally:
            if conn is not None:
                conn.close()
            self.queries += 1
            self.receipts.append({"duration_s": round(self.clock() - started, 4), "wal_bytes": size})

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[tuple]:
        return self._read(sql, args, dictionaries=False)

    def rows(self, sql: str, args: Sequence[Any] = ()) -> list[dict]:
        return self._read(sql, args, dictionaries=True)


def load_json(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {} if default is None else default


def _value(raw: Any) -> Any:
    return raw.get("value") if isinstance(raw, Mapping) else raw


def _positive(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(_value(raw)))
        return value if value.is_finite() and value > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _measure_clock(raw: Any) -> int | None:
    if not isinstance(raw, Mapping):
        return None
    receipt = raw.get("receipt") or {}
    try:
        return int(receipt["observed_at_ms"])
    except (TypeError, ValueError, KeyError):
        return None


def _usable_measure(raw: Any) -> bool:
    if not isinstance(raw, Mapping):
        return False
    receipt = raw.get("receipt") or {}
    allowed = {"verified_onchain", "provider_reported", "derived", "cached"}
    # Legacy packets have no basis. Explicit unavailable/stale/estimated evidence
    # is never upgraded by a retained numeric value or a new dossier build time.
    return (raw.get("basis", "provider_reported") in allowed
            and receipt.get("basis", "provider_reported") in allowed)


def _cached_info(chain: str, token: str, now_ms: int, cache_root: Path | None) -> dict:
    """Read an already captured response; never call or import a provider transport."""
    from kaiba.core.config import get_settings
    from kaiba.core.schemas import digest
    root = Path(cache_root or get_settings().kaiba_data_dir / "cache" / "gmgn")
    key = f"gmgn-cli token info --address {token} --chain {chain} --raw"
    path = root / (digest(key)[:32] + ".json")
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            return {}
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        if len(raw) > 2_000_000:
            return {}
        packet = json.loads(raw)
        data = packet.get("data")
        at = packet.get("fetched_ms")
        if not isinstance(data, dict) or type(at) is not int or not 0 <= now_ms - at <= DAY_MS:
            return {}
        def same(address):
            return isinstance(address, str) and (
                address == token if chain == "sol" else address.lower() == token.lower())
        if not same(data.get("address")):
            return {}
        price = data.get("price")
        if isinstance(price, Mapping) and not same(price.get("address")):
            return {}
        # Reject explicit cross-chain/market contradictions. A pool's address is
        # its LP identity; the tracked token can be either constituent.
        pool = data.get("pool")
        for block in (data, price, pool):
            if isinstance(block, Mapping) and "chain" in block and block["chain"] != chain:
                return {}
        if (isinstance(pool, Mapping) and isinstance(pool.get("base_address"), str)
                and isinstance(pool.get("quote_address"), str)
                and not any(same(pool[k]) for k in ("base_address", "quote_address"))):
            return {}
        return {"data": data, "fetched_ms": at, "sha256": hashlib.sha256(raw).hexdigest(),
                "source": "captured_gmgn_token_info"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def quote_evidence(reader: GuardedReader, chain: str, token: str, *, now_ms: int,
                   max_age_ms: int = 120_000, cache_root: Path | None = None) -> dict:
    """Use captured fresh curve/depth and native prices; unknown data stays unavailable."""
    diagnostics: dict[str, Any] = {"chain": chain, "token": token, "checked_ms": now_ms}
    def unavailable(reason):
        return {"chain": chain, "token": token, "unavailable_reason": reason, "diagnostics": diagnostics}
    cached = _cached_info(chain, token, now_ms, cache_root)
    tokens = reader.rows("SELECT decimals,migrated_ms FROM tokens WHERE chain=? AND address=?", (chain, token))
    raw_dec = tokens[0]["decimals"] if tokens else None
    metadata_ms = 0
    if raw_dec is None and cached:
        raw_dec = cached["data"].get("decimals")
        metadata_ms = cached["fetched_ms"]
        diagnostics["metadata"] = {k: v for k, v in cached.items() if k != "data"}
    try:
        n = Decimal(str(raw_dec))
        dec = int(n) if n.is_finite() and n == n.to_integral_value() and not isinstance(raw_dec, bool) else -1
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        dec = -1
    if not 0 <= dec <= 30:
        return unavailable("token_decimals_missing_or_invalid")
    diagnostics["decimals"] = dec
    from kaiba.core.schemas import Chain
    from kaiba.providers.native_price import price_source_chain
    native_chain = price_source_chain(Chain(chain)).value
    def common_at(packet_ms: int) -> dict | None:
        packet_ms = max(packet_ms, metadata_ms)  # precision captured later cannot backdate a quote
        native = reader.rows("SELECT price_usd,ts_ms,source FROM native_prices WHERE chain=? AND ts_ms<=? "
                             "ORDER BY ts_ms DESC LIMIT 1", (native_chain, packet_ms))
        if not native:
            return None
        usd = _positive(native[0]["price_usd"])
        if usd is None or not 0 <= now_ms - int(native[0]["ts_ms"]) <= 300_000:
            return None
        return {"chain": chain, "token": token, "decimals": dec, "native_usd": str(usd),
                "native_price_ts_ms": native[0]["ts_ms"], "native_price_source": native[0]["source"],
                "native_source_chain": native_chain, "cost_model_assumed": True, "live_cost_verified": False}
    if chain == "sol" and tokens and not tokens[0]["migrated_ms"]:
        curves = reader.rows("SELECT * FROM curve_snapshots WHERE chain=? AND token=? AND observed_ms<=? "
                             "ORDER BY observed_ms DESC LIMIT 1", (chain, token, now_ms))
        if (curves and metadata_ms <= int(curves[0]["observed_ms"])
                and 0 <= now_ms - int(curves[0]["observed_ms"]) <= max_age_ms):
            from kaiba.execution.curve_price import CurveState
            c = curves[0]
            state, _ = CurveState.build(virtual_sol=int(c["virtual_sol_lamports"]),
                                       virtual_token=int(c["virtual_token_atoms"]), real_sol=int(c["real_sol_lamports"]),
                                       real_token=int(c["real_token_atoms"]), observed_ms=int(c["observed_ms"]),
                                       source=str(c["source"]))
            if state is not None:
                common = common_at(state.observed_ms)
                if common is None:
                    return unavailable("native_price_missing_or_stale")
                return {**common, "quote_ts_ms": max(state.observed_ms, metadata_ms), "diagnostics": diagnostics,
                        "price_usd": str(state.spot_price_usd(Decimal(common["native_usd"]), dec)), "liquidity_usd": None,
                        "curve": {k: getattr(state, k) for k in ("virtual_sol", "virtual_token", "real_sol",
                                   "real_token", "observed_ms", "source", "complete")}, "basis": "captured_curve",
                        "tax_known_zero": True}
    dossiers = reader.rows("SELECT built_at_ms,dossier_json FROM token_dossiers WHERE chain=? AND address=?",
                           (chain, token))
    d = load_json(dossiers[0]["dossier_json"]) if dossiers else {}
    d = d if isinstance(d, Mapping) else {}
    p, depth = _positive(d.get("price_usd")), _positive(d.get("liquidity_usd"))
    pt, dt = _measure_clock(d.get("price_usd")), _measure_clock(d.get("liquidity_usd"))
    valid = (p is not None and depth is not None and pt is not None and dt is not None
             and all(_usable_measure(d.get(k)) for k in ("price_usd", "liquidity_usd"))
             and all(0 <= now_ms - t <= max_age_ms for t in (pt, dt)))
    diagnostics.update({"dossier_price_ms": pt, "dossier_depth_ms": dt,
                        "cached_info_ms": cached.get("fetched_ms")})
    basis = "captured_pool_depth"
    # A coherent packet is preferable to combining independently refreshed fields.
    # Capture time is retained exactly; reading a cache never refreshes its price.
    if cached and 0 <= now_ms - cached["fetched_ms"] <= max_age_ms:
        cp = cached["data"].get("price")
        cp = _positive(cp.get("price")) if isinstance(cp, Mapping) else None
        cd = _positive(cached["data"].get("liquidity"))
        if cp is not None and cd is not None and (not valid or cached["fetched_ms"] > max(pt, dt)):
            p, depth, pt, dt = cp, cd, cached["fetched_ms"], cached["fetched_ms"]
            basis, valid = "captured_gmgn_pool_depth", True
            diagnostics["cached_info"] = {k: v for k, v in cached.items() if k != "data"}
    if not valid:
        return unavailable("pool_price_or_depth_missing_or_stale")
    packet_ms = max(pt, dt, metadata_ms)
    common = common_at(packet_ms)
    if common is None:
        return unavailable("native_price_missing_or_stale")
    def tax(key):
        raw = d.get(key)
        if isinstance(raw, Mapping) and not _usable_measure(raw):
            return None
        stamp = _measure_clock(raw)
        if isinstance(raw, Mapping) and raw.get("receipt") is not None:
            if stamp is None or not 0 <= packet_ms - stamp <= max_age_ms:
                return None
        v = _value(raw)
        if v is None or isinstance(v, bool):
            return None
        try:
            n = int(v)
            return n if 0 <= n < 10_000 else None
        except (TypeError, ValueError):
            return None
    return {**common, "quote_ts_ms": packet_ms, "price_ts_ms": pt, "depth_ts_ms": dt,
            "price_usd": str(p), "liquidity_usd": str(depth),
            "basis": basis, "buy_tax_bps": tax("buy_tax_bps"),
            "sell_tax_bps": tax("sell_tax_bps"), "diagnostics": diagnostics}


def quote_snapshot(reader: GuardedReader, chain: str, token: str, *, now_ms: int,
                   max_age_ms: int = 120_000, cache_root: Path | None = None) -> dict | None:
    """Compatibility API: unavailable evidence remains None, never a synthetic price."""
    packet = quote_evidence(reader, chain, token, now_ms=now_ms, max_age_ms=max_age_ms, cache_root=cache_root)
    return None if packet.get("unavailable_reason") else packet


def read_opportunities(reader: GuardedReader, *, after_rowid: int, selected_ms: int,
                       now_ms: int, lanes: Sequence[str], chains: Sequence[str], limit: int = 40) -> dict:
    """Freeze entry-time signal payloads. No mutable vendor roster/grade becomes a feature."""
    marks_l, marks_c = ",".join("?" * len(lanes)), ",".join("?" * len(chains))
    rows = reader.rows(f"SELECT rowid AS source_rowid,decision_id,ts_ms,lane,chain,token,action,"
                       f"size_base_units,signals_json FROM decisions WHERE rowid>? AND ts_ms>? AND ts_ms<=? "
                       f"AND lane IN ({marks_l}) AND chain IN ({marks_c}) ORDER BY rowid LIMIT ?",
                       (after_rowid, selected_ms, now_ms, *lanes, *chains, min(100, int(limit))))
    opportunities, skipped = [], []
    for row in rows:
        if row["action"] not in ("enter", "scale_in") or int(row["size_base_units"] or 0) <= 0:
            skipped.append({"decision_id": row["decision_id"], "reason": "incumbent_skipped_or_unsized"})
            continue
        features: dict[str, Any] = {}
        for sid in load_json(row["signals_json"], [])[:5]:
            sig = reader.rows("SELECT created_ms,chain,token,lane,payload_json FROM signals WHERE signal_id=?", (sid,))
            if (sig and int(sig[0]["created_ms"]) <= int(row["ts_ms"])
                    and all(sig[0][k] == row[k] for k in ("chain", "token", "lane"))):
                payload = load_json(sig[0]["payload_json"])
                for key in ("holder_count", "market_cap_usd", "liquidity_usd", "top10_pct", "bundler_pct",
                            "token_age_s", "migrated", "launchpad", "smart_wallets_still_holding",
                            "smart_wallets_sold", "smart_sold_frac_max"):
                    if key in payload:
                        raw = payload[key]
                        stamp = _measure_clock(raw)
                        if (isinstance(raw, Mapping) and raw.get("receipt") is not None
                                and (stamp is None or stamp > int(row["ts_ms"]))):
                            continue
                        if isinstance(raw, Mapping) and not _usable_measure(raw):
                            continue
                        features[key] = _value(raw)
        opportunities.append({**row, "signal_id": row["decision_id"], "signal_ms": int(row["ts_ms"]),
                              "features": features, "feature_basis": "stored_signal_at_decision",
                              "feature_ts_ms": int(row["ts_ms"]),
                              "mode": "shadow"})
    return {"opportunities": opportunities, "skipped": skipped, "source_rows": len(rows),
            "next_rowid": max((r["source_rowid"] for r in rows), default=after_rowid)}


def closed_packets(reader: GuardedReader, *, after_ms: int, after_rowid: int = 0,
                   now_ms: int, limit: int = 30) -> dict:
    from kaiba.learning.fabricated import is_fabricated_outcome
    positions = reader.rows("SELECT rowid AS source_rowid,* FROM positions WHERE closed_ms IS NOT NULL "
                            "AND (closed_ms>? OR (closed_ms=? AND rowid>?)) AND closed_ms<=? "
                            "ORDER BY closed_ms,rowid LIMIT ?", (after_ms, after_ms, after_rowid, now_ms, limit))
    packets, fabricated = [], 0
    for p in positions:
        if is_fabricated_outcome(p.get("exit_reason"), p.get("mode")):
            fabricated += 1
            continue
        orders = reader.rows("SELECT o.* FROM position_orders po JOIN orders o ON o.order_id=po.order_id "
                             "WHERE po.position_id=? ORDER BY po.ts_ms", (p["position_id"],))
        costs = []
        for order in orders[:30]:
            costs.extend(reader.rows("SELECT * FROM fill_costs WHERE order_id=?", (order["order_id"],)))
        # Applied-order links alone do not prove attempted sends, recoveries or full inventory.
        packets.append({"position": p, "orders": orders, "fill_costs": costs,
                        "inventory": {"qty_remaining": p.get("qty")}, "coverage": {},
                        "source": "canonical_closed_positions", "projection_frontier_ms": now_ms})
    last = positions[-1] if positions else None
    return {"packets": packets, "fabricated_excluded": fabricated,
            "next_closed_ms": int(last["closed_ms"]) if last else after_ms,
            "next_rowid": int(last["source_rowid"]) if last else after_rowid}


def persist_lessons(db_path: Path | str, lessons: Sequence[Mapping[str, Any]]) -> dict:
    """Append one research lesson/credit per evidence ID, atomically, via existing APIs."""
    reader = GuardedReader(db_path)
    reader.guard()
    from kaiba.core import journal
    from kaiba.learning import playbook
    conn = sqlite3.connect(str(db_path), timeout=.5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    added, repeated = [], 0
    try:
        for lesson in lessons[:40]:
            reader.guard()
            eid = str(lesson["evidence_id"])
            key = "autolearn:evidence:" + hashlib.sha256(eid.encode()).hexdigest()
            existing = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if existing and load_json(existing[0]).get("journal_seq"):
                repeated += 1
                continue
            text = str(lesson["text"])[:280]
            rid = playbook.rule_id_for(text, lesson.get("lane"))
            if not conn.execute("SELECT 1 FROM playbook WHERE rule_id=?", (rid,)).fetchone():
                playbook.add_rule(text, lane=lesson.get("lane"), evidence=[eid], status="proposed", conn=conn)
            # journal.append owns its transaction. Credit+dedupe is committed first;
            # a missing journal acknowledgment resumes without crediting twice.
            if existing is None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if lesson.get("outcome") in ("hit", "miss"):
                        fn = playbook.record_hit if lesson["outcome"] == "hit" else playbook.record_miss
                        fn(rid, ref=eid, conn=conn)
                    conn.execute("INSERT INTO kv(key,value,updated_ms) VALUES (?,?,?)",
                                 (key, json.dumps({"rule_id": rid, "journal_seq": None}), int(time.time() * 1000)))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            subject = "autolearn:" + eid
            prior = conn.execute("SELECT seq FROM journal WHERE subject=? ORDER BY seq DESC LIMIT 1", (subject,)).fetchone()
            body = str(lesson["body"])
            if len(body) > 3000:
                # Never cut JSON in half. Large callers should persist a bounded
                # immutable receipt and journal its path/hash instead.
                body = json.dumps({"type": "autolearn_oversized_body", "evidence_id": eid[:160],
                    "original_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                    "original_body_chars": len(body), "details_omitted": True, "body_prefix": body[:240]},
                    ensure_ascii=False)
            seq = prior[0] if prior else journal.append(
                "lesson", body, subject=subject, refs=[eid, rid], conn=conn)["seq"]
            conn.execute("UPDATE kv SET value=?,updated_ms=? WHERE key=?",
                         (json.dumps({"rule_id": rid, "journal_seq": seq}), int(time.time() * 1000), key))
            added.append({"evidence_id": eid, "rule_id": rid, "journal_seq": seq})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"added": added, "repeated": repeated}
