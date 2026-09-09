from app.collectors.naver_daily import parse_fchart, parse_last_page, parse_market_page

XML = """<?xml version="1.0" encoding="EUC-KR" ?>
<protocol><chartdata symbol="005930" name="삼성전자" count="3" timeframe="day">
<item data="20260903|254000|255000|243000|250000|13756022" />
<item data="20260904|254000|259000|252500|255500|14031862" />
<item data="20260907|||||" />
</chartdata></protocol>"""

PAGE = """
<tr  onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)">
  <td class="no">1</td>
  <td><a href="/item/main.naver?code=196170" class="tltle">알테오젠</a></td>
  <td class="number">276,500</td>
  <td class="number"><em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">500</span></td>
  <td class="number"><span class="tah p11 nv01">-0.18%</span></td>
  <td class="number">500</td>
  <td class="number">192,596</td>
  <td class="number">69,655</td>
  <td class="number">14.69</td>
  <td class="number">419,213</td>
  <td class="number">101.13</td>
  <td class="number">39.42</td>
</tr>
<tr  onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)">
  <td class="no">2</td>
  <td><a href="/item/main.naver?code=000001" class="tltle">삼성스팩9호</a></td>
  <td class="number">2,000</td><td class="number">0</td><td class="number">0.00%</td><td class="number">100</td>
  <td class="number">100</td><td class="number">100</td><td class="number">N/A</td><td class="number">1,000</td>
</tr>
<td class="pgRR"><a href="/sise/sise_market_sum.naver?sosok=1&page=37">맨뒤</a></td>
"""


def test_parse_fchart_skips_empty_bars():
    cs = parse_fchart(XML)
    assert [c.date for c in cs] == ["20260903", "20260904"]
    assert cs[0].open == 254000 and cs[0].high == 255000 and cs[0].low == 243000 and cs[0].close == 250000
    assert cs[0].volume == 13756022 and cs[0].amount == 13756022 * 250000


def test_parse_market_page():
    rows = parse_market_page(PAGE, "코스닥")
    assert len(rows) == 2
    a = rows[0]
    assert a.code == "196170" and a.name == "알테오젠" and a.price == 276500 and a.volume == 419213 and a.market == "코스닥"
    assert round(a.amount_eok, 1) == round(276500 * 419213 / 1e8, 1)
    assert parse_last_page(PAGE) == 37
