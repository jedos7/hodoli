from app.kiwoom.parse import inum, num, parse_basic, parse_daily, parse_investor, parse_real
from app.state import MarketState, Stock, Theme


def test_signed_numbers():
    assert num("+71,500") == 71500 and num("-0.42") == -0.42 and num("--1200") == -1200 and num("") == 0 and num(None, 5) == 5
    assert inum("+300") == 300 and inum("-300") == -300


def test_parse_real_0b():
    msg = {"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": {
        "10": "+71500", "11": "+300", "12": "+0.42", "13": "123456", "14": "8800", "15": "-10",
        "16": "71200", "17": "71800", "18": "71000", "20": "093012", "228": "132.55"}},
        {"type": "0D", "item": "005930", "values": {}}]}
    ts = parse_real(msg, amount_unit=1_000_000)
    assert len(ts) == 1
    t = ts[0]
    assert t.code == "005930" and t.price == 71500 and t.change == 300 and t.change_rate == 0.42
    assert t.volume == 10 and t.acc_volume == 123456 and t.acc_amount == 8800 * 1_000_000 and t.cttr == 132.55
    assert t.time == "093012" and t.open == 71200 and t.high == 71800 and t.low == 71000
    assert parse_real({"trnm": "PING"}) == []
    nx = parse_real({"trnm": "REAL", "data": [{"type": "0B", "item": "005930_NX", "values": {"10": "-71000", "20": "183000"}}]})
    assert nx[0].code == "005930" and nx[0].market == "NX" and t.market == "KRX"


def test_parse_basic_and_prev_close():
    b = parse_basic({"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "-71500", "pred_pre": "-300", "flu_rt": "-0.42",
                     "trde_qty": "123456", "open_pric": "+71200", "high_pric": "+71800", "low_pric": "-71000"})
    assert b.price == 71500 and b.change == -300 and b.prev_close == 71800 and b.change_rate == -0.42 and b.amount == 0


def test_parse_daily_sorted_oldest_first():
    body = {"stk_dt_pole_chart_qry": [
        {"dt": "20260909", "cur_prc": "71500", "open_pric": "71200", "high_pric": "71800", "low_pric": "71000", "trde_qty": "100", "trde_prica": "7"},
        {"dt": "20260908", "cur_prc": "71000", "open_pric": "70000", "high_pric": "71500", "low_pric": "69900", "trde_qty": "90", "trde_prica": "6"},
    ]}
    cs = parse_daily(body, amount_unit=1_000_000)
    assert [c.date for c in cs] == ["20260908", "20260909"] and cs[1].close == 71500 and cs[1].amount == 7_000_000


def test_parse_investor_real_shape():
    # 2026-09-09 실응답: 금액 단위 백만원
    body = {"stk_invsr_orgn": [{"dt": "20260909", "cur_prc": "-268500", "pre_sig": "5", "pred_pre": "-1000", "flu_rt": "-37",
                                "acc_trde_qty": "13451782", "acc_trde_prica": "3648859", "ind_invsr": "0", "frgnr_invsr": "136412", "orgn": "211366"}]}
    r = parse_investor(body, amount_unit=1_000_000)
    assert r["date"] == "20260909" and r["price"] == 268500 and r["prev_close"] == 269500
    assert r["acc_amount"] == 3_648_859_000_000 and r["frgn"] == 136_412_000_000 and r["orgn"] == 211_366_000_000 and r["prsn"] == 0
    assert parse_investor({"stk_invsr_orgn": []}) is None


def test_real_tick_reaches_state_in_eok():
    """실시간 체결 한 건이 상태(억 단위)까지 들어가는 길 전체.

    2026-10-19 KIS 연동을 걷어내며 Trade.acc_amount_eok 를 빠뜨려 app/feeds.py 가 틱마다 AttributeError 를 냈고,
    그 예외가 WS 루프까지 올라가 **체결이 올 때마다 재접속**했다 (그날 12:55~18:50 사이 1만 번).
    같은 일이 다시 생기지 않게 원(acc_amount)과 억(acc_amount_eok) 두 단위를 함께 못 박는다."""
    msg = {"trnm": "REAL", "data": [{"type": "0B", "item": "005930", "values": {
        "10": "+71500", "12": "+0.42", "13": "123456", "14": "8800", "20": "093012", "228": "132.55"}}]}
    t, = parse_real(msg, amount_unit=1_000_000)
    assert t.acc_amount == 8_800_000_000 and t.acc_amount_eok == 88.0

    stock = Stock(code='005930', name='삼성전자', theme_id='semi', ref=71200)
    state = MarketState([Theme(id='semi', name='반도체', grade='A', report='', news='', stocks=[stock])])
    state.update(t.code, t.price, t.acc_amount_eok, change_rate=t.change_rate, cttr=t.cttr)
    s = state.stocks['005930']
    assert s.price == 71500 and s.acc_amount == 88.0 and s.cttr == 132.55
