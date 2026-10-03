"""The tunable half of the execution gate: how much, how often, and when to stop.

``kaiba.execution.policy`` enforces the one rule that is not a number. Everything here *is*
a number, and Hermes may move it — inside the operator's envelope in ``config/risk.yaml``.
That split is the owner's mandate (``docs/PLAN.md`` §1): full authority over sizing and
strategy, one hard exclusion, and "missing numeric authorization is not infinity".

Two design choices follow from that last clause:

* **Unfunded is not unlimited.** A chain whose ``bankroll_base_units`` is 0 cannot size a
  position at all; it does not fall back to "whatever the wallet holds".
* **The config is re-read on every check.** ``config/risk.yaml`` is edited while the agent
  runs — by the dashboard's risk dials, by Hermes retuning itself, by the operator hitting
  the kill switch. A gate that cached it would keep trading for as long as the process
  lived. :class:`RiskGate` holds no configuration state.

* **A position of the wrong size is refused, at either end.** Round-trip cost is
  U-shaped: the flat half does not shrink with the position (at 0.02 SOL it is ~9.9% of
  the trade before any venue fee) and our own price impact grows with it (0.06 SOL into
  a $25 pool is ~3,200 bps a leg). ``kaiba.execution.viability`` measures the fee terms
  per chain from our own closed trades and the impact term per *token* from its curve or
  pool, and :meth:`RiskGate.check_entry` refuses outside the resulting band.

Exits are never gated. The kill switch, ``reduce_only``, ``entries_paused``, the daily
loss stop and the sizing band all block *entries*; :meth:`RiskGate.check_exit` always
allows, because a brake that also stops you selling is not a brake, it is a way to lose
the whole position. The sizing band is the worst possible place to get that wrong: an
oversized position's only way out is through the same thin pool, and a refused sell
strands it permanently.

* **The bankroll compounds.** ``bankroll_base_units`` in ``config/risk.yaml`` is a number
  a human typed; it does not move when we win or lose, so a doubled account keeps betting
  the same absolute amount and a halved one bets a dangerously large fraction of what is
  left. :class:`BankrollTracker` turns it into a *baseline* and derives the live figure
  from realised PnL, capped by what the wallet actually holds. See that class for the
  whole argument; the two rules that matter most are that the cap is ground truth (we
  never size against money we do not have) and that "we could not look" holds the last
  known good value instead of collapsing to zero.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from kaiba.core import events
from kaiba.core.config import ChainBudget, RiskConfig, get_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload
from kaiba.core.schemas import NATIVE_DECIMALS, Chain, EventKind, Lane, LaneMode, now_ms
from kaiba.execution import viability
from kaiba.execution.policy import PolicyDecision

log = logging.getLogger(__name__)

#: score -> fraction of the lane's maximum size (``docs/PLAN.md`` §6.2). Below 70 the lane
#: has no size at all: a signal we cannot grade is a signal we do not take.
#:
#: MEASURED 2026-09-23 over 117 closed live fills, by the conviction that admitted them:
#:
#:     70-80   n=10   mean -10.0%   win 40%    <- the LOWEST band we admit, and the best
#:     80-90   n=44   mean -20.9%   win 14%
#:     >=90    n=63   mean -16.6%   win 17%
#:     <70     n= 0        (never admitted, so never measured)
#:
#: Conviction is anti-calibrated inside the range we trade, the same shape as smart-wallet
#: count (3 wallets -8.9%, 4+ -18.4%, 7+ a 0% win rate) and our own dossier score (>70
#: returned -40.8% with no winners). Three independent confidence measures, all pointing
#: the wrong way. That argues for admitting the 60-70 band.
#:
#: A 60-70 rung at 0.10x was written and REVERTED the same day, because the size it
#: promised does not exist. Every chain floors a position at `min_position_base_units`,
#: which is the economic floor below which fees dominate, so the rung would have been
#: clamped straight back up:
#:
#:     sol        0.25x -> 0.050000   0.10x -> 0.045000   (10% cheaper)
#:     bsc        0.25x -> 0.005000   0.10x -> 0.003000   (40% cheaper)
#:     robinhood  0.25x -> 0.004350   0.10x -> 0.004350   (IDENTICAL)
#:
#: There is no cheap probe on-chain. Admitting 60-70 means admitting it at real size in a
#: band with zero measured outcomes, which is a different decision from the one the
#: anti-calibration evidence supports. The question belongs in the shadow lane, where a
#: size of zero costs nothing and the answer is worth the same.
SCORE_LADDER: tuple[tuple[float, Decimal], ...] = (
    (95.0, Decimal("1.00")),
    (90.0, Decimal("0.75")),
    (80.0, Decimal("0.50")),
    (70.0, Decimal("0.25")),
)

#: Rejections that mean "a brake is on" rather than "this particular size is wrong". Only
#: these are worth an event; a size that missed the envelope is ordinary operation.
_BRAKE_REASONS = frozenset(
    {"kill_switch", "entries_paused", "reduce_only", "daily_loss_stop", "daily_stop_reserve",
     "lane_off"}
)


def day_key(ts_ms: int | None = None) -> str:
    """UTC day. The loss stop resets at 00:00 UTC, not at the operator's midnight."""
    ts = ts_ms if ts_ms is not None else now_ms()
    return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")


#: Lane conviction (0-1) -> the 0-100 score the ladder reads.
SCORE_SCALE = 100.0


def score_from_strength(strength: float) -> float:
    """Convert a lane's 0-1 conviction into the 0-100 score :data:`SCORE_LADDER` expects.

    The two scales were never written down, so ``Signal.strength`` went into the ladder
    raw and every signal landed below its lowest rung. Naming the conversion is the fix;
    the guard in :func:`score_fraction` is the alarm if anyone skips it again.
    """
    return float(strength) * SCORE_SCALE


def score_fraction(score: float) -> Decimal:
    """Score (0-100) -> fraction of the lane's maximum size. Below 70 there is no size.

    A value in (0, 1] is almost certainly an unconverted ``Signal.strength``: a genuine
    score in that range is already below every rung, so the result is the same either way
    and saying so costs nothing. Silence here cost us half a day of a dead engine.
    """
    if 0.0 < score <= 1.0:
        log.warning(
            "score_fraction got %.4f, which looks like an unconverted 0-1 Signal.strength "
            "rather than a 0-100 score; sizing to zero. Use risk.score_from_strength().",
            score,
        )
    for threshold, fraction in SCORE_LADDER:
        if score >= threshold:
            return fraction
    return Decimal(0)


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def _as_int(value: Any, default: int = 0) -> int:
    """Base units come back from SQLite as TEXT to survive 2^63. Never via float."""
    if value is None or value == "":
        return default
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default


# ======================================================================================
# the compounding bankroll
# ======================================================================================
#
# Everything from here to :class:`RiskGate` answers one question: *how much money do we
# actually have on this chain right now?* The old answer was "whatever the operator last
# typed into config/risk.yaml", which is wrong in both directions and expensively so.
# Measured 2026-09-21 against the live wallets:
#
#   chain       config bankroll   wallet holds      invisible to the sizer
#   sol         4.5    SOL        4.574456858 SOL   0.074456858 SOL  (~$8.76)
#   bsc         0.95   BNB        1.031957    BNB   0.081957    BNB  (~$64.01)
#   robinhood   0.15   ETH        0.392310584 ETH   0.242310584 ETH  (~$654.09)
#
# ~$727 of a ~$2,403 book — 30% — is unspendable because of a stale number in a YAML
# file. That is the cost of *not* compounding measured on today's balances, before a
# single trade is placed.

#: The native-coin address GMGN's ``portfolio token-balance`` wants, per chain.
#:
#: MEASURED 2026-09-21 by running the CLI against all three funded wallets. This table is
#: the whole reason this section exists as code rather than as a suggestion, because the
#: Solana entry is a trap:
#:
#:   --token So1111...111  ->  {"balance":"4.574456858", "height":448948213}   correct
#:   --token So1111...112  ->  {"balance":"0",           "height":0}           WRONG
#:
#: ``So111...112`` is the canonical wrapped-SOL mint and the address any reasonable person
#: would reach for. GMGN answers it with the wallet's *wrapped* SOL token account, which is
#: empty, and reports it as a clean zero. Wire that address into a bankroll and Solana --
#: our most-measured chain -- reads as unfunded and the agent silently stops trading on it.
#: The one-character difference is why :func:`parse_native_balance` refuses to believe a
#: zero that arrives with ``height == 0``.
_EVM_NATIVE_TOKEN = "0x" + "0" * 40
NATIVE_BALANCE_TOKEN: dict[Chain, str] = {
    Chain.SOL: "So11111111111111111111111111111111111111111",
    Chain.ETH: _EVM_NATIVE_TOKEN,
    Chain.BSC: _EVM_NATIVE_TOKEN,
    Chain.BASE: _EVM_NATIVE_TOKEN,
    Chain.ROBINHOOD: _EVM_NATIVE_TOKEN,
    Chain.ARC: _EVM_NATIVE_TOKEN,
    Chain.STABLE: _EVM_NATIVE_TOKEN,
}

#: Not a typo for the above. Kept named so nobody "fixes" the constant back to it.
#: MEASURED 2026-09-21: returns ``balance="0", height=0`` for a wallet holding 4.574 SOL.
WRAPPED_SOL_MINT_READS_ZERO = "So11111111111111111111111111111111111111112"

#: How long a balance read stays fresh. A read costs 848-1053 ms (MEASURED, n=3, mean
#: ~969 ms, gmgn-cli on this host), which is far too slow to do inside a sizing decision
#: when the operator wants high volume. At a 30 s TTL the provider is on the critical path
#: for ~3% of wall-clock and every decision in between is served from SQLite. 30 s is the
#: same TTL ``gmgn_cli`` already gives ``portfolio.holdings``.
BANKROLL_FRESH_MS = 30_000

#: Beyond :data:`BANKROLL_FRESH_MS` a cached read is still *held* -- it is the last thing
#: we actually saw -- but the bankroll is frozen: it may fall, never rise. Past this
#: window the cached figure is too old to be evidence of anything and we fall back to the
#: operator's configured number, which is an authorisation rather than an observation.
#: INVENTED: 24 h is a judgement call, not a measurement. It is long because the failure
#: it guards against (sizing off a day-old balance) produces failed sends, not losses,
#: while the alternative (refusing to trade during a provider outage) is the thing the
#: operator explicitly does not want.
BANKROLL_HOLD_MS = 24 * 60 * 60 * 1000

#: Ceiling on how far the bankroll may *rise* in one read: nothing may more than double in
#: a single step. Falls are never clamped -- under-sizing cannot cause ruin and
#: over-sizing can.
#:
#: INVENTED. No measurement fixes this number, and it is deliberately loose, because the
#: structural defences do the real work and a tight clamp costs the operator money:
#:
#: * Growth comes from ``min(ledger, on-chain cap)``. A wrong *high* balance read cannot
#:   raise the bankroll at all, because the ledger does not know about the extra money.
#:   A wrong high ledger cannot raise it either, because the chain caps it. Only both
#:   being wrong in the same direction at the same time gets through, and this clamp is
#:   the tripwire for that one case.
#: * Even a bankroll that did run away cannot spend money we do not hold:
#:   :attr:`BankrollReading.free_base_units` is separately clamped to the wallet balance
#:   minus the gas reserve, and ``max_position_base_units`` still caps any single order.
#:
#: There is deliberately **no time term**. An earlier draft allowed +100%/hour, which
#: meant a real, verified +20% day took twelve minutes to reach the sizer -- lag the
#: operator did not ask for, buying safety that ``min(ledger, cap)`` already provides.
#: A rate would also have to be computed from a clock, and clocks jump.
BANKROLL_MAX_GROWTH_FACTOR_PER_READ = Decimal("2")

#: Only *this* mode's trades move real money, so only this mode's trades compound the
#: bankroll. Paper fills (SHADOW and CANARY both fill on the paper broker) must never
#: raise the live sizing denominator: that is how a simulated +219% becomes a real
#: oversized bet.
COMPOUNDING_MODE: LaneMode = LaneMode.LIVE

_KV_PREFIX = "risk:bankroll:v1:"


class BankrollBasis:
    """Where the on-chain half of a reading came from. Never a bare bool."""

    FRESH = "fresh"           #: read from the provider inside this call
    CACHED = "cached"         #: read recently enough to still count as an observation
    HELD = "held"             #: last known good, past its TTL: usable as a cap, frozen
    EXPIRED = "expired"       #: last known good, older than the hold window: unusable
    UNAVAILABLE = "unavailable"  #: we looked and could not see. NOT zero, and nothing cached.
    UNWIRED = "unwired"       #: no provider injected -- nobody has looked at all
    NO_WALLET = "no_wallet"   #: the chain budget names no wallet to read

    #: Bases that are not a current observation of the chain. Under any of them the
    #: bankroll holds and may fall but must not rise: growing on evidence we do not have
    #: is the whole failure this class exists to prevent.
    NO_GROWTH = frozenset({HELD, EXPIRED, UNAVAILABLE, UNWIRED, NO_WALLET})


@dataclass(frozen=True)
class NativeBalance:
    """One native-coin balance reading, or an explicit admission that there is none.

    ``base_units`` is ``None`` for *every* failure mode. It is never 0, because the whole
    point of this type is that "the wallet is empty" and "we could not look" are different
    facts and conflating them already cost a day: a zero read is indistinguishable from an
    unfunded chain, and an unfunded chain refuses every entry.
    """

    base_units: int | None
    basis: str
    note: str
    height: int | None = None

    @property
    def ok(self) -> bool:
        return self.base_units is not None

    def __bool__(self) -> bool:  # `if balance:` must not be true for UNAVAILABLE
        return self.ok


def _native_decimals(chain: Chain) -> int:
    return NATIVE_DECIMALS.get(chain, 18)


def parse_native_balance(
    chain: Chain, payload: Any, *, wallet: str | None = None
) -> NativeBalance:
    """Turn GMGN's ``portfolio token-balance`` payload into integer base units.

    MEASURED payload, 2026-09-21::

        {"balances":[{"wallet_address":"0x7243...","token_address":"0x0000...",
                      "balance":"1.031957","decimal":0,"height":123117107,"tx_index":0}]}

    Three things about that shape are load-bearing and none of them are obvious:

    * ``balance`` is a **decimal string in whole coins**, not base units. It goes through
      :class:`~decimal.Decimal` and is scaled by the chain's known decimals. Floats never
      touch it: ``float("0.392310583560926627") * 1e18`` is off by hundreds of wei, and
      the direction of that error is not guaranteed to be down.
    * ``decimal`` is **0 on every chain we measured**, including chains whose native coin
      obviously is not an integer. It is a placeholder. Trusting it would make 1.031957
      BNB into 1 wei. We ignore the field entirely and use
      :data:`kaiba.core.schemas.NATIVE_DECIMALS`.
    * ``height`` is the block the balance was read at, and it is ``0`` exactly when GMGN
      has nothing -- both zero readings we produced (wrong Solana mint, system program)
      came back ``balance="0", height=0`` while all three real readings carried a real
      block number. So a zero with no height is reported UNAVAILABLE, not empty.
      MEASURED on n=5 readings, which is a small sample; the rule is deliberately the
      conservative direction, since calling a real zero "unavailable" holds the last known
      good value and refuses to grow, whereas calling an unavailable "zero" stops trading.

    Fractional base units are truncated toward zero. Rounding up invents money we do not
    have, and money we do not have becomes a failed send rather than a trade.
    """
    if payload is None:
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, "provider returned nothing")
    if isinstance(payload, NativeBalance):
        return payload
    if not isinstance(payload, dict):
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, f"unexpected payload {type(payload).__name__}")

    rows = payload.get("balances")
    if isinstance(payload.get("data"), dict) and rows is None:
        rows = payload["data"].get("balances")
    if not isinstance(rows, list) or not rows:
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, "payload carried no balances array")

    want_token = NATIVE_BALANCE_TOKEN.get(chain, _EVM_NATIVE_TOKEN).lower()
    want_wallet = wallet.lower() if wallet else None
    match: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("token_address", "")).lower() != want_token:
            continue
        if want_wallet and str(row.get("wallet_address", "")).lower() != want_wallet:
            continue
        match = row
        break
    if match is None:
        # Strict on purpose. A payload about a different wallet or a different token is
        # not a weaker version of the answer, it is a different question, and guessing
        # here is how a bankroll ends up sized off somebody else's balance.
        return NativeBalance(
            None, BankrollBasis.UNAVAILABLE, "no row for the native token on this wallet"
        )

    raw = match.get("balance")
    if raw is None or raw == "":
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, "row carried no balance")
    try:
        coins = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, f"unparseable balance {raw!r}")
    if not coins.is_finite() or coins < 0:
        return NativeBalance(None, BankrollBasis.UNAVAILABLE, f"nonsensical balance {raw!r}")

    height = match.get("height")
    height_i = int(height) if isinstance(height, int | float | str) and str(height).strip().lstrip("-").isdigit() else None
    if coins == 0 and not height_i:
        return NativeBalance(
            None,
            BankrollBasis.UNAVAILABLE,
            "zero at block 0 -- GMGN's shape for 'nothing here', not a measured empty "
            "wallet (see WRAPPED_SOL_MINT_READS_ZERO)",
            height=height_i,
        )

    base_units = int(coins.scaleb(_native_decimals(chain)).to_integral_value(rounding="ROUND_DOWN"))
    return NativeBalance(base_units, BankrollBasis.FRESH, f"block {height_i}", height=height_i)


#: A provider is anything that, given a chain and a wallet, hands back GMGN's
#: ``portfolio token-balance`` payload (or a :class:`NativeBalance` directly). It may
#: raise, return junk or block; :meth:`BankrollTracker._read` assumes all three.
#:
#: Deliberately injected rather than imported. ``kaiba.providers.gmgn_cli`` does not
#: expose ``portfolio token-balance`` yet -- it is absent from that module's ``_ALLOWED``
#: allowlist -- and this file does not own that file. Until it is wired the basis is
#: UNWIRED, the cap is the operator's configured number, and behaviour is byte-identical
#: to the static bankroll. Compounding *down* works with no provider at all; compounding
#: *up* deliberately requires proof of funds.
BalanceProvider = Callable[[Chain, str], Any]


@dataclass(frozen=True)
class BankrollReading:
    """What the sizer should use, and every input that produced it.

    ``equity_base_units`` is the denominator: the capital the strategy is running, at cost
    basis, including what is currently deployed. ``free_base_units`` is what can actually
    be spent right now. They are different numbers and using one for the other is the
    double-count that makes size collapse as positions open.
    """

    chain: Chain
    equity_base_units: int
    free_base_units: int
    config_base_units: int
    realized_base_units: int
    open_exposure_base_units: int
    onchain_base_units: int | None
    onchain_basis: str
    onchain_age_ms: int | None
    binding: str
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def compounded(self) -> int:
        """Signed difference from the number the operator typed. Negative after losses."""
        return self.equity_base_units - self.config_base_units


class BankrollTracker:
    """A bankroll that tracks reality instead of a number somebody typed last week.

    **The shape.** Three candidate answers exist and each is wrong on its own:

    ``(a) read the wallet every time``
        Ground truth, and the only thing that knows about gas burnt, failed sends and the
        operator moving money by hand. But it costs ~969 ms (MEASURED, n=3), it cannot see
        capital that is currently deployed into a position -- so the bankroll would appear
        to shrink every time we open one and grow every time we close one, which is
        exactly backwards for a compounding sizer -- and a provider blip becomes a sizing
        event.
    ``(b) config baseline + realised PnL``
        Compounds correctly and costs nothing, but it drifts. It cannot see gas, a failed
        send, a manual withdrawal, or a trade the accounting missed, and every one of
        those errors is in the *optimistic* direction: the ledger thinks we are richer
        than the chain does.
    ``(c) high-water mark / equity curve``
        Rejected outright as a primary. An HWM sizer keeps betting off the peak through a
        drawdown, which is the precise opposite of the operator's "size must fall after
        losses", and on a 4.5 SOL book it is a route to zero.

    So: **(b) compounds and (a) caps.** ``equity = min(ledger, on-chain + deployed)``. The
    ledger's errors are all optimistic, so a ceiling built from the chain catches every
    one of them; the chain cannot see deployed capital, so the ledger supplies it. Only
    *realised* PnL compounds -- an open position that is up 300% does not raise the size
    of the next bet, because unrealised gains are the mark-to-market pyramid that turns
    one good trade into a blown account.

    **Why the cap adds deployed capital back.** A position's cost has already left the
    wallet. Capping equity at the bare wallet balance and *then* subtracting open exposure
    would charge us for the same money twice, and size would collapse toward zero as the
    agent opened positions -- fatal for "high volume". The wallet balance is the cap on
    what is *spendable*; wallet + cost-basis of open positions is the cap on *equity*.
    Both are enforced (:attr:`BankrollReading.free_base_units` and
    :attr:`BankrollReading.equity_base_units`) and both are tested.

    **The four guards, and where each one lives.**

    1. *Never size against money we do not have.* ``equity`` is capped by
       ``on-chain + deployed`` and ``free`` by ``on-chain - gas_reserve``; both clamps are
       applied unconditionally at the end of :meth:`reading`, after every other term, so
       no later adjustment can lift the result back over the cap.
    2. *Unavailable is not zero.* :func:`parse_native_balance` returns ``None`` for every
       failure including a suspicious zero, and :meth:`reading` treats ``None`` as "hold
       the last known good value and forbid growth". The cap never becomes 0 because a
       read failed, and it never becomes infinite either -- with nothing cached at all the
       cap is the operator's configured number, which is the pre-existing behaviour.
    3. *Deployed capital is not free capital.* ``free = equity - open_exposure -
       gas_reserve``, floored at zero, recomputed on every call.
    4. *An outage must not halve the size.* An outage produces UNAVAILABLE, which holds;
       only a **successful** read that is genuinely lower lowers the bankroll. Falls from
       real readings are applied immediately and in full, because a fall cannot cause ruin
       and delaying it can.

    **Growth is clamped, falls are not.** See
    :data:`BANKROLL_MAX_GROWTH_FACTOR_PER_READ`. The asymmetry is the entire safety argument:
    the failure mode of accepting a fall too fast is a small bet, and the failure mode of
    accepting a rise too fast is a large bet on garbage.

    **The honest caveat.** None of this creates an edge. Compounding multiplies whatever
    the per-trade expectancy is, and the research says a perfect graduation oracle has a
    *negative* median net return in all 108 cells tested. If expectancy is negative this
    machinery makes the account die faster and more smoothly. That is the correct
    behaviour for a compounding sizer and the reason guard 4 is written the way it is:
    losses shrink the next bet automatically, so a negative edge decays the book
    geometrically toward the minimum position instead of blowing it up in a straight line.
    Ruin is prevented by the sizing *falling*, not by anyone noticing.
    """

    def __init__(
        self,
        balance_provider: BalanceProvider | None = None,
        *,
        clock: Callable[[], int] = now_ms,
        mode: LaneMode = COMPOUNDING_MODE,
        fresh_ms: int = BANKROLL_FRESH_MS,
        hold_ms: int = BANKROLL_HOLD_MS,
    ) -> None:
        self._provider = balance_provider
        self._clock = clock
        self._mode = mode
        self._fresh_ms = int(fresh_ms)
        self._hold_ms = int(hold_ms)

    # ------------------------------------------------------------------ persisted state

    def _key(self, chain: Chain) -> str:
        return f"{_KV_PREFIX}{chain.value}"

    def state(self, chain: Chain, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """The cached last-known-good, as plain JSON. Money stored as TEXT, always.

        ``kv`` rather than a new table: 10 BNB is 10**19 base units, which does not fit in
        a SQLite INTEGER, and adding a migration would mean editing a file this change does
        not own.
        """
        row = fetch_one(_conn(conn), "SELECT value FROM kv WHERE key = ?", (self._key(chain),))
        if row is None:
            return {}
        loaded = jload(row["value"], {})
        return loaded if isinstance(loaded, dict) else {}

    def _save(self, chain: Chain, state: dict[str, Any], conn: sqlite3.Connection | None) -> None:
        _conn(conn).execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
            (self._key(chain), jdump(state), self._clock()),
        )

    def rebaseline(
        self, chain: Chain, config_base_units: int, conn: sqlite3.Connection | None = None,
        *, at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Anchor the ledger to the configured number as of now, discarding history.

        Called automatically whenever ``bankroll_base_units`` changes in ``config/risk.yaml``.
        That is the operator saying "this is what the account is worth today", which is
        also the only sane moment to reset: they retype that number after depositing,
        withdrawing, or reconciling, and counting PnL from before the change would apply
        yesterday's wins to today's balance twice.
        """
        now = self._clock() if at_ms is None else int(at_ms)
        state = self.state(chain, conn)
        state.update(
            {
                "baseline_config_base_units": str(int(config_base_units)),
                "baseline_ms": now,
                # The growth clamp re-anchors too: a config change is deliberate operator
                # intent and must take effect at once, not over the next hour.
                "accepted_equity_base_units": str(int(config_base_units)),
                "accepted_ms": now,
            }
        )
        self._save(chain, state, conn)
        return state

    # ------------------------------------------------------------------ the ledger term

    def realized_since(
        self, chain: Chain, since_ms: int, conn: sqlite3.Connection | None = None
    ) -> int:
        """Signed realised PnL in base units from closed trades at or after ``since_ms``.

        ``trades`` and not ``positions``: accounting writes exactly one ``trades`` row per
        closed position, inside the same transaction that closes it, so there is nothing
        to double count. Filtered to :data:`COMPOUNDING_MODE` -- paper fills are recorded
        with the same columns and a simulated +219% must never raise a real bet.

        Partial exits on a still-open position are not counted here. They are already
        reflected in :meth:`RiskGate.open_exposure`, which nets proceeds against cost, so
        counting them again would be double counting. The residual case -- a partial that
        has already returned more than the whole cost -- is left uncounted until the
        position closes, which understates equity. Conservative, and deliberate.
        """
        rows = fetch_all(
            _conn(conn),
            "SELECT pnl_native FROM trades WHERE chain = ? AND mode = ? AND closed_ms >= ?",
            (chain.value, self._mode.value, int(since_ms)),
        )
        return sum(_as_int(r["pnl_native"]) for r in rows)

    # ------------------------------------------------------------------ the on-chain cap

    def _read(
        self, chain: Chain, wallet: str | None, state: dict[str, Any], conn: sqlite3.Connection | None
    ) -> tuple[int | None, str, int | None, str]:
        """``(base_units | None, basis, age_ms | None, cause)``.

        Never raises and never returns 0 to mean "we could not look". The caller only ever
        gets an integer when somebody actually observed the chain; every other outcome is
        ``None`` plus a basis that says which kind of nothing it is.
        """
        now = self._clock()
        cached = state.get("onchain_base_units")
        cached_ms = state.get("onchain_read_ms")
        cached_units = _as_int(cached, -1) if cached is not None else -1
        cached_age = (now - int(cached_ms)) if cached_ms is not None else None
        have_cache = cached_units >= 0 and cached_age is not None and cached_age >= 0

        if have_cache and cached_age is not None and cached_age < self._fresh_ms:
            return cached_units, BankrollBasis.CACHED, cached_age, "ttl"

        if self._provider is None:
            cause = BankrollBasis.UNWIRED
        elif not wallet:
            cause = BankrollBasis.NO_WALLET
        else:
            try:
                payload = self._provider(chain, wallet)
            except Exception as exc:  # noqa: BLE001 - a provider must never stop sizing
                log.warning("bankroll balance read failed for %s: %s", chain.value, exc)
                payload = None
            reading = parse_native_balance(chain, payload, wallet=wallet)
            if reading.ok:
                assert reading.base_units is not None
                state["onchain_base_units"] = str(reading.base_units)
                state["onchain_read_ms"] = now
                state["onchain_note"] = reading.note
                self._save(chain, state, conn)
                return reading.base_units, BankrollBasis.FRESH, 0, "read"
            log.info("bankroll balance unavailable for %s: %s", chain.value, reading.note)
            cause = BankrollBasis.UNAVAILABLE

        # Guard 2 and guard 4 in one place. Nothing current, so hold the last thing we
        # actually saw -- as a cap that may bind downward, and frozen against any rise.
        # An outage therefore changes nothing about the size; it only stops it growing.
        if have_cache and cached_age is not None:
            if cached_age <= self._hold_ms:
                return cached_units, BankrollBasis.HELD, cached_age, cause
            return None, BankrollBasis.EXPIRED, cached_age, cause
        return None, cause, cached_age, cause

    # ------------------------------------------------------------------ the answer

    def reading(
        self,
        chain: Chain,
        budget: ChainBudget,
        open_exposure_base_units: int,
        conn: sqlite3.Connection | None = None,
    ) -> BankrollReading:
        """The live bankroll for this chain. Pure of side effects except the cache write."""
        now = self._clock()
        config = int(budget.bankroll_base_units)
        exposure = max(0, int(open_exposure_base_units))
        reserve = max(0, int(budget.gas_reserve_base_units))
        findings: list[str] = []

        state = self.state(chain, conn)
        baseline_cfg = state.get("baseline_config_base_units")
        if baseline_cfg is None or _as_int(baseline_cfg, -1) != config:
            state = self.rebaseline(chain, config, conn, at_ms=now)
            findings.append("bankroll_rebaselined_to_config")
        baseline_ms = int(state.get("baseline_ms", now))

        if config <= 0:
            # Unfunded is not unlimited, and it is not a thing to compound either.
            return BankrollReading(
                chain=chain, equity_base_units=0, free_base_units=0, config_base_units=config,
                realized_base_units=0, open_exposure_base_units=exposure,
                onchain_base_units=None, onchain_basis=BankrollBasis.UNWIRED,
                onchain_age_ms=None, binding="config_unfunded",
                findings=(*findings, "bankroll_config_unfunded"),
            )

        realized = self.realized_since(chain, baseline_ms, conn)
        ledger = config + realized
        findings.append(f"bankroll_realized_since_baseline:{realized}")

        onchain, basis, age, cause = self._read(chain, budget.wallet, state, conn)
        findings.append(f"bankroll_onchain_basis:{basis}")
        if cause != basis:
            findings.append(f"bankroll_onchain_cause:{cause}")
        if onchain is None:
            # Guard 2. No observation of the chain, so the cap is the operator's own
            # authorisation. Wins cannot push us past a number nobody has verified; losses
            # still shrink us, because `ledger` already carries them.
            cap = config
            binding = "config_cap"
        else:
            cap = onchain + exposure
            binding = "onchain_cap"
            findings.append(f"bankroll_onchain:{onchain}")
        if age is not None:
            findings.append(f"bankroll_onchain_age_ms:{age}")

        equity = min(ledger, cap)
        if ledger < cap:
            # Reported only when the ledger is strictly the smaller of the two, so that a
            # cap sitting exactly on the ledger still shows up as a cap. A tie means the
            # ceiling is touching, and that is the interesting fact.
            binding = "ledger"

        # Guard: growth clamp. Falls pass through untouched.
        accepted = _as_int(state.get("accepted_equity_base_units"), -1)
        if accepted >= 0 and equity > accepted:
            if basis in BankrollBasis.NO_GROWTH:
                # Guard 2, the second half: "we could not look" holds and refuses to grow.
                ceiling = accepted
                clamp = "hold_no_growth"
            else:
                # ``config`` floors the ceiling so that one bad low reading cannot pin the
                # bankroll under the operator's own authorised number for several reads.
                # It can never let equity exceed the ledger: this is a ceiling, and
                # ``equity`` was already ``min(ledger, cap)`` before it.
                ceiling = max(int(Decimal(accepted) * BANKROLL_MAX_GROWTH_FACTOR_PER_READ), config)
                clamp = "growth_clamp"
            if equity > ceiling:
                equity = ceiling
                binding = clamp
                findings.append(f"bankroll_growth_clamped_to:{ceiling}")

        equity = max(0, equity)

        if equity != accepted:
            state["accepted_equity_base_units"] = str(equity)
            state["accepted_ms"] = now
            self._save(chain, state, conn)

        # Guard 3. Deployed capital is not spendable, and neither is the gas reserve.
        #
        # This single line also delivers the literal form of guard 1 -- "never more than
        # the wallet holds, minus the reserve" -- and it is worth writing down why, since
        # an earlier draft repeated the clamp here and no test could tell the two copies
        # apart. When the chain was readable, ``cap = onchain + exposure`` and every step
        # above only ever lowered ``equity``, so::
        #
        #     free = equity - exposure - reserve
        #          <= (onchain + exposure) - exposure - reserve
        #          =  onchain - reserve
        #
        # The ``exposure`` term cancels exactly because it is the same integer in both
        # places. The invariant is held by the property test over a spread of wallet,
        # ledger and exposure states rather than by a second copy of the arithmetic:
        # tests/test_compounding_bankroll.py::
        # test_free_is_never_more_than_the_chain_can_pay_whatever_else_is_true.
        free = max(0, equity - exposure - reserve)

        return BankrollReading(
            chain=chain,
            equity_base_units=equity,
            free_base_units=free,
            config_base_units=config,
            realized_base_units=realized,
            open_exposure_base_units=exposure,
            onchain_base_units=onchain,
            onchain_basis=basis,
            onchain_age_ms=age,
            binding=binding,
            findings=tuple(findings),
        )



#: Why the last ``position_size`` for a (chain, lane, token) came out zero, kept on the
#: database the sizer read. A side channel, because ``position_size`` returns a bare int
#: to many callers and the engine reaches ``check_entry`` on a *different* ``RiskGate``
#: instance (it builds one per call), so instance state cannot carry it; and on the
#: database rather than in the process, because sqlite connections cannot be weakly
#: referenced and a dict keyed on ``id(conn)`` labelled one database's refusal with
#: another's cause the moment an id was reused. MEASURED 2026-09-22 on the live box: 28 of
#: the 51 decisions in one half hour were refused as ``size_not_positive`` -- seven
#: distinct causes (lane off, chain not in lane, bankroll zero, score below the ladder,
#: exposure cap full, no viable band, remainder below the minimum) behind one word,
#: unreadable off the decisions table. The sizer knew; the row did not say.
ZERO_SIZE_CAUSE_TTL_MS = 10_000  # the sizer and the gate run inside one engine pass
_ZERO_SIZE_KEY = "risk:zero_size:{chain}:{lane}:{token}"


def _zero_size_keys(chain: Chain, lane: Lane, token: str | None) -> list[str]:
    keys = [_ZERO_SIZE_KEY.format(chain=chain.value, lane=lane.value, token=token or "*")]
    if token:
        keys.append(_ZERO_SIZE_KEY.format(chain=chain.value, lane=lane.value, token="*"))
    return keys


def note_zero_size(conn: Any, chain: Chain, lane: Lane, token: str | None, cause: str) -> int:
    """Record why a size is zero and return 0, so ``return note_zero_size(...)`` reads.

    Never raises and never changes the answer: a read-only connection or a missing table
    costs the explanation, not the refusal. Connections are autocommit (``db.connect``).
    """
    ts = now_ms()
    try:
        c = conn or get_conn()
        for key in _zero_size_keys(chain, lane, token):
            c.execute(
                "INSERT INTO kv (key, value, updated_ms) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ms=excluded.updated_ms",
                (key, jdump({"cause": cause, "ts": ts}), ts),
            )
    except Exception as exc:  # noqa: BLE001 - bookkeeping never moves a size
        log.debug("could not record zero-size cause %r: %s", cause, exc)
    return 0


def zero_size_cause(conn: Any, chain: Chain, lane: Lane, token: str | None = None) -> str | None:
    """The cause the sizer recorded on this database for (chain, lane[, token]), if fresh."""
    ts = now_ms()
    try:
        c = conn or get_conn()
        for key in _zero_size_keys(chain, lane, token):
            row = fetch_one(c, "SELECT value FROM kv WHERE key = ?", (key,))
            if row is None:
                continue
            data = jload(row["value"], {})
            if isinstance(data, dict) and ts - int(data.get("ts") or 0) <= ZERO_SIZE_CAUSE_TTL_MS:
                return str(data.get("cause") or "") or None
    except Exception as exc:  # noqa: BLE001 - an unreadable explanation is no explanation
        log.debug("could not read zero-size cause: %s", exc)
    return None


# --------------------------------------------------------------------------------------
# drawdown -> size multiplier  (OWNER 2026-09-23: "if got stoploss next day downsize
# until recoup")
#
# DAY-BOUNDARIED, which is what was asked for and also what makes it safe: it reads days
# that are CLOSED. An intraday drawdown does not shrink the next trade in the same session,
# so a run of stops cannot spiral the book to nothing inside an hour, and a position that
# is merely open and marked down cannot trigger it.
#
# "RECOUP" IS A HIGH-WATER MARK, not the last day's number. A book that made +5, lost -3
# and then made +1 is still 2 below its best and still trades reduced. Restoring full size
# on any green day would not be recouping, it would be forgetting.
#
# The drawdown is expressed as a FRACTION OF BANKROLL so one rung means the same thing on
# a 4.5 SOL book and a 0.39 ETH one.
#
# INVENTED. No study prices "size after a drawdown" on this tape; this is the owner's
# instruction, expressed as the gentlest ladder that still obeys it. It never reaches zero:
# a zero multiplier is a halt wearing a sizer's clothes, and halting is `risk_state`'s job,
# which says so out loud.

#: drawdown as a fraction of bankroll (exclusive upper bound) -> size multiplier.
RECOVERY_LADDER: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0.05"), Decimal("1.00")),
    (Decimal("0.10"), Decimal("0.80")),
    (Decimal("0.20"), Decimal("0.60")),
    (Decimal("0.35"), Decimal("0.45")),
)
#: Past the last rung. Still trading, still smaller.
RECOVERY_BELOW_LADDER = Decimal("0.35")


def recovery_multiplier(
    drawdown_base_units: int, bankroll_base_units: int
) -> tuple[Decimal, str]:
    """Size multiplier for a book that is below its high-water mark.

    A negative drawdown is a NEW high-water mark, never a bonus: the multiplier is capped
    at 1.0. An unknown or non-positive bankroll returns 1.0 rather than shrinking, because
    a reduction is a claim that we lost money and an unreadable book is not that claim.
    """
    try:
        dd = int(drawdown_base_units)
        bank = int(bankroll_base_units)
    except (TypeError, ValueError):
        return Decimal(1), "recovery:unreadable"
    if bank <= 0 or dd <= 0:
        return Decimal(1), "recovery:none"
    fraction = Decimal(dd) / Decimal(bank)
    for edge, mult in RECOVERY_LADDER:
        if fraction < edge:
            return mult, f"recovery:dd{fraction * 100:.1f}%"
    return RECOVERY_BELOW_LADDER, f"recovery:dd{fraction * 100:.1f}%"


def prior_day_drawdown(
    chain: Chain, conn: sqlite3.Connection | None = None, *, today: str | None = None
) -> tuple[int, int]:
    """``(drawdown, high_water)`` in base units, from days STRICTLY BEFORE ``today``.

    Walks the closed days in order, accumulating realised P&L and tracking the best
    cumulative total ever reached. The drawdown is how far below that best the book
    currently sits; zero means it has recouped.

    Never raises and never invents: a row we cannot parse contributes nothing, and no
    history at all is no drawdown.
    """
    cutoff = today or day_key()
    try:
        rows = fetch_all(
            conn if conn is not None else get_conn(),
            "SELECT day_key, realized_native_json FROM risk_state WHERE day_key < ? "
            "ORDER BY day_key",
            (cutoff,),
        )
    except sqlite3.Error:
        return 0, 0
    cumulative = 0
    high_water = 0
    for row in rows:
        blob = jload(row["realized_native_json"], {}) or {}
        if not isinstance(blob, dict):
            continue
        try:
            cumulative += int(blob.get(chain.value) or 0)
        except (TypeError, ValueError):
            continue
        high_water = max(high_water, cumulative)
    return max(0, high_water - cumulative), high_water


# --------------------------------------------------------------------------------------
# launch-wave concentration -> size multiplier
# --------------------------------------------------------------------------------------
#
# The operator's policy, in his words: *"if its bundled dev buying more than 20% 30% we
# can still buy but we need to be careful"*. So concentration is **not a veto**. It is a
# multiplier on a size that every other clamp has already agreed to, and it only ever
# moves the size down.
#
# **What is measured.** ``token_bundles`` (migration 024) stores, per token, the share of
# supply taken inside the launch window by wallets other than the creator:
# ``bundled_pct`` (same-slot contiguous groups of >= 2 entities) plus ``sniped_pct``
# (launch-window buys outside any such group). They are disjoint by construction, share
# one denominator, and are written together or not at all -- MEASURED on the live box
# 2026-09-22: of 87 ``coverage='measured'`` rows, 29 have both NULL and 0 have one
# without the other. The creator's own buy is a third role and is deliberately excluded:
# it has a 98.7% base rate (``docs/EDGE-AND-VARIABLES.md`` §4 #16) and ``dyor``'s
# ``dev_concentration`` blocker already owns it.
#
# Both arms are summed because either pair of hands can dump on us and the operator's
# 20/30% is about supply in somebody else's hands, not about the block-ordering trick
# used to get it. MEASURED (n=58 rows with a supply basis): summing moves the corpus from
# {<20: 50, 20-35: 4, 35-50: 2, >=50: 2} to {<20: 49, 20-35: 3, 35-50: 3, >=50: 3}, and
# the worst token moves from 29.469% bundled to 72.875% of supply once its 43.405% of
# snipers is counted -- two bands, on one number the bundler arm alone called mid-range.
#
# **The numbers below are INVENTED.** They are a policy, not a measurement: nothing in
# our data says a 37%-bundled launch is worth exactly half a position. What is measured
# is the shape of the population they act on (live box, 2026-09-22):
#
#   * ``token_bundles``: 87 measured rows against 6,337 unavailable (1.35% measured).
#     5,789 of the refusals are "the tape for this mint was never pulled".
#   * Launch wave over the 58 measured rows that have a supply basis: median 0.044%,
#     p75 5.991%, p90 26.563%, max 72.875%; 49 under 20%, 9 at or above it.
#   * Of 78 ENTER decisions this agent has ever taken, 76 (97.4%) had no usable
#     measurement at the moment it sized them. The two that did measured 0.300% and
#     13.190% -- both 1.0x.
#
# **How much discount the book can actually carry, which is what set these rungs.**
# MEASURED on the 52 ENTER tokens whose pool can still be priced: every one of them was
# sized between 1.022x and 1.789x its own economic floor (deciles 1.139 / 1.207 / 1.218 /
# 1.263 / 1.515), because the ladder wants less than the pool allows and
# ``_clamp_to_band`` lifts it to the floor. The book is piled up at ~1.22x, so the
# survivor curve has a cliff, not a slope -- entries still tradable after the multiplier,
# against both the floor and the 45,000,000 chain minimum:
#
#     x1.00 52/52   x0.95 51/52   x0.90 46/52   x0.85 45/52
#     x0.80 11/52   x0.75 11/52   x0.60  5/52   x0.50  0/52
#
# So at a 4.5 SOL sol bankroll the size lever has about 18% of travel and the operator's
# 0.5x proposal is a **veto** by another name: it refuses 52 of 52. He explicitly ruled
# a veto out for the 20-30% band, so the careful rung is 0.85 -- the deepest discount
# that still lets the book trade -- and the deeper rungs stay where a refusal is the
# intended answer. The rungs express the policy, not today's bankroll: the same 0.5x that
# refuses everything at 4.5 SOL is a real half-position once the bankroll clears roughly
# 2x the pool floor per entry.
#
# **What "careful" cannot buy here.** 15% off a position is not protection. Between 20%
# and 35% the real lever is ``kaiba/execution/protection.py`` -- a tighter stop and an
# earlier first take-profit -- and that file belongs to another task. Sizing says so
# rather than pretending the multiplier is the answer.
#
# What would settle these numbers: the forward PnL of our own fills bucketed by measured
# wave share. ``bundles.rug_separation`` is the query; the sample is nowhere near it yet
# (9 tokens at or above 20% in the whole corpus, 2 in anything we traded).
#
#: pct of supply taken in the launch window (exclusive upper bound) -> size multiplier.
#: INVENTED (see above). Strictly non-increasing, and every value <= 1.0, because this
#: mechanism exists to make a position smaller and must never be able to make one larger.
#: How far BELOW the chain minimum a pool-clamped size may land and still be rounded
#: up to it. The pool band and the chain floor come from different inputs -- pool
#: depth versus the operator's "never trade dust" -- so they land a fraction apart
#: routinely. MEASURED 2026-09-24: a live sol entry was refused at 555,524,237
#: against a floor of 560,000,000, which is 99.2% of it, vetoed over 0.8%.
#:
#: 10% is INVENTED, and deliberately small. It bounds how far a size may exceed what
#: the pool band allowed; beyond it the refusal stands, because a pool that can carry
#: only half the floor is a real veto and must keep saying so.
MIN_POSITION_NEAR_MISS: Decimal = Decimal("0.10")


CONCENTRATION_LADDER: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("20"), Decimal("1.0")),
    (Decimal("35"), Decimal("0.85")),
    (Decimal("50"), Decimal("0.5")),
)
#: Applied at or above the last rung. INVENTED.
CONCENTRATION_ABOVE_LADDER = Decimal("0.1")
#: What an UNMEASURABLE launch is worth. **INVENTED, and the load-bearing choice here,**
#: because unknown is not the edge case: it is 76 of our 78 entries.
#:
#: It is not 1.0x. MEASURED: 76 of this agent's 78 ENTER decisions (97.4%) had no usable
#: measurement, so an unknown worth full size is a mechanism that has never once fired
#: and never would -- fail-open in exactly the population it was built for.
#:
#: It is not 0, and it is not 0.5 either. The operator explicitly buys tokens one second
#: old; at t+1s the launch wave has not happened yet, so "unavailable" is the *normal*
#: state of his best trade rather than a red flag. And 0.5x refuses 52 of 52 priced
#: entries outright (see the survivor curve above), which would close the lane on our own
#: ingestion gap: 5,789 of the 6,337 refusals are "the tape for this mint was never
#: pulled", a fact about our collection, not about the token.
#:
#: 0.9 is where two independent lines meet. Priced risk-neutrally against our own corpus
#: (n=58 measured rows, 49/3/3/3 across the four rungs) they imply
#: 0.845*1.0 + 0.052*0.85 + 0.052*0.5 + 0.052*0.1 = 0.920x; and 0.90 is the deepest
#: discount that still leaves the book trading
#: (46 of 52, against 11 of 52 at 0.80). Both say the same thing: roughly a tenth off.
#:
#: What that tenth does *not* cover, stated so nobody mistakes it for cover. GIVE
#: (12qeY9vz1uZHZjWtQPuRfmJtMidPXg9mU1CY4Mpkygiv), re-measured from GMGN's trader tape on
#: 2026-09-22, n=100 traders: 5 wallets held 23.695% of supply by t+1s, 16 held 36.693%
#: by t+5s, 43 held 59.418% by t+34s, 31 carry GMGN's own ``bundler`` tag for 50.550%,
#: and 93 of 100 have fully exited. At the operator's stated entry age the true
#: concentration was already in his own careful band, and this sizer will have priced it
#: at 0.9x because nothing in our tape could see it. A tenth off a position is not
#: protection from that; a stop is. This number buys honesty about the unknown and a
#: standing incentive to close the tape gap -- not safety.
#:
#: What would settle it: our own fills bucketed by the wave share a *relay-aware*
#: detector measures. Until one exists this stays a judgement, and it is one line of
#: ``config/risk.yaml`` for the operator to move.
CONCENTRATION_UNKNOWN = Decimal("1.0")
#
# 2026-09-22: this was 0.9, and 0.9 was well argued -- 76 of 78 ENTER decisions (97.4%)
# carry no usable measurement, so 1.0 means the mechanism almost never fires, and 0.9 was
# the corpus-implied risk-neutral value (0.920, n=58).
#
# It is 1.0 now for two MEASURED reasons.
#
# 1. It was not a haircut, it was a veto. Once the lane band was narrowed to clear sol's
#    economic floor (0.045 SOL = ~1.04% of equity), a 0.9x on an unmeasured token landed
#    under the per-token VIABLE floor: 4 of 6 live sol candidates refused with
#    `concentration:unknown`, 0 of 6 sized. The operator's rule is "we can still buy but
#    we need to be careful"; refusing every entry is not that.
# 2. There is no measured basis for charging it. The concentration outcome study found
#    the sign of concentration -> forward return FLIPS between consecutive days (-0.208,
#    then +0.197) and vanishes at partial rho +0.004 once wallet count is controlled for.
#    Charging 10% for not knowing a number that does not predict the outcome is a tax on
#    ignorance, not on risk.
#
# The MEASURED ladder is untouched: a token we CAN read at >= 20% still takes the
# operator's haircut, and >= 50% still refuses. This changes only what we do when we
# cannot see. It lives in the CODE rather than the config because `save_risk` rewrites
# config/risk.yaml from the pydantic model and `EnvelopeBounds` does not declare the
# `concentration:` block -- MEASURED on the live box, which lost the whole block and all
# 24 of its comments to exactly that. The code default is the one that survives.


# --------------------------------------------------------------------------------------
# dev supply -> size multiplier
# --------------------------------------------------------------------------------------
#
# The same operator sentence, the other half of it: *"if its bundled dev buying more than
# 20% 30% we can still buy but we need to be careful"*. On 2026-09-22
# `kaiba.intelligence.dyor.DEV_PCT_BLOCK` was raised from 10 to 30, which implements "we
# can still buy". This implements "carefully", and without it that raise is a widening
# with nothing on the other side of it.
#
# **Why this is a second mechanism and not the one above.** The ladder above prices the
# launch WAVE -- supply taken at launch by wallets **other than the creator**; the
# creator's own buy is excluded from it by construction (see `_stored_bundle_wave_pct`
# and the `token_bundles` definition). So `dev_pct` fed nothing in this file at all:
# MEASURED, a token whose creator held 25% was sized exactly like one whose creator held
# 2%, and once the veto moved the only thing left holding it down was gone.
#
# **What is measured.** `token_dossiers.dossier_json` -> `dev_pct`, the dossier's own
# creator-supply share (RugCheck `creatorBalance`, GoPlus `creator_percent`, GMGN
# `dev_token_amount_rate`). Coverage is the opposite of the wave's: MEASURED ON THE LIVE
# BOX 2026-09-22, 9,809 dossiers carry a dev_pct and every one of the 546 decisions the
# dev veto refused has one, against 1.35% coverage for `token_bundles`. This arm therefore
# fires on nearly everything it is asked about, which is exactly why its rungs are
# shallow.
#
# **The rungs are INVENTED; the constraint that shaped them is MEASURED.** From the
# survivor curve in the section above -- our own 52 priced ENTER tokens, every one sized
# 1.022-1.789x its own economic floor -- the entries still tradable after a multiplier:
#
#     x1.00 52/52   x0.95 51/52   x0.90 46/52   x0.85 45/52   x0.80 11/52   x0.50 0/52
#
# The book has about 18% of travel. 0.9 and 0.85 are the two deepest rungs that still let
# it trade (46 and 45 of 52); 0.8 falls off a cliff to 11 of 52 and would be a veto by
# another name in the band where the operator explicitly ruled a veto out. So the band is
# split at his own number: 10-20% -> 0.9, 20-30% -> 0.85. 10 is PLAN §5.5's superseded
# veto, kept as the first rung that costs anything rather than thrown away; 20 and 30 are
# the numbers he said out loud.
#
# **The distribution these rungs will actually meet**, MEASURED ON THE LIVE BOX
# 2026-09-22. Over the 546 all-time refusals the dev veto produced: 49 land in 10-20%
# (0.9x) and 29 in 20-30% (0.85x), so 78 of 546 (14.3%) are admitted at a haircut and 440
# (80.6%) stay refused. Over all 9,809 dossiers that carry a dev_pct: p50 0.35%, p90
# 13.79%, 58.1% under 1% -- the first rung bites at roughly the top decile, and the median
# token never meets this ladder at all.
#
# (First measured on `data/kaiba.db`, the LOCAL scratch copy, which said 6.3% and 28
# tokens. It is not a sample of production. Measure the box the money is on.)
#
# **What "careful" cannot buy here, restated because it applies twice as hard to this
# arm.** 10-15% off a position is not protection from a creator who holds a quarter of
# the float. The real lever between 10% and 30% is `kaiba/execution/protection.py` -- a
# tighter stop and an earlier first take-profit -- which belongs to another task. This
# says so rather than pretending the multiplier is the answer.
#
# What would settle the rungs: forward PnL of our own fills bucketed by `dev_pct`. The
# sample is empty today *because of the rule being changed here* -- every token above 10%
# was refused and therefore never priced -- so the first honest thing this change buys is
# the evidence to replace it.
#
#: pct of supply attributable to the CREATOR (exclusive upper bound) -> size multiplier.
#: INVENTED (see above). Strictly non-increasing, every value <= 1.0, same contract as
#: CONCENTRATION_LADDER: this mechanism exists to shrink a position and must never be able
#: to grow one.
DEV_SUPPLY_LADDER: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("10"), Decimal("1.0")),
    (Decimal("20"), Decimal("0.9")),
    (Decimal("30"), Decimal("0.85")),
    # 30-50% became admissible on 2026-09-23 when `dyor.DEV_PCT_BLOCK` moved to 50 on a
    # 7,738-token population study (that constant carries the numbers). It is sized BELOW
    # the 20-30% rung on purpose: the study measures how often a token doubles on the tape,
    # which is not how often WE make money on it -- we exit on a ladder and pay ~6.5% round
    # trip -- and a creator holding a third of the float remains a tail that a median
    # cannot price.
    #
    # The owner's call (2026-09-23) was to admit the band with NO EXTRA penalty, against a
    # proposed 0.6x. It is 0.85 and not 1.0 because the ladder must stay monotonic: a
    # literal 1.0 would size a 30-50% token ABOVE a 20-30% one, which inverts the only
    # thing this ladder exists to express. 0.85 carries no penalty beyond the rung below.
    (Decimal("50"), Decimal("0.85")),
)
#: Applied at or above the last rung, which is `dyor.DEV_PCT_BLOCK` itself. INVENTED.
#:
#: In the live path this should be unreachable: `engine.decide` refuses a dossier carrying
#: the `dev_concentration` blocker long before a size exists, so anything ABOVE 30% never
#: gets here. It is a backstop for the day somebody retunes that rule -- "declared is not
#: enforced" is this repo's most expensive lesson, so the second place that could stop the
#: trade also stops it. At the live sol bankroll 0.1x is a refusal (0 of 52 priced entries
#: survive it), and it refuses NAMING the dev supply instead of silently.
#:
#: One knife edge, stated rather than papered over: `dyor` blocks on `_over`, strictly
#: greater, so a dev_pct of exactly 30 is admitted there and lands on this rung here. The
#: two disagree on one exact value and the sizer is the tighter of the two, which is the
#: safe direction to disagree in.
DEV_SUPPLY_ABOVE_LADDER = Decimal("0.1")
#: What an UNMEASURABLE creator share is worth. 1.0, and deliberately the same value and
#: the same reasoning as CONCENTRATION_UNKNOWN above -- read that comment; both would have
#: to move together.
#:
#: Two reasons it is not a haircut. First the MEASURED one: a 0.9 on unknown was observed
#: refusing 4 of 6 live sol candidates outright, because at a 4.5 SOL bankroll every entry
#: sits 1.02-1.79x its own pool floor and a tenth off lands under it. Second, and specific
#: to this arm: an unmeasured `dev_pct` is a token `dyor`'s dev rules never fired on
#: either, so charging it here would tax a token nobody flagged, on a number that the
#: concentration outcome study could not show predicts the forward return in either
#: direction (rho flips sign between consecutive days, +0.004 partial).
#:
#: What it must NOT do is read as clean. The label always travels -- `dev_unknown:<why>`
#: is in every refusal and every log line this file writes -- so the share of the book
#: running on an unmeasured creator share stays visible. That is the whole discipline: the
#: number is honest about what we do not know, not protective against it.
#:
#: What would settle it: fills bucketed by whether dev_pct was measurable at entry.
DEV_SUPPLY_UNKNOWN = Decimal("1.0")


def _policy_from_block(
    block: Any,
    ladder: tuple[tuple[Decimal, Decimal], ...],
    above: Decimal,
    unknown: Decimal,
    *,
    what: str,
) -> tuple[tuple[tuple[Decimal, Decimal], ...], Decimal, Decimal]:
    """Parse one concentration policy block over its defaults. Pure; no I/O.

    Shared by :func:`_concentration_policy` and :func:`_dev_supply_policy` so the two
    cannot drift into different ideas of what a legal policy is. A non-monotone or
    widening ladder returns the defaults untouched and says so in the log, and garbage
    that cannot be parsed at all raises into the caller's handler, which does the same: a
    policy we cannot read is not the absence of a policy.
    """
    if not isinstance(block, dict):
        return ladder, above, unknown
    rungs: list[tuple[Decimal, Decimal]] = []
    for pair in block.get("ladder") or ():
        threshold, multiplier = pair  # a 2-list, as protection.tp_ladder is
        rungs.append((Decimal(str(threshold)), Decimal(str(multiplier))))
    if rungs:
        thresholds = [t for t, _ in rungs]
        multipliers = [m for _, m in rungs]
        ok = (
            all(a < b for a, b in zip(thresholds, thresholds[1:], strict=False))
            and all(a >= b for a, b in zip(multipliers, multipliers[1:], strict=False))
            and all(Decimal(0) <= m <= Decimal(1) for m in multipliers)
        )
        if not ok:
            log.warning("%s ladder in the risk file is not monotone; using the default", what)
            return ladder, above, unknown
        ladder = tuple(rungs)
    if "above_ladder_multiplier" in block:
        above = Decimal(str(block["above_ladder_multiplier"]))
    if "unknown_multiplier" in block:
        unknown = Decimal(str(block["unknown_multiplier"]))
    above = min(max(above, Decimal(0)), Decimal(1))
    unknown = min(max(unknown, Decimal(0)), Decimal(1))
    return ladder, above, unknown


def _raw_concentration_block() -> Any:
    """The ``concentration:`` mapping out of the risk file, or ``None``.

    Read raw rather than through :class:`RiskConfig` for the reason
    :func:`_concentration_policy` documents: ``EnvelopeBounds`` does not declare this block
    and ``save_risk`` writes the model back, so the file is an override and the code is the
    authority.
    """
    import os
    from pathlib import Path

    import yaml

    from kaiba.core.config import DEFAULT_RISK_PATH

    path = Path(os.environ.get("KAIBA_RISK_PATH", DEFAULT_RISK_PATH))
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = raw.get("concentration")
    if block is None and isinstance(raw.get("bounds"), dict):
        block = raw["bounds"].get("concentration")
    return block


def _dev_supply_policy() -> tuple[tuple[tuple[Decimal, Decimal], ...], Decimal, Decimal]:
    """``(ladder, above_ladder, unknown)`` for the CREATOR's share. Override or default.

    Lives under ``concentration.dev_supply`` in the risk file, so the operator can retune
    it in the same block as the wave arm. Every caveat on :func:`_concentration_policy`
    applies unchanged: the block is deleted by the first ``save_risk``, so the code default
    is the authority, and an unreadable or widening block falls back to it rather than to
    1.0.
    """
    ladder, above, unknown = DEV_SUPPLY_LADDER, DEV_SUPPLY_ABOVE_LADDER, DEV_SUPPLY_UNKNOWN
    try:
        block = _raw_concentration_block()
        if not isinstance(block, dict):
            return ladder, above, unknown
        return _policy_from_block(block.get("dev_supply"), ladder, above, unknown, what="dev supply")
    except Exception as exc:  # noqa: BLE001 - an unreadable policy is not a missing policy
        log.warning("could not read the dev supply policy (%s); using the default", exc)
        return DEV_SUPPLY_LADDER, DEV_SUPPLY_ABOVE_LADDER, DEV_SUPPLY_UNKNOWN


def _dossier_dev_pct(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal | None, str]:
    """The creator's share of supply from this token's dossier. ``(pct, why)``.

    ``(None, reason)`` is a real answer and never a 0: a creator share nobody could
    establish is not a creator share of nothing. The reason travels into the label so a
    refusal names what it could not see.

    The row is read here rather than through ``engine.load_dossier`` because ``engine``
    imports this module; same query, same model, same rule ``viability`` already follows
    for the tax.

    Freshness is deliberately NOT re-checked. ``engine.decide`` refuses a dossier older
    than ``DOSSIER_MAX_AGE_S`` before a size is ever asked for, so in the live path this
    is the same row the entry was already admitted on, and a second, stricter clock here
    would only turn admitted tokens into unknowns (1.0x) without changing a refusal. A
    caller that sizes outside that gate is trusting a dossier nobody re-read, which is a
    property of that caller and not something this function can fix.
    """
    try:
        from kaiba.core.schemas import TokenDossier

        row = fetch_one(
            conn if conn is not None else get_conn(),
            "SELECT dossier_json FROM token_dossiers WHERE chain = ? AND address = ?",
            (chain.value, token),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable dossier is an unknown, not a 0
        return None, f"dossier_unreadable:{type(exc).__name__}"
    if row is None:
        return None, "no_dossier"
    try:
        dossier = TokenDossier.model_validate_json(row["dossier_json"])
    except Exception as exc:  # noqa: BLE001 - a corrupt dossier is the same as no dossier
        return None, f"dossier_unparseable:{type(exc).__name__}"
    measure = getattr(dossier, "dev_pct", None)
    if measure is None or not getattr(measure, "known", False):
        return None, "dev_unmeasured"
    # The next two branches are the second line, not the first: ``TokenDossier`` types
    # ``dev_pct.value`` as ``Decimal | None`` and pydantic rejects NaN and Infinity
    # outright, so a garbage row fails validation above and comes back
    # ``dossier_unparseable``. They are kept because the failure they cover is silent
    # rather than loud -- a NaN compares False against every rung, so it would land on the
    # above-ladder multiplier by accident rather than by decision -- and because a model
    # that stops validating is a change nobody would make with this file in mind. Neither
    # is reachable through the stored model today and no test can kill them.
    try:  # pragma: no cover - unreachable while dev_pct is a typed Decimal
        pct = Decimal(str(measure.value))
    except (InvalidOperation, ValueError, TypeError):
        return None, "dev_unparseable"
    if not pct.is_finite():  # pragma: no cover - pydantic rejects NaN/Inf before this
        return None, "dev_not_finite"
    if pct < 0:
        return None, "dev_negative"
    return pct, f"{getattr(measure.basis, 'value', measure.basis)}"


def _dev_supply_multiplier(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal, str]:
    """``(multiplier, label)`` for the creator's share. Never above 1; only ever shrinks."""
    ladder, above, unknown = _dev_supply_policy()
    pct, detail = _dossier_dev_pct(chain, token, conn)
    if pct is None:
        return min(unknown, Decimal(1)), f"dev_unknown:{detail}"
    multiplier = above
    for threshold, value in ladder:
        if pct < threshold:
            multiplier = value
            break
    return min(multiplier, Decimal(1)), f"dev:{pct:.3f}%:{detail}"


def _concentration_policy() -> tuple[tuple[tuple[Decimal, Decimal], ...], Decimal, Decimal]:
    """``(ladder, above_ladder, unknown)``, the operator's override or the code default.

    Read raw from the risk file rather than through :class:`RiskConfig`, because
    ``EnvelopeBounds`` drops every key it does not declare and ``save_risk`` writes the
    model back: a typed home for this block belongs in ``kaiba/core/config.py``, which
    this task does not own. Two consequences, both handled here rather than hidden:

    * The block **disappears** the first time ``kaiba risk`` or a self-tune rewrites the
      file. So the code default is the authority and the file is an override -- a deleted
      block changes nothing, where a config-only policy would silently switch the
      mechanism off. (``docs/PLAN.md``: declared is not enforced.)
    * An **unparseable or widening** block falls back to the default too, and says so in
      the log. A policy we cannot read is not the absence of a policy.

    Values are clamped into ``[0, 1]`` and the ladder is required to be strictly
    increasing in threshold and non-increasing in multiplier, so no edit to the file can
    turn this into something that raises a size.
    """
    ladder, above, unknown = CONCENTRATION_LADDER, CONCENTRATION_ABOVE_LADDER, CONCENTRATION_UNKNOWN
    try:
        return _policy_from_block(
            _raw_concentration_block(), ladder, above, unknown, what="concentration"
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable policy is not a missing policy
        log.warning("could not read the concentration policy (%s); using the default", exc)
        return CONCENTRATION_LADDER, CONCENTRATION_ABOVE_LADDER, CONCENTRATION_UNKNOWN


def _relay_aware_pct(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal | None, str]:
    """``kaiba.intelligence.launch_concentration``'s headline, if it will give one.

    **The preferred source, because it is the only one that sees the evasion.** The
    stored ``token_bundles`` figure groups same-slot contiguous runs; the relay --
    buy, sell, re-buy the same supply through wallets that share no funding edge -- is
    neither contiguous nor entity-linked, and GIVE's 59.418% launch wave is invisible to
    it. The headline is the peak *simultaneous* net holding of the launch wave, the
    co-timed cohorts and the relay takeover, so it is bounded by supply and it is the
    quantity the operator's "more than 20% 30%" is actually about: how much somebody can
    dump on us at once.

    Imported inside the call, and every failure swallowed into an unknown, because that
    module is owned by another task and is being edited while this one runs. A sizer that
    died on its signature changing would stop the agent trading; a sizer that quietly
    prices its absence at 0.9x does not.
    """
    try:
        from kaiba.intelligence import launch_concentration

        report = launch_concentration.measure(chain, token, conn)
        if not report.measured:
            return None, f"launch_concentration:{getattr(report.gate, 'value', report.gate)}"
        value = report.headline_pct.value
        if value is None:
            return None, "launch_concentration:no_headline"
        return Decimal(str(value)), f"headline/{report.model_id}"
    except Exception as exc:  # noqa: BLE001 - an unavailable detector is an unknown, not a 0
        log.debug("launch concentration unavailable for %s: %s", token, exc)
        return None, f"launch_concentration:{type(exc).__name__}"


def _measured_launch_wave_pct(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal | None, str]:
    """Share of supply in non-creator hands at launch, from the best source that has one.

    Two sources, in order. :func:`_relay_aware_pct` first, because it is the one that
    survives the sell-and-rebuy relay. ``token_bundles`` second: it is a cruder,
    *cumulative* figure that recycled supply can push over 100%, but it is stored, it
    costs one row read, and having it is strictly better than pricing a token as unknown.
    The label says which one answered, so a refusal can be traced to a detector.

    ``(None, reason)`` is a real answer and the common one: a token we have not measured
    is **not** a token with 0% bundling. The reason travels with it so the refusal it
    eventually causes can name what we failed to see rather than shrugging.

    The stored denominator is whatever ``token_bundles.supply_basis`` recorded. On a
    pump.fun mint the curve holds 79.31% of supply at launch, so a curve-basis row reads
    1.26x larger than a supply-basis one; it is used as stored and the basis is carried
    into the label, because rescaling it here would be inventing a conversion the row did
    not claim. That error is in the tightening direction.
    """
    relayed, relay_detail = _relay_aware_pct(chain, token, conn)
    if relayed is not None:
        return relayed, relay_detail
    stored, stored_detail = _stored_bundle_wave_pct(chain, token, conn)
    if stored is not None:
        return stored, stored_detail
    return None, f"{relay_detail}+{stored_detail}"


def _stored_bundle_wave_pct(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal | None, str]:
    """``token_bundles.bundled_pct + sniped_pct`` for this token, or why there is none."""
    try:
        row = fetch_one(
            conn or get_conn(),
            "SELECT coverage, reason, bundled_pct, sniped_pct, supply_basis, model "
            "FROM token_bundles WHERE chain = ? AND token = ?",
            (chain.value, token),
        )
    except Exception as exc:  # noqa: BLE001 - a missing table is a missing measurement
        return None, f"unreadable:{type(exc).__name__}"
    if row is None:
        return None, "never_measured"
    coverage = str(row["coverage"] or "")
    if coverage != "measured":
        return None, f"coverage_{coverage or 'missing'}"
    bundled, sniped = row["bundled_pct"], row["sniped_pct"]
    if bundled is None or sniped is None:
        # Both are NULL together (0 of 87 live rows have one without the other): the
        # supply was not resolved, so there is no denominator and no percentage. The
        # stored share-of-launch-buys figure is deliberately NOT substituted -- it
        # answers a different question and would read ~100% on a quiet launch.
        return None, f"no_supply_basis:{row['supply_basis'] or 'unknown'}"
    try:
        wave = Decimal(str(bundled)) + Decimal(str(sniped))
    except (InvalidOperation, ValueError):
        return None, "unparseable_pct"
    if wave < 0:
        return None, "negative_pct"
    return wave, f"{row['supply_basis'] or 'unknown'}/{row['model'] or 'unknown'}"


def _concentration_multiplier(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal, str]:
    """``(multiplier, label)``. Never above 1: this mechanism only ever shrinks a size.

    Two arms, composed by taking the **deeper** haircut rather than multiplying them:
    the launch wave (supply taken at launch by everyone except the creator) and the
    creator's own share. Both labels always travel, so a refusal names both what it saw
    and what it could not.

    **Why the minimum and not the product.** Compounding two INVENTED ladders multiplies
    the invention, not the evidence, and it lands where the evidence says we cannot go:
    0.85 * 0.85 = 0.7225, and MEASURED on our own 52 priced entries only 11 survive a
    0.80x at all (see the survivor curve above). A product would therefore be a veto in
    the exact band where the operator ruled a veto out, arrived at by accident. The two
    quantities are disjoint by construction -- the wave excludes the creator -- so taking
    one of them is not ignoring a part of the other. What would settle it: our own fills
    bucketed jointly by wave share and dev share, which needs a sample neither arm has.
    """
    wave_multiplier, wave_label = _launch_wave_multiplier(chain, token, conn)
    dev_multiplier, dev_label = _dev_supply_multiplier(chain, token, conn)
    who_multiplier, who_label = _deployer_multiplier(chain, token, conn)
    flow_multiplier, flow_label = _early_flow_multiplier(chain, token, conn)
    multiplier = min(wave_multiplier, dev_multiplier, who_multiplier, flow_multiplier)
    return min(multiplier, Decimal(1)), f"{wave_label}+{dev_label}+{who_label}+{flow_label}"


#: A token whose first five minutes of tape are more SELLS than buys is one we would be
#: buying from a seller. MEASURED 2026-09-22 two ways, lookahead-free (features from
#: [t0, t0+300s], outcome strictly after):
#:
#:   population, n=1,689 sol tokens, baseline 22.4% reach 2x:
#:       buy fraction < 0.45  ->  13.4% reach 2x   (0.60x the baseline rate, 15% of the sample)
#:       buy fraction >= 0.45 ->  24.0%            (1.07x)
#:   our own closed live fills, n=50, mean -20.6%:
#:       buy fraction < 0.45  ->  mean -36.5%  (13 trades)
#:       buy fraction >= 0.45 ->  mean -15.0%  (37 trades)
#:
#: The DIRECTION replicates in both samples. The MAGNITUDE does not -- our 50 fills show a
#: far bigger gap than the population, which is what a 50-sample does. So the threshold is
#: taken from the large sample and the charge is sized to the large sample's effect, not to
#: the one that would be most flattering.
#:
#: DELIBERATELY NOT A VETO. A 1.07x lift does not justify refusing 15% of candidates
#: outright, and this module's operator ruled out vetoes at this kind of effect size.
#:
#: What was REJECTED, and why it matters: the same study first proposed pairing this with
#: "price ran less than 1.05x in the first 5 minutes". On the population that looked like a
#: clean avoid (13.7% vs 22.5%). On our own fills it excluded 2 trades averaging **+5.3%** --
#: it was avoiding WINNERS. It is not here. A population result that reverses on our own
#: fills is not a rule.
EARLY_BUY_FRACTION_FLOOR = Decimal("0.45")
EARLY_FLOW_MULTIPLIER = Decimal("0.80")
EARLY_FLOW_WINDOW_S = 300
#: Below this many priced prints in the window there is no flow to read, and "no evidence"
#: is charged nothing -- the same rule every other arm here keeps.
EARLY_FLOW_MIN_PRINTS = 5


def _early_flow_multiplier(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal, str]:
    """``(multiplier, label)`` for the buy/sell balance of the token's first minutes."""
    if conn is None:
        return Decimal(1), "flow:no_conn"
    try:
        row = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buys, "
            "MIN(ts_ms) AS t0 FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL",
            (chain.value, token),
        )
        if row is None or not row["t0"]:
            return Decimal(1), "flow:no_tape"
        window = fetch_one(
            conn,
            "SELECT COUNT(*) AS n, SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buys "
            "FROM swaps WHERE chain=? AND token=? AND price_usd IS NOT NULL "
            "AND ts_ms BETWEEN ? AND ?",
            (chain.value, token, int(row["t0"]), int(row["t0"]) + EARLY_FLOW_WINDOW_S * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable tape charges nothing
        log.debug("early flow unavailable for %s: %s", token, exc)
        return Decimal(1), "flow:unavailable"
    if window is None:
        return Decimal(1), "flow:no_window"
    n = int(window["n"] or 0)
    if n < EARLY_FLOW_MIN_PRINTS:
        return Decimal(1), f"flow:too_few_prints({n})"
    fraction = Decimal(int(window["buys"] or 0)) / Decimal(n)
    if fraction < EARLY_BUY_FRACTION_FLOOR:
        return EARLY_FLOW_MULTIPLIER, f"flow:buyfrac{fraction:.2f}"
    return Decimal(1), f"flow:buyfrac{fraction:.2f}"


#: Deployer record -> size multiplier. DERIVED 2026-09-22 from our own tape (sol, 7 days,
#: 3,062 attributed tokens); the full table is in :mod:`kaiba.intelligence.deployer`.
#: Baseline over that set is 13.2% of launches reaching 2x. By bucket:
#:
#:     spam(51+)/all_dud    5.3%   0.40x the baseline rate
#:     mid(11-50)/all_dud   8.2%   0.62x
#:     low(1-10)/all_dud   11.5%   0.87x
#:     */no_prior         12.7-15.4%  at baseline
#:     */runner           12.6-19.2%  at or above baseline
#:
#: The multipliers here are deliberately SHALLOWER than those rate ratios. Charging the raw
#: 0.40x would make the worst bucket a veto in all but name: this module's own survivor
#: curve (see CONCENTRATION_LADDER) measured that only 11 of 52 priced entries clear their
#: economic floor after a 0.80x at all, and the operator ruled out a veto here. So the two
#: buckets the measurement separates cleanly are charged, and nothing else is.
#:
#: Nothing in this table is above 1. A deployer with a prior runner reads 17-19% against a
#: 13.2% baseline, which is a real lift -- but this mechanism only ever shrinks, and the
#: cells behind that lift are 47 and 125 tokens. Sizing UP on 47 observations would be
#: inventing conviction, and the place to take more risk is the lane's size band, which is
#: an operator setting backed by the lane's own measured expectancy.
DEPLOYER_LADDER: dict[str, Decimal] = {
    "spam/all_dud": Decimal("0.70"),
    "mid/all_dud": Decimal("0.85"),
}

#: An unmeasured deployer is charged nothing, exactly as an unmeasured concentration is.
#: We cannot tell a fresh-wallet dev from a serial rugger, and guessing which costs either
#: every clean launch or every dirty one.
DEPLOYER_UNKNOWN = Decimal(1)


def _deployer_multiplier(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal, str]:
    """``(multiplier, label)`` for who shipped this token. See :data:`DEPLOYER_LADDER`."""
    try:
        from kaiba.intelligence import deployer as who

        record = who.lookup(conn, chain, token) if conn is not None else who.UNKNOWN
    except Exception as exc:  # noqa: BLE001 - an unreadable record charges nothing
        log.debug("deployer record unavailable for %s: %s", token, exc)
        return DEPLOYER_UNKNOWN, "deployer:unavailable"
    label = record.label
    if not record.known:
        return DEPLOYER_UNKNOWN, f"deployer:{label}"
    return DEPLOYER_LADDER.get(label, Decimal(1)), f"deployer:{label}"


def _launch_wave_multiplier(
    chain: Chain, token: str, conn: sqlite3.Connection | None
) -> tuple[Decimal, str]:
    """``(multiplier, label)`` for the launch wave alone. See ``CONCENTRATION_LADDER``."""
    ladder, above, unknown = _concentration_policy()
    wave, detail = _measured_launch_wave_pct(chain, token, conn)
    if wave is None:
        return min(unknown, Decimal(1)), f"unknown:{detail}"
    multiplier = above
    for threshold, value in ladder:
        if wave < threshold:
            multiplier = value
            break
    return min(multiplier, Decimal(1)), f"wave:{wave:.3f}%:{detail}"


# --------------------------------------------------------------------------------------
# the daily stop RESERVES what is already committed  (journal #5048, 2026-10-02)
#
# The stop used to read realised PnL only: block when `realized_today <= -stop`. With a
# ticket larger than the budget left, one more full ticket was admitted, and positions
# already open were not counted at all. MEASURED on SOL 2026-10-02: stop 0.8 SOL, ticket
# 0.83 SOL; CATGPT opened 19:06 and ZETA 19:10 with 0.0855 SOL of budget left; ZETA lost
# 0.678 (emergency_loss, -82% in a minute), CATGPT 0.254, and the day closed at -1.647
# SOL -- 206% of the stop.
#
# So an entry is admitted only if the budget survives every open live position AND the new
# ticket being stopped out:   realized - (open_exposure + ticket) * reserve > -stop.
# `reserve` is the fraction of the at-risk cost a stop exit is expected to give back.
# MEASURED: stop exits fill at -36% to -38% on this book, so the default is 0.40. A tail
# like ZETA's (-82%) is NOT covered by it; this bounds the expected day, not the worst one.
#
# Replayed on 14 days of live fills on the box (current stops, open cost at entry time):
# sol 12 of 95 entries refused (net -1.26 SOL of the -3.41 lost), bsc 20 of 51 (-0.112 of
# -0.314 BNB), robinhood 30 of 104 (-0.141 of -0.143 ETH). The refused trades' MEAN return
# is the same as the admitted ones' (sol -14.2% vs -14.9%): this is a cap on the size of a
# bad day, not an edge, and it should be judged as that.

#: ``protection.daily_stop_reserve_pct`` default, as a 0-1 fraction of open cost.
DAILY_STOP_RESERVE_DEFAULT = Decimal("0.40")


def daily_stop_reserve(cfg: RiskConfig) -> Decimal:
    """The configured reserve fraction, or the default. Never looser for being unreadable.

    Missing -> 0.40. Unreadable (a bool, text, NaN, negative) -> 0.40 with a warning,
    because a typo must not switch the brake off. Above 1 -> 1.0: more than the whole
    ticket is not a loss that can happen, and clamping down would loosen it. An explicit
    number in [0, 1] is the operator's and is used as written -- including 0, which turns
    the reservation off and leaves the plain realised-only stop.
    """
    raw = (cfg.protection or {}).get("daily_stop_reserve_pct")
    if raw is None:
        return DAILY_STOP_RESERVE_DEFAULT
    value: Decimal | None = None
    if not isinstance(raw, bool) and isinstance(raw, (int, float, str, Decimal)):
        try:
            value = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError):
            value = None
    if value is None or not value.is_finite() or value < 0:
        log.warning("protection.daily_stop_reserve_pct=%r is unreadable; using %s",
                    raw, DAILY_STOP_RESERVE_DEFAULT)
        return DAILY_STOP_RESERVE_DEFAULT
    return min(value, Decimal(1))


def daily_stop_reserved(open_exposure: int, size: int, reserve: Decimal) -> int:
    """Base units the stop holds back for open positions plus this ticket. Rounded UP."""
    held = (Decimal(max(0, int(open_exposure))) + Decimal(max(0, int(size)))) * reserve
    return int(held.to_integral_value(rounding=ROUND_CEILING))


# --------------------------------------------------------------------------------------
# SHADOW spends nothing, so live-money brakes do not apply to it
#
# Measured on the box, 7 days to 2026-10-03: shadow lanes were refused `daily_loss_stop`
# 224 times (migration-fade 166, pons-robinhood 58), `chain_disabled` 1,385 times and
# `size_not_positive:total_exposure_cap` 3 times -- the live book's losing day stopped the
# PAPER record growing, so the shadow sample is selected on live PnL. A paper position
# moves no money: the daily stop, the per-token and total exposure caps, the compounding
# bankroll / free balance and "chain enabled for real money" are all about money.
#
# What still binds a shadow entry: the kill switch, global/lane OFF, entries_paused,
# reduce_only, a halt, the lane's own chains, the size ladder and the pool band -- and a
# cap on concurrently open shadow positions per lane, because every one of them is quoted
# by protection (`watchdog.MAX_SHADOW_QUOTE_KEYS_PER_TICK` with live inventory, ALL of
# them without), and a protection tick that overruns halts entries on every chain.

#: ``protection.shadow_max_open_per_lane`` default. Over 30 days on the box the most
#: shadow positions one lane held at once was 13 (migration-fade, median hold 445 s), with
#: 43 of 186 abandoned unpriceable. INVENTED as a number: at 5 per lane the 2-keys-a-tick
#: rotation still reaches each paper position about every 3 ticks per active lane.
SHADOW_MAX_OPEN_PER_LANE_DEFAULT = 5


def shadow_max_open_per_lane(cfg: RiskConfig) -> int:
    """The configured per-lane cap on open shadow positions, or the default.

    A non-integer, a bool or a negative number is unreadable and falls back to the default
    rather than to "no cap". 0 is allowed and means no new shadow entries at all.
    """
    raw = (cfg.protection or {}).get("shadow_max_open_per_lane")
    if raw is None:
        return SHADOW_MAX_OPEN_PER_LANE_DEFAULT
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        log.warning("protection.shadow_max_open_per_lane=%r is unreadable; using %s",
                    raw, SHADOW_MAX_OPEN_PER_LANE_DEFAULT)
        return SHADOW_MAX_OPEN_PER_LANE_DEFAULT
    return raw


class RiskGate:
    """Entry admission, the daily loss stop, and the score -> size ladder.

    ``risk_provider`` exists so tests can inject a config without touching the filesystem;
    the default reads ``config/risk.yaml`` fresh on every single call.

    ``bankroll`` is a :class:`BankrollTracker`. With no balance provider wired it caps the
    bankroll at the configured number, which is what this gate did before it existed, so
    the default is byte-compatible: wins cannot raise the size until somebody proves the
    funds are there, and losses lower it immediately either way.
    """

    def __init__(
        self,
        risk_provider: Callable[[], RiskConfig] = get_risk,
        *,
        bankroll: BankrollTracker | None = None,
        balance_provider: BalanceProvider | None = None,
    ) -> None:
        self._risk_provider = risk_provider
        self.bankroll = bankroll or BankrollTracker(balance_provider)

    # ---------------------------------------------------------------- bankroll

    def bankroll_reading(
        self, chain: Chain, conn: sqlite3.Connection | None = None, cfg: RiskConfig | None = None
    ) -> BankrollReading:
        """The live, compounding bankroll for this chain. See :class:`BankrollTracker`.

        Read through this rather than ``cfg.chain_budget(chain).bankroll_base_units``: the
        config value is a baseline the operator typed, not the money we have.
        """
        c = cfg if cfg is not None else self._risk_provider()
        budget = c.chain_budget(chain)
        return self.bankroll.reading(chain, budget, self.open_exposure(chain, conn), conn)

    # ---------------------------------------------------------------- risk_state rows

    def _state_row(self, conn: sqlite3.Connection | None = None, day: str | None = None) -> dict[str, Any]:
        c = _conn(conn)
        key = day or day_key()
        row = fetch_one(c, "SELECT * FROM risk_state WHERE day_key = ?", (key,))
        if row is None:
            return {
                "day_key": key,
                "realized_native_json": "{}",
                "entries": 0,
                "halted": 0,
                "halt_reason": None,
                "updated_ms": now_ms(),
            }
        return row

    def _write_state(self, row: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
        c = _conn(conn)
        row = {**row, "updated_ms": now_ms()}
        c.execute(
            "INSERT INTO risk_state (day_key, realized_native_json, entries, halted, halt_reason, updated_ms) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(day_key) DO UPDATE SET "
            "realized_native_json=excluded.realized_native_json, entries=excluded.entries, "
            "halted=excluded.halted, halt_reason=excluded.halt_reason, updated_ms=excluded.updated_ms",
            (
                row["day_key"],
                row["realized_native_json"],
                int(row["entries"]),
                int(row["halted"]),
                row["halt_reason"],
                row["updated_ms"],
            ),
        )

    def realized_today(self, chain: Chain, conn: sqlite3.Connection | None = None) -> int:
        """Signed realised PnL in base units for the current UTC day. Negative is a loss.

        REWRITTEN 2026-09-21. This used to read ``risk_state.realized_native_json``, which
        was written by ``record_fill`` -- and ``record_fill`` has ZERO production callers.
        ``risk_state`` had 0 rows on the live box after 8 closed positions and 0 rows locally
        after 49. So this returned 0 forever, and with it the daily loss brake in
        ``check_entry``, the ``check_exit`` warning, ``daily_summary.stopped`` and
        ``loss_budget_left`` were all dead: the operator was told a bad day was capped and
        it was not. Found by adversarial review of the high-volume design.

        The positions table already holds every closed round trip with its realised
        native PnL, so this reads the truth directly rather than a ledger nothing feeds.
        Paper positions are excluded: a shadow loss must not halt live entries, and a
        shadow win must not license them.
        """
        from datetime import datetime, timezone

        c = conn or get_conn()
        now = datetime.now(timezone.utc)
        day_start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        row = c.execute(
            "SELECT COALESCE(SUM(realized_native), 0) AS r FROM positions "
            "WHERE chain=? AND closed_ms IS NOT NULL AND closed_ms >= ? AND mode != 'shadow'",
            (chain.value, day_start_ms),
        ).fetchone()
        return _as_int(row["r"] if row is not None else 0)

    # ---------------------------------------------------------------- exposure

    def open_exposure(
        self, chain: Chain, conn: sqlite3.Connection | None = None, token: str | None = None
    ) -> int:
        """Base units still at risk: cost less proceeds already taken, floored at zero.

        Paper positions are excluded, by the same rule as :meth:`realized_today`: a
        shadow position is not money. Until 2026-10-01 they were counted, so paper could
        shrink or refuse a live entry through the total-exposure cap, the per-token cap
        and ``free`` in the bankroll reading. Latent while no RH shadow lane held paper;
        live the moment the engine paper-trades launchpad-refused entries (engine
        ``_launchpad_refusal``) -- RH ran at ~62% of its 70% cap that day, so one 0.04
        ETH twin would have refused the next pons entry.
        """
        c = _conn(conn)
        sql = (
            "SELECT cost_native, proceeds_native FROM positions "
            "WHERE chain = ? AND closed_ms IS NULL AND mode != 'shadow'"
        )
        params: list[Any] = [chain.value]
        if token is not None:
            sql += " AND token = ?"
            params.append(token)
        total = 0
        for row in fetch_all(c, sql, params):
            total += max(0, _as_int(row["cost_native"]) - _as_int(row["proceeds_native"]))
        return total

    def open_shadow_positions(self, lane: Lane, conn: sqlite3.Connection | None = None) -> int:
        """Paper positions this lane holds open right now, on every chain."""
        row = fetch_one(
            _conn(conn),
            "SELECT COUNT(*) AS n FROM positions WHERE lane = ? AND mode = 'shadow' "
            "AND closed_ms IS NULL",
            (lane.value,),
        )
        return _as_int(row["n"] if row is not None else 0)

    # ---------------------------------------------------------------- admission

    def check_entry(
        self,
        chain: Chain,
        lane: Lane,
        size_base_units: int,
        conn: sqlite3.Connection | None = None,
        *,
        token: str | None = None,
    ) -> PolicyDecision:
        """May we open (or add to) a position of this size, on this chain, in this lane?"""
        cfg = self._risk_provider()
        findings: list[str] = []

        if cfg.kill_switch:
            return self._deny("kill_switch", chain, lane, conn)
        mode = cfg.effective_mode(lane)
        if mode is LaneMode.OFF:
            return self._deny("lane_off", chain, lane, conn)
        if cfg.entries_paused:
            return self._deny("entries_paused", chain, lane, conn)
        if cfg.reduce_only:
            return self._deny("reduce_only", chain, lane, conn)

        budget = cfg.chain_budget(chain)
        # "Enabled" is permission for REAL money on this chain; a paper entry spends none.
        if not budget.enabled and mode is not LaneMode.SHADOW:
            return self._deny("chain_disabled", chain, lane, conn)
        lane_cfg = cfg.lane(lane)
        if chain not in lane_cfg.chains:
            return self._deny("lane_chain_not_enabled", chain, lane, conn)

        state = self._state_row(conn)
        if int(state["halted"]):
            return self._deny(f"halted:{state['halt_reason'] or 'unspecified'}", chain, lane, conn)

        if mode is LaneMode.SHADOW:
            return self._check_shadow_entry(chain, lane, int(size_base_units), cfg, conn, token=token)

        realized = self.realized_today(chain, conn)
        stop = budget.daily_loss_stop_base_units
        if stop > 0 and realized <= -stop:
            return self._deny("daily_loss_stop", chain, lane, conn)
        findings.append(f"realized_today:{realized}")

        size = int(size_base_units)
        if size <= 0:
            cause = zero_size_cause(conn, chain, lane, token)
            return self._deny("size_not_positive" + (f":{cause}" if cause else ""), chain, lane, conn)
        if budget.min_position_base_units and size < budget.min_position_base_units:
            return self._deny(f"size_below_min:{size}", chain, lane, conn)
        if budget.max_position_base_units and size > budget.max_position_base_units:
            return self._deny(f"size_above_max:{size}", chain, lane, conn)

        # The stop RESERVES what is already committed: every open live position on this
        # chain, and this ticket, are assumed to stop out at `reserve` of their at-risk
        # cost. See `daily_stop_reserve` for the 2026-10-02 overshoot this closes.
        if stop > 0:
            reserved = daily_stop_reserved(
                self.open_exposure(chain, conn), size, daily_stop_reserve(cfg)
            )
            if realized - reserved <= -stop:
                return self._deny(
                    f"daily_stop_reserve:{realized}/{reserved}/{stop}", chain, lane, conn
                )
            findings.append(f"daily_stop_reserved:{reserved}/{stop}")

        # The compounding bankroll, not the number in the file. After a losing day this is
        # smaller than the configured baseline and every percentage below is measured
        # against the money we still have; after a winning one, and only once a balance
        # provider has proved the funds exist, it is larger. See :class:`BankrollTracker`.
        book = self.bankroll_reading(chain, conn, cfg)
        bankroll = book.equity_base_units
        findings.extend(book.findings)
        if bankroll <= 0:
            # Missing numeric authorization is not infinity.
            return self._deny("bankroll_unfunded", chain, lane, conn)
        pct = Decimal(size) * 100 / Decimal(bankroll)
        # Compared in Decimal. This used to build the ceiling as
        # Decimal(str(cfg.clamp_size_pct(float(pct)))): float(Decimal) rounds DOWN by an
        # ULP, so an exact pct came back a hair below itself and `pct > ceiling` fired on a
        # rounding error. MEASURED 2026-09-21 on the shipped sol budget: 44.4% of all
        # 55,001 legal sizes refused here although every one was under the 5% bound
        # (live message: "size_above_clamp:1.7478>1.7478155555555555"). This is the SECOND
        # copy of that bug -- position_size had the same round-trip -- and it is the one
        # that actually produced the refusals, because check_entry runs on the size
        # position_size already chose. A float in a money comparison, against this
        # repo's own rule.
        ceiling = Decimal(str(cfg.bounds.max_size_pct_bankroll))
        if pct > ceiling:
            return self._deny(f"size_above_clamp:{pct:.4f}>{ceiling}", chain, lane, conn)
        findings.append(f"size_pct_bankroll:{pct:.4f}")

        # The sizing band (``kaiba.execution.viability``, docs/TRADING-METHOD.md §5a).
        # Round-trip cost is U-shaped in size: the flat half does not care how small the
        # position is (at 0.02 SOL it eats 8.8% before any venue fee), and our own price
        # impact does not care how large it is until suddenly it is all that matters. Too
        # small is a guaranteed loss with a lottery ticket attached; too large does not
        # fill. Refuse either rather than resize — picking a size here would spend money
        # the operator did not authorise for this trade.
        #
        # ``token`` is what makes the upper arm possible: depth is per token, not per
        # chain. Without one only the fee arm is enforced and the findings say so.
        #
        # It sits *after* the bankroll checks on purpose: "you have no money on this
        # chain" is a truer answer than "this trade is the wrong size" when both are
        # true. The cost is reported in ``findings`` whether or not it fires, so a
        # decision record always shows what the round trip was expected to cost.
        cost = viability.check_size(chain, size, conn, token=token, cfg=cfg)
        findings.extend(cost.findings)
        if not cost.ok:
            return self._deny(cost.reason, chain, lane, conn)

        # Guard 1 and guard 3 at the gate. ``book.free_base_units`` has already netted out
        # open exposure and the gas reserve and, when the chain was readable, has already
        # been clamped to the wallet balance minus that reserve -- so an order that would
        # spend money we do not hold is refused here rather than failing on send.
        exposure = book.open_exposure_base_units
        if size > book.free_base_units:
            left = bankroll - exposure - size
            return self._deny(
                f"gas_reserve:{left}<{budget.gas_reserve_base_units}", chain, lane, conn
            )

        if token is None:
            findings.append("exposure_check_skipped_no_token")
        else:
            token_exposure = self.open_exposure(chain, conn, token=token)
            token_pct = Decimal(token_exposure + size) * 100 / Decimal(bankroll)
            if token_pct > Decimal(str(budget.max_exposure_pct)):
                return self._deny(
                    f"max_exposure_pct:{token_pct:.4f}>{budget.max_exposure_pct}", chain, lane, conn
                )
            findings.append(f"token_exposure_pct:{token_pct:.4f}")

        findings.append(f"mode:{mode.value}")
        return PolicyDecision(allowed=True, reason="entry_within_envelope", findings=findings)

    def _check_shadow_entry(
        self,
        chain: Chain,
        lane: Lane,
        size: int,
        cfg: RiskConfig,
        conn: sqlite3.Connection | None,
        *,
        token: str | None,
    ) -> PolicyDecision:
        """A PAPER entry: the money brakes are skipped, the size and volume checks are not.

        Reached only after the kill switch, OFF, ``entries_paused``, ``reduce_only``, the
        lane's chains and a halt have all been checked by :meth:`check_entry`. Skipped
        here, because a paper position moves no money: the daily loss stop and its
        reservation, the per-token and total exposure caps, the compounding bankroll and
        free balance. What remains is what makes the paper record mean something -- a
        size the lane would really send and a pool that could take it -- plus
        :func:`shadow_max_open_per_lane`, which protects protection's quote budget.
        """
        budget = cfg.chain_budget(chain)
        findings = ["mode:shadow", "paper_entry:live_money_brakes_skipped"]
        if not budget.enabled:
            findings.append("chain_not_enabled_for_live")
        if size <= 0:
            cause = zero_size_cause(conn, chain, lane, token)
            return self._deny("size_not_positive" + (f":{cause}" if cause else ""), chain, lane, conn)
        if budget.min_position_base_units and size < budget.min_position_base_units:
            return self._deny(f"size_below_min:{size}", chain, lane, conn)
        if budget.max_position_base_units and size > budget.max_position_base_units:
            return self._deny(f"size_above_max:{size}", chain, lane, conn)
        # The configured baseline, not the live compounding equity: see `position_size`.
        bankroll = budget.bankroll_base_units
        if bankroll <= 0:
            return self._deny("bankroll_unfunded", chain, lane, conn)
        pct = Decimal(size) * 100 / Decimal(bankroll)
        ceiling = Decimal(str(cfg.bounds.max_size_pct_bankroll))
        if pct > ceiling:
            return self._deny(f"size_above_clamp:{pct:.4f}>{ceiling}", chain, lane, conn)
        findings.append(f"size_pct_bankroll:{pct:.4f}")

        cap = shadow_max_open_per_lane(cfg)
        held = self.open_shadow_positions(lane, conn)
        if held >= cap:
            return self._deny(f"shadow_open_cap:{held}/{cap}", chain, lane, conn)
        findings.append(f"shadow_open:{held}/{cap}")

        cost = viability.check_size(chain, size, conn, token=token, cfg=cfg)
        findings.extend(cost.findings)
        if not cost.ok:
            return self._deny(cost.reason, chain, lane, conn)
        return PolicyDecision(allowed=True, reason="paper_entry_within_envelope", findings=findings)

    def check_exit(
        self, chain: Chain, lane: Lane, conn: sqlite3.Connection | None = None
    ) -> PolicyDecision:
        """Always allowed. Recorded here so the reason an exit ran is auditable."""
        cfg = self._risk_provider()
        findings = []
        if cfg.kill_switch:
            findings.append("kill_switch_active")
        if cfg.reduce_only:
            findings.append("reduce_only_active")
        if cfg.entries_paused:
            findings.append("entries_paused_active")
        state = self._state_row(conn)
        if int(state["halted"]):
            findings.append(f"halted:{state['halt_reason'] or 'unspecified'}")
        stop = cfg.chain_budget(chain).daily_loss_stop_base_units
        if stop > 0 and self.realized_today(chain, conn) <= -stop:
            findings.append("daily_loss_stop_active")
        return PolicyDecision(allowed=True, reason="exit_never_blocked", findings=findings)

    def _deny(
        self, reason: str, chain: Chain, lane: Lane, conn: sqlite3.Connection | None
    ) -> PolicyDecision:
        head = reason.split(":", 1)[0]
        if head in _BRAKE_REASONS or head == "halted":
            try:
                events.emit(
                    EventKind.RISK_HALT,
                    {"gate": "risk", "reason": reason, "lane": lane.value},
                    chain=chain,
                    level="warn",
                    conn=conn,
                )
            except Exception as exc:  # pragma: no cover - the refusal stands regardless
                log.warning("risk event emit failed reason=%s: %s", reason, exc)
        return PolicyDecision(allowed=False, reason=reason, findings=[])

    # ---------------------------------------------------------------- bookkeeping

    def record_fill(
        self,
        chain: Chain,
        pnl_native: int,
        conn: sqlite3.Connection | None = None,
        *,
        is_entry: bool = False,
    ) -> dict[str, Any]:
        """Fold one fill's realised PnL into today's row. Entries carry ``pnl_native=0``."""
        row = self._state_row(conn)
        realized = jload(row["realized_native_json"], {})
        realized[chain.value] = _as_int(realized.get(chain.value, 0)) + int(pnl_native)
        row["realized_native_json"] = jdump(realized)
        if is_entry:
            row["entries"] = int(row["entries"]) + 1
        self._write_state(row, conn)

        cfg = self._risk_provider()
        stop = cfg.chain_budget(chain).daily_loss_stop_base_units
        if stop > 0 and realized[chain.value] <= -stop and not int(row["halted"]):
            # Trip once, loudly. Entries are refused from here until the UTC day rolls over
            # or the operator resumes; exits are untouched.
            try:
                events.emit(
                    EventKind.RISK_HALT,
                    {
                        "gate": "risk",
                        "reason": "daily_loss_stop_tripped",
                        "realized": realized[chain.value],
                        "stop": stop,
                    },
                    chain=chain,
                    level="warn",
                    conn=conn,
                )
            except Exception as exc:  # pragma: no cover
                log.warning("risk event emit failed: %s", exc)
        return row

    def halt(self, reason: str, conn: sqlite3.Connection | None = None) -> None:
        row = self._state_row(conn)
        row["halted"] = 1
        row["halt_reason"] = reason
        self._write_state(row, conn)
        try:
            events.emit(
                EventKind.RISK_HALT, {"gate": "risk", "reason": reason, "action": "halt"},
                level="warn", conn=conn,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("risk event emit failed: %s", exc)

    def resume(self, conn: sqlite3.Connection | None = None) -> None:
        row = self._state_row(conn)
        row["halted"] = 0
        row["halt_reason"] = None
        self._write_state(row, conn)
        try:
            events.emit(
                EventKind.RISK_HALT, {"gate": "risk", "reason": "resumed", "action": "resume"},
                level="info", conn=conn,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("risk event emit failed: %s", exc)

    def resume_if_reason_starts_with(
        self, prefix: str, conn: sqlite3.Connection | None = None
    ) -> bool:
        """Lift the halt ONLY if this mechanism is the one that set it. Returns whether it did.

        A failsafe that cannot recover turns a transient fault into an outage. MEASURED
        2026-09-22: the protection-overrun failsafe fired correctly on three consecutive
        18.5 s ticks, and then entries stayed dead for 40 minutes on ALL THREE chains
        while the watchdog was back to 0.8-2.1 s ticks -- 17 decisions skipped with
        `risk_halt`, zero entries, nothing wrong any more.

        The prefix match is the whole safety property: an automatic resume may never lift
        an operator's halt, a daily-loss-stop halt, or any halt set by a different
        subsystem. It may only undo its own.
        """
        row = self._state_row(conn)
        if not int(row["halted"]):
            return False
        reason = str(row["halt_reason"] or "")
        if not prefix or not reason.startswith(prefix):
            return False
        log.warning("clearing self-set halt %r: the condition that set it has passed", reason)
        self.resume(conn)
        return True

    def daily_summary(self, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        cfg = self._risk_provider()
        row = self._state_row(conn)
        # The SAME number the gate enforces with, not the stored ledger beside it.
        #
        # MEASURED 2026-09-23 on the live box: this summary reported sol at -0.213516 with
        # `stopped: False` and 0.236 SOL of headroom, while `realized_today` -- which is
        # what `check_entry` actually calls -- returned -0.554208 against a 0.45 stop and
        # refused 41 entries in one hour. bsc read -0.010502/not-stopped against a live
        # -0.104802 past a 0.10 stop. Every operator-facing view of the day's risk, human
        # and agent, was reading the number that decides nothing.
        #
        # Two sources exist and they do not agree. `realized_native_json` accumulates
        # per-fill through `record_fill`, which `accounting._record_risk_fill` began
        # calling on 2026-09-22; `realized_today` sums `positions.realized_native` for the
        # UTC day. A ledger that accrues from the moment it was wired cannot match a table
        # that has every closed round trip, and on the live box it did not: -0.213516
        # against -0.554208 on the same chain at the same second.
        #
        # Which is authoritative is not a matter of taste: `check_entry` refuses entries
        # using `realized_today`, so that IS the day's risk. A summary that reports the
        # other number is reporting something that decides nothing.
        #
        # The stored blob is still reported, under a name that says what it is, so a
        # future divergence is visible here instead of having to be discovered by tracing
        # a refusal backwards.
        stored = {k: _as_int(v) for k, v in jload(row["realized_native_json"], {}).items()}
        chains: dict[str, Any] = {}
        for chain, budget in cfg.chains.items():
            got = self.realized_today(chain, conn)
            stop = budget.daily_loss_stop_base_units
            book = self.bankroll_reading(chain, conn, cfg)
            chains[chain.value] = {
                "realized_native": got,
                #: What `record_fill` stored, kept for comparison only. It decides nothing.
                "realized_native_stored": stored.get(chain.value, 0),
                "daily_loss_stop_base_units": stop,
                "loss_budget_left": max(0, stop + got) if stop else None,
                "stopped": bool(stop) and got <= -stop,
                "open_exposure": book.open_exposure_base_units,
                # The configured baseline stays under its historical key so nothing that
                # reads this dict starts silently reporting a different quantity; the
                # compounded figure is new and named for what it is.
                "bankroll_base_units": budget.bankroll_base_units,
                "bankroll_equity_base_units": book.equity_base_units,
                "bankroll_free_base_units": book.free_base_units,
                "bankroll_compounded_base_units": book.compounded,
                "bankroll_onchain_base_units": book.onchain_base_units,
                "bankroll_onchain_basis": book.onchain_basis,
                "bankroll_onchain_age_ms": book.onchain_age_ms,
                "bankroll_binding": book.binding,
            }
        return {
            "day": row["day_key"],
            "entries": int(row["entries"]),
            "halted": bool(int(row["halted"])),
            "halt_reason": row["halt_reason"],
            "kill_switch": cfg.kill_switch,
            "entries_paused": cfg.entries_paused,
            "reduce_only": cfg.reduce_only,
            "global_mode": cfg.global_mode.value,
            "chains": chains,
        }

    # ---------------------------------------------------------------- sizing

    def position_size(
        self,
        chain: Chain,
        lane: Lane,
        score: float,
        conn: sqlite3.Connection | None = None,
        *,
        token: str | None = None,
    ) -> int:
        """Score -> allocation ladder, clamped by the envelope, the budget and the pool.

        Returns base units, or 0 when the ladder, the envelope, the remaining budget or
        the token's own depth leaves nothing worth sending. 0 is a real answer: the caller
        must not treat it as "use the default".

        ``token`` is optional only so existing callers keep working. Pass it: without it
        the size is chosen with no knowledge of the pool it is going into, which is how
        0.06 SOL orders ended up in $25 pools. See :meth:`_clamp_to_band`.

        **This is where compounding actually bites.** The percentage comes from the lane
        and the score; the *amount* comes from :meth:`bankroll_reading`, so the same 5%
        conviction bet is a smaller number of lamports after a losing run and a larger one
        after a winning run, with no human editing anything. Both directions are automatic
        and neither is optional -- that symmetry is what keeps an aggressive sizer from
        being a ruinous one.

        **Order of composition**, because it is load-bearing and each step can only take
        money off the table: lane ladder -> lane floor and ceiling -> the 5% envelope ->
        the per-chain per-position cap -> the 25% total exposure cap -> free balance ->
        the viability band -> the chain minimum -> the concentration multiplier. The
        multiplier is last so that nothing can undo it and it can never raise a size; see
        ``CONCENTRATION_LADDER`` (the launch wave) and ``DEV_SUPPLY_LADDER`` (the
        creator's own share) for the two policies, and :func:`_concentration_multiplier`
        for how they compose and what "unknown" is worth. It needs ``token``: without one
        there is nothing to look up and the size is returned unscaled.
        """
        cfg = self._risk_provider()
        budget = cfg.chain_budget(chain)
        lane_cfg = cfg.lane(lane)
        mode = cfg.effective_mode(lane)
        if cfg.kill_switch or mode is LaneMode.OFF:
            return note_zero_size(conn, chain, lane, token, "lane_off")
        # PAPER sizes from the configured baseline and skips every money brake below: the
        # chain's live permission, compounding, the drawdown cut, the total exposure cap
        # and the free balance. It keeps the ladder, the envelope, the position cap, the
        # pool band and the concentration cut, and is RAISED to the chain minimum (the
        # live ticket), so a paper position is the trade live would really send. See
        # `_check_shadow_entry`.
        paper = mode is LaneMode.SHADOW
        if (not budget.enabled and not paper) or chain not in lane_cfg.chains:
            return note_zero_size(conn, chain, lane, token, "chain_not_in_lane")
        if budget.bankroll_base_units <= 0:
            return note_zero_size(conn, chain, lane, token, "bankroll_zero")

        fraction = score_fraction(score)
        if fraction <= 0:
            return note_zero_size(conn, chain, lane, token, f"score_below_ladder:{score:g}")
        pct = Decimal(str(lane_cfg.size_pct_max)) * fraction
        # The ladder scales the lane maximum, but the lane minimum is still the lane's floor.
        pct = max(pct, Decimal(str(lane_cfg.size_pct_min)))
        pct = min(pct, Decimal(str(lane_cfg.size_pct_max)))
        # The envelope ceiling, compared in Decimal. This used to round-trip through
        # cfg.clamp_size_pct(float(pct)): float(Decimal) rounds DOWN by an ULP, so an exact
        # pct came back a hair smaller than itself and `pct > ceiling` fired on a rounding
        # error -- MEASURED 2026-09-21: 44.4% of all 55,001 legal sizes on the shipped sol
        # budget refused with size_above_clamp although every one was under the 5% bound
        # (the live box's own message: 1.7478>1.7478155555555555). A float in a money
        # comparison, against this repo's own rule.
        pct = min(pct, Decimal(str(cfg.bounds.max_size_pct_bankroll)))

        if paper:
            # The paper twin of this method's live path, stopping at the position cap.
            size = int(Decimal(budget.bankroll_base_units) * pct / Decimal(100))
            if budget.max_position_base_units:
                size = min(size, budget.max_position_base_units)
            # ...and never below the chain's ticket floor. MEASURED on the box, 7 days to
            # 2026-10-03: of the 1,612 shadow entries refused by a money brake, 1,171 then
            # sized BELOW `min_position_base_units` (migration-fade 0.10 SOL against the
            # 0.83 SOL floor, pons-robinhood 0.0018 ETH against 0.037) and would only have
            # traded their refusal for `below_min_position` -- skipping the money brakes
            # alone admits 0 of them. The floor is the size live sends (min = max = the
            # flat ticket on sol and robinhood), so a paper position at the floor is the
            # trade live would take, at live's impact. Raising a PAPER size spends nothing.
            if budget.min_position_base_units:
                size = max(size, budget.min_position_base_units)
            if size <= 0:
                return note_zero_size(conn, chain, lane, token, "paper_size_zero")
            return self._finish_size(chain, lane, token, size, budget, conn)

        book = self.bankroll_reading(chain, conn, cfg)
        if book.equity_base_units <= 0:
            return note_zero_size(conn, chain, lane, token, "equity_zero")
        size = int(Decimal(book.equity_base_units) * pct / Decimal(100))
        # OWNER 2026-09-23: after a losing day, trade smaller until it is made back.
        # Applied BEFORE the position cap so the cap is a ceiling on the reduced size and
        # cannot quietly restore what the drawdown took off.
        drawdown, _high = prior_day_drawdown(chain, conn)
        recovery, recovery_label = recovery_multiplier(drawdown, budget.bankroll_base_units)
        if recovery < 1:
            reduced = int(Decimal(size) * recovery)
            log.info(
                "%s on %s cut the size %sx: %d -> %d base units",
                recovery_label, chain.value, recovery, size, reduced,
            )
            size = reduced
        if budget.max_position_base_units:
            size = min(size, budget.max_position_base_units)
        # AGGREGATE EXPOSURE CAP. max_exposure_pct is applied PER TOKEN and
        # max_concurrent_positions is declared and enforced nowhere, so nothing capped the
        # basket: a simulated high-volume run admitted 22 simultaneous 0.2 SOL positions =
        # 97.8% of the bankroll before `free` hit the gas reserve (MEASURED 2026-09-21).
        # Positions on one chain in one hour are one factor, not N bets: the pairwise
        # correlation of returns is +0.22 (robust across 15/30/60-min buckets), so ten
        # positions are 3.4 independent bets and diversification is exhausted by ~8. A cap
        # on TOTAL exposure respects the owner's no-count-cap mandate and self-tightens as
        # the bankroll shrinks. 25% is the risk-scaling agent's figure; INVENTED in the
        # sense that the ceiling is a judgement, MEASURED in that rho=0.22 bounds its value.
        total_cap_pct = Decimal(str(getattr(cfg.bounds, "max_total_exposure_pct", None) or 25))
        total_cap = int(Decimal(budget.bankroll_base_units) * total_cap_pct / Decimal(100))
        already = int(self.open_exposure(chain, conn))
        room = total_cap - already
        if room <= 0:
            return note_zero_size(conn, chain, lane, token, f"total_exposure_cap:{already}/{total_cap}")
        size = min(size, room)
        # ``free`` has already had open exposure and the gas reserve taken out of it, and
        # when the chain was readable it has already been clamped to the wallet balance
        # minus that reserve. Subtracting exposure again here would charge us twice for
        # the same money and shrink every subsequent bet as positions opened.
        size = min(size, book.free_base_units)
        if size <= 0:
            return note_zero_size(conn, chain, lane, token, f"free_zero:{book.free_base_units}")
        return self._finish_size(chain, lane, token, size, budget, conn)

    def _finish_size(
        self,
        chain: Chain,
        lane: Lane,
        token: str | None,
        size: int,
        budget: ChainBudget,
        conn: sqlite3.Connection | None,
    ) -> int:
        """The tail of :meth:`position_size` shared by live and paper: pool band, chain
        minimum, concentration. Each step can only take money off the table."""
        banded = self._clamp_to_band(chain, token, size, conn)
        if banded <= 0:
            return note_zero_size(conn, chain, lane, token, "no_viable_band")
        if banded < budget.min_position_base_units:
            # A NEAR MISS is rounding, not a decision. MEASURED 2026-09-24 on the live
            # box: a sol entry was refused at 555,524,237 against a floor of 560,000,000
            # -- 99.2% of it, vetoed over 0.8%. The pool band and the chain floor are
            # computed from different inputs (pool depth vs the operator's "never trade
            # dust"), so they land a fraction apart routinely, and every time they do the
            # trade is lost to arithmetic rather than to judgement.
            #
            # Clamping UP is the conservative direction here: the result is EXACTLY the
            # operator's own minimum, the size every other trade on this chain already
            # uses, and the overshoot against the pool band is bounded by the tolerance.
            # Outside the tolerance the refusal stands -- a pool that can carry only half
            # the floor is a real veto and must keep saying so.
            shortfall = Decimal(budget.min_position_base_units - banded) / Decimal(
                budget.min_position_base_units
            )
            if shortfall <= MIN_POSITION_NEAR_MISS:
                banded = int(budget.min_position_base_units)
            else:
                # Attributed BEFORE the concentration multiplier runs, deliberately: a
                # size that was already under the chain minimum was not put there by the
                # multiplier, and a cause that claimed otherwise would be a false one.
                return note_zero_size(
                    conn, chain, lane, token,
                    f"below_min_position:{banded}<{budget.min_position_base_units}",
                )
        # CONCENTRATION, last and downwards only: the launch wave (CONCENTRATION_LADDER)
        # and the creator's own share (DEV_SUPPLY_LADDER), whichever cuts deeper.
        #
        # It is applied *after* every other clamp so that it cannot be undone by one:
        # ahead of the 5% envelope or the exposure cap a multiplier would only change
        # which ceiling binds, and ahead of `_clamp_to_band` the band's own floor would
        # raise the shrunken size straight back to it -- a 59%-bundled launch would come
        # out the same size as a clean one. Placed here the composition is one-way:
        # `scaled <= banded` for every input, which is the property the tests pin.
        #
        # Without a token there is nothing to look up. The same rule `_clamp_to_band`
        # already applies to depth: the sizer does not pretend to know a token it was
        # never told about, and `check_entry` still refuses what it cannot price.
        if token is None:
            return banded
        multiplier, label = _concentration_multiplier(chain, token, conn)
        if multiplier >= 1:
            return banded
        # Truncates. Money is integers and the remainder is dropped, not rounded, so the
        # multiplier is never generous by a base unit.
        scaled = int(Decimal(banded) * multiplier)
        log.info(
            "concentration %s on %s cut the size %sx: %d -> %d base units",
            label, token, multiplier, banded, scaled,
        )
        if scaled < budget.min_position_base_units:
            # Requirement: a refusal that names the concentration, not a silent zero.
            return note_zero_size(
                conn, chain, lane, token,
                f"concentration:{label}:{multiplier}x:{scaled}<{budget.min_position_base_units}",
            )
        floor = self._viable_floor(chain, token, conn)
        if floor is not None and scaled < floor:
            # The multiplier asked for a position smaller than this pool can carry
            # economically. `check_entry` would refuse it too, as `round_trip_cost...`,
            # but only the sizer knows *why* the size got there -- and letting it through
            # to be refused downstream would bury the cause. The band is composed with,
            # not overridden: we do not raise the size back to the floor, because that is
            # exactly how a concentrated launch would end up the same size as a clean one.
            return note_zero_size(
                conn, chain, lane, token,
                f"concentration:{label}:{multiplier}x:{scaled}<viable_floor:{floor}",
            )
        return scaled

    def _viable_floor(
        self, chain: Chain, token: str, conn: sqlite3.Connection | None
    ) -> int | None:
        """The smallest size at which this token's round trip can pay for itself, or None.

        A second, cheap read of the same band ``_clamp_to_band`` used (both are local
        SQLite reads; neither touches a provider). Separate because that method returns a
        clamped size rather than the band, and it belongs to another task.

        None means "we could not look", which is never a reason to refuse here: depth is
        stale or missing on the overwhelming majority of decisions, and a sizer that
        refused on a missing floor would stop the agent trading and report it as a
        concentration problem.
        """
        try:
            band = viability.sizing_band(chain, conn, token=token)
        except Exception as exc:  # noqa: BLE001 - sizing must not die on a missing pool
            log.debug("no sizing band for %s on %s: %s", token, chain.value, exc)
            return None
        floor = band.min_viable_base_units
        return int(floor) if floor is not None else None

    def _clamp_to_band(
        self,
        chain: Chain,
        token: str | None,
        size: int,
        conn: sqlite3.Connection | None,
    ) -> int:
        """Clamp the ladder's answer into the sizes this token can actually be traded at.

        The ladder scales by conviction and the envelope caps by bankroll; neither knows
        anything about the pool. ``viability.sizing_band`` does: the flat cost sets a
        floor below which the trade cannot pay for itself, and the token's own depth sets
        a ceiling above which our own order moves the price more than the ceiling allows.

        Three outcomes, and the middle one is the point of this method:

        * **no band** -- the floor is above the ceiling, so there is no size at which this
          trade is economic. Returns 0, which :meth:`position_size` documents as a real
          answer and not a request for a default. On pools under roughly $500 the cheapest
          possible round trip is 14-30% against a 7% ceiling, and 16 of our first 32 fills
          went into pools that thin.
        * **size below the floor** -- raise it to the floor when the envelope allows.
          Without this the engine sizes 0.025 SOL from the ladder, the entry gate refuses
          it as uneconomic, and a tradable token is skipped over a number we chose.
        * **size above the ceiling** -- lower it. This is the case that cost us 137 of 211
          orders to ``slippage_exceeded``: sized by bankroll, refused by the pool.

        Never raises and never invents. A band we cannot compute leaves the size alone --
        ``check_entry`` still refuses an unpriceable token, so the refusal happens once, in
        the gate, rather than twice with two different reasons. That distinction is
        load-bearing right now: depth is almost never fresh at decision time (see below),
        so a sizer that refused on missing depth would stop the agent trading entirely and
        report it as a sizing problem.
        """
        if not token:
            return size
        try:
            from kaiba.execution.viability import sizing_band

            band = sizing_band(chain, conn, token=token)
        except Exception as exc:  # noqa: BLE001 - sizing must not die on a missing pool
            log.debug("no sizing band for %s on %s: %s", token, chain.value, exc)
            return size
        floor = band.min_viable_base_units
        ceiling = band.max_viable_base_units
        if (floor is None or ceiling is None) and band.depth is not None:
            # We priced the pool and there is still no band: the cheapest size that
            # amortises the flat cost is already too big for it. A real answer, so 0.
            # `depth` is the discriminator rather than `reason`, because a reason is a
            # sentence and this is a fact about whether we managed to look.
            return 0
        if floor is None or ceiling is None:
            # Depth unavailable. Leave the size alone rather than returning 0: refusing
            # here *and* in check_entry means two refusals with two reasons for one
            # cause, and it hides which gate actually fired. Measured 2026-09-20: of 60
            # ENTER decisions, 54 had no curve snapshot at all and the other 6 were
            # 65-2050s stale against a 60s freshness window -- so returning 0 here would
            # silently refuse every entry the agent makes. The gap between when the
            # scanner snapshots a curve and when the engine decides is the real defect;
            # pricing it as "no size" in the sizer would bury that behind a shrug.
            return size
        if floor > ceiling:
            # A real answer, not missing data: the cheapest size that amortises the flat
            # cost is already too big for this pool. On pools under roughly $500 the
            # bottom of the cost curve is 14-30% against a 7% ceiling.
            return 0
        return max(floor, min(size, ceiling))


__all__ = [
    "BANKROLL_FRESH_MS",
    "BANKROLL_HOLD_MS",
    "BANKROLL_MAX_GROWTH_FACTOR_PER_READ",
    "COMPOUNDING_MODE",
    "NATIVE_BALANCE_TOKEN",
    "SCORE_LADDER",
    "WRAPPED_SOL_MINT_READS_ZERO",
    "BalanceProvider",
    "BankrollBasis",
    "BankrollReading",
    "BankrollTracker",
    "NativeBalance",
    "RiskGate",
    "day_key",
    "parse_native_balance",
    "prior_day_drawdown",
    "recovery_multiplier",
    "score_fraction",
]
