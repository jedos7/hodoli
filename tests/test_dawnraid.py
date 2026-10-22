"""⚡ 시초 역습 (app/dawnraid.py). 09:00 동시호가에 받아 10:00에 판다."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.autosell import KST, SellMonitor
from app.closebet import in_window
from app.dawnraid import (BUY_WINDOW, GAP_MAX, INDEX_GAP_MIN, MAX_PICKS, TAKE_PROFIT_PCT, DawnRaid,
                          gap_pct, index_gap, order_price, plan_quantity, qualifies, take_profit_price)
from app.kiwoom.orders import NotSent


# ── 규칙 ─────────────────────────────────────────────────────
def test_overnight_filter_is_the_rule_document():
    assert qualifies(-5.0, 500)                 # 전날 −5% · 500억
    assert qualifies(-3.0, 100) and qualifies(-10.0, 1000)      # 경계는 넣는다
    assert not qualifies(-2.9, 500)             # 덜 빠짐
    assert not qualifies(-10.1, 500)            # 너무 빠짐
    assert not qualifies(-5.0, 99) and not qualifies(-5.0, 1001)


def test_gap_and_order_price():
    assert gap_pct(9_300, 10_000) == pytest.approx(-7.0)
    assert order_price(9_300) == 9_320          # 5천~2만원대 호가단위 10원 → +2틱
    assert order_price(0) == 0


def test_take_profit_rounds_down_to_a_tick():
    assert take_profit_price(10_000) == 10_700
    assert take_profit_price(9_333) == 9_980    # 9,986 → 호가단위 10원 내림
    assert take_profit_price(0) == 0


def test_index_gap_is_weighted_by_market_cap():
    rows = [{'cap': 100, 'prev_close': 1000, 'expected': 900},     # −10%
            {'cap': 300, 'prev_close': 1000, 'expected': 1000}]    # 0%
    assert index_gap(rows) == pytest.approx(-2.5)
    assert index_gap([{'cap': 100, 'prev_close': 1000}]) is None   # 예상가 없으면 모름


def test_quantity_respects_the_two_percent_of_yesterday():
    qty, why = plan_quantity(10_000, 1_000_000, 5_000_000, prev_amount=10_000_000, cap_qty=10 ** 8)
    assert qty == 20 and '2%' in why            # 전날 대금 1,000만 × 2% = 20만 → 20주
    assert plan_quantity(0, 1, 1, 1, 1)[0] == 0


def test_buy_window_is_a_few_seconds_before_the_open():
    assert in_window(datetime(2026, 10, 22, 8, 59, 45, tzinfo=KST), BUY_WINDOW)
    assert not in_window(datetime(2026, 10, 22, 8, 59, 44, tzinfo=KST), BUY_WINDOW)
    assert not in_window(datetime(2026, 10, 22, 9, 0, 0, tzinfo=KST), BUY_WINDOW)


# ── 전 과정 ──────────────────────────────────────────────────
class Basic:
    def __init__(self, price=0, expected=0, open=0, high=0):
        self.price, self.expected, self.open, self.high = price, expected, open, high
        self.low = 0


class Broker:
    def __init__(self):
        self.calls, self.rows, self.info = [], {}, {}
        self.rest = SimpleNamespace(basic=self._basic)
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def _basic(self, code):
        return self.info.get(code, Basic())

    async def stock(self, code, price):
        return {'code': code, 'name': '종목'}

    async def capacity(self, code, price):
        return {'quantity': 10000, 'amount': 100000000}

    async def positions(self):
        return {k: v.copy() for k, v in self.rows.items()}

    async def open_orders(self, code):
        return []

    async def buy_once(self, code, qty, price, guard):
        guard()
        self.calls.append(('buy', code, qty, price))
        return '0001'

    async def sell_limit_once(self, code, qty, price, guard):
        guard()
        self.calls.append(('sell_limit', code, qty, price))
        return '0002'

    async def sell_market_once(self, code, qty, guard):
        guard()
        self.calls.append(('sell', code, qty))
        return '0003'

    async def cancel_once(self, order, guard):
        guard()
        self.calls.append(('cancel', order['code']))
        return '0004'

    async def detail(self, order):
        return self.detail_response


WATCH = {'000001': {'code': '000001', 'name': '종목A', 'market': '코스피', 'prev_close': 10_000,
                    'prev_chg': -5.0, 'prev_amount_eok': 500.0, 'expected': 0, 'gap': None, 'at': ''},
         '000002': {'code': '000002', 'name': '종목B', 'market': '코스닥', 'prev_close': 20_000,
                    'prev_chg': -6.0, 'prev_amount_eok': 300.0, 'expected': 0, 'gap': None, 'at': ''}}
INDEX = [{'code': '005930', 'name': '큰종목', 'prev_close': 70_000, 'cap': 1000}]


@pytest.fixture
def eng(tmp_path):
    now = [datetime(2026, 10, 22, 8, 59, 45, tzinfo=KST)]
    b = Broker()
    notes = []
    e = DawnRaid(tmp_path / 'dr.json', 'real', lambda: b, lambda: True, lambda: {'value': '3'},
                 SellMonitor(tmp_path / 'shadow.json'), lambda: now[0], notify=notes.append)
    e.open()
    e.watch = {k: dict(v) for k, v in WATCH.items()}
    e.index = [dict(r) for r in INDEX]
    yield SimpleNamespace(e=e, b=b, now=now, notes=notes)
    e.close_lock()


def run(coro):
    return asyncio.run(coro)


async def tick_and_wait(e):
    await e.tick()
    if e.tasks:
        await asyncio.gather(*list(e.tasks))


def on(e, mode='live', **kw):
    e.update_settings({'mode': mode, 'enabled': True, 'slot_amount': 1_000_000,
                       'max_positions': 2, 'total_cap': 3_000_000} | kw)


def quotes(eng, a_gap=-8.0, b_gap=-9.0, index_gap_pct=-1.0):
    eng.b.info = {
        '000001': Basic(expected=int(10_000 * (1 + a_gap / 100))),
        '000002': Basic(expected=int(20_000 * (1 + b_gap / 100))),
        '005930': Basic(expected=int(70_000 * (1 + index_gap_pct / 100))),
    }


def test_buys_the_two_deepest_gaps_by_yesterdays_amount(eng):
    quotes(eng)
    on(eng.e)
    run(tick_and_wait(eng.e))
    # 예상가 9,200 → +2틱 9,220 · 한 칸 100만 (전날 대금 500억의 2%=10억보다 작다) → 108주
    assert ('buy', '000001', 108, 9220) in eng.b.calls
    assert len([c for c in eng.b.calls if c[0] == 'buy']) == MAX_PICKS
    assert eng.e.day()['index_gap'] == -1.0
    assert [p['name'] for p in eng.e.day()['picked']] == ['종목A', '종목B']    # 전날 대금 큰 순


def test_rests_when_the_index_gaps_down_hard(eng):
    """🧭 코스피 예상 갭이 −2%보다 많이 빠진 날은 쉰다 (제작자: 그런 날 PF 0.66)."""
    quotes(eng, index_gap_pct=-2.5)
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls == [] and '코스피 예상 갭' in eng.e.day()['skipped'][0]
    assert any('쉽니다' in n for n in eng.notes)


def test_a_shallow_gap_is_not_bought(eng):
    quotes(eng, a_gap=-6.9, b_gap=-6.5)
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls == [] and eng.e.day()['picked'] == []
    assert any(f'{GAP_MAX:.0f}%' in n for n in eng.notes)


def test_dry_fill_uses_the_real_opening_price(eng):
    """흉내 체결은 지정가가 아니라 **09:00 실제 시가**로 판정한다. 시가가 지정가보다 높으면 미체결."""
    quotes(eng)
    on(eng.e, 'dry')
    run(tick_and_wait(eng.e))
    assert eng.b.calls == []
    eng.b.info = {'000001': Basic(price=9_300, open=9_100, high=9_400),      # 지정가 9,210 ≥ 시가 9,100 → 체결
                  '000002': Basic(price=18_300, open=18_500, high=18_600)}   # 지정가 18,220 < 시가 → 미체결
    eng.now[0] = datetime(2026, 10, 22, 9, 0, 30, tzinfo=KST)
    run(tick_and_wait(eng.e))
    p = eng.e.positions['000001']
    assert p['average'] == 9_100 and p['status'] == 'held' and p['take_profit'] == take_profit_price(9_100)
    assert '000002' not in eng.e.positions
    assert any('미체결' in o['note'] for o in eng.e.orders if o['code'] == '000002')


def test_dry_close_takes_the_profit_when_the_high_reached_it(eng):
    quotes(eng)
    on(eng.e, 'dry')
    run(tick_and_wait(eng.e))
    eng.b.info = {'000001': Basic(price=9_300, open=9_000, high=9_400),
                  '000002': Basic(price=1, open=10 ** 7, high=1)}
    eng.now[0] = datetime(2026, 10, 22, 9, 0, 30, tzinfo=KST)
    run(tick_and_wait(eng.e))
    target = eng.e.positions['000001']['take_profit']            # 9,000 × 1.07 → 9,630
    eng.b.info['000001'] = Basic(price=9_500, open=9_000, high=target + 10)
    eng.now[0] = datetime(2026, 10, 22, 10, 0, 0, tzinfo=KST)
    run(tick_and_wait(eng.e))
    p = eng.e.positions['000001']
    assert p['status'] == 'sold' and p['sold_price'] == target and '익절' in p['note']


def test_dry_close_sells_at_ten_when_the_profit_was_not_reached(eng):
    quotes(eng)
    on(eng.e, 'dry')
    run(tick_and_wait(eng.e))
    eng.b.info = {'000001': Basic(price=9_300, open=9_000, high=9_100),
                  '000002': Basic(price=1, open=10 ** 7, high=1)}
    eng.now[0] = datetime(2026, 10, 22, 9, 0, 30, tzinfo=KST)
    run(tick_and_wait(eng.e))
    eng.b.info['000001'] = Basic(price=9_150, open=9_000, high=9_200)
    eng.now[0] = datetime(2026, 10, 22, 10, 0, 0, tzinfo=KST)
    run(tick_and_wait(eng.e))
    p = eng.e.positions['000001']
    assert p['status'] == 'sold' and p['sold_price'] == 9_150 and '10:00' in p['note']


def test_live_arms_a_take_profit_limit_then_cancels_and_sells_at_market(eng):
    quotes(eng)
    on(eng.e)
    run(tick_and_wait(eng.e))
    eng.b.detail_response = {'filled': 108, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 108, 'available': 108, 'average': 9_100.0}
    eng.now[0] = datetime(2026, 10, 22, 9, 0, 30, tzinfo=KST)
    run(tick_and_wait(eng.e))
    target = take_profit_price(9_100)
    assert ('sell_limit', '000001', 108, target) in eng.b.calls
    eng.now[0] = datetime(2026, 10, 22, 9, 59, 50, tzinfo=KST)
    run(tick_and_wait(eng.e))
    assert ('cancel', '000001') in eng.b.calls
    eng.b.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}   # 익절 미체결 · 취소됨
    eng.now[0] = datetime(2026, 10, 22, 10, 0, 0, tzinfo=KST)
    run(tick_and_wait(eng.e))
    assert ('sell', '000001', 108) in eng.b.calls


def test_a_tripped_breaker_blocks_the_buy(eng):
    from app.breakers import Breakers
    br = Breakers(eng.e.path.parent / 'br.json', 'real', lambda: None, lambda: eng.now[0])
    br.open()
    br.feed_equity(10_000_000)
    br.feed_equity(8_000_000)
    eng.e.breakers = br
    quotes(eng)
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls == [] and any('차단기' in x for x in eng.e.day()['skipped'])


def test_market_volume_multiplier_shrinks_the_slot(eng):
    async def sizing():
        return 0.5
    eng.e.sizing = sizing
    quotes(eng)
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert ('buy', '000001', 54, 9220) in eng.b.calls            # 100만 → 50만 → 54주
