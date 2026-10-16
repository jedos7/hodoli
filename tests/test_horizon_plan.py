import asyncio
from datetime import date, datetime, timedelta
import pytest
from app.autosell import KST
from app.horizon_plan import HorizonExit, exit_prices
from app.kiwoom.orders import OrderUnknown
from tests.test_autoexit import rig


@pytest.fixture
def v2(rig, tmp_path):
    r=rig
    r.e.close_lock()
    r.e=HorizonExit(tmp_path/'v2.json','real',lambda:r.b,lambda:r.connected[0],lambda:r.phase[0],r.m,lambda:r.now[0])
    r.e.open()
    r.m.sessions={'2026-10-05','2026-10-06','2026-10-07','2026-10-08','2026-10-09'}
    r.m.calendar_start='2026-10-05';r.m.calendar_end='2026-10-09'
    r.now[0]=datetime(2026,10,6,10,tzinfo=KST);r.phase[0]={'value':'3','at':r.now[0].isoformat()}
    async def valid(*a):pass
    async def limit(code,qty,price,guard):
        guard();r.b.calls.append((code,qty,price))
        if r.b.failure:raise r.b.failure
        return '123'
    async def cancel(order,guard):guard();r.b.calls.append(('cancel',order['order_no']));return '456'
    async def cancel_detail(order):return {'confirmed':order['quantity']-order['filled']}
    r.b.validate_sell_limit=valid;r.b.sell_limit_once=limit
    r.b.extended_cancel_once=cancel;r.b.extended_cancel_detail=cancel_detail
    yield r
    r.e.close_lock()


async def arm(r):
    p=await r.e.prepare('005930',date(2026,10,5))
    await r.e.arm(p['id'],'real',True)
    return r.e.plans['005930']


async def drain(r):
    if r.e.tasks:await asyncio.gather(*list(r.e.tasks))


def test_prices():
    assert exit_prices(20700)==(20850,23850)
    assert exit_prices(20701)[0]==20850
    assert exit_prices(19999)[0]==20100


def test_profit_70_limit_and_remaining_watch(v2):
    async def run():
        p=await arm(v2)
        v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
        assert v2.b.calls==[('005930',7,11500)]
        v2.b.response={'filled':7,'remaining':0,'rejected':False}
        v2.b.row.update(quantity=3,available=3)
        await v2.e.reconcile()
        assert p['status']=='armed' and p['quantity']==3 and p['profit_done']
        v2.e.quote('005930',12000,v2.now[0],'KRX');await drain(v2)
        assert len(v2.b.calls)==1
        v2.e.quote('005930',9000,v2.now[0],'KRX');await drain(v2)
        assert v2.b.calls[-1]==('005930',3)
    asyncio.run(run())


def test_d2_not_d1(v2):
    async def run():
        p=await arm(v2)
        for day,expected in [(6,None),(7,'D2_CLOSE')]:
            at=datetime(2026,10,day,15,20,tzinfo=KST)
            v2.phase[0]={'value':'2','at':at.isoformat()}
            assert v2.e.reason(p,at)==expected
    asyncio.run(run())


def test_first_open_below_then_cancel_then_close(v2):
    async def run():
        p=await arm(v2)
        v2.now[0]=datetime(2026,10,7,9,0,1,tzinfo=KST)
        v2.phase[0]={'value':'3','at':v2.now[0].isoformat()};v2.e.last_check=v2.now[0]
        v2.e.quote('005930',9900,v2.now[0],'KRX');await drain(v2)
        assert v2.b.calls==[('005930',10,10050)]
        v2.b.response={'filled':2,'remaining':8,'rejected':False}
        v2.now[0]=datetime(2026,10,7,15,18,tzinfo=KST);v2.e.last_check=v2.now[0]
        v2.e.check();await drain(v2)
        assert v2.b.calls[-1]==('cancel','123')
        await v2.e.reconcile()
        assert p['status']=='ordered'
        v2.b.response={'filled':2,'remaining':0,'rejected':False};v2.b.row.update(quantity=8,available=8)
        # confirmed cancellation must match filled count returned by broker
        v2.e.orders[-1]['filled']=2
        await v2.e.reconcile()
        assert p['status']=='armed' and p['quantity']==8
        v2.now[0]=datetime(2026,10,7,15,20,tzinfo=KST);v2.phase[0]={'value':'2','at':v2.now[0].isoformat()};v2.e.last_check=v2.now[0]
        v2.e.check();await drain(v2)
        assert v2.b.calls[-1]==('005930',8)
    asyncio.run(run())


def test_unknown_submission_never_replayed(v2):
    async def run():
        await arm(v2);v2.b.failure=OrderUnknown('unknown')
        for _ in range(2):v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
        assert len(v2.b.calls)==1 and v2.e.orders[-1]['status']=='unknown'
    asyncio.run(run())


def test_manual_pause_prevents_resume(v2):
    async def run():
        p=await arm(v2);v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
        v2.e.pause('005930');v2.b.response={'filled':7,'remaining':0,'rejected':False};v2.b.row.update(quantity=3,available=3)
        await v2.e.reconcile();assert p['status']=='paused'
    asyncio.run(run())


def test_balance_lag_does_not_resubmit(v2):
    async def run():
        p=await arm(v2);v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
        v2.b.response={'filled':7,'remaining':0,'rejected':False}
        await v2.e.reconcile();assert p['status']=='ordered'
        v2.e.check();await drain(v2);assert len(v2.b.calls)==1
        v2.b.row.update(quantity=3,available=3);await v2.e.reconcile();assert p['status']=='armed'
    asyncio.run(run())


def test_open_above_base_never_triggers_on_later_dip(v2):
    async def run():
        p=await arm(v2)
        v2.now[0]=datetime(2026,10,7,9,0,1,tzinfo=KST)
        v2.phase[0]={'value':'3','at':v2.now[0].isoformat()};v2.e.last_check=v2.now[0]
        v2.e.quote('005930',10100,v2.now[0],'KRX');await drain(v2)
        v2.e.quote('005930',9900,v2.now[0],'KRX');await drain(v2)
        assert not v2.b.calls
    asyncio.run(run())


def test_unknown_cancel_blocks_close(v2):
    async def run():
        p=await arm(v2);v2.e.quote('005930',11500,v2.now[0],'KRX');await drain(v2)
        v2.b.response={'filled':0,'remaining':7,'rejected':False}
        async def bad(*a):raise OrderUnknown('cancel unknown')
        v2.b.extended_cancel_once=bad
        v2.now[0]=datetime(2026,10,7,15,18,tzinfo=KST);v2.e.last_check=v2.now[0]
        v2.e.check();await drain(v2)
        assert p['status']=='paused'
        v2.now[0]=datetime(2026,10,7,15,20,tzinfo=KST);v2.e.last_check=v2.now[0];v2.phase[0]={'value':'2','at':v2.now[0].isoformat()}
        v2.e.check();await drain(v2)
        assert len(v2.b.calls)==1
    asyncio.run(run())


def test_holiday_not_counted(v2):
    async def run():
        p=await arm(v2);v2.m.sessions.remove('2026-10-06')
        assert not v2.e.due(p,datetime(2026,10,7,15,20,tzinfo=KST))
    asyncio.run(run())


def test_premarket_breakeven_uses_nxt_only(v2):
    async def run():
        p=await arm(v2)
        p['after_hours']='both'
        for r in p['rules']:r.update(after_hours='both',after_hours_venues=['NXT','KRX'])
        v2.now[0]=datetime(2026,10,7,8,0,1,tzinfo=KST);v2.e.last_check=v2.now[0]
        async def extended(code,quantity,price,venue,guard):
            guard();v2.b.calls.append((code,quantity,price,venue));return '123'
        v2.b.extended_sell_once=extended
        v2.e.quote('005930',9900,v2.now[0],'NX');await drain(v2)
        assert v2.b.calls==[('005930',10,10050,'NXT')]
    asyncio.run(run())
