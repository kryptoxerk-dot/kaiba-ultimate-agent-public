"""Row-only wallet vetoes must beat every positive source route (offline DBs only)."""
import json
import sqlite3

import pytest

from kaiba.core.schemas import HARD_QUARANTINE_TAGS
from kaiba.execution import lanes
from tests.test_lanes import build_ctx, load_fixture
from tests.test_pons_tax_entry import CREATOR, buy, clean_book, make_ctx


ROUTES = [
    'confluence_5', 'trusted_copy', 'curve_tape', 'curve_dossier',
    'smart_screened', 'smart_vendor', 'smart_archetype', 'pons_robinhood',
]
HARD_TAGS = sorted(t.value for t in HARD_QUARANTINE_TAGS)


def context(conn, route):
    if route == 'pons_robinhood':
        ctx = make_ctx(conn, params={'min_entities': 4})
        evaluator = lanes.pons_robinhood
    else:
        name = ('curve_velocity' if route.startswith('curve_') else
                'sm_trenches' if route.startswith('smart_') else route)
        fx = load_fixture(name)
        if route.startswith('smart_'):
            tags = {'smart_screened': ['smart_money'],
                    'smart_vendor': ['gmgn:smart_degen'], 'smart_archetype': []}[route]
            for wallet in fx['wallets'][:3]:
                wallet['tags'] = tags
            fx['scores'] = [dict(address=w['address'], score=85, grade='A',
                                 archetype='smart_money') for w in fx['wallets'][:3]]
        if route == 'curve_tape':
            fx['dossier']['graded_wallets'] = []
        ctx = build_ctx(conn, fx, params={'min_graded_wallets': 2})
        evaluator = getattr(lanes, name)
    signal = evaluator(ctx)
    assert signal is not None, route
    target = signal.wallets[0]
    # Explicitly clean stored labels, with a good grade, even for Pons' ungraded route.
    conn.execute(
        'INSERT OR IGNORE INTO wallets(chain,address,first_seen_ms,last_seen_ms,tags_json) '
        'VALUES(?,?,?,?,?)', (ctx.chain.value, target, ctx.now_ms, ctx.now_ms, '[]'))
    conn.execute(
        'INSERT OR IGNORE INTO wallet_scores(chain,address,score,grade,evidence_weight,'
        'archetype,factors_json,penalties_json,blockers_json,receipts_json,model_version,'
        'scored_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (ctx.chain.value, target, 85, 'A', 100, 'smart_money', '[]', '[]', '[]', '[]',
         'fixture', ctx.now_ms))
    assert evaluator(ctx) is not None
    return ctx, evaluator, target


def add_veto(ctx, target, tag, placement):
    source = next(r for r in ctx.recent_buys if r['wallet'] == target)
    if placement == 'own_row':
        source['tags'] = ['smart_money', 'gmgn:smart_degen', tag]
    else:
        # A non-voting row still carries a wallet-level veto. The clean positive row
        # remains newest, so selecting just that row cannot clear the wallet.
        ctx.recent_buys.append({**source, 'side': 'sell', 'usd_value': '0',
                                'ts_ms': source['ts_ms'] - 1,
                                'tags': json.dumps([tag])})


@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('tag', HARD_TAGS + ['gmgn:' + t for t in HARD_TAGS])
@pytest.mark.parametrize('placement', ['own_row', 'sibling_row'])
def test_row_only_hard_veto_beats_positive_route(tmp_db, route, tag, placement):
    ctx, evaluator, target = context(tmp_db, route)
    add_veto(ctx, target, tag, placement)
    assert evaluator(ctx) is None
    # No stored veto was used to get that refusal.
    stored = tmp_db.execute('SELECT tags_json,cohort FROM wallets WHERE chain=? AND address=?',
                            (ctx.chain.value, target)).fetchone()
    assert not (set(json.loads(stored['tags_json'])) & set(HARD_TAGS))
    assert stored['cohort'] != 'blacklist'
    grade = tmp_db.execute('SELECT grade FROM wallet_scores WHERE chain=? AND address=?',
                           (ctx.chain.value, target)).fetchone()['grade']
    assert grade in {'A', 'B'}


def test_pons_veto_removes_strength_and_book_votes_without_rejecting_clean_sources(tmp_db):
    ctx = make_ctx(tmp_db)
    before = lanes.pons_robinhood(ctx)
    assert before is not None
    target = before.wallets[0]
    add_veto(ctx, target, 'gmgn:sandwich_bot', 'sibling_row')
    check = lanes.pons_entry_check(ctx)
    assert check.ok
    assert target not in check.post_tax_buyers
    assert target not in check.early_buyers
    assert check.post_tax_entities == check.early_entities == 3
    after = lanes.pons_robinhood(ctx)
    assert after is not None
    assert target not in after.wallets
    assert after.payload['book']['post_tax_buyers'] == 3
    assert after.payload['book']['early_buyers'] == 3
    assert after.strength < before.strength


@pytest.mark.parametrize('hazard', ['bundler', 'dev'])
def test_pons_row_veto_does_not_hide_raw_book_hazards(tmp_db, hazard):
    ctx = make_ctx(tmp_db)
    assert lanes.pons_robinhood(ctx) is not None
    if hazard == 'bundler':
        target = ctx.recent_buys[1]['wallet']
        tmp_db.execute('INSERT INTO token_bundle_members(chain,token,address,role,atoms,buys) '
                       'VALUES(?,?,?,?,?,?)', (ctx.chain.value, ctx.token, target, 'bundler', '1', 1))
        add_veto(ctx, target, 'sandwich_bot', 'own_row')
        refusal = 'known_bundler_in_window'
    else:
        ctx = make_ctx(tmp_db, buys=clean_book() + [buy(CREATOR, 50)])
        add_veto(ctx, CREATOR, 'sandwich_bot', 'own_row')
        refusal = 'dev_bought_after_tax_zero'
    check = lanes.pons_entry_check(ctx)
    assert refusal in check.refusals
    assert lanes.pons_robinhood(ctx) is None


@pytest.mark.parametrize('route', ROUTES)
def test_qualifier_db_failure_is_not_source_clearance(tmp_db, route, monkeypatch):
    ctx, evaluator, target = context(tmp_db, route)
    original = lanes.fetch_one

    def fail_qualifier(conn, sql, args=()):
        if 'LEFT JOIN hub_addresses' in sql:
            raise sqlite3.OperationalError('fixture qualifier failure')
        return original(conn, sql, args)

    monkeypatch.setattr(lanes, 'fetch_one', fail_qualifier)
    with pytest.raises(sqlite3.OperationalError, match='fixture qualifier failure'):
        evaluator(ctx)
