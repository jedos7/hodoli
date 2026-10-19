"""조회를 몇 개씩 겹쳐 보내는 작은 도우미.

키움 조회는 app/kiwoom/rest.py 의 잠금에서 초당 KIWOOM_RPS 개로 **간격**이 맞춰진다.
하나씩 기다리면 '간격 + 왕복시간' 이 매번 더해져 실제로는 초당 1.5~2개밖에 못 보낸다.
몇 개를 겹쳐 두면 간격은 그대로 지키면서 왕복시간이 겹쳐 전체 시간이 준다.
(키움이 보는 요청 간격은 잠금이 정하므로 겹쳐도 호출 제한은 그대로다.)
"""
from __future__ import annotations

import asyncio
import os

LIMIT = max(1, int(os.getenv('FANOUT_LIMIT', '4')))   # 한 번에 겹쳐 보낼 조회 수 (1 = 하나씩)


async def fanout(items, work, limit=LIMIT):
    """items 를 limit 개씩 겹쳐 work(item) 에 넘기고 **입력 순서대로** 결과를 돌려준다.
    한 건이 실패하면 그 자리에 예외 객체가 들어가고 나머지는 계속한다 (취소는 그대로 올린다)."""
    items = list(items)
    out = [None] * len(items)
    sem = asyncio.Semaphore(max(1, limit))

    async def one(i, item):
        async with sem:
            try:
                out[i] = await work(item)
            except Exception as exc:
                out[i] = exc

    await asyncio.gather(*(one(i, x) for i, x in enumerate(items)))
    return out
