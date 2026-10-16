"""Explicit, reviewed cancellation of this engine's own sell orders only."""
import asyncio
import secrets
from datetime import datetime
from app.autoexit import PENDING
from app.kiwoom.orders import NotSent, OrderRejected

class ManualCancelMixin:
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.cancel_reviews={}

    def open(self):
        super().open()
        self.cancel_reviews.clear()
        for o in self.orders:
            if o.get('manual_cancel_state')=='sending':
                o.update(manual_cancel_state='unknown',manual_cancel_note='취소 전송 중 재시작 · 재전송 차단 · 키움 확인 필요')
        if self.owner and not self.error:self.persist()

    def cancel_identity(self,o):
        return (o['id'],o['code'],o['order_no'],o['quantity'],o['day'],o.get('venue','KRX'))

    def cancel_detail_valid(self,o,d):
        if d is None:raise NotSent('원주문 조회 없음 · 취소 대상에서 제외')
        f,r=d['filled'],d['remaining']
        if type(f) is not int or type(r) is not int or not o['filled']<=f<=o['quantity'] or not 0<=r<=o['quantity']-f:
            raise NotSent('원주문 체결·잔량 불일치')
        return f,r

    def cancel_eligible(self,o):
        return (o['status'] in PENDING and bool(o.get('order_no'))
            and o['day']==self.clock().date().isoformat()
            and not o.get('manual_cancel_state')
            and o.get('replace_state') not in ('cancelling','cancel_wait','stopped','replaced'))

    async def prepare_manual_cancel(self):
        async with self.reconcile_lock:
            async with self.lock:
                self.gate()
                if self.env!='real':raise NotSent('미체결 매도 일괄취소는 현재 실계좌만 지원합니다')
                epoch=self.epoch;items=[];excluded=[]
                for o in self.orders:
                    if o['status'] not in PENDING:continue
                    if not self.cancel_eligible(o):
                        excluded.append(dict(name=o['name'],order_no=o.get('order_no'),note='날짜·취소 진행·원주문번호 확인 필요'));continue
                    try:
                        d=await self.broker().detail(o);f,r=self.cancel_detail_valid(o,d)
                        self.gate()
                        if epoch!=self.epoch:raise NotSent('감시 상태 변경 · 다시 조회하세요')
                        if r and not d['rejected']:
                            items.append(dict(id=o['id'],identity=self.cancel_identity(o),name=o['name'],code=o['code'],
                                order_no=o['order_no'],quantity=o['quantity'],filled=f,remaining=r,
                                venue=o.get('venue','KRX'),limit_price=o.get('limit_price')))
                    except Exception:
                        excluded.append(dict(name=o['name'],order_no=o.get('order_no'),note='증권사 원주문·잔량 조회 확인 필요'))
                self.gate()
                if epoch!=self.epoch:raise NotSent('감시 상태 변경 · 다시 조회하세요')
                review=dict(review_id=secrets.token_urlsafe(24),environment=self.env,at=self.clock().isoformat(),epoch=epoch,items=items,excluded=excluded)
                self.cancel_reviews={review['review_id']:review}
                return review

    async def confirm_manual_cancel(self,review_id,environment,confirm):
        async with self.reconcile_lock:
            async with self.lock:
                self.gate();review=self.cancel_reviews.pop(review_id,None)
                if (not review or confirm is not True or environment!=self.env or review['environment']!=self.env
                    or review['epoch']!=self.epoch or not 0<=(self.clock()-datetime.fromisoformat(review['at'])).total_seconds()<=60):
                    raise NotSent('취소 확인이 만료되었거나 상태가 바뀌었습니다 · 다시 조회하세요')
                result=[]
                # Disable continuation/repricing before any cancellation request; never re-arm.
                for code in {i['code'] for i in review['items']}:self.pause(code,note='사용자 미체결 취소 · 해당 종목 감시 중지')
                epoch=self.epoch
                for i in review['items']:
                    o=next((o for o in self.orders if o['id']==i['id']),None)
                    sent=False
                    try:
                        def guard():
                            self.gate()
                            if (epoch!=self.epoch or not o or self.cancel_identity(o)!=tuple(i['identity'])
                                or o['day']!=self.clock().date().isoformat()
                                or not 0<=(self.clock()-datetime.fromisoformat(review['at'])).total_seconds()<=60):raise NotSent('취소 대상·계좌 상태 변경')
                        guard()
                        if not self.cancel_eligible(o):raise NotSent('원주문 상태 변경 · 취소하지 않았습니다')
                        d=await self.broker().detail(o);guard();f,r=self.cancel_detail_valid(o,d)
                        if d['rejected'] or not r:
                            result.append(dict(id=i['id'],state='skipped',note='잔량 없음 · 취소 전송 안 함'));continue
                        if r>i['remaining']:raise NotSent('확인한 잔량보다 증가 · 다시 확인하세요')
                        o.update(manual_cancel_state='sending',manual_cancel_at=self.clock().isoformat(),
                            manual_cancel_remaining=r,manual_cancel_note='잔량 취소 전송 중 · 완료 아님')
                        self.persist();sent=True
                        number=await self.broker().extended_cancel_once(o|{'venue':i['venue']},guard)
                        o.update(manual_cancel_state='accepted',manual_cancel_no=number,manual_cancel_note='취소 접수 · 증권사 완료 확인 대기')
                        self.persist();result.append(dict(id=i['id'],state='accepted',note=o['manual_cancel_note']))
                    except (NotSent,OrderRejected) as exc:
                        if o and sent:o.update(manual_cancel_state='rejected' if isinstance(exc,OrderRejected) else 'not_sent',manual_cancel_note=str(exc))
                        result.append(dict(id=i['id'],state='not_sent',note=str(exc)))
                    except (Exception,asyncio.CancelledError):
                        if o and sent:o.update(manual_cancel_state='unknown',manual_cancel_note='취소 응답 불명 · 재전송 없음 · 키움 확인 필요')
                        result.append(dict(id=i['id'],state='unknown',note='취소 결과 확인 필요 · 추가 전송 중지'))
                        break
                    finally:
                        if self.owner and not self.error:self.persist()
                return dict(results=result)

    async def _reconcile(self):
        await super()._reconcile()
        for o in self.orders:
            if o.get('manual_cancel_state')!='accepted':continue
            try:
                d=await self.broker().detail(o);f,r=self.cancel_detail_valid(o,d)
                if f==o['quantity']:
                    o.update(manual_cancel_state='filled',manual_cancel_note='취소 완료 전 전량 체결 확인');continue
                view=o|{'venue':o.get('venue','KRX'),'cancel_no':o['manual_cancel_no']}
                cancel=await self.broker().extended_cancel_detail(view)
                if cancel and not r and cancel['confirmed']==o['quantity']-f:
                    o.update(manual_cancel_state='confirmed',manual_cancel_quantity=cancel['confirmed'],manual_cancel_note='증권사 잔량 취소 완료 확인')
                else:o['manual_cancel_note']='취소 접수 · 증권사 완료 확인 대기'
            except Exception:o['manual_cancel_note']='취소 완료 조회 실패 · 키움 확인 필요'
        if self.owner and not self.error:self.persist()
