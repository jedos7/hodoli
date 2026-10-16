"""Versioned Horizon D+2 exit plan. Real activation remains user-operated."""
import asyncio
import math
import secrets
from datetime import datetime

from app.horizon_after_hours import HorizonExit as LegacyHorizonExit
from app.autoexit import PENDING
from app.autosell import Position
from app.kiwoom.orders import NotSent, OrderRejected
from app.kiwoom.extended_sell import tick


def ceil_tick(value):
    value = math.ceil(value)
    return ((value + tick(value) - 1) // tick(value)) * tick(value)


def exit_prices(average):
    base = ceil_tick(average)
    return ceil_tick(average * 1.005), ceil_tick(average * 1.15)


class HorizonExit(LegacyHorizonExit):
    hold_sessions = 2
    close_reason = 'D2_CLOSE'

    def pause(self, code=None, note='사용자가 호라이즌 자동매도를 껐습니다'):
        super().pause(code, note)
        for p in self.plans.values():
            if (code is None or p['code'] == code) and p['status'] == 'ordered':
                p.update(status='paused', note=note + ' · 접수 주문은 별도 확인')
        if self.owner and not self.error:
            self.persist()

    async def prepare(self, *args, **kwargs):
        prepared = await super().prepare(*args, **kwargs)
        p = self.plans[prepared['code']]
        breakeven, profit = exit_prices(p['average'])
        try:
            await self.broker().validate_sell_limit(p['code'], breakeven)
            await self.broker().validate_sell_limit(p['code'], profit)
        except Exception:
            p.update(status='paused', note='본전·익절 지정가 호가단위 확인 실패')
            self.persist()
            raise
        p.update(strategy_version=4, original_quantity=p['quantity'],
                 profit_quantity=max(1, p['quantity'] * 70 // 100), profit_done=False, trailing_high=None, trailing_started_at=None,
                 breakeven_price=breakeven, breakeven_trigger=ceil_tick(p['average']), profit_price=profit, opening_seen=[])
        self.persist()
        return p.copy()

    async def arm(self, plan_id, environment, market_order):
        p = next((p for p in self.plans.values() if p['id'] == plan_id), None)
        if not p or p.get('strategy_version') != 4:
            raise NotSent('새 D+2·익절 70% 계획을 다시 확인하세요')
        await super().arm(plan_id, environment, market_order)

    def due(self, p, at):
        held = self.monitor.days_held(Position(p['code'], p['name'], p['quantity'],
                                    p['average'], p['bought_on']), at.date())
        return held is not None and held >= self.hold_sessions and at.date().isoformat() in self.monitor.sessions

    def reason(self, p, at):
        if p.get('strategy_version') != 4:
            return None
        tm = (at.hour, at.minute)
        if self.due(p, at) and (15, 20) <= tm < (15, 30) and self.market(at) == '2':
            return 'D2_CLOSE'
        # No additional profit/breakeven orders during the closing cleanup window.
        if self.due(p, at) and (15, 18) <= tm < (15, 30):
            return None
        reason = super().reason(p, at)
        if reason == 'TAKE_PROFIT' and p.get('profit_done'):
            self.horizon_triggers.pop(p['code'], None)
            reason = None
        if reason:
            return reason
        trail = self.trailing_context(p, at)
        if trail:
            r, ctx = trail
            self.horizon_triggers.pop(p['code'], None)
            if ctx['extended']:
                self.horizon_triggers[p['code']] = dict(reason='TRAILING_STOP', rule=r, context=ctx.copy())
            return 'TRAILING_STOP'
        if not self.due(p, at):
            return None
        # Use the first fresh quote at the opening, never an arbitrary midday quote.
        slot = '08' if tm == (8, 0) else '09' if tm == (9, 0) else None
        if not slot:
            return None
        key = at.date().isoformat() + '-' + slot
        if key in p['opening_seen']:
            return None
        r = p['rules'][0]
        ctx = self.ticket_context(p, r, at)
        if not ctx or (slot == '08' and ctx['venue'] != 'NXT'):
            return None
        start = at.replace(minute=0, second=0, microsecond=0)
        if ctx['at'] < start:
            return None
        p['opening_seen'].append(key)
        self.persist()
        if ctx['price'] >= p['breakeven_trigger']:
            return None
        self.horizon_triggers[p['code']] = dict(reason='BREAKEVEN', rule=r, context=ctx.copy())
        return 'BREAKEVEN'

    async def execute(self, p, reason, trigger_at, epoch):
        # Keep proven market/extended stop-loss transport and repricing intact.
        if reason in ('STOP_LOSS', 'TRAILING_STOP'):
            return await super().execute(p, reason, trigger_at, epoch)
        trigger = self.horizon_triggers.pop(p['code'], None)
        ctx = trigger['context'] if trigger else None
        extended = bool(ctx and ctx['extended'])
        venue = ctx['venue'] if ctx else 'KRX'
        quantity = min(p['quantity'], p['profit_quantity']) if reason == 'TAKE_PROFIT' else p['quantity']
        price = p['profit_price'] if reason == 'TAKE_PROFIT' else p['breakeven_price'] if reason == 'BREAKEVEN' else None
        order = None
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    if epoch != self.epoch or p['status'] != 'checking' or self.plans.get(p['code']) is not p:
                        raise NotSent('감시 중지·계획 변경')
                    if not 0 <= (self.clock() - trigger_at).total_seconds() <= 10:
                        raise NotSent('주문 준비 지연')
                    if reason == 'D2_CLOSE':
                        if not self.due(p, self.clock()) or self.market(self.clock()) != '2':
                            raise NotSent('D+2 마감 동시호가 확인 필요')
                    elif extended:
                        live = self.ticket_context(p, trigger['rule'], self.clock(), venue)
                        if not live or not live['extended']:
                            raise NotSent('시간외 거래 상태 변경')
                    elif self.market(self.clock()) != '3':
                        raise NotSent('정규장 거래 상태 변경')
                guard()
                row = await self.inspect(p['code'])
                guard()
                if (row['quantity'], row['average']) != (p['quantity'], p['average']):
                    raise NotSent('잔고 변경 · 다시 확인하세요')
                if price is not None:
                    await self.broker().validate_sell_limit(p['code'], price)
                    guard()
                order = dict(id=secrets.token_hex(12), plan_id=p['id'], code=p['code'], name=p['name'],
                    quantity=quantity, position_quantity=p['quantity'], reason=reason, environment=self.env,
                    day=trigger_at.date().isoformat(), at=trigger_at.isoformat(), status='submitting',
                    order_no='', filled=0, remaining=quantity, checked_at=None, note='주문 전송 중',
                    order_type='market' if price is None else 'limit', limit_price=price,
                    after_hours=extended, venue=venue, strategy_version=4, chase_active=False)
                self.orders.append(order)
                self.persist()
                b = self.broker()
                if price is None:
                    number = await b.sell_once(p['code'], quantity, guard)
                elif extended:
                    number = await b.extended_sell_once(p['code'], quantity, price, venue, guard)
                else:
                    number = await b.sell_limit_once(p['code'], quantity, price, guard)
                order.update(order_no=number, status='accepted', note='접수 · 체결 확인 중')
                p.update(status='ordered', note=order['note'])
            self.persist()
        except (NotSent, OrderRejected) as exc:
            if order:
                order.update(status='not_sent' if isinstance(exc, NotSent) else 'rejected', note=str(exc))
            p.update(status='paused', note=str(exc))
            self.persist()
        except (Exception, asyncio.CancelledError):
            if order:
                order.update(status='unknown', note='주문 응답 불명 · 재전송 차단')
            p.update(status='paused', note='주문 결과 확인 필요')
            if not self.error:
                self.persist()

    def check(self):
        # The regular timer also handles cancellation while an order is pending.
        at = self.clock()
        gap = (at - self.last_check).total_seconds()
        for p in self.plans.values():
            if p.get('strategy_version') != 4 or p['status'] != 'ordered' or p.get('schedule_busy'):
                continue
            if not self.connected() or gap < 0 or gap > 15:
                p.update(status='paused', note='감시 공백 · 미체결 주문 직접 확인 필요')
                self.persist()
                continue
            stop = self.loss_hit(p, at)
            close = self.due(p, at) and (15, 18) <= (at.hour, at.minute) < (15, 30)
            if stop or close:
                p['schedule_busy'] = True
                task = asyncio.create_task(self.cancel_pending(p, self.epoch))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        super().check()

    def loss_hit(self, p, at):
        r = next((r for r in p.get('rules', []) if r['kind'] == 'loss'), None)
        ctx = self.ticket_context(p, r, at) if r else None
        return bool(ctx and ctx['price'] * 100 <= p['average'] * 90) or bool(self.trailing_context(p, at))

    def trailing_context(self, p, at):
        if not p.get('trailing_started_at') or not p.get('profit_done'):
            return None
        r = next((r for r in p.get('rules', []) if r['kind'] == 'loss'), None)
        ctx = self.ticket_context(p, r, at) if r else None
        if not ctx or ctx['at'] < datetime.fromisoformat(p['trailing_started_at']):
            return None
        high = max(p.get('trailing_high') or p['profit_price'], ctx['price'])
        if high != p.get('trailing_high'):
            p['trailing_high'] = high
            self.persist()
        return (r, ctx) if ctx['price'] * 100 <= high * 97 else None

    async def cancel_pending(self, p, epoch):
        try:
            async with self.reconcile_lock:
                async with self.lock:
                    def guard():
                        self.gate()
                        if epoch != self.epoch or p['status'] != 'ordered':
                            raise NotSent('감시 중지 · 취소 중단')
                    guard()
                    for o in self.orders:
                        if o.get('plan_id') != p['id'] or o['status'] not in PENDING or o.get('scheduled_cancel'):
                            continue
                        if o['reason'] not in ('TAKE_PROFIT', 'BREAKEVEN') or not o.get('order_no'):
                            continue
                        if o['day'] != self.clock().date().isoformat() or o.get('manual_cancel_state'):
                            raise NotSent('원주문 날짜·취소 상태 확인 필요')
                        d = await self.broker().detail(o)
                        guard()
                        f, remaining = self.cancel_detail_valid(o, d)
                        if not remaining or d['rejected']:
                            continue
                        o.update(scheduled_cancel='sending', chase_active=False)
                        self.persist()
                        try:
                            number = await self.broker().extended_cancel_once(o, guard)
                            o.update(scheduled_cancel='accepted', cancel_no=number, note='청산 취소 접수 · 완료 확인 대기')
                        except (Exception, asyncio.CancelledError):
                            o.update(scheduled_cancel='unknown', note='취소 응답 불명 · 추가 주문 차단')
                            raise
                        self.persist()
        except (Exception, asyncio.CancelledError):
            p.update(status='paused', note='취소 결과 확인 필요 · 추가 주문 차단')
        finally:
            p.pop('schedule_busy', None)
            if not self.error:
                self.persist()

    async def _reconcile(self):
        epoch = self.epoch
        active = {p['id'] for p in self.plans.values() if p['status'] == 'ordered' and p.get('strategy_version') == 4}
        await super()._reconcile()
        if epoch != self.epoch or not self.connected():
            return
        for p in self.plans.values():
            if p['id'] not in active:
                continue
            orders = [o for o in self.orders if o.get('plan_id') == p['id']]
            if not orders:
                continue
            o = orders[-1]
            if o.get('strategy_version') != 4:
                continue
            if o.get('scheduled_cancel'):
                try:
                    d = await self.broker().detail(o)
                    f, remaining = self.cancel_detail_valid(o, d)
                    if f != o['quantity']:
                        if o['scheduled_cancel'] != 'accepted':
                            raise NotSent('취소 결과 불명')
                        cancel = await self.broker().extended_cancel_detail(o)
                        if remaining or not cancel or cancel['confirmed'] != o['quantity'] - f:
                            p.update(status='ordered', note='취소 완료 대기 · 잔량 매도 차단')
                            continue
                    o.update(filled=f, remaining=0, status='filled' if f == o['quantity'] else 'closed', scheduled_cancel='confirmed')
                except Exception:
                    p.update(status='paused', note='취소 완료 확인 실패 · 추가 주문 차단')
                    continue
            if o['status'] not in ('filled', 'closed'):
                continue
            if o['status'] == 'closed' and o.get('scheduled_cancel') != 'confirmed':
                p.update(status='paused', note='주문 종료 사유 확인 필요')
                continue
            if o.get('manual_cancel_state'):
                p.update(status='paused', note='사용자 취소 · 감시 재개 안 함')
                continue
            try:
                expected = o['position_quantity'] - o['filled']
                b = self.broker()
                row = (await b.positions()).get(p['code'])
                if await b.open_orders(p['code']):
                    raise NotSent('미체결 주문 대기')
                if epoch != self.epoch or not self.connected():
                    return
                if expected == 0:
                    if row and row['quantity']:
                        raise NotSent('잔고 반영 대기')
                    p.update(status='completed', quantity=0, note='전량 체결·잔고 확인 완료')
                else:
                    if not row or (row['quantity'], row['available'], row['average']) != (expected, expected, p['average']):
                        raise NotSent('실제 잔고 일치 확인 대기')
                    p.update(quantity=expected, available=expected, status='armed', note='잔량 손절·D+2 감시 중')
                    if o['reason'] == 'TAKE_PROFIT':
                        p['profit_done'] = True
                        if o['filled'] == o['quantity'] and not p.get('trailing_started_at'):
                            p.update(trailing_started_at=self.clock().isoformat(), trailing_high=p['profit_price'])
                    self.quotes.pop(p['code'], None)
                    for v in ('KRX', 'NXT'):
                        self.extended_quotes.pop((p['code'], v), None)
            except Exception:
                p.update(status='ordered', note='체결 후 실제 잔고 확인 대기 · 추가 주문 차단')
        if self.owner and not self.error:
            self.persist()

    def snapshot(self):
        s = super().snapshot()
        s['rules'].update(hold_sessions=2, take_profit_quantity_pct=70, trailing_drawdown_pct=3, strategy_version=4)
        return s
