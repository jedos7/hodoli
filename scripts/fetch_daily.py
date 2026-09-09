"""일봉을 받아 고가놀이 스크리너를 돌리고 data/hoga.json 을 만든다. 장 마감 후 하루 한 번 실행.

  py scripts/fetch_daily.py            # KIS_ENV 에 따라 REST 로 일봉 수집 (mock 이면 합성 일봉)
  py scripts/fetch_daily.py --days 180 # 수집 기간
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 출력

from app.config import settings  # noqa: E402
from app.kis.rest import Candle  # noqa: E402
from app.screener.hoga_play import backtest, baseline, find_setups  # noqa: E402


def load_universe() -> list[tuple[str, str, str]]:
    d = json.loads(settings.themes_file.read_text("utf-8"))
    return [(s["code"], s["name"], t["name"]) for t in d["themes"] for s in t["stocks"]]


def synthetic_candles(days: int, seed: int, start_price: int = 20000) -> list[Candle]:
    """모의 모드용 합성 일봉. 가끔 급등 후 며칠 횡보하는 패턴을 섞어 넣는다."""
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
            plan = rng.randint(1, 4)  # 1: 급등봉, 이후 횡보
        if plan == 1:
            r = rng.uniform(0.09, 0.2)
        elif plan > 1:
            r = rng.gauss(0.0, 0.015)
        else:
            r = rng.gauss(0.0005, 0.02)
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


async def fetch_real(universe, days: int) -> dict[str, list[Candle]]:
    from app.kis.auth import KisAuth
    from app.kis.rest import KisRest

    auth = KisAuth(settings)
    rest = KisRest(settings, auth)
    end = date.today()
    start = end - timedelta(days=int(days * 1.5))
    out: dict[str, list[Candle]] = {}
    try:
        for code, name, _ in universe:
            cs: list[Candle] = []
            cur_end = end
            while len(cs) < days:  # 한 번에 100봉까지라 여러 번 나눠 받는다
                chunk = await rest.daily_candles(code, start.strftime("%Y%m%d"), cur_end.strftime("%Y%m%d"))
                if not chunk:
                    break
                cs = chunk + cs
                first = date(int(chunk[0].date[:4]), int(chunk[0].date[4:6]), int(chunk[0].date[6:]))
                cur_end = first - timedelta(days=1)
                if len(chunk) < 100:
                    break
            out[code] = cs[-days:]
            print(f"  {name}({code}) {len(out[code])}봉")
    finally:
        await rest.close()
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--recent", type=int, default=5, help="표에 보여줄 최근 거래일 수")
    args = ap.parse_args()

    universe = load_universe()
    if settings.is_mock:
        print(f"모의 모드: 합성 일봉 {len(universe)}종목")
        candles = {code: synthetic_candles(args.days, seed=int(code), start_price=random.Random(code).choice([3000, 12000, 45000, 150000])) for code, _, _ in universe}
    else:
        settings.validate()
        print(f"{settings.env}: REST 일봉 수집 {len(universe)}종목")
        candles = await fetch_real(universe, args.days)

    setups = []
    for code, name, theme in universe:
        setups += find_setups(candles.get(code, []), code, name, theme)
    base = baseline(candles)
    strict = [s for s in setups if s.strict]
    asof = max((cs[-1].date for cs in candles.values() if cs), default=None)
    dates = sorted({c.date for cs in candles.values() for c in cs})
    recent = set(dates[-args.recent:])
    rows = sorted((s for s in setups if s.date in recent), key=lambda s: (s.date, -s.amount_eok), reverse=True)

    result = {
        "asof": asof,
        "env": settings.env,
        "stats": {"strict": backtest(strict, base), "all": backtest(setups, base), "baseline": round(base, 2)},
        "recentDays": args.recent,
        "rows": [s.as_dict() for s in rows],
        "totalRecent": len(rows),
        "droppedRecent": sum(1 for s in rows if not s.strict),
    }
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    out = settings.data_dir / "hoga.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    st = result["stats"]
    print(f"확정 일봉 {asof} 까지 · 최근 {args.recent}거래일 {len(rows)}자리 (엄선 {len(rows) - result['droppedRecent']})")
    print(f"엄선 {st['strict']['n']}자리 승률 {st['strict']['win']}% 건당 {st['strict']['avg']:+}% · 전체 {st['all']['n']}자리 승률 {st['all']['win']}% · 기준선 {base:+.2f}%")
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
