"""Offline replay of the tweet launcher's creative half: which words, which logo.

Owner 2026-10-08: "stop auto launching for now it works but buggy as hell / we need to
rework / choosing the right words / generate image from the right words" -- then "yes test
everything". Nothing here sends anything. Three stages, each runnable on its own:

``extract``  reads the posts we recorded, what we launched on each, and what the MARKET
             launched on the same post (``tweet_sources``: tokens whose own metadata links
             the post, with graduation). Short reads, one connection per query, chunked by
             tweet id, refusing to start while the WAL is over the database rule's 1.5 GB.
             Output: ``<out>/posts.jsonl`` (small; /dev/shm on the box).
``pick``     runs the configured picker (Hermes on Claude OAuth, ``namer.hermes_model`` /
             ``hermes_reasoning``; optionally several model variants) and the logo maker
             (``logo.backends``) on every extracted post. No database. Output:
             ``<out>/picks.jsonl`` and ``<out>/logos/``.
``report``   one self-contained HTML page for the owner's review: per post the market's
             first token, the old pick, each new pick, the logo; and the agreement rates.

The yardstick is the market: a token whose metadata links the post is what traders
actually named it. "Agrees" = our ticker equals a market ticker for that post; the first
market launch is reported separately, since it is the only one with a measured edge
(``tweet_sources.board``).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

WAL_LIMIT_BYTES = 1_500_000_000
CHUNK = 200


# --------------------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------------------


def _db_path() -> Path:
    from kaiba.core.config import get_settings

    return Path(get_settings().db_path)


def _wait_for_wal(db: Path, *, limit: int = WAL_LIMIT_BYTES, wait_s: float = 60.0, tries: int = 20) -> None:
    wal = Path(str(db) + "-wal")
    for _ in range(tries):
        size = wal.stat().st_size if wal.exists() else 0
        if size <= limit:
            return
        log.warning("WAL %.2f GB > %.2f GB: waiting %.0f s (database read rule)", size / 1e9, limit / 1e9, wait_s)
        time.sleep(wait_s)
    raise SystemExit("WAL stayed over the limit; not reading the database")


def _query(db: Path, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """One short read: its own read-only connection, closed straight after."""
    _wait_for_wal(db)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _norm(sym: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (sym or "").upper())


def extract(out: Path, *, db: Path | None = None, limit: int = 120) -> int:
    """Write ``posts.jsonl``: every post we launched on live, plus posts the market launched on."""
    db = db or _db_path()
    launches = _query(db, "SELECT tweet_id, author, chain, dex, mode, verdict, state, name, symbol, image_source, "
                          "token, reasons_json, decided_ms FROM tweet_launches")
    by_tweet: dict[str, list[dict[str, Any]]] = {}
    for r in launches:
        by_tweet.setdefault(r["tweet_id"], []).append(r)
    ids = sorted(by_tweet)
    market: dict[str, list[dict[str, Any]]] = {}
    posts: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in _query(db, f"SELECT tweet_id, author, kind, text, media_json, created_ms FROM tweet_launch_tweets "
                            f"WHERE tweet_id IN ({marks})", tuple(chunk)):
            posts[r["tweet_id"]] = r
        for r in _query(db, "SELECT s.tweet_id, s.chain, s.token, s.name, s.symbol, s.launchpad, s.token_created_ms, "
                            "(t.migrated_ms IS NOT NULL OR EXISTS (SELECT 1 FROM graduation_observations g "
                            " WHERE g.chain = s.chain AND g.token = s.token)) AS graduated "
                            "FROM tweet_sources s LEFT JOIN tokens t ON t.chain = s.chain AND t.address = s.token "
                            f"WHERE s.kind = 'tweet' AND s.tweet_id IN ({marks})", tuple(chunk)):
            market.setdefault(r["tweet_id"], []).append(r)
    ours = [r["token"] for r in launches if r["state"] == "confirmed" and r["token"]]
    outcome: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ours), CHUNK):
        chunk = ours[i:i + CHUNK]
        marks = ",".join("?" * len(chunk))
        for r in _query(db, "SELECT token, cost_native, realized_native, exit_reason, closed_ms FROM positions "
                            f"WHERE lane = 'tweet-launch' AND token IN ({marks})", tuple(chunk)):
            outcome[r["token"]] = r

    rows = []
    for tid, ls in by_tweet.items():
        post = posts.get(tid)
        if post is None:
            continue
        mk = sorted(market.get(tid, []), key=lambda m: m["token_created_ms"] or 0)
        live = [x for x in ls if x["mode"] == "live" and x["state"] == "confirmed"]
        if not live and not mk:
            continue
        rows.append({
            "tweet_id": tid, "author": post["author"], "kind": post["kind"], "text": post["text"],
            "media": json.loads(post["media_json"] or "[]"), "created_ms": post["created_ms"],
            "old": [{"chain": x["chain"], "dex": x["dex"], "state": x["state"], "verdict": x["verdict"],
                     "name": x["name"], "symbol": x["symbol"], "image_source": x["image_source"],
                     "token": x["token"], "outcome": outcome.get(x["token"] or "")} for x in ls],
            "market": [{k: m[k] for k in ("chain", "token", "name", "symbol", "launchpad", "token_created_ms")}
                       | {"graduated": bool(m["graduated"])} for m in mk],
        })
    # Every post we launched on, then market-launched posts with a graduation, then the rest.
    rows.sort(key=lambda r: (not any(o["state"] == "confirmed" for o in r["old"]),
                             not any(m["graduated"] for m in r["market"]), -(r["created_ms"] or 0)))
    rows = rows[:limit]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "posts.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")
    log.info("extract: %d posts (%d with our live launch, %d with market launches)", len(rows),
             sum(any(o["state"] == "confirmed" for o in r["old"]) for r in rows), sum(bool(r["market"]) for r in rows))
    return len(rows)


# --------------------------------------------------------------------------------------
# pick
# --------------------------------------------------------------------------------------


def _variants(spec: list[str], cfg: Any) -> list[tuple[str, str | None, str | None]]:
    """``model:reasoning`` strings -> (label, model, reasoning); default = the shipped config."""
    if not spec:
        return [(f"{cfg.hermes_model or 'profile'}:{cfg.hermes_reasoning or 'default'}",
                 cfg.hermes_model, cfg.hermes_reasoning)]
    out = []
    for s in spec:
        model, _, reasoning = s.partition(":")
        out.append((s, model or None, reasoning or None))
    return out


def pick(out: Path, *, variants: list[str], workers: int = 3, logos: bool = True,
         namer: Any = None, logo_maker: Any = None) -> int:
    from kaiba.execution import tweet_creative as tc
    from kaiba.execution import tweet_launch as tl

    cfg = tl.load_config()
    rows = [json.loads(line) for line in (out / "posts.jsonl").read_text(encoding="utf-8").splitlines() if line]
    vs = _variants(variants, cfg)
    (out / "logos").mkdir(parents=True, exist_ok=True)
    namer = namer or tc.hermes_identity
    logo_maker = logo_maker or tc.make_logo

    def one(row: dict[str, Any]) -> dict[str, Any]:
        res: dict[str, Any] = {"tweet_id": row["tweet_id"], "heuristic": None, "picks": {}}
        h = tl.derive_identity(row["text"] or "")
        res["heuristic"] = None if h is None else {"name": h.name, "symbol": h.symbol, "basis": h.basis}
        first = None
        for label, model, reasoning in vs:
            t0 = time.monotonic()
            got = namer(row["text"] or "", row["author"], profile=cfg.hermes_profile, timeout_s=90,
                        majors=tl.MAJORS, model=model, reasoning=reasoning)
            res["picks"][label] = None if got is None else {
                "launch": got.launch, "name": got.name, "symbol": got.symbol, "logo_prompt": got.logo_prompt,
                "reason": got.reason}
            res["picks"][label + "#s"] = round(time.monotonic() - t0, 1)
            first = first or got
        if logos and first is not None:
            t0 = time.monotonic()
            logo = logo_maker(tuple(row["media"] or ()), first.name, first.symbol, first.logo_prompt,
                              timeout_s=cfg.logo_timeout_s, backends=cfg.logo_backends)
            res["logo"] = {"source": logo.source, "url": logo.url, "note": logo.note,
                           "s": round(time.monotonic() - t0, 1)}
            if logo.b64:
                path = out / "logos" / f"{row['tweet_id']}.jpg"
                path.write_bytes(base64.b64decode(logo.b64))
                res["logo"]["file"] = path.name
        return res

    done = {}
    picks_path = out / "picks.jsonl"
    if picks_path.exists():                       # resumable: keep what earlier runs finished
        for line in picks_path.read_text(encoding="utf-8").splitlines():
            if line:
                r = json.loads(line)
                done[r["tweet_id"]] = r
    todo = [r for r in rows if r["tweet_id"] not in done]
    with open(picks_path, "a", encoding="utf-8") as fh, ThreadPoolExecutor(max(1, workers)) as ex:
        for res in ex.map(one, todo):
            fh.write(json.dumps(res) + "\n")
            fh.flush()
            log.info("pick %s %s", res["tweet_id"], {k: (v or {}).get("symbol") if isinstance(v, dict) or v is None
                                                     else v for k, v in res["picks"].items()})
    return len(todo)


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def score(rows: list[dict[str, Any]], picks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per picker, over posts the market launched on: did it name the market's ticker, and would
    it have launched -- overall and on the posts whose market token graduated?"""
    labels = sorted({k for p in picks.values() for k in p["picks"] if not k.endswith("#s")})
    tally: dict[str, dict[str, Any]] = {}

    def bump(who: str, sym: str | None, mk: list[dict[str, Any]], launch: bool | None,
             secs: float | None = None) -> None:
        t = tally.setdefault(who, {"posts": 0, "named": 0, "any": 0, "first": 0, "graduated": 0,
                                   "grad_posts": 0, "launch": 0, "launch_on_grad": 0, "secs": []})
        t["posts"] += 1
        grad_post = any(m["graduated"] for m in mk)
        t["grad_posts"] += grad_post
        if launch:
            t["launch"] += 1
            t["launch_on_grad"] += grad_post
        if secs is not None:
            t["secs"].append(secs)
        s = _norm(sym)
        if not s:
            return
        t["named"] += 1
        t["any"] += s in {_norm(m["symbol"]) for m in mk}
        t["first"] += s == _norm(mk[0]["symbol"])
        t["graduated"] += s in {_norm(m["symbol"]) for m in mk if m["graduated"]}

    for r in rows:
        # Our own launches link the post too; they are not the market's verdict on it.
        ours = {o["token"] for o in r["old"] if o.get("token")}
        mk = [m for m in r["market"] if m["token"] not in ours]
        if not mk:
            continue
        p = picks.get(r["tweet_id"])
        old = next((o for o in r["old"] if o["symbol"]), None)
        bump("old picker (as shipped)", old["symbol"] if old else None, mk,
             any(o["state"] == "confirmed" for o in r["old"]))
        if p is None:
            continue
        bump("word heuristic (today)", (p.get("heuristic") or {}).get("symbol"), mk, None)
        for lab in labels:
            got = p["picks"].get(lab)
            bump(lab, got["symbol"] if got else None, mk, bool(got and got.get("launch")),
                 p["picks"].get(lab + "#s"))
    for t in tally.values():
        s = sorted(t.pop("secs"))
        t["median_s"] = s[len(s) // 2] if s else None
    return tally


_CSS = """
/* Review sheet: summary table first, then one row per post, newest evidence on the right. */
:root{--bg:#f6f7f4;--fg:#1b1f1c;--mut:#5f6862;--line:#dfe3de;--card:#ffffff;--ok:#1c7a4a;--bad:#b4332a;
--acc:#2f5d8a;--chip:#eef1ec;--ui:'IBM Plex Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#121513;--fg:#e6ebe7;--mut:#97a29a;
--line:#2a302c;--card:#181c19;--ok:#5ccb8e;--bad:#f0877c;--acc:#8db6e0;--chip:#202622;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#121513;--fg:#e6ebe7;--mut:#97a29a;--line:#2a302c;--card:#181c19;--ok:#5ccb8e;
--bad:#f0877c;--acc:#8db6e0;--chip:#202622;color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:15px/1.5 var(--ui);padding-inline:16px;padding-block:24px 48px}
.wrap{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
h1{font-size:24px;line-height:1.2;margin:0;text-wrap:balance}
h2{font-size:15px;margin:0;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
p{margin:0;max-width:72ch}.mut{color:var(--mut)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;font-size:12px;letter-spacing:.03em;text-transform:uppercase;color:var(--mut)}
tr:last-child td{border-bottom:0}
.post{display:grid;grid-template-columns:88px minmax(0,1fr);gap:14px;padding:14px 0;border-top:1px solid var(--line)}
.logo{width:88px;height:88px;border-radius:50%;object-fit:cover;background:var(--chip);display:block}
.body{min-width:0;display:flex;flex-direction:column;gap:6px}
.text{white-space:pre-wrap;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{font:12.5px/1.5 var(--mono);padding:1px 8px;border-radius:6px;background:var(--chip);border:1px solid var(--line)}
.chip.ok{color:var(--ok);border-color:var(--ok)}.chip.bad{color:var(--bad);border-color:var(--bad)}
.lbl{font-size:12px;color:var(--mut);min-width:5.5em}
.line{display:flex;flex-wrap:wrap;gap:6px;align-items:baseline}
@media (max-width:560px){.post{grid-template-columns:56px minmax(0,1fr)}.logo{width:56px;height:56px}}
"""

_FONTS = ("<link rel='preconnect' href='https://fonts.googleapis.com'>"
          "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500"
          "&family=IBM+Plex+Sans:wght@400;600&display=swap'>")


def _pct(n: int, d: int) -> str:
    return f"{n} <span class=mut>({100 * n / d:.0f}%)</span>" if d else "0"


def report(out: Path, *, title: str = "Tweet Launcher Replay") -> Path:
    rows = [json.loads(x) for x in (out / "posts.jsonl").read_text(encoding="utf-8").splitlines() if x]
    picks = {}
    if (out / "picks.jsonl").exists():
        for x in (out / "picks.jsonl").read_text(encoding="utf-8").splitlines():
            if x:
                r = json.loads(x)
                picks[r["tweet_id"]] = r
    tally = score(rows, picks)
    e = html.escape
    logo_src: dict[str, int] = {}
    for p in picks.values():
        src = (p.get("logo") or {}).get("source")
        if src:
            logo_src[src] = logo_src.get(src, 0) + 1
    parts = [f"<title>{e(title)}</title>{_FONTS}<style>{_CSS}</style><div class=wrap>",
             f"<h1>{e(title)}</h1>",
             f"<p class=mut>{len(rows)} recorded posts from the watched accounts, {len(picks)} replayed through the new "
             "picker. Nothing was launched. <b>Market</b> = tokens other people launched whose own metadata links the "
             "post; first = earliest, grad = graduated off the curve. Green ticker = matches a market ticker for that "
             "post, red = does not.</p>",
             "<h2>Scorecard</h2><div class=scroll><table><tr><th>picker</th><th>posts</th><th>would launch</th>"
             "<th>launch on grad posts</th><th>= a market ticker</th><th>= first market ticker</th>"
             "<th>= graduated ticker</th><th>median s</th></tr>"]
    for who, t in tally.items():
        n = t["posts"]
        parts.append(
            f"<tr><td>{e(who)}</td><td>{n}</td><td>{_pct(t['launch'], n) if who != 'word heuristic (today)' else '-'}"
            f"</td><td>{t['launch_on_grad']} / {t['grad_posts']}</td><td>{_pct(t['any'], n)}</td>"
            f"<td>{_pct(t['first'], n)}</td><td>{t['graduated']}</td>"
            f"<td>{'-' if t['median_s'] is None else t['median_s']}</td></tr>")
    parts.append("</table></div>")
    if logo_src:
        parts.append("<p class=mut>Logos made in this replay: " + ", ".join(
            f"{e(k)} {v}" for k, v in sorted(logo_src.items(), key=lambda kv: -kv[1])) + ".</p>")
    parts.append("<h2>Posts</h2><div>")
    for r in rows:
        p = picks.get(r["tweet_id"], {})
        logo = p.get("logo") or {}
        if logo.get("file") and (out / "logos" / logo["file"]).exists():
            b = base64.b64encode((out / "logos" / logo["file"]).read_bytes()).decode()
            img = f"<img class=logo alt='' src='data:image/jpeg;base64,{b}'>"
        else:
            img = "<div class=logo></div>"
        ours = {o["token"] for o in r["old"] if o.get("token")}
        msyms = {_norm(m["symbol"]) for m in r["market"] if m["token"] not in ours}

        def chip(sym: str | None, label: str, note: str = "", msyms: set[str] = msyms) -> str:
            if not sym:
                return f"<span class=chip>{e(label)}: none{e(note)}</span>"
            cls = " ok" if _norm(sym) in msyms else (" bad" if msyms else "")
            return f"<span class='chip{cls}'>{e(label)}: ${e(sym)}{e(note)}</span>"

        oc = []
        for o in r["old"]:
            if o["state"] != "confirmed":
                continue
            res = o.get("outcome") or {}
            try:
                pct = 100 * int(res.get("realized_native") or 0) / max(1, int(res.get("cost_native") or 1))
                oc.append(f"{o['chain']} {pct:+.0f}% {res.get('exit_reason') or ''}")
            except (TypeError, ValueError):
                oc.append(o["chain"])
        old = next((o["symbol"] for o in r["old"] if o["symbol"]), None)
        new = []
        reasons = []
        for k, v in p.get("picks", {}).items():
            if k.endswith("#s"):
                continue
            secs = p["picks"].get(k + "#s")
            if v is None:
                new.append(f"<span class=chip>{e(k)}: no answer ({secs}s)</span>")
                continue
            verdict = "launch" if v.get("launch") else "skip"
            new.append(chip(v.get("symbol") or None, k, f" - {verdict} ({secs}s)"))
            if v.get("reason"):
                reasons.append(f"{k.split(':')[0].replace('claude-', '')}: {v['reason']}")
        mk = "".join(
            f"<span class='chip{' ok' if m['graduated'] else ''}'>{'ours ' if m['token'] in ours else ''}"
            f"{'first ' if i == 0 else ''}${e(m['symbol'] or '?')}"
            f" {e((m['name'] or '')[:22])}{' grad' if m['graduated'] else ''} - {e(m['launchpad'] or m['chain'])}</span>"
            for i, m in enumerate(r["market"][:6]))
        when = time.strftime("%m-%d %H:%M", time.gmtime((r["created_ms"] or 0) / 1000))
        parts.append(
            f"<div class=post>{img}<div class=body><div><b>@{e(r['author'])}</b> "
            f"<span class=mut>{e(r['kind'] or '')} - {when} UTC</span></div>"
            f"<div class=text>{e((r['text'] or '')[:420])}</div>"
            f"<div class=line><span class=lbl>Market</span><span class=chips>"
            f"{mk or '<span class=mut>none recorded</span>'}</span></div>"
            f"<div class=line><span class=lbl>We shipped</span><span class=chips>{chip(old, 'old')}"
            f"{''.join(f'<span class=chip>{e(x)}</span>' for x in oc)}</span></div>"
            f"<div class=line><span class=lbl>New picker</span><span class=chips>"
            f"{chip((p.get('heuristic') or {}).get('symbol'), 'heuristic')}{''.join(new)}</span></div>"
            + (f"<div class=mut>{e(' | '.join(reasons))}</div>" if reasons else "")
            + (f"<div class=mut>logo: {e(logo.get('source', ''))} {logo.get('s', '')}s "
               f"{e((logo.get('note') or '')[:120])}</div>" if logo else "")
            + "</div></div>")
    parts.append("</div></div>")
    path = out / "tweet-launcher-replay.html"
    path.write_text("".join(parts), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.execution.tweet_replay")
    ap.add_argument("--out", type=Path, default=Path("/dev/shm/tweet_replay"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("extract")
    x.add_argument("--limit", type=int, default=120)
    p = sub.add_parser("pick")
    p.add_argument("--variant", action="append", default=[], help="model:reasoning (repeatable)")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--no-logos", action="store_true")
    sub.add_parser("report")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.cmd == "extract":
        extract(a.out, limit=a.limit)
    elif a.cmd == "pick":
        pick(a.out, variants=a.variant, workers=a.workers, logos=not a.no_logos)
    else:
        print(report(a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
