#!/usr/bin/env bash
# Install the three Kaiba Hermes profiles, the MCP server wiring and the cron fleet.
#
# Codex coordination edit (P7-1n, 2026-09-21): the fallback-readiness slice keeps
# OAuth instructions explicit and separate from the Claude-owned profile design. Run
# this script as the same service user/HERMES_HOME that will run the gateway.
#
# Idempotent: safe to re-run after a config change. It never writes a credential — model
# auth is done interactively with `hermes auth`, and provider keys live in the Kaiba
# .env that only the kaiba service user can read.
#
# Usage:  deploy/install-hermes.sh [--root /opt/kaiba/current] [--python /opt/kaiba/venv/bin/python]
#                                  [--hermes /opt/kaiba/hermes-venv/bin/hermes]

set -euo pipefail

KAIBA_ROOT="${KAIBA_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-}"
HERMES_BIN="${HERMES_BIN:-hermes}"
PROFILES=(kaiba-operator kaiba-research kaiba-reflect)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) KAIBA_ROOT="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --hermes) HERMES_BIN="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# `deploy/install.sh` keeps the production venv beside the `current` release symlink,
# while a source checkout conventionally keeps `.venv` inside the checkout. Prefer the
# installed layout when it exists, then retain the checkout default for local installs.
if [[ -z "$PYTHON_BIN" ]]; then
  KAIBA_PARENT="$(cd "$KAIBA_ROOT/.." && pwd)"
  PYTHON_BIN="${KAIBA_ROOT}/.venv/bin/python"
  if [[ ! -x "$PYTHON_BIN" && -x "$KAIBA_PARENT/venv/bin/python" ]]; then
    PYTHON_BIN="$KAIBA_PARENT/venv/bin/python"
  fi
fi

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Profile paths are resolved from HOME/HERMES_HOME.  A root invocation would silently
# create /root/.hermes, while kaiba-hermes.service reads /var/lib/kaiba/hermes/.hermes.
# Refuse that class of deployment error instead of relying on a prose warning.
if [[ "$(id -u)" -eq 0 ]]; then
  die "run install-hermes.sh as the Hermes service user (for example: sudo -u kaiba-agent env HOME=/var/lib/kaiba/hermes HERMES_HOME=/var/lib/kaiba/hermes/.hermes ... )"
fi
if [[ "$(id -un)" == "kaiba-agent" && -z "${HERMES_HOME:-}" ]]; then
  die "HERMES_HOME must be explicit for kaiba-agent; use /var/lib/kaiba/hermes/.hermes"
fi

command -v "$HERMES_BIN" >/dev/null 2>&1 || die "hermes not on PATH. Install it first: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
[[ -x "$PYTHON_BIN" ]] || die "python not found at $PYTHON_BIN (create the venv first)"
[[ -f "$KAIBA_ROOT/kaiba/mcp/server.py" ]] || die "KAIBA_ROOT looks wrong: $KAIBA_ROOT"
[[ -f "$KAIBA_ROOT/kaiba/mcp/socket_bridge.py" ]] || die "MCP socket bridge is missing from $KAIBA_ROOT"

say "kaiba root:  $KAIBA_ROOT"
say "python:      $PYTHON_BIN"

# The MCP server must actually start before we point three profiles at it.
say "checking the MCP server imports"
"$PYTHON_BIN" -c "from kaiba.mcp.server import TOOLS; from kaiba.mcp import socket_bridge; print(f'  {len(TOOLS)} tools + socket bridge')" \
  || die "kaiba.mcp.server failed to import"

for profile in "${PROFILES[@]}"; do
  src="$KAIBA_ROOT/hermes/profiles/$profile"
  [[ -d "$src" ]] || die "missing profile source: $src"

  if "$HERMES_BIN" profile list 2>/dev/null | grep -qx "$profile"; then
    say "profile $profile already exists"
  else
    say "creating profile $profile"
    "$HERMES_BIN" profile create "$profile" \
      --description "Kaiba ${profile#kaiba-} agent" >/dev/null
  fi

  home="$($HERMES_BIN profile path "$profile" 2>/dev/null || echo "$HOME/.hermes/profiles/$profile")"
  mkdir -p "$home"

  # Substitute the two placeholders and install.
  sed -e "s|{{KAIBA_ROOT}}|$KAIBA_ROOT|g" -e "s|{{PYTHON}}|$PYTHON_BIN|g" \
    "$src/config.yaml" > "$home/config.yaml"
  cp "$src/SOUL.md" "$home/SOUL.md"
  say "installed config + SOUL into $home"
done

# Cron fleet. Jobs pin their own model, so a later change to a profile default cannot
# silently move a recurring job onto an expensive one.
if [[ -f "$KAIBA_ROOT/hermes/cron/jobs.yaml" ]]; then
  say "applying cron fleet"
  "$PYTHON_BIN" - "$KAIBA_ROOT" "$HERMES_BIN" <<'PY'
import subprocess, sys, yaml, pathlib
root, hermes = sys.argv[1], sys.argv[2]
spec = yaml.safe_load(pathlib.Path(root, "hermes/cron/jobs.yaml").read_text())
for job in spec.get("jobs", []):
    existing = subprocess.run(
        [hermes, "--profile", job["profile"], "cron", "list"],
        capture_output=True, text=True,
    ).stdout
    verb = "edit" if job["name"] in existing else "create"
    cmd = [hermes, "--profile", job["profile"], "cron", verb, job["schedule"], job["prompt"],
           "--name", job["name"], "--model", job["model"], "--provider", job["provider"],
           "--reasoning-effort", job.get("reasoning_effort", "medium"),
           "--deliver", job.get("deliver", "local")]
    for skill in job.get("skills", []):
        cmd += ["--skill", skill]
    result = subprocess.run(cmd, capture_output=True, text=True)
    status = "ok" if result.returncode == 0 else f"FAILED: {result.stderr.strip()[:160]}"
    print(f"  {verb:6} {job['name']:32} {status}")
PY
fi

cat <<EOF

$(say "done")

Remaining steps, which need you rather than a script:

  1. Authenticate the model providers once per profile, as the same service user and
     HERMES_HOME that will run the gateway:
       hermes --profile kaiba-operator auth add anthropic     # Claude Max + usage credits
       hermes --profile kaiba-operator auth add openai-codex  # Codex OAuth fallback
     Repeat for kaiba-research and kaiba-reflect. Do not copy or share ~/.codex/auth.json:
     Hermes keeps its own OAuth store under HERMES_HOME and refresh tokens are single-use.
     The Codex login uses the device-code flow on the pinned VPS runtime; do not add a
     browser flag unless the installed Hermes version advertises that flag.

     Check the credential without printing it:
       hermes --profile kaiba-operator auth status openai-codex

  2. Point the operator profile at Telegram:
       hermes --profile kaiba-operator gateway setup
     Use the @your_kaiba_bot token from the Kaiba .env, and set
     TELEGRAM_ALLOWED_USERS to your own numeric id.

  3. Start the gateway:
       hermes --profile kaiba-operator gateway start
     or install the systemd unit: deploy/systemd/kaiba-hermes.service

  4. Verify end to end (after both OAuth logins):
       hermes --profile kaiba-operator chat --once "kaiba_status"
     You should get lane modes and the line about withdrawals being unavailable.
EOF
