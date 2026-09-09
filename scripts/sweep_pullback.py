"""눌림목 조건 조합 비교. 저장된 일봉(data/candles_*.json) 위에서 조건 값을 바꿔 가며 승률·건당·기준선 대비를 표로 낸다.

  py scripts/sweep_pullback.py                        # 캐시(kiwoom/market)가 있으면 그걸로, 없으면 받아서 저장
  py scripts/sweep_pullback.py --source naver --universe themes
  py scripts/sweep_pullback.py --hold 5               # 5일 보유 기준으로 비교 (기본 3)
  py scripts/sweep_pullback.py --min-n 300            # 표본이 이보다 적은 조합은 뺀다
  py scripts/sweep_pullback.py --stage 2              # 2단계(추가 조건)만

1단계: 기본 조건(눌림 폭·거래량·눌림 일수·20일선·저점·급등 크기) 조합.
2단계: 1단계 1등 조합 위에 추가 조건 — 추세 강도(ADX), 첫 눌림만, 20일선 근처, 직전보다 얕은 눌림, 시장 필터(코스피 20일선 위),
        진입 확인(다음 날 전일 고가 돌파 때 진입) — 을 하나씩·조합으로 붙여 본다.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings  # noqa: E402
from app.screener.pullback import find_pullbacks, sma  # noqa: E402
from app.screener.runner import drop_incomplete_today, get_candles  # noqa: E402

# 1단계 값들. 필요하면 여기만 고친다.
GRID = {
    "depth": [(3, 10), (5, 10), (5, 15), (8, 15), (8, 20), (3, 25)],   # 고점 대비 눌림 폭 (min, max) %
    "vol": [0.3, 0.5, 0.7, 9.9],                                        # 눌림 구간 평균 거래량 / 급등일 (9.9 = 조건 없음)
    "ma20": [True, False],                                              # 종가가 20일선 위여야 하는가
    "low": [True, False],                                               # 급등봉 저점을 지켜야 하는가
    "pull_days": [(1, 3), (1, 5), (2, 10), (1, 10)],                    # 눌림 일수 (min, max)
    "spike": [8, 12],                                                   # 급등봉 최소 등락률 %
}
# 2단계 값들
GRID2 = {
    "adx": [0, 20, 25, 30],            # 자리 날 ADX(14) 하한 (0 = 조건 없음)
    "first": [False, True],            # 첫 눌림만
    "near": [False, True],             # 20일선과의 거리 5% 이내
    "shrink": [False, True],           # 직전 눌림보다 얕은 눌림만 (직전 눌림이 없으면 통과)
    "market": [False, True],           # 코스피가 20일선 위인 날만
    "entry": ["close", "confirm"],     # close = 자리 날 종가 매수, confirm = 다음 날 전일 고가 돌파 시 매수
}


def stats(rets: list[float], base: float) -> dict:
    if not rets:
        return {"n": 0}
    half = len(rets) // 2 or 1
    return {"n": len(rets), "win": round(sum(r > 0 for r in rets) / len(rets) * 100, 1), "avg": round(mean(rets), 2),
            "vsBase": round(mean(rets) - base, 2), "h1": round(mean(rets[:half]), 2), "h2": round(mean(rets[half:]), 2) if rets[half:] else None}


async def kospi_above_ma20() -> dict[str, bool]:
    """날짜(YYYYMMDD) → 코스피 종가가 20일선 위인가. 야후 ^KS11 일봉."""
    h = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(headers=h, timeout=15) as c:
        r = await c.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11", params={"range": "2y", "interval": "1d"})
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
    kst = timezone(timedelta(hours=9))
    pts = [(datetime.fromtimestamp(t, kst).strftime("%Y%m%d"), c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
    closes = [c for _, c in pts]
    ma = sma(closes, 20)
    return {d: (m is not None and c > m) for (d, c), m in zip(pts, ma)}


def fmt_row(r: dict) -> str:
    return (f"  {str(r['depth']):<9}{('없음' if r['vol'] > 9 else str(r['vol'])):<7}{('예' if r['ma20'] else '아니오'):<6}{('예' if r['low'] else '아니오'):<5}"
            f"{str(r['pullDays']):<8}{r['spike']:<5}{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")


def fmt_row2(r: dict) -> str:
    return (f"  {('없음' if not r['adx'] else str(r['adx'])):<6}{('예' if r['first'] else '아니오'):<7}{('예' if r['near'] else '아니오'):<7}"
            f"{('예' if r['shrink'] else '아니오'):<7}{('예' if r['market'] else '아니오'):<7}{('돌파' if r['entry'] == 'confirm' else '종가'):<6}"
            f"{r['n']:>6}{r['win']:>6}%{r['avg']:>+8.2f}{r['vsBase']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto")
    ap.add_argument("--universe", choices=["themes", "market"], default="market")
    ap.add_argument("--hold", type=int, default=3, choices=[1, 3, 5, 10])
    ap.add_argument("--min-n", type=int, default=300)
    ap.add_argument("--stage", type=int, default=0, help="0 = 둘 다, 1 = 기본 조합만, 2 = 추가 조건만")
    ap.add_argument("--refresh", action="store_true", help="캐시를 무시하고 일봉을 새로 받는다")
    args = ap.parse_args()
    H = args.hold

    source, uni, candles = await get_candles(args.source, args.universe, 250, 30.0, use_cache=not args.refresh)
    drop_incomplete_today(candles)
    print(f"{source} · {args.universe} · {len(uni)}종목 · 보유 {H}일")

    cands = []
    for code, name, label in uni:
        cands += find_pullbacks(candles.get(code, []), code, name, label, spike_min=min(GRID["spike"]), depth_any_min=3.0, lookahead=10)
    # 같은 종목·같은 날은 가장 최근 급등봉 하나만 (하루 한 거래)
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

    def ret(c, entry="close"):
        return c.fwd.get(H) if entry == "close" else c.confirm.get(H)

    # ── 1단계 ──
    rows = []
    if args.stage in (0, 1):
        print("[조건 하나씩] 통과했을 때 / 안 했을 때 평균 수익")
        single = {"눌림 폭 -3~-10%": lambda c: -10 <= c.depth_pct <= -3, "거래량 ≤0.3배": lambda c: c.vol_ratio <= 0.3, "20일선 위": lambda c: c.ma20_pct >= 0,
                  "급등봉 저점 유지": lambda c: c.low_hold, "눌림 3일 이내": lambda c: c.pull_days <= 3, "급등 12% 이상": lambda c: c.spike_pct >= 12}
        for label, f in single.items():
            yes = [c.fwd[H] for c in cands if f(c)]
            no = [c.fwd[H] for c in cands if not f(c)]
            print(f"  {label:<14} 통과 n={len(yes):>5} 승률 {stats(yes, base).get('win', 0):>5}% 평균 {mean(yes) if yes else 0:+.2f}%   |  탈락 n={len(no):>5} 평균 {mean(no) if no else 0:+.2f}%")
        for depth, vol, ma, low, pd, spike in itertools.product(*GRID.values()):
            sel = [c for c in cands if depth[0] <= -c.depth_pct <= depth[1] and c.vol_ratio <= vol and (not ma or c.ma20_pct >= 0)
                   and (not low or c.low_hold) and pd[0] <= c.pull_days <= pd[1] and c.spike_pct >= spike]
            st = stats([c.fwd[H] for c in sel], base)
            if st["n"] >= args.min_n:
                rows.append({"depth": depth, "vol": vol, "ma20": ma, "low": low, "pullDays": pd, "spike": spike, **st})
        rows.sort(key=lambda r: r["vsBase"], reverse=True)
        print(f"\n[1단계 · 조합 {len(rows)}개, 표본 {args.min_n} 이상] 기준선 대비 상위")
        print(f"  {'눌림폭':<9}{'거래량':<7}{'20일선':<6}{'저점':<5}{'눌림일':<8}{'급등':<5}{'n':>6}{'승률':>7}{'건당':>8}{'대비':>8}{'전반':>7}{'후반':>7}")
        for r in rows[:10]:
            print(fmt_row(r))
        result["combos"] = rows

    # ── 2단계: 기본 조합(-3~-10%, 0.3배, 1~3일, 20일선 위, 저점 유지, 급등 8%) 위에 추가 조건 ──
    if args.stage in (0, 2):
        base_sel = [c for c in cands if -10 <= c.depth_pct <= -3 and c.vol_ratio <= 0.3 and c.ma20_pct >= 0 and c.low_hold and c.pull_days <= 3]
        print(f"\n[2단계] 기본 조합 {len(base_sel)}자리 위에 추가 조건")
        try:
            mkt = await kospi_above_ma20()
        except Exception as e:
            print(f"  (코스피 20일선 자료를 못 받아 시장 필터는 뺍니다: {e})")
            mkt = {}
        singles2 = {
            "ADX ≥25": lambda c: c.adx is not None and c.adx >= 25,
            "ADX ≥30": lambda c: c.adx is not None and c.adx >= 30,
            "첫 눌림만": lambda c: c.pull_no == 1,
            "20일선 5% 이내": lambda c: abs(c.ma20_pct) <= 5,
            "직전보다 얕은 눌림": lambda c: c.prev_depth is None or c.depth_pct > c.prev_depth,
            "코스피 20일선 위": lambda c: mkt.get(c.date, True),
            "추세일수 ≥15/20": lambda c: c.trend_days >= 15,
        }
        print("  조건 하나씩 (종가 매수 기준): 통과 / 탈락")
        single2_out = {}
        for label, f in singles2.items():
            yes = [c.fwd[H] for c in base_sel if f(c)]
            no = [c.fwd[H] for c in base_sel if not f(c)]
            single2_out[label] = {"pass": stats(yes, base), "fail": stats(no, base)}
            print(f"    {label:<16} 통과 n={len(yes):>5} 승률 {stats(yes, base).get('win', 0):>5}% 평균 {mean(yes) if yes else 0:+.2f}%   |  탈락 n={len(no):>5} 승률 {stats(no, base).get('win', 0):>5}% 평균 {mean(no) if no else 0:+.2f}%")
        conf = [c.confirm[H] for c in base_sel if H in c.confirm]
        print(f"    {'진입 확인(돌파 매수)':<16} 체결 n={len(conf):>5} 승률 {stats(conf, base).get('win', 0):>5}% 평균 {mean(conf) if conf else 0:+.2f}%   |  종가 매수 n={len(base_sel):>5} 평균 {mean([c.fwd[H] for c in base_sel]) if base_sel else 0:+.2f}%")

        rows2 = []
        for adx_min, first, near, shrink, market, entry in itertools.product(*GRID2.values()):
            sel = [c for c in base_sel if (not adx_min or (c.adx is not None and c.adx >= adx_min)) and (not first or c.pull_no == 1)
                   and (not near or abs(c.ma20_pct) <= 5) and (not shrink or c.prev_depth is None or c.depth_pct > c.prev_depth)
                   and (not market or mkt.get(c.date, True))]
            rets = [r for r in (ret(c, entry) for c in sel) if r is not None]
            st = stats(rets, base)
            if st["n"] >= max(100, args.min_n // 3):
                rows2.append({"adx": adx_min, "first": first, "near": near, "shrink": shrink, "market": market, "entry": entry, **st})
        rows2.sort(key=lambda r: r["vsBase"], reverse=True)
        print(f"\n  [조합 {len(rows2)}개] 기준선 대비 상위")
        print(f"  {'ADX':<6}{'첫눌림':<7}{'MA근처':<7}{'얕아짐':<7}{'시장':<7}{'진입':<6}{'n':>6}{'승률':>7}{'건당':>8}{'대비':>8}{'전반':>7}{'후반':>7}")
        for r in rows2[:12]:
            print(fmt_row2(r))
        print("  [하위 3]")
        for r in rows2[-3:]:
            print(fmt_row2(r))
        result["stage2"] = {"base": stats([c.fwd[H] for c in base_sel], base), "single": single2_out, "combos": rows2}

    out = settings.data_dir / "sweep_pullback.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
