"""키움 실시간 WebSocket.

  접속 → {"trnm":"LOGIN","token":…} → 응답 return_code 0
  구독 → {"trnm":"REG","grp_no":"1","refresh":"1","data":[{"item":[코드…],"type":["0B"]}]}   (0B 주식체결)
  수신 → {"trnm":"REAL","data":[{"type":"0B","item":"005930","values":{"10":…}}]}
  PING → {"trnm":"PING"} 을 받으면 그대로 돌려준다.
끊기면 지수 백오프로 재접속하고 다시 구독한다. 그룹당 종목 수는 MAX_ITEMS 로 나눈다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import websockets

from app.config import Settings
from app.kiwoom.auth import KiwoomAuth
from app.kiwoom.parse import parse_real
from app.kis.parse import Trade

log = logging.getLogger(__name__)
MAX_ITEMS = 100


class KiwoomWebSocket:
    def __init__(self, settings: Settings, auth: KiwoomAuth, on_trade: Callable[[Trade], None]):
        self.s = settings
        self.auth = auth
        self.on_trade = on_trade
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, codes: list[str]) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                token = await self.auth.access_token()
                async with websockets.connect(self.s.kiwoom_ws_url, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
                    login = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if str(login.get("return_code", "0")) not in ("0", "None"):
                        raise RuntimeError(f"키움 WS 로그인 실패: {login.get('return_msg')}")
                    for i in range(0, len(codes), MAX_ITEMS):
                        group = codes[i : i + MAX_ITEMS]
                        await ws.send(json.dumps({"trnm": "REG", "grp_no": str(i // MAX_ITEMS + 1), "refresh": "1",
                                                  "data": [{"item": group, "type": ["0B"]}]}))
                    log.info("키움 WS 접속 · %d 종목 구독", len(codes))
                    backoff = 1
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle(ws, raw)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                log.warning("키움 WS 끊김: %s → %ds 후 재접속", e, backoff)
            except Exception:
                log.exception("키움 WS 오류 → %ds 후 재접속", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        t = msg.get("trnm")
        if t == "PING":
            await ws.send(raw)
        elif t == "REAL":
            for tr in parse_real(msg, self.s.kiwoom_amount_unit):
                self.on_trade(tr)
        elif t == "REG":
            if str(msg.get("return_code", "0")) not in ("0", "None"):
                log.warning("키움 구독 응답 오류: %s", msg.get("return_msg"))
