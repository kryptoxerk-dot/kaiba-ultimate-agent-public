"""Pinned BAW client; closed command surface, no shell or secret-bearing errors.

Codex BAW-AUTOMINT-INTEGRATION-20261102. Authentication is operator-only.
The remote Hermes surface must never expose this object's auth/execute methods.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

CLI_VERSION = "1.12.0"
CLI_SHA256 = "e63e2dff4a6d267d2375a0bbcdf518f10ddd04b0ffaff378c265a26f0961fe4f"
# BAW-FUNDED-MINTS-20261102: runtime-confirmed Base/Ethereum, preview-only.
# No Abstract mapping is inferred from an App balance or a network nickname.
CHAIN_IDS = {"bsc": "56", "robinhood": "4663", "sol": "CT_501", "base": "8453", "eth": "1"}
MAX_RESPONSE = 2 * 1024 * 1024


class WalletUnavailable(RuntimeError):
    """Only a stable local reason is safe to show; never stringify CLI stderr."""


def _require(ok, reason):
    if not ok:
        raise WalletUnavailable(reason)


def validate_pairing(data):
    _require(isinstance(data, dict), "pairing_shape")
    if data.get("status") == "ALREADY_CONNECTED":
        return {"status": "ALREADY_CONNECTED"}
    url, code, qr = (data.get(k) for k in ("urlForWeb", "pairingCode", "qrCodeId"))
    _require(isinstance(url, str) and len(url) <= 8192, "pairing_url")
    parts = urlsplit(url)
    _require(parts.scheme == "https" and parts.hostname is not None and
             (parts.hostname == "binance.com" or parts.hostname.endswith(".binance.com"))
             and parts.username is None and parts.password is None and parts.port in (None, 443),
             "pairing_host")
    _require(isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9 -]{1,64}", code), "pairing_code")
    _require(isinstance(qr, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", qr), "pairing_id")
    # Preserve the provider's URL and pairing code verbatim.
    return {"urlForWeb": url, "pairingCode": code, "qrCodeId": qr,
            "expireAt": data.get("expireAt")}


class BinanceWalletCLI:
    def __init__(self, *, node, script, state_dir, runner=subprocess.run):
        self.node, self.script, self.state_dir = map(Path, (node, script, state_dir))
        self.runner = runner

    def _environment(self):
        env = {k: v for k, v in os.environ.items() if k in
               ("SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "USERPROFILE",
                "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL")}
        env["BINANCE_BAW_DIR"] = str(self.state_dir)
        return env

    def _run(self, args, *, timeout=30):
        _require(self.node.is_file() and self.script.is_file(), "cli_missing")
        _require(hashlib.sha256(self.script.read_bytes()).hexdigest() == CLI_SHA256, "cli_source_changed")
        try:
            result = self.runner([str(self.node), str(self.script), *args, "--json"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                                 env=self._environment(), shell=False)
        except subprocess.TimeoutExpired as exc:
            raise WalletUnavailable("cli_timeout_reconcile_before_retry") from exc
        except OSError as exc:
            raise WalletUnavailable("cli_start_failed") from exc
        _require(len(result.stdout.encode("utf-8")) <= MAX_RESPONSE, "cli_response_size")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise WalletUnavailable("cli_response_invalid") from exc
        if (result.returncode != 0 or not isinstance(payload, dict) or
                payload.get("success") is not True or "data" not in payload):
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            suffix = f":{code}" if type(code) is int and 0 <= code <= 1_000_000_000 else ""
            raise WalletUnavailable("cli_request_unavailable" + suffix)
        return payload["data"]

    def query(self, operation):
        _require(operation in ("status", "chains", "address", "settings"), "read_operation_refused")
        return self._run(["wallet", operation])

    def signin(self):
        """Operator-only pairing; not exposed to the mint worker's HTTP client."""
        return validate_pairing(self._run(["auth", "signin"]))

    def verify(self, qr_id):
        _require(isinstance(qr_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", qr_id), "pairing_id")
        return self._run(["auth", "verify", "--qrCodeId", qr_id], timeout=320)

    def preview(self, *, chain, wallet, contract=None, calldata=None, unsigned_tx=None):
        """Simulation only. This client has no contract-call execute or send method."""
        _require(chain in CHAIN_IDS, "preview_chain_refused")
        args = ["contract-call", "preview", "--binanceChainId", CHAIN_IDS[chain], "--from", wallet]
        if chain == "sol":
            _require(isinstance(wallet, str) and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", wallet), "wallet_address")
            _require(isinstance(unsigned_tx, str) and len(unsigned_tx) <= 1800, "solana_transaction_size")
            try:
                raw = base64.b64decode(unsigned_tx, validate=True)
            except ValueError as exc:
                raise WalletUnavailable("solana_transaction_encoding") from exc
            _require(0 < len(raw) <= 1232 and contract is None and calldata is None, "solana_transaction_shape")
            args += ["--unsignedTx", unsigned_tx]
        else:
            _require(all(isinstance(a, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", a)
                         for a in (wallet, contract)), "evm_address")
            _require(isinstance(calldata, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){4,4096}", calldata), "calldata")
            _require(unsigned_tx is None, "evm_transaction_shape")
            args += ["--to", contract, "--value", "0", "--inputData", calldata]
        return self._run(args)


def connection_view(client):
    """Actual read-only capability check. Missing identity stays unavailable."""
    status = client.query("status")
    if not isinstance(status, dict) or status.get("status") != "CONNECTED":
        return {"connected": False, "minting_enabled": False, "reason": "wallet_unconnected"}
    raw_chains = client.query("chains")
    raw_addresses = client.query("address")
    settings = client.query("settings")
    _require(isinstance(raw_chains, list) and isinstance(raw_addresses, dict) and
             isinstance(raw_addresses.get("addresses"), list) and isinstance(settings, dict), "wallet_shape")
    ids = {str(r.get("binanceChainId")) for r in raw_chains if isinstance(r, dict)}
    addresses = {}
    for row in raw_addresses["addresses"]:
        _require(isinstance(row, dict), "address_shape")
        for chain, chain_id in CHAIN_IDS.items():
            if str(row.get("binanceChainId")) == chain_id:
                address = row.get("address")
                pattern = r"[1-9A-HJ-NP-Za-km-z]{32,44}" if chain == "sol" else r"0x[0-9a-fA-F]{40}"
                _require(isinstance(address, str) and re.fullmatch(pattern, address), "address_invalid")
                _require(chain not in addresses, "duplicate_chain_address")
                addresses[chain] = address
    safe_settings = {k: settings.get(k) for k in (
        "dailyLimit", "quotaLeft", "tradeAllTokens", "abnormalTxnHandling",
        "sessionExpireTime", "inactiveSignOutTime", "developerModeQuotaUsed", "devMode")}
    mode = settings.get("devMode")
    safe_settings["devMode"] = ({k: mode.get(k) for k in
                                 ("enabled", "expiresAt", "dailyLimit", "balanceExceeded")}
                                if isinstance(mode, dict) else None)
    return {"connected": True, "minting_enabled": False,
            "chains": {chain: {"supported": chain_id in ids, "wallet": addresses.get(chain)}
                       for chain, chain_id in CHAIN_IDS.items()}, "settings": safe_settings,
            "reason": "execution_policy_and_fee_bounds_not_yet_verified"}
