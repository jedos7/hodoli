"""종가배팅 검증 — 오늘 종가에 사서 내일 시가(또는 내일 종가)에 팔 때의 성적을 조건별로 비교한다.

  py scripts/sweep_close.py                 # 캐시(kiwoom/market) 일봉으로
  py scripts/sweep_close.py --exit close    # 내일 종가 매도 기준
  py scripts/sweep_close.py --min-n 300

읽는 법
  갭 승률  = 내일 시가 > 오늘 종가 인 비율. 기준선(전 종목·전 일자)을 넘어야 의미가 있다.
  갭 평균  = (내일 시가 / 오늘 종가 - 1) %. 수수료·세금 왕복 약 0.25% 를 빼고 봐야 한다.
  전반/후반 = 기간을 반으로 나눈 성적. 둘 다 좋아야 우연이 아니다.
결과는 data/sweep_close.json 에 저장.
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
from app.screener.pullback import find_pullbacks, sma  # noqa: E402
from app.screener.runner import drop_incomplete_today, get_candles  # noqa: E402
from app.screener.theme_calendar import index_daily  # noqa: E402

COST = 0.25  # 왕복 비용 추정 % (수수료 + 매도 세금)


def stats(rets: list[float], base: float) -> dict:
    if not rets:
        return {"n": 0}
    half = len(rets) // 2 or 1
    return {"n": len(rets), "win": round(sum(r > 0 for r in rets) / len(rets) * 100, 1), "avg": round(mean(rets), 2),
            "vsBase": round(mean(rets) - base, 2), "h1": round(mean(rets[:half]), 2), "h2": round(mean(rets[half:]), 2) if rets[half:] else None,
            "net": round(mean(rets) - COST, 2)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto")
    ap.add_argument("--universe", choices=["themes", "market"], default="market")
    ap.add_argument("--exit", choices=["open", "close"], default="open", help="open = 내일 시가 매도, close = 내일 종가 매도")
    ap.add_argument("--min-n", type=int, default=300)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    source, uni, candles = await get_candles(args.source, args.universe, 250, 30.0, use_cache=not args.refresh)
    drop_incomplete_today(candles)
    try:
        kospi = await index_daily("KOSPI", 300)
    except Exception:
        kospi = {}
    print(f"{source} · {args.universe} · {len(uni)}종목 · 매도 = 내일 {'시가' if args.exit == 'open' else '종가'}")

    # 종목-일자별 특징값
    rows = []
    for code, name, label in uni:
        cs = candles.get(code, [])
        if len(cs) < 70:
            continue
        closes = [float(c.close) for c in cs]
        ma20 = sma(closes, 20)
        vols = [float(c.volume) for c in cs]
        vma20 = sma(vols, 20)
        for i in range(60, len(cs) - 1):
            c, n = cs[i], cs[i + 1]
            if c.close <= 0 or cs[i - 1].close <= 0 or n.open <= 0:
                continue
            ret = ((n.open if args.exit == "open" else n.close) / c.close - 1) * 100
            rows.append({
                "date": c.date, "code": code, "ret": ret,
                "chg": (c.close / cs[i - 1].close - 1) * 100,
                "near_high": (c.close / c.high - 1) * 100 if c.high else -99,   # 0 에 가까울수록 고가 마감
                "bull": c.close > c.open,
                "amt": c.amount / 1e8,
                "vol_x": (c.volume / vma20[i]) if vma20[i] else 0.0,
                "above_ma20": ma20[i] is not None and c.close > ma20[i],
                "new_high60": c.close >= max(x.close for x in cs[i - 59 : i + 1]),
                "kospi_up": kospi.get(c.date, 0.0) > 0,
                "upper_wick": (c.high / max(c.close, c.open) - 1) * 100,     # 윗꼬리 길이 %
            })
    rows.sort(key=lambda r: r["date"])
    base = mean(r["ret"] for r in rows)
    print(f"표본 {len(rows):,} 종목·일 · 기준선(아무 날 종가 매수) 승률 {sum(r['ret'] > 0 for r in rows) / len(rows) * 100:.1f}% 평균 {base:+.2f}%\n")

    # ── 조건 하나씩 ──
    single = {
        "고가 1% 이내 마감": lambda r: r["near_high"] >= -1,
        "고가 3% 이내 마감": lambda r: r["near_high"] >= -3,
        "양봉": lambda r: r["bull"],
        "등락 +3% 이상": lambda r: r["chg"] >= 3,
        "등락 +8% 이상": lambda r: r["chg"] >= 8,
        "등락 +15% 이상": lambda r: r["chg"] >= 15,
        "상한가(+29%)": lambda r: r["chg"] >= 29,
        "거래대금 100억↑": lambda r: r["amt"] >= 100,
        "거래량 20일평균 2배↑": lambda r: r["vol_x"] >= 2,
        "60일 신고가": lambda r: r["new_high60"],
        "20일선 위": lambda r: r["above_ma20"],
        "코스피 상승일": lambda r: r["kospi_up"],
        "윗꼬리 1% 이하": lambda r: r["upper_wick"] <= 1,
        "하락 -3% 이하(역발상)": lambda r: r["chg"] <= -3,
    }
    print(f"[조건 하나씩] {'조건':<20}{'n':>8}{'승률':>7}{'평균':>8}{'비용후':>8}{'전반':>7}{'후반':>7}")
    single_out = {}
    for label, f in single.items():
        st = stats([r["ret"] for r in rows if f(r)], base)
        single_out[label] = st
        if st["n"]:
            print(f"  {label:<20}{st['n']:>8,}{st['win']:>6}%{st['avg']:>+8.2f}{st['net']:>+8.2f}{st['h1']:>+7.2f}{st['h2']:>+7.2f}")

    # ── 조합 ──
    GRID = {
        "near": [-1, -2, -99],                 # 고가 대비 마감 하한 % (-99 = 조건 없음)
        "chg": [3, 8, 15],                     # 당일 등락 하한
        "amt": [50, 200],                      # 거래대금 하한 (억)
        "volx": [1, 2, 3],                     # 거래량 20일 평균 대비 배수
        "nh": [False, True],                   # 60일 신고가
        "mkt": [False, True],                  # 코스피 상승일
    }
    combos = []
    for near, chg, amt, volx, nh, mkt in itertools.product(*GRID.values()):
        sel = [r["ret"] for r in rows if r["near_high"] >= near and r["chg"] >= chg and r["amt"] >= amt and r["vol_x"] >= volx
               and (not nh or r["new_high60"]) and (not mkt or r["kospi_up"])]
        st = stats(sel, base)
        if st["n"] >= args.min_n:
            combos.append({"near": near, "chg": chg, "amt": amt, "volx": volx, "nh": nh, "mkt": mkt, **st})
    combos.sort(key=lambda r: r["avg"], reverse=True)
    print(f"\n[조합 {len(combos)}개, 표본 {args.min_n} 이상] 평균 수익 상위")
    print(f"  {'고가마감':<8}{'등락≥':<6}{'대금≥':<6}{'거래량×':<7}{'신고가':<6}{'코스피↑':<7}{'n':>7}{'승률':>7}{'평균':>8}{'비용후':>8}{'전반':>7}{'후반':>7}")
    for r in combos[:12]:
        print(f"  {('없음' if r['near'] < -50 else str(r['near']) + '%'):<8}{r['chg']:<6}{r['amt']:<6}{r['volx']:<7}{('예' if r['nh'] else '아니오'):<6}{('예' if r['mkt'] else '아니오'):<7}{r['n']:>7,}{r['win']:>6}%{r['avg']:>+8.2f}{r['net']:>+8.2f}{r['h1']:>+7.2f}{r['h2']:>+7.2f}")
    print("  [하위 3]")
    for r in combos[-3:]:
        print(f"  {('없음' if r['near'] < -50 else str(r['near']) + '%'):<8}{r['chg']:<6}{r['amt']:<6}{r['volx']:<7}{('예' if r['nh'] else '아니오'):<6}{('예' if r['mkt'] else '아니오'):<7}{r['n']:>7,}{r['win']:>6}%{r['avg']:>+8.2f}{r['net']:>+8.2f}")

    # ── 우리 스크리너 자리를 종가배팅으로 쓰면 ──
    print("\n[스크리너 자리 종가 매수 → 내일 " + ("시가" if args.exit == "open" else "종가") + " 매도]")
    nxt = {}
    for code, cs in candles.items():
        for i in range(len(cs) - 1):
            nxt[(code, cs[i].date)] = ((cs[i + 1].open if args.exit == "open" else cs[i + 1].close) / cs[i].close - 1) * 100 if cs[i].close else None
    for label, fn in (("고가놀이 엄선", find_setups), ("눌림목 엄선", find_pullbacks)):
        setups = []
        for code, name, lab in uni:
            setups += [s for s in fn(candles.get(code, []), code, name, lab) if s.strict]
        latest = {}
        for s in setups:
            k = (s.code, s.date)
            if k not in latest or s.spike_date > latest[k].spike_date:
                latest[k] = s
        rets = [nxt[(s.code, s.date)] for s in sorted(latest.values(), key=lambda s: s.date) if nxt.get((s.code, s.date)) is not None]
        st = stats(rets, base)
        if st["n"]:
            print(f"  {label:<10} n={st['n']:>5} 승률 {st['win']}% 평균 {st['avg']:+.2f}% 비용후 {st['net']:+.2f}% (전반 {st['h1']:+.2f} / 후반 {st['h2']:+.2f})")

    out = settings.data_dir / "sweep_close.json"
    out.write_text(json.dumps({"source": source, "universe": args.universe, "exit": args.exit, "baseline": round(base, 2), "samples": len(rows),
                               "single": single_out, "combos": combos}, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
