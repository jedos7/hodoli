"""종가배팅 전용 증권사 어댑터. 조회(순위·일봉·시간봉·호가)와 1회 전송 주문(지정가 매수 kt10000 · 시장가 매도 kt10001)."""
from datetime import date

from app.autosell import normal_code
from app.kiwoom.buy_orders import KiwoomBuyOrders, integer
from app.kiwoom.orders import KiwoomOrders, NotSent
from app.kiwoom.parse import num, parse_daily


class CloseBetBroker(KiwoomBuyOrders):
    async def ranking(self, market):
        """ka10032 거래대금 상위 100 (market '001' 코스피 · '101' 코스닥). trde_prica 는 백만원."""
        data, _ = await self.rest.post('/api/dostk/rkinfo', 'ka10032', {'mrkt_tp': market, 'mang_stk_incls': '1', 'stex_tp': '1'})
        rows = data.get('trde_prica_upper')
        if not isinstance(rows, list):
            raise ValueError('거래대금 순위 응답 형식 확인 필요')
        out = []
        for r in rows:
            code = str(r.get('stk_cd', '')).strip()
            if len(code) != 6:
                continue
            out.append({'code': code, 'name': str(r.get('stk_nm', '')).strip(), 'price': abs(int(num(r.get('cur_prc')))),
                        'amount_eok': num(r.get('trde_prica')) / 100, 'chg': num(r.get('flu_rt'))})
        return out

    async def daily(self, code, days=80):
        """오늘 봉(장중 잠정)을 포함한 일봉, 오래된 순."""
        data, _ = await self.rest.post('/api/dostk/chart', 'ka10081', {'stk_cd': code, 'base_dt': date.today().strftime('%Y%m%d'), 'upd_stkpc_tp': '1'})
        return parse_daily(data, self.rest.s.kiwoom_amount_unit)[-days:]

    async def minutes(self, code, scope):
        """ka10080 분봉 (scope '60' · '5'). KRX 정규장 09:00~15:30 봉만, 오래된 순. [(stamp, o, h, l, c, v)]"""
        data, _ = await self.rest.post('/api/dostk/chart', 'ka10080', {'stk_cd': code, 'tic_scope': str(scope), 'upd_stkpc_tp': '1'})
        rows = data.get('stk_min_pole_chart_qry')
        if not isinstance(rows, list):
            raise ValueError('분봉 응답 형식 확인 필요')
        out = []
        for r in rows:
            stamp = str(r.get('cntr_tm', ''))
            if len(stamp) != 14 or not stamp.isdigit() or not '090000' <= stamp[8:] <= '153000':
                continue
            o, h, l, c = (abs(num(r.get(k))) for k in ('open_pric', 'high_pric', 'low_pric', 'cur_prc'))
            if min(o, h, l, c) <= 0:
                continue
            out.append((stamp, o, h, l, c, abs(num(r.get('trde_qty')))))
        out.sort()
        return out

    async def book(self, code):
        """ka10004 호가: 매도1호가·잔량, 매수1호가·잔량."""
        data, _ = await self.rest.post('/api/dostk/mrkcond', 'ka10004', {'stk_cd': code})
        return {'ask': abs(int(num(data.get('sel_fpr_bid')))), 'ask_qty': int(num(data.get('sel_fpr_req'))),
                'bid': abs(int(num(data.get('buy_fpr_bid')))), 'bid_qty': int(num(data.get('buy_fpr_req'))),
                'at': str(data.get('bid_req_base_tm', ''))}

    async def detail(self, order):
        if order.get('side') == 'sell':
            return await KiwoomOrders.detail(self, order)
        return await super().detail(order)

    async def sell_market_once(self, code, quantity, before_send):
        return await self.sell_once(normal_code(code), quantity, before_send)
