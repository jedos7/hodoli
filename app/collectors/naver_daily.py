"""네이버 금융 일봉 · 종목 목록. KIS 키 없이 스크리너를 실제 일봉으로 돌리기 위한 소스.

일봉:  https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=day&count=250&requestType=0
       XML 한 번에 최대 수백 봉. 항목 = 날짜|시가|고가|저가|종가|거래량 (수정주가). 거래대금은 없어서 거래량×종가로 근사한다.
목록:  https://finance.naver.com/sise/sise_market_sum.naver?sosok=0|1&page=N   (0 코스피, 1 코스닥)
       현재가·거래량이 같이 있어 '오늘 거래대금 ≥ N억' 으로 미리 걸러 요청 수를 줄인다.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass

import httpx

from app.kis.rest import Candle

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
DELAY = 0.08
_ITEM = re.compile(r'<item data="(\d{8})\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)"')
_ROW_NAME = re.compile(r'<a href="/item/main\.naver\?code=(\d{6})" class="tltle">([^<]+)</a>')
_ROW_NUMS = re.compile(r'<td class="number"[^>]*>\s*(?:<em[^>]*>.*?</em>)?\s*(?:<span[^>]*>)?\s*([-+\d,.]+%?|N/A)', re.S)
_LAST_PAGE = re.compile(r'class="pgRR"[^>]*>\s*<a href="[^"]*page=(\d+)"')
_SKIP_NAME = re.compile(r"스팩|\d+호$|우$|우[A-C]$|\(전환\)|ETN|KODEX|TIGER|KBSTAR|ACE |SOL |PLUS |HANARO|ARIRANG|KOSEF")


@dataclass(slots=True)
class Listed:
    code: str
    name: str
    market: str        # 코스피 | 코스닥
    price: int
    volume: int

    @property
    def amount_eok(self) -> float:
        return self.price * self.volume / 1e8


def parse_fchart(xml: str) -> list[Candle]:
    out = []
    for d, o, h, l, c, v in _ITEM.findall(xml):
        o, h, l, c, v = int(o), int(h), int(l), int(c), int(v)
        if c <= 0:
            continue
        out.append(Candle(d, o, h, l, c, v, v * c))
    return out


def parse_market_page(page_html: str, market: str) -> list[Listed]:
    out = []
    for chunk in re.split(r"<tr\s+onMouseOver", page_html)[1:]:
        chunk = chunk.split("</tr>", 1)[0]
        m = _ROW_NAME.search(chunk)
        if not m:
            continue
        nums = _ROW_NUMS.findall(chunk)  # [현재가, 전일비, 등락률, 액면가, 시총, 상장주식수, 외국인비율, 거래량, PER, ROE]
        if len(nums) < 8:
            continue
        try:
            price = int(nums[0].replace(",", ""))
            volume = int(nums[7].replace(",", ""))
        except ValueError:
            continue
        name = html.unescape(m.group(2)).strip()
        out.append(Listed(m.group(1), name, market, price, volume))
    return out


def parse_last_page(page_html: str) -> int:
    m = _LAST_PAGE.search(page_html)
    return int(m.group(1)) if m else 1


class NaverDaily:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def candles(self, code: str, count: int = 250) -> list[Candle]:
        r = await self.client.get("https://fchart.stock.naver.com/sise.nhn",
                                  params={"symbol": code, "timeframe": "day", "count": count, "requestType": "0"})
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return parse_fchart(r.content.decode("euc-kr", "replace"))

    async def market_list(self, min_amount_eok: float = 30.0, skip_special: bool = True) -> list[Listed]:
        """코스피+코스닥 전 종목 중 오늘 거래대금(현재가×거래량) 이 min_amount_eok 억 이상인 것."""
        out: list[Listed] = []
        for sosok, market in (("0", "코스피"), ("1", "코스닥")):
            first = await self._market_page(sosok, 1)
            last = parse_last_page(first)
            rows = parse_market_page(first, market)
            for p in range(2, last + 1):
                rows += parse_market_page(await self._market_page(sosok, p), market)
            log.info("%s 목록 %d쪽 %d종목", market, last, len(rows))
            out += rows
        picked = [r for r in out if r.amount_eok >= min_amount_eok and not (skip_special and _SKIP_NAME.search(r.name))]
        picked.sort(key=lambda r: r.amount_eok, reverse=True)
        return picked

    async def _market_page(self, sosok: str, page: int) -> str:
        r = await self.client.get("https://finance.naver.com/sise/sise_market_sum.naver", params={"sosok": sosok, "page": page})
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return r.content.decode("euc-kr", "replace")
