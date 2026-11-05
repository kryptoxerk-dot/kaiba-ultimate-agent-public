#!/usr/bin/env bash
# One screen of truth about a Kaiba host, in a single SSH round trip.
#
#   ssh kaiba-vps 'sudo /opt/kaiba/current/deploy/status.sh'
#
# What it answers, in the order you want it at 3am:
#   is anything down · how stale is the event stream · what is open and is it protected ·
#   which providers are cooled down · will the disk fill · what mode is each lane in.
#
# Read-only. It starts nothing, stops nothing and writes nothing. Safe to run as often
# as you like, and safe to run while everything is on fire.
#
# Never prints a secret: credential presence is reported as a name and a boolean.
#
# Usage: deploy/status.sh [--json] [--db PATH]

set -uo pipefail   # deliberately NOT -e: a broken section must not hide the rest

KAIBA_PREFIX="${KAIBA_PREFIX:-/opt/kaiba}"
KAIBA_STATE="${KAIBA_STATE:-/var/lib/kaiba}"
KAIBA_CONF="${KAIBA_CONF:-/etc/kaiba}"
DB_PATH="${KAIBA_DB_PATH:-$KAIBA_STATE/db/kaiba.db}"
RISK_PATH="${KAIBA_RISK_PATH:-$KAIBA_CONF/config/risk.yaml}"
PY="${KAIBA_VENV_PY:-$KAIBA_PREFIX/venv/bin/python}"
JSON=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1; shift ;;
    --db) DB_PATH="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -x "$PY" ]] || PY="$(command -v python3 || true)"

UNITS=(kaiba-signer kaiba-mcp kaiba-ingest kaiba-ops kaiba-scan kaiba-engine kaiba-protection
       kaiba-dashboard kaiba-hermes)
TIMERS=(kaiba-reconcile.timer kaiba-backup.timer)

hr() { printf '%s\n' "------------------------------------------------------------------------"; }

if (( ! JSON )); then
  printf '\033[1mKAIBA  %s  %s  up %s\033[0m\n' \
    "$(hostname -s)" "$(date -u '+%Y-%m-%d %H:%M:%SZ')" "$(uptime -p 2>/dev/null | sed 's/^up //')"
  hr

  # ---------------------------------------------------------------- units
  printf '\033[1mSERVICES\033[0m\n'
  for u in "${UNITS[@]}"; do
    state="$(systemctl is-active "$u.service" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$u.service" 2>/dev/null || echo '-')"
    since="$(systemctl show -p ActiveEnterTimestamp --value "$u.service" 2>/dev/null || true)"
    nrestarts="$(systemctl show -p NRestarts --value "$u.service" 2>/dev/null || echo 0)"
    case "$state" in
      active)   colour='\033[32m' ;;
      inactive) colour='\033[33m' ;;
      failed)   colour='\033[31m' ;;
      *)        colour='\033[0m'  ;;
    esac
    # A protection service that is not active is the single worst line on this screen.
    flag=''
    if [[ "$u" == "kaiba-protection" && "$state" != "active" ]]; then
      flag=' <-- POSITIONS ARE UNPROTECTED'
      colour='\033[31;1m'
    fi
    printf "  %-18s ${colour}%-9s\033[0m %-9s restarts=%-4s %s%s\n" \
      "$u" "${state:-unknown}" "$enabled" "${nrestarts:-0}" "${since:-}" "$flag"
  done
  for t in "${TIMERS[@]}"; do
    state="$(systemctl is-active "$t" 2>/dev/null || true)"
    next="$(systemctl show -p NextElapseUSecRealtime --value "$t" 2>/dev/null || true)"
    last="$(systemctl show -p LastTriggerUSec --value "${t%.timer}.service" 2>/dev/null || true)"
    printf '  %-18s %-9s next=%s last=%s\n' "$t" "${state:-unknown}" "${next:-?}" "${last:-never}"
  done
  hr
fi

# ---------------------------------------------------------------- database facts
#
# One python process does every database question, so the whole script is one SSH call
# and one sqlite open. stdlib only: this must work even when the venv is broken, which
# is exactly when you are reading it.
if [[ -n "$PY" && -r "$DB_PATH" ]]; then
  KAIBA_STATUS_DB="$DB_PATH" KAIBA_STATUS_RISK="$RISK_PATH" KAIBA_STATUS_JSON="$JSON" \
  "$PY" - <<'PY'
import json, os, sqlite3, sys, time

db_path = os.environ["KAIBA_STATUS_DB"]
risk_path = os.environ["KAIBA_STATUS_RISK"]
as_json = os.environ.get("KAIBA_STATUS_JSON") == "1"
now = int(time.time() * 1000)
out = {}

def table_exists(c, name):
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None

def human_age(ms):
    if ms is None:
        return "never"
    s = max(0, (now - ms) // 1000)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"

# mode=ro never blocks a writer and never creates a file. It does need the -shm to
# already exist for a WAL database, which is true whenever any service is running. When
# everything is stopped the -shm is gone and mode=ro fails, so fall back to immutable=1
# — safe precisely because nothing is writing at that moment.
conn = None
for uri in (f"file:{db_path}?mode=ro", f"file:{db_path}?immutable=1"):
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1").fetchone()
        break
    except Exception as exc:
        last_error = exc
        conn = None
if conn is None:
    print(f"  database unreadable: {last_error}")
    sys.exit(0)

# ---- event stream ------------------------------------------------------------------
if table_exists(conn, "events"):
    row = conn.execute("SELECT MAX(id) AS id FROM events").fetchone()
    last_id = row["id"] if row else None
    ts = None
    if last_id:
        ts = conn.execute("SELECT ts_ms FROM events WHERE id=?", (last_id,)).fetchone()["ts_ms"]
    counts = {
        r["kind"]: r["n"]
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM events WHERE ts_ms > ? GROUP BY kind "
            "ORDER BY n DESC LIMIT 6",
            (now - 3600_000,),
        )
    }
    errors = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE level IN ('error','warn') AND ts_ms > ?",
        (now - 3600_000,),
    ).fetchone()["n"]
    out["events"] = {"last_id": last_id, "age": human_age(ts), "last_hour": counts,
                     "warn_error_last_hour": errors}

# ---- positions ---------------------------------------------------------------------
if table_exists(conn, "positions"):
    rows = [dict(r) for r in conn.execute(
        "SELECT position_id, chain, token, lane, mode, opened_ms, protected, "
        "       cost_native, realized_native, exit_reason "
        "FROM positions WHERE closed_ms IS NULL ORDER BY opened_ms DESC LIMIT 25"
    )]
    out["open_positions"] = [
        {"id": r["position_id"][:12], "chain": r["chain"], "token": r["token"][:10],
         "lane": r["lane"], "mode": r["mode"], "age": human_age(r["opened_ms"]),
         "protected": bool(r["protected"])}
        for r in rows
    ]
    out["unprotected"] = sum(1 for r in rows if not r["protected"])

# ---- orders in an unresolved state --------------------------------------------------
if table_exists(conn, "orders"):
    unresolved = [dict(r) for r in conn.execute(
        "SELECT state, COUNT(*) AS n FROM orders "
        "WHERE state NOT IN ('filled','cancelled','failed','rejected') GROUP BY state"
    )]
    if unresolved:
        out["orders_unresolved"] = {r["state"]: r["n"] for r in unresolved}

# ---- provider cooldowns -------------------------------------------------------------
providers = {}
if table_exists(conn, "provider_state"):
    for r in conn.execute("SELECT * FROM provider_state"):
        banned = r["banned_until_ms"] or 0
        providers[r["provider"]] = {
            "cooldown_s": max(0, (banned - now) // 1000),
            "penalty": r["penalty_level"],
            "spent_today": r["spent_today"],
        }
if table_exists(conn, "provider_family_bans"):
    for r in conn.execute("SELECT * FROM provider_family_bans WHERE banned_until_ms > ?", (now,)):
        p = providers.setdefault(r["provider"], {})
        p.setdefault("families_cooled", []).append(
            f"{r['family']}:{(r['banned_until_ms'] - now) // 1000}s"
        )
if providers:
    out["providers"] = providers

# ---- journal ------------------------------------------------------------------------
if table_exists(conn, "journal"):
    r = conn.execute("SELECT COUNT(*) AS n, MAX(ts_ms) AS last FROM journal").fetchone()
    out["journal"] = {"entries": r["n"], "last": human_age(r["last"])}

conn.close()

# ---- lane modes ---------------------------------------------------------------------
# Parsed straight out of the YAML rather than through kaiba.core.config, so a broken
# import or a missing dependency still leaves this screen useful.
lanes, globals_ = {}, {}
try:
    text = open(risk_path, encoding="utf-8").read()
    section, lane = None, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        # Strip a trailing inline comment: risk.yaml documents its enums inline
        # ("global_mode: shadow        # off | shadow | canary | live") and without
        # this the mode reads as the whole comment.
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if indent == 0 and line.endswith(":"):
            section = line[:-1]
            lane = None
            continue
        if indent == 0 and ":" in line:
            k, _, v = line.partition(":")
            if k in ("global_mode", "kill_switch", "entries_paused", "reduce_only"):
                globals_[k] = v.strip().strip('"')
            section = None
            continue
        if section == "lanes" and indent == 2 and line.endswith(":"):
            lane = line[:-1].strip('"')
            lanes[lane] = "?"
            continue
        if section == "lanes" and lane and line.startswith("mode:"):
            lanes[lane] = line.split(":", 1)[1].strip().strip('"')
    out["risk"] = globals_
    out["lanes"] = lanes
except FileNotFoundError:
    out["risk"] = {"error": f"no risk file at {risk_path}"}
except Exception as exc:
    out["risk"] = {"error": str(exc)}

if as_json:
    print(json.dumps(out, indent=2, sort_keys=True))
    sys.exit(0)

# ---- render -------------------------------------------------------------------------
ev = out.get("events", {})
print("\033[1mEVENTS\033[0m")
print(f"  last id {ev.get('last_id', '-')}   age {ev.get('age', '-')}"
      f"   warn/error last hour: {ev.get('warn_error_last_hour', '-')}")
if ev.get("last_hour"):
    print("  last hour: " + "  ".join(f"{k}={v}" for k, v in ev["last_hour"].items()))
age = ev.get("age") or ""
if age and age != "never" and age.endswith(("m", "h", "d")):
    print("  \033[33mthe event stream is stale — ingestion is probably down\033[0m")

print()
print("\033[1mPOSITIONS\033[0m")
pos = out.get("open_positions")
if pos is None:
    print("  (no positions table yet)")
elif not pos:
    print("  none open")
else:
    print(f"  {'id':<13}{'chain':<11}{'token':<12}{'lane':<16}{'mode':<8}{'age':<7}prot")
    for p in pos:
        mark = "yes" if p["protected"] else "\033[31;1mNO\033[0m"
        print(f"  {p['id']:<13}{p['chain']:<11}{p['token']:<12}{p['lane']:<16}"
              f"{p['mode']:<8}{p['age']:<7}{mark}")
    if out.get("unprotected"):
        print(f"  \033[31;1m{out['unprotected']} open position(s) with no protection — "
              f"see docs/runbooks/incident-position-unprotected.md\033[0m")
if out.get("orders_unresolved"):
    print("  \033[33munresolved orders: "
          + ", ".join(f"{k}={v}" for k, v in out["orders_unresolved"].items())
          + "  -> docs/runbooks/incident-ambiguous-send.md\033[0m")

print()
print("\033[1mPROVIDERS\033[0m")
if not out.get("providers"):
    print("  no provider state recorded yet")
else:
    for name, p in sorted(out["providers"].items()):
        cd = p.get("cooldown_s", 0)
        bits = [f"cooldown={cd}s" if cd else "ready"]
        if p.get("penalty"):
            bits.append(f"penalty={p['penalty']}")
        if p.get("spent_today"):
            bits.append(f"spent_today={p['spent_today']}")
        if p.get("families_cooled"):
            bits.append("cooled: " + ",".join(p["families_cooled"]))
        colour = "\033[33m" if cd else ""
        reset = "\033[0m" if cd else ""
        print(f"  {colour}{name:<16}{' '.join(bits)}{reset}")

print()
print("\033[1mLANES\033[0m")
g = out.get("risk", {})
if g.get("error"):
    print(f"  {g['error']}")
else:
    kill = str(g.get("kill_switch", "false")).lower() == "true"
    banner = "\033[31;1mKILL SWITCH ON\033[0m" if kill else f"global={g.get('global_mode', '?')}"
    print(f"  {banner}   entries_paused={g.get('entries_paused', '?')}"
          f"   reduce_only={g.get('reduce_only', '?')}")
    for lane, mode in sorted(out.get("lanes", {}).items()):
        colour = {"live": "\033[31m", "canary": "\033[33m", "shadow": "\033[32m"}.get(mode, "")
        reset = "\033[0m" if colour else ""
        effective = "off" if kill else mode
        note = "  (kill switch)" if kill and mode != "off" else ""
        print(f"    {lane:<18}{colour}{mode:<9}{reset}effective={effective}{note}")

j = out.get("journal")
if j:
    print()
    print(f"\033[1mJOURNAL\033[0m  {j['entries']} entries, last {j['last']}")
PY
else
  if [[ -z "$PY" ]]; then
    echo "  no python available; skipping the database section"
  else
    echo "  cannot read $DB_PATH (run with sudo, or check the kaiba-state group)"
  fi
fi

# ---------------------------------------------------------------- disk and memory
if (( ! JSON )); then
  echo
  hr
  printf '\033[1mDISK\033[0m\n'
  df -h "$KAIBA_STATE" "$KAIBA_PREFIX" /var/log 2>/dev/null \
    | awk 'NR==1 || /%/ {printf "  %s\n", $0}' | sort -u
  # A full disk stops sqlite writes, which stops the journal, which stops protection
  # recording its own exits. Shout about it early.
  used="$(df --output=pcent "$KAIBA_STATE" 2>/dev/null | tail -1 | tr -dc '0-9')"
  if [[ -n "$used" ]] && (( used > 85 )); then
    printf '  \033[31;1mdisk at %s%% — sqlite writes will start failing\033[0m\n' "$used"
  fi
  if [[ -d "$KAIBA_STATE/backups" ]]; then
    n="$(find "$KAIBA_STATE/backups" -maxdepth 1 -name 'kaiba-*.tar.*' 2>/dev/null | wc -l)"
    newest="$(find "$KAIBA_STATE/backups" -maxdepth 1 -name 'kaiba-*.tar.*' -printf '%T@ %p\n' 2>/dev/null \
              | sort -rn | head -1 | cut -d' ' -f2-)"
    if [[ -n "$newest" ]]; then
      age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
      printf '  backups: %s archives, newest %sh old\n' "$n" "$age_h"
      if (( age_h > 36 )); then
        printf '  \033[33mno backup in %sh — check kaiba-backup.timer\033[0m\n' "$age_h"
      fi
    else
      printf '  \033[33mbackups: none yet\033[0m\n'
    fi
  fi

  echo
  printf '\033[1mRECENT FAILURES\033[0m\n'
  failed="$(systemctl list-units --state=failed --no-legend 'kaiba*' 2>/dev/null)"
  if [[ -n "$failed" ]]; then
    printf '  \033[31m%s\033[0m\n' "$failed"
    echo "  journalctl -u <unit> -n 50 --no-pager"
  else
    echo "  none"
  fi
  hr
fi
