"""오늘 장세 맥락 — 네 매매법이 '좋은 칸'인지 '나쁜 칸'인지 (변성환 2026-10-20 자료).

**표시만 합니다.** 매수 여부는 바꾸지 않습니다. 각 매매 화면 위에 한 줄로 나옵니다.

| 매매법 | 가르는 값 (그날 아침에 아는 값) | 좋은 칸 | 나쁜 칸 |
|---|---|---|---|
| 🗻 대장 눌림목 | 전날 코스피 등락 | −4.9% 아래 · PF 22.5 (25번) | +2.3% 초과 · PF 0.57 |
| 🔥 기준봉 종가 | 전날 밤 S&P 500 | +0.43% 위 · PF 10.8 (71번) | −0.14% 아래 · PF 2.6 |
| 🌙 종가배팅 | 전날 원달러 | −0.25% 아래(원화 강세) · PF 6.4 | 0% 위(원화 약세) · PF 1.0 |
| 🌅 호라이즌존 | — | 맥락을 덜 탑니다 (어느 칸이든 PF 2.8 위) | — |

숫자를 그대로 믿지 마세요. 🗻 의 PF 22.5 는 **25번**이고, 네 맥락은 여러 후보 중 '가장 크게 갈린' 것을
고른 것이라 고르는 행위 자체가 성적을 부풀립니다. 🔥·🌙 는 자료의 앞뒤 반이 같은 방향이었다고 표시돼 있습니다(★).

값은 야후 일봉에서 **오늘 이전 마지막 봉**의 등락을 씁니다 (장중에 바뀌지 않게). 30분 동안 기억합니다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9), 'KST')
YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
NAVER_INDEX = 'https://m.stock.naver.com/api/index/{code}/price'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/128.0 Safari/537.36', 'Accept': 'application/json'}
TTL_SECONDS = 1800

# (키, 아이콘, 이름, 가르는 값, 어디서, 심볼, 좋은 칸, 나쁜 칸, 앞뒤 반 같은 방향)
#   칸은 (문턱, 'below'|'above') — 'below' 면 문턱보다 작을 때 그 칸
#   코스피는 야후를 쓰지 않는다: ^KS11 일봉의 종가가 비어 오는 날이 있어 조용히 옛날 값을 쓰게 된다
#   (2026-10-20 확인: 10-19 봉 close=null → 10-16 값으로 밀림). 네이버 지수 일별 시세를 쓴다.
RULES = (
    ('leaderdip', '🗻', '대장 눌림목', '전날 코스피 등락', 'naver', 'KOSPI',
     (-4.9, 'below', 'PF 22.5 (25번)'), (2.3, 'above', 'PF 0.57'), False),
    ('firebar', '🔥', '기준봉 종가', '전날 밤 S&P 500', 'yahoo', '^GSPC',
     (0.43, 'above', 'PF 10.8 (71번)'), (-0.14, 'below', 'PF 2.6'), True),
    ('closebet', '🌙', '종가배팅', '전날 원달러', 'yahoo', 'KRW=X',
     (-0.25, 'below', 'PF 6.4 · 원화 강세'), (0.0, 'above', 'PF 1.0 · 원화 약세'), True),
)
STEADY = {'key': 'horizonbuy', 'icon': '🌅', 'label': '호라이즌존', 'cell': 'steady',
          'what': '맥락을 덜 탑니다', 'note': '어느 칸이든 PF 2.8 위'}


def edge(spec):
    """'+0.43% 위' · '0% 위' 처럼 사람이 읽는 문턱."""
    sign = f'{spec[0]:+g}' if spec[0] else '0'
    return f"{sign}% {'아래' if spec[1] == 'below' else '위'}"


def side(value, threshold, which):
    return value < threshold if which == 'below' else value > threshold


def cell(value, good, bad):
    """좋은 칸 · 나쁜 칸 · 가운데. 값이 없으면 '미상'."""
    if value is None:
        return 'unknown'
    if side(value, good[0], good[1]):
        return 'good'
    if side(value, bad[0], bad[1]):
        return 'bad'
    return 'mid'


MAX_AGE_DAYS = 5      # 마지막 봉이 이보다 오래되면 조용히 옛 값을 쓰지 않고 '미상'으로 둔다


def fresh(day, today):
    if (today - day).days > MAX_AGE_DAYS:
        raise ValueError(f'마지막 자료가 {day.isoformat()} 로 오래됐습니다')
    return day


async def yahoo_change(client, symbol, today):
    """오늘 이전 마지막 일봉의 등락률(%). 장중에 값이 흔들리지 않게 오늘 봉은 쓰지 않는다."""
    r = await client.get(YAHOO.format(symbol=symbol), params={'range': '1mo', 'interval': '1d'}, headers=HEADERS)
    r.raise_for_status()
    result = (r.json().get('chart') or {}).get('result') or []
    if not result:
        raise ValueError('야후 응답 형식 확인 필요')
    d = result[0]
    stamps = d.get('timestamp') or []
    closes = ((d.get('indicators') or {}).get('quote') or [{}])[0].get('close') or []
    bars = [(datetime.fromtimestamp(t, KST).date(), c) for t, c in zip(stamps, closes) if c]
    bars = [b for b in bars if b[0] < today]
    if len(bars) < 2:
        raise ValueError('일봉이 모자랍니다')
    (day, now), (_, prev) = bars[-1], bars[-2]
    if not prev:
        raise ValueError('전전날 종가 없음')
    return (now / prev - 1) * 100, fresh(day, today).isoformat()


async def naver_index_change(client, code, today):
    """네이버 지수 일별 시세의 '오늘 이전 마지막 날' 등락률(%)."""
    r = await client.get(NAVER_INDEX.format(code=code), params={'pageSize': 10, 'page': 1},
                         headers=HEADERS | {'Referer': 'https://m.stock.naver.com/'})
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError('네이버 지수 응답 형식 확인 필요')
    for row in rows:
        day = date.fromisoformat(str(row.get('localTradedAt', '')))
        if day >= today:
            continue
        return float(str(row['fluctuationsRatio']).replace(',', '')), fresh(day, today).isoformat()
    raise ValueError('오늘 이전 자료가 없습니다')


class Regime:
    def __init__(self, clock):
        self.clock = clock
        self.cached, self.at = None, None
        self.lock = asyncio.Lock()

    async def read(self, force=False):
        now = self.clock()
        if not force and self.cached and self.at and (now - self.at).total_seconds() < TTL_SECONDS \
                and self.cached.get('day') == now.date().isoformat():
            return self.cached
        async with self.lock:
            if not force and self.cached and self.at and (now - self.at).total_seconds() < TTL_SECONDS \
                    and self.cached.get('day') == now.date().isoformat():
                return self.cached
            out = {'day': now.date().isoformat(), 'at': now.isoformat(), 'rows': [], 'error': ''}
            today = now.date()
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    for key, icon, label, what, where, symbol, good, bad, star in RULES:
                        row = {'key': key, 'icon': icon, 'label': label, 'what': what, 'symbol': symbol,
                               'good': good[2], 'bad': bad[2], 'star': star}
                        try:
                            value, day = await (naver_index_change(client, symbol, today) if where == 'naver'
                                                else yahoo_change(client, symbol, today))
                            row |= {'value': round(value, 2), 'valueDay': day, 'cell': cell(value, good, bad),
                                    'goodAt': edge(good), 'badAt': edge(bad)}
                        except Exception as exc:
                            row |= {'value': None, 'cell': 'unknown', 'error': str(exc)[:80]}
                        out['rows'].append(row)
                out['rows'].append(dict(STEADY))
            except Exception as exc:
                out['error'] = f'장세 맥락 조회 실패: {str(exc)[:100]}'
            self.cached, self.at = out, now
            return out


def create_regime_router(regime):
    """GET /api/regime — 읽기만 하므로 토큰 없이 로컬 주소에서만."""
    router = APIRouter()

    @router.get('/api/regime')
    async def read(request: Request, response: Response):
        if (request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver')):
            raise HTTPException(403, '이 PC의 로컬 주소에서만 볼 수 있습니다')
        response.headers['Cache-Control'] = 'no-store'
        return await regime.read()

    return router
