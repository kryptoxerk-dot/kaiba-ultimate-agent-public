"""On-chain cashflow beside the book figure for closed LIVE Solana positions (SOL-LIVE-CASHFLOW-CLAUDE-20261104).

WHY. ``positions.realized_native`` (the books) leaves out the GMGN router fee and the network fee on
sol: books minus the legs' summed wallet deltas equals router + network within 0.0005 SOL on 96 of 98
decoded sm-trenches positions (Hermes journal 8495, verified in 3795861). The owner's daily report
shows only the books, so the live lane looked about 2 pp a round trip better than the chain says.
This module puts the chain beside the books. It changes no book number, no limit and no ledger.

WHAT COUNTS. Every order linked to the position (``position_orders``) must be known and terminal:
a missing order or one still unresolved (anything but filled or a terminal non-fill) makes the whole
position UNKNOWN; terminal non-fills (failed, cancelled, ...) are disclosed and are not legs. Every
filled leg must then bind, as ``kaiba.learning.loss_cost_evidence`` binds a decoder row:

* order: live, sol, the position's token and lane, side == the link's side (buy or sell);
* ``fill_costs`` row: same order_id, chain, token, side and lane as the order; its ``signature``
  equals the order's ``tx_hash`` (the decoder fetched it at ``commitment: finalized`` and refuses a
  transaction with ``meta.err``); basis ``venue_event`` or ``venue_event_unpriced``;
  ``detail_json.wallet_bound`` true and ``native_unit`` exactly ``"lamport"`` (the decoder's own
  string); integer ``wallet_native_delta`` and ``wallet_token_delta`` with the token sign matching
  the side (buy > 0, sell < 0).

A covered position also has a buy and a sell, no transaction shared with any other linked order in
the searched scope (``orders.tx_hash`` over every order linked to a closed live sol position in the
window, whether or not it has a decoder row), and its token deltas sum to exactly zero. A bound
position whose token deltas do not close is reported as PARTIAL INVENTORY, never as a closed return.

WHAT THE SUM IS. ``wallet_native_delta`` is the fee payer's own lamports plus every token account it
owns, so router, network and priority fees are inside it and it is summed ONCE per leg; nothing is
added. Rent locked in a token account the wallet still owns stays the wallet's in this sum (it cancels
within the wallet); rent, tax and each fee component are NOT measured separately here.

WHAT IS COMPARED. Only the same covered positions, each figure on its own basis and labelled:
book % = realized_native / cost_native; chain % = summed wallet delta / the buy legs' wallet outflow.
Different denominators, so the gap between the percents is never presented as a fee. The native sums
(lamports, same positions, same unit) are shown side by side. Nothing not covered is extrapolated, and
an empty covered cohort has NO net figure (n/a): a sum over zero positions is not a zero P&L.

WHAT IS IN NEITHER FIGURE, DISCLOSED. Live sol orders created in the window that ended failed or
cancelled (a failure that landed paid a network fee; the decoder refuses ``meta.err``, so it is not
measured), and live sol orders linked to no position at all (counted by state and by whether a bound
decoder row exists; never summed, since nothing here rules out two of them sharing one transaction, so
their cashflow is reported as unknown). See ``scope`` in the output.

READS. AGENTS.md database read rule: every query is one chunk on its own fresh ``mode=ro`` +
``query_only`` connection with a deadline and row cap, closed before the next
(:class:`kaiba.learning.autolearn_io.GuardedReader`), and before each one the WAL is admitted: over
1.5 GB waits 60 s and re-checks, three checks, then the section defers. Keyed or indexed reads only.

    python -m kaiba.ops.live_cashflow [--db PATH] [--days 7] [--days 365] [--json]

prints aggregates only (counts, percents, native sums): no position id, order id or signature.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics as st
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

VERSION = "sol-live-cashflow/3"
DECODED = ("venue_event", "venue_event_unpriced")
TERMINAL_NONFILL = ("failed", "cancelled", "canceled", "refused", "rejected", "expired")
NATIVE_UNIT = "lamport"  # fill_costs._cost_blank("sol", "lamport")
LAMPORTS = 1_000_000_000
CHUNK = 400
WAL_WAIT_S = 60.0
WAL_CHECKS = 3
QUERY_SECONDS = 20.0
_INT = re.compile(r"0|-?[1-9][0-9]{0,77}")


def _int(x: Any) -> int | None:
    """An exact integer from an int (not bool) or a canonical decimal string; None otherwise."""
    if type(x) is int:
        return x
    if isinstance(x, str) and _INT.fullmatch(x):
        return int(x)
    return None


def _detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 65_536:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cost_problem(order: Mapping, fc: Mapping | None) -> str | None:
    """None when the order's decoder row binds to it (identity, signature, wallet, unit, signed deltas)."""
    if fc is None:
        return "no_fill_costs_row"
    if any(fc.get(k) != order.get(k) for k in ("order_id", "chain", "token", "side", "lane")):
        return "decoder_order_identity_mismatch"
    if fc.get("basis") not in DECODED:
        return f"leg_basis:{fc.get('basis')}"
    sig = order.get("tx_hash")
    if not isinstance(sig, str) or not sig or fc.get("signature") != sig:
        return "signature_unbound"
    detail = _detail(fc.get("detail_json"))
    if detail.get("wallet_bound") is not True:
        return "wallet_unbound"
    if detail.get("native_unit") != NATIVE_UNIT:
        return "native_unit_mismatch"
    if _int(fc.get("wallet_native_delta")) is None:
        return "wallet_native_delta_unparseable"
    tokens = _int(fc.get("wallet_token_delta"))
    if tokens is None:
        return "wallet_token_delta_unparseable"
    side = order.get("side")
    if side not in ("buy", "sell") or (side == "buy" and tokens <= 0) or (side == "sell" and tokens >= 0):
        return "token_delta_sign_contradicts_side"
    return None


def _leg_problem(position: Mapping, link: Mapping, order: Mapping, fc: Mapping | None) -> str | None:
    """None when a FILLED leg is bound; otherwise the first reason it is not."""
    if order.get("mode") != "live" or order.get("chain") != "sol":
        return "order_not_live_sol"
    if order.get("token") != position.get("token") or order.get("lane") != position.get("lane"):
        return "order_position_identity_mismatch"
    if link.get("side") not in ("buy", "sell") or order.get("side") != link.get("side"):
        return "side_mismatch"
    return _cost_problem(order, fc)


def _summary(pcts: list[float]) -> dict[str, Any]:
    if not pcts:
        return {"n": 0, "mean_pct": None, "median_pct": None, "wins": 0}
    return {"n": len(pcts), "mean_pct": round(st.fmean(pcts), 4), "median_pct": round(st.median(pcts), 4),
            "wins": sum(1 for p in pcts if p > 0)}


def assess(positions: Iterable[Mapping], links: Mapping[str, list[Mapping]], orders: Mapping[str, Mapping],
           fill_costs: Mapping[str, Mapping]) -> dict[str, Any]:
    """Pure: the covered cohort and every exclusion, from already-read rows (no I/O)."""
    positions = list(positions)
    why: Counter = Counter()
    leg_why: Counter = Counter()
    linked = [orders.get(lk["order_id"]) or {} for p in positions for lk in links.get(p["position_id"], [])]
    failed_linked = [o for o in linked if o.get("state") in TERMINAL_NONFILL]
    # Duplicate scope: orders.tx_hash of EVERY order linked to a position in this population, decoded or
    # not, failed or not. A transaction used by two links cannot be split between them.
    tx_uses = Counter(o.get("tx_hash") for o in linked)
    tx_uses.pop(None, None)
    tx_uses.pop("", None)
    covered: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for p in positions:
        plinks = links.get(p["position_id"], [])
        if not plinks:
            why["no_linked_order"] += 1
            continue
        if len({lk["order_id"] for lk in plinks}) != len(plinks):
            why["duplicate_order_link"] += 1
            continue
        missing = [lk for lk in plinks if lk["order_id"] not in orders]
        if missing:
            why["unknown:linked_order_missing"] += 1
            continue
        states = [orders[lk["order_id"]].get("state") for lk in plinks]
        if any(s != "filled" and s not in TERMINAL_NONFILL for s in states):
            why["unknown:unresolved_linked_order"] += 1
            continue
        filled = [lk for lk in plinks if orders[lk["order_id"]].get("state") == "filled"]
        if not filled:
            why["no_filled_linked_order"] += 1
            continue
        problems = [_leg_problem(p, lk, orders[lk["order_id"]], fill_costs.get(lk["order_id"])) for lk in filled]
        bad = [x for x in problems if x]
        if bad:
            leg_why.update(bad)
            why["incomplete_legs"] += 1
            continue
        if {lk["side"] for lk in filled} != {"buy", "sell"}:
            why["missing_buy_or_sell"] += 1
            continue
        if any(tx_uses[orders[lk["order_id"]]["tx_hash"]] > 1 for lk in plinks
               if orders[lk["order_id"]].get("tx_hash")):
            why["shared_transaction"] += 1
            continue
        rows = [(lk["side"], fill_costs[lk["order_id"]]) for lk in filled]
        chain_net = sum(_int(fc["wallet_native_delta"]) for _, fc in rows)
        outflow = -sum(_int(fc["wallet_native_delta"]) for side, fc in rows if side == "buy")
        token_net = sum(_int(fc["wallet_token_delta"]) for _, fc in rows)
        cost, books = _int(p.get("cost_native")), _int(p.get("realized_native"))
        if cost is None or cost <= 0 or books is None or outflow <= 0:
            why["book_or_chain_basis_unusable"] += 1
            continue
        rec = {"position_id": p["position_id"], "lane": p.get("lane"), "books_native": books, "cost_native": cost,
               "chain_net_native": chain_net, "chain_outflow_native": outflow, "token_net": token_net,
               "books_pct": books * 100 / cost, "chain_pct": chain_net * 100 / outflow}
        if token_net != 0:
            partial.append(rec)  # bound, but inventory did not close: not a closed economic return
            why["partial_inventory"] += 1
            continue
        covered.append(rec)
    # A sum over zero positions is not evidence of a zero P&L: no net figure for an empty cohort.
    books_sum = sum(c["books_native"] for c in covered) if covered else None
    chain_sum = sum(c["chain_net_native"] for c in covered) if covered else None
    return {
        "version": VERSION, "closed_positions": len(positions), "covered": len(covered),
        "not_covered": dict(sorted(why.items())), "leg_problems": dict(sorted(leg_why.items())),
        "terminal_nonfill_orders_linked": len(failed_linked),
        "terminal_nonfill_linked_with_tx_hash": sum(1 for o in failed_linked if o.get("tx_hash")),
        "duplicate_scope": "orders.tx_hash of every order linked to a closed live sol position in the window",
        "books_same_cohort": {**_summary([c["books_pct"] for c in covered]), "basis": "realized_native / cost_native",
                              "net_native": books_sum},
        "chain_same_cohort": {**_summary([c["chain_pct"] for c in covered]),
                              "basis": "sum of fill_costs.wallet_native_delta / buy legs' wallet outflow",
                              "net_native": chain_sum},
        "native_difference_same_positions": None if books_sum is None else books_sum - chain_sum,
        "partial_inventory": {"n": len(partial),
                              "chain_net_native": sum(c["chain_net_native"] for c in partial) if partial else None,
                              "note": "bound positions whose token deltas do not sum to zero; not closed returns"},
        "by_lane": {lane: {"n": len(rows), "books_net_native": sum(r["books_native"] for r in rows),
                           "chain_net_native": sum(r["chain_net_native"] for r in rows)}
                    for lane in sorted({c["lane"] for c in covered})
                    for rows in [[c for c in covered if c["lane"] == lane]]},
        "notes": ["Same covered positions on both sides; each percent on its own denominator, so the gap "
                  "between the percents is NOT a fee measurement.",
                  "wallet_native_delta includes router, network and priority fees and is summed once per leg; "
                  "rent kept in the wallet's own token accounts cancels within the wallet; rent, tax and each fee "
                  "component are not measured separately here.",
                  "Positions not covered are counted by reason; nothing is extrapolated to them. An empty covered "
                  "cohort has no net figure."],
    }


def disclose(window_orders: Iterable[Mapping], linked_ids: set[str],
             fill_costs: Mapping[str, Mapping]) -> dict[str, Any]:
    """Pure: live sol orders created in the window whose cashflow is in neither figure."""
    window_orders = list(window_orders)
    failed = [o for o in window_orders if o.get("state") in TERMINAL_NONFILL]
    unlinked = [o for o in window_orders if o["order_id"] not in linked_ids]
    unlinked_filled = [o for o in unlinked if o.get("state") == "filled"]
    bound = [o for o in unlinked_filled if _cost_problem(o, fill_costs.get(o["order_id"])) is None]
    return {
        "orders_in_window": len(window_orders),
        "failed": {"n": len(failed), "with_tx_hash": sum(1 for o in failed if o.get("tx_hash")),
                   "linked_to_a_position": sum(1 for o in failed if o["order_id"] in linked_ids),
                   "note": "a failure that landed paid a network fee; the decoder refuses meta.err, so it is "
                           "not measured and is in neither figure"},
        "unlinked": {"n": len(unlinked), "by_state": dict(sorted(Counter(str(o.get("state")) for o in unlinked).items())),
                     "filled": len(unlinked_filled), "filled_decoded_bound": len(bound),
                     # Counts only: two unlinked orders can carry one transaction, and nothing here proves
                     # they do not, so a sum could show the same money twice.
                     "cashflow_native": "unknown",
                     "note": "live sol orders linked to no position; counted, never summed; their cashflow is in "
                             "neither figure"},
    }


# --------------------------------------------------------------------------------------
# bounded reads: one admitted, fresh, short read-only connection per chunk
# --------------------------------------------------------------------------------------

def _db_path(conn: Any, db_path: Path | str | None) -> Path:
    if db_path is not None:
        return Path(db_path)
    if conn is not None:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return Path(row[2])
    raise ValueError("live_cashflow reads a database file (no file behind this connection)")


def _admit(reader: Any, sleep: Callable[[float], None]) -> None:
    """AGENTS.md read rule 2 before every chunk: WAL over 1.5 GB waits 60 s and re-checks; then defers."""
    from kaiba.learning.autolearn_io import SourceDeferred

    for check in range(WAL_CHECKS):
        try:
            reader.guard()
            return
        except SourceDeferred:
            if check + 1 == WAL_CHECKS:
                raise
            sleep(WAL_WAIT_S)


def _chunks(keys: list[str]) -> Iterable[list[str]]:
    for i in range(0, len(keys), CHUNK):
        yield keys[i:i + CHUNK]


def report(conn: Any = None, *, days: int = 7, chain: str = "sol", now: int | None = None,
           db_path: Path | str | None = None, reader: Any = None,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Closed LIVE sol positions in the window, read by key or index range; read-only.

    ``conn`` only names the database file; no query runs on it. Every chunk is admitted against the WAL
    and read on its own short connection (``reader``, a GuardedReader by default)."""
    if chain != "sol":
        return {"version": VERSION, "status": "not_applicable", "chain": chain,
                "reason": "the on-chain wallet-delta reconciliation is sol-only"}
    days = max(1, min(int(days), 365))
    until = int(now if now is not None else time.time() * 1000)
    since = until - days * 86_400_000
    if reader is None:
        from kaiba.learning.autolearn_io import GuardedReader

        reader = GuardedReader(_db_path(conn, db_path), query_seconds=QUERY_SECONDS)

    def rows(sql: str, args: tuple) -> list[dict]:
        _admit(reader, sleep)
        return reader.rows(sql, args)

    positions = rows("SELECT position_id, token, lane, cost_native, realized_native FROM positions "
                     "WHERE chain='sol' AND mode='live' AND closed_ms >= ? AND closed_ms < ?", (since, until))
    links: dict[str, list[dict]] = {}
    for part in _chunks([p["position_id"] for p in positions]):
        marks = ",".join("?" * len(part))
        for r in rows(f"SELECT position_id, order_id, side FROM position_orders WHERE position_id IN ({marks})",
                      tuple(part)):
            links.setdefault(r["position_id"], []).append(r)
    orders: dict[str, dict] = {}
    fcs: dict[str, dict] = {}
    fc_cols = "order_id, chain, token, lane, side, signature, basis, wallet_native_delta, wallet_token_delta, detail_json"
    for part in _chunks(sorted({lk["order_id"] for ls in links.values() for lk in ls})):
        marks = ",".join("?" * len(part))
        for r in rows(f"SELECT order_id, chain, token, lane, side, mode, state, tx_hash FROM orders "
                      f"WHERE order_id IN ({marks})", tuple(part)):
            orders[r["order_id"]] = r
        for r in rows(f"SELECT {fc_cols} FROM fill_costs WHERE order_id IN ({marks})", tuple(part)):
            fcs[r["order_id"]] = r
    out = assess(positions, links, orders, fcs)

    # Disclosure: live sol orders created in the window, failed or linked to no position (idx_orders_token).
    window = rows("SELECT order_id, chain, token, lane, side, mode, state, tx_hash FROM orders "
                  "WHERE chain='sol' AND mode='live' AND created_ms >= ? AND created_ms < ?", (since, until))
    linked_ids: set[str] = set()
    for part in _chunks(sorted({o["order_id"] for o in window})):
        marks = ",".join("?" * len(part))
        linked_ids.update(r["order_id"] for r in rows(
            f"SELECT DISTINCT order_id FROM position_orders WHERE order_id IN ({marks})", tuple(part)))
    unlinked_filled = sorted(o["order_id"] for o in window if o["order_id"] not in linked_ids
                             and o.get("state") == "filled")
    window_fcs: dict[str, dict] = {}
    for part in _chunks(unlinked_filled):
        marks = ",".join("?" * len(part))
        for r in rows(f"SELECT {fc_cols} FROM fill_costs WHERE order_id IN ({marks})", tuple(part)):
            window_fcs[r["order_id"]] = r
    out["failed_and_unlinked"] = disclose(window, linked_ids, window_fcs)

    receipts = getattr(reader, "receipts", [])
    out.update({
        "chain": "sol", "days": days, "since_ms": since, "until_ms": until,
        "source": "positions, position_orders, orders, fill_costs (keyed or index-range reads; read-only)",
        "scope": {
            "population": "positions chain=sol mode=live with closed_ms in [since_ms, until_ms)",
            "both_figures": "the covered positions only",
            "chain_figure": "each filled linked leg's wallet_native_delta, summed once (router, network and "
                            "priority fees inside; rent in the wallet's own token accounts cancels)",
            "in_neither_figure": ["positions not covered (counted by reason)", "open positions",
                                  "network fees of failed transactions that landed (not measured)",
                                  "cashflow of live orders linked to no position (counted only; unknown)",
                                  "every other chain"],
            "disclosure_window": "orders chain=sol mode=live with created_ms in [since_ms, until_ms)",
        },
        "reads": {"queries": getattr(reader, "queries", None), "wal_waits": getattr(reader, "wal_waits", None),
                  "max_query_s": max((r["duration_s"] for r in receipts), default=None),
                  "max_wal_bytes": max((r["wal_bytes"] for r in receipts), default=None),
                  "rule": "each chunk: WAL admitted (<=1.5 GB; 60 s re-check), own mode=ro query_only "
                          "connection with a deadline, closed before the next"},
    })
    return out


def _sol(x: int | None) -> str:
    return "n/a" if x is None else f"{x / LAMPORTS:+.4f}"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def render_lines(label: str, rep: Mapping[str, Any]) -> list[str]:
    """Daily-report lines: the same covered positions, books and chain side by side; then what neither has."""
    if rep.get("status") == "not_applicable":
        return []
    if "error" in rep:
        return [f"  {label} on-chain: {rep['error']}"]
    b, c = rep["books_same_cohort"], rep["chain_same_cohort"]
    missing = rep["closed_positions"] - rep["covered"]
    lines = [f"  {label} on-chain, same {rep['covered']} of {rep['closed_positions']} closed: "
             f"books mean {_pct(b['mean_pct'])} median {_pct(b['median_pct'])} net {_sol(b['net_native'])} SOL | "
             f"chain mean {_pct(c['mean_pct'])} median {_pct(c['median_pct'])} net {_sol(c['net_native'])} SOL | "
             f"not covered {missing}"]
    fu = rep.get("failed_and_unlinked")
    if fu:
        top = sorted(rep["not_covered"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        reasons = f" ({', '.join(f'{k} {v}' for k, v in top)})" if top else ""
        f, u = fu["failed"], fu["unlinked"]
        lines.append(f"  {label} in neither figure: not covered {missing}{reasons} | failed live orders {f['n']} "
                     f"({f['with_tx_hash']} with a tx hash; any network fee they paid is not measured) | unlinked "
                     f"live orders {u['n']} (filled {u['filled']}, decoded {u['filled_decoded_bound']}; "
                     f"cashflow unknown, not summed)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kaiba.ops.live_cashflow",
                                     description="Closed LIVE sol positions: books beside on-chain cashflow (read-only)")
    parser.add_argument("--db", type=Path, default=None, help="database path (default: settings db_path)")
    parser.add_argument("--days", type=int, action="append", help="window in days, 1-365 (default 7); repeatable")
    parser.add_argument("--json", action="store_true", help="print the aggregates as JSON")
    args = parser.parse_args(argv)
    if args.db is None:
        from kaiba.core.config import get_settings

        args.db = get_settings().db_path
    now = int(time.time() * 1000)
    out: dict[str, Any] = {}
    for days in args.days or [7]:
        try:
            out[f"{days}d"] = report(db_path=args.db, days=days, now=now)
        except Exception as exc:  # noqa: BLE001 - reported as data, like the daily report's sections
            out[f"{days}d"] = {"error": f"cannot measure: {type(exc).__name__}: {exc}"[:240]}
    if args.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        for key, rep in out.items():
            print("\n".join(render_lines(key, rep)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
