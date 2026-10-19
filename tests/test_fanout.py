"""겹쳐 보내기 도우미: 순서 유지 · 한 건 실패가 전체를 멈추지 않음 · 동시 개수 제한."""
import asyncio

import pytest

from app.fanout import fanout


def test_keeps_input_order_even_when_later_items_finish_first():
    async def work(x):
        await asyncio.sleep(0.01 * (3 - x))      # 뒤에 넣은 것이 먼저 끝난다
        return x * 10
    assert asyncio.run(fanout([0, 1, 2], work)) == [0, 10, 20]


def test_one_failure_does_not_stop_the_rest():
    async def work(x):
        if x == 1:
            raise ValueError('조회 실패')
        return x
    out = asyncio.run(fanout([0, 1, 2], work))
    assert out[0] == 0 and out[2] == 2 and isinstance(out[1], ValueError)


def test_never_runs_more_than_the_limit_at_once():
    state = {'now': 0, 'max': 0}

    async def work(x):
        state['now'] += 1
        state['max'] = max(state['max'], state['now'])
        await asyncio.sleep(0.01)
        state['now'] -= 1
        return x
    assert asyncio.run(fanout(range(12), work, limit=3)) == list(range(12))
    assert state['max'] == 3


def test_cancellation_is_not_swallowed():
    async def work(x):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fanout([0], work))
