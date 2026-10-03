"""The wallet importer: read the operator's notes without trusting or leaking them."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.schemas import Chain, WalletTag
from kaiba.intelligence import import_wallets as iw

EVM_A = "0x" + "a1" * 20
EVM_B = "0x" + "b2" * 20
SOL_A = "Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB"


# ------------------------------------------------------------------ label parsing


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("DEV SCAMMER", {WalletTag.DEV, WalletTag.SCAMMER}),
        ("FreshInsider?", {WalletTag.FRESH_WALLET, WalletTag.SUSPECTED_INSIDER}),
        ("$PEZ TOP TRADER", {WalletTag.TOP_TRADER}),
        ("Sniper GG juga", {WalletTag.SNIPER}),
        ("TopTraderWhale", {WalletTag.TOP_TRADER, WalletTag.TOP_HOLDER}),
    ],
)
def test_real_labels_from_the_export_produce_the_right_tags(label, expected):
    assert set(iw.parse_label(label).tags) == expected


def test_a_hedged_insider_is_only_ever_suspected():
    """The operator's question mark is the whole difference between a claim and a guess."""
    sure = iw.parse_label("Insider")
    unsure = iw.parse_label("Insider?")
    assert WalletTag.INSIDER in sure.tags and WalletTag.SUSPECTED_INSIDER not in sure.tags
    assert WalletTag.SUSPECTED_INSIDER in unsure.tags and WalletTag.INSIDER not in unsure.tags
    assert unsure.hedged is True


def test_an_unreadable_label_yields_nothing_rather_than_a_guess():
    facts = iw.parse_label("asdlkjqwe 8812 zzz")
    assert facts.tags == frozenset()
    assert facts.pnl_amount is None
    assert facts.parsed_anything is False


def test_an_empty_label_is_not_an_error():
    assert iw.parse_label(None).raw == ""
    assert iw.parse_label("").parsed_anything is False


@pytest.mark.parametrize(
    ("label", "amount", "unit"),
    [
        ("100 BNB PNL", Decimal("100"), "bnb"),
        ("EARLY BANGET $401K RH", Decimal("401000"), "usd"),
        ("167e+ pnl rh", Decimal("167"), "eth"),
    ],
)
def test_pnl_figures_are_read_as_decimals_with_their_unit(label, amount, unit):
    facts = iw.parse_label(label)
    assert facts.pnl_amount == amount
    assert facts.pnl_unit == unit
    assert isinstance(facts.pnl_amount, Decimal)


def test_a_winrate_is_not_mistaken_for_a_pnl_amount():
    facts = iw.parse_label("55%WR")
    assert facts.winrate_pct == Decimal("55")
    assert facts.pnl_amount is None


def test_an_impossible_winrate_is_rejected():
    assert iw.parse_label("900% wr").winrate_pct is None


@pytest.mark.parametrize(
    ("label", "chain"),
    [("100 BNB PNL", Chain.BSC), ("167e+ pnl rh", Chain.ROBINHOOD), ("top trader base", Chain.BASE)],
)
def test_the_label_supplies_the_chain_an_evm_address_cannot(label, chain):
    assert iw.parse_label(label).chain_hint is chain


# ------------------------------------------------------------------ file reading


def test_the_credential_preamble_is_dropped_and_never_returned(tmp_path):
    """The notes file opens with secrets. None of them may survive the read."""
    secret = "GMGN_API_KEY=sk-live-DO-NOT-LEAK-7f3a9c"
    f = tmp_path / "notes.txt"
    f.write_text(
        f"{secret}\nsome other private line\nBEARER abcdef123456\n"
        f'[{{"address": "{EVM_A}", "name": "top trader", "emoji": ""}}]\n',
        encoding="utf-8",
    )
    records = iw.load_notes(f)
    assert len(records) == 1
    assert records[0]["address"] == EVM_A
    blob = json.dumps(records)
    assert "sk-live" not in blob
    assert "DO-NOT-LEAK" not in blob
    assert "BEARER" not in blob


def test_a_notes_file_with_no_json_returns_nothing_rather_than_raising(tmp_path):
    f = tmp_path / "creds_only.txt"
    f.write_text("API_KEY=nope\nSECRET=alsonope\n", encoding="utf-8")
    assert iw.load_notes(f) == []


def test_the_export_must_be_a_json_array(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text('{"address": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON array"):
        iw.load_export(f)


def test_export_records_without_an_address_are_dropped(tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps([{"name": "orphan"}, {"address": EVM_A, "name": "keep"}]), encoding="utf-8")
    assert [r["name"] for r in iw.load_export(f)] == ["keep"]


# ------------------------------------------------------------------ importing


def rec(addr: str, name: str = "") -> dict:
    return {"address": addr, "name": name, "emoji": "", "sound": ""}


def test_a_scammer_label_lands_in_the_blacklist_not_the_watchlist(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(EVM_A, "DEV SCAMMER")], source="t", conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT cohort FROM wallets WHERE address=?", (EVM_A.lower(),))
    assert row["cohort"] == iw.COHORT_BLACKLIST


def test_a_blacklisting_is_not_cleared_by_a_later_neutral_import(tmp_db):
    """Re-importing a cleaned-up export must not quietly un-blacklist a known scammer."""
    from kaiba.core.db import fetch_one

    iw.import_records([rec(EVM_A, "rug")], source="t", conn=tmp_db)
    iw.import_records([rec(EVM_A, "nice wallet")], source="t", conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT cohort FROM wallets WHERE address=?", (EVM_A.lower(),))
    assert row["cohort"] == iw.COHORT_BLACKLIST


def test_a_good_label_never_reaches_a_trusted_cohort(tmp_db):
    """A label is a hypothesis. Only measured PnL may promote a wallet."""
    from kaiba.core.db import fetch_all

    iw.import_records(
        [rec(EVM_A, "$PEZ TOP TRADER"), rec(EVM_B, "100 BNB PNL")], source="t", conn=tmp_db
    )
    cohorts = {r["cohort"] for r in fetch_all(tmp_db, "SELECT cohort FROM wallets", [])}
    assert cohorts == {iw.COHORT_RESEARCH}
    assert "trusted_copy" not in cohorts


def test_claimed_pnl_is_stored_as_a_label_not_as_a_measurement(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(EVM_A, "100 BNB PNL")], source="t", conn=tmp_db)
    meta = json.loads(fetch_one(tmp_db, "SELECT meta_json FROM wallets", [])["meta_json"])
    assert meta["label_basis"] == "operator_label"
    assert meta["label_pnl"] == {"amount": "100", "unit": "bnb", "window_days": None}


def test_importing_twice_updates_rather_than_duplicates(tmp_db):
    from kaiba.core.db import fetch_all

    first = iw.import_records([rec(EVM_A, "sniper")], source="t", conn=tmp_db)
    second = iw.import_records([rec(EVM_A, "sniper")], source="t", conn=tmp_db)
    assert first.imported == 1 and second.imported == 0 and second.updated == 1
    assert len(fetch_all(tmp_db, "SELECT address FROM wallets", [])) == 1


def test_tags_accumulate_across_imports_rather_than_being_replaced(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(EVM_A, "sniper")], source="t", conn=tmp_db)
    iw.import_records([rec(EVM_A, "fresh")], source="t", conn=tmp_db)
    tags = set(json.loads(fetch_one(tmp_db, "SELECT tags_json FROM wallets", [])["tags_json"]))
    assert tags == {"sniper", "fresh_wallet"}


def test_the_chain_hint_beats_the_evm_default(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(EVM_A, "100 BNB PNL")], source="t", conn=tmp_db, default_evm_chain=Chain.ETH)
    assert fetch_one(tmp_db, "SELECT chain FROM wallets", [])["chain"] == Chain.BSC.value


def test_a_solana_address_is_detected_without_a_hint(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(SOL_A, "")], source="t", conn=tmp_db)
    row = fetch_one(tmp_db, "SELECT chain, address FROM wallets", [])
    assert row["chain"] == Chain.SOL.value
    assert row["address"] == SOL_A  # solana addresses stay case-sensitive


def test_evm_addresses_are_lowercased(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec("0x" + "AB" * 20, "")], source="t", conn=tmp_db)
    assert fetch_one(tmp_db, "SELECT address FROM wallets", [])["address"] == "0x" + "ab" * 20


def test_a_malformed_address_is_counted_not_imported(tmp_db):
    rep = iw.import_records([rec("not-an-address", "x"), rec(EVM_A, "y")], source="t", conn=tmp_db)
    assert rep.skipped_bad_address == 1
    assert rep.imported == 1


def test_dry_run_writes_nothing(tmp_db):
    from kaiba.core.db import fetch_all

    rep = iw.import_records([rec(EVM_A, "sniper")], source="t", conn=tmp_db, dry_run=True)
    assert rep.seen == 1
    assert fetch_all(tmp_db, "SELECT address FROM wallets", []) == []


def test_the_report_states_how_little_was_parsed(tmp_db):
    """The parse rate is the honest headline: most labels yield nothing structured."""
    records = [rec(EVM_A, "sniper"), rec(EVM_B, "zzz qqq 999")]
    rep = iw.import_records(records, source="t", conn=tmp_db)
    assert rep.labels_total == 2
    assert rep.labels_parsed == 1
    assert rep.parse_rate == 0.5
    assert rep.as_dict()["by_tag"] == {"sniper": 1}


def test_a_defaulted_chain_is_counted_and_marked_unverified(tmp_db):
    """4,579 of the operator's 6,236 wallets have no chain in their label. Say so."""
    from kaiba.core.db import fetch_one

    rep = iw.import_records([rec(EVM_A, "top trader")], source="t", conn=tmp_db)
    assert rep.chain_defaulted == 1
    meta = json.loads(fetch_one(tmp_db, "SELECT meta_json FROM wallets", [])["meta_json"])
    assert meta["chain_is_default_not_observed"] is True
    assert "unverified" in rep.as_dict()["chain_defaulted_note"]


def test_a_chain_named_in_the_label_is_not_counted_as_defaulted(tmp_db):
    from kaiba.core.db import fetch_one

    rep = iw.import_records([rec(EVM_A, "100 BNB PNL")], source="t", conn=tmp_db)
    assert rep.chain_defaulted == 0
    meta = json.loads(fetch_one(tmp_db, "SELECT meta_json FROM wallets", [])["meta_json"])
    assert "chain_is_default_not_observed" not in meta


def test_a_solana_address_is_never_counted_as_a_defaulted_chain(tmp_db):
    assert iw.import_records([rec(SOL_A, "")], source="t", conn=tmp_db).chain_defaulted == 0


# ------------------------------------------- GMGN's own export convention


@pytest.mark.parametrize(
    ("label", "chain", "usd", "days", "wr"),
    [
        ("SOL_5.2KUSD_30dPnL_wrNA", Chain.SOL, Decimal("5200.0"), 30, None),
        ("SOL_12.4KUSD_7dPnL_63wr", Chain.SOL, Decimal("12400.0"), 7, Decimal("63")),
        ("ETH_1.5MUSD_30dPnL_55wr", Chain.ETH, Decimal("1500000.0"), 30, Decimal("55")),
    ],
)
def test_the_gmgn_convention_is_read_exactly(label, chain, usd, days, wr):
    """277 labels in the notes file use this machine-written form. Read it, do not guess."""
    f = iw.parse_label(label)
    assert f.structured is True
    assert f.chain_hint is chain
    assert f.pnl_amount == usd
    assert f.pnl_window_days == days
    assert f.winrate_pct == wr


def test_a_near_miss_of_the_convention_falls_through_rather_than_half_parsing(tmp_db):
    """A truncated label must not be read as if the missing field were absent-but-fine."""
    f = iw.parse_label("SOL_5.2KUSD_30dPnL")
    assert f.structured is False
    assert f.pnl_amount is None


def test_a_machine_written_figure_records_a_different_basis(tmp_db):
    from kaiba.core.db import fetch_one

    iw.import_records([rec(SOL_A, "SOL_5.2KUSD_30dPnL_wrNA")], source="t", conn=tmp_db)
    meta = json.loads(fetch_one(tmp_db, "SELECT meta_json FROM wallets", [])["meta_json"])
    assert meta["label_basis"] == "gmgn_export_label"
    assert meta["label_pnl"]["window_days"] == 30


def test_even_a_machine_written_figure_never_promotes_a_wallet(tmp_db):
    """It is still an export from an unknown moment, not a measurement we made."""
    from kaiba.core.db import fetch_one

    iw.import_records([rec(SOL_A, "SOL_900KUSD_30dPnL_95wr")], source="t", conn=tmp_db)
    assert fetch_one(tmp_db, "SELECT cohort FROM wallets", [])["cohort"] == iw.COHORT_RESEARCH


def test_the_report_separates_structured_labels_from_guessed_ones(tmp_db):
    rep = iw.import_records(
        [rec(SOL_A, "SOL_5.2KUSD_30dPnL_wrNA"), rec(EVM_A, "sniper")], source="t", conn=tmp_db
    )
    assert rep.labels_structured == 1
    assert rep.labels_parsed == 2
