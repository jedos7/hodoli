"""KIS REST 시세 조회. 필요한 것만 얇게 감쌌다.

호출 제한: 실전 초당 20건, 모의 초당 2건. 세마포어 + 최소 간격으로 맞춘다.
tr_id 는 실전/모의가 같은 조회 API 라 그대로 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.kis.auth import KisAuth

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Quote:
    code: str
    price: int
    prev_close: int
    change_rate: float
    open: int
    high: int
    low: int
    acc_volume: int
    acc_amount: int  # 원


@dataclass(slots=True)
class Candle:
    date: str  # YYYYMMDD
    open: int
    high: int
    low: int
    close: int
    volume: int
    amount: int  # 원


class KisRest:
    def __init__(self, settings: Settings, auth: KisAuth):
        self.s = settings
        self.auth = auth
        self.client = httpx.AsyncClient(base_url=settings.rest_base, timeout=10)
        per_sec = 18 if settings.env == "real" else 2
        self._gap = 1.0 / per_sec
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, tr_id: str, params: dict) -> dict:
        token = await self.auth.access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        async with self._lock:  # 호출 간격 유지
            wait = self._gap - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        r = await self.client.get(path, headers=headers, params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("rt_cd") != "0":
            raise RuntimeError(f"KIS {tr_id} 오류 {body.get('msg_cd')}: {body.get('msg1')}")
        return body

    # ── 현재가 ──
    async def quote(self, code: str) -> Quote:
        body = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
        )
        o = body["output"]
        price = int(o["stck_prpr"])
        return Quote(
            code=code,
            price=price,
            prev_close=price - int(o["prdy_vrss"]),
            change_rate=float(o["prdy_ctrt"]),
            open=int(o["stck_oprc"]),
            high=int(o["stck_hgpr"]),
            low=int(o["stck_lwpr"]),
            acc_volume=int(o["acml_vol"]),
            acc_amount=int(o["acml_tr_pbmn"]),
        )

    # ── 일봉 (최대 100건/호출, 최신순으로 옴 → 오래된 순으로 뒤집어 반환) ──
    async def daily_candles(self, code: str, start: str, end: str) -> list[Candle]:
        body = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",  # 0 수정주가
            },
        )
        rows = [r for r in body.get("output2", []) if r.get("stck_bsop_date")]
        out = [
            Candle(
                date=r["stck_bsop_date"],
                open=int(r["stck_oprc"]),
                high=int(r["stck_hgpr"]),
                low=int(r["stck_lwpr"]),
                close=int(r["stck_clpr"]),
                volume=int(r["acml_vol"]),
                amount=int(r["acml_tr_pbmn"]),
            )
            for r in rows
        ]
        out.sort(key=lambda c: c.date)
        return out

    # ── 외인·기관 장중 추정 순매수 (수량). 장중 09:30 부터 몇 차례 갱신. 모의투자 서버는 지원하지 않을 수 있다 ──
    async def investor_estimate(self, code: str) -> tuple[int, int, str] | None:
        """(외국인 추정 순매수 수량, 기관 추정 순매수 수량, 집계 구분) — 데이터 없으면 None"""
        body = await self._get(
            "/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
            "HHPTJ04160200",
            {"MKSC_SHRN_ISCD": code},
        )
        rows = body.get("output2") or []
        if not rows:
            return None
        r = rows[-1]  # 가장 최근 집계
        return int(r.get("frgn_fake_ntby_qty", 0)), int(r.get("orgn_fake_ntby_qty", 0)), str(r.get("bsop_hour_gb", ""))

    # ── 투자자별 매매동향 (일별 확정치, 최신 행 = 가장 최근 확정일) ──
    async def investor_daily(self, code: str) -> dict | None:
        body = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code},
        )
        rows = body.get("output") or []
        if not rows:
            return None
        r = rows[0]
        return {
            "date": r.get("stck_bsop_date", ""),
            "frgn_amount": int(r.get("frgn_ntby_tr_pbmn", 0)),  # 원
            "orgn_amount": int(r.get("orgn_ntby_tr_pbmn", 0)),
            "prsn_amount": int(r.get("prsn_ntby_tr_pbmn", 0)),
        }

    # ── 지수 (0001 코스피, 1001 코스닥, 2001 코스피200) ──
    async def index(self, code: str = "0001") -> dict:
        body = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000",
            {"fid_cond_mrkt_div_code": "U", "fid_input_iscd": code},
        )
        o = body["output"]
        return {
            "code": code,
            "value": float(o["bstp_nmix_prpr"]),
            "change_rate": float(o["bstp_nmix_prdy_ctrt"]),
            "acc_amount": int(o.get("acml_tr_pbmn", 0)),
        }
