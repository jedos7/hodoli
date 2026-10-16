import asyncio
from datetime import date,timedelta
import pytest
from app.horizon_after_hours import HorizonExit
from app.kiwoom.orders import NotSent,OrderUnknown
from tests.test_after_hours_exit import ah,custom,arm as arm_ticket,quote,poll,cancel_completed

@pytest.fixture(params=['custom','horizon'])
def rig(request,ah,tmp_path):
    original=ah.e
    if request.param=='horizon':
        ah.e=HorizonExit(tmp_path/'hz_cancel.json','real',original.broker,original.connected,original.phase,ah.m,original.clock);ah.e.open()
        ah.m.sessions={'2026-09-18','2026-09-21','2026-09-22'}
    ah.mode=request.param
    yield ah
    if ah.e is not original:ah.e.close_lock()

async def pending(r):
    if r.mode=='horizon':
        p=await r.e.prepare('005930',date(2026,9,18),after_hours='both',retry_unfilled=True)
        await r.e.arm(p['id'],'real',True)
    else:await arm_ticket(r)
    await quote(r,price=9000)
    assert len(r.sends)==1
    return r.e.orders[0]

async def confirm(r,review):return await r.e.confirm_manual_cancel(review['review_id'],'real',True)

def test_preview_read_only_and_confirmed_cancel_not_acceptance(rig):
    async def run():
        o=await pending(rig);review=await rig.e.prepare_manual_cancel()
        assert review['items'][0]['remaining']==10 and not rig.cancels and o['chase_active']
        await confirm(rig,review)
        assert rig.cancels==['101'] and o['manual_cancel_state']=='accepted' and not o['chase_active']
        await rig.e.reconcile();assert o['manual_cancel_state']=='accepted'
        cancel_completed(rig,3);await rig.e.reconcile()
        assert o['manual_cancel_state']=='confirmed' and o['manual_cancel_quantity']==7
        await poll(rig);assert len(rig.sends)==1 and len(rig.cancels)==1
    asyncio.run(run())

@pytest.mark.parametrize('issue',['wrong_env','false_ack','expired','paused','consumed'])
def test_invalid_review_never_cancels(rig,issue):
    async def run():
        await pending(rig);review=await rig.e.prepare_manual_cancel()
        if issue=='expired':rig.now[0]+=timedelta(seconds=61)
        if issue=='paused':rig.e.pause()
        if issue=='consumed':await confirm(rig,review)
        with pytest.raises(NotSent):await rig.e.confirm_manual_cancel(review['review_id'],'paper' if issue=='wrong_env' else 'real',issue!='false_ack')
        assert len(rig.cancels)==(1 if issue=='consumed' else 0)
    asyncio.run(run())

@pytest.mark.parametrize('issue',['all_filled','partial','missing','bad_remaining','other_order','pause_during_send','timeout'])
def test_cancel_races(rig,issue):
    async def run():
        o=await pending(rig);review=await rig.e.prepare_manual_cancel()
        if issue=='all_filled':rig.details['101']={'filled':10,'remaining':0,'rejected':False}
        if issue=='partial':rig.details['101']={'filled':3,'remaining':7,'rejected':False}
        if issue=='missing':rig.details.clear()
        if issue=='bad_remaining':rig.details['101']={'filled':3,'remaining':8,'rejected':False}
        if issue=='other_order':o['order_no']='777'
        if issue=='pause_during_send':rig.before_cancel=lambda:rig.e.pause()
        if issue=='timeout':rig.cancel_failure=OrderUnknown('timeout')
        await confirm(rig,review)
        assert len(rig.cancels)==(1 if issue in ('partial','timeout') else 0)
        assert len(rig.sends)==1
        if issue=='timeout':
            assert o['manual_cancel_state']=='unknown'
            again=await rig.e.prepare_manual_cancel();assert not again['items']
            await rig.e.reconcile();assert len(rig.cancels)==1
    asyncio.run(run())

def test_cancel_in_flight_is_excluded(rig):
    async def run():
        o=await pending(rig);rig.book_best=9400;await poll(rig)
        assert o['replace_state']=='cancel_wait'
        review=await rig.e.prepare_manual_cancel();assert not review['items'] and review['excluded']
        assert len(rig.cancels)==1
    asyncio.run(run())

def test_restart_never_replays_manual_cancel(rig):
    async def run():
        o=await pending(rig);review=await rig.e.prepare_manual_cancel();await confirm(rig,review)
        o['manual_cancel_state']='sending';rig.e.persist();rig.e.close_lock();rig.e.open()
        assert rig.e.orders[0]['manual_cancel_state']=='unknown'
        await poll(rig);assert len(rig.cancels)==1 and len(rig.sends)==1
    asyncio.run(run())

def test_manual_orders_not_in_app_journal_never_targeted(rig):
    async def run():
        rig.b.pending=[{'ord_no':'999','oso_qty':'50'}]
        review=await rig.e.prepare_manual_cancel();assert review['items']==[]
        await confirm(rig,review);assert not rig.cancels
    asyncio.run(run())

@pytest.mark.parametrize('kind',['horizon','custom'])
def test_worker_keeps_confirming_cancel_after_original_closed(kind):
    from unittest.mock import AsyncMock,Mock
    from types import SimpleNamespace
    from app.autoexit_service import ExitService
    from app.custom_exit_service import CustomExitService
    async def run():
        cls=ExitService if kind=='horizon' else CustomExitService
        service=object.__new__(cls)
        check=AsyncMock(side_effect=asyncio.CancelledError)
        service.engine=SimpleNamespace(check=Mock(),reprice=AsyncMock(),reconcile=check,pause=Mock(),
            orders=[{'status':'closed','manual_cancel_state':'accepted','balance_done':True}],plans={})
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(service.loop(),.3)
        check.assert_awaited_once()
    asyncio.run(run())

def test_cancel_routes_csrf_review_and_explicit_confirmation(rig):
    from types import SimpleNamespace
    from fastapi import FastAPI
    import httpx
    from app.autoexit_service import create_exit_router
    async def run():
        await pending(rig)
        app=FastAPI();app.include_router(create_exit_router(SimpleNamespace(engine=rig.e,token='FAKE')))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',9)),base_url='http://127.0.0.1') as c:
            assert (await c.post('/api/autoexit/cancel/prepare')).status_code==403
            headers={'x-autoexit-token':'FAKE'}
            review=(await c.post('/api/autoexit/cancel/prepare',headers=headers)).json()
            body={'review_id':review['review_id'],'environment':'real','confirm':'true'}
            assert (await c.post('/api/autoexit/cancel/confirm',headers=headers,json=body)).status_code==422
            assert not rig.cancels
            response=await c.post('/api/autoexit/cancel/confirm',headers=headers,json=body|{'confirm':True})
            assert response.status_code==200 and rig.cancels==['101']
    asyncio.run(run())
