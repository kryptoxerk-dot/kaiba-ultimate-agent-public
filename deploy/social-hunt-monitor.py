"""Hermes cron monitor: stable pending IDs and health transitions, read-only.

Codex HUNT-DELIVERY-20261029: an empty dead feed must not look like healthy quiet.
The existing cron owns notification delivery; this script only returns a snapshot.
"""

import json
import sys
from pathlib import Path

root = Path("/home/ubuntu/kaiba")
sys.path.insert(0, str(root))
from kaiba.hunters.social_health import monitor_payload, monitor_snapshot  # noqa: E402
from kaiba.hunters.social_hunt import load_config  # noqa: E402

try:
    config = load_config(root / "config/social_hunt.yaml")
    database = Path(config["database"])
    if not database.is_absolute():
        database = root / database
    snapshot = monitor_snapshot(database, enabled=config["enabled"],
        max_age_s=int(config.get("max_post_age_s", 86400)),
        budget=Path.home() / ".config/kaiba/social-research-budget.json")
except (OSError, ValueError, KeyError, TypeError):
    snapshot = {"pending": [], "delivery": {"status": "configuration_unavailable"}, "financial_execution": False}
print(json.dumps(monitor_payload(snapshot), sort_keys=True, separators=(",", ":")))
