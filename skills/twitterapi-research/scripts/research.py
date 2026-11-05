"""Bounded read-only TwitterAPI.io research. Does not write Kaiba's database."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.twitterapi.io"
MAX_PAGE_CREDITS = 300
DEFAULT_ROOT = Path.home() / ".config" / "kaiba"


class Unavailable(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Unavailable("redirect_refused")


def credential(path: Path | None = None) -> str:
    value = os.environ.get("TWITTERAPI_IO_KEY", "").strip()
    if not value:
        try:
            value = (path or DEFAULT_ROOT / "twitterapi-research.key").read_text(
                encoding="utf-8-sig"
            ).strip().strip("\"'")
        except OSError:
            if path is not None:
                raise Unavailable("missing_credential") from None
            try:
                lines = (DEFAULT_ROOT / ".env").read_text(encoding="utf-8-sig").splitlines()
                for line in lines:
                    found = re.match(r"^\s*(?:export\s+)?TWITTERAPI_IO_KEY\s*=\s*(.*)$", line)
                    if found:
                        value = found.group(1).strip().strip("\"'")
                        break
            except OSError:
                raise Unavailable("missing_credential") from None
    if not value or any(c.isspace() for c in value):
        raise Unavailable("invalid_credential_file")
    return value


def reserve(ledger: Path, credits: int, now: float | None = None) -> dict:
    """Reserve before dispatch, under one interprocess lock; never refund uncertain calls."""
    if type(credits) is not int or credits <= 0:
        raise Unavailable("invalid_reservation")
    now = time.time() if now is None else now
    day = dt.datetime.fromtimestamp(now, dt.UTC).date().isoformat()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock = ledger.with_name(ledger.name + ".lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise Unavailable("budget_lock_busy") from None
    try:
        state = {"version": 1, "lifetime_credits": 0, "day": day, "daily_credits": 0}
        if ledger.exists():
            try:
                state = json.loads(ledger.read_text(encoding="utf-8"))
                if state["version"] != 1:
                    raise ValueError
                for key in ("lifetime_credits", "daily_credits"):
                    if type(state[key]) is not int or state[key] < 0:
                        raise ValueError
                if not isinstance(state["day"], str):
                    raise ValueError
            except (OSError, ValueError, KeyError, TypeError):
                raise Unavailable("invalid_budget_ledger") from None
        daily = state["daily_credits"] if state["day"] == day else 0
        if state["lifetime_credits"] + credits > 2_000_000:
            raise Unavailable("lifetime_budget_exhausted")
        if daily + credits > 50_000:
            raise Unavailable("daily_budget_exhausted")
        state.update(day=day, daily_credits=daily + credits,
                     lifetime_credits=state["lifetime_credits"] + credits)
        temporary = ledger.with_name(ledger.name + ".new")
        temporary.write_text(json.dumps(state) + "\n", encoding="utf-8")
        temporary.replace(ledger)
        return state
    finally:
        lock.rmdir()


def get(path: str, key: str, params: dict | None = None) -> dict:
    if path not in ("/oapi/my/info", "/twitter/tweet/advanced_search"):
        raise Unavailable("endpoint_not_allowed")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"X-API-Key": key,
                                                 "User-Agent": "KaibaSocialResearch/1"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            body = response.read(4_000_001)
        if len(body) > 4_000_000:
            raise Unavailable("response_too_large")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("Retry-After", "")
        suffix = ":retry_after=" + retry if re.fullmatch(r"[0-9]{1,8}", retry) else ""
        raise Unavailable(f"http_{exc.code}{suffix}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Unavailable("network_unavailable") from None
    except (ValueError, UnicodeError):
        raise Unavailable("malformed_response") from None
    if not isinstance(data, dict):
        raise Unavailable("malformed_response")
    if data.get("status") == "error" or data.get("error") or data.get("success") is False:
        raise Unavailable("provider_semantic_error")
    return data


def created_ms(value) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if value > 100_000_000_000 else int(value * 1000)
    if isinstance(value, str):
        for parse in (lambda v: dt.datetime.fromisoformat(v.replace("Z", "+00:00")),
                      lambda v: dt.datetime.strptime(v, "%a %b %d %H:%M:%S %z %Y")):
            try:
                stamp = parse(value)
                if stamp.tzinfo:
                    return int(stamp.timestamp() * 1000)
            except ValueError:
                pass
    return None


def normalize(tweet: dict, observed_ms: int) -> dict | None:
    if not isinstance(tweet, dict) or not tweet.get("id"):
        return None
    author = tweet.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    text = str(tweet.get("text") or "")[:20000]
    return {"source": "twitterapi.io", "post_id": str(tweet["id"]),
            "first_seen_ms": observed_ms, "created_ms": created_ms(tweet.get("createdAt")),
            "author_id": str(author.get("id") or ""), "author": author.get("userName"),
            "followers": author.get("followers"), "url": tweet.get("url"), "text": text,
            "is_reply": tweet.get("isReply"), "is_quote": bool(tweet.get("quoted_tweet")),
            "is_retweet": bool(tweet.get("retweeted_tweet")),
            "evm_address_candidates": sorted(set(re.findall(r"\b0x[a-fA-F0-9]{40}\b", text))),
            "sol_address_candidates": sorted(set(re.findall(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", text)))}


def search(key: str, query: str, ledger: Path, output: Path, max_pages: int) -> dict:
    seen = set()
    if output.exists():
        for line in output.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(str(json.loads(line)["post_id"]))
            except (ValueError, KeyError, TypeError):
                raise Unavailable("invalid_existing_output") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    cursor = ""
    cursors = set()
    rows = pages = 0
    more = False
    for _ in range(max_pages):
        reserve(ledger, MAX_PAGE_CREDITS)
        data = get("/twitter/tweet/advanced_search", key,
                   {"query": query, "queryType": "Latest", "cursor": cursor})
        tweets = data.get("tweets")
        if not isinstance(tweets, list) or len(tweets) > 20:
            raise Unavailable("unexpected_search_shape")
        observed = int(time.time() * 1000)
        with output.open("a", encoding="utf-8") as stream:
            for tweet in tweets:
                post = normalize(tweet, observed)
                if post is not None and post["post_id"] not in seen:
                    stream.write(json.dumps(post, ensure_ascii=False) + "\n")
                    seen.add(post["post_id"])
                    rows += 1
        pages += 1
        more = data.get("has_next_page") is True
        next_cursor = data.get("next_cursor")
        if not tweets or not more:
            break
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            raise Unavailable("invalid_or_repeated_cursor")
        cursors.add(next_cursor)
        cursor = next_cursor
    return {"status": "ok", "pages": pages, "new_posts": rows,
            "reserved_usd": pages * MAX_PAGE_CREDITS / 100000,
            "truncated": more, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_ROOT / "social-research-budget.json")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("balance")
    sub = subs.add_parser("search")
    sub.add_argument("--query", required=True)
    sub.add_argument("--max-pages", type=int, choices=range(1, 6), default=1)
    sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        key = credential(args.key_file)
        if args.command == "balance":
            reserve(args.ledger, 15)
            data = get("/oapi/my/info", key)
            fields = {k: v for k, v in data.items() if k in
                      ("recharge_credits", "total_bonus_credits") and type(v) in (int, float)}
            if "recharge_credits" not in fields:
                raise Unavailable("unexpected_balance_shape")
            result = {"status": "ok", **fields}
        else:
            result = search(key, args.query, args.ledger, args.output, args.max_pages)
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
