from datetime import datetime

from app.scheduler import Job, is_due


async def _noop():
    return {}


def test_due_after_time_on_weekday_once_per_day():
    j = Job("screener", "15:45", _noop)
    assert not is_due(j, datetime(2026, 9, 9, 15, 44))      # 아직
    assert is_due(j, datetime(2026, 9, 9, 15, 45))          # 정각
    assert is_due(j, datetime(2026, 9, 9, 20, 0))           # 늦게 켜도 따라잡기
    j.last_date = "2026-09-09"
    assert not is_due(j, datetime(2026, 9, 9, 20, 0))       # 오늘 이미 돌았음
    assert is_due(j, datetime(2026, 9, 10, 15, 45))         # 다음 날


def test_failed_job_retries_after_ten_minutes_up_to_three_times():
    j = Job("collect", "15:35", _noop)
    j.last_date, j.attempts = "2026-09-10", 1
    j.last_result = {"ok": False, "at": "2026-09-10T15:35:10", "error": "x"}
    assert not is_due(j, datetime(2026, 9, 10, 15, 40))      # 10분 안 됨
    assert is_due(j, datetime(2026, 9, 10, 15, 46))          # 10분 지남 → 재시도
    j.attempts = 3
    assert not is_due(j, datetime(2026, 9, 10, 16, 30))      # 3번 다 썼음
    j.attempts, j.last_result = 1, {"ok": True, "at": "2026-09-10T15:35:10"}
    assert not is_due(j, datetime(2026, 9, 10, 16, 30))      # 성공했으면 끝


def test_weekend_and_disabled_and_running():
    assert not is_due(Job("x", "15:45", _noop), datetime(2026, 9, 12, 16, 0))   # 토요일
    assert not is_due(Job("x", "", _noop), datetime(2026, 9, 9, 16, 0))         # 꺼짐
    j = Job("x", "15:45", _noop)
    j.running = True
    assert not is_due(j, datetime(2026, 9, 9, 16, 0))
