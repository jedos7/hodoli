"""종가배팅 자동매매 (변성환 규칙). 15:15 후보 선정 → 15:19:50 매도1호가 지정가 매수 → 다음 거래일 08:40 시장가 매도(09:00 시초가 체결).

규칙 (제공 자료 그대로. 원본 수식이 없는 부분은 아래에 명시)
  후보: 15:15 까지 정규장 거래대금 ≥ 문턱(기본 300억)
      · 일봉 MACD(12,26,9) 가 오늘 시그널을 위로 돌파
      · 일봉 슬로우 스토캐스틱(12,5,5) %K 가 오늘 20 을 위로 돌파
      · 240분봉 스토캐스틱(12,5,5) 의 fast %K · slow %K · slow %D 중 둘 이상이 직전 봉보다 상승  ← "둘 이상 상향" 해석
      · 전날 등락 +6.65% 초과 종목 제외
      · 오늘 고가 근처 종목 제외: (현재가−저가)/(고가−저가) > 81.4%
  매수: 15:19:50 에 매도1호가 지정가. 수량 ≤ 매도1·매수1 잔량 중 작은 쪽, 금액 ≤ 설정 금액, ≤ 15:00~15:20 거래대금의 10%, 총액 ≤ 상한(1,000만 원 미만)
  매도: 다음 거래일 08:40 시장가(장전 동시호가 → 09:00 시초가). 09:03 체결 대조, 안 팔렸으면 장중 시장가 1회 더.
  240분봉 = 키움 60분봉을 09:00~12:00 / 13:00~15:30 두 덩어리로 합친 것. 오늘 봉은 15:15 시점의 미완성 봉.

검증(2026-10-17, scripts 폴더 bsh_close 재현): 우리 일봉으로는 '300억↑ 아무거나 종가 매수'(승률 54%, 건당 +0.48%)와 차이가 없었다.
240분 조건은 일봉으로 못 봤다. 사용자가 실전 소액으로 직접 확인하기로 결정했다.

안전장치: 실전 전송은 mode='live' 일 때만. dry 는 같은 흐름을 기록만 한다. 총액·종목 수 상한, 시간창 밖 금지, 연결 끊김 금지,
전송 전 저널, 전송 1회(응답 불명은 재전송 안 함), 다른 자동매매 엔진(호라이즌·내 조건)에 걸린 종목 건드리지 않음, 비상 정지.
"""
from __future__ import annotations

import asyncio
import json
import math
import secrets
from datetime import datetime, time
from statistics import mean

from app.autoexit import PENDING, AutoExit
from app.autosell import normal_code
from app.kiwoom.orders import NotSent, OrderRejected

TOTAL_CAP_MAX = 9_990_000       # 사용자 요구: 총 투입 1,000만 원 미만
MAX_POSITIONS_MAX = 5
SCAN_WINDOW = (time(15, 15), time(15, 18))
BUY_WINDOW = (time(15, 19, 50), time(15, 22))
SELL_WINDOW = (time(8, 30), time(9, 0))
RETRY_WINDOW = (time(9, 3), time(9, 30))
FILL_WINDOW = (time(15, 31), time(15, 45))
SKIP_NAME = ('KODEX', 'TIGER', 'KBSTAR', 'ACE ', 'SOL ', 'PLUS ', 'HANARO', 'ARIRANG', 'KOSEF', 'RISE ', 'KIWOOM ', 'ETN', '스팩', '레버리지', '인버스', '선물', '리츠')
DEFAULT_SETTINGS = {'mode': 'dry', 'enabled': False, 'per_trade_amount': 1_000_000, 'max_positions': 3,
                    'total_cap': 3_000_000, 'threshold_eok': 300, 'sell_time': '08:40'}


# ── 지표 ──────────────────────────────────────────────────────
def ema(xs, n):
    k = 2 / (n + 1)
    out, e = [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    m = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    return m, ema(m, signal)


def stoch(bars, n=12, k=5, d=5):
    """bars = [(high, low, close)]. 슬로우 스토캐스틱 → (fast %K, slow %K, slow %D)"""
    fast = []
    for i in range(len(bars)):
        w = bars[max(0, i - n + 1):i + 1]
        hi, lo = max(b[0] for b in w), min(b[1] for b in w)
        fast.append((bars[i][2] - lo) / (hi - lo) * 100 if hi > lo else 50.0)
    sma = lambda xs, m: [mean(xs[max(0, i - m + 1):i + 1]) for i in range(len(xs))]
    sk = sma(fast, k)
    return fast, sk, sma(sk, d)


def bars_240(hourly):
    """키움 60분봉 [(stamp, o, h, l, c, v)] → 240분봉 [(day, o, h, l, c)] (09:00~12:59 / 13:00~15:30)."""
    groups = {}
    for stamp, o, h, l, c, v in hourly:
        key = (stamp[:8], 0 if stamp[8:10] < '13' else 1)
        g = groups.setdefault(key, [o, h, l, c])
        g[1], g[2], g[3] = max(g[1], h), min(g[2], l), c
    return [(k[0], *groups[k]) for k in sorted(groups)]


def crossed_up(series, level=None, other=None):
    """오늘 값이 기준(level 또는 other 시리즈)을 위로 넘었나. 어제는 기준 이하, 오늘은 초과."""
    if len(series) < 2:
        return False
    ref_now, ref_prev = (level, level) if other is None else (other[-1], other[-2])
    return series[-1] > ref_now and series[-2] <= ref_prev


def evaluate(row, daily, hourly, threshold_eok, today):
    """후보 한 종목의 조건 판정. row = ranking 행, daily = 오늘 봉 포함 일봉(Candle), hourly = 60분봉.
    → {'pass': bool, 'checks': {이름: (통과, 설명)}, 값들}"""
    checks = {}
    amount_ok = row['amount_eok'] >= threshold_eok
    checks['amount'] = (amount_ok, f"15:15 거래대금 {row['amount_eok']:,.0f}억 (문턱 {threshold_eok:,}억)")
    if today is None and daily:          # 미리보기(휴장일 등): 마지막 일봉 날짜를 '오늘'로 본다
        today = daily[-1].date
    if not daily or daily[-1].date != today or len(daily) < 35:
        checks['data'] = (False, '오늘 일봉 없음 또는 일봉 35개 미만')
        return {'pass': False, 'checks': checks}
    closes = [c.close for c in daily]
    m, s = macd(closes)
    checks['macd'] = (crossed_up(m, other=s), f"MACD {m[-1]:.1f} / 시그널 {s[-1]:.1f} (어제 {m[-2]:.1f} / {s[-2]:.1f})")
    _, sk, _ = stoch([(c.high, c.low, c.close) for c in daily])
    checks['stoch'] = (crossed_up(sk, level=20), f"스토캐스틱 %K {sk[-1]:.1f} (어제 {sk[-2]:.1f}), 20 상향 돌파")
    b240 = bars_240(hourly)
    if len(b240) < 14:
        checks['stoch240'] = (False, f'240분봉 {len(b240)}개 (14개 필요)')
    else:
        f, k, d = stoch([(b[2], b[3], b[4]) for b in b240])
        ups = sum(1 for x in (f, k, d) if x[-1] > x[-2])
        checks['stoch240'] = (ups >= 2, f"240분 스토캐스틱 fast {f[-1]:.0f}·%K {k[-1]:.0f}·%D {d[-1]:.0f}, 상승 {ups}/3")
    t, y = daily[-1], daily[-2]
    prev_chg = (y.close / daily[-3].close - 1) * 100 if daily[-3].close else 0.0
    checks['prev'] = (prev_chg <= 6.65, f"전날 등락 {prev_chg:+.2f}% (6.65% 초과면 제외)")
    pos = (t.close - t.low) / (t.high - t.low) * 100 if t.high > t.low else 50.0
    checks['high'] = (pos <= 81.4, f"오늘 범위 위치 {pos:.0f}% (81.4% 넘으면 고가 근처 제외)")
    return {'pass': all(ok for ok, _ in checks.values()), 'checks': checks, 'prev_chg': round(prev_chg, 2), 'pos': round(pos, 1)}


# ── 사이징 ───────────────────────────────────────────────────
def late_amount(five_min, today):
    """15:00~15:20 거래대금 근사: 오늘 15:00 이후 5분봉 종가×거래량 합 (원)."""
    return sum(c * v for stamp, o, h, l, c, v in five_min if stamp[:8] == today and stamp[8:12] >= '1500')


def plan_quantity(ask, ask_qty, bid_qty, per_trade, remaining_cap, late_amt, cap_qty):
    """매수 수량과 이유. 0 이면 못 산다."""
    if ask <= 0:
        return 0, '매도1호가 없음'
    budget = min(per_trade, remaining_cap)
    if late_amt > 0:
        budget = min(budget, late_amt * 0.10)
    qty = int(budget // ask)
    depth = min(ask_qty, bid_qty)
    qty = min(qty, depth, cap_qty)
    if qty <= 0:
        return 0, f'예산 {budget:,.0f}원 · 호가잔량 {depth:,}주 · 주문가능 {cap_qty:,}주 → 1주도 못 삼'
    return qty, f'{qty:,}주 × {ask:,}원 = {qty * ask:,}원 (예산 {budget:,.0f}원, 잔량 {depth:,}주)'


def in_window(now: datetime, window):
    return window[0] <= now.time() < window[1]


def normalize_settings(raw):
    s = DEFAULT_SETTINGS | {k: raw[k] for k in DEFAULT_SETTINGS if k in raw}
    if s['mode'] not in ('dry', 'live'):
        raise ValueError('mode 는 dry 또는 live')
    s['enabled'] = bool(s['enabled'])
    for key in ('per_trade_amount', 'max_positions', 'total_cap', 'threshold_eok'):
        if type(s[key]) is not int or s[key] <= 0:
            raise ValueError(f'{key} 는 양의 정수')
    if s['total_cap'] > TOTAL_CAP_MAX:
        raise ValueError(f'총 투입 상한은 {TOTAL_CAP_MAX:,}원 이하 (1,000만 원 미만)')
    if s['max_positions'] > MAX_POSITIONS_MAX:
        raise ValueError(f'종목 수는 최대 {MAX_POSITIONS_MAX}')
    if s['per_trade_amount'] > s['total_cap']:
        raise ValueError('한 종목 금액이 총 투입 상한보다 큽니다')
    try:
        t = datetime.strptime(s['sell_time'], '%H:%M').time()
    except ValueError:
        raise ValueError('sell_time 은 HH:MM')
    if not SELL_WINDOW[0] <= t < SELL_WINDOW[1]:
        raise ValueError('매도 시각은 08:30~08:59 (장전 동시호가)')
    return s


# ── 엔진 ─────────────────────────────────────────────────────
class CloseBet(AutoExit):
    """plans 는 쓰지 않는다. positions = 종배로 산 보유 {code: {...}}, scans = 날짜별 후보 판정, settings."""

    def __init__(self, path, env, broker, connected, phase, monitor, clock, peers=(), notify=None):
        super().__init__(path, env, broker, connected, phase, monitor, clock)
        self.peers, self.notify = peers, notify or (lambda text: None)
        self.settings = dict(DEFAULT_SETTINGS)
        self.positions, self.scans, self.days = {}, {}, {}
        self.scan_task = None

    # 저장: AutoExit 의 plans/orders 에 settings/positions/scans/days 를 더한다
    def open(self):
        extra = {}
        if self.path.exists():
            try:
                extra = json.loads(self.path.read_text('utf-8'))
            except (OSError, ValueError):
                extra = {}
        super().open()
        if self.owner and not self.error:
            self.settings = normalize_settings(extra.get('settings') or {})
            self.positions, self.scans, self.days = extra.get('positions') or {}, extra.get('scans') or {}, extra.get('days') or {}
            self.scans = dict(sorted(self.scans.items())[-10:])
            self.save()

    def save(self):
        if not self.owner:
            raise RuntimeError('종가배팅 실행 잠금을 확보하지 못했습니다')
        import os
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'environment': self.env, 'plans': self.plans, 'orders': self.orders,
                       'settings': self.settings, 'positions': self.positions, 'scans': self.scans, 'days': self.days},
                      f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def update_settings(self, raw):
        s = normalize_settings(raw)
        if s['mode'] == 'live' and self.env != 'real':
            raise ValueError('실전 전송(live)은 실전 계좌 환경(KIWOOM_ENV=real)에서만 켤 수 있습니다')
        self.settings = s
        self.persist()
        return s

    def stop(self, note='사용자가 비상 정지를 눌렀습니다'):
        """새 전송을 모두 막는다. 이미 접수된 주문은 취소하지 않는다. 보유 종목은 남는다 (다시 켜면 다음 매도 시각에 판다)."""
        self.epoch += 1
        self.settings['enabled'] = False
        self.days.setdefault(self.today(), {})['stopped'] = note
        if self.owner and not self.error:
            self.persist()

    def today(self):
        return self.clock().date().isoformat()

    def day(self):
        return self.days.setdefault(self.today(), {})

    def live(self):
        return self.settings['mode'] == 'live'

    def invested(self):
        return sum(p['quantity'] * p['average'] for p in self.positions.values() if p['status'] in ('held', 'selling') and not p.get('dry'))

    def held_codes(self):
        return [c for c, p in self.positions.items() if p['status'] in ('held', 'selling')]

    def peer_busy(self, code):
        for peer in self.peers:
            if peer.plans.get(code, {}).get('status') in ('prepared', 'armed', 'checking', 'ordered', 'remainder_check') or peer.blocked(code):
                return True
        return False

    def guard_live(self, epoch):
        if not self.live():
            return
        self.gate()
        if epoch != self.epoch or not self.settings['enabled']:
            raise NotSent('종가배팅이 꺼졌습니다')

    # ── 1초마다 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5 or not self.owner or self.error:
            return
        d = self.day()
        if in_window(now, SCAN_WINDOW) and not d.get('scanned') and not self.scan_task:
            self.scan_task = asyncio.create_task(self.scan(), name='closebet-scan')
            self.tasks.add(self.scan_task)
            self.scan_task.add_done_callback(self.tasks.discard)
        if in_window(now, BUY_WINDOW) and d.get('scanned') and not d.get('bought') and self.settings['enabled']:
            d['bought'] = now.isoformat()
            self.persist()
            await self.buy_all(now)
        if in_window(now, (datetime.strptime(self.settings['sell_time'], '%H:%M').time(), SELL_WINDOW[1])) and not d.get('sold') and self.held_codes():
            d['sold'] = now.isoformat()
            self.persist()
            await self.sell_all(now, 'OPEN_AUCTION')
        if in_window(now, RETRY_WINDOW) and not d.get('retried') and self.held_codes():
            d['retried'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.sell_all(now, 'OPEN_RETRY', only_without_pending=True)
        if in_window(now, FILL_WINDOW) and d.get('bought') and not d.get('filled'):
            d['filled'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.settle_buys()

    # ── 15:15 후보 선정 ──
    async def scan(self, preview=False):
        today = self.today()
        at = self.clock()
        result = {'at': at.isoformat(), 'preview': preview, 'threshold_eok': self.settings['threshold_eok'], 'rows': [], 'error': ''}
        try:
            broker = self.broker()
            rows = []
            for market in ('001', '101'):
                rows += await broker.ranking(market)
            rows = [r for r in rows if r['amount_eok'] >= self.settings['threshold_eok'] and not any(k in r['name'] for k in SKIP_NAME)]
            rows.sort(key=lambda r: -r['amount_eok'])
            ymd = None if preview else today.replace('-', '')
            for r in rows:
                try:
                    daily = await broker.daily(r['code'])
                    ev = evaluate(r, daily, [], self.settings['threshold_eok'], ymd)
                    core = [k for k in ('macd', 'stoch', 'prev', 'high') if k in ev['checks']]
                    if ev['checks'].get('data', (True,))[0] and all(ev['checks'][k][0] for k in core):
                        hourly = await broker.minutes(r['code'], '60')     # 일봉 조건을 다 넘은 종목만 240분봉을 받는다
                        ev = evaluate(r, daily, hourly, self.settings['threshold_eok'], ymd)
                    r = r | {'pass': ev['pass'], 'checks': {k: list(v) for k, v in ev['checks'].items()}}
                except Exception as exc:
                    r = r | {'pass': False, 'checks': {'error': [False, f'조회 실패: {str(exc)[:80]}']}}
                result['rows'].append(r)
            result['passed'] = [r['code'] for r in result['rows'] if r['pass']]
        except Exception as exc:
            result['error'] = f'후보 선정 실패: {str(exc)[:120]}'
        if not preview:
            self.scans[today] = result
            self.day()['scanned'] = at.isoformat()
            self.scans = dict(sorted(self.scans.items())[-10:])
            self.persist()
            passed = [r for r in result['rows'] if r['pass']]
            self.notify('🌙 종배 15:15 후보 ' + (f"{len(passed)}종목: " + ', '.join(f"{r['name']}({r['amount_eok']:,.0f}억)" for r in passed[:5])
                                             if passed else f"없음 (문턱 {self.settings['threshold_eok']}억 넘는 {len(result['rows'])}종목 중 조건 통과 0)")
                        + ('' if self.settings['enabled'] else ' · 자동매매 꺼져 있어 매수 안 함') + ('' if self.live() else ' · 흉내 모드'))
        self.scan_task = None
        return result

    # ── 15:19:50 매수 ──
    async def buy_all(self, now):
        scan = self.scans.get(self.today()) or {}
        picks = [r for r in scan.get('rows', []) if r.get('pass')]
        room = self.settings['max_positions'] - len(self.held_codes())
        bought = []
        for r in picks:
            if room <= 0:
                break
            if r['code'] in self.positions and self.positions[r['code']]['status'] in ('held', 'selling'):
                continue
            if self.peer_busy(r['code']):
                self.day().setdefault('skipped', []).append(f"{r['name']}: 다른 자동매매에 걸린 종목")
                continue
            ok = await self.buy_one(r, now)
            if ok:
                room -= 1
                bought.append(r['name'])
        if self.owner and not self.error:
            self.persist()
        mode = '실전' if self.live() else '흉내'
        self.notify(f"🌙 종배 매수({mode}) " + (', '.join(bought) if bought else '없음') + (f" · 건너뜀 {len(self.day().get('skipped', []))}" if self.day().get('skipped') else ''))

    async def buy_one(self, r, now):
        code, epoch = r['code'], self.epoch
        order = None
        try:
            async with self.lock:
                self.guard_live(epoch)
                broker = self.broker()
                book = await broker.book(code)
                five = await broker.minutes(code, '5')
                remaining = self.settings['total_cap'] - self.invested()
                cap_qty = 10 ** 8
                if self.live():
                    broker_stock = await broker.stock(code, book['ask'] or 1)   # 거래정지·투자유의·호가단위 확인
                    cap_qty = (await broker.capacity(code, book['ask']))['quantity'] if book['ask'] else 0
                    if code in (await broker.positions()):
                        raise NotSent('이미 계좌에 있는 종목 (종배 외 보유) · 섞이지 않게 건너뜀')
                qty, why = plan_quantity(book['ask'], book['ask_qty'], book['bid_qty'], self.settings['per_trade_amount'], remaining,
                                         late_amount(five, self.today().replace('-', '')), cap_qty)
                if qty <= 0:
                    raise NotSent(why)
                self.guard_live(epoch)
                order = {'id': secrets.token_hex(12), 'side': 'buy', 'code': code, 'name': r['name'], 'quantity': qty, 'limit_price': book['ask'],
                         'reason': 'CLOSE_BET', 'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(),
                         'status': 'submitting', 'order_no': '', 'filled': 0, 'remaining': qty, 'note': why, 'checked_at': None,
                         'dry': not self.live()}
                self.orders.append(order)
                self.persist()
                if self.live():
                    order['order_no'] = await broker.buy_once(code, qty, book['ask'], lambda: self.guard_live(epoch))
                    order['status'], order['note'] = 'accepted', '지정가 매수 접수 · 15:31 체결 확인'
                else:
                    order['status'], order['note'], order['filled'], order['remaining'] = 'dry', '흉내: 전송하지 않음 · 매도1호가에 전량 체결로 가정', qty, 0
                    self.positions[code] = {'code': code, 'name': r['name'], 'quantity': qty, 'average': book['ask'], 'bought_on': now.date().isoformat(),
                                            'status': 'held', 'dry': True, 'buy_order': order['id']}
                self.persist()
                return True
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            self.day().setdefault('skipped', []).append(f"{r['name']}: {exc}")
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
        except (Exception, asyncio.CancelledError) as exc:
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 주문내역 확인'
            else:
                self.day().setdefault('skipped', []).append(f"{r['name']}: 조회 실패 {str(exc)[:60]}")
        return False

    # ── 15:31 체결 → 보유 ──
    async def settle_buys(self):
        if not self.live():
            return
        try:
            account = await self.broker().positions()
        except Exception:
            account = {}
        for o in self.orders:
            if o['side'] != 'buy' or o.get('dry') or o['day'] != self.today() or o['status'] not in ('filled', 'partial', 'closed', 'accepted', 'attention'):
                continue
            if o['filled'] <= 0 or o['code'] in self.positions and self.positions[o['code']]['status'] in ('held', 'selling'):
                continue
            row = account.get(o['code'])
            qty = min(o['filled'], row['quantity']) if row else o['filled']
            avg = row['average'] if row else o['limit_price']
            self.positions[o['code']] = {'code': o['code'], 'name': o['name'], 'quantity': qty, 'average': avg, 'bought_on': o['day'],
                                         'status': 'held', 'dry': False, 'buy_order': o['id'],
                                         'note': '' if row else '계좌 잔고에 아직 안 보임 · 주문 체결수량 기준'}
        self.persist()
        held = [p for p in self.positions.values() if p['status'] == 'held' and p['bought_on'] == self.today()]
        if held:
            self.notify('🌙 종배 체결 보유: ' + ', '.join(f"{p['name']} {p['quantity']}주@{p['average']:,.0f}" for p in held)
                        + f" · 내일 {self.settings['sell_time']} 시장가 매도 예정")

    # ── 다음 날 매도 ──
    async def sell_all(self, now, reason, only_without_pending=False):
        sold = []
        for code in list(self.held_codes()):
            p = self.positions[code]
            if only_without_pending and (self.blocked(code) or p['status'] != 'held'):
                continue
            if await self.sell_one(p, now, reason):
                sold.append(p['name'])
        if self.owner and not self.error:
            self.persist()
        if sold:
            self.notify(f"🌙 종배 매도 주문({'실전' if self.live() else '흉내'}, {reason}): " + ', '.join(sold))

    async def sell_one(self, p, now, reason):
        code, epoch = p['code'], self.epoch
        order = None
        try:
            async with self.lock:
                if p.get('dry'):
                    order = {'id': secrets.token_hex(12), 'side': 'sell', 'code': code, 'name': p['name'], 'quantity': p['quantity'], 'reason': reason,
                             'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(), 'status': 'dry', 'order_no': '',
                             'filled': p['quantity'], 'remaining': 0, 'note': '흉내: 09:00 시초가 매도로 가정 · 실제 시초가는 화면에서 확인', 'checked_at': None, 'dry': True}
                    self.orders.append(order)
                    p['status'], p['sold_on'] = 'sold', now.date().isoformat()
                    self.persist()
                    return True
                self.guard_live(epoch)
                if self.blocked(code):
                    raise NotSent('이 종목에 미확정 주문이 있음 · 재전송 차단')
                if self.peer_busy(code):
                    raise NotSent('다른 자동매매에 걸린 종목 · 종배가 팔지 않음')
                if reason == 'OPEN_RETRY' and (self.phase() or {}).get('value') != '3':
                    raise NotSent('장중 상태 확인 안 됨 · 재시도 보류')
                broker = self.broker()
                row = (await broker.positions()).get(code)
                if not row or row['quantity'] <= 0:
                    p['status'], p['note'] = 'sold', '계좌에 잔고 없음 (이미 매도됨)'
                    self.persist()
                    return False
                qty = min(p['quantity'], row['available'])
                if qty <= 0:
                    raise NotSent('주문가능 수량 0')
                self.guard_live(epoch)
                order = {'id': secrets.token_hex(12), 'side': 'sell', 'code': code, 'name': p['name'], 'quantity': qty, 'reason': reason,
                         'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(), 'status': 'submitting', 'order_no': '',
                         'filled': 0, 'remaining': qty, 'note': '시장가 매도 전송 중', 'checked_at': None, 'dry': False}
                self.orders.append(order)
                self.persist()
                order['order_no'] = await broker.sell_market_once(code, qty, lambda: self.guard_live(epoch))
                order['status'], order['note'] = 'accepted', '시장가 매도 접수 · 체결 확인 중'
                p['status'], p['sell_order'] = 'selling', order['id']
                self.persist()
                return True
        except NotSent as exc:
            if order:
                order['status'], order['note'] = 'not_sent', str(exc)
            p['note'] = f'매도 보류: {exc}'
        except OrderRejected as exc:
            order['status'], order['note'] = 'rejected', str(exc)
            p['note'] = f'매도 거부: {exc}'
        except (Exception, asyncio.CancelledError):
            if order:
                order['status'], order['note'] = 'unknown', '전송/저장 결과 불명 · 재전송 차단 · 키움 주문내역 확인'
            p['note'] = '매도 결과 불명 · 키움 확인 필요'
        return False

    async def _reconcile(self):
        await super()._reconcile()
        for o in self.orders:
            if o['side'] != 'sell' or o.get('dry'):
                continue
            p = self.positions.get(o['code'])
            if p and p.get('sell_order') == o['id'] and p['status'] == 'selling':
                if o['status'] == 'filled':
                    p['status'], p['sold_on'], p['note'] = 'sold', o['day'], '전량 매도 체결'
                elif o['status'] in ('closed', 'rejected', 'not_sent'):
                    p['status'], p['note'] = 'held', f"매도 주문 종료({o['status']}) · 잔량 보유 중"
        if self.owner and not self.error:
            self.persist()

    def snapshot(self):
        at = self.clock()
        today = self.today()
        return {'environment': self.env, 'settings': self.settings, 'live': self.live(), 'enabled': self.settings['enabled'],
                'connected': bool(self.connected()), 'error': self.error, 'asof': at.isoformat(),
                'invested': self.invested(), 'positions': list(self.positions.values()),
                'day': self.days.get(today, {}), 'scan': self.scans.get(today), 'scan_dates': sorted(self.scans),
                'orders': self.orders[-100:], 'scanning': self.scan_task is not None,
                'windows': {'scan': '15:15', 'buy': '15:19:50', 'sell': self.settings['sell_time'], 'retry': '09:03', 'fill': '15:31'}}
