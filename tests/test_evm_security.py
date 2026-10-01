"""GMGN's Solana authority fields must not be read as answers on an EVM chain.

The bug these tests exist for. GMGN's ``token security`` body carries
``renounced_mint`` and ``renounced_freeze_account`` on every chain. On Solana they are
real: they describe the mint and freeze authorities of an SPL mint account. On an EVM
chain there is no such account, and GMGN emits both as a constant ``false``. The adapter
piped that constant into ``mint_authority_revoked=False`` and
``freeze_authority_revoked=False``, which are two separate BLOCKERs in ``dyor.RULES``, so
every EVM token GMGN could describe was quarantined on a property nobody had measured.

What was measured, live, on 2026-09-21 (``gmgn-cli token security``, one read each):

===========================  =========  ==============  ==================  =============
address / name               chain      renounced_mint  renounced_freeze..  is_renounced
===========================  =========  ==============  ==================  =============
31 tokens from the local     robinhood  false (31/31)   false (31/31)       true (31/31)
``token_dossiers`` copy
0x6982..1933 PEPE            eth        false           false               true
0x2260..c599 WBTC            eth        false           false               true
0x95ad..c4ce SHIB            eth        false           false               true
0x0e09..ce82 CAKE            bsc        false           false               true
0x8076..d8d3 SafeMoon v1     bsc        false           false               false
0x4ed4..efed DEGEN           base       false           false               true
0x532f..42e4 BRETT           base       false           false               true
So11..1112   WSOL            sol        **true**        **true**            null
DezX..B263   BONK            sol        **true**        **true**            null
3NZ9..qmJh                   sol        **true**        **true**            null
5XZw..uVqQ                   sol        **false**       **false**           null
===========================  =========  ==============  ==================  =============

38 of 38 EVM rows say ``false``; each of the 4 Solana rows says something that tracks its
own mint. PEPE and SHIB are the proof that the EVM value is not a strict read but a stub:
neither contract has a mint function, and both are reported as "mint not renounced".
``transfer_pausable`` and ``slippage_modifiable`` were absent on all 42.

Two things these tests deliberately do **not** allow.

*Substitution.* ``is_renounced`` is present on EVM and was ``true`` on 37 of the 38 rows,
which makes it look like the field to use. It is not. It describes ownership
renouncement, not mint authority — a contract can have a dead owner and a public
``mint()`` — and it does not even hold as ownership: GMGN reports ``is_renounced=true``
for WBTC, whose ``owner()`` answered 0xca06...beb7 when this repo read the chain on
2026-09-20 (the PEPE/BOBO/WBTC note in ``goplus._normalize_evm``). Asserting
``mint_authority_revoked=True`` from it would swap a false blocker for a false all-clear,
which is the worse of the two errors. The property stays unknown and
``partial_security_coverage`` says so.

*Collateral damage on Solana.* 5XZw..uVqQ is the control that matters: a live Solana mint
authority reported with the same literal ``false`` the EVM stub uses. Skipping on chain
rather than on value is the only thing that keeps that one a blocker, and
``test_a_solana_false_is_still_a_false`` fails if the skip is ever made value-based.

Every payload below is a verbatim live body, quoted whole so a reader can check the claim
rather than take the field list on trust. The only edited one is
``EVM_BODY_WITHOUT_VOCABULARY``, and it says what was removed and why.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from kaiba.core.schemas import EVM_CHAINS, Chain
from kaiba.intelligence import dyor
from kaiba.providers import gmgn_cli as g

# --------------------------------------------------------------------------- payloads
#
# Recorded 2026-09-21 from gmgn-cli `token security --raw`. Verbatim.

#: robinhood (Pons v2 launchpad), the chain the operator is waiting on.
#: ``renounced_mint``/``renounced_freeze_account`` false, ``is_renounced`` true.
EVM_BODY: dict[str, Any] = {
    "address": "0x0073950e4e64bc0bc180a1a5c96ffd0fc5c94352",
    "is_show_alert": False,
    "top_10_holder_rate": "0.0002",
    "burn_ratio": "0",
    "burn_status": "",
    "dev_token_burn_amount": "0",
    "dev_token_burn_ratio": "0",
    "is_open_source": True,
    "open_source": 1,
    "is_blacklist": False,
    "blacklist": 0,
    "is_honeypot": False,
    "honeypot": 0,
    "is_renounced": True,
    "renounced": 1,
    "renounced_freeze_account": False,
    "renounced_mint": False,
    "can_sell": 0,
    "can_not_sell": 0,
    "buy_tax": "0",
    "sell_tax": "0",
    "average_tax": "0",
    "high_tax": "0",
    "flags": [],
    "lockInfo": None,
    "lock_summary": {
        "is_locked": True,
        "lock_detail": [
            {"percent": "0.95", "pool": "0x" + "0" * 40, "is_blackhole": True}
        ],
        "lock_tags": None,
        "lock_percent": "0",
        "left_lock_percent": "0",
    },
    "hide_risk": False,
    "privileges": None,
}

#: WSOL. Both authorities really are revoked, and GMGN says so.
SOL_SAFE_BODY: dict[str, Any] = {
    "address": "So11111111111111111111111111111111111111112",
    "is_show_alert": False,
    "top_10_holder_rate": "0.0106259",
    "burn_ratio": "1",
    "burn_status": "burn",
    "dev_token_burn_amount": "0",
    "dev_token_burn_ratio": "0",
    "is_open_source": None,
    "open_source": 0,
    "is_blacklist": None,
    "blacklist": 0,
    "is_honeypot": None,
    "honeypot": 0,
    "is_renounced": None,
    "renounced": None,
    "renounced_freeze_account": True,
    "renounced_mint": True,
    "can_sell": 0,
    "can_not_sell": 0,
    "buy_tax": "0",
    "sell_tax": "0",
    "average_tax": "0",
    "high_tax": "0",
    "flags": [],
    "lockInfo": None,
    "lock_summary": {
        "is_locked": False,
        "lock_detail": None,
        "lock_tags": None,
        "lock_percent": "0",
        "left_lock_percent": "0",
    },
    "hide_risk": False,
    "privileges": None,
}

#: A Solana mint with a **live** mint and freeze authority. Reported with the identical
#: literal ``false`` that the EVM stub uses, which is exactly why the skip keys on the
#: chain and not on the value. Its dossier in the local DB copy blocks on
#: ``["mint_authority","freeze_authority"]``, and it must keep doing so.
SOL_UNSAFE_BODY: dict[str, Any] = {
    "address": "5XZw2LKTyrfvfiskJ78AMpackRjPcyCif1WhUsPDuVqQ",
    "is_show_alert": True,
    "top_10_holder_rate": "0.0701",
    "burn_ratio": "0",
    "burn_status": "none",
    "dev_token_burn_amount": "0",
    "dev_token_burn_ratio": "0",
    "is_open_source": None,
    "open_source": 0,
    "is_blacklist": None,
    "blacklist": 0,
    "is_honeypot": None,
    "honeypot": 0,
    "is_renounced": None,
    "renounced": None,
    "renounced_freeze_account": False,
    "renounced_mint": False,
    "can_sell": 0,
    "can_not_sell": 0,
    "buy_tax": "0",
    "sell_tax": "0",
    "average_tax": "0",
    "high_tax": "0",
    "flags": [],
    "lockInfo": None,
    "lock_summary": {
        "is_locked": False,
        "lock_detail": None,
        "lock_tags": None,
        "lock_percent": "0",
        "left_lock_percent": "0",
    },
    "hide_risk": False,
    "privileges": None,
}

#: :data:`EVM_BODY` with ``can_sell`` deleted, and nothing else changed.
#:
#: Needed because ``dyor._unwrap_gmgn`` decides a payload "already speaks our vocabulary"
#: when any key matches, and ``can_sell`` is both a GMGN field name and one of ours. With
#: it present the raw body is passed straight through and neither normalizer runs — the
#: long-standing behaviour documented in ``gmgn_cli``'s security section. Removing it is
#: the only way to reach the two translation paths from a realistic body.
EVM_BODY_WITHOUT_VOCABULARY: dict[str, Any] = {
    k: v for k, v in EVM_BODY.items() if k != "can_sell"
}

AUTHORITIES = ("mint_authority_revoked", "freeze_authority_revoked")


# ------------------------------------------------------------------ the two normalizers
#
# Both are tested against the same bodies, because dyor's ``normalize_gmgn`` is the
# fallback that runs when the provider adapter is missing or older, and a fix that lands
# in only one of them leaves the bug reachable.

NORMALIZERS = pytest.mark.parametrize(
    "normalize",
    [
        pytest.param(lambda body, chain: g.normalize_security(body, chain=chain), id="gmgn_cli"),
        pytest.param(lambda body, chain: dyor.normalize_gmgn(body, chain=chain), id="dyor"),
    ],
)


@NORMALIZERS
@pytest.mark.parametrize("chain", sorted(EVM_CHAINS, key=lambda c: c.value))
def test_evm_authority_stubs_are_absent_not_false(normalize, chain):
    """The whole bug, on every EVM chain: a stub must leave the property UNKNOWN."""
    props = normalize(EVM_BODY, chain)
    for prop in AUTHORITIES:
        assert prop not in props, f"{chain.value}: {prop}={props[prop]!r} was invented"


@NORMALIZERS
def test_solana_authorities_are_still_read(normalize):
    props = normalize(SOL_SAFE_BODY, Chain.SOL)
    assert props["mint_authority_revoked"] is True
    assert props["freeze_authority_revoked"] is True


@NORMALIZERS
def test_a_solana_false_is_still_a_false(normalize):
    """The control. Same literal ``false`` as the EVM stub, but on Solana it is an answer.

    If anyone ever rewrites the skip as "drop the field when it is ``false``" instead of
    "drop the field on an EVM chain", this is the test that fails.
    """
    props = normalize(SOL_UNSAFE_BODY, Chain.SOL)
    assert props["mint_authority_revoked"] is False
    assert props["freeze_authority_revoked"] is False


@NORMALIZERS
def test_is_renounced_is_never_substituted(normalize):
    """``is_renounced`` is true in this body. It must not become a mint-authority claim.

    Nor an ownership claim we do not have a property for: GMGN said ``is_renounced=true``
    for WBTC, which has a live owner on chain.
    """
    props = normalize(EVM_BODY, Chain.ROBINHOOD)
    assert EVM_BODY["is_renounced"] is True
    assert props.get("mint_authority_revoked") is None
    assert props.get("freeze_authority_revoked") is None
    assert not any("renounce" in k or "owner" in k for k in props)


@NORMALIZERS
def test_everything_else_in_the_body_survives(normalize):
    """The skip is two fields wide. Removing more would trade one blocker for another."""
    props = normalize(EVM_BODY, Chain.ROBINHOOD)
    assert props["can_sell"] is True  # from is_honeypot=false
    assert props["source_verified"] is True
    assert props["buy_tax_bps"] == Decimal(0)
    assert props["sell_tax_bps"] == Decimal(0)
    assert props["top10_pct"] == Decimal("0.02")


@NORMALIZERS
def test_an_evm_body_still_reports_a_real_honeypot(normalize):
    """Nothing about the skip may soften a field GMGN genuinely answers on EVM."""
    body = copy.deepcopy(EVM_BODY)
    body["is_honeypot"] = True
    props = normalize(body, Chain.ROBINHOOD)
    assert props["can_sell"] is False


# ------------------------------------------------------- two spellings, one property
#
# ``_BOOL_MAP`` writes ``freeze_authority_revoked`` from two GMGN fields and ``can_sell``
# from two more. Both were plain assignments, so tuple order decided the winner. Dropping
# ``renounced_freeze_account`` on EVM changes which entry survives, which is why the
# ordering has to stop deciding this.


@NORMALIZERS
def test_a_honeypot_is_not_overwritten_by_the_later_field(normalize):
    """``is_honeypot=1`` with ``can_not_sell=0`` used to resolve to ``can_sell=True``.

    ``can_not_sell`` is listed after ``is_honeypot``, so the cheerful half won and the
    ``honeypot`` blocker never fired. Not seen live — across the 42 bodies read on
    2026-09-21 the two fields never disagreed — but a provider contradicting itself about
    whether a token can be sold is not an argument for believing the safe half.
    """
    body = copy.deepcopy(SOL_SAFE_BODY)
    body["is_honeypot"] = 1
    body["can_not_sell"] = 0
    assert normalize(body, Chain.SOL)["can_sell"] is False


@NORMALIZERS
def test_a_pausable_transfer_does_not_overwrite_a_live_freeze_authority(normalize):
    """The same ordering hazard on the other pair, in the direction that was fail-open.

    ``renounced_freeze_account=false`` (authority live) followed by
    ``transfer_pausable=false`` (nothing pausable) used to end as ``True``, because
    ``transfer_pausable`` is listed last.
    """
    body = copy.deepcopy(SOL_UNSAFE_BODY)
    body["transfer_pausable"] = False
    assert normalize(body, Chain.SOL)["freeze_authority_revoked"] is False


@NORMALIZERS
def test_on_evm_transfer_pausable_is_the_only_voice_left(normalize):
    """With the stub gone, the real EVM analogue still answers — and still blocks.

    ``transfer_pausable`` was absent on all 42 bodies read on 2026-09-21, so this is
    the untaken branch rather than a measurement. It is pinned because the skip is what
    makes it reachable.
    """
    body = copy.deepcopy(EVM_BODY)
    body["transfer_pausable"] = True
    assert normalize(body, Chain.ROBINHOOD)["freeze_authority_revoked"] is False
    body["transfer_pausable"] = False
    assert normalize(body, Chain.ROBINHOOD)["freeze_authority_revoked"] is True


# ------------------------------------------------------------------ the constants
#
# Two copies of the skip set exist on purpose: a provider module importing from
# ``kaiba.intelligence`` would invert the layering, the same reason ``_BOOL_MAP`` itself is
# duplicated. Duplication needs a test or it becomes divergence.


def test_the_skip_set_is_exactly_the_two_measured_stubs():
    """Pinned so widening it is a deliberate act with its own measurement.

    Every other field in the body is either chain-neutral (``is_honeypot``, ``buy_tax``)
    or already absent on the chain that does not have it.
    """
    assert g._SOL_ONLY_BOOL_FIELDS == frozenset(
        {"renounced_mint", "renounced_freeze_account"}
    )


def test_the_two_copies_of_the_skip_set_agree():
    assert g._SOL_ONLY_BOOL_FIELDS == dyor._GMGN_SOL_ONLY_BOOLS


def test_the_skipped_fields_are_real_entries_in_the_map():
    """A typo in the skip set would silently do nothing at all."""
    sources = {src for src, _prop, _risk in g._BOOL_MAP}
    assert g._SOL_ONLY_BOOL_FIELDS <= sources
    assert {src for src, _p, _r in dyor._GMGN_BOOL_MAP} >= dyor._GMGN_SOL_ONLY_BOOLS


def test_the_providers_copy_of_the_unsafe_values_matches_dyors():
    """``gmgn_cli`` cannot import ``dyor``; this is what keeps the copy honest."""
    for prop, unsafe in g._BOOL_UNSAFE.items():
        assert dyor.BOOL_PROPERTIES[prop].unsafe_value == unsafe, prop
    emitted = {prop for _src, prop, _risk in g._BOOL_MAP}
    assert emitted <= set(g._BOOL_UNSAFE), emitted - set(g._BOOL_UNSAFE)


@pytest.mark.parametrize("chain", sorted(EVM_CHAINS, key=lambda c: c.value))
def test_every_evm_chain_is_recognised_as_evm(chain):
    assert g._is_evm(chain) is True
    assert g._is_evm(chain.value) is True


@pytest.mark.parametrize("value", [Chain.SOL, "sol", "not-a-chain", "", None])
def test_anything_not_provably_evm_keeps_the_solana_mapping(value):
    """The safe direction: wrongly Solana over-refuses, wrongly EVM under-refuses."""
    assert g._is_evm(value) is False


def test_an_unknown_chain_string_refuses_rather_than_forgets():
    """Concretely, what the previous test buys: the stub still blocks, it never vanishes."""
    props = g.normalize_security(EVM_BODY, chain="not-a-chain")
    assert props["mint_authority_revoked"] is False


# --------------------------------------------------------------- threading the chain
#
# The normalizers are only correct if the chain actually reaches them. Two call paths do
# that, and both used to drop it.


def test_normalize_security_defaults_to_solana():
    """The default has to be the mapping whose failure mode is a refusal."""
    assert g.normalize_security(SOL_UNSAFE_BODY)["mint_authority_revoked"] is False
    assert dyor.normalize_gmgn(SOL_UNSAFE_BODY)["mint_authority_revoked"] is False


def test_unwrap_gmgn_hands_the_chain_to_the_adapter():
    """``dyor._unwrap_gmgn`` called ``normalize_security(payload)`` with no chain at all,
    so the Solana mapping survived in the adapter even after the adapter learned better."""
    seen: list[Any] = []

    class FakeModule:
        @staticmethod
        def normalize_security(payload, *, chain):
            seen.append(chain)
            return g.normalize_security(payload, chain=chain)

    props, _receipt = dyor._unwrap_gmgn(
        EVM_BODY_WITHOUT_VOCABULARY, FakeModule, Chain.ROBINHOOD
    )
    assert seen == [Chain.ROBINHOOD]
    for prop in AUTHORITIES:
        assert prop not in props


def test_unwrap_gmgn_falls_back_chain_aware_when_the_adapter_has_no_normalizer():
    """An adapter without the hook lands on ``dyor.normalize_gmgn``, which must also skip."""
    props, _receipt = dyor._unwrap_gmgn(
        EVM_BODY_WITHOUT_VOCABULARY, object(), Chain.ROBINHOOD
    )
    assert props, "the fallback produced nothing at all"
    for prop in AUTHORITIES:
        assert prop not in props


@pytest.fixture
def legacy_entry_point(monkeypatch):
    """Strip ``security_properties`` so ``collect_gmgn`` falls back to ``token_security``.

    This is the path where the chain has furthest to travel: ``collect_gmgn`` ->
    ``_call_flexible`` -> ``_unwrap_gmgn`` -> the adapter's normalizer. The preferred entry
    point hands back already-translated props, so it never exercises the hand-off — which
    is exactly how a missing argument here could go unnoticed.
    """

    def install(body: dict[str, Any]):
        monkeypatch.delattr(g, "security_properties", raising=False)
        monkeypatch.delattr(g, "token_security_properties", raising=False)
        monkeypatch.setattr(
            g, "token_security", lambda address, chain, conn=None, **kw: copy.deepcopy(body)
        )

    return install


def test_collect_gmgn_threads_the_chain_all_the_way_down(legacy_entry_point, tmp_db):
    legacy_entry_point(EVM_BODY_WITHOUT_VOCABULARY)
    claims, _receipts, status = dyor.collect_gmgn(
        EVM_BODY["address"], Chain.ROBINHOOD, tmp_db
    )
    assert status == "ok"
    values = dyor.resolve(claims).values
    for prop in AUTHORITIES:
        assert prop not in values, f"{prop}={values[prop]!r} came from the Solana mapping"


def test_collect_gmgn_on_solana_still_reaches_the_mint_authority(legacy_entry_point, tmp_db):
    """Same fallback path, Solana body: the chain must not be lost in the other direction."""
    body = {k: v for k, v in SOL_UNSAFE_BODY.items() if k != "can_sell"}
    legacy_entry_point(body)
    claims, _receipts, status = dyor.collect_gmgn(body["address"], Chain.SOL, tmp_db)
    assert status == "ok"
    values = dyor.resolve(claims).values
    assert values["mint_authority_revoked"] is False
    assert values["freeze_authority_revoked"] is False


def test_unwrap_gmgn_does_not_retry_without_the_chain():
    """A pre-chain adapter must fail over to the chain-aware fallback, not be re-called.

    Calling it again unqualified would put the Solana mapping straight back on an EVM
    body, which is the failure this argument exists to remove.
    """
    calls: list[tuple] = []

    class OldModule:
        @staticmethod
        def normalize_security(payload):  # no chain kwarg, as before this change
            calls.append(("legacy",))
            return {"mint_authority_revoked": False}

    props, _receipt = dyor._unwrap_gmgn(
        EVM_BODY_WITHOUT_VOCABULARY, OldModule, Chain.ROBINHOOD
    )
    assert calls == []  # rejected by TypeError before the body ran
    assert "mint_authority_revoked" not in props


# ------------------------------------------------------------------- through the CLI
#
# One seam is patched, ``gmgn_cli._spawn``, so argv construction, classification, the
# limiter and the receipts are all the real ones.


@pytest.fixture
def replay(monkeypatch, tmp_db):
    """Serve a recorded ``token security`` body and an empty ``token info`` from the CLI."""
    import dataclasses
    import json

    from kaiba.core import limiter as lim

    real = lim.limits_for

    def relaxed(provider: str):
        base = real(provider)
        if provider != g.PROVIDER:
            return base
        return dataclasses.replace(base, min_interval_ms=0, capacity=10_000, refill_per_s=10_000.0)

    monkeypatch.setattr(lim, "limits_for", relaxed)
    monkeypatch.setattr(g, "cli_argv", lambda: ["node", "index.js"])

    def install(body: dict[str, Any]):
        def fake_spawn(argv: list[str], timeout_s: float) -> g._Raw:
            if "security" in argv:
                return g._Raw(0, json.dumps(body), "")
            return g._Raw(0, json.dumps({"address": body["address"]}), "")

        monkeypatch.setattr(g, "_spawn", fake_spawn)

    return install


def test_security_properties_threads_the_chain_on_evm(replay):
    replay(EVM_BODY)
    result = g.security_properties(EVM_BODY["address"], Chain.ROBINHOOD)
    assert result.ok
    for prop in AUTHORITIES:
        assert prop not in result.data
    assert result.data["can_sell"] is True


def test_security_properties_threads_the_chain_on_solana(replay):
    replay(SOL_UNSAFE_BODY)
    result = g.security_properties(SOL_UNSAFE_BODY["address"], Chain.SOL)
    assert result.ok
    assert result.data["mint_authority_revoked"] is False
    assert result.data["freeze_authority_revoked"] is False


def test_security_properties_accepts_the_chain_as_a_string(replay):
    """``dyor._call_flexible`` passes whatever the caller had; ``Chain | str`` is the
    declared type on every other entry point in the module."""
    replay(EVM_BODY)
    result = g.security_properties(EVM_BODY["address"], "robinhood")
    assert result.ok
    for prop in AUTHORITIES:
        assert prop not in result.data


# --------------------------------------------------------------- the verdict it changes
#
# The point of the whole change: the dossier that comes out the other end.


def _verdict(address: str, chain: Chain, conn) -> tuple[dyor.Resolution, list[dyor.Finding]]:
    claims, _receipts, status = dyor.collect_gmgn(address, chain, conn)
    assert status == "ok", status
    resolution = dyor.resolve(claims)
    return resolution, dyor.evaluate(resolution)


def _names(findings, severity):
    return {f.risk.value for f in findings if f.severity is severity}


def test_the_robinhood_token_is_no_longer_blocked_on_an_invented_authority(replay, tmp_db):
    replay(EVM_BODY)
    resolution, findings = _verdict(EVM_BODY["address"], Chain.ROBINHOOD, tmp_db)

    blockers = _names(findings, dyor.Severity.BLOCKER)
    assert "mint_authority" not in blockers
    assert "freeze_authority" not in blockers
    # It is not silently "fine" either: one of three critical properties is known, and the
    # dossier has to say so out loud.
    assert resolution.security_coverage == 1
    assert resolution.values["can_sell"] is True
    warnings = [f for f in findings if f.severity is dyor.Severity.WARNING]
    assert any("security_coverage=1/3" in f.detail for f in warnings), [
        f.detail for f in warnings
    ]
    assert any("mint_authority_revoked" in f.detail for f in warnings)


def test_the_solana_token_with_a_live_mint_authority_is_still_blocked(replay, tmp_db):
    """The same code path, same literal ``false``, opposite and correct verdict."""
    replay(SOL_UNSAFE_BODY)
    resolution, findings = _verdict(SOL_UNSAFE_BODY["address"], Chain.SOL, tmp_db)

    blockers = _names(findings, dyor.Severity.BLOCKER)
    assert "mint_authority" in blockers
    assert "freeze_authority" in blockers
    # Full 3/3 on Solana, against 1/3 for the EVM body above: the difference is entirely
    # the two fields GMGN can actually answer here and cannot answer there.
    assert resolution.security_coverage == 3


def test_an_evm_honeypot_is_still_blocked(replay, tmp_db):
    """Coverage of 1/3 is only acceptable while the one property left is doing its job."""
    body = copy.deepcopy(EVM_BODY)
    body["is_honeypot"] = True
    replay(body)
    _resolution, findings = _verdict(body["address"], Chain.ROBINHOOD, tmp_db)
    assert "honeypot" in _names(findings, dyor.Severity.BLOCKER)


def test_an_evm_token_with_no_sellability_answer_fails_closed(replay, tmp_db):
    """Removing the stubs must not turn a zero-coverage body into a pass.

    With ``is_honeypot`` and ``can_not_sell`` gone there is nothing left, and the verdict
    has to be ``no_security_coverage`` — a BLOCKER — not an empty clean sheet.
    """
    body = {k: v for k, v in EVM_BODY.items() if k not in ("is_honeypot", "can_not_sell")}
    replay(body)
    resolution, findings = _verdict(body["address"], Chain.ROBINHOOD, tmp_db)
    assert resolution.security_coverage == 0
    assert "unknown_safety" in _names(findings, dyor.Severity.BLOCKER)
