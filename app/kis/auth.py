"""한국투자증권 OAuth.

- 접근토큰(access_token): REST 호출용. 24시간 유효. 앱당 1개이며 재발급을 자주 하면 제한에 걸리므로
  파일에 캐시해 두고 만료 1시간 전까지는 재사용한다.
- 웹소켓 접속키(approval_key): 실시간 체결·호가 구독용. 접속할 때마다 새로 받아도 된다.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class KisAuth:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._load_cache()

    # ── 접근토큰 ──────────────────────────────────────────────
    async def access_token(self) -> str:
        if self._token and time.time() < self._expires_at - 3600:
            return self._token
        async with httpx.AsyncClient(base_url=self.s.rest_base, timeout=10) as c:
            r = await c.post(
                "/oauth2/tokenP",
                json={"grant_type": "client_credentials", "appkey": self.s.app_key, "appsecret": self.s.app_secret},
            )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 86400))
        self._save_cache()
        log.info("KIS 접근토큰 발급 (env=%s)", self.s.env)
        return self._token

    # ── 웹소켓 접속키 ────────────────────────────────────────
    async def approval_key(self) -> str:
        async with httpx.AsyncClient(base_url=self.s.rest_base, timeout=10) as c:
            r = await c.post(
                "/oauth2/Approval",
                json={"grant_type": "client_credentials", "appkey": self.s.app_key, "secretkey": self.s.app_secret},
            )
        r.raise_for_status()
        return r.json()["approval_key"]

    # ── 캐시 ─────────────────────────────────────────────────
    def _load_cache(self) -> None:
        p: Path = self.s.token_cache
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text("utf-8"))
            if d.get("app_key") == self.s.app_key:
                self._token, self._expires_at = d["token"], float(d["expires_at"])
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        p: Path = self.s.token_cache
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"app_key": self.s.app_key, "token": self._token, "expires_at": self._expires_at}), "utf-8")
