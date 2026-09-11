from app.collectors.naver_research import Report, _api_path, parse_detail, parse_list

IND = [{"researchCategory": "산업분석", "category": "자동차", "researchId": 46017, "title": "업사이드 리스크 vs 다운사이드 리스크",
        "brokerName": "대신증권", "writeDate": "2026-09-09", "readCount": "841", "endUrl": "https://m.stock.naver.com/research/industry/46017"},
       {"researchCategory": "산업분석", "category": "기타", "researchId": 46016, "title": "안녕하세요 데일리에요(로봇/방산/조선)",
        "brokerName": "유진투자증권", "writeDate": "2026-09-09", "endUrl": "https://m.stock.naver.com/research/industry/46016"}]
COMP = [{"researchCategory": "종목분석", "itemCode": "112610", "itemName": "씨에스윈드", "researchId": 96103, "title": "무관심에서 관심의 영역으로",
         "brokerName": "DS투자증권", "writeDate": "2026-09-11", "endUrl": "https://m.stock.naver.com/research/company/96103"}]
DETAIL = {"researchContent": {"researchId": 96103, "content": "<p><strong>투자의견 매수, 목표주가 88,000원으로 상향</strong></p><p><br>씨에스윈드에 대해 투자의견 매수를 유지하고 목표주가는 88,000원으로 상향한다. "
                                                            "최근 시장의 관심이 높아지고 있다. 미국 풍력 타워 시장의 경쟁 구도가 바뀌고 있다.</p>"}}


def test_parse_lists():
    rows = parse_list(IND, "산업")
    assert len(rows) == 2 and rows[0].category == "자동차" and rows[0].broker == "대신증권" and rows[0].date == "2026-09-09" and rows[0].nid == 46017
    c = parse_list(COMP, "종목")[0]
    assert c.category == "씨에스윈드" and c.code == "112610" and c.kind == "종목"
    assert _api_path(c) == "/research/company/96103"
    assert _api_path(Report("종목", "", "", "", "", 5, "")) == "/research/company/5"


def test_parse_detail_bullets_and_target():
    d = parse_detail(DETAIL)
    assert d["target"] == 88000 and d["opinion"] == "매수"
    assert d["bullets"][0].startswith("투자의견 매수, 목표주가") and len(d["bullets"]) >= 2
    assert parse_detail({}) == {"bullets": [], "target": None, "opinion": ""}
