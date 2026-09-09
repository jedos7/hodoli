import asyncio

from app.notify import Telegram


def test_disabled_without_token_and_kind_filter():
    t = Telegram()
    t.token, t.chat_id = "", ""
    assert not t.enabled
    assert asyncio.run(t.send("x")) is False                       # 설정 없으면 조용히 False
    t.token, t.chat_id, t.kinds = "tok", "1", {"brk"}
    assert t.enabled
    # 종류가 목록에 없으면 전송 시도 자체를 안 한다 (네트워크 호출 없음)
    assert asyncio.run(t.send_alert({"kind": "up", "text": "x"})) is False
