from app.collectors.naver_news import Report, parse_news, parse_reports, report_line

RESEARCH = """
<tr>
  <td style="padding-left:10"><a href="/item/main.naver?code=000660" title="SK하이닉스" class="stock_item">SK하이닉스</a></td>
  <td><a href="company_read.naver?nid=96025&page=1&searchType=itemCode&itemCode=000660">HBM 고객 다변화 본격화</a></td>
  <td>미래에셋증권</td>
  <td class="file"><a href="https://x/a.pdf" target="_blank"><img alt="pdf"></a></td>
  <td class="date" style="padding-left:5px">26.09.07</td>
  <td class="date">19112</td>
</tr>
<tr>
  <td style="padding-left:10"><a href="/item/main.naver?code=000660" class="stock_item">SK하이닉스</a></td>
  <td><a href="company_read.naver?nid=95869&page=1&searchType=itemCode&itemCode=000660">짙은 안개속 선명한 성장</a></td>
  <td>신한투자증권</td>
  <td class="file"></td>
  <td class="date" style="padding-left:5px">26.08.26</td>
  <td class="date">56062</td>
</tr>
"""

NEWS = [
    {"total": 1, "items": [{"officeName": "헤럴드경제", "datetime": "202609091255", "title": "짧은 제목", "titleFull": "SK하이닉스 “2028년 D램 공정에 ASML 차세대 노광장비 도입”", "mobileNewsUrl": "https://n.news.naver.com/mnews/article/016/0002694911"}]},
    {"total": 3, "items": [{"officeName": "뉴시스", "datetime": "202609091254", "title": "&quot;추석전 타결하나&quot; SK하이닉스 노조", "mobileNewsUrl": "https://n.news.naver.com/x"}, {"title": "묶음 2번째"}]},
]


def test_parse_reports():
    rs = parse_reports(RESEARCH)
    assert len(rs) == 2
    assert rs[0].title == "HBM 고객 다변화 본격화" and rs[0].broker == "미래에셋증권" and rs[0].date == "2026-09-07"
    assert rs[0].url.startswith("https://finance.naver.com/research/company_read.naver?nid=96025")
    assert rs[1].broker == "신한투자증권" and rs[1].date == "2026-08-26"


def test_parse_news_takes_cluster_head():
    ns = parse_news(NEWS)
    assert len(ns) == 2
    assert ns[0].title.startswith("SK하이닉스 “2028년") and ns[0].press == "헤럴드경제" and ns[0].at == "2026-09-09 12:55"
    assert ns[1].title.startswith('"추석전 타결하나"')


def test_report_line():
    assert report_line([], 7) == "리포트 7일 없음"
    rs = [Report("a", "미래에셋증권", "2026-09-07", ""), Report("b", "신한투자증권", "2026-09-05", ""), Report("c", "미래에셋증권", "2026-09-03", "")]
    assert report_line(rs, 7) == "리포트 7일 3건 · 증권사 2곳 · 최근 미래에셋 09/07"
