"""종목별 증권사 리포트 · 뉴스 (네이버 금융). 테마 카드의 '리포트 줄' 과 '뉴스 줄' 을 채운다.

뉴스 줄 규칙 요약
  후보  = 테마 종목들의 최근 6건씩 중, 시황·마감 기사가 아니고, 3일 안이며, '관련' 인 것
  관련  = 테마 핵심어가 제목에 낱말로 들어 있거나, 종목명 + 재료 단서(급등·수주·계약·목표가…)가 같이 있는 것
  선택  = 점수(종목명 +2, 핵심어당 +2 최대 +4) 최고 → 대장주 순 → 최신 순
  없음  = "테마 관련 기사 없음" (링크 없음)

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


# 시황·마감 기사처럼 종목 자체와 무관한 제목. 뒤로 미룬다 (다른 기사가 없으면 그대로 쓴다).
MARKET_WRAP = re.compile(r"시황|코스피|코스닥|증시|양대\s*지수|마감|개장|외국인.*순매[수도]|데이터랩|거래\s*상위|주말머니|주간\s*전망|오늘의\s*증권|이 시각")


FRESH_DAYS = 3  # 이보다 오래된 종목 기사라면 차라리 최신 시황 기사를 쓴다


def is_fresh(at: str, days: int = FRESH_DAYS, now: datetime | None = None) -> bool:
    try:
        t = datetime.strptime(at[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return (now or datetime.now()) - t <= timedelta(days=days)


_STOP = {"등", "관련", "대표주", "생산", "테마", "기타", "및", "국내", "해외", "사업", "업체", "기업", "전문", "제조", "개발", "판매",
         "주요", "통해", "위한", "대한", "있는", "하는", "으로", "에서", "부문", "분야", "제품", "서비스", "시장", "영위", "보유", "기술",
         "최초", "글로벌", "확대", "공급", "계열", "종속회사", "종속회사로", "전문업체", "제조업체", "부품업체", "친환경", "고효율", "소자", "장비",
         "등을", "등의", "등이", "등과", "각종", "주요제품", "체결", "부품", "패키지", "세계"}
_PARTICLES = ("으로", "에서", "에는", "이며", "하며", "에", "을", "를", "의", "와", "과")  # 로·이·가·도·은·는 은 낱말 끝에도 흔해 뗴지 않는다
_HANGUL = re.compile(r"[가-힣]")


def kw_hit(title: str, kw: str) -> bool:
    """핵심어가 제목에 '낱말로' 들어 있는가. 양쪽이 모두 한글이면 다른 낱말의 일부로 본다
    (한국'마이크로'소프트 ✗, AI'반도체'소부장 ✓, '원전'주 ✓)."""
    start = 0
    while (i := title.find(kw, start)) != -1:
        before = title[i - 1] if i > 0 else ""
        after = title[i + len(kw)] if i + len(kw) < len(title) else ""
        if not (_HANGUL.match(before) and _HANGUL.match(after)):
            return True
        start = i + 1
    return False
_TOKEN = re.compile(r"[가-힣A-Za-z0-9]{2,}")


def _stem(t: str) -> str:
    """끝에 붙은 조사를 뗀다 (루멘텀에 → 루멘텀, 공급을 → 공급). 두 글자 이하가 되면 그대로 둔다."""
    for p in _PARTICLES:
        if t.endswith(p) and len(t) - len(p) >= 2:
            return t[: -len(p)]
    return t


def theme_keywords(theme_name: str, whys: list[str] | None = None, max_from_why: int = 6) -> list[str]:
    """테마명 토큰 + 편입 사유에서 2종목 이상 겹치는 낱말. 뉴스 제목 가점에 쓴다."""
    kws: list[str] = []
    for t in _TOKEN.findall(theme_name):
        if t not in _STOP and t not in kws:
            kws.append(t)
    if whys:
        count: dict[str, int] = {}
        for w in whys:
            for t in {_stem(x) for x in _TOKEN.findall(w)}:
                if len(t) >= 2 and t not in _STOP and not t.isdigit():
                    count[t] = count.get(t, 0) + 1
        common = sorted((t for t, c in count.items() if c >= 2 and t not in kws), key=lambda t: (-count[t], -len(t)))
        kws += common[:max_from_why]
    return kws


NO_NEWS = "테마 관련 기사 없음"
# 종목명만 있는 기사도 '재료' 냄새가 나면 관련 기사로 친다 (봉사활동·인사 같은 기사는 걸러진다)
CUE = re.compile(r"급등|급락|상한가|특징주|수주|계약|공급|목표가|실적|상향|하향|진출|투자|양산|승인|허가|인수|합병|유증|증자")  # 출시·신제품은 셋톱박스 출시 같은 일상 기사가 걸려 뺐다


def is_related(n: News, name: str, keywords: list[str] | tuple[str, ...]) -> bool:
    if any(kw_hit(n.title, k) for k in keywords):
        return True
    return bool(name and name in n.title and CUE.search(n.title))


def news_score(n: News, name: str, keywords: list[str] | tuple[str, ...] = ()) -> int:
    """종목명 +2, 테마 핵심어 하나당 +2 (최대 +4), 시황성 -3."""
    s = 0
    if name and name in n.title:
        s += 2
    hits = sum(1 for k in keywords if kw_hit(n.title, k))
    s += min(2, hits) * 2
    if MARKET_WRAP.search(n.title):
        s -= 3
    return s


def pick_news(items: list[News], name: str, keywords: list[str] | tuple[str, ...] = ()) -> News | None:
    """점수 높은 기사, 같으면 최신 순(입력 순서)."""
    if not items:
        return None
    return max(items, key=lambda n: (news_score(n, name, keywords), -items.index(n)))


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

    async def enrich(self, stocks: list[tuple[str, str]], leader_code: str, days: int = 7,
                     keywords: list[str] | tuple[str, ...] = ()) -> dict:
        """stocks = [(code, name)]. 테마 하나의 리포트 줄·뉴스 줄을 만든다. 실패한 종목은 건너뛴다.
        뉴스는 모든 종목의 최근 기사 중 (테마 핵심어·종목명 가점, 시황 감점) 점수가 가장 높은 것을 고른다.
        점수가 같으면 대장주 → 다음 종목 순, 그다음 최신 순."""
        reports: list[Report] = []
        for code, _ in stocks:
            try:
                reports += await self.reports(code, days)
            except Exception as e:
                log.debug("리포트 실패 %s: %s", code, e)
        out = {"report": report_line(reports, days), "reportCount": len(reports), "brokers": sorted({r.broker for r in reports}),
               "news": "", "newsUrl": "", "newsAt": ""}
        names = dict(stocks)
        cands: list[tuple[int, int, int, str, News]] = []  # (점수, -종목순서, -기사순서, 코드, 기사)
        for order, code in enumerate([leader_code] + [c for c, _ in stocks if c != leader_code]):
            try:
                items = await self.news(code, 6)
            except Exception as e:
                log.debug("뉴스 실패 %s: %s", code, e)
                continue
            nm = names.get(code, "")
            for idx, n in enumerate(items):
                if MARKET_WRAP.search(n.title) or not is_fresh(n.at) or not is_related(n, nm, keywords):
                    continue
                cands.append((news_score(n, nm, keywords), -order, -idx, code, n))
        if cands:
            score, _, _, code, best = max(cands)
            out.update(news=f"{names.get(code, '')} — {best.title}", newsUrl=best.url, newsAt=best.at, newsPress=best.press, newsScore=score)
        else:
            out.update(news=NO_NEWS, newsUrl="", newsAt="", newsPress="", newsScore=None)
        out["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        return out
