import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.autoexit import AutoExit
from app.autosell import KST, SellMonitor
from app.closebet import (STOCH_SETS, CloseBet, bars_240, buy_price, crossed_within, evaluate, in_window,
                          late_amount, macd, normalize_settings, plan_quantity, stoch_k, turned_up_from_low,
                          BUY_WINDOW, SCAN_WINDOW)
from app.models import Candle
from app.kiwoom.orders import OrderRejected, OrderUnknown


# ── 지표·판정 ──
def test_crossed_within_looks_back_not_just_today():
    a, b = [1, 3, 4, 5], [2, 2, 2, 2]          # 3번째 봉에서 돌파, 오늘은 이미 위
    assert crossed_within(a, b, 10) and not crossed_within(a, b, 1)
    assert not crossed_within([3, 4, 5], [2, 2, 2], 10)   # 처음부터 위 = 돌파 아님


def test_turned_up_from_low_needs_recent_low_above_now_and_rising():
    assert turned_up_from_low([10, 15, 22, 25], 20, 10)          # 최근 20 이하 → 지금 위 → 오름
    assert not turned_up_from_low([10, 15, 30, 25], 20, 10)      # 어제보다 내림
    assert not turned_up_from_low([10, 15, 18, 19], 20, 10)      # 아직 20 아래
    assert not turned_up_from_low([30, 40, 50, 60], 20, 2)       # 최근 2봉에 20 이하가 없음


def test_bars_240_groups_morning_and_afternoon():
    hourly = [('20261016090000', 10, 12, 9, 11, 1), ('20261016100000', 11, 13, 10, 12, 1), ('20261016120000', 12, 12, 11, 11, 1),
              ('20261016130000', 11, 14, 11, 13, 1), ('20261016150000', 13, 13, 12, 12, 1), ('20261017090000', 12, 12, 11, 11, 1)]
    b = bars_240(hourly)
    assert b == [('20261016', 10, 13, 9, 11), ('20261016', 11, 14, 11, 12), ('20261017', 12, 12, 11, 11)]


def _rally(decline=55, days=4, step=6.0):
    """55일 하락 뒤 4일 +6% 반등 — MACD 10일 내 돌파·스토캐스틱 셋 다 20 재돌파를 만족한다."""
    base = [100 - i * 0.9 for i in range(decline)]
    return base + [base[-1] * (1 + step / 100) ** (j + 1) for j in range(days)]


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


def test_evaluate_passes_when_all_documented_conditions_hold():
    closes = _rally()
    daily = _daily(closes)
    last = closes[-1]
    daily[-1] = Candle(daily[-1].date, last * 0.94, last * 1.02, last * 0.93, last, 1000, 0)   # 장중 위치 약 76%
    row = {'code': '000001', 'name': '테스트', 'price': 12000, 'amount_eok': 400, 'chg': 6.0}
    ev = evaluate(row, daily, _hourly(trend=1.003), 300, daily[-1].date)
    assert all(ok for ok, _ in ev['checks'].values()), ev['checks']
    assert ev['pass']
    assert len(STOCH_SETS) == 3 and STOCH_SETS[0] == (5, 3, 3)


def test_evaluate_needs_all_three_daily_stochastics():
    """(20,12,12) 는 느려서 짧은 반등으로는 20 을 못 넘는다 → 셋 다 조건이 실제로 거른다."""
    daily = _daily(_rally(days=2))
    row = {'code': '000001', 'name': '테스트', 'price': 12000, 'amount_eok': 400, 'chg': 6.0}
    ev = evaluate(row, daily, _hourly(trend=1.003), 300, daily[-1].date)
    ks = [stoch_k([(c.high, c.low, c.close) for c in daily], n, k) for n, k, _ in STOCH_SETS]
    assert ks[0][-1] > 20 and ks[2][-1] <= 20          # 빠른 것만 올라옴
    assert not ev['checks']['stoch'][0] and not ev['pass']


def test_evaluate_rejects_cheap_and_limit_up():
    daily = _daily(_rally())
    cheap = evaluate({'code': 'A', 'name': 'x', 'price': 900, 'amount_eok': 400, 'chg': 5.0}, daily, None, 300, daily[-1].date)
    assert not cheap['checks']['price'][0]
    limit = evaluate({'code': 'A', 'name': 'x', 'price': 12000, 'amount_eok': 400, 'chg': 29.8}, daily, None, 300, daily[-1].date)
    assert not limit['checks']['limit'][0]


def test_evaluate_rejects_near_high_prev_spike_and_missing_today():
    daily = _daily([100 + i for i in range(60)])
    t = daily[-1]
    daily[-1] = Candle(t.date, 130, 140, 130, 139, 1000, 0)          # 범위 90% → 고가 근처
    daily[-2] = Candle(daily[-2].date, 120, 130, 120, 130, 1000, 0)  # 전날 급등
    daily[-3] = Candle(daily[-3].date, 120, 120, 120, 120, 1000, 0)
    row = {'code': '000001', 'name': '테스트', 'price': 12000, 'amount_eok': 400, 'chg': 1.0}
    ev = evaluate(row, daily, _hourly(), 300, '20261017')
    assert not ev['checks']['high'][0] and not ev['checks']['prev'][0] and not ev['pass']
    stale = evaluate(row, daily, _hourly(), 300, '20261018')
    assert not stale['pass'] and 'data' in stale['checks']
    assert not evaluate(row | {'amount_eok': 100}, daily, _hourly(), 300, '20261017')['checks']['amount'][0]


# ── 사이징·설정 ──
def test_buy_price_adds_two_ticks():
    assert buy_price(10000) == 10020      # 1만원대 호가단위 10
    assert buy_price(60000) == 60200      # 5만~20만 호가단위 100
    assert buy_price(0) == 0


def test_plan_quantity_caps():
    assert plan_quantity(10000, 500, 1_000_000, 3_000_000, 0, 10**8)[0] == 100
    assert plan_quantity(10000, 50, 1_000_000, 3_000_000, 0, 10**8)[0] == 50        # 호가 총잔량
    assert plan_quantity(10000, 500, 1_000_000, 300_000, 0, 10**8)[0] == 30         # 총액 잔여
    assert plan_quantity(10000, 500, 1_000_000, 3_000_000, 2_000_000, 10**8)[0] == 20   # 15:00~15:20 대금의 10%
    assert plan_quantity(10000, 500, 1_000_000, 3_000_000, 0, 7)[0] == 7            # 주문가능 수량
    assert plan_quantity(10000, 10**9, 10**9, 10**9, 0, 10**8)[0] == 20000          # 한 종목 2억 상한
    assert plan_quantity(0, 500, 1_000_000, 3_000_000, 0, 10**8)[0] == 0
    assert plan_quantity(2_000_000, 5, 1_000_000, 3_000_000, 0, 10**8)[0] == 0
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
        self.ask, self.ask_qty, self.bid_qty, self.total_ask_qty = 10000, 300, 300, 3000
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def ranking(self, market):
        return [{'code': '000001', 'name': '종목A', 'price': 10000, 'amount_eok': 500, 'chg': 6.0}] if market == '001' else []

    async def daily(self, code):
        closes = _rally()
        d = _daily(closes, today='20261019')
        last = closes[-1]
        d[-1] = Candle('20261019', last * 0.94, last * 1.02, last * 0.93, last, 1000, 0)
        return d

    async def minutes(self, code, scope):
        if scope == '60':
            return _hourly(trend=1.003)
        return [('20261019150500', 1, 1, 1, 10000, 500)]

    async def book(self, code):
        return {'ask': self.ask, 'ask_qty': self.ask_qty, 'bid': self.ask - 50, 'bid_qty': self.bid_qty,
                'total_ask_qty': self.total_ask_qty, 'at': '151950'}

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
    assert p['dry'] and p['quantity'] == 49 and p['average'] == 10020 and p['status'] == 'held'
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.e.positions['000001']['status'] == 'sold' and eng.b.calls == []
    assert [o['side'] for o in eng.e.orders] == ['buy', 'sell'] and all(o['dry'] for o in eng.e.orders)


def test_live_buy_journals_intent_then_sends_once_and_settles(eng):
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2, 'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 50, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == [('buy', '000001', 49, 10020)], eng.e.day()
    o = eng.e.orders[-1]
    assert o['status'] == 'accepted' and o['order_no'] == '0001' and o['side'] == 'buy' and not o['dry']
    run(eng.e.tick())                                          # 같은 창에서 두 번 안 산다
    assert len(eng.b.calls) == 1
    eng.b.detail_response = {'filled': 49, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 49, 'available': 49, 'average': 10030.0}
    eng.now[0] = datetime(2026, 10, 19, 15, 31, 0, tzinfo=KST)
    run(eng.e.tick())
    p = eng.e.positions['000001']
    assert p['status'] == 'held' and p['quantity'] == 49 and p['average'] == 10030.0 and not p['dry']
    assert eng.e.invested() == 49 * 10030.0
    # 다음 날 08:40 시장가 매도 → 체결 → sold
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls[-1] == ('sell', '000001', 49) and eng.e.positions['000001']['status'] == 'selling'
    eng.b.detail_response = {'filled': 49, 'remaining': 0, 'rejected': False}
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


def test_total_cap_holds_within_one_1519_batch(eng, monkeypatch):
    """실전은 체결 확인 전까지 보유가 안 잡힌다 → 같은 묶음 안에서도 접수 금액을 총액 상한에 포함해야 한다."""
    import app.closebet as cb
    rows = [{'code': f'00000{i}', 'name': f'종목{i}', 'price': 10000, 'amount_eok': 500 - i, 'chg': 6.0} for i in (1, 2, 3)]

    async def ranking(market):
        return rows if market == '001' else []
    eng.b.ranking = ranking
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 3,
                           'total_cap': 700_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    run(eng.e.scan())
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 50, tzinfo=KST)
    run(eng.e.tick())
    spent = sum(q * px for kind, _, q, px in eng.b.calls if kind == 'buy')
    assert eng.b.calls and spent <= 700_000, (eng.b.calls, spent)


def test_stop_never_blocks_selling_or_cancelling(eng):
    """비상 정지·자동매매 끄기는 새 매수만 막는다. 이미 산 종목은 다음 날 반드시 팔려야 한다."""
    eng.e.update_settings({'mode': 'live', 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2,
                           'total_cap': 1_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})
    eng.e.positions['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 10, 'average': 10000,
                                 'bought_on': '2026-10-19', 'status': 'held', 'dry': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 10, 'available': 10, 'average': 10000.0}
    eng.e.persist()
    eng.e.stop()                                        # 비상 정지
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert ('sell', '000001', 10) in eng.b.calls
    assert eng.e.positions['000001']['status'] == 'selling'
