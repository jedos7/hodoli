from datetime import date, datetime, timedelta

import pytest

from app.autosell import KST, Position, SellMonitor


def stamp(day=14, hour=10, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=KST)


@pytest.fixture
def monitor(tmp_path):
    m = SellMonitor(tmp_path / 'shadow.json')
    m.set_calendar(['2026-09-10', '2026-09-11', '2026-09-14', '2026-09-15', '2026-09-16'])
    m.positions['005930'] = Position('005930', '테스트', 10, 10000, '2026-09-10', '매수 체결 감지')
    m.enabled = True
    return m


@pytest.mark.parametrize('price,reason', [(11499, None), (11500, 'TAKE_PROFIT'), (9001, None), (9000, 'STOP_LOSS')])
def test_exact_thresholds_and_full_quantity(monitor, price, reason):
    monitor.quote('005930', price, stamp())
    signals = monitor.evaluate(stamp())
    assert [s['reason'] for s in signals] == ([] if reason is None else [reason])
    if signals:
        assert signals[0]['quantity'] == 10
        assert signals[0]['sent'] is False and signals[0]['filled'] is False
        assert monitor.positions['005930'].quantity == 10  # 신호를 체결로 취급하지 않는다.


def test_no_duplicate_signal_after_restart(monitor):
    monitor.quote('005930', 12000, stamp())
    assert len(monitor.evaluate(stamp())) == 1
    assert monitor.evaluate(stamp()) == []
    restored = SellMonitor(monitor.path)
    assert not restored.enabled
    restored.enabled = True
    restored.quote('005930', 12000, stamp())
    assert restored.evaluate(stamp()) == []


def test_d0_plus_two_sessions_and_close_auction(monitor):
    p = monitor.positions['005930']
    assert monitor.days_held(p, date(2026, 9, 10)) == 0
    assert monitor.days_held(p, date(2026, 9, 14)) == 2
    assert monitor.evaluate(stamp(11, 15, 20)) == []
    assert monitor.evaluate(stamp(14, 15, 19)) == []
    signals = monitor.evaluate(stamp(14, 15, 20))
    assert signals[0]['reason'] == 'D2_CLOSE'
    assert signals[0]['reference_price'] is None  # 종가 체결가격을 만들어내지 않는다.


def test_holidays_not_counted_and_incomplete_calendar_blocks_close(monitor):
    monitor.sessions.remove('2026-09-11')  # 가상 임시휴장일
    assert monitor.days_held(monitor.positions['005930'], date(2026, 9, 14)) == 1
    assert monitor.days_held(monitor.positions['005930'], date(2026, 9, 15)) == 2
    assert monitor.evaluate(stamp(14, 15, 20)) == []
    monitor.calendar_end = '2026-09-14'
    assert monitor.evaluate(stamp(15, 15, 20)) == []


@pytest.mark.parametrize('delta', [-16, 1])
def test_stale_or_future_quote_does_not_trigger(monitor, delta):
    monitor.quote('005930', 12000, stamp() + timedelta(seconds=delta))
    assert monitor.evaluate(stamp()) == []


def test_nxt_off_hours_and_disabled_do_not_trigger(monitor):
    monitor.quote('005930', 12000, stamp(), 'NX')
    assert monitor.evaluate(stamp()) == []
    monitor.quote('005930', 12000, stamp(14, 8, 50))
    assert monitor.evaluate(stamp(14, 8, 50)) == []
    monitor.quote('005930', 12000, stamp(14, 15, 30))
    assert monitor.evaluate(stamp(14, 15, 30)) == []
    monitor.enabled = False
    monitor.quote('005930', 12000, stamp())
    assert monitor.evaluate(stamp()) == []


def balance(qty, avg=10000):
    return {'type': '04', 'values': {'9001': 'A005930', '302': '테스트', '930': str(qty), '931': str(avg)}}


def buy():
    return {'type': '00', 'values': {'9001': 'A005930', '913': '체결', '907': '2',
                                   '909': '123', '910': '10000', '911': '5'}}


def test_actual_buy_date_partial_fills_and_additions_do_not_extend_d2(monitor):
    monitor.sync_holdings([], stamp(10))
    monitor.account_event(buy(), stamp(10))
    monitor.account_event(balance(5), stamp(10))
    monitor.account_event(balance(5), stamp(10))  # 동일 잔고 이벤트 중복
    assert monitor.positions['005930'].quantity == 5
    monitor.account_event(buy(), stamp(11))
    monitor.account_event(balance(10, 11000), stamp(11))
    assert monitor.positions['005930'].quantity == 10
    assert monitor.positions['005930'].average == 11000
    assert monitor.positions['005930'].bought_on == '2026-09-10'
    monitor.account_event(balance(0), stamp(14))
    monitor.account_event(buy(), stamp(15))
    monitor.account_event(balance(5), stamp(15))
    assert monitor.positions['005930'].bought_on == '2026-09-15'


def test_preexisting_position_needs_date_confirmation(monitor):
    monitor.positions.clear()
    monitor.sync_holdings([{'stk_cd': 'A005930', 'rmnd_qty': '10', 'pur_pric': '10000'}], stamp())
    monitor.account_event(buy(), stamp())
    assert monitor.positions['005930'].bought_on is None
    assert monitor.evaluate(stamp(15, 15, 20)) == []
    monitor.set_buy_date('005930', date(2026, 9, 10))
    assert monitor.evaluate(stamp(14, 15, 20))[0]['reason'] == 'D2_CLOSE'


def test_invalid_date_and_credit_excluded(monitor):
    with pytest.raises(ValueError):
        monitor.set_buy_date('005930', date(2026, 9, 12))
    monitor.sync_holdings([{'stk_cd': 'A005930', 'rmnd_qty': '10', 'pur_pric': '10000', 'crd_tp': '03'}], stamp())
    assert not monitor.positions


def test_reconnect_cannot_guess_rebuy_date(monitor):
    monitor.sync_holdings([{'stk_cd': 'A005930', 'rmnd_qty': '10', 'pur_pric': '10000'}], stamp(15))
    assert monitor.positions['005930'].bought_on is None
    assert monitor.evaluate(stamp(15, 15, 20)) == []


def test_websocket_account_registration_is_read_only():
    import asyncio
    import json
    from types import SimpleNamespace
    from app.kiwoom.ws import KiwoomWebSocket
    packets, events = [], []
    ws = None
    class FakeSocket:
        async def send(self, raw):
            packets.append(json.loads(raw))
            await ws._handle(self, json.dumps({'trnm': 'REG', 'return_code': 0}))
    async def check():
        nonlocal ws
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        ws._ws = FakeSocket()
        ws.on_account = events.append
        await ws.ensure_account()
        await ws.ensure_account()
        assert len(packets) == 1
        assert packets[0]['trnm'] == 'REG'
        assert packets[0]['data'][0]['type'] == ['00', '04']
        await ws._handle(ws._ws, json.dumps({'trnm': 'REAL', 'data': [balance(10)]}))
        assert events == [balance(10)]
    asyncio.run(check())


def test_missed_close_does_not_pretend_it_sold(monitor):
    assert monitor.evaluate(stamp(14, 16)) == []
    assert not monitor.positions['005930'].signal
    assert monitor.evaluate(stamp(15, 15, 20))[0]['reason'] == 'OVERDUE_CLOSE'


def test_router_exposes_only_shadow_mode(tmp_path):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    from app.autosell_service import SellService, create_router

    service = SellService(SimpleNamespace(data_dir=tmp_path, kiwoom_env='paper', kiwoom_app_key='test'), lambda: None)
    app = FastAPI()
    app.include_router(create_router(service))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = (await client.get('/api/autosell')).json()
            assert result['mode'] == 'shadow' and result['live_orders'] is False
            assert result['rules']['take_profit_pct'] == 15
            assert result['rules']['hold_sessions'] == 2
            assert (await client.post('/api/autosell/buy-date', json={'code': '005930', 'bought_on': 'nonsense'})).status_code == 422
            assert (await client.post('/api/autosell/live')).status_code == 404
    asyncio.run(check())


@pytest.mark.parametrize('price,reason', [(11500, 'TAKE_PROFIT'), (9000, 'STOP_LOSS')])
def test_threshold_touch_between_timer_ticks_is_not_lost(tmp_path, monkeypatch, price, reason):
    from types import SimpleNamespace
    import app.autosell_service as module
    service = module.SellService(SimpleNamespace(data_dir=tmp_path, kiwoom_env='paper', kiwoom_app_key='test'), lambda: None)
    m = service.monitor
    m.set_calendar(['2026-09-14'])
    m.positions['005930'] = Position('005930', '가상 테스트', 10, 10000, '2026-09-14')
    m.enabled = True
    monkeypatch.setattr(module, 'now_kst', lambda: stamp())
    # 1초 타이머가 실행되기 전에 문턱을 찍고 평단으로 되돌아온다.
    service.quote(SimpleNamespace(code='005930', price=price, time='100000', market='KRX'))
    service.quote(SimpleNamespace(code='005930', price=10000, time='100000', market='KRX'))
    m.evaluate(stamp())
    assert len(m.history) == 1
    assert m.history[0]['reason'] == reason
    assert m.history[0]['reference_price'] == price
    assert m.history[0]['quantity'] == 10
    assert not m.history[0]['sent'] and not m.history[0]['filled']


@pytest.mark.parametrize('price,reason', [(11500, 'TAKE_PROFIT'), (9000, 'STOP_LOSS')])
def test_d0_d1_still_check_fresh_threshold_at_1520(monitor, price, reason):
    # D+2 청산일이 아닌 보유분까지 15:20부터 일괄 검사를 끄면 안 된다.
    at = stamp(11, 15, 20)
    monitor.quote('005930', price, at)
    assert monitor.evaluate(at)[0]['reason'] == reason


@pytest.mark.parametrize('price,reason', [(11500, 'TAKE_PROFIT'), (9000, 'STOP_LOSS')])
def test_fill_to_quote_to_status_api_without_orders(tmp_path,monkeypatch,price,reason):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from fastapi import FastAPI
    import app.autosell_service as module
    from app.kiwoom.parse import parse_real
    service=module.SellService(SimpleNamespace(data_dir=tmp_path,kiwoom_env='paper',kiwoom_app_key='test'),lambda:None)
    service.monitor.set_calendar(['2026-09-14'])
    service.monitor.enabled=True
    monkeypatch.setattr(module,'now_kst',lambda:stamp())
    service.account(buy())
    service.account(balance(10))
    packet={'trnm':'REAL','data':[{'type':'0B','item':'005930','values':{'10':str(price),'20':'100000'}}]}
    trade=parse_real(packet)[0]
    service.quote(trade)
    service.quote(trade)  # 같은 체결 재수신
    app=FastAPI();app.include_router(module.create_router(service))
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            d=(await client.get('/api/autosell')).json()
            assert d['positions'][0]['signal']['reason']==reason
            assert d['positions'][0]['signal']['quantity']==10
            assert len(d['history'])==1 and not d['history'][0]['sent'] and not d['live_orders']
            assert d['positions'][0]['quantity']==10
    asyncio.run(check())


def test_quote_record_failure_does_not_break_market_feed(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import app.autosell_service as module
    service=module.SellService(SimpleNamespace(data_dir=tmp_path,kiwoom_env='paper',kiwoom_app_key='test'),lambda:None)
    service.monitor.enabled=True
    monkeypatch.setattr(module,'now_kst',lambda:stamp())
    def fail(at):raise OSError('disk failure')
    monkeypatch.setattr(service.monitor,'evaluate',fail)
    service.quote(SimpleNamespace(code='005930',price=11500,time='100000',market='KRX'))
    assert not service.monitor.enabled and '기록 실패' in service.monitor.error
