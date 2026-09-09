"""장중 돌파 감시. 눌림목 엄선 자리의 매수 기준가(자리 날 고가)·손절가(자리 날 저가)를 들고 실시간 체결을 본다.

  대기(waiting) → 체결가가 기준가를 넘으면 돌파(broke)  → 알림 "매수 기준가 돌파"
                → 체결가가 손절가 아래로 가면 이탈(stopped) → 알림 "손절가 이탈 (자리 무효)"
돌파 뒤 손절가를 깨면 다시 알림(손절). 알림은 항목당 상태 전이마다 한 번만.
감시 목록은 data/pullback.json 의 가장 최근 자리(엄선)에서 만들고, 손으로 추가할 수도 있다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class WatchItem:
    code: str
    name: str
    entry: int            # 매수 기준가
    stop: int             # 손절가
    setup_date: str       # 자리 잡은 날 YYYYMMDD
    theme: str = ""
    source: str = "눌림목"  # 눌림목 | 수동
    status: str = "waiting"  # waiting | broke | stopped
    price: int = 0
    price_at: str = ""
    broke_at: str = ""
    stopped_at: str = ""
    high_since: int = 0   # 돌파 후 최고가 (수익 추적용)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["riskPct"] = round((self.entry / self.stop - 1) * 100, 1) if self.stop else None
        d["gainPct"] = round((self.price / self.entry - 1) * 100, 2) if self.status == "broke" and self.entry else None
        return d


class Watchlist:
    def __init__(self):
        self.items: dict[str, WatchItem] = {}
        self.loaded_from: str = ""

    @property
    def codes(self) -> list[str]:
        return list(self.items)

    def load_pullback(self, path: Path, keep_manual: bool = True) -> int:
        return self.load_setups([(path, "눌림목")], keep_manual)

    def load_setups(self, sources: list[tuple[Path, str]], keep_manual: bool = True) -> int:
        """스크리너 결과 파일들(pullback.json, hoga.json)의 가장 최근 자리(엄선)로 감시 목록을 만든다.
        같은 종목이 두 스크리너에 모두 걸리면 먼저 온 소스(눌림목)를 쓴다. 이미 있는 항목은 상태를 유지한다."""
        manual = {c: it for c, it in self.items.items() if it.source == "수동"} if keep_manual else {}
        new: dict[str, WatchItem] = {}
        asofs = []
        for path, source in sources:
            if not path.exists():
                continue
            d = json.loads(path.read_text("utf-8"))
            asofs.append(str(d.get("asof")))
            # 파일의 확정 일봉 날짜(asof) 자리만 감시한다. 며칠 전 자리가 최신이라고 남아 있으면 안 된다.
            latest = str(d.get("asof") or "")
            rows = [r for r in d.get("rows", []) if r.get("strict") and r.get("entryPrice") and r["date"] == latest]
            if not rows:
                continue
            for r in rows:
                if r["date"] != latest or r["code"] in new:
                    continue
                old = self.items.get(r["code"])
                if old and old.setup_date == r["date"] and old.source == source:
                    new[r["code"]] = old  # 같은 자리면 상태 유지
                else:
                    new[r["code"]] = WatchItem(r["code"], r["name"], int(r["entryPrice"]), int(r["stopPrice"]), r["date"], r.get("theme", ""), source=source)
        new.update(manual)
        self.items = new
        self.loaded_from = f"{max(asofs) if asofs else '-'} · {len(new)}종목"
        return len(new)

    def add(self, code: str, name: str, entry: int, stop: int, theme: str = "") -> WatchItem:
        it = WatchItem(code, name, entry, stop, datetime.now().strftime("%Y%m%d"), theme, source="수동")
        self.items[code] = it
        return it

    def remove(self, code: str) -> bool:
        return self.items.pop(code, None) is not None

    def on_trade(self, code: str, price: int, ts: str | None = None) -> list[dict]:
        """체결 하나를 반영하고 상태가 바뀌면 알림 목록을 돌려준다."""
        it = self.items.get(code)
        if not it or price <= 0:
            return []
        now = ts or datetime.now().strftime("%H:%M:%S")
        it.price, it.price_at = price, now
        alerts: list[dict] = []
        if it.status == "waiting":
            if price > it.entry:
                it.status, it.broke_at, it.high_since = "broke", now, price
                alerts.append({"kind": "brk", "code": code, "name": it.name, "price": price, "entry": it.entry, "stop": it.stop, "at": now,
                               "text": f"{it.name} 매수 기준가 {it.entry:,}원 돌파 → {price:,}원 (손절 {it.stop:,}원, -{(it.entry / it.stop - 1) * 100:.1f}%)"})
            elif price < it.stop:
                it.status, it.stopped_at = "stopped", now
                alerts.append({"kind": "stop", "code": code, "name": it.name, "price": price, "entry": it.entry, "stop": it.stop, "at": now,
                               "text": f"{it.name} 손절가 {it.stop:,}원 이탈 → {price:,}원 (자리 무효)"})
        elif it.status == "broke":
            it.high_since = max(it.high_since, price)
            if price < it.stop:
                it.status, it.stopped_at = "stopped", now
                alerts.append({"kind": "stop", "code": code, "name": it.name, "price": price, "entry": it.entry, "stop": it.stop, "at": now,
                               "text": f"{it.name} 돌파 후 손절가 {it.stop:,}원 이탈 → {price:,}원 ({(price / it.entry - 1) * 100:+.1f}%)"})
        return alerts

    def as_list(self) -> list[dict]:
        order = {"broke": 0, "waiting": 1, "stopped": 2}
        return [it.as_dict() for it in sorted(self.items.values(), key=lambda x: (order.get(x.status, 9), x.name))]
