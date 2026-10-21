import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.autosell import KST, SellMonitor
from app.closebet import in_window
from app.leaderdip import (CANCEL_WINDOW, MONITOR_WINDOW, LeaderDip, anchor_line, normalize_settings, zones)
from app.kiwoom.orders import OrderUnknown


def bar(hhmm, o, h, l, c, v=1000, day='20261019'):
    return (f'{day}{hhmm}00', o, h, l, c, v)


def series(closes, day='20261019'):
    """15분봉. 봉 안 되돌림은 작게 두고 종가만 움직여 '반등 4%' 규칙에 안 걸리게 한다."""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hhmm = f'{9 + (i * 15) // 60:02d}{(i * 15) % 60:02d}'
        out.append((f'{day}{hhmm}00', prev, max(prev, c), min(prev, c) - 20, c, 1000))
        prev = c
    return out


# ── 고가라인·F존 ──
def test_anchor_takes_the_latest_new_high():
    bars = series([10000, 9850, 9700, 9550])
    anchor, at = anchor_line(bars)
    assert anchor == 10000 and at.endswith('090000')      # 첫 봉이 그날 고가


def test_anchor_resets_to_a_strong_rebound_bar_even_if_lower():
    """크게 빠진 뒤 4% 넘게 반등한 봉이 생기면 그 고점에서 다시 잰다."""
    bars = [bar('0900', 10000, 12000, 9900, 10000), bar('0915', 10000, 10100, 9500, 9600),
            bar('0930', 9600, 10500, 9500, 10400)]              # 저가 9,500 → 종가 10,400 = +9.5% 되돌림
    anchor, at = anchor_line(bars)
    assert anchor == 10500 and at.endswith('093000')
    quiet = [bar('0900', 10000, 12000, 9900, 10000), bar('0915', 10000, 10100, 9900, 10000)]
    assert anchor_line(quiet)[0] == 12000                        # 1% 되돌림은 기준선을 못 바꾼다
    crash = [bar('0900', 10000, 12000, 9900, 10000), bar('0915', 10000, 10000, 9000, 9000)]
    assert anchor_line(crash)[0] == 12000                        # 급락 봉(되돌림 0)은 기준선을 못 내린다


def test_zones_are_ten_and_twelve_percent_down():
    assert zones(10000) == (9000, 8800)


def test_settings_limits():
    s = normalize_settings({'mode': 'dry', 'enabled': True, 'slot_amount': 1_000_000, 'max_positions': 2,
                            'total_cap': 3_000_000, 'top_n': 10, 'sell_time': '08:40'})
    assert s['top_n'] == 10
    for bad in ({'total_cap': 10_000_000}, {'max_positions': 6}, {'top_n': 1}, {'top_n': 50},
                {'slot_amount': 5_000_000, 'total_cap': 3_000_000}, {'sell_time': '09:30'}):
        with pytest.raises(ValueError):
            normalize_settings(s | bad)


def test_windows():
    assert in_window(datetime(2026, 10, 19, 10, 0, tzinfo=KST), MONITOR_WINDOW)
    assert not in_window(datetime(2026, 10, 19, 9, 59, tzinfo=KST), MONITOR_WINDOW)
    assert not in_window(datetime(2026, 10, 19, 14, 59, tzinfo=KST), MONITOR_WINDOW)
    assert in_window(datetime(2026, 10, 19, 15, 19, tzinfo=KST), CANCEL_WINDOW)


# ── 엔진 ──
class Broker:
    def __init__(self):
        self.calls, self.cancels, self.rows, self.failure = [], [], {}, None
        self.bars = {}
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def minutes(self, code, scope):
        return self.bars.get(code, [])

    async def stock(self, code, price):
        return {'code': code, 'name': code}

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
        return f'ord{len(self.calls)}'

    async def cancel_once(self, order, guard):
        guard()
        self.cancels.append(order['order_no'])
        return 'c1'

    async def sell_market_once(self, code, qty, guard):
        guard()
        self.calls.append(('sell', code, qty))
        return 'sell1'

    async def detail(self, order):
        return self.detail_response


# 고가라인 10,000 → F2 9,000 / F3 8,800. 마지막 8,900 이 F2 아래로 내려온다
DIPPED = series([10000, 9850, 9700, 9550, 9400, 9250, 9100, 8950, 8900])


@pytest.fixture
def eng(tmp_path):
    now = [datetime(2026, 10, 19, 10, 0, 0, tzinfo=KST)]
    b = Broker()
    b.bars['000001'] = DIPPED
    notes = []
    e = LeaderDip(tmp_path / 'ld.json', 'real', lambda: b, lambda: True, lambda: {'value': '3'},
                  SellMonitor(tmp_path / 'shadow.json'), lambda: now[0], notify=notes.append)
    e.open()

    async def universe():
        return {'000001': {'code': '000001', 'name': '대장주A', 'cap_eok': 5_000_000}}
    e.universe = universe
    yield SimpleNamespace(e=e, b=b, now=now, notes=notes, path=tmp_path / 'ld.json')
    e.close_lock()


def run(coro):
    return asyncio.run(coro)


async def tick_and_wait(e):
    await e.tick()
    if e.tasks:
        await asyncio.gather(*list(e.tasks))


def on(e, mode='live', **kw):
    e.update_settings({'mode': mode, 'enabled': True, 'slot_amount': 1_000_000, 'max_positions': 2,
                       'total_cap': 3_000_000, 'top_n': 10, 'sell_time': '08:40'} | kw)


def test_places_half_at_f2_and_half_at_f3_when_price_drops_below_f2(eng):
    on(eng.e)
    run(tick_and_wait(eng.e))
    # 한 칸 100만 → 반씩 50만. F2 9,000 → 55주, F3 8,800 → 56주
    assert eng.b.calls == [('buy', '000001', 55, 9000), ('buy', '000001', 56, 8800)]
    setup = eng.e.setups['2026-10-19:000001']
    assert setup['anchor'] == 10000 and setup['f2'] == 9000 and setup['f3'] == 8800 and not setup['crowded']
    assert 'F2 터치' in eng.notes[-1]


def test_does_not_enter_twice_for_the_same_stock_in_one_day(eng):
    on(eng.e)
    run(tick_and_wait(eng.e))
    first = len(eng.b.calls)
    eng.now[0] = datetime(2026, 10, 19, 11, 0, 0, tzinfo=KST)
    run(tick_and_wait(eng.e))
    assert len(eng.b.calls) == first


def test_no_entry_while_price_is_above_f2(eng):
    eng.b.bars['000001'] = series([10000, 9800, 9600, 9400, 9250])      # F2(9,000) 위에서 멈춤
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls == [] and not eng.e.setups


def test_crowded_day_halves_the_slot(eng):
    for i, code in enumerate(('000001', '000002', '000003'), 1):
        eng.b.bars[code] = DIPPED

    async def universe():
        return {c: {'code': c, 'name': f'대장주{c}', 'cap_eok': 1} for c in ('000001', '000002', '000003')}
    eng.e.universe = universe
    on(eng.e, max_positions=5)
    run(tick_and_wait(eng.e))
    setups = eng.e.setups
    assert len(setups) == 3
    assert not setups['2026-10-19:000001']['crowded'] and not setups['2026-10-19:000002']['crowded']
    assert setups['2026-10-19:000003']['crowded']                     # 세 번째부터 몰린 날
    third = [c for c in eng.b.calls if c[1] == '000003']
    assert third == [('buy', '000003', 27, 9000), ('buy', '000003', 28, 8800)]   # 반 칸(50만)을 반씩


def test_cancels_unfilled_orders_at_1519(eng):
    on(eng.e)
    run(tick_and_wait(eng.e))
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 0, tzinfo=KST)
    run(eng.e.tick())
    assert sorted(eng.b.cancels) == ['ord1', 'ord2']
    run(eng.e.tick())                                   # 두 번 보내지 않는다
    assert len(eng.b.cancels) == 2


def test_settle_combines_both_fills_into_one_position(eng):
    on(eng.e)
    run(tick_and_wait(eng.e))
    eng.b.detail_response = {'filled': 55, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '대장주A', 'quantity': 111, 'available': 111, 'average': 8900.0}
    eng.now[0] = datetime(2026, 10, 19, 15, 31, 0, tzinfo=KST)
    eng.e.day()['bought'] = eng.now[0].isoformat()
    run(eng.e.tick())
    p = eng.e.positions['000001']
    assert p['quantity'] == 111 and p['average'] == 8900.0 and sorted(p['zones']) == ['F2', 'F3']
    eng.now[0] = datetime(2026, 10, 20, 8, 40, 0, tzinfo=KST)
    run(eng.e.tick())
    assert ('sell', '000001', 111) in eng.b.calls


def test_unknown_reply_blocks_resend_and_survives_restart(eng):
    on(eng.e)
    eng.b.failure = OrderUnknown('timeout')
    run(tick_and_wait(eng.e))
    assert any(o['status'] == 'unknown' for o in eng.e.orders) and eng.e.blocked('000001')
    eng.e.close_lock()
    e2 = LeaderDip(eng.path, 'real', lambda: eng.b, lambda: True, lambda: None,
                   SellMonitor(eng.path.parent / 's2.json'), lambda: eng.now[0])
    e2.open()
    try:
        assert e2.settings['mode'] == 'live' and '2026-10-19:000001' in e2.setups
    finally:
        e2.close_lock()
    eng.e.open()


def test_stop_blocks_new_orders(eng):
    on(eng.e)
    eng.e.stop()
    run(tick_and_wait(eng.e))
    assert eng.b.calls == [] and not eng.e.settings['enabled']


def test_dry_mode_records_both_halves_as_one_holding(eng):
    on(eng.e, 'dry')
    run(tick_and_wait(eng.e))
    assert eng.b.calls == []
    p = eng.e.positions['000001']
    assert p['dry'] and p['quantity'] == 111 and 8800 < p['average'] < 9000


def test_stop_does_not_block_cancelling_unfilled_buys(eng):
    """비상 정지해도 15:19 미체결 취소는 보낸다. 미체결 매수를 밤새 남기는 쪽이 더 위험하다."""
    on(eng.e)
    run(tick_and_wait(eng.e))
    eng.e.stop()
    eng.now[0] = datetime(2026, 10, 19, 15, 19, 0, tzinfo=KST)
    run(eng.e.tick())
    assert sorted(eng.b.cancels) == ['ord1', 'ord2']


def test_rests_when_the_market_gate_says_so(eng):
    """🧭 제작자 10-20 규칙: 전날 코스피가 0% 위면 🗻 는 그날 쉰다.
    화면 값(F존)은 계속 갱신하되 **새 매수만** 멈춘다."""
    async def gate(key):
        assert key == 'leaderdip'
        return True, '전날 코스피 등락 +3.29% · 전날 코스피가 올랐습니다'
    uni = {'000001': {'code': '000001', 'name': '대장주A', 'cap_eok': 5_000_000}}

    async def universe():
        return uni
    eng.e.universe, eng.e.gate_for = universe, gate
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls == []                                  # 아무 주문도 안 나갔다
    assert eng.e.setups == {}
    assert '코스피' in eng.e.day()['resting']
    assert any('오늘은 쉽니다' in n for n in eng.notes)
    assert uni['000001'].get('f2')                            # F존 값은 그대로 갱신됐다


def test_does_not_rest_when_the_gate_is_open(eng):
    async def gate(key):
        return False, ''
    eng.e.gate_for = gate
    on(eng.e)
    run(tick_and_wait(eng.e))
    assert eng.b.calls and not eng.e.day().get('resting')      # 평소처럼 산다
