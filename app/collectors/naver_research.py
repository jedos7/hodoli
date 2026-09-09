"""리서치 브리핑 — 네이버 금융 리서치 게시판.

  산업분석 industry_list (분류 열 있음) · 경제분석 economy_list · 시황정보 market_info_list · 투자정보 invest_list
  상세 페이지(*_read.naver?nid=) 의 view_cnt 블록에 리포트 요약 원문이 있다. 종목분석 상세에는 목표가·투자의견도 있다.

브리핑 = 최근 N일 산업·경제·시황 리포트의 요약 문장(원문 그대로) + 테마별 목표가 추정치 방향
  추정치 방향: 테마 종목의 최근 N일 종목 리포트마다, 같은 증권사의 직전 리포트 목표가와 비교해 상향/하향을 센다.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

BASE = "https://finance.naver.com/research/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Referer": "https://finance.naver.com/research/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
DELAY = 0.15
LISTS = [("industry", "산업", True), ("economy", "경제", False), ("market_info", "시황", False), ("invest", "투자정보", False)]


@dataclass(slots=True)
class Report:
    kind: str        # 산업 | 경제 | 시황 | 투자정보 | 종목
    category: str    # 산업 분류 또는 종목명
    title: str
    broker: str
    date: str        # YYYY-MM-DD
    nid: int
    url: str
    bullets: list[str] = field(default_factory=list)
    target: int | None = None
    opinion: str = ""


_ROW_CAT = re.compile(r'<td style="padding-left:10">(?:<a[^>]*>)?([^<]+)(?:</a>)?\s*</td>\s*<td><a href="([a-z_]+_read\.naver\?nid=(\d+)[^"]*)">([^<]+)</a>.*?</td>\s*<td>([^<]+)</td>.*?<td class="date"[^>]*>\s*(\d{2}\.\d{2}\.\d{2})', re.S)
_ROW_NOCAT = re.compile(r'<td><a href="([a-z_]+_read\.naver\?nid=(\d+)[^"]*)">([^<]+)</a>.*?</td>\s*<td>([^<]+)</td>.*?<td class="date"[^>]*>\s*(\d{2}\.\d{2}\.\d{2})', re.S)


def _d(s: str) -> str:
    yy, mm, dd = s.split(".")
    return f"20{yy}-{mm}-{dd}"


def parse_list(page_html: str, kind: str, has_category: bool) -> list[Report]:
    out = []
    if has_category:
        for cat, href, nid, title, broker, d in _ROW_CAT.findall(page_html):
            out.append(Report(kind, html.unescape(cat).strip(), html.unescape(title).strip(), html.unescape(broker).strip(), _d(d), int(nid), BASE + html.unescape(href)))
    else:
        for href, nid, title, broker, d in _ROW_NOCAT.findall(page_html):
            out.append(Report(kind, "", html.unescape(title).strip(), html.unescape(broker).strip(), _d(d), int(nid), BASE + html.unescape(href)))
    return out


_VIEW = re.compile(r'class="view_cnt">(.*?)</td>', re.S)
_TARGET = re.compile(r'목표가\s*<em class="money"><strong>([\d,]+)</strong>')
_OPINION = re.compile(r'투자의견\s*<em class="coment">([^<]+)</em>')


def parse_detail(page_html: str, max_bullets: int = 4) -> dict:
    """요약 원문을 문장 단위로 잘라 bullets 로. 목표가·투자의견도 뽑는다."""
    m = _VIEW.search(page_html)
    bullets: list[str] = []
    if m:
        body = m.group(1)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        body = body.split('<div style="TEXT-ALIGN: left">')[0]
        body = re.sub(r"</?(p|br|div)[^>]*>", "\n", body)
        text = html.unescape(re.sub(r"<[^>]+>", "", body))
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
        lines = [ln for ln in lines if len(ln) >= 8]
        if lines:
            head = lines[0]
            rest = " ".join(lines[1:])
            sents = [s.strip() for s in re.split(r"(?<=[.다요음됨함])\s+", rest) if len(s.strip()) >= 10]
            bullets = [head] + sents[: max_bullets - 1]
    t = _TARGET.search(page_html)
    o = _OPINION.search(page_html)
    return {"bullets": bullets, "target": int(t.group(1).replace(",", "")) if t else None, "opinion": html.unescape(o.group(1)).strip() if o else ""}


class NaverResearch:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> str:
        r = await self.client.get(BASE + path, params=params)
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return r.content.decode("euc-kr", "replace")

    async def list_reports(self, list_key: str, kind: str, has_cat: bool, since: str, pages: int = 2) -> list[Report]:
        out: list[Report] = []
        for p in range(1, pages + 1):
            rows = parse_list(await self._get(f"{list_key}_list.naver", {"page": p}), kind, has_cat)
            out += [r for r in rows if r.date >= since]
            if not rows or rows[-1].date < since:
                break
        return out

    async def fill_detail(self, r: Report) -> Report:
        try:
            d = parse_detail(await self._get(r.url.replace(BASE, "")))
            r.bullets, r.target, r.opinion = d["bullets"], d["target"], d["opinion"]
        except Exception as e:
            log.debug("리포트 상세 실패 %s: %s", r.nid, e)
        return r

    async def company_reports(self, code: str, pages: int = 1) -> list[Report]:
        out: list[Report] = []
        for p in range(1, pages + 1):
            page = await self._get("company_list.naver", {"searchType": "itemCode", "itemCode": code, "page": p})
            out += parse_list(page, "종목", True)
        return out

    async def target_direction(self, stocks: list[tuple[str, str]], since: str, max_reports: int = 8) -> dict:
        """테마 종목들의 최근 리포트 목표가를 같은 증권사 직전 리포트와 비교 → {"up": n, "down": m, "items": [...]}"""
        up = down = 0
        items = []
        for code, name in stocks:
            try:
                reps = await self.company_reports(code)
            except Exception as e:
                log.debug("종목 리포트 목록 실패 %s: %s", code, e)
                continue
            recent = [r for r in reps if r.date >= since][:max_reports]
            for r in recent:
                prev = next((p for p in reps if p.broker == r.broker and p.date < r.date), None)
                if not prev:
                    continue
                await self.fill_detail(r)
                await self.fill_detail(prev)
                if r.target and prev.target and r.target != prev.target:
                    if r.target > prev.target:
                        up += 1
                    else:
                        down += 1
                    items.append({"stock": name, "broker": r.broker, "date": r.date, "from": prev.target, "to": r.target, "opinion": r.opinion, "url": r.url})
        return {"up": up, "down": down, "items": items}


async def briefing(themes: list[tuple[str, list[tuple[str, str]]]], days: int = 7, max_reports: int = 40) -> dict:
    """themes = [(테마명, [(code, name), ...])]. 결과는 data/research.json 형식."""
    since = (date.today() - timedelta(days=days)).isoformat()
    nr = NaverResearch()
    try:
        reports: list[Report] = []
        for key, kind, has_cat in LISTS:
            try:
                reports += await nr.list_reports(key, kind, has_cat, since)
            except Exception as e:
                log.warning("리서치 목록 실패 %s: %s", key, e)
        reports.sort(key=lambda r: (r.date, r.nid), reverse=True)
        reports = reports[:max_reports]
        for r in reports:
            await nr.fill_detail(r)
        direction = []
        for name, stocks in themes:
            d = await nr.target_direction(stocks, since)
            if d["up"] or d["down"]:
                direction.append({"theme": name, **d})
        direction.sort(key=lambda x: (x["up"] - x["down"], x["up"]), reverse=True)
        return {"asof": datetime.now().isoformat(timespec="seconds"), "days": days, "since": since,
                "direction": direction, "reports": [asdict(r) for r in reports]}
    finally:
        await nr.close()
