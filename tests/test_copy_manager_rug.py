"""copy_manager's rug rules: named, sell-everything, each with its own off/dry/live mode.

Owner, 2026-10-03: "control the copy trade sell before it rug or something". MEASURED the
same day on the copy book's last 30 days (302 holding windows with tape):
no rule had a confidence interval clear of zero on the right side, so all three ship DRY --
they decide, and each firing is recorded once per cycle for the forward measurement.
These tests pin the rules' shapes, the graduation guard (a Kaiba rule once sold 33 of 45
graduations as rugs), and that a dry rule can neither send nor mask the live giveback.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from kaiba.core.db import fetch_all, jload
from kaiba.core.schemas import Chain
from kaiba.execution import copy_manager as CM

RH = Chain.ROBINHOOD
TOK = "0x96c59a1883bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
T0 = 1_790_700_000_000
MIN = 60_000
STARTED = (T0 - 3_600_000) // 1000          # held for an hour before the first pass
CREATED = STARTED - 600


def row(*, price="1.0", liq="100000", pnl="0.0", alert=None, honeypot=False, value="150",
        opened=CREATED, created=CREATED, started=STARTED):
    tok = {"token_address": TOK, "symbol": "RUG", "decimals": 18, "price": price, "liquidity": liq,
           "is_honeypot": honeypot, "open_timestamp": opened, "creation_timestamp": created}
    if alert is not None:
        tok["is_show_alert"] = alert
    return {"usd_value": value, "unrealized_profit_pnl": pnl, "start_holding_at": started, "token": tok}


def cfg(**over):
    """Giveback-only policy (the box's), rug rules as given; live job."""
    return CM.CopyConfig.from_params({"live": True, "tp_rungs": [], "weak_cut_enabled": False, **over})


class Book:
    def __init__(self, conn, config, migrated=None):
        self.conn, self.cfg, self.sent, self.migrated = conn, config, [], migrated or {}

    def at(self, t, **r):
        def submit(token, qty, min_out):
            self.sent.append((token, qty))
            return {"order_id": f"ord:{len(self.sent)}", "state": "submitted"}

        return CM.run(self.conn, RH, self.cfg, fetch_holdings=lambda: {"list": [row(**r)]},
                      wallet_units=lambda t_, d: 10**24, native_usd=lambda: Decimal("2700"),
                      submit=submit, slippage_bps=2500, now=t,
                      migrated_ms=lambda token: self.migrated.get(token))


def rule_events(conn):
    return [jload(r["payload"], {}) for r in fetch_all(conn, "SELECT payload FROM events WHERE kind='system'")
            if jload(r["payload"], {}).get("action") == CM.RULE_ACTION]


def kinds(rep):
    return [(r["kind"], r["action"]) for r in rep.rules]


# --------------------------------------------------------------------------------------
# fast crash
# --------------------------------------------------------------------------------------


def test_a_fast_crash_is_decided_and_recorded_once_but_never_sent_while_dry(tmp_db):
    b = Book(tmp_db, cfg())
    b.at(T0, price="1.0")
    rep = b.at(T0 + 6 * MIN, price="0.45")                 # -55% from the 15-min high
    assert kinds(rep) == [("fast_crash", "dry_rule")] and b.sent == []
    b.at(T0 + 7 * MIN, price="0.44")
    ev = rule_events(tmp_db)
    assert len(ev) == 1, "one record per cycle per rule, however many dry passes see it"
    assert (ev[0]["kind"], ev[0]["mode"], ev[0]["sent"], ev[0]["price_usd"]) == ("fast_crash", "dry", False, "0.45")
    assert "position_id" not in ev[0] and "intent" not in ev[0]


def test_a_live_fast_crash_sells_everything(tmp_db):
    b = Book(tmp_db, cfg(fast_crash="live"))
    b.at(T0, price="1.0")
    rep = b.at(T0 + 6 * MIN, price="0.45")
    assert [d["kind"] for d in rep.decisions] == ["fast_crash"] and b.sent == [(TOK, 10**24)]
    assert kinds(rep) == [("fast_crash", "submitted")] and rule_events(tmp_db) == []
    st = CM.load_state(tmp_db, RH, TOK)
    assert st.closed and st.closed_kind == "fast_crash"


def test_a_live_rule_still_waits_for_a_live_job(tmp_db):
    b = Book(tmp_db, CM.CopyConfig.from_params({"live": False, "tp_rungs": [], "fast_crash": "live"}))
    b.at(T0, price="1.0")
    rep = b.at(T0 + 6 * MIN, price="0.45")
    assert [d["action"] for d in rep.decisions] == ["dry_run"] and b.sent == []
    assert rule_events(tmp_db)[0]["why_not"] == "dry_run"


@pytest.mark.parametrize("price, started, fires", [
    ("0.45", STARTED, True),
    ("0.60", STARTED, False),                               # -40%: under the 50% threshold
    ("0.45", (T0 + 6 * MIN) // 1000 - 60, False),           # bought a minute ago: min hold
])
def test_the_crash_threshold_and_the_minimum_hold(tmp_db, price, started, fires):
    b = Book(tmp_db, cfg())
    b.at(T0, price="1.0", started=started)
    rep = b.at(T0 + 6 * MIN, price=price, started=started)
    assert bool(kinds(rep)) is fires


# --------------------------------------------------------------------------------------
# liquidity collapse, and the graduation that looks like one
# --------------------------------------------------------------------------------------


def test_liquidity_and_price_falling_together_is_a_collapse(tmp_db):
    b = Book(tmp_db, cfg(fast_crash="off"))
    b.at(T0, price="1.0", liq="100000")
    rep = b.at(T0 + 5 * MIN, price="0.65", liq="50000")
    assert kinds(rep) == [("liquidity_collapse", "dry_rule")]


def test_liquidity_leaving_without_the_price_falling_is_not_a_rug(tmp_db):
    """A graduation drains the curve; the price does not crash. A drained pool cannot fill
    a sell at +43%, which is how the 2026-09-23 graduations-as-rugs were caught."""
    b = Book(tmp_db, cfg(fast_crash="off"))
    b.at(T0, price="1.0", liq="100000")
    assert kinds(b.at(T0 + 5 * MIN, price="1.05", liq="0")) == []


@pytest.mark.parametrize("graduated_min_ago, fires", [(5, False), (40, True)])
def test_gmgns_open_timestamp_marks_a_graduation(tmp_db, graduated_min_ago, fires):
    now = T0 + 5 * MIN
    opened = (now - graduated_min_ago * MIN) // 1000
    b = Book(tmp_db, cfg(fast_crash="off"))
    b.at(T0, price="1.0", liq="100000", opened=opened)
    rep = b.at(now, price="0.65", liq="50000", opened=opened)
    assert bool(kinds(rep)) is fires


def test_kaibas_own_migration_record_also_marks_one(tmp_db):
    now = T0 + 5 * MIN
    b = Book(tmp_db, cfg(fast_crash="off"), migrated={TOK: now - 3 * MIN})
    b.at(T0, price="1.0", liq="100000")
    assert kinds(b.at(now, price="0.65", liq="50000")) == []
    b2 = Book(tmp_db, cfg(fast_crash="off"), migrated={TOK: now + 3 * MIN})   # future: ignored
    assert kinds(b2.at(now + MIN, price="0.60", liq="40000")) == [("liquidity_collapse", "dry_rule")]


def test_the_default_migration_lookup_reads_kaibas_tokens_table(tmp_db):
    now = T0 + 5 * MIN
    tmp_db.execute("INSERT INTO tokens (chain, address, migrated_ms, first_seen_ms) VALUES (?,?,?,?)",
                   (RH.value, TOK, now - MIN, T0))
    assert CM.kaiba_migrated_ms(tmp_db, RH, TOK) == now - MIN
    b = Book(tmp_db, cfg(fast_crash="off"))
    b.migrated = None

    def at(t, **r):
        return CM.run(tmp_db, RH, b.cfg, fetch_holdings=lambda: {"list": [row(**r)]},
                      wallet_units=lambda t_, d: 10**24, native_usd=lambda: Decimal("2700"),
                      submit=lambda *a: {"order_id": "x"}, slippage_bps=2500, now=t)

    at(T0, price="1.0", liq="100000")
    assert kinds(at(now, price="0.65", liq="50000")) == []


def test_a_pool_that_collapses_below_the_floor_is_still_watched_by_the_rug_rules_only(tmp_db):
    """``thin_pool`` used to drop a token the moment its pool fell under $5k -- exactly when a
    rug sell is due. A token managed above the floor stays visible to the rug rules; the
    policy rules (giveback here, armed by the +80% peak) do not act on a thin pool."""
    b = Book(tmp_db, cfg(fast_crash="off", liquidity_collapse="live"))
    b.at(T0, price="1.0", liq="50000", pnl="0.80")
    rep = b.at(T0 + 5 * MIN, price="0.40", liq="2000", pnl="-0.28")
    assert rep.skipped == {"thin_pool": 1} and rep.managed == 0
    assert [d["kind"] for d in rep.decisions] == ["liquidity_collapse"] and b.sent == [(TOK, 10**24)]
    # Never seen above the floor: still skipped outright.
    fresh = Book(tmp_db, cfg(liquidity_collapse="live"))
    other = CM.run(tmp_db, RH, fresh.cfg,
                   fetch_holdings=lambda: {"list": [{**row(price="0.4", liq="2000"),
                                                     "token": {**row()["token"], "token_address": "0x" + "c" * 40,
                                                               "liquidity": "2000"}}]},
                   wallet_units=lambda t_, d: 10**24, native_usd=lambda: Decimal("2700"),
                   submit=lambda *a: {"order_id": "y"}, slippage_bps=2500, now=T0 + 6 * MIN)
    assert other.rules == [] and other.decisions == []


# --------------------------------------------------------------------------------------
# risk flags
# --------------------------------------------------------------------------------------


def test_an_alert_that_turns_on_while_held_fires_and_one_already_on_does_not(tmp_db):
    b = Book(tmp_db, cfg(fast_crash="off"))
    b.at(T0, alert=False)
    assert kinds(b.at(T0 + MIN, alert=True)) == [("risk_flags", "dry_rule")]
    tmp_db.execute("DELETE FROM kv")
    b2 = Book(tmp_db, cfg(fast_crash="off"))
    b2.at(T0, alert=True)                                    # on from the first sighting
    assert kinds(b2.at(T0 + MIN, alert=True)) == []


def test_a_honeypot_flip_is_recorded_and_never_sent_even_when_live(tmp_db):
    b = Book(tmp_db, cfg(fast_crash="off", risk_flags="live"))
    b.at(T0, honeypot=False)
    rep = b.at(T0 + MIN, honeypot=True)
    assert rep.skipped == {"honeypot": 1} and b.sent == []
    assert kinds(rep) == [("risk_flags", "unsellable")]
    assert [e["why_not"] for e in rule_events(tmp_db)] == ["unsellable"]


# --------------------------------------------------------------------------------------
# a dry rule never masks the live giveback
# --------------------------------------------------------------------------------------


def test_a_dry_rug_rule_does_not_mask_a_live_giveback(tmp_db):
    b = Book(tmp_db, cfg())
    b.at(T0, price="1.0", pnl="0.80")
    rep = b.at(T0 + 6 * MIN, price="0.45", pnl="0.30")      # gave back AND crashed
    assert [d["kind"] for d in rep.decisions] == ["giveback"] and b.sent == [(TOK, 10**24)]
    assert kinds(rep) == [("fast_crash", "dry_rule")]


# --------------------------------------------------------------------------------------
# config and state
# --------------------------------------------------------------------------------------


def test_every_rug_rule_ships_dry_and_modes_are_spelled_out():
    base = CM.CopyConfig()
    assert (base.liquidity_collapse, base.risk_flags, base.fast_crash) == ("dry", "dry", "dry")
    for bad in (True, "on", "yes", 1):
        with pytest.raises(ValueError, match="one of"):
            CM.CopyConfig.from_params({"fast_crash": bad})
    with pytest.raises(ValueError):
        CM.CopyConfig.from_params({"crash_drop": 1})
    assert CM.CopyConfig.from_params({"risk_flags": "OFF"}).risk_flags == "off"


def test_an_off_rule_is_not_evaluated(tmp_db):
    b = Book(tmp_db, cfg(fast_crash="off", liquidity_collapse="off"))
    b.at(T0, price="1.0", liq="100000")
    assert b.at(T0 + 6 * MIN, price="0.30", liq="20000").rules == []


def test_state_written_before_the_rug_rules_still_loads():
    old = {"started_s": 5, "cycle_ms": 7, "peak_pnl": "0.5", "prices": [[1, "0.1"]], "closed": False}
    st = CM.TokenState.load(old)
    assert st.liqs == [] and st.alert is None and st.logged == [] and not st.honeypot
    assert CM.TokenState.load(st.dump()) == st
