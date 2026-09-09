from app.collectors.naver_theme import grade_of, parse_last_page, parse_theme_detail, parse_theme_list

LIST_HTML = """
<tr>
  <td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=586">통신장비(케이블/광섬유 등)</a></td>
  <td class="number col_type2">
    <span class="tah p11 red01">
    +9.46%
    </span>
  </td>
  <td class="number col_type3">
    <span class="tah p11 red01">
    +2.09%
    </span>
  </td>
  <td class="number col_type4">14</td>
  <td class="number col_type4">0</td>
  <td class="number col_type4">1</td>
  <td class="ls col_type5"><img src='x' alt='상승'><a href="/item/main.naver?code=100590">머큐리</a></td>
  <td class="ls col_type6"><img src='x' alt='상승'><a href="/item/main.naver?code=327260">RF시스템..</a></td>
</tr>
<tr>
  <td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=12">조선</a></td>
  <td class="number col_type2"><span class="tah p11 nv01">-1.20%</span></td>
  <td class="number col_type3"><span class="tah p11">0.00%</span></td>
  <td class="number col_type4">2</td>
  <td class="number col_type4">1</td>
  <td class="number col_type4">9</td>
  <td class="ls col_type5"><a href="/item/main.naver?code=042660">한화오션</a></td>
</tr>
<td class="pgRR"><a href="/sise/theme.naver?page=7">맨뒤</a></td>
"""

DETAIL_HTML = """
<tbody>
<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
  <td class="name"><div class="name_area"><a href="/item/main.naver?code=100590">머큐리</a> <span class="dot">*</span></div></td>
  <td><div class="theme_info_area"><a href="javascript:;" class="btn_history"><span class="blind">테마 편입 사유</span></a>
    <div class="info_layer_wrap"><strong class="info_title">머큐리</strong>
    <p class="info_txt">정보통신장비 개발, 생산, 판매업체. 광케이블을 직접 생산.</p></div></div></td>
  <td class="number" style="padding-right:15px;">4,315</td>
  <td class="number" style="padding-right:15px;"><em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
    965
    </span></td>
  <td class="number" style="padding-right:20px;"> <span class="tah p11 red01">
    +28.81%
    </span></td>
  <td class="number" style="padding-right:20px;">4,355</td>
  <td class="number" style="padding-right:20px;">0</td>
  <td class="number" style="padding-right:20px;">3,643,249</td>
  <td class="number" style="padding-right:20px;">15,350</td>
  <td class="number" style="padding-right:20px;">101,640</td>
  <td class="center"><a href="/item/board.naver?code=100590">토론</a></td>
</tr>
<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
  <td class="name"><div class="name_area"><a href="/item/main.naver?code=327260">RF시스템즈</a></div></td>
  <td></td>
  <td class="number">12,000</td>
  <td class="number"><em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">100</span></td>
  <td class="number"><span class="tah p11 nv01">-0.83%</span></td>
  <td class="number">11,990</td>
  <td class="number">12,000</td>
  <td class="number">50,000</td>
  <td class="number">600</td>
  <td class="number">40,000</td>
</tr>
</tbody>
"""


def test_parse_theme_list():
    rows = parse_theme_list(LIST_HTML)
    assert [r.no for r in rows] == [586, 12]
    a = rows[0]
    assert a.name == "통신장비(케이블/광섬유 등)" and a.chg == 9.46 and a.chg3d == 2.09
    assert (a.up, a.flat, a.down) == (14, 0, 1)
    assert a.leaders == [("100590", "머큐리"), ("327260", "RF시스템..")]
    assert rows[1].chg == -1.2 and rows[1].chg3d == 0.0 and rows[1].leaders == [("042660", "한화오션")]
    assert parse_last_page(LIST_HTML) == 7


def test_parse_theme_detail():
    rows = parse_theme_detail(DETAIL_HTML)
    assert len(rows) == 2
    m = rows[0]
    assert m.code == "100590" and m.name == "머큐리" and m.price == 4315 and m.chg == 28.81
    assert m.volume == 3_643_249 and m.amount_million == 15_350 and m.amount_eok == 153.5
    assert m.why.startswith("정보통신장비")
    assert m.prev_close == 3350  # 4315 / 1.2881
    r = rows[1]
    assert r.chg == -0.83 and r.amount_million == 600 and r.why == ""


def test_grade():
    assert grade_of(5.0, 0.9) == "A"
    assert grade_of(5.0, 0.3) == "B"
    assert grade_of(1.0, 0.9) == "B"
    assert grade_of(1.0, 0.3) == "C"
