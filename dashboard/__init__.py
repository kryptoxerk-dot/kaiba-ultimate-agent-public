"""Operator dashboard for the Kaiba agent (PLAN §9, task P1-9).

One FastAPI process, server-rendered Jinja2, HTMX for partial refresh and an SSE feed off
``kaiba.core.events``. There is no build step and no npm: the only third-party browser code
is HTMX and Cytoscape from a CDN, and the page renders and stays honest without either.

Everything this package does against the core is **read-only** except the four operator
controls and the risk dial, which write ``config/risk.yaml`` through
``kaiba.core.config.save_risk`` (the bounds block is re-read from disk there, so the
dashboard cannot widen the operator's envelope either).

There is deliberately no withdraw control anywhere in this package.
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):  # noqa: ANN002, ANN003 - thin re-export, keeps imports lazy
    from dashboard.app import create_app as _create_app

    return _create_app(*args, **kwargs)
