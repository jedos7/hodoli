"""일봉을 받아 고가놀이 스크리너를 돌리고 data/hoga.json 을 만든다. 장 마감 후 하루 한 번 실행.

  py scripts/fetch_daily.py                         # 네이버 일봉, themes.json 종목 (수 초)
  py scripts/fetch_daily.py --universe market       # 코스피+코스닥 거래대금 30억 이상 전 종목 (1~3분)
  py scripts/fetch_daily.py --source kis            # KIS REST 일봉 (KIS_ENV=vts/real 필요)
  py scripts/fetch_daily.py --source mock           # 합성 일봉 (UI 확인용)

서버가 떠 있으면 화면의 '다시 찾기' 버튼이나 POST /api/screener/run 으로 같은 일을 한다.
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
from app.screener.runner import run_screener  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["auto", "naver", "kis", "kiwoom", "mock"], default="naver")
    ap.add_argument("--universe", choices=["themes", "market"], default="themes")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--recent", type=int, default=5, help="표에 보여줄 최근 거래일 수")
    ap.add_argument("--min-amount", type=float, default=30.0, help="market 범위일 때 오늘 거래대금 하한(억)")
    ap.add_argument("--include-today", action="store_true", help="장중에도 오늘(미완성) 봉을 판정에 포함")
    ap.add_argument("--use-cache", action="store_true", help="data/candles_*.json 에 저장된 일봉을 다시 쓴다 (새로 받지 않음)")
    args = ap.parse_args()

    t0 = time.time()

    def progress(i, n, name):
        if i % 50 == 0 or i == n:
            print(f"  {i}/{n} {name}")

    r = await run_screener(args.source, args.universe, args.days, args.recent, args.min_amount, progress, include_today=args.include_today,
                           use_cache=args.use_cache)
    st = r["stats"]
    print(f"\n확정 일봉 {r['asof']} 까지 · {r['source']} · {r['universe']} {r['stocks']}종목 · {time.time() - t0:.0f}초"
          + ("" if r["todayIncluded"] else " · 장중이라 오늘 봉 제외"))
    print(f"최근 {args.recent}거래일 {r['totalRecent']}자리 (엄선 {r['totalRecent'] - r['droppedRecent']})")
    print(f"엄선 {st['strict']['n']}자리 승률 {st['strict']['win']}% 건당 {st['strict']['avg']:+}% · 전체 {st['all']['n']}자리 승률 {st['all']['win']}% · 기준선 {st['baseline']:+}%")
    for row in r["rows"][:12]:
        mark = "" if row["strict"] else " ✗ " + " · ".join(row["reasons"])
        print(f"  {row['date']} {row['name']:<14} {row['theme']:<10} 급등 {row['spikeDate'][4:]} {row['spikePct']:+.1f}%  횡보 {row['holdDays']}일 {row['boxPct']:.0f}%  고가대비 {row['vsHighPct']:.1f}%{mark}")
    print(f"→ {settings.data_dir / 'hoga.json'}")

    pb = r.get("pullback")
    if pb:
        st = pb["stats"]
        print(f"\n[눌림목] 최근 {args.recent}거래일 {pb['totalRecent']}자리 (엄선 {pb['totalRecent'] - pb['droppedRecent']})")
        print(f"엄선 {st['strict']['n']}자리 승률 {st['strict']['win']}% 건당 {st['strict']['avg']:+}% · 전체 {st['all']['n']}자리 승률 {st['all']['win']}% · 기준선 {st['baseline']:+}%")
        for row in [x for x in pb["rows"] if x["strict"]][:12]:
            print(f"  {row['date']} {row['name']:<14} {row['theme']:<10} 급등 {row['spikeDate'][4:]} {row['spikePct']:+.1f}%  고점대비 {row['depthPct']:.1f}%  눌림 {row['pullDays']}일  거래량 {row['volRatio']:.2f}배  20일선 {row['ma20Pct']:+.1f}%")
        print(f"→ {settings.data_dir / 'pullback.json'}")


if __name__ == "__main__":
    asyncio.run(main())
