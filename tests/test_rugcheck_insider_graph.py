"""RugCheck's insider graph, read from LIVE bodies, and the cluster rules it feeds.

Until 2026-10-21 ``rugcheck.normalize_insider_graph`` read ``size`` and ``tokenAmount`` off
each network. Those are the keys of the REPORT's ``insiderNetworks`` rows; ``/insiders/graph``
sends ``nodes[].holdings``. Every network was skipped, the parser returned a literal
``cluster_pct = 0`` for every token, coverage counted that 0 as an established reading, and
dyor's 30% reject and 20% review could never fire. The only test of it used a hand-built
fixture in the wrong shape, so it passed for as long as the bug existed.

Everything here replays real bodies from ``tests/fixtures/rugcheck/live/`` (captured with the
keyless public API, 2026-10-20/21 UTC) and from the September recordings in
``tests/fixtures/dyor_live/``. The load-bearing ones:

* ``test_live_graphs_are_read_not_zeroed`` -- the defect itself: on the old parser every
  value asserted here was 0.
* ``test_an_empty_graph_is_unknown_not_zero`` -- what the scanner mostly sees.
* ``test_a_sum_above_supply_is_unknown_not_a_clamped_reject`` -- the old clamp turned a
  malformed body into a certain reject.
* ``test_the_cluster_rules_are_report_only_by_default`` -- a gate that never fired must not
  start refusing trades as a side effect of the parser fix.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis, Grade, Receipt, TokenRisk
from kaiba.intelligence import coverage, dyor
from kaiba.providers import rugcheck
from kaiba.providers._http import Fetched

LIVE = Path(__file__).parent / "fixtures" / "rugcheck" / "live"
DYOR_LIVE = Path(__file__).parent / "fixtures" / "dyor_live"

TEN_NETWORKS = "graph_ten_networks_sum_over_30.json"
SHARED_WALLETS = "graph_seven_networks_shared_wallets.json"
REPORT_OVER_100 = "graph_two_networks_report_over_100.json"
FRESH_MINT = "graph_fresh_mint_empty.json"


def live(name: str) -> dict[str, Any]:
    return json.loads((LIVE / name).read_text(encoding="utf-8"))


def supply_of(capture: dict[str, Any]) -> Decimal:
    supply = rugcheck.normalize_report(capture["report"]).get("total_supply")
    assert supply is not None and supply > 0, "every live capture carries token.supply"
    return supply


def pct3(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(Decimal("0.001"))


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    """No test here may depend on whatever the developer's shell has armed."""
    monkeypatch.delenv(dyor.CLUSTER_GATE_ENV, raising=False)


# --------------------------------------------------------------------------------------
# the parser, against live bodies
# --------------------------------------------------------------------------------------


def test_the_live_graph_has_no_key_the_old_parser_read():
    """The shape, pinned: nodes[].holdings, and none of size / tokenAmount anywhere."""
    for name in (TEN_NETWORKS, SHARED_WALLETS, REPORT_OVER_100):
        graph = live(name)["graph"]
        assert isinstance(graph, list) and graph, name
        for network in graph:
            assert "size" not in network and "tokenAmount" not in network, name
            assert isinstance(network["nodes"], list) and network["nodes"], name
            for node in network["nodes"]:
                assert set(node) >= {"id", "holdings"}, name


@pytest.mark.parametrize(
    ("name", "cluster", "largest", "networks", "wallets"),
    [
        # 10 networks of 12-68 wallets; the summed share crosses the 30% reject line
        # while no single network is above 7.7%.
        (TEN_NETWORKS, "34.334", "7.657", 10, 308),
        # 32 node rows, 29 wallets: three wallets sit in two networks (0 in one of them).
        (SHARED_WALLETS, "19.590", "4.556", 7, 29),
        # One wallet in both networks with two NON-zero holdings: 3.716% summed per
        # network, 3.606% counting each wallet once.
        (REPORT_OVER_100, "3.606", "3.606", 2, 7),
    ],
)
def test_live_graphs_are_read_not_zeroed(name, cluster, largest, networks, wallets):
    """The defect: the old parser returned cluster_pct = 0 for every one of these."""
    capture = live(name)
    reading = rugcheck.read_insider_graph(capture["graph"], supply_of(capture))

    assert reading.why_unknown is None
    assert pct3(reading.cluster_pct) == Decimal(cluster)
    assert pct3(reading.largest_pct) == Decimal(largest)
    assert (reading.networks, reading.wallets) == (networks, wallets)
    assert pct3(rugcheck.normalize_insider_graph(capture["graph"], supply_of(capture))["cluster_pct"]) == Decimal(
        cluster
    )


def test_a_wallet_in_two_networks_is_counted_once():
    """Summing per network double-counts; the reading takes each wallet once, at its max."""
    capture = live(REPORT_OVER_100)
    supply = supply_of(capture)
    per_network = sum(
        (Decimal(node["holdings"]) for network in capture["graph"] for node in network["nodes"]), Decimal(0)
    )
    assert pct3(per_network / supply * 100) == Decimal("3.716")
    assert pct3(rugcheck.read_insider_graph(capture["graph"], supply).cluster_pct) == Decimal("3.606")


def test_graph_holdings_are_the_reports_current_holding_in_raw_atoms():
    """The unit: each network's summed holdings is the report's ``currentHolding`` exactly.

    That is what makes ``holdings / token.supply`` a share of supply rather than a guess
    about decimals. Checked on the two captures whose graph and report were fetched seconds
    apart and list the same networks.
    """
    for name in (SHARED_WALLETS, REPORT_OVER_100):
        capture = live(name)
        report_rows = {int(row["currentHolding"]) for row in capture["report"]["insiderNetworks"]}
        for network in capture["graph"]:
            held = sum(int(node["holdings"]) for node in network["nodes"])
            assert held in report_rows, (name, network["net_id"], held)


def test_the_september_recordings_carry_the_same_shape():
    """The shape is not new. The dyor_live bodies recorded in September were already in it.

    So the field mapping was wrong from the first commit, not broken by a provider change.
    Three of those recordings have networks; the old parser read 0 off all three.
    """
    read = {}
    for path in sorted(DYOR_LIVE.glob("sol-*.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        rug = body.get("rugcheck") or {}
        if rug.get("insiders") is None or not isinstance(rug.get("report"), dict):
            continue
        supply = rugcheck.normalize_report(rug["report"]).get("total_supply")
        read[path.stem] = rugcheck.read_insider_graph(rug["insiders"], supply)
    with_networks = {slug for slug, r in read.items() if r.networks}
    assert with_networks == {"sol-bonk", "sol-pumpcurve", "sol-pumpdevheavy"}
    for slug in with_networks:
        assert read[slug].cluster_pct is not None and read[slug].cluster_pct > 0, slug
    for slug in set(read) - with_networks:
        assert read[slug].cluster_pct is None, f"{slug}: an empty recorded graph must be unknown"


def test_an_empty_graph_is_unknown_not_zero():
    """A fresh pump.fun mint: [] at creation and [] again six minutes later.

    "Not analysed yet" and "analysed, nothing found" are the same body, so the honest
    reading is unknown. The old parser returned ``cluster_pct = 0`` here.
    """
    capture = live(FRESH_MINT)
    assert capture["graph"] == [] and capture["graph_refetch"] == []
    assert capture["report"]["graphInsidersDetected"] == 0

    reading = rugcheck.read_insider_graph(capture["graph"], supply_of(capture))
    assert reading.cluster_pct is None
    assert "no networks returned" in (reading.why_unknown or "")
    assert rugcheck.normalize_insider_graph(capture["graph"], supply_of(capture)) == {}


def test_the_reports_insider_rows_are_not_the_graph_shape():
    """The keys the old parser read DO exist -- on the report. Fed here they are unknown.

    And they are the dangerous rows: this capture's report lists a 2,018-wallet network
    whose tokenAmount is 215% of supply, which the old parser's ``min(..., 100)`` would have
    turned into a certain reject had it ever been pointed at them.
    """
    capture = live(REPORT_OVER_100)
    rows = capture["report"]["insiderNetworks"]
    supply = supply_of(capture)
    assert {"size", "tokenAmount", "currentHolding"} <= set(rows[0])
    assert max(Decimal(row["tokenAmount"]) for row in rows) / supply * 100 > 200

    reading = rugcheck.read_insider_graph(rows, supply)
    assert reading.cluster_pct is None
    assert "unrecognised network shape" in (reading.why_unknown or "")


def test_a_sum_above_supply_is_unknown_not_a_clamped_reject():
    """A share above 100% is a malformed body. Clamping it to 100 manufactures a reject."""
    capture = live(REPORT_OVER_100)
    graph = capture["graph"]
    held = sum(Decimal(node["holdings"]) for network in graph for node in network["nodes"])
    too_small = held / 2  # as if the supply were half of what the networks hold

    reading = rugcheck.read_insider_graph(graph, too_small)
    assert reading.cluster_pct is None
    assert "malformed" in (reading.why_unknown or "")
    assert rugcheck.normalize_insider_graph(graph, too_small) == {}


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda g: g[0].pop("nodes"), "no nodes list"),
        (lambda g: g[0]["nodes"][0].pop("holdings"), "needs id and holdings"),
        (lambda g: g[0]["nodes"][0].update(holdings=-1), "needs id and holdings"),
        (lambda g: g[0]["nodes"][0].update(holdings=True), "needs id and holdings"),
        (lambda g: g[0].update(relatedMint="So11111111111111111111111111111111111111112"), "related mint"),
    ],
    ids=["no-nodes", "no-holdings", "negative-holdings", "bool-holdings", "related-mint"],
)
def test_an_unrecognised_shape_is_unknown_never_partially_counted(mutate, why):
    """Skipping the network we cannot read would UNDERSTATE the share: the expensive way."""
    capture = live(TEN_NETWORKS)
    graph = copy.deepcopy(capture["graph"])
    mutate(graph)
    reading = rugcheck.read_insider_graph(graph, supply_of(capture))
    assert reading.cluster_pct is None
    assert why in (reading.why_unknown or "")


def test_no_supply_is_unknown():
    capture = live(TEN_NETWORKS)
    for supply in (None, Decimal(0)):
        assert rugcheck.normalize_insider_graph(capture["graph"], supply) == {}


def test_a_receipt_line_cannot_take_the_read_down():
    """``render`` runs inside ``security_properties``; a raise there loses every RugCheck field."""
    hand_built = rugcheck.InsiderReading(cluster_pct=Decimal(0))
    assert "largest_network_pct=n/a" in hand_built.render()
    assert "cluster_largest_pct" not in hand_built.props()


# --------------------------------------------------------------------------------------
# security_properties: the reading reaches the props and leaves a receipt
# --------------------------------------------------------------------------------------


def _serve(monkeypatch, capture: dict[str, Any], graph_key: str = "graph") -> None:
    """Answer RugCheck's three routes from one capture, without the HTTP stack."""

    def fetched(endpoint: str, body: Any):
        return lambda *_a, **_k: Fetched(body, Receipt(provider="rugcheck", endpoint=endpoint))

    monkeypatch.setattr(rugcheck, "report_summary", fetched("token.summary", {"risks": []}))
    monkeypatch.setattr(rugcheck, "report", fetched("token.report", capture["report"]))
    monkeypatch.setattr(rugcheck, "insider_graph", fetched("token.insiders", capture[graph_key]))


def _reading_note(receipts: list[Receipt]) -> str:
    notes = [r.note or "" for r in receipts if r.endpoint == "token.insiders.reading"]
    assert len(notes) == 1, [r.endpoint for r in receipts]
    return notes[0]


def test_security_properties_carries_the_reading_and_its_receipt(monkeypatch):
    _serve(monkeypatch, live(TEN_NETWORKS))
    props, receipts = rugcheck.security_properties("EHPugw1ghE46BeySAa7cHvoZHwYometbp1aK1Q7jpump")

    assert pct3(props["cluster_pct"]) == Decimal("34.334")
    note = _reading_note(receipts)
    assert "cluster_pct=34.334" in note and "largest_network_pct=7.657" in note
    assert "report_graphInsidersDetected=310" in note
    reading = next(r for r in receipts if r.endpoint == "token.insiders.reading")
    assert reading.basis is EvidenceBasis.DERIVED


def test_security_properties_leaves_an_empty_graph_unknown_and_says_why(monkeypatch):
    _serve(monkeypatch, live(FRESH_MINT))
    props, receipts = rugcheck.security_properties("H1dYaZErMWQWT81vC3tgrLLrNQTD7biRDoQhKLwmpump")

    assert "cluster_pct" not in props
    assert "total_supply" in props, "the report answered; only the graph was empty"
    note = _reading_note(receipts)
    assert "UNKNOWN (no networks returned" in note and "report_graphInsidersDetected=0" in note


# --------------------------------------------------------------------------------------
# dyor: report-only by default, and coverage counts unknown as missing
# --------------------------------------------------------------------------------------


def _scan_rugcheck_only(monkeypatch, capture: dict[str, Any], address: str) -> Any:
    """The dossier RugCheck alone would produce for this capture (other providers silent)."""
    _serve(monkeypatch, capture)
    claims, receipts, status = dyor.collect_rugcheck(address, Chain.SOL)
    assert status == "ok"
    resolution = dyor.resolve(claims, chain=Chain.SOL)
    resolution.receipts.extend(receipts)
    resolution.providers_ok = {"rugcheck"}
    return resolution


def _without(resolution: dyor.Resolution, prop: str) -> dyor.Resolution:
    kept = [c for claims in resolution.claims.values() for c in claims if c.prop != prop]
    out = dyor.resolve(kept, chain=Chain.SOL)
    out.receipts = list(resolution.receipts)
    out.providers_ok = set(resolution.providers_ok)
    return out


def test_the_cluster_rules_are_report_only_by_default(monkeypatch):
    """A real 34.3% reading, the switch unset: recorded, and it refuses nothing.

    The control is the same dossier with the cluster claim removed: blockers, warnings and
    the QUARANTINED-or-not outcome must be identical, because nothing may act on a gate that
    has never been measured.
    """
    address = "EHPugw1ghE46BeySAa7cHvoZHwYometbp1aK1Q7jpump"
    resolution = _scan_rugcheck_only(monkeypatch, live(TEN_NETWORKS), address)
    assert pct3(resolution.n("cluster_pct")) == Decimal("34.334")
    control = _without(resolution, "cluster_pct")  # before build_dossier derives anything

    with_cluster = dyor.build_dossier(address, Chain.SOL, resolution)
    without_cluster = dyor.build_dossier(address, Chain.SOL, control)

    assert TokenRisk.CLUSTER_CONCENTRATION not in with_cluster.blockers
    assert TokenRisk.CLUSTER_CONCENTRATION not in with_cluster.warnings
    assert with_cluster.blockers == without_cluster.blockers
    assert with_cluster.warnings == without_cluster.warnings
    assert with_cluster.tradeable is without_cluster.tradeable
    # engine.decide also refuses QUARANTINED with an empty blocker list, and the score/grade feed
    # sizing and reports: report-only must move none of them (review of 8738c58, 2026-10-21).
    assert with_cluster.grade is not Grade.QUARANTINED
    assert with_cluster.grade == without_cluster.grade
    assert with_cluster.score == without_cluster.score
    assert pct3(with_cluster.cluster_pct.value) == Decimal("34.334")

    recorded = [r for r in with_cluster.receipts if r.endpoint == "rule.report_only"]
    assert len(recorded) == 1, "the would-be review folds into the would-be reject"
    assert recorded[0].basis is EvidenceBasis.DERIVED
    note = recorded[0].note or ""
    assert note.startswith("report_only cluster_concentration: would_have_fired=blocker")
    assert "rule=cluster_concentration_reject cluster_pct=34.33 > 30" in note


def test_armed_the_same_reading_rejects(monkeypatch):
    """Positive control: the switch is the only thing standing between 34.3% and a reject."""
    monkeypatch.setenv(dyor.CLUSTER_GATE_ENV, " Enforce ")
    address = "EHPugw1ghE46BeySAa7cHvoZHwYometbp1aK1Q7jpump"
    dossier = dyor.build_dossier(address, Chain.SOL, _scan_rugcheck_only(monkeypatch, live(TEN_NETWORKS), address))

    assert TokenRisk.CLUSTER_CONCENTRATION in dossier.blockers
    assert not dossier.tradeable
    assert not [r for r in dossier.receipts if r.endpoint == "rule.report_only"]


def test_armed_the_reading_also_enters_the_score(monkeypatch):
    """Positive control for the score: only once enforced does the share lower insider_supply."""
    address = "EHPugw1ghE46BeySAa7cHvoZHwYometbp1aK1Q7jpump"
    off = dyor.build_dossier(address, Chain.SOL, _scan_rugcheck_only(monkeypatch, live(TEN_NETWORKS), address))
    monkeypatch.setenv(dyor.CLUSTER_GATE_ENV, "enforce")
    on = dyor.build_dossier(address, Chain.SOL, _scan_rugcheck_only(monkeypatch, live(TEN_NETWORKS), address))
    assert on.score is not None and off.score is not None and on.score < off.score


def test_a_would_be_review_is_recorded_not_warned(monkeypatch):
    """20-30%: report-only it is a receipt, not a warning; armed it is the mandate's review."""
    pairs = {
        "can_sell": True, "mint_authority_revoked": True, "freeze_authority_revoked": True,
        "transfer_hook": False, "balance_mutable": False, "tax_modifiable": False,
        "metadata_mutable": False, "lp_burned_pct": Decimal(100), "top10_pct": Decimal(10),
        "dev_pct": Decimal(1), "liquidity_usd": Decimal(500_000), "cluster_pct": Decimal(25),
    }
    receipt = Receipt(provider="test", endpoint="test.fixture")
    claims = [dyor.Claim(k, "test", v, receipt) for k, v in pairs.items()]

    def build() -> Any:
        resolution = dyor.resolve(claims, chain=Chain.SOL)
        resolution.providers_ok = {"test"}
        return dyor.build_dossier("X", Chain.SOL, resolution)

    unarmed = build()
    assert TokenRisk.CLUSTER_CONCENTRATION not in unarmed.warnings
    notes = [r.note or "" for r in unarmed.receipts if r.endpoint == "rule.report_only"]
    assert len(notes) == 1 and "would_have_fired=warning" in notes[0]
    assert "rule=cluster_concentration_review cluster_pct=25.00 > 20" in notes[0]

    monkeypatch.setenv(dyor.CLUSTER_GATE_ENV, dyor.CLUSTER_GATE_ENFORCE)
    assert TokenRisk.CLUSTER_CONCENTRATION in build().warnings


@pytest.mark.parametrize(
    ("raw", "armed"),
    [
        (None, False), ("", False), ("report_only", False), (" REPORT_ONLY ", False),
        ("enforce", True), (" Enforce ", True),
        # An unreadable switch leaves an unmeasured veto OFF, and says so in the log.
        ("1", False), ("true", False), ("on", False), ("enforced", False),
    ],
)
def test_the_switch_arms_only_on_the_exact_word(monkeypatch, raw, armed):
    if raw is None:
        monkeypatch.delenv(dyor.CLUSTER_GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(dyor.CLUSTER_GATE_ENV, raw)
    assert dyor.cluster_gate_enforced() is armed


def test_coverage_counts_an_empty_graph_as_missing_and_a_live_one_as_established(monkeypatch):
    """coverage.py weighs cluster_pct at 12 points. Only a graph that answered may earn them."""
    fresh = _scan_rugcheck_only(monkeypatch, live(FRESH_MINT), "H1dYaZErMWQWT81vC3tgrLLrNQTD7biRDoQhKLwmpump")
    assert "cluster_pct" in fresh.unknown
    assert "cluster_pct" not in dyor.coverage_of(fresh).established

    read = _scan_rugcheck_only(monkeypatch, live(SHARED_WALLETS), "2qgnTgJbZU6Tu6CMrm5Z5ePDGuUAyGTACbBoBCw6pump")
    assert "cluster_pct" not in read.unknown
    assert "cluster_pct" in dyor.coverage_of(read).established
    # Exactly the field's own weight, measured on one dossier with and without the claim.
    gained = (
        dyor.coverage_of(read).established_weight
        - dyor.coverage_of(_without(read, "cluster_pct")).established_weight
    )
    assert gained == coverage.FIELD_WEIGHTS["cluster_pct"]
