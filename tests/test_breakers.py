"""차단기 (app/breakers.py). 멈춤은 **새 매수만**, 매도는 막지 않는다."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from app.autosell import KST
from app.breakers import (BUY_MAX_CODE, BUY_MAX_DAY, EQUITY_DROP, REJECT_MAX, SELL_BURST,
                          Breakers, NO_BREAKERS, create_breakers)
from app.kiwoom.orders import NotSent

MORNING = datetime(2026, 10, 19, 9, 5, tzinfo=KST)


def make(tmp_path, equity=None, at=MORNING):
    now = [at]
    notes = []

    class Broker:
        def __init__(self):
            self.asked = 0

        async def equity(self):
            self.asked += 1
            if equity is None:
                raise ValueError('예탁자산 항목 없음')
            return equity.pop(0) if isinstance(equity, list) else equity

    broker = Broker()
    b = Breakers(tmp_path / 'br.json', 'real', lambda: broker, lambda: now[0], notes.append)
    b.open()
    return b, now, notes, broker


# ── 예탁자산 ─────────────────────────────────────────────────
def test_morning_value_becomes_the_base_and_minus_12_percent_trips(tmp_path):
    b, now, notes, _ = make(tmp_path)
    b.feed_equity(10_000_000)
    assert b.day()['base_equity'] == 10_000_000 and not b.day()['base_late']
    b.feed_equity(8_900_000)                      # -11%
    assert b.tripped() == '' and b.day()['drop'] == -11.0
    b.feed_equity(8_800_000)                      # -12.0% → 한도
    assert '예탁자산' in b.tripped() and f'{EQUITY_DROP:+.0f}%' in b.tripped()
    assert any('새 매수만' in n for n in notes)


def test_base_is_marked_late_when_the_server_started_after_the_open(tmp_path):
    b, now, _, _ = make(tmp_path, at=datetime(2026, 10, 19, 13, 0, tzinfo=KST))
    b.feed_equity(5_000_000)
    assert b.day()['base_late'] is True           # 아침 기준을 못 잡았다는 표시


def test_refresh_reads_equity_only_inside_the_watch_window(tmp_path):
    b, now, _, broker = make(tmp_path, equity=10_000_000, at=datetime(2026, 10, 19, 20, 0, tzinfo=KST))
    asyncio.run(b.refresh())
    assert broker.asked == 0                      # 장 시간 밖에서는 조회하지 않는다
    now[0] = MORNING
    asyncio.run(b.refresh())
    assert broker.asked == 1 and b.day()['equity'] == 10_000_000
    asyncio.run(b.refresh())
    assert broker.asked == 1                      # 1분 안에는 다시 조회하지 않는다
    now[0] = MORNING + timedelta(seconds=61)
    asyncio.run(b.refresh())
    assert broker.asked == 2


def test_equity_failure_is_recorded_and_does_not_trip(tmp_path):
    b, _, _, _ = make(tmp_path, equity=None)
    asyncio.run(b.refresh())
    assert b.tripped() == '' and '예탁자산' in b.day()['equity_error']


# ── 주문 거부 ────────────────────────────────────────────────
def test_six_rejects_trip_the_breaker(tmp_path):
    b, _, _, _ = make(tmp_path)
    for i in range(REJECT_MAX - 1):
        b.record_reject('005930', f'거부 {i}')
    assert b.tripped() == ''
    b.check_buy('005930')                         # 아직 살 수 있다
    b.record_reject('005930', '거부 마지막')
    assert f'{REJECT_MAX}건' in b.tripped()
    with pytest.raises(NotSent, match='차단기'):
        b.check_buy('000660')


# ── 매수 건수·금액·되풀이 ──────────────────────────────────────
def test_daily_and_per_stock_buy_counts(tmp_path):
    b, _, _, _ = make(tmp_path)
    for i in range(BUY_MAX_CODE):
        b.record_buy('005930', 100_000)
    with pytest.raises(NotSent, match='이 종목 오늘 매수'):
        b.check_buy('005930')
    b.check_buy('000660')                         # 다른 종목은 된다
    for i in range(BUY_MAX_DAY - BUY_MAX_CODE):
        b.record_buy(f'0000{i:02d}', 100_000)
    with pytest.raises(NotSent, match='하루'):
        b.check_buy('000660')


def test_single_order_amount_cap(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.check_buy('005930', 300_000_000)
    with pytest.raises(NotSent, match='한 번 매수'):
        b.check_buy('005930', 300_000_001)


def test_a_stock_sold_today_is_not_bought_again(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.record_sell('005930')
    with pytest.raises(NotSent, match='되풀이'):
        b.check_buy('005930')
    b.check_buy('000660')


# ── 매도는 막지 않는다 ────────────────────────────────────────
def test_selling_is_never_blocked_by_a_tripped_breaker(tmp_path):
    """가장 중요한 성질: 차단기가 걸려도 보유 종목은 팔 수 있어야 한다."""
    b, _, _, _ = make(tmp_path)
    b.feed_equity(10_000_000)
    b.feed_equity(1_000_000)                      # -90%
    assert b.tripped()
    b.check_sell('005930')                        # 아무 예외도 나지 않는다
    b.record_sell('005930')
    with pytest.raises(NotSent):
        b.check_buy('005930')


def test_sell_burst_blocks_only_a_runaway_loop(tmp_path):
    b, now, _, _ = make(tmp_path)
    for i in range(SELL_BURST - 1):
        b.record_sell('005930')
    b.check_sell('005930')                        # 7건까지는 보낸다
    b.record_sell('005930')
    with pytest.raises(NotSent, match='10분'):
        b.check_sell('005930')
    b.check_sell('000660')                        # 다른 종목은 상관없다
    now[0] = MORNING + timedelta(minutes=11)
    b.check_sell('005930')                        # 10분이 지나면 다시 보낸다


# ── 해제와 재시작 ────────────────────────────────────────────
def test_release_lets_buying_resume_and_does_not_trip_again_today(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.feed_equity(10_000_000)
    b.feed_equity(8_000_000)
    assert b.tripped()
    assert b.release() == ['equity']
    assert b.tripped() == ''
    b.check_buy('005930')
    b.feed_equity(7_000_000)                      # 더 빠져도 다시 걸지 않는다 (사용자가 풀었다)
    assert b.tripped() == '' and b.day()['released'] == ['equity']


def test_state_survives_a_restart(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.record_reject('005930', '거부')
    b.feed_equity(10_000_000)
    b.feed_equity(8_000_000)
    assert b.tripped()
    again = Breakers(tmp_path / 'br.json', 'real', lambda: None, lambda: MORNING)
    again.open()
    assert again.tripped() == b.tripped() and again.day()['rejects'] and again.day()['base_equity'] == 10_000_000
    with pytest.raises(NotSent):
        again.check_buy('005930')


def test_another_account_file_is_not_reused(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.feed_equity(10_000_000)
    b.feed_equity(8_000_000)
    mock = Breakers(tmp_path / 'br.json', 'mock', lambda: None, lambda: MORNING)
    mock.open()
    assert mock.tripped() == '' and not mock.day().get('base_equity')   # 환경이 다르면 쓰지 않는다


def test_day_rolls_over(tmp_path):
    b, now, _, _ = make(tmp_path)
    b.feed_equity(10_000_000)
    b.feed_equity(8_000_000)
    assert b.tripped()
    now[0] = MORNING + timedelta(days=1)
    assert b.tripped() == '' and b.day()['buys'] == []
    b.check_buy('005930')


def test_saved_file_shape(tmp_path):
    b, _, _, _ = make(tmp_path)
    b.record_buy('005930', 500_000, '종가배팅')
    d = json.loads((tmp_path / 'br.json').read_text('utf-8'))
    assert d['environment'] == 'real' and d['days']['2026-10-19']['buys'][0]['by'] == '종가배팅'


def test_no_breakers_blocks_nothing(tmp_path):
    assert NO_BREAKERS.tripped() == '' and NO_BREAKERS.snapshot() == {'on': False}
    NO_BREAKERS.check_buy('005930', 10 ** 12)
    NO_BREAKERS.check_sell('005930')
    NO_BREAKERS.record_reject('005930', 'x')


def test_non_kiwoom_setup_gets_no_breakers(tmp_path):
    from types import SimpleNamespace
    s = SimpleNamespace(broker='mock', data_dir=tmp_path, kiwoom_env='mock', kiwoom_app_key='')
    assert create_breakers(s, lambda: None, lambda: MORNING) is NO_BREAKERS
