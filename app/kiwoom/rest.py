"""키움 REST API 조회.

모든 조회는 POST {base}/api/dostk/{분류} 이고, 어떤 조회인지는 헤더 api-id 로 정한다.
  헤더: Content-Type application/json;charset=UTF-8 · authorization Bearer {token} · api-id kaXXXXX · cont-yn N · next-key ''
  연속조회: 응답 헤더 cont-yn=Y 이면 next-key 를 다음 요청 헤더에 넣는다.
쓰는 조회
  ka10001 주식기본정보   /api/dostk/stkinfo   {"stk_cd"}
  ka10081 주식일봉차트   /api/dostk/chart     {"stk_cd","base_dt","upd_stkpc_tp":"1"}   (1 = 수정주가)
  ka10059 종목별투자자   /api/dostk/stkinfo   {"dt","stk_cd","amt_qty_tp":"1","trde_tp":"0","unit_tp":"1000"}  (금액·순매수·천원)
필드명이 문서와 다르면 scripts/probe_kiwoom.py 로 원본을 보고 parse.py 를 고친다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

import httpx

from app.config import Settings
from app.kiwoom.auth import KiwoomAuth
from app.kiwoom.parse import BasicInfo, parse_basic, parse_daily, parse_investor
from app.kis.rest import Candle

log = logging.getLogger(__name__)


class KiwoomRest:
    def __init__(self, settings: Settings, auth: KiwoomAuth):
        self.s = settings
        self.auth = auth
        self.client = httpx.AsyncClient(base_url=settings.kiwoom_base, timeout=10)
        self._gap = 1.0 / settings.kiwoom_rps
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def post(self, path: str, api_id: str, body: dict, next_key: str = "") -> tuple[dict, str]:
        """(응답 JSON, 다음 next-key 또는 '')"""
        token = await self.auth.access_token()
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
            "cont-yn": "Y" if next_key else "N",
            "next-key": next_key,
        }
        async with self._lock:
            wait = self._gap - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        r = await self.client.post(path, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        rc = str(data.get("return_code", "0"))
        if rc not in ("0", "None"):
            raise RuntimeError(f"키움 {api_id} 오류 {rc}: {data.get('return_msg')}")
        nk = r.headers.get("next-key", "") if r.headers.get("cont-yn") == "Y" else ""
        return data, nk

    async def basic(self, code: str) -> BasicInfo:
        data, _ = await self.post("/api/dostk/stkinfo", "ka10001", {"stk_cd": code})
        return parse_basic(data, self.s.kiwoom_amount_unit)

    async def daily_candles(self, code: str, days: int = 250) -> list[Candle]:
        out: list[Candle] = []
        base_dt = date.today().strftime("%Y%m%d")
        nk = ""
        for _ in range(10):  # 연속조회 최대 10회
            data, nk = await self.post("/api/dostk/chart", "ka10081", {"stk_cd": code, "base_dt": base_dt, "upd_stkpc_tp": "1"}, nk)
            chunk = parse_daily(data, self.s.kiwoom_amount_unit)
            out = chunk + out if chunk else out
            if not nk or len(out) >= days:
                break
        # 중복 제거 후 오래된 순
        uniq = {c.date: c for c in out}
        return [uniq[d] for d in sorted(uniq)][-days:]

    async def snapshot(self, code: str) -> dict | None:
        """ka10059 한 번으로 현재가·전일종가·누적거래대금·외국인/기관/개인 순매수(금액, 원) 를 받는다.
        첫 행이 당일이면 장중 잠정치. ka10001 에는 거래대금이 없어서 시작 시 초기값은 이걸 쓴다."""
        data, _ = await self.post("/api/dostk/stkinfo", "ka10059",
                                  {"dt": date.today().strftime("%Y%m%d"), "stk_cd": code, "amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000"})
        return parse_investor(data, self.s.kiwoom_amount_unit)
