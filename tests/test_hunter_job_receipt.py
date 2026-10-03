"""The hunters job's ops_runs record must be readable, not ``{"truncated": true}``.

MEASURED 2026-10-02: every hunters run on the box stored ``{"truncated":true,...,"chars":2149}``
because the three full refresh receipts exceeded ``scheduler._small``'s 2,000-char cap.
"""

from __future__ import annotations

from types import SimpleNamespace

from kaiba.hunters import airdrops, listings, nft
from kaiba.hunters.ev import OpportunityEvidence, OpportunityKind
from kaiba.ops import scheduler


def test_hunters_job_result_fits_the_ops_runs_cell(tmp_db, monkeypatch):
    opps = [OpportunityEvidence(kind=OpportunityKind.POINTS, name=f"Programme {i}", points_program=True,
                                sources=[f"src{i % 4}"], url=f"https://p{i}.example") for i in range(40)]
    collectors = {f"src{i}": (lambda conn=None, i=i: [o for o in opps if o.sources == [f"src{i}"]])
                  for i in range(4)}

    def failing(conn=None):
        airdrops.provider_error("srcx", "feed", "HTTPStatusError: 403 Forbidden " + "x" * 400, conn=conn)
        return []

    # Three dead sources with full-length error strings, as on the box (upbit, bithumb and
    # cryptolisting each failed every run).
    for name in ("srcx", "srcy", "srcz"):
        collectors[name] = failing
    monkeypatch.setattr(airdrops, "COLLECTORS", collectors)
    monkeypatch.setattr(nft, "opensea_drops", lambda conn=None: [])
    monkeypatch.setattr(listings, "refresh", lambda conn=None: 0)
    monkeypatch.setenv("KAIBA_ENABLE_NFT_HUNTER", "1")

    result = scheduler.job_hunters(SimpleNamespace(conn=tmp_db))
    full = airdrops.refresh_report(tmp_db, collectors)
    # The fixture reproduces the defect: the uncut airdrop receipt alone overflows the cell.
    assert "truncated" in scheduler._small({"airdrop": full})
    stored = scheduler._small(result)
    assert "truncated" not in stored
    air = stored["airdrop"]
    assert air["written"] == 40 and air["qualified_count"] == 0 and air["funded_action_authorized"] is False
    assert air["sources"]["srcx"]["state"] == "error" and len(air["sources"]["srcx"]["error"]) <= 80
    assert "unqualified_reason_counts" not in air and "note" not in air
    assert stored["nft"]["sources"]["helius_mint_watch"]["state"] == "no_deliveries"
    assert stored["listing"] == 0
