"""네이버 금융 일봉 · 종목 목록. KIS 키 없이 스크리너를 실제 일봉으로 돌리기 위한 소스.

일봉:  https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=day&count=250&requestType=0
       XML 한 번에 최대 수백 봉. 항목 = 날짜|시가|고가|저가|종가|거래량 (수정주가). 거래대금은 없어서 거래량×종가로 근사한다.
목록:  https://m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ}?page=N&pageSize=100   (JSON, 시가총액 순)
       현재가·거래량·거래대금이 같이 있어 '오늘 거래대금 ≥ N억' 으로 미리 걸러 요청 수를 줄인다.
       (옛 finance.naver.com/sise/sise_market_sum.naver 는 2026-09-11 부터 302 로 넘어가 표가 없다)
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass

import httpx

from app.collectors.naver_api import NaverApi, num
from app.kis.rest import Candle

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
DELAY = 0.08
_ITEM = re.compile(r'<item data="(\d{8})\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)"')
_SKIP_NAME = re.compile(r"스팩|\d+호$|우$|우[A-C]$|\(전환\)|ETN|KODEX|TIGER|KBSTAR|ACE |SOL |PLUS |HANARO|ARIRANG|KOSEF")


@dataclass(slots=True)
class Listed:
    code: str
    name: str
    market: str        # 코스피 | 코스닥
    price: int
    volume: int
    amount_million: int = 0   # 오늘 거래대금(백만원). 0 이면 현재가×거래량으로 근사

    @property
    def amount_eok(self) -> float:
        return self.amount_million / 100 if self.amount_million else self.price * self.volume / 1e8


def parse_fchart(xml: str) -> list[Candle]:
    out = []
    for d, o, h, l, c, v in _ITEM.findall(xml):
        o, h, l, c, v = int(o), int(h), int(l), int(c), int(v)
        if c <= 0:
            continue
        out.append(Candle(d, o, h, l, c, v, v * c))
    return out


def parse_market_list(d: dict, market: str) -> list[Listed]:
    """/api/stocks/marketValue/{KOSPI|KOSDAQ} 한 쪽 → Listed. 거래정지 종목은 뺀다."""
    out = []
    for r in d.get("stocks") or []:
        code = str(r.get("itemCode") or "")
        if len(code) != 6 or r.get("stockEndType", "stock") != "stock":
            continue
        stop = (r.get("tradeStopType") or {}).get("name")
        if stop and stop != "TRADING":
            continue
        raw = r.get("accumulatedTradingValueRaw")
        amount_m = int(num(raw) // 1_000_000) if raw not in (None, "") else int(num(r.get("accumulatedTradingValue")))
        out.append(Listed(code, html.unescape(str(r.get("stockName", ""))).strip(), market, int(num(r.get("closePrice"))),
                          int(num(r.get("accumulatedTradingVolume"))), amount_m))
    return out


class NaverDaily(NaverApi):
    async def candles(self, code: str, count: int = 250) -> list[Candle]:
        r = await self.client.get("https://fchart.stock.naver.com/sise.nhn",
                                  params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"})
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return parse_fchart(r.content.decode("euc-kr", "replace"))

    async def market_list(self, min_amount_eok: float = 30.0, skip_special: bool = True) -> list[Listed]:
        """코스피+코스닥 전 종목 중 오늘 거래대금이 min_amount_eok 억 이상인 것 (거래대금 순)."""
        out: list[Listed] = []
        for key, market in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            rows: list[Listed] = []
            for page in range(1, 60):
                d = await self.json(f"/stocks/marketValue/{key}", {"page": page, "pageSize": 100})
                rows += parse_market_list(d, market)
                got = len(d.get("stocks") or [])
                if got < 100 or (d.get("totalCount") and page * 100 >= int(d["totalCount"])):
                    break
            log.info("%s 목록 %d종목", market, len(rows))
            out += rows
        picked = [r for r in out if r.amount_eok >= min_amount_eok and not (skip_special and _SKIP_NAME.search(r.name))]
        picked.sort(key=lambda r: r.amount_eok, reverse=True)
        return picked
