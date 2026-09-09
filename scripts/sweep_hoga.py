"""고가놀이 조건 조합 비교. 저장된 일봉(data/candles_*.json) 위에서 조건 값을 바꿔 가며 승률·건당·기준선 대비를 표로 낸다.

  py scripts/sweep_hoga.py                  # 캐시(kiwoom/market)가 있으면 그걸로, 없으면 받아서 저장
  py scripts/sweep_hoga.py --hold 5
  py scripts/sweep_hoga.py --stage 2        # 추가 조건(추세 길이·거래량·돌파 진입·시장 필터)만

1단계: 횡보 폭 · 횡보 일수 · 고가 대비 · 저점 상승 · MACD · 급등 크기.
2단계: 1단계 기본 조합 위에 횡보 구간 거래량 비율 · 추세 길이 · 코스피 20일선 · 다음 날 자리 날 고가 돌파 진입.
읽는 법은 sweep_pullback.py 와 같다. 결과는 data/sweep_hoga.json 에 저장.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings  # noqa: E402
from app.screener.hoga_play import find_setups  # noqa: E402
from app.screener.runner import drop_incomplete_today, get_candles  # noqa: E402
from scripts.sweep_pullback import kospi_above_ma20, stats  # noqa: E402

GRID = {
    "box": [6, 8, 10, 15, 99],                 # 횡보 폭 상한 % (99 = 조건 없음)
    "hold": [(1, 1), (1, 2), (1, 3), (2, 3)],  # 횡보 일수 (min, max)
    "vshigh": [-3, -5, -8, -15],               # 급등봉 고가 대비 하한 %
    "lowup": [True, False],                    # 저점 상승
    "macd": [True, False],                     # MACD 0선 위
    "spike": [8, 12],                          # 급등봉 최소 등락률 %
}
GRID2 = {
    "vol": [0.3, 0.5, 0.7, 9.9],               # 횡보 구간 평균 거래량 / 급등일 (9.9 = 없음)
    "trend": [99, 15, 10],                     # 자리 전 20일 중 20일선 위 일수 상한 (99 = 없음)
    "market": [False, True],                   # 코스피 20일선 위
    "entry": ["close", "confirm"],             # 종가 매수 / 다음 날 자리 날 고가 돌파 매수
}


def row1(r):
    return (f"  {('없음' if r['box'] > 50 else str(r['box'])):<6}{str(r['hold']):<8}{r['vshigh']:<7}{('예' if r['lowup'] else '아니오'):<6}{('예' if r['macd'] else '아니오'):<6}{r['spike']:<5}"
            f"{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")


def row2(r):
    return (f"  {('없음' if r['vol'] > 9 else str(r['vol'])):<7}{('없음' if r['trend'] > 50 else str(r['trend'])):<7}{('예' if r['market'] else '아니오'):<6}{('돌파' if r['entry'] == 'confirm' else '종가'):<6}"
            f"{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto")
    ap.add_argument("--universe", choices=["themes", "market"], default="market")
    ap.add_argument("--hold", type=int, default=3, choices=[1, 3, 5, 10])
    ap.add_argument("--min-n", type=int, default=300)
    ap.add_argument("--stage", type=int, default=0)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    H = args.hold

    source, uni, candles = await get_candles(args.source, args.universe, 250, 30.0, use_cache=not args.refresh)
    drop_incomplete_today(candles)
    print(f"{source} · {args.universe} · {len(uni)}종목 · 보유 {H}일")

    cands = []
    for code, name, label in uni:
        cands += find_setups(candles.get(code, []), code, name, label, spike_min=min(GRID["spike"]), box_max=999, vs_high_min=min(GRID["vshigh"]))
    latest = {}
    for c in cands:
        k = (c.code, c.date)
        if k not in latest or c.spike_date > latest[k].spike_date:
            latest[k] = c
    cands = sorted((c for c in latest.values() if H in c.fwd), key=lambda c: c.date)
    base_rets = [(cs[i + H].close / cs[i].close - 1) * 100 for cs in candles.values() for i in range(len(cs) - H) if cs[i].close > 0]
    base = mean(base_rets) if base_rets else 0.0
    print(f"후보 {len(cands)}자리 · 기준선 {base:+.2f}%\n")
    result = {"source": source, "universe": args.universe, "hold": H, "baseline": round(base, 2), "candidates": len(cands)}

    rows = []
    if args.stage in (0, 1):
        print("[조건 하나씩] 통과 / 탈락 평균 수익")
        single = {"횡보 폭 <10%": lambda c: c.box_pct < 10, "횡보 폭 <6%": lambda c: c.box_pct < 6, "고가대비 ≥-5%": lambda c: c.vs_high_pct >= -5,
                  "저점 상승": lambda c: c.low_up, "MACD 0선 위": lambda c: c.macd_pos, "횡보 1일": lambda c: c.hold_days == 1, "급등 12%↑": lambda c: c.spike_pct >= 12}
        for label, f in single.items():
            yes = [c.fwd[H] for c in cands if f(c)]
            no = [c.fwd[H] for c in cands if not f(c)]
            print(f"  {label:<13} 통과 n={len(yes):>5} 승률 {stats(yes, base).get('win', 0):>5}% 평균 {mean(yes) if yes else 0:+.2f}%   |  탈락 n={len(no):>5} 평균 {mean(no) if no else 0:+.2f}%")
        for box, hold, vsh, lowup, mac, spike in itertools.product(*GRID.values()):
            sel = [c for c in cands if c.box_pct < box and hold[0] <= c.hold_days <= hold[1] and c.vs_high_pct >= vsh
                   and (not lowup or c.low_up) and (not mac or c.macd_pos) and c.spike_pct >= spike]
            st = stats([c.fwd[H] for c in sel], base)
            if st["n"] >= args.min_n:
                rows.append({"box": box, "hold": hold, "vshigh": vsh, "lowup": lowup, "macd": mac, "spike": spike, **st})
        rows.sort(key=lambda r: r["vsBase"], reverse=True)
        print(f"\n[1단계 · 조합 {len(rows)}개, 표본 {args.min_n} 이상] 기준선 대비 상위")
        print(f"  {'횡보폭':<6}{'횡보일':<8}{'고가대비':<7}{'저점↑':<6}{'MACD':<6}{'급등':<5}{'n':>6}{'승률':>7}{'건당':>8}{'대비':>8}{'전반':>7}{'후반':>7}")
        for r in rows[:12]:
            print(row1(r))
        print("  [하위 3]")
        for r in rows[-3:]:
            print(row1(r))
        old = [c for c in cands if c.box_pct < 10 and c.vs_high_pct >= -15 and c.low_up and c.macd_pos]
        print(f"\n  처음 기본값(폭<10, 고가대비≥-15, 저점↑, MACD): n={len(old)} " + json.dumps(stats([c.fwd[H] for c in old], base), ensure_ascii=False))
        result["combos"] = rows

    if args.stage in (0, 2):
        # 1단계 결과의 기본 조합: 폭<6 · 고가 대비 ≥-3 · MACD · 저점↑ · 횡보 1~3일 · 급등 8%
        base_sel = [c for c in cands if c.box_pct < 6 and c.vs_high_pct >= -3 and c.macd_pos and c.low_up]
        print(f"\n[2단계] 기본 조합(폭<6·고가대비≥-3·MACD·저점↑) {len(base_sel)}자리 위에 추가 조건")
        try:
            mkt = await kospi_above_ma20()
        except Exception as e:
            print(f"  (코스피 자료 없음: {e})")
            mkt = {}
        singles2 = {"거래량 ≤0.5배": lambda c: c.vol_ratio <= 0.5, "거래량 ≤0.3배": lambda c: c.vol_ratio <= 0.3, "추세 <15/20일": lambda c: c.trend_days < 15,
                    "추세 <10/20일": lambda c: c.trend_days < 10, "코스피 20일선 위": lambda c: mkt.get(c.date, True)}
        for label, f in singles2.items():
            yes = [c.fwd[H] for c in base_sel if f(c)]
            no = [c.fwd[H] for c in base_sel if not f(c)]
            print(f"    {label:<14} 통과 n={len(yes):>5} 승률 {stats(yes, base).get('win', 0):>5}% 평균 {mean(yes) if yes else 0:+.2f}%   |  탈락 n={len(no):>5} 승률 {stats(no, base).get('win', 0):>5}% 평균 {mean(no) if no else 0:+.2f}%")
        conf = [c.confirm[H] for c in base_sel if H in c.confirm]
        print(f"    {'돌파 매수':<14} 체결 n={len(conf):>5} 승률 {stats(conf, base).get('win', 0):>5}% 평균 {mean(conf) if conf else 0:+.2f}%   |  종가 매수 n={len(base_sel):>5} 평균 {mean([c.fwd[H] for c in base_sel]) if base_sel else 0:+.2f}%")
        rows2 = []
        for vol, trend, market, entry in itertools.product(*GRID2.values()):
            sel = [c for c in base_sel if c.vol_ratio <= vol and c.trend_days < trend and (not market or mkt.get(c.date, True))]
            rets = [r for r in ((c.fwd.get(H) if entry == "close" else c.confirm.get(H)) for c in sel) if r is not None]
            st = stats(rets, base)
            if st["n"] >= max(100, args.min_n // 3):
                rows2.append({"vol": vol, "trend": trend, "market": market, "entry": entry, **st})
        rows2.sort(key=lambda r: r["vsBase"], reverse=True)
        print(f"\n  [조합 {len(rows2)}개] 기준선 대비 상위")
        print(f"  {'거래량':<7}{'추세<':<7}{'시장':<6}{'진입':<6}{'n':>6}{'승률':>7}{'건당':>8}{'대비':>8}{'전반':>7}{'후반':>7}")
        for r in rows2[:10]:
            print(row2(r))
        print("  [하위 3]")
        for r in rows2[-3:]:
            print(row2(r))
        result["stage2"] = rows2

    out = settings.data_dir / "sweep_hoga.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
