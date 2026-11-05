"""Read-outs of the two research labs, for the dashboard and the agent (2026-10-28).

* Wallet behavior lab -- ``data/labs/wallet/``, written daily by
  ``deploy/research/wallet_lab/daily.py``: one behavior profile per tracked sol S/A/B wallet, plus
  the pre-registered follower test's verdict (``lab_result.json``,
  docs/research/claude-wallet-lab-prereg-20261028.md).
* DLMM LP lab -- ``data/labs/lp/result.json``, written daily by
  ``deploy/research/lp_replay/lp_lab.py daily``: what real Meteora DLMM positions in a frozen panel
  of pools earned against holding, per pool and forward from the freeze
  (docs/research/claude-lp-lab-20261028.md).

Nothing here calls a provider, reads the database or moves value. Every reader returns ``None`` or
an ``available: False`` dict when the job has not written yet -- never a guess. ``lp_decision`` is
the only judgement: it applies the lab's MEASURED terciles (read from the result file, not
constants) to one position and says which way the evidence points, with the numbers behind it.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings

#: A result older than this is reported as stale: the timers run daily.
STALE_S = 36 * 3600
LP_BOT_STALE_S = 300
LP_BOARD_TTL_S = 30
_board_cache: dict[str, tuple[float, dict[str, Any]]] = {}
LP_TRACKS = {
    "fast": ("Fast scalp", "Hot pools, tight ranges, held for seconds to minutes, with a two-sided re-open."),
    "slow": ("Slow wide", "Hot pools, wide two-sided ranges, held for hours."),
    "dump": ("Post-dump", "A runner that fell 60-75% with its selling faded, held from -50% to +100%."),
    "filtered": ("Filtered pools", "SOL only below the price, in the pool types that held up on 41,000 real positions, held for hours."),
}
#: Why the selector passed over a pool, as a person reads it.
LP_SKIPS = {
    "busy": "already held or cooling off", "volume": "too little volume", "fee_tvl": "fees too thin", "tvl": "too small",
    "bin_step": "range step too wide", "bin_step_too_fine": "range step too fine", "base_fee": "base fee too high",
    "quote_not_sol_y": "not a SOL pair", "freeze_authority": "token can be frozen", "transfer_fee": "token charges a fee",
    "transfer_hook": "token has a transfer hook", "mint_unreadable": "token unreadable", "blacklisted": "blacklisted",
    "setup_no_run": "no big run first", "setup_short_history": "too new", "setup_top_too_recent": "peak too recent",
    "setup_drop_out_of_band": "fall not 60-75%", "setup_selling_not_faded": "still selling hard",
    "setup_recheck_later": "checked recently", "setup_budget": "not checked this pass", "bin_step_skipped": "losing pool type", "too_new": "pool under an hour old", "age_unknown": "pool age unknown", "dumped_last_hour": "fell 20% in the last hour",
}
CLASS_NOTES = {
    "sniper": "median entry under 60 s after the token was created",
    "fast_flipper": "median first sell under 2 minutes after the first buy",
    "early_accumulator": "enters 1-60 min after creation and holds at least 30 min before selling",
    "dip_buyer": "buys at or below 60% of the prior 6 h high",
    "breakout_buyer": "buys at or above 90% of the prior 6 h high",
    "multi_entry": "adds on a 30% dip or re-enters after a sell in at least 40% of tokens",
    "staged_exit": "first sell takes at most 60% of the bag",
    "small_size": "first buy at most 0.05% of market cap",
}

_cache: dict[str, tuple[float, Any]] = {}


def labs_dir(data_dir: Path | None = None) -> Path:
    return Path(data_dir or get_settings().kaiba_data_dir) / "labs"


def _read(path: Path) -> Any:
    """Parsed JSON (gzip or plain), cached until the file changes; None when absent or unreadable."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    _cache[str(path)] = (mtime, value)
    return value


def _freshness(at: Any) -> dict[str, Any]:
    try:
        age = max(0, int(time.time()) - int(at))
    except (TypeError, ValueError):
        return {"at": None, "age_s": None, "stale": True}
    return {"at": int(at), "age_s": age, "stale": age > STALE_S}


def _dig(d: Any, *path: str) -> Any:
    for key in path:
        d = d.get(key) if isinstance(d, dict) else None
    return d


def _pick(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Exactly ``keys``, None where the file lacks one: readers and templates rely on the shape."""
    return {k: (d.get(k) if isinstance(d, dict) else None) for k in keys}


STAT = ("n", "mean", "median", "pos_share")
PROFILE = ("tier", "name", "episodes", "classes", "F1_entry_age_s", "F2_entry_vs_prior_high", "F5_usd",
           "F5_bps_of_mcap", "F6_first_sell_s", "F6_first_sell_frac", "F6_left_at_end", "F7_hold_s",
           "share_multi_entry", "share_fully_exited", "f2_priced", "closed", "closed_won", "closed_net_usd",
           "launchpads", "last_trade", "activity_complete")
POOL = ("address", "name", "quote", "positions", "lps", "lp_minus_hodl", "in_range", "open", "open_in_range",
        "per_sol_day", "tvl", "fee_24h", "fee_tvl_24h", "blacklisted", "gone")


def _profile(row: dict[str, Any]) -> dict[str, Any]:
    out = _pick(row, PROFILE)
    out["classes"] = list(out["classes"] or [])
    out["episodes"] = int(out["episodes"] or 0)
    return out


# --------------------------------------------------------------------------------------
# wallet behavior lab
# --------------------------------------------------------------------------------------


def wallet_behavior(address: str, data_dir: Path | None = None) -> dict[str, Any] | None:
    """One tracked wallet's 14-day behavior profile, or None when the lab has not profiled it."""
    prof = _read(labs_dir(data_dir) / "wallet" / "profiles.json.gz")
    if not isinstance(prof, dict):
        return None
    row = (prof.get("wallets") or {}).get(address)
    if not isinstance(row, dict):
        return None
    row = _profile(row)
    return {"address": address, **row, "class_notes": {c: CLASS_NOTES.get(c, "") for c in row["classes"]},
            **_freshness(prof.get("at")), "window_days": prof.get("window_days"),
            "caveat": "descriptive: how the wallet trades, not whether following it pays (see the lab verdict)"}


def wallet_lab(data_dir: Path | None = None, limit: int = 200) -> dict[str, Any]:
    """Class counts, the profiled wallets (best tier first) and the pre-registered test's verdict."""
    base = labs_dir(data_dir) / "wallet"
    summary = _read(base / "summary.json")
    prof = _read(base / "profiles.json.gz")
    if not isinstance(summary, dict) or not isinstance(prof, dict):
        return {"available": False, "reason": "the daily wallet-lab job has not written yet"}
    rows = [{"address": a, **_profile(p)} for a, p in (prof.get("wallets") or {}).items() if isinstance(p, dict)]
    rows = [r for r in rows if r["episodes"]]
    rows.sort(key=lambda r: (str(r["tier"] or "Z"), -r["episodes"]))
    summary = {**_pick(summary, ("tracked", "profiled", "classed", "episodes")),
               "class_counts": {c: int(_dig(summary, "class_counts", c) or 0) for c in CLASS_NOTES}}
    return {"available": True, **_freshness(prof.get("at")), "summary": summary,
            "wallets": rows[: max(1, int(limit))], "class_notes": CLASS_NOTES, "verdict": lab_verdict(data_dir)}


def lab_verdict(data_dir: Path | None = None) -> dict[str, Any]:
    """The pre-registered follower test (graded wallets, test half): passing cells, best cells."""
    res = _read(labs_dir(data_dir) / "wallet" / "lab_result.json")
    if not isinstance(res, dict) or not isinstance(res.get("table"), list):
        return {"available": False, "reason": "the one-off follower test has not finished"}
    keys = ("class", "exit", "n", "wallets", "mean", "median", "win", "lb95", "beats_control", "PASS")
    graded = [_pick(r, keys) for r in res["table"] if isinstance(r, dict) and r.get("group") == "graded" and r.get("n")]
    graded.sort(key=lambda r: -(r["mean"] if r["mean"] is not None else -9))
    return {"available": True, "passing": [r for r in graded if r["PASS"]], "cells": graded,
            "test_episodes": res.get("test_episodes"), "test_censored": res.get("test_censored"),
            "pass_line": "n>=100 from >=10 wallets, mean>0, wallet-clustered 95% lower bound>0, beats the control class"}


# --------------------------------------------------------------------------------------
# DLMM LP lab
# --------------------------------------------------------------------------------------


def _split_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    """The terciles in one shape for the page: label, unit, cuts, and mean per tercile and half."""
    out = []
    for label, path, unit in (("Time in range", ("Q3_in_range",), "share"), ("Duration", ("Q4", "dur_s"), "s"),
                              ("Width", ("Q4", "width"), "share"), ("Move in the 6 h before", ("Q4", "prior_move"), "share")):
        sp = _dig(res, *path)
        if not isinstance(sp, dict) or not sp.get("cuts"):
            continue
        out.append({"label": label, "unit": unit, "cuts": list(sp["cuts"]),
                    "cells": {t: {h: _dig(sp, t, h, "mean") for h in ("all", "older", "newer")} for t in ("low", "mid", "high")}})
    return out


def lp_lab(data_dir: Path | None = None) -> dict[str, Any]:
    """Headline numbers, the measured splits, the forward record and the per-pool table.

    Every key is always present (None where the result file lacks it); the raw tercile dicts
    (``in_range``, ``duration``, ``width``, ``frequent_lp``) are what :func:`lp_decision` reads."""
    res = _read(labs_dir(data_dir) / "lp" / "result.json")
    if not isinstance(res, dict) or "Q1" not in res:
        return {"available": False, "reason": "the daily LP-lab job has not written yet"}
    fwd = res.get("forward") if isinstance(res.get("forward"), dict) else None
    return {
        "available": True, **_freshness(res.get("at")),
        "positions": res.get("positions"), "open_positions": res.get("open_positions"),
        "pools": res.get("pools"), "lps": res.get("owners"),
        "vs_sol": _pick(_dig(res, "Q1", "lp_sol", "all"), STAT),
        "vs_usd": _pick(_dig(res, "Q1", "lp_usd", "all"), STAT),
        "vs_holding_token": _pick(_dig(res, "Q1", "lp_minus_hodl", "all"), STAT),
        "halves": {h: _pick(_dig(res, "Q1", "lp_sol", h), STAT) for h in ("older", "newer")},
        "fees_cover_share": res.get("Q2_fees_cover_share"),
        "fees_mean": _dig(res, "Q2_fees", "mean"), "inventory_mean": _dig(res, "Q2_inventory", "mean"),
        "per_sol_day": _dig(res, "capital_day", "all", "all", "per_sol_day"),
        "pnl_sol": _dig(res, "capital_day", "all", "all", "pnl_sol"),
        "per_lp": _pick(res.get("per_lp"), ("lps", "share_positive", "median_total_return")),
        "frequent": {"yes": _dig(res, "capital_day", "frequent_lp", "True", "all", "per_sol_day"),
                     "no": _dig(res, "capital_day", "frequent_lp", "False", "all", "per_sol_day")},
        "forward": None if fwd is None else {
            "since": fwd.get("since"), "n": _dig(fwd, "lp_sol", "n") or 0, "mean": _dig(fwd, "lp_sol", "mean"),
            "vs_holding": _dig(fwd, "lp_minus_hodl", "mean"), "per_sol_day": _dig(fwd, "capital_day", "per_sol_day"),
            "pools": fwd.get("pools")},
        "splits": _split_rows(res),
        "in_range": res.get("Q3_in_range"), "duration": _dig(res, "Q4", "dur_s"), "width": _dig(res, "Q4", "width"),
        "frequent_lp": _dig(res, "capital_day", "frequent_lp"),
        "pools_table": [{**_pick(p, POOL), "lp_sol": _pick(p.get("lp_sol"), STAT)}
                        for p in res.get("pools_table") or [] if isinstance(p, dict)],
        "caveats": ["pools sampled by CREATION date instead (phase 1b, 120 pools, 94% dead now) lost -0.29% per "
                    "SOL-day vs holding SOL: the headline here is mostly survivorship "
                    "(docs/research/claude-dlmm-birth-pools-20261028.md)",
                    "pools were picked by fees on the freeze day; the forward section is the unbiased part",
                    "LPs studied all had a position open at the freeze",
                    "bin-array rent (~0.07 SOL when a range needs a new bin array) and tx fees are not in pnlSol"],
    }


def lp_pool(address: str, data_dir: Path | None = None) -> dict[str, Any] | None:
    lab = lp_lab(data_dir)
    return next((p for p in lab.get("pools_table") or [] if p.get("address") == address), None)


def _tercile(split: dict[str, Any] | None, value: float) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(split, dict) or not split.get("cuts"):
        return None
    lo, hi = split["cuts"]
    name = "low" if value < lo else "mid" if value < hi else "high"
    return name, split.get(name) or {}


def _mean(cell: dict[str, Any], half: str = "all") -> float | None:
    return (cell.get(half) or {}).get("mean")


def _pct(x: float | None) -> str:
    return f"{x:+.2%}" if x is not None else "n/a"


def _width_note(width: dict[str, Any] | None) -> str:
    """Whether one width tercile was best in BOTH halves; the lab's finding rule, read live."""
    if not isinstance(width, dict) or not width.get("cuts"):
        return "no width measurement yet"
    best = {h: max(("low", "mid", "high"), key=lambda n: _mean(width.get(n) or {}, h) or -9) for h in ("older", "newer")}
    if best["older"] == best["newer"]:
        return f"the {best['older']} width tercile (cuts {width['cuts']}) was best in both halves"
    return "no width tercile was best in both halves: there is no measured case for narrowing"


def lp_decision(in_range_share: float, hours_open: float, out_of_range_now: bool = False,
                pool: str = "", data_dir: Path | None = None) -> dict[str, Any]:
    """keep / rebalance / close for one DLMM position, from the lab's measured terciles.

    ``in_range_share`` is the fraction of the position's life the price sat inside its bins (0-1).
    Order of the rules: a pool whose panel LPs lost AND did worse than holding -> close; a position
    out of range now -> rebalance when frequent re-centering measured better per SOL-day, else close;
    otherwise keep. The answer is evidence, not an order: Kaiba holds no LP position."""
    lab = lp_lab(data_dir)
    if not lab.get("available"):
        return {"decision": None, "reason": lab.get("reason")}
    share = max(0.0, min(1.0, float(in_range_share)))
    hours = max(0.0, float(hours_open))
    evidence: list[str] = []
    rng = _tercile(lab.get("in_range"), share)
    if rng:
        name, cell = rng
        evidence.append(f"time in range {share:.0%} is the {name} tercile (cuts {lab['in_range']['cuts']}): LPs there "
                        f"returned {_pct(_mean(cell))} vs SOL (older {_pct(_mean(cell, 'older'))}, "
                        f"newer {_pct(_mean(cell, 'newer'))})")
    dur = _tercile(lab.get("duration"), hours * 3600)
    if dur:
        evidence.append(f"{hours:.1f} h open is the {dur[0]} duration tercile: {_pct(_mean(dur[1]))} vs SOL")
    freq = lab.get("frequent_lp") or {}
    f_yes = ((freq.get("True") or {}).get("all") or {}).get("per_sol_day")
    f_no = ((freq.get("False") or {}).get("all") or {}).get("per_sol_day")
    pool_row = lp_pool(pool, data_dir) if pool else None
    pool_mean = ((pool_row or {}).get("lp_sol") or {}).get("mean")
    if pool_row:
        evidence.append(f"{pool_row.get('name')}: panel LPs {_pct(pool_mean)} vs SOL over {pool_row.get('positions')} "
                        f"positions, {_pct(pool_row.get('lp_minus_hodl'))} vs holding the deposit")
    if pool_mean is not None and pool_mean < 0 and (pool_row.get("lp_minus_hodl") or 0) < 0:
        decision = "close"
        evidence.append("this pool's LPs lost and did worse than simply holding")
    elif out_of_range_now:
        if f_yes is not None and f_no is not None and f_yes > f_no:
            decision = "rebalance"
            evidence.append(f"LPs who re-center often earned {_pct(f_yes)} per SOL-day vs {_pct(f_no)}: idle bins "
                            "cost more than re-centering in this sample")
        else:
            decision = "close"
            evidence.append("out of range earns no fees, and re-centering did not measure better here")
    else:
        decision = "keep"
        evidence.append("in range: the measured gains came from positions that stayed in range and were held longest")
    evidence.append(_width_note(lab.get("width")))
    return {"decision": decision, "evidence": evidence, "pool": pool_row, **_freshness(lab.get("at")),
            "note": "research evidence from real positions, not an order; Kaiba runs no LP lane"}


def _describe_arm(arm: Any) -> str:
    hold = f"{arm.max_hold_s // 3600} h" if arm.max_hold_s >= 3600 else f"{arm.max_hold_s} s"
    if arm.range_x:
        return f"-50% to +100%, {hold}, stop {arm.stop_pct:g}%"
    if arm.two_sided:
        return f"±{arm.two_sided} bins, {hold}, stop {arm.stop_pct:g}%"
    return f"{arm.width} bins below, {hold}" + (f", re-open ±{arm.reopen_width}" if arm.reopen_width else "")


def _pass_rows(verdict: dict[str, Any], profile: Any) -> list[dict[str, Any]]:
    """The pass line as five plain rows. A check with no data yet is pending, never failed."""
    checks = verdict.get("checks") or {}
    mean, lower, halves = verdict.get("mean_net_pct"), verdict.get("lower95"), verdict.get("halves") or []
    return [
        {"label": "Trades", "value": f"{verdict.get('n', 0):,} of {profile.pass_min_n}", "ok": checks.get("n", False)},
        {"label": "Days", "value": f"{verdict.get('days', 0)} of {profile.pass_min_days}", "ok": checks.get("days", False)},
        {"label": f"Mean at least +{profile.pass_min_mean_pct:.2f}%", "value": "n/a" if mean is None else f"{mean:+.2f}%",
         "ok": checks.get("mean", False)},
        {"label": "Worst-case mean above 0", "value": "needs 30 trades" if lower is None else f"{lower:+.2f}%",
         "ok": checks.get("lower95", False)},
        {"label": "Both halves positive", "value": "n/a" if not halves else " / ".join(f"{h:+.2f}%" for h in halves),
         "ok": checks.get("halves", False)},
    ]


def _lp_track(name: str, profile: Any, base: Path) -> dict[str, Any]:
    from kaiba.execution.lp_paper import ledger, report

    root = profile.root(base)
    title, blurb = LP_TRACKS.get(name, (name, ""))
    track: dict[str, Any] = {"name": name, "title": title, "blurb": blurb, "primary": profile.primary, "available": False}
    status = _read(root / "status.json")
    if not isinstance(status, dict):
        return track
    at = status.get("at_ms")
    age_s = max(0.0, time.time() - at / 1000) if isinstance(at, int | float) else None
    rep = report(root, profile, arm_lower=False)
    verdict = rep.get("verdict") or {}
    described = {a.name: _describe_arm(a) for a in profile.arms}
    arms = [{**cell, "name": arm, "about": described.get(arm, ""), "primary": arm == profile.primary}
            for arm, cell in (rep.get("arms") or {}).items()]
    seen = status.get("last_selection") or {}
    skips = sorted(((LP_SKIPS.get(k, k.replace("_", " ")), v) for k, v in seen.items() if k not in ("listed", "picked")),
                   key=lambda kv: -kv[1])
    return {**track, "available": True, "age_s": age_s, "stale": age_s is None or age_s > LP_BOT_STALE_S,
            "open": status.get("open", 0), "counts": status.get("counts") or {}, "listed": seen.get("listed", 0),
            "picked": seen.get("picked", 0), "skips": skips[:6], "verdict": verdict, "passed": bool(verdict.get("pass")),
            "pass_rows": _pass_rows(verdict, profile), "arms": sorted(arms, key=lambda a: (not a["primary"], a["name"])),
            "by_age": rep.get("primary_by_pool_age_h") or {}, **ledger(root, profile)}


def lp_board(data_dir: Path | None = None) -> dict[str, Any]:
    """Everything the /lp page shows. Reads the bots' own small stores, never the live database.

    Cached briefly: the page refreshes on a timer, and the pass line's lower bound is a bootstrap.
    """
    base = labs_dir(data_dir)
    hit = _board_cache.get(str(base))
    if hit and time.monotonic() - hit[0] < LP_BOARD_TTL_S:
        return hit[1]
    from kaiba.execution.lp_paper import PROFILES

    tracks = [_lp_track(name, profile, base / "lp_paper") for name, profile in PROFILES.items()]
    recent = sorted(({**r, "track": t["title"]} for t in tracks for r in t.get("recent") or []),
                    key=lambda r: -r["at_ms"])[:25]
    live = _read(base / "lp_live" / "status.json")
    board = {"tracks": tracks, "recent": recent, "live": live if isinstance(live, dict) else None,
             "any_passed": any(t.get("passed") for t in tracks), "at": time.time()}
    _board_cache[str(base)] = (time.monotonic(), board)
    return board


def best_wallets(data_dir: Path | None = None) -> dict[str, Any]:
    """The daily best-wallets list and how its earlier lists did afterwards. A list, never a signal."""
    board = _read(labs_dir(data_dir) / "wallet" / "best.json")
    if not isinstance(board, dict) or not board.get("at"):
        return {"available": False, "reason": "the first list is built by the daily wallet lab"}
    return {"available": True, **board, **_freshness(board.get("at"))}


def overview(data_dir: Path | None = None) -> dict[str, Any]:
    """Both labs for one page: the dashboard's /labs."""
    return {"wallet": wallet_lab(data_dir), "lp": lp_lab(data_dir), "lp_bot": lp_bot(data_dir), "best": best_wallets(data_dir)}


def lp_bot(data_dir: Path | None = None) -> dict[str, Any]:
    """The DLMM fee-scalp paper bot (kaiba.execution.lp_paper): its heartbeat and its verdict.

    A heartbeat over five minutes old is reported as stale: a stopped bot must not read as a quiet one.
    """
    from kaiba.execution.lp_paper import PROFILES, report

    profiles: dict[str, Any] = {}
    for name, profile in PROFILES.items():
        root = profile.root(labs_dir(data_dir) / "lp_paper")
        status = _read(root / "status.json")
        if not isinstance(status, dict):
            continue
        at = status.get("at_ms")
        age_s = max(0.0, time.time() - at / 1000) if isinstance(at, int | float) else None
        profiles[name] = {"age_s": age_s, "stale": age_s is None or age_s > LP_BOT_STALE_S, "status": status,
                          "report": report(root, profile)}
    return {"available": True, "profiles": profiles} if profiles else {"available": False, "reason": "LP bot has not run"}


def _ago(seconds: Any) -> str:
    if seconds is None:
        return "never"
    return f"{seconds / 3600:.0f} h ago" if seconds >= 3600 else f"{seconds / 60:.0f} min ago"


def render_lines(view: dict[str, Any], *, pools: int = 2) -> list[str]:
    """The daily report's LABS lines: one per lab that has run (the report is capped at ~30 lines)."""
    def r2(x: Any) -> str:
        return f"{x * 100:+.2f}%" if isinstance(x, int | float) else "n/a"

    lp, wl = view.get("lp") or {}, view.get("wallet") or {}
    if not lp.get("available") and not wl.get("available"):
        return ["LABS not run yet (LP lab, wallet lab)"]
    lines = []
    if lp.get("available"):
        vs, fwd = lp["vs_sol"], lp.get("forward")
        table = [p for p in lp["pools_table"] if p["lp_sol"]["mean"] is not None]
        best = ", ".join(f"{p['name']} {r2(p['lp_sol']['mean'])}" for p in table[:pools])
        worst = ", ".join(f"{p['name']} {r2(p['lp_sol']['mean'])}" for p in table[::-1][:pools] if p["lp_sol"]["mean"] < 0)
        lines.append(
            f"LABS LP ({_ago(lp['age_s'])}{' STALE' if lp['stale'] else ''}) {lp['pools']} DLMM pools, "
            f"{lp['positions']} positions: vs SOL {r2(vs['mean'])} (median {r2(vs['median'])}), "
            f"vs holding {r2(lp['vs_holding_token']['mean'])}, {r2(lp['per_sol_day'])}/SOL-day, "
            + (f"forward n {fwd['n']} {r2(fwd['mean'])}" if fwd else "forward from the first daily run")
            + (f" | best {best}" if best else "") + (f" | worst {worst}" if worst else ""))
    else:
        lines.append(f"LABS LP {lp.get('reason', 'no result')}")
    if wl.get("available"):
        sm, v = wl["summary"], wl["verdict"]
        counts = ", ".join(f"{c.replace('_', ' ')} {n}" for c, n in sm["class_counts"].items() if n)
        if not v.get("available"):
            test = f"follower test: {v.get('reason')}"
        elif v["passing"]:
            test = "follower test PASS (paper hypothesis, not a lane): " + ", ".join(
                f"{c['class']} {c['exit']} {r2(c['mean'])} n {c['n']}" for c in v["passing"])
        else:
            top = v["cells"][0] if v["cells"] else None
            test = "follower test: no cell passes" + (
                f" (best {top['class']} {top['exit']} {r2(top['mean'])} n {top['n']})" if top else "")
        lines.append(f"LABS wallets ({_ago(wl['age_s'])}{' STALE' if wl['stale'] else ''}) {sm['profiled']} of "
                     f"{sm['tracked']} sol S/A/B profiled, {sm['classed']} classed" + (f" ({counts})" if counts else "")
                     + f" | {test}")
    else:
        lines.append(f"LABS wallets {wl.get('reason', 'no result')}")
    best = view.get("best") or {}
    if best.get("available"):
        proof = (best.get("forward") or {}).get("proof") or {}
        lines.append(f"LABS best wallets ({_ago(best['age_s'])}{' STALE' if best['stale'] else ''}) {best.get('good', 0)} good of {best['eligible']} ranked, "
                     f"{best['lists']} daily lists; top 5 forward {proof.get('n', 0)} of {proof.get('need_n')} trades, "
                     + (f"mean {r2(proof['mean'])}" if isinstance(proof.get("mean"), int | float) else "no result yet")
                     + (" | PROVEN, the shadow lane decision is the owner's" if proof.get("pass") else " | not proven"))
    bot = view.get("lp_bot") or {}
    for name, one in (bot.get("profiles") or {}).items():
        verdict = (one["report"].get("verdict") or {})
        failing = ", ".join(k for k, ok in (verdict.get("checks") or {}).items() if not ok)
        mean = verdict.get("mean_net_pct")
        lines.append(f"LABS LP bot {name} (paper, {_ago(one['age_s'])}{' STALE' if one['stale'] else ''}) "
                     f"{verdict.get('arm')}: n {verdict.get('n', 0)}, {verdict.get('days', 0)} d, mean net "
                     + (f"{mean:+.2f}%" if isinstance(mean, int | float) else "n/a")
                     + (" | PASS, the wallet decision is the owner's" if verdict.get("pass") else f" | not passing: {failing or 'no data'}"))
    return lines
