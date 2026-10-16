"""Local, user-operated activation surface for automatic exits."""
from app.exit_cancel_routes import attach_cancel_routes
import asyncio
import hashlib
import secrets
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from app.autoexit import PENDING
from app.horizon_plan import HorizonExit
from app.autosell import normal_code
from app.kiwoom.extended_sell import ExtendedSellOrders
from app.kiwoom.orders import NotSent


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    bought_on: date
    after_hours: Literal["off", "KRX", "NXT", "both"] = "off"
    retry_unfilled: StrictBool = False
    empty_book: Literal["wait", "floor"] = "wait"


class Arm(BaseModel):
    plan_id: str
    environment: str
    market_order: bool


class Pause(BaseModel):
    code: str | None = None


class RemovePlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    plan_id: str


class ExitService:
    def __init__(self, settings, sell):
        self.sell, self.settings = sell, settings
        identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
        self.engine = HorizonExit(settings.data_dir / f'autoexit_{identity}.json', settings.kiwoom_env,
                               self.broker, self.connected, self.phase, sell.monitor)
        self.token = secrets.token_urlsafe(32)
        self.task = None
        sell.execution = self

    def connected(self):
        feed = self.sell.get_feed()
        return (feed is not None and feed is self.sell.bound_feed and self.sell.monitor.enabled
                and not self.sell.monitor.error and feed.ws.exit_ready
                and self.task is not None and not self.task.done()
                and self.sell.task is not None and not self.sell.task.done()
                and feed.ws.reconnects == self.sell.connected_generation)

    def phase(self):
        feed = self.sell.get_feed()
        if not self.connected():
            return None
        ws = getattr(feed, 'ws', None)
        return ws.market_phase() if hasattr(ws, 'market_phase') else getattr(ws, 'market_state', None)

    def broker(self):
        feed = self.sell.get_feed()
        if not feed or not hasattr(feed, 'rest') or self.settings.broker != 'kiwoom':
            raise NotSent('키움 계좌 연결이 필요합니다')
        return ExtendedSellOrders(feed.rest, self.settings.kiwoom_env)

    async def start(self):
        self.engine.open()  # Restarts NEVER arm or send orders.
        if not self.engine.error:
            self.task = asyncio.create_task(self.loop(), name='autoexit-order-status')

    async def stop(self):
        self.engine.pause(note='서버 종료 · 다시 확인 후 켜세요')
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.engine.tasks:
            # Let an already transmitted request resolve; never cancel and resend it.
            await asyncio.gather(*list(self.engine.tasks), return_exceptions=True)
        self.engine.close_lock()

    def quote(self, trade, stamp):
        try:
            self.engine.quote(trade.code, trade.price, stamp, trade.market)
        except Exception:
            self.engine.pause(note='가격/주문 기록 오류 · 자동매도 중단')

    def account(self):
        self.engine.account_changed()

    async def loop(self):
        count = 0
        while True:
            try:
                self.engine.check()
                if any(o.get("chase_active") for o in self.engine.orders):
                    await self.engine.reprice()
                if count % 5 == 0 and (any((o['status'] in PENDING or o.get('manual_cancel_state')=='accepted') for o in self.engine.orders) or any(p['status']=='ordered' for p in self.engine.plans.values())):
                    await self.engine.reconcile()
            except Exception:
                self.engine.pause(note='자동매도 처리 오류 · 주문내역 확인 필요')
            count += 1
            await asyncio.sleep(1)


def create_exit_router(service):
    router = APIRouter(prefix='/api/autoexit')

    def local(request: Request):
        # Host validation blocks DNS rebinding; no forwarded headers are trusted here.
        if request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
            raise HTTPException(403, '이 PC에서만 자동매도를 설정할 수 있습니다')
        if request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            raise HTTPException(403, '로컬 주소로 접속하세요')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')
        return request

    def mutate(request: Request = Depends(local)):
        if not secrets.compare_digest(request.headers.get('x-autoexit-token', ''), service.token):
            raise HTTPException(403, '설정 화면을 새로 열어주세요')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '요청 출처를 확인하세요')

    @router.get('', dependencies=[Depends(local)])
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.engine.snapshot() | {'token': service.token}

    @router.post('/prepare', dependencies=[Depends(mutate)])
    async def prepare(body: Prepare):
        try:
            await service.sell.start()  # Read-only balance/account subscriptions.
            await service.sell.bound_feed.ws.ensure_market()
            await service.sell.calendar()
            code = normal_code(body.code)
            if body.after_hours in ('NXT','both'):
                await service.sell.get_feed().watch_codes([code+'_NX'])
            if body.after_hours == 'off' and not body.retry_unfilled and body.empty_book == 'wait':
                plan = await service.engine.prepare(code, body.bought_on)
            else:
                plan = await service.engine.prepare(code, body.bought_on, after_hours=body.after_hours,
                    retry_unfilled=body.retry_unfilled, empty_book=body.empty_book)
            if plan['code'] in service.sell.monitor.positions:
                service.sell.monitor.set_buy_date(plan['code'], body.bought_on)
            return plan
        except (NotSent, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, '잔고/연결 조회 실패 · 잠시 후 다시 확인하세요') from exc

    @router.post('/arm', dependencies=[Depends(mutate)])
    async def arm(body: Arm):
        try:
            await service.engine.arm(body.plan_id, body.environment, body.market_order)
        except (NotSent, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, '잔고/연결 조회 실패 · 시작되지 않았습니다') from exc
        return service.engine.snapshot()

    @router.post('/pause', dependencies=[Depends(mutate)])
    async def pause(body: Pause):
        service.engine.pause(body.code)
        return service.engine.snapshot()

    @router.post('/reconcile', dependencies=[Depends(mutate)])
    async def reconcile():
        await service.engine.reconcile()  # Queries only; no cancellation or order submission.
        return service.engine.snapshot()

    @router.post('/remove', dependencies=[Depends(mutate)])
    async def remove(body: RemovePlan):
        try:
            removed = await service.engine.remove_plan(body.code, body.plan_id)
        except (NotSent, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, '조건 삭제가 완료되지 않았습니다 · 저장 상태를 확인하세요') from exc
        return service.engine.snapshot() | removed

    attach_cancel_routes(router,service,mutate)
    return router
