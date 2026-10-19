"""여러 모듈이 같이 쓰는 자료형. (예전에는 app/kis 안에 있었다. 한국투자증권 연동을 걷어내면서 여기로 옮김)"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Candle:
    date: str  # YYYYMMDD
    open: int
    high: int
    low: int
    close: int
    volume: int
    amount: int  # 원


@dataclass(slots=True)
class Trade:
    """실시간 체결 한 건 (키움 0B)."""
    code: str
    time: str
    price: int
    change: int
    change_rate: float
    open: int
    high: int
    low: int
    volume: int
    acc_volume: int
    acc_amount: int  # 원
    cttr: float = 0.0        # 체결강도
    buy_total: int = 0       # 총매수수량
    sell_total: int = 0      # 총매도수량
    market: str = "KRX"      # KRX | NX (키움 NXT 야간·프리장 체결)
    signed_volume: int | None = None  # 매수/매도 방향이 명시된 체결량 (피드가 줄 때만)

    @property
    def acc_amount_eok(self) -> float:
        # 누적거래대금을 억으로. 화면·상태(app/state.py)는 억 단위를 쓴다.
        return self.acc_amount / 1e8
