"""Keyless Robinhood Chain readers for OpenSea's primary (SeaDrop) and secondary (Seaport).

READ-ONLY. Nothing here builds, signs or sends a transaction. It decodes two event logs and
answers a handful of ``eth_call`` questions about an NFT contract, for the paper mint study
(:mod:`kaiba.learning.mint_study`). Selling an NFT needs an OpenSea API key and signer
capabilities we do not have; see ``scratchpad/mint-farm-design.md`` §2.3.

What was verified on chain, 2026-10-02 (raw responses kept beside the design doc):

* SeaDrop 1.x at its canonical address (21,081 bytes; ``getPublicDrop`` in its dispatch).
  A public mint is ``mintPublic(nft, feeRecipient, minterIfNotPayer, quantity)`` with
  ``msg.value == mintPrice * quantity`` EXACTLY. The marketplace fee is a SPLIT of that
  price, not an addition: tx ``0x47496b5b…72aa8`` minted 10 at a unit price of
  15,000,000,000,000 wei with ``feeBps`` 1000 and carried ``value`` 150,000,000,000,000 --
  ten times the price, nothing on top.
* ``SeaDropMint.dropStageIndex == 0`` does NOT mean a public mint: tx ``0x93525727…6d7d``
  is a ``mintSigned`` (selector ``0x4b61cd6f``, a 65-byte server signature in its calldata)
  whose event says stage 0. Whether a public stage is open is read from ``getPublicDrop``.
* Every observed mint (171/171) paid OpenSea's fee recipient :data:`OPENSEA_FEE_RECIPIENT`
  at ``feeBps`` 1000.
* Seaport 1.6 at its canonical address. Of 300 ``OrderFulfilled`` in one 34-minute window,
  98 were ACCEPTED BIDS, 186 were asks being bought, and 16 were the seller's own mirror
  order in an accept-offer match: a listing-shaped order whose ``offerer == recipient``,
  emitted beside the bidder's order in the same transaction, moving the same token. Those
  16 are not sales -- counting them would double-count 16 bids as asks (see
  :func:`classify`).
* Bid fees are paid by the SELLER out of the bid: an accepted 0.0166 WETH bid carried a
  1% OpenSea fee and a 6.5% royalty as consideration items, so the seller kept 0.015355.
  Listing fees are consideration items too; the seller keeps what is addressed to them.
* All four sampled minting collections are EIP-1167 clones of one implementation,
  :data:`STUDIO_CLONE_IMPLEMENTATION` (21,257 bytes, carries ``mintSeaDrop``,
  ``getTransferValidator``, ``setTransferValidator``, ``maxSupply``, ``getMintStats``). A
  minimal clone cannot be upgraded; its owner CAN set a transfer validator later.
* A sampled clone returned transfer validator :data:`STUDIO_DEFAULT_VALIDATOR` (15,463
  bytes of code on chain). Identity NOT verified against a published source -- recorded as
  "observed default", and the decisive sellability evidence is that the collection's tokens
  have actually moved through Seaport (the study's resale rule), not this list.
* Gas, MEASURED from receipts: a signed qty-1 mint used 137,585 gas; an accept-offer match
  284,614; a plain bid fill 232,932; effective gas price ~35.3 Mwei (0.035 gwei, no tip).

Transport: :func:`default_rpc` sends every batch through ``kaiba.ingest.robinhood.rpc_batch``
-- the existing RH JSON-RPC client, so the shared ``robinhood-rpc`` limiter bucket (refill
0.6/s, ``max_inflight`` 1, shared with LIVE stop-losses) accounts for every call -- at
``Priority.RESEARCH``. Never EXIT or POSITION: those are the priorities a live stop needs,
and this is research. The endpoints are in the ``chain.`` family on purpose, as
``onchain_pool`` does: a 429 we earn cools every non-EXIT chain reader, and a cooldown
someone else earned refuses us too, so we never hammer a throttled endpoint.

Money is integer base units end to end. No value here passes through ``float``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from kaiba.core.limiter import Priority

# --------------------------------------------------------------------------------------
# addresses (lowercase), each with the read that established it
# --------------------------------------------------------------------------------------

CHAIN_ID = 4663
ZERO_ADDRESS = "0x" + "0" * 40

#: SeaDrop 1.x, canonical. VERIFIED: eth_getCode 21,081 bytes on RH.
SEADROP = "0x00005ea00ac477b1030ce78506496e8c2de24bf5"
#: Seaport 1.6, canonical. VERIFIED: eth_getCode 23,981 bytes on RH.
SEAPORT = "0x0000000000000068f116a894984e2db1123eb395"
#: OpenSea's fee recipient: topic 3 of 171/171 SeaDropMint logs, and the fee item on
#: 186 of 186 OpenSea-zone sales in the Seaport sample.
OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1f0df003000390027140000faa719"
#: OpenSea's signed zone on RH: the zone of 235 of 300 OrderFulfilled in the sample (the
#: other 65 are zone 0x0: the 16 mirror orders plus asks posted without a zone).
OPENSEA_ZONE = "0x000056f7000000ece9003ca63978907a00ffd100"
#: Payment tokens seen in the sample, identified by ``symbol()`` on chain. WETH is 18
#: decimals and par with ETH; USDG is 6 decimals (MEASURED, see onchain_pool).
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
USDG_DECIMALS = 6

#: The implementation every sampled Studio drop clones (4/4 minting collections sampled).
STUDIO_CLONE_IMPLEMENTATION = "0x09a26fc8fcef18192e267d7a6da9dfb4be81dd6a"
#: Implementations whose clones the study treats as recognised SeaDrop ERC-721s. Add one
#: only after reading its code and checking the selectors listed in the module docstring.
KNOWN_SEADROP_IMPLEMENTATIONS: Mapping[str, str] = {
    STUDIO_CLONE_IMPLEMENTATION: "OpenSea Studio ERC721SeaDropCloneable (observed 2026-10-02; "
                                 "selectors checked, source not verified)",
}
#: The validator a sampled Studio clone returned from ``getTransferValidator()``.
STUDIO_DEFAULT_VALIDATOR = "0xa000027a9b2802e1ddf7000061001e5c005a0000"
KNOWN_TRANSFER_VALIDATORS: Mapping[str, str] = {
    STUDIO_DEFAULT_VALIDATOR: "observed default on Studio clones (15,463 bytes of code on RH); "
                              "identity UNVERIFIED",
}

# --------------------------------------------------------------------------------------
# topics and selectors. Constants, pinned against kaiba.execution.policy.keccak256 in
# tests/test_nft_mint_study.py, and confirmed against real logs.
# --------------------------------------------------------------------------------------

#: ``SeaDropMint(address indexed nftContract, address indexed minter, address indexed
#: feeRecipient, address payer, uint256 quantityMinted, uint256 unitMintPrice,
#: uint256 feeBps, uint256 dropStageIndex)``
TOPIC_SEADROP_MINT = "0xe90cf9cc0a552cf52ea6ff74ece0f1c8ae8cc9ad630d3181f55ac43ca076b7d6"
#: ``OrderFulfilled(bytes32 orderHash, address indexed offerer, address indexed zone,
#: address recipient, SpentItem[] offer, ReceivedItem[] consideration)``
TOPIC_ORDER_FULFILLED = "0x9d9af8e38d66c62e2c12f0225249fd9d721c54b83f48d9352c97c6cacdcb6f31"

SEL_GET_PUBLIC_DROP = "0xbc6a629c"       # getPublicDrop(address) on SeaDrop
SEL_MINT_PUBLIC = "0x161ac21f"           # mintPublic(address,address,address,uint256) -- documented, never sent
SEL_MINT_SEADROP = "0x64869dad"          # mintSeaDrop(address,uint256) -- what makes an NFT SeaDrop-mintable
SEL_GET_TRANSFER_VALIDATOR = "0x098144d4"  # getTransferValidator()
SEL_SET_TRANSFER_VALIDATOR = "0xa9fc664e"  # setTransferValidator(address)
SEL_MAX_SUPPLY = "0xd5abeb01"            # maxSupply()
SEL_TOTAL_SUPPLY = "0x18160ddd"          # totalSupply()
SEL_GET_MINT_STATS = "0x840e15d4"        # getMintStats(address) -> (minted, totalSupply, maxSupply)
SEL_SUPPORTS_INTERFACE = "0x01ffc9a7"    # supportsInterface(bytes4)
SEL_LOCKED = "0xb45a3c0e"                # locked(uint256) -- ERC-5192 soulbound
IFACE_ERC721 = "0x80ac58cd"

#: EIP-1967 implementation slot: keccak("eip1967.proxy.implementation") - 1.
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
#: EIP-1167 minimal proxy runtime: prefix, 20-byte implementation, suffix (45 bytes).
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"
#: Below this a "contract" is a stub; real ERC-721 code is tens of kB (clones excepted).
MIN_PLAIN_CODE_BYTES = 2048

# --------------------------------------------------------------------------------------
# Seaport item types
# --------------------------------------------------------------------------------------

ITEM_NATIVE = 0
ITEM_ERC20 = 1
ITEM_ERC721 = 2
ITEM_ERC1155 = 3
ITEM_ERC721_CRITERIA = 4
ITEM_ERC1155_CRITERIA = 5
_NFT_TYPES = frozenset({ITEM_ERC721, ITEM_ERC1155, ITEM_ERC721_CRITERIA, ITEM_ERC1155_CRITERIA})
_FUNGIBLE_TYPES = frozenset({ITEM_NATIVE, ITEM_ERC20})
#: A decoder bound: an order with more items than this is not a retail NFT sale and is
#: refused rather than allocated. The sample's largest consideration had 6 items.
MAX_ITEMS = 64

#: Limiter priority for every read here. A test pins that it is not EXIT or POSITION.
READ_PRIORITY = Priority.RESEARCH
ENDPOINT_TAPE = "chain.nft_tape"
ENDPOINT_CHECK = "chain.nft_check"

# --------------------------------------------------------------------------------------
# ABI helpers
# --------------------------------------------------------------------------------------


def hex_int(value: Any) -> int | None:
    """A hex quantity -> int, or ``None``. Never 0 as a fallback."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16) if len(value) > 2 else None
    except ValueError:
        return None


def words(data: Any) -> list[str]:
    """ABI payload -> 32-byte hex words, or ``[]``."""
    if not isinstance(data, str) or not data.startswith("0x"):
        return []
    body = data[2:]
    return [body[i:i + 64] for i in range(0, len(body) - len(body) % 64, 64)]


def _int(ws: Sequence[str], i: int) -> int | None:
    if i < 0 or i >= len(ws):
        return None
    try:
        return int(ws[i], 16)
    except ValueError:
        return None


def _addr(word: Any) -> str | None:
    if not isinstance(word, str) or len(word) < 40:
        return None
    tail = word[-40:].lower()
    try:
        int(tail, 16)
    except ValueError:
        return None
    return "0x" + tail


def _topic_addr(topics: Sequence[Any], i: int) -> str | None:
    return _addr(topics[i]) if i < len(topics) and isinstance(topics[i], str) else None


def pad_address(address: str) -> str:
    """An address as one ABI word (no 0x)."""
    return address.lower().removeprefix("0x").rjust(64, "0")


def log_ts_ms(entry: Mapping[str, Any]) -> int | None:
    """``blockTimestamp`` off a log, or ``None``. ``0x0`` is ABSENT, not the epoch.

    Nitro fills it only within a few blocks of the head (kaiba.ingest.robinhood measured
    it; both fixtures in this study carry ``0x0`` on every entry).
    """
    ts = hex_int(entry.get("blockTimestamp"))
    return ts * 1000 if ts and ts > 0 else None


# --------------------------------------------------------------------------------------
# SeaDropMint
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeaDropMint:
    tx: str
    log_index: int
    block: int
    ts_ms: int | None
    collection: str
    minter: str
    fee_recipient: str
    payer: str | None
    quantity: int
    unit_price_wei: int
    fee_bps: int
    stage_index: int

    @property
    def value_wei(self) -> int:
        """What the payer sent: price x quantity. The fee is inside it."""
        return self.unit_price_wei * self.quantity


def decode_seadrop_mint(entry: Any) -> SeaDropMint | None:
    """One ``SeaDropMint`` log -> :class:`SeaDropMint`, or ``None`` (never a partial row)."""
    if not isinstance(entry, Mapping) or entry.get("removed") is True:
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 4:
        return None
    if str(topics[0]).lower() != TOPIC_SEADROP_MINT:
        return None
    if str(entry.get("address") or "").lower() != SEADROP:
        return None  # same topic from any other contract is not OpenSea's primary
    collection, minter, fee_recipient = (_topic_addr(topics, i) for i in (1, 2, 3))
    ws = words(entry.get("data"))
    qty, price, bps, stage = (_int(ws, i) for i in (1, 2, 3, 4))
    block, log_index = hex_int(entry.get("blockNumber")), hex_int(entry.get("logIndex"))
    tx = entry.get("transactionHash")
    if None in (collection, minter, fee_recipient, qty, price, bps, stage, block, log_index):
        return None
    if not isinstance(tx, str) or qty <= 0:  # type: ignore[operator]
        return None
    return SeaDropMint(
        tx=tx.lower(), log_index=log_index, block=block, ts_ms=log_ts_ms(entry),  # type: ignore[arg-type]
        collection=collection, minter=minter, fee_recipient=fee_recipient,  # type: ignore[arg-type]
        payer=_addr(ws[0]) if ws else None, quantity=qty, unit_price_wei=price,  # type: ignore[arg-type]
        fee_bps=bps, stage_index=stage,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# OrderFulfilled
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    item_type: int
    token: str
    identifier: int
    amount: int
    recipient: str | None = None  # consideration items only

    @property
    def is_nft(self) -> bool:
        return self.item_type in _NFT_TYPES

    @property
    def is_fungible(self) -> bool:
        return self.item_type in _FUNGIBLE_TYPES

    @property
    def payment_token(self) -> str:
        """Native ETH as the zero address, an ERC-20 as its address."""
        return ZERO_ADDRESS if self.item_type == ITEM_NATIVE else self.token


@dataclass(frozen=True)
class OrderFulfilled:
    tx: str
    log_index: int
    block: int
    ts_ms: int | None
    order_hash: str
    offerer: str
    zone: str
    recipient: str
    offer: tuple[Item, ...]
    consideration: tuple[Item, ...]


def _items(ws: Sequence[str], offset_word: int | None, width: int) -> tuple[Item, ...] | None:
    """Decode a dynamic array of ``width``-word structs at a byte offset, or ``None``."""
    if offset_word is None or offset_word % 32:
        return None
    at = offset_word // 32
    n = _int(ws, at)
    if n is None or n > MAX_ITEMS or at + 1 + n * width > len(ws):
        return None
    out: list[Item] = []
    for k in range(n):
        base = at + 1 + k * width
        item_type, token, ident, amount = _int(ws, base), _addr(ws[base + 1]), _int(ws, base + 2), _int(ws, base + 3)
        if item_type is None or token is None or ident is None or amount is None or item_type > 5:
            return None
        recipient = _addr(ws[base + 4]) if width == 5 else None
        if width == 5 and recipient is None:
            return None
        out.append(Item(item_type, token, ident, amount, recipient))
    return tuple(out)


def decode_order_fulfilled(entry: Any) -> OrderFulfilled | None:
    """One Seaport ``OrderFulfilled`` log -> :class:`OrderFulfilled`, or ``None``."""
    if not isinstance(entry, Mapping) or entry.get("removed") is True:
        return None
    topics = entry.get("topics")
    if not isinstance(topics, Sequence) or len(topics) < 3:
        return None
    if str(topics[0]).lower() != TOPIC_ORDER_FULFILLED:
        return None
    if str(entry.get("address") or "").lower() != SEAPORT:
        return None
    ws = words(entry.get("data"))
    if len(ws) < 6:
        return None
    offer = _items(ws, _int(ws, 2), 4)
    consideration = _items(ws, _int(ws, 3), 5)
    offerer, zone, recipient = _topic_addr(topics, 1), _topic_addr(topics, 2), _addr(ws[1])
    block, log_index = hex_int(entry.get("blockNumber")), hex_int(entry.get("logIndex"))
    tx = entry.get("transactionHash")
    if offer is None or consideration is None or None in (offerer, zone, recipient, block, log_index):
        return None
    if not isinstance(tx, str):
        return None
    return OrderFulfilled(
        tx=tx.lower(), log_index=log_index, block=block, ts_ms=log_ts_ms(entry),  # type: ignore[arg-type]
        order_hash="0x" + ws[0], offerer=offerer, zone=zone, recipient=recipient,  # type: ignore[arg-type]
        offer=offer, consideration=consideration,
    )


@dataclass(frozen=True)
class Sale:
    """One NFT sale. ``kind`` is ``listing`` (an ask was bought) or ``offer`` (a bid was
    accepted). Amounts are in ``payment_token`` base units for the WHOLE order."""

    tx: str
    log_index: int
    block: int
    ts_ms: int | None
    kind: str
    collection: str
    token_id: int
    units: int
    payment_token: str
    gross: int
    seller_net: int
    market_fee: int
    royalty: int
    seller: str | None
    buyer: str | None
    zone: str


#: Classification labels that are not a sale. ``counter`` is the seller's own mirror order.
NOT_A_SALE = ("counter", "other")


def classify(of: OrderFulfilled) -> tuple[str, Sale | None]:
    """``(kind, sale)``: ``listing`` / ``offer`` carry a :class:`Sale`; the rest ``None``.

    * listing -- offer is NFTs only, consideration is one fungible token: the offerer is
      the seller, ``recipient`` the buyer. Gross is every fungible consideration item; the
      seller keeps the items addressed to them.
    * offer -- offer is one fungible ERC-20 (a bid), consideration is the NFTs (to the
      bidder) plus fee items in the same token: ``recipient`` is the seller, who keeps the
      bid minus those fee items.
    * counter -- listing-shaped with ``offerer == recipient``: the seller's mirror order in
      an accept-offer match. MEASURED: all 16 in the sample sit in a transaction beside the
      bidder's order and move the same token. Not a sale.
    * other -- anything else (NFT-for-NFT, mixed collections or payment tokens, a self-
      filled bid). Counted by the tape, never priced.
    """
    nft_offer = [i for i in of.offer if i.is_nft]
    fung_offer = [i for i in of.offer if i.is_fungible]
    nft_cons = [i for i in of.consideration if i.is_nft]
    fung_cons = [i for i in of.consideration if i.is_fungible]
    zero = ZERO_ADDRESS

    if nft_offer and not fung_offer and fung_cons and not nft_cons:
        if of.offerer == of.recipient:
            return "counter", None
        collections = {i.token for i in nft_offer}
        tokens = {i.payment_token for i in fung_cons}
        if len(collections) != 1 or len(tokens) != 1:
            return "other", None
        gross = sum(i.amount for i in fung_cons)
        seller_net = sum(i.amount for i in fung_cons if i.recipient == of.offerer)
        fee = sum(i.amount for i in fung_cons if i.recipient == OPENSEA_FEE_RECIPIENT)
        if seller_net <= 0 or gross <= 0:
            return "other", None
        return "listing", Sale(
            tx=of.tx, log_index=of.log_index, block=of.block, ts_ms=of.ts_ms, kind="listing",
            collection=nft_offer[0].token, token_id=nft_offer[0].identifier,
            units=sum(max(1, i.amount) for i in nft_offer), payment_token=tokens.pop(),
            gross=gross, seller_net=seller_net, market_fee=fee, royalty=gross - seller_net - fee,
            seller=of.offerer, buyer=None if of.recipient == zero else of.recipient, zone=of.zone,
        )

    if fung_offer and not nft_offer and nft_cons:
        if of.offerer == of.recipient:
            return "other", None  # a bidder filling their own bid moves nothing
        collections = {i.token for i in nft_cons}
        tokens = {i.payment_token for i in fung_offer} | {i.payment_token for i in fung_cons}
        if len(collections) != 1 or len(tokens) != 1:
            return "other", None
        if any(i.recipient != of.offerer for i in nft_cons):
            return "other", None  # the NFTs do not go to the bidder: not a plain bid
        if any(i.recipient == of.offerer for i in fung_cons):
            return "other", None
        gross = sum(i.amount for i in fung_offer)
        fees = sum(i.amount for i in fung_cons)
        fee = sum(i.amount for i in fung_cons if i.recipient == OPENSEA_FEE_RECIPIENT)
        if gross <= 0 or fees >= gross:
            return "other", None
        return "offer", Sale(
            tx=of.tx, log_index=of.log_index, block=of.block, ts_ms=of.ts_ms, kind="offer",
            collection=nft_cons[0].token, token_id=nft_cons[0].identifier,
            units=sum(max(1, i.amount) for i in nft_cons), payment_token=tokens.pop(),
            gross=gross, seller_net=gross - fees, market_fee=fee, royalty=fees - fee,
            seller=None if of.recipient == zero else of.recipient, buyer=of.offerer, zone=of.zone,
        )
    return "other", None


# --------------------------------------------------------------------------------------
# contract facts (eth_call / eth_getCode answers, parsed purely)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicDrop:
    """``SeaDrop.getPublicDrop(nft)``. All zero means no public drop was ever configured."""

    mint_price_wei: int
    start_s: int
    end_s: int
    max_per_wallet: int
    fee_bps: int
    restrict_fee_recipients: bool

    @property
    def configured(self) -> bool:
        return bool(self.start_s or self.end_s or self.mint_price_wei or self.max_per_wallet)

    def open_at(self, at_s: int) -> bool:
        return self.configured and self.max_per_wallet > 0 and self.start_s <= at_s < self.end_s


def decode_public_drop(result: Any) -> PublicDrop | None:
    ws = words(result)
    if len(ws) < 6:
        return None
    vals = [_int(ws, i) for i in range(6)]
    if any(v is None for v in vals):
        return None
    price, start, end, per_wallet, bps, restricted = vals  # type: ignore[misc]
    return PublicDrop(price, start, end, per_wallet, bps, bool(restricted))  # type: ignore[arg-type]


def push4_selectors(code: bytes) -> frozenset[str]:
    """Every PUSH4 operand in runtime code: a superset of the dispatch table, no 0x."""
    out: set[str] = set()
    i, n = 0, len(code)
    while i < n:
        op = code[i]
        if op == 0x63 and i + 4 < n:
            out.add(code[i + 1:i + 5].hex())
        if 0x60 <= op <= 0x7F:
            i += op - 0x5F  # skip PUSH data so it is never read as opcodes
        i += 1
    return frozenset(out)


@dataclass(frozen=True)
class ContractFacts:
    """What the code and the EIP-1967 slot say. ``kind``: empty | eip1167 | eip1967 | plain."""

    code_size: int
    kind: str
    implementation: str | None = None
    selectors: frozenset[str] = field(default_factory=frozenset)

    @property
    def implementation_known(self) -> bool:
        return self.kind == "eip1167" and self.implementation in KNOWN_SEADROP_IMPLEMENTATIONS

    @property
    def recognised(self) -> bool:
        """A SeaDrop ERC-721 we can name: a clone of a known implementation, or plain
        (non-proxy) code of real size that carries ``mintSeaDrop``."""
        if self.kind == "eip1167":
            return self.implementation_known
        return (self.kind == "plain" and self.code_size >= MIN_PLAIN_CODE_BYTES
                and SEL_MINT_SEADROP[2:] in self.selectors)

    @property
    def soulbound(self) -> bool:
        return SEL_LOCKED[2:] in self.selectors

    @property
    def has_transfer_validator(self) -> bool:
        """Whether ``getTransferValidator()`` must be answerable for this contract."""
        if self.kind == "eip1167":
            return self.implementation_known  # the known implementation carries it
        return SEL_GET_TRANSFER_VALIDATOR[2:] in self.selectors


def contract_facts(code_hex: Any, impl_slot_word: Any) -> ContractFacts | None:
    """``eth_getCode`` + the EIP-1967 implementation slot -> :class:`ContractFacts`."""
    if not isinstance(code_hex, str) or not code_hex.startswith("0x"):
        return None
    try:
        code = bytes.fromhex(code_hex[2:])
    except ValueError:
        return None
    if not code:
        return ContractFacts(0, "empty")
    body = code.hex()
    if len(code) == 45 and body.startswith(EIP1167_PREFIX) and body.endswith(EIP1167_SUFFIX):
        return ContractFacts(45, "eip1167", "0x" + body[len(EIP1167_PREFIX):len(EIP1167_PREFIX) + 40])
    impl = _addr(impl_slot_word[2:]) if isinstance(impl_slot_word, str) and impl_slot_word.startswith("0x") else None
    if impl is not None and impl != ZERO_ADDRESS:
        return ContractFacts(len(code), "eip1967", impl, push4_selectors(code))
    return ContractFacts(len(code), "plain", None, push4_selectors(code))


@dataclass(frozen=True)
class CollectionCheck:
    """What two small batches of reads said about one NFT contract. ``None`` = not answered.

    ``reads`` is the number of JSON-RPC items spent, for the run's budget.
    """

    collection: str
    public_drop: PublicDrop | None
    facts: ContractFacts | None
    transfer_validator: str | None
    is_erc721: bool | None
    max_supply: int | None
    total_supply: int | None
    transport_ok: bool
    note: str | None = None
    reads: int = 0

    @property
    def headroom(self) -> int | None:
        if self.max_supply is None or self.total_supply is None:
            return None
        return self.max_supply - self.total_supply


def stage1_calls(nft: str) -> list[tuple[str, list[Any]]]:
    """Two reads: is a public stage open, and what code is this? A collection whose public
    stage is closed, upcoming or never configured stops here. (How many minting collections
    that is was NOT measured; the 6 collections read on 2026-10-02 -- 2 OpenSea-listed, 4
    seen minting -- all had a public stage open when read.)"""
    n = nft.lower()
    return [
        ("eth_call", [{"to": SEADROP, "data": SEL_GET_PUBLIC_DROP + pad_address(n)}, "latest"]),
        ("eth_getCode", [n, "latest"]),
    ]


def stage2_calls(nft: str, facts: ContractFacts | None) -> list[tuple[str, list[Any]]]:
    """The reads only an OPEN, recognisable drop earns, in :func:`parse_collection_check` order.

    A clone of a known implementation needs two (validator, mint stats): the implementation
    is a SeaDrop ERC-721 by inspection. Plain code also needs the EIP-1967 slot (a proxy can
    be larger than a stub) and the ERC-721 interface. Anything else fails the rule on its
    code alone, so nothing more is read for it.
    """
    n = nft.lower()
    validator = ("eth_call", [{"to": n, "data": SEL_GET_TRANSFER_VALIDATOR}, "latest"])
    stats = ("eth_call", [{"to": n, "data": SEL_GET_MINT_STATS + pad_address(ZERO_ADDRESS)}, "latest"])
    if facts is None:
        return []
    if facts.kind == "eip1167":
        return [validator, stats] if facts.implementation_known else []
    if facts.kind == "plain":
        return [
            ("eth_getStorageAt", [n, EIP1967_IMPL_SLOT, "latest"]),
            validator,
            ("eth_call", [{"to": n, "data": SEL_SUPPORTS_INTERFACE + IFACE_ERC721[2:].ljust(64, "0")}, "latest"]),
            stats,
        ]
    return []


def decode_mint_stats(result: Any) -> tuple[int, int] | None:
    """``getMintStats(minter)`` -> (currentTotalSupply, maxSupply), or ``None``."""
    ws = words(result)
    if len(ws) < 3:
        return None
    total, cap = _int(ws, 1), _int(ws, 2)
    return None if total is None or cap is None else (total, cap)


def parse_collection_check(nft: str, stage1: Sequence[Any], stage2: Sequence[Any] | None = None,
                           note: str | None = None) -> CollectionCheck:
    """Pure: stage-1 results (+ stage-2 results, ``None`` where a read reverted) -> check."""
    n = nft.lower()
    if len(stage1) < 2:
        return CollectionCheck(n, None, None, None, None, None, None, False, note or "no response")
    drop = decode_public_drop(stage1[0])
    facts = contract_facts(stage1[1], None)
    validator: str | None = None
    is_erc721: bool | None = None
    stats: tuple[int, int] | None = None
    reads = 2
    results = list(stage2 or [])
    if facts is not None and facts.kind == "eip1167" and len(results) >= 2:
        reads += 2
        v_words = words(results[0])
        validator = _addr(v_words[0]) if v_words else None
        stats = decode_mint_stats(results[1])
        is_erc721 = True  # the known implementation IS a SeaDrop ERC-721 (read 2026-10-02)
    elif facts is not None and facts.kind == "plain" and len(results) >= 4:
        reads += 4
        facts = contract_facts(stage1[1], results[0])
        v_words = words(results[1])
        validator = _addr(v_words[0]) if v_words else None
        i_words = words(results[2])
        is_erc721 = (_int(i_words, 0) == 1) if i_words else None
        stats = decode_mint_stats(results[3])
    return CollectionCheck(
        collection=n, public_drop=drop, facts=facts, transfer_validator=validator, is_erc721=is_erc721,
        total_supply=stats[0] if stats else None, max_supply=stats[1] if stats else None,
        transport_ok=True, note=note, reads=reads,
    )


# --------------------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------------------


class RpcAnswer(Protocol):
    results: list[Any]
    ok: bool
    note: str | None


#: ``(calls, endpoint) -> answer``. ``answer.results`` is empty on a transport failure and
#: otherwise one entry per call, ``None`` where that call errored (``rpc_batch``'s shape).
Rpc = Callable[[Sequence[tuple[str, list[Any]]], str], RpcAnswer]


def default_rpc(conn: Any = None, *, wait_for_slot_s: float = 5.0, timeout_s: float = 20.0) -> Rpc:
    """The production transport: ``kaiba.ingest.robinhood.rpc_batch`` at :data:`READ_PRIORITY`."""

    def call(calls: Sequence[tuple[str, list[Any]]], endpoint: str) -> RpcAnswer:
        from kaiba.ingest.robinhood import rpc_batch

        return rpc_batch(calls, endpoint=endpoint, priority=READ_PRIORITY, conn=conn,
                         wait_for_slot_s=wait_for_slot_s, timeout_s=timeout_s)

    return call


def read_collection(nft: str, rpc: Rpc, *, at_s: int | None = None) -> CollectionCheck:
    """Two small HTTP batches at most: stage 1 always, stage 2 only for a drop whose public
    stage is open at ``at_s`` and whose code is recognisable.

    Kept small on purpose. MEASURED 2026-10-02 from the workstation: a single 7-read batch
    sent 2.5 s after a 4-read batch was answered HTTP 429 by the public RPC, while 1- and
    4-read batches went through; the endpoint's quota evidently counts reads, not requests.
    """
    first = rpc(stage1_calls(nft), ENDPOINT_CHECK)
    if not first.results:
        return CollectionCheck(nft.lower(), None, None, None, None, None, None, False, first.note or "no response")
    check = parse_collection_check(nft, first.results, None, first.note)
    drop = check.public_drop
    if drop is None or (at_s is not None and not drop.open_at(at_s)):
        return check
    calls = stage2_calls(nft, check.facts)
    if not calls:
        return check
    second = rpc(calls, ENDPOINT_CHECK)
    if not second.results:
        return CollectionCheck(nft.lower(), drop, check.facts, None, None, None, None, False,
                               second.note or "no response", reads=2)
    return parse_collection_check(nft, first.results, second.results, second.note or first.note)


def tape_chunk_calls(from_block: int, to_block: int, *,
                     with_from_anchor: bool = True) -> list[tuple[str, list[Any]]]:
    """One HTTP batch per block range, in this order: the ``to`` anchor block, SeaDrop
    mints, Seaport fills, and (only when the cursor has no anchor of its own yet) the
    ``from`` anchor block. A warm tape reuses the previous chunk's end as its start anchor,
    so a steady chunk is three reads, not four."""
    span = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    calls: list[tuple[str, list[Any]]] = [
        ("eth_getBlockByNumber", [hex(to_block), False]),
        ("eth_getLogs", [{**span, "address": SEADROP, "topics": [TOPIC_SEADROP_MINT]}]),
        ("eth_getLogs", [{**span, "address": SEAPORT, "topics": [TOPIC_ORDER_FULFILLED]}]),
    ]
    if with_from_anchor:
        calls.append(("eth_getBlockByNumber", [hex(from_block), False]))
    return calls


__all__ = [
    "CollectionCheck",
    "ContractFacts",
    "Item",
    "KNOWN_SEADROP_IMPLEMENTATIONS",
    "KNOWN_TRANSFER_VALIDATORS",
    "OrderFulfilled",
    "PublicDrop",
    "Rpc",
    "Sale",
    "SeaDropMint",
    "classify",
    "decode_mint_stats",
    "contract_facts",
    "decode_order_fulfilled",
    "decode_public_drop",
    "decode_seadrop_mint",
    "default_rpc",
    "parse_collection_check",
    "push4_selectors",
    "read_collection",
    "stage1_calls",
    "stage2_calls",
    "tape_chunk_calls",
]
