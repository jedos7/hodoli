"""시장 지수 패널 재료 (왼쪽 위 '시장 지수' · '코스피/코스닥 당일 수급').

처음 시제품 때 넣은 고정 숫자(코스피 7,069 …)가 2026-09-11 까지 그대로 화면에 남아 있었다. 이제 실제 값만 보여준다.
  지수·거래대금·시가/고가/저가:  https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ,KPI200,FUT   (키 불필요, 1분 지연 없음)
  상승/하락 종목 수 · 투자자별 순매수(개인/외국인/기관, 억) · 프로그램 순매수:  https://m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/integration
  투자자 세부(금투·투신·연기금 등):  키움 ka10051 업종별 투자자 순매수 (/api/dostk/sect, 억원) — 키움 피드일 때만. 네이버 3분류와 값이 같다
  코스피200 선물 베이시스 = 선물 - 코스피200 (양수 콘탱고, 음수 백워데이션)
  ADR(20일) = 최근 20거래일 상승 종목 수 합 / 하락 종목 수 합 × 100. 스크리너 일봉 캐시(data/candles_kiwoom_market.json, 거래대금 30억↑ 종목)로 계산.
     120% 위면 과열, 75% 아래면 과매도로 흔히 읽는다. 전 종목이 아니라 캐시 종목 기준이라 증권사 화면 값과 조금 다르다.
없는 것: VKOSPI(네이버 API 에 없음), 예탁금·신용잔고(일 단위 금투협 자료, 붙이지 않음). 옛 화면의 PCR·콜/풋·미결제 칸은 소스가 없어 뺐다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

from app.collectors.naver_api import HEADERS, num
from app.config import settings

log = logging.getLogger(__name__)

POLL = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ,KPI200,FUT"
INTEGRATION = "https://m.stock.naver.com/api/index/{code}/integration"
NAMES = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "KPI200": "코스피200", "FUT": "코스피200 선물"}
# 키움 ka10051 필드 → 표시 이름. 코스피는 5줄, 코스닥은 4줄 (참고 화면과 같은 구성)
KIWOOM_FLOWS = {"KOSPI": [("ind_netprps", "개인"), ("frgnr_netprps", "외인"), ("orgn_netprps", "기관"), ("sc_netprps", "금투"), ("endw_netprps", "연기금")],
                "KOSDAQ": [("ind_netprps", "개인"), ("frgnr_netprps", "외인"), ("orgn_netprps", "기관"), ("invtrt_netprps", "투신")]}


def parse_polling(payload: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in payload.get("datas") or []:
        code = d.get("itemCode")
        if code not in NAMES:
            continue
        out[code] = {
            "name": NAMES[code], "price": num(d.get("closePrice")), "chg": num(d.get("fluctuationsRatio")),
            "diff": num(d.get("compareToPreviousClosePrice")),
            "open": num(d.get("openPriceRaw") or d.get("openPrice")), "high": num(d.get("highPriceRaw") or d.get("highPrice")),
            "low": num(d.get("lowPriceRaw") or d.get("lowPrice")),
            "amountJo": round(num(d.get("accumulatedTradingValue")) / 1e6, 2),   # '19,674,136백만' → 조
            "status": d.get("marketStatus"), "at": str(d.get("localTradedAt", ""))[11:16],
        }
    return out


def parse_integration(d: dict) -> dict:
    ud = d.get("upDownStockInfo") or {}
    deal = d.get("dealTrendInfo") or {}
    prog = d.get("programTrendInfo") or {}
    return {
        "rise": int(num(ud.get("riseCount"))), "fall": int(num(ud.get("fallCount"))), "flat": int(num(ud.get("steadyCount"))),
        "upper": int(num(ud.get("upperCount"))), "lower": int(num(ud.get("lowerCount"))),
        "investors": [["개인", num(deal.get("personalValue"))], ["외인", num(deal.get("foreignValue"))], ["기관", num(deal.get("institutionalValue"))]],
        "program": num(prog.get("indexTotalReal")) if prog.get("indexTotalReal") is not None else None,
        "bizdate": deal.get("bizdate"),
    }


def parse_kiwoom_investors(body: dict, market: str) -> list[list] | None:
    rows = body.get("inds_netprps") or []
    if not rows:
        return None
    row = rows[0]
    return [[label, num(row.get(key))] for key, label in KIWOOM_FLOWS[market]]


def adr(candles: dict[str, list], days: int = 20) -> tuple[float | None, int]:
    """(ADR %, 날 수). candles = {code: [[date,o,h,l,c,v,a], ...]} 또는 Candle 목록."""
    by_day: dict[str, list[int]] = {}
    for cs in candles.values():
        prev = None
        for c in cs:
            date, close = (c[0], c[4]) if isinstance(c, (list, tuple)) else (c.date, c.close)
            if prev and close and prev:
                up_down = by_day.setdefault(date, [0, 0])
                if close > prev:
                    up_down[0] += 1
                elif close < prev:
                    up_down[1] += 1
            prev = close or prev
    use = sorted(by_day)[-days:]
    ups = sum(by_day[d][0] for d in use)
    downs = sum(by_day[d][1] for d in use)
    return (round(ups / downs * 100, 1) if downs else None), len(use)


_adr_cache: dict = {"mtime": None, "value": (None, 0)}


def adr_from_cache(path: Path | None = None) -> tuple[float | None, int]:
    p = path or (settings.data_dir / "candles_kiwoom_market.json")
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None, 0
    if _adr_cache["mtime"] != mtime:
        try:
            d = json.loads(p.read_text("utf-8"))
            _adr_cache.update(mtime=mtime, value=adr(d.get("candles") or {}))
        except Exception as e:
            log.debug("ADR 계산 실패: %s", e)
            _adr_cache.update(mtime=mtime, value=(None, 0))
    return _adr_cache["value"]


async def collect(rest=None, client: httpx.AsyncClient | None = None) -> dict:
    """rest = 키움 KiwoomRest (있으면 투자자 세부를 키움에서). 네이버가 안 되면 예외."""
    own = client is None
    client = client or httpx.AsyncClient(headers=HEADERS, timeout=15)
    try:
        r = await client.get(POLL)
        r.raise_for_status()
        idx = parse_polling(r.json())
        if "KOSPI" not in idx or "KOSDAQ" not in idx:
            raise RuntimeError("네이버 지수 응답에 코스피·코스닥이 없습니다")
        for code in ("KOSPI", "KOSDAQ"):
            try:
                r = await client.get(INTEGRATION.format(code=code))
                r.raise_for_status()
                idx[code].update(parse_integration(r.json()))
            except Exception as e:
                log.warning("지수 상세(%s) 실패: %s", code, e)
        src = "네이버"
        if rest is not None:
            for code, tp in (("KOSPI", "0"), ("KOSDAQ", "1")):
                try:
                    body, _ = await rest.post("/api/dostk/sect", "ka10051", {"mrkt_tp": tp, "amt_qty_tp": "0", "base_dt": "", "stex_tp": "1"})
                    flows = parse_kiwoom_investors(body, code)
                    if flows:
                        idx[code]["investors"] = flows
                        src = "키움"
                except Exception as e:
                    log.debug("키움 투자자(%s) 실패: %s", code, e)
    finally:
        if own:
            await client.aclose()
    fut, k200 = idx.get("FUT"), idx.get("KPI200")
    basis = round(fut["price"] - k200["price"], 2) if fut and k200 and fut["price"] and k200["price"] else None
    adr_v, adr_days = await asyncio.to_thread(adr_from_cache)
    return {"asof": datetime.now().isoformat(timespec="seconds"), "kospi": idx["KOSPI"], "kosdaq": idx["KOSDAQ"],
            "kospi200": k200, "futures": fut, "basis": basis, "adr20": adr_v, "adrDays": adr_days,
            "investorsSrc": src, "status": idx["KOSPI"].get("status")}
