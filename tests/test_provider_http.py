"""The shared provider base. Three properties here are load-bearing for everything else.

1. A dead provider yields no data and an `UNAVAILABLE` receipt, never a zero.
2. Nothing it writes ever contains a credential.
3. A cached value reports the age it actually has.

The second and third were both broken when this file was first written, and both were
found by other people reading the code rather than by a test. Hence this file.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from kaiba.core.schemas import EvidenceBasis, now_ms
from kaiba.providers import _http

# ----------------------------------------------------------------- failure is not zero


def test_an_unreachable_provider_returns_no_data_and_says_so(tmp_db):
    got = _http.get_json("t", "probe.x", "http://127.0.0.1:1/nope", timeout_s=0.3, conn=tmp_db)
    assert got.data is None
    assert got.ok is False
    assert got.receipt.basis is EvidenceBasis.UNAVAILABLE


def test_a_failure_is_falsy_so_it_cannot_be_mistaken_for_a_result(tmp_db):
    assert not _http.get_json("t", "probe.x", "http://127.0.0.1:1/x", timeout_s=0.3, conn=tmp_db)


def test_a_failure_never_raises(tmp_db):
    """A provider outage must not be able to stop a trading loop."""
    _http.get_json("t", "probe.x", "not-even-a-url", timeout_s=0.3, conn=tmp_db)


def test_a_failure_is_recorded_as_a_provider_error(tmp_db):
    from kaiba.core import events as ev
    from kaiba.core.schemas import EventKind

    _http.get_json("t", "probe.x", "http://127.0.0.1:1/x", timeout_s=0.3, conn=tmp_db)
    assert EventKind.PROVIDER_ERROR.value in [e.kind for e in ev.recent(conn=tmp_db)]


# --------------------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "text",
    [
        "401 Unauthorized for url 'https://api.helius.xyz/v0/x?api-key=SUPER-SECRET-123456'",
        "GET https://x.io/a?foo=1&token=SUPER-SECRET-123456 failed",
        "Authorization: Bearer SUPER-SECRET-123456",
        "apikey=SUPER-SECRET-123456",
    ],
)
def test_no_shape_of_credential_survives_redaction(text):
    assert "SUPER-SECRET-123456" not in _http.redact_text(text)
    assert "<redacted>" in _http.redact_text(text)


def test_a_configured_credential_is_struck_out_even_in_an_unexpected_field(monkeypatch):
    """Belt and braces: if the pattern misses the shape, the literal value still goes."""
    from kaiba.core.config import get_settings

    monkeypatch.setenv("HELIUS_API_KEY", "zzz-literal-secret-value-999")
    get_settings.cache_clear()
    try:
        out = _http.redact_text("provider said: weird_field(zzz-literal-secret-value-999)")
        assert "zzz-literal-secret-value-999" not in out
    finally:
        get_settings.cache_clear()


def test_redaction_leaves_ordinary_text_alone():
    msg = "ConnectError: connection refused to 127.0.0.1:8899"
    assert _http.redact_text(msg) == msg


def test_the_receipt_note_from_a_failed_request_is_redacted(tmp_db, monkeypatch):
    """This is the exact leak: httpx puts the whole URL in the exception message."""
    import httpx

    def boom(*a, **k):
        req = httpx.Request("GET", "https://api.x.io/v0/a?api-key=SUPER-SECRET-123456")
        raise httpx.HTTPStatusError("bad", request=req, response=httpx.Response(401, request=req))

    monkeypatch.setattr(httpx, "request", boom)
    got = _http.get_json("t", "probe.x", "https://api.x.io/v0/a", conn=tmp_db)
    assert "SUPER-SECRET-123456" not in (got.receipt.note or "")


def test_the_emitted_event_from_a_failed_request_is_redacted(tmp_db, monkeypatch):
    import httpx

    from kaiba.core import events as ev

    def boom(*a, **k):
        req = httpx.Request("GET", "https://api.x.io/v0/a?api-key=SUPER-SECRET-123456")
        raise httpx.HTTPStatusError("bad", request=req, response=httpx.Response(401, request=req))

    monkeypatch.setattr(httpx, "request", boom)
    _http.get_json("t", "probe.x", "https://api.x.io/v0/a", conn=tmp_db)
    blob = json.dumps([e.model_dump() for e in ev.recent(conn=tmp_db)], default=str)
    assert "SUPER-SECRET-123456" not in blob


def test_secret_named_params_are_redacted_in_a_mapping():
    out = _http.redact({"api_key": "SUPER-SECRET-123456", "chain": "sol"})
    assert out == {"api_key": "<redacted>", "chain": "sol"}


# ------------------------------------------------------------------------------- cache


def test_a_cached_hit_reports_the_age_it_actually_has(tmp_db):
    """A receipt stamped at read time makes every cached price look brand new."""
    _http.cache_write("t", "k", {"v": 1})
    hit = _http.cache_read("t", "k", ttl_s=600)
    assert hit is not None
    data, is_stale, fetched_ms = hit
    assert data == {"v": 1}
    assert is_stale is False
    assert abs(fetched_ms - now_ms()) < 5_000


def test_a_receipt_for_a_cached_value_carries_the_fetch_time_not_the_read_time(tmp_db):
    from pathlib import Path

    old = now_ms() - 120_000
    p = _http.cache_path("t", "GET:https://x.io/a:{}")
    p.parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps({"fetched_ms": old, "data": {"v": 2}}), encoding="utf-8")

    got = _http.request_json("t", "probe.x", "https://x.io/a", ttl_s=600, conn=tmp_db)
    assert got.data == {"v": 2}
    assert got.receipt.basis is EvidenceBasis.CACHED
    assert abs(got.receipt.observed_at_ms - old) < 1_000
    assert got.receipt.age_seconds > 100


def test_an_expired_entry_inside_the_grace_window_is_marked_stale(tmp_db):
    from pathlib import Path

    p = _http.cache_path("t", "k2")
    p.parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(
        json.dumps({"fetched_ms": now_ms() - 60_000, "data": 7}), encoding="utf-8"
    )
    hit = _http.cache_read("t", "k2", ttl_s=10, stale_grace_s=600)
    assert hit is not None and hit[1] is True


def test_an_entry_past_the_grace_window_is_not_served(tmp_db):
    from pathlib import Path

    p = _http.cache_path("t", "k3")
    p.parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps({"fetched_ms": now_ms() - 10**7, "data": 7}), encoding="utf-8")
    assert _http.cache_read("t", "k3", ttl_s=10, stale_grace_s=60) is None


def test_a_corrupt_cache_file_is_a_miss_not_a_crash(tmp_db):
    p = _http.cache_path("t", "k4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert _http.cache_read("t", "k4", ttl_s=600) is None


def test_ttl_zero_never_reads_the_cache(tmp_db):
    _http.cache_write("t", "k5", {"v": 1})
    assert _http.cache_read("t", "k5", ttl_s=0) is None


# ------------------------------------------------------------------------------- money


def test_json_numbers_are_parsed_as_decimal_not_float(tmp_db, monkeypatch):
    """0.1 + 0.2 must not be how a liquidity figure reaches a sizing decision."""
    import httpx

    class Resp:
        status_code = 200
        headers: dict[str, str] = {}
        text = '{"liquidity": 0.1, "price": 1234.5678901234567890}'

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "request", lambda *a, **k: Resp())
    got = _http.get_json("t", "probe.x", "https://x.io/a", conn=tmp_db)
    assert isinstance(got.data["liquidity"], Decimal)
    assert got.data["price"] == Decimal("1234.5678901234567890")


# ------------------------------------------------------------------- limiter waiting


def test_waiting_for_a_slot_lets_a_second_call_through(tmp_db, monkeypatch):
    """Without this, every provider that makes two calls loses the second one."""
    import httpx

    calls = {"n": 0}

    class Resp:
        status_code = 200
        headers: dict[str, str] = {}
        text = '{"ok": 1}'

        def raise_for_status(self):
            calls["n"] += 1

    monkeypatch.setattr(httpx, "request", lambda *a, **k: Resp())
    a = _http.get_json("rugcheck", "report.a", "https://x.io/a", wait_for_slot_s=5, conn=tmp_db)
    b = _http.get_json("rugcheck", "report.b", "https://x.io/b", wait_for_slot_s=5, conn=tmp_db)
    assert a.ok and b.ok


def test_a_slot_that_never_arrives_is_unavailable_not_a_hang(tmp_db, monkeypatch):
    from kaiba.core import limiter

    def never(*a, **k):
        raise limiter.RateLimited("t", "no capacity", 60)

    monkeypatch.setattr(limiter, "wait_for", never)
    got = _http.get_json("t", "probe.x", "https://x.io/a", wait_for_slot_s=1, conn=tmp_db)
    assert got.ok is False
    assert got.receipt.basis is EvidenceBasis.UNAVAILABLE


def test_a_zero_ttl_never_serves_a_freshly_written_entry(tmp_db):
    """Written and read in the same millisecond, age is exactly 0 and `0 <= 0` is true."""
    _http.cache_write("t", "same-ms", {"v": 1})
    assert _http.cache_read("t", "same-ms", ttl_s=0) is None
    assert _http.cache_read("t", "same-ms", ttl_s=0, stale_grace_s=0) is None


# ---------------------------------------- the credential must not reach durable storage
#
# Three separate routes carry an httpx error message into a table: Receipt.note, the
# events bus, and the limiter's provider_calls/provider_family_bans rows. A fix to one is
# not a fix to the others, so each is asserted on its own.

KEY = "SUPER-SECRET-KEY-123456"
KEYED_URL = f"https://api.example.test/v1/thing?api-key={KEY}"


def _raise_401(*a, **k):
    import httpx

    req = httpx.Request("GET", KEYED_URL)
    raise httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{KEYED_URL}'",
        request=req,
        response=httpx.Response(401, request=req),
    )


def test_a_401_on_a_keyed_url_leaks_into_no_table(tmp_db, monkeypatch):
    """Reproduces the confirmed leak: the key reached events.payload and provider_calls."""
    import httpx

    monkeypatch.setattr(httpx, "request", _raise_401)
    got = _http.get_json("helius", "rpc.thing", KEYED_URL, conn=tmp_db)
    assert got.ok is False

    events = tmp_db.execute("SELECT payload FROM events").fetchall()
    assert events, "no event recorded; the test would pass vacuously"
    assert not any(KEY in r["payload"] for r in events)

    calls = tmp_db.execute("SELECT detail FROM provider_calls").fetchall()
    assert calls, "no provider_call recorded; the test would pass vacuously"
    assert not any(KEY in (r["detail"] or "") for r in calls)

    assert KEY not in (got.receipt.note or "")


def test_the_whole_database_holds_no_copy_of_the_key(tmp_db, monkeypatch):
    """Belt and braces: sweep every text column rather than the two we thought of."""
    import httpx

    monkeypatch.setattr(httpx, "request", _raise_401)
    _http.get_json("helius", "rpc.thing", KEYED_URL, conn=tmp_db)

    tables = [
        r["name"]
        for r in tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        cols = [r["name"] for r in tmp_db.execute(f"PRAGMA table_info({table})")]
        for col in cols:
            hits = tmp_db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE CAST({col} AS TEXT) LIKE ?",
                (f"%{KEY}%",),
            ).fetchone()["n"]
            assert hits == 0, f"credential found in {table}.{col}"


def test_a_rate_limit_ban_reason_is_redacted_too(tmp_db):
    """provider_family_bans.reason takes the same detail string as provider_calls."""
    from kaiba.core import limiter

    limiter.release(
        "helius", "rpc.thing", status="rate_limited",
        detail=f"HTTPStatusError: 429 for url '{KEYED_URL}'", conn=tmp_db,
    )
    rows = tmp_db.execute("SELECT reason FROM provider_family_bans").fetchall()
    assert rows
    assert not any(KEY in (r["reason"] or "") for r in rows)


def test_the_limiter_redacts_a_detail_its_caller_supplied(tmp_db):
    """A caller passing its own detail must not be able to leak either."""
    from kaiba.core import limiter

    limiter.release("helius", "rpc.thing", status="error", detail=f"boom {KEYED_URL}", conn=tmp_db)
    row = tmp_db.execute("SELECT detail FROM provider_calls ORDER BY id DESC LIMIT 1").fetchone()
    assert KEY not in (row["detail"] or "")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.helius.xyz/v0/x?api-key=SUPER-SECRET-KEY-123456",
        "https://api.etherscan.io/api?module=x&apikey=SUPER-SECRET-KEY-123456",
        "https://eth-mainnet.g.alchemy.com/v2/SUPER-SECRET-KEY-123456",
        "https://api.telegram.org/bot123456789:SUPER-SECRET-KEY-123456/getMe",
    ],
)
def test_every_real_credential_bearing_url_shape_is_covered(url):
    """The four shapes actually used by providers in this tree."""
    from kaiba.core.redact import redact_text

    assert KEY not in redact_text(f"Client error '401 Unauthorized' for url '{url}'")
