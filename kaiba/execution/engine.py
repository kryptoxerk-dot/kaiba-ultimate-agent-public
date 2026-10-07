"""Signal → decision. The one place where "should we act on this?" is answered.

Precedence is the operator's mandate (PLAN §6.1) and it is not negotiable, so it is coded
as a straight-line sequence rather than a scoring function:

    permission → financial → data → risk veto → lane eligibility → size

Concretely: an OFF lane is a permission answer and comes first; a missing or stale dossier
is a data answer and comes before any risk arithmetic; the risk gate can refuse for money
reasons the lane knows nothing about; only what survives all of that gets a size.

**Every decision is persisted, including SKIP.** That is a hard requirement, not an
optimisation. The learning loop cannot compute what we missed from a table that only
contains what we took, and a lane that stops firing is invisible unless its skips are on
the record. ``decisions`` plus ``EventKind.DECISION`` are written on every path through
:func:`decide`.

This module never places a live order. In SHADOW and CANARY it hands off to the paper
broker; in LIVE it leaves a ``planned`` order row and the executor picks it up. Keeping the
signer on the other side of a table is what makes "the agent decided" and "money moved"
two separately auditable events.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

from kaiba.core import journal
from kaiba.core.config import get_risk
from kaiba.core.db import fetch_all, fetch_one, get_conn, jdump, jload, upsert
from kaiba.core.events import emit
from kaiba.core.schemas import (
    QUOTE_ASSETS,  # noqa: F401 - re-exported for callers that check candidates
    is_quote_asset,
    EVM_ZERO,
    NATIVE_DECIMALS,
    NATIVE_SYMBOL,
    SOL_NATIVE_MINT,
    Action,
    Chain,
    Decision,
    EventKind,
    EvidenceBasis,
    Grade,
    Lane,
    LaneMode,
    Order,
    OrderState,
    Side,
    Signal,
    TokenDossier,
    digest,
    normalize_address,
    now_ms,
)
from kaiba.execution.lanes import signal_from_row
from kaiba.execution.paper import PaperBroker, position_for_order

log = logging.getLogger(__name__)

#: A dossier older than this is not evidence about a token that is minutes old.
DOSSIER_MAX_AGE_S = 300

WATERMARK_KEY = "engine.signal_watermark"
PARAMS_VERSION = "v1"

# --------------------------------------------------------------------------------------
# sibling modules that are landing in parallel
# --------------------------------------------------------------------------------------

try:  # kaiba/execution/risk.py — written concurrently by another agent
    from kaiba.execution.risk import RiskGate
except ImportError:  # sibling module still landing
    RiskGate = None  # type: ignore[assignment]

try:  # kaiba/execution/risk.py — the ONE definition of which day a halt belongs to
    from kaiba.execution.risk import day_key
except ImportError:  # sibling still landing; same rule, stated locally

    def day_key(ts_ms: int | None = None) -> str:
        """UTC day. Must match ``risk.day_key`` exactly: a halt scoped to a different day
        than the one ``RiskGate.resume`` clears is a halt nothing can lift."""
        from datetime import UTC, datetime

        ts = ts_ms if ts_ms is not None else now_ms()
        return datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%d")

try:  # kaiba/execution/risk.py
    from kaiba.execution.risk import score_from_strength as risk_score_from_strength
except ImportError:  # sibling still landing; the scales are 0-1 and 0-100

    def risk_score_from_strength(strength: float) -> float:
        return float(strength) * 100.0


try:  # kaiba/execution/risk.py
    from kaiba.execution.risk import position_size
except ImportError:  # sibling module still landing

    def position_size(chain: Chain, lane: Lane, score: float, conn: Any = None) -> int:
        """Refuse. A sizer that is not the risk gate does not get to size a position.

        This name exists only so importing it cannot fail. It used to interpolate the
        lane band between the chain's min and max position, applying NONE of the controls
        that make a size safe -- no live equity, no daily loss stop, no total-exposure
        cap, no viability band, no free-balance check -- and it RAISED a below-minimum
        size up to ``min_position_base_units``, turning a refusal into an order.

        MEASURED 2026-09-22: ``risk.py`` exports no module-level ``position_size``, so the
        ``except ImportError`` branch above is PERMANENTLY taken and this was the engine's
        live fallback on a funded, armed box. One exception inside :class:`RiskGate` would
        have produced an unchecked position. A missed entry is cheap; an unbounded one is not.
        """
        log.error(
            "engine.position_size fallback called for %s/%s: refusing. Only the risk gate "
            "may size a position.", chain.value, lane.value,
        )
        return 0


try:  # kaiba/execution/protection.py — written concurrently by another agent
    from kaiba.execution.protection import arm as arm_protection
except ImportError:  # sibling module still landing

    def arm_protection(position_id: str, conn: Any = None) -> None:
        """Fallback: no protection. A shadow fill is unprotected until protection.py lands."""
        log.debug("protection module absent; position %s left unprotected (paper only)", position_id)


# --------------------------------------------------------------------------------------
# dossier
# --------------------------------------------------------------------------------------


def load_dossier(chain: Chain, token: str, conn: sqlite3.Connection) -> TokenDossier | None:
    row = fetch_one(
        conn, "SELECT dossier_json FROM token_dossiers WHERE chain=? AND address=?", (chain.value, token)
    )
    if not row:
        return None
    try:
        return TokenDossier.model_validate_json(row["dossier_json"])
    except Exception as exc:  # a corrupt dossier is the same as no dossier
        log.warning("unreadable dossier for %s:%s: %s", chain.value, token, exc)
        return None


def _dossier_stale(dossier: TokenDossier, at_ms: int, max_age_s: int = DOSSIER_MAX_AGE_S) -> bool:
    return (at_ms - dossier.built_at_ms) > max_age_s * 1000


# --------------------------------------------------------------------------------------
# risk gate adapter
# --------------------------------------------------------------------------------------


def _local_refusal(chain: Chain, mode: LaneMode, conn: sqlite3.Connection) -> str | None:
    """Guards this module owns regardless of whether ``risk.py`` is present yet."""
    cfg = get_risk()
    if cfg.entries_paused:
        return "entries_paused"
    if cfg.reduce_only:
        return "reduce_only"
    if mode is LaneMode.LIVE and not cfg.chain_budget(chain).enabled:
        return "chain_disabled"
    # TODAY's row, not any row that has ever been halted.
    #
    # MEASURED 2026-09-23. This read was `WHERE halted=1 LIMIT 1`, with no day at all. The
    # 2026-09-22 row was halted at 07:47 UTC+8 by a protection overrun and the UTC day
    # rolled before anything cleared it, so that row sat `halted=1` forever. From 00:00 UTC
    # onward EVERY entry on EVERY chain was refused with
    # `risk_halt:protection_overrun:15204ms>5000ms x348` -- a fault from the previous day,
    # already fixed, still halting the book hours later.
    #
    # It was invisible from either side: `risk_state` for today read `halted=0`, a fresh
    # `RiskGate.check_entry` allowed the entry, and no `risk.halt` event fired, because
    # nothing WAS halting. Only this query disagreed, and it is the one the engine asks.
    #
    # `risk.py` has always scoped its own read to `day_key()`; this is the same rule, and
    # the reason the two must agree is that `RiskGate.resume` -- the thing an operator or
    # the watchdog's auto-recovery calls -- only ever clears today's row. A halt this
    # function can see but `resume` cannot reach is a halt nothing can lift.
    row = fetch_one(
        conn,
        "SELECT halted, halt_reason FROM risk_state WHERE day_key = ? AND halted = 1",
        (day_key(),),
    )
    if row:
        return f"risk_halt:{row['halt_reason'] or 'halted'}"
    return None


def _normalise_verdict(result: Any) -> str | None:
    """Turn whatever ``RiskGate.check_entry`` returns into a refusal reason or ``None``."""
    if result is None or result is True:
        return None
    if result is False:
        return "risk_veto"
    if isinstance(result, str):
        return result or None
    if isinstance(result, (tuple, list)) and result:
        ok = bool(result[0])
        reason = str(result[1]) if len(result) > 1 and result[1] else "risk_veto"
        return None if ok else reason
    if isinstance(result, dict):
        ok = result.get("ok", result.get("allowed", result.get("passed")))
        reason = result.get("reason") or result.get("blocker")
        blockers = result.get("blockers") or []
        if ok is False or (ok is None and blockers):
            return str(reason or (blockers[0] if blockers else "risk_veto"))
        return None
    ok = getattr(result, "ok", getattr(result, "allowed", getattr(result, "passed", None)))
    if ok is False:
        reason = getattr(result, "reason", None) or getattr(result, "blocker", None)
        blockers = getattr(result, "blockers", None) or []
        return str(reason or (blockers[0] if blockers else "risk_veto"))
    return None


def _risk_refusal(
    signal: Signal, mode: LaneMode, size_base_units: int, conn: sqlite3.Connection
) -> str | None:
    """Ask :mod:`kaiba.execution.risk` for permission; return a refusal reason or ``None``.

    The call is introspected rather than hard-coded because ``RiskGate.check_entry`` is
    being written in parallel and only its name is fixed by the contract.
    """
    local = _local_refusal(signal.chain, mode, conn)
    if local:
        return local
    if RiskGate is None:
        # The gate is not optional. If the module is absent there is nothing enforcing
        # size, exposure or the daily loss stop, and the honest answer is no entry.
        return "risk_gate_unavailable"

    # This used to construct and call the gate by introspection, because risk.py was
    # being written in parallel and only the method name was fixed. That shim outlived
    # its purpose and became the most dangerous bug in the system: it tried
    # ``RiskGate(conn)`` first, which *succeeds* because the real constructor takes one
    # optional argument, binding a sqlite connection where a risk provider belongs.
    # check_entry then raised, and the handler below logged "treating as no objection"
    # and returned None. Every entry passed an unexecuted risk check. Bind directly.
    try:
        verdict = RiskGate().check_entry(
            chain=signal.chain,
            lane=signal.lane,
            size_base_units=size_base_units,
            conn=conn,
            token=signal.token,
        )
    except Exception as exc:  # noqa: BLE001 - a broken gate must refuse, never wave through
        log.error("RiskGate.check_entry raised; refusing the entry: %s", exc)
        ev_emit_risk_error(signal, exc, conn)
        return f"risk_gate_error:{type(exc).__name__}"
    return _normalise_verdict(verdict)


def ev_emit_risk_error(signal: Signal, exc: Exception, conn: sqlite3.Connection) -> None:
    """Make a broken risk gate loud. A silent one is how it stayed broken."""
    try:
        emit(
            EventKind.RISK_HALT,
            {"reason": "risk_gate_error", "error": f"{type(exc).__name__}: {exc}"[:300],
             "lane": signal.lane.value},
            chain=signal.chain, subject=signal.token, level="error", conn=conn,
        )
    except Exception:  # noqa: BLE001 - telemetry must not mask the refusal
        pass


def _size_for(signal: Signal, conn: sqlite3.Connection) -> int:
    """The real sizer, via ``RiskGate``. The fallback is a last resort, not a default.

    ``position_size`` is a *method* on :class:`RiskGate`, never a module-level function,
    so the optimistic import at the top of this file has always raised ``ImportError``
    and every size this engine has ever produced came from the fallback below it. That
    fallback interpolates between the chain's min and max and ignores the bankroll
    fraction, the score ladder, the 5% envelope clamp, the gas reserve and open
    exposure -- so a low-strength signal was sized like a high-strength one, and the
    envelope was not enforced at the point the size was chosen. Found 2026-09-20 while
    investigating why raising ``max_position_base_units`` moved every trade straight to
    the new ceiling; that is the signature of an interpolator, not a ladder.
    """
    if RiskGate is not None:
        try:
            # RiskGate() takes a *risk provider*, not a connection. `RiskGate(conn)` does
            # not raise -- it binds the connection as the provider and then fails deeper
            # in, which is exactly how `_risk_refusal` spent this morning "checking" risk
            # with a gate that could never run. The connection is the method's argument.
            gate = RiskGate()
            # `Signal.strength` is 0-1 conviction; `position_size` wants a 0-100 score.
            # Passing the raw value is what silently sized every signal to zero.
            score = risk_score_from_strength(signal.strength)
            return int(
                gate.position_size(signal.chain, signal.lane, score, conn, token=signal.token)
            )
        except Exception as exc:  # noqa: BLE001 - a sizer that cannot run REFUSES
            # Deliberately NO fallback. Until 2026-09-22 this fell through to the
            # module-level `position_size` above, which applies none of the controls and
            # rounds a sub-minimum size UP to the chain minimum. An exception here means
            # we could not establish what is safe, and "we do not know" sizes at zero.
            log.exception("RiskGate.position_size raised for %s; refusing", signal.token)
            try:
                from kaiba.execution.risk import note_zero_size

                note_zero_size(conn, signal.chain, signal.lane, signal.token,
                               f"sizer_raised:{type(exc).__name__}")
            except Exception:  # noqa: BLE001 - the refusal stands, labelled or not
                pass
            return 0
    log.error("RiskGate is unavailable; refusing to size %s", signal.token)
    return 0


# --------------------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------------------


def _register_trial(signal: Signal, conn: sqlite3.Connection) -> None:
    """Note the lane's effective parameters in the trial registry. Never raises."""
    try:
        from kaiba.execution.lanes import LaneContext
        from kaiba.learning.registry import register

        params = LaneContext(chain=signal.chain, token=signal.token).lane_params(signal.lane)
        register(signal.lane, params, conn)
    except Exception as exc:  # noqa: BLE001 - measurement must never block a decision
        log.debug("could not register trial for %s: %s", signal.lane.value, exc)


def decision_id_for(signal_id: str, params_version: str = PARAMS_VERSION) -> str:
    """Deterministic, so re-deciding a signal updates its row instead of duplicating it."""
    return "dec_" + digest({"signal_id": signal_id, "params_version": params_version})[:24]


#: Row fields that mark inventory ARRIVING rather than being bought. Mirrors the lane's
#: own markers; the engine re-reads them so a payload cannot enter on a claim alone.
_COPY_TRANSFER_MARKERS: frozenset[str] = frozenset(
    {"transfer_in", "transfer", "receive", "received", "airdrop", "in", "mint"}
)


def _copy_source_refusal(
    signal: Signal, dossier: TokenDossier | None, conn: sqlite3.Connection
) -> tuple[str, str] | None:
    """``(blocker, why)`` when a copy signal cannot be acted on. ``None`` to proceed.

    Four questions, each answered from evidence that is still readable at this moment:

    1. did the payload record a decoded BUY at all? A missing marker is UNKNOWN, and
       unknown never reads as the value that authorises spending money.
    2. does anything on it mark inventory merely arriving? A row saying ``buy`` in one
       field and ``transfer_in`` in another has not proved a purchase; the disagreement
       is the reason to refuse.
    3. does the DATABASE still put this wallet in the trusted-copy cohort? Curation can
       be withdrawn between the signal and the decision, and withdrawing it is exactly
       what an operator does on noticing a source going bad.
    4. is our own research complete? Copying is acting on someone else's conclusion, so
       an unknown of ours is not covered by their conviction.
    """
    if signal.lane is not Lane.TRUSTED_COPY:
        return None
    payload = signal.payload or {}

    if "source_side" not in payload or "source_event" not in payload:
        return ("copy_source_not_decoded_buy",
                "the copy payload carries no decoded buy marker; UNKNOWN is not a buy")
    side = str(payload.get("source_side") or "").strip().lower()
    if not side:
        return ("copy_source_not_decoded_buy",
                "the copy source has no side: UNKNOWN, which is not a buy")

    event = str(payload.get("source_event") or "").strip().lower()
    if event in _COPY_TRANSFER_MARKERS:
        return ("copy_source_transfer_in",
                f"the copy source is marked {event!r}: inventory arriving is not a purchase")
    if side != "buy":
        return ("copy_source_not_decoded_buy",
                f"the copy source side is {side!r}; only an explicit buy means someone paid")

    wallet = str(payload.get("source_wallet") or "")
    cohort = None
    if wallet:
        try:
            row = fetch_one(
                conn,
                "SELECT cohort FROM wallets WHERE chain=? AND address=?",
                (signal.chain.value, wallet),
            )
            cohort = (row["cohort"] if row else None) or None
        except Exception as exc:  # noqa: BLE001 - if we cannot confirm trust, we do not have it
            log.warning("could not re-read the copy source cohort: %s", exc)
            cohort = None
    if cohort != "trusted_copy":
        return ("copy_source_not_trusted",
                f"the copy source is in cohort {cohort!r}, not trusted_copy, at decision time")

    unknowns = [str(u) for u in (getattr(dossier, "unknowns", None) or [])]
    if unknowns:
        return ("unknown_safety",
                "copying needs our own research complete; unknown: " + ", ".join(unknowns))
    return None


# --------------------------------------------------------------------------------------
# launchpad allowlist for live money
#
# OWNER-APPROVED 2026-10-01 (docs/research/audit-20261001-strategy.md section 2). On all
# 89 Robinhood live fills, pons/pons_v2 n=42 mean +1.0% (18 wins); every other launchpad
# n=31 mean -23.7% (3 wins); unknown n=17 mean -9.9%. Permutation p=0.024, same sign in
# both halves of the record.
#
# MEASURED on the scanned population before shipping (box, read-only, RH sm-trenches
# signals 09-23..10-01, launchpad as the `tokens` row read at decision time): the rule
# moves 171 of 481 signals (35.6%) and 38 of 94 enters (40.4%) off live money -- 127/28
# from named non-pons launchpads, 44/10 unknown. Of 312 signals on tokens later known to
# be pons, 0 were unknown at decision time, so "unknown is not live" costs no pons entry.
#
# An ALLOWLIST, not a blocklist: `sm-trenches.params.blocked_launchpads` names what is
# known to be bad, and a launchpad that appears next week walks straight past it.
#
# Those refused entries are not discarded. The evidence is on FILLS only, so the outcome
# of the signals we stop taking is unmeasured; each one becomes a SHADOW twin, paper
# traded by the ordinary paper broker and exited by the ordinary protection path. That
# is the forward outcome the rule needs to be confirmed or withdrawn.
# --------------------------------------------------------------------------------------

#: Lane param: ``{chain: [launchpad, ...]}``. A chain absent from the mapping is
#: unrestricted; a chain present enters live ONLY the launchpads listed for it.
LIVE_LAUNCHPADS_PARAM = "live_launchpads_by_chain"

#: Blocker head. The launchpad (lower-cased, or ``unknown``) follows the colon.
LAUNCHPAD_NOT_LIVE = "launchpad_not_live"

#: Folded into the twin's decision id so it is deterministic per signal and can never
#: collide with the live decision's own id.
SHADOW_TWIN_VERSION = PARAMS_VERSION + ":shadow_twin"

#: (lane, chain) pairs that never enter with real money, WHATEVER the config says: a LIVE or
#: CANARY entry there always takes the launchpad twin branch (a SHADOW twin, the live
#: decision a SKIP). launch-snipe on bsc: OWNER 2026-10-05 "enable bnb snipes", paper first.
#: The snipe producer refuses to signal unless the config already makes every one a twin
#: (``snipe.paper_only_refusal``), but that guard sits in front of the dossier, not at the
#: money: a signal recorded before a config edit, a producer that skips it, or a launchpad
#: label that differs from the launch's venue would otherwise reach a LIVE ENTER here
#: (review 2026-10-05). ``snipe.PAPER_ONLY_CHAINS`` must equal the launch-snipe entry (a test
#: pins it). Arming one of these is a code change, not a config edit.
PAPER_ONLY_LANE_CHAINS: dict[Lane, frozenset[Chain]] = {
    Lane.LAUNCH_SNIPE: frozenset({Chain.BSC}),
}


def shadow_twin_id(signal_id: str) -> str:
    """The paper twin's decision id for ``signal_id``. Deterministic, like the live one."""
    return decision_id_for(signal_id, SHADOW_TWIN_VERSION)


def _live_launchpads(cfg: Any, signal: Signal) -> frozenset[str] | None:
    """The live allowlist for this signal's lane and chain; ``None`` when unrestricted.

    Malformed configuration FAILS CLOSED -- an empty allowlist -- because the failure this
    guards against is a typo quietly opening the chain to every launchpad again. A bare
    string is read as one launchpad, never as its letters.
    """
    try:
        params = cfg.lane(signal.lane).params or {}
    except Exception as exc:  # noqa: BLE001 - an unreadable lane config admits nothing new
        log.error("lane params unreadable for %s; launchpad allowlist fails closed: %s",
                  signal.lane.value, exc)
        return frozenset()
    if LIVE_LAUNCHPADS_PARAM not in params:
        return None
    by_chain = params[LIVE_LAUNCHPADS_PARAM]
    if not isinstance(by_chain, dict):
        log.error("%s.%s is %r, not a mapping; failing closed on every chain",
                  signal.lane.value, LIVE_LAUNCHPADS_PARAM, type(by_chain).__name__)
        return frozenset()
    keyed = {str(k).strip().lower(): v for k, v in by_chain.items()}
    if signal.chain.value not in keyed:
        return None
    names = keyed[signal.chain.value]
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple, set, frozenset)):
        log.error("%s.%s.%s is %r, not a list; failing closed", signal.lane.value,
                  LIVE_LAUNCHPADS_PARAM, signal.chain.value, names)
        return frozenset()
    return frozenset(str(n).strip().lower() for n in names if isinstance(n, str) and n.strip())


def _token_launchpad(chain: Chain, token: str, conn: sqlite3.Connection) -> str | None:
    """``tokens.launchpad``, lower-cased; ``None`` when unknown or unreadable.

    The same row the lane read (``scanner.load_token_meta`` -> ``ctx.token_meta``), so the
    lane and this rule cannot disagree about a token. An unreadable row is unknown, and
    unknown is not on any allowlist: the refusal is the safe direction.
    """
    try:
        row = fetch_one(conn, "SELECT launchpad FROM tokens WHERE chain=? AND address=?",
                        (chain.value, token))
    except sqlite3.Error as exc:
        log.warning("tokens row unreadable for %s; launchpad unknown: %s", token[:12], exc)
        return None
    name = str((row or {}).get("launchpad") or "").strip().lower()
    return name or None


def _launchpad_refusal(
    signal: Signal, mode: LaneMode, cfg: Any, conn: sqlite3.Connection
) -> str | None:
    """``launchpad_not_live:<name>`` when real money may not enter this token's launchpad.

    LIVE and CANARY only. A SHADOW lane records exactly as before -- the rule is about
    whose money takes the trade, not about what the lane is allowed to see.

    A :data:`PAPER_ONLY_LANE_CHAINS` pair refuses regardless of the allowlist: the same
    blocker, so the same twin branch, and no configuration can open it.
    """
    if mode not in (LaneMode.LIVE, LaneMode.CANARY):
        return None
    if signal.chain in PAPER_ONLY_LANE_CHAINS.get(signal.lane, frozenset()):
        launchpad = _token_launchpad(signal.chain, signal.token, conn)
        return f"{LAUNCHPAD_NOT_LIVE}:{launchpad or 'unknown'}"
    allowed = _live_launchpads(cfg, signal)
    if allowed is None:
        return None
    launchpad = _token_launchpad(signal.chain, signal.token, conn)
    if launchpad is not None and launchpad in allowed:
        return None
    return f"{LAUNCHPAD_NOT_LIVE}:{launchpad or 'unknown'}"


#: Lane param: ``{chain: seconds}``. A LIVE/CANARY entry on a token that migrated (graduated off
#: its launch curve) less than this long ago is refused and becomes a paper twin.
POST_MIGRATION_PARAM = "post_migration_cooldown_s"  # NOT min_<feature>: that prefix is a lane feature threshold (refuses unknown)
POST_MIGRATION_COOLDOWN = "post_migration_cooldown"


def _post_migration_refusal(signal: Signal, mode: LaneMode, cfg: Any) -> str | None:
    """``post_migration_cooldown:<age>s<<min>s`` when real money would buy into a fresh migration.

    MEASURED 2026-10-05 on 614 replayed sol sm-trenches signals (live exits, 3.25%/leg): signals
    within 2 min of migration -20.8% (n=88), 2-10 min -26.8% (n=66), against -16.9% for all; on
    live fills <2 min -20.6% (n=14). The 2026-10-05 18:28 EGTvzb buy filled 141 s after migration at
    the spike and closed -79% 30 s later. Consistent with the migration-fade evidence (73% of
    migrations trade below 40% of the migration price within 20 min). Unknown age is NOT refused:
    only a measured fresh migration is. Paper twins keep measuring the skipped entries.
    """
    if mode not in (LaneMode.LIVE, LaneMode.CANARY):
        return None
    try:
        params = cfg.lane(signal.lane).params or {}
    except Exception:  # noqa: BLE001 - unreadable lane config: this guard adds no refusal
        return None
    raw = params.get(POST_MIGRATION_PARAM)
    if isinstance(raw, dict):
        raw = raw.get(signal.chain.value, raw.get("default"))
    try:
        minimum = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        log.error("%s.%s=%r is not a number; post-migration guard off", signal.lane.value,
                  POST_MIGRATION_PARAM, raw)
        return None
    if not minimum or minimum <= 0:
        return None
    payload = signal.payload or {}
    if not payload.get("migrated"):
        return None
    try:
        age = float(payload.get("since_migration_s"))
    except (TypeError, ValueError):
        return None
    if age < minimum:
        return f"{POST_MIGRATION_COOLDOWN}:{int(age)}s<{int(minimum)}s"
    return None


def _twin_refusal(signal: Signal, conn: sqlite3.Connection) -> str | None:
    """Why no paper twin should be opened for this signal, or ``None`` to open one.

    One open twin per token: the paper broker ADDS to an open position of the same
    lane and mode, so every repeat signal on a token would otherwise pile another full
    size into one paper position and blur what a single entry earns.
    """
    try:
        row = fetch_one(
            conn,
            "SELECT position_id FROM positions WHERE chain=? AND token=? AND lane=? "
            "AND mode=? AND closed_ms IS NULL LIMIT 1",
            (signal.chain.value, signal.token, signal.lane.value, LaneMode.SHADOW.value),
        )
    except sqlite3.Error as exc:
        return f"positions unreadable: {exc}"[:120]
    if row:
        return f"shadow twin {row['position_id']} already open on this token"
    return None


def decide(
    signal: Signal,
    conn: sqlite3.Connection | None = None,
    *,
    twins: list[Decision] | None = None,
) -> Decision:
    """Turn one signal into a decision and persist it, whatever the answer is.

    ``twins``: a caller that will hand a paper twin off passes a list; when a LIVE/CANARY
    entry is refused only because its launchpad is not on the lane's live allowlist, the
    SHADOW twin decision is persisted and appended to it. A caller that passes nothing
    gets no twin, so a twin row never exists without someone to open its position.
    """
    c = conn or get_conn()
    cfg = get_risk()
    # Record the configuration this decision ran under, before deciding anything.
    # The deflated Sharpe ratio divides by the number of attempts, and an attempt
    # nobody wrote down still happened. See kaiba/learning/registry.py.
    _register_trial(signal, c)
    mode = cfg.effective_mode(signal.lane)
    ts = now_ms()

    def _skip(blockers: list[str], thesis: str, *, dossier: TokenDossier | None = None) -> Decision:
        return _persist(
            Decision(
                decision_id=decision_id_for(signal.signal_id),
                ts_ms=ts,
                lane=signal.lane,
                mode=mode,
                chain=signal.chain,
                token=signal.token,
                action=Action.SKIP,
                thesis=thesis,
                confidence=signal.strength,
                signals=[signal.signal_id],
                dossier_grade=dossier.grade if dossier else Grade.UNSCORED,
                blockers=blockers,
                invalidation=None,
                params_version=PARAMS_VERSION,
            ),
            c,
        )

    # 0. is this even a trade? A candidate that is the asset we PAY with is not one.
    # FOUND 2026-09-22 the moment the smart-money cohort was seeded: the top-ranked sol
    # candidate was WSOL itself (31 smart net buyers), because every SOL-denominated buy
    # in `swaps` carries the wrapped mint on one side. Entering it pays SOL to receive SOL
    # and pays the round trip twice. Refused in EVERY mode -- a shadow record of buying
    # the quote asset is not evidence about anything either. Two checks: the structural
    # one (same mint in and out) needs no table; the address one catches the EVM wrapped
    # and stable mints, where the input leg is EVM_ZERO and the structural check is blind.
    _input_leg = SOL_NATIVE_MINT if signal.chain is Chain.SOL else EVM_ZERO
    if signal.token == _input_leg or is_quote_asset(signal.chain, signal.token):
        return _skip(
            ["token_is_quote_asset"],
            f"{signal.token} is an asset positions are denominated in on "
            f"{signal.chain.value}; buying it with itself only pays the round trip",
        )

    # 1. permission: is this lane allowed to act at all?
    if mode is LaneMode.OFF:
        return _skip(["lane_off"], f"{signal.lane.value} is off (kill switch, global mode or ceiling)")

    # 1b. direction. A signal carrying ``bias: sell`` is a thesis that the price falls.
    # Every venue this agent trades is spot with no borrow and no perp, so no arm
    # expresses that thesis, and the live order builder below hardcodes ``side=Side.BUY``:
    # an ENTER here is a long on a short thesis. MEASURED 2026-09-21: migration-fade
    # (bias sell, "73% of migrations trade below 40% within 20 min") was armed live and
    # its first two real fills lost -91.5% and -66.0% -- the thesis working, against us.
    # Shadow keeps recording the long arm: that record is what kaiba.learning.replay
    # prices the executable alternatives from (enter later, exit sooner, do not enter).
    # Real money does not take the wrong side of its own thesis.
    if str((signal.payload or {}).get("bias") or "").lower() == "sell" and mode in (LaneMode.LIVE, LaneMode.CANARY):
        return _skip(
            ["bias_sell_no_short_venue"],
            f"{signal.lane.value} is a sell-bias signal; this venue has no short arm; not opening a long",
        )

    # 2/3. data: no dossier, no trade. Unknown safety is not safe (CONTRACT rule 2).
    dossier = load_dossier(signal.chain, signal.token, c)
    if dossier is None:
        return _skip(["no_dossier"], "no DYOR dossier for this token")
    if _dossier_stale(dossier, ts):
        age_s = (ts - dossier.built_at_ms) / 1000.0
        return _skip(["no_dossier"], f"dossier is {age_s:.0f}s old (budget {DOSSIER_MAX_AGE_S}s)", dossier=dossier)
    if dossier.blockers:
        blockers = [b.value if hasattr(b, "value") else str(b) for b in dossier.blockers]
        return _skip(blockers, "dossier blockers: " + ", ".join(blockers), dossier=dossier)
    # A QUARANTINED grade is a condemnation in its own right, and it has to refuse even
    # when the reason list above is empty. Today dyor writes the two together -- a BLOCKER
    # finding is what produces the grade (`intelligence/dyor._grade_from_score`) -- but
    # nothing enforces that pairing on the way back in: `load_dossier` will validate any
    # stored JSON, `TokenDossier.tradeable` asks only about `blockers`, and the coverage
    # ceiling lowers a grade without ever adding a blocker. So a dossier that reaches this
    # engine condemned but with its reasons lost reads as clean everywhere else. The grade
    # is the verdict; refuse on the verdict too, before a size exists.
    # Ordered after the blocker check on purpose: a dossier that DOES name its reasons must
    # report them, not this generic label (tests/test_bsc_lane_inputs.py pins that list).
    if dossier.grade is Grade.QUARANTINED:
        return _skip(
            ["dossier_quarantined"],
            "dossier grade is QUARANTINED with no named blocker; a condemned token is not sized",
            dossier=dossier,
        )

    # 3b. the copy boundary, re-checked here rather than inherited from the lane.
    #
    # A trusted-copy entry rests entirely on claims about a THIRD PARTY: that a particular
    # wallet paid for this token, and that we trust that wallet. Both were checked when the
    # signal was built, but the signal then travelled through a queue and a table, and a
    # lane verdict is not evidence -- the evidence is the payload and the wallets row, and
    # both are still here to be read.
    #
    # Scoped to this lane deliberately. Every other lane reasons about the TOKEN, where the
    # dossier gates above are the right check; only this one reasons about a person.
    copy_refusal = _copy_source_refusal(signal, dossier, c)
    if copy_refusal:
        return _skip([copy_refusal[0]], copy_refusal[1], dossier=dossier)

    # 4. risk veto, then 5. lane eligibility and 6. size.
    size = _size_for(signal, c)
    refusal = _risk_refusal(signal, mode, size, c)
    if refusal:
        return _skip([refusal], f"risk gate refused: {refusal}", dossier=dossier)
    if size <= 0:
        return _skip(["no_size"], "sizer returned zero base units", dossier=dossier)

    budget = cfg.chain_budget(signal.chain)
    pct = (
        float(Decimal(size) / Decimal(budget.bankroll_base_units) * 100)
        if budget.bankroll_base_units
        else None
    )

    # 7. launchpad allowlist for real money. LAST on purpose: everything above has
    # already said yes, so a refusal here is exactly "the trade we would have taken",
    # and that is what the paper twin records. See `_launchpad_refusal`.
    lp_blocker = _launchpad_refusal(signal, mode, cfg, c) or _post_migration_refusal(signal, mode, cfg)
    if lp_blocker is not None:
        twin_id = shadow_twin_id(signal.signal_id)
        no_twin = "not requested by this caller" if twins is None else _twin_refusal(signal, c)
        skipped = _skip(
            [lp_blocker],
            f"{lp_blocker}: {signal.lane.value} live entry refused on {signal.chain.value} "
            "(launchpad allowlist or post-migration cooldown); "
            + (f"paper twin {twin_id}" if no_twin is None else f"no paper twin: {no_twin}"),
            dossier=dossier,
        )
        if no_twin is None and twins is not None:
            twins.append(_persist(Decision(
                decision_id=twin_id,
                ts_ms=ts,
                lane=signal.lane,
                mode=LaneMode.SHADOW,
                chain=signal.chain,
                token=signal.token,
                action=Action.ENTER,
                thesis=f"shadow twin of {skipped.decision_id} ({lp_blocker}): "
                + ("; ".join(signal.reasons) or f"{signal.lane.value} fired"),
                confidence=signal.strength,
                signals=[signal.signal_id],
                dossier_grade=dossier.grade,
                size_base_units=size,
                size_pct_bankroll=pct,
                invalidation=_invalidation(signal),
                # On an ENTER this is not a refusal: it is WHY this entry is paper and
                # not live, and what tells a twin apart from an ordinary shadow entry
                # if the lane is ever switched to shadow as a whole.
                blockers=[lp_blocker],
                params_version=PARAMS_VERSION,
            ), c))
        return skipped

    return _persist(
        Decision(
            decision_id=decision_id_for(signal.signal_id),
            ts_ms=ts,
            lane=signal.lane,
            mode=mode,
            chain=signal.chain,
            token=signal.token,
            action=Action.ENTER,
            thesis="; ".join(signal.reasons) or f"{signal.lane.value} fired",
            confidence=signal.strength,
            signals=[signal.signal_id],
            dossier_grade=dossier.grade,
            size_base_units=size,
            size_pct_bankroll=pct,
            invalidation=_invalidation(signal),
            params_version=PARAMS_VERSION,
        ),
        c,
    )


def _invalidation(signal: Signal) -> str:
    """What would prove this decision wrong. Written at entry, read at post-mortem."""
    payload = signal.payload or {}
    if payload.get("never_hold_through_migration"):
        return f"not out within {payload.get('sell_within_s')}s of migration"
    if signal.lane is Lane.TRUSTED_COPY:
        return "source wallet sells, or price drifts beyond the copy band"
    if signal.lane is Lane.CURVE_VELOCITY:
        return "curve velocity decays below the entry threshold or bundler share rises"
    if signal.lane is Lane.CONFLUENCE_5:
        return "entities net-sell, or the confluence turns out to be one entity"
    if signal.lane is Lane.LISTING_POP:
        return "no venue pop within the latency window"
    return "thesis conditions no longer hold"


def _persist(decision: Decision, conn: sqlite3.Connection) -> Decision:
    upsert(
        conn,
        "decisions",
        {
            "decision_id": decision.decision_id,
            "ts_ms": decision.ts_ms,
            "lane": decision.lane.value,
            "mode": decision.mode.value,
            "chain": decision.chain.value,
            "token": decision.token,
            "action": decision.action.value,
            "thesis": decision.thesis,
            "confidence": decision.confidence,
            "signals_json": jdump(decision.signals),
            "dossier_grade": decision.dossier_grade.value,
            "size_base_units": decision.size_base_units,
            "size_pct_bankroll": decision.size_pct_bankroll,
            "expected_return_pct": decision.expected_return_pct,
            "invalidation": decision.invalidation,
            "regime": decision.regime,
            "blockers_json": jdump(decision.blockers),
            "params_version": decision.params_version,
            "model": decision.model,
            "trace_id": decision.trace_id,
        },
        ["decision_id"],
    )
    emit(
        EventKind.DECISION,
        {
            "decision_id": decision.decision_id,
            "lane": decision.lane.value,
            "mode": decision.mode.value,
            "action": decision.action.value,
            "thesis": decision.thesis,
            "confidence": decision.confidence,
            "blockers": decision.blockers,
            "size_base_units": decision.size_base_units,
            "signals": decision.signals,
        },
        chain=decision.chain,
        subject=decision.token,
        level="info" if decision.action is not Action.SKIP else "debug",
        dedupe_key=f"decision:{decision.decision_id}",
        conn=conn,
    )
    return decision


# --------------------------------------------------------------------------------------
# run loop
# --------------------------------------------------------------------------------------


def _watermark(conn: sqlite3.Connection) -> tuple[int, str]:
    row = fetch_one(conn, "SELECT value FROM kv WHERE key=?", (WATERMARK_KEY,))
    if not row:
        return (0, "")
    data = jload(row["value"], {}) or {}
    return (int(data.get("created_ms", 0)), str(data.get("signal_id", "")))


def _set_watermark(conn: sqlite3.Connection, created_ms: int, signal_id: str) -> None:
    upsert(
        conn,
        "kv",
        {
            "key": WATERMARK_KEY,
            "value": jdump({"created_ms": created_ms, "signal_id": signal_id}),
            "updated_ms": now_ms(),
        },
        ["key"],
    )


def run_once(conn: sqlite3.Connection | None = None, *, limit: int = 200) -> list[Decision]:
    """Decide every signal recorded since the watermark, oldest first.

    The watermark is ``(created_ms, signal_id)`` rather than a bare timestamp so two
    signals written in the same millisecond cannot silently skip each other.
    """
    c = conn or get_conn()
    wm_ms, wm_id = _watermark(c)
    rows = fetch_all(
        c,
        "SELECT * FROM signals WHERE created_ms > ? OR (created_ms = ? AND signal_id > ?) "
        "ORDER BY created_ms ASC, signal_id ASC LIMIT ?",
        (wm_ms, wm_ms, wm_id, limit),
    )
    decisions: list[Decision] = []
    for row in rows:
        signal = signal_from_row(row)
        twins: list[Decision] = []
        decision = decide(signal, c, twins=twins)
        decisions.append(decision)
        if decision.action is Action.ENTER:
            try:
                handoff(decision, signal, c)
            except Exception:  # noqa: BLE001 - one bad handoff must not stall the watermark
                log.exception("handoff failed for %s", decision.decision_id)
        # A live entry refused for its launchpad is paper-traded instead (see
        # `_launchpad_refusal`). The twin is SHADOW, so `handoff` sends it to the paper
        # broker and never to the executor. Not appended to `decisions`: one decision
        # per signal is what callers count, and the twin has its own row and event.
        for twin in twins:
            try:
                handoff(twin, signal, c)
            except Exception:  # noqa: BLE001 - a paper twin must never stall the watermark
                log.exception("shadow twin handoff failed for %s", twin.decision_id)
        wm_ms, wm_id = int(row["created_ms"]), str(row["signal_id"])
    if rows:
        _set_watermark(c, wm_ms, wm_id)
    return decisions


def run_loop(
    conn: sqlite3.Connection | None = None,
    *,
    interval_s: float = 2.0,
    idle_interval_s: float = 10.0,
    limit: int = 200,
    max_passes: int | None = None,
    stop: Any = None,
    install_signals: bool = True,
) -> dict[str, Any]:
    """Decide signals until stopped. This is what ``kaiba engine run`` runs.

    It did not exist. ``main.py`` imported it at the top of the command body, so
    ``kaiba engine run`` and ``kaiba engine run --once`` both failed on the import and
    ``kaiba-engine.service`` was dead on arrival. The consequence was not just that the
    engine did not run: a tier-1 scan produced signals, nothing consumed them, and by the
    time anything did the dossier had aged past its 300 s budget and the decision was
    refused as stale. Six of thirteen live signals were lost that way.

    Two rates rather than one: a busy pass comes straight back for more, an empty pass
    waits longer. Deciding is cheap, but polling an empty table twice a second for weeks
    is the kind of idle burn nobody notices until it is the reason a disk filled up.
    """
    import threading
    import time as _time

    c = conn or get_conn()
    stop_event = stop if stop is not None else threading.Event()

    if install_signals:
        import signal as _signal

        def _on_signal(_sig: int, _frm: Any) -> None:
            stop_event.set()

        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                _signal.signal(sig, _on_signal)
            except (ValueError, OSError):  # pragma: no cover - not all threads/platforms
                pass

    totals = {"passes": 0, "decisions": 0, "entered": 0, "skipped": 0, "errors": 0}
    log.info("engine loop started (interval %.1fs, idle %.1fs)", interval_s, idle_interval_s)
    while not stop_event.is_set():
        if max_passes is not None and totals["passes"] >= max_passes:
            break
        totals["passes"] += 1
        try:
            decisions = run_once(c, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end the service
            totals["errors"] += 1
            log.exception("engine pass failed: %s", exc)
            decisions = []
        totals["decisions"] += len(decisions)
        totals["entered"] += sum(1 for d in decisions if d.action is not Action.SKIP)
        totals["skipped"] += sum(1 for d in decisions if d.action is Action.SKIP)

        if decisions:
            emit(
                EventKind.SYSTEM,
                {"service": "engine", "event": "pass", "decisions": len(decisions),
                 "entered": sum(1 for d in decisions if d.action is not Action.SKIP)},
                conn=c,
            )
        wait = interval_s if decisions else idle_interval_s
        if hasattr(stop_event, "wait"):
            stop_event.wait(wait)
        else:  # pragma: no cover - a plain sentinel object
            _time.sleep(wait)

    log.info("engine loop stopped: %s", totals)
    return totals


def _protectability_probe(chain: Chain, token: str) -> tuple[bool, str]:
    """Ask :func:`viability.quote_asset_is_protectable` whether the watchdog can see this.

    A seam of its own so a test can make it raise, and so the import sits on the entry
    path rather than at module scope: ``viability`` imports ``engine.load_dossier``
    lazily for the same cycle, and this module must stay importable while that one is
    being edited.
    """
    from kaiba.execution.viability import quote_asset_is_protectable

    return quote_asset_is_protectable(chain, token)


def _protection_refusal(decision: Decision) -> str | None:
    """Why this entry must not be opened, or ``None`` if it may be. Never raises.

    **Every chain.** This was Solana-only until 2026-09-23, on the reasoning that "EVM
    already enforces the same rule through the same function: ``quote_asset_rate`` probes
    the quote asset before it will rate one". That is true, and it is a DIFFERENT
    question. ``quote_asset_rate`` asks whether the **quote asset** can be priced; for a
    robinhood token quoted in native ETH the quote asset is ETH, which always can be, so
    the check passes and nothing ever asks whether the watchdog can price the position's
    OWN token.

    What that cost, measured: a live robinhood sm-trenches position
    (``0x9f06f1978809b119...``) filled, went blind, stayed blind past the 300 s budget
    (``longest_blind_s: 585``) and tripped ``protection_blind_timeout``, which halts
    entries on EVERY chain. A second robinhood position had been blind for 194 minutes. At
    that instant ``quote_asset_is_protectable`` answered ``(False,
    "protection_path_has_no_price")`` for the token and this function answered ``None``.
    The probe knew; the gate never asked it.

    The price of asking is one provider call per entry, at the entry rate (4-10 an hour)
    rather than the scan rate. A position that cannot be priced is a position whose stop
    cannot be evaluated, and the failsafe's only remaining move is to stop the whole agent
    trading -- which is what it did.

    **Fails CLOSED.** Every failure to answer — an import error while a sibling module is
    landing, a provider that raises, a stub that returns something other than ``True`` —
    is a refusal. "We could not check" and "we cannot see it" have the same consequence
    for a position: a stop that cannot be evaluated.
    """
    try:
        ok, why = _protectability_probe(decision.chain, decision.token)
    except Exception as exc:  # noqa: BLE001 - a gate that cannot run is not permission
        return f"protection_probe_unavailable:{type(exc).__name__}"
    if ok is True:
        return None
    # Not `if not ok`: only an explicit True is a pass, so a probe that returns None or
    # some truthy stand-in cannot be read as one.
    return str(why or "protection_path_has_no_price")


def handoff(decision: Decision, signal: Signal, conn: sqlite3.Connection) -> Order | None:
    """Shadow and canary fill on paper; live leaves a ``planned`` row for the executor.

    ``None`` on either side means the same thing: we could not price the trade honestly,
    so nothing was written. Paper refuses to invent a fill; live refuses to plan an order
    with no floor under it. Both record ``abandoned`` on the decision.

    **This is the entry path**, and the protectability gate below is here for that reason.
    ``run_once`` calls this only for an ``Action.ENTER`` decision and ``submit_agent_intent``
    calls it for an agent-requested one, so it runs at the entry rate (4-10 an hour on the
    live box, MEASURED 2026-09-22) rather than the scan rate (hundreds an hour). ``decide``
    would have been the wrong place for a provider call for exactly that reason.
    """
    # 0. can the thing that will have to PROTECT this position see it at all?
    #
    # MEASURED on the live box 2026-09-22: a direct probe of every live position right
    # after the router fallback landed found 4 of 4 priceable and 0 blind; ten minutes
    # later, with 10 open positions, blind-per-tick ran min 0, max 7, average 3.2. A
    # backlog being cleared falls; this rose, which is new entries arriving unpriceable.
    # A position that cannot be priced has a stop that cannot be evaluated, and
    # `protection.max_blind_halt_entries` then stops entries on EVERY chain.
    #
    # Before the paper/live split, so a shadow position is refused too: `open_positions`
    # selects on `closed_ms IS NULL` with no mode filter, so the watchdog polls a paper
    # position and counts it as blind exactly like a funded one.
    refusal = _protection_refusal(decision)
    if refusal is not None:
        emit(
            EventKind.SYSTEM,
            {
                "service": "engine",
                "event": "entry_unprotectable",
                "decision_id": decision.decision_id,
                "token": decision.token,
                "mode": decision.mode.value,
                "lane": decision.lane.value,
                "reason": refusal,
            },
            chain=decision.chain,
            subject=decision.token,
            level="warn",
            conn=conn,
        )
        mark_outcome(
            decision.decision_id,
            outcome="abandoned",
            note=(
                f"entry refused: the protection path cannot price {decision.token} "
                f"on {decision.chain.value}: {refusal}"
            )[:300],
            conn=conn,
        )
        log.warning(
            "entry for %s on %s refused: unprotectable (%s)",
            decision.token, decision.chain.value, refusal,
        )
        return None

    if decision.mode is LaneMode.LIVE:
        return _plan_live_order(decision, conn)

    dossier = load_dossier(decision.chain, decision.token, conn)
    price = dossier.price_usd.value if dossier and dossier.price_usd.known else None
    liquidity = dossier.liquidity_usd.value if dossier and dossier.liquidity_usd.known else None
    if price is None or liquidity is None:
        # No quote, no paper fill. Recording a fill at an imagined price is the exact
        # dishonesty the shadow lane exists to avoid.
        emit(
            EventKind.SYSTEM,
            {"decision_id": decision.decision_id, "reason": "paper_fill_skipped_no_quote"},
            chain=decision.chain,
            subject=decision.token,
            level="warn",
            conn=conn,
        )
        mark_outcome(decision.decision_id, outcome="abandoned", note="no price/liquidity quote", conn=conn)
        return None

    broker = PaperBroker(conn)
    order = broker.buy(decision, price_usd=price, liquidity_usd=liquidity, now_ms=decision.ts_ms)
    if order.state is OrderState.FILLED:
        position_id = position_for_order(conn, order.order_id)
        mark_outcome(decision.decision_id, position_id=position_id, order_id=order.order_id,
                     outcome="open", conn=conn)
        if position_id:
            try:
                arm_protection(position_id, conn)
            except Exception as exc:  # pragma: no cover - only while protection.py is landing
                log.warning("protection arm failed for %s: %s", position_id, exc)
    else:
        mark_outcome(decision.decision_id, order_id=order.order_id, outcome="rejected",
                     note=order.error, conn=conn)
    return order


# --------------------------------------------------------------------------------------
# min_out: the floor a live BUY is allowed to fill at
#
# Until 2026-09-21 this module wrote ``min_out=0`` on every planned live order. Two
# consequences, both measured:
#
# 1. Nothing ever traded. ``policy.check_gmgn_swap_body`` calls ``_positive_amount`` on
#    ``min_output_amount`` (policy.py:1105-1107), so every live buy the engine planned was
#    refused at the signer with ``gmgn_body_amount_invalid:min_output_amount``. 110
#    decisions, 0 live orders.
# 2. Had the policy not caught it, ``min_out=0`` is an *unbounded-slippage market order*:
#    it tells the venue "any output is acceptable", which is the exact shape a sandwich
#    bot is looking for. The policy was right and this module was wrong.
#
# The exit side already got this right — ``watchdog.DefaultExitSubmitter._min_out``
# (watchdog.py:695-714) converts quantity through a USD price into native atoms, applies
# the slippage band, and refuses rather than sending zero. This is the same arithmetic run
# the other way for the entry side, and it refuses the same way.
# --------------------------------------------------------------------------------------

#: Decimal context for the min_out arithmetic. Same shape as ``fills._CTX`` (fills.py:139)
#: and for the same reason: a memecoin at 1e-12 USD with 18 decimals produces an atom count
#: around 1e31, which needs more than the default 28 significant digits to stay exact.
#: Money is integers in base units and USD is Decimal; no float touches any of this.
_MIN_OUT_CTX = Context(prec=50, rounding=ROUND_HALF_EVEN)

#: How old a stored native/USD sample may be before this path takes a fresh one. 60_000 ms
#: is ``native_price.ensure_recent``'s own default, and that function is documented as the
#: one-shot form "for a caller about to submit a live order" — which is precisely here.
NATIVE_SAMPLE_MAX_AGE_MS = 60_000


@dataclass(frozen=True)
class MinOutPlan:
    """The floor, or the reason there isn't one. ``value is None`` means refuse the order.

    ``basis`` is ``UNAVAILABLE`` exactly when ``value`` is ``None``. There is no third
    state where a number is returned with a shrug attached: either we can price the trade
    or we do not send it.
    """

    value: int | None
    basis: EvidenceBasis
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.value is not None and self.value > 0 and self.basis is not EvidenceBasis.UNAVAILABLE


def _unpriceable(reason: str, **detail: Any) -> MinOutPlan:
    return MinOutPlan(value=None, basis=EvidenceBasis.UNAVAILABLE, reason=reason, detail=detail)


def _native_pricing_chain(chain: Chain) -> Chain | None:
    """Which chain's stored native samples price ``chain``'s native unit.

    A chain with its own reference pool prices itself. A chain without one is priced by
    another chain carrying the *same asset*: Robinhood Chain's native token is ETH, and
    ``kaiba.ingest.robinhood`` (robinhood.py:1053-1055) already establishes the reasoning —
    "ETH's price is not chain-specific, so the deep mainnet pools are a better source than
    anything quoted on a one-year-old L2". ARC and STABLE are ETH-native by the same table.

    Derived from ``NATIVE_SYMBOL`` and ``native_price.WRAPPED_NATIVE`` rather than written
    out here, so this cannot drift away from the two maps that are the actual authority. A
    chain whose native symbol has no reference pool anywhere returns ``None`` and its
    orders are refused — it does not borrow a price from a different asset.
    """
    try:
        from kaiba.providers.native_price import WRAPPED_NATIVE
    except Exception as exc:  # noqa: BLE001 - a missing provider is not a price
        log.debug("native_price unavailable: %s", exc)
        return None
    if chain in WRAPPED_NATIVE:
        return chain
    symbol = NATIVE_SYMBOL.get(chain)
    if not symbol:
        return None
    for candidate in WRAPPED_NATIVE:
        if NATIVE_SYMBOL.get(candidate) == symbol:
            return candidate
    return None


def _min_out_atoms(
    *,
    amount_in: int,
    native_usd: Decimal,
    token_usd: Decimal,
    native_decimals: int,
    token_decimals: int,
    slippage_bps: int,
) -> int | None:
    """Output-token atoms we insist on receiving, or ``None`` when the inputs cannot say.

    ``amount_in`` is native base units in, so the conversion is
    ``in/10^native_dec * native_usd / token_usd * 10^token_dec``, less the slippage band.
    Rounded **down**: a floor that was rounded up is not a floor.

    The band is ``bounds.max_slippage_bps`` (2500 = 25% in the shipped config), the same
    number written into ``order.slippage_bps`` and therefore into the body's ``slippage``
    field. They must be the same number — if the enforceable on-chain floor and the
    slippage we declare to the provider disagreed, one of them would be a lie. 25% is wide,
    deliberately: the dossier price is a pool mid, and the floor has to survive the LP fee,
    the provider fee and the price impact of our own size on a thin book. It is a ceiling
    on how bad a fill may be, not a target.
    """
    amount_in = int(amount_in)
    native_decimals = int(native_decimals)
    token_decimals = int(token_decimals)
    slippage_bps = int(slippage_bps)
    if amount_in <= 0 or native_usd <= 0 or token_usd <= 0:
        return None
    if native_decimals < 0 or token_decimals < 0:
        return None
    if not 0 <= slippage_bps < 10_000:
        # 10_000 bps is a 100% band, i.e. min_out=0 by arithmetic. Refuse rather than
        # compute our way back to the bug this function exists to fix.
        return None
    with localcontext(_MIN_OUT_CTX):
        spend_usd = Decimal(amount_in) / (Decimal(10) ** native_decimals) * Decimal(native_usd)
        expected_atoms = spend_usd / Decimal(token_usd) * (Decimal(10) ** token_decimals)
        floor_atoms = expected_atoms * (Decimal(10_000 - slippage_bps)) / Decimal(10_000)
        out = int(floor_atoms.to_integral_value(rounding=ROUND_FLOOR))
    # A sub-atom floor rounds to 0, and 0 is the unbounded market order. If our size buys
    # less than one atom of the token the trade is not worth sending anyway.
    return out if out > 0 else None


def _plan_min_out(decision: Decision, conn: sqlite3.Connection) -> MinOutPlan:
    """Price the buy from evidence we actually hold, or say why we cannot.

    Every input is fetched honestly and every miss is ``UNAVAILABLE``:

    * **token USD** from the decision's own dossier, re-read and re-freshness-checked here
      rather than trusted from ``decide()`` — this is the number the order is sized on.
    * **token decimals** from ``fills.token_decimals``, which reads the chain. Verified
      on chain or nothing: a decimals value that is wrong by ``d`` is wrong by ``10^|d|``,
      and in the *low* direction that manufactures a near-zero floor, which is the bug
      this whole function exists to prevent. Measured 2026-09-21: all 5,533 ``tokens``
      rows on the live VPS have ``decimals IS NULL``, so the chain read is not a fallback
      here, it is the only source.
    * **native USD** from ``native_price``: a stored sample, topped up first via
      ``ensure_recent`` so a live order is not floored on a price from an hour ago.

    The order of those three is deliberate and is the cheapest-first order, not the
    reading order. The dossier is a local row; decimals is a local row for a token the
    chain read has already cached (``ttl_s=86_400``, and a mint's decimals never change);
    the native top-up is the only step that can reach a provider on a warm cache. Spending
    a provider call to price SOL and *then* discovering we cannot size the floor anyway is
    a request we did not need to make, on the limiter budget an entry is competing for.
    """
    cfg = get_risk()
    slippage_bps = int(cfg.bounds.max_slippage_bps)
    amount_in = int(decision.size_base_units or 0)
    if amount_in <= 0:
        return _unpriceable("size_base_units_not_positive", amount_in=amount_in)

    dossier = load_dossier(decision.chain, decision.token, conn)
    if dossier is None:
        return _unpriceable("no_dossier")
    at_ms = now_ms()
    if _dossier_stale(dossier, at_ms):
        # decide() already checked this against its own clock. Checked again because the
        # order is written *now*, and a price we would refuse to decide on is a price we
        # must refuse to floor a live order on.
        return _unpriceable(
            "dossier_stale", age_s=round((at_ms - dossier.built_at_ms) / 1000.0, 1),
            budget_s=DOSSIER_MAX_AGE_S,
        )
    token_usd = dossier.price_usd.value if dossier.price_usd.known else None
    if token_usd is None or not token_usd.is_finite() or token_usd <= 0:
        return _unpriceable("token_price_unavailable", basis=dossier.price_usd.basis.value)
    # The dossier build clock is not the price observation clock. A refresh of
    # unrelated fields must not renew a cached/expired price for a funded plan.
    price = dossier.price_usd
    if price.receipt is None or price.freshness_budget_s <= 0:
        return _unpriceable("token_price_time_unavailable")
    price_age_ms = at_ms - price.receipt.observed_at_ms
    if price_age_ms < 0:
        return _unpriceable("token_price_future", observed_ms=price.receipt.observed_at_ms)
    if price.basis is EvidenceBasis.STALE or price_age_ms > price.freshness_budget_s * 1000:
        return _unpriceable("token_price_stale", age_s=price_age_ms / 1000,
                            budget_s=price.freshness_budget_s)

    decimals, dec_basis, dec_note = _token_decimals(decision.chain, decision.token, conn)
    if decimals is None:
        return _unpriceable("token_decimals_unavailable", note=dec_note)

    pricing_chain = _native_pricing_chain(decision.chain)
    if pricing_chain is None:
        return _unpriceable("no_native_price_source_for_chain", chain=decision.chain.value)
    native_usd, native_note = _native_usd(pricing_chain, conn, at_ms=at_ms)
    if native_usd is None:
        return _unpriceable(
            "native_price_unavailable", pricing_chain=pricing_chain.value, note=native_note,
        )
    final_price_age_ms = now_ms() - price.receipt.observed_at_ms
    if not 0 <= final_price_age_ms <= price.freshness_budget_s * 1000:
        return _unpriceable(
            "token_price_future" if final_price_age_ms < 0 else "token_price_stale",
            age_s=final_price_age_ms / 1000, budget_s=price.freshness_budget_s,
        )

    value = _min_out_atoms(
        amount_in=amount_in,
        native_usd=native_usd,
        token_usd=token_usd,
        native_decimals=NATIVE_DECIMALS[decision.chain],
        token_decimals=decimals,
        slippage_bps=slippage_bps,
    )
    if value is None or value <= 0:
        return _unpriceable(
            "min_out_not_representable", amount_in=amount_in, slippage_bps=slippage_bps,
        )
    return MinOutPlan(
        value=value,
        basis=EvidenceBasis.DERIVED,
        reason="derived_from_dossier_price_and_native_sample",
        detail={
            "token_usd": str(token_usd),
            "token_price_observed_ms": price.receipt.observed_at_ms,
            "token_price_freshness_budget_s": price.freshness_budget_s,
            "token_price_basis": price.basis.value,
            "token_price_provider": price.receipt.provider,
            "native_usd": str(native_usd),
            "native_pricing_chain": pricing_chain.value,
            "native_note": native_note,
            "token_decimals": decimals,
            "token_decimals_basis": dec_basis,
            "slippage_bps": slippage_bps,
        },
    )


def _native_usd(chain: Chain, conn: sqlite3.Connection, *, at_ms: int) -> tuple[Decimal | None, str]:
    """``(price, note)`` for one native unit in USD. ``None`` when no sample is contemporaneous.

    ``ensure_recent`` first, because the sampler may not be running and a live order
    floored on a stale native price is floored on the wrong market. Then ``at()``, which
    is the same accessor ``fills`` prices a settled fill with — so the floor we accept and
    the entry we later record come from one number rather than two.
    """
    try:
        from kaiba.providers import native_price
    except Exception as exc:  # noqa: BLE001 - a missing provider is not a price
        return None, f"native_price import failed: {type(exc).__name__}"
    try:
        native_price.ensure_recent(chain, conn, max_age_ms=NATIVE_SAMPLE_MAX_AGE_MS)
    except Exception as exc:  # noqa: BLE001 - a failed top-up is not fatal; a stale read is caught below
        log.debug("native price top-up failed for %s: %s", chain.value, exc)
    try:
        quote = native_price.at(chain, at_ms, conn)
    except Exception as exc:  # noqa: BLE001
        return None, f"native_price.at failed: {type(exc).__name__}"
    if not quote.known or quote.price_usd is None or quote.price_usd <= 0:
        return None, (quote.receipt.note or "no contemporaneous native sample")[:200]
    return quote.price_usd, f"sample {quote.distance_ms} ms away"


def _token_decimals(chain: Chain, token: str, conn: sqlite3.Connection) -> tuple[int | None, str, str]:
    """``(decimals, basis, note)`` — verified on chain, or nothing.

    ``fills.token_decimals`` deliberately returns an unverified ``tokens``-row value with a
    note rather than refusing, because its own caller is accounting and a slightly wrong
    accounting number is still worth having. This caller is not accounting: it is sizing a
    floor on real money, where a provider-supplied decimals that is off by six turns a
    correct floor into a floor a million times too small. So only ``verified_onchain`` is
    accepted here.
    """
    try:
        from kaiba.execution import fills
    except Exception as exc:  # noqa: BLE001
        return None, "unavailable", f"fills import failed: {type(exc).__name__}"
    try:
        decimals, basis, note = fills.token_decimals(chain, token, conn, fetch=True, register=False)
    except Exception as exc:  # noqa: BLE001 - a dead RPC refuses the order, it does not raise here
        return None, "unavailable", f"decimals lookup failed: {type(exc).__name__}: {exc}"[:200]
    if decimals is None:
        return None, basis, note or "decimals unavailable"
    if basis != fills.DECIMALS_VERIFIED:
        return None, basis, f"decimals not verified on chain ({basis}); {note or ''}".strip()[:200]
    return int(decimals), basis, note or ""


def _plan_live_order(decision: Decision, conn: sqlite3.Connection) -> Order | None:
    """Write the intent and stop. The executor, not this module, talks to a venue.

    Returns ``None`` when the buy cannot be priced. No order row is written in that case:
    an order with no floor is one the signer will refuse anyway, and leaving it in
    ``planned`` would only give the executor something to trip over every sweep. The
    decision outcome records ``abandoned`` with the reason, so the refusal is on the
    record and countable rather than silent.
    """
    cfg = get_risk()
    plan = _plan_min_out(decision, conn)
    if not plan.usable or plan.value is None:
        emit(
            EventKind.SYSTEM,
            {
                "service": "engine",
                "event": "live_order_unpriced",
                "decision_id": decision.decision_id,
                "reason": plan.reason,
                # `**plan.detail` LAST would win, and _plan_min_out puts a key literally
                # named "basis" into detail (the dossier's own price basis). Found by
                # adversarial review 2026-09-21: the emitted payload read
                # {"reason": "token_price_unavailable", "basis": "provider_reported"} on a
                # refusal whose real basis was UNAVAILABLE -- a reassuring label on the
                # audit record for a trade we could not price, which is exactly what this
                # codebase forbids. No money effect (the order is refused either way), but
                # the record is the point. Spread detail FIRST so the plan's own basis wins,
                # and keep the inner one under an unambiguous name rather than dropping it.
                **{("detail_" + k if k in ("basis", "reason", "service", "event") else k): v
                   for k, v in plan.detail.items()},
                "basis": plan.basis.value,
            },
            chain=decision.chain,
            subject=decision.token,
            level="warn",
            conn=conn,
        )
        mark_outcome(
            decision.decision_id,
            outcome="abandoned",
            note=f"live order not planned: {plan.reason}"[:300],
            conn=conn,
        )
        log.warning(
            "live order for %s on %s refused: %s (%s)",
            decision.token, decision.chain.value, plan.reason, plan.detail,
        )
        return None

    order = Order(
        order_id="ord_" + digest({"decision_id": decision.decision_id, "side": "buy"})[:24],
        decision_id=decision.decision_id,
        chain=decision.chain,
        token=decision.token,
        side=Side.BUY,
        lane=decision.lane,
        mode=decision.mode,
        input_token=SOL_NATIVE_MINT if decision.chain is Chain.SOL else EVM_ZERO,
        output_token=decision.token,
        amount_in=int(decision.size_base_units or 0),
        # Never 0. See _plan_min_out: 0 means "accept any output", which is an
        # unbounded-slippage market order and is refused by policy.check_gmgn_swap_body.
        min_out=plan.value,
        slippage_bps=int(cfg.bounds.max_slippage_bps),
        state=OrderState.PLANNED,
        # `provider` is how kaiba.execution.executor picks a submission lane; GMGN is the
        # default route in PLAN 6.3. The executor, not this module, decides to send.
        provider="gmgn",
        created_ms=decision.ts_ms,
        updated_ms=decision.ts_ms,
    )
    conn.execute(
        "INSERT OR IGNORE INTO orders (order_id, decision_id, chain, token, side, lane, mode, input_token, "
        "output_token, amount_in, min_out, slippage_bps, state, provider, created_ms, updated_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            order.order_id,
            order.decision_id,
            order.chain.value,
            order.token,
            order.side.value,
            order.lane.value,
            order.mode.value,
            order.input_token,
            order.output_token,
            str(order.amount_in),
            str(order.min_out),
            order.slippage_bps,
            order.state.value,
            order.provider,
            order.created_ms,
            order.updated_ms,
        ),
    )
    conn.execute(
        "INSERT INTO order_events (order_id, ts_ms, state, detail) VALUES (?,?,?,?)",
        (
            order.order_id,
            order.updated_ms,
            order.state.value,
            # The floor's provenance goes on the order's own timeline, not only in the
            # event bus: when a fill comes back worse than expected, the first question is
            # what we told the venue and what we priced it from.
            (
                "planned by engine; executor owns submission; "
                f"min_out={order.min_out} atoms basis={plan.basis.value} {jdump(plan.detail)}"
            )[:500],
        ),
    )
    mark_outcome(decision.decision_id, order_id=order.order_id, outcome="planned", conn=conn)
    return order


# --------------------------------------------------------------------------------------
# outcomes
# --------------------------------------------------------------------------------------


def mark_outcome(
    decision_id: str,
    *,
    position_id: str | None = None,
    trade_id: str | None = None,
    order_id: str | None = None,
    outcome: str | None = None,
    pnl_native: int | None = None,
    pnl_pct: float | None = None,
    note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Link a decision to what happened next. Fields left ``None`` keep their old value."""
    c = conn or get_conn()
    existing = fetch_one(c, "SELECT * FROM decision_outcomes WHERE decision_id=?", (decision_id,)) or {}
    row = {
        "decision_id": decision_id,
        "position_id": position_id if position_id is not None else existing.get("position_id"),
        "trade_id": trade_id if trade_id is not None else existing.get("trade_id"),
        "order_id": order_id if order_id is not None else existing.get("order_id"),
        "outcome": outcome if outcome is not None else existing.get("outcome"),
        "pnl_native": str(pnl_native) if pnl_native is not None else existing.get("pnl_native"),
        "pnl_pct": pnl_pct if pnl_pct is not None else existing.get("pnl_pct"),
        "note": note if note is not None else existing.get("note"),
        "linked_ms": now_ms(),
    }
    upsert(c, "decision_outcomes", row, ["decision_id"])
    return row


def load_decision(decision_id: str, conn: sqlite3.Connection | None = None) -> Decision | None:
    c = conn or get_conn()
    row = fetch_one(c, "SELECT * FROM decisions WHERE decision_id=?", (decision_id,))
    if not row:
        return None
    return Decision(
        decision_id=row["decision_id"],
        ts_ms=row["ts_ms"],
        lane=Lane(row["lane"]),
        mode=LaneMode(row["mode"]),
        chain=Chain(row["chain"]),
        token=row["token"],
        action=Action(row["action"]),
        thesis=row["thesis"] or "",
        confidence=row["confidence"] if row["confidence"] is not None else 0.5,
        signals=jload(row["signals_json"], []),
        dossier_grade=Grade(row["dossier_grade"] or "UNSCORED"),
        size_base_units=row["size_base_units"],
        size_pct_bankroll=row["size_pct_bankroll"],
        expected_return_pct=row["expected_return_pct"],
        invalidation=row["invalidation"],
        regime=row["regime"],
        blockers=jload(row["blockers_json"], []),
        params_version=row["params_version"],
        model=row["model"],
        trace_id=row["trace_id"],
    )


# --------------------------------------------------------------------------------------
# agent intent
#
# The model can read a dossier, grade a wallet and propose an experiment, but until now it
# could not ask for a position. Authority you cannot exercise is not authority, so this is
# the entry point the MCP tool calls.
#
# It deliberately adds no new path to a venue. An agent intent becomes a Signal and goes
# through exactly the same decide() -> handoff() sequence as an automated one, which means
# it inherits every gate: the kill switch, entries_paused, reduce_only, the chain budget,
# the dossier freshness requirement, the risk gate and the size ladder. There is no
# argument the model can pass to skip any of them.
#
# Two choices worth defending:
#
# 1. **The intent always runs on Lane.MANUAL, whatever lane the model names.** Letting the
#    model pick the lane would let it pick the lane's *mode*, so it could route an
#    unsupported hunch through a lane the operator had promoted to LIVE on the strength of
#    a backtest that has nothing to do with this trade. The lane the model names is kept as
#    a reason, because its reasoning is worth reading, but the authority comes from the
#    MANUAL dial, which the operator sets on its own.
#
# 2. **The requested size is a request.** position_size() and the risk gate decide the real
#    number. A model that asks for ten times the envelope gets the envelope, and the
#    difference is recorded so an operator can see it asked.
# --------------------------------------------------------------------------------------


AGENT_INTENT_SOURCE = "agent_intent"


def submit_agent_intent(
    chain: Chain,
    token: str,
    lane: Lane | str | None = None,
    size_base_units: int | None = None,
    thesis: str = "",
    *,
    strength: float = 0.5,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Let the agent ask for an entry, through every gate an automated signal passes.

    Returns a plain dict describing what happened: the decision, its action, the refusal
    reason if it was skipped, and the order id if one was created. Never raises for an
    ordinary refusal — a refusal is the answer, not an error.
    """
    c = conn or get_conn()
    try:
        chain = Chain(chain)
    except ValueError:
        return {"ok": False, "reason": f"unknown chain {chain!r}"}

    token = normalize_address(str(token), chain)
    requested_lane: str | None = None
    if lane is not None:
        try:
            requested_lane = Lane(lane).value
        except ValueError:
            requested_lane = str(lane)[:40]

    reasons = [f"agent_intent:{thesis[:300]}" if thesis else "agent_intent"]
    if requested_lane and requested_lane != Lane.MANUAL.value:
        reasons.append(f"agent_named_lane:{requested_lane}")

    requested = max(0, int(size_base_units or 0))
    signal = Signal(
        signal_id="sig_" + digest(
            {"src": AGENT_INTENT_SOURCE, "chain": chain.value, "token": token,
             "thesis": thesis[:300], "ts": now_ms() // 60_000}
        )[:24],
        lane=Lane.MANUAL,
        chain=chain,
        token=token,
        strength=max(0.0, min(1.0, float(strength))),
        reasons=reasons,
        payload={
            "source": AGENT_INTENT_SOURCE,
            "requested_size_base_units": requested,
            "requested_lane": requested_lane,
            "thesis": thesis[:500],
        },
    )
    _persist_signal(signal, c)

    decision = decide(signal, c)
    out: dict[str, Any] = {
        "ok": decision.action is not Action.SKIP,
        "decision_id": decision.decision_id,
        "action": decision.action.value,
        "mode": decision.mode.value,
        "lane": decision.lane.value,
        "chain": chain.value,
        "token": token,
        "size_base_units": int(decision.size_base_units or 0),
        "requested_size_base_units": requested,
        "blockers": list(decision.blockers or []),
        "thesis": decision.thesis,
        "confidence": decision.confidence,
    }
    if requested and int(decision.size_base_units or 0) != requested:
        out["size_note"] = "size set by the risk ladder, not by the request"

    if decision.action is Action.SKIP:
        journal.append(
            "observation",
            f"agent intent refused for {token[:16]} on {chain.value}: {decision.thesis}",
            subject=token, conn=c,
        )
        return out

    order = handoff(decision, signal, c)
    if order is not None:
        out["order_id"] = order.order_id
        out["order_state"] = order.state.value
    else:
        out["order_id"] = None
        out["order_state"] = None
        out["note"] = "no order created; see the decision outcome"
    journal.append(
        "change",
        f"agent intent accepted for {token[:16]} on {chain.value} "
        f"({decision.mode.value}, {out['size_base_units']} base units)",
        subject=token, conn=c,
    )
    return out


def _persist_signal(signal: Signal, conn: sqlite3.Connection) -> None:
    """Store the signal so the decision row has something to point at."""
    try:
        upsert(
            conn,
            "signals",
            {
                "signal_id": signal.signal_id,
                "lane": signal.lane.value,
                "chain": signal.chain.value,
                "token": signal.token,
                "strength": signal.strength,
                "reasons_json": jdump(list(signal.reasons)),
                "wallets_json": jdump(list(signal.wallets)),
                "entities_json": jdump(list(signal.entities)),
                "window_s": signal.window_s,
                "created_ms": signal.created_ms,
                "payload_json": jdump(dict(signal.payload)),
            },
            conflict=["signal_id"],
            update=["strength", "reasons_json", "payload_json"],
        )
    except Exception as exc:  # noqa: BLE001 - a storage hiccup must not lose the intent
        log.warning("could not persist agent signal %s: %s", signal.signal_id, exc)
