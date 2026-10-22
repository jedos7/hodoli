"""⚡ 시초 역습 (변성환 2026-10-22 규칙 · 그가 10-22부터 실매매를 시작한 매매법).

깊게 갭하락으로 열리는 종목을 **09:00 동시호가에 받아 10:00에 판다.** 밤을 넘기지 않는 장중 매매라
하룻밤 전략(🌅 🗻 🔥 🌙)과 칸을 다투지 않는다.

  고르기  전날 등락 −3~−10% · 전날 거래대금 100~1,000억 (그날 아침에 아는 값)
  판단    08:59:45 동시호가 **예상체결가**(키움 ka10001 exp_cntr_pric)로
          · 예상 갭 −7% 이하 · 🧭 코스피 예상 갭이 −2%보다 덜 빠진 날만 (시총 상위 10 가중)
  사기    예상가 +2틱 지정가로 09:00 동시호가에 · 전날 대금의 2%까지 · 하루 2종목 (전날 대금 큰 순)
  팔기    체결되면 바로 **+7% 익절 지정가** · 09:59:50에 거두고 **10:00 시장가 전량**. 손절 없음

제작자 검증(우리 계좌 성과 아님): 144번 · 건당 +3.10% · 승률 78% · PF 8.12.
**시각에 아주 민감하다** — 09:05에 사면 PF 1.35로 무너진다고 본인이 적었다(반등이 5분 안에 끝난다).
우리 구조는 REST 조회가 초당 3건이라 예상체결가가 몇 초 묵을 수 있다. 흉내로 먼저 체결 밀림을 재야 한다.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, time

from app.closebet import SKIP_NAME, CloseBet, in_window
from app.collectors.naver_api import NaverApi, num
from app.fanout import fanout
from app.kiwoom.extended_sell import tick
from app.kiwoom.orders import NotSent, OrderRejected

LIST_WINDOW = (time(8, 10), time(8, 45))          # 전날 자료로 후보 추리기
WATCH_WINDOW = (time(8, 50), time(8, 59, 40))     # 예상체결가 지켜보기
BUY_WINDOW = (time(8, 59, 45), time(8, 59, 58))   # 판단과 주문
FILL_WINDOW = (time(9, 0, 30), time(9, 5))        # 체결 확인 → 익절 걸기
CANCEL_WINDOW = (time(9, 59, 50), time(10, 0))    # 익절 미체결 거두기
CLOSE_WINDOW = (time(10, 0), time(10, 10))        # 시장가 전량

GAP_MAX = -7.0                 # 예상 갭 (전날 종가 대비)
PREV_LOW, PREV_HIGH = -10.0, -3.0
AMOUNT_LOW_EOK, AMOUNT_HIGH_EOK = 100.0, 1000.0
INDEX_GAP_MIN = -2.0           # 코스피 예상 갭이 이보다 덜 빠진 날만
INDEX_TOP = 10                 # 시총 상위 10 가중
MAX_PICKS = 2
TAKE_PROFIT_PCT = 7.0
PREV_AMOUNT_SHARE = 0.02       # 한 종목 = 전날 대금의 2%까지
POLL_SECONDS = 20
NEAR_GAP = -5.0                # 주문 직전에 다시 볼 '가까운' 후보
TOTAL_CAP_MAX = 9_990_000
MAX_POSITIONS_MAX = 5
DEFAULT_SETTINGS = {'mode': 'dry', 'enabled': False, 'slot_amount': 1_000_000,
                    'max_positions': 2, 'total_cap': 3_000_000}


def normalize_settings(raw):
    s = DEFAULT_SETTINGS | {k: raw[k] for k in DEFAULT_SETTINGS if k in raw}
    if s['mode'] not in ('dry', 'live'):
        raise ValueError('mode 는 dry 또는 live')
    s['enabled'] = bool(s['enabled'])
    for key in ('slot_amount', 'max_positions', 'total_cap'):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError(f'{key} 는 양의 정수')
    if s['total_cap'] > TOTAL_CAP_MAX:
        raise ValueError(f'총 투입 상한은 {TOTAL_CAP_MAX:,}원 이하 (1,000만 원 미만)')
    if s['max_positions'] > MAX_POSITIONS_MAX:
        raise ValueError(f'자리 수는 최대 {MAX_POSITIONS_MAX}')
    if s['slot_amount'] > s['total_cap']:
        raise ValueError('한 칸 금액이 총 투입 상한보다 큽니다')
    return s


# ── 규칙 ─────────────────────────────────────────────────────
def qualifies(prev_chg, amount_eok):
    """전날 등락 −3~−10% · 전날 거래대금 100~1,000억."""
    return PREV_LOW <= prev_chg <= PREV_HIGH and AMOUNT_LOW_EOK <= amount_eok <= AMOUNT_HIGH_EOK


def gap_pct(expected, prev_close):
    return (expected / prev_close - 1) * 100 if prev_close > 0 and expected > 0 else 0.0


def order_price(expected):
    """예상가 + 2호가 (규칙 문서). 동시호가에 걸어 시가에 받는다."""
    return expected + tick(expected) * 2 if expected > 0 else 0


def take_profit_price(fill):
    """+7% 익절 지정가. 호가 단위로 **내려** 잡아 닿을 수 있게 한다."""
    if fill <= 0:
        return 0
    raw = int(fill * (1 + TAKE_PROFIT_PCT / 100))
    step = tick(raw)
    return raw - raw % step


def index_gap(rows):
    """코스피 예상 갭 = 시총 상위 N의 (예상가/전날종가 − 1)을 시총으로 가중한 값."""
    weight = sum(r['cap'] for r in rows if r.get('expected'))
    if weight <= 0:
        return None
    return sum(r['cap'] * gap_pct(r['expected'], r['prev_close']) for r in rows if r.get('expected')) / weight


def plan_quantity(price, budget, remaining_cap, prev_amount, cap_qty):
    """수량과 까닭. 예산 = min(한 칸, 총액 잔여, 전날 대금의 2%)."""
    if price <= 0:
        return 0, '예상체결가 없음'
    limit = min(budget, remaining_cap, prev_amount * PREV_AMOUNT_SHARE)
    qty = min(int(limit // price), cap_qty)
    if qty <= 0:
        return 0, f'예산 {limit:,.0f}원 · 주문가능 {cap_qty:,}주 → 1주도 못 삼'
    return qty, f'{qty:,}주 × {price:,}원 = {qty * price:,}원 (예산 {limit:,.0f}원 · 전날 대금의 2%까지)'


class DawnRaid(CloseBet):
    label, icon = '시초 역습', '⚡'
    gate_key = None                 # 제작자 규칙에 ⚡ 맥락 문은 없다 (코스피 예상 갭 조건이 그 자리)
    normalizer = staticmethod(normalize_settings)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings = dict(DEFAULT_SETTINGS)
        self.watch, self.index = {}, []     # 후보 {code: {...}} · 코스피 바구니
        self.polled_at = None
        self.poll_task = None

    # ── 하루 흐름 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5 or not self.owner or self.error:
            return
        d = self.day()
        if in_window(now, LIST_WINDOW) and not d.get('listed') and self.settings['enabled']:
            d['listed'] = now.isoformat()
            self.persist()
            await self.build_list(now)
        if (in_window(now, WATCH_WINDOW) and self.watch and self.settings['enabled'] and not self.poll_task
                and (self.polled_at is None or (now - self.polled_at).total_seconds() >= POLL_SECONDS)):
            self.polled_at = now
            self.poll_task = asyncio.create_task(self._poll(), name='dawnraid-poll')
            self.tasks.add(self.poll_task)
            self.poll_task.add_done_callback(self.tasks.discard)
        if in_window(now, BUY_WINDOW) and not d.get('bought') and self.settings['enabled']:
            d['bought'] = now.isoformat()
            self.persist()
            await self.decide_and_buy(now)
        if in_window(now, FILL_WINDOW) and d.get('bought') and not d.get('filled'):
            d['filled'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.settle(now)
        if in_window(now, CANCEL_WINDOW) and not d.get('cancelled') and self.open_sell_orders():
            d['cancelled'] = now.isoformat()
            self.persist()
            await self.cancel_take_profit(now)
        if in_window(now, CLOSE_WINDOW) and not d.get('closed') and self.positions:
            d['closed'] = now.isoformat()
            self.persist()
            await self.reconcile()      # 익절이 붙었는지·취소됐는지 먼저 맞춘다
            await self.close_out(now)

    async def scan(self, preview=False):
        """지금 후보 보기. **장중에 누르면 오늘 값**으로 세므로 '전날 자료' 가 아니다 (based_on 에 적는다)."""
        at = self.clock()
        await self.build_list(at, preview=preview)
        rows = sorted(self.watch.values(), key=lambda r: -r['prev_amount_eok'])
        self.scan_task = None
        return {'at': at.isoformat(), 'preview': preview, 'rows': rows[:60],
                'based_on': self.day().get('based_on', ''), 'error': self.day().get('list_error', '')}

    # ── 08:10 후보 추리기 (전날 자료 · 네이버) ──
    async def build_list(self, now, preview=False):
        api = NaverApi()
        watch, index, based = {}, [], ''
        try:
            for market, key in (('코스피', 'KOSPI'), ('코스닥', 'KOSDAQ')):
                for page in range(1, 40):
                    d = await api.json(f'/stocks/marketValue/{key}', {'page': page, 'pageSize': 100})
                    rows = d.get('stocks') or []
                    for r in rows:
                        code, name = str(r.get('itemCode') or ''), str(r.get('stockName') or '').strip()
                        if len(code) != 6 or r.get('stockEndType', 'stock') != 'stock':
                            continue
                        stop = (r.get('tradeStopType') or {}).get('name')
                        if (stop and stop != 'TRADING') or any(k in name for k in SKIP_NAME):
                            continue
                        based = based or str(r.get('localTradedAt', ''))[:10]
                        prev_close = int(num(r.get('closePrice')))
                        chg = num(r.get('fluctuationsRatio'))
                        amount_eok = num(r.get('accumulatedTradingValue')) / 100      # 백만원 → 억
                        cap = num(r.get('marketValue'))
                        if key == 'KOSPI' and len(index) < INDEX_TOP and prev_close > 0:
                            index.append({'code': code, 'name': name, 'prev_close': prev_close, 'cap': cap})
                        if prev_close >= 1000 and qualifies(chg, amount_eok):
                            watch[code] = {'code': code, 'name': name, 'market': market, 'prev_close': prev_close,
                                           'prev_chg': round(chg, 2), 'prev_amount_eok': round(amount_eok, 1),
                                           'expected': 0, 'gap': None, 'at': ''}
                    if len(rows) < 100:
                        break
        except Exception as exc:
            self.day()['list_error'] = f'후보 목록 조회 실패: {str(exc)[:80]}'
        finally:
            await api.close()
        if watch:
            self.watch, self.index = watch, index
        self.day()['candidates'] = len(self.watch)
        self.day()['based_on'] = based
        if preview:
            return
        self.persist()
        self.notify(f'{self.icon} {self.label} 08:10 후보 {len(self.watch)}종목 '
                    f'(전날 {PREV_HIGH:.0f}~{PREV_LOW:.0f}% · 대금 {AMOUNT_LOW_EOK:.0f}~{AMOUNT_HIGH_EOK:.0f}억)'
                    + ('' if self.live() else ' · 흉내 모드'))

    # ── 08:50~08:59:40 예상체결가 지켜보기 ──
    async def _poll(self, near_only=False):
        try:
            # 마지막 확인은 '가까운 것' 과 **아직 못 읽은 것** 만 다시 본다 (초를 아끼려고)
            codes = [c for c, r in self.watch.items()
                     if not near_only or r.get('gap') is None or r['gap'] <= NEAR_GAP]
            if not codes:
                return
            broker = self.broker()
            got = await fanout(codes, lambda c: broker.rest.basic(c))
            at = self.clock().isoformat()
            for code, info in zip(codes, got):
                row = self.watch.get(code)
                if row is None or isinstance(info, Exception):
                    continue
                expected = int(getattr(info, 'expected', 0) or 0)
                if expected > 0:
                    row.update(expected=expected, gap=round(gap_pct(expected, row['prev_close']), 2), at=at)
        except Exception as exc:
            self.day()['poll_error'] = str(exc)[:80]
        finally:
            self.poll_task = None

    async def read_index(self):
        """코스피 예상 갭 (시총 상위 10 가중). 못 읽으면 None."""
        if not self.index:
            return None
        broker = self.broker()
        got = await fanout([r['code'] for r in self.index], lambda c: broker.rest.basic(c))
        for row, info in zip(self.index, got):
            if not isinstance(info, Exception):
                row['expected'] = int(getattr(info, 'expected', 0) or 0)
        return index_gap(self.index)

    # ── 08:59:45 판단과 주문 ──
    async def decide_and_buy(self, now):
        d = self.day()
        try:
            await self._poll(near_only=True)        # 가까운 후보만 마지막으로 다시 본다
            gap = await self.read_index()
        except Exception as exc:
            d['error'] = f'판단 자료 조회 실패: {str(exc)[:80]}'
            self.persist()
            self.notify(f'{self.icon} {self.label} 못 삼 — {d["error"]}')
            return
        d['index_gap'] = None if gap is None else round(gap, 2)
        if gap is None:
            d['skipped'] = ['코스피 예상 갭을 못 읽어 쉼']
            self.persist()
            self.notify(f'{self.icon} {self.label} 코스피 예상 갭을 못 읽어 오늘은 쉽니다')
            return
        if gap < INDEX_GAP_MIN:
            d['skipped'] = [f'코스피 예상 갭 {gap:+.2f}% · {INDEX_GAP_MIN:+.0f}% 보다 많이 빠져 쉼']
            self.persist()
            self.notify(f'{self.icon} {self.label} 오늘은 쉽니다 · 코스피 예상 갭 {gap:+.2f}%')
            return
        picks = [r for r in self.watch.values() if r.get('gap') is not None and r['gap'] <= GAP_MAX]
        picks.sort(key=lambda r: -r['prev_amount_eok'])
        d['picked'] = [{'code': r['code'], 'name': r['name'], 'gap': r['gap'],
                        'prev_amount_eok': r['prev_amount_eok']} for r in picks[:MAX_PICKS]]
        if not picks:
            self.persist()
            self.notify(f'{self.icon} {self.label} 예상 갭 {GAP_MAX:.0f}% 이하 종목 없음 · 코스피 {gap:+.2f}%')
            return
        await self.read_sizing()
        room = self.settings['max_positions'] - len(self.held_codes())
        bought = []
        for r in picks[:MAX_PICKS]:
            if room <= 0:
                break
            if r['code'] in self.positions and self.positions[r['code']]['status'] in ('held', 'selling'):
                continue
            if self.peer_busy(r['code']):
                d.setdefault('skipped', []).append(f"{r['name']}: 다른 자동매매에 걸린 종목")
                continue
            if await self.buy_one(r, now):
                room -= 1
                bought.append(r['name'])
        if self.owner and not self.error:
            self.persist()
        mode = '실전' if self.live() else '흉내'
        self.notify(f"{self.icon} {self.label} 매수({mode}) " + (', '.join(bought) if bought else '없음')
                    + f" · 코스피 예상 갭 {gap:+.2f}%")

    async def buy_one(self, r, now):
        code, epoch = r['code'], self.epoch
        order = None
        try:
            async with self.lock:
                self.guard_live(epoch)
                self.breakers.check_buy(code)
                price = order_price(r['expected'])
                remaining = self.settings['total_cap'] - self.committed()
                cap_qty = 10 ** 8
                broker = self.broker()
                if self.live():
                    await broker.stock(code, price or 1)
                    cap_qty = (await broker.capacity(code, price))['quantity'] if price else 0
                    if code in (await broker.positions()):
                        raise NotSent('이미 계좌에 있는 종목 · 섞이지 않게 건너뜀')
                qty, why = plan_quantity(price, self.slot_amount(self.settings['slot_amount']), remaining,
                                         r['prev_amount_eok'] * 1e8, cap_qty)
                if qty <= 0:
                    raise NotSent(why)
                self.guard_live(epoch)
                self.breakers.check_buy(code, qty * price)
                order = {'id': secrets.token_hex(12), 'side': 'buy', 'code': code, 'name': r['name'],
                         'quantity': qty, 'limit_price': price, 'reason': 'DAWN_RAID', 'environment': self.env,
                         'day': now.date().isoformat(), 'at': now.isoformat(), 'status': 'submitting',
                         'order_no': '', 'filled': 0, 'remaining': qty, 'note': why, 'checked_at': None,
                         'dry': not self.live(), 'expected': r['expected'], 'gap': r['gap'],
                         'prev_close': r['prev_close']}
                self.orders.append(order)
                self.persist()
                if self.live():
                    order['order_no'] = await broker.buy_once(code, qty, price, lambda: self.guard_live(epoch))
                    order['status'], order['note'] = 'accepted', '동시호가 지정가 매수 접수 · 09:00 체결 확인'
                else:
                    order['status'], order['note'] = 'dry', '흉내: 전송하지 않음 · 09:00 시가로 체결 판정'
                self.breakers.record_buy(code, qty * price, self.label)
                self.persist()
                return True
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            self.day().setdefault('skipped', []).append(f"{r['name']}: {exc}")
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
            self.breakers.record_reject(code, exc, f'{self.label} 매수')
        except (Exception, asyncio.CancelledError) as exc:
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 주문내역 확인'
            else:
                self.day().setdefault('skipped', []).append(f"{r['name']}: 조회 실패 {str(exc)[:60]}")
        return False

    # ── 09:00:30 체결 확인 → 보유 → 익절 걸기 ──
    async def settle(self, now):
        today = self.today()
        if self.live():
            await super().settle_buys()
        else:
            broker = self.broker()
            for o in self.orders:
                if o['side'] != 'buy' or not o.get('dry') or o['day'] != today or o['code'] in self.positions:
                    continue
                try:
                    info = await broker.rest.basic(o['code'])
                    open_price = int(getattr(info, 'open', 0) or 0)
                except Exception as exc:
                    o['note'] = f'흉내 체결 판정 실패: {str(exc)[:60]}'
                    continue
                if open_price <= 0:
                    o['note'] = '흉내: 09:00 시가를 못 읽어 체결 판정 보류'
                    continue
                if open_price > o['limit_price']:
                    o['status'], o['filled'], o['remaining'] = 'closed', 0, o['quantity']
                    o['note'] = f'흉내: 시가 {open_price:,}원 > 지정가 {o["limit_price"]:,}원 · 미체결'
                    continue
                o['filled'], o['remaining'] = o['quantity'], 0
                o['note'] = f'흉내: 09:00 시가 {open_price:,}원에 전량 체결'
                self.positions[o['code']] = {'code': o['code'], 'name': o['name'], 'quantity': o['quantity'],
                                             'average': open_price, 'bought_on': today, 'status': 'held',
                                             'dry': True, 'buy_order': o['id']}
        self.persist()
        await self.arm_take_profit(now)

    async def arm_take_profit(self, now):
        """체결된 종목에 +7% 익절 지정가를 건다. 흉내는 기록만 하고 10:00에 고가로 판정한다."""
        armed = []
        for code in list(self.held_codes()):
            p = self.positions[code]
            if p.get('take_profit'):
                continue
            target = take_profit_price(int(p['average']))
            p['take_profit'] = target
            if p.get('dry'):
                p['note'] = f'흉내: +{TAKE_PROFIT_PCT:.0f}% 익절 {target:,}원 · 10:00까지 고가로 판정'
                armed.append(f"{p['name']} {target:,}")
                continue
            epoch = self.epoch
            try:
                async with self.lock:
                    self.guard_sell(epoch)
                    self.breakers.check_sell(code)
                    order = {'id': secrets.token_hex(12), 'side': 'sell', 'code': code, 'name': p['name'],
                             'quantity': p['quantity'], 'reason': 'TAKE_PROFIT', 'limit_price': target,
                             'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(),
                             'status': 'submitting', 'order_no': '', 'filled': 0, 'remaining': p['quantity'],
                             'note': f'+{TAKE_PROFIT_PCT:.0f}% 익절 지정가 전송 중', 'checked_at': None, 'dry': False}
                    self.orders.append(order)
                    self.persist()
                    self.breakers.record_sell(code, self.label)
                    order['order_no'] = await self.broker().sell_limit_once(
                        code, p['quantity'], target, lambda: self.guard_sell(epoch))
                    order['status'], order['note'] = 'accepted', '익절 지정가 접수 · 09:59:50 거둠'
                    p['status'], p['sell_order'] = 'selling', order['id']
                    armed.append(f"{p['name']} {target:,}")
            except Exception as exc:
                p['note'] = f'익절 걸기 실패: {str(exc)[:60]} · 10:00 시장가로 판다'
        if self.owner and not self.error:
            self.persist()
        if armed:
            self.notify(f"{self.icon} {self.label} +{TAKE_PROFIT_PCT:.0f}% 익절: " + ', '.join(armed))

    def sell_note(self):
        return f' · 바로 +{TAKE_PROFIT_PCT:.0f}% 익절을 걸고 10:00에 정리합니다'

    def open_sell_orders(self):
        return [o for o in self.orders if o['side'] == 'sell' and not o.get('dry')
                and o['day'] == self.today() and o['status'] in ('accepted', 'submitting') and o['order_no']]

    async def cancel_take_profit(self, now):
        for o in self.open_sell_orders():
            if o.get('cancel_sent'):
                continue
            epoch = self.epoch
            try:
                async with self.lock:
                    self.guard_sell(epoch)
                    o['cancel_sent'] = now.isoformat()
                    self.persist()
                    await self.broker().cancel_once(o, lambda: self.guard_sell(epoch))
                    o['note'] = '익절 미체결 취소 요청 · 10:00 시장가로 판다'
            except Exception as exc:
                o['note'] = f'익절 취소 실패: {str(exc)[:60]} · 키움에서 직접 확인'
        if self.owner and not self.error:
            self.persist()

    # ── 10:00 시장가 전량 ──
    async def close_out(self, now):
        sold = []
        for code in list(self.held_codes()):
            p = self.positions[code]
            if p.get('dry'):
                await self.close_dry(p, now)
                sold.append(p['name'])
                continue
            if await self.sell_one(p, now, 'RAID_CLOSE'):
                sold.append(p['name'])
        if self.owner and not self.error:
            self.persist()
        if sold:
            self.notify(f"{self.icon} {self.label} 10:00 정리({'실전' if self.live() else '흉내'}): " + ', '.join(sold))

    async def close_dry(self, p, now):
        """흉내 정리: 09:00~10:00 고가가 익절가에 닿았으면 익절가, 아니면 10:00 값으로 판다."""
        price, how = 0, ''
        try:
            info = await self.broker().rest.basic(p['code'])
            high = int(getattr(info, 'high', 0) or 0)
            last = int(getattr(info, 'price', 0) or 0)
            if high >= p.get('take_profit', 0) > 0:
                price, how = p['take_profit'], f'+{TAKE_PROFIT_PCT:.0f}% 익절 닿음 (고가 {high:,}원)'
            elif last > 0:
                price, how = last, '10:00 시장가'
        except Exception as exc:
            how = f'값 조회 실패: {str(exc)[:50]}'
        order = {'id': secrets.token_hex(12), 'side': 'sell', 'code': p['code'], 'name': p['name'],
                 'quantity': p['quantity'], 'reason': 'RAID_CLOSE', 'limit_price': price,
                 'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(),
                 'status': 'dry', 'order_no': '', 'filled': p['quantity'], 'remaining': 0,
                 'note': f'흉내: {how}', 'checked_at': None, 'dry': True}
        self.orders.append(order)
        p['status'], p['sold_on'], p['sold_price'] = 'sold', now.date().isoformat(), price
        p['note'] = f'흉내 정리 · {how}'
        self.persist()

    def snapshot(self):
        base = super().snapshot()
        base.pop('scan', None)
        base.pop('scan_dates', None)
        return base | {'watch': sorted(self.watch.values(), key=lambda r: -r['prev_amount_eok'])[:40],
                       'index': self.index,
                       'windows': {'list': '08:10', 'watch': '08:50~08:59:40', 'buy': '08:59:45',
                                   'fill': '09:00:30', 'close': '10:00'}}
