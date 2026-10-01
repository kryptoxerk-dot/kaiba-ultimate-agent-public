"""Strip credentials out of text before it is stored, logged, emitted or displayed.

This is a core primitive rather than a provider helper because the leak it prevents is not
confined to the provider layer. An httpx ``HTTPStatusError`` renders the **full request
URL** into its message, and several providers carry their credential in that URL:

* Helius ``?api-key=``
* Etherscan ``?apikey=``
* Alchemy ``/v2/<key>``
* Telegram ``/bot<id>:<token>/getMe``

That message then travels three separate routes into durable storage: the ``Receipt.note``
handed back to the caller, the ``events`` table via ``PROVIDER_ERROR`` (and from there to
the dashboard feed), and ``provider_calls.detail`` plus ``provider_family_bans.reason``
via the limiter. Reproduced on 2026-09-20: a 401 on a keyed URL wrote the literal key into
two tables.

It lives in ``kaiba.core`` and imports nothing from the rest of the package, so both
``kaiba.core.limiter`` and ``kaiba.providers._http`` can use it without a cycle.

Two passes, in this order:

1. **Exact configured values.** The only thing we can be certain about. Longest first, so
   a credential that contains another as a prefix cannot leave a fragment behind.
2. **Shapes.** A provider can always invent a parameter name we did not anticipate, and a
   token leaked once is leaked permanently.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "<redacted>"

#: Field, parameter and header names whose *values* must never be recorded.
SECRET_NAMES = re.compile(
    r"(key|token|secret|auth|password|jwt|signature|sign|apikey|api_key|access)", re.I
)

#: ``?api-key=...``, ``&token=...``, ``secret: ...`` and friends, anywhere in a string.
_ASSIGNED = re.compile(
    r"(?i)(\b(?:api[-_]?key|apikey|key|token|secret|auth|access[-_]?token|jwt|password"
    r"|passwd|sig|signature)\s*[=:]\s*)([^&\s'\"<>)]+)"
)
#: ``Authorization: Bearer <token>`` echoed in a header dump or an exception.
_BEARER = re.compile(r"(?i)\b((?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{8,})")
#: Telegram puts the whole bot token in the path.
_TG_PATH = re.compile(r"/bot[0-9]+:[A-Za-z0-9_\-]+")
#: Alchemy and friends put the key in the last path segment.
_KEY_PATH = re.compile(r"(?i)(/v[0-9]+/)[A-Za-z0-9_\-]{20,}")


def secret_values() -> list[str]:
    """Every configured credential, longest first. Empty if settings cannot be read."""
    try:
        from kaiba.core.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - redaction must work even when config is broken
        return []
    out: list[str] = []
    for name, value in settings.model_dump().items():
        if not isinstance(value, str) or len(value) < 8:
            continue
        if SECRET_NAMES.search(name) or "url" in name.lower():
            out.append(value)
    return sorted(set(out), key=len, reverse=True)


def redact_text(text: Any, secrets: Iterable[str] | None = None) -> str:
    """Return ``text`` with every credential value and credential-shaped fragment removed.

    Accepts any object so a caller can pass an exception straight in without a guard.
    Passing ``secrets`` explicitly skips reading settings, which matters in hot paths and
    in tests.
    """
    out = text if isinstance(text, str) else ("" if text is None else str(text))
    if not out:
        return out
    for value in secrets if secrets is not None else secret_values():
        if value and len(value) >= 8:
            out = out.replace(value, REDACTED)
    out = _ASSIGNED.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    out = _TG_PATH.sub(f"/bot{REDACTED}", out)
    out = _KEY_PATH.sub(lambda m: f"{m.group(1)}{REDACTED}", out)
    return out


def redact(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """Copy a param/header dict with every secret-*named* value replaced."""
    if not mapping:
        return {}
    return {k: (REDACTED if SECRET_NAMES.search(k) else v) for k, v in mapping.items()}


# --------------------------------------------------------------------------- logging

#: Loggers that print a full request URL at INFO. httpx does it for every request, and
#: our provider keys travel in the query string -- so for those providers the URL *is*
#: the credential. Muting them is the cheap half of the fix; :class:`_RedactingFilter`
#: is the half that survives someone adding a new client library.
_CHATTY_HTTP_LOGGERS = ("httpx", "httpcore", "hpack", "urllib3", "websockets.client")

_LOG_REDACTION_INSTALLED = False


class _RedactingFilter(logging.Filter):
    """Scrub credentials out of every record before a handler can write it.

    A filter on the root logger is the only place that catches *all* of it: our own
    log lines, a library's, and a traceback whose exception text carries a keyed URL.
    It runs on the emitting thread, so it must never raise -- a logging call that
    throws takes the caller down with it, and a credential leak is not worth a crash
    in the exit watchdog.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            secrets = secret_values()
            if not secrets:
                return True
            # Render once, here, rather than leaving args for the handler: the secret
            # may live in an arg, and by the time the handler formats it we are gone.
            message = record.getMessage()
            cleaned = redact_text(message, secrets)
            if cleaned != message:
                record.msg = cleaned
                record.args = ()
            if record.exc_text:
                record.exc_text = redact_text(record.exc_text, secrets)
        except Exception:  # noqa: BLE001 - logging must never be the thing that fails
            return True
        return True


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Scrub a record in place. Never raises: logging must not be what takes us down."""
    try:
        secrets = secret_values()
        if not secrets:
            return record
        message = record.getMessage()
        cleaned = redact_text(message, secrets)
        if cleaned != message:
            # Render once, here. The secret may live in an arg, and if we leave args for
            # the handler to interpolate it reappears after we have stopped looking.
            record.msg = cleaned
            record.args = ()
        if getattr(record, "exc_text", None):
            record.exc_text = redact_text(record.exc_text, secrets)
    except Exception:  # noqa: BLE001 - a logging call that throws takes its caller down
        pass
    return record


class _RedactingFilter(logging.Filter):
    """Handler-level scrub, for records that reach a handler without a new factory call.

    Belt to the record factory's braces: a handler filter sees every record the handler
    is about to write, including ones propagated from a child logger, which is exactly
    what a filter on the *root logger* does not see. Getting that wrong is how the first
    version of this fix passed review and leaked anyway.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


def _patch_exception_formatting() -> None:
    """Redact tracebacks, which no filter can reach.

    ``record.exc_text`` does not exist when a filter runs -- the formatter builds it
    afterwards, from ``exc_info``, and caches it on the record. So a keyed URL inside an
    exception message survives every other layer here and lands in the log. This is the
    exact shape of the leak that was already fixed once in the provider layer: httpx's
    ``HTTPStatusError`` carries the full request URL, and the URL is the credential.

    Patching ``logging.Formatter`` is a global change to a stdlib class, which is worth
    it for two methods whose entire output is text we are about to write to disk.
    """
    for name in ("formatException", "formatStack"):
        original = getattr(logging.Formatter, name, None)
        if original is None or getattr(original, "_kaiba_redacts", False):
            continue

        def wrapper(self: logging.Formatter, value: Any, _original: Any = original) -> str:
            try:
                return redact_text(_original(self, value), secret_values())
            except Exception:  # noqa: BLE001 - never break the log write
                return _original(self, value)

        wrapper._kaiba_redacts = True  # type: ignore[attr-defined]
        setattr(logging.Formatter, name, wrapper)


def install_log_redaction(*, mute_http_clients: bool = True) -> None:
    """Redact credentials from every log record. Idempotent; safe to call anywhere.

    Called on ``import kaiba.core`` so every entry point gets it -- the CLI, the ingest
    runner, the MCP server, the signer and the scheduler each call ``basicConfig``
    independently, and a fix living in one of them would protect only that process.

    Three layers, because no single one covers every topology:

    * **A log record factory.** Every record, from any logger, is built through it. This
      is the layer that actually works, and it keeps working when an entry point calls
      ``basicConfig(force=True)`` and throws our handlers away.
    * **A filter on the root handlers**, for anything constructed without the factory.
    * **Raising the level of the HTTP clients**, which log a full request URL at INFO.
      Our provider keys travel in the query string, so for those providers the URL *is*
      the credential.
    """
    global _LOG_REDACTION_INSTALLED
    if _LOG_REDACTION_INSTALLED:
        return

    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return _redact_record(previous(*args, **kwargs))

    logging.setLogRecordFactory(factory)

    root = logging.getLogger()
    if not any(isinstance(f, _RedactingFilter) for f in root.filters):
        root.addFilter(_RedactingFilter())
    for handler in list(root.handlers):
        if not any(isinstance(f, _RedactingFilter) for f in handler.filters):
            handler.addFilter(_RedactingFilter())

    _patch_exception_formatting()

    if mute_http_clients:
        for name in _CHATTY_HTTP_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
    _LOG_REDACTION_INSTALLED = True
