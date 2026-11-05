"""The vendor-label leak, one file over from naming: ``tracker.seed_from_cohorts``.

naming-v2 closed the leak on its own path by writing every GMGN label as ``gmgn:<label>``.
``seed_from_cohorts`` step 1 still wrote ``cohort.gmgn_tags`` verbatim and bare onto
``wallets.tags_json`` for EVERY feed wallet, before the screen, refused wallets included.
Read side: ``grade._tags_from`` admits a bare ``kol``/``bluechip_owner`` into
``POSITIVE_REPUTATION_TAGS`` (1.2-2.4 reputation points on the backfill path), a bare
``pump_smart`` reaches ``infer_archetype`` -> SMART_MONEY, and ``lanes._tags`` counts a
bare ``smart_money``/``pump_smart``/``renowned``/``top_trader`` toward ``sm_trenches``'s
smart cohort. ``config/schedule.yaml`` runs the seed every 1800 s.

MEASURED 2026-09-22 on the live box (wallets 0 rows, watchlist 0 rows, feed rows only):
the first pass under the old code would have written a bare ``kol``/``bluechip_owner`` on
122 bsc, 155 sol and 143 robinhood wallets and a bare ``pump_smart`` on 1 sol wallet,
none of them screened. Under the code these tests pin: 0.

The rule: before the screen, vendor-namespaced tags only. A bare tag in
``lanes.SMART_TAGS | grade.POSITIVE_REPUTATION_TAGS`` may be written only after the
wallet passed the screen (the call that wrote its ``tracker_watchlist`` row), and only
``smart_money`` (``tracker.COHORT_DERIVED_TAGS``). A refused or evicted wallet loses it.
"""

from __future__ import annotations

import pytest

from kaiba.core.db import fetch_one, jdump, jload
from kaiba.core.events import emit
from kaiba.core.schemas import Archetype, Chain, EventKind, EvidenceBasis, Receipt, WalletTag, now_ms
from kaiba.execution import lanes
from kaiba.intelligence import discover, grade, naming, tracker
from kaiba.intelligence.naming import GMGN_TAG_PREFIX, vendor_tag

BSC = Chain.BSC
SMART = "gmgn:smartmoney"
NOW = now_ms()
HOUR = 3_600_000
WEI = "150000000000000000"  # 0.15 BNB in base units

#: Every word the money path reads bare out of ``wallets.tags_json``.
MONEY_PATH_WORDS: frozenset[str] = frozenset(
    {t.value for t in lanes.SMART_TAGS}
    | {t.value for t in grade.POSITIVE_REPUTATION_TAGS}
    | tracker.LANE_SMART_TAGS
)
#: Every cohort label GMGN is known to send, plus every WalletTag spelling, so a label
#: spelled exactly like a tag the money path reads cannot slip through by that spelling.
EVERY_LABEL = sorted(
    set(naming.GMGN_COHORT_ORDER) | {t.value for t in WalletTag} | {"brand_new_cohort"}
)
#: A feed wallet wearing every positive word the money path knows, plus the smart label.
LOADED = ("smart_degen", "kol", "smart_money", "pump_smart", "top_trader", "renowned", "bluechip_owner", "gmgn")


def _evm(i: int) -> str:
    return "0x" + f"{i:040x}"


def _tok(i: int) -> str:
    return "0x" + f"{0xF000 + i:040x}"


def _feed_trade(conn, wallet: str, token: str, side: str, ts: int, *, tags: tuple[str, ...]) -> None:
    tx = f"tx-{wallet[-6:]}-{token[-4:]}-{side}-{ts}"
    conn.execute(
        "INSERT OR IGNORE INTO swaps (chain, tx, slot, ts_ms, wallet, token, side, amount_token, "
        " amount_native, price_usd, usd_value, program, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (BSC.value, tx, None, ts, wallet, token, side, "1000000", WEI, "0.0001", "120", None, SMART),
    )
    emit(
        EventKind.WALLET_TRADE,
        {
            "chain": BSC.value, "tx": tx, "ts_ms": ts, "wallet": wallet, "token": token, "side": side,
            "usd_value": "120", "source": SMART, "feed": "smartmoney", "wallet_name": None,
            "tags": list(tags),
        },
        chain=BSC,
        subject=wallet,
        conn=conn,
    )


def _feed_wallet(conn, wallet: str, *, tags: tuple[str, ...], buys: int = 8, sells: int = 6) -> None:
    """A bsc feed wallet with enough history to be shape-checked (>= 10 swaps, buys > 10%)."""
    ts = NOW - 6 * HOUR
    for k in range(buys):
        _feed_trade(conn, wallet, _tok(k), "buy", ts, tags=tags)
        ts += 60_000
    for k in range(sells):
        _feed_trade(conn, wallet, _tok(k), "sell", ts, tags=tags)
        ts += 60_000
    conn.commit()


def _relabel(conn, wallet: str, tags: tuple[str, ...]) -> None:
    """Replace what the feed events say about ``wallet`` (labels are gathered from events)."""
    conn.execute("DELETE FROM events WHERE kind='wallet.trade' AND chain=? AND subject=?", (BSC.value, wallet))
    conn.execute("DELETE FROM swaps WHERE chain=? AND wallet=?", (BSC.value, wallet))
    conn.commit()
    _feed_wallet(conn, wallet, tags=tags)


def _measured(rate: float, *, seen: int = 500):
    def _fake(chain, address, conn=None, *, config=tracker.DEFAULT_CONFIG):
        return (
            rate,
            {"signatures_seen": seen, "signatures_failed": int(rate * seen), "last_activity_ms": NOW},
            Receipt(provider="helius", endpoint="tx.getTransactionsForAddress"),
        )

    return _fake


def _row(conn, wallet: str):
    return fetch_one(conn, "SELECT * FROM wallets WHERE chain=? AND address=?", (BSC.value, wallet))


def _tags(conn, wallet: str) -> set[str]:
    row = _row(conn, wallet)
    return {str(t) for t in (jload(row["tags_json"], []) if row else [])}


def _bare(tags: set[str]) -> set[str]:
    return {t for t in tags if not t.startswith(GMGN_TAG_PREFIX)}


def _derived(conn, wallet: str) -> list[str]:
    row = _row(conn, wallet)
    return list(jload(row["meta_json"], {}).get("kaiba_derived_tags", [])) if row else []


def _lane_reads_smart(conn, wallet: str) -> bool:
    """``lanes._tags`` on a swap row without a tags column, the way sm_trenches reads it."""
    return bool(lanes._tags(BSC, {"wallet": wallet}, conn) & {t.value for t in lanes.SMART_TAGS})


# --------------------------------------------------------------------------------------
# (a) a feed wallet that does not pass the screen carries nothing the money path reads
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", EVERY_LABEL)
def test_a_refused_feed_wallet_carries_no_money_path_tag_whatever_gmgn_called_it(tmp_db, monkeypatch, label):
    """The pin for step 1. Writing ``cohort.gmgn_tags`` bare fails this for the label
    concerned: a bare ``kol`` earns reputation points, a bare ``pump_smart`` is
    SMART_MONEY to the archetype, a bare ``top_trader`` is smart to the lane."""
    conn = tmp_db
    w = _evm(1)
    _feed_wallet(conn, w, tags=(label, "gmgn"))
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.37))

    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == [] and report.refused[0]["address"] == w
    assert tracker.get_entry(BSC, w, conn) is None  # refused: no watchlist row at all
    assert report.wallet_rows_written == 1  # the vendor's word was recorded ...

    tags = _tags(conn, w)
    assert tags == {vendor_tag(label), vendor_tag("gmgn")}, tags  # ... namespaced, nothing else
    assert not (tags & MONEY_PATH_WORDS)
    assert not (tags & {t.value for t in WalletTag}), "a vendor label is never spelled as a WalletTag"
    assert jload(_row(conn, w)["meta_json"], {})["gmgn"]["tags"] == [label, "gmgn"]  # the raw fact is kept

    # The three readers, read the way they read it.
    assert tracker.lane_smart_wallets(BSC, conn) == set()
    assert not _lane_reads_smart(conn, w)
    evidence = grade.build_evidence(w, BSC, conn)
    assert not (set(evidence.tags) & set(grade.POSITIVE_REPUTATION_TAGS))
    assert not (set(evidence.tags) & lanes.SMART_TAGS)
    assert all(t in grade.VENDOR_ADMITTED_TAGS for t in evidence.tags), evidence.tags
    assert not any(f.name == "reputation" for f in grade.components_for(evidence)[0])
    assert naming.infer_archetype(evidence) not in {Archetype.SMART_MONEY, Archetype.TOP_TRADER, Archetype.KOL}


def test_before_the_screen_the_row_carries_only_namespaced_labels(tmp_db, monkeypatch):
    """Step 1 runs before the screen. At the moment the screen reads the row, nothing bare
    is on it and nothing is on the watchlist; a bare word appears only after admission."""
    conn = tmp_db
    w = _evm(2)
    _feed_wallet(conn, w, tags=LOADED)
    real_screen = tracker.screen_wallet
    at_screen: list[dict] = []

    def _spy(chain, address, conn_=None, **kw):
        at_screen.append({
            "tags": _tags(conn, address),
            "watchlist": tracker.get_entry(chain, address, conn),
            "lane": tracker.lane_smart_wallets(chain, conn),
        })
        return real_screen(chain, address, conn_, **kw)

    monkeypatch.setattr(tracker, "screen_wallet", _spy)
    report = tracker.seed_from_cohorts(BSC, conn)

    (seen,) = at_screen
    assert seen["tags"] == {vendor_tag(t) for t in LOADED}, seen["tags"]
    assert not _bare(seen["tags"])
    assert seen["watchlist"] is None and seen["lane"] == set()

    # bsc: no signature source, so the wallet enters at the lower tier — screened, admitted.
    assert report.admitted == [w] and report.smart_tags_written == 1
    after = _tags(conn, w)
    assert _bare(after) == {WalletTag.SMART_MONEY.value}
    assert after - _bare(after) == {vendor_tag(t) for t in LOADED}
    assert _derived(conn, w) == [WalletTag.SMART_MONEY.value]
    meta = jload(_row(conn, w)["meta_json"], {})["gmgn"]
    assert meta["tags"] == list(LOADED) and meta["tag_prefix"] == GMGN_TAG_PREFIX
    # ``kol``, ``pump_smart``, ``top_trader``, ``renowned``, ``bluechip_owner`` arrived as
    # labels and are still not tags: the only positive word on the row is ours.
    assert set(grade._tags_from(after)) == {WalletTag.SMART_MONEY}


def test_the_namespace_is_naming_v2s_not_a_second_spelling(tmp_db):
    conn = tmp_db
    w = _evm(3)
    _feed_wallet(conn, w, tags=("smart_degen", "kol"))
    (cohort,) = discover.cohort_wallets(BSC, conn)
    assert tracker._vendor_tags(cohort) == [vendor_tag("smart_degen"), vendor_tag("kol")]
    assert all(t.startswith(naming.GMGN_TAG_PREFIX) for t in tracker._vendor_tags(cohort))
    assert naming.GMGN_TAG_PREFIX == grade.VENDOR_TAG_PREFIX
    assert not any(w_.startswith(GMGN_TAG_PREFIX) for w_ in MONEY_PATH_WORDS)
    assert tracker.COHORT_DERIVED_TAGS == {WalletTag.SMART_MONEY}
    assert {t.value for t in tracker.COHORT_DERIVED_TAGS} <= tracker.LANE_SMART_TAGS


def test_the_helper_refuses_to_write_any_bare_tag_but_smart_money(tmp_db):
    conn = tmp_db
    w = _evm(4)
    _feed_wallet(conn, w, tags=("smart_degen", "kol"))
    (cohort,) = discover.cohort_wallets(BSC, conn)
    for tag in (WalletTag.KOL, WalletTag.TOP_TRADER, WalletTag.PUMP_SMART, WalletTag.RENOWNED, WalletTag.BLUECHIP_OWNER):
        with pytest.raises(ValueError, match="may not write bare"):
            tracker._write_wallet_tags(conn, BSC, w, cohort, add=[tag], source="gmgn:cohort:smart_degen")
    assert _row(conn, w) is None  # refused before any write
    added, stripped = tracker._write_wallet_tags(
        conn, BSC, w, cohort, add=[WalletTag.SMART_MONEY], source="gmgn:cohort:smart_degen"
    )
    assert added and not stripped
    assert _bare(_tags(conn, w)) == {WalletTag.SMART_MONEY.value}


# --------------------------------------------------------------------------------------
# (b) a screened-and-admitted wallet carries smart_money, where the lane reads it
# --------------------------------------------------------------------------------------


def test_a_screened_and_admitted_smart_cohort_wallet_carries_smart_money(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(5)
    _feed_wallet(conn, w, tags=("smart_degen", "gmgn"))
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.02))

    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == [w] and report.smart_tags_written == 1
    entry = tracker.get_entry(BSC, w, conn)
    assert entry is not None and entry.status == "active" and entry.tier is tracker.Tier.CANDIDATE

    tags = _tags(conn, w)
    assert _bare(tags) == {WalletTag.SMART_MONEY.value}
    assert vendor_tag("smart_degen") in tags and "smart_degen" not in tags
    assert _derived(conn, w) == [WalletTag.SMART_MONEY.value]
    assert tracker.lane_smart_wallets(BSC, conn) == {w}
    assert _lane_reads_smart(conn, w)
    assert WalletTag.SMART_MONEY in grade._tags_from(tags)  # a bare value from a screened path passes


def test_a_kol_only_wallet_is_admitted_and_still_carries_no_bare_word(tmp_db):
    conn = tmp_db
    w = _evm(6)
    _feed_wallet(conn, w, tags=("kol", "bluechip_owner", "gmgn"))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == [w] and report.smart_tags_written == 0
    tags = _tags(conn, w)
    assert not _bare(tags) and tags == {vendor_tag("kol"), vendor_tag("bluechip_owner"), vendor_tag("gmgn")}
    evidence = grade.build_evidence(w, BSC, conn)
    assert not any(f.name == "reputation" for f in grade.components_for(evidence)[0])
    assert naming.infer_archetype(evidence) is not Archetype.KOL
    assert tracker.lane_smart_wallets(BSC, conn) == set()


# --------------------------------------------------------------------------------------
# (c) eviction removes it
# --------------------------------------------------------------------------------------


def test_eviction_strips_the_bare_tag_and_keeps_the_vendor_record(tmp_db, monkeypatch):
    conn = tmp_db
    w = _evm(7)
    _feed_wallet(conn, w, tags=("smart_degen", "gmgn"))
    tracker.seed_from_cohorts(BSC, conn)
    assert _lane_reads_smart(conn, w)

    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.49))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.removed[0]["address"] == w and report.smart_tags_stripped == 1
    assert tracker.get_entry(BSC, w, conn).status == "removed"
    tags = _tags(conn, w)
    assert not _bare(tags), tags
    assert tags == {vendor_tag("smart_degen"), vendor_tag("gmgn")}
    assert _derived(conn, w) == []
    assert tracker.lane_smart_wallets(BSC, conn) == set()
    assert not _lane_reads_smart(conn, w)

    # Idempotent: a third pass strips nothing (there is nothing) and re-adds nothing.
    again = tracker.seed_from_cohorts(BSC, conn)
    assert again.smart_tags_stripped == 0 and again.smart_tags_written == 0
    assert _tags(conn, w) == tags


def test_eviction_strips_even_when_the_feeds_no_longer_call_the_wallet_smart(tmp_db, monkeypatch):
    """``strip`` must not depend on the wallet still being in the smart cohort: the bare
    tag was ours, and a refused wallet loses it whatever GMGN says today."""
    conn = tmp_db
    w = _evm(8)
    _feed_wallet(conn, w, tags=("smart_degen", "gmgn"))
    tracker.seed_from_cohorts(BSC, conn)
    assert _bare(_tags(conn, w)) == {WalletTag.SMART_MONEY.value}

    _relabel(conn, w, tags=("kol", "gmgn"))  # now kol-only in every feed event
    (cohort,) = discover.cohort_wallets(BSC, conn)
    assert not cohort.smart_cohort
    monkeypatch.setattr(tracker, "measure_failure_rate", _measured(0.49))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.removed[0]["address"] == w and report.smart_tags_stripped == 1
    assert not _bare(_tags(conn, w))
    assert not _lane_reads_smart(conn, w)


def test_a_watched_wallet_the_feeds_stop_calling_smart_loses_the_tag_while_staying_watched(tmp_db):
    conn = tmp_db
    w = _evm(9)
    _feed_wallet(conn, w, tags=("smart_degen", "gmgn"))
    tracker.seed_from_cohorts(BSC, conn)
    _relabel(conn, w, tags=("kol", "gmgn"))
    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.already_watched == [w] and report.smart_tags_stripped == 1
    assert tracker.get_entry(BSC, w, conn).status == "active"
    assert not _bare(_tags(conn, w)) and vendor_tag("kol") in _tags(conn, w)
    assert not _lane_reads_smart(conn, w)


# --------------------------------------------------------------------------------------
# dry run and the no-write mode see the labels the same way the live row would carry them
# --------------------------------------------------------------------------------------


def test_a_dry_run_folds_the_labels_through_the_same_namespace(tmp_db, monkeypatch):
    conn = tmp_db
    good, bad = _evm(10), _evm(11)
    _feed_wallet(conn, good, tags=("smart_degen", "kol", "smart_money", "pump_smart"))
    _feed_wallet(conn, bad, tags=("smart_degen", "wash_trader"))
    real_screen = tracker.screen_wallet
    screens: dict[str, tracker.Screen] = {}

    def _spy(chain, address, conn_=None, **kw):
        screens[address] = real_screen(chain, address, conn_, **kw)
        return screens[address]

    monkeypatch.setattr(tracker, "screen_wallet", _spy)
    report = tracker.seed_from_cohorts(BSC, conn, dry_run=True)
    assert report.admitted == [good]
    assert report.refused[0]["address"] == bad and report.refused[0]["codes"] == ["quarantine_tags"]
    # The fold put the quarantine label on the screen and none of the positive ones.
    assert WalletTag.WASH_TRADER in screens[bad].tags
    assert not (set(screens[good].tags) & (set(grade.POSITIVE_REPUTATION_TAGS) | lanes.SMART_TAGS))
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0


def test_without_the_wallet_write_the_screen_still_sees_a_quarantine_label(tmp_db):
    conn = tmp_db
    w = _evm(12)
    _feed_wallet(conn, w, tags=("smart_degen", "sandwich_bot"))
    report = tracker.seed_from_cohorts(BSC, conn, write_wallet_tags=False)
    assert report.refused[0]["address"] == w and report.refused[0]["codes"] == ["quarantine_tags"]
    assert tracker.get_entry(BSC, w, conn) is None
    assert fetch_one(conn, "SELECT COUNT(*) AS n FROM wallets")["n"] == 0


# --------------------------------------------------------------------------------------
# rows the pre-namespace code wrote are migrated once; nothing else's bare tag is touched
# --------------------------------------------------------------------------------------


def test_bare_labels_left_by_the_previous_code_are_moved_into_the_namespace_once(tmp_db):
    conn = tmp_db
    w = _evm(13)
    _feed_wallet(conn, w, tags=("smart_degen", "gmgn", "kol"))
    # Exactly what round 1 wrote: the labels bare, our derived tag, the record without a
    # prefix marker — plus a bare ``early_buyer`` no pass of ours recorded (someone else's).
    conn.execute(
        "INSERT INTO wallets (chain, address, name, source, tags_json, first_seen_ms, last_seen_ms, "
        " cohort, meta_json) VALUES (?,?,NULL,?,?,?,?,NULL,?)",
        (
            BSC.value, w, "gmgn:cohort:smart_degen",
            jdump(["smart_degen", "gmgn", "kol", "smart_money", "early_buyer"]),
            NOW - 6 * HOUR, NOW,
            jdump({
                "kaiba_derived_tags": ["smart_money"],
                "gmgn": {"feeds": [SMART], "tags": ["smart_degen", "gmgn", "kol"],
                         "tags_basis": EvidenceBasis.PROVIDER_REPORTED.value, "name": None,
                         "feed_rows": 14, "gathered_ms": NOW},
            }),
        ),
    )
    conn.commit()
    assert _lane_reads_smart(conn, w)  # the leak as it stood: kol bare, smart_money bare, unscreened

    report = tracker.seed_from_cohorts(BSC, conn)
    assert report.admitted == [w]
    tags = _tags(conn, w)
    assert _bare(tags) == {"smart_money", "early_buyer"}, tags  # ours (re-admitted) and theirs
    assert tags - _bare(tags) == {vendor_tag("smart_degen"), vendor_tag("gmgn"), vendor_tag("kol")}
    assert jload(_row(conn, w)["meta_json"], {})["gmgn"]["tag_prefix"] == GMGN_TAG_PREFIX

    # Migrated once. A bare tag someone writes afterwards that happens to match a vendor
    # label is not ours and stays: the marker keeps this path off it.
    conn.execute(
        "UPDATE wallets SET tags_json=? WHERE chain=? AND address=?",
        (jdump(sorted(tags | {"kol"})), BSC.value, w),
    )
    conn.commit()
    tracker.seed_from_cohorts(BSC, conn)
    assert "kol" in _tags(conn, w)
    assert _tags(conn, w) == tags | {"kol"}


def test_seeding_twice_writes_the_same_namespaced_row(tmp_db):
    conn = tmp_db
    w = _evm(14)
    _feed_wallet(conn, w, tags=LOADED)
    tracker.seed_from_cohorts(BSC, conn)
    first = _row(conn, w)
    tracker.seed_from_cohorts(BSC, conn)
    second = _row(conn, w)
    assert jload(first["tags_json"], []) == jload(second["tags_json"], [])
    assert len(jload(second["tags_json"], [])) == len(LOADED) + 1  # every label once, smart_money once
    assert _derived(conn, w) == [WalletTag.SMART_MONEY.value]
