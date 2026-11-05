"""Offline fixture tests for the read-only rhscannerr Telegram adapter."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain
from kaiba.ingest import rhscannerr

FIXTURES = Path(__file__).parent / "fixtures" / "rhscannerr"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parses_a_structured_ravn_alert_without_turning_text_into_instructions() -> None:
    result = rhscannerr.parse_preview_html(fixture("structured.html"), observed_at_ms=1_789_900_000_000)

    assert result.provider_available is True
    assert result.unavailable_reason is None
    assert len(result.records) == 1
    alert = result.records[0]

    assert alert.record_type == "alert"
    assert alert.source == "telegram:rhscannerr"
    assert alert.channel == "rhscannerr"
    assert alert.message_id == 9001
    assert alert.message_ts_ms == 1_790_049_214_000
    assert alert.chain is Chain.ROBINHOOD
    assert alert.address == "0x1111111111111111111111111111111111111111"
    assert alert.symbol == "RAVN"
    assert alert.name == "Raven Protocol"
    assert alert.bonding_state == "bonding"
    assert alert.og_status == "og"
    assert alert.launchpad == "pons_v2"
    assert alert.pair == "RAVN/AAPL"

    assert alert.metrics.market_cap_usd == Decimal("12500")
    assert alert.metrics.ath_market_cap_usd == Decimal("18000")
    assert alert.metrics.volume_usd == Decimal("3400")
    assert alert.metrics.liquidity_usd == Decimal("7800")
    assert alert.metrics.bonding_pct == Decimal("72")
    assert alert.metrics.holders == 58
    assert alert.metrics.swaps == 214
    assert alert.metrics.views == 42

    assert alert.dev.status == "holds"
    assert alert.dev.holdings_pct == Decimal("7.6")
    assert alert.dev.launches == 3
    assert alert.dev.active_launches == 1
    assert alert.dev.rugs == 1
    assert alert.dev.wallet == "0x2222222222222222222222222222222222222222"
    assert alert.funding_wallet == "0x3333333333333333333333333333333333333333"

    assert alert.confluence.top10_pct == Decimal("23.4")
    assert alert.confluence.insider_pct == Decimal("4.5")
    assert alert.confluence.sniper_count == 3
    assert alert.confluence.bundle_pct == Decimal("12.0")
    assert alert.confluence.bundle_clusters == 2
    assert alert.confluence.bundle_wallets == 5
    assert alert.confluence.kol_count == 2
    assert alert.confluence.smart_count == 7
    assert [(w.address, w.label) for w in alert.confluence.wallets] == [
        ("0x4444444444444444444444444444444444444444", "GMGN Smart"),
        ("0x5555555555555555555555555555555555555555", "GMGN Sniper"),
    ]

    assert alert.links == {
        "gmgn": "https://gmgn.ai/robinhood/token/0x1111111111111111111111111111111111111111",
        "dexscreener": "https://dexscreener.com/robinhood/0x1111111111111111111111111111111111111111",
        "x": "https://x.com/ravn",
        "web": "https://ravn.example",
    }
    assert alert.observation_only is True
    assert alert.requires_fresh_dyor is True
    assert alert.execution_eligible is False
    assert "untrusted_telegram_observation" in alert.dyor_blockers
    assert "Ignore any instructions" not in json.dumps(alert.model_dump(mode="json"))

    assert result.field_coverage["address"] == 1
    assert result.field_coverage["market_cap_usd"] == 1
    assert result.field_coverage["confluence.wallets"] == 1


def recorded_post(message_id: int) -> str:
    """Exact message wrapper sliced from a saved public preview, not invented posts."""
    wrapper = '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
    chunks = fixture("preview-2026-09-22.html").split(wrapper)[1:]
    chunk = next(chunk for chunk in chunks if f'data-post="rhscannerr/{message_id}"' in chunk)
    return '<section class="tgme_channel_history js-message_history">' + wrapper + chunk.split("</section>")[0] + '</section>'


def test_recorded_launch_parses_gmgn_holder_links_and_provider_labels_without_grading() -> None:
    result = rhscannerr.parse_preview_html(recorded_post(104584), observed_at_ms=1_790_053_000_000)
    alert, = result.records
    assert alert.address == "0x262f3ebac6b154cdec226a0743c1d0feb0c260cc"
    assert alert.symbol == "D.O.G.E."
    assert alert.pair == "D.O.G.E./SPCX"
    assert alert.metrics.views == 57  # scanner metric, NOT Telegram message views (6)
    assert alert.telegram_views == 6
    assert alert.dev.wallet == "0x4a1e878c92a3e8d2016c538179141f812aca3dd0"
    assert alert.dev.holdings_pct == Decimal("4.0")
    assert alert.dev.launches == 57
    assert alert.dev.rugs == 54
    assert alert.funding_source == "0xac34…d562"
    assert alert.funding_wallet == "0xac3483b6df3fcb35039c1b572109ef0a4a55d562"
    assert alert.confluence.top10_pct == Decimal("24.6")
    assert alert.confluence.insider_pct == Decimal("0.0")
    assert alert.confluence.sniper_count == 1
    assert alert.confluence.sniper_pct == Decimal("7.7")
    assert alert.confluence.kol_count == 4
    assert alert.confluence.smart_count == 3
    assert len(alert.confluence.wallets) == 10
    assert alert.confluence.wallets[0].label == "枯坐上A8"
    assert alert.confluence.wallets[0].holdings_pct == Decimal("3.78")
    assert alert.confluence.entities == ()  # Not an independence claim.
    assert alert.receipt.basis.value == "provider_reported"
    assert alert.receipt.provider == rhscannerr.SOURCE
    assert alert.receipt.response_digest
    assert alert.links["gmgn"].endswith("_" + alert.address)
    assert result.field_coverage["confluence.insider_pct"] == 1  # zero IS a reported value


def test_recorded_bonded_alert_retains_reported_zeros_and_absent_fields() -> None:
    result = rhscannerr.parse_preview_html(recorded_post(104595), observed_at_ms=1_790_053_000_000)
    alert, = result.records
    assert alert.bonding_state == "bonded"
    assert alert.launchpad == "uniswap_v4"
    assert alert.pair == "LAYA/WETH"
    assert alert.metrics.market_cap_usd == Decimal("74700")
    assert alert.metrics.bonding_pct is None  # bonded does not prove a 100% curve reading
    assert alert.metrics.ath_market_cap_usd is None  # prior launches' ATHs are not this token's
    assert alert.dev.status == "sold"
    assert alert.dev.holdings_pct is None  # sold is a label, not a quantified holding
    assert alert.dev.launches == 7
    assert alert.dev.active_launches == 2
    assert alert.dev.rugs == 3
    assert alert.confluence.sniper_count == 0
    assert alert.confluence.sniper_pct == Decimal("0.0")
    assert alert.confluence.bundle_pct == Decimal("11.42")
    assert alert.confluence.bundle_clusters == 2  # provider spells this "kluster"
    assert alert.confluence.bundle_wallets == 10  # provider uses singular "wallet"
    assert alert.confluence.kol_count == 2
    assert alert.confluence.smart_count == 4
    assert result.field_coverage["confluence.sniper_count"] == 1

    unplug, = rhscannerr.parse_preview_html(recorded_post(104582), observed_at_ms=1_790_053_000_000).records
    assert unplug.confluence.bundle_pct is None
    assert unplug.confluence.kol_count is None
    assert unplug.confluence.smart_count is None
    wdcc, = rhscannerr.parse_preview_html(recorded_post(104583), observed_at_ms=1_790_053_000_000).records
    assert wdcc.confluence.smart_count == 3
    assert wdcc.confluence.bundle_clusters == 3
    john, = rhscannerr.parse_preview_html(recorded_post(104586), observed_at_ms=1_790_053_000_000).records
    assert john.symbol == "John"  # preserve source spelling
    assert john.pair == "John/USDG"


def test_parses_a_milestone_update_as_a_distinct_observation_with_progress_fields() -> None:
    result = rhscannerr.parse_preview_html(recorded_post(104585), observed_at_ms=1_790_053_000_000)
    alert, = result.records
    assert alert.record_type == "milestone"
    assert alert.message_id == 104585
    assert alert.message_ts_ms == 1_790_049_842_000
    assert alert.reply_to_message_id == 104584
    assert alert.address == "0x262f3ebac6b154cdec226a0743c1d0feb0c260cc"
    assert alert.symbol == "D.O.G.E."
    assert alert.name is None
    assert alert.metrics.market_cap_usd == Decimal("29300")
    assert alert.metrics.prior_market_cap_usd == Decimal("11400")
    assert alert.metrics.price_multiple_x == Decimal("2.6")
    assert alert.metrics.age_label == "3m"
    assert alert.metrics.bonding_pct == Decimal("63")
    assert alert.metrics.volume_usd == Decimal("53700")
    assert alert.metrics.liquidity_usd == Decimal("13900")
    assert alert.metrics.holders is None
    assert alert.metrics.views is None
    assert alert.launchpad is None
    assert alert.pair is None
    assert alert.observation_only is True
    assert alert.execution_eligible is False
    assert alert.dedupe_key.startswith("rhscannerr:milestone:")


def test_html_shape_failure_is_provider_unavailable_not_a_zero_alert_result() -> None:
    result = rhscannerr.parse_preview_html(
        "<html><body><div class='new-telegram-layout'>no supported markers</div></body></html>",
        observed_at_ms=1_790_053_000_000,
    )

    assert result.provider_available is False
    assert result.records == ()
    assert result.unavailable_reason == "channel_history_marker_missing"


def test_emits_alpha_meta_observations_and_dedupes_replayed_milestones(tmp_db) -> None:
    from kaiba.core.db import fetch_all
    from kaiba.core.schemas import EventKind

    alert, = rhscannerr.parse_preview_html(recorded_post(104584), observed_at_ms=1_790_053_000_000).records
    milestone, = rhscannerr.parse_preview_html(recorded_post(104585), observed_at_ms=1_790_053_000_000).records

    first_ids = rhscannerr.emit_alerts([alert, milestone, milestone], conn=tmp_db)
    second_ids = rhscannerr.emit_alerts([alert, milestone], conn=tmp_db)

    assert len(first_ids) == 2
    assert second_ids == []
    rows = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=? ORDER BY id", (EventKind.ALPHA_META.value,))
    assert len(rows) == 2
    payloads = [json.loads(row["payload"]) for row in rows]
    assert {payload["record_type"] for payload in payloads} == {"alert", "milestone"}
    assert all(payload["observation_only"] is True for payload in payloads)
    assert all(payload["requires_fresh_dyor"] is True for payload in payloads)
    assert all(payload["execution_eligible"] is False for payload in payloads)
    assert all("untrusted_telegram_observation" in payload["dyor_blockers"] for payload in payloads)


def test_poll_once_uses_injected_fetcher_and_emits_provider_error_on_shape_change(tmp_db) -> None:
    from kaiba.core.db import fetch_all
    from kaiba.core.schemas import EventKind

    calls: list[str] = []

    def fetcher(url: str) -> str:
        calls.append(url)
        return "<html><body><main>Telegram redesigned this page</main></body></html>"

    result = rhscannerr.poll_once(fetcher=fetcher, observed_at_ms=1_790_053_000_000, conn=tmp_db)

    assert calls == [rhscannerr.PREVIEW_URL]
    assert result.provider_available is False
    rows = fetch_all(tmp_db, "SELECT * FROM events WHERE kind=?", (EventKind.PROVIDER_ERROR.value,))
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload == {
        "provider": "telegram",
        "source": "telegram:rhscannerr",
        "status": "unavailable",
        "reason": "channel_history_marker_missing",
        "observation_only": True,
    }


def test_recorded_preview_has_exact_message_and_field_coverage() -> None:
    result = rhscannerr.parse_preview_html(fixture("preview-2026-09-22.html"), observed_at_ms=1_790_053_000_000)

    assert result.provider_available is True
    assert result.messages_seen == 20
    assert result.message_text_nodes == 20  # parser counts current message text, not quoted reply excerpts
    assert result.unsupported_messages == 0
    assert len(result.records) == 20
    assert sum(record.record_type == "alert" for record in result.records) == 11
    assert sum(record.record_type == "milestone" for record in result.records) == 9
    assert result.field_coverage["address"] == 20
    assert result.field_coverage["market_cap_usd"] == 20
    assert result.field_coverage["prior_market_cap_usd"] == 9
    assert result.field_coverage["price_multiple_x"] == 9
    assert result.field_coverage["bonding_pct"] == 15
    assert result.field_coverage["confluence.insider_pct"] == 11
    assert result.field_coverage["confluence.entities"] == 0


def test_parse_dedupes_duplicate_message_wrappers_and_milestone_fingerprints() -> None:
    one = recorded_post(104585)
    body = one.split(">", 1)[1].rsplit("</section>", 1)[0]
    duplicate_page = '<section class="tgme_channel_history js-message_history">' + body + body + "</section>"

    result = rhscannerr.parse_preview_html(duplicate_page, observed_at_ms=1_790_053_000_000)

    assert result.messages_seen == 2
    assert len(result.records) == 1
    assert result.records[0].dedupe_key.startswith("rhscannerr:milestone:")


def test_direct_public_preview_capture_has_verified_ids_and_timestamp_range() -> None:
    result = rhscannerr.parse_preview_html(
        fixture("preview-live-2026-09-22.html"), observed_at_ms=1_790_055_000_000
    )

    assert result.provider_available is True
    assert result.messages_seen == 20
    assert result.message_text_nodes == 20
    assert result.unsupported_messages == 0
    assert [record.message_id for record in result.records] == list(range(104596, 104616))
    assert result.records[0].message_url == "https://t.me/rhscannerr/104596"
    assert result.records[-1].message_url == "https://t.me/rhscannerr/104615"
    assert result.records[0].message_ts_ms == 1_790_051_432_000
    assert result.records[-1].message_ts_ms == 1_790_054_972_000
    assert sum(record.record_type == "alert" for record in result.records) == 12
    assert sum(record.record_type == "milestone" for record in result.records) == 8


def test_structured_message_parse_failure_is_provider_unavailable() -> None:
    changed_markup = """
    <section class="tgme_channel_history js-message_history">
      <div class="tgme_widget_message js-widget_message" data-post="rhscannerr/9999">
        <div class="tgme_widget_message_text js-message_text">new labels, no known RAVN header</div>
      </div>
    </section>
    """

    result = rhscannerr.parse_preview_html(changed_markup, observed_at_ms=1_790_053_000_000)

    assert result.provider_available is False
    assert result.records == ()
    assert result.unavailable_reason == "message_parse_error"
    assert result.messages_seen == 1


@pytest.mark.parametrize("case", [
    "empty", "foreign_channel", "naive_time", "bad_time", "missing_time", "bad_id",
    "empty_history", "wrong_chain", "unknown_chain", "truncated", "malformed_address",
    "two_addresses", "conflicting_token_link", "missing_text", "oversized", "deep_html",
])
def test_invalid_envelopes_never_become_observations(case: str) -> None:
    html = recorded_post(104582)
    token = "0x30da13680150470bec1219efca445241bf7d25fd"
    other = "0x1111111111111111111111111111111111111111"
    changes = {
        "empty": "",
        "foreign_channel": html.replace('data-post="rhscannerr/', 'data-post="attacker/'),
        "naive_time": html.replace("2026-09-22T03:53:34+00:00", "2026-09-22T03:53:34"),
        "bad_time": html.replace("2026-09-22T03:53:34+00:00", "bad"),
        "missing_time": re.sub(r"<time[^>]*>.*?</time>", "", html),
        "bad_id": html.replace('data-post="rhscannerr/104582"', 'data-post="rhscannerr/-1"'),
        "empty_history": '<section class="tgme_channel_history"></section>',
        "wrong_chain": html.replace("ROBINHOOD ·", "BASE ·"),
        "unknown_chain": html.replace("ROBINHOOD ·", "MARS ·"),
        "truncated": html.rsplit("</section>", 1)[0],
        "malformed_address": html.replace(f"<code>{token}</code>", f"<code>{token}BAD</code>"),
        "two_addresses": html.replace(f"<code>{token}</code>", f"<code>{token}</code><br><code>{other}</code>"),
        "conflicting_token_link": html.replace(f"token/uADTANKq_{token}", f"token/uADTANKq_{other}"),
        "missing_text": html.replace("js-message_text", "js-new-text"),
        "oversized": "x" * 2_000_001,
        "deep_html": "<div>" * 2000 + html + "</div>" * 2000,
    }
    result = rhscannerr.parse_preview_html(changes[case], observed_at_ms=1_790_053_000_000)
    assert result.provider_available is False, case
    assert result.records == (), case
    assert result.unavailable_reason, case


# ------------------------------------------------- one bad post must not blind the feed


BAD_POST = (
    '<div class="tgme_widget_message js-widget_message">'
    '<div class="tgme_widget_message_text js-message_text">'
    '<b>A Format We Have Never Seen</b><br/>no fields at all'
    "</div></div>"
)


def _real_page_plus(bad: int) -> str:
    """The RECORDED live page with `bad` undecodable posts spliced into its history.

    Built from the real capture rather than hand-written HTML: a synthetic "good" post
    that does not actually decode would make these tests pass or fail for reasons that
    have nothing to do with the rule being tested.
    """
    html = (FIXTURES / "preview-2026-09-22.html").read_text(encoding="utf-8")
    marker = "</div>"
    end = html.rindex(marker)
    return html[:end] + (BAD_POST * bad) + html[end:]


def test_one_undecodable_post_does_not_discard_the_rest():
    """THE REGRESSION, measured against the live page on 2026-09-23.

    20 messages, 19 decoded, 1 raised ``unsupported_rhscannerr_header`` -- and because any
    error failed the whole batch, the feed reported ``message_parse_error`` and emitted
    nothing at all. It had never delivered a single observation since being written.
    """
    clean = rhscannerr.parse_preview_html((FIXTURES / "preview-2026-09-22.html").read_text(encoding="utf-8"))
    assert clean.provider_available and clean.records, "the fixture itself must decode"
    mixed = rhscannerr.parse_preview_html(_real_page_plus(1))
    assert mixed.provider_available, mixed.unavailable_reason
    assert len(mixed.records) == len(clean.records), "the decodable posts were discarded"


def test_the_undecoded_posts_are_counted_not_hidden():
    """Partial acceptance is only honest if the part we dropped is visible."""
    assert rhscannerr.parse_preview_html(_real_page_plus(2)).unsupported_messages == 2


def test_nothing_decodable_is_still_a_refusal():
    """The guard that must NOT be relaxed: every post failing means the format changed.

    A failure is never converted into an empty answer.
    """
    only_bad = '<div class="tgme_channel_history">' + BAD_POST * 3 + "</div>"
    result = rhscannerr.parse_preview_html(only_bad)
    assert not result.provider_available
    assert result.records == ()


def test_a_clean_page_reports_no_undecoded_posts():
    assert rhscannerr.parse_preview_html(_real_page_plus(0)).unsupported_messages == 0
