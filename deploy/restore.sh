#!/usr/bin/env bash
# Restore a Kaiba backup, then PROVE the journal hash chain survived.
#
# The verification is the point of this script. A restored database that opens cleanly
# tells you almost nothing: sqlite will happily open a file whose journal has been
# truncated, reordered or edited. The journal is hash-chained (CONTRACT.md,
# kaiba.core.journal), so a restore either reproduces the chain or it does not, and a
# broken chain means the history of what the agent did with money is not trustworthy.
#
# This script REFUSES to exit 0 on a broken chain. It leaves the restored files in place
# under a quarantine name so you can investigate, and it tells you what to do next.
#
# It never writes over a live installation without being told to. By default it restores
# into a scratch directory and verifies there; --activate is what moves it into place,
# and --activate refuses to run while the services are up.
#
# Usage:
#   sudo deploy/restore.sh --archive /var/lib/kaiba/backups/kaiba-<stamp>.tar.age \
#                          [--identity /path/to/age-identity] \
#                          [--into /var/lib/kaiba/restore] [--activate] [--force]
#
# The identity file is the private half of the age/gpg recipient the backup was encrypted
# to. It does NOT live on this host — bring it in for the restore and take it away again.

set -euo pipefail

KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
KAIBA_PREFIX="${KAIBA_PREFIX:-/opt/kaiba}"
VENV_PY="${KAIBA_VENV_PY:-$KAIBA_PREFIX/venv/bin/python}"
ARCHIVE=""
IDENTITY="${AGE_IDENTITY_FILE:-}"
RESTORE_DIR="${KAIBA_RESTORE_DIR:-$KAIBA_STATE/restore}"
ACTIVATE=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) ARCHIVE="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --into) RESTORE_DIR="$2"; shift 2 ;;
    --activate) ACTIVATE=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
bad()  { printf '\033[31m FAIL\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ -n "$ARCHIVE" ]] || die "--archive is required"
[[ -f "$ARCHIVE" ]] || die "no such archive: $ARCHIVE"
[[ "$(id -u)" -eq 0 ]] || die "run as root"

SERVICES=(kaiba-ingest kaiba-ops kaiba-scan kaiba-engine kaiba-protection kaiba-dashboard kaiba-mcp kaiba-hermes)

# ---------------------------------------------------------------------------- decrypt

case "$ARCHIVE" in
  *.tar.age)
    command -v age >/dev/null 2>&1 || die "age not installed"
    [[ -n "$IDENTITY" ]] || die "--identity is required for an age archive"
    [[ -r "$IDENTITY" ]] || die "cannot read identity file"
    DECRYPT=(age --decrypt -i "$IDENTITY")
    ;;
  *.tar.gpg)
    command -v gpg >/dev/null 2>&1 || die "gpg not installed"
    DECRYPT=(gpg --batch --quiet --decrypt)
    ;;
  *.tar)
    warn "archive is not encrypted"
    DECRYPT=(cat)
    ;;
  *) die "unrecognised archive type: $ARCHIVE" ;;
esac

say "restoring into $RESTORE_DIR"
if [[ -d "$RESTORE_DIR" && -n "$(ls -A "$RESTORE_DIR" 2>/dev/null)" ]] && (( ! FORCE )); then
  die "$RESTORE_DIR is not empty; move it aside or pass --force"
fi
rm -rf "${RESTORE_DIR:?}"
install -d -m 0700 "$RESTORE_DIR"

umask 077
"${DECRYPT[@]}" < "$ARCHIVE" | tar -C "$RESTORE_DIR" -xf - \
  || die "decrypt/extract failed — wrong identity, or the archive is damaged"
ok "extracted"

# ---------------------------------------------------------------------------- manifest

if [[ -f "$RESTORE_DIR/manifest.json" ]]; then
  say "checking the manifest digests"
  mismatch=0
  while IFS=$'\t' read -r rel sum; do
    [[ -n "$rel" ]] || continue
    if [[ "$rel" == "manifest.json" ]]; then continue; fi
    if [[ ! -f "$RESTORE_DIR/$rel" ]]; then
      bad "missing from archive: $rel"; mismatch=1; continue
    fi
    actual="$(sha256sum "$RESTORE_DIR/$rel" | cut -d' ' -f1)"
    if [[ "$actual" != "$sum" ]]; then
      bad "digest mismatch: $rel"; mismatch=1
    fi
  done < <(grep -o '{"path": "[^"]*", "sha256": "[^"]*"' "$RESTORE_DIR/manifest.json" \
           | sed 's/{"path": "//; s/", "sha256": "/\t/')
  if (( mismatch )); then
    die "manifest verification failed; this archive is not intact"
  fi
  ok "manifest digests match"
else
  warn "no manifest.json in the archive; skipping digest verification"
fi

# ---------------------------------------------------------------------------- database

DB="$RESTORE_DIR/kaiba.db"
if [[ -f "$DB" ]]; then
  say "sqlite integrity_check"
  integrity="$(sqlite3 "file:$DB?immutable=1" 'PRAGMA integrity_check;' | head -1)"
  [[ "$integrity" == "ok" ]] || die "restored database fails integrity_check: $integrity"
  ok "integrity_check"
else
  die "archive contains no kaiba.db; there is nothing to verify"
fi

# ---------------------------------------------------------------------------- the point
#
# Verify the hash chain with the project's own implementation, not a reimplementation
# here. If kaiba.core.journal.verify() and this script ever disagreed about what a valid
# chain is, the one that matters is the one the agent uses.

say "verifying the journal hash chain (kaiba.core.journal.verify)"
CHAIN_OK=0
if [[ -x "$VENV_PY" ]]; then
  set +e
  chain_out="$(KAIBA_DB_PATH="$DB" "$VENV_PY" -c '
import json, os, sys
from pathlib import Path
from kaiba.core import db, journal
# db.connect takes a Path, not a str.
conn = db.connect(Path(os.environ["KAIBA_DB_PATH"]))
ok, err = journal.verify(conn)
st = journal.stats(conn)
print(json.dumps({"ok": bool(ok), "error": err, "total": st.get("total")}))
sys.exit(0 if ok else 1)
' 2>&1)"
  rc=$?
  set -e
  echo "     $chain_out"
  if (( rc == 0 )); then CHAIN_OK=1; fi
else
  warn "$VENV_PY not found — falling back to an in-script chain walk"
  set +e
  chain_out="$(python3 - "$DB" <<'PY' 2>&1
import hashlib, json, sqlite3, sys
GENESIS = "0" * 64
conn = sqlite3.connect(f"file:{sys.argv[1]}?immutable=1", uri=True)
conn.row_factory = sqlite3.Row
prev, n = GENESIS, 0
for r in conn.execute("SELECT * FROM journal ORDER BY seq ASC"):
    blob = f"{r['seq']}|{r['ts_ms']}|{r['kind']}|{r['subject'] or ''}|{r['body']}|{prev}"
    expect = hashlib.sha256(blob.encode()).hexdigest()
    if r["prev_hash"] != prev:
        print(json.dumps({"ok": False, "error": f"seq {r['seq']}: prev_hash mismatch", "total": n}))
        sys.exit(1)
    if r["entry_hash"] != expect:
        print(json.dumps({"ok": False, "error": f"seq {r['seq']}: entry_hash mismatch", "total": n}))
        sys.exit(1)
    prev, n = r["entry_hash"], n + 1
print(json.dumps({"ok": True, "error": None, "total": n}))
PY
)"
  rc=$?
  set -e
  echo "     $chain_out"
  if (( rc == 0 )); then CHAIN_OK=1; fi
fi

# Cross-check against the independently exported journal.json, if the archive has one.
# Same chain, two artefacts: if the database was edited after the export, this catches it.
if [[ -s "$RESTORE_DIR/journal.json" ]]; then
  say "cross-checking against the exported journal"
  set +e
  cross="$(python3 - "$DB" "$RESTORE_DIR/journal.json" <<'PY' 2>&1
import json, sqlite3, sys
db, export = sys.argv[1], sys.argv[2]
rows = json.load(open(export, encoding="utf-8"))
conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
have = {r[0]: r[1] for r in conn.execute("SELECT seq, entry_hash FROM journal")}
bad = [r["seq"] for r in rows if have.get(r["seq"]) != r["entry_hash"]]
missing = [r["seq"] for r in rows if r["seq"] not in have]
print(json.dumps({"export_entries": len(rows), "db_entries": len(have),
                  "hash_disagreements": bad[:10], "missing_from_db": missing[:10]}))
sys.exit(1 if bad or missing else 0)
PY
)"
  cross_rc=$?
  set -e
  echo "     $cross"
  if (( cross_rc != 0 )); then CHAIN_OK=0; fi
fi

# ---------------------------------------------------------------------------- verdict

if (( ! CHAIN_OK )); then
  QUARANTINE="${RESTORE_DIR%/}-BROKEN-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$RESTORE_DIR" "$QUARANTINE"
  bad "journal hash chain does NOT verify"
  cat >&2 <<EOF

The restored data is quarantined at:
    $QUARANTINE

Do not activate it and do not arm anything against it. A broken chain means one of:

  * the archive was tampered with or truncated in transit;
  * the database was edited outside the append-only journal API (an UPDATE or DELETE on
    the journal table, which nothing in kaiba is allowed to do);
  * the backup captured the database mid-write because something bypassed
    sqlite .backup and copied the file.

Next: take the most recent archive that DOES verify, restore that instead, and
reconcile forward from its last event id. docs/runbooks/upgrade.md has the procedure.
EOF
  exit 1
fi

ok "journal hash chain verified"

# ---------------------------------------------------------------------------- activate

if (( ! ACTIVATE )); then
  cat <<EOF

Verified, not activated. The restored tree is at:
    $RESTORE_DIR

To put it live:
    sudo systemctl stop kaiba.target
    sudo deploy/restore.sh --archive "$ARCHIVE" --identity <identity> --activate

Activation stops here rather than guessing, because replacing a live database under a
running engine loses whatever it has written since the snapshot.
EOF
  exit 0
fi

say "activating"
running=()
for s in "${SERVICES[@]}"; do
  if systemctl is-active --quiet "$s.service"; then running+=("$s"); fi
done
if (( ${#running[@]} > 0 )) && (( ! FORCE )); then
  die "these services are running: ${running[*]}. Stop them first (systemctl stop kaiba.target)."
fi

# Never overwrite; always move the current files aside first. A restore that turns out to
# be the wrong archive must be undoable.
ASIDE="$KAIBA_STATE/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$ASIDE"
for f in "$KAIBA_STATE/db/kaiba.db" "$KAIBA_STATE/db/kaiba.db-wal" "$KAIBA_STATE/db/kaiba.db-shm"; do
  if [[ -e "$f" ]]; then mv "$f" "$ASIDE/"; fi
done
say "previous database moved to $ASIDE"

install -o kaiba-core -g kaiba-state -m 0660 "$DB" "$KAIBA_STATE/db/kaiba.db"
ok "database restored"

shopt -s nullglob

restore_yaml_file() {
  local source_file="$1" target_dir="$2" aside_dir="$3" owner="$4" group="$5" mode="$6"
  local base="$(basename "$source_file")"
  install -d -m 0700 "$aside_dir"
  if [[ -e "$target_dir/$base" ]]; then
    cp -p "$target_dir/$base" "$aside_dir/$base"
  fi
  install -o "$owner" -g "$group" -m "$mode" "$source_file" "$target_dir/$base"
  ok "config: ${target_dir##*/}/$base"
}

restore_yaml_dir() {
  local source_dir="$1" target_dir="$2" aside_dir="$3" owner="$4" group="$5" mode="$6"
  local f
  [[ -d "$source_dir" ]] || return 0
  for f in "$source_dir"/*.yaml; do
    restore_yaml_file "$f" "$target_dir" "$aside_dir" "$owner" "$group" "$mode"
  done
}

# New archives preserve /etc/kaiba/config and /etc/kaiba/policy as separate top-level
# directories. Older archives merged both into RESTORE_DIR/config; classify those by the
# known mutable config names so a legacy schedule is not restored as policy.
if [[ -d "$RESTORE_DIR/policy" ]]; then
  restore_yaml_dir "$RESTORE_DIR/config" "$KAIBA_CONF/config" "$ASIDE/config" \
    root kaiba-state 0660
  restore_yaml_dir "$RESTORE_DIR/policy" "$KAIBA_CONF/policy" "$ASIDE/policy" \
    root root 0644
else
  for f in "$RESTORE_DIR/config"/*.yaml; do
    base="$(basename "$f")"
    case "$base" in
      risk.yaml|schedule.yaml|signals.yaml)
        restore_yaml_file "$f" "$KAIBA_CONF/config" "$ASIDE/config" \
          root kaiba-state 0660
        ;;
      *)
        restore_yaml_file "$f" "$KAIBA_CONF/policy" "$ASIDE/policy" \
          root root 0644
        ;;
    esac
  done
fi

if [[ -f "$RESTORE_DIR/fingerprints/current.tsv" ]]; then
  install -d -m 0700 "$ASIDE/fingerprints"
  if [[ -e "$KAIBA_CONF/fingerprints/current.tsv" ]]; then
    cp -p "$KAIBA_CONF/fingerprints/current.tsv" "$ASIDE/fingerprints/current.tsv"
  fi
  install -o root -g kaiba-core -m 0640 "$RESTORE_DIR/fingerprints/current.tsv" \
    "$KAIBA_CONF/fingerprints/current.tsv"
  ok "config: fingerprints/current.tsv"
elif [[ -f "$RESTORE_DIR/config/current.tsv" ]]; then
  # Legacy archives stored the fingerprint beside the YAML files.
  install -d -m 0700 "$ASIDE/fingerprints"
  if [[ -e "$KAIBA_CONF/fingerprints/current.tsv" ]]; then
    cp -p "$KAIBA_CONF/fingerprints/current.tsv" "$ASIDE/fingerprints/current.tsv"
  fi
  install -o root -g kaiba-core -m 0640 "$RESTORE_DIR/config/current.tsv" \
    "$KAIBA_CONF/fingerprints/current.tsv"
  ok "config: fingerprints/current.tsv (legacy archive)"
fi
shopt -u nullglob

if [[ -d "$RESTORE_DIR/hermes" ]]; then
  say "Hermes home: NOT restored automatically"
  cat <<EOF
     The profiles are extracted at $RESTORE_DIR/hermes. Copy what you need by hand and
     re-run deploy/install-hermes.sh. Model credentials were deliberately excluded from
     the backup, so a blind copy would produce a profile that cannot authenticate.
EOF
fi

cat <<EOF

$(ok "activated")

Before you start anything:
  1. sudo deploy/preflight.py              # migrations, chain, envelope, signer, clock
  2. sudo systemctl start kaiba-reconcile  # reconcile order state against the chain
  3. Read docs/runbooks/upgrade.md before restarting the engine. The restored database is
     as of the snapshot; anything the agent did after it is in $ASIDE, not here.
  4. Lanes come back at whatever mode risk.yaml says. Check that before starting the
     engine, not after: docs/runbooks/arming.md.
EOF
