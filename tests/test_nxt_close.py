from app.nxt_close import Candidate, alerts_for


def test_alerts_for_picks_and_none():
    picks = [Candidate("A", "에이", "급등 +9.0%", 10000, 9.0, 120, 9800, -2.0, True)]
    res = {"at": "2026-09-09T19:50:00", "closing": True, "candidates": [c.__dict__ for c in picks], "picks": [c.__dict__ for c in picks]}
    a = alerts_for(res)
    assert len(a) == 1 and a[0]["kind"] == "nxt" and "에이" in a[0]["text"] and "NXT 마감 9,800원 (-2.0%)" in a[0]["text"]
    none = alerts_for({"at": "2026-09-09T19:50:00", "closing": True, "candidates": [{}], "picks": []})
    assert "후보 없음" in none[0]["text"]
