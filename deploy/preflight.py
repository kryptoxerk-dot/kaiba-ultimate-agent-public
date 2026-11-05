#!/usr/bin/env python3
"""Pre-arm gate. Run this, read it, and only then decide whether to go live.

This is the checklist between "the software is installed" and "the software is allowed
to spend money". It answers nine questions:

    1. migrations   is the schema actually current, or is the code ahead of the database
    2. journal      does the hash chain verify end to end
    3. envelope     does risk.yaml parse, and are the bounds sane
    4. wallets      does every enabled chain have a bound wallet and a non-zero bankroll
    5. signer       does the socket answer, and does it refuse a withdrawal
    6. providers    does the probe come back green
    7. hermes       are the three profiles installed where the units expect them
    8. clock        is the skew under 2 seconds
    9. layout       do the unix boundaries still hold (kaiba-agent cannot read the keys)

Every check prints PASS, FAIL, or SKIP with a reason. The exit code is non-zero if any
check fails, so it works as a gate in a script as well as a thing a person reads.

A check that cannot run is a FAIL, not a pass. "We could not verify the signer" and
"the signer is fine" are different sentences, and only one of them is a reason to arm.

Never prints a credential value: presence is reported as a name and a boolean.

Usage:
    sudo deploy/preflight.py [--json] [--skip signer,providers] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------
# deployment paths — every one overridable, nothing about this host baked in
# --------------------------------------------------------------------------------------

PREFIX = Path(os.environ.get("KAIBA_PREFIX", "/opt/kaiba"))
STATE = Path(os.environ.get("KAIBA_STATE", "/var/lib/kaiba"))
CONF = Path(os.environ.get("KAIBA_CONF", "/etc/kaiba"))
RUN = Path(os.environ.get("KAIBA_RUN", "/run/kaiba"))

DB_PATH = Path(os.environ.get("KAIBA_DB_PATH", STATE / "db" / "kaiba.db"))
RISK_PATH = Path(os.environ.get("KAIBA_RISK_PATH", CONF / "config" / "risk.yaml"))
SIGNER_SOCKET = Path(os.environ.get("KAIBA_SIGNER_SOCKET", RUN / "signer" / "signer.sock"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", STATE / "hermes" / ".hermes"))
REPORTS_DIR = Path(os.environ.get("KAIBA_REPORTS_DIR", STATE / "reports"))

MAX_CLOCK_SKEW_S = float(os.environ.get("KAIBA_MAX_CLOCK_SKEW_S", "2.0"))
HERMES_PROFILES = ("kaiba-operator", "kaiba-research", "kaiba-reflect")
REPORTS = ("status.json", "positions.json", "signals.json",
           "providers.json", "risk.json", "journal.json")

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        # A SKIP is a failure of the gate even though it is not a failure of the system:
        # you cannot arm on an unverified check. --skip is the operator saying so out loud.
        return self.status in (FAIL, SKIP)


RESULTS: list[Result] = []


def record(name: str, status: str, detail: str = "", lines: list[str] | None = None) -> Result:
    r = Result(name, status, detail, lines or [])
    RESULTS.append(r)
    return r


def _import_kaiba():
    """Import the installed package, adding the release dir if we are run from a checkout."""
    try:
        import kaiba  # noqa: F401
        return True, ""
    except ImportError:
        for candidate in (PREFIX / "current", Path(__file__).resolve().parent.parent):
            if (candidate / "kaiba" / "__init__.py").exists():
                sys.path.insert(0, str(candidate))
                try:
                    import kaiba  # noqa: F401
                    return True, f"imported from {candidate}"
                except ImportError as exc:
                    return False, str(exc)
        return False, "kaiba package not importable and no checkout found"


# --------------------------------------------------------------------------------------
# 1. migrations
# --------------------------------------------------------------------------------------

def check_migrations() -> None:
    name = "migrations current"
    if not DB_PATH.exists():
        record(name, FAIL, f"no database at {DB_PATH} — run deploy/install.sh")
        return
    try:
        import sqlite3

        from kaiba.core import db as coredb
    except Exception as exc:
        record(name, FAIL, f"cannot import kaiba.core.db: {exc}")
        return

    on_disk = sorted(p.name for p in coredb.MIGRATIONS_DIR.glob("*.sql"))
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
        conn.close()
    except sqlite3.OperationalError as exc:
        record(name, FAIL, f"cannot read schema_migrations: {exc}")
        return
    applied = {r[0] for r in rows}
    missing = [m for m in on_disk if m not in applied]
    extra = sorted(applied - set(on_disk))

    if missing:
        record(name, FAIL,
               f"{len(missing)} unapplied: {', '.join(missing)}",
               ["the running code expects tables the database does not have",
                "fix: sudo deploy/install.sh   (it runs migrate as kaiba-core)"])
    elif extra:
        # The database is ahead of the code: almost always a rollback that went too far.
        record(name, FAIL,
               f"database has migrations this release does not: {', '.join(extra)}",
               ["you have rolled the code back past a schema change",
                "fix: roll forward, or restore the matching database snapshot"])
    else:
        record(name, PASS, f"{len(on_disk)} applied, none pending")


# --------------------------------------------------------------------------------------
# 2. journal chain
# --------------------------------------------------------------------------------------

def check_journal() -> None:
    name = "journal hash chain"
    if not DB_PATH.exists():
        record(name, FAIL, f"no database at {DB_PATH}")
        return
    try:
        import sqlite3

        from kaiba.core import journal
    except Exception as exc:
        record(name, FAIL, f"cannot import kaiba.core.journal: {exc}")
        return
    try:
        # Read-only, deliberately: kaiba.core.db.connect() would create the file and its
        # parent directories, and a gate that writes is not a gate. journal.verify reads
        # rows by column name, so it needs the Row factory.
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        ok, err = journal.verify(conn)
        stats = journal.stats(conn)
        conn.close()
    except Exception as exc:
        record(name, FAIL, f"verify raised: {exc}")
        return

    if ok:
        record(name, PASS, f"{stats.get('total', 0)} entries, chain intact")
    else:
        record(name, FAIL, f"chain broken at {err}",
               ["the record of what this agent did with money is not trustworthy",
                "do not arm. docs/runbooks/upgrade.md covers restoring from a verified backup"])


# --------------------------------------------------------------------------------------
# 3 + 4. risk envelope, wallets and bankroll
# --------------------------------------------------------------------------------------

def check_risk_and_wallets() -> None:
    try:
        from kaiba.core.config import load_risk
    except Exception as exc:
        record("risk envelope parses", FAIL, f"cannot import kaiba.core.config: {exc}")
        record("chains bound and funded", SKIP, "risk config did not load")
        return

    if not RISK_PATH.exists():
        record("risk envelope parses", FAIL, f"no risk file at {RISK_PATH}")
        record("chains bound and funded", SKIP, "no risk file")
        return

    try:
        risk = load_risk(RISK_PATH)
    except Exception as exc:
        record("risk envelope parses", FAIL, f"{type(exc).__name__}: {exc}",
               ["a risk.yaml that does not parse means every service falls back to",
                "defaults you did not choose. Fix the YAML before anything starts."])
        record("chains bound and funded", SKIP, "risk config did not parse")
        return

    b = risk.bounds
    notes = [
        f"global_mode={risk.global_mode.value}  kill_switch={risk.kill_switch}  "
        f"entries_paused={risk.entries_paused}  reduce_only={risk.reduce_only}",
        f"bounds: max_size={b.max_size_pct_bankroll}% of bankroll  "
        f"max_daily_loss={b.max_daily_loss_pct}%  max_slippage={b.max_slippage_bps}bps  "
        f"ceiling={b.max_lane_mode.value}",
    ]
    problems = []
    if b.max_size_pct_bankroll <= 0:
        problems.append("max_size_pct_bankroll is 0: nothing can ever size a position")
    if b.max_size_pct_bankroll > 25:
        problems.append(f"max_size_pct_bankroll is {b.max_size_pct_bankroll}% — that is a"
                        " concentration limit in name only")
    if b.max_daily_loss_pct <= 0 or b.max_daily_loss_pct > 50:
        problems.append(f"max_daily_loss_pct {b.max_daily_loss_pct} is outside a sane range")
    if b.max_slippage_bps > 5000:
        problems.append(f"max_slippage_bps {b.max_slippage_bps} allows a 50% haircut")

    record("risk envelope parses", FAIL if problems else PASS,
           "; ".join(problems) if problems else f"{risk.version}, bounds sane", notes)

    # ---- per chain -------------------------------------------------------------------
    lines, bad = [], []
    enabled = [(c, cb) for c, cb in risk.chains.items() if cb.enabled]
    if not enabled:
        record("chains bound and funded", FAIL, "no chain is enabled in risk.yaml")
        return

    for chain, cb in sorted(enabled, key=lambda kv: kv[0].value):
        issues = []
        if not cb.wallet:
            issues.append("no wallet bound")
        if cb.bankroll_base_units <= 0:
            issues.append("bankroll is 0")
        if cb.max_position_base_units <= 0:
            issues.append("max_position is 0")
        if cb.gas_reserve_base_units <= 0:
            issues.append("no gas reserve: a position you cannot exit is not a position")
        if (cb.bankroll_base_units and cb.max_position_base_units
                and cb.max_position_base_units > cb.bankroll_base_units):
            issues.append("max_position exceeds the whole bankroll")
        if (cb.bankroll_base_units and cb.daily_loss_stop_base_units
                and cb.daily_loss_stop_base_units > cb.bankroll_base_units):
            issues.append("daily_loss_stop exceeds the bankroll: it can never trigger")

        # Wallet addresses are money-adjacent identifiers; show a truncated form only.
        w = cb.wallet or ""
        shown = f"{w[:6]}…{w[-4:]}" if len(w) > 12 else (w or "(unbound)")
        mark = "ok " if not issues else "BAD"
        lines.append(f"    {mark} {chain.value:<11} wallet={shown:<14} "
                     f"bankroll={cb.bankroll_base_units:<22} max_pos={cb.max_position_base_units}")
        if issues:
            bad.append(f"{chain.value}: {'; '.join(issues)}")
            lines.append(f"        -> {'; '.join(issues)}")

    record("chains bound and funded",
           FAIL if bad else PASS,
           "; ".join(bad) if bad else f"{len(enabled)} enabled chain(s) bound and funded",
           lines)

    # ---- lanes -----------------------------------------------------------------------
    lane_lines = []
    for lane, cfg in sorted(risk.lanes.items(), key=lambda kv: kv[0].value):
        eff = risk.effective_mode(lane)
        flag = "  <-- LIVE" if eff.value == "live" else ""
        lane_lines.append(f"    {lane.value:<18} configured={cfg.mode.value:<8} "
                          f"effective={eff.value}{flag}")
    live = [lane.value for lane in risk.lanes if risk.effective_mode(lane).value == "live"]
    record("lane modes", PASS,
           f"{len(live)} live: {', '.join(live)}" if live else "nothing is live",
           lane_lines)


# --------------------------------------------------------------------------------------
# 5. signer socket
# --------------------------------------------------------------------------------------

def check_signer() -> None:
    name = "signer socket answers"
    if not SIGNER_SOCKET.exists():
        record(name, FAIL, f"no socket at {SIGNER_SOCKET}",
               ["systemctl start kaiba-signer, then check journalctl -u kaiba-signer"])
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(SIGNER_SOCKET))
    except PermissionError:
        record(name, FAIL, "socket exists but this user cannot connect",
               ["run preflight as root, or add yourself to the kaiba-signer-ipc group"])
        return
    except OSError as exc:
        record(name, FAIL, f"connect failed: {exc}")
        return

    try:
        # The deployment contract: one JSON object per line, one JSON object back.
        req = json.dumps({"op": "ping", "id": f"preflight-{int(time.time())}"}) + "\n"
        s.sendall(req.encode())
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        raw = buf.split(b"\n", 1)[0].decode(errors="replace")
        reply = json.loads(raw) if raw else {}
    except Exception as exc:
        record(name, FAIL, f"no usable reply: {exc}")
        return
    finally:
        s.close()

    if reply.get("ok"):
        wallets = reply.get("wallets") or {}
        # Truncate: an address is not a secret, but a preflight transcript gets pasted
        # into chats, and there is no reason to publish the agent's wallets.
        shown = ", ".join(
            f"{k}:{str(v)[:6]}…" for k, v in list(wallets.items())[:8]
        ) or "no wallets loaded"
        detail = f"policy={reply.get('policy_digest', '?')[:12]} {shown}"
        if not wallets:
            record(name, FAIL, "signer answers but has no wallets loaded", [detail])
        else:
            record(name, PASS, detail)
    else:
        record(name, FAIL, f"signer replied but not ok: {str(reply)[:200]}")


def check_signer_refuses_withdrawal() -> None:
    """The one gate in the whole system. Prove it in the deployed process, every time.

    A policy that is only enforced in a code path nobody exercised is a policy you hope
    for. This sends a real transfer-shaped request to the running signer and requires a
    refusal. If the signer ever answers anything other than a refusal here, stop.
    """
    name = "signer refuses a withdrawal"
    if not SIGNER_SOCKET.exists():
        record(name, SKIP, "signer socket not present")
        return
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(str(SIGNER_SOCKET))
        probe = {
            "op": "sign",
            "id": f"preflight-withdrawal-{int(time.time())}",
            "dry_run": True,
            "intent": {
                # A deliberately non-owned destination. The signer must refuse on the
                # operation vocabulary alone, before it ever looks at an address.
                "operation": "transfer",
                "chain": "sol",
                "to": "PreflightNotAnOwnedAddress11111111111111111",
                "amount_base_units": 1,
            },
        }
        s.sendall((json.dumps(probe) + "\n").encode())
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0].decode(errors="replace") or "{}")
    except Exception as exc:
        record(name, FAIL, f"could not run the refusal probe: {exc}")
        return
    finally:
        try:
            s.close()
        except Exception:
            pass

    refused = (reply.get("ok") is False) and bool(reply.get("error") or reply.get("reason"))
    if refused:
        record(name, PASS, f"refused: {str(reply.get('error') or reply.get('reason'))[:120]}")
    else:
        record(name, FAIL,
               f"signer did NOT refuse a transfer: {str(reply)[:200]}",
               ["STOP. The only hard gate in the system did not hold.",
                "Do not arm, do not fund. kaiba/execution/policy.py and the signer"
                " policy in /etc/kaiba/policy/signer-policy.yaml."])


# --------------------------------------------------------------------------------------
# 6. provider probe
# --------------------------------------------------------------------------------------

def check_providers() -> None:
    name = "provider probe green"
    try:
        from kaiba.cli import probe  # noqa: F401
    except ImportError:
        record(name, FAIL, "kaiba.cli.probe is not implemented yet (task P0-4)",
               ["you cannot arm without knowing which providers answer",
                "until it lands, run the provider calls by hand and record the result"])
        return

    py = PREFIX / "venv" / "bin" / "python"
    exe = [str(py)] if py.exists() else [sys.executable]
    try:
        out = subprocess.run(
            exe + ["-m", "kaiba.cli.main", "probe", "--json"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        record(name, FAIL, "probe timed out after 120s")
        return

    if out.returncode != 0 and not out.stdout.strip():
        record(name, FAIL, f"probe exited {out.returncode}: {out.stderr.strip()[:200]}")
        return

    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        record(name, WARN, "probe ran but did not return JSON",
               [line for line in out.stdout.splitlines()[:12]])
        return

    rows = data if isinstance(data, list) else data.get("providers", [])
    bad, lines = [], []
    for row in rows:
        prov = row.get("provider") or row.get("name", "?")
        status = str(row.get("status", "?"))
        lines.append(f"    {'ok ' if status == 'ok' else 'BAD'} {prov:<16} {status}")
        if status != "ok":
            bad.append(f"{prov}={status}")
    record(name, FAIL if bad else PASS,
           "; ".join(bad) if bad else f"{len(rows)} provider(s) green", lines)


# --------------------------------------------------------------------------------------
# 7. hermes profiles
# --------------------------------------------------------------------------------------

def check_hermes() -> None:
    name = "hermes profiles installed"
    missing, lines = [], []
    for profile in HERMES_PROFILES:
        cfg = HERMES_HOME / "profiles" / profile / "config.yaml"
        soul = HERMES_HOME / "profiles" / profile / "SOUL.md"
        have = cfg.exists()
        lines.append(f"    {'ok ' if have else 'BAD'} {profile:<16} "
                     f"config={'yes' if have else 'MISSING'} "
                     f"soul={'yes' if soul.exists() else 'missing'}")
        if not have:
            missing.append(profile)
    if missing:
        record(name, FAIL, f"missing: {', '.join(missing)}",
               lines + ["fix: deploy/install-hermes.sh"])
        return

    # The units read the six report files, not the database. If they do not exist, the
    # agent starts blind and its first answer to "what is our position" is wrong.
    absent = [r for r in REPORTS if not (REPORTS_DIR / r).exists()]
    if absent:
        record(name, WARN, f"profiles ok, but {len(absent)} report file(s) missing: "
                           f"{', '.join(absent)}",
               lines + ["they are written by the reconcile timer:",
                        "  systemctl start kaiba-reconcile.service"])
    else:
        record(name, PASS, f"{len(HERMES_PROFILES)} profiles, {len(REPORTS)} report files", lines)


# --------------------------------------------------------------------------------------
# 8. clock
# --------------------------------------------------------------------------------------

def check_clock() -> None:
    """Skew matters here for two concrete reasons, not as hygiene.

    GMGN request signing includes a timestamp and rejects a skewed one, and every
    confluence window is measured in seconds against provider timestamps. A box two
    seconds out silently stops seeing 120s confluences correctly.
    """
    name = f"clock skew < {MAX_CLOCK_SKEW_S}s"

    if shutil.which("chronyc"):
        try:
            out = subprocess.run(["chronyc", "tracking"], capture_output=True,
                                 text=True, timeout=10)
            if out.returncode == 0:
                offset, leap = None, ""
                for line in out.stdout.splitlines():
                    if line.startswith("System time"):
                        parts = line.split(":", 1)[1].split()
                        offset = float(parts[0])
                    if line.startswith("Leap status"):
                        leap = line.split(":", 1)[1].strip()
                if offset is not None:
                    detail = f"chrony offset {offset:.4f}s, leap status {leap or 'unknown'}"
                    if leap and leap.lower() != "normal":
                        record(name, FAIL, f"{detail} — chrony is not synchronised")
                    else:
                        record(name, PASS if offset < MAX_CLOCK_SKEW_S else FAIL, detail)
                    return
        except Exception:
            pass

    if shutil.which("timedatectl"):
        try:
            out = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"],
                capture_output=True, text=True, timeout=10,
            )
            values = dict(
                line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
            )
            synced = values.get("NTPSynchronized") == "yes"
            record(name, PASS if synced else FAIL,
                   f"timedatectl NTPSynchronized={values.get('NTPSynchronized', '?')} "
                   f"NTP={values.get('NTP', '?')}",
                   [] if synced else
                   ["timedatectl reports the clock is not synchronised.",
                    "fix: apt install chrony && systemctl enable --now chrony",
                    "note: this only proves synchronisation, not the actual offset.",
                    "Install chrony for a measured number."])
            return
        except Exception:
            pass

    url = os.environ.get("KAIBA_TIME_URL", "")
    if url:
        try:
            import urllib.request
            from email.utils import parsedate_to_datetime

            before = time.time()
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                server_date = resp.headers.get("Date")
            after = time.time()
            if server_date:
                remote = parsedate_to_datetime(server_date).timestamp()
                local = (before + after) / 2
                skew = abs(remote - local)
                # An HTTP Date header has one-second resolution, so this can only ever
                # bound the skew, never measure it precisely.
                record(name, PASS if skew < MAX_CLOCK_SKEW_S + 1 else FAIL,
                       f"skew vs {url} is ~{skew:.1f}s (1s resolution)")
                return
        except Exception as exc:
            record(name, FAIL, f"time check against {url} failed: {exc}")
            return

    record(name, FAIL, "no clock source available",
           ["install chrony (apt install chrony) or set KAIBA_TIME_URL to an https",
            "endpoint whose Date header you trust"])


# --------------------------------------------------------------------------------------
# 9. unix boundaries
# --------------------------------------------------------------------------------------

def check_layout() -> None:
    """Re-prove the privilege separation. Permissions drift; this is cheap to check."""
    name = "unix boundaries hold"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        record(name, SKIP, "not a POSIX host")
        return
    if geteuid() != 0:
        record(name, SKIP, "needs root to test what another user can read")
        return
    if not shutil.which("runuser"):
        record(name, SKIP, "runuser not available")
        return

    problems, lines = [], []

    def readable_by(user: str, path: Path) -> bool | None:
        if not path.exists():
            return None
        out = subprocess.run(["runuser", "-u", user, "--", "test", "-r", str(path)],
                             capture_output=True)
        return out.returncode == 0

    must_not = [
        ("kaiba-agent", CONF / "signer"),
        ("kaiba-agent", CONF / "core.env"),
        ("kaiba-agent", DB_PATH),
        ("kaiba-dash", CONF / "signer"),
        ("kaiba-core", CONF / "signer" / "keys"),
    ]
    for user, path in must_not:
        r = readable_by(user, path)
        if r is None:
            lines.append(f"    --  {user:<13} {path} (absent)")
        elif r:
            problems.append(f"{user} can read {path}")
            lines.append(f"    BAD {user:<13} CAN READ {path}")
        else:
            lines.append(f"    ok  {user:<13} cannot read {path}")

    must = [("kaiba-agent", REPORTS_DIR / "status.json")]
    for user, path in must:
        r = readable_by(user, path)
        if r is None:
            lines.append(f"    --  {user:<13} {path} (absent)")
        elif not r:
            problems.append(f"{user} cannot read {path}")
            lines.append(f"    BAD {user:<13} CANNOT READ {path}")
        else:
            lines.append(f"    ok  {user:<13} can read {path}")

    # Code must not be writable by any service user.
    current = PREFIX / "current"
    if current.exists():
        st = current.resolve().stat()
        if st.st_uid != 0:
            problems.append(f"{current} is not root-owned")
        if st.st_mode & 0o022:
            problems.append(f"{current} is group- or world-writable")
        lines.append(f"    {'ok ' if st.st_uid == 0 and not st.st_mode & 0o022 else 'BAD'} "
                     f"{'code':<13} {current} uid={st.st_uid} mode={oct(st.st_mode & 0o777)}")

    record(name, FAIL if problems else PASS,
           "; ".join(problems) if problems else "privilege separation intact", lines)


# --------------------------------------------------------------------------------------
# credentials present (names and booleans only)
# --------------------------------------------------------------------------------------

def check_credentials() -> None:
    name = "credentials present"
    try:
        from kaiba.core.config import get_settings
    except Exception as exc:
        record(name, SKIP, f"cannot import settings: {exc}")
        return
    try:
        present = get_settings().present()
    except Exception as exc:
        record(name, FAIL, f"settings failed to load: {exc}")
        return
    have = [k for k, v in present.items() if v]
    missing = [k for k, v in present.items() if not v]
    # Not a hard failure: plenty of these are optional, and which ones matter depends on
    # the lanes you are arming. The probe is the check that actually gates.
    record(name, PASS if have else FAIL,
           f"{len(have)}/{len(present)} set",
           [f"    set:     {', '.join(have) or '(none)'}",
            f"    missing: {', '.join(missing) or '(none)'}"])


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------

CHECKS = {
    "migrations": check_migrations,
    "journal": check_journal,
    "risk": check_risk_and_wallets,
    "signer": check_signer,
    "withdrawal": check_signer_refuses_withdrawal,
    "providers": check_providers,
    "hermes": check_hermes,
    "clock": check_clock,
    "layout": check_layout,
    "credentials": check_credentials,
}


def run_preflight(*, json_out: bool = False, quiet: bool = False, skip: str = "") -> int:
    """Run the pre-arm checks and return a process-style status code.

    The script entrypoint and ``kaiba risk preflight`` use this same function.  Clearing
    the module-level result list makes repeated in-process invocations honest, which is
    useful to the CLI and prevents a previous failed run from contaminating a later one.
    ``skip`` has the same comma-separated spelling as the standalone script.
    """

    RESULTS.clear()
    skip_set = {s.strip() for s in skip.split(",") if s.strip()}
    unknown = skip_set - set(CHECKS)
    if unknown:
        print(f"unknown check(s) to skip: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    imported, note = _import_kaiba()
    if not imported:
        print(f"FATAL: {note}", file=sys.stderr)
        print("The kaiba package is not importable. Run deploy/install.sh first.",
              file=sys.stderr)
        return 2

    for key, fn in CHECKS.items():
        if key in skip_set:
            record(key, SKIP, "skipped on the command line")
            continue
        try:
            fn()
        except Exception as exc:  # a broken check must not hide the other nine
            record(key, FAIL, f"check itself raised {type(exc).__name__}: {exc}")

    failures = [r for r in RESULTS if r.failed]

    if json_out:
        print(json.dumps(
            {"ok": not failures,
             "checks": [{"name": r.name, "status": r.status, "detail": r.detail}
                        for r in RESULTS]},
            indent=2))
        return 1 if failures else 0

    colour = {PASS: "\033[32m", FAIL: "\033[31;1m", WARN: "\033[33m", SKIP: "\033[33m"}
    width = max(len(r.name) for r in RESULTS) + 2

    print()
    print(f"\033[1mKAIBA PREFLIGHT\033[0m  {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}")
    print(f"  prefix={PREFIX}  state={STATE}  conf={CONF}")
    print("-" * 78)
    for r in RESULTS:
        if quiet and r.status == PASS:
            continue
        c = colour.get(r.status, "")
        print(f"  {c}{r.status:<5}\033[0m {r.name:<{width}} {r.detail}")
        for line in r.lines:
            print(f"        {line}" if not line.startswith("    ") else line)
    print("-" * 78)

    if failures:
        print(f"\n  \033[31;1m{len(failures)} check(s) not green. DO NOT ARM.\033[0m")
        for r in failures:
            print(f"    - {r.name}: {r.detail}")
        print("\n  Fix these, re-run, and only then read docs/runbooks/arming.md.")
        return 1

    print("\n  \033[32;1mAll checks green.\033[0m")
    print("""
  Green means the machinery is sound. It does not mean the strategy is.
  Arming is still a separate decision with its own evidence bar:
  docs/runbooks/arming.md. Start in canary, at canary size, with one lane.
""")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kaiba pre-arm gate")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    ap.add_argument("--skip", default="",
                    help=f"comma-separated checks to skip ({', '.join(CHECKS)})")
    args = ap.parse_args()
    return run_preflight(json_out=args.json, quiet=args.quiet, skip=args.skip)


if __name__ == "__main__":
    sys.exit(main())
