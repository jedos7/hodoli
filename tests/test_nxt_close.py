from app.nxt_close import MIN_DIP, Candidate, alerts_for, score


def _c(code, name, why, close, amt, nx, move, near):
    return Candidate(code, name, why, close, 9.0, amt, nx, move, True, True, near).__dict__


def test_score_prefers_deep_dip_big_amount_and_hoga():
    deep_big = _c("A", "에이", "급등 +9.0%", 10000, 1500, 9600, -4.0, -4.0)
    shallow_small = _c("B", "비", "급등 +12.0%", 5000, 120, 4980, -0.4, 0.0)
    hoga = _c("C", "씨", "고가놀이", 20000, 300, 19700, -1.5, -0.5)
    assert score(deep_big)[0] > score(hoga)[0] > score(shallow_small)[0]
    assert any("검증 최상" in w for w in score(deep_big)[1]) and any("고가놀이" in w for w in score(hoga)[1])


def test_alerts_top3_single_message():
    picks = [_c(str(i), f"종목{i}", "급등 +9.0%", 10000, 100 + i * 300, 9800 - i * 50, -2 - i * 0.3, -1.0) for i in range(5)]
    res = {"at": "2026-09-09T19:50:00", "closing": True, "candidates": picks, "picks": picks}
    a = alerts_for(res)
    assert len(a) == 1 and a[0]["kind"] == "nxt"
    assert "상위 3" in a[0]["text"] and "외 2종목" in a[0]["text"] and len(a[0]["top"]) == 3
    assert a[0]["top"][0]["name"] == "종목4"        # 가장 많이 눌리고 대금이 큰 것이 1위
    none = alerts_for({"at": "2026-09-09T19:50:00", "closing": True, "candidates": [{}], "picks": []})
    assert "후보 없음" in none[0]["text"]


def test_shallow_dip_is_not_a_pick_and_message_says_so():
    # 2026-09-10 사례: -0.2~-1.1% 얕은 눌림만 있던 날 → 후보 없음 + 얕은 눌림 개수 안내
    shallow = [_c(str(i), f"종목{i}", "급등 +10.0%", 10000, 500, 9950, -0.5, -3.0) for i in range(3)]
    for c in shallow:
        c["pick"] = c["nx_move"] <= MIN_DIP
    assert not any(c["pick"] for c in shallow)
    res = {"at": "2026-09-10T19:50:00", "closing": True, "candidates": shallow, "picks": []}
    txt = alerts_for(res)[0]["text"]
    assert "후보 없음" in txt and "살짝 내린 종목 3개" in txt and "-2%" in txt


def test_alert_includes_market_risk_line():
    picks = [_c("A", "에이", "급등 +9.0%", 10000, 1500, 9600, -4.0, -4.0)]
    res = {"at": "2026-09-10T19:50:00", "closing": True, "candidates": picks, "picks": picks,
           "market": {"level": "주의", "note": "수량을 줄이세요", "moves": {"nq": -0.35, "es": -0.22, "cl": 1.79, "krw": 0.05}}}
    txt = alerts_for(res)[0]["text"]
    assert "🟠 시장 위험 [주의]" in txt and "나스닥선물 -0.35%" in txt
    assert "시장 위험" not in alerts_for({**res, "market": None})[0]["text"]
