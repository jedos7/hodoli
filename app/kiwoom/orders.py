"""Cash sell transport. One submission only; an uncertain reply is never retried."""
import asyncio
import logging
import time

from app.autosell import normal_code, positive

log = logging.getLogger(__name__)


class NotSent(RuntimeError):
    pass


class OrderRejected(RuntimeError):
    pass


class OrderUnknown(RuntimeError):
    pass


def cash_positions(rows):
    result = {}
    for row in rows:
        if str(row.get('crd_tp', '')).strip() not in ('', '0', '00', '000', '현금'):
            continue
        qty = int(row['rmnd_qty'])
        if qty <= 0:
            continue
        code = normal_code(row['stk_cd'])
        if code in result:
            raise ValueError('동일 종목의 복수 잔고를 확인해야 합니다')
        result[code] = dict(code=code, name=row.get('stk_nm', code), quantity=qty,
                            available=int(row['trde_able_qty']), average=positive(row['pur_pric']))
    return result


class KiwoomOrders:
    def __init__(self, rest, env):
        self.rest, self.env = rest, env

    async def pages(self, api, body, field):
        rows, key, seen = [], '', set()
        for _ in range(100):
            data, key = await self.rest.post('/api/dostk/acnt', api, body, key)
            if not isinstance(data.get(field), list):
                raise ValueError('증권사 조회 형식을 확인해야 합니다')
            rows.extend(data[field])
            if not key:
                return rows
            if key in seen:
                break
            seen.add(key)
        raise ValueError('증권사 전체 페이지 조회 실패')

    async def positions(self):
        return cash_positions(await self.pages('kt00018', {'qry_tp': '2', 'dmst_stex_tp': 'KRX'},
                                               'acnt_evlt_remn_indv_tot'))

    _equity_logged = False

    async def equity(self):
        """kt00018 추정예탁자산(원). 차단기의 기준값 (app/breakers.py)."""
        data, _ = await self.rest.post('/api/dostk/acnt', 'kt00018', {'qry_tp': '2', 'dmst_stex_tp': 'KRX'})
        if not KiwoomOrders._equity_logged:
            KiwoomOrders._equity_logged = True
            log.info('kt00018 상위 항목: %s', {k: v for k, v in data.items() if not isinstance(v, (list, dict))})
        for key in ('prsm_dpst_aset_amt', 'tot_evlt_amt'):
            text = str(data.get(key, '')).strip().lstrip('+')
            if text.isdigit():
                return int(text)
        raise ValueError('kt00018 에서 예탁자산(추정예탁자산) 항목을 찾지 못했습니다')

    async def open_orders(self, code):
        rows = await self.pages('ka10075', {'all_stk_tp': '1', 'trde_tp': '0',
                                          'stk_cd': code, 'stex_tp': '0'}, 'oso')
        return [r for r in rows if normal_code(r['stk_cd']) == code and int(r['oso_qty']) > 0]

    async def detail(self, order):
        rows = await self.pages('kt00007', {'ord_dt': order['day'].replace('-', ''), 'qry_tp': '1',
                                          'stk_bond_tp': '1', 'sell_tp': '1', 'stk_cd': order['code'],
                                          'fr_ord_no': '', 'dmst_stex_tp': '%'}, 'acnt_ord_cntr_prps_dtl')
        found = [r for r in rows if str(r['ord_no']).lstrip('0') == order['order_no'].lstrip('0')
                 and normal_code(r['stk_cd']) == order['code']]
        if len(found) != 1:
            return None  # Absence from unfilled orders is NOT evidence of a fill.
        r = found[0]
        if order.get('after_hours') and r.get('dmst_stex_tp') != order['venue']:
            raise ValueError('시간외 원주문 거래시장 불일치')
        if int(r['ord_qty']) != order['quantity'] or '매도' not in r['io_tp_nm']:
            raise ValueError('주문 상세 불일치')
        if order.get('order_type') == 'limit' and (int(r['ord_uv']) != order['limit_price']
                or any(x in r['io_tp_nm'] for x in ('취소','정정','신용'))):
            raise ValueError('원 지정가 매도 주문 상세 불일치')
        return {'filled': int(r['cntr_qty']), 'remaining': int(r['ord_remnq']),
                'rejected': '거부' in str(r.get('acpt_tp', ''))}

    async def sell_once(self, code, quantity, before_send):
        return await self._sell_once(code, quantity, None, before_send)

    async def validate_sell_limit(self, code, price):
        if type(price) is not int or not 0 < price < 10**9:
            raise NotSent('지정가 매도 가격을 확인하세요')
        data, _ = await self.rest.post('/api/dostk/stkinfo', 'ka10100', {'stk_cd':code})
        if normal_code(data['code']) != code:
            raise NotSent('지정가 종목 정보 불일치')
        market = str(data.get('marketCode'))
        if market not in ('0','10','8'):
            raise NotSent('이 종목의 지정가 호가단위를 확인할 수 없습니다 · 시장가를 선택하세요')
        tick = 5 if market == '8' else next(step for ceiling, step in
            ((2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500),(10**12,1000)) if price < ceiling)
        if type(price) is not int or price <= 0 or price % tick:
            raise NotSent(f'이 가격대의 지정가는 {tick:,}원 단위로 입력하세요')

    async def sell_limit_once(self, code, quantity, price, before_send):
        if type(price) is not int or not 0 < price < 10**9:
            raise NotSent('지정가 매도 가격을 확인하세요')
        return await self._sell_once(code, quantity, price, before_send)

    async def _sell_once(self, code, quantity, price, before_send):
        if type(quantity) is not int or quantity <= 0:
            raise NotSent('매도 수량을 확인하세요')
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
                before_send()  # Stop/disconnect during authentication or rate waiting must win.
                rest._last = time.monotonic()
        except NotSent:
            raise
        except Exception as exc:
            raise NotSent('주문 전 인증/연결 확인 실패') from exc
        try:
            response = await rest.client.post('/api/dostk/ordr', headers={
                'Content-Type': 'application/json;charset=UTF-8', 'authorization': f'Bearer {token}',
                'api-id': 'kt10001', 'cont-yn': 'N', 'next-key': ''}, json={
                'dmst_stex_tp': 'KRX', 'stk_cd': normal_code(code), 'ord_qty': str(quantity),
                'ord_uv': str(price) if price is not None else '', 'trde_tp': '0' if price is not None else '3', 'cond_uv': ''})
            # No 429, timeout, HTTP, or authentication replay on this transport.
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise OrderUnknown('주문 응답 불명 · 키움 주문내역을 확인하세요') from exc
        if 'return_code' not in data:
            raise OrderUnknown('주문 응답 형식 불명')
        if str(data['return_code']) != '0':
            raise OrderRejected('증권사 주문 거부 · 키움 주문내역을 확인하세요')
        number = str(data.get('ord_no', '')).strip()
        if not number.isdigit() or not int(number):
            raise OrderUnknown('접수 응답에 주문번호가 없습니다')
        return number
