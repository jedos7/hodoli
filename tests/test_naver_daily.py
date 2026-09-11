from app.collectors.naver_daily import parse_fchart, parse_market_list

XML = """<?xml version="1.0" encoding="EUC-KR" ?>
<protocol><chartdata symbol="005930" name="삼성전자" count="3" timeframe="day">
<item data="20260903|254000|255000|243000|250000|13756022" />
<item data="20260904|254000|259000|252500|255500|14031862" />
<item data="20260907|||||" />
</chartdata></protocol>"""

PAGE = {"stockListSortType": "MARKET_VALUE", "stockListCategoryType": "KOSDAQ", "totalCount": 1800, "page": 1, "pageSize": 100,
        "stocks": [
            {"itemCode": "196170", "stockName": "알테오젠", "sosok": "1", "closePrice": "276,500", "accumulatedTradingVolume": "419,213",
             "accumulatedTradingValue": "115,913", "accumulatedTradingValueRaw": "115913000000", "stockEndType": "stock", "tradeStopType": {"name": "TRADING"}},
            {"itemCode": "000001", "stockName": "삼성스팩9호", "sosok": "1", "closePrice": "2,000", "accumulatedTradingVolume": "100",
             "accumulatedTradingValue": "0", "stockEndType": "stock"},
            {"itemCode": "999999", "stockName": "정지종목", "closePrice": "1,000", "accumulatedTradingVolume": "0", "stockEndType": "stock", "tradeStopType": {"name": "STOP"}},
            {"itemCode": "KOSDAQ", "stockName": "지수", "closePrice": "820", "stockEndType": "index"},
        ]}


def test_parse_fchart_skips_empty_bars():
    cs = parse_fchart(XML)
    assert [c.date for c in cs] == ["20260903", "20260904"]
    assert cs[0].open == 254000 and cs[0].high == 255000 and cs[0].low == 243000 and cs[0].close == 250000
    assert cs[0].volume == 13756022 and cs[0].amount == 13756022 * 250000


def test_parse_market_list():
    rows = parse_market_list(PAGE, "코스닥")
    assert [r.code for r in rows] == ["196170", "000001"]      # 거래정지·지수 행은 뺀다
    a = rows[0]
    assert a.name == "알테오젠" and a.price == 276500 and a.volume == 419213 and a.market == "코스닥"
    assert a.amount_million == 115_913 and a.amount_eok == 1159.13      # API 거래대금(백만) 우선
    assert rows[1].amount_eok == 2000 * 100 / 1e8                        # 거래대금 없으면 현재가×거래량
