"""Stable Hermes change trigger. No provider calls or missing-store creation."""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/kaiba")
MODEL_ACTIONS = ("model_resolve_identity", "model_verify_official_terms", "model_verify_primary_source")
sys.path.insert(0, str(ROOT))
from kaiba.hunters.grok_alpha_bridge import IntakeRefused, Store  # noqa: E402


def payload(snapshot, model_pending=(), pipeline="unavailable"):
    run = snapshot.get("sender_reported_run") or {}
    return {"model_pending": list(model_pending), "pipeline": pipeline,
            "delivery": snapshot.get("delivery", "unavailable"),
            "sender_reported_status": run.get("status"), "financial_execution": False}


def model_handoff(store, pipeline):
    """Join only the two bounded research stores; never open canonical kaiba.db.

    Ignore transient leases in the change trigger. Review, source revision and
    expiry change the queue; polling clocks and a model's own lease do not.
    """
    if not pipeline.is_file():
        raise IntakeRefused("pipeline_missing")
    if pipeline.stat().st_size > 64 * 1024 * 1024:
        raise IntakeRefused("pipeline_size")
    with store.connection() as con:
        con.execute("ATTACH DATABASE ? AS processing", (pipeline.resolve().as_uri() + "?mode=ro",))
        rows = con.execute(
            "SELECT r.key AS observation_id,r.digest AS revision,p.next_action FROM revisions r "
            "JOIN observations o ON o.key=r.key AND o.digest=r.digest "
            "JOIN processing.pipeline p ON p.observation_id=r.key AND p.revision=r.digest "
            "WHERE r.state='unverified' AND (r.expires_ms IS NULL OR r.expires_ms>?) "
            "AND p.next_action IN (?,?,?) ORDER BY r.received_ms,r.key LIMIT 100",
            (store.clock(), *MODEL_ACTIONS)).fetchall()
    return [dict(row) for row in rows]


def main():
    try:
        store = Store(ROOT/"data/grok-alpha/inbox.db", readonly=True)
        status = store.status()
        try:
            pending = model_handoff(store, ROOT/"data/grok-alpha/pipeline.db")
            out = payload(status, pending, "ready")
        except (IntakeRefused, OSError, ValueError, sqlite3.Error):
            out = payload(status)
    except (IntakeRefused, OSError, ValueError, sqlite3.Error):
        out = payload({"delivery": "intake_unavailable"})
    print(json.dumps(out, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
