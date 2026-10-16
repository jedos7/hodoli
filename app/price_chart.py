"""Read-only KRX OHLCV charts. Uses the existing quote client's rate limiter."""
from __future__ import annotations

import asyncio
import math
import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.autosell import KST, now_kst

Interval = Literal['1m', '5m', '15m', '1d']


def parse_chart(body, interval, at):
    daily = interval == '1d'
    rows = body.get('stk_dt_pole_chart_qry', body.get('stk_dt_pole_chart')) if daily else body.get('stk_min_pole_chart_qry')
    if not isinstance(rows, list):
        raise ValueError('Missing chart rows')
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = str(row.get('dt' if daily else 'cntr_tm', ''))
        if not re.fullmatch(r'\d{8}' if daily else r'\d{14}', stamp):
            continue
        try:
            date = datetime.strptime(stamp, '%Y%m%d' if daily else '%Y%m%d%H%M%S').replace(tzinfo=KST)
            if date > at or (not daily and not '090000' <= stamp[8:] <= '153000'):
                continue
            values = [abs(float(str(row[key]).replace(',', '').strip()))
                for key in ('open_pric', 'high_pric', 'low_pric', 'cur_prc', 'trde_qty')]
            op, hi, lo, close, volume = values
            if not all(math.isfinite(v) for v in values) or not 0 < lo <= min(op, close) <= max(op, close) <= hi:
                continue
            out[stamp] = dict(stamp=stamp, open=op, high=hi, low=lo, close=close, volume=volume)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return [out[key] for key in sorted(out)]


class PriceChartService:
    def __init__(self, get_rest, clock=now_kst, timer=time.monotonic):
        self.get_rest, self.clock, self.timer = get_rest, clock, timer
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def read(self, code, interval, limit=240):
        if not re.fullmatch(r'[0-9A-Z]{6}', code) or interval not in ('1m', '5m', '15m', '1d'):
            raise ValueError('Invalid chart selection')
        key = (code, interval)
        async with self.lock:
            rest = self.get_rest()
            if rest is None:
                raise RuntimeError('Quote connection unavailable')
            cached = self.cache.get(key)
            if cached and cached[0] is rest and 0 <= self.timer()-cached[1] < 15:
                self.cache.move_to_end(key)
                return cached[2] | {'bars': cached[2]['bars'][-limit:]}
            at = self.clock()
            if interval == '1d':
                api_id = 'ka10081'
                payload = {'stk_cd': code, 'base_dt': at.strftime('%Y%m%d'), 'upd_stkpc_tp': '1'}
            else:
                api_id = 'ka10080'
                payload = {'stk_cd': code, 'tic_scope': interval[:-1], 'upd_stkpc_tp': '1'}
            # Exactly one quote-page per refresh; no account/order API, subscriptions,
            # scan, or execution-engine state changes are made by chart viewing.
            body, _ = await asyncio.wait_for(rest.post('/api/dostk/chart', api_id, payload), timeout=12)
            fetched = self.clock()
            bars = parse_chart(body, interval, fetched)[-600:]
            result = {'code': code, 'interval': interval, 'bars': bars, 'source': '키움 · KRX · 수정주가',
                'fetched_at': fetched.isoformat(timespec='seconds'), 'last_bar_at': bars[-1]['stamp'] if bars else None}
            self.cache[key] = (rest, self.timer(), result)
            self.cache.move_to_end(key)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return result | {'bars': bars[-limit:]}


def create_price_chart_router(service):
    router = APIRouter()

    def local(request: Request):
        if request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            raise HTTPException(403, '이 PC에서만 조회할 수 있습니다')
        if request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            raise HTTPException(403, '로컬 주소로 접속하세요')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    @router.get('/api/price-chart/{code}', dependencies=[Depends(local)])
    async def chart(response: Response, code: str, interval: Interval = '1m', limit: int = Query(240, ge=20, le=600)):
        response.headers['Cache-Control'] = 'no-store'
        if not re.fullmatch(r'[0-9A-Z]{6}', code):
            raise HTTPException(422, '6자리 종목코드를 확인하세요')
        try:
            return await service.read(code, interval, limit)
        except Exception as exc:
            raise HTTPException(502, '차트 조회 실패 · 키움 시세 연결을 확인하세요') from exc

    return router
