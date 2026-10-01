"""Realised fill prices: the ratio, the decimals, the USD refusal, and the chain check.

Coverage is not the point. Five properties are, and each one is a way this module could
put a wrong number into a stop on real money:

* the price must come from the fill's own two legs, exactly, in ``Decimal``;
* a USD figure must come from a *contemporaneous* native sample or not exist at all;
* decimals must come from the chain, never from a guess — a wrong guess is a 10^12 error;
* a fill reconstructed from the chain must be comparable to what the venue claimed, and a
  disagreement must be recorded rather than silently resolved;
* nothing here may call ``float()``.

The chain fixtures under ``tests/fixtures/fills/`` are real responses recorded from the
public Solana RPC on 2026-09-20, not hand-written shapes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core import events as ev
from kaiba.core.db import fetch_all, fetch_one, jdump
from kaiba.core.schemas import (
    Chain,
    EvidenceBasis,
    Lane,
    LaneMode,
    Order,
    OrderState,
    Receipt,
    Side,
)
from kaiba.execution import fills
from kaiba.providers._http import Fetched

FIXTURES = Path(__file__).parent / "fixtures" / "fills"

#: The real pump.fun buy in the recorded transaction fixture.
SIG = "DDQ2SFZ4exsy5jr6aJusbyrD1RqB4uDWG5oAaUdVFLR5bPkZt1xmGQwJJj41aZeWGWf3NYB9HwgCP9UAuCn3E8X"
MINT = "3GzeyKuMi3QNuQbSWcxASLx3kxmgiYxxYZyFS2SXpump"
TRADER = "dev12bVcv5ZLjo7eYgZcSmZ7KBjEVfnfvorwqdZ14fo"
CURVE_LAMPORTS = 299_555_554
CURVE_ATOMS = 10_599_860_362_927
BLOCK_MS = 1_789_893_363_000

TOKEN = "CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb"
NATIVE = "So11111111111111111111111111111111111111112"
ONE_SOL = 1_000_000_000
NOW = 1_780_000_000_000


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"), parse_float=Decimal)


def ok(data: Any) -> Fetched:
    return Fetched(data, Receipt(provider="rpc", endpoint="sol.getTransaction",
                                 basis=EvidenceBasis.PROVIDER_REPORTED))


def down(note: str = "ConnectError: refused") -> Fetched:
    return Fetched(None, Receipt(provider="rpc", endpoint="sol.getTransaction",
                                 basis=EvidenceBasis.UNAVAILABLE, note=note))


@pytest.fixture
def rpc(monkeypatch):
    """Stand in for the JSON-RPC transport; queue responses, inspect the calls."""
    state: dict[str, Any] = {"queue": [], "calls": []}

    def fake(provider: str, endpoint: str, url: str, **kw: Any) -> Fetched:
        state["calls"].append({"provider": provider, "endpoint": endpoint, "url": url, **kw})
        return state["queue"].pop(0) if state["queue"] else down("no stubbed response")

    monkeypatch.setattr(fills, "post_json", fake)
    return state


def make_order(
    side: Side = Side.BUY,
    *,
    order_id: str = "ord_1",
    amount_in: int = ONE_SOL,
    filled_out: int | None = 1_000_000,
    fee_native: int | None = None,
    token: str = TOKEN,
    tx_hash: str | None = "0xfeed",
    updated_ms: int = NOW,
) -> Order:
    buy = side is Side.BUY
    return Order(
        order_id=order_id, decision_id="dec_1", chain=Chain.SOL, token=token, side=side,
        lane=Lane.CONFLUENCE_5, mode=LaneMode.LIVE,
        input_token=NATIVE if buy else token, output_token=token if buy else NATIVE,
        amount_in=amount_in, min_out=0, slippage_bps=300, state=OrderState.FILLED,
        provider="gmgn", tx_hash=tx_hash, filled_out=filled_out, fee_native=fee_native,
        created_ms=NOW, updated_ms=updated_ms,
    )


def put_decimals(conn, decimals: int = 6, token: str = TOKEN, *, verified: bool = True) -> None:
    meta = {"decimals_source": "verified_onchain"} if verified else {}
    conn.execute(
        "INSERT OR REPLACE INTO tokens (chain, address, decimals, first_seen_ms, meta_json) "
        "VALUES (?,?,?,?,?)",
        (Chain.SOL.value, token, decimals, NOW, jdump(meta)),
    )


def put_native(conn, ts_ms: int, price: str, chain: Chain = Chain.SOL) -> None:
    receipt = Receipt(provider="test", endpoint="native", observed_at_ms=ts_ms,
                      basis=EvidenceBasis.PROVIDER_REPORTED)
    conn.execute(
        "INSERT OR REPLACE INTO native_prices (chain, ts_ms, price_usd, source, receipt_json) "
        "VALUES (?,?,?,?,?)",
        (chain.value, ts_ms, price, "test", receipt.model_dump_json()),
    )


# --------------------------------------------------------------------------------------
# the ratio: the whole point of the module
# --------------------------------------------------------------------------------------


def test_the_ratio_is_the_realised_price_from_two_integers():
    """1 SOL for 1.0 six-decimal token is 1 SOL per token. No provider involved."""
    assert fills.ratio(ONE_SOL, 1_000_000, 9, 6) == Decimal(1)


def test_the_ratio_includes_the_slippage_actually_paid():
    """The dossier quote said 1 SOL/token; we paid 1 SOL for 0.9 tokens. That is the point.

    The old estimate would have recorded 1.0 and cut a stop from it. The realised price is
    11% higher, and so is the stop.
    """
    realised = fills.ratio(ONE_SOL, 900_000, 9, 6)
    assert realised > Decimal(1)
    assert realised.quantize(Decimal("1E-9")) == Decimal("1.111111111")
    premium_pct = (realised - Decimal(1)) * 100
    assert premium_pct.quantize(Decimal("1E-2")) == Decimal("11.11")


def test_the_ratio_is_exact_for_a_power_of_ten_and_never_a_float():
    assert fills.ratio(1, 1, 9, 9) == Decimal(1)
    assert fills.ratio(1, 1, 9, 6) == Decimal("0.001")
    assert fills.ratio(10**18, 1, 9, 0) == Decimal(10**9)


def test_the_ratio_survives_a_quantity_beyond_two_to_the_sixty_three():
    huge = 2**70 + 12345
    assert fills.ratio(huge, huge, 9, 9) == Decimal(1)


def test_the_ratio_matches_a_real_recorded_pump_fun_fill():
    """The numbers from the recorded transaction: 0.299555554 SOL for 10,599,860.362927 tokens."""
    price = fills.ratio(CURVE_LAMPORTS, CURVE_ATOMS, 9, 6)
    assert price.quantize(Decimal("1E-18")) == Decimal("0.000000028260330207")
    # and it multiplies back out to the lamports that actually left the wallet
    back = price * Decimal(CURVE_ATOMS) / Decimal(10**6) * Decimal(10**9)
    assert back.quantize(Decimal("1")) == Decimal(CURVE_LAMPORTS)


@pytest.mark.parametrize("native,token", [(0, 1), (1, 0), (-1, 1), (1, -1), (0, 0)])
def test_a_leg_that_is_not_positive_is_refused_rather_than_priced(native, token):
    with pytest.raises(ValueError, match="positive"):
        fills.ratio(native, token, 9, 6)


def test_negative_decimals_are_refused():
    with pytest.raises(ValueError, match="decimals"):
        fills.ratio(1, 1, 9, -1)


# --------------------------------------------------------------------------------------
# decimals from the chain
# --------------------------------------------------------------------------------------


def test_decimals_are_read_from_a_real_mint_account(tmp_db, rpc):
    put_decimals(tmp_db, 0, verified=False)  # a row exists, unverified
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_token2022")))
    dec, basis, note = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec == 6
    assert basis == fills.DECIMALS_VERIFIED


def test_decimals_are_read_from_a_classic_spl_mint_too(tmp_db, rpc):
    put_decimals(tmp_db, 0, verified=False)
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_usdc")))
    dec, basis, _ = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec == 6 and basis == fills.DECIMALS_VERIFIED


def test_verified_decimals_are_written_back_and_not_fetched_twice(tmp_db, rpc):
    put_decimals(tmp_db, 0, verified=False)
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_token2022")))
    fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert len(rpc["calls"]) == 1
    row = fetch_one(tmp_db, "SELECT decimals, meta_json FROM tokens WHERE address=?", (TOKEN,))
    assert row["decimals"] == 6
    assert "verified_onchain" in row["meta_json"]
    dec, basis, _ = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec == 6 and basis == fills.DECIMALS_VERIFIED
    assert len(rpc["calls"]) == 1, "a verified value is never re-fetched"


def test_a_decimals_conflict_with_the_stored_row_is_loud(tmp_db, rpc):
    """The chain is the authority, and whatever sized on the old value was wrong."""
    put_decimals(tmp_db, 18, verified=False)
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_token2022")))
    dec, _, _ = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec == 6
    loud = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "token_decimals_conflict"]
    assert loud and loud[0].level == "error"
    assert loud[0].payload["tokens_row"] == 18 and loud[0].payload["onchain"] == 6


def test_an_account_that_is_not_a_mint_yields_no_decimals(tmp_db, rpc):
    put_decimals(tmp_db, 0, verified=False)
    rpc["queue"].append(ok(fixture("getAccountInfo_not_a_mint")))
    dec, basis, note = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec == 0, "the unverified row value survives as a fallback"
    assert basis == fills.DECIMALS_TOKENS_ROW
    assert "not parsed token data" in (note or "")


def test_no_row_and_no_chain_answer_is_unavailable_not_a_guess(tmp_db, rpc):
    """``paper.py`` would return 6 here. The live path must not."""
    rpc["queue"].append(down("HTTP 503"))
    dec, basis, note = fills.token_decimals(Chain.SOL, TOKEN, tmp_db, register=True)
    assert dec is None
    assert basis == fills.DECIMALS_UNAVAILABLE
    assert "503" in (note or "")


def test_an_unregistered_token_is_not_enriched_from_the_execution_path(tmp_db, rpc):
    dec, basis, note = fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert dec is None and basis == fills.DECIMALS_UNAVAILABLE
    assert rpc["calls"] == [], "no chain read for a token we have never ingested"
    assert "not in the tokens registry" in (note or "")


def test_register_creates_the_row_for_a_deliberate_caller(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_token2022")))
    dec, basis, _ = fills.token_decimals(Chain.SOL, TOKEN, tmp_db, register=True)
    assert dec == 6 and basis == fills.DECIMALS_VERIFIED
    assert fetch_one(tmp_db, "SELECT decimals FROM tokens WHERE address=?", (TOKEN,))["decimals"] == 6


def test_the_decimals_read_waits_for_limiter_capacity(tmp_db, rpc):
    put_decimals(tmp_db, 0, verified=False)
    rpc["queue"].append(ok(fixture("getAccountInfo_mint_token2022")))
    fills.token_decimals(Chain.SOL, TOKEN, tmp_db)
    assert rpc["calls"][0]["wait_for_slot_s"] > 0
    assert rpc["calls"][0]["cache_key"], "JSON-RPC calls share a URL; the key must disambiguate"


def test_the_evm_decimals_word_is_parsed():
    result = fixture("eth_call_decimals_18_synthetic")["result"]
    assert fills.parse_evm_decimals(result) == (18, "ok")


@pytest.mark.parametrize(
    "raw,why",
    [("0x", "empty"), ("not hex", "malformed"), (None, "malformed"),
     ("0x" + "f" * 64, "reports decimals")],
)
def test_a_bad_evm_decimals_answer_is_refused(raw, why):
    dec, note = fills.parse_evm_decimals(raw)
    assert dec is None and why in note


# --------------------------------------------------------------------------------------
# derive: the fill price and its basis
# --------------------------------------------------------------------------------------


def test_derive_produces_a_measured_usd_price_from_a_contemporaneous_sample(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    assert fp.basis == fills.BASIS_FILL_RATIO
    assert fp.price_native_per_token == Decimal(1)
    assert fp.price_usd == Decimal(200)
    assert fp.native_sample_distance_ms == 0
    assert fp.usable_for_stop is True


def test_derive_refuses_usd_when_no_sample_is_contemporaneous(tmp_db):
    """The refusal this module exists for: never today's price on an old fill."""
    put_decimals(tmp_db)
    put_native(tmp_db, NOW - 86_400_000, "200")  # a day earlier
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    assert fp.basis == fills.BASIS_FILL_RATIO_NATIVE_ONLY
    assert fp.price_usd is None
    assert fp.price_native_per_token == Decimal(1), "the native ratio is still exact"
    assert fp.usable_for_stop is False
    assert any("not contemporaneous" in n for n in fp.notes)


def test_derive_without_decimals_has_no_price_at_all(tmp_db):
    put_native(tmp_db, NOW, "200")
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    assert fp.basis == fills.BASIS_UNAVAILABLE
    assert fp.price_native_per_token is None and fp.price_usd is None
    assert fp.usable_for_stop is False


def test_derive_refuses_a_fill_with_a_missing_leg(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=0, conn=tmp_db)
    assert fp.basis == fills.BASIS_UNAVAILABLE
    assert any("positive" in n for n in fp.notes)


def test_derive_carries_the_fee_inclusive_variant_separately(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = make_order(fee_native=100_000_000)  # 0.1 SOL
    fp = fills.derive(order, native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    assert fp.price_native_per_token == Decimal(1)
    assert fp.price_native_all_in == Decimal("1.1")


def test_derive_uses_a_block_time_over_the_order_row_when_one_is_known(tmp_db):
    """``updated_ms`` is when we recorded the fill; the block time is when it happened."""
    put_decimals(tmp_db)
    put_native(tmp_db, BLOCK_MS, "200")
    tmp_db.execute(
        "INSERT INTO fill_reconciliations (order_id, chain, signature, wallet, token, verdict, "
        "block_time_ms, checked_ms) VALUES (?,?,?,?,?,?,?,?)",
        ("ord_1", Chain.SOL.value, SIG, TRADER, TOKEN, "agree", BLOCK_MS, NOW),
    )
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    assert fp.fill_ts_basis == "block_time"
    assert fp.fill_ts_ms == BLOCK_MS
    assert fp.price_usd == Decimal(200)


def test_a_caller_supplied_timestamp_wins(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW + 600_000, "300")
    fp = fills.derive(make_order(), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db,
                      fill_ts_ms=NOW + 600_000, fill_ts_basis="caller")
    assert fp.fill_ts_basis == "caller" and fp.price_usd == Decimal(300)


def test_derive_never_calls_float(tmp_db, monkeypatch):
    def banned(*a, **kw):
        raise AssertionError("float() must never touch a fill price")

    monkeypatch.setattr(fills, "float", banned, raising=False)
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "0.1")
    fp = fills.derive(make_order(amount_in=2**70), native_atoms=2**70, token_atoms=3,
                      conn=tmp_db, decimals=6)
    assert fp.price_usd is not None


def test_a_derivation_round_trips_through_the_table(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    fp = fills.derive(make_order(fee_native=7), native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db)
    fills.record(tmp_db, fp)
    back = fills.load(tmp_db, "ord_1")
    assert back is not None
    assert back.price_usd == fp.price_usd
    assert back.price_native_per_token == fp.price_native_per_token
    assert back.basis == fp.basis and back.token_decimals == 6
    assert back.notes == fp.notes


def test_a_recomputation_replaces_rather_than_duplicates(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = make_order()
    fills.record(tmp_db, fills.derive(order, native_atoms=ONE_SOL, token_atoms=1_000_000, conn=tmp_db))
    fills.record(tmp_db, fills.derive(order, native_atoms=ONE_SOL, token_atoms=500_000, conn=tmp_db))
    rows = fetch_all(tmp_db, "SELECT * FROM fill_prices WHERE order_id='ord_1'", ())
    assert len(rows) == 1
    assert rows[0]["price_native_per_token"] == "2"


def test_money_columns_are_text_not_real(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    fills.record(tmp_db, fills.derive(make_order(), native_atoms=2**70, token_atoms=1_000_000,
                                      conn=tmp_db))
    row = fetch_one(tmp_db, "SELECT native_atoms, price_usd FROM fill_prices", ())
    assert row["native_atoms"] == str(2**70)
    assert isinstance(row["price_usd"], str)


# --------------------------------------------------------------------------------------
# parsing a real transaction
# --------------------------------------------------------------------------------------


def test_a_real_pump_fun_buy_parses_into_its_two_legs():
    got = fills.parse_solana_transaction(
        fixture("getTransaction_pumpfun_buy")["result"], token=MINT, wallet=TRADER, signature=SIG
    )
    assert got is not None
    assert got.wallet_token_delta == CURVE_ATOMS
    assert got.buying is True
    assert got.venue_leg == CURVE_LAMPORTS
    assert got.block_time_ms == BLOCK_MS
    assert got.token_decimals == 6
    assert got.failed is False
    assert got.tx_fee == 28_645


def test_the_wallet_leg_is_the_net_cost_without_the_chain_fee():
    got = fills.parse_solana_transaction(
        fixture("getTransaction_pumpfun_buy")["result"], token=MINT, wallet=TRADER, signature=SIG
    )
    assert got.all_in_native == 304_912_484, "everything that left the wallet"
    assert got.wallet_leg == 304_912_484 - 28_645


def test_the_signer_is_used_when_no_wallet_is_named():
    got = fills.parse_solana_transaction(
        fixture("getTransaction_pumpfun_buy")["result"], token=MINT, signature=SIG
    )
    assert got is not None and got.wallet == TRADER


def test_a_transaction_that_did_not_move_this_token_is_not_a_fill():
    got = fills.parse_solana_transaction(
        fixture("getTransaction_pumpfun_buy")["result"], token=TOKEN, wallet=TRADER, signature=SIG
    )
    assert got is None


def test_a_wallet_that_did_not_trade_is_not_a_fill():
    got = fills.parse_solana_transaction(
        fixture("getTransaction_pumpfun_buy")["result"], token=MINT,
        wallet="9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin", signature=SIG
    )
    assert got is None


@pytest.mark.parametrize("bad", [None, {}, {"meta": {}}, "nonsense", 42])
def test_a_malformed_transaction_never_raises(bad):
    assert fills.parse_solana_transaction(bad, token=MINT, wallet=TRADER) is None


# --------------------------------------------------------------------------------------
# reconciliation against the chain
# --------------------------------------------------------------------------------------


def test_a_matching_fill_agrees_via_the_venue_leg(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.BUY,
        claimed_native=CURVE_LAMPORTS, claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.agrees
    assert rec.delta_tokens == 0 and rec.delta_native == 0
    assert rec.native_basis == "venue_leg"
    assert rec.block_time_ms == BLOCK_MS


def test_the_wallet_leg_carries_a_route_the_venue_leg_cannot_see(tmp_db, rpc):
    """An aggregator hops through pools we never identify; the venue leg reads 0.

    Measured on 2026-09-20: over 56 live swaps the wallet leg matched the venue's own
    figure 41 times and the venue leg 10; neither ever contradicted the other.
    """
    raw = fixture("getTransaction_pumpfun_buy")["result"]
    raw["meta"]["preTokenBalances"] = [
        b for b in raw["meta"]["preTokenBalances"] if b.get("owner") == TRADER
    ]
    raw["meta"]["postTokenBalances"] = [
        b for b in raw["meta"]["postTokenBalances"] if b.get("owner") == TRADER
    ]
    rpc["queue"].append(ok({"jsonrpc": "2.0", "result": raw}))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.BUY,
        claimed_native=304_912_484 - 28_645, claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.agrees and rec.native_basis == "wallet_leg"
    assert rec.delta_native == 0


def test_a_token_quantity_that_differs_by_one_atom_disagrees(tmp_db, rpc):
    """Tokens must match exactly. A venue has no reason to report a different quantity."""
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.BUY,
        claimed_native=CURVE_LAMPORTS, claimed_tokens=CURVE_ATOMS + 1, conn=tmp_db,
    )
    assert rec.verdict == fills.VERDICT_DISAGREE
    assert rec.delta_tokens == -1


def test_a_native_leg_outside_the_band_disagrees_and_names_both_measures(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.BUY,
        claimed_native=CURVE_LAMPORTS * 2, claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.verdict == fills.VERDICT_DISAGREE
    assert rec.native_basis == "none"
    assert "wallet_leg" in rec.detail and "venue_leg" in rec.detail


def test_a_disagreement_is_loud_and_journalled_and_changes_nothing(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.BUY,
        claimed_native=CURVE_LAMPORTS, claimed_tokens=1, conn=tmp_db,
    )
    from kaiba.core import journal

    loud = [e for e in ev.recent(conn=tmp_db)
            if e.payload.get("event") == "fill_reconciliation_mismatch"]
    assert loud and loud[0].level == "error"
    assert any(e["kind"] == "correction" for e in journal.read(conn=tmp_db))
    assert fetch_all(tmp_db, "SELECT * FROM positions", ()) == []


def test_a_claimed_side_the_chain_contradicts_disagrees(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, side=Side.SELL,
        claimed_native=CURVE_LAMPORTS, claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.verdict == fills.VERDICT_DISAGREE
    assert "wallet's token balance says buy" in rec.detail


def test_a_failed_transaction_settled_nothing(tmp_db, rpc):
    raw = fixture("getTransaction_pumpfun_buy")
    raw["result"]["meta"]["err"] = {"InstructionError": [2, {"Custom": 6002}]}
    rpc["queue"].append(ok(raw))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, claimed_native=CURVE_LAMPORTS,
        claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.verdict == fills.VERDICT_DISAGREE
    assert "failed on chain" in rec.detail


def test_an_unreachable_chain_is_unavailable_not_a_disagreement(tmp_db, rpc):
    """A dead RPC must never read as "the venue lied"."""
    rpc["queue"].append(down("HTTP 503"))
    rec = fills.reconcile_onchain(
        Chain.SOL, SIG, MINT, wallet=TRADER, claimed_native=CURVE_LAMPORTS,
        claimed_tokens=CURVE_ATOMS, conn=tmp_db,
    )
    assert rec.verdict == fills.VERDICT_UNAVAILABLE
    assert not rec.agrees
    assert [e for e in ev.recent(conn=tmp_db)
            if e.payload.get("event") == "fill_reconciliation_mismatch"] == []


def test_an_rpc_level_error_is_not_cached_as_an_answer(tmp_db, rpc, monkeypatch):
    """A JSON-RPC error arrives as HTTP 200 and ``_http`` caches it. Purge it, or a
    transient refusal is replayed from disk for an hour and a fix looks like a no-op."""
    purged: list[str] = []
    monkeypatch.setattr(fills, "_purge_cache", lambda e, k: purged.append(k))
    rpc["queue"].append(ok({"jsonrpc": "2.0", "error": {"code": -32015, "message": "version"}}))
    rec = fills.reconcile_onchain(Chain.SOL, SIG, MINT, wallet=TRADER, conn=tmp_db)
    assert rec.verdict == fills.VERDICT_UNAVAILABLE
    assert purged, "the cached error must be dropped"


def test_the_transaction_read_accepts_any_transaction_version(tmp_db, rpc):
    """Pinning this at 0 made 21% of a live pump.fun sample unreadable, and an unreadable
    fill is a position with no entry price and therefore no stop."""
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    fills.reconcile_onchain(Chain.SOL, SIG, MINT, wallet=TRADER, conn=tmp_db)
    params = rpc["calls"][0]["json_body"]["params"]
    assert params[1]["maxSupportedTransactionVersion"] >= 1
    assert rpc["calls"][0]["wait_for_slot_s"] > 0


def test_a_reconciliation_is_appended_never_overwritten(tmp_db, rpc):
    """Two checks of the same fill are two rows. Neither replaces the recorded fill."""
    for _ in range(2):
        rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
        fills.reconcile_onchain(
            Chain.SOL, SIG, MINT, wallet=TRADER, claimed_native=CURVE_LAMPORTS,
            claimed_tokens=CURVE_ATOMS, order_id="ord_1", conn=tmp_db,
        )
    rows = fetch_all(tmp_db, "SELECT * FROM fill_reconciliations WHERE order_id='ord_1'", ())
    assert len(rows) == 2
    assert all(r["onchain_tokens"] == str(CURVE_ATOMS) for r in rows)


def test_store_false_checks_without_recording(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    fills.reconcile_onchain(Chain.SOL, SIG, MINT, wallet=TRADER, conn=tmp_db, store=False)
    assert fetch_all(tmp_db, "SELECT * FROM fill_reconciliations", ()) == []


def test_reconciling_an_order_uses_the_row_as_the_claim(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    order = make_order(amount_in=CURVE_LAMPORTS, filled_out=CURVE_ATOMS, token=MINT, tx_hash=SIG)
    rec = fills.reconcile_order(order, tmp_db, wallet=TRADER)
    assert rec.agrees and rec.order_id == "ord_1"
    assert rec.claimed_native == CURVE_LAMPORTS and rec.claimed_tokens == CURVE_ATOMS


def test_reconciling_a_sell_order_reads_the_legs_the_other_way_round(tmp_db, rpc):
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    order = make_order(Side.SELL, amount_in=CURVE_ATOMS, filled_out=CURVE_LAMPORTS,
                       token=MINT, tx_hash=SIG)
    rec = fills.reconcile_order(order, tmp_db, wallet=TRADER)
    assert rec.claimed_native == CURVE_LAMPORTS and rec.claimed_tokens == CURVE_ATOMS


def test_an_order_with_no_signature_cannot_be_reconciled(tmp_db, rpc):
    rec = fills.reconcile_order(make_order(tx_hash=None), tmp_db)
    assert rec.verdict == fills.VERDICT_UNAVAILABLE
    assert "no transaction signature" in rec.detail
    assert rpc["calls"] == []


def test_a_swap_row_reconciles_against_the_chain(tmp_db, rpc):
    """How the pump.fun trade feed was checked: the row is the claim."""
    rpc["queue"].append(ok(fixture("getTransaction_pumpfun_buy")))
    row = {"chain": "sol", "tx": SIG, "token": MINT, "wallet": TRADER, "side": "buy",
           "amount_native": str(CURVE_LAMPORTS), "amount_token": str(CURVE_ATOMS)}
    rec = fills.reconcile_swap_row(row, tmp_db)
    assert rec.agrees and rec.order_id is None


def test_an_evm_reconciliation_says_it_is_not_implemented(tmp_db, rpc):
    rec = fills.reconcile_onchain(Chain.ETH, "0xabc", "0x" + "1" * 40, conn=tmp_db)
    assert rec.verdict == fills.VERDICT_UNAVAILABLE
    assert "not implemented" in rec.detail
    assert rpc["calls"] == []


# --------------------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------------------


def persist(conn, order: Order) -> Order:
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "provider_order_id, tx_hash, filled_out, fee_native, created_ms, updated_ms, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order.order_id, order.decision_id, order.chain.value, order.token, order.side.value,
         order.lane.value, order.mode.value, order.input_token, order.output_token,
         str(order.amount_in), str(order.min_out), order.slippage_bps, order.state.value,
         order.provider, order.provider_order_id, order.tx_hash,
         None if order.filled_out is None else str(order.filled_out),
         None if order.fee_native is None else str(order.fee_native),
         order.created_ms, order.updated_ms, order.error),
    )
    return order


def test_backfill_prices_every_filled_order_from_its_own_legs(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    persist(tmp_db, make_order(order_id="ord_a"))
    persist(tmp_db, make_order(order_id="ord_b", amount_in=2 * ONE_SOL, filled_out=1_000_000))
    report = fills.backfill(tmp_db, fetch_decimals=False)
    assert report.orders_seen == 2 and report.priced == 2
    rows = {r["order_id"]: r for r in fetch_all(tmp_db, "SELECT * FROM fill_prices", ())}
    assert rows["ord_a"]["price_usd"] == "200"
    assert rows["ord_b"]["price_usd"] == "400"


def test_backfill_marks_what_it_could_not_price(tmp_db):
    put_decimals(tmp_db)
    persist(tmp_db, make_order(order_id="ord_a"))  # no native sample at all
    report = fills.backfill(tmp_db, fetch_decimals=False)
    assert report.native_only == 1 and report.priced == 0
    row = fetch_one(tmp_db, "SELECT * FROM fill_prices", ())
    assert row["basis"] == fills.BASIS_FILL_RATIO_NATIVE_ONLY
    assert row["price_native_per_token"] == "1", "the native ratio survives"
    assert row["price_usd"] is None


def test_backfill_skips_orders_that_never_filled(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    persist(tmp_db, make_order(order_id="ord_x").model_copy(update={"state": OrderState.FAILED}))
    assert fills.backfill(tmp_db, fetch_decimals=False).orders_seen == 0


def test_backfill_records_only_unless_asked_to_apply(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = persist(tmp_db, make_order())
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, entry_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pos_1", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "1000000", "1000000", str(ONE_SOL), "999"),
    )
    tmp_db.execute(
        "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
        ("pos_1", order.order_id, "buy", NOW),
    )
    fills.backfill(tmp_db, fetch_decimals=False)
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions", ())["entry_price_usd"] == "999"
    report = fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    assert report.positions_updated == 1
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions", ())["entry_price_usd"] == "200"


def test_backfill_applies_a_quantity_weighted_entry_across_fills(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "1")
    a = persist(tmp_db, make_order(order_id="ord_a", amount_in=ONE_SOL, filled_out=1_000_000))
    b = persist(tmp_db, make_order(order_id="ord_b", amount_in=6 * ONE_SOL, filled_out=3_000_000))
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native) VALUES (?,?,?,?,?,?,?,?,?)",
        ("pos_1", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "4000000", "4000000", str(7 * ONE_SOL)),
    )
    for order in (a, b):
        tmp_db.execute(
            "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
            ("pos_1", order.order_id, "buy", NOW),
        )
    fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions", ())["entry_price_usd"] == "1.75"


def test_backfill_never_rewrites_a_shadow_position(tmp_db):
    """The paper broker converted with a placeholder native price on purpose."""
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = persist(tmp_db, make_order().model_copy(update={"mode": LaneMode.SHADOW}))
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, entry_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pos_s", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.SHADOW.value, NOW,
         "1000000", "1000000", str(ONE_SOL), "999"),
    )
    tmp_db.execute(
        "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
        ("pos_s", order.order_id, "buy", NOW),
    )
    report = fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    assert report.positions_updated == 0
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions", ())["entry_price_usd"] == "999"


def test_backfill_refuses_a_position_whose_fills_are_not_all_measured(tmp_db):
    """One unpriceable leg means the weighted average would be a partial truth."""
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    a = persist(tmp_db, make_order(order_id="ord_a"))
    b = persist(tmp_db, make_order(order_id="ord_b", updated_ms=NOW + 86_400_000))
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, entry_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pos_1", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "2000000", "2000000", str(2 * ONE_SOL), "999"),
    )
    for order in (a, b):
        tmp_db.execute(
            "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
            ("pos_1", order.order_id, "buy", NOW),
        )
    report = fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    assert report.positions_updated == 0
    assert fetch_one(tmp_db, "SELECT entry_price_usd FROM positions", ())["entry_price_usd"] == "999"


def test_backfill_emits_what_it_changed(tmp_db):
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = persist(tmp_db, make_order())
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native, entry_price_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pos_1", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "1000000", "1000000", str(ONE_SOL), "999"),
    )
    tmp_db.execute(
        "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
        ("pos_1", order.order_id, "buy", NOW),
    )
    fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    done = [e for e in ev.recent(conn=tmp_db) if e.payload.get("event") == "position_entry_backfilled"]
    assert done
    assert done[0].payload["old_entry_price_usd"] == "999"
    assert done[0].payload["new_entry_price_usd"] == "200"
    assert done[0].payload["entry_price_basis"] == fills.BASIS_FILL_RATIO


def test_a_backfilled_position_comes_out_armed(tmp_db):
    """A measured entry is only worth having if a stop follows from it."""
    put_decimals(tmp_db)
    put_native(tmp_db, NOW, "200")
    order = persist(tmp_db, make_order())
    tmp_db.execute(
        "INSERT INTO positions (position_id, chain, token, lane, mode, opened_ms, qty, qty_total, "
        "cost_native) VALUES (?,?,?,?,?,?,?,?,?)",
        ("pos_1", Chain.SOL.value, TOKEN, Lane.CONFLUENCE_5.value, LaneMode.LIVE.value, NOW,
         "1000000", "1000000", str(ONE_SOL)),
    )
    tmp_db.execute(
        "INSERT INTO position_orders (position_id, order_id, side, ts_ms) VALUES (?,?,?,?)",
        ("pos_1", order.order_id, "buy", NOW),
    )
    fills.backfill(tmp_db, apply=True, fetch_decimals=False)
    row = fetch_one(tmp_db, "SELECT stop_price_usd, protected FROM positions", ())
    from kaiba.execution.protection import protection_config as _pcfg
    _bps = Decimal(_pcfg().stop_loss_bps) / Decimal(10000)
    assert Decimal(row["stop_price_usd"]) == Decimal(200) * (1 - _bps)
    assert row["protected"] == 1
