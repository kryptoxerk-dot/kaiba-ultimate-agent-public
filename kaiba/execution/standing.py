"""Venue-side standing orders: the protection that outlives this process.

``kaiba.execution.protection`` is Layer 2 of the safety net and it is correct — the
ladder, the ratchet, the anti-wick rule and the rug monitor are pure, tested and right.
They are also entirely in memory and in a database row. Kill this process while a
position is open and nothing is watching it: no stop, no ladder, no rug monitor. A crash,
a reboot, a VPS migration or an OOM kill leaves real money in a memecoin with nobody at
the wheel, while ``stop_loss_bps: 3000`` in ``config/risk.yaml`` tells the operator a stop
exists. This module is the mirror that closes that gap, and the honesty that says when it
has not.

What the venue can actually do
------------------------------

Read this before assuming ``to_gmgn_condition_orders`` can simply be posted somewhere.
Verified against the installed gmgn-cli 1.6.1 (``dist/commands/swap.js``,
``dist/client/OpenApiClient.js``, ``dist/validate.js``), not against a research digest:

* ``swap --condition-orders '<json>'`` takes exactly the array
  :func:`~kaiba.execution.protection.to_gmgn_condition_orders` renders — but it is a flag
  on a **buy**. It attaches protection at entry and there is no command that amends it
  afterwards. It therefore cannot protect a position that is already open, which is the
  whole failure this module exists for.
* ``order strategy create --order-type smart_trade --sub-order-type mix_trade
  --condition-orders <json>`` takes the same array standalone, but the CLI's own help says
  it "must include a ``buy_low`` entry + TP/SL entries". ``smart_trade`` is an
  entry-plus-protection bundle; submitting one for a position we already hold would open a
  **new buy**. It is not a protection mechanism and this module never sends it.
* ``order strategy create --order-type limit_order --sub-order-type stop_loss|take_profit
  --check-price <price>`` is the one standalone shape that protects tokens already held.
  It takes an **absolute trigger price**, one order per rung, with no trailing variant.

So the mirror is a translation, not a pass-through. ``to_gmgn_condition_orders`` stays the
single source of truth for *which rungs survive and at what percentages* — this module
calls it and reads its output — and :func:`render_intents` turns each rendered percentage
into the absolute price the only usable venue command accepts. No ladder arithmetic is
reimplemented here; every number traces to ``protection.py``.

Two rungs cannot be mirrored and are reported rather than faked:

* ``profit_stop_trace`` (the trailing ratchet) has no ``limit_order`` form. The trail stays
  in process. What the venue holds is the **last level the ratchet reached**, re-placed by
  :func:`reconcile` when it moves materially. Between our ticks the venue is behind by at
  most one reprice threshold, which is a bounded, stated loss of fidelity.
* ``loss_stop`` in the condition-order shape cannot express a stop above entry (see
  ``protection.to_gmgn_condition_orders``); the ``limit_order`` shape can, because it takes
  an absolute price. The mirror therefore uses ``state.stop_price`` — still protection.py's
  own output — whenever it is higher, and never a value below what protection.py says.

The three failure directions, handled rather than hoped away
------------------------------------------------------------

**Double-sell.** The ownership rule is: *a quantity has exactly one owner, the owner is
recorded in* ``standing_orders.owner``, *and the venue owns a rung only while its row is
live*. Before the watchdog sells, it calls :func:`claim_for_exit`, which cancels the venue
rungs that overlap the sale and takes ownership back. **The claim never refuses, never
blocks and never raises** — exits are never gated (``risk.py``: a brake that stops you
selling is not a brake). When a cancel cannot be confirmed the rung is marked
``contested``, an ``error`` event names the exposure, and no re-placement happens until
reconciliation resolves it. A trim releases only the take-profit rungs that could fire at
the current price; the stop is left alone because ``sell_ratio_type=hold_amount`` with
ratio 100 means "everything still held", which stays correct after a partial sale.

**Ambiguous placement.** Same rule as the swap path. The row is written ``placing``
*before* the venue call. A create we could not read the result of is ``unknown``, never
"not placed", and an ``unknown`` rung is **never placed again** — only :func:`reconcile`,
reading ``order strategy list``, may resolve it. Assuming failure is how you get two
standing orders selling the same tokens.

**Silent non-placement.** Every refusal, rate limit and outage writes an ``error``-level
event *and* an ``unprotected_reason`` on ``standing_sync``, and
:func:`protection_status` reports ``venue_protected=False`` with an ``UNAVAILABLE`` basis.
Nothing in this module reports protected because the feature is switched on;
:func:`feature_state` is what makes ``use_provider_orders`` honest, and it is false
whenever the venue call cannot actually be made.

Cost
----

A GMGN write costs weight 10 against a Free capacity of 10 refilling at 0.2/s — one write
per fifty seconds, and the 429 is IP-wide, so an overspend blinds the price reads too.
Placement is therefore scheduled, not per tick: at most :data:`DEFAULT_MAX_WRITES` writes
per position per sync, no sync inside ``standing_min_sync_interval_s``, and a live order
whose trigger is within ``standing_reprice_bps`` of the ladder is left alone. Reconcile
reads the whole wallet with one cheap ``order strategy list``.

Withdrawal
----------

A conditional order is a sell into our own wallet and is gated like any other value
operation: every create and cancel goes through
:func:`kaiba.execution.policy.assert_no_withdrawal` (``order_strategy`` / ``order_cancel``,
both already in the closed vocabulary), the parameter set is a frozen allowlist checked
for equality, and **no code path here accepts or emits a destination**. ``--from`` must
equal the wallet bound in ``config/risk.yaml`` and GMGN rejects one that is not bound to
the API key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

from kaiba.core import events as ev
from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import fetch_all, fetch_one, get_conn, upsert
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import (
    EVM_ZERO,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    EvidenceBasis,
    Lane,
    LaneMode,
    Position,
    digest,
    now_ms,
)
from kaiba.execution.protection import (
    GMGN_SELL_RATIO_TYPE,
    NO_CONDITION_ORDER_CHAINS,
    ProtectionConfig,
    ProtectionState,
    protection_config,
    to_gmgn_condition_orders,
)

log = logging.getLogger(__name__)

#: Payload discriminator on every event this module writes, matching the watchdog's
#: convention. Filter with ``kaiba_events(kinds=["system"])`` on ``payload.service``.
SERVICE = "standing"

PROVIDER = "gmgn"

#: Endpoint strings for the limiter. The *family* (before the dot) is what a 429 cools
#: down; the *last segment* is what ``Limits.weight_for`` charges when the full string is
#: absent from the weight table. ``strategy.swap`` is deliberate, not a typo: GMGN's
#: published weight table (``docs/research/04-data-sources.md``) prices ``quote`` and
#: ``swap`` at 10 and says nothing about strategy writes, and a signed trade write that is
#: billed as weight 1 is exactly the bug ``limiter.weight_for`` documents — the most
#: expensive call charged as the cheapest, so the real 429 arrives while the budget still
#: reports headroom. Charging a create as a swap is the conservative reading. The clean
#: fix is an explicit ``provider_budgets.gmgn.weights`` entry, which is core-owned.
CREATE_ENDPOINT = "strategy.swap"
LIST_ENDPOINT = "strategy.list"
CANCEL_ENDPOINT = "strategy.cancel"

#: Only ``limit_order`` protects tokens already held. ``smart_trade`` needs a ``buy_low``
#: leg and would open a position; it is never sent from here.
VENUE_ORDER_TYPE = "limit_order"
SUB_TAKE_PROFIT = "take_profit"
SUB_STOP_LOSS = "stop_loss"

STOP_TAG = "stop"

#: Hard ceiling on one CLI invocation, matching ``executor``.
DEFAULT_TIMEOUT_S = 45.0

#: Scheduling defaults, overridable in the ``protection:`` block of ``config/risk.yaml``.
DEFAULT_MIN_SYNC_INTERVAL_S = 60
DEFAULT_MAX_WRITES = 2
DEFAULT_REPRICE_BPS = 100  # 1%: below this the venue stop is close enough to leave alone

#: How long an ``unknown`` placement may stay unresolved before a *successful* listing that
#: does not contain it is accepted as positive evidence it never landed.
UNKNOWN_GRACE_MS = 120_000

#: Repeat interval for the "this position is not venue-protected" alarm. Loud, not 5s loud.
UNPROTECTED_WARN_INTERVAL_S = 300


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    """Parse money. ``None`` rather than 0 for anything unreadable (CONTRACT rule 2)."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):  # a float never touches money here
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(value: Decimal | None) -> str | None:
    """Plain decimal string: no exponent, no float, no trailing-zero noise."""
    if value is None:
        return None
    try:
        return format(value.normalize(), "f")
    except (InvalidOperation, ValueError):
        return None


def _native_mint(chain: Chain) -> str:
    return SOL_NATIVE_MINT if chain is Chain.SOL else EVM_ZERO


def _chain_of(value: Any) -> Chain | None:
    try:
        return value if isinstance(value, Chain) else Chain(str(value))
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# rendering: protection.py's ladder, translated into what the venue accepts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StandingIntent:
    """One rung of the ladder as the venue would hold it.

    ``trigger_price_usd`` is in our unit. The unit the venue is actually given is decided
    later, in :func:`sync_position`, because it depends on an operator declaration and on
    a native price we may not have.
    """

    tag: str
    order_type: str  # what protection.py rendered: profit_stop / loss_stop
    sub_order_type: str  # what gmgn-cli is given: take_profit / stop_loss
    trigger_price_usd: Decimal
    sell_ratio: Decimal

    @property
    def digest(self) -> str:
        return digest(
            {
                "tag": self.tag,
                "sub": self.sub_order_type,
                "px": _s(self.trigger_price_usd),
                "ratio": _s(self.sell_ratio),
            }
        )


@dataclass(frozen=True)
class Rendering:
    """What can be mirrored, what cannot, and why."""

    intents: tuple[StandingIntent, ...] = ()
    unmirrorable: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.intents)

    def by_tag(self) -> dict[str, StandingIntent]:
        return {i.tag: i for i in self.intents}


def _tp_tag_for_scale(scale: Decimal, cfg: ProtectionConfig) -> str | None:
    """Map a rendered ``price_scale`` back to its ladder rung.

    ``to_gmgn_condition_orders`` omits rungs that already fired, so position in the output
    list is not the rung index. The scale is ``(multiple - 1) * 100``, and the rung is
    whichever ladder entry has that multiple — read from the same config the render used,
    so the tags always agree with ``ProtectionState.tp_done``.
    """
    multiple = Decimal(1) + scale / Decimal(100)
    for index, (mult, _pct) in enumerate(cfg.tp_ladder):
        if Decimal(mult) == multiple:
            return f"tp{index + 1}"
    return None


def render_intents(
    state: ProtectionState,
    *,
    chain: Chain,
    cfg: ProtectionConfig | None = None,
) -> Rendering:
    """Translate the surviving ladder into absolute-price standing orders.

    The rungs and their percentages come from
    :func:`kaiba.execution.protection.to_gmgn_condition_orders`; this function only turns
    each percentage into the absolute trigger that ``order strategy create`` needs. If that
    function ever changes, this follows it — there is no second ladder here.
    """
    cfg = cfg or protection_config()
    entry = state.entry_price
    if entry is None or entry <= 0:
        return Rendering(reason="entry_price_unavailable")

    rendered = to_gmgn_condition_orders(state, cfg, chain=chain.value)
    if not rendered:
        return Rendering(reason=f"venue_has_no_condition_orders_for_chain:{chain.value}")

    intents: list[StandingIntent] = []
    unmirrorable: list[str] = []
    for order in rendered:
        kind = str(order.get("order_type"))
        scale = _dec(order.get("price_scale"))
        ratio = _dec(order.get("sell_ratio"))
        if scale is None:
            unmirrorable.append(f"{kind}:price_scale_unreadable")
            continue

        if kind == "profit_stop":
            tag = _tp_tag_for_scale(scale, cfg)
            if tag is None or ratio is None:
                unmirrorable.append(f"{kind}:rung_not_in_ladder")
                continue
            intents.append(
                StandingIntent(
                    tag=tag,
                    order_type=kind,
                    sub_order_type=SUB_TAKE_PROFIT,
                    trigger_price_usd=entry * (Decimal(1) + scale / Decimal(100)),
                    sell_ratio=ratio,
                )
            )
        elif kind == "loss_stop":
            # The rendered drop is clamped at breakeven because `loss_stop` cannot express
            # a stop above entry. `limit_order` can, so take whichever of the two is
            # higher — both are protection.py's own numbers and the stop only ever rises.
            from_render = entry * (Decimal(1) - scale / Decimal(100))
            trigger = max(from_render, state.stop_price) if state.stop_price else from_render
            intents.append(
                StandingIntent(
                    tag=STOP_TAG,
                    order_type=kind,
                    sub_order_type=SUB_STOP_LOSS,
                    trigger_price_usd=trigger,
                    sell_ratio=ratio if ratio is not None else Decimal(100),
                )
            )
        else:
            # profit_stop_trace and anything added later: no limit_order equivalent.
            unmirrorable.append(f"{kind}:no_limit_order_equivalent")

    return Rendering(tuple(intents), tuple(unmirrorable))


# --------------------------------------------------------------------------------------
# feature honesty: `use_provider_orders` may only be true when it is true
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueEnv:
    """Everything outside this module that decides whether a placement can happen.

    Injected rather than read at every call site so a test never depends on the operator's
    ``.env`` and so :func:`feature_state` can be asked hypothetical questions.
    """

    api_key: str = ""
    signing_key: str = ""
    automation_enabled: bool = False
    cli_present: bool = False
    wallet: str | None = None
    price_unit: str | None = None
    priority_fee: str | None = None
    tip_fee: str | None = None


def _protection_block() -> dict[str, Any]:
    try:
        return dict(get_risk().protection or {})
    except Exception as exc:  # noqa: BLE001 - an unreadable risk file must not raise here
        log.warning("standing: could not read the protection block: %s", exc)
        return {}


def _opt_str(block: dict[str, Any], key: str) -> str | None:
    value = block.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def venue_env(chain: Chain, *, block: dict[str, Any] | None = None) -> VenueEnv:
    """Read the real environment. Never raises; an unreadable input is simply absent."""
    block = _protection_block() if block is None else block
    try:
        settings = get_settings()
        api_key = settings.gmgn_api_key
        signing_key = settings.gmgn_private_key
        automation = int(settings.gmgn_allow_automated_trades or 0) == 1
    except Exception as exc:  # noqa: BLE001
        log.warning("standing: settings unavailable: %s", type(exc).__name__)
        api_key, signing_key, automation = "", "", False

    try:
        from kaiba.providers.gmgn_cli import cli_argv

        cli_present = cli_argv() is not None
    except Exception as exc:  # noqa: BLE001
        log.debug("standing: could not resolve gmgn-cli: %s", type(exc).__name__)
        cli_present = False

    try:
        wallet = get_risk().chain_budget(chain).wallet or None
    except Exception as exc:  # noqa: BLE001
        log.warning("standing: could not read the chain budget: %s", type(exc).__name__)
        wallet = None

    return VenueEnv(
        api_key=api_key,
        signing_key=signing_key,
        automation_enabled=automation,
        cli_present=cli_present,
        wallet=wallet,
        price_unit=_opt_str(block, "standing_price_unit"),
        priority_fee=_opt_str(block, "standing_priority_fee_sol"),
        tip_fee=_opt_str(block, "standing_tip_fee_sol"),
    )


@dataclass(frozen=True)
class FeatureState:
    """Whether server-side protection is configured, possible, and therefore real.

    ``configured`` is what ``config/risk.yaml`` says. ``available`` is what the machine and
    the account can actually do. **Only** :attr:`effective` may be reported as protection;
    a dashboard that shows ``use_provider_orders: true`` while ``available`` is false is the
    original hole with a green light on it.
    """

    configured: bool
    available: bool
    blockers: tuple[str, ...] = ()

    @property
    def effective(self) -> bool:
        return self.configured and self.available

    def as_payload(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "available": self.available,
            "effective": self.effective,
            "blockers": list(self.blockers),
        }


def feature_state(
    chain: Chain,
    *,
    cfg: ProtectionConfig | None = None,
    env: VenueEnv | None = None,
    block: dict[str, Any] | None = None,
) -> FeatureState:
    """Everything that must be true before a standing order can exist. Never raises."""
    block = _protection_block() if block is None else block
    cfg = cfg or protection_config(block or None)
    env = venue_env(chain, block=block) if env is None else env

    blockers: list[str] = []
    if chain.value in NO_CONDITION_ORDER_CHAINS:
        blockers.append(f"chain_unsupported:{chain.value}")
    if not env.cli_present:
        blockers.append("gmgn_cli_not_found")
    if not env.api_key:
        blockers.append("gmgn_api_key_missing")
    if not env.signing_key:
        # `order strategy create|list|cancel` are authSignedRequest; the API key alone is
        # a 401 AUTH_SIGNATURE_INVALID, which is a silent non-placement if we let it fly.
        blockers.append("gmgn_signing_key_missing")
    if not env.automation_enabled:
        # gmgn-cli's confirmTrade aborts unless GMGN_ALLOW_AUTOMATED_TRADES=1 is set in the
        # operator's own environment. --yes alone is refused by the CLI, by design.
        blockers.append("automation_not_enabled")
    if not env.wallet:
        blockers.append("wallet_unbound_in_risk_yaml")
    if env.price_unit not in {"usd", "quote"}:
        # `--check-price` is an absolute number and gmgn-cli does not document whether it
        # is quoted in USD or in the quote token. Guessing is a ~100x error on SOL, in the
        # direction of a stop that never fires. The operator declares it; we do not guess.
        blockers.append("standing_price_unit_undeclared")
    if chain is Chain.SOL and not (env.priority_fee and env.tip_fee):
        # gmgn-cli's help marks both as required for SOL. Inventing them would be inventing
        # a cost we then pay on every placement.
        blockers.append("sol_fee_params_unconfigured")

    try:
        from kaiba.core.limiter import limits_for

        limits = limits_for(PROVIDER)
        if limits.weight_for(CREATE_ENDPOINT) > limits.capacity:
            blockers.append("gmgn_plan_capacity_below_write_weight")
    except Exception as exc:  # noqa: BLE001 - a limiter problem is a blocker, not a crash
        blockers.append(f"limiter_unreadable:{type(exc).__name__}")

    return FeatureState(
        configured=bool(cfg.use_provider_orders),
        available=not blockers,
        blockers=tuple(blockers),
    )


def effective_use_provider_orders(chain: Chain = Chain.SOL) -> bool:
    """The only correct way to ask whether protection currently survives our downtime."""
    return feature_state(chain).effective


# --------------------------------------------------------------------------------------
# venue transport
# --------------------------------------------------------------------------------------


class Outcome(StrEnum):
    """What a venue call proved. ``AMBIGUOUS`` is never "failed"."""

    OK = "ok"
    REFUSED = "refused"  # positively not sent
    AMBIGUOUS = "ambiguous"  # may or may not have landed
    RATE_LIMITED = "rate_limited"  # refused before anything left this machine


@dataclass(frozen=True)
class VenueResult:
    outcome: Outcome
    data: Any | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def ambiguous(self) -> bool:
        return self.outcome is Outcome.AMBIGUOUS


class CliRunner(Protocol):
    """Executes one already-built, already-authorised argv.

    Argv construction and the withdrawal check live in this module, not in the runner, so a
    test double sees the exact bytes we would have sent and can assert that no destination
    ever appears in them.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        endpoint: str,
        priority: Priority,
        mutating: bool,
        conn: sqlite3.Connection | None = None,
    ) -> VenueResult:
        """Run the CLI. Must never raise."""


_RATE_WORDS = re.compile(r"\b429\b|rate[_ ]limit", re.I)


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child *and* its children. Node leaves workers behind on a plain kill."""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001 - best effort; we still fall through to kill()
        log.debug("standing: process-group kill failed: %s", type(exc).__name__)
    try:
        proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.debug("standing: process kill failed: %s", type(exc).__name__)


class GmgnCliRunner:
    """The real transport: ``gmgn-cli order strategy ...`` as a subprocess.

    Mirrors :func:`kaiba.execution.executor._run_gmgn` deliberately, because the three
    facts it encodes were each paid for in real debugging and all three apply here:

    * the CLI emits UTF-8 and Python's default ANSI decode raises on real output;
    * ``subprocess.run(timeout=...)`` reaps only the direct child, so Node's workers
      survive — hence a new process group and a tree kill;
    * on an HTTP error the CLI does not exit cleanly (Node aborts, Windows reports
      ``3221226505``), so failure is read from the output and never from the exit value.

    ``mutating`` is the important argument. A create or a cancel may already have reached
    the venue, so any failure we cannot positively identify as pre-flight resolves to
    :attr:`Outcome.AMBIGUOUS`. Duplicating a standing order is worse than missing one.

    There is one implementation of this logic too many in the tree now. The right fix is
    for ``executor.py`` to expose its ``_run_gmgn`` publicly; that file is owned elsewhere,
    so this copy carries the same comments instead of silently drifting from it.
    """

    name = "gmgn-cli"

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    def run(
        self,
        argv: Sequence[str],
        *,
        endpoint: str,
        priority: Priority,
        mutating: bool,
        conn: sqlite3.Connection | None = None,
    ) -> VenueResult:
        from kaiba.providers.gmgn_cli import cli_argv, scrub

        base = cli_argv()
        if base is None:
            return VenueResult(Outcome.REFUSED, None, "gmgn-cli not found; set GMGN_CLI_PATH")

        cmd = [*base, *argv]
        if "--raw" not in cmd:
            cmd.append("--raw")
        try:
            with guarded(PROVIDER, endpoint, priority, conn=conn):
                return self._spawn(cmd, mutating=mutating, scrub=scrub)
        except RateLimited as exc:
            # Our own limiter refused: nothing left this machine, so this is not ambiguous.
            return VenueResult(Outcome.RATE_LIMITED, None, f"limiter refused: {exc}")
        except Exception as exc:  # noqa: BLE001 - a transport must not raise at this edge
            detail = scrub(f"unexpected {type(exc).__name__}: {exc}")[:300]
            return VenueResult(Outcome.AMBIGUOUS if mutating else Outcome.REFUSED, None, detail)

    def _spawn(
        self, cmd: Sequence[str], *, mutating: bool, scrub: Callable[[str], str]
    ) -> VenueResult:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(list(cmd), **kwargs)  # noqa: S603 - validated argv, no shell
        except OSError as exc:
            # The process never started, so nothing can have been sent.
            return VenueResult(Outcome.REFUSED, None, scrub(f"could not run gmgn-cli: {exc}")[:300])

        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001 - the tree is already being torn down
                pass
            return VenueResult(
                Outcome.AMBIGUOUS if mutating else Outcome.REFUSED,
                None,
                f"gmgn-cli timed out after {self.timeout_s}s",
            )
        except Exception as exc:  # noqa: BLE001 - unknown failure around a possible send
            _kill_tree(proc)
            return VenueResult(
                Outcome.AMBIGUOUS if mutating else Outcome.REFUSED,
                None,
                scrub(f"gmgn-cli failed unreadably: {type(exc).__name__}: {exc}")[:300],
            )

        stdout, stderr = stdout or "", stderr or ""
        blob = f"{stderr}\n{stdout}"
        if proc.returncode != 0:
            if _RATE_WORDS.search(blob):
                # A 429 is refused upstream before the order is created, so this is honest.
                return VenueResult(Outcome.RATE_LIMITED, None, scrub(blob.strip())[:300])
            detail = scrub(stderr[:300] or stdout[:300])
            return VenueResult(
                Outcome.AMBIGUOUS if mutating else Outcome.REFUSED, None, detail
            )
        try:
            payload = json.loads(stdout or "{}")
        except ValueError as exc:
            # Unreadable output after a POST is exactly the ambiguous case.
            return VenueResult(
                Outcome.AMBIGUOUS if mutating else Outcome.REFUSED,
                None,
                f"gmgn-cli returned unparseable output: {exc}",
            )
        return VenueResult(Outcome.OK, payload, "")


def default_runner() -> CliRunner:
    return GmgnCliRunner()


# --------------------------------------------------------------------------------------
# argv, and the withdrawal gate in front of it
# --------------------------------------------------------------------------------------


#: The complete parameter set a standing order may carry. Compared for **equality**, like
#: ``policy.GMGN_SWAP_BODY_KEYS``: a set that has gained a key is as changed as one that
#: has lost one, and a destination could only ever arrive as a gained key.
CREATE_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "chain",
        "from_address",
        "base_token",
        "quote_token",
        "order_type",
        "sub_order_type",
        "check_price",
        "amount_in_percent",
        "sell_ratio_type",
        "slippage",
        "priority_fee",
        "tip_fee",
    }
)

CANCEL_PARAM_KEYS: frozenset[str] = frozenset({"chain", "from_address", "order_id", "order_type"})


class StandingRefused(Exception):
    """The order was not built and nothing was sent. Always safe to surface."""


def _authorize(operation: str, params: dict[str, Any], expected: frozenset[str]) -> None:
    """Fail closed. A missing policy module means no standing orders, not free ones."""
    try:
        from kaiba.execution.policy import assert_no_withdrawal
    except ImportError as exc:  # pragma: no cover - policy.py is not optional in this tree
        raise StandingRefused(f"policy module unavailable, refusing to send: {exc}") from exc

    keys = set(params)
    if keys != expected:
        extra = sorted(keys - expected)
        missing = sorted(expected - keys)
        raise StandingRefused(
            f"standing param set changed (extra={extra}, missing={missing}); refusing to send"
        )
    # Raises WithdrawalBlocked on a forbidden key or a non-allowlisted operation. It is
    # deliberately not caught: a standing order that trips the withdrawal gate is an
    # incident, not a retry.
    assert_no_withdrawal(operation, params)


def create_params(
    *,
    chain: Chain,
    wallet: str,
    token: str,
    intent: StandingIntent,
    trigger_price: Decimal,
    slippage_pct: str,
    priority_fee: str | None,
    tip_fee: str | None,
) -> dict[str, str]:
    """Build the create parameters. There is no destination argument and cannot be one."""
    params: dict[str, str] = {
        "chain": chain.value,
        "from_address": wallet,
        "base_token": token,
        "quote_token": _native_mint(chain),
        "order_type": VENUE_ORDER_TYPE,
        "sub_order_type": intent.sub_order_type,
        "check_price": _s(trigger_price) or "",
        "amount_in_percent": _s(intent.sell_ratio) or "",
        "sell_ratio_type": GMGN_SELL_RATIO_TYPE,
        "slippage": slippage_pct,
    }
    # gmgn-cli marks both as required on SOL and omits them elsewhere. Keeping the key set
    # exact in both cases is what lets _authorize compare for equality.
    params["priority_fee"] = priority_fee or ""
    params["tip_fee"] = tip_fee or ""
    return params


_FLAG_FOR_PARAM: dict[str, str] = {
    "chain": "--chain",
    "from_address": "--from",
    "base_token": "--base-token",
    "quote_token": "--quote-token",
    "order_type": "--order-type",
    "sub_order_type": "--sub-order-type",
    "check_price": "--check-price",
    "amount_in_percent": "--amount-in-percent",
    "sell_ratio_type": "--sell-ratio-type",
    "slippage": "--slippage",
    "priority_fee": "--priority-fee",
    "tip_fee": "--tip-fee",
    "order_id": "--order-id",
}

#: Order the flags appear in, so argv is deterministic and a test can pin it.
_CREATE_ORDER = (
    "chain",
    "from_address",
    "base_token",
    "quote_token",
    "order_type",
    "sub_order_type",
    "check_price",
    "amount_in_percent",
    "sell_ratio_type",
    "slippage",
    "priority_fee",
    "tip_fee",
)


def create_argv(params: dict[str, str]) -> list[str]:
    argv = ["order", "strategy", "create"]
    for key in _CREATE_ORDER:
        value = params.get(key, "")
        if value == "":
            continue  # an empty fee on a non-SOL chain is an omitted flag, not an empty one
        argv += [_FLAG_FOR_PARAM[key], value]
    argv.append("--yes")
    return argv


def cancel_argv(params: dict[str, str]) -> list[str]:
    argv = ["order", "strategy", "cancel"]
    for key in ("chain", "from_address", "order_id", "order_type"):
        value = params.get(key, "")
        if value:
            argv += [_FLAG_FOR_PARAM[key], value]
    return argv


def entry_condition_order_argv(
    chain: Chain, cfg: ProtectionConfig | None = None
) -> list[str]:
    """The ``swap`` flags that attach the opening ladder to a **buy**, or ``[]``.

    This is the other half of the hole and it is the cheaper half: ``--condition-orders``
    rides on the entry swap, so it costs no extra GMGN weight and it is the only path on
    which the trailing rung (``profit_stop_trace``) can be mirrored at all. It cannot be
    amended afterwards, which is why :func:`sync_position` exists — but a position that
    arrives already carrying its stop is never unprotected in the first place.

    Exposed rather than wired because ``executor.py`` is owned elsewhere. The call site is
    :func:`kaiba.execution.executor.submit_gmgn`, in the ``args`` list built immediately
    before ``_transition(order, OrderState.SUBMITTING, c)``::

        if order.side is Side.BUY:
            args += standing.entry_condition_order_argv(order.chain)

    Two things make that safe to add: the returned list is empty unless
    ``use_provider_orders`` is *effectively* on for the chain, and it adds no key to the
    policy-checked ``gmgn_swap_body`` — the flags travel in argv, and the body
    ``check_gmgn_swap_body`` inspects is unchanged.

    The rendered percentages do not depend on the entry price (``price_scale`` is relative
    and ``loss_stop``'s drop is ``stop_loss_bps``), so a nominal entry of 1 produces
    exactly the array a real fill would, which is what lets this run before the fill.
    """
    cfg = cfg or protection_config()
    if not feature_state(chain, cfg=cfg).effective:
        return []
    from kaiba.execution.protection import initial_state

    orders = to_gmgn_condition_orders(initial_state("entry", 1, cfg), cfg, chain=chain.value)
    if not orders:
        return []
    return [
        "--condition-orders",
        json.dumps(orders, separators=(",", ":")),
        # risk.yaml documents tp_ladder as "pct of remaining"; GMGN defaults to
        # buy_amount, so omitting this silently rescales the whole ladder.
        "--sell-ratio-type",
        GMGN_SELL_RATIO_TYPE,
    ]


def list_argv(chain: Chain, wallet: str, token: str | None = None) -> list[str]:
    argv = ["order", "strategy", "list", "--chain", chain.value, "--from", wallet, "--type", "open"]
    if token:
        argv += ["--base-token", token]
    return argv


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------


class StandingState(StrEnum):
    PLACING = "placing"
    LIVE = "live"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    GONE = "gone"


#: States in which the venue may still be holding this rung. Exactly the set the partial
#: unique index in ``022_standing.sql`` guards.
OPEN_STATES: frozenset[str] = frozenset(
    {StandingState.PLACING.value, StandingState.LIVE.value, StandingState.UNKNOWN.value}
)


class Owner(StrEnum):
    VENUE = "venue"
    WATCHDOG = "watchdog"
    CONTESTED = "contested"


def rows_for(
    position_id: str, conn: sqlite3.Connection | None = None, *, open_only: bool = True
) -> list[dict[str, Any]]:
    c = conn or get_conn()
    sql = "SELECT * FROM standing_orders WHERE position_id=?"
    if open_only:
        sql += " AND state IN ('placing','live','unknown')"
    return fetch_all(c, sql + " ORDER BY id", (position_id,))


def _emit(
    event: str,
    payload: dict[str, Any],
    *,
    conn: sqlite3.Connection | None = None,
    level: str = "info",
    chain: Chain | None = None,
    subject: str | None = None,
    kind: EventKind = EventKind.SYSTEM,
    dedupe_key: str | None = None,
) -> None:
    try:
        ev.emit(
            kind,
            {"service": SERVICE, "event": event, **payload},
            chain=chain,
            subject=subject,
            level=level,
            conn=conn,
            dedupe_key=dedupe_key,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry never breaks protection
        log.warning("standing: could not emit %s: %s", event, exc)


def _settle(
    conn: sqlite3.Connection,
    row_id: int,
    state: StandingState,
    *,
    owner: Owner | None = None,
    detail: str = "",
    ts: int | None = None,
) -> None:
    """Close a row out. Terminal states only; use :func:`_mark_unknown` for an ambiguity."""
    ts = ts or now_ms()
    conn.execute(
        "UPDATE standing_orders SET state=?, owner=COALESCE(?, owner), settled_ms=?, "
        "updated_ms=?, detail=? WHERE id=?",
        (state.value, owner.value if owner else None, ts, ts, detail[:300], row_id),
    )


def _mark_unknown(conn: sqlite3.Connection, row_id: int, detail: str, ts: int) -> None:
    """Latch a row as ambiguous: still open, owned by nobody, never re-placed.

    ``unknown`` stays inside the partial unique index's state set on purpose, so the
    database itself refuses a second placement for the rung until reconciliation moves it.
    ``settled_ms`` is deliberately left NULL: nothing about this row is settled.
    """
    conn.execute(
        "UPDATE standing_orders SET state=?, owner=?, settled_ms=NULL, updated_ms=?, detail=? "
        "WHERE id=?",
        (StandingState.UNKNOWN.value, Owner.CONTESTED.value, ts, detail[:300], row_id),
    )


def _note_sync(
    conn: sqlite3.Connection,
    position_id: str,
    *,
    ts: int,
    wrote: bool,
    unprotected_reason: str | None,
    detail: str | None = None,
) -> None:
    existing = fetch_one(
        conn, "SELECT writes FROM standing_sync WHERE position_id=?", (position_id,)
    )
    writes = int((existing or {}).get("writes") or 0) + (1 if wrote else 0)
    row: dict[str, Any] = {
        "position_id": position_id,
        "last_sync_ms": ts,
        "writes": writes,
        "unprotected_reason": unprotected_reason,
        "detail": (detail or "")[:300] or None,
        "updated_ms": ts,
    }
    if wrote:
        row["last_write_ms"] = ts
    upsert(conn, "standing_sync", row, ["position_id"])


# --------------------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------------------


@dataclass
class SyncReport:
    """What one sync pass did to one position. Returned, and folded into the heartbeat."""

    position_id: str
    placed: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: str | None = None
    unprotected_reason: str | None = None

    @property
    def writes(self) -> int:
        return len(self.placed) + len(self.replaced) + len(self.ambiguous) + len(self.failed)

    def as_payload(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "placed": self.placed,
            "replaced": self.replaced,
            "left_alone": self.left_alone,
            "ambiguous": self.ambiguous,
            "failed": self.failed,
            "skipped": self.skipped,
            "unprotected_reason": self.unprotected_reason,
        }


def _int_opt(block: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(block.get(key, default))
    except (TypeError, ValueError):
        return default


def _needs_replace(live: Decimal | None, desired: Decimal, reprice_bps: int) -> tuple[bool, str]:
    """Decide whether a live venue trigger is close enough to the ladder to leave alone.

    Three answers, and the middle one is the whole point of reconciling cheaply:

    * the ladder wants a **lower** trigger than the venue already holds — never replace.
      The ratchet only raises a stop, so a lower desired stop means our state went
      backwards, which is a bug to report and not an order to send;
    * the difference is under ``reprice_bps`` — leave it alone. Re-placing a stop for a
      0.1% ratchet move costs weight 10 and buys nothing;
    * otherwise replace, cancel first.
    """
    if live is None:
        return True, "no_live_trigger"
    if desired < live:
        return False, "desired_below_live"
    if live <= 0:
        return True, "live_trigger_unreadable"
    drift_bps = (desired - live) * Decimal(10_000) / live
    if drift_bps < Decimal(reprice_bps):
        return False, f"within_{reprice_bps}bps"
    return True, f"drift_{drift_bps.quantize(Decimal('1'))}bps"


def sync_position(
    position: Position,
    state: ProtectionState,
    *,
    conn: sqlite3.Connection | None = None,
    runner: CliRunner | None = None,
    cfg: ProtectionConfig | None = None,
    native_price_usd: Callable[[], Decimal | None] | Decimal | None = None,
    now: int | None = None,
    force: bool = False,
) -> SyncReport:
    """Bring the venue's standing orders in line with this position's ladder.

    Never raises: a protection failure is an event and a warning, never something that
    unwinds the caller. Never places a rung the ladder has already fired, never re-places
    an ``unknown``, and never writes more than ``standing_max_writes`` orders in one pass.
    """
    c = conn or get_conn()
    ts = now or now_ms()
    report = SyncReport(position_id=position.position_id)
    try:
        return _sync_position(
            position, state, c, runner, cfg, native_price_usd, ts, force, report
        )
    except Exception as exc:  # noqa: BLE001 - protection bookkeeping never breaks the loop
        log.exception("standing: sync failed for %s", position.position_id)
        report.skipped = f"error:{type(exc).__name__}"
        report.unprotected_reason = f"sync_error:{type(exc).__name__}"
        _emit(
            "standing_sync_failed",
            {
                "position_id": position.position_id,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "impact": "this position may have no venue-side stop",
            },
            conn=c,
            level="error",
            chain=position.chain,
            subject=position.token,
        )
        return report


def _resolve_native_price(
    source: Callable[[], Decimal | None] | Decimal | None,
) -> Decimal | None:
    if source is None:
        return None
    if isinstance(source, Decimal):
        return source
    try:
        return _dec(source())
    except Exception as exc:  # noqa: BLE001 - a dead price source is missing data
        log.warning("standing: native price source raised %s", type(exc).__name__)
        return None


def _sync_position(  # noqa: C901 - one linear decision path; splitting it hides the order
    position: Position,
    state: ProtectionState,
    c: sqlite3.Connection,
    runner: CliRunner | None,
    cfg: ProtectionConfig | None,
    native_price_usd: Callable[[], Decimal | None] | Decimal | None,
    ts: int,
    force: bool,
    report: SyncReport,
) -> SyncReport:
    block = _protection_block()
    cfg = cfg or protection_config(block or None)
    env = venue_env(position.chain, block=block)
    state_of_feature = feature_state(position.chain, cfg=cfg, env=env, block=block)

    if not state_of_feature.effective:
        reason = (
            "use_provider_orders_off"
            if not state_of_feature.configured
            else "feature_unavailable:" + ",".join(state_of_feature.blockers)
        )
        report.skipped = reason
        report.unprotected_reason = reason
        if state_of_feature.configured:
            # Configured but impossible is the dishonest state. Say so, loudly, throttled.
            _emit(
                "standing_unavailable",
                {
                    "position_id": position.position_id,
                    **state_of_feature.as_payload(),
                    "impact": (
                        "use_provider_orders is true but no standing order can be placed; "
                        "this position's only stop is in-process"
                    ),
                },
                conn=c,
                level="error",
                chain=position.chain,
                subject=position.token,
                dedupe_key=f"standing_unavailable:{position.position_id}:{','.join(state_of_feature.blockers)}",
            )
            _note_sync(c, position.position_id, ts=ts, wrote=False, unprotected_reason=reason)
        return report

    interval_s = _int_opt(block, "standing_min_sync_interval_s", DEFAULT_MIN_SYNC_INTERVAL_S)
    last = fetch_one(c, "SELECT * FROM standing_sync WHERE position_id=?", (position.position_id,))
    last_ms = int((last or {}).get("last_sync_ms") or 0)
    if not force and last_ms and ts - last_ms < interval_s * 1000:
        report.skipped = f"rate_limited:next_in_{max(0, interval_s - (ts - last_ms) // 1000)}s"
        report.unprotected_reason = (last or {}).get("unprotected_reason")
        return report

    rendering = render_intents(state, chain=position.chain, cfg=cfg)
    if not rendering.ok:
        reason = rendering.reason or "nothing_to_mirror"
        report.skipped = reason
        report.unprotected_reason = reason
        _note_sync(c, position.position_id, ts=ts, wrote=False, unprotected_reason=reason)
        return report

    unit = env.price_unit
    native = _resolve_native_price(native_price_usd) if unit == "quote" else None
    if unit == "quote" and (native is None or native <= 0):
        # Missing data is None with an UNAVAILABLE basis, never a 1.0 that ships a stop at
        # a hundredth of its intended price.
        reason = "native_price_unavailable"
        report.skipped = reason
        report.unprotected_reason = reason
        _emit(
            "standing_price_unit_unconvertible",
            {
                "position_id": position.position_id,
                "price_unit": unit,
                "basis": EvidenceBasis.UNAVAILABLE.value,
                "impact": "no standing order placed; the venue trigger cannot be computed",
            },
            conn=c,
            level="error",
            chain=position.chain,
            subject=position.token,
        )
        _note_sync(c, position.position_id, ts=ts, wrote=False, unprotected_reason=reason)
        return report

    runner = runner or default_runner()
    max_writes = _int_opt(block, "standing_max_writes", DEFAULT_MAX_WRITES)
    reprice_bps = _int_opt(block, "standing_reprice_bps", DEFAULT_REPRICE_BPS)
    slippage_pct = _slippage_pct()

    existing = {r["tag"]: r for r in rows_for(position.position_id, c)}
    wanted = rendering.by_tag()

    # A rung the ladder no longer wants (it fired) must not stay live at the venue.
    for tag, row in existing.items():
        if tag in wanted:
            continue
        if row["state"] == StandingState.UNKNOWN.value:
            continue  # reconciliation owns this one
        _release_row(c, runner, position, row, reason="rung_no_longer_in_ladder", ts=ts)

    writes = 0
    # The stop first: it is the rung whose absence is the hole this module closes.
    ordered = sorted(wanted.values(), key=lambda i: (i.tag != STOP_TAG, i.tag))
    for intent in ordered:
        row = existing.get(intent.tag)
        if row is not None and row["state"] == StandingState.UNKNOWN.value:
            # THE rule: an ambiguous placement is never placed again. Only reconcile,
            # reading the venue's own list, may resolve it. Placing it because we assumed
            # failure is how two standing orders end up selling the same tokens.
            report.ambiguous.append(intent.tag)
            continue
        trigger = _venue_trigger(intent.trigger_price_usd, unit, native)
        if trigger is None:
            report.failed.append(intent.tag)
            continue
        if row is not None and row["state"] in {
            StandingState.LIVE.value,
            StandingState.PLACING.value,
        }:
            live_trigger = _dec(row["trigger_price_usd"])
            replace, why = _needs_replace(live_trigger, intent.trigger_price_usd, reprice_bps)
            if not replace:
                report.left_alone.append(intent.tag)
                if why == "desired_below_live":
                    _emit(
                        "standing_stop_regressed",
                        {
                            "position_id": position.position_id,
                            "tag": intent.tag,
                            "live_trigger_usd": row["trigger_price_usd"],
                            "ladder_trigger_usd": _s(intent.trigger_price_usd),
                            "note": "the ratchet never lowers a stop; the venue order was left alone",
                        },
                        conn=c,
                        level="warn",
                        chain=position.chain,
                        subject=position.token,
                    )
                continue
            if writes >= max_writes:
                report.skipped = report.skipped or "write_budget_exhausted"
                continue
            # Cancel before replace. A failed cancel means we do not place: two live orders
            # for one rung is the double-sell we are here to prevent.
            released = _release_row(c, runner, position, row, reason=f"reprice:{why}", ts=ts)
            if not released:
                report.failed.append(intent.tag)
                continue
            writes += 1
            if _place(c, runner, position, intent, trigger, unit, env, slippage_pct, ts, report):
                report.replaced.append(intent.tag)
            writes += 1
            continue

        if writes >= max_writes:
            report.skipped = report.skipped or "write_budget_exhausted"
            continue
        writes += 1
        if _place(c, runner, position, intent, trigger, unit, env, slippage_pct, ts, report):
            report.placed.append(intent.tag)

    unprotected = _unprotected_reason(position.position_id, c)
    report.unprotected_reason = unprotected
    _note_sync(
        c,
        position.position_id,
        ts=ts,
        wrote=writes > 0,
        unprotected_reason=unprotected,
        detail=",".join(rendering.unmirrorable) or None,
    )
    _mirror_protection_ids(c, position.position_id)
    if unprotected:
        _emit(
            "position_not_venue_protected",
            {
                "position_id": position.position_id,
                "reason": unprotected,
                "basis": EvidenceBasis.UNAVAILABLE.value,
                "impact": "no stop at the venue; if this process dies the position is unwatched",
            },
            conn=c,
            level="error",
            chain=position.chain,
            subject=position.token,
            dedupe_key=(
                f"standing_unprotected:{position.position_id}:{unprotected}:"
                f"{ts // (UNPROTECTED_WARN_INTERVAL_S * 1000)}"
            ),
        )
    return report


def _slippage_pct() -> str:
    try:
        bps = int(get_risk().bounds.max_slippage_bps)
    except Exception as exc:  # noqa: BLE001 - a missing bound is not a licence to guess wide
        log.warning("standing: slippage bound unreadable (%s); using 1%%", type(exc).__name__)
        bps = 100
    return f"{Decimal(bps) / Decimal(100):.2f}"


def _venue_trigger(usd: Decimal, unit: str | None, native: Decimal | None) -> Decimal | None:
    """Convert a USD trigger into the unit the venue was declared to speak."""
    if unit == "usd":
        return usd
    if unit == "quote" and native and native > 0:
        return usd / native
    return None


def _place(
    c: sqlite3.Connection,
    runner: CliRunner,
    position: Position,
    intent: StandingIntent,
    trigger: Decimal,
    unit: str | None,
    env: VenueEnv,
    slippage_pct: str,
    ts: int,
    report: SyncReport,
) -> bool:
    """Reserve a row, send, settle. Returns whether the rung is now live at the venue."""
    wallet = env.wallet or ""
    try:
        params = create_params(
            chain=position.chain,
            wallet=wallet,
            token=position.token,
            intent=intent,
            trigger_price=trigger,
            slippage_pct=slippage_pct,
            priority_fee=env.priority_fee,
            tip_fee=env.tip_fee,
        )
        _authorize("order_strategy", params, CREATE_PARAM_KEYS)
    except StandingRefused as exc:
        report.failed.append(intent.tag)
        _emit(
            "standing_refused",
            {
                "position_id": position.position_id,
                "tag": intent.tag,
                "reason": str(exc)[:300],
                "impact": "no venue-side order for this rung",
            },
            conn=c,
            level="error",
            chain=position.chain,
            subject=position.token,
        )
        return False

    # Reserve before submit: the row exists before the network call, so a crash mid-flight
    # leaves evidence instead of a rung nobody knows whether we placed.
    c.execute(
        "INSERT INTO standing_orders (position_id, chain, token, tag, order_type, sub_order_type, "
        "trigger_price_usd, trigger_price, price_unit, sell_ratio, intent_digest, state, owner, "
        "provider, attempts, created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            position.position_id,
            position.chain.value,
            position.token,
            intent.tag,
            intent.order_type,
            intent.sub_order_type,
            _s(intent.trigger_price_usd),
            _s(trigger),
            unit,
            _s(intent.sell_ratio),
            intent.digest,
            StandingState.PLACING.value,
            Owner.VENUE.value,
            PROVIDER,
            1,
            ts,
            ts,
        ),
    )
    row_id = int(c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    result = runner.run(
        create_argv(params),
        endpoint=CREATE_ENDPOINT,
        priority=Priority.POSITION,
        mutating=True,
        conn=c,
    )

    if result.ok:
        order_id = _order_id_of(result.data)
        c.execute(
            "UPDATE standing_orders SET state=?, owner=?, provider_order_id=?, placed_ms=?, "
            "last_seen_ms=?, updated_ms=?, detail=? WHERE id=?",
            (
                StandingState.LIVE.value,
                Owner.VENUE.value,
                order_id,
                ts,
                ts,
                ts,
                "",
                row_id,
            ),
        )
        _emit(
            "standing_placed",
            {
                "position_id": position.position_id,
                "tag": intent.tag,
                "sub_order_type": intent.sub_order_type,
                "trigger_price_usd": _s(intent.trigger_price_usd),
                "trigger_price": _s(trigger),
                "price_unit": unit,
                "sell_ratio": _s(intent.sell_ratio),
                "provider_order_id": order_id,
            },
            conn=c,
            # SYSTEM rather than PROTECTION_SET on purpose: the watchdog's drain treats a
            # PROTECTION_SET carrying a position_id as an operator protection override, and
            # a placement notice is not one. Filter on payload.service == "standing".
            chain=position.chain,
            subject=position.token,
        )
        return True

    if result.ambiguous:
        _mark_unknown(c, row_id, result.detail, ts)
        report.ambiguous.append(intent.tag)
        _emit(
            "standing_placement_ambiguous",
            {
                "position_id": position.position_id,
                "tag": intent.tag,
                "detail": result.detail,
                "rule": (
                    "a placement we could not read is UNKNOWN, never 'not placed'; it is "
                    "resolved by reconcile against `order strategy list` and never re-sent"
                ),
                "impact": "this rung may or may not exist at the venue",
            },
            conn=c,
            level="error",
            chain=position.chain,
            subject=position.token,
        )
        return False

    _settle(c, row_id, StandingState.FAILED, owner=Owner.WATCHDOG, detail=result.detail, ts=ts)
    report.failed.append(intent.tag)
    _emit(
        "standing_placement_failed",
        {
            "position_id": position.position_id,
            "tag": intent.tag,
            "outcome": result.outcome.value,
            "detail": result.detail,
            "impact": (
                "this rung has no venue-side order; if this process dies it is unprotected"
            ),
        },
        conn=c,
        level="error",
        chain=position.chain,
        subject=position.token,
    )
    return False


def _order_id_of(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    for key in ("order_id", "id", "strategy_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _release_row(
    c: sqlite3.Connection,
    runner: CliRunner,
    position: Position,
    row: dict[str, Any],
    *,
    reason: str,
    ts: int,
) -> bool:
    """Cancel one venue order and take ownership back. Returns whether that is certain.

    A cancel we cannot confirm leaves the row ``unknown`` and ``contested``: the venue may
    still act on it, so nothing may be placed for that rung, and the operator is told.
    """
    row_id = int(row["id"])
    order_id = row.get("provider_order_id")
    if not order_id:
        # Nothing to cancel at the venue that we can name. A `placing` or `unknown` row
        # without an id is the crash-mid-flight case: it may exist there, so it stays
        # unknown rather than being quietly written off.
        if row["state"] in {StandingState.PLACING.value, StandingState.UNKNOWN.value}:
            _mark_unknown(c, row_id, f"{reason}:no_provider_order_id", ts)
            return False
        _settle(c, row_id, StandingState.SUPERSEDED, owner=Owner.WATCHDOG, detail=reason, ts=ts)
        return True

    wallet = venue_env(position.chain).wallet or ""
    params = {
        "chain": position.chain.value,
        "from_address": wallet,
        "order_id": str(order_id),
        "order_type": VENUE_ORDER_TYPE,
    }
    try:
        _authorize("order_cancel", params, CANCEL_PARAM_KEYS)
    except StandingRefused as exc:
        _mark_unknown(c, row_id, f"cancel_refused:{exc}", ts)
        return False

    result = runner.run(
        cancel_argv(params),
        # EXIT is the highest limiter priority: a cancel is part of selling and must not be
        # starved by discovery work competing for the same GMGN budget.
        endpoint=CANCEL_ENDPOINT,
        priority=Priority.EXIT,
        mutating=True,
        conn=c,
    )
    if result.ok:
        _settle(c, row_id, StandingState.CANCELLED, owner=Owner.WATCHDOG, detail=reason, ts=ts)
        _emit(
            "standing_cancelled",
            {
                "position_id": position.position_id,
                "tag": row["tag"],
                "provider_order_id": order_id,
                "reason": reason,
            },
            conn=c,
            chain=position.chain,
            subject=position.token,
        )
        return True

    _mark_unknown(c, row_id, f"cancel_{result.outcome.value}:{result.detail}", ts)
    _emit(
        "standing_cancel_unconfirmed",
        {
            "position_id": position.position_id,
            "tag": row["tag"],
            "provider_order_id": order_id,
            "outcome": result.outcome.value,
            "detail": result.detail,
            "reason": reason,
            "impact": (
                "the venue may still hold this order while we act on the same tokens; "
                "double-sell exposure until reconciliation resolves it"
            ),
        },
        conn=c,
        level="error",
        chain=position.chain,
        subject=position.token,
    )
    return False


def _unprotected_reason(position_id: str, c: sqlite3.Connection) -> str | None:
    """``None`` only when a live venue stop actually exists for this position."""
    row = fetch_one(
        c,
        "SELECT state FROM standing_orders WHERE position_id=? AND tag=? "
        "AND state IN ('placing','live','unknown') ORDER BY id DESC LIMIT 1",
        (position_id, STOP_TAG),
    )
    if row is None:
        return "no_venue_stop"
    if row["state"] == StandingState.LIVE.value:
        return None
    return f"venue_stop_{row['state']}"


def _mirror_protection_ids(c: sqlite3.Connection, position_id: str) -> None:
    """Keep ``positions.protection_ids_json`` truthful: live venue order ids only."""
    from kaiba.core.db import jdump

    ids = [
        r["provider_order_id"]
        for r in fetch_all(
            c,
            "SELECT provider_order_id FROM standing_orders WHERE position_id=? AND state=?",
            (position_id, StandingState.LIVE.value),
        )
        if r["provider_order_id"]
    ]
    try:
        c.execute(
            "UPDATE positions SET protection_ids_json=? WHERE position_id=?",
            (jdump(ids), position_id),
        )
    except sqlite3.Error as exc:  # bookkeeping must not break protection
        log.warning("standing: could not mirror protection ids for %s: %s", position_id, exc)


# --------------------------------------------------------------------------------------
# ownership: the double-sell rule
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """The result of taking a quantity back from the venue before selling it ourselves.

    ``clean`` says the watchdog is now the only party that can sell these tokens. It is
    **not** permission: the caller sells either way. A contested claim is an incident to
    report, never a reason to hold a position that the ladder says to exit.
    """

    position_id: str
    released: tuple[str, ...] = ()
    contested: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    skipped: str | None = None

    @property
    def clean(self) -> bool:
        return not self.contested

    def as_payload(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "released": list(self.released),
            "contested": list(self.contested),
            "absent": list(self.absent),
            "skipped": self.skipped,
            "clean": self.clean,
        }


def tags_to_release(
    rows: Iterable[dict[str, Any]], *, pct: Decimal, price_usd: Decimal | None
) -> list[str]:
    """Which venue rungs overlap the sale we are about to make.

    A **full** exit overlaps everything, including the stop. A **trim** overlaps only the
    take-profit rungs that could fire right now: the stop is written as "sell 100% of what
    is held" (``sell_ratio_type=hold_amount``), which stays exactly correct after a partial
    sale and must not be cancelled — cancelling it would leave the position unprotected for
    the length of the sell. When the price is unknown we cannot tell which rungs are live,
    so every take-profit rung is released; a cancelled take-profit costs upside, a
    duplicated one costs the position.
    """
    tags: list[str] = []
    for row in rows:
        tag = str(row["tag"])
        if pct >= Decimal(100):
            tags.append(tag)
            continue
        if tag == STOP_TAG:
            continue
        trigger = _dec(row.get("trigger_price_usd"))
        if price_usd is None or trigger is None or price_usd >= trigger:
            tags.append(tag)
    return tags


def claim_for_exit(
    position_id: str,
    *,
    chain: Chain,
    token: str = "",
    pct: Decimal | int | str = 100,
    price_usd: Decimal | None = None,
    reason: str = "",
    conn: sqlite3.Connection | None = None,
    runner: CliRunner | None = None,
    now: int | None = None,
) -> Claim:
    """Take ownership of the quantity we are about to sell. **Never blocks an exit.**

    Call this immediately before submitting a sell. It cancels the venue orders that would
    otherwise fire on the same tokens and records who owns what. It returns a
    :class:`Claim` on every path — including a missing table, a dead CLI and an exploding
    runner — because ``execution/risk.py`` is explicit that a brake which stops you selling
    is not a brake, and this must never become one.
    """
    ts = now or now_ms()
    share = _dec(pct) or Decimal(100)
    try:
        c = conn or get_conn()
        rows = rows_for(position_id, c)
        if not rows:
            return Claim(position_id, skipped="no_standing_orders")

        wanted = set(tags_to_release(rows, pct=share, price_usd=_dec(price_usd)))
        if not wanted:
            return Claim(position_id, absent=tuple(r["tag"] for r in rows), skipped="no_overlap")

        runner = runner or default_runner()
        # `_release_row` only needs the identity fields for its argv and its event; the
        # lane and mode are placeholders and never reach the venue.
        position = Position(
            position_id=position_id,
            chain=chain,
            token=token or "",
            lane=Lane.MANUAL,
            mode=LaneMode.SHADOW,
        )
        released: list[str] = []
        contested: list[str] = []
        for row in rows:
            tag = str(row["tag"])
            if tag not in wanted:
                continue
            ok = _release_row(
                c, runner, position, row, reason=f"exit:{reason or 'watchdog'}"[:120], ts=ts
            )
            (released if ok else contested).append(tag)

        claim = Claim(position_id, tuple(released), tuple(contested))
        _mirror_protection_ids(c, position_id)
        _emit(
            "standing_claimed",
            {
                **claim.as_payload(),
                "pct": _s(share),
                "reason": reason,
                "rule": (
                    "one owner per quantity; the venue owns a rung only while its row is "
                    "live. This never refuses an exit."
                ),
                "impact": (
                    "double-sell exposure: the venue may still sell these tokens"
                    if contested
                    else None
                ),
            },
            conn=c,
            level="error" if contested else "info",
            chain=chain,
            subject=token or None,
        )
        return claim
    except Exception as exc:  # noqa: BLE001 - an exit is never held up by this module
        log.exception("standing: claim failed for %s", position_id)
        return Claim(position_id, skipped=f"error:{type(exc).__name__}: {exc}"[:200])


# --------------------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------------------


@dataclass
class ReconcileReport:
    checked: int = 0
    confirmed: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    still_unknown: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    error: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "confirmed": self.confirmed,
            "resolved": self.resolved,
            "gone": self.gone,
            "still_unknown": self.still_unknown,
            "orphans": self.orphans,
            "error": self.error,
        }


def _listed_orders(payload: Any) -> list[dict[str, Any]]:
    """Pull the order array out of whatever envelope the CLI handed back."""
    body = payload
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        body = body["data"]
    if isinstance(body, dict):
        for key in ("orders", "list", "items", "records"):
            if isinstance(body.get(key), list):
                body = body[key]
                break
    return [o for o in body if isinstance(o, dict)] if isinstance(body, list) else []


def _listed_id(order: dict[str, Any]) -> str | None:
    for key in ("order_id", "id", "strategy_id"):
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def reconcile(
    *,
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    runner: CliRunner | None = None,
    now: int | None = None,
) -> ReconcileReport:
    """Compare the venue's live orders against what our rows claim, and repair the gap.

    One ``order strategy list`` call for the whole wallet, not one per position: the write
    budget is the scarce thing and this read is what lets most positions need no write at
    all. Three transitions, and the middle one is the only legitimate way out of
    ``unknown``:

    * ``live`` here, absent there → the venue no longer holds it (filled, expired or
      cancelled elsewhere). Mark ``gone``; the next sync may place it again if the ladder
      still wants it;
    * ``unknown`` here, present there → it landed. Resolve to ``live``. **This is the only
      thing that may clear an ambiguous placement**;
    * ``unknown`` here, absent from a *successful* listing for longer than
      :data:`UNKNOWN_GRACE_MS` → positive evidence it never landed. Resolve to ``failed``.
      A listing that itself failed proves nothing and resolves nothing.
    """
    ts = now or now_ms()
    report = ReconcileReport()
    try:
        c = conn or get_conn()
        env = venue_env(chain)
        if not env.wallet:
            report.error = "wallet_unbound_in_risk_yaml"
            return report
        runner = runner or default_runner()
        result = runner.run(
            list_argv(chain, env.wallet),
            endpoint=LIST_ENDPOINT,
            priority=Priority.UNRESOLVED,
            mutating=False,
            conn=c,
        )
        if not result.ok:
            report.error = f"{result.outcome.value}:{result.detail}"[:300]
            _emit(
                "standing_reconcile_unavailable",
                {
                    "chain": chain.value,
                    "outcome": result.outcome.value,
                    "detail": result.detail,
                    "impact": (
                        "cannot confirm venue-side protection; unknown placements stay unknown"
                    ),
                },
                conn=c,
                level="error",
                chain=chain,
            )
            return report

        listed = _listed_orders(result.data)
        by_id = {oid: o for o in listed if (oid := _listed_id(o))}
        ours = fetch_all(
            c,
            "SELECT * FROM standing_orders WHERE chain=? AND state IN ('placing','live','unknown')",
            (chain.value,),
        )
        report.checked = len(ours)
        seen: set[str] = set()

        for row in ours:
            row_id = int(row["id"])
            label = f"{row['position_id']}:{row['tag']}"
            oid = row["provider_order_id"]
            present = bool(oid) and oid in by_id
            if present:
                seen.add(str(oid))
            if row["state"] == StandingState.LIVE.value:
                if present:
                    c.execute(
                        "UPDATE standing_orders SET last_seen_ms=?, updated_ms=? WHERE id=?",
                        (ts, ts, row_id),
                    )
                    report.confirmed.append(label)
                else:
                    _settle(c, row_id, StandingState.GONE, owner=Owner.WATCHDOG,
                            detail="absent from a successful venue listing", ts=ts)
                    report.gone.append(label)
                    _emit(
                        "standing_gone",
                        {
                            "position_id": row["position_id"],
                            "tag": row["tag"],
                            "provider_order_id": oid,
                            "impact": "this rung is no longer protected at the venue",
                        },
                        conn=c,
                        level="warn",
                        chain=chain,
                        subject=row["token"],
                    )
                continue

            # placing / unknown
            if present:
                c.execute(
                    "UPDATE standing_orders SET state=?, owner=?, last_seen_ms=?, updated_ms=?, "
                    "detail=? WHERE id=?",
                    (
                        StandingState.LIVE.value,
                        Owner.VENUE.value,
                        ts,
                        ts,
                        "resolved by reconcile",
                        row_id,
                    ),
                )
                report.resolved.append(label)
                continue
            age = ts - int(row["updated_ms"] or row["created_ms"] or ts)
            if age >= UNKNOWN_GRACE_MS:
                _settle(c, row_id, StandingState.FAILED, owner=Owner.WATCHDOG,
                        detail="absent from a successful venue listing after the grace window",
                        ts=ts)
                report.gone.append(label)
            else:
                report.still_unknown.append(label)

        for oid, order in by_id.items():
            if oid in seen:
                continue
            known = fetch_one(
                c, "SELECT id FROM standing_orders WHERE provider_order_id=?", (oid,)
            )
            if known:
                continue
            report.orphans.append(oid)
            _emit(
                "standing_orphan",
                {
                    "provider_order_id": oid,
                    "base_token": order.get("base_token") or order.get("token"),
                    "note": (
                        "a strategy order at the venue that this agent did not place; "
                        "left alone — it may be the operator's"
                    ),
                },
                conn=c,
                level="warn",
                chain=chain,
            )
        return report
    except Exception as exc:  # noqa: BLE001 - reconciliation never takes the loop down
        log.exception("standing: reconcile failed")
        report.error = f"{type(exc).__name__}: {exc}"[:300]
        return report


# --------------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectionStatus:
    """What the dashboard and ``kaiba probe`` should read. Never optimistic."""

    position_id: str
    venue_protected: bool
    basis: EvidenceBasis
    stop_trigger_usd: Decimal | None = None
    live_tags: tuple[str, ...] = ()
    unknown_tags: tuple[str, ...] = ()
    contested_tags: tuple[str, ...] = ()
    unprotected_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "venue_protected": self.venue_protected,
            "basis": self.basis.value,
            "stop_trigger_usd": _s(self.stop_trigger_usd),
            "live_tags": list(self.live_tags),
            "unknown_tags": list(self.unknown_tags),
            "contested_tags": list(self.contested_tags),
            "unprotected_reason": self.unprotected_reason,
        }


def protection_status(
    position_id: str, conn: sqlite3.Connection | None = None
) -> ProtectionStatus:
    """Whether this position's protection would survive us dying right now."""
    try:
        c = conn or get_conn()
        rows = rows_for(position_id, c)
    except Exception as exc:  # noqa: BLE001
        return ProtectionStatus(
            position_id,
            False,
            EvidenceBasis.UNAVAILABLE,
            unprotected_reason=f"unreadable:{type(exc).__name__}",
        )
    live = tuple(r["tag"] for r in rows if r["state"] == StandingState.LIVE.value)
    unknown = tuple(r["tag"] for r in rows if r["state"] == StandingState.UNKNOWN.value)
    contested = tuple(r["tag"] for r in rows if r["owner"] == Owner.CONTESTED.value)
    stop_row = next(
        (r for r in rows if r["tag"] == STOP_TAG and r["state"] == StandingState.LIVE.value), None
    )
    reason = _unprotected_reason(position_id, c)
    protected = reason is None
    return ProtectionStatus(
        position_id=position_id,
        venue_protected=protected,
        basis=EvidenceBasis.PROVIDER_REPORTED if protected else EvidenceBasis.UNAVAILABLE,
        stop_trigger_usd=_dec((stop_row or {}).get("trigger_price_usd")),
        live_tags=live,
        unknown_tags=unknown,
        contested_tags=contested,
        unprotected_reason=reason,
    )


def health(chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """One dict for ``kaiba probe`` and the dashboard.

    ``says_protected`` is the honest answer to "do my stops survive a crash". It is true
    only when the feature is effective *and* every open position has a live venue stop.
    A caller that reports ``config/risk.yaml``'s ``use_provider_orders`` instead of this
    is reproducing the original hole with a green light on it.
    """
    state = feature_state(chain)
    payload: dict[str, Any] = {"feature": state.as_payload(), "chain": chain.value}
    try:
        c = conn or get_conn()
        open_rows = fetch_all(
            c,
            "SELECT position_id FROM positions WHERE closed_ms IS NULL AND CAST(qty AS INTEGER) > 0 "
            "AND chain=?",
            (chain.value,),
        )
        statuses = [protection_status(r["position_id"], c) for r in open_rows]
        unprotected = [s.position_id for s in statuses if not s.venue_protected]
        contested = [s.position_id for s in statuses if s.contested_tags]
        payload.update(
            {
                "open_positions": len(statuses),
                "venue_protected": len(statuses) - len(unprotected),
                "unprotected": unprotected,
                "contested": contested,
                "says_protected": state.effective and not unprotected,
            }
        )
    except Exception as exc:  # noqa: BLE001
        payload.update({"error": f"{type(exc).__name__}: {exc}"[:200], "says_protected": False})
    return payload


__all__ = [
    "CANCEL_ENDPOINT",
    "CANCEL_PARAM_KEYS",
    "CREATE_ENDPOINT",
    "CREATE_PARAM_KEYS",
    "DEFAULT_MAX_WRITES",
    "DEFAULT_MIN_SYNC_INTERVAL_S",
    "DEFAULT_REPRICE_BPS",
    "LIST_ENDPOINT",
    "OPEN_STATES",
    "SERVICE",
    "STOP_TAG",
    "SUB_STOP_LOSS",
    "SUB_TAKE_PROFIT",
    "UNKNOWN_GRACE_MS",
    "VENUE_ORDER_TYPE",
    "Claim",
    "CliRunner",
    "FeatureState",
    "GmgnCliRunner",
    "Outcome",
    "Owner",
    "ProtectionStatus",
    "ReconcileReport",
    "Rendering",
    "StandingIntent",
    "StandingRefused",
    "StandingState",
    "SyncReport",
    "VenueEnv",
    "VenueResult",
    "cancel_argv",
    "claim_for_exit",
    "create_argv",
    "create_params",
    "default_runner",
    "effective_use_provider_orders",
    "entry_condition_order_argv",
    "feature_state",
    "health",
    "list_argv",
    "protection_status",
    "reconcile",
    "render_intents",
    "rows_for",
    "sync_position",
    "tags_to_release",
    "venue_env",
]
