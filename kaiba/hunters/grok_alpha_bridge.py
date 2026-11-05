"""Bounded research intake for Grok Bot. No market, wallet or execution calls.

Owns a small isolated evidence store, not kaiba.db. Model claims remain claims.
Receipt time is stamped here; import never manufactures an earlier observation.
"""
# Claude (lead) 2026-11-02 GROK-LAUNCH-FEED-CLAUDE-20261102: one change in this Codex-owned file,
# the "launch" track (one launchable X post per card, read by kaiba/ingest/grok_launch_feed.py;
# contract in docs/runbooks/grok-launch-cards.md). Validation and storage are unchanged.
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "kaiba.grok.observation/1"
TRACKS = ("nft", "token", "airdrop", "chain", "macro", "ai_crypto", "flow", "launch")
MAX_BODY = 256 * 1024
MAX_CARD = 32 * 1024
DAY = 86_400_000


class IntakeRefused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise IntakeRefused(reason)


def milliseconds(value, *, optional=True):
    if value is None and optional:
        return None
    require(isinstance(value, str) and len(value) <= 64, "timestamp_timezone_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "timestamp_timezone_required")
        result = int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, OSError) as exc:
        raise IntakeRefused("timestamp_invalid") from exc
    require(result > 0, "timestamp_invalid")
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def normalize(card, *, now_ms):
    require(isinstance(card, dict), "observation_object_required")
    allowed = {"schema_version", "track", "project", "claim", "network", "contract", "sources",
               "observed_at", "received_at", "event_at", "expires_at", "verification", "economics",
               "provenance", "usage", "state", "next_check"}
    require(not set(card) - allowed and card.get("schema_version") == SCHEMA, "observation_schema")
    require(card.get("track") in TRACKS, "track_invalid")
    for field, limit in (("project", 160), ("claim", 8000)):
        require(isinstance(card.get(field), str) and 0 < len(card[field].strip()) <= limit, field + "_required")
    for field, limit in (("network", 160), ("next_check", 1600)):
        require(card.get(field) is None or isinstance(card[field], str) and len(card[field]) <= limit,
                field + "_invalid")
    # Sender may describe its clock, but cannot set the receiver's admission clock.
    require(card.get("received_at") is None, "receiver_timestamp_is_server_owned")
    observed = milliseconds(card.get("observed_at"))
    require(observed is None or observed <= now_ms + 5000, "observation_in_future")
    expires = milliseconds(card.get("expires_at"))
    milliseconds(card.get("event_at"))
    sources = card.get("sources")
    require(isinstance(sources, list) and 1 <= len(sources) <= 12, "source_citation_required")
    source_ids = []
    for source in sources:
        require(isinstance(source, dict) and isinstance(source.get("url"), str), "source_shape")
        require(len(source["url"]) <= 512, "source_url_size")
        url = urlsplit(source["url"])
        require(url.scheme == "https" and url.hostname in ("x.com", "www.x.com", "twitter.com", "www.twitter.com")
                and url.username is None and url.password is None and url.port in (None, 443), "source_host")
        match = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/(\d{5,25})/?", url.path)
        require(match is not None, "source_post_identity")
        source_ids.append(match[1])
        published = milliseconds(source.get("published_at"))
        retrieved = milliseconds(source.get("retrieved_at"))
        require(published is None or published <= now_ms + 5000, "source_publication_in_future")
        require(retrieved is None or retrieved <= now_ms + 5000, "source_retrieval_in_future")
        require(published is None or retrieved is None or retrieved >= published, "source_clock_order")
        require(observed is None or published is None or observed >= published, "source_after_observation")
        require(source.get("text") is None or isinstance(source["text"], str)
                and len(source["text"]) <= 12000, "source_text_size")
    require(len(source_ids) == len(set(source_ids)), "duplicate_source_citation")
    provenance = card.get("provenance")
    require(isinstance(provenance, dict) and provenance.get("backend") in
            ("grok_bot_browser", "grok_bot_x_search", "manual_export"), "backend_provenance_required")
    # Stable source identity, with explicit content revisions. Poll clocks alone do not revise a claim.
    identity = {"track": card["track"], "project": card["project"].strip().casefold(),
                "network": card.get("network"), "contract": card.get("contract"), "posts": sorted(source_ids)}
    key = hashlib.sha256(canonical(identity).encode()).hexdigest()
    content = {k: v for k, v in card.items() if k not in ("observed_at", "received_at", "usage", "provenance")}
    content["sources"] = [{k: v for k, v in s.items() if k != "retrieved_at"} for s in sources]
    digest = hashlib.sha256(canonical(content).encode()).hexdigest()
    raw = canonical(card)
    require(len(raw.encode()) <= MAX_CARD, "observation_size")
    return key, digest, raw, expires


class Store:
    def __init__(self, path, *, clock=lambda: int(time.time() * 1000), readonly=False):
        self.path, self.clock, self.readonly = Path(path), clock, readonly
        if readonly:
            require(self.path.is_file(), "intake_store_missing")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS observations(
                  key TEXT PRIMARY KEY, track TEXT NOT NULL, first_received_ms INTEGER NOT NULL,
                  last_received_ms INTEGER NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                  key TEXT NOT NULL, digest TEXT NOT NULL, received_ms INTEGER NOT NULL,
                  payload TEXT NOT NULL, expires_ms INTEGER, state TEXT NOT NULL,
                  lease_until_ms INTEGER NOT NULL DEFAULT 0, review TEXT,
                  PRIMARY KEY(key,digest));
                CREATE INDEX IF NOT EXISTS revision_pending ON revisions(state,lease_until_ms,received_ms);
                CREATE TABLE IF NOT EXISTS delivery_nonces(nonce TEXT PRIMARY KEY, received_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS heartbeat(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  received_ms INTEGER NOT NULL,payload TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    def accept_nonce(self, nonce):
        now = self.clock()
        with self.connection() as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM delivery_nonces WHERE received_ms<?", (now - 600_000,))
                require(con.execute("SELECT COUNT(*) FROM delivery_nonces").fetchone()[0] < 2000,
                        "delivery_rate_limit")
                try:
                    con.execute("INSERT INTO delivery_nonces VALUES(?,?)", (nonce, now))
                except sqlite3.IntegrityError as exc:
                    raise IntakeRefused("delivery_nonce_reused") from exc

    def heartbeat(self, run):
        now = self.clock()
        require(isinstance(run, dict) and set(run) <= {"run_id", "backend", "status", "completed_at", "usage", "reason"},
                "heartbeat_shape")
        require(isinstance(run.get("run_id"), str) and 0 < len(run["run_id"]) <= 160, "heartbeat_run_id")
        require(run.get("backend") in ("grok_bot_browser", "grok_bot_x_search"), "heartbeat_backend")
        require(run.get("status") in ("complete", "unavailable", "usage_limited"), "heartbeat_status")
        completed = milliseconds(run.get("completed_at"), optional=False)
        require(completed <= now + 5000, "heartbeat_clock")
        raw = canonical(run)
        require(len(raw.encode()) <= 4096, "heartbeat_size")
        with self.connection() as con:
            with con:
                con.execute("INSERT INTO heartbeat VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                            "received_ms=excluded.received_ms,payload=excluded.payload", (now, raw))
        return {"heartbeat_received_ms": now, "sender_reported_status": run["status"], "financial_execution": False}

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=2) if self.readonly else sqlite3.connect(self.path, timeout=2)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=2000")
        deadline = time.monotonic() + 3
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            yield con
        finally:
            con.close()

    def ingest(self, cards):
        now = self.clock()
        require(isinstance(cards, list) and 1 <= len(cards) <= 20, "batch_size")
        normalized = [normalize(card, now_ms=now) for card in cards]
        require(self.path.stat().st_size < 64 * 1024 * 1024
                and shutil.disk_usage(self.path.parent).free >= 2 * 1024**3, "research_store_capacity")
        accepted = []
        with self.connection() as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM revisions WHERE received_ms < ?", (now - 7 * DAY,))
                con.execute("DELETE FROM observations WHERE NOT EXISTS (SELECT 1 FROM revisions r WHERE r.key=observations.key)")
                require(con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] + len(cards) <= 4000,
                        "research_queue_capacity")
                for card, (key, digest, raw, expires) in zip(cards, normalized, strict=True):
                    new = con.execute("INSERT OR IGNORE INTO revisions(key,digest,received_ms,payload,expires_ms,state) "
                                      "VALUES(?,?,?,?,?,?)", (key, digest, now, raw, expires,
                                      "expired" if expires is not None and expires <= now else "unverified")).rowcount == 1
                    con.execute("INSERT INTO observations VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                                "last_received_ms=excluded.last_received_ms,digest=excluded.digest",
                                (key, card["track"], now, now, digest))
                    accepted.append({"observation_id": key, "revision": digest, "new": new})
        return {"accepted": accepted, "received_ms": now, "financial_execution": False}

    def status(self):
        now = self.clock()
        with self.connection() as con:
            counts = {row[0]: row[1] for row in con.execute("SELECT track,COUNT(*) FROM observations GROUP BY track")}
            latest = con.execute("SELECT MAX(last_received_ms) FROM observations").fetchone()[0]
            heartbeat = con.execute("SELECT received_ms,payload FROM heartbeat WHERE singleton=1").fetchone()
            pending = [dict(r) for r in con.execute("SELECT r.key,r.digest,o.track FROM revisions r JOIN observations o "
                       "ON o.key=r.key AND o.digest=r.digest WHERE r.state='unverified' AND r.lease_until_ms<=? "
                       "AND (r.expires_ms IS NULL OR r.expires_ms>?) ORDER BY r.received_ms LIMIT 20", (now, now))]
        delivery_ms = max([v for v in (latest, heartbeat[0] if heartbeat else None) if v is not None], default=None)
        return {"schema": "kaiba.grok.intake/1", "counts": counts, "pending": pending,
                "last_received_ms": latest,
                "last_delivery_ms": delivery_ms,
                "sender_reported_run": json.loads(heartbeat[1]) if heartbeat else None,
                "delivery": "no_receipt" if delivery_ms is None else "stale" if now - delivery_ms > 30 * 60_000 else "recent_receipt",
                "financial_execution": False, "raw_source_verified": False}

    def claim(self, limit=5, *, only=None):
        """Lease current evidence, optionally restricted BEFORE admission.

        Separate deterministic and model workers must select their own revisions;
        filtering after this call would still consume another worker's lease.
        """
        require(type(limit) is int and 1 <= limit <= 10, "claim_limit")
        selected, params = "", []
        if only is not None:
            require(isinstance(only, (list, tuple, set, frozenset)) and len(only) <= 100,
                    "claim_selection_size")
            for pair in only:
                require(isinstance(pair, (list, tuple)) and len(pair) == 2 and
                        all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in pair),
                        "claim_selection_identity")
            if not only:
                return []
            pairs = sorted(set(tuple(pair) for pair in only))
            selected = " AND (" + " OR ".join("(r.key=? AND r.digest=?)" for _ in pairs) + ")"
            params = [value for pair in pairs for value in pair]
        now, out = self.clock(), []
        with self.connection() as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                rows = con.execute("SELECT r.*,o.track,o.first_received_ms FROM revisions r JOIN observations o "
                       "ON o.key=r.key AND o.digest=r.digest WHERE r.state='unverified' AND r.lease_until_ms<=? "
                       "AND (r.expires_ms IS NULL OR r.expires_ms>?)" + selected +
                       " ORDER BY r.received_ms,r.key LIMIT ?", (now, now, *params, limit)).fetchall()
                for row in rows:
                    con.execute("UPDATE revisions SET lease_until_ms=? WHERE key=? AND digest=?", (now + 300_000, row["key"], row["digest"]))
                    out.append({"observation_id": row["key"], "revision": row["digest"],
                                "received_ms": row["received_ms"], "first_received_ms": row["first_received_ms"],
                                "observation": json.loads(row["payload"]), "research_state": "unverified",
                                "financial_execution": False})
        return out

    def review(self, key, digest, state, note):
        require(state in ("watch", "verified", "rejected", "unavailable", "expired"), "review_state")
        require(all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for v in (key, digest)), "review_identity")
        require(isinstance(note, str) and 0 < len(note) <= 2000, "review_evidence_required")
        with self.connection() as con:
            with con:
                changed = con.execute("UPDATE revisions SET state=?,review=?,lease_until_ms=0 WHERE key=? AND digest=?",
                                      (state, note, key, digest)).rowcount
                require(changed == 1, "review_missing")
        return {"research_state": state, "financial_execution": False}


def handler(store, public_key_hex):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    require(isinstance(public_key_hex, str) and re.fullmatch(r"[0-9a-f]{64}", public_key_hex), "intake_public_key")
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                require(self.path in ("/v1/observations", "/v1/heartbeat"), "operation_refused")
                require(not self.headers.get("Origin"), "browser_origin_refused")
                require(self.headers.get("Transfer-Encoding") is None, "transfer_encoding_refused")
                size = int(self.headers.get("Content-Length", "0"))
                require(0 < size <= MAX_BODY, "request_size")
                raw_body = self.rfile.read(size)
                require(len(raw_body) == size, "request_incomplete")
                stamp, nonce = self.headers.get("X-Kaiba-Timestamp", ""), self.headers.get("X-Kaiba-Nonce", "")
                try:
                    require(re.fullmatch(r"\d{13}", stamp) and re.fullmatch(r"[0-9a-f]{32}", nonce), "signature_headers")
                    require(abs(store.clock() - int(stamp)) <= 300_000, "signature_expired")
                    encoded = self.headers.get("X-Kaiba-Signature", "")
                    require(len(encoded) <= 100, "signature_size")
                    signature = base64.b64decode(encoded, validate=True)
                    message = ("kaiba.grok.delivery/1\n" + stamp + "\n" + nonce + "\n"
                               + hashlib.sha256(raw_body).hexdigest()).encode()
                    public_key.verify(signature, message)
                except (IntakeRefused, InvalidSignature, ValueError):
                    self.send_error(403)
                    return
                store.accept_nonce(nonce)
                body = json.loads(raw_body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                require(isinstance(body, dict), "request_shape")
                if self.path == "/v1/heartbeat":
                    require(set(body) == {"run"}, "request_shape")
                    out = store.heartbeat(body["run"])
                else:
                    require(set(body) == {"observations"}, "request_shape")
                    out = store.ingest(body["observations"])
                code = 200
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
                out, code = {"reason": str(exc) if isinstance(exc, IntakeRefused) else "request_unavailable",
                             "financial_execution": False}, 400
            raw = canonical(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "status", "import", "next", "review"))
    parser.add_argument("--db", type=Path, default=Path("data/grok-alpha/inbox.db"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--selection", type=Path, help="next: JSON list of observation_id/revision pairs")
    parser.add_argument("--settings", type=Path, default=Path.home()/".config/kaiba/grok-alpha-worker.json")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--observation-id")
    parser.add_argument("--revision")
    parser.add_argument("--state")
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    try:
        store = Store(args.db, readonly=args.action == "status")
        if args.action == "serve":
            settings = json.loads(args.settings.read_text())
            server = ThreadingHTTPServer(("127.0.0.1", settings["port"]), handler(store, settings["public_key_hex"]))
            server.daemon_threads = True
            server.serve_forever()
            return 0
        if args.action == "import":
            require(args.input is not None and args.input.stat().st_size <= MAX_BODY, "input_size")
            payload = json.loads(args.input.read_text())
            out = store.ingest(payload["observations"])
        elif args.action == "next":
            selection = None
            if args.selection is not None:
                require(args.selection.stat().st_size <= 32 * 1024, "claim_selection_size")
                selection = json.loads(args.selection.read_text(encoding="utf-8"))
                require(isinstance(selection, list) and len(selection) <= 100 and
                        all(isinstance(r, dict) and set(r) == {"observation_id", "revision"}
                            for r in selection), "claim_selection_shape")
                selection = [(r["observation_id"], r["revision"]) for r in selection]
            out = store.claim(args.limit, only=selection)
        elif args.action == "review":
            out = store.review(args.observation_id, args.revision, args.state, args.note)
        else:
            out = store.status()
        print(canonical(out))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(canonical({"reason": str(exc) if isinstance(exc, IntakeRefused) else "intake_unavailable",
                         "financial_execution": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
