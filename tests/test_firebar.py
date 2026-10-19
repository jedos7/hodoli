import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.autosell import KST, SellMonitor
from app.closebet import in_window
from app.firebar import BUY_WINDOW, SCAN_WINDOW, FireBar, evaluate, tail_pct, upper_limit
from app.models import Candle
from app.kiwoom.orders import NotSent


def daily(rows, today='20261019'):
    """rows = [(close, high, amount)] 오래된 순. 마지막이 오늘."""
    out, base = [], datetime(2026, 8, 1)
    for i, (c, h, a) in enumerate(rows):
        d = (base + timedelta(days=i)).strftime('%Y%m%d') if i < len(rows) - 1 else today
        out.append(Candle(d, c, h, c * 0.95, c, 1000, a))
    return out


def good(close=12000, high=12050, amount=500e8, prev=10000, n=30):
    """20일 평균 대금 10억, 오늘 500억(50배), 전날 종가 prev, 등락 +20%, 윗꼬리 0.5%p"""
    rows = [(prev, prev, 10e8)] * n
    rows[-1] = (prev, prev, 10e8)
    return daily(rows + [(close, high, amount)])


def test_tail_and_upper_limit():
    assert tail_pct(10200, 10000, 10000) == pytest.approx(2.0)
    assert upper_limit(10000) == 13000          # 1만원대 호가단위 10
    assert upper_limit(0) == 0


def test_evaluate_passes_a_clean_firebar():
    ev = evaluate({'amount_eok': 500, 'price': 12000, 'chg': 20.0}, good(), 300, '20261019')
    assert ev['pass'], ev['checks']
    assert ev['chg'] == pytest.approx(20.0) and ev['tail'] == pytest.approx(0.5) and ev['multiple'] >= 5
    assert ev['upper_limit'] == 13000


def test_evaluate_rejects_limit_up_small_multiple_long_tail_and_cheap():
    assert not evaluate({'amount_eok': 500, 'price': 13000, 'chg': 30.0}, good(close=13000, high=13000), 300, '20261019')['checks']['rise'][0]
    assert not evaluate({'amount_eok': 500, 'price': 11000, 'chg': 10.0}, good(close=11000, high=11050), 300, '20261019')['checks']['rise'][0]
    weak = good(amount=40e8)                      # 평소의 4배
    assert not evaluate({'amount_eok': 40, 'price': 12000, 'chg': 20.0}, weak, 10, '20261019')['checks']['multiple'][0]
    tail = good(close=12000, high=12300)          # 윗꼬리 3%p
    assert not evaluate({'amount_eok': 500, 'price': 12000, 'chg': 20.0}, tail, 300, '20261019')['checks']['tail'][0]
    cheap = good(close=960, high=965, prev=800)
    assert not evaluate({'amount_eok': 500, 'price': 960, 'chg': 20.0}, cheap, 300, '20261019')['checks']['price'][0]
    assert not evaluate({'amount_eok': 100, 'price': 12000, 'chg': 20.0}, good(), 300, '20261019')['checks']['amount'][0]


def test_windows_are_ten_seconds_before_closebet():
    assert in_window(datetime(2026, 10, 19, 15, 10, tzinfo=KST), SCAN_WINDOW)
    assert in_window(datetime(2026, 10, 19, 15, 19, 40, tzinfo=KST), BUY_WINDOW)
    assert not in_window(datetime(2026, 10, 19, 15, 19, 39, tzinfo=KST), BUY_WINDOW)


# ── 엔진 ──
class Basic:
    def __init__(self, price, high):
        self.price, self.high = price, high


class Broker:
    def __init__(self):
        self.calls, self.rows, self.failure = [], {}, None
        self.basic_info = Basic(12000, 12050)
        self.rest = SimpleNamespace(basic=self._basic)
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def _basic(self, code):
        return self.basic_info

    async def ranking(self, market):
        return [{'code': '000001', 'name': '종목A', 'price': 12000, 'amount_eok': 500, 'chg': 20.0}] if market == '001' else []

    async def daily(self, code):
        return good()

    async def minutes(self, code, scope):
        return [('20261019150500', 1, 1, 1, 12000, 500)]

    async def book(self, code):
        return {'ask': 12050, 'ask_qty': 300, 'bid': 12000, 'bid_qty': 300, 'total_ask_qty': 3000, 'at': '151940'}

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
        return '0002'

    async def detail(self, order):
        return self.detail_response


@pytest.fixture
def eng(tmp_path):
    now = [datetime(2026, 10, 19, 15, 10, 0, tzinfo=KST)]
    b = Broker()
    notes = []
    e = FireBar(tmp_path / 'fb.json', 'real', lambda: b, lambda: True, lambda: {'value': '3'},
                SellMonitor(tmp_path / 'shadow.json'), lambda: now[0], notify=notes.append)
    e.open()
    yield SimpleNamespace(e=e, b=b, now=now, notes=notes)
    e.close_lock()


def run(coro):
    return asyncio.run(coro)


async def tick_and_wait(e):
    await e.tick()
    if e.tasks:
        await asyncio.gather(*list(e.tasks))


def on(e, mode='live'):
    e.update_settings({'mode': mode, 'enabled': True, 'per_trade_amount': 500_000, 'max_positions': 2,
                       'total_cap': 2_000_000, 'threshold_eok': 300, 'sell_time': '08:40'})


def test_scan_at_1510_then_buy_at_151940_at_last_price_plus_two_ticks(eng):
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.e.scans['2026-10-19']['passed'] == ['000001']
    assert '기준봉 종가 15:10 후보' in eng.notes[-1]
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 40, tzinfo=KST)
    run(eng.e.tick())
    # 지금 값 12,000 + 2틱(10원) = 12,020 · 예산 50만 → 41주
    assert eng.b.calls == [('buy', '000001', 41, 12020)]


def test_order_price_never_exceeds_upper_limit(eng):
    """+2틱이 상한가를 넘으면 상한가로 자른다 (등락 범위 검사와 별개인 안전장치)."""
    eng.b.basic_info = Basic(12995, 13000)          # 상한가 13,000 코앞 → +2틱이면 13,015
    r = {'code': '000001', 'price': 12995, 'upper_limit': 13000, 'prev_close': 10000}
    price = run(eng.e.order_price(eng.b, {'ask': 13000}, r))
    assert price == 13000
    r2 = r | {'upper_limit': 0}
    assert run(eng.e.order_price(eng.b, {'ask': 13000}, r2)) == 13015


def test_pre_buy_check_skips_when_tail_grew_after_1510(eng):
    on(eng.e)
    run(eng.e.scan())
    eng.b.basic_info = Basic(11600, 12300)          # 윗꼬리 7%p 로 벌어짐
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 40, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == [] and any('윗꼬리' in s for s in eng.e.day()['skipped'])


def test_dry_mode_records_then_sells_next_morning(eng):
    on(eng.e, 'dry')
    run(tick_and_wait(eng.e))
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 40, tzinfo=KST)
    run(eng.e.tick())
    assert eng.b.calls == [] and eng.e.positions['000001']['dry']
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.e.positions['000001']['status'] == 'sold' and eng.b.calls == []


def test_limit_up_is_rejected_even_when_adjusted_daily_says_otherwise():
    """2026-10-19 에스엠벡셀: 수정주가 일봉은 전날 3,150(등락 28.25%)이지만 실제 전날은 3,110(+29.9% 상한가).
    키움 순위 자료의 등락률을 써야 상한가를 제대로 거른다."""
    rows = [(3155, 3470, 1381e8)] * 25 + [(3150, 3425, 321e8), (4040, 4040, 994e8)]
    d = daily(rows)
    ev = evaluate({'amount_eok': 994, 'price': 4040, 'chg': 29.9}, d, 300, '20261019')
    assert not ev['checks']['rise'][0] and not ev['pass']
    assert ev['chg'] == 29.9 and ev['prev_close'] == 3110
