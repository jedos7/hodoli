import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.autoexit import AutoExit
from app.autosell import KST, SellMonitor
from app.horizonbuy import BUY_WINDOW, FILL_WINDOW, HorizonBuy, normalize_settings, pick
from app.closebet import in_window
from app.kiwoom.orders import NotSent, OrderRejected, OrderUnknown


def row(code='000001', name='종목A', grade='A', base=2000, price=12000, status='ready'):
    return {'code': code, 'name': name, 'grade': grade, 'status': status, 'price': price,
            'base': {'amountEok': base}, 'd0': '20261015'}


# ── 후보 거름망 ──
def test_pick_keeps_only_noon_ready_graded_rows():
    rows = [row(), row('000002', '종목B', status='preview'), row('000003', '종목C', grade='—'),
            row('000004', '종목D', base=800), row('000005', '종목E', price=900)]
    picks, skipped = pick(rows, 1500)
    assert [p['code'] for p in picks] == ['000001']
    assert len(skipped) == 3                                    # 등급·기준봉대금·주가 (preview 는 조용히 건너뜀)
    assert any('기준봉 대금' in s for s in skipped) and any('1,000원' in s for s in skipped)


def test_pick_gives_s_two_slots_and_sorts_s_first():
    picks, _ = pick([row('000001', 'A등급', 'A', base=9000), row('000002', 'S등급', 'S', base=2000)], 1500)
    assert [p['code'] for p in picks] == ['000002', '000001']   # S 먼저
    assert picks[0]['slots'] == 2 and picks[1]['slots'] == 1


def test_settings_limits():
    s = normalize_settings({'mode': 'dry', 'enabled': True, 'slot_amount': 1_000_000, 'max_positions': 3,
                            'total_cap': 3_000_000, 'min_base_eok': 1500, 'link_exit': True})
    assert s['enabled'] and s['link_exit'] and s['min_base_eok'] == 1500
    for bad in ({'total_cap': 10_000_000}, {'max_positions': 6}, {'slot_amount': 5_000_000, 'total_cap': 3_000_000},
                {'mode': 'x'}, {'min_base_eok': 0}):
        with pytest.raises(ValueError):
            normalize_settings(s | bad)


def test_buy_window_is_the_noon_minute():
    assert in_window(datetime(2026, 10, 19, 12, 0, 0, tzinfo=KST), BUY_WINDOW)
    assert in_window(datetime(2026, 10, 19, 12, 0, 59, tzinfo=KST), BUY_WINDOW)
    assert not in_window(datetime(2026, 10, 19, 11, 59, 59, tzinfo=KST), BUY_WINDOW)
    assert not in_window(datetime(2026, 10, 19, 12, 1, 0, tzinfo=KST), BUY_WINDOW)
    assert in_window(datetime(2026, 10, 19, 12, 5, 0, tzinfo=KST), FILL_WINDOW)


# ── 엔진 ──
class Broker:
    def __init__(self):
        self.calls, self.rows, self.failure = [], {}, None
        self.ask, self.total_ask_qty = 10000, 3000
        self.detail_response = {'filled': 0, 'remaining': 0, 'rejected': False}

    async def book(self, code):
        return {'ask': self.ask, 'ask_qty': 300, 'bid': self.ask - 50, 'bid_qty': 300,
                'total_ask_qty': self.total_ask_qty, 'at': '120000'}

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

    async def detail(self, order):
        return self.detail_response


class FakeExit:
    """호라이즌 자동매도 흉내: prepare → arm 만 본다."""
    def __init__(self):
        self.plans, self.error, self.owner, self.armed, self.fail = {}, '', object(), [], None

    async def prepare(self, code, bought_on):
        if self.fail:
            raise self.fail
        assert isinstance(bought_on, date)
        p = {'id': 'plan-' + code, 'code': code, 'status': 'prepared'}
        self.plans[code] = p
        return p

    async def arm(self, plan_id, environment, market_order):
        assert market_order is True and environment == 'real'
        self.armed.append(plan_id)
        for p in self.plans.values():
            if p['id'] == plan_id:
                p['status'] = 'armed'


@pytest.fixture
def eng(tmp_path):
    now = [datetime(2026, 10, 19, 12, 0, 0, tzinfo=KST)]
    connected, rows = [True], [[row()]]
    b, ex = Broker(), FakeExit()
    m = SellMonitor(tmp_path / 'shadow.json')
    peer = AutoExit(tmp_path / 'peer.json', 'real', lambda: b, lambda: connected[0], lambda: None, m, lambda: now[0])
    peer.open()
    notes = []
    e = HorizonBuy(tmp_path / 'hb.json', 'real', lambda: b, lambda: connected[0], lambda: None, m, lambda: now[0],
                   candidates=lambda: rows[0], exit_engine=lambda: ex, peers=(peer,), notify=notes.append)
    e.open()
    yield SimpleNamespace(e=e, b=b, ex=ex, now=now, rows=rows, connected=connected, peer=peer, notes=notes, path=tmp_path / 'hb.json')
    e.close_lock()
    peer.close_lock()


def run(coro):
    return asyncio.run(coro)


def on(e, mode='live', **kw):
    e.update_settings({'mode': mode, 'enabled': True, 'slot_amount': 500_000, 'max_positions': 2,
                       'total_cap': 2_000_000, 'min_base_eok': 1500, 'link_exit': True} | kw)


def test_dry_mode_buys_on_paper_without_sending(eng):
    on(eng.e, 'dry')
    run(eng.e.tick())
    assert eng.b.calls == []
    p = eng.e.positions['000001']
    assert p['dry'] and p['status'] == 'held' and p['average'] == 10020 and p['quantity'] == 49
    assert '호라이즌존 매수(흉내)' in eng.notes[-1]
    eng.now[0] = datetime(2026, 10, 19, 12, 5, 0, tzinfo=KST)
    run(eng.e.tick())
    assert eng.ex.armed == []                      # 흉내는 자동매도에 넘기지 않는다


def test_live_buys_once_at_noon_then_links_the_exit(eng):
    on(eng.e)
    run(eng.e.tick())
    assert eng.b.calls == [('buy', '000001', 49, 10020)]        # 매도1호가 10,000 + 2틱
    assert eng.e.orders[-1]['status'] == 'accepted' and eng.e.orders[-1]['reason'] == 'HORIZON_A'
    run(eng.e.tick())                                           # 같은 창에서 두 번 안 산다
    assert len(eng.b.calls) == 1
    eng.b.detail_response = {'filled': 49, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 49, 'available': 49, 'average': 10030.0}
    eng.now[0] = datetime(2026, 10, 19, 12, 5, 0, tzinfo=KST)
    run(eng.e.tick())
    p = eng.e.positions['000001']
    assert p['status'] == 'linked' and p['quantity'] == 49 and p['average'] == 10030.0
    assert eng.ex.armed == ['plan-000001'] and eng.e.invested() == 49 * 10030.0


def test_s_grade_uses_two_slots(eng):
    eng.rows[0] = [row(grade='S')]
    on(eng.e)
    run(eng.e.tick())
    # 한 자리 50만 × 2자리 = 100만, 10,020원 → 99주
    assert eng.b.calls == [('buy', '000001', 99, 10020)]


def test_total_cap_and_position_limit(eng):
    eng.rows[0] = [row('000001', 'A', base=3000), row('000002', 'B', base=2500), row('000003', 'C', base=2000)]
    on(eng.e, max_positions=2, total_cap=700_000)
    run(eng.e.tick())
    assert len(eng.b.calls) == 2                                 # 자리 수 상한
    assert sum(q * px for _, _, q, px in eng.b.calls) <= 700_000  # 총액 상한


def test_link_failure_keeps_position_and_warns(eng):
    on(eng.e)
    run(eng.e.tick())
    eng.b.detail_response = {'filled': 49, 'remaining': 0, 'rejected': False}
    eng.b.rows['000001'] = {'code': '000001', 'name': '종목A', 'quantity': 49, 'available': 49, 'average': 10030.0}
    eng.ex.fail = NotSent('잔고 확인 실패')
    eng.now[0] = datetime(2026, 10, 19, 12, 5, 0, tzinfo=KST)
    run(eng.e.tick())
    p = eng.e.positions['000001']
    assert p['status'] == 'held' and '자동매도 연결 실패' in p['note']
    assert any('연결 실패' in n for n in eng.notes)


def test_unknown_reply_blocks_resend_and_survives_restart(eng):
    on(eng.e)
    eng.b.failure = OrderUnknown('timeout')
    run(eng.e.tick())
    assert eng.e.orders[-1]['status'] == 'unknown' and eng.e.blocked('000001')
    eng.e.close_lock()
    e2 = HorizonBuy(eng.path, 'real', lambda: eng.b, lambda: True, lambda: None,
                    SellMonitor(eng.path.parent / 's2.json'), lambda: eng.now[0])
    e2.open()
    try:
        assert e2.settings['mode'] == 'live' and e2.orders[-1]['status'] == 'unknown'
        assert e2.days['2026-10-19'].get('bought')              # 같은 날 다시 사지 않는다
    finally:
        e2.close_lock()
    eng.e.open()


def test_stop_and_peer_conflict_block_buying(eng):
    on(eng.e)
    eng.e.stop()
    run(eng.e.tick())
    assert eng.b.calls == [] and not eng.e.settings['enabled']
    eng.e.settings['enabled'] = True
    eng.e.days['2026-10-19'].pop('bought', None)
    eng.peer.plans['000001'] = {'code': '000001', 'id': 'x', 'status': 'armed'}
    run(eng.e.tick())
    assert eng.b.calls == [] and any('다른 자동매매' in s for s in eng.e.day()['skipped'])


def test_rejected_order_is_not_a_position(eng):
    on(eng.e)
    eng.b.failure = OrderRejected('거부')
    run(eng.e.tick())
    assert eng.e.orders[-1]['status'] == 'rejected' and '000001' not in eng.e.positions


def test_candidate_source_failure_is_reported_not_silent(eng):
    def boom():
        raise RuntimeError('관제탑 수집 중')
    eng.e.candidates = boom
    on(eng.e)
    run(eng.e.tick())
    assert eng.b.calls == [] and '관제탑' in eng.e.day()['error']
    assert any('매수 못 함' in n for n in eng.notes)


def test_live_requires_real_env(tmp_path):
    b = Broker()
    paper = HorizonBuy(tmp_path / 'paper.json', 'paper', lambda: b, lambda: True, lambda: None,
                       SellMonitor(tmp_path / 's.json'), lambda: datetime(2026, 10, 19, 12, 0, tzinfo=KST))
    paper.open()
    try:
        with pytest.raises(ValueError):
            paper.update_settings({'mode': 'live', 'enabled': True, 'slot_amount': 100_000, 'max_positions': 1,
                                   'total_cap': 100_000, 'min_base_eok': 1500, 'link_exit': True})
    finally:
        paper.close_lock()
