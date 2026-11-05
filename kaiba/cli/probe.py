"""`kaiba probe` — which provider credentials exist, and which providers actually answer.

This is the first command you run on a new host and the one you run at 3am when a lane has
gone quiet, so it has three hard properties.

**It never prints a secret.** Not the value, not a masked prefix, not in a verbose mode,
not inside an error string. Only the credential's *name* and whether it is set. Provider
errors are the dangerous part: ``httpx`` puts the full request URL into its exception text,
and several providers (Helius, Etherscan, Alchemy, Telegram) carry the credential in that
URL, so every string this module emits goes through :func:`redact` first — exact credential
values from settings, then a sweep for credential *shapes*. That redactor started here and
now lives in :mod:`kaiba.core.redact`, because the same leak reaches the event bus and the
limiter's call log; this module imports it rather than keeping a second copy.
``tests/test_probe.py`` injects fake credentials and asserts none of them appear anywhere
in the output; that test is the feature.

**It is safe to run while trading.** Every call goes through the shared limiter at
:attr:`Priority.RESEARCH`, the lowest class, so a probe starves before an exit does. It
reads only the cheapest endpoint each provider offers, never quotes, never submits, and
writes no trading state.

**It degrades instead of crashing.** Adapters that other agents are still writing are
imported defensively and reported as ``not built``; a provider that is down is a row in the
table, not a traceback.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from kaiba.core.config import Settings, get_settings
from kaiba.core.limiter import Priority
from kaiba.core.redact import SECRET_NAMES, redact_text
from kaiba.core.redact import secret_values as _core_secret_values
from kaiba.providers._http import Fetched, get_json, post_json

log = logging.getLogger(__name__)

#: The string redactor, under the name this module has always exported and the name the
#: tests and the docstring above use. ``kaiba.core.redact.redact`` is the *dict* variant
#: (it blanks secret-named keys in a params/headers mapping); what a probe needs is the
#: text one, so this is an alias rather than a second implementation.
redact = redact_text

OK = "ok"
AUTH_ERROR = "auth error"
RATE_LIMITED = "rate limited"
MISSING_CREDENTIAL = "missing credential"
UNREACHABLE = "unreachable"
NOT_BUILT = "not built"
BUDGET_EXHAUSTED = "budget exhausted"
UNKNOWN = "unknown"

#: Probes are diagnostics. They must lose every race against real trading traffic.
PROBE_PRIORITY = Priority.RESEARCH

DEFAULT_TIMEOUT_S = 6.0

SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class Outcome:
    """What one provider answered. ``detail`` is redacted before it leaves :func:`run_probe`."""

    status: str
    detail: str = ""


def secret_values(settings: Settings | None = None) -> list[str]:
    """Every configured value we must never echo, longest first so prefixes cannot survive.

    Delegates to :func:`kaiba.core.redact.secret_values` for the normal case. The optional
    ``settings`` argument is the injection point the tests use to redact against a specific
    instance instead of whatever the process-wide cache happens to hold.
    """
    if settings is None:
        return _core_secret_values()
    out: list[str] = []
    for name, value in settings.model_dump().items():
        if not isinstance(value, str) or len(value) < 8:
            continue
        if SECRET_NAMES.search(name) or "url" in name:
            out.append(value)
    return sorted(set(out), key=len, reverse=True)


# ------------------------------------------------------------------------------ classify


_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden", "invalid api key", "invalid key",
               "api key", "authentication")
_RATE_HINTS = ("429", "rate limit", "rate_limit", "too many requests", "cooldown",
               "minimum interval", "bucket exhausted", "daily credit cap")


def classify(fetched: Fetched, *, ok_detail: str = "") -> Outcome:
    """Turn a :class:`Fetched` into a probe status. HTTP status hides in the note text."""
    if fetched.ok:
        return Outcome(OK, ok_detail)
    note = (fetched.receipt.note or "no detail").lower()
    if "budget exhausted" in note:
        return Outcome(BUDGET_EXHAUSTED, fetched.receipt.note or "")
    if any(h in note for h in _RATE_HINTS):
        return Outcome(RATE_LIMITED, fetched.receipt.note or "")
    if any(h in note for h in _AUTH_HINTS):
        return Outcome(AUTH_ERROR, fetched.receipt.note or "")
    return Outcome(UNREACHABLE, fetched.receipt.note or "")


def _zero_arg_entry(module: Any, names: Sequence[str]) -> tuple[str, Callable[[], Any]] | None:
    """First attribute in ``names`` that can be called with no arguments, and its name."""
    for name in names:
        fn = getattr(module, name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):  # builtins without signatures
            return name, fn
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        if not required:
            return name, fn
    return None


# -------------------------------------------------------------------------------- checks


def _check_gmgn(s: Settings, timeout_s: float) -> Outcome:
    """GMGN reads go through the CLI wrapper (task P0-2), which may not exist yet."""
    try:
        mod = importlib.import_module("kaiba.providers.gmgn_cli")
    except Exception as exc:  # noqa: BLE001 - a half-written sibling module must not crash us
        return Outcome(NOT_BUILT, f"kaiba.providers.gmgn_cli: {type(exc).__name__}: {exc}")
    if not s.gmgn_api_key:
        return Outcome(MISSING_CREDENTIAL, "wrapper present, GMGN_API_KEY unset")
    found = _zero_arg_entry(mod, ("probe", "health", "ping", "cli_argv", "cli_version", "version"))
    if found is None:
        return Outcome(UNKNOWN, "wrapper present, no zero-argument probe entry point")
    name, entry = found
    try:
        result = entry()
    except Exception as exc:  # noqa: BLE001 - the wrapper shells out; anything can come back
        return Outcome(UNREACHABLE, f"{type(exc).__name__}: {exc}")
    if not result:
        # `cli_argv()` answers None when gmgn-cli is not installed. An empty answer is a
        # finding, not a pass.
        return Outcome(UNREACHABLE, f"{name}() returned {result!r}")
    return Outcome(OK, f"{name}: {str(result)[:60]}")


def _check_helius(s: Settings, timeout_s: float) -> Outcome:
    if not s.helius_api_key:
        return Outcome(MISSING_CREDENTIAL, "HELIUS_API_KEY unset")
    try:
        from kaiba.providers import helius
    except Exception as exc:  # noqa: BLE001
        return Outcome(NOT_BUILT, f"kaiba.providers.helius: {type(exc).__name__}: {exc}")
    result, receipt = helius.ping()
    if result is not None:
        return Outcome(OK, f"getHealth={result}")
    return classify(Fetched(None, receipt))


def _check_birdeye(s: Settings, timeout_s: float) -> Outcome:
    if not s.birdeye_api_key:
        return Outcome(MISSING_CREDENTIAL, "BIRDEYE_API_KEY unset")
    fetched = get_json(
        "birdeye",
        "price.single",
        "https://public-api.birdeye.so/defi/price",
        params={"address": SOL_MINT},
        headers={"X-API-KEY": s.birdeye_api_key, "x-chain": "solana"},
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    if fetched.ok and isinstance(fetched.data, dict) and fetched.data.get("success") is False:
        return Outcome(AUTH_ERROR, str(fetched.data.get("message") or "success=false")[:120])
    return classify(fetched, ok_detail="SOL price")


def _check_dexscreener(s: Settings, timeout_s: float) -> Outcome:
    """Keyless. Prefer the adapter (task P1-3) but probe the endpoint directly without it."""
    note = ""
    try:
        mod = importlib.import_module("kaiba.providers.dexscreener")
        found = _zero_arg_entry(mod, ("probe", "health", "ping"))
        if found is not None:
            try:
                return Outcome(OK, str(found[1]())[:80])
            except Exception as exc:  # noqa: BLE001
                return Outcome(UNREACHABLE, f"{type(exc).__name__}: {exc}")
        note = "adapter present without a probe entry point; "
    except Exception as exc:  # noqa: BLE001
        note = f"adapter not built ({type(exc).__name__}); "
    fetched = get_json(
        "dexscreener",
        "profiles.latest",
        "https://api.dexscreener.com/token-profiles/latest/v1",
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    outcome = classify(fetched, ok_detail="token-profiles/latest")
    return Outcome(outcome.status, f"{note}{outcome.detail}")


def _check_coingecko(s: Settings, timeout_s: float) -> Outcome:
    """Keyless works; a demo key only raises the rate limit, so an absent key is not a failure."""
    headers = {"x-cg-demo-api-key": s.coingecko_api_key} if s.coingecko_api_key else None
    fetched = get_json(
        "coingecko",
        "ping.ping",
        "https://api.coingecko.com/api/v3/ping",
        headers=headers,
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    suffix = "" if s.coingecko_api_key else " (keyless)"
    return classify(fetched, ok_detail=f"ping{suffix}")


def _check_etherscan(s: Settings, timeout_s: float) -> Outcome:
    """Etherscan answers 200 with an error body for a bad key, so read the body."""
    if not s.etherscan_api_key:
        return Outcome(MISSING_CREDENTIAL, "ETHERSCAN_API_KEY unset")
    fetched = get_json(
        "etherscan",
        "proxy.blockNumber",
        "https://api.etherscan.io/v2/api",
        params={
            "chainid": 1,
            "module": "proxy",
            "action": "eth_blockNumber",
            "apikey": s.etherscan_api_key,
        },
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    if not fetched.ok:
        return classify(fetched)
    data = fetched.data if isinstance(fetched.data, dict) else {}
    result = data.get("result")
    if isinstance(result, str) and result.startswith("0x"):
        return Outcome(OK, f"block {int(result, 16)}")
    body = str(result or data.get("message") or data)[:120].lower()
    if "rate limit" in body:
        return Outcome(RATE_LIMITED, str(result)[:120])
    if "invalid api key" in body or "api key" in body:
        return Outcome(AUTH_ERROR, str(result)[:120])
    return Outcome(UNREACHABLE, str(result or data)[:120])


def _check_alchemy(s: Settings, timeout_s: float) -> Outcome:
    if not s.alchemy_api_key:
        return Outcome(MISSING_CREDENTIAL, "ALCHEMY_API_KEY unset")
    fetched = post_json(
        "alchemy",
        "rpc.blockNumber",
        f"https://eth-mainnet.g.alchemy.com/v2/{s.alchemy_api_key}",
        json_body={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    if fetched.ok and isinstance(fetched.data, dict):
        if fetched.data.get("error"):
            return Outcome(AUTH_ERROR, str(fetched.data["error"])[:120])
        block = fetched.data.get("result")
        if isinstance(block, str) and block.startswith("0x"):
            return Outcome(OK, f"eth block {int(block, 16)}")
    return classify(fetched)


def _check_solana_rpc(s: Settings, timeout_s: float) -> Outcome:
    """The configured Solana RPC, which may be the public endpoint or a keyed one."""
    if not s.solana_rpc_url:
        return Outcome(MISSING_CREDENTIAL, "SOLANA_RPC_URL unset")
    fetched = post_json(
        "rpc",
        "rpc.getHealth",
        s.solana_rpc_url,
        json_body={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    if fetched.ok and isinstance(fetched.data, dict):
        if fetched.data.get("result") == "ok":
            return Outcome(OK, "getHealth=ok")
        if fetched.data.get("error"):
            return Outcome(UNREACHABLE, str(fetched.data["error"])[:120])
    return classify(fetched)


def _check_telegram(s: Settings, timeout_s: float) -> Outcome:
    if not s.telegram_bot_token:
        return Outcome(MISSING_CREDENTIAL, "TELEGRAM_BOT_TOKEN unset")
    fetched = get_json(
        "telegram",
        "bot.getMe",
        f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe",
        priority=PROBE_PRIORITY,
        timeout_s=timeout_s,
    )
    if fetched.ok and isinstance(fetched.data, dict):
        if fetched.data.get("ok"):
            username = (fetched.data.get("result") or {}).get("username")
            return Outcome(OK, f"@{username}" if username else "getMe ok")
        return Outcome(AUTH_ERROR, str(fetched.data.get("description") or "")[:120])
    return classify(fetched)


@dataclass(frozen=True)
class Target:
    """One row of the probe table. ``credential`` is a *name*; values never appear here."""

    name: str
    credential: str
    endpoint: str
    check: Callable[[Settings, float], Outcome]
    optional: bool = False


TARGETS: tuple[Target, ...] = (
    Target("gmgn", "GMGN_API_KEY", "gmgn-cli wrapper", _check_gmgn),
    Target("helius", "HELIUS_API_KEY", "getHealth", _check_helius),
    Target("birdeye", "BIRDEYE_API_KEY", "/defi/price", _check_birdeye),
    Target("dexscreener", "", "token-profiles/latest/v1", _check_dexscreener, optional=True),
    Target("coingecko", "COINGECKO_API_KEY", "/api/v3/ping", _check_coingecko, optional=True),
    Target("etherscan", "ETHERSCAN_API_KEY", "v2 eth_blockNumber", _check_etherscan),
    Target("alchemy", "ALCHEMY_API_KEY", "eth_blockNumber", _check_alchemy),
    Target("solana-rpc", "SOLANA_RPC_URL", "getHealth", _check_solana_rpc, optional=True),
    Target("telegram", "TELEGRAM_BOT_TOKEN", "getMe", _check_telegram),
)


def credential_present(settings: Settings, name: str) -> bool:
    """Is this credential set? By name, from settings — the value is never returned."""
    if not name:
        return True
    return bool(getattr(settings, name.lower(), ""))


def run_probe(
    names: Sequence[str] | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> list[dict[str, Any]]:
    """Check every provider and return one JSON-safe row each.

    Called with no arguments by ``kaiba probe``; ``names`` narrows it for debugging. Keys
    ``provider`` / ``credential`` / ``reachable`` / ``detail`` are the CLI's contract, the
    rest are extra.
    """
    settings = get_settings()
    secrets = secret_values(settings)

    # The limiter is SQLite-backed, so the schema has to exist before any call is made.
    # Creating it is the only write this command performs beyond events and provider
    # bookkeeping.
    try:
        from kaiba.core.db import ensure_db

        ensure_db()
    except Exception as exc:  # noqa: BLE001 - probing a read-only checkout is still useful
        log.warning("probe could not prepare the database: %s: %s", type(exc).__name__, exc)

    wanted = {n.lower() for n in names} if names else None
    rows: list[dict[str, Any]] = []
    for target in TARGETS:
        if wanted is not None and target.name not in wanted:
            continue
        present = credential_present(settings, target.credential)
        started = time.perf_counter()
        try:
            outcome = target.check(settings, timeout_s)
        except Exception as exc:  # noqa: BLE001 - a probe that crashes is a probe that lies
            outcome = Outcome(UNREACHABLE, f"{type(exc).__name__}: {exc}")
        latency_ms = int((time.perf_counter() - started) * 1000)
        rows.append(
            {
                "provider": target.name,
                "credential": present,
                "credential_name": target.credential or "(keyless)",
                "credential_optional": target.optional,
                "endpoint": target.endpoint,
                "status": outcome.status,
                "reachable": outcome.status == OK,
                "latency_ms": latency_ms if outcome.status != MISSING_CREDENTIAL else None,
                "detail": redact(outcome.detail, secrets)[:200],
            }
        )
    return rows


_STATUS_STYLE = {
    OK: "green",
    AUTH_ERROR: "red",
    RATE_LIMITED: "yellow",
    MISSING_CREDENTIAL: "dim",
    UNREACHABLE: "red",
    NOT_BUILT: "dim",
    BUDGET_EXHAUSTED: "yellow",
    UNKNOWN: "yellow",
}


def render(results: Sequence[dict[str, Any]], console: Any = None) -> Any:
    """Print the status table. Returns the console so a caller can capture it."""
    from rich.console import Console
    from rich.table import Table

    out = console or Console()
    table = Table(
        "provider", "credential", "set", "status", "latency", "endpoint", "detail",
        title="Provider probe", title_justify="left",
    )
    for r in results:
        style = _STATUS_STYLE.get(r["status"], "white")
        latency = "-" if r["latency_ms"] is None else f"{r['latency_ms']} ms"
        is_set = "yes" if r["credential"] else ("n/a" if r["credential_optional"] else "no")
        table.add_row(
            r["provider"],
            r["credential_name"],
            is_set,
            f"[{style}]{r['status']}[/{style}]",
            latency,
            r["endpoint"],
            (r["detail"] or "")[:60],
        )
    out.print(table)
    out.print("[dim]credential names only; no value is ever printed[/dim]")
    return out


def main(json_out: bool = False) -> int:
    """Entry point for ``python -m kaiba.cli.probe``."""
    from rich.console import Console

    results = run_probe()
    console = Console()
    if json_out:
        console.print_json(json.dumps(results, default=str))
    else:
        render(results, console)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
