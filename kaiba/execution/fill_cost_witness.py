"""Private evidence from an already-returned RPC call, never another lookup.

The encoding reuses reviewed paid-response-witness/1 (87eee0c8). It preserves
parsed JSON/Decimal values, not HTTP wire bytes, and records selected orders
without asserting their wallet, attribution, completeness or profitability.

The default caller only encodes bounded bytes and enqueues without waiting for
disk. One process-global daemon performs serial storage. A hung filesystem
syscall can hang that daemon indefinitely; cooperative deadlines cannot interrupt
it. Pending/queued observations never become retained without a durable ack and
can be lost on process exit. There is no automatic replay. Expiry is enforced on
admission/read and pruned on the next successful store; idle/crashed files are
not promised physical deletion at a timer deadline.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

VERSION = "paid-response-witness/2"
STORE_VERSION = "fill-cost-witness-store/1"
CAP = 2 * 1024 * 1024
INDEX_CAP = 256 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_RECORDS = 256
MAX_ORDERS = 256
RETENTION_MS = 13 * 86_400_000
MAX_NODES = 50_000
PRIVATE_KEYS = frozenset(("url", "rpc_url", "authorization", "api_key", "private_key", "headers",
                          "env", "environ", "cookie", "password", "secret", "access_token"))
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ORDER_FIELDS = ("order_id", "chain", "side", "lane", "token", "tx_hash", "amount_in", "min_out",
                 "slippage_bps", "filled_out", "updated_ms")
_CODES = frozenset(("store_busy", "store_unsafe", "store_corrupt", "store_capacity", "store_deadline",
                    "store_partial", "store_io", "queue_full", "worker_unavailable", "different_store_root"))


def _now():
    return int(time.time() * 1000)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _require(value, code):
    if not value:
        raise ValueError(code)


def encoded(value, *, cap=CAP):
    result = bytearray()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode(value):
        body = part.encode()
        _require(len(result) + len(body) <= cap, "encoded_byte_cap")
        result.extend(body)
    return bytes(result)


def typed(value, *, budget=None):
    """Original typed-json/1: exact integers and explicit Decimal paths."""
    paths = []
    budget = {"nodes": 0, "bytes": 0} if budget is None else budget

    def charge(count):
        budget["bytes"] += count
        _require(budget["bytes"] <= CAP, "bounded_encoded_bytes")

    def text_bytes(text):
        return len(json.dumps(text, ensure_ascii=True).encode())

    charge(128)

    def walk(item, path, depth, path_bytes):
        budget["nodes"] += 1
        _require(depth <= 32 and budget["nodes"] <= MAX_NODES, "bounded_structure")
        if item is None or type(item) is bool:
            charge(4 if item is None or item is True else 5)
            return item
        if type(item) is int:
            _require(item.bit_length() <= 4096, "bounded_integer")
            charge(len(str(item)))
            return item
        if isinstance(item, Decimal):
            _require(item.is_finite() and item.__sizeof__() <= 4096, "finite_bounded_decimal")
            text = str(item)
            _require(len(text) <= 1000, "finite_decimal")
            charge(text_bytes(text))
            charge(path_bytes + int(bool(paths)))
            paths.append(path)
            return text
        if isinstance(item, str):
            _require(len(item) <= 65536 and not any(t in item.lower() for t in ("http://", "https://", "bearer ")),
                     "private_or_large_text")
            charge(text_bytes(item))
            return item
        if isinstance(item, (list, dict)):
            _require(len(item) <= MAX_NODES - budget["nodes"], "bounded_structure")
        if isinstance(item, list):
            charge(2 + max(0, len(item) - 1))
            return [walk(child, [*path, index], depth + 1,
                         path_bytes + int(bool(path)) + len(str(index))) for index, child in enumerate(item)]
        if isinstance(item, dict):
            charge(2 + max(0, len(item) - 1))
            result = {}
            for key, child in item.items():
                _require(isinstance(key, str) and key.lower() not in PRIVATE_KEYS, "private_or_invalid_key")
                walk(key, [], depth + 1, 2)
                charge(1)
                result[key] = walk(child, [*path, key], depth + 1,
                                   path_bytes + int(bool(path)) + text_bytes(key))
            return result
        raise ValueError("unsupported_type")

    return {"format": "typed-json/1", "value": walk(value, [], 0, 2), "decimal_paths": paths}


def restore(value):
    """Restore a locally validated typed envelope; not an untrusted-file parser."""
    result = json.loads(json.dumps(value["value"]))
    for path in value["decimal_paths"]:
        if not path:
            result = Decimal(result)
            continue
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = Decimal(parent[path[-1]])
    return result


def _integer(value, *, maximum=2**63 - 1):
    _require(type(value) is int and 0 <= value <= maximum, "invalid_integer")
    return value


def _quantity(value):
    # SQLite intentionally stores base-unit quantities as TEXT. Preserve that
    # original type/value while validating its exact, bounded integer meaning.
    if type(value) is str:
        _require(re.fullmatch(r"0|[1-9][0-9]{0,77}", value) is not None, "selected_order_quantity")
        _integer(int(value), maximum=2**256 - 1)
    else:
        _integer(value, maximum=2**256 - 1)


def _orders(rows, now):
    _require(type(rows) in (list, tuple) and 0 < len(rows) <= MAX_ORDERS, "selected_order_count")
    result, seen = [], set()
    for row in rows:
        _require(isinstance(row, dict) and all(key in row for key in _ORDER_FIELDS), "selected_order_fields")
        item = {key: row[key] for key in _ORDER_FIELDS}
        for key in ("order_id", "chain", "side", "lane", "token", "tx_hash"):
            _require(isinstance(item[key], str) and 0 < len(item[key]) <= 256, "selected_order_text")
        _require(len(item["chain"]) <= 32 and item["side"] in ("buy", "sell"), "selected_order_identity")
        _require(item["order_id"] not in seen, "duplicate_selected_order")
        seen.add(item["order_id"])
        _quantity(item["amount_in"])
        _quantity(item["min_out"])
        _integer(item["slippage_bps"], maximum=10000)
        if item["filled_out"] is not None:
            _quantity(item["filled_out"])
        _require(0 < _integer(item["updated_ms"]) <= now, "selected_order_clock")
        result.append(item)
    # This also rejects URL/credential-shaped values, without logging them.
    typed(result)
    return sorted(result, key=lambda item: item["order_id"])


def _status():
    return {"status": "unavailable", "reason": "witness_retention_unavailable", "coverage": "UNKNOWN",
            "economic_complete": False, "profitability_proven": False, "wallet_attribution_proven": False,
            "additional_provider_calls": 0}


class WitnessRecorder:
    """Freeze the actual selected-order rows and encode before a sink can queue."""

    def __init__(self, sink, *, selected_orders, clock=_now):
        self.sink, self.clock, self.statuses, self.calls = sink, clock, [], 0
        self._orders = _orders(selected_orders, _integer(clock()))

    def __call__(self, *, chain, signature, request, fetched):
        status = _status()
        try:
            captured = _integer(self.clock())
            expected = "getTransaction" if chain == "sol" else "eth_getTransactionReceipt"
            _require(isinstance(chain, str) and 0 < len(chain) <= 32 and isinstance(signature, str)
                     and 0 < len(signature) <= 256 and isinstance(request, dict)
                     and request.get("method") == expected and isinstance(request.get("params"), list)
                     and request["params"] and request["params"][0] == signature, "request_identity")
            contexts = [dict(row) for row in self._orders if row["chain"] == chain and row["tx_hash"] == signature]
            _require(contexts and all(row["updated_ms"] <= captured for row in contexts), "selected_order_linkage")
            receipt = fetched.receipt.model_dump(mode="json")
            observed = receipt.get("observed_at_ms")
            _require(type(observed) is int and 0 < observed <= captured < 2**63, "original_clock")
            budget = {"nodes": 0, "bytes": 4096}
            request_body = typed(request, budget=budget)
            identity = _sha(encoded({"chain": chain, "signature": signature, "method": expected, "body": request_body}))
            witness = {"version": VERSION, "identity_sha256": identity, "chain": chain, "signature": signature,
                "request": request_body, "parsed_response": typed(fetched.data, budget=budget),
                "original_receipt": typed(receipt, budget=budget), "captured_ms": captured,
                "provider_observed_ms": observed, "selected_orders": contexts,
                "selected_orders_sha256": _sha(encoded(contexts)), "transport_wire_bytes_available": False,
                "provider_request_digest_binds_json_body": False,
                "coverage": "UNKNOWN", "economic_complete": False, "profitability_proven": False,
                "historical_trading_wallet": None, "native_unit": None,
                "transaction_allocation_proven": False,
                "unknowns": ["custody", "historical_wallet", "failed_attempt_coverage", "venue", "slippage",
                             "inventory", "full_cashflow", "chain_finality", "order_allocation"]}
            raw = encoded(witness)
            checksum = _sha(raw)
            ack = self.sink(identity, checksum, raw)
            if ack == checksum:
                result = {"status": "retained", "reason": "none"}
            else:
                _require(isinstance(ack, dict) and ack.get("status") in ("queued", "unavailable"), "sink_ack_unconfirmed")
                result = {"status": ack["status"], "reason": ack.get("reason", "witness_retention_unavailable")}
                _require(result["reason"] in _CODES or result["reason"] == "none", "invalid_sink_status")
            status.update(result, witness_sha256=checksum, identity_sha256=identity, bytes=len(raw),
                          provider_observed_ms=observed, captured_ms=captured,
                          selected_orders_sha256=witness["selected_orders_sha256"], selected_order_count=len(contexts))
        except Exception:
            pass
        self.calls += 1
        self.statuses.append(status)
        del self.statuses[:-32]
        return dict(status)

    def summary(self):
        statuses = [dict(value) for value in self.statuses]
        lookup = getattr(self.sink, "lookup", None)
        if lookup is not None:
            for value in statuses:
                if value["status"] == "queued":
                    update = lookup(value["witness_sha256"])
                    if update is not None:
                        value.update(update)
        return {"version": VERSION, "calls": self.calls, "statuses_scope": "last_32_calls",
                "omitted_statuses": max(0, self.calls - len(statuses)), "statuses": statuses,
                "status_counts": {key: sum(value["status"] == key for value in statuses)
                                  for key in ("queued", "pending", "retained", "unavailable")},
                "disk_wait_on_fetch_path": False, "worker_syscall_duration_bounded": False,
                "additional_provider_calls": 0, "coverage": "UNKNOWN", "economic_complete": False}


def _identity(info):
    # POSIX ctime protects metadata-preserving content mutations. Windows can
    # expose a stale path ctime immediately after rename; it is not the target
    # deployment's ctime contract. Full SHA readback is required on both hosts.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, (info.st_ctime_ns if os.name == "posix" else 0)


def _plain(info, *, directory=False):
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and not getattr(info, "st_file_attributes", 0) & 1024, "store_unsafe")
    if not directory:
        _require(info.st_nlink == 1, "store_unsafe")
    if os.name == "posix":
        _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600),
                 "store_unsafe")


def _read(path, cap):
    before = path.lstat()
    _plain(before)
    _require(before.st_size <= cap, "store_corrupt")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        _require(_identity(before) == _identity(opened), "store_unsafe")
        raw = os.read(fd, cap + 1)
        _require(len(raw) == opened.st_size and _identity(os.fstat(fd)) == _identity(opened)
                 and _identity(path.lstat()) == _identity(opened), "store_corrupt")
        return raw, _identity(opened)
    finally:
        os.close(fd)


def _sync(directory):
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, "store_corrupt")
        value[key] = item
    return value


class PrivateWitnessStore:
    """Synchronous private store; default callers use its single daemon sink.

    Index publication is atomic and is the only retention commitment. Unknown
    files, interrupted writes and stale locks refuse further work; no recovery,
    lock reclamation or provider retry is performed. The I/O budget is checked
    between bounded operations and is NOT a syscall timeout.
    """

    def __init__(self, path, *, clock=_now, io_budget_s=2.0):
        self.path = Path(os.path.abspath(os.fspath(path)))
        _require(type(io_budget_s) in (int, float) and 0 < io_budget_s <= 10, "invalid_io_budget")
        self.clock, self.io_budget_s = clock, io_budget_s
        self._active = threading.Lock()

    def _check(self):
        _require(time.monotonic() <= self._deadline, "store_deadline")
        if hasattr(self, "_root_identity"):
            info = self.path.lstat()
            _plain(info, directory=True)
            _require((info.st_dev, info.st_ino) == self._root_identity, "store_unsafe")

    def _directory(self, *, create=True):
        # Reject linked components, including the existing parent; never chmod
        # someone else's directory to turn it into our store.
        for component in reversed((self.path, *self.path.parents)):
            try:
                info = component.lstat()
            except FileNotFoundError:
                _require(create and component == self.path, "store_unsafe")
                component.mkdir(mode=0o700)
                _sync(component.parent)
                info = component.lstat()
            _require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)
                     and not getattr(info, "st_file_attributes", 0) & 1024, "store_unsafe")
        info = self.path.lstat()
        _plain(info, directory=True)
        self._root_identity = info.st_dev, info.st_ino

    def _owns(self):
        try:
            info = self.path.lstat()
            _plain(info, directory=True)
            if (info.st_dev, info.st_ino) != self._root_identity:
                return False
            raw, identity = _read(self.path / "store.lock", 1024)
            return (identity == self._lock_identity and raw == self._lock_raw
                    and _identity(os.fstat(self._lock_fd)) == self._lock_identity)
        except (OSError, ValueError):
            return False

    def _write(self, path, raw):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            offset = 0
            while offset < len(raw):
                self._check()
                written = os.write(fd, raw[offset:])
                _require(written > 0, "store_io")
                offset += written
            os.fsync(fd)
            info = os.fstat(fd)
            _plain(info)
            identity = _identity(info)
        finally:
            os.close(fd)
        result, checked = _read(path, len(raw))
        _require(result == raw and checked == identity, "store_corrupt")
        return identity

    def _commit(self, index, prior):
        self._check()
        raw = encoded(index, cap=INDEX_CAP)
        pending = self.path / ".index.pending"
        identity = self._write(pending, raw)
        self._check()
        current = self.path / "index.json"
        staged, info = _read(pending, INDEX_CAP)
        _require(staged == raw and info == identity, "store_unsafe")
        _require(self._owns(), "store_unsafe")
        # The prior-index CAS follows the staged read and ownership I/O. A
        # replacement observed at either earlier boundary is never overwritten.
        if prior is None:
            _require(not os.path.lexists(current), "store_unsafe")
        else:
            found, info = _read(current, INDEX_CAP)
            _require((found, info) == prior, "store_unsafe")
        _require(_identity(pending.lstat()) == identity, "store_unsafe")
        os.replace(pending, current)
        _sync(self.path)
        _require(self._owns(), "store_corrupt")
        confirmed, _ = _read(current, INDEX_CAP)
        _require(confirmed == raw, "store_corrupt")

    def _inventory(self, now):
        paths = {}
        with os.scandir(self.path) as entries:
            for entry in entries:
                self._check()
                _require(len(paths) < MAX_RECORDS + 2, "store_capacity")
                # Windows DirEntry.stat omits real link/inode fields; obtain
                # the same full lstat used by the pinned-file checks instead.
                info = (self.path / entry.name).lstat()
                _plain(info)
                _require(entry.name in ("store.lock", "index.json") or re.fullmatch(r"[0-9a-f]{64}\.json", entry.name),
                         "store_partial")
                paths[entry.name] = info.st_size
        _require(sum(paths.values()) <= MAX_TOTAL, "store_capacity")
        prior = None
        if "index.json" in paths:
            prior = _read(self.path / "index.json", INDEX_CAP)
            index = json.loads(prior[0], object_pairs_hook=_unique)
            _require(type(index) is dict and set(index) == {"version", "updated_ms", "records"}
                     and index["version"] == STORE_VERSION and type(index["records"]) is list
                     and len(index["records"]) <= MAX_RECORDS and 0 < _integer(index["updated_ms"]) <= now,
                     "store_corrupt")
        else:
            index = {"version": STORE_VERSION, "updated_ms": now, "records": []}
        names = {"store.lock", "index.json"} & paths.keys()
        hashes = set()
        for item in index["records"]:
            self._check()
            _require(type(item) is dict and set(item) == {"identity_sha256", "witness_sha256", "bytes", "captured_ms", "file_identity"},
                     "store_corrupt")
            _require(all(type(item[key]) is str and _HASH.fullmatch(item[key]) for key in ("identity_sha256", "witness_sha256")),
                     "store_corrupt")
            name = item["witness_sha256"] + ".json"
            _require(name not in hashes and name in paths and 0 < _integer(item["bytes"], maximum=CAP)
                     and 0 < _integer(item["captured_ms"]) <= now, "store_corrupt")
            recorded = item["file_identity"]
            _require(type(recorded) is list and len(recorded) == 5 and all(type(part) is int and part >= 0 for part in recorded),
                     "store_corrupt")
            current = (self.path / name).lstat()
            _plain(current)
            # Prior admission uses exact recorded file metadata, not repeated
            # historical payload scans. Consumers MUST rehash with read_record.
            _require(list(_identity(current)) == recorded and current.st_size == item["bytes"], "store_corrupt")
            hashes.add(name)
        _require(names | hashes == paths.keys(), "store_partial")
        return index, prior

    def __call__(self, identity, checksum, raw):
        _require(self._active.acquire(blocking=False), "store_busy")
        try:
            return self._store(identity, checksum, raw)
        finally:
            self._active.release()

    def _store(self, identity, checksum, raw):
        self._deadline = time.monotonic() + self.io_budget_s
        _require(type(identity) is str and _HASH.fullmatch(identity) and type(checksum) is str
                 and _HASH.fullmatch(checksum) and type(raw) is bytes and 0 < len(raw) <= CAP
                 and _sha(raw) == checksum, "store_corrupt")
        value = json.loads(raw, object_pairs_hook=_unique)
        # Re-apply the shared structural/privacy bound before interpreting input.
        typed(value)
        now = _integer(self.clock())
        _require(value.get("version") == VERSION and value.get("identity_sha256") == identity
                 and 0 < _integer(value.get("provider_observed_ms")) <= _integer(value.get("captured_ms")) <= now
                 and now - value["captured_ms"] <= RETENTION_MS, "store_corrupt")
        _require(value.get("coverage") == "UNKNOWN" and value.get("historical_trading_wallet") is None
                 and value.get("native_unit") is None and all(value.get(key) is False for key in
                 ("economic_complete", "profitability_proven", "transaction_allocation_proven",
                  "transport_wire_bytes_available", "provider_request_digest_binds_json_body")), "store_corrupt")
        contexts = _orders(value.get("selected_orders"), value["captured_ms"])
        _require(contexts == value["selected_orders"] and _sha(encoded(contexts)) == value.get("selected_orders_sha256")
                 and all(item["chain"] == value.get("chain") and item["tx_hash"] == value.get("signature")
                         for item in contexts), "store_corrupt")
        request = value.get("request")
        _require(type(request) is dict and request.get("format") == "typed-json/1", "store_corrupt")
        expected = "getTransaction" if value.get("chain") == "sol" else "eth_getTransactionReceipt"
        original_request = restore(request)
        receipt = restore(value.get("original_receipt"))
        _require(type(original_request) is dict and original_request.get("method") == expected
                 and type(original_request.get("params")) is list and original_request["params"]
                 and original_request["params"][0] == value["signature"] and type(receipt) is dict
                 and receipt.get("observed_at_ms") == value["provider_observed_ms"], "store_corrupt")
        _require(_sha(encoded({"chain": value["chain"], "signature": value["signature"], "method": expected,
                               "body": request})) == identity, "store_corrupt")
        self._check()
        self._directory()
        self._check()
        self._lock_raw = encoded({"version": STORE_VERSION, "pid": os.getpid(), "nonce": secrets.token_hex(16)})
        try:
            self._lock_identity = self._write(self.path / "store.lock", self._lock_raw)
        except FileExistsError as exc:
            raise ValueError("store_busy") from exc
        self._lock_fd = os.open(self.path / "store.lock", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _require(self._owns(), "store_unsafe")
            index, prior = self._inventory(now)
            expired = [item for item in index["records"] if now - item["captured_ms"] > RETENTION_MS]
            if expired:
                index["records"] = [item for item in index["records"] if item not in expired]
                index["updated_ms"] = now
                self._commit(index, prior)
                for item in expired:
                    self._check()
                    path = self.path / (item["witness_sha256"] + ".json")
                    _require(self._owns(), "store_corrupt")
                    original, info = _read(path, CAP)
                    _require(_sha(original) == item["witness_sha256"] and list(info) == item["file_identity"],
                             "store_corrupt")
                    path.unlink()
                _sync(self.path)
                prior = _read(self.path / "index.json", INDEX_CAP)
            existing = next((item for item in index["records"] if item["witness_sha256"] == checksum), None)
            if existing is not None:
                _require(existing["identity_sha256"] == identity and self._owns(), "store_corrupt")
                original, _ = _read(self.path / (checksum + ".json"), CAP)
                _require(original == raw and _sha(original) == checksum, "store_corrupt")
                return checksum
            _require(len(index["records"]) < MAX_RECORDS and sum(item["bytes"] for item in index["records"])
                     + len(raw) + 2 * INDEX_CAP + 1024 <= MAX_TOTAL, "store_capacity")
            self._check()
            file_identity = self._write(self.path / (checksum + ".json"), raw)
            _sync(self.path)
            index["records"].append({"identity_sha256": identity, "witness_sha256": checksum,
                                     "bytes": len(raw), "captured_ms": value["captured_ms"],
                                     "file_identity": list(file_identity)})
            index["updated_ms"] = now
            self._commit(index, prior)
            _require(self._owns(), "store_unsafe")
            # Acknowledgement requires one final full body rehash and exact
            # index readback after the last ownership check.
            confirmed, info = _read(self.path / (checksum + ".json"), CAP)
            _require(confirmed == raw and info == file_identity, "store_corrupt")
            committed, _ = _read(self.path / "index.json", INDEX_CAP)
            _require(committed == encoded(index, cap=INDEX_CAP), "store_corrupt")
            return checksum
        finally:
            owned = self._owns()
            # POSIX keeps the inode pinned through unlink. Windows requires the
            # handle closed first; recheck the identity before cleanup there.
            if os.name == "posix":
                try:
                    if owned:
                        (self.path / "store.lock").unlink()
                        _sync(self.path)
                finally:
                    os.close(self._lock_fd)
            else:
                os.close(self._lock_fd)
                if owned:
                    lock = self.path / "store.lock"
                    raw_lock, info = _read(lock, 1024)
                    if info == self._lock_identity and raw_lock == self._lock_raw:
                        lock.unlink()

    def read_record(self, checksum):
        """Authenticate one indexed, unexpired body; no lock or clock refresh.

        This is synchronous filesystem I/O for an explicit consumer, not used by
        the original fetch path. A concurrent publication/prune is refused.
        """
        _require(type(checksum) is str and _HASH.fullmatch(checksum), "store_corrupt")
        self._directory(create=False)
        before = _read(self.path / "index.json", INDEX_CAP)
        index = json.loads(before[0], object_pairs_hook=_unique)
        _require(type(index) is dict and index.get("version") == STORE_VERSION
                 and type(index.get("records")) is list and len(index["records"]) <= MAX_RECORDS, "store_corrupt")
        now = _integer(self.clock())
        matches = [item for item in index["records"] if type(item) is dict and item.get("witness_sha256") == checksum]
        _require(len(matches) == 1, "store_corrupt")
        item = matches[0]
        _require(0 < _integer(item.get("captured_ms")) <= now
                 and now - item["captured_ms"] <= RETENTION_MS, "store_corrupt")
        raw, identity = _read(self.path / (checksum + ".json"), CAP)
        _require(_sha(raw) == checksum and len(raw) == item.get("bytes")
                 and list(identity) == item.get("file_identity") and _read(self.path / "index.json", INDEX_CAP) == before,
                 "store_corrupt")
        return raw


class _QueuedSink:
    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue(maxsize=1)
        self._results = deque(maxlen=32)
        self._mutex = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="fill-cost-witness", daemon=True)
        self._worker.start()

    def _record(self, checksum, status, reason):
        with self._mutex:
            self._results.append((checksum, {"status": status, "reason": reason}))

    def lookup(self, checksum):
        with self._mutex:
            return next((dict(value) for key, value in reversed(self._results) if key == checksum), None)

    def __call__(self, identity, checksum, raw):
        _require(type(raw) is bytes and len(raw) <= CAP, "bounded_queue_body")
        try:
            self.queue.put_nowait((identity, checksum, raw))
        except queue.Full:
            return {"status": "unavailable", "reason": "queue_full"}
        return {"status": "queued", "reason": "none"}

    def _run(self):
        while True:
            identity, checksum, raw = self.queue.get()
            self._record(checksum, "pending", "none")
            try:
                _require(self.store(identity, checksum, raw) == checksum, "store_io")
                self._record(checksum, "retained", "none")
            except Exception as exc:
                code = (exc.args[0] if type(exc) is ValueError and len(exc.args) == 1
                        and type(exc.args[0]) is str and exc.args[0] in _CODES else "store_io")
                self._record(checksum, "unavailable", code)
            finally:
                self.queue.task_done()


_DEFAULT_MUTEX = threading.Lock()
_DEFAULT_ROOT = None
_DEFAULT_SINK = None


def default_recorder(path, selected_orders):
    """Memory-only construction after the first daemon; never wait for storage.

    Exactly one configured root/worker is permitted for the process. Changing
    roots refuses capture; it cannot silently create a second allowance/store.
    """
    global _DEFAULT_ROOT, _DEFAULT_SINK
    root = os.path.abspath(os.fspath(path))
    with _DEFAULT_MUTEX:
        if _DEFAULT_SINK is None:
            _DEFAULT_ROOT = root
            _DEFAULT_SINK = _QueuedSink(PrivateWitnessStore(root))
        sink = _DEFAULT_SINK if _DEFAULT_ROOT == root else lambda *_: {"status": "unavailable", "reason": "different_store_root"}
    return WitnessRecorder(sink, selected_orders=selected_orders)
