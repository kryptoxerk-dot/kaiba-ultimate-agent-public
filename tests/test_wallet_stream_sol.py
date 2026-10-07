"""kaiba/ingest/wallet_stream_sol.py: trusted and proven Solana wallets -> confirmed `swaps` rows.

Every address and signature is synthetic. Transactions follow the shape Alchemy's Solana
endpoint returns for ``getTransaction`` with ``jsonParsed`` (MEASURED 2026-10-04 on the box:
``accountKeys`` as ``{pubkey, signer, writable, source}``, ``transactionIndex`` present,
token balances carrying ``owner`` and ``uiTokenAmount.amount``/``decimals``).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import QUOTE_ASSETS, Chain
from kaiba.ingest import alchemy_ws as aw
from kaiba.ingest import runner
from kaiba.ingest import wallet_stream_sol as ss


def addr(tag: str) -> str:
    """A synthetic, valid-looking base58 address (44 chars, no 0/O/I/l)."""
    out = (tag + "1" * 44)[:44]
    assert ss.is_sol_address(out), out
    return out


W1 = addr("TrackedWa11etAAA")
W2 = addr("TrackedWa11etBBB")
W3 = addr("TrackedWa11etCCC")
STRANGER = addr("StrangerXXX")
FEE_PAYER = addr("Re1ayerFeePayer")
W1_ATA = addr("W1TokenAccount")
W1_WSOL = addr("W1WrappedSo1")
CURVE = addr("CurveAccount")
TOKEN = addr("MintTokenAAA")
TOKEN_B = addr("MintTokenBBB")
UNKNOWN_ROUTER = addr("SomeAggregatorProgram")
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SYSTEM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SIG = "5igA" + "a" * 84
SLOT = 453_169_576
BLOCK_TIME = 1_791_093_671
RECV_MS = BLOCK_TIME * 1000 + 900
NOW_MS = BLOCK_TIME * 1000 + 1_400
SOL_USD = Decimal("200")
FEE = 55_777
SOL = 10 ** 9


# --------------------------------------------------------------------------------------
# transaction builders
# --------------------------------------------------------------------------------------


def tb(index: int, mint: str, owner: str, amount: int, decimals: int | None = 6) -> dict[str, Any]:
    ui: dict[str, Any] = {"amount": str(amount), "uiAmountString": "x"}
    if decimals is not None:
        ui["decimals"] = decimals
    return {"accountIndex": index, "mint": mint, "owner": owner, "programId": TOKEN_PROGRAM, "uiTokenAmount": ui}


def sol_tx(*, accounts: list[str], signer_keys: tuple[str, ...], pre: list[int], post: list[int],
           pre_tb: list[dict[str, Any]] | None = None, post_tb: list[dict[str, Any]] | None = None,
           programs: tuple[str, ...] = (PUMP,), inner: tuple[str, ...] = (), fee: int = FEE,
           err: Any = None, sig: str = SIG, slot: int = SLOT, block_time: int | None = BLOCK_TIME,
           index: int = 7) -> dict[str, Any]:
    keys = [{"pubkey": k, "signer": k in signer_keys, "source": "transaction", "writable": True} for k in accounts]
    return {
        "slot": slot, "blockTime": block_time, "transactionIndex": index, "version": 0,
        "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                 "preTokenBalances": pre_tb or [], "postTokenBalances": post_tb or [],
                 "innerInstructions": [{"index": 0, "instructions": [{"programId": p, "stackHeight": 2}
                                                                      for p in inner]}],
                 "logMessages": []},
        "transaction": {"signatures": [sig],
                        "message": {"accountKeys": keys,
                                    "instructions": [{"programId": p, "stackHeight": 1} for p in programs]}},
    }


def buy_tx(*, paid: int = SOL // 2, atoms: int = 10 ** 12, wallet: str = W1, fee_payer: str | None = None,
           sig: str = SIG, token: str = TOKEN, decimals: int | None = 6, **kw: Any) -> dict[str, Any]:
    """``wallet`` pays ``paid`` lamports to the curve and receives ``atoms`` of ``token``."""
    payer = fee_payer or wallet
    if payer == wallet:
        accounts = [wallet, W1_ATA, CURVE, PUMP]
        pre = [10 * SOL, 0, 30 * SOL, 1]
        post = [10 * SOL - paid - FEE, 0, 30 * SOL + paid, 1]
        signer_keys: tuple[str, ...] = (wallet,)
    else:
        accounts = [payer, wallet, W1_ATA, CURVE, PUMP]
        pre = [5 * SOL, 10 * SOL, 0, 30 * SOL, 1]
        post = [5 * SOL - FEE, 10 * SOL - paid, 0, 30 * SOL + paid, 1]
        signer_keys = (payer, wallet)
    ata = accounts.index(W1_ATA)
    return sol_tx(accounts=accounts, signer_keys=signer_keys, pre=pre, post=post,
                  post_tb=[tb(ata, token, wallet, atoms, decimals)], sig=sig, **kw)


def sell_tx(*, received: int = SOL // 4, atoms: int = 4 * 10 ** 11, wallet: str = W1, sig: str = SIG,
            **kw: Any) -> dict[str, Any]:
    accounts = [wallet, W1_ATA, CURVE, PUMP]
    return sol_tx(accounts=accounts, signer_keys=(wallet,), pre=[10 * SOL, 0, 30 * SOL, 1],
                  post=[10 * SOL + received - FEE, 0, 30 * SOL - received, 1],
                  pre_tb=[tb(1, TOKEN, wallet, 10 ** 12)], post_tb=[tb(1, TOKEN, wallet, 10 ** 12 - atoms)],
                  sig=sig, **kw)


class FakeRpc:
    """Answers the reads the engine makes; records every method it was asked for."""

    def __init__(self) -> None:
        self.txs: dict[str, Any] = {}
        self.sigs: dict[str, list[dict[str, Any]]] = {}
        self.slot = SLOT + 10
        self.slot_seq: list[int] = []  # answered first, in order, when set
        self.calls: list[tuple[str, Any]] = []
        self.null_first: dict[str, int] = {}

    async def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "getTransaction":
            if self.null_first.get(params[0], 0) > 0:
                self.null_first[params[0]] -= 1
                return None
            return self.txs.get(params[0])
        if method == "getSignaturesForAddress":
            wallet, opts = params
            got = self.sigs.get(wallet, [])
            if opts.get("before"):
                idx = next((i for i, s in enumerate(got) if s["signature"] == opts["before"]), len(got))
                got = got[idx + 1:]
            return got[: int(opts.get("limit", 1000))]
        if method == "getSlot":
            return self.slot_seq.pop(0) if self.slot_seq else self.slot
        if method == "getBlockTime":
            return BLOCK_TIME
        return None

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


async def _no_sleep(_: float) -> None:
    return None


def engine(conn, rpc: FakeRpc, *, sol_usd: Decimal | None = SOL_USD, wallets=(W1, W2)) -> ss.SolWalletStream:
    e = ss.SolWalletStream(rpc, reader=conn, writer=conn, sol_usd=lambda ts: sol_usd,
                           clock_ms=lambda: NOW_MS, sleep=_no_sleep)
    e.wallets = frozenset(wallets)
    return e


def notice(sig: str = SIG, wallet: str = W1, *, backfilled: bool = False) -> ss.Notice:
    return ss.Notice(signature=sig, wallet=wallet, slot=SLOT, failed=False, logs=None, recv_ms=RECV_MS,
                     backfilled=backfilled)


def process(e: ss.SolWalletStream, sig: str = SIG, n: ss.Notice | None = None) -> list[ss.Outcome]:
    return asyncio.run(e.process_signature(sig, n if n is not None else notice(sig)))


def rows(conn) -> list[dict[str, Any]]:
    return fetch_all(conn, "SELECT * FROM swaps WHERE source = ? ORDER BY id", (ss.SOURCE,))


# --------------------------------------------------------------------------------------
# the trades: units, fee add-back, NULLs
# --------------------------------------------------------------------------------------


def test_a_pump_buy_is_written_in_the_units_of_the_existing_sol_rows(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx(paid=SOL // 2, atoms=10 ** 12)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and out.side == "buy"
    (row,) = rows(tmp_db)
    assert (row["chain"], row["tx"], row["wallet"], row["token"], row["side"]) == ("sol", SIG, W1, TOKEN, "buy")
    assert row["amount_token"] == str(10 ** 12)                       # token atoms
    assert row["amount_native"] == str(SOL // 2)                      # lamports, fee added back
    assert Decimal(row["usd_value"]) == Decimal("100")                # 0.5 SOL x $200
    assert Decimal(row["price_usd"]) == Decimal("0.0001")             # USD per WHOLE token (1e6 tokens)
    assert row["slot"] == SLOT and row["ts_ms"] == BLOCK_TIME * 1000 and row["block_index"] == 7
    assert row["fee_payer"] == W1 and row["program"] == PUMP and row["source"] == "alchemy:ws"
    assert row["amount_quote"] is None and row["quote_mint"] is None and row["is_create_tx"] == 0
    ev = fetch_all(tmp_db, "SELECT subject, payload, dedupe_key FROM events WHERE kind = 'wallet.trade'")
    assert len(ev) == 1 and ev[0]["subject"] == W1
    assert ev[0]["dedupe_key"] == f"wallet.trade:alchemy:ws:{SIG}:{W1}:{TOKEN}:buy"
    payload = json.loads(ev[0]["payload"])
    assert payload["recv_latency_ms"] == 900 and payload["write_latency_ms"] == 1400
    assert payload["venue"] == "pump.fun" and payload["fee_added_back"] is True
    assert "cohort" not in payload and "tags" not in payload  # the feed claims nothing about trust


def test_units_agree_with_the_helius_backfill_derivation_of_the_same_trade(tmp_db):
    """The same buy as an enhanced-transaction payload: ``backfill.classify_swap`` (the
    ``helius:backfill`` rows) and this feed must agree on token, side, atoms and lamports."""
    from kaiba.ingest import backfill

    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx(paid=123_456_789, atoms=987_654_321)
    process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    enhanced = {"signature": SIG, "type": "SWAP", "timestamp": BLOCK_TIME, "slot": SLOT, "fee": FEE,
                "feePayer": W1, "instructions": [{"programId": PUMP}],
                "accountData": [{"account": W1, "nativeBalanceChange": -123_456_789 - FEE,
                                 "tokenBalanceChanges": [{"userAccount": W1, "mint": TOKEN,
                                                          "rawTokenAmount": {"tokenAmount": "987654321",
                                                                             "decimals": 6}}]}]}
    helius, why = backfill.classify_swap(enhanced, W1)
    assert why is None
    assert (row["token"], row["side"]) == (helius.token, helius.side)
    assert row["amount_token"] == str(helius.amount_token) and row["amount_native"] == str(helius.amount_native)
    assert row["ts_ms"] == helius.ts_ms and row["slot"] == helius.slot


def test_the_fee_is_added_back_only_for_the_wallet_that_paid_it(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx(paid=SOL // 2, wallet=W2, fee_payer=FEE_PAYER)
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written"
    (row,) = rows(tmp_db)
    assert row["amount_native"] == str(SOL // 2)  # the relayer paid the fee; nothing to add back
    assert row["fee_payer"] == FEE_PAYER
    leg, _ = ss.classify(buy_tx(paid=SOL // 2), W1)
    assert leg is not None and leg.lamports == SOL // 2  # the payer's own fee comes back out


def test_a_sell_receives_lamports_with_the_fee_added_back(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = sell_tx(received=SOL // 4, atoms=4 * 10 ** 11)
    (out,) = process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    assert out.status == "written" and row["side"] == "sell"
    assert row["amount_native"] == str(SOL // 4) and row["amount_token"] == str(4 * 10 ** 11)


def test_wsol_is_sol(tmp_db):
    """Paid from a standing WSOL account: native moved only by the fee, WSOL by the price."""
    accounts = [W1, W1_ATA, W1_WSOL, CURVE, PUMP_AMM]
    tx = sol_tx(accounts=accounts, signer_keys=(W1,), programs=(PUMP_AMM,),
                pre=[10 * SOL, 0, 2 * SOL, 30 * SOL, 1], post=[10 * SOL - FEE, 0, 2 * SOL, 30 * SOL, 1],
                pre_tb=[tb(2, ss.WSOL_MINT, W1, 3 * SOL, 9)],
                post_tb=[tb(1, TOKEN, W1, 5 * 10 ** 11), tb(2, ss.WSOL_MINT, W1, 3 * SOL - 70_000_000, 9)])
    leg, why = ss.classify(tx, W1)
    assert why == "ok" and leg is not None
    assert (leg.side, leg.atoms, leg.lamports, leg.program) == ("buy", 5 * 10 ** 11, 70_000_000, PUMP_AMM)


def test_no_sol_price_leaves_usd_and_price_null_never_zero(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    process(engine(tmp_db, rpc, sol_usd=None))
    (row,) = rows(tmp_db)
    assert row["usd_value"] is None and row["price_usd"] is None and row["amount_native"] == str(SOL // 2)


def test_unknown_decimals_leave_only_the_price_null(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx(decimals=None)
    process(engine(tmp_db, rpc))
    (row,) = rows(tmp_db)
    assert row["price_usd"] is None and Decimal(row["usd_value"]) == Decimal("100")


def test_price_fields_are_none_when_any_input_is_unknown():
    assert ss.price_fields(lamports=SOL, atoms=10 ** 6, decimals=6, sol_usd=None) == (None, None)
    assert ss.price_fields(lamports=0, atoms=10 ** 6, decimals=6, sol_usd=SOL_USD) == (None, None)
    usd, price = ss.price_fields(lamports=SOL, atoms=10 ** 6, decimals=6, sol_usd=SOL_USD)
    assert Decimal(usd) == 200 and Decimal(price) == 200
    assert ss.price_fields(lamports=SOL, atoms=10 ** 6, decimals=None, sol_usd=SOL_USD)[1] is None


def test_a_missing_block_time_is_read_from_the_slot_not_invented(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx(block_time=None)
    process(engine(tmp_db, rpc))
    assert rows(tmp_db)[0]["ts_ms"] == BLOCK_TIME * 1000 and "getBlockTime" in rpc.methods()


def test_a_transaction_not_yet_served_is_retried(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    rpc.null_first[SIG] = 2
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and rpc.methods().count("getTransaction") == 3


# --------------------------------------------------------------------------------------
# confirmation rules
# --------------------------------------------------------------------------------------


def _airdrop() -> dict[str, Any]:
    """Tokens arrive, a venue ran (someone else's swap), the wallet paid nothing."""
    return sol_tx(accounts=[STRANGER, W1, W1_ATA, PUMP], signer_keys=(STRANGER,),
                  pre=[5 * SOL, 10 * SOL, 0, 1], post=[5 * SOL - FEE, 10 * SOL, 0, 1],
                  post_tb=[tb(2, TOKEN, W1, 10 ** 9)])


def _dust() -> dict[str, Any]:
    return buy_tx(paid=5_000)


def _no_venue() -> dict[str, Any]:
    return buy_tx(programs=(SYSTEM, TOKEN_PROGRAM))


def _failed() -> dict[str, Any]:
    return buy_tx(err={"InstructionError": [2, {"Custom": 6001}]})


def _two_tokens() -> dict[str, Any]:
    t = buy_tx()
    t["meta"]["postTokenBalances"].append(tb(1, TOKEN_B, W1, 5))
    return t


def _stable_quoted() -> dict[str, Any]:
    return sol_tx(accounts=[W1, W1_ATA, W1_WSOL, CURVE, PUMP_AMM], signer_keys=(W1,), programs=(PUMP_AMM,),
                  pre=[10 * SOL, 0, 0, 0, 1], post=[10 * SOL - FEE, 0, 0, 0, 1],
                  pre_tb=[tb(2, ss.USDC_MINT, W1, 50_000_000)],
                  post_tb=[tb(1, TOKEN, W1, 10 ** 9), tb(2, ss.USDC_MINT, W1, 0)])


def _routed_through() -> dict[str, Any]:
    t = buy_tx()
    t["meta"]["preTokenBalances"] = [tb(1, TOKEN, W1, 10 ** 12)]  # in and out: net zero
    return t


def _same_direction() -> dict[str, Any]:
    """Tokens AND SOL arrive: a gift, not a buy."""
    return sol_tx(accounts=[STRANGER, W1, W1_ATA, PUMP], signer_keys=(STRANGER,),
                  pre=[5 * SOL, 10 * SOL, 0, 1], post=[4 * SOL - FEE, 11 * SOL, 0, 1],
                  post_tb=[tb(2, TOKEN, W1, 10 ** 9)])


@pytest.mark.parametrize("build,reason", [
    (_airdrop, "no_opposite_sol"),
    (_dust, "no_opposite_sol"),
    (_same_direction, "no_opposite_sol"),
    (_no_venue, "no_swap_program"),
    (_failed, "failed_transaction"),
    (_two_tokens, "multi_leg_route"),
    (_stable_quoted, "stable_quoted"),
    (_routed_through, "no_position_leg"),
], ids=lambda x: x if isinstance(x, str) else x.__name__)
def test_only_a_confirmed_sol_swap_is_written(tmp_db, build, reason):
    rpc = FakeRpc()
    rpc.txs[SIG] = build()
    outs = process(engine(tmp_db, rpc))
    assert [(o.status, o.reason) for o in outs if o.wallet == W1] == [("unconfirmed", reason)]
    assert rows(tmp_db) == []
    assert fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind = 'wallet.trade'") == []


def test_a_venue_reached_only_through_an_aggregator_still_confirms():
    leg, why = ss.classify(buy_tx(programs=(UNKNOWN_ROUTER,), inner=(SYSTEM, PUMP_AMM)), W1)
    assert why == "ok" and leg is not None and leg.program == PUMP_AMM
    assert ss.swap_program(buy_tx(programs=("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",), inner=(PUMP,))) \
        == "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"  # top level first, as helius records it


def test_mixed_sol_and_stable_legs_are_not_attributed():
    t = buy_tx()
    t["meta"]["preTokenBalances"].append(tb(1, ss.USDC_MINT, W1, 10_000_000))
    t["meta"]["postTokenBalances"].append(tb(1, ss.USDC_MINT, W1, 0))
    assert ss.classify(t, W1) == (None, "mixed_quote_legs")


def test_every_tracked_wallet_in_one_transaction_is_handled_once(tmp_db):
    t = buy_tx()
    t["transaction"]["message"]["accountKeys"].append({"pubkey": W2, "signer": False, "writable": False})
    t["meta"]["preBalances"].append(1)
    t["meta"]["postBalances"].append(1)
    rpc = FakeRpc()
    rpc.txs[SIG] = t
    outs = process(engine(tmp_db, rpc))
    assert sorted((o.wallet, o.status) for o in outs) == [(W1, "written"), (W2, "unconfirmed")]


# --------------------------------------------------------------------------------------
# dedupe
# --------------------------------------------------------------------------------------


def _other_feed_row(conn, *, tx: str, source: str = "pumpfun:trades", amount_token: str = "1") -> None:
    conn.execute("INSERT INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, amount_native, "
                 "price_usd, usd_value, program, source) VALUES ('sol',?,?,?,?,?,'buy',?,?,?,?,?,?)",
                 (tx, SLOT, BLOCK_TIME * 1000, W1, TOKEN, amount_token, "500000000", "0.0001", "100", "pump",
                  source))


def test_a_trade_another_feed_already_wrote_is_not_written_again(tmp_db):
    _other_feed_row(tmp_db, tx=SIG)
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "matched_existing" and out.matched_sources == ("pumpfun:trades",)
    assert out.row is not None and out.row["amount_native"] == str(SOL // 2)  # kept for the comparison
    assert rows(tmp_db) == []


def test_dedupe_is_per_tx_wallet_token_and_case_sensitive(tmp_db):
    """Positive controls: the wallet's OTHER trade on the token, and a signature that differs
    only in case (base58 is case-sensitive), are not matches."""
    _other_feed_row(tmp_db, tx="5igB" + "a" * 84)
    _other_feed_row(tmp_db, tx=SIG.upper())
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    (out,) = process(engine(tmp_db, rpc))
    assert out.status == "written" and len(rows(tmp_db)) == 1


def test_replaying_our_own_trade_writes_one_row_and_one_event(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    e = engine(tmp_db, rpc)
    assert process(e)[0].status == "written"
    assert process(e)[0].status == "duplicate"
    assert len(rows(tmp_db)) == 1
    assert len(fetch_all(tmp_db, "SELECT 1 FROM events WHERE kind = 'wallet.trade'")) == 1


# --------------------------------------------------------------------------------------
# the tracked set and the screen
# --------------------------------------------------------------------------------------


@pytest.fixture
def proven(monkeypatch):
    from kaiba.learning import proven as pv

    box: dict[str, Any] = {"value": frozenset({W3})}
    monkeypatch.setattr(pv, "proven_members", lambda conn, chain, **kw: (
        None if box["value"] is None else SimpleNamespace(members=box["value"])))
    return box


def _wallet(conn, address: str, *, chain: str = "sol", cohort: str = "trusted_copy") -> None:
    conn.execute("INSERT OR REPLACE INTO wallets (chain, address, cohort, first_seen_ms, last_seen_ms) "
                 "VALUES (?,?,?,0,0)", (chain, address, cohort))


def test_the_tracked_set_is_sol_trusted_copy_plus_the_proven_cohort(tmp_db, proven):
    _wallet(tmp_db, W1)
    _wallet(tmp_db, W2, chain="robinhood")            # not sol
    _wallet(tmp_db, STRANGER, cohort="research")      # not trusted
    _wallet(tmp_db, "0x" + "ab" * 20)                 # not a Solana address
    assert ss.candidate_set(tmp_db) == tuple(sorted({W1, W3}))
    assert ss.tracked_set(tmp_db, excluded={W3}) == (W1,)
    proven["value"] = None                            # no cohort, or a stale one
    assert ss.candidate_set(tmp_db) == (W1,)


def _sigs(n: int, *, span_s: float, now: float, ok_every: int = 1, prefix: str = "s") -> list[dict[str, Any]]:
    return [{"signature": f"{prefix}{i:06d}", "slot": SLOT - i, "blockTime": int(now - span_s * i / max(1, n - 1)),
             "err": None if i % ok_every == 0 else {"x": 1}} for i in range(n)]


def test_the_screen_excludes_a_bot_by_rate_and_a_router_by_zero_net_swaps(tmp_db):
    now = float(BLOCK_TIME)
    rpc = FakeRpc()
    rpc.sigs[W1] = _sigs(1000, span_s=72, now=now)                    # 50,000/h: a bot
    rpc.sigs[W2] = _sigs(40, span_s=3600, now=now, prefix="r")         # routes everything
    rpc.sigs[W3] = _sigs(40, span_s=3600, now=now, prefix="k")         # keeps what it buys
    for s in rpc.sigs[W2]:
        t = buy_tx(wallet=W2, sig=s["signature"])
        t["meta"]["preTokenBalances"] = [tb(1, TOKEN, W2, 10 ** 12)]  # bought and passed on
        rpc.txs[s["signature"]] = t
    for s in rpc.sigs[W3]:
        rpc.txs[s["signature"]] = buy_tx(wallet=W3, sig=s["signature"])
    e = engine(tmp_db, rpc, wallets=())
    excluded = asyncio.run(e.screen([W1, W2, W3], now_s=now))
    assert set(excluded) == {W1, W2}
    assert excluded[W1]["reason"] == "bot" and excluded[W1]["ok_per_hour"] >= ss.BOT_MAX_TXS_PER_HOUR
    assert excluded[W2]["reason"] == "router" and excluded[W2]["zero_net_share"] == 1.0
    assert excluded[W2]["swap_txs"] == ss.ROUTER_SAMPLE_TXS
    # the bot was never sampled; a second screen does not pay for what it already read
    sampled = [p[0] for m, p in rpc.calls if m == "getTransaction"]
    assert not any(s.startswith("s") for s in sampled) and len(sampled) == 2 * ss.ROUTER_SAMPLE_TXS
    rpc.calls.clear()
    asyncio.run(e.screen([W1, W2, W3], now_s=now))
    assert rpc.methods().count("getTransaction") == 0


def test_the_screen_never_pays_twice_for_a_transaction_that_is_not_evidence(tmp_db):
    """A wallet mostly MENTIONED in others' transactions (MEASURED: 4 of 16 proven sol wallets
    signed 0 of their 10 newest) never builds router evidence; without a memory of what was
    read, every hourly screen would buy the same transactions again at 40 CU each."""
    now = float(BLOCK_TIME)
    rpc = FakeRpc()
    rpc.sigs[W1] = _sigs(5, span_s=3600, now=now, prefix="m")
    for s in rpc.sigs[W1]:
        rpc.txs[s["signature"]] = _airdrop()  # signed by a stranger: no evidence either way
    e = engine(tmp_db, rpc, wallets=())
    assert asyncio.run(e.screen([W1], now_s=now)) == {}
    assert rpc.methods().count("getTransaction") == 5
    rpc.calls.clear()
    asyncio.run(e.screen([W1], now_s=now))
    assert rpc.methods() == ["getSignaturesForAddress"]


def test_the_router_sample_can_reach_the_rule_with_a_few_non_evidence_transactions(tmp_db):
    """MEASURED on the box: a 100% zero-net wallet sampled 20 deep kept 18-19 pieces of
    evidence (a transfer or two among its newest), under the 20 the rule needs."""
    now = float(BLOCK_TIME)
    rpc = FakeRpc()
    rpc.sigs[W2] = _sigs(40, span_s=3600, now=now, prefix="r")
    for i, s in enumerate(rpc.sigs[W2]):
        t = buy_tx(wallet=W2, sig=s["signature"])
        t["meta"]["preTokenBalances"] = [tb(1, TOKEN, W2, 10 ** 12)]
        rpc.txs[s["signature"]] = _airdrop() if i % 10 == 0 else t  # 1 in 10 is not evidence
    excluded = asyncio.run(engine(tmp_db, rpc, wallets=()).screen([W2], now_s=now))
    assert excluded[W2]["reason"] == "router" and excluded[W2]["swap_txs"] >= ss.ROUTER_MIN_TXS


def test_a_tracked_wallet_that_starts_routing_is_flagged_once(tmp_db):
    rpc = FakeRpc()
    flagged: list[str] = []
    e = engine(tmp_db, rpc, wallets=(W1,))
    e.on_router = flagged.append
    for i in range(ss.ROUTER_MIN_TXS + 5):
        e.note_evidence(W1, f"t{i}", True)
        e.note_evidence(W2, f"t{i}", True)  # not tracked: nothing to drop
    assert flagged == [W1]
    e.note_evidence(W1, "u0", False)
    assert flagged == [W1]
    # the screen's own sampling decides on the evidence itself: no second screen requested
    quiet = engine(tmp_db, rpc, wallets=(W2,))
    quiet.on_router = flagged.append
    for i in range(ss.ROUTER_MIN_TXS + 5):
        quiet.note_evidence(W2, f"t{i}", True, notify=False)
    assert flagged == [W1]


def test_run_drops_a_wallet_that_starts_routing_before_the_hourly_refresh(tmp_db, proven, monkeypatch):
    _wallet(tmp_db, W1)
    _wallet(tmp_db, W2)
    proven["value"] = frozenset()
    monkeypatch.setattr(ss, "RESCREEN_MIN_S", 0.0)
    rpc = FakeRpc()
    rpc.sigs[W1], rpc.sigs[W2] = [], []           # the startup screen sees nothing either way
    sigs = [f"z{i:05d}" for i in range(ss.ROUTER_MIN_TXS)]
    for sig in sigs:
        t = buy_tx(wallet=W2, sig=sig)
        t["meta"]["preTokenBalances"] = [tb(1, TOKEN, W2, 10 ** 12)]
        rpc.txs[sig] = t
    calls: list[tuple[str, ...]] = []
    stop = asyncio.Event()

    async def fake_stream(url: str, wallets: Any, **kw: Any):
        calls.append(tuple(wallets))
        if len(calls) == 1:
            for sig in sigs:
                yield ss.Notice(signature=sig, wallet=W2, slot=SLOT, failed=False, logs=None, recv_ms=RECV_MS)
            await kw["stop"].wait()           # the refresher ends this socket
        else:
            stop.set()

    monkeypatch.setattr(ss, "stream", fake_stream)
    asyncio.run(asyncio.wait_for(ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db,
                                        rpc=_Client(rpc), sol_usd=lambda ts: SOL_USD, resume=False,
                                        refresh_s=3600), timeout=20))
    assert calls == [(W1, W2), (W1,)]
    (event,) = _system(tmp_db, "excluded")
    assert event["address"] == W2 and event["reason"] == "router" and event["zero_net_share"] == 1.0


def test_router_and_bot_thresholds_need_enough_evidence():
    assert ss.classify_routers({W1: {f"t{i}": True for i in range(19)}}) == frozenset()
    assert ss.classify_routers({W1: {**{f"t{i}": True for i in range(16)}, **{f"u{i}": False for i in range(4)}}}) \
        == frozenset({W1})                            # 80%
    assert ss.classify_routers({W1: {**{f"t{i}": True for i in range(15)}, **{f"u{i}": False for i in range(5)}}}) \
        == frozenset()                                # 75%
    few = ss.signature_rate(_sigs(10, span_s=1, now=1e6), now_s=1e6)
    assert not ss.is_bot(few)                         # 10 signatures are not a rate
    slow = ss.signature_rate(_sigs(1000, span_s=7200, now=1e6), now_s=1e6)    # 500/h
    fast = ss.signature_rate(_sigs(1000, span_s=3000, now=1e6), now_s=1e6)    # 1,200/h
    assert not ss.is_bot(slow) and ss.is_bot(fast)


def test_zero_net_is_evidence_only_for_a_signed_swap():
    routed = buy_tx()
    routed["meta"]["preTokenBalances"] = [tb(1, TOKEN, W1, 10 ** 12)]
    assert ss.zero_net(routed, W1) is True
    assert ss.zero_net(buy_tx(), W1) is False
    assert ss.zero_net(_airdrop(), W1) is None        # the wallet did not sign it
    assert ss.zero_net(_no_venue(), W1) is None
    assert ss.zero_net(_failed(), W1) is None


# --------------------------------------------------------------------------------------
# source hygiene and constants
# --------------------------------------------------------------------------------------


def test_no_wallet_address_is_written_in_the_stream_source():
    """The tracked wallets are a private choice; this source is exported publicly. The only
    base58 literals allowed are the venue programs and the quote mints."""
    import re
    from pathlib import Path

    text = Path(ss.__file__).read_text(encoding="utf-8")
    literals = set(re.findall(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])", text))
    assert literals == set(ss.SWAP_PROGRAMS) | {ss.WSOL_MINT, ss.USDC_MINT, ss.USDT_MINT}
    assert not re.findall(r"0x[0-9a-fA-F]{40}", text)


def test_constants_agree_with_their_other_homes():
    from kaiba.execution import policy, snipe
    from kaiba.ingest import backfill, wallet_stream

    assert all(policy.DEX_PROGRAMS[p] == name for p, name in ss.SWAP_PROGRAMS.items())
    assert ss.QUOTE_MINTS == QUOTE_ASSETS[Chain.SOL] == backfill.QUOTE_MINTS
    assert ss.WSOL_MINT == backfill.WSOL_MINT and ss.MIN_QUOTE_LAMPORTS == backfill.MIN_QUOTE_LAMPORTS
    assert ss.PROVEN_MAX_AGE_S == snipe.DEFAULT_PARAMS["proven_max_cohort_age_s"]
    assert ss.SOURCE == wallet_stream.SOURCE and not ss.SOURCE.startswith("gmgn:")


def test_the_listener_is_registered_with_the_ingest_runner():
    assert "sol_wallets" in runner.REGISTRY and "sol_wallets" not in runner.DEFAULT_FEEDS


def test_http_rpc_errors_never_carry_the_key():
    class Boom:
        async def post(self, url: str, json: Any) -> Any:
            raise ConnectionError(f"cannot reach {url}")

    rpc = ss.SolRpc("https://solana-mainnet.example/v2/SECRETKEY123", client=Boom())
    with pytest.raises(ss.RpcFailure) as err:
        asyncio.run(rpc.call("getSlot", []))
    assert "SECRETKEY123" not in str(err.value) and "/v2/***" in str(err.value)
    assert rpc.estimated_cu() == ss.CU_PER_CALL["getSlot"]


def test_notifications_worth_no_read_are_skipped_for_free():
    logs_swap = ("Program ComputeBudget111111111111111111111111111111 invoke [1]", f"Program {PUMP} invoke [2]")
    logs_none = (f"Program {SYSTEM} invoke [1]", f"Program {SYSTEM} success")
    mk = lambda **kw: ss.Notice(signature=SIG, wallet=W1, slot=SLOT, recv_ms=0, **kw)  # noqa: E731
    assert ss.should_read(mk(failed=True, logs=logs_swap)) == "failed"
    assert ss.should_read(mk(failed=False, logs=logs_none)) == "no_swap_program"
    assert ss.should_read(mk(failed=False, logs=logs_swap)) is None
    assert ss.should_read(mk(failed=False, logs=(*logs_none, "Log truncated"))) is None  # cannot tell: read
    assert ss.should_read(mk(failed=False, logs=None)) is None                         # backfill: read


# --------------------------------------------------------------------------------------
# the socket, against a scripted endpoint
# --------------------------------------------------------------------------------------


class FakeSolWS:
    def __init__(self, server: FakeSolServer, n: int) -> None:
        self.server, self.n = server, n
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.subs: dict[str, int] = {}

    async def __aenter__(self) -> FakeSolWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, text: str) -> None:
        req = json.loads(text)
        self.server.requests.append((self.n, req))
        script = self.server.scripts[self.n]
        if req["method"] == "logsUnsubscribe":
            self.server.unsubscribed.append((self.n, req["params"][0]))
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": True}))
        if req["method"] == "logsSubscribe":
            wallet = req["params"][0]["mentions"][0]
            sid = 1000 * (self.n + 1) + len(self.subs)
            self.subs[wallet] = sid
            self.q.put_nowait(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": sid}))
            if len(self.subs) == script.get("expect_subs", 1):
                for item in script.get("pushes", []):
                    self.q.put_nowait(item if isinstance(item, BaseException) else self.push(*item))

    def push(self, wallet: str, sig: str, slot: int, err: Any = None, logs: list[str] | None = None) -> str:
        return json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                           "params": {"subscription": self.subs[wallet],
                                      "result": {"context": {"slot": slot},
                                                 "value": {"signature": sig, "err": err, "logs": logs or []}}}})

    async def recv(self) -> Any:
        item = await self.q.get()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeSolServer:
    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        self.scripts = scripts
        self.requests: list[tuple[int, dict[str, Any]]] = []
        self.urls: list[str] = []
        self.unsubscribed: list[tuple[int, int]] = []
        self.dials = 0

    def connect(self, url: str) -> FakeSolWS:
        self.urls.append(url)
        n = self.dials
        self.dials += 1
        if n >= len(self.scripts):
            raise ConnectionRefusedError("no script")
        return FakeSolWS(self, n)


@pytest.fixture
def no_backoff(monkeypatch):
    async def no_wait(stop: asyncio.Event, seconds: float) -> None:
        return None

    monkeypatch.setattr(aw, "_wait", no_wait)


def _collect(server: FakeSolServer, rpc: FakeRpc, wallets: list[str], n: int, **kw: Any):
    out: list[ss.Notice] = []
    statuses: list[dict[str, Any]] = []
    stats = ss.SolFeedStats()

    async def go() -> None:
        stop = asyncio.Event()
        async for rec in ss.stream("https://solana.example/v2/SECRETKEY987", wallets, rpc=rpc, stop=stop,
                                   connect=server.connect, stats=stats, on_status=statuses.append,
                                   poll_s=0.01, **kw):
            out.append(rec)
            if len(out) >= n:
                stop.set()

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    return out, statuses, stats


def test_one_logs_subscription_per_wallet_at_confirmed_and_pushes_are_mapped(no_backoff):
    server = FakeSolServer([{"expect_subs": 2, "pushes": [(W2, "sigB", SLOT + 1, None, [f"Program {PUMP} invoke [1]"]),
                                                          (W1, "sigF", SLOT + 2, {"InstructionError": 1})]}])
    out, statuses, stats = _collect(server, FakeRpc(), [W1, W2, "not-an-address"], 2, max_attempts=1)
    subs = [r for _, r in server.requests if r["method"] == "logsSubscribe"]
    assert [s["params"] for s in subs] == [[{"mentions": [w]}, {"commitment": "confirmed"}] for w in sorted([W1, W2])]
    assert [(n.signature, n.wallet, n.slot, n.failed, n.backfilled) for n in out] == \
        [("sigB", W2, SLOT + 1, False, False), ("sigF", W1, SLOT + 2, True, False)]
    assert out[0].logs == (f"Program {PUMP} invoke [1]",)
    assert stats.notifications == 2 and stats.failed == 1 and stats.notification_bytes > 0
    assert server.urls == ["wss://solana.example/v2/SECRETKEY987"]
    assert "SECRETKEY987" not in json.dumps(statuses)


def test_a_malformed_push_is_counted_and_never_drops_the_socket(no_backoff):
    server = FakeSolServer([{"pushes": [(W1, "sigGood", SLOT)]}])
    real_connect = server.connect

    def connect(url: str) -> FakeSolWS:
        ws = real_connect(url)
        sid = 1000  # the first subscription id FakeSolWS hands out
        for bad in ({"result": "not-a-mapping"},
                    {"result": {"context": 7, "value": {"signature": "sigNoSlot", "err": None}}}):
            ws.q.put_nowait(json.dumps({"jsonrpc": "2.0", "method": "logsNotification",
                                        "params": {"subscription": sid, **bad}}))
        return ws

    server.connect = connect  # type: ignore[method-assign]
    out, _, stats = _collect(server, FakeRpc(), [W1], 2, max_attempts=1)
    assert [(n.signature, n.slot) for n in out] == [("sigNoSlot", None), ("sigGood", SLOT)]
    assert stats.undecoded == 1 and stats.disconnects == 0 and stats.connects == 1


def test_a_reconnect_backfills_each_wallet_from_the_last_slot_without_duplicates(no_backoff):
    rpc = FakeRpc()
    rpc.slot = SLOT + 50
    # newest first, as getSignaturesForAddress answers: one failed, one older than the gap
    rpc.sigs[W1] = [{"signature": "sigNew", "slot": SLOT + 40, "err": None, "blockTime": BLOCK_TIME + 9},
                    {"signature": "sigBad", "slot": SLOT + 30, "err": {"x": 1}, "blockTime": BLOCK_TIME + 8},
                    {"signature": "sigLive", "slot": SLOT + 20, "err": None, "blockTime": BLOCK_TIME + 5},
                    {"signature": "sigOld", "slot": SLOT - 5, "err": None, "blockTime": BLOCK_TIME - 2}]
    server = FakeSolServer([
        {"pushes": [(W1, "sigLive", SLOT + 20), ConnectionResetError("peer reset")]},
        {"pushes": [(W1, "sigAfter", SLOT + 60)]},
    ])
    out, statuses, stats = _collect(server, rpc, [W1], 3, max_attempts=3, anchor_on_subscribe=False)
    assert [(n.signature, n.backfilled) for n in out] == [("sigLive", False), ("sigNew", True), ("sigAfter", False)]
    assert stats.duplicates == 1 and stats.connects == 2 and stats.backfilled == 1
    calls = [p for m, p in rpc.calls if m == "getSignaturesForAddress"]
    assert calls and calls[0][0] == W1 and calls[0][1]["commitment"] == "confirmed"
    assert [s["event"] for s in statuses][:4] == ["subscribed", "disconnected", "subscribed", "backfilled"]


def test_from_slot_backfills_on_the_first_connection_and_a_page_bound_is_reported(no_backoff):
    rpc = FakeRpc()
    rpc.slot = SLOT + 500
    rpc.sigs[W1] = [{"signature": f"p{i:03d}", "slot": SLOT + 400 - i, "err": None, "blockTime": BLOCK_TIME}
                    for i in range(10)]
    server = FakeSolServer([{"pushes": []}])
    out, statuses, stats = _collect(server, rpc, [W1], 4, from_slot=SLOT, backfill_page=2, backfill_max_pages=2,
                                    max_attempts=1)
    assert [n.signature for n in out] == ["p003", "p002", "p001", "p000"]  # oldest first, bounded
    assert all(n.backfilled for n in out)
    assert any(s["event"] == "gap_truncated" and s.get("reason") == "page_bound" for s in statuses)
    assert stats.gaps_truncated == 1


@pytest.mark.parametrize("anchor", [True, False], ids=["anchored", "unanchored-control"])
def test_a_quiet_reconnect_backfills_its_gap_only_when_anchored(no_backoff, anchor):
    """The Robinhood twin's MEASURED failure: a quiet set reconnected with no point to backfill
    from. The anchor (slot read after subscribe) gives it one."""
    rpc = FakeRpc()
    rpc.slot = SLOT
    rpc.sigs[W1] = [{"signature": "sigGap", "slot": SLOT + 3, "err": None, "blockTime": BLOCK_TIME}]
    server = FakeSolServer([{"pushes": [ConnectionResetError("drop")]}, {"pushes": []}])
    out: list[ss.Notice] = []

    async def go() -> None:
        stop = asyncio.Event()
        async for rec in ss.stream("https://s.example/v2/K", [W1], rpc=rpc, stop=stop, connect=server.connect,
                                   poll_s=0.01, max_attempts=3, anchor_on_subscribe=anchor):
            out.append(rec)
            stop.set()

    try:
        asyncio.run(asyncio.wait_for(go(), timeout=1.0))
    except TimeoutError:
        pass
    if anchor:
        assert [(n.signature, n.backfilled) for n in out] == [("sigGap", True)]
    else:
        assert out == [] and "getSignaturesForAddress" not in rpc.methods()


# --------------------------------------------------------------------------------------
# the listener, end to end
# --------------------------------------------------------------------------------------


def _system(conn, status: str) -> list[dict[str, Any]]:
    out = [json.loads(r["payload"]) for r in fetch_all(conn, "SELECT payload FROM events WHERE kind = 'system'")]
    return [p for p in out if p.get("status") == status]


class _Client:
    def __init__(self, rpc: FakeRpc) -> None:
        self.call = rpc
        self.calls = {}

    def estimated_cu(self) -> int:
        return 0


def test_run_writes_a_pushed_trade_saves_its_resume_point_and_never_logs_the_key(tmp_db, proven, no_backoff):
    _wallet(tmp_db, W1)
    proven["value"] = frozenset()
    rpc = FakeRpc()
    rpc.sigs[W1] = []
    rpc.txs[SIG] = buy_tx()
    server = FakeSolServer([{"pushes": [(W1, "sigNoSwap", SLOT, None, [f"Program {SYSTEM} invoke [1]"]),
                                        (W1, "sigFailed", SLOT, {"e": 1}, [f"Program {PUMP} invoke [1]"]),
                                        (W1, SIG, SLOT, None, [f"Program {PUMP} invoke [1]"])]}])
    written: list[ss.Outcome] = []

    async def go() -> dict[str, Any]:
        stop = asyncio.Event()

        def done(o: ss.Outcome) -> None:
            written.append(o)
            stop.set()

        return await asyncio.wait_for(
            ss.run(stop, url="https://solana.example/v2/SECRETKEY", reader=tmp_db, writer=tmp_db,
                   rpc=_Client(rpc), sol_usd=lambda ts: SOL_USD, connect=server.connect, resume=False,
                   on_written=done), timeout=10)

    summary = asyncio.run(go())
    assert [o.status for o in written] == ["written"]
    assert summary["stats"]["written"] == 1 and summary["stats"]["tracked"] == 1
    assert summary["stats"]["skipped_failed"] == 1 and summary["stats"]["skipped_no_swap_program"] == 1
    fetched = [p[0] for m, p in rpc.calls if m == "getTransaction"]
    assert fetched == [SIG]  # the failed and the venue-less notifications cost nothing
    assert rows(tmp_db)[0]["wallet"] == W1
    resume = fetch_all(tmp_db, "SELECT value FROM kv WHERE key = ?", (ss.KV_RESUME,))
    assert resume and int(resume[0]["value"]) == SLOT
    assert "SECRETKEY" not in json.dumps(fetch_all(tmp_db, "SELECT payload FROM events WHERE kind = 'system'"))


def test_every_excluded_wallet_is_named_in_its_own_system_event_and_not_subscribed(tmp_db, proven, monkeypatch):
    _wallet(tmp_db, W1)
    proven["value"] = frozenset({W2})
    rpc = FakeRpc()
    import time

    rpc.sigs[W2] = _sigs(1000, span_s=60, now=time.time())  # a bot in the proven cohort
    rpc.sigs[W1] = []
    seen: dict[str, Any] = {}

    async def fake_stream(url: str, wallets: Any, **kw: Any):
        seen.update(kw, wallets=tuple(wallets))
        kw["stop"].set()
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(ss, "stream", fake_stream)
    stop = asyncio.Event()

    async def go() -> None:
        async def later() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.ensure_future(later())
        await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db, rpc=_Client(rpc),
                     sol_usd=lambda ts: SOL_USD, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert seen["wallets"] == (W1,) and seen["anchor_on_subscribe"] is True
    (event,) = _system(tmp_db, "excluded")
    assert event["address"] == W2 and event["reason"] == "bot" and event["sigs"] == 1000
    assert event["rule"]["bot_max_txs_per_hour"] == ss.BOT_MAX_TXS_PER_HOUR
    subj = fetch_all(tmp_db, "SELECT subject, level FROM events WHERE kind = 'system' AND payload LIKE '%excluded\"%'")
    assert any(r["subject"] == W2 and r["level"] == "warn" for r in subj)
    assert _system(tmp_db, "starting")[0]["excluded"] == {W2: "bot"}


def test_with_no_wallet_to_track_the_feed_idles_and_says_so(tmp_db, proven):
    _wallet(tmp_db, W1, chain="bsc")
    proven["value"] = None

    async def go() -> None:
        stop = asyncio.Event()

        async def later() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.ensure_future(later())
        await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db, rpc=_Client(FakeRpc()),
                     sol_usd=lambda ts: SOL_USD, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert _system(tmp_db, "idle")[0]["reason"] == "no trusted_copy or proven sol wallet to track"


def test_without_an_alchemy_url_the_feed_is_idle(tmp_db, monkeypatch):
    monkeypatch.setattr(ss, "alchemy_url", lambda: None)
    assert asyncio.run(ss.run(asyncio.Event(), reader=tmp_db, writer=tmp_db)) == {"idle": "no websocket endpoint"}


def test_a_stored_resume_point_is_used_only_when_recent(tmp_db):
    ss._save_resume(tmp_db, SLOT)
    assert ss._resume_from_kv(tmp_db, SLOT + 100) == SLOT
    assert ss._resume_from_kv(tmp_db, SLOT + ss.RESUME_MAX_SLOTS + 1) is None
    assert ss._resume_from_kv(tmp_db, SLOT - 1) is None


# --------------------------------------------------------------------------------------
# lagging connections, parallel readers, the resume point, RPC errors
# --------------------------------------------------------------------------------------


def test_lag_watch_flags_a_slow_connection_once_and_ignores_stale_reads():
    lw = ss.LagWatch(limit_ms=8_000, window=5)
    assert [lw.add(1, 1_500) for _ in range(10)] == [None] * 10            # on time: never
    verdicts = [lw.add(2, 23_000) for _ in range(5)]
    assert verdicts == [None, None, None, None, 23_000]                    # a slow backend: once
    assert lw.add(2, 23_000) is None                                       # the window restarted
    assert [lw.add(1, 60_000) for _ in range(5)] == [None] * 5             # an older connection's queue
    assert [lw.add(3, v) for v in (30_000, 30_000, 1_000, 1_000, 1_000)][-1] is None  # median, not max


def test_the_resume_point_never_passes_a_slot_still_in_flight():
    rp = ss.ResumePoint()
    assert rp.value() is None
    rp.queued(10)
    rp.queued(20)
    rp.finished(20)
    assert rp.value() == 10           # 20 is read, 10 is not: a restart must go back to 10
    rp.finished(10)
    assert rp.value() == 20
    rp.queued(30)
    rp.queued(30)
    rp.finished(30)
    assert rp.value() == 30 and rp.waiting[30] == 1   # one of two slot-30 reads still in flight
    rp.queued(25)
    assert rp.value() == 25


def test_error_keys_are_short_and_secret_free():
    assert ss.error_key(ss.RpcFailure("getTransaction: http 429")) == "http:429"
    assert ss.error_key(ss.RpcFailure("getTransaction: {'code': -32005, 'message': 'slow'}")) == "code:-32005"
    assert ss.error_key(ss.RpcFailure("getTransaction: ReadTimeout: https://x/v2/***")) == "ReadTimeout"

    class TooMany:
        status_code = 429

        def json(self) -> Any:
            return {"jsonrpc": "2.0", "error": {"code": 429, "message": "rate"}}

    class Client:
        async def post(self, url: str, json: Any) -> Any:
            return TooMany()

    rpc = ss.SolRpc("https://s.example/v2/K", client=Client())
    e = ss.SolWalletStream(rpc.call, reader=None, writer=None, sol_usd=lambda ts: None, sleep=_no_sleep,
                           tx_retries=(0.1,))
    assert asyncio.run(e._get_tx(SIG)) is None
    assert e.stats.rpc_errors == {"http:429": 2} and e.stats.tx_null_answers == 0


def test_a_reconnect_request_drops_the_connection_and_backfills_its_gap(no_backoff):
    rpc = FakeRpc()
    rpc.slot_seq = [SLOT, SLOT + 10]  # the anchor of connection 1, then the head at the reconnect
    rpc.sigs[W1] = [{"signature": "sigMissed", "slot": SLOT + 5, "err": None, "blockTime": BLOCK_TIME},
                    {"signature": "sigFirst", "slot": SLOT + 1, "err": None, "blockTime": BLOCK_TIME}]
    server = FakeSolServer([{"pushes": [(W1, "sigFirst", SLOT + 1)]}, {"pushes": [(W1, "sigAfter", SLOT + 11)]}])
    stats = ss.SolFeedStats()
    out: list[ss.Notice] = []
    lagging = asyncio.Event()

    async def go() -> None:
        stop = asyncio.Event()
        async for rec in ss.stream("https://s.example/v2/K", [W1], rpc=rpc, stop=stop, connect=server.connect,
                                   stats=stats, poll_s=0.01, max_attempts=3, reconnect=lagging):
            out.append(rec)
            if rec.signature == "sigFirst":
                lagging.set()  # the listener's verdict about connection 1
            if len(out) == 3:
                stop.set()

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert [(n.signature, n.backfilled, n.conn) for n in out] == \
        [("sigFirst", False, 1), ("sigMissed", True, 2), ("sigAfter", False, 2)]
    assert stats.lag_reconnects == 1 and stats.connects == 2 and not lagging.is_set()
    assert stats.duplicates == 1  # sigFirst came back in the backfill and was not yielded twice


@pytest.mark.parametrize("behind_s,reconnects", [(60, True), (0, False)], ids=["lagging", "on-time-control"])
def test_run_reconnects_a_connection_whose_pushes_arrive_late(tmp_db, proven, no_backoff, behind_s, reconnects):
    import time as _time

    _wallet(tmp_db, W1)
    proven["value"] = frozenset()
    rpc = FakeRpc()
    rpc.sigs[W1] = []
    sigs = [f"late{i}" for i in range(ss.LAG_WINDOW)]
    bt = int(_time.time()) - behind_s
    for sig in sigs:
        rpc.txs[sig] = buy_tx(sig=sig, block_time=bt)
    server = FakeSolServer([{"pushes": [(W1, sig, SLOT, None, [f"Program {PUMP} invoke [1]"]) for sig in sigs]},
                            {"pushes": []}])
    stop = asyncio.Event()

    async def go() -> None:
        async def later() -> None:
            await asyncio.sleep(1.5)
            stop.set()

        asyncio.ensure_future(later())
        await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db, rpc=_Client(rpc),
                     sol_usd=lambda ts: SOL_USD, connect=server.connect, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=10))
    events = _system(tmp_db, "lagging_reconnect")
    assert len(rows(tmp_db)) == ss.LAG_WINDOW  # late data is still data: every trade is written
    if reconnects:
        assert len(events) == 1 and events[0]["median_push_lag_ms"] >= 59_000 and server.dials == 2
    else:
        assert events == [] and server.dials == 1


def test_a_transaction_that_never_answers_does_not_hold_up_the_next(tmp_db, proven, no_backoff, monkeypatch):
    """MEASURED on the box: one reader let each missing transaction's retries delay every
    trade behind it (block->row p90 8.2 s). With two readers the next trade goes through."""
    _wallet(tmp_db, W1)
    proven["value"] = frozenset()
    monkeypatch.setattr(ss, "TX_RETRIES", (0.4, 0.4))
    rpc = FakeRpc()
    rpc.sigs[W1] = []
    rpc.txs["good"] = buy_tx(sig="good")       # "stuck" is never answered
    logs = [f"Program {PUMP} invoke [1]"]
    server = FakeSolServer([{"pushes": [(W1, "stuck", SLOT, None, logs), (W1, "good", SLOT + 1, None, logs)]}])
    seen_stuck_calls: list[int] = []
    stop = asyncio.Event()

    def done(o: ss.Outcome) -> None:
        seen_stuck_calls.append(sum(1 for m, p in rpc.calls if m == "getTransaction" and p[0] == "stuck"))
        stop.set()

    asyncio.run(asyncio.wait_for(ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db,
                                        rpc=_Client(rpc), sol_usd=lambda ts: SOL_USD, connect=server.connect,
                                        resume=False, on_written=done), timeout=10))
    assert seen_stuck_calls and seen_stuck_calls[0] < 3  # "good" was written while "stuck" was still retrying


# --------------------------------------------------------------------------------------
# cost guard: mostly-failed wallets, the per-wallet budget, params, per-wallet cost
# --------------------------------------------------------------------------------------


def _failing_sigs(n: int, failed: int, *, span_s: float, now: float, prefix: str = "f") -> list[dict[str, Any]]:
    """``n`` signatures over ``span_s``, the first ``failed`` of them failed."""
    return [{"signature": f"{prefix}{i:06d}", "slot": SLOT - i, "blockTime": int(now - span_s * i / max(1, n - 1)),
             "err": {"InstructionError": [0, {"Custom": 1}]} if i < failed else None} for i in range(n)]


def test_mostly_failed_needs_the_share_and_enough_transactions():
    now = 1e6

    def rate(n: int, f: int) -> dict[str, Any]:
        return ss.signature_rate(_failing_sigs(n, f, span_s=36_000, now=now), now_s=now)

    assert rate(1000, 900)["failed"] == 900 and rate(1000, 900)["failed_share"] == 0.9
    assert ss.mostly_failed(rate(1000, 900))
    assert ss.mostly_failed(rate(50, 40))                       # exactly 0.8 of exactly 50
    assert not ss.mostly_failed(rate(49, 49))                   # too few to be evidence
    assert not ss.mostly_failed(rate(100, 79))                  # 0.79
    assert not ss.mostly_failed(rate(1000, 900), max_share=0.95)
    assert not ss.mostly_failed(rate(60, 60), min_tx=61)


def test_the_screen_excludes_a_mostly_failed_wallet_with_its_evidence(tmp_db):
    now = float(BLOCK_TIME)
    rpc = FakeRpc()
    rpc.sigs[W1] = _failing_sigs(1000, 890, span_s=36_000, now=now, prefix="a")   # 11 ok/h: no bot
    rpc.sigs[W2] = _failing_sigs(100, 79, span_s=36_000, now=now, prefix="b")
    e = engine(tmp_db, rpc, wallets=())
    excluded = asyncio.run(e.screen([W1, W2], now_s=now))
    assert set(excluded) == {W1}
    ev = excluded[W1]
    assert ev["reason"] == "failed" and ev["rules"] == ["failed"]
    assert ev["sigs"] == 1000 and ev["failed"] == 890 and ev["failed_share"] == 0.89
    assert not any(p[0].startswith("a") for m, p in rpc.calls if m == "getTransaction")  # nothing sampled
    # the threshold is a param: at 0.95 the same wallet is kept
    kept = asyncio.run(engine(tmp_db, rpc, wallets=()).screen([W1, W2], now_s=now, p={"max_failed_share": 0.95}))
    assert kept == {}
    # a bot that is also mostly failed (MEASURED: the 2,636 ok/h, 86.5%-failed proven wallet) reads as both
    rpc.sigs[W3] = _failing_sigs(1000, 865, span_s=180, now=now, prefix="c")
    both = asyncio.run(engine(tmp_db, rpc, wallets=()).screen([W3], now_s=now))
    assert both[W3]["reason"] == "bot" and both[W3]["rules"] == ["bot", "failed"]


def test_params_come_from_the_launch_snipe_lane_and_a_bad_value_never_wins():
    from kaiba.core.config import RiskConfig

    cfg = RiskConfig.model_validate({"lanes": {"launch-snipe": {"params": {"sol_wallets": {
        "max_failed_share": 0.9, "min_tx_for_failed_screen": "60", "max_notifications_per_wallet_per_hour": 500.0,
        "bogus_key": 1, "router_zero_net_share": 1.5, "bot_min_sigs": "many", "lag_window": -1}}}}})
    p = ss.params(cfg)
    assert p["max_failed_share"] == 0.9
    assert p["min_tx_for_failed_screen"] == 60 and isinstance(p["min_tx_for_failed_screen"], int)
    assert p["max_notifications_per_wallet_per_hour"] == 500
    for key in ("router_zero_net_share", "bot_min_sigs", "lag_window"):
        assert p[key] == ss.DEFAULT_PARAMS[key]
    assert "bogus_key" not in p
    assert ss.params(RiskConfig()) == ss.DEFAULT_PARAMS
    assert set(ss.PARAMS_PROVENANCE) == set(ss.DEFAULT_PARAMS)
    assert ss.DEFAULT_PARAMS["max_failed_share"] == 0.8 and ss.DEFAULT_PARAMS["min_tx_for_failed_screen"] == 50
    assert ss.DEFAULT_PARAMS["max_notifications_per_wallet_per_hour"] == 2000


def test_run_reads_the_params_and_names_a_mostly_failed_wallet(tmp_db, proven, monkeypatch):
    import time as _time

    _wallet(tmp_db, W1)
    _wallet(tmp_db, W2)
    proven["value"] = frozenset()
    rpc = FakeRpc()
    rpc.sigs[W1] = _failing_sigs(200, 150, span_s=36_000, now=_time.time())  # 75%: the default keeps it
    rpc.sigs[W2] = []
    asked: list[int] = []

    def fake_params(cfg: Any = None) -> dict[str, Any]:
        asked.append(1)
        return {**ss.DEFAULT_PARAMS, "max_failed_share": 0.7, "min_tx_for_failed_screen": 100}

    monkeypatch.setattr(ss, "params", fake_params)
    seen: dict[str, Any] = {}

    async def fake_stream(url: str, wallets: Any, **kw: Any):
        seen.update(kw, wallets=tuple(wallets))
        kw["stop"].set()
        return
        yield  # pragma: no cover

    monkeypatch.setattr(ss, "stream", fake_stream)
    stop = asyncio.Event()

    async def go() -> None:
        async def later() -> None:
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.ensure_future(later())
        await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db, rpc=_Client(rpc),
                     sol_usd=lambda ts: SOL_USD, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert asked and seen["wallets"] == (W2,)
    (event,) = _system(tmp_db, "excluded")
    assert event["address"] == W1 and event["reason"] == "failed"
    assert (event["sigs"], event["failed"], event["failed_share"]) == (200, 150, 0.75)
    assert event["rule"]["max_failed_share"] == 0.7 and event["rule"]["min_tx_for_failed_screen"] == 100


def test_the_notification_budget_is_a_rolling_hour():
    b = ss.NotificationBudget(3)
    assert [b.add(W1, 60.0 * m) for m in (0, 0, 10, 59)] == [1, 2, 3, 4]
    assert b.over(4) and not b.over(3)
    assert b.add(W1, 60.0 * 61) == 3       # the two from minute 0 have aged out: 10, 59, 61 remain
    assert b.add(W2, 0.0) == 1             # per wallet
    assert not ss.NotificationBudget(0).over(10 ** 6)   # 0 = off


def test_a_muted_wallet_is_unsubscribed_and_its_in_flight_pushes_are_dropped(no_backoff):
    server = FakeSolServer([{"expect_subs": 2, "pushes": [(W1, "sigA", SLOT), (W1, "sigB", SLOT + 1),
                                                          (W2, "sigC", SLOT + 2)]}])
    muted: set[str] = set()
    meter = ss.WalletMeter()
    out: list[ss.Notice] = []
    statuses: list[dict[str, Any]] = []
    stats = ss.SolFeedStats()

    async def go() -> None:
        stop = asyncio.Event()
        async for rec in ss.stream("https://s.example/v2/K", [W1, W2], rpc=FakeRpc(), stop=stop,
                                   connect=server.connect, stats=stats, on_status=statuses.append, poll_s=0.01,
                                   max_attempts=1, muted=muted, meter=meter):
            out.append(rec)
            if rec.signature == "sigA":
                muted.add(W1)  # the listener's budget verdict
            if rec.signature == "sigC":
                stop.set()

    asyncio.run(asyncio.wait_for(go(), timeout=5))
    assert [n.signature for n in out] == ["sigA", "sigC"]
    w1_sid = next(r["params"][0] for _, r in server.requests if r["method"] == "logsUnsubscribe")
    assert server.unsubscribed == [(0, w1_sid)] and stats.unsubscribes == 1 and stats.muted_dropped == 1
    assert any(s["event"] == "unsubscribed" for s in statuses)
    assert meter.notices[W1] == 2 and meter.bytes[W1] > 0  # the dropped push still cost bytes, and is counted
    # a new connection does not subscribe a muted wallet at all
    server2 = FakeSolServer([{"pushes": [(W2, "sigD", SLOT)]}])

    async def again() -> None:
        stop = asyncio.Event()
        async for _rec in ss.stream("https://s.example/v2/K", [W1, W2], rpc=FakeRpc(), stop=stop,
                                    connect=server2.connect, poll_s=0.01, max_attempts=1, muted={W1}):
            stop.set()

    asyncio.run(asyncio.wait_for(again(), timeout=5))
    subs = [r["params"][0]["mentions"] for _, r in server2.requests if r["method"] == "logsSubscribe"]
    assert subs == [[W2]]


def test_run_mutes_a_wallet_over_its_budget_until_the_next_screen(tmp_db, proven, no_backoff, monkeypatch):
    _wallet(tmp_db, W1)
    _wallet(tmp_db, W2)
    proven["value"] = frozenset()
    monkeypatch.setattr(ss, "params", lambda cfg=None: {**ss.DEFAULT_PARAMS,
                                                        "max_notifications_per_wallet_per_hour": 3})
    rpc = FakeRpc()
    rpc.sigs[W1], rpc.sigs[W2] = [], []
    noisy = [(W1, f"fail{i}", SLOT + i, {"e": 1}) for i in range(6)]
    server = FakeSolServer([{"expect_subs": 2, "pushes": noisy}, {"expect_subs": 2, "pushes": []}])
    stop = asyncio.Event()
    result: dict[str, Any] = {}

    async def go() -> None:
        async def later() -> None:
            while server.dials < 2:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            stop.set()

        asyncio.ensure_future(later())
        result.update(await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db,
                                   rpc=_Client(rpc), sol_usd=lambda ts: SOL_USD, connect=server.connect,
                                   resume=False, refresh_s=0.5))

    asyncio.run(asyncio.wait_for(go(), timeout=10))
    (event,) = [e for e in _system(tmp_db, "excluded") if e.get("reason") == "notification_budget"]
    assert event["address"] == W1 and event["notifications_last_hour"] == 4
    assert event["rule"]["max_notifications_per_wallet_per_hour"] == 3 and event["until"] == "next screen"
    assert [n for n, _ in server.unsubscribed] == [0]               # unsubscribed on the live connection
    resub = [r["params"][0]["mentions"][0] for n, r in server.requests if n == 1 and r["method"] == "logsSubscribe"]
    assert sorted(resub) == sorted([W1, W2]) and result["muted"] == []  # the next screen lifted it


def test_the_meter_prices_each_wallet_per_hour():
    clock = {"t": 0.0}
    m = ss.WalletMeter(clock=lambda: clock["t"])
    for i in range(10):
        m.note_notice(W1, failed=i < 9, nbytes=1000)
    for _ in range(3):
        m.note_call(W1, "getTransaction")
    m.note_call(W1, "getSignaturesForAddress")
    m.note_call(None, "getTransaction")  # unattributed: not charged to anyone
    clock["t"] = 1800.0
    snap = m.snapshot()
    assert snap == {W1: {"notifications_per_hour": 20.0, "failed_per_hour": 18.0, "reads_per_hour": 6.0,
                         "stream_bytes_per_hour": 20_000,
                         "est_cu_per_hour": round((4 * 40 + 10_000 * ss.CU_PER_STREAM_BYTE) / 0.5, 1),
                         "est_cu_per_hour_at_evm_byte_rate": round((4 * 40 + 10_000 * 0.04) / 0.5, 1)}}
    assert m.snapshot() == {}  # reset


def test_reads_are_charged_to_the_wallet_whose_push_triggered_them(tmp_db):
    rpc = FakeRpc()
    rpc.txs[SIG] = buy_tx()
    rpc.null_first[SIG] = 1
    e = engine(tmp_db, rpc)
    e.meter = ss.WalletMeter()
    process(e)
    assert e.meter.calls[W1] == {"getTransaction": 2}


def test_the_stats_event_carries_each_wallets_cost_per_hour(tmp_db, proven, no_backoff, monkeypatch):
    _wallet(tmp_db, W1)
    proven["value"] = frozenset()
    monkeypatch.setattr(ss, "STATS_EVERY_S", -1.0)  # every notice
    rpc = FakeRpc()
    rpc.sigs[W1] = []
    rpc.txs[SIG] = buy_tx()
    server = FakeSolServer([{"pushes": [(W1, SIG, SLOT, None, [f"Program {PUMP} invoke [1]"]),
                                        (W1, "sigLater", SLOT + 1, {"e": 1})]}])
    stop = asyncio.Event()

    async def go() -> None:
        async def later() -> None:
            await asyncio.sleep(0.5)
            stop.set()

        asyncio.ensure_future(later())
        await ss.run(stop, url="https://s.example/v2/K", reader=tmp_db, writer=tmp_db, rpc=_Client(rpc),
                     sol_usd=lambda ts: SOL_USD, connect=server.connect, resume=False)

    asyncio.run(asyncio.wait_for(go(), timeout=10))
    stats = [s for s in _system(tmp_db, "stats") if s.get("per_wallet")]
    assert stats
    row = {k: sum(s["per_wallet"].get(W1, {}).get(k, 0) for s in stats)
           for k in ("notifications_per_hour", "est_cu_per_hour", "stream_bytes_per_hour")}
    assert row["notifications_per_hour"] > 0 and row["est_cu_per_hour"] > 0 and row["stream_bytes_per_hour"] > 0
    assert all("est_cu_per_hour_at_evm_byte_rate" in v for s in stats for v in s["per_wallet"].values())
