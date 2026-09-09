"""고가놀이 스크리너 — "터뜨리고 버티며 저점을 높이는 자리".

판정은 완성된 일봉으로만 한다.
  1. 급등봉: 종가 기준 전일 대비 +spike_min% 이상, 거래대금 amount_min 이상.
  2. 이후 1~max_hold 일 횡보: 종가가 급등봉 시가 아래로 무너지지 않고, 급등봉 고가 대비 vs_high_min% 이내.
  3. 엄선 조건(각각 탈락 사유로 기록):
     - 횡보 폭 = (횡보 구간 최고가 − 최저가) / 급등봉 종가 < box_max%
     - 저점 상승 = 횡보 마지막 날 저가 ≥ 첫 날 저가 (하루면 급등봉 저가와 비교)
     - MACD 선 > 0 (12, 26, 9)
자리는 (급등봉, 횡보 일수) 조합마다 하나씩 나온다. 같은 급등봉의 2일차 자리는 1일차 자리와 별개의 관측이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from app.kis.rest import Candle
from app.screener.indicators import macd


@dataclass
class Setup:
    date: str            # 자리 잡은 날 (횡보 마지막 봉)
    code: str
    name: str
    theme: str
    spike_date: str
    spike_pct: float
    hold_days: int
    box_pct: float
    vs_high_pct: float   # 급등봉 고가 대비 (음수)
    close: int           # 자리 잡은 날 종가
    amount_eok: float    # 자리 잡은 날 거래대금(억)
    reasons: list[str] = field(default_factory=list)  # 비어 있으면 엄선 통과
    fwd_ret: float | None = None  # 백테스트용: 3일 뒤 종가 수익률(%)

    @property
    def strict(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict:
        return {"date": self.date, "code": self.code, "name": self.name, "theme": self.theme, "spikeDate": self.spike_date,
                "spikePct": round(self.spike_pct, 1), "holdDays": self.hold_days, "boxPct": round(self.box_pct, 1),
                "vsHighPct": round(self.vs_high_pct, 1), "close": self.close, "amountEok": round(self.amount_eok),
                "reasons": self.reasons, "strict": self.strict}


def find_setups(
    candles: list[Candle], code: str, name: str, theme: str, *,
    spike_min: float = 8.0, amount_min_eok: float = 50.0, max_hold: int = 3,
    box_max: float = 10.0, vs_high_min: float = -15.0, hold_for_ret: int = 3,
) -> list[Setup]:
    if len(candles) < 30:
        return []
    closes = [float(c.close) for c in candles]
    macd_line, _, _ = macd(closes)
    out: list[Setup] = []
    for i in range(1, len(candles)):
        spike = candles[i]
        prev = candles[i - 1]
        if prev.close <= 0:
            continue
        spike_pct = (spike.close / prev.close - 1) * 100
        if spike_pct < spike_min or spike.amount / 1e8 < amount_min_eok:
            continue
        for k in range(1, max_hold + 1):
            j = i + k
            if j >= len(candles):
                break
            window = candles[i + 1 : j + 1]
            last = window[-1]
            if any(w.close < spike.open for w in window):  # 급등을 되돌림 → 자리 아님
                break
            vs_high = (last.close / spike.high - 1) * 100
            if vs_high < vs_high_min:
                break
            box = (max(w.high for w in window) - min(w.low for w in window)) / spike.close * 100
            first_low = window[0].low if k > 1 else spike.low
            reasons: list[str] = []
            if box >= box_max:
                reasons.append(f"횡보 폭 넓음({box:.0f}%)")
            if last.low < first_low:
                reasons.append("저점이 안 올라옴")
            if macd_line[j] <= 0:
                reasons.append("MACD 0선 아래")
            fwd = None
            if j + hold_for_ret < len(candles):
                fwd = (candles[j + hold_for_ret].close / last.close - 1) * 100
            out.append(Setup(date=last.date, code=code, name=name, theme=theme, spike_date=spike.date, spike_pct=spike_pct,
                             hold_days=k, box_pct=box, vs_high_pct=vs_high, close=last.close, amount_eok=last.amount / 1e8,
                             reasons=reasons, fwd_ret=fwd))
    return out


def backtest(setups: list, baseline_ret: float = 0.0, rets: list[float] | None = None) -> dict:
    """3일 보유 성과. baseline_ret 은 같은 기간 모든 봉의 3일 수익률 평균(기준선).
    rets 를 주면 (날짜순 정렬된) 수익률 목록으로 바로 계산한다 (돌파 매수 등 다른 진입 방식용)."""
    if rets is None:
        xs = [s for s in setups if s.fwd_ret is not None]
        xs.sort(key=lambda s: s.date)
        rets = [s.fwd_ret for s in xs]
    if not rets:
        return {"n": 0, "win": 0.0, "avg": 0.0, "vsBase": 0.0, "h1": 0.0, "h2": 0.0}
    half = len(rets) // 2 or 1
    return {
        "n": len(rets),
        "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg": round(mean(rets), 2),
        "vsBase": round(mean(rets) - baseline_ret, 2),
        "h1": round(mean(rets[:half]), 2),
        "h2": round(mean(rets[half:]), 2) if rets[half:] else 0.0,
    }


def baseline(candles_by_code: dict[str, list[Candle]], hold: int = 3) -> float:
    rets = []
    for cs in candles_by_code.values():
        for i in range(len(cs) - hold):
            if cs[i].close > 0:
                rets.append((cs[i + hold].close / cs[i].close - 1) * 100)
    return mean(rets) if rets else 0.0
