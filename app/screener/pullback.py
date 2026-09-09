"""눌림목 스크리너 — "크게 오른 뒤 거래량 줄며 얕게 쉬는 자리".

판정은 완성된 일봉으로만 한다.
  1. 급등봉: 종가 기준 +spike_min% 이상, 거래대금 amount_min 이상.
  2. 급등 뒤 고점(peak): 급등봉부터 자리 전날까지의 최고가. 자리 날은 급등 2일 뒤부터 lookahead 일 안 (급등 첫날 눌림은 뺀다).
  3. 자리 후보: 종가가 고점 대비 -depth_any_min% 이상 내려온 날 (얕게라도 눌린 날). 급등봉 시가 아래로 무너지면 끝.
  4. 엄선 조건 (각각 탈락 사유):
     - 눌림 폭    : 고점 대비 -depth_min ~ -depth_max % 사이 (기본 -3 ~ -10, 조합 비교 결과)
     - 거래량 감소: 눌리는 동안 평균 거래량 ≤ 급등일 거래량 × vol_ratio_max (기본 0.3)
     - 눌림 일수  : 고점 이후 max_pull_days 일 이내 (기본 3)
     - 20일선 위  : 종가 ≥ 20일 이동평균 × (1 - ma_tol)
     - 저점 유지  : 자리 날 저가 ≥ 급등봉 저가
자리는 (급등봉, 자리 날) 조합마다 하나. 3일 뒤 종가 수익률로 백테스트한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.kis.rest import Candle
from app.screener.hoga_play import backtest  # 같은 통계 함수 재사용


@dataclass
class PullbackSetup:
    date: str
    code: str
    name: str
    theme: str
    spike_date: str
    spike_pct: float
    peak_date: str
    peak_high: int
    depth_pct: float      # 고점 대비 종가 (음수)
    pull_days: int        # 고점 이후 눌린 일수
    vol_ratio: float      # 눌림 구간 평균 거래량 / 급등일 거래량
    ma20_pct: float       # 종가 vs 20일선 (%)
    close: int
    amount_eok: float
    reasons: list[str] = field(default_factory=list)
    fwd_ret: float | None = None
    low_hold: bool = True                       # 자리 날 저가 ≥ 급등봉 저가
    fwd: dict[int, float] = field(default_factory=dict)  # 보유일 → 수익률% (조건 조합 비교용)

    @property
    def strict(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict:
        return {"date": self.date, "code": self.code, "name": self.name, "theme": self.theme, "spikeDate": self.spike_date,
                "spikePct": round(self.spike_pct, 1), "peakDate": self.peak_date, "peakHigh": self.peak_high,
                "depthPct": round(self.depth_pct, 1), "pullDays": self.pull_days, "volRatio": round(self.vol_ratio, 2),
                "ma20Pct": round(self.ma20_pct, 1), "close": self.close, "amountEok": round(self.amount_eok),
                "reasons": self.reasons, "strict": self.strict}


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def find_pullbacks(
    candles: list[Candle], code: str, name: str, theme: str, *,
    spike_min: float = 8.0, amount_min_eok: float = 50.0, lookahead: int = 10,
    depth_any_min: float = 3.0, depth_min: float = 3.0, depth_max: float = 10.0,
    vol_ratio_max: float = 0.3, max_pull_days: int = 3, ma_tol: float = 0.0, hold_for_ret: int = 3,
) -> list[PullbackSetup]:
    """기본값은 2026-09-09 시장 전체 491종목 · 250일 조합 비교(scripts/sweep_pullback.py) 결과다:
    얕게(고점 대비 -3~-10%) · 짧게(눌림 1~3일) · 거래량이 확 줄어든(급등일의 0.3배 이하) 자리가
    3일 보유 승률 48%, 건당 +1.9%, 기준선 대비 +1.0%p 로 전반·후반 모두 가장 좋았다. 깊은 눌림(-8~-20%)은 기준선을 밑돌았다."""
    if len(candles) < 30:
        return []
    closes = [float(c.close) for c in candles]
    ma20 = sma(closes, 20)
    out: list[PullbackSetup] = []
    for i in range(1, len(candles)):
        spike, prev = candles[i], candles[i - 1]
        if prev.close <= 0:
            continue
        spike_pct = (spike.close / prev.close - 1) * 100
        if spike_pct < spike_min or spike.amount / 1e8 < amount_min_eok:
            continue
        for j in range(i + 2, min(i + lookahead, len(candles) - 1) + 1):
            day = candles[j]
            if day.close < spike.open:          # 급등을 되돌림 → 눌림이 아니라 이탈
                break
            before = candles[i:j]               # 급등봉 ~ 자리 전날
            peak_idx = max(range(i, j), key=lambda k: candles[k].high)
            peak = candles[peak_idx]
            depth = (day.close / peak.high - 1) * 100
            if depth > -depth_any_min:          # 아직 안 눌림 (고가놀이 영역)
                continue
            pull = candles[peak_idx + 1 : j + 1]
            pull_days = len(pull)
            vol_ratio = (sum(c.volume for c in pull) / pull_days) / spike.volume if spike.volume and pull_days else 1.0
            m = ma20[j]
            ma20_pct = (day.close / m - 1) * 100 if m else 0.0
            reasons: list[str] = []
            if not (-depth_max <= depth <= -depth_min):
                reasons.append(f"눌림 폭 {'과다' if depth < -depth_max else '부족'}({depth:.0f}%)")
            if vol_ratio > vol_ratio_max:
                reasons.append(f"거래량 안 줄음({vol_ratio:.1f}배)")
            if pull_days > max_pull_days:
                reasons.append(f"눌림 길어짐({pull_days}일)")
            if m and day.close < m * (1 - ma_tol):
                reasons.append("20일선 아래")
            if day.low < spike.low:
                reasons.append("급등봉 저점 이탈")
            fwd = (candles[j + hold_for_ret].close / day.close - 1) * 100 if j + hold_for_ret < len(candles) else None
            fwds = {h: (candles[j + h].close / day.close - 1) * 100 for h in (1, 3, 5, 10) if j + h < len(candles)}
            out.append(PullbackSetup(day.date, code, name, theme, spike.date, spike_pct, peak.date, peak.high, depth, pull_days,
                                     vol_ratio, ma20_pct, day.close, day.amount / 1e8, reasons, fwd, day.low >= spike.low, fwds))
    return out


__all__ = ["PullbackSetup", "find_pullbacks", "backtest", "sma"]
