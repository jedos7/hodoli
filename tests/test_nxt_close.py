from app.nxt_close import Candidate, alerts_for, score


def _c(code, name, why, close, amt, nx, move, near):
    return Candidate(code, name, why, close, 9.0, amt, nx, move, True, True, near).__dict__


def test_score_prefers_deep_dip_big_amount_and_hoga():
    deep_big = _c("A", "에이", "급등 +9.0%", 10000, 1500, 9600, -4.0, -4.0)
    shallow_small = _c("B", "비", "급등 +12.0%", 5000, 120, 4980, -0.4, 0.0)
    hoga = _c("C", "씨", "고가놀이", 20000, 300, 19700, -1.5, -0.5)
    assert score(deep_big)[0] > score(hoga)[0] > score(shallow_small)[0]
    assert any("검증 최상" in w for w in score(deep_big)[1]) and any("고가놀이" in w for w in score(hoga)[1])


def test_alerts_top3_single_message():
    picks = [_c(str(i), f"종목{i}", "급등 +9.0%", 10000, 100 + i * 300, 9900 - i * 50, -(i + 1) * 0.6, -1.0) for i in range(5)]
    res = {"at": "2026-09-09T19:50:00", "closing": True, "candidates": picks, "picks": picks}
    a = alerts_for(res)
    assert len(a) == 1 and a[0]["kind"] == "nxt"
    assert "상위 3" in a[0]["text"] and "외 2종목" in a[0]["text"] and len(a[0]["top"]) == 3
    assert a[0]["top"][0]["name"] == "종목4"        # 가장 많이 눌리고 대금이 큰 것이 1위
    none = alerts_for({"at": "2026-09-09T19:50:00", "closing": True, "candidates": [{}], "picks": []})
    assert "후보 없음" in none[0]["text"]
