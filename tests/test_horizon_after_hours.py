"""Offline Horizon integration; fake broker only."""
import asyncio
from datetime import date, timedelta
import pytest
from app.horizon_after_hours import HorizonExit
from app.kiwoom.orders import NotSent, OrderUnknown
from app.kiwoom.extended_sell import QuoteWait
from tests.test_after_hours_exit import ah, custom, quote, poll, cancel_completed

@pytest.fixture
def hz(ah,tmp_path):
    old=ah.e
    ah.e=HorizonExit(tmp_path/'hz_extended.json','real',old.broker,old.connected,old.phase,ah.m,old.clock)
    ah.e.open()
    ah.m.sessions={'2026-09-18','2026-09-21','2026-09-22'}
    ah.m.calendar_start='2026-09-18';ah.m.calendar_end='2026-09-22'
    yield ah
    ah.e.close_lock()

async def arm(r,**kw):
    p=await r.e.prepare('005930',date(2026,9,18),**(dict(after_hours='both',retry_unfilled=True,empty_book='wait')|kw))
    assert not r.sends and not r.b.calls
    await r.e.arm(p['id'],'real',True)
    return r.e.plans['005930']

@pytest.mark.parametrize('mode,market,venue',[('both','NX','NXT'),('NXT','NX','NXT'),('KRX','KRX','KRX')])
def test_bid_not_floor_once(hz,mode,market,venue):
    async def run():
        await arm(hz,after_hours=mode,empty_book='floor')
        for _ in range(20):await quote(hz,market,9000)
        assert hz.sends==[('005930',10,9500,venue)] and not hz.b.calls
        await hz.e.reconcile();assert hz.e.orders[0]['status']=='accepted'
    asyncio.run(run())

@pytest.mark.parametrize('fallback,expected',[('wait',None),('floor',7000)])
def test_empty_book(hz,fallback,expected):
    async def run():
        p=await arm(hz,empty_book=fallback);hz.book_best=hz.book_lowest=None
        await quote(hz,price=9000)
        assert hz.sends==([] if expected is None else [('005930',10,expected,'NXT')])
        if expected is None:
            assert p['status']=='armed'
            hz.book_best=9600;hz.book_lowest=9500;hz.now[0]+=timedelta(seconds=12)
            await quote(hz,price=9000);assert len(hz.sends)==1
    asyncio.run(run())

@pytest.mark.parametrize('failure',[QuoteWait('stale'),NotSent('missing fields')])
def test_bad_book_never_floor(hz,failure):
    async def run():
        await arm(hz,empty_book='floor');hz.book_failure=failure
        await quote(hz,price=9000);assert not hz.sends
    asyncio.run(run())

@pytest.mark.parametrize('issue',['off','wrong_venue','stale','unknown_day','weekend','closed'])
def test_gate(hz,issue):
    async def run():
        await arm(hz,after_hours='off' if issue=='off' else 'KRX',retry_unfilled=issue!='off')
        if issue=='unknown_day':hz.m.sessions.remove('2026-09-22')
        if issue=='weekend':hz.now[0]=hz.now[0].replace(day=26)
        if issue=='closed':hz.now[0]=hz.now[0].replace(hour=20)
        hz.e.last_check=hz.now[0]
        await quote(hz,'NX' if issue=='wrong_venue' else 'KRX',9000,age=11 if issue=='stale' else 0)
        assert not hz.sends and not hz.b.calls
    asyncio.run(run())

@pytest.mark.parametrize('price,reason',[(11500,'TAKE_PROFIT'),(9000,'STOP_LOSS')])
def test_regular_unchanged(hz,price,reason):
    async def run():
        hz.now[0]=hz.now[0].replace(hour=10);hz.phase[0]={'value':'3','at':hz.now[0].isoformat()}
        await arm(hz);await quote(hz,'KRX',price)
        assert hz.b.calls==[('005930',10)] and not hz.sends and hz.e.orders[0]['reason']==reason
    asyncio.run(run())

def test_d2_regular_close_only(hz):
    async def run():
        hz.now[0]=hz.now[0].replace(hour=15,minute=20);hz.phase[0]={'value':'2','at':hz.now[0].isoformat()}
        await arm(hz);await quote(hz,'KRX',10000)
        assert hz.b.calls==[('005930',10)] and not hz.sends and hz.e.orders[0]['reason']=='D2_CLOSE'
    asyncio.run(run())

def test_missed_d2_not_after_hours(hz):
    async def run():
        await arm(hz);await quote(hz,price=10000);assert not hz.sends
    asyncio.run(run())

def test_stale_regular_phase_never_market_after_hours(hz):
    async def run():
        hz.phase[0]={'value':'3','at':hz.now[0].replace(hour=9).isoformat()}
        await arm(hz);await quote(hz,'KRX',9000)
        assert not hz.b.calls and hz.sends==[('005930',10,9500,'KRX')]
    asyncio.run(run())

def test_profit_never_chases(hz):
    async def run():
        await arm(hz);await quote(hz,price=11500);hz.book_best=9000;await poll(hz)
        assert len(hz.sends)==1 and not hz.cancels and not hz.e.orders[0]['chase_active']
    asyncio.run(run())

def test_partial_cancel_chain(hz):
    async def run():
        p=await arm(hz);await quote(hz,price=9000);hz.book_best=9400;await poll(hz)
        assert len(hz.cancels)==1
        await poll(hz);assert len(hz.sends)==1
        cancel_completed(hz,filled=3);await hz.e.reconcile()
        assert len(hz.sends)==1 and p['status']=='ordered'
        await poll(hz);assert hz.sends[-1]==('005930',7,9390,'NXT')
        await hz.e.reconcile();assert p['status']=='ordered'
        hz.details[hz.e.orders[-1]['order_no']]={'filled':7,'remaining':0,'rejected':False}
        await hz.e.reconcile();await poll(hz);await hz.e.reconcile()
        assert p['status']=='completed' and len(hz.sends)==2
    asyncio.run(run())

@pytest.mark.parametrize('issue',['pause','unknown','peer','disconnect','restart'])
def test_no_replay(hz,issue):
    async def run():
        await arm(hz)
        if issue=='unknown':hz.b.failure=OrderUnknown('timeout')
        await quote(hz,price=9000)
        if issue=='pause':hz.e.pause()
        if issue=='peer':hz.h.plans['005930']={'id':'peer','code':'005930','status':'armed'}
        if issue=='disconnect':hz.connected[0]=False
        if issue=='restart':hz.e.close_lock();hz.e.open()
        hz.book_best=9400
        for _ in range(3):await poll(hz)
        assert len(hz.sends)==1 and not hz.cancels
    asyncio.run(run())

def test_five_limit(hz):
    async def run():
        await arm(hz);await quote(hz,price=9000)
        for n in range(1,6):
            hz.book_best=9500-100*n;await poll(hz);cancel_completed(hz);await poll(hz)
        hz.book_best=8800;await poll(hz)
        assert len(hz.sends)==6 and len(hz.cancels)==5
        assert hz.e.blocked('005930') and not hz.e.orders[-1]['chase_active']
    asyncio.run(run())

@pytest.mark.parametrize('kw',[{'after_hours':'off','empty_book':'floor'},{'after_hours':'BAD'},{'retry_unfilled':1}])
def test_invalid_config(hz,kw):
    async def run():
        with pytest.raises(NotSent):await arm(hz,**kw)
        assert not hz.e.plans and not hz.sends
    asyncio.run(run())

def test_service_accepts_after_hours_and_never_arms_on_prepare(hz):
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.autoexit_service import create_exit_router
    async def run():
        feed=SimpleNamespace(watch_codes=AsyncMock(),ws=SimpleNamespace(ensure_market=AsyncMock()))
        sell=SimpleNamespace(start=AsyncMock(),calendar=AsyncMock(),bound_feed=feed,get_feed=lambda:feed,monitor=hz.m)
        service=SimpleNamespace(sell=sell,engine=hz.e,token='FAKE')
        app=FastAPI();app.include_router(create_exit_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://127.0.0.1') as client:
            body=dict(code='005930',bought_on='2026-09-18',after_hours='both',retry_unfilled=True,empty_book='floor')
            response=await client.post('/api/autoexit/prepare',json=body,headers={'x-autoexit-token':'FAKE'})
            assert response.status_code==200,response.text
            assert response.json()['after_hours']=='both' and response.json()['status']=='prepared'
            feed.watch_codes.assert_awaited_once_with(['005930_NX'])
            assert not hz.sends and not hz.b.calls
            response=await client.post('/api/autoexit/prepare',json=body|{'retry_unfilled':'true'},headers={'x-autoexit-token':'FAKE'})
            assert response.status_code==422
    asyncio.run(run())
