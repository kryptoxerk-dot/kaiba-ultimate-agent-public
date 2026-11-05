"""No module may start drawing on protection's limiter families without saying so here.

Why (2026-10-23). Two build agents shared a scratch ``mutate.py``; one ran the other's mutant and
the restore failed (OSError 22), leaving ``BSC_PROVIDER = "rpc"`` in
``kaiba/execution/evm_pool_price.py`` where ``"bsc-pool-rpc"`` belongs. ``rpc`` is the key
protection's sol and bsc stops share (``evm_price.json_rpc_batch``), so shipping it would have put
every BSC paper pool read on the stop-loss watchdog's budget -- the shape of the 09-22 outage where
scanner workers blinded live stops through a shared bucket. It was caught by eye before commit.
Nothing would have caught it mechanically: the module's own tests pass with either value.

What this pins. The families are read from protection's own readers, not typed here
(:func:`protection_families`). Every use of one in ``kaiba/`` -- a string literal outside a
docstring, or a reference to another module's constant that holds one -- is counted per module,
and the count must equal :data:`ALLOWED` exactly. A new use fails with its scopes and lines; a
removed use fails too, so the list cannot rot into a blanket pass. ``config/schedule.yaml`` may not
route a job onto a family at all (no value there names one today).

Scope, stated: the on-chain RPC buckets only. The vendor families protection also reads
(``dexscreener`` for robinhood marks, ``jupiter``, ``gmgn``) are shared with discovery by design
and governed by the limiter's EXIT priority; listing every discovery module that reads them would
make this a census, not a guard.

MEASURED 2026-10-27, the tree as it stands: 15 modules use a family. Six are protection's readers or
the limiter itself; five use the name only as a label or to READ protection's pressure and back off;
dyor_verify, lookalike, transaction_receipts, fill_costs and the pons poller (``ingest.robinhood``),
plus launch-snipe's own reads, really do draw on protection's buckets at RESEARCH / DISCOVERY
priority. Those are frozen as found, not endorsed: moving them to their own keys is board work.
"""

from __future__ import annotations

import ast
import collections
import inspect
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "kaiba"

#: module -> {family: exact number of uses}, with the reason each is allowed. Edit deliberately:
#: an addition here is a decision to put that module on the stop-loss watchdog's budget.
ALLOWED: dict[str, dict[str, int]] = {
    # protection's own on-chain readers, and the limiter that defines the families
    "kaiba.execution.pumpswap_price": {"rpc": 1},       # RPC_BUCKET: sol pool + curve reads (stops, paper)
    "kaiba.execution.evm_price": {"rpc": 1},            # json_rpc_batch default: sol + bsc stop reads
    "kaiba.execution.onchain_pool": {"robinhood-rpc": 1},  # PROVIDER: robinhood pool reads (stops)
    "kaiba.execution.fills": {"rpc": 1},                # RPC_PROVIDER: live-fill reconciliation
    "kaiba.core.limiter": {"rpc": 1},                   # DEFAULTS entry that defines the bucket
    # live-lane reads that are allowed to share by design (protection's EXIT reads go first)
    "kaiba.execution.snipe": {"robinhood-rpc": 4, "rpc": 1},  # RPC_BUCKET default + params; pump curve read
    "kaiba.execution.launch_preflight": {"rpc": 1},     # read_pump_global, once per launch-snipe entry
    # names used as labels, or to READ protection's pressure and stay off its bucket
    "kaiba.cli.probe": {"rpc": 1},                      # operator-run probe, not a service
    "kaiba.hunters.watch_wallets": {"rpc": 3},          # Reading source label; dials its own public URLs
    "kaiba.ingest.flap": {"rpc": 1},                    # PRESSURE_PROVIDER: backs off while protection is busy
    # research / discovery consumers of protection's buckets, frozen as found 2026-10-27 (board)
    "kaiba.ingest.robinhood": {"robinhood-rpc": 1},     # pons poller, 1 batch/s on the robinhood family
    "kaiba.intelligence.dyor_verify": {"rpc": 9},       # 3 RESEARCH reads + 6 receipt labels
    "kaiba.intelligence.lookalike": {"rpc": 2, "robinhood-rpc": 1},  # cache-dir name x2; research.getLogs (Codex)
    "kaiba.learning.transaction_receipts": {"rpc": 9},  # paced reads + receipt validation (Codex)
    "kaiba.execution.fill_costs": {"rpc": 1},           # live-fill cost witness at RESEARCH (Codex)
}


def protection_families() -> frozenset[str]:
    """The limiter keys protection's on-chain stop reads go through, read from those readers."""
    from kaiba.execution import evm_price, onchain_pool, pumpswap_price

    stop_key = inspect.signature(evm_price.json_rpc_batch).parameters["provider"].default
    return frozenset({pumpswap_price.RPC_BUCKET, stop_key, onchain_pool.PROVIDER})


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _sources() -> dict[str, str]:
    return {_module_name(p): p.read_text(encoding="utf-8") for p in sorted(PACKAGE.rglob("*.py"))}


def family_constants(sources: dict[str, str], families: frozenset[str]) -> dict[tuple[str, str], str]:
    """``(module, NAME) -> family`` for every module-level ``NAME = "<family>"``: the constants a
    module can borrow instead of spelling the family out."""
    out: dict[tuple[str, str], str] = {}
    for mod, src in sources.items():
        for node in ast.parse(src).body:
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if not (isinstance(value, ast.Constant) and value.value in families):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            out.update({(mod, t.id): value.value for t in targets if isinstance(t, ast.Name)})
    return out


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def family_uses(src: str, module: str, families: frozenset[str],
                consts: dict[tuple[str, str], str]) -> list[tuple[str, str, int, str]]:
    """``(family, scope, line, how)`` for every use of a family in ``src``. Docstrings are not uses."""
    tree = ast.parse(src)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    aliases: dict[str, str] = {}  # local name -> what it stands for (a module, or module.NAME)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                aliases[a.asname or a.name] = f"{n.module}.{a.name}"

    def borrowed(dotted: str) -> str | None:
        head, _, rest = dotted.partition(".")
        full = aliases.get(head, head) + ("." + rest if rest else "")
        mod, _, name = full.rpartition(".")
        return consts.get((mod, name)) if mod and mod != module else None

    uses: list[tuple[str, str, int, str]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            elif node is tree and isinstance(child, (ast.Assign, ast.AnnAssign)):
                target = child.targets[0] if isinstance(child, ast.Assign) else child.target
                inner = target.id if isinstance(target, ast.Name) else scope
            where = inner or "<module>"
            if isinstance(child, ast.Constant) and isinstance(child.value, str) \
                    and child.value in families and id(child) not in docstrings:
                uses.append((child.value, where, child.lineno, "literal"))
            elif isinstance(child, ast.Attribute) or (isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)):
                dotted = _dotted(child)
                family = borrowed(dotted) if dotted else None
                if family:
                    uses.append((family, where, child.lineno, f"borrows {dotted}"))
            visit(child, inner)

    visit(tree, "")
    return uses


def tree_uses() -> dict[str, list[tuple[str, str, int, str]]]:
    families = protection_families()
    sources = _sources()
    consts = family_constants(sources, families)
    out = {}
    for mod, src in sources.items():
        found = family_uses(src, mod, families, consts)
        if found:
            out[mod] = found
    return out


# --------------------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------------------


def test_the_families_are_protections_own_keys():
    """If protection moves to its own keys this set moves with it, and every ALLOWED entry turns
    stale -- which is the point: the ratchet must then be rebuilt against the new keys."""
    assert protection_families() == {"rpc", "robinhood-rpc"}


def stale_candidates(allowed: dict[str, dict[str, int]], present: set[str]) -> set[str]:
    """ALLOWED entries whose module exists. A module that is not in the tree draws on nothing, so
    its entry is not stale -- MEASURED 2026-10-27: the box runs without ``intelligence.lookalike``
    (one of 12 repo modules not deployed there), and every other module matched these counts."""
    return {mod for mod in allowed if mod in present}


def test_no_module_draws_on_a_protection_family_beyond_the_allowlist():
    found = tree_uses()
    counts = {mod: dict(collections.Counter(f for f, *_ in uses)) for mod, uses in found.items()}
    problems = []
    for mod in sorted(set(counts) | stale_candidates(ALLOWED, set(_sources()))):
        have, allowed = counts.get(mod, {}), ALLOWED.get(mod, {})
        if have == allowed:
            continue
        detail = "; ".join(f"{f} {how} in {scope} (line {line})" for f, scope, line, how in found.get(mod, []))
        problems.append(f"{mod}: uses {have or 'none'}, ALLOWED {allowed or 'none'} -- {detail or 'no uses left'}")
    assert not problems, (
        "a module's use of protection's limiter families changed. A NEW use puts that module on the "
        "stop-loss watchdog's budget: give it its own limiter key, or add it to ALLOWED with the reason. "
        "A REMOVED use: lower ALLOWED.\n  " + "\n  ".join(problems))


def test_the_schedule_routes_no_job_onto_a_protection_family():
    import yaml

    families = protection_families()
    raw = yaml.safe_load((ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")) or {}
    hits: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        elif isinstance(value, str) and value in families:
            hits.append(f"{path} = {value!r}")

    walk(raw, "schedule")
    assert not hits, f"a job is configured onto protection's limiter budget: {hits}"


# --------------------------------------------------------------------------------------
# the scanner is only a guard if it sees each way a family can arrive
# --------------------------------------------------------------------------------------

FAMILIES = frozenset({"rpc", "robinhood-rpc"})
CONSTS = {("kaiba.execution.pumpswap_price", "RPC_BUCKET"): "rpc",
          ("kaiba.ingest.robinhood", "PROVIDER"): "robinhood-rpc"}


@pytest.mark.parametrize("src, family", [
    ('BSC_PROVIDER = "rpc"\n', "rpc"),  # the 2026-10-23 leftover, verbatim
    ('def read():\n    return post_json("robinhood-rpc", "x", "u")\n', "robinhood-rpc"),
    ('from kaiba.execution.pumpswap_price import RPC_BUCKET\nP = RPC_BUCKET\n', "rpc"),
    ('from kaiba.execution import pumpswap_price as ps\ndef f():\n    return ps.RPC_BUCKET\n', "rpc"),
    ('import kaiba.ingest.robinhood\nP = kaiba.ingest.robinhood.PROVIDER\n', "robinhood-rpc"),
    ('PARAMS = {"buckets": {"sol": "rpc"}}\n', "rpc"),
])
def test_the_scanner_sees_every_route_in(src, family):
    found = family_uses(src, "kaiba.research.example", FAMILIES, CONSTS)
    assert [f for f, *_ in found] == [family], found


def test_the_scanner_ignores_docstrings_and_lookalikes():
    src = ('"""Reads go through the rpc bucket."""\n'
           'def f():\n    """rpc"""\n    return "rpc.getTransaction", "bsc-pool-rpc", "sol-feed-rpc"\n')
    assert family_uses(src, "kaiba.research.example", FAMILIES, CONSTS) == []


def test_a_missing_module_is_not_stale_but_an_existing_one_that_changed_is():
    allowed = {"kaiba.a": {"rpc": 1}, "kaiba.gone": {"rpc": 2}}
    assert stale_candidates(allowed, {"kaiba.a", "kaiba.b"}) == {"kaiba.a"}


def test_the_10_23_leftover_would_have_failed_the_guard():
    """Replays the incident against the real tree: evm_pool_price with BSC_PROVIDER = "rpc"."""
    path = PACKAGE / "execution" / "evm_pool_price.py"
    src = path.read_text(encoding="utf-8")
    assert 'BSC_PROVIDER = "bsc-pool-rpc"' in src, "the module this test replays has changed; update it"
    families = protection_families()
    consts = family_constants(_sources(), families)
    clean = family_uses(src, "kaiba.execution.evm_pool_price", families, consts)
    mutant = family_uses(src.replace('BSC_PROVIDER = "bsc-pool-rpc"', 'BSC_PROVIDER = "rpc"', 1),
                         "kaiba.execution.evm_pool_price", families, consts)
    assert clean == [] and "kaiba.execution.evm_pool_price" not in ALLOWED
    assert [(f, scope) for f, scope, *_ in mutant] == [("rpc", "BSC_PROVIDER")]
