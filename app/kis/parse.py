"""KIS 실시간 웹소켓 메시지 파서.

체결 데이터는 JSON 이 아니라 '|' 와 '^' 로 구분된 한 줄 텍스트로 온다:
    0|H0STCNT0|001|005930^093012^71500^2^300^0.42^...
    ^ 암호화여부(0 평문) ^ TR ID ^ 레코드 수 ^ 필드들(^ 구분, 레코드 수만큼 반복)
제어 메시지(구독 응답, PINGPONG)는 JSON 으로 온다.
"""
from __future__ import annotations

from dataclasses import dataclass

TR_TRADE = "H0STCNT0"  # 국내주식 실시간 체결가
TR_ORDERBOOK = "H0STASP0"  # 국내주식 실시간 호가

# H0STCNT0 필드 순서 (KIS 문서 기준, 앞부분만 사용)
_TRADE_FIELDS = [
    "code",          # 0  MKSC_SHRN_ISCD 종목코드
    "time",          # 1  STCK_CNTG_HOUR 체결시간 HHMMSS
    "price",         # 2  STCK_PRPR 현재가
    "sign",          # 3  PRDY_VRSS_SIGN 전일대비부호 (1 상한 2 상승 3 보합 4 하한 5 하락)
    "change",        # 4  PRDY_VRSS 전일대비
    "change_rate",   # 5  PRDY_CTRT 전일대비율
    "vwap",          # 6  WGHN_AVRG_STCK_PRC 가중평균가
    "open",          # 7  STCK_OPRC
    "high",          # 8  STCK_HGPR
    "low",           # 9  STCK_LWPR
    "ask1",          # 10 ASKP1 매도호가1
    "bid1",          # 11 BIDP1 매수호가1
    "volume",        # 12 CNTG_VOL 체결거래량
    "acc_volume",    # 13 ACML_VOL 누적거래량
    "acc_amount",    # 14 ACML_TR_PBMN 누적거래대금(원)
    "sell_cnt",      # 15 SELN_CNTG_CSNU 매도체결건수
    "buy_cnt",       # 16 SHNU_CNTG_CSNU 매수체결건수
    "net_cnt",       # 17 NTBY_CNTG_CSNU 순매수체결건수
    "cttr",          # 18 CTTR 체결강도 (매수체결량/매도체결량×100)
    "sell_total",    # 19 SELN_CNTG_SMTN 총매도수량
    "buy_total",     # 20 SHNU_CNTG_SMTN 총매수수량
    "ccld_dvsn",     # 21 CCLD_DVSN 체결구분 (1 매수, 3 매도, 5 보합)
    "buy_rate",      # 22 SHNU_RATE 매수비율
]


@dataclass(slots=True)
class Trade:
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

    @property
    def acc_amount_eok(self) -> float:
        return self.acc_amount / 1e8


def _f(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return 0.0


def _i(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        return 0


def parse_trades(raw: str) -> list[Trade]:
    """'0|H0STCNT0|n|fields' 한 줄을 Trade 목록으로. 다른 TR 이나 형식이면 빈 목록."""
    parts = raw.split("|")
    if len(parts) < 4 or parts[0] != "0" or parts[1] != TR_TRADE:
        return []
    count = int(parts[2])
    fields = parts[3].split("^")
    width = len(fields) // count if count else 0
    if width < len(_TRADE_FIELDS):
        return []
    out: list[Trade] = []
    for i in range(count):
        f = fields[i * width : (i + 1) * width]
        rec = dict(zip(_TRADE_FIELDS, f))
        sign = -1 if rec["sign"] in ("4", "5") else 1
        out.append(
            Trade(
                code=rec["code"],
                time=rec["time"],
                price=int(rec["price"]),
                change=sign * abs(int(rec["change"])),
                change_rate=float(rec["change_rate"]),
                open=int(rec["open"]),
                high=int(rec["high"]),
                low=int(rec["low"]),
                volume=int(rec["volume"]),
                acc_volume=int(rec["acc_volume"]),
                acc_amount=int(rec["acc_amount"]),
                cttr=_f(rec["cttr"]),
                buy_total=_i(rec["buy_total"]),
                sell_total=_i(rec["sell_total"]),
            )
        )
    return out
