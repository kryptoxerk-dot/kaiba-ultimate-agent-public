"""Tests for tier-0 triage.

Three of these are load-bearing rather than ordinary coverage:

* ``test_tier0_makes_no_network_calls`` patches the socket layer to explode. Tier 0's
  entire value is that it costs nothing; the moment it dials out it becomes tier 1.
* ``test_no_reject_on_absent_data`` and its siblings encode the rule that "we do not know"
  is ``defer``. A screen that rejects on a missing field eats the whole market the day a
  provider renames a key, and does it silently.
* ``test_tier0_p99_under_budget`` measures the 100 ms p99 claim rather than asserting it.
"""

from __future__ import annotations

import socket
import sys
import time
import types
from dataclasses import replace
from typing import Any

import pytest

from kaiba.core.db import fetch_all, jdump
from kaiba.core.schemas import Chain, Token, now_ms
from kaiba.execution import triage as T

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CREATOR_A = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
CREATOR_B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
FUNDER = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"


#: The base58 alphabet minus '1', which is reserved here as the padding character so a
#: short tail can never collide with a longer one.
_B58_TAIL = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"



def mint(n: int) -> str:
    """A distinct, base58-valid mint.

    The tail is base58-encoded rather than decimal: '0' is not in the alphabet, so a
    decimal tail silently produces addresses that ``looks_solana`` rejects, and the test
    then measures the unparseable path instead of the one it meant to.
    """
    radix = len(_B58_TAIL)
    tail, value = "", n
    while True:
        tail = _B58_TAIL[value % radix] + tail
        value //= radix
        if value == 0:
            break
    return "M" + "1" * (42 - len(tail)) + tail


def event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "txType": "create",
        "mint": MINT_A,
        "name": "Kaiba Test Coin",
        "symbol": "KTC",
        "uri": "https://ipfs.io/ipfs/QmOriginalMetadata",
        "traderPublicKey": CREATOR_A,
        "solAmount": 0.25,
        "timestamp": 1_758_300_000,
        "pool": "pump",
    }
    base.update(over)
    return base


def seed_wallet(conn: Any, address: str, *, cohort: str | None = None, tags: list[str] | None = None,
                funder: str | None = None) -> None:
    ts = now_ms()
    conn.execute(
        "INSERT OR REPLACE INTO wallets (chain, address, source, tags_json, first_seen_ms, "
        "last_seen_ms, first_funder, cohort) VALUES (?,?,?,?,?,?,?,?)",
        ("sol", address, "test", jdump(tags or []), ts, ts, funder, cohort),
    )


def seed_creator(conn: Any, address: str, *, launches: int = 0, graduated: int = 0,
                 rugged: int = 0) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO creators (chain, address, launches, graduated, rugged, updated_ms) "
        "VALUES (?,?,?,?,?,?)",
        ("sol", address, launches, graduated, rugged, now_ms()),
    )


def decide(score: float, verdict: T.Verdict = T.Verdict.DEFER, token: str | None = None) -> T.TriageDecision:
    return T.TriageDecision(
        chain=Chain.SOL,
        token=token or mint(int(score * 1000)),
        verdict=verdict,
        score=score,
        ts_ms=now_ms(),
    )


@pytest.fixture
def real_dedup() -> Any:
    """Opt in to the sibling as it actually shipped.

    Every other test runs with it hidden. That is not squeamishness: another agent owns
    ``kaiba/intelligence/dedup.py`` and changes it while this suite runs, and a unit test
    of *this* module's rules must not flip verdict because their classifier got better.
    The tests that do want the real thing ask for it by name, so the integration stays
    covered and the rest stays deterministic.
    """
    return pytest.importorskip("kaiba.intelligence.dedup")


@pytest.fixture(autouse=True)
def _fresh_module_state(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    T.LATENCY.reset()
    T.set_queue(None)
    T.reset_dedup()
    if "real_dedup" not in request.fixturenames:
        hide_dedup(monkeypatch)
    yield
    T.set_queue(None)
    T.reset_dedup()


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------


def test_parse_launch_reads_the_pumpportal_shape() -> None:
    facts = T.parse_launch(event())
    assert facts.mint == MINT_A
    assert facts.creator == CREATOR_A
    assert facts.symbol == "KTC"
    assert facts.metadata_uri.endswith("QmOriginalMetadata")
    assert facts.dev_buy_lamports == 250_000_000
    assert facts.event_ms == 1_758_300_000_000  # seconds promoted to ms
    assert facts.chain is Chain.SOL


def test_parse_launch_accepts_a_token_model() -> None:
    token = Token(
        address=MINT_B,
        chain=Chain.SOL,
        symbol="ABC",
        name="Abc",
        creator=CREATOR_B,
        created_ms=1_758_300_000_000,
        launchpad="pump.fun",
        meta={"uri": "https://ipfs.io/ipfs/QmX", "initial_buy_lamports": 100},
    )
    facts = T.parse_launch(token)
    assert (facts.mint, facts.creator, facts.metadata_uri) == (MINT_B, CREATOR_B, "https://ipfs.io/ipfs/QmX")
    assert facts.dev_buy_lamports == 100


def test_parse_launch_records_what_it_could_not_read() -> None:
    facts = T.parse_launch({"mint": MINT_A})
    assert "creator" in facts.unknowns
    assert "name" in facts.unknowns
    assert facts.mint == MINT_A


def test_parse_launch_never_raises_on_garbage() -> None:
    for junk in (None, 17, "not a dict", [], {"mint": {"nested": True}}):
        facts = T.parse_launch(junk)  # type: ignore[arg-type]
        assert facts.mint is None


def test_fingerprints_ignore_case_whitespace_and_zero_width_tricks() -> None:
    a = T.parse_launch(event(name="Kaiba Test Coin", symbol="KTC"))
    b = T.parse_launch(event(mint=MINT_B, name="  kaiba   test​ coin ", symbol="ktc"))
    assert a.name_fingerprint == b.name_fingerprint


# --------------------------------------------------------------------------------------
# the rule that matters: never reject on absent data
# --------------------------------------------------------------------------------------


def test_empty_payload_defers_rather_than_rejects(tmp_db: Any) -> None:
    d = T.triage({}, conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert d.token is None
    assert "payload_unreadable" in d.reasons[0]


def test_renamed_provider_key_defers_the_whole_stream(tmp_db: Any) -> None:
    """The failure mode this design exists to make loud."""
    renamed = [{"txType": "create", "tokenMintAddr": mint(i), "nm": "x"} for i in range(20)]
    verdicts = {T.triage(e, conn=tmp_db).verdict for e in renamed}
    assert verdicts == {T.Verdict.DEFER}  # loud, not silent


def test_no_reject_on_absent_data(tmp_db: Any) -> None:
    """Every reject rule stands down when its inputs are unknown."""
    facts = T.parse_launch({"mint": MINT_A})  # nothing but a mint
    db = T.DbFacts(unknowns=("creator_history", "creator_cohort", "funder_cohort", "fingerprints"))
    assert T.evaluate_rejects(facts, db) == []
    assert T.triage({"mint": MINT_A}, conn=tmp_db).verdict is T.Verdict.DEFER


def test_reject_rules_declare_every_fact_they_read(tmp_db: Any) -> None:
    """A rule may only fire when all of its declared inputs are readable."""
    facts = T.parse_launch(event())
    for rule in T.REJECT_RULES:
        db = T.DbFacts(unknowns=rule.requires)
        assert rule.test is not None
        assert not any(
            name == rule.name for name, _ in T.evaluate_rejects(facts, db)
        ), f"{rule.name} fired with {rule.requires} unknown"


def test_a_rejection_never_cites_an_unknown(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, cohort="blacklist")
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert "creator" not in d.unknowns


def test_unknown_creator_is_neutral_not_adverse(tmp_db: Any) -> None:
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert not any(f.weight < 0 for f in d.factors)


# --------------------------------------------------------------------------------------
# rejections
# --------------------------------------------------------------------------------------


def test_blacklisted_creator_is_rejected(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, cohort="blacklist")
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert "creator_blacklisted" in d.reasons[0]


def test_hard_quarantine_tag_on_the_creator_is_rejected(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, tags=["scammer", "dev"])
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert any("scammer" in r for r in d.reasons)


def test_soft_penalty_tag_is_not_a_rejection(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, tags=["bundler"])
    assert T.triage(event(), conn=tmp_db).verdict is T.Verdict.DEFER


def test_blacklisted_funder_is_rejected(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, funder=FUNDER)
    seed_wallet(tmp_db, FUNDER, cohort="blacklist")
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert "funder_blacklisted" in d.reasons[0]


def test_creator_rug_history_rejects_at_the_bar_not_below_it(tmp_db: Any) -> None:
    seed_creator(tmp_db, CREATOR_A, launches=4, rugged=1)
    below = T.triage(event(), conn=tmp_db)
    assert below.verdict is T.Verdict.DEFER
    assert any(f.name == "creator_soft_rug" for f in below.factors)

    seed_creator(tmp_db, CREATOR_A, launches=4, rugged=2)
    at_bar = T.triage(event(), conn=tmp_db)
    assert at_bar.verdict is T.Verdict.REJECT


def test_reused_image_fingerprint_is_rejected_on_the_second_sighting(tmp_db: Any) -> None:
    first = event(mint=MINT_A, image="https://ipfs.io/ipfs/QmSameImage")
    assert T.screen_launch(first, conn=tmp_db).verdict is not T.Verdict.REJECT

    second = event(mint=MINT_B, name="Different Name", symbol="DIF",
                   image="https://ipfs.io/ipfs/QmSameImage")
    d = T.screen_launch(second, conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert "image_reused" in d.reasons[0]


def test_a_token_is_never_its_own_duplicate(tmp_db: Any) -> None:
    d = T.screen_launch(event(image="https://ipfs.io/ipfs/QmUnique"), conn=tmp_db)
    assert d.verdict is not T.Verdict.REJECT
    rows = fetch_all(tmp_db, "SELECT kind, hits FROM triage_fingerprints ORDER BY kind")
    assert [r["hits"] for r in rows] == [1, 1]


def test_name_reuse_needs_more_repeats_than_an_image(tmp_db: Any) -> None:
    for i in range(T.DEFAULT_CONFIG.reject_min_name_reuses):
        d = T.screen_launch(event(mint=mint(i), image=f"https://img/{i}"), conn=tmp_db)
        assert d.verdict is not T.Verdict.REJECT, f"rejected too early at repeat {i}"
    final = T.screen_launch(event(mint=mint(99), image="https://img/99"), conn=tmp_db)
    assert final.verdict is T.Verdict.REJECT
    assert "name_reused" in final.reasons[0]


# --------------------------------------------------------------------------------------
# promote / defer
# --------------------------------------------------------------------------------------


def test_creator_with_a_prior_graduate_is_promoted(tmp_db: Any) -> None:
    seed_creator(tmp_db, CREATOR_A, launches=3, graduated=1)
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.PROMOTE
    assert d.score >= T.DEFAULT_CONFIG.promote_threshold
    assert any(f.name == "creator_prior_graduate" for f in d.factors)


def test_trusted_cohort_creator_is_promoted(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_A, cohort="trusted_copy")
    seed_creator(tmp_db, CREATOR_A, launches=2, graduated=1)
    assert T.triage(event(), conn=tmp_db).verdict is T.Verdict.PROMOTE


def test_serial_launcher_with_nothing_to_show_is_penalised_not_rejected(tmp_db: Any) -> None:
    seed_creator(tmp_db, CREATOR_A, launches=40, graduated=0, rugged=0)
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert any(f.name == "creator_serial_launcher" for f in d.factors)


def test_defer_is_the_common_case_on_an_empty_database(tmp_db: Any) -> None:
    verdicts = [T.triage(event(mint=mint(i)), conn=tmp_db).verdict for i in range(25)]
    assert set(verdicts) == {T.Verdict.DEFER}


def test_score_is_clamped_and_ordered(tmp_db: Any) -> None:
    seed_creator(tmp_db, CREATOR_A, launches=9, graduated=5)
    seed_wallet(tmp_db, CREATOR_A, cohort="trusted_copy")
    strong = T.triage(event(), conn=tmp_db)
    weak = T.triage(event(mint=MINT_B, traderPublicKey=CREATOR_B), conn=tmp_db)
    assert 0.0 <= weak.score < strong.score <= 1.0


def test_missing_metadata_is_a_nudge_not_a_verdict(tmp_db: Any) -> None:
    d = T.triage({"txType": "create", "mint": MINT_A, "traderPublicKey": CREATOR_A}, conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    penalty = next(f for f in d.factors if f.name == "metadata_incomplete")
    assert abs(penalty.weight) < 0.1


# --------------------------------------------------------------------------------------
# no network, at all
# --------------------------------------------------------------------------------------


def test_tier0_makes_no_network_calls(real_dedup: Any, tmp_db: Any,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the socket layer to explode. Tier 0 must not notice.

    The attempt is *also* recorded in a list, because tier 0 swallows exceptions coming
    out of the sibling modules it calls. Asserting only on the raise would let a network
    call inside ``dedup`` pass this test silently, which is precisely the regression this
    exists to catch: ``dedup.classify`` measured 1.9 s on 2026-09-20 doing exactly that.
    """
    attempts: list[str] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        attempts.append(str(args[:2]))
        raise AssertionError("tier 0 attempted a network call")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    import httpx

    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(httpx, "get", boom, raising=False)
    monkeypatch.setattr(httpx, "post", boom, raising=False)

    from kaiba.providers import _http

    monkeypatch.setattr(_http, "get_json", boom)
    monkeypatch.setattr(_http, "post_json", boom)

    seed_wallet(tmp_db, CREATOR_A, funder=FUNDER)
    seed_creator(tmp_db, CREATOR_A, launches=2, graduated=1)
    d = T.screen_launch(event(), conn=tmp_db)
    assert attempts == [], f"tier 0 reached for the network: {attempts}"
    assert d.verdict in {T.Verdict.PROMOTE, T.Verdict.DEFER}


def test_triage_survives_a_database_that_is_gone(tmp_db: Any) -> None:
    tmp_db.close()
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER  # unreadable is not the same as bad


# --------------------------------------------------------------------------------------
# dedup, landing in parallel
# --------------------------------------------------------------------------------------


def hide_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the sibling unimportable, as it was before it landed.

    Both halves are needed: ``None`` in ``sys.modules`` makes the import raise, but
    ``from kaiba.intelligence import dedup`` never reaches the import machinery while the
    package still carries the attribute from an earlier successful import.
    """
    import kaiba.intelligence as pkg

    monkeypatch.delattr(pkg, "dedup", raising=False)
    monkeypatch.setitem(sys.modules, "kaiba.intelligence.dedup", None)
    T.reset_dedup()


def test_works_without_the_dedup_module(tmp_db: Any) -> None:
    """Before the sibling lands, tier 0 still works and says it does not know."""
    d = T.triage(event(), conn=tmp_db)
    assert "copycat_classification" in d.unknowns
    assert d.verdict is T.Verdict.DEFER


def test_a_failed_probe_is_not_retried_on_every_launch(tmp_db: Any) -> None:
    """A failing import is not cached by Python, and re-probing per launch costs real time."""
    assert T._load_dedup() is None
    first = T._DEDUP["probed_at"]
    assert first > 0
    for _ in range(10):
        assert T._load_dedup() is None
    assert T._DEDUP["probed_at"] == first  # one probe, not eleven


def test_tier0_never_binds_the_fetching_entry_point() -> None:
    """dedup.classify pulls IPFS metadata (1.9 s measured). It must not be reachable."""
    assert "classify" not in T._DEDUP_LOCAL_FUNCS
    assert "classify" not in T._DEDUP_FALLBACK_FUNCS
    assert T._DEDUP_LOCAL_FUNCS == ("classify_meta",)


def install_dedup(monkeypatch: pytest.MonkeyPatch, fn: Any, name: str = "classify_copycat") -> None:
    module = types.ModuleType("kaiba.intelligence.dedup")
    setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "kaiba.intelligence.dedup", module)
    import kaiba.intelligence as pkg

    monkeypatch.setattr(pkg, "dedup", module, raising=False)
    T.reset_dedup()


@pytest.mark.parametrize(
    "returned",
    [
        True,
        0.95,
        "copycat",
        {"is_copycat": True, "confidence": 0.95},
        {"classification": "copycat", "score": 0.95},
        (True, 0.95),
    ],
)
def test_dedup_result_shapes_are_all_understood(tmp_db: Any, monkeypatch: pytest.MonkeyPatch,
                                                returned: Any) -> None:
    install_dedup(monkeypatch, lambda *a, **k: returned)
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.REJECT
    assert "dedup_copycat" in d.reasons[0]


def test_a_dedup_that_raises_never_rejects(tmp_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*a: Any, **k: Any) -> Any:
        raise RuntimeError("half-written sibling")

    install_dedup(monkeypatch, explode)
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert "copycat_classification" in d.unknowns


def test_dedup_with_an_unexpected_signature_is_still_used(tmp_db: Any,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def positional_only(address: str, chain: Any) -> dict[str, Any]:
        return {"is_copycat": True, "confidence": 0.99}

    install_dedup(monkeypatch, positional_only, name="copycat")
    assert T.triage(event(), conn=tmp_db).verdict is T.Verdict.REJECT


def test_low_confidence_copycat_penalises_without_rejecting(tmp_db: Any,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    install_dedup(monkeypatch, lambda *a, **k: {"is_copycat": True, "confidence": 0.6})
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert any(f.name == "copycat_suspected" for f in d.factors)


def test_a_classifier_saying_unknown_is_an_unknown_not_an_acquittal(
    tmp_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dedup(monkeypatch, lambda *a, **k: {"status": "unknown"})
    d = T.triage(event(), conn=tmp_db)
    assert "copycat_classification" in d.unknowns


def test_a_slow_dedup_is_disconnected_after_one_call(tmp_db: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 0's 100 ms promise cannot be hostage to a sibling another team is changing."""
    calls: list[int] = []

    def slow(*a: Any, **k: Any) -> dict[str, Any]:
        calls.append(1)
        time.sleep(0.05)
        return {"is_copycat": False}

    install_dedup(monkeypatch, slow)
    cfg = replace(T.DEFAULT_CONFIG, dedup_budget_us=5_000)
    T.triage(event(), conn=tmp_db, config=cfg)
    assert len(calls) == 1
    assert T.dedup_status()["disabled_reason"] is not None
    for i in range(5):
        T.triage(event(mint=mint(i)), conn=tmp_db, config=cfg)
    assert len(calls) == 1, "breaker did not hold"


def test_the_real_dedup_module_is_bound_to_its_local_entry_point(real_dedup: Any, tmp_db: Any) -> None:
    """Integration with the sibling as it actually shipped."""
    binding = T._load_dedup()
    assert binding is not None, "dedup is importable but no local entry point was found"
    assert binding.name == "classify_meta"
    assert binding.local is True
    d = T.triage(event(), conn=tmp_db)
    assert d.verdict in {T.Verdict.DEFER, T.Verdict.PROMOTE}
    assert T.dedup_status()["disabled_reason"] is None, "the local entry point blew the budget"


def test_the_real_dedup_catches_a_repeat_once_the_index_is_fed(real_dedup: Any, tmp_db: Any) -> None:
    """screen_launch registers each launch so dedup's coverage window actually starts."""
    # Distinct URIs, identical name and symbol: our own image rule cannot see this, and
    # our name rule needs three repeats. Only dedup's text fingerprint catches it.
    first = T.screen_launch(
        event(mint=mint(1), name="Zebra Coin", symbol="ZBR", uri="https://ipfs.io/ipfs/QmOne"),
        conn=tmp_db,
    )
    assert first.verdict is not T.Verdict.REJECT
    second = T.screen_launch(
        event(mint=mint(2), name="Zebra Coin", symbol="ZBR", uri="https://ipfs.io/ipfs/QmTwo"),
        conn=tmp_db,
    )
    assert second.verdict is T.Verdict.REJECT
    assert "dedup_copycat" in second.reasons[0], second.reasons


def test_triage_alone_does_not_write_to_the_dedup_index(real_dedup: Any, tmp_db: Any) -> None:
    """triage() is the replayable, read-only half; only screen_launch registers."""
    before = fetch_all(tmp_db, "SELECT COUNT(*) AS n FROM dedup_mints")[0]["n"]
    T.triage(event(mint=mint(7), name="Read Only", symbol="RO"), conn=tmp_db)
    after = fetch_all(tmp_db, "SELECT COUNT(*) AS n FROM dedup_mints")[0]["n"]
    assert after == before


# --------------------------------------------------------------------------------------
# recording
# --------------------------------------------------------------------------------------


def test_every_decision_is_recorded_including_rejections(tmp_db: Any) -> None:
    seed_wallet(tmp_db, CREATOR_B, cohort="blacklist")
    T.screen_launch(event(), conn=tmp_db)
    T.screen_launch(event(mint=MINT_B, traderPublicKey=CREATOR_B, image="https://i/2",
                          name="Other", symbol="OTH"), conn=tmp_db)
    rows = fetch_all(tmp_db, "SELECT verdict, reasons_json, latency_us FROM triage_decisions")
    assert len(rows) == 2
    assert {r["verdict"] for r in rows} == {"defer", "reject"}
    assert all(r["latency_us"] is not None for r in rows)
    assert all(r["reasons_json"] != "[]" for r in rows)


def test_verdict_split_reports_the_shape_of_the_screen(tmp_db: Any) -> None:
    seed_creator(tmp_db, CREATOR_B, launches=2, graduated=2)
    for i in range(5):
        T.screen_launch(event(mint=mint(i), image=f"https://img/{i}", name=f"N{i}"), conn=tmp_db)
    T.screen_launch(event(mint=mint(50), traderPublicKey=CREATOR_B, name="Grad",
                          image="https://img/50"), conn=tmp_db)
    split = T.verdict_split(0, tmp_db)
    assert split["promote"] == 1
    assert split["defer"] >= 4
    assert sum(split.values()) == 6


def test_unknowns_are_persisted_so_the_absent_data_rule_is_auditable(tmp_db: Any) -> None:
    T.screen_launch({"txType": "create", "mint": MINT_A}, conn=tmp_db)
    row = fetch_all(tmp_db, "SELECT unknowns_json FROM triage_decisions")[0]
    assert "creator" in row["unknowns_json"]


# --------------------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------------------


def test_promote_always_outranks_defer_whatever_the_score() -> None:
    q = T.TriageQueue(capacity=8)
    q.offer(decide(0.99, T.Verdict.DEFER, token=mint(1)))
    q.offer(decide(0.10, T.Verdict.PROMOTE, token=mint(2)))
    assert q.pop().token == mint(2)


def test_higher_score_is_served_first() -> None:
    q = T.TriageQueue(capacity=8)
    for i, s in enumerate([0.1, 0.9, 0.5]):
        q.offer(decide(s, T.Verdict.DEFER, token=mint(i)))
    assert [d.score for d in q.pop_batch(3)] == [0.9, 0.5, 0.1]


def test_ties_are_served_oldest_first() -> None:
    q = T.TriageQueue(capacity=8)
    for i in range(3):
        q.offer(decide(0.4, T.Verdict.DEFER, token=mint(i)))
    assert [d.token for d in q.pop_batch(3)] == [mint(0), mint(1), mint(2)]


def test_rejections_never_enter_the_queue() -> None:
    q = T.TriageQueue(capacity=8)
    assert q.offer(decide(0.9, T.Verdict.REJECT, token=mint(1))) is False
    assert len(q) == 0


def test_a_bounded_queue_sheds_the_tail_not_the_head() -> None:
    q = T.TriageQueue(capacity=3)
    for i, s in enumerate([0.9, 0.8, 0.7]):
        q.offer(decide(s, T.Verdict.DEFER, token=mint(i)))
    assert q.offer(decide(0.1, T.Verdict.DEFER, token=mint(9))) is False  # worst: shed at the door
    assert q.offer(decide(0.95, T.Verdict.DEFER, token=mint(10))) is True  # better: evicts 0.7
    scores = [d.score for d in q.pop_batch(5)]
    assert scores == [0.95, 0.9, 0.8]
    assert q.stats().shed == 2


def test_the_same_mint_is_not_queued_twice() -> None:
    q = T.TriageQueue(capacity=8)
    assert q.offer(decide(0.5, T.Verdict.DEFER, token=MINT_A)) is True
    assert q.offer(decide(0.6, T.Verdict.DEFER, token=MINT_A)) is False
    assert len(q) == 1


def test_strict_mode_admits_promotions_only() -> None:
    q = T.TriageQueue(capacity=8, admit_defers=False)
    assert q.offer(decide(0.99, T.Verdict.DEFER, token=mint(1))) is False
    assert q.offer(decide(0.60, T.Verdict.PROMOTE, token=mint(2))) is True


def test_backpressure_reports_how_far_behind_we_are() -> None:
    q = T.TriageQueue(capacity=4)
    for i in range(10):
        q.offer(decide(0.5 + i / 100, T.Verdict.DEFER, token=mint(i)))
    stats = q.stats()
    assert stats.depth == 4
    assert stats.shed == 6
    assert stats.saturated is True
    assert stats.arrival_per_min > 0
    assert stats.drain_eta_s is None  # nothing served yet: "never", not a big number
    q.pop()
    q.pop()
    time.sleep(0.01)
    after = q.stats()
    assert after.served == 2
    assert after.drain_eta_s is not None and after.drain_eta_s >= 0


def test_backpressure_snapshot_is_persisted(tmp_db: Any) -> None:
    q = T.TriageQueue(capacity=2, name="tier1")
    for i in range(5):
        q.offer(decide(0.5, T.Verdict.DEFER, token=mint(i)))
    payload = T.snapshot_backpressure(q, tmp_db)
    rows = fetch_all(tmp_db, "SELECT depth, shed, saturated FROM triage_backpressure")
    assert len(rows) == 1
    assert rows[0]["depth"] == 2
    assert rows[0]["saturated"] == 1
    assert payload["shed"] == 3


def test_screen_launch_feeds_the_default_queue(tmp_db: Any) -> None:
    q = T.TriageQueue(capacity=16)
    T.set_queue(q)
    seed_creator(tmp_db, CREATOR_A, launches=2, graduated=1)
    d = T.screen_launch(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.PROMOTE
    assert T.get_queue() is q
    assert q.pop().token == MINT_A


def test_screen_launch_never_raises(tmp_db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(T, "read_db_facts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    d = T.screen_launch(event(), conn=tmp_db)
    assert d.verdict is T.Verdict.DEFER
    assert d.reasons[0].startswith("triage_error")


# --------------------------------------------------------------------------------------
# provenance and latency
# --------------------------------------------------------------------------------------


def test_every_threshold_declares_where_it_came_from() -> None:
    fields = set(T.TriageConfig.__dataclass_fields__)
    assert fields == set(T.THRESHOLD_PROVENANCE), (
        "TriageConfig and THRESHOLD_PROVENANCE disagree: "
        f"{fields ^ set(T.THRESHOLD_PROVENANCE)}"
    )
    for name, why in T.THRESHOLD_PROVENANCE.items():
        assert len(why) > 20, f"{name} has no real provenance note"


def test_invented_thresholds_are_marked_as_such() -> None:
    marked = [k for k, v in T.THRESHOLD_PROVENANCE.items() if "INVENTED" in v]
    assert len(marked) >= 12, "most of these are guesses and the code must say so"


def test_tier0_p99_under_budget(tmp_db: Any) -> None:
    """The 100 ms p99 claim, measured rather than asserted."""
    seed_wallet(tmp_db, CREATOR_A, funder=FUNDER)
    seed_wallet(tmp_db, FUNDER, cohort="tracked")
    seed_creator(tmp_db, CREATOR_A, launches=3, graduated=1)
    for i in range(200):  # a populated fingerprint index, not an empty one
        T.record_fingerprints(T.parse_launch(event(mint=mint(i), image=f"https://img/{i}",
                                                   name=f"Coin {i}")), tmp_db)
    T.LATENCY.reset()
    for i in range(500):
        T.triage(event(mint=mint(1000 + i), image=f"https://img/live/{i}"), conn=tmp_db)
    report = T.latency_report()
    assert report["samples"] == 500
    assert report["p99_us"] <= T.DEFAULT_CONFIG.latency_budget_us, report
    assert report["within_budget"] is True


def test_latency_is_recorded_on_the_decision(tmp_db: Any) -> None:
    d = T.triage(event(), conn=tmp_db)
    assert d.latency_us >= 0
    assert T.latency_report()["samples"] == 1


# ------------------------------------- the breaker must not cost us the filter forever


def test_a_tripped_breaker_recovers_after_its_cooldown(monkeypatch):
    """One slow call used to disconnect the copycat filter for the life of the process.

    On a long-running ingest service that means forever, and silently: the warning
    scrolls past once and the best-evidenced cheap filter we have is never heard from
    again. A trip is now temporary and counted.
    """
    T.reset_dedup_probe()
    clock = {"t": 1000.0}
    monkeypatch.setattr(T.time, "monotonic", lambda: clock["t"])

    T.disable_dedup("took too long")
    assert T.dedup_status()["disabled_reason"] is not None
    assert T._load_dedup() is None

    clock["t"] += T.DEDUP_COOLDOWN_S + 1
    T._load_dedup()  # the retry clears the trip regardless of whether the import works
    assert T.dedup_status()["disabled_reason"] is None


def test_trips_are_counted_so_a_broken_sibling_is_visible(monkeypatch):
    T.reset_dedup_probe()
    monkeypatch.setattr(T.time, "monotonic", lambda: 500.0)
    T.disable_dedup("one")
    T.disable_dedup("two")
    assert T.dedup_status()["trips"] == 2


def test_the_budget_affords_the_measured_cost():
    """classify_meta measured 26 ms live and tripped a 15 ms budget. 40 ms affords it."""
    assert T.TriageConfig().dedup_budget_us >= 30_000
    assert T.TriageConfig().dedup_budget_us <= T.TriageConfig().latency_budget_us
