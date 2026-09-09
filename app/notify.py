"""텔레그램 알림. 봇 토큰과 채팅 ID 만 있으면 된다.

  .env
    TELEGRAM_BOT_TOKEN=123456:ABC...   BotFather 가 준 토큰
    TELEGRAM_CHAT_ID=987654321         봇에게 아무 말이나 보낸 뒤 GET /api/notify/chatid 로 찾는다
    NOTIFY_KINDS=brk,stop,nxt          텔레그램으로 보낼 알림 종류 (brk 매수기준 돌파 · stop 손절 이탈 · nxt 종가배팅 후보 · sys 시스템)
    NOTIFY_DAILY=1                     스케줄 작업(스크리너·테마 수집) 끝난 뒤 한 줄 요약도 보낼지

토큰이 비어 있으면 아무것도 보내지 않고 조용히 지나간다. 전송 실패는 로그에만 남긴다.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self):
        self.token = settings.telegram_token
        self.chat_id = settings.telegram_chat_id
        self.kinds = {k.strip() for k in settings.notify_kinds.split(",") if k.strip()}
        self.daily = settings.notify_daily
        self.sent = 0
        self.last_error = ""
        self._last_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def status(self) -> dict:
        return {"enabled": self.enabled, "hasToken": bool(self.token), "hasChatId": bool(self.chat_id), "kinds": sorted(self.kinds),
                "daily": self.daily, "sent": self.sent, "lastError": self.last_error}

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        # 텔레그램은 초당 1건 정도가 안전. 너무 빠르면 잠깐 쉰다.
        gap = time.monotonic() - self._last_at
        if gap < 1.0:
            import asyncio
            await asyncio.sleep(1.0 - gap)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(API.format(token=self.token, method="sendMessage"),
                                 json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True})
            self._last_at = time.monotonic()
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}: {r.text[:120]}"
                log.warning("텔레그램 전송 실패 %s", self.last_error)
                return False
            self.sent += 1
            self.last_error = ""
            return True
        except Exception as e:
            self.last_error = str(e)
            log.warning("텔레그램 전송 실패: %s", e)
            return False

    async def send_alert(self, a: dict) -> bool:
        if a.get("kind") not in self.kinds:
            return False
        icon = {"brk": "🟢", "stop": "🔴", "nxt": "🌙", "sys": "ℹ️"}.get(a.get("kind"), "•")
        return await self.send(f"{icon} {a.get('text', '')}\n{a.get('at', '')}")

    async def find_chat_id(self) -> dict:
        """봇에게 메시지를 보낸 사람의 채팅 ID 를 getUpdates 로 찾는다."""
        if not self.token:
            return {"error": "TELEGRAM_BOT_TOKEN 이 비어 있습니다"}
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(API.format(token=self.token, method="getUpdates"))
            d = r.json()
        except Exception as e:
            return {"error": str(e)}
        if not d.get("ok"):
            return {"error": d.get("description", "getUpdates 실패")}
        chats = {}
        for u in d.get("result", []):
            m = u.get("message") or u.get("edited_message") or {}
            ch = m.get("chat") or {}
            if ch.get("id"):
                chats[str(ch["id"])] = {"name": " ".join(x for x in (ch.get("first_name"), ch.get("last_name"), ch.get("username")) if x), "text": (m.get("text") or "")[:40]}
        if not chats:
            return {"error": "봇에게 보낸 메시지가 없습니다. 텔레그램에서 봇을 찾아 아무 말이나 보낸 뒤 다시 하세요.", "chats": {}}
        return {"chats": chats}


notifier = Telegram()
