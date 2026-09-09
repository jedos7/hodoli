"""KIS 실시간 웹소켓 클라이언트.

- 접속키(approval_key)로 연결한 뒤 종목별로 H0STCNT0(체결) 구독 메시지를 보낸다.
- 한 접속당 구독 한도는 약 41건. 종목이 더 많으면 접속을 나눠야 한다 (MAX_SUBS).
- 서버가 보내는 PINGPONG 은 받은 그대로 되돌려 준다.
- 끊기면 지수 백오프로 재접속하고 구독을 다시 건다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import websockets

from app.config import Settings
from app.kis.auth import KisAuth
from app.kis.parse import TR_TRADE, Trade, parse_trades

log = logging.getLogger(__name__)
MAX_SUBS = 40


class KisWebSocket:
    def __init__(self, settings: Settings, auth: KisAuth, on_trade: Callable[[Trade], None]):
        self.s = settings
        self.auth = auth
        self.on_trade = on_trade
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, codes: list[str]) -> None:
        """codes 를 MAX_SUBS 단위로 나눠 접속 하나씩 돌린다. 종료될 때까지 블록."""
        groups = [codes[i : i + MAX_SUBS] for i in range(0, len(codes), MAX_SUBS)]
        await asyncio.gather(*(self._run_one(g) for g in groups))

    async def _run_one(self, codes: list[str]) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                key = await self.auth.approval_key()
                async with websockets.connect(self.s.ws_url, ping_interval=None) as ws:
                    log.info("KIS WS 접속 (%d 종목)", len(codes))
                    backoff = 1
                    for code in codes:
                        await ws.send(self._sub_msg(key, code))
                        await asyncio.sleep(0.05)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle(ws, raw)
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("KIS WS 끊김: %s → %ds 후 재접속", e, backoff)
            except Exception:
                log.exception("KIS WS 오류 → %ds 후 재접속", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle(self, ws, raw: str) -> None:
        if raw.startswith("0|") or raw.startswith("1|"):
            for t in parse_trades(raw):
                self.on_trade(t)
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            log.debug("알 수 없는 메시지: %s", raw[:80])
            return
        tr_id = msg.get("header", {}).get("tr_id")
        if tr_id == "PINGPONG":
            await ws.send(raw)
        elif "body" in msg:
            b = msg["body"]
            if b.get("rt_cd") != "0":
                log.warning("구독 응답 오류 %s: %s", b.get("msg_cd"), b.get("msg1"))

    @staticmethod
    def _sub_msg(approval_key: str, code: str, tr_id: str = TR_TRADE) -> str:
        return json.dumps(
            {
                "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": tr_id, "tr_key": code}},
            }
        )
