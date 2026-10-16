"""Individually reviewed conditional exits, serialized per holding."""
import asyncio
import copy
import hashlib
import json
import secrets
from datetime import date, datetime
from decimal import Decimal

from app.autosell import normal_code
from app.kiwoom.orders import NotSent, OrderRejected


def fingerprint(p):
    # Display-only note changes must not invalidate a reviewed order.
    material = {k:v for k,v in p.items() if k != 'note'} if p else p
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def rules_of(p):
    if p.get('mode') == 'tickets':
        return copy.deepcopy(p['rules'])
    rules = []
    for key, kind, comparison in [('profit_protection','protection','lt'),('stop_loss','loss','lte'),('take_profit','profit','gte')]:
        leg = p.get(key)
        if not leg: continue
        rules.append({'id':'legacy-'+key, 'kind':kind, 'price':str(leg['price']),
            'comparison':comparison, 'quantity':leg['quantity'], 'all_remaining':bool(leg.get('all_remaining')),
            'order_type':'market', 'limit_price':None, 'expires_on':None,
            'enabled':p['status'] in ('armed','checking') and not (key=='take_profit' and p.get('take_profit_done')),
            'state':'done' if key=='take_profit' and p.get('take_profit_done') else 'paused'})
    return rules


class TicketMixin:
    def persist(self):
        try:
            return super().persist()
        except Exception:
            self.ticket_pause()
            raise

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticket_reviews = {}

    def open(self):
        super().open()
        self.ticket_reviews.clear()
        for p in self.plans.values():
            if p.get('mode') == 'tickets':
                for r in p['rules']:
                    r['enabled'] = False
        if self.owner and not self.error: self.persist()

    def ticket_expired(self, r):
        at = self.clock()
        return bool(r.get('expires_on') and (at.date().isoformat() > r['expires_on'] or
            (at.date().isoformat() == r['expires_on'] and (at.hour, at.minute) >= (15,20))))

    def ticket_refresh_state(self, p):
        if self.error or not self.owner or self.blocked(p['code']) or p['status'] not in ('armed','paused'):
            return
        previous = p['status']
        p['status'] = 'armed' if any(r['enabled'] for r in p['rules']) else 'paused'
        if p['status'] != previous or not p.get('note'):
            # Keep the reason for a restart/disconnect/balance-change pause.
            p['note'] = '조건 감시 중 · 한 번에 한 주문' if p['status']=='armed' else '조건 감시 꺼짐'

    def ticket_expire(self):
        changed = False
        for p in self.plans.values():
            if p.get('mode') != 'tickets': continue
            for r in p['rules']:
                if r['enabled'] and self.ticket_expired(r):
                    r.update(enabled=False, state='expired'); changed=True
            self.ticket_refresh_state(p)
        if changed: self.persist()

    async def prepare_ticket(self, code, kind, price, comparison, quantity, all_remaining,
                             order_type, limit_price, expires_on, rule_id=None,
                             after_hours='off', retry_unfilled=False, empty_book='wait'):
        async with self.lock:
            self.gate(); code=normal_code(code); epoch=self.epoch
            old=self.plans.get(code)
            if self.blocked(code) or (old and old['status'] in ('checking','ordered','remainder_check')):
                raise NotSent('이 종목의 주문·잔고 확인이 끝난 뒤 조건을 설정하세요')
            if old and old.get('mode')!='tickets' and old['status'] in ('armed','checking'):
                raise NotSent('기존 묶음 조건 감시를 먼저 끈 뒤 개별 조건으로 설정하세요')
            before=fingerprint(old)
            row=await self.inspect(code)
            self.gate()
            if epoch!=self.epoch or fingerprint(self.plans.get(code))!=before:
                raise NotSent('잔고·조건 상태가 바뀌었습니다 · 다시 확인하세요')
            value=Decimal(str(price))
            if not value.is_finite() or not 0<value<10**9 or kind not in ('profit','protection','loss'):
                raise ValueError('조건 종류와 기준 가격을 확인하세요')
            if comparison not in ('gte','lte','lt') or (kind=='profit') != (comparison=='gte'):
                raise ValueError('익절은 이상, 이익보전·손절은 이하 또는 미만으로 설정하세요')
            if type(all_remaining) is not bool or type(quantity) is not int or not 1<=quantity<=row['quantity']:
                raise ValueError('수량은 보유 수량 이하의 양의 정수여야 합니다')
            if order_type not in ('market','limit'):
                raise ValueError('시장가 또는 지정가를 선택하세요')
            if after_hours not in ('off','KRX','NXT','both') or type(retry_unfilled) is not bool or empty_book not in ('wait','floor'):
                raise ValueError('시간외 시장·재주문 설정을 확인하세요')
            if retry_unfilled and (kind=='profit' or after_hours=='off'):
                raise ValueError('재주문은 시간외 손절·이익보전 조건만 가능합니다')
            if empty_book=='floor' and (after_hours=='off' or order_type!='market'):
                raise ValueError('하한가 전환은 시간외 시장가 변환에서만 선택할 수 있습니다')
            venues=[]
            if after_hours!='off':
                if self.env!='real': raise NotSent('시간외 자동매도는 실계좌에서만 지원합니다')
                venues=(await self.broker().extended_metadata(code))['venues']
                if after_hours!='both' and after_hours not in venues: raise NotSent('이 종목은 선택한 시간외 시장에서 거래할 수 없습니다')
            if order_type=='limit':
                if type(limit_price) is not int or not 1<=limit_price<10**9:
                    raise ValueError('지정가 주문 가격을 입력하세요')
                await self.broker().validate_sell_limit(code, limit_price)
            elif limit_price is not None:
                raise ValueError('시장가에는 지정가 가격을 넣지 마세요')
            if expires_on is not None and (type(expires_on) is not date or not 0<=(expires_on-self.clock().date()).days<=30):
                raise ValueError('기한은 오늘부터 30일 이내로 선택하세요')
            rules=rules_of(old) if old else []
            prior=next((r for r in rules if r['id']==rule_id),None) if rule_id else None
            if rule_id and (not prior or prior['enabled'] or prior['state'] in ('pending','checking','done','closed')):
                raise NotSent('꺼진 조건만 다시 확인해 켤 수 있습니다 · 완료 조건은 새로 추가하세요')
            r={'id':rule_id or secrets.token_hex(10), 'kind':kind,'price':str(value),'comparison':comparison,
               'quantity':row['quantity'] if all_remaining else quantity,'all_remaining':all_remaining,
               'order_type':order_type,'limit_price':limit_price,'expires_on':expires_on.isoformat() if expires_on else None,
               'enabled':False,'state':'paused'}
            r.update(after_hours=after_hours,retry_unfilled=retry_unfilled,empty_book=empty_book,after_hours_venues=venues)
            if self.ticket_expired(r): raise NotSent('설정한 감시 기한이 지났습니다')
            if len(rules)>=30 and not prior: raise NotSent('종목당 조건은 최대 30개입니다 · 꺼진 조건을 삭제하세요')
            self.gate()
            if epoch!=self.epoch or fingerprint(self.plans.get(code))!=before:
                raise NotSent('확인 중 다른 조건이 바뀌었습니다 · 다시 확인하세요')
            review=row | {'review_id':secrets.token_urlsafe(18),'environment':self.env,'rule':r,
                          'prepared_at':self.clock().isoformat(),'fingerprint':before,'epoch':epoch}
            self.ticket_reviews={k:v for k,v in self.ticket_reviews.items()
                if v['code']!=code and (self.clock()-datetime.fromisoformat(v['prepared_at'])).total_seconds()<90}
            self.ticket_reviews[review['review_id']]=review
            return {k:v for k,v in review.items() if k not in ('fingerprint','epoch')}

    async def arm_ticket(self, review_id, environment, confirm_order):
        async with self.lock:
            self.gate(); draft=self.ticket_reviews.get(review_id)
            if not draft or environment!=self.env or confirm_order is not True:
                raise NotSent('확인할 조건과 계좌를 다시 선택하세요')
            code=draft['code']; old=self.plans.get(code)
            def guard():
                self.gate()
                if (draft['epoch']!=self.epoch or fingerprint(self.plans.get(code))!=draft['fingerprint'] or
                    not 0<=(self.clock()-datetime.fromisoformat(draft['prepared_at'])).total_seconds()<=90 or
                    self.ticket_expired(draft['rule']) or self.blocked(code)):
                    raise NotSent('조건·잔고가 바뀌거나 확인 시간이 지났습니다 · 다시 확인하세요')
            guard(); row=await self.inspect(code); guard()
            if (row['quantity'],row['average']) != (draft['quantity'],draft['average']):
                raise NotSent('잔고가 바뀌었습니다 · 새 수량으로 다시 확인하세요')
            r=copy.deepcopy(draft['rule'])
            if r.get('after_hours','off')!='off':
                venues=(await self.broker().extended_metadata(code))['venues'];guard()
                if venues!=r['after_hours_venues']:raise NotSent('시간외 거래 가능 시장이 바뀌었습니다 · 다시 확인하세요')
            if r['order_type']=='limit': await self.broker().validate_sell_limit(code,r['limit_price']); guard()
            rules=rules_of(old) if old else []
            rules=[x for x in rules if x['id']!=r['id']]
            r.update(enabled=True,state='watching');rules.append(r)
            p=(copy.deepcopy(old) if old else {}) | row | {'id':old['id'] if old and old.get('mode')=='tickets' else secrets.token_urlsafe(18),
                'mode':'tickets','environment':self.env,'status':'armed','rules':rules,'note':'개별 조건 감시 중',
                'prepared_at':draft['prepared_at'],'continue_stop':False,
                'take_profit':None,'stop_loss':None,'profit_protection':None}
            self.plans[code]=p
            self.quotes.pop(code,None);self.last_check=self.clock();self.persist()
            if hasattr(self,'extended_quotes'):
                for venue in ('KRX','NXT'):self.extended_quotes.pop((code,venue),None)
            del self.ticket_reviews[review_id]

    def pause_rule(self, code, plan_id, rule_id):
        p=self.plans.get(normal_code(code))
        if not p or p['id']!=plan_id: raise NotSent('조건이 바뀌었습니다 · 다시 확인하세요')
        if p.get('mode')!='tickets':
            # Legacy plans are a single reviewed bundle; preserve their existing stop semantics.
            self.pause(code); return
        r=next((r for r in p['rules'] if r['id']==rule_id),None)
        if not r: raise NotSent('조건을 찾을 수 없습니다')
        r['enabled']=False
        if r['state'] not in ('pending','done','closed'): r['state']='paused'
        self.ticket_refresh_state(p); self.persist()

    def remove_rule(self, code, plan_id, rule_id):
        p=self.plans.get(normal_code(code))
        if not self.owner or self.error or not p or p['id']!=plan_id or self.blocked(code):
            raise NotSent('주문·저장 상태를 확인한 뒤 삭제하세요')
        if p.get('mode')!='tickets':
            if p['status'] not in ('paused','prepared','completed'):
                raise NotSent('기존 조건을 먼저 끄세요')
            legacy_rules = rules_of(p)
            if not any(x['id'] == rule_id for x in legacy_rules):
                raise NotSent('조건을 찾을 수 없습니다')
            for x in legacy_rules: x['enabled'] = False
            p.update(mode='tickets',rules=legacy_rules,status='paused',take_profit=None,
                     stop_loss=None,profit_protection=None,continue_stop=False)
        r=next((r for r in p['rules'] if r['id']==rule_id),None)
        if not r or r['enabled'] or r['state'] in ('pending','checking'):
            raise NotSent('꺼진 조건만 삭제할 수 있습니다')
        p['rules'].remove(r);self.persist()

    def ticket_pause(self, code=None):
        for p in self.plans.values():
            if p.get('mode')=='tickets' and (code is None or p['code']==code):
                for r in p['rules']:
                    r['enabled']=False
                    if r['state'] not in ('pending','done','closed','expired'):r['state']='paused'

    def ticket_reason(self, p, at):
        if p.get('after_hours_retry_at') and (at-datetime.fromisoformat(p['after_hours_retry_at'])).total_seconds()<12:
            return None
        for r in sorted(p['rules'],key=lambda r:{'protection':0,'loss':1,'profit':2}[r['kind']]):
            context=self.ticket_context(p,r,at)
            if not context:continue
            price=Decimal(str(context['price']))
            threshold=Decimal(r['price'])
            if r['enabled'] and not self.ticket_expired(r) and {'gte':price>=threshold,'lte':price<=threshold,'lt':price<threshold}[r['comparison']]:
                return 'TICKET:'+r['id']
        return None

    async def ticket_execute(self,p,reason,trigger_at,epoch):
        order=None;r=next((r for r in p['rules'] if 'TICKET:'+r['id']==reason),None)
        context=self.ticket_context(p,r,trigger_at) if r else None
        book=None
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    if (epoch!=self.epoch or p['status']!='checking' or not r or not r['enabled'] or self.ticket_expired(r)
                        or not context or not self.ticket_context(p,r,self.clock(),context['venue'] if context['extended'] else None)
                        or not 0<=(self.clock()-trigger_at).total_seconds()<=10):
                        raise NotSent('조건 중지·기한·시세·연결 변경 · 주문하지 않았습니다')
                    if book and not 0<=(self.clock()-datetime.fromisoformat(book['at'])).total_seconds()<=10:
                        raise NotSent('주문 준비 중 호가 지연')
                guard();row=await self.inspect(p['code']);guard()
                if (row['quantity'],row['average'])!=(p['quantity'],p['average']):
                    raise NotSent('잔고 변경 · 모든 조건을 다시 확인하세요')
                qty=row['quantity'] if r['all_remaining'] else min(r['quantity'],row['quantity'])
                if type(qty) is not int or not 1 <= qty <= row['available']:
                    raise NotSent('주문 가능한 매도 수량을 확인하세요')
                if r['order_type']=='limit':await self.broker().validate_sell_limit(p['code'],r['limit_price']);guard()
                actual_type,actual_price=r['order_type'],r['limit_price']
                if context['extended']:
                    book=await self.broker().extended_book(p['code'],context['venue']);guard()
                    actual_type,actual_price='limit',self.book_price(r,book)
                order={'id':secrets.token_hex(12),'plan_id':p['id'],'rule_id':r['id'],'code':p['code'],'name':p['name'],
                    'quantity':qty,'position_quantity':row['quantity'],'reason':'TICKET_'+r['kind'].upper(),
                    'environment':self.env,'day':trigger_at.date().isoformat(),'at':trigger_at.isoformat(),
                    'status':'submitting','order_no':'','filled':0,'remaining':qty,'checked_at':None,
                    'note':'개별 조건 주문 전송 중','trigger_price':r['price'],'trigger_comparison':r['comparison'],
                    'order_type':actual_type,'limit_price':actual_price}
                if context['extended']:
                    order.update(after_hours=True,venue=context['venue'],chase_active=r.get('retry_unfilled',False),
                        retry_attempt=0,replace_state='watching')
                self.orders.append(order);r['state']='checking';self.persist()
                if context['extended']:number=await self.broker().extended_sell_once(p['code'],qty,actual_price,context['venue'],guard)
                elif r['order_type']=='limit':number=await self.broker().sell_limit_once(p['code'],qty,r['limit_price'],guard)
                else:number=await self.broker().sell_once(p['code'],qty,guard)
                order.update(order_no=number,status='accepted',note='접수 · 체결 확인 중')
                r.update(enabled=False,state='pending');p.update(status='ordered',note='체결 확인 중 · 다른 조건 주문 대기');self.persist()
        except NotSent as exc:
            from app.kiwoom.extended_sell import QuoteWait
            if isinstance(exc,QuoteWait) and order is None and epoch==self.epoch and r and r['enabled']:
                p.update(status='armed',note=str(exc),after_hours_retry_at=self.clock().isoformat())
                return
            if order:order.update(status='not_sent',note=str(exc))
            if r and not r['enabled'] and epoch==self.epoch:
                r['state']='paused';p.update(status='paused',note=str(exc));self.ticket_refresh_state(p)
            else:
                self.ticket_pause(p['code']);p.update(status='paused',note=str(exc))
        except OrderRejected as exc:
            if order:order.update(status='rejected',note=str(exc))
            self.ticket_pause(p['code']);p.update(status='paused',note='주문 거부 · 조건을 다시 확인하세요')
        except (Exception,asyncio.CancelledError):
            if order:order.update(status='unknown',note='주문 결과 불명 · 자동 재전송 없음')
            self.ticket_pause(p['code']);p.update(status='paused',note='주문·저장 확인 필요')
        finally:
            if not self.error:
                try:self.persist()
                except Exception:pass

    async def ticket_reconcile(self):
        for p in self.plans.values():
            if p.get('mode')!='tickets':continue
            for order in self.orders:
                if order.get('plan_id')!=p['id'] or not order.get('rule_id') or order.get('balance_done') or order['status'] not in ('filled','closed'):
                    continue
                if order.get('replace_state') in ('cancelling','cancel_wait','replaced'):continue
                r=next((r for r in p['rules'] if r['id']==order['rule_id']),None)
                if r:r.update(enabled=False,state='done' if order['status']=='filled' else 'closed')
                expected=order['position_quantity']-order['filled']
                if expected==0:
                    self.ticket_pause(p['code']);p.update(quantity=0,available=0,status='completed',note='보유 수량 매도 완료')
                    order['balance_done']=True;self.persist();continue
                if not any(x['enabled'] for x in p['rules']):
                    p.update(status='paused',note='주문 종료 · 남은 조건 감시 꺼짐');order['balance_done']=True;self.persist();continue
                p.update(status='remainder_check',note='남은 잔고 확인 중 · 다음 주문 대기');epoch=self.epoch
                try:
                    self.gate();row=await self.inspect(p['code']);self.gate()
                    if epoch!=self.epoch or not any(x['enabled'] for x in p['rules']):continue
                    if (row['quantity'],row['average'])!=(expected,p['average']):raise NotSent('익절 후 잔고 불일치')
                    actual=self.monitor.positions.get(p['code'])
                    if not actual or (actual.quantity,actual.average)!=(expected,p['average']):self.persist();continue
                    p.update(quantity=expected,available=row['available'],status='paused')
                    for x in p['rules']:
                        if x['all_remaining']:x['quantity']=expected
                        if self.ticket_expired(x):x.update(enabled=False,state='expired')
                    self.ticket_refresh_state(p);self.quotes.pop(p['code'],None);self.last_check=self.clock()
                    order['balance_done']=True;self.persist()
                except Exception:
                    self.ticket_pause(p['code']);p.update(status='paused',note='체결 후 잔고·연결 확인 실패 · 다시 확인하세요');self.persist()
