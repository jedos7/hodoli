"""Horizon reference analytics. No account, order, or strategy dependencies.

CVD uses Kiwoom 0B FID 15 (+buy/-sell), never candle/price-change inference.
Quotes use the existing REST limiter. Only a selected stock requests extra data.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.autosell import KST, now_kst

log = logging.getLogger(__name__)
from app.trade_tape import TradeTape

CODE = re.compile(r'[0-9A-Z]{6}')


def integer(value):
    text = str(value).strip().replace(',', '')
    return int(text) if re.fullmatch(r'[+-]?\d{1,18}', text) else None


def valid_date(value):
    try:
        return bool(re.fullmatch(r'\d{8}', value) and datetime.strptime(value, '%Y%m%d'))
    except (TypeError, ValueError):
        return False


def clock_time(value):
    value = str(value).strip()
    try:
        return value if re.fullmatch(r'\d{6}', value) and datetime.strptime(value, '%H%M%S') else None
    except ValueError:
        return None


class TradeCapture:
    """Bounded price-level aggregates; the exchange cumulative counter deduplicates ticks."""
    def __init__(self, path: Path, clock=now_kst):
        self.path, self.clock = path, clock
        self.days = {}
        self.tape = TradeTape(path.parent / 'tape')
        self.dirty = False
        self.storage_error = False
        self.generations = {}
        if path.exists():
            try:
                body = json.loads(path.read_text('utf-8'))
                if body['version'] != 1 or not isinstance(body['days'], dict):
                    raise ValueError('Invalid capture version')
                # Discard malformed records without interpreting them as zero flow.
                for key, r in body['days'].items():
                    if (CODE.fullmatch(str(r['code'])) and valid_date(r['date'])
                            and key == r['code'] + ':' + r['date']
                            and isinstance(r['levels'], dict)
                            and all(isinstance(r[n], int) and r[n] >= 0 for n in ('acc', 'ticks', 'buy_won', 'sell_won'))
                            and all(integer(p) and int(p) > 0 and isinstance(q, list) and len(q) == 2
                                    and all(type(v) is int and v >= 0 for v in q) for p, q in r['levels'].items())):
                        r['partial'] = True  # A restart cannot establish continuous capture.
                        self.days[key] = r
                self.prune()
            except (ValueError, TypeError, KeyError, OSError):
                self.storage_error = True
                log.warning('Horizon flow capture could not be loaded')

    def prune(self):
        cutoff = (self.clock() - timedelta(days=45)).strftime('%Y%m%d')
        today = self.clock().strftime('%Y%m%d')
        self.days = {k: v for k, v in self.days.items() if cutoff <= v['date'] <= today}
        for key in sorted(self.days, key=lambda k: self.days[k]['date'])[:-3000]:
            self.days.pop(key)
        self.generations = {k: v for k, v in self.generations.items() if k in self.days}

    def on_trade(self, trade, generation=None):
        at = self.clock()
        stamp = clock_time(trade.time)
        signed = getattr(trade, 'signed_volume', None)
        # Reject delayed/replayed packets and all non-KRX or non-regular-session data.
        if (trade.market != 'KRX' or not CODE.fullmatch(trade.code) or not stamp
                or not '090000' <= stamp <= '153000' or at.weekday() >= 5
                or not isinstance(signed, int) or signed == 0 or trade.price <= 0
                or trade.acc_volume <= 0 or abs(signed) != trade.volume):
            return
        event = datetime.combine(at.date(), datetime.strptime(stamp, '%H%M%S').time(), KST)
        if not -2 <= (at - event).total_seconds() <= 15:
            return
        key = trade.code + ':' + at.strftime('%Y%m%d')
        r = self.days.get(key)
        if r is None:
            self.prune()
            # Bound memory without evicting current-day captures on every tick.
            if len(self.days) >= 3000:
                return
            r = self.days[key] = dict(code=trade.code, date=at.strftime('%Y%m%d'), levels={}, acc=0,
                ticks=0, buy_won=0, sell_won=0, first_at=event.isoformat(), last_at=event.isoformat(),
                last_price=trade.price, partial=trade.acc_volume != abs(signed))
        if trade.acc_volume <= r['acc']:
            return
        if (trade.acc_volume - r['acc'] != abs(signed)
                or (key in self.generations and self.generations[key] != generation)):
            r['partial'] = True
        self.generations[key] = generation
        p = str(trade.price)
        if p not in r['levels'] and len(r['levels']) >= 5000:
            r['partial'] = True
            return
        quantities = r['levels'].setdefault(p, [0, 0])
        quantities[0 if signed > 0 else 1] += abs(signed)
        r['buy_won' if signed > 0 else 'sell_won'] += trade.price * abs(signed)
        r.update(acc=trade.acc_volume, ticks=r['ticks'] + 1, last_at=event.isoformat(), last_price=trade.price)
        minutes = r.setdefault('minutes', {})
        minute = stamp[:4]
        point = minutes.setdefault(minute, dict(time=minute, open=trade.price, high=trade.price,
            low=trade.price, close=trade.price, volume=0, net_won=0))
        point.update(high=max(point['high'],trade.price), low=min(point['low'],trade.price),
                     close=trade.price, volume=point['volume']+abs(signed),
                     net_won=r['buy_won']-r['sell_won'])
        self.tape.add(r['date'], trade.code, stamp, trade.price, signed)
        self.dirty = True

    def view(self, code, since, sessions):
        rs = sorted((r for r in self.days.values() if r['code'] == code and r['date'] >= since), key=lambda r: r['date'])
        daily = [dict(date=r['date'], net_won=r['buy_won'] - r['sell_won'], ticks=r['ticks'],
                      first_at=r['first_at'], last_at=r['last_at'], partial=r['partial']) for r in rs]
        latest = rs[-1] if rs else None
        levels = [dict(price=int(p), buy=q[0], sell=q[1], net=q[0]-q[1], volume=sum(q))
                  for p, q in (latest['levels'].items() if latest else [])]
        expected = [d for d in sessions if d >= since]
        complete = bool(rs and since in sessions and expected and [r['date'] for r in rs] == expected
                        and all(not r['partial'] for r in rs))
        base = next((r['net_won'] for r in daily if r['date'] == since), None)
        total = sum(r['net_won'] for r in daily) if daily else None
        return dict(date=latest['date'] if latest else None, ticks=latest['ticks'] if latest else 0,
                    first_at=latest['first_at'] if latest else None, last_at=latest['last_at'] if latest else None,
                    last_price=latest['last_price'] if latest else None, partial=latest['partial'] if latest else True,
                    poc=max(levels, key=lambda x: (x['volume'], -x['price']))['price'] if levels else None,
                    received=sorted((x for x in levels if x['net'] > 0), key=lambda x: -x['net'])[:2],
                    sold=sorted((x for x in levels if x['net'] < 0), key=lambda x: x['net'])[:2],
                    levels=sorted(levels,key=lambda x:x['price']),
                    minutes=[v for k,v in sorted((latest.get('minutes',{}) if latest else {}).items())
                             if isinstance(v,dict) and all(type(v.get(n)) in (int,float) for n in
                                ('open','high','low','close','volume','net_won'))],
                    daily=daily, base_won=base, total_won=total, complete=complete,
                    ratio_pct=total/base*100 if complete and base is not None and base > 0 else None,
                    storage_error=self.storage_error)

    async def flush(self):
        if not self.dirty:
            return
        self.prune()
        body = copy.deepcopy(dict(version=1, days=self.days))
        self.dirty = False
        def write():
            self.tape.flush()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(body, ensure_ascii=False, separators=(',', ':')), 'utf-8')
            tmp.replace(self.path)
        try:
            await asyncio.to_thread(write)
            self.storage_error = False
        except OSError:
            self.dirty = True
            self.storage_error = True
            log.warning('Horizon flow capture save failed')


class WallCapture:
    def __init__(self):
        self.rows, self.last_at, self.date = {}, None, None

    def update(self, body, at):
        stamp = clock_time(body.get('bid_req_base_tm'))
        if not stamp or not '090000' <= stamp <= '153000':
            raise ValueError('호가 기준 시각 미확인')
        event = datetime.combine(at.date(), datetime.strptime(stamp, '%H%M%S').time(), KST)
        if at.weekday() >= 5 or not 0 <= (at-event).total_seconds() <= 30:
            raise ValueError('정규장 최신 호가 수집 대기')
        if self.last_at and (at-self.last_at).total_seconds() < 10:
            return self.view()  # Cache refreshes do not inflate the sample count.
        if self.date != at.strftime('%Y%m%d') or self.last_at and (at-self.last_at).total_seconds() > 50:
            self.rows = {}
        current = {}
        for level in range(1, 11):
            prefix = 'sel_fpr' if level == 1 else f'sel_{level}th_pre'
            p, q = integer(body.get(prefix+'_bid')), integer(body.get(prefix+'_req'))
            if p is None or q is None or q < 0:
                raise ValueError('10단계 매도호가 자료 미확인')
            if p and q:
                current[abs(p)] = q
        self.rows = {p: dict(price=p, quantity=q, samples=self.rows.get(p, {}).get('samples', 0)+1,
                            total=self.rows.get(p, {}).get('total', 0)+q) for p, q in current.items()}
        self.last_at, self.date = at, at.strftime('%Y%m%d')
        return self.view()

    def view(self):
        return dict(at=self.last_at.isoformat() if self.last_at else None,
                    rows=[dict(price=r['price'], quantity=r['quantity'], samples=r['samples'],
                               average=r['total']/r['samples']) for r in sorted(self.rows.values(), key=lambda x: -x['total']/x['samples'])[:4]])


def parse_program(body, day, at):
    rows = body.get('stk_tm_prm_trde_trnsn')
    if not isinstance(rows, list):
        raise ValueError('프로그램 자료 없음')
    valid = []
    for r in rows:
        stamp = clock_time(r.get('tm'))
        if not stamp or r.get('stex_tp') not in ('KRX', '1'):
            continue
        # Venue mismatches and future times must not be presented as current KRX data.
        if day == at.strftime('%Y%m%d') and stamp > at.strftime('%H%M%S'):
            continue
        values = {k: integer(r.get(field)) for k, field in (
            ('buy_won', 'prm_buy_amt'), ('sell_won', 'prm_sell_amt'), ('net_won', 'prm_netprps_amt'),
            ('buy', 'prm_buy_qty'), ('sell', 'prm_sell_qty'), ('net', 'prm_netprps_qty'))}
        if values['net_won'] is None:
            continue
        for key in ('buy_won', 'sell_won', 'net_won'):
            if values[key] is not None:
                values[key] *= 1_000_000  # ka90008 amounts are million KRW, not shares or KRW.
        valid.append(dict(date=day, time=stamp, **values))
    if not valid:
        raise ValueError('KRX 프로그램 자료 미확인')
    return max(valid, key=lambda r: r['time'])


def parse_brokers(body):
    if not isinstance(body.get('stk_sec_rank'), list):
        raise ValueError('창구 자료 없음')
    rows = []
    def broker_number(value):
        # ka10038 live responses use directional signs on quantities and duplicate
        # the minus on net sells (e.g. sell=-229903, net=--181419).
        m = re.fullmatch(r'([+-]{0,2})(\d{1,18})', str(value).strip().replace(',', ''))
        if not m or len(set(m[1])) > 1:
            return None
        return int(m[2]) * (-1 if '-' in m[1] else 1)
    for r in body['stk_sec_rank']:
        name = str(r.get('mmcm_nm', '')).strip()
        buy, sell, net = (broker_number(r.get(k)) for k in ('buy_qty', 'sell_qty', 'acc_netprps_qty'))
        if not name or buy is None or sell is None or net is None:
            continue
        buy, sell = abs(buy), abs(sell)
        if buy-sell != net:
            continue
        rows.append(dict(name=name[:80], buy=buy, sell=sell, net=net))
    if body['stk_sec_rank'] and not rows:
        raise ValueError('창구 수량 형식 미확인')
    return rows


class StockFlowService:
    def __init__(self, path, get_feed, clock=now_kst, timer=time.monotonic):
        self.get_feed, self.clock, self.timer = get_feed, clock, timer
        self.capture = TradeCapture(path, clock)
        self.cache, self.walls = OrderedDict(), OrderedDict()
        self.lock = asyncio.Lock()
        self.task = None

    def on_trade(self, trade):
        feed = self.get_feed()
        self.capture.on_trade(trade, (id(feed), getattr(getattr(feed, 'ws', None), 'reconnects', None)))

    async def start(self):
        self.stopping = asyncio.Event()
        async def loop():
            while not self.stopping.is_set():
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=15)
                except TimeoutError:
                    pass
                await self.capture.flush()
        self.task = asyncio.create_task(loop(), name='horizon-flow-save')

    async def stop(self):
        if self.task:
            self.stopping.set()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.capture.flush()

    async def cached(self, key, rest, ttl, fn):
        prior = self.cache.get(key)
        if prior and prior[0] is rest and self.timer()-prior[1] < ttl:
            self.cache.move_to_end(key)
            return prior[2]
        try:
            result = await fn()
            value = dict(data=result, error=None, fetched_at=self.clock().isoformat(timespec='seconds'))
        except Exception as exc:
            known = {'정규장 최신 호가 수집 대기', '호가 기준 시각 미확인', '10단계 매도호가 자료 미확인',
                     'KRX 프로그램 자료 미확인', '창구 수량 형식 미확인', '거래일 미확인'}
            value = dict(data=prior[2]['data'] if prior and prior[0] is rest else None,
                         error=str(exc) if isinstance(exc, ValueError) and str(exc) in known else '조회 지연 · 이전 자료가 있으면 표시합니다',
                         fetched_at=prior[2]['fetched_at'] if prior and prior[0] is rest else None)
        self.cache[key] = (rest, self.timer(), value)
        self.cache.move_to_end(key)
        while len(self.cache) > 160:
            self.cache.popitem(last=False)
        return value

    async def read(self, code, since):
        if not CODE.fullmatch(code) or not valid_date(since) or since > self.clock().strftime('%Y%m%d'):
            raise ValueError('종목코드·기준봉 날짜를 확인하세요')
        at = self.clock()
        unavailable = dict(data=None, error='키움 시세 연결 대기', fetched_at=None)
        out = dict(code=code, since=since, at=at.isoformat(timespec='seconds'), connected=False,
                   book=unavailable, program=unavailable, brokers=unavailable)
        sessions = []
        feed = self.get_feed()
        if feed is not None and getattr(feed, 'rest', None) is not None:
            async with self.lock:
                rest = feed.rest
                async def post(path, api, body):
                    return await asyncio.wait_for(rest.post(path, api, body), timeout=8)
                out['connected'] = bool(getattr(feed.ws, 'connected', False))
                # Read-only quote subscription. No account registration or order activation.
                try:
                    await asyncio.wait_for(feed.watch_codes([code]), timeout=4)
                except Exception:
                    out['connected'] = False
                async def calendar():
                    body, _ = await post('/api/dostk/chart', 'ka20006', {'inds_cd': '101', 'base_dt': at.strftime('%Y%m%d')})
                    dates = sorted({r['dt'] for r in body.get('inds_dt_pole_qry', [])
                                    if valid_date(r.get('dt')) and r['dt'] <= at.strftime('%Y%m%d')
                                    and (integer(r.get('trde_qty')) or 0) > 0})
                    if not dates:
                        raise ValueError('거래일 미확인')
                    return dates
                cal = await self.cached(('calendar', at.strftime('%Y%m%d')), rest, 300, calendar)
                sessions = cal['data'] or []
                async def book():
                    body, _ = await post('/api/dostk/mrkcond', 'ka10004', {'stk_cd': code})
                    wall = self.walls.setdefault(code, WallCapture())
                    self.walls.move_to_end(code)
                    while len(self.walls) > 32:
                        self.walls.popitem(last=False)
                    return wall.update(body, self.clock())
                out['book'] = await self.cached(('book', code), rest, 15, book)
                if sessions and not cal['error']:
                    day = sessions[-1]
                    async def program():
                        body, _ = await post('/api/dostk/mrkcond', 'ka90008', {'amt_qty_tp': '1', 'stk_cd': code, 'date': day})
                        return parse_program(body, day, self.clock())
                    out['program'] = await self.cached(('program', code, day), rest, 60, program)
                    async def brokers():
                        days = sessions[-2:]
                        async def rank(start, end):
                            found = {}
                            truncated = False
                            for kind in ('2', '1'):
                                body, more = await post('/api/dostk/rkinfo', 'ka10038',
                                    {'stk_cd': code, 'strt_dt': start, 'end_dt': end, 'qry_tp': kind, 'dt': ''})
                                truncated = truncated or bool(more)
                                for r in parse_brokers(body):
                                    # Do not sum the same broker returned in two sort orders.
                                    found.setdefault(r['name'], r)
                            return list(found.values()), truncated
                        rs, partial = await rank(days[0], days[-1])
                        flips = []
                        if len(days) == 2:
                            prev, _ = await rank(days[0], days[0])
                            today, _ = await rank(days[1], days[1])
                            before = {r['name']: r['net'] for r in prev}
                            flips = [dict(name=r['name'], before=before[r['name']], now=r['net'])
                                     for r in today if before.get(r['name'], 0) > 0 and r['net'] < 0]
                        return dict(days=days, partial=partial,
                            bought=sorted((r for r in rs if r['net'] > 0), key=lambda r: -r['net'])[:3],
                            sold=sorted((r for r in rs if r['net'] < 0), key=lambda r: r['net'])[:3],
                            flips=sorted(flips, key=lambda r: r['now'])[:2])
                    out['brokers'] = await self.cached(('brokers', code, day), rest, 90, brokers)
        out['capture'] = self.capture.view(code, since, sessions)
        return out


def create_stock_flow_router(service):
    router = APIRouter()
    def local(request: Request):
        if (request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver')):
            raise HTTPException(403, '이 PC에서만 조회할 수 있습니다')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    @router.get('/api/horizon-flow/{code}', dependencies=[Depends(local)])
    async def read(response: Response, code: str, since: str = Query(..., pattern=r'^\d{8}$')):
        response.headers['Cache-Control'] = 'no-store'
        try:
            return await asyncio.wait_for(service.read(code, since), timeout=40)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, '수급 조회 지연 · 잠시 후 다시 확인합니다') from exc
    @router.get('/api/trade-profile/{code}', dependencies=[Depends(local)])
    async def profile(response: Response, code: str, start: str, end: str):
        response.headers['Cache-Control'] = 'no-store'
        try:
            if not CODE.fullmatch(code) or not re.fullmatch(r'\d{12}', start) or not re.fullmatch(r'\d{12}', end):
                raise ValueError()
            a, b = datetime.strptime(start, '%Y%m%d%H%M'), datetime.strptime(end, '%Y%m%d%H%M')
            if b < a or (b-a).days > 400:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, '조회 기간을 확인하세요')
        try:
            return await asyncio.to_thread(service.capture.tape.read, code, start, end)
        except (OSError, ValueError):
            raise HTTPException(503, '체결 기록을 읽을 수 없습니다')
    return router
