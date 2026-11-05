"""kaiba/ingest/wallet_track.py: the graded sol wallets reach the tape, and their confluence is flagged.

What these guard:

* only S/A/B wallets of the high-tier export are tracked; the rest of the GMGN follow list never
  reaches the tape, and a wallet the follow list covers is not polled on its own;
* follow-feed and activity rows land in ``swaps`` through the same writer as the smart-money feed,
  once (the cursor and the table's unique key), with seconds turned into milliseconds;
* a confluence is two or more TRACKED wallets buying one token inside the window -- not a quote
  asset, not an untracked wallet, not a buy that has left the window -- and it alerts once per
  level, saying plainly that it is not a buy signal.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from kaiba.core.db import fetch_all
from kaiba.core.schemas import SOL_NATIVE_MINT, Chain, now_ms
from kaiba.ingest import gmgn_feeds, wallet_track

# Public example addresses (Solana docs / well-known programs), never the owner's or the agent's.
W_A = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
W_B = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
W_C = "HN7cABqLq46Es1jh92dQQisAq662SmxELLLsHHe4YWrH"
W_OTHER = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
TOKEN = "EGLpQAhQVB3gN9ZMsgjCBjabf42sPQ3K7T6DRAvy8pr"
TOKEN2 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.fixture
def db(tmp_db, monkeypatch):
    @contextlib.contextmanager
    def passthrough(*_args, **_kwargs):
        yield

    monkeypatch.setattr(gmgn_feeds, "guarded", passthrough)
    from kaiba.execution import fills

    def forbidden(*_args, **_kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("the ingest path must never read decimals from the chain")

    monkeypatch.setattr(fills, "_fetch_decimals", forbidden)
    return tmp_db


def export(tmp_path, tiers: dict[str, str | None]):
    folder = tmp_path / "claude-runners" / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [{"address": w, "name": f"{t or 'C'} $20k w50% r40% 2x3 30m",
             "groups": ["default"] + ([f"kaiba-hightier-{t}"] if t else [])} for w, t in tiers.items()]
    (folder / "kaiba-solana-hightier.txt").write_text(json.dumps(rows), encoding="utf-8")
    return tmp_path


def follow_row(wallet: str, token: str, side: str, ts_s: int, tx: str) -> dict:
    """The MEASURED ``track follow-wallet`` row shape (2026-10-28), trimmed to what is read."""
    return {"id": tx, "chain": "sol", "transaction_hash": tx, "maker": wallet, "base_amount": 1000.0,
            "quote_amount": 0.5, "amount_usd": 95.0, "price_usd": 0.000095, "timestamp": ts_s, "side": side,
            "base_address": token, "quote_address": SOL_NATIVE_MINT, "quote_symbol": "SOL",
            "launchpad": "pump", "base_token": {"symbol": "KAIBA", "address": token},
            "maker_info": {"address": wallet, "tags": ["smart_degen"], "name": ""}}


def activity(wallet: str, token: str, event: str, ts_s: int, tx: str) -> dict:
    """The MEASURED ``portfolio activity`` row shape (2026-10-28)."""
    return {"wallet": wallet, "chain": "sol", "tx_hash": tx, "timestamp": ts_s, "event_type": event,
            "token": {"address": token, "symbol": "KAIBA", "total_supply": "1000000000"},
            "token_amount": "21485805.27", "quote_amount": "0.62", "cost_usd": "62.69", "price_usd": "0.0000029",
            "quote_address": SOL_NATIVE_MINT, "launchpad": "pump", "launchpad_platform": "Pump.fun"}


def swap(conn, wallet, token, side, ts_ms, tx, usd="100"):
    conn.execute("INSERT INTO swaps (chain, tx, ts_ms, wallet, token, side, amount_token, usd_value, source) "
                 "VALUES ('sol', ?, ?, ?, ?, ?, '1000', ?, 'test')", (tx, ts_ms, wallet, token, side, usd))


def rows_of(conn, source):
    return fetch_all(conn, "SELECT wallet, token, side, ts_ms FROM swaps WHERE source = ? ORDER BY ts_ms", (source,))


def run(conn, data_dir, **kw):
    kw.setdefault("fetch_follow", lambda _chain: [])
    kw.setdefault("fetch_activity", lambda _w, _chain: [])
    kw.setdefault("send", lambda _text: True)
    return wallet_track.run(conn, Chain.SOL, data_dir=data_dir, **kw)


def test_only_s_a_b_wallets_of_the_export_are_tracked(tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: None})
    assert wallet_track.tracked_wallets(Chain.SOL, data) == {
        W_A: {"tier": "A", "name": "A $20k w50% r40% 2x3 30m"}, W_B: {"tier": "B", "name": "B $20k w50% r40% 2x3 30m"}}
    assert wallet_track.tracked_wallets(Chain.SOL, tmp_path / "nowhere") == {}, "no export, nothing tracked"
    assert wallet_track.tracked_wallets(Chain.BSC, data) == {}


def test_follow_feed_keeps_tracked_wallets_only_and_writes_each_trade_once(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    now_s = now_ms() // 1000
    page = [follow_row(W_A, TOKEN, "buy", now_s - 5, "tx1"), follow_row(W_OTHER, TOKEN, "buy", now_s - 4, "tx2"),
            follow_row(W_B, TOKEN2, "sell", now_s - 3, "tx3")]
    out = run(db, data, fetch_follow=lambda _c: page)
    got = rows_of(db, "gmgn:follow")
    assert [(r["wallet"], r["side"]) for r in got] == [(W_A, "buy"), (W_B, "sell")], "the rest of the follow list is dropped"
    assert got[0]["ts_ms"] == (now_s - 5) * 1000, "seconds become milliseconds"
    assert out["follow_rows"] == 2 and out["written"] == 2
    again = run(db, data, fetch_follow=lambda _c: page)
    assert again["written"] == 0 and len(rows_of(db, "gmgn:follow")) == 2, "a replayed page writes nothing"


def test_the_activity_poll_covers_tracked_wallets_the_follow_list_does_not(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: "B"})
    now_s = now_ms() // 1000
    asked = []

    def fetch_activity(wallet, _chain):
        asked.append(wallet)
        return [activity(wallet, TOKEN, "buy", now_s - 60, f"act-{wallet[:4]}"),
                {**activity(wallet, TOKEN, "transferIn", now_s - 50, f"xfer-{wallet[:4]}")},
                activity(W_OTHER, TOKEN, "buy", now_s - 40, "act-other")]

    out = run(db, data, fetch_follow=lambda _c: [follow_row(W_A, TOKEN2, "buy", now_s - 2, "tx9")],
              fetch_activity=fetch_activity, poll_per_run=5)
    assert sorted(asked) == sorted([W_B, W_C]), "a wallet the follow list covers is not polled on its own"
    got = rows_of(db, "gmgn:tracked")
    assert sorted(r["wallet"] for r in got) == sorted([W_B, W_C]), "transfers and other wallets' rows are not trades of it"
    assert out["polled"] == 2 and out["followed"] == 1


def test_polling_rotates_least_recently_polled_first(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: "B"})
    asked = []
    t0 = now_ms()
    for i in range(3):
        run(db, data, now=t0 + i * 15_000, poll_per_run=1, fetch_activity=lambda w, _c: asked.append(w) or [])
    assert sorted(asked) == sorted([W_A, W_B, W_C]), "three runs of one poll each visit every wallet once"


def test_two_tracked_buyers_alert_once_per_level_and_say_it_is_not_a_buy_signal(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: "B"})
    now = now_ms()
    sent: list[str] = []
    swap(db, W_A, TOKEN, "buy", now - 600_000, "c1", usd="120")
    swap(db, W_B, TOKEN, "buy", now - 300_000, "c2", usd="80")
    swap(db, W_OTHER, TOKEN, "buy", now - 200_000, "c3")                     # not tracked
    swap(db, W_C, TOKEN, "buy", now - 3 * 3_600_000, "c4")                   # outside the window
    for i, w in enumerate((W_A, W_B, W_C)):
        swap(db, w, SOL_NATIVE_MINT, "buy", now - 100_000, f"q{i}")          # a quote asset
    out = run(db, data, now=now, send=sent.append)
    assert out["alerts"] == 1 and len(sent) == 1
    assert "2 tracked wallets bought" in sent[0] and TOKEN in sent[0] and "A ×1 · B ×1" in sent[0]
    assert "not a buy signal" in sent[0].lower() or "Watch, not a buy signal" in sent[0]
    rows = fetch_all(db, "SELECT kind, subject, payload_json FROM alpha_signals WHERE source = ?", (wallet_track.ALERT_SOURCE,))
    assert [(r["kind"], r["subject"]) for r in rows] == [("wallet_confluence", TOKEN)]
    assert json.loads(rows[0]["payload_json"])["usd"] == 200.0

    assert run(db, data, now=now + 15_000, send=sent.append)["alerts"] == 0, "same level, no second alert"
    swap(db, W_C, TOKEN, "buy", now + 20_000, "c5")
    assert run(db, data, now=now + 30_000, send=sent.append)["alerts"] == 1 and "3 tracked wallets" in sent[-1]


def test_a_wallet_that_already_sold_is_named_in_the_alert(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 600_000, "s1")
    swap(db, W_B, TOKEN, "buy", now - 500_000, "s2")
    swap(db, W_A, TOKEN, "sell", now - 100_000, "s3")
    sent: list[str] = []
    run(db, data, now=now, send=sent.append)
    assert "1 already sold" in sent[0]


def test_no_export_means_no_reads_and_no_alerts(db, tmp_path):
    calls = []
    out = wallet_track.run(db, Chain.SOL, data_dir=tmp_path, fetch_follow=lambda _c: calls.append(1) or [],
                           fetch_activity=lambda *_a: calls.append(2) or [], send=lambda _t: calls.append(3) or True)
    assert out["tracked"] == 0 and calls == []


def test_the_ops_job_sends_alerts_through_the_owner_helper_that_reads_notify_env(monkeypatch, tmp_db):
    """MEASURED 2026-10-28 on the box: kaiba-ops has the bot token but no chat id, so
    trade_notify.credentials() alone would drop every alert. The job must pass the scheduler's
    helper, which also reads ~/.config/kaiba/notify.env."""
    from kaiba.ops import scheduler as S

    seen = {}
    monkeypatch.setattr(wallet_track, "run", lambda conn, chain, **kw: seen.update(kw) or {"tracked": 0})
    ctx = S.JobContext(name="wallet_track", conn=tmp_db, params={}, config=None, started_ms=0, deadline_ms=10**15)
    S.job_wallet_track(ctx)
    assert seen["send"] is S._send_owner_telegram


def best_file(data_dir, ranks: dict[str, int], age_h: float = 1, mean: float = 0.05):
    folder = data_dir / "labs" / "wallet"
    folder.mkdir(parents=True, exist_ok=True)
    at = int(now_ms() / 1000 - age_h * 3600)
    (folder / "best.json").write_text(json.dumps({"at": at, "top": [{"wallet": w, "rank": r, "mean": mean} for w, r in ranks.items()]}), encoding="utf-8")


def test_the_reviewed_wallets_are_read_before_the_rest_of_the_rotation(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: "S"})
    best_file(data, {W_C: 1})
    read: list[str] = []
    wallet_track.run(db, data_dir=data, now=now_ms(), fetch_follow=lambda c: [], send=lambda t: True, poll_per_run=1,
                     fetch_activity=lambda w, c: read.append(w) or [])
    assert read == [W_C]


def test_a_reviewed_wallet_only_takes_its_share_of_the_reads(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B", W_C: "S"})
    best_file(data, {W_A: 1, W_B: 2, W_C: 3})
    read: list[str] = []
    wallet_track.run(db, data_dir=data, now=now_ms(), fetch_follow=lambda c: [], send=lambda t: True, poll_per_run=2,
                     fetch_activity=lambda w, c: read.append(w) or [])
    assert len(read) == 2


def test_a_top_wallet_alone_alerts_once_and_says_it_is_tracking_only(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    best_file(data, {W_A: 1})
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 60_000, "t1")
    sent: list[str] = []
    for _ in range(2):
        wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    assert len(sent) == 1
    assert sent[0].startswith("Best wallet #1 (sol) bought") and "Tracking only" in sent[0] and TOKEN in sent[0]


def test_a_lower_rank_alone_is_silent_but_two_of_the_list_together_alert(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    best_file(data, {W_A: 7, W_B: 9})
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 60_000, "t1")
    sent: list[str] = []
    wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    assert sent == []
    swap(db, W_B, TOKEN, "buy", now - 30_000, "t2")
    wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    best_alerts = [t for t in sent if t.startswith("Best-wallet confluence")]
    assert len(best_alerts) == 1 and "(#7 and #9)" in best_alerts[0]


def test_an_old_list_is_ignored_rather_than_trusted(db, tmp_path):
    data = export(tmp_path, {W_A: "A"})
    best_file(data, {W_A: 1}, age_h=49)
    assert wallet_track.best_list(data, now_ms()) == {}
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 60_000, "t1")
    sent: list[str] = []
    wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    assert sent == []


def test_a_broken_list_is_no_list(tmp_path):
    (tmp_path / "labs" / "wallet").mkdir(parents=True)
    (tmp_path / "labs" / "wallet" / "best.json").write_text("{not json", encoding="utf-8")
    assert wallet_track.best_list(tmp_path, now_ms()) == {}



def test_a_token_that_already_alerted_stays_quiet_as_its_first_buyer_leaves_the_window(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    best_file(data, {W_A: 1, W_B: 2})
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 1_790_000, "t1")
    swap(db, W_B, TOKEN, "buy", now - 60_000, "t2")
    sent: list[str] = []
    wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    best_alerts = [t for t in sent if t.startswith("Best")]
    assert len(best_alerts) == 1 and best_alerts[0].startswith("Best-wallet confluence")
    # W_A is now out of the window, so what is left is a new, lower hit with a different key.
    sent.clear()
    wallet_track.run(db, data_dir=data, now=now + 60_000, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    assert [t for t in sent if t.startswith("Best")] == []


def test_a_wallet_a_copier_lost_on_is_not_read_first_and_does_not_alert(db, tmp_path):
    data = export(tmp_path, {W_A: "A", W_B: "B"})
    best_file(data, {W_A: 1, W_B: 2}, mean=-0.02)
    assert wallet_track.best_list(data, now_ms()) == {}
    now = now_ms()
    swap(db, W_A, TOKEN, "buy", now - 60_000, "t1")
    swap(db, W_B, TOKEN, "buy", now - 30_000, "t2")
    sent: list[str] = []
    out = wallet_track.run(db, data_dir=data, now=now, fetch_follow=lambda c: [], fetch_activity=lambda w, c: [], send=lambda t: sent.append(t) or True)
    assert [t for t in sent if t.startswith("Best")] == [] and out["best_watched"] == 0
