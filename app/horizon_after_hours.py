"""Opt-in Horizon extended exits using the existing checked venue adapter/repricer."""
import asyncio
import secrets
from datetime import datetime

from app.autoexit import AutoExit, PENDING
from app.autosell import normal_code
from app.after_hours_exit import AfterHoursMixin, extended_session, selected_venues
from app.kiwoom.orders import NotSent, OrderRejected
from app.kiwoom.extended_sell import QuoteWait


class HorizonExit(AfterHoursMixin, AutoExit):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.horizon_triggers={}

    async def prepare(self,code,bought_on,after_hours='off',retry_unfilled=False,empty_book='wait'):
        if after_hours not in ('off','KRX','NXT','both') or type(retry_unfilled) is not bool or empty_book not in ('wait','floor'):
            raise NotSent('시간외 매도 설정 확인 필요')
        if after_hours=='off' and (retry_unfilled or empty_book!='wait'):
            raise NotSent('시간외 시장을 선택한 뒤 추가 옵션을 켜세요')
        code=normal_code(code)
        venues=[]
        if after_hours!='off':
            self.gate();epoch=self.epoch
            if self.env!='real':raise NotSent('시간외 매도는 실계좌에서만 지원합니다')
            venues=(await self.broker().extended_metadata(code))['venues']
            if not venues or (after_hours!='both' and after_hours not in venues):raise NotSent('선택한 시간외 시장에서 거래할 수 없는 종목입니다')
            if epoch!=self.epoch:raise NotSent('연결·감시 상태 변경 · 다시 확인하세요')
        prepared=await super().prepare(code,bought_on)
        p=self.plans[prepared['code']]
        p.update(after_hours=after_hours,retry_unfilled=retry_unfilled,empty_book=empty_book,after_hours_venues=venues)
        p['rules']=[dict(id='horizon-'+kind,kind=kind,order_type='market',limit_price=None,
            expires_on=None,after_hours=after_hours,after_hours_venues=venues,
            retry_unfilled=retry_unfilled and kind=='loss',empty_book=empty_book)
            for kind in ('loss','profit')]
        self.persist();return p.copy()

    async def arm(self,plan_id,environment,market_order):
        p=next((p for p in self.plans.values() if p['id']==plan_id),None)
        if p and p.get('after_hours','off')!='off':
            self.gate();epoch=self.epoch
            venues=(await self.broker().extended_metadata(p['code']))['venues']
            if epoch!=self.epoch or venues!=p['after_hours_venues']:raise NotSent('시간외 거래 가능 시장·감시 상태 변경 · 다시 확인하세요')
        await super().arm(plan_id,environment,market_order)
        if p:
            for venue in ('KRX','NXT'):self.extended_quotes.pop((p['code'],venue),None)

    def pause(self,code=None,note='사용자가 호라이즌 자동매도를 껐습니다'):
        self.stop_reprices(code)
        super().pause(code,note)

    def ticket_pause(self,code=None):
        self.pause(code,note='시간외 재주문 중단 · 주문내역 확인 필요')

    def ticket_context(self,p,r,at,venue=None):
        # A clock/weekday alone is not evidence that today is a trading day.
        if at.date().isoformat() not in self.monitor.sessions:return None
        return super().ticket_context(p,r,at,venue)

    def reason(self,p,at):
        regular=super().reason(p,at) if (9,0) <= (at.hour,at.minute) < (15,30) else None
        if regular:
            self.horizon_triggers.pop(p['code'],None)
            return regular
        if p.get('after_hours_retry_at') and (at-datetime.fromisoformat(p['after_hours_retry_at'])).total_seconds()<12:return None
        for r in p.get('rules',[]):
            context=self.ticket_context(p,r,at)
            if not context or not context['extended']:continue
            price=context['price']
            hit=price*100<=p['average']*90 if r['kind']=='loss' else price*100>=p['average']*115
            if hit:
                reason='STOP_LOSS' if r['kind']=='loss' else 'TAKE_PROFIT'
                self.horizon_triggers[p['code']]={'reason':reason,'rule':r,'context':context.copy()}
                return reason
        return None

    async def execute(self,p,reason,trigger_at,epoch):
        trigger=self.horizon_triggers.pop(p['code'],None)
        if not trigger:return await super().execute(p,reason,trigger_at,epoch)
        order=None;book=None;r=trigger['rule'];venue=trigger['context']['venue']
        try:
            async with self.lock:
                def guard():
                    self.gate()
                    ctx=self.ticket_context(p,r,self.clock(),venue)
                    if (epoch!=self.epoch or p['status']!='checking' or self.plans.get(p['code']) is not p
                        or reason!=trigger['reason'] or not ctx or not ctx['extended']
                        or not 0<=(self.clock()-trigger_at).total_seconds()<=10):
                        raise NotSent('조건 중지·시세·거래시간 변경 · 시간외 주문하지 않았습니다')
                    if book and not 0<=(self.clock()-datetime.fromisoformat(book['at'])).total_seconds()<=10:
                        raise QuoteWait('주문 준비 중 호가 지연 · 최신 호가 대기')
                guard();row=await self.inspect(p['code']);guard()
                if (row['quantity'],row['average'])!=(p['quantity'],p['average']):raise NotSent('잔고 변경 · 다시 확인하세요')
                book=await self.broker().extended_book(p['code'],venue);guard()
                price=self.book_price(r,book)
                order=dict(id=secrets.token_hex(12),plan_id=p['id'],rule_id=r['id'],code=p['code'],name=p['name'],
                    quantity=p['quantity'],position_quantity=row['quantity'],reason=reason,environment=self.env,
                    day=trigger_at.date().isoformat(),at=trigger_at.isoformat(),status='submitting',order_no='',
                    filled=0,remaining=p['quantity'],checked_at=None,note='시간외 지정가 전송 중',
                    order_type='limit',limit_price=price,after_hours=True,venue=venue,
                    chase_active=bool(r['retry_unfilled']),retry_attempt=0,replace_state='watching')
                self.orders.append(order);self.persist()
                order['order_no']=await self.broker().extended_sell_once(p['code'],p['quantity'],price,venue,guard)
                order.update(status='accepted',note='시간외 지정가 접수 · 체결 확인 중')
                p.update(status='ordered',note=order['note']);self.persist()
        except NotSent as exc:
            if isinstance(exc,QuoteWait) and order is None and epoch==self.epoch and p['status']=='checking':
                p.update(status='armed',note=str(exc),after_hours_retry_at=self.clock().isoformat())
            else:
                if order:order.update(status='not_sent',chase_active=False,note=str(exc))
                self.pause(p['code'],str(exc))
        except OrderRejected as exc:
            if order:order.update(status='rejected',chase_active=False,note=str(exc))
            self.pause(p['code'],'시간외 주문 거부 · 조건·주문내역 확인 필요')
        except (Exception,asyncio.CancelledError):
            if order:order.update(status='unknown',chase_active=False,note='응답 불명 · 자동 재전송 없음')
            self.pause(p['code'],'시간외 주문·저장 결과 확인 필요')
        finally:
            if not self.error:
                try:self.persist()
                except Exception:pass

    def order_session_open(self, order, at):
        if order.get('after_hours'):
            return order['day']==at.date().isoformat() and extended_session(order['venue'],at)
        return super().order_session_open(order,at)

    async def _reconcile(self):
        tracking={p['id'] for p in self.plans.values() if p['status']=='ordered'}
        await super()._reconcile()
        for p in self.plans.values():
            orders=[o for o in self.orders if o.get('plan_id')==p['id']]
            if p['id'] in tracking:
                pending=[o for o in orders if o['status'] in PENDING or o.get('chase_active')]
                if pending:p.update(status='ordered',note=pending[-1]['note'])
                elif orders:
                    latest=orders[-1]
                    if latest['status']=='filled':p.update(status='completed',note=latest['note'])
                    elif latest['status'] in ('closed','rejected','not_sent'):p.update(status='paused',note=latest['note'])
        if self.owner and not self.error:self.persist()

    def snapshot(self):
        s=super().snapshot();at=self.clock()
        for p in s['plans']:
            if p.get('after_hours','off')=='off':continue
            p['watch_state']=p['status']
            if p['status']=='armed':
                contexts=[self.ticket_context(p,r,at) for r in p.get('rules',[])]
                live=next((x for x in contexts if x and x['extended']),None)
                if live:p.update(watch_state='watching',note=live['venue']+' 시간외 익절·손절 감시 중')
                elif any(extended_session(v,at) for r in p.get('rules',[]) for v in selected_venues(r)):
                    p.update(watch_state='quote_wait',note='시간외 최신 체결가·거래일 확인 대기')
        s['enabled']=s['enabled'] or any(o.get('chase_active') for o in self.orders)
        s['rules']['after_hours_supported']=True
        return s
