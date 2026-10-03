"""Offline airdrop adapter regressions; generated bodies are not live programme evidence."""
from __future__ import annotations

import gzip
import json

import httpx
import pytest

from kaiba.core import events
from kaiba.core.schemas import EventKind
from kaiba.hunters import airdrops
from kaiba.hunters.ev import HunterConfig, score_opportunity


@pytest.fixture
def serve(monkeypatch):
    """Exercise real httpx response streaming/decoding without opening a socket."""
    clients = []

    def install(body, *, headers=None, status=200):
        def handler(request):
            if isinstance(body, httpx.SyncByteStream):
                return httpx.Response(status, stream=body, headers=headers, request=request)
            return httpx.Response(status, content=body, headers=headers, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        monkeypatch.setattr(airdrops.httpx, "get", client.get)
        monkeypatch.setattr(airdrops.httpx, "stream", client.stream)

    yield install
    for client in clients:
        client.close()


def errors(conn):
    return events.recent(limit=50, kinds=[EventKind.PROVIDER_ERROR.value], conn=conn)


def test_complete_large_protocols_json_is_parsed_as_discovery_only(serve, tmp_db):
    # Synthetic 8.9MB JSON: the eligible row sits beyond the former 4M-char slice.
    payload = [
        {"name": "Padding", "symbol": "EXISTING", "description": "x" * 8_900_000},
        {"name": "Synthetic Lead", "symbol": "-", "tvl": 2_000_000, "chain": "Solana"},
    ]
    serve(json.dumps(payload).encode(), headers={"content-type": "application/json"})
    leads = airdrops.from_defillama(conn=tmp_db)
    assert [lead.name for lead in leads] == ["Synthetic Lead"]
    lead = leads[0]
    assert lead.confirmed_token is False
    assert lead.expected_value_usd is None
    assert lead.official_url is None and lead.deadline_ms is None
    assert score_opportunity(lead, HunterConfig()).ev_usd < 25
    assert errors(tmp_db) == []


@pytest.mark.parametrize("text", [
    "No airdrop confirmed today.",
    "There is not a confirmed token; earn points instead.",
    "Token confirmed? Not yet.",
    "The token confirmed claim is false.",
    "Rumour: airdrop confirmed for next week.",
    "No token confirmed. Points program is live.",
])
def test_negated_or_uncertain_confirmation_is_not_a_token_promise(text):
    lead = airdrops._evidence_from_text(
        name="Synthetic Negation", text=text, url=None, source="offline-regression",
    )
    assert lead is not None
    assert lead.confirmed_token is False
    assert lead.meta["excerpt"] == text


def test_positive_confirmation_is_not_erased_by_an_unrelated_sentence():
    lead = airdrops._evidence_from_text(
        name="Synthetic Positive", text="No KYC required. Token confirmed for October.",
        url=None, source="offline-regression",
    )
    assert lead.confirmed_token is True


def test_alphadrops_negated_status_is_not_confirmed(tmp_db):
    page = '<script id="__NEXT_DATA__">' + json.dumps({
        "programs": [{"name": "Synthetic Points", "status": "No airdrop confirmed"}],
    }) + '</script>'
    leads = airdrops.from_alphadrops(page, conn=tmp_db)
    assert len(leads) == 1 and leads[0].points_program
    assert leads[0].confirmed_token is False


# Retained minimal markup from the official public points index, captured
# 2026-09-24T11:08:37Z. Full response SHA256:
# e034daa6f8f7fdf7cdd99d4d898b06ee9e74dc860124a9d3ce67691c881feaba
# The second h3 in each real card is a duplicate non-link title; Next Flight scripts
# are not executable parser input. Only h3 programme anchors define this card schema.
ALPHADROPS_CARD_FIXTURE = '''<main>
<h3 class="truncate text-base font-bold transition-colors flex items-center gap-2 text-foreground group-hover:text-orange-500"><a class="hover:underline z-10 before:absolute before:inset-0" href="/airdrops/popdex">PopDEX</a></h3>
<h3>PopDEX</h3>
<h3 class="truncate text-base font-bold transition-colors flex items-center gap-2 text-foreground group-hover:text-orange-500"><a class="hover:underline z-10 before:absolute before:inset-0" href="/airdrops/bulk">Bulk</a></h3>
<h3>Bulk</h3>
</main>'''


def test_current_alphadrops_cards_are_discovery_leads_not_healthy_empty(tmp_db):
    leads = airdrops.from_alphadrops(ALPHADROPS_CARD_FIXTURE, conn=tmp_db)
    assert [lead.name for lead in leads] == ["PopDEX", "Bulk"]
    assert [lead.url for lead in leads] == [
        "https://alphadrops.net/airdrops/popdex", "https://alphadrops.net/airdrops/bulk",
    ]
    assert all(lead.points_program and not lead.confirmed_token for lead in leads)
    assert all(lead.official_url is None and lead.expected_value_usd is None for lead in leads)
    assert all(lead.deadline_ms is None and lead.chain is None for lead in leads)
    assert all(score_opportunity(lead, HunterConfig()).ev_usd < 25 for lead in leads)
    assert errors(tmp_db) == []


@pytest.mark.parametrize("page", [
    "<html><h1>Programmes</h1><div>Unrecognized populated card</div></html>",
    '<script id="__NEXT_DATA__">{"props":{"pageProps":{"other":[]}}}</script>',
    "",
])
def test_alphadrops_unrecognized_page_reports_parse_failure(page, tmp_db):
    assert airdrops.from_alphadrops(page, conn=tmp_db) == []
    assert any("unrecognized points page" in e.payload["detail"] for e in errors(tmp_db))


def test_alphadrops_explicit_empty_programme_list_is_not_a_parse_error(tmp_db):
    page = '<script id="__NEXT_DATA__">' + json.dumps({
        "props": {"pageProps": {"programs": []}},
    }) + '</script>'
    assert airdrops.from_alphadrops(page, conn=tmp_db) == []
    assert errors(tmp_db) == []


def telegram_fragment(title, description, links):
    anchors = " ".join(f'<a href="{url}">link</a>' for url in links)
    return (
        '<div class="tgme_widget_message" data-post="airdrops_io/123">'
        f'<div class="tgme_widget_message_text"><b>{title}</b><br/>{description} {anchors}</div>'
        '<time datetime="2026-09-23T16:24:43+00:00"></time></div>'
    )


def test_telegram_media_link_cannot_displace_programme_link(tmp_db):
    page = telegram_fragment("Synthetic Points S2", "Points program on Solana.", [
        "https://cdn.example/poster.JPG?raw=1", "https://project.example/season-2",
    ])
    leads = airdrops.from_airdrops_io_telegram(page, conn=tmp_db)
    assert len(leads) == 1
    assert leads[0].url == "https://project.example/season-2"
    assert leads[0].official_url is None
    assert leads[0].first_seen_ms == airdrops.parse_iso_ms("2026-09-23T16:24:43+00:00")


def test_media_only_programme_retains_source_receipt_but_no_participation_url(tmp_db):
    page = telegram_fragment("Synthetic Points S2", "Points program announced.", [
        "https://cdn.example/poster.png",
    ])
    leads = airdrops.from_airdrops_io_telegram(page, conn=tmp_db)
    assert len(leads) == 1
    assert leads[0].url is None and leads[0].official_url is None
    assert leads[0].meta["source_url"] == "https://t.me/airdrops_io/123"


@pytest.mark.parametrize(("title", "description"), [
    ("airdrops.io pinned Photo", "New points programs this week."),
    ("Weekly airdrop roundup", "Five points programs you should know."),
    ("Market update", "Bitcoin gains after a volatile week."),
    ("Crypto price news", "An airdrop mentioned in unrelated market coverage."),
])
def test_nonprogramme_telegram_posts_are_excluded(title, description, tmp_db):
    page = telegram_fragment(title, description, ["https://news.example/article"])
    assert airdrops.from_airdrops_io_telegram(page, conn=tmp_db) == []


def test_rss_nonprogramme_items_are_excluded(tmp_db):
    page = (
        '<rss><channel><item><title>Weekly airdrop roundup</title>'
        '<link>https://news.example/roundup</link>'
        '<description>Points programs and market news</description></item></channel></rss>'
    )
    assert airdrops.from_airdropalert_rss(page, conn=tmp_db) == []


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    def close(self):
        self.closed = True


def test_oversized_stream_is_closed_without_parsing_a_valid_json_prefix(serve, tmp_db):
    # The entire response and its first chunk are valid JSON; clipping would silently
    # appear successful. No Content-Length is available, so enforce the streaming cap.
    chunk = airdrops.HTTP_CHUNK_BYTES
    body = ObservedStream([b"[]" + b" " * (chunk - 2)] + [b" " * chunk] * 20)
    serve(body, headers={"content-type": "application/json"})
    assert airdrops.fetch_json("defillama", "protocols.list", "https://fixture.example", conn=tmp_db,
                               max_body_bytes=chunk) is None
    assert body.reads == 2 and body.closed
    failure, = errors(tmp_db)
    assert "body exceeds" in failure.payload["detail"]
    assert failure.payload["http_status"] == 200
    assert failure.payload["received_body_bytes"] == 2 * chunk
    assert failure.payload["max_body_bytes"] == chunk
    receipt = tmp_db.execute("SELECT status, detail FROM provider_calls").fetchone()
    assert receipt["status"] == "error" and "body exceeds" in receipt["detail"]


def test_exact_byte_limit_accepts_complete_utf8_json(serve, tmp_db):
    body = json.dumps({"name": "☃"}, ensure_ascii=False).encode()
    serve(body, headers={"content-type": "application/json; charset=utf-8"})
    assert airdrops.fetch_json("fixture", "json", "https://fixture.example", conn=tmp_db,
                               max_body_bytes=len(body)) == {"name": "☃"}
    assert errors(tmp_db) == []


def test_byte_ceiling_is_not_a_character_ceiling(serve, tmp_db):
    text = json.dumps({"name": "☃"}, ensure_ascii=False)
    serve(text.encode(), headers={"content-type": "application/json; charset=utf-8"})
    assert airdrops.fetch_json("fixture", "json", "https://fixture.example", conn=tmp_db,
                               max_body_bytes=len(text)) is None
    assert "body exceeds" in errors(tmp_db)[0].payload["detail"]


def test_compressed_small_wire_body_still_obeys_decoded_byte_limit(serve, tmp_db):
    body = gzip.compress(b'"' + b"x" * 128_000 + b'"')
    serve(ObservedStream([body]), headers={
        "content-encoding": "gzip", "content-length": str(len(body)),
    })
    assert airdrops.fetch_json("fixture", "json", "https://fixture.example", conn=tmp_db,
                               max_body_bytes=64_000) is None
    assert errors(tmp_db)[0].payload["received_body_bytes"] > 64_000


@pytest.mark.parametrize("limit", [0, -1, True, None])
def test_unbounded_or_invalid_limit_is_refused_without_http(limit, tmp_db, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid limits must be rejected before transport")

    monkeypatch.setattr(airdrops.httpx, "stream", unexpected)
    assert airdrops.fetch_text("fixture", "html", "https://fixture.example", conn=tmp_db,
                               max_body_bytes=limit) is None
    assert "positive integer" in errors(tmp_db)[0].payload["detail"]


@pytest.mark.parametrize(("status", "receipt_status"), [(500, "error"), (429, "rate_limited")])
def test_http_failure_preserves_limiter_and_provider_receipts(serve, tmp_db, status, receipt_status):
    serve(b"unavailable", status=status)
    assert airdrops.fetch_json("fixture", "json", "https://fixture.example", conn=tmp_db) is None
    assert errors(tmp_db)[0].payload["http_status"] == status
    receipt = tmp_db.execute("SELECT status FROM provider_calls").fetchone()
    assert receipt["status"] == receipt_status


@pytest.mark.parametrize("url", [
    "https://cdn.example/image.JPEG?ref=campaign",
    "https://cdn.example/image%2epng#photo",
    "https://cdn.example/video.mp4",
    "javascript:alert(1)", "data:image/png;base64,eA==",
    "https://user@programme.example", "https://t.me/airdrops_io/123",
])
def test_text_evidence_never_uses_media_or_unsafe_links(url):
    lead = airdrops._evidence_from_text(
        name="Synthetic Points", text="Points programme", url=url, source="fixture",
    )
    assert lead is not None and lead.url is None and lead.official_url is None


def test_card_parser_ignores_navigation_scripts_external_links_and_duplicates(tmp_db):
    noise = '''<nav><a href="/airdrops/navigation">Navigation</a></nav>
    <h3><a href="https://external.example/airdrops/phish">External</a></h3>
    <script>self.__next_f.push([1,'<h3><a href="/airdrops/script">Script</a></h3>'])</script>
    <h3><a href="/airdrops/popdex">PopDEX</a></h3>'''
    leads = airdrops.from_alphadrops(ALPHADROPS_CARD_FIXTURE + noise, conn=tmp_db)
    assert [lead.name for lead in leads] == ["PopDEX", "Bulk"]
    assert errors(tmp_db) == []


def test_programme_name_alone_does_not_supply_card_chain_evidence(tmp_db):
    page = '<h3><a href="/airdrops/solana-brand">Solana Brand</a></h3>'
    lead, = airdrops.from_alphadrops(page, conn=tmp_db)
    assert lead.chain is None and lead.chain_hint is None


@pytest.mark.parametrize("source", ["defillama", "alphadrops"])
def test_structured_programme_rows_cannot_supply_media_urls(source, tmp_db):
    row = {"name": "Synthetic Points", "url": "https://cdn.example/logo.png",
           "symbol": "-", "tvl": 2_000_000, "status": "Live"}
    if source == "defillama":
        lead, = airdrops.from_defillama([row], conn=tmp_db)
    else:
        page = '<script id="__NEXT_DATA__">' + json.dumps({"programs": [row]}) + '</script>'
        lead, = airdrops.from_alphadrops(page, conn=tmp_db)
    assert lead.url is None and lead.official_url is None
