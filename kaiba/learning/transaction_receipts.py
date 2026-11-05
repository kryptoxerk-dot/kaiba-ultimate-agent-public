"""One admitted Solana receipt lookup, retained for research without profit certification.

The store is deliberately finite: at most ten logical request slots per UTC day,
32 lifetime slots/retained attempts and 16 MiB. Legacy lookups consume one slot;
explicit configured lookups reserve two before either leg. These are not provider
invoice weights, which remain with the shared limiter. An admitted request is never automatically sent
again, even after process loss. No order, position or financial configuration is
written. Provider bookkeeping remains with the existing shared HTTP limiter.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from kaiba.core.schemas import Chain, digest
from kaiba.execution.fill_costs import _sol_id, parse_solana_costs
from kaiba.execution.fills import MAX_TX_VERSION
from kaiba.providers._http import Fetched, post_json

VERSION = "transaction-receipts/1"
MAX_RECEIPTS = 32
DAILY_ADMISSIONS = 10
LIFETIME_REQUEST_SLOTS = 32
CONFIGURED_POLICY = "configured-solana-mainnet/1"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_STORE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_PACKET_BYTES = 256 * 1024
DAY_MS = 86_400_000
_HEX = re.compile(r"[0-9a-f]{64}")
_RECEIPT = re.compile(r"receipt-([0-9a-f]{64})\.json")


class ReceiptRefused(ValueError):
    """A fixed, non-sensitive reason code; never interpolate provider/input text."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ReceiptRefused(code)


def _clock(value) -> bool:
    return type(value) is int and value > 0


def _encode(value, cap: int) -> bytes:
    chunks, size = [], 0
    try:
        def numeric(item):
            if isinstance(item, Decimal) and item.is_finite():
                return str(item)  # Same normalization as the original Receipt.response_digest.
            raise TypeError("unsupported_json_value")
        for chunk in json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False,
                                      default=numeric).iterencode(value):
            raw = chunk.encode()
            size += len(raw)
            _require(size <= cap, "serialized_size_budget")
            chunks.append(raw)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, ReceiptRefused):
            raise
        raise ReceiptRefused("invalid_serializable_evidence") from None
    return b"".join(chunks)


def _hash(value) -> str:
    return hashlib.sha256(_encode(value, MAX_FILE_BYTES)).hexdigest()


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _paths(directory: Path | str, learning_root: Path | str | None) -> Path:
    if learning_root is None:
        from kaiba.core.config import get_settings
        learning_root = get_settings().kaiba_data_dir / "learning"
    root, directory = Path(learning_root).absolute(), Path(directory).absolute()
    for candidate in (root, directory):
        _require(not any(_linked(p) for p in (candidate, *candidate.parents)), "store_symlink")
    root, directory = root.resolve(), directory.resolve()
    _require(directory != root and directory.is_relative_to(root), "store_outside_learning_root")
    return directory


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _read(path: Path, cap: int):
    _require(not _linked(path), "store_symlink")
    _require(path.is_file() and path.stat().st_size <= cap, "stored_file_budget_or_type")
    with path.open("rb") as stream:
        raw = stream.read(cap + 1)
    _require(len(raw) <= cap, "stored_file_budget_or_type")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ReceiptRefused("nonfinite_json")))
    except (ValueError, RecursionError):
        raise ReceiptRefused("invalid_stored_json") from None
    return value, hashlib.sha256(raw).hexdigest()


def _write_new(path: Path, raw: bytes) -> None:
    _require(not _linked(path), "store_symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _save_manifest(directory: Path, state: dict) -> None:
    destination, temporary = directory / "manifest.json", directory / "manifest.partial"
    _require(not _linked(destination) and not _linked(temporary), "store_symlink")
    _write_new(temporary, _encode(state, MAX_MANIFEST_BYTES))
    os.replace(temporary, destination)
    _sync_directory(directory)


def _load_manifest(directory: Path) -> dict:
    path = directory / "manifest.json"
    if not path.exists():
        _require(not any(_RECEIPT.fullmatch(p.name) for p in directory.iterdir()), "receipt_without_admission_manifest")
        return {"version": VERSION, "attempts": {}, "last_admitted_ms": 0, "retry_after_ms": 0}
    state, _ = _read(path, MAX_MANIFEST_BYTES)
    _require(isinstance(state, dict) and state.get("version") == VERSION
             and isinstance(state.get("attempts"), dict) and len(state["attempts"]) <= MAX_RECEIPTS,
             "manifest_shape")
    _require(type(state.get("last_admitted_ms")) is int and state["last_admitted_ms"] >= 0, "manifest_clock")
    state.setdefault("retry_after_ms", 0)
    _require(type(state["retry_after_ms"]) is int and state["retry_after_ms"] >= 0, "manifest_retry_clock")
    for aid, row in state["attempts"].items():
        _require(isinstance(aid, str) and _HEX.fullmatch(aid) is not None and isinstance(row, dict)
                 and row.get("attempt_id") == aid and _clock(row.get("admitted_ms"))
                 and isinstance(row.get("context"), dict) and _hash(row["context"]) == row.get("context_hash")
                 and aid == _hash({"chain": "sol", "signature": row["context"].get("signature")}),
                 "manifest_attempt_identity")
        configured = row.get("acquisition_policy") == CONFIGURED_POLICY
        _require(row.get("acquisition_policy") in (None, CONFIGURED_POLICY)
                 and type(row.get("request_slots", 1)) is int
                 and row.get("request_slots", 1) == (2 if configured else 1)
                 and (not configured or isinstance(row.get("settings_binding"), str)
                      and _HEX.fullmatch(row["settings_binding"]) is not None), "manifest_request_slots")
        if "genesis" in row:
            _encode(row["genesis"], 8192)
            _require(configured and _genesis_valid(row), "manifest_genesis_witness")
    _require(sum(r.get("request_slots", 1) for r in state["attempts"].values()) <= LIFETIME_REQUEST_SLOTS,
             "manifest_request_budget")
    for path in directory.iterdir():
        match = _RECEIPT.fullmatch(path.name)
        _require(match is None or match[1] in state["attempts"], "receipt_without_admission_manifest")
    return state


def _store_size(directory: Path) -> tuple[int, int]:
    total = receipts = files = 0
    for path in directory.iterdir():
        files += 1
        _require(files <= MAX_RECEIPTS + 5 and not _linked(path) and path.is_file(), "store_contents_refused")
        size = path.stat().st_size
        receipt = _RECEIPT.fullmatch(path.name)
        _require(bool(receipt) or path.name in {"manifest.json", "manifest.partial", "acquisition.lock"},
                 "store_contents_refused")
        _require(size <= (MAX_FILE_BYTES if receipt else MAX_MANIFEST_BYTES), "stored_file_budget_or_type")
        total += size
        receipts += bool(receipt)
    _require(total <= MAX_STORE_BYTES and receipts <= MAX_RECEIPTS, "store_budget")
    return total, receipts


def _qualified(row: Mapping) -> bool:
    q = row.get("_capture_qualifications", {})
    return (not isinstance(q, dict) or q.get("available_at_frontier") is False
            or q.get("position_identity_verified") is False
            or q.get("position_link_available_at_frontier") is False)


def _context(packet: Mapping, order_id: str, now_ms: int) -> dict:
    _require(isinstance(packet, Mapping) and isinstance(order_id, str) and 0 < len(order_id) <= 256,
             "input_identity")
    _encode(packet, MAX_PACKET_BYTES)
    p, orders, links = packet.get("position"), packet.get("orders"), packet.get("position_order_links")
    _require(isinstance(p, dict) and p.get("chain") == "sol" and p.get("mode") in ("live", "canary")
             and not _qualified(p), "position_identity")
    _require(isinstance(orders, list) and isinstance(links, list) and len(orders) <= 64 and len(links) <= 64,
             "explicit_position_order_links_required")
    selected = [o for o in orders if isinstance(o, dict) and o.get("order_id") == order_id]
    joined = [r for r in links if isinstance(r, dict) and r.get("order_id") == order_id]
    _require(len(selected) == len(joined) == 1, "exact_unique_order_link_required")
    o, link = selected[0], joined[0]
    _require(o.get("state") == "filled" and o.get("side") in ("buy", "sell") and not _qualified(o)
             and not _qualified(link) and link.get("position_id") == p.get("position_id")
             and link.get("side") == o.get("side")
             and all(o.get(k) == p.get(k) for k in ("chain", "token", "mode", "lane")), "order_identity")
    for value in (p.get("position_id"), p.get("lane")):
        _require(isinstance(value, str) and 0 < len(value) <= 256, "position_identity")
    for row, fields in ((p, ("opened_ms", "closed_ms")), (o, ("created_ms", "updated_ms")),
                        (link, ("ts_ms",))):
        for field in fields:
            value = row.get(field)
            _require(value is None or (_clock(value) and value <= now_ms), "future_or_invalid_source_clock")
    try:
        _sol_id(p.get("token"), 32)
        _sol_id(o.get("tx_hash"), 64)
    except ValueError:
        raise ReceiptRefused("malformed_chain_identifier") from None
    return {"position_id": p["position_id"], "order_id": order_id, "chain": "sol", "token": p["token"],
            "side": o["side"], "signature": o["tx_hash"], "mode": p["mode"], "lane": p["lane"]}


def endpoint_category(endpoint: str, *, configured: bool = False) -> str:
    """Return category metadata only. Never return any endpoint or credential text."""
    try:
        if configured:
            _require(isinstance(endpoint, str) and 0 < len(endpoint) <= 8192
                     and all(32 < ord(c) < 127 for c in endpoint) and "\\" not in endpoint
                     and re.search(r"%(?![0-9a-fA-F]{2})", endpoint) is None
                     and all(ord(c) >= 32 and ord(c) != 127 and c != "\\" for c in unquote(endpoint)),
                     "configured_endpoint_not_permitted")
            p = urlsplit(endpoint)
            host = p.hostname or ""
            if (p.scheme == "https" and p.username is None and p.password is None and not p.fragment
                    and p.port in (None, 443) and len(host) <= 253
                    and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                            for label in host.split("."))
                    and p.netloc.lower() in {host.lower(), host.lower() + ":443"}
                    and len(p.path.split("/")) <= 32 and len(parse_qsl(p.query, keep_blank_values=True)) <= 32):
                return "configured_solana_rpc"
            return "not_permitted"
        p = urlsplit(endpoint)
        if (p.scheme == "https" and p.netloc == "api.mainnet-beta.solana.com" and p.path in {"", "/"}
                and not p.query and not p.fragment and p.username is None and p.password is None):
            return "public_solana_mainnet"
    except (TypeError, ValueError, AttributeError):
        pass
    return "not_permitted"


def _configured_settings() -> tuple[str, Path, str]:
    # Ordinary cached Settings only. Do not reload a different config or substitute an endpoint.
    from kaiba.core.config import get_settings
    try:
        settings = get_settings()
        endpoint, path = settings.rpc_for(Chain.SOL), Path(settings.db_path).resolve()
    except Exception:
        raise ReceiptRefused("configured_settings_unavailable") from None
    _require(endpoint_category(endpoint, configured=True) == "configured_solana_rpc",
             "configured_endpoint_not_permitted")
    return endpoint, path, _hash({"endpoint": endpoint, "db_path": str(path)})


def configured_binding() -> str:
    """Opaque PRIVATE preprobe binding. Never publish this endpoint-derived token."""
    return _configured_settings()[2]


def _private_response(data, endpoint: str) -> None:
    """Reject endpoint credential echoes before retaining parsed provider JSON."""
    raw = _encode(data, MAX_FILE_BYTES)
    parts = urlsplit(endpoint)
    path_parts = [p for p in parts.path.split("/") if p]
    query_parts = [p.partition("=")[2] for p in parts.query.split("&") if "=" in p]
    needles = {endpoint, parts.netloc, *path_parts, *query_parts, *[unquote(p) for p in path_parts],
               *[v for _, v in parse_qsl(parts.query, keep_blank_values=True) if v]}
    needles.discard("")
    for needle in needles:
        encoded = _encode(needle, 16384)
        # Short values require a whole JSON string; long keys may appear inside an error message.
        _require((encoded[1:-1] if len(needle) >= 8 else encoded) not in raw,
                 "configured_response_credential_echo")
    sensitive = {"apikey", "privatekey", "authorization", "password", "secret", "accesstoken", "authtoken"}
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                _require(not (isinstance(key, str) and re.sub(r"[^a-z]", "", key.lower()) in sensitive
                              and item not in (None, "")), "configured_response_credential_echo")
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)


def _receipt_fields(receipt, endpoint: str = "sol.getTransaction") -> dict | None:
    if receipt is None:
        return None
    try:
        original = receipt.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError):
        return None
    return {"provider": "rpc" if original.get("provider") == "rpc" else "unexpected_provider",
        "endpoint": endpoint if original.get("endpoint") == endpoint else "unexpected_endpoint",
        "observed_at_ms": original.get("observed_at_ms") if type(original.get("observed_at_ms")) is int else None,
        "basis": original.get("basis") if original.get("basis") in (
            "provider_reported", "cached", "stale", "unavailable", "verified_onchain") else "unexpected_basis",
        **{k: original.get(k) if isinstance(original.get(k), str) and _HEX.fullmatch(original[k]) else None
           for k in ("request_digest", "response_digest")}}


def _genesis_valid(content: Mapping) -> bool:
    """An original endpoint-bound RPC assertion of mainnet, not independent consensus proof."""
    try:
        aid, genesis = content["attempt_id"], content["genesis"]
        request, response, receipt = (genesis[k] for k in ("request", "response", "receipt"))
        start, end = genesis["dispatched_ms"], genesis["completed_ms"]
        observed = receipt["observed_at_ms"]
        return (content["acquisition_policy"] == CONFIGURED_POLICY
            and type(content["request_slots"]) is int and content["request_slots"] == 2
            and isinstance(content["settings_binding"], str) and _HEX.fullmatch(content["settings_binding"]) is not None
            and genesis["settings_binding"] == content["settings_binding"]
            and aid == _hash({"chain": "sol", "signature": content["context"]["signature"]})
            and request == {"jsonrpc": "2.0", "id": "kaiba-genesis-" + aid[:16],
                            "method": "getGenesisHash", "params": []}
            and genesis["request_identity_sha256"] == _hash({"chain": "sol", "request": request})
            and receipt["provider"] == "rpc" and receipt["endpoint"] == "sol.getGenesisHash"
            and receipt["basis"] == "provider_reported"
            and receipt["request_digest"] == digest("genesis:sol:" + aid)
            and receipt["response_digest"] == digest(response)
            and response.get("jsonrpc") == "2.0" and response.get("id") == request["id"]
            and response.get("error") is None and response.get("result") == MAINNET_GENESIS
            and all(_clock(t) for t in (start, end, observed, content["admitted_ms"]))
            and content["admitted_ms"] <= start <= observed <= end
            and start // DAY_MS == content["admitted_ms"] // DAY_MS)
    except (KeyError, TypeError, AttributeError, ReceiptRefused):
        return False


def _uint(value) -> bool:
    return type(value) is int and 0 <= value < 2 ** 64


def _sol_address(value) -> bool:
    try:
        _sol_id(value, 32)
        return True
    except ValueError:
        return False


def summarize_receipt(content: Mapping) -> dict:
    """Observed balance components only; no fee-payer or missing-owner inference."""
    summary = {"status": "unavailable", "economic_complete": False, "complete_cashflow": False,
        "economic_native_delta": None, "rent_classification": "unproven", "funding_allowed": False,
        "wallet_attribution": "not_supplied" if content.get("expected_wallet") is None else "unproven",
        "side_matches_supplied_wallet": None,
        "wallet_native_delta_atoms": None, "wallet_token_delta_atoms": None,
        "network_fee_native_atoms": None, "native_account_balances": [], "token_account_balances": [],
        "unknown_reasons": []}
    if any(k in content for k in ("acquisition_policy", "request_slots", "settings_binding", "genesis")):
        transaction_ms = content.get("transaction_dispatched_ms")
        end = content.get("completed_ms")
        if (not _genesis_valid(content) or content.get("settings_consistent") is not True
                or not _clock(transaction_ms) or not _clock(end)
                or not content["genesis"]["completed_ms"] <= transaction_ms <= end
                or transaction_ms // DAY_MS != content["admitted_ms"] // DAY_MS
                or content.get("request_identity_sha256") != _hash({"chain": "sol", "request": content.get("request")})):
            summary.update(status="configured_mainnet_unproved", unknown_reasons=["original_mainnet_witness_or_binding_unproved"])
            return summary
    context, request, response, receipt = (content.get(k) for k in ("context", "request", "response", "receipt"))
    if not all(isinstance(v, dict) for v in (context, request, response, receipt)):
        summary["unknown_reasons"].append("provider_response_or_receipt_unavailable")
        return summary
    params = request.get("params")
    if (context.get("chain") != "sol" or context.get("side") not in ("buy", "sell")
            or not isinstance(context.get("signature"), str) or request.get("jsonrpc") != "2.0"
            or request.get("method") != "getTransaction" or not isinstance(params, list) or len(params) != 2
            or params[0] != context["signature"] or not isinstance(params[1], dict)
            or params[1].get("encoding") != "jsonParsed" or params[1].get("commitment") != "finalized"
            or type(params[1].get("maxSupportedTransactionVersion")) is not int):
        summary.update(status="identity_mismatch", unknown_reasons=["explicit_request_identity_mismatch"])
        return summary
    if (receipt.get("provider") != "rpc" or receipt.get("endpoint") != "sol.getTransaction"
            or receipt.get("basis") != "provider_reported"
            or receipt.get("request_digest") != digest("tx:sol:" + context["signature"])
            or receipt.get("response_digest") != digest(response)):
        summary.update(status="provenance_mismatch", unknown_reasons=["provider_receipt_identity_mismatch"])
        return summary
    observed, end, start = receipt.get("observed_at_ms"), content.get("completed_ms"), content.get("admitted_ms")
    if not (_clock(observed) and _clock(start) and _clock(end) and start <= observed <= end):
        summary.update(status="clock_unavailable", unknown_reasons=["provider_observation_clock_unproved"])
        return summary
    if content.get("acquisition_policy") == CONFIGURED_POLICY and observed < content["transaction_dispatched_ms"]:
        summary.update(status="clock_unavailable", unknown_reasons=["provider_observation_clock_unproved"])
        return summary
    if response.get("jsonrpc") != "2.0" or response.get("id") != request.get("id"):
        summary.update(status="identity_mismatch", unknown_reasons=["rpc_request_response_identity_mismatch"])
        return summary
    if response.get("error") is not None:
        summary.update(status="rpc_error", unknown_reasons=["rpc_error_response"])
        return summary
    result = response.get("result")
    if not isinstance(result, dict):
        summary["unknown_reasons"].append("transaction_result_unavailable")
        return summary
    tx, meta = result.get("transaction"), result.get("meta")
    if not isinstance(tx, dict) or not isinstance(meta, dict):
        summary["unknown_reasons"].append("transaction_or_meta_unavailable")
        return summary
    signatures = tx.get("signatures")
    if not isinstance(signatures, list) or not signatures or signatures[0] != context["signature"]:
        summary.update(status="identity_mismatch", unknown_reasons=["transaction_signature_mismatch"])
        return summary
    block = result.get("blockTime")
    if not (type(block) is int and 0 < block * 1000 <= observed and _clock(result.get("slot"))):
        summary.update(status="clock_unavailable", unknown_reasons=["transaction_clock_or_slot_unproved"])
        return summary
    summary["status"] = "observed_success" if "err" in meta and meta["err"] is None else "observed_failed" \
        if "err" in meta else "transaction_status_unknown"
    if summary["status"] == "observed_success" and not any(
            isinstance(row, dict) and row.get("mint") == context.get("token")
            for field in ("preTokenBalances", "postTokenBalances")
            for row in (meta.get(field) if isinstance(meta.get(field), list) else [])):
        summary.update(status="identity_mismatch", unknown_reasons=["expected_token_not_observed_in_transaction"])
        return summary
    summary["network_fee_native_atoms"] = str(meta["fee"]) if _uint(meta.get("fee")) else None
    message = tx.get("message")
    entries = message.get("accountKeys") if isinstance(message, dict) else None
    keys = [e.get("pubkey") if isinstance(e, dict) else e for e in entries] if isinstance(entries, list) else []
    pre, post = meta.get("preBalances"), meta.get("postBalances")
    if (0 < len(keys) <= 512 and all(_sol_address(k) for k in keys) and len(set(keys)) == len(keys)
            and isinstance(pre, list) and isinstance(post, list) and len(keys) == len(pre) == len(post)
            and all(_uint(n) for n in pre + post)):
        summary["native_account_balances"] = [{"account": key, "pre_atoms": str(pre[i]),
            "post_atoms": str(post[i]), "delta_atoms": str(post[i] - pre[i])} for i, key in enumerate(keys)]
    else:
        summary["unknown_reasons"].append("native_balance_arrays_unproved")
    token_rows, blocked = {}, set()
    for section, label in (("preTokenBalances", "pre_atoms"), ("postTokenBalances", "post_atoms")):
        rows = meta.get(section)
        if not isinstance(rows, list) or len(rows) > 512:
            summary["unknown_reasons"].append("token_balance_arrays_unproved")
            continue
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            i, owner, mint = row.get("accountIndex"), row.get("owner"), row.get("mint")
            amount = row.get("uiTokenAmount", {})
            raw, decimals = (amount.get("amount"), amount.get("decimals")) if isinstance(amount, dict) else (None, None)
            if (type(i) is not int or not 0 <= i < len(keys) or not _sol_address(owner)
                    or not _sol_address(mint) or not isinstance(raw, str)
                    or not re.fullmatch(r"0|[1-9][0-9]{0,19}", raw) or int(raw) >= 2 ** 64
                    or type(decimals) is not int or not 0 <= decimals <= 255):
                summary["unknown_reasons"].append("token_balance_owner_or_quantity_unproved")
                continue
            key = (i, owner, mint, decimals)
            if key in blocked:
                continue
            if key in seen:
                summary["unknown_reasons"].append("duplicate_token_balance")
                blocked.add(key)
                token_rows.pop(key, None)
                continue
            seen.add(key)
            target = token_rows.setdefault(key, {"account": keys[i], "owner": owner, "mint": mint,
                "decimals": decimals, "pre_atoms": None, "post_atoms": None, "delta_atoms": None})
            target[label] = raw
    for row in token_rows.values():
        if row["pre_atoms"] is not None and row["post_atoms"] is not None:
            row["delta_atoms"] = str(int(row["post_atoms"]) - int(row["pre_atoms"]))
    summary["token_account_balances"] = list(token_rows.values())
    wallet = content.get("expected_wallet")
    if wallet is not None and summary["status"] == "observed_success":
        parsed = parse_solana_costs(result, wallet=wallet, token=context["token"],
                                    signature=context["signature"], side=context["side"])
        delta = parsed.get("wallet_token_delta_atoms")
        matches = delta is not None and (int(delta) > 0 if context["side"] == "buy" else int(delta) < 0)
        summary["side_matches_supplied_wallet"] = bool(parsed.get("identity_verified") and matches)
        if parsed.get("identity_verified") and matches:
            summary.update(wallet_attribution="supplied_signer_and_token_side_observed",
                wallet_native_delta_atoms=parsed.get("wallet_native_delta_atoms"), wallet_token_delta_atoms=delta)
        else:
            summary["unknown_reasons"].append("supplied_wallet_or_order_side_unproved")
    summary["unknown_reasons"] = sorted(set(summary["unknown_reasons"] + [
        "episode_coverage_unproved", "rent_transfers_and_recoveries_unproved", "historical_wallet_binding_not_certified"]))
    return summary


def _metadata(row: Mapping, *, reused: bool) -> dict:
    return {"version": VERSION, "attempt_id": row["attempt_id"], "status": row.get("status", "admitted"),
        "admitted_ms": row.get("admitted_ms"), "completed_ms": row.get("completed_ms"),
        "receipt_file": row.get("receipt_file"), "receipt_sha256": row.get("receipt_sha256"),
        "observation_status": row.get("observation_status"), "reused": reused,
        "dispatch_admitted": not reused,
        "requires_manual_review": row.get("status") in {"admitted", "ambiguous", "response_not_retained"},
        "economic_complete": False, "funding_allowed": False,
        **({"acquisition_policy": row["acquisition_policy"], "reserved_request_slots": row["request_slots"]}
           if row.get("acquisition_policy") else {})}


def _load_receipt(directory: Path, row: dict) -> tuple[dict, str]:
    path = directory / ("receipt-" + row["attempt_id"] + ".json")
    stored, sha = _read(path, MAX_FILE_BYTES)
    _require(isinstance(stored, dict) and isinstance(stored.get("content"), dict)
             and stored.get("content_sha256") == _hash(stored["content"]), "stored_receipt_integrity")
    content = stored["content"]
    _require(content.get("attempt_id") == row["attempt_id"] and content.get("context") == row["context"]
             and content.get("context_hash") == row["context_hash"], "stored_receipt_identity")
    _require(all(content.get(k) == row.get(k) for k in ("acquisition_policy", "request_slots", "settings_binding")),
             "stored_receipt_policy")
    _require("genesis" not in row or content.get("genesis") == row["genesis"], "stored_genesis_witness")
    _require(row.get("receipt_sha256") in (None, sha), "stored_receipt_integrity")
    return content, sha


def _guard_wal(db_path: Path) -> None:
    # Only filesystem metadata here: no source query or persistent read connection.
    from kaiba.learning.autolearn_io import GuardedReader
    GuardedReader(db_path, query_seconds=3).guard()


def _bookkeeping_db(db_path: Path) -> None:
    """Inspect the shared thread-local connection; never replace or close it."""
    from kaiba.core import db
    try:
        conn = db.get_conn()
        _require(conn.in_transaction is False, "configured_bookkeeping_transaction")
        cursor = conn.execute("PRAGMA database_list")
        try:
            rows = cursor.fetchmany(3)
        finally:
            cursor.close()
        main = [r for r in rows if r[1] == "main"]
        # Core migrations initialize SQLite's own empty-filename temp schema.
        # It is not an attached bookkeeping DB; every other attachment refuses.
        _require(len(rows) <= 2 and len(main) == 1 and all(r[1] == "main" or r[1] == "temp" and r[2] == "" for r in rows)
                 and isinstance(main[0][2], str) and bool(main[0][2]) and Path(main[0][2]).resolve() == db_path,
                 "configured_bookkeeping_database_mismatch")
    except ReceiptRefused:
        raise
    except Exception:
        raise ReceiptRefused("configured_bookkeeping_unavailable") from None


def _configured_pace(db_path: Path, clock: Callable) -> dict:
    """Observe one exact shared floor, close its reader, then wait at most once.

    This does not reserve capacity, predict cooldown/credits, or replace the
    authoritative limiter. The original HTTP caller still uses wait0/retries1.
    """
    from kaiba.core.limiter import limits_for
    from kaiba.learning.autolearn_io import GuardedReader

    before, mono = clock(), time.monotonic()
    _require(_clock(before) and math.isfinite(mono), "configured_pacing_clock")
    reader = GuardedReader(db_path, query_seconds=3, max_rows=1)
    rows = reader.rows("SELECT last_call_ms,penalty_level FROM provider_state WHERE provider=? LIMIT 1", ("rpc",))
    # rows() has closed the source connection before returning.
    at, current_mono = clock(), time.monotonic()
    _require(_clock(at) and at >= before and math.isfinite(current_mono) and current_mono >= mono,
             "configured_pacing_clock")
    _require(len(rows) == 1 and type(rows[0].get("last_call_ms")) is int
             and 0 < rows[0]["last_call_ms"] <= at and type(rows[0].get("penalty_level")) is int
             and rows[0]["penalty_level"] >= 0, "configured_pacing_state_unavailable")
    interval = limits_for("rpc").min_interval_ms
    _require(type(interval) is int and interval >= 0, "configured_pacing_policy_unavailable")
    floor = interval * 2 ** min(rows[0]["penalty_level"], 8)
    remaining = max(0, rows[0]["last_call_ms"] + floor - at)
    _require(remaining <= 20_000, "configured_pacing_exceeds_bound")
    if remaining:
        started = time.monotonic()
        time.sleep(remaining / 1000)
        after, stopped = clock(), time.monotonic()
        _require(_clock(after) and after >= at + remaining and math.isfinite(stopped)
                 and stopped >= started and stopped - started + 1e-6 >= remaining / 1000,
                 "configured_pacing_clock")
    return {"floor_ms": floor, "wait_ms": remaining, "source_query_receipts": reader.receipts}


def _configured_fetch(content: dict, endpoint: str, db_path: Path, binding: str,
                      clock: Callable, transport: Callable, state: dict, directory: Path) -> None:
    from kaiba.core.limiter import Priority
    from kaiba.learning.autolearn_io import SourceDeferred

    prior = content["admitted_ms"]

    def boundary(*, provider: bool = False) -> int:
        nonlocal prior
        _require(_configured_settings() == (endpoint, db_path, binding), "configured_settings_changed")
        try:
            _guard_wal(db_path)  # The original HTTP limiter touches this exact bookkeeping DB.
        except SourceDeferred:
            at = clock()
            state["retry_after_ms"] = max(prior, at if _clock(at) else prior) + 60_000
            _save_manifest(directory, state)
            raise ReceiptRefused("configured_source_wal_backoff") from None
        at = clock()
        _require(_clock(at) and at >= prior, "configured_clock_moved_backwards")
        _require(at // DAY_MS == content["admitted_ms"] // DAY_MS, "configured_admission_day_changed")
        _require(_configured_settings() == (endpoint, db_path, binding), "configured_settings_changed")
        if provider:
            _bookkeeping_db(db_path)
            # Opening the ordinary shared connection can itself take time.
            at = clock()
            _require(_clock(at) and at >= prior, "configured_clock_moved_backwards")
            _require(at // DAY_MS == content["admitted_ms"] // DAY_MS, "configured_admission_day_changed")
            _require(_configured_settings() == (endpoint, db_path, binding), "configured_settings_changed")
        prior = at
        return at

    aid = content["attempt_id"]
    request = {"jsonrpc": "2.0", "id": "kaiba-genesis-" + aid[:16], "method": "getGenesisHash", "params": []}
    content.update(genesis={"request": request, "request_identity_sha256": _hash({"chain": "sol", "request": request}),
        "settings_binding": binding, "response": None, "receipt": None}, settings_consistent=False)
    try:
        content["genesis"]["dispatched_ms"] = boundary(provider=True)
        got = transport("rpc", "sol.getGenesisHash", endpoint, json_body=request,
            cache_key="genesis:sol:" + aid, priority=Priority.RESEARCH,
            ttl_s=0, retries=1, wait_for_slot_s=0, timeout_s=20)
        _require(isinstance(got, Fetched), "configured_transport_result_shape")
        _private_response(got.data, endpoint)
        content["genesis"].update(response=got.data, receipt=_receipt_fields(got.receipt, "sol.getGenesisHash"))
        content["genesis"]["completed_ms"] = boundary()
        _require(_genesis_valid(content), "configured_mainnet_witness_unavailable")
        _encode(content["genesis"], 8192)
        # The original validated witness is durable BEFORE pacing or transaction dispatch.
        state["attempts"][aid]["genesis"] = content["genesis"]
        _save_manifest(directory, state)
        content["inter_leg_pacing"] = _configured_pace(db_path, clock)
        content["transaction_dispatched_ms"] = boundary(provider=True)
        got = transport("rpc", "sol.getTransaction", endpoint, json_body=content["request"],
            cache_key="tx:sol:" + content["context"]["signature"], priority=Priority.RESEARCH,
            ttl_s=0, retries=1, wait_for_slot_s=0, timeout_s=20)
        _require(isinstance(got, Fetched), "configured_transport_result_shape")
        _private_response(got.data, endpoint)
        content.update(response=got.data, receipt=_receipt_fields(got.receipt))
        content["completed_ms"] = boundary()
        content.update(settings_consistent=True, status="retained")
    except SourceDeferred:
        at = clock()
        state["retry_after_ms"] = max(prior, at if _clock(at) else prior) + 60_000
        _save_manifest(directory, state)
        content.update(status="ambiguous", failure_reason="configured_source_wal_backoff")
    except ReceiptRefused as exc:
        content.update(status="ambiguous", failure_reason=str(exc))
    except Exception:
        # Never store provider exception class names, messages, URLs or credential-bearing notes.
        content.update(status="ambiguous", failure_reason="configured_transport_unavailable")


def capture_one(directory: Path | str, packet: Mapping, order_id: str, *, now_ms: int,
                expected_wallet: str | None = None, learning_root: Path | str | None = None,
                endpoint: str | None = None, transport: Callable | None = None,
                lock_factory: Callable | None = None, clock: Callable[[], int] | None = None,
                db_path: Path | str | None = None, configured: bool = False,
                settings_binding: str | None = None) -> dict:
    """Admit one lookup (two request slots in explicit configured mode), never resend.

    ``lock_factory(path)`` must implement the existing JobLock context-manager
    contract. Tests inject locks and transport; the production default also takes
    the shared ``fill_costs.lock``. No supplied wallet is inferred from a fee payer.
    Configured mode derives endpoint/DB solely from ordinary Settings, optionally
    matching a PRIVATE ``configured_binding()`` preprobe token. The same cached
    Settings view is checked at each boundary; this is not a disk-config reload.
    """
    from kaiba.core.limiter import Priority
    from kaiba.learning.autolearn_io import SourceDeferred
    from kaiba.ops.scheduler import (
        AlreadyRunning,
        JobLock,
        ScheduleConfigError,
        default_lock_dir,
        load_config,
    )

    clock = clock or (lambda: int(time.time() * 1000))
    admitted = False
    try:
        _require(_clock(now_ms), "invalid_admission_clock")
        _require(type(configured) is bool, "invalid_configured_mode")
        _require(configured or settings_binding is None, "configured_binding_without_mode")
        context = _context(packet, order_id, now_ms)
        if expected_wallet is not None:
            try:
                _sol_id(expected_wallet, 32)
            except ValueError:
                raise ReceiptRefused("malformed_expected_wallet") from None
        directory = _paths(directory, learning_root)
        binding = None
        if configured:
            _require(endpoint is None and db_path is None, "configured_caller_override")
            endpoint, db_path, binding = _configured_settings()
            _require(settings_binding is None or settings_binding == binding, "configured_probe_binding_changed")
        elif endpoint is None or db_path is None:
            from kaiba.core.config import get_settings
            settings = get_settings()
            endpoint = settings.rpc_for(Chain.SOL) if endpoint is None else endpoint
            db_path = settings.db_path if db_path is None else db_path
        db_path = Path(db_path).resolve()
        _require(not db_path.is_relative_to(directory), "source_inside_receipt_store")
        if not configured:
            _require(endpoint_category(endpoint) == "public_solana_mainnet", "endpoint_not_permitted")
        directory.mkdir(parents=True, exist_ok=True)
        if lock_factory is None:
            stale = load_config().lock_stale_s
            def factory(path):
                return JobLock(path, stale_after_s=stale)
        else:
            factory = lock_factory
        with ExitStack() as locks:
            locks.enter_context(factory(default_lock_dir() / "fill_costs.lock"))
            locks.enter_context(factory(directory / "acquisition.lock"))
            _store_size(directory)
            if configured:
                _require((directory / "manifest.json").is_file(), "configured_existing_manifest_required")
            state = _load_manifest(directory)
            if configured:
                for prior_row in state["attempts"].values():
                    prior_path = directory / ("receipt-" + prior_row["attempt_id"] + ".json")
                    _require(not prior_row.get("receipt_file") or prior_path.is_file(), "stored_receipt_missing")
                    _require(prior_row.get("status") != "retained" or prior_row.get("receipt_file"),
                             "stored_receipt_missing")
                    if prior_path.exists():
                        _require(prior_row.get("receipt_file") in (None, prior_path.name), "stored_receipt_identity")
                        _load_receipt(directory, prior_row)
            aid = _hash({"chain": "sol", "signature": context["signature"]})
            row = state["attempts"].get(aid)
            if row is not None:
                _require(row["context_hash"] == _hash(context), "transaction_already_bound_to_another_context")
                receipt_path = directory / ("receipt-" + aid + ".json")
                _require(not row.get("receipt_file") or receipt_path.exists(), "stored_receipt_missing")
                if receipt_path.exists():
                    content, sha = _load_receipt(directory, row)
                    if row.get("status") == "admitted":
                        row.update(status=content["status"], completed_ms=content["completed_ms"],
                            receipt_file=receipt_path.name, receipt_sha256=sha,
                            observation_status=summarize_receipt(content)["status"])
                        _save_manifest(directory, state)
                return _metadata(row, reused=True)
            _require(now_ms >= state["last_admitted_ms"], "admission_clock_moved_backwards")
            slots = 2 if configured else 1
            daily = sum(r.get("request_slots", 1) for r in state["attempts"].values()
                        if r["admitted_ms"] // DAY_MS == now_ms // DAY_MS)
            _require(daily + slots <= DAILY_ADMISSIONS, "daily_admission_budget")
            _require(len(state["attempts"]) < MAX_RECEIPTS, "receipt_count_budget")
            _require(sum(r.get("request_slots", 1) for r in state["attempts"].values()) + slots <= LIFETIME_REQUEST_SLOTS,
                     "lifetime_request_budget")
            total, count = _store_size(directory)
            _require(count < MAX_RECEIPTS and total + MAX_FILE_BYTES + 2 * MAX_MANIFEST_BYTES <= MAX_STORE_BYTES,
                     "store_admission_budget")
            if now_ms < state["retry_after_ms"]:
                return {"version": VERSION, "status": "deferred", "reason": "source_wal_backoff",
                    "retry_after_ms": state["retry_after_ms"], "dispatch_admitted": False,
                    "economic_complete": False, "funding_allowed": False}
            try:
                _guard_wal(db_path)
            except SourceDeferred:
                checked = clock()
                state["retry_after_ms"] = max(now_ms, checked if _clock(checked) else now_ms) + 60_000
                _save_manifest(directory, state)
                return {"version": VERSION, "status": "deferred", "reason": "source_wal_backoff",
                    "retry_after_ms": state["retry_after_ms"], "dispatch_admitted": False,
                    "economic_complete": False, "funding_allowed": False}
            state["retry_after_ms"] = 0
            if configured:
                _require(_configured_settings() == (endpoint, db_path, binding), "configured_settings_changed")
            request = {"jsonrpc": "2.0", "id": "kaiba-economic-" + aid[:16], "method": "getTransaction",
                "params": [context["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                                   "maxSupportedTransactionVersion": MAX_TX_VERSION}]}
            row = {"attempt_id": aid, "context": context, "context_hash": _hash(context),
                   "admitted_ms": now_ms, "status": "admitted"}
            if configured:
                row.update(acquisition_policy=CONFIGURED_POLICY, request_slots=2, settings_binding=binding)
            state["attempts"][aid] = row
            state["last_admitted_ms"] = now_ms
            _save_manifest(directory, state)  # Durable before any dispatch; no retry of uncertain work.
            admitted = True
            content = {"version": VERSION, **row, "request": request, "expected_wallet": expected_wallet,
                "input_packet_sha256": hashlib.sha256(_encode(packet, MAX_PACKET_BYTES)).hexdigest(),
                "endpoint_category": "public_solana_mainnet", "response": None, "receipt": None,
                "numeric_encoding": "integer_atoms_unchanged_decimals_as_strings_matching_provider_digest",
                "receipt_note_retention": "omitted_to_avoid_unsafe_provider_exception_text"}
            if configured:
                content.update(endpoint_category="configured_solana_rpc",
                               request_identity_sha256=_hash({"chain": "sol", "request": request}))
                _configured_fetch(content, endpoint, db_path, binding, clock, transport or post_json, state, directory)
            else:
                try:
                    got = (transport or post_json)("rpc", "sol.getTransaction", endpoint, json_body=request,
                        cache_key="tx:sol:" + context["signature"], priority=Priority.RESEARCH,
                        ttl_s=0, retries=1, wait_for_slot_s=0, timeout_s=20)
                    _require(isinstance(got, Fetched), "transport_result_shape")
                    content.update(response=got.data, receipt=_receipt_fields(got.receipt), status="retained")
                except Exception as exc:
                    content.update(status="ambiguous", transport_error_type=type(exc).__name__)
            end = clock()
            content["completed_ms"] = end if _clock(end) and end >= now_ms else now_ms
            try:
                stored = {"content": content, "content_sha256": _hash(content)}
                raw = _encode(stored, MAX_FILE_BYTES)
            except ReceiptRefused:
                row.update(status="response_not_retained", completed_ms=content["completed_ms"],
                           observation_status="response_size_or_serialization_refused")
                _save_manifest(directory, state)
                return _metadata(row, reused=False)
            receipt_path = directory / ("receipt-" + aid + ".json")
            _write_new(receipt_path, raw)
            row.update(status=content["status"], completed_ms=content["completed_ms"],
                receipt_file=receipt_path.name, receipt_sha256=hashlib.sha256(raw).hexdigest(),
                observation_status=summarize_receipt(content)["status"])
            _save_manifest(directory, state)
            return _metadata(row, reused=False)
    except AlreadyRunning:
        return {"version": VERSION, "status": "deferred", "reason": "receipt_job_locked",
                "dispatch_admitted": False, "economic_complete": False, "funding_allowed": False}
    except ScheduleConfigError:
        return {"version": VERSION, "status": "refused", "reason": "receipt_schedule_unavailable",
                "dispatch_admitted": False, "economic_complete": False, "funding_allowed": False}
    except (OSError, ReceiptRefused) as exc:
        return {"version": VERSION, "status": "ambiguous" if admitted else "refused",
            "reason": str(exc) if isinstance(exc, ReceiptRefused) else "receipt_storage_unavailable",
            "dispatch_admitted": admitted, "requires_manual_review": admitted,
            "economic_complete": False, "funding_allowed": False}


def read_metadata(directory: Path | str, *, learning_root: Path | str | None = None) -> dict:
    """Bounded, read-only metadata; no raw addresses, signatures, notes or transport."""
    directory = _paths(directory, learning_root)
    if not directory.exists():
        return {"version": VERSION, "status": "unavailable", "attempts": [], "funding_allowed": False}
    total, receipts = _store_size(directory)
    state = _load_manifest(directory)
    for row in state["attempts"].values():
        if row.get("receipt_file"):
            _load_receipt(directory, row)
    return {"version": VERSION, "status": "complete", "store_bytes": total, "receipt_count": receipts,
        "retry_after_ms": state["retry_after_ms"],
        "attempts": [_metadata(row, reused=True) for row in state["attempts"].values()],
        "economic_complete": False, "funding_allowed": False}
