"""네이버 증권 테마 수집기 (JSON API).

  목록: https://m.stock.naver.com/api/stocks/theme?page=N&pageSize=100      groups[]: no · name · changeRate · riseCount · fallCount · steadyCount (266개)
  상세: https://m.stock.naver.com/api/stocks/theme/{no}?page=1&pageSize=100  stocks[] (현재가·등락률·거래량·거래대금) + themeItemInfoMap{코드: 편입 사유}

2026-09-11 까지 쓰던 finance.naver.com 의 theme.naver / sise_group_detail.naver 는 그날부터 stock.naver.com 으로 302 넘어가 표가 없다.
옛 목록에 있던 '최근 3일 등락률'과 대장주 2종목은 새 API 에 없다 (chg3d=0, leaders=[]).
비공식 소스이므로 요청 간격(DELAY)을 두고, 실패해도 서버가 죽지 않게 예외는 호출자가 처리한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.collectors.naver_api import NaverApi, num

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ThemeRow:
    no: int
    name: str
    chg: float        # 전일대비 등락률 %
    chg3d: float      # 최근 3일 등락률 % (새 API 에 없음 → 0)
    up: int
    flat: int
    down: int
    leaders: list[tuple[str, str]] = field(default_factory=list)  # (코드, 이름) — 새 API 에 없음


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


# ── 파서 (JSON) ────────────────────────────────────────────────
def parse_theme_list(d: dict) -> list[ThemeRow]:
    return [ThemeRow(no=int(g["no"]), name=str(g.get("name", "")).strip(), chg=num(g.get("changeRate")), chg3d=0.0,
                     up=int(num(g.get("riseCount"))), flat=int(num(g.get("steadyCount"))), down=int(num(g.get("fallCount"))))
            for g in (d.get("groups") or []) if g.get("no") is not None]


def parse_theme_detail(d: dict) -> list[StockRow]:
    why = d.get("themeItemInfoMap") or {}
    out: list[StockRow] = []
    for s in d.get("stocks") or []:
        code = s.get("itemCode")
        if not code:
            continue
        raw = s.get("accumulatedTradingValueRaw")
        amount_m = int(num(raw) // 1_000_000) if raw not in (None, "") else int(num(s.get("accumulatedTradingValue")))
        out.append(StockRow(code=str(code), name=str(s.get("stockName", "")).strip(), price=int(num(s.get("closePrice"))),
                            chg=num(s.get("fluctuationsRatio")), volume=int(num(s.get("accumulatedTradingVolume"))),
                            amount_million=amount_m, why=str(why.get(code) or "").strip()))
    return out


class NaverThemeCollector(NaverApi):
    async def theme_list(self, max_pages: int = 10) -> list[ThemeRow]:
        groups = await self.pages("/stocks/theme", "groups", page_size=100, max_pages=max_pages)
        return parse_theme_list({"groups": groups})

    async def theme_detail(self, no: int) -> list[StockRow]:
        return parse_theme_detail(await self.json(f"/stocks/theme/{no}", {"page": 1, "pageSize": 100}))


def grade_of(chg: float, width: float) -> str:
    """등락률과 테마 폭(상승 종목 비율)으로 재료 등급. A: 강하고 넓다, B: 강하거나 넓다, C: 나머지"""
    strong, wide = chg >= 4.0, width >= 0.7
    return "A" if strong and wide else "B" if strong or wide else "C"


async def collect(top: int = 12, per: int = 6, min_stocks: int = 3, min_amount_eok: float = 10.0,
                  candidates: int | None = None, collector: NaverThemeCollector | None = None, enrich: bool = True) -> dict:
    """themes.json 형식의 dict 를 만든다.

    - 목록의 등락률 순으로 후보를 훑으며, 거래대금 min_amount_eok 억 이상인 종목이 min_stocks 개 이상인 테마만 채택.
    - 종목은 거래대금 순으로 per 개. 같은 종목이 여러 테마에 걸리면 먼저 채택된(순위 높은) 테마에만 넣는다
      (서버 상태가 종목코드 하나를 테마 하나에만 귀속시키기 때문).
    - enrich=True 면 증권사 리포트 건수와 대장주 최신 뉴스로 리포트 줄·뉴스 줄을 채운다 (naver_news.py).
    """
    own = collector is None
    c = collector or NaverThemeCollector()
    news = None
    if enrich:
        from app.collectors.naver_news import NaverNews

        news = NaverNews()
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
            width = row.up / max(1, row.up + row.flat + row.down)
            lead = picked[0]
            theme = {
                "id": f"nv{row.no}", "no": row.no, "name": row.name,
                "grade": grade_of(row.chg, width),
                "report": f"상승 {row.up} · 보합 {row.flat} · 하락 {row.down}" + (f" · 3일 {row.chg3d:+.2f}%" if row.chg3d else ""),
                "news": (lead.name + " — " + lead.why)[:110] if lead.why else "",
                "chg": row.chg, "chg3d": row.chg3d, "up": row.up, "flat": row.flat, "down": row.down,
                "stocks": [{"code": s.code, "name": s.name, "ref": s.prev_close, "why": s.why[:140]} for s in picked],
            }
            if news:
                from app.collectors.naver_news import theme_keywords

                leader = max(picked, key=lambda s: s.chg)  # 뉴스는 등락률 1위 종목부터
                kws = theme_keywords(row.name, [s.why for s in picked])
                theme["keywords"] = kws
                theme.update(await news.enrich([(s.code, s.name) for s in picked], leader.code, keywords=kws))
            themes.append(theme)
        return {"_comment": "네이버 증권 테마 API 에서 자동 수집. ref = 수집 시점 현재가로 역산한 전일 종가.",
                "source": "naver", "collected_at": datetime.now().isoformat(timespec="seconds"),
                "themes": themes}
    finally:
        if own:
            await c.close()
        if news:
            await news.close()
