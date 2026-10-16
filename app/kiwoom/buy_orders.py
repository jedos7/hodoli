"""Cash limit buys and explicit remainder cancellations. Never replay mutations."""
import asyncio
import time

from app.autosell import normal_code
from app.kiwoom.orders import KiwoomOrders, NotSent, OrderRejected, OrderUnknown


def integer(value):
    text = str(value).strip()
    if not text or not text.lstrip('+').isdigit():
        raise ValueError('증권사 수량·금액 응답 확인 필요')
    return int(text)


class KiwoomBuyOrders(KiwoomOrders):
    async def stock(self, code, price):
        data, _ = await self.rest.post('/api/dostk/stkinfo', 'ka10100', {'stk_cd': code})
        if normal_code(data['code']) != code or not data.get('name'):
            raise NotSent('종목 정보를 확인하세요')
        if str(data.get('marketCode')) not in ('0', '10'):
            raise NotSent('현재 자동매수는 코스피·코스닥 주식만 지원합니다')
        state = str(data.get('state', ''))
        if not state or any(x in state for x in ('거래정지', '관리종목', '정리매매')):
            raise NotSent('거래 가능한 종목 상태를 확인하세요')
        if str(data.get('orderWarning', '')) not in ('0',):
            raise NotSent('투자유의 종목은 이 자동매수에서 제외합니다')
        tick = next(step for ceiling, step in ((2000,1),(5000,5),(20000,10),(50000,50),
                    (200000,100),(500000,500),(10**12,1000)) if price < ceiling)
        if price % tick:
            raise NotSent(f'이 가격대는 {tick:,}원 단위로 입력하세요')
        return {'code': code, 'name': data['name']}

    async def capacity(self, code, price):
        data, _ = await self.rest.post('/api/dostk/acnt', 'kt00011', {'stk_cd': code, 'uv': str(price)})
        return {'quantity': integer(data['min_ord_alowq']), 'amount': integer(data['min_ord_alow_amt'])}

    async def detail(self, order):
        rows = await self.pages('kt00007', {'ord_dt': order['day'].replace('-', ''), 'qry_tp': '1',
            'stk_bond_tp': '1', 'sell_tp': '2', 'stk_cd': order['code'], 'fr_ord_no': '',
            'dmst_stex_tp': 'KRX'}, 'acnt_ord_cntr_prps_dtl')
        found = [r for r in rows if str(r['ord_no']).lstrip('0') == order['order_no'].lstrip('0')
                 and normal_code(r['stk_cd']) == order['code']]
        if len(found) != 1:
            return None
        r = found[0]
        kind = str(r['io_tp_nm'])
        if (integer(r['ord_qty']) != order['quantity'] or integer(r['ord_uv']) != order['limit_price']
                or '매수' not in kind or any(x in kind for x in ('정정', '취소', '신용'))):
            raise ValueError('원 매수 주문 상세 불일치')
        return {'filled': integer(r['cntr_qty']), 'remaining': integer(r['ord_remnq']),
                'rejected': '거부' in str(r.get('acpt_tp', ''))}

    async def mutate_once(self, api, body, before_send):
        rest = self.rest
        expected = {'real': 'api.kiwoom.com', 'paper': 'mockapi.kiwoom.com'}.get(self.env)
        if not expected or rest.client.base_url.host != expected or rest.s.kiwoom_env != self.env:
            raise NotSent('계좌 환경과 주문 서버가 다릅니다')
        try:
            token = await rest.auth.access_token()
            async with rest._lock:
                wait = rest._gap - (time.monotonic() - rest._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                before_send()
                rest._last = time.monotonic()
        except NotSent:
            raise
        except Exception as exc:
            raise NotSent('주문 전 인증·상태 확인 실패') from exc
        try:
            response = await rest.client.post('/api/dostk/ordr', headers={
                'Content-Type':'application/json;charset=UTF-8', 'authorization':f'Bearer {token}',
                'api-id':api, 'cont-yn':'N', 'next-key':''}, json=body)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise OrderUnknown('주문 응답 불명 · 자동 재전송 차단 · 키움 확인 필요') from exc
        if 'return_code' not in data:
            raise OrderUnknown('주문 응답 형식 불명')
        if str(data['return_code']) != '0':
            raise OrderRejected('증권사 주문 거부 · 키움 주문내역 확인 필요')
        number = str(data.get('ord_no', '')).strip()
        if not number.isdigit() or not int(number):
            raise OrderUnknown('접수 응답에 주문번호가 없습니다')
        return number

    async def buy_once(self, code, quantity, price, before_send):
        if type(quantity) is not int or quantity <= 0 or type(price) is not int or price <= 0:
            raise NotSent('매수가와 수량을 확인하세요')
        return await self.mutate_once('kt10000', {'dmst_stex_tp':'KRX', 'stk_cd':normal_code(code),
            'ord_qty':str(quantity), 'ord_uv':str(price), 'trde_tp':'0', 'cond_uv':''}, before_send)

    async def cancel_once(self, order, before_send):
        return await self.mutate_once('kt10003', {'dmst_stex_tp':'KRX', 'stk_cd':order['code'],
            'orig_ord_no':order['order_no'], 'cncl_qty':'0'}, before_send)
