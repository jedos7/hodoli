"""네이버 증권 새 JSON API 공통 (m.stock.naver.com/api).

2026-09-11 부터 finance.naver.com 의 옛 HTML 페이지(테마 목록·테마 상세·시가총액 목록·리서치)가 stock.naver.com 으로 302 넘어가
표가 사라졌다. 새 사이트가 화면을 그릴 때 부르는 JSON API 를 그대로 쓴다. 키가 필요 없고 응답이 JSON 이라 정규식 파싱이 없다.
아직 옛 주소가 살아 있는 것: fchart.stock.naver.com(일봉), sise/investorDealTrendDay(선물 투자자), m.stock.naver.com/api/news(뉴스).
"""
from __future__ import annotations

import asyncio
import re

import httpx

API = "https://m.stock.naver.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.stock.naver.com/",
}
DELAY = 0.15  # 요청 간격(초)

_UNIT = re.compile(r"[,%+원주]|백만|천|억|조")


def num(v, default: float = 0.0) -> float:
    """'19,674,136백만' · '+7.69' · '-1.95%' · 'N/A' · None → 숫자. 단위 글자는 떼고 숫자만 본다 (단위 환산은 호출자가)."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    s = _UNIT.sub("", str(v)).strip()
    try:
        return float(s)
    except ValueError:
        return default


def million_to_eok(v) -> float:
    return num(v) / 100


class NaverApi:
    """얇은 클라이언트. 컬렉터들이 공유한다."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=15)

    async def close(self) -> None:
        await self.client.aclose()

    async def json(self, path: str, params: dict | None = None):
        r = await self.client.get(API + path, params=params)
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return r.json()

    async def pages(self, path: str, key: str, page_size: int = 100, max_pages: int = 50, params: dict | None = None) -> list:
        """page/pageSize 로 넘기는 목록을 totalCount 까지 모두 받는다."""
        out: list = []
        for page in range(1, max_pages + 1):
            d = await self.json(path, {"page": page, "pageSize": page_size, **(params or {})})
            part = d.get(key) or [] if isinstance(d, dict) else d
            out += part
            total = int(d.get("totalCount", 0)) if isinstance(d, dict) else 0
            if not part or len(part) < page_size or (total and len(out) >= total):
                break
        return out
