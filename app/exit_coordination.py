"""Share the account feed; prevent two independent exit plans selling one holding."""
from app.kiwoom.orders import NotSent


class CoordinatedBroker:
    def __init__(self, broker, peer, peer_name):
        self.broker, self.peer, self.peer_name = broker, peer, peer_name

    def guard(self, code):
        if self.peer.error or not self.peer.owner:
            raise NotSent(f'{self.peer_name} 주문 기록 확인 필요 · 중복 매도 방지를 위해 중단')
        p = self.peer.plans.get(code, {})
        if p.get('status') in ('prepared','armed','checking','ordered','remainder_check') or self.peer.blocked(code):
            raise NotSent(f'이 종목은 {self.peer_name}에 등록되어 있습니다 · 해당 계획을 먼저 끄고 주문내역을 확인하세요')

    async def positions(self):
        return await self.broker.positions()

    async def open_orders(self, code):
        self.guard(code)
        rows = await self.broker.open_orders(code)
        self.guard(code)
        return rows

    async def sell_once(self, code, quantity, before_send):
        def guard():
            self.guard(code)
            before_send()
        guard()
        return await self.broker.sell_once(code, quantity, guard)

    async def detail(self, order):
        return await self.broker.detail(order)

    async def validate_sell_limit(self, code, price):
        self.guard(code)
        await self.broker.validate_sell_limit(code, price)
        self.guard(code)

    async def sell_limit_once(self, code, quantity, price, before_send):
        def guard():
            self.guard(code)
            before_send()
        guard()
        return await self.broker.sell_limit_once(code, quantity, price, guard)

    async def extended_metadata(self, code):
        self.guard(code)
        value=await self.broker.extended_metadata(code)
        self.guard(code)
        return value

    async def extended_book(self, code, venue):
        self.guard(code)
        value=await self.broker.extended_book(code,venue)
        self.guard(code)
        return value

    async def extended_sell_once(self, code, quantity, price, venue, before_send):
        def guard():
            self.guard(code);before_send()
        guard()
        return await self.broker.extended_sell_once(code,quantity,price,venue,guard)

    async def extended_cancel_once(self, order, before_send):
        def guard():
            self.guard(order['code']);before_send()
        guard()
        return await self.broker.extended_cancel_once(order,guard)

    async def extended_cancel_detail(self, order):
        return await self.broker.extended_cancel_detail(order)


class ExecutionGroup:
    def __init__(self, *services):
        self.services = services
        self.engine = self  # SellService's shared connection lifecycle calls pause().

    def pause(self, code=None, note='계좌 연결 변경 · 다시 확인 후 켜세요'):
        error = None
        for service in self.services:
            try:
                service.engine.pause(code, note)
            except Exception as exc:
                error = exc
        if error:
            raise error

    def quote(self, trade, stamp):
        for service in self.services:
            service.quote(trade, stamp)

    def account(self):
        for service in self.services:
            service.account()
