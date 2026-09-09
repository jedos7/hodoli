"""고가놀이 스크리너 — "터뜨리고 버티며 저점을 높이는 자리".

판정은 완성된 일봉으로만 한다.
  1. 급등봉: 종가 기준 전일 대비 +spike_min% 이상, 거래대금 amount_min 이상.
  2. 이후 1~max_hold 일 횡보: 종가가 급등봉 시가 아래로 무너지지 않고, 급등봉 고가 대비 vs_high_min% 이내.
  3. 엄선 조건(각각 탈락 사유로 기록):
     - 횡보 폭 = (횡보 구간 최고가 − 최저가) / 급등봉 종가 < box_max% (기본 6)
     - 고가 밀착 = 종가가 급등봉 고가 대비 vs_high_strict% 이내 (기본 -3)
     - 저점 상승 = 횡보 마지막 날 저가 ≥ 첫 날 저가 (하루면 급등봉 저가와 비교)
     - MACD 선 > 0 (12, 26, 9)
     - 추세 짧음 = 자리 전 20일 중 20일선 위 < max_trend_days (기본 15)
  4. 매수 기준가 = 자리 날 고가(다음 날 넘길 때), 손절 기준 = 자리 날 저가.
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
    # ── 조건 조합 비교용 특징값 (scripts/sweep_hoga.py) ──
    low_up: bool = True                      # 저점 상승
    macd_pos: bool = True                    # MACD 0선 위
    vol_ratio: float = 1.0                   # 횡보 구간 평균 거래량 / 급등일 거래량
    trend_days: int = 0                      # 자리 전 20일 중 종가가 20일선 위였던 날 수
    high: int = 0                            # 자리 날 고가 (돌파 매수 기준가)
    low: int = 0                             # 자리 날 저가 (손절 기준가)
    fwd: dict[int, float] = field(default_factory=dict)      # 보유일 → 종가 매수 수익률%
    confirm: dict[int, float] = field(default_factory=dict)  # 다음 날 자리 날 고가 돌파 매수 → 보유일 → 수익률%
    gap: float | None = None                                 # 종가 매수 → 다음 날 시가 매도 수익률% (종가배팅)

    @property
    def strict(self) -> bool:
        return not self.reasons

    @property
    def close_bet(self) -> bool:
        """종가배팅 적합: 엄선 통과 + 횡보 1일째. 시장 전체 1년 검증에서 이 자리를 종가에 사서 다음 날 시가에 팔면
        갭 승률 60.8%, 평균 +1.31% (전반 +1.08 / 후반 +1.53). 횡보 2~3일째는 +0.41% 로 약했다."""
        return self.strict and self.hold_days == 1

    def as_dict(self) -> dict:
        return {"date": self.date, "code": self.code, "name": self.name, "theme": self.theme, "spikeDate": self.spike_date,
                "spikePct": round(self.spike_pct, 1), "holdDays": self.hold_days, "boxPct": round(self.box_pct, 1),
                "vsHighPct": round(self.vs_high_pct, 1), "close": self.close, "amountEok": round(self.amount_eok),
                "trendDays": self.trend_days, "entryPrice": self.high, "stopPrice": self.low,
                "riskPct": round((self.high / self.low - 1) * 100, 1) if self.low else None,
                "closeBet": self.close_bet, "gapPct": None if self.gap is None else round(self.gap, 2),
                "reasons": self.reasons, "strict": self.strict}


def find_setups(
    candles: list[Candle], code: str, name: str, theme: str, *,
    spike_min: float = 8.0, amount_min_eok: float = 50.0, max_hold: int = 3,
    box_max: float = 6.0, vs_high_min: float = -15.0, vs_high_strict: float = -3.0, max_trend_days: int = 10,
    hold_for_ret: int = 3,
) -> list[Setup]:
    """기본값은 2026-09-09 시장 전체 491종목 · 250일 조합 비교(scripts/sweep_hoga.py) 결과다.
    - 횡보 폭 6% 미만 · 급등봉 고가 대비 -3% 이내(고가에 붙어 있음) · MACD 0선 위 · 횡보 1~3일:
      3일 보유 승률 55.6%, 건당 +2.44%, 기준선 대비 +1.6%p. 처음 값(폭 10%, 고가 대비 -15%)은 +0.4%p 에 그쳤다.
      '고가에 딱 붙어 좁게 버티는' 자리만 의미가 있다.
    - 여기에 추세 짧음(자리 전 20일 중 20일선 위 10일 미만)을 더하면 승률 61.9%, 건당 +4.33%, 기준선 대비 +3.5%p (전반 +3.6 / 후반 +5.1).
    - 눌림목과 달리 '다음 날 고가 돌파 매수' 는 오히려 나빴다 (+1.77% vs 종가 매수 +2.35%). 고가놀이는 자리 날 종가(또는 다음 날 시가)에 산다.
    - 저점 상승·횡보 구간 거래량 조건은 성적 차이가 없었다. 저점 상승은 원본 화면의 개념이라 남겨 둔다."""
    if len(candles) < 30:
        return []
    closes = [float(c.close) for c in candles]
    macd_line, _, _ = macd(closes)
    ma20 = _sma(closes, 20)
    above = [1 if (m is not None and c > m) else 0 for c, m in zip(closes, ma20)]
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
            if vs_high < vs_high_strict:
                reasons.append(f"고가에서 멀어짐({vs_high:.1f}%)")
            if last.low < first_low:
                reasons.append("저점이 안 올라옴")
            if macd_line[j] <= 0:
                reasons.append("MACD 0선 아래")
            trend_days = sum(above[max(0, j - 20) : j])
            if trend_days >= max_trend_days:
                reasons.append(f"추세 길어짐({trend_days}/20일)")
            fwd = None
            if j + hold_for_ret < len(candles):
                fwd = (candles[j + hold_for_ret].close / last.close - 1) * 100
            fwds = {h: (candles[j + h].close / last.close - 1) * 100 for h in (1, 3, 5, 10) if j + h < len(candles)}
            confirm: dict[int, float] = {}
            if j + 1 < len(candles) and candles[j + 1].high > last.high:
                entry = max(candles[j + 1].open, last.high)
                confirm = {h: (candles[j + 1 + h].close / entry - 1) * 100 for h in (1, 3, 5, 10) if j + 1 + h < len(candles)}
            vol_ratio = (sum(w.volume for w in window) / len(window)) / spike.volume if spike.volume else 1.0
            gap = (candles[j + 1].open / last.close - 1) * 100 if j + 1 < len(candles) and candles[j + 1].open > 0 else None
            out.append(Setup(date=last.date, code=code, name=name, theme=theme, spike_date=spike.date, spike_pct=spike_pct,
                             hold_days=k, box_pct=box, vs_high_pct=vs_high, close=last.close, amount_eok=last.amount / 1e8,
                             reasons=reasons, fwd_ret=fwd, low_up=last.low >= first_low, macd_pos=macd_line[j] > 0,
                             vol_ratio=vol_ratio, trend_days=trend_days, high=last.high, low=last.low,
                             fwd=fwds, confirm=confirm, gap=gap))
    return out


def _sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
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
