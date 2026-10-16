"""One-shot, explicitly armed limit buys with separate durable intent and fills."""
import asyncio
import secrets
from datetime import date, datetime

from app.autoexit import AutoExit, PENDING
from app.autosell import normal_code
from app.custom_exit import condition
from app.kiwoom.orders import NotSent, OrderRejected

ACTIVE = {'prepared', 'armed', 'checking', 'ordered', 'remainder_check'}


class AutoBuy(AutoExit):
    def __init__(self, *args, peers=(), custom=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.peers, self.custom = peers, custom

    def open(self):
        super().open()
        for p in self.plans.values():
            p['continue_exit'] = False
        if self.owner and not self.error:
            self.persist()

    def peers_clear(self, code):
        for peer in self.peers:
            if peer.error or not peer.owner:
                raise NotSent('자동매도 주문 기록 확인 필요')
            if peer.plans.get(code, {}).get('status') in ACTIVE or peer.blocked(code):
                raise NotSent('같은 종목의 자동매도·미확정 주문을 먼저 확인하세요')

    def validate(self, code, limit_price, quantity, max_amount, expires_on, take_profit_pct, stop_loss_pct):
        code = normal_code(code)
        if (type(limit_price) is not int or not 1 <= limit_price < 10**9
                or type(quantity) is not int or not 1 <= quantity <= 10**8
                or type(max_amount) is not int or not 1 <= max_amount <= 10**12
                or limit_price * quantity > max_amount):
            raise ValueError('가격·수량은 양의 정수이며 주문 원금이 최대 금액 이하여야 합니다')
        if not isinstance(expires_on, date) or not 0 <= (expires_on-self.clock().date()).days <= 30:
            raise ValueError('매수 대기 기한은 오늘부터 30일 이내로 선택하세요')
        tp = condition(take_profit_pct, quantity if take_profit_pct is not None else None, limit_price, quantity)
        sl = condition(stop_loss_pct, quantity if stop_loss_pct is not None else None, limit_price, quantity, True)
        return code, tp, sl

    async def inspect_buy(self, p):
        self.peers_clear(p['code'])
        # Any unresolved buy can still consume cash. No new buy until resolved.
        if any(o['status'] in PENDING for o in self.orders):
            raise NotSent('먼저 기존 매수 주문의 체결·취소 결과를 확인하세요')
        b = self.broker()
        stock = await b.stock(p['code'], p['limit_price'])
        if await b.open_orders(p['code']):
            raise NotSent('이 종목에 기존 미체결 주문이 있습니다')
        row = (await b.positions()).get(p['code'])
        baseline = {'quantity': row['quantity'], 'average': row['average']} if row else {'quantity':0, 'average':0}
        if (p.get('take_profit') or p.get('stop_loss')) and baseline['quantity']:
            raise NotSent('매수 후 자동매도 연결은 기존 보유가 없는 종목만 가능합니다 · 추가 매수는 연결 없이 설정하세요')
        if (p.get('take_profit') or p.get('stop_loss')) and p['code'] in self.custom.plans:
            raise NotSent('기존 내 조건 자동매도 계획이 있습니다 · 기존 조건을 먼저 확인하세요')
        cap = await b.capacity(p['code'], p['limit_price'])
        reserved = sum(x['limit_price']*x['quantity'] for x in self.plans.values()
                       if x['code'] != p['code'] and x['status'] in ('armed','checking'))
        if p['quantity'] > cap['quantity'] or p['limit_price']*p['quantity'] + reserved > cap['amount']:
            raise NotSent('미수 없는 주문가능 금액·수량이 부족합니다 · 다른 매수 대기 금액도 포함합니다')
        self.peers_clear(p['code'])
        return stock | {'baseline':baseline, 'cash_amount':cap['amount'], 'cash_quantity':cap['quantity']}

    async def prepare(self, code, limit_price, quantity, max_amount, expires_on,
                      take_profit_pct=None, stop_loss_pct=None):
        async with self.lock:
            self.gate()
            code, tp, sl = self.validate(code, limit_price, quantity, max_amount, expires_on,
                                        take_profit_pct, stop_loss_pct)
            if self.plans.get(code, {}).get('status') in ACTIVE or self.blocked(code):
                raise NotSent('기존 매수 계획을 중지하고 미체결 주문을 확인하세요')
            epoch = self.epoch
            p = {'id':secrets.token_urlsafe(18), 'code':code, 'limit_price':limit_price, 'quantity':quantity,
                 'max_amount':max_amount, 'expires_on':expires_on.isoformat(), 'take_profit':tp, 'stop_loss':sl,
                 'environment':self.env, 'status':'prepared', 'mode':'buy', 'continue_exit':False,
                 'prepared_at':self.clock().isoformat(), 'note':'매수 계획 확인 · 아직 주문하지 않음'}
            p.update(await self.inspect_buy(p))
            self.gate()
            if epoch != self.epoch:
                raise NotSent('연결·감시 상태가 바뀌었습니다 · 다시 확인하세요')
            self.plans[code] = p
            self.persist()
            return p.copy()

    async def arm(self, plan_id, environment, confirm_limit, confirm_exit):
        async with self.lock:
            self.gate()
            p = next((x for x in self.plans.values() if x['id'] == plan_id), None)
            if not p or p['status'] != 'prepared' or self.blocked(p['code']):
                raise NotSent('매수 계획을 다시 확인하세요')
            linked = bool(p['take_profit'] or p['stop_loss'])
            if environment != self.env or confirm_limit is not True or confirm_exit is not linked:
                raise NotSent('계좌·지정가 매수·자동매도 연결 여부를 확인하세요')
            if not 0 <= (self.clock()-datetime.fromisoformat(p['prepared_at'])).total_seconds() <= 90:
                raise NotSent('확인 시간이 지났습니다 · 계획을 다시 확인하세요')
            epoch = self.epoch
            row = await self.inspect_buy(p)
            self.gate()
            if epoch != self.epoch or p['status'] != 'prepared' or row['baseline'] != p['baseline']:
                raise NotSent('잔고·연결·조건이 바뀌었습니다 · 계획을 다시 확인하세요')
            if self.expired(p):
                raise NotSent('매수 대기 기한이 지났습니다')
            self.quotes.pop(p['code'], None)
            p.update(status='armed', note='입력한 가격 이하 매수 대기 · 1회만 주문', continue_exit=linked)
            self.last_check = self.clock()
            self.persist()

    def expired(self, p):
        at = self.clock()
        return at.date().isoformat() > p['expires_on'] or (
            at.date().isoformat() == p['expires_on'] and (at.hour,at.minute) >= (15,20))

    def reason(self, p, at):
        q = self.quotes.get(p['code'])
        if (self.market(at) == '3' and (at.hour,at.minute) < (15,20) and not self.expired(p)
                and q and 0 <= (at-q[1]).total_seconds() <= 10 and q[0] <= p['limit_price']):
            return 'LIMIT_BUY'
        return None

    def check(self):
        at = self.clock()
        gap = (at-self.last_check).total_seconds()
        self.last_check = at
        for p in self.plans.values():
            if p['status'] == 'armed' and self.expired(p):
                self.pause(p['code'], '매수 대기 기한 만료 · 자동 연장하지 않음')
        active = [p for p in self.plans.values() if p['status']=='armed']
        if not active:
            return
        if gap < 0 or gap > 15:
            self.pause(note='PC 절전·시계 변경·감시 공백 · 매수 계획을 다시 확인하세요')
            return
        if not self.connected():
            self.pause(note='실시간 연결 중단 · 자동매수를 다시 확인 후 켜세요')
            return
        for p in active:
            if self.reason(p,at):
                p.update(status='checking',note='매수가 도달 · 주문가능 금액과 잔고 확인 중')
                self.persist()
                task = asyncio.create_task(self.execute(p,'LIMIT_BUY',at,self.epoch))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    def pause(self, code=None, note='매수 대기 중지 · 접수된 주문은 별도로 잔량 취소하세요'):
        self.epoch += 1
        for p in self.plans.values():
            if code is None or p['code'] == code:
                p['continue_exit'] = False
                if p['status'] in ACTIVE:
                    p.update(status='paused', note=note)
        if self.owner and not self.error:
            self.persist()

    def account_changed(self):
        for p in self.plans.values():
            if p['status'] not in ('armed','checking') or self.blocked(p['code']):
                continue
            row = self.monitor.positions.get(p['code'])
            baseline = {'quantity':row.quantity,'average':row.average} if row else {'quantity':0,'average':0}
            if baseline != p['baseline']:
                self.pause(p['code'], '보유 잔고 변경 · 매수 계획을 다시 확인하세요')

    async def execute(self, p, reason, trigger_at, epoch):
        order = None
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    self.peers_clear(p['code'])
                    if epoch != self.epoch or p['status'] != 'checking':
                        raise NotSent('매수 대기가 중지되었거나 잔고가 변경되었습니다')
                    if (self.market(self.clock()) != '3' or self.expired(p)
                            or (self.clock().hour,self.clock().minute) >= (15,20)
                            or not 0 <= (self.clock()-trigger_at).total_seconds() <= 10):
                        raise NotSent('KRX 장 상태·매수 기한·시세 유효시간 확인 필요')
                guard()
                row = await self.inspect_buy(p)
                guard()
                if row['baseline'] != p['baseline']:
                    raise NotSent('매수 전 잔고가 변경되었습니다')
                order = {'id':secrets.token_hex(12), 'plan_id':p['id'], 'code':p['code'], 'name':p['name'],
                    'quantity':p['quantity'], 'limit_price':p['limit_price'], 'reason':'LIMIT_BUY',
                    'environment':self.env, 'day':trigger_at.date().isoformat(), 'at':trigger_at.isoformat(),
                    'status':'submitting', 'order_no':'', 'filled':0, 'remaining':p['quantity'],
                    'note':'매수 주문 전송 중', 'checked_at':None}
                self.orders.append(order)
                self.persist()
                order['order_no'] = await self.broker().buy_once(p['code'],p['quantity'],p['limit_price'],guard)
                order.update(status='accepted',note='지정가 매수 접수 · 체결 확인 중')
                p.update(status='ordered',note='매수 주문 접수 · 추가 주문 차단')
        except NotSent as exc:
            if order:
                order.update(status='not_sent',note=str(exc))
            p.update(status='paused',note=str(exc),continue_exit=False)
        except OrderRejected as exc:
            if order:
                order.update(status='rejected',note=str(exc))
            p.update(status='paused',note=str(exc),continue_exit=False)
        except (Exception,asyncio.CancelledError):
            if order:
                order.update(status='unknown',note='매수 전송/저장 결과 불명 · 재전송 차단 · 키움 확인 필요')
            p.update(status='paused',note='매수 주문 결과 확인 필요',continue_exit=False)
        finally:
            if not self.error:
                try:
                    self.persist()
                except Exception:
                    pass

    async def _reconcile(self):
        await super()._reconcile()
        for p in self.plans.values():
            related = [o for o in self.orders if o['plan_id'] == p['id']]
            if not related:
                continue
            o = related[-1]
            if o['status'] in ('filled','closed') and p.get('continue_exit') and not p.get('link_attempted'):
                if not o.get('terminal_at'):
                    o['terminal_at'] = self.clock().isoformat()
                    self.persist()
                if o['filled'] > 0:
                    p.update(status='remainder_check',note='매수 잔량 종료 확인 · 체결 잔고와 자동매도 연결 확인 중')
                    await self.link_exit(p,o)
                else:
                    p.update(status='completed',note='체결 없이 주문 종료',continue_exit=False)
            elif o['status'] == 'partial':
                p['note'] = '일부 체결 · 잔량 대기 · 자동매도 연결 전 (체결 수량도 아직 감시하지 않음)'
        if self.owner and not self.error:
            self.persist()

    async def link_exit(self,p,o):
        async with self.lock:
            async with self.custom.lock:
                epoch = self.epoch
                try:
                    self.gate()
                    if (self.clock().date().isoformat()!=o['day'] or not 0 <=
                            (self.clock()-datetime.fromisoformat(o['terminal_at'])).total_seconds() <= 60):
                        raise NotSent('매수 체결 후 잔고 확인 제한시간 경과')
                    self.peers_clear(p['code'])
                    rows = await self.broker().positions()
                    pending = await self.broker().open_orders(p['code'])
                    self.gate()
                    if epoch != self.epoch or not p['continue_exit']:
                        return
                    row = rows.get(p['code'])
                    actual = self.monitor.positions.get(p['code'])
                    if (pending or not row or row['quantity'] != o['filled'] or row['available'] != o['filled']
                            or not 0 < row['average'] <= p['limit_price']
                            or not actual or (actual.quantity,actual.average) != (row['quantity'],row['average'])):
                        p['note'] = '체결 후 REST·실시간 잔고 일치 대기 · 아직 자동매도 감시 전'
                        return
                    self.peers_clear(p['code'])
                    if p['code'] in self.custom.plans:
                        raise NotSent('기존 자동매도 설정이 있어 자동 연결하지 않았습니다')
                    # Journal the handoff BEFORE arming the exit. After a crash,
                    # neither this intent nor either service is automatically rearmed.
                    p['link_attempted'] = True
                    self.persist()
                    tp = condition(p['take_profit']['pct'],row['quantity'],row['average'],row['quantity']) if p['take_profit'] else None
                    sl = condition(p['stop_loss']['pct'],row['quantity'],row['average'],row['quantity'],True) if p['stop_loss'] else None
                    linked = row | {'id':secrets.token_urlsafe(18),'prepared_at':self.clock().isoformat(),
                        'status':'armed','note':'자동매수 체결 잔고 · 선택한 익절·손절 감시 중',
                        'environment':self.env,'mode':'custom','take_profit':tp,'stop_loss':sl,
                        'after_trigger':'keep_stop_after_profit','take_profit_done':False,
                        'original_quantity':row['quantity'],'continue_stop':True,'source_buy_id':p['id']}
                    self.custom.gate()
                    self.custom.plans[p['code']] = linked
                    self.custom.quotes.pop(p['code'],None)
                    self.custom.last_check = self.clock()
                    self.custom.persist()
                    p.update(status='completed',note='매수 체결 확인 · 내 조건 자동매도 연결 완료',
                             linked_exit_id=linked['id'],continue_exit=False)
                    self.persist()
                except Exception:
                    self.pause(p['code'],'매수 후 자동매도 연결 실패 · 체결 잔고를 직접 확인하세요')

    async def cancel(self, order_id):
        async with self.reconcile_lock:
            async with self.lock:
                self.gate()
                o = next((o for o in self.orders if o['id'] == order_id),None)
                if not o or not o['order_no'] or o['status'] not in PENDING or o.get('cancel'):
                    raise NotSent('취소할 원 매수 주문을 확인하세요 · 중복 취소하지 않습니다')
                d = await self.broker().detail(o)
                if (not d or not o['filled'] <= d['filled'] <= o['quantity']
                        or not 0 < d['remaining'] <= o['quantity']-d['filled']):
                    raise NotSent('미체결 잔량을 확인할 수 없습니다 · 체결 다시 조회 후 확인하세요')
                if o['day'] != self.clock().date().isoformat():
                    raise NotSent('오늘 주문만 취소할 수 있습니다 · 키움에서 이전 주문을 확인하세요')
                self.pause(o['code'],'잔량 취소 요청 준비 · 자동매도 연결 해제 · 체결 수량은 직접 확인하세요')
                at = self.clock()
                def guard():
                    self.gate()
                    if not 0 <= (self.clock()-at).total_seconds() <= 10:
                        raise NotSent('취소 준비 지연 · 키움 주문내역 확인 필요')
                o['cancel'] = {'status':'submitting','at':at.isoformat(),'order_no':''}
                self.persist()
                try:
                    number = await self.broker().cancel_once(o,guard)
                    o['cancel'].update(status='accepted',order_no=number)
                    o['note'] = '잔량 취소 접수 · 원 주문의 종료 확인 전까지 미확정'
                except NotSent as exc:
                    o['cancel']['status'] = 'not_sent'
                    o['note'] = str(exc)
                except OrderRejected as exc:
                    o['cancel']['status'] = 'rejected'
                    o['note'] = str(exc)
                except (Exception,asyncio.CancelledError):
                    o['cancel']['status'] = 'unknown'
                    o['note'] = '취소 결과 불명 · 재전송하지 않음 · 키움 확인 필요'
                self.persist()

    def snapshot(self):
        at = self.clock()
        plans = []
        for p in self.plans.values():
            row = p.copy()
            row['watch_state'] = p['status']
            if p['status'] == 'armed':
                q = self.quotes.get(p['code'])
                row['watch_state'] = ('connection_wait' if not self.connected() else
                    'session_wait' if self.market(at) != '3' else
                    'quote_wait' if not q or not 0 <= (at-q[1]).total_seconds() <= 10 else 'watching')
            plans.append(row)
        return {'environment':self.env,'mode':'buy','connected':bool(self.connected()),'market':self.market(at),
                'enabled':any(p['status'] in ('armed','checking','remainder_check') or
                    (p['status']=='ordered' and p.get('continue_exit')) for p in self.plans.values()),
                'plans':plans,'orders':self.orders[-100:],'error':self.error,'asof':at.isoformat()}
