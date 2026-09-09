"""일별 테마 달력 데이터를 만든다 (전체 테마 매핑 + 전 종목 일봉 + 지수). 처음엔 5분쯤, 그날 두 번째부터는 캐시로 몇 초.

  py scripts/build_calendar.py
  py scripts/build_calendar.py --refresh      # 오늘 캐시가 있어도 다시 받는다
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings  # noqa: E402
from app.screener.theme_calendar import build_calendar  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    t0 = time.time()

    def progress(i, n, name):
        if i % 50 == 0 or i == n:
            print(f"  {i}/{n} {name}")

    r = await build_calendar(progress, refresh_map=args.refresh, refresh_candles=args.refresh)
    days = sorted(r["days"])
    print(f"\n테마 {r['themes']}개 · 종목 {r['stocks']}개 · 거래일 {len(days)}일 ({days[0]}~{days[-1]}) · {time.time() - t0:.0f}초")
    last = r["days"][days[-1]]
    print(f"{days[-1]} 코스피 {last['kospi']:+.2f}% 코스닥 {last['kosdaq']:+.2f}%")
    for t in last["themes"][:5]:
        print(f"  {t['name']:<22} 상위4 {t['top4']:+.2f}% 중앙 {t['median']:+.2f}% {t['n']}종목 대표 {t['rep']} heat {t['heat']}")
    print(f"→ {settings.data_dir / 'theme_calendar.json'}")


if __name__ == "__main__":
    asyncio.run(main())
