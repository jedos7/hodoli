"""🌅 호라이즌존 자동매수 서비스: 1초 루프 + 로컬 전용 API. 실제 전송은 엔진 settings.mode == 'live' 일 때만."""
import asyncio
import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from app.autoexit import PENDING
from app.horizonbuy import HorizonBuy, pick
from app.kiwoom.closebet_orders import CloseBetBroker
from app.kiwoom.orders import NotSent


class HorizonBuySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: str
    enabled: StrictBool
    slot_amount: StrictInt
    max_positions: StrictInt
    total_cap: StrictInt
    min_base_eok: StrictInt
    link_exit: StrictBool
    confirm_live: StrictBool = False


class HorizonBuyService:
    def __init__(self, settings, sell, horizon, exit_service, custom, auto_buy, closebet, notifier=None,
                 breakers=None, sizing=None, wallet=None):
        self.settings, self.sell, self.horizon, self.exit_service = settings, sell, horizon, exit_service
        self.task, self.token = None, secrets.token_urlsafe(32)
        self.notifier = notifier
        identity = hashlib.sha256((settings.kiwoom_env + settings.kiwoom_app_key).encode()).hexdigest()[:12]
        from app.autosell import now_kst
        self.engine = HorizonBuy(settings.data_dir / f'horizonbuy_{identity}.json', settings.kiwoom_env,
                                 self.broker, self.connected, exit_service.phase, sell.monitor, now_kst,
                                 candidates=self.candidates, exit_engine=lambda: exit_service.engine,
                                 peers=(exit_service.engine, custom.engine, auto_buy.engine, closebet.engine),
                                 notify=self.notify, breakers=breakers, sizing=sizing, wallet=wallet)

    def notify(self, text):
        if self.notifier and self.notifier.enabled:
            asyncio.create_task(self.notifier.send(text), name='horizonbuy-telegram')

    def candidates(self):
        """관제탑 판정 결과. 수집이 멈춰 있으면 빈 목록이 아니라 예외를 올려 '못 샀다'를 남긴다."""
        snap = self.horizon.snapshot()
        if snap.get('error'):
            raise RuntimeError(snap['error'])
        return snap['rows']

    def connected(self):
        return self.task is not None and not self.task.done() and self.exit_service.connected()

    def broker(self):
        feed = self.sell.get_feed()
        if not feed or not hasattr(feed, 'rest') or self.settings.broker != 'kiwoom':
            raise NotSent('키움 계좌 연결이 필요합니다')
        return CloseBetBroker(feed.rest, self.settings.kiwoom_env)

    async def start(self):
        self.engine.open()
        if not self.engine.error:
            self.task = asyncio.create_task(self.loop(), name='horizonbuy')

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
                if self.engine.settings['enabled']:
                    self.horizon.start()          # 12:00 판정을 쓰려면 관제탑 수집이 돌고 있어야 한다
                    if count % 30 == 0 and self.engine.live() and not self.connected():
                        try:
                            await self.sell.start()
                        except Exception as exc:
                            self.engine.day()['connect_error'] = f'계좌 감시 연결 실패: {str(exc)[:80]}'
                await self.engine.tick()
                if count % 30 == 0 and any(o['status'] in PENDING for o in self.engine.orders):
                    await self.engine.reconcile()
            except Exception:
                self.engine.stop(note='호라이즌 자동매수 처리 오류 · 새 전송 중단 · 키움 주문내역 확인')
            count += 1
            await asyncio.sleep(1)


def create_horizonbuy_router(service):
    router = APIRouter(prefix='/api/horizonbuy')

    def local(request: Request):
        if (request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient')
                or request.url.hostname not in ('127.0.0.1', 'localhost', '::1', 'testserver')):
            raise HTTPException(403, '이 PC의 로컬 주소에서만 설정할 수 있습니다')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, '요청 출처를 확인하세요')

    def mutate(request: Request, _: None = Depends(local)):
        if (request.headers.get('sec-fetch-site') == 'cross-site'
                or not secrets.compare_digest(request.headers.get('x-horizonbuy-token', ''), service.token)):
            raise HTTPException(403, '호라이즌 자동매수 화면을 다시 열어주세요')

    @router.get('', dependencies=[Depends(local)])
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.engine.snapshot() | {'token': service.token}

    @router.post('/settings', dependencies=[Depends(mutate)])
    async def update(body: HorizonBuySettings):
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

    @router.post('/preview', dependencies=[Depends(mutate)])
    async def preview():
        """지금 관제탑 자리로 '정오였다면 무엇을 샀을지' 미리 본다. 저장·주문 없음."""
        service.horizon.start()
        try:
            rows = service.candidates()
        except Exception as exc:
            raise HTTPException(409, f'관제탑 자리 조회 실패: {exc}') from exc
        ready = [r for r in rows if r.get('status') in ('ready', 'preview')]
        picks, skipped = pick([r | {'status': 'ready'} for r in ready], service.engine.settings['min_base_eok'])
        return {'at': service.engine.clock().isoformat(), 'readyCount': len(ready), 'picks': picks, 'skipped': skipped,
                'note': '11시 미리보기(preview) 자리까지 포함합니다. 실제 매수는 12:00 정각 판정만 씁니다.'}

    @router.post('/reconcile', dependencies=[Depends(mutate)])
    async def reconcile():
        await service.engine.reconcile()
        return service.engine.snapshot()

    return router
