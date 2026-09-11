"""고가놀이 스크리너 실행기. 일봉 소스와 종목 범위를 고르고 결과를 data/hoga.json 으로 만든다.

소스   naver : 네이버 일봉 (키 불필요, 거래대금은 거래량×종가 근사)   ← 기본
       kis   : 한국투자증권 REST 일봉 (KIS_ENV 가 vts/real 일 때)
       mock  : 합성 일봉 (UI 확인용)
범위   themes: themes.json 의 종목만 (수십 개, 수 초)
       market: 코스피+코스닥 중 오늘 거래대금 min_amount_eok 억 이상 전 종목 (수백 개, 1~3분)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta

from app.config import settings
from app.kis.rest import Candle
from app.screener.hoga_play import backtest, baseline, find_setups
from app.screener.pullback import find_pullbacks

log = logging.getLogger(__name__)
Progress = Callable[[int, int, str], None]


def theme_map() -> dict[str, tuple[str, str]]:
    """종목코드 → (종목명, 테마명)"""
    d = json.loads(settings.themes_file.read_text("utf-8"))
    return {s["code"]: (s["name"], t["name"]) for t in d["themes"] for s in t["stocks"]}


def synthetic_candles(days: int, seed: int, start_price: int = 20000) -> list[Candle]:
    """모의용 합성 일봉. 가끔 급등 후 며칠 횡보하는 패턴을 섞어 넣는다."""
    rng = random.Random(seed)
    d = date.today() - timedelta(days=int(days * 1.45))
    px = float(start_price)
    out: list[Candle] = []
    plan = 0
    while len(out) < days:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if plan == 0 and rng.random() < 0.04:
            plan = rng.randint(1, 4)
        r = rng.uniform(0.09, 0.2) if plan == 1 else rng.gauss(0.0, 0.015) if plan > 1 else rng.gauss(0.0005, 0.02)
        o = px * (1 + rng.gauss(0, 0.004))
        c = px * (1 + r)
        hi = max(o, c) * (1 + abs(rng.gauss(0, 0.01)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.01)))
        vol = int(rng.uniform(2e5, 2e6) * (4 if plan == 1 else 1))
        out.append(Candle(d.strftime("%Y%m%d"), int(o), int(hi), int(lo), int(c), vol, int(vol * c)))
        px = c
        if plan:
            plan = 0 if plan >= 4 else plan + 1
    return out


async def load_universe(universe: str, source: str, min_amount_eok: float) -> list[tuple[str, str, str]]:
    """(코드, 이름, 테마/시장 라벨) 목록"""
    tm = theme_map()
    if universe == "themes" or source == "mock":
        return [(c, n, t) for c, (n, t) in tm.items()]
    from app.collectors.naver_daily import NaverDaily

    nd = NaverDaily()
    try:
        listed = await nd.market_list(min_amount_eok=min_amount_eok)
    finally:
        await nd.close()
    return [(r.code, r.name, tm[r.code][1] if r.code in tm else r.market) for r in listed]


async def fetch_candles(source: str, universe: list[tuple[str, str, str]], days: int, progress: Progress | None) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    n = len(universe)
    if source == "mock":
        for i, (code, name, _) in enumerate(universe, 1):
            out[code] = synthetic_candles(days, seed=int(code), start_price=random.Random(code).choice([3000, 12000, 45000, 150000]))
        return out
    if source == "kiwoom":
        settings.validate()
        from app.kiwoom.auth import KiwoomAuth
        from app.kiwoom.rest import KiwoomRest

        rest = KiwoomRest(settings, KiwoomAuth(settings))
        try:
            for i, (code, name, _) in enumerate(universe, 1):
                try:
                    out[code] = await rest.daily_candles(code, days)
                except Exception as e:
                    log.warning("키움 일봉 실패 %s(%s): %s", name, code, e)
                if progress:
                    progress(i, n, name)
        finally:
            await rest.close()
        return out
    if source == "kis":
        settings.validate()
        from app.kis.auth import KisAuth
        from app.kis.rest import KisRest

        rest = KisRest(settings, KisAuth(settings))
        end = date.today()
        start = end - timedelta(days=int(days * 1.5))
        try:
            for i, (code, name, _) in enumerate(universe, 1):
                cs: list[Candle] = []
                cur_end = end
                while len(cs) < days:  # 한 번에 100봉까지라 나눠 받는다
                    chunk = await rest.daily_candles(code, start.strftime("%Y%m%d"), cur_end.strftime("%Y%m%d"))
                    if not chunk:
                        break
                    cs = chunk + cs
                    first = datetime.strptime(chunk[0].date, "%Y%m%d").date()
                    cur_end = first - timedelta(days=1)
                    if len(chunk) < 100:
                        break
                out[code] = cs[-days:]
                if progress:
                    progress(i, n, name)
        finally:
            await rest.close()
        return out
    # naver
    from app.collectors.naver_daily import NaverDaily

    nd = NaverDaily()
    try:
        for i, (code, name, _) in enumerate(universe, 1):
            try:
                out[code] = await nd.candles(code, count=days)
            except Exception as e:
                log.warning("일봉 실패 %s(%s): %s", name, code, e)
            if progress:
                progress(i, n, name)
    finally:
        await nd.close()
    return out


def market_open(now: datetime | None = None) -> bool:
    """오늘 봉이 아직 완성되지 않았는가 (평일 15:40 이전)."""
    now = now or datetime.now()
    return now.weekday() < 5 and (now.hour, now.minute) < (15, 40)


def drop_incomplete_today(candles: dict[str, list[Candle]], now: datetime | None = None) -> int:
    """장중이면 오늘 날짜 봉을 뺀다. 판정은 완성된 일봉으로만 한다. 뺀 종목 수를 돌려준다."""
    if not market_open(now):
        return 0
    today = (now or datetime.now()).strftime("%Y%m%d")
    n = 0
    for code, cs in candles.items():
        if cs and cs[-1].date == today:
            candles[code] = cs[:-1]
            n += 1
    return n


def cache_path(source: str, universe: str):
    return settings.data_dir / f"candles_{source}_{universe}.json"


def save_cache(source: str, universe: str, uni: list[tuple[str, str, str]], candles: dict[str, list[Candle]]) -> None:
    """일봉을 파일에 둔다. 조건 조합 비교(scripts/sweep_pullback.py)처럼 같은 일봉을 여러 번 쓸 때 다시 받지 않기 위해."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    data = {"savedAt": datetime.now().isoformat(timespec="seconds"), "source": source, "universe": universe, "universeList": uni,
            "candles": {c: [[x.date, x.open, x.high, x.low, x.close, x.volume, x.amount] for x in cs] for c, cs in candles.items()}}
    cache_path(source, universe).write_text(json.dumps(data, ensure_ascii=False), "utf-8")


def load_cache(source: str, universe: str) -> tuple[list[tuple[str, str, str]], dict[str, list[Candle]], str] | None:
    p = cache_path(source, universe)
    if not p.exists():
        return None
    d = json.loads(p.read_text("utf-8"))
    candles = {c: [Candle(*row) for row in rows] for c, rows in d["candles"].items()}
    return [tuple(u) for u in d["universeList"]], candles, d.get("savedAt", "")


async def get_candles(source: str, universe: str, days: int, min_amount_eok: float, progress: Progress | None = None,
                      use_cache: bool = False) -> tuple[str, list[tuple[str, str, str]], dict[str, list[Candle]]]:
    """(실제 소스, 종목 목록, 일봉). use_cache 면 파일에서, 아니면 새로 받아 파일에 저장."""
    if source == "auto":
        source = "naver" if settings.is_mock else settings.broker
    if use_cache:
        cached = load_cache(source, universe)
        if cached:
            uni, candles, saved = cached
            log.info("일봉 캐시 사용: %s/%s %d종목 (%s)", source, universe, len(uni), saved)
            return source, uni, candles
        log.info("일봉 캐시 없음 → 새로 받음")
    uni = await load_universe(universe, source, min_amount_eok)
    log.info("스크리너: 소스 %s · 범위 %s · %d종목", source, universe, len(uni))
    if not uni:
        # 2026-09-11 네이버 목록 페이지가 바뀌어 0종목이 왔고, 그대로 진행해 일봉 캐시·결과 파일을 빈 것으로 덮어쓴 적이 있다
        raise RuntimeError("종목 목록이 비었습니다 — 종목 목록 소스(네이버 시가총액 API)를 못 읽었습니다. 기존 일봉 캐시와 결과는 그대로 둡니다")
    candles = await fetch_candles(source, uni, days, progress)
    if source != "mock":
        got = sum(1 for cs in candles.values() if cs)
        if got < max(10, len(uni) // 10):
            raise RuntimeError(f"일봉을 거의 못 받았습니다 ({got}/{len(uni)}종목) — 증권사 API 상태를 확인하세요. 기존 일봉 캐시는 그대로 둡니다")
        save_cache(source, universe, uni, candles)
    return source, uni, candles


async def run_screener(source: str = "naver", universe: str = "themes", days: int = 250, recent: int = 5,
                       min_amount_eok: float = 30.0, progress: Progress | None = None, write: bool = True,
                       include_today: bool = False, use_cache: bool = False) -> dict:
    source, uni, candles = await get_candles(source, universe, days, min_amount_eok, progress, use_cache)
    if source != "mock" and not include_today:
        dropped = drop_incomplete_today(candles)
        if dropped:
            log.info("장중이라 오늘 봉 %d종목 제외 (완성된 일봉만 판정)", dropped)

    base = baseline(candles)
    gap_rets = [(cs[i + 1].open / cs[i].close - 1) * 100 for cs in candles.values() for i in range(len(cs) - 1) if cs[i].close > 0 and cs[i + 1].open > 0]
    gap_base = sum(gap_rets) / len(gap_rets) if gap_rets else 0.0   # 아무 날 종가 매수 → 다음 날 시가 (종가배팅 기준선)
    asof = max((cs[-1].date for cs in candles.values() if cs), default=None)
    dates = sorted({c.date for cs in candles.values() for c in cs})
    recent_days = set(dates[-recent:])
    meta = {
        "asof": asof, "env": settings.env, "source": source, "universe": universe, "stocks": len(uni),
        "ranAt": datetime.now().isoformat(timespec="seconds"), "todayIncluded": include_today or not market_open(), "recentDays": recent,
    }

    def package(setups: list) -> dict:
        # 같은 종목·같은 날에 급등봉이 여러 개 걸리면(연속 급등) 가장 최근 급등봉 기준 하나만 남긴다 — 하루 한 거래로 센다
        latest: dict[tuple[str, str], object] = {}
        for s in setups:
            k = (s.code, s.date)
            if k not in latest or s.spike_date > latest[k].spike_date:
                latest[k] = s
        setups = list(latest.values())
        strict = [s for s in setups if s.strict]
        rows = sorted((s for s in setups if s.date in recent_days), key=lambda s: (s.date, -s.amount_eok), reverse=True)
        stats = {"strict": backtest(strict, base), "all": backtest(setups, base), "baseline": round(base, 2)}
        if setups and hasattr(setups[0], "confirm"):  # 다음 날 고가 돌파 매수 성적도 같이
            conf = sorted((s for s in strict if 3 in s.confirm), key=lambda s: s.date)
            stats["strictConfirm"] = backtest([], base, rets=[s.confirm[3] for s in conf])
        if setups and hasattr(setups[0], "close_bet"):  # 고가놀이: 종가배팅(종가 매수 → 다음 날 시가) 성적
            cb = sorted((s for s in setups if s.close_bet and s.gap is not None), key=lambda s: s.date)
            stats["closeBet"] = backtest([], gap_base, rets=[s.gap for s in cb])
            stats["gapBaseline"] = round(gap_base, 2)
        return meta | {
            "stats": stats,
            "rows": [s.as_dict() for s in rows],
            "totalRecent": len(rows),
            "droppedRecent": sum(1 for s in rows if not s.strict),
        }

    # 같은 일봉으로 두 스크리너를 한 번에
    hoga, pull = [], []
    for code, name, label in uni:
        cs = candles.get(code, [])
        hoga += find_setups(cs, code, name, label)
        pull += find_pullbacks(cs, code, name, label)
    result = package(hoga)
    result["pullback"] = package(pull)
    if write:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "hoga.json").write_text(json.dumps({k: v for k, v in result.items() if k != "pullback"}, ensure_ascii=False, indent=1), "utf-8")
        (settings.data_dir / "pullback.json").write_text(json.dumps(result["pullback"], ensure_ascii=False, indent=1), "utf-8")
    return result
