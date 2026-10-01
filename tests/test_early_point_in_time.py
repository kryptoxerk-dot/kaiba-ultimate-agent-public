"""Early-mooner research must reject evidence observed after its feature cutoff."""
from kaiba.core.schemas import Chain
from kaiba.learning import variables
from kaiba.learning.mooner import TokenCase


def case():
    return TokenCase(chain=Chain.SOL,token='fixture',early_wallets=('a','b'),
                     early_buys=8,early_sells=2,first_price=1.0,window_end_price=1.1,
                     window_end_ms=100_000,multiple=6.0,launchpad='current-only')


def test_dossier_built_after_feature_cutoff_is_not_predictive():
    body={'built_at_ms':101_000,'_grade':'A',
          'liquidity_usd':{'value':'1000000','basis':'provider_reported',
                           'receipt':{'observed_at_ms':101_000}}}
    f=variables.features_for(case(),body)
    assert f.get('liquidity_usd') is None
    assert f.get('grade') is None
    assert f['early_buy_fraction']==0.8


def test_numeric_bins_are_fitted_without_holdout_rows(tmp_db, monkeypatch):
    rows=[({'holder_count':float(i)},i%2==0,i) for i in range(1,201)]
    rows += [({'holder_count':float(10000+i)},i%2==0,200+i) for i in range(1,201)]
    monkeypatch.setattr(variables,'build_rows',lambda *a,**kw: rows)
    original=variables._binner
    fitted={}
    def spy(name,sample):
        fitted[name]=[r[2] for r in sample]
        return original(name,sample)
    monkeypatch.setattr(variables,'_binner',spy)
    variables.run(tmp_db,pairs=False)
    assert max(fitted['holder_count'])==200
    assert len(fitted['holder_count'])==200


def test_unversioned_launchpad_metadata_is_not_a_predictive_feature():
    assert variables.features_for(case(),None).get('launchpad') is None


def test_future_field_receipt_is_not_rescued_by_old_dossier_timestamp():
    body={'built_at_ms':90_000,'liquidity_usd':{
        'value':'1000000','basis':'provider_reported',
        'receipt':{'observed_at_ms':101_000}}}
    assert variables.features_for(case(),body).get('liquidity_usd') is None


def test_same_timestamp_price_cannot_be_future_outcome(tmp_db):
    from kaiba.learning import mooner
    prices=[1.0]*10+[100.0]+[1.0]*5
    for i,price in enumerate(prices):
        ts=100_000 if i<=10 else 100_000+i
        tmp_db.execute('INSERT INTO swaps (chain,tx,ts_ms,wallet,token,side,price_usd,source) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       ('sol',f'fixture-tx-{i}',ts,f'w{i}','fixture','buy',str(price),'test'))
    tmp_db.commit()
    cases=mooner.build_cases(tmp_db)
    assert len(cases)==1
    assert cases[0].multiple==1.0
