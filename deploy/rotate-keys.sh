#!/usr/bin/env bash
# Guided credential rotation.
#
# Every credential listed in docs/research/01-prior-work-inventory.md §8 must be treated
# as exposed: they sat in plain-text files on a Windows box for months. Rotating them is
# Phase 0 work (PLAN §10), and this is the tool for it.
#
# The rules this script exists to enforce:
#
#   * It NEVER prints a value. Not the old one, not the new one, not a prefix, not a
#     "last four". The only thing it displays or records is a sha256 fingerprint, which
#     is enough to answer "is the box running the key I just made?" and useless to an
#     attacker reading a terminal recording or a log.
#   * It never puts a value in a command line, so nothing shows up in `ps`.
#   * It writes with `install -m 0600`, atomically, to a root-owned file.
#   * It records the change in the hash-chained journal: name, fingerprint, timestamp.
#     Never the value.
#   * Shell history and xtrace are disabled for the duration.
#
# You rotate in the provider's dashboard first; this script only installs the result.
#
# Usage:
#   sudo deploy/rotate-keys.sh --list
#   sudo deploy/rotate-keys.sh GMGN_API_KEY TELEGRAM_BOT_TOKEN
#   sudo deploy/rotate-keys.sh --all
#   sudo deploy/rotate-keys.sh --verify          # fingerprints only, no prompts

set -uo pipefail
set +o history 2>/dev/null || true
set +x

KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
KAIBA_PREFIX="${KAIBA_PREFIX:-/opt/kaiba}"
KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
PY="${KAIBA_VENV_PY:-$KAIBA_PREFIX/venv/bin/python}"
FINGERPRINTS="$KAIBA_CONF/fingerprints/current.tsv"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------------
# The catalogue: NAME | target env file | where you regenerate it
#
# Grouped by which service must be able to see it, which is what decides the file and
# therefore the unix group. A key in the wrong file is a key the wrong service can read.
# ---------------------------------------------------------------------------------
CATALOGUE=(
  # --- execution and signing (core.env; kaiba-core) ---
  "GMGN_API_KEY|core|GMGN portal -> API keys. Revoke the old key in the same visit."
  "GMGN_PRIVATE_KEY|core|gmgn-cli config: the Ed25519 REQUEST-signing key, not a wallet key."

  # --- operator surface (hermes.env; kaiba-agent) ---
  "TELEGRAM_BOT_TOKEN|hermes|@BotFather -> /revoke then /token for @your_kaiba_bot."
  "TG_API_ID|hermes|my.telegram.org -> API development tools (userbot listener)."
  "TG_API_HASH|hermes|my.telegram.org -> API development tools."

  # --- dashboard (dashboard.env; kaiba-dash) ---
  "KAIBA_DASHBOARD_PASSWORD|dashboard|Choose a fresh passphrase; it is the only auth in front of the kill switch."

  # --- solana data (core.env) ---
  "HELIUS_API_KEY|core|dashboard.helius.dev -> API keys -> regenerate."
  "HELIUS_WEBHOOK_SECRET|core|Helius webhook config; must match the receiver."
  "SOLANA_TRACKER_API_KEY|core|solanatracker.io account -> Data API."
  "BIRDEYE_API_KEY|core|bds.birdeye.so -> API keys."
  "PUMPPORTAL_API_KEY|core|pumpportal.fun -> API key (only for metered streams)."
  "JUPITER_API_KEY|core|portal.jup.ag -> API keys."
  "BITQUERY_TOKEN|core|account.bitquery.io -> access tokens."

  # --- evm / robinhood data (core.env) ---
  "ALCHEMY_API_KEY|core|dashboard.alchemy.com -> app -> API key -> rotate."
  "ALCHEMY_WEBHOOK_SIGNING_KEY|core|Alchemy -> Webhooks -> signing key."
  "ETHERSCAN_API_KEY|core|etherscan.io -> API keys."
  "BLOCKSCOUT_API_KEY|core|Blockscout instance -> account -> API keys."

  # --- safety / enrichment (core.env) ---
  "GOPLUS_APP_KEY|core|gopluslabs.io developer console."
  "GOPLUS_APP_SECRET|core|gopluslabs.io developer console."
  "RUGCHECK_JWT|core|rugcheck.xyz -> re-sign in to mint a new JWT."
  "COINGECKO_API_KEY|core|coingecko.com developer dashboard."
  "NANSEN_API_KEY|core|Nansen account -> API."
  "CIELO_API_KEY|core|Cielo account -> API."
  "MOBULA_API_KEY|core|Mobula dashboard."
  "CODEX_IO_API_KEY|core|codex.io dashboard."
  "TWITTERSCORE_API_KEY|core|twitterscore.io account."

  # --- news / models (core.env) ---
  "CRYPTOPANIC_API_KEY|core|cryptopanic.com -> developers."
  "NEWSAPI_KEY|core|newsapi.org account."
  "XAI_API_KEY|core|console.x.ai -> API keys (per-post billing since 2026-09-21)."
  "ANTHROPIC_API_KEY|core|console.anthropic.com. Optional: Hermes uses OAuth, not this."
  "OPENROUTER_API_KEY|core|openrouter.ai -> keys."

  # --- observability (core.env) ---
  "LANGFUSE_PUBLIC_KEY|core|Langfuse project settings."
  "LANGFUSE_SECRET_KEY|core|Langfuse project settings."

  # --- signer (signer.env; kaiba-signer only) ---
  "KAIBA_SIGNER_PASSPHRASE|signer|Keystore passphrase. Rotating this RE-ENCRYPTS the keystore: run 'kaiba signer rekey' afterwards, and do not lose the old one until it succeeds."
)

target_file() {
  case "$1" in
    core)      printf '%s\n' "$KAIBA_CONF/core.env" ;;
    hermes)    printf '%s\n' "$KAIBA_CONF/hermes.env" ;;
    dashboard) printf '%s\n' "$KAIBA_CONF/dashboard.env" ;;
    signer)    printf '%s\n' "$KAIBA_CONF/signer.env" ;;
    *) die "unknown target group: $1" ;;
  esac
}

target_group() {
  case "$1" in
    core)      printf '%s\n' "kaiba-core" ;;
    hermes)    printf '%s\n' "kaiba-agent" ;;
    dashboard) printf '%s\n' "kaiba-dash" ;;
    signer)    printf '%s\n' "kaiba-signer" ;;
  esac
}

lookup() {
  local name="$1" entry
  for entry in "${CATALOGUE[@]}"; do
    if [[ "${entry%%|*}" == "$name" ]]; then printf '%s\n' "$entry"; return 0; fi
  done
  return 1
}

# fingerprint_of <<< value   — reads the value on stdin, prints sha256. The value never
# appears in argv, in an environment variable, or on screen.
fingerprint_of() { sha256sum | cut -d' ' -f1; }

# ---------------------------------------------------------------------------------

MODE="rotate"
NAMES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)   MODE="list"; shift ;;
    --verify) MODE="verify"; shift ;;
    --all)    MODE="all"; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) NAMES+=("$1"); shift ;;
  esac
done

if [[ "$MODE" == "list" ]]; then
  printf '%-30s %-12s %s\n' "NAME" "FILE" "WHERE TO REGENERATE"
  for entry in "${CATALOGUE[@]}"; do
    IFS='|' read -r name group where <<<"$entry"
    printf '%-30s %-12s %s\n' "$name" "$(basename "$(target_file "$group")")" "$where"
  done
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || die "run as root"

if [[ "$MODE" == "verify" ]]; then
  say "installed credential fingerprints (sha256, first 12 hex)"
  if [[ ! -s "$FINGERPRINTS" ]]; then
    warn "no fingerprints recorded yet at $FINGERPRINTS"
    exit 1
  fi
  printf '%-30s %-14s %s\n' "NAME" "FINGERPRINT" "ROTATED"
  while IFS=$'\t' read -r name fp when _rest; do
    [[ -n "${name:-}" ]] || continue
    printf '%-30s %-14s %s\n' "$name" "${fp:0:12}" "$when"
  done < "$FINGERPRINTS"
  exit 0
fi

if [[ "$MODE" == "all" ]]; then
  NAMES=()
  for entry in "${CATALOGUE[@]}"; do NAMES+=("${entry%%|*}"); done
fi

if [[ ${#NAMES[@]} -eq 0 ]]; then
  die "name at least one credential, or use --all / --list / --verify"
fi

install -d -m 0750 -o root -g kaiba "$KAIBA_CONF/fingerprints" 2>/dev/null || true
[[ -e "$FINGERPRINTS" ]] || install -m 0640 -o root -g kaiba-core /dev/null "$FINGERPRINTS"

cat <<'EOF'

Before you start
----------------
Rotate in the provider's dashboard FIRST, and revoke the old credential there in the
same visit. A key that still works somewhere is not rotated, it is duplicated.

This script will not show you any value you type. If you fat-finger one, you will find
out from `kaiba probe`, not from this screen. That is deliberate.

Press Enter with an empty value to skip a credential.

EOF

ROTATED=0
SKIPPED=0

for name in "${NAMES[@]}"; do
  if ! entry="$(lookup "$name")"; then
    warn "$name is not in the catalogue; skipping (run --list)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  IFS='|' read -r _ group where <<<"$entry"
  file="$(target_file "$group")"
  fgroup="$(target_group "$group")"

  printf '\n\033[1m%s\033[0m -> %s\n' "$name" "$file"
  printf '  %s\n' "$where"

  # Existing fingerprint, so the operator can tell afterwards that it actually changed.
  old_fp=""
  if [[ -r "$file" ]]; then
    old_value="$(sed -n "s/^${name}=//p" "$file" | head -1)"
    if [[ -n "$old_value" ]]; then
      old_fp="$(printf '%s' "$old_value" | fingerprint_of)"
      printf '  currently installed: %s...\n' "${old_fp:0:12}"
    else
      printf '  currently installed: (none)\n'
    fi
    unset old_value
  fi

  # -s: no echo. -r: no backslash mangling, which matters for keys with escapes in them.
  printf '  new value (input hidden, Enter to skip): '
  IFS= read -rs value || value=""
  printf '\n'

  if [[ -z "$value" ]]; then
    printf '  skipped\n'
    SKIPPED=$((SKIPPED + 1))
    unset value
    continue
  fi

  new_fp="$(printf '%s' "$value" | fingerprint_of)"
  if [[ -n "$old_fp" && "$new_fp" == "$old_fp" ]]; then
    warn "  that is the SAME value that is already installed — nothing rotated"
    unset value
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Reject a value that is obviously a paste accident before it breaks a service.
  case "$value" in
    *$'\n'*|*$'\r'*) warn "  value contains a newline; refusing"; unset value; SKIPPED=$((SKIPPED+1)); continue ;;
    " "*|*" ") warn "  value has leading or trailing whitespace; refusing"; unset value; SKIPPED=$((SKIPPED+1)); continue ;;
  esac
  # systemd's EnvironmentFile parser processes quotes and backslashes, so a
  # credential containing them can reach the process differently from what you typed.
  case "$value" in
    *'"'*|*"'"*|*\\*) warn "  value contains a quote or backslash: systemd may re-interpret it. Verify with kaiba probe." ;;
  esac

  # Atomic write. The temporary file is created under the target's own directory (same
  # filesystem, so the rename is atomic) with a 077 umask, and is removed on any exit.
  umask 077
  tmp="$(mktemp "${file}.XXXXXXXX")"
  trap 'rm -f "$tmp" 2>/dev/null' EXIT
  if [[ -f "$file" ]]; then
    grep -v "^${name}=" "$file" > "$tmp" || true
  else
    : > "$tmp"
  fi
  # printf, not echo: no interpretation, and the value never becomes an argv entry of
  # any external command.
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
  unset value

  # Created 0600 so the new file is never readable by anyone, not even for the instant
  # between creation and the chown. It then lands at 0640 root:<service group>, which is
  # what bootstrap.sh establishes; systemd reads EnvironmentFile as root before dropping
  # privileges, so the group read is a convenience for `kaiba probe`, not a requirement.
  install -m 0600 -o root -g root "$tmp" "$file.new"
  chown "root:$fgroup" "$file.new"
  chmod 0640 "$file.new"
  mv -f "$file.new" "$file"
  rm -f "$tmp"
  trap - EXIT

  ok "$name installed  fingerprint ${new_fp:0:12}...  (root:$fgroup 0640)"

  # Record: name, fingerprint, when, target file. No value, ever.
  when="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  tmpfp="$(mktemp "${FINGERPRINTS}.XXXXXXXX")"
  grep -v "^${name}"$'\t' "$FINGERPRINTS" > "$tmpfp" 2>/dev/null || true
  printf '%s\t%s\t%s\t%s\n' "$name" "$new_fp" "$when" "$(basename "$file")" >> "$tmpfp"
  sort -o "$tmpfp" "$tmpfp"
  install -m 0640 -o root -g kaiba-core "$tmpfp" "$FINGERPRINTS"
  rm -f "$tmpfp"

  # And into the hash-chained journal, so the rotation is part of the agent's history
  # and a later post-mortem can line a provider failure up against a key change.
  if [[ -x "$PY" ]]; then
    runuser -u kaiba-core -- env \
      KAIBA_ROT_NAME="$name" KAIBA_ROT_FP="$new_fp" KAIBA_ROT_OLD="${old_fp:0:12}" \
      KAIBA_DB_PATH="${KAIBA_DB_PATH:-$KAIBA_STATE/db/kaiba.db}" \
      "$PY" -c '
import os
from pathlib import Path
from kaiba.core import db, journal
# db.connect takes a Path, not a str.
conn = db.connect(Path(os.environ["KAIBA_DB_PATH"]))
db.migrate(conn)
name = os.environ["KAIBA_ROT_NAME"]
new = os.environ["KAIBA_ROT_FP"][:12]
old = os.environ.get("KAIBA_ROT_OLD") or "none"
journal.append(
    "change",
    f"credential rotated: {name} sha256:{new} (was sha256:{old}); value never logged",
    subject=name,
    conn=conn,
)
' >/dev/null 2>&1 && ok "  journalled" || warn "  could not write the journal entry"
  else
    warn "  no venv python at $PY; the rotation is NOT in the journal"
  fi

  ROTATED=$((ROTATED + 1))
  old_fp=""
done

cat <<EOF

$(say "rotated $ROTATED, skipped $SKIPPED")

Nothing above printed a credential value, and nothing wrote one to the journal, to a
log, or to your shell history.

Next:
  1. Restart whatever reads what you changed. Env files are read by PID 1 at start, so a
     rotation does not take effect until a restart:
        systemctl restart kaiba-ingest kaiba-ops kaiba-scan kaiba-engine kaiba-mcp   # core.env
        systemctl restart kaiba-hermes                          # hermes.env
        systemctl restart kaiba-dashboard                       # dashboard.env
        systemctl restart kaiba-signer                          # signer.env
  2. Prove the new credentials work before you trust them:
        sudo -u kaiba-core $PY -m kaiba.cli.main probe
  3. Confirm what is installed, by fingerprint:
        sudo deploy/rotate-keys.sh --verify
  4. Revoke the old credential in the provider's dashboard if you have not already.

If you rotated KAIBA_SIGNER_PASSPHRASE, the keystore is still encrypted with the OLD
one until you re-key it. Do that now, and keep the old passphrase until it reports
success: docs/runbooks/keys.md.
EOF
