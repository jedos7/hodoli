from datetime import datetime

from app.kis.rest import Candle
from app.screener.runner import drop_incomplete_today, market_open


def _c(d):
    return Candle(d, 1, 1, 1, 1, 1, 1)


def test_market_open():
    assert market_open(datetime(2026, 9, 9, 12, 0))        # 수요일 장중
    assert not market_open(datetime(2026, 9, 9, 15, 45))   # 마감 후
    assert not market_open(datetime(2026, 9, 12, 12, 0))   # 토요일


def test_drop_today_only_during_session():
    cs = {"A": [_c("20260908"), _c("20260909")], "B": [_c("20260908")]}
    n = drop_incomplete_today(cs, datetime(2026, 9, 9, 12, 0))
    assert n == 1 and [c.date for c in cs["A"]] == ["20260908"] and len(cs["B"]) == 1
    cs = {"A": [_c("20260908"), _c("20260909")]}
    assert drop_incomplete_today(cs, datetime(2026, 9, 9, 16, 0)) == 0 and len(cs["A"]) == 2
