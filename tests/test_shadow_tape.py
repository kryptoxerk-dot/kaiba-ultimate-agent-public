"""shadow_tape: protection's per-tick quotes are kept, once each, without touching the DB."""
import json
import sqlite3

from kaiba.learning import shadow_tape as st


def _db(tmp_path):
    db = tmp_path / "kaiba.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_ms INTEGER)")
    c.commit()
    c.close()
    return db


def _put(db, pid, obs, px, updated, *, chain="sol", token="Mint1", liq=None):
    rec = {"price": {"observed_ms": obs, "price_usd": px, "source": "prices:dexscreener", "venue": "pumpswap",
                     "pool_id": "P1", "executable_quote_usd": None},
           "liquidity": {"liquidity_usd": liq, "liquidity_kind": "pool"} if liq else None}
    c = sqlite3.connect(db)
    c.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (f"watchdog.quote:{pid}:{chain}:{token}", json.dumps(rec), updated))
    c.execute("INSERT OR REPLACE INTO kv VALUES ('other.key', 'x', ?)", (updated,))
    c.commit()
    c.close()


def test_reads_only_quote_records_and_parses_them(tmp_path):
    db = _db(tmp_path)
    _put(db, "pos_a", 1_000, "0.001", 1_100, liq="5000")
    rows = [st.parse(*r) for r in st.read_updates(db, 0)]
    assert [r["pid"] for r in rows] == ["pos_a"]
    assert rows[0]["chain"] == "sol" and rows[0]["token"] == "Mint1" and rows[0]["px"] == "0.001"
    assert rows[0]["liq"] == "5000" and rows[0]["venue"] == "pumpswap"


def test_a_rewritten_observation_is_kept_once_and_a_new_one_appended(tmp_path):
    db, out = _db(tmp_path), tmp_path / "tape"
    tape = st.Tape(out)
    _put(db, "pos_a", 1_791_000_000_000, "0.001", 1)
    assert tape.add(st.parse(*r) for r in st.read_updates(db, 0)) == 1
    _put(db, "pos_a", 1_791_000_000_000, "0.001", 2)  # next tick, same observation
    assert tape.add(st.parse(*r) for r in st.read_updates(db, 1)) == 0
    _put(db, "pos_a", 1_791_000_012_000, "0.0012", 3)
    assert tape.add(st.parse(*r) for r in st.read_updates(db, 2)) == 1
    got = st.load(out, 1_790_999_000_000, 1_791_001_000_000)
    assert [r["px"] for r in got["pos_a"]] == ["0.001", "0.0012"]


def test_records_without_a_price_are_ignored():
    assert st.parse("watchdog.quote:pos_a:sol:M", json.dumps({"price": {"observed_ms": 5, "price_usd": None}}), 1) is None
    assert st.parse("watchdog.quote:pos_a:sol:M", "not json", 1) is None
    assert st.parse("watchdog.quote:bad", "{}", 1) is None


def test_reading_is_skipped_while_the_wal_is_over_the_read_rule(tmp_path, monkeypatch):
    db = _db(tmp_path)
    (tmp_path / "kaiba.db-wal").write_bytes(b"x")
    monkeypatch.setattr(st, "WAL_LIMIT_BYTES", 0)
    assert st.read_updates(db, 0) is None


def test_old_days_are_pruned(tmp_path):
    out = tmp_path / "tape"
    tape = st.Tape(out)
    (out / "2026-09-01.jsonl").write_text("{}\n")
    (out / "2026-10-15.jsonl").write_text("{}\n")
    assert tape.prune(now_s=1_792_100_000, keep_days=16) == 1  # 2026-10-17 -> keep from 10-01
    assert [f.name for f in out.glob("*.jsonl")] == ["2026-10-15.jsonl"]
