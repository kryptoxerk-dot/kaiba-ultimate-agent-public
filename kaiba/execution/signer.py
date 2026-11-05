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

**What is checked is what is signed** (fixed 2026-10-02; design review
``docs/research/mint-farm-design-20261002.md``). Until then the policy judged a
caller-supplied *description* (decoded instructions / a tx dict) while the key signed
caller-supplied *bytes* (a serialized transaction / the whole payload): two inputs that
could disagree, and the one that moved value was the one nobody checked. Now:

* Solana: the signer decodes the serialized transaction it will sign
  (:mod:`kaiba.execution.txwire`). Instruction accounts and their signer/writable flags
  come from the message header, never from the caller. A caller *may* also send its own
  decoded ``instructions``; if it does, they must equal what the bytes say, or the request
  is refused. The signature covers exactly the judged message bytes.
* EVM: the request is one transaction dict. The signer canonicalises it (closed field set,
  chain id required and equal to the chain's), judges that canonical dict, and hands a copy
  of the same dict to the signing library. It then decodes the signed bytes and refuses to
  release a signature whose ``to``/``value``/``data``/``chainId``/nonce/gas/fees differ
  from what was judged.
* A request id is single-use. A second request under an id already signed is refused, and
  the ledger survives a restart when ``KAIBA_SIGNER_STATE`` is set.

Key material never leaves this module: keys are loaded from files readable only by the
signer user, held in memory, and never returned, logged or included in an error. Keys are
stored as plain text at 0600 inside a 0700 directory; nothing in this module encrypts
them (``KAIBA_SIGNER_PASSPHRASE`` is read by nothing).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import base58

from kaiba.core import events as ev
from kaiba.core.config import USER_CONFIG_DIR, get_risk
from kaiba.core.schemas import CHAIN_IDS, EVM_CHAINS, Chain, EventKind, Order, looks_evm, looks_solana, now_ms
from kaiba.execution import txwire

log = logging.getLogger(__name__)

#: The environment names this module reads. ``deploy/systemd/kaiba-signer.service`` must
#: set exactly these (``tests/test_signer_unit_env.py`` parses the unit and checks).
ENV_SOCKET = "KAIBA_SIGNER_SOCKET"
ENV_KEYSTORE = "KAIBA_KEYSTORE_DIR"
ENV_STATE = "KAIBA_SIGNER_STATE"
ENV_PEER_UIDS = "KAIBA_SIGNER_PEER_UIDS"

DEFAULT_SOCKET = Path(os.environ.get("KAIBA_SIGNER_SOCKET", "/run/kaiba/signer.sock"))
KEYSTORE_DIR = Path(os.environ.get("KAIBA_KEYSTORE_DIR", USER_CONFIG_DIR / "keys"))
MAX_FRAME = 256 * 1024

#: Key families. One EVM key serves every EVM chain, so EVM keys are filed by family.
KEY_FAMILIES = ("evm", "sol")

#: secp256k1 group order; an EVM private key must lie in [1, N).
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class SignerRefused(Exception):
    """The signer declined. The reason is safe to show; it never contains key material."""


class SignerUnavailable(Exception):
    """The signer is not reachable. Callers must treat this as 'not sent', never 'sent'."""


class KeyFormatError(ValueError):
    """A key could not be parsed. The message never echoes the input."""


class _Refuse(Exception):
    """A request-level refusal with a stable reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _SignedMismatch(SignerRefused):
    """What came out of signing is not what the policy judged. Nothing is released."""


@dataclass(frozen=True)
class SignRequest:
    """What the executor asks the signer to do.

    ``payload`` is chain-specific and closed:

    * Solana: ``{"transaction": <base64 serialized transaction>}``, optionally with the
      caller's own decoded ``"instructions"``, which must then match the bytes.
    * EVM: ``{"tx": {chainId, nonce, to, value, data, gas, maxFeePerGas,
      maxPriorityFeePerGas}}`` (or ``gasPrice`` instead of the two fee fields).

    Any other key is refused. Note there is no ``recipient`` field and no ``force`` flag —
    the destination is whatever the decoded transaction says, and policy decides.
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


@dataclass(frozen=True)
class _Checked:
    """The object the policy judged, which is also the object that will be signed.

    Holding both in one value is the point: there is no second input to diverge from.
    """

    chain: Chain
    kind: str  # "sol" | "evm"
    view: Any  # what the policy saw: instruction dicts / EvmTx fields
    sol: txwire.SolTransaction | None
    evm: Mapping[str, Any] | None
    digest: str


# --------------------------------------------------------------------------------------
# decoding: derive the policy's view from exactly what will be signed
# --------------------------------------------------------------------------------------

_SOL_PAYLOAD_KEYS = frozenset({"transaction", "instructions"})
_EVM_PAYLOAD_KEYS = frozenset({"tx"})

_EVM_COMMON_FIELDS = ("chainId", "nonce", "to", "value", "data", "gas")
_EVM_1559_FIELDS = ("maxFeePerGas", "maxPriorityFeePerGas")
_EVM_LEGACY_FIELDS = ("gasPrice",)
_EVM_OPTIONAL_FIELDS = ("type", "accessList")
_EVM_ALL_FIELDS = frozenset(
    _EVM_COMMON_FIELDS + _EVM_1559_FIELDS + _EVM_LEGACY_FIELDS + _EVM_OPTIONAL_FIELDS
)


def _check_payload_keys(payload: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _Refuse("payload_not_an_object")
    extra = sorted(str(k) for k in payload if k not in allowed)
    if extra:
        # A second description beside the thing signed is exactly the gap being closed.
        raise _Refuse(f"payload_keys_unexpected:{','.join(extra)}")
    return payload


def _assert_description_matches(desc: Any, views: list[dict[str, Any]], policy: Any) -> None:
    """The caller's decoded instructions, if sent, must say what the bytes say."""
    if not isinstance(desc, list):
        raise _Refuse("request_parts_disagree:instructions_not_a_list")
    if len(desc) != len(views):
        raise _Refuse(f"request_parts_disagree:instruction_count:{len(desc)}!={len(views)}")
    model = policy.SolInstruction  # AttributeError here is an interface mismatch
    for i, (claimed_raw, actual_raw) in enumerate(zip(desc, views, strict=True)):
        try:
            claimed = model.model_validate(claimed_raw)
        except Exception:  # noqa: BLE001 - a description we cannot read cannot match
            raise _Refuse(f"request_parts_disagree:ix{i}:undecodable") from None
        actual = model.model_validate(actual_raw)
        for field in ("program_id", "accounts", "data"):
            if getattr(claimed, field) != getattr(actual, field):
                raise _Refuse(f"request_parts_disagree:ix{i}.{field}")
        for field in ("is_signer", "is_writable", "resolved_from_alt"):
            if field in claimed.model_fields_set and getattr(claimed, field) != getattr(actual, field):
                raise _Refuse(f"request_parts_disagree:ix{i}.{field}")


def _prepare_sol(req: SignRequest, policy: Any) -> _Checked:
    payload = _check_payload_keys(req.payload, _SOL_PAYLOAD_KEYS)
    encoded = payload.get("transaction")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("no serialized transaction supplied")
    tx = txwire.parse_sol_transaction(base64.b64decode(encoded, validate=True))
    views = txwire.sol_instruction_views(tx.message)
    if "instructions" in payload:
        _assert_description_matches(payload["instructions"], views, policy)
    return _Checked(
        chain=req.chain, kind="sol", view=tuple(views), sol=tx, evm=None,
        digest=hashlib.sha256(tx.message_bytes).hexdigest(),
    )


def _uint(value: Any, field: str, bits: int = 256) -> int:
    if isinstance(value, bool):
        raise _Refuse(f"tx_field_invalid:{field}")
    if isinstance(value, int):
        out = value
    elif isinstance(value, str) and value[:2].lower() == "0x" and len(value) > 2:
        try:
            out = int(value[2:], 16)
        except ValueError:
            raise _Refuse(f"tx_field_invalid:{field}") from None
    else:
        raise _Refuse(f"tx_field_invalid:{field}")
    if not 0 <= out < (1 << bits):
        raise _Refuse(f"tx_field_out_of_range:{field}")
    return out


def _eip55(address: str, keccak: Callable[[bytes], bytes]) -> str:
    lower = address.lower()[2:]
    digest = keccak(lower.encode("ascii")).hex()
    return "0x" + "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(lower))


def _canonical_evm_tx(raw: Any, chain: Chain, keccak: Callable[[bytes], bytes]) -> dict[str, Any]:
    """One spelling per field, chain id required. The result is both judged and signed."""
    if not isinstance(raw, dict):
        raise ValueError("no transaction supplied")
    unknown = sorted(str(k) for k in raw if k not in _EVM_ALL_FIELDS)
    if unknown:
        raise _Refuse(f"tx_fields_unexpected:{','.join(unknown)}")
    legacy = "gasPrice" in raw
    if legacy and any(k in raw for k in _EVM_1559_FIELDS):
        raise _Refuse("tx_fee_fields_mixed")
    fee_fields = _EVM_LEGACY_FIELDS if legacy else _EVM_1559_FIELDS
    if "chainId" not in raw:
        # policy.check_evm_transaction skips its chain check when chain_id is None; the
        # signer does not sign a transaction whose chain it cannot name.
        raise _Refuse("chain_id_missing")
    missing = [k for k in _EVM_COMMON_FIELDS + fee_fields if k not in raw]
    if missing:
        raise _Refuse(f"tx_fields_missing:{','.join(missing)}")
    if "type" in raw and _uint(raw["type"], "type", 8) != (0 if legacy else 2):
        raise _Refuse("tx_type_inconsistent")
    if "accessList" in raw and raw["accessList"] not in ([], ()):
        raise _Refuse("access_list_not_empty")

    out: dict[str, Any] = {}
    for field in ("chainId", "value", "gas", *fee_fields):
        out[field] = _uint(raw[field], field)
    out["nonce"] = _uint(raw["nonce"], "nonce", 64)

    to = raw["to"]
    if not isinstance(to, str) or not looks_evm(to) or to != to.strip():
        raise _Refuse("to_invalid")
    hexpart = to[2:]
    checksummed = _eip55(to, keccak)
    if hexpart not in (hexpart.lower(), hexpart.upper()) and to != checksummed:
        raise _Refuse("to_checksum_invalid")
    out["to"] = checksummed

    data = raw["data"]
    if not isinstance(data, str) or data[:2].lower() != "0x" or len(data) % 2:
        raise _Refuse("tx_field_invalid:data")
    try:
        bytes.fromhex(data[2:])
    except ValueError:
        raise _Refuse("tx_field_invalid:data") from None
    out["data"] = "0x" + data[2:].lower()

    expected = CHAIN_IDS.get(chain)
    if expected is None:
        raise _Refuse(f"chain_id_unknown_for:{chain.value}")
    if out["chainId"] != expected:
        raise _Refuse(f"chain_id_mismatch:{out['chainId']}!={expected}")
    return out


def _prepare_evm(req: SignRequest, policy: Any) -> _Checked:
    if req.chain not in EVM_CHAINS:
        raise _Refuse(f"chain_not_supported:{req.chain.value}")
    payload = _check_payload_keys(req.payload, _EVM_PAYLOAD_KEYS)
    canonical = _canonical_evm_tx(payload.get("tx"), req.chain, policy.keccak256)
    view = {
        "to": canonical["to"], "value": canonical["value"],
        "data": canonical["data"], "chain_id": canonical["chainId"],
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    return _Checked(
        chain=req.chain, kind="evm", view=MappingProxyType(view), sol=None,
        evm=MappingProxyType(canonical), digest=digest,
    )


# --------------------------------------------------------------------------------------
# policy evaluation (server side)
# --------------------------------------------------------------------------------------


def _judge(req: SignRequest) -> tuple[_Checked | None, bool, str, list[str]]:
    """Decode once, judge that, and return the judged object for signing.

    Fail closed on any doubt: a decode error, a disagreement between the request's parts,
    or a policy module that does not expose what we need is a refusal.
    """
    try:
        from kaiba.execution import policy
    except ImportError as exc:  # pragma: no cover - policy is required in production
        return None, False, f"policy module unavailable: {exc}", []

    try:
        checked = _prepare_sol(req, policy) if req.chain is Chain.SOL else _prepare_evm(req, policy)
        loaded = policy.load_policy()
        if checked.kind == "sol":
            instructions = [policy.SolInstruction.model_validate(v) for v in checked.view]
            decision = policy.check_solana_transaction(instructions, chain=req.chain, policy=loaded)
        else:
            tx = policy.EvmTx.model_validate(dict(checked.view))
            decision = policy.check_evm_transaction(tx, chain=req.chain, policy=loaded)
    except _Refuse as exc:
        return None, False, exc.reason, []
    except AttributeError as exc:
        # The policy module exists but does not expose what we need. Refuse rather than
        # guess — a signer that guesses is not a signer.
        return None, False, f"policy interface mismatch: {exc}", []
    except Exception as exc:  # noqa: BLE001 - any decode failure is a refusal
        return None, False, f"undecodable transaction: {type(exc).__name__}: {exc}", []

    allowed = getattr(decision, "allowed", False) is True
    return (
        checked if allowed else None,
        allowed,
        str(getattr(decision, "reason", "")),
        list(getattr(decision, "findings", [])),
    )


def _evaluate(req: SignRequest) -> tuple[bool, str, list[str]]:
    """Run the request past the policy without signing. ``(allowed, reason, findings)``."""
    _checked, allowed, reason, findings = _judge(req)
    return allowed, reason, findings


# --------------------------------------------------------------------------------------
# key handling
# --------------------------------------------------------------------------------------


def _family(chain: Chain) -> str:
    if chain is Chain.SOL:
        return "sol"
    if chain in EVM_CHAINS:
        return "evm"
    raise SignerRefused(f"no key family for chain {chain.value}")


def _family_name(name: str) -> str:
    family = str(name).strip().lower()
    if family not in KEY_FAMILIES:
        raise ValueError(f"unknown key family {name!r}; expected one of {', '.join(KEY_FAMILIES)}")
    return family


def _normalise_wallet(family: str, wallet: str) -> str:
    """A wallet string is a filename component, so it must be an address and nothing else."""
    if not isinstance(wallet, str):
        raise SignerRefused(f"wallet is not a valid {family} address")
    if family == "sol":
        try:
            ok = looks_solana(wallet) and wallet == wallet.strip() and len(base58.b58decode(wallet)) == 32
        except ValueError:
            ok = False
        if not ok:
            raise SignerRefused("wallet is not a valid sol address")
        return wallet
    if not looks_evm(wallet) or wallet != wallet.strip():
        raise SignerRefused("wallet is not a valid evm address")
    return wallet.lower()


def _key_filename(family: str, address: str) -> str:
    return f"{family}-{_normalise_wallet(family, address)}.key"


def _parse_sol_secret(text: str) -> bytes:
    """A 64-byte keypair as base58 (Phantom export) or a JSON array (solana-keygen)."""
    t = text.strip()
    try:
        if t.startswith("["):
            arr = json.loads(t)
            if not isinstance(arr, list) or not all(isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 255 for b in arr):
                raise ValueError
            raw = bytes(arr)
        else:
            raw = base58.b58decode(t)
    except Exception:  # noqa: BLE001 - never chain an error that may quote the input
        raise KeyFormatError(
            "not a valid sol private key (expected a base58 64-byte keypair or a 64-number JSON array)"
        ) from None
    if len(raw) != 64:
        raise KeyFormatError("not a valid sol private key (keypair must be 64 bytes)")
    _sol_address(raw)  # the public half must match the seed
    return raw


def _parse_evm_secret(text: str) -> int:
    t = text.strip()
    if t[:2] in ("0x", "0X"):
        t = t[2:]
    if len(t) != 64 or any(c not in "0123456789abcdefABCDEF" for c in t):
        raise KeyFormatError("not a valid evm private key (expected 32 bytes of hex)")
    k = int(t, 16)
    if not 1 <= k < _SECP256K1_N:
        raise KeyFormatError("not a valid evm private key (out of range for secp256k1)")
    return k


def _sol_address(keypair: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(keypair) != 64:
        raise KeyFormatError("not a valid sol private key (keypair must be 64 bytes)")
    derived = Ed25519PrivateKey.from_private_bytes(keypair[:32]).public_key().public_bytes_raw()
    if derived != keypair[32:]:
        raise KeyFormatError("not a valid sol private key (public half does not match the seed)")
    return txwire.b58(derived)


def _evm_address(k: int) -> str:
    from cryptography.hazmat.primitives.asymmetric import ec

    from kaiba.execution.policy import keccak256

    if not 1 <= k < _SECP256K1_N:
        raise KeyFormatError("not a valid evm private key (out of range for secp256k1)")
    numbers = ec.derive_private_key(k, ec.SECP256K1()).public_key().public_numbers()
    point = numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return _eip55("0x" + keccak256(point)[-20:].hex(), keccak256)


class _SolKey:
    """An ed25519 signing key. Exposes the address and the ability to sign; nothing else."""

    __slots__ = ("_private", "_public", "address")

    def __init__(self, keypair: bytes) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self.address = _sol_address(keypair)
        self._private = Ed25519PrivateKey.from_private_bytes(keypair[:32])
        self._public = self._private.public_key()

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def verify(self, signature: bytes, message: bytes) -> bool:
        from cryptography.exceptions import InvalidSignature

        try:
            self._public.verify(signature, message)
        except InvalidSignature:
            return False
        return True

    def __repr__(self) -> str:
        return f"<sol signing key {self.address}>"


class _EvmKey:
    """A secp256k1 signing key via eth-account. Returns the raw signed transaction bytes."""

    __slots__ = ("_account", "address")

    def __init__(self, secret: str) -> None:
        try:
            from eth_account import Account  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SignerUnavailable("eth-account is not installed (extra: signer)") from exc
        self._account = Account.from_key("0x" + _parse_evm_secret(secret).to_bytes(32, "big").hex())
        self.address = str(self._account.address)

    def sign_transaction(self, tx: dict[str, Any]) -> bytes:
        signed = self._account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:  # eth-account < 0.13
            raw = signed.rawTransaction
        return bytes(raw)

    def __repr__(self) -> str:
        return f"<evm signing key {self.address}>"


class Keystore:
    """Loads signing keys from files only this process's user can read.

    Keys are never returned by any public method. The only thing that leaves is a
    signature. Files are ``sol-<pubkey>.key`` and ``evm-<lowercase address>.key``; the
    key inside must derive the address in its name, or it is refused.
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
        family = _family(chain)
        address = _normalise_wallet(family, wallet)
        cache_key = f"{family}:{address}"
        with self._lock:
            if cache_key in self._keys:
                return self._keys[cache_key]
            path = self.directory / _key_filename(family, address)
            if not path.exists():
                raise SignerRefused(f"no key for {chain.value}:{address[:8]}…")
            self._check_permissions(path)
            raw = path.read_text(encoding="utf-8").strip()
            signer = self._build_signer(chain, raw)
            derived = signer.address if family == "sol" else signer.address.lower()
            if derived != address:
                raise SignerRefused(f"key file for {address[:8]}… holds a different wallet")
            self._keys[cache_key] = signer
            return signer

    @staticmethod
    def _build_signer(chain: Chain, raw: str) -> Any:
        try:
            if chain is Chain.SOL:
                return _SolKey(_parse_sol_secret(raw))
            return _EvmKey(raw)
        except KeyFormatError:
            raise SignerRefused(f"key file for {chain.value} is malformed") from None

    def forget(self) -> None:
        with self._lock:
            self._keys.clear()


# --------------------------------------------------------------------------------------
# key creation: keygen and import (operator CLI only; never reachable over the socket)
# --------------------------------------------------------------------------------------


def _ensure_keystore_dir(directory: Path) -> None:
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
        if os.name == "posix":
            os.chmod(directory, 0o700)
    if not directory.is_dir():
        raise SignerRefused(f"keystore path {directory} is not a directory")
    if os.name == "posix":
        mode = directory.stat().st_mode & 0o777
        if mode & 0o077:
            raise SignerRefused(
                f"keystore directory {directory} is group/world accessible ({oct(mode)}); chmod 700 it"
            )


def _store_new_key(directory: Path, family: str, address: str, text: str) -> Path:
    """Create the key file 0600 with O_EXCL. An existing key is never overwritten."""
    _ensure_keystore_dir(directory)
    path = directory / _key_filename(family, address)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SignerRefused(f"refusing to overwrite the existing key for {address}") from None
    written = False
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # umask can only remove bits; this pins the mode regardless
        data = (text + "\n").encode("ascii")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        written = True
    finally:
        os.close(fd)
        if not written:
            path.unlink(missing_ok=True)
    log.info("stored a new %s key for %s", family, address)
    return path


def _new_sol_keypair() -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw() + private.public_key().public_bytes_raw()


def _new_evm_secret() -> int:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256K1()).private_numbers().private_value


def keygen(family: str, directory: Path) -> str:
    """Generate a key inside ``directory`` (0600, never overwriting). Returns ONLY the address."""
    fam = _family_name(family)
    if fam == "sol":
        keypair = _new_sol_keypair()
        address = _sol_address(keypair)
        text = txwire.b58(keypair)
    else:
        k = _new_evm_secret()
        address = _evm_address(k)
        text = "0x" + k.to_bytes(32, "big").hex()
    _store_new_key(directory, fam, address, text)
    return address


def import_secret(family: str, secret: str, directory: Path) -> str:
    """Store an existing private key (read by the caller from a hidden prompt or stdin).

    Returns ONLY the address. The secret is never logged, echoed or quoted in an error.
    """
    fam = _family_name(family)
    if fam == "sol":
        keypair = _parse_sol_secret(secret)
        address = _sol_address(keypair)
        text = txwire.b58(keypair)
    else:
        k = _parse_evm_secret(secret)
        address = _evm_address(k)
        text = "0x" + k.to_bytes(32, "big").hex()
    _store_new_key(directory, fam, address, text)
    return address


# --------------------------------------------------------------------------------------
# replay: a request id is single-use
# --------------------------------------------------------------------------------------


class _RequestLedger:
    """Order ids already signed. Append-only file when a state dir is configured."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._seen: set[str] = set()
        self._loaded = False
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    # A corrupt ledger raises here, and the request is refused: fail closed.
                    self._seen.add(str(json.loads(line)["order_id"]))
        self._loaded = True

    def claim(self, order_id: str, chain: Chain, digest: str) -> None:
        if not isinstance(order_id, str) or not order_id:
            raise SignerRefused("request_id_missing")
        with self._lock:
            if not self._loaded:
                self._load()
            if order_id in self._seen:
                raise SignerRefused(f"request_id_replayed:{order_id}")
            if self.path is not None:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                line = json.dumps({"order_id": order_id, "chain": chain.value,
                                   "digest": digest, "ts_ms": now_ms()}) + "\n"
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self._seen.add(order_id)


_LEDGERS: dict[str, _RequestLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def _ledger() -> _RequestLedger:
    state = os.environ.get("KAIBA_SIGNER_STATE", "")
    with _LEDGERS_LOCK:
        if state not in _LEDGERS:
            _LEDGERS[state] = _RequestLedger(Path(state) / "signed-requests.jsonl" if state else None)
        return _LEDGERS[state]


# --------------------------------------------------------------------------------------
# signing
# --------------------------------------------------------------------------------------


def _emit(kind: EventKind, payload: dict[str, Any], chain: Chain, level: str) -> None:
    """Audit, but a storage failure never changes a decision (the signer has no DB on the box)."""
    try:
        ev.emit(kind, payload, chain=chain, level=level)
    except Exception as exc:  # noqa: BLE001
        log.warning("signer audit emit failed: %s", type(exc).__name__)


def _refusal(req: SignRequest, reason: str, findings: list[str] | tuple[str, ...] = ()) -> SignResponse:
    _emit(
        EventKind.RISK_HALT,
        {"order_id": req.order_id, "chain": req.chain.value,
         "reason": reason, "findings": list(findings), "source": "signer"},
        req.chain, "warn",
    )
    log.warning("signer refused order %s: %s", req.order_id, reason)
    return SignResponse(ok=False, reason=reason, findings=tuple(findings))


def _sign_sol(checked: _Checked, key: Any) -> str:
    tx = checked.sol
    assert tx is not None
    signers = tx.message.signer_keys
    index = signers.index(key.address)
    signatures = list(tx.signatures)
    signatures[index] = key.sign(tx.message_bytes)
    out = txwire.serialize_sol_transaction(signatures, tx.message_bytes)
    # signed == checked: the output must carry the judged message, and our signature on it.
    again = txwire.parse_sol_transaction(out)
    if again.message_bytes != tx.message_bytes:
        raise _SignedMismatch("signed_differs_from_checked:message")
    if not key.verify(again.signatures[index], again.message_bytes):
        raise _SignedMismatch("signed_differs_from_checked:signature")
    return base64.b64encode(out).decode()


def _sign_evm(checked: _Checked, key: Any) -> str:
    canonical = dict(checked.evm or {})
    raw = key.sign_transaction(dict(canonical))  # a copy: the library cannot edit ours
    if not isinstance(raw, (bytes, bytearray)):
        raise _SignedMismatch("signed_differs_from_checked:not_bytes")
    try:
        decoded = txwire.decode_signed_evm_transaction(bytes(raw))
    except txwire.WireError as exc:
        raise _SignedMismatch(f"signed_differs_from_checked:undecodable:{exc}") from None
    diffs: list[str] = []
    if decoded["type"] != (0 if "gasPrice" in canonical else 2):
        diffs.append("type")
    for field, want in canonical.items():
        got = decoded.get(field)
        if isinstance(want, str):
            want = want.lower()
            got = got.lower() if isinstance(got, str) else got
        if got != want:
            diffs.append(field)
    if decoded.get("accessList"):
        diffs.append("accessList")
    if diffs:
        raise _SignedMismatch(f"signed_differs_from_checked:{','.join(diffs)}")
    return "0x" + bytes(raw).hex()


def sign_request(req: SignRequest, keystore: Keystore | None = None) -> SignResponse:
    """Policy first, signature second — over the same object. This is the whole module."""
    checked, allowed, reason, findings = _judge(req)
    if not allowed or checked is None:
        return _refusal(req, reason or "policy refused", findings)

    ks = keystore or Keystore()
    try:
        key = ks.load(req.chain, req.wallet)
        if checked.kind == "sol" and key.address not in checked.sol.message.signer_keys:
            raise SignerRefused("wallet_not_a_required_signer")
        _ledger().claim(req.order_id, req.chain, checked.digest)
        signature = _sign_sol(checked, key) if checked.kind == "sol" else _sign_evm(checked, key)
    except SignerRefused as exc:
        return _refusal(req, str(exc))
    except SignerUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak key material in the message
        return SignResponse(ok=False, reason=f"signing failed: {type(exc).__name__}")

    _emit(
        EventKind.ORDER_SUBMITTED,
        {"order_id": req.order_id, "signed": True, "digest": checked.digest, "source": "signer"},
        req.chain, "info",
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
