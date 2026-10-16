"""Opt-in extended-session tickets and bounded, confirmed cancel/replacement."""
from app.manual_exit_cancel import ManualCancelMixin
import asyncio
import copy
import math
import secrets
from datetime import date, datetime

from app.kiwoom.orders import NotSent, OrderRejected
from app.kiwoom.extended_sell import lower_tick, tick, QuoteWait

MAX_REPRICES = 5
POLL_SECONDS = 12


def extended_session(venue, at):
    if at.weekday() >= 5: return False
    tm = (at.hour, at.minute)
    if venue == 'NXT': return (8,0) <= tm < (8,50) or (15,40) <= tm < (20,0)
    return venue == 'KRX' and at.date() >= date(2026,9,14) and (16,0) <= tm < (20,0)


def selected_venues(rule):
    mode=rule.get('after_hours','off')
    return [v for v in (['NXT','KRX'] if mode=='both' else [mode])
            if v in rule.get('after_hours_venues',[])]


class AfterHoursMixin(ManualCancelMixin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extended_quotes = {}
        self.reprice_checked = self.clock()

    def open(self):
        super().open()
        self.extended_quotes.clear()
        for o in self.orders:
            o['chase_active'] = False
            if o.get('replace_state') in ('cancelling','cancel_wait'):
                o['replace_state'] = 'stopped'
                o['note'] = '재시작 · 취소 결과 확인 필요 · 재주문 꺼짐'
        if self.owner and not self.error: self.persist()

    def quote(self, code, price, at, market):
        venue = {'KRX':'KRX','NX':'NXT'}.get(market)
        now = self.clock()
        if not venue or not math.isfinite(price) or price <= 0 or not 0 <= (now-at).total_seconds() <= 10:
            return
        old = self.extended_quotes.get((code,venue))
        if old and at < old[1]: return
        self.extended_quotes[(code,venue)] = (price,at)
        if market == 'KRX': super().quote(code,price,at,market)
        else: self.check()

    def ticket_expired(self, r):
        at = self.clock()
        cutoff = (20,0) if r.get('after_hours','off') != 'off' else (15,20)
        return bool(r.get('expires_on') and (at.date().isoformat() > r['expires_on'] or
            (at.date().isoformat() == r['expires_on'] and (at.hour,at.minute) >= cutoff)))

    def blocked(self, code):
        return super().blocked(code) or any(o['code']==code and o.get('chase_active') for o in self.orders)

    def ticket_context(self, p, r, at, venue=None):
        if at.weekday() >= 5: return None
        if venue is None and self.market(at) == '3' and (9,0) <= (at.hour,at.minute) < (15,20):
            q = self.quotes.get(p['code'])
            if q and 0 <= (at-q[1]).total_seconds() <= 10:
                return {'venue':'KRX','price':q[0],'at':q[1],'extended':False}
        for market in selected_venues(r):
            if (venue and market != venue) or not extended_session(market,at): continue
            q = self.extended_quotes.get((p['code'],market))
            if q and 0 <= (at-q[1]).total_seconds() <= 10:
                return {'venue':market,'price':q[0],'at':q[1],'extended':True}
        return None

    def stop_reprices(self, code=None, rule_id=None):
        for o in self.orders:
            if (code is None or o['code']==code) and (rule_id is None or o.get('rule_id')==rule_id):
                if o.get('chase_active'):
                    o['chase_active'] = False
                    o['note'] = '자동 재주문 중지 · 접수 주문·취소 결과는 별도 확인'
                if o.get('replace_state') in ('cancelling','cancel_wait'):
                    o['replace_state'] = 'stopped'

    def ticket_pause(self, code=None):
        self.stop_reprices(code)
        super().ticket_pause(code)

    def pause_rule(self, code, plan_id, rule_id):
        p = self.plans.get(code)
        if not p or p['id'] != plan_id: raise NotSent('조건이 바뀌었습니다 · 다시 확인하세요')
        self.stop_reprices(code,rule_id)
        super().pause_rule(code,plan_id,rule_id)

    def check(self):
        # The original watchdog considered armed plans only; pending reprices
        # also stop on sleep/disconnect and never silently resume.
        if any(o.get('chase_active') for o in self.orders):
            gap = (self.clock()-self.last_check).total_seconds()
            if not self.connected() or gap < 0 or gap > 15:
                self.pause(note='연결·감시 공백 · 시간외 재주문 중지')
        super().check()

    def book_price(self, r, book):
        value = r['limit_price'] if r['order_type']=='limit' else book['lowest']
        if value is None and r.get('empty_book')=='floor': value=book['floor']
        if value is None: raise QuoteWait('매수호가 없음 · 시간외 주문 대기')
        if not book['floor'] <= value <= book['ceiling'] or value % tick(value):
            raise NotSent('시간외 지정가가 가격제한폭·호가단위에 맞지 않습니다')
        return value

    async def reprice(self):
        """Called by the worker only, never by the read-only reconcile route."""
        now = self.clock()
        if (now-self.reprice_checked).total_seconds() < POLL_SECONDS: return
        self.reprice_checked = now
        async with self.reconcile_lock:
            async with self.lock:
                for o in list(self.orders):
                    if not o.get('chase_active'): continue
                    try:
                        await self._reprice_order(o)
                    except QuoteWait as exc:
                        # A transient stale/empty book is not permission to guess a price.
                        o['note']=str(exc)
                        self.persist()
                    except (Exception, asyncio.CancelledError) as exc:
                        o['chase_active']=False
                        if o.get('replace_state') in ('cancelling','cancel_wait'): o['replace_state']='stopped'
                        o['note'] = str(exc) if isinstance(exc,NotSent) else '재주문 결과·연결 확인 필요 · 추가 전송 중지'
                        self.ticket_pause(o['code'])
                        if not self.error: self.persist()
                        if isinstance(exc,asyncio.CancelledError): raise

    async def _reprice_order(self, o):
        p=self.plans.get(o['code'])
        r=next((r for r in p.get('rules',[]) if r['id']==o.get('rule_id')),None) if p else None
        epoch=self.epoch
        def guard():
            self.gate()
            if (epoch!=self.epoch or not o.get('chase_active') or not p or p['id']!=o['plan_id'] or not r
                    or self.ticket_expired(r) or o['day']!=self.clock().date().isoformat()
                    or not extended_session(o['venue'],self.clock())):
                raise NotSent('중지·기한·거래시간 변경 · 재주문하지 않았습니다')
        guard()
        if o['status'] in ('filled','rejected','not_sent','unknown','submitting'):
            o['chase_active']=False;self.persist();return
        b=self.broker()
        d=await b.detail(o);guard()
        if d is None: raise NotSent('원주문 조회 없음 · 재주문 중지')
        filled,remaining=d['filled'],d['remaining']
        if not o['filled'] <= filled <= o['quantity'] or not 0 <= remaining <= o['quantity']-filled:
            raise NotSent('원주문 체결·잔량 불일치 · 재주문 중지')
        o.update(filled=filled,remaining=remaining,checked_at=self.clock().isoformat())
        if filled==o['quantity']:
            o.update(status='filled',chase_active=False,replace_state='finished',note='전량 체결 확인');self.persist();return
        if d['rejected']: raise NotSent('원주문 거부 · 재주문 중지')
        if o.get('replace_state')=='cancel_wait':
            if (self.clock()-datetime.fromisoformat(o['cancel_at'])).total_seconds()>60:
                raise NotSent('취소·잔고·시세 확인 지연 · 자동 재주문 중지')
            cancel=await b.extended_cancel_detail(o);guard()
            if not cancel or cancel['confirmed']==0 or remaining:
                o['note']='취소 접수 · 증권사 취소 완료 확인 대기';self.persist();return
            if cancel['confirmed'] != o['quantity']-filled:
                raise NotSent('취소 확인 수량과 최종 체결수량 불일치')
            o.update(status='closed')
            # Keep this chain reserved until every source agrees that the old
            # order ended and the remaining holding is available again.
            row=(await b.positions()).get(o['code']);guard()
            expected=o['position_quantity']-filled
            actual=self.monitor.positions.get(o['code'])
            if not row or (row['quantity'],row['average'])!=(expected,p['average']):
                raise NotSent('취소 후 보유 잔고 변경 · 재주문 중지')
            if (not actual or (actual.quantity,actual.average)!=(expected,p['average'])
                    or row['available']!=expected):
                o['note']='취소 확인 · 주문가능 잔고 동기화 대기';self.persist();return
            if await b.open_orders(o['code']): raise NotSent('다른 미체결 주문 확인 · 재주문 중지')
            guard()
            if not self.ticket_context(p,r,self.clock(),o['venue']):
                o['note']='취소 확인 · 최신 시간외 체결가 대기';self.persist();return
            book=await b.extended_book(o['code'],o['venue']);guard()
            if not book['best']:
                o['note']='취소 확인 · 매수호가 대기';self.persist();return
            price=max(book['floor'],lower_tick(book['best']))
            # Never replace with a higher price following a rebound.
            price=min(price,o['limit_price'])
            attempts=sum(1 for x in self.orders if x['code']==o['code'] and x['day']==o['day'] and x.get('retry_parent'))
            if attempts>=MAX_REPRICES: raise NotSent('종목당 오늘 재주문 5회 도달 · 추가 재주문 중지')
            qty=o['quantity']-filled
            if not 0<qty<=row['available']: raise NotSent('재주문 수량 확인 필요')
            def send_guard():
                guard()
                if (not self.ticket_context(p,r,self.clock(),o['venue']) or
                        not 0 <= (self.clock()-datetime.fromisoformat(book['at'])).total_seconds() <= 10):
                    raise NotSent('재주문 준비 중 시세·호가 지연')
            child=copy.deepcopy(o)
            for key in ('cancel_no','cancel_at','balance_done'): child.pop(key,None)
            child.update(id=secrets.token_hex(12),retry_parent=o['id'],retry_attempt=attempts+1,
                position_quantity=expected,quantity=qty,filled=0,remaining=qty,order_no='',status='submitting',
                limit_price=price,at=self.clock().isoformat(),checked_at=None,replace_state='watching',
                note=f'시간외 재주문 {attempts+1}/5 전송 중',chase_active=True)
            self.orders.append(child);o.update(balance_done=True,replace_state='replaced')
            p.update(status='ordered');self.persist()
            try:
                child['order_no']=await b.extended_sell_once(o['code'],qty,price,o['venue'],send_guard)
                child.update(status='accepted',note=f'시간외 재주문 {attempts+1}/5 접수 · 체결 확인 중')
            except NotSent as exc:
                child.update(status='not_sent',chase_active=False,note=str(exc));raise
            except OrderRejected as exc:
                child.update(status='rejected',chase_active=False,note=str(exc));raise
            except (Exception,asyncio.CancelledError):
                child.update(status='unknown',chase_active=False,note='재주문 응답 불명 · 재전송 없음');raise
            finally:
                o['chase_active']=False
                if not self.error:self.persist()
            return
        if remaining==0:
            o.update(status='closed',chase_active=False,replace_state='finished',note='원주문 종료 · 임의 취소 주문은 재주문하지 않음');self.persist();return
        attempts=sum(1 for x in self.orders if x['code']==o['code'] and x['day']==o['day'] and x.get('retry_parent'))
        if attempts>=MAX_REPRICES:
            o.update(chase_active=False,note='종목당 오늘 재주문 5회 도달 · 마지막 지정가 잔량은 유지');self.persist();return
        if not self.ticket_context(p,r,self.clock(),o['venue']): return
        book=await b.extended_book(o['code'],o['venue']);guard()
        if not book['best'] or book['best']>=o['limit_price']: return
        opened=await b.open_orders(o['code']);guard()
        if (len(opened)!=1 or str(opened[0]['ord_no']).lstrip('0')!=o['order_no'].lstrip('0')
                or not 0<int(opened[0]['oso_qty'])<=remaining):
            raise NotSent('미체결 원주문·잔량 불일치 · 취소·재주문 중지')
        def cancel_guard():
            guard()
            if (not self.ticket_context(p,r,self.clock(),o['venue']) or
                    not 0 <= (self.clock()-datetime.fromisoformat(book['at'])).total_seconds() <= 10):
                raise NotSent('취소 준비 중 시세·호가 지연')
        cancel_guard()
        o.update(replace_state='cancelling',cancel_at=self.clock().isoformat(),note='가격 하락 · 원주문 잔량 취소 전송 중')
        self.persist()
        try:
            o['cancel_no']=await b.extended_cancel_once(o,cancel_guard)
        except (Exception,asyncio.CancelledError):
            o.update(chase_active=False,replace_state='stopped',note='취소 응답 확인 필요 · 재주문하지 않음')
            if not self.error:self.persist()
            raise
        o.update(replace_state='cancel_wait',note='취소 접수 · 완료 확인 전 재주문 금지');self.persist()
