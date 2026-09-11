from app.collectors.naver_theme import grade_of, parse_theme_detail, parse_theme_list

LIST_JSON = {"stockListSortType": "THEME", "totalCount": 266, "page": 1, "pageSize": 100,
             "groups": [{"no": 405, "name": "MLCC(적층세라믹콘덴서)", "totalCount": 11, "changeRate": "7.69", "riseCount": 8, "fallCount": 2, "steadyCount": 1},
                        {"no": 12, "name": "조선", "totalCount": 12, "changeRate": "-1.20", "riseCount": 2, "fallCount": 9, "steadyCount": 1}]}

DETAIL_JSON = {
    "stocks": [
        {"itemCode": "052710", "stockName": "아모텍", "sosok": "1", "closePrice": "15,130", "fluctuationsRatio": "29.98",
         "accumulatedTradingVolume": "3,396,316", "accumulatedTradingValue": "49,791", "accumulatedTradingValueRaw": "49791000000", "stockEndType": "stock"},
        {"itemCode": "009150", "stockName": "삼성전기", "sosok": "0", "closePrice": "412,000", "fluctuationsRatio": "-0.83",
         "accumulatedTradingVolume": "50,000", "accumulatedTradingValue": "600", "stockEndType": "stock"},
    ],
    "groupInfo": {"no": 405, "name": "MLCC(적층세라믹콘덴서)"},
    "themeItemInfoMap": {"052710": "신소재를 바탕으로 한 종합부품소재기업. MLCC를 새로운 사업 아이템으로 양산중."},
}


def test_parse_theme_list():
    rows = parse_theme_list(LIST_JSON)
    assert [r.no for r in rows] == [405, 12]
    a = rows[0]
    assert a.name == "MLCC(적층세라믹콘덴서)" and a.chg == 7.69 and a.chg3d == 0.0
    assert (a.up, a.flat, a.down) == (8, 1, 2) and a.leaders == []
    assert rows[1].chg == -1.2


def test_parse_theme_detail():
    rows = parse_theme_detail(DETAIL_JSON)
    assert len(rows) == 2
    m = rows[0]
    assert m.code == "052710" and m.name == "아모텍" and m.price == 15130 and m.chg == 29.98
    assert m.volume == 3_396_316 and m.amount_million == 49_791 and m.amount_eok == 497.91
    assert m.why.startswith("신소재")
    assert m.prev_close == 11640  # 15130 / 1.2998
    r = rows[1]
    assert r.chg == -0.83 and r.amount_million == 600 and r.why == ""   # Raw 없으면 '백만' 표시값


def test_grade():
    assert grade_of(5.0, 0.9) == "A"
    assert grade_of(5.0, 0.3) == "B"
    assert grade_of(1.0, 0.9) == "B"
    assert grade_of(1.0, 0.3) == "C"
