"""Venue-explicit after-market sells. All mutations use a single-attempt transport."""
import re
from datetime import datetime

from app.autosell import KST, normal_code, now_kst
from app.kiwoom.buy_orders import KiwoomBuyOrders, integer
from app.kiwoom.orders import KiwoomOrders, NotSent


class QuoteWait(NotSent):
    pass


def tick(price):
    return next(step for ceiling, step in ((2000,1),(5000,5),(20000,10),
        (50000,50),(200000,100),(500000,500),(10**12,1000)) if price < ceiling)


def lower_tick(price):
    value = price - 1
    return value - value % tick(value)


def price_number(value):
    text = str(value).strip()
    if not re.fullmatch(r'[+-]?\d+', text):
        raise NotSent('호가 숫자 형식 확인 필요')
    return abs(int(text))


class ExtendedSellOrders(KiwoomBuyOrders):
    async def detail(self, order):
        # The parent buy adapter has a buy-only detail method.
        return await KiwoomOrders.detail(self, order)

    async def extended_metadata(self, code):
        data, _ = await self.rest.post('/api/dostk/stkinfo', 'ka10100', {'stk_cd':code})
        if (normal_code(data.get('code','')) != code or str(data.get('marketCode')) not in ('0','10')
                or not data.get('state') or any(x in data['state'] for x in ('거래정지','관리종목','정리매매'))):
            raise NotSent('시간외 거래 가능한 코스피·코스닥 주식을 확인하세요')
        if str(data.get('nxtEnable','')).upper() not in ('Y','N'):
            raise NotSent('NXT 거래 가능 여부 확인 필요')
        return {'venues':['KRX','NXT'] if str(data['nxtEnable']).upper()=='Y' else ['KRX']}

    async def extended_book(self, code, venue):
        if venue not in ('KRX','NXT') or self.env != 'real':
            raise NotSent('시간외 주문은 지원되는 실계좌 시장에서만 가능합니다')
        meta = await self.extended_metadata(code)
        if venue not in meta['venues']:
            raise NotSent('현재 이 종목은 선택한 시장에서 거래할 수 없습니다')
        item = code + ('_NX' if venue == 'NXT' else '')
        info, _ = await self.rest.post('/api/dostk/stkinfo', 'ka10001', {'stk_cd':item})
        if normal_code(str(info.get('stk_cd','')).removesuffix('_NX')) != code:
            raise NotSent('가격제한폭 종목 불일치')
        floor, ceiling = price_number(info.get('lst_pric')), price_number(info.get('upl_pric'))
        if not 0 < floor < ceiling or floor % tick(floor) or ceiling % tick(ceiling):
            raise NotSent('당일 가격제한폭 확인 필요')
        book, _ = await self.rest.post('/api/dostk/mrkcond', 'ka10004', {'stk_cd':item})
        now = now_kst()
        text = str(book.get('bid_req_base_tm','')).strip()
        if not re.fullmatch(r'\d{6}', text):
            raise NotSent('최신 호가 시각 확인 필요')
        try:
            stamp = datetime.combine(now.date(), datetime.strptime(text,'%H%M%S').time(), KST)
        except ValueError as exc:
            raise NotSent('호가 시각 형식 확인 필요') from exc
        if not 0 <= (now-stamp).total_seconds() <= 10:
            raise QuoteWait('최신 매수호가 대기 · 10초 이상 지난 호가로 주문하지 않습니다')
        bids = []
        for level in range(1,11):
            prefix = 'buy_fpr' if level == 1 else f'buy_{level}th_pre'
            if prefix+'_bid' not in book or prefix+'_req' not in book:
                raise NotSent('매수호가 응답 누락 · 하한가로 대신 주문하지 않습니다')
            price, qty = price_number(book[prefix+'_bid']), integer(book[prefix+'_req'])
            if qty and not price:
                raise NotSent('매수호가 가격·잔량 불일치')
            if price and qty:
                if not floor <= price <= ceiling or price % tick(price):
                    raise NotSent('호가단위·가격제한폭이 맞지 않습니다')
                bids.append(price)
        if bids != sorted(bids, reverse=True):
            raise NotSent('매수호가 순서 확인 필요')
        return {'venue':venue,'at':stamp.isoformat(),'floor':floor,'ceiling':ceiling,
                'best':bids[0] if bids else None,'lowest':bids[-1] if bids else None}

    async def extended_sell_once(self, code, quantity, price, venue, before_send):
        if self.env != 'real' or venue not in ('KRX','NXT'):
            raise NotSent('시간외 실계좌·거래시장 확인 필요')
        if type(quantity) is not int or quantity <= 0 or type(price) is not int or price <= 0 or price % tick(price):
            raise NotSent('시간외 지정가·수량 확인 필요')
        return await self.mutate_once('kt10001', {'dmst_stex_tp':venue,'stk_cd':normal_code(code),
            'ord_qty':str(quantity),'ord_uv':str(price),'trde_tp':'0','cond_uv':''}, before_send)

    async def extended_cancel_once(self, order, before_send):
        if self.env != 'real' or order.get('venue') not in ('KRX','NXT'):
            raise NotSent('취소 대상 시장 확인 필요')
        return await self.mutate_once('kt10003', {'dmst_stex_tp':order['venue'],'stk_cd':order['code'],
            'orig_ord_no':order['order_no'],'cncl_qty':'0'}, before_send)

    async def extended_cancel_detail(self, order):
        rows = await self.pages('kt00007', {'ord_dt':order['day'].replace('-',''),'qry_tp':'1',
            'stk_bond_tp':'1','sell_tp':'1','stk_cd':order['code'],'fr_ord_no':'',
            'dmst_stex_tp':order['venue']}, 'acnt_ord_cntr_prps_dtl')
        found = [r for r in rows if str(r['ord_no']).lstrip('0') == order['cancel_no'].lstrip('0')]
        if len(found) != 1: return None
        r = found[0]
        if (normal_code(r['stk_cd']) != order['code'] or str(r.get('ori_ord','')).lstrip('0') != order['order_no'].lstrip('0')
                or '취소' not in r.get('io_tp_nm','') or '매도' not in r['io_tp_nm']
                or r.get('dmst_stex_tp') != order['venue']):
            raise NotSent('취소 주문·원주문·거래시장 불일치')
        if '거부' in str(r.get('acpt_tp','')): raise NotSent('취소 주문이 거부되었습니다')
        return {'confirmed':integer(r['cnfm_qty'])}
