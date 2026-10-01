"""GoPlus token security — EVM ``token_security/{chain_id}`` and the Solana beta route.

Why this module exists in the shape it does:

* **Credentials are optional.** GoPlus answers keyless at a lower rate limit, and an
  ``app_key``/``app_secret`` pair only raises that limit. A safety check that silently
  stops running because a key is missing is worse than a slow one, so the keyless path is
  the default and the signed path is an upgrade.
* **GoPlus signals failure with HTTP 200.** A bad request comes back as ``{"code": 4029,
  "message": "..."}`` with a perfectly healthy status line, so :func:`_http.request_json`
  would happily cache it as a successful answer. Every response is therefore checked for
  ``code == 1`` and a non-ok body evicts its own cache entry — otherwise one hiccup would
  read as "no risks found" for the whole TTL.
* **Normalisation lives here, not in the caller.** GoPlus encodes booleans as the strings
  ``"0"``/``"1"``, taxes as fractional strings, and percentages as fractions. Those quirks
  are GoPlus's problem, so :func:`normalize_security` converts them into the shared DYOR
  property vocabulary (see :mod:`kaiba.intelligence.dyor`) where a property nobody answered
  is simply absent — never ``False`` and never ``0``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from kaiba.core.limiter import Priority
from kaiba.core.schemas import CHAIN_IDS, EVM_CHAINS, Chain, EvidenceBasis, Receipt
from kaiba.providers._http import Fetched, cache_path, get_json, post_json

log = logging.getLogger(__name__)

PROVIDER = "goplus"
BASE = "https://api.gopluslabs.io/api/v1"

#: Security facts change when an owner acts, not tick by tick; five minutes is plenty.
SECURITY_TTL_S = 300.0
#: If GoPlus is down we would rather reason about a fifteen-minute-old authority flag,
#: clearly marked STALE, than have no opinion at all.
STALE_GRACE_S = 900.0

#: EVM chain ids this repo can currently address *and* GoPlus publishes. Robinhood (4663)
#: and the chains with no id in ``CHAIN_IDS`` are not covered; asking anyway would spend a
#: request to be told nothing.
SUPPORTED_EVM_CHAIN_IDS: frozenset[int] = frozenset({1, 56, 8453})

#: Access tokens last an hour; refresh a minute early rather than race the expiry.
_TOKEN_SKEW_S = 60.0
_token_cache: tuple[str, float] | None = None

#: Owner addresses that mean "nobody can call the owner-gated functions any more". GoPlus
#: attaches this caveat to its own flags: "When the contract does not have an owner (or if
#: the owner is a black hole address) ... this function will most likely be disabled."
#: An empty string is documented as "the contract has no owner", which is the same thing.
EVM_BLACKHOLES: frozenset[str] = frozenset(
    {
        "",
        "0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead",
        "0x0000000000000000000000000000000000000001",
    }
)


# --------------------------------------------------------------------------------------
# small, total parsers — a mangled field is "unknown", never a default
# --------------------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if d.is_nan() else d


def _flag(value: Any) -> bool | None:
    """GoPlus's ``"0"``/``"1"`` strings. Anything else is genuinely unknown."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip()
    if s in {"1", "true", "True"}:
        return True
    if s in {"0", "false", "False"}:
        return False
    return None


def _status(block: Any) -> bool | None:
    """Solana blocks are ``{"status": "1", "authority": [...]}`` — read the status."""
    if isinstance(block, dict):
        return _flag(block.get("status"))
    return _flag(block)


def _unavailable(endpoint: str, note: str) -> Fetched:
    return Fetched(
        None,
        Receipt(
            provider=PROVIDER, endpoint=endpoint, basis=EvidenceBasis.UNAVAILABLE, note=note[:300]
        ),
    )


def _report(endpoint: str, note: str, conn: Any = None) -> None:
    """Announce a soft failure (a 200 with an error body) the same way _http does a hard one."""
    try:
        from kaiba.core import events as ev
        from kaiba.core.schemas import EventKind

        ev.emit(
            EventKind.PROVIDER_ERROR,
            {"provider": PROVIDER, "endpoint": endpoint, "detail": note[:300]},
            level="warn",
            dedupe_key=f"provider_error:{PROVIDER}:{endpoint}:{note[:60]}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a safety check
        log.debug("goplus could not record provider error: %s", exc)


def _purge(cache_key: str) -> None:
    """Drop a cached body we have decided not to trust."""
    try:
        cache_path(PROVIDER, cache_key).unlink(missing_ok=True)
    except OSError as exc:
        log.debug("goplus cache purge failed: %s", exc)


#: Longest we will stall to respect the limiter's floor before letting a refusal happen.
MAX_PACE_S = 5.0
#: The limiter compares wall-clock milliseconds with a strict ``<``; sleeping for exactly
#: the interval lands on the boundary and loses the race about half the time.
PACE_MARGIN_S = 0.05
_last_request_s: float = 0.0


def _pace() -> None:
    """Space our own consecutive requests by the limiter's minimum interval.

    ``_http.request_json`` returns an UNAVAILABLE receipt on a limiter refusal instead of
    waiting, so the sign-in POST followed immediately by the security GET would starve the
    GET on every credentialed scan — the *only* call that actually checks the token. The
    interval comes from the limiter's own config. This belongs in ``_http``; it lives here
    because that file is owned elsewhere.
    """
    global _last_request_s
    try:
        from kaiba.core.limiter import limits_for

        interval = limits_for(PROVIDER).min_interval_ms / 1000.0 + PACE_MARGIN_S
    except Exception as exc:  # noqa: BLE001 - pacing must never be the thing that fails
        log.debug("goplus could not read its limiter interval: %s", exc)
        interval = 0.5 + PACE_MARGIN_S
    wait = _last_request_s + interval - time.monotonic()
    if wait > 0:
        time.sleep(min(wait, MAX_PACE_S))
    _last_request_s = time.monotonic()


# --------------------------------------------------------------------------------------
# credentials (optional)
# --------------------------------------------------------------------------------------


def _credentials() -> tuple[str, str] | None:
    """``(app_key, app_secret)`` from settings, or ``None`` for the keyless tier."""
    from kaiba.core.config import get_settings

    s = get_settings()
    key = (s.goplus_app_key or "").strip()
    secret = (s.goplus_app_secret or "").strip()
    return (key, secret) if key and secret else None


def access_token(*, conn: Any = None, force: bool = False) -> str | None:
    """Sign in for the higher rate limit, or ``None`` when we are running keyless.

    The signature GoPlus documents is ``sha1(app_key + unix_time + app_secret)``. The token
    is held in memory only: it is a credential, so it must not reach the disk cache, which
    is why this call passes ``ttl_s=0``.
    """
    global _token_cache

    creds = _credentials()
    if creds is None:
        return None
    if not force and _token_cache is not None and _token_cache[1] > time.time():
        return _token_cache[0]

    app_key, app_secret = creds
    stamp = int(time.time())
    sign = hashlib.sha1(f"{app_key}{stamp}{app_secret}".encode()).hexdigest()  # noqa: S324
    _pace()
    fetched = post_json(
        PROVIDER,
        "auth.token",
        f"{BASE}/token",
        json_body={"app_key": app_key, "time": stamp, "sign": sign},
        priority=Priority.RESEARCH,
        ttl_s=0,
        conn=conn,
    )
    body = fetched.data if isinstance(fetched.data, dict) else {}
    if not fetched.ok or body.get("code") != 1:
        _report("auth.token", f"code={body.get('code')} message={body.get('message')}", conn)
        return None
    result = body.get("result") or {}
    token = str(result.get("access_token") or "").strip()
    if not token:
        return None
    expires_in = _dec(result.get("expires_in")) or Decimal(3600)
    _token_cache = (token, time.time() + max(0.0, float(expires_in) - _TOKEN_SKEW_S))
    return token


def _headers(conn: Any = None) -> dict[str, str]:
    token = access_token(conn=conn)
    return {"Authorization": token} if token else {}


def reset_credentials_cache() -> None:
    """Forget the in-memory access token. Used by tests and by a settings reload."""
    global _token_cache
    _token_cache = None


# --------------------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------------------


def token_security(address: str, chain: Chain, *, conn: Any = None) -> Fetched:
    """Security report for one token, already unwrapped from GoPlus's address map.

    Returns a :class:`Fetched` whose ``data`` is the per-address dict, or an UNAVAILABLE
    receipt. Never raises, including when the chain is one GoPlus does not cover.
    """
    addr = address.strip()
    if chain is Chain.SOL:
        url = f"{BASE}/solana/token_security"
        endpoint = "token.security_sol"
        lookup = addr
    elif chain in EVM_CHAINS:
        chain_id = CHAIN_IDS.get(chain)
        if chain_id is None or chain_id not in SUPPORTED_EVM_CHAIN_IDS:
            return _unavailable("token.security", f"goplus does not cover chain {chain.value}")
        url = f"{BASE}/token_security/{chain_id}"
        endpoint = "token.security"
        lookup = addr.lower()
    else:
        return _unavailable("token.security", f"goplus does not cover chain {chain.value}")

    params = {"contract_addresses": lookup}
    cache_key = f"goplus:{endpoint}:{chain.value}:{lookup}"
    headers = _headers(conn)  # may sign in, which is itself a paced request
    _pace()
    fetched = get_json(
        PROVIDER,
        endpoint,
        url,
        params=params,
        headers=headers,
        priority=Priority.RESEARCH,
        ttl_s=SECURITY_TTL_S,
        stale_grace_s=STALE_GRACE_S,
        cache_key=cache_key,
        retries=2,
        conn=conn,
    )
    if not fetched.ok:
        return fetched

    body = fetched.data if isinstance(fetched.data, dict) else {}
    if body.get("code") != 1:
        note = f"code={body.get('code')} message={str(body.get('message'))[:120]}"
        _purge(cache_key)
        _report(endpoint, note, conn)
        return _unavailable(endpoint, note)

    result = body.get("result")
    if not isinstance(result, dict) or not result:
        _purge(cache_key)
        return _unavailable(endpoint, "empty result — goplus has no record of this token")

    # GoPlus keys the result by the address it echoed back, whose case may not match ours.
    entry = result.get(lookup)
    if entry is None:
        wanted = lookup.lower()
        entry = next((v for k, v in result.items() if str(k).lower() == wanted), None)
    if not isinstance(entry, dict):
        _purge(cache_key)
        return _unavailable(endpoint, "result did not contain the requested address")
    return Fetched(entry, fetched.receipt)


# --------------------------------------------------------------------------------------
# normalisation into the shared DYOR vocabulary
# --------------------------------------------------------------------------------------


def _percent_sum(rows: Any, *, skip_locked: bool, limit: int | None = None) -> Decimal | None:
    """Sum ``percent`` (a fraction) over holder-like rows, as a 0–100 percentage.

    ``skip_locked`` drops rows GoPlus marks locked/burned/pooled: a burn address holding
    half the supply is the opposite of a concentration risk, and counting it as one is how
    a scanner cries wolf on every fair launch.
    """
    if not isinstance(rows, list):
        return None
    total = Decimal(0)
    counted = 0
    seen_any = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        pct = _dec(row.get("percent"))
        if pct is None:
            continue
        seen_any = True
        if skip_locked:
            tag = str(row.get("tag") or "").lower()
            if _flag(row.get("is_locked")) or any(w in tag for w in ("lock", "burn", "null", "pool")):
                continue
        total += pct
        counted += 1
        if limit is not None and counted >= limit:
            break
    if not seen_any:
        return None
    return total * 100


def _locked_lp_pct(rows: Any) -> Decimal | None:
    """Percentage of LP that is burned or locked — the only part that cannot be pulled.

    The range check at the end is not defensive tidiness. On the EVM route ``percent`` is the
    documented 0–1 fraction (BOBO's burn row reads ``0.9996``), but the Solana route returns
    something else entirely: USDC's largest LP row reported ``percent="199651036.3714"`` on
    2026-09-20, which is not a share of anything under either reading. Summed and scaled that
    becomes a number the grader reads as "LP fully locked", so a value outside 0–100 is
    treated as a field we cannot interpret rather than as reassurance.
    """
    if not isinstance(rows, list) or not rows:
        return None
    total = Decimal(0)
    seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        pct = _dec(row.get("percent"))
        if pct is None:
            continue
        seen = True
        tag = str(row.get("tag") or "").lower()
        address = str(row.get("address") or "").lower()
        burned = "burn" in tag or "null" in tag or address.endswith("000000000000000000000000dead")
        if _flag(row.get("is_locked")) or burned:
            total += pct
    if not seen:
        return None
    total *= 100
    return total if Decimal(0) <= total <= Decimal(100) else None


def _dex_liquidity(rows: Any) -> Decimal | None:
    """Pool depth in USD, summed across venues. EVM only, deliberately.

    The two routes spell this differently and only the EVM one is usable. EVM rows carry
    ``liquidity``; Solana rows carry ``tvl`` and no ``liquidity`` key at all, so GoPlus
    contributes no liquidity figure on Solana — and after measuring it, that silence is
    correct and is now intentional rather than accidental. Summing ``tvl`` was tried and
    reverted, because the Solana ``dex`` array is neither complete nor exhaustive
    (2026-09-20):

    * **It is capped at ten rows.** BONK's report lists 10 pools; RugCheck resolves 1,285
      for the same mint, and JUP 1,409 against GoPlus's 10. The sum is therefore a top-ten
      lower bound, and ``liquidity_usd`` resolves to the *lower* of the providers' claims,
      so publishing it would drag every deeply-traded token toward a low-liquidity reading.
    * **It is venue-incomplete.** FluxBot trades mainly on fluxbeam, which GoPlus does not
      index; its two visible pools total 46.53 USD. That is not a small error, it is a
      different quantity.

    A lower bound published under the name of a total is worse than no answer, so on Solana
    there is no answer.
    """
    if not isinstance(rows, list) or not rows:
        return None
    total = Decimal(0)
    seen = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        liq = _dec(row.get("liquidity"))
        if liq is None:
            continue
        seen = True
        total += liq
    return total if seen else None


def _positive(value: Decimal | None) -> Decimal | None:
    """A count GoPlus reports as zero is missing data, not a measurement.

    Binance-Peg USDT (0x55d3...7955, BSC) came back with ``holder_count: "0"`` on
    2026-09-20 for a token with millions of holders. A literal zero there is both false and
    the worse direction: ``holder_count`` resolves to the *lower* of the providers' claims,
    so one unindexed zero silently wins over the other provider's real figure and raises a
    conflict on top.
    """
    return value if value is not None and value > 0 else None


def _transfer_fee_bps(block: Any) -> Decimal | None:
    """Token-2022 transfer fee, in basis points.

    Both the key and the unit were wrong, so this returned ``None`` for every token ever
    scanned and the transfer-fee warning could not fire. The live shape, from FluxBot
    (``FLUXBmPhT3Fd1EDVFdg46YREqHBeNypn1h4EbnTzWERX``) on 2026-09-20, is::

        "transfer_fee": {"current_fee_rate": {"fee_rate": "0.03", "maximum_fee": "5e13"},
                         "scheduled_fee_rate": [{"epoch": "530", "fee_rate": "0.03", ...}]}

    ``current_fee_rate`` was not among the spellings tried. The unit is a **fraction**:
    that mint's own ``transferFeeConfig.newerTransferFee.transferFeeBasisPoints`` reads
    ``300``, so ``0.03`` is 3% and converts with ×10,000 — not the "part per ten thousand,
    e.g. 200 means 2%" the Solana field reference states, which would have made it 0.0003%.
    Chain truth decides, so the documentation is treated as wrong here.
    """
    if block is None:
        return None
    if isinstance(block, (str, int, float)):
        pct = _dec(block)
        return pct * 10_000 if pct is not None else None
    if not isinstance(block, dict):
        return None

    current = block.get("current_fee_rate")
    if isinstance(current, dict):
        rate = _dec(current.get("fee_rate"))
        if rate is not None:
            return rate * 10_000
    scheduled = block.get("scheduled_fee_rate")
    if isinstance(scheduled, list):
        rates = [_dec(row.get("fee_rate")) for row in scheduled if isinstance(row, dict)]
        known = [r for r in rates if r is not None]
        if known:
            # A schedule is a commitment to charge the highest of them eventually.
            return max(known) * 10_000

    for nest in ("newer_transfer_fee", "older_transfer_fee", "current_transfer_fee"):
        inner = block.get(nest)
        if isinstance(inner, dict):
            bps = _dec(inner.get("transfer_fee_basis_points") or inner.get("basis_points"))
            if bps is not None:
                return bps
    bps = _dec(block.get("transfer_fee_basis_points") or block.get("basis_points"))
    if bps is not None:
        return bps
    rate = _dec(block.get("fee_rate"))
    if rate is not None:
        return rate * 10_000
    pct = _dec(block.get("transfer_fee_percent") or block.get("percent"))
    return pct * 100 if pct is not None else None


def _owner_can_act(p: dict[str, Any]) -> bool | None:
    """Is anyone still able to exercise this contract's owner-gated functions?

    ``True`` yes, ``False`` provably not, ``None`` unknown — and unknown must stay
    pessimistic at the call site, because GoPlus documents a missing ``owner_address`` as
    "the owner address is unknown", which is not the same as there being none.
    """
    if _flag(p.get("hidden_owner")) or _flag(p.get("can_take_back_ownership")):
        # Either the renouncement is a facade or it is reversible; the capability is live.
        return True
    if "owner_address" not in p:
        return None
    return str(p.get("owner_address") or "").strip().lower() not in EVM_BLACKHOLES


def normalize_security(payload: Any, chain: Chain) -> dict[str, Any]:
    """Translate a GoPlus body into the shared DYOR property vocabulary.

    A key is present only when GoPlus actually answered it. Absent means unknown, and the
    merge in :mod:`kaiba.intelligence.dyor` depends on that distinction being kept here.
    """
    if not isinstance(payload, dict):
        return {}
    try:
        return _normalize_sol(payload) if chain is Chain.SOL else _normalize_evm(payload)
    except Exception as exc:  # noqa: BLE001 - a layout change degrades to "unknown"
        log.warning("goplus payload did not parse (%s); treating every field as unknown", exc)
        return {}


def _put(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        out[key] = value


def _normalize_evm(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    honeypot = _flag(p.get("is_honeypot"))
    cannot_sell = _flag(p.get("cannot_sell_all"))
    if honeypot is not None or cannot_sell is not None:
        out["can_sell"] = not (bool(honeypot) or bool(cannot_sell))

    mintable = _flag(p.get("is_mintable"))
    if mintable is not None:
        out["mint_authority_revoked"] = not mintable

    # An owner who can pause transfers can strand us in the position; on EVM that is the
    # same operational fact a Solana freeze authority describes. But `transfer_pausable`
    # reports a capability in the bytecode, not an actor holding it, and treating the two as
    # identical made a blocker out of tokens nobody can pause. Live, 2026-09-20: PEPE
    # (0x6982...1933) and BOBO (0xb90b...5295) both report transfer_pausable=1 with
    # owner_address=0x00..00, and `eth_call owner()` against each contract returns the zero
    # address, so the chain agrees the owner is gone. Both were being quarantined on
    # FREEZE_AUTHORITY. WBTC (0x2260...c599) is the control: transfer_pausable=1 with a live
    # owner (0xca06...beb7 on chain), and it must and does still block.
    pausable = _flag(p.get("transfer_pausable"))
    if pausable is False:
        out["freeze_authority_revoked"] = True
    elif pausable is True and _owner_can_act(p) is not False:
        # Live owner, or an owner state we could not establish — stay pessimistic.
        out["freeze_authority_revoked"] = False
    # pausable with a provably renounced, non-reclaimable owner leaves the property unknown.
    # That costs a partial-coverage warning, which is the honest price; asserting the
    # opposite would claim a freeze authority is revoked on a chain that has no such concept.

    buy_tax = _dec(p.get("buy_tax"))
    if buy_tax is not None:
        out["buy_tax_bps"] = buy_tax * 10_000
    sell_tax = _dec(p.get("sell_tax"))
    if sell_tax is not None:
        out["sell_tax_bps"] = sell_tax * 10_000

    modifiable = [_flag(p.get(k)) for k in ("slippage_modifiable", "personal_slippage_modifiable")]
    if any(v is not None for v in modifiable):
        out["tax_modifiable"] = any(bool(v) for v in modifiable)

    open_source = _flag(p.get("is_open_source"))
    if open_source is not None:
        out["source_verified"] = open_source

    # `can_take_back_ownership` is documented as "ownership can be reclaimed" — it says the
    # renouncement is reversible, not that anyone can rewrite a holder's balance. GoPlus has
    # a separate field for that, `owner_change_balance`: "Owner has authority to change the
    # balance of any token holder." The two are independent in live data, 2026-09-20:
    # SafeMoon v1 (0x8076...d8d3, BSC) and Pitbull (0xa57a...2e50, BSC) each report
    # can_take_back_ownership=1 alongside owner_change_balance=0, and folding the first into
    # balance_mutable made both a blocker for an authority they do not have. VIRTUAL
    # (0x0b3e...7e1b, Base) is the control for the genuine case, owner_change_balance=1, and
    # still resolves to True. Reclaimable ownership is real information, but it is not this
    # property; nothing in the dossier vocabulary currently carries it.
    owner_balance = _flag(p.get("owner_change_balance"))
    if owner_balance is not None:
        out["balance_mutable"] = owner_balance

    scam = [_flag(p.get(k)) for k in ("is_airdrop_scam", "fake_token")]
    if any(v is not None for v in scam):
        out["rugged"] = any(bool(v) for v in scam)
    prior = _dec(p.get("honeypot_with_same_creator"))
    if prior is not None:
        out["creator_rug_count"] = prior

    _put(out, "lp_burned_pct", _locked_lp_pct(p.get("lp_holders")))
    _put(out, "top10_pct", _percent_sum(p.get("holders"), skip_locked=True, limit=10))
    creator_pct = _dec(p.get("creator_percent"))
    if creator_pct is not None:
        out["dev_pct"] = creator_pct * 100
    _put(out, "holder_count", _positive(_dec(p.get("holder_count"))))
    _put(out, "liquidity_usd", _dex_liquidity(p.get("dex")))
    _put(out, "creator", (p.get("creator_address") or None))
    _put(out, "symbol", (p.get("token_symbol") or None))
    _put(out, "name", (p.get("token_name") or None))
    return out


def _normalize_sol(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}

    mintable = _status(p.get("mintable"))
    if mintable is not None:
        out["mint_authority_revoked"] = not mintable
    freezable = _status(p.get("freezable"))
    if freezable is not None:
        out["freeze_authority_revoked"] = not freezable

    balance_mutable = _status(p.get("balance_mutable_authority"))
    if balance_mutable is not None:
        out["balance_mutable"] = balance_mutable

    non_transferable = _status(p.get("non_transferable"))
    if non_transferable is not None:
        out["can_sell"] = not non_transferable

    hook = p.get("transfer_hook")
    if isinstance(hook, list):
        out["transfer_hook"] = bool(hook)
    else:
        hook_status = _status(hook)
        if hook_status is not None:
            out["transfer_hook"] = hook_status

    _put(out, "transfer_fee_bps", _transfer_fee_bps(p.get("transfer_fee")))
    upgradable = _status(p.get("transfer_fee_upgradable"))
    if upgradable is not None:
        out["tax_modifiable"] = upgradable

    metadata_mutable = _status(p.get("metadata_mutable"))
    if metadata_mutable is not None:
        out["metadata_mutable"] = metadata_mutable

    _put(out, "lp_burned_pct", _locked_lp_pct(p.get("lp_holders")))
    _put(out, "top10_pct", _percent_sum(p.get("holders"), skip_locked=True, limit=10))
    _put(out, "holder_count", _positive(_dec(p.get("holder_count"))))
    _put(out, "liquidity_usd", _dex_liquidity(p.get("dex")))
    # ``total_supply`` is deliberately not emitted: GoPlus reports a UI amount while
    # RugCheck reports raw atoms, and merging the two would manufacture a conflict on
    # every single Solana token out of a units mismatch neither provider got wrong.

    creators = p.get("creators")
    if isinstance(creators, list) and creators:
        first = creators[0]
        if isinstance(first, dict):
            _put(out, "creator", first.get("address") or None)
        elif isinstance(first, str):
            _put(out, "creator", first or None)

    meta = p.get("metadata")
    if isinstance(meta, dict):
        _put(out, "symbol", meta.get("symbol") or None)
        _put(out, "name", meta.get("name") or None)
    return out


def security_properties(address: str, chain: Chain, *, conn: Any = None) -> tuple[dict[str, Any], Receipt]:
    """Fetch and normalise in one step: ``(properties, receipt)``.

    On any failure the properties dict is empty and the receipt's basis is UNAVAILABLE, so
    a caller that merges these cannot accidentally read a dead provider as a clean bill.
    """
    fetched = token_security(address, chain, conn=conn)
    if not fetched.ok:
        return {}, fetched.receipt
    props = normalize_security(fetched.data, chain)
    if not props:
        return {}, Receipt(
            provider=PROVIDER,
            endpoint=fetched.receipt.endpoint,
            basis=EvidenceBasis.UNAVAILABLE,
            note="goplus responded but no known field parsed",
        )
    return props, fetched.receipt


def _debug_dump(payload: Any) -> str:  # pragma: no cover - operator convenience only
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


__all__ = [
    "BASE",
    "PROVIDER",
    "SECURITY_TTL_S",
    "SUPPORTED_EVM_CHAIN_IDS",
    "access_token",
    "normalize_security",
    "reset_credentials_cache",
    "security_properties",
    "token_security",
]
