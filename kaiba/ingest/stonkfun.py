"""StonkFun launches and trades — the second venue, and what a second venue costs.

Until this module every number this system produced came from pump.fun. Tape coverage,
bundle share, creator graduation rate, the exclusion layer's quarantine rate, the
wallet-discovery base rate: one venue, one sample, and every one of them reported as
though it described Solana. On DefiLlama's 2026-09-20 reading StonkFun was 27.1% of
Solana launchpad curve volume and 25.0% of the fees. A second venue is the only way to
tell which of our findings are about memecoins and which are about pump.fun.

What StonkFun is, and how each claim was established
----------------------------------------------------

Everything marked **CHAIN** below was read off Solana mainnet on 2026-09-21. Everything
marked **API** was read from a provider response on the same day. Nothing here comes from
a documentation page; where a doc disagreed, the chain won and the disagreement is noted.

* **StonkFun is a frontend, not a program.** **CHAIN**: pool
  ``7fZCV17XxD3MTwmguASF8jzSKYeNkRXsnNoaDfp43xZQ`` is owned by
  ``LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj`` — Raydium LaunchLab — and its 429-byte
  ``PoolState`` carries ``platform_config = 6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt``.
  That pubkey is the venue attribution and it is on chain, which is why
  :func:`parse_launch` refuses any row whose platform config is not one of
  :data:`PLATFORM_CONFIGS`.

* **It is a bonding curve.** **CHAIN**: the pool account decodes to
  ``virtual_base``/``virtual_quote``/``real_base``/``real_quote`` — constant product over
  virtual reserves, the same shape pump.fun uses — plus ``total_base_sell`` and
  ``total_quote_fund_raising``. (The task brief flagged that two agents this week
  described a venue's curve from documentation and were wrong. This one was decoded from
  the account bytes; the struct is 429 bytes and the layout is confirmed by the size
  matching the IDL exactly and by ``base_mint`` decoding to the mint we asked about.)

* **The quote asset is usually not SOL.** **API**, over all 7,556 pools reachable from the
  two platform configs: 1,935 quote in STONK, 537 in ZEC, 518 in wrapped SOL, 472 in AVAX,
  and the tail runs to 501 distinct quote mints including tokenised equities at 8
  decimals. **Wrapped SOL is 6.9% of the venue.** This one fact drives most of the design
  below: ``swaps.amount_native`` is a lamports column and it is left NULL on 93% of this
  venue rather than filled with a number in the wrong unit.

* **The base mint is Token-2022 with a transfer fee.** **CHAIN**: mint
  ``EbRFZHChBh6xExbo4Ypedr4kxYruRxaWiXk2GEqhC8Qn`` is owned by ``TokenzQdBNbLq...``.
  **API**: 7,551 of 7,556 base mints are Token-2022 and 3,819 carry a 100-300 bps transfer
  fee. **CHAIN**, on a 100 bps pool: a sell of ``amountA = 2,758,013.726204`` debited the
  trader ``2,758,013,726,204`` atoms and credited the vault ``2,730,433,588,941`` — 99% of
  it. So the curve-side and wallet-side base amounts differ, and :func:`parse_trade`
  records the curve-side figure with :attr:`Launch.transfer_fee_bps` alongside it rather
  than inventing a net amount.

* **Graduation is a per-launch quote target, not a constant.** **CHAIN**: a finished pool
  reads ``status = 2`` with ``real_quote >= total_quote_fund_raising`` and
  ``real_base = total_base_sell``. **API**: the target spans 0.1044713 to 5,194,848,650
  quote units across the sample, a factor of 5e10. Exactly the pump.fun lesson — there is
  no venue constant to hard-code.

* **Fees.** **CHAIN**, from a buy of ``amountB = 140.239628094`` STONK: the platform wallet
  received ``1,402,396,281`` atoms, exactly 1.0000% of the trader's quote, and the vault
  received the remaining 99%. **API**: ``platformInfo.feeRate = 10000`` (1e6 denominator)
  and ``configInfo.tradeFeeRate = 2500``, so 1% leaves to the platform and 0.25% is
  retained in the vault as ``quote_protocol_fee`` — 1.25% total. A third-party doc claimed
  a flat 1%; the chain says 1.25% and the chain is what this docstring records.

* **Graduation goes to a Raydium CPMM pool** on the 0.25% tier. **API**:
  ``migrateType = "cpmm"`` on 7,556 of 7,556. **CHAIN**: the pool byte is
  ``migrate_type = 1``; the mapping from that byte to "cpmm" is the index's, not ours, so
  the string is stored as reported and the byte is not reinterpreted here.

The tape is **not** a hot window, and that is the headline
----------------------------------------------------------

pump.fun's ``/trades`` route serves only recently-active mints: a mint idle for an hour
returns 503 forever, so coverage is a going-forward property and a missed request is an
unrecoverable loss (see ``tape.HOT_WINDOW_ANSWERED_MAX_IDLE_MIN``). The obvious question
for a second venue is whether the same is true, because it decides whether this is a
backfill job or a capture-at-scan-time job.

**Measured 2026-09-21, 70 pools stratified by idle time, one request each:**

===============================  ====  ==========  ======================
last trade was                   n     HTTP 200    reached launch in 1 page
===============================  ====  ==========  ======================
0-52 min ago                     14    14          13
52 min - 5.8 h ago               14    14          13
5.8 h - 19.6 h ago               14    14          14
19.6 h - 6.7 d ago               14    14          13
6.7 d - 13.9 d ago               14    14          9
===============================  ====  ==========  ======================

**70 of 70 answered, zero 503s, including pools whose last trade was 20,077 minutes
earlier.** The eight that did not reach launch in one page all carried a pagination
cursor and were simply longer than 100 trades; one of them was walked to its end in nine
pages and reached its launch timestamp to the millisecond. Graduated pools answer too.

So this is a **backfill job**. :func:`run` can work through the whole known population at
whatever rate is polite, and a request we skip today is a request we can make tomorrow.
That is the opposite of the pump.fun collector's constraint and it is why
:attr:`StonkConfig.backoff_s` is gentle rather than aggressive.

And the tape is complete with respect to trades, which was checked rather than assumed
-----------------------------------------------------------------------------------------

"The walk terminated" proves we reached the beginning. It does not prove the route
reported every trade in between, and a spot-check against the chain said it might not:
on four pools our completed tapes held 10, 5, 20 and 3 rows against 29, 17, 27 and 14
*successful* transactions on the pool account. A 2-3x shortfall in something labelled
complete is exactly the kind of number that must not be waved through.

**It was chased down. All 53 of those extra transactions contain no LaunchLab instruction
at all.** Every one is a Solana version-1 transaction whose only top-level program is
``DhpyNWkdxFh3DRPsBrwRwrK3TYC5t7Q4arnSvf3t84HY`` — a bot that reads the pool account and
returns a value without swapping. It lands in ``getSignaturesForAddress`` because the pool
is in its account list, and it is not a trade. **LaunchLab buys or sells missing from
``/trade``: 0 of 53 candidates, across 4 pools.**

(The check was nearly abandoned as "unresolvable": those transactions are version 1, and
``getTransaction`` with the usual ``maxSupportedTransactionVersion: 0`` returns an error
rather than a result, so the first three attempts recorded them as RPC failures. The
answer was a parameter away from being a permanent open question.)

One limit on the word *complete*, stated because it is structural. ``/trade`` is keyed by
the **LaunchLab pool id**, and a graduated token's subsequent trading happens in a Raydium
CPMM pool, which is a different account. So a complete StonkFun tape is a complete
**bonding-curve** tape. Observed on six graduated pools: the newest row was 50-99 minutes
old on every one, i.e. the tape stops at graduation. This differs from pump.fun, whose
trade route covers the post-graduation venue too, and anything reasoning about a
graduated token's later flow needs a different source.

What is reused, and the one thing that is not
----------------------------------------------

The dangerous kind of duplication here is a second definition of the word *complete*, so
none of that is duplicated:

* :func:`collect_trades` returns ``token_flow.FlowResult`` and speaks ``collect_trades``'s
  exact reason vocabulary (``end_of_history``, ``reached_watermark``, ``unavailable``, ...),
* :func:`collect_token` classifies through ``tape._classify`` — the same pure function the
  pump.fun job and the tier-1 scanner both use,
* coverage is written with ``tape.store`` into the same ``token_tape`` table, and a failed
  observation goes through ``tape.record_failed_attempt`` so it can never overwrite a
  proof,
* :func:`run` reports a ``tape.RunReport``.

What is **not** reused is ``token_flow``'s writer, and for two reasons that are not style:
``TradeRow.as_params`` hard-codes ``source = 'pumpfun:trades'`` with no hook, and its
INSERT has no column for a quote leg that is not SOL. Writing StonkFun rows through it
would mislabel their provenance — the one thing ``token_tape``'s CHECK constraints exist
to prevent — and drop the quote amount entirely on 93% of the venue. So :func:`write_trades`
is its own function, over the same table plus the two columns migration 027 adds.

Missing is not zero
-------------------

``usd_value`` and ``price_usd`` are **always NULL** here. The trade route reports no USD
figure, and the quote asset is a token whose price we do not hold. Pricing a USDC-quoted
trade at par would be a peg assumption dressed as a measurement, and pricing the rest at
zero would be worse. ``amount_native`` is NULL unless the pool quotes in wrapped SOL.
A token with no ``token_tape`` row is ``UNAVAILABLE``, never "0 trades".

Pacing
------

``config/risk.yaml`` has no ``raydium_launchpad`` budget yet; see the block reported to
the operator. Measured 2026-09-21 by ramping a serial client against
``launch-history-v1.raydium.io/trade``: 301 requests at 1, 2, 4, 8 and 16 req/s targets
returned **301 x HTTP 200 and zero 429s**, with p50 187 ms. The 8 and 16 req/s phases were
capped by the client's own serialism at ~5 req/s, so 5 req/s is a measured clean floor and
not a measured ceiling. No ``x-ratelimit-*`` headers are served. The recommended budget is
deliberately well under that: there is no hot window, so slow costs nothing.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, upsert
from kaiba.core.limiter import Priority
from kaiba.core.schemas import Chain, EventKind, Receipt, now_ms
from kaiba.ingest import tape
from kaiba.ingest.token_flow import FlowResult
from kaiba.providers._http import request_json

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------------------

#: Limiter bucket. Both hosts below are Raydium launchpad infrastructure, so they share
#: one provider budget: a 429 from one is a signal about the other.
PROVIDER = "raydium_launchpad"

#: ``family.name``; the family before the dot is what a 429 cools down. The launch index
#: and the trade history are different hosts, so they are different families and a
#: cooldown on one does not blind us to the other.
LAUNCHES_ENDPOINT = "launch.list"
MINTS_ENDPOINT = "launch.mints"
TRADES_ENDPOINT = "trade.list"

LAUNCHES_URL = "https://launch-mint-v1.raydium.io/get/list"
MINTS_URL = "https://launch-mint-v1.raydium.io/get/by/mints"
TRADES_URL = "https://launch-history-v1.raydium.io/trade"

USER_AGENT = "Mozilla/5.0 (compatible; kaiba/0.1; +https://www.stonkfun.xyz)"

#: ``tokens.launchpad``. The venue the human means, not the program that ran it.
LAUNCHPAD = "stonkfun"

#: CHAIN-VERIFIED: the program that owns every StonkFun pool account.
LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"

#: CHAIN-VERIFIED: the two ``platform_config`` pubkeys that mark a LaunchLab pool as
#: StonkFun's. Read out of the pool account at byte offset 173 on a live pool and
#: cross-checked against the launch index's ``platformInfo.pubKey`` and its
#: ``platformInfo.name == "StonkFun"``. **This is the venue test.** Anything else on
#: LaunchLab is somebody else's launchpad and :func:`parse_launch` refuses it, because a
#: venue label we cannot check on chain is exactly the kind of attribution that quietly
#: contaminates a second sample with a third venue.
PLATFORM_CONFIGS: dict[str, str] = {
    "6BwHHDg3u1854jC8PDLXvR4spTcLNaoBxLJNGC4nTESt": "reward",
    "4E876qZTE9FJMrBzgVtBrSrzz2TLivB5Y5QXPjB4gZL7": "standard",
}

#: ``swaps.source`` and ``token_tape.route``. One string for both, exactly as
#: ``token_flow.SOURCE`` and ``tape.ROUTE_TRADES`` are one string: the route is how the
#: rows were obtained, and there is one way to obtain these.
SOURCE = "raydium:launchlab"
ROUTE = SOURCE

#: The quote mints for which ``swaps.amount_native`` may be written in lamports. Mirrors
#: ``token_flow.SOL_QUOTE_MINTS``; anything else is a different unit and the column stays
#: NULL. 518 of 7,556 sampled pools quote in wrapped SOL.
SOL_QUOTE_MINTS: frozenset[str] = frozenset(
    {
        "11111111111111111111111111111111",
        "So11111111111111111111111111111111111111112",
    }
)
SOL_DECIMALS = 9

#: ``program`` on a swap row: what executed the trade. The venue name, not a program id,
#: matching ``token_flow``'s treatment of pump.fun's ``venue`` field.
VENUE = "launchlab"

#: Both StonkFun platform configs sell this many base units on the curve — 793,100,000 of
#: a 1,000,000,000 supply, on 7,556 of 7,556 pools sampled. Recorded as an **observation**,
#: not used as an input to anything: pump.fun looked constant too, and then its graduation
#: targets turned out to span 0.41 to 115 SOL.
OBSERVED_TOTAL_BASE_SELL = 793_100_000_000_000

#: Observed base decimals, 7,556 of 7,556. Also recorded rather than assumed — the launch
#: index reports ``decimals`` per mint and that is what is stored.
OBSERVED_BASE_DECIMALS = 6

#: What would actually populate ``swaps.fee_payer``, stated as data so a report can quote
#: it. Same shape as ``tape.FEE_PAYER_NOTE`` and the same reasoning, re-measured here
#: rather than assumed to carry over.
FEE_PAYER_NOTE = (
    "The LaunchLab trade record reports `owner` and no signer. CHAIN-VERIFIED on four "
    "trades: `owner` is the owner of the token accounts that moved (4 of 4), and it is "
    "NOT always the fee payer (3 of 4 matched, 1 did not). Deriving fee_payer from owner "
    "would therefore be right on the easy cases and wrong on exactly the bundled case "
    "entity resolution exists to detect. One Solana RPC getTransaction per DISTINCT "
    "signature resolves it exactly: the fee payer is account index 0 of the message."
)


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StonkConfig:
    """Every number is annotated with where it came from."""

    #: MEASURED: ``limit=101`` answers ``{"success": false, "msg": "limit max 100"}``.
    trade_page_limit: int = 100

    #: MEASURED: the launch index served 100 rows per page and paged back 19 days.
    launch_page_size: int = 100

    #: Launch-index pages one :func:`ingest_launches` call may fetch **per platform
    #: config**. One page is ~17 minutes of the busier config's launches at the observed
    #: 5.7/min, so a poller on a 60 s clock never needs a second page and the budget only
    #: bites on a deliberate backfill.
    launch_pages: int = 1

    #: Seconds between polls in :func:`watch`. The observed launch rate across both
    #: configs is ~7/min and one page holds 100, so a 30 s clock has ~28x headroom.
    poll_interval_s: float = 30.0

    #: Trade pages one full walk may spend. MEASURED: the busiest non-graduated pool in a
    #: 7,556-pool sample needed 9 pages; 24 covers one an order of magnitude busier.
    #: Above this the token is recorded ``partial`` and finished on a later pass, which
    #: costs nothing here because there is no hot window.
    walk_pages: int = 24

    #: Pages a top-up of an already-complete tape may spend before the unreached watermark
    #: has to be called a gap. Same rule and the same number as ``tape.TapeConfig``.
    topup_pages: int = 3

    #: Wall-clock budget for one :func:`run`. Resumable, so short and often beats long and
    #: rarely.
    budget_s: float = 600.0
    max_tokens: int = 500

    #: Required by docs/CONTRACT.md: a walk makes many calls per logical operation, so the
    #: non-waiting default would silently drop every page after the first.
    wait_for_slot_s: float = 20.0
    timeout_s: float = 20.0
    #: MEASURED: 301 consecutive requests returned 200. Retries exist for transport
    #: failures, not for a route that refuses.
    retries: int = 3
    #: Trade history is append-only but the newest page is not the same page a second
    #: later. No caching on the tape.
    ttl_s: float = 0.0
    #: The launch index is a list of things that already happened; a few seconds of reuse
    #: across the scanner and this poller is free and polite.
    launch_ttl_s: float = 5.0

    #: Backoff after an ``unavailable`` attempt, by attempt count; the last entry repeats.
    #: Deliberately gentler than ``tape.TapeConfig.backoff_s`` and for a measured reason:
    #: pump.fun's aggressive backoff exists because a mint that aged out of the hot window
    #: will never answer again, so re-asking is pure noise. Here 70 of 70 pools answered
    #: regardless of idleness, so an ``unavailable`` is a transport problem and worth
    #: retrying on a human timescale.
    backoff_s: tuple[int, ...] = (60, 300, 1_800, 7_200)
    max_attempts: int = 6
    #: A partial tape is not urgent here — there is no window to beat — but it is cheap to
    #: finish, so it is retried before a refusal is.
    partial_retry_s: int = 120

    #: MEASURED: ``get/by/mints?ids=`` served 60 ids in one response. 50 leaves headroom.
    mints_batch: int = 50

    def backoff_for(self, attempts: int) -> int:
        idx = max(0, min(attempts, len(self.backoff_s)) - 1)
        return self.backoff_s[idx]


DEFAULT_CONFIG = StonkConfig()


# --------------------------------------------------------------------------------------
# parsed shapes
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Launch:
    """One StonkFun launch: the token, its pool, and the units its trades are denominated in.

    Every field is reported by the launch index. Nothing is derived and nothing is
    defaulted — a launch whose decimals or quote mint could not be read is not a
    :class:`Launch`, because a trade parsed against a guessed exponent is a silent
    factor-of-a-thousand error that looks entirely plausible.
    """

    chain: Chain
    token: str
    pool: str
    platform_config: str
    creator: str | None
    created_ms: int | None
    symbol: str | None
    name: str | None
    base_decimals: int
    quote_mint: str
    quote_symbol: str | None
    quote_decimals: int
    transfer_fee_bps: int | None = None
    config_id: str | None = None
    total_base_sell: int | None = None
    graduation_quote: int | None = None
    migrate_type: str | None = None
    progress_pct: Decimal | None = None
    image_url: str | None = None
    metadata_url: str | None = None

    @property
    def platform_kind(self) -> str:
        return PLATFORM_CONFIGS.get(self.platform_config, "unknown")

    @property
    def quote_is_sol(self) -> bool:
        return self.quote_mint in SOL_QUOTE_MINTS

    @property
    def graduated(self) -> bool:
        """Has the curve finished? ``False`` also covers "we do not know yet"."""
        return self.progress_pct is not None and self.progress_pct >= Decimal(100)

    def as_meta(self) -> dict[str, Any]:
        """``tokens.meta_json`` payload. Strings and ints only, so it round-trips."""
        meta: dict[str, Any] = {
            "venue": LAUNCHPAD,
            "program": LAUNCHLAB_PROGRAM,
            "pool": self.pool,
            "platform_config": self.platform_config,
            "platform_kind": self.platform_kind,
            "config_id": self.config_id,
            "quote_mint": self.quote_mint,
            "quote_symbol": self.quote_symbol,
            "quote_decimals": self.quote_decimals,
            "transfer_fee_bps": self.transfer_fee_bps,
            "total_base_sell": None if self.total_base_sell is None else str(self.total_base_sell),
            "graduation_quote": None if self.graduation_quote is None else str(self.graduation_quote),
            "migrate_type": self.migrate_type,
            "progress_pct": None if self.progress_pct is None else str(self.progress_pct),
            "image_url": self.image_url,
            "metadata_url": self.metadata_url,
            "source": SOURCE,
        }
        return {k: v for k, v in meta.items() if v is not None}


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One row destined for ``swaps``, in the column contract of ``001_core.sql`` plus the
    two columns migration 027 adds.

    Three fields are deliberately absent rather than guessed.

    ``slot`` and ``block_index``: the trade route reports ``blockTime`` in whole seconds
    and nothing else about position. Inventing an ordering key would make two trades in
    the same second look ordered when they are not.

    ``fee_payer``: see :data:`FEE_PAYER_NOTE`. ``owner`` is the token-account owner, which
    is the trader; it is not always the signer, and assuming it is would manufacture the
    very clusters entity resolution is meant to discover.
    """

    chain: Chain
    tx: str
    ts_ms: int
    wallet: str
    token: str
    side: str
    #: Base units of the token, **curve-side and gross of the Token-2022 transfer fee**.
    #: CHAIN-VERIFIED on a 100 bps pool: on a sell this is what left the trader and the
    #: vault received 99% of it; on a buy this is what left the vault and the trader
    #: received 99% of it. The net figure is not stored because the fee can be capped
    #: (``maximumFee``) and a computed net would be an inference wearing a measurement's
    #: clothes. :attr:`Launch.transfer_fee_bps` is stored so a consumer can do it properly.
    amount_token: int | None
    #: Base units of :attr:`quote_mint`, **trader-side**. CHAIN-VERIFIED: on a buy this is
    #: what the trader paid including the 1.25% venue fee; on a sell it is what the trader
    #: received net of it.
    amount_quote: int | None
    quote_mint: str
    #: Lamports, and **only** when :attr:`quote_mint` is wrapped SOL. NULL otherwise —
    #: never 0. 93% of this venue does not trade against SOL.
    amount_native: int | None

    def as_params(self) -> tuple[Any, ...]:
        return (
            self.chain.value,
            self.tx,
            None,  # slot
            None,  # block_index
            self.ts_ms,
            self.wallet,
            self.token,
            self.side,
            None if self.amount_token is None else str(self.amount_token),
            None if self.amount_native is None else str(self.amount_native),
            None,  # price_usd: the route reports none and the quote is not USD
            None,  # usd_value: ditto. A peg assumption is not a measurement.
            VENUE,
            SOURCE,
            0,  # is_create_tx; set later from a proved tape, never inferred here
            None,  # fee_payer
            None if self.amount_quote is None else str(self.amount_quote),
            self.quote_mint,
        )


@dataclass(slots=True)
class LaunchReport:
    """What one :func:`ingest_launches` pass did."""

    pages: int = 0
    rows_seen: int = 0
    rows_rejected: int = 0
    tokens_new: int = 0
    tokens_written: int = 0
    oldest_ms: int | None = None
    newest_ms: int | None = None
    elapsed_s: float = 0.0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def launches_per_min(self) -> float:
        """Launch rate implied by the createAt span this pass actually covered."""
        if self.oldest_ms is None or self.newest_ms is None or self.newest_ms <= self.oldest_ms:
            return 0.0
        return self.rows_seen / ((self.newest_ms - self.oldest_ms) / 60_000)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "rows_seen": self.rows_seen,
            "rows_rejected": self.rows_rejected,
            "tokens_new": self.tokens_new,
            "tokens_written": self.tokens_written,
            "oldest_ms": self.oldest_ms,
            "newest_ms": self.newest_ms,
            "launches_per_min": round(self.launches_per_min, 2),
            "elapsed_s": round(self.elapsed_s, 2),
            "reasons": dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])),
        }


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


def _atoms(value: Any, decimals: int) -> int | None:
    """A decimal amount from the API to exact base units, or ``None`` if it is not exact.

    The trade route reports amounts already scaled by the mint's decimals
    (``33722134.792695`` for a 6-decimal token). ``_http`` parses provider JSON with
    ``parse_float=Decimal``, so the digits arrive intact and multiplying by ``10**decimals``
    is exact — **as long as the result is a whole number**. If it is not, our idea of the
    mint's decimals is wrong, and truncating would produce a plausible-looking wrong
    amount rather than an error. Measured over 1,189 real rows from 40 pools: 1,189 of
    1,189 were exact at the decimals the launch index reports, and none carried more than
    15 significant digits, so nothing was lost before we saw it either.
    """
    amount = _dec(value)
    if amount is None or decimals < 0:
        return None
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _payload(data: Any) -> dict[str, Any] | None:
    """Unwrap Raydium's ``{"success": bool, "data": {...}, "msg": str}`` envelope.

    A ``success: false`` body arrives with HTTP 400 and ``_http`` has already turned it
    into an UNAVAILABLE receipt, so this only guards the case where the envelope shape
    changes underneath us.
    """
    if not isinstance(data, Mapping) or not data.get("success"):
        return None
    inner = data.get("data")
    return dict(inner) if isinstance(inner, Mapping) else None


# --------------------------------------------------------------------------------------
# the launch index
# --------------------------------------------------------------------------------------


def fetch_launch_page(
    platform_config: str,
    *,
    page_id: str | None = None,
    sort: str = "new",
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """One page of the launch index for one platform config, newest first. Never raises.

    ``page_id`` is the ``nextPageId`` from a previous page. MEASURED: of the eight cursor
    parameter names tried, only ``nextPageId`` advances the page; the other seven are
    accepted and silently ignored, which would have looked like a working backfill that
    re-read page one forever.
    """
    params: dict[str, Any] = {
        "platformId": platform_config,
        "sort": sort,
        "size": max(1, min(int(config.launch_page_size), 100)),
        "includeNsfw": "true",
    }
    if page_id:
        params["nextPageId"] = page_id
    got = request_json(
        PROVIDER,
        LAUNCHES_ENDPOINT,
        LAUNCHES_URL,
        params=params,
        headers={"user-agent": USER_AGENT, "accept": "application/json"},
        ttl_s=config.launch_ttl_s,
        priority=priority,
        wait_for_slot_s=config.wait_for_slot_s,
        timeout_s=config.timeout_s,
        retries=config.retries,
        conn=conn,
    )
    if not got.ok:
        return None, got.receipt
    return _payload(got.data), got.receipt


def fetch_launches_by_mints(
    mints: Sequence[str],
    *,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.RESEARCH,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """Look up specific mints in the launch index. MEASURED: 60 ids in one call worked.

    A mint that is not a LaunchLab launch comes back as an empty row set rather than an
    error, so an absent row means "not this venue", not "lookup failed".
    """
    ids = ",".join(m for m in mints if m)
    got = request_json(
        PROVIDER,
        MINTS_ENDPOINT,
        MINTS_URL,
        params={"ids": ids},
        headers={"user-agent": USER_AGENT, "accept": "application/json"},
        ttl_s=config.launch_ttl_s,
        priority=priority,
        wait_for_slot_s=config.wait_for_slot_s,
        timeout_s=config.timeout_s,
        retries=config.retries,
        conn=conn,
    )
    if not got.ok:
        return None, got.receipt
    return _payload(got.data), got.receipt


def parse_launch(raw: Mapping[str, Any], *, chain: Chain = Chain.SOL) -> Launch | None:
    """One launch-index row to a :class:`Launch`, or ``None`` when it is not usable.

    Pure, so every rejection is testable without a network.

    The first test is the venue test and it is not negotiable: a row whose
    ``platformInfo.pubKey`` is not one of :data:`PLATFORM_CONFIGS` is somebody else's
    launchpad on the same program. The whole point of this module is a *second* sample; a
    third venue leaking into it silently would defeat that more thoroughly than having no
    second venue at all.
    """
    platform = raw.get("platformInfo")
    platform_config = _text(platform.get("pubKey")) if isinstance(platform, Mapping) else None
    if platform_config not in PLATFORM_CONFIGS:
        return None

    token = _text(raw.get("mint"))
    pool = _text(raw.get("poolId"))
    if not token or not pool:
        return None

    base_decimals = _int(raw.get("decimals"))
    if base_decimals is None or not 0 <= base_decimals <= 18:
        return None

    quote = raw.get("mintB")
    quote_mint = _text(quote.get("address")) if isinstance(quote, Mapping) else None
    quote_decimals = _int(quote.get("decimals")) if isinstance(quote, Mapping) else None
    if not quote_mint or quote_decimals is None or not 0 <= quote_decimals <= 18:
        # Without the quote exponent every quote amount on this pool would be wrong by a
        # power of ten, and 501 distinct quote mints means there is no sane default.
        return None

    conf = raw.get("configInfo")
    progress = _dec(raw.get("finishingRate"))
    return Launch(
        chain=chain,
        token=token,
        pool=pool,
        platform_config=platform_config,
        creator=_text(raw.get("creator")),
        created_ms=_int(raw.get("createAt")),
        symbol=_text(raw.get("symbol")),
        name=_text(raw.get("name")),
        base_decimals=base_decimals,
        quote_mint=quote_mint,
        quote_symbol=_text(quote.get("symbol")) if isinstance(quote, Mapping) else None,
        quote_decimals=quote_decimals,
        transfer_fee_bps=_int(raw.get("transferFeeBasePoints")),
        config_id=_text(raw.get("configId")) or (
            _text(conf.get("pubKey")) if isinstance(conf, Mapping) else None
        ),
        total_base_sell=_int(raw.get("totalSellA")),
        graduation_quote=_int(raw.get("totalFundRaisingB")),
        migrate_type=_text(raw.get("migrateType")),
        progress_pct=progress,
        image_url=_text(raw.get("imgUrl")),
        metadata_url=_text(raw.get("metadataUrl")),
    )


def record_launch(
    launch: Launch, conn: sqlite3.Connection | None = None, *, at_ms: int | None = None
) -> bool:
    """Upsert ``tokens`` + ``stonkfun_launches`` and emit ``TOKEN_CREATED`` once.

    Returns whether this was the first time we had seen the mint. ``tokens.migrated_ms``
    is deliberately never stamped here: see the note in migration 027.
    """
    c = _conn(conn)
    ts = at_ms if at_ms is not None else now_ms()
    try:
        upsert(
            c,
            "tokens",
            {
                "chain": launch.chain.value,
                "address": launch.token,
                "symbol": launch.symbol,
                "name": launch.name,
                "decimals": launch.base_decimals,
                "creator": launch.creator,
                "created_ms": launch.created_ms,
                "launchpad": LAUNCHPAD,
                "pool": launch.pool,
                "first_seen_ms": ts,
                "meta_json": jdump(launch.as_meta()),
            },
            conflict=["chain", "address"],
            # first_seen_ms and migrated_ms are never updated: the first sighting is the
            # number the latency work cares about, and migrated_ms is not ours to set.
            update=["symbol", "name", "decimals", "creator", "created_ms", "launchpad",
                    "pool", "meta_json"],
        )
        graduated_ms = ts if launch.graduated else None
        prior = fetch_one(
            c,
            "SELECT graduated_seen_ms, first_seen_ms FROM stonkfun_launches WHERE chain=? AND token=?",
            (launch.chain.value, launch.token),
        )
        if prior is not None and _int(prior["graduated_seen_ms"]) is not None:
            # First sighting of a finished curve is the one worth keeping.
            graduated_ms = _int(prior["graduated_seen_ms"])
        upsert(
            c,
            "stonkfun_launches",
            {
                "chain": launch.chain.value,
                "token": launch.token,
                "pool": launch.pool,
                "platform_config": launch.platform_config,
                "config_id": launch.config_id,
                "base_decimals": launch.base_decimals,
                "quote_mint": launch.quote_mint,
                "quote_symbol": launch.quote_symbol,
                "quote_decimals": launch.quote_decimals,
                "transfer_fee_bps": launch.transfer_fee_bps,
                "total_base_sell": None if launch.total_base_sell is None else str(launch.total_base_sell),
                "graduation_quote": (
                    None if launch.graduation_quote is None else str(launch.graduation_quote)
                ),
                "migrate_type": launch.migrate_type,
                "is_reward_launch": 1 if launch.platform_kind == "reward" else 0,
                "progress_pct": None if launch.progress_pct is None else str(launch.progress_pct),
                "graduated_seen_ms": graduated_ms,
                "created_ms": launch.created_ms,
                "observed_ms": ts,
                "first_seen_ms": _int(prior["first_seen_ms"]) if prior else ts,
                "source": SOURCE,
            },
            conflict=["chain", "token"],
            update=["pool", "platform_config", "config_id", "base_decimals", "quote_mint",
                    "quote_symbol", "quote_decimals", "transfer_fee_bps", "total_base_sell",
                    "graduation_quote", "migrate_type", "is_reward_launch", "progress_pct",
                    "graduated_seen_ms", "created_ms", "observed_ms", "source"],
        )
    except sqlite3.Error as exc:
        log.warning("stonkfun: launch write failed for %s (%s)", launch.token[:12], exc)
        return False

    return _emit_created(launch, c)


def _emit_created(launch: Launch, conn: sqlite3.Connection) -> bool:
    """Telemetry must never break an ingest pass."""
    try:
        from kaiba.core.events import emit_once

        event_id = emit_once(
            EventKind.TOKEN_CREATED,
            {
                "mint": launch.token,
                "symbol": launch.symbol,
                "name": launch.name,
                "creator": launch.creator,
                "launchpad": LAUNCHPAD,
                "pool": launch.pool,
                "quote_mint": launch.quote_mint,
                "quote_symbol": launch.quote_symbol,
                "created_ms": launch.created_ms,
                "source": SOURCE,
            },
            chain=launch.chain,
            subject=launch.token,
            dedupe_key=f"{EventKind.TOKEN_CREATED.value}:{launch.chain.value}:{launch.token}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry is not load-bearing
        log.debug("stonkfun: could not emit token.created for %s (%s)", launch.token[:12], exc)
        return False
    return event_id is not None


def launch_of(
    chain: Chain, token: str, conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    """The stored ``stonkfun_launches`` row, or ``None`` if this is not a StonkFun mint."""
    try:
        return fetch_one(
            _conn(conn),
            "SELECT * FROM stonkfun_launches WHERE chain=? AND token=?",
            (chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("stonkfun: launch read failed for %s (%s)", token[:12], exc)
        return None


def ingest_launches(
    conn: sqlite3.Connection | None = None,
    *,
    chain: Chain = Chain.SOL,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
    pages: int | None = None,
    platform_configs: Iterable[str] | None = None,
    at_ms: int | None = None,
) -> LaunchReport:
    """Poll the launch index and record what is new. Never raises.

    Both platform configs are walked because they are both StonkFun and they launch at
    very different rates: MEASURED 5.7 launches/min on ``reward`` against 1.2/min on
    ``standard``. Polling only the busy one would silently drop a sixth of the venue.
    """
    c = _conn(conn)
    report = LaunchReport()
    started = time.perf_counter()
    budget = max(1, int(pages if pages is not None else config.launch_pages))
    wanted = list(platform_configs) if platform_configs is not None else list(PLATFORM_CONFIGS)

    for platform in wanted:
        page_id: str | None = None
        for _ in range(budget):
            payload, _receipt = fetch_launch_page(
                platform, page_id=page_id, config=config, priority=priority, conn=c
            )
            if payload is None:
                report.note(f"unavailable:{PLATFORM_CONFIGS.get(platform, platform[:8])}")
                break
            report.pages += 1
            rows = payload.get("rows")
            if not isinstance(rows, list):
                report.note("malformed_page")
                break
            if not rows:
                report.note("end_of_index")
                break
            for raw in rows:
                if not isinstance(raw, Mapping):
                    report.rows_rejected += 1
                    continue
                launch = parse_launch(raw, chain=chain)
                if launch is None:
                    report.rows_rejected += 1
                    continue
                report.rows_seen += 1
                if launch.created_ms is not None:
                    report.oldest_ms = (
                        launch.created_ms if report.oldest_ms is None
                        else min(report.oldest_ms, launch.created_ms)
                    )
                    report.newest_ms = (
                        launch.created_ms if report.newest_ms is None
                        else max(report.newest_ms, launch.created_ms)
                    )
                if record_launch(launch, c, at_ms=at_ms):
                    report.tokens_new += 1
                report.tokens_written += 1
            page_id = _text(payload.get("nextPageId"))
            if not page_id:
                report.note("end_of_index")
                break

    report.elapsed_s = time.perf_counter() - started
    return report


# --------------------------------------------------------------------------------------
# the trade tape
# --------------------------------------------------------------------------------------


def fetch_trades_page(
    pool: str,
    *,
    page_key: str | None = None,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any] | None, Receipt]:
    """One page of ``/trade?poolId=...``, newest first. Never raises.

    ``page_key`` is the ``nextPageKey`` from a previous page and is **exclusive** —
    MEASURED by walking a 839-trade pool across nine pages and finding 839 unique txids in
    839 rows. The absence of ``nextPageKey`` on a page is the end of history, and it is
    load-bearing: the final page of that walk held 39 rows and no key, and its oldest
    trade matched the pool's creation timestamp to the millisecond.
    """
    params: dict[str, Any] = {
        "poolId": pool,
        "limit": max(1, min(int(config.trade_page_limit), 100)),
    }
    if page_key:
        params["nextPageKey"] = page_key
    got = request_json(
        PROVIDER,
        TRADES_ENDPOINT,
        TRADES_URL,
        params=params,
        headers={"user-agent": USER_AGENT, "accept": "application/json"},
        ttl_s=config.ttl_s,
        priority=priority,
        wait_for_slot_s=config.wait_for_slot_s,
        timeout_s=config.timeout_s,
        retries=config.retries,
        conn=conn,
    )
    if not got.ok:
        return None, got.receipt
    return _payload(got.data), got.receipt


def parse_trade(raw: Mapping[str, Any], launch: Launch) -> TradeRow | None:
    """One trade record to a ``swaps`` row, or ``None`` when it cannot be trusted. Pure.

    The :class:`Launch` is required rather than optional because both amounts are
    meaningless without it: ``amountA`` needs the base exponent and ``amountB`` needs the
    quote exponent, and on this venue the quote exponent ranges over 4, 5, 6, 8, 9, 11 and
    12 across 501 distinct quote mints.
    """
    tx = _text(raw.get("txid"))
    wallet = _text(raw.get("owner"))
    side = str(raw.get("side") or "").lower()
    block_time = _int(raw.get("blockTime"))
    if not tx or not wallet or side not in {"buy", "sell"} or block_time is None:
        return None
    # A pool id on the row that is not this pool means we have mismatched a tape to a
    # token, which corrupts an entire mint rather than one row of it.
    row_pool = _text(raw.get("poolId"))
    if row_pool is not None and row_pool != launch.pool:
        return None

    amount_token = _atoms(raw.get("amountA"), launch.base_decimals)
    amount_quote = _atoms(raw.get("amountB"), launch.quote_decimals)

    amount_native: int | None = None
    if launch.quote_is_sol and amount_quote is not None:
        # Wrapped SOL is 9 decimals, so quote base units already are lamports; the guard
        # is here so a hypothetical non-9-decimal SOL wrapper cannot slip through.
        amount_native = amount_quote if launch.quote_decimals == SOL_DECIMALS else None

    return TradeRow(
        chain=launch.chain,
        tx=tx,
        ts_ms=block_time * 1000,
        wallet=wallet,
        token=launch.token,
        side=side,
        amount_token=amount_token,
        amount_quote=amount_quote,
        quote_mint=launch.quote_mint,
        amount_native=amount_native,
    )


_SWAP_INSERT = (
    "INSERT OR IGNORE INTO swaps "
    "(chain, tx, slot, block_index, ts_ms, wallet, token, side, amount_token, amount_native, "
    " price_usd, usd_value, program, source, is_create_tx, fee_payer, amount_quote, quote_mint) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def write_trades(conn: sqlite3.Connection, rows: Sequence[TradeRow]) -> tuple[int, int]:
    """``(written, duplicate)``. Idempotent on the transaction signature.

    Same dedupe strategy as ``token_flow.write_trades`` and for the same reason: the
    table's UNIQUE is ``(chain, tx, wallet, token, side, amount_token)``, SQLite treats
    NULLs as distinct, and a row whose atoms could not be resolved would therefore insert
    a fresh duplicate on every re-run. Signatures already present for this mint are
    filtered first, which also means a row another collector wrote is never counted twice.
    """
    if not rows:
        return 0, 0
    known: set[str] = set()
    by_token: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        by_token.setdefault((row.chain.value, row.token), []).append(row.tx)
    for (chain_value, token), signatures in by_token.items():
        for start in range(0, len(signatures), 400):
            chunk = signatures[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            try:
                found = fetch_all(
                    conn,
                    f"SELECT DISTINCT tx FROM swaps WHERE chain=? AND token=? AND tx IN ({placeholders})",
                    (chain_value, token, *chunk),
                )
            except sqlite3.Error as exc:
                log.warning("stonkfun: duplicate probe failed (%s)", exc)
                found = []
            known.update(str(r["tx"]) for r in found)

    written = 0
    for row in rows:
        if row.tx in known:
            continue
        try:
            written += conn.execute(_SWAP_INSERT, row.as_params()).rowcount or 0
        except sqlite3.Error as exc:
            log.warning("stonkfun: swap insert failed for %s (%s)", row.tx[:12], exc)
    return written, len(rows) - written


def collect_trades(
    launch: Launch,
    conn: sqlite3.Connection | None = None,
    *,
    since_ms: int | None = None,
    max_pages: int | None = None,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.DISCOVERY,
) -> FlowResult:
    """Page this pool's trades backwards to ``since_ms`` and write them to ``swaps``.

    Returns ``token_flow.FlowResult`` and speaks its exact reason vocabulary, so
    ``tape._classify`` can grade the outcome without knowing which venue produced it.
    That is the whole point: "complete" must mean the same thing on both launchpads.

    Stops on the first of: a page shorter than the limit or carrying no cursor (end of
    history), a page whose oldest trade is at or before ``since_ms``, or the page budget.
    """
    c = _conn(conn)
    budget = max(1, int(max_pages if max_pages is not None else config.walk_pages))
    limit = max(1, min(int(config.trade_page_limit), 100))

    receipts: list[Receipt] = []
    rows: list[TradeRow] = []
    seen: set[str] = set()
    page_key: str | None = None
    pages = 0
    oldest: int | None = None
    newest: int | None = None
    end_of_history = False
    reached_since = False
    reason = "page_budget_exhausted"

    while pages < budget:
        payload, receipt = fetch_trades_page(
            launch.pool, page_key=page_key, config=config, priority=priority, conn=c
        )
        receipts.append(receipt)
        if payload is None:
            reason = "unavailable" if pages == 0 else "partial_provider_error"
            break
        pages += 1
        page = payload.get("rows")
        if not isinstance(page, list):
            reason = "malformed_page"
            break
        if not page:
            end_of_history = True
            reason = "end_of_history"
            break

        page_oldest: int | None = None
        for item in page:
            if not isinstance(item, Mapping):
                continue
            row = parse_trade(item, launch)
            if row is None or row.tx in seen:
                continue
            seen.add(row.tx)
            rows.append(row)
            page_oldest = row.ts_ms if page_oldest is None else min(page_oldest, row.ts_ms)
            oldest = row.ts_ms if oldest is None else min(oldest, row.ts_ms)
            newest = row.ts_ms if newest is None else max(newest, row.ts_ms)

        next_key = _text(payload.get("nextPageKey"))
        if len(page) < limit or not next_key:
            # Both are end-of-history signals and both are needed. MEASURED: the last page
            # of a nine-page walk held 39 rows AND no cursor. A pool whose trade count is
            # an exact multiple of 100 would produce a full page with no cursor, and
            # treating only the short page as terminal would leave it forever `partial`.
            end_of_history = True
            reason = "end_of_history"
            break
        if since_ms is not None and page_oldest is not None and page_oldest <= since_ms:
            reached_since = True
            reason = "reached_watermark"
            break
        page_key = next_key

    written, duplicate = write_trades(c, rows)

    if end_of_history:
        coverage_from = launch.created_ms if launch.created_ms is not None else oldest
        if coverage_from is not None and oldest is not None:
            coverage_from = min(coverage_from, oldest)
    else:
        coverage_from = oldest

    return FlowResult(
        chain=launch.chain,
        token=launch.token,
        trades_seen=len(rows),
        rows_written=written,
        rows_duplicate=duplicate,
        pages=pages,
        complete=end_of_history or reached_since,
        coverage_from_ms=coverage_from,
        oldest_ms=oldest,
        newest_ms=newest,
        reason=reason,
        receipts=tuple(receipts),
    )


# --------------------------------------------------------------------------------------
# coverage — read through tape.py, never beside it
# --------------------------------------------------------------------------------------


def _proved(record: tape.TapeRecord | None) -> bool:
    """Thin alias for ``tape.TapeRecord.proved``, kept only as a null-safe call site.

    This used to be a full duplicate of the predicate, because ``TapeRecord.proved``
    hard-coded ``route == 'pumpfun:trades'`` and this task did not own tape.py. That
    widening landed on 2026-09-21 (``tape.PER_TOKEN_ROUTES``), so the copy is gone and
    there is one definition of "proved" again.

    ``tests/test_stonkfun.py::test_proved_predicate_matches_tape`` was written to fail the
    moment tape.py was widened, precisely so the duplicate could not be forgotten. It did.
    """
    return record is not None and record.proved


def completeness(
    chain: Chain, token: str, conn: sqlite3.Connection | None = None
) -> tuple[bool, str]:
    """``(complete, reason)``. Fails closed on every path, exactly as ``tape.completeness``.

    No record, a partial record, a wallet-walk record, a proof made against a
    ``created_ms`` that has since moved earlier, or a database that will not answer all
    return ``False``. There is deliberately no argument that relaxes this.
    """
    c = _conn(conn)
    rec = tape.record_of(chain, token, c)
    if rec is None:
        return False, "no_tape_record"
    if not _proved(rec):
        return False, f"{rec.coverage}:{rec.reason}"
    try:
        row = fetch_one(
            c, "SELECT created_ms FROM tokens WHERE chain=? AND address=?", (chain.value, token)
        )
    except sqlite3.Error as exc:
        log.warning("stonkfun: token read failed for %s (%s)", token[:12], exc)
        return False, "tokens_unreadable"
    current = _int(row["created_ms"]) if row else None
    if current is None:
        return False, "creation_time_unknown"
    if rec.covered_from_ms is None or rec.covered_from_ms > current:
        return False, (
            f"launch_moved_before_coverage:{rec.covered_from_ms}>{current};created_ms_moved"
        )
    return True, rec.proof or "complete"


def is_complete(chain: Chain, token: str, conn: sqlite3.Connection | None = None) -> bool:
    """Do we hold every trade this mint has ever had? The gate for anything that needs it."""
    return completeness(chain, token, conn)[0]


def complete_tokens(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> list[str]:
    """Every StonkFun mint whose tape is proved complete, re-checked against ``tokens``."""
    try:
        rows = fetch_all(
            _conn(conn),
            "SELECT tt.token AS token FROM token_tape tt "
            "JOIN tokens tk ON tk.chain = tt.chain AND tk.address = tt.token "
            "WHERE tt.chain=? AND tt.coverage=? AND tt.route=? AND tt.proof IS NOT NULL "
            "  AND tt.covered_from_ms IS NOT NULL AND tk.created_ms IS NOT NULL "
            "  AND tt.covered_from_ms <= tk.created_ms "
            "ORDER BY tt.token",
            (chain.value, tape.COMPLETE, ROUTE),
        )
    except sqlite3.Error as exc:
        log.warning("stonkfun: complete-token listing failed (%s)", exc)
        return []
    return [str(r["token"]) for r in rows]


def coverage_summary(
    chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Counts an operator can read at a glance and check against the database by hand."""
    c = _conn(conn)
    out: dict[str, Any] = {
        "launches_known": 0,
        "complete": 0,
        "partial": 0,
        "unavailable": 0,
        "unassessed": 0,
        "swaps": 0,
        "swaps_with_sol_quote": 0,
        "quote_mints": 0,
    }
    try:
        row = fetch_one(
            c, "SELECT COUNT(*) AS n FROM stonkfun_launches WHERE chain=?", (chain.value,)
        )
        out["launches_known"] = _int(row["n"]) if row else 0
        out["complete"] = len(complete_tokens(chain, c))
        for r in fetch_all(
            c,
            "SELECT tt.coverage AS coverage, COUNT(*) AS n FROM token_tape tt "
            "JOIN stonkfun_launches sl ON sl.chain = tt.chain AND sl.token = tt.token "
            "WHERE tt.chain=? GROUP BY tt.coverage",
            (chain.value,),
        ):
            key = str(r["coverage"])
            if key in (tape.PARTIAL, tape.UNAVAILABLE):
                out[key] = _int(r["n"]) or 0
        assessed = fetch_one(
            c,
            "SELECT COUNT(*) AS n FROM token_tape tt "
            "JOIN stonkfun_launches sl ON sl.chain = tt.chain AND sl.token = tt.token "
            "WHERE tt.chain=?",
            (chain.value,),
        )
        out["unassessed"] = max(0, out["launches_known"] - (_int(assessed["n"]) if assessed else 0))
        row = fetch_one(
            c,
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN amount_native IS NOT NULL THEN 1 ELSE 0 END) AS sol, "
            "       COUNT(DISTINCT quote_mint) AS quotes "
            "FROM swaps WHERE chain=? AND source=?",
            (chain.value, SOURCE),
        )
        if row is not None:
            out["swaps"] = _int(row["n"]) or 0
            out["swaps_with_sol_quote"] = _int(row["sol"]) or 0
            out["quote_mints"] = _int(row["quotes"]) or 0
    except sqlite3.Error as exc:
        log.warning("stonkfun: coverage summary failed (%s)", exc)
    return out


# --------------------------------------------------------------------------------------
# collecting one token
# --------------------------------------------------------------------------------------


def launch_from_row(row: Mapping[str, Any], *, chain: Chain = Chain.SOL) -> Launch | None:
    """Rebuild a :class:`Launch` from its stored ``stonkfun_launches`` row.

    ``None`` when a field a trade parse depends on is missing, which is the same refusal
    :func:`parse_launch` would have made. A stored row cannot be trusted more than the
    payload it came from.
    """
    token = _text(row.get("token"))
    pool = _text(row.get("pool"))
    quote_mint = _text(row.get("quote_mint"))
    base_decimals = _int(row.get("base_decimals"))
    quote_decimals = _int(row.get("quote_decimals"))
    platform_config = _text(row.get("platform_config"))
    if not token or not pool or not quote_mint:
        return None
    if base_decimals is None or quote_decimals is None or platform_config is None:
        return None
    return Launch(
        chain=chain,
        token=token,
        pool=pool,
        platform_config=platform_config,
        creator=None,
        created_ms=_int(row.get("created_ms")),
        # The launch row does not carry the token's own symbol or creator -- those live on
        # `tokens` and nothing in the trade path reads them. Left None rather than filled
        # with the quote's symbol, which is a different token.
        symbol=None,
        name=None,
        base_decimals=base_decimals,
        quote_mint=quote_mint,
        quote_symbol=_text(row.get("quote_symbol")),
        quote_decimals=quote_decimals,
        transfer_fee_bps=_int(row.get("transfer_fee_bps")),
        config_id=_text(row.get("config_id")),
        total_base_sell=_int(row.get("total_base_sell")),
        graduation_quote=_int(row.get("graduation_quote")),
        migrate_type=_text(row.get("migrate_type")),
        progress_pct=_dec(row.get("progress_pct")),
    )


def _swap_counts(chain: Chain, token: str, conn: sqlite3.Connection) -> dict[str, int | None]:
    """Row counts and the time span we hold for a mint, split by how they were obtained."""
    out: dict[str, int | None] = {"total": 0, "route": 0, "oldest_ms": None, "newest_ms": None}
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, MIN(ts_ms) AS lo, MAX(ts_ms) AS hi, "
            " SUM(CASE WHEN source=? THEN 1 ELSE 0 END) AS route "
            "FROM swaps WHERE chain=? AND token=?",
            (SOURCE, chain.value, token),
        )
    except sqlite3.Error as exc:
        log.warning("stonkfun: swap counts unreadable for %s (%s)", token[:12], exc)
        return out
    if row is None:
        return out
    out["total"] = _int(row["n"]) or 0
    out["route"] = _int(row["route"]) or 0
    out["oldest_ms"] = _int(row["lo"])
    out["newest_ms"] = _int(row["hi"])
    return out


def collect_token(
    chain: Chain,
    token: str,
    conn: sqlite3.Connection | None = None,
    *,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.RESEARCH,
    at_ms: int | None = None,
) -> tuple[tape.TapeRecord, FlowResult | None]:
    """Pull one mint's tape and record what that establishes. Never raises.

    Three modes, chosen from what we already hold, exactly as ``tape.collect_token``:

    * **Top-up** of a proved tape, from its stored watermark. Usually one page.
    * **Full walk** for everything else, until the route stops paginating.
    * **Refusal without a request** when we do not hold the launch record, because the
      base and quote exponents come from it and a trade parsed without them would be
      wrong by a power of ten in both legs.

    The verdict comes from ``tape._classify`` — the same pure function the pump.fun job
    and the tier-1 scanner use — so a tape proved here means what a tape proved there
    means.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    prior = tape.record_of(chain, token, c)
    attempts = (prior.attempts if prior else 0) + 1

    stored = launch_of(chain, token, c)
    launch = launch_from_row(stored, chain=chain) if stored else None

    def _finish(
        coverage: str,
        reason: str,
        *,
        proof: str | None = None,
        covered_from: int | None = None,
        covered_to: int | None = None,
        pages: int | None = None,
        created_ms: int | None = None,
        retry: bool,
    ) -> tape.TapeRecord:
        fresh = _swap_counts(chain, token, c)
        if coverage == tape.COMPLETE:
            next_attempt = None
        elif coverage == tape.PARTIAL and retry:
            next_attempt = now + config.partial_retry_s * 1000
        elif retry:
            next_attempt = now + config.backoff_for(attempts) * 1000
        else:
            next_attempt = now + config.backoff_s[-1] * 1000
        record = tape.TapeRecord(
            chain=chain,
            token=token,
            coverage=coverage,
            # The per-token route is what ran, whatever it returned -- `route` records how
            # the collection was MADE, not an inventory of which sources left rows here.
            route=ROUTE,
            proof=proof,
            reason=reason[:400],
            covered_from_ms=covered_from,
            covered_to_ms=covered_to,
            created_ms=created_ms,
            oldest_ms=fresh["oldest_ms"],
            newest_ms=fresh["newest_ms"],
            swaps_route=fresh["route"],
            swaps_total=fresh["total"],
            pages=pages,
            attempts=attempts,
            last_attempt_ms=now,
            next_attempt_ms=next_attempt,
        )
        if not tape.store(record, c, at_ms=now):
            # Migration 027's CHECK constraints refused the claim. Record the weaker truth
            # rather than leaving the token looking unassessed, and never retry into it.
            fallback = tape.TapeRecord(
                chain=chain,
                token=token,
                coverage=tape.PARTIAL,
                route=ROUTE,
                reason=f"coverage_claim_rejected:{reason}"[:400],
                covered_to_ms=covered_to,
                created_ms=created_ms,
                oldest_ms=fresh["oldest_ms"],
                newest_ms=fresh["newest_ms"],
                swaps_route=fresh["route"],
                swaps_total=fresh["total"],
                pages=pages,
                attempts=attempts,
                last_attempt_ms=now,
                next_attempt_ms=now + config.backoff_for(attempts) * 1000,
            )
            tape.store(fallback, c, at_ms=now)
            return fallback
        return record

    if chain is not Chain.SOL:
        return _finish(tape.UNAVAILABLE, f"no_trade_source_for_{chain.value}", retry=False), None
    if launch is None:
        return _finish(
            tape.UNAVAILABLE,
            "no_stonkfun_launch_record:decimals_and_quote_unknown",
            retry=False,
        ), None

    created_ms = launch.created_ms
    topping_up = (
        _proved(prior) and prior is not None and prior.covered_to_ms is not None
        and created_ms is not None and prior.covered_from_ms is not None
        and prior.covered_from_ms <= created_ms
    )
    watermark = prior.covered_to_ms if (topping_up and prior is not None) else None
    budget = config.topup_pages if topping_up else config.walk_pages

    flow = collect_trades(
        launch, c, since_ms=watermark, max_pages=budget, config=config, priority=priority
    )

    # A refusal must never be written over a proof earned while the route was answering.
    # Inherited from tape.py rather than rediscovered: `complete -> unavailable` on
    # silence destroyed five irrecoverable proofs on 2026-09-20.
    if flow.reason in tape.NON_OBSERVATIONS and prior is not None and _proved(prior):
        return tape.record_failed_attempt(
            prior,
            c,
            flow_reason=flow.reason,
            now=now,
            config=tape.TapeConfig(backoff_s=config.backoff_s),
        ), flow

    newest = max(flow.newest_ms or 0, (prior.covered_to_ms if prior else 0) or 0) or None
    verdict = tape._classify(
        flow_reason=flow.reason,
        pages=flow.pages,
        created_ms=created_ms,
        oldest_ms=flow.coverage_from_ms if flow.coverage_from_ms is not None else flow.oldest_ms,
        prior=prior,
        topping_up=topping_up,
    )
    record = _finish(
        verdict.coverage,
        verdict.reason,
        proof=verdict.proof,
        covered_from=verdict.covered_from_ms,
        covered_to=newest,
        pages=flow.pages,
        created_ms=created_ms,
        retry=verdict.retry,
    )
    if _proved(record):
        mark_create_tx(chain, token, c)
    return record, flow


def mark_create_tx(
    chain: Chain, token: str, conn: sqlite3.Connection | None = None
) -> int:
    """Set ``swaps.is_create_tx`` on the first trade of a **proved-complete** tape.

    Returns rows marked. Idempotent, and it never clears a flag another collector set.

    This is a derived fact with a stated basis, not the usual guess. ``tape`` gets the
    pump.fun create signature from ``tokens.meta_json``, which the launchpad supplies; the
    LaunchLab index supplies no signature, so the basis here is different and is recorded
    as ``first_trade_of_proved_tape``:

    * **MEASURED**: across 57 tapes that walked to end-of-history, the oldest trade's
      block time equalled the launch index's ``createAt`` exactly on 56 and was 1,000 ms
      later on 1. No sampled pool had zero trades.
    * **CHAIN-VERIFIED, 19 of 19**: on a separate sample of 19 terminated tapes, the
      oldest row's transaction logs ``Instruction: Initialize*`` — the pool creation and
      the first buy are one transaction, as they are on pump.fun. (11 further pools in
      that sample were skipped because the public RPC throttled, not because they
      disagreed.)

    The flag is set by **transaction**, not by row, so a create transaction carrying more
    than one trade marks all of them — same rule as ``tape.repair_create_flags``. It is
    deliberately not restricted to the creator's own wallet: in the same sample the oldest
    row's ``owner`` matched the pool's ``creator`` on only 10 of 19, so "the creator's
    first buy" would have missed nearly half of them.

    The guard is that the tape must be **proved complete**. On a partial tape the oldest
    row we hold is the oldest row we *fetched*, which is an artefact of our page budget —
    marking that would put a phantom create at an arbitrary point in the token's history,
    which is precisely the class of confident-wrong number this codebase refuses.
    """
    c = _conn(conn)
    if not _proved(tape.record_of(chain, token, c)):
        return 0
    try:
        row = fetch_one(
            c,
            "SELECT tx FROM swaps WHERE chain=? AND token=? AND source=? "
            "ORDER BY ts_ms ASC, tx ASC LIMIT 1",
            (chain.value, token, SOURCE),
        )
        if row is None:
            return 0
        return c.execute(
            "UPDATE swaps SET is_create_tx=1 WHERE chain=? AND token=? AND tx=? AND is_create_tx=0",
            (chain.value, token, str(row["tx"])),
        ).rowcount or 0
    except sqlite3.Error as exc:
        log.warning("stonkfun: create-flag update failed for %s (%s)", token[:12], exc)
        return 0


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


def candidates(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    limit: int = 500,
    config: StonkConfig = DEFAULT_CONFIG,
    at_ms: int | None = None,
    include_retired: bool = False,
) -> list[str]:
    """StonkFun mints lacking a proved tape and due an attempt, newest launch first.

    Ordering is newest-first for a *different* reason than ``tape.candidates``' and it is
    worth saying which. There the order is forced: the pump.fun route serves a hot window,
    so an old mint is a request that cannot succeed. Here 70 of 70 pools answered
    regardless of idleness, so the order is only a preference — a fresh launch is the one
    a lane might act on today. A backfill that wants the oldest first can simply ask for
    it, and nothing is lost by waiting.
    """
    c = _conn(conn)
    now = at_ms if at_ms is not None else now_ms()
    # The completeness predicate is compared against `tokens.created_ms`, not against the
    # copy stored on the tape row, so this asks exactly the question `completeness` answers.
    # Using the stored copy would leave a token whose launch time moved earlier looking
    # done here while reading as incomplete everywhere else.
    sql = (
        "SELECT sl.token AS token FROM stonkfun_launches sl "
        "LEFT JOIN token_tape tt ON tt.chain = sl.chain AND tt.token = sl.token "
        "LEFT JOIN tokens tk ON tk.chain = sl.chain AND tk.address = sl.token "
        "WHERE sl.chain=? "
        "  AND NOT (tt.coverage = ? AND tt.route = ? AND tt.proof IS NOT NULL "
        "           AND tt.covered_from_ms IS NOT NULL AND tk.created_ms IS NOT NULL "
        "           AND tt.covered_from_ms <= tk.created_ms) "
        "  AND (tt.next_attempt_ms IS NULL OR tt.next_attempt_ms <= ?) "
    )
    params: list[Any] = [chain.value, tape.COMPLETE, ROUTE, now]
    if not include_retired:
        sql += "  AND (tt.attempts IS NULL OR tt.attempts < ?) "
        params.append(int(config.max_attempts))
    sql += "ORDER BY sl.created_ms IS NULL, sl.created_ms DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        rows = fetch_all(c, sql, tuple(params))
    except sqlite3.Error as exc:
        log.warning("stonkfun: candidate query failed (%s)", exc)
        return []
    return [str(r["token"]) for r in rows]


def _provider_calls_since(conn: sqlite3.Connection, since_ms: int) -> tuple[int, int]:
    """``(requests, rate_limited)`` the limiter recorded for this provider since ``since_ms``.

    Six lines duplicated from ``tape._provider_calls_since`` because that one is keyed to
    the pump.fun provider string and takes no argument for another. The limiter's own
    ledger is the only honest account of what we put on the wire — it counts the calls
    that came back 4xx too — and a job that reports a rate must report that one.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, SUM(CASE WHEN status='rate_limited' THEN 1 ELSE 0 END) AS limited "
            "FROM provider_calls WHERE provider=? AND ts_ms >= ?",
            (PROVIDER, int(since_ms)),
        )
    except sqlite3.Error as exc:
        log.debug("stonkfun: provider call ledger unreadable (%s)", exc)
        return 0, 0
    if row is None:
        return 0, 0
    return (_int(row["n"]) or 0), (_int(row["limited"]) or 0)


def run(
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    config: StonkConfig = DEFAULT_CONFIG,
    priority: Priority = Priority.RESEARCH,
    limit: int | None = None,
    budget_s: float | None = None,
    launches: bool = True,
    launch_pages: int | None = None,
    tokens: Sequence[str] | None = None,
) -> tape.RunReport:
    """One StonkFun pass: refresh the launch index, then pull tapes. Never raises.

    Reuses ``tape.RunReport`` so an operator reads the same numbers for both venues,
    including ``request_rate_per_s``, which is counted from the limiter's ledger rather
    than from pages obtained — the two differ, and only one of them is what the endpoint
    feels.

    Safe to run repeatedly and resumable: a proved-complete token is topped up from its
    watermark, usually in one page, and a token in backoff is not asked at all.
    """
    c = _conn(conn)
    report = tape.RunReport()
    # perf_counter, not monotonic: on Windows CPython 3.12 monotonic() is GetTickCount64
    # at ~15.6 ms resolution, so a short pass measures 0.0 s and reports a 0.0 rate.
    deadline = time.perf_counter() + float(budget_s if budget_s is not None else config.budget_s)

    if launches:
        found = ingest_launches(c, chain=chain, config=config, pages=launch_pages,
                                priority=priority)
        report.note(f"launches_seen:{found.rows_seen}")
        report.note(f"launches_new:{found.tokens_new}")

    report.complete_before = len(complete_tokens(chain, c))
    todo = (
        list(tokens)
        if tokens is not None
        else candidates(chain, c, limit=int(limit or config.max_tokens), config=config)
    )
    started = time.perf_counter()
    ledger_from_ms = now_ms()

    for token in todo[: int(limit or config.max_tokens)]:
        if time.perf_counter() >= deadline:
            report.note("budget_exhausted")
            break
        record, flow = collect_token(chain, token, c, config=config, priority=priority)
        report.attempted += 1
        report.pages += flow.pages if flow else 0
        report.rows_written += flow.rows_written if flow else 0
        if record.coverage == tape.COMPLETE:
            report.completed += 1
        elif record.coverage == tape.PARTIAL:
            report.partial += 1
        else:
            report.unavailable += 1
        report.note(record.reason.split(":")[0][:60])

    report.elapsed_s = time.perf_counter() - started
    report.requests, report.rate_limited = _provider_calls_since(c, ledger_from_ms)
    report.complete_after = len(complete_tokens(chain, c))
    report.skipped = max(0, len(todo) - report.attempted)
    _emit_summary(chain, report, c)
    return report


async def watch(
    stop: asyncio.Event | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    chain: Chain = Chain.SOL,
    config: StonkConfig = DEFAULT_CONFIG,
    max_polls: int | None = None,
) -> dict[str, Any]:
    """Poll the launch index until ``stop``. The shape ``ingest.runner.REGISTRY`` expects.

    Launches only. The tape is **not** collected here, and that is a decision the
    measurement earned: pump.fun's tape has to be captured at scan time because it is gone
    an hour later, but 70 of 70 StonkFun pools served their full tape after up to 14 days
    of idleness. Coupling the tape to the launch feed would put a variable, unbounded cost
    inside a latency-sensitive loop to buy something :func:`run` can get later for free.
    """
    c = _conn(conn)
    stop = stop or asyncio.Event()
    polls = 0
    totals = {"polls": 0, "rows_seen": 0, "tokens_new": 0, "pages": 0, "rejected": 0}
    while not stop.is_set():
        try:
            found = await asyncio.to_thread(
                ingest_launches, c, chain=chain, config=config, priority=Priority.DISCOVERY
            )
            totals["polls"] += 1
            totals["rows_seen"] += found.rows_seen
            totals["tokens_new"] += found.tokens_new
            totals["pages"] += found.pages
            totals["rejected"] += found.rows_rejected
        except Exception as exc:  # noqa: BLE001 - a feed must not die on one bad poll
            log.warning("stonkfun: launch poll failed (%s)", exc)
        polls += 1
        if max_polls is not None and polls >= max_polls:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.poll_interval_s)
        except TimeoutError:
            continue
    return totals


def _emit_summary(chain: Chain, report: tape.RunReport, conn: sqlite3.Connection) -> None:
    """Telemetry must never break a collection pass."""
    try:
        from kaiba.core import events as ev

        ev.emit(
            EventKind.SYSTEM,
            {"job": "ingest.stonkfun", **report.as_dict()},
            chain=chain,
            subject="ingest.stonkfun",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry is not load-bearing
        log.debug("stonkfun: could not record run summary (%s)", exc)


__all__ = [
    "DEFAULT_CONFIG",
    "FEE_PAYER_NOTE",
    "LAUNCHES_ENDPOINT",
    "LAUNCHES_URL",
    "LAUNCHLAB_PROGRAM",
    "LAUNCHPAD",
    "MINTS_ENDPOINT",
    "MINTS_URL",
    "OBSERVED_BASE_DECIMALS",
    "OBSERVED_TOTAL_BASE_SELL",
    "PLATFORM_CONFIGS",
    "PROVIDER",
    "ROUTE",
    "SOL_QUOTE_MINTS",
    "SOURCE",
    "TRADES_ENDPOINT",
    "TRADES_URL",
    "VENUE",
    "Launch",
    "LaunchReport",
    "StonkConfig",
    "TradeRow",
    "candidates",
    "collect_token",
    "collect_trades",
    "complete_tokens",
    "completeness",
    "coverage_summary",
    "fetch_launch_page",
    "fetch_launches_by_mints",
    "fetch_trades_page",
    "ingest_launches",
    "is_complete",
    "launch_from_row",
    "launch_of",
    "mark_create_tx",
    "parse_launch",
    "parse_trade",
    "record_launch",
    "run",
    "watch",
    "write_trades",
]
