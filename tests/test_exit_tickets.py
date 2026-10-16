"""Offline fixtures only: no credentials, sockets, or actual broker orders."""
import asyncio
import copy
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.custom_exit_service import create_custom_exit_router
from app.exit_tickets import rules_of
from app.kiwoom.orders import KiwoomOrders, NotSent, OrderUnknown, OrderRejected
from tests.test_custom_exit import custom, tick


@pytest.fixture
def rig(custom):
    async def validate(code,price):
        if price%10:raise NotSent('tick')
    async def limit(code,qty,price,guard):
        if custom.b.on_send:custom.b.on_send()
        guard();custom.b.calls.append((code,qty,price));return '000124'
    custom.b.validate_sell_limit=validate;custom.b.sell_limit_once=limit
    return custom


def body(r,**kwargs):
    return dict(code='005930',kind='profit',price=10500,comparison='gte',quantity=4,
        all_remaining=False,order_type='market',limit_price=None,expires_on=r.now[0].date())|kwargs


async def add(r,**kwargs):
    d=await r.e.prepare_ticket(**body(r,**kwargs))
    await r.e.arm_ticket(d['review_id'],'paper',True)
    return r.e.plans['005930'],d['rule']['id']


@pytest.mark.parametrize('kind,comparison,price,quote,sends',[
    ('profit','gte',10500,10499,False),('profit','gte',10500,10500,True),
    ('loss','lte',9700,9701,False),('loss','lte',9700,9700,True),
    ('protection','lt',83800,83800,False),('protection','lt',83800,83799,True),
    ('protection','lte',83800,83800,True)])
def test_boundaries_and_only_once(rig,kind,comparison,price,quote,sends):
    async def run():
        await add(rig,kind=kind,comparison=comparison,price=price)
        for _ in range(30):await tick(rig,quote)
        assert rig.b.calls==([('005930',4)] if sends else [])
        assert rig.h.plans=={} and rig.h.orders==[]
    asyncio.run(run())


def test_legacy_vinatech_rule_survives_review_without_activation(rig):
    async def run():
        p=await rig.e.prepare('005930',profit_protection_price=83800)
        rig.e.pause();before=copy.deepcopy(rig.e.plans)
        d=await rig.e.prepare_ticket(**body(rig))
        assert rig.e.plans==before and not rig.b.calls
        await rig.e.arm_ticket(d['review_id'],'paper',True)
        old=rules_of(rig.e.plans['005930'])[0]
        assert (old['price'],old['comparison'],old['all_remaining'],old['quantity'],old['enabled'])==('83800','lt',True,10,False)
        assert rig.e.plans['005930']['rules'][-1]['enabled']
    asyncio.run(run())


@pytest.mark.parametrize('order_type', ['market','limit'])
def test_partial_fill_serializes_then_continues_and_clips_stop(rig,order_type):
    async def run():
        await add(rig,kind='loss',comparison='lte',price=9700,quantity=10)
        p,profit=await add(rig,order_type=order_type,limit_price=10500 if order_type=='limit' else None)
        await tick(rig,10500)
        assert rig.e.snapshot()['enabled'] # other condition remains queued
        rig.b.response={'filled':2,'remaining':2,'rejected':False};await rig.e.reconcile()
        await tick(rig,9600);assert len(rig.b.calls)==1
        rig.b.row.update(quantity=6,available=6);rig.b.response={'filled':4,'remaining':0,'rejected':False}
        await rig.e.reconcile();assert p['status']=='remainder_check'
        await tick(rig,9500);assert len(rig.b.calls)==1
        rig.m.positions['005930'].quantity=6;await rig.e.reconcile()
        assert p['status']=='armed' and p['quantity']==6
        await tick(rig,11000);assert len(rig.b.calls)==1
        await tick(rig,9700);assert rig.b.calls[-1]==('005930',6)
        rig.b.response={'filled':0,'remaining':6,'rejected':False};await rig.e.reconcile()
        assert p['status']=='ordered' # old filled order cannot mark new order completed
        await tick(rig,9000);assert len(rig.b.calls)==2
        rig.b.response={'filled':6,'remaining':0,'rejected':False};await rig.e.reconcile()
        await tick(rig,9000);assert p['status']=='completed' and not rig.e.snapshot()['enabled']
    asyncio.run(run())


def test_own_balance_event_before_ack_preserves_followup(rig):
    async def run():
        await add(rig,kind='loss',price=9700,comparison='lte',all_remaining=True,quantity=10)
        p,_=await add(rig)
        async def send(code,qty,guard):
            guard();rig.b.calls.append((code,qty));rig.m.positions[code].quantity=6;rig.e.account_changed();return '123'
        rig.b.sell_once=send;await tick(rig,10500)
        rig.b.row.update(quantity=6,available=6);rig.b.response={'filled':4,'remaining':0,'rejected':False};await rig.e.reconcile()
        assert p['status']=='armed' and p['rules'][0]['enabled'] and p['rules'][0]['quantity']==6
    asyncio.run(run())


@pytest.mark.parametrize('case',['manual_balance','pause_all','disconnect','sleep','restart'])
def test_no_silent_rearm(rig,case):
    async def run():
        await add(rig)
        if case=='manual_balance':rig.m.positions['005930'].quantity=9;rig.e.account_changed()
        if case=='pause_all':rig.e.pause()
        if case=='disconnect':rig.connected[0]=False
        if case=='sleep':rig.now[0]+=timedelta(seconds=16)
        if case=='restart':rig.e.close_lock();rig.e.open()
        await tick(rig,11000);rig.connected[0]=True
        for _ in range(3):await tick(rig,11000)
        assert not rig.b.calls and not any(r['enabled'] for r in rig.e.plans['005930']['rules'])
    asyncio.run(run())


@pytest.mark.parametrize('failure',[OrderUnknown('unknown'),OrderRejected('rejected')])
def test_failure_never_retries_and_stops_other_rules(rig,failure):
    async def run():
        await add(rig,price=10600);await add(rig)
        rig.b.failure=failure
        await tick(rig,11000);await tick(rig,11000)
        assert len(rig.b.calls)==1 and not rig.e.snapshot()['enabled']
    asyncio.run(run())


def test_individual_pause_during_preflight_does_not_stop_other_rule(rig):
    async def run():
        await add(rig,kind='loss',price=9700,comparison='lte',quantity=10)
        p,profit=await add(rig)
        rig.b.on_send=lambda:rig.e.pause_rule('005930',p['id'],profit)
        await tick(rig,10500);assert not rig.b.calls and p['rules'][0]['enabled']
        rig.b.on_send=None;await tick(rig,9700)
        assert rig.b.calls==[('005930',10)]
    asyncio.run(run())


def test_pause_while_order_pending_stays_off_after_fill(rig):
    async def run():
        _,stop=await add(rig,kind='loss',price=9700,comparison='lte',quantity=10)
        p,_=await add(rig);await tick(rig,10500)
        rig.e.pause_rule('005930',p['id'],stop)
        rig.b.row.update(quantity=6,available=6);rig.m.positions['005930'].quantity=6
        rig.b.response={'filled':4,'remaining':0,'rejected':False};await rig.e.reconcile()
        await tick(rig,9500);assert len(rig.b.calls)==1 and p['status']=='paused'
    asyncio.run(run())


@pytest.mark.parametrize('stage',['prepare','arm','send','pending'])
def test_expiry(stage,rig):
    async def run():
        if stage=='prepare':
            rig.now[0]=rig.now[0].replace(hour=15,minute=20)
            with pytest.raises(NotSent):await rig.e.prepare_ticket(**body(rig))
            return
        if stage=='arm':
            rig.now[0]=rig.now[0].replace(hour=15,minute=19,second=59)
            d=await rig.e.prepare_ticket(**body(rig));rig.now[0]+=timedelta(seconds=1)
            with pytest.raises(NotSent):await rig.e.arm_ticket(d['review_id'],'paper',True)
            return
        p,_=await add(rig)
        if stage=='pending':await tick(rig,10500)
        rig.now[0]=rig.now[0].replace(hour=15,minute=20);rig.e.last_check=rig.now[0]
        await tick(rig,10500)
        assert len(rig.b.calls)==(1 if stage=='pending' else 0)
        assert not p['rules'][0]['enabled']
        if stage=='pending':assert rig.e.orders[0]['status']=='accepted'
    asyncio.run(run())


@pytest.mark.parametrize('change',['pause','balance','elapsed','environment','confirm'])
def test_review_cannot_arm_after_change(rig,change):
    async def run():
        await add(rig,kind='loss',price=9700,comparison='lte')
        d=await rig.e.prepare_ticket(**body(rig))
        if change=='pause':rig.e.pause()
        if change=='balance':rig.b.row['quantity']=11;rig.b.row['available']=11
        if change=='elapsed':rig.now[0]+=timedelta(seconds=91)
        with pytest.raises(NotSent):await rig.e.arm_ticket(d['review_id'],'real' if change=='environment' else 'paper',False if change=='confirm' else True)
        assert not rig.b.calls
    asyncio.run(run())


def test_delete_off_rule_preserves_other_rules_and_journal(rig):
    async def run():
        p,rid=await add(rig)
        with pytest.raises(NotSent):rig.e.remove_rule('005930',p['id'],rid)
        rig.e.pause_rule('005930',p['id'],rid)
        p,other=await add(rig,price=12000)
        rig.e.remove_rule('005930',p['id'],rid)
        assert len(p['rules'])==1 and p['rules'][0]['id']==other and p['rules'][0]['enabled']
        assert not rig.b.calls
    asyncio.run(run())


def test_legacy_remove_never_enables(rig):
    async def run():
        await rig.e.prepare('005930',5,4,3,10,9800);rig.e.pause()
        p=rig.e.plans['005930'];rig.e.remove_rule('005930',p['id'],'legacy-profit_protection')
        assert len(p['rules'])==2 and not any(r['enabled'] for r in p['rules'])
    asyncio.run(run())


def test_durable_intent_failure_stops_all(rig,monkeypatch):
    async def run():
        await add(rig)
        def fail():raise OSError('disk full')
        monkeypatch.setattr(rig.e,'save',fail)
        with pytest.raises(OSError):await tick(rig,11000)
        assert rig.e.error and not rig.e.snapshot()['enabled'] and not rig.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('patch',[{'quantity':True},{'quantity':1.5},{'order_type':'limit','limit_price':None},
    {'comparison':'lt'},{'expires_on':'2026-12-01'},{'unknown':1}])
def test_ticket_api_validation_and_origin(rig,patch):
    async def start():pass
    service=SimpleNamespace(engine=rig.e,token='fake',sell=SimpleNamespace(start=start))
    app=FastAPI();app.include_router(create_custom_exit_router(service))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
            b=body(rig,expires_on='2026-09-22')|patch
            assert (await c.post('/api/custom-exit/tickets/prepare',json=b)).status_code==403
            h={'X-Custom-Exit-Token':'fake'}
            assert (await c.post('/api/custom-exit/tickets/prepare',json=b,headers=h|{'Origin':'https://evil.test'})).status_code==403
            assert (await c.post('/api/custom-exit/tickets/prepare',json=b,headers=h)).status_code in (409,422)
            assert not rig.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('status,reply',[(200,{'return_code':0,'ord_no':'123'}),(429,{}),(500,{})])
def test_limit_transport_exact_order_type_price_and_no_retry(status,reply):
    async def run():
        calls=[]
        def handler(request):calls.append(request);return httpx.Response(status,json=reply)
        async def token():return 'FAKE'
        async with httpx.AsyncClient(base_url='https://mockapi.kiwoom.com',transport=httpx.MockTransport(handler)) as client:
            rest=SimpleNamespace(client=client,s=SimpleNamespace(kiwoom_env='paper'),auth=SimpleNamespace(access_token=token),_lock=asyncio.Lock(),_gap=0,_last=0)
            b=KiwoomOrders(rest,'paper')
            if status==200:assert await b.sell_limit_once('005930',4,10500,lambda:None)=='123'
            else:
                with pytest.raises(OrderUnknown):await b.sell_limit_once('005930',4,10500,lambda:None)
        assert len(calls)==1
        assert json.loads(calls[0].content)==dict(dmst_stex_tp='KRX',stk_cd='005930',ord_qty='4',ord_uv='10500',trde_tp='0',cond_uv='')
    asyncio.run(run())


@pytest.mark.parametrize('market,price,ok',[('0',83800,True),('10',83801,False),('8',8135,True),('8',8132,False),('99',10000,False),('0',True,False)])
def test_limit_tick_metadata(market,price,ok):
    async def run():
        async def post(*args):return {'code':'005930','marketCode':market},''
        b=KiwoomOrders(SimpleNamespace(post=post),'paper')
        if ok:await b.validate_sell_limit('005930',price)
        else:
            with pytest.raises(NotSent):await b.validate_sell_limit('005930',price)
    asyncio.run(run())


@pytest.mark.parametrize('delay,venue',[(-11,'KRX'),(1,'KRX'),(0,'NXT')])
def test_stale_future_and_nxt_do_not_trigger(rig,delay,venue):
    async def run():
        await add(rig);rig.e.quote('005930',11000,rig.now[0]+timedelta(seconds=delay),venue)
        if rig.e.tasks:await asyncio.gather(*rig.e.tasks)
        assert not rig.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('cause',['peer','expiry','late_quote','balance','disk'])
def test_final_gate_blocks_limit_before_transport(rig,cause,monkeypatch):
    async def run():
        await add(rig,order_type='limit',limit_price=10500)
        def change():
            if cause=='peer':rig.h.plans['005930']={'status':'armed'}
            if cause=='expiry':rig.now[0]=rig.now[0].replace(hour=15,minute=20)
            if cause=='late_quote':rig.now[0]+=timedelta(seconds=11)
            if cause=='balance':rig.m.positions['005930'].quantity=9;rig.e.account_changed()
            if cause=='disk':rig.e.error='disk error'
        rig.b.on_send=change;await tick(rig,11000)
        assert not rig.b.calls
    asyncio.run(run())


def test_explicit_priority_and_fresh_quote_after_arming(rig):
    async def run():
        await add(rig,kind='loss',comparison='lte',price=9800,quantity=10)
        await add(rig,kind='protection',comparison='lt',price=9900,quantity=5)
        rig.e.quotes['005930']=(9700,rig.now[0])
        await add(rig,price=12000)
        rig.e.check();assert not rig.b.calls
        await tick(rig,9700)
        assert rig.b.calls==[('005930',5)] and rig.e.orders[-1]['reason']=='TICKET_PROTECTION'
    asyncio.run(run())


def test_partial_order_cancel_resumes_only_remaining_conditions(rig):
    async def run():
        await add(rig,kind='loss',comparison='lte',price=9700,all_remaining=True)
        p,_=await add(rig);await tick(rig,10500)
        rig.b.response={'filled':2,'remaining':0,'rejected':False}
        rig.b.row.update(quantity=8,available=8);rig.m.positions['005930'].quantity=8
        await rig.e.reconcile();await tick(rig,9700)
        assert rig.b.calls==[('005930',4),('005930',8)]
    asyncio.run(run())


@pytest.mark.parametrize('price,side,valid',[('10500','매도',True),('10400','매도',False),('10500','매도정정',False),('10500','매수',False)])
def test_limit_detail_must_match_original_order(price,side,valid):
    async def run():
        broker=KiwoomOrders(None,'paper')
        async def pages(*args):return [{'ord_no':'000123','stk_cd':'005930','ord_qty':'4','io_tp_nm':side,'ord_uv':price,'cntr_qty':'2','ord_remnq':'2'}]
        broker.pages=pages
        order=dict(day='2026-09-22',code='005930',order_no='123',quantity=4,order_type='limit',limit_price=10500)
        if valid:assert (await broker.detail(order))['filled']==2
        else:
            with pytest.raises(ValueError):await broker.detail(order)
    asyncio.run(run())
