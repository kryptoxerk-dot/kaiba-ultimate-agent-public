"""Realised fill prices: from the fill itself, in USD only with a contemporaneous sample.

The stop-loss on a live position is cut from ``positions.entry_price_usd``. Until this
module existed that number was the pre-trade dossier quote — the price *before* the trade,
excluding the slippage actually paid, which on a curve snipe is routinely 10%+. A too-low
entry is a too-low stop, which exits late, on real money. The ledger's own docstring
called it the weakest number in the module. This module replaces it with a measured one.

**What a fill establishes, exactly.** A filled order records the native it paid
(``amount_in``, or the provider's ``input_amount`` when it reports one) and the token
atoms it received (``filled_out``). Their ratio *is* the realised price in native per
token, slippage included, and it needs no provider at all: two integers and the token's
decimals. :func:`ratio` computes it in ``Decimal`` from the integers, never through a
float, and that native figure is stored on every fill whether or not a USD figure can be
attached to it.

**What has to be looked up, and what is refused.**

* *Decimals* are read from the mint account over the RPC once per token and stored on
  ``tokens`` (:func:`token_decimals`). The paper broker guesses 6 for Solana and 18 for
  EVM; a wrong guess is a 10^12 error in the quantity, so the live path here never
  guesses. No decimals means no per-token price and an ``unavailable`` basis.
* *Native/USD* comes from :mod:`kaiba.providers.native_price`: the stored sample nearest
  to the fill's own instant, within a tolerance, with the distance reported. When no such
  sample exists the USD figure is ``None`` and the basis says so. It is never the current
  price stamped on an old fill.

**Basis values**, which land in the ``entry_price_basis`` field of the position events and
in ``fill_prices.basis``:

* :data:`BASIS_FILL` — the caller supplied the venue's own realised USD price.
* :data:`BASIS_FILL_RATIO` — the fill's native/token ratio converted with a contemporaneous
  native sample. **Measured. The only basis a stop may be built from.**
* :data:`BASIS_FILL_RATIO_NATIVE_ONLY` — the ratio is exact but no contemporaneous native
  sample exists, so there is no USD figure. Recorded, not used for a stop.
* :data:`BASIS_UNAVAILABLE` — the fill's legs or the token's decimals are not known.

**Reconciliation against the chain** (:func:`reconcile_onchain`) reads the actual balance
changes from the transaction and sets them beside what the order claims, with the delta.
It catches a venue reporting one fill and settling another. It records; it never
overwrites the order or the position, because a discrepancy is a fact to be looked at, not
a value to be replaced.

Money is integers in base units; USD is ``Decimal``; nothing here calls ``float()``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any

from kaiba.core import journal
from kaiba.core.config import get_settings
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.limiter import Priority
from kaiba.core.schemas import (
    EVM_CHAINS,
    NATIVE_DECIMALS,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    EvidenceBasis,
    Order,
    OrderState,
    Receipt,
    Side,
    now_ms,
)
from kaiba.providers import native_price
from kaiba.providers._http import cache_path, post_json, redact_text

log = logging.getLogger(__name__)

BASIS_FILL = "fill"
BASIS_FILL_RATIO = "fill_ratio"
BASIS_FILL_RATIO_NATIVE_ONLY = "fill_ratio_native_only"
BASIS_UNAVAILABLE = "unavailable"

#: Bases a stop may be cut from. Everything else is recorded and never armed against.
STOP_BASES: frozenset[str] = frozenset({BASIS_FILL, BASIS_FILL_RATIO})

DECIMALS_VERIFIED = "verified_onchain"
DECIMALS_TOKENS_ROW = "tokens_row"
DECIMALS_CALLER = "caller"
DECIMALS_UNAVAILABLE = "unavailable"

VERDICT_AGREE = "agree"
VERDICT_DISAGREE = "disagree"
VERDICT_UNAVAILABLE = "unavailable"

#: Limiter provider for the public JSON-RPC endpoints; ``kaiba.core.limiter.DEFAULTS``
#: already carries an entry for it.
RPC_PROVIDER = "rpc"

#: Required by docs/CONTRACT.md. A reconciliation is one call, but the decimals lookup
#: happens inside the fill path where a limiter refusal would silently cost a stop.
DEFAULT_WAIT_FOR_SLOT_S = 5.0

#: The venue leg of a fill may differ from what the venue reported by its own fee take,
#: rounding, and whether the reported figure was gross or net of fees. Three percent is
#: wide enough to absorb pump.fun's 1.25% and PumpSwap's tiers plus a tip, and narrow
#: enough that a venue settling a different trade cannot hide inside it. INVENTED; the
#: bps delta is recorded on every row so the band can be recalibrated from data.
DEFAULT_MAX_NATIVE_DELTA_BPS = 300

#: Tokens must match exactly. There is no legitimate reason for a venue to report a
#: token quantity other than the one that landed in the wallet.
MAX_TOKEN_DELTA_ATOMS = 0

#: ``maxSupportedTransactionVersion`` on every ``getTransaction``: the highest transaction
#: version this client claims it can render.
#:
#: This was pinned at 0 and it cost real coverage. Measured on 2026-09-20 against a random
#: sample of 24 pump.fun swaps from our own ``swaps`` table: **5 of 24 (21%) were version-1
#: transactions** and came back ``-32015 Transaction version (1) is not supported``, which
#: this module then reported as ``unavailable`` — no reconciliation, and on the fill path
#: no entry price and therefore no stop. Refusing a fill over a version number is not
#: caution, it is a blind position.
#:
#: Accepting any version is safe *for this parser specifically*, because
#: :func:`parse_solana_transaction` never reads the encoded message: it reads
#: ``meta.pre/postBalances`` indexed against ``accountKeys`` plus ``meta.loadedAddresses``,
#: and ``meta.pre/postTokenBalances`` by ``accountIndex``. Those are version-independent,
#: and ``loadedAddresses`` is exactly the escape hatch for versioned address-lookup-table
#: accounts. The value is deliberately far above anything that exists so a new version does
#: not silently reopen the same gap; the public RPC accepts any integer (verified at 2, 5
#: and 128 on the same date).
MAX_TX_VERSION = 128

#: Fifty significant digits for the one division in this module. A ratio of two integers
#: is not always representable, so "exact" here means exact to fifty digits — far beyond
#: anything downstream can use, and with no binary floating point anywhere in the path.
_CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)

_ERC20_DECIMALS_SELECTOR = "0x313ce567"


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _int(value: Any, default: int | None = None) -> int | None:
    """Base units as an integer. Never ``float()``; a bool is never a money value."""
    if value is None or value == "" or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            d = Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return default
        return int(d) if d == d.to_integral_value() else default


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _s(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return format(value, "f")


def _receipt(endpoint: str, basis: EvidenceBasis, note: str | None = None, **kw: Any) -> Receipt:
    return Receipt(
        provider=RPC_PROVIDER,
        endpoint=endpoint,
        basis=basis,
        note=redact_text(note)[:300] if note else None,
        **kw,
    )


def ratio(native_atoms: int, token_atoms: int, native_decimals: int, token_decimals: int) -> Decimal:
    """Native units per whole token, from the two integer legs of a fill.

    ``(native_atoms / 10^native_decimals) / (token_atoms / 10^token_decimals)``, rearranged
    so the only division is one integer by another under a fifty-digit context. Raises on
    a non-positive leg because a price from a zero leg is not a price.
    """
    native_atoms = int(native_atoms)
    token_atoms = int(token_atoms)
    if native_atoms <= 0 or token_atoms <= 0:
        raise ValueError("both legs of a fill must be positive")
    if native_decimals < 0 or token_decimals < 0:
        raise ValueError("decimals cannot be negative")
    with localcontext(_CTX):
        numerator = Decimal(native_atoms * (10 ** int(token_decimals)))
        denominator = Decimal(token_atoms * (10 ** int(native_decimals)))
        return numerator / denominator


# --------------------------------------------------------------------------------------
# RPC
# --------------------------------------------------------------------------------------


def _purge_cache(endpoint: str, cache_key: str) -> None:
    """Drop a cached JSON-RPC response. Best effort; a cache we cannot clear is not fatal."""
    try:
        cache_path(RPC_PROVIDER, cache_key).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - a locked cache file is not worth failing on
        log.debug("could not purge cached %s response: %s", endpoint, exc)


def _rpc(
    chain: Chain,
    method: str,
    params: list[Any],
    *,
    cache_key: str,
    ttl_s: float,
    conn: sqlite3.Connection | None,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
    timeout_s: float = 15.0,
) -> tuple[Any, Receipt]:
    """One JSON-RPC call to the chain's configured public endpoint. Never raises.

    ``cache_key`` is mandatory because ``_http.request_json`` keys its disk cache on the
    URL and query parameters only — every JSON-RPC call to one endpoint would otherwise
    share a single cache slot.
    """
    family = "eth" if chain in EVM_CHAINS else chain.value
    endpoint = f"{family}.{method}"
    url = get_settings().rpc_for(chain)
    if not url:
        return None, _receipt(endpoint, EvidenceBasis.UNAVAILABLE, f"no RPC url configured for {chain.value}")
    got = post_json(
        RPC_PROVIDER,
        endpoint,
        url,
        json_body={"jsonrpc": "2.0", "id": f"kaiba-{method}", "method": method, "params": params},
        priority=Priority.POSITION,
        ttl_s=ttl_s,
        cache_key=cache_key,
        timeout_s=timeout_s,
        retries=2,
        wait_for_slot_s=wait_for_slot_s,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, dict):
        return None, got.receipt
    err = got.data.get("error")
    if err:
        # A JSON-RPC error arrives as HTTP 200, so ``_http`` has just written it to the
        # disk cache as though it were data, and every retry for the next ``ttl_s`` would
        # be served that error without a request going out. That is how a *fixed* bug goes
        # on looking broken: raising ``maxSupportedTransactionVersion`` changed nothing for
        # an hour because the refusals were being replayed from disk. Drop the entry so the
        # next call is a real one.
        _purge_cache(endpoint, cache_key)
        detail = json.dumps(err, default=str)[:200]
        return None, _receipt(endpoint, EvidenceBasis.UNAVAILABLE, f"rpc error: {detail}")
    return got.data.get("result"), got.receipt


# --------------------------------------------------------------------------------------
# decimals from the chain
# --------------------------------------------------------------------------------------


def parse_solana_mint_decimals(result: Any) -> tuple[int | None, str]:
    """Decimals from a ``getAccountInfo`` (jsonParsed) result, or ``(None, why)``."""
    if not isinstance(result, dict):
        return None, "malformed getAccountInfo result"
    value = result.get("value")
    if not isinstance(value, dict):
        return None, "no such account"
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict):
        return None, f"account data is not parsed token data (owner {value.get('owner')})"
    if str(parsed.get("type") or "") != "mint":
        return None, f"account is a {parsed.get('type')!r}, not a mint"
    info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
    dec = _int(info.get("decimals"))
    if dec is None or not 0 <= dec <= 36:
        return None, f"mint reports decimals={info.get('decimals')!r}"
    return dec, "ok"


def parse_evm_decimals(result: Any) -> tuple[int | None, str]:
    """Decimals from an ``eth_call`` to ``decimals()``: a 32-byte hex word."""
    if not isinstance(result, str) or not result.startswith("0x"):
        return None, "malformed eth_call result"
    body = result[2:]
    if not body:
        return None, "empty eth_call result: not an ERC-20, or no decimals()"
    try:
        dec = int(body, 16)
    except ValueError:
        return None, "non-hex eth_call result"
    if not 0 <= dec <= 36:
        return None, f"contract reports decimals={dec}"
    return dec, "ok"


def _fetch_decimals(
    chain: Chain, token: str, conn: sqlite3.Connection | None, *, wait_for_slot_s: float
) -> tuple[int | None, Receipt]:
    key = f"decimals:{chain.value}:{token}"
    if chain is Chain.SOL:
        result, receipt = _rpc(
            chain,
            "getAccountInfo",
            [token, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            cache_key=key,
            ttl_s=86_400.0,  # a mint's decimals never change
            conn=conn,
            wait_for_slot_s=wait_for_slot_s,
        )
        if result is None:
            return None, receipt
        dec, why = parse_solana_mint_decimals(result)
    elif chain in EVM_CHAINS:
        result, receipt = _rpc(
            chain,
            "eth_call",
            [{"to": token, "data": _ERC20_DECIMALS_SELECTOR}, "latest"],
            cache_key=key,
            ttl_s=86_400.0,
            conn=conn,
            wait_for_slot_s=wait_for_slot_s,
        )
        if result is None:
            return None, receipt
        dec, why = parse_evm_decimals(result)
    else:
        return None, _receipt("decimals", EvidenceBasis.UNAVAILABLE, f"no decimals source for {chain.value}")
    if dec is None:
        return None, receipt.model_copy(update={"basis": EvidenceBasis.UNAVAILABLE, "note": why[:300]})
    return dec, receipt.model_copy(update={"basis": EvidenceBasis.VERIFIED_ONCHAIN})


def _store_decimals(
    conn: sqlite3.Connection, chain: Chain, token: str, decimals: int, *, register: bool
) -> bool:
    """Write verified decimals onto the ``tokens`` row. Returns whether a row now holds them."""
    row = fetch_one(conn, "SELECT decimals, meta_json FROM tokens WHERE chain=? AND address=?", (chain.value, token))
    stamp = now_ms()
    if row is None:
        if not register:
            return False
        conn.execute(
            "INSERT INTO tokens (chain, address, decimals, first_seen_ms, meta_json) VALUES (?,?,?,?,?)",
            (chain.value, token, int(decimals), stamp,
             jdump({"decimals_source": DECIMALS_VERIFIED, "decimals_verified_ms": stamp})),
        )
        return True
    prior = _int(row["decimals"])
    if prior is not None and prior != decimals:
        # The chain is the authority. Whatever wrote the row was wrong by 10^|diff|, and
        # anything that sized on it was too — say so where it will be seen.
        emit(
            EventKind.SYSTEM,
            {
                "event": "token_decimals_conflict",
                "token": token,
                "tokens_row": prior,
                "onchain": int(decimals),
                "impact": "quantities computed with the stored value are off by a power of ten; "
                "the chain value now replaces it",
            },
            chain=chain,
            subject=token,
            level="error",
            dedupe_key=f"fills:decimals_conflict:{chain.value}:{token}",
            conn=conn,
        )
    meta = jload(row["meta_json"], {}) or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["decimals_source"] = DECIMALS_VERIFIED
    meta["decimals_verified_ms"] = stamp
    conn.execute(
        "UPDATE tokens SET decimals=?, meta_json=? WHERE chain=? AND address=?",
        (int(decimals), jdump(meta), chain.value, token),
    )
    return True


def token_decimals(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    fetch: bool = True,
    register: bool = False,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> tuple[int | None, str, str | None]:
    """``(decimals, basis, note)`` for a token. Never a guess.

    Order of authority:

    1. the ``tokens`` row, when its ``meta_json`` says the value was verified on chain;
    2. the chain, read once via the public RPC and written back to the row;
    3. the ``tokens`` row's unverified value (a provider payload wrote it), with a note;
    4. nothing — ``(None, "unavailable", why)``.

    The chain read only happens for a token that already has a ``tokens`` row unless
    ``register=True``. ``tokens`` is the registry of what the agent has ingested and every
    engine-traded token is in it by construction; a fill on an unregistered token is an
    anomaly that should surface as one rather than be quietly enriched from the execution
    path. Callers such as the backfill pass ``register=True`` deliberately.
    """
    c = conn or get_conn()
    try:
        row = fetch_one(c, "SELECT decimals, meta_json FROM tokens WHERE chain=? AND address=?", (chain.value, token))
    except sqlite3.Error as exc:
        return None, DECIMALS_UNAVAILABLE, f"tokens row unreadable: {exc}"
    stored = _int(row["decimals"]) if row else None
    meta = (jload(row["meta_json"], {}) or {}) if row else {}
    verified = isinstance(meta, dict) and meta.get("decimals_source") == DECIMALS_VERIFIED
    if stored is not None and verified:
        return stored, DECIMALS_VERIFIED, None

    if fetch and (row is not None or register):
        dec, receipt = _fetch_decimals(chain, token, c, wait_for_slot_s=wait_for_slot_s)
        if dec is not None:
            _store_decimals(c, chain, token, dec, register=register)
            return dec, DECIMALS_VERIFIED, None
        why = f"chain read failed: {receipt.note}"
        if stored is not None:
            return stored, DECIMALS_TOKENS_ROW, f"unverified tokens-row value; {why}"
        return None, DECIMALS_UNAVAILABLE, why

    if stored is not None:
        return stored, DECIMALS_TOKENS_ROW, "unverified tokens-row value; chain read not attempted"
    if row is None:
        return None, DECIMALS_UNAVAILABLE, "token not in the tokens registry; decimals not fetched"
    return None, DECIMALS_UNAVAILABLE, "tokens row has no decimals and the chain read was not attempted"


# --------------------------------------------------------------------------------------
# the fill price
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FillPrice:
    """What one fill established, with the provenance of every number in it.

    ``price_native_per_token`` is exact from the legs and the decimals; it is ``None`` only
    when the decimals are unknown. ``price_usd`` is that ratio times the native sample
    nearest the fill, or ``None`` when no sample was contemporaneous. ``basis`` says which.
    """

    order_id: str
    chain: Chain
    token: str
    side: Side
    fill_ts_ms: int
    fill_ts_basis: str
    native_atoms: int
    token_atoms: int
    fee_native: int | None
    token_decimals: int | None
    decimals_basis: str
    price_native_per_token: Decimal | None
    price_native_all_in: Decimal | None
    price_usd: Decimal | None
    native_usd: Decimal | None
    native_sample_ts_ms: int | None
    native_sample_distance_ms: int | None
    basis: str
    notes: tuple[str, ...] = ()
    computed_ms: int = field(default_factory=now_ms)

    @property
    def usable_for_stop(self) -> bool:
        """Only a measured USD price may set the entry a stop is cut from."""
        return self.price_usd is not None and self.basis in STOP_BASES

    def event_fields(self) -> dict[str, Any]:
        """The provenance, in the shape the position events carry."""
        return {
            "entry_price_basis": self.basis,
            "fill_ts_ms": self.fill_ts_ms,
            "fill_ts_basis": self.fill_ts_basis,
            "price_native_per_token": _s(self.price_native_per_token),
            "token_decimals": self.token_decimals,
            "decimals_basis": self.decimals_basis,
            "native_usd": _s(self.native_usd),
            "native_sample_distance_ms": self.native_sample_distance_ms,
            "fill_notes": list(self.notes),
        }


def _fill_time(
    order: Order, c: sqlite3.Connection, fill_ts_ms: int | None, fill_ts_basis: str | None
) -> tuple[int, str]:
    """The instant to price the fill at, and where that instant came from.

    A block time from a stored chain reconciliation beats the order's ``updated_ms``, which
    is the moment the order was recorded FILLED — within reconcile latency of the real
    fill, and the best the row itself can offer.
    """
    if fill_ts_ms is not None:
        return int(fill_ts_ms), fill_ts_basis or "caller"
    try:
        row = fetch_one(
            c,
            "SELECT block_time_ms FROM fill_reconciliations WHERE order_id=? AND block_time_ms IS NOT NULL "
            "ORDER BY checked_ms DESC LIMIT 1",
            (order.order_id,),
        )
    except sqlite3.Error:
        row = None
    if row and row["block_time_ms"]:
        return int(row["block_time_ms"]), "block_time"
    return int(order.updated_ms), "order_updated"


def derive(
    order: Order,
    *,
    native_atoms: Any,
    token_atoms: Any,
    conn: sqlite3.Connection | None = None,
    fill_ts_ms: int | None = None,
    fill_ts_basis: str | None = None,
    tolerance_ms: int = native_price.DEFAULT_TOLERANCE_MS,
    decimals: int | None = None,
    fetch_decimals: bool = True,
    register_token: bool = False,
) -> FillPrice:
    """The realised price of one fill. Never raises; never guesses.

    ``native_atoms`` is the native leg in base units, fees excluded — what was swapped, not
    what the transaction cost all in. ``token_atoms`` is the token leg. On a buy that is
    (spent, received); on a sell it is (received, sold).
    """
    c = conn or get_conn()
    notes: list[str] = []
    n = _int(native_atoms, 0) or 0
    t = _int(token_atoms, 0) or 0
    fee = _int(order.fee_native)
    ts, ts_basis = _fill_time(order, c, fill_ts_ms, fill_ts_basis)

    base = dict(
        order_id=order.order_id, chain=order.chain, token=order.token, side=order.side,
        fill_ts_ms=ts, fill_ts_basis=ts_basis, native_atoms=n, token_atoms=t, fee_native=fee,
    )
    if n <= 0 or t <= 0:
        notes.append(f"both legs must be positive integers (native={n}, token={t})")
        return FillPrice(
            **base, token_decimals=None, decimals_basis=DECIMALS_UNAVAILABLE,
            price_native_per_token=None, price_native_all_in=None, price_usd=None, native_usd=None,
            native_sample_ts_ms=None, native_sample_distance_ms=None, basis=BASIS_UNAVAILABLE,
            notes=tuple(notes),
        )

    if decimals is not None:
        dec, dec_basis, dec_note = int(decimals), DECIMALS_CALLER, None
    else:
        dec, dec_basis, dec_note = token_decimals(
            order.chain, order.token, c, fetch=fetch_decimals, register=register_token
        )
    if dec_note:
        notes.append(f"decimals: {dec_note}")

    native_dec = NATIVE_DECIMALS[order.chain]
    per_token: Decimal | None = None
    all_in: Decimal | None = None
    if dec is not None:
        per_token = ratio(n, t, native_dec, dec)
        all_in = ratio(n + (fee or 0), t, native_dec, dec) if fee else per_token

    looked = native_price.at(order.chain, ts, c, tolerance_ms=tolerance_ms)
    if looked.receipt.note:
        notes.append(f"native: {looked.receipt.note}")

    if per_token is None:
        basis = BASIS_UNAVAILABLE
        usd: Decimal | None = None
    elif looked.known and looked.price_usd is not None:
        basis = BASIS_FILL_RATIO
        with localcontext(_CTX):
            usd = per_token * looked.price_usd
    else:
        basis = BASIS_FILL_RATIO_NATIVE_ONLY
        usd = None

    return FillPrice(
        **base,
        token_decimals=dec,
        decimals_basis=dec_basis,
        price_native_per_token=per_token,
        price_native_all_in=all_in,
        price_usd=usd,
        native_usd=looked.price_usd if looked.known else None,
        native_sample_ts_ms=looked.sample_ts_ms,
        native_sample_distance_ms=looked.distance_ms,
        basis=basis,
        notes=tuple(notes),
    )


def record(conn: sqlite3.Connection, fp: FillPrice) -> None:
    """Store the derivation. Replaces an earlier derivation for the same order: this row
    is computed from the order, not observed, so a recomputation with a better fill time
    or a newly verified decimals value is the more accurate row, not a competing record."""
    conn.execute(
        "INSERT OR REPLACE INTO fill_prices (order_id, chain, token, side, fill_ts_ms, fill_ts_basis, "
        "native_atoms, token_atoms, fee_native, token_decimals, decimals_basis, price_native_per_token, "
        "price_native_all_in, price_usd, native_usd, native_sample_ts_ms, native_sample_distance_ms, "
        "basis, computed_ms, notes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fp.order_id, fp.chain.value, fp.token, fp.side.value, fp.fill_ts_ms, fp.fill_ts_basis,
            str(fp.native_atoms), str(fp.token_atoms), _s(fp.fee_native), fp.token_decimals,
            fp.decimals_basis, _s(fp.price_native_per_token), _s(fp.price_native_all_in),
            _s(fp.price_usd), _s(fp.native_usd), fp.native_sample_ts_ms, fp.native_sample_distance_ms,
            fp.basis, fp.computed_ms, jdump(list(fp.notes)),
        ),
    )


def load(conn: sqlite3.Connection, order_id: str) -> FillPrice | None:
    row = fetch_one(conn, "SELECT * FROM fill_prices WHERE order_id=?", (order_id,))
    if not row:
        return None
    return FillPrice(
        order_id=row["order_id"], chain=Chain(row["chain"]), token=row["token"], side=Side(row["side"]),
        fill_ts_ms=int(row["fill_ts_ms"]), fill_ts_basis=row["fill_ts_basis"],
        native_atoms=_int(row["native_atoms"], 0) or 0, token_atoms=_int(row["token_atoms"], 0) or 0,
        fee_native=_int(row["fee_native"]), token_decimals=_int(row["token_decimals"]),
        decimals_basis=row["decimals_basis"], price_native_per_token=_dec(row["price_native_per_token"]),
        price_native_all_in=_dec(row["price_native_all_in"]), price_usd=_dec(row["price_usd"]),
        native_usd=_dec(row["native_usd"]), native_sample_ts_ms=_int(row["native_sample_ts_ms"]),
        native_sample_distance_ms=_int(row["native_sample_distance_ms"]), basis=row["basis"],
        notes=tuple(jload(row["notes_json"], []) or []), computed_ms=int(row["computed_ms"]),
    )


# --------------------------------------------------------------------------------------
# reconciliation against the chain
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OnchainFill:
    """The balance changes one transaction actually made, read from the chain.

    There are two defensible answers to "how much native did this fill move", and which one
    a venue reports turns out to be a property of the venue. Both are computed here; see
    :attr:`wallet_leg` and :attr:`venue_leg`.

    ``venue`` is the counterparty that holds the token on the other side of the swap — the
    owner of the token vault whose balance moved against the wallet's.
    """

    chain: Chain
    signature: str
    wallet: str
    token: str
    slot: int | None
    block_time_ms: int | None
    tx_fee: int | None
    failed: bool
    wallet_native_delta: int
    wallet_wsol_delta: int
    wallet_token_delta: int
    venue: str | None
    venue_native_delta: int
    venue_wsol_delta: int
    token_decimals: int | None
    receipt: Receipt

    @property
    def buying(self) -> bool:
        """Which way the token moved for the wallet. The chain's opinion, not the venue's."""
        return self.wallet_token_delta > 0

    @property
    def venue_leg(self) -> int:
        """Native that entered or left the counterparty pool, as a magnitude.

        Correct for a single-hop trade against a pool that holds native directly — a
        pump.fun bonding curve, where this matches the venue's reported figure to the
        lamport. **Wrong, and often zero, for an aggregator**: a Jupiter route hops through
        pools the wallet never touches, wraps and unwraps SOL in accounts owned by nobody
        we identified as the counterparty, and leaves this measure reading 0 while real
        money moved. Kept as corroboration, not as the primary measure.
        """
        return abs(self.venue_native_delta if self.venue_native_delta != 0 else self.venue_wsol_delta)

    @property
    def all_in_native(self) -> int:
        """What the wallet paid (+) or received (−) net, fees, tips and rent included."""
        return -(self.wallet_native_delta + self.wallet_wsol_delta)

    @property
    def wallet_leg(self) -> int:
        """What the wallet parted with or received for the swap, net of the chain fee.

        The wallet's own native delta (lamports plus wrapped SOL, so a route that wraps
        mid-transaction is still counted once) with the transaction fee taken back out,
        because the fee is paid to the validator and is not part of the trade.

        **This is the primary measure.** Measured on 2026-09-20 against 13 real swaps
        spanning the pump.fun curve, PumpSwap, Jupiter, Whirlpool, FLASHX and two other
        AMMs: this figure matched what the venue reported in **13 of 13**, and in 12 of
        those to the exact lamport. :attr:`venue_leg` matched 1 of 13. The one non-exact
        case was 10,000 lamports on 18.4 SOL (0.005 bps) of fee attribution.

        It still includes venue fees, tips and account rent, which is why the pump.fun
        curve case sits ~177 bps above the curve's own figure rather than on it: the 1%
        pump.fun fee and the associated-token-account rent leave the wallet but never
        enter the curve. That is a real difference in what is being counted, not an error,
        and it is inside the agreement band.
        """
        fee = self.tx_fee or 0
        return (self.all_in_native - fee) if self.buying else (-self.all_in_native + fee)


@dataclass(frozen=True)
class Reconciliation:
    """A claimed fill beside the chain's version of it, with the delta. Never a rewrite."""

    order_id: str | None
    chain: Chain
    signature: str
    wallet: str
    token: str
    side: Side | None
    slot: int | None
    block_time_ms: int | None
    claimed_native: int | None
    claimed_tokens: int | None
    onchain_native: int | None
    onchain_native_all_in: int | None
    onchain_tx_fee: int | None
    onchain_tokens: int | None
    delta_native: int | None
    delta_tokens: int | None
    delta_native_bps: int | None
    verdict: str
    detail: str
    receipt: Receipt
    #: Which chain-side measure the verdict rests on: ``wallet_leg``, ``venue_leg``, or
    #: ``none`` when neither was within the band (or nothing was claimed to compare).
    #: Derivable from the stored columns, so it is carried on the object and in the event
    #: payload rather than adding a column to an applied migration.
    native_basis: str = "none"
    checked_ms: int = field(default_factory=now_ms)

    @property
    def agrees(self) -> bool:
        return self.verdict == VERDICT_AGREE

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id, "chain": self.chain.value, "signature": self.signature,
            "wallet": self.wallet, "token": self.token, "side": self.side.value if self.side else None,
            "slot": self.slot, "block_time_ms": self.block_time_ms,
            "claimed_native": _s(self.claimed_native), "claimed_tokens": _s(self.claimed_tokens),
            "onchain_native": _s(self.onchain_native), "onchain_native_all_in": _s(self.onchain_native_all_in),
            "onchain_tx_fee": _s(self.onchain_tx_fee), "onchain_tokens": _s(self.onchain_tokens),
            "delta_native": _s(self.delta_native), "delta_tokens": _s(self.delta_tokens),
            "delta_native_bps": self.delta_native_bps, "native_basis": self.native_basis,
            "verdict": self.verdict, "detail": self.detail,
        }


def _account_keys(result: Mapping[str, Any]) -> list[str]:
    """Static keys then loaded (writable, readonly): the order ``pre/postBalances`` use."""
    message = (result.get("transaction") or {}).get("message") or {}
    keys: list[str] = []
    for k in message.get("accountKeys") or []:
        keys.append(str(k.get("pubkey")) if isinstance(k, dict) else str(k))
    loaded = (result.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(str(k) for k in loaded.get("writable") or [])
    keys.extend(str(k) for k in loaded.get("readonly") or [])
    return keys


def _first_signer(result: Mapping[str, Any]) -> str | None:
    message = (result.get("transaction") or {}).get("message") or {}
    for k in message.get("accountKeys") or []:
        if isinstance(k, dict) and k.get("signer"):
            return str(k.get("pubkey"))
    keys = message.get("accountKeys") or []
    if keys:
        first = keys[0]
        return str(first.get("pubkey")) if isinstance(first, dict) else str(first)
    return None


def _token_deltas(meta: Mapping[str, Any], mint: str) -> tuple[dict[str, int], int | None]:
    """Per-owner atom delta for ``mint`` across pre/post token balances, plus its decimals."""
    pre: dict[int, tuple[str, int]] = {}
    post: dict[int, tuple[str, int]] = {}
    decimals: int | None = None
    for bucket, source in ((pre, meta.get("preTokenBalances")), (post, meta.get("postTokenBalances"))):
        for entry in source or []:
            if not isinstance(entry, dict) or str(entry.get("mint")) != mint:
                continue
            owner = str(entry.get("owner") or "")
            amount = _int(((entry.get("uiTokenAmount") or {}).get("amount")), 0) or 0
            idx = _int(entry.get("accountIndex"))
            if idx is None:
                continue
            bucket[idx] = (owner, amount)
            d = _int((entry.get("uiTokenAmount") or {}).get("decimals"))
            if d is not None:
                decimals = d
    by_owner: dict[str, int] = {}
    for idx in set(pre) | set(post):
        owner_pre, amount_pre = pre.get(idx, ("", 0))
        owner_post, amount_post = post.get(idx, ("", 0))
        owner = owner_post or owner_pre
        if not owner:
            continue
        by_owner[owner] = by_owner.get(owner, 0) + (amount_post - amount_pre)
    return by_owner, decimals


def parse_solana_transaction(
    result: Mapping[str, Any], *, token: str, wallet: str | None = None, signature: str = ""
) -> OnchainFill | None:
    """Balance changes of one ``getTransaction`` (jsonParsed) result. Pure.

    Returns ``None`` when the wallet's balance in ``token`` did not move at all, which
    means the transaction is not a fill of that token for that wallet.
    """
    if not isinstance(result, Mapping):
        return None
    meta = result.get("meta") or {}
    keys = _account_keys(result)
    who = wallet or _first_signer(result)
    if not who or not keys:
        return None
    pre_l = [(_int(x, 0) or 0) for x in meta.get("preBalances") or []]
    post_l = [(_int(x, 0) or 0) for x in meta.get("postBalances") or []]
    lamports: dict[str, int] = {}
    for i, key in enumerate(keys):
        if i < len(pre_l) and i < len(post_l):
            lamports[key] = lamports.get(key, 0) + (post_l[i] - pre_l[i])

    token_by_owner, decimals = _token_deltas(meta, token)
    wsol_by_owner, _ = _token_deltas(meta, SOL_NATIVE_MINT)
    wallet_token = token_by_owner.get(who, 0)
    if wallet_token == 0:
        return None
    others = {o: d for o, d in token_by_owner.items() if o != who and d != 0}
    venue = max(others, key=lambda o: abs(others[o])) if others else None

    block_time = _int(result.get("blockTime"))
    return OnchainFill(
        chain=Chain.SOL,
        signature=signature,
        wallet=who,
        token=token,
        slot=_int(result.get("slot")),
        block_time_ms=block_time * 1000 if block_time is not None else None,
        tx_fee=_int(meta.get("fee")),
        failed=meta.get("err") is not None,
        wallet_native_delta=lamports.get(who, 0),
        wallet_wsol_delta=wsol_by_owner.get(who, 0),
        wallet_token_delta=wallet_token,
        venue=venue,
        venue_native_delta=lamports.get(venue, 0) if venue else 0,
        venue_wsol_delta=wsol_by_owner.get(venue, 0) if venue else 0,
        token_decimals=decimals,
        receipt=Receipt(provider=RPC_PROVIDER, endpoint="sol.getTransaction", basis=EvidenceBasis.VERIFIED_ONCHAIN),
    )


def fetch_onchain_fill(
    chain: Chain,
    signature: str,
    token: str,
    *,
    wallet: str | None = None,
    conn: sqlite3.Connection | None = None,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> tuple[OnchainFill | None, Receipt]:
    """Read one fill from the chain. Solana only; EVM says so honestly."""
    if chain is not Chain.SOL:
        return None, _receipt(
            "eth.getTransactionReceipt",
            EvidenceBasis.UNAVAILABLE,
            "EVM fill reconciliation not implemented: needs receipt log decoding",
        )
    result, receipt = _rpc(
        chain,
        "getTransaction",
        [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": MAX_TX_VERSION,
                "commitment": "confirmed",
            },
        ],
        cache_key=f"tx:{chain.value}:{signature}",
        ttl_s=3600.0,  # a confirmed transaction does not change
        conn=conn,
        wait_for_slot_s=wait_for_slot_s,
    )
    if result is None:
        if receipt.basis is not EvidenceBasis.UNAVAILABLE:
            receipt = receipt.model_copy(
                update={"basis": EvidenceBasis.UNAVAILABLE, "note": "transaction not found (not yet confirmed, or dropped)"}
            )
        return None, receipt
    parsed = parse_solana_transaction(result, token=token, wallet=wallet, signature=signature)
    if parsed is None:
        return None, receipt.model_copy(
            update={"basis": EvidenceBasis.UNAVAILABLE, "note": f"no balance change in {token[:12]}.. for the wallet"}
        )
    return replace(parsed, receipt=receipt.model_copy(update={"basis": EvidenceBasis.VERIFIED_ONCHAIN})), receipt


def _bps(delta: int, claimed: int) -> int | None:
    if claimed == 0:
        return None
    with localcontext(_CTX):
        return int((Decimal(delta) * Decimal(10_000) / Decimal(claimed)).to_integral_value(ROUND_HALF_EVEN))


def reconcile_onchain(
    chain: Chain,
    signature: str,
    token: str,
    *,
    wallet: str | None = None,
    side: Side | None = None,
    claimed_native: Any = None,
    claimed_tokens: Any = None,
    order_id: str | None = None,
    conn: sqlite3.Connection | None = None,
    max_native_delta_bps: int = DEFAULT_MAX_NATIVE_DELTA_BPS,
    store: bool = True,
    wait_for_slot_s: float = DEFAULT_WAIT_FOR_SLOT_S,
) -> Reconciliation:
    """Compare a claimed fill with the chain's balance changes. Records; never overwrites.

    Tokens must match to the atom. The native leg is compared against two measures — see
    :attr:`OnchainFill.wallet_leg` and :attr:`OnchainFill.venue_leg` — and may differ by up
    to ``max_native_delta_bps`` from whichever of them is closest, because venue fees,
    tips and account rent land differently in each. ``native_basis`` on the result names
    the measure the verdict rests on, and the bps figure is stored either way so the band
    is auditable rather than assumed.

    The three verdicts mean exactly:

    ``agree``
        The token leg matched to the atom, and the native leg matched one of the two
        measures within the band — *or* there was no claimed native figure to compare
        against, in which case ``native_basis`` is ``none`` and ``detail`` says the native
        leg was recorded but not compared. Check ``native_basis`` before reading an
        ``agree`` as "both legs were verified".
    ``disagree``
        Something is genuinely inconsistent: a token quantity that differs at all, a
        native leg outside the band on every measure, a side the chain contradicts, or a
        transaction that failed on chain. This is loud — an error event plus a journal
        correction — and it changes no other row.
    ``unavailable``
        We could not look. A dead RPC, a dropped transaction, an unsupported chain. This
        must never be read as the venue having lied.
    """
    c = conn or get_conn()
    c_native = _int(claimed_native)
    c_tokens = _int(claimed_tokens)
    got, receipt = fetch_onchain_fill(chain, signature, token, wallet=wallet, conn=c, wait_for_slot_s=wait_for_slot_s)

    def _finish(rec: Reconciliation) -> Reconciliation:
        if store:
            record_reconciliation(c, rec)
        if rec.verdict == VERDICT_DISAGREE:
            emit(
                EventKind.SYSTEM,
                {"event": "fill_reconciliation_mismatch", **rec.as_dict(),
                 "impact": "the venue's reported fill and the chain's settled fill differ; "
                 "neither record was changed"},
                chain=chain, subject=token, level="error",
                dedupe_key=f"fills:mismatch:{chain.value}:{signature}", conn=c,
            )
            journal.append(
                "correction",
                f"fill {signature[:16]}.. for {token[:12]}.. disagrees with the chain: {rec.detail}. "
                "Order and position rows were left as reported; reconcile by hand.",
                subject=token, refs=[order_id or "", signature], conn=c,
            )
        return rec

    base = dict(order_id=order_id, chain=chain, signature=signature, wallet=wallet or "", token=token, side=side)
    if got is None:
        return _finish(Reconciliation(
            **base, slot=None, block_time_ms=None, claimed_native=c_native, claimed_tokens=c_tokens,
            onchain_native=None, onchain_native_all_in=None, onchain_tx_fee=None, onchain_tokens=None,
            delta_native=None, delta_tokens=None, delta_native_bps=None,
            verdict=VERDICT_UNAVAILABLE, detail=str(receipt.note or "chain read unavailable"), receipt=receipt,
        ))
    base["wallet"] = got.wallet

    if got.failed:
        return _finish(Reconciliation(
            **base, slot=got.slot, block_time_ms=got.block_time_ms, claimed_native=c_native,
            claimed_tokens=c_tokens, onchain_native=None, onchain_native_all_in=None,
            onchain_tx_fee=got.tx_fee, onchain_tokens=None, delta_native=None, delta_tokens=None,
            delta_native_bps=None, verdict=VERDICT_DISAGREE,
            detail="transaction failed on chain; nothing settled", receipt=got.receipt,
        ))

    inferred = Side.BUY if got.wallet_token_delta > 0 else Side.SELL
    notes: list[str] = []
    if side is not None and side is not inferred:
        notes.append(f"claimed side {side.value} but the wallet's token balance says {inferred.value}")
    on_tokens = abs(got.wallet_token_delta)
    on_all_in = abs(got.all_in_native)

    # Two measures, compared in order of how well they track what venues report. The
    # wallet leg is primary (13/13 on the live sample); the venue leg is corroboration and
    # is only meaningful when a counterparty was identified at all.
    candidates: list[tuple[str, int]] = [("wallet_leg", got.wallet_leg)]
    if got.venue is not None and got.venue_leg > 0:
        candidates.append(("venue_leg", got.venue_leg))

    matched: str | None = None
    on_native = got.wallet_leg
    d_native: int | None = None
    d_bps: int | None = None
    if not c_native:
        # No claimed native to compare against — a dust trade the endpoint reported as 0,
        # or a caller that only claimed a quantity. The chain figures are recorded, but
        # saying "chain X vs claimed 0" would read as a comparison that never happened.
        notes.append(
            f"venue reported no native amount; chain wallet_leg={got.wallet_leg} "
            f"venue_leg={got.venue_leg if got.venue else 'n/a'} recorded, not compared"
        )
    if c_native:
        scored = [(name, value, _bps(value - c_native, c_native) or 0) for name, value in candidates]
        inside = [s for s in scored if abs(s[2]) <= int(max_native_delta_bps)]
        best = min(inside or scored, key=lambda s: abs(s[2]))
        matched = best[0] if inside else None
        on_native, d_native, d_bps = best[1], best[1] - c_native, best[2]

    d_tokens = on_tokens - c_tokens if c_tokens is not None else None
    verdict = VERDICT_AGREE
    if d_tokens is not None and abs(d_tokens) > MAX_TOKEN_DELTA_ATOMS:
        verdict = VERDICT_DISAGREE
        notes.append(f"tokens: chain {on_tokens} vs claimed {c_tokens} ({d_tokens:+d} atoms)")
    if d_bps is not None and abs(d_bps) > int(max_native_delta_bps):
        verdict = VERDICT_DISAGREE
        notes.append(
            f"native: no measure matched - wallet_leg {got.wallet_leg}, venue_leg "
            f"{got.venue_leg if got.venue else 'n/a'} vs claimed {c_native} "
            f"(closest {d_bps:+d} bps, band {max_native_delta_bps})"
        )
    if side is not None and side is not inferred:
        verdict = VERDICT_DISAGREE
    if c_tokens is None and c_native is None:
        notes.append("nothing claimed; chain figures recorded only")
    if verdict == VERDICT_AGREE and not notes:
        notes.append(
            f"tokens match to the atom; native {on_native} vs claimed {c_native} via {matched}"
            + (f" ({d_bps:+d} bps)" if d_bps is not None else "")
        )
    return _finish(Reconciliation(
        **base, slot=got.slot, block_time_ms=got.block_time_ms, claimed_native=c_native,
        claimed_tokens=c_tokens, onchain_native=on_native, onchain_native_all_in=on_all_in,
        onchain_tx_fee=got.tx_fee, onchain_tokens=on_tokens, delta_native=d_native, delta_tokens=d_tokens,
        delta_native_bps=d_bps, verdict=verdict, detail="; ".join(notes)[:500], receipt=got.receipt,
        native_basis=matched or "none",
    ))


def reconcile_order(
    order: Order,
    conn: sqlite3.Connection | None = None,
    *,
    wallet: str | None = None,
    filled_in: Any = None,
    **kw: Any,
) -> Reconciliation:
    """Reconcile one order row against its own transaction.

    The claim is what the row says: on a buy, native spent (``filled_in`` when the provider
    reported one, else ``amount_in``) against ``filled_out`` tokens; on a sell, ``filled_out``
    native against the token quantity sold. Neither the order nor the position is touched.
    """
    c = conn or get_conn()
    if not order.tx_hash:
        rec = Reconciliation(
            order_id=order.order_id, chain=order.chain, signature="", wallet=wallet or "", token=order.token,
            side=order.side, slot=None, block_time_ms=None, claimed_native=None, claimed_tokens=None,
            onchain_native=None, onchain_native_all_in=None, onchain_tx_fee=None, onchain_tokens=None,
            delta_native=None, delta_tokens=None, delta_native_bps=None, verdict=VERDICT_UNAVAILABLE,
            detail="order has no transaction signature",
            receipt=_receipt("sol.getTransaction", EvidenceBasis.UNAVAILABLE, "no signature"),
        )
        return rec
    if order.side is Side.BUY:
        native, tokens = (_int(filled_in) or order.amount_in), order.filled_out
    else:
        native, tokens = order.filled_out, (_int(filled_in) or order.amount_in)
    return reconcile_onchain(
        order.chain, order.tx_hash, order.token, wallet=wallet, side=order.side, claimed_native=native,
        claimed_tokens=tokens, order_id=order.order_id, conn=c, **kw,
    )


def reconcile_swap_row(row: Mapping[str, Any], conn: sqlite3.Connection | None = None, **kw: Any) -> Reconciliation:
    """Reconcile one ``swaps`` row — a venue-reported trade — against the chain.

    This is how the pump.fun trade feed was checked against settlement: the row's
    ``amount_native`` and ``amount_token`` are the claim, the wallet is the row's trader.
    """
    side = Side(str(row["side"])) if row.get("side") in ("buy", "sell") else None
    return reconcile_onchain(
        Chain(str(row["chain"])), str(row["tx"]), str(row["token"]), wallet=str(row["wallet"]), side=side,
        claimed_native=row.get("amount_native"), claimed_tokens=row.get("amount_token"), conn=conn, **kw,
    )


def record_reconciliation(conn: sqlite3.Connection, rec: Reconciliation) -> None:
    """Append. A later check never replaces an earlier one; both stay readable."""
    conn.execute(
        "INSERT INTO fill_reconciliations (order_id, chain, signature, wallet, token, side, slot, "
        "block_time_ms, claimed_native, claimed_tokens, onchain_native, onchain_native_all_in, "
        "onchain_tx_fee, onchain_tokens, delta_native, delta_tokens, delta_native_bps, verdict, detail, "
        "receipt_json, checked_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec.order_id, rec.chain.value, rec.signature, rec.wallet, rec.token,
            rec.side.value if rec.side else None, rec.slot, rec.block_time_ms, _s(rec.claimed_native),
            _s(rec.claimed_tokens), _s(rec.onchain_native), _s(rec.onchain_native_all_in),
            _s(rec.onchain_tx_fee), _s(rec.onchain_tokens), _s(rec.delta_native), _s(rec.delta_tokens),
            rec.delta_native_bps, rec.verdict, rec.detail, rec.receipt.model_dump_json(), rec.checked_ms,
        ),
    )


# --------------------------------------------------------------------------------------
# backfill
# --------------------------------------------------------------------------------------


@dataclass
class BackfillReport:
    orders_seen: int = 0
    priced: int = 0
    native_only: int = 0
    unavailable: int = 0
    positions_updated: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


def _order_from_row(row: Mapping[str, Any]) -> Order:
    from kaiba.core.schemas import Lane, LaneMode

    return Order(
        order_id=row["order_id"], decision_id=row["decision_id"], chain=Chain(row["chain"]), token=row["token"],
        side=Side(row["side"]), lane=Lane(row["lane"]), mode=LaneMode(row["mode"]), input_token=row["input_token"],
        output_token=row["output_token"], amount_in=_int(row["amount_in"], 0) or 0, min_out=_int(row["min_out"], 0) or 0,
        slippage_bps=int(row["slippage_bps"] or 0), state=OrderState(row["state"]), provider=row["provider"],
        provider_order_id=row["provider_order_id"], tx_hash=row["tx_hash"], filled_out=_int(row["filled_out"]),
        fee_native=_int(row["fee_native"]), created_ms=int(row["created_ms"]), updated_ms=int(row["updated_ms"]),
        error=row["error"],
    )


def backfill(
    conn: sqlite3.Connection | None = None,
    *,
    apply: bool = False,
    tolerance_ms: int = native_price.DEFAULT_TOLERANCE_MS,
    fetch_decimals: bool = True,
) -> BackfillReport:
    """Recompute the fill price of every FILLED order from its own legs and record it.

    Records only, unless ``apply=True``: then an **open, live or canary** position whose
    buy fills all carry a measured USD price gets its ``entry_price_usd`` replaced by the
    quantity-weighted fill price, with the old and new values emitted. Shadow and paper
    positions are never rewritten — the paper broker converted with a placeholder native
    price by design, and a fill-ratio entry would make its record inconsistent with its
    own model rather than more honest. A stop that was already raised is left where it is:
    ``protection.arm`` never lowers one.
    """
    from kaiba.core.schemas import LaneMode

    c = conn or get_conn()
    report = BackfillReport()
    rows = fetch_all(c, "SELECT * FROM orders WHERE state=? ORDER BY created_ms", (OrderState.FILLED.value,))
    for row in rows:
        report.orders_seen += 1
        order = _order_from_row(row)
        if order.side is Side.BUY:
            native, tokens = order.amount_in, order.filled_out
        else:
            native, tokens = order.filled_out, order.amount_in
        fp = derive(
            order, native_atoms=native, token_atoms=tokens, conn=c, tolerance_ms=tolerance_ms,
            fetch_decimals=fetch_decimals, register_token=True,
        )
        record(c, fp)
        if fp.basis == BASIS_FILL_RATIO:
            report.priced += 1
        elif fp.basis == BASIS_FILL_RATIO_NATIVE_ONLY:
            report.native_only += 1
        else:
            report.unavailable += 1
        report.details.append({"order_id": order.order_id, "side": order.side.value, "basis": fp.basis,
                               "price_native_per_token": _s(fp.price_native_per_token),
                               "price_usd": _s(fp.price_usd), "notes": list(fp.notes)})

    if not apply:
        return report

    positions = fetch_all(
        c,
        "SELECT position_id, entry_price_usd, peak_price_usd, chain, token, mode FROM positions "
        "WHERE closed_ms IS NULL AND mode IN (?, ?)",
        (LaneMode.LIVE.value, LaneMode.CANARY.value),
    )
    for pos in positions:
        buys = fetch_all(
            c,
            "SELECT fp.* FROM fill_prices fp JOIN position_orders po ON po.order_id = fp.order_id "
            "WHERE po.position_id=? AND fp.side='buy'",
            (pos["position_id"],),
        )
        if not buys or any(b["basis"] != BASIS_FILL_RATIO or b["price_usd"] is None for b in buys):
            continue
        with localcontext(_CTX):
            qty_total = sum(_int(b["token_atoms"], 0) or 0 for b in buys)
            if qty_total <= 0:
                continue
            weighted = sum(
                (Decimal(str(b["price_usd"])) * Decimal(_int(b["token_atoms"], 0) or 0) for b in buys), Decimal(0)
            ) / Decimal(qty_total)
        old = _dec(pos["entry_price_usd"])
        if old is not None and old == weighted:
            continue
        peak = _dec(pos["peak_price_usd"])
        new_peak = max(peak, weighted) if peak is not None else weighted
        c.execute(
            "UPDATE positions SET entry_price_usd=?, peak_price_usd=? WHERE position_id=?",
            (_s(weighted), _s(new_peak), pos["position_id"]),
        )
        report.positions_updated += 1
        emit(
            EventKind.SYSTEM,
            {"event": "position_entry_backfilled", "position_id": pos["position_id"],
             "old_entry_price_usd": _s(old), "new_entry_price_usd": _s(weighted),
             "entry_price_basis": BASIS_FILL_RATIO, "fills": len(buys)},
            chain=pos["chain"], subject=pos["token"], conn=c,
        )
        try:
            from kaiba.execution.protection import arm

            arm(pos["position_id"], c)
        except Exception as exc:  # noqa: BLE001 - a backfill must never leave a position half-updated
            log.warning("could not re-arm %s after backfill: %s", pos["position_id"], exc)
    return report


__all__ = [
    "BASIS_FILL",
    "BASIS_FILL_RATIO",
    "BASIS_FILL_RATIO_NATIVE_ONLY",
    "BASIS_UNAVAILABLE",
    "DEFAULT_MAX_NATIVE_DELTA_BPS",
    "MAX_TX_VERSION",
    "DECIMALS_CALLER",
    "DECIMALS_TOKENS_ROW",
    "DECIMALS_UNAVAILABLE",
    "DECIMALS_VERIFIED",
    "STOP_BASES",
    "VERDICT_AGREE",
    "VERDICT_DISAGREE",
    "VERDICT_UNAVAILABLE",
    "BackfillReport",
    "FillPrice",
    "OnchainFill",
    "Reconciliation",
    "backfill",
    "derive",
    "fetch_onchain_fill",
    "load",
    "parse_evm_decimals",
    "parse_solana_mint_decimals",
    "parse_solana_transaction",
    "ratio",
    "reconcile_onchain",
    "reconcile_order",
    "reconcile_swap_row",
    "record",
    "record_reconciliation",
    "token_decimals",
]
