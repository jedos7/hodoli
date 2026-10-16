"""키움 실시간 WebSocket.

  접속 → {"trnm":"LOGIN","token":…} → 응답 return_code 0
  구독 → {"trnm":"REG","grp_no":"1","refresh":"1","data":[{"item":[코드…],"type":["0B"]}]}   (0B 주식체결)
  수신 → {"trnm":"REAL","data":[{"type":"0B","item":"005930","values":{"10":…}}]}
  PING → {"trnm":"PING"} 을 받으면 그대로 돌려준다.
끊기면 지수 백오프로 재접속하고 다시 구독한다. 그룹당 종목 수는 MAX_ITEMS 로 나눈다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable

import websockets

from app.config import Settings
from app.kiwoom.auth import KiwoomAuth
from app.kiwoom.parse import parse_real, inum
from app.kis.parse import Trade

log = logging.getLogger(__name__)
MAX_ITEMS = 100
REG_TIMEOUT = 10
RECEIVE_TIMEOUT = 60


class KiwoomWebSocket:
    def __init__(self, settings: Settings, auth: KiwoomAuth, on_trade: Callable[[Trade], None]):
        self.s = settings
        self.auth = auth
        self.on_trade = on_trade
        self._stop = asyncio.Event()
        self._ws = None
        self._codes: list[str] = []       # 구독하려는 전체 목록 (재접속 때 다시 건다)
        self._subscribed: set[str] = set()
        self._grp = 0
        self.on_account = None
        self._account_registered = False
        self._market_registered = False
        self.market_state = None
        self.regular_trade_state = None
        self._invalid_market_event = False
        self.last_trade_at: float = 0.0   # 마지막 체결 수신 시각 (epoch) — 끊김 감시용
        self.reconnects = 0
        self._registration_lock = asyncio.Lock()
        self._reg_ack = None
        self._registration_error = ''
        self._received_at = None

    @property
    def connected(self) -> bool:
        return (self._ws is not None and not self._registration_error
                and self._received_at is not None
                and 0 <= time.monotonic() - self._received_at < RECEIVE_TIMEOUT)

    @property
    def exit_ready(self) -> bool:
        return self.connected and self._account_registered and self._market_registered

    def status(self) -> dict:
        import time
        return {"connected": self.connected, "subscribed": len(self._subscribed), "reconnects": self.reconnects,
                "subscriptionError": self._registration_error,
                "lastTradeAgo": round(time.time() - self.last_trade_at) if self.last_trade_at else None,
                "marketSession": self.market_phase(), "marketNotification": self.market_state}

    def market_phase(self):
        """Use a current KRX notification, or fresh broker-labelled regular trades.

        0s announces session transitions and may be absent on an intraday login.
        Official 0B FID 290=2 identifies an in-session trade. It can bootstrap
        continuous trading only before 15:20, never the closing auction.
        """
        from app.autosell import now_kst
        from datetime import datetime
        if not self.connected or self._invalid_market_event:
            return None
        now = now_kst()
        if self.market_state:
            stamp = datetime.fromisoformat(self.market_state['at'])
            if stamp.date() == now.date():
                # A fresh trade must never undo an explicit pre/open/close state.
                return self.market_state
        state = self.regular_trade_state
        if state and now.weekday() < 5 and (9, 0) <= (now.hour, now.minute) < (15, 20):
            stamp = datetime.fromisoformat(state['at'])
            if stamp.date() == now.date() and 0 <= (now - stamp).total_seconds() <= 10:
                return state
        return None

    def _observe_regular_session(self, msg):
        from app.autosell import now_kst, KST
        from datetime import datetime
        now = now_kst()
        if now.weekday() >= 5 or not (9, 0) <= (now.hour, now.minute) < (15, 20):
            return
        for item in msg.get('data', []):
            # No _NX, _AL or unknown venue suffixes; no REST/chart prices.
            if item.get('type') != '0B' or not re.fullmatch(r'A?[0-9A-Z]{6}', str(item.get('item', ''))):
                continue
            values = item.get('values') or {}
            if str(values.get('290', '')).strip() != '2':
                continue
            stamp_text = str(values.get('20', ''))
            if not re.fullmatch(r'\d{6}', stamp_text):
                continue
            try:
                stamp = datetime.combine(now.date(), datetime.strptime(stamp_text, '%H%M%S').time(), KST)
                if (not (9, 0) <= (stamp.hour, stamp.minute) < (15, 20)
                        or not 0 <= (now - stamp).total_seconds() <= 10
                        or abs(inum(values.get('10'))) <= 0 or abs(inum(values.get('15'))) <= 0):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            if self.regular_trade_state and stamp < datetime.fromisoformat(self.regular_trade_state['at']):
                continue
            self.regular_trade_state = {'value': '3', 'at': stamp.isoformat(), 'source': 'KRX_0B_290'}

    def stop(self) -> None:
        self._stop.set()

    async def _register(self, ws, packet):
        # REG replies need not echo a group id. Keep exactly one request awaiting
        # acknowledgement; a sent packet alone never establishes readiness.
        async with self._registration_lock:
            if ws is not self._ws or self._registration_error:
                raise RuntimeError('키움 실시간 구독 연결 확인 필요')
            future = asyncio.get_running_loop().create_future()
            self._reg_ack = future
            try:
                await ws.send(json.dumps(packet))
                await asyncio.wait_for(future, REG_TIMEOUT)
                if ws is not self._ws or self._registration_error:
                    raise RuntimeError('구독 확인 중 연결 변경')
            except (Exception, asyncio.CancelledError):
                self._registration_error = '실시간 구독 확인 실패 · 연결을 다시 확인하세요'
                self.market_state = None
                self.regular_trade_state = None
                # A late ACK must not be mistaken for the next subscription.
                if hasattr(ws, 'close'):
                    await ws.close()
                raise
            finally:
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # Also consume errors if send() itself failed.
                self._reg_ack = None

    async def _receive(self, ws):
        try:
            while not self._stop.is_set():
                # Server PINGs count as liveness even when there are no trades.
                raw = await asyncio.wait_for(ws.recv(), RECEIVE_TIMEOUT)
                await self._handle(ws, raw)
        finally:
            if self._ws is ws:
                self._ws = None
                self.market_state = None
                self.regular_trade_state = None
            if self._reg_ack is not None and not self._reg_ack.done():
                self._reg_ack.set_exception(RuntimeError('구독 확인 중 연결 끊김'))

    async def _reg(self, ws, codes: list[str]) -> None:
        for i in range(0, len(codes), MAX_ITEMS):
            self._grp += 1
            batch = codes[i : i + MAX_ITEMS]
            await self._register(ws, {"trnm": "REG", "grp_no": str(self._grp), "refresh": "1",
                                      "data": [{"item": batch, "type": ["0B"]}]})
            self._subscribed.update(batch)

    async def ensure_account(self):
        if self._ws is None:
            raise RuntimeError("키움 WebSocket 연결 대기")
        if not self._account_registered:
            await self._register(self._ws, {"trnm": "REG", "grp_no": "9000", "refresh": "1",
                                           "data": [{"item": [""], "type": ["00", "04"]}]})
            self._account_registered = True

    async def ensure(self, codes: list[str]) -> int:
        """접속 중이면 아직 구독하지 않은 종목만 추가로 건다 (감시 종목이 늘었을 때). 돌려주는 값 = 새로 건 수."""
        for c in codes:
            if c not in self._codes:
                self._codes.append(c)
        new = [c for c in codes if c not in self._subscribed]
        if new and self._ws is not None:
            try:
                await self._reg(self._ws, new)
                log.info("키움 WS 추가 구독 %d 종목", len(new))
            except Exception as e:
                log.warning("추가 구독 실패(재접속 때 다시 시도): %s", e)
                raise
        return len(new)

    async def ensure_market(self):
        if self._ws is None:
            raise RuntimeError('키움 장 상태 연결 대기')
        if not self._market_registered:
            await self._register(self._ws, {'trnm': 'REG', 'grp_no': '9001', 'refresh': '1',
                                           'data': [{'item': [''], 'type': ['0s']}]})
            self._market_registered = True

    async def run(self, codes: list[str]) -> None:
        for c in codes:
            if c not in self._codes:
                self._codes.append(c)
        backoff = 1
        while not self._stop.is_set():
            try:
                token = await self.auth.access_token()
                async with websockets.connect(self.s.kiwoom_ws_url, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
                    login = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if login.get('trnm') != 'LOGIN' or str(login.get('return_code')) != '0':
                        raise RuntimeError('키움 WS 로그인 확인 실패')
                    self._subscribed, self._grp = set(), 0
                    self._account_registered = False
                    self._market_registered = False
                    self.market_state = None
                    self.regular_trade_state = None
                    self._invalid_market_event = False
                    self._registration_error = ''
                    self._received_at = time.monotonic()
                    self._ws = ws
                    reader = asyncio.create_task(self._receive(ws))
                    try:
                        await self._reg(ws, list(self._codes))
                        log.info("키움 WS 접속 · %d 종목 구독 확인", len(self._subscribed))
                        backoff = 1
                        await reader
                    finally:
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                log.warning("키움 WS 끊김: %s → %ds 후 재접속", e, backoff)
            except Exception:
                log.exception("키움 WS 오류 → %ds 후 재접속", backoff)
            finally:
                self._ws = None
                self.market_state = None
                self.regular_trade_state = None
                self._account_registered = self._market_registered = False
                self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handle(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self._received_at = time.monotonic()
        t = msg.get("trnm")
        if t == "PING":
            await ws.send(raw)
        elif t == "REAL":
            self._observe_regular_session(msg)
            from app.autosell import now_kst, KST
            from datetime import datetime
            for item in msg.get('data', []):
                if item.get('type') != '0s':
                    continue
                v = item.get('values') or {}
                # NXT events share 0s; they must not overwrite the KRX session.
                if str(v.get('215')) not in ('0', '2', '3', '4', '8', '9', 'a', 'b', 'c', 'd'):
                    continue
                now = now_kst()
                try:
                    stamp = datetime.combine(now.date(), datetime.strptime(v['20'], '%H%M%S').time(), KST)
                    if not 0 <= (now - stamp).total_seconds() <= 90:
                        continue
                    if self.market_state and stamp < datetime.fromisoformat(self.market_state['at']):
                        continue
                    self.market_state = {'value': str(v['215']), 'at': stamp.isoformat()}
                    self._invalid_market_event = False
                except (KeyError, ValueError):
                    self.market_state = None
                    self.regular_trade_state = None
                    self._invalid_market_event = True
            if self.on_account:
                for item in msg.get("data", []):
                    if item.get("type") in ("00", "04"):
                        self.on_account(item)
            for tr in parse_real(msg, self.s.kiwoom_amount_unit):
                self.last_trade_at = time.time()
                self.on_trade(tr)
        elif t == "REG":
            if str(msg.get('return_code')) != '0' or self._reg_ack is None or self._reg_ack.done():
                self._registration_error = '키움 실시간 구독 거부/응답 확인 실패'
                self.market_state = None
                self.regular_trade_state = None
                if self._reg_ack is not None and not self._reg_ack.done():
                    self._reg_ack.set_exception(RuntimeError(self._registration_error))
                raise RuntimeError(self._registration_error)
            self._reg_ack.set_result(None)
