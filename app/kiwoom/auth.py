"""키움 REST API 접근토큰.

POST {base}/oauth2/token  body {"grant_type":"client_credentials","appkey":…,"secretkey":…}
→ {"token": "...", "token_type": "bearer", "expires_dt": "YYYYMMDDHHMMSS", "return_code": 0, "return_msg": "..."}
토큰은 파일에 캐시하고 만료 1시간 전까지 재사용한다. 웹소켓 로그인에도 같은 토큰을 쓴다.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class KiwoomAuth:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._load_cache()

    async def access_token(self) -> str:
        if self._token and time.time() < self._expires_at - 3600:
            return self._token
        async with httpx.AsyncClient(base_url=self.s.kiwoom_base, timeout=10) as c:
            r = await c.post(
                "/oauth2/token",
                headers={"Content-Type": "application/json;charset=UTF-8"},
                json={"grant_type": "client_credentials", "appkey": self.s.kiwoom_app_key, "secretkey": self.s.kiwoom_app_secret},
            )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or str(body.get("return_code", 0)) not in ("0", "None"):
            raise RuntimeError(f"키움 토큰 발급 실패 HTTP {r.status_code}: {body.get('return_msg') or r.text[:200]}")
        self._token = body["token"]
        try:
            self._expires_at = datetime.strptime(body["expires_dt"], "%Y%m%d%H%M%S").timestamp()
        except (KeyError, ValueError):
            self._expires_at = time.time() + 86400
        self._save_cache()
        log.info("키움 접근토큰 발급 (env=%s)", self.s.kiwoom_env)
        return self._token

    def _load_cache(self) -> None:
        p: Path = self.s.kiwoom_token_cache
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text("utf-8"))
            if d.get("app_key") == self.s.kiwoom_app_key:
                self._token, self._expires_at = d["token"], float(d["expires_at"])
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        p: Path = self.s.kiwoom_token_cache
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"app_key": self.s.kiwoom_app_key, "token": self._token, "expires_at": self._expires_at}), "utf-8")
