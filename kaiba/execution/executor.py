"""Order submission: the only module that can spend money.

EXIT-1 (Codex, Claude-approved 2026-09-24): pass trusted Order.side to policy;
no order lifecycle, routing or withdrawal-policy changes.

Two lanes, one policy. The GMGN lane goes through the vendor CLI against custodial
wallets; the direct lane builds a transaction and asks our own signer to sign it. Both
pass through :mod:`kaiba.execution.policy` first, and both write the same journal.

Invariants, each of which exists because the prior system got it wrong or never got there:

* **Reserve before submit.** A row in ``orders`` with state ``reserved`` exists before any
  network call, so a crash mid-flight leaves evidence instead of a mystery.
* **An ambiguous send is never retried.** Timeouts and unreadable responses move the order
  to ``UNKNOWN``. Only reconciliation against the chain may resolve it. Blind resubmission
  is how you buy the same token twice with money you did not have.
* **Policy is not optional.** There is no code path from a decision to a signature that
  skips :func:`_authorize`. If the policy module is missing, submission fails closed.
* **Shadow mode never touches the network.** Mode is checked here as well as upstream.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from kaiba.core import events as ev
from kaiba.core import journal
from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import fetch_one, get_conn
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import (
    EVM_ZERO,
    SOL_NATIVE_MINT,
    Chain,
    EventKind,
    Lane,
    LaneMode,
    Order,
    OrderState,
    Side,
    digest,
    now_ms,
)

log = logging.getLogger(__name__)


class ExecutionRefused(Exception):
    """The order was not sent, and no money moved. Safe to surface to the operator."""


class ExecutionAmbiguous(Exception):
    """We may or may not have sent. The order is UNKNOWN until reconciliation says otherwise."""


@dataclass(frozen=True)
class SubmitResult:
    order_id: str
    state: OrderState
    provider_order_id: str | None = None
    tx_hash: str | None = None
    detail: str | None = None


# --------------------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------------------


def _authorize(order: Order, body: dict[str, Any], wallet: str) -> None:
    """Fail closed. A missing policy module means no trading, not free trading."""
    try:
        from kaiba.execution.policy import check_gmgn_swap_body
    except ImportError as exc:  # pragma: no cover - only before policy.py lands
        raise ExecutionRefused(f"policy module unavailable, refusing to submit: {exc}") from exc

    bounds = get_risk().bounds
    decision = check_gmgn_swap_body(
        body, wallet=wallet, chain=order.chain, side=order.side,
        max_slippage_bps=bounds.max_slippage_bps,
        max_exit_slippage_bps=getattr(bounds, "max_exit_slippage_bps", None),
    )
    if not getattr(decision, "allowed", False):
        reason = getattr(decision, "reason", "policy refused")
        ev.emit(
            EventKind.RISK_HALT,
            {"order_id": order.order_id, "reason": reason,
             "findings": list(getattr(decision, "findings", []))},
            chain=order.chain, subject=order.token, level="warn",
        )
        raise ExecutionRefused(reason)


def _check_mode(order: Order) -> None:
    """Gate entries. Never gate exits.

    Every brake in the risk config means "stop taking on risk", and a sell reduces risk.
    An earlier version applied the lane-mode and kill-switch refusals to both sides, which
    meant hitting the kill switch on a live position locked the money in: the watchdog
    would decide to exit, the executor would refuse, and the only way out was a manual
    trade. That is the opposite of what a kill switch is for, and it contradicts
    ``risk.check_exit``, which promises an exit is never blocked.

    The one thing still checked on a sell is the *order's own* mode. A shadow order belongs
    to the paper broker and must not reach a venue. That is deliberately read from the
    order rather than from the lane, so dropping a lane to shadow after a live fill does
    not strand the real position it opened.
    """
    risk = get_risk()
    if order.side is not Side.BUY:
        if order.mode in {LaneMode.OFF, LaneMode.SHADOW}:
            raise ExecutionRefused(
                f"order {order.order_id} is a {order.mode.value} order; it exits on paper"
            )
        return

    effective = risk.effective_mode(order.lane)
    if effective in {LaneMode.OFF, LaneMode.SHADOW}:
        raise ExecutionRefused(f"lane {order.lane.value} is {effective.value}; no live submission")
    if risk.kill_switch:
        raise ExecutionRefused("kill switch engaged")
    if risk.reduce_only:
        raise ExecutionRefused("reduce-only: buys are refused")
    if risk.entries_paused:
        raise ExecutionRefused("entries paused")


# --------------------------------------------------------------------------------------
# order bookkeeping
# --------------------------------------------------------------------------------------


def _persist(order: Order, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO orders (order_id, decision_id, chain, token, side, lane, mode, "
        "input_token, output_token, amount_in, min_out, slippage_bps, state, provider, "
        "provider_order_id, tx_hash, filled_out, fee_native, created_ms, updated_ms, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order.order_id, order.decision_id, order.chain.value, order.token, order.side.value,
            order.lane.value, order.mode.value, order.input_token, order.output_token,
            str(order.amount_in), str(order.min_out), order.slippage_bps, order.state.value,
            order.provider, order.provider_order_id, order.tx_hash,
            None if order.filled_out is None else str(order.filled_out),
            None if order.fee_native is None else str(order.fee_native),
            order.created_ms, order.updated_ms, order.error,
        ),
    )


def _transition(
    order: Order, state: OrderState, conn: sqlite3.Connection, detail: str | None = None
) -> Order:
    """Move an order's state and append to its immutable history."""
    order = order.model_copy(update={"state": state, "updated_ms": now_ms()})
    # Keep the venue complaint at the tail of verbose CLI output. A second
    # head-only truncation here used to undo _error_detail's preservation.
    stored_detail = _error_detail(detail or "", "", limit=500)
    if detail and state in {OrderState.FAILED, OrderState.UNKNOWN}:
        order = order.model_copy(update={"error": stored_detail})
    _persist(order, conn)
    conn.execute(
        "INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
        (order.order_id, now_ms(), state.value, stored_detail),
    )
    kind = {
        OrderState.SUBMITTED: EventKind.ORDER_SUBMITTED,
        OrderState.FILLED: EventKind.ORDER_FILLED,
        OrderState.FAILED: EventKind.ORDER_FAILED,
    }.get(state)
    if kind:
        ev.emit(
            kind,
            {"order_id": order.order_id, "token": order.token, "side": order.side.value,
             "state": state.value, "detail": detail},
            chain=order.chain, subject=order.token, conn=conn,
        )
    return order


def build_order(
    *,
    decision_id: str | None,
    chain: Chain,
    token: str,
    side: Side,
    lane: Lane,
    mode: LaneMode,
    amount_in: int,
    min_out: int,
    slippage_bps: int,
    provider: str = "gmgn",
) -> Order:
    native = SOL_NATIVE_MINT if chain is Chain.SOL else EVM_ZERO
    input_token, output_token = (native, token) if side is Side.BUY else (token, native)
    order_id = "ord:" + digest(
        {"d": decision_id, "c": chain.value, "t": token, "s": side.value,
         "a": amount_in, "ts": now_ms() // 1000}
    )[:20]
    return Order(
        order_id=order_id, decision_id=decision_id, chain=chain, token=token, side=side,
        lane=lane, mode=mode, input_token=input_token, output_token=output_token,
        amount_in=amount_in, min_out=min_out, slippage_bps=slippage_bps, provider=provider,
    )


# --------------------------------------------------------------------------------------
# GMGN lane
# --------------------------------------------------------------------------------------


def _gmgn_cli_path() -> list[str]:
    """Resolve the globally installed gmgn-cli. Node is required; we do not reimplement it."""
    settings = get_settings()
    if settings.gmgn_cli_path:
        p = Path(settings.gmgn_cli_path)
        if p.exists():
            return [str(p)] if p.suffix != ".js" else ["node", str(p)]
    which = shutil.which("gmgn-cli")
    if which:
        return [which]
    import os

    appdata = os.environ.get("APPDATA", "")
    candidate = Path(appdata) / "npm" / "node_modules" / "gmgn-cli" / "dist" / "index.js"
    if candidate.exists():
        return ["node", str(candidate)]
    raise ExecutionRefused("gmgn-cli not found; install it or set GMGN_CLI_PATH")


def gmgn_swap_body(order: Order, wallet: str) -> dict[str, Any]:
    """The exact nine keys GMGN's swap endpoint accepts. Any extra key is a policy failure.

    Slippage goes over the wire as a decimal-percent string ("1.37"), which is what the
    vendor CLI sends; sending bps here silently overpays by a factor of a hundred.
    """
    return {
        "chain": order.chain.value,
        "from_address": wallet,
        "input_token": order.input_token,
        "output_token": order.output_token,
        "input_amount": str(order.amount_in),
        "min_output_amount": str(order.min_out),
        "swap_mode": "ExactIn",
        "slippage": f"{order.slippage_bps / 100:.2f}",
        "auto_slippage": False,
    }


#: ``Rate limit resets at 2026-09-20 09:50`` in the CLI's 429 message.
_RESET_RE = re.compile(r"resets? at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", re.I)


def _retry_after_from(text: str) -> float | None:
    """Turn the CLI's printed reset time into seconds, or ``None`` if it is absent.

    The CLI prints no timezone, so this reads it as naive local time and clamps hard. A
    wrong guess must not be able to produce a multi-hour cooldown or a zero-second one.
    """
    m = _RESET_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return max(1.0, min(3600.0, (when - datetime.now()).total_seconds()))
    return None


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child *and* its children. Node leaves workers behind on a plain kill."""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001 - best effort; we still fall through to kill()
        log.debug("process-group kill failed: %s", type(exc).__name__)
    try:
        proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.debug("process kill failed: %s", type(exc).__name__)


def _run_gmgn(args: list[str], timeout_s: int = 45, *, mutating: bool = False) -> dict[str, Any]:
    """Invoke the CLI. Never logs the environment; the API key stays in the child's env.

    ``mutating`` says whether this invocation could already have sent an order. When it
    could, **any** failure we cannot positively identify as pre-flight becomes
    :class:`ExecutionAmbiguous` rather than :class:`ExecutionRefused`, because FAILED
    invites a retry and a retry on an order that did reach the venue buys twice.

    Three failure modes found against the real gmgn-cli 1.6.1, all of which used to
    escape as something worse than ambiguous:

    * The CLI emits UTF-8 and Python decoded it with the machine's ANSI codepage, so real
      output raised ``UnicodeDecodeError`` from inside ``subprocess.run``
      (``'charmap' codec can't decode byte 0x8d``, reproduced on ``market trending``).
      Token names routinely carry bytes outside cp1252. That exception is not one the
      caller catches, so a swap that may have been submitted left the order stuck in
      SUBMITTING with no transition at all.
    * ``subprocess.run(timeout=...)`` calls ``proc.kill()``, which reaps the direct child
      and leaves Node's workers running. Hence Popen in its own group plus a tree kill.
    * gmgn-cli does not exit cleanly on an HTTP error. Node aborts
      (``Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)``) and Windows reports
      ``3221226505`` (0xC0000409), not 1 and not the HTTP status. So failure is read from
      the output, never from a specific exit value. Do not key new logic off the code.
    """
    cmd = _gmgn_cli_path() + args + ["--raw"]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        # The CLI emits UTF-8. Letting Python guess the ANSI code page raises
        # UnicodeDecodeError on real token names, from inside the subprocess call.
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    # A new group/session is what makes a tree kill possible at all.
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - fixed argv, no shell
    except OSError as exc:
        # The process never started, so nothing can have been sent.
        raise ExecutionRefused(f"could not run gmgn-cli: {exc}") from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the tree is already being torn down
            pass
        raise ExecutionAmbiguous(f"gmgn-cli timed out after {timeout_s}s") from exc
    except Exception as exc:  # noqa: BLE001 - unknown failure around a possible send
        _kill_tree(proc)
        if mutating:
            raise ExecutionAmbiguous(f"gmgn-cli failed unreadably: {type(exc).__name__}: {exc}") from exc
        raise ExecutionRefused(f"gmgn-cli failed: {type(exc).__name__}: {exc}") from exc

    stdout = stdout or ""
    stderr = stderr or ""
    # The 429 marker has been observed on both streams, so read both. Checking only
    # stderr would let a rate limit be reported as an unparseable-output ambiguity.
    blob = f"{stderr}\n{stdout}"
    if proc.returncode != 0:
        if "429" in blob or "rate limit" in blob.lower():
            # Rate limiting is refused before anything is sent, so FAILED is honest here.
            raise RateLimited("gmgn", "provider returned 429", _retry_after_from(blob) or 300)
        detail = _error_detail(stderr, stdout)
        # A refusal that happens BEFORE any network send is never ambiguous. gmgn-cli's
        # interactive confirmation prompt is the one we have MEASURED: on 2026-09-21
        # 16:00:24 the first live stop-loss died with exit 1 and this text, because the
        # protection unit lacked GMGN_ALLOW_AUTOMATED_TRADES=1 and the CLI stopped to ask.
        # Classifying that as AMBIGUOUS put the order in UNKNOWN, the watchdog's rule is
        # "ambiguous is resolved by reconcile, never resent", and reconcile could not
        # resolve it (no provider order id) -- a deadlock that held a -30% stop open to
        # -92%. The prompt is printed before the request is built, so nothing was sent.
        # Hard evidence of a send (an order id, a tx hash) outranks everything below:
        # retrying something already live is the double-send this module exists to prevent.
        # Precedence, strongest evidence first. Getting the ORDER of these wrong is how a
        # rejection becomes a phantom position or a live order becomes a double-send:
        #   1. an order id or a tx hash  -> AMBIGUOUS, always
        #   2. the venue answered 4xx    -> REFUSED, nothing was created
        #   3. the request left us       -> AMBIGUOUS
        #   4. the confirmation prompt   -> REFUSED, it stops before the request is built
        if not _hard_send_evidence(blob):
            # The venue ANSWERED and refused. Nothing was created, so this is not ambiguous.
            # Checked BEFORE `_looks_sent` on purpose: the failure line that carries the
            # status code, `POST /v1/trade/swap failed: HTTP 400`, is itself a weak
            # send-marker, so asking "did it look sent?" first makes this unreachable.
            rejected, why = _venue_rejected(blob)
            if rejected:
                raise ExecutionRefused(f"gmgn-cli: {why}, so no order was created: {detail}")
            # A genuine prompt stop: the banner is there and the CLI never went past it.
            if (
                not _looks_sent(blob)
                and "confirmation required" in detail.lower()
                and _PROCEEDED_ANYWAY not in blob.lower()
            ):
                raise ExecutionRefused(
                    f"gmgn-cli refused before send (confirmation prompt): {detail}"
                )
        if mutating:
            raise ExecutionAmbiguous(f"gmgn-cli exit {proc.returncode}: {detail}")
        raise ExecutionRefused(f"gmgn-cli exit {proc.returncode}: {detail}")
    try:
        return json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        # Unreadable output after a swap POST is exactly the ambiguous case.
        raise ExecutionAmbiguous(f"gmgn-cli returned unparseable output: {exc}") from exc


def submit_gmgn(order: Order, conn: sqlite3.Connection | None = None) -> SubmitResult:
    """Submit through GMGN. Custodial wallets; the API has no transfer endpoint."""
    c = conn or get_conn()
    risk = get_risk()
    budget = risk.chain_budget(order.chain)
    wallet = budget.wallet
    if not wallet:
        raise ExecutionRefused(f"no wallet bound for {order.chain.value} in config/risk.yaml")

    _check_mode(order)
    body = gmgn_swap_body(order, wallet)
    _authorize(order, body, wallet)

    # Only an original, provably unsent BUY can return to the age-bounded planned queue.
    # Never recycle a submitted/UNKNOWN order or one carrying venue identity.
    can_defer = (
        order.side is Side.BUY
        and order.state is OrderState.PLANNED
        and not order.provider_order_id
        and not order.tx_hash
    )
    order = _transition(order, OrderState.RESERVED, c, "policy passed")

    args = [
        "swap",
        "--chain", order.chain.value,
        "--from", wallet,
        "--input-token", order.input_token,
        "--output-token", order.output_token,
        "--amount", str(order.amount_in),
        "--min-output", str(order.min_out),
        "--slippage", body["slippage"],
        "--anti-mev",
        "--yes",
    ]

    # A SELL is an exit -- a stop-loss, a trailing stop, a rug escape -- and it must never
    # queue behind entries and discovery for the same limiter bucket. Found 2026-09-21
    # by adversarial review of the high-volume design: this line hardcoded ENTRY on every
    # swap with no branch on side, so every stop-loss the watchdog fired was submitted
    # at the same priority as a speculative buy. The watchdog reads the price at EXIT
    # (watchdog.py:265) and then handed the sell to a queue where that priority was
    # discarded. Under an entry burst that is how a bounded loss becomes an unbounded one.
    swap_priority = Priority.EXIT if order.side is Side.SELL else Priority.ENTRY
    send_started = False
    try:
        with guarded("gmgn", "trade.swap", swap_priority, conn=c):
            # A refused local reservation has not sent anything. Mark SUBMITTING only
            # after acquiring the slot, before the process can possibly send.
            order = _transition(order, OrderState.SUBMITTING, c)
            send_started = True
            # mutating: this call can already have sent, so an unclassifiable failure
            # must resolve to UNKNOWN rather than FAILED.
            payload = _run_gmgn(args, mutating=True)
    except ExecutionAmbiguous as exc:
        order = _transition(order, OrderState.UNKNOWN, c, str(exc))
        journal.append(
            "observation",
            f"order {order.order_id} ambiguous: {exc}. Reconcile before any retry.",
            subject=order.token, conn=c,
        )
        raise
    except RateLimited as exc:
        if (
            can_defer and not send_started and exc.provider == "gmgn"
            and exc.reason in {"minimum interval", "max inflight", "bucket exhausted"}
        ):
            # The existing consumer retries PLANNED at its normal cadence, re-checks
            # permission/policy and expires using the ORIGINAL created_ms. No sleep,
            # quota bypass, age extension, or retry of any network-side failure.
            order = _transition(
                order, OrderState.PLANNED, c, f"deferred before send: {exc}"
            )
        else:
            order = _transition(order, OrderState.FAILED, c, str(exc))
        raise
    except ExecutionRefused as exc:
        order = _transition(order, OrderState.FAILED, c, str(exc))
        raise

    data = payload.get("data") or payload
    provider_id = str(data.get("order_id") or data.get("id") or "") or None
    tx_hash = data.get("tx_hash") or data.get("hash")
    order = order.model_copy(update={"provider_order_id": provider_id, "tx_hash": tx_hash})
    order = _transition(order, OrderState.SUBMITTED, c, f"provider_order_id={provider_id}")
    return SubmitResult(order.order_id, OrderState.SUBMITTED, provider_id, tx_hash)


#: Markers that a request actually reached the venue. If any appears in a failed
#: invocation's output we must NOT classify it as a pre-send refusal, whatever else the
#: text says: a retry of something already live is the double-send this codebase exists
#: to prevent. Deliberately broad -- a false AMBIGUOUS costs one manual reconcile, a
#: false REFUSED can cost a duplicate position.
#: HARD evidence: an order exists or a transaction was broadcast. Nothing outranks this.
#: If any of these appears, the only safe reading is "something may be live", whatever
#: else the text says -- including a 4xx further down, which would then be a LATER request
#: failing rather than the order never having been created.
_HARD_SEND_MARKERS: tuple[str, ...] = (
    "order_id", "order id", "orderid",
    "tx_hash", "txhash", "signature", "transaction hash",
    "submitted", "broadcast", "landed", "confirmed", "pending confirmation",
)

#: WEAKER evidence: the request left this process, but we cannot tell from the marker
#: alone whether the venue ever saw it. `POST /v1/trade/swap failed: network error` is the
#: measured case -- the socket died with the request in flight, and an order may or may not
#: exist. Ambiguous is the only honest reading, and :func:`_venue_rejected` is the ONE
#: thing that can downgrade it, because a 4xx is the venue itself saying it refused.
_REQUEST_LEFT_MARKERS: tuple[str, ...] = ("post /v1/trade/",)

_SENT_MARKERS: tuple[str, ...] = _HARD_SEND_MARKERS + _REQUEST_LEFT_MARKERS


def _hard_send_evidence(blob: str) -> bool:
    """True when the output names an order or a transaction. Outranks a venue 4xx."""
    low = (blob or "").lower()
    return any(marker in low for marker in _HARD_SEND_MARKERS)


def _looks_sent(blob: str) -> bool:
    """True when a failed invocation's output shows any sign the request left us."""
    low = (blob or "").lower()
    return any(marker in low for marker in _SENT_MARKERS)


#: What gmgn-cli prints when the venue ANSWERED the trade POST. Only the trade endpoint
#: speaks to whether an order exists, so the path is part of the pattern: a 400 from
#: `/v1/wallet/info` says nothing about a swap.
_TRADE_POST_FAILURE = re.compile(
    r"POST\s+(/v\d+/trade/\S*)\s+failed:\s*HTTP\s+(\d{3})", re.IGNORECASE
)


def _venue_rejected(blob: str) -> tuple[bool, str]:
    """``(rejected, why)`` -- did the venue answer the trade POST with a 4xx?

    A 4xx is the venue saying it received the request, parsed it, and refused it. No order
    was created and no ``provider_order_id`` was issued, so there is nothing in flight and
    nothing for :func:`reconcile` to query. That is REFUSED, which is retryable.

    A 5xx, a timeout or a connection error is emphatically NOT this: the request may have
    been accepted and the answer lost. Those stay AMBIGUOUS.

    MEASURED 2026-09-22: a live bsc exit died on ``POST /v1/trade/swap failed: HTTP 400
    code=400``, was classified ambiguous, and deadlocked the position for 23 minutes while
    it fell from 8.75x to 3.79x. See ``tests/test_venue_rejection_is_not_ambiguous.py``.
    """
    match = _TRADE_POST_FAILURE.search(blob or "")
    if match is None:
        return False, ""
    status = int(match.group(2))
    named = _venue_error_name(blob)
    what = f"venue answered HTTP {status}{named} on {match.group(1)}"
    if 400 <= status < 500:
        return True, what
    return False, f"HTTP {status}{named} on {match.group(1)} is not a rejection"


#: The venue's own diagnosis, which gmgn-cli prints on its LAST line.
_VENUE_ERROR = re.compile(r"\berror=(\d+)\s+message=(\w+)", re.IGNORECASE)


def _venue_error_name(blob: str) -> str:
    """`` (GetSwapRouteError/40002700)`` when the venue named its complaint, else ``""``.

    WHY THIS EXISTS. gmgn-cli prints its ``⚠️  Swap — confirmation required`` banner
    FIRST and the venue's real answer LAST, so every refusal *reads* as an automation
    problem. MEASURED 2026-09-24: 272 of 291 failed sells carried that banner, and the
    actual cause on the robinhood positions stuck for 21-46 hours was
    ``error=40002700 message=GetSwapRouteError`` -- no swap route exists, which no
    amount of ``GMGN_ALLOW_AUTOMATED_TRADES`` can fix. Two separate sessions have now
    spent an hour each chasing the banner. Naming the venue's own error in the refusal
    reason puts the diagnosis where the operator and the journal actually look.

    Classification is deliberately UNCHANGED: a named 4xx is still REFUSED, because the
    venue answering at all is what proves no order was created.
    """
    found = _VENUE_ERROR.search(blob or "")
    if found is None:
        return ""
    return f" ({found.group(2)}/{found.group(1)})"


#: The CLI prints this when ``--yes`` and GMGN_ALLOW_AUTOMATED_TRADES=1 are both present,
#: i.e. it did NOT stop to ask. Its ABSENCE under the banner is what proves a prompt stop.
_PROCEEDED_ANYWAY = "proceeding non-interactively"


def _error_detail(stderr: str, stdout: str, limit: int = 1200) -> str:
    """The head AND the tail of a failed invocation.

    gmgn-cli prints a summary banner first and its real complaint last, so a head-only
    slice keeps the decoration and discards the diagnosis. MEASURED 2026-09-22: every
    failed order read "confirmation required" and nothing else, because 300 characters
    did not reach past the banner.
    """
    blob = (stderr or "").strip() or (stdout or "").strip()
    if len(blob) <= limit:
        return blob
    marker = "\n...[middle chars omitted]...\n"
    budget = max(0, limit - len(marker))
    if not budget:
        return blob[:max(0, limit)]
    head = (budget * 2) // 3
    tail = budget - head
    return f"{blob[:head]}{marker}{blob[-tail:]}"


def query_gmgn_order(order: Order, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Ask the provider what happened. The only legitimate way out of UNKNOWN."""
    if not order.provider_order_id:
        raise ExecutionRefused("no provider order id; cannot query")
    with guarded("gmgn", "trade.query_order", Priority.UNRESOLVED, conn=conn):
        return _run_gmgn(
            ["order", "get", "--chain", order.chain.value, "--order-id", order.provider_order_id]
        )


# --------------------------------------------------------------------------------------
# direct lane
# --------------------------------------------------------------------------------------


def submit_direct(order: Order, conn: sqlite3.Connection | None = None) -> SubmitResult:
    """Build, policy-check and sign a transaction ourselves.

    This lane exists so a GMGN outage or plan limit cannot stall the agent, which is
    exactly what happened to the previous system. It requires the signer service; until
    that is deployed the call fails closed rather than silently falling back.
    """
    c = conn or get_conn()
    _check_mode(order)
    try:
        from kaiba.execution.signer import sign_and_send
    except ImportError as exc:
        raise ExecutionRefused(
            "direct lane needs the signer service; use the GMGN lane or deploy it"
        ) from exc

    order = _transition(order, OrderState.RESERVED, c, "policy pending in signer")
    order = _transition(order, OrderState.SUBMITTING, c)
    try:
        tx_hash = sign_and_send(order)
    except TimeoutError as exc:
        order = _transition(order, OrderState.UNKNOWN, c, str(exc))
        raise ExecutionAmbiguous(str(exc)) from exc
    except Exception as exc:
        order = _transition(order, OrderState.FAILED, c, str(exc))
        raise ExecutionRefused(str(exc)) from exc

    order = order.model_copy(update={"tx_hash": tx_hash})
    order = _transition(order, OrderState.SUBMITTED, c, f"tx={tx_hash}")
    return SubmitResult(order.order_id, OrderState.SUBMITTED, None, tx_hash)


# --------------------------------------------------------------------------------------
# entry point and reconciliation
# --------------------------------------------------------------------------------------


def submit(order: Order, conn: sqlite3.Connection | None = None) -> SubmitResult:
    """Route to a lane. Shadow orders are recorded and never sent."""
    c = conn or get_conn()
    if order.mode is LaneMode.SHADOW:
        _persist(order, c)
        return SubmitResult(order.order_id, OrderState.PLANNED, detail="shadow: not sent")
    if order.provider == "gmgn":
        return submit_gmgn(order, c)
    return submit_direct(order, c)


def unresolved_orders(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    c = conn or get_conn()
    from kaiba.core.db import fetch_all

    return fetch_all(
        c,
        "SELECT * FROM orders WHERE state IN (?,?,?) ORDER BY created_ms",
        (OrderState.UNKNOWN.value, OrderState.SUBMITTING.value, OrderState.SUBMITTED.value),
    )


def reconcile(order_id: str, conn: sqlite3.Connection | None = None) -> OrderState:
    """Resolve one order against the provider and the chain.

    This is the only function permitted to move an order out of ``UNKNOWN``.
    """
    c = conn or get_conn()
    row = fetch_one(c, "SELECT * FROM orders WHERE order_id=?", (order_id,))
    if not row:
        raise ExecutionRefused(f"unknown order {order_id}")

    order = Order(
        order_id=row["order_id"], decision_id=row["decision_id"], chain=Chain(row["chain"]),
        token=row["token"], side=Side(row["side"]), lane=Lane(row["lane"]),
        mode=LaneMode(row["mode"]), input_token=row["input_token"],
        output_token=row["output_token"], amount_in=int(row["amount_in"]),
        min_out=int(row["min_out"]), slippage_bps=row["slippage_bps"],
        state=OrderState(row["state"]), provider=row["provider"],
        provider_order_id=row["provider_order_id"], tx_hash=row["tx_hash"],
        created_ms=row["created_ms"], updated_ms=row["updated_ms"],
    )

    # An order the venue REFUSED has no provider_order_id to query, so the branch below
    # cannot reach it and it stays UNKNOWN for good -- and the watchdog will not resubmit
    # an exit while the previous one is unresolved, so that is a position the machine can
    # never close. MEASURED 2026-09-22: four exits in exactly this shape, three of them
    # still holding open positions 1.2-1.8 hours later.
    #
    # All four conditions, because the fourth is what makes this evidence and not a guess:
    # absence of an id is not proof of absence of an order (a POST that timed out may have
    # created one whose answer we lost), whereas a 4xx is the venue stating it created
    # nothing.
    if (
        order.state is OrderState.UNKNOWN
        and not order.provider_order_id
        and not order.tx_hash
    ):
        rejected, why = _venue_rejected(str(row["error"] or ""))
        if rejected:
            order = _transition(
                order, OrderState.FAILED, c,
                f"reconciled: {why}, and we hold no order id or tx hash, so nothing was "
                f"created; the venue's reply is in this order's earlier history",
            )
            journal.append(
                "outcome",
                f"order {order_id} resolved to failed: the venue rejected it before "
                f"creating an order ({why})",
                subject=order.token, conn=c,
            )
            return OrderState.FAILED

    if order.provider == "gmgn" and order.provider_order_id:
        try:
            payload = query_gmgn_order(order, c)
        except (ExecutionRefused, RateLimited, ExecutionAmbiguous) as exc:
            log.warning("reconcile %s could not query provider: %s", order_id, exc)
            return order.state
        data = payload.get("data") or payload
        status = str(data.get("status") or "").lower()
        mapping = {
            "successful": OrderState.FILLED, "confirmed": OrderState.FILLED,
            "processed": OrderState.FILLED, "failed": OrderState.FAILED,
            "expired": OrderState.EXPIRED, "pending": OrderState.SUBMITTED,
        }
        new_state = mapping.get(status)
        if new_state:
            # GMGN nests the fill under `report`. MEASURED 2026-09-21 on the agent's FIRST
            # real fill (od10sold992a43b57ec671d81dbc4b3a8ec0eb67): the payload was
            # {"status":"confirmed","hash":..,"report":{"input_amount":"56250000",
            # "output_amount":"155121046952",..}} and this line read the TOP level, got
            # None, wrote filled_out=None, and apply_fill opened NO POSITION -- so the
            # watchdog did not know 155,121 tokens were sitting in the wallet. Exactly the
            # shape of the feeds bug (right data, wrong key path). Top-level keys are kept
            # as a fallback because the vendor has changed this shape before.
            report = data.get("report") if isinstance(data.get("report"), dict) else {}
            filled = (report.get("output_amount") or data.get("output_amount")
                      or data.get("out_amount"))
            if new_state is OrderState.FILLED and not filled:
                # MEASURED 2026-09-21 on the agent's SECOND real fill
                # (od10sol583185031bd06360f06e74416ec9dadf3): 30 s after the send GMGN
                # answered status "processed" with NO report object, and this function
                # wrote FILLED with filled_out=None; apply_fill had no quantity and opened
                # no position, so 399,830,918,258 tokens sat unprotected until an operator
                # booked them by hand from the "confirmed" read three minutes later, which
                # did carry report.output_amount. "processed" is the provider saying the
                # transaction landed, not that it has parsed the fill. A fill we cannot
                # quantify is not a fill we can account: leave the order in its current
                # (unresolved) state so reconcile_all asks again next pass, and say so.
                log.warning("reconcile %s: provider says %s but reports no fill quantity yet; "
                            "leaving it %s", order_id, status, order.state.value)
                journal.append(
                    "outcome",
                    f"order {order_id} reported {status} without a fill quantity; still "
                    f"{order.state.value}, will re-query",
                    subject=order.token, refs=[data.get("tx_hash") or data.get("hash") or order.tx_hash or ""], conn=c,
                )
                return order.state
            order = order.model_copy(
                update={
                    "tx_hash": data.get("tx_hash") or order.tx_hash,
                    "filled_out": int(filled) if filled else None,
                }
            )
            order = _transition(order, new_state, c, f"reconciled: {status}")
            if new_state is OrderState.FILLED:
                # The order row is not the position. Until the ledger moves, a filled buy
                # is real money the watchdog cannot see and a filled sell never reduces
                # anything. Imported late and guarded so a bookkeeping fault can never
                # undo a transition we have already written, and `apply_fill` is itself
                # idempotent because this function is meant to run again and again.
                try:
                    from kaiba.execution import accounting

                    accounting.apply_fill(
                        order, c,
                        filled_in=report.get("input_amount") or data.get("input_amount"),
                    )
                except Exception:  # noqa: BLE001 - the fill happened; never unwind it
                    log.exception("position ledger unavailable for %s", order_id)
            journal.append(
                "outcome", f"order {order_id} reconciled to {new_state.value}",
                subject=order.token, refs=[order.tx_hash or ""], conn=c,
            )
            return new_state
    return order.state


def reconcile_all(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    """Run by the reconcile timer. Every unresolved order gets one attempt per pass."""
    c = conn or get_conn()
    out: dict[str, str] = {}
    for row in unresolved_orders(c):
        try:
            out[row["order_id"]] = reconcile(row["order_id"], c).value
        except Exception as exc:  # noqa: BLE001 - one bad order must not stop the sweep
            log.warning("reconcile failed for %s: %s", row["order_id"], exc)
            out[row["order_id"]] = f"error: {exc}"
    if out:
        ev.emit(EventKind.SYSTEM, {"reconciled": out}, conn=c)
    return out
