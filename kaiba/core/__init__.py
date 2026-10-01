"""Core primitives: schemas, config, database, journal, event bus, redaction.

Importing this package installs credential redaction on the root logger. That happens
here, at package import, because every entry point in this tree calls
``logging.basicConfig`` for itself -- the CLI, the ingest runner, the MCP server, the
signer, the scheduler -- and a fix placed in any one of them would protect only that
process. Every module in ``kaiba`` imports something from ``kaiba.core``, so this is the
one hook that covers all of them.

The leak it closes is real and was found in production logs on 2026-09-20: httpx logs
the full request URL at INFO, ``kaiba_log_level`` defaults to INFO, and our provider
keys travel in the query string -- so for those providers the URL *is* the credential.
"""

from __future__ import annotations

from kaiba.core.redact import install_log_redaction

install_log_redaction()

__all__ = ["install_log_redaction"]
