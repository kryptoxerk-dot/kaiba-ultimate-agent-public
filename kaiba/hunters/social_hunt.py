"""Fast social NFT/airdrop/token research inbox. No HTTP or financial execution imports.

The single provider stream sends local datagrams; this research service owns a SMALL
separate database. It records attribution and latency before any costly enrichment.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT = Path("config/social_hunt.yaml")
PATTERNS = {
    "nft": re.compile(
        r"\b(?:nfts?|mint(?:ing)?|allowlist|whitelist|wl|public sale|reveal|collection|candy machine)\b", re.I
    ),
    "airdrop": re.compile(
        r"\b(?:airdrop|points|snapshot|eligibility|eligible|claim(?:ing)?|testnet|quest|reward|tge|allocation)\b",
        re.I,
    ),
    "token": re.compile(
        r"\$[A-Za-z][A-Za-z0-9]{1,14}\b|\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"
    ),
}


def load_config(path: Path = DEFAULT) -> dict:
    if not path.exists():
        return {
            "enabled": False,
            "accounts": {},
            "socket_path": "data/social-hunt.sock",
            "database": "data/social-hunt.db",
        }
    c = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(c, dict) or type(c.get("enabled")) is not bool:
        raise ValueError("invalid_social_hunt_configuration")
    accounts = c.get("accounts", {})
    if not isinstance(accounts, dict) or set(accounts) - set(PATTERNS):
        raise ValueError("invalid_social_hunt_accounts")
    for lane, handles in accounts.items():
        if not isinstance(handles, list) or any(
            not isinstance(h, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h) for h in handles
        ):
            raise ValueError("invalid_social_hunt_handle")
        accounts[lane] = list(dict.fromkeys(h.lower() for h in handles))
    if (
        not 1 <= int(c.get("max_post_age_s", 86400)) <= 604800
        or not 1 <= int(c.get("retention_days", 7)) <= 30
    ):
        raise ValueError("invalid_social_hunt_retention")
    return c


def classify(event: dict, config: dict, *, now_ms: int | None = None) -> list[dict]:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if not config.get("enabled") or not re.fullmatch(r"\d{5,25}", str(event.get("tweet_id", ""))):
        return []
    author = str(event.get("author") or "").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", author):
        return []
    created = event.get("created_ms")
    if (
        type(created) is not int
        or created > now + 2000
        or now - created > int(config.get("max_post_age_s", 86400)) * 1000
    ):
        return []
    if event.get("kind") == "repost":
        return []
    quote = event.get("quoted") or {}
    text = str(event.get("text") or "")[:12000]
    qtext = str(quote.get("text") or "")[:12000]
    combined = text + "\n" + qtext
    addresses = sorted(set(re.findall(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", combined)))
    urls = []
    for value in re.findall(r"https?://[^\s<>]+", combined):
        value = value.rstrip(".,);]")
        u = urlsplit(value)
        if len(value) <= 2048 and u.hostname and not u.username and not u.password:
            urls.append(value)
    out = []
    for lane, pattern in PATTERNS.items():
        hits = sorted(set(x.lower() for x in pattern.findall(combined)))
        # In J7-all mode any delivered source may carry a hunt. Purpose lists prioritize
        # coverage on the funded custom-rule source, not claims of identity or trust.
        if not hits:
            continue
        received = event.get("received_ms")
        dispatched = event.get("dispatched_ms")
        out.append(
            {
                "tweet_id": str(event["tweet_id"]),
                "lane": lane,
                "author": author,
                "url": f"https://x.com/{author}/status/{event['tweet_id']}",
                "text": text,
                "created_ms": created,
                "received_ms": received,
                "dispatched_ms": dispatched,
                "handled_ms": now,
                "feed_latency_ms": received - created
                if type(received) is int and received >= created
                else None,
                "dispatch_latency_ms": now - received if type(received) is int and now >= received else None,
                "source_priority": author in config.get("accounts", {}).get(lane, []),
                "quoted": quote,
                "parent_id": event.get("parent_id"),
                "media": event.get("media", []),
                "hits": hits,
                "addresses": addresses,
                "links": list(dict.fromkeys(urls)),
                "status": "needs_source_and_contract_verification",
                "financial_execution": False,
            }
        )
    return out


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.query(
            "CREATE TABLE IF NOT EXISTS posts (tweet_id TEXT PRIMARY KEY, author TEXT, created_ms INTEGER, received_ms INTEGER, handled_ms INTEGER, payload TEXT)"
        )
        self.query(
            "CREATE TABLE IF NOT EXISTS signals (tweet_id TEXT, lane TEXT, author TEXT, created_ms INTEGER, handled_ms INTEGER, payload TEXT, PRIMARY KEY(tweet_id,lane))"
        )
        self.query(
            "CREATE TABLE IF NOT EXISTS reviews (tweet_id TEXT, lane TEXT, lease_until_ms INTEGER, reviewed_ms INTEGER, status TEXT, note TEXT, PRIMARY KEY(tweet_id,lane))"
        )

    def query(self, sql, args=()):
        start = time.monotonic()
        c = sqlite3.connect(self.path, timeout=1)
        try:
            c.set_progress_handler(lambda: int(time.monotonic() - start > 2), 1000)
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
            c.commit()
        finally:
            c.close()
        return rows

    def record(self, event: dict, signals: list[dict], now_ms: int):
        if (
            self.path.stat().st_size > 256 * 1024 * 1024
            or shutil.disk_usage(self.path.parent).free < 2 * 1024**3
        ):
            raise OSError("social_store_capacity")
        tid = str(event.get("tweet_id", ""))
        if not re.fullmatch(r"\d{5,25}", tid):
            return
        # First receipt time is never overwritten by later enriched revisions.
        self.query(
            "INSERT INTO posts VALUES (?,?,?,?,?,?) ON CONFLICT(tweet_id) DO UPDATE SET handled_ms=excluded.handled_ms,payload=excluded.payload",
            (
                tid,
                event.get("author"),
                event.get("created_ms"),
                event.get("received_ms"),
                now_ms,
                json.dumps(event),
            ),
        )
        for row in signals:
            self.query(
                "INSERT INTO signals VALUES (?,?,?,?,?,?) ON CONFLICT(tweet_id,lane) DO UPDATE SET handled_ms=excluded.handled_ms,payload=excluded.payload",
                (tid, row["lane"], row["author"], row["created_ms"], now_ms, json.dumps(row)),
            )

    def maintain(self, days: int, now_ms: int):
        cutoff = now_ms - days * 86400000
        for table in ("posts", "signals"):
            for _ in range(10):
                ids = self.query(f"SELECT rowid FROM {table} WHERE handled_ms<? LIMIT 1000", (cutoff,))
                if not ids:
                    break
                self.query(
                    f"DELETE FROM {table} WHERE rowid IN ({','.join('?' for _ in ids)})",
                    tuple(r["rowid"] for r in ids),
                )
        self.query(
            "DELETE FROM reviews WHERE NOT EXISTS (SELECT 1 FROM signals s WHERE s.tweet_id=reviews.tweet_id AND s.lane=reviews.lane)"
        )

    def claim(self, limit: int, now_ms: int):
        c = sqlite3.connect(self.path, timeout=1)
        try:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT s.tweet_id,s.lane,s.payload FROM signals s LEFT JOIN reviews r ON r.tweet_id=s.tweet_id AND r.lane=s.lane WHERE r.reviewed_ms IS NULL AND (r.lease_until_ms IS NULL OR r.lease_until_ms<=?) ORDER BY CASE s.lane WHEN 'nft' THEN 0 WHEN 'airdrop' THEN 1 ELSE 2 END,s.handled_ms DESC LIMIT ?",
                (now_ms, max(1, min(limit, 20))),
            ).fetchall()
            for tid, lane, _ in rows:
                c.execute(
                    "INSERT INTO reviews (tweet_id,lane,lease_until_ms) VALUES (?,?,?) ON CONFLICT(tweet_id,lane) DO UPDATE SET lease_until_ms=excluded.lease_until_ms",
                    (tid, lane, now_ms + 300000),
                )
            c.commit()
            return [json.loads(r[2]) for r in rows]
        finally:
            c.close()


def status(store: Store) -> dict:
    posts = store.query("SELECT COUNT(*) n,MAX(handled_ms) latest_ms FROM posts")[0]
    counts = store.query("SELECT lane,COUNT(*) n FROM signals GROUP BY lane")
    rows = store.query("SELECT created_ms,received_ms,handled_ms FROM posts ORDER BY rowid DESC LIMIT 1000")

    def percentiles(values):
        values = sorted(v for v in values if v is not None and v >= 0)
        return {
            "n": len(values),
            "p50_ms": values[(len(values) - 1) // 2] if values else None,
            "p95_ms": values[int((len(values) - 1) * 0.95)] if values else None,
        }

    return {
        "posts": posts,
        "signals": counts,
        "feed": percentiles(
            [
                r["received_ms"] - r["created_ms"]
                if type(r["received_ms"]) is int and type(r["created_ms"]) is int
                else None
                for r in rows
            ]
        ),
        "dispatch": percentiles(
            [r["handled_ms"] - r["received_ms"] if type(r["received_ms"]) is int else None for r in rows]
        ),
        "financial_execution": False,
    }


def run(config_path: Path):
    c = load_config(config_path)
    store = Store(Path(c["database"]))
    path = Path(c["socket_path"]).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Process lock prevents a second receiver from unlinking a live service's socket.
    import fcntl

    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(str(path))
            path.chmod(0o600)
            sock.settimeout(1)
            print(json.dumps({"status": "ready", "socket": str(path)}), flush=True)
            try:
                while True:
                    try:
                        payload = sock.recv(65536)
                        now = int(time.time() * 1000)
                        event = json.loads(payload)
                        c = load_config(config_path)
                        if not isinstance(event, dict):
                            continue
                        signals = classify(event, c, now_ms=now)
                        store.record(event, signals, now)
                        if signals:
                            print(
                                json.dumps(
                                    {
                                        "status": "signals",
                                        "post": event["tweet_id"],
                                        "lanes": [r["lane"] for r in signals],
                                        "dispatch_ms": signals[0]["dispatch_latency_ms"],
                                    }
                                ),
                                flush=True,
                            )
                    except TimeoutError:
                        continue
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}), flush=True)
            finally:
                path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "status", "accounts", "maintain"):
        sub.add_parser(command)
    ls = sub.add_parser("list")
    ls.add_argument("--lane", choices=tuple(PATTERNS))
    ls.add_argument("--limit", type=int, default=20)
    replay = sub.add_parser("replay")
    replay.add_argument("input", type=Path)
    claim = sub.add_parser("next")
    claim.add_argument("--limit", type=int, default=8)
    done = sub.add_parser("review")
    done.add_argument("tweet_id")
    done.add_argument("--lane", choices=tuple(PATTERNS), required=True)
    done.add_argument("--status", choices=("verified", "watch", "rejected", "unavailable"), required=True)
    done.add_argument("--note", default="")
    a = parser.parse_args(argv)
    c = load_config(a.config)
    if a.command == "run":
        run(a.config)
        return
    if a.command == "accounts":
        print(json.dumps(c["accounts"]))
        return
    store = Store(Path(c["database"]))
    if a.command == "next":
        for row in store.claim(a.limit, int(time.time() * 1000)):
            print(json.dumps(row))
        return
    if a.command == "review":
        if not store.query("SELECT 1 FROM signals WHERE tweet_id=? AND lane=?", (a.tweet_id, a.lane)):
            raise ValueError("unknown_signal")
        store.query(
            "INSERT INTO reviews VALUES (?,?,?,?,?,?) ON CONFLICT(tweet_id,lane) DO UPDATE SET reviewed_ms=excluded.reviewed_ms,status=excluded.status,note=excluded.note",
            (a.tweet_id, a.lane, 0, int(time.time() * 1000), a.status, a.note[:500]),
        )
        print(json.dumps({"status": "review_saved", "financial_execution": False}))
        return
    if a.command == "replay":
        count = 0
        signals = 0
        for line in a.input.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            now = int(time.time() * 1000)
            rows = classify(event, c, now_ms=now)
            store.record(event, rows, now)
            count += 1
            signals += len(rows)
        print(json.dumps({"replayed": count, "signals": signals, "historical_replay": True}))
        return
    if a.command == "maintain":
        store.maintain(int(c.get("retention_days", 7)), int(time.time() * 1000))
    if a.command in ("status", "maintain"):
        print(json.dumps(status(store)))
        return
    limit = max(1, min(a.limit, 100))
    where = "WHERE lane=?" if a.lane else ""
    rows = store.query(
        f"SELECT payload FROM signals {where} ORDER BY handled_ms DESC LIMIT ?",
        tuple([a.lane] if a.lane else []) + (limit,),
    )
    for row in rows:
        print(row["payload"])


if __name__ == "__main__":
    main()
