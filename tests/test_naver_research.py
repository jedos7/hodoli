from app.collectors.naver_research import parse_detail, parse_list

IND = """
<tr>
  <td style="padding-left:10">자동차</td>
  <td><a href="industry_read.naver?nid=46017&page=1">업사이드 리스크 vs 다운사이드 리스크</a><img src="x" class="ico_new" alt="NEW"></td>
  <td>대신증권</td>
  <td class="file"><a href="https://x.pdf"></a></td>
  <td class="date" style="padding-left:5px">26.09.09</td>
  <td class="date">841</td>
</tr>
<tr>
  <td style="padding-left:10">기타</td>
  <td><a href="industry_read.naver?nid=46016&page=1">안녕하세요 데일리에요(로봇/방산/조선)</a></td>
  <td>유진투자증권</td>
  <td class="file"></td>
  <td class="date" style="padding-left:5px">26.09.09</td>
  <td class="date">10</td>
</tr>
"""
ECO = """
<tr>
  <td><a href="economy_read.naver?nid=31000&page=1">환율과 금리</a></td>
  <td>KB증권</td>
  <td class="file"></td>
  <td class="date" style="padding-left:5px">26.09.08</td>
  <td class="date">10</td>
</tr>
"""
DETAIL = """
<div class="view_info"><div class="view_info_1">목표가 <em class="money"><strong>3,100,000</strong></em><span class="division">|</span>투자의견 <em class="coment">매수</em></div></div>
<td colspan="2" class="view_cnt"><div style="x">
<p><strong>이익 추정치 하향에도 절대적으로 낮은 밸류에이션</strong></p><p><br>동사에 대한 목표주가를 310만원(기존 280만원)으로 +10.7% 상향한다. 26F DRAM ASP 전망치를 상향했으나, 환율 하락을 반영해 EPS 는 소폭 하향. 그럼에도 밸류에이션은 절대적으로 낮다.</p>
</div><!-- 원문보기버튼 --><div style="TEXT-ALIGN: left"><a href="x.pdf">원문</a></div></td>
"""


def test_parse_lists():
    rs = parse_list(IND, "산업", True)
    assert len(rs) == 2 and rs[0].category == "자동차" and rs[0].title.startswith("업사이드") and rs[0].broker == "대신증권"
    assert rs[0].date == "2026-09-09" and rs[0].nid == 46017 and rs[0].url.endswith("industry_read.naver?nid=46017&page=1")
    es = parse_list(ECO, "경제", False)
    assert len(es) == 1 and es[0].title == "환율과 금리" and es[0].broker == "KB증권" and es[0].category == ""


def test_parse_detail_bullets_and_target():
    d = parse_detail(DETAIL)
    assert d["target"] == 3_100_000 and d["opinion"] == "매수"
    assert d["bullets"][0] == "이익 추정치 하향에도 절대적으로 낮은 밸류에이션"
    assert d["bullets"][1].startswith("동사에 대한 목표주가를 310만원") and len(d["bullets"]) <= 4
    assert not any("원문" in b for b in d["bullets"])
