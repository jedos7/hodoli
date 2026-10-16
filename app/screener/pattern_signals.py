"""Configurable observation rules, not a reproduction of proprietary indicators."""
from dataclasses import dataclass, asdict
from datetime import datetime
from math import isfinite
from statistics import mean, pstdev


@dataclass(frozen=True)
class Rules:
    spike_ratio: float = 2.0
    spike_gain: float = 0.04
    lookback: int = 60
    base_days: int = 5
    base_width: float = 0.12
    support_tolerance: float = 0.03
    near112: float = 0.03
    settle_days: int = 3
    rebound_volume: float = 1.2


def analyze(candles, rules=Rules()):
    rows = sorted(candles, key=lambda b: b.date)
    if len({b.date for b in rows}) != len(rows):
        raise ValueError('중복 일봉 날짜')
    for b in rows:
        datetime.strptime(b.date, '%Y%m%d')
        values = (b.open, b.high, b.low, b.close, b.volume)
        if not all(isfinite(x) for x in values) or min(values[:4]) <= 0 or b.volume < 0 or not b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high:
            raise ValueError('유효하지 않은 OHLCV 일봉')
    n = len(rows)
    c = [b.close for b in rows]
    def ma(period, i=n-1):
        return mean(c[i-period+1:i+1]) if i >= period-1 else None
    def inverse(i):
        return i >= 447 and ma(112,i) < ma(224,i) < ma(448,i)
    out = dict(stage='데이터 부족', rank=0, signal256='데이터 부족', asof=rows[-1].date if rows else None,
               bars=n, conditions={}, averages={str(p):ma(p) for p in (2,5,6,20,60,112,224,448)},
               volume_ratio=None, distance112=None, bb_upper=None, spike_date=None, support=None,
               rules=asdict(rules), note='설정 기반 관찰 신호 · 수익/매매 보장 아님')
    if n < 22:
        return out
    avgvol = mean(b.volume for b in rows[-21:-1])
    vr = rows[-1].volume / avgvol if avgvol > 0 else None
    out['volume_ratio'] = vr
    out['bb_upper'] = ma(20) + 2*pstdev(c[-20:])
    short_up = ma(2) > ma(2,n-2) and ma(2,n-2) <= ma(2,n-3)
    crossed = ma(2) > max(ma(5),ma(6)) and ma(2,n-2) <= max(ma(5,n-2),ma(6,n-2))
    recovered = c[-1] >= ma(20) and c[-1] >= max(ma(5),ma(6))
    volume_ok = vr is not None and vr >= rules.rebound_volume
    if recovered and (short_up or crossed) and volume_ok and c[-1] > rows[-1].open:
        sig='반등확인'
    elif c[-1] < ma(20) or ma(2) < ma(5) < ma(6):
        sig='추세약화'
    elif c[-1] >= ma(20) and c[-1] < max(ma(5),ma(6)) and vr is not None and vr < 1:
        sig='눌림관찰'
    else:
        sig='관찰'
    out['signal256']=sig
    out['conditions'].update(turn2=short_up, cross256=crossed, recovered256=recovered, volume256=volume_ok)
    if n < 448:
        return out
    current_inverse=inverse(n-1)
    candidates=[]
    # The setup must start with inverse alignment, followed by a volume candle.
    for i in range(max(447,n-rules.lookback),n):
        basevol=mean(b.volume for b in rows[i-20:i])
        if inverse(i) and basevol>0 and rows[i].volume >= basevol*rules.spike_ratio and c[i] >= c[i-1]*(1+rules.spike_gain) and c[i]>rows[i].open:
            candidates.append(i)
    spike=candidates[-1] if candidates else None
    base=False
    support=None
    if spike is not None:
        out['spike_date']=rows[spike].date
        support=rows[spike].low*(1-rules.support_tolerance)
        after=rows[spike+1:]
        # Require a completed base before today's breakout, never count the spike.
        for end in range(spike+rules.base_days,n-1):
            window=rows[end-rules.base_days+1:end+1]
            if (max(b.high for b in window)/min(b.low for b in window)-1 <= rules.base_width
                    and all(b.low>=support for b in after)):
                base=True
                break
    recovery=c[-1]>=ma(20) and c[-1]>=ma(60)
    distance=(c[-1]/ma(112)-1)*100
    cross=c[-1]>ma(112) and c[-2]<=ma(112,n-2)
    settled=all(c[i]>ma(112,i) for i in range(n-rules.settle_days,n))
    near=abs(distance)<=rules.near112*100
    broken=spike is not None and any(b.low<support for b in rows[spike+1:])
    rank=0
    stage='조건 미충족'
    if current_inverse: stage,rank='역배열 관찰',1
    if spike is not None: stage,rank='거래량봉 관찰',2
    if base: stage,rank='횡보·지지 확인',3
    if base and recovery:
        stage,rank='20·60선 회복',4
        if near: stage,rank='112선 접근',5
        if cross: stage,rank='112선 돌파',6
        elif c[-1]>ma(112): stage,rank='112선 위 관찰',6
        if settled: stage,rank='112선 안착',7
    if broken: stage,rank='지지 이탈',0
    out.update(stage=stage,rank=rank,support=support,distance112=distance)
    out['conditions'].update(inverse=current_inverse, spike=spike is not None, base=base,
                             recovery=recovery, near112=near,cross112=cross,settled112=settled, support_broken=broken)
    return out
