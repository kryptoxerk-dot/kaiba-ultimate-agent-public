"""Synthetic closed books; no provider or canonical DB access."""
import copy
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from kaiba.learning import candidate_pair_audit as mod


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def seal(state, receipt, initialized):
    receipt['receipt_sha256'] = mod.digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
    initialized['receipt_sha256'] = receipt['receipt_sha256']
    initialized['seal_sha256'] = mod.digest({k: v for k, v in initialized.items() if k != 'seal_sha256'})
    folder = state / 'research-allocation'
    write(folder / 'receipt.json', receipt)
    write(folder / 'new-books.json', initialized)
    write(folder / 'authority.json', {'receipt_sha256': receipt['receipt_sha256'],
                                     'new_books_sha256': initialized['seal_sha256']})


@pytest.fixture
def books(tmp_path):
    state = tmp_path / 'autolearn'
    (state / 'paper').mkdir(parents=True)
    (state / 'research-allocation').mkdir()
    records = []
    for n in range(3):
        cid = 'old' if n < 2 else 'new'
        arm = f'fixture-arm-{n}'
        identity = {'campaign_id': cid, 'hypothesis_id': arm,
                    'runner': {'fee_policy': 'fixture-model'},
                    'spec': {'chain': 'sol', 'exit': {'name': f'hold-{n}'}}}
        name = 'candidate-' + mod.digest([cid, arm])[:32] + '.db'
        record = {'campaign_id': cid, 'hypothesis_id': arm, 'identity': identity, 'book': name}
        if n < 2:
            record['lease'] = 'retired' if n == 0 else 'drain_only'
        records.append(record)
        with closing(sqlite3.connect(state / 'paper' / name)) as conn, conn:
            conn.executescript('''
            CREATE TABLE candidate_meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE orders(order_id TEXT PRIMARY KEY,mode TEXT,provider TEXT,side TEXT,amount_in TEXT,filled_out TEXT,fee_native TEXT,created_ms INTEGER,state TEXT);
            CREATE TABLE trades(position_id TEXT,chain TEXT,token TEXT,opened_ms INTEGER,closed_ms INTEGER,cost_native TEXT,proceeds_native TEXT,mode TEXT,trade_id TEXT);
            CREATE TABLE positions(position_id TEXT,qty_total TEXT);
            CREATE TABLE candidate_entries(position_id TEXT,signal_id TEXT);
            CREATE TABLE position_orders(position_id TEXT,order_id TEXT);
            CREATE TABLE order_events(id INTEGER,order_id TEXT,detail TEXT);
            ''')
            conn.execute('INSERT INTO candidate_meta VALUES (?,?)', ('identity', json.dumps(identity)))
            conn.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)',
                         ('position', 'sol', 'private-fixture-token', 1000, 2000, '100', str(110+n), 'shadow', 'trade'))
            conn.execute('INSERT INTO positions VALUES (?,?)', ('position', '1000'))
            conn.execute('INSERT INTO candidate_entries VALUES (?,?)', ('position', 'fixture-signal'))
            for j, side in enumerate(('buy', 'sell')):
                order = f'order-{j}'
                conn.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)',
                             (order, 'shadow', 'paper', side, '100', '1000', '1', 1000+j*1000, 'filled'))
                conn.execute('INSERT INTO position_orders VALUES (?,?)', ('position', order))
                evidence = {'quote_ms': 1000+j*1000, 'price_ts_ms': 1000+j*1000,
                            'depth_ts_ms': 1000+j*1000, 'native_usd': '100', 'native_price_ts_ms': 1000,
                            'decimals': 6, 'fee_policy': 'fixture-model', 'tax_bps': None,
                            'tax_complete': False, 'venue_complete': False,
                            'commission_complete': True, 'source': 'fixture-pool'}
                conn.execute('INSERT INTO candidate_meta VALUES (?,?)', ('fill_evidence:'+order, json.dumps(evidence)))
                conn.execute('INSERT INTO order_events VALUES (?,?,?)', (j, order, json.dumps({'basis': 'pool'})))
    receipt = {'legacy_books': records[:2], 'lifetime_family_size': 3,
               'legacy_manifests': [{'manifest': {'campaign_id': 'old', 'hypotheses':
                    [{'hypothesis_id': r['hypothesis_id']} for r in records[:2]]}}],
               'new_manifest': {'campaign_id': 'new', 'hypotheses': [{'hypothesis_id': records[2]['hypothesis_id']}]}}
    initialized = {'new_books': records[2:]}
    seal(state, receipt, initialized)
    # This is the actual operational shape: a retired row has no book_path.
    write(state / 'latest-report.json', {'campaigns': [{'paper': {'arms': [
        {'allocation': 'retired_read_only', 'hypothesis_id': records[0]['hypothesis_id'], 'open': 0}]}}]})
    return state, records, receipt, initialized


def mutate(books, sql, params=(), *, book=1):
    state, records, *_ = books
    with closing(sqlite3.connect(state / 'paper' / records[book]['book'])) as conn, conn:
        conn.execute(sql, params)


def evidence(books, transform):
    state, records, *_ = books
    with closing(sqlite3.connect(state / 'paper' / records[1]['book'])) as conn, conn:
        raw = conn.execute("SELECT value FROM candidate_meta WHERE key='fill_evidence:order-0'").fetchone()[0]
        value = json.loads(raw)
        transform(value)
        conn.execute("UPDATE candidate_meta SET value=? WHERE key='fill_evidence:order-0'", (json.dumps(value),))


def test_retired_and_active_inventory_exact_pair_and_no_mutation(books):
    state, *_ = books
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.rglob('*') if p.is_file()}
    result = mod.audit(state, frontier_ms=3000)
    assert result['books'] == 3 and result['retired_books'] == 1
    assert result['rows']['closed_rows'] == 3 and result['queries'] == 15
    assert result['pairs']['same_campaign_token_signal_closed_pairs'] == 1  # cross-campaign excluded
    assert result['pairs']['identical_recorded_entry'] == 1
    assert result['pairs']['both_all_tax_complete'] == 0
    assert result['rows']['closed_has_pool_id_field_every_leg'] == 0
    assert not result['scoring_performed'] and not result['absolute_profit_proven']
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.rglob('*') if p.is_file()}
    assert 'private-fixture-token' not in json.dumps(result)


@pytest.mark.parametrize('field,value', [('amount_in', '101'), ('filled_out', '999'), ('created_ms', 1001)])
def test_different_entries_are_not_paired(books, field, value):
    mutate(books, f"UPDATE orders SET {field}=? WHERE side='buy'", (value,))
    result = mod.audit(books[0], frontier_ms=3000)
    assert result['pairs']['entry_differs'] == 1
    assert result['pairs']['entry_diff_order_' + field] == 1
    assert result['pairs'].get('identical_recorded_entry', 0) == 0


def test_missing_entry_evidence_is_unknown_not_equal(books):
    evidence(books, lambda v: v.pop('native_price_ts_ms'))
    result = mod.audit(books[0], frontier_ms=3000)
    assert result['pairs']['entry_evidence_missing'] == 1
    assert result['pairs'].get('identical_recorded_entry', 0) == 0


def test_pool_label_and_complete_flags_do_not_make_a_profit_claim(books):
    evidence(books, lambda v: v.update(pool='private-fixture-pool', tax_complete=True, venue_complete=True))
    result = mod.audit(books[0], frontier_ms=3000)
    assert not result['scoring_performed'] and not result['absolute_profit_proven']
    assert 'private-fixture-pool' not in json.dumps(result)


def test_bad_flag_does_not_leak_arbitrary_strings(books):
    evidence(books, lambda v: v.update(tax_complete='private-body'))
    result = mod.audit(books[0], frontier_ms=3000)
    assert result['rows']['legs_tax_complete:unknown'] == 1
    assert 'private-body' not in json.dumps(result)


def test_unknown_exit_label_is_not_exported(books):
    state, records, receipt, initialized = books
    identity = records[1]['identity']
    identity['spec']['exit']['name'] = 'private-label-that-is-not-a-declared-exit'
    mutate(books, "UPDATE candidate_meta SET value=? WHERE key='identity'", (json.dumps(identity),))
    seal(state, receipt, initialized)
    result = mod.audit(state, frontier_ms=3000)
    assert result['arms'][1]['exit'] == 'other'
    assert 'private-label-that-is-not-a-declared-exit' not in json.dumps(result)


def test_frontier_excludes_future_closes(books):
    result = mod.audit(books[0], frontier_ms=1999)
    assert result['rows'].get('closed_rows', 0) == 0 and result['pairs'] == {}


@pytest.mark.parametrize('filename', ['receipt.json', 'new-books.json', 'authority.json'])
def test_tampered_allocation_stops_before_db(books, filename, monkeypatch):
    path = books[0] / 'research-allocation' / filename
    value = json.loads(path.read_text(encoding='utf-8'))
    if filename == 'authority.json':
        value['receipt_sha256'] = 'wrong'
    else:
        value['unexpected'] = True
    write(path, value)
    monkeypatch.setattr(mod.sqlite3, 'connect', lambda *a, **k: pytest.fail('must not open any database'))
    with pytest.raises(ValueError, match='hash_mismatch'):
        mod.audit(books[0], frontier_ms=3000)


def test_retired_arm_cannot_be_silently_dropped(books):
    state, _, receipt, initialized = books
    receipt['legacy_books'].pop(0)
    receipt['lifetime_family_size'] = 2
    seal(state, receipt, initialized)
    with pytest.raises(ValueError, match='manifest_inventory_mismatch'):
        mod.audit(state, frontier_ms=3000)


def test_allocation_cannot_redirect_to_canonical_database(books):
    state, _, receipt, initialized = books
    receipt['legacy_books'][0]['book'] = '../../kaiba.db'
    seal(state, receipt, initialized)
    with pytest.raises(ValueError, match='book_name_mismatch'):
        mod.audit(state, frontier_ms=3000)


def test_foreign_execution_refused(books):
    mutate(books, "UPDATE orders SET mode='live' WHERE side='buy'")
    with pytest.raises(ValueError, match='foreign_inventory'):
        mod.audit(books[0], frontier_ms=3000)


def test_unmatched_trade_not_silently_removed(books):
    mutate(books, 'DELETE FROM candidate_entries')
    with pytest.raises(ValueError, match='unmatched_closed_rows'):
        mod.audit(books[0], frontier_ms=3000)


def test_identity_drift_refused(books):
    changed = copy.deepcopy(books[1][1]['identity'])
    changed['runner']['fee_policy'] = 'changed'
    mutate(books, "UPDATE candidate_meta SET value=? WHERE key='identity'", (json.dumps(changed),))
    with pytest.raises(ValueError, match='book_identity_mismatch'):
        mod.audit(books[0], frontier_ms=3000)


def test_query_only_prevents_mutations_and_closes_connection(books):
    state, records, *_ = books
    path = state / 'paper' / records[0]['book']
    reader = mod.Reader(state / 'paper')
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        reader.query(path, "INSERT INTO candidate_meta SELECT 'extra','value' FROM orders", 1)
    assert reader.query(path, 'SELECT cost_native FROM trades', 10)[0]['cost_native'] == '100'


def test_row_cap_refuses_truncation(books):
    state, records, *_ = books
    with pytest.raises(ValueError, match='row_limit'):
        mod.Reader(state / 'paper').query(state / 'paper' / records[0]['book'], 'SELECT * FROM orders', 1)


def test_expensive_query_interrupted_and_next_connection_usable(books):
    state, records, *_ = books
    reader = mod.Reader(state / 'paper')
    path = state / 'paper' / records[0]['book']
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        reader.query(path, '''WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n
                             WHERE x<1000000000) SELECT SUM(x) FROM n''', 1)
    assert reader.durations[-1] < 3
    assert reader.query(path, 'SELECT COUNT(*) AS n FROM trades', 1)[0]['n'] == 1


def test_cli_fails_unavailable_without_raw_path(books, capsys):
    mutate(books, "DELETE FROM candidate_meta WHERE key='identity'")
    assert mod.main(['--state-dir', str(books[0])]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output['reason'] == 'book_identity_missing'
    assert str(books[0]) not in json.dumps(output)
