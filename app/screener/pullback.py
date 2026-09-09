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
     - 추세 짧음  : 자리 전 20일 중 20일선 위였던 날 < max_trend_days (기본 15). 오래 오른 뒤의 눌림은 성적이 나빴다.
  5. 매수는 자리 날 종가가 아니라 '다음 날 자리 날 고가를 넘길 때'(entry_price). 손절 기준은 자리 날 저가(stop_price).
자리는 (급등봉, 자리 날) 조합마다 하나. 3일 뒤 종가 수익률로 백테스트한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.kis.rest import Candle
from app.screener.hoga_play import backtest  # 같은 통계 함수 재사용
from app.screener.indicators import adx


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
    high: int = 0                               # 자리 날 고가 (매수 기준가)
    low: int = 0                                # 자리 날 저가 (손절 기준가)
    fwd: dict[int, float] = field(default_factory=dict)  # 보유일 → 수익률% (조건 조합 비교용)
    # ── 조건 조합 비교용 추가 특징값 ──
    adx: float | None = None                    # 자리 날 ADX(14): 추세 강도
    trend_days: int = 0                         # 자리 전 20일 중 종가가 20일선 위였던 날 수
    pull_no: int = 1                            # 급등 뒤 몇 번째 눌림인가 (1 = 첫 눌림)
    prev_depth: float | None = None             # 직전 눌림의 최대 폭(%) — 없으면 None
    confirm: dict[int, float] = field(default_factory=dict)  # 다음 날 전일 고가 돌파 시 진입(체결가 = max(시가, 전일 고가)) 보유일 → 수익률%. 돌파 없으면 비어 있음

    @property
    def strict(self) -> bool:
        return not self.reasons

    @property
    def entry_price(self) -> int:
        """매수 기준가: 자리 날 고가. 다음 날 이 가격을 넘길 때 산다 (조합 비교에서 종가 매수보다 승률·수익 모두 높았다)."""
        return self.high

    @property
    def stop_price(self) -> int:
        """손절 기준가: 자리 날 저가. 여기를 깨면 눌림이 아니라 이탈로 본다."""
        return self.low

    def as_dict(self) -> dict:
        return {"date": self.date, "code": self.code, "name": self.name, "theme": self.theme, "spikeDate": self.spike_date,
                "spikePct": round(self.spike_pct, 1), "peakDate": self.peak_date, "peakHigh": self.peak_high,
                "depthPct": round(self.depth_pct, 1), "pullDays": self.pull_days, "volRatio": round(self.vol_ratio, 2),
                "ma20Pct": round(self.ma20_pct, 1), "close": self.close, "amountEok": round(self.amount_eok),
                "adx": None if self.adx is None else round(self.adx, 1), "pullNo": self.pull_no, "trendDays": self.trend_days,
                "entryPrice": self.entry_price, "stopPrice": self.stop_price,
                "riskPct": round((self.entry_price / self.stop_price - 1) * 100, 1) if self.stop_price else None,
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
    max_trend_days: int = 15,
) -> list[PullbackSetup]:
    """기본값은 2026-09-09 시장 전체 491종목 · 250일 조합 비교(scripts/sweep_pullback.py) 결과다.
    - 얕게(고점 대비 -3~-10%) · 짧게(눌림 1~3일) · 거래량이 확 줄어든(급등일의 0.3배 이하) 자리: 승률 48%, 건당 +1.6% (3일, 종가 매수)
    - 여기에 '추세가 길지 않음'(자리 전 20일 중 20일선 위 < 15일)을 더하고 '다음 날 전일 고가 돌파 때 매수' 하면
      승률 54%, 건당 +3.4%, 기준선(+0.8%) 대비 +2.5%p, 전반 +3.6 / 후반 +3.2 로 안정적이었다.
    - 반대로 ADX 25 이상·오래된 추세는 성적을 깎았고, 깊은 눌림(-8~-20%)은 기준선을 밑돌았다."""
    if len(candles) < 30:
        return []
    closes = [float(c.close) for c in candles]
    ma20 = sma(closes, 20)
    adx14 = adx([float(c.high) for c in candles], [float(c.low) for c in candles], closes, 14)
    above = [1 if (m is not None and c > m) else 0 for c, m in zip(closes, ma20)]
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
            # 몇 번째 눌림인가 · 직전 눌림 폭: 급등 뒤 하루씩 훑으며 '고점 갱신 → 3% 이상 하락' 을 한 번의 눌림으로 센다
            pull_no, prev_depth, run_peak, in_dip, dip_low = 1, None, spike.high, False, None
            for k in range(i + 1, j + 1):
                c = candles[k]
                if c.high > run_peak and in_dip:            # 눌림 끝나고 새 고점 → 다음 눌림 시작
                    pull_no += 1
                    prev_depth = (dip_low / run_peak - 1) * 100 if dip_low else None
                    in_dip, dip_low = False, None
                run_peak = max(run_peak, c.high)
                if c.close <= run_peak * (1 - depth_any_min / 100):
                    in_dip = True
                    dip_low = c.close if dip_low is None else min(dip_low, c.close)
            trend_days = sum(above[max(0, j - 20) : j])
            if trend_days >= max_trend_days:
                reasons.append(f"추세 길어짐({trend_days}/20일)")
            confirm: dict[int, float] = {}
            if j + 1 < len(candles) and candles[j + 1].high > day.high:   # 다음 날 전일 고가 돌파 → 그 가격(또는 갭이면 시가)에 진입
                entry = max(candles[j + 1].open, day.high)
                confirm = {h: (candles[j + 1 + h].close / entry - 1) * 100 for h in (1, 3, 5, 10) if j + 1 + h < len(candles)}
            out.append(PullbackSetup(day.date, code, name, theme, spike.date, spike_pct, peak.date, peak.high, depth, pull_days,
                                     vol_ratio, ma20_pct, day.close, day.amount / 1e8, reasons, fwd, day.low >= spike.low, day.high, day.low,
                                     fwds, adx14[j], trend_days, pull_no, prev_depth, confirm))
    return out


__all__ = ["PullbackSetup", "find_pullbacks", "backtest", "sma"]
