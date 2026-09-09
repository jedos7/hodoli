"""수급 점수. 0~100, 50 이 중립.

세 가지 신호를 더한다. 가중치는 WEIGHTS 에서 조정한다.
  체결강도  : 실시간 체결의 CTTR (매수 체결량 / 매도 체결량 × 100). 100 중립, 200 이상이면 만점, 0 이면 최저.
  외인·기관 : 당일 외국인 + 기관 순매수 금액 / 당일 거래대금. ±10% 면 만점.
              장중에는 KIS '외인기관 추정가집계'(추정치), 없으면 전일 확정치를 쓴다. 출처는 Stock.investor_src 에 남는다.
  대금 가속 : 테마의 5분 유입 / 직전 5분 유입. 1.5배 이상이면 만점, 0.5배 이하면 최저.

방향(flow): 외인 + 기관 순매수 부호. 거래대금의 0.5% 미만이면 0(중립).
신호가 없으면(None) 그 항목은 0점 처리해서 50 근처에 머문다.
"""
from __future__ import annotations

from dataclasses import dataclass

WEIGHTS = {"cttr": 20.0, "investor": 20.0, "accel": 10.0}
INVESTOR_FULL = 0.10   # 순매수 / 거래대금 이 이 비율이면 만점
FLOW_DEADBAND = 0.005  # 이 비율 미만이면 방향 중립
ACCEL_FULL = 0.5       # 5분 유입이 직전 대비 +50% 면 만점


def clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


@dataclass(slots=True)
class SupplyInputs:
    cttr: float | None            # 체결강도
    net_investor_eok: float | None  # 외인 + 기관 순매수 (억)
    turnover_eok: float           # 당일 거래대금 (억)
    five: float = 0.0             # 테마 5분 유입 (억)
    prev_five: float = 0.0        # 직전 5분 유입 (억)


def score(inp: SupplyInputs) -> tuple[int, int]:
    """(점수 0~100, 방향 -1/0/1)"""
    s = 50.0
    if inp.cttr is not None and inp.cttr > 0:
        s += WEIGHTS["cttr"] * clamp((inp.cttr - 100.0) / 100.0)
    ratio = None
    if inp.net_investor_eok is not None and inp.turnover_eok > 0:
        ratio = inp.net_investor_eok / inp.turnover_eok
        s += WEIGHTS["investor"] * clamp(ratio / INVESTOR_FULL)
    if inp.prev_five > 0:
        s += WEIGHTS["accel"] * clamp((inp.five / inp.prev_five - 1.0) / ACCEL_FULL)
    flow = 0
    if ratio is not None:
        flow = 1 if ratio > FLOW_DEADBAND else -1 if ratio < -FLOW_DEADBAND else 0
    return int(round(clamp(s, 0.0, 100.0))), flow
