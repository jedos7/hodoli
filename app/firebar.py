"""🔥 기준봉 종가 (변성환 규칙 문서 2026-10-19).

오늘 대금이 터진 기준봉 가운데 **윗꼬리 없이** 마감하는 종목을 종가에 사서 다음 날 시초가에 판다.
🌙 종가배팅과 흐름이 같아(종가 매수 → 다음 날 09:00 시초가 시장가 매도) app/closebet.py 의 뼈대를 물려받고
후보 고르는 법과 매수 지정가만 바꾼다.

  후보: 오늘 등락 +15% ~ +29% 미만(상한가에 잠기면 종가에 못 사므로 제외)
        · 대금 300억↑(문턱 설정) · 앞 20일 평균 대금의 5배↑ · 전날 종가 1,000원↑
  판정: 15:10 에 1차로 고르고, 15:19:40 주문 직전에 다시 확인한다 (문서의 "15:10 · 15:15 값으로 판정")
        윗꼬리 = (고가 − 지금 값) ÷ 전날 종가 < 2%p  → 고가 근처에서 끝나는 종목만. 대금 큰 순
  매수: 15:19:40 에 **지금 값 +2틱** 지정가(상한가는 넘지 않게) · 현금 1배 — 종가배팅(15:19:50)보다 10초 먼저
  매도: 다음 날 09:00 시초가에 시장가 전량. 손절 없음

제작자 성적(우리 계좌 성과 아님): 213번 PF 4.34, 네 매매를 같이 돌리면 129번 PF 3.20.
최근 30번은 PF 2.44 로 누적보다 낮아 제작자도 지켜보는 중이라고 적혀 있다.
"""
from __future__ import annotations

from datetime import time
from statistics import mean
from time import monotonic

from app.closebet import SKIP_NAME, CloseBet, buy_price
from app.fanout import fanout
from app.kiwoom.extended_sell import tick
from app.kiwoom.orders import NotSent

SCAN_WINDOW = (time(15, 10), time(15, 14))
BUY_WINDOW = (time(15, 19, 40), time(15, 21))
MIN_RISE, MAX_RISE = 15.0, 29.0      # 상한가(+29.x%)에 잠긴 종목은 종가에 못 산다
MIN_MULTIPLE = 5.0                   # 앞 20일 평균 대금의 몇 배
MAX_TAIL = 2.0                       # 윗꼬리 %p (전날 종가 대비)


def upper_limit(prev_close):
    """상한가 근사: 전날 종가 × 1.3 을 호가단위로 내림."""
    if prev_close <= 0:
        return 0
    cap = int(prev_close * 1.3)
    return cap - cap % tick(cap)


def tail_pct(high, price, prev_close):
    """윗꼬리 = (고가 − 지금 값) ÷ 전날 종가 × 100 (%p)."""
    return (high - price) / prev_close * 100 if prev_close > 0 else 99.0


def cheap_checks(row, threshold_eok):
    """일봉 없이 순위 자료만으로 보는 조건 → (checks, 전날 종가).

    전날 종가·등락률은 **키움 순위 자료(flu_rt)** 로 계산한다.
    ka10081 일봉은 수정주가라 과거 종가가 실제와 다를 수 있다 (2026-10-19 에스엠벡셀: 일봉 3,150 / 실제 3,110
    → 등락이 28.25% 로 계산돼 상한가 제외를 빠져나갔다).
    등락이 +15~29% 밖인 종목(보통 거래대금 상위의 95%)은 여기서 끝나므로 일봉을 받지 않는다."""
    checks = {'amount': (row['amount_eok'] >= threshold_eok,
                         f"거래대금 {row['amount_eok']:,.0f}억 (문턱 {threshold_eok:,}억)")}
    chg, price = row.get('chg'), row.get('price')
    if chg is None or 1 + chg / 100 <= 0 or not price:
        checks['data'] = (False, '순위 자료의 등락률·현재가 확인 필요')
        return checks, 0
    prev_close = round(price / (1 + chg / 100))
    checks['price'] = (prev_close >= 1000, f"전날 종가 {prev_close:,}원 (1,000원 미만 제외)")
    checks['rise'] = (MIN_RISE <= chg < MAX_RISE, f"오늘 등락 {chg:+.2f}% ({MIN_RISE:.0f}~{MAX_RISE:.0f}% 사이만)")
    return checks, prev_close


def evaluate(row, daily, threshold_eok, today):
    """후보 한 종목 판정. row = 거래대금 순위 행, daily = 오늘 봉 포함 일봉(Candle).
    daily=None 이면 싼 조건만 보고 **일봉이 필요한지**(need_daily) 알려준다."""
    checks, prev_close = cheap_checks(row, threshold_eok)
    out = {'pass': False, 'checks': checks, 'need_daily': False,
           'prev_close': prev_close, 'upper_limit': upper_limit(prev_close)}
    if row.get('chg') is not None:
        out['chg'] = round(row['chg'], 2)
    if not all(ok for ok, _ in checks.values()):
        return out
    if daily is None:
        return out | {'need_daily': True}

    # 오늘 봉의 고가·거래대금은 수정 대상이 아니라 그대로 쓴다.
    if not daily or daily[-1].date != (today or daily[-1].date) or len(daily) < 25:
        checks['data'] = (False, '오늘 일봉 없음 또는 일봉 25개 미만')
        return out
    t = daily[-1]
    avg20 = mean(c.amount for c in daily[-21:-1]) if len(daily) >= 21 else 0
    mult = t.amount / avg20 if avg20 else 0
    checks['multiple'] = (mult >= MIN_MULTIPLE, f"평소(20일 평균) 대비 {mult:,.1f}배 ({MIN_MULTIPLE:.0f}배 이상)")
    tail = tail_pct(t.high, row['price'], prev_close)      # 지금 값은 순위 자료 기준
    checks['tail'] = (tail < MAX_TAIL, f"윗꼬리 {tail:.2f}%p (고가 {t.high:,} − 지금 {t.close:,}) · {MAX_TAIL}%p 미만만")
    return out | {'pass': all(ok for ok, _ in checks.values()),
                  'tail': round(tail, 2), 'multiple': round(mult, 1)}


class FireBar(CloseBet):
    label, icon = '기준봉 종가', '🔥'
    scan_window, buy_window = SCAN_WINDOW, BUY_WINDOW

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._info = {}      # code → (받은 시각, ka10001) · 주문 직전에 같은 조회를 두 번 하지 않게

    async def scan(self, preview=False):
        today = self.today()
        at, started = self.clock(), monotonic()
        result = {'at': at.isoformat(), 'preview': preview, 'threshold_eok': self.settings['threshold_eok'],
                  'rows': [], 'error': ''}
        try:
            broker = self.broker()
            rows = []
            for market in ('001', '101'):
                rows += await broker.ranking(market)
            rows = [r for r in rows if r['amount_eok'] >= self.settings['threshold_eok']
                    and not any(k in r['name'] for k in SKIP_NAME)]
            rows.sort(key=lambda r: -r['amount_eok'])
            ymd = None if preview else today.replace('-', '')
            th = self.settings['threshold_eok']

            async def judge(r):
                """등락이 범위 밖이면 일봉을 받지 않는다 (거래대금 상위 90종목 중 보통 2~5종목만 받는다)."""
                ev = evaluate(r, None, th, ymd)
                if ev['need_daily']:
                    ev = evaluate(r, await broker.daily(r['code']), th, ymd)
                return r | {'pass': ev['pass'], 'checks': {k: list(v) for k, v in ev['checks'].items()},
                            'prev_close': ev.get('prev_close'), 'upper_limit': ev.get('upper_limit'),
                            'tail': ev.get('tail'), 'multiple': ev.get('multiple')}

            for r, judged in zip(rows, await fanout(rows, judge)):
                result['rows'].append(judged if not isinstance(judged, Exception) else
                                      r | {'pass': False, 'checks': {'error': [False, f'조회 실패: {str(judged)[:80]}']}})
            result['passed'] = [r['code'] for r in result['rows'] if r['pass']]
            result['fetched'] = sum(1 for r in result['rows'] if r.get('multiple') is not None)
        except Exception as exc:
            result['error'] = f'후보 선정 실패: {str(exc)[:120]}'
        result['took_ms'] = round((monotonic() - started) * 1000)
        if not preview:
            self.scans[today] = result
            self.day()['scanned'] = at.isoformat()
            self.scans = dict(sorted(self.scans.items())[-10:])
            self.persist()
            passed = [r for r in result['rows'] if r['pass']]
            self.notify(f"{self.icon} {self.label} 15:10 후보 " + (
                f"{len(passed)}종목: " + ', '.join(f"{r['name']}({r['amount_eok']:,.0f}억·윗꼬리 {r['tail']:.1f}%p)" for r in passed[:5])
                if passed else f"없음 ({len(result['rows'])}종목 확인)")
                + ('' if self.settings['enabled'] else ' · 자동매매 꺼져 있어 매수 안 함') + ('' if self.live() else ' · 흉내 모드'))
        self.scan_task = None
        return result

    async def now_info(self, broker, code):
        """지금 시세(ka10001). 주문가 계산과 주문 직전 재확인이 **같은 값**을 쓰게 5초만 기억한다."""
        hit = self._info.get(code)
        if hit and monotonic() - hit[0] < 5:
            return hit[1]
        info = await broker.rest.basic(code)
        self._info[code] = (monotonic(), info)
        return info

    async def pre_buy_check(self, broker, r, price):
        """15:19:40 주문 직전 재확인 — 15:10 이후 윗꼬리가 생겼거나 상한가에 잠기면 사지 않는다."""
        prev_close = r.get('prev_close') or 0
        if prev_close <= 0:
            return
        info = await self.now_info(broker, r['code'])
        if info.price <= 0 or info.high <= 0:
            raise NotSent('주문 직전 시세 확인 실패')
        chg = (info.price / prev_close - 1) * 100
        tail = tail_pct(info.high, info.price, prev_close)
        if not MIN_RISE <= chg < MAX_RISE:
            raise NotSent(f'주문 직전 등락 {chg:+.1f}% · 범위를 벗어남')
        if tail >= MAX_TAIL:
            raise NotSent(f'주문 직전 윗꼬리 {tail:.2f}%p · {MAX_TAIL}%p 이상이라 건너뜀')

    async def order_price(self, broker, book, r):
        """지금 값 + 2틱. 상한가는 넘지 않는다."""
        info = await self.now_info(broker, r['code'])
        now_px = info.price if info.price > 0 else r['price']
        price = buy_price(now_px)
        cap = r.get('upper_limit') or 0
        return min(price, cap) if cap else price
