"""종가배팅 자동매매 (변성환 규칙). 15:15 후보 선정 → 15:19:50 매도1호가 지정가 매수 → 다음 거래일 08:40 시장가 매도(09:00 시초가 체결).

규칙 (제공 자료 그대로. 원본 수식이 없는 부분은 아래에 명시)
  후보 (2026-10-19 받은 규칙 문서 그대로):
      · 15:15 까지 정규장 거래대금 ≥ 문턱(기본 300억) · 주가 1,000원↑ · 상한가 근처(+29.5%↑) 제외
      · 일봉 MACD(12,26,9): 최근 10일 안에 시그널 상향 돌파 + 오늘 MACD > 시그널 + 어제보다 오름
      · 일봉 슬로우 스토캐스틱 (5,3,3)·(10,6,6)·(20,12,12) 셋 다: 최근 10일 안에 20 이하였다가 지금 20 위 + 어제보다 오름
      · 240분봉(09:00~12:59 / 13:00~15:30) 같은 스토캐스틱 셋 중 둘 이상이 앞 봉보다 상승
      · 전날 등락 +6.65% 초과 종목 제외
      · 15:15 장중 위치 (현재가−저가)/(고가−저가) > 81.4% 면 제외
  매수: 15:19:50 에 매도1호가 +2틱 지정가, 대금 큰 순. 수량 ≤ 호가 총잔량, 금액 ≤ 설정 금액·2억·15:00~15:20 거래대금의 10%, 총액 ≤ 상한(1,000만 원 미만)
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
from time import monotonic

from app.autoexit import PENDING, AutoExit
from app.autosell import normal_code
from app.breakers import NO_BREAKERS
from app.fanout import fanout
from app.kiwoom.extended_sell import tick
from app.kiwoom.orders import NotSent, OrderRejected

TOTAL_CAP_MAX = 9_990_000       # 사용자 요구: 총 투입 1,000만 원 미만
PER_STOCK_MAX = 200_000_000     # 규칙 문서: 한 종목 최대 2억
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


STOCH_SETS = ((5, 3, 3), (10, 6, 6), (20, 12, 12))   # 슬로우 스토캐스틱 세 가지 (규칙 문서 그대로)


def stoch_k(bars, n, k):
    """슬로우 %K 수열. bars = [(high, low, close)]. fast %K(n) 를 k 로 평활한 값."""
    fast = []
    for i in range(len(bars)):
        w = bars[max(0, i - n + 1):i + 1]
        hi, lo = max(b[0] for b in w), min(b[1] for b in w)
        fast.append((bars[i][2] - lo) / (hi - lo) * 100 if hi > lo else 50.0)
    return [mean(fast[max(0, i - k + 1):i + 1]) for i in range(len(fast))]


def bars_240(hourly):
    """키움 60분봉 [(stamp, o, h, l, c, v)] → 240분봉 [(day, o, h, l, c)] (09:00~12:59 / 13:00~15:30)."""
    groups = {}
    for stamp, o, h, l, c, v in hourly:
        key = (stamp[:8], 0 if stamp[8:10] < '13' else 1)
        g = groups.setdefault(key, [o, h, l, c])
        g[1], g[2], g[3] = max(g[1], h), min(g[2], l), c
    return [(k[0], *groups[k]) for k in sorted(groups)]


def crossed_within(a, b, lookback=10):
    """최근 lookback 봉 안에 a 가 b 를 위로 돌파한 적이 있나 (오늘 돌파가 아니어도 됨)."""
    for j in range(max(1, len(a) - lookback), len(a)):
        if a[j] > b[j] and a[j - 1] <= b[j - 1]:
            return True
    return False


def turned_up_from_low(series, level=20, lookback=10):
    """최근 lookback 봉 안에 level 이하였다가, 지금은 level 위이고, 앞 봉보다 올랐나."""
    if len(series) < 2:
        return False
    return min(series[-(lookback + 1):]) <= level and series[-1] > level and series[-1] > series[-2]


def evaluate(row, daily, hourly, threshold_eok, today):
    """후보 한 종목의 조건 판정 (규칙 문서 '🌙 종가배팅' 그대로).
    row = 거래대금 순위 행, daily = 오늘 봉 포함 일봉(Candle), hourly = 60분봉(없으면 None → 240분 조건 보류).
    → {'pass': bool, 'checks': {이름: (통과, 설명)}}"""
    checks = {}
    checks['amount'] = (row['amount_eok'] >= threshold_eok,
                        f"15:15 거래대금 {row['amount_eok']:,.0f}억 (문턱 {threshold_eok:,}억)")
    checks['price'] = (row['price'] >= 1000, f"주가 {row['price']:,}원 (1,000원 미만 제외)")
    checks['limit'] = (row['chg'] < 29.5, f"오늘 등락 {row['chg']:+.2f}% (상한가 근처 +29.5% 이상 제외)")
    if today is None and daily:          # 미리보기(휴장일 등): 마지막 일봉 날짜를 '오늘'로 본다
        today = daily[-1].date
    if not daily or daily[-1].date != today or len(daily) < 50:
        checks['data'] = (False, '오늘 일봉 없음 또는 일봉 50개 미만')
        return {'pass': False, 'checks': checks}

    closes = [c.close for c in daily]
    hlc = [(c.high, c.low, c.close) for c in daily]
    m, s = macd(closes)
    macd_ok = crossed_within(m, s, 10) and m[-1] > s[-1] and m[-1] > m[-2]
    checks['macd'] = (macd_ok, f"MACD {m[-1]:,.0f} / 시그널 {s[-1]:,.0f} · 최근 10일 돌파 {'O' if crossed_within(m, s, 10) else 'X'}"
                               f" · 어제보다 {'오름' if m[-1] > m[-2] else '내림'}")
    ks = [stoch_k(hlc, n, k) for n, k, _ in STOCH_SETS]
    daily_ok = [turned_up_from_low(x, 20, 10) for x in ks]
    checks['stoch'] = (all(daily_ok), '스토캐스틱 ' + ' · '.join(
        f"({n},{k},{d}) {x[-1]:.0f}{'O' if ok else 'X'}" for (n, k, d), x, ok in zip(STOCH_SETS, ks, daily_ok))
        + ' (셋 다 최근 10일 20 이하 → 지금 20 위 · 상승)')

    b240 = bars_240(hourly or [])
    if hourly is None:
        checks['stoch240'] = (False, '일봉 조건을 먼저 통과해야 240분봉을 조회함')
    elif len(b240) < 34:
        checks['stoch240'] = (False, f'240분봉 {len(b240)}개 (34개 필요)')
    else:
        bars = [(b[2], b[3], b[4]) for b in b240]
        ups = [stoch_k(bars, n, k) for n, k, _ in STOCH_SETS]
        rising = [x[-1] > x[-2] for x in ups]
        checks['stoch240'] = (sum(rising) >= 2, '240분 스토캐스틱 ' + ' · '.join(
            f"({n},{k},{d}) {x[-1]:.0f}{'↑' if r else '↓'}" for (n, k, d), x, r in zip(STOCH_SETS, ups, rising))
            + f" · 상승 {sum(rising)}/3 (둘 이상)")

    y = daily[-2]
    prev_chg = (y.close / daily[-3].close - 1) * 100 if daily[-3].close else 0.0
    checks['prev'] = (prev_chg <= 6.65, f"전날 등락 {prev_chg:+.2f}% (6.65% 초과면 제외)")
    t = daily[-1]
    pos = (t.close - t.low) / (t.high - t.low) * 100 if t.high > t.low else 50.0
    checks['high'] = (pos <= 81.4, f"오늘 장중 위치 {pos:.0f}% (81.4% 넘으면 고가 근처 제외)")
    return {'pass': all(ok for ok, _ in checks.values()), 'checks': checks,
            'prev_chg': round(prev_chg, 2), 'pos': round(pos, 1)}


# ── 사이징 ───────────────────────────────────────────────────
def late_amount(five_min, today):
    """15:00~15:20 거래대금 근사: 오늘 15:00 이후 5분봉 종가×거래량 합 (원)."""
    return sum(c * v for stamp, o, h, l, c, v in five_min if stamp[:8] == today and stamp[8:12] >= '1500')


def buy_price(ask):
    """매도1호가 + 2틱 (규칙 문서). 호가단위는 가격대별."""
    if ask <= 0:
        return 0
    return ask + tick(ask) * 2


def plan_quantity(price, total_ask_qty, per_trade, remaining_cap, late_amt, cap_qty):
    """매수 수량과 이유. 0 이면 못 산다.
    예산 = min(한 종목 설정액, 총액 잔여, 15:00~15:20 대금의 10%, 한 종목 2억) · 수량 ≤ 호가 총잔량 · 주문가능 수량"""
    if price <= 0:
        return 0, '매도1호가 없음'
    budget = min(per_trade, remaining_cap, PER_STOCK_MAX)
    if late_amt > 0:
        budget = min(budget, late_amt * 0.10)
    qty = min(int(budget // price), total_ask_qty, cap_qty)
    if qty <= 0:
        return 0, f'예산 {budget:,.0f}원 · 호가 총잔량 {total_ask_qty:,}주 · 주문가능 {cap_qty:,}주 → 1주도 못 삼'
    return qty, f'{qty:,}주 × {price:,}원 = {qty * price:,}원 (예산 {budget:,.0f}원, 호가 총잔량 {total_ask_qty:,}주)'


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
    """종가 매수 → 다음 날 시초가 매도 공통 뼈대. plans 는 쓰지 않는다.
    positions = 이 전략으로 산 보유 {code: {...}}, scans = 날짜별 후보 판정, settings.
    🔥 기준봉 종가(app/firebar.py)와 🗻 대장 눌림목(app/leaderdip.py)이 이 클래스를 물려받아 후보·매수만 바꾼다."""

    label, icon = '종배', '🌙'
    gate_key = 'closebet'          # 🧭 전날 밤 원달러가 올랐으면(원화 약세) 그날은 쉼
    scan_window, buy_window, fill_window = SCAN_WINDOW, BUY_WINDOW, FILL_WINDOW
    sell_end, retry_window = SELL_WINDOW[1], RETRY_WINDOW
    normalizer = staticmethod(normalize_settings)

    def __init__(self, path, env, broker, connected, phase, monitor, clock, peers=(), notify=None,
                 breakers=None, gate=None, sizing=None):
        super().__init__(path, env, broker, connected, phase, monitor, clock)
        self.peers, self.notify = peers, notify or (lambda text: None)
        self.breakers = breakers or NO_BREAKERS        # 차단기 (네 매매법이 같이 쓴다. app/breakers.py)
        self.gate_for = gate                           # 🧭 맥락 문 (app/regime.py) · 없으면 안 막는다
        self.sizing = sizing                           # 💧 시장 대금 배팅 크기 · 없으면 ×1
        self.size_mult = 1.0
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
            self.settings = self.normalizer(extra.get('settings') or {})
            self.positions, self.scans, self.days = extra.get('positions') or {}, extra.get('scans') or {}, extra.get('days') or {}
            self.scans = dict(sorted(self.scans.items())[-10:])
            self.save()

    def save(self):
        if not self.owner:
            raise RuntimeError(f'{self.label} 실행 잠금을 확보하지 못했습니다')
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
        s = self.normalizer(raw)
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

    def committed(self):
        """보유한 돈 + 아직 보유로 안 잡힌 접수·미확정 매수 주문 금액.
        실전에서는 체결 확인 전까지 보유가 안 잡히므로, 이걸 안 쓰면 같은 묶음 안에서 총액 상한이 뚫린다."""
        spent = self.invested()
        for o in self.orders:
            if o['side'] != 'buy' or o.get('dry') or o['status'] not in PENDING:
                continue
            p = self.positions.get(o['code'])
            if p and p.get('buy_order') == o['id'] and p['status'] in ('held', 'selling'):
                continue
            spent += o['quantity'] * o['limit_price']
        return spent


    def peer_busy(self, code):
        for peer in self.peers:
            if peer.plans.get(code, {}).get('status') in ('prepared', 'armed', 'checking', 'ordered', 'remainder_check') or peer.blocked(code):
                return True
        return False

    async def read_sizing(self):
        """💧 오늘 한 칸에 곱할 배수를 읽어 둔다 (전날 시장 대금 ÷ 앞 20일 평균)."""
        self.size_mult = 1.0
        if not self.sizing:
            return 1.0
        try:
            self.size_mult = float(await self.sizing())
        except Exception:
            self.size_mult = 1.0
        if self.size_mult != 1.0:
            self.day()['sizing'] = f'💧 시장 대금 배팅 크기 ×{self.size_mult:g}'
        return self.size_mult

    def slot_amount(self, base):
        """설정 금액에 💧 배수를 곱하고 🧱 한 종목 상한(아침 예탁자산)으로 자른 한 칸 크기."""
        amount = int(base * self.size_mult)
        cap = self.breakers.stock_cap()
        return min(amount, int(cap)) if cap else amount

    async def resting(self):
        """🧭 맥락 문: 오늘 이 매매법이 쉬어야 하나 (제작자 10-20 규칙). 하루 한 번만 알리고 기록한다."""
        if not self.gate_for or not self.gate_key:
            return False
        skip, why = await self.gate_for(self.gate_key)
        if not skip:
            return False
        if not self.day().get('resting'):
            self.day()['resting'] = why
            if self.owner and not self.error:
                self.persist()
            self.notify(f'{self.icon} {self.label} 오늘은 쉽니다 · {why}')
        return True

    def guard_live(self, epoch):
        """새 **매수** 전송 직전 확인. 자동매매가 꺼져 있으면 사지 않는다."""
        if not self.live():
            return
        self.gate()
        if epoch != self.epoch or not self.settings['enabled']:
            raise NotSent(f'{self.label}이 꺼졌습니다')

    def guard_sell(self, epoch):
        """**매도·취소** 전송 직전 확인. 자동매매를 끄거나 비상 정지해도 막지 않는다.
        이미 산 종목은 다음 날 시초가에 반드시 팔아야 하고, 미체결 매수 취소는 위험을 줄이는 쪽이다.
        연결·저장 상태와 '전송 준비 중 상태 변경' 만 본다."""
        if not self.live():
            return
        self.gate()
        if epoch != self.epoch:
            raise NotSent('전송 준비 중 상태가 바뀌었습니다 · 다시 확인하세요')

    # ── 1초마다 ──
    async def tick(self):
        now = self.clock()
        if now.weekday() >= 5 or not self.owner or self.error:
            return
        d = self.day()
        if in_window(now, self.scan_window) and not d.get('scanned') and not self.scan_task:
            self.scan_task = asyncio.create_task(self.scan(), name=f'{type(self).__name__}-scan')
            self.tasks.add(self.scan_task)
            self.scan_task.add_done_callback(self.tasks.discard)
        if in_window(now, self.buy_window) and d.get('scanned') and not d.get('bought') and self.settings['enabled']:
            d['bought'] = now.isoformat()
            self.persist()
            await self.buy_all(now)
        await self.tick_sell(now, d)

    async def tick_sell(self, now, d):
        """다음 날 아침 매도·재시도와 당일 체결 확인. 하룻밤 전략 셋(🌙 🔥 🗻)이 같이 쓴다."""
        if in_window(now, (datetime.strptime(self.settings['sell_time'], '%H:%M').time(), self.sell_end)) and not d.get('sold') and self.held_codes():
            d['sold'] = now.isoformat()
            self.persist()
            await self.sell_all(now, 'OPEN_AUCTION')
        if in_window(now, self.retry_window) and not d.get('retried') and self.held_codes():
            d['retried'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.sell_all(now, 'OPEN_RETRY', only_without_pending=True)
        if in_window(now, self.fill_window) and d.get('bought') and not d.get('filled'):
            d['filled'] = now.isoformat()
            self.persist()
            await self.reconcile()
            await self.settle_buys()

    def sell_note(self):
        """체결 알림 끝에 붙는 '언제 파는지'. ⚡ 처럼 당일 파는 전략은 이 함수만 바꾼다."""
        return f" · 내일 {self.settings['sell_time']} 시장가 매도 예정"

    async def order_price(self, broker, book, r):
        """매수 지정가. 기본은 매도1호가 +2틱 (🌙 종가배팅 규칙). 다른 전략은 이 함수만 바꾼다."""
        return buy_price(book['ask'])

    async def pre_buy_check(self, broker, r, price):
        """주문 직전 재확인 자리. 기본은 아무것도 안 한다. NotSent 를 올리면 그 종목만 건너뛴다."""
        return

    # ── 15:15 후보 선정 ──
    async def scan(self, preview=False):
        today = self.today()
        at, started = self.clock(), monotonic()
        result = {'at': at.isoformat(), 'preview': preview, 'threshold_eok': self.settings['threshold_eok'], 'rows': [], 'error': ''}
        try:
            broker = self.broker()
            rows = []
            for market in ('001', '101'):
                rows += await broker.ranking(market)
            rows = [r for r in rows if r['amount_eok'] >= self.settings['threshold_eok'] and not any(k in r['name'] for k in SKIP_NAME)]
            rows.sort(key=lambda r: -r['amount_eok'])
            ymd = None if preview else today.replace('-', '')
            th = self.settings['threshold_eok']

            async def judge(r):
                daily = await broker.daily(r['code'])
                ev = evaluate(r, daily, None, th, ymd)
                core = [k for k in ('macd', 'stoch', 'prev', 'high') if k in ev['checks']]
                if ev['checks'].get('data', (True,))[0] and all(ev['checks'][k][0] for k in core):
                    hourly = await broker.minutes(r['code'], '60')     # 일봉 조건을 다 넘은 종목만 240분봉을 받는다
                    ev = evaluate(r, daily, hourly, th, ymd)
                return r | {'pass': ev['pass'], 'checks': {k: list(v) for k, v in ev['checks'].items()}}

            # 종목마다 일봉을 하나씩 기다리면 '호출 간격 + 왕복시간' 이 매번 더해진다. 몇 개를 겹쳐 보낸다.
            for r, judged in zip(rows, await fanout(rows, judge)):
                result['rows'].append(judged if not isinstance(judged, Exception) else
                                      r | {'pass': False, 'checks': {'error': [False, f'조회 실패: {str(judged)[:80]}']}})
            result['passed'] = [r['code'] for r in result['rows'] if r['pass']]
        except Exception as exc:
            result['error'] = f'후보 선정 실패: {str(exc)[:120]}'
        result['took_ms'] = round((monotonic() - started) * 1000)
        if not preview:
            self.scans[today] = result
            self.day()['scanned'] = at.isoformat()
            self.scans = dict(sorted(self.scans.items())[-10:])
            self.persist()
            passed = [r for r in result['rows'] if r['pass']]
            self.notify(f'{self.icon} {self.label} 후보 ' + (f"{len(passed)}종목: " + ', '.join(f"{r['name']}({r['amount_eok']:,.0f}억)" for r in passed[:5])
                                             if passed else f"없음 (문턱 {self.settings['threshold_eok']}억 넘는 {len(result['rows'])}종목 중 조건 통과 0)")
                        + ('' if self.settings['enabled'] else ' · 자동매매 꺼져 있어 매수 안 함') + ('' if self.live() else ' · 흉내 모드'))
        self.scan_task = None
        return result

    # ── 15:19:50 매수 ──
    async def buy_all(self, now):
        if await self.resting():
            return
        await self.read_sizing()
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
        self.notify(f"{self.icon} {self.label} 매수({mode}) " + (', '.join(bought) if bought else '없음') + (f" · 건너뜀 {len(self.day().get('skipped', []))}" if self.day().get('skipped') else ''))

    async def buy_one(self, r, now):
        code, epoch = r['code'], self.epoch
        order = None
        try:
            async with self.lock:
                self.guard_live(epoch)
                self.breakers.check_buy(code)          # 차단기: 멈춤·되풀이·건수 (흉내에서도 본다)
                broker = self.broker()
                book = await broker.book(code)
                five = await broker.minutes(code, '5')
                remaining = self.settings['total_cap'] - self.committed()
                price = await self.order_price(broker, book, r)
                await self.pre_buy_check(broker, r, price)
                cap_qty = 10 ** 8
                if self.live():
                    await broker.stock(code, price or 1)   # 거래정지·투자유의·호가단위 확인
                    cap_qty = (await broker.capacity(code, price))['quantity'] if price else 0
                    if code in (await broker.positions()):
                        raise NotSent(f'이미 계좌에 있는 종목 ({self.label} 외 보유) · 섞이지 않게 건너뜀')
                qty, why = plan_quantity(price, book['total_ask_qty'], self.slot_amount(self.settings['per_trade_amount']), remaining,
                                         late_amount(five, self.today().replace('-', '')), cap_qty)
                if qty <= 0:
                    raise NotSent(why)
                self.guard_live(epoch)
                self.breakers.check_buy(code, qty * price)      # 차단기: 한 번 금액
                order = {'id': secrets.token_hex(12), 'side': 'buy', 'code': code, 'name': r['name'], 'quantity': qty, 'limit_price': price,
                         'reason': 'CLOSE_BET', 'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(),
                         'status': 'submitting', 'order_no': '', 'filled': 0, 'remaining': qty, 'note': why, 'checked_at': None,
                         'dry': not self.live()}
                self.orders.append(order)
                self.persist()
                if self.live():
                    order['order_no'] = await broker.buy_once(code, qty, price, lambda: self.guard_live(epoch))
                    order['status'], order['note'] = 'accepted', '지정가 매수 접수 · 15:31 체결 확인'
                else:
                    order['status'], order['note'], order['filled'], order['remaining'] = 'dry', '흉내: 전송하지 않음 · 매도1호가+2틱에 전량 체결로 가정', qty, 0
                    self.positions[code] = {'code': code, 'name': r['name'], 'quantity': qty, 'average': price, 'bought_on': now.date().isoformat(),
                                            'status': 'held', 'dry': True, 'buy_order': order['id']}
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
            self.notify(f'{self.icon} {self.label} 체결 보유: ' + ', '.join(f"{p['name']} {p['quantity']}주@{p['average']:,.0f}" for p in held)
                        + self.sell_note())

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
            self.notify(f"{self.icon} {self.label} 매도 주문({'실전' if self.live() else '흉내'}, {reason}): " + ', '.join(sold))

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
                self.guard_sell(epoch)
                self.breakers.check_sell(code)          # 고장 되풀이만 막는다 (10분 8건)
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
                self.guard_sell(epoch)
                order = {'id': secrets.token_hex(12), 'side': 'sell', 'code': code, 'name': p['name'], 'quantity': qty, 'reason': reason,
                         'environment': self.env, 'day': now.date().isoformat(), 'at': now.isoformat(), 'status': 'submitting', 'order_no': '',
                         'filled': 0, 'remaining': qty, 'note': '시장가 매도 전송 중', 'checked_at': None, 'dry': False}
                self.orders.append(order)
                self.persist()
                self.breakers.record_sell(code, self.label)
                order['order_no'] = await broker.sell_market_once(code, qty, lambda: self.guard_sell(epoch))
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
            self.breakers.record_reject(code, exc, f'{self.label} 매도')
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
                'breakers': self.breakers.snapshot(),
                'windows': {'scan': '15:15', 'buy': '15:19:50', 'sell': self.settings.get('sell_time', ''), 'retry': '09:03', 'fill': '15:31'}}
