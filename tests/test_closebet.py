import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.autoexit import AutoExit
from app.autosell import KST, SellMonitor
from app.closebet import (CloseBet, bars_240, crossed_up, evaluate, in_window, late_amount, macd, normalize_settings,
                          plan_quantity, stoch, BUY_WINDOW, SCAN_WINDOW)
from app.kis.rest import Candle
from app.kiwoom.orders import OrderRejected, OrderUnknown


# ── 지표·판정 ──
def test_crossed_up_level_and_series():
    assert crossed_up([15, 25], level=20) and not crossed_up([25, 30], level=20) and not crossed_up([25, 15], level=20)
    assert crossed_up([1, 3], other=[2, 2]) and not crossed_up([3, 4], other=[2, 2])


def test_bars_240_groups_morning_and_afternoon():
    hourly = [('20261016090000', 10, 12, 9, 11, 1), ('20261016100000', 11, 13, 10, 12, 1), ('20261016120000', 12, 12, 11, 11, 1),
              ('20261016130000', 11, 14, 11, 13, 1), ('20261016150000', 13, 13, 12, 12, 1), ('20261017090000', 12, 12, 11, 11, 1)]
    b = bars_240(hourly)
    assert b == [('20261016', 10, 13, 9, 11), ('20261016', 11, 14, 11, 12), ('20261017', 12, 12, 11, 11)]


def _daily(closes, today='20261017'):
    out = []
    base = datetime(2026, 8, 1)
    for i, c in enumerate(closes):
        d = (base + timedelta(days=i)).strftime('%Y%m%d') if i < len(closes) - 1 else today
        out.append(Candle(d, c, c * 1.02, c * 0.98, c, 1000, c * 1000))
    return out


def _hourly(days=20, trend=1.0):
    """앞 (days-3)일은 내리고 마지막 3일은 trend 로 오르는 60분봉 → 스토캐스틱이 바닥에서 올라오는 모양."""
    out, px = [], 100.0
    for i in range(days):
        day = (datetime(2026, 9, 1) + timedelta(days=i)).strftime('%Y%m%d')
        for hh in ('09', '10', '11', '12', '13', '14', '15'):
            px *= 0.995 if i < days - 3 else trend
            out.append((f'{day}{hh}0000', px, px * 1.01, px * 0.99, px, 100))
    return out


def test_evaluate_passes_on_fresh_crosses_and_quiet_day():
    closes = [100 - i for i in range(36)] + [66, 70]   # 36일 하락 뒤 이틀 반등: 오늘 MACD·스토캐스틱(12,5,5) 둘 다 돌파
    daily = _daily(closes)
    t = daily[-1]
    daily[-1] = Candle(t.date, 64, 74, 64, 70, 1000, 0)           # 범위 위치 60% (고가 근처 아님)
    row = {'code': '000001', 'name': '테스트', 'amount_eok': 400, 'chg': 6.0}
    ev = evaluate(row, daily, _hourly(trend=1.003), 300, '20261017')
    assert ev['checks']['amount'][0] and ev['checks']['prev'][0] and ev['checks']['high'][0]
    assert ev['checks']['macd'][0] and ev['checks']['stoch'][0] and ev['checks']['stoch240'][0]
    assert ev['pass']


def test_evaluate_rejects_near_high_prev_spike_and_missing_today():
    daily = _daily([100 + i for i in range(40)])
    t = daily[-1]
    daily[-1] = Candle(t.date, 130, 140, 130, 139, 1000, 0)          # 범위 90% → 고가 근처
    daily[-2] = Candle(daily[-2].date, 120, 130, 120, 130, 1000, 0)  # 전날 급등
    daily[-3] = Candle(daily[-3].date, 120, 120, 120, 120, 1000, 0)
    row = {'code': '000001', 'name': '테스트', 'amount_eok': 400, 'chg': 1.0}
    ev = evaluate(row, daily, _hourly(), 300, '20261017')
    assert not ev['checks']['high'][0] and not ev['checks']['prev'][0] and not ev['pass']
    stale = evaluate(row, daily, _hourly(), 300, '20261018')
    assert not stale['pass'] and 'data' in stale['checks']
    assert not evaluate(row | {'amount_eok': 100}, daily, _hourly(), 300, '20261017')['checks']['amount'][0]


# ── 사이징·설정 ──
def test_plan_quantity_caps():
    assert plan_quantity(10000, 500, 400, 1_000_000, 3_000_000, 0, 10**8)[0] == 100
    assert plan_quantity(10000, 50, 400, 1_000_000, 3_000_000, 0, 10**8)[0] == 50       # 호가잔량
    assert plan_quantity(10000, 500, 400, 1_000_000, 300_000, 0, 10**8)[0] == 30       # 총액 잔여
    assert plan_quantity(10000, 500, 400, 1_000_000, 3_000_000, 2_000_000, 10**8)[0] == 20   # 15:00~15:20 대금의 10%
    assert plan_quantity(10000, 500, 400, 1_000_000, 3_000_000, 0, 7)[0] == 7           # 주문가능 수량
    assert plan_quantity(0, 500, 400, 1_000_000, 3_000_000, 0, 10**8)[0] == 0
    assert plan_quantity(2_000_000, 5, 5, 1_000_000, 3_000_000, 0, 10**8)[0] == 0
    assert late_amount([('20261017145500', 1, 1, 1, 100, 10), ('20261017150000', 1, 1, 1, 100, 10), ('20261017151500', 1, 1, 1, 200, 5)], '20261017') == 2000


def test_settings_limits():
    s = normalize_settings({'mode': 'dry', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 5, 'total_cap': 2_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    assert s['enabled'] and s['max_positions'] == 5
    for bad in ({'total_cap': 10_000_000}, {'max_positions': 6}, {'per_trade_amount': 5_000_000, 'total_cap': 3_000_000}, {'sell_time': '09:10'}, {'mode': 'x'}):
        with pytest.raises(ValueError):
            normalize_settings(s | bad)


def test_windows():
    assert in_window(datetime(2026, 10, 19, 15, 15, 0, tzinfo=KST), SCAN_WINDOW) and not in_window(datetime(2026, 10, 19, 15, 18, tzinfo=KST), SCAN_WINDOW)
    assert in_window(datetime(2026, 10, 19, 15, 19, 50, tzinfo=KST), BUY_WINDOW) and not in_window(datetime(2026, 10, 19, 15, 19, 49, tzinfo=KST), BUY_WINDOW)


# ── 엔진 (가짜 증권사) ──
class Broker:
    def __init__(self):
        self.calls, self.rows, self.failure = [], {}, None
        self.ask, self.ask_qty, self.bid_qty = 10000, 300, 300
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def ranking(self, market):
        return [{'code': '000001', 'name': '종목A', 'price': 10000, 'amount_eok': 500, 'chg': 3.0}] if market == '001' else []

    async def daily(self, code):
        closes = [100 - i for i in range(36)] + [66, 70]
        d = _daily(closes, today='20261019')
        d[-1] = Candle('20261019', 64, 74, 64, 70, 1000, 0)
        return d

    async def minutes(self, code, scope):
        if scope == '60':
            return _hourly(trend=1.003)
        return [('20261019150500', 1, 1, 1, 10000, 500)]

    async def book(self, code):
        return {'ask': self.ask, 'ask_qty': self.ask_qty, 'bid': self.ask - 50, 'bid_qty': self.bid_qty, 'at': '151950'}

    async def stock(self, code, price):
        return {'code': code, 'name': '종목A'}

    async def capacity(self, code, price):
        return {'quantity': 10000, 'amount': 100000000}

    async def positions(self):
        return {k: v.copy() for k, v in self.rows.items()}

    async def open_orders(self, code):
        return []

    async def buy_once(self, code, qty, price, guard):
        guard()
        self.calls.append(('buy', code, qty, price))
        if self.failure:
            raise self.failure
        return '0001'

    async def sell_market_once(self, code, qty, guard):
        guard()
        self.calls.append(('sell', code, qty))
        if self.failure:
            raise self.failure
        return '0002'

    async def detail(self, order):
        return self.detail_response


@pytest.fixture
def eng(tmp_path):
    now = [datetime(2026, 10, 19, 15, 15, 0, tzinfo=KST)]
    connected = [True]
    phase = [{'value': '3', 'at': now[0].isoformat()}]
    b = Broker()
    m = SellMonitor(tmp_path / 'shadow.json')
    peer = AutoExit(tmp_path / 'peer.json', 'real', lambda: b, lambda: connected[0], lambda: phase[0], m, lambda: now[0])
    peer.open()
    notes = []
    e = CloseBet(tmp_path / 'closebet.json', 'real', lambda: b, lambda: connected[0], lambda: phase[0], m, lambda: now[0], peers=(peer,), notify=notes.append)
    e.open()
    yield SimpleNamespace(e=e, b=b, now=now, connected=connected, phase=phase, peer=peer, notes=notes, path=tmp_path / 'closebet.json')
    e.close_lock()
    peer.close_lock()


def run(coro):
    return asyncio.run(coro)


async def tick_and_wait(e):
    await e.tick()
    if e.tasks:
        await asyncio.gather(*list(e.tasks))


def test_dry_mode_full_cycle_records_without_sending(eng):
    eng.e.update_settings({'mode': 'dry', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(tick_and_wait(eng.e))
    assert eng.e.scans['2026-10-19']['passed'] == ['000001'] and '후보 1종목' in eng.notes[-1]
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 50, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == []                                   # 흉내: 전송 없음
    p = eng.e.positions['000001']
    assert p['dry'] and p['quantity'] == 50 and p['average'] == 10000 and p['status'] == 'held'
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.e.positions['000001']['status'] == 'sold' and eng.b.calls == []
    assert [o['side'] for o in eng.e.orders] == ['buy', 'sell'] and all(o['dry'] for o in eng.e.orders)


def test_live_buy_journals_intent_then_sends_once_and_settles(eng):
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 50, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == [('buy', '000001', 50, 10000)], eng.e.day()
    o = eng.e.orders[-1]
    assert o['status'] == 'accepted' and o['order_no'] == '0001' and o['side'] == 'buy' and not o['dry']
    run(eng.e.tick())                                          # 같은 창에서 두 번 안 산다
    assert len(eng.b.calls) == 1
    eng.b.detail_response = {'filled': 50, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 50, 'available': 50, 'average': 10020.0}
    eng.now[0] = datetime(2026, 10, 19, 15, 31, 0, tzinfo=KST)
    run(eng.e.tick())
    p = eng.e.positions['000001']
    assert p['status'] == 'held' and p['quantity'] == 50 and p['average'] == 10020.0 and not p['dry']
    assert eng.e.invested() == 50 * 10020.0
    # 다음 날 08:40 시장가 매도 → 체결 → sold
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls[-1] == ('sell', '000001', 50) and eng.e.positions['000001']['status'] == 'selling'
    eng.b.detail_response = {'filled': 50, 'remaining': 0, 'rejected': False}
    run(eng.e.reconcile())
    assert eng.e.positions['000001']['status'] == 'sold'
    eng.now[0] = datetime(2026, 10, 20, 9, 3, 0, tzinfo=KST)
    run(eng.e.tick())
    assert len([c for c in eng.b.calls if c[0] == 'sell']) == 1


def test_live_unknown_reply_blocks_resend_and_restart_keeps_positions(eng):
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.b.failure = OrderUnknown('timeout')
    eng.now[0] = datetime(2026, 10, 19, 15, 20, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.e.orders[-1]['status'] == 'unknown' and eng.e.blocked('000001')
    eng.b.failure = None
    # 보유가 있는 상태에서 재시작 → 보유 유지, 주문은 unknown 유지
    eng.e.positions['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 10, 'average': 10000, 'bought_on': '2026-10-19', 'status': 'held', 'dry': False}
    eng.e.persist()
    eng.e.close_lock()
    e2 = CloseBet(eng.path, 'real', lambda: eng.b, lambda: True, lambda: eng.phase[0], SellMonitor(eng.path.parent / 's2.json'), lambda: eng.now[0])
    e2.open()
    try:
        assert e2.positions['000001']['status'] == 'held' and e2.settings['mode'] == 'live' and e2.orders[-1]['status'] == 'unknown'
        eng.now[0] = datetime(2026, 10, 20, 8, 45, 0, tzinfo=KST)
        run(e2.tick())
        assert e2.orders[-1]['status'] == 'unknown' and not any(c[0] == 'sell' for c in eng.b.calls)   # 미확정 주문이 있는 종목은 새 주문을 만들지 않는다
        assert '보류' in e2.positions['000001']['note']
    finally:
        e2.close_lock()
    eng.e.open()


def test_stop_blocks_sending_and_peer_conflict_skips(eng):
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.e.stop()
    eng.now[0] = datetime(2026, 10, 19, 15, 20, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == [] and not eng.e.settings['enabled']
    eng.e.settings['enabled'] = True
    eng.peer.plans['000001'] = {'code': '000001', 'id': 'x', 'status': 'armed'}
    eng.e.days['2026-10-19'].pop('bought', None)
    run(eng.e.tick())
    assert eng.b.calls == [] and any('다른 자동매매' in s for s in eng.e.day()['skipped'])


def test_live_requires_real_env_and_rejected_order(eng, tmp_path):
    b = Broker()
    paper = CloseBet(tmp_path / 'paper.json', 'paper', lambda: b, lambda: True, lambda: None, SellMonitor(tmp_path / 's.json'), lambda: eng.now[0])
    paper.open()
    try:
        with pytest.raises(ValueError):
            paper.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 100_000, 'max_positions': 1, 'total_cap': 100_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    finally:
        paper.close_lock()
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.b.failure = OrderRejected('거부')
    eng.now[0] = datetime(2026, 10, 19, 15, 20, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.e.orders[-1]['status'] == 'rejected' and '000001' not in eng.e.positions
