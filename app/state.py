"""장중 상태. 종목 시세를 받아 테마 지표(활동도·5분 유입·대장주·집중도)를 계산한다.

프론트(static/index.html)의 recompute() 와 같은 수식을 쓴다. 계산의 진실은 여기 서버 쪽이고,
프론트는 서버가 준 값을 그대로 그리는 것이 목표다.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from app.supply import SupplyInputs
from app.supply import score as supply_score
from app.watch import Watchlist

FIVE_MIN = 300.0


@dataclass
class Stock:
    code: str
    name: str
    theme_id: str
    ref: int                   # 전일 종가
    price: int = 0
    change_rate: float = 0.0
    acc_amount: float = 0.0    # 억
    score: int = 50            # 수급 점수 0~100 (app/supply.py)
    flow: int = 0              # 수급 방향 -1/0/1 (외인+기관 순매수 부호)
    last_ts: float = 0.0
    # 수급 입력값
    cttr: float | None = None      # 체결강도 (실시간 체결)
    frgn_eok: float | None = None  # 외국인 순매수 (억)
    orgn_eok: float | None = None  # 기관 순매수 (억)
    investor_src: str = ""         # "추정" | "전일" | "모의" | ""
    investor_ts: float = 0.0
    why: str = ""                  # 테마 편입 사유 (수집기가 채움)
    nx_price: int = 0              # NXT(프리·애프터장) 최근 체결가
    nx_at: str = ""                # 그 시각 HH:MM:SS

    @property
    def net_investor_eok(self) -> float | None:
        if self.frgn_eok is None and self.orgn_eok is None:
            return None
        return (self.frgn_eok or 0.0) + (self.orgn_eok or 0.0)

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "price": self.price, "chg": round(self.change_rate, 2),
                "vol": round(self.acc_amount, 1), "score": self.score, "flow": self.flow,
                "cttr": None if self.cttr is None else round(self.cttr, 1),
                "frgn": None if self.frgn_eok is None else round(self.frgn_eok, 1),
                "orgn": None if self.orgn_eok is None else round(self.orgn_eok, 1),
                "src": self.investor_src, "why": self.why,
                "nx": self.nx_price or None, "nxAt": self.nx_at,
                "nxChg": round((self.nx_price / self.price - 1) * 100, 2) if self.nx_price and self.price else None}


@dataclass
class Theme:
    id: str
    name: str
    grade: str
    report: str
    news: str
    stocks: list[Stock]
    news_url: str = ""
    news_at: str = ""
    news_updated: str = ""
    inflow: deque = field(default_factory=lambda: deque(maxlen=4000))  # (ts, 억) 최근 유입 기록
    # 계산값
    total: float = 0.0
    avg: float = 0.0
    five: float = 0.0
    prev_five: float = 0.0
    leader: Stock | None = None
    top: Stock | None = None
    conc: float = 0.0
    width: int = 0
    heat: int = 0

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "grade": self.grade, "report": self.report, "news": self.news,
                "newsUrl": self.news_url, "newsAt": self.news_at, "newsUpdated": self.news_updated,
                "total": round(self.total, 1), "avg": round(self.avg, 2), "five": round(self.five, 1),
                "prevFive": round(self.prev_five, 1), "leader": self.leader.code if self.leader else None,
                "top": self.top.code if self.top else None, "conc": round(self.conc, 1), "width": self.width,
                "heat": self.heat, "stocks": [s.as_dict() for s in self.stocks]}


class MarketState:
    def __init__(self, themes: list[Theme]):
        self.themes = themes
        self.stocks: dict[str, Stock] = {s.code: s for t in themes for s in t.stocks}
        self.theme_of: dict[str, Theme] = {s.code: t for t in themes for s in t.stocks}
        self.tick = 0
        self.started = time.time()
        self.watch = Watchlist()          # 장중 돌파 감시 (app/watch.py)
        self.alerts: list[dict] = []      # 방송 대기 중인 알림 (broadcaster 가 비운다)

    def on_trade(self, code: str, price: int, ts: str | None = None) -> None:
        """감시 종목이면 돌파/이탈을 판정해 알림을 쌓는다. 테마 종목 갱신(update)과 별개."""
        for a in self.watch.on_trade(code, price, ts):
            self.alerts.append(a)

    @classmethod
    def from_file(cls, path: Path) -> "MarketState":
        d = json.loads(path.read_text("utf-8"))
        themes = []
        for t in d["themes"]:
            stocks = [Stock(code=s["code"], name=s["name"], theme_id=t["id"], ref=int(s["ref"]), price=int(s["ref"]), why=s.get("why", ""))
                      for s in t["stocks"]]
            themes.append(Theme(id=t["id"], name=t["name"], grade=t.get("grade", "-"), report=t.get("report", ""), news=t.get("news", ""),
                                stocks=stocks, news_url=t.get("newsUrl", ""), news_at=t.get("newsAt", ""), news_updated=t.get("updatedAt", "")))
        st = cls(themes)
        st.recompute()
        return st

    @property
    def codes(self) -> list[str]:
        return list(self.stocks)

    # ── 시세 반영 ──
    def update(self, code: str, price: int, acc_amount_eok: float, change_rate: float | None = None,
               ts: float | None = None, cttr: float | None = None) -> None:
        s = self.stocks.get(code)
        if s is None:
            return
        ts = ts or time.time()
        inc = max(0.0, acc_amount_eok - s.acc_amount)
        s.price = price
        s.acc_amount = acc_amount_eok
        s.change_rate = change_rate if change_rate is not None else (price / s.ref - 1) * 100 if s.ref else 0.0
        s.last_ts = ts
        if cttr is not None and cttr > 0:
            s.cttr = cttr
        if inc > 0:
            self.theme_of[code].inflow.append((ts, inc))

    def update_nx(self, code: str, price: int, at: str = "") -> None:
        """NXT 체결. 정규장 가격·거래대금과 섞지 않고 따로 둔다."""
        s = self.stocks.get(code)
        if s and price > 0:
            s.nx_price, s.nx_at = price, at

    def set_investor(self, code: str, frgn_eok: float | None, orgn_eok: float | None, src: str, ts: float | None = None) -> None:
        s = self.stocks.get(code)
        if s is None:
            return
        s.frgn_eok, s.orgn_eok, s.investor_src, s.investor_ts = frgn_eok, orgn_eok, src, ts or time.time()

    def seed(self, code: str, price: int, acc_amount_eok: float, prev_close: int | None = None, change_rate: float | None = None) -> None:
        """시작 시 초기값. 누적 거래대금을 '유입' 으로 세지 않는다 (그러면 5분 유입 = 전체가 되어 활동도가 99 로 몰린다)."""
        s = self.stocks.get(code)
        if s is None:
            return
        if prev_close:
            s.ref = prev_close
        s.price = price
        s.acc_amount = acc_amount_eok
        s.change_rate = change_rate if change_rate is not None else (price / s.ref - 1) * 100 if s.ref else 0.0
        s.last_ts = time.time()

    def set_ref(self, code: str, prev_close: int) -> None:
        if code in self.stocks and prev_close > 0:
            self.stocks[code].ref = prev_close

    # ── 테마 지표 ──
    def recompute(self, now: float | None = None) -> None:
        now = now or time.time()
        for t in self.themes:
            ss = t.stocks
            t.total = sum(s.acc_amount for s in ss)
            t.avg = sum(s.change_rate for s in ss) / len(ss) if ss else 0.0
            t.five = sum(a for ts, a in t.inflow if now - ts <= FIVE_MIN)
            t.prev_five = sum(a for ts, a in t.inflow if FIVE_MIN < now - ts <= 2 * FIVE_MIN)
            t.leader = max(ss, key=lambda s: s.change_rate, default=None)
            t.top = max(ss, key=lambda s: s.acc_amount, default=None)
            t.conc = (t.top.acc_amount / t.total * 100) if t.top and t.total else 0.0
            t.width = sum(1 for s in ss if s.change_rate > 0)
            ratio = (t.five / t.total) if t.total else 0.0
            # 5분 유입/전체 비율은 0.2 에서 자른다. 장 시작 직후에는 전체 = 5분 유입이라 모든 테마가 99 로 몰리는 것을 막는다
            heat = 28 + t.avg * 4.5 + min(ratio, 0.2) * 180 + (t.width / len(ss) if ss else 0) * 10
            t.heat = int(max(3, min(99, round(heat))))
            for s in ss:
                s.score, s.flow = supply_score(SupplyInputs(
                    cttr=s.cttr, net_investor_eok=s.net_investor_eok, turnover_eok=s.acc_amount,
                    five=t.five, prev_five=t.prev_five))

    def snapshot(self) -> dict:
        return {"type": "tick", "t": self.tick, "ts": time.time(), "themes": [t.as_dict() for t in self.themes],
                "watch": self.watch.as_list()}
