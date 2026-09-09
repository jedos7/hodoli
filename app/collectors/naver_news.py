"""종목별 증권사 리포트 · 뉴스 (네이버 금융). 테마 카드의 '리포트 줄' 과 '뉴스 줄' 을 채운다.

리포트: https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode=005930
        표 = 종목명 · 제목 · 증권사 · 첨부 · 작성일(YY.MM.DD). 최근 N일만 센다.
뉴스:   https://m.stock.naver.com/api/news/stock/005930?pageSize=3&page=1
        JSON. 비슷한 기사끼리 묶음(cluster) 으로 오고, 묶음의 첫 기사가 대표 기사.

테마 리포트 줄 = 테마 종목 전체의 최근 N일 리포트 합계 (건수·증권사 수·가장 최근 것).
테마 뉴스 줄   = 대장주(거래대금 1위 또는 등락률 1위) 의 최신 기사 제목.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
DELAY = 0.12


@dataclass(slots=True)
class Report:
    title: str
    broker: str
    date: str   # YYYY-MM-DD
    url: str


@dataclass(slots=True)
class News:
    title: str
    press: str
    at: str     # YYYY-MM-DD HH:MM
    url: str


_REPORT_ROW = re.compile(
    r'<a href="(company_read\.naver\?nid=\d+[^"]*)">([^<]+)</a></td>\s*<td>([^<]+)</td>.*?<td class="date"[^>]*>\s*(\d{2}\.\d{2}\.\d{2})\s*</td>',
    re.S,
)


def parse_reports(page_html: str) -> list[Report]:
    out = []
    for href, title, broker, d in _REPORT_ROW.findall(page_html):
        yy, mm, dd = d.split(".")
        out.append(Report(html.unescape(title).strip(), html.unescape(broker).strip(), f"20{yy}-{mm}-{dd}",
                          "https://finance.naver.com/research/" + html.unescape(href)))
    return out


def parse_news(payload: list | dict) -> list[News]:
    clusters = payload if isinstance(payload, list) else payload.get("items", [payload])
    out = []
    for c in clusters:
        items = c.get("items", [c]) if isinstance(c, dict) else []
        if not items:
            continue
        it = items[0]
        dt = str(it.get("datetime", ""))
        at = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]} {dt[8:10]}:{dt[10:12]}" if len(dt) >= 12 else dt
        title = html.unescape(str(it.get("titleFull") or it.get("title") or "")).strip()
        if not title:
            continue
        out.append(News(title, str(it.get("officeName", "")), at, str(it.get("mobileNewsUrl", ""))))
    return out


def report_line(reports: list[Report], days: int) -> str:
    if not reports:
        return f"리포트 {days}일 없음"
    brokers = {r.broker for r in reports}
    latest = max(reports, key=lambda r: r.date)
    return f"리포트 {days}일 {len(reports)}건 · 증권사 {len(brokers)}곳 · 최근 {latest.broker.replace('증권', '')} {latest.date[5:].replace('-', '/')}"


class NaverNews:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def reports(self, code: str, days: int = 7) -> list[Report]:
        r = await self.client.get("https://finance.naver.com/research/company_list.naver",
                                  params={"searchType": "itemCode", "itemCode": code})
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        since = (date.today() - timedelta(days=days)).isoformat()
        return [x for x in parse_reports(r.content.decode("euc-kr", "replace")) if x.date >= since]

    async def news(self, code: str, n: int = 3) -> list[News]:
        r = await self.client.get(f"https://m.stock.naver.com/api/news/stock/{code}", params={"pageSize": n, "page": 1})
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return parse_news(r.json())

    async def enrich(self, stocks: list[tuple[str, str]], leader_code: str, days: int = 7) -> dict:
        """stocks = [(code, name)]. 테마 하나의 리포트 줄·뉴스 줄을 만든다. 실패한 종목은 건너뛴다."""
        reports: list[Report] = []
        for code, _ in stocks:
            try:
                reports += await self.reports(code, days)
            except Exception as e:
                log.debug("리포트 실패 %s: %s", code, e)
        out = {"report": report_line(reports, days), "reportCount": len(reports), "brokers": sorted({r.broker for r in reports}),
               "news": "", "newsUrl": "", "newsAt": ""}
        name = dict(stocks).get(leader_code, "")
        for code in [leader_code] + [c for c, _ in stocks if c != leader_code]:
            try:
                items = await self.news(code, 1)
            except Exception as e:
                log.debug("뉴스 실패 %s: %s", code, e)
                continue
            if items:
                nm = dict(stocks).get(code, name)
                out.update(news=f"{nm} — {items[0].title}", newsUrl=items[0].url, newsAt=items[0].at, newsPress=items[0].press)
                break
        out["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        return out
