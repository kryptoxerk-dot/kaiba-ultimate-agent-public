"""Graduation observer: forward, PAPER-ONLY evidence for three pre-declared graduation rules.

Why this exists (lead, 2026-10-05). A 944-config profit search found no executable edge, but
three graduation leads survived it, and each was found POST HOC on a population our tracked
wallets later chose to trade (the future-attention artifact). The only clean test is forward
data on EVERY graduation, recorded before anyone looks:

* **A** robinhood Pons graduations whose curve is quoted in a NON-ETH token (OLD n=47
  +7.38%, NEW n=32 +18.97%, +4.80% without the top 3), entering ~30 s after graduation;
* **B** bsc Flap at graduation (~0 EV, a handful a day);
* **C** sol pump.fun after graduation onto PumpSwap (graduates were 8-16 pts less bad than
  curve tokens).

The rules and their pass lines are :data:`GRADUATION_HYPOTHESES`, written BEFORE any row
exists; :data:`DECLARATION_DIGEST` is pinned by a test, so changing a rule after results
exist fails a test and has to be said out loud. :func:`evaluate` reports them.

What it records, per graduation (table ``graduation_observations``, migration 040):

* detection: the graduation's own time (block time where the source carries it), when our
  ingest wrote it, when this observer saw it;
* the quote asset and the curve's stats at graduation;
* a paper BUY on the new pool at graduation + 30 s (:data:`ENTRY_AFTER_GRADUATION_S`): the
  pool's reserves read on chain, priced with the pool's own fee (and Pons' hook fee, and a
  Flap token's buy tax), so ``entry_tokens`` is what that buy would have received;
* marks at +5 / +15 / +60 min and +6 h after the entry (:data:`HORIZONS_S`): the SELL of
  those tokens into the live pool with our paper buy put back in (:meth:`PoolState.holding`),
  plus the spot value -- every number from the pool itself, one source, never a wallet feed
  or an aggregator.

Event sources, VERIFIED on the box 2026-10-05:

* **sol** -- ``events`` kind ``token.migrated`` from ``pumpportal`` (1,100 in 24 h). The frame
  carries no slot and no pool; ``migrated_ms`` is our RECEIVE clock (a sample migration's
  ``blockTime`` was 3.3 s earlier). The pool is the canonical PumpSwap PDA
  ``["pool", 0u16, PDA(pump, ["pool-authority", mint]), mint, WSOL]`` under
  ``pAMMBay...`` -- derived here, matched 3/3 live (owner, base mint, quote WSOL). The block
  time is read with ``getTransaction`` (``sol_block_time``) and replaces the receive clock.
* **robinhood** -- ``events`` kind ``token.migrated`` from the Pons poller (30 in 24 h; 8 of
  69 in 51 h quoted in an ERC-20). ``migrated_ms`` is the block timestamp; ingest lag p50
  6.4 s, p90 57 s. The graduation receipt carries the v4 ``Initialize`` (PoolKey: native ETH
  / token, fee 0, tickSpacing 200, the Pons hook), a FULL-RANGE ``ModifyLiquidity``
  (-887200..887200), and a ``HookFeeCollected`` of exactly 1.000% of the first swap's output.
  State is ``extsload(slot, 4)`` on the PoolManager (sqrtPrice, lpFee, liquidity), verified.
* **bsc** -- ``tokens.migrated_ms`` on ``launchpad='flap'`` rows (``ingest/flap.py`` stamps
  it at block time from ``LaunchedToDEX``; GMGN feeds also stamp it). No event is written,
  so this observer scans ``tokens`` every ``bsc_scan_every_s``. The pool is the Flap record's
  word 14 (= PancakeSwap V2 ``getPair``, verified 4/4); state is ``getReserves()``.

Budget. Every read is JSON-RPC through ``providers._http.post_json`` at
``Priority.RESEARCH``, on this observer's OWN limiter buckets (``buckets``), never the
``rpc`` / ``robinhood-rpc`` buckets protection's EXIT reads spend, so a refusal or a 429 here
cannot cost a stop a slot. On top of the limiter, a hard per-hour and per-day cap on JSON-RPC
calls (``max_calls_per_hour`` / ``max_calls_per_day``, persisted in ``kv`` across restarts).
Marks are batched: one ``getMultipleAccounts`` / one JSON-RPC batch per chain per pass.

What it never does: no order, no signal, no decision, no position, no lane or config change,
no ``gmgn-cli``. It writes exactly two tables -- ``graduation_observations`` and two ``kv``
keys -- and every JSON-RPC method it can send is a read (:data:`READ_METHODS`; anything else
raises before a byte leaves). ``tests/test_graduation_observer.py`` pins both.

Wiring (the lead's call; this module edits neither file):

* ingest runner: ``REGISTRY["graduation_observer"] = lambda stop: graduation_observer.run(stop=stop)``
  (a long-running loop; :func:`run` idles when no RPC is configured), or
* ops job: call :func:`tick` with the job's ``conn`` and ``ctx.params`` every 5 s
  (``interval_s: 5``); a tick is a few short statements and at most a few HTTP requests.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import random
import sqlite3
import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, digest, now_ms

log = logging.getLogger(__name__)

TABLE = "graduation_observations"
MIGRATION_FILE = Path(__file__).resolve().parents[1] / "core" / "migrations" / "040_graduation_observations.sql"
FEED = "graduation_observer"
EVENT_KIND = "token.migrated"  # schemas.EventKind.TOKEN_MIGRATED
CURSOR_KEY = "graduation_observer:event_cursor"
BSC_SCAN_KEY = "graduation_observer:bsc_scan_ms"
BUDGET_KEY = "graduation_observer:rpc_budget"

#: The paper entry: this long after the graduation's own time.
ENTRY_AFTER_GRADUATION_S = 30
#: An entry not made by ``entry_due + ENTRY_MAX_LATE_S`` is never made (status ``late``):
#: the population is graduations entered 30-90 s after graduation, nothing later.
ENTRY_MAX_LATE_S = 60
#: Marks, seconds after the ENTRY: +5 min, +15 min, +60 min, +6 h.
HORIZONS_S: tuple[int, ...] = (300, 900, 3600, 21600)

#: The only JSON-RPC methods this module may send. Every one is a read.
READ_METHODS: frozenset[str] = frozenset(
    {"eth_call", "eth_getTransactionReceipt", "getMultipleAccounts", "getTransaction"}
)

# --------------------------------------------------------------------------------------
# the pre-declared rules -- WRITTEN 2026-10-05, BEFORE ANY ROW EXISTS
# --------------------------------------------------------------------------------------

#: Each rule is judged on ``graduation_observations`` at ONE horizon declared here. Return
#: of one observation: ``value_h / entry_quote_in * (1 - cost)**2 - 1``, where ``value_h`` is
#: the mark's SELL of ``entry_tokens`` into the pool (pool fee, Pons hook fee and Flap sell
#: tax inside; our paper buy put back in) and ``cost`` is the per-leg cost ON TOP of the pool
#: (router commission, priority fee, latency slippage). A drained pool marks 0 (-100%).
#: Population: ``sampled=1``, an entry made 30-90 s after graduation, the rule's filter.
#: A row whose horizon mark was MISSED (no read inside its grace) is excluded and counted;
#: more than ``max_missed_share`` of them makes the verdict INCONCLUSIVE, not PASS.
#: PASS = n >= min_n AND >= min_distinct_utc_days AND mean > 0 AND the 2.5th percentile of
#: ``bootstrap_resamples`` resampled means > 0. Every other horizon is reported, NOT evidence.
#: Changing any field after results exist is changing the test; the work log must say so.
GRADUATION_HYPOTHESES: tuple[dict[str, Any], ...] = (
    {
        "rule": "A_rh_pons_nonnative_quote",
        "chain": "robinhood",
        "venue": "pons_v4",
        "filter": {"quote_is_native": 0},
        "provenance": ("post hoc 2026-10-05: OLD n=47 +7.38%, NEW n=32 +18.97% (+4.80% without top 3), "
                       "entering ~30 s after graduation; WETH counts as ETH"),
        "entry_after_graduation_s": ENTRY_AFTER_GRADUATION_S,
        "entry_max_late_s": ENTRY_MAX_LATE_S,
        "horizon_s": 3600,
        "report_horizons_s": HORIZONS_S,
        "cost_per_leg": "0.045",
    },
    {
        "rule": "B_bsc_flap_graduation",
        "chain": "bsc",
        "venue": "flap_pancake_v2",
        "filter": {},
        "provenance": "search 2026-10-05: ~0 EV at graduation, ~12/day",
        "entry_after_graduation_s": ENTRY_AFTER_GRADUATION_S,
        "entry_max_late_s": ENTRY_MAX_LATE_S,
        "horizon_s": 3600,
        "report_horizons_s": HORIZONS_S,
        "cost_per_leg": "0.035",
    },
    {
        "rule": "C_sol_pumpswap_after_graduation",
        "chain": "sol",
        "venue": "pumpswap",
        "filter": {},
        "provenance": "search 2026-10-05: graduated tokens 8-16 pts less bad than curve tokens",
        "entry_after_graduation_s": ENTRY_AFTER_GRADUATION_S,
        "entry_max_late_s": ENTRY_MAX_LATE_S,
        "horizon_s": 3600,
        "report_horizons_s": HORIZONS_S,
        "cost_per_leg": "0.030",
    },
)
#: Shared pass line, also pre-declared.
PASS_LINE: dict[str, Any] = {
    "min_n": 30,
    "min_distinct_utc_days": 3,
    "max_missed_share": "0.10",
    "bootstrap_resamples": 10_000,
    "bootstrap_seed": 20261005,
    "lower_percentile": "0.025",
}
DECLARED_ON = "2026-10-05"
DECLARATION_DIGEST = digest({"rules": GRADUATION_HYPOTHESES, "pass": PASS_LINE, "horizons": HORIZONS_S,
                             "declared_on": DECLARED_ON})[:16]

DEFAULT_PARAMS: dict[str, Any] = {
    "chains": ["sol", "robinhood", "bsc"],
    "poll_s": 5.0,
    #: Hard caps on JSON-RPC CALLS (a batch of 5 is 5), across all chains.
    "max_calls_per_hour": 400,
    "max_calls_per_day": 6000,
    #: Deterministic 1-in-N by token hash (an unsampled graduation is recorded, never read).
    "sample_every": {"sol": 3, "robinhood": 1, "bsc": 1},
    #: Paper size on a natively quoted pool, base units (~ the $120 flat live cap).
    "paper_size_native": {"sol": 500_000_000, "robinhood": 40_000_000_000_000_000,
                          "bsc": 150_000_000_000_000_000},
    #: On an ERC-20-quoted pool: this share of the pool's quote reserve at entry (~1% is what
    #: the native sizes above are on a fresh graduate).
    "paper_size_bps_of_quote_reserve": 100,
    "max_batch": 20,
    "max_rows_per_pass": 60,
    "bsc_scan_every_s": 10.0,
    "detect_lookback_s": 600,
    "resolve_retry_s": 5.0,
    "sol_block_time": True,
    "buckets": {"sol": "graduation-rpc-sol", "robinhood": "graduation-rpc-robinhood", "bsc": "graduation-rpc-bsc"},
    "wait_for_slot_s": 2.0,
    "timeout_s": 15.0,
}

# --------------------------------------------------------------------------------------
# venue constants
# --------------------------------------------------------------------------------------

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_SWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
#: PumpSwap ``Pool`` account: 8 discriminator, u8 bump, u16 index, then creator, base_mint,
#: quote_mint, lp_mint, pool_base_token_account, pool_quote_token_account (32 bytes each).
#: VERIFIED 2026-10-05 on 3 live pools (301 bytes; base mint at 43 == the graduated mint).
PUMPSWAP_POOL_MIN_LEN = 203
#: PumpSwap's first post-graduation fee tier, on the quote leg. The tree's single number is
#: ``curve_price.PUMPSWAP_TIER1_TOTAL_FEE_BPS`` (a test pins them equal); not re-measured here.
PUMPSWAP_FEE_BPS = 120
#: SPL token account: ``amount`` (u64 LE) at byte 64, same in Token-2022's base layout.
SPL_AMOUNT_OFFSET = 64
#: PancakeSwap V2: 25 bps off the input (``getAmountsOut``'s 9975/10000).
PANCAKE_V2_FEE_PPM = 2500
SEL_GET_RESERVES = "0x0902f1ac"
#: Pons V4 hook fee, bps of a swap's OUTPUT. MEASURED 2026-10-05 on one graduation receipt
#: (``HookFeeCollected`` 9.2758844e22 on a 9.2758844e24-token output = 100 bps); re-measured
#: per graduation from its own receipt when it can be (``pool_meta.hook_bps``). The sell side
#: is ASSUMED symmetric (no sell receipt read).
PONS_HOOK_FEE_BPS_DEFAULT = 100
#: ``extsload(bytes32 startSlot, uint256 nSlots)`` on the v4 PoolManager. VERIFIED 2026-10-05.
SEL_EXTSLOAD_N = "0x35fd631a"
V4_FULL_RANGE_TICK = 887_000
Q96 = 1 << 96
ROBINHOOD_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
EVM_ZERO = "0x" + "0" * 40

#: Venue name per chain (one venue each).
VENUE: dict[str, str] = {"sol": "pumpswap", "robinhood": "pons_v4", "bsc": "flap_pancake_v2"}

# --------------------------------------------------------------------------------------
# small pure helpers
# --------------------------------------------------------------------------------------


def _params(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    p = dict(DEFAULT_PARAMS)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if isinstance(p.get(key), dict) and isinstance(value, Mapping):
            p[key] = {**p[key], **dict(value)}
        else:
            p[key] = value
    if isinstance(p.get("chains"), str):
        p["chains"] = [c.strip() for c in p["chains"].split(",") if c.strip()]
    return p


def _per_chain(p: Mapping[str, Any], key: str, chain: str, default: Any = None) -> Any:
    value = p.get(key)
    if isinstance(value, Mapping):
        return value.get(chain, default)
    return value if value is not None else default


def obs_id_for(chain: str, token: str) -> str:
    return "grd_" + digest({"chain": chain, "token": token})[:24]


def in_sample(token: str, every: int) -> bool:
    """Deterministic 1-in-``every`` by the token's own hash: independent of anything we know
    about the token, so the sample is not selected on outcome."""
    every = max(1, int(every or 1))
    return every == 1 or int(hashlib.sha256(token.encode()).hexdigest()[:12], 16) % every == 0


def grace_ms(horizon_s: int) -> int:
    """How late a mark may still be taken: max(120 s, 10% of the horizon)."""
    return max(120_000, int(horizon_s) * 100)


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _words(raw: Any) -> list[int]:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return []
    body = raw[2:]
    try:
        return [int(body[i:i + 64], 16) for i in range(0, len(body) - 63, 64)]
    except ValueError:
        return []


def _signed(word: int, bits: int = 256) -> int:
    return word - (1 << bits) if word >= 1 << (bits - 1) else word


def _addr(word: int) -> str:
    return "0x" + format(word & ((1 << 160) - 1), "040x")


def _addr_word(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


# --------------------------------------------------------------------------------------
# Solana: the PumpSwap pool of a graduated mint
# --------------------------------------------------------------------------------------


def find_program_address(seeds: Sequence[bytes], program: bytes) -> bytes | None:
    """Solana ``find_program_address``: the first bump (255 down) whose hash is OFF the
    ed25519 curve. Uses ``launch_feed``'s curve check (verified on pump.fun curves)."""
    from kaiba.ingest.launch_feed import _on_ed25519_curve  # noqa: PLC2701 - the tree's one copy

    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_ed25519_curve(h):
            return h
    return None  # pragma: no cover


def b58decode(s: str) -> bytes:
    from kaiba.ingest.launch_feed import _b58decode  # noqa: PLC2701

    return _b58decode(s)


def b58encode(b: bytes) -> str:
    from kaiba.ingest.launch_feed import _b58encode  # noqa: PLC2701

    return _b58encode(b)


def pumpswap_pool_address(mint: str) -> str | None:
    """The canonical PumpSwap pool pump.fun migrates ``mint`` into. VERIFIED 3/3 live."""
    try:
        raw = b58decode(mint)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    authority = find_program_address([b"pool-authority", raw], b58decode(PUMP_PROGRAM))
    if authority is None:
        return None
    pool = find_program_address([b"pool", (0).to_bytes(2, "little"), authority, raw, b58decode(WSOL_MINT)],
                                b58decode(PUMP_SWAP_PROGRAM))
    return b58encode(pool) if pool is not None else None


def decode_pumpswap_pool(data: bytes) -> dict[str, str] | None:
    if len(data) < PUMPSWAP_POOL_MIN_LEN:
        return None
    return {"base_mint": b58encode(data[43:75]), "quote_mint": b58encode(data[75:107]),
            "base_vault": b58encode(data[139:171]), "quote_vault": b58encode(data[171:203])}


def _account_bytes(value: Any) -> bytes | None:
    if not isinstance(value, Mapping):
        return None
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str):
        return None
    try:
        return base64.b64decode(data[0])
    except (ValueError, TypeError):
        return None


def _sliced_amount(value: Any) -> int | None:
    raw = _account_bytes(value)
    if raw is None or len(raw) < 8:
        return None
    return struct.unpack("<Q", raw[:8])[0]


# --------------------------------------------------------------------------------------
# Uniswap v4 (Pons graduates)
# --------------------------------------------------------------------------------------

_TOPICS: dict[str, str] = {}


def _topic(signature: str) -> str:
    if signature not in _TOPICS:
        from kaiba.execution.policy import keccak256

        _TOPICS[signature] = "0x" + keccak256(signature.encode()).hex()
    return _TOPICS[signature]


def topic_v4_swap() -> str:
    return _topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")


def topic_v4_modify_liquidity() -> str:
    return _topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")


def decode_v4_slot0(word: int) -> dict[str, int]:
    """``Pool.State.slot0``: sqrtPriceX96 (160) | tick (24) | protocolFee (24) | lpFee (24)."""
    return {"sqrt_price_x96": word & ((1 << 160) - 1), "tick": _signed((word >> 160) & 0xFFFFFF, 24),
            "protocol_fee": (word >> 184) & 0xFFFFFF, "lp_fee_ppm": (word >> 208) & 0xFFFFFF}


def v4_virtual_reserves(sqrt_price_x96: int, liquidity: int) -> tuple[int, int]:
    """Full-range liquidity ``L`` at ``sqrtP`` behaves as x*y = L^2 with x = L/sqrtP, y = L*sqrtP."""
    if sqrt_price_x96 <= 0 or liquidity <= 0:
        return 0, 0
    return liquidity * Q96 // sqrt_price_x96, liquidity * sqrt_price_x96 // Q96


def parse_graduation_receipt(receipt: Any, token: str) -> dict[str, Any] | None:
    """The PoolKey (from ``Initialize``, verified by hashing back to the id), the liquidity's
    tick range, and the hook fee measured on the graduation's own first swap."""
    from kaiba.execution import onchain_pool as op
    from kaiba.ingest import robinhood as rh

    if not isinstance(receipt, Mapping) or not isinstance(receipt.get("logs"), list):
        return None
    logs = [lg for lg in receipt["logs"] if isinstance(lg, dict)]
    key = next((k for k in (op.decode_initialize_log(lg) for lg in logs
                            if str(lg.get("address", "")).lower() == op.POOL_MANAGER) if k), None)
    if key is None:
        return None
    c0, c1, fee, spacing, hooks = key
    t = token.lower()
    if t not in (c0, c1):
        return None
    pool_id = op.v4_pool_id(c0, c1, fee, spacing, hooks)
    out: dict[str, Any] = {"pool_id": pool_id, "currency0": c0, "currency1": c1, "fee": fee,
                           "tick_spacing": spacing, "hooks": hooks, "token_is_currency0": t == c0,
                           "quote": c1 if t == c0 else c0, "state_slot": op.v4_state_slot(pool_id),
                           "hook_bps": None, "tick_lower": None, "tick_upper": None}
    swap_output: tuple[str, int] | None = None
    for lg in logs:
        topics = [str(x).lower() for x in (lg.get("topics") or [])]
        if not topics:
            continue
        w = _words(lg.get("data"))
        if topics[0] == topic_v4_modify_liquidity() and len(topics) > 1 and topics[1] == pool_id and len(w) >= 3:
            out["tick_lower"], out["tick_upper"] = _signed(w[0]), _signed(w[1])
        elif topics[0] == topic_v4_swap() and len(topics) > 1 and topics[1] == pool_id and len(w) >= 2:
            a0, a1 = _signed(w[0]), _signed(w[1])
            swap_output = (c0, a0) if a0 > 0 else (c1, a1) if a1 > 0 else None
        elif topics[0] == rh.TOPIC_CURVE_BUY and len(w) >= 2:
            out["graduating_buy_quote_in"], out["graduating_buy_tokens_out"] = str(w[0]), str(w[1])
    if swap_output is not None:
        for lg in logs:
            topics = [str(x).lower() for x in (lg.get("topics") or [])]
            w = _words(lg.get("data"))
            if topics and topics[0] == rh.TOPIC_HOOK_FEE and len(w) >= 3 and _addr(w[0]) == swap_output[0]:
                out["hook_bps"] = int(((w[1] + w[2]) * 10_000 + swap_output[1] // 2) // swap_output[1])  # nearest bp
                break
    return out


# --------------------------------------------------------------------------------------
# the pool, priced
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolState:
    """A constant-product view of the new pool. Reserves are base units (virtual on v4).

    Fees, each where the venue takes it: ``fee_input_ppm`` off whatever goes in (PancakeSwap
    V2 25 bps; v4 ``lpFee``), ``fee_output_bps`` off whatever comes out (the Pons hook),
    ``fee_quote_bps`` off the quote leg in either direction (PumpSwap). ``buy_tax_bps`` /
    ``sell_tax_bps``: a Flap token's own tax, taken in tokens."""

    quote: int
    base: int
    basis: str
    fee_input_ppm: int = 0
    fee_output_bps: int = 0
    fee_quote_bps: int = 0
    buy_tax_bps: int = 0
    sell_tax_bps: int = 0

    @property
    def ok(self) -> bool:
        return self.quote > 0 and self.base > 0

    def into_pool(self, quote_in: int) -> int:
        """The quote that reaches the reserves from a ``quote_in`` buy."""
        q = int(quote_in) * (10_000 - self.fee_quote_bps) // 10_000
        return q * (1_000_000 - self.fee_input_ppm) // 1_000_000

    def buy(self, quote_in: int) -> int:
        if not self.ok or quote_in <= 0:
            return 0
        eff = self.into_pool(quote_in)
        out = self.base * eff // (self.quote + eff)
        out = out * (10_000 - self.fee_output_bps) // 10_000
        return out * (10_000 - self.buy_tax_bps) // 10_000

    def sell(self, tokens: int) -> int:
        if not self.ok or tokens <= 0:
            return 0
        t = int(tokens) * (10_000 - self.sell_tax_bps) // 10_000
        t = t * (1_000_000 - self.fee_input_ppm) // 1_000_000
        out = self.quote * t // (self.base + t)
        out = out * (10_000 - self.fee_output_bps) // 10_000
        return out * (10_000 - self.fee_quote_bps) // 10_000

    def holding(self, shadow_quote: int) -> PoolState:
        """The pool as it would be had our paper buy landed: ``shadow_quote`` more quote at
        the same product (the same move ``snipe.with_holding`` makes on a curve)."""
        if shadow_quote <= 0 or not self.ok:
            return self
        q2 = self.quote + int(shadow_quote)
        return replace(self, quote=q2, base=self.quote * self.base // q2)

    def spot(self) -> Decimal | None:
        return Decimal(self.quote) / Decimal(self.base) if self.ok else None

    def as_json(self) -> dict[str, Any]:
        return {"quote": str(self.quote), "base": str(self.base), "basis": self.basis}


def sol_state(base: int | None, quote: int | None) -> PoolState | None:
    if base is None or quote is None:
        return None
    return PoolState(quote=int(quote), base=int(base), basis="pumpswap_vaults", fee_quote_bps=PUMPSWAP_FEE_BPS)


def v4_state(raw: Any, meta: Mapping[str, Any]) -> PoolState | None:
    """``extsload(slot, 4)`` -> state. Words: slot0, feeGrowth0, feeGrowth1, liquidity."""
    w = _words(raw)
    if len(w) < 6 or w[0] != 0x20 or w[1] < 4:
        return None
    slot0, liquidity = decode_v4_slot0(w[2]), w[5]
    r0, r1 = v4_virtual_reserves(slot0["sqrt_price_x96"], liquidity)
    token0 = bool(meta.get("token_is_currency0"))
    quote, base = (r1, r0) if token0 else (r0, r1)
    hook = meta.get("hook_bps")
    lo, hi = meta.get("tick_lower"), meta.get("tick_upper")
    full = lo is not None and hi is not None and int(lo) <= -V4_FULL_RANGE_TICK and int(hi) >= V4_FULL_RANGE_TICK
    return PoolState(quote=quote, base=base, basis="v4_full_range" if full else "v4_range_unverified",
                     fee_input_ppm=int(slot0["lp_fee_ppm"]),
                     fee_output_bps=int(hook) if hook is not None else PONS_HOOK_FEE_BPS_DEFAULT)


def v2_state(raw: Any, meta: Mapping[str, Any]) -> PoolState | None:
    """``getReserves()`` -> state; ``token0`` is the lower address (V2 sorts)."""
    w = _words(raw)
    if len(w) < 2:
        return None
    token0 = bool(meta.get("token_is_token0"))
    quote, base = (w[1], w[0]) if token0 else (w[0], w[1])
    return PoolState(quote=quote, base=base, basis="pancake_v2_reserves", fee_input_ppm=PANCAKE_V2_FEE_PPM,
                     buy_tax_bps=int(meta.get("buy_tax_bps") or 0), sell_tax_bps=int(meta.get("sell_tax_bps") or 0))


# --------------------------------------------------------------------------------------
# the budget
# --------------------------------------------------------------------------------------


def _kv_get(conn: sqlite3.Connection, key: str) -> Any:
    try:
        row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (key,))
    except sqlite3.Error:
        return None
    return jload(row["value"], None) if row else None


def _kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute("INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value, updated_ms=excluded.updated_ms", (key, jdump(value), now_ms()))


class RpcBudget:
    """A hard cap on JSON-RPC CALLS per UTC hour and per UTC day, persisted in ``kv`` so a
    restart does not reset it. ``take`` is all-or-nothing for one request."""

    def __init__(self, conn: sqlite3.Connection, per_hour: int, per_day: int) -> None:
        self.conn, self.per_hour, self.per_day = conn, max(0, int(per_hour)), max(0, int(per_day))
        saved = _kv_get(conn, BUDGET_KEY)
        self.state: dict[str, Any] = dict(saved) if isinstance(saved, dict) else {}
        self.refused = 0

    def _roll(self, at_ms: int) -> None:
        hour = datetime.fromtimestamp(at_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H")
        day = hour[:10]
        if self.state.get("day") != day:
            self.state.update(day=day, d=0)
        if self.state.get("hour") != hour:
            self.state.update(hour=hour, h=0)

    def take(self, calls: int, *, at_ms: int | None = None) -> bool:
        self._roll(at_ms or now_ms())
        if int(self.state.get("h", 0)) + calls > self.per_hour or int(self.state.get("d", 0)) + calls > self.per_day:
            self.refused += 1
            return False
        self.state["h"] = int(self.state.get("h", 0)) + calls
        self.state["d"] = int(self.state.get("d", 0)) + calls
        _kv_set(self.conn, BUDGET_KEY, self.state)
        return True


# --------------------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------------------

#: ``(chain, [(method, params)], endpoint) -> [result | None] | None`` (None = the request failed).
Rpc = Callable[[str, Sequence[tuple[str, list[Any]]], str], "list[Any] | None"]


def rpc_url(chain: str) -> str | None:
    """sol / robinhood: the configured RPC (Alchemy on the box); bsc: the dedicated snipe
    endpoint when set (protection's price reads use ``BSC_RPC_URL``'s key), else that one."""
    try:
        from kaiba.core.config import get_settings

        if chain == Chain.BSC.value:
            from kaiba.ingest.launch_feed import bsc_snipe_rpc_url

            return bsc_snipe_rpc_url() or get_settings().rpc_for(Chain.BSC) or None
        return get_settings().rpc_for(Chain(chain)) or None
    except Exception as exc:  # noqa: BLE001 - an unreadable setting is "not configured"
        log.debug("graduation_observer: no rpc for %s (%s)", chain, type(exc).__name__)
        return None


def default_rpc(p: Mapping[str, Any], conn: Any = None) -> Rpc:
    """JSON-RPC batches through ``post_json`` on this observer's own limiter bucket, at
    ``Priority.RESEARCH``, never cached."""

    def send(chain: str, calls: Sequence[tuple[str, list[Any]]], endpoint: str) -> list[Any] | None:
        from kaiba.providers._http import post_json

        url = rpc_url(chain)
        if not url or not calls:
            return None
        body = [{"jsonrpc": "2.0", "id": i + 1, "method": m, "params": a} for i, (m, a) in enumerate(calls)]
        got = post_json(str(_per_chain(p, "buckets", chain, f"graduation-rpc-{chain}")), f"grad.{endpoint}", url,
                        json_body=body, priority=Priority.RESEARCH, ttl_s=0.0,
                        wait_for_slot_s=float(p.get("wait_for_slot_s") or 0), timeout_s=float(p.get("timeout_s") or 15),
                        conn=conn)
        if not got.ok or not isinstance(got.data, list):
            return None
        by_id = {item.get("id"): item for item in got.data if isinstance(item, Mapping)}
        return [(by_id.get(i + 1) or {}).get("result") for i in range(len(calls))]

    return send


class _BudgetSpent(Exception):
    """This pass's budget is gone; stop reading, keep every row for the next pass."""


# --------------------------------------------------------------------------------------
# rows
# --------------------------------------------------------------------------------------


def ensure_table(conn: sqlite3.Connection) -> None:
    """Migration 040's DDL (idempotent), for a connection that has not migrated."""
    conn.executescript(MIGRATION_FILE.read_text(encoding="utf-8"))


def _update(conn: sqlite3.Connection, obs_id: str, at_ms: int, **cols: Any) -> None:
    cols["updated_ms"] = at_ms
    sets = ", ".join(f"{k}=?" for k in cols)
    conn.execute(f"UPDATE {TABLE} SET {sets} WHERE obs_id=?", (*cols.values(), obs_id))


def insert_observation(conn: sqlite3.Connection, *, chain: str, token: str, source: str, graduated_ms: int,
                       graduated_basis: str, detected_ms: int, p: Mapping[str, Any], event_id: int | None = None,
                       signature: str | None = None, source_seen_ms: int | None = None,
                       curve: Mapping[str, Any] | None = None) -> bool:
    """One row per graduation (``INSERT OR IGNORE``: a re-seen graduation is a no-op).
    ``True`` when the row is new."""
    sampled = in_sample(token, int(_per_chain(p, "sample_every", chain, 1) or 1))
    due = int(graduated_ms) + ENTRY_AFTER_GRADUATION_S * 1000
    if not sampled:
        status, nxt = "unsampled", None
    elif detected_ms > due + ENTRY_MAX_LATE_S * 1000:
        status, nxt = "late", None
    else:
        status, nxt = "pending", detected_ms
    cur = conn.execute(
        f"INSERT OR IGNORE INTO {TABLE} (obs_id, chain, token, venue, source, event_id, signature, graduated_ms, "
        "graduated_basis, source_seen_ms, detected_ms, detect_latency_ms, sampled, curve_json, entry_due_ms, "
        "next_due_ms, status, note, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (obs_id_for(chain, token), chain, token, VENUE[chain], source, event_id, signature, int(graduated_ms),
         graduated_basis, source_seen_ms, detected_ms, detected_ms - int(graduated_ms), int(sampled),
         jdump(dict(curve or {})), due, nxt, status, "detected_after_entry_window" if status == "late" else None,
         detected_ms, detected_ms),
    )
    return bool(cur.rowcount)


# --------------------------------------------------------------------------------------
# the observer
# --------------------------------------------------------------------------------------


class Observer:
    """One pass = detect, resolve, enter, mark. Every read goes through :meth:`send`."""

    def __init__(self, conn: sqlite3.Connection, params: Mapping[str, Any] | None = None, *,
                 rpc: Rpc | None = None, clock: Callable[[], int] = now_ms) -> None:
        self.conn = conn
        self.p = _params(params)
        self.clock = clock
        self.rpc = rpc or default_rpc(self.p, conn)
        self.budget = RpcBudget(conn, int(self.p["max_calls_per_hour"]), int(self.p["max_calls_per_day"]))
        self.counts: dict[str, int] = {"detected": 0, "resolved": 0, "entered": 0, "marks": 0, "late": 0,
                                       "unresolved": 0, "calls": 0, "requests": 0, "failed_requests": 0,
                                       "budget_refused": 0}

    @property
    def chains(self) -> list[str]:
        return [c for c in (str(x) for x in self.p.get("chains") or []) if c in VENUE]

    # ---- transport ---------------------------------------------------------------

    def send(self, chain: str, calls: Sequence[tuple[str, list[Any]]], endpoint: str) -> list[Any] | None:
        """READ_METHODS only (raises otherwise, before anything is sent); all-or-nothing
        against the budget (raises :class:`_BudgetSpent`); ``None`` when the request failed."""
        for method, _ in calls:
            if method not in READ_METHODS:
                raise PermissionError(f"graduation_observer sends reads only, not {method!r}")
        if not calls:
            return []
        if not self.budget.take(len(calls), at_ms=self.clock()):
            self.counts["budget_refused"] += 1
            raise _BudgetSpent
        self.counts["requests"] += 1
        self.counts["calls"] += len(calls)
        try:
            got = self.rpc(chain, list(calls), endpoint)
        except Exception as exc:  # noqa: BLE001 - a failed read is retried, never a crash
            log.debug("graduation_observer: %s %s raised %s", chain, endpoint, type(exc).__name__)
            got = None
        if got is None or len(got) < len(calls):
            self.counts["failed_requests"] += 1
            return None
        return got

    def _spend(self, rows: Iterable[Mapping[str, Any]], calls_each: int) -> None:
        for r in rows:
            self.conn.execute(f"UPDATE {TABLE} SET rpc_calls=rpc_calls+? WHERE obs_id=?", (calls_each, r["obs_id"]))

    # ---- detect ------------------------------------------------------------------

    def detect(self) -> int:
        now = self.clock()
        found = 0
        wanted = set(self.chains)
        cursor = _kv_get(self.conn, CURSOR_KEY)
        if not isinstance(cursor, int):
            top = fetch_one(self.conn, "SELECT MAX(id) AS id FROM events") or {}
            _kv_set(self.conn, CURSOR_KEY, int(top.get("id") or 0))  # cold start: from now on
        else:
            rows = fetch_all(self.conn, "SELECT id, ts_ms, chain, subject, payload FROM events "
                             "WHERE kind=? AND id>? ORDER BY id LIMIT 500", (EVENT_KIND, cursor))
            for r in rows:
                chain = str(r.get("chain") or "")
                if chain in wanted and chain in ("sol", "robinhood") and r.get("subject"):
                    found += self._insert_event(r, now)
            if rows:
                _kv_set(self.conn, CURSOR_KEY, int(rows[-1]["id"]))
        if "bsc" in wanted:
            found += self._detect_bsc(now)
        self.counts["detected"] += found
        return found

    def _token_row(self, chain: str, token: str) -> dict[str, Any]:
        return fetch_one(self.conn, "SELECT created_ms, launchpad, creator, meta_json FROM tokens "
                         "WHERE chain=? AND address=?", (chain, token)) or {}

    def _insert_event(self, r: Mapping[str, Any], now: int) -> int:
        chain, token = str(r["chain"]), str(r["subject"])
        payload = jload(r.get("payload"), {}) or {}
        migrated = int(payload.get("migrated_ms") or r["ts_ms"])
        tok = self._token_row(chain, token)
        meta = jload(tok.get("meta_json"), {}) or {}
        curve: dict[str, Any] = {"created_ms": tok.get("created_ms"), "launchpad": tok.get("launchpad"),
                                 "creator": tok.get("creator")}
        if tok.get("created_ms"):
            curve["seconds_to_graduate"] = (migrated - int(tok["created_ms"])) // 1000
        if chain == "robinhood":
            basis = "block_time"
            curve.update({"pair_token": meta.get("pair_token"), "graduation_threshold_wei":
                          meta.get("graduation_threshold_wei"), "seed_token_amount": payload.get("token_amount"),
                          "seed_pair_token_amount": payload.get("pair_token_amount"), "block": payload.get("slot")})
        else:
            # PumpPortal's frame stamp when it carried one, else our receive clock; the
            # resolve step replaces either with the transaction's blockTime when it reads it.
            basis = "provider_timestamp" if payload.get("source_ms") else "ingest_clock"
        return int(insert_observation(
            self.conn, chain=chain, token=token, source=f"event:{EVENT_KIND}", graduated_ms=migrated,
            graduated_basis=basis, detected_ms=now, p=self.p, event_id=int(r["id"]),
            signature=payload.get("signature"), source_seen_ms=int(r["ts_ms"]), curve=curve))

    def _detect_bsc(self, now: int) -> int:
        last = _kv_get(self.conn, BSC_SCAN_KEY)
        if isinstance(last, int) and now - last < float(self.p["bsc_scan_every_s"]) * 1000:
            return 0
        _kv_set(self.conn, BSC_SCAN_KEY, now)
        rows = fetch_all(self.conn, "SELECT address, created_ms, migrated_ms, creator, meta_json FROM tokens "
                         "WHERE chain='bsc' AND launchpad='flap' AND migrated_ms > ?",
                         (now - int(self.p["detect_lookback_s"]) * 1000,))
        found = 0
        for r in rows:
            meta = jload(r.get("meta_json"), {}) or {}
            migrated = int(r["migrated_ms"])
            curve = {"created_ms": r.get("created_ms"), "creator": r.get("creator"), "row_source": meta.get("source"),
                     "seconds_to_graduate": (migrated - int(r["created_ms"])) // 1000 if r.get("created_ms") else None}
            found += int(insert_observation(
                self.conn, chain="bsc", token=str(r["address"]).lower(), source="tokens.migrated_ms",
                graduated_ms=migrated, graduated_basis="chain_or_provider_timestamp", detected_ms=now, p=self.p,
                curve=curve))
        return found

    # ---- rows due ------------------------------------------------------------------

    def _due(self, status: str, chain: str, now: int) -> list[dict[str, Any]]:
        return fetch_all(self.conn, f"SELECT * FROM {TABLE} WHERE status=? AND chain=? AND next_due_ms IS NOT NULL "
                         "AND next_due_ms<=? ORDER BY next_due_ms LIMIT ?",
                         (status, chain, now, int(self.p["max_rows_per_pass"])))

    def _expire_entries(self, now: int) -> None:
        cut = now - ENTRY_MAX_LATE_S * 1000
        for status, label in (("pending", "unresolved"), ("resolved", "late")):
            rows = fetch_all(self.conn, f"SELECT obs_id, note FROM {TABLE} WHERE status=? AND entry_due_ms<?",
                             (status, cut))
            for r in rows:
                _update(self.conn, r["obs_id"], now, status=label, next_due_ms=None,
                        note=r.get("note") or ("pool_never_resolved" if label == "unresolved" else "entry_not_made_in_window"))
                self.counts[label] += 1

    # ---- resolve -------------------------------------------------------------------

    def resolve(self, chain: str, now: int) -> None:
        rows = self._due("pending", chain, now)[:int(self.p["max_batch"])]
        if not rows:
            return
        {"sol": self._resolve_sol, "robinhood": self._resolve_rh, "bsc": self._resolve_bsc}[chain](rows, now)

    def _retry(self, r: Mapping[str, Any], now: int, note: str) -> None:
        _update(self.conn, r["obs_id"], now, attempts=int(r.get("attempts") or 0) + 1, note=note[:120],
                next_due_ms=now + int(float(self.p["resolve_retry_s"]) * 1000))

    def _resolved(self, r: Mapping[str, Any], now: int, *, pool: str, meta: Mapping[str, Any], quote: str | None,
                  native: bool, curve_extra: Mapping[str, Any] | None = None, graduated_ms: int | None = None,
                  basis: str | None = None) -> None:
        curve = {**(jload(r.get("curve_json"), {}) or {}), **dict(curve_extra or {})}
        cols: dict[str, Any] = {"status": "resolved", "pool": pool, "pool_meta_json": jdump(dict(meta)),
                                "quote_asset": "native" if native else quote, "quote_is_native": int(native),
                                "curve_json": jdump(curve), "next_due_ms": int(r["entry_due_ms"]), "note": None,
                                "attempts": int(r.get("attempts") or 0) + 1}
        if graduated_ms is not None:
            due = graduated_ms + ENTRY_AFTER_GRADUATION_S * 1000
            cols.update(graduated_ms=graduated_ms, graduated_basis=basis, entry_due_ms=due, next_due_ms=due,
                        detect_latency_ms=int(r["detected_ms"]) - graduated_ms)
        _update(self.conn, r["obs_id"], now, **cols)
        self.counts["resolved"] += 1

    def _resolve_sol(self, rows: list[dict[str, Any]], now: int) -> None:
        pools = {r["obs_id"]: pumpswap_pool_address(r["token"]) for r in rows}
        for r in rows:
            if not pools[r["obs_id"]]:
                self._retry(r, now, "pool_pda_underivable")
        rows = [r for r in rows if pools[r["obs_id"]]]
        if not rows:
            return
        calls: list[tuple[str, list[Any]]] = [("getMultipleAccounts", [[pools[r["obs_id"]] for r in rows],
                                                                      {"encoding": "base64", "commitment": "confirmed"}])]
        want_time = [r for r in rows if bool(self.p.get("sol_block_time")) and r.get("signature")
                     and r.get("graduated_basis") != "block_time"]
        calls += [("getTransaction", [r["signature"], {"commitment": "confirmed", "maxSupportedTransactionVersion": 0,
                                                       "encoding": "base64"}]) for r in want_time]
        got = self.send("sol", calls, "sol_resolve")
        if got is None:
            for r in rows:
                self._retry(r, now, "rpc_failed")
            return
        self._spend(rows, 1)
        self._spend(want_time, 1)
        accounts = ((got[0] or {}).get("value") if isinstance(got[0], Mapping) else None) or []
        times = {r["obs_id"]: (tx.get("blockTime") if isinstance(tx, Mapping) else None)
                 for r, tx in zip(want_time, got[1:], strict=False)}
        for r, acct in zip(rows, list(accounts) + [None] * (len(rows) - len(accounts)), strict=False):
            raw = _account_bytes(acct)
            pool = decode_pumpswap_pool(raw) if raw else None
            owner = acct.get("owner") if isinstance(acct, Mapping) else None
            if pool is None or owner != PUMP_SWAP_PROGRAM or pool["base_mint"] != r["token"] or pool["quote_mint"] != WSOL_MINT:
                self._retry(r, now, "pool_not_found" if pool is None else "pool_mismatch")
                continue
            bt = times.get(r["obs_id"])
            self._resolved(r, now, pool=pools[r["obs_id"]], meta={"base_vault": pool["base_vault"],
                           "quote_vault": pool["quote_vault"], "fee_quote_bps": PUMPSWAP_FEE_BPS},
                           quote=WSOL_MINT, native=True,
                           graduated_ms=int(bt) * 1000 if isinstance(bt, int) else None,
                           basis="block_time" if isinstance(bt, int) else None)

    def _resolve_rh(self, rows: list[dict[str, Any]], now: int) -> None:
        for r in rows:
            if not r.get("signature"):
                self._retry(r, now, "no_graduation_tx")
        rows = [r for r in rows if r.get("signature")]
        if not rows:
            return
        got = self.send("robinhood", [("eth_getTransactionReceipt", [r["signature"]]) for r in rows], "rh_resolve")
        if got is None:
            for r in rows:
                self._retry(r, now, "rpc_failed")
            return
        self._spend(rows, 1)
        for r, receipt in zip(rows, got, strict=False):
            meta = parse_graduation_receipt(receipt, r["token"])
            if meta is None:
                self._retry(r, now, "receipt_unread" if receipt is None else "no_v4_initialize")
                continue
            quote = str(meta["quote"])
            native = quote in (EVM_ZERO, ROBINHOOD_WETH)
            extra = {k: meta.get(k) for k in ("graduating_buy_quote_in", "graduating_buy_tokens_out") if meta.get(k)}
            self._resolved(r, now, pool=str(meta["pool_id"]), meta=meta, quote=quote, native=native, curve_extra=extra)

    def _resolve_bsc(self, rows: list[dict[str, Any]], now: int) -> None:
        from kaiba.execution import evm_price as ep

        calls = [("eth_call", [{"to": ep.FLAP_PORTAL, "data": ep.SEL_GET_TOKEN_V8_SAFE + _addr_word(r["token"])},
                               "latest"]) for r in rows]
        got = self.send("bsc", calls, "bsc_resolve")
        if got is None:
            for r in rows:
                self._retry(r, now, "rpc_failed")
            return
        self._spend(rows, 1)
        for r, raw in zip(rows, got, strict=False):
            rec = ep.flap_record(_words(raw))
            if rec is None:
                self._retry(r, now, "flap_record_unread")
                continue
            if rec.status != ep.FLAP_STATUS_DEX or not rec.pool or rec.pool == EVM_ZERO:
                self._retry(r, now, f"flap_status:{rec.status}")
                continue
            native = rec.quote_token is None or rec.quote_token == ep.BSC_WRAPPED_NATIVE
            quote = ep.BSC_WRAPPED_NATIVE if rec.quote_token is None else rec.quote_token
            meta = {"pair": rec.pool, "quote": quote, "token_is_token0": r["token"].lower() < quote.lower(),
                    "buy_tax_bps": rec.buy_tax_bps or 0, "sell_tax_bps": rec.sell_tax_bps or 0,
                    "tax_read": rec.buy_tax_bps is not None and rec.sell_tax_bps is not None}
            curve = {"quote_raised_base": str(rec.quote_raised_base), "tokens_sold_atoms": str(rec.tokens_sold_atoms),
                     "graduation_tokens_atoms": str(rec.graduation_tokens_atoms), "token_version": rec.token_version,
                     "buy_tax_bps": rec.buy_tax_bps, "sell_tax_bps": rec.sell_tax_bps, "dex_id": rec.dex_id}
            self._resolved(r, now, pool=rec.pool, meta=meta, quote=quote, native=native, curve_extra=curve)

    # ---- read the pools (entry and marks share it) ---------------------------------------

    def read_states(self, chain: str, rows: list[dict[str, Any]], endpoint: str) -> dict[str, PoolState | None] | None:
        """One request for every row (per ``max_batch``). ``None`` = the request failed."""
        metas = {r["obs_id"]: jload(r.get("pool_meta_json"), {}) or {} for r in rows}
        if chain == "sol":
            accts: list[str] = []
            for r in rows:
                accts += [metas[r["obs_id"]].get("base_vault"), metas[r["obs_id"]].get("quote_vault")]
            got = self.send("sol", [("getMultipleAccounts", [accts, {"encoding": "base64", "commitment": "confirmed",
                                                                    "dataSlice": {"offset": SPL_AMOUNT_OFFSET,
                                                                                  "length": 8}}])], endpoint)
            if got is None:
                return None
            vals = ((got[0] or {}).get("value") if isinstance(got[0], Mapping) else None) or []
            vals = list(vals) + [None] * (len(accts) - len(vals))
            self._spend(rows, 1)  # one batched call, counted once per row it served
            return {r["obs_id"]: sol_state(_sliced_amount(vals[2 * i]), _sliced_amount(vals[2 * i + 1]))
                    for i, r in enumerate(rows)}
        if chain == "robinhood":
            from kaiba.execution import onchain_pool as op

            calls = [("eth_call", [{"to": op.POOL_MANAGER, "data": SEL_EXTSLOAD_N + format(
                int(str(metas[r["obs_id"]]["state_slot"]), 16), "064x") + format(4, "064x")}, "latest"]) for r in rows]
            got = self.send("robinhood", calls, endpoint)
            if got is None:
                return None
            self._spend(rows, 1)
            return {r["obs_id"]: v4_state(raw, metas[r["obs_id"]]) for r, raw in zip(rows, got, strict=False)}
        calls = [("eth_call", [{"to": metas[r["obs_id"]]["pair"], "data": SEL_GET_RESERVES}, "latest"]) for r in rows]
        got = self.send("bsc", calls, endpoint)
        if got is None:
            return None
        self._spend(rows, 1)
        return {r["obs_id"]: v2_state(raw, metas[r["obs_id"]]) for r, raw in zip(rows, got, strict=False)}

    # ---- enter -----------------------------------------------------------------------

    def paper_size(self, chain: str, r: Mapping[str, Any], state: PoolState) -> int:
        if int(r.get("quote_is_native") or 0):
            return int(_per_chain(self.p, "paper_size_native", chain, 0) or 0)
        return state.quote * int(self.p["paper_size_bps_of_quote_reserve"]) // 10_000

    def enter(self, chain: str, now: int) -> None:
        rows = self._due("resolved", chain, now)[:int(self.p["max_batch"])]
        if not rows:
            return
        states = self.read_states(chain, rows, f"{chain}_entry")
        if states is None:
            return  # retried next pass, until the entry window closes
        for r in rows:
            state = states.get(r["obs_id"])
            if state is None:
                continue  # unread this time
            if not state.ok:
                _update(self.conn, r["obs_id"], now, status="unresolved", next_due_ms=None, note="pool_empty_at_entry",
                        entry_reserves_json=jdump(state.as_json()))
                self.counts["unresolved"] += 1
                continue
            size = self.paper_size(chain, r, state)
            tokens = state.buy(size)
            if size <= 0 or tokens <= 0:
                _update(self.conn, r["obs_id"], now, status="unresolved", next_due_ms=None, note="no_tokens_for_size")
                self.counts["unresolved"] += 1
                continue
            _update(self.conn, r["obs_id"], now, status="open", entry_ms=now,
                    entry_offset_ms=now - int(r["graduated_ms"]), entry_quote_in=str(size), entry_tokens=str(tokens),
                    entry_shadow_quote=str(state.into_pool(size)), entry_price=str(state.spot()),
                    entry_reserves_json=jdump(state.as_json()), entry_basis=state.basis,
                    next_due_ms=now + HORIZONS_S[0] * 1000)
            self.counts["entered"] += 1

    # ---- mark ------------------------------------------------------------------------

    def mark(self, chain: str, now: int) -> None:
        rows = self._due("open", chain, now)
        for start in range(0, len(rows), int(self.p["max_batch"])):
            self._mark_batch(chain, rows[start:start + int(self.p["max_batch"])], now)

    def _mark_batch(self, chain: str, rows: list[dict[str, Any]], now: int) -> None:
        live: list[dict[str, Any]] = []
        for r in rows:
            marks = jload(r.get("marks_json"), {}) or {}
            changed = False
            for h in HORIZONS_S:
                due = int(r["entry_ms"]) + h * 1000
                if str(h) not in marks and now > due + grace_ms(h):
                    marks[str(h)] = {"missed": True, "at_ms": now}
                    changed = True
            if changed:
                r = {**r, "marks_json": jdump(marks)}
                self._finish_or_reschedule(r, marks, now)
            if any(str(h) not in marks and now >= int(r["entry_ms"]) + h * 1000 for h in HORIZONS_S):
                live.append(r)
        if not live:
            return
        states = self.read_states(chain, live, f"{chain}_marks")
        if states is None:
            return
        for r in live:
            state = states.get(r["obs_id"])
            if state is None:
                continue
            marks = jload(r.get("marks_json"), {}) or {}
            tokens, shadow = int(r["entry_tokens"]), int(r.get("entry_shadow_quote") or 0)
            held = state.holding(shadow)
            value = held.sell(tokens)
            spot = state.spot()
            mark = {"at_ms": now, "value": str(value), "spot_value": str(int(spot * tokens)) if spot is not None else None,
                    "reserves": state.as_json(), "note": None if state.ok else "pool_empty"}
            for h in HORIZONS_S:
                if str(h) not in marks and now >= int(r["entry_ms"]) + h * 1000:
                    marks[str(h)] = mark
                    self.counts["marks"] += 1
            self._finish_or_reschedule({**r, "marks_json": jdump(marks)}, marks, now)

    def _finish_or_reschedule(self, r: Mapping[str, Any], marks: Mapping[str, Any], now: int) -> None:
        todo = [int(r["entry_ms"]) + h * 1000 for h in HORIZONS_S if str(h) not in marks]
        if todo:
            _update(self.conn, r["obs_id"], now, marks_json=jdump(dict(marks)), next_due_ms=max(min(todo), now + 1))
        else:
            _update(self.conn, r["obs_id"], now, marks_json=jdump(dict(marks)), status="done", next_due_ms=None)

    # ---- one pass ----------------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        now = self.clock()
        self.detect()
        self._expire_entries(now)
        for step in (self.resolve, self.enter, self.mark):
            for chain in self.chains:
                try:
                    step(chain, now)
                except _BudgetSpent:
                    return {**self.counts, "budget": "spent", "declaration": DECLARATION_DIGEST}
        return {**self.counts, "declaration": DECLARATION_DIGEST}


def tick(conn: sqlite3.Connection, params: Mapping[str, Any] | None = None, *, rpc: Rpc | None = None,
         clock: Callable[[], int] = now_ms) -> dict[str, Any]:
    """One observer pass on ``conn`` (an ops job's own connection). Short statements only."""
    return Observer(conn, params, rpc=rpc, clock=clock).tick()


def configured_chains(params: Mapping[str, Any] | None = None) -> list[str]:
    p = _params(params)
    return [c for c in p["chains"] if c in VENUE and rpc_url(c)]


async def run(stop: asyncio.Event | None = None, *, params: Mapping[str, Any] | None = None,
              rpc: Rpc | None = None) -> dict[str, Any]:
    """Shaped for ``kaiba.ingest.runner``: one pass every ``poll_s`` until ``stop``, each on
    a fresh short-lived connection in a worker thread (no read transaction outlives a pass)."""
    from kaiba.core.db import session

    stop = stop or asyncio.Event()
    p = _params(params)
    chains = configured_chains(p) if rpc is None else [c for c in p["chains"] if c in VENUE]
    if not chains:
        return {"idle": "no RPC configured for any graduation chain"}
    p["chains"] = chains
    totals: dict[str, int] = {}

    def _pass() -> dict[str, Any]:
        with session() as conn:
            out = tick(conn, p, rpc=rpc)
            if out.get("detected"):
                with contextlib.suppress(Exception):
                    from kaiba.ingest.runner import note_events

                    note_events(FEED, int(out["detected"]), conn)
            return out

    while not stop.is_set():
        try:
            out = await asyncio.to_thread(_pass)
            for k, v in out.items():
                if isinstance(v, int):
                    totals[k] = totals.get(k, 0) + v
        except Exception as exc:  # noqa: BLE001 - one bad pass never stops the observer
            log.warning("graduation_observer pass failed: %s", type(exc).__name__)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=float(p["poll_s"]))
    return totals


# --------------------------------------------------------------------------------------
# the evaluator
# --------------------------------------------------------------------------------------


def net_return(value: int, quote_in: int, cost_per_leg: Decimal) -> Decimal | None:
    if quote_in <= 0:
        return None
    return Decimal(value) / Decimal(quote_in) * (1 - cost_per_leg) ** 2 - 1


def bootstrap_lower(xs: Sequence[float], *, resamples: int, seed: int, pct: float) -> float | None:
    if not xs:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choices(xs, k=n)) / n for _ in range(int(resamples)))
    return means[min(len(means) - 1, max(0, int(pct * len(means))))]


def _matches(row: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    if row["chain"] != rule["chain"] or row["venue"] != rule["venue"]:
        return False
    return all(row.get(k) == v for k, v in (rule.get("filter") or {}).items())


def rule_returns(rows: Sequence[Mapping[str, Any]], rule: Mapping[str, Any], horizon_s: int) -> dict[str, Any]:
    """Net returns at ``horizon_s`` for the rule's population; missed / pending counted apart."""
    cost = Decimal(str(rule["cost_per_leg"]))
    lo = int(rule["entry_after_graduation_s"]) * 1000
    hi = lo + int(rule["entry_max_late_s"]) * 1000
    out: dict[str, Any] = {"returns": [], "days": [], "missed": 0, "pending": 0}
    for r in rows:
        if not _matches(r, rule) or not int(r.get("sampled") or 0) or r.get("entry_ms") is None:
            continue
        if not lo <= int(r.get("entry_offset_ms") or -1) <= hi:
            continue
        mark = (jload(r.get("marks_json"), {}) or {}).get(str(horizon_s))
        if mark is None:
            out["pending"] += 1
            continue
        if mark.get("missed") or mark.get("value") is None:
            out["missed"] += 1
            continue
        ret = net_return(int(mark["value"]), int(r["entry_quote_in"]), cost)
        if ret is None:
            out["missed"] += 1
            continue
        out["returns"].append(float(ret))
        out["days"].append(utc_day(int(r["graduated_ms"])))
    return out


def evaluate(conn: sqlite3.Connection, *, hypotheses: Sequence[Mapping[str, Any]] = GRADUATION_HYPOTHESES,
             pass_line: Mapping[str, Any] = PASS_LINE, resamples: int | None = None) -> list[dict[str, Any]]:
    """Report every pre-declared rule from the table. Verdict PENDING / INCONCLUSIVE / PASS /
    FAIL at its declared horizon; the other horizons are reported, not judged."""
    chains = sorted({str(h["chain"]) for h in hypotheses})
    rows: list[dict[str, Any]] = []
    for chain in chains:  # one short read per chain
        rows += fetch_all(conn, f"SELECT chain, venue, sampled, quote_is_native, entry_ms, entry_offset_ms, "
                          f"entry_quote_in, marks_json, graduated_ms FROM {TABLE} WHERE chain=? AND entry_ms IS NOT NULL",
                          (chain,))
    n_boot = int(resamples if resamples is not None else pass_line["bootstrap_resamples"])
    report = []
    for rule in hypotheses:
        res = rule_returns(rows, rule, int(rule["horizon_s"]))
        xs = res["returns"]
        n = len(xs)
        mean = sum(xs) / n if n else None
        lower = bootstrap_lower(xs, resamples=n_boot, seed=int(pass_line["bootstrap_seed"]),
                                pct=float(pass_line["lower_percentile"])) if n else None
        days = len(set(res["days"]))
        judged = n + res["missed"]
        missed_share = res["missed"] / judged if judged else 0.0
        if n < int(pass_line["min_n"]) or days < int(pass_line["min_distinct_utc_days"]):
            verdict = "PENDING"
        elif missed_share > float(pass_line["max_missed_share"]):
            verdict = "INCONCLUSIVE"
        elif mean is not None and lower is not None and mean > 0 and lower > 0:
            verdict = "PASS"
        else:
            verdict = "FAIL"
        top3 = sorted(xs, reverse=True)[3:]
        secondary = {}
        for h in rule.get("report_horizons_s") or ():
            if int(h) == int(rule["horizon_s"]):
                continue
            other = rule_returns(rows, rule, int(h))["returns"]
            secondary[str(h)] = {"n": len(other), "mean": sum(other) / len(other) if other else None}
        report.append({
            "rule": rule["rule"], "chain": rule["chain"], "horizon_s": rule["horizon_s"], "verdict": verdict,
            "n": n, "distinct_utc_days": days, "mean": mean, "bootstrap_lower": lower,
            "median": sorted(xs)[n // 2] if n else None, "win_rate": sum(1 for x in xs if x > 0) / n if n else None,
            "mean_without_top3": sum(top3) / len(top3) if top3 else None, "missed": res["missed"],
            "missed_share": missed_share, "pending": res["pending"], "secondary_not_evidence": secondary,
            "declaration": DECLARATION_DIGEST,
        })
    return report


def coverage(conn: sqlite3.Connection, *, since_ms: int | None = None) -> list[dict[str, Any]]:
    """Rows by chain and status, with detection latency and RPC spend: is the observer seeing
    the population it claims to?"""
    since = since_ms if since_ms is not None else now_ms() - 86_400_000
    return fetch_all(conn, f"SELECT chain, status, COUNT(*) AS n, AVG(detect_latency_ms) AS mean_detect_ms, "
                     f"AVG(entry_offset_ms) AS mean_entry_offset_ms, SUM(rpc_calls) AS rpc_calls FROM {TABLE} "
                     "WHERE detected_ms>=? GROUP BY chain, status ORDER BY chain, status", (since,))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m kaiba.learning.graduation_observer evaluate|coverage`` (read-only)."""
    import argparse
    import json

    from kaiba.core.db import session

    parser = argparse.ArgumentParser(prog="graduation_observer")
    parser.add_argument("what", choices=["evaluate", "coverage"])
    args = parser.parse_args(argv)
    with session() as conn:
        out = evaluate(conn) if args.what == "evaluate" else coverage(conn)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "DECLARATION_DIGEST", "GRADUATION_HYPOTHESES", "HORIZONS_S", "Observer", "PASS_LINE", "PoolState",
    "READ_METHODS", "RpcBudget", "coverage", "evaluate", "insert_observation", "parse_graduation_receipt",
    "pumpswap_pool_address", "run", "tick",
)
