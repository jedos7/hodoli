from datetime import datetime

from app.collectors.overnight import KST, Row, baseline_time, notes, series, value_at


def test_baseline_is_most_recent_2005():
    assert baseline_time(datetime(2026, 9, 9, 12, 0, tzinfo=KST)) == datetime(2026, 9, 8, 20, 5, tzinfo=KST)
    assert baseline_time(datetime(2026, 9, 9, 20, 4, tzinfo=KST)) == datetime(2026, 9, 8, 20, 5, tzinfo=KST)
    assert baseline_time(datetime(2026, 9, 9, 20, 5, tzinfo=KST)) == datetime(2026, 9, 9, 20, 5, tzinfo=KST)
    assert baseline_time(datetime(2026, 9, 9, 23, 0, tzinfo=KST)) == datetime(2026, 9, 9, 20, 5, tzinfo=KST)


def _chart(points):
    return {"chart": {"result": [{"timestamp": [t for t, _ in points],
                                  "indicators": {"quote": [{"close": [c for _, c in points]}]}}]}}


def test_series_and_value_at():
    base = datetime(2026, 9, 8, 20, 5, tzinfo=KST)
    e = int(base.timestamp())
    pts = series(_chart([(e - 1800, 100.0), (e - 900, None), (e, 101.0), (e + 900, 103.0)]))
    assert pts == [(e - 1800, 100.0), (e, 101.0), (e + 900, 103.0)]
    assert value_at(pts, base) == (101.0, e)                       # 같은 시각 포함
    assert value_at(pts, datetime(2026, 9, 8, 20, 0, tzinfo=KST)) == (100.0, e - 1800)
    assert value_at(pts, datetime(2026, 9, 8, 19, 0, tzinfo=KST)) is None


def test_notes():
    rows = [Row("wti", "WTI", "CL=F", 90.0, 90.4, 0.42, 2, "oil", None, None),
            Row("usdkrw", "환율", "KRW=X", 1365.58, 1358.04, -0.55, 2, "fx", None, None),
            Row("vix", "VIX", "^VIX", 16.66, 15.2, -8.76, 2, "vix", None, None)]
    n = notes(rows)
    assert n[0].startswith("유가(WTI) +0.42% — 큰 변화 없음")
    assert "원화 강세" in n[1] and "공포 완화" in n[2]
