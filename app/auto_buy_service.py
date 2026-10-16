"""Local user-operated buy setup; shared account feed, separate buy journal."""
import asyncio
import hashlib
import secrets
import time
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.auto_buy import AutoBuy
from app.autoexit import PENDING
from app.exit_coordination import CoordinatedBroker, ExecutionGroup
from app.kiwoom.buy_orders import KiwoomBuyOrders
from app.kiwoom.orders import NotSent


class BuyPrepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(pattern=r'^[0-9A-Z]{6}$')
    limit_price: StrictInt = Field(gt=0,lt=10**9)
    quantity: StrictInt = Field(gt=0,le=10**8)
    max_amount: StrictInt = Field(gt=0,le=10**12)
    expires_on: date
    take_profit_pct: Decimal | None = Field(None,gt=0,le=1000,decimal_places=2)
    stop_loss_pct: Decimal | None = Field(None,gt=0,lt=100,decimal_places=2)


class BuyArm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_id: str
    environment: str
    confirm_limit: StrictBool
    confirm_exit: StrictBool


class BuyPause(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str | None = None


class BuyCancel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_id: str


class BuyRemove(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    plan_id: str


class AutoBuyService:
    def __init__(self,settings,sell,horizon,custom):
        self.settings,self.sell,self.horizon,self.custom = settings,sell,horizon,custom
        self.task,self.token = None,secrets.token_urlsafe(32)
        self.stocks_cache, self.stocks_at = [], 0
        identity = hashlib.sha256((settings.kiwoom_env+settings.kiwoom_app_key).encode()).hexdigest()[:12]
        self.engine = AutoBuy(settings.data_dir/f'auto_buy_{identity}.json',settings.kiwoom_env,
            self.broker,self.connected,horizon.phase,sell.monitor,
            peers=(horizon.engine,custom.engine),custom=custom.engine)
        for service in (horizon,custom):
            original = service.engine.broker
            service.engine.broker = lambda original=original: CoordinatedBroker(original(),self.engine,'내 조건 자동매수')
        sell.execution = ExecutionGroup(horizon,custom,self)

    def connected(self):
        return self.task is not None and not self.task.done() and self.horizon.connected()

    def broker(self):
        feed = self.sell.get_feed()
        if not feed or not hasattr(feed,'rest') or self.settings.broker != 'kiwoom':
            raise NotSent('키움 계좌 연결이 필요합니다')
        return KiwoomBuyOrders(feed.rest,self.settings.kiwoom_env)

    async def stocks(self,query):
        query = query.strip().casefold()
        if not query or len(query)>30:
            return []
        if time.monotonic()-self.stocks_at > 600 or not self.stocks_cache:
            rest = self.broker().rest
            rows = []
            for market in ('0','10'):
                key,seen = '',set()
                for _ in range(100):
                    data,key = await rest.post('/api/dostk/stkinfo','ka10099',{'mrkt_tp':market},key)
                    if not isinstance(data.get('list'),list):
                        raise ValueError('종목 목록 응답 확인 필요')
                    rows.extend({'code':r['code'],'name':r['name']} for r in data['list'])
                    if not key:
                        break
                    if key in seen:
                        raise ValueError('종목 목록 연속조회 확인 필요')
                    seen.add(key)
                else:
                    raise ValueError('종목 목록 조회 미완료')
            self.stocks_cache = rows
            self.stocks_at = time.monotonic()
        return [r for r in self.stocks_cache if query in r['name'].casefold() or query in r['code'].casefold()][:30]

    async def start(self):
        self.engine.open()
        if not self.engine.error:
            self.task = asyncio.create_task(self.loop(),name='auto-buy-status')

    async def stop(self):
        self.engine.pause(note='서버 종료 · 자동매수와 체결 후 연결은 다시 확인 후 켜세요')
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.engine.tasks:
            await asyncio.gather(*list(self.engine.tasks),return_exceptions=True)
        self.engine.close_lock()

    def quote(self,trade,stamp):
        try:
            self.engine.quote(trade.code,trade.price,stamp,trade.market)
        except Exception:
            self.engine.pause(note='가격·주문 기록 오류 · 자동매수 중단')

    def account(self):
        self.engine.account_changed()

    async def loop(self):
        count = 0
        while True:
            try:
                self.engine.check()
                if count%5 == 0 and (any(o['status'] in PENDING for o in self.engine.orders)
                        or any(p['status']=='remainder_check' for p in self.engine.plans.values())):
                    await self.engine.reconcile()
            except Exception:
                self.engine.pause(note='자동매수 처리 오류 · 키움 주문내역 확인 필요')
            count += 1
            await asyncio.sleep(1)


def create_auto_buy_router(service):
    router = APIRouter(prefix='/api/auto-buy')

    def local(request:Request):
        if (request.client is None or request.client.host not in ('127.0.0.1','::1','testclient')
                or request.url.hostname not in ('127.0.0.1','localhost','::1','testserver')):
            raise HTTPException(403,'이 PC의 로컬 주소에서만 설정할 수 있습니다')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403,'요청 출처를 확인하세요')

    def mutate(request:Request,_:None=Depends(local)):
        if (request.headers.get('sec-fetch-site')=='cross-site' or not
                secrets.compare_digest(request.headers.get('x-auto-buy-token',''),service.token)):
            raise HTTPException(403,'자동매수 화면을 다시 열어주세요')

    @router.get('',dependencies=[Depends(local)])
    async def status(response:Response):
        response.headers['Cache-Control']='no-store'
        return service.engine.snapshot() | {'token':service.token}

    @router.post('/prepare',dependencies=[Depends(mutate)])
    async def prepare(body:BuyPrepare):
        try:
            await service.sell.start()
            # Watch ACK is required even for a stock outside themes/holdings.
            await service.sell.get_feed().watch_codes([body.code])
            return await service.engine.prepare(**body.model_dump())
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409,'매수 계획 조회 실패 · 연결·종목·주문가능 금액 확인 필요') from exc

    @router.get('/stocks',dependencies=[Depends(local)])
    async def stocks(q:str=''):
        try:
            return {'stocks':await service.stocks(q)}
        except Exception as exc:
            raise HTTPException(409,'종목 검색 실패 · 코드 6자리를 직접 입력할 수 있습니다') from exc

    @router.post('/arm',dependencies=[Depends(mutate)])
    async def arm(body:BuyArm):
        try:
            await service.engine.arm(**body.model_dump())
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409,'시작 결과 확인 필요 · 상태를 다시 조회하세요') from exc
        return service.engine.snapshot()

    @router.post('/pause',dependencies=[Depends(mutate)])
    async def pause(body:BuyPause):
        service.engine.pause(body.code)
        return service.engine.snapshot()

    @router.post('/cancel',dependencies=[Depends(mutate)])
    async def cancel(body:BuyCancel):
        try:
            await service.engine.cancel(body.order_id)
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        return service.engine.snapshot()

    @router.post('/remove',dependencies=[Depends(mutate)])
    async def remove(body:BuyRemove):
        try:
            result = await service.engine.remove_plan(body.code,body.plan_id)
        except (ValueError,NotSent) as exc:
            raise HTTPException(409,str(exc)) from exc
        return service.engine.snapshot() | result

    @router.post('/reconcile',dependencies=[Depends(mutate)])
    async def reconcile():
        await service.engine.reconcile()
        return service.engine.snapshot()

    return router
