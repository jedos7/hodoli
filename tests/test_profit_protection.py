"""Fixed-price protection tests. All brokers and prices are in-memory fakes."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.custom_exit import protection_condition
from app.custom_exit_service import create_custom_exit_router
from app.kiwoom.orders import NotSent, OrderRejected, OrderUnknown
from tests.test_custom_exit import custom, tick


async def protect(r, *, price=83800, tp=None, tq=None, sl=None, sq=None):
    r.b.row['average'] = 81227
    r.m.positions['005930'].average = 81227
    p = await r.e.prepare('005930', tp, tq, sl, sq, price)
    await r.e.arm(p['id'], 'paper', True)
    return r.e.plans['005930']


@pytest.mark.parametrize('price,send', [(83801, False), (83800, False), (83799, True), (80000, True)])
def test_strict_threshold_all_holdings_once(custom, price, send):
    async def run():
        p = await protect(custom)
        for _ in range(20):
            await tick(custom, price)
        assert custom.b.calls == ([('005930', 10)] if send else [])
        assert custom.h.plans == {} and custom.h.orders == []
        if send:
            order = custom.e.orders[0]
            assert order['reason'] == 'CUSTOM_PROFIT_PROTECTION'
            assert order['trigger_price'] == '83800'
            assert order['trigger_comparison'] == 'lt'
            assert order['trigger_pct'] is None
            assert p['status'] == 'ordered'
    asyncio.run(run())


@pytest.mark.parametrize('price', [0, -1, 10**9, 83800.0, 83800.1, True, '83800', 'NaN', float('nan')])
def test_invalid_protection_price_rejected(price):
    with pytest.raises(ValueError):
        protection_condition(price, 10)


def test_preparing_or_arming_never_uses_old_quote(custom):
    async def run():
        custom.e.quote('005930', 80000, custom.now[0], 'KRX')
        p = await custom.e.prepare('005930', profit_protection_price=83800)
        assert p['status'] == 'prepared' and not custom.b.calls
        await custom.e.arm(p['id'], 'paper', True)
        custom.e.check()
        assert not custom.b.calls
        # A newly received below-threshold quote can trigger immediately.
        await tick(custom, 83700)
        assert custom.b.calls == [('005930', 10)]
    asyncio.run(run())


@pytest.mark.parametrize('sl,sq', [(None, None), (10, 3)])
def test_partial_profit_continues_protection_for_all_remaining(custom, sl, sq):
    async def run():
        p = await protect(custom, tp=15, tq=4, sl=sl, sq=sq)
        await tick(custom, 94000)
        custom.b.response = {'filled': 2, 'remaining': 2, 'rejected': False}
        await custom.e.reconcile()
        await tick(custom, 83000)
        assert custom.b.calls == [('005930', 4)]
        custom.b.response = {'filled': 4, 'remaining': 0, 'rejected': False}
        custom.b.row.update(quantity=6, available=6)
        await custom.e.reconcile()
        assert p['status'] == 'remainder_check'  # REST alone is insufficient.
        await tick(custom, 83000)
        assert len(custom.b.calls) == 1
        custom.m.positions['005930'].quantity = 6
        await custom.e.reconcile()
        assert p['status'] == 'armed' and p['take_profit_done']
        assert p['profit_protection']['quantity'] == 6
        if sl:
            assert p['stop_loss']['quantity'] == 3
        await tick(custom, 94000)
        assert len(custom.b.calls) == 1  # TP must not repeat.
        await tick(custom, 83800)
        assert len(custom.b.calls) == 1
        await tick(custom, 83700)
        assert custom.b.calls == [('005930', 4), ('005930', 6)]
        custom.b.response = {'filled': 6, 'remaining': 0, 'rejected': False}
        await custom.e.reconcile()
        await tick(custom, 80000)
        assert p['status'] == 'completed' and len(custom.b.calls) == 2
    asyncio.run(run())


def test_protection_precedes_smaller_stop_quantity_on_gap(custom):
    async def run():
        await protect(custom, sl=10, sq=3)
        await tick(custom, 70000)
        assert custom.b.calls == [('005930', 10)]
        assert custom.e.orders[0]['reason'] == 'CUSTOM_PROFIT_PROTECTION'
    asyncio.run(run())


def test_overlapping_profit_and_protection_rejected(custom):
    async def run():
        with pytest.raises(ValueError, match='조건 중복'):
            await custom.e.prepare('005930', 5, 4, profit_protection_price=10501)
        p = await custom.e.prepare('005930', 5, 4, profit_protection_price=10500)
        await custom.e.arm(p['id'], 'paper', True)
        await tick(custom, 10500)
        assert custom.e.orders[0]['reason'] == 'CUSTOM_TAKE_PROFIT'
    asyncio.run(run())


@pytest.mark.parametrize('case', ['stale', 'future', 'nxt', 'closing', 'pause', 'balance', 'average',
    'available', 'disconnect', 'late', 'clock_back', 'pending', 'peer'])
def test_invalid_state_never_sends_protection(custom, case):
    async def run():
        await protect(custom)
        stamp = custom.now[0]
        market = 'KRX'
        if case == 'stale': stamp -= timedelta(seconds=11)
        if case == 'future': stamp += timedelta(seconds=1)
        if case == 'nxt': market = 'NX'
        if case == 'closing': custom.phase[0]['value'] = '2'
        if case == 'pause': custom.b.on_send = lambda: custom.e.pause()
        if case == 'balance': custom.b.row.update(quantity=9, available=9)
        if case == 'average': custom.b.row['average'] = 80000
        if case == 'available': custom.b.row['available'] = 9
        if case == 'disconnect': custom.b.on_positions = lambda: custom.connected.__setitem__(0, False)
        if case == 'late': custom.b.on_positions = lambda: custom.now.__setitem__(0, stamp+timedelta(seconds=11))
        if case == 'clock_back':
            custom.phase[0]['at'] = (stamp-timedelta(seconds=30)).isoformat()
            custom.b.on_positions = lambda: custom.now.__setitem__(0, stamp-timedelta(seconds=1))
        if case == 'pending': custom.b.pending = [{'ord_no': 'other'}]
        if case == 'peer': custom.b.on_send = lambda: custom.h.plans.update({'005930': {'status': 'armed'}})
        custom.e.quote('005930', 83000, stamp, market)
        if custom.e.tasks: await asyncio.gather(*list(custom.e.tasks))
        assert not custom.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('failure,status', [(OrderUnknown('fake timeout'), 'unknown'), (OrderRejected('fake rejected'), 'rejected')])
def test_protection_failure_never_replays(custom, failure, status):
    async def run():
        await protect(custom)
        custom.b.failure = failure
        await tick(custom, 83000)
        for _ in range(3):
            await custom.e.reconcile()
            await tick(custom, 83000)
        assert custom.e.orders[0]['status'] == status
        assert custom.b.calls == [('005930', 10)]
    asyncio.run(run())


@pytest.mark.parametrize('pause', [False, True])
def test_protection_partial_fill_never_duplicates(custom, pause):
    async def run():
        p = await protect(custom)
        await tick(custom, 83000)
        custom.b.response = {'filled': 3, 'remaining': 7, 'rejected': False}
        await custom.e.reconcile()
        if pause: custom.e.pause()
        await tick(custom, 80000)
        assert custom.b.calls == [('005930', 10)]
        custom.b.response = {'filled': 10, 'remaining': 0, 'rejected': False}
        await custom.e.reconcile()
        await tick(custom, 80000)
        assert p['status'] == 'completed' and len(custom.b.calls) == 1
        assert not custom.e.snapshot()['enabled']
    asyncio.run(run())


@pytest.mark.parametrize('restart', [False, True])
def test_stop_or_restart_prevents_protection_continuation(custom, restart):
    async def run():
        await protect(custom, tp=15, tq=4)
        await tick(custom, 94000)
        if restart:
            custom.e.close_lock()
            custom.e.open()
        else:
            custom.e.pause()
        custom.b.row.update(quantity=6, available=6)
        custom.m.positions['005930'].quantity = 6
        custom.b.response = {'filled': 4, 'remaining': 0, 'rejected': False}
        await custom.e.reconcile()
        await tick(custom, 83000)
        assert not custom.e.snapshot()['enabled']
        assert custom.b.calls == [('005930', 4)]
    asyncio.run(run())


def test_protection_save_failure_blocks_order(custom, monkeypatch):
    async def run():
        await protect(custom)
        def fail(): raise OSError('fake disk full')
        monkeypatch.setattr(custom.e, 'save', fail)
        with pytest.raises(OSError):
            custom.e.quote('005930', 83000, custom.now[0], 'KRX')
        assert custom.e.error and not custom.e.snapshot()['enabled']
        assert not custom.b.calls
    asyncio.run(run())


def test_old_percentage_only_plan_still_works(custom):
    async def run():
        p = await custom.e.prepare('005930', 5, 4, 3, 10)
        del custom.e.plans['005930']['profit_protection']
        custom.e.persist()
        await custom.e.arm(p['id'], 'paper', True)
        await tick(custom, 10500)
        assert custom.b.calls == [('005930', 4)]
        assert custom.e.orders[0]['reason'] == 'CUSTOM_TAKE_PROFIT'
    asyncio.run(run())


def test_api_strict_price_and_prepare_only(custom):
    async def start(): pass
    service = SimpleNamespace(engine=custom.e, token='fake', sell=SimpleNamespace(start=start))
    app = FastAPI()
    app.include_router(create_custom_exit_router(service))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
            headers = {'X-Custom-Exit-Token': 'fake'}
            for price in [True, 83800.5, 83800.0, '83800', 0, -1, 10**9]:
                response = await client.post('/api/custom-exit/prepare', headers=headers,
                    json={'code': '005930', 'profit_protection_price': price})
                assert response.status_code == 422
            body = {'code': '005930', 'profit_protection_price': 83800}
            assert (await client.post('/api/custom-exit/prepare', headers=headers | {'Origin': 'https://other.test'}, json=body)).status_code == 403
            response = await client.post('/api/custom-exit/prepare', headers=headers, json=body)
            assert response.status_code == 200
            p = response.json()
            assert p['status'] == 'prepared' and p['profit_protection']['quantity'] == 10
            assert p['take_profit'] is None and p['stop_loss'] is None
            assert not custom.e.snapshot()['enabled'] and not custom.b.calls
    asyncio.run(run())
