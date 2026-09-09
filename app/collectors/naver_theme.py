"""네이버 금융 테마 수집기.

  목록: https://finance.naver.com/sise/theme.naver?page=N            (7쪽 안팎, 기본 정렬 = 전일대비 등락률 내림차순)
  상세: https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no=NNN
        종목명(코드) · 현재가 · 전일비 · 등락률 · 매수호가 · 매도호가 · 거래량 · 거래대금(백만) · 전일거래량 · 테마 편입 사유

페이지는 EUC-KR 이고 표 구조가 단순해서 정규식으로 읽는다. 구조가 바뀌면 parse_* 함수만 고치면 된다.
비공식 소스이므로 요청 간격(DELAY)을 두고, 실패해도 서버가 죽지 않게 예외는 호출자가 처리한다.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

BASE = "https://finance.naver.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": BASE + "/sise/theme.naver",
}
DELAY = 0.25  # 요청 간격(초)


@dataclass(slots=True)
class ThemeRow:
    no: int
    name: str
    chg: float        # 전일대비 등락률 %
    chg3d: float      # 최근 3일 등락률 %
    up: int
    flat: int
    down: int
    leaders: list[tuple[str, str]] = field(default_factory=list)  # (코드, 이름)


@dataclass(slots=True)
class StockRow:
    code: str
    name: str
    price: int
    chg: float           # %
    volume: int
    amount_million: int  # 거래대금(백만원)
    why: str = ""        # 테마 편입 사유

    @property
    def amount_eok(self) -> float:
        return self.amount_million / 100

    @property
    def prev_close(self) -> int:
        return int(round(self.price / (1 + self.chg / 100))) if self.chg > -100 else self.price


# ── 파서 ──────────────────────────────────────────────────────────
_NUM = lambda s: float(s.replace(",", "").replace("%", "").replace("+", "") or 0)  # noqa: E731

_LIST_ROW = re.compile(
    r'col_type1"><a href="[^"]*no=(\d+)">([^<]+)</a></td>\s*'
    r'<td class="number col_type2">\s*<span[^>]*>\s*([-+\d.,]+)%\s*</span>\s*</td>\s*'
    r'<td class="number col_type3">\s*<span[^>]*>\s*([-+\d.,]+)%\s*</span>\s*</td>\s*'
    r'<td class="number col_type4">(\d+)</td>\s*<td class="number col_type4">(\d+)</td>\s*<td class="number col_type4">(\d+)</td>(.*?)</tr>',
    re.S,
)
_LEADER = re.compile(r'code=(\d+)">([^<]+)</a>')
_LAST_PAGE = re.compile(r'class="pgRR"[^>]*>\s*<a href="[^"]*page=(\d+)"')


def parse_theme_list(page_html: str) -> list[ThemeRow]:
    out = []
    for m in _LIST_ROW.finditer(page_html):
        no, name, chg, chg3d, up, flat, down, rest = m.groups()
        out.append(ThemeRow(int(no), html.unescape(name).strip(), _NUM(chg), _NUM(chg3d), int(up), int(flat), int(down),
                            [(c, html.unescape(n).strip()) for c, n in _LEADER.findall(rest)]))
    return out


def parse_last_page(page_html: str) -> int:
    m = _LAST_PAGE.search(page_html)
    return int(m.group(1)) if m else 1


_DETAIL_NAME = re.compile(r'<td class="name">.*?<a href="/item/main\.naver\?code=(\d+)">([^<]+)</a>', re.S)
_DETAIL_WHY = re.compile(r'<p class="info_txt">(.*?)</p>', re.S)
_DETAIL_NUMS = re.compile(r'<td class="number"[^>]*>\s*(?:<em[^>]*>.*?</em>)?\s*(?:<span[^>]*>)?\s*([-+\d,.]+%?)', re.S)


def parse_theme_detail(page_html: str) -> list[StockRow]:
    out = []
    for chunk in re.split(r"<tr onMouseOver", page_html)[1:]:
        chunk = chunk.split("</tr>", 1)[0]
        nm = _DETAIL_NAME.search(chunk)
        if not nm:
            continue
        nums = _DETAIL_NUMS.findall(chunk)
        # [현재가, 전일비, 등락률, 매수호가, 매도호가, 거래량, 거래대금, 전일거래량]
        if len(nums) < 7:
            continue
        why = _DETAIL_WHY.search(chunk)
        out.append(StockRow(
            code=nm.group(1), name=html.unescape(nm.group(2)).strip(),
            price=int(_NUM(nums[0])), chg=_NUM(nums[2]), volume=int(_NUM(nums[5])), amount_million=int(_NUM(nums[6])),
            why=html.unescape(re.sub(r"<[^>]+>", "", why.group(1))).strip() if why else "",
        ))
    return out


# ── 수집 ──────────────────────────────────────────────────────────
class NaverThemeCollector:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str) -> str:
        r = await self.client.get(BASE + path)
        r.raise_for_status()
        await asyncio.sleep(DELAY)
        return r.content.decode("euc-kr", "replace")

    async def theme_list(self, max_pages: int = 10) -> list[ThemeRow]:
        first = await self._get("/sise/theme.naver?page=1")
        rows = parse_theme_list(first)
        last = min(parse_last_page(first), max_pages)
        for p in range(2, last + 1):
            rows += parse_theme_list(await self._get(f"/sise/theme.naver?page={p}"))
        return rows

    async def theme_detail(self, no: int) -> list[StockRow]:
        return parse_theme_detail(await self._get(f"/sise/sise_group_detail.naver?type=theme&no={no}"))


def grade_of(chg: float, width: float) -> str:
    """등락률과 테마 폭(상승 종목 비율)으로 재료 등급. A: 강하고 넓다, B: 강하거나 넓다, C: 나머지"""
    strong, wide = chg >= 4.0, width >= 0.7
    return "A" if strong and wide else "B" if strong or wide else "C"


async def collect(top: int = 12, per: int = 6, min_stocks: int = 3, min_amount_eok: float = 10.0,
                  candidates: int | None = None, collector: NaverThemeCollector | None = None) -> dict:
    """themes.json 형식의 dict 를 만든다.

    - 목록의 등락률 순으로 후보를 훑으며, 거래대금 min_amount_eok 억 이상인 종목이 min_stocks 개 이상인 테마만 채택.
    - 종목은 거래대금 순으로 per 개. 같은 종목이 여러 테마에 걸리면 먼저 채택된(순위 높은) 테마에만 넣는다
      (서버 상태가 종목코드 하나를 테마 하나에만 귀속시키기 때문).
    """
    own = collector is None
    c = collector or NaverThemeCollector()
    try:
        rows = await c.theme_list()
        rows.sort(key=lambda r: r.chg, reverse=True)
        seen: set[str] = set()
        themes: list[dict] = []
        for row in rows[: candidates or top * 3]:
            if len(themes) >= top:
                break
            try:
                stocks = await c.theme_detail(row.no)
            except httpx.HTTPError as e:
                log.warning("테마 %s(%d) 상세 실패: %s", row.name, row.no, e)
                continue
            picked = [s for s in stocks if s.amount_eok >= min_amount_eok and s.code not in seen]
            picked.sort(key=lambda s: s.amount_million, reverse=True)
            picked = picked[:per]
            if len(picked) < min_stocks:
                continue
            seen.update(s.code for s in picked)
            total = len(row.leaders) and (row.up + row.flat + row.down) or 1
            width = row.up / max(1, row.up + row.flat + row.down)
            lead = picked[0]
            themes.append({
                "id": f"nv{row.no}", "no": row.no, "name": row.name,
                "grade": grade_of(row.chg, width),
                "report": f"네이버 테마 · 상승 {row.up} · 보합 {row.flat} · 하락 {row.down} · 3일 {row.chg3d:+.2f}%",
                "news": (lead.name + " — " + lead.why)[:110] if lead.why else "",
                "chg": row.chg, "chg3d": row.chg3d, "up": row.up, "flat": row.flat, "down": row.down,
                "stocks": [{"code": s.code, "name": s.name, "ref": s.prev_close, "why": s.why[:140]} for s in picked],
            })
        return {"_comment": "네이버 금융 테마에서 자동 수집. ref = 수집 시점 현재가로 역산한 전일 종가.",
                "source": "naver", "collected_at": datetime.now().isoformat(timespec="seconds"),
                "themes": themes}
    finally:
        if own:
            await c.close()
