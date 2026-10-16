"""키움 계좌를 읽어 매도 조건만 검증한다. 증권사 주문·텔레그램 발송 기능 없음."""
import asyncio
import hashlib
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.autosell import KST, SellMonitor, now_kst
from app.collectors.naver_daily import NaverDaily


class BuyDateRequest(BaseModel):
    code: str
    bought_on: date


class SellService:
    def __init__(self, settings, get_feed):
        identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
        self.monitor = SellMonitor(settings.data_dir / f'autosell_shadow_{identity}.json')
        self.get_feed = get_feed
        self.task = None
        self.bound_feed = None
        self.connected_generation = None
        self.lock = asyncio.Lock()
        self.calendar_at = None
        self.execution = None

    async def calendar(self):
        client = NaverDaily()
        try:
            candles = await client.candles('069500', 250)
            sessions = [datetime.strptime(c.date, '%Y%m%d').date().isoformat() for c in candles if c.volume > 0]
            self.monitor.set_calendar(sessions)
        except Exception as exc:
            self.monitor.calendar_error = f'거래일 확인 실패: {type(exc).__name__}'
        finally:
            self.calendar_at = now_kst()  # 실패 시에도 초당 재요청하지 않는다.
            await client.close()

    async def holdings(self, feed):
        rows, key, seen = [], '', set()
        for _ in range(100):
            data, key = await feed.rest.post('/api/dostk/acnt', 'kt00018',
                                            {'qry_tp': '2', 'dmst_stex_tp': 'KRX'}, key)
            if 'acnt_evlt_remn_indv_tot' not in data:
                raise RuntimeError('잔고 응답 형식 확인 필요')
            rows.extend(data['acnt_evlt_remn_indv_tot'] or [])
            if not key:
                return rows
            if key in seen:
                break
            seen.add(key)
        raise RuntimeError('잔고 전체 페이지를 확인하지 못했습니다')

    async def bind(self):
        if self.execution:
            self.execution.engine.pause(note='계좌 연결 변경 · 매수일과 잔고 확인 후 다시 켜세요')
        from app.feeds import KiwoomFeed
        feed = self.get_feed()
        if not isinstance(feed, KiwoomFeed) or not feed.ws.connected:
            raise RuntimeError('키움 실시간 연결 후 시작할 수 있습니다')
        if self.bound_feed:
            self.bound_feed.account_observer = None
            self.bound_feed.quote_observer = None
        # 새 연결의 계좌 동기화 중 도착하는 이벤트는 순서대로 적용한다.
        pending = []
        feed.account_observer = lambda event: pending.append(event)
        try:
            await feed.ws.ensure_account()
            if self.execution:
                await feed.ws.ensure_market()
            rows = await self.holdings(feed)
            self.monitor.sync_holdings(rows, now_kst())
            self.monitor.enabled = True
            feed.account_observer = self.account
            feed.quote_observer = self.quote
            self.bound_feed = feed
            self.connected_generation = feed.ws.reconnects
            for event in pending:
                # 동기화 중 체결의 시각을 최초 매수일로 추측하지 않는다.
                if event.get('type') == '04':
                    self.account(event)
            await feed.watch_codes(list(self.monitor.positions))
            self.monitor.error = ''
        except Exception:
            feed.account_observer = None
            feed.quote_observer = None
            raise

    def account(self, event):
        try:
            self.monitor.account_event(event, now_kst())
            if self.execution:
                self.execution.account()
        except Exception:
            self.monitor.error = '계좌 이벤트 확인 필요 · 감시를 멈춘 뒤 다시 시작하세요'
            self.monitor.enabled = False

    def quote(self, trade):
        if trade.market not in ('KRX','NX'):
            return
        now = now_kst()
        try:
            stamp = datetime.combine(now.date(), datetime.strptime(trade.time[:6], '%H%M%S').time(), KST)
        except ValueError:
            return
        if trade.market=='NX':
            if self.execution:self.execution.quote(trade,stamp)
            return
        self.monitor.quote(trade.code, trade.price, stamp, trade.market)
        # 1초 루프 사이에 문턱을 찍고 돌아와도 최초 도달 신호를 놓치지 않는다.
        # 루프는 D+2 시간 조건과 연결 상태 확인을 계속 담당한다.
        try:
            self.monitor.evaluate(now)
        except Exception:
            self.monitor.enabled = False
            self.monitor.error = '매도조건 기록 실패 · 감시를 멈춘 뒤 다시 시작하세요'
        if self.execution:
            self.execution.quote(trade, stamp)

    async def start(self):
        async with self.lock:
            if self.task and not self.task.done():
                return
            await self.calendar()
            await self.bind()
            self.task = asyncio.create_task(self.loop(), name='autosell-shadow')

    async def stop(self):
        async with self.lock:
            if self.execution:
                self.execution.engine.pause(note='계좌 감시 중단 · 자동매도도 중단했습니다')
            self.monitor.enabled = False
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                self.task = None
            if self.bound_feed:
                self.bound_feed.account_observer = None
                self.bound_feed.quote_observer = None
                self.bound_feed = None

    async def loop(self):
        while True:
            try:
                feed = self.get_feed()
                if feed is not self.bound_feed or not feed.ws.connected or feed.ws.reconnects != self.connected_generation:
                    self.monitor.enabled = False
                    self.monitor.error = '연결 변경 · 계좌 재확인 중'
                    await self.bind()
                if not self.calendar_at or (now_kst() - self.calendar_at).total_seconds() > 300:
                    await self.calendar()
                await feed.watch_codes(list(self.monitor.positions))
                if self.monitor.enabled:
                    self.monitor.evaluate(now_kst())
            except Exception as exc:
                self.monitor.error = f'조건 감시 중단: {type(exc).__name__} · 연결 확인 중'
            await asyncio.sleep(1)


def create_router(service: SellService):
    router = APIRouter(prefix='/api/autosell')

    @router.get('')
    def status():
        return service.monitor.snapshot()

    @router.post('/start')
    async def start():
        try:
            await service.start()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return service.monitor.snapshot()

    @router.post('/stop')
    async def stop():
        await service.stop()
        return service.monitor.snapshot()

    @router.post('/buy-date')
    async def buy_date(body: BuyDateRequest):
        try:
            service.monitor.set_buy_date(body.code, body.bought_on)
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return service.monitor.snapshot()

    return router
