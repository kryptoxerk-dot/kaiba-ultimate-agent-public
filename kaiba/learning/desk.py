"""The analyst desk: role-based forecasts across memecoins, perps, LP, NFT and airdrops, scored for calibration.

Owner, 2026-11-04: an AI analyst team for everything Kaiba does. **The desk has NO trading authority.**
Nothing it produces reaches sizing, admission, lanes, risk, the executor or a wallet; no module under
``kaiba/`` imports it, and ``tests/test_desk.py`` pins that. A verdict is a forecast, recorded and scored.

**Why calibration is the point.** Conviction score, smart-wallet count and dossier grade are all
ANTI-CALIBRATED on our own fills (AGENTS.md). A team of confident analysts is one more confidence
measure until it is proven otherwise, so every verdict is one question from a closed menu
(:mod:`kaiba.learning.desk_outcomes`), a probability and a horizon, later settled from data Kaiba
already holds and compared with the base rate. Below :data:`MIN_N` resolved verdicts no skill number is
shown at all. An outcome that cannot be settled stays ``unresolved`` with its reason: counted and shown,
never scored as a hit or a miss.

The flow, each step printing JSON:

    python -m kaiba.learning.desk queue                  # what to look at, at most 5 subjects
    python -m kaiba.learning.desk brief DOMAIN SUBJECT   # the evidence packet, stored in the desk file
    python -m kaiba.learning.desk record < verdict.json  # strict validation, then one forecast
    python -m kaiba.learning.desk score                  # settle what is due
    python -m kaiba.learning.desk report                 # the calibration table

Storage: ``data/labs/desk/desk.db``, a small file of its own, never ``kaiba.db``. Reads of the live
database go through ``GuardedReader`` and follow the DATABASE READ RULE. Run by hand under
``timeout 120 nice -n 19 ionice -c3``. No RPC, no GMGN, no provider and no network call of any kind:
exit and reconciliation capacity is untouched. Subjects are contract addresses (token, pool, collection)
or ids, never wallets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

from kaiba.learning.autolearn_io import SourceDeferred
from kaiba.learning.desk_outcomes import ScoreContext, Unresolvable, hit, menu, resolve
from kaiba.learning.desk_packets import (
    AUTHORITY,
    CANDIDATES,
    DOMAINS,
    H_MS,
    SUBJECT,
    WARNINGS,
    Sources,
    evidence,
    sources,
)

SCHEMA = "kaiba.desk.packet/1"
MIN_N = 30                  # owner's brief, fixed: no skill number below this
DEFAULT_CAP = 5             # the hard ceiling too: queue() never returns more, so model use stays bounded
STANCES = ("long", "short", "avoid", "no_view")
#: Stored for a no_view, whose row still needs a due time. It is never scored.
NO_VIEW_HORIZON_H = 24
#: Only perps have a short venue (AGENTS.md: bias_sell_no_short_venue). An on-chain adverse view is ``avoid``.
SHORT_DOMAINS = ("perps",)
ROLES = ("market", "onchain", "sentiment", "fundamentals", "bull", "bear", "trader", "risk", "manager")
ROLE_CHARS = 600
MAX_EVIDENCE = 12
MAX_HORIZON_H = 720
#: A packet older than this cannot support a verdict: the forecast must rest on what was true when made.
MAX_PACKET_AGE_MS = 2 * H_MS
#: An unresolved verdict is re-tried this long, because some marks arrive after the due time.
RETRY_MS = 7 * 24 * H_MS
#: One scoring pass: few verdicts, one short query each.
SCORE_ROWS = 200
#: Monitor batches. The monitor runs hourly to score; the model is woken at most this often.
BATCH_EVERY_MS = 12 * H_MS
UNCITED_PACKET_MS = 48 * H_MS
FIELDS = ("packet_id", "domain", "subject", "horizon_h", "stance", "probability", "outcome", "roles", "evidence")
OUTCOME_FIELDS = ("metric", "op", "threshold")
DESK_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
  packet_id TEXT PRIMARY KEY, domain TEXT NOT NULL, subject TEXT NOT NULL, built_ms INTEGER NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verdicts (
  verdict_id TEXT PRIMARY KEY, packet_id TEXT NOT NULL, domain TEXT NOT NULL, subject TEXT NOT NULL,
  stance TEXT NOT NULL, horizon_h INTEGER NOT NULL, probability REAL, outcome_id TEXT, metric TEXT, op TEXT,
  threshold REAL, scoreable TEXT, anchor_json TEXT NOT NULL DEFAULT '{}', roles_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, issued_ms INTEGER NOT NULL, due_ms INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','resolved','unresolved','abstained')),
  reason TEXT, y INTEGER, brier REAL, mark_value REAL, mark_ms INTEGER, mark_source TEXT, checked_ms INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS verdicts_one_open ON verdicts(domain, subject) WHERE state='open';
CREATE INDEX IF NOT EXISTS verdicts_state ON verdicts(state, due_ms);
CREATE TRIGGER IF NOT EXISTS verdicts_forecast_is_frozen
  BEFORE UPDATE OF probability, outcome_id, stance, horizon_h, issued_ms, due_ms, anchor_json ON verdicts
  BEGIN SELECT RAISE(ABORT, 'a forecast never changes after it is recorded'); END;
CREATE TRIGGER IF NOT EXISTS verdicts_forecast_is_kept BEFORE DELETE ON verdicts
  BEGIN SELECT RAISE(ABORT, 'a forecast is never deleted'); END;
"""


class Refused(ValueError):
    """A verdict the desk will not record. The message is the machine-readable reason."""


def desk_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "labs" / "desk"


def _connect(src: Sources) -> sqlite3.Connection:
    """The desk's own file. This is the only write path; the live database is opened read-only, elsewhere."""
    folder = desk_dir(src.data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "desk.db", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript(DESK_SCHEMA)
    return conn


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]


def _subject(domain: str, subject: str) -> None:
    if domain not in DOMAINS:
        raise Refused(f"unknown_domain:{domain}")
    if not isinstance(subject, str) or not SUBJECT[domain].match(subject):
        raise Refused("subject_malformed")


# --------------------------------------------------------------------------------------
# brief and queue
# --------------------------------------------------------------------------------------


def brief(domain: str, subject: str, *, src: Sources | None = None) -> dict[str, Any]:
    """Build the evidence packet and store it, so its verdict is checked against exactly what was read."""
    _subject(domain, subject)
    src = src or sources()
    e = evidence(domain, subject, src)
    packet = {"schema": SCHEMA, "domain": domain, "subject": subject, "built_ms": src.now_ms, "facts": e.facts,
              "unknowns": e.unknowns, "outcomes": menu(domain, subject, e.facts), "warnings": WARNINGS[domain],
              "untrusted_keys": e.untrusted, "authority": AUTHORITY}
    packet["packet_id"] = _digest(_canonical(packet))
    with closing(_connect(src)) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO packets(packet_id,domain,subject,built_ms,body) VALUES (?,?,?,?,?)",
                     (packet["packet_id"], domain, subject, src.now_ms, _canonical(packet)))
    return packet


def queue(cap: int = DEFAULT_CAP, *, domains: Iterable[str] = DOMAINS, src: Sources | None = None) -> dict[str, Any]:
    """Pick subjects live now, one per domain in turn, skipping any with an open verdict."""
    src = src or sources()
    cap = max(1, min(int(cap), DEFAULT_CAP))
    with closing(_connect(src)) as conn:
        busy = {(r["domain"], r["subject"]) for r in conn.execute("SELECT domain,subject FROM verdicts WHERE state='open'")}
    pools: dict[str, list[dict[str, str]]] = {}
    unavailable: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {}
    for domain in domains:
        try:
            rows = CANDIDATES[domain](src)
        except SourceDeferred as exc:      # a domain we could not read is reported, never an empty one
            unavailable[domain] = str(exc)
            continue
        pools[domain] = [r for r in rows if (domain, r["subject"]) not in busy]
        counts[domain] = {"candidates": len(rows), "skipped_open": len(rows) - len(pools[domain])}
    picked: list[dict[str, str]] = []
    while len(picked) < cap and any(pools.values()):
        for domain, rows in pools.items():
            if rows and len(picked) < cap:
                picked.append({"domain": domain, **rows.pop(0)})
    return {"queued": picked, "cap": cap, "counts": counts, "unavailable": unavailable, "authority": AUTHORITY}


# --------------------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------------------


def _strict_loads(raw: str | bytes) -> Any:
    def no_constant(token: str) -> None:
        raise Refused(f"non_finite_number:{token}")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out = dict(pairs)
        if len(out) != len(pairs):
            raise Refused("duplicate_key")
        return out

    try:
        return json.loads(raw, parse_constant=no_constant, object_pairs_hook=no_duplicates)
    except ValueError as exc:
        if isinstance(exc, Refused):
            raise
        raise Refused("invalid_json") from exc


def _exact_keys(value: Any, keys: tuple[str, ...], name: str) -> None:
    if not isinstance(value, dict):
        raise Refused(f"{name}_not_an_object")
    for key in keys:
        if key not in value:
            raise Refused(f"missing_field:{name}.{key}")
    for key in value:
        if key not in keys:
            raise Refused(f"unknown_field:{name}.{key}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_shape(v: Any) -> None:
    _exact_keys(v, FIELDS, "verdict")
    _subject(v["domain"], v["subject"])
    if v["stance"] not in STANCES:
        raise Refused(f"bad_stance:{v['stance']}")
    if v["stance"] == "short" and v["domain"] not in SHORT_DOMAINS:
        raise Refused(f"no_short_venue:{v['domain']}")
    # A no_view forecasts nothing, so it has no horizon to give. FOUND 2026-11-04 by the first real run:
    # `null` was refused, and a refusal the skill says to answer with a no_view has no way out.
    if v["stance"] == "no_view" and v["horizon_h"] is None:
        v["horizon_h"] = NO_VIEW_HORIZON_H
    horizon = v["horizon_h"]
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not 1 <= horizon <= MAX_HORIZON_H:
        raise Refused("bad_horizon")
    _exact_keys(v["roles"], ROLES, "roles")
    for role, view in v["roles"].items():
        if not isinstance(view, str) or not view.strip() or len(view) > ROLE_CHARS:
            raise Refused(f"bad_role_view:{role}")
    keys = v["evidence"]
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or len(keys) != len(set(keys)):
        raise Refused("bad_evidence_list")
    if len(keys) > MAX_EVIDENCE:
        raise Refused("too_many_evidence_keys")
    if v["stance"] == "no_view":
        if v["probability"] is not None or v["outcome"] is not None:
            raise Refused("no_view_carries_no_forecast")
        return
    probability = v["probability"]
    if not _is_number(probability):
        raise Refused("probability_not_a_number")
    if not 0 <= probability <= 1:
        raise Refused("probability_out_of_range")
    _exact_keys(v["outcome"], OUTCOME_FIELDS, "outcome")
    if not keys:
        raise Refused("forecast_without_evidence")


def _choose_outcome(v: dict[str, Any], packet: dict[str, Any]) -> dict[str, Any] | None:
    if v["stance"] == "no_view":
        return None
    want = v["outcome"]
    item = next((o for o in packet["outcomes"] if o["metric"] == want["metric"] and o["op"] == want["op"]
                 and o["threshold"] == want["threshold"]), None)
    if item is None:
        raise Refused("outcome_not_in_packet_menu")
    if item["horizon_h"] != v["horizon_h"]:
        raise Refused(f"horizon_must_be:{item['horizon_h']}")
    if (item["direction"] == "up") != (v["stance"] == "long"):
        raise Refused(f"stance_contradicts_outcome:{v['stance']}:{item['direction']}")
    return item


def _check_evidence(v: dict[str, Any], packet: dict[str, Any]) -> None:
    known = {u["key"] for u in packet["unknowns"]}
    for key in v["evidence"]:
        if key not in packet["facts"]:
            raise Refused(f"evidence_key_is_unknown:{key}" if key in known else f"evidence_key_not_in_packet:{key}")


def record(raw: str | bytes, *, src: Sources | None = None) -> dict[str, Any]:
    """Validate a verdict and store it. Raises :class:`Refused`; nothing is stored unless everything passes."""
    v = _strict_loads(raw)
    _validate_shape(v)
    src = src or sources()
    with closing(_connect(src)) as conn:
        row = conn.execute("SELECT domain,subject,built_ms,body FROM packets WHERE packet_id=?",
                           (str(v["packet_id"]),)).fetchone()
        if row is None:
            raise Refused("unknown_packet")
        if row["domain"] != v["domain"] or row["subject"] != v["subject"]:
            raise Refused("packet_is_for_another_subject")
        if src.now_ms - row["built_ms"] > MAX_PACKET_AGE_MS:
            raise Refused("packet_expired")
        packet = json.loads(row["body"])
        item = _choose_outcome(v, packet)
        _check_evidence(v, packet)
        due_ms = src.now_ms + v["horizon_h"] * H_MS
        verdict_id = _digest(v["packet_id"], _canonical(v))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO verdicts(verdict_id,packet_id,domain,subject,stance,horizon_h,probability,outcome_id,"
                    "metric,op,threshold,scoreable,anchor_json,roles_json,evidence_json,issued_ms,due_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (verdict_id, v["packet_id"], v["domain"], v["subject"], v["stance"], v["horizon_h"],
                     None if item is None else float(v["probability"]), item and item["id"], item and item["metric"],
                     item and item["op"], item and item["threshold"], item and item["scoreable"],
                     _canonical(item["anchor"] if item else {}), _canonical(v["roles"]), _canonical(v["evidence"]),
                     src.now_ms, due_ms))
        except sqlite3.IntegrityError as exc:
            raise Refused("open_verdict_exists") from exc
    return {"recorded": True, "verdict_id": verdict_id, "state": "open", "due_ms": due_ms, "authority": AUTHORITY}


# --------------------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------------------


def _settle(conn: sqlite3.Connection, verdict_id: str, *, state: str, now_ms: int, reason: str | None = None,
            y: int | None = None, brier: float | None = None, mark: Any = None) -> None:
    with conn:
        conn.execute("UPDATE verdicts SET state=?,reason=?,y=?,brier=?,mark_value=?,mark_ms=?,mark_source=?,checked_ms=? "
                     "WHERE verdict_id=? AND state IN ('open','unresolved')",
                     (state, reason, y, brier, mark and mark.value, mark and mark.ms, mark and mark.source, now_ms,
                      verdict_id))


def score(*, src: Sources | None = None) -> dict[str, Any]:
    """Settle what is due. A busy database defers (the verdict keeps its state); it never resolves anything."""
    src = src or sources()
    now = src.now_ms
    tally: Counter[str] = Counter()
    ctx = ScoreContext(src)
    with closing(_connect(src)) as conn:
        rows = conn.execute("SELECT * FROM verdicts WHERE (state='open' AND due_ms<=?) OR (state='unresolved' AND due_ms>?) "
                            "ORDER BY due_ms LIMIT ?", (now, now - RETRY_MS, SCORE_ROWS)).fetchall()
        for row in rows:
            v = dict(row)
            if v["stance"] == "no_view":
                _settle(conn, v["verdict_id"], state="abstained", now_ms=now)
                tally["abstained"] += 1
                continue
            v["anchor"] = json.loads(v["anchor_json"])
            try:
                mark = resolve(v, ctx)
            except SourceDeferred:
                tally["deferred"] += 1
                continue
            except Unresolvable as exc:
                _settle(conn, v["verdict_id"], state="unresolved", now_ms=now, reason=str(exc))
                tally["unresolved"] += 1
                continue
            y = hit(v["op"], mark.value, v["threshold"])
            _settle(conn, v["verdict_id"], state="resolved", now_ms=now, y=y, brier=(v["probability"] - y) ** 2, mark=mark)
            tally["resolved"] += 1
        with conn:
            conn.execute("DELETE FROM packets WHERE built_ms<? AND packet_id NOT IN (SELECT packet_id FROM verdicts)",
                         (now - UNCITED_PACKET_MS,))
    return {"checked": len(rows), **{k: tally[k] for k in ("resolved", "unresolved", "abstained", "deferred")},
            "authority": AUTHORITY}


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def _reason_head(reason: Any) -> str:
    return str(reason or "none").split(":", 1)[0]


def _group(rows: list[dict[str, Any]], base: dict[str, float]) -> dict[str, Any]:
    """Counts always; a Brier score, base rate and skill only from :data:`MIN_N` resolved verdicts.

    The reference forecast is the base rate of each verdict's OWN question, taken in sample. Pooling
    questions would credit the desk for knowing that graduation is rarer than a 3% move; taking it in
    sample gives the reference hindsight. Both make the bar harder to clear, never easier.
    """
    done = [r for r in rows if r["state"] == "resolved"]
    out: dict[str, Any] = {
        "n_total": len(rows), "n_open": sum(r["state"] == "open" for r in rows), "n_resolved": len(done),
        "n_unresolved": sum(r["state"] == "unresolved" for r in rows),
        "n_abstained": sum(r["state"] == "abstained" for r in rows),
        "unresolved_reasons": dict(sorted(Counter(_reason_head(r["reason"]) for r in rows
                                                  if r["state"] == "unresolved").items())),
        "brier": None, "base_rate": None, "reference_brier": None, "skill": None}
    if len(done) < MIN_N:
        out["withheld"] = f"n={len(done)} below {MIN_N}"
        return out
    brier = sum(r["brier"] for r in done) / len(done)
    reference = sum((base[r["outcome_id"]] - r["y"]) ** 2 for r in done) / len(done)
    out.update(brier=round(brier, 4), base_rate=round(sum(r["y"] for r in done) / len(done), 4),
               reference_brier=round(reference, 4), skill=round(1 - brier / reference, 4) if reference > 0 else None)
    if reference == 0:
        out["withheld"] = "every question had one answer: skill is undefined"
    return out


def _calibration(done: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bins = defaultdict(list)
    for row in done:
        bins[min(int(row["probability"] * 10), 9)].append(row)
    table = []
    for index in sorted(bins):
        rows = bins[index]
        enough = len(rows) >= MIN_N
        table.append({"p_from": index / 10, "p_to": (index + 1) / 10, "n": len(rows),
                      "mean_p": round(sum(r["probability"] for r in rows) / len(rows), 4) if enough else None,
                      "hit_rate": round(sum(r["y"] for r in rows) / len(rows), 4) if enough else None})
    return table


def report(*, src: Sources | None = None) -> dict[str, Any]:
    src = src or sources()
    with closing(_connect(src)) as conn:
        rows = [dict(r) for r in conn.execute("SELECT domain,stance,outcome_id,scoreable,probability,state,reason,y,brier "
                                              "FROM verdicts")]
    done = [r for r in rows if r["state"] == "resolved"]
    answers: dict[str, list[int]] = defaultdict(list)
    for row in done:
        answers[row["outcome_id"]].append(row["y"])
    base = {key: sum(ys) / len(ys) for key, ys in answers.items()}

    def by(*fields: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups["/".join(str(row[f] or "no_question") for f in fields)].append(row)
        return {name: _group(group, base) for name, group in sorted(groups.items())}

    scored_or_not = len(done) + sum(r["state"] == "unresolved" for r in rows)
    return {"min_n": MIN_N, "all": _group(rows, base), "by_domain": by("domain"), "by_stance": by("stance"),
            "by_domain_stance": by("domain", "stance"), "by_question": by("outcome_id"),
            "calibration": _calibration(done),
            "unresolved_share": round(1 - len(done) / scored_or_not, 4) if scored_or_not else None,
            "reading": "skill above 0 beats each question's own base rate; unresolved rows are excluded from every "
                       "Brier number, so a high unresolved share means the scored rows may be survivors",
            "authority": AUTHORITY}


# --------------------------------------------------------------------------------------
# monitor: the Hermes gate
# --------------------------------------------------------------------------------------


def monitor(*, src: Sources | None = None) -> str:
    """Score, then print the bytes Hermes hashes. New bytes wake the model; the same bytes cost nothing.

    Hermes wakes the model on ANY change of output, including a queue draining to empty, so an empty
    queue must not change the bytes. The last batch is kept and re-printed: only a NEW non-empty batch,
    at most one per :data:`BATCH_EVERY_MS`, can change them. Scoring runs on every tick at no model cost.
    """
    src = src or sources()
    score(src=src)
    path = desk_dir(src.data_dir) / "monitor_batch.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state = state if isinstance(state, dict) else {}
    if src.now_ms - int(state.get("issued_ms") or 0) >= BATCH_EVERY_MS:
        queued = queue(src=src)["queued"]
        if queued:
            state = {"issued_ms": src.now_ms,
                     "body": {"batch_id": _digest(str(src.now_ms), _canonical(queued)), "queued": queued,
                              "authority": "none"}}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(_canonical(state), encoding="utf-8")
            os.replace(temporary, path)
    return _canonical(state.get("body") or {"authority": "none", "batch_id": None, "queued": []})


# --------------------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.learning.desk", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("queue")
    one.add_argument("--cap", type=int, default=DEFAULT_CAP)
    one = sub.add_parser("brief")
    one.add_argument("domain", choices=DOMAINS)
    one.add_argument("subject")
    one = sub.add_parser("record")
    one.add_argument("--file", type=Path, help="verdict JSON; stdin when omitted")
    sub.add_parser("score")
    sub.add_parser("report")
    sub.add_parser("monitor")
    args = parser.parse_args(argv)
    try:
        if args.command == "monitor":
            sys.stdout.write(monitor())
            return 0
        if args.command == "queue":
            out = queue(args.cap)
        elif args.command == "brief":
            out = brief(args.domain, args.subject)
        elif args.command == "record":
            out = record(args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read())
        else:
            out = score() if args.command == "score" else report()
    except Refused as exc:
        print(json.dumps({"recorded": False, "refused": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
