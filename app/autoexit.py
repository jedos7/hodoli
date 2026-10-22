"""Opt-in automatic exits with a durable journal and fail-closed submission gate."""
import asyncio
import json
import math
import os
import secrets
from datetime import date, datetime
from pathlib import Path

from app.autosell import HOLD_SESSIONS, KST, Position, normal_code, now_kst
from app.kiwoom.orders import NotSent, OrderRejected, OrderUnknown

PENDING = {'submitting', 'unknown', 'accepted', 'partial', 'attention'}


class AutoExit:
    hold_sessions = HOLD_SESSIONS
    close_reason = "D2_CLOSE"
    early_reason = "D2_CLOSE_EARLY"      # ⏩ 청산날 앞당기기 (app/earlyclose.py) · 정규장 시장가

    def __init__(self, path, env, broker, connected, phase, monitor, clock=now_kst):
        self.path, self.env = Path(path), env
        self.broker, self.connected, self.phase, self.monitor, self.clock = broker, connected, phase, monitor, clock
        self.plans, self.orders, self.quotes = {}, [], {}
        self.error, self.owner, self.epoch = '', None, 0
        self.last_check = self.clock()
        self.lock = asyncio.Lock()
        self.reconcile_lock = asyncio.Lock()
        self.tasks = set()

    def open(self):
        """Hold an OS lock for this account/environment for the process lifetime."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path.with_suffix('.lock'), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner = handle
            if self.path.exists():
                d = json.loads(self.path.read_text('utf-8'))
                if d['environment'] != self.env:
                    raise ValueError('저장된 계좌 환경 불일치')
                self.plans, self.orders = d['plans'], d['orders']
            for plan in self.plans.values():
                plan['status'], plan['note'] = 'paused', '서버 재시작 · 다시 확인 후 켜세요'
            for order in self.orders:
                if order['status'] == 'submitting':
                    order['status'], order['note'] = 'unknown', '전송 중 재시작 · 주문내역 확인 필요'
            self.save()
        except Exception:
            self.error = '자동매도 저장파일 또는 중복 실행 확인 필요 · 주문 차단'
            if self.owner:
                self.close_lock()
            else:
                handle.close()

    def close_lock(self):
        if self.owner:
            self.owner.close()
            self.owner = None

    def save(self):
        if not self.owner:
            raise RuntimeError('자동매도 실행 잠금을 확보하지 못했습니다')
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'environment': self.env, 'plans': self.plans,
                       'orders': self.orders}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def persist(self):
        try:
            self.save()
        except Exception:
            self.error = '주문기록 저장 실패 · 자동매도 중단 · 키움 주문내역 확인 필요'
            self.epoch += 1
            for p in self.plans.values():
                p['status'] = 'paused'
            raise

    def blocked(self, code):
        return any(o['code'] == code and o['status'] in PENDING for o in self.orders)

    async def remove_plan(self, code, plan_id):
        """Delete only the reviewed inactive plan; retain every order record."""
        async with self.reconcile_lock:
            async with self.lock:
                if not self.owner or self.error:
                    raise NotSent('저장 상태를 확인할 수 없어 삭제하지 않았습니다')
                code = normal_code(code)
                p = self.plans.get(code)
                if not p or p['id'] != plan_id:
                    raise NotSent('조건이 변경되었거나 이미 삭제되었습니다 · 다시 확인하세요')
                if p['status'] not in ('paused', 'completed'):
                    raise NotSent('이 종목의 조건 감시를 먼저 끈 뒤 삭제하세요')
                if self.blocked(code):
                    raise NotSent('미체결·미확정 주문이 있어 삭제할 수 없습니다 · 키움 주문내역을 확인하세요')
                del self.plans[code]
                try:
                    self.persist()
                except Exception:
                    self.plans[code] = p
                    p['status'] = 'paused'
                    raise
                self.quotes.pop(code, None)
                # Do not invalidate another stock's in-flight preflight check.
                # Removing an inactive plan neither sends nor cancels an order.
                return {'deleted_code': code, 'deleted_plan_id': plan_id}

    def gate(self):
        if self.error or not self.owner or not self.connected():
            raise NotSent('실시간 계좌 연결을 확인하세요')

    async def inspect(self, code):
        broker = self.broker()
        positions = await broker.positions()
        row = positions.get(code)
        if not row or row['quantity'] <= 0:
            raise NotSent('해당 현금 잔고가 없습니다')
        if row['available'] != row['quantity']:
            raise NotSent('전량 주문가능 수량을 확인하세요')
        if await broker.open_orders(code):
            raise NotSent('이 종목에 기존 미체결 주문이 있습니다 · 키움에서 확인하세요')
        return row

    async def prepare(self, code, bought_on):
        async with self.lock:
            self.gate()
            epoch = self.epoch
            code = normal_code(code)
            if self.blocked(code):
                raise NotSent('이전 주문이 미확정입니다 · 주문내역을 먼저 확인하세요')
            if self.plans.get(code, {}).get('status') in ('armed', 'checking'):
                raise NotSent('먼저 이 종목의 자동매도를 끄세요')
            if bought_on > self.clock().date() or bought_on.isoformat() not in self.monitor.sessions:
                raise NotSent('확인된 거래일 중 최초 매수일을 선택하세요')
            row = await self.inspect(code)
            self.gate()
            if epoch != self.epoch:
                raise NotSent('계좌 연결/감시 상태가 변경됐습니다 · 다시 확인하세요')
            p = row | {'id': secrets.token_urlsafe(18), 'bought_on': bought_on.isoformat(),
                       'prepared_at': self.clock().isoformat(), 'status': 'prepared',
                       'note': '계획 확인 후 시작할 수 있습니다', 'environment': self.env,
                       'hold_sessions': self.hold_sessions}
            self.plans[code] = p
            self.persist()
            return p.copy()

    async def arm(self, plan_id, environment, market_order):
        async with self.lock:
            self.gate()
            epoch = self.epoch
            p = next((p for p in self.plans.values() if p['id'] == plan_id), None)
            if not p or p['status'] != 'prepared' or self.blocked(p['code']):
                raise NotSent('매도 계획을 다시 확인하세요')
            if p.get('hold_sessions') != self.hold_sessions:
                raise NotSent('보유기간 설정이 변경됐습니다 · 매도 계획을 다시 확인하세요')
            if environment != self.env or market_order is not True:
                raise NotSent('계좌 환경과 시장가 전량 매도를 확인하세요')
            if not 0 <= (self.clock() - datetime.fromisoformat(p['prepared_at'])).total_seconds() <= 90:
                raise NotSent('계획 확인 시간이 지났습니다 · 다시 확인하세요')
            row = await self.inspect(p['code'])
            self.gate()
            if epoch != self.epoch or p['status'] != 'prepared':
                raise NotSent('시작 준비 중 중단되었습니다 · 계획을 다시 확인하세요')
            if (row['quantity'], row['average']) != (p['quantity'], p['average']):
                raise NotSent('잔고가 바뀌었습니다 · 계획을 다시 확인하세요')
            # A preparation quote or yesterday's price must never activate an order.
            self.quotes.pop(p['code'], None)
            p['status'], p['note'] = 'armed', f'익절·손절·D+{self.hold_sessions} 종가 감시 중'
            self.last_check = self.clock()
            self.persist()

    def pause(self, code=None, note='사용자가 자동매도를 껐습니다'):
        self.epoch += 1  # Also invalidates an in-flight preflight check before transmission.
        for p in self.plans.values():
            if (code is None or p['code'] == code) and p['status'] in ('armed', 'checking', 'prepared'):
                p['status'], p['note'] = 'paused', note
        if self.owner and not self.error:
            self.persist()

    def market(self, at):
        phase = self.phase() or {}
        stamp = phase.get('at')
        if not stamp:
            return ''
        stamp = datetime.fromisoformat(stamp)
        if stamp.date() != at.date() or stamp > at or not 8 <= at.hour < 18:
            return ''
        value = phase.get('value', '')
        if value == '2':
            # FID 214 is the OPENING countdown, not a documented closing timer.
            # Act on a recent KRX closing-auction notification only. If that
            # notification is missed, wait visibly rather than guessing the session.
            elapsed = (at - stamp).total_seconds()
            return '2' if elapsed <= 90 else ''
        return '3' if value == '3' else ''

    def reason(self, p, at):
        market = self.market(at)
        if market == '2':
            held = self.monitor.days_held(Position(p['code'], p['name'], p['quantity'],
                                                  p['average'], p['bought_on']), at.date())
            if held is not None and held >= self.hold_sessions:
                return self.close_reason
        if market != '3':
            return None
        q = self.quotes.get(p['code'])
        if q and 0 <= (at - q[1]).total_seconds() <= 10:
            if q[0] * 100 >= p['average'] * 115:
                return 'TAKE_PROFIT'
            if q[0] * 100 <= p['average'] * 90:
                return 'STOP_LOSS'
        return None

    def quote(self, code, price, at, market):
        if market != 'KRX' or not math.isfinite(price) or price <= 0:
            return
        self.quotes[code] = (price, at)
        self.check()

    def check(self):
        at = self.clock()
        gap = (at - self.last_check).total_seconds()
        self.last_check = at
        active = [p for p in self.plans.values() if p['status'] == 'armed']
        if not active:
            return
        if gap < 0 or gap > 15:
            self.pause(note='PC 절전/감시 공백 감지 · 계좌 확인 후 다시 켜세요')
            return
        if not self.connected():
            self.pause(note='실시간 연결 중단 · 계좌 확인 후 다시 켜세요')
            return
        for p in active:
            reason = self.reason(p, at)
            if not reason:
                continue
            p['status'], p['note'] = 'checking', '조건 도달 · 주문 전 잔고 확인 중'
            self.persist()
            task = asyncio.create_task(self.execute(p, reason, at, self.epoch))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def execute(self, p, reason, trigger_at, epoch):
        order = None
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    if epoch != self.epoch or p['status'] != 'checking':
                        raise NotSent('자동매도가 꺼졌거나 잔고가 변경되었습니다')
                    market = self.market(self.clock())
                    if market != ('2' if reason == self.close_reason else '3'):
                        raise NotSent('정규장 거래 상태를 확인하세요')
                    if not 0 <= (self.clock() - trigger_at).total_seconds() <= 10:
                        raise NotSent('주문 준비 지연/시계 변경 · 다시 확인하세요')
                guard()
                row = await self.inspect(p['code'])
                guard()
                if (row['quantity'], row['average']) != (p['quantity'], p['average']):
                    raise NotSent('잔고가 변경되어 자동매도를 중단했습니다')
                order = {'id': secrets.token_hex(12), 'plan_id': p['id'], 'code': p['code'], 'name': p['name'],
                         'quantity': p['quantity'], 'reason': reason, 'environment': self.env,
                         'day': trigger_at.date().isoformat(), 'at': trigger_at.isoformat(),
                         'status': 'submitting', 'order_no': '', 'filled': 0, 'remaining': p['quantity'],
                         'note': '주문 전송 중', 'checked_at': None}
                self.orders.append(order)
                self.persist()  # Durable intent BEFORE the first byte can reach the broker.
                order['order_no'] = await self.broker().sell_once(p['code'], p['quantity'], guard)
                order['status'], order['note'] = 'accepted', '주문 접수 · 체결 확인 중'
                p['status'], p['note'] = 'ordered', '접수된 주문의 체결 확인 중'
                self.persist()
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            p['status'], p['note'] = 'paused', str(exc)
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
            p['status'], p['note'] = 'paused', str(exc)
        except (Exception, asyncio.CancelledError):
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 확인 필요'
            p['status'], p['note'] = 'paused', '자동매도 중단 · 주문내역 확인 필요'
        finally:
            if not self.error:
                try:
                    self.persist()
                except Exception:
                    pass

    def due_today(self):
        """오늘이 **청산날**인 보유 종목 코드 (자동매도가 켜진 것만)."""
        today = self.clock().date()
        out = []
        for p in self.plans.values():
            if p['status'] != 'armed':
                continue
            held = self.monitor.days_held(Position(p['code'], p['name'], p['quantity'],
                                                  p['average'], p['bought_on']), today)
            if held is not None and held >= self.hold_sessions:
                out.append(p['code'])
        return out

    async def close_early(self, code, note=''):
        """⏩ 청산날 앞당기기 — 오늘이 청산날인 보유를 **정규장에서 지금** 시장가로 판다.

        청산날이 아닌 종목은 건드리지 않는다. 익절·손절 감시와 같은 전송 규율을 그대로 쓴다
        (전송 전 저널 · 1회 전송 · 잔고 재확인). 원래는 15:20 종가 단일가에 파는 것을
        15:13 으로 앞당기는 것뿐이다."""
        p = self.plans.get(code)
        at = self.clock()
        if not p or p['status'] != 'armed':
            raise NotSent('자동매도가 켜진 보유가 아닙니다')
        if code not in self.due_today():
            raise NotSent('오늘은 이 종목의 청산날이 아닙니다')
        if self.market(at) != '3':
            raise NotSent('정규장 거래 상태를 확인하세요')
        p['status'], p['note'] = 'checking', note or '⏩ 청산날 앞당기기 · 주문 전 잔고 확인 중'
        self.persist()
        await self.execute(p, self.early_reason, at, self.epoch)
        return p['status']

    def account_changed(self):
        for p in self.plans.values():
            if p['status'] not in ('armed', 'checking'):
                continue
            actual = self.monitor.positions.get(p['code'])
            if not actual or (actual.quantity, actual.average) != (p['quantity'], p['average']):
                self.pause(p['code'], '수동 매매/잔고 변경 · 새 잔고로 다시 확인하세요')

    async def reconcile(self):
        # The background poll and the user's refresh may run simultaneously.
        async with self.reconcile_lock:
            await self._reconcile()

    def order_session_open(self, order, at):
        return at.date().isoformat() == order['day'] and self.market(at) in ('2', '3')

    async def _reconcile(self):
        for o in self.orders:
            if o['status'] not in PENDING or not o['order_no']:
                continue
            try:
                detail = await self.broker().detail(o)
                if detail is None:
                    o['note'] = '주문내역 조회에 없음 · 체결로 간주하지 않음 · 키움 확인 필요'
                    continue
                filled, remaining = detail['filled'], detail['remaining']
                if not 0 <= filled <= o['quantity'] or not 0 <= remaining <= o['quantity'] - filled:
                    raise ValueError('체결수량 불일치')
                if filled < o['filled']:
                    raise ValueError('이전 체결수량보다 작은 응답')
                o.update(filled=filled, remaining=remaining, checked_at=self.clock().isoformat())
                if filled == o['quantity']:
                    o['status'], o['note'] = 'filled', '전량 체결 확인'
                elif detail['rejected'] or remaining == 0:
                    o['status'], o['note'] = 'closed', '미체결 잔량 취소/거부 확인 · 자동 재전송 없음'
                else:
                    o['status'] = 'partial' if filled else 'accepted'
                    o['note'] = '일부 체결 · 잔량 대기' if filled else '접수 · 체결 대기'
                    if not self.order_session_open(o, self.clock()):
                        o['status'], o['note'] = 'attention', '미체결 잔량 확인 필요 · 키움 주문내역 확인'
            except Exception:
                o['note'] = '체결 조회 실패 · 접수 주문 재전송 차단 · 키움 확인 필요'
        for o in self.orders:
            p = self.plans.get(o['code'])
            if p and p['id'] == o.get('plan_id') and p['status'] == 'ordered':
                p['note'] = o['note']
                if o['status'] == 'filled':
                    p['status'] = 'completed'
                elif o['status'] == 'closed':
                    p['status'] = 'paused'
        if self.owner and not self.error:
            self.persist()

    def snapshot(self):
        plans = []
        at = self.clock()
        market = self.market(at)
        for p in self.plans.values():
            row = p.copy()
            q = self.quotes.get(p['code'])
            if p['status'] == 'armed':
                if not market:
                    row['note'] = '켜짐 · KRX 장 상태 수신 대기 (아직 주문 판단하지 않음)'
                elif market == '3' and (not q or not 0 <= (at-q[1]).total_seconds() <= 10):
                    row['note'] = '켜짐 · 최신 KRX 체결가 대기'
                elif self.monitor.days_held(Position(p['code'], p['name'], p['quantity'], p['average'],
                                                     p['bought_on']), at.date()) is None:
                    row['note'] = f'익절·손절 감시 · D+{self.hold_sessions} 거래일 자료 확인 대기'
            plans.append(row)
        return {'environment': self.env, 'enabled': any(p['status'] in ('armed', 'checking') for p in self.plans.values()),
                'rules': {'hold_sessions': self.hold_sessions, 'day_zero': 'first_buy_fill',
                          'take_profit_pct': 15, 'stop_loss_pct': -10},
                'connected': bool(self.connected()), 'market': market,
                'plans': plans, 'orders': self.orders[-100:], 'error': self.error,
                'asof': self.clock().isoformat()}
