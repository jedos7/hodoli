from app.collectors.naver_news import News, Report, parse_news, parse_reports, pick_news, report_line


def _n(title, at="2026-09-09 12:00"):
    return News(title, "언론", at, "https://x")


def test_pick_news_prefers_stock_specific_over_market_wrap():
    items = [_n("[이 시각 시황] 코스피 7000 재돌파…전력주 강세"),            # 최신이지만 시황
             _n("개인 팔자, 기관 사자…코스피 7100 공방전[장중시황]"),
             _n("심텍, 유리기판 시제품 공급 개시…목표가 상향", "2026-09-09 09:00")]
    assert pick_news(items, "심텍").title.startswith("심텍, 유리기판")


def test_is_fresh():
    from datetime import datetime

    from app.collectors.naver_news import is_fresh

    now = datetime(2026, 9, 9, 13, 0)
    assert is_fresh("2026-09-08 10:00", now=now)
    assert not is_fresh("2026-08-14 08:37", now=now)
    assert not is_fresh("", now=now)


def test_theme_keywords():
    from app.collectors.naver_news import theme_keywords

    kws = theme_keywords("광통신(광케이블/광섬유 등)", ["광케이블 및 광통신 장비 생산업체", "광섬유 기반 광케이블 제조", "통신장비 개발업체"])
    assert kws[:3] == ["광통신", "광케이블", "광섬유"]   # 테마명 토큰, '등' 은 제외
    assert "생산" not in kws and "및" not in kws           # 불용어 제외


def test_keyword_bonus_beats_unrelated_stock_article():
    from app.collectors.naver_news import news_score

    kws = ["마이크로", "LED"]
    unrelated = _n("LG전자 라이프굿 봉사단, 미얀마서 봉사활동")
    related = _n("LG전자, 마이크로 LED 사이니지 신제품 공개")
    assert news_score(related, "LG전자", kws) > news_score(unrelated, "LG전자", kws)
    assert pick_news([unrelated, related], "LG전자", kws) is related


def test_kw_hit_word_boundary():
    from app.collectors.naver_news import kw_hit

    assert not kw_hit("한국마이크로소프트, 30일 코엑스서 인더스트리 서밋 개최", "마이크로")
    assert kw_hit("LG전자, 마이크로 LED 사이니지 공개", "마이크로")
    assert kw_hit("신한운용, SOL AI반도체소부장 ETF 순자산 1조", "반도체")
    assert kw_hit("미 원전 건설 기대에 원전주 급등", "원전")


def test_pick_news_falls_back_to_latest_when_all_wrap():
    items = [_n("[마감시황] 코스피 상승"), _n("[개장시황] 코스닥 강세")]
    assert pick_news(items, "심텍").title == "[마감시황] 코스피 상승"
    assert pick_news([], "심텍") is None

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
