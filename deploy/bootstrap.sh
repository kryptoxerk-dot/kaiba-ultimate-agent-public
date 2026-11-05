#!/usr/bin/env bash
# Create the Kaiba unix identities and the on-disk layout on an Ubuntu 24.04 host.
#
# Four identities, least privilege, the same split that the money-maker box proved:
#
#   kaiba-agent   runs Hermes. Reads the six report files and talks to the MCP socket.
#                 Cannot read the signer directory, cannot read provider credentials.
#   kaiba-core    runs ingestion, the signal engine, the protection watchdog, reconcile,
#                 backup, and (by default) the dashboard backend. Owns the state dir.
#   kaiba-signer  owns the wallet keys. Reachable only over a unix socket.
#   kaiba-dash    separate dashboard identity, used when the dashboard is exposed.
#
# Three shared groups carry the crossings between them, so no service user ever has to be
# a member of another service's primary group:
#
#   kaiba             traverse-only membership for /etc/kaiba and /var/lib/kaiba
#   kaiba-reports     read the six report files under $KAIBA_STATE/reports
#   kaiba-state       read/write the sqlite database and risk.yaml
#   kaiba-signer-ipc  connect to the signer socket
#   kaiba-mcp-ipc     connect to the MCP socket
#
# Idempotent. Re-running it changes nothing that is already correct. It REFUSES to run if
# an existing path would have to be loosened to reach the target mode: tightening is
# applied, widening is an error you have to resolve by hand, because a permission that is
# already stricter than this script expects is usually somebody's deliberate decision.
#
# Usage:
#   sudo deploy/bootstrap.sh [--prefix /opt/kaiba] [--state /var/lib/kaiba]
#                            [--conf /etc/kaiba] [--run /run/kaiba]
#                            [--separate-dash] [--dry-run]
#
# Nothing here writes a credential. bootstrap only creates the empty, correctly-owned
# files that `deploy/rotate-keys.sh` and the operator later fill in.

set -euo pipefail

KAIBA_PREFIX="${KAIBA_PREFIX:-/opt/kaiba}"
KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
KAIBA_RUN="${KAIBA_RUN:-/run/kaiba}"
SEPARATE_DASH="${KAIBA_SEPARATE_DASH:-0}"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) KAIBA_PREFIX="$2"; shift 2 ;;
    --state) KAIBA_STATE="$2"; shift 2 ;;
    --conf) KAIBA_CONF="$2"; shift 2 ;;
    --run) KAIBA_RUN="$2"; shift 2 ;;
    --separate-dash) SEPARATE_DASH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if (( DRY_RUN )); then printf '     would: %s\n' "$*"; else "$@"; fi; }

CHANGES=0
note() { CHANGES=$((CHANGES + 1)); printf '     %s\n' "$*"; }

# ---------------------------------------------------------------------------- preconditions

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo deploy/bootstrap.sh)"
command -v systemctl >/dev/null 2>&1 || die "systemd not found; this layout assumes systemd"
command -v useradd   >/dev/null 2>&1 || die "useradd not found"
[[ -d /etc/tmpfiles.d ]] || die "/etc/tmpfiles.d missing; systemd-tmpfiles is required for $KAIBA_RUN"

for p in "$KAIBA_PREFIX" "$KAIBA_STATE" "$KAIBA_CONF" "$KAIBA_RUN"; do
  [[ "$p" = /* ]] || die "paths must be absolute: $p"
done

NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"

# ---------------------------------------------------------------------------- helpers

ensure_group() {
  local name="$1"
  if getent group "$name" >/dev/null; then
    return 0
  fi
  run groupadd --system "$name"
  note "created group $name"
}

# ensure_user <name> <primary-group> <home>
ensure_user() {
  local name="$1" group="$2" home="$3"
  if getent passwd "$name" >/dev/null; then
    local shell current_home
    shell="$(getent passwd "$name" | cut -d: -f7)"
    current_home="$(getent passwd "$name" | cut -d: -f6)"
    [[ "$shell" == "$NOLOGIN" || "$shell" == "/bin/false" || "$shell" == "/usr/sbin/nologin" ]] \
      || die "user $name has an interactive shell ($shell); refusing to touch it"
    [[ "$current_home" == "$home" ]] \
      || warn "user $name home is $current_home, expected $home (left alone)"
    # A service identity that has picked up an administrative group is a weakening we will
    # not silently inherit.
    local extra
    extra="$(id -nG "$name" 2>/dev/null || true)"
    for bad in sudo adm admin wheel docker; do
      case " $extra " in
        *" $bad "*) die "user $name is in group '$bad'; remove it before running bootstrap" ;;
      esac
    done
    return 0
  fi
  run useradd --system --gid "$group" --home-dir "$home" --shell "$NOLOGIN" \
      --comment "Kaiba service identity" "$name"
  note "created user $name (primary group $group, home $home)"
}

# add_to_group <user> <group>
add_to_group() {
  local user="$1" group="$2"
  if id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$group"; then
    return 0
  fi
  run usermod --append --groups "$group" "$user"
  note "added $user to $group"
}

# Refuse to loosen. Returns non-zero (dies) when the target mode would add a permission
# bit the path does not currently have.
# check_not_weaker <path> <target-octal>
check_not_weaker() {
  local path="$1" target="$2" current
  [[ -e "$path" ]] || return 0
  current="$(stat -c '%a' "$path")"
  local c=$((8#$current)) t=$((8#$target))
  if (( (t & ~c) != 0 )); then
    die "$path is currently mode $current; the requested $target would ADD permissions. \
Fix it deliberately (chmod $target $path) or re-run with the mode you actually want."
  fi
  return 0
}

# ensure_dir <path> <owner> <group> <mode>
ensure_dir() {
  local path="$1" owner="$2" group="$3" mode="$4"
  check_not_weaker "$path" "$mode"
  if [[ ! -d "$path" ]]; then
    if [[ -e "$path" ]]; then die "$path exists and is not a directory"; fi
    run install -d -o "$owner" -g "$group" -m "$mode" "$path"
    note "created dir $path ($owner:$group $mode)"
    return 0
  fi
  local cur_owner cur_group cur_mode
  cur_owner="$(stat -c '%U' "$path")"; cur_group="$(stat -c '%G' "$path")"; cur_mode="$(stat -c '%a' "$path")"
  if [[ "$cur_owner:$cur_group" != "$owner:$group" ]]; then
    # Handing a root-owned config path to a service user is a privilege change, not a fixup.
    if [[ "$cur_owner" == "root" && "$owner" != "root" && "$path" == "$KAIBA_CONF"* && "$path" != "$KAIBA_CONF/signer" ]]; then
      die "$path is root-owned; refusing to hand it to $owner. Move the data instead."
    fi
    run chown "$owner:$group" "$path"
    note "chown $path $cur_owner:$cur_group -> $owner:$group"
  fi
  if [[ "$cur_mode" != "$mode" ]]; then
    run chmod "$mode" "$path"
    note "chmod $path $cur_mode -> $mode"
  fi
}

# ensure_file <path> <owner> <group> <mode>  (creates empty; never overwrites content)
ensure_file() {
  local path="$1" owner="$2" group="$3" mode="$4"
  check_not_weaker "$path" "$mode"
  if [[ ! -e "$path" ]]; then
    run install -o "$owner" -g "$group" -m "$mode" /dev/null "$path"
    note "created empty $path ($owner:$group $mode)"
    return 0
  fi
  [[ -f "$path" && ! -L "$path" ]] || die "$path exists and is not a regular file"
  local cur_owner cur_group cur_mode
  cur_owner="$(stat -c '%U' "$path")"; cur_group="$(stat -c '%G' "$path")"; cur_mode="$(stat -c '%a' "$path")"
  if [[ "$cur_owner:$cur_group" != "$owner:$group" ]]; then
    run chown "$owner:$group" "$path"
    note "chown $path -> $owner:$group"
  fi
  if [[ "$cur_mode" != "$mode" ]]; then
    run chmod "$mode" "$path"
    note "chmod $path $cur_mode -> $mode"
  fi
}

# ---------------------------------------------------------------------------- identities

say "identities"
for g in kaiba kaiba-reports kaiba-state kaiba-signer-ipc kaiba-mcp-ipc \
         kaiba-agent kaiba-core kaiba-signer kaiba-dash; do
  ensure_group "$g"
done

ensure_user kaiba-agent  kaiba-agent  "$KAIBA_STATE/hermes"
ensure_user kaiba-core   kaiba-core   "$KAIBA_STATE"
ensure_user kaiba-signer kaiba-signer "$KAIBA_STATE/signer"
ensure_user kaiba-dash   kaiba-dash   "$KAIBA_STATE/dash"

# Traversal only. Being in `kaiba` lets a service walk into /etc/kaiba and /var/lib/kaiba;
# it does not make any file inside readable — that is decided per file below.
for u in kaiba-agent kaiba-core kaiba-signer kaiba-dash; do
  add_to_group "$u" kaiba
done

# The agent and the dashboard read the reports kaiba-core writes.
add_to_group kaiba-agent kaiba-reports
add_to_group kaiba-dash  kaiba-reports

# Read/write on the database and on risk.yaml. kaiba-core always. kaiba-dash only when the
# dashboard runs under its own identity (--separate-dash), which is the posture for an
# exposed dashboard; by default kaiba-dashboard.service runs as kaiba-core.
add_to_group kaiba-core kaiba-state
if (( SEPARATE_DASH )); then
  add_to_group kaiba-dash kaiba-state
  say "separate dashboard identity: kaiba-dash may read/write the database"
elif id -nG kaiba-dash 2>/dev/null | tr ' ' '\n' | grep -qx kaiba-state; then
  warn "kaiba-dash is already in kaiba-state but --separate-dash was not passed."
  warn "Leaving it. Remove with: gpasswd -d kaiba-dash kaiba-state"
fi

# Only kaiba-core may speak to the signer. Hermes and the dashboard may not.
add_to_group kaiba-core   kaiba-signer-ipc
add_to_group kaiba-signer kaiba-signer-ipc

# Hermes reaches Kaiba through the MCP socket, which kaiba-core owns.
add_to_group kaiba-agent kaiba-mcp-ipc
add_to_group kaiba-core  kaiba-mcp-ipc

# kaiba-signer is deliberately NOT in kaiba-reports and NOT in kaiba-mcp-ipc.
# kaiba-agent is deliberately NOT in kaiba-signer-ipc, kaiba-core, or kaiba-dash.

# ---------------------------------------------------------------------------- code

say "code at $KAIBA_PREFIX (root-owned, read-only to every service)"
ensure_dir "$KAIBA_PREFIX"            root root 0755
ensure_dir "$KAIBA_PREFIX/releases"   root root 0755
ensure_dir "$KAIBA_PREFIX/bin"        root root 0755
# $KAIBA_PREFIX/current is a symlink managed by install.sh, not by bootstrap.
# $KAIBA_PREFIX/venv is created by install.sh as root.

# ---------------------------------------------------------------------------- config

say "config at $KAIBA_CONF (root-owned, group-readable per service)"
# 0750 root:kaiba — every service user can traverse, none can read a file it is not the
# group of. The per-file groups below are what actually grant access.
ensure_dir "$KAIBA_CONF" root kaiba 0750

ensure_file "$KAIBA_CONF/core.env"      root kaiba-core   0640   # provider + chain credentials
ensure_file "$KAIBA_CONF/dashboard.env" root kaiba-dash   0640   # dashboard password only
ensure_file "$KAIBA_CONF/hermes.env"    root kaiba-agent  0640   # telegram token only
ensure_file "$KAIBA_CONF/signer.env"    root kaiba-signer 0640   # keystore passphrase source
ensure_file "$KAIBA_CONF/deploy.env"    root root         0600   # host vars for backup/caddy/install

# risk.yaml is read by every service and written by the dashboard and by Hermes through
# the MCP tools. The `bounds` block inside it is re-read from disk on every save
# (kaiba.core.config.save_risk), so group-write here cannot widen the envelope.
ensure_dir  "$KAIBA_CONF/config" root kaiba-state 0770
ensure_file "$KAIBA_CONF/config/risk.yaml" root kaiba-state 0660

# Root-owned and read-only to everyone: the evaluator's files. The agent may read the
# signer policy and the promotion gates; it may not edit them (PLAN §3, design rules).
ensure_dir  "$KAIBA_CONF/policy" root root 0755
ensure_file "$KAIBA_CONF/policy/signer-policy.yaml" root root 0644
ensure_file "$KAIBA_CONF/policy/gates.yaml"         root root 0644

# The one directory that holds key material. 0700 kaiba-signer, and every unit except
# kaiba-signer.service also lists it under InaccessiblePaths.
ensure_dir "$KAIBA_CONF/signer" kaiba-signer kaiba-signer 0700
ensure_dir "$KAIBA_CONF/signer/keys" kaiba-signer kaiba-signer 0700

# Fingerprints of the credentials in use. sha256 only, never a value. rotate-keys.sh
# appends here and to the journal.
ensure_dir  "$KAIBA_CONF/fingerprints" root kaiba 0750
ensure_file "$KAIBA_CONF/fingerprints/current.tsv" root kaiba-core 0640

# ---------------------------------------------------------------------------- state

say "state at $KAIBA_STATE (kaiba-core owns it)"
ensure_dir "$KAIBA_STATE" kaiba-core kaiba 0750

# The database lives here. SQLite needs to create the -wal and -shm siblings, so the
# directory is group-writable rather than just the file. Group kaiba-state, so an exposed
# dashboard running as kaiba-dash can be granted access without joining kaiba-core.
ensure_dir "$KAIBA_STATE/db"       kaiba-core kaiba-state 0770
ensure_dir "$KAIBA_STATE/cache"    kaiba-core kaiba-core 0750
ensure_dir "$KAIBA_STATE/exports"  kaiba-core kaiba-core 0750
ensure_dir "$KAIBA_STATE/logs"     kaiba-core kaiba-core 0750

# The six report files. This is the whole of what kaiba-agent may read out of the state
# directory: flat JSON snapshots, refreshed by the reconcile timer. Hermes never opens the
# database, so a compromised model context cannot read wallets, keys or provider counters.
ensure_dir "$KAIBA_STATE/reports" kaiba-core kaiba-reports 0750
for report in status positions signals providers risk journal; do
  ensure_file "$KAIBA_STATE/reports/$report.json" kaiba-core kaiba-reports 0640
done

# Hermes home. Its own profiles, memory and session store live here.
ensure_dir "$KAIBA_STATE/hermes" kaiba-agent kaiba-agent 0750

# Dashboard scratch (session store, rendered fragments).
ensure_dir "$KAIBA_STATE/dash" kaiba-dash kaiba-dash 0750

# Signer state: nonce cursors and its own audit log. Not key material — that is in
# $KAIBA_CONF/signer.
ensure_dir "$KAIBA_STATE/signer" kaiba-signer kaiba-signer 0700

# Backups are root-only. The service users can neither read the encrypted archives nor
# delete them, so a compromised service cannot destroy its own history.
ensure_dir "$KAIBA_STATE/backups" root root 0700

# ---------------------------------------------------------------------------- runtime

say "runtime sockets at $KAIBA_RUN"
# /run is a tmpfs: these have to be recreated on every boot, which is what tmpfiles.d is
# for. The units also declare RuntimeDirectory= so a manual start works before a reboot.
TMPFILES=/etc/tmpfiles.d/kaiba.conf
tmpfiles_content="$(cat <<EOF
# Kaiba runtime sockets. Written by deploy/bootstrap.sh — edit there, not here.
d ${KAIBA_RUN}        0755 root         kaiba            - -
d ${KAIBA_RUN}/signer 0750 kaiba-signer kaiba-signer-ipc - -
d ${KAIBA_RUN}/mcp    0750 kaiba-core   kaiba-mcp-ipc    - -
d ${KAIBA_RUN}/core   0750 kaiba-core   kaiba-core       - -
EOF
)"
if [[ -f "$TMPFILES" ]] && [[ "$(cat "$TMPFILES")" == "$tmpfiles_content" ]]; then
  :
else
  if (( DRY_RUN )); then
    printf '     would: write %s\n' "$TMPFILES"
  else
    printf '%s\n' "$tmpfiles_content" > "$TMPFILES"
    chmod 0644 "$TMPFILES"
    note "wrote $TMPFILES"
  fi
fi
run systemd-tmpfiles --create "$TMPFILES"

# ---------------------------------------------------------------------------- verification

say "verifying the boundaries actually hold"
fail=0

# runuser is util-linux and always present for root on Ubuntu; sudo may not be installed.
as_user() { runuser -u "$1" -- "${@:2}"; }

if (( DRY_RUN )); then
  say "dry run: skipping the live boundary checks"
else
  # kaiba-agent must not be able to reach the signer directory or the core credentials.
  for forbidden in "$KAIBA_CONF/signer" "$KAIBA_CONF/core.env" "$KAIBA_CONF/signer/keys"; do
    if [[ -e "$forbidden" ]] && as_user kaiba-agent test -r "$forbidden" 2>/dev/null; then
      warn "kaiba-agent can read $forbidden — that must not be true"
      fail=1
    fi
  done

  # kaiba-dash must not reach the signer either.
  if [[ -e "$KAIBA_CONF/signer" ]] && as_user kaiba-dash test -r "$KAIBA_CONF/signer" 2>/dev/null; then
    warn "kaiba-dash can read $KAIBA_CONF/signer — that must not be true"
    fail=1
  fi

  # kaiba-agent must be able to read the reports, or Hermes is blind.
  if [[ -e "$KAIBA_STATE/reports/status.json" ]] \
     && ! as_user kaiba-agent test -r "$KAIBA_STATE/reports/status.json" 2>/dev/null; then
    warn "kaiba-agent cannot read $KAIBA_STATE/reports/status.json — Hermes will be blind"
    warn "group membership only takes effect in a new session; this is expected mid-run"
  fi
fi

# kaiba-signer must not reach the reports, the MCP socket or the database.
for g in kaiba-reports kaiba-mcp-ipc kaiba-state; do
  if id -nG kaiba-signer 2>/dev/null | tr ' ' '\n' | grep -qx "$g"; then
    warn "kaiba-signer is in $g — remove it (gpasswd -d kaiba-signer $g)"
    fail=1
  fi
done

if (( fail )); then
  die "boundary verification failed; see the warnings above"
fi

say "done — ${CHANGES} change(s)"
cat <<EOF

Layout:
  code    $KAIBA_PREFIX        root:root, read-only to services (install.sh populates it)
  config  $KAIBA_CONF          root:kaiba 0750, one env file per service group
  state   $KAIBA_STATE         kaiba-core:kaiba 0750
  reports $KAIBA_STATE/reports kaiba-core:kaiba-reports 0750 (the six files Hermes may read)
  keys    $KAIBA_CONF/signer   kaiba-signer:kaiba-signer 0700 (nothing else may enter)
  sockets $KAIBA_RUN           signer/ and mcp/ with their own ipc groups

Next:
  1. Fill the env files with deploy/rotate-keys.sh (it never prints a value).
  2. Run deploy/install.sh to sync code, build the venv and install the units.
EOF
