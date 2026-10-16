"""종가배팅 자동매매 서비스: 1초 루프 + 로컬 전용 API. 실제 전송은 엔진 settings.mode == 'live' 일 때만."""
import asyncio
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from app.autoexit import PENDING
from app.closebet import CloseBet
from app.kiwoom.closebet_orders import CloseBetBroker
from app.kiwoom.orders import NotSent


class CloseBetSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: str
    enabled: StrictBool
    per_trade_amount: StrictInt
    max_positions: StrictInt
    total_cap: StrictInt
    threshold_eok: StrictInt
    sell_time: str
    confirm_live: StrictBool = False


class CloseBetService:
    def __init__(self, settings, sell, horizon, custom, auto_buy, notifier=None):
        self.settings, self.sell, self.horizon = settings, sell, horizon
        self.task, self.token = None, secrets.token_urlsafe(32)
        identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
        from app.autosell import now_kst
        self.engine = CloseBet(settings.data_dir / f'closebet_{identity}.json', settings.kiwoom_env, self.broker, self.connected,
                               horizon.phase, sell.monitor, now_kst, peers=(horizon.engine, custom.engine, auto_buy.engine), notify=self.notify)
        self.notifier = notifier

    def notify(self, text):
        if self.notifier and self.notifier.enabled:
            asyncio.create_task(self.notifier.send(text), name='closebet-telegram')

    def connected(self):
        return self.task is not None and not self.task.done() and self.horizon.connected()

    def broker(self):
        feed = self.sell.get_feed()
        if not feed or not hasattr(feed, 'rest') or self.settings.broker != 'kiwoom':
            raise NotSent('키움 계좌 연결이 필요합니다')
        return CloseBetBroker(feed.rest, self.settings.kiwoom_env)

    async def start(self):
        self.engine.open()
        if not self.engine.error:
            self.task = asyncio.create_task(self.loop(), name='closebet')

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.engine.tasks:
            await asyncio.gather(*list(self.engine.tasks), return_exceptions=True)
        self.engine.close_lock()

    async def loop(self):
        count = 0
        while True:
            try:
                if count % 30 == 0 and self.engine.live() and self.engine.settings['enabled'] and not self.connected():
                    # 실전 전송은 계좌 실시간 감시(잔고·체결 이벤트)가 붙어 있어야 한다. 사용자가 다른 화면을 열지 않아도 여기서 붙인다.
                    try:
                        await self.sell.start()
                    except Exception as exc:
                        self.engine.day()['connect_error'] = f'계좌 감시 연결 실패: {str(exc)[:80]}'
                await self.engine.tick()
                if count % 30 == 0 and any(o['status'] in PENDING for o in self.engine.orders):
                    await self.engine.reconcile()
            except Exception:
                self.engine.stop(note='종가배팅 처리 오류 · 새 전송 중단 · 키움 주문내역 확인')
            count += 1
            await asyncio.sleep(1)


def create_closebet_router(service):
    router = APIRouter(prefix='/api/closebet')

    def local(request: Request):
        if (request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver')):
            raise HTTPException(403, '이 PC의 로컬 주소에서만 설정할 수 있습니다')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    def mutate(request: Request, _: None = Depends(local)):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or not secrets.compare_digest(request.headers.get('x-closebet-token', ''), service.token)):
            raise HTTPException(403, '종가배팅 화면을 다시 열어주세요')

    @router.get('', dependencies=[Depends(local)])
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.engine.snapshot() | {'token': service.token}

    @router.post('/settings', dependencies=[Depends(mutate)])
    async def update(body: CloseBetSettings):
        raw = body.model_dump()
        confirm = raw.pop('confirm_live')
        if raw['mode'] == 'live' and not confirm:
            raise HTTPException(409, '실전 전송을 켜려면 확인란을 선택하세요')
        try:
            service.engine.update_settings(raw)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return service.engine.snapshot()

    @router.post('/stop', dependencies=[Depends(mutate)])
    async def stop():
        service.engine.stop()
        return service.engine.snapshot()

    @router.post('/scan', dependencies=[Depends(mutate)])
    async def scan():
        """지금 시점 자료로 후보를 미리 본다. 저장하지 않고 매수하지 않는다."""
        if service.engine.scan_task:
            raise HTTPException(409, '15:15 후보 선정이 진행 중입니다')
        try:
            return await service.engine.scan(preview=True)
        except Exception as exc:
            raise HTTPException(409, f'후보 선정 실패: {exc}') from exc

    @router.post('/reconcile', dependencies=[Depends(mutate)])
    async def reconcile():
        await service.engine.reconcile()
        return service.engine.snapshot()

    return router
