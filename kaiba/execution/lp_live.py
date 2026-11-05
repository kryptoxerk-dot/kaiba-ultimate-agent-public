"""Live stage of the DLMM fee-scalp bot. It ships off: dry is the default, and dry cannot send.

Owner 2026-11-04: "Get everything going trading lp". `kaiba.execution.lp_paper` decides whether hot
pools pay; this drives one of its arms on the real chain, with the same selection and the same exit
arithmetic, in a wallet of the owner's that holds only what he chooses to risk. GMGN does not do
liquidity, so nothing here imports the executor or the GMGN provider, and this process shares no
limiter, database or RPC host with trading.

**Live needs all of these, checked in `open_gate`.** `KAIBA_LP_LIVE=1`; a key file named in
`KAIBA_LP_KEY_FILE`, private on POSIX, read only to sign and never logged; that key's address equal
to the configured wallet; and that wallet not Kaiba's trading wallet, read from `config/risk.yaml`
at run time. If Kaiba's wallet cannot be read, the two cannot be shown to differ, so it refuses. With
`KAIBA_LP_LIVE` unset the runner holds a `DrySender`. `sendTransaction` appears once in this file, in
`Transport.broadcast`, which raises without a `Gate`, and only `open_gate` can make one. Every other
RPC call goes through an allowlist of reads. Dry builds and simulates each open, and stops there.

**A send is known before it happens.** A Solana signature is computed locally, so the position, the
signature and the signed bytes are written to the store BEFORE the first broadcast. A crash at any
point leaves a row that reconciliation can settle, and there is no window where money moved and the
store knows nothing. A failed broadcast is ambiguous, never failed: the transaction may have reached
a leader. A send settles only three ways: a confirmed status, a confirmed error, or a chain height
past its blockhash's last valid height, after which it can never land. Until then the only retry is
the identical signed bytes, which cannot execute twice, and nothing is rebuilt for that position.
After a send settles, the position account decides what happened, not the send: an expired open whose
position exists is an open.

**Exits ignore the caps.** A cap stops a new position and nothing else. Exits need no price bound, so
they land however far the pool has moved: remove, claim and close in one transaction, then sell what
is left. If that sale fails the token stays in the wallet, recorded, and the sale is retried on a
fresh quote with a wider and bounded minimum, for as long as it takes. A position occupies its slot
until its token is gone, so tokens that will not sell stop new opens by themselves. The one exception
is dust worth under a lamport or so, which no minimum can sell: see `is_dust`. It is kept and recorded,
and it frees the slot, because retrying it forever would stop the bot for nothing.

**On startup** it reads every position the wallet owns from the chain. One the store does not know is
adopted and exited, because nothing about how it was opened can be trusted. Nothing opens until that
scan has succeeded once.

**Marks are the real position's.** `exit_reason` runs on stakes built from the position account:
each bin's constant-sum value pro rata of its shares, and fees from its own checkpoints. `model_view`
sets supply to zero, as `calibrate.py` does, because real shares are already in the bins and the
paper dilution correction would count them twice. P&L is not modelled at all: it is the sum of the
wallet's real SOL and wrapped SOL changes over every transaction of a trip, read from the chain.

    python -m kaiba.execution.lp_live run --arm r20_60 [--profile fast] [--wallet ADDRESS] [--seconds N]
    python -m kaiba.execution.lp_live status
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import math
import os
import secrets
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import base58

from kaiba.execution import lp_tx as tx
from kaiba.execution.lp_model import (
    LAMPORTS,
    POSITION_RENT_LAMPORTS,
    TX_FEE_LAMPORTS,
    Arm,
    Plan,
    PoolState,
    Stake,
    arrays_for,
    bin_price,
    eligible,
    exit_reason,
    plan_bins,
    quote_sell,
    value,
    wants_reopen,
)
from kaiba.execution.lp_paper import (
    FAST,
    MAX_ARRAYS,
    POOLS_PATH,
    PROFILES,
    SELECTOR,
    SOL_MINT,
    WALK_BINS,
    Bot,
    HttpTransport,
    Profile,
    rpc_url,
)
from kaiba.learning.dlmm_state import DISCRIMINATORS, PROGRAM, decode_bins, decode_position

log = logging.getLogger("kaiba.lp_live")

VERSION = "lp-live/1"
LIVE_ENV = "KAIBA_LP_LIVE"
KEY_ENV = "KAIBA_LP_KEY_FILE"
WALLET_ENV = "KAIBA_LP_WALLET"
DIR_ENV = "KAIBA_LP_LIVE_DIR"
POSIX = os.name == "posix"
#: Blocks past a blockhash's last valid height before a send counts as expired. Heights differ
#: slightly between nodes, and calling a send expired too early is the error that loses money.
EXPIRY_MARGIN_BLOCKS = 20
CONFIRMED = ("confirmed", "finalized")
UNRESOLVED = ("signed", "sent", "ambiguous")
FINISHED = ("done", "failed")
#: A compute limit is charged whether used or not, so it comes from the simulation, plus the client's 30%.
UNIT_BUFFER = 1.3
#: Used only when a simulation could not be run at all: an exit must still go out.
EXIT_FALLBACK_UNITS = 800_000
RESCAN_S = 600.0
OPEN_UNREAD_S = 120.0
#: The wallet needs this on top of the ticket: position rent and two token accounts, all refunded at
#: the end, plus the transactions of a round trip.
TOKEN_ACCOUNT_RENT_LAMPORTS = 2_100_000
OPEN_FLOAT_LAMPORTS = POSITION_RENT_LAMPORTS + 2 * TOKEN_ACCOUNT_RENT_LAMPORTS + 3 * TX_FEE_LAMPORTS
#: Below this, a re-open's SOL side is left in the wallet and the position holds token only.
REOPEN_MIN_LAMPORTS = 10_000_000
#: A sale quoted under this cannot clear: the program refuses an output that rounds to nothing.
MIN_SALE_LAMPORTS = 2
#: Arrays a two-sided open's swap may walk. MEASURED 2026-11-04: with 3, real opens reached 1,208 of
#: 1,232 bytes, and a pool needing a bitmap extension would not fit. Half a ticket stays within 140 bins.
OPEN_SWAP_ARRAYS = 2
ZERO_BLOCKHASH = tx.SYSTEM  # 32 zero bytes; a simulation replaces it


class LiveRefused(Exception):
    """A refusal by name. Never carries key material or a file's contents."""


@dataclass(frozen=True)
class Caps:
    """Hard limits on opening. No cap ever stops an exit."""

    ticket_lamports: int = 500_000_000
    max_open: int = 2
    max_priority_lamports_day: int = 100_000_000
    max_daily_loss_lamports: int = 500_000_000
    wallet_floor_lamports: int = 100_000_000


@dataclass(frozen=True)
class Settings:
    wallet: str
    arm: Arm
    profile: Profile = FAST
    caps: Caps = Caps()
    ticket_lamports: int = 500_000_000
    #: Off by default, and rent is refused in `lp_tx` when it is. On a small ticket it is the strategy.
    allow_bin_array_rent: bool = False
    #: The model's `TX_FEE_LAMPORTS` floor, less the base fee.
    priority_lamports: int = TX_FEE_LAMPORTS - tx.BASE_FEE_LAMPORTS
    #: One-sided entries land while the active bin is at most twice this above the planned one.
    up_bins: int = 100
    #: Two-sided entries land within this many bins of where their swap should leave the pool.
    two_sided_bins: int = 8
    open_slip_bps: int = 300
    #: Each failed sale of leftover token moves one rung up. The last rung is the bound.
    sweep_slip_bps: tuple[int, ...] = (100, 300, 1_000, 2_500)
    sweep_backoff_s: float = 10.0
    sweep_backoff_max_s: float = 300.0


# ------------------------------------------------------------------------------------ the live gate
_PROOF = object()


@dataclass(frozen=True)
class Gate:
    """Proof that every live condition held. Only `open_gate` can make one."""

    wallet: str
    sign: Callable[[bytes], bytes] = field(repr=False)
    proof: object = field(repr=False)


def require_gate(gate: Any) -> Gate:
    if not isinstance(gate, Gate) or gate.proof is not _PROOF:
        raise LiveRefused("no_live_gate")
    return gate


def trading_wallet() -> str | None:
    """Kaiba's sol wallet from config at run time. Never hardcoded, and None when it cannot be read."""
    from kaiba.core.config import load_risk
    from kaiba.core.schemas import Chain

    try:
        budget = load_risk().chains.get(Chain.SOL)
    except Exception as exc:  # noqa: BLE001 - an unreadable config refuses; it must not raise past the gate
        log.warning("risk config unreadable: %s", type(exc).__name__)
        return None
    return budget.wallet if budget and budget.wallet else None


def load_signer(path: Path, wallet: str) -> Callable[[bytes], bytes]:
    """A signing function for ``wallet``, from a key file. `from None` on every refusal keeps the
    file's contents out of any traceback."""
    from nacl.signing import SigningKey

    try:
        mode = path.stat().st_mode
    except OSError:
        raise LiveRefused("key_file_unreadable") from None
    if POSIX and mode & 0o077:
        raise LiveRefused("key_file_too_open")
    try:
        text = path.read_text(encoding="utf-8").strip()
        secret = bytes(json.loads(text)) if text.startswith("[") else base58.b58decode(text)
        if len(secret) not in (32, 64):
            raise ValueError
        key = SigningKey(secret[:32])
    except Exception:  # noqa: BLE001 - whatever is wrong with the file, its bytes stay out of the error
        raise LiveRefused("key_file_not_a_solana_key") from None
    if base58.b58encode(bytes(key.verify_key)).decode() != wallet:
        raise LiveRefused("key_does_not_match_wallet")
    return lambda data: key.sign(data).signature


def open_gate(wallet: str, env: Mapping[str, str] | None = None) -> Gate:
    """Every condition for sending, or a named refusal. The key is read last, so a run that cannot
    go live for another reason never touches it."""
    env = os.environ if env is None else env
    if env.get(LIVE_ENV) != "1":
        raise LiveRefused("live_env_not_set")
    path = env.get(KEY_ENV)
    if not path:
        raise LiveRefused("key_file_not_set")
    kaiba = trading_wallet()
    if not kaiba:
        raise LiveRefused("trading_wallet_unknown")
    if wallet == kaiba:
        raise LiveRefused("wallet_is_kaibas")
    return Gate(wallet=wallet, sign=load_signer(Path(path), wallet), proof=_PROOF)


class DrySender:
    can_send = False
    gate = None

    def send(self, raw: bytes) -> str:
        raise LiveRefused("dry_mode_cannot_send")


class LiveSender:
    can_send = True

    def __init__(self, transport: Any, gate: Gate):
        self.gate = require_gate(gate)
        self.t = transport

    def send(self, raw: bytes) -> str:
        return self.t.broadcast(self.gate, raw)


READ_METHODS = frozenset({
    "getMultipleAccounts", "getAccountInfo", "getBalance", "getLatestBlockhash", "getBlockHeight",
    "getSignatureStatuses", "getTransaction", "getProgramAccounts", "simulateTransaction"})


class Transport(HttpTransport):
    """Reads and simulations, by allowlist. Sending is one method that demands a `Gate`."""

    def rpc(self, method: str, params: list[Any]) -> Any:
        if method not in READ_METHODS:
            raise LiveRefused(f"rpc_method_refused:{method}")
        return super().rpc(method, params)

    def broadcast(self, gate: Gate, raw: bytes) -> str:
        require_gate(gate)
        return super().rpc("sendTransaction", [base64.b64encode(raw).decode(), {
            "encoding": "base64", "skipPreflight": True}])


# ------------------------------------------------------------------------------------ pure pieces
def live_range(position: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    """(lo, hi, has_shares) over bins with shares or unclaimed fees: the client clamps to these, so
    the program never loads an array that may not exist. (None, None, False) when nothing is left."""
    held = [b for b in position["bins"] if b["shares"] or any(b["fee_pending"])]
    if not held:
        return None, None, False
    return held[0]["id"], held[-1]["id"], any(b["shares"] for b in held)


def has_rewards_pending(position: dict[str, Any]) -> bool:
    return any(any(b["reward_pending"]) for b in position["bins"])


def stakes_from_chain(position: dict[str, Any], state: PoolState) -> dict[int, Stake]:
    """The real position as `lp_model` stakes. A bin with shares that this read did not include
    raises: a partial position is a wrong mark, and a wrong mark can fire a stop."""
    stakes: dict[int, Stake] = {}
    for held in position["bins"]:
        shares = held["shares"]
        if not shares:
            continue
        row = state.bins.get(held["id"])
        if row is None or not row["supply"]:
            raise ValueError("position_bin_unread")
        x, y = row["amounts"]
        worth = (bin_price(held["id"], state.bin_step) * x + y) * (shares / row["supply"])
        stakes[held["id"]] = Stake(y=int(worth), shares=shares, growth0=tuple(held["fee_checkpoint"]), supply0=0)
    return stakes


def model_view(state: PoolState) -> PoolState:
    """Supply zero switches off `lp_model`'s dilution, which is for a stake that is not really in the
    bin. These shares are. `calibrate.py` does the same; `fees()` then matches the chain's arithmetic."""
    return replace(state, bins={b: {**row, "supply": 0} for b, row in state.bins.items()})


def tx_delta(result: dict[str, Any], wallet: str) -> tuple[int, int]:
    """(lamports, fee) one transaction moved for the wallet: native plus wrapped SOL. A token account
    closing or opening nets out, so the sum over a trip is its real result."""
    meta = result["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in result["transaction"]["message"]["accountKeys"]]
    i = keys.index(wallet)
    delta = int(meta["postBalances"][i]) - int(meta["preBalances"][i])

    def wrapped(rows: Iterable[dict[str, Any]]) -> int:
        return sum(int(r["uiTokenAmount"]["amount"]) for r in rows or ()
                   if r.get("owner") == wallet and r.get("mint") == tx.NATIVE_MINT)

    return delta + wrapped(meta.get("postTokenBalances")) - wrapped(meta.get("preTokenBalances")), int(meta["fee"])


def build_open(arm: Arm, pool: tx.PoolKeys, state: PoolState, owner: str, position: str, *, ticket: int,
               liquid: Iterable[int], settings: Settings, payer: str | None = None) -> list[tx.Instruction]:
    """The opening instructions for an arm on this state, from the same plan paper uses."""
    plan = plan_bins(arm, state, ticket)
    allow = settings.allow_bin_array_rent
    if arm.two_sided:
        half = ticket // 2
        got, left, last = tx.quote_buy(half, state, pool.fee_rate)
        if left > 0 or got < 1:
            raise tx.LpTxError("two_sided_quote_unfilled")
        return tx.open_two_sided(
            pool, owner, position, lo=plan.lo, hi=plan.hi, lamports=ticket, swap_in=half,
            swap_min_out=int(got * (10_000 - settings.open_slip_bps) / 10_000),
            arrays=tx.swap_arrays(state.active, False, liquid, take=OPEN_SWAP_ARRAYS), active=last,
            max_slippage=settings.two_sided_bins, missing=state.missing_arrays, allow_bin_array_rent=allow, payer=payer)
    return tx.open_one_sided(pool, owner, position, lo=plan.lo, hi=plan.hi, lamports=ticket, active=state.active,
                             up_bins=settings.up_bins, missing=state.missing_arrays,
                             allow_bin_array_rent=allow, payer=payer)


def build_reopen(arm: Arm, pool: tx.PoolKeys, state: PoolState, owner: str, position: str, *, token: int, lamports: int,
                 settings: Settings, payer: str | None = None) -> tuple[int, int, list[tx.Instruction]]:
    """(lo, hi, instructions) to re-open around the price with what the last leg held, with no swap.
    ``lamports`` under `REOPEN_MIN_LAMPORTS` stays in the wallet: a leg that fell through its whole
    range holds only token, which is this path's usual case."""
    lo, hi = state.active - arm.reopen_width, state.active + arm.reopen_width
    allow = settings.allow_bin_array_rent
    if lamports < REOPEN_MIN_LAMPORTS:
        return lo, hi, tx.open_token_only(pool, owner, position, lo=lo, hi=hi, amount_x=token, active=state.active,
                                          down_bins=settings.two_sided_bins, missing=state.missing_arrays,
                                          allow_bin_array_rent=allow, payer=payer)
    return lo, hi, tx.open_two_sided(
        pool, owner, position, lo=lo, hi=hi, lamports=lamports, swap_in=0, swap_min_out=0, arrays=(), amount_x=token,
        active=state.active, max_slippage=settings.two_sided_bins, missing=state.missing_arrays,
        allow_bin_array_rent=allow, payer=payer)


def build_exit(pool: tx.PoolKeys, owner: str, address: str, position: dict[str, Any],
               payer: str | None = None) -> list[tx.Instruction]:
    lo, hi, has_shares = live_range(position)
    return tx.exit_position(pool, owner, address, lo=lo, hi=hi, has_shares=has_shares, payer=payer)


def is_dust(balance: int, state: PoolState, rate: float) -> bool:
    """Token no minimum can sell: the pool would take all of it and pay under `MIN_SALE_LAMPORTS`.

    MEASURED 2026-11-04: a sale quoted at 0.39 or 0.16 lamports reverts with 6039 at `swap2.rs:793`
    with a minimum of 1, and every sale quoted above a lamport cleared. Retrying one would hold its
    position's slot forever, the 2026-09 "dust remainder" incident in a new form. An empty book is
    not dust: its quote also pays nothing, but it leaves the balance unfilled, and it can recover.
    """
    out, unfilled = quote_sell(balance, state, rate)
    return unfilled <= 0 and out < MIN_SALE_LAMPORTS


def sweep_quote(balance: int, state: PoolState, rate: float, slip_bps: int) -> tuple[int, int]:
    """(amount in, minimum out) for selling up to ``balance`` down this pool. It sells what the book
    can fill, because a swap the pool cannot complete reverts whole. (0, 0) when nothing can be sold."""
    out, unfilled = quote_sell(balance, state, rate)
    amount = balance
    if unfilled > 0:
        amount = int(balance - math.ceil(unfilled))
        if amount <= 0:
            return 0, 0
        out, _ = quote_sell(amount, state, rate)
    return amount, max(int(out * (10_000 - slip_bps) / 10_000), 1)


# ----------------------------------------------------------------------------------------- the store
SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
  id TEXT PRIMARY KEY, pool TEXT NOT NULL, arm TEXT NOT NULL, status TEXT NOT NULL, adopted INTEGER NOT NULL DEFAULT 0,
  started_ms INTEGER NOT NULL, finished_ms INTEGER, net_lamports INTEGER);
CREATE TABLE IF NOT EXISTS positions (
  address TEXT PRIMARY KEY, trip TEXT NOT NULL, pool TEXT NOT NULL, mint TEXT NOT NULL, arm TEXT NOT NULL,
  status TEXT NOT NULL, ticket INTEGER NOT NULL, lo INTEGER NOT NULL, hi INTEGER NOT NULL, opened_ms INTEGER, above_since_ms INTEGER, reopened INTEGER NOT NULL DEFAULT 0,
  reopen_wanted INTEGER NOT NULL DEFAULT 0, reopen_lamports INTEGER NOT NULL DEFAULT 0,
  exit_reason TEXT, adopted INTEGER NOT NULL DEFAULT 0,
  exit_tries INTEGER NOT NULL DEFAULT 0, sweep_tries INTEGER NOT NULL DEFAULT 0, next_sweep_ms INTEGER NOT NULL DEFAULT 0,
  token_left INTEGER, note TEXT, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sends (
  signature TEXT PRIMARY KEY, position TEXT NOT NULL, trip TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  raw TEXT NOT NULL, last_valid INTEGER NOT NULL, priority INTEGER NOT NULL, created_ms INTEGER NOT NULL,
  settled_ms INTEGER, slot INTEGER, fee INTEGER, delta INTEGER, error TEXT);
CREATE INDEX IF NOT EXISTS idx_sends_status ON sends(status);
"""


class Store:
    """The bot's own small database. It never opens the main one."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.db = sqlite3.connect(root / "lp_live.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def rows(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args)]

    def one(self, sql: str, *args: Any) -> Any:
        row = self.db.execute(sql, args).fetchone()
        return row[0] if row else None

    def insert(self, table: str, **fields: Any) -> None:
        names = ",".join(fields)
        marks = ",".join("?" * len(fields))
        self.db.execute(f"INSERT INTO {table}({names}) VALUES ({marks})", tuple(fields.values()))

    def update(self, table: str, key: str, value: Any, **fields: Any) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", (*fields.values(), value))
        self.db.commit()

    def active(self) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(FINISHED))
        return self.rows(f"SELECT * FROM positions WHERE status NOT IN ({marks}) ORDER BY created_ms", *FINISHED)

    def position(self, address: str) -> dict[str, Any] | None:
        got = self.rows("SELECT * FROM positions WHERE address=?", address)
        return got[0] if got else None

    def unresolved(self, position: str | None = None) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(UNRESOLVED))
        sql = f"SELECT * FROM sends WHERE status IN ({marks})"
        if position is None:
            return self.rows(sql + " ORDER BY created_ms", *UNRESOLVED)
        return self.rows(sql + " AND position=?", *UNRESOLVED, position)

    def priority_since(self, ms: int) -> int:
        return int(self.one("SELECT COALESCE(SUM(COALESCE(fee, priority)), 0) FROM sends"
                            " WHERE created_ms >= ? AND status != 'expired'", ms) or 0)

    def net_so_far(self, trip: str) -> tuple[int, bool]:
        """(sum of real changes, every landed transaction read). Unknown is never treated as zero."""
        got = self.rows("SELECT COALESCE(SUM(delta), 0) AS net, SUM(status IN ('landed', 'reverted') AND delta IS NULL)"
                        " AS unknown FROM sends WHERE trip=?", trip)[0]
        return int(got["net"]), not got["unknown"]

    def realized_loss_since(self, ms: int) -> tuple[int, bool]:
        """(lamports lost, every trip's figure known). A trip whose token has failed to sell counts
        with that token at zero, because it may never sell. One not yet tried does not: a leg that
        ended in token is not a loss until something says so, and it would refuse its own re-open."""
        loss, known = 0, True
        for trip in self.rows("SELECT id, status, finished_ms FROM trips WHERE adopted=0 AND"
                              " (finished_ms >= ? OR (status='running' AND id IN"
                              " (SELECT trip FROM positions WHERE status='sweeping' AND sweep_tries > 0)))", ms):
            net, whole = self.net_so_far(trip["id"])
            known = known and whole
            loss -= net
        return loss, known


class Reader(Bot):
    """`lp_paper`'s reads, unchanged, on this transport. `Bot.__init__` is skipped on purpose: it
    opens paper's database, and none of paper's write path may exist here."""

    def __init__(self, transport: Any, now: Callable[[], float]):
        self.t = transport
        self.now = now
        self.keys: dict[tuple[str, int], str] = {}
        self.mints: dict[str, str | None] = {}


# ---------------------------------------------------------------------------------------- the runner
class Runner:
    def __init__(self, transport: Any, store: Store, settings: Settings, *, sender: Any = None,
                 now: Callable[[], float] = time.time, keygen: Callable[[], Any] | None = None):
        self.t = transport
        self.store = store
        self.s = settings
        self.sender = sender or DrySender()
        self.now = now
        self.keygen = keygen or self._default_keygen
        self.reader = Reader(transport, now)
        self.counts: Counter[str] = Counter()
        self.last_reasons: dict[str, int] = {}
        self.plans: dict[str, Plan] = {}
        self.active_seen: dict[str, int] = {}
        self.chain_scan_ok = False
        self.last_scan = self.last_select = self.last_status = float("-inf")
        self.dry_seen: dict[str, float] = {}
        if settings.ticket_lamports > settings.caps.ticket_lamports:
            raise LiveRefused("ticket_above_cap")
        if self.sender.can_send and require_gate(self.sender.gate).wallet != settings.wallet:
            raise LiveRefused("gate_wallet_mismatch")

    @staticmethod
    def _default_keygen() -> Any:
        from nacl.signing import SigningKey

        return SigningKey.generate()

    def now_ms(self) -> int:
        return int(self.now() * 1000)

    def day_start_ms(self) -> int:
        return self.now_ms() // 86_400_000 * 86_400_000

    # ------------------------------------------------------------------------------------- reads
    def read_keys(self, address: str) -> tuple[tx.PoolKeys, frozenset[int]]:
        """The pool's accounts and the bin arrays that hold liquidity, with the extension if any."""
        ext_key = tx.bitmap_extension_address(address)
        res = self.t.rpc("getMultipleAccounts", [[address, ext_key], {"encoding": "base64", "commitment": "confirmed"}])
        pool_acct, ext_acct = res["value"]
        keys = tx.decode_keys(pool_acct, address, extension=ext_key if ext_acct else None)
        liquid = tx.liquid_arrays(keys.bitmap)
        return keys, liquid | tx.liquid_arrays_extension(ext_acct) if ext_acct else liquid

    def read_position(self, address: str, min_slot: int | None = None) -> dict[str, Any] | None:
        options: dict[str, Any] = {"encoding": "base64", "commitment": "confirmed"}
        if min_slot:
            options["minContextSlot"] = min_slot
        value = self.t.rpc("getMultipleAccounts", [[address], options])["value"][0]
        return decode_position(value) if value else None

    def read_managed(self, row: dict[str, Any]) -> tuple[dict[str, Any] | None, PoolState]:
        """Position and pool from one slot, with the position's arrays and the ones around the price."""
        address = row["address"]
        last = self.active_seen.get(address, (row["lo"] + row["hi"]) // 2)
        indices = sorted(set(arrays_for(row["lo"], row["hi"])) | set(arrays_for(last - WALK_BINS, last + 2)))[:MAX_ARRAYS]
        keys = [row["pool"], address, *(tx.bin_array_key(row["pool"], i) for i in indices)]
        res = self.t.rpc("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "confirmed"}])
        values = res["value"]
        head = tx.decode_keys(values[0], row["pool"])
        bins: dict[int, dict[str, Any]] = {}
        missing = set()
        for index, account in zip(indices, values[2:], strict=True):
            if account is None:
                missing.add(index)
                continue
            bins.update({b["id"]: b for b in decode_bins(account)["bins"]})
        self.active_seen[address] = head.active
        state = PoolState(slot=int(res["context"]["slot"]), at_ms=self.now_ms(), active=head.active,
                          bin_step=head.bin_step, bins=bins, missing_arrays=frozenset(missing))
        return (decode_position(values[1]) if values[1] else None), state

    def token_balances(self, keys: tx.PoolKeys) -> tuple[int | None, int | None]:
        """(token, wrapped SOL) in the wallet's accounts. None means the account does not exist."""
        accounts = [tx.ata_address(self.s.wallet, keys.mint_x, keys.program_x),
                    tx.ata_address(self.s.wallet, keys.mint_y, keys.program_y)]
        res = self.t.rpc("getMultipleAccounts", [accounts, {"encoding": "jsonParsed", "commitment": "confirmed"}])

        def amount(value: Any) -> int | None:
            return int(value["data"]["parsed"]["info"]["tokenAmount"]["amount"]) if value else None

        return amount(res["value"][0]), amount(res["value"][1])

    def simulate(self, instructions: list[tx.Instruction]) -> dict[str, Any]:
        message = tx.compile_message(self.s.wallet, tx.finish(instructions, tx.SIMULATION_UNITS, 0), ZERO_BLOCKHASH)
        res = self.t.rpc("simulateTransaction", [base64.b64encode(tx.serialize(message)).decode(), {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed"}])
        return res["value"]

    # ------------------------------------------------------------------------------------ the cap
    def may_open(self, *, counting_slots: bool = True) -> str | None:
        """Why no new position may open now, or None. Everything unknown refuses."""
        caps = self.s.caps
        if not self.chain_scan_ok:
            return "chain_scan_pending"
        if self.store.unresolved():
            return "send_unresolved"
        if counting_slots and len(self.store.active()) >= min(caps.max_open, self.s.profile.max_open):
            return "max_open"
        day = self.day_start_ms()
        if self.store.priority_since(day) + self.s.priority_lamports > caps.max_priority_lamports_day:
            return "priority_cap"
        loss, known = self.store.realized_loss_since(day)
        if not known:
            return "pnl_unknown"
        if loss >= caps.max_daily_loss_lamports:
            return "daily_loss_stop"
        balance = int(self.t.rpc("getBalance", [self.s.wallet, {"commitment": "confirmed"}])["value"])
        if balance - self.s.ticket_lamports - OPEN_FLOAT_LAMPORTS < caps.wallet_floor_lamports:
            return "wallet_floor"
        return None

    # --------------------------------------------------------------------------------- the send path
    def transmit(self, kind: str, trip: str, position: str, instructions: list[tx.Instruction], *, units: int,
                 priority: int, signers: dict[str, Any] | None = None, before: Callable[[], None] | None = None) -> str:
        """Sign, record, then broadcast. The record is committed first: a crash after this line has
        a row to reconcile, and a broadcast that raises is ambiguous, never failed."""
        if not self.sender.can_send:
            raise LiveRefused("dry_mode_cannot_send")
        latest = self.t.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
        message = tx.compile_message(self.s.wallet, tx.finish(instructions, units, priority), latest["blockhash"])
        sign_by = {self.s.wallet: self.sender.gate.sign, **{k: v.sign for k, v in (signers or {}).items()}}
        signatures = []
        for key in message.signers:
            result = sign_by[key](message.raw)
            signatures.append(result.signature if hasattr(result, "signature") else result)
        raw = tx.serialize(message, signatures)
        signature = base58.b58encode(signatures[0]).decode()
        if before:
            before()
        self.store.insert("sends", signature=signature, position=position, trip=trip, kind=kind, status="signed",
                          raw=base64.b64encode(raw).decode(), last_valid=int(latest["lastValidBlockHeight"]),
                          priority=priority + tx.BASE_FEE_LAMPORTS * len(message.signers), created_ms=self.now_ms())
        self.store.db.commit()
        try:
            self.sender.send(raw)
        except Exception as exc:  # noqa: BLE001 - it may have reached a leader; only the chain can say
            self.store.update("sends", "signature", signature, status="ambiguous", error=type(exc).__name__)
            self.counts["send_ambiguous"] += 1
            log.warning("%s send ambiguous: %s", kind, type(exc).__name__)
            return signature
        self.store.update("sends", "signature", signature, status="sent")
        self.counts[f"sent_{kind}"] += 1
        return signature

    def reconcile(self, send: dict[str, Any]) -> None:
        """Settle one send from the chain, or leave it unresolved. Only three things settle it."""
        signature = send["signature"]
        got = self.t.rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])["value"][0]
        slot = None
        if got and got.get("confirmationStatus") in CONFIRMED:
            outcome, slot = ("reverted" if got.get("err") else "landed"), got.get("slot")
        else:
            height = int(self.t.rpc("getBlockHeight", [{"commitment": "confirmed"}]))
            if height <= send["last_valid"] + EXPIRY_MARGIN_BLOCKS:
                self.rebroadcast(send)
                return
            outcome = "expired"
        position = self.read_position(send["position"], slot) if send["kind"] != "sweep" else None
        if outcome == "expired" and send["kind"] == "open" and position:
            outcome = "landed"  # The status was missed; the position account is the answer.
        error = str(got.get("err"))[:160] if got and got.get("err") else None
        self.store.update("sends", "signature", signature, status=outcome, settled_ms=self.now_ms(), slot=slot,
                          error=error, delta=0 if outcome == "expired" else None, fee=0 if outcome == "expired" else None)
        self.counts[f"settled_{outcome}"] += 1
        self.apply(send, outcome, position)

    def rebroadcast(self, send: dict[str, Any]) -> None:
        """The same signed bytes: same signature, so it cannot execute twice."""
        if not self.sender.can_send:
            return
        try:
            self.sender.send(base64.b64decode(send["raw"]))
        except Exception as exc:  # noqa: BLE001 - it stays unresolved either way
            self.counts["rebroadcast_failed"] += 1
            log.warning("rebroadcast failed: %s", type(exc).__name__)

    def apply(self, send: dict[str, Any], outcome: str, position: dict[str, Any] | None) -> None:
        """What a settled send changed, decided by the position account where there is one."""
        row = self.store.position(send["position"])
        if row is None:
            return
        address = row["address"]
        if send["kind"] == "open":
            if position:
                self.store.update("positions", "address", address, status="open", opened_ms=self.now_ms(),
                                  updated_ms=self.now_ms())
            elif outcome == "landed":
                # It landed and the position is not there. Never assumed either way: held and re-read.
                self.store.update("positions", "address", address, note="landed_position_unread", updated_ms=self.now_ms())
                log.error("open landed but position unread: %s", address)
            elif row["reopened"]:
                # The re-open never happened, so the last leg's token is in the wallet: sell it.
                self.store.update("positions", "address", address, status="sweeping", reopen_wanted=0,
                                  note=f"reopen_{outcome}", updated_ms=self.now_ms())
            else:
                self.store.update("positions", "address", address, status="failed", note=f"open_{outcome}",
                                  updated_ms=self.now_ms())
                self.close_trip(row["trip"], "failed")
        elif send["kind"] == "exit":
            if position:
                self.store.update("positions", "address", address, status="exiting", exit_tries=row["exit_tries"] + 1,
                                  note=f"exit_{outcome}", updated_ms=self.now_ms())
            else:
                self.store.update("positions", "address", address, status="sweeping", updated_ms=self.now_ms())
        elif send["kind"] == "sweep" and outcome != "landed":
            self.sweep_failed(row, outcome)

    def sweep_failed(self, row: dict[str, Any], why: str) -> None:
        """Another try later, one rung wider, forever. The token is kept and reported, never written off."""
        tries = row["sweep_tries"] + 1
        wait = min(self.s.sweep_backoff_s * 2 ** min(tries, 8), self.s.sweep_backoff_max_s)
        self.store.update("positions", "address", row["address"], sweep_tries=tries, note=f"sweep_{why}",
                          next_sweep_ms=self.now_ms() + int(wait * 1000), updated_ms=self.now_ms())
        if tries >= len(self.s.sweep_slip_bps):
            self.counts["sweep_stuck"] += 1
            log.error("token stuck after %d tries: mint %s, last %s", tries, row["mint"], why)

    def close_trip(self, trip: str, status: str) -> None:
        self.store.update("trips", "id", trip, status=status, finished_ms=self.now_ms())

    def settle_deltas(self) -> None:
        """Read what each settled transaction really did. Until read, opening stays refused."""
        for send in self.store.rows("SELECT signature FROM sends WHERE status IN ('landed', 'reverted') AND delta IS NULL"):
            res = self.t.rpc("getTransaction", [send["signature"], {
                "encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
            if res:
                delta, fee = tx_delta(res, self.s.wallet)
                self.store.update("sends", "signature", send["signature"], delta=delta, fee=fee)
        for trip in self.store.rows("SELECT id FROM trips WHERE finished_ms IS NOT NULL AND net_lamports IS NULL"):
            net, known = self.store.net_so_far(trip["id"])
            if known:
                self.store.update("trips", "id", trip["id"], net_lamports=net)

    # ------------------------------------------------------------------------------------ recovery
    def scan_chain(self) -> None:
        """Every position this wallet owns. One the store has never seen, or wrote off, is exited."""
        self.last_scan = self.now()
        found = self.t.rpc("getProgramAccounts", [PROGRAM, {
            "encoding": "base64", "commitment": "confirmed", "dataSlice": {"offset": 0, "length": 0},
            "filters": [{"memcmp": {"offset": 0, "bytes": base58.b58encode(DISCRIMINATORS["position"]).decode()}},
                        {"memcmp": {"offset": 40, "bytes": self.s.wallet}}]}])
        for item in found:
            row = self.store.position(item["pubkey"])
            if row is None:
                self.adopt(item["pubkey"])
            elif row["status"] == "failed":
                self.revive(row)
        self.chain_scan_ok = True

    def revive(self, row: dict[str, Any]) -> None:
        """An open written off that exists after all: its status was missed and a node that had not caught
        up said no position. Left as failed, no rescan would look at it again and it would never exit."""
        if self.read_position(row["address"]) is None:
            return
        at = self.now_ms()
        self.store.update("trips", "id", row["trip"], status="running", adopted=1, finished_ms=None)
        self.store.update("positions", "address", row["address"], status="open", adopted=1, opened_ms=at,
                          note="revived_from_chain", updated_ms=at)
        self.counts["revived"] += 1
        log.error("a written-off open exists on chain: %s", row["address"])

    def adopt(self, address: str) -> None:
        position = self.read_position(address)
        if position is None:
            return
        keys, _ = self.read_keys(position["pool"])
        trip = secrets.token_hex(8)
        at = self.now_ms()
        self.store.insert("trips", id=trip, pool=position["pool"], arm=self.s.arm.name, status="running", adopted=1, started_ms=at)
        self.store.insert("positions", address=address, trip=trip, pool=position["pool"], mint=keys.mint_x, arm=self.s.arm.name,
                          status="open", ticket=self.s.ticket_lamports, lo=position["lower_bin"], hi=position["upper_bin"],
                          opened_ms=at, adopted=1, created_ms=at, updated_ms=at)
        self.store.db.commit()
        self.counts["adopted"] += 1
        log.error("adopted a position the store did not know: %s", address)

    # ---------------------------------------------------------------------------------- management
    def plan_for(self, row: dict[str, Any]) -> Plan:
        """The arm's plan for a position, restored from the store after a restart."""
        plan = self.plans.get(row["address"])
        if plan is None:
            plan = Plan(arm=self.s.arm, ticket=row["ticket"], lo=row["lo"], hi=row["hi"], opened_ms=row["opened_ms"] or 0,
                        above_since_ms=row["above_since_ms"], reopened=bool(row["reopened"]))
            self.plans[row["address"]] = plan
        return plan

    def manage(self, row: dict[str, Any]) -> None:
        """One step for one position. A position with an unresolved send waits: anything built now
        could double what that send already did."""
        if self.store.unresolved(row["address"]):
            self.counts["waiting_on_send"] += 1
            return
        status = row["status"]
        if status == "opening":
            self.check_opening(row)
        elif status == "open":
            self.tick_open(row)
        elif status == "exiting":
            self.try_exit(row)
        elif status == "sweeping":
            self.try_sweep(row)

    def check_opening(self, row: dict[str, Any]) -> None:
        """An open whose send landed but whose position was not readable yet. A node can lag, so it
        gets `OPEN_UNREAD_S`. After that the money is somewhere else, and the sweep finds what is left."""
        position = self.read_position(row["address"])
        if position:
            self.store.update("positions", "address", row["address"], status="open", opened_ms=self.now_ms(),
                              note=None, updated_ms=self.now_ms())
        elif self.now_ms() - row["updated_ms"] > OPEN_UNREAD_S * 1000:
            self.store.update("positions", "address", row["address"], status="sweeping", note="landed_never_readable",
                              updated_ms=self.now_ms())
            log.error("open landed and no position ever appeared: %s", row["address"])

    def tick_open(self, row: dict[str, Any]) -> None:
        position, state = self.read_managed(row)
        if position is None:
            # Closed by something other than this bot's own send. Whatever it left, sell it.
            self.store.update("positions", "address", row["address"], status="sweeping", exit_reason="vanished",
                              note="position_gone_unexpectedly", updated_ms=self.now_ms())
            log.error("position vanished: %s", row["address"])
            return
        plan = self.plan_for(row)
        plan.stakes = stakes_from_chain(position, state)
        view = model_view(state)
        reason = "adopted" if row["adopted"] else exit_reason(plan, view)
        if plan.above_since_ms != row["above_since_ms"]:
            self.store.update("positions", "address", row["address"], above_since_ms=plan.above_since_ms)
        if not reason:
            return
        again = bool(not row["adopted"] and wants_reopen(plan, reason, view))
        # What the leg holds in SOL now. Not the exit's balance change: that includes the position's
        # refunded rent, which would be wrapped and deposited as if it were proceeds.
        worth = value(plan, view)
        self.store.update("positions", "address", row["address"], status="exiting", exit_reason=reason,
                          reopen_wanted=int(again), reopen_lamports=int(worth.y + worth.fee_y), updated_ms=self.now_ms())
        self.try_exit({**row, "status": "exiting", "exit_reason": reason}, position)

    def try_exit(self, row: dict[str, Any], position: dict[str, Any] | None = None) -> None:
        """Remove, claim, close. No cap is consulted anywhere on this path."""
        if position is None:
            position, _ = self.read_managed(row)
        if position is None:
            self.store.update("positions", "address", row["address"], status="sweeping", updated_ms=self.now_ms())
            return
        if has_rewards_pending(position):
            log.warning("position has rewards pending, close will not complete: %s", row["address"])
        keys, _ = self.read_keys(row["pool"])
        instructions = build_exit(keys, self.s.wallet, row["address"], position)
        units = EXIT_FALLBACK_UNITS
        try:
            sim = self.simulate(instructions)
        except Exception as exc:  # noqa: BLE001 - no simulator is no reason to stay in a position
            self.counts["exit_unsimulated"] += 1
            log.warning("exit not simulated: %s", type(exc).__name__)
        else:
            if sim.get("err"):
                self.counts["exit_sim_failed"] += 1
                self.store.update("positions", "address", row["address"], note=f"exit_sim:{str(sim['err'])[:120]}",
                                  exit_tries=row["exit_tries"] + 1, updated_ms=self.now_ms())
                log.warning("exit simulation failed: %s", str(sim["err"])[:160])
                return
            units = self.units_for(sim)
        if not self.sender.can_send:
            self.counts["dry_exit_ok"] += 1
            return
        priority = self.s.priority_lamports * 2 ** min(row["exit_tries"], 3)
        self.transmit("exit", row["trip"], row["address"], instructions, units=units, priority=priority)

    @staticmethod
    def units_for(sim: dict[str, Any]) -> int:
        return min(math.ceil(int(sim.get("unitsConsumed") or EXIT_FALLBACK_UNITS) * UNIT_BUFFER), tx.SIMULATION_UNITS)

    def try_sweep(self, row: dict[str, Any]) -> None:
        """Sell what is left, unwrap, and recover the token account's rent, then finish the trip."""
        if row["next_sweep_ms"] > self.now_ms():
            return
        keys, liquid = self.read_keys(row["pool"])
        token, wrapped = self.token_balances(keys)
        self.store.update("positions", "address", row["address"], token_left=token or 0, updated_ms=self.now_ms())
        if token is None and wrapped is None:
            self.finish(row)
            return
        if row["reopen_wanted"] and token and self.try_reopen(row, keys, token):
            return
        amount_in, min_out, arrays, dust = 0, 0, [], False
        if token:
            ladder = self.s.sweep_slip_bps
            slip = ladder[min(row["sweep_tries"], len(ladder) - 1)]
            state = self.reader.read_state(row["pool"], keys.active - 3 * 70, keys.active + 2)
            dust = is_dust(token, state, keys.fee_rate)
            if not dust:
                amount_in, min_out = sweep_quote(token, state, keys.fee_rate, slip)
                arrays = tx.swap_arrays(state.active, True, liquid)
                if amount_in <= 0 or not arrays:
                    self.sweep_failed(row, "no_bid_in_pool")
                    return
        if dust and wrapped is None:
            # Kept, not burned, and recorded. It cannot be sold, so its account's rent stays locked.
            self.finish(row, token_left=token, note="dust_left")
            log.warning("unsellable dust left in the wallet: mint %s, %d atoms", row["mint"], token)
            return
        instructions = tx.sweep(keys, self.s.wallet, amount_in=amount_in, min_out=min_out, arrays=arrays,
                                close_token=token is not None and amount_in == token, unwrap=wrapped is not None)
        try:
            sim = self.simulate(instructions)
        except Exception as exc:  # noqa: BLE001 - try again later, on a fresh quote
            self.counts["sweep_unsimulated"] += 1
            log.warning("sweep not simulated: %s", type(exc).__name__)
            self.sweep_failed(row, "unsimulated")
            return
        if sim.get("err"):
            self.sweep_failed(row, f"sim:{str(sim['err'])[:100]}")
            return
        if not self.sender.can_send:
            self.counts["dry_sweep_ok"] += 1
            return
        self.transmit("sweep", row["trip"], row["address"], instructions, units=self.units_for(sim),
                      priority=self.s.priority_lamports)

    def try_reopen(self, row: dict[str, Any], keys: tx.PoolKeys, token: int) -> bool:
        """Re-open around the price with what the last leg left. True if a re-open went out. Any
        refusal clears the wish and falls through to selling, so token is never left without a plan."""
        self.store.update("positions", "address", row["address"], reopen_wanted=0)
        if not self.sender.can_send or self.may_open(counting_slots=False):
            return False
        lamports = min(row["reopen_lamports"], row["ticket"])
        width = self.s.arm.reopen_width
        state = self.reader.read_state(row["pool"], keys.active - width - 1, keys.active + width + 2)
        key = self.keygen()
        address = base58.b58encode(bytes(key.verify_key)).decode()
        try:
            lo, hi, instructions = build_reopen(self.s.arm, keys, state, self.s.wallet, address, token=token,
                                                lamports=lamports, settings=self.s)
            sim = self.simulate(instructions)
        except tx.LpTxError as exc:
            self.counts["reopen_refused"] += 1
            log.info("re-open refused: %s", str(exc)[:120])
            return False
        if sim.get("err"):
            self.counts["reopen_sim_failed"] += 1
            return False
        at = self.now_ms()

        def record() -> None:
            self.store.insert("positions", address=address, trip=row["trip"], pool=row["pool"], mint=row["mint"], arm=row["arm"],
                              status="opening", ticket=row["ticket"], lo=lo, hi=hi, reopened=1,
                              exit_reason=row["exit_reason"], created_ms=at, updated_ms=at)
            self.store.db.execute("UPDATE positions SET status='done', note='reopened', updated_ms=? WHERE address=?",
                                  (at, row["address"]))
            self.plans.pop(row["address"], None)

        self.transmit("open", row["trip"], address, instructions, units=self.units_for(sim),
                      priority=self.s.priority_lamports, signers={address: key}, before=record)
        return True

    def finish(self, row: dict[str, Any], *, token_left: int = 0, note: str | None = None) -> None:
        self.store.update("positions", "address", row["address"], status="done", token_left=token_left,
                          note=note or row["note"], updated_ms=self.now_ms())
        self.plans.pop(row["address"], None)
        self.active_seen.pop(row["address"], None)
        self.close_trip(row["trip"], "done")
        self.counts["trips_done"] += 1

    # ------------------------------------------------------------------------------------- opening
    def cooling(self, pool: str) -> bool:
        cut = self.now_ms() - int(self.s.profile.cooldown_s * 1000)
        return bool(self.store.one("SELECT 1 FROM trips WHERE pool=? AND (finished_ms IS NULL OR finished_ms > ?) LIMIT 1", pool, cut))

    def select(self) -> None:
        """The paper bot's selection, in the same order, then one attempt on the first it admits."""
        self.last_select = self.now()
        refusal = self.may_open()
        reasons: Counter[str] = Counter()
        if refusal:
            reasons[refusal] += 1
            self.last_reasons = dict(reasons)
            return
        rows = (self.t.api(POOLS_PATH) or {}).get("data") or []
        reasons["listed"] = len(rows)
        held = {r["mint"] for r in self.store.active()}
        for row in rows:
            address, mint = row.get("address"), (row.get("token_x") or {}).get("address") or ""
            why = eligible(row, SELECTOR, SOL_MINT)
            if not why and (mint in held or self.cooling(address) or self.dry_seen.get(address, 0) > self.now()):
                why = "busy"
            if not why:
                why = self.reader.mint_problem(mint)
            if not why:
                why = self.try_open(row)
            reasons[why or "opened"] += 1
            if not why:
                break
        self.last_reasons = dict(reasons)

    def try_open(self, row: dict[str, Any]) -> str | None:
        """Plan, build, simulate, then send if live. A named refusal, or None when one went out."""
        address = row["address"]
        keys, liquid = self.read_keys(address)
        if keys.status != 0:
            return "pool_disabled"
        if keys.rewards_present:
            return "pool_has_rewards"  # A reward balance would stop `close_position_if_empty`.
        if keys.mint_y != SOL_MINT:
            return "pool_refused"
        arm, ticket = self.s.arm, self.s.ticket_lamports
        width = max(arm.width, arm.two_sided)
        state = self.reader.read_state(address, keys.active - width - 1, keys.active + width + 2)
        keys = replace(keys, active=state.active)
        key = self.keygen()
        position = base58.b58encode(bytes(key.verify_key)).decode()
        try:
            instructions = build_open(arm, keys, state, self.s.wallet, position, ticket=ticket, liquid=liquid, settings=self.s)
            sim = self.simulate(instructions)
        except tx.LpTxError as exc:
            return str(exc).split(":")[0]
        if sim.get("err"):
            log.info("open simulation failed: %s", str(sim["err"])[:160])
            return "simulation_failed"
        if not self.sender.can_send:
            self.counts["dry_open_ok"] += 1
            self.dry_seen[address] = self.now() + self.s.profile.cooldown_s
            return "dry_open_ok"
        plan = plan_bins(arm, state, ticket)
        trip, at = secrets.token_hex(8), self.now_ms()

        def record() -> None:
            self.store.insert("trips", id=trip, pool=address, arm=arm.name, status="running", started_ms=at)
            self.store.insert("positions", address=position, trip=trip, pool=address, mint=keys.mint_x, arm=arm.name,
                              status="opening", ticket=ticket, lo=plan.lo, hi=plan.hi, created_ms=at, updated_ms=at)

        self.transmit("open", trip, position, instructions, units=self.units_for(sim),
                      priority=self.s.priority_lamports, signers={position: key}, before=record)
        return None

    # --------------------------------------------------------------------------------------- the loop
    def step(self, name: str, work: Callable[[], None]) -> None:
        """A failure here is counted and logged, and the next step still runs: above all, an RPC
        error while picking pools must never stop a position's exit."""
        try:
            work()
        except Exception as exc:  # noqa: BLE001
            self.counts[f"{name}_failed"] += 1
            log.warning("%s failed: %s", name, str(exc)[:160])

    def cycle(self) -> None:
        if self.now() - self.last_scan >= RESCAN_S:
            self.step("scan", self.scan_chain)
        for send in self.store.unresolved():
            self.step("reconcile", lambda s=send: self.reconcile(s))
        self.step("deltas", self.settle_deltas)
        for row in self.store.active():
            self.step("manage", lambda r=row: self.manage(r))
        if self.now() - self.last_select >= self.s.profile.select_s:
            self.step("select", self.select)
        self.housekeeping()

    def housekeeping(self) -> None:
        if self.now() - self.last_status < 60:
            return
        self.last_status = self.now()
        tmp = self.store.root / "status.json.tmp"
        tmp.write_text(json.dumps({**status(self.store), "at_ms": self.now_ms(), "counts": dict(self.counts),
                                   "live": bool(self.sender.can_send), "last_selection": self.last_reasons}), encoding="utf-8")
        os.replace(tmp, self.store.root / "status.json")

    def run(self, seconds: float | None = None) -> None:
        stop = {"now": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
        until = self.now() + seconds if seconds else None
        while not stop["now"] and (until is None or self.now() < until):
            started = self.now()
            self.cycle()
            time.sleep(max(0.2, self.s.profile.poll_s - (self.now() - started)))


def status(store: Store) -> dict[str, Any]:
    """What is open, what is stuck, and the real result. The wallet and keys are not in it."""
    active = store.active()
    trips = store.rows("SELECT status, COUNT(*) AS n, COALESCE(SUM(net_lamports), 0) AS net FROM trips GROUP BY status")
    return {"version": VERSION, "by_status": dict(Counter(r["status"] for r in active)),
            "stuck_tokens": [{"mint": r["mint"], "atoms": r["token_left"], "tries": r["sweep_tries"], "note": r["note"]}
                             for r in active if r["status"] == "sweeping" and r["sweep_tries"]],
            "dust_left": store.rows("SELECT mint, token_left AS atoms FROM positions WHERE note='dust_left'"),
            "unresolved_sends": len(store.unresolved()),
            "trips": {r["status"]: {"n": r["n"], "net_sol": r["net"] / LAMPORTS} for r in trips}}


def data_root() -> Path:
    return Path(os.environ.get(DIR_ENV) or "data/labs/lp_live")


def find_arm(profile: Profile, name: str) -> Arm:
    for arm in profile.arms:
        if arm.name == name:
            return arm
    raise SystemExit(f"lp_arm_unknown:{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("run", "status"))
    parser.add_argument("--profile", choices=sorted(PROFILES), default="fast")
    parser.add_argument("--arm")
    parser.add_argument("--wallet")
    parser.add_argument("--ticket-sol", type=float)
    parser.add_argument("--seconds", type=float)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(Store(data_root())), indent=1))
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    profile = PROFILES[args.profile]
    wallet = args.wallet or os.environ.get(WALLET_ENV)
    if not wallet:
        raise SystemExit("lp_wallet_missing")
    settings = Settings(wallet=wallet, arm=find_arm(profile, args.arm or profile.primary), profile=profile)
    if args.ticket_sol:
        settings = replace(settings, ticket_lamports=int(args.ticket_sol * LAMPORTS))
    transport = Transport(rpc_url())
    sender: Any = DrySender()
    if os.environ.get(LIVE_ENV) == "1":
        try:
            sender = LiveSender(transport, open_gate(wallet))
        except LiveRefused as exc:
            raise SystemExit(str(exc)) from None
    log.info("lp_live %s arm %s", "LIVE" if sender.can_send else "dry", settings.arm.name)
    Runner(transport, Store(data_root()), settings, sender=sender).run(args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
