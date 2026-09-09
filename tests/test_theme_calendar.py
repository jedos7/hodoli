from app.kis.rest import Candle
from app.screener.theme_calendar import compute_days


def _cs(closes, amt=100):
    return [Candle(f"202609{i + 1:02d}", c, c, c, c, 1000, int(amt * 1e8)) for i, c in enumerate(closes)]


def test_compute_days_ranks_themes():
    tm = {"themes": {"1": {"name": "전선", "stocks": [{"code": "A", "name": "에이"}, {"code": "B", "name": "비"}, {"code": "C", "name": "씨"}]},
                     "2": {"name": "조선", "stocks": [{"code": "D", "name": "디"}, {"code": "E", "name": "이"}, {"code": "F", "name": "에프"}]},
                     "3": {"name": "작은테마", "stocks": [{"code": "A", "name": "에이"}]}}}
    candles = {"A": _cs([100, 110, 121]), "B": _cs([100, 105, 100]), "C": _cs([100, 102, 104]),
               "D": _cs([100, 99, 98]), "E": _cs([100, 100, 100]), "F": _cs([100, 101, 100])}
    days = compute_days(tm, candles, days=10)
    assert set(days) == {"20260902", "20260903"}
    d2 = days["20260902"]
    assert [t["name"] for t in d2["themes"]][:2] == ["전선", "조선"]      # 작은테마는 종목 3개 미만 → 제외
    t = d2["themes"][0]
    assert t["rep"] == "에이" and t["n"] == 3 and round(t["top4"], 2) == round((10 + 5 + 2) / 3, 2) and t["median"] == 5.0
    assert t["stocks"][0]["chg"] == 10.0 and t["stocks"][0]["price"] == 110 and t["amount"] == 300
    assert d2["themeCount"] == 2
