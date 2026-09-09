"""종가배팅 — NXT 야간장(~20:00) 까지 보고 살 때의 성적. 키움 일봉의 NXT 종목코드(종목코드_NX)를 쓴다.

  py scripts/sweep_close_nxt.py            # 시장 전체 캐시 종목의 NXT 일봉을 받아(처음 2분) 비교
  py scripts/sweep_close_nxt.py --refresh

비교하는 것 (다음 날 KRX 시가 매도 기준)
  A. KRX 종가(15:30)에 매수
  B. NXT 종가(20:00)에 매수
  C. NXT 에서 KRX 종가보다 올라 마감한 날만 NXT 종가에 매수 (야간 흐름 확인 후 진입)
  D. NXT 에서 KRX 종가보다 내려 마감한 날만
전체 종목·일, 급등주(당일 +8% 이상), 고가놀이 엄선 자리 각각에 대해 낸다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.config import settings  # noqa: E402
from app.kis.rest import Candle  # noqa: E402
from app.kiwoom.auth import KiwoomAuth  # noqa: E402
from app.kiwoom.rest import KiwoomRest  # noqa: E402
from app.screener.hoga_play import find_setups  # noqa: E402
from app.screener.runner import drop_incomplete_today, get_candles  # noqa: E402

CACHE = settings.data_dir / "candles_kiwoom_market_nx.json"


async def fetch_nx(codes: list[str]) -> dict[str, list[Candle]]:
    rest = KiwoomRest(settings, KiwoomAuth(settings))
    out: dict[str, list[Candle]] = {}
    try:
        for i, code in enumerate(codes, 1):
            try:
                out[code] = await rest.daily_candles(code + "_NX", days=120)
            except Exception as e:
                print(f"  {code} 실패: {str(e)[:60]}")
            if i % 50 == 0:
                print(f"  {i}/{len(codes)}")
    finally:
        await rest.close()
    CACHE.write_text(json.dumps({"savedAt": datetime.now().isoformat(timespec="seconds"),
                                 "candles": {c: [[x.date, x.open, x.high, x.low, x.close, x.volume, x.amount] for x in cs] for c, cs in out.items()}}), "utf-8")
    return out


def st(rets: list[float], label: str) -> dict:
    if not rets:
        print(f"  {label:<44} n=0")
        return {"n": 0}
    h = len(rets) // 2 or 1
    d = {"n": len(rets), "win": round(sum(r > 0 for r in rets) / len(rets) * 100, 1), "avg": round(mean(rets), 2), "h1": round(mean(rets[:h]), 2), "h2": round(mean(rets[h:]), 2)}
    print(f"  {label:<44} n={d['n']:>6,} 승률 {d['win']:>5}% 평균 {d['avg']:+.2f}%  전반 {d['h1']:+.2f} 후반 {d['h2']:+.2f}")
    return d


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    settings.validate()
    source, uni, krx = await get_candles("kiwoom", "market", 250, 30.0, use_cache=True)
    drop_incomplete_today(krx)
    codes = [c for c, _, _ in uni]
    nx: dict[str, list[Candle]] = {}
    if CACHE.exists() and not args.refresh and json.loads(CACHE.read_text("utf-8"))["savedAt"][:10] == date.today().isoformat():
        nx = {c: [Candle(*r) for r in rows] for c, rows in json.loads(CACHE.read_text("utf-8"))["candles"].items()}
        print(f"NXT 일봉 캐시 {len(nx)}종목")
    else:
        print(f"NXT 일봉 수집 {len(codes)}종목 (2분쯤)")
        nx = await fetch_nx(codes)
    drop_incomplete_today(nx)

    # 종목·일 표
    rows = []
    for code, name, lab in uni:
        k, n = krx.get(code, []), {c.date: c for c in nx.get(code, [])}
        for i in range(1, len(k) - 1):
            c, nxt = k[i], k[i + 1]
            x = n.get(c.date)
            if c.close <= 0 or nxt.open <= 0 or not x or x.close <= 0 or k[i - 1].close <= 0:
                continue
            rows.append({"date": c.date, "code": code, "chg": (c.close / k[i - 1].close - 1) * 100,
                         "gap_krx": (nxt.open / c.close - 1) * 100, "gap_nx": (nxt.open / x.close - 1) * 100,
                         "nx_move": (x.close / c.close - 1) * 100, "nx_vol": x.volume})
    rows.sort(key=lambda r: r["date"])
    print(f"\n표본 {len(rows):,} 종목·일 (KRX 종가와 NXT 종가가 모두 있는 날)\n")
    result = {}
    for label, sel in (("전체", rows), ("급등주(+8%↑)", [r for r in rows if r["chg"] >= 8]), ("급등주(+15~29%)", [r for r in rows if 15 <= r["chg"] < 29])):
        print(f"[{label}]")
        result[label] = {
            "A KRX 종가 매수": st([r["gap_krx"] for r in sel], "A. KRX 종가(15:30) 매수 → 다음 날 시가"),
            "B NXT 종가 매수": st([r["gap_nx"] for r in sel], "B. NXT 종가(20:00) 매수 → 다음 날 시가"),
            "C NXT 상승 마감만": st([r["gap_nx"] for r in sel if r["nx_move"] > 0], "C. NXT 에서 올라 마감한 날만 NXT 종가 매수"),
            "C2 NXT +1%↑": st([r["gap_nx"] for r in sel if r["nx_move"] >= 1], "   NXT 에서 +1% 이상 올라 마감"),
            "D NXT 하락 마감만": st([r["gap_nx"] for r in sel if r["nx_move"] < 0], "D. NXT 에서 내려 마감한 날만"),
            "E NXT 거래 있음": st([r["gap_nx"] for r in sel if r["nx_vol"] > 0], "   (참고) NXT 거래 있는 날"),
        }
        print()

    # 고가놀이 엄선 자리
    setups = []
    for code, name, lab in uni:
        setups += [s for s in find_setups(krx.get(code, []), code, name, lab) if s.strict]
    latest = {}
    for s in setups:
        kk = (s.code, s.date)
        if kk not in latest or s.spike_date > latest[kk].spike_date:
            latest[kk] = s
    by = {(r["code"], r["date"]): r for r in rows}
    sel = [by[k] for k in latest if k in by]
    sel.sort(key=lambda r: r["date"])
    print("[고가놀이 엄선 자리]")
    result["고가놀이"] = {
        "A": st([r["gap_krx"] for r in sel], "A. KRX 종가 매수"),
        "B": st([r["gap_nx"] for r in sel], "B. NXT 종가 매수"),
        "C": st([r["gap_nx"] for r in sel if r["nx_move"] > 0], "C. NXT 상승 마감한 날만"),
        "D": st([r["gap_nx"] for r in sel if r["nx_move"] < 0], "D. NXT 하락 마감한 날만"),
    }
    (settings.data_dir / "sweep_close_nxt.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n→ {settings.data_dir / 'sweep_close_nxt.json'}")


if __name__ == "__main__":
    asyncio.run(main())
