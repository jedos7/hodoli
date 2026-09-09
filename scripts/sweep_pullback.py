"""눌림목 조건 조합 비교. 저장된 일봉(data/candles_*.json) 위에서 조건 값을 바꿔 가며 승률·건당·기준선 대비를 표로 낸다.

  py scripts/sweep_pullback.py                        # 캐시(kiwoom/market)가 있으면 그걸로, 없으면 받아서 저장
  py scripts/sweep_pullback.py --source naver --universe themes
  py scripts/sweep_pullback.py --hold 5               # 5일 보유 기준으로 비교 (기본 3)
  py scripts/sweep_pullback.py --min-n 300            # 표본이 이보다 적은 조합은 뺀다

읽는 법
  - 기준선 = 같은 종목·같은 기간에서 아무 날이나 사서 같은 일수 들고 있을 때의 평균 수익. 이걸 못 이기면 조건이 의미가 없다.
  - 전반/후반 = 기간을 반으로 나눈 성적. 둘 다 좋아야 우연이 아니다.
  - 표본(n)이 적으면 승률이 튀므로 --min-n 아래는 무시한다.
결과는 data/sweep_pullback.json 에도 저장한다.
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
from app.screener.pullback import find_pullbacks  # noqa: E402
from app.screener.runner import drop_incomplete_today, get_candles  # noqa: E402

# 비교할 조건 값들. 필요하면 여기만 고친다.
GRID = {
    "depth": [(3, 10), (5, 10), (5, 15), (8, 15), (8, 20), (3, 25)],   # 고점 대비 눌림 폭 (min, max) %
    "vol": [0.3, 0.5, 0.7, 9.9],                                        # 눌림 구간 평균 거래량 / 급등일 (9.9 = 조건 없음)
    "ma20": [True, False],                                              # 종가가 20일선 위여야 하는가
    "low": [True, False],                                               # 급등봉 저점을 지켜야 하는가
    "pull_days": [(1, 3), (1, 5), (2, 10), (1, 10)],                    # 눌림 일수 (min, max)
    "spike": [8, 12],                                                   # 급등봉 최소 등락률 %
}


def stats(rets: list[float], base: float) -> dict:
    if not rets:
        return {"n": 0}
    half = len(rets) // 2 or 1
    return {"n": len(rets), "win": round(sum(r > 0 for r in rets) / len(rets) * 100, 1), "avg": round(mean(rets), 2),
            "vsBase": round(mean(rets) - base, 2), "h1": round(mean(rets[:half]), 2), "h2": round(mean(rets[half:]), 2) if rets[half:] else None}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto")
    ap.add_argument("--universe", choices=["themes", "market"], default="market")
    ap.add_argument("--hold", type=int, default=3, choices=[1, 3, 5, 10])
    ap.add_argument("--min-n", type=int, default=300)
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 일봉을 새로 받는다")
    args = ap.parse_args()

    source, uni, candles = await get_candles(args.source, args.universe, 250, 30.0, use_cache=not args.refresh)
    drop_incomplete_today(candles)
    print(f"{source} · {args.universe} · {len(uni)}종목 · 보유 {args.hold}일")

    # 후보를 가장 느슨한 조건으로 한 번만 뽑고(급등 8%), 조합은 특징값으로 거른다
    cands = []
    for code, name, label in uni:
        cands += find_pullbacks(candles.get(code, []), code, name, label, spike_min=min(GRID["spike"]), depth_any_min=3.0, lookahead=10)
    cands = [c for c in cands if args.hold in c.fwd]
    cands.sort(key=lambda c: c.date)
    # 기준선: 모든 봉의 hold 일 수익률 평균
    base_rets = [(cs[i + args.hold].close / cs[i].close - 1) * 100 for cs in candles.values() for i in range(len(cs) - args.hold) if cs[i].close > 0]
    base = mean(base_rets) if base_rets else 0.0
    print(f"후보 {len(cands)}자리 · 기준선 {base:+.2f}%\n")

    # 조건별 단독 효과 (통과 vs 탈락)
    print("[조건 하나씩] 통과했을 때 / 안 했을 때 평균 수익")
    single = {
        "눌림 폭 -5~-15%": lambda c: -15 <= c.depth_pct <= -5,
        "거래량 ≤0.5배": lambda c: c.vol_ratio <= 0.5,
        "거래량 ≤0.3배": lambda c: c.vol_ratio <= 0.3,
        "20일선 위": lambda c: c.ma20_pct >= 0,
        "급등봉 저점 유지": lambda c: c.low_hold,
        "눌림 3일 이내": lambda c: c.pull_days <= 3,
        "급등 12% 이상": lambda c: c.spike_pct >= 12,
    }
    single_out = {}
    for label, f in single.items():
        yes = [c.fwd[args.hold] for c in cands if f(c)]
        no = [c.fwd[args.hold] for c in cands if not f(c)]
        single_out[label] = {"pass": stats(yes, base), "fail": stats(no, base)}
        print(f"  {label:<14} 통과 n={len(yes):>5} 승률 {stats(yes, base).get('win', 0):>5}% 평균 {mean(yes) if yes else 0:+.2f}%   |  탈락 n={len(no):>5} 평균 {mean(no) if no else 0:+.2f}%")

    # 조합
    rows = []
    for depth, vol, ma, low, pd, spike in itertools.product(*GRID.values()):
        sel = [c for c in cands if depth[0] <= -c.depth_pct <= depth[1] and c.vol_ratio <= vol and (not ma or c.ma20_pct >= 0)
               and (not low or c.low_hold) and pd[0] <= c.pull_days <= pd[1] and c.spike_pct >= spike]
        st = stats([c.fwd[args.hold] for c in sel], base)
        if st["n"] < args.min_n:
            continue
        rows.append({"depth": depth, "vol": vol, "ma20": ma, "low": low, "pullDays": pd, "spike": spike, **st})
    rows.sort(key=lambda r: r["vsBase"], reverse=True)
    print(f"\n[조합 {len(rows)}개, 표본 {args.min_n} 이상] 기준선 대비 상위")
    print(f"  {'눌림폭':<9}{'거래량':<7}{'20일선':<6}{'저점':<5}{'눌림일':<8}{'급등':<5}{'n':>6}{'승률':>7}{'건당':>8}{'대비':>8}{'전반':>7}{'후반':>7}")
    for r in rows[:15]:
        print(f"  {str(r['depth']):<9}{('없음' if r['vol'] > 9 else str(r['vol'])):<7}{('예' if r['ma20'] else '아니오'):<6}{('예' if r['low'] else '아니오'):<5}{str(r['pullDays']):<8}{r['spike']:<5}{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")
    print("\n[기준선 대비 하위 5]")
    for r in rows[-5:]:
        print(f"  {str(r['depth']):<9}{('없음' if r['vol'] > 9 else str(r['vol'])):<7}{('예' if r['ma20'] else '아니오'):<6}{('예' if r['low'] else '아니오'):<5}{str(r['pullDays']):<8}{r['spike']:<5}{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}")

    out = settings.data_dir / "sweep_pullback.json"
    out.write_text(json.dumps({"source": source, "universe": args.universe, "hold": args.hold, "baseline": round(base, 2),
                               "candidates": len(cands), "single": single_out, "combos": rows}, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
