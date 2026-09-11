from app.collectors.market_index import adr, parse_integration, parse_kiwoom_investors, parse_polling

POLL = {"datas": [
    {"itemCode": "KOSPI", "closePrice": "6,909.91", "compareToPreviousClosePrice": "-124.01", "fluctuationsRatio": "-1.76", "openPriceRaw": "6802.50",
     "highPriceRaw": "6948.83", "lowPriceRaw": "6802.50", "accumulatedTradingValue": "19,674,136백만", "marketStatus": "CLOSE", "localTradedAt": "2026-09-11T18:55:00+09:00"},
    {"itemCode": "KOSDAQ", "closePrice": "820.64", "fluctuationsRatio": "-1.95", "accumulatedTradingValue": "6,414,353백만", "localTradedAt": "2026-09-11T18:56:00+09:00"},
    {"itemCode": "KPI200", "closePrice": "1,090.22", "fluctuationsRatio": "-1.97"},
    {"itemCode": "FUT", "closePrice": "1,088.30", "fluctuationsRatio": "-2.13"},
    {"itemCode": "OTHER", "closePrice": "1"},
]}
INTEG = {"upDownStockInfo": {"upperCount": "1", "riseCount": "369", "lowerCount": "0", "fallCount": "475", "steadyCount": "73"},
         "dealTrendInfo": {"bizdate": "20260911", "personalValue": "+18,675", "foreignValue": "-22,984", "institutionalValue": "-12,184"},
         "programTrendInfo": {"indexTotalReal": "-18,128"}}
KW = {"inds_netprps": [{"ind_netprps": "+18675", "frgnr_netprps": "-23110", "orgn_netprps": "-12184", "sc_netprps": "-10345", "endw_netprps": "+2659", "invtrt_netprps": "-3367"}]}


def test_parse_polling():
    idx = parse_polling(POLL)
    assert set(idx) == {"KOSPI", "KOSDAQ", "KPI200", "FUT"}
    k = idx["KOSPI"]
    assert k["price"] == 6909.91 and k["chg"] == -1.76 and k["open"] == 6802.5 and k["amountJo"] == 19.67 and k["at"] == "18:55"
    assert idx["FUT"]["price"] - idx["KPI200"]["price"] < 0     # 백워데이션


def test_parse_integration_and_kiwoom():
    d = parse_integration(INTEG)
    assert (d["rise"], d["fall"], d["flat"], d["upper"]) == (369, 475, 73, 1)
    assert d["investors"] == [["개인", 18675.0], ["외인", -22984.0], ["기관", -12184.0]] and d["program"] == -18128.0
    assert parse_kiwoom_investors(KW, "KOSPI") == [["개인", 18675.0], ["외인", -23110.0], ["기관", -12184.0], ["금투", -10345.0], ["연기금", 2659.0]]
    assert parse_kiwoom_investors({"inds_netprps": []}, "KOSDAQ") is None


def test_adr_counts_up_and_down_days():
    c = {"A": [["d1", 0, 0, 0, 100, 0, 0], ["d2", 0, 0, 0, 110, 0, 0], ["d3", 0, 0, 0, 105, 0, 0]],
         "B": [["d1", 0, 0, 0, 50, 0, 0], ["d2", 0, 0, 0, 55, 0, 0], ["d3", 0, 0, 0, 55, 0, 0]],
         "C": [["d2", 0, 0, 0, 10, 0, 0], ["d3", 0, 0, 0, 9, 0, 0]]}
    v, days = adr(c, days=20)
    assert days == 2                       # d2, d3 (첫 봉은 전일이 없어 못 센다)
    assert v == 100.0                      # 상승 2 (A,B on d2) / 하락 2 (A,C on d3)
    assert adr({}, 20) == (None, 0)
