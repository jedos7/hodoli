import json

from app.watch import Watchlist


def test_breakout_then_stop_alerts_once_each():
    w = Watchlist()
    w.add("000001", "테스트", entry=10000, stop=9500)
    assert w.on_trade("000001", 9900) == []                        # 대기
    a = w.on_trade("000001", 10050)
    assert len(a) == 1 and a[0]["kind"] == "brk" and w.items["000001"].status == "broke"
    assert w.on_trade("000001", 10300) == []                       # 돌파 뒤 상승: 알림 없음, 최고가 갱신
    assert w.items["000001"].high_since == 10300
    a = w.on_trade("000001", 9400)
    assert len(a) == 1 and a[0]["kind"] == "stop" and w.items["000001"].status == "stopped"
    assert w.on_trade("000001", 9300) == []                        # 이탈 뒤 반복 알림 없음
    d = w.as_list()[0]
    assert d["riskPct"] == 5.3 and d["status"] == "stopped"


def test_stop_before_breakout_invalidates():
    w = Watchlist()
    w.add("000002", "둘", entry=20000, stop=19000)
    a = w.on_trade("000002", 18900)
    assert a[0]["kind"] == "stop" and w.items["000002"].status == "stopped"
    assert w.on_trade("000002", 20500) == []                       # 무효가 된 자리는 돌파해도 알림 없음


def test_load_from_pullback_keeps_state_and_manual(tmp_path):
    p = tmp_path / "pullback.json"
    rows = [{"date": "20260909", "code": "A", "name": "에이", "theme": "t", "entryPrice": 100, "stopPrice": 90, "strict": True},
            {"date": "20260909", "code": "B", "name": "비", "theme": "t", "entryPrice": 200, "stopPrice": 180, "strict": False},
            {"date": "20260908", "code": "C", "name": "씨", "theme": "t", "entryPrice": 300, "stopPrice": 270, "strict": True}]
    p.write_text(json.dumps({"asof": "20260909", "rows": rows}), "utf-8")
    w = Watchlist()
    w.add("M", "수동", 50, 45)
    assert w.load_pullback(p) == 2                                  # A(엄선·최신) + 수동 M. B 는 탈락, C 는 옛 자리
    assert set(w.codes) == {"A", "M"}
    w.on_trade("A", 101)
    assert w.items["A"].status == "broke"
    w.load_pullback(p)                                              # 같은 자리 다시 읽어도 상태 유지
    assert w.items["A"].status == "broke" and "M" in w.items
    # 확정일(asof)이 넘어갔는데 그날 엄선 자리가 없으면 옛 자리는 감시하지 않는다
    p.write_text(json.dumps({"asof": "20260910", "rows": rows}), "utf-8")
    assert w.load_pullback(p) == 1 and set(w.codes) == {"M"}
