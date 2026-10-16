"""Offline, broker-labelled session bootstrap and one-shot exit regression."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.autosell import KST, Position
from app.kiwoom.ws import KiwoomWebSocket
from tests.test_custom_exit import custom
from tests.test_exit_tickets import rig, add


def trade(at, **changes):
    values = {'290': '2', '20': at.strftime('%H%M%S'), '10': '85600', '15': '+1'}
    values.update(changes.pop('values', {}))
    return {'type': '0B', 'item': '005930', 'values': values} | changes


async def receive(ws, *items):
    await ws._handle(ws._ws, json.dumps({'trnm': 'REAL', 'data': list(items)}))


@pytest.fixture
def session(monkeypatch):
    import app.autosell as module
    now = [datetime(2026, 9, 23, 14, 30, tzinfo=KST)]
    monkeypatch.setattr(module, 'now_kst', lambda: now[0])
    ws = KiwoomWebSocket(SimpleNamespace(kiwoom_amount_unit=1000000), None, lambda t: None)
    ws._ws = SimpleNamespace()
    return ws, now


def test_intraday_login_uses_fresh_broker_session_label(session):
    ws, now = session
    async def run():
        assert ws.market_phase() is None
        await receive(ws, trade(now[0]))
        assert ws.market_state is None  # No session-transition notification.
        assert ws.market_phase() == {'value': '3', 'at': now[0].isoformat(), 'source': 'KRX_0B_290'}
        now[0] += timedelta(seconds=11)
        assert ws.market_phase() is None
        await receive(ws, trade(now[0]))
        assert ws.market_phase()['value'] == '3'
        ws._ws = None
        assert ws.market_phase() is None
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    {'item': '005930_NX'}, {'item': '005930_AL'}, {'item': '005930_NEW'},
    {'item': ''}, {'type': '0A'}, {'values': {'290': ''}},
    {'values': {'290': '1'}}, {'values': {'290': '3'}},
    {'values': {'20': '142949'}}, {'values': {'20': '143001'}},
    {'values': {'20': ''}}, {'values': {'20': '1430'}},
    {'values': {'10': '0'}}, {'values': {'15': '0'}},
])
def test_unknown_stale_future_or_other_venue_never_opens_session(session, change):
    ws, now = session
    async def run():
        await receive(ws, trade(now[0], **change))
        assert ws.market_phase() is None
    asyncio.run(run())


@pytest.mark.parametrize('hour,minute', [(8,59),(15,20),(15,30),(16,0)])
def test_trade_label_does_not_bootstrap_auction_or_extended_hours(session, hour, minute):
    ws, now = session
    now[0] = now[0].replace(hour=hour, minute=minute)
    async def run():
        await receive(ws, trade(now[0]))
        assert ws.market_phase() is None
    asyncio.run(run())


def test_bootstrap_expires_at_auction_boundary_even_with_fresh_last_trade(session):
    ws, now = session
    now[0] = now[0].replace(hour=15, minute=19, second=59)
    async def run():
        await receive(ws, trade(now[0]))
        assert ws.market_phase()['value'] == '3'
        now[0] += timedelta(seconds=1)
        assert ws.market_phase() is None
    asyncio.run(run())


@pytest.mark.parametrize('value', ['0','2','4','8','9','a','b','c','d'])
def test_explicit_krx_state_overrides_trade_bootstrap(session, value):
    ws, now = session
    async def run():
        await receive(ws, trade(now[0]))
        await receive(ws, {'type':'0s','values':{'215':value,'20':now[0].strftime('%H%M%S')}})
        now[0] += timedelta(seconds=1)
        await receive(ws, trade(now[0]))
        assert ws.market_phase()['value'] == value
    asyncio.run(run())


def test_nxt_notification_does_not_override_krx_bootstrap(session):
    ws, now = session
    async def run():
        await receive(ws, trade(now[0]))
        await receive(ws, {'type':'0s','values':{'215':'R','20':now[0].strftime('%H%M%S')}})
        assert ws.market_phase()['value'] == '3'
    asyncio.run(run())


def test_malformed_krx_notification_blocks_until_valid_notification(session):
    ws, now = session
    async def run():
        await receive(ws, trade(now[0]))
        await receive(ws, {'type':'0s','values':{'215':'4','20':'bad'}})
        await receive(ws, trade(now[0]))
        assert ws.market_phase() is None
        await receive(ws, {'type':'0s','values':{'215':'3','20':now[0].strftime('%H%M%S')}})
        assert ws.market_phase()['value'] == '3'
    asyncio.run(run())


@pytest.mark.parametrize('active', [False, True])
def test_85900_exit_after_intraday_connect_sends_only_once_when_user_armed(rig, session, active):
    ws, now = session
    rig.now[0] = now[0]
    rig.b.row.update(quantity=340, available=340, average=81217)
    rig.m.positions['005930'] = Position('005930','테스트',340,81217,'2026-09-18')
    rig.e.phase = ws.market_phase
    ws.on_trade = lambda tr: rig.e.quote(tr.code, tr.price, now[0], tr.market)
    async def run():
        p, rule_id = await add(rig, kind='protection', comparison='lte', price=85900, quantity=340)
        if not active:
            rig.e.pause_rule(p['code'], p['id'], rule_id)
        # A chart price / missing session label cannot start an order.
        await receive(ws, trade(now[0], values={'290': ''}))
        assert not rig.b.calls
        await receive(ws, trade(now[0], values={'10': '86000'}))
        assert not rig.b.calls
        for _ in range(20):
            await receive(ws, trade(now[0], values={'10': '85900'}))
            if rig.e.tasks:
                await asyncio.gather(*list(rig.e.tasks))
        assert rig.b.calls == ([('005930',340)] if active else [])
        assert len(rig.e.orders) == (1 if active else 0)
        assert rig.h.plans == {} and rig.h.orders == []
    asyncio.run(run())


def test_exit_service_uses_the_verified_session_accessor(tmp_path, session):
    from app.autoexit_service import ExitService
    ws, now = session
    alive = SimpleNamespace(done=lambda:False)
    feed = SimpleNamespace(ws=ws)
    sell = SimpleNamespace(get_feed=lambda:feed, bound_feed=feed, connected_generation=0,
        task=alive, monitor=SimpleNamespace(enabled=True,error=''))
    service = ExitService(SimpleNamespace(kiwoom_env='paper',kiwoom_app_key='fake',data_dir=tmp_path),sell)
    service.task = alive
    async def run():
        ws._account_registered = ws._market_registered = True
        await receive(ws, trade(now[0]))
        assert service.phase()['source'] == 'KRX_0B_290'
        ws._market_registered = False
        assert service.phase() is None
    asyncio.run(run())


def test_periodic_checks_preserve_the_reason_a_condition_was_paused(rig):
    async def run():
        p, _ = await add(rig)
        rig.e.pause(note='실시간 연결 중단 · 계좌 확인 후 다시 켜세요')
        for _ in range(3):
            rig.e.check()
        assert p['status'] == 'paused'
        assert p['note'] == '실시간 연결 중단 · 계좌 확인 후 다시 켜세요'
        assert not rig.b.calls
    asyncio.run(run())


def test_old_or_weekend_trade_cannot_bootstrap_session(session):
    ws, now = session
    async def run():
        await receive(ws, trade(now[0]))
        now[0] += timedelta(days=1)
        assert ws.market_phase() is None
        now[0] = now[0].replace(day=26)
        await receive(ws, trade(now[0]))
        assert ws.market_phase() is None
    asyncio.run(run())
