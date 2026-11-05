#!/usr/bin/env bash
# Nightly encrypted snapshot of everything that is expensive to lose.
#
# What goes in:
#   1. kaiba.db      via sqlite3 `.backup` — NOT a file copy. The database runs in WAL
#                    mode; copying kaiba.db while a writer holds the WAL gives you a
#                    file that opens fine and is missing the last N transactions, or
#                    refuses to open at all. `.backup` takes a consistent snapshot
#                    through the sqlite backup API and is safe against live writers.
#   2. the journal   exported as JSONL with its hash chain, so a restore can be verified
#                    against something other than the database it came from.
#   3. config/*.yaml and policy/*.yaml, preserving their /etc/kaiba subdirectories.
#   4. Hermes homes  profiles, SOUL/MEMORY, cron definitions, session store.
#
# What stays out, deliberately:
#   - /etc/kaiba/signer/**. Wallet keys are not backed up to the same place as the
#     agent's state. They are generated on the box and re-generated on a rebuild; see
#     docs/runbooks/keys.md. A backup that contains the keys is a second copy of the
#     wallet with weaker access control than the first.
#   - .env files. Credentials are rotated, not restored.
#
# Encryption: age (preferred) or gpg, to a PUBLIC recipient. This host cannot decrypt
# its own backups, which is the point: an attacker who owns the box gets ciphertext.
#
# Safe to run twice: the archive name carries a timestamp, a re-run inside the same
# second is refused rather than overwriting, and rotation only ever deletes files that
# match our own naming pattern and are older than the retention window.
#
# Usage:
#   sudo deploy/backup.sh [--out DIR] [--keep DAYS] [--no-push] [--dry-run]

set -euo pipefail

KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
BACKUP_DIR="${KAIBA_BACKUP_DIR:-$KAIBA_STATE/backups}"
DB_PATH="${KAIBA_DB_PATH:-$KAIBA_STATE/db/kaiba.db}"
HERMES_HOME_DIR="${KAIBA_HERMES_HOME:-$KAIBA_STATE/hermes}"
KEEP_DAYS="${KAIBA_BACKUP_KEEP_DAYS:-14}"
AGE_RECIPIENTS="${AGE_RECIPIENTS:-}"        # e.g. "age1abc... age1def..." (public keys)
AGE_RECIPIENTS_FILE="${AGE_RECIPIENTS_FILE:-}"
GPG_RECIPIENT="${GPG_RECIPIENT:-}"          # e.g. a key id or uid, public key only
RCLONE_REMOTE="${RCLONE_REMOTE:-}"          # e.g. "b2:kaiba-backups/vps1"; empty = local only
PUSH=1
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) BACKUP_DIR="$2"; shift 2 ;;
    --keep) KEEP_DAYS="$2"; shift 2 ;;
    --no-push) PUSH=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,36p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || die "--keep takes a number of days"
(( KEEP_DAYS >= 1 )) || die "--keep must be at least 1"

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 not installed (apt install sqlite3)"
command -v tar     >/dev/null 2>&1 || die "tar not installed"

# ---------------------------------------------------------------------------- encryption

ENCRYPT_CMD=()
if [[ -n "$AGE_RECIPIENTS" || -n "$AGE_RECIPIENTS_FILE" ]]; then
  command -v age >/dev/null 2>&1 || die "AGE_RECIPIENTS is set but age is not installed"
  ENCRYPT_CMD=(age)
  if [[ -n "$AGE_RECIPIENTS_FILE" ]]; then
    [[ -r "$AGE_RECIPIENTS_FILE" ]] || die "cannot read AGE_RECIPIENTS_FILE"
    ENCRYPT_CMD+=(-R "$AGE_RECIPIENTS_FILE")
  fi
  for r in $AGE_RECIPIENTS; do ENCRYPT_CMD+=(-r "$r"); done
  EXT="tar.age"
elif [[ -n "$GPG_RECIPIENT" ]]; then
  command -v gpg >/dev/null 2>&1 || die "GPG_RECIPIENT is set but gpg is not installed"
  ENCRYPT_CMD=(gpg --batch --yes --trust-model always --encrypt --recipient "$GPG_RECIPIENT" --output -)
  EXT="tar.gpg"
else
  die "no recipient configured. Set AGE_RECIPIENTS (or AGE_RECIPIENTS_FILE, or \
GPG_RECIPIENT) in $KAIBA_CONF/deploy.env. An unencrypted backup of a trading journal and \
a wallet graph is not a backup, it is a liability."
fi

# ---------------------------------------------------------------------------- staging

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$BACKUP_DIR/kaiba-$STAMP.$EXT"
MANIFEST="$BACKUP_DIR/kaiba-$STAMP.manifest.json"

if [[ -e "$ARCHIVE" ]]; then die "$ARCHIVE already exists; refusing to overwrite"; fi

install -d -m 0700 "$BACKUP_DIR"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/kaiba-backup.XXXXXXXX")"
chmod 0700 "$STAGE"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT INT TERM

say "staging in $STAGE"

# ---- 1. database -----------------------------------------------------------------
#
# Run sqlite3 as the database's owner, never as root. Opening a WAL database read-write
# as root creates -wal and -shm owned by root:root in the db directory, and the service
# users then cannot write to their own database. That failure surfaces hours later as
# "attempt to write a readonly database" in the engine, which is a miserable thing to
# debug at 3am.
DB_OWNER="$(stat -c '%U' "$DB_PATH" 2>/dev/null || echo '')"
SQLITE_AS=()
if [[ "$(id -u)" -eq 0 && -n "$DB_OWNER" && "$DB_OWNER" != "root" ]]; then
  command -v runuser >/dev/null 2>&1 || die "runuser not found; needed to read the db as $DB_OWNER"
  SQLITE_AS=(runuser -u "$DB_OWNER" --)
fi

if [[ -f "$DB_PATH" ]]; then
  say "sqlite .backup of $(basename "$DB_PATH") as ${DB_OWNER:-$(id -un)} (a file copy of a WAL db is not safe)"
  install -d -m 0700 "$STAGE/db"
  if [[ ${#SQLITE_AS[@]} -gt 0 ]]; then chown "$DB_OWNER" "$STAGE/db"; fi
  # The backup API copies through sqlite itself and retries on SQLITE_BUSY; the timeout
  # keeps it from giving up under a write burst from the ingest loop.
  "${SQLITE_AS[@]}" sqlite3 "$DB_PATH" <<SQL || die "sqlite .backup failed"
.timeout 30000
.backup '$STAGE/db/kaiba.db'
SQL
  [[ -s "$STAGE/db/kaiba.db" ]] || die "sqlite .backup produced nothing"
  chown root:root "$STAGE/db" "$STAGE/db/kaiba.db" 2>/dev/null || true
  mv "$STAGE/db/kaiba.db" "$STAGE/kaiba.db"
  rmdir "$STAGE/db"

  # Prove the snapshot opens and is internally consistent before we ship it. A backup
  # nobody checked is a guess. immutable=1 reads it without creating -wal/-shm siblings
  # that would otherwise end up inside the archive.
  integrity="$(sqlite3 "file:$STAGE/kaiba.db?immutable=1" 'PRAGMA integrity_check;' | head -1)"
  [[ "$integrity" == "ok" ]] || die "snapshot failed integrity_check: $integrity"
  say "  integrity_check ok"
else
  warn "no database at $DB_PATH — backing up configuration only"
fi

# ---- 2. journal ------------------------------------------------------------------
# Exported separately from the database so a restore can verify the chain against an
# independent artefact. Hash-chained and append-only (CONTRACT.md), so the export is
# both the data and its own tamper evidence.
if [[ -f "$STAGE/kaiba.db" ]]; then
  say "exporting the journal with its hash chain"
  sqlite3 -json "file:$STAGE/kaiba.db?immutable=1" \
    "SELECT seq, ts_ms, kind, subject, body, refs_json, prev_hash, entry_hash \
     FROM journal ORDER BY seq ASC;" > "$STAGE/journal.json" 2>/dev/null \
    || warn "journal table not present yet"
  if [[ -s "$STAGE/journal.json" ]]; then
    say "  $(grep -c '"seq"' "$STAGE/journal.json" || echo 0) entries"
  fi
fi

# ---- 3. config -------------------------------------------------------------------
say "config"
install -d -m 0700 "$STAGE/config"
install -d -m 0700 "$STAGE/policy"
install -d -m 0700 "$STAGE/fingerprints"
# Only YAML. The .env files are NOT copied: credentials get rotated, not restored.
shopt -s nullglob
for f in "$KAIBA_CONF/config"/*.yaml; do
  cp -p "$f" "$STAGE/config/"
done
for f in "$KAIBA_CONF/policy"/*.yaml; do
  cp -p "$f" "$STAGE/policy/"
done
if [[ -f "$KAIBA_CONF/fingerprints/current.tsv" ]]; then
  # sha256 fingerprints only — this file never holds a value.
  cp -p "$KAIBA_CONF/fingerprints/current.tsv" "$STAGE/fingerprints/"
fi
shopt -u nullglob

# ---- 4. Hermes homes -------------------------------------------------------------
if [[ -d "$HERMES_HOME_DIR" ]]; then
  say "Hermes profiles, memory and cron definitions"
  install -d -m 0700 "$STAGE/hermes"
  # Exclude the credential store and the caches. Model OAuth tokens are re-authed on a
  # rebuild (`hermes auth add`), not restored from a backup.
  tar -C "$HERMES_HOME_DIR" -cf - \
      --exclude='./.hermes/credentials*' \
      --exclude='./.hermes/auth*' \
      --exclude='./.hermes/cache' \
      --exclude='./.hermes/logs' \
      --exclude='*.session' \
      --exclude='*.sock' \
      . 2>/dev/null | tar -C "$STAGE/hermes" -xf - || warn "hermes home partially copied"
fi

# ---- manifest --------------------------------------------------------------------
# Written inside the archive AND beside it, so the operator can see what a given archive
# holds without decrypting it.
{
  printf '{\n'
  printf '  "schema": "kaiba_backup_v1",\n'
  printf '  "created_utc": "%s",\n' "$STAMP"
  printf '  "host": "%s",\n' "$(hostname -s)"
  printf '  "encryption": "%s",\n' "${ENCRYPT_CMD[0]}"
  printf '  "contents": [\n'
  first=1
  while IFS= read -r -d '' f; do
    rel="${f#"$STAGE"/}"
    sum="$(sha256sum "$f" | cut -d' ' -f1)"
    size="$(stat -c '%s' "$f")"
    (( first )) || printf ',\n'
    first=0
    printf '    {"path": "%s", "sha256": "%s", "bytes": %s}' "$rel" "$sum" "$size"
  done < <(find "$STAGE" -type f -print0 | sort -z)
  printf '\n  ]\n}\n'
} > "$STAGE/manifest.json"

cp "$STAGE/manifest.json" "$MANIFEST.tmp"

# ---- encrypt ---------------------------------------------------------------------
say "encrypting to $ARCHIVE"
if (( DRY_RUN )); then
  echo "     would: tar -C $STAGE -cf - . | ${ENCRYPT_CMD[*]} > $ARCHIVE"
  rm -f "$MANIFEST.tmp"
  say "dry run complete; nothing written"
  exit 0
fi

umask 077
# Write to a temporary name and rename on success, so an interrupted run never leaves a
# truncated archive that looks complete to the rotation logic below.
tar -C "$STAGE" -cf - . | "${ENCRYPT_CMD[@]}" > "$ARCHIVE.part"
mv "$ARCHIVE.part" "$ARCHIVE"
mv "$MANIFEST.tmp" "$MANIFEST"
chmod 0600 "$ARCHIVE" "$MANIFEST"
say "wrote $(du -h "$ARCHIVE" | cut -f1) to $ARCHIVE"

# ---- rotate ----------------------------------------------------------------------
# Only our own files, only older than the window, and only if at least one newer archive
# exists — rotation must never leave zero backups.
say "rotating archives older than $KEEP_DAYS days"
newer_count="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name "kaiba-*.$EXT" -mtime "-$KEEP_DAYS" | wc -l)"
if (( newer_count < 1 )); then
  warn "no archive newer than $KEEP_DAYS days; skipping rotation entirely"
else
  deleted=0
  while IFS= read -r -d '' old; do
    rm -f -- "$old" "${old%.$EXT}.manifest.json"
    deleted=$((deleted + 1))
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "kaiba-*.$EXT" -mtime "+$KEEP_DAYS" -print0)
  say "  removed $deleted"
fi
# Sweep abandoned .part files from interrupted runs older than a day.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'kaiba-*.part' -mtime +1 -delete

# ---- optional offsite push -------------------------------------------------------
if (( PUSH )) && [[ -n "$RCLONE_REMOTE" ]]; then
  if command -v rclone >/dev/null 2>&1; then
    say "pushing to the configured remote"
    # No --delete: the remote keeps its own retention. A bug here must not be able to
    # wipe the offsite copy.
    if rclone copy --quiet --immutable "$ARCHIVE" "$RCLONE_REMOTE/" \
       && rclone copy --quiet --immutable "$MANIFEST" "$RCLONE_REMOTE/"; then
      say "  pushed"
    else
      warn "rclone push failed; the local archive is intact"
      exit 3
    fi
  else
    warn "RCLONE_REMOTE is set but rclone is not installed; local copy only"
  fi
fi

say "done"
