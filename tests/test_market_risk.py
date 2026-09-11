from datetime import datetime, timedelta, timezone

from app.market_risk import assess, base_time, line

KST = timezone(timedelta(hours=9))


def test_base_time_is_last_weekday_1530():
    assert base_time(datetime(2026, 9, 10, 19, 50, tzinfo=KST)) == datetime(2026, 9, 10, 15, 30, tzinfo=KST)
    assert base_time(datetime(2026, 9, 11, 9, 0, tzinfo=KST)) == datetime(2026, 9, 10, 15, 30, tzinfo=KST)    # 아침엔 전날
    assert base_time(datetime(2026, 9, 13, 19, 50, tzinfo=KST)) == datetime(2026, 9, 11, 15, 30, tzinfo=KST)   # 일요일 → 금요일


def test_levels_follow_verified_thresholds():
    assert assess({"nq": -0.35, "es": -0.22})[0] == "주의"     # 2026-09-10 저녁 실제 값 → 다음 날 코스피200 -2.8%
    assert assess({"nq": -0.6, "es": -0.1})[0] == "위험"
    assert assess({"nq": -0.1, "es": -0.4})[0] == "위험"
    assert assess({"nq": 0.2, "es": 0.1, "krw": 0.5, "cl": 3.0})[0] == "보통"   # 환율·유가는 예측력 없음 → 등급에 안 씀
    assert assess({"nq": None, "es": None})[0] == "미상"


def test_line_mentions_level_and_moves():
    risk = {"level": "주의", "note": "n", "moves": {"nq": -0.35, "es": -0.22, "cl": 1.79, "krw": None}}
    t = line(risk)
    assert "[주의]" in t and "나스닥선물 -0.35%" in t and "환율 -" in t
