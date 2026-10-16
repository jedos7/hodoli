import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.autoexit import AutoExit
from app.autosell import KST, Position, SellMonitor
from app.custom_exit import CustomExit, condition
from app.custom_exit_service import create_custom_exit_router
from app.exit_coordination import CoordinatedBroker, ExecutionGroup
from app.kiwoom.orders import NotSent, OrderUnknown, OrderRejected
from tests.test_autoexit import Broker


@pytest.fixture
def custom(tmp_path):
    now=[datetime(2026,9,22,10,tzinfo=KST)]; connected=[True]
    phase=[{'value':'3','at':now[0].isoformat()}]
    monitor=SellMonitor(tmp_path/'shadow.json')
    monitor.positions['005930']=Position('005930','테스트',10,10000)
    broker=Broker()
    args=('paper',lambda:broker,lambda:connected[0],lambda:phase[0],monitor,lambda:now[0])
    engine=CustomExit(tmp_path/'custom.json',*args);engine.open()
    horizon=AutoExit(tmp_path/'horizon.json',*args);horizon.open()
    engine.broker=lambda:CoordinatedBroker(broker,horizon,'호라이즌')
    horizon.broker=lambda:CoordinatedBroker(broker,engine,'직접 설정')
    rig=SimpleNamespace(e=engine,h=horizon,b=broker,m=monitor,now=now,connected=connected,phase=phase)
    yield rig
    engine.close_lock();horizon.close_lock()


async def arm(r, tp=5, tq=4, sl=3, sq=10):
    p=await r.e.prepare('005930',tp,tq,sl,sq)
    await r.e.arm(p['id'],'paper',True)
    return r.e.plans['005930']


async def tick(r,price):
    r.e.quote('005930',price,r.now[0],'KRX')
    if r.e.tasks: await asyncio.gather(*list(r.e.tasks))


@pytest.mark.parametrize('price,reason,qty',[(10499,None,0),(10500,'CUSTOM_TAKE_PROFIT',4),
    (9701,None,0),(9700,'CUSTOM_STOP_LOSS',10)])
def test_exact_custom_thresholds_send_only_requested_quantity(custom,price,reason,qty):
    async def run():
        await arm(custom)
        for _ in range(20): await tick(custom,price)
        assert custom.b.calls==([('005930',qty)] if reason else [])
        if reason: assert custom.e.orders[0]['reason']==reason
        assert custom.h.plans=={} and custom.h.orders==[]
    asyncio.run(run())


@pytest.mark.parametrize('stop_quantity,expected',[(10,6),(3,3)])
def test_partial_profit_resumes_stop_once_for_remaining_balance(custom,stop_quantity,expected):
    async def run():
        p=await arm(custom,sq=stop_quantity)
        await tick(custom,10500)
        custom.b.response={'filled':2,'remaining':2,'rejected':False}
        await custom.e.reconcile()
        await tick(custom,9500)
        assert custom.b.calls==[('005930',4)]  # Do not race a still-open TP order.
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        custom.b.row.update(quantity=6,available=6)
        custom.m.positions['005930'].quantity=6
        await custom.e.reconcile()
        assert p['status']=='armed' and p['take_profit_done'] and p['quantity']==6
        assert p['stop_loss']['quantity']==expected
        await tick(custom,12000)
        assert len(custom.b.calls)==1  # Profit leg never repeats after a rebound.
        await tick(custom,9700)
        assert custom.b.calls==[('005930',4),('005930',expected)]
        custom.b.response={'filled':expected,'remaining':0,'rejected':False}
        await custom.e.reconcile()
        await tick(custom,9000)
        assert p['status']=='completed' and len(custom.b.calls)==2
    asyncio.run(run())


def test_balance_event_before_order_reply_does_not_drop_chosen_stop_continuation(custom):
    async def run():
        p=await arm(custom)
        async def send(code,qty,guard):
            guard();custom.b.calls.append((code,qty))
            custom.m.positions[code].quantity=6
            custom.e.account_changed()
            return '123'
        custom.b.sell_once=send
        await tick(custom,10500)
        assert p['status']=='ordered' and p['continue_stop']
        custom.b.row.update(quantity=6,available=6)
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        await custom.e.reconcile()
        assert p['status']=='armed' and p['take_profit_done']
    asyncio.run(run())


def test_explicit_stop_while_profit_pending_prevents_auto_resume(custom):
    async def run():
        p=await arm(custom);await tick(custom,10500)
        custom.e.pause()
        custom.b.row.update(quantity=6,available=6)
        custom.m.positions['005930'].quantity=6
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        await custom.e.reconcile();await tick(custom,9000)
        assert p['status']=='completed' and not custom.e.snapshot()['enabled']
        assert custom.b.calls==[('005930',4)]
    asyncio.run(run())


def test_stop_continuation_waits_for_account_events_and_can_be_paused(custom):
    async def run():
        p=await arm(custom);await tick(custom,10500)
        custom.b.row.update(quantity=6,available=6)
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        await custom.e.reconcile()
        assert p['status']=='remainder_check'
        await tick(custom,9000);assert len(custom.b.calls)==1
        custom.e.pause()
        custom.m.positions['005930'].quantity=6
        await custom.e.reconcile();await tick(custom,9000)
        assert p['status']=='paused' and len(custom.b.calls)==1
    asyncio.run(run())


@pytest.mark.parametrize('change',['balance','average','pending','disconnected'])
def test_remainder_mismatch_never_resumes_or_guesses_quantity(custom,change):
    async def run():
        p=await arm(custom);await tick(custom,10500)
        custom.b.row.update(quantity=6,available=6)
        custom.m.positions['005930'].quantity=6
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        if change=='balance': custom.b.row.update(quantity=7,available=7)
        if change=='average': custom.b.row['average']=9999
        if change=='pending': custom.b.pending=[{'ord_no':'other'}]
        if change=='disconnected': custom.connected[0]=False
        await custom.e.reconcile();await tick(custom,9000)
        assert p['status']=='paused' and len(custom.b.calls)==1
    asyncio.run(run())


@pytest.mark.parametrize('tp,tq,sl,sq,price,quantity',[(7,3,None,None,10700,3),(None,None,4,2,9600,2),(5,10,3,10,10500,10)])
def test_single_leg_and_full_profit_do_not_rearm(custom,tp,tq,sl,sq,price,quantity):
    async def run():
        p=await arm(custom,tp,tq,sl,sq);await tick(custom,price)
        custom.b.response={'filled':quantity,'remaining':0,'rejected':False}
        await custom.e.reconcile();await tick(custom,9000)
        assert p['status']=='completed' and custom.b.calls==[('005930',quantity)]
    asyncio.run(run())


@pytest.mark.parametrize('pct,quantity,loss',[(None,1,False),(1,None,False),(0,1,False),(-1,1,False),
    (100,1,True),(1001,1,False),(1.234,1,False),('NaN',1,False),('Infinity',1,False),
    (5,0,False),(5,11,False),(5,1.2,False),(True,1,False),(5,True,False)])
def test_invalid_percentages_and_quantities_rejected(pct,quantity,loss):
    with pytest.raises(ValueError): condition(pct,quantity,10000,10,loss)


def test_no_calendar_or_holding_day_exit(custom):
    async def run():
        await arm(custom)
        custom.now[0]+=timedelta(days=30);custom.e.last_check=custom.now[0]
        custom.phase[0]={'value':'2','at':custom.now[0].isoformat()}
        custom.e.check();await tick(custom,12000)
        assert not custom.b.calls
        assert custom.e.snapshot()['rules']['time_exit'] is False
    asyncio.run(run())


@pytest.mark.parametrize('failure,status',[(OrderUnknown('timeout'),'unknown'),(OrderRejected('reject'),'rejected')])
def test_failed_orders_never_resume_or_replay(custom,failure,status):
    async def run():
        await arm(custom);custom.b.failure=failure
        await tick(custom,10500)
        for _ in range(3): await custom.e.reconcile();await tick(custom,9000)
        assert custom.e.orders[0]['status']==status and len(custom.b.calls)==1
    asyncio.run(run())


def test_restart_disarms_stop_continuation(custom):
    async def run():
        await arm(custom);await tick(custom,10500)
        custom.e.close_lock();custom.e.open()
        custom.b.row.update(quantity=6,available=6);custom.m.positions['005930'].quantity=6
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        await custom.e.reconcile();await tick(custom,9000)
        assert not custom.e.snapshot()['enabled'] and len(custom.b.calls)==1
    asyncio.run(run())


def test_horizon_rules_and_both_directions_of_conflict(custom):
    async def run():
        custom.m.sessions={'2026-09-18','2026-09-21','2026-09-22'}
        hp=await custom.h.prepare('005930',date(2026,9,18))
        await custom.h.arm(hp['id'],'paper',True)
        with pytest.raises(NotSent,match='호라이즌'): await custom.e.prepare('005930',5,4,3,10)
        assert custom.h.plans['005930']['status']=='armed'
        assert custom.h.snapshot()['rules']=={'hold_sessions':2,'day_zero':'first_buy_fill','take_profit_pct':15,'stop_loss_pct':-10}
        custom.h.pause('005930')
        await arm(custom)
        with pytest.raises(NotSent,match='직접 설정'): await custom.h.prepare('005930',date(2026,9,18))
        custom.e.pause()
        hp=await custom.h.prepare('005930',date(2026,9,18))
        assert hp['hold_sessions']==2 and not custom.b.calls
    asyncio.run(run())


def test_concurrent_same_stock_preparations_only_one_reserves(custom):
    async def run():
        custom.m.sessions={'2026-09-18'}
        waiting=0
        barrier=asyncio.Event()
        async def open_orders(code):
            nonlocal waiting
            waiting+=1
            if waiting==2:barrier.set()
            await barrier.wait()
            return []
        custom.b.open_orders=open_orders
        result=await asyncio.gather(custom.e.prepare('005930',5,4,3,10),
                                    custom.h.prepare('005930',date(2026,9,18)),return_exceptions=True)
        assert sum(isinstance(x,dict) for x in result)==1
        assert sum(isinstance(x,NotSent) for x in result)==1
    asyncio.run(run())


@pytest.mark.parametrize('case',['stale','future','nxt','pause','balance','average','available','disconnect','late','clock_back'])
def test_custom_preflight_never_sends_from_invalid_state(custom,case):
    async def run():
        await arm(custom)
        stamp=custom.now[0];market='KRX'
        if case=='stale':stamp-=timedelta(seconds=11)
        if case=='future':stamp+=timedelta(seconds=1)
        if case=='nxt':market='NX'
        if case=='pause':custom.b.on_send=lambda:custom.e.pause()
        if case=='balance':custom.b.row.update(quantity=9,available=9)
        if case=='average':custom.b.row['average']=9999
        if case=='available':custom.b.row['available']=9
        if case=='disconnect':custom.b.on_positions=lambda:custom.connected.__setitem__(0,False)
        if case=='late':custom.b.on_positions=lambda:custom.now.__setitem__(0,stamp+timedelta(seconds=11))
        if case=='clock_back':
            custom.phase[0]['at']=(stamp-timedelta(seconds=30)).isoformat()
            custom.b.on_positions=lambda:custom.now.__setitem__(0,stamp-timedelta(seconds=1))
        custom.e.quote('005930',10600,stamp,market)
        if custom.e.tasks:await asyncio.gather(*list(custom.e.tasks))
        assert not custom.b.calls
    asyncio.run(run())


def test_stop_during_remainder_balance_lookup_wins(custom):
    async def run():
        p=await arm(custom);await tick(custom,10500)
        custom.b.row.update(quantity=6,available=6);custom.m.positions['005930'].quantity=6
        custom.b.response={'filled':4,'remaining':0,'rejected':False}
        custom.b.on_positions=lambda:custom.e.pause()
        await custom.e.reconcile();await tick(custom,9000)
        assert p['status']=='paused' and len(custom.b.calls)==1
    asyncio.run(run())


def test_custom_write_failure_blocks_order_and_leaves_horizon_settings_intact(custom,monkeypatch):
    async def run():
        await arm(custom)
        before=custom.h.path.read_bytes()
        def fail():raise OSError('disk full')
        monkeypatch.setattr(custom.e,'save',fail)
        with pytest.raises(OSError):custom.e.quote('005930',10500,custom.now[0],'KRX')
        assert not custom.b.calls and custom.e.error and not custom.e.snapshot()['enabled']
        assert custom.h.path.read_bytes()==before
    asyncio.run(run())


def test_custom_stop_does_not_pause_horizon_other_stock(custom):
    custom.h.plans['000660']={'code':'000660','status':'armed'}
    custom.e.pause()
    assert custom.h.plans['000660']['status']=='armed'


def test_peer_becomes_active_during_send_wait_blocks_actual_transport(custom):
    async def run():
        await arm(custom)
        custom.b.on_send=lambda:custom.h.plans.update({'005930':{'status':'armed'}})
        await tick(custom,10500)
        assert not custom.b.calls and custom.e.plans['005930']['status']=='paused'
    asyncio.run(run())


def test_custom_api_rejects_cross_origin_fractional_qty_and_horizon_token(custom):
    async def start():pass
    service=SimpleNamespace(engine=custom.e,token='custom-token',sell=SimpleNamespace(start=start))
    app=FastAPI();app.include_router(create_custom_exit_router(service))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
            response=await client.get('/api/custom-exit')
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            assert (await client.post('/api/custom-exit/pause',json={},headers={'X-Autoexit-Token':'horizon-token'})).status_code==403
            headers={'X-Custom-Exit-Token':'custom-token'}
            assert (await client.post('/api/custom-exit/pause',json={},headers=headers|{'Origin':'https://other.test'})).status_code==403
            body={'code':'005930','take_profit_pct':5,'take_profit_quantity':1.5}
            assert (await client.post('/api/custom-exit/prepare',json=body,headers=headers)).status_code==422
            body['take_profit_quantity']=4
            response=await client.post('/api/custom-exit/prepare',json=body,headers=headers)
            assert response.status_code==200 and response.json()['take_profit']['quantity']==4
            assert custom.b.calls==[]
    asyncio.run(run())
