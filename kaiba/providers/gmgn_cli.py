"""Read-only adapter for the vendored ``gmgn-cli`` binary.

GMGN has no usable public HTTP client for the data we need, so the vendor's Node CLI is
the interface. This module is the *read* half of that relationship: token, market,
portfolio, track and quote lookups. It deliberately cannot trade.

Why a whole module rather than ad-hoc ``subprocess.run`` calls at each call site:

* **It cannot submit an order.** ``kaiba.execution.executor`` owns the swap path. Here the
  (group, command) pair is checked against :data:`_ALLOWED` before the process is spawned,
  ``swap``/``multi-swap``/``order get``/``order strategy``/``cooking``/``config`` are not in
  it, and ``GMGN_ALLOW_AUTOMATED_TRADES=0`` is forced into the child's environment. A second
  way to spend money is a second thing that can go wrong with real money.
* **A dead or hostile CLI returns data, not an exception.** Non-zero exit, timeout,
  unreadable stdout and an unexpected payload shape all return ``None`` with an
  ``UNAVAILABLE`` receipt and a ``PROVIDER_ERROR`` event. Never a zero, never a raise.
* **Nothing hangs the trading loop.** Every spawn has a hard timeout and the whole process
  tree is killed when it expires — ``proc.kill()`` alone leaves Node's children behind.
* **Secrets stay in the environment.** No credential is ever placed in ``argv`` (where any
  process on the box can read it) or in a cache path. The CLI reads ``GMGN_API_KEY`` and its
  Ed25519 signing key from its own config; we only inherit the environment. Anything the CLI
  prints is scrubbed before it reaches a receipt, a log line or an event payload.

Two Windows-specific facts that cost real debugging time and are encoded here:

* The CLI emits UTF-8. ``subprocess`` with ``text=True`` and no explicit encoding decodes
  with the ANSI code page and raises ``UnicodeDecodeError`` on perfectly good output, so the
  encoding is pinned.
* On an HTTP error the CLI does not exit cleanly — Node aborts (``Assertion failed:
  !(handle->flags & UV_HANDLE_CLOSING)``) and the exit code arrives as ``3221226505``
  (``0xC0000409``). Failure detection therefore keys off stderr content, not the exit value.

Rate limiting. Endpoint strings are ``family.name`` so a 429 cools down only the family
that earned it. ``order quote`` uses the bare endpoint ``"quote"`` on purpose: the limiter's
weight table for gmgn is keyed on the *full* endpoint string, so ``"quote"`` is the only
spelling that charges the real weight of 10, and it also isolates quotes into their own
cooldown family. Note that GMGN's observed 429 is IP-wide ("IP rate limit exceeded"), so a
family cooldown under-reacts on its own; the provider-wide penalty level that the limiter
raises on the same event is what supplies the global back-pressure.

Credentials are **not** simply inherited, whatever the environment we hand the child says.
``gmgn-cli``'s own ``dist/config.js`` calls ``dotenv({path: ~/.config/gmgn/.env,
override: true})`` before it reads ``process.env``, so that file wins over anything we
export. Setting ``GMGN_API_KEY`` in our ``.env`` while a different value sits in the CLI's
own file is therefore a silent no-op, and the resulting ``401 AUTH_KEY_INVALID`` names
neither file. :func:`credential_source` reports the mismatch — by comparison, never by
value — and the auth note points at it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import (
    EVM_CHAINS,
    Chain,
    EventKind,
    EvidenceBasis,
    Receipt,
    digest,
    now_ms,
)
from kaiba.providers._http import cache_read, cache_write

log = logging.getLogger(__name__)

PROVIDER = "gmgn"

#: Hard ceiling on a single CLI invocation. A hung vendor process must not stall a loop.
DEFAULT_TIMEOUT_S = 45.0

#: GMGN's own request weights, from the vendor's plan table (docs/research/04-data-sources).
#: Reads cost 1; a quote costs the same as a swap.
GMGN_WEIGHTS: dict[str, int] = {"quote": 10, "swap": 10}

#: Weight the Free plan allows per window, observed 2026-09-13. A quote cannot fit in it.
FREE_PLAN_WEIGHT = 5

#: Said verbatim in the receipt and the event so the dashboard shows a purchasable problem.
#:
#: It no longer names a tier to buy. ``order quote`` was confirmed working on 2026-09-21
#: under the PRO subscription — real quote returned, API key only, no private key — so
#: "upgrade to Plus" is advice this account has already taken, and telling an operator to
#: buy something they hold is worse than saying nothing. What stays true is the weight
#: arithmetic, and GMGN attaches its own ``upgrade_message`` to a genuine plan refusal
#: (``dist/client/OpenApiClient.js`` ``buildOpenApiErrorMessage``), which arrives inside
#: the note in parentheses. That text, not ours, is the thing to act on.
PLAN_NOTE = (
    "GMGN plan limit: `order quote` costs weight {w}, and the Free plan allowed {a} per "
    "window. Quotes were confirmed working under the PRO subscription on 2026-09-21, so a "
    "refusal now means the subscription lapsed or the allowance was exceeded — check the "
    "provider's own message below before buying anything."
)

#: (group, command) pairs this module is allowed to execute. Read-only by construction:
#: every money-moving command is absent and there is no code path that can add one.
_ALLOWED: frozenset[tuple[str, str]] = frozenset(
    {
        ("token", "info"),
        ("token", "security"),
        ("token", "holders"),
        ("token", "traders"),
        ("market", "trending"),
        ("market", "trenches"),
        ("market", "signal"),
        ("market", "search"),
        ("portfolio", "stats"),
        ("portfolio", "profits"),
        ("portfolio", "activity"),
        ("portfolio", "holdings"),
        ("portfolio", "info"),
        # Read-only, and load-bearing for exits: `position.qty` is our ledger's
        # arithmetic, the wallet is the truth, and on a token that taxes transfers the
        # two diverge. Asking to sell more than we hold is rejected outright -- MEASURED
        # 2026-09-22, two sol positions unsellable for over an hour at +6.4% and +1.0%
        # over balance. See `watchdog.wallet_token_units`.
        ("portfolio", "token-balance"),
        ("track", "smartmoney"),
        ("track", "kol"),
        ("track", "follow-wallet"),
        ("order", "quote"),
    }
)

#: endpoint -> (ttl_s, stale_grace_s). TTL 0 disables caching; grace 0 refuses to serve an
#: expired value even when the provider is down. Anything a position decision reads gets a
#: short TTL and no grace: a stale holding is worse than a known-missing one.
_TTL: dict[str, tuple[float, float]] = {
    # No grace at all. A stale balance that reads HIGH is exactly the failure this
    # endpoint exists to prevent: it would let an exit ask for tokens we no longer hold.
    "portfolio.token_balance": (5.0, 0.0),
    "token.info": (15.0, 120.0),
    "token.security": (900.0, 3600.0),
    "token.holders": (120.0, 600.0),
    "token.traders": (120.0, 600.0),
    "market.trending": (30.0, 120.0),
    "market.trenches": (15.0, 60.0),
    "market.signal": (20.0, 120.0),
    "market.search": (300.0, 1800.0),
    "portfolio.stats": (300.0, 1800.0),
    "portfolio.profits": (300.0, 1800.0),
    "portfolio.activity": (60.0, 300.0),
    "portfolio.holdings": (30.0, 0.0),
    # The account's own key/wallet binding. Cheap, rarely changes, and the one read that
    # answers "is this key still valid" — no grace, because a stale yes is the answer that
    # sends every other endpoint into a 401 nobody investigates.
    "portfolio.info": (300.0, 0.0),
    "track.smartmoney": (10.0, 0.0),
    "track.kol": (10.0, 0.0),
    "track.follow_wallet": (10.0, 0.0),
    "quote": (0.0, 0.0),
}

#: endpoint -> key holding the payload once the optional ``{"code":..,"data":..}`` envelope
#: is removed. ``None`` means the whole object is the payload. A missing key is treated as a
#: contract break (UNAVAILABLE + PROVIDER_ERROR) rather than quietly handing back the
#: envelope, because silently-wrong shapes are how a scorer ends up reading garbage.
_PAYLOAD_KEY: dict[str, str | None] = {
    "token.info": None,
    "token.security": None,
    "token.holders": "list",
    "token.traders": "list",
    "market.trending": "rank",
    "market.trenches": None,
    "market.signal": None,
    "market.search": None,
    "portfolio.stats": None,
    "portfolio.profits": "list",
    "portfolio.activity": "activities",
    "portfolio.holdings": None,
    # MEASURED 2026-09-22: {"balances":[{"wallet_address":..,"token_address":..,
    # "balance":"47876.517476461","decimal":0,"height":449306003}]}. Unwrapped to the
    # list so a missing key is reported as a contract break here rather than read as an
    # empty wallet by a caller sizing a sell.
    "portfolio.token_balance": "balances",
    # Observed shape is ``{"wallets": [{chain, address, balances: [...]}, ...]}``, but the
    # whole body is handed back rather than unwrapped to ``wallets``: this endpoint is the
    # credential probe, and the one thing it must never do is report a contract break when
    # what it is being asked is "did the key work at all".
    "portfolio.info": None,
    "track.smartmoney": "list",
    "track.kol": "list",
    "track.follow_wallet": "list",
    "quote": None,
}


class Failure(StrEnum):
    """Why a call produced no data. Carried in the receipt note and the error event."""

    NONE = "none"
    NOT_INSTALLED = "not_installed"
    REFUSED_LOCALLY = "refused_locally"
    LIMITER = "limiter"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNPARSEABLE = "unparseable"
    BAD_SHAPE = "bad_shape"
    CLI_ERROR = "cli_error"


class GmgnResult(NamedTuple):
    """``(payload, Receipt)`` as the contract requires, with names for readability.

    Falsy when there is no usable data, matching :class:`kaiba.providers._http.Fetched`.
    Unpacking still works: ``data, receipt = token_info(...)``.
    """

    data: Any | None
    receipt: Receipt

    @property
    def ok(self) -> bool:
        return self.data is not None and self.receipt.basis is not EvidenceBasis.UNAVAILABLE

    def __bool__(self) -> bool:  # noqa: D105 - see class docstring
        return self.ok


# ---------------------------------------------------------------------------- secrets


_SECRET_KV = re.compile(
    r"((?:api[_-]?key|apikey|private[_-]?key|secret|signature|authorization|password|bearer)"
    r"\s*[=:]\s*)(\S+)",
    re.I,
)


def _secret_values() -> list[str]:
    """Literal credential values that must never appear in our output.

    Read fresh each time and never stored in a module global, so nothing can dump them.
    """
    values: list[str] = []
    try:
        from kaiba.core.config import get_settings

        s = get_settings()
        values += [s.gmgn_api_key, s.gmgn_private_key]
    except Exception as exc:  # noqa: BLE001 - config problems must not defeat scrubbing
        log.debug("could not read settings while scrubbing: %s", type(exc).__name__)
    values += [os.environ.get(n, "") for n in ("GMGN_API_KEY", "GMGN_PRIVATE_KEY")]
    return [v for v in values if v and len(v) >= 8]


def scrub(text: str) -> str:
    """Remove credentials from anything that is about to be logged, stored or emitted."""
    if not text:
        return ""
    out = text
    for secret in _secret_values():
        out = out.replace(secret, "<redacted>")
    return _SECRET_KV.sub(r"\1<redacted>", out)


# ------------------------------------------------------------------------- binary path


def cli_argv() -> list[str] | None:
    """Locate the CLI the same way :mod:`kaiba.execution.executor` does, or return ``None``.

    Same three sources in the same order as the executor. The one difference: when the
    resolved entry is a Windows ``.cmd``/``.bat`` shim we swap in the underlying
    ``dist/index.js`` if we can find it, because ``CreateProcess`` runs batch shims through
    ``cmd.exe`` and we would rather not hand provider-supplied token addresses to a shell
    parser. If the shim is all we have, we still use it — the argument validator below is
    the second line of defence.
    """
    try:
        from kaiba.core.config import get_settings

        configured = get_settings().gmgn_cli_path
    except Exception as exc:  # noqa: BLE001 - a missing config is not a crash
        log.debug("settings unavailable while resolving gmgn-cli: %s", type(exc).__name__)
        configured = ""

    if configured:
        p = Path(configured)
        if p.exists():
            return ["node", str(p)] if p.suffix == ".js" else _deshim(p)

    which = shutil.which("gmgn-cli")
    if which:
        return _deshim(Path(which))

    candidate = (
        Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "gmgn-cli" / "dist" / "index.js"
    )
    if candidate.exists():
        return ["node", str(candidate)]
    return None


def _deshim(path: Path) -> list[str]:
    if path.suffix.lower() not in {".cmd", ".bat"}:
        return [str(path)]
    js = path.parent / "node_modules" / "gmgn-cli" / "dist" / "index.js"
    return ["node", str(js)] if js.exists() else [str(path)]


# -------------------------------------------------------------------------- arguments


#: Deliberately narrow. Rejects every shell metacharacter, so even the ``.cmd`` shim path
#: cannot be turned into command injection by a token address that came from a provider.
_SAFE_ARG = re.compile(r"^[A-Za-z0-9 _.:/@+,=-]+$")
_MAX_ARG_LEN = 256


def _reject_reason(args: Sequence[str]) -> str | None:
    if len(args) < 2:
        return "command too short"
    pair = (args[0], args[1])
    if pair not in _ALLOWED:
        return f"{args[0]} {args[1]} is not a read-only command this module may run"
    for a in args:
        if not a or len(a) > _MAX_ARG_LEN:
            return "argument empty or too long"
        if not _SAFE_ARG.match(a):
            return "argument contains characters that are not allowed in an argv value"
    return None


def _chain_value(chain: Chain | str) -> str | None:
    try:
        return Chain(str(chain)).value
    except ValueError:
        return None


def _flag(name: str, value: Any) -> list[str]:
    """One optional flag, omitted entirely when the value is ``None``."""
    if value is None:
        return []
    if isinstance(value, bool):
        return [name, "true" if value else "false"]
    return [name, str(value)]


def _repeated(name: str, values: Iterable[Any] | None) -> list[str]:
    """A repeatable flag. The CLI takes ``--filter a --filter b``, not a comma list."""
    if not values:
        return []
    out: list[str] = []
    for v in values:
        out += [name, str(v)]
    return out


# ------------------------------------------------------------------------- subprocess


@dataclass(frozen=True)
class _Raw:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    spawn_error: str | None = None


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child *and its children*. Node leaves workers behind if you only kill it."""
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
        log.debug("process-group kill failed: %s", type(exc).__name__)
    try:
        proc.kill()
    except Exception as exc:  # noqa: BLE001
        log.debug("process kill failed: %s", type(exc).__name__)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # Belt and braces: even a malformed argv cannot arm an automated trade from this module.
    env["GMGN_ALLOW_AUTOMATED_TRADES"] = "0"
    # Keep stdout pure JSON; an update banner would look like unparseable output.
    env["NO_UPDATE_NOTIFIER"] = "1"
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    # GMGN_DEBUG makes gmgn-cli print the request as a curl line and dump the raw response
    # on every failure. It redacts the `X-APIKEY` header but not the query string, and both
    # streams land in our receipts, our logs and the events table. `scrub` would catch the
    # literal key, but the safe move is for the child never to print it: an operator who
    # exported GMGN_DEBUG=1 for a manual debugging session must not silently turn every
    # provider error in this tree into a credential dump.
    env.pop("GMGN_DEBUG", None)
    # gmgn-cli 1.6.1 retries an API-key read *once* when `x-ratelimit-reset` is inside 5s
    # (dist/client/OpenApiClient.js: authExistRequest passes autoRetryOnRateLimit=true).
    # That hides the 429 from our limiter, so `penalty_level` never climbs and no family
    # cooldown opens — while the retry is itself one of the "repeated requests" the CLI's
    # own message warns "can extend the ban by 5s up to 5 minutes". GMGN's 429 is IP-wide,
    # so an invisible one is the expensive kind. Make the child surface it and let the
    # shared limiter decide when we may call again.
    env["GMGN_RATE_LIMIT_AUTO_RETRY_MAX_WAIT_MS"] = "0"
    return env


def _spawn(argv: list[str], timeout_s: float) -> _Raw:
    """Run the CLI with a hard deadline. Never raises."""
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        # The CLI emits UTF-8; letting Python guess the ANSI code page raises on real output.
        "encoding": "utf-8",
        "errors": "replace",
        "env": _child_env(),
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603 - validated argv, no shell
    except OSError as exc:
        return _Raw(None, "", "", spawn_error=f"{type(exc).__name__}: {exc}")

    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the tree is already being torn down
            out, err = "", ""
        return _Raw(proc.returncode, out or "", err or "", timed_out=True)
    return _Raw(proc.returncode, out or "", err or "", timed_out=False)


# --------------------------------------------------------------------- classification


_HTTP_RE = re.compile(r"HTTP\s+(\d{3})")
_ERRNAME_RE = re.compile(r"\berror=([A-Za-z_]+)")
_RESET_RE = re.compile(
    r"resets? at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)", re.I
)
#: gmgn-cli renders ``x-ratelimit-reset`` as ``<local timestamp> (~<n>s remaining)``
#: (dist/client/OpenApiClient.js ``formatRateLimitReset``). The relative half is the one
#: worth reading: it is computed by the CLI against its own clock at the moment of the
#: failure, so it needs no timezone guess of ours.
_REMAINING_RE = re.compile(r"~\s*(\d+)\s*s\s+remaining", re.I)
#: Deliberately the CLI's *error* shape, not the bare number. The previous pattern was
#: ``\b429\b|rate[_ ]limit`` searched against stdout as well as stderr, and a token whose
#: ``"volume_1h"`` happened to render as ``"429.56"`` therefore classified a perfectly
#: good ``token info`` response as a rate limit: the payload was thrown away, the call was
#: recorded as ``rate_limited``, ``penalty_level`` rose and a 60 s family cooldown opened
#: on a read that had in fact succeeded. Measured at roughly 1 read in 450 during a burst
#: — self-reinforcing, because the cooldown it opens then refuses everything behind it.
_RATE_WORDS = re.compile(
    r"HTTP\s+429|RATE_LIMIT_(?:EXCEEDED|BANNED|BLOCKED)|rate limit (?:resets|exceeded)",
    re.I,
)
_AUTH_WORDS = re.compile(r"\b401\b|\b403\b|AUTH_|signature invalid|unauthor", re.I)
_PLAN_WORDS = re.compile(r"upgrade|subscription|\bplan\b|not allowed|insufficient|permission", re.I)
#: The server saying the key string itself is not one it knows, as opposed to a signature
#: or a plan problem. Distinct because the operator action is distinct.
_BAD_KEY_WORDS = re.compile(r"AUTH_KEY_INVALID|api[ _]key invalid|invalid api[ _]key", re.I)


def _retry_after_from(text: str) -> float | None:
    """Turn the CLI's rate-limit hint into seconds.

    Two spellings, preferred in this order:

    * ``(~42s remaining)`` — the CLI's own countdown, already relative, no clock involved.
    * ``Rate limit resets at 2026-09-20 09:50`` — a local wall-clock timestamp with no
      timezone. The one sample observed matched local time, so it is read as naive local.

    Either way the result is clamped hard: a wrong guess must not produce a multi-hour
    cooldown or a zero-second one.
    """
    m = _REMAINING_RE.search(text)
    if m:
        return max(1.0, min(3600.0, float(m.group(1))))
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


# ------------------------------------------------------------------- credential source


#: gmgn-cli loads this with ``override: true`` before reading ``process.env``, so it beats
#: anything we put in the child's environment.
CLI_GLOBAL_ENV = Path.home() / ".config" / "gmgn" / ".env"


def _key_in_cli_global_env() -> str | None:
    """The ``GMGN_API_KEY`` the CLI will actually use, or ``None`` if that file has none.

    Returned only to be *compared*. No caller may log it, and none does.
    """
    try:
        if not CLI_GLOBAL_ENV.exists():
            return None
        for line in CLI_GLOBAL_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?GMGN_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'") or None
    except OSError as exc:
        log.debug("could not read the gmgn-cli credential file: %s", type(exc).__name__)
    return None


def credential_source() -> str:
    """One line saying *which* key the CLI will use, without ever saying what it is.

    Compares by value and reports only the verdict, so this is safe to put in a receipt,
    an event payload or a test assertion.
    """
    try:
        from kaiba.core.config import get_settings

        ours = (get_settings().gmgn_api_key or "").strip()
    except Exception as exc:  # noqa: BLE001 - a missing config must not break diagnosis
        log.debug("settings unavailable while diagnosing credentials: %s", type(exc).__name__)
        ours = ""
    theirs = _key_in_cli_global_env()

    if theirs is None:
        if not ours:
            return "no GMGN_API_KEY configured anywhere gmgn-cli looks"
        return f"gmgn-cli inherits our GMGN_API_KEY ({CLI_GLOBAL_ENV} has none)"
    if not ours:
        return f"gmgn-cli uses the key in {CLI_GLOBAL_ENV}; we have none configured"
    if theirs == ours:
        return f"gmgn-cli uses the key in {CLI_GLOBAL_ENV}, which matches ours"
    return (
        f"{CLI_GLOBAL_ENV} holds a DIFFERENT GMGN_API_KEY and is loaded with override=true, "
        "so gmgn-cli is ignoring the one in our settings; update that file, not .env"
    )


def _classify(raw: _Raw) -> tuple[Failure, str, float | None]:
    """Decide what happened. Returns ``(failure, note, retry_after_s)``."""
    if raw.spawn_error:
        return Failure.NOT_INSTALLED, f"could not start gmgn-cli: {raw.spawn_error}", None
    if raw.timed_out:
        return Failure.TIMEOUT, "gmgn-cli exceeded its deadline and its process tree was killed", None

    # gmgn-cli writes diagnostics to stderr and nothing but JSON to stdout, so a healthy
    # payload must not be searched for failure words at all. Every recorded failure —
    # 429, 401, bad address, bad chain — carries its message on stderr with stdout empty.
    # stdout joins the search only when the process itself failed, which is the one case
    # where it might hold a message rather than a response.
    blob = raw.stderr if raw.returncode in (0, None) else f"{raw.stderr}\n{raw.stdout}"
    if _RATE_WORDS.search(blob):
        return Failure.RATE_LIMITED, _first_line(raw) or "provider returned 429", _retry_after_from(blob)
    if raw.returncode not in (0, None):
        if _AUTH_WORDS.search(blob):
            return Failure.AUTH, _first_line(raw) or "provider rejected our credentials", None
        http = _HTTP_RE.search(blob)
        if http and http.group(1).startswith("4"):
            return Failure.BAD_REQUEST, _first_line(raw) or f"provider returned {http.group(1)}", None
        return Failure.CLI_ERROR, _first_line(raw) or f"gmgn-cli exited {raw.returncode}", None
    if _AUTH_WORDS.search(raw.stderr):
        return Failure.AUTH, _first_line(raw) or "provider rejected our credentials", None
    return Failure.NONE, "", None


def _first_line(raw: _Raw) -> str:
    for stream in (raw.stderr, raw.stdout):
        for line in (stream or "").splitlines():
            if line.strip():
                return scrub(line.strip())[:300]
    return ""


def _is_plan_gated(endpoint: str, failure: Failure, note: str) -> bool:
    """A quote refused for weight is a bill to pay, not an outage. Say so explicitly.

    A rejected *key* is never a plan problem, whatever endpoint it lands on: buying a
    bigger plan does not make an unknown key known, and reporting it as ``plan_gated``
    sends the operator to the checkout page for a problem the checkout page cannot fix.
    """
    if _BAD_KEY_WORDS.search(note):
        return False
    if endpoint == "quote" and failure in {Failure.RATE_LIMITED, Failure.AUTH}:
        return True
    return failure is Failure.AUTH and bool(_PLAN_WORDS.search(note))


def _auth_hint(endpoint: str, note: str) -> str:
    """Append the operator action for the two auth failures that have distinct fixes."""
    if _BAD_KEY_WORDS.search(note):
        return (
            f"{note} — GMGN does not recognise this API key. Re-issue one at "
            f"https://gmgn.ai/ai for the subscribed account and write it to "
            f"{CLI_GLOBAL_ENV}. Note: {credential_source()}"
        )
    if endpoint == "portfolio.holdings":
        return (
            f"{note} — this endpoint is signed; run `gmgn-cli config` to bind an "
            "Ed25519 request key for this wallet"
        )
    return note


# --------------------------------------------------------------------------- payload


def _envelope(payload: Any) -> tuple[Any, str | None]:
    """Strip GMGN's optional ``{"code":0,"data":...}`` wrapper. Returns ``(body, error)``."""
    if isinstance(payload, dict) and isinstance(payload.get("code"), int):
        if payload["code"] != 0:
            msg = str(payload.get("msg") or payload.get("message") or "")[:200]
            return None, f"provider envelope code={payload['code']} {msg}".strip()
        return payload.get("data"), None
    return payload, None


def _extract(endpoint: str, payload: Any) -> tuple[Any, str | None]:
    body, err = _envelope(payload)
    if err:
        return None, err
    key = _PAYLOAD_KEY.get(endpoint)
    if key is None:
        return body, None
    if not isinstance(body, dict) or key not in body:
        got = ",".join(sorted(body)[:8]) if isinstance(body, dict) else type(body).__name__
        return None, f"payload has no {key!r} (got {got}); gmgn-cli output shape changed"
    return body[key], None


# ----------------------------------------------------------------------------- errors


class _CliRateLimited(Exception):
    """Raised inside ``guarded`` so the limiter opens a cooldown for this endpoint family."""

    status_code = 429

    def __init__(self, note: str, retry_after_s: float | None) -> None:
        super().__init__("gmgn-cli reported HTTP 429")
        self.note = note
        self.retry_after_s = retry_after_s


class _CliFailure(Exception):
    """Raised inside ``guarded`` so a failed call is recorded as an error, not a success."""

    def __init__(self, failure: Failure, note: str) -> None:
        super().__init__(f"gmgn-cli {failure.value}")
        self.failure = failure
        self.note = note


def _emit_error(endpoint: str, failure: Failure, note: str, extra: dict[str, Any], conn: Any) -> None:
    """Telemetry must never be the reason a read fails."""
    try:
        from kaiba.core import db
        from kaiba.core import events as ev

        # Resolved here rather than inside emit() so the caller's connection — or the test
        # harness's — is the one that gets written, instead of a second thread-local handle
        # whose INSERT nobody can see.
        target = conn if conn is not None else db.get_conn()
        ev.emit(
            EventKind.PROVIDER_ERROR,
            {
                "provider": PROVIDER,
                "endpoint": endpoint,
                "failure": failure.value,
                "detail": scrub(note)[:300],
                **extra,
            },
            level="warn",
            dedupe_key=f"provider_error:{PROVIDER}:{endpoint}:{failure.value}:{scrub(note)[:60]}",
            conn=target,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("could not record provider error: %s", type(exc).__name__)


def _unavailable(
    endpoint: str,
    failure: Failure,
    note: str,
    *,
    req_digest: str | None = None,
    plan_gated: bool = False,
    retry_after_s: float | None = None,
    conn: Any = None,
) -> GmgnResult:
    clean = scrub(note)
    if plan_gated:
        clean = f"{PLAN_NOTE.format(w=GMGN_WEIGHTS['quote'], a=FREE_PLAN_WEIGHT)} ({clean})"
    extra: dict[str, Any] = {}
    if plan_gated:
        extra = {
            "plan_gated": True,
            "gmgn_weight": GMGN_WEIGHTS.get(endpoint, 1),
            "free_plan_weight": FREE_PLAN_WEIGHT,
            "operator_action": (
                "check the GMGN subscription is still active; quotes were confirmed "
                "working under PRO on 2026-09-21, so this is a lapse or an overspend "
                "rather than a tier that was never bought"
            ),
        }
    if retry_after_s is not None:
        extra["retry_after_s"] = round(retry_after_s, 1)
    _emit_error(endpoint, failure, clean, extra, conn)
    return GmgnResult(
        None,
        Receipt(
            provider=PROVIDER,
            endpoint=endpoint,
            basis=EvidenceBasis.UNAVAILABLE,
            request_digest=req_digest,
            note=clean[:300],
        ),
    )


# ------------------------------------------------------------------------- the runner


def run_read(
    endpoint: str,
    args: Sequence[str],
    *,
    priority: Priority = Priority.RESEARCH,
    ttl_s: float | None = None,
    stale_grace_s: float | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    wait_for_slot_s: float = 0.0,
    conn: Any = None,
) -> GmgnResult:
    """Run one allow-listed read command and return ``(payload, Receipt)``.

    The typed helpers below are thin wrappers over this. Call it directly only for a read
    the helpers do not cover; the allowlist still applies, so it cannot be used to trade.

    ``wait_for_slot_s`` is the contract's rule for anything that makes more than one call
    per logical operation (``docs/CONTRACT.md``, "Provider modules"). gmgn's configured
    floor is 1.2 s, so a caller issuing two reads back to back gets its *second* read
    refused by our own limiter and silently loses half its answer — which is exactly the
    failure the contract describes. Waiting only ever delays a reservation that has not
    been charged yet; a provider 429 is never retried here, because retrying one is how a
    5 s IP ban becomes a 5 minute one.
    """
    # --raw always, so we parse JSON instead of scraping the human-readable table.
    argv = list(args) if "--raw" in args else [*args, "--raw"]
    cache_key = "gmgn-cli " + " ".join(argv)
    req_digest = digest(cache_key)

    reason = _reject_reason(args)
    if reason:
        return _unavailable(
            endpoint, Failure.REFUSED_LOCALLY, reason, req_digest=req_digest, conn=conn
        )

    ttl, grace = _TTL.get(endpoint, (0.0, 0.0))
    ttl = ttl if ttl_s is None else ttl_s
    grace = grace if stale_grace_s is None else stale_grace_s

    if ttl > 0:
        hit = cache_read(PROVIDER, cache_key, ttl, grace)
        if hit is not None:
            data, is_stale, fetched_ms = hit
            return GmgnResult(
                data,
                Receipt(
                    provider=PROVIDER,
                    endpoint=endpoint,
                    observed_at_ms=fetched_ms,
                    basis=EvidenceBasis.STALE if is_stale else EvidenceBasis.CACHED,
                    request_digest=req_digest,
                    response_digest=digest(data),
                ),
            )

    base = cli_argv()
    if base is None:
        return _unavailable(
            endpoint,
            Failure.NOT_INSTALLED,
            "gmgn-cli not found; install it globally or set GMGN_CLI_PATH",
            req_digest=req_digest,
            conn=conn,
        )

    def attempt() -> Any:
        with guarded(PROVIDER, endpoint, priority, conn=conn):
            raw = _spawn([*base, *argv], timeout_s)
            failure, note, retry_after = _classify(raw)
            if failure is Failure.RATE_LIMITED:
                raise _CliRateLimited(note, retry_after)
            if failure is not Failure.NONE:
                raise _CliFailure(failure, note)
            try:
                payload = json.loads(raw.stdout or "")
            except ValueError as exc:
                raise _CliFailure(
                    Failure.UNPARSEABLE, f"stdout was not JSON ({type(exc).__name__})"
                ) from exc
            body, shape_err = _extract(endpoint, payload)
            if shape_err:
                raise _CliFailure(Failure.BAD_SHAPE, shape_err)
            return body

    deadline = time.monotonic() + max(0.0, wait_for_slot_s)
    try:
        while True:
            try:
                data = attempt()
                break
            except RateLimited as exc:
                # Our own limiter, before anything was spent with the provider. Waiting is
                # safe here and losing the call is not; `reserve` charges nothing when it
                # refuses, so this cannot double-bill.
                remaining = deadline - time.monotonic()
                # A cooldown longer than the budget will still be a cooldown when the
                # budget runs out, so sleeping through it only burns a caller's latency to
                # arrive at the same refusal. dyor's tier-1 scan has 7.6 s for everything.
                if remaining <= 0 or exc.retry_after_s > remaining:
                    raise
                time.sleep(min(max(exc.retry_after_s, 0.05), remaining, 5.0))
    except RateLimited as exc:
        # Our own limiter refused before we spent anything with the provider.
        return _unavailable(
            endpoint,
            Failure.LIMITER,
            f"limiter refused: {exc}",
            req_digest=req_digest,
            plan_gated=False,
            retry_after_s=exc.retry_after_s,
            conn=conn,
        )
    except _CliRateLimited as exc:
        return _unavailable(
            endpoint,
            Failure.RATE_LIMITED,
            exc.note,
            req_digest=req_digest,
            plan_gated=_is_plan_gated(endpoint, Failure.RATE_LIMITED, exc.note),
            retry_after_s=exc.retry_after_s,
            conn=conn,
        )
    except _CliFailure as exc:
        if exc.failure is Failure.AUTH:
            exc.note = _auth_hint(endpoint, exc.note)
        return _unavailable(
            endpoint,
            exc.failure,
            exc.note,
            req_digest=req_digest,
            plan_gated=_is_plan_gated(endpoint, exc.failure, exc.note),
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - the contract forbids raising at a provider edge
        return _unavailable(
            endpoint,
            Failure.CLI_ERROR,
            f"unexpected {type(exc).__name__}: {exc}",
            req_digest=req_digest,
            conn=conn,
        )

    if ttl > 0:
        cache_write(PROVIDER, cache_key, data)
    return GmgnResult(
        data,
        Receipt(
            provider=PROVIDER,
            endpoint=endpoint,
            basis=EvidenceBasis.PROVIDER_REPORTED,
            request_digest=req_digest,
            response_digest=digest(data),
        ),
    )


def _read(
    endpoint: str,
    args: list[str],
    chain: Chain | str,
    priority: Priority,
    kw: dict[str, Any],
) -> GmgnResult:
    """Shared prologue: resolve the chain, then hand off to :func:`run_read`."""
    value = _chain_value(chain)
    if value is None:
        return _unavailable(
            endpoint,
            Failure.REFUSED_LOCALLY,
            f"unknown chain {chain!r}; gmgn supports {', '.join(c.value for c in Chain)}",
            conn=kw.get("conn"),
        )
    return run_read(endpoint, [*args, "--chain", value], priority=priority, **kw)


# ------------------------------------------------------------------------------ track


def track_smartmoney(
    chain: Chain | str = Chain.SOL,
    *,
    limit: int = 100,
    side: str | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Recent smart-money trades. ``limit`` is 1–200 per the CLI."""
    args = ["track", "smartmoney", "--limit", str(limit), *_flag("--side", side)]
    return _read("track.smartmoney", args, chain, priority, kw)


def track_kol(
    chain: Chain | str = Chain.SOL,
    *,
    limit: int = 100,
    side: str | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Recent KOL trades. ``limit`` is 1–200 per the CLI."""
    args = ["track", "kol", "--limit", str(limit), *_flag("--side", side)]
    return _read("track.kol", args, chain, priority, kw)


def track_follow_wallet(
    chain: Chain | str = Chain.SOL,
    *,
    wallet: str | None = None,
    limit: int = 10,
    side: str | None = None,
    filters: Sequence[str] | None = None,
    min_amount_usd: float | None = None,
    max_amount_usd: float | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Trades by the wallets followed on the GMGN account. ``limit`` is 1–100."""
    args = [
        "track",
        "follow-wallet",
        "--limit",
        str(limit),
        *_flag("--wallet", wallet),
        *_flag("--side", side),
        *_repeated("--filter", filters),
        *_flag("--min-amount-usd", min_amount_usd),
        *_flag("--max-amount-usd", max_amount_usd),
    ]
    return _read("track.follow_wallet", args, chain, priority, kw)


# -------------------------------------------------------------------------- portfolio


def portfolio_stats(
    wallets: str | Sequence[str],
    chain: Chain | str = Chain.SOL,
    *,
    period: str = "7d",
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Trading statistics for one or more wallets. ``period`` is ``7d`` or ``30d``."""
    ws = [wallets] if isinstance(wallets, str) else list(wallets)
    args = ["portfolio", "stats", "--period", period, *_repeated("--wallet", ws)]
    return _read("portfolio.stats", args, chain, priority, kw)


def portfolio_profits(
    wallets: str | Sequence[str],
    chain: Chain | str = Chain.SOL,
    *,
    period: str = "7d",
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Batch PnL for 1–100 wallets. ``period`` is ``1d``/``7d``/``30d``/``all``."""
    ws = [wallets] if isinstance(wallets, str) else list(wallets)
    args = ["portfolio", "profits", "--period", period, *_repeated("--wallet", ws)]
    return _read("portfolio.profits", args, chain, priority, kw)


def portfolio_activity(
    wallet: str,
    chain: Chain | str = Chain.SOL,
    *,
    token: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    types: Sequence[str] | None = None,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Wallet transaction activity. ``types`` is buy/sell/transferIn/transferOut/add/remove."""
    args = [
        "portfolio",
        "activity",
        "--wallet",
        wallet,
        "--limit",
        str(limit),
        *_flag("--token", token),
        *_flag("--cursor", cursor),
        *_repeated("--type", types),
    ]
    return _read("portfolio.activity", args, chain, priority, kw)


def account_info(
    *,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """The account behind ``GMGN_API_KEY``: its bound wallets and their native balances.

    ``/v1/user/info``, API-key auth, no chain flag — so it does not go through
    :func:`_read`. This is the cheapest read that answers "is this key valid", which is
    the question that has to be settled before any rate-limit or plan measurement means
    anything: a rejected key and a throttled key look identical from a scan's point of
    view, and only one of them can be fixed by paying.

    GMGN's OpenAPI has no plan or quota endpoint — ``dist/client/OpenApiClient.js`` lists
    every path the CLI knows and none of them report a tier — so the subscription level is
    only ever observable as behaviour: the ``upgrade_message`` GMGN attaches to a refusal,
    and the rate at which reads stop being refused.
    """
    return run_read("portfolio.info", ["portfolio", "info"], priority=priority, **kw)


def portfolio_holdings(
    wallet: str,
    chain: Chain | str = Chain.SOL,
    *,
    limit: int = 20,
    cursor: str | None = None,
    order_by: str | None = None,
    direction: str | None = None,
    priority: Priority = Priority.POSITION,
    **kw: Any,
) -> GmgnResult:
    """Wallet token holdings.

    This is a *signed* endpoint: without an Ed25519 request key bound to the wallet the CLI
    returns ``401 AUTH_SIGNATURE_INVALID``, which this module reports as an auth failure with
    the fix in the note. The success payload shape is unverified on this account, so the
    whole object is returned rather than a guessed key.
    """
    args = [
        "portfolio",
        "holdings",
        "--wallet",
        wallet,
        "--limit",
        str(limit),
        *_flag("--cursor", cursor),
        *_flag("--order-by", order_by),
        *_flag("--direction", direction),
    ]
    return _read("portfolio.holdings", args, chain, priority, kw)


# ------------------------------------------------------------------------------ token


def token_info(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Token metadata plus a realtime price block. Cached briefly; the receipt says so."""
    return _read("token.info", ["token", "info", "--address", address], chain, priority, kw)


def token_security(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Honeypot / tax / mint-authority style security metrics."""
    return _read("token.security", ["token", "security", "--address", address], chain, priority, kw)


def token_holders(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    limit: int = 20,
    order_by: str | None = None,
    direction: str | None = None,
    tag: str | None = None,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Top holders. An empty list is real data, not a failure — GMGN returns one for WSOL."""
    args = [
        "token",
        "holders",
        "--address",
        address,
        "--limit",
        str(limit),
        *_flag("--order-by", order_by),
        *_flag("--direction", direction),
        *_flag("--tag", tag),
    ]
    return _read("token.holders", args, chain, priority, kw)


def token_traders(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    limit: int = 20,
    order_by: str | None = None,
    direction: str | None = None,
    tag: str | None = None,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Top traders, optionally filtered to a wallet tag such as ``smart_degen``."""
    args = [
        "token",
        "traders",
        "--address",
        address,
        "--limit",
        str(limit),
        *_flag("--order-by", order_by),
        *_flag("--direction", direction),
        *_flag("--tag", tag),
    ]
    return _read("token.traders", args, chain, priority, kw)


# ------------------------------------------------------------- security properties
#
# Why this section exists at all.
#
# ``kaiba.intelligence.dyor.collect_gmgn`` probes this module for an entry point named
# ``security_properties`` first, then falls back to ``token_security`` and hands whatever
# comes back to ``dyor._unwrap_gmgn``. That fallback has never contributed a single field
# to a dossier, and the reason is one key: GMGN's ``token security`` body contains
# ``"can_sell": 0``. ``_unwrap_gmgn`` checks ``any(k in our_vocabulary for k in payload)``
# to decide whether a payload "already speaks our vocabulary"; ``can_sell`` is in that
# vocabulary, so the raw body is passed straight through and ``dyor.normalize_gmgn`` --
# the translator that would have produced ``top10_pct``, ``lp_burned_pct``, both
# authorities and both taxes -- is never called. ``_claims_from`` then keeps the single
# recognised key, ``can_sell=0``, and ``resolve`` discards it because ``0`` is an ``int``
# and not a ``bool``. Net contribution: nothing, silently, on every scan ever run.
#
# That is why ``bundler_pct`` and ``sniper_pct`` were unknown on 615 of 615 dossiers, and
# why ``top10_pct`` was unknown on 97% of them. It was never the billing plan. Providing
# the preferred entry point, returning our own vocabulary with the right *types*, fixes
# all of it from inside this file -- ``dyor.py`` is not ours to edit.
#
# The second half of the fix is that the two named fields are not on ``token security`` at
# all. They live in the ``stat`` block of ``token info``: ``top_bundler_trader_percentage``
# and ``top70_sniper_hold_rate``. So this reads both endpoints and merges them.
#
# **What GMGN's two numbers actually measure, which is not what ours measure.** Both of
# their names end in a holdings word, and the data says to take that literally: they are
# *current* holdings of wallets GMGN has tagged as bundlers or snipers, not the share
# bought during the launch window. Measured 2026-09-21 against ``bundles.analyse`` over
# 200 pump.fun mints whose tape we hold back to the create slot:
#
# * On mints under an hour old, GMGN reported a non-zero bundler share on 10 of 29 rows
#   where either source saw bundling.
# * On mints 18.7-21.7 h old, GMGN reported ``0`` on **every** one of the 7 rows where our
#   tape still measured a bundle -- 205 percentage points of bundling between them, all of
#   it reported by GMGN as zero.
#
# So their figure decays towards zero as the bundlers sell, and a token that was bundled
# and dumped reads as clean. Ours is a structural fact about the launch block and does not
# decay. That makes GMGN a genuine second opinion only inside the first hour or so, and
# makes it useless as a sole source for a gate that runs on an aged token.
#
# ``dyor.resolve`` adopts the **worse** (maximum) of the two, so a decayed zero can never
# pull our number down -- it can only raise it. That is the safe direction, and it is why
# emitting this field is worth doing even though the two sources disagree on 86% of the
# rows where either sees a bundle. Read a PROVIDER_CONFLICT here as "different clocks",
# not "somebody is wrong".
#
# Cost note: this is two weight-1 reads per scan where the old path made one. At the Free
# budget (refill 0.2/s) that halves DYOR throughput; see the measured budget in the work
# log before assuming the default is still right.


#: Sub-objects GMGN nests facts inside. Flattened into one lookup, outermost wins.
_NESTED_BLOCKS: tuple[str, ...] = ("stat", "dev", "price", "pool", "wallet_tags_stat")

#: GMGN field -> our boolean property, and whether the source is the *risk* polarity.
#: Copied from ``dyor._GMGN_BOOL_MAP`` rather than imported: a provider module importing
#: from ``kaiba.intelligence`` would invert the layering. If the two ever diverge, dyor's
#: is authoritative -- the same convention ``bundles`` applies to ``token_flow``.
_BOOL_MAP: tuple[tuple[str, str, bool], ...] = (
    ("renounced_mint", "mint_authority_revoked", False),
    ("renounced_freeze_account", "freeze_authority_revoked", False),
    ("is_honeypot", "can_sell", True),
    ("can_not_sell", "can_sell", True),
    ("transfer_pausable", "freeze_authority_revoked", True),
    ("slippage_modifiable", "tax_modifiable", False),
    ("is_open_source", "source_verified", False),
    ("is_mutable_metadata", "metadata_mutable", False),
)

#: GMGN fields that only mean anything on Solana. **Skipped entirely on an EVM chain**,
#: where GMGN emits them as constants rather than as answers.
#:
#: Measured 2026-09-21 by re-reading ``token security`` live: ``renounced_mint`` and
#: ``renounced_freeze_account`` came back ``false`` on **38 of 38** EVM tokens -- 31
#: robinhood tokens sampled from ``token_dossiers``, plus PEPE / WBTC / SHIB (eth), CAKE /
#: SafeMoon v1 (bsc) and DEGEN / BRETT (base). The same two fields on Solana carry real
#: answers on the 4 mints read alongside them: ``true`` for WSOL, BONK and 3NZ9..qmJh,
#: ``false`` for 5XZw..uVqQ, which really does have a live mint authority. They are Solana
#: account authorities; an ERC-20 has neither, so there is nothing for GMGN to report and
#: it reports ``false``.
#:
#: The cost of piping that through was total: ``false`` becomes
#: ``mint_authority_revoked=False`` and ``freeze_authority_revoked=False``, which are two
#: separate BLOCKERs in ``dyor.RULES``, so **every** robinhood/base/eth/bsc token GMGN
#: could describe was QUARANTINED on a field that was never measured. In the local DB copy
#: that is 112 of the 1099 robinhood dossiers (the other 987 got no security body at all
#: and fail on ``no_security_coverage``, which this change does not touch). PEPE and SHIB
#: are the proof it is a false negative and not a strict read: neither contract has a mint
#: function, and both are reported here as "mint not renounced".
#:
#: **Not substituted.** GMGN does send ``is_renounced`` on EVM, and it is tempting because
#: it was ``true`` on 37 of the 38 rows above. It is the wrong fact twice over. First, it
#: is *ownership* renouncement, not mint authority: a token can have a dead owner and a
#: public ``mint()``, and asserting ``mint_authority_revoked=True`` from it would swap one
#: invented safety claim for another -- the more dangerous one, because it reads green.
#: Second, it does not even hold as ownership: GMGN says ``is_renounced=true`` for WBTC,
#: whose ``owner()`` answered 0xca06...beb7 when this repo checked the chain on 2026-09-20
#: (see the PEPE/BOBO/WBTC note in ``goplus._normalize_evm``). So the field stays UNKNOWN,
#: ``dyor`` drops to 1/3 critical coverage and raises ``partial_security_coverage``. An
#: honest warning beats a confident wrong answer in both directions.
#:
#: The real EVM mint-authority signal is GoPlus's ``is_mintable``, which
#: ``goplus._normalize_evm`` already reads; the fix for coverage is to run that provider on
#: EVM, not to guess here.
_SOL_ONLY_BOOL_FIELDS: frozenset[str] = frozenset(
    {"renounced_mint", "renounced_freeze_account"}
)

#: Our boolean property -> the value that means "unsafe", for the merge in
#: :func:`_offer_bool`. Mirrors ``dyor.BOOL_PROPERTIES[...].unsafe_value`` for exactly the
#: properties :data:`_BOOL_MAP` can emit, copied rather than imported for the layering
#: reason above; dyor's remains authoritative.
_BOOL_UNSAFE: dict[str, bool] = {
    "can_sell": False,
    "mint_authority_revoked": False,
    "freeze_authority_revoked": False,
    "tax_modifiable": True,
    "source_verified": False,
    "metadata_mutable": True,
}

#: GMGN share field -> our percent property. **Every one of these is read as a 0-1 rate
#: and multiplied by 100**, then range-checked to [0, 100] with anything outside dropped.
#:
#: The scale is *observed*, not documented. ``top_10_holder_rate`` is 0.0106259 for WSOL
#: on both ``token security`` and ``token info``; ``bundler_trader_amount_rate`` is
#: 0.1185/0.1293 on ``market signal``; ``bundler_rate`` is 0.0607/0.1369/0.2263 on
#: ``market trending``; ``top70_sniper_hold_rate`` runs 4.9e-05 to 0.1369 across three
#: endpoints.
#:
#: ``top_bundler_trader_percentage`` is the only per-token bundler field GMGN exposes, and
#: its name says the opposite of its neighbours'. It was **calibrated directly** rather
#: than guessed: ``market trending`` reports ``bundler_rate`` and ``token info`` reports
#: ``top_bundler_trader_percentage`` for the *same* address, so reading both settles it.
#: Measured 2026-09-21 over 8 trending tokens with bundler share 3%-35% (Printer, fomo,
#: OpenAI, SPCX, SCAM, EZO, +++++, UFO): ratio 1.00 on six and 1.01/1.03 on the two where
#: minutes passed between the two reads. It is the same 0-1 rate, despite the name.
#:
#: The range check below stays regardless, because it is the cheap direction to be wrong
#: in: a value that is not a 0-1 rate is dropped, ``bundler_pct`` goes unknown, and
#: ``curve-velocity`` fails closed -- never a small number that would fire the lane on the
#: launches it exists to avoid.
_PCT_MAP: tuple[tuple[str, str], ...] = (
    ("top_10_holder_rate", "top10_pct"),
    ("top70_sniper_hold_rate", "sniper_pct"),
    ("sniper_amount_rate", "sniper_pct"),
    ("top_bundler_trader_percentage", "bundler_pct"),
    ("bundler_trader_amount_rate", "bundler_pct"),
    ("bundler_rate", "bundler_pct"),
    ("suspected_insider_hold_rate", "insider_pct"),
    ("insider_amount_rate", "insider_pct"),
    ("dev_team_hold_rate", "dev_pct"),
    ("creator_hold_rate", "dev_pct"),
    ("dev_token_amount_rate", "dev_pct"),
    ("burn_ratio", "lp_burned_pct"),
)

#: Percent fields whose scale is assumed rather than measured. **Empty**, and a test pins
#: it empty, so adding one is a deliberate act rather than a slip. It held
#: ``top_bundler_trader_percentage`` until the cross-endpoint calibration above settled
#: it. Anything listed here is named in the receipt, so a reader can see which number
#: rests on an assumption.
SCALE_UNVERIFIED: frozenset[str] = frozenset()

#: Properties where a *larger* number is the worse one, so two GMGN spellings of the same
#: idea resolve the way ``dyor.resolve`` would resolve two providers. ``lp_burned_pct`` is
#: absent on purpose: less burn is worse, so it takes the minimum.
_WORSE_IS_MAX: frozenset[str] = frozenset(
    {"top10_pct", "sniper_pct", "bundler_pct", "insider_pct", "dev_pct"}
)

#: GMGN tax field -> our bps property. GMGN reports a 0-1 fraction; 1% is 0.01 is 100 bps.
_BPS_MAP: tuple[tuple[str, str], ...] = (("buy_tax", "buy_tax_bps"), ("sell_tax", "sell_tax_bps"))

#: GMGN field -> our property, taken as-is, with the range that makes it believable.
_PLAIN_MAP: tuple[tuple[str, str, Decimal | None], ...] = (
    ("rug_ratio", "rug_ratio", Decimal(1)),
    ("holder_count", "holder_count", None),
    ("liquidity", "liquidity_usd", None),
    ("price", "price_usd", None),
)

HUNDRED = Decimal(100)
TEN_K = Decimal(10_000)


def _num(value: Any) -> Decimal | None:
    """A GMGN scalar as a Decimal, or ``None``. ``""``, ``null`` and a bool are all None."""
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        got = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    return got if got.is_finite() else None


def _flag_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"1", "true", "yes"}:
        return True
    if s in {"0", "false", "no"}:
        return False
    return None


def flatten_payload(payload: Any) -> dict[str, Any]:
    """GMGN's top level plus :data:`_NESTED_BLOCKS`, as one scalar lookup.

    The outermost value wins, so ``token info``'s top-level ``liquidity`` beats the copy
    inside ``pool``. Non-scalars are skipped at the top level, which is what lets the
    nested ``price.price`` land under ``price`` even though the top-level ``price`` is the
    whole realtime block.
    """
    out: dict[str, Any] = {}
    if not isinstance(payload, Mapping):
        return out
    body = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    if not isinstance(body, Mapping):
        return out
    for key, value in body.items():
        if not isinstance(value, (dict, list)):
            out[key] = value
    for block in _NESTED_BLOCKS:
        inner = body.get(block)
        if not isinstance(inner, Mapping):
            continue
        for key, value in inner.items():
            if not isinstance(value, (dict, list)) and key not in out:
                out[key] = value
    return out


def _offer(out: dict[str, Any], prop: str, value: Decimal) -> None:
    """Keep the worse of two spellings of the same property, per :data:`_WORSE_IS_MAX`."""
    have = out.get(prop)
    if not isinstance(have, Decimal):
        out[prop] = value
        return
    out[prop] = max(have, value) if prop in _WORSE_IS_MAX else min(have, value)


def _offer_bool(out: dict[str, Any], prop: str, value: bool) -> None:
    """Keep the *unsafe* of two spellings of the same boolean, as ``dyor.resolve`` would.

    :data:`_BOOL_MAP` writes ``freeze_authority_revoked`` from two GMGN fields
    (``renounced_freeze_account`` and ``transfer_pausable``) and ``can_sell`` from two more
    (``is_honeypot`` and ``can_not_sell``). With a plain assignment the winner was whichever
    came later in the tuple, which is not a safety argument -- it is a line number.

    The ``can_sell`` pair is the one that could already bite: ``can_not_sell`` is listed
    after ``is_honeypot``, so a body saying ``is_honeypot=1, can_not_sell=0`` resolved to
    ``can_sell=True`` and the ``honeypot`` blocker never fired. Not observed live -- on the
    42 bodies read 2026-09-21 the two never disagreed (``is_honeypot`` false and
    ``can_not_sell`` 0 on all 38 EVM rows, ``is_honeypot`` null on all 4 Solana rows) -- but
    it is fail-open, and one provider contradicting itself about whether a token can be sold
    is not the moment to believe the cheerful half.

    ``transfer_pausable`` is absent on all 42 of those bodies, so the freeze pair has never
    fired either way -- but removing ``renounced_freeze_account`` on EVM changes which entry
    survives, so the ordering must stop deciding this before that lands.
    """
    have = out.get(prop)
    unsafe = _BOOL_UNSAFE.get(prop)
    if not isinstance(have, bool) or unsafe is None:
        out[prop] = value
        return
    out[prop] = unsafe if unsafe in (have, value) else value


def _is_evm(chain: Chain | str | None) -> bool:
    """True only for a chain we can *name* and that is EVM.

    An unrecognised chain reads as non-EVM, which keeps the Solana mapping. That is the
    direction to be wrong in: the Solana mapping applied to an EVM body invents a *refusal*
    (``mint_authority_revoked=False`` is a blocker), while the EVM skip applied to a Solana
    body would drop a real ``renounced_mint=false`` and downgrade a blocker to a coverage
    warning. Only the second one can talk us into a trade.
    """
    if chain is None:
        return False
    try:
        return Chain(str(chain)) in EVM_CHAINS
    except ValueError:
        return False


def normalize_security(
    payload: Any,
    *,
    chain: Chain | str | None = Chain.SOL,
    dropped: list[str] | None = None,
) -> dict[str, Any]:
    """Translate a GMGN ``token security`` and/or ``token info`` body into our vocabulary.

    Returns real ``bool`` and ``Decimal`` values, because ``dyor.resolve`` throws away a
    boolean claim that is not a ``bool`` -- which is precisely how GMGN's ``can_sell: 0``
    has been evaporating on every scan. Anything unrecognised, unparseable or outside a
    believable range is left out and appended to ``dropped``; nothing is ever defaulted,
    because a default that reads as safe is the one failure mode this whole tree is built
    against.

    Also usable as ``dyor._unwrap_gmgn``'s documented ``normalize_security`` hook.

    ``chain`` decides whether :data:`_SOL_ONLY_BOOL_FIELDS` are read as answers or ignored
    as EVM stubs; see that constant for the measurement. It defaults to ``Chain.SOL`` to
    match every other entry point in this module, and because a caller who forgets it can
    then only make us *more* suspicious of an EVM token, never less of a Solana one.
    """
    lost = dropped if dropped is not None else []
    body = flatten_payload(payload)
    out: dict[str, Any] = {}
    if not body:
        return out

    evm = _is_evm(chain)
    for src, prop, is_risk in _BOOL_MAP:
        if src not in body:
            continue
        if evm and src in _SOL_ONLY_BOOL_FIELDS:
            # Not "dropped": nothing was unparseable. GMGN simply has no answer to give on
            # this chain, so the property stays unknown and coverage says so.
            continue
        flag = _flag_bool(body.get(src))
        if flag is None:
            continue
        _offer_bool(out, prop, (not flag) if is_risk else flag)

    for src, prop in _PCT_MAP:
        value = _num(body.get(src))
        if value is None:
            continue
        pct = value * HUNDRED
        if not (0 <= pct <= HUNDRED):
            lost.append(f"{src}={value} is not a 0-1 rate")
            continue
        _offer(out, prop, pct)

    for src, prop in _BPS_MAP:
        value = _num(body.get(src))
        if value is None:
            continue
        bps = value * TEN_K
        if not (0 <= bps <= TEN_K):
            lost.append(f"{src}={value} is not a 0-1 tax fraction")
            continue
        out[prop] = bps

    for src, prop, ceiling in _PLAIN_MAP:
        value = _num(body.get(src))
        if value is None:
            continue
        if value < 0 or (ceiling is not None and value > ceiling):
            lost.append(f"{src}={value} out of range")
            continue
        out[prop] = value

    # Market cap, derived here because this is the one place both units are GMGN's own.
    # MEASURED 2026-09-22: `market_cap_usd` was known on 0 of 7,980 dossiers while
    # `liquidity_usd` was known on 100%. GMGN does not send a market cap on `token info`;
    # it sends the two numbers it is made of, and `flatten_payload` already surfaces both.
    # A rule written against the empty field -- "size up above $500k" -- would have read
    # UNKNOWN forever and never fired, the same shape as `creator_rug_count`.
    #
    # `dyor.NUM_PROPERTIES` omits `total_supply` on purpose, because providers report it
    # in different units and cross-provider comparison would invent conflicts; it adds
    # "each adapter uses its own supply internally, where the units are known". This is
    # that. What leaves here is a market cap, not a supply for anyone else to compare.
    reported = _num(body.get("usd_market_cap") or body.get("market_cap"))
    if reported is not None and reported > 0:
        out["market_cap_usd"] = reported
    else:
        supply = next(
            (v for v in (_num(body.get("circulating_supply")), _num(body.get("total_supply")))
             if v is not None and v > 0),
            None,
        )
        unit_price = _num(body.get("price"))
        if supply is not None and unit_price is not None and unit_price > 0:
            out["market_cap_usd"] = supply * unit_price

    status = str(body.get("creator_token_status") or "").strip().lower()
    if status:
        out["dev_sold"] = status in {"creator_sell", "creator_close", "sell"}

    creator = body.get("creator_address") or body.get("creator")
    if isinstance(creator, str) and creator.strip():
        out["creator"] = creator.strip()
    for text_prop in ("symbol", "name"):
        value = body.get(text_prop)
        if isinstance(value, str) and value.strip():
            out[text_prop] = value.strip()
    return out


def _merged_receipt(parts: Sequence[Receipt], note: str) -> Receipt:
    """One receipt for a merged read, honest about its *weakest* contributor.

    Basis is the worst of the parts and ``observed_at_ms`` the oldest, because a merged
    view is exactly as fresh as its stalest half and ``Measure.stale`` is what the
    execution engine reads.
    """
    order = {
        EvidenceBasis.PROVIDER_REPORTED: 0,
        EvidenceBasis.CACHED: 1,
        EvidenceBasis.STALE: 2,
        EvidenceBasis.UNAVAILABLE: 3,
    }
    worst = max(parts, key=lambda r: order.get(r.basis, 3))
    observed = min((r.observed_at_ms for r in parts if r.observed_at_ms), default=now_ms())
    return Receipt(
        provider=PROVIDER,
        endpoint="+".join(r.endpoint for r in parts),
        observed_at_ms=observed,
        basis=worst.basis,
        note=scrub(note)[:300],
    )


def security_properties(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    conn: Any = None,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = 5.0,
    **kw: Any,
) -> GmgnResult:
    """Everything GMGN knows about one token's safety, in our vocabulary.

    Two weight-1 reads: ``token security`` for the authorities, taxes and burn, and
    ``token info`` for the ``stat`` block -- which is where ``top_bundler_trader_percentage``,
    ``top70_sniper_hold_rate``, ``top_10_holder_rate``, ``dev_team_hold_rate`` and the
    holder count actually live. Either half answering is enough; both failing returns
    ``None`` with ``UNAVAILABLE``, never an empty dict that reads as "nothing wrong".

    ``token info`` is issued second and with ``wait_for_slot_s``, because gmgn's minimum
    interval would otherwise refuse it and this call would quietly return half its fields
    -- see the note on ``run_read``.

    ``chain`` is passed on to :func:`normalize_security`, not just to the CLI: on an EVM
    chain two of GMGN's "authorities" are Solana fields pinned to ``false`` and must not be
    read as answers (:data:`_SOL_ONLY_BOOL_FIELDS`). An EVM token therefore comes back with
    ~1/3 critical coverage from this provider, which is what it has always actually had.
    """
    sec = token_security(address, chain, priority=priority, conn=conn, **kw)
    info = token_info(
        address,
        chain,
        priority=priority,
        conn=conn,
        wait_for_slot_s=wait_for_slot_s,
        **kw,
    )

    # ``sec`` is applied last so it wins any collision. On the fields both endpoints carry
    # -- ``top_10_holder_rate`` is the only one observed -- they agree, but where they
    # might not, the dedicated security endpoint is the one to believe.
    body: dict[str, Any] = {}
    for result in (info, sec):
        if result.ok:
            body.update(flatten_payload(result.data))

    parts = [r.receipt for r in (sec, info) if r.ok]
    if not parts:
        return _unavailable(
            "token.security+token.info",
            Failure.CLI_ERROR,
            f"neither read answered: security={sec.receipt.note or 'n/a'}; "
            f"info={info.receipt.note or 'n/a'}",
            conn=conn,
        )

    dropped: list[str] = []
    props = normalize_security(body, chain=chain, dropped=dropped)
    if not props:
        return _unavailable(
            "token.security+token.info",
            Failure.BAD_SHAPE,
            "gmgn answered but carried no field we recognise"
            + (f"; dropped {'; '.join(dropped[:3])}" if dropped else ""),
            conn=conn,
        )

    note = f"{len(props)} field(s) from {', '.join(r.endpoint for r in parts)}"
    if SCALE_UNVERIFIED:
        note += f"; scale assumed for {','.join(sorted(SCALE_UNVERIFIED))}"
    if dropped:
        note += f"; dropped {'; '.join(dropped[:2])}"
    return GmgnResult(props, _merged_receipt(parts, note))


# ----------------------------------------------------------------------------- market


def market_trending(
    chain: Chain | str = Chain.SOL,
    *,
    interval: str = "1h",
    limit: int = 100,
    order_by: str | None = None,
    direction: str | None = None,
    filters: Sequence[str] | None = None,
    platforms: Sequence[str] | None = None,
    min_liquidity: float | None = None,
    min_marketcap: float | None = None,
    max_marketcap: float | None = None,
    min_holder_count: int | None = None,
    min_smart_degen_count: int | None = None,
    max_top10_holder_rate: float | None = None,
    min_created: str | None = None,
    max_created: str | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Trending tokens. The CLI exposes ~30 more numeric filters; use :func:`run_read` for those.

    ``min_created``/``max_created`` need a unit suffix (``30m``, ``6h``, ``7d``); a bare
    number is rejected by the CLI.
    """
    args = [
        "market",
        "trending",
        "--interval",
        interval,
        "--limit",
        str(limit),
        *_flag("--order-by", order_by),
        *_flag("--direction", direction),
        *_repeated("--filter", filters),
        *_repeated("--platform", platforms),
        *_flag("--min-liquidity", min_liquidity),
        *_flag("--min-marketcap", min_marketcap),
        *_flag("--max-marketcap", max_marketcap),
        *_flag("--min-holder-count", min_holder_count),
        *_flag("--min-smart-degen-count", min_smart_degen_count),
        *_flag("--max-top10-holder-rate", max_top10_holder_rate),
        *_flag("--min-created", min_created),
        *_flag("--max-created", max_created),
    ]
    return _read("market.trending", args, chain, priority, kw)


def market_trenches(
    chain: Chain | str = Chain.SOL,
    *,
    types: Sequence[str] | None = None,
    limit: int = 80,
    filter_preset: str | None = None,
    sort_by: str | None = None,
    direction: str | None = None,
    max_rug_ratio: float | None = None,
    min_smart_degen_count: int | None = None,
    min_holder_count: int | None = None,
    min_progress: float | None = None,
    max_progress: float | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Launchpad trenches, keyed ``new_creation`` / ``near_completion`` / ``completed``.

    The returned dict always carries all three keys even when ``types`` narrows the query;
    the unasked-for categories come back empty.
    """
    args = [
        "market",
        "trenches",
        "--limit",
        str(limit),
        *_repeated("--type", types),
        *_flag("--filter-preset", filter_preset),
        *_flag("--sort-by", sort_by),
        *_flag("--direction", direction),
        *_flag("--max-rug-ratio", max_rug_ratio),
        *_flag("--min-smart-degen-count", min_smart_degen_count),
        *_flag("--min-holder-count", min_holder_count),
        *_flag("--min-progress", min_progress),
        *_flag("--max-progress", max_progress),
    ]
    return _read("market.trenches", args, chain, priority, kw)


# --------------------------------------------------------------------- rug ratio source
#
# ``rug_ratio`` is not on ``token security`` and not on ``token info``. MEASURED
# 2026-09-21 by reading both endpoints live for three pump.fun mints (9PSU..pump,
# 8ycE..pump, 54tf..pump) and one bsc token (0x6d0f..fd5c), and by re-reading the two
# recorded fixture pairs: 35 / 176 keys on Solana, 27 / 163 on bsc, and not one of them is
# a rug field -- the nearest names are ``dev_token_burn_ratio``, ``hide_risk`` and
# ``creator_created_count``. ``dyor.normalize_gmgn`` and :data:`_PLAIN_MAP` have been
# mapping a key that never arrives, which is why ``rug_ratio`` was UNAVAILABLE on 4,298 of
# 4,298 stored dossiers (live box, same day) and why ``lanes.sm_trenches`` -- which fails
# closed on an unknown rug ratio -- has never been able to fire.
#
# The field GMGN does send lives on the *feed* rows: ``market trenches`` (and also
# ``trending`` and ``signal``) carry ``rug_ratio`` per token. The CLI documents the
# matching filter as "rug pull risk score (0-1)" (``dist/commands/market.js``,
# ``TRENCHES_FILTER_FIELDS``) and its smart-money preset applies ``max_rug_ratio: 0.3`` --
# the same 0.3 the lane gates on. The lane was written against this number, so reading it
# from the feed is its source, not a substitute for it.
#
# What was MEASURED about it, so nobody mistakes it for more than it is:
#
# * **Unit**: a 0-1 fraction. Solana, 180 rows (60 per category at ``--limit 80``): min 0,
#   median 0.008, max 1.0, 90 exact zeros, 23 at or above 0.3, 57 distinct values.
# * **Coverage by chain**: present on 180/180 sol rows. On bsc 8/180 (all 0, 172 null);
#   robinhood 8/180 (all 0); base 54/180 (all 0). On the EVM chains GMGN has, in effect,
#   no answer. A null here stays UNAVAILABLE and is never read as 0 -- and a reported 0
#   on an EVM chain is read as "unscored", not as a measured 0 (:data:`EVM_ZERO_RUG_NOTE`):
#   two independent re-reads on the live box (2026-09-22) found every EVM value ever
#   observed to be exactly 0 and category-linked (bsc 12/180 present, all 0; robinhood
#   36/180, all 0; base 0/180; trending 50/50 int 0; signal 50/50 int 0).
# * **Not a creator-history ratio we can reproduce**: on the same rows it equals
#   ``1 - creator_created_open_ratio`` on only 30/180 and ``(created - open) / created`` on
#   the same 30, and tokens whose creator has exactly one (open) coin carry 0.29, 0.20 and
#   0.07. GMGN does not publish the definition. It is an opaque provider score, labelled
#   PROVIDER_REPORTED, never DERIVED and never a claim about the developer.
# * **It moves**: two sol reads a few minutes apart shared 103 tokens and 8 of them changed,
#   five from 0 to non-zero (one 0 -> 0.682); in one 90 s re-read (2026-09-22) 8 of 9
#   changes were 0 -> non-zero (0.041..0.270), and the rug==0 group was the young one
#   (median age 211 s, 54/95 first-time creators, against 771 s for rug>0). A zero on a
#   young sol token is therefore weak evidence -- it may mean "not scored yet"
#   (:data:`SOL_ZERO_RUG_NOTE` says so on every such receipt) -- so the value is only ever
#   served inside the dossier's own freshness budget, never from an old row.
#
# The read is one weight-1 call per chain, cached for the ``market.trenches`` TTL (15 s,
# 60 s grace), so a burst of scans shares it. ``dyor.collect_gmgn_feed`` runs it *after*
# ``security_properties`` and with ``wait_for_slot_s``: issued first it would spend the
# 1.2 s pacing slot and the unpaced ``token security`` read behind it would be refused by
# our own limiter -- the "quietly loses half its answer" failure ``run_read`` documents.

#: ``--limit`` per category. The CLI's documented maximum; the service returned 60 per
#: category at both 80 and 100 (MEASURED 2026-09-21), so asking for more changes nothing.
TRENCHES_FEED_LIMIT = 80

#: Cache window for the per-token feed read, overriding the ``market.trenches`` default
#: of 15 s TTL / 60 s grace that the ingest poller uses. The grace is the part that
#: matters: ``run_read`` serves a grace-window entry *without* trying the network, as
#: STALE, and a STALE receipt raises the dossier's ``stale_evidence`` warning (-15 on
#: UNKNOWN_SAFETY). MEASURED on the first 20-token re-scan: 4 of the 6 feed hits came back
#: STALE for being 15-60 s old, on a number a fresh read of which costs 0.95 s. So no
#: grace at all, and a TTL of 30 s. INVENTED as a number: the score was seen to move
#: within a few minutes and, in one 90 s re-read, within 90 s; whether it can move inside
#: 30 s was never measured (nothing read the same token twice that close together), so
#: 30 s is a guess at a window the score does not cross, not a bound. It would be settled
#: by re-reading the feed every 10 s for an hour and timing the first change per token.
TRENCHES_FEED_TTL_S = 30.0
TRENCHES_FEED_GRACE_S = 0.0

#: Receipt endpoint for "the feed answered and this is what it held for the token", as
#: opposed to the bare ``market.trenches`` a failed *read* carries. A dossier receipt
#: reading ``market.trenches.row unavailable`` therefore means GMGN answered and had no
#: number for this token, which is not an outage and must not be counted as one.
TRENCHES_ROW_ENDPOINT = "market.trenches.row"

#: What a reported ``0`` means, in the words every rug-ratio receipt must carry. The
#: stored-row reader ``dyor._stored_feed_rug_ratio`` keeps a copy of both strings (it must
#: work with this module absent) and a test holds the copies equal, so a reader of any
#: receipt meets one vocabulary.
#:
#: On every EVM chain, every ``rug_ratio`` value ever observed is exactly 0 and
#: category-linked (MEASURED 2026-09-22, two independent live-box re-reads: bsc 12/180
#: present, all 0; robinhood 36/180, all 0; base 0/180; trending 50/50 int 0; signal
#: 50/50 int 0). A score that is 0 on 100% of the rows that carry it says nothing about
#: any one token, so a 0 there is served as UNAVAILABLE, never as a measured 0. That is
#: an inference, labelled INVENTED because GMGN publishes no definition; one EVM row
#: carrying a non-zero value would settle it the other way.
EVM_ZERO_RUG_NOTE = (
    "reported 0 = unscored on this chain (INVENTED inference: 100% of EVM values observed are 0)"
)

#: On Solana a 0 is served, but it marks the young token more often than the safe one
#: (MEASURED 2026-09-22: the rug==0 group had median age 211 s and 54/95 first-time
#: creators against 771 s for rug>0, and in one 90 s re-read 8 of 9 changes were
#: 0 -> non-zero, 0.041..0.270). The receipt says so; "opaque score" alone would not.
SOL_ZERO_RUG_NOTE = "zero may mean unscored (young tokens flip 0 -> non-zero within minutes)"


def _zero_means_unscored(chain: Chain | str | None, value: Decimal) -> bool:
    """True when a reported 0 on this chain is read as "no score" rather than "safe".

    The chain test is :func:`_is_evm` -- the :class:`Chain` enum against
    :data:`EVM_CHAINS`, never a string list -- so a chain added to the enum is covered
    the day it is added and an unrecognised chain reads as non-EVM (the direction that
    serves the number with the Solana caveat rather than hiding it).
    """
    return value == 0 and _is_evm(chain)


def _feed_key(address: str, chain: Chain | str | None) -> str:
    """The address as it should be compared: case-folded on EVM, verbatim on Solana."""
    a = (address or "").strip()
    return a.lower() if _is_evm(chain) else a


def trenches_row(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    conn: Any = None,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = 5.0,
    **kw: Any,
) -> GmgnResult:
    """This token's own row in the **unfiltered** ``market trenches`` feed, or nothing.

    Unfiltered on purpose. The smart-money preset drops rows over ``max_rug_ratio`` on
    the server, so a token missing from *that* list is ambiguous -- rugged out or merely
    unloved. Here absence means one thing: not among the 60 newest, 60 nearest completion
    or 60 latest completed on the chain, which is the honest answer and is reported as
    UNAVAILABLE under :data:`TRENCHES_ROW_ENDPOINT` with no ``PROVIDER_ERROR`` event.

    A failed read (limiter, 429, CLI absent) returns the read's own receipt untouched, so
    the two cases stay distinguishable in the dossier.

    Served fresh or from a cache entry at most :data:`TRENCHES_FEED_TTL_S` old, never from
    the grace window: a row can be PROVIDER_REPORTED or CACHED here, not STALE.
    """
    feed = market_trenches(
        chain,
        limit=TRENCHES_FEED_LIMIT,
        priority=priority,
        conn=conn,
        wait_for_slot_s=wait_for_slot_s,
        ttl_s=TRENCHES_FEED_TTL_S,
        stale_grace_s=TRENCHES_FEED_GRACE_S,
        **kw,
    )
    if not feed.ok:
        return GmgnResult(None, feed.receipt)

    wanted = _feed_key(address, chain)
    seen = 0
    body = feed.data if isinstance(feed.data, Mapping) else {"rows": feed.data}
    for category, rows in body.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            seen += 1
            if wanted and _feed_key(str(row.get("address") or ""), chain) == wanted:
                return GmgnResult(
                    dict(row),
                    Receipt(
                        provider=PROVIDER,
                        endpoint=TRENCHES_ROW_ENDPOINT,
                        observed_at_ms=feed.receipt.observed_at_ms,
                        basis=feed.receipt.basis,
                        request_digest=feed.receipt.request_digest,
                        note=f"row found in {category} ({seen} rows scanned)",
                    ),
                )
    return GmgnResult(
        None,
        Receipt(
            provider=PROVIDER,
            endpoint=TRENCHES_ROW_ENDPOINT,
            observed_at_ms=feed.receipt.observed_at_ms,
            basis=EvidenceBasis.UNAVAILABLE,
            request_digest=feed.receipt.request_digest,
            note=f"not among the {seen} rows of the {_chain_value(chain) or chain} trenches feed",
        ),
    )


def feed_rug_ratio(
    address: str,
    chain: Chain | str = Chain.SOL,
    *,
    conn: Any = None,
    **kw: Any,
) -> GmgnResult:
    """``{"rug_ratio": Decimal}`` for one token, from its trenches row, in our vocabulary.

    Only that one field is lifted from the row. The row also carries ``top_10_holder_rate``
    and friends, but those already arrive through :func:`security_properties` with their
    own receipts; taking them again from a feed that may be 15 s older would only
    manufacture PROVIDER_CONFLICTs against ourselves.

    Four ways to come back empty, each with its reason in the receipt note and never a
    zero: the read failed (the read's receipt), the token is not on the feed, the row is
    there and ``rug_ratio`` is ``null`` -- the normal case on every EVM chain measured
    (bsc 172/180, robinhood 172/180, base 126/180 null on 2026-09-21) -- or the row is
    there and carries a ``0`` on an EVM chain, which is read as "unscored"
    (:data:`EVM_ZERO_RUG_NOTE`: 100% of EVM values ever observed are 0). On Solana a 0
    is served, PROVIDER_REPORTED, with :data:`SOL_ZERO_RUG_NOTE` on the receipt.
    """
    got = trenches_row(address, chain, conn=conn, **kw)
    if not got.ok or not isinstance(got.data, Mapping):
        return GmgnResult(None, got.receipt)

    raw = got.data.get("rug_ratio")
    value = _num(raw)
    if value is None:
        return GmgnResult(
            None,
            Receipt(
                provider=PROVIDER,
                endpoint=TRENCHES_ROW_ENDPOINT,
                observed_at_ms=got.receipt.observed_at_ms,
                basis=EvidenceBasis.UNAVAILABLE,
                request_digest=got.receipt.request_digest,
                note=(
                    f"row present but rug_ratio is {raw!r}; GMGN omits it on most EVM rows "
                    "(bsc 172/180, robinhood 172/180, base 126/180 null, 2026-09-21)"
                ),
            ),
        )
    if not (0 <= value <= 1):
        return GmgnResult(
            None,
            Receipt(
                provider=PROVIDER,
                endpoint=TRENCHES_ROW_ENDPOINT,
                observed_at_ms=got.receipt.observed_at_ms,
                basis=EvidenceBasis.UNAVAILABLE,
                request_digest=got.receipt.request_digest,
                note=f"rug_ratio={value} is not a 0-1 fraction; dropped rather than guessed at",
            ),
        )
    if _zero_means_unscored(chain, value):
        return GmgnResult(
            None,
            Receipt(
                provider=PROVIDER,
                endpoint=TRENCHES_ROW_ENDPOINT,
                observed_at_ms=got.receipt.observed_at_ms,
                basis=EvidenceBasis.UNAVAILABLE,
                request_digest=got.receipt.request_digest,
                # The verdict first: the row note behind it is context and may be cut.
                note=f"{EVM_ZERO_RUG_NOTE}; {got.receipt.note or ''}"[:300],
            ),
        )
    unscored = f"; {SOL_ZERO_RUG_NOTE}" if value == 0 else ""
    return GmgnResult(
        {"rug_ratio": value},
        Receipt(
            provider=PROVIDER,
            endpoint=TRENCHES_ROW_ENDPOINT,
            observed_at_ms=got.receipt.observed_at_ms,
            basis=got.receipt.basis,
            request_digest=got.receipt.request_digest,
            note=(
                f"rug_ratio={value} from the trenches feed{unscored}; opaque GMGN 0-1 score, "
                f"definition unpublished; {got.receipt.note or ''}"
            )[:300],
        ),
    )


def market_signal(
    chain: Chain | str = Chain.SOL,
    *,
    signal_types: Sequence[int] | None = None,
    mc_min: float | None = None,
    mc_max: float | None = None,
    trigger_mc_min: float | None = None,
    trigger_mc_max: float | None = None,
    priority: Priority = Priority.DISCOVERY,
    **kw: Any,
) -> GmgnResult:
    """Token signals (types 1–21). Returns a JSON *list*, one group per signal type.

    Only sol/bsc/robinhood/arc/stable are accepted by the CLI here; eth and base are not.
    """
    args = [
        "market",
        "signal",
        *_repeated("--signal-type", signal_types),
        *_flag("--mc-min", mc_min),
        *_flag("--mc-max", mc_max),
        *_flag("--trigger-mc-min", trigger_mc_min),
        *_flag("--trigger-mc-max", trigger_mc_max),
    ]
    return _read("market.signal", args, chain, priority, kw)


def market_search(
    query: str,
    chain: Chain | str = Chain.SOL,
    *,
    order_by: str | None = None,
    is_launched: bool | None = None,
    priority: Priority = Priority.RESEARCH,
    **kw: Any,
) -> GmgnResult:
    """Search tokens and wallets. Returns ``{"coins": [...], "wallets": [...]}``.

    The CLI's flag is ``-q/--query``, not ``--address``.
    """
    args = [
        "market",
        "search",
        "--query",
        query,
        *_flag("--order-by", order_by),
        *_flag("--is-launched", is_launched),
    ]
    return _read("market.search", args, chain, priority, kw)


# ------------------------------------------------------------------------------ quote


def order_quote(
    *,
    input_token: str,
    output_token: str,
    amount: int,
    from_address: str,
    chain: Chain | str = Chain.SOL,
    slippage_bps: int = 100,
    priority: Priority = Priority.ENTRY,
    **kw: Any,
) -> GmgnResult:
    """Price a swap without submitting one. **This module never submits.**

    ``amount`` is in the input token's base units, per the money rules in the contract.
    Slippage is sent as a decimal-percent string (``"1.00"``), matching
    :func:`kaiba.execution.executor.gmgn_swap_body`; sending bps overpays by 100x.

    On the Free plan this call is refused: it costs weight 10 against an allowance of 5. The
    refusal is reported as ``plan_gated`` so the dashboard shows a purchasable problem rather
    than a mystery outage. Never cached — a quote is a price for money.
    """
    args = [
        "order",
        "quote",
        "--from",
        from_address,
        "--input-token",
        input_token,
        "--output-token",
        output_token,
        "--amount",
        str(int(amount)),
        "--slippage",
        f"{slippage_bps / 100:.2f}",
    ]
    return _read("quote", args, chain, priority, kw)


__all__ = [
    "CLI_GLOBAL_ENV",
    "DEFAULT_TIMEOUT_S",
    "FREE_PLAN_WEIGHT",
    "GMGN_WEIGHTS",
    "PLAN_NOTE",
    "PROVIDER",
    "SCALE_UNVERIFIED",
    "TRENCHES_FEED_GRACE_S",
    "TRENCHES_FEED_LIMIT",
    "TRENCHES_FEED_TTL_S",
    "TRENCHES_ROW_ENDPOINT",
    "Failure",
    "GmgnResult",
    "account_info",
    "cli_argv",
    "credential_source",
    "feed_rug_ratio",
    "flatten_payload",
    "market_search",
    "market_signal",
    "market_trending",
    "market_trenches",
    "normalize_security",
    "order_quote",
    "portfolio_activity",
    "portfolio_holdings",
    "portfolio_profits",
    "portfolio_stats",
    "run_read",
    "scrub",
    "security_properties",
    "token_holders",
    "token_info",
    "token_security",
    "token_traders",
    "track_follow_wallet",
    "track_kol",
    "track_smartmoney",
    "trenches_row",
]
