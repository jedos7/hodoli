"""차단기 — 변성환 프로그램의 안전장치 목록 그대로.

하나라도 걸리면 **새 매수만** 멈춘다. **매도와 미체결 취소는 막지 않는다**
(산 것을 못 팔거나 미체결 매수를 밤새 남기는 쪽이 더 위험하다).
네 매매법(🌅 🗻 🔥 🌙)이 차단기 하나를 같이 쓴다. 계좌·환경마다 파일 하나.

  멈춤(그날 전체)
    · 예탁자산이 **아침 첫 조회 대비 -12%** 이하
    · 주문 **거부 6건**
  그 주문만 거절
    · 자동 매수 하루 **15건** · 한 종목 **5건** · 한 번 **3억** 초과
    · **같은 종목 되풀이**: 그날 팔았던 종목은 다시 사지 않는다
    · 한 종목 **매도 10분에 8건** 넘으면 그 종목 매도를 잠시 막는다 (고장이 되풀이될 때만 걸린다)

사용자가 화면에서 **해제**하면 그 차단기는 그날 다시 걸리지 않는다 (해제 기록은 남는다).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.kiwoom.orders import NotSent

log = logging.getLogger(__name__)

EQUITY_DROP = -12.0          # 예탁자산 아침 대비 (%)
REJECT_MAX = 6               # 하루 주문 거부 건수
BUY_MAX_DAY = 15             # 하루 자동 매수 건수
BUY_MAX_CODE = 5             # 한 종목 하루 매수 건수
BUY_MAX_AMOUNT = 300_000_000  # 한 번 매수 금액
SELL_BURST, SELL_WINDOW_MIN = 8, 10   # 한 종목 매도 N건 / M분
POLL_SECONDS = 60
WATCH = (time(8, 30), time(15, 40))   # 예탁자산을 지켜보는 시간
TRIP_NAMES = ('equity', 'rejects')    # 멈춤을 일으키는 차단기
LABELS = {'equity': '예탁자산 차단기', 'rejects': '주문 거부 차단기'}


def watching(now):
    return now.weekday() < 5 and WATCH[0] <= now.time() <= WATCH[1]


class Breakers:
    def __init__(self, path, env, get_broker, clock, notify=None):
        self.path, self.env = path, env
        self.get_broker, self.clock = get_broker, clock
        self.notify = notify or (lambda text: None)
        self.days = {}
        self.polled_at = None
        self.refreshing = asyncio.Lock()
        self.error = ''

    # ── 저장 ──
    def open(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text('utf-8'))
                if d.get('environment') == self.env:
                    self.days = d.get('days') or {}
        except (OSError, ValueError):
            self.error = '차단기 기록을 읽지 못했습니다 (새로 시작합니다)'
            self.days = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.days = dict(sorted(self.days.items())[-10:])
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'environment': self.env, 'days': self.days}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def today(self):
        return self.clock().date().isoformat()

    def day(self):
        return self.days.setdefault(self.today(), {'buys': [], 'sells': [], 'rejects': [],
                                                   'tripped': [], 'released': []})

    # ── 상태 ──
    def tripped(self):
        """걸린 차단기 사유 (없으면 '')."""
        return ' · '.join(t['why'] for t in self.day()['tripped']) or ''

    def trip(self, name, why):
        d = self.day()
        if name in d['released'] or any(t['name'] == name for t in d['tripped']):
            return
        d['tripped'].append({'name': name, 'why': why, 'at': self.clock().isoformat()})
        self.save()
        log.warning('차단기 걸림 (%s): %s', name, why)
        self.notify(f'🛑 {LABELS.get(name, name)} 걸림 · {why} · **새 매수만** 멈춥니다 (매도는 그대로)')

    def release(self, note='사용자가 해제했습니다'):
        """걸린 차단기를 사용자가 푼다. 푼 차단기는 그날 다시 걸리지 않는다."""
        d = self.day()
        freed = [t['name'] for t in d['tripped']]
        for name in freed:
            if name not in d['released']:
                d['released'].append(name)
        d['tripped'] = []
        if freed:
            d.setdefault('release_log', []).append({'at': self.clock().isoformat(), 'names': freed, 'note': note})
        self.save()
        return freed

    # ── 매수 ──
    def check_buy(self, code=None, amount=0):
        """새 매수 전송 직전. 걸리면 NotSent (그 종목만 건너뛴다)."""
        why = self.tripped()
        if why:
            raise NotSent(f'차단기: {why} · 새 매수를 멈췄습니다')
        d = self.day()
        if len(d['buys']) >= BUY_MAX_DAY:
            raise NotSent(f'차단기: 오늘 자동 매수 {len(d["buys"])}건 (하루 {BUY_MAX_DAY}건까지)')
        if amount > BUY_MAX_AMOUNT:
            raise NotSent(f'차단기: 한 번 매수 {amount:,}원 (한 번 {BUY_MAX_AMOUNT:,}원까지)')
        if code:
            mine = [b for b in d['buys'] if b['code'] == code]
            if len(mine) >= BUY_MAX_CODE:
                raise NotSent(f'차단기: 이 종목 오늘 매수 {len(mine)}건 (한 종목 {BUY_MAX_CODE}건까지)')
            if any(s['code'] == code for s in d['sells']):
                raise NotSent('차단기: 오늘 팔았던 종목입니다 (같은 종목 되풀이 매매 금지)')

    def record_buy(self, code, amount, label=''):
        self.day()['buys'].append({'at': self.clock().isoformat(), 'code': code, 'amount': int(amount), 'by': label})
        self.save()

    # ── 매도 (막지 않는다. 고장 되풀이만 막는다) ──
    def check_sell(self, code):
        """매도 전송 직전. 같은 종목을 10분에 8번 넘게 팔려 할 때만 막는다 (고장 되풀이 방지)."""
        since = self.clock() - timedelta(minutes=SELL_WINDOW_MIN)
        recent = [s for s in self.day()['sells'] if s['code'] == code and _at(s['at']) >= since]
        if len(recent) >= SELL_BURST:
            raise NotSent(f'차단기: 이 종목 매도를 {SELL_WINDOW_MIN}분에 {len(recent)}번 보냈습니다 · '
                          '키움 주문내역을 확인하세요')

    def record_sell(self, code, label=''):
        self.day()['sells'].append({'at': self.clock().isoformat(), 'code': code, 'by': label})
        self.save()

    # ── 주문 거부 ──
    def record_reject(self, code, why, label=''):
        d = self.day()
        d['rejects'].append({'at': self.clock().isoformat(), 'code': code, 'why': str(why)[:120], 'by': label})
        self.save()
        if len(d['rejects']) >= REJECT_MAX:
            self.trip('rejects', f'주문 거부 {len(d["rejects"])}건 (한도 {REJECT_MAX}건)')

    # ── 예탁자산 ──
    def feed_equity(self, value, at=None):
        """예탁자산(원)을 넣는다. 그날 첫 값이 기준(아침)이 된다."""
        at = at or self.clock()
        d = self.day()
        if value <= 0:
            return
        if not d.get('base_equity'):
            d['base_equity'] = int(value)
            d['base_at'] = at.isoformat()
            d['base_late'] = at.time() > time(9, 30)     # 서버가 늦게 떠서 아침 기준을 못 잡았을 수 있다
        d['equity'] = int(value)
        d['equity_at'] = at.isoformat()
        drop = (value / d['base_equity'] - 1) * 100
        d['drop'] = round(drop, 2)
        self.save()
        if drop <= EQUITY_DROP:
            self.trip('equity', f'예탁자산 {d["base_equity"]:,}원 → {int(value):,}원 ({drop:+.1f}%, 한도 {EQUITY_DROP:+.0f}%)')

    async def refresh(self, force=False):
        """예탁자산을 다시 읽는다 (08:30~15:40 평일, 1분에 한 번)."""
        now = self.clock()
        if not force and (not watching(now) or
                          (self.polled_at and (now - self.polled_at).total_seconds() < POLL_SECONDS)):
            return
        if self.refreshing.locked():
            return
        async with self.refreshing:
            self.polled_at = now
            try:
                value = await self.get_broker().equity()
            except Exception as exc:
                self.day()['equity_error'] = str(exc)[:100]
                return
            self.day().pop('equity_error', None)
            self.feed_equity(value, now)

    async def loop(self):
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception('차단기 예탁자산 조회')
            await asyncio.sleep(10)

    # ── 화면 ──
    def snapshot(self):
        d = self.day()
        return {'on': True, 'day': self.today(), 'tripped': d['tripped'], 'why': self.tripped(),
                'released': d['released'], 'error': self.error or d.get('equity_error', ''),
                'equity': d.get('equity'), 'baseEquity': d.get('base_equity'), 'baseAt': d.get('base_at'),
                'baseLate': d.get('base_late', False), 'drop': d.get('drop'), 'equityAt': d.get('equity_at'),
                'buys': len(d['buys']), 'sells': len(d['sells']), 'rejects': len(d['rejects']),
                'limits': {'equityDrop': EQUITY_DROP, 'rejectMax': REJECT_MAX, 'buyMaxDay': BUY_MAX_DAY,
                           'buyMaxCode': BUY_MAX_CODE, 'buyMaxAmount': BUY_MAX_AMOUNT,
                           'sellBurst': SELL_BURST, 'sellWindowMin': SELL_WINDOW_MIN}}


def _at(stamp):
    return datetime.fromisoformat(stamp)


class NoBreakers:
    """차단기를 안 쓰는 경우 (시험·다른 증권사). 아무것도 막지 않는다."""

    def tripped(self):
        return ''

    def check_buy(self, code=None, amount=0):
        pass

    def record_buy(self, code, amount, label=''):
        pass

    def check_sell(self, code):
        pass

    def record_sell(self, code, label=''):
        pass

    def record_reject(self, code, why, label=''):
        pass

    def snapshot(self):
        return {'on': False}


NO_BREAKERS = NoBreakers()


# ── 만들기와 API ──────────────────────────────────────────────
def create_breakers(settings, get_broker, clock, notify=None):
    """계좌·환경마다 차단기 하나. 키움이 아니면 아무것도 막지 않는 NoBreakers 를 준다."""
    if settings.broker != 'kiwoom':
        return NO_BREAKERS
    import hashlib
    identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
    b = Breakers(settings.data_dir / f'breakers_{identity}.json', settings.kiwoom_env, get_broker, clock, notify)
    b.open()
    return b


def create_breakers_router(breakers):
    """GET /api/breakers 상태 · POST /api/breakers/release 해제. 로컬 주소·같은 출처·화면 토큰만."""
    import secrets as _secrets

    router = APIRouter(prefix='/api/breakers')
    token = getattr(breakers, 'token', None) or _secrets.token_urlsafe(32)
    breakers.token = token

    def local(request: Request):
        if (request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver')):
            raise HTTPException(403, '이 PC의 로컬 주소에서만 볼 수 있습니다')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    def mutate(request: Request, _: None = Depends(local)):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or not _secrets.compare_digest(request.headers.get('x-breakers-token', ''), token)):
            raise HTTPException(403, '화면을 다시 열어주세요')

    @router.get('', dependencies=[Depends(local)])
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return breakers.snapshot() | {'token': token}

    @router.post('/release', dependencies=[Depends(mutate)])
    async def release():
        if not hasattr(breakers, 'release'):
            raise HTTPException(409, '차단기를 쓰지 않는 설정입니다')
        freed = breakers.release()
        if not freed:
            raise HTTPException(409, '걸린 차단기가 없습니다')
        return breakers.snapshot()

    @router.post('/refresh', dependencies=[Depends(mutate)])
    async def refresh():
        if not hasattr(breakers, 'refresh'):
            raise HTTPException(409, '차단기를 쓰지 않는 설정입니다')
        await breakers.refresh(force=True)
        return breakers.snapshot()

    return router
