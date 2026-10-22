"""⏩ 청산날 앞당기기 (app/earlyclose.py). 모자랄 때만 15:13에 먼저 판다."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.autosell import KST
from app.earlyclose import EarlyClose
from app.kiwoom.orders import NotSent
from app.wallet import Wallet

JUDGE = datetime(2026, 10, 22, 15, 12, 0, tzinfo=KST)
SELL = datetime(2026, 10, 22, 15, 13, 0, tzinfo=KST)


def buyer(passed=0, held=0, max_positions=2, slot=1_000_000, cap=3_000_000, enabled=True):
    rows = [{'code': f'{i:06d}', 'pass': True} for i in range(passed)] + [{'code': 'x', 'pass': False}]
    return SimpleNamespace(
        settings={'enabled': enabled, 'max_positions': max_positions, 'per_trade_amount': slot, 'total_cap': cap},
        scans={'2026-10-22': {'rows': rows}},
        held_codes=lambda: ['h'] * held,
        committed=lambda: held * slot,
        slot_amount=lambda base: base)


class Exit:
    def __init__(self, due, fail=None):
        self._due, self.fail, self.sold = due, fail, []

    def due_today(self):
        return list(self._due)

    async def close_early(self, code, note=''):
        if self.fail:
            raise self.fail
        self.sold.append(code)
        self._due.remove(code)
        return 'ordered'


def make(buyers, due, fail=None, now=None):
    clock = [now or JUDGE]
    ex = Exit(due, fail)
    notes = []
    w = Wallet().register(*buyers)
    e = EarlyClose(buyers, lambda: ex, w, lambda: clock[0], notes.append)
    return SimpleNamespace(e=e, ex=ex, clock=clock, notes=notes)


def run(coro):
    return asyncio.run(coro)


def test_does_nothing_when_there_is_room():
    """후보가 살 자리보다 적으면 아무것도 하지 않는다 (원래대로 15:20 종가에 판다)."""
    t = make([buyer(passed=1, held=0)], due=['005930'])
    run(t.e.tick())
    assert t.e.day()['need'] == 0
    t.clock[0] = SELL
    run(t.e.tick())
    assert t.ex.sold == [] and t.notes == []


def test_sells_the_close_day_holding_when_slots_run_out():
    """후보 3 · 빈 칸 1 → 모자란 2만큼 청산날 보유를 15:13에 판다."""
    t = make([buyer(passed=3, held=1, max_positions=2)], due=['005930', '000660', '035720'])
    run(t.e.tick())
    d = t.e.day()
    assert (d['wanted'], d['room'], d['need']) == (3, 1, 2)
    assert any('15:13' in n for n in t.notes)
    t.clock[0] = SELL
    run(t.e.tick())
    assert t.ex.sold == ['005930', '000660']          # 모자란 만큼만
    assert any('앞당겨 매도' in n for n in t.notes)


def test_money_can_be_the_binding_limit_not_slots():
    """빈 칸은 있어도 지갑에 한 칸 값이 없으면 '살 자리'가 아니다."""
    b = buyer(passed=2, held=0, max_positions=3, slot=1_000_000, cap=1_500_000)
    t = make([b], due=['005930'])
    run(t.e.tick())
    assert t.e.day()['room'] == 1 and t.e.day()['need'] == 1


def test_a_disabled_engine_is_not_counted():
    t = make([buyer(passed=3, held=0, enabled=False)], due=['005930'])
    run(t.e.tick())
    assert t.e.day()['wanted'] == 0 and t.e.day()['need'] == 0


def test_nothing_to_sell_means_nothing_to_do():
    """청산날 보유가 없으면 모자라도 할 일이 없다."""
    t = make([buyer(passed=3, held=2)], due=[])
    run(t.e.tick())
    assert t.e.day()['need'] == 0 and t.notes == []


def test_a_failed_early_sell_is_reported_and_left_to_the_close():
    t = make([buyer(passed=3, held=1)], due=['005930'], fail=NotSent('오늘은 청산날이 아닙니다'))
    run(t.e.tick())
    t.clock[0] = SELL
    run(t.e.tick())
    assert t.ex.sold == [] and t.e.day()['failed']
    assert any('15:20에 원래대로' in n for n in t.notes)


def test_it_judges_once_a_day():
    t = make([buyer(passed=3, held=1)], due=['005930', '000660'])
    run(t.e.tick())
    first = t.e.day()['judged']
    t.clock[0] = JUDGE.replace(second=30)
    run(t.e.tick())
    assert t.e.day()['judged'] == first
    t.clock[0] = SELL
    run(t.e.tick())
    run(t.e.tick())
    assert len(t.ex.sold) == 2                       # 두 번 팔지 않는다


def test_weekend_is_skipped():
    t = make([buyer(passed=3, held=1)], due=['005930'], now=datetime(2026, 10, 24, 15, 12, tzinfo=KST))
    run(t.e.tick())
    assert t.e.day() == {}
