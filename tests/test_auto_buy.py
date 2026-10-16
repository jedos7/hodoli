import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.auto_buy import AutoBuy
from app.auto_buy_service import create_auto_buy_router, BuyPrepare
from app.autoexit import AutoExit
from app.autosell import SellMonitor, Position, KST
from app.custom_exit import CustomExit
from app.exit_coordination import CoordinatedBroker
from app.kiwoom.buy_orders import KiwoomBuyOrders
from app.kiwoom.orders import NotSent, OrderUnknown, OrderRejected


class BuyBroker:
    def __init__(self):
        self.rows,self.pending,self.calls = {},[],[]
        self.cap = {'quantity':10000,'amount':100000000}
        self.response,self.failure,self.on_send,self.on_positions = None,None,None,None
        self.cancels = []

    async def positions(self):
        if self.on_positions:
            self.on_positions()
        return {k:v.copy() for k,v in self.rows.items()}

    async def stock(self,code,price):
        return {'code':code,'name':'테스트 주식'}

    async def open_orders(self,code):
        return self.pending

    async def capacity(self,code,price):
        return self.cap.copy()

    async def buy_once(self,code,quantity,price,guard):
        if self.on_send:
            self.on_send()
        guard()
        self.calls.append((code,quantity,price))
        if self.failure:
            raise self.failure
        return '0000123'

    async def sell_once(self,code,quantity,guard):
        guard()
        self.calls.append(('sell',code,quantity))
        return '0000456'

    async def detail(self,order):
        return self.response

    async def cancel_once(self,order,guard):
        guard()
        self.cancels.append(order['order_no'])
        if self.failure:
            raise self.failure
        return '0000124'


@pytest.fixture
def buy(tmp_path):
    now=[datetime(2026,9,22,10,tzinfo=KST)]; connected=[True]
    phase=[{'value':'3','at':now[0].isoformat()}]
    m=SellMonitor(tmp_path/'shadow.json'); b=BuyBroker()
    args=('paper',lambda:b,lambda:connected[0],lambda:phase[0],m,lambda:now[0])
    h=AutoExit(tmp_path/'horizon.json',*args);h.open()
    c=CustomExit(tmp_path/'custom.json',*args);c.open()
    e=AutoBuy(tmp_path/'buy.json',*args,peers=(h,c),custom=c);e.open()
    c.broker=lambda:CoordinatedBroker(CoordinatedBroker(b,h,'Horizon'),e,'Buy')
    h.broker=lambda:CoordinatedBroker(CoordinatedBroker(b,c,'Custom'),e,'Buy')
    yield SimpleNamespace(e=e,b=b,c=c,h=h,m=m,now=now,connected=connected,phase=phase)
    e.close_lock();c.close_lock();h.close_lock()


async def prepare(r,**kw):
    return await r.e.prepare(**({'code':'005930','limit_price':260000,'quantity':100,
        'max_amount':26000000,'expires_on':r.now[0].date()}|kw))


async def arm(r,**kw):
    p=await prepare(r,**kw)
    await r.e.arm(p['id'],'paper',True,bool(p['take_profit'] or p['stop_loss']))
    return r.e.plans[p['code']]


async def tick(r,price=260000,market='KRX',delta=0,code='005930'):
    r.e.quote(code,price,r.now[0]+timedelta(seconds=delta),market)
    if r.e.tasks:
        await asyncio.gather(*list(r.e.tasks))


def settle(r,qty=100,average=259000):
    r.b.response={'filled':qty,'remaining':0,'rejected':False}
    r.b.rows['005930']={'code':'005930','name':'테스트 주식','quantity':qty,'available':qty,'average':average}
    r.m.positions['005930']=Position('005930','테스트 주식',qty,average)


@pytest.mark.parametrize('price,expected',[(260001,0),(260000,1),(259000,1)])
def test_limit_once_no_market_or_repeated_buy(buy,price,expected):
    async def run():
        await arm(buy)
        for _ in range(20):
            await tick(buy,price)
        assert buy.b.calls==([('005930',100,260000)] if expected else [])
        if expected:
            assert buy.e.orders[0]['status']=='accepted' and buy.e.orders[0]['filled']==0
        assert not buy.h.plans and not buy.c.plans
    asyncio.run(run())


@pytest.mark.parametrize('market,delta',[('NXT',0),('KRX',-11),('KRX',1)])
def test_wrong_venue_stale_future_never_buy(buy,market,delta):
    async def run():
        await arm(buy);await tick(buy,market=market,delta=delta)
        assert not buy.b.calls
    asyncio.run(run())


def test_prepare_only_and_wrong_stock_never_orders(buy):
    async def run():
        await prepare(buy);await tick(buy)
        assert not buy.b.calls
        buy.e.pause();await arm(buy);await tick(buy,code='000660')
        assert not buy.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('kw',[{'quantity':True},{'quantity':0},{'quantity':1.5},{'limit_price':True},
    {'limit_price':0},{'max_amount':25999999},{'take_profit_pct':0},{'stop_loss_pct':100},
    {'take_profit_pct':float('nan')},{'stop_loss_pct':1.001}])
def test_invalid_plan_values(buy,kw):
    async def run():
        with pytest.raises(ValueError): await prepare(buy,**kw)
        assert not buy.b.calls and not buy.e.plans
    asyncio.run(run())


@pytest.mark.parametrize('kind',['cash','qty','pending','peer','unknown','peer_error','existing_link','custom_record'])
def test_preflight_blocks(buy,kind):
    async def run():
        kw={}
        if kind=='cash': buy.b.cap['amount']=25999999
        if kind=='qty': buy.b.cap['quantity']=99
        if kind=='pending': buy.b.pending=[{'ord_no':'other'}]
        if kind=='peer': buy.h.plans['005930']={'status':'armed'}
        if kind=='unknown': buy.e.orders=[{'code':'000660','status':'unknown'}]
        if kind=='peer_error': buy.h.error='broken journal'
        if kind=='existing_link': settle(buy);kw={'take_profit_pct':15}
        if kind=='custom_record': buy.c.plans['005930']={'status':'paused'};kw={'stop_loss_pct':10}
        with pytest.raises(NotSent):await prepare(buy,**kw)
        assert not buy.b.calls
    asyncio.run(run())


def test_other_armed_buy_reserves_budget(buy):
    async def run():
        await arm(buy)
        buy.b.cap['amount']=50000000
        with pytest.raises(NotSent):await prepare(buy,code='000660')
    asyncio.run(run())


def test_existing_holdings_allowed_without_exit_link(buy):
    async def run():
        settle(buy,qty=20)
        p=await arm(buy);await tick(buy)
        assert p['baseline']['quantity']==20 and len(buy.b.calls)==1
    asyncio.run(run())


@pytest.mark.parametrize('point',['arm','send','positions'])
def test_pause_during_await_wins(buy,point):
    async def run():
        if point=='arm':
            p=await prepare(buy);buy.b.on_positions=lambda:buy.e.pause()
            with pytest.raises(NotSent):await buy.e.arm(p['id'],'paper',True,False)
        else:
            await arm(buy)
            if point=='send':buy.b.on_send=lambda:buy.e.pause()
            else:buy.b.on_positions=lambda:buy.e.pause()
            await tick(buy)
        assert not buy.b.calls
        assert buy.e.plans['005930']['status']=='paused'
    asyncio.run(run())


@pytest.mark.parametrize('kind',['disconnected','sleep','clock_back','expired','auction','prior_day_phase'])
def test_connection_clock_expiry_and_session_blocks(buy,kind):
    async def run():
        await arm(buy)
        if kind=='disconnected':buy.connected[0]=False
        if kind=='sleep':buy.now[0]+=timedelta(seconds=16)
        if kind=='clock_back':buy.now[0]-=timedelta(seconds=1)
        if kind=='expired':buy.now[0]=buy.now[0].replace(hour=15,minute=20);buy.e.last_check=buy.now[0]
        if kind=='auction':buy.phase[0]['value']='2'
        if kind=='prior_day_phase':buy.phase[0]['at']=(buy.now[0]-timedelta(days=1)).isoformat()
        await tick(buy)
        assert not buy.b.calls
    asyncio.run(run())


def test_review_expiry_and_confirmation(buy):
    async def run():
        p=await prepare(buy,take_profit_pct=15)
        for env,limit,linked in [('real',True,True),('paper',False,True),('paper',True,False)]:
            with pytest.raises(NotSent):await buy.e.arm(p['id'],env,limit,linked)
        buy.now[0]+=timedelta(seconds=91)
        with pytest.raises(NotSent):await buy.e.arm(p['id'],'paper',True,True)
        assert not buy.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('exc,status',[(OrderUnknown('timeout'),'unknown'),(OrderRejected('denied'),'rejected')])
def test_ambiguous_or_rejected_never_replayed(buy,exc,status):
    async def run():
        await arm(buy);buy.b.failure=exc;await tick(buy)
        for _ in range(10):await buy.e.reconcile();await tick(buy)
        assert len(buy.b.calls)==1 and buy.e.orders[0]['status']==status
    asyncio.run(run())


def test_intent_on_disk_before_send_and_restart_disarms(buy):
    async def run():
        await arm(buy,take_profit_pct=15)
        def verify():
            saved=json.loads(buy.e.path.read_text('utf-8'))
            assert saved['orders'][0]['status']=='submitting'
        buy.b.on_send=verify;await tick(buy)
        o=buy.e.orders[0];o.update(status='submitting',order_no='');buy.e.persist()
        buy.e.close_lock();buy.e.open()
        assert buy.e.orders[0]['status']=='unknown'
        assert not buy.e.plans['005930']['continue_exit']
        await tick(buy);assert len(buy.b.calls)==1
        with pytest.raises(NotSent):await prepare(buy)
    asyncio.run(run())


def test_storage_failure_prevents_transmission(buy,monkeypatch):
    async def run():
        await arm(buy)
        def fail():raise OSError('disk full')
        monkeypatch.setattr(buy.e,'save',fail)
        with pytest.raises(OSError):await tick(buy)
        assert not buy.b.calls and buy.e.error
    asyncio.run(run())


def test_no_exit_before_full_fill_and_actual_balance_confirmation(buy):
    async def run():
        await arm(buy,take_profit_pct=15,stop_loss_pct=10);await tick(buy)
        buy.b.response={'filled':40,'remaining':60,'rejected':False}
        await buy.e.reconcile();assert not buy.c.plans
        settle(buy)
        buy.m.positions.clear();await buy.e.reconcile();assert not buy.c.plans
        buy.m.positions['005930']=Position('005930','테스트',100,259000)
        await buy.e.reconcile()
        p=buy.c.plans['005930']
        assert p['status']=='armed' and p['quantity']==100 and p['average']==259000
        assert p['take_profit']['price']=='297850.0' and p['stop_loss']['price']=='233100.0'
        assert buy.e.plans['005930']['status']=='completed'
        await buy.e.reconcile();assert buy.c.plans['005930']['id']==p['id']
        buy.c.quote('005930',297850,buy.now[0],'KRX')
        if buy.c.tasks:await asyncio.gather(*list(buy.c.tasks))
        assert buy.b.calls[-1]==('sell','005930',100)
        assert not buy.h.plans and not buy.h.orders
    asyncio.run(run())


def test_external_partial_cancel_links_only_filled_quantity(buy):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy);settle(buy,qty=40)
        await buy.e.reconcile()
        assert buy.e.orders[0]['status']=='closed'
        assert buy.c.plans['005930']['quantity']==40
    asyncio.run(run())


def test_pause_prevents_followon_exit(buy):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy);buy.e.pause();settle(buy)
        await buy.e.reconcile();assert not buy.c.plans
        assert buy.e.orders[0]['status']=='filled'
    asyncio.run(run())


def test_delayed_balance_never_links_days_later(buy):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy)
        buy.b.response={'filled':100,'remaining':0,'rejected':False}
        await buy.e.reconcile();assert buy.e.plans['005930']['status']=='remainder_check'
        buy.now[0]+=timedelta(seconds=61);settle(buy)
        await buy.e.reconcile()
        assert not buy.c.plans and buy.e.plans['005930']['status']=='paused'
    asyncio.run(run())


def test_handoff_write_failure_never_leaves_exit_armed(buy,monkeypatch):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy);settle(buy)
        def fail():raise OSError('disk full')
        monkeypatch.setattr(buy.c,'save',fail)
        await buy.e.reconcile()
        assert buy.c.error and buy.c.plans['005930']['status']=='paused'
        assert buy.e.plans['005930']['status']=='paused'
        await buy.e.reconcile();assert len(buy.b.calls)==1
    asyncio.run(run())


def test_concurrent_reconciliation_links_only_once(buy):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy);settle(buy)
        await asyncio.gather(buy.e.reconcile(),buy.e.reconcile())
        assert len(buy.c.plans)==1 and buy.e.plans['005930']['linked_exit_id']==buy.c.plans['005930']['id']
    asyncio.run(run())


def test_cancel_impossible_remaining_does_not_send(buy):
    async def run():
        await arm(buy);await tick(buy)
        buy.b.response={'filled':40,'remaining':100,'rejected':False}
        with pytest.raises(NotSent):await buy.e.cancel(buy.e.orders[0]['id'])
        assert not buy.b.cancels
    asyncio.run(run())


def test_buy_capacity_fails_closed_on_missing_cash_fields():
    async def run():
        async def post(*args):return {'profa_20ord_alowq':'10000'},''
        b=KiwoomBuyOrders(SimpleNamespace(post=post),'paper')
        with pytest.raises(KeyError):await b.capacity('005930',260000)
    asyncio.run(run())


def test_cancel_once_waits_for_original_order_close(buy):
    async def run():
        await arm(buy,stop_loss_pct=10);await tick(buy)
        buy.b.response={'filled':40,'remaining':60,'rejected':False}
        await buy.e.reconcile();o=buy.e.orders[0]
        await buy.e.cancel(o['id'])
        assert buy.b.cancels==['0000123'] and o['status']=='partial'
        assert o['cancel']['status']=='accepted' and buy.e.blocked('005930')
        with pytest.raises(NotSent):await buy.e.cancel(o['id'])
        settle(buy,qty=40);await buy.e.reconcile()
        assert o['status']=='closed' and not buy.c.plans
    asyncio.run(run())


@pytest.mark.parametrize('exc,status',[(OrderUnknown('timeout'),'unknown'),(OrderRejected('no'),'rejected')])
def test_cancel_errors_never_retried(buy,exc,status):
    async def run():
        await arm(buy);await tick(buy);buy.b.response={'filled':0,'remaining':100,'rejected':False}
        buy.b.failure=exc;o=buy.e.orders[0];await buy.e.cancel(o['id'])
        with pytest.raises(NotSent):await buy.e.cancel(o['id'])
        assert o['cancel']['status']==status and len(buy.b.cancels)==1
    asyncio.run(run())


def test_stale_deleted_plan_and_pending_plan_protected(buy):
    async def run():
        p=await arm(buy);await tick(buy);buy.e.pause()
        with pytest.raises(NotSent):await buy.e.remove_plan(p['code'],p['id'])
        settle(buy);await buy.e.reconcile()
        with pytest.raises(NotSent):await buy.e.remove_plan(p['code'],'stale')
        await buy.e.remove_plan(p['code'],p['id'])
        assert not buy.e.plans and len(buy.e.orders)==1
    asyncio.run(run())


def test_same_stock_peer_cannot_sell_while_buying(buy):
    async def run():
        await arm(buy)
        with pytest.raises(NotSent):await buy.c.broker().open_orders('005930')
        with pytest.raises(NotSent):await buy.h.broker().open_orders('005930')
        assert await buy.h.broker().open_orders('000660')==[]
    asyncio.run(run())


def test_peer_added_before_transport_blocks_buy(buy):
    async def run():
        await arm(buy)
        buy.b.on_send=lambda:buy.h.plans.update({'005930':{'status':'prepared'}})
        await tick(buy);assert not buy.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('status,body,exc',[(429,{},OrderUnknown),(500,{},OrderUnknown),
    (200,{},OrderUnknown),(200,{'return_code':1},OrderRejected),(200,{'return_code':0},OrderUnknown)])
def test_buy_and_cancel_transport_never_retries(status,body,exc):
    async def run():
        for cancel in (False,True):
            calls=[]
            async def token():return 'FAKE'
            def handle(req):calls.append(req);return httpx.Response(status,json=body)
            async with httpx.AsyncClient(base_url='https://mockapi.kiwoom.com',transport=httpx.MockTransport(handle)) as client:
                rest=SimpleNamespace(client=client,s=SimpleNamespace(kiwoom_env='paper'),
                    auth=SimpleNamespace(access_token=token),_lock=asyncio.Lock(),_gap=0,_last=0)
                b=KiwoomBuyOrders(rest,'paper')
                with pytest.raises(exc):
                    if cancel:await b.cancel_once({'code':'005930','order_no':'123'},lambda:None)
                    else:await b.buy_once('005930',100,260000,lambda:None)
            assert len(calls)==1 and calls[0].headers['api-id']==('kt10003' if cancel else 'kt10000')
            payload=json.loads(calls[0].content)
            if cancel:assert payload['cncl_qty']=='0' and payload['orig_ord_no']=='123'
            else:assert payload['trde_tp']=='0' and payload['ord_uv']=='260000' and payload['ord_qty']=='100'
    asyncio.run(run())


def test_order_detail_checks_buy_side_price_and_original():
    async def run():
        row={'ord_no':'0000123','stk_cd':'005930','ord_qty':'100','ord_uv':'260000',
             'io_tp_nm':'현금매수','cntr_qty':'40','ord_remnq':'60'}
        requests=[]
        async def post(path,api,body,key=''):
            requests.append(body);return {'acnt_ord_cntr_prps_dtl':[row]},''
        b=KiwoomBuyOrders(SimpleNamespace(post=post),'paper')
        order={'code':'005930','order_no':'123','day':'2026-09-22','quantity':100,'limit_price':260000}
        assert (await b.detail(order))['filled']==40 and requests[-1]['sell_tp']=='2'
        for kind in ('현금매도','매수취소','매수정정','신용매수'):
            row['io_tp_nm']=kind
            with pytest.raises(ValueError):await b.detail(order)
        row['io_tp_nm']='현금매수';row['ord_uv']='260500'
        with pytest.raises(ValueError):await b.detail(order)
    asyncio.run(run())


def test_api_token_origin_and_strict_payload(buy):
    async def run():
        async def noop(*args):pass
        service=SimpleNamespace(engine=buy.e,token='FAKE',sell=SimpleNamespace(start=noop,
            get_feed=lambda:SimpleNamespace(watch_codes=noop)))
        app=FastAPI();app.include_router(create_auto_buy_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
            assert (await client.get('/api/auto-buy')).status_code==200
            assert (await client.post('/api/auto-buy/pause',json={})).status_code==403
            headers={'X-Auto-Buy-Token':'FAKE'}
            assert (await client.post('/api/auto-buy/pause',json={},headers=headers|{'Origin':'https://evil.example'})).status_code==403
            assert (await client.get('/api/auto-buy',headers={'Host':'evil.example'})).status_code==403
            body={'code':'005930','limit_price':260000,'quantity':100,'max_amount':26000000,'expires_on':'2026-09-22'}
            assert (await client.post('/api/auto-buy/prepare',json=body|{'quantity':True},headers=headers)).status_code==422
            assert (await client.post('/api/auto-buy/prepare',json=body|{'extra':1},headers=headers)).status_code==422
            res=await client.post('/api/auto-buy/prepare',json=body,headers=headers)
            assert res.status_code==200 and not buy.b.calls
            pid=res.json()['id']
            res=await client.post('/api/auto-buy/arm',json={'plan_id':pid,'environment':'paper','confirm_limit':'true','confirm_exit':False},headers=headers)
            assert res.status_code==422
    asyncio.run(run())
