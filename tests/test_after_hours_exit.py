"""Offline only: broker/clock/HTTP are fakes; no account credentials or sockets."""
import asyncio
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.after_hours_exit import extended_session
from app.autosell import KST
from app.kiwoom.extended_sell import ExtendedSellOrders, QuoteWait, lower_tick
from app.kiwoom.orders import NotSent, OrderRejected, OrderUnknown
from tests.test_custom_exit import custom


@pytest.fixture
def ah(custom):
    r=custom
    r.now[0]=datetime(2026,9,22,17,tzinfo=KST)
    r.phase[0]={'value':'4','at':r.now[0].isoformat()}
    r.e.env='real';r.e.last_check=r.now[0];r.e.reprice_checked=r.now[0]
    r.sends=[];r.cancels=[];r.details={};r.confirmed=0;r.book_best=9600;r.book_lowest=9500
    r.cancel_failure=None;r.book_failure=None;r.before_cancel=None;r.before_sell=None
    r.venues=['KRX','NXT']
    async def metadata(code): return {'venues':r.venues.copy()}
    async def book(code,venue):
        if r.book_failure:raise r.book_failure
        return {'venue':venue,'at':r.now[0].isoformat(),'best':r.book_best,
                'lowest':r.book_lowest,'floor':7000,'ceiling':13000}
    async def sell(code,qty,price,venue,guard):
        if r.before_sell:r.before_sell()
        guard();r.sends.append((code,qty,price,venue))
        if r.b.failure:raise r.b.failure
        number=str(100+len(r.sends))
        r.details[number]={'filled':0,'remaining':qty,'rejected':False}
        r.b.pending=[{'ord_no':number,'oso_qty':str(qty)}]
        return number
    async def cancel(order,guard):
        if r.before_cancel:r.before_cancel()
        guard();r.cancels.append(order['order_no'])
        if r.cancel_failure:raise r.cancel_failure
        return str(500+len(r.cancels))
    async def detail(order):return copy.deepcopy(r.details.get(order['order_no']))
    async def cancel_detail(order):return {'confirmed':r.confirmed}
    async def validate(code,price):
        if price%10:raise NotSent('invalid tick')
    r.b.extended_metadata=metadata;r.b.extended_book=book
    r.b.extended_sell_once=sell;r.b.extended_cancel_once=cancel
    r.b.extended_cancel_detail=cancel_detail;r.b.detail=detail;r.b.validate_sell_limit=validate
    return r


async def arm(r,**kw):
    body=dict(code='005930',kind='loss',price=9700,comparison='lte',quantity=10,
        all_remaining=True,order_type='market',limit_price=None,expires_on=r.now[0].date(),
        after_hours='both',retry_unfilled=True,empty_book='wait')|kw
    review=await r.e.prepare_ticket(**body)
    assert not r.sends
    await r.e.arm_ticket(review['review_id'],'real',True)
    return r.e.plans['005930']


async def quote(r,market='NX',price=9600,age=0):
    r.e.quote('005930',price,r.now[0]-timedelta(seconds=age),market)
    if r.e.tasks:await asyncio.gather(*list(r.e.tasks))


async def poll(r,seconds=12,market='NX'):
    r.now[0]+=timedelta(seconds=seconds)
    await quote(r,market)
    await r.e.reprice()


def cancel_completed(r,filled=0):
    o=r.e.orders[-1]
    r.details[o['order_no']]={'filled':filled,'remaining':0,'rejected':False}
    r.confirmed=o['quantity']-filled
    expected=o['position_quantity']-filled
    r.b.row.update(quantity=expected,available=expected)
    r.m.positions['005930'].quantity=expected
    r.b.pending=[]


@pytest.mark.parametrize('venue,hm,expected',[
    ('NXT',(7,59),False),('NXT',(8,0),True),('NXT',(8,49),True),('NXT',(8,50),False),
    ('NXT',(15,39),False),('NXT',(15,40),True),('NXT',(19,59),True),('NXT',(20,0),False),
    ('KRX',(15,59),False),('KRX',(16,0),True),('KRX',(19,59),True),('KRX',(20,0),False)])
def test_current_market_windows(venue,hm,expected):
    assert extended_session(venue,datetime(2026,9,23,*hm,tzinfo=KST)) is expected
    assert not extended_session(venue,datetime(2026,9,26,*hm,tzinfo=KST))


@pytest.mark.parametrize('mode,venue',[('both','NXT'),('NXT','NXT'),('KRX','KRX')])
def test_opt_in_market_conversion_and_no_double_order(ah,mode,venue):
    async def run():
        await arm(ah,after_hours=mode)
        await quote(ah,'NX' if venue=='NXT' else 'KRX')
        for _ in range(20):await quote(ah,'KRX');await quote(ah,'NX')
        assert ah.sends==[('005930',10,9500,venue)]
        assert ah.h.orders==[] and ah.h.plans=={} and ah.e.snapshot()['enabled']
    asyncio.run(run())


def test_both_prefers_nxt_when_both_fresh_and_krx_when_only_krx(ah):
    async def run():
        p=await arm(ah)
        ah.e.extended_quotes[('005930','KRX')]=(9600,ah.now[0])
        await quote(ah)
        assert ah.sends[0][3]=='NXT'
        ah.e.pause()
    asyncio.run(run())


@pytest.mark.parametrize('mode,feed,age',[('off','KRX',0),('KRX','NX',0),('NXT','KRX',0),('both','NX',11),('both','OTHER',0)])
def test_default_or_wrong_or_stale_quotes_never_order(ah,mode,feed,age):
    async def run():
        if mode=='off':ah.now[0]=ah.now[0].replace(hour=14);ah.e.last_check=ah.now[0]
        await arm(ah,after_hours=mode,retry_unfilled=mode!='off')
        if mode=='off':ah.now[0]=ah.now[0].replace(hour=17);ah.e.last_check=ah.now[0]
        await quote(ah,feed,age=age)
        assert not ah.sends and not ah.b.calls
    asyncio.run(run())


def test_regular_session_order_is_unchanged_even_with_extended_opt_in(ah):
    async def run():
        ah.now[0]=ah.now[0].replace(hour=10);ah.phase[0]={'value':'3','at':ah.now[0].isoformat()}
        await arm(ah)
        await quote(ah,'KRX')
        assert ah.b.calls==[('005930',10)] and not ah.sends
        assert not ah.e.orders[0].get('after_hours')
    asyncio.run(run())


@pytest.mark.parametrize('fallback,sends',[('wait',0),('floor',1)])
def test_empty_book_default_wait_and_explicit_floor_only(ah,fallback,sends):
    async def run():
        p=await arm(ah,empty_book=fallback)
        ah.book_best=ah.book_lowest=None
        await quote(ah)
        assert len(ah.sends)==sends
        if sends:assert ah.sends[0][2]==7000
        else:
            assert p['status']=='armed' and not ah.e.orders
            ah.book_best=9600;ah.book_lowest=9500
            await poll(ah)
            assert ah.sends==[('005930',10,9500,'NXT')]
    asyncio.run(run())


@pytest.mark.parametrize('kw',[dict(kind='profit',comparison='gte'),dict(after_hours='off'),
    dict(order_type='limit',limit_price=9600,empty_book='floor')])
def test_invalid_retry_and_floor_modes_rejected(ah,kw):
    async def run():
        with pytest.raises(ValueError):await arm(ah,**kw)
        assert not ah.e.plans and not ah.sends
    asyncio.run(run())


def test_profit_limit_never_chases(ah):
    async def run():
        await arm(ah,kind='profit',comparison='gte',price=9500,retry_unfilled=False,
                  order_type='limit',limit_price=9800)
        await quote(ah)
        ah.book_best=9000
        await poll(ah)
        assert ah.sends==[('005930',10,9800,'NXT')] and not ah.cancels
    asyncio.run(run())


def test_partial_fill_during_cancel_replaces_only_confirmed_remainder(ah):
    async def run():
        await arm(ah);await quote(ah)
        ah.book_best=9400;ah.book_lowest=9300
        await poll(ah,11);assert not ah.cancels
        await poll(ah,1)
        assert ah.cancels==['101'] and len(ah.sends)==1
        await poll(ah);assert len(ah.sends)==1 # Accepted cancellation is not a completed cancellation.
        cancel_completed(ah,filled=3)
        await ah.e.reconcile() # A concurrent UI refresh must never send a replacement.
        assert len(ah.sends)==1 and ah.e.blocked('005930')
        await poll(ah)
        assert ah.sends[-1]==('005930',7,9390,'NXT')
        assert ah.e.orders[0]['filled']==3 and ah.e.orders[1]['retry_attempt']==1
        assert ah.e.orders[0]['balance_done'] and ah.e.orders[1]['position_quantity']==7
    asyncio.run(run())


@pytest.mark.parametrize('change',['all_filled','mismatch','manual_order','cancel_unknown','cancel_rejected','stale','pause','disconnect','sleep','expired'])
def test_never_replace_on_uncertainty_or_stop(ah,change):
    async def run():
        p=await arm(ah);await quote(ah)
        ah.book_best=9400
        if change=='cancel_unknown':ah.cancel_failure=OrderUnknown('timeout')
        if change=='cancel_rejected':ah.cancel_failure=OrderRejected('rejected')
        await poll(ah)
        if change in ('cancel_unknown','cancel_rejected'):
            await poll(ah)
            assert len(ah.cancels)==1
        else:
            cancel_completed(ah,filled=10 if change=='all_filled' else 2)
            if change=='mismatch':ah.b.row.update(quantity=9,available=9)
            if change=='manual_order':ah.b.pending=[{'ord_no':'777','oso_qty':'1'}]
            if change=='pause':ah.e.pause_rule('005930',p['id'],p['rules'][0]['id'])
            if change=='disconnect':ah.connected[0]=False
            if change=='stale':ah.book_failure=QuoteWait('stale')
            if change=='expired':ah.now[0]=ah.now[0].replace(hour=20);ah.e.last_check=ah.now[0]
            await poll(ah,16 if change=='sleep' else 12)
        assert len(ah.sends)==1
        if change!='stale':assert not any(o.get('chase_active') for o in ah.e.orders)
    asyncio.run(run())


def test_five_replacements_per_stock_day_then_keep_final_limit(ah):
    async def run():
        await arm(ah);await quote(ah)
        for attempt in range(1,6):
            ah.book_best=9500-attempt*100
            await poll(ah)
            assert len(ah.cancels)==attempt
            cancel_completed(ah)
            await poll(ah)
            assert len(ah.sends)==attempt+1
        ah.book_best=8800
        await poll(ah)
        assert len(ah.cancels)==5 and len(ah.sends)==6
        assert not ah.e.orders[-1]['chase_active'] and ah.e.blocked('005930')
        assert ah.e.orders[-1]['remaining']==10
    asyncio.run(run())


def test_restart_disables_chain_and_never_replays_cancel(ah):
    async def run():
        await arm(ah);await quote(ah);ah.book_best=9400;await poll(ah)
        ah.e.close_lock();ah.e.open()
        assert not any(o.get('chase_active') for o in ah.e.orders)
        await poll(ah)
        assert len(ah.sends)==1 and len(ah.cancels)==1
    asyncio.run(run())


def test_replacement_timeout_is_durable_and_not_replayed(ah):
    async def run():
        await arm(ah);await quote(ah);ah.book_best=9400;await poll(ah)
        cancel_completed(ah,2);ah.b.failure=OrderUnknown('timeout')
        await poll(ah);await poll(ah)
        assert len(ah.sends)==2 and ah.e.orders[-1]['status']=='unknown'
        assert ah.e.blocked('005930') and not ah.e.orders[-1]['chase_active']
        saved=json.loads(ah.e.path.read_text('utf-8'))
        assert saved['orders'][-1]['status']=='unknown'
    asyncio.run(run())


@pytest.mark.parametrize('where',['cancel','sell'])
def test_pause_during_transport_preflight_wins(ah,where):
    async def run():
        await arm(ah)
        if where=='sell':ah.before_sell=lambda:ah.e.pause()
        await quote(ah)
        if where=='sell':assert not ah.sends;return
        ah.before_cancel=lambda:ah.e.pause()
        ah.book_best=9400;await poll(ah)
        assert len(ah.sends)==1 and not ah.cancels
    asyncio.run(run())


@pytest.mark.parametrize('price,expected',[(2000,1999),(5000,4995),(20000,19990),(50000,49950),(200000,199900),(500000,499500),(83800,83700)])
def test_previous_tick_at_band_boundaries(price,expected):assert lower_tick(price)==expected


@pytest.mark.parametrize('venue',['NXT','KRX'])
@pytest.mark.parametrize('operation',['sell','cancel'])
@pytest.mark.parametrize('status',[200,429])
def test_single_attempt_explicit_venue_transport(venue,operation,status):
    async def run():
        calls=[]
        def handler(req):calls.append(req);return httpx.Response(status,json={'return_code':0,'ord_no':'123'})
        async def token():return 'FAKE'
        async with httpx.AsyncClient(base_url='https://api.kiwoom.com',transport=httpx.MockTransport(handler)) as client:
            rest=SimpleNamespace(client=client,s=SimpleNamespace(kiwoom_env='real'),auth=SimpleNamespace(access_token=token),_lock=asyncio.Lock(),_gap=0,_last=0)
            b=ExtendedSellOrders(rest,'real')
            async def send():
                if operation=='sell':return await b.extended_sell_once('005930',7,9390,venue,lambda:None)
                return await b.extended_cancel_once({'code':'005930','order_no':'100','venue':venue},lambda:None)
            if status==429:
                with pytest.raises(OrderUnknown):await send()
            else:assert await send()=='123'
        assert len(calls)==1
        payload=json.loads(calls[0].content)
        assert payload['dmst_stex_tp']==venue and payload['stk_cd']=='005930'
        assert calls[0].headers['api-id']==('kt10001' if operation=='sell' else 'kt10003')
        if operation=='sell':assert payload['trde_tp']=='0' and payload['ord_uv']=='9390' and payload['ord_qty']=='7'
        else:assert payload['cncl_qty']=='0' and payload['orig_ord_no']=='100'
    asyncio.run(run())


def book_fixture():
    data={'bid_req_base_tm':'170000'}
    for n in range(1,11):
        prefix='buy_fpr' if n==1 else f'buy_{n}th_pre'
        data[prefix+'_bid']=str(10000-n*10);data[prefix+'_req']='100'
    return data


@pytest.mark.parametrize('issue',['ok','empty','missing','stale','bad_tick','bad_bounds','unordered','ETF','NXT_unavailable'])
def test_book_validation_and_venue_specific_read(monkeypatch,issue):
    async def run():
        monkeypatch.setattr('app.kiwoom.extended_sell.now_kst',lambda:datetime(2026,9,22,17,tzinfo=KST))
        book=book_fixture();info={'stk_cd':'005930','lst_pric':'7000','upl_pric':'13000'}
        meta={'code':'005930','marketCode':'0','state':'정상','nxtEnable':'Y'};requests=[]
        if issue=='empty':book={k:('170000' if k=='bid_req_base_tm' else '0') for k in book}
        if issue=='missing':book.pop('buy_fpr_req')
        if issue=='stale':book['bid_req_base_tm']='165949'
        if issue=='bad_tick':book['buy_fpr_bid']='9999'
        if issue=='bad_bounds':info['lst_pric']='0'
        if issue=='unordered':book['buy_fpr_bid']='8000'
        if issue=='ETF':meta['marketCode']='8'
        if issue=='NXT_unavailable':meta['nxtEnable']='N'
        async def post(path,api,body):
            requests.append((api,body))
            return {'ka10100':meta,'ka10001':info,'ka10004':book}[api],''
        b=ExtendedSellOrders(SimpleNamespace(post=post),'real')
        if issue not in ('ok','empty'):
            with pytest.raises((NotSent,ValueError)):await b.extended_book('005930','NXT')
        else:
            value=await b.extended_book('005930','NXT')
            assert value['best']==(9990 if issue=='ok' else None)
            assert value['lowest']==(9900 if issue=='ok' else None)
            assert requests[-1]==('ka10004',{'stk_cd':'005930_NX'})
    asyncio.run(run())


@pytest.mark.parametrize('issue',['ok','wrong_market','wrong_original','wrong_side','rejected','absent'])
def test_confirmed_cancel_must_match_original_and_venue(issue):
    async def run():
        row={'ord_no':'501','stk_cd':'A005930','ori_ord':'101','io_tp_nm':'현금매도취소','dmst_stex_tp':'NXT','cnfm_qty':'7','acpt_tp':'확인'}
        order={'code':'005930','day':'2026-09-22','order_no':'101','cancel_no':'501','venue':'NXT'}
        if issue=='wrong_market':row['dmst_stex_tp']='KRX'
        if issue=='wrong_original':row['ori_ord']='102'
        if issue=='wrong_side':row['io_tp_nm']='현금매수취소'
        if issue=='rejected':row['acpt_tp']='거부'
        async def pages(*args):return [] if issue=='absent' else [row]
        b=ExtendedSellOrders(None,'real');b.pages=pages
        if issue=='ok':assert await b.extended_cancel_detail(order)=={'confirmed':7}
        elif issue=='absent':assert await b.extended_cancel_detail(order) is None
        else:
            with pytest.raises(NotSent):await b.extended_cancel_detail(order)
    asyncio.run(run())


def test_adapter_reads_sell_side_and_rejects_other_venue():
    async def run():
        row={'ord_no':'101','stk_cd':'A005930','ord_qty':'10','ord_uv':'9500',
             'io_tp_nm':'현금매도','cntr_qty':'3','ord_remnq':'7','dmst_stex_tp':'NXT'}
        seen=[]
        async def pages(api,body,key):seen.append(body);return [row]
        b=ExtendedSellOrders(None,'real');b.pages=pages
        order={'code':'005930','day':'2026-09-22','order_no':'101','quantity':10,
               'after_hours':True,'venue':'NXT','order_type':'limit','limit_price':9500}
        assert await b.detail(order)=={'filled':3,'remaining':7,'rejected':False}
        assert seen[-1]['sell_tp']=='1'
        row['dmst_stex_tp']='KRX'
        with pytest.raises(ValueError):await b.detail(order)
    asyncio.run(run())


def test_confirmed_cancel_waits_for_balance_sync_then_times_out(ah):
    async def run():
        await arm(ah);await quote(ah);ah.book_best=9400;await poll(ah)
        cancel_completed(ah,2);ah.m.positions['005930'].quantity=10
        for _ in range(6):await poll(ah)
        assert len(ah.sends)==1 and not ah.e.orders[-1]['chase_active']
        assert '확인 지연' in ah.e.orders[-1]['note']
    asyncio.run(run())


def test_peer_conflict_before_cancel_prevents_mutation(ah):
    async def run():
        await arm(ah);await quote(ah)
        ah.h.plans['005930']={'id':'other','code':'005930','status':'armed'}
        ah.book_best=9400;await poll(ah)
        assert len(ah.sends)==1 and not ah.cancels
    asyncio.run(run())
