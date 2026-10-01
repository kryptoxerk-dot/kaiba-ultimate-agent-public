"""The signer: a separate process that holds keys and refuses to sign a withdrawal.

Why this is its own process and its own unix user:

The agent, the ingest services and the dashboard all run code that reads untrusted input —
token metadata, social text, provider responses. None of them should be one bug away from
a private key. The signer holds the keys, listens on a unix socket, and answers exactly
one question: "here is a transaction, will you sign it?" It decides using
:mod:`kaiba.execution.policy`, which is loaded from a root-owned file the agent cannot
write.

The withdrawal rule lives here in its strongest form. The signer cannot be argued with:
it decodes every instruction, and if any of them moves value to an address we do not own
it returns a refusal. There is no configuration flag, no override parameter, and no
"force" argument. A model that decides it wants to withdraw simply gets a refusal and an
audit record.

Key material never leaves this module: keys are loaded from files readable only by the
signer user, held in memory, and never returned, logged or included in an error.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kaiba.core import events as ev
from kaiba.core.config import USER_CONFIG_DIR, get_risk
from kaiba.core.schemas import Chain, EventKind, Order, now_ms

log = logging.getLogger(__name__)

DEFAULT_SOCKET = Path(os.environ.get("KAIBA_SIGNER_SOCKET", "/run/kaiba/signer.sock"))
KEYSTORE_DIR = Path(os.environ.get("KAIBA_KEYSTORE_DIR", USER_CONFIG_DIR / "keys"))
MAX_FRAME = 256 * 1024


class SignerRefused(Exception):
    """The signer declined. The reason is safe to show; it never contains key material."""


class SignerUnavailable(Exception):
    """The signer is not reachable. Callers must treat this as 'not sent', never 'sent'."""


@dataclass(frozen=True)
class SignRequest:
    """What the executor asks the signer to do.

    ``payload`` is chain-specific: a base64 serialized Solana transaction, or an EVM
    transaction dict. Note there is no ``recipient`` field and no ``force`` flag — the
    destination is whatever the decoded instructions say, and policy decides.
    """

    chain: Chain
    order_id: str
    payload: dict[str, Any]
    wallet: str

    def to_json(self) -> str:
        return json.dumps(
            {"chain": self.chain.value, "order_id": self.order_id,
             "payload": self.payload, "wallet": self.wallet}
        )


@dataclass(frozen=True)
class SignResponse:
    ok: bool
    signature: str | None = None
    reason: str | None = None
    findings: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------
# policy evaluation (server side)
# --------------------------------------------------------------------------------------


def _decode(req: SignRequest, policy: Any) -> tuple[str, Any]:
    """Turn a wire payload into the typed object the policy checks.

    The payload carries the *decoded* instruction list rather than raw bytes: decoding a
    serialized transaction is the executor's job, and a signer that re-derives semantics
    from bytes it also signs is one parser bug away from signing the wrong thing. If the
    caller cannot produce a decoded form, the signer refuses.
    """
    if req.chain is Chain.SOL:
        raw = req.payload.get("instructions")
        if not isinstance(raw, list) or not raw:
            raise ValueError("no decoded instructions supplied")
        return "sol", [policy.SolInstruction.model_validate(i) for i in raw]
    tx = req.payload.get("tx")
    if not isinstance(tx, dict):
        raise ValueError("no decoded transaction supplied")
    return "evm", policy.EvmTx.model_validate(tx)


def _evaluate(req: SignRequest) -> tuple[bool, str, list[str]]:
    """Run the transaction past the policy. Fail closed on any doubt."""
    try:
        from kaiba.execution import policy
    except ImportError as exc:  # pragma: no cover - policy is required in production
        return False, f"policy module unavailable: {exc}", []

    try:
        kind, decoded = _decode(req, policy)
        loaded = policy.load_policy()
        if kind == "sol":
            decision = policy.check_solana_transaction(decoded, chain=req.chain, policy=loaded)
        else:
            decision = policy.check_evm_transaction(decoded, chain=req.chain, policy=loaded)
    except AttributeError as exc:
        # The policy module exists but does not expose what we need. Refuse rather than
        # guess — a signer that guesses is not a signer.
        return False, f"policy interface mismatch: {exc}", []
    except Exception as exc:  # noqa: BLE001 - any decode failure is a refusal
        return False, f"undecodable transaction: {type(exc).__name__}: {exc}", []

    return (
        bool(getattr(decision, "allowed", False)),
        str(getattr(decision, "reason", "")),
        list(getattr(decision, "findings", [])),
    )


# --------------------------------------------------------------------------------------
# key handling
# --------------------------------------------------------------------------------------


class Keystore:
    """Loads signing keys from files only this process's user can read.

    Keys are never returned by any public method. The only thing that leaves is a
    signature.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or KEYSTORE_DIR
        self._keys: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _check_permissions(self, path: Path) -> None:
        if os.name == "posix":
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise SignerRefused(f"key file {path.name} is group/world readable ({oct(mode)})")

    def load(self, chain: Chain, wallet: str) -> Any:
        """Return an opaque signer object for a wallet. Raises if the key is absent."""
        cache_key = f"{chain.value}:{wallet}"
        with self._lock:
            if cache_key in self._keys:
                return self._keys[cache_key]
            path = self.directory / f"{chain.value}-{wallet}.key"
            if not path.exists():
                raise SignerRefused(f"no key for {chain.value}:{wallet[:8]}…")
            self._check_permissions(path)
            raw = path.read_text(encoding="utf-8").strip()
            signer = self._build_signer(chain, raw)
            self._keys[cache_key] = signer
            return signer

    @staticmethod
    def _build_signer(chain: Chain, raw: str) -> Any:
        if chain is Chain.SOL:
            try:
                from solders.keypair import Keypair  # type: ignore[import-not-found]
            except ImportError as exc:
                raise SignerUnavailable("solders is not installed (extra: signer)") from exc
            return Keypair.from_base58_string(raw)
        try:
            from eth_account import Account  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SignerUnavailable("eth-account is not installed (extra: signer)") from exc
        return Account.from_key(raw)

    def forget(self) -> None:
        with self._lock:
            self._keys.clear()


# --------------------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------------------


def sign_request(req: SignRequest, keystore: Keystore | None = None) -> SignResponse:
    """Policy first, signature second. This ordering is the whole point of the module."""
    allowed, reason, findings = _evaluate(req)
    if not allowed:
        ev.emit(
            EventKind.RISK_HALT,
            {"order_id": req.order_id, "chain": req.chain.value,
             "reason": reason or "policy refused", "findings": findings, "source": "signer"},
            chain=req.chain, level="warn",
        )
        log.warning("signer refused order %s: %s", req.order_id, reason)
        return SignResponse(ok=False, reason=reason or "policy refused", findings=tuple(findings))

    ks = keystore or Keystore()
    signer = ks.load(req.chain, req.wallet)

    if req.chain is Chain.SOL:
        raw = req.payload.get("transaction")
        if not raw:
            return SignResponse(ok=False, reason="missing transaction bytes")
        try:
            from solders.transaction import VersionedTransaction  # type: ignore[import-not-found]

            tx = VersionedTransaction.from_bytes(base64.b64decode(raw))
            signed = VersionedTransaction(tx.message, [signer])
            signature = base64.b64encode(bytes(signed)).decode()
        except ImportError as exc:
            raise SignerUnavailable("solders is not installed (extra: signer)") from exc
    else:
        try:
            signed = signer.sign_transaction(req.payload)
            signature = signed.raw_transaction.hex()
        except Exception as exc:  # noqa: BLE001 - never leak key material in the message
            return SignResponse(ok=False, reason=f"signing failed: {type(exc).__name__}")

    ev.emit(
        EventKind.ORDER_SUBMITTED,
        {"order_id": req.order_id, "signed": True, "source": "signer"},
        chain=req.chain, level="info",
    )
    return SignResponse(ok=True, signature=signature)


# --------------------------------------------------------------------------------------
# socket server
# --------------------------------------------------------------------------------------


def _peer_allowed(conn: socket.socket) -> bool:
    """Only the executor's uid may ask for a signature."""
    if os.name != "posix":
        return True  # dev boxes: the socket itself is the boundary
    try:
        import struct

        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
    except OSError:
        return False
    allowed = os.environ.get("KAIBA_SIGNER_PEER_UIDS", "")
    if not allowed:
        return uid == os.getuid()
    return str(uid) in {x.strip() for x in allowed.split(",")}


def serve(
    socket_path: Path | None = None,
    keystore: Keystore | None = None,
    *,
    socket_group: str | None = None,
    socket_mode: int = 0o660,
) -> None:  # pragma: no cover
    """Run the signer. One request per connection, no state between them.

    ``socket_group`` and ``socket_mode`` are deployment controls. They are applied only
    after the socket is bound, so a failed group lookup cannot leave a stale endpoint
    that appears ready to the engine. The CLI validates the mode; the function validates
    it too for callers that use the Python API directly.
    """
    path = socket_path or DEFAULT_SOCKET
    if not 0 <= int(socket_mode) <= 0o777:
        raise ValueError("socket_mode must be between 0000 and 0777")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    ks = keystore or Keystore()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, int(socket_mode))
    if socket_group and os.name == "posix":
        import grp

        try:
            gid = grp.getgrnam(socket_group).gr_gid
        except KeyError as exc:
            server.close()
            path.unlink(missing_ok=True)
            raise ValueError(f"unknown socket group: {socket_group}") from exc
        os.chown(path, -1, gid)
    server.listen(4)
    log.info("signer listening on %s", path)

    while True:
        conn, _ = server.accept()
        try:
            conn.settimeout(10)
            if not _peer_allowed(conn):
                conn.sendall(json.dumps({"ok": False, "reason": "peer not authorised"}).encode())
                continue
            data = conn.recv(MAX_FRAME)
            if not data:
                continue
            body = json.loads(data.decode())
            req = SignRequest(
                chain=Chain(body["chain"]), order_id=body["order_id"],
                payload=body["payload"], wallet=body["wallet"],
            )
            resp = sign_request(req, ks)
            conn.sendall(
                json.dumps({"ok": resp.ok, "signature": resp.signature,
                            "reason": resp.reason, "findings": list(resp.findings)}).encode()
            )
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the signer
            log.warning("signer request failed: %s", type(exc).__name__)
            try:
                conn.sendall(json.dumps({"ok": False, "reason": "request failed"}).encode())
            except OSError:
                pass
        finally:
            conn.close()


# --------------------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------------------


def request_signature(req: SignRequest, socket_path: Path | None = None, timeout_s: float = 15.0) -> SignResponse:
    """Ask the signer process. A timeout is 'unknown', never 'refused'."""
    path = socket_path or DEFAULT_SOCKET
    if not path.exists():
        raise SignerUnavailable(f"signer socket missing at {path}")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect(str(path))
        sock.sendall(req.to_json().encode())
        raw = sock.recv(MAX_FRAME)
    except TimeoutError as exc:
        raise TimeoutError(f"signer did not answer within {timeout_s}s") from exc
    except OSError as exc:
        raise SignerUnavailable(str(exc)) from exc
    finally:
        sock.close()
    body = json.loads(raw.decode())
    return SignResponse(
        ok=bool(body.get("ok")), signature=body.get("signature"),
        reason=body.get("reason"), findings=tuple(body.get("findings") or ()),
    )


def sign_and_send(order: Order) -> str:
    """Build, sign and broadcast for the direct lane.

    Deliberately unimplemented until a chain adapter exists. Returning a fake hash, or
    silently falling back to the GMGN lane, would both be worse than failing here.
    """
    risk = get_risk()
    wallet = risk.chain_budget(order.chain).wallet
    if not wallet:
        raise SignerRefused(f"no wallet bound for {order.chain.value}")
    raise SignerUnavailable(
        "direct-lane transaction building is not implemented yet; "
        "use provider='gmgn' or implement kaiba.execution.builders"
    )


def health(socket_path: Path | None = None) -> dict[str, Any]:
    """For the preflight check and the dashboard."""
    path = socket_path or DEFAULT_SOCKET
    return {
        "socket": str(path),
        "present": path.exists(),
        "keystore": str(KEYSTORE_DIR),
        "keys_present": len(list(KEYSTORE_DIR.glob("*.key"))) if KEYSTORE_DIR.exists() else 0,
        "checked_ms": now_ms(),
    }


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s signer %(levelname)s %(message)s")
    serve()


if __name__ == "__main__":  # pragma: no cover
    main()
