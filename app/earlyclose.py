"""⏩ 청산날 앞당기기 (변성환 2026-10-22 규칙).

> "15:12 에 🔥·🌙 후보가 빈 칸보다 많으면 청산 종목을 15:13 시장가로 팔아 그 칸·돈을 15:19 매수에 씁니다
>  (후보가 없으면 15:20 그대로)."

오늘이 **청산날**인 🌅 호라이즌존 보유를 15:20 종가가 아니라 **15:13 정규장 시장가**로 먼저 팔아,
그 돈으로 15:19:40(🔥)·15:19:50(🌙) 매수를 할 수 있게 합니다. 제작자 자료로는 현금 판 잔고가
7.19억 → 8.44억으로 늘고 🔥 129 → 140번 · 🌙 81 → 88번으로 매매가 늘었습니다.

**모자랄 때만** 팝니다. 살 자리와 돈이 넉넉하면 아무것도 하지 않고 원래대로 15:20에 팝니다.

우리 시계와 다른 점: 제작자는 15:12 에 🔥·🌙 후보를 함께 셉니다. 우리 🌙 은 규칙대로 **15:15** 에
판정하므로 15:12 시점에는 🔥 후보만 압니다. 그의 시각을 옮기지 않고, 그 시각에 아는 것으로 셉니다.
"""
from __future__ import annotations

import logging
from datetime import time

from app.closebet import in_window

log = logging.getLogger(__name__)

JUDGE_WINDOW = (time(15, 12), time(15, 12, 59))
SELL_WINDOW = (time(15, 13), time(15, 14))

SLOT_KEY = {'per_trade_amount', 'slot_amount'}


def slot_size(engine):
    """그 매매법의 한 칸 금액 (💧 배수·🧱 상한까지 반영된 값)."""
    base = engine.settings.get('per_trade_amount') or engine.settings.get('slot_amount') or 0
    try:
        return max(1, engine.slot_amount(base))
    except Exception:
        return max(1, base)


class EarlyClose:
    def __init__(self, buyers, exit_engine, wallet, clock, notify=None):
        self.buyers = tuple(buyers)          # (🔥 기준봉 종가, 🌙 종가배팅)
        self.exit_engine = exit_engine       # 호라이즌 자동매도 (🌅 보유를 들고 있다)
        self.wallet, self.clock = wallet, clock
        self.notify = notify or (lambda text: None)
        self.state = {}                      # 날짜 → 그날 기록

    def today(self):
        return self.clock().date().isoformat()

    def day(self):
        return self.state.setdefault(self.today(), {})

    # ── 세기 ──
    def wanted(self):
        """🔥·🌙 이 지금 사고 싶어 하는 종목 수 (그 시각에 아는 후보)."""
        total = 0
        for e in self.buyers:
            if not e.settings.get('enabled'):
                continue
            scan = (e.scans or {}).get(self.today()) or {}
            total += len([r for r in (scan.get('rows') or []) if r.get('pass')])
        return total

    def room(self):
        """지금 더 살 수 있는 종목 수 = 빈 칸과 돈 중 작은 쪽을 매매법마다 더한 것."""
        total = 0
        for e in self.buyers:
            if not e.settings.get('enabled'):
                continue
            slots = e.settings['max_positions'] - len(e.held_codes())
            money = self.wallet.room(e) // slot_size(e)
            total += max(0, min(slots, int(money)))
        return total

    def due(self):
        try:
            engine = self.exit_engine()
            return engine.due_today() if engine else []
        except Exception as exc:
            log.warning('⏩ 청산날 보유 확인 실패: %s', exc)
            return []

    # ── 1초마다 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5:
            return
        d = self.day()
        if in_window(now, JUDGE_WINDOW) and 'judged' not in d:
            wanted, room, due = self.wanted(), self.room(), self.due()
            d.update(judged=now.isoformat(), wanted=wanted, room=room, due=due,
                     need=max(0, wanted - room) if due else 0)
            if d['need']:
                self.notify(f'⏩ 청산날 앞당기기 · 후보 {wanted}종목 / 살 자리 {room} · '
                            f'15:13에 청산날 {min(d["need"], len(due))}종목을 먼저 팝니다')
            return
        if in_window(now, SELL_WINDOW) and d.get('need') and 'sold' not in d:
            d['sold'] = now.isoformat()
            await self.sell(d)

    async def sell(self, d):
        engine = self.exit_engine()
        sold, failed = [], []
        for code in self.due()[:d['need']]:
            try:
                await engine.close_early(code, '⏩ 청산날 앞당기기 · 15:19 매수에 쓰려고 먼저 팝니다')
                sold.append(code)
            except Exception as exc:
                failed.append(f'{code}: {str(exc)[:60]}')
        d['closed'], d['failed'] = sold, failed
        if sold:
            self.notify(f'⏩ 청산날 앞당겨 매도: {", ".join(sold)} (원래는 15:20 종가)')
        if failed:
            self.notify(f'⏩ 앞당기기 실패: {" · ".join(failed)} · 15:20에 원래대로 팝니다')

    def snapshot(self):
        return {'day': self.today(), **self.day()}
