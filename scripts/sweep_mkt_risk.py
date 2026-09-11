"""시장 갭다운 위험 표시 검증 — 19:50 에 알 수 있는 해외 지표의 15:30→19:45 움직임이 다음 날 국내 시장 갭을 예측하는가.

  py scripts/sweep_mkt_risk.py

재료: 키움 KRX·NXT 일봉 캐시(data/candles_kiwoom_market*.json, sweep_close_nxt 가 만든 것) + 야후 1시간봉 1년(나스닥·S&P·유가·환율·항셍·유로스톡스·니케이 선물).
'다음 날 시장 갭' 은 지수 대신 전 종목 다음 날 시가갭의 중앙값을 쓴다(종가배팅 종목이 겪는 갭에 더 가깝다).
결과는 app/market_risk.py 머리말에 요약. data/sweep_mkt_risk.json 에 날짜별 신호·시장갭을 남긴다.
"""
import asyncio, json, sys
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import httpx
from app.config import settings
from app.kis.rest import Candle
from app.screener.runner import drop_incomplete_today, get_candles
from app.collectors.overnight import series, value_at, YAHOO
KST = timezone(timedelta(hours=9))
SYMS = {"NQ": "NQ=F", "ES": "ES=F", "CL": "CL=F", "KRW": "KRW=X", "DX": "DX=F", "HSI": "^HSI", "STOXX": "^STOXX50E", "NKF": "NKD=F"}

def corr(xs, ys):
    mx, my = mean(xs), mean(ys)
    sx = sum((x-mx)**2 for x in xs)**.5; sy = sum((y-my)**2 for y in ys)**.5
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys))/(sx*sy) if sx and sy else 0.0

async def main():
    _, uni, krx = await get_candles("kiwoom", "market", 250, 30.0, use_cache=True); drop_incomplete_today(krx)
    cache = json.loads((settings.data_dir/"candles_kiwoom_market_nx.json").read_text("utf-8"))
    nx = {c: [Candle(*r) for r in rows] for c, rows in cache["candles"].items()}; drop_incomplete_today(nx)
    rows = []
    for code, name, lab in uni:
        k, n = krx.get(code, []), {c.date: c for c in nx.get(code, [])}
        for i in range(1, len(k)-1):
            c, nxt = k[i], k[i+1]; x = n.get(c.date)
            if c.close <= 0 or nxt.open <= 0 or not x or x.close <= 0 or k[i-1].close <= 0: continue
            rows.append({"date": c.date, "chg": (c.close/k[i-1].close-1)*100, "gap_krx": (nxt.open/c.close-1)*100,
                         "gap_nx": (nxt.open/x.close-1)*100, "nx_move": (x.close/c.close-1)*100, "near_high": (c.close/c.high-1)*100 if c.high else 0})
    byday = {}
    for r in rows: byday.setdefault(r["date"], []).append(r["gap_krx"])
    mkt = {d: median(v) for d, v in byday.items()}          # 그날 전 종목 다음날 시가갭 중앙값
    picks = {}
    for r in rows:
        if r["chg"] >= 8 and r["nx_move"] <= -2: picks.setdefault(r["date"], []).append(r["gap_nx"])
    days = sorted(byday)
    print(f"거래일 {len(days)} ({days[0]}~{days[-1]}), 급등주 -2%이하 눌림 표본 {sum(len(v) for v in picks.values())}")

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as cl:
        bars = {}
        for k, sym in SYMS.items():
            try:
                r = await cl.get(YAHOO.format(symbol=sym), params={"range": "1y", "interval": "1h", "includePrePost": "true"})
                bars[k] = series(r.json()); print(f"  {k:<5} {sym:<10} 1h봉 {len(bars[k])}개  {datetime.fromtimestamp(bars[k][0][0],KST):%m-%d}~")
            except Exception as e: print(k, "실패", e)
        # 15분봉(60일)로 정확도 확인용
        r = await cl.get(YAHOO.format(symbol="NQ=F"), params={"range": "60d", "interval": "15m", "includePrePost": "true"}); nq15 = series(r.json())

    sig = {}
    for d in days:
        dt = datetime.strptime(d, "%Y%m%d").replace(tzinfo=KST)
        t0, t1 = dt.replace(hour=15, minute=30), dt.replace(hour=19, minute=45)
        s = {}
        for k, pts in bars.items():
            a, b = value_at(pts, t0), value_at(pts, t1)
            if a and b and a[1] > t0.timestamp()-3*3600*24 and b[1] > a[1]: s[k] = (b[0]/a[0]-1)*100
        a, b = value_at(nq15, t0), value_at(nq15, t1)
        if a and b and b[1] > a[1]: s["NQ15"] = (b[0]/a[0]-1)*100
        sig[d] = s
    print()
    print("[상관: 15:30→19:45 지표 움직임 vs 다음 날 전 종목 시가갭 중앙값]")
    for k in list(SYMS) + ["NQ15"]:
        ds = [d for d in days if k in sig[d]]
        if len(ds) < 20: print(f"  {k:<5} 표본 부족 {len(ds)}"); continue
        xs = [sig[d][k] for d in ds]; ys = [mkt[d] for d in ds]
        print(f"  {k:<5} n={len(ds):>3} 상관 {corr(xs,ys):+.2f}   지표 표준편차 {(mean([x*x for x in xs])-mean(xs)**2)**.5:.2f}%")
    # 갭다운 날의 지표 분포
    print("\n[다음 날 시장 -1% 이하 갭다운이었던 날, 19:45 시점 나스닥 선물은?]")
    for d in days:
        if mkt[d] <= -1 and "NQ" in sig[d]:
            print(f"  {d}  시장갭 {mkt[d]:+.2f}%  NQ {sig[d]['NQ']:+.2f}%  ES {sig[d].get('ES',0):+.2f}%  CL {sig[d].get('CL',0):+.2f}%  KRW {sig[d].get('KRW',0):+.2f}%  HSI {sig[d].get('HSI',0):+.2f}%")
    # 임계값 규칙 평가
    print("\n[규칙 평가: 표시된 날 vs 아닌 날 — 급등주 -2%이하 눌림 종가배팅 성적, 시장 갭다운 적중]")
    def evalrule(name, fn):
        flag = [d for d in days if d in sig[d] or True]
        f = [d for d in days if fn(sig[d]) is True]; nf = [d for d in days if fn(sig[d]) is False]
        if not f: print(f"  {name:<34} 표시 0일"); return
        def pk(ds):
            v = [g for d in ds for g in picks.get(d, [])]
            return f"n={len(v):>3} 승률 {sum(g>0 for g in v)/len(v)*100:5.1f}% 평균 {mean(v):+.2f}%" if v else "n=  0"
        gd = sum(mkt[d] <= -1 for d in f); gdall = sum(mkt[d] <= -1 for d in f+nf)
        print(f"  {name:<34} 표시 {len(f):>3}일(그중 실제 갭다운 {gd}, 전체 갭다운 {gdall}) 시장갭평균 표시 {mean(mkt[d] for d in f):+.2f}% / 미표시 {mean(mkt[d] for d in nf):+.2f}%")
        print(f"      종가배팅 표시일 {pk(f)}  |  미표시일 {pk(nf)}")
    evalrule("NQ ≤ -0.3%", lambda s: (s["NQ"] <= -0.3) if "NQ" in s else None)
    evalrule("NQ ≤ -0.5%", lambda s: (s["NQ"] <= -0.5) if "NQ" in s else None)
    evalrule("NQ ≤ -0.8%", lambda s: (s["NQ"] <= -0.8) if "NQ" in s else None)
    evalrule("ES ≤ -0.3%", lambda s: (s["ES"] <= -0.3) if "ES" in s else None)
    evalrule("CL ≥ +2%", lambda s: (s["CL"] >= 2) if "CL" in s else None)
    evalrule("KRW ≥ +0.3%", lambda s: (s["KRW"] >= 0.3) if "KRW" in s else None)
    evalrule("HSI ≤ -1%", lambda s: (s["HSI"] <= -1) if "HSI" in s else None)
    evalrule("NQ≤-0.3 or CL≥+2 or KRW≥+0.3", lambda s: (s["NQ"] <= -0.3 or s.get("CL",0) >= 2 or s.get("KRW",0) >= 0.3) if "NQ" in s else None)
    evalrule("NQ≤-0.5 or KRW≥+0.3", lambda s: (s["NQ"] <= -0.5 or s.get("KRW",0) >= 0.3) if "NQ" in s else None)
    json.dump({d: {"sig": sig[d], "mkt": mkt[d]} for d in days}, open(settings.data_dir/"sweep_mkt_risk.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
asyncio.run(main())
