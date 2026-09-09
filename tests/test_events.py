from datetime import date

from app.events import Events, rule_events


def test_rule_events_september_2026():
    ev = rule_events(date(2026, 9, 9), date(2026, 10, 9))
    titles = {(e.date, e.title) for e in ev}
    assert ("2026-09-10", "선물·옵션 동시만기 (네 마녀)") in titles   # 9월 둘째 목요일
    assert ("2026-09-16", "FOMC 결과 발표") in titles
    assert ("2026-10-02", "미국 고용보고서(비농업)") in titles         # 10월 첫째 금요일
    assert ("2026-10-01", "ISM 제조업지수") in titles                  # 10월 첫 영업일
    assert ("2026-10-08", "코스피200 옵션 만기") in titles             # 10월 둘째 목요일
    assert ("2026-09-25", "미국 PCE 물가") in titles                   # 9월 마지막 금요일
    assert all(e.estimated for e in ev if "CPI" in e.title)


def test_manual_events_roundtrip(tmp_path):
    ev = Events(tmp_path / "events.json")
    e = ev.add("2026-09-24", "상장", "니어스랩 신규상장", "상장일 거래대금 분산", 2, "nv123")
    up = ev.upcoming(30, today=date(2026, 9, 9))
    mine = [x for x in up["items"] if x["source"] == "등록"]
    assert len(mine) == 1 and mine[0]["dday"] == 15 and mine[0]["weekday"] == "목" and mine[0]["themeId"] == "nv123"
    assert up["auto"] > 5
    assert ev.remove(e.id) and not ev.remove("nope")
    ev2 = Events(tmp_path / "events.json")
    assert ev2.manual == []
