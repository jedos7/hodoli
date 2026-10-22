import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.autosell import KST, Position, SellMonitor
from app.autoexit import AutoExit
from app.autoexit_service import create_exit_router
from app.kiwoom.orders import KiwoomOrders, OrderRejected, OrderUnknown, NotSent, cash_positions


class Broker:
    def __init__(self):
        self.row = {'code': '005930', 'name': '테스트', 'quantity': 10, 'available': 10, 'average': 10000}
        self.pending, self.calls, self.response = [], [], None
        self.failure, self.on_positions, self.on_send = None, None, None

    async def positions(self):
        if self.on_positions:
            self.on_positions()
        return {'005930': self.row.copy()}

    async def open_orders(self, code):
        return self.pending

    async def sell_once(self, code, quantity, guard):
        if self.on_send:
            self.on_send()
        guard()
        self.calls.append((code, quantity))
        if self.failure:
            raise self.failure
        return '0000123'

    async def detail(self, order):
        return self.response


@pytest.fixture
def rig(tmp_path):
    now = [datetime(2026, 9, 21, 10, tzinfo=KST)]
    connected = [True]
    phase = [{'value': '3', 'at': now[0].isoformat(), 'remaining': 0}]
    m = SellMonitor(tmp_path/'shadow.json')
    m.sessions = {'2026-09-18', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-28'}
    m.calendar_start, m.calendar_end = '2026-09-18', '2026-09-28'
    m.positions['005930'] = Position('005930', '테스트', 10, 10000, '2026-09-18')
    b = Broker()
    e = AutoExit(tmp_path/'orders.json', 'paper', lambda:b, lambda:connected[0], lambda:phase[0], m, lambda:now[0])
    e.open()
    r = SimpleNamespace(e=e,b=b,now=now,connected=connected,phase=phase,m=m)
    yield r
    e.close_lock()


async def arm(r):
    p = await r.e.prepare('005930', date(2026,9,18))
    await r.e.arm(p['id'], 'paper', True)
    return r.e.plans['005930']


async def drain(r):
    if r.e.tasks:
        await asyncio.gather(*list(r.e.tasks))


@pytest.mark.parametrize('price,reason', [(11499,None),(11500,'TAKE_PROFIT'),(9001,None),(9000,'STOP_LOSS')])
def test_threshold_full_sell_and_no_duplicate(rig, price, reason):
    async def run():
        await arm(rig)
        for _ in range(30):
            rig.e.quote('005930',price,rig.now[0],'KRX')
        rig.e.quote('005930',10000,rig.now[0],'KRX')  # Brief touch is preserved during preflight.
        await drain(rig)
        assert rig.b.calls == ([] if reason is None else [('005930',10)])
        if reason:
            assert rig.e.orders[-1]['reason'] == reason
            assert rig.e.orders[-1]['status'] == 'accepted'
            assert rig.e.orders[-1]['filled'] == 0
            assert rig.m.positions['005930'].quantity == 10
    asyncio.run(run())


@pytest.mark.parametrize('delta,market', [(-11,'KRX'),(1,'KRX'),(0,'NX')])
def test_stale_future_and_nxt_never_send(rig,delta,market):
    async def run():
        await arm(rig)
        rig.e.quote('005930',12000,rig.now[0]+timedelta(seconds=delta),market)
        await drain(rig)
        assert not rig.b.calls
    asyncio.run(run())


def test_friday_buy_exits_tuesday_d2_only_in_closing_auction(rig):
    async def run():
        await arm(rig)
        rig.now[0]=datetime(2026,9,21,15,20,tzinfo=KST)
        rig.e.last_check=rig.now[0]
        rig.phase[0]={'value':'2','at':rig.now[0].isoformat(),'remaining':600}
        rig.e.check(); await drain(rig)
        assert not rig.b.calls  # Monday is D+1, with Saturday/Sunday excluded.
        rig.now[0]=datetime(2026,9,22,15,20,tzinfo=KST)
        rig.e.last_check=rig.now[0]
        rig.phase[0]=None
        rig.e.check(); await drain(rig)
        assert not rig.b.calls  # Clock alone is insufficient.
        rig.phase[0]={'value':'2','at':rig.now[0].isoformat(),'remaining':600}
        rig.e.check(); await drain(rig)
        assert rig.b.calls == [('005930',10)]
        assert rig.e.orders[0]['reason']=='D2_CLOSE'
        assert rig.e.snapshot()['rules']['hold_sessions']==2
    asyncio.run(run())


def test_missing_calendar_and_expired_auction_block_close(rig):
    async def run():
        await arm(rig)
        rig.now[0]=datetime(2026,9,28,15,20,tzinfo=KST)
        rig.e.last_check=rig.now[0]
        rig.m.calendar_end='2026-09-23'
        rig.phase[0]={'value':'2','at':rig.now[0].isoformat(),'remaining':600}
        rig.e.check(); await drain(rig)
        assert not rig.b.calls
        rig.m.calendar_end='2026-09-28'
        rig.now[0]+=timedelta(minutes=10)
        rig.e.last_check=rig.now[0]
        rig.e.check(); await drain(rig)
        assert not rig.b.calls
    asyncio.run(run())


def test_delayed_market_close_uses_broker_session(rig):
    async def run():
        await arm(rig)
        rig.now[0]=datetime(2026,9,22,16,20,tzinfo=KST)
        rig.e.last_check=rig.now[0]
        rig.phase[0]={'value':'2','at':rig.now[0].isoformat(),'remaining':600}
        rig.e.check(); await drain(rig)
        assert rig.b.calls == [('005930',10)]
    asyncio.run(run())


def test_d2_skips_an_actual_holiday(rig):
    async def run():
        await arm(rig)
        rig.m.sessions.remove('2026-09-21')  # Simulated Monday market holiday.
        for day in (22,23):
            rig.now[0]=datetime(2026,9,day,15,20,tzinfo=KST)
            rig.e.last_check=rig.now[0]
            rig.phase[0]={'value':'2','at':rig.now[0].isoformat(),'remaining':600}
            rig.e.check(); await drain(rig)
            assert len(rig.b.calls)==(0 if day==22 else 1)
        assert rig.e.orders[0]['reason']=='D2_CLOSE'
    asyncio.run(run())


def test_previous_three_day_plan_requires_new_review(rig):
    async def run():
        p=await rig.e.prepare('005930',date(2026,9,18))
        rig.e.plans['005930']['hold_sessions']=3
        with pytest.raises(NotSent,match='보유기간 설정이 변경'):
            await rig.e.arm(p['id'],'paper',True)
        assert not rig.e.snapshot()['enabled']
        assert not rig.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('case',['manual_order','quantity','average','available','disconnect','pause','timeout_preflight'])
def test_preflight_changes_do_not_send(rig,case):
    async def run():
        await arm(rig)
        if case=='manual_order': rig.b.pending=[{'ord_no':'999'}]
        elif case=='quantity': rig.b.row['quantity']=11
        elif case=='average': rig.b.row['average']=9000
        elif case=='available': rig.b.row['available']=9
        elif case=='disconnect': rig.b.on_positions=lambda:rig.connected.__setitem__(0,False)
        elif case=='pause': rig.b.on_send=lambda:rig.e.pause()
        elif case=='timeout_preflight': rig.b.on_positions=lambda:rig.now.__setitem__(0,rig.now[0]+timedelta(seconds=11))
        rig.e.quote('005930',12000,rig.now[0],'KRX')
        await drain(rig)
        assert not rig.b.calls
        assert rig.e.plans['005930']['status']=='paused'
    asyncio.run(run())


@pytest.mark.parametrize('failure,status',[(OrderUnknown('timeout'),'unknown'),(OrderRejected('rejected'),'rejected')])
def test_uncertain_and_rejected_orders_are_never_replayed(rig,failure,status):
    async def run():
        await arm(rig)
        rig.b.failure=failure
        rig.e.quote('005930',9000,rig.now[0],'KRX'); await drain(rig)
        for _ in range(4):
            rig.e.quote('005930',8000,rig.now[0],'KRX'); rig.e.check()
            await rig.e.reconcile()
        assert len(rig.b.calls)==1
        assert rig.e.orders[0]['status']==status
        if status=='unknown':
            with pytest.raises(NotSent): await rig.e.prepare('005930',date(2026,9,18))
    asyncio.run(run())


def test_partial_fills_are_absolute_and_missing_order_is_not_fill(rig):
    async def run():
        await arm(rig)
        rig.e.quote('005930',12000,rig.now[0],'KRX'); await drain(rig)
        await rig.e.reconcile()
        assert rig.e.orders[0]['filled']==0
        rig.b.response={'filled':4,'remaining':6,'rejected':False}
        await rig.e.reconcile(); await rig.e.reconcile()
        assert rig.e.orders[0]['filled']==4
        assert rig.e.orders[0]['status']=='partial'
        rig.b.response={'filled':10,'remaining':0,'rejected':False}
        await rig.e.reconcile()
        assert rig.e.orders[0]['status']=='filled'
        assert len(rig.b.calls)==1
    asyncio.run(run())


def test_disconnect_restart_and_account_change_require_rearming(rig):
    async def run():
        await arm(rig)
        rig.m.positions['005930'].quantity=9
        rig.e.account_changed()
        assert rig.e.plans['005930']['status']=='paused'
        await arm(rig)
        rig.connected[0]=False
        rig.e.check()
        assert rig.e.plans['005930']['status']=='paused'
        rig.connected[0]=True
        await arm(rig)
        rig.e.close_lock()
        rig.e.open()
        assert not rig.e.snapshot()['enabled']
        assert not rig.b.calls
    asyncio.run(run())


def test_stop_during_arm_preflight_wins(rig):
    async def run():
        p=await rig.e.prepare('005930',date(2026,9,18))
        rig.b.on_positions=lambda:rig.e.pause()
        with pytest.raises(NotSent): await rig.e.arm(p['id'],'paper',True)
        assert not rig.e.snapshot()['enabled']
    asyncio.run(run())


def test_submission_intent_persisted_and_restart_blocks_duplicate(rig):
    async def run():
        await arm(rig)
        def persisted():
            saved=json.loads(rig.e.path.read_text('utf-8'))
            assert saved['orders'][0]['status']=='submitting'
            assert saved['orders'][0]['quantity']==10
        rig.b.on_send=persisted
        rig.e.quote('005930',12000,rig.now[0],'KRX'); await drain(rig)
        rig.e.orders[0]['status']='submitting';rig.e.orders[0]['order_no']='';rig.e.save()
        rig.e.close_lock();rig.e.open()
        assert rig.e.orders[0]['status']=='unknown'
        with pytest.raises(NotSent): await rig.e.prepare('005930',date(2026,9,18))
    asyncio.run(run())


def test_file_lock_and_write_failure_prevent_orders(rig,monkeypatch):
    other=AutoExit(rig.e.path,'paper',None,lambda:True,lambda:None,rig.m)
    other.open()
    assert other.error and not other.owner
    async def run():
        await arm(rig)
        def fail(): raise OSError('disk full')
        monkeypatch.setattr(rig.e,'save',fail)
        with pytest.raises(OSError): rig.e.quote('005930',12000,rig.now[0],'KRX')
        assert not rig.e.snapshot()['enabled'] and rig.e.error
        assert not rig.b.calls
    asyncio.run(run())


def test_new_and_unselected_positions_are_not_armed(rig):
    async def run():
        await arm(rig)
        rig.m.positions['000660']=Position('000660','다른종목',20,10000)
        rig.e.quote('000660',7000,rig.now[0],'KRX'); await drain(rig)
        assert not rig.b.calls
    asyncio.run(run())


def test_sleep_resume_never_silently_rearms(rig):
    async def run():
        await arm(rig)
        rig.now[0]+=timedelta(minutes=5)
        rig.e.quote('005930',8000,rig.now[0],'KRX')
        await drain(rig)
        assert not rig.b.calls
        assert rig.e.plans['005930']['status']=='paused'
        assert '절전' in rig.e.plans['005930']['note']
    asyncio.run(run())


@pytest.mark.parametrize('status,body,error',[(429,{},OrderUnknown),(500,{},OrderUnknown),
    (200,{'return_code':1},OrderRejected),(200,{'return_code':0},OrderUnknown)])
def test_order_http_transport_never_retries(status,body,error):
    async def run():
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(status,json=body)
        async def token():return 'FAKE-TEST-TOKEN'
        async with httpx.AsyncClient(base_url='https://mockapi.kiwoom.com',transport=httpx.MockTransport(handler)) as client:
            rest=SimpleNamespace(client=client,s=SimpleNamespace(kiwoom_env='paper'),auth=SimpleNamespace(access_token=token),
                                 _lock=asyncio.Lock(),_gap=0,_last=0)
            with pytest.raises(error): await KiwoomOrders(rest,'paper').sell_once('005930',10,lambda:None)
        assert len(calls)==1
        assert calls[0].headers['api-id']=='kt10001'
        assert json.loads(calls[0].content)=={'dmst_stex_tp':'KRX','stk_cd':'005930','ord_qty':'10','ord_uv':'','trde_tp':'3','cond_uv':''}
    asyncio.run(run())


def test_environment_mismatch_and_late_pause_do_not_reach_transport():
    async def run():
        calls=[]
        async def token():return 'FAKE-TEST-TOKEN'
        async with httpx.AsyncClient(base_url='https://mockapi.kiwoom.com',transport=httpx.MockTransport(lambda r:calls.append(r))) as client:
            rest=SimpleNamespace(client=client,s=SimpleNamespace(kiwoom_env='paper'),auth=SimpleNamespace(access_token=token),
                                 _lock=asyncio.Lock(),_gap=0,_last=0)
            with pytest.raises(NotSent): await KiwoomOrders(rest,'real').sell_once('005930',10,lambda:None)
            def stopped(): raise NotSent('stopped')
            with pytest.raises(NotSent): await KiwoomOrders(rest,'paper').sell_once('005930',10,stopped)
        assert not calls
    asyncio.run(run())


def test_local_token_and_origin_required(rig):
    async def run():
        service=SimpleNamespace(engine=rig.e,token='test-token')
        app=FastAPI();app.include_router(create_exit_router(service))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
            assert (await c.get('/api/autoexit')).status_code==200
            assert (await c.post('/api/autoexit/pause',json={})).status_code==403
            assert (await c.post('/api/autoexit/pause',json={},headers={'X-Autoexit-Token':'test-token','Origin':'https://evil.example'})).status_code==403
            assert (await c.get('/api/autoexit',headers={'Host':'evil.example'})).status_code==403
            assert (await c.post('/api/autoexit/pause',json={},headers={'X-Autoexit-Token':'test-token','Origin':'http://127.0.0.1:8000'})).status_code==200
    asyncio.run(run())


def test_complete_pagination_and_cash_only():
    async def run():
        keys=[]
        async def post(path,api,body,key):
            keys.append(key)
            row={'stk_cd':'A005930','stk_nm':'테스트','rmnd_qty':'10','trde_able_qty':'10','pur_pric':'10000','crd_tp':'00'}
            return ({'acnt_evlt_remn_indv_tot':[row] if key else []}, '' if key else 'next')
        b=KiwoomOrders(SimpleNamespace(post=post),'paper')
        assert (await b.positions())['005930']['quantity']==10
        assert keys==['','next']
        async def loop(*args):return {'x':[]},'same'
        b.rest.post=loop
        with pytest.raises(ValueError):await b.pages('test',{},'x')
    asyncio.run(run())
    assert cash_positions([{'crd_tp':'03','rmnd_qty':'10'}])=={}


def test_market_subscription_and_session_decoding(monkeypatch):
    from app.kiwoom.ws import KiwoomWebSocket
    import app.autosell as autosell
    at=datetime(2026,9,23,15,20,tzinfo=KST)
    monkeypatch.setattr(autosell,'now_kst',lambda:at)
    async def run():
        sent=[]
        async def send(raw):
            sent.append(json.loads(raw))
            await ws._handle(ws._ws,json.dumps({'trnm':'REG','return_code':0}))
        ws=KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000),None,lambda t:None)
        ws._ws=SimpleNamespace(send=send)
        await ws.ensure_market();await ws.ensure_market()
        assert len(sent)==1 and sent[0]['data'][0]['type']==['0s']
        event={'trnm':'REAL','data':[{'type':'0s','item':'','values':{'215':'2','20':'152000','214':'001000'}}]}
        await ws._handle(ws._ws,json.dumps(event))
        assert ws.market_state=={'value':'2','at':at.isoformat()}
        event['data'][0]['values']['215']='4'
        await ws._handle(ws._ws,json.dumps(event))
        assert ws.market_state['value']=='4'
    asyncio.run(run())


def test_official_order_detail_is_cumulative_not_open_order_absence():
    async def run():
        body_seen=[]
        async def post(path,api,body,key):
            body_seen.append((api,body))
            return {'acnt_ord_cntr_prps_dtl':[{'ord_no':'0000123','stk_cd':'A005930',
                'ord_qty':'0000000010','cntr_qty':'0000000004','ord_remnq':'0000000006',
                'io_tp_nm':'현금매도','acpt_tp':'접수'}]},''
        b=KiwoomOrders(SimpleNamespace(post=post),'paper')
        result=await b.detail({'order_no':'0000123','code':'005930','quantity':10,'day':'2026-09-21'})
        assert result=={'filled':4,'remaining':6,'rejected':False}
        assert body_seen[0][0]=='kt00007' and body_seen[0][1]['ord_dt']=='20260921'
    asyncio.run(run())


def test_app_starts_exit_controller_once_and_closes_it(monkeypatch):
    from unittest.mock import AsyncMock
    import app.main as main
    start,stop=AsyncMock(),AsyncMock()
    monkeypatch.setattr(main,'start_feed',AsyncMock())
    monkeypatch.setattr(main,'stop_feed',AsyncMock())
    monkeypatch.setattr(main,'exit_service',SimpleNamespace(start=start,stop=stop))
    custom_start,custom_stop=AsyncMock(),AsyncMock()
    monkeypatch.setattr(main,'custom_exit_service',SimpleNamespace(start=custom_start,stop=custom_stop))
    monkeypatch.setattr(main.horizon_service,'stop',AsyncMock())
    monkeypatch.setattr(main.sell_service,'stop',AsyncMock())
    async def idle():await asyncio.Future()
    for name in ['broadcaster','market_loop','news_loop','overnight_loop','autocollect']:
        monkeypatch.setattr(main,name,idle)
    monkeypatch.setattr(main.scheduler,'loop',idle)
    async def run():
        async with main.lifespan(main.app):
            start.assert_awaited_once()
            custom_start.assert_awaited_once()
            stop.assert_not_awaited()
        stop.assert_awaited_once()
        custom_stop.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('opening_countdown', [0, None, 600])
def test_d2_auction_does_not_depend_on_opening_countdown(rig, opening_countdown):
    async def run():
        await arm(rig)
        rig.now[0] = datetime(2026, 9, 22, 15, 20, tzinfo=KST)
        rig.e.last_check = rig.now[0]
        rig.phase[0] = {'value': '2', 'at': rig.now[0].isoformat(), 'remaining': opening_countdown}
        rig.e.check()
        await drain(rig)
        assert rig.b.calls == [('005930', 10)]
        assert rig.e.orders[0]['reason'] == 'D2_CLOSE'
    asyncio.run(run())


def test_old_auction_signal_cannot_authorize_new_order(rig):
    async def run():
        await arm(rig)
        rig.now[0] = datetime(2026, 9, 22, 15, 22, tzinfo=KST)
        rig.e.last_check = rig.now[0]
        rig.phase[0] = {'value': '2', 'at': (rig.now[0]-timedelta(minutes=2)).isoformat(), 'remaining': 600}
        rig.e.check()
        await drain(rig)
        assert not rig.b.calls
    asyncio.run(run())


def test_clock_backwards_during_preflight_blocks_submission(rig):
    async def run():
        await arm(rig)
        # Market signal stays earlier than the rolled-back wall clock.
        rig.phase[0]['at'] = (rig.now[0]-timedelta(minutes=5)).isoformat()
        rig.b.on_positions = lambda: rig.now.__setitem__(0, rig.now[0]-timedelta(seconds=5))
        rig.e.quote('005930', 9000, rig.now[0], 'KRX')
        await drain(rig)
        assert not rig.b.calls
        assert rig.e.plans['005930']['status'] == 'paused'
    asyncio.run(run())


def test_concurrent_reconciliation_does_not_overlap(rig):
    async def run():
        await arm(rig)
        rig.e.quote('005930', 9000, rig.now[0], 'KRX')
        await drain(rig)
        count = 0
        async def detail(order):
            nonlocal count
            count += 1
            await asyncio.sleep(0)
            return {'filled': 10, 'remaining': 0, 'rejected': False}
        rig.b.detail = detail
        await asyncio.gather(rig.e.reconcile(), rig.e.reconcile())
        assert count == 1
        assert rig.e.orders[0]['status'] == 'filled'
        assert rig.e.plans['005930']['status'] == 'completed'
    asyncio.run(run())


# ── ⏩ 청산날 앞당기기 (app/earlyclose.py 가 부른다) ────────────
def test_close_early_sells_at_market_on_the_close_day(rig):
    """원래 15:20 종가에 팔 종목을 정규장에서 지금 시장가로 판다. D+2(청산날)에만 된다."""
    async def run():
        await arm(rig)
        rig.now[0] = datetime(2026, 9, 21, 15, 13, tzinfo=KST)        # 아직 청산날이 아님
        rig.phase[0] = {'value': '3', 'at': rig.now[0].isoformat(), 'remaining': 0}
        assert rig.e.due_today() == []
        with pytest.raises(NotSent, match='청산날'):
            await rig.e.close_early('005930')
        assert rig.b.calls == []

        rig.now[0] = datetime(2026, 9, 22, 15, 13, tzinfo=KST)        # 청산날
        rig.phase[0] = {'value': '3', 'at': rig.now[0].isoformat(), 'remaining': 0}
        assert rig.e.due_today() == ['005930']
        await rig.e.close_early('005930', '⏩ 시험')
        await drain(rig)
        assert rig.b.calls == [('005930', 10)]
        order = rig.e.orders[-1]
        assert order['reason'] == 'D2_CLOSE_EARLY' and order['status'] == 'accepted'
        assert rig.e.plans['005930']['status'] == 'ordered'
    asyncio.run(run())


def test_close_early_refuses_outside_the_regular_session(rig):
    """장 상태가 정규장이 아니면 보내지 않는다 (종가 단일가·장 마감 뒤 포함)."""
    async def run():
        await arm(rig)
        rig.now[0] = datetime(2026, 9, 22, 15, 13, tzinfo=KST)
        rig.phase[0] = {'value': '2', 'at': rig.now[0].isoformat(), 'remaining': 0}   # 종가 단일가
        with pytest.raises(NotSent, match='정규장'):
            await rig.e.close_early('005930')
        assert rig.b.calls == []
    asyncio.run(run())
