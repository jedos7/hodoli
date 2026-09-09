"""일별 테마 — "어느 날 어느 테마로 돈이 갔나".

재료
  1) 네이버 전체 테마 매핑 (266개 테마 × 구성 종목)      → data/theme_map.json      (하루 한 번)
  2) 그 종목 전체의 일봉 (네이버 fchart, 최근 ~70봉)      → data/candles_naver_all.json (하루 한 번)
  3) 코스피·코스닥 지수 일봉 (fchart KOSPI/KOSDAQ)
계산 (거래일마다)
  테마별 종목 등락률(종가/전일 종가) → 상위4 평균 · 중앙값 · 종목 수 · 테마 거래대금 합 · 대표(등락 1위) · 상위 6종목
  heat = 상위4 평균과 참여 폭(상승 종목 비율)을 합친 0~99 점수, 발화 = 상승 종목 비율
결과 → data/theme_calendar.json  {days: {YYYYMMDD: {kospi, kosdaq, themes: [...]}}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from statistics import median

import httpx

from app.collectors.naver_daily import HEADERS as NAVER_HEADERS
from app.collectors.naver_daily import NaverDaily, parse_fchart
from app.collectors.naver_theme import NaverThemeCollector
from app.config import settings
from app.kis.rest import Candle

log = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]


# ── 1) 전체 테마 매핑 ──
async def collect_theme_map(progress: Progress | None = None) -> dict:
    c = NaverThemeCollector()
    try:
        rows = await c.theme_list()
        themes = {}
        for i, row in enumerate(rows, 1):
            try:
                stocks = await c.theme_detail(row.no)
            except Exception as e:
                log.warning("테마 상세 실패 %s: %s", row.name, e)
                continue
            themes[str(row.no)] = {"name": row.name, "stocks": [{"code": s.code, "name": s.name} for s in stocks]}
            if progress:
                progress(i, len(rows), row.name)
    finally:
        await c.close()
    data = {"savedAt": datetime.now().isoformat(timespec="seconds"), "themes": themes}
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "theme_map.json").write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    return data


def load_theme_map() -> dict | None:
    p = settings.data_dir / "theme_map.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else None


# ── 2) 전 종목 일봉 ──
async def collect_all_candles(codes: list[str], count: int = 70, progress: Progress | None = None) -> dict[str, list[Candle]]:
    nd = NaverDaily()
    out: dict[str, list[Candle]] = {}
    try:
        for i, code in enumerate(codes, 1):
            try:
                out[code] = await nd.candles(code, count=count)
            except Exception as e:
                log.debug("일봉 실패 %s: %s", code, e)
            if progress and (i % 50 == 0 or i == len(codes)):
                progress(i, len(codes), code)
    finally:
        await nd.close()
    data = {"savedAt": datetime.now().isoformat(timespec="seconds"),
            "candles": {c: [[x.date, x.open, x.high, x.low, x.close, x.volume, x.amount] for x in cs] for c, cs in out.items()}}
    (settings.data_dir / "candles_naver_all.json").write_text(json.dumps(data), "utf-8")
    return out


def load_all_candles() -> tuple[dict[str, list[Candle]], str] | None:
    p = settings.data_dir / "candles_naver_all.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text("utf-8"))
    return {c: [Candle(*r) for r in rows] for c, rows in d["candles"].items()}, d.get("savedAt", "")


async def index_daily(symbol: str, count: int = 70) -> dict[str, float]:
    """날짜 → 지수 등락률 %"""
    async with httpx.AsyncClient(headers=NAVER_HEADERS, timeout=15) as c:
        r = await c.get("https://fchart.stock.naver.com/sise.nhn", params={"symbol": symbol, "timeframe": "day", "count": count, "requestType": "0"})
        r.raise_for_status()
    items = re.findall(r'<item data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|', r.content.decode("euc-kr", "replace"))
    out, prev = {}, None
    for d, o, h, l, cl in items:
        cl = float(cl)
        if prev:
            out[d] = (cl / prev - 1) * 100
        prev = cl
    return out


# ── 3) 계산 ──
def compute_days(theme_map: dict, candles: dict[str, list[Candle]], days: int = 45, keep: int = 30) -> dict:
    # 종목별 날짜 → (등락률, 종가, 거래대금)
    by_code: dict[str, dict[str, tuple[float, int, float]]] = {}
    dates: set[str] = set()
    for code, cs in candles.items():
        m = {}
        for i in range(1, len(cs)):
            if cs[i - 1].close > 0:
                m[cs[i].date] = ((cs[i].close / cs[i - 1].close - 1) * 100, cs[i].close, cs[i].amount / 1e8)
        by_code[code] = m
        dates.update(m)
    recent = sorted(dates)[-days:]
    out: dict[str, dict] = {}
    for d in recent:
        themes = []
        for no, t in theme_map["themes"].items():
            rows = []
            for s in t["stocks"]:
                v = by_code.get(s["code"], {}).get(d)
                if v:
                    rows.append((s["name"], s["code"], v[0], v[1], v[2]))
            if len(rows) < 3:
                continue
            rows.sort(key=lambda r: r[2], reverse=True)
            chgs = [r[2] for r in rows]
            top4 = sum(chgs[:4]) / min(4, len(chgs))
            med = median(chgs)
            up_ratio = sum(1 for c in chgs if c > 0) / len(chgs)
            amount = sum(r[4] for r in rows)
            heat = int(max(0, min(99, 30 + top4 * 3 + med * 4 + up_ratio * 25)))
            themes.append({"no": no, "name": t["name"], "top4": round(top4, 2), "median": round(med, 2), "n": len(rows), "upRatio": round(up_ratio, 2),
                           "amount": round(amount), "heat": heat, "rep": rows[0][0],
                           "stocks": [{"name": r[0], "code": r[1], "chg": round(r[2], 2), "price": r[3], "amount": round(r[4])} for r in rows[:6]],
                           "top6Amount": round(sum(r[4] for r in rows[:6]))})
        by_top4 = sorted(themes, key=lambda t: t["top4"], reverse=True)[:keep]
        by_med = sorted(themes, key=lambda t: t["median"], reverse=True)[:10]
        by_amt = sorted(themes, key=lambda t: t["amount"], reverse=True)[:10]
        seen, merged = set(), []
        for t in by_top4 + by_med + by_amt:
            if t["no"] not in seen:
                seen.add(t["no"])
                merged.append(t)
        out[d] = {"themes": merged, "themeCount": len(themes)}
    return out


async def build_calendar(progress: Progress | None = None, refresh_map: bool = False, refresh_candles: bool = False) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    tm = None if refresh_map else load_theme_map()
    if not tm or not tm.get("savedAt", "").startswith(today):
        log.info("전체 테마 매핑 수집")
        tm = await collect_theme_map(progress)
    codes = sorted({s["code"] for t in tm["themes"].values() for s in t["stocks"]})
    cached = None if refresh_candles else load_all_candles()
    if cached and cached[1].startswith(today):
        candles = cached[0]
    else:
        log.info("전 종목 일봉 수집 %d종목", len(codes))
        candles = await collect_all_candles(codes, progress=progress)
    kospi, kosdaq = await index_daily("KOSPI"), await index_daily("KOSDAQ")
    days = compute_days(tm, candles)
    for d, v in days.items():
        v["kospi"] = round(kospi.get(d, 0.0), 2)
        v["kosdaq"] = round(kosdaq.get(d, 0.0), 2)
    result = {"asof": datetime.now().isoformat(timespec="seconds"), "themeMapAt": tm.get("savedAt"), "stocks": len(codes),
              "themes": len(tm["themes"]), "days": days}
    (settings.data_dir / "theme_calendar.json").write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    return result
