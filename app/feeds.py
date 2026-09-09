"""시세 피드. 두 구현 모두 MarketState 만 갱신하고, 방송은 main 의 broadcaster 가 맡는다.

MockFeed : 키 없이 랜덤워크로 시세를 만든다. 개발·UI 작업용.
KisFeed  : 시작 시 REST 로 전일 종가·현재가를 채우고, 이후 웹소켓 체결 데이터로 갱신한다.
"""
from __future__ import annotations

import asyncio
import logging
import random

from app.config import Settings
from app.kis.auth import KisAuth
from app.kis.parse import Trade
from app.kis.rest import KisRest
from app.kis.ws import KisWebSocket
from app.state import MarketState

log = logging.getLogger(__name__)


def _tick_size(p: float) -> int:
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if p < lim:
            return t
    return 1000


def _round_px(p: float) -> int:
    t = _tick_size(p)
    return int(round(p / t) * t)


class MockFeed:
    def __init__(self, state: MarketState, interval: float = 1.0):
        self.state = state
        self.interval = interval
        self._mom: dict[str, float] = {}
        self._chg: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        # 시작 등락률·거래대금·수급 입력값을 그럴듯하게 깔아 둔다
        for t in self.state.themes:
            self._mom[t.id] = random.gauss(0, 0.6)
            for s in t.stocks:
                self._chg[s.code] = random.gauss(2.0, 3.0)
                s.acc_amount = random.uniform(80, 3000)
                self._seed_supply(s)
        self._task = asyncio.create_task(self._loop(), name="mock-feed")

    def _seed_supply(self, s) -> None:
        """체결강도와 외인·기관 순매수를 등락률과 느슨하게 연동해 만든다."""
        c = self._chg[s.code]
        s.cttr = max(20.0, random.gauss(100 + c * 8, 25))
        tilt = random.gauss(c * 0.008, 0.03)  # 순매수/거래대금 비율
        frgn = s.acc_amount * tilt * random.uniform(0.3, 0.7)
        orgn = s.acc_amount * tilt - frgn
        self.state.set_investor(s.code, round(frgn, 1), round(orgn, 1), "모의")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def watch_codes(self, codes: list[str]) -> int:
        return 0  # 모의 피드는 테마 종목만 흔든다

    async def _loop(self) -> None:
        n = 0
        while True:
            n += 1
            for t in self.state.themes:
                if n % 40 == 0:
                    self._mom[t.id] = max(-1.2, min(1.2, random.gauss(0, 0.6)))
                mom = self._mom[t.id]
                for s in t.stocks:
                    c = self._chg[s.code]
                    if c < 29.9:
                        c = max(-30.0, min(30.0, c + random.gauss(0, 0.11) + mom * 0.012))
                    self._chg[s.code] = c
                    inc = s.acc_amount * (0.0012 + random.random() * 0.003) * (1 + abs(c) / 12) * (1.25 if mom > 0 else 0.85)
                    cttr = max(20.0, (s.cttr or 100.0) + random.gauss(c * 0.05, 2.0))
                    self.state.update(s.code, _round_px(s.ref * (1 + c / 100)), s.acc_amount + inc, change_rate=c, cttr=cttr)
                    self.state.on_trade(s.code, s.price)
                    if n % 15 == 0:  # 외인·기관 순매수는 15초마다 조금씩 누적
                        drift = inc * random.gauss(c * 0.02, 0.15)
                        self.state.set_investor(s.code, round((s.frgn_eok or 0) + drift * 0.6, 1), round((s.orgn_eok or 0) + drift * 0.4, 1), "모의")
            self.state.tick = n
            await asyncio.sleep(self.interval)


class KiwoomFeed:
    """키움 REST API: 시작 시 ka10001 로 현재가·전일 종가, 이후 WebSocket 0B 체결로 갱신, ka10059 로 외인·기관 순매수."""

    def __init__(self, state: MarketState, settings: Settings):
        from app.kiwoom.auth import KiwoomAuth
        from app.kiwoom.rest import KiwoomRest
        from app.kiwoom.ws import KiwoomWebSocket

        self.state = state
        self.s = settings
        self.auth = KiwoomAuth(settings)
        self.rest = KiwoomRest(settings, self.auth)
        self.ws = KiwoomWebSocket(settings, self.auth, self._on_trade)
        self._task: asyncio.Task | None = None
        self._inv_task: asyncio.Task | None = None
        self._nx_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self._refresh_all(initial=True)
        self.state.recompute()
        nx = [c + "_NX" for c in self.state.codes]  # 테마 종목은 NXT(프리·애프터장) 체결도 같이 받는다
        self._task = asyncio.create_task(self.ws.run(self.state.codes + self.state.watch.codes + nx), name="kiwoom-ws")
        self._inv_task = asyncio.create_task(self._investor_loop(), name="kiwoom-investor")
        self._nx_task = asyncio.create_task(self._seed_nx(), name="kiwoom-nx-seed")

    async def _seed_nx(self) -> None:
        """NXT 마지막 체결가를 조회(ka10001, 코드_NX)로 채운다. 프리장(08:00~08:50)이 끝난 뒤 켜거나 테마가 바뀌어도
        카드에 NXT 가격이 보이게. 실시간 NXT 체결이 이미 들어온 종목은 건너뛴다. 시작을 막지 않게 뒤에서 돈다."""
        for code in self.state.codes:
            s = self.state.stocks.get(code)
            if not s or s.nx_price:
                continue
            try:
                b = await self.rest.basic(code + "_NX")
                if b.price > 0 and b.volume > 0:  # 거래가 있었던 종목만
                    self.state.update_nx(code, b.price, "조회")
            except Exception as e:
                log.debug("NXT 조회 실패 %s: %s", code, e)

    async def watch_codes(self, codes: list[str]) -> int:
        """감시 종목이 바뀌었을 때 실시간 구독에 추가한다."""
        return await self.ws.ensure(codes)

    def status(self) -> dict:
        return {"type": "kiwoom", **self.ws.status()}

    async def _refresh_all(self, initial: bool = False) -> None:
        """ka10059 로 종목마다 현재가·전일종가·누적거래대금·외인/기관 순매수를 채운다 (종목당 조회 1회)."""
        from datetime import date

        today = date.today().strftime("%Y%m%d")
        for code in self.state.codes:
            try:
                d = await self.rest.snapshot(code)
            except Exception as e:
                log.warning("키움 조회 실패 %s: %s", code, e)
                continue
            if not d:
                continue
            s = self.state.stocks[code]
            if initial:
                self.state.seed(code, d["price"], d["acc_amount"] / 1e8, prev_close=d["prev_close"])
            elif d["acc_amount"] / 1e8 > s.acc_amount:  # 실시간이 끊겼을 때를 대비해 누적 거래대금은 큰 쪽으로
                self.state.update(code, d["price"], d["acc_amount"] / 1e8)
            src = "당일" if d["date"] == today else d["date"][4:6] + "/" + d["date"][6:]
            self.state.set_investor(code, d["frgn"] / 1e8, d["orgn"] / 1e8, src)

    async def stop(self) -> None:
        self.ws.stop()
        for t in (self._task, self._inv_task, self._nx_task):
            if t:
                t.cancel()
        await self.rest.close()

    def _on_trade(self, t: Trade) -> None:
        hhmmss = t.time[:2] + ":" + t.time[2:4] + ":" + t.time[4:6] if len(t.time) >= 6 else ""
        if t.market == "NX":
            self.state.update_nx(t.code, t.price, hhmmss)   # 야간·프리장은 참고 가격만, 돌파 판정·대금에는 안 섞는다
            return
        self.state.update(t.code, t.price, t.acc_amount_eok, change_rate=t.change_rate, cttr=t.cttr)
        self.state.on_trade(t.code, t.price, hhmmss or None)
        self.state.tick += 1

    async def _investor_loop(self) -> None:
        """2분마다 외국인·기관 순매수(당일 잠정치)를 다시 받는다."""
        while True:
            await asyncio.sleep(120.0)
            await self._refresh_all()


class KisFeed:
    def __init__(self, state: MarketState, settings: Settings):
        self.state = state
        self.s = settings
        self.auth = KisAuth(settings)
        self.rest = KisRest(settings, self.auth)
        self.ws = KisWebSocket(settings, self.auth, self._on_trade)
        self._task: asyncio.Task | None = None
        self._inv_task: asyncio.Task | None = None
        self._estimate_ok = True

    async def start(self) -> None:
        # 1) REST 로 초기값: 전일 종가(ref), 현재가, 누적 거래대금
        for code in self.state.codes:
            try:
                q = await self.rest.quote(code)
                self.state.seed(code, q.price, q.acc_amount / 1e8, prev_close=q.prev_close, change_rate=q.change_rate)
            except Exception as e:  # 한 종목 실패로 전체를 멈추지 않는다
                log.warning("초기 시세 실패 %s: %s", code, e)
        self.state.recompute()
        # 2) 웹소켓 실시간 체결
        self._task = asyncio.create_task(self.ws.run(self.state.codes), name="kis-ws")
        # 3) 외인·기관 순매수 주기 조회
        self._inv_task = asyncio.create_task(self._investor_loop(), name="kis-investor")

    async def stop(self) -> None:
        self.ws.stop()
        for t in (self._task, self._inv_task):
            if t:
                t.cancel()
        await self.rest.close()

    def _on_trade(self, t: Trade) -> None:
        self.state.update(t.code, t.price, t.acc_amount_eok, change_rate=t.change_rate, cttr=t.cttr)
        self.state.on_trade(t.code, t.price)
        self.state.tick += 1

    async def watch_codes(self, codes: list[str]) -> int:
        """KIS 웹소켓은 시작 시 구독 목록이 고정이라 추가 구독은 재시작이 필요하다."""
        missing = [c for c in codes if c not in self.state.codes]
        if missing:
            log.warning("KIS 피드는 감시 종목 %d개를 실시간 구독에 추가하지 못합니다 (서버 재시작 필요)", len(missing))
        return 0

    async def _investor_loop(self) -> None:
        """장중 추정치를 우선 쓰고, 그 API 가 안 되면(모의투자 등) 전일 확정치로 대신한다.
        한 바퀴에 종목 수만큼 호출하므로 호출 제한을 rest 가 알아서 늦춘다."""
        interval = 60.0 if self.s.env == "real" else 180.0
        while True:
            for code in self.state.codes:
                s = self.state.stocks[code]
                try:
                    if self._estimate_ok:
                        est = await self.rest.investor_estimate(code)
                        if est is not None:
                            frgn_qty, orgn_qty, _ = est
                            px = s.price or s.ref
                            self.state.set_investor(code, frgn_qty * px / 1e8, orgn_qty * px / 1e8, "추정")
                            continue
                except Exception as e:
                    if self._estimate_ok:
                        log.warning("추정가집계 API 사용 불가(%s) → 전일 확정치로 대체", e)
                    self._estimate_ok = False
                try:
                    d = await self.rest.investor_daily(code)
                    if d:
                        self.state.set_investor(code, d["frgn_amount"] / 1e8, d["orgn_amount"] / 1e8, "전일")
                except Exception as e:
                    log.warning("투자자 매매동향 실패 %s: %s", code, e)
            await asyncio.sleep(interval)
