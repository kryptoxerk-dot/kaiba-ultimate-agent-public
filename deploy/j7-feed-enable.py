"""Enable J7 social source only after a real external session passes the accounts API.

No password capture, browser-storage access, wallet import or authentication bypass.
Run from ~/kaiba on the VPS. Missing/expired session means no config/service change.
"""

import hashlib
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import yaml

root = Path("/home/ubuntu/kaiba")
session = Path.home() / ".config/kaiba/j7.session"
if not session.exists():
    raise SystemExit("J7_SESSION_REQUIRED_NO_CHANGE")
assert session.stat().st_mode & 0o077 == 0, "session_permissions_must_be_600"
token = session.read_text().strip()
assert token and not any(c.isspace() for c in token), "invalid_session_file"
with httpx.Client(timeout=12, follow_redirects=False) as client:
    response = client.get(
        "https://core.j7tracker.io/api/watched-accounts", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200, "j7_session_rejected_no_change"
    data = response.json()
    assert data.get("success") is True, "j7_accounts_unavailable_no_change"
    accounts = (data.get("x") or {}).get("accounts") or []
    assert isinstance(accounts, list) and accounts, "j7_roster_unavailable_no_change"
cfg = root / "config/tweet_launch.yaml"
before = cfg.read_bytes()
parsed = yaml.safe_load(before)
assert parsed["feed"]["backend"] in {"twitterapi_rule", "j7"}, "unexpected_feed_no_change"
if parsed["feed"]["backend"] == "j7":
    print(json.dumps({"status": "j7_source_already_selected", "x_main_accounts": len(accounts)}))
    raise SystemExit(0)
db = root / "data/kaiba.db"
wal = Path(str(db) + "-wal")
assert not wal.exists() or wal.stat().st_size <= 1_500_000_000, "wal_over_limit_no_read_or_change"
start = time.monotonic()
c = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True, timeout=1)
c.set_progress_handler(lambda: int(time.monotonic() - start > 3), 1000)
try:
    inflight = c.execute(
        "SELECT launch_id FROM tweet_launches WHERE state IN ('submitting','submitted') LIMIT 1"
    ).fetchall()
finally:
    c.close()
assert not inflight, "launch_submission_in_flight_no_change"
changed, n = re.subn(rb"(\nfeed:\s*\n\s+backend:\s*)twitterapi_rule", rb"\1j7", before, count=1)
assert n == 1, "feed_edit_not_unique_no_change"
after = yaml.safe_load(changed)
after["feed"]["backend"] = parsed["feed"]["backend"]
assert after == parsed, "configuration_scope_changed_no_install"
assert cfg.read_bytes() == before, "concurrent_configuration_change_no_install"
def pid(unit):
    return subprocess.check_output(
        ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], text=True
    ).strip()
protection = pid("kaiba-protection.service")
backup = root / "data/codex-social-hunts-20261007/j7-feed-config-before.yaml"
backup.write_bytes(before)
temp = cfg.with_name("tweet_launch.yaml.j7-stage")
temp.write_bytes(changed)
temp.replace(cfg)
subprocess.run(["systemctl", "--user", "restart", "kaiba-tweet-launch.service"], check=True, timeout=15)
receipt = {
    "status": "j7_social_source_selected",
    "x_main_accounts": len(accounts),
    "session_stored_outside_repo": True,
    "financial_config_equal_except_feed": True,
    "protection_pid_unchanged": protection == pid("kaiba-protection.service"),
    "config_sha256": hashlib.sha256(changed).hexdigest(),
    "live_receipt_verification_required": True,
}
(root / "data/codex-social-hunts-20261007/j7-enable.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps(receipt))
