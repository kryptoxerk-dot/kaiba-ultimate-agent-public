"""Selectable tweet source for the lead's existing launch pipeline.

No provider-rule activation, subscription purchase, database or execution access.
The lead integrates this iterator; configuring a feed does not arm trading.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

# Codex SOCIAL-HUNTS-FAST-20261007: fan out before slow launch creativity/execution.
# Codex SOCIAL-FOLLOW-SNIPE: prospective official-account posts share this socket.
from kaiba.ingest import follow_dispatch, j7_launch_bridge, social_dispatch, x_stream
from kaiba.ingest.x_stream import XPost


class FeedConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class FeedConfig:
    backend: str = "twitterapi_monitor"
    region: str = "dfw"

    @property
    def sync_monitored_accounts(self) -> bool:
        return self.backend == "twitterapi_monitor"


def parse_config(raw: Mapping) -> FeedConfig:
    """Explicit source selection; legacy configs retain their existing monitor source."""
    spec = raw.get("feed", {})
    if not isinstance(spec, Mapping):
        raise FeedConfigurationError("invalid_feed_configuration")
    backend = spec.get("backend", "twitterapi_monitor")
    region = spec.get("region", "dfw")
    if backend not in ("twitterapi_monitor", "twitterapi_rule", "j7"):
        raise FeedConfigurationError("unsupported_feed_backend")
    if region not in j7_launch_bridge.HOSTS:
        raise FeedConfigurationError("unsupported_j7_region")
    return FeedConfig(backend=backend, region=region)


def load_config(path: Path) -> FeedConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, Mapping):
        raise FeedConfigurationError("invalid_launch_configuration")
    return parse_config(raw)


async def stream(config: FeedConfig, launch_config, *, api_key: str = "") -> AsyncIterator[XPost]:
    """Feed the same XPost interface on all chains, filtering to configured authors.

    Rule mode shares the existing provider socket; it never calls monitor subscription
    endpoints or turns on rules. Do not start alongside another consumer of the same key.
    J7 supplies its own authenticated feed and never falls back to a paid source on failure.
    """
    if config.backend == "j7":
        token = j7_launch_bridge.session_token()
        social = social_dispatch.configuration()
        enabled = social.get("enabled", False)
        from kaiba.ingest.j7_social_events import SocialEvents

        research = SocialEvents() if enabled else None
        follow_enabled = follow_dispatch.configuration().get("enabled",False)
        project_events = SocialEvents(sink=follow_dispatch.offer_post) if follow_enabled else None
        def events(event,payload,*,now_ms):
            if research:
                research.ingest(event,payload,now_ms=now_ms)
            if project_events:
                project_events.ingest(event,payload,now_ms=now_ms)
            if follow_enabled:
                follow_dispatch.offer_event(event,payload,now_ms=now_ms)
        authors = (
            set(launch_config.accounts)
            | (set(launch_config.alpha.accounts) if getattr(launch_config, "alpha", None) else set())
            if enabled
            else launch_config.accounts
        )
        source = j7_launch_bridge.stream(
            token,
            authors,
            max_age_s=launch_config.max_tweet_age_s,
            require_image=launch_config.require_image and not launch_config.logo_generate,
            region=config.region,
            **({"on_event":events} if research or project_events else {}),
        )
    elif config.backend in ("twitterapi_monitor", "twitterapi_rule"):
        if not api_key:
            raise FeedConfigurationError("missing_twitterapi_key")
        source = x_stream.stream(api_key)
    else:
        raise FeedConfigurationError("unsupported_feed_backend")
    # Lead 2026-10-07: alpha accounts (tweet_refs) share this socket, so they pass too.
    allowed = set(launch_config.accounts) | (
        set(launch_config.alpha.accounts) if getattr(launch_config, "alpha", None) is not None else set()
    )
    allowed |= social_dispatch.watched_accounts(j7=config.backend == "j7")
    async for post in source:
        # Dynamic project roster affects only this independent source. No restart,
        # launch-author expansion or second provider socket is needed after verification.
        if config.backend != "j7" and post.author in follow_dispatch.projects():
            follow_dispatch.offer_post(post)
        if post.author in allowed:
            if config.backend != "j7":
                social_dispatch.offer(post)
            # Research-only authors never enter launch workers or financial alpha routes.
            if post.author in launch_config.accounts or (
                getattr(launch_config, "alpha", None) is not None
                and post.author in launch_config.alpha.accounts
            ):
                yield post
