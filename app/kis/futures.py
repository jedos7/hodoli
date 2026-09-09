"""코스피200 선물 외국인 순매수 (계약 수).

KIS 의 '시장별 투자자매매동향' 계열 API 로 읽는다. 어느 거래 ID 와 시장 코드가 선물을 돌려주는지는
계정 종류·문서 버전에 따라 다를 수 있어 경로·거래 ID·파라미터·필드명을 모두 .env 에서 바꿀 수 있게 했다.

  KIS_FUT_PATH    기본 /uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market
  KIS_FUT_TR_ID   기본 FHPTJ04040000  (시장별 투자자매매동향 · 일별)
  KIS_FUT_PARAMS  기본 {"FID_INPUT_ISCD": "F001", "FID_INPUT_DATE_1": "{today}"}   ({today} 는 YYYYMMDD 로 치환)
  KIS_FUT_FIELD   기본 frgn_ntby_qty  (없으면 frgn_shnu_vol - frgn_seln_vol 로 계산)

맞는지 확인하려면:  py scripts/probe_kis.py  — 원본 응답을 그대로 출력한다.
응답 목록의 첫 행을 '당일(잠정)', 둘째 행을 '전일' 로 본다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from app.config import Settings
from app.kis.rest import KisRest


@dataclass(slots=True)
class ForeignFutures:
    today: int | None     # 당일 순매수 계약 (장중 잠정)
    prev: int | None      # 전일 순매수 계약
    today_date: str = ""
    prev_date: str = ""
    src: str = "KIS"
    error: str | None = None


def _rows(body: dict) -> list[dict]:
    for k in ("output", "output1", "output2"):
        v = body.get(k)
        if isinstance(v, list) and v:
            return v
        if isinstance(v, dict) and v:
            return [v]
    return []


def _net(row: dict, field: str) -> int | None:
    def num(k):
        v = row.get(k)
        if v in (None, ""):
            return None
        try:
            return int(float(str(v).replace(",", "")))
        except ValueError:
            return None

    v = num(field)
    if v is not None:
        return v
    buy, sell = num("frgn_shnu_vol"), num("frgn_seln_vol")
    if buy is not None and sell is not None:
        return buy - sell
    return None


def extract_foreign_net(body: dict, field: str = "frgn_ntby_qty") -> ForeignFutures:
    rows = _rows(body)
    if not rows:
        return ForeignFutures(None, None, error="응답에 행이 없습니다")
    today = _net(rows[0], field)
    prev = _net(rows[1], field) if len(rows) > 1 else None
    if today is None and prev is None:
        return ForeignFutures(None, None, error=f"필드 {field} 를 찾지 못했습니다 (행 키: {', '.join(list(rows[0])[:8])}…)")
    return ForeignFutures(today, prev, str(rows[0].get("stck_bsop_date", "")), str(rows[1].get("stck_bsop_date", "")) if len(rows) > 1 else "")


class KisFutures:
    def __init__(self, rest: KisRest, settings: Settings):
        self.rest = rest
        self.s = settings

    def params(self) -> dict:
        raw = json.loads(self.s.fut_params)
        today = date.today().strftime("%Y%m%d")
        return {k: (v.replace("{today}", today) if isinstance(v, str) else v) for k, v in raw.items()}

    async def raw(self) -> dict:
        return await self.rest._get(self.s.fut_path, self.s.fut_tr_id, self.params())

    async def foreign_net(self) -> ForeignFutures:
        try:
            body = await self.raw()
        except Exception as e:
            return ForeignFutures(None, None, error=str(e))
        return extract_foreign_net(body, self.s.fut_field)
