#!/usr/bin/env bash
# One-command installer for a Kaiba host.
#
#   bootstrap identities -> sync code as a new release -> build the venv ->
#   install the package -> migrate the database -> install the units -> tell the human
#   what is left.
#
# It installs and enables; it does NOT start the trading services and it does NOT arm
# anything. Nothing here can move money. The last thing it prints is the list of steps
# that need a person, and `deploy/preflight.py` is the gate before any of them.
#
# Safe to run twice. Every step is either idempotent or writes a new immutable release
# directory and flips a symlink.
#
# Usage:
#   sudo deploy/install.sh [--repo /path/to/checkout] [--prefix /opt/kaiba]
#                          [--python python3.12] [--with-caddy] [--skip-bootstrap]
#                          [--no-enable] [--dry-run]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${KAIBA_REPO:-$(cd "$HERE/.." && pwd)}"
KAIBA_PREFIX="${KAIBA_PREFIX:-/opt/kaiba}"
KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
PYTHON="${KAIBA_PYTHON:-python3.12}"
WITH_CADDY=0
SKIP_BOOTSTRAP=0
DO_ENABLE=1
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --prefix) KAIBA_PREFIX="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --with-caddy) WITH_CADDY=1; shift ;;
    --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
    --no-enable) DO_ENABLE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if (( DRY_RUN )); then printf '     would: %s\n' "$*"; else "$@"; fi; }

[[ "$(id -u)" -eq 0 ]] || die "run as root"
[[ -f "$REPO/pyproject.toml" ]] || die "$REPO does not look like the Kaiba checkout"
[[ -d "$REPO/deploy/systemd" ]] || die "$REPO/deploy/systemd missing"

command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found. On Ubuntu 24.04: apt install python3.12-venv"
PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[[ "$PYVER" == "3.12" ]] || warn "python is $PYVER; the project targets 3.12"

VENV="$KAIBA_PREFIX/venv"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE="$KAIBA_PREFIX/releases/$STAMP"
CURRENT="$KAIBA_PREFIX/current"

# ---------------------------------------------------------------------------- 1. identities

if (( SKIP_BOOTSTRAP )); then
  say "skipping bootstrap (--skip-bootstrap)"
else
  say "identities and directory layout"
  BOOTSTRAP_ARGS=(--prefix "$KAIBA_PREFIX" --state "$KAIBA_STATE" --conf "$KAIBA_CONF")
  if (( DRY_RUN )); then BOOTSTRAP_ARGS+=(--dry-run); fi
  "$HERE/bootstrap.sh" "${BOOTSTRAP_ARGS[@]}"
fi
getent passwd kaiba-core >/dev/null || die "kaiba-core does not exist; run deploy/bootstrap.sh"

# ---------------------------------------------------------------------------- 2. code

say "syncing code to $RELEASE"
run install -d -m 0755 -o root -g root "$RELEASE"
if command -v rsync >/dev/null 2>&1; then
  run rsync -a --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'data' --exclude '.env' --exclude '.pytest_cache' --exclude 'reference' \
    --exclude 'node_modules' \
    --exclude '*wallet*.txt' --exclude '*wallets*.txt' --exclude '*Wallet*.txt' \
    --exclude '*seed*.txt' --exclude '*mnemonic*' --exclude '*private*key*' \
    --exclude '*privkey*' --exclude 'secrets' \
    --exclude '*.key' --exclude '*_ed25519' --exclude '*_rsa' --exclude '*.ppk' \
    "$REPO/" "$RELEASE/"
else
  warn "rsync not installed; falling back to tar"
  run bash -c "tar -C '$REPO' \
      --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
      --exclude='data' --exclude='.env' --exclude='.pytest_cache' --exclude='reference' \
      --exclude='node_modules' \
      --exclude='*wallet*.txt' --exclude='*wallets*.txt' --exclude='*Wallet*.txt' \
      --exclude='*seed*.txt' --exclude='*mnemonic*' --exclude='*private*key*' \
      --exclude='*privkey*' --exclude='secrets' \
      --exclude='*.key' --exclude='*_ed25519' --exclude='*_rsa' --exclude='*.ppk' \
      -cf - . | tar -C '$RELEASE' -xf -"
fi

# Root-owned, world-readable, nothing writable by a service user. A service that can
# rewrite its own code is not a sandbox.
run chown -R root:root "$RELEASE"
run find "$RELEASE" -type d -exec chmod 0755 {} +
run find "$RELEASE" -type f -exec chmod 0644 {} +
run find "$RELEASE/deploy" -type f -name '*.sh' -exec chmod 0755 {} +
run chmod 0755 "$RELEASE/deploy/preflight.py"

# A .env that slipped into the checkout would land in a root-readable release directory.
if [[ -e "$RELEASE/.env" ]]; then
  run rm -f "$RELEASE/.env"
  warn "removed a .env that came along in the sync; credentials belong in $KAIBA_CONF"
fi
ok "release $STAMP staged"

# ---------------------------------------------------------------------------- 3. venv

say "virtualenv at $VENV"
if [[ ! -x "$VENV/bin/python" ]]; then
  run "$PYTHON" -m venv "$VENV"
  ok "created"
else
  ok "exists"
fi
run "$VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel

say "installing the kaiba package"
# Not editable: the venv gets a real copy, so flipping `current` to a new release never
# leaves a half-imported package behind. The dashboard is not part of the kaiba
# distribution (pyproject packages = kaiba*), so it is reached through the .pth below.
# The enabled production units include Telegram ingestion, the MCP boundary and the
# isolated signer.  Installing the base distribution alone would leave those imports
# absent and produce a green-looking install followed by three immediate crash loops.
# Keep an escape hatch for a deliberately read-only research host, but make the default
# explicit and auditable rather than relying on whichever extras happened to be present.
KAIBA_EXTRAS="${KAIBA_INSTALL_EXTRAS:-production}"
if [[ -n "$KAIBA_EXTRAS" ]]; then
  run "$VENV/bin/python" -m pip install --quiet "$RELEASE[$KAIBA_EXTRAS]"
else
  run "$VENV/bin/python" -m pip install --quiet "$RELEASE"
fi

SITE="$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || echo '')"
if [[ -n "$SITE" && -d "$SITE" ]]; then
  # Why a .pth: the units run python with -I (isolated), which drops the working
  # directory from sys.path AND ignores PYTHONPATH. `dashboard` is not an installed
  # distribution, so without this uvicorn cannot import dashboard.app.
  run bash -c "printf '%s\n' '$CURRENT' > '$SITE/kaiba-dashboard.pth'"
  ok "sys.path entry for $CURRENT"
else
  warn "could not locate site-packages; the dashboard may fail to import"
fi

# ---------------------------------------------------------------------------- 4. flip current

say "pointing $CURRENT at release $STAMP"
if (( ! DRY_RUN )); then
  ln -sfn "$RELEASE" "$CURRENT.new"
  mv -Tf "$CURRENT.new" "$CURRENT"
fi
ok "$CURRENT -> $RELEASE"

# Keep the last five releases so a rollback is a symlink flip, not a re-deploy.
if (( ! DRY_RUN )); then
  mapfile -t old < <(find "$KAIBA_PREFIX/releases" -maxdepth 1 -mindepth 1 -type d | sort | head -n -5)
  for d in "${old[@]:-}"; do
    if [[ -n "$d" && "$d" != "$RELEASE" ]]; then
      rm -rf -- "$d"
      say "  pruned $(basename "$d")"
    fi
  done
fi

# ---------------------------------------------------------------------------- 5. operator configuration

# bootstrap creates the mutable config files so a host can be prepared before a release
# exists. Seed the scheduler from the release on first install, but never overwrite a
# non-empty operator file on an upgrade. The ops unit uses an absolute path under
# /etc/kaiba because a release rollback must not silently change the schedule.
seed_release_file() {
  local src="$1" dst="$2" owner="$3" group="$4" mode="$5"
  [[ -f "$src" ]] || die "release is missing required config: $src"
  if [[ -L "$dst" ]]; then
    die "$dst is a symlink; refusing to seed through it"
  fi
  if [[ -e "$dst" && ! -f "$dst" ]]; then
    die "$dst exists and is not a regular file"
  fi
  if [[ -s "$dst" ]]; then
    ok "preserving operator config $(basename "$dst")"
  else
    run install -o "$owner" -g "$group" -m "$mode" "$src" "$dst"
    ok "seeded $(basename "$dst") from the release"
  fi
}

say "operator configuration"
run install -d -m 0770 -o root -g kaiba-state "$KAIBA_CONF/config"
seed_release_file "$RELEASE/config/schedule.yaml" \
  "$KAIBA_CONF/config/schedule.yaml" root kaiba-core 0640
ok "signals.yaml remains optional; the signal poller uses documented defaults when absent"

# ---------------------------------------------------------------------------- 6. migrations

say "database migrations"
if (( DRY_RUN )); then
  echo "     would: run kaiba.core.db.migrate as kaiba-core"
else
  runuser -u kaiba-core -- env \
    KAIBA_CONFIG_DIR="$KAIBA_CONF" \
    KAIBA_DATA_DIR="$KAIBA_STATE" \
    KAIBA_DB_PATH="$KAIBA_STATE/db/kaiba.db" \
    "$VENV/bin/python" -c '
import os
from pathlib import Path
from kaiba.core import db
# db.connect takes a Path, not a str.
conn = db.connect(Path(os.environ["KAIBA_DB_PATH"]))
applied = db.migrate(conn)
print("     applied:", ", ".join(applied) if applied else "none (already current)")
' || die "migrations failed"
  ok "schema current"
fi

# ---------------------------------------------------------------------------- 7. units

say "systemd units"
#
# ENTRY POINT CONTRACT. Each unit below invokes an implemented CLI surface. Whoever
# changes kaiba/cli/** owes the matching unit and its contract test; changing a name here
# means changing a unit.
#
#   kaiba ingest run --listeners all              kaiba-ingest.service
#   kaiba ops run                                  kaiba-ops.service
#   kaiba scan run                                kaiba-scan.service
#   kaiba engine run                              kaiba-engine.service
#   kaiba protection run                          kaiba-protection.service
#   kaiba reconcile once --write-reports          kaiba-reconcile.service
#   kaiba signer serve --socket P --socket-group G --socket-mode 0660
#                                                 kaiba-signer.service
#   python -m kaiba.mcp.socket_bridge serve --socket P --socket-group G --socket-mode 0660
#                                                 kaiba-mcp.service   (FastMCP stdio over unix socket)
#   uvicorn dashboard.app:app                     kaiba-dashboard.service  (exists today)
#   hermes --profile kaiba-operator gateway run   kaiba-hermes.service     (exists today)
#
# Two protocols the units and deploy/preflight.py assume:
#   * signer socket: one JSON object per line in, one JSON object per line out.
#     {"op":"ping"} -> {"ok":true,"policy_digest":"...","wallets":{"sol":"..."}}
#     A transfer-shaped request must come back {"ok":false,"error":"..."}.
#   * reconcile --write-reports refreshes the six files in $KAIBA_STATE/reports, which
#     are the ONLY thing kaiba-agent may read out of the state directory.
#
# The runbooks also use `kaiba risk pause|resume|reduce-only|kill|lane|bounds|global`,
# `kaiba positions show|close`, `kaiba orders show|replace`, `kaiba journal add`,
# and `kaiba signer keygen|import --chain evm|sol --keystore DIR` (both print only the
# address; import reads the key from a hidden prompt, never argv). `signer rekey` and
# `signer retire` do NOT exist, and the keystore is plain 0600 files, not encrypted.
#
UNIT_SRC="$RELEASE/deploy/systemd"
UNITS=(kaiba-ingest.service kaiba-ops.service kaiba-scan.service kaiba-engine.service kaiba-protection.service
       kaiba-dashboard.service kaiba-signer.service kaiba-mcp.service kaiba-hermes.service
       kaiba-reconcile.service kaiba-reconcile.timer
       kaiba-backup.service kaiba-backup.timer kaiba.target)

for u in "${UNITS[@]}"; do
  [[ -f "$UNIT_SRC/$u" ]] || die "missing unit $u"
  if [[ "$KAIBA_PREFIX" == "/opt/kaiba" && "$KAIBA_STATE" == "/var/lib/kaiba" && "$KAIBA_CONF" == "/etc/kaiba" ]]; then
    run install -m 0644 -o root -g root "$UNIT_SRC/$u" "/etc/systemd/system/$u"
  else
    # Non-default prefixes: rewrite the paths rather than shipping placeholders, so the
    # units in the repo stay directly verifiable with systemd-analyze.
    if (( DRY_RUN )); then
      echo "     would: install $u with rewritten paths"
    else
      sed -e "s#/opt/kaiba#$KAIBA_PREFIX#g" \
          -e "s#/var/lib/kaiba#$KAIBA_STATE#g" \
          -e "s#/etc/kaiba#$KAIBA_CONF#g" \
          "$UNIT_SRC/$u" > "/etc/systemd/system/$u.tmp"
      install -m 0644 -o root -g root "/etc/systemd/system/$u.tmp" "/etc/systemd/system/$u"
      rm -f "/etc/systemd/system/$u.tmp"
    fi
  fi
done
ok "${#UNITS[@]} units installed"

# ---- signer RPC allow list ----------------------------------------------------------
# Rendered from deployment data, never hardcoded. Empty list => loopback only => the
# signer fails closed rather than reaching an address nobody approved.
if [[ -r "$KAIBA_CONF/deploy.env" ]]; then
  # shellcheck disable=SC1090
  set -a; . "$KAIBA_CONF/deploy.env"; set +a
fi
ALLOW="${KAIBA_RPC_ALLOW_CIDRS:-}"
if [[ -n "$ALLOW" ]]; then
  say "signer RPC allow list"
  DROPIN_DIR="/etc/systemd/system/kaiba-signer.service.d"
  run install -d -m 0755 "$DROPIN_DIR"
  if (( ! DRY_RUN )); then
    lines=""
    for cidr in ${ALLOW//,/ }; do
      lines+="IPAddressAllow=$cidr"$'\n'
    done
    sed "s#@RPC_ALLOW_LINES@#${lines//$'\n'/\\n}#" \
      "$UNIT_SRC/dropins/kaiba-signer-rpc-allow.conf.template" \
      > "$DROPIN_DIR/10-rpc-allow.conf"
    chmod 0644 "$DROPIN_DIR/10-rpc-allow.conf"
  fi
  ok "allow list rendered into $DROPIN_DIR/10-rpc-allow.conf"
else
  warn "KAIBA_RPC_ALLOW_CIDRS is not set in $KAIBA_CONF/deploy.env"
  warn "the signer will be able to reach loopback only (fails closed, which is correct"
  warn "for sign-only mode; set it if the signer must talk to RPC itself)"
fi

# ---- optional: relax W^X for units that spawn the node-based gmgn-cli ---------------
if [[ -n "${KAIBA_ALLOW_NODE_JIT:-}" ]]; then
  for u in ${KAIBA_ALLOW_NODE_JIT//,/ }; do
    say "relaxing MemoryDenyWriteExecute for $u (node/V8 needs W+X)"
    run install -d -m 0755 "/etc/systemd/system/$u.d"
    run install -m 0644 "$UNIT_SRC/dropins/allow-node-jit.conf" \
        "/etc/systemd/system/$u.d/20-allow-node-jit.conf"
    warn "$u is now allowed W+X memory. Record why in docs/worklogs/."
  done
fi

run systemctl daemon-reload

if (( DO_ENABLE )); then
  say "enabling units (NOT starting the trading services)"
  # Timers and the always-safe services are enabled. The engine and the signer are
  # enabled too, so a reboot brings the box back, but nothing is started here: starting
  # is a decision a person makes after preflight.
  run systemctl enable --quiet kaiba.target kaiba-signer.service kaiba-mcp.service \
      kaiba-ingest.service kaiba-ops.service kaiba-scan.service kaiba-engine.service kaiba-protection.service \
      kaiba-dashboard.service kaiba-hermes.service \
      kaiba-reconcile.timer kaiba-backup.timer
  ok "enabled"
fi

# ---------------------------------------------------------------------------- 7. caddy

if (( WITH_CADDY )); then
  say "Caddy"
  if command -v caddy >/dev/null 2>&1; then
    run install -m 0644 -o root -g root "$RELEASE/deploy/Caddyfile" /etc/caddy/Caddyfile
    if (( ! DRY_RUN )); then
      if caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
        ok "Caddyfile valid"
        systemctl reload caddy || warn "caddy reload failed; check systemctl status caddy"
      else
        warn "caddy validate failed. The most likely cause is the rate_limit directive:"
        warn "stock Caddy does not have it. Either build with"
        warn "  xcaddy build --with github.com/mholt/caddy-ratelimit"
        warn "or comment out the rate_limit blocks in /etc/caddy/Caddyfile."
        caddy validate --config /etc/caddy/Caddyfile || true
      fi
    fi
  else
    warn "caddy is not installed; skipping"
  fi
fi

# ---------------------------------------------------------------------------- done

cat <<EOF

$(ok "install complete — release $STAMP")

Nothing is running yet and nothing is armed. What is left needs a person:

 1. Credentials. Nothing on this box has any yet.
      sudo deploy/rotate-keys.sh --all
    It prompts for each value, never echoes one, writes with install -m 0600 and records
    only a sha256 fingerprint in the journal.

 2. Wallet keys. Generated on this host as the signer user, never imported from a laptop.
    --keystore is required: sudo -u does not inherit the unit's KAIBA_KEYSTORE_DIR.
      sudo -u kaiba-signer $VENV/bin/python -m kaiba.cli.main signer keygen --chain sol --keystore $KAIBA_CONF/signer/keys
      sudo -u kaiba-signer $VENV/bin/python -m kaiba.cli.main signer keygen --chain evm --keystore $KAIBA_CONF/signer/keys
    Each prints only the new address (one evm key serves every EVM chain).
    Then bind each wallet in risk.yaml (chains.<chain>.wallet) and, for the GMGN lane,
    confirm the binding in the GMGN portal per chain.

 3. Risk envelope. $KAIBA_CONF/config/risk.yaml starts at bankroll 0 and global_mode
    shadow, which means the agent can think but cannot buy. Set the per-chain bankroll
    only after the wallets are funded.

 4. Hermes. Install the profiles first as the service user, then authenticate both
    providers in that same Hermes home. This prevents a root shell from writing profiles
    under /root while the systemd unit reads /var/lib/kaiba/hermes:
      sudo -u kaiba-agent env HOME=$KAIBA_STATE/hermes HERMES_HOME=$KAIBA_STATE/hermes/.hermes \\
        $RELEASE/deploy/install-hermes.sh --root $RELEASE --python $VENV/bin/python \\
        --hermes $KAIBA_PREFIX/hermes-venv/bin/hermes
      sudo -u kaiba-agent env HOME=$KAIBA_STATE/hermes HERMES_HOME=$KAIBA_STATE/hermes/.hermes \\
        $KAIBA_PREFIX/hermes-venv/bin/hermes --profile kaiba-operator auth add anthropic
      sudo -u kaiba-agent env HOME=$KAIBA_STATE/hermes HERMES_HOME=$KAIBA_STATE/hermes/.hermes \\
        $KAIBA_PREFIX/hermes-venv/bin/hermes --profile kaiba-operator auth add openai-codex
    Repeat the two auth commands for kaiba-research and kaiba-reflect. Use the device-code
    flow on the pinned runtime; do not copy ~/.codex/auth.json or share refresh tokens.
    Verify with `auth status openai-codex` before starting kaiba-hermes.

 5. TLS. Point KAIBA_DASHBOARD_HOSTNAME at this host and re-run with --with-caddy.

 6. The gate. Before starting anything that can trade:
      sudo deploy/preflight.py
    It exits non-zero until every check is green. Read it, do not skim it.

Start order once preflight passes:
      systemctl start kaiba-signer kaiba-mcp kaiba-protection
      systemctl start kaiba-ingest kaiba-ops kaiba-scan kaiba-dashboard kaiba-hermes
      systemctl start kaiba-engine        # last: nothing decides before protection runs
      systemctl start kaiba-reconcile.timer kaiba-backup.timer

Moving a lane from shadow to canary to live is a separate, evidenced decision:
docs/runbooks/arming.md.
EOF
