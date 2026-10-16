"""매도 조건 검증 전용. 주문 API를 호출하거나 실제 보유수량을 차감하지 않는다.

평균 체결가 대비 +15% / -10% (비용 전), 최초 매수일 D0 이후 2거래일 KRX 종가.
실제 거래일은 일봉과 당일 KRX 체결로 확인하며 주말을 단순히 영업일로 추정하지 않는다.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
HOLD_SESSIONS = 2  # 매수일 D0를 제외하고 세는 실제 거래일 수. 자동매도와 검증 모드가 공유한다.


def now_kst() -> datetime:
    return datetime.now(KST)


def normal_code(raw: str) -> str:
    code = str(raw).strip()
    if len(code) == 7 and code.startswith('A'):
        code = code[1:]
    if not re.fullmatch(r'[0-9A-Z]{6}', code):
        raise ValueError('종목코드는 6자리여야 합니다')
    return code


def positive(value) -> float:
    x = abs(float(str(value).replace(',', '').strip()))
    if not math.isfinite(x) or x <= 0:
        raise ValueError('가격과 수량은 양수여야 합니다')
    return x


@dataclass
class Position:
    code: str
    name: str
    quantity: int
    average: float
    bought_on: str | None = None
    date_source: str = '매수일 미확인'
    price: float | None = None
    quote_at: str | None = None
    signal: dict | None = None


class SellMonitor:
    def __init__(self, path: Path):
        self.path = path
        self.positions: dict[str, Position] = {}
        self.history: list[dict] = []
        self.sessions: set[str] = set()
        self.calendar_start: str | None = None
        self.calendar_end: str | None = None
        self.error = ''
        self.calendar_error = ''
        self.enabled = False  # 재시작 때 계좌 연결/감시는 반드시 다시 선택한다.
        if path.exists():
            d = json.loads(path.read_text('utf-8'))
            self.positions = {c: Position(**p) for c, p in d.get('positions', {}).items()}
            self.history = d.get('history', [])[-200:]
            self.sessions = set(d.get('sessions', []))
            self.calendar_start, self.calendar_end = d.get('calendar_start'), d.get('calendar_end')
        self._quotes: dict[str, tuple[float, datetime]] = {}
        self._existing_codes: set[str] = set()
        self._new_buy_dates: dict[str, str] = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'positions': {c: asdict(p) for c, p in self.positions.items()},
                                  'history': self.history[-200:], 'sessions': sorted(self.sessions),
                                  'calendar_start': self.calendar_start, 'calendar_end': self.calendar_end},
                                 ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.path)

    def set_calendar(self, sessions: list[str]):
        # 미래 예상 영업일은 받지 않는다. 일봉 자료에 나타난 거래일만 저장한다.
        dates = sorted({date.fromisoformat(s).isoformat() for s in sessions if s <= now_kst().date().isoformat()})
        if not dates:
            raise ValueError('거래일 자료가 비어 있습니다')
        self.sessions.update(dates)
        self.calendar_start, self.calendar_end = dates[0], dates[-1]
        self.calendar_error = ''
        self.save()

    def days_held(self, p: Position, today: date) -> int | None:
        if not p.bought_on or not self.calendar_start or not self.calendar_end:
            return None
        if p.bought_on < self.calendar_start or p.bought_on not in self.sessions:
            return None
        # 최소 전 거래일까지의 일봉 + 오늘 실제 KRX 체결이 있어야 종가 신호를 낸다.
        # calendar_end 이후 미확인 거래일이 있으면 임의 계산하지 않는다.
        if self.calendar_end < today.isoformat():
            return None
        return sum(p.bought_on < d <= today.isoformat() for d in self.sessions)

    def set_buy_date(self, code: str, bought_on: date):
        p = self.positions[normal_code(code)]
        if bought_on > now_kst().date() or bought_on.isoformat() not in self.sessions:
            raise ValueError('확인된 거래일 중 실제 최초 매수일을 선택하세요')
        p.bought_on = bought_on.isoformat()
        p.date_source = '사용자 확인'
        self.save()

    def sync_holdings(self, rows: list[dict], at: datetime):
        """서버의 잔고 조회 전체가 성공했을 때만 동기화. 기존 잔고의 매수일은 추측하지 않는다."""
        fresh = {}
        for r in rows:
            code = normal_code(r['stk_cd'])
            qty = int(r['rmnd_qty'])
            if qty <= 0:
                continue
            # 현금 주식만: 신용/대출 포지션은 자동 포함하지 않는다.
            if str(r.get('crd_tp', '')).strip() not in ('', '0', '00', '000', '현금'):
                continue
            avg = positive(r['pur_pric'])
            old = self.positions.get(code)
            if old:
                old.quantity, old.average = qty, avg
                # 연결 공백 중 전량 매도/재매수가 있었는지 잔고 한 번으로는 알 수 없다.
                old.bought_on, old.date_source = None, '재연결 뒤 매수일 확인'
                fresh[code] = old
            else:
                fresh[code] = Position(code, r.get('stk_nm', code), qty, avg)
        self.positions = fresh
        self._existing_codes = set(fresh)
        self._new_buy_dates.clear()
        self._quotes.clear()  # 잔고 스냅샷 현재가는 신선한 체결로 취급하지 않는다.
        self.save()

    def account_event(self, data: dict, at: datetime):
        """공식 실시간 잔고(04). 절대 보유수량/평단이므로 중복 알림도 수량을 더하지 않는다."""
        if not self.enabled or data.get('type') not in ('00', '04'):
            return
        v = data.get('values') or {}
        if data.get('type') == '00':
            if v.get('913') != '체결' or str(v.get('907')) != '2' or not v.get('909'):
                return
            if str(v.get('922', '')).strip() not in ('', '0', '00', '000', '현금'):
                return
            if positive(v.get('910', 0)) <= 0 or positive(v.get('911', 0)) <= 0:
                return
            code = normal_code(v.get('9001') or data.get('item', ''))
            if code not in self._existing_codes:
                bought = at.astimezone(KST).date().isoformat()
                self._new_buy_dates.setdefault(code, bought)
                if code in self.positions:
                    self.positions[code].bought_on = self._new_buy_dates[code]
                    self.positions[code].date_source = '매수 체결 감지'
                    self.save()
            return
        if str(v.get('917', '')).strip() not in ('', '0', '00', '000', '현금'):
            return
        code = normal_code(v.get('9001') or data.get('item', ''))
        qty = int(v['930'])
        if qty <= 0:
            self.positions.pop(code, None)
            self._quotes.pop(code, None)
            self._existing_codes.discard(code)
            self._new_buy_dates.pop(code, None)
            self.save()
            return
        avg = positive(v['931'])
        old = self.positions.get(code)
        if old:
            old.quantity, old.average = qty, avg
        else:
            # 04의 보유 신규 편입만으로 최초 매수일을 확정하지 않는다.
            # 계좌 이관/누락된 과거 잔고일 수도 있으므로 날짜는 별도 확인한다.
            bought = self._new_buy_dates.get(code)
            self.positions[code] = Position(code, str(v.get('302') or code), qty, avg, bought,
                                            '매수 체결 감지' if bought else '매수일 미확인')
        self.save()

    def quote(self, code: str, price: float, at: datetime, market: str = 'KRX'):
        if self.enabled and code in self.positions and market == 'KRX' and price > 0 and math.isfinite(price):
            self._quotes[code] = (price, at)

    def evaluate(self, at: datetime) -> list[dict]:
        if not self.enabled:
            return []
        at = at.astimezone(KST)
        if at.date().isoformat() not in self.sessions or not time(9) <= at.time() < time(15, 30):
            return []
        out = []
        for p in self.positions.values():
            if p.signal or p.quantity <= 0:
                continue
            quote = self._quotes.get(p.code)
            fresh = quote and 0 <= (at - quote[1]).total_seconds() <= 15
            held = self.days_held(p, at.date())
            reason = None
            # 종가 시점에는 장중 익절/손절보다 D+2 잔량 정리 신호를 우선 기록한다.
            if held is not None and held >= HOLD_SESSIONS and time(15, 20) <= at.time() < time(15, 30):
                reason = 'D2_CLOSE' if held == HOLD_SESSIONS else 'OVERDUE_CLOSE'
            elif fresh:
                price = quote[0]
                if price * 100 >= p.average * 115:
                    reason = 'TAKE_PROFIT'
                elif price * 100 <= p.average * 90:
                    reason = 'STOP_LOSS'
            if fresh:
                p.price, p.quote_at = quote[0], quote[1].isoformat()
            if not reason:
                continue
            signal = {'code': p.code, 'name': p.name, 'quantity': p.quantity, 'reason': reason,
                      'at': at.isoformat(), 'reference_price': quote[0] if fresh else None,
                      'mode': 'shadow', 'sent': False, 'filled': False}
            p.signal = signal
            self.history.append(signal)
            out.append(signal)
        if out:
            self.save()
        return out

    def snapshot(self, at: datetime | None = None) -> dict:
        at = at or now_kst()
        rows = []
        for p in self.positions.values():
            r = asdict(p)
            r.update(days_held=self.days_held(p, at.date()), take_profit=round(p.average * 1.15, 2),
                     stop_loss=round(p.average * .90, 2))
            rows.append(r)
        return {'mode': 'shadow', 'live_orders': False, 'enabled': self.enabled,
                'rules': {'take_profit_pct': 15, 'stop_loss_pct': -10, 'sell_fraction': 1,
                          'hold_sessions': HOLD_SESSIONS, 'day_zero': 'first_buy_fill', 'trailing_enabled': False,
                          'close_market': 'KRX', 'close_window': '15:20~15:30', 'return_basis': 'before_costs'},
                'positions': rows, 'history': self.history[-50:], 'error': self.error,
                'calendar_error': self.calendar_error, 'calendar_end': self.calendar_end}
