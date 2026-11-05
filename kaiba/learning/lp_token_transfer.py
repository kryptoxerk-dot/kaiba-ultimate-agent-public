"""Read the narrow Token-2022 mint basis supported by the LP research model."""

from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
from struct import unpack_from

from kaiba.execution.lp_tx import TOKEN_2022
from kaiba.learning.dlmm_state import public_key

U64_MAX = 2**64 - 1
ZERO_KEY = bytes(32)
FEE_CONFIG = 1
TRANSFER_HOOK = 14
METADATA_POINTER = 18
TOKEN_METADATA = 19


@dataclass(frozen=True)
class Fee:
    epoch: int = 0
    maximum_atoms: int = 0
    bps: int = 0

    def charge(self, gross: int) -> int:
        if type(gross) is not int or not 0 <= gross <= U64_MAX:
            raise ValueError("token_amount_out_of_range")
        return min(self.maximum_atoms, (gross * self.bps + 9_999) // 10_000)


@dataclass(frozen=True)
class MintTransfers:
    older: Fee = Fee()
    newer: Fee = Fee()
    hook: str = "absent"
    problem: str | None = None
    extensions: tuple[int, ...] = ()

    def fee_at(self, epoch: int) -> Fee:
        return self.newer if epoch >= self.newer.epoch else self.older


def mint_transfers(address: str, account: dict) -> MintTransfers:
    mint = public_key(address)
    if not isinstance(account, dict) or account.get("owner") != TOKEN_2022 or account.get("executable") is not False:
        raise ValueError("invalid_token2022_mint_owner")
    data = account["data"]
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64" or len(data[0]) > 14_000:
        raise ValueError("invalid_token2022_mint_encoding")
    raw = b64decode(data[0], validate=True)
    if (
        len(raw) < 82 or raw[45] != 1
        or unpack_from("<I", raw, 0)[0] not in (0, 1)
        or unpack_from("<I", raw, 46)[0] not in (0, 1)
    ):
        raise ValueError("invalid_token2022_mint_header")
    frozen = unpack_from("<I", raw, 46)[0] != 0
    if len(raw) == 82:
        return MintTransfers(problem="token_freeze_authority" if frozen else None)
    if len(raw) < 166 or len(raw) == 355 or raw[82:165] != bytes(83) or raw[165] != 1:
        raise ValueError("invalid_token2022_mint_layout")
    older = newer = Fee()
    hook = "absent"
    problem = "token_freeze_authority" if frozen else None
    seen = set()
    at = 166
    while at < len(raw):
        if not any(raw[at:]):
            break
        if len(raw) - at < 4:
            raise ValueError("truncated_mint_extension")
        kind, length = unpack_from("<HH", raw, at)
        at += 4
        end = at + length
        if not kind or kind in seen or end > len(raw):
            raise ValueError("invalid_mint_extension")
        value = raw[at:end]
        at = end
        seen.add(kind)
        if kind == FEE_CONFIG:
            if length != 108:
                raise ValueError("invalid_transfer_fee_config")
            older, newer = (Fee(*unpack_from("<QQH", value, off)) for off in (72, 90))
            if older.epoch > newer.epoch or max(older.bps, newer.bps) > 10_000:
                raise ValueError("invalid_transfer_fee_schedule")
        elif kind == TRANSFER_HOOK:
            if length != 64:
                raise ValueError("invalid_transfer_hook")
            hook = "enabled" if value[32:] != ZERO_KEY else "disabled"
        elif kind == METADATA_POINTER:
            if length != 64:
                raise ValueError("invalid_metadata_pointer")
        elif kind == TOKEN_METADATA:
            if length < 80 or value[32:64] != mint:
                raise ValueError("invalid_token_metadata_identity")
        else:
            problem = problem or "token2022_extension_unmodeled"
    if hook == "enabled":
        problem = "transfer_hook_effects_unverified"
    return MintTransfers(older, newer, hook, problem, tuple(sorted(seen)))


@dataclass(frozen=True)
class EpochSchedule:
    slots_per_epoch: int
    first_normal_epoch: int
    first_normal_slot: int

    def at_slot(self, slot: int) -> int:
        if slot < self.first_normal_slot:
            raise ValueError("pre_normal_epoch_unmodeled")
        return self.first_normal_epoch + (slot - self.first_normal_slot) // self.slots_per_epoch


def epoch_schedule(receipt: dict) -> EpochSchedule:
    request, response = receipt["request"], receipt["response"]
    if (
        request.get("jsonrpc") != "2.0" or response.get("jsonrpc") != "2.0"
        or request.get("method") != "getEpochSchedule" or request.get("params") != []
        or type(request.get("id")) is not int or request["id"] != 2
        or type(response.get("id")) is not int or response["id"] != 2 or "error" in response
    ):
        raise ValueError("invalid_epoch_schedule_envelope")
    result = response["result"]
    slots, first_epoch, first_slot = (
        result[key] for key in ("slotsPerEpoch", "firstNormalEpoch", "firstNormalSlot")
    )
    if (
        any(type(n) is not int or not 0 <= n <= U64_MAX for n in (slots, first_epoch, first_slot))
        or not slots or type(result.get("warmup")) is not bool
        or (not result["warmup"] and (first_epoch or first_slot))
        or (result["warmup"] and (not first_epoch or not first_slot))
    ):
        raise ValueError("invalid_epoch_schedule")
    return EpochSchedule(slots, first_epoch, first_slot)
