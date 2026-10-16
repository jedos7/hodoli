"""User-defined exits and a fixed protection price, separate from Horizon."""
import asyncio
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.autoexit import AutoExit, PENDING
from app.autosell import normal_code
from app.kiwoom.orders import NotSent, OrderRejected
from app.exit_tickets import TicketMixin, rules_of
from app.after_hours_exit import AfterHoursMixin, extended_session, selected_venues


def condition(pct, quantity, average, held, loss=False):
    if pct is None and quantity is None:
        return None
    if pct is None or quantity is None or isinstance(pct, bool):
        raise ValueError('사용할 조건의 퍼센트와 수량을 모두 입력하세요')
    try:
        value = Decimal(str(pct))
    except InvalidOperation as exc:
        raise ValueError('퍼센트를 숫자로 입력하세요') from exc
    if not value.is_finite() or value <= 0 or value > 1000 or (loss and value >= 100):
        raise ValueError('익절은 0% 초과 1,000% 이하, 손절은 0% 초과 100% 미만입니다')
    if value != value.quantize(Decimal('0.01')):
        raise ValueError('퍼센트는 소수점 둘째 자리까지 입력하세요')
    if type(quantity) is not int or not 1 <= quantity <= held:
        raise ValueError('매도 수량은 보유 수량 이하의 양의 정수여야 합니다')
    target = Decimal(str(average)) * (100 - value if loss else 100 + value) / 100
    return {'pct': float(value), 'quantity': quantity, 'price': str(target)}


def protection_condition(price, held):
    if price is None:
        return None
    if type(price) is not int or not 1 <= price < 10**9:
        raise ValueError('이익보전 기준가는 1원 이상 정수로 입력하세요')
    return {'price': str(price), 'quantity': held, 'comparison': 'lt', 'all_remaining': True}


class CustomExit(AfterHoursMixin, TicketMixin, AutoExit):
    """Reuse durability, connection guards and reconciliation; no Horizon time exit."""

    async def prepare(self, code, take_profit_pct=None, take_profit_quantity=None,
                      stop_loss_pct=None, stop_loss_quantity=None, profit_protection_price=None):
        async with self.lock:
            self.gate()
            code = normal_code(code)
            if self.blocked(code):
                raise NotSent('이전 주문이 미확정입니다 · 키움 주문내역을 먼저 확인하세요')
            if self.plans.get(code, {}).get('status') in ('armed', 'checking'):
                raise NotSent('이 종목의 직접 설정 자동매도를 먼저 끄세요')
            epoch = self.epoch
            row = await self.inspect(code)
            self.gate()
            if epoch != self.epoch:
                raise NotSent('계좌 연결/감시 상태가 변경됐습니다 · 다시 확인하세요')
            tp = condition(take_profit_pct, take_profit_quantity, row['average'], row['quantity'])
            sl = condition(stop_loss_pct, stop_loss_quantity, row['average'], row['quantity'], loss=True)
            protection = protection_condition(profit_protection_price, row['quantity'])
            if tp is None and sl is None and protection is None:
                raise ValueError('익절·손절·이익보전 중 하나 이상 설정하세요')
            if protection and tp and Decimal(protection['price']) > Decimal(tp['price']):
                raise ValueError('이익보전 기준가는 익절 목표 가격 이하여야 합니다 · 조건 중복을 확인하세요')
            p = row | {'id': secrets.token_urlsafe(18), 'prepared_at': self.clock().isoformat(),
                       'status': 'prepared', 'note': '직접 설정한 조건·수량을 확인 후 켜세요',
                       'environment': self.env, 'mode': 'custom', 'take_profit': tp, 'stop_loss': sl,
                       'profit_protection': protection,
                       'after_trigger': 'keep_stop_after_profit', 'take_profit_done': False,
                       'original_quantity': row['quantity'], 'continue_stop': True}
            self.plans[code] = p
            self.persist()
            return p.copy()

    async def arm(self, plan_id, environment, market_order):
        async with self.lock:
            self.gate()
            epoch = self.epoch
            p = next((p for p in self.plans.values() if p['id'] == plan_id), None)
            if not p or p['status'] != 'prepared' or p.get('mode') != 'custom' or self.blocked(p['code']):
                raise NotSent('직접 설정 매도 계획을 다시 확인하세요')
            if environment != self.env or market_order is not True:
                raise NotSent('계좌 환경과 설정 수량 시장가 매도를 확인하세요')
            if not 0 <= (self.clock()-datetime.fromisoformat(p['prepared_at'])).total_seconds() <= 90:
                raise NotSent('계획 확인 시간이 지났습니다 · 다시 확인하세요')
            row = await self.inspect(p['code'])
            self.gate()
            if epoch != self.epoch or p['status'] != 'prepared':
                raise NotSent('시작 준비 중 중단되었습니다 · 계획을 다시 확인하세요')
            if (row['quantity'], row['average']) != (p['quantity'], p['average']):
                raise NotSent('잔고가 바뀌었습니다 · 계획을 다시 확인하세요')
            self.quotes.pop(p['code'], None)
            p['status'], p['note'] = 'armed', ('이익보전 포함 조건 감시 중 · 지정 가격 미만이면 남은 잔고 전량 매도'
                if p.get('profit_protection') else '직접 설정 조건 감시 중 · 익절 후 남은 수량 손절 감시 계속')
            self.last_check = self.clock()
            self.persist()

    def reason(self, p, at):
        if p.get('mode') == 'tickets':
            return self.ticket_reason(p, at)
        if self.market(at) != '3':
            return None
        q = self.quotes.get(p['code'])
        if not q or not 0 <= (at-q[1]).total_seconds() <= 10:
            return None
        price = Decimal(str(q[0]))
        if p.get('profit_protection') and price < Decimal(p['profit_protection']['price']):
            return 'CUSTOM_PROFIT_PROTECTION'
        if not p.get('take_profit_done') and p['take_profit'] and price >= Decimal(p['take_profit']['price']):
            return 'CUSTOM_TAKE_PROFIT'
        if p['stop_loss'] and price <= Decimal(p['stop_loss']['price']):
            return 'CUSTOM_STOP_LOSS'
        return None

    async def execute(self, p, reason, trigger_at, epoch):
        if p.get('mode') == 'tickets':
            return await self.ticket_execute(p, reason, trigger_at, epoch)
        order = None
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    if epoch != self.epoch or p['status'] != 'checking':
                        raise NotSent('직접 설정 자동매도가 꺼졌거나 잔고가 변경되었습니다')
                    if self.market(self.clock()) != '3':
                        raise NotSent('KRX 연속매매 상태를 확인하세요')
                    if not 0 <= (self.clock()-trigger_at).total_seconds() <= 10:
                        raise NotSent('주문 준비 지연/시계 변경 · 다시 확인하세요')
                guard()
                row = await self.inspect(p['code'])
                guard()
                if (row['quantity'], row['average']) != (p['quantity'], p['average']):
                    raise NotSent('잔고가 변경되어 직접 설정 자동매도를 중단했습니다')
                leg = {'CUSTOM_TAKE_PROFIT':p['take_profit'], 'CUSTOM_STOP_LOSS':p['stop_loss'],
                       'CUSTOM_PROFIT_PROTECTION':p.get('profit_protection')}.get(reason)
                if not leg:
                    raise NotSent('매도 조건을 다시 확인하세요')
                quantity = leg['quantity']
                if type(quantity) is not int or not 1 <= quantity <= row['available']:
                    raise NotSent('설정 수량의 주문가능 잔고를 확인하세요')
                order = {'id': secrets.token_hex(12), 'plan_id': p['id'], 'code': p['code'], 'name': p['name'],
                         'quantity': quantity, 'reason': reason, 'environment': self.env,
                         'day': trigger_at.date().isoformat(), 'at': trigger_at.isoformat(),
                         'status': 'submitting', 'order_no': '', 'filled': 0, 'remaining': quantity,
                         'note': '설정 수량 주문 전송 중', 'checked_at': None,
                         'trigger_pct': leg.get('pct'), 'trigger_price': leg['price'],
                         'trigger_comparison': leg.get('comparison')}
                self.orders.append(order)
                self.persist()
                order['order_no'] = await self.broker().sell_once(p['code'], quantity, guard)
                order['status'], order['note'] = 'accepted', '설정 수량 주문 접수 · 체결 확인 중'
                p['status'], p['note'] = 'ordered', '설정 수량 접수 · 체결 확인 중 추가 주문 차단'
                self.persist()
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            p['status'], p['note'] = 'paused', str(exc)
        except OrderRejected as exc:
            if order:
                order['status'], order['note'] = 'rejected', str(exc)
            p['status'], p['note'] = 'paused', str(exc)
        except (Exception, asyncio.CancelledError):
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 확인 필요'
            p['status'], p['note'] = 'paused', '직접 설정 자동매도 중단 · 주문내역 확인 필요'
        finally:
            if not self.error:
                try:
                    self.persist()
                except Exception:
                    pass

    def pause(self, code=None, note='사용자가 직접 설정 자동매도를 껐습니다'):
        self.ticket_pause(code)
        for p in self.plans.values():
            if code is None or p['code'] == code:
                p['continue_stop'] = False
                if p['status'] == 'remainder_check':
                    p['status'], p['note'] = 'paused', note
                elif p['status'] == 'ordered':
                    p['note'] = '접수 주문은 체결 확인 중 · 이후 손절·이익보전 이어 감시는 꺼짐'
        super().pause(code, note)

    def account_changed(self):
        for p in self.plans.values():
            if p['status'] not in ('armed', 'checking'):
                continue
            actual = self.monitor.positions.get(p['code'])
            if not actual or (actual.quantity, actual.average) != (p['quantity'], p['average']):
                if p.get('mode') == 'tickets':
                    # An account event can precede our own order acknowledgement.
                    # Invalidate an unsent request; reconciliation checks both balances.
                    own_request = any(o.get('plan_id') == p['id'] and o['status'] in
                        ('submitting','accepted','partial','unknown') for o in self.orders)
                    if own_request:
                        super().pause(p['code'], '잔고 변경 · 주문 전송·잔고 확인 필요')
                    else:
                        self.pause(p['code'], '수동 매매·잔고 변경 · 조건을 다시 확인하세요')
                    continue
                # A balance event may precede our order's HTTP acknowledgement.
                # Invalidate any unsent request, but let confirmed TP fills later
                # use the originally chosen continuation policy after reconciliation.
                super().pause(p['code'], '잔고 변경 · 전송 전 주문 중단 · 체결/잔고 확인 필요')

    async def _reconcile(self):
        await super()._reconcile()
        for p in self.plans.values():
            if p.get('mode') == 'tickets':
                # A prior completed ticket must not mark a newer pending ticket completed.
                pending = [o for o in self.orders if o.get('plan_id') == p['id'] and o['status'] in PENDING]
                if pending:
                    p.update(status='ordered', note=pending[-1]['note'])
                continue
            if p['status'] == 'completed' and not p.get('take_profit_done'):
                fills = [o for o in self.orders if o.get('plan_id') == p['id'] and o['status'] == 'filled'
                         and o['reason'] == 'CUSTOM_TAKE_PROFIT']
                if fills:
                    p['take_profit_done'] = True
                    p['expected_remainder'] = p['quantity'] - fills[-1]['quantity']
                    if p.get('continue_stop') and (p['stop_loss'] or p.get('profit_protection')) and p['expected_remainder'] > 0:
                        p['status'], p['note'] = 'remainder_check', '익절 수량 체결 완료 · 남은 잔고 확인 중'
                    self.persist()
            if p['status'] == 'remainder_check':
                await self._resume_stop(p)
        await self.ticket_reconcile()

    def check(self):
        self.ticket_expire()
        super().check()

    async def _resume_stop(self, p):
        # Both the fresh REST balance and account events must agree before a
        # remaining stop/protection plan resumes. Never infer balance from acceptance.
        epoch = self.epoch
        try:
            self.gate()
            row = await self.inspect(p['code'])
            self.gate()
            if epoch != self.epoch or p['status'] != 'remainder_check':
                return
            if (row['quantity'], row['average']) != (p['expected_remainder'], p['average']):
                raise NotSent('익절 후 잔고가 예상과 다릅니다 · 남은 수량을 다시 설정하세요')
            actual = self.monitor.positions.get(p['code'])
            if not actual or (actual.quantity, actual.average) != (row['quantity'], row['average']):
                p['note'] = '익절 후 잔고 이벤트 확인 대기 · 아직 후속 매도 주문 판단하지 않음'
                self.persist()
                return
            p['quantity'], p['available'] = row['quantity'], row['available']
            if p['stop_loss']:
                p['stop_loss'] = p['stop_loss'] | {'quantity': min(p['stop_loss']['quantity'], row['quantity'])}
            if p.get('profit_protection'):
                p['profit_protection'] = p['profit_protection'] | {'quantity': row['quantity']}
            p['status'] = 'armed'
            p['note'] = (f"익절 완료 · 남은 잔고 {p['quantity']}주 전량 이익보전 감시 중"
                if p.get('profit_protection') else f"익절 완료 · 남은 잔고 {p['quantity']}주 중 {p['stop_loss']['quantity']}주 손절 감시 중")
            self.last_check = self.clock()
            self.persist()
        except Exception:
            if epoch == self.epoch and p['status'] == 'remainder_check':
                self.pause(p['code'], '익절 후 잔고/연결 확인 실패 · 남은 수량을 직접 확인하세요')

    def snapshot(self):
        at = self.clock()
        connected, market = bool(self.connected()), self.market(at)
        plans = []
        for p in self.plans.values():
            row = p.copy()
            row['conditions'] = rules_of(p)
            row['watch_state'] = p['status']
            q = self.quotes.get(p['code'])
            if p['status'] == 'armed':
                if self.error or not connected:
                    row.update(watch_state='connection_wait', note='계좌 감시 연결 확인 필요')
                elif market != '3' or (p.get('mode') == 'tickets' and (at.hour, at.minute) >= (15, 20)):
                    row.update(watch_state='session_wait', note='켜짐 · KRX 연속매매 장 상태 대기')
                elif not q or not 0 <= (at-q[1]).total_seconds() <= 10:
                    row.update(watch_state='quote_wait', note='켜짐 · 최신 KRX 체결가 대기')
                else:
                    row['watch_state'] = 'watching'
            if p['status'] == 'completed':
                row['note'] = '설정 수량 체결 완료 · 이 계획 종료 · 남은 보유 수량은 직접 관리'
            if p.get('mode')=='tickets' and p['status']=='armed' and connected and not self.error:
                for r in row['conditions']:
                    if not r['enabled']:continue
                    context=self.ticket_context(p,r,at)
                    if context and context['extended']:
                        r['watch_state']='watching'
                        row.update(watch_state='watching',note=context['venue']+' 시간외 체결가 감시 중')
                    elif any(extended_session(v,at) for v in selected_venues(r)):
                        r['watch_state']='quote_wait'
                        if row['watch_state']!='watching':row.update(watch_state='quote_wait',note='시간외 최신 체결가·거래시장 확인 대기')
            plans.append(row)
        return {'environment': self.env, 'mode': 'custom', 'connected': connected, 'market': market,
                'enabled': any(o.get('chase_active') for o in self.orders) or any((p.get('mode') == 'tickets' and any(r['enabled'] for r in p['rules']))
                               or p['status'] in ('armed','checking','remainder_check')
                               or (p['status'] == 'ordered' and p.get('continue_stop')) for p in self.plans.values()),
                'plans': plans, 'orders': self.orders[-100:], 'error': self.error, 'asof': at.isoformat(),
                'rules': {'basis':'average', 'time_exit':False, 'after_trigger':'keep_stop_after_profit'}}
