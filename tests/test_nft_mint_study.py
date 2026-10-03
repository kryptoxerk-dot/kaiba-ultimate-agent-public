"""PAPER NFT mint study (Robinhood Chain): decoders on real logs, the tape, the rule, the
marks, the gate, the job and the daily-report line.

Fixtures are REAL chain reads from 2026-10-02 (tests/fixtures/nft_mint_study/*.json.gz):
171 SeaDropMint and 300 Seaport OrderFulfilled logs over the same ~20,000 blocks, plus the
single reads the study's checks make (getPublicDrop, getCode, the EIP-1967 slot,
getTransferValidator). Anything not read is marked synthetic where it is built.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kaiba.core.db import jdump
from kaiba.core.limiter import Priority
from kaiba.ingest import nft_tape
from kaiba.learning import mint_study as ms
from kaiba.providers import seaport_rh as sp

FIX = Path(__file__).parent / "fixtures" / "nft_mint_study"
H = 3_600_000
DAY = 86_400_000
ZERO_WORD = "0x" + "0" * 64


def _load(name: str) -> Any:
    with gzip.open(FIX / name, "rt", encoding="utf-8") as f:
        return json.load(f)


MINT_LOGS: list[dict[str, Any]] = _load("seadrop_mint_logs.json.gz")["logs"]
FILL_LOGS: list[dict[str, Any]] = _load("seaport_fulfilled_logs.json.gz")["logs"]
READS: dict[str, Any] = _load("rpc_reads.json.gz")
ANCHORS = sorted((int(b, 16), int(t, 16)) for b, t in READS["block_anchors"].items() if b.startswith("0x"))
FIRST_BLOCK = int(_load("seaport_fulfilled_logs.json.gz")["params"]["fromBlock"], 16)
LAST_LOG_BLOCK = max(int(entry["blockNumber"], 16) for entry in MINT_LOGS + FILL_LOGS)

STUDIO_CLONE = "0x1f8afda705ef2dfe88a4cc79197031a3b1ddc097"   # minted 8x in the window, 15e12 wei
TRADED_MINT = "0xf7f4652930166143944942880b4200047064ee5d"    # minted 97x and sold in the window
ART_BLOCKERS = "0x68be4d47cfd76f9d6af44fc06499fb53767dc17e"   # plain (non-clone) SeaDrop ERC-721


def block_ts(block: int) -> int:
    """Unix seconds at a block, piecewise-linear through the REAL anchors (extrapolated)."""
    pts = ANCHORS
    if block <= pts[0][0]:
        (b0, t0), (b1, t1) = pts[0], pts[1]
    elif block >= pts[-1][0]:
        (b0, t0), (b1, t1) = pts[-2], pts[-1]
    else:
        (b0, t0), (b1, t1) = next((pts[i], pts[i + 1]) for i in range(len(pts) - 1) if pts[i + 1][0] >= block)
    return t0 + (block - b0) * (t1 - t0) // (b1 - b0)


def word(value: int) -> str:
    return "0x" + hex(value)[2:].rjust(64, "0")


def addr_word(address: str) -> str:
    return "0x" + sp.pad_address(address)


def collections_from_reads() -> dict[str, dict[str, Any]]:
    """The study's seven reads per collection. Supply and ERC-721 answers are SYNTHETIC
    (not read on chain); everything else is the real response."""
    out: dict[str, dict[str, Any]] = {}
    for nft in set(READS["public_drop"]) | set(READS["code"]):
        out[nft] = {
            "public_drop": READS["public_drop"].get(nft),
            "code": READS["code"].get(nft, "0x"),
            "impl_slot": READS["impl_slot"].get(nft, ZERO_WORD),
            "validator": READS["transfer_validator"].get(nft, addr_word(sp.STUDIO_DEFAULT_VALIDATOR)),
            "erc721": True,          # synthetic
            "max_supply": 10_000,    # synthetic
            "total_supply": 1_234,   # synthetic
        }
    return out


class FakeChain:
    """A Robinhood RPC replaying the real logs. Every call is recorded."""

    def __init__(self, *, head: int | None = None, log_limit: int = 10_000,
                 collections: dict[str, dict[str, Any]] | None = None,
                 mint_logs: list[dict[str, Any]] | None = None, fill_logs: list[dict[str, Any]] | None = None,
                 fail_batches: set[int] | None = None) -> None:
        self.head = head if head is not None else LAST_LOG_BLOCK + 50
        self.log_limit = log_limit
        self.collections = collections if collections is not None else collections_from_reads()
        self.mint_logs = MINT_LOGS if mint_logs is None else mint_logs
        self.fill_logs = FILL_LOGS if fill_logs is None else fill_logs
        self.fail_batches = fail_batches or set()
        self.batches: list[tuple[str, list[str]]] = []

    def _logs(self, params: dict[str, Any]) -> list[dict[str, Any]] | None:
        lo, hi = int(params["fromBlock"], 16), int(params["toBlock"], 16)
        pool = self.mint_logs if params["address"].lower() == sp.SEADROP else self.fill_logs
        topic = params["topics"][0]
        got = [e for e in pool if lo <= int(e["blockNumber"], 16) <= hi and e["topics"][0] == topic]
        return None if len(got) > self.log_limit else got

    def _call(self, to: str, data: str) -> str | None:
        to = to.lower()
        if to == sp.SEADROP and data.startswith(sp.SEL_GET_PUBLIC_DROP):
            nft = "0x" + data[-40:]
            return self.collections.get(nft, {}).get("public_drop") or "0x" + "0" * 64 * 6
        c = self.collections.get(to)
        if c is None:
            return None  # revert
        if data == sp.SEL_GET_TRANSFER_VALIDATOR:
            return c.get("validator")
        if data.startswith(sp.SEL_SUPPORTS_INTERFACE):
            return None if c.get("erc721") is None else word(int(bool(c["erc721"])))
        if data.startswith(sp.SEL_GET_MINT_STATS):
            if c.get("max_supply") is None:
                return None
            return "0x" + "0" * 64 + word(c["total_supply"])[2:] + word(c["max_supply"])[2:]
        if data == sp.SEL_MAX_SUPPLY:
            return None if c.get("max_supply") is None else word(c["max_supply"])
        if data == sp.SEL_TOTAL_SUPPLY:
            return None if c.get("total_supply") is None else word(c["total_supply"])
        return None

    def __call__(self, calls: Any, endpoint: str) -> SimpleNamespace:
        self.batches.append((endpoint, [m for m, _ in calls]))
        if len(self.batches) in self.fail_batches:
            return SimpleNamespace(results=[], ok=False, note="RuntimeError: 429 rate limited")
        results: list[Any] = []
        errors: list[str] = []
        for method, params in calls:
            if method == "eth_blockNumber":
                results.append(hex(self.head))
            elif method == "eth_getBlockByNumber":
                b = int(params[0], 16)
                results.append({"number": params[0], "timestamp": hex(block_ts(b)),
                                "baseFeePerGas": READS["base_fee_wei"]})
            elif method == "eth_getLogs":
                got = self._logs(params[0])
                if got is None:
                    errors.append("eth_getLogs: {'code': -32000, 'message': 'logs matched by query exceeds "
                                  "limit of 10000'}")
                results.append(got)
            elif method == "eth_getCode":
                results.append(self.collections.get(params[0].lower(), {}).get("code", "0x"))
            elif method == "eth_getStorageAt":
                results.append(self.collections.get(params[0].lower(), {}).get("impl_slot", ZERO_WORD))
            elif method == "eth_call":
                r = self._call(params[0]["to"], params[0]["data"])
                if r is None:
                    errors.append("eth_call: {'code': 3, 'message': 'execution reverted'}")
                results.append(r)
            else:
                results.append(None)
                errors.append(f"{method}: unsupported")
        return SimpleNamespace(results=results, ok=not errors, note="; ".join(errors)[:200] or None)

    def endpoints(self) -> Counter:
        return Counter(e for e, _ in self.batches)


def seed_cursor(conn: Any, next_block: int = FIRST_BLOCK) -> None:
    nft_tape._save_cursor(conn, nft_tape.TapeCursor(next_block=next_block, first_block=next_block, updated_ms=1))


def tape_counts(conn: Any) -> tuple[int, dict[str, int]]:
    mints = conn.execute("SELECT COUNT(*) FROM nft_mints").fetchone()[0]
    fills = dict(conn.execute("SELECT kind, COUNT(*) FROM nft_fills GROUP BY kind").fetchall())
    return mints, fills


# ======================================================================================
# constants are the keccak of their signatures
# ======================================================================================


def test_topics_selectors_and_slot_are_the_keccak_of_their_signatures():
    from kaiba.execution.policy import keccak256, selector_of

    assert sp.TOPIC_SEADROP_MINT == "0x" + keccak256(
        b"SeaDropMint(address,address,address,address,uint256,uint256,uint256,uint256)").hex()
    assert sp.TOPIC_ORDER_FULFILLED == "0x" + keccak256(
        b"OrderFulfilled(bytes32,address,address,address,(uint8,address,uint256,uint256)[],"
        b"(uint8,address,uint256,uint256,address)[])").hex()
    for const, sig in [
        (sp.SEL_GET_PUBLIC_DROP, "getPublicDrop(address)"),
        (sp.SEL_MINT_PUBLIC, "mintPublic(address,address,address,uint256)"),
        (sp.SEL_MINT_SEADROP, "mintSeaDrop(address,uint256)"),
        (sp.SEL_GET_TRANSFER_VALIDATOR, "getTransferValidator()"),
        (sp.SEL_SET_TRANSFER_VALIDATOR, "setTransferValidator(address)"),
        (sp.SEL_MAX_SUPPLY, "maxSupply()"),
        (sp.SEL_TOTAL_SUPPLY, "totalSupply()"),
        (sp.SEL_GET_MINT_STATS, "getMintStats(address)"),
        (sp.SEL_SUPPORTS_INTERFACE, "supportsInterface(bytes4)"),
        (sp.SEL_LOCKED, "locked(uint256)"),
    ]:
        assert const == selector_of(sig), sig
    slot = int.from_bytes(keccak256(b"eip1967.proxy.implementation"), "big") - 1
    assert sp.EIP1967_IMPL_SLOT == "0x" + hex(slot)[2:].rjust(64, "0")
    # every topic on the real logs is ours
    assert {e["topics"][0] for e in MINT_LOGS} == {sp.TOPIC_SEADROP_MINT}
    assert {e["topics"][0] for e in FILL_LOGS} == {sp.TOPIC_ORDER_FULFILLED}


def test_reads_never_use_exit_or_position_priority(monkeypatch):
    """The robinhood-rpc bucket's EXIT/POSITION privileges belong to live stop-losses."""
    assert sp.READ_PRIORITY >= Priority.DISCOVERY
    assert sp.READ_PRIORITY not in (Priority.EXIT, Priority.POSITION, Priority.UNRESOLVED)
    seen: list[dict[str, Any]] = []

    def fake_rpc_batch(calls, **kw):
        seen.append(kw)
        return SimpleNamespace(results=["0x1"], ok=True, note=None)

    import kaiba.ingest.robinhood as rh

    monkeypatch.setattr(rh, "rpc_batch", fake_rpc_batch)
    sp.default_rpc()([("eth_blockNumber", [])], sp.ENDPOINT_TAPE)
    sp.read_collection(STUDIO_CLONE, sp.default_rpc())
    assert [kw["priority"] for kw in seen] == [Priority.RESEARCH, Priority.RESEARCH]
    # the chain.* family: refused during any chain cooldown another reader earned
    assert {kw["endpoint"].split(".")[0] for kw in seen} == {"chain"}


# ======================================================================================
# SeaDropMint
# ======================================================================================


def test_seadrop_mint_logs_decode_to_the_measured_window():
    mints = [sp.decode_seadrop_mint(e) for e in MINT_LOGS]
    assert all(m is not None for m in mints)
    assert len(mints) == 171
    assert len({m.collection for m in mints}) == 27
    assert sum(m.unit_price_wei == 0 for m in mints) == 126
    assert {m.fee_recipient for m in mints} == {sp.OPENSEA_FEE_RECIPIENT}
    assert {m.fee_bps for m in mints} == {1000}
    assert sum(m.quantity == 1 for m in mints) == 124
    first = mints[0]
    assert (first.collection, first.minter, first.payer, first.quantity, first.unit_price_wei, first.stage_index) == (
        STUDIO_CLONE, "0x3f7077965a650f0509c688883f269cf14a2e8f18", "0x3f7077965a650f0509c688883f269cf14a2e8f18",
        10, 15_000_000_000_000, 0)
    # VERIFIED on chain: that transaction's value was 0x886c98b76000 -- price x quantity, the
    # 10% fee is a split of the price, not an addition.
    assert first.tx == "0x47496b5b22ff2e6b451b70b939e268d7d4d83fee098552ceec2c8c89fcf72aa8"
    assert first.value_wei == 0x886C98B76000
    assert first.ts_ms is None  # 0x0 is absent, never the epoch


def test_a_seadrop_topic_from_another_contract_or_a_removed_log_is_not_a_mint():
    entry = dict(MINT_LOGS[0])
    assert sp.decode_seadrop_mint({**entry, "address": "0x" + "11" * 20}) is None
    assert sp.decode_seadrop_mint({**entry, "removed": True}) is None
    assert sp.decode_seadrop_mint({**entry, "data": entry["data"][:130]}) is None


# ======================================================================================
# OrderFulfilled: offer vs listing vs the seller's mirror order
# ======================================================================================


def _classified() -> list[tuple[str, sp.Sale | None, sp.OrderFulfilled]]:
    out = []
    for entry in FILL_LOGS:
        of = sp.decode_order_fulfilled(entry)
        assert of is not None
        kind, sale = sp.classify(of)
        out.append((kind, sale, of))
    return out


def test_seaport_logs_classify_into_bids_asks_and_mirror_orders():
    rows = _classified()
    assert Counter(k for k, _, _ in rows) == {"listing": 186, "offer": 98, "counter": 16}
    sales = [s for _, s, _ in rows if s]
    assert len({s.collection for s in sales}) == 66
    assert Counter((s.kind, s.payment_token) for s in sales) == {
        ("listing", sp.ZERO_ADDRESS): 155, ("listing", sp.USDG): 31,
        ("offer", sp.WETH): 74, ("offer", sp.USDG): 24,
    }
    # OpenSea's secondary fee on Robinhood is 1% on every sale in the window.
    assert {round(s.market_fee * 10_000 / s.gross) for s in sales} == {100}
    # Every mirror order sits beside a bid in the same tx and moves the token that bid buys.
    bids_by_tx: dict[str, set[tuple[str, int]]] = {}
    for kind, sale, _ in rows:
        if kind == "offer":
            bids_by_tx.setdefault(sale.tx, set()).add((sale.collection, sale.token_id))
    for kind, _, of in rows:
        if kind == "counter":
            moved = {(i.token, i.identifier) for i in of.offer if i.is_nft}
            assert of.offerer == of.recipient and moved <= bids_by_tx.get(of.tx, set())


def test_an_accepted_bid_pays_its_fees_out_of_the_bid():
    """tx 0x26307f62: the seller accepted a 0.0166 WETH bid (receipt read on chain)."""
    rows = [r for r in _classified() if r[2].tx.startswith("0x26307f62")]
    assert sorted(k for k, _, _ in rows) == ["counter", "offer"]
    sale = next(s for k, s, _ in rows if k == "offer")
    assert (sale.collection, sale.token_id, sale.payment_token) == (
        "0x6f2893a2bf65cc52a23fc5c1bb4626742965a84d", 344, sp.WETH)
    assert sale.gross == 16_600_000_000_000_000
    assert sale.market_fee == 166_000_000_000_000          # 1%
    assert sale.royalty == 1_079_000_000_000_000           # 6.5%
    assert sale.seller_net == 15_355_000_000_000_000
    assert sale.seller == "0x5896d4d9c12967c6a9c19d470b0e6c246892d732"   # the fulfiller
    assert sale.buyer == "0x234628c22f88567101051520789b390c26be72f2"    # the bidder


def test_a_bought_ask_pays_the_seller_what_is_addressed_to_them():
    rows = [r for r in _classified() if r[2].tx.startswith("0x5d26ffa2")]
    assert [k for k, _, _ in rows] == ["listing"]
    sale = rows[0][1]
    assert (sale.payment_token, sale.gross, sale.seller_net, sale.market_fee, sale.royalty) == (
        sp.ZERO_ADDRESS, 190_000_000_000_000, 169_100_000_000_000, 1_900_000_000_000, 19_000_000_000_000)
    assert sale.seller == "0x0e7befa88927f3270fba4db0d99a9d75dbfe7a58"
    assert sale.buyer == "0x1fa0c9cf10ae4ead90a2b053c9b06fd57af5ee61"


def test_nft_for_nft_and_mixed_payment_orders_are_not_priced():
    of = sp.decode_order_fulfilled(FILL_LOGS[0])
    swap = sp.OrderFulfilled(of.tx, of.log_index, of.block, None, of.order_hash, of.offerer, of.zone,
                             of.recipient, of.offer, (sp.Item(2, "0x" + "22" * 20, 1, 1, of.offerer),))
    assert sp.classify(swap) == ("other", None)
    mixed = sp.OrderFulfilled(of.tx, of.log_index, of.block, None, of.order_hash, of.offerer, of.zone,
                              of.recipient, of.offer,
                              (sp.Item(0, sp.ZERO_ADDRESS, 0, 5, of.offerer), sp.Item(1, sp.WETH, 0, 5, of.offerer)))
    assert sp.classify(mixed) == ("other", None)


# ======================================================================================
# contract reads
# ======================================================================================


def test_public_drop_decodes_and_its_window_is_start_inclusive_end_exclusive():
    art = sp.decode_public_drop(READS["public_drop"][ART_BLOCKERS])
    assert art == sp.PublicDrop(10**15, 1790272800, 1792864800, 50, 1000, True)  # 0.001 ETH, 50/wallet
    hood = sp.decode_public_drop(READS["public_drop"]["0x3750eda69bf4ce9c999ddf328be48c35be8a94ba"])
    assert (hood.mint_price_wei, hood.max_per_wallet) == (10**16, 30)
    assert art.open_at(art.start_s) and not art.open_at(art.start_s - 1) and not art.open_at(art.end_s)
    never = sp.decode_public_drop("0x" + "0" * 64 * 6)
    assert never is not None and not never.configured and not never.open_at(1790914000)
    assert sp.decode_public_drop("0x1234") is None


def test_contract_facts_name_clones_proxies_and_plain_code():
    clone = sp.contract_facts(READS["code"][STUDIO_CLONE], ZERO_WORD)
    assert (clone.kind, clone.code_size, clone.implementation) == ("eip1167", 45, sp.STUDIO_CLONE_IMPLEMENTATION)
    assert clone.recognised and clone.has_transfer_validator and not clone.soulbound
    # every one of the four sampled minting collections is that same clone
    assert {sp.contract_facts(READS["code"][a], ZERO_WORD).implementation for a in READS["code"]
            if a != ART_BLOCKERS} == {sp.STUDIO_CLONE_IMPLEMENTATION}
    # the implementation carries what the docstring says (selectors read off its 21,257 bytes)
    assert READS["implementation_code_bytes"] == 21_257
    assert {"64869dad", "098144d4", "a9fc664e", "d5abeb01", "840e15d4"} <= set(READS["implementation_selectors_sample"])
    assert "b45a3c0e" not in READS["implementation_selectors_sample"]

    stranger = READS["code"][STUDIO_CLONE].replace(sp.STUDIO_CLONE_IMPLEMENTATION[2:], "ab" * 20)
    assert not sp.contract_facts(stranger, ZERO_WORD).recognised

    plain = sp.contract_facts(READS["code"][ART_BLOCKERS], READS["impl_slot"][ART_BLOCKERS])
    assert (plain.kind, plain.code_size) == ("plain", 19_658) and plain.recognised

    proxy = sp.contract_facts(READS["code"][ART_BLOCKERS], addr_word("0x" + "cd" * 20))
    assert proxy.kind == "eip1967" and not proxy.recognised
    assert sp.contract_facts("0x", ZERO_WORD).kind == "empty"
    # a plain contract carrying locked(uint256) is soulbound; a tiny one is not recognised
    sb = "0x" + "63" + sp.SEL_MINT_SEADROP[2:] + "63" + sp.SEL_LOCKED[2:] + "00" * 3000
    assert sp.contract_facts(sb, ZERO_WORD).soulbound
    assert not sp.contract_facts("0x63" + sp.SEL_MINT_SEADROP[2:] + "00" * 100, ZERO_WORD).recognised


def test_a_collection_check_is_two_small_batches_and_only_an_open_drop_earns_the_second():
    """MEASURED 2026-10-02: a 7-read batch drew an HTTP 429 from the public RPC where 1- and
    4-read batches did not, so no batch here carries more than four reads."""
    open_s = 1_790_914_000  # inside the clone's real public window
    chain = FakeChain()
    check = sp.read_collection(STUDIO_CLONE, chain, at_s=open_s)
    assert chain.batches == [(sp.ENDPOINT_CHECK, ["eth_call", "eth_getCode"]),
                             (sp.ENDPOINT_CHECK, ["eth_call", "eth_call"])]
    assert check.transport_ok and check.facts.recognised and check.reads == 4 and check.is_erc721 is True
    assert check.transfer_validator == "0xa000027a9b2802e1ddf7000061001e5c005a0000"  # the real read
    assert (check.total_supply, check.max_supply, check.headroom) == (1_234, 10_000, 10_000 - 1_234)

    chain = FakeChain()
    closed = sp.read_collection(STUDIO_CLONE, chain, at_s=1_700_000_000)  # before its stage opened
    assert len(chain.batches) == 1 and closed.reads == 2 and closed.max_supply is None
    unknown = sp.read_collection("0x" + "99" * 20, chain, at_s=open_s)  # no drop, no code: stage 1 only
    assert len(chain.batches) == 2 and unknown.reads == 2 and not unknown.public_drop.configured

    chain = FakeChain()
    plain = sp.read_collection(ART_BLOCKERS, chain, at_s=1_790_914_000)
    assert chain.batches[1] == (sp.ENDPOINT_CHECK, ["eth_getStorageAt", "eth_call", "eth_call", "eth_call"])
    assert plain.reads == 6 and plain.facts.kind == "plain" and plain.is_erc721 is True
    assert max(len(methods) for _, methods in chain.batches) <= 4

    down = sp.read_collection(STUDIO_CLONE, FakeChain(fail_batches={1}), at_s=open_s)
    assert not down.transport_ok and "429" in down.note
    late = sp.read_collection(STUDIO_CLONE, FakeChain(fail_batches={2}), at_s=open_s)
    assert not late.transport_ok and late.public_drop is not None and late.reads == 2


# ======================================================================================
# the tape
# ======================================================================================


def test_the_tape_reads_the_window_into_rows_and_a_gap_free_cursor(tmp_db):
    seed_cursor(tmp_db)
    chain = FakeChain()
    res = nft_tape.run_tape(tmp_db, rpc=chain, config=nft_tape.TapeConfig(chunk_blocks=5_000, max_chunks_per_run=10,
                                                                          retention_days=0))
    assert res.ok and res.note is None
    assert tape_counts(tmp_db) == (171, {"listing": 186, "offer": 98})
    assert res.skipped == {"counter": 16}
    cur = nft_tape.load_cursor(tmp_db)
    safe_head = chain.head - 50
    assert (cur.next_block, cur.through_block, cur.first_block) == (safe_head + 1, safe_head, FIRST_BLOCK)
    assert cur.first_ts_ms == block_ts(FIRST_BLOCK) * 1000 and cur.through_ts_ms == block_ts(safe_head) * 1000
    assert cur.base_fee_wei == int(READS["base_fee_wei"], 16)
    assert res.rpc_calls == 1 + res.chunks and res.behind_blocks == 0
    # head + ceil(20,812 / 5,000) chunks, all in the chain.* family
    assert chain.endpoints() == {sp.ENDPOINT_TAPE: 1 + res.chunks}
    # the seeded cursor has no anchor yet: 4 reads in the first chunk, 3 in every later one
    assert [len(m) for _, m in chain.batches] == [1, 4] + [3] * (res.chunks - 1)
    # interpolated times stay within seconds of the real anchors' curve, never cross a
    # minute, and are monotonic in block order
    rows = tmp_db.execute("SELECT block, ts_ms, ts_exact FROM nft_fills ORDER BY block, log_index").fetchall()
    assert all(abs(ts - block_ts(block) * 1000) <= 10_000 for block, ts, _ in rows)
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)
    assert {exact for _, _, exact in rows} <= {0, 1}


def test_the_tape_resumes_where_it_stopped_without_gaps_or_duplicates(tmp_db):
    seed_cursor(tmp_db)
    cfg = nft_tape.TapeConfig(chunk_blocks=4_000, max_chunks_per_run=2, retention_days=0)
    first = nft_tape.run_tape(tmp_db, rpc=FakeChain(), config=cfg)
    assert first.ok and first.chunks == 2 and first.behind_blocks > 0
    partial = tape_counts(tmp_db)
    assert partial[0] < 171
    while nft_tape.run_tape(tmp_db, rpc=FakeChain(), config=cfg).behind_blocks:
        pass
    assert tape_counts(tmp_db) == (171, {"listing": 186, "offer": 98})
    # a re-read of the same range (cursor wound back) inserts nothing twice
    seed_cursor(tmp_db)
    nft_tape.run_tape(tmp_db, rpc=FakeChain(), config=nft_tape.TapeConfig(chunk_blocks=30_000, retention_days=0))
    assert tape_counts(tmp_db) == (171, {"listing": 186, "offer": 98})


def test_a_failed_batch_leaves_the_cursor_and_the_next_run_fills_the_gap(tmp_db):
    seed_cursor(tmp_db)
    cfg = nft_tape.TapeConfig(chunk_blocks=5_000, max_chunks_per_run=10, retention_days=0)
    res = nft_tape.run_tape(tmp_db, rpc=FakeChain(fail_batches={3}), config=cfg)  # head, chunk 1, FAIL
    assert not res.ok and "429" in res.note and res.chunks == 1
    assert nft_tape.load_cursor(tmp_db).next_block == FIRST_BLOCK + 5_000
    nft_tape.run_tape(tmp_db, rpc=FakeChain(), config=cfg)
    assert tape_counts(tmp_db) == (171, {"listing": 186, "offer": 98})


def test_an_unreadable_head_or_anchor_never_advances_the_cursor(tmp_db):
    seed_cursor(tmp_db)
    res = nft_tape.run_tape(tmp_db, rpc=FakeChain(fail_batches={1}))
    assert not res.ok and res.note.startswith("head unreadable")
    chain = FakeChain()
    original = chain.__call__

    def drop_result(index):
        def call(calls, endpoint):
            got = original(calls, endpoint)
            if len(calls) > 1:
                got.results[index] = None
            return got
        return call

    for index in (0, 3):  # the chunk's end block; a cold cursor's start block
        res = nft_tape.run_tape(tmp_db, rpc=drop_result(index))
        assert not res.ok and res.chunks == 0 and "incomplete" in res.note
        assert nft_tape.load_cursor(tmp_db).next_block == FIRST_BLOCK and tape_counts(tmp_db) == (0, {})


def test_a_query_over_the_log_limit_halves_the_chunk_instead_of_failing(tmp_db):
    seed_cursor(tmp_db)
    res = nft_tape.run_tape(tmp_db, rpc=FakeChain(log_limit=120),
                            config=nft_tape.TapeConfig(chunk_blocks=20_000, max_chunks_per_run=40, retention_days=0))
    assert res.ok and res.halvings >= 2
    assert tape_counts(tmp_db) == (171, {"listing": 186, "offer": 98})


def test_a_cold_start_backfills_and_old_rows_are_pruned(tmp_db):
    chain = FakeChain()
    res = nft_tape.run_tape(tmp_db, rpc=chain, config=nft_tape.TapeConfig(backfill_hours=1.0, retention_days=0,
                                                                          max_chunks_per_run=10))
    cur = nft_tape.load_cursor(tmp_db)
    assert cur.first_block == chain.head - 50 - nft_tape.blocks_for_hours(1.0)
    assert res.ok and tape_counts(tmp_db)[0] > 0
    newest = tmp_db.execute("SELECT MAX(ts_ms) FROM nft_fills").fetchone()[0]
    pruned = nft_tape.prune(tmp_db, before_ms=newest - 10 * 60_000, chunk=7, max_chunks=1000)
    assert pruned > 0
    assert tmp_db.execute("SELECT MIN(ts_ms) FROM nft_fills").fetchone()[0] >= newest - 10 * 60_000


def test_a_log_carrying_its_own_timestamp_is_stamped_exactly(tmp_db):
    entry = dict(FILL_LOGS[5])
    entry["blockTimestamp"] = hex(1_790_912_345)
    blk = int(entry["blockNumber"], 16)
    nft_tape.persist_chunk(tmp_db, [], [entry], from_block=blk - 10, from_ms=1, to_block=blk + 10, to_ms=21)
    assert tuple(tmp_db.execute("SELECT ts_ms, ts_exact FROM nft_fills").fetchone()) == (1_790_912_345_000, 1)
    assert nft_tape.interpolate_ms(15, 10, 1000, 20, 2000) == 1500


# ======================================================================================
# the rule (pure)
# ======================================================================================


AT_S = 1_790_915_000
GAS = 35_276_000  # wei per gas, MEASURED effective gas price


def good_check(**over: Any) -> sp.CollectionCheck:
    base = dict(
        collection=STUDIO_CLONE,
        public_drop=sp.PublicDrop(10**15, AT_S - 3600, AT_S + 86_400, 10, 1000, True),
        facts=sp.ContractFacts(45, "eip1167", sp.STUDIO_CLONE_IMPLEMENTATION),
        transfer_validator=sp.STUDIO_DEFAULT_VALIDATOR, is_erc721=True, max_supply=10_000,
        total_supply=100, transport_ok=True)
    base.update(over)
    return sp.CollectionCheck(**base)


def fills(n_offer: int, n_listing: int, price_wei: int, *, at_ms: int, spread_h: float = 20.0,
          buyers: int = 5) -> list[ms.FillPoint]:
    out = []
    total = n_offer + n_listing
    for i in range(total):
        ts = at_ms - int((i + 0.5) * spread_h * H / max(total, 1))
        out.append(ms.FillPoint(ts, "offer" if i < n_offer else "listing", price_wei,
                                f"0xb{i % buyers}", f"0xs{i}"))
    return out


def good_evidence(price_wei: int = 5 * 10**15) -> ms.Evidence:
    return ms.evidence_from(fills(3, 5, price_wei, at_ms=AT_S * 1000, spread_h=5.5), at_ms=AT_S * 1000)


def test_a_drop_with_real_bids_above_cost_is_a_paper_mint():
    d = ms.decide(good_check(), good_evidence(), at_s=AT_S, gas_price_wei=GAS)
    assert (d.verdict, d.failed) == ("mint", ())
    assert d.mint_price_wei == 10**15 and d.mint_fee_wei == 10**14  # 10% is inside the price
    assert d.mint_gas_wei == 200_000 * GAS and d.sell_gas_wei == 300_000 * GAS
    assert d.cost_wei == 10**15 + 200_000 * GAS
    assert d.features["offers_24h"] == 3 and d.features["buyers_24h"] == 5


@pytest.mark.parametrize("clause, check_over, evidence", [
    ("mint_price_above_cap", {"public_drop": sp.PublicDrop(6 * 10**15, AT_S - 1, AT_S + 9, 1, 1000, True)},
     ms.evidence_from(fills(3, 5, 2 * 10**16, at_ms=AT_S * 1000, spread_h=5), at_ms=AT_S * 1000)),
    ("upgradeable_proxy", {"facts": sp.ContractFacts(9000, "eip1967", "0x" + "cd" * 20)}, None),
    ("contract_unrecognised", {"facts": sp.ContractFacts(45, "eip1167", "0x" + "ab" * 20)}, None),
    ("no_code", {"facts": sp.ContractFacts(0, "empty")}, None),
    ("not_erc721", {"is_erc721": None}, None),
    ("soulbound", {"facts": sp.ContractFacts(9000, "plain", None, frozenset({"64869dad", "b45a3c0e"}))}, None),
    ("transfer_validator_unvetted", {"transfer_validator": "0x" + "ee" * 20}, None),
    ("transfer_validator_unreadable", {"transfer_validator": None}, None),
    ("sold_out", {"total_supply": 10_000}, None),
    ("supply_unreadable", {"max_supply": None}, None),
    ("resale_fills_24h", {}, ms.evidence_from(fills(1, 3, 5 * 10**15, at_ms=AT_S * 1000, spread_h=5), at_ms=AT_S * 1000)),
    ("resale_buyers_24h", {}, ms.evidence_from(fills(3, 5, 5 * 10**15, at_ms=AT_S * 1000, spread_h=5, buyers=2),
                                               at_ms=AT_S * 1000)),
    ("resale_no_accepted_bid_24h", {}, ms.evidence_from(fills(0, 8, 5 * 10**15, at_ms=AT_S * 1000, spread_h=5),
                                                        at_ms=AT_S * 1000)),
    ("resale_below_cost_multiple", {}, good_evidence(price_wei=15 * 10**14)),  # 1.5x the 0.001 mint
])
def test_each_clause_alone_turns_a_mint_into_a_shadow(clause, check_over, evidence):
    d = ms.decide(good_check(**check_over), evidence or good_evidence(), at_s=AT_S, gas_price_wei=GAS)
    assert d.verdict == "shadow" and d.failed == (clause,), d.failed


def test_a_collapsing_or_silent_market_is_a_shadow():
    at = AT_S * 1000
    old = [ms.FillPoint(at - 20 * H + i, "offer", 10**16, f"0xb{i}", "0xs") for i in range(6)]
    cheap_now = [ms.FillPoint(at - H, "offer", 5 * 10**15, "0xbx", "0xs")]
    d = ms.decide(good_check(), ms.evidence_from(old + cheap_now, at_ms=at), at_s=AT_S, gas_price_wei=GAS)
    assert d.failed == ("resale_collapsing_6h",)
    d = ms.decide(good_check(), ms.evidence_from(old, at_ms=at), at_s=AT_S, gas_price_wei=GAS)
    assert d.failed == ("resale_collapsing_6h",)  # no fill at all in 6 h


def test_an_ask_only_drop_with_no_sales_is_a_shadow_and_a_closed_stage_is_not_a_row():
    empty = ms.evidence_from([], at_ms=AT_S * 1000)
    d = ms.decide(good_check(), empty, at_s=AT_S, gas_price_wei=GAS)
    assert d.verdict == "shadow"
    assert {"resale_fills_24h", "resale_buyers_24h", "resale_no_accepted_bid_24h",
            "resale_below_cost_multiple", "resale_collapsing_6h"} <= set(d.failed)
    closed = good_check(public_drop=sp.PublicDrop(10**15, AT_S + 60, AT_S + 999, 5, 1000, True))
    assert ms.decide(closed, empty, at_s=AT_S, gas_price_wei=GAS).skip_reason == "no_open_public_stage"
    never = good_check(public_drop=sp.PublicDrop(0, 0, 0, 0, 0, False))
    assert ms.decide(never, empty, at_s=AT_S, gas_price_wei=GAS).skip_reason == "no_public_drop"
    unread = good_check(transport_ok=False, note="429")
    assert ms.decide(unread, empty, at_s=AT_S, gas_price_wei=GAS).skip_reason == "check_unread"


def test_usdg_fills_are_converted_at_the_eth_price_of_their_time_or_left_unpriced():
    class Fx:
        def at(self, ts_ms):
            return Decimal("2500") if ts_ms < 10 else None

    assert ms.to_eth_wei(sp.USDG, 25_000_000, 1, Fx()) == 10**16       # 25 USDG = 0.01 ETH at $2,500
    assert ms.to_eth_wei(sp.USDG, 25_000_000, 99, Fx()) is None         # no price -> unpriced
    assert ms.to_eth_wei(sp.WETH, 7, 1, None) == 7 and ms.to_eth_wei(sp.ZERO_ADDRESS, 7, 1, None) == 7
    assert ms.to_eth_wei("0x" + "12" * 20, 7, 1, Fx()) is None


# ======================================================================================
# marks (pure)
# ======================================================================================


DECIDED = 1_790_000_000_000
COST = 10**15
SELL_GAS = 10**13


def at_mark(hours: float, kind: str, price: int, *, h: int = 24) -> ms.FillPoint:
    return ms.FillPoint(DECIDED + h * H + int(hours * H), kind, price, "0xb", "0xs")


def mark(points: list[ms.FillPoint], h: int = 24) -> ms.Mark:
    return ms.mark_from(points, decided_ms=DECIDED, horizon_h=h, half_window_h=6.0, cost_wei=COST,
                        sell_gas_wei=SELL_GAS)


def test_no_sale_in_the_window_is_an_exit_of_zero_and_loses_the_whole_cost():
    m = mark([at_mark(-7, "offer", 10**17), at_mark(6.5, "offer", 10**17)])  # both just outside
    assert (m.exit_basis, m.exit_wei, m.net_exit_wei, m.pnl_wei) == ("none", 0, 0, -COST)
    assert (m.best_basis, m.pnl_best_wei) == ("none", -COST)


def test_accepted_bids_set_the_exit_and_asks_only_count_for_half():
    m = mark([at_mark(-6, "offer", 3 * 10**15), at_mark(0, "offer", 2 * 10**15), at_mark(6, "listing", 10**17)])
    assert (m.exit_basis, m.exit_wei, m.n_offer, m.n_listing) == ("offer_median", 25 * 10**14, 2, 1)
    assert m.pnl_wei == 25 * 10**14 - SELL_GAS - COST
    assert (m.best_basis, m.best_exit_wei) == ("best_sale", 10**17)  # optimistic, reported, never gated
    m = mark([at_mark(1, "listing", 4 * 10**15), at_mark(2, "listing", 2 * 10**15)])
    assert (m.exit_basis, m.exit_wei) == ("listing_half", 15 * 10**14)
    assert m.window_from_ms == DECIDED + 18 * H and m.window_to_ms == DECIDED + 30 * H


def test_an_exit_below_the_sell_gas_is_held_not_sold():
    m = mark([at_mark(0, "offer", SELL_GAS // 2)])
    assert (m.exit_wei, m.net_exit_wei, m.pnl_wei) == (SELL_GAS // 2, 0, -COST)


def test_unpriced_fills_are_counted_and_never_used_as_a_price():
    m = mark([ms.FillPoint(DECIDED + 24 * H, "offer", None, "0xb", "0xs")])
    assert (m.n_unpriced, m.n_offer, m.exit_basis) == (1, 1, "none")


# ======================================================================================
# statistics and the gate (pure)
# ======================================================================================


def scored(n: int, *, collections: int, pnl: int, days: float = 8.0, n_offer: int = 3,
           basis: str = "offer_median", verdict: str = "mint", start: int = 0) -> list[ms.ScoredRow]:
    return [ms.ScoredRow(start + i, f"0xc{i % collections}", verdict, int(i * days * DAY / max(n - 1, 1)),
                         pnl + (i % 3) * 10**12, pnl, basis, n_offer) for i in range(n)]


def passing() -> dict[str, Any]:
    return {"mint": scored(40, collections=12, pnl=10**15), "shadow": scored(40, collections=15, pnl=-10**15,
                                                                               verdict="shadow", start=100),
            "mint_robust": scored(35, collections=12, pnl=5 * 10**14, start=200)}


def gate(data: dict[str, Any], *, age_days: float = 10.0) -> ms.GateVerdict:
    now = 1_800_000_000_000
    return ms.evaluate_gate(data["mint"], data["shadow"], data["mint_robust"],
                            started_ms=now - int(age_days * DAY), now=now)


def test_the_gate_passes_only_when_every_clause_holds():
    v = gate(passing())
    assert v.status == "PASS" and v.failing == [] and len(v.checks) == 10


@pytest.mark.parametrize("clause, mutate", [
    ("scored_mints", lambda d: d.update(mint=d["mint"][:29])),
    ("collections", lambda d: d.update(mint=scored(40, collections=9, pnl=10**15))),
    ("span_days", lambda d: d.update(mint=scored(40, collections=12, pnl=10**15, days=6.5))),
    ("mean_pnl", lambda d: d.update(mint=scored(40, collections=12, pnl=-10**13))),
    ("ci_lower", lambda d: d.update(mint=[*scored(39, collections=12, pnl=-10**14),
                                         ms.ScoredRow(999, "0xbig", "mint", DAY, 10**17, 0, "offer_median", 3)])),
    ("realizable_share", lambda d: d.update(mint=scored(40, collections=12, pnl=10**15, basis="none"))),
    ("fillable_share", lambda d: d.update(mint=scored(40, collections=12, pnl=10**15, n_offer=1))),
    ("shadow_scored", lambda d: d.update(shadow=d["shadow"][:29])),
    ("beats_shadow", lambda d: d.update(shadow=scored(40, collections=15, pnl=2 * 10**15, verdict="shadow"))),
    ("robust_72h", lambda d: d.update(mint_robust=scored(35, collections=12, pnl=-10**14))),
])
def test_breaking_any_one_clause_stops_a_pass(clause, mutate):
    data = passing()
    mutate(data)
    v = gate(data)
    assert v.status == "PENDING" and clause in v.failing, (clause, v.failing)


def test_the_gate_is_final_fail_after_thirty_days_and_pending_before():
    data = passing()
    data["mint"] = scored(40, collections=12, pnl=-10**15)
    assert gate(data, age_days=29.9).status == "PENDING"
    assert gate(data, age_days=30.0).status == "FAIL"
    assert gate({"mint": [], "shadow": [], "mint_robust": []}, age_days=1).status == "PENDING"
    assert ms.evaluate_gate([], [], [], started_ms=None, now=1).status == "PENDING"


def test_the_bootstrap_resamples_collections_and_is_deterministic():
    rows = [(f"c{i % 4}", v) for i, v in enumerate([5, -3, 2, 8, 1, -1, 4, 0])]
    ci = ms.cluster_bootstrap_ci(rows, draws=500, seed=1)
    assert ci == ms.cluster_bootstrap_ci(rows, draws=500, seed=1)
    assert ci[0] <= sum(v for _, v in rows) / len(rows) <= ci[1]
    assert ms.cluster_bootstrap_ci([("a", 1), ("b", 2)]) is None  # two clusters is not an interval
    # 30 rows in ONE collection + 2 others: three clusters, so the interval stays wide
    lumpy = [("big", 10)] * 30 + [("x", -10), ("y", -10)]
    lo, hi = ms.cluster_bootstrap_ci(lumpy, draws=500)
    assert lo < 0 < hi
    diff = ms.cluster_bootstrap_diff_ci([(f"a{i}", 10) for i in range(5)], [(f"b{i}", -10) for i in range(5)])
    assert diff == (20.0, 20.0)


# ======================================================================================
# the study end to end on the real window, the job, the report
# ======================================================================================


def study_params(**over: Any) -> ms.StudyParams:
    """The shipped rule, mark and gate; only the evidence window shrinks to the 34-minute
    fixture, because a real 24 h window cannot be replayed from one window of reads."""
    base = dict(evidence_hours=0.5, candidate_lookback_h=1.0, max_checks_per_run=40, pace_s=0.0,
                tape=nft_tape.TapeConfig(chunk_blocks=10_000, max_chunks_per_run=10, retention_days=0))
    base.update(over)
    return ms.StudyParams(**base)


def frontier_ms(chain: FakeChain) -> int:
    return block_ts(chain.head - 50) * 1000


def test_the_study_holds_decisions_until_the_tape_covers_the_evidence_window(tmp_db):
    seed_cursor(tmp_db)
    chain = FakeChain()
    out = ms.run(tmp_db, rpc=chain, now=frontier_ms(chain) + 60_000, params=study_params(evidence_hours=24.0))
    assert out["decisions"]["held"].startswith("tape_history_short:")
    assert tmp_db.execute("SELECT COUNT(*) FROM nft_paper_mints").fetchone()[0] == 0
    out = ms.run(tmp_db, rpc=FakeChain(), now=frontier_ms(chain) + 60 * 60_000, params=study_params())
    assert out["decisions"]["held"].startswith("tape_behind:")


def test_the_real_window_produces_shadow_rows_priced_on_chain_and_nothing_mints(tmp_db):
    seed_cursor(tmp_db)
    chain = FakeChain()
    now = frontier_ms(chain) + 60_000
    out = ms.run(tmp_db, rpc=chain, now=now, params=study_params())
    dec = out["decisions"]
    assert out["tape"]["ok"] and out["tape"]["mints"] == 171
    assert dec["candidates"] == 27 and dec["checks"] == 27
    # The fixture holds real getPublicDrop reads for three of the 27 minting collections; the
    # fake chain answers "never configured" for the other 24 (SYNTHETIC -- not a measurement
    # of how many RH drops lack a public stage).
    rows = {r[0]: r for r in tmp_db.execute(
        "SELECT collection, verdict, failed_json, mint_price_wei, mint_fee_wei, cost_wei, rule_version "
        "FROM nft_paper_mints")}
    assert set(rows) == {STUDIO_CLONE, TRADED_MINT, "0x5fccc158af2a52618b47ff07088a67c0a27ac8f6"}
    assert dec["mint"] == 0 and dec["shadow"] == 3 and dec["skip"] == {"no_public_drop": 24}
    clone = rows[STUDIO_CLONE]
    assert clone[1] == "shadow" and clone[3] == "15000000000000" and clone[4] == "1500000000000"
    assert int(clone[5]) == 15_000_000_000_000 + 200_000 * int(READS["base_fee_wei"], 16)
    assert clone[6] == ms.RULE_VERSION
    # the one minted collection that sold in the window sold ONE ask, in USDG, and with no
    # ETH/USD sample in this database it stays unpriced: no median, so no price evidence
    assert json.loads(rows[TRADED_MINT][2]) == ["resale_fills_24h", "resale_buyers_24h",
                                                "resale_no_accepted_bid_24h", "resale_below_cost_multiple",
                                                "resale_collapsing_6h"]
    feats = json.loads(tmp_db.execute("SELECT features_json FROM nft_paper_mints WHERE collection=?",
                                      (TRADED_MINT,)).fetchone()[0])
    assert (feats["fills_24h"], feats["listings_24h"], feats["unpriced_24h"], feats["median_net_24h_wei"],
            feats["mints_1h"], feats["code_kind"]) == (1, 1, 1, None, 97, "eip1167")
    # 27 stage-1 batches (2 reads) + 3 stage-2 batches (2 reads) for the three open drops
    assert out["http_calls"] == out["tape"]["rpc_calls"] + 27 + 3 and dec["reads"] == 27 * 2 + 3 * 2
    assert tmp_db.execute("SELECT value FROM kv WHERE key=?", (ms.STARTED_KEY,)).fetchone()[0] == str(
        frontier_ms(chain))
    gate_kv = json.loads(tmp_db.execute("SELECT value FROM kv WHERE key=?", (ms.GATE_KEY,)).fetchone()[0])
    assert gate_kv["status"] == "PENDING" and gate_kv["version"] == ms.GATE_VERSION
    # once per collection per UTC day: a second run makes no new rows and no new checks
    before = len(chain.batches)
    again = ms.run(tmp_db, rpc=chain, now=now + 1000, params=study_params())
    assert again["decisions"]["checks"] == 0 and len(chain.batches) == before + 1  # the head read only
    assert tmp_db.execute("SELECT COUNT(*) FROM nft_paper_mints").fetchone()[0] == 3
    assert out["gate"]["status"] == "PENDING"
    assert len(jdump(out)) < 2000  # fits the scheduler's result cell untruncated


def test_marks_are_written_only_once_the_tape_covers_the_window(tmp_db):
    tmp_db.execute(
        "INSERT INTO nft_paper_mints (collection, day_utc, decided_ms, verdict, rule_version, mint_price_wei, "
        "mint_fee_wei, mint_gas_wei, sell_gas_wei, cost_wei) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (TRADED_MINT, "2026-09-01", DECIDED, "mint", ms.RULE_VERSION, str(COST), "0", "0", str(SELL_GAS), str(COST)))
    for i, (hours, kind, price) in enumerate([(-1, "offer", 4 * 10**15), (2, "offer", 2 * 10**15),
                                              (3, "listing", 9 * 10**15)]):
        tmp_db.execute(
            "INSERT INTO nft_fills (tx, log_index, block, ts_ms, kind, collection, units, payment_token, gross, "
            "seller_net, market_fee, royalty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"0x{i}", 0, i, DECIDED + 24 * H + hours * H, kind, TRADED_MINT, 1, sp.WETH, str(price), str(price),
             "0", "0"))
    fx = ms.EthUsd(tmp_db)
    p = ms.StudyParams()
    early = nft_tape.TapeCursor(1, 0, DECIDED - DAY, 9, DECIDED + 30 * H - 1)
    assert ms.score_due(tmp_db, cursor=early, now=1, params=p, fx=fx)["scored"] == 0
    gap = nft_tape.TapeCursor(1, 0, DECIDED + 19 * H, 9, DECIDED + 31 * H)  # tape started inside the window
    assert ms.score_due(tmp_db, cursor=gap, now=1, params=p, fx=fx) == {"scored": 0, "unscorable_tape_gap": 1}
    ok = nft_tape.TapeCursor(1, 0, DECIDED - DAY, 9, DECIDED + 30 * H)
    assert ms.score_due(tmp_db, cursor=ok, now=1, params=p, fx=fx)["scored"] == 1  # 24 h only; 72 h not yet
    row = tmp_db.execute("SELECT horizon_h, exit_basis, exit_wei, pnl_wei, best_exit_wei FROM nft_paper_marks"
                         ).fetchone()
    assert tuple(row) == (24, "offer_median", str(3 * 10**15), str(3 * 10**15 - SELL_GAS - COST), str(9 * 10**15))
    assert ms.score_due(tmp_db, cursor=ok, now=2, params=p, fx=fx)["scored"] == 0  # never twice
    rows = ms.load_scored(tmp_db, 24, "mint")
    assert [(r.collection, r.pnl_wei, r.n_offer) for r in rows] == [(TRADED_MINT, 3 * 10**15 - SELL_GAS - COST, 2)]


def test_the_migration_enforces_one_decision_per_collection_per_day(tmp_db):
    ins = ("INSERT INTO nft_paper_mints (collection, day_utc, decided_ms, verdict, rule_version, mint_price_wei, "
           "mint_fee_wei, mint_gas_wei, sell_gas_wei, cost_wei) VALUES (?,?,?,?,?,?,?,?,?,?)")
    tmp_db.execute(ins, ("0xa", "2026-10-02", 1, "shadow", "v", "0", "0", "0", "0", "0"))
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        tmp_db.execute(ins, ("0xa", "2026-10-02", 2, "mint", "v", "0", "0", "0", "0", "0"))
    tmp_db.execute(ins, ("0xa", "2026-10-03", 3, "mint", "v", "0", "0", "0", "0", "0"))


def test_the_scheduler_job_is_registered_disabled_and_runs_on_the_fake_chain(tmp_db, monkeypatch):
    import time as _time

    from kaiba.ops import scheduler as S

    config = S.load_config(Path(__file__).resolve().parents[1] / "config" / "schedule.yaml")
    job = config.jobs["nft_mint_study"]
    assert job.enabled is False and job.timeout_s < config.lock_stale_s
    assert S.JOBS["nft_mint_study"].run is S.job_nft_mint_study
    assert S.JOBS["nft_mint_study"].spends_helius is False

    seed_cursor(tmp_db)
    chain = FakeChain()
    monkeypatch.setattr(sp, "default_rpc", lambda conn=None, **kw: chain)
    ctx = S.JobContext(name="nft_mint_study", conn=tmp_db, params={**job.params, "pace_s": 0}, config=config,
                       started_ms=0, deadline_ms=10**15, clock=lambda: frontier_ms(chain) / 1000 + 60)
    t0 = _time.monotonic()
    result = S.job_nft_mint_study(ctx)
    assert _time.monotonic() - t0 < 30
    assert result["tape"]["ok"] and result["tape"]["mints"] == 171
    # the shipped 24 h evidence window: a 34-minute tape holds every decision
    assert result["decisions"]["held"].startswith("tape_history_short")
    assert "truncated" not in S._small(result)
    assert {e.split(".")[0] for e in chain.endpoints()} == {"chain"}


def test_the_daily_report_prints_paper_mints_read_only(tmp_db, tmp_path, monkeypatch, capsys):
    from kaiba.core.config import RiskConfig, save_risk
    from kaiba.ops import daily_report

    save_risk(RiskConfig(), tmp_path / "risk.yaml")
    monkeypatch.setenv("KAIBA_RISK_PATH", str(tmp_path / "risk.yaml"))
    (tmp_path / "schedule.yaml").write_text("jobs: {}\n", encoding="utf-8")
    monkeypatch.setenv("KAIBA_SCHEDULE_CONFIG", str(tmp_path / "schedule.yaml"))
    seed_cursor(tmp_db)
    chain = FakeChain()
    ms.run(tmp_db, rpc=chain, now=frontier_ms(chain) + 60_000, params=study_params())
    tmp_db.commit()
    assert daily_report.main(["--db", str(tmp_path / "kaiba.db")]) == 0
    text = capsys.readouterr().out
    lines = [line for line in text.splitlines() if "PAPER MINTS" in line or line.startswith("  @24h")]
    assert lines[0].startswith(f"PAPER MINTS (robinhood, keyless, spends nothing) {ms.RULE_VERSION} | decided 24h "
                               "mint 0 shadow 3 | all mint 0 shadow 3 | tape ")
    assert lines[1].startswith("  @24h mint n 0 | shadow n 0 | gate PENDING: scored_mints 0/>=30")
    assert "cannot measure" not in "\n".join(lines)

    tmp_db.execute("DROP TABLE nft_paper_mints")
    tmp_db.commit()
    assert daily_report.main(["--db", str(tmp_path / "kaiba.db")]) == 0
    assert "PAPER MINTS not started (migration 034 not applied)" in capsys.readouterr().out


def test_an_open_opensea_drop_is_a_candidate_first_and_carries_its_label(tmp_db):
    """The hunter's OpenSea rows are a LABEL (their floor is an ask); on-chain reads decide."""
    tmp_db.execute(
        "INSERT INTO opportunities (opportunity_id, kind, name, chain, evidence_json, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?)",
        ("op1", "nft_mint", "Art Blockers", "robinhood",
         json.dumps({"meta": {"contract_address": ART_BLOCKERS.upper().replace("0X", "0x"), "stage_state": "open",
                              "public_phase": True, "floor_price": "0.0062", "floor_unit": "ETH", "sales_1d": 0}}),
         1, 1))
    labels = ms.opensea_labels(tmp_db)
    assert labels[ART_BLOCKERS]["floor_ask"] == "0.0062"
    seed_cursor(tmp_db)
    chain = FakeChain()
    ms.run(tmp_db, rpc=chain, now=frontier_ms(chain) + 60_000, params=study_params(max_checks_per_run=1))
    row = tmp_db.execute("SELECT collection, verdict, label, mint_price_wei, features_json FROM nft_paper_mints"
                         ).fetchone()
    assert (row[0], row[1], row[2], row[3]) == (ART_BLOCKERS, "shadow", "Art Blockers", str(10**15))
    feats = json.loads(row[4])
    assert feats["code_kind"] == "plain" and feats["opensea"]["sales_1d"] == 0


def test_schedule_knobs_reach_the_study_but_the_rule_does_not():
    p = ms.StudyParams.from_params({"max_checks_per_run": 3, "chunk_blocks": 777, "backfill_hours": 2,
                                    "retention_days": 5, "min_fills_24h": 0, "max_mint_price_wei": 1})
    assert (p.max_checks_per_run, p.tape.chunk_blocks, p.tape.backfill_hours, p.tape.retention_days) == (
        3, 777, 2.0, 5.0)
    assert p.rule == ms.RuleParams() and p.gate == ms.GateParams()  # pre-registered: code only


def test_an_upcoming_public_stage_is_read_again_once_it_opens_and_nothing_else_is(tmp_db):
    seed_cursor(tmp_db)
    chain = FakeChain()
    f = frontier_ms(chain)
    upcoming = "0xb002bd63507b2f4db50e7219ffb5c77d52a09d10"  # minted 13x in the window (allowlist)
    opens_s = f // 1000 + 300
    chain.collections[upcoming] = {  # SYNTHETIC drop: opens 5 minutes after the frontier
        "public_drop": "0x" + "".join(w[2:] for w in (word(10**14), word(opens_s), word(opens_s + 86_400),
                                                        word(5), word(1000), word(1))),
        "code": READS["code"][STUDIO_CLONE], "impl_slot": ZERO_WORD,
        "validator": addr_word(sp.STUDIO_DEFAULT_VALIDATOR), "erc721": True, "max_supply": 100, "total_supply": 1}
    first = ms.run(tmp_db, rpc=chain, now=f + 60_000, params=study_params())
    assert first["decisions"]["skip"]["no_open_public_stage"] == 1
    early = ms.run(tmp_db, rpc=chain, now=f + 120_000, params=study_params())
    assert early["decisions"]["checks"] == 0
    opened = ms.run(tmp_db, rpc=chain, now=f + 400_000, params=study_params())
    assert opened["decisions"]["checks"] == 1 and opened["decisions"]["shadow"] == 1
    assert tmp_db.execute("SELECT verdict FROM nft_paper_mints WHERE collection=?", (upcoming,)).fetchone()[0] == "shadow"


def _fill(conn: Any, i: int, ts: int, kind: str, net: int, buyer: str, collection: str = STUDIO_CLONE) -> None:
    conn.execute(
        "INSERT INTO nft_fills (tx, log_index, block, ts_ms, kind, collection, units, payment_token, gross, "
        "seller_net, market_fee, royalty, buyer, seller) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"0xsyn{i}", 0, i, ts, kind, collection, 1, sp.WETH, str(net), str(net), "0", "0", buyer, f"0xs{i}"))


def test_a_synthetic_market_mints_on_paper_is_scored_and_reaches_the_summary(tmp_db):
    """SYNTHETIC fills (not chain data) through the real decision, scoring and summary path."""
    f = 1_791_000_000_000
    cursor = nft_tape.TapeCursor(next_block=2, first_block=0, first_ts_ms=f - 2 * DAY, through_block=1,
                                 through_ts_ms=f, base_fee_wei=GAS)
    tmp_db.execute("INSERT INTO nft_mints (tx, log_index, block, ts_ms, collection, minter, fee_recipient, quantity, "
                   "unit_price_wei, fee_bps, stage_index) VALUES ('0xm', 0, 1, ?, ?, '0xa', ?, 1, '0', 1000, 0)",
                   (f - 600_000, STUDIO_CLONE, sp.OPENSEA_FEE_RECIPIENT))
    for i in range(8):  # 3 accepted bids + 5 bought asks in the last 5 h, 5 buyers, 0.0005 ETH net each
        _fill(tmp_db, i, f - (i + 1) * 30 * 60_000, "offer" if i < 3 else "listing", 5 * 10**14, f"0xb{i % 5}")
    chain = FakeChain()
    clone = chain.collections[STUDIO_CLONE]
    drop_words = sp.words(clone["public_drop"])
    clone["public_drop"] = "0x" + "".join([drop_words[0], word(f // 1000 - 60)[2:], word(f // 1000 + DAY // 1000)[2:],
                                            *drop_words[3:]])  # SYNTHETIC window around f; real price 15e12
    p = ms.StudyParams(pace_s=0.0)
    out = ms.make_decisions(tmp_db, rpc=chain, cursor=cursor, now=f + 60_000, params=p, fx=ms.EthUsd(tmp_db))
    assert out["mint"] == 1 and out["checks"] == 1
    row = tmp_db.execute("SELECT verdict, failed_json, cost_wei, sell_gas_wei FROM nft_paper_mints").fetchone()
    assert tuple(row) == ("mint", "[]", str(15 * 10**12 + 200_000 * GAS), str(300_000 * GAS))

    _fill(tmp_db, 100, f + 23 * H, "offer", 4 * 10**14, "0xbx")
    _fill(tmp_db, 101, f + 25 * H, "offer", 2 * 10**14, "0xby")
    later = nft_tape.TapeCursor(next_block=9, first_block=0, first_ts_ms=f - 2 * DAY, through_block=8,
                                through_ts_ms=f + 31 * H)
    assert ms.score_due(tmp_db, cursor=later, now=f + 31 * H, params=p, fx=ms.EthUsd(tmp_db))["scored"] == 1
    expected = 3 * 10**14 - 300_000 * GAS - (15 * 10**12 + 200_000 * GAS)
    s = ms.summary(tmp_db, now=f + 31 * H)
    assert s["ev_mint"]["n"] == 1 and s["ev_mint"]["mean_eth"] == Decimal(expected) / Decimal(10**18)
    assert s["ev_mint"]["realizable"] == 1 and s["ev_shadow"]["n"] == 0
    lines = ms.render_lines(s)
    assert lines[1].startswith(f"  @24h mint n 1 mean {Decimal(expected) / Decimal(10**18):+.6f} ETH | shadow n 0")


def test_the_resale_bar_is_exactly_1_6x_price_plus_both_gas_legs_with_the_fee_inside_the_price():
    """Boundary: the fee is a split of the mint price (verified on chain), so it must not
    be added on top; the bar is 1.6 x (price + mint gas + sell gas), inclusive."""
    all_in = 10**15 + 200_000 * GAS + 300_000 * GAS
    bar = (8 * all_in + 4) // 5  # ceil(1.6 x all_in), integer
    at = good_evidence(price_wei=bar)
    assert at.median_net_24h_wei == bar
    assert ms.decide(good_check(), at, at_s=AT_S, gas_price_wei=GAS).verdict == "mint"
    below = good_evidence(price_wei=bar - 1)
    assert ms.decide(good_check(), below, at_s=AT_S, gas_price_wei=GAS).failed == ("resale_below_cost_multiple",)


def test_the_study_paces_its_own_requests():
    t = {"now": 0.0}
    slept: list[float] = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        t["now"] += seconds

    def rpc(calls, endpoint):
        t["now"] += 0.5  # each request takes half a second
        return SimpleNamespace(results=[], ok=True, note=None)

    call = ms.paced(rpc, 3.0, sleep=sleep, clock=lambda: t["now"])
    for _ in range(3):
        call([("eth_blockNumber", [])], sp.ENDPOINT_TAPE)
    assert slept == [3.0, 3.0] and call.calls == 3
    assert ms.StudyParams().pace_s >= 3.0
    assert ms.StudyParams.from_params({"pace_s": 0}).pace_s == 0.0
