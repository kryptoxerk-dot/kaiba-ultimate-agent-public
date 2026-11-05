"""The nightly reflection job: build a packet, mask it, validate what comes back.

Three ideas are load-bearing here.

**1. Skips are the training data.** A journal of winners teaches nothing. The review
packet carries every closed trade *and* every SKIP decision in the window, with what the
token subsequently did where we can observe it. Standing aside correctly and standing
aside expensively look identical in a PnL curve and completely different here.

**2. The packet is data, never prose.** Nothing a provider wrote reaches the model. Token
names, symbols and any provider-supplied text are dropped; addresses, enum values and
numbers survive. Agent-authored free text (a thesis, a lesson) is scrubbed to a single
line and length-capped. A reflection prompt that quotes provider prose is a
prompt-injection surface, and the thing on the other end of it can propose parameter
changes.

**3. Masking, because names make models tell stories.** Per the KTD-Fin finding, a model
shown real token identities reasons from what it remembers about the name instead of from
the factors in front of it. :func:`mask` replaces every token identity with a stable
pseudonym (``TOKEN_A``) and keeps the mapping locally so proposals can be un-masked
afterwards. The model never sees which token it is talking about.

And one rule about the output: **a malformed model response is rejected, never repaired.**
A truncated JSON blob that we "fix up" is how a half-formed idea becomes a live parameter
change. :func:`apply_reflection` writes lessons to the journal, deltas to the playbook and
parameter ideas to ``experiments`` as *proposals*. It does not touch ``config/risk.yaml``;
only :func:`kaiba.learning.gates.promote` does, and only after the deterministic gates.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from kaiba.core import journal
from kaiba.core.config import EnvelopeBounds
from kaiba.core.db import fetch_all, get_conn, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import (
    MISTAKE_TAGS,
    Action,
    EventKind,
    Lane,
    LaneMode,
    digest,
    now_ms,
)
from kaiba.learning import metrics, playbook

log = logging.getLogger(__name__)

MAX_LESSON_CHARS = 280
MAX_LESSONS = 5
MAX_PLAYBOOK_DELTAS = 3
MAX_PARAM_PROPOSALS = 2
MAX_THESIS_CHARS = 240

#: How far after a SKIP we look for evidence of what the token then did.
SKIP_OUTCOME_HORIZON_MS = 24 * 3600 * 1000

#: Parameter keys the reflection job may never propose. The operator's envelope and the
#: lane's promotion state are not tunables — a model that could propose widening
#: ``max_size_pct_bankroll`` would have found the shortest path to more risk.
FORBIDDEN_PARAM_KEYS: frozenset[str] = frozenset(EnvelopeBounds.model_fields) | {
    "mode",
    "kill_switch",
    "entries_paused",
    "reduce_only",
    "global_mode",
    "bounds",
    "wallet",
    "bankroll_base_units",
}

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PRINTABLE_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ReflectionRejected(ValueError):
    """The model's response was not usable. Carries every reason, repairs nothing."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def _scrub(text: Any, limit: int = MAX_THESIS_CHARS) -> str | None:
    """One line, printable, capped. Applied to every free-text field in the packet."""
    if text is None:
        return None
    cleaned = _PRINTABLE_RE.sub(" ", str(text))
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:limit]


# --------------------------------------------------------------------------------------
# the review packet
# --------------------------------------------------------------------------------------


class TradeRecord(BaseModel):
    """A closed round trip. Numbers, enums, addresses — nothing a provider wrote."""

    trade_id: str
    decision_id: str | None = None
    lane: str
    mode: str
    chain: str
    token: str
    opened_ms: int
    closed_ms: int
    hold_s: int
    cost_native: int
    pnl_native: int
    return_frac: float | None = None
    mae_pct: float | None = None
    mfe_pct: float | None = None
    exit_reason: str | None = None
    mistakes: list[str] = Field(default_factory=list)
    lesson: str | None = None
    params_version: str = "v1"


class SkipRecord(BaseModel):
    """What we passed on, and what it did afterwards if we can observe it.

    ``outcome_basis`` is ``"unavailable"`` when nothing in our own record tells us what
    happened. That is the honest answer far more often than not, and it must not read as
    "the skip was fine".
    """

    decision_id: str
    ts_ms: int
    lane: str
    mode: str
    chain: str
    token: str
    confidence: float | None = None
    regime: str | None = None
    dossier_grade: str | None = None
    blockers: list[str] = Field(default_factory=list)
    thesis: str | None = None
    outcome_basis: Literal["observed_trade", "unavailable"] = "unavailable"
    subsequent_return_frac: float | None = None
    subsequent_trades: int = 0


class ReviewPacket(BaseModel):
    """Everything the reflection model is allowed to see, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    since_ms: int
    until_ms: int
    generated_ms: int = Field(default_factory=now_ms)
    mode: str | None = None
    masked: bool = False
    trades: list[TradeRecord] = Field(default_factory=list)
    skips: list[SkipRecord] = Field(default_factory=list)
    lane_stats: dict[str, metrics.LaneStats] = Field(default_factory=dict)
    regime_stats: dict[str, metrics.LaneStats] = Field(default_factory=dict)
    mistake_counts: dict[str, int] = Field(default_factory=dict)
    calibration: dict[str, Any] = Field(default_factory=dict)
    playbook: list[dict[str, Any]] = Field(default_factory=list)
    mistake_vocabulary: list[str] = Field(default_factory=lambda: sorted(MISTAKE_TAGS))
    counts: dict[str, int] = Field(default_factory=dict)

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class MaskedReview(BaseModel):
    """A packet with token identities replaced, plus the mapping needed to undo it."""

    model_config = ConfigDict(extra="forbid")

    packet: ReviewPacket
    mapping: dict[str, str] = Field(default_factory=dict)  # pseudonym -> chain:address
    reverse: dict[str, str] = Field(default_factory=dict)  # chain:address -> pseudonym


def _skip_outcome(
    conn: sqlite3.Connection,
    chain: str,
    token: str,
    after_ms: int,
    horizon_ms: int,
) -> tuple[str, float | None, int]:
    """What happened to a token we passed on, from our own recorded trades only.

    We do not reconstruct a price series here: that would need a provider call at review
    time, and a number fetched tonight is not what we would have got then. If some lane
    did trade the token inside the horizon, its realised return is the evidence; if not,
    we say we do not know.
    """
    rows = fetch_all(
        conn,
        "SELECT * FROM trades WHERE chain = ? AND token = ? AND opened_ms >= ? AND opened_ms <= ?",
        (chain, token, after_ms, after_ms + horizon_ms),
    )
    if not rows:
        return "unavailable", None, 0
    rets = metrics.returns_of(rows)
    if not rets:
        return "unavailable", None, len(rows)
    avg = sum(rets) / len(rets)
    return "observed_trade", float(avg), len(rows)


def build_review(
    conn: sqlite3.Connection | None = None,
    since_ms: int = 0,
    until_ms: int | None = None,
    *,
    mode: LaneMode | str | None = None,
    skip_outcome_horizon_ms: int = SKIP_OUTCOME_HORIZON_MS,
    playbook_lane: Lane | str | None = None,
) -> ReviewPacket:
    """Assemble the nightly review packet for ``[since_ms, until_ms]``."""
    c = conn or get_conn()
    until = until_ms if until_ms is not None else now_ms()
    mode_v = mode.value if isinstance(mode, LaneMode) else (str(mode) if mode else None)

    trade_rows = metrics.load_trades(c, since_ms=since_ms, until_ms=until, mode=mode_v)
    trades: list[TradeRecord] = []
    for r in trade_rows:
        ret = metrics.trade_return(r)
        trades.append(
            TradeRecord(
                trade_id=str(r["trade_id"]),
                decision_id=r.get("decision_id"),
                lane=str(r["lane"]),
                mode=str(r["mode"]),
                chain=str(r["chain"]),
                token=str(r["token"]),
                opened_ms=int(r["opened_ms"]),
                closed_ms=int(r["closed_ms"]),
                hold_s=int(r["hold_s"]),
                cost_native=int(metrics.cost_native(r)),
                pnl_native=int(metrics.pnl_native(r)),
                return_frac=float(ret) if ret is not None else None,
                mae_pct=r.get("mae_pct"),
                mfe_pct=r.get("mfe_pct"),
                exit_reason=_scrub(r.get("exit_reason"), 64),
                mistakes=[t for t in (r.get("mistakes") or []) if t in MISTAKE_TAGS],
                lesson=_scrub(r.get("lesson"), MAX_LESSON_CHARS),
                params_version=str(r.get("params_version") or "v1"),
            )
        )

    sql = "SELECT * FROM decisions WHERE action = ? AND ts_ms >= ? AND ts_ms <= ?"
    params: list[Any] = [Action.SKIP.value, since_ms, until]
    if mode_v:
        sql += " AND mode = ?"
        params.append(mode_v)
    sql += " ORDER BY ts_ms ASC"
    skips: list[SkipRecord] = []
    for d in fetch_all(c, sql, params):
        basis, ret, n = _skip_outcome(
            c, str(d["chain"]), str(d["token"]), int(d["ts_ms"]), skip_outcome_horizon_ms
        )
        skips.append(
            SkipRecord(
                decision_id=str(d["decision_id"]),
                ts_ms=int(d["ts_ms"]),
                lane=str(d["lane"]),
                mode=str(d["mode"]),
                chain=str(d["chain"]),
                token=str(d["token"]),
                confidence=d.get("confidence"),
                regime=_scrub(d.get("regime"), 32),
                dossier_grade=d.get("dossier_grade"),
                blockers=[str(b)[:48] for b in jload(d.get("blockers_json"), [])][:12],
                thesis=_scrub(d.get("thesis")),
                outcome_basis=basis,  # type: ignore[arg-type]
                subsequent_return_frac=ret,
                subsequent_trades=n,
            )
        )

    lane_stats = metrics.by_lane(c, since_ms, mode_v, until_ms=until)
    regime_stats = metrics.by_regime(c, since_ms, mode_v, until_ms=until)
    mistake_stats = metrics.by_mistake_tag(c, since_ms, mode_v, until_ms=until)
    points = metrics.calibration_points(c, since_ms=since_ms, until_ms=until, mode=mode_v)

    return ReviewPacket(
        since_ms=since_ms,
        until_ms=until,
        mode=mode_v,
        trades=trades,
        skips=skips,
        lane_stats={k.value: v for k, v in lane_stats.items()},
        regime_stats=regime_stats,
        mistake_counts={k: v.count for k, v in mistake_stats.items()},
        calibration={
            "n": len(points),
            "brier": metrics.brier_score(points),
            "brier_skill_score": metrics.brier_skill_score(points),
            "base_rate": metrics.base_rate(points),
            "buckets": metrics.calibration_buckets(points),
        },
        playbook=[
            {
                "rule_id": r["rule_id"],
                "lane": r["lane"],
                "text": r["text"],
                "hits": int(r["hits"]),
                "misses": int(r["misses"]),
            }
            for r in playbook.active_rules(playbook_lane, c)
        ],
        counts={"trades": len(trades), "skips": len(skips)},
    )


# --------------------------------------------------------------------------------------
# masking
# --------------------------------------------------------------------------------------


def _pseudonym(index: int) -> str:
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"TOKEN_{letters}"


def mask(packet: ReviewPacket) -> MaskedReview:
    """Replace every token identity with a stable pseudonym.

    Assignment is by sorted ``chain:address`` so the same packet always produces the same
    map — a reflection run must be reproducible from its inputs. Chains, lanes, modes,
    mistake tags and every number are left alone: those are the factors the model is
    supposed to reason from.
    """
    keys = sorted({f"{t.chain}:{t.token}" for t in packet.trades} | {f"{s.chain}:{s.token}" for s in packet.skips})
    reverse = {key: _pseudonym(i) for i, key in enumerate(keys)}
    mapping = {v: k for k, v in reverse.items()}

    copy = packet.model_copy(deep=True)
    for t in copy.trades:
        t.token = reverse[f"{t.chain}:{t.token}"]
    for s in copy.skips:
        s.token = reverse[f"{s.chain}:{s.token}"]
    copy.masked = True
    return MaskedReview(packet=copy, mapping=mapping, reverse=reverse)


def unmask_text(text: str, masked: MaskedReview) -> str:
    """Put the real ``chain:address`` back into a string the model wrote."""
    out = text
    for pseudo, real in sorted(masked.mapping.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(pseudo, real)
    return out


# --------------------------------------------------------------------------------------
# what the model is allowed to return
# --------------------------------------------------------------------------------------


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    mistake_tag: str
    lane: Lane | None = None
    evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: str) -> str:
        cleaned = " ".join(str(v).split())
        if not cleaned:
            raise ValueError("lesson text is empty")
        if len(cleaned) > MAX_LESSON_CHARS:
            raise ValueError(f"lesson is {len(cleaned)} chars, limit is {MAX_LESSON_CHARS}")
        return cleaned

    @field_validator("mistake_tag")
    @classmethod
    def _check_tag(cls, v: str) -> str:
        tag = str(v).strip()
        if tag not in MISTAKE_TAGS:
            raise ValueError(f"mistake tag {tag!r} is not in the fixed vocabulary")
        return tag


class PlaybookDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["add", "retire"]
    text: str | None = None
    rule_id: str | None = None
    lane: Lane | None = None
    reason: str | None = None
    evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("text")
    @classmethod
    def _check_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = " ".join(str(v).split())
        if len(cleaned) > playbook.MAX_RULE_CHARS:
            raise ValueError(f"rule is {len(cleaned)} chars, limit is {playbook.MAX_RULE_CHARS}")
        return cleaned or None

    @model_validator(mode="after")
    def _check_shape(self) -> PlaybookDelta:
        if self.op == "add" and not self.text:
            raise ValueError("playbook add needs text")
        if self.op == "retire" and not self.rule_id:
            raise ValueError("playbook retire needs rule_id")
        return self


class ParamProposal(BaseModel):
    """A single-key parameter change. A proposal, never an application."""

    model_config = ConfigDict(extra="forbid")

    lane: Lane
    key: str
    old: float | int | bool | str | None = None
    new: float | int | bool | str
    rationale: str

    @field_validator("key")
    @classmethod
    def _check_key(cls, v: str) -> str:
        key = str(v).strip()
        if not _KEY_RE.match(key):
            raise ValueError(f"parameter key {key!r} is not a plain lowercase identifier")
        if key in FORBIDDEN_PARAM_KEYS:
            raise ValueError(f"parameter key {key!r} is operator-owned and cannot be proposed")
        return key

    @field_validator("rationale")
    @classmethod
    def _check_rationale(cls, v: str) -> str:
        cleaned = " ".join(str(v).split())
        if not cleaned:
            raise ValueError("param proposal needs a rationale")
        return cleaned[:MAX_LESSON_CHARS]

    @model_validator(mode="after")
    def _check_change(self) -> ParamProposal:
        if self.old is not None and self.old == self.new:
            raise ValueError("param proposal does not change anything")
        return self


class ReflectionResult(BaseModel):
    """The only shape a reflection response may take. Extra keys are a rejection."""

    model_config = ConfigDict(extra="forbid")

    lessons: list[Lesson] = Field(default_factory=list, max_length=MAX_LESSONS)
    playbook_deltas: list[PlaybookDelta] = Field(default_factory=list, max_length=MAX_PLAYBOOK_DELTAS)
    param_proposals: list[ParamProposal] = Field(default_factory=list, max_length=MAX_PARAM_PROPOSALS)
    summary: str | None = None

    @field_validator("summary")
    @classmethod
    def _check_summary(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return " ".join(str(v).split())[:1000] or None


def parse_reflection(payload: str | Mapping[str, Any]) -> ReflectionResult:
    """Validate a raw model response. Raises :class:`ReflectionRejected` — never repairs.

    A truncated or malformed response is a failed reflection, not a puzzle to solve. The
    cost of guessing is a live parameter change built on half a sentence.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ReflectionRejected([f"response is not valid JSON: {exc.msg} at char {exc.pos}"]) from exc
    else:
        data = payload
    if not isinstance(data, Mapping):
        raise ReflectionRejected([f"response is a {type(data).__name__}, expected an object"])
    try:
        return ReflectionResult.model_validate(dict(data))
    except ValidationError as exc:
        reasons = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
        ]
        raise ReflectionRejected(reasons) from exc


class ApplyReport(BaseModel):
    lessons_written: int = 0
    rules_added: list[str] = Field(default_factory=list)
    rules_retired: list[str] = Field(default_factory=list)
    experiments_created: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


def apply_reflection(
    result: ReflectionResult,
    conn: sqlite3.Connection | None = None,
    *,
    masked: MaskedReview | None = None,
    run_id: str | None = None,
) -> ApplyReport:
    """Persist a validated reflection. Proposals only — nothing reaches ``risk.yaml``.

    Lessons become journal entries, playbook deltas become playbook rows, parameter ideas
    become ``experiments`` rows with status ``proposed``. The only code that writes a
    parameter into ``config/risk.yaml`` is :func:`kaiba.learning.gates.promote`, after the
    replay and shadow gates have both passed. That separation is the point of the whole
    module: the thing that proposes a change is not the thing that approves it.
    """
    if not isinstance(result, ReflectionResult):
        raise ReflectionRejected(["apply_reflection requires a validated ReflectionResult"])
    c = conn or get_conn()
    report = ApplyReport()
    ts = now_ms()

    for lesson in result.lessons:
        if lesson.mistake_tag not in MISTAKE_TAGS:  # defence in depth: validators can be bypassed
            report.skipped.append(f"lesson tag {lesson.mistake_tag!r} outside vocabulary")
            continue
        text = unmask_text(lesson.text, masked) if masked else lesson.text
        body = f"[{lesson.mistake_tag}] {text}"
        journal.append(
            "lesson",
            body,
            subject=lesson.lane.value if lesson.lane else None,
            refs=[unmask_text(e, masked) if masked else e for e in lesson.evidence],
            conn=c,
        )
        report.lessons_written += 1

    for delta in result.playbook_deltas:
        if delta.op == "add" and delta.text:
            text = unmask_text(delta.text, masked) if masked else delta.text
            rid = playbook.add_rule(
                text,
                lane=delta.lane,
                evidence=[unmask_text(e, masked) if masked else e for e in delta.evidence],
                conn=c,
            )
            report.rules_added.append(rid)
        elif delta.op == "retire" and delta.rule_id:
            if playbook.retire(delta.rule_id, delta.reason or "retired by reflection", conn=c):
                report.rules_retired.append(delta.rule_id)
            else:
                report.skipped.append(f"retire: unknown or already retired rule {delta.rule_id}")

    for prop in result.param_proposals:
        if prop.key in FORBIDDEN_PARAM_KEYS:
            report.skipped.append(f"param {prop.key!r} is operator-owned")
            continue
        diff = {
            "lane": prop.lane.value,
            "key": prop.key,
            "old": prop.old,
            "new": prop.new,
            "rationale": prop.rationale,
        }
        experiment_id = "exp_" + digest({"diff": diff, "ts": ts, "run": run_id})[:16]
        c.execute(
            "INSERT OR IGNORE INTO experiments (experiment_id, created_ms, lane, hypothesis, "
            "diff_json, status) VALUES (?,?,?,?,?,'proposed')",
            (experiment_id, ts, prop.lane.value, prop.rationale, jdump(diff)),
        )
        journal.append(
            "experiment",
            f"proposed {prop.lane.value}.{prop.key}: {prop.old!r} -> {prop.new!r} ({prop.rationale})",
            subject=experiment_id,
            refs=[],
            conn=c,
        )
        report.experiments_created.append(experiment_id)

    if run_id:
        c.execute(
            "UPDATE reflection_runs SET status = 'applied', result_json = ?, applied_json = ? "
            "WHERE run_id = ?",
            (jdump(result.model_dump(mode="json")), jdump(report.model_dump(mode="json")), run_id),
        )

    emit(
        EventKind.REFLECTION,
        {
            "lessons": report.lessons_written,
            "rules_added": len(report.rules_added),
            "rules_retired": len(report.rules_retired),
            "experiments": len(report.experiments_created),
            "skipped": report.skipped,
            "run_id": run_id,
        },
        conn=c,
    )
    return report


def record_run(
    packet: ReviewPacket,
    masked: MaskedReview | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Persist the packet fingerprint and mask map so a run can be audited later."""
    c = conn or get_conn()
    run_id = "refl_" + digest({"fp": packet.fingerprint(), "ts": now_ms()})[:16]
    c.execute(
        "INSERT OR REPLACE INTO reflection_runs (run_id, created_ms, since_ms, until_ms, "
        "packet_digest, mask_json, status) VALUES (?,?,?,?,?,?, 'built')",
        (
            run_id,
            now_ms(),
            packet.since_ms,
            packet.until_ms,
            packet.fingerprint(),
            jdump(masked.mapping if masked else {}),
        ),
    )
    return run_id


def reject_run(run_id: str, reasons: Sequence[str], conn: sqlite3.Connection | None = None) -> None:
    """A rejected reflection is kept with its reason; silence would hide a broken model."""
    c = conn or get_conn()
    c.execute(
        "UPDATE reflection_runs SET status = 'rejected', reason = ? WHERE run_id = ?",
        ("; ".join(reasons)[:2000], run_id),
    )
    journal.append(
        "correction",
        f"reflection {run_id} rejected: {'; '.join(reasons)[:1000]}",
        subject=run_id,
        conn=c,
    )


__all__ = [
    "ApplyReport",
    "FORBIDDEN_PARAM_KEYS",
    "Lesson",
    "MAX_LESSONS",
    "MAX_LESSON_CHARS",
    "MAX_PARAM_PROPOSALS",
    "MAX_PLAYBOOK_DELTAS",
    "MaskedReview",
    "ParamProposal",
    "PlaybookDelta",
    "ReflectionRejected",
    "ReflectionResult",
    "ReviewPacket",
    "SkipRecord",
    "TradeRecord",
    "apply_reflection",
    "build_review",
    "mask",
    "parse_reflection",
    "record_run",
    "reject_run",
    "unmask_text",
]
