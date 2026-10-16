import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.autosell import KST
from app.kiwoom.ws import KiwoomWebSocket


@pytest.mark.parametrize('nxt_state', list('PQRSTUVW'))
def test_nxt_events_do_not_replace_krx_session(monkeypatch, nxt_state):
    import app.autosell as autosell
    at = datetime(2026, 9, 22, 9, tzinfo=KST)
    monkeypatch.setattr(autosell, 'now_kst', lambda: at)
    async def run():
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def event(value):
            await ws._handle(None, json.dumps({'trnm':'REAL', 'data':[
                {'type':'0s', 'values':{'215':value, '20':'090000', '214':'000000'}}]}))
        await event('3')
        await event(nxt_state)
        assert ws.market_state['value'] == '3'
        await event('4')
        assert ws.market_state['value'] == '4'
    asyncio.run(run())


@pytest.mark.parametrize('registration', ['ensure_market', 'ensure_account'])
def test_registration_waits_for_positive_broker_ack(registration):
    async def run():
        sent = asyncio.Event()
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def send(raw): sent.set()
        ws._ws = SimpleNamespace(send=send)
        task = asyncio.create_task(getattr(ws, registration)())
        await sent.wait()
        try:
            assert not task.done(), 'Sending a REG packet is not registration confirmation'
            await ws._handle(ws._ws, json.dumps({'trnm':'REG', 'return_code':0}))
            await task
            assert getattr(ws, '_market_registered' if registration == 'ensure_market' else '_account_registered')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('return_code', [1, None, 'invalid'])
def test_registration_error_invalidates_connection(return_code):
    async def run():
        sent = asyncio.Event()
        closed = []
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def send(raw): sent.set()
        async def close(): closed.append(True)
        ws._ws = SimpleNamespace(send=send, close=close)
        task = asyncio.create_task(ws.ensure_market())
        await sent.wait()
        with pytest.raises(RuntimeError):
            await ws._handle(ws._ws, json.dumps({'trnm':'REG', 'return_code':return_code}))
        with pytest.raises(RuntimeError): await task
        assert not ws.connected and not ws.exit_ready and not ws._market_registered
        assert closed and ws.market_state is None
    asyncio.run(run())


def test_missing_registration_ack_times_out_and_closes(monkeypatch):
    import app.kiwoom.ws as module
    monkeypatch.setattr(module, 'REG_TIMEOUT', 0.01)
    async def run():
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        closed = []
        async def send(raw): pass
        async def close(): closed.append(True)
        ws._ws = SimpleNamespace(send=send, close=close)
        with pytest.raises(asyncio.TimeoutError): await ws.ensure_account()
        assert not ws.connected and not ws._account_registered and closed
    asyncio.run(run())


def test_silent_connection_is_not_healthy_and_reader_expires(monkeypatch):
    import app.kiwoom.ws as module
    monkeypatch.setattr(module, 'RECEIVE_TIMEOUT', 0.01)
    async def run():
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def recv(): await asyncio.Future()
        socket = SimpleNamespace(recv=recv)
        ws._ws = socket
        ws._received_at = module.time.monotonic() - 1
        assert not ws.connected
        with pytest.raises(asyncio.TimeoutError): await ws._receive(socket)
        assert ws._ws is None and not ws.connected
    asyncio.run(run())


def test_server_ping_is_liveness_but_not_a_stock_trade():
    async def run():
        sent = []
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def send(raw): sent.append(raw)
        ws._ws = SimpleNamespace(send=send)
        await ws._handle(ws._ws, '{"trnm":"PING"}')
        assert ws.connected and len(sent) == 1
        assert ws.last_trade_at == 0
        assert not ws.exit_ready
    asyncio.run(run())


def test_run_receives_acknowledgements_while_registering(monkeypatch):
    import app.kiwoom.ws as module
    async def run():
        queue = asyncio.Queue()
        registered = asyncio.Event()
        packets = []
        class Socket:
            async def send(self, raw):
                packet = json.loads(raw)
                packets.append(packet)
                await queue.put(json.dumps({'trnm':packet['trnm'], 'return_code':0}))
                if packet['trnm'] == 'REG': registered.set()
            async def recv(self): return await queue.get()
            async def close(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        monkeypatch.setattr(module.websockets, 'connect', lambda *a, **kw: Socket())
        async def token(): return 'FAKE'
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000, kiwoom_ws_url='wss://invalid.test'),
                            SimpleNamespace(access_token=token), lambda t: None)
        task = asyncio.create_task(ws.run(['005930']))
        try:
            await asyncio.wait_for(registered.wait(), 1)
            await asyncio.wait_for(ws.ensure_account(), 1)
            await asyncio.wait_for(ws.ensure_market(), 1)
            assert ws.exit_ready and ws._subscribed == {'005930'}
            assert [p['data'][0]['type'] for p in packets if p['trnm']=='REG'] == [['0B'], ['00','04'], ['0s']]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not ws.connected
    asyncio.run(run())


def test_stale_and_out_of_order_krx_states_do_not_reopen_session(monkeypatch):
    import app.autosell as autosell
    at = datetime(2026, 9, 22, 15, 30, tzinfo=KST)
    monkeypatch.setattr(autosell, 'now_kst', lambda: at)
    async def run():
        ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
        async def event(value, stamp):
            await ws._handle(None, json.dumps({'trnm':'REAL', 'data':[
                {'type':'0s', 'values':{'215':value, '20':stamp, '214':'000000'}}]}))
        await event('4', '153000')
        await event('2', '152959')
        await event('3', '090000')
        assert ws.market_state['value'] == '4'
    asyncio.run(run())


@pytest.mark.parametrize('item,code,market', [('005930','005930','KRX'), ('A005930','005930','KRX'),
    ('005930_NX','005930','NX'), ('005930_AL','005930','OTHER'), ('005930_NEW','005930','OTHER'),
    ('12345A','12345A','KRX')])
def test_venue_parser_does_not_treat_integrated_ticks_as_krx(item, code, market):
    from app.kiwoom.parse import parse_real
    trades = parse_real({'trnm':'REAL','data':[{'type':'0B','item':item,'values':{'10':'10000','20':'100000'}}]})
    assert len(trades) == 1
    assert (trades[0].code, trades[0].market) == (code, market)


def test_exit_connection_requires_live_loops_and_confirmed_subscriptions(tmp_path):
    from app.autoexit_service import ExitService
    ws = SimpleNamespace(exit_ready=True, reconnects=2)
    feed = SimpleNamespace(ws=ws)
    alive = SimpleNamespace(done=lambda: False)
    dead = SimpleNamespace(done=lambda: True)
    sell = SimpleNamespace(get_feed=lambda:feed, bound_feed=feed, connected_generation=2,
                           monitor=SimpleNamespace(enabled=True, error=''), task=alive)
    service = ExitService(SimpleNamespace(kiwoom_env='paper', kiwoom_app_key='fake', data_dir=tmp_path), sell)
    service.task = alive
    assert service.connected()
    ws.exit_ready = False
    assert not service.connected()
    ws.exit_ready = True
    service.task = dead
    assert not service.connected()
    service.task = alive
    sell.task = dead
    assert not service.connected()
