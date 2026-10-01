"""EXIT-1: real executor and policy; only final CLI transport is replaced."""
from decimal import Decimal

import pytest
import yaml

from kaiba.core.config import load_risk
from kaiba.core.schemas import EVM_ZERO, Chain, Lane, LaneMode, OrderState, Side
from kaiba.execution import executor, policy

SOL_TOKEN = 'CJF7MNqb9xv1XrTs5St1Du5JuQXLsfFBB137vmYRKpnb'
SOL_WALLET = '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM'


@pytest.fixture
def book(tmp_db, tmp_path, monkeypatch):
    cfg = load_risk()
    cfg.bounds.max_slippage_bps = 2500
    cfg.bounds.max_exit_slippage_bps = 8800
    cfg.kill_switch = cfg.reduce_only = cfg.entries_paused = False
    cfg.global_mode = cfg.bounds.max_lane_mode = LaneMode.LIVE
    cfg.lanes[Lane.SM_TRENCHES].mode = LaneMode.LIVE
    cfg.lanes[Lane.SM_TRENCHES].chains = [Chain.SOL, Chain.BSC, Chain.ROBINHOOD]
    for chain in (Chain.SOL, Chain.BSC, Chain.ROBINHOOD):
        cfg.chains[chain].wallet = SOL_WALLET if chain is Chain.SOL else '0x' + 'a' * 40
    path = tmp_path / 'exit-policy-risk.yaml'
    path.write_text(yaml.safe_dump(cfg.model_dump(mode='json')))
    monkeypatch.setenv('KAIBA_RISK_PATH', str(path))
    sent = []
    def send(args, **kw):
        sent.append(args)
        return {'data': {'order_id': 'fixture-wide-exit'}}
    monkeypatch.setattr(executor, '_run_gmgn', send)
    return tmp_db, sent, path


def order(chain=Chain.SOL, side=Side.SELL, bps=8800, minimum=1):
    return executor.build_order(decision_id=None, chain=chain,
                                token=SOL_TOKEN if chain is Chain.SOL else '0x' + 'b' * 40,
                                side=side, lane=Lane.SM_TRENCHES, mode=LaneMode.LIVE,
                                amount_in=1000000, min_out=minimum, slippage_bps=bps)


@pytest.mark.parametrize('chain', [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_wide_sell_reaches_transport_with_positive_floor_and_anti_mev(book, chain):
    db, sent, _ = book
    result = executor.submit_gmgn(order(chain), db)
    assert result.state is OrderState.SUBMITTED and len(sent) == 1
    args = sent[0]
    assert args[args.index('--slippage') + 1] == '88.00'
    assert int(args[args.index('--min-output') + 1]) > 0 and '--anti-mev' in args
    assert db.execute("SELECT COUNT(*) FROM orders WHERE state='submitted'").fetchone()[0] == 1


@pytest.mark.parametrize('side,bps', [(Side.BUY, 8800), (Side.BUY, 2501),
                                    (Side.SELL, 8801), (Side.SELL, 10000)])
def test_wrong_tolerance_refuses_before_transport(book, side, bps):
    db, sent, _ = book
    with pytest.raises(executor.ExecutionRefused, match='slippage_out_of_range'):
        executor.submit_gmgn(order(side=side, bps=bps), db)
    assert sent == []
    assert db.execute('SELECT COUNT(*) FROM orders').fetchone()[0] == 0


def test_entry_at_its_bound_still_sends(book):
    db, sent, _ = book
    assert executor.submit_gmgn(order(side=Side.BUY, bps=2500), db).state is OrderState.SUBMITTED
    assert len(sent) == 1


@pytest.mark.parametrize('damage', ['pretend_sell', 'wrong_output', 'unknown_side', 'zero_floor'])
def test_direction_context_and_amount_cannot_bypass(book, damage):
    db, sent, _ = book
    candidate = order(Chain.BSC)
    if damage == 'pretend_sell':
        candidate = candidate.model_copy(update={'input_token': EVM_ZERO,
                                                 'output_token': candidate.token})
    elif damage == 'wrong_output':
        candidate = candidate.model_copy(update={'output_token': '0x' + 'c' * 40})
    elif damage == 'unknown_side':
        candidate = candidate.model_copy(update={'side': 'unexpected'})
    else:
        candidate = candidate.model_copy(update={'min_out': 0})
    with pytest.raises(executor.ExecutionRefused):
        executor.submit_gmgn(candidate, db)
    assert sent == []


@pytest.mark.parametrize('extra', [{'beneficiary': '0x'+'f'*40}, {'side': 'sell'}])
def test_injected_body_fields_still_refuse(book, monkeypatch, extra):
    db, sent, _ = book
    real = executor.gmgn_swap_body
    monkeypatch.setattr(executor, 'gmgn_swap_body', lambda *a: {**real(*a), **extra})
    with pytest.raises(executor.ExecutionRefused):
        executor.submit_gmgn(order(), db)
    assert sent == []


@pytest.mark.parametrize('slip', ['NaN', 'Infinity', '-Infinity', '100', '0', '-1', True])
def test_nonfinite_and_unbounded_slippage_refuses(book, slip):
    db, _, _ = book
    body = executor.gmgn_swap_body(order(), SOL_WALLET)
    body['slippage'] = slip
    assert not policy.check_gmgn_swap_body(body, wallet=SOL_WALLET, chain=Chain.SOL,
                                         side=Side.SELL, conn=db).allowed


def test_side_less_caller_keeps_entry_ceiling(book):
    db, _, _ = book
    body = executor.gmgn_swap_body(order(), SOL_WALLET)
    assert not policy.check_gmgn_swap_body(body, wallet=SOL_WALLET, chain=Chain.SOL, conn=db).allowed
    body['slippage'] = '25.00'
    assert policy.check_gmgn_swap_body(body, wallet=SOL_WALLET, chain=Chain.SOL, conn=db).allowed


def test_overwide_config_does_not_authorize_an_unbounded_exit(book):
    db, sent, path = book
    config = yaml.safe_load(path.read_text())
    config['bounds']['max_exit_slippage_bps'] = 10000
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(executor.ExecutionRefused, match='slippage_bound_invalid'):
        executor.submit_gmgn(order(), db)
    assert sent == []


def test_decimal_comparison_does_not_round_past_entry_ceiling(book):
    db, _, _ = book
    body = executor.gmgn_swap_body(order(side=Side.BUY), SOL_WALLET)
    body['slippage'] = str(Decimal('25.000000000000001'))
    assert not policy.check_gmgn_swap_body(body, wallet=SOL_WALLET, chain=Chain.SOL,
                                         side=Side.BUY, conn=db).allowed
