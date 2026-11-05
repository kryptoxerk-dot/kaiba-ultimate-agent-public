"""Paper stage of the DLMM fee-scalp bot. Reads only: it cannot sign, send or hold a key.

Owner 2026-11-04: "LP bot is doable too develop one". Live LP needs a signing wallet that nothing on
the box has today (GMGN executes swaps, not liquidity), so this stage answers the question that
decides whether that wallet is worth creating: do hot pools, picked by a rule at the moment, pay a
small ticket after every cost? `kaiba.execution.lp_model` holds the arithmetic and why it is exact.

**It stands apart from trading on purpose.** One process, its own small database under `data/labs/`,
no limiter row and no read of the live database, so it cannot slow an exit. Its RPC host may never
be one protection uses, the same invariant `kaiba.ingest.feed_endpoints` holds for the feeds, and
the run refuses to start otherwise.

Each observation is one pool from the moment it was picked: every arm shares its reads, so testing
another width costs no extra call. An arm lands and closes only `LAND_S` after it decides, on a later
read, because a real transaction does not land on the state that prompted it.

Three profiles run as three services, each with its own database, so the styles can be compared:
`fast` scalps for seconds (the audited challenge), `slow` holds wide two-sided ranges for hours (the
style in the owner's 2026-11-04 Grok LP screenshots), and `dump` waits for a runner to fall 60-75% with
its selling faded, then holds a -50%/+100% range (the owner's 2026-11-04 @zhaidf_ post).

    python -m kaiba.execution.lp_paper run [--profile fast|slow|dump] [--seconds N]
    python -m kaiba.execution.lp_paper report [--profile fast|slow|dump]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sqlite3
import statistics as st
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kaiba.execution.lp_model import (
    BIN_ARRAY_RENT_LAMPORTS,
    LAMPORTS,
    TX_FEE_LAMPORTS,
    Arm,
    Plan,
    PoolState,
    SelectorConfig,
    arrays_for,
    dump_setup,
    eligible,
    exit_reason,
    half_width,
    land,
    plan_bins,
    recent_dump,
    reopen,
    settle,
    wants_reopen,
)
from kaiba.learning.dlmm_state import DecodeError, bin_array_address, decode_bins, decode_pool

log = logging.getLogger("kaiba.lp_paper")

API = "https://dlmm.datapi.meteora.ag"
#: Hot pools first, by fees over liquidity in the last 30 minutes. `tvl>5000` is the server's filter.
POOLS_PATH = "/pools?filter_by=tvl%3E5000&sort_by=fee_tvl_ratio_30m:desc&page_size=50"
#: NOT publicnode: that is protection's host on the box (MEASURED 2026-11-04), and this file's guard
#: refuses it. getMultipleAccounts answered here in about 10 ms from the box.
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
VERSION = "lp-paper/1"
TICKET = LAMPORTS
#: A transaction decided now lands on a later read, never on the state that prompted it.
LAND_S = 3.0
#: Bins read below the price so leftover token can be sold by walking the pool.
WALK_BINS = 30
MAX_ARRAYS = 6
RETAIN_DAYS = 90
AGE_BUCKETS: tuple[tuple[str, float], ...] = (("under_1h", 1.0), ("1_to_24h", 24.0), ("over_1d", float("inf")))
SELECTOR = SelectorConfig()
FAST_ARMS: tuple[Arm, ...] = (
    Arm("s20_60", 20, 60, 6.0, 2.0, 20),
    Arm("s40_120", 40, 120, 6.0, 2.0, 30),
    Arm("s69_180", 69, 180, 8.0, 3.0, 45),
    Arm("r20_60", 20, 60, 6.0, 2.0, 20, reopen_width=10, reopen_hold_s=120),
    Arm("r40_120", 40, 120, 6.0, 2.0, 30, reopen_width=15, reopen_hold_s=180),
    # Exploratory, added after the audit: ranges of 5 bins or fewer were the wallets' cleanest cut.
    Arm("s5_30", 5, 30, 4.0, 1.0, 15),
    Arm("r5_60", 5, 60, 4.0, 1.0, 20, reopen_width=5, reopen_hold_s=90),
)
#: Runners by 24 h volume, not by current fee rate: a token that has already fallen earns little now.
DUMP_POOLS_PATH = "/pools?filter_by=tvl%3E5000&sort_by=volume_24h:desc&page_size=100"
#: Below this a -50%/+100% range takes more bins than one read can cover.
DUMP_MIN_BIN_STEP = 50
DUMP_CHECKS_PER_SELECT = 10
DUMP_RECHECK_S = 1_800.0
DUMP_HISTORY_S = 3 * 86_400
DUMP_SELECTOR = SelectorConfig(min_volume_30m_usd=1_000.0, min_fee_tvl_30m_pct=0.0, max_bin_step=250)
DUMP_ARMS: tuple[Arm, ...] = (
    Arm("d2x_24h", 0, 86_400, 40.0, 15.0, 3_600, range_x=2.0),
    Arm("d2x_12h", 0, 43_200, 40.0, 15.0, 3_600, range_x=2.0),
    Arm("d2x_24h_s25", 0, 86_400, 25.0, 15.0, 3_600, range_x=2.0),
)
#: SOL only below the price in pool types that held up on real positions. f40_6h is the shape of the slow
#: track's b40_6h, so the two compare the filters on one shape. claude-lp-filtered-prereg-20261104.md.
HOUR_CHECKS_PER_SELECT = 10
#: Not the fast track's hot bars: the scan behind this track had no such filter, and those bars left no pool at all.
FILTERED_SELECTOR = SelectorConfig(min_volume_30m_usd=1_000.0, min_fee_tvl_30m_pct=0.10, max_bin_step=250, max_base_fee_pct=5.0,
                                   skip_bin_steps=(81, 125), min_age_h=1.0)
FILTERED_ARMS: tuple[Arm, ...] = (
    Arm("f40_6h", 40, 21_600, 15.0, 6.0, 1_800),
    Arm("f69_6h", 69, 21_600, 15.0, 6.0, 1_800),
    Arm("f69_24h", 69, 86_400, 25.0, 10.0, 3_600),
    Arm("f20_1h", 20, 3_600, 8.0, 3.0, 600),
)
SLOW_ARMS: tuple[Arm, ...] = (
    Arm("w30_6h", 30, 21_600, 15.0, 6.0, 1_800, two_sided=30),
    Arm("w30_12h", 30, 43_200, 20.0, 10.0, 1_800, two_sided=30),
    Arm("n15_6h", 15, 21_600, 15.0, 6.0, 1_800, two_sided=15),
    Arm("b40_6h", 40, 21_600, 15.0, 6.0, 1_800),
)


@dataclass(frozen=True)
class Profile:
    """A style of running the bot. `primary` is the one arm its pre-registered pass line judges; the
    rest are exploratory. docs/research/claude-lp-scalp-prereg-20261104.md."""

    name: str
    arms: tuple[Arm, ...]
    primary: str
    max_open: int
    poll_s: float
    select_s: float
    cooldown_s: float
    pass_min_n: int
    pass_min_days: int
    pass_min_mean_pct: float
    pools_path: str = POOLS_PATH
    selector: SelectorConfig = SELECTOR
    dump: bool = False
    hour_check: bool = False

    @property
    def stale_s(self) -> float:
        return max(30.0, 3 * self.poll_s)

    def root(self, base: Path) -> Path:
        return base if self.name == "fast" else base.with_name(f"{base.name}_{self.name}")


FAST = Profile("fast", FAST_ARMS, "r20_60", 3, 5.0, 30.0, 60.0, 300, 5, 0.30)
SLOW = Profile("slow", SLOW_ARMS, "w30_6h", 6, 60.0, 300.0, 3_600.0, 100, 7, 0.50)
DUMP = Profile("dump", DUMP_ARMS, "d2x_24h", 6, 60.0, 300.0, 21_600.0, 60, 10, 1.00,
               pools_path=DUMP_POOLS_PATH, selector=DUMP_SELECTOR, dump=True)
FILTERED = Profile("filtered", FILTERED_ARMS, "f40_6h", 6, 60.0, 300.0, 3_600.0, 100, 7, 0.50, selector=FILTERED_SELECTOR, hour_check=True)
PROFILES = {p.name: p for p in (FAST, SLOW, DUMP, FILTERED)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, pool TEXT NOT NULL, name TEXT, planned_ms INTEGER NOT NULL, closed_ms INTEGER,
  status TEXT NOT NULL, features_json TEXT NOT NULL, version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(planned_ms);
CREATE TABLE IF NOT EXISTS arm_results (
  obs_id INTEGER NOT NULL, arm TEXT NOT NULL, status TEXT NOT NULL, exit_reason TEXT, at_ms INTEGER NOT NULL,
  hold_s REAL, net_pct REAL, net_sol REAL, fee_pct REAL, inventory_pct REAL, exit_cost_pct REAL,
  tx_sol REAL, rent_sol REAL, unfilled REAL, in_range_share REAL, reopened INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (obs_id, arm));
CREATE INDEX IF NOT EXISTS idx_arm_time ON arm_results(arm, at_ms);
"""


def data_root() -> Path:
    return Path(os.environ.get("KAIBA_LP_PAPER_DIR") or "data/labs/lp_paper")


def rpc_url(hosts: Iterable[str] | None = None) -> str:
    """The RPC this process may use. Refuses a host protection reads its stops from."""
    url = os.environ.get("KAIBA_LP_RPC_URL") or DEFAULT_RPC
    if hosts is None:
        from kaiba.core.schemas import Chain
        from kaiba.ingest.feed_endpoints import protection_hosts

        hosts = protection_hosts(Chain.SOL)
    host = (urlsplit(url).hostname or "").lower()
    if not host or host in {h.lower() for h in hosts}:
        raise SystemExit("lp_rpc_host_shared_with_protection")
    return url


class HttpTransport:
    """Two read-only calls and nothing else. There is no send path in this module."""

    def __init__(self, url: str):
        import httpx

        self.url = url
        self.client = httpx.Client(timeout=10.0, headers={"User-Agent": "kaiba-lp-paper"})

    def rpc(self, method: str, params: list[Any]) -> Any:
        r = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(str(body["error"])[:200])
        return body["result"]

    def api(self, path: str) -> Any:
        r = self.client.get(API + path)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


@dataclass
class Run:
    plan: Plan
    status: str = "planned"
    reason: str | None = None
    decided_ms: int = 0


@dataclass
class Observation:
    id: int
    pool: str
    planned_ms: int
    fee_rate: float
    active: int
    runs: list[Run] = field(default_factory=list)
    last_state_ms: int = 0


class Bot:
    def __init__(self, transport: Any, root: Path, *, now: Callable[[], float] = time.time,
                 profile: Profile = FAST):
        self.t = transport
        self.now = now
        self.profile = profile
        self.arms = profile.arms
        self.selector = profile.selector
        self.setup_rechecks: dict[str, float] = {}
        self.setup_checks = 0
        self.setup_measures: dict[str, dict[str, float]] = {}
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(root / "lp_paper.db")
        self.db.executescript(SCHEMA)
        self.db.execute("UPDATE observations SET status='abandoned' WHERE status='open'")
        self.db.commit()
        self.open: list[Observation] = []
        self.cooldown: dict[str, float] = {}
        self.keys: dict[tuple[str, int], str] = {}
        self.mints: dict[str, str | None] = {}
        self.counts: Counter[str] = Counter()
        self.last_reasons: dict[str, int] = {}
        self.last_select = 0.0
        self.last_status = 0.0
        self.last_prune = 0.0

    def now_ms(self) -> int:
        return int(self.now() * 1000)

    # ------------------------------------------------------------------------------- reads
    def _key(self, pool: str, index: int) -> str:
        return self.keys.setdefault((pool, index), bin_array_address(pool, index))

    def read_state(self, pool: str, lo: int, hi: int) -> PoolState:
        """One slot's pool and bins. An uninitialised array is recorded, never read as empty."""
        indices = arrays_for(lo, hi)[:MAX_ARRAYS]
        keys = [pool] + [self._key(pool, i) for i in indices]
        res = self.t.rpc("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "confirmed"}])
        values = res["value"]
        head = decode_pool(values[0])
        bins: dict[int, dict[str, Any]] = {}
        missing: set[int] = set()
        for index, account in zip(indices, values[1:], strict=True):
            if account is None:
                missing.add(index)
                continue
            arr = decode_bins(account)
            if arr["pool"] != pool or arr["index"] != index:
                raise DecodeError("bin_array_identity_mismatch")
            bins.update({b["id"]: b for b in arr["bins"]})
        return PoolState(slot=int(res["context"]["slot"]), at_ms=self.now_ms(), active=head["active_bin"],
                         bin_step=head["bin_step"], bins=bins, missing_arrays=frozenset(missing))

    def mint_problem(self, mint: str) -> str | None:
        """Why this token cannot be held, cached per run. A failed read refuses, never admits."""
        if mint not in self.mints:
            res = self.t.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            info = (((res or {}).get("value") or {}).get("data") or {})
            parsed = (info.get("parsed") or {}).get("info") if isinstance(info, dict) else None
            why = "mint_unreadable" if not parsed else "freeze_authority" if parsed.get("freezeAuthority") else None
            for ext in (parsed or {}).get("extensions") or []:
                name = ext.get("extension")
                if name == "transferHook" and (ext.get("state") or {}).get("programId"):
                    why = "transfer_hook"
                if name == "transferFeeConfig":
                    state = ext.get("state") or {}
                    fee = max(int((state.get(k) or {}).get("transferFeeBasisPoints") or 0)
                              for k in ("olderTransferFee", "newerTransferFee"))
                    why = "transfer_fee" if fee > 0 else why
            self.mints[mint] = why
        return self.mints[mint]

    # --------------------------------------------------------------------------- selection
    def features(self, pool: dict[str, Any]) -> dict[str, Any]:
        conf = pool.get("pool_config") or {}
        created = int(pool.get("created_at") or 0)
        vol, ratio = pool.get("volume") or {}, pool.get("fee_tvl_ratio") or {}
        x = pool.get("token_x") or {}
        return {"tvl": pool.get("tvl"), "fee_tvl_30m": ratio.get("30m"), "fee_tvl_1h": ratio.get("1h"),
                "vol_30m": vol.get("30m"), "vol_1h": vol.get("1h"), "bin_step": conf.get("bin_step"),
                "base_fee": conf.get("base_fee_pct"), "dyn_fee": pool.get("dynamic_fee_pct"),
                "age_h": round((self.now_ms() - created) / 3.6e6, 2) if created else None,
                "launchpad": pool.get("launchpad"), "market_cap": x.get("market_cap"), "holders": x.get("holders"),
                **self.setup_measures.get(pool.get("address"), {})}

    def dump_problem(self, pool: dict[str, Any]) -> str | None:
        """Why this pool is not the post-dump setup. Rationed, and a failed pool waits before a recheck."""
        address = pool["address"]
        if int((pool.get("pool_config") or {}).get("bin_step") or 0) < DUMP_MIN_BIN_STEP:
            return "bin_step_too_fine"
        if self.setup_rechecks.get(address, 0) > self.now():
            return "setup_recheck_later"
        if self.setup_checks >= DUMP_CHECKS_PER_SELECT:
            return "setup_budget"
        self.setup_checks += 1
        now = int(self.now())
        body = self.t.api(f"/pools/{address}/ohlcv?timeframe=1h&start_time={now - DUMP_HISTORY_S}&end_time={now}")
        why, measures = dump_setup((body or {}).get("data") or [])
        self.setup_measures[address] = measures
        if why:
            self.setup_rechecks[address] = self.now() + DUMP_RECHECK_S
        return why

    def hour_problem(self, pool: dict[str, Any]) -> str | None:
        """Why this pool just fell too far to enter. Rationed, because each check is a read."""
        if self.setup_checks >= HOUR_CHECKS_PER_SELECT:
            return "setup_budget"
        self.setup_checks += 1
        now = int(self.now())
        address = pool["address"]
        body = self.t.api(f"/pools/{address}/ohlcv?timeframe=1h&start_time={now - 3 * 3600}&end_time={now}")
        why, measures = recent_dump((body or {}).get("data") or [])
        self.setup_measures[address] = measures
        return why

    def select(self) -> None:
        """Start observations on the hottest pools the rule admits, using only what shows now."""
        self.last_select = self.now()
        self.setup_checks = 0
        body = self.t.api(self.profile.pools_path)
        pools = (body or {}).get("data") or []
        reasons: Counter[str] = Counter()
        reasons["listed"] = len(pools)
        busy = {o.pool for o in self.open}
        for pool in pools:
            if len(self.open) >= self.profile.max_open:
                break
            address = pool.get("address")
            why = eligible(pool, self.selector, SOL_MINT, self.now_ms())
            if not why and (address in busy or self.cooldown.get(address, 0) > self.now()):
                why = "busy"
            if not why and self.profile.dump:
                why = self.dump_problem(pool)
            if not why and self.profile.hour_check:
                why = self.hour_problem(pool)
            if not why:
                why = self.mint_problem((pool.get("token_x") or {}).get("address") or "")
            if why:
                reasons[why] += 1
                continue
            reasons["picked"] += 1
            self.start(pool)
        self.last_reasons = dict(reasons)

    def start(self, pool: dict[str, Any]) -> None:
        address = pool["address"]
        head = self.t.rpc("getMultipleAccounts", [[address], {"encoding": "base64", "commitment": "confirmed"}])
        info = decode_pool(head["value"][0])
        if info["status"] != 0 or info["mints"][1] != SOL_MINT:
            self.counts["pool_refused"] += 1
            return
        widest = max(max(a.width, half_width(a, info["bin_step"])) for a in self.arms)
        state = self.read_state(address, info["active_bin"] - widest - 1, info["active_bin"] + widest + 2)
        conf = pool.get("pool_config") or {}
        fee_rate = (float(conf.get("base_fee_pct") or 0) + float(pool.get("dynamic_fee_pct") or 0)) / 100
        cur = self.db.execute(
            "INSERT INTO observations(pool, name, planned_ms, status, features_json, version) VALUES (?,?,?,?,?,?)",
            (address, pool.get("name"), state.at_ms, "open", json.dumps(self.features(pool)), VERSION))
        self.db.commit()
        obs = Observation(id=int(cur.lastrowid), pool=address, planned_ms=state.at_ms, fee_rate=fee_rate,
                          active=state.active, last_state_ms=state.at_ms)
        obs.runs = [Run(plan_bins(arm, state, TICKET)) for arm in self.arms]
        self.open.append(obs)
        self.counts["observations"] += 1

    # ------------------------------------------------------------------------------- ticks
    def _bounds(self, obs: Observation) -> tuple[int, int]:
        lo = min([r.plan.lo for r in obs.runs] + [obs.active - WALK_BINS])
        hi = max([r.plan.hi for r in obs.runs] + [obs.active + 2])
        return lo, hi

    def record(self, obs: Observation, run: Run, status: str, at_ms: int, **f: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO arm_results(obs_id, arm, status, exit_reason, at_ms, hold_s, net_pct, net_sol,"
            " fee_pct, inventory_pct, exit_cost_pct, tx_sol, rent_sol, unfilled, in_range_share, reopened)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (obs.id, run.plan.arm.name, status, run.reason, at_ms, f.get("hold_s"), f.get("net_pct"),
             f.get("net_sol"), f.get("fee_pct"), f.get("inventory_pct"), f.get("exit_cost_pct"), f.get("tx_sol"),
             f.get("rent_sol"), f.get("unfilled"), f.get("in_range_share"), int(run.plan.reopened)))
        self.db.commit()
        self.counts[f"arm_{status}"] += 1

    def step(self, obs: Observation, run: Run, state: PoolState) -> bool:
        """Advance one arm on one read. True while it still needs reads."""
        plan = run.plan
        if run.status == "planned":
            if state.at_ms - obs.planned_ms < LAND_S * 1000:
                return True
            if not land(plan, state, obs.fee_rate):
                # The transaction carries an active-bin bound, so it reverts and pays its fee.
                self.record(obs, run, "missed", state.at_ms, net_pct=-100 * TX_FEE_LAMPORTS / plan.ticket,
                            net_sol=-TX_FEE_LAMPORTS / LAMPORTS, tx_sol=TX_FEE_LAMPORTS / LAMPORTS, rent_sol=0.0)
                return False
            run.status = "open"
            return True
        if run.status == "open":
            reason = exit_reason(plan, state)
            if reason and wants_reopen(plan, reason, state):
                reopen(plan, state)
            elif reason:
                run.status, run.reason, run.decided_ms = "closing", reason, state.at_ms
            return True
        if state.at_ms - run.decided_ms < LAND_S * 1000:
            return True
        res = settle(plan, state, obs.fee_rate)
        ticket = plan.ticket
        self.record(obs, run, "closed", state.at_ms, hold_s=(state.at_ms - obs.planned_ms) / 1000,
                    net_pct=res.net_pct, net_sol=res.net / LAMPORTS, fee_pct=100 * res.fee_value / ticket,
                    inventory_pct=100 * res.inventory_pnl / ticket, exit_cost_pct=100 * res.exit_cost / ticket,
                    tx_sol=res.tx_cost / LAMPORTS, rent_sol=res.rent_cost / LAMPORTS, unfilled=res.unfilled_x,
                    in_range_share=plan.in_range_ticks / plan.ticks if plan.ticks else None)
        return False

    def tick(self, obs: Observation) -> None:
        lo, hi = self._bounds(obs)
        state = self.read_state(obs.pool, lo, hi)
        obs.active, obs.last_state_ms = state.active, state.at_ms
        obs.runs = [r for r in obs.runs if self.step(obs, r, state)]

    def retire(self, obs: Observation, status: str) -> None:
        self.db.execute("UPDATE observations SET status=?, closed_ms=? WHERE id=?", (status, self.now_ms(), obs.id))
        self.db.commit()
        self.cooldown[obs.pool] = self.now() + self.profile.cooldown_s
        self.open.remove(obs)

    def cycle(self) -> None:
        """One pass. A failed read skips that step: nothing here may stop the loop."""
        if len(self.open) < self.profile.max_open and self.now() - self.last_select >= self.profile.select_s:
            try:
                self.select()
            except Exception as exc:  # noqa: BLE001 - a dead API must show in status, not kill the bot
                self.counts["select_failed"] += 1
                log.warning("select failed: %s", str(exc)[:160])
        for obs in list(self.open):
            try:
                self.tick(obs)
            except Exception as exc:  # noqa: BLE001 - same: counted, and retired below when stale
                self.counts["read_failed"] += 1
                log.warning("read failed: %s", str(exc)[:160])
            if not obs.runs:
                self.retire(obs, "closed")
            elif self.now_ms() - obs.last_state_ms > self.profile.stale_s * 1000:
                # A blind stretch cannot be priced, so it is excluded and counted, never scored.
                for run in obs.runs:
                    self.record(obs, run, "stale", self.now_ms())
                self.retire(obs, "stale")
        self.housekeeping()

    def housekeeping(self) -> None:
        if self.now() - self.last_status >= 60:
            self.last_status = self.now()
            body = {"at_ms": self.now_ms(), "version": VERSION, "profile": self.profile.name, "open": len(self.open),
                    "counts": dict(self.counts),
                    "last_selection": self.last_reasons}
            tmp = self.root / "status.json.tmp"
            tmp.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp, self.root / "status.json")
        if self.now() - self.last_prune >= 86_400:
            self.last_prune = self.now()
            cut = self.now_ms() - RETAIN_DAYS * 86_400_000
            self.db.execute("DELETE FROM arm_results WHERE at_ms < ?", (cut,))
            self.db.execute("DELETE FROM observations WHERE planned_ms < ?", (cut,))
            self.db.commit()

    def run(self, seconds: float | None = None) -> None:
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        until = self.now() + seconds if seconds else None
        while not stop["now"] and (until is None or self.now() < until):
            started = self.now()
            self.cycle()
            time.sleep(max(0.2, self.profile.poll_s - (self.now() - started)))


def bootstrap_lower(xs: list[float], *, seed: int = 20261104, n: int = 2_000) -> float:
    rng = random.Random(seed)
    means = sorted(st.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return means[int(0.05 * n)]


def by_pool_age(rows: list[dict[str, Any]], ages: dict[int, float | None]) -> dict[str, Any]:
    """The primary arm by pool age when picked. The audit's one cut still alive in October, and the
    cut most exposed to bin-array rent, so both show here. Unknown ages are counted, never bucketed."""
    cells: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in AGE_BUCKETS}
    unknown = 0
    for r in rows:
        age = ages.get(r["obs_id"])
        if age is None:
            unknown += 1
            continue
        cells[next(label for label, top in AGE_BUCKETS if age < top)].append(r)
    out: dict[str, Any] = {"unknown_age": unknown}
    for label, mine in cells.items():
        if mine:
            out[label] = {"n": len(mine), "mean_net_pct": round(st.fmean(r["net_pct"] for r in mine), 3),
                          "rent_paid_share": round(sum((r["rent_sol"] or 0) > 0 for r in mine) / len(mine), 3)}
    return out


def at_ticket(rows: list[dict[str, Any]], ticket_sol: float, days: float) -> float:
    """SOL a day at another ticket: fees and inventory scale with it, transactions and rent do not."""
    total = 0.0
    for r in rows:
        fixed = (r["tx_sol"] or 0.0) + (r["rent_sol"] or 0.0)
        gross_pct = (r["net_pct"] or 0.0) + 100 * fixed * LAMPORTS / TICKET
        total += ticket_sol * gross_pct / 100 - fixed
    return round(total / days, 3)


def ledger(root: Path, profile: Profile, *, limit: int = 25, points: int = 240) -> dict[str, Any]:
    """The primary arm's running total and its latest positions, for the dashboard.

    The total is in % of one stake, the unit of the terminal's own equity curve. Thinned to ``points``,
    always keeping the last, so a long run stays light on the page.
    """
    path = root / "lp_paper.db"
    if not path.exists():
        return {"curve": {"unit": "% of one stake", "points": []}, "recent": []}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute(
        "SELECT r.at_ms, r.status, r.exit_reason, r.hold_s, r.net_pct, r.fee_pct, r.rent_sol, r.reopened, "
        "o.pool, o.name, o.features_json FROM arm_results r JOIN observations o ON o.id = r.obs_id "
        "WHERE r.arm = ? AND r.status IN ('closed', 'missed') ORDER BY r.at_ms", (profile.primary,))]
    db.close()
    total, curve = 0.0, []
    for r in rows:
        total += r["net_pct"] or 0.0
        curve.append([r["at_ms"], round(total, 3)])
    step = max(1, len(curve) // points)
    thin = curve[::step] + ([curve[-1]] if curve and len(curve) % step != 1 % step else [])
    recent = []
    for r in rows[-limit:][::-1]:
        features = json.loads(r.pop("features_json") or "{}")
        recent.append({**r, "age_h": features.get("age_h"), "fee_tvl_30m": features.get("fee_tvl_30m")})
    return {"curve": {"unit": "% of one stake", "points": thin}, "recent": recent}


def report(root: Path | None = None, profile: Profile = FAST, *, arm_lower: bool = True) -> dict[str, Any]:
    """Per-arm results and the pre-registered verdict. Missed entries count: they cost a fee.

    ``arm_lower`` False skips the exploratory arms' bootstrap, which only the daily report needs. The
    primary arm's lower bound, which the pass line reads, is always computed.
    """
    root = root or profile.root(data_root())
    path = root / "lp_paper.db"
    if not path.exists():
        return {"status": "no_data"}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT * FROM arm_results")]
    ages = {r["id"]: json.loads(r["features_json"]).get("age_h")
            for r in db.execute("SELECT id, features_json FROM observations")}
    db.close()
    arms: dict[str, Any] = {}
    for name in sorted({r["arm"] for r in rows}):
        mine = [r for r in rows if r["arm"] == name]
        scored = [r for r in mine if r["status"] in ("closed", "missed")]
        closed = [r for r in scored if r["status"] == "closed"]
        cell: dict[str, Any] = {"n": len(scored), "missed": len(scored) - len(closed),
                                "stale_excluded": len(mine) - len(scored)}
        if closed:
            nets = [r["net_pct"] for r in scored]
            days = max(1 / 24, (max(r["at_ms"] for r in scored) - min(r["at_ms"] for r in scored)) / 86_400_000)
            wins, loss = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
            cell.update({
                "mean_net_pct": round(st.fmean(nets), 3), "median_net_pct": round(st.median(nets), 3),
                "win_pct": round(100 * sum(x > 0 for x in nets) / len(nets), 1),
                "profit_factor": round(wins / loss, 2) if loss else None,
                "lower95": round(bootstrap_lower(nets), 3) if len(nets) >= 30 and arm_lower else None,
                "fee_pct": round(st.fmean(r["fee_pct"] for r in closed), 3),
                "inventory_pct": round(st.fmean(r["inventory_pct"] for r in closed), 3),
                "exit_cost_pct": round(st.fmean(r["exit_cost_pct"] for r in closed), 3),
                "rent_paid_share": round(sum(r["rent_sol"] > 0 for r in closed) / len(closed), 3),
                "reopened_share": round(sum(r["reopened"] for r in closed) / len(closed), 3),
                "unfilled": sum(1 for r in closed if (r["unfilled"] or 0) > 0),
                "per_day": round(len(scored) / days, 1),
                "sol_per_day_by_ticket": {str(t): at_ticket(scored, t, days) for t in (0.5, 1.0, 2.0)},
                "exits": Counter(r["exit_reason"] for r in closed).most_common(6)})
        arms[name] = cell
    verdict: dict[str, Any] = {"arm": profile.primary, "pass": False, "why": "no_data"}
    primary = [r for r in rows if r["arm"] == profile.primary and r["status"] in ("closed", "missed")]
    if primary:
        nets = [r["net_pct"] for r in primary]
        days_seen = len({r["at_ms"] // 86_400_000 for r in primary})
        cut = st.median(r["at_ms"] for r in primary)
        halves = [st.fmean([r["net_pct"] for r in primary if side(r["at_ms"], cut)] or [0.0])
                  for side in (lambda t, c: t < c, lambda t, c: t >= c)]
        lower = bootstrap_lower(nets) if len(nets) >= 30 else None
        checks = {"n": len(nets) >= profile.pass_min_n, "days": days_seen >= profile.pass_min_days,
                  "mean": st.fmean(nets) >= profile.pass_min_mean_pct, "lower95": lower is not None and lower > 0,
                  "halves": all(h > 0 for h in halves)}
        verdict = {"arm": profile.primary, "pass": all(checks.values()), "checks": checks, "n": len(nets),
                   "days": days_seen,
                   "mean_net_pct": round(st.fmean(nets), 3), "lower95": None if lower is None else round(lower, 3),
                   "halves": [round(h, 3) for h in halves]}
    scored = [r for r in rows if r["arm"] == profile.primary and r["status"] in ("closed", "missed")]
    return {"status": "ok", "version": VERSION, "profile": profile.name, "ticket_sol": TICKET / LAMPORTS,
            "primary_by_pool_age_h": by_pool_age(scored, ages),
            "array_rent_sol": BIN_ARRAY_RENT_LAMPORTS / LAMPORTS, "verdict": verdict, "arms": arms}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "report"))
    parser.add_argument("--profile", choices=sorted(PROFILES), default="fast")
    parser.add_argument("--seconds", type=float)
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    if args.command == "report":
        print(json.dumps(report(profile=profile), indent=1, default=str))
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Bot(HttpTransport(rpc_url()), profile.root(data_root()), profile=profile).run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
