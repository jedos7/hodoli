"""코스피200 선물 투자자별 매매동향 (네이버 금융). '외인 선물' 칸의 소스.

  https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate=YYYYMMDD&sosok=03
  sosok 03 = 선물 (01 코스피, 02 코스닥, 04 콜옵션, 05 풋옵션). 표 = 날짜 · 개인 · 외국인 · 기관계 · … (단위: 계약)
  장중에는 당일 행이 잠정치로 갱신되고, 장 마감 뒤 확정된다.

키움 REST API 에는 선물 투자자 조회가 없어(명세에 선물·옵션 분류 자체가 없음) 증권사와 무관하게 이 소스를 쓴다.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

log = logging.getLogger(__name__)

URL = "https://finance.naver.com/sise/investorDealTrendDay.naver"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
}
_ROW = re.compile(r"(\d{2})\.(\d{2})\.(\d{2})\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(-?[\d,]+)")


@dataclass(slots=True)
class FuturesRow:
    date: str      # YYYY-MM-DD
    prsn: int      # 개인 순매수 (계약)
    frgn: int      # 외국인
    orgn: int      # 기관계


def parse_rows(page_html: str) -> list[FuturesRow]:
    body = re.sub(r"<script.*?</script>", "", page_html, flags=re.S)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body)))
    out = []
    for yy, mm, dd, p, f, o in _ROW.findall(text):
        n = lambda s: int(s.replace(",", ""))  # noqa: E731
        out.append(FuturesRow(f"20{yy}-{mm}-{dd}", n(p), n(f), n(o)))
    return out  # 최신순


async def fetch_rows(client: httpx.AsyncClient | None = None, bizdate: date | None = None) -> list[FuturesRow]:
    own = client is None
    c = client or httpx.AsyncClient(headers=HEADERS, timeout=15)
    try:
        r = await c.get(URL, params={"bizdate": (bizdate or date.today()).strftime("%Y%m%d"), "sosok": "03"})
        r.raise_for_status()
        return parse_rows(r.content.decode("euc-kr", "replace"))
    finally:
        if own:
            await c.aclose()


def foreign_summary(rows: list[FuturesRow], today: date | None = None) -> dict:
    """{prev: 전일(가장 최근 지난 거래일) 외국인 순매수, now: 당일(장중 잠정) 외국인 순매수 또는 None, src}"""
    if not rows:
        return {"prev": None, "now": None, "src": "네이버", "error": "선물 투자자 표를 읽지 못했습니다"}
    t = (today or date.today()).isoformat()
    todays = next((r for r in rows if r.date == t), None)
    prevs = [r for r in rows if r.date < t]
    prev = prevs[0] if prevs else None
    return {
        "prev": prev.frgn if prev else None,
        "now": todays.frgn if todays else None,
        "src": "네이버 " + ((todays.date[5:].replace("-", "/") + " 잠정") if todays else (prev.date[5:].replace("-", "/") + " 확정" if prev else "")),
        "prsn": todays.prsn if todays else (prev.prsn if prev else None),
        "orgn": todays.orgn if todays else (prev.orgn if prev else None),
    }
