"""Leader-exit observer: forward, RECORD-ONLY evidence for "sell when the first signal wallet sells".

Why this exists (lead, 2026-10-05). The wallet study found one exit rule that cut sol losses:
sell when the FIRST of the wallets that triggered an sm-trenches entry sells the token.
sol sm-trenches OLD -8.69% -> -8.10%, NEW -17.29% -> -7.59% (median -32.3% -> -9.6%); with an
on-chain trigger 5 s after their sell, NEW -21.77% -> -9.64%. On robinhood it made things
worse. It was found POST HOC, so before it changes any exit it is recorded forward here and
judged against a pass line written into this file BEFORE any row exists
(:data:`LEADER_EXIT_RULE`, :data:`PASS_LINE`, :data:`DECLARATION_DIGEST` pinned by a test).

The ``gmgn:smartmoney`` feed misses most sol sells, so detection is ON CHAIN:

* **Who.** For every OPEN sol ``sm-trenches`` position (``live`` and ``shadow``), the signal
  wallets that triggered it: ``decision_outcomes.position_id`` (else the position's buy order)
  -> ``decisions.signals_json`` -> ``signals.wallets_json``.
* **What is watched.** Not the wallet (busy wallets push hundreds of unrelated transactions an
  hour) but the wallet's TOKEN ACCOUNT for this mint: its associated token account, derived
  under the mint's own token program (classic or Token-2022, read from the mint account), plus
  any other account ``getTokenAccountsByOwner`` finds when the ATA holds nothing. A
  ``logsSubscribe`` ``mentions`` filter on a token account pushes only the transactions that
  touch THAT wallet's balance of THAT token. The socket is
  :func:`kaiba.ingest.wallet_stream_sol.stream` (one per position), reused unchanged:
  reconnect, backfill-on-reconnect and the lag watchdog come with it.
* **From entry.** On watch start every watched address's signatures are read back to the
  position's ``opened_ms``, so a sale between our fill and the watch starting is not lost.
  Only a sale whose BLOCK time is at or after ``opened_ms`` can trigger; earlier ones are
  recorded with ``post_entry = 0`` and never trigger.
* **A sale.** ``getTransaction`` is the evidence. The wallet's net change of the mint (by
  token-account owner, :func:`kaiba.ingest.wallet_stream_sol.wallet_delta`) is negative AND a
  known swap program ran: ``swap_sell`` when it is a SOL-quoted single-token swap
  (:func:`~kaiba.ingest.wallet_stream_sol.classify`, the leader's own fill price is then
  known), ``swap_sell_unpriced`` for a stable-quoted or multi-token route. A reduction with no
  swap program is ``transfer_out``: recorded, never a trigger. Any size counts (the study's
  rule); the share of the wallet's balance it sold is recorded.
* **Prices.** As soon as the trigger is seen, and again at the sale's block time + 5 s, the
  token's pool is read on chain from this module's own budget: the pump.fun bonding curve
  (exact curve math and fee, ``execution.curve_price``) or, after graduation, the canonical
  PumpSwap pool's vaults (``learning.graduation_observer``'s derivation, 3/3 verified there).
  MEASURED 2026-10-05: 39 of the last 40 sol sm-trenches positions opened AFTER graduation,
  so PumpSwap is the venue that matters. The marks store reserves, not a price, so the
  evaluator prices OUR held quantity through the pool (impact included).
* **Ours.** At close: ``positions`` (cost, proceeds, realized, exit reason) and the position's
  filled sell orders, so the quantity we still held at the trigger and what we had already
  realised come from the order history (``held_basis = orders``), else from a snapshot taken
  when the trigger was seen (``snapshot``).

Per position, ``rule_return`` = (proceeds realised before the trigger + the +5 s pool quote of
the quantity still held x (1 - 3%)) / cost - 1, and ``ladder_return`` = realised / cost. When no
leader sold before our close, the rule never fired and ``rule_return = ladder_return``.

Budget. Every JSON-RPC call goes through :class:`LeaderRpc`: :data:`READ_METHODS` only (any
other method raises before a byte leaves), a hard cap per UTC hour and per UTC day persisted
in ``kv`` (``max_calls_per_hour`` / ``max_calls_per_day``), and then ``providers._http.post_json``
on this module's OWN limiter bucket (``leader-exit-rpc``) at ``Priority.RESEARCH`` -- never
the ``rpc`` bucket protection's EXIT reads spend. Pushes are counted against
``max_pushes_per_hour``. Expected spend (DERIVED, 2026-10-05 rates: ~40 sol sm-trenches
positions a day, 3-7 signal wallets each): ~25-30 calls a position, ~1,200 calls a day (~48k
Alchemy CU), plus socket bytes for a handful of pushes per position.

What it never does: no order, no signal, no decision, no position change, no lane or config
change, no ``gmgn-cli``. It writes ``leader_exit_observations``, ``leader_exit_sells`` and one
``kv`` key, nothing else (``tests/test_leader_exit.py`` pins it with an SQLite authorizer).

Wiring (the lead's call; this module edits neither file):

* ingest runner: ``REGISTRY["leader_exit"] = lambda stop: leader_exit.run(stop=stop)``
  (long-running; :func:`run` idles when no Alchemy Solana endpoint is configured);
* migration ``041_leader_exit_observations.sql`` is applied by ``kaiba.core.db.migrate``
  (and idempotently by :func:`ensure_table` on start);
* optional ``config/risk.yaml`` ``provider_budgets.leader-exit-rpc``: ``{min_interval_ms:
  250, capacity: 10, refill_per_s: 2, max_inflight: 2}``; without it the limiter's default
  (1 request/s) applies, which can make a +5 s mark late when several leaders sell at once;
* read-only report: ``python -m kaiba.learning.leader_exit evaluate|coverage``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import sqlite3
import struct
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, jdump, jload
from kaiba.core.limiter import Priority
from kaiba.core.schemas import digest, now_ms
from kaiba.ingest import wallet_stream_sol as wss
from kaiba.learning import graduation_observer as grad

log = logging.getLogger(__name__)

TABLE = "leader_exit_observations"
SELLS_TABLE = "leader_exit_sells"
MIGRATION_FILE = Path(__file__).resolve().parents[1] / "core" / "migrations" / "041_leader_exit_observations.sql"
FEED = "leader_exit"
BUDGET_KEY = "leader_exit:rpc_budget"
CHAIN = "sol"
LANE = "sm-trenches"
BUCKET = "leader-exit-rpc"

#: The only JSON-RPC methods this module may send. Every one is a read.
READ_METHODS: frozenset[str] = frozenset({
    "getMultipleAccounts", "getSignaturesForAddress", "getTransaction", "getTokenAccountsByOwner",
    "getSlot", "getBlockTime",
})

# --------------------------------------------------------------------------------------
# the pre-declared rule -- WRITTEN 2026-10-05, BEFORE ANY ROW EXISTS
# --------------------------------------------------------------------------------------

#: Changing any field after results exist is changing the test; the work log must say so.
LEADER_EXIT_RULE: dict[str, Any] = {
    "rule": "sell_on_first_leader_sell",
    "chain": CHAIN,
    "lane": LANE,
    "modes": ["live", "shadow"],
    "population": ("closed sol sm-trenches positions observed while open, every signal wallet resolved and "
                   "read back to entry (watch_complete = 1)"),
    "trigger": ("the earliest on-chain swap (swap_sell or swap_sell_unpriced) that reduced any signal wallet's "
                "balance of the token, with block time >= positions.opened_ms; any size"),
    "exit_price": "on-chain pool sell quote of the quantity still held, read at trigger block time + 5 s",
    "on_time_offset_ms": [5_000, 10_000],
    "sell_cost_per_leg": "0.03",
    "entry_leg": "the position's real cost_native (every buy cost already inside)",
    "not_fired": "no trigger before closed_ms -> rule_return = ladder_return (included in the population)",
    "provenance": ("wallet study 2026-10-05, sol sm-trenches: OLD -8.69% -> -8.10%, NEW -17.29% -> -7.59% "
                   "(median -32.3% -> -9.6%); on-chain trigger +5 s NEW -21.77% -> -9.64%; worse on robinhood"),
}
#: Two verdicts, both at ``min_n`` judged positions:
#: SHIP_EXIT_RULE = mean(rule - ladder) > 0 AND the lower end of its 90% bootstrap CI > 0;
#: JUSTIFIES_ENTRIES = mean(rule_return) >= 0 (3% on the simulated leg, real cost on the rest).
#: A fired position whose +5 s mark is missing or late is MISSED (excluded, counted); more
#: than ``max_missed_share`` of the fired positions missed makes both verdicts INCONCLUSIVE.
PASS_LINE: dict[str, Any] = {
    "min_n": 100,
    "ci": "0.90",
    "lower_percentile": "0.05",
    "bootstrap_resamples": 10_000,
    "bootstrap_seed": 20261005,
    "max_missed_share": "0.20",
    "entries_bar_mean_at_least": "0",
}
DECLARED_ON = "2026-10-05"
DECLARATION_DIGEST = digest({"rule": LEADER_EXIT_RULE, "pass": PASS_LINE, "declared_on": DECLARED_ON})[:16]

DEFAULT_PARAMS: dict[str, Any] = {
    "modes": ["live", "shadow"],
    "poll_s": 5.0,
    #: Hard caps on JSON-RPC CALLS, persisted in kv across restarts.
    "max_calls_per_hour": 600,
    "max_calls_per_day": 6000,
    #: Pushes read per hour across every position (a push past it is counted, not read).
    "max_pushes_per_hour": 2000,
    "max_positions": 30,
    "max_leaders": 12,
    #: getTokenAccountsByOwner lookups per position (only for leaders whose ATA holds nothing).
    "max_owner_lookups": 6,
    "backfill_limit": 100,
    #: A position is finalised this long after its close was seen (a late push still counts).
    "close_grace_ms": 15_000,
    "plus5_ms": 5_000,
    "tx_retries": [0.15, 0.3, 0.6, 1.2, 2.4, 4.8],
    "workers": 2,
    #: Socket health. A leader's token account is quiet, so a LAGGING connection (MEASURED
    #: 2026-10-05 on the box: a fresh Alchemy socket pushed 29 transactions p50 26.5 s / p90
    #: 34.8 s after their block; wallet_stream_sol measured the same on 4 of 5 connections)
    #: would go unnoticed until the trigger arrives late. The token's MINT is subscribed on the
    #: same socket as a canary (every trade mentions it); its block->push lag is sampled with
    #: ``getBlockTime`` on each new connection and every ``canary_sample_s``, and a sample over
    #: ``lag_reconnect_ms`` reconnects the socket.
    "canary_sample_s": 120.0,
    "lag_reconnect_ms": 8_000,
    #: Canary pushes per position per hour before the canary is unsubscribed (bytes are billed).
    "max_canary_pushes_per_hour": 3_600,
    "wait_for_slot_s": 3.0,
    "timeout_s": 10.0,
}

# --------------------------------------------------------------------------------------
# chain constants
# --------------------------------------------------------------------------------------

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PUMP_PROGRAM = grad.PUMP_PROGRAM
#: pump.fun ``BondingCurve``: 8-byte discriminator, then u64 virtual_token_reserves,
#: virtual_sol_reserves, real_token_reserves, real_sol_reserves, token_total_supply, bool
#: complete. ``CurveState.build``'s reserved-token invariant validates every decode.
CURVE_MIN_LEN = 49
SPL_AMOUNT_OFFSET = 64


# --------------------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------------------


def _params(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    p = dict(DEFAULT_PARAMS)
    for key, value in (overrides or {}).items():
        if value is not None:
            p[key] = value
    return p


def _int(value: Any) -> int | None:
    return wss._int(value)  # noqa: SLF001 - the tree's one lenient int


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def ata_address(owner: str, mint: str, token_program: str) -> str | None:
    """The associated token account of ``owner`` for ``mint`` under ``token_program``."""
    try:
        seeds = [grad.b58decode(owner), grad.b58decode(token_program), grad.b58decode(mint)]
    except ValueError:
        return None
    if any(len(s) != 32 for s in seeds):
        return None
    pda = grad.find_program_address(seeds, grad.b58decode(ATA_PROGRAM))
    return grad.b58encode(pda) if pda is not None else None


def bonding_curve_address(mint: str) -> str | None:
    try:
        raw = grad.b58decode(mint)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    pda = grad.find_program_address([b"bonding-curve", raw], grad.b58decode(PUMP_PROGRAM))
    return grad.b58encode(pda) if pda is not None else None


def account_data(value: Any) -> bytes | None:
    return grad._account_bytes(value)  # noqa: SLF001 - one base64 account decoder


def token_amount(value: Any) -> int | None:
    """An SPL / Token-2022 account's ``amount`` (u64 at byte 64), ``None`` if not an account."""
    raw = account_data(value)
    if raw is None or len(raw) < SPL_AMOUNT_OFFSET + 8:
        return None
    return struct.unpack("<Q", raw[SPL_AMOUNT_OFFSET:SPL_AMOUNT_OFFSET + 8])[0]


def decode_curve(data: bytes | None) -> dict[str, Any] | None:
    if data is None or len(data) < CURVE_MIN_LEN:
        return None
    vt, vs, rt, rs, supply = struct.unpack("<QQQQQ", data[8:48])
    return {"virtual_token": vt, "virtual_sol": vs, "real_token": rt, "real_sol": rs, "supply": supply,
            "complete": bool(data[48])}


@dataclass(frozen=True, slots=True)
class SellEvidence:
    kind: str                 # swap_sell | swap_sell_unpriced | transfer_out
    atoms: int                # token atoms that left the wallet
    lamports: int | None      # SOL received, fee added back (swap_sell only)
    pre_balance: int
    fraction: Decimal | None
    program: str | None

    @property
    def triggers(self) -> bool:
        return self.kind in ("swap_sell", "swap_sell_unpriced")

    @property
    def lamports_per_atom(self) -> Decimal | None:
        if self.lamports is None or self.atoms <= 0:
            return None
        return Decimal(self.lamports) / Decimal(self.atoms)


def sell_evidence(tx: Mapping[str, Any], wallet: str, mint: str) -> SellEvidence | None:
    """Did ``tx`` reduce ``wallet``'s balance of ``mint``? ``None`` when it did not (a buy,
    an add, no change, a failed transaction). Pure; never raises."""
    try:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None
        d = wss.wallet_delta(tx, wallet)
        delta = d.tokens.get(mint, 0)
        if delta >= 0:
            return None
        pre = 0
        for bal in meta.get("preTokenBalances") or []:
            if isinstance(bal, Mapping) and bal.get("owner") == wallet and bal.get("mint") == mint:
                pre += _int((bal.get("uiTokenAmount") or {}).get("amount")) or 0
        program = wss.swap_program(tx)
        leg, _why = wss.classify(tx, wallet)
        if leg is not None and leg.side == "sell" and leg.token == mint:
            kind, lamports = "swap_sell", int(leg.lamports)
        elif program is not None:
            kind, lamports = "swap_sell_unpriced", None
        else:
            kind, lamports = "transfer_out", None
        atoms = -int(delta)
        fraction = Decimal(atoms) / Decimal(pre) if pre > 0 else None
        return SellEvidence(kind=kind, atoms=atoms, lamports=lamports, pre_balance=pre, fraction=fraction,
                            program=program)
    except Exception as exc:  # noqa: BLE001 - malformed payload is "not evidence"
        log.debug("leader_exit: unreadable transaction (%s)", type(exc).__name__)
        return None


def mark_from_accounts(curve_value: Any, base_value: Any, quote_value: Any, *, at_ms: int) -> dict[str, Any]:
    """A mark from one ``getMultipleAccounts`` answer: the live curve if it exists and is not
    complete, else the PumpSwap vaults, else ``missed``."""
    curve = decode_curve(account_data(curve_value)) if curve_value else None
    if curve is not None and not curve["complete"]:
        state, note = _curve_state(curve)
        if state is not None:
            return {"at_ms": at_ms, "venue": "pump_curve", **{k: str(v) for k, v in curve.items() if k != "complete"}}
        return {"at_ms": at_ms, "missed": f"curve_unreadable:{note}"}
    base, quote = token_amount(base_value), token_amount(quote_value)
    if base and quote:
        return {"at_ms": at_ms, "venue": "pumpswap", "base": str(base), "quote": str(quote),
                "fee_quote_bps": grad.PUMPSWAP_FEE_BPS}
    return {"at_ms": at_ms, "missed": "no_pool_state"}


def _curve_state(curve: Mapping[str, Any]) -> tuple[Any, str]:
    from kaiba.execution.curve_price import CurveState

    return CurveState.build(virtual_sol=int(curve["virtual_sol"]), virtual_token=int(curve["virtual_token"]),
                            real_sol=int(curve["real_sol"]), real_token=int(curve["real_token"]))


def mark_sell_value(mark: Mapping[str, Any] | None, atoms: int) -> int | None:
    """Lamports a sale of ``atoms`` into the marked pool returns, venue fee inside. ``None``
    when the mark has no state."""
    if not mark or mark.get("missed") or atoms < 0:
        return None
    if atoms == 0:
        return 0
    try:
        if mark.get("venue") == "pump_curve":
            from kaiba.execution.curve_price import platform_fee

            state, _ = _curve_state(mark)
            if state is None:
                return None
            gross, _after, _capped = state.sell_exact_in(int(atoms))
            return max(0, gross - platform_fee(gross)[0])
        if mark.get("venue") == "pumpswap":
            pool = grad.PoolState(quote=int(mark["quote"]), base=int(mark["base"]), basis="pumpswap_vaults",
                                  fee_quote_bps=int(mark.get("fee_quote_bps") or grad.PUMPSWAP_FEE_BPS))
            return pool.sell(int(atoms))
    except (KeyError, TypeError, ValueError):
        return None
    return None


def held_from_orders(qty_total: int, sells: Sequence[Mapping[str, Any]], at_ms: int) -> tuple[int, int]:
    """``(atoms still held, lamports realised)`` at ``at_ms`` from filled sell orders."""
    sold = got = 0
    for s in sells:
        if int(s.get("ts_ms") or 0) <= at_ms:
            sold += _int(s.get("amount_in")) or 0
            got += _int(s.get("filled_out")) or 0
    return max(0, int(qty_total) - sold), got


def compute_outcome(obs: Mapping[str, Any], pos: Mapping[str, Any], sells: Sequence[Mapping[str, Any]], *,
                    sell_cost: Decimal | None = None, on_time: Sequence[int] | None = None) -> dict[str, Any]:
    """The per-position verdict columns. Pure; the evaluator's arithmetic lives here."""
    cost_leg = sell_cost if sell_cost is not None else Decimal(LEADER_EXIT_RULE["sell_cost_per_leg"])
    lo, hi = on_time or LEADER_EXIT_RULE["on_time_offset_ms"]
    cost = _int(pos.get("cost_native")) or 0
    proceeds = _int(pos.get("proceeds_native")) or 0
    out: dict[str, Any] = {"fired": None, "ladder_return": None, "rule_return": None, "rule_basis": None,
                           "rule_return_leader_fill": None, "rule_return_detect": None,
                           "held_at_trigger": obs.get("held_at_trigger"),
                           "proceeds_before_trigger": obs.get("proceeds_before_trigger"),
                           "held_basis": obs.get("held_basis")}
    if cost <= 0:
        out["rule_basis"] = "missed:no_cost"
        return out
    ladder = float(Decimal(proceeds) / Decimal(cost) - 1)
    out["ladder_return"] = ladder
    trig = _int(obs.get("trigger_block_ms"))
    closed = _int(pos.get("closed_ms"))
    if trig is None or closed is None or trig >= closed:
        out.update(fired=0, rule_return=ladder, rule_basis="not_fired", rule_return_leader_fill=ladder,
                   rule_return_detect=ladder)
        return out
    out["fired"] = 1
    if sells:
        held, before = held_from_orders(_int(pos.get("qty_total")) or 0, sells, trig)
        out.update(held_at_trigger=str(held), proceeds_before_trigger=str(before), held_basis="orders")
    else:
        held = _int(obs.get("held_at_trigger"))
        before = _int(obs.get("proceeds_before_trigger")) or 0
        if held is None:
            out["rule_basis"] = "missed:no_held_quantity"
            return out
    keep = 1 - cost_leg

    def ret(value: int | Decimal | None) -> float | None:
        if value is None:
            return None
        return float((Decimal(before) + Decimal(value) * keep) / Decimal(cost) - 1)

    lpa = _dec(obs.get("leader_fill_lamports_per_atom"))
    out["rule_return_leader_fill"] = ret(Decimal(held) * lpa if lpa is not None else None)
    detect = jload(obs.get("detect_mark_json"), {}) or {}
    out["rule_return_detect"] = ret(mark_sell_value(detect, held))
    mark = jload(obs.get("plus5_mark_json"), {}) or {}
    if not mark:
        out["rule_basis"] = "missed:no_plus5_mark"
        return out
    if mark.get("missed"):
        out["rule_basis"] = f"missed:{mark['missed']}"
        return out
    offset = (_int(mark.get("at_ms")) or 0) - trig
    if not lo <= offset <= hi:
        out["rule_basis"] = f"missed:late_{offset}ms" if offset > hi else f"missed:early_{offset}ms"
        return out
    value = mark_sell_value(mark, held)
    if value is None:
        out["rule_basis"] = "missed:unpriceable_mark"
        return out
    out["rule_return"] = ret(value)
    out["rule_basis"] = "plus5_pool"
    return out


def bootstrap_interval(xs: Sequence[float], *, resamples: int, seed: int, lower: float) -> tuple[float, float] | None:
    """Percentile bootstrap of the mean: ``(lower, upper)`` at ``lower`` / ``1 - lower``."""
    if not xs:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choices(xs, k=n)) / n for _ in range(int(resamples)))
    lo_i = min(len(means) - 1, max(0, int(lower * len(means))))
    hi_i = min(len(means) - 1, max(0, int((1 - lower) * len(means)) - 1))
    return means[lo_i], means[hi_i]


# --------------------------------------------------------------------------------------
# the budget and the transport
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


class CallBudget:
    """Hard cap on JSON-RPC calls per UTC hour and per UTC day; the counts persist in ``kv``
    (one key) so a restart does not reset them. ``take`` is all-or-nothing."""

    def __init__(self, per_hour: int, per_day: int, *, load: Callable[[], Any] | None = None,
                 save: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.per_hour, self.per_day = max(0, int(per_hour)), max(0, int(per_day))
        saved = load() if load else None
        self.state: dict[str, Any] = dict(saved) if isinstance(saved, dict) else {}
        self.save = save
        self.refused = 0

    def _roll(self, at_ms: int) -> None:
        hour = datetime.fromtimestamp(at_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H")
        if self.state.get("day") != hour[:10]:
            self.state.update(day=hour[:10], d=0)
        if self.state.get("hour") != hour:
            self.state.update(hour=hour, h=0)

    def take(self, calls: int, *, at_ms: int) -> bool:
        self._roll(at_ms)
        h, d = int(self.state.get("h", 0)), int(self.state.get("d", 0))
        if h + calls > self.per_hour or d + calls > self.per_day:
            self.refused += 1
            return False
        self.state.update(h=h + calls, d=d + calls)
        if self.save is not None:
            try:
                self.save(dict(self.state))
            except Exception as exc:  # noqa: BLE001 - the in-memory count still binds
                log.debug("leader_exit: budget not persisted (%s)", type(exc).__name__)
        return True


#: ``(method, params) -> result`` (raises ``wss.RpcFailure`` on any failure).
Send = Callable[[str, list[Any]], Awaitable[Any]]


def default_send(url: str, p: Mapping[str, Any]) -> Send:
    """One JSON-RPC call through ``post_json`` on :data:`BUCKET` at ``Priority.RESEARCH``,
    never cached, in a worker thread. The URL carries the API key: errors are redacted."""
    from kaiba.ingest import alchemy_ws as aws

    def _post(method: str, params: list[Any]) -> Any:
        from kaiba.providers._http import post_json

        got = post_json(BUCKET, f"leader.{method}", url,
                        json_body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        priority=Priority.RESEARCH, ttl_s=0.0, wait_for_slot_s=float(p["wait_for_slot_s"]),
                        timeout_s=float(p["timeout_s"]))
        if not got.ok or not isinstance(got.data, Mapping):
            raise wss.RpcFailure(aws.redact(f"{method}: unavailable: {got.receipt.note or ''}", url)[:200])
        if got.data.get("error") is not None:
            raise wss.RpcFailure(aws.redact(f"{method}: {got.data['error']}", url)[:200])
        return got.data.get("result")

    async def send(method: str, params: list[Any]) -> Any:
        return await asyncio.to_thread(_post, method, params)

    return send


class LeaderRpc:
    """The ONLY way this module reaches the chain: read methods, then the budget, then send."""

    def __init__(self, send: Send, budget: CallBudget, clock: Callable[[], int] = now_ms) -> None:
        self.send, self.budget, self.clock = send, budget, clock
        self.calls: dict[str, int] = {}
        self.failures = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        if method not in READ_METHODS:
            raise PermissionError(f"leader_exit sends reads only, not {method!r}")
        if not self.budget.take(1, at_ms=self.clock()):
            raise wss.RpcFailure(f"{method}: leader_exit call budget spent")
        self.calls[method] = self.calls.get(method, 0) + 1
        try:
            return await self.send(method, params)
        except wss.RpcFailure:
            self.failures += 1
            raise
        except Exception as exc:  # noqa: BLE001 - one failure type for every caller
            self.failures += 1
            raise wss.RpcFailure(f"{method}: {type(exc).__name__}") from None


# --------------------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------------------


def ensure_table(conn: sqlite3.Connection) -> None:
    """Migration 041's DDL (idempotent)."""
    conn.executescript(MIGRATION_FILE.read_text(encoding="utf-8"))


def open_positions(conn: sqlite3.Connection, modes: Sequence[str]) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(modes))
    return fetch_all(conn, "SELECT position_id, token, mode, opened_ms, qty, qty_total, proceeds_native, cost_native "
                     f"FROM positions WHERE closed_ms IS NULL AND chain=? AND lane=? AND mode IN ({marks})",
                     (CHAIN, LANE, *modes))


def leaders_for(conn: sqlite3.Connection, position_id: str, token: str, opened_ms: int) -> dict[str, Any]:
    """The signal wallets behind a position, point in time. Never raises on missing links."""
    did = None
    row = fetch_one(conn, "SELECT decision_id FROM decision_outcomes WHERE position_id=? LIMIT 1", (position_id,))
    if row:
        did = row["decision_id"]
    if not did:
        row = fetch_one(conn, "SELECT o.decision_id FROM position_orders po JOIN orders o ON o.order_id=po.order_id "
                        "WHERE po.position_id=? AND po.side='buy' ORDER BY po.ts_ms LIMIT 1", (position_id,))
        did = row["decision_id"] if row else None
    out: dict[str, Any] = {"decision_id": did, "signal_ids": [], "wallets": [], "note": None}
    if not did:
        out["note"] = "no_entry_decision"
        return out
    d = fetch_one(conn, "SELECT chain, token, ts_ms, signals_json FROM decisions WHERE decision_id=?", (did,))
    if not d or d["chain"] != CHAIN or d["token"] != token or int(d["ts_ms"]) > int(opened_ms) + 1000:
        out["note"] = "entry_decision_not_point_in_time"
        return out
    sids = [str(s) for s in (jload(d["signals_json"], []) or [])]
    wallets: list[str] = []
    for sid in sids:
        s = fetch_one(conn, "SELECT chain, token, created_ms, wallets_json FROM signals WHERE signal_id=?", (sid,))
        if not s or s["chain"] != CHAIN or s["token"] != token or int(s["created_ms"]) > int(opened_ms):
            continue
        out["signal_ids"].append(sid)
        for w in jload(s["wallets_json"], []) or []:
            addr = w.get("wallet") or w.get("address") if isinstance(w, Mapping) else w
            if wss.is_sol_address(addr) and addr not in wallets:
                wallets.append(str(addr).strip())
    out["wallets"] = wallets
    if not wallets:
        out["note"] = "no_signal_wallets"
    return out


def position_row(conn: sqlite3.Connection, position_id: str) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT position_id, closed_ms, qty, qty_total, cost_native, proceeds_native, "
                     "realized_native, exit_reason FROM positions WHERE position_id=?", (position_id,))


def sell_fills(conn: sqlite3.Connection, position_id: str) -> list[dict[str, Any]]:
    return fetch_all(conn, "SELECT po.ts_ms, o.amount_in, o.filled_out FROM position_orders po JOIN orders o "
                     "ON o.order_id=po.order_id WHERE po.position_id=? AND po.side='sell' AND o.state='filled' "
                     "ORDER BY po.ts_ms", (position_id,))


def _update(conn: sqlite3.Connection, position_id: str, at_ms: int, where: str = "", args: Sequence[Any] = (),
            **cols: Any) -> int:
    cols["updated_ms"] = at_ms
    sets = ", ".join(f"{k}=?" for k in cols)
    cur = conn.execute(f"UPDATE {TABLE} SET {sets} WHERE position_id=?{where}", (*cols.values(), position_id, *args))
    return cur.rowcount


# --------------------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------------------


@contextlib.contextmanager
def _session() -> Iterator[sqlite3.Connection]:
    from kaiba.core.db import session

    with session() as conn:
        yield conn


@dataclass
class Watch:
    position_id: str
    token: str
    mode: str
    opened_ms: int
    by_address: dict[str, str] = field(default_factory=dict)      # watched address -> leader wallet
    venue: dict[str, Any] = field(default_factory=dict)
    processed: set[tuple[str, str]] = field(default_factory=set)  # (signature, wallet)
    trigger_sig: str | None = None
    trigger_slot: int | None = None
    trigger_block_ms: int | None = None
    closing_since: int | None = None
    complete: bool = True
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    reconnect: asyncio.Event = field(default_factory=asyncio.Event)
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)
    lag: wss.LagWatch = field(default_factory=wss.LagWatch)
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    calls: int = 0
    canary: str | None = None
    canary_conn: int | None = None
    canary_last_ms: int = 0
    canary_busy: bool = False
    canary_pushes: list[int] = field(default_factory=list)
    lag_samples: list[int] = field(default_factory=list)
    lag_reconnects: int = 0
    muted: set[str] = field(default_factory=set)


class LeaderExitWatcher:
    """Everything is injectable: ``rpc`` (a :class:`LeaderRpc`), ``db`` (a context manager
    factory yielding a connection), ``connect`` (the socket dialer), ``clock`` / ``sleep``."""

    def __init__(self, *, rpc: LeaderRpc, db: Callable[[], Any] = _session, url: str | None = None,
                 params: Mapping[str, Any] | None = None, connect: Callable[[str], Any] | None = None,
                 clock: Callable[[], int] = now_ms, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 stream_enabled: bool = True) -> None:
        self.rpc, self.db, self.url, self.connect = rpc, db, url, connect
        self.p = _params(params)
        self.clock, self.sleep = clock, sleep
        self.stream_enabled = stream_enabled and bool(url)
        self.watches: dict[str, Watch] = {}
        self.queue: asyncio.Queue[tuple[str, wss.Notice]] = asyncio.Queue()
        self._workers: list[asyncio.Task[Any]] = []
        self._starting: dict[str, asyncio.Task[Any]] = {}
        self._tx_cache: OrderedDict[str, Mapping[str, Any] | None] = OrderedDict()
        self._push_window: list[int] = []
        self.counts: dict[str, int] = {"started": 0, "finalized": 0, "unwatchable": 0, "triggers": 0, "sells": 0,
                                       "tx_unread": 0, "after_trigger_skipped": 0, "pushes": 0, "pushes_over_budget": 0, "marks": 0}

    # ---- db plumbing ---------------------------------------------------------------

    async def _db(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        def run() -> Any:
            with self.db() as conn:
                return fn(conn)

        return await asyncio.to_thread(run)

    # ---- the loop ------------------------------------------------------------------

    async def sync(self) -> None:
        """Start watching new open positions; finalise the ones that closed."""
        if not self._workers:
            self._workers = [asyncio.create_task(self._worker()) for _ in range(max(1, int(self.p["workers"])))]
        modes = [str(m) for m in self.p["modes"]]
        rows = await self._db(lambda c: open_positions(c, modes))
        open_ids = {r["position_id"]: r for r in rows}
        now = self.clock()
        for pid, w in list(self.watches.items()):
            if pid in open_ids:
                continue
            if w.closing_since is None:
                w.closing_since = now
            if now - w.closing_since >= int(self.p["close_grace_ms"]) and not self._marks_pending(w, now):
                await self.finalize(pid)
        # rows left 'watching' by a previous process whose position has closed
        stale = await self._db(lambda c: fetch_all(
            c, f"SELECT position_id FROM {TABLE} WHERE status='watching' AND chain=?", (CHAIN,)))
        for r in stale:
            if r["position_id"] not in open_ids and r["position_id"] not in self.watches:
                await self.finalize(r["position_id"])
        for pid, row in open_ids.items():
            if pid in self.watches or pid in self._starting:
                continue
            if len(self.watches) + len(self._starting) >= int(self.p["max_positions"]):
                break
            # A start is RPC-bound (resolve + read back to entry); it must not hold up the
            # close detection of every other position, so it runs as its own task.
            self._starting[pid] = asyncio.create_task(self._start_guarded(row), name=f"start:{pid}")

    async def _start_guarded(self, row: Mapping[str, Any]) -> None:
        pid = str(row["position_id"])
        try:
            await self.start(row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad start never stops the observer
            log.warning("leader_exit: start of %s failed: %s", pid, type(exc).__name__)
        finally:
            self._starting.pop(pid, None)

    async def wait_started(self) -> None:
        """Every start in flight has finished (tests and the box probe)."""
        while self._starting:
            await asyncio.gather(*list(self._starting.values()), return_exceptions=True)

    def _marks_pending(self, w: Watch, now: int) -> bool:
        return w.trigger_block_ms is not None and now < w.trigger_block_ms + int(LEADER_EXIT_RULE["on_time_offset_ms"][1]) + 2000 \
            and any(not t.done() for t in w.tasks if t.get_name().startswith("marks:"))

    async def close(self) -> None:
        for w in self.watches.values():
            w.stop.set()
            for t in w.tasks:
                t.cancel()
        for t in [*self._workers, *self._starting.values()]:
            t.cancel()
        tasks = [t for w in self.watches.values() for t in w.tasks] + self._workers + list(self._starting.values())
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---- start ---------------------------------------------------------------------

    async def start(self, row: Mapping[str, Any]) -> Watch | None:
        pid, token, opened = str(row["position_id"]), str(row["token"]), int(row["opened_ms"])
        now = self.clock()
        info = await self._db(lambda c: leaders_for(c, pid, token, opened))

        def insert(c: sqlite3.Connection) -> dict[str, Any] | None:
            c.execute(f"INSERT OR IGNORE INTO {TABLE} (position_id, chain, token, lane, mode, opened_ms, decision_id, "
                      "signal_ids_json, n_leaders, status, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (pid, CHAIN, token, LANE, str(row["mode"]), opened, info["decision_id"],
                       jdump(info["signal_ids"]), len(info["wallets"]), "watching", now, now))
            return fetch_one(c, f"SELECT * FROM {TABLE} WHERE position_id=?", (pid,))

        obs = await self._db(insert)
        w = Watch(position_id=pid, token=token, mode=str(row["mode"]), opened_ms=opened)
        self.watches[pid] = w
        self.counts["started"] += 1
        if obs and obs.get("trigger_sig"):
            w.trigger_sig, w.trigger_block_ms = obs["trigger_sig"], _int(obs["trigger_block_ms"])
            w.trigger_slot = _int(obs.get("trigger_slot"))
        leaders = list(info["wallets"])[: int(self.p["max_leaders"])]
        if not leaders:
            await self._db(lambda c: _update(c, pid, self.clock(), status="unwatchable", note=info["note"]))
            self.counts["unwatchable"] += 1
            w.complete = False
            return w
        try:
            await self._resolve(w, leaders)
        except wss.RpcFailure as exc:
            w.complete = False
            why = f"resolve_failed:{wss.error_key(exc)}"
            await self._db(lambda c: _update(c, pid, self.clock(), note=why))
        if w.by_address and self.stream_enabled:
            w.tasks.append(asyncio.create_task(self._stream(w), name=f"stream:{pid}"))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(w.subscribed.wait(), timeout=20.0)
        await self._backfill(w)
        await self._db(lambda c: _update(c, pid, self.clock(), watch_complete=int(w.complete),
                                         rpc_calls=w.calls))
        return w

    async def _call(self, w: Watch, method: str, params: list[Any]) -> Any:
        w.calls += 1
        return await self.rpc.call(method, params)

    async def _resolve(self, w: Watch, leaders: list[str]) -> None:
        mint = w.token
        curve = bonding_curve_address(mint)
        pool = grad.pumpswap_pool_address(mint)
        got = await self._call(w, "getMultipleAccounts", [[mint, curve, pool], {"encoding": "base64"}])
        values = (got or {}).get("value") if isinstance(got, Mapping) else None
        values = values if isinstance(values, list) and len(values) == 3 else [None, None, None]
        mint_acc, _curve_acc, pool_acc = values
        program = str(mint_acc.get("owner")) if isinstance(mint_acc, Mapping) else None
        if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            w.complete = False
            program = program or "unknown"
        venue: dict[str, Any] = {"curve": curve, "pool": pool}
        decoded = grad.decode_pumpswap_pool(account_data(pool_acc) or b"") if pool_acc else None
        if decoded and decoded.get("base_mint") == mint:
            venue.update(base_vault=decoded["base_vault"], quote_vault=decoded["quote_vault"])
        w.venue = venue
        w.canary = mint
        atas = {ld: ata_address(ld, mint, program) for ld in leaders} if program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) else {}
        balances: dict[str, int | None] = {}
        ata_list = [a for a in atas.values() if a]
        if ata_list:
            got = await self._call(w, "getMultipleAccounts", [ata_list, {"encoding": "base64"}])
            vals = (got or {}).get("value") if isinstance(got, Mapping) else None
            if isinstance(vals, list) and len(vals) == len(ata_list):
                by_ata = dict(zip(ata_list, vals, strict=True))
                balances = {ld: token_amount(by_ata.get(a)) if a else None for ld, a in atas.items()}
        lookups = 0
        state: list[dict[str, Any]] = []
        for ld in leaders:
            addrs = [atas[ld]] if atas.get(ld) else []
            bal = balances.get(ld)
            if not bal and lookups < int(self.p["max_owner_lookups"]):
                lookups += 1
                try:
                    found = await self._call(w, "getTokenAccountsByOwner", [ld, {"mint": mint}, {"encoding": "base64"}])
                    for acc in ((found or {}).get("value") or []) if isinstance(found, Mapping) else []:
                        pk = acc.get("pubkey") if isinstance(acc, Mapping) else None
                        if wss.is_sol_address(pk) and pk not in addrs:
                            addrs.append(str(pk))
                            bal = (bal or 0) + (token_amount(acc.get("account")) or 0)
                except wss.RpcFailure:
                    w.complete = False
            for a in addrs:
                w.by_address[a] = ld
            state.append({"wallet": ld, "addresses": addrs, "balance_at_watch": None if bal is None else str(bal),
                          "state": "holding" if bal else "empty"})
            if not addrs:
                w.complete = False
        now = self.clock()
        await self._db(lambda c: _update(c, w.position_id, now, leaders_json=jdump(state), token_program=program,
                                         venue_json=jdump(venue), watch_started_ms=now))

    async def _backfill(self, w: Watch) -> None:
        """Every watched address read back to the position's entry; oldest first."""
        floor_s = (w.opened_ms - 2000) // 1000
        found: list[tuple[int, str, str, int | None]] = []
        for addr in list(w.by_address):
            try:
                sigs = await self._call(w, "getSignaturesForAddress",
                                        [addr, {"limit": int(self.p["backfill_limit"]), "commitment": wss.COMMITMENT}])
            except wss.RpcFailure:
                w.complete = False
                continue
            sigs = sigs if isinstance(sigs, list) else []
            if len(sigs) >= int(self.p["backfill_limit"]) and (_int(sigs[-1].get("blockTime")) or 0) >= floor_s:
                w.complete = False  # the page bound cut the walk short of entry
            for s in sigs:
                if not isinstance(s, Mapping) or s.get("err") is not None or not isinstance(s.get("signature"), str):
                    continue
                bt = _int(s.get("blockTime"))
                if bt is not None and bt < floor_s:
                    continue
                found.append((bt or 0, str(s["signature"]), addr, _int(s.get("slot"))))
        for bt, sig, addr, slot in sorted(found):
            await self.handle(w.position_id, wss.Notice(signature=sig, wallet=addr, slot=slot, failed=False, logs=None,
                                                         recv_ms=self.clock(), backfilled=True,
                                                         block_time_ms=bt * 1000 if bt else None))

    # ---- the socket ----------------------------------------------------------------

    async def _stream(self, w: Watch) -> None:
        def status(payload: Mapping[str, Any]) -> None:
            if payload.get("event") == "subscribed":
                w.subscribed.set()
            elif payload.get("event") == "gap_truncated" and payload.get("wallet") != w.canary:
                w.complete = False  # a gap on the canary loses nothing

        addresses = list(w.by_address) + ([w.canary] if w.canary and w.canary not in w.by_address else [])
        try:
            async for n in wss.stream(self.url or "", addresses, rpc=self.rpc.call, stop=w.stop,
                                      connect=self.connect, on_status=status, reconnect=w.reconnect,
                                      muted=w.muted, backfill_page=int(self.p["backfill_limit"]),
                                      backfill_max_pages=1):
                if n.wallet == w.canary:
                    self.on_canary(w, n)
                    continue
                await self.queue.put((w.position_id, n))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead stream marks the watch incomplete
            w.complete = False
            log.warning("leader_exit: stream for %s ended: %s", w.position_id, type(exc).__name__)

    def on_canary(self, w: Watch, n: wss.Notice) -> asyncio.Task[Any] | None:
        """A push of the canary (the mint): sample this connection's lag when due. Never reads
        the transaction; one ``getBlockTime`` per sample."""
        now = self.clock()
        w.canary_pushes = [t for t in w.canary_pushes if t > now - 3_600_000]
        w.canary_pushes.append(now)
        if w.canary and len(w.canary_pushes) > int(self.p["max_canary_pushes_per_hour"]):
            w.muted.add(w.canary)
            return None
        if n.failed or n.backfilled or n.slot is None or w.canary_busy:
            return None
        if w.canary_conn == n.conn and now - w.canary_last_ms < float(self.p["canary_sample_s"]) * 1000:
            return None
        w.canary_busy, w.canary_conn, w.canary_last_ms = True, n.conn, now
        task = asyncio.create_task(self._sample_lag(w, n), name=f"canary:{w.position_id}")
        w.tasks.append(task)
        return task

    async def _sample_lag(self, w: Watch, n: wss.Notice) -> None:
        try:
            bt = _int(await self._call(w, "getBlockTime", [n.slot]))
        except wss.RpcFailure:
            bt = None
        finally:
            w.canary_busy = False
        if not bt:
            return
        lag = n.recv_ms - bt * 1000
        w.lag_samples = [*w.lag_samples[-49:], lag]
        if lag > int(self.p["lag_reconnect_ms"]):
            w.lag_reconnects += 1
            w.canary_conn = None  # sample the replacement connection on its first push
            w.reconnect.set()

    def _push_allowed(self, now: int) -> bool:
        self._push_window = [t for t in self._push_window if t > now - 3_600_000]
        if len(self._push_window) >= int(self.p["max_pushes_per_hour"]):
            return False
        self._push_window.append(now)
        return True

    async def _worker(self) -> None:
        while True:
            pid, n = await self.queue.get()
            try:
                if n.failed:
                    continue
                self.counts["pushes"] += 1
                if not self._push_allowed(self.clock()):
                    self.counts["pushes_over_budget"] += 1
                    w = self.watches.get(pid)
                    if w is not None:
                        w.complete = False
                    continue
                await self.handle(pid, n)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad push never stops the workers
                log.warning("leader_exit: push for %s failed: %s", pid, type(exc).__name__)
            finally:
                self.queue.task_done()

    # ---- one signature --------------------------------------------------------------

    async def _get_tx(self, w: Watch, sig: str) -> Mapping[str, Any] | None:
        if sig in self._tx_cache:
            return self._tx_cache[sig]
        params = [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": wss.COMMITMENT}]
        got: Mapping[str, Any] | None = None
        for delay in (0.0, *[float(x) for x in self.p["tx_retries"]]):
            if delay:
                await self.sleep(delay)
            try:
                ans = await self._call(w, "getTransaction", params)
            except wss.RpcFailure:
                continue
            if isinstance(ans, Mapping) and isinstance(ans.get("meta"), Mapping):
                got = ans
                break
        self._tx_cache[sig] = got
        while len(self._tx_cache) > 2000:
            self._tx_cache.popitem(last=False)
        return got

    @staticmethod
    def _after_trigger(w: Watch, n: wss.Notice) -> bool:
        if w.trigger_block_ms is None:
            return False
        if n.block_time_ms is not None:
            return n.block_time_ms >= w.trigger_block_ms
        if n.slot is not None and w.trigger_slot is not None:
            return n.slot >= w.trigger_slot
        return not n.backfilled  # a live push arrives after the trigger it follows

    async def handle(self, pid: str, n: wss.Notice) -> None:
        w = self.watches.get(pid)
        if w is None or n.failed:
            return
        leader = w.by_address.get(n.wallet)
        if leader is None or (n.signature, leader) in w.processed:
            return
        if self._after_trigger(w, n):
            # Only the EARLIEST post-entry sale is the rule's input: a signature at or after the
            # trigger's block cannot move it, so it is not read (MEASURED 2026-10-05: one runner
            # with 8 active leaders cost 361 calls when every later transaction was read).
            self.counts["after_trigger_skipped"] += 1
            return
        w.processed.add((n.signature, leader))
        tx = await self._get_tx(w, n.signature)
        if tx is None:
            self.counts["tx_unread"] += 1
            w.complete = False
            return
        bt = _int(tx.get("blockTime"))
        block_ms = bt * 1000 if bt else n.block_time_ms
        if not n.backfilled and block_ms:
            verdict = w.lag.add(n.conn, n.recv_ms - block_ms)
            if verdict is not None:
                w.reconnect.set()
        ev = sell_evidence(tx, leader, w.token)
        if ev is None or block_ms is None:
            return
        post = int(block_ms >= w.opened_ms)
        slot = _int(tx.get("slot")) or n.slot
        detect_ms = n.recv_ms

        def write(c: sqlite3.Connection) -> None:
            c.execute(f"INSERT OR IGNORE INTO {SELLS_TABLE} (position_id, wallet, signature, slot, block_ms, detect_ms, "
                      "backfilled, post_entry, kind, atoms, lamports, pre_balance, fraction, program) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (pid, leader, n.signature, slot, block_ms, detect_ms, int(n.backfilled), post, ev.kind,
                       str(ev.atoms), None if ev.lamports is None else str(ev.lamports), str(ev.pre_balance),
                       None if ev.fraction is None else str(ev.fraction), ev.program))

        await self._db(write)
        self.counts["sells"] += 1
        if post and ev.triggers and (w.trigger_block_ms is None or block_ms < w.trigger_block_ms):
            await self._trigger(w, leader, n, slot, block_ms, ev)

    async def _trigger(self, w: Watch, leader: str, n: wss.Notice, slot: int | None, block_ms: int,
                       ev: SellEvidence) -> None:
        pid = w.position_id
        w.trigger_sig, w.trigger_block_ms, w.trigger_slot = n.signature, block_ms, slot
        self.counts["triggers"] += 1
        lpa = ev.lamports_per_atom

        def write(c: sqlite3.Connection) -> None:
            pos = position_row(c, pid) or {}
            _update(c, pid, self.clock(), trigger_wallet=leader, trigger_sig=n.signature, trigger_slot=slot,
                    trigger_block_ms=block_ms, trigger_detect_ms=n.recv_ms, detect_latency_ms=n.recv_ms - block_ms,
                    trigger_backfilled=int(n.backfilled), trigger_kind=ev.kind,
                    trigger_fraction=None if ev.fraction is None else str(ev.fraction),
                    leader_fill_lamports_per_atom=None if lpa is None else str(lpa),
                    held_at_trigger=pos.get("qty"), proceeds_before_trigger=pos.get("proceeds_native"),
                    held_basis="snapshot", detect_mark_json="{}", plus5_mark_json="{}")

        await self._db(write)
        w.tasks.append(asyncio.create_task(self._marks(w, n.signature, block_ms), name=f"marks:{pid}:{n.signature[:8]}"))

    # ---- marks ---------------------------------------------------------------------

    async def read_mark(self, w: Watch) -> dict[str, Any]:
        """One ``getMultipleAccounts`` of the curve and the pool vaults (a second only when the
        pool's vaults are not known yet)."""
        v = w.venue
        try:
            if not v.get("base_vault") and v.get("pool"):
                got = await self._call(w, "getMultipleAccounts", [[v["pool"]], {"encoding": "base64"}])
                vals = (got or {}).get("value") if isinstance(got, Mapping) else None
                pool_acc = vals[0] if isinstance(vals, list) and vals else None
                decoded = grad.decode_pumpswap_pool(account_data(pool_acc) or b"") if pool_acc else None
                if decoded and decoded.get("base_mint") == w.token:
                    v.update(base_vault=decoded["base_vault"], quote_vault=decoded["quote_vault"])
            keys = [v.get("curve"), v.get("base_vault"), v.get("quote_vault")]
            present = [k for k in keys if k]
            if not present:
                return {"at_ms": self.clock(), "missed": "no_venue"}
            got = await self._call(w, "getMultipleAccounts", [present, {"encoding": "base64"}])
            at = self.clock()
            vals = (got or {}).get("value") if isinstance(got, Mapping) else None
            if not isinstance(vals, list) or len(vals) != len(present):
                return {"at_ms": at, "missed": "bad_answer"}
            by_key = dict(zip(present, vals, strict=True))
            self.counts["marks"] += 1
            return mark_from_accounts(by_key.get(keys[0]) if keys[0] else None,
                                      by_key.get(keys[1]) if keys[1] else None,
                                      by_key.get(keys[2]) if keys[2] else None, at_ms=at)
        except wss.RpcFailure as exc:
            return {"at_ms": self.clock(), "missed": f"rpc:{wss.error_key(exc)}"}

    async def _marks(self, w: Watch, sig: str, block_ms: int) -> None:
        pid = w.position_id
        due = block_ms + int(self.p["plus5_ms"])
        detect = await self.read_mark(w)
        if w.trigger_sig != sig:
            return
        await self._db(lambda c: _update(c, pid, self.clock(), " AND trigger_sig=?", (sig,),
                                         detect_mark_json=jdump(detect)))
        if int(detect.get("at_ms") or 0) >= due:
            plus5 = detect  # detected after +5 s: the same read is the +5 s mark (its offset says how late)
        else:
            await self.sleep(max(0.0, (due - self.clock()) / 1000))
            if w.trigger_sig != sig:
                return
            plus5 = await self.read_mark(w)
        plus5 = {**plus5, "offset_ms": int(plus5.get("at_ms") or 0) - block_ms}
        await self._db(lambda c: _update(c, pid, self.clock(), " AND trigger_sig=?", (sig,),
                                         plus5_mark_json=jdump(plus5), rpc_calls=w.calls))

    # ---- finalize ------------------------------------------------------------------

    async def finalize(self, pid: str) -> None:
        w = self.watches.pop(pid, None)
        if w is not None:
            w.stop.set()
            for t in w.tasks:
                if t.get_name().startswith("stream:"):
                    t.cancel()

        def write(c: sqlite3.Connection) -> None:
            obs = fetch_one(c, f"SELECT * FROM {TABLE} WHERE position_id=?", (pid,))
            if not obs:
                return
            pos = position_row(c, pid)
            now = self.clock()
            if not pos:
                _update(c, pid, now, status="done", note="position_missing")
                return
            if pos.get("closed_ms") is None:
                return  # still open (a restart picked it up as stale): leave it watching
            out = compute_outcome(obs, pos, sell_fills(c, pid))
            cols = {k: out[k] for k in ("fired", "ladder_return", "rule_return", "rule_basis",
                                        "rule_return_leader_fill", "rule_return_detect", "held_at_trigger",
                                        "proceeds_before_trigger", "held_basis")}
            if w is not None:
                cols.update(watch_complete=int(w.complete and bool(obs.get("watch_complete", 1))), rpc_calls=w.calls)
                lags = sorted(w.lag_samples)
                cols["socket_json"] = jdump({"lag_samples": len(lags), "lag_p50_ms": lags[len(lags) // 2] if lags else None,
                                             "lag_max_ms": lags[-1] if lags else None,
                                             "lag_reconnects": w.lag_reconnects})
            elif obs.get("status") == "watching":
                cols["watch_complete"] = 0  # not watched to the end by this process
            _update(c, pid, now, closed_ms=pos["closed_ms"], exit_reason=pos.get("exit_reason"),
                    cost_native=pos.get("cost_native"), proceeds_native=pos.get("proceeds_native"),
                    realized_native=pos.get("realized_native"),
                    status="done" if obs.get("status") != "unwatchable" else "unwatchable", **cols)

        await self._db(write)
        self.counts["finalized"] += 1


# --------------------------------------------------------------------------------------
# the runner entry
# --------------------------------------------------------------------------------------


async def run(stop: asyncio.Event | None = None, *, params: Mapping[str, Any] | None = None,
              url: str | None = None, send: Send | None = None, connect: Callable[[str], Any] | None = None,
              db: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Shaped for ``kaiba.ingest.runner``: a sync every ``poll_s`` until ``stop``. Each DB
    touch is its own short connection in a worker thread (no read transaction outlives it)."""
    stop = stop or asyncio.Event()
    p = _params(params)
    url = url or wss.alchemy_url()
    if not url and send is None:
        return {"idle": "no Alchemy Solana endpoint (SOLANA_RPC_URL with /v2/)"}
    dbf = db or _session

    def load() -> Any:
        with dbf() as c:
            ensure_table(c)
            return _kv_get(c, BUDGET_KEY)

    def save(state: dict[str, Any]) -> None:
        with dbf() as c:
            _kv_set(c, BUDGET_KEY, state)

    saved = await asyncio.to_thread(load)
    budget = CallBudget(int(p["max_calls_per_hour"]), int(p["max_calls_per_day"]), load=lambda: saved, save=save)
    rpc = LeaderRpc(send or default_send(str(url), p), budget)
    watcher = LeaderExitWatcher(rpc=rpc, db=dbf, url=url, params=p, connect=connect)
    try:
        while not stop.is_set():
            try:
                await watcher.sync()
            except Exception as exc:  # noqa: BLE001 - one bad sync never stops the observer
                log.warning("leader_exit sync failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=float(p["poll_s"]))
    finally:
        await watcher.close()
    return {**watcher.counts, "calls": dict(rpc.calls), "budget_refused": budget.refused,
            "declaration": DECLARATION_DIGEST}


# --------------------------------------------------------------------------------------
# the evaluator
# --------------------------------------------------------------------------------------


def _pctl(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(q * len(s))))]


def judge(rows: Sequence[Mapping[str, Any]], *, pass_line: Mapping[str, Any] = PASS_LINE,
          resamples: int | None = None) -> dict[str, Any]:
    """The pre-declared verdicts from finished observation rows. Pure."""
    pop = [r for r in rows if r.get("status") == "done" and int(r.get("watch_complete") or 0) == 1
           and r.get("ladder_return") is not None]
    judged = [r for r in pop if r.get("rule_return") is not None]
    fired = [r for r in pop if int(r.get("fired") or 0) == 1]
    missed = [r for r in fired if r.get("rule_return") is None]
    diffs = [float(r["rule_return"]) - float(r["ladder_return"]) for r in judged]
    rule = [float(r["rule_return"]) for r in judged]
    n = len(judged)
    n_boot = int(resamples if resamples is not None else pass_line["bootstrap_resamples"])
    ci = bootstrap_interval(diffs, resamples=n_boot, seed=int(pass_line["bootstrap_seed"]),
                            lower=float(pass_line["lower_percentile"])) if diffs else None
    mean_diff = sum(diffs) / n if n else None
    mean_rule = sum(rule) / n if n else None
    missed_share = len(missed) / len(fired) if fired else 0.0
    if n < int(pass_line["min_n"]):
        ship = entries = "PENDING"
    elif missed_share > float(pass_line["max_missed_share"]):
        ship = entries = "INCONCLUSIVE"
    else:
        ship = "PASS" if mean_diff is not None and ci is not None and mean_diff > 0 and ci[0] > 0 else "FAIL"
        entries = "PASS" if mean_rule is not None and mean_rule >= float(pass_line["entries_bar_mean_at_least"]) else "FAIL"

    def sec(key: str) -> dict[str, Any]:
        xs = [float(r[key]) for r in pop if r.get(key) is not None]
        return {"n": len(xs), "mean": sum(xs) / len(xs) if xs else None}

    strict = [(1 + x) * (1 - float(LEADER_EXIT_RULE["sell_cost_per_leg"])) - 1 for x in rule]
    lat = [float(r["detect_latency_ms"]) for r in fired if r.get("detect_latency_ms") is not None
           and not int(r.get("trigger_backfilled") or 0)]
    by_mode: dict[str, Any] = {}
    for mode in sorted({str(r.get("mode")) for r in judged}):
        d = [float(r["rule_return"]) - float(r["ladder_return"]) for r in judged if r.get("mode") == mode]
        by_mode[mode] = {"n": len(d), "mean_diff": sum(d) / len(d) if d else None}
    return {
        "rule": LEADER_EXIT_RULE["rule"], "declaration": DECLARATION_DIGEST,
        "verdict_ship_exit_rule": ship, "verdict_justifies_entries": entries,
        "n": n, "min_n": int(pass_line["min_n"]), "population": len(pop), "fired": len(fired),
        "missed": len(missed), "missed_share": missed_share,
        "mean_rule_minus_ladder": mean_diff, "ci90_rule_minus_ladder": ci,
        "mean_rule_return": mean_rule,
        "mean_ladder_return": sum(float(r["ladder_return"]) for r in judged) / n if n else None,
        "median_rule_minus_ladder": _pctl(diffs, 0.5),
        "by_mode": by_mode,
        "secondary_not_evidence": {
            "rule_return_leader_fill": sec("rule_return_leader_fill"),
            "rule_return_detect": sec("rule_return_detect"),
            "rule_return_strict_3pct_both_legs": {"n": len(strict), "mean": sum(strict) / len(strict) if strict else None},
        },
        "detect_latency_ms_p50": _pctl(lat, 0.5), "detect_latency_ms_p90": _pctl(lat, 0.9),
        "excluded_incomplete_watch": sum(1 for r in rows if r.get("status") == "done"
                                         and not int(r.get("watch_complete") or 0)),
        "unwatchable": sum(1 for r in rows if r.get("status") == "unwatchable"),
    }


def evaluate(conn: sqlite3.Connection, *, resamples: int | None = None) -> dict[str, Any]:
    rows = fetch_all(conn, f"SELECT position_id, mode, status, watch_complete, fired, ladder_return, rule_return, "
                     "rule_basis, rule_return_leader_fill, rule_return_detect, detect_latency_ms, trigger_backfilled "
                     f"FROM {TABLE} WHERE chain=? AND lane=?", (CHAIN, LANE))
    return judge(rows, resamples=resamples)


def coverage(conn: sqlite3.Connection, *, since_ms: int | None = None) -> list[dict[str, Any]]:
    """Rows by mode and status, with detection latency and RPC spend."""
    since = since_ms if since_ms is not None else now_ms() - 86_400_000
    return fetch_all(conn, f"SELECT mode, status, watch_complete, COUNT(*) AS n, SUM(fired) AS fired, "
                     f"AVG(detect_latency_ms) AS mean_detect_ms, SUM(rpc_calls) AS rpc_calls FROM {TABLE} "
                     "WHERE opened_ms>=? GROUP BY mode, status, watch_complete ORDER BY mode, status", (since,))


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m kaiba.learning.leader_exit evaluate|coverage`` (read-only)."""
    import argparse
    import json

    from kaiba.core.db import session

    parser = argparse.ArgumentParser(prog="leader_exit")
    parser.add_argument("what", choices=["evaluate", "coverage"])
    args = parser.parse_args(argv)
    with session() as conn:
        out = evaluate(conn) if args.what == "evaluate" else coverage(conn)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__: Sequence[str] = (
    "DECLARATION_DIGEST", "LEADER_EXIT_RULE", "PASS_LINE", "READ_METHODS", "CallBudget", "LeaderExitWatcher",
    "LeaderRpc", "ata_address", "bonding_curve_address", "compute_outcome", "coverage", "evaluate", "judge",
    "mark_from_accounts", "mark_sell_value", "run", "sell_evidence",
)
