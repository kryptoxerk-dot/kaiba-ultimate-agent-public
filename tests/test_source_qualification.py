"""Explicit source vetoes beat positive labels; no new grade/history policy."""
import json

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import lanes
from tests.test_lanes import build_ctx, load_fixture

# Real infrastructure address from the application's built-in hub registry.
BINANCE_SOL = '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9'


@pytest.mark.parametrize('chain', ['sol', 'bsc', 'robinhood'])
def test_smart_label_does_not_admit_known_infrastructure(tmp_db, chain):
    fx = load_fixture('sm_trenches')
    if chain != 'sol':
        mapping = {w['address']: '0x' + f'{i+1:040x}' for i,w in enumerate(fx['wallets'])}
        for w in fx['wallets']:
            w['address'] = mapping[w['address']]
        for b in fx['buys']:
            b['wallet'] = mapping[b['wallet']]
        fx['token'] = '0x' + 'a' * 40
        fx['token_meta']['launchpad'] = 'four.meme' if chain == 'bsc' else 'pons'
    fx['chain'] = chain
    hub = BINANCE_SOL if chain == 'sol' else '0x28c6c06298d514db089934071355e5743bf21d60'
    old = fx['wallets'][0]['address']
    fx['wallets'][0]['address'] = hub
    for row in fx['buys']:
        if row['wallet'] == old:
            row['wallet'] = hub
    ctx = build_ctx(tmp_db, fx)
    # Positive control: threshold and token evidence really reach the source route.
    assert lanes.sm_trenches(ctx) is not None
    tmp_db.execute(
        'INSERT INTO hub_addresses(chain,address,kind,label,source) VALUES(?,?,?,?,?)',
        (chain, hub, 'cex', 'Binance hot wallet', 'builtin'),
    )
    assert lanes.sm_trenches(ctx) is None


def mark_excluded(conn, address, kind, stamp, *, chain='sol'):
    if kind in {'hub_coverage', 'cluster_quarantine'}:
        conn.execute(
            'INSERT INTO clustering_coverage(chain,address,status,checked,funding_basis,'
            'signer_basis,run_id,updated_ms) VALUES(?,?,?,?,?,?,?,?)',
            (chain, address, 'hub' if kind == 'hub_coverage' else 'quarantined',
             0, 'unavailable', 'unavailable', 1, stamp),
        )
    elif kind == 'blacklist':
        conn.execute('UPDATE wallets SET cohort=? WHERE chain=? AND address=?',
                     ('blacklist', chain, address))
    elif kind == 'score_quarantine':
        conn.execute('UPDATE wallet_scores SET grade=? WHERE chain=? AND address=?',
                     ('QUARANTINED', chain, address))
    elif kind in {'bare_hard_tag', 'vendor_hard_tag'}:
        tag = 'sandwich_bot' if kind == 'bare_hard_tag' else 'gmgn:sandwich_bot'
        conn.execute('UPDATE wallets SET tags_json=? WHERE chain=? AND address=?',
                     (json.dumps(['smart_money', tag]), chain, address))
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize('kind', [
    'hub_coverage', 'cluster_quarantine', 'blacklist', 'score_quarantine',
    'bare_hard_tag', 'vendor_hard_tag',
])
@pytest.mark.parametrize('route', ['screened_tag', 'provider_label', 'archetype'])
def test_explicit_exclusion_beats_every_smart_route(tmp_db, kind, route):
    fx = load_fixture('sm_trenches')
    target = fx['wallets'][0]['address']
    fx['wallets'][0]['tags'] = {
        'screened_tag': ['smart_money'], 'provider_label': ['gmgn:smart_degen'],
        'archetype': [],
    }[route]
    fx['scores'] = [{'address': target, 'score': 55, 'grade': 'B',
                     'archetype': 'smart_money'}]
    ctx = build_ctx(tmp_db, fx)
    assert lanes.sm_trenches(ctx) is not None
    mark_excluded(tmp_db, target, kind, ctx.now_ms)
    # Feed-carried positive labels must not conceal a newer DB hard exclusion.
    for row in ctx.recent_buys:
        if row['wallet'] == target:
            row['tags'] = ['smart_money'] if route == 'screened_tag' else (
                ['gmgn:smart_degen'] if route == 'provider_label' else [])
    assert lanes.sm_trenches(ctx) is None


@pytest.mark.parametrize('kind', ['hub', 'hub_coverage', 'cluster_quarantine', 'blacklist',
                                 'score_quarantine', 'bare_hard_tag', 'vendor_hard_tag'])
@pytest.mark.parametrize('name', ['confluence_5', 'trusted_copy', 'curve_velocity'])
def test_other_wallet_lanes_respect_explicit_source_veto(tmp_db, name, kind):
    fx = load_fixture(name)
    if name in {'curve_velocity', 'confluence_5'}:
        fx['wallets'] = [{'address': s['address'], 'tags': []} for s in fx['scores']]
    ctx = build_ctx(tmp_db, fx, params={'min_graded_wallets': 2} if name == 'curve_velocity' else {})
    evaluator = getattr(lanes, name)
    signal = evaluator(ctx)
    assert signal is not None
    target = signal.wallets[0]
    if kind == 'hub':
        tmp_db.execute('INSERT INTO hub_addresses(chain,address,kind,source) VALUES(?,?,?,?)',
                       ('sol', target, 'router', 'fixture'))
    else:
        mark_excluded(tmp_db, target, kind, ctx.now_ms)
    assert evaluator(ctx) is None


def test_pons_does_not_count_known_infrastructure_as_confluence(tmp_db):
    from tests.test_pons_tax_entry import make_ctx
    ctx = make_ctx(tmp_db)
    signal = lanes.pons_robinhood(ctx)
    assert signal is not None
    targets = signal.wallets[:2]
    assert len(targets) == 2
    for target in targets:
        tmp_db.execute('INSERT INTO hub_addresses(chain,address,kind,source) VALUES(?,?,?,?)',
                       ('robinhood', target, 'router', 'fixture'))
    assert lanes.pons_robinhood(ctx) is None


def test_partial_history_b_is_not_silently_given_a_new_floor(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture('confluence_5'))
    tmp_db.execute(
        'UPDATE wallet_scores SET grade=?,score=?,evidence_weight=?,closed_trades=?,'
        'blockers_json=? WHERE grade IN (?,?)',
        ('B', 55, 34, 0, json.dumps(['capped to B: sample was truncated by a provider page limit']),
         'A', 'B'),
    )
    signal = lanes.confluence_5(ctx)
    assert signal is not None
    assert len(signal.wallets) == 5  # no new episode/evidence-weight floor in a safety repair


def test_unknown_independence_remains_explicitly_unproven_not_quarantine(tmp_db):
    from kaiba.intelligence.clustering import independence
    ctx = build_ctx(tmp_db, load_fixture('confluence_5'))
    signal = lanes.confluence_5(ctx)
    assert signal is not None
    view = independence(Chain.SOL, signal.wallets, tmp_db)
    assert view.floor == 0
    assert view.ceiling == 5
    assert view.satisfies(5) is None
    # Characterization of the UNREPAIRED policy boundary: the old lane counts unknown
    # addresses. Removing hard exclusions does not prove these five independent.
    assert signal.payload['entity_count'] == 5


def test_positive_feed_tag_cannot_cancel_its_own_hard_quarantine(tmp_db):
    ctx = build_ctx(tmp_db, load_fixture('sm_trenches'))
    assert lanes.sm_trenches(ctx) is not None
    ctx.recent_buys[0]['tags'] = ['smart_money', 'gmgn:sandwich_bot']
    assert lanes.sm_trenches(ctx) is None


def test_hub_exclusion_preserves_chain_identity(tmp_db):
    wallet = '0x28c6c06298d514db089934071355e5743bf21d60'
    contexts = {}
    for chain in ('bsc', 'robinhood'):
        fx = load_fixture('sm_trenches')
        fx['chain'] = chain
        old = fx['wallets'][0]['address']
        fx['wallets'][0]['address'] = wallet
        for buy in fx['buys']:
            if buy['wallet'] == old:
                buy['wallet'] = wallet
        contexts[chain] = build_ctx(tmp_db, fx)
        assert lanes.sm_trenches(contexts[chain]) is not None
    tmp_db.execute('INSERT INTO hub_addresses(chain,address,kind,source) VALUES(?,?,?,?)',
                   ('bsc', wallet, 'cex', 'fixture'))
    assert lanes.sm_trenches(contexts['bsc']) is None
    assert lanes.sm_trenches(contexts['robinhood']) is not None


def test_excluded_pons_vote_does_not_hide_a_bundler_hazard(tmp_db):
    from tests.test_pons_tax_entry import make_ctx
    ctx = make_ctx(tmp_db)
    wallet = ctx.recent_buys[1]['wallet']
    tmp_db.execute('INSERT INTO hub_addresses(chain,address,kind,source) VALUES(?,?,?,?)',
                   ('robinhood', wallet, 'router', 'fixture'))
    tmp_db.execute('INSERT INTO wallets(chain,address,first_seen_ms,last_seen_ms,tags_json) '
                   'VALUES(?,?,?,?,?)', ('robinhood', wallet, ctx.now_ms,ctx.now_ms,'["bundler"]'))
    check = lanes.pons_entry_check(ctx)
    assert wallet not in check.post_tax_buyers
    assert wallet in check.known_bundlers
    assert 'known_bundler_in_window' in check.refusals
