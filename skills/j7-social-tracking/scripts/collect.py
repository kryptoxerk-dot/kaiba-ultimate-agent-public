"""Read-only J7 account export and bounded social post capture."""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TOKEN = Path.home() / ".config" / "kaiba" / "j7.session"
HOSTS = {"nyc": "https://nyc.j7tracker.io", "dfw": "https://dfw.j7tracker.io"}


class Unavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unavailable("redirect_refused")


def token(path: Path) -> str:
    value = os.environ.get("J7_SESSION_JWT", "").strip()
    if not value:
        try:
            value = path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            raise Unavailable("missing_j7_session") from None
    if not value or any(c.isspace() for c in value):
        raise Unavailable("invalid_j7_session_file")
    return value


def account_rows(data: dict) -> list[dict]:
    if not isinstance(data, dict) or data.get("success") is not True:
        raise Unavailable("accounts_unavailable")
    if not isinstance(data.get("x"), dict) or not isinstance(data["x"].get("accounts"), list):
        raise Unavailable("unexpected_accounts_shape")
    out = []
    groups = [("main_feed", data["x"]["accounts"]),
              ("custom", data.get("custom", {}).get("accounts", [])),
              ("user_available", data.get("custom", {}).get("availableAccounts", [])),
              ("available_pool", data.get("available", {}).get("accounts", []))]
    hidden = {str(h).lstrip("@").lower() for h in data.get("hidden", [])}
    for kind, items in groups:
        for item in items:
            handle = item.get("handle") if isinstance(item, dict) else item
            if not isinstance(handle, str) or not handle:
                continue
            handle = handle.lstrip("@").lower()
            out.append({"handle": handle, "profile_url": "https://x.com/" + handle,
                        "j7_kind": kind, "hidden": handle in hidden,
                        "source": "j7_accounts_api"})
    return out


class Posts:
    def __init__(self):
        self.posts = {}
        self.lock = threading.Lock()

    def upsert(self, post, *, now_ms: int, backlog: bool = False) -> dict | None:
        if not isinstance(post, dict) or not post.get("id"):
            return None
        platform = str(post.get("platform") or "X")
        key = (platform, str(post["id"]))
        with self.lock:
            old = self.posts.get(key)
            merged = dict(old["payload"]) if old else {}
            merged.update(post)
            if old and isinstance(post.get("author"), dict):
                previous_author = old["payload"].get("author") or {}
                merged["author"] = {**previous_author, **post["author"]}
            if old and merged == old["payload"]:
                return None
            author = merged.get("author") or {}
            if not isinstance(author, dict):
                author = {}
            first_seen = old["first_seen_ms"] if old else now_ms
            originally_backlog = old["is_backlog"] if old else backlog
            revision = old["revision"] + 1 if old else 1
            self.posts[key] = {"payload": merged, "first_seen_ms": first_seen,
                               "revision": revision, "is_backlog": originally_backlog}
            text = str(merged.get("text") or "")[:20000]
            if platform != "X":
                text = html.unescape(text)
            return {"source": "j7tracker", "platform": platform, "post_id": key[1],
                    "first_seen_ms": first_seen, "observed_ms": now_ms,
                    "is_backlog": originally_backlog, "revision": revision,
                    "created_at": merged.get("createdAt"), "author": author.get("handle"),
                    "author_id": author.get("id"), "text": text,
                    "url": merged.get("tweetUrl"), "post_type": merged.get("type"),
                    "is_reply": merged.get("isReply"), "is_quote": merged.get("isQuote"),
                    "is_retweet": merged.get("isRetweet"),
                    "upstream_chain_hint": merged.get("chain"),
                    "upstream_contract_hint": merged.get("contractAddress")}


def accounts(jwt: str, output: Path) -> dict:
    request = urllib.request.Request("https://core.j7tracker.io/api/watched-accounts",
                                     headers={"Authorization": "Bearer " + jwt,
                                              "User-Agent": "KaibaSocialResearch/1"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            body = response.read(8_000_001)
        if len(body) > 8_000_000:
            raise Unavailable("response_too_large")
        rows = account_rows(json.loads(body))
    except urllib.error.HTTPError as exc:
        raise Unavailable(f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Unavailable("network_unavailable") from None
    except (ValueError, TypeError, AttributeError):
        raise Unavailable("malformed_accounts_response") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["handle", "profile_url", "j7_kind",
                                                    "hidden", "source"])
        writer.writeheader()
        writer.writerows(rows)
    return {"status": "ok", "rows": len(rows), "output": str(output)}


def capture(jwt: str, output: Path, region: str, seconds: int, max_events: int) -> dict:
    try:
        import socketio
    except ImportError:
        raise Unavailable("missing_python_socketio_client") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    reducer = Posts()
    stop = threading.Event()
    state = {"received": 0, "records": 0, "auth_failed": False, "connected": False,
             "disconnected": False}
    write_lock = threading.Lock()
    with output.open("a", encoding="utf-8") as stream:
        def save(post, backlog=False):
            with write_lock:
                if state["received"] >= max_events:
                    stop.set()
                    return
                state["received"] += 1
                row = reducer.upsert(post, now_ms=int(time.time() * 1000), backlog=backlog)
                if row:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                    state["records"] += 1
                if state["received"] >= max_events:
                    stop.set()

        @sio.event
        def connect():
            state["connected"] = True
            sio.emit("user_connected", jwt)

        @sio.on("initialTweets")
        def initial(items):
            if isinstance(items, list):
                for item in items:
                    save(item, True)

        for event in ("tweet", "tweet_update", "external_message"):
            sio.on(event, save)

        @sio.on("auth_error")
        def auth_error(_payload):
            state["auth_failed"] = True
            stop.set()

        @sio.event
        def disconnect():
            state["disconnected"] = True
            stop.set()

        try:
            sio.connect(HOSTS[region], transports=["websocket"], auth={"token": jwt},
                        wait_timeout=15)
            stop.wait(seconds)
        except Exception:
            raise Unavailable("feed_connect_failed") from None
        finally:
            unexpected_disconnect = state["disconnected"]
            if sio.connected:
                sio.disconnect()
    if state["auth_failed"]:
        raise Unavailable("feed_auth_error")
    return {"status": "ok", **state, "output": str(output),
            "bounded_seconds": seconds, "event_cap_reached": state["received"] >= max_events,
            "ended_early": unexpected_disconnect}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    subs = parser.add_subparsers(dest="command", required=True)
    acc = subs.add_parser("accounts")
    acc.add_argument("--output", type=Path, required=True)
    cap = subs.add_parser("capture")
    cap.add_argument("--output", type=Path, required=True)
    cap.add_argument("--region", choices=HOSTS, default="nyc")
    cap.add_argument("--seconds", type=int, choices=range(1, 301), default=60)
    cap.add_argument("--max-events", type=int, choices=range(1, 10001), default=1000)
    args = parser.parse_args()
    try:
        jwt = token(args.token_file)
        result = (accounts(jwt, args.output) if args.command == "accounts" else
                  capture(jwt, args.output, args.region, args.seconds, args.max_events))
        print(json.dumps(result))
        return 0
    except Unavailable as exc:
        print(json.dumps({"status": "unavailable", "reason": str(exc)}))
        return 1
    except OSError:
        print(json.dumps({"status": "unavailable", "reason": "local_io_error"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
