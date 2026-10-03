"""FIX27: no-network regressions for wallet work amplification and concurrent writers."""
import json

import pytest

from kaiba.core.schemas import Chain, Grade, WalletTag
from kaiba.intelligence import grade, pnl, tracker

WALLET = 'Bnd5oBSWpPpXoaWTckgXTVx9TCKkX5GyVqxcrwBzUYsB'


def seed_shape(conn, *, buys=12, sells=10, tags=(), provider=None):
    conn.execute(
        'INSERT INTO wallets(chain,address,tags_json,meta_json,first_seen_ms,last_seen_ms) VALUES (?,?,?,?,1000,1000)',
        ('sol', WALLET, json.dumps(list(tags)), json.dumps(provider or {})),
    )
    conn.execute(
        'INSERT INTO wallet_scores(chain,address,score,grade,evidence_weight,archetype,model_version,scored_at_ms) '
        'VALUES (?,?,?,?,?,?,?,?)',
        ('sol', WALLET, 50.0, 'B', 40.0, 'trader', grade.MODEL_ID_TAPE, 1000),
    )
    conn.executemany(
        'INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,source) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        [('sol', f'tx-{side}-{i}', 1000 + i, WALLET, 'token', side, '10', '100', 'test')
         for side, n in [('buy', buys), ('sell', sells)] for i in range(n)],
    )


@pytest.mark.parametrize('buys,sells,tags,provider,expected', [
    (12, 10, (), {}, 'failure_rate_disabled'),
    (0, 40, (), {'buy_count': 100, 'sell_count': 100}, 'sell_only'),
    (0, 0, (), {'buy_count': 12, 'sell_count': 10}, 'failure_rate_disabled'),
    (0, 0, (), {}, 'shape_unassessable'),
    (12, 10, ('gmgn:wash_trader',), {}, 'quarantine_tags'),
])
def test_admission_uses_shape_and_stored_grade_without_replaying_pnl(
    tmp_db, monkeypatch, buys, sells, tags, provider, expected,
):
    seed_shape(tmp_db, buys=buys, sells=sells, tags=tags, provider=provider)
    calls = []
    reconstruct = pnl.reconstruct

    def counted(*args, **kwargs):
        calls.append(len(args[0]))
        return reconstruct(*args, **kwargs)

    monkeypatch.setattr(pnl, 'reconstruct', counted)
    screen = tracker.screen_wallet(Chain.SOL, WALLET, tmp_db, check_failure_rate=False)
    assert screen.grade == Grade.B
    assert expected in screen.blocker_codes
    assert screen.observed_buys == (buys if buys + sells else None)
    assert screen.observed_sells == (sells if buys + sells else None)
    if tags:
        assert WalletTag.WASH_TRADER in screen.tags
    if buys + sells:
        assert float(screen.buy_share) == pytest.approx(buys / (buys + sells))
    elif provider:
        assert float(screen.buy_share) == pytest.approx(12 / 22)
    else:
        assert screen.buy_share is None
    assert calls == [], 'admission rebuilt PnL it never consumes, once per screened wallet'


def test_backfill_reranks_only_tokens_from_the_bounded_history_page(tmp_db, monkeypatch):
    from kaiba.ingest import backfill
    from tests.test_backfill import FakeHelius, WALLET as HISTORY_WALLET, fixture

    page = fixture('enhanced_page')
    FakeHelius([page], None).install(monkeypatch)
    tmp_db.execute(
        'INSERT INTO swaps(chain,tx,ts_ms,wallet,token,side,amount_token,amount_native,source) '
        "VALUES ('sol','unrelated',1000,'unrelated-wallet','unrelated-token','buy','1','1','test')"
    )
    report = backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[HISTORY_WALLET], with_meta=False)
    assert report.swaps_written == 3
    touched = {r.token for r in backfill.parse_page(page, HISTORY_WALLET).swaps if r.side == 'buy'}
    rebuilt = {r[0] for r in tmp_db.execute('SELECT DISTINCT token FROM first_buyers')}
    assert rebuilt == touched, 'a bounded wallet backfill rebuilt unrelated chain-wide first buyers'
    assert report.first_buyers_written > 0


def test_backfill_empty_explicit_targets_do_not_fall_back_to_registry(tmp_db, monkeypatch):
    from kaiba.ingest import backfill
    from tests.test_backfill import FakeHelius, WALLET as HISTORY_WALLET, fixture

    fake = FakeHelius([fixture('enhanced_page')], None).install(monkeypatch)
    tmp_db.execute(
        'INSERT INTO wallets(chain,address,first_seen_ms,last_seen_ms,cohort) VALUES (?,?,0,0,?)',
        ('sol', HISTORY_WALLET, 'tracked'),
    )
    report = backfill.backfill_wallets(tmp_db, Chain.SOL, wallets=[], with_meta=False)
    assert report.wallets == 0, 'empty explicit targets widened to the tracked registry'
    assert fake.enhanced_calls == []


def test_explicit_tape_targets_bound_all_evidence_reads(tmp_db):
    from tests.test_tape_grading import round_trips, seed_swaps, seed_trade_event

    seed_shape(tmp_db, buys=0, sells=0, provider={'seed_confluence': 3})
    seed_swaps(tmp_db, WALLET, round_trips(8))
    seed_trade_event(tmp_db, WALLET, ['smart_degen', 'wash_trader'])
    for i in range(10):
        tmp_db.execute(
            'INSERT INTO first_buyers(chain,token,wallet,rank,ts_ms,source) VALUES (?,?,?,?,?,?)',
            ('sol', 'TOKEN000', WALLET if i == 0 else f'peer{i}', i + 1, 1000, 'test'),
        )

    def measured():
        ticks = []
        evidence = []
        tmp_db.set_progress_handler(lambda: ticks.append(1) or 0, 100)
        try:
            report = grade.grade_tape(tmp_db, Chain.SOL, wallets=[WALLET], store=False,
                                      as_of_ms=1_790_000_000_000, on_score=lambda ev, score: evidence.append((ev, score)))
        finally:
            tmp_db.set_progress_handler(None, 0)
        assert report.wallets_seen == report.wallets_scored == 1
        ev, score = evidence[0]
        assert ev.seed_confluence == 3
        assert ev.early_metrics.validated_early_tokens == 1  # token population is not target-limited
        assert set(ev.tape.provider_tags) == {'smart_degen', 'wash_trader'}
        assert score.grade == Grade.QUARANTINED  # hard exclusion survives the cheaper path
        assert score.model_version == grade.MODEL_ID_TAPE
        return len(ticks), score.model_dump(exclude={'scored_at_ms', 'receipts'})

    small, expected = measured()
    tmp_db.executemany(
        'INSERT INTO events(ts_ms,kind,chain,subject,payload) VALUES (0,?,?,?,?)',
        [('wallet.trade', 'sol', f'unrelated-{i}', json.dumps({'tags': ['smart_degen']})) for i in range(2000)],
    )
    tmp_db.executemany(
        'INSERT INTO wallets(chain,address,first_seen_ms,last_seen_ms,meta_json) VALUES (?,?,0,0,?)',
        [('sol', f'unrelated-{i}', json.dumps({'seed_confluence': i})) for i in range(2000)],
    )
    large, actual = measured()
    assert actual == expected
    assert large <= small + 20, f'explicit one-wallet grade scanned unrelated evidence: VM ticks {small} -> {large}'


def test_naming_releases_writer_between_bounded_batches(tmp_db, monkeypatch):
    from kaiba.intelligence import naming

    tmp_db.executemany(
        'INSERT INTO wallets(chain,address,first_seen_ms,last_seen_ms) VALUES (?,?,0,0)',
        [('sol', f'naming-{i}') for i in range(5)],
    )
    monkeypatch.setattr(naming, 'NAMING_WRITE_BATCH', 2, raising=False)
    queries = []
    tmp_db.set_trace_callback(queries.append)
    try:
        report = naming.name_wallets(tmp_db, now=1000)
    finally:
        tmp_db.set_trace_callback(None)
    assert report.updated == 5
    batch_sizes, current = [], None
    for sql in queries:
        if sql.startswith('BEGIN'):
            current = 0
        elif sql.startswith('UPDATE wallets') and current is not None:
            current += 1
        elif sql.startswith('COMMIT') and current is not None:
            batch_sizes.append(current)
            current = None
    assert batch_sizes and max(batch_sizes) <= 2, f'namer monopolized the writer: {batch_sizes}'
    assert naming.name_wallets(tmp_db, now=1000).unchanged == 5


@pytest.mark.parametrize('preexisting', [True, False])
def test_naming_preserves_concurrent_registration_and_safety_tags(tmp_db, monkeypatch, preexisting):
    import sqlite3
    from kaiba.intelligence import naming

    seed_shape(tmp_db)
    if not preexisting:
        tmp_db.execute('DELETE FROM wallets WHERE chain=? AND address=?', ('sol', WALLET))
    gather = naming.gather_facts
    path = tmp_db.execute('PRAGMA database_list').fetchone()[2]

    def concurrent_change(conn, chain=None):
        result = gather(conn, chain)
        other = sqlite3.connect(path, isolation_level=None)
        try:
            other.execute(
                'INSERT INTO wallets(chain,address,name,tags_json,meta_json,first_seen_ms,last_seen_ms) '
                'VALUES (?,?,?,?,?,1000,1000) ON CONFLICT(chain,address) DO UPDATE SET '
                'name=excluded.name,tags_json=excluded.tags_json,meta_json=excluded.meta_json',
                ('sol', WALLET, 'operator edit', '["gmgn:wash_trader"]', '{"operator_note":"keep"}'),
            )
        finally:
            other.close()
        return result

    monkeypatch.setattr(naming, 'gather_facts', concurrent_change)
    try:
        report = naming.name_wallets(tmp_db, now=1000)
    except sqlite3.IntegrityError as exc:
        pytest.fail(f'naming raced registration and aborted the whole batch: {exc}')
    row = tmp_db.execute('SELECT name,tags_json,meta_json FROM wallets WHERE chain=? AND address=?',
                         ('sol', WALLET)).fetchone()
    assert tuple(row) == ('operator edit', '["gmgn:wash_trader"]', '{"operator_note":"keep"}'), 'stale naming evidence erased concurrent safety metadata'
    assert report.inserted == report.updated == 0
    assert report.as_dict()['concurrent_skipped'] == 1
