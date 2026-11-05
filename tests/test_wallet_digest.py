"""The owner's daily A/B wallet list (kaiba/learning/wallet_digest.py).

Owner 2026-10-21: "send me daily wallet gather, A B tier only, solana bnb robinhood and all
chains possible, daily with the daily report". Each test is the failure it prevents:

* a wallet below B (or untiered) reaches the owner;
* S (stricter than A) is dropped, or counted as something other than A;
* every wallet reads as "new" on the first day, or nothing ever does after;
* a chain without a graded list silently disappears from the message;
* the message is too long for Telegram;
* the files are not GMGN-importable (the same JSON shape as the exports).
"""

from __future__ import annotations

import json
from pathlib import Path

from kaiba.learning import wallet_digest as WD

NOW_MS = 1_800_000_000_000


def _row(addr: str, tier: str, name: str = "") -> dict:
    return {"address": addr, "name": name or f"{tier} $50k w55% r60% 3x3 20m", "emoji": "*",
            "groups": ["default", f"kaiba-hightier-{tier}"]}


def _write(d: Path, label: str, rows: list[dict]) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"kaiba-{label}-hightier.txt").write_text(json.dumps(rows), encoding="utf-8")


def test_only_s_a_b_rows_are_kept_and_each_tier_is_its_own_line_item(tmp_path):
    _write(tmp_path, "solana", [_row("S1" * 20, "S"), _row("A1" * 20, "A"), _row("B1" * 20, "B"),
                                {"address": "C1" * 20, "groups": ["default", "kaiba-hightier-C"]},
                                {"address": "X1" * 20, "groups": ["default"]}])
    (sol,) = [c for c in WD.load(tmp_path, NOW_MS / 1000) if c.label == "solana"]
    assert [sol.count(t) for t in ("S", "A", "B")] == [1, 1, 1] and len(sol.items) == 3
    text = WD.render(WD.load(tmp_path, NOW_MS / 1000), "2026-10-21")
    assert "Solana: S 1 | A 1 | B 1" in text


def test_new_wallets_are_marked_but_not_on_the_first_list(tmp_path, tmp_db):
    _write(tmp_path / "x", "solana", [_row("A1" * 20, "A")])
    first = WD.run(tmp_db, tmp_path / "x", tmp_path / "out", now_ms=NOW_MS)
    assert first["counts"]["solana"]["new"] == 0, "day one: nothing is new"
    _write(tmp_path / "x", "solana", [_row("A1" * 20, "A"), _row("A2" * 20, "A"), _row("B9" * 20, "B")])
    second = WD.run(tmp_db, tmp_path / "x", tmp_path / "out", now_ms=NOW_MS + 86_400_000)
    assert second["counts"]["solana"]["new"] == 2
    assert "A 2 (+1 new)" in second["text"] and "B 1 (+1 new)" in second["text"]
    assert "*A2A2A2" in second["text"]


def test_every_chain_is_named_even_without_a_list(tmp_path):
    _write(tmp_path, "bnb", [_row("0x" + "a" * 40, "B")])
    text = WD.render(WD.load(tmp_path, NOW_MS / 1000), "2026-10-21")
    for name in ("Solana", "BNB", "Robinhood", "Base", "Ethereum"):
        assert name in text
    assert "Robinhood: S 0 | A 0 | B 0  (no graded list yet" in text and "BNB: S 0 | A 0 | B 1" in text


def test_the_message_fits_telegram_with_thousands_of_wallets(tmp_path):
    for label, _ in WD.CHAINS:
        _write(tmp_path, label, [_row(f"{label}{i:040d}"[:44], "A" if i % 3 else "B", "A " + "x" * 60) for i in range(2000)])
    text = WD.render(WD.load(tmp_path, NOW_MS / 1000), "2026-10-21")
    assert len(text) <= 4096


def test_attachments_are_gmgn_import_json_with_only_tiered_rows(tmp_path):
    _write(tmp_path / "x", "robinhood", [_row("0x" + "b" * 40, "A"),
                                         {"address": "0x" + "c" * 40, "groups": ["default"]}])
    paths = WD.attachments(WD.load(tmp_path / "x", NOW_MS / 1000), "2026-10-21", tmp_path / "out")
    assert [p.name for p in paths] == ["kaiba-robinhood-SAB-wallets-2026-10-21.txt"]
    rows = json.loads(paths[0].read_text(encoding="utf-8"))
    assert rows == [_row("0x" + "b" * 40, "A")], "same shape as the exports, tiered rows only"


def test_send_document_posts_multipart_and_never_raises(tmp_path, monkeypatch):
    f = tmp_path / "w.txt"
    f.write_text("[]", encoding="utf-8")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):
        seen["url"], seen["body"], seen["ct"] = req.full_url, req.data, req.headers.get("Content-type")
        return Resp()

    monkeypatch.setattr(WD.urllib.request, "urlopen", fake_urlopen)
    assert WD.send_document(f, token="T", chat_id="42", caption="w.txt") is True
    assert seen["url"].endswith("/sendDocument") and b'name="document"; filename="w.txt"' in seen["body"]
    assert seen["ct"].startswith("multipart/form-data")

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(WD.urllib.request, "urlopen", boom)
    assert WD.send_document(f, token="T", chat_id="42") is False


def test_the_report_job_sends_the_list_after_the_report_and_survives_a_failure(tmp_db, tmp_path, monkeypatch):
    from kaiba.ops import scheduler as S

    monkeypatch.setattr(S, "get_settings", lambda: type("St", (), {"kaiba_data_dir": tmp_path})())
    _write(tmp_path / "claude-runners" / "exports", "solana", [_row("A1" * 20, "A")])
    ctx = S.JobContext("paper_report", tmp_db, {}, S.ScheduleConfig(), NOW_MS, NOW_MS + 300_000)
    out = S._send_wallet_digest(ctx, notify=False)
    assert out["counts"]["solana"]["A"] == 1 and out["sent"] == {"message": False, "files": 0}

    monkeypatch.setattr(WD, "run", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad list")))
    assert "bad list" in S._send_wallet_digest(ctx, notify=False)["error"]


def test_the_hold_reads_in_human_units():
    assert WD._label("A $81k w50% r130% 2x3 58678m") == "A $81k w50% r130% 2 at 3x+ 40d hold"
    assert WD._label("A $169k w52% r309% 3x3 13m") == "A $169k w52% r309% 3 at 3x+ 13m hold"
    assert WD._label("B $12k w46% r31% 0x3 95m tw") == "B $12k w46% r31% 0 at 3x+ 1h hold tw"


def test_all_three_tiers_always_show_and_each_gets_its_own_top_wallets(tmp_path):
    """Owner 2026-10-21: "get b a s tiers always". Many A wallets must not crowd B out."""
    rows = [_row(f"A{i:043d}", "A") for i in range(20)] + [_row("B" + "9" * 43, "B")]
    _write(tmp_path, "solana", rows)
    text = WD.render(WD.load(tmp_path, NOW_MS / 1000), "2026-10-21")
    assert "Solana: S 0 | A 20 | B 1" in text
    assert "B99999...9999" in text, "the B tier gets its own top wallets"
    assert text.count("A00000") <= WD.TOP_PER_TIER
    for name in ("BNB", "Robinhood", "Base", "Ethereum"):
        assert f"{name}: S 0 | A 0 | B 0" in text


def _board(**over):
    return {"available": True, "window_days": 7, "top": [
        {"rank": 1, "wallet": "A" * 44, "mean": 0.32, "n": 8, "win": 0.5, "new": True},
        {"rank": 2, "wallet": "B" * 44, "mean": -0.05, "n": 6, "win": 0.33, "new": False}],
        "eligible": 2, "forward": {"proof": {"n": 12, "need_n": 100, "mean": -0.04}}, **over}


def test_the_list_shows_the_best_wallets_and_says_they_are_not_proven(tmp_path):
    text = WD.render(WD.load(tmp_path, NOW_MS / 1000), "2026-10-21", _board())
    assert "Wallets a copier made money on, Solana, last 7 days: 1 of 2 ranked" in text
    assert "-5.0%" not in text  # the wallet a copier lost on is not listed as a good one
    assert "+32.0% over 8 trades, 50% won" in text and "*1." in text
    assert "12 of 100 trades, mean -4.0%" in text and "Not proven" in text


def test_no_best_list_leaves_the_message_as_it_was(tmp_path):
    lists = WD.load(tmp_path, NOW_MS / 1000)
    plain = WD.render(lists, "2026-10-21")
    assert WD.render(lists, "2026-10-21", {"available": False}) == plain
    assert WD.render(lists, "2026-10-21", _board(top=[])) == plain

