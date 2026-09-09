"""키움 REST/WebSocket 응답 파서.

키움은 숫자를 부호 붙은 문자열로 준다 ("+71500", "-300", "--1200" 같은 형태도 있어 부호는 하나로 정리).
실시간 주식체결(type 0B) 의 values 는 FID 번호가 키다:
  10 현재가  11 전일대비  12 등락율  13 누적거래량  14 누적거래대금  15 거래량(+매수/-매도 체결)
  16 시가  17 고가  18 저가  20 체결시간(HHMMSS)  27 최우선매도호가  28 최우선매수호가  228 체결강도
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.kis.parse import Trade  # 동일한 Trade 구조를 재사용해 상태 계산 코드를 공유한다
from app.kis.rest import Candle

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def num(v, default: float = 0.0) -> float:
    """'+71,500' → 71500.0, '-0.42' → -0.42, '' → default. 부호가 겹쳐도 하나로 본다."""
    if v is None:
        return default
    s = str(v).replace(",", "").strip()
    neg = s.startswith("-")
    s = s.lstrip("+-")
    m = _NUM.match(s)
    if not m:
        return default
    x = float(m.group())
    return -x if neg else x


def inum(v, default: int = 0) -> int:
    return int(num(v, default))


def parse_real(msg: dict, amount_unit: int = 1) -> list[Trade]:
    """{"trnm":"REAL","data":[{"type":"0B","item":"005930","values":{...}}]} → Trade 목록"""
    if msg.get("trnm") != "REAL":
        return []
    out = []
    for d in msg.get("data", []):
        if d.get("type") != "0B":
            continue
        v = d.get("values", {})
        price = abs(inum(v.get("10")))
        vol = inum(v.get("15"))
        item = str(d.get("item", ""))
        market = "NX" if item.endswith("_NX") else "KRX"
        out.append(Trade(
            code=item.replace("A", "")[:6],
            time=str(v.get("20", "")),
            price=price,
            change=inum(v.get("11")),
            change_rate=num(v.get("12")),
            open=abs(inum(v.get("16"))),
            high=abs(inum(v.get("17"))),
            low=abs(inum(v.get("18"))),
            volume=abs(vol),
            acc_volume=inum(v.get("13")),
            acc_amount=inum(v.get("14")) * amount_unit,
            cttr=num(v.get("228")),
            buy_total=0,
            sell_total=0,
            market=market,
        ))
    return out


@dataclass(slots=True)
class BasicInfo:
    code: str
    name: str
    price: int
    change: int
    change_rate: float
    volume: int
    amount: int   # 원 (없으면 0)
    open: int
    high: int
    low: int

    @property
    def prev_close(self) -> int:
        return self.price - self.change


def parse_basic(body: dict, amount_unit: int = 1) -> BasicInfo:
    """ka10001 주식기본정보 응답"""
    return BasicInfo(
        code=str(body.get("stk_cd", "")).replace("A", "")[:6],
        name=str(body.get("stk_nm", "")),
        price=abs(inum(body.get("cur_prc"))),
        change=inum(body.get("pred_pre")),
        change_rate=num(body.get("flu_rt")),
        volume=abs(inum(body.get("trde_qty"))),
        amount=abs(inum(body.get("trde_prica"))) * amount_unit,
        open=abs(inum(body.get("open_pric"))),
        high=abs(inum(body.get("high_pric"))),
        low=abs(inum(body.get("low_pric"))),
    )


def parse_daily(body: dict, amount_unit: int = 1) -> list[Candle]:
    """ka10081 주식일봉차트 응답. 최신순으로 오므로 오래된 순으로 뒤집는다."""
    rows = body.get("stk_dt_pole_chart_qry") or body.get("stk_dt_pole_chart") or []
    out = []
    for r in rows:
        d = str(r.get("dt", ""))
        c = abs(inum(r.get("cur_prc")))
        if len(d) != 8 or c <= 0:
            continue
        out.append(Candle(d, abs(inum(r.get("open_pric"))), abs(inum(r.get("high_pric"))), abs(inum(r.get("low_pric"))), c,
                          abs(inum(r.get("trde_qty"))), abs(inum(r.get("trde_prica"))) * amount_unit))
    out.sort(key=lambda x: x.date)
    return out


def parse_investor(body: dict, amount_unit: int = 1) -> dict | None:
    """ka10059 종목별투자자기관별 응답의 첫 행(가장 최근 일자, 장중이면 당일 잠정치).
    금액 항목(amt_qty_tp=1)은 백만원 단위라 amount_unit 을 곱해 원으로 돌려준다.
    → {date, price, prev_close, acc_amount(원), frgn(원), orgn(원), prsn(원)}"""
    rows = body.get("stk_invsr_orgn") or []
    if not rows:
        return None
    r = rows[0]
    price = abs(inum(r.get("cur_prc")))
    return {
        "date": str(r.get("dt", "")),
        "price": price,
        "prev_close": price - inum(r.get("pred_pre")),
        "acc_amount": abs(inum(r.get("acc_trde_prica"))) * amount_unit,
        "frgn": inum(r.get("frgnr_invsr")) * amount_unit,
        "orgn": inum(r.get("orgn")) * amount_unit,
        "prsn": inum(r.get("ind_invsr")) * amount_unit,
    }
