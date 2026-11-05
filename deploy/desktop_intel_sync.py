"""Collect three public desktop-only feeds and mirror their original cache bytes.

The default invocation is a network-free dry run. --collect-and-sync is intended
for the owner's five-minute desktop task, after the corresponding server feeds
are paused. No trading database, credentials, account data or financial action
is involved. Existing OpenSSH handles its own key; this module never reads it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

KEYS = ("etf_flows", "cot", "markets")
REPO = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO / "data" / "market-intel-desktop"
MIRROR_ROOT = REPO / "data" / "market-intel" / "intel"
REMOTE_ROOT = "/home/ubuntu/kaiba/data/market-intel/intel"
SSH_TARGET = "ubuntu@203.0.113.10"
KEY_PATH = Path.home() / ".ssh" / "kaiba_ultimate_agent_ed25519"
COLLECTION_SECONDS = 120
SYNC_SECONDS = 60
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 3 * ((MAX_FILE_BYTES + 2) // 3 * 4) + 4096
REFUSED_BACKOFF_SECONDS = 6 * 3600
MAX_REQUESTS = 32
_ENV_LOCK = threading.Lock()

# This same stdlib-only implementation runs locally in fixture tests and remotely
# under python3. Content travels on stdin, never through shell interpolation.
REMOTE_PROGRAM = r'''
import base64, hashlib, json, math, os, re, secrets, shutil, stat, sys, time
from datetime import datetime
from pathlib import Path

ROOT = "/home/ubuntu/kaiba/data/market-intel/intel"
KEYS = ("etf_flows", "cot", "markets")
MAX_FILE = 2097152
MAX_INPUT = 8392708
AGES = {"etf_flows": 21600, "cot": 86400, "markets": 600}
DATA_KEYS = {
    "etf_flows": {"attribution", "units", "feeds", "assets", "verdict"},
    "cot": {"note", "feeds", "contracts", "by_coin", "verdict"},
    "markets": {"quotes", "groups", "feeds"},
}
ENVELOPE_KEYS = {"key", "title", "ok", "stale", "error", "fetched_at", "fetched_ts", "duration_s", "interval_s", "sources", "data", "attempted_at", "attempted_ts"}
SENSITIVE = re.compile(r"secret|password|api.?key|private.?key|credential|authorization|bearer|wallet|address|account", re.I)

def finite(value):
    return type(value) in (float, int) and math.isfinite(value)

def inspect_json(value, depth=0):
    if depth > 12:
        raise ValueError("document_depth")
    if isinstance(value, dict):
        if len(value) > 200:
            raise ValueError("document_width")
        for key, item in value.items():
            if not isinstance(key, str) or SENSITIVE.search(key):
                raise ValueError("non_public_field")
            inspect_json(item, depth+1)
    elif isinstance(value, list):
        if len(value) > 500:
            raise ValueError("document_rows")
        for item in value:
            inspect_json(item, depth+1)
    elif isinstance(value, str):
        if len(value) > 8192 or "\x00" in value:
            raise ValueError("document_text")
    elif value is not None and not isinstance(value, bool) and not finite(value):
        raise ValueError("document_number")

def validate_document(key, raw, now):
    if key not in KEYS or not isinstance(raw, bytes) or len(raw) > MAX_FILE:
        raise ValueError("document_limit_or_key")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or set(doc) - ENVELOPE_KEYS:
        raise ValueError("document_envelope")
    inspect_json(doc)
    if doc.get("key") != key or doc.get("ok") is not True or doc.get("stale") is not False or doc.get("error"):
        raise ValueError("document_failed_or_stale")
    ts = doc.get("fetched_ts")
    if not finite(ts) or ts <= 0 or ts > now + 60 or now - ts > AGES[key]:
        raise ValueError("document_clock")
    stamp = datetime.fromisoformat(str(doc.get("fetched_at", "")).replace("Z", "+00:00"))
    if stamp.tzinfo is None or abs(stamp.timestamp() - ts) > 2:
        raise ValueError("document_clock_mismatch")
    data = doc.get("data")
    if not isinstance(data, dict) or set(data) - DATA_KEYS[key]:
        raise ValueError("document_data_schema")
    if key == "markets":
        if not isinstance(data.get("quotes"), list) or not 0 < len(data["quotes"]) <= 50:
            raise ValueError("document_no_quotes")
    elif key == "etf_flows":
        assets = data.get("assets")
        if data.get("units") != "US$m" or not isinstance(assets, dict) or set(assets) - {"BTC", "ETH"}:
            raise ValueError("document_etf_schema")
        if not any(isinstance(a, dict) and a.get("ok") is True and isinstance(a.get("rows"), list) and a["rows"] for a in assets.values()):
            raise ValueError("document_no_etf_rows")
    else:
        contracts = data.get("contracts")
        if not isinstance(contracts, dict) or not any(isinstance(c, dict) and c.get("ok") is True and isinstance(c.get("reports"), list) and c["reports"] for c in contracts.values()):
            raise ValueError("document_no_cot_reports")
    return doc

def verify_root(root, create=False):
    root = Path(root).absolute()
    for path in reversed([root, *root.parents]):
        if not path.exists() and not path.is_symlink():
            if create:
                path.mkdir()
            else:
                raise ValueError("destination_missing")
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_dir():
            raise ValueError("destination_unsafe")
    if root.resolve() != root:
        raise ValueError("destination_escape")
    if hasattr(os, "geteuid") and root.stat().st_uid != os.geteuid():
        raise ValueError("destination_owner")
    return root

def regular_file(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("destination_file_unsafe")
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("destination_file_unsafe")

def apply_payload(payload, *, root=ROOT, now=None, disk_usage=shutil.disk_usage, create=False):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or set(payload) != {"version", "documents"} or payload["version"] != 1:
        raise ValueError("payload_schema")
    docs = payload["documents"]
    if not isinstance(docs, list) or not 0 < len(docs) <= 3:
        raise ValueError("payload_documents")
    staged, seen, total = [], set(), 0
    for row in docs:
        if not isinstance(row, dict) or set(row) != {"key", "sha256", "content_b64"}:
            raise ValueError("payload_document_schema")
        key = row["key"]
        if key not in KEYS or key in seen or not isinstance(row["content_b64"], str) or len(row["content_b64"]) > (MAX_FILE + 2)//3*4:
            raise ValueError("payload_key_or_size")
        seen.add(key)
        raw = base64.b64decode(row["content_b64"], validate=True)
        total += len(raw)
        if total > 3*MAX_FILE or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("payload_hash_or_size")
        doc = validate_document(key, raw, now)
        staged.append((key, raw, doc["fetched_ts"], row["sha256"]))
    root = verify_root(root, create)
    usage = disk_usage(root)
    if 100*usage.used/max(1, usage.used+usage.free) >= 92:
        raise ValueError("destination_disk_halt")
    # Validate every target before replacing any of them.
    for key, _, _, _ in staged:
        target = root / (key + ".json")
        regular_file(target)
        if target.exists() and target.stat().st_size > MAX_FILE:
            raise ValueError("destination_existing_oversize")
    report = {"status": "ok", "written": [], "retained": [], "sha256": {}, "fetched_ts": {}}
    for key, raw, ts, digest in staged:
        path = root / (key + ".json")
        if path.exists():
            if path.stat().st_size > MAX_FILE:
                raise ValueError("destination_existing_oversize")
            try:
                with path.open("rb") as handle:
                    previous = json.loads(handle.read(MAX_FILE+1))
                previous_raw = json.dumps(previous, separators=(",", ":")).encode("utf-8")
                previous_ts = validate_document(key, previous_raw, now)["fetched_ts"]
            except (ValueError, OSError, RecursionError, OverflowError):
                previous_ts = None
            if finite(previous_ts) and previous_ts >= ts:
                report["retained"].append(key)
                continue
        temporary = root / (".desktop-sync-" + secrets.token_hex(12) + ".tmp")
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            regular_file(path)
            if root.resolve() != root:
                raise ValueError("destination_escape")
            os.replace(temporary, path)
            report["written"].append(key)
            report["sha256"][key] = digest
            report["fetched_ts"][key] = ts
        finally:
            temporary.unlink(missing_ok=True)
    return report

def main():
    raw = sys.stdin.buffer.read(MAX_INPUT+1)
    try:
        if len(raw) > MAX_INPUT:
            raise ValueError("payload_size")
        report = apply_payload(json.loads(raw))
    except (ValueError, OSError, RecursionError, OverflowError):
        print(json.dumps({"status": "refused", "reason": "cache_validation_or_write_refused"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''

_RECEIVER: dict = {"__name__": "desktop_intel_receiver"}
exec(compile(REMOTE_PROGRAM, "<desktop-intel-receiver>", "exec"), _RECEIVER)
validate_document = _RECEIVER["validate_document"]
apply_payload = _RECEIVER["apply_payload"]


def _family(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host == "farside.co.uk" or host.endswith(".farside.co.uk"):
        return "farside"
    if host == "publicreporting.cftc.gov":
        return "cftc"
    if host == "finance.yahoo.com" or host.endswith(".finance.yahoo.com"):
        return "yahoo"
    raise RuntimeError("desktop public source refused")


class HostCircuit:
    """One durable refusal circuit per public provider, including partial results."""

    def __init__(self, root: Path, *, deadline: float, clock=time.monotonic, wall=time.time):
        self.path = Path(root) / "desktop_provider_cooldowns.json"
        self.deadline, self.clock, self.wall = deadline, clock, wall
        self.locks = {key: threading.Lock() for key in ("farside", "cftc", "yahoo")}
        self.state_lock = threading.Lock()
        self.until = {}
        self.requests = 0
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 8192:
                raise ValueError("invalid cooldown state")
            state = json.loads(self.path.read_bytes())
            for key in self.locks:
                value = state.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value > 0:
                    self.until[key] = value
                elif key in state:
                    self.until[key] = self.wall() + REFUSED_BACKOFF_SECONDS
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            # Unknown cooldown state cannot grant immediate provider capacity.
            self.until = {key: self.wall() + REFUSED_BACKOFF_SECONDS for key in self.locks}

    def refused(self, family: str, retry_after: str | None) -> None:
        now = self.wall()
        until = now + REFUSED_BACKOFF_SECONDS
        try:
            retry = float(retry_after)
            if math.isfinite(retry) and retry > 0:
                until = max(until, now + retry if math.isfinite(now + retry) else 253402300799.0)
        except (ValueError, TypeError, OverflowError):
            try:
                parsed = parsedate_to_datetime(retry_after or "")
                if parsed.tzinfo is not None:
                    until = max(until, parsed.timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
        with self.state_lock:
            self.until[family] = max(until, self.until.get(family, 0))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise RuntimeError("desktop cooldown state refused")
            temporary = self.path.with_suffix(".tmp")
            if temporary.is_symlink():
                raise RuntimeError("desktop cooldown state refused")
            temporary.write_text(json.dumps(self.until, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)


class GuardedHttp:
    """Wrap the existing transport; serialize hosts and stop on first 403/429."""

    def __init__(self, inner, circuit: HostCircuit):
        self.inner, self.circuit = inner, circuit
        self._c = getattr(inner, "_c", None)

    def _call(self, method, url, *args, **kwargs):
        family = _family(url)
        with self.circuit.locks[family]:
            remaining = self.circuit.deadline - self.circuit.clock()
            if remaining <= 0:
                raise RuntimeError("desktop collector time budget exhausted")
            if self.circuit.until.get(family, 0) > self.circuit.wall():
                raise RuntimeError("429 Too Many Requests: desktop provider cooldown")
            with self.circuit.state_lock:
                if self.circuit.requests >= MAX_REQUESTS:
                    raise RuntimeError("desktop collector request budget exhausted")
                self.circuit.requests += 1
            client = getattr(self.inner, "_c", None)
            if client is not None:
                client.timeout = min(20.0, remaining)
            try:
                return getattr(self.inner, method)(url, *args, **kwargs)
            except Exception as exc:
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) in (403, 429):
                    self.circuit.refused(family, getattr(response, "headers", {}).get("Retry-After"))
                raise

    def get_text(self, url, headers=None):
        return self._call("get_text", url, headers=headers)

    def get_json(self, url, headers=None):
        return self._call("get_json", url, headers=headers)

    def get_bytes(self, url, headers=None):
        return self._call("get_bytes", url, headers=headers)

    def post_json(self, url, body):
        raise RuntimeError("desktop collector POST refused")


@contextmanager
def _collector_environment(root: Path):
    with _ENV_LOCK:
        previous = os.environ.get("AEGIS_TERMINAL_DATA")
        os.environ["AEGIS_TERMINAL_DATA"] = str(root)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("AEGIS_TERMINAL_DATA", None)
            else:
                os.environ["AEGIS_TERMINAL_DATA"] = previous


def collect_local(root: Path = DATA_ROOT, *, runner=None, http_factory=None, disk_usage=None) -> dict:
    from kaiba.market_intel import refresh
    from kaiba.market_intel.aegis import base

    circuit = HostCircuit(root, deadline=time.monotonic() + COLLECTION_SECONDS)
    factory = http_factory or (lambda: base.LiveHttp(timeout=20.0))
    options = {"only": KEYS, "budget_s": float(COLLECTION_SECONDS),
               "http_factory": lambda: GuardedHttp(factory(), circuit)}
    if disk_usage is not None:
        options["disk_usage"] = disk_usage
    with _collector_environment(root):
        report = (runner or refresh.run)(**options)
    return {"status": report.get("status", "unknown"),
            "ran": {key: {"ok": value.get("ok") is True, "stale": value.get("stale") is True}
                    for key, value in report.get("ran", {}).items() if key in KEYS},
            "not_due": [key for key in report.get("not_due", []) if key in KEYS],
            "budget_stopped": [key for key in report.get("budget_stopped", []) if key in KEYS]}


def build_payload(root: Path | None = None, *, now: float | None = None) -> tuple[dict, dict]:
    root = DATA_ROOT if root is None else root
    now = time.time() if now is None else now
    documents, excluded, reasons = [], [], {}
    for key in KEYS:
        path = Path(root) / "intel" / f"{key}.json"
        try:
            if not path.exists():
                raise FileNotFoundError
            if path.is_symlink() or not path.is_file():
                raise ValueError("source_unsafe")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("source_size")
            with path.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            validate_document(key, raw, now)
            documents.append({"key": key, "sha256": hashlib.sha256(raw).hexdigest(),
                              "content_b64": base64.b64encode(raw).decode("ascii")})
        except (OSError, ValueError, RecursionError, OverflowError) as exc:
            excluded.append(key)
            message = str(exc)
            reasons[key] = "source_missing" if isinstance(exc, FileNotFoundError) else message if re.fullmatch(r"(?:source|document)_[a-z_]+", message) else "source_invalid"
    payload = {"version": 1, "documents": documents}
    cooldowns = {}
    try:
        path = Path(root) / "desktop_provider_cooldowns.json"
        if not path.is_symlink() and path.stat().st_size <= 8192:
            state = json.loads(path.read_bytes())
            if isinstance(state, dict):
                for key, family in {"etf_flows": "farside", "cot": "cftc", "markets": "yahoo"}.items():
                    until = state.get(family)
                    if type(until) in (int, float) and math.isfinite(until) and until > now:
                        cooldowns[key] = {"remaining_s": math.ceil(until - now), "until_ms": int(until * 1000)}
    except (OSError, ValueError, OverflowError):
        pass
    return payload, {"eligible": [doc["key"] for doc in documents], "excluded": excluded,
                     "excluded_reasons": reasons, "cooldowns": cooldowns}


def _safe_process_env() -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOME",
            "LOCALAPPDATA", "APPDATA", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"}
    return {key: value for key, value in os.environ.items() if key.upper() in keep}


def run_collection_worker() -> dict:
    try:
        proc = subprocess.run([sys.executable, "-m", "deploy.desktop_intel_sync", "--_collect-worker"],
                              cwd=REPO, env=_safe_process_env(), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=COLLECTION_SECONDS,
                              check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = json.loads(proc.stdout)
        return value if proc.returncode == 0 and isinstance(value, dict) else {"status": "failed"}
    except subprocess.TimeoutExpired:
        return {"status": "timed_out"}
    except (OSError, ValueError):
        return {"status": "failed"}


def sync_payload(payload: dict) -> dict:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        return {"status": "refused", "reason": "payload_size"}
    command = "/usr/bin/nice -n 19 /usr/bin/ionice -c3 /usr/bin/timeout 55s python3 -I -c " + shlex.quote(REMOTE_PROGRAM)
    args = ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", "-i", str(KEY_PATH), SSH_TARGET, command]
    try:
        proc = subprocess.run(args, input=raw, capture_output=True,
                              timeout=SYNC_SECONDS, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        value = json.loads(proc.stdout)
        if proc.returncode != 0 or not isinstance(value, dict) or value.get("status") != "ok":
            return {"status": "failed", "reason": "remote_refused_or_unreachable"}
        return {"status": "ok", "written": [key for key in value.get("written", []) if key in KEYS],
                "retained": [key for key in value.get("retained", []) if key in KEYS],
                "sha256": {key: value.get("sha256", {}).get(key) for key in KEYS if key in value.get("written", [])}}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "ssh_timeout"}
    except (OSError, ValueError):
        return {"status": "failed", "reason": "remote_refused_or_unreachable"}


@contextmanager
def _single_run(root: Path):
    root = _RECEIVER["verify_root"](root, True)
    path = root / "desktop_sync.lock"
    _RECEIVER["regular_file"](path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _read_state(root: Path) -> dict:
    path = root / "desktop_sync_state.json"
    try:
        if path.is_symlink() or path.stat().st_size > 32_768:
            return {}
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _record(root: Path, report: dict, hashes: dict) -> None:
    path = root / "desktop_sync_state.json"
    _RECEIVER["regular_file"](path)
    temporary = root / ".desktop_sync_state.tmp"
    _RECEIVER["regular_file"](temporary)
    value = {"version": 1, "observed_at_ms": int(time.time() * 1000),
             "successful_hashes": hashes, "last_run": report}
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(raw) > 32_768:
        raise ValueError("receipt size")
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run_active() -> dict:
    state = _read_state(DATA_ROOT)
    previous = state.get("successful_hashes", {})
    previous = previous if isinstance(previous, dict) else {}
    hashes = {key: value for key, value in previous.items()
              if key in KEYS and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)}
    collection = run_collection_worker()
    payload, sources = build_payload()
    report = {"status": "ok", "collection": collection,
              "sources": sources, "provider_scope": list(KEYS), "local_mirror": {"status": "not_run"},
              "sync": {"status": "not_run"}}
    if not payload["documents"]:
        report["status"] = "partial"
        _record(DATA_ROOT, report, hashes)
        return report
    try:
        mirrored = apply_payload(payload, root=MIRROR_ROOT, create=True)
        report["local_mirror"] = {"status": mirrored["status"], "written": mirrored["written"], "retained": mirrored["retained"]}
    except (OSError, ValueError, RecursionError, OverflowError):
        report["local_mirror"] = {"status": "refused"}
    changed = [row for row in payload["documents"] if hashes.get(row["key"]) != row["sha256"]]
    if changed:
        report["sync"] = sync_payload({"version": 1, "documents": changed})
        if report["sync"]["status"] == "ok":
            accepted = set(report["sync"].get("written", [])) | set(report["sync"].get("retained", []))
            hashes.update({row["key"]: row["sha256"] for row in changed if row["key"] in accepted})
    else:
        report["sync"] = {"status": "unchanged", "written": [], "retained": sources["eligible"]}
    if report["sync"]["status"] not in ("ok", "unchanged") or report["local_mirror"]["status"] != "ok":
        report["status"] = "partial"
    if collection.get("status") not in ("ok", "skipped") or sources["excluded"]:
        report["status"] = "partial"
    _record(DATA_ROOT, report, hashes)
    return report


def run(*, collect_and_sync: bool = False) -> dict:
    if not collect_and_sync:
        _, sources = build_payload()
        return {"status": "dry_run", "collection": {"status": "not_run"}, "sources": sources,
                "provider_scope": list(KEYS), "local_mirror": {"status": "not_run"}, "sync": {"status": "not_run"}}
    with _single_run(DATA_ROOT):
        return _run_active()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-and-sync", action="store_true", help="Collect only three due public feeds, mirror and sync")
    parser.add_argument("--_collect-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        report = collect_local() if args._collect_worker else run(collect_and_sync=args.collect_and_sync)
    except (OSError, ValueError, RuntimeError, RecursionError, OverflowError):
        report = {"status": "refused", "reason": "desktop_cache_operation_refused"}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report.get("status") in ("ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
