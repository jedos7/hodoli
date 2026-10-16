"""Separate user settings/API, sharing only account data and conflict checks."""
from app.exit_cancel_routes import attach_cancel_routes
import asyncio
import hashlib
import secrets
from decimal import Decimal
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool

from app.autoexit import PENDING
from app.autosell import normal_code
from app.custom_exit import CustomExit
from app.exit_coordination import CoordinatedBroker, ExecutionGroup
from app.kiwoom.orders import NotSent
from app.kiwoom.extended_sell import ExtendedSellOrders


class CustomPrepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    take_profit_pct: Decimal | None = Field(None, gt=0, le=1000, decimal_places=2)
    take_profit_quantity: StrictInt | None = Field(None, gt=0)
    stop_loss_pct: Decimal | None = Field(None, gt=0, lt=100, decimal_places=2)
    stop_loss_quantity: StrictInt | None = Field(None, gt=0)
    profit_protection_price: StrictInt | None = Field(None, gt=0, lt=10**9)


class CustomArm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str
    environment: str
    market_order: bool


class CustomPause(BaseModel):
    code: str | None = None


class TicketPrepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    kind: Literal['profit','protection','loss']
    price: Decimal = Field(gt=0, lt=10**9)
    comparison: Literal['gte','lte','lt']
    quantity: StrictInt = Field(gt=0)
    all_remaining: StrictBool = False
    order_type: Literal['market','limit']
    limit_price: StrictInt | None = Field(None, gt=0, lt=10**9)
    expires_on: date | None = None
    rule_id: str | None = None
    after_hours: Literal['off','KRX','NXT','both'] = 'off'
    retry_unfilled: StrictBool = False
    empty_book: Literal['wait','floor'] = 'wait'


class TicketArm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    review_id: str
    environment: str
    confirm_order: StrictBool


class TicketSelect(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    plan_id: str
    rule_id: str


class CustomExitService:
    def __init__(self, settings, sell, horizon):
        self.sell, self.horizon, self.settings = sell, horizon, settings
        self.task, self.token = None, secrets.token_urlsafe(32)
        identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
        self.engine = CustomExit(settings.data_dir / f'custom_exit_{identity}.json', settings.kiwoom_env,
                                 self.broker, self.connected, horizon.phase, sell.monitor)
        # Horizon's strategy, plan file, thresholds and routes are unchanged.
        # The adapter only blocks a conflicting plan for the same holding.
        original = horizon.engine.broker
        horizon.engine.broker = lambda: CoordinatedBroker(original(), self.engine, '직접 설정 자동매도')
        sell.execution = ExecutionGroup(horizon, self)

    def broker(self):
        base=self.horizon.broker()
        return CoordinatedBroker(ExtendedSellOrders(base.rest,base.env), self.horizon.engine, '호라이즌 자동매도')

    def connected(self):
        return self.task is not None and not self.task.done() and self.horizon.connected()

    async def start(self):
        self.engine.open()
        if not self.engine.error:
            self.task = asyncio.create_task(self.loop(), name='custom-exit-status')

    async def stop(self):
        self.engine.pause(note='서버 종료 · 직접 설정 자동매도를 다시 확인 후 켜세요')
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.engine.tasks:
            await asyncio.gather(*list(self.engine.tasks), return_exceptions=True)
        self.engine.close_lock()

    def quote(self, trade, stamp):
        try:
            self.engine.quote(trade.code, trade.price, stamp, trade.market)
        except Exception:
            self.engine.pause(note='직접 설정 가격/주문 기록 오류 · 감시 중단')

    def account(self):
        self.engine.account_changed()

    async def loop(self):
        count = 0
        while True:
            try:
                self.engine.check()
                await self.engine.reprice()
                if count % 5 == 0 and (any(o['status'] in PENDING or o.get('manual_cancel_state')=='accepted' or
                        (o.get('rule_id') and not o.get('balance_done') and o['status'] in ('filled','closed'))
                        for o in self.engine.orders)
                        or any(p['status'] == 'remainder_check' for p in self.engine.plans.values())):
                    await self.engine.reconcile()
            except Exception:
                self.engine.pause(note='직접 설정 자동매도 오류 · 주문내역 확인 필요')
            count += 1
            await asyncio.sleep(1)


def create_custom_exit_router(service):
    router = APIRouter(prefix='/api/custom-exit')

    def local(request: Request):
        if request.client is None or request.client.host not in ('127.0.0.1','::1','testclient'):
            raise HTTPException(403, '이 PC에서만 설정할 수 있습니다')
        if request.url.hostname not in ('127.0.0.1','localhost','::1','testserver'):
            raise HTTPException(403, '로컬 주소로 접속하세요')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    def mutate(request: Request, _: None = Depends(local)):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or not secrets.compare_digest(request.headers.get('x-custom-exit-token',''), service.token)):
            raise HTTPException(403, '직접 설정 자동매도 화면을 다시 열어주세요')

    @router.get('', dependencies=[Depends(local)])
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.engine.snapshot() | {'token':service.token}

    @router.post('/holdings', dependencies=[Depends(mutate)])
    async def holdings():
        try:
            await service.sell.start()
            rows = await service.horizon.broker().positions()
            return {'positions': list(rows.values())}
        except Exception as exc:
            raise HTTPException(409, '잔고 조회 실패 · 키움 연결을 확인하세요') from exc

    @router.post('/prepare', dependencies=[Depends(mutate)])
    async def prepare(body: CustomPrepare):
        try:
            await service.sell.start()
            return await service.engine.prepare(**body.model_dump())
        except (ValueError, NotSent) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, '계획 확인 실패 · 잔고와 연결을 확인하세요') from exc

    @router.post('/arm', dependencies=[Depends(mutate)])
    async def arm(body: CustomArm):
        try:
            await service.engine.arm(body.plan_id, body.environment, body.market_order)
        except (ValueError, NotSent) as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, '시작되지 않았습니다 · 연결과 주문 기록을 확인하세요') from exc
        return service.engine.snapshot()

    @router.post('/pause', dependencies=[Depends(mutate)])
    async def pause(body: CustomPause):
        service.engine.pause(body.code)
        return service.engine.snapshot()

    @router.post('/reconcile', dependencies=[Depends(mutate)])
    async def reconcile():
        await service.engine.reconcile()
        return service.engine.snapshot()

    @router.post('/tickets/prepare', dependencies=[Depends(mutate)])
    async def ticket_prepare(body: TicketPrepare):
        try:
            await service.sell.start()
            if body.after_hours in ('NXT','both'):
                await service.sell.get_feed().watch_codes([normal_code(body.code)+'_NX'])
            return await service.engine.prepare_ticket(**body.model_dump())
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409,'조건 확인 실패 · 잔고·연결을 확인하세요') from exc

    @router.post('/tickets/arm', dependencies=[Depends(mutate)])
    async def ticket_arm(body: TicketArm):
        try:
            await service.engine.arm_ticket(**body.model_dump())
            return service.engine.snapshot()
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409,'시작 실패 · 계좌·주문 기록을 확인하세요') from exc

    @router.post('/tickets/pause', dependencies=[Depends(mutate)])
    async def ticket_pause(body: TicketSelect):
        try:
            service.engine.pause_rule(**body.model_dump())
            return service.engine.snapshot()
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc

    @router.post('/tickets/remove', dependencies=[Depends(mutate)])
    async def ticket_remove(body: TicketSelect):
        try:
            service.engine.remove_rule(**body.model_dump())
            return service.engine.snapshot()
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc

    attach_cancel_routes(router,service,mutate)
    return router
