from datetime import date

from app.collectors.naver_futures import foreign_summary, parse_rows

PAGE = """
<table class="type_1"><tr><th>날짜</th><th>개인</th><th>외국인</th><th>기관계</th></tr>
<tr><td class="date">26.09.09</td><td class="number"><span>-803</span></td><td class="number">3,312</td><td class="number">-2,538</td><td>-1,847</td></tr>
<tr><td class="date">26.09.08</td><td>2,905</td><td>1,288</td><td>-4,553</td><td>-2,561</td></tr>
<tr><td class="date">26.09.02</td><td>1,792</td><td>-8,107</td><td>6,014</td><td>10,042</td></tr>
</table>
"""


def test_parse_rows():
    rows = parse_rows(PAGE)
    assert [(r.date, r.prsn, r.frgn, r.orgn) for r in rows] == [
        ("2026-09-09", -803, 3312, -2538), ("2026-09-08", 2905, 1288, -4553), ("2026-09-02", 1792, -8107, 6014)]


def test_summary_intraday_and_after_close():
    rows = parse_rows(PAGE)
    s = foreign_summary(rows, today=date(2026, 9, 9))
    assert s["prev"] == 1288 and s["now"] == 3312 and "09/09 잠정" in s["src"]
    s = foreign_summary(rows, today=date(2026, 9, 10))       # 다음날 장 전: 당일 행 없음 → 전일 확정만
    assert s["prev"] == 3312 and s["now"] is None and "09/09 확정" in s["src"]
    assert foreign_summary([], today=date(2026, 9, 9))["error"]
