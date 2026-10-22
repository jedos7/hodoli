"""다섯 매매법이 쓰는 돈을 한 곳에서 센다.

사용자 요구는 **총 투입 1,000만 원 미만**인데, 매매법마다 제 상한(total_cap)을 따로 들고 있어서
다섯이 동시에 차면 합이 그 상한을 넘을 수 있었다 (3백만 × 5 = 1,500만).
이 지갑이 다섯의 '쓴 돈'을 합쳐서 **전체 상한**도 함께 지킨다.

  쓸 수 있는 돈 = min(제 상한 − 제가 쓴 돈, 전체 상한 − 다섯이 쓴 돈)

'쓴 돈' 은 보유 금액 + 아직 체결 확인 전인 매수 주문 금액(committed)이다.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

GLOBAL_CAP = 9_990_000      # 사용자 요구: 총 투입 1,000만 원 미만


class Wallet:
    def __init__(self, cap=GLOBAL_CAP):
        self.cap = cap
        self.engines = []

    def register(self, *engines):
        for e in engines:
            if e not in self.engines:
                self.engines.append(e)
        return self

    def spent(self):
        """다섯이 지금 넣어 둔 돈 (흉내 보유는 뺀다 — committed 가 이미 뺀다)."""
        total = 0
        for e in self.engines:
            try:
                total += e.committed()
            except Exception:
                log.warning('지갑: %s 의 투입 금액을 못 읽었습니다', type(e).__name__)
        return total

    def room(self, engine):
        """이 매매법이 지금 더 넣을 수 있는 돈. 제 상한과 전체 상한을 둘 다 지킨다."""
        own = engine.settings['total_cap'] - engine.committed()
        return max(0, int(min(own, self.cap - self.spent())))

    def snapshot(self):
        return {'cap': self.cap, 'spent': self.spent(), 'engines': len(self.engines)}


class NoWallet:
    """지갑을 안 쓰는 경우(시험). 제 상한만 본다."""

    def register(self, *engines):
        return self

    def spent(self):
        return 0

    def room(self, engine):
        return max(0, engine.settings['total_cap'] - engine.committed())

    def snapshot(self):
        return {'cap': None, 'spent': 0, 'engines': 0}


NO_WALLET = NoWallet()
